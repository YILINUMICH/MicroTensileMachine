/*
 * ADS1263_NoiseFloor_PIO — full SPS × PGA noise-floor sweep
 *
 * Phase 1.2 of doc/MEMO_baseline_testing.md.
 *
 * Target hardware:  Arduino Portenta H7 (ABX00042)
 *                 + Arduino Portenta Mid Carrier (ASX00055)
 *                 + TI ADS1263 EVM (6-wire SPI per doc/MEMO_cable_map.md)
 *                 + TI REF7050 external 5 V reference on AIN0/AIN1
 *
 * Premise:
 *   Bring-up (ADS1263_FirstPowerUp_PIO) established the chip is alive at
 *   ONE operating point (400 SPS, PGA bypass, AINCOM-shorted, 1.4 µV RMS).
 *   That's one cell of the chip's full noise-vs-mode surface. This sketch
 *   walks the rest: for every combination of (DR ∈ {10, 50, 100, 400,
 *   1200, 2400, 4800} SPS) × (PGA ∈ {1, 2, 4, 8, 16, 32}), it collects N
 *   samples with the inputs internally shorted (INPMUX = 0xAA → both ADC
 *   differential inputs routed to AINCOM, biased to mid-supply via the
 *   VBIAS function), computes mean / RMS / pk-pk / noise-free bits, and
 *   prints a CSV row.
 *
 *   The output CSV is meant to be captured to a file and post-processed
 *   by tools/analyze_noise_floor.py — that script compares each cell
 *   against the datasheet's typical noise spec (Table 7.10) and flags
 *   anything more than 1.5× above expected.
 *
 * Why some operating modes are deliberately excluded:
 *
 *   - PGA bypass mode (MODE2 bit 7 = 1) is NOT swept here. The bring-up
 *     covered that one point; what we don't know is how the PGA-enabled
 *     path behaves across gains and rates. PGA bypass is a fallback
 *     that the rest of the rig doesn't need.
 *
 *   - SPS > 4800 is NOT swept because RDATA1 at 500 kHz SPI takes
 *     ~112 µs per transaction. The shortest period in the table
 *     (4800 SPS = 208 µs) leaves ~96 µs of margin per sample — fine.
 *     7200 SPS (139 µs) and 14400 SPS (69 µs) would either alias or
 *     require bumping SPI to 1 MHz+. Worth doing in a follow-up sketch
 *     after the link is proven clean at the cap we trust.
 *
 *   - Filter mode (Sinc1/2/3/4/FIR — MODE1 register) is NOT swept here.
 *     Default Sinc3 is what every downstream driver uses. Other filters
 *     are a separate question.
 *
 * What to do with the output:
 *
 *   1. Capture serial to a file:
 *        pio device monitor 2>&1 | tee data/noisefloor_$(date +%Y%m%d_%H%M).csv
 *      (or use pio device monitor's --output flag).
 *
 *   2. Strip the leading comment lines (anything starting with #):
 *        grep -v '^#' data/noisefloor_*.csv > data/noisefloor_clean.csv
 *
 *   3. Feed to the analysis script:
 *        python3 tools/analyze_noise_floor.py data/noisefloor_clean.csv
 *
 * Author: Yilin Ma — HDR Lab, University of Michigan
 */

#include <Arduino.h>
#include <SPI.h>
#include <math.h>

// =====================================================================
// PIN DEFINES — match ADS1263_FirstPowerUp_PIO exactly. See cable map.
// =====================================================================
#define PIN_CS     PA_8       // J15-25 → J2-59 → PA8,  /CS
#define PIN_DRDY   PC_6       // J15-27 → J2-61 → PC6,  /DRDY (not gated here)
#define PIN_RESET  PC_7       // J15-29 → J2-63 → PC7,  /RESET

// =====================================================================
// ADS1263 commands & registers (datasheet, see doc/)
// =====================================================================
#define ADS1263_CMD_RESET   0x06
#define ADS1263_CMD_START1  0x08
#define ADS1263_CMD_STOP1   0x0A
#define ADS1263_CMD_RREG    0x20
#define ADS1263_CMD_WREG    0x40
#define ADS1263_CMD_RDATA1  0x12

#define ADS1263_REG_ID         0x00
#define ADS1263_REG_POWER      0x01
#define ADS1263_REG_INTERFACE  0x02
#define ADS1263_REG_MODE0      0x03
#define ADS1263_REG_MODE1      0x04
#define ADS1263_REG_MODE2      0x05
#define ADS1263_REG_INPMUX     0x06
#define ADS1263_REG_REFMUX     0x0F

#define ADS1263_EXPECTED_ID_UPPER_5BITS  0x20

// SPI settings — same 500 kHz as the rest of the rig. Faster would
// reduce per-sample SPI overhead but introduces a new variable; keep
// it at the verified speed.
static const SPISettings SPI_CFG(500000, MSBFIRST, SPI_MODE1);

// =====================================================================
// Sweep table: (SPS, DR_code) and PGA gains
// =====================================================================
struct SpsEntry {
    uint8_t  dr_code;       // bits 3:0 of MODE2
    uint16_t sps;           // nominal samples-per-second
    uint32_t period_us;     // 1e6 / sps, rounded
};

static const SpsEntry SPS_TABLE[] = {
    { 0x02,    10, 100000 },    // 10 SPS
    { 0x05,    50,  20000 },    // 50 SPS
    { 0x07,   100,  10000 },    // 100 SPS
    { 0x08,   400,   2500 },    // 400 SPS (the bring-up point)
    { 0x09,  1200,    833 },    // 1200 SPS
    { 0x0A,  2400,    417 },    // 2400 SPS
    { 0x0B,  4800,    208 },    // 4800 SPS — near the 500 kHz SPI throughput limit
};
static const size_t N_SPS = sizeof(SPS_TABLE) / sizeof(SPS_TABLE[0]);

static const uint8_t GAIN_CODES [] = { 0, 1, 2, 3,  4,  5 };   // MODE2 bits 6:4
static const uint8_t GAIN_VALUES[] = { 1, 2, 4, 8, 16, 32 };
static const size_t N_GAINS = sizeof(GAIN_VALUES) / sizeof(GAIN_VALUES[0]);

// Sample buffer — sized for the largest N we'll capture per point.
// At 4800 SPS we collect 2000 samples = 8 KB on the M7's heap stack
// (codes + sums fit comfortably in static memory).
#define N_MAX 2000
static int32_t codes[N_MAX];

// =====================================================================
// ADS1263 low-level helpers — identical to FirstPowerUp's so any future
// driver consolidation merges cleanly.
// =====================================================================
static uint8_t ads_read_reg(uint8_t addr) {
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(ADS1263_CMD_RREG | (addr & 0x1F));
    SPI.transfer(0x00);
    uint8_t v = SPI.transfer(0x00);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    return v;
}

static void ads_write_reg(uint8_t addr, uint8_t val) {
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(ADS1263_CMD_WREG | (addr & 0x1F));
    SPI.transfer(0x00);
    SPI.transfer(val);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
}

static void ads_command(uint8_t cmd) {
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(cmd);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
}

static void ads_read_conversion(int32_t *out_code) {
    uint8_t frame[6];
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(ADS1263_CMD_RDATA1);
    for (int i = 0; i < 6; i++) frame[i] = SPI.transfer(0x00);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    uint32_t raw = ((uint32_t)frame[1] << 24)
                 | ((uint32_t)frame[2] << 16)
                 | ((uint32_t)frame[3] << 8)
                 |  (uint32_t)frame[4];
    *out_code = (int32_t)raw;
}

// =====================================================================
// Per-cell measurement — runs one (SPS, gain) point and prints a CSV row
// =====================================================================
static void measure_point(const SpsEntry &sps_e, uint8_t gain_code, uint8_t gain) {
    uint8_t mode2 = (uint8_t)((gain_code << 4) | sps_e.dr_code);
    ads_write_reg(ADS1263_REG_MODE2, mode2);
    ads_command(ADS1263_CMD_START1);

    // Settling: 4 conversion periods + 50 ms floor + 200 ms ceiling.
    // (The Sinc3 filter needs ~4 conversions to fully settle.)
    uint32_t settle_us = 4UL * sps_e.period_us + 50000UL;
    if (settle_us > 200000UL) settle_us = 200000UL;
    delayMicroseconds(settle_us);

    // Sample count: target ~10 seconds of data, capped at N_MAX,
    // floored at 200 (RMS estimator's relative uncertainty at N=200
    // is roughly 1/sqrt(2N) ≈ 5%).
    int N = (int)((uint32_t)10 * sps_e.sps);
    if (N < 200)   N = 200;
    if (N > N_MAX) N = N_MAX;

    // Timed sampling using micros() as the absolute reference. The
    // `(int32_t)(micros() - t_next) < 0` trick handles 32-bit rollover
    // for any actual interval < 2^31 µs ≈ 35 min.
    int stuck = 0;
    uint32_t t_next = micros();
    for (int i = 0; i < N; i++) {
        while ((int32_t)(micros() - t_next) < 0) { /* spin */ }
        ads_read_conversion(&codes[i]);
        if (i > 0 && codes[i] == codes[i-1]) stuck++;
        t_next += sps_e.period_us;
    }

    // Statistics — two-pass to keep double-precision accumulation honest
    // when raw codes can hit ±2^31 ≈ ±2.1e9.
    double  sum = 0.0;
    int32_t lo  = codes[0];
    int32_t hi  = codes[0];
    for (int i = 0; i < N; i++) {
        sum += (double)codes[i];
        if (codes[i] < lo) lo = codes[i];
        if (codes[i] > hi) hi = codes[i];
    }
    double mean_code = sum / (double)N;

    double var = 0.0;
    for (int i = 0; i < N; i++) {
        double d = (double)codes[i] - mean_code;
        var += d * d;
    }
    double rms_code  = sqrt(var / (double)N);
    double pkpk_code = (double)(hi - lo);

    // Volts-per-code: code is signed 32-bit, full-scale ±2^31 corresponds
    // to ±VREF/gain at the input. Output-referred numbers use VREF/2^31
    // directly; input-referred numbers divide by gain.
    const double VREF = 5.0;
    const double LSB  = VREF / 2147483648.0;
    double out_mean_uV = mean_code  * LSB * 1e6;
    double out_rms_uV  = rms_code   * LSB * 1e6;
    double out_pkpk_uV = pkpk_code  * LSB * 1e6;
    double in_mean_uV  = out_mean_uV / (double)gain;
    double in_rms_uV   = out_rms_uV  / (double)gain;
    double in_pkpk_uV  = out_pkpk_uV / (double)gain;

    // Noise-free bits, datasheet §6.5 convention:
    //   NFR = log2( 2 * full_scale_code / pkpk_code )
    //       = log2( 2^32 / pkpk_code )
    //       = 32 - log2(pkpk_code)
    // Floor at 0 to avoid -inf when pkpk is somehow zero.
    double nfb = (pkpk_code > 0.5) ? (32.0 - log(pkpk_code) / log(2.0)) : 32.0;
    if (nfb < 0.0) nfb = 0.0;

    double stuck_pct = 100.0 * (double)stuck / (double)(N > 1 ? (N - 1) : 1);

    // CSV row. Columns are documented in the header line printed once
    // at the top of setup().
    char buf[256];
    snprintf(buf, sizeof(buf),
             "%u,0x%02X,%u,%u,0x%02X,%d,%lu,"
             "%.4f,%.4f,%.4f,"
             "%.4f,%.4f,%.4f,"
             "%.3f,%.2f",
             sps_e.sps, sps_e.dr_code, gain, gain_code, mode2, N,
             (unsigned long)sps_e.period_us,
             out_mean_uV, out_rms_uV, out_pkpk_uV,
             in_mean_uV,  in_rms_uV,  in_pkpk_uV,
             nfb, stuck_pct);
    Serial.println(buf);
}

// =====================================================================
// Setup / loop
// =====================================================================
void setup() {
    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) { /* wait for USB CDC */ }

    Serial.println();
    Serial.println(F("# ============================================================"));
    Serial.println(F("#   ADS1263 noise-floor sweep — Phase 1.2"));
    Serial.println(F("# ============================================================"));
    Serial.println(F("# Hardware:  Portenta H7 + Mid Carrier + ADS1263 EVM"));
    Serial.println(F("# Reference: TI REF7050 (5.000 V) on AIN0/AIN1, REFMUX = 0x09"));
    Serial.println(F("# Input:     AINCOM-shorted via INPMUX = 0xAA"));
    Serial.println(F("# Bias:      VBIAS on (POWER bit 1) → AINCOM at +2.5 V"));
    Serial.println(F("# Filter:    Sinc3 (MODE1 default), no chop, no FIR"));
    Serial.println(F("# SPI:       500 kHz, MODE1"));
    Serial.println(F("# Sweep:     SPS ∈ {10, 50, 100, 400, 1200, 2400, 4800}"));
    Serial.println(F("#            × PGA ∈ {1, 2, 4, 8, 16, 32}"));
    Serial.println(F("# Samples:   N = clamp(10·SPS, 200, 2000)"));
    Serial.println(F("# See:       doc/MEMO_baseline_testing.md, ADS1263 datasheet Tbl 7.10"));
    Serial.println(F("# ============================================================"));

    // -------------------- Bring-up subset (minimal) --------------------
    // Same pin defines as FirstPowerUp; same /RESET pulse, same ID check.
    // If anything fails here, run ADS1263_FirstPowerUp_PIO/ first — its
    // checkpoints will localize the failure.

    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LOW);

    pinMode(PIN_CS,    OUTPUT);
    pinMode(PIN_RESET, OUTPUT);
    pinMode(PIN_DRDY,  INPUT);
    digitalWrite(PIN_CS,    HIGH);
    digitalWrite(PIN_RESET, HIGH);

    digitalWrite(PIN_RESET, LOW);
    delay(100);
    digitalWrite(PIN_RESET, HIGH);
    delay(50);

    SPI.begin();
    delay(3000);                           // power-up settle per integration notes
    ads_command(ADS1263_CMD_RESET);
    delay(50);

    uint8_t id = ads_read_reg(ADS1263_REG_ID);
    Serial.print(F("# ADS1263 ID register = 0x"));
    Serial.println(id, HEX);
    if ((id & 0xF8) != ADS1263_EXPECTED_ID_UPPER_5BITS) {
        Serial.println(F("# FAIL: ADS1263 not responding correctly."));
        Serial.println(F("#       Run ADS1263_FirstPowerUp_PIO/ to localize."));
        while (1) {
            digitalWrite(LED_BUILTIN, HIGH); delay(150);
            digitalWrite(LED_BUILTIN, LOW);  delay(150);
        }
    }

    // -------------------- Sweep configuration --------------------
    // REFMUX = external 5 V (AIN0+, AIN1−)
    ads_write_reg(ADS1263_REG_REFMUX, 0x09);
    // INPMUX = AINCOM-shorted (both ADC inputs internally routed to AINCOM)
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);
    // POWER: enable VBIAS while preserving INTREF (so REFOUT pin still has
    // its 2.5 V — not strictly needed here but doesn't hurt)
    uint8_t pwr_before = ads_read_reg(ADS1263_REG_POWER);
    ads_write_reg(ADS1263_REG_POWER, (uint8_t)(pwr_before | 0x02));
    delay(5);                              // VBIAS settle
    uint8_t pwr_after = ads_read_reg(ADS1263_REG_POWER);
    if ((pwr_after & 0x02) == 0) {
        Serial.println(F("# FAIL: VBIAS bit did not stick — POWER write failed."));
        Serial.println(F("#       Run ADS1263_FirstPowerUp_PIO/ cp6 to triage."));
        while (1) {
            digitalWrite(LED_BUILTIN, HIGH); delay(150);
            digitalWrite(LED_BUILTIN, LOW);  delay(150);
        }
    }
    Serial.print(F("# POWER: 0x")); Serial.print(pwr_before, HEX);
    Serial.print(F(" → 0x"));        Serial.println(pwr_after,  HEX);

    // INTERFACE: keep the chip's default 0x05 (STATUS + CRC) — we read
    // the 6-byte frame anyway in ads_read_conversion().
    uint8_t intf = ads_read_reg(ADS1263_REG_INTERFACE);
    Serial.print(F("# INTERFACE = 0x")); Serial.println(intf, HEX);

    // -------------------- CSV header --------------------
    Serial.println(F("#"));
    Serial.println(F("# CSV columns:"));
    Serial.println(F("#   sps          configured data rate (SPS) — nominal value"));
    Serial.println(F("#   sps_code     MODE2 bits 3:0 (DR field) hex value"));
    Serial.println(F("#   gain         PGA gain (V/V)"));
    Serial.println(F("#   gain_code    MODE2 bits 6:4 (GAIN field) hex value"));
    Serial.println(F("#   mode2        full MODE2 register value (bypass=0, PGA on)"));
    Serial.println(F("#   n_samples    number of samples averaged for this point"));
    Serial.println(F("#   period_us    1/sps in microseconds (sample-loop target)"));
    Serial.println(F("#   out_mean_uV  ADC-output-referred mean voltage in µV"));
    Serial.println(F("#   out_rms_uV   ADC-output-referred RMS noise in µV"));
    Serial.println(F("#   out_pkpk_uV  ADC-output-referred peak-to-peak noise in µV"));
    Serial.println(F("#   in_mean_uV   input-referred mean (output / gain) in µV"));
    Serial.println(F("#   in_rms_uV    input-referred RMS (output / gain) — compare to datasheet Tbl 7.10"));
    Serial.println(F("#   in_pkpk_uV   input-referred peak-to-peak"));
    Serial.println(F("#   nfb          noise-free bits = 32 − log2(pkpk_code)"));
    Serial.println(F("#   stuck_pct    % of samples identical to the previous "
                     "(non-zero suggests SPI polling > conversion rate)"));
    Serial.println(F("#"));
    Serial.println(F("sps,sps_code,gain,gain_code,mode2,n_samples,period_us,"
                     "out_mean_uV,out_rms_uV,out_pkpk_uV,"
                     "in_mean_uV,in_rms_uV,in_pkpk_uV,"
                     "nfb,stuck_pct"));

    // -------------------- The actual sweep --------------------
    digitalWrite(LED_BUILTIN, HIGH);       // LED on during sweep
    for (size_t si = 0; si < N_SPS; si++) {
        for (size_t gi = 0; gi < N_GAINS; gi++) {
            measure_point(SPS_TABLE[si], GAIN_CODES[gi], GAIN_VALUES[gi]);
        }
    }
    digitalWrite(LED_BUILTIN, LOW);

    Serial.print(F("# Sweep complete. "));
    Serial.print((unsigned)(N_SPS * N_GAINS));
    Serial.println(F(" points emitted."));
    Serial.println(F("# Capture the CSV above (strip lines starting with #),"));
    Serial.println(F("# then run:  python3 tools/analyze_noise_floor.py <file>"));
}

void loop() {
    // Slow heartbeat — sweep is done, just signal alive.
    digitalWrite(LED_BUILTIN, HIGH); delay(1500);
    digitalWrite(LED_BUILTIN, LOW);  delay(1500);
}
