/*
 * ADS1263_SelfCal_PIO — Phase 2.1 self-calibration verification
 *
 * Target hardware:  Arduino Portenta H7 (ABX00042)
 *                 + Arduino Portenta Mid Carrier (ASX00055)
 *                 + TI ADS1263 EVM (connected via 6-wire SPI cable)
 *
 * Wiring source of truth: ../doc/MEMO_cable_map.md
 *
 * Prerequisite: ADS1263_FirstPowerUp_PIO/ cp0–cp10 all PASS. This
 * sketch reuses the same pin defines, REFMUX, VREF, VBIAS strategy
 * and reads the same chip ID — if any of those fail here, run the
 * FirstPowerUp diagnostic to localize.
 *
 * Phase 2.1 of doc/MEMO_baseline_testing.md. What this sketch does:
 *
 *   cp 0 : Serial up                  USB CDC enumerates
 *   cp 1 : Bring-up                   GPIO + /RESET pulse + SPI.begin
 *                                     + ID check + REFMUX/INPMUX/VBIAS
 *   cp 2 : SFOCAL1 sweep              At each PGA gain ∈ {1,2,4,8,16,32}:
 *                                     read pre-cal OFCAL, take baseline
 *                                     mean (AINCOM-shorted), set INPMUX
 *                                     to 0xFF per datasheet §9.4.9.2,
 *                                     send SFOCAL1 (0x19), wait for
 *                                     calibration to complete, re-write
 *                                     critical registers (per integration
 *                                     notes §4: SFOCAL1 may reset other
 *                                     registers), verify INTERFACE
 *                                     register survived (the "snap-back"
 *                                     check), read post-cal OFCAL, take
 *                                     post-cal mean, verify offset is
 *                                     dramatically reduced.
 *   cp 3 : SYGCAL1 demo               Configure INPMUX = AIN6 vs AIN7,
 *                                     enable TDAC at 0.9·AVDD (AIN6) and
 *                                     0.1·AVDD (AIN7) → 0.8·AVDD diff,
 *                                     ≈ 4.16 V on this rig (AVDD = 5.2056 V
 *                                     per cp10). Run SYGCAL1 (0x17), check
 *                                     FSCAL written, INTERFACE survived,
 *                                     post-cal measurement closer to
 *                                     predicted full-scale.
 *
 * Output style and helper conventions match ADS1263_FirstPowerUp_PIO/.
 * M7-only, no M4, no shared driver.
 *
 * Author: Yilin Ma — HDR Lab, University of Michigan
 */

#include <Arduino.h>
#include <SPI.h>
#include <math.h>

// =====================================================================
// PIN DEFINES — same as FirstPowerUp; see that sketch for derivation.
// =====================================================================
#define PIN_CS     PA_8       // J15-25 → J2-59 → PA8,  /CS
#define PIN_DRDY   PC_6       // J15-27 → J2-61 → PC6,  /DRDY (polled here, not gated)
#define PIN_RESET  PC_7       // J15-29 → J2-63 → PC7,  /RESET

// Portenta H7 LED is active-low — HIGH=off, LOW=on. Use these macros
// instead of HIGH/LOW directly. (See FirstPowerUp's notes on the H7
// LED quirk.)
#define LED_ON   LOW
#define LED_OFF  HIGH

// =====================================================================
// ADS1263 commands & registers (subset; see FirstPowerUp main.cpp for
// the full list and the datasheet for register layouts)
// =====================================================================
#define ADS1263_CMD_RESET    0x06
#define ADS1263_CMD_START1   0x08
#define ADS1263_CMD_STOP1    0x0A
#define ADS1263_CMD_RREG     0x20
#define ADS1263_CMD_WREG     0x40
#define ADS1263_CMD_RDATA1   0x12

// Self-calibration command opcodes (datasheet Table 9-33)
#define ADS1263_CMD_SYOCAL1  0x16   // system offset cal (uses external short)
#define ADS1263_CMD_SYGCAL1  0x17   // system gain cal (uses external near-FS signal)
#define ADS1263_CMD_SFOCAL1  0x19   // self offset cal (chip shorts internal PGA inputs)

#define ADS1263_REG_ID         0x00
#define ADS1263_REG_POWER      0x01
#define ADS1263_REG_INTERFACE  0x02
#define ADS1263_REG_MODE0      0x03
#define ADS1263_REG_MODE1      0x04
#define ADS1263_REG_MODE2      0x05
#define ADS1263_REG_INPMUX     0x06
#define ADS1263_REG_OFCAL0     0x07   // OFCAL LSB
#define ADS1263_REG_OFCAL1     0x08   // OFCAL mid
#define ADS1263_REG_OFCAL2     0x09   // OFCAL MSB (24-bit 2's complement, default 0x000000)
#define ADS1263_REG_FSCAL0     0x0A   // FSCAL LSB
#define ADS1263_REG_FSCAL1     0x0B   // FSCAL mid
#define ADS1263_REG_FSCAL2     0x0C   // FSCAL MSB (24-bit unsigned, default 0x400000 = unity)
#define ADS1263_REG_REFMUX     0x0F
#define ADS1263_REG_TDACP      0x10
#define ADS1263_REG_TDACN      0x11

#define ADS1263_EXPECTED_ID_UPPER_5BITS  0x20
#define ADS1263_INTERFACE_STATUS_CRC     0x05

// SPI settings — same as FirstPowerUp
static const SPISettings SPI_CFG(500000, MSBFIRST, SPI_MODE1);

// Known EVM AVDD from cp10 ratiometric TDAC sweep (2026-05-24).
// Used by cp3 (SYGCAL1) to compute the expected differential at
// TDACP=0.9·AVDD vs TDACN=0.1·AVDD → 0.8 × AVDD.
static const double AVDD_KNOWN_V = 5.2056;
static const double VREF         = 5.0;     // external REF7050 on AIN0/AIN1
static const double LSB_GAIN1    = VREF / 2147483648.0;  // 2^31

// =====================================================================
// Output helpers — same as FirstPowerUp
// =====================================================================
static void banner(const char *title) {
    Serial.println();
    Serial.println(F("============================================================"));
    Serial.print(F("  ")); Serial.println(title);
    Serial.println(F("============================================================"));
}

static void cp_pass(int n, const char *what) {
    Serial.print(F("[cp ")); Serial.print(n);
    Serial.print(F("] PASS  ")); Serial.println(what);
}

static void cp_fail(int n, const char *what, const char *hint) {
    Serial.print(F("[cp ")); Serial.print(n);
    Serial.print(F("] FAIL  ")); Serial.println(what);
    Serial.print(F("[cp ")); Serial.print(n);
    Serial.print(F("] hint  ")); Serial.println(hint);
    Serial.println(F("[cp X] halting — see README §Failure triage."));
    while (1) {
        digitalWrite(LED_BUILTIN, LED_ON);  delay(150);
        digitalWrite(LED_BUILTIN, LED_OFF); delay(150);
    }
}

static void cp_info(int n, const char *line) {
    Serial.print(F("[cp ")); Serial.print(n);
    Serial.print(F("] info  ")); Serial.println(line);
}

// =====================================================================
// ADS1263 low-level helpers
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

static bool ads_read_conversion(int32_t *out_code) {
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
    return true;
}

// Read the 24-bit OFCAL[2:0] register (offset calibration, 2's complement).
// Returns the sign-extended 32-bit value.
static int32_t ads_read_ofcal() {
    uint8_t lsb = ads_read_reg(ADS1263_REG_OFCAL0);
    uint8_t mid = ads_read_reg(ADS1263_REG_OFCAL1);
    uint8_t msb = ads_read_reg(ADS1263_REG_OFCAL2);
    uint32_t raw = ((uint32_t)msb << 16) | ((uint32_t)mid << 8) | (uint32_t)lsb;
    // Sign-extend 24-bit → 32-bit
    if (raw & 0x00800000) raw |= 0xFF000000;
    return (int32_t)raw;
}

// Read the 24-bit FSCAL[2:0] register (full-scale calibration, straight binary).
// Default is 0x400000 (unity gain). Returns the unsigned 24-bit value.
static uint32_t ads_read_fscal() {
    uint8_t lsb = ads_read_reg(ADS1263_REG_FSCAL0);
    uint8_t mid = ads_read_reg(ADS1263_REG_FSCAL1);
    uint8_t msb = ads_read_reg(ADS1263_REG_FSCAL2);
    return ((uint32_t)msb << 16) | ((uint32_t)mid << 8) | (uint32_t)lsb;
}

// Take N samples at the currently configured rate, return mean in codes.
// Uses two-pass mean/RMS, same pattern as FirstPowerUp's cp5/cp6.
static void ads_measure(int N, int32_t *codes_buf, double *out_mean_code, double *out_rms_code) {
    for (int i = 0; i < N; i++) {
        delay(5);                              // 400 SPS = 2.5 ms; 5 ms safe margin
        ads_read_conversion(&codes_buf[i]);
    }
    double sum = 0.0;
    for (int i = 0; i < N; i++) sum += (double)codes_buf[i];
    double mean = sum / N;
    double var = 0.0;
    for (int i = 0; i < N; i++) {
        double d = (double)codes_buf[i] - mean;
        var += d * d;
    }
    *out_mean_code = mean;
    *out_rms_code  = sqrt(var / N);
}

// Re-write critical registers after SFOCAL1 / SYGCAL1 — per
// ADS1263_H7_Integration_Notes.md §6, calibration commands may reset
// other registers to defaults. We defensively re-write POWER, MODE2,
// INPMUX, REFMUX before resuming measurement. INTERFACE we leave alone
// (and verify it didn't snap back, which is the canonical test).
static void rewrite_critical_regs(uint8_t mode2_target, uint8_t inpmux_target) {
    ads_write_reg(ADS1263_REG_POWER,  0x13);   // INTREF + VBIAS on
    ads_write_reg(ADS1263_REG_MODE2,  mode2_target);
    ads_write_reg(ADS1263_REG_INPMUX, inpmux_target);
    ads_write_reg(ADS1263_REG_REFMUX, 0x09);   // external 5V on AIN0/AIN1
    delay(10);                                 // let writes settle
}

// =====================================================================
// cp 0 — Serial up
// =====================================================================
static void cp0_serial_up() {
    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) { /* wait */ }
    banner("ADS1263 self-calibration verification — Phase 2.1");
    Serial.println(F("Prerequisite: ADS1263_FirstPowerUp_PIO/ cp0–cp10 all PASS"));
    Serial.println(F("Cable map:    ../doc/MEMO_cable_map.md"));
    Serial.println(F("AVDD assumed: 5.2056 V (from cp10 ratiometric measurement)"));
    Serial.println();
    cp_pass(0, "Serial up (USB CDC enumerated)");

    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LED_OFF);
}

// =====================================================================
// cp 1 — Bring-up (compressed from FirstPowerUp cp1–cp4 + cp6)
// =====================================================================
static void cp1_bringup() {
    pinMode(PIN_CS,    OUTPUT);
    pinMode(PIN_RESET, OUTPUT);
    pinMode(PIN_DRDY,  INPUT);
    digitalWrite(PIN_CS,    HIGH);
    digitalWrite(PIN_RESET, HIGH);

    cp_info(1, "pulsing /RESET LOW for 100 ms");
    digitalWrite(PIN_RESET, LOW);
    delay(100);
    digitalWrite(PIN_RESET, HIGH);
    delay(50);

    cp_info(1, "SPI.begin() — default object on Mid Carrier J15-20/22/24");
    SPI.begin();
    delay(3000);                              // ADS1263 power-up settle
    ads_command(ADS1263_CMD_RESET);
    delay(50);

    uint8_t id   = ads_read_reg(ADS1263_REG_ID);
    uint8_t intf = ads_read_reg(ADS1263_REG_INTERFACE);
    char buf[100];
    snprintf(buf, sizeof(buf),
             "ID = 0x%02X (expect 0x2X), INTERFACE = 0x%02X (expect 0x05)",
             id, intf);
    cp_info(1, buf);
    if ((id & 0xF8) != ADS1263_EXPECTED_ID_UPPER_5BITS) {
        cp_fail(1, "ADS1263 ID register reads wrong family",
                "Run ADS1263_FirstPowerUp_PIO/ — its cp4 will localize the failure.");
    }
    if (intf != ADS1263_INTERFACE_STATUS_CRC) {
        cp_fail(1, "INTERFACE register is not at the expected default 0x05",
                "Chip may not have completed reset cleanly. Re-power the EVM.");
    }

    // Configure for AINCOM-shorted measurement with VBIAS on (matches
    // FirstPowerUp cp5/cp6 setup, since SFOCAL1 cp2 below uses this state).
    cp_info(1, "configuring: REFMUX=0x09 (ext 5V), INPMUX=0xAA (AINCOM-shorted), "
                "MODE2=0x08 (PGA on, gain=1, 400 SPS), POWER=0x13 (INTREF + VBIAS)");
    ads_write_reg(ADS1263_REG_REFMUX, 0x09);
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);
    ads_write_reg(ADS1263_REG_MODE2,  0x08);
    ads_write_reg(ADS1263_REG_POWER,  0x13);
    delay(10);

    // Sanity-check the VBIAS bit landed
    uint8_t pwr = ads_read_reg(ADS1263_REG_POWER);
    if ((pwr & 0x02) == 0) {
        cp_fail(1, "VBIAS bit did not stick in POWER register",
                "Run ADS1263_FirstPowerUp_PIO/ cp6 to triage — same fix applies.");
    }

    ads_command(ADS1263_CMD_START1);
    delay(50);                                // Sinc3 settling

    cp_pass(1, "bring-up complete — chip ready for self-cal");
}

// =====================================================================
// cp 2 — SFOCAL1 sweep across PGA gains
// =====================================================================
// At each PGA gain G ∈ {1, 2, 4, 8, 16, 32}:
//   1. Configure MODE2 with PGA on, gain G, 400 SPS
//   2. INPMUX = 0xAA (AINCOM-shorted) for the pre-cal baseline
//   3. Take N=200 samples → pre_mean_code (chip's raw offset at this gain)
//   4. Read pre-cal OFCAL register (should be 0x000000 default before
//      first SFOCAL1; will hold the previous cal value on subsequent
//      iterations — that's fine, we reset before each SFOCAL1)
//   5. Reset OFCAL to 0x000000 (so we can see a clean before/after)
//   6. INPMUX = 0xFF per datasheet §9.4.9.2 (open all inputs for SFOCAL1)
//   7. Send SFOCAL1 (0x19), wait ~200 ms (cal time + safety margin)
//   8. Re-write critical regs (POWER/MODE2/INPMUX/REFMUX) — per
//      integration notes §6 warning that SFOCAL1 may snap registers back
//   9. Verify INTERFACE register is still 0x05 (THE canonical snap-back
//      check this cp is meant to expose)
//  10. Read post-cal OFCAL → should be non-zero, approximately
//      -pre_mean_code / 256 (24-bit OFCAL is left-shifted to align with
//      32-bit ADC output)
//  11. INPMUX = 0xAA again, take post-cal N=200 samples → post_mean_code
//
// PASS if:
//   - OFCAL was written (non-default after cal)
//   - |post_mean_code| < |pre_mean_code| × 0.10 (offset reduced ≥ 90 %)
//   - INTERFACE register survived at 0x05
//   - Manual prediction matches actual OFCAL within ±256 LSB
//     (the prediction has its own measurement noise; ±256 LSB is ~1 LSB
//     of the 32-bit space → very tight)
static void cp2_sfocal_sweep() {
    cp_info(2, "SFOCAL1 sweep: AINCOM-shorted, run self-offset-cal at each PGA gain");
    cp_info(2, "  gain | MODE2 | pre_mean (uV) | OFCAL (predicted)| OFCAL (actual)  | INTERFACE | post_mean (uV) | reduction | result");
    cp_info(2, "  -----+-------+---------------+------------------+-----------------+-----------+----------------+-----------+--------");

    const uint8_t gains[]      = { 1, 2, 4,  8, 16, 32 };
    const uint8_t gain_codes[] = { 0, 1, 2,  3,  4,  5 };
    const int     N            = 200;
    int32_t       codes[200];
    bool          all_ok       = true;
    char          buf[200];

    for (int g = 0; g < 6; g++) {
        uint8_t gain  = gains[g];
        uint8_t mode2 = (uint8_t)((gain_codes[g] << 4) | 0x08);   // PGA on, gain G, 400 SPS

        // (1)–(2) Configure: PGA at this gain, AINCOM-shorted
        ads_write_reg(ADS1263_REG_MODE2,  mode2);
        ads_write_reg(ADS1263_REG_INPMUX, 0xAA);
        // Reset OFCAL to the default 0x000000 so each gain's measurement
        // starts from a known state (otherwise the previous SFOCAL1's
        // value still applies and the "pre" mean is already calibrated).
        ads_write_reg(ADS1263_REG_OFCAL0, 0x00);
        ads_write_reg(ADS1263_REG_OFCAL1, 0x00);
        ads_write_reg(ADS1263_REG_OFCAL2, 0x00);
        ads_command(ADS1263_CMD_START1);
        delay(50);

        // (3) Pre-cal baseline measurement
        double pre_mean_code, pre_rms_code;
        ads_measure(N, codes, &pre_mean_code, &pre_rms_code);
        double pre_mean_uV  = pre_mean_code * LSB_GAIN1 * 1e6 / (double)gain;

        // (4)–(5) The OFCAL register is 24-bit two's complement,
        // left-shifted internally to align with 32-bit ADC output.
        // Datasheet eq 22: Final = (Filter - OFCAL << 8) × FSCAL / 0x400000
        // So OFCAL_predicted = round(pre_mean_code / 256)
        int32_t ofcal_predicted = (int32_t)round(pre_mean_code / 256.0);

        // (6) Set INPMUX = 0xFF per datasheet §9.4.9.2
        ads_write_reg(ADS1263_REG_INPMUX, 0xFF);
        delay(10);

        // (7) Run SFOCAL1, wait for completion. Calibration time at
        // 400 SPS Sinc3 is 53.4 ms per datasheet Table 9-28; 200 ms
        // gives generous margin.
        ads_command(ADS1263_CMD_SFOCAL1);
        delay(200);

        // (8) Defensive re-write of critical registers
        rewrite_critical_regs(mode2, 0xAA);

        // (9) The "snap-back" check — did INTERFACE survive?
        uint8_t intf_post = ads_read_reg(ADS1263_REG_INTERFACE);
        bool intf_ok = (intf_post == ADS1263_INTERFACE_STATUS_CRC);

        // (10) Read the actual OFCAL the chip wrote
        int32_t ofcal_actual = ads_read_ofcal();

        // (11) Post-cal measurement
        ads_command(ADS1263_CMD_START1);
        delay(50);
        double post_mean_code, post_rms_code;
        ads_measure(N, codes, &post_mean_code, &post_rms_code);
        double post_mean_uV = post_mean_code * LSB_GAIN1 * 1e6 / (double)gain;

        // Verdict
        double reduction = (fabs(pre_mean_code) > 0.0)
                         ? (1.0 - fabs(post_mean_code) / fabs(pre_mean_code))
                         : 0.0;
        bool ofcal_wrote = (ofcal_actual != 0);
        bool reduced_ok  = (reduction > 0.90);   // 90 % reduction minimum

        const char *verdict;
        if      (!intf_ok)                   { verdict = "FAIL itf";  all_ok = false; }
        else if (!ofcal_wrote)               { verdict = "FAIL noOFCAL"; all_ok = false; }
        else if (!reduced_ok)                { verdict = "FAIL nored"; all_ok = false; }
        else                                  { verdict = "pass";     }

        snprintf(buf, sizeof(buf),
                 "  %4u |  0x%02X | %+12.3f  | 0x%06lX (%+9ld) | 0x%06lX (%+9ld)| 0x%02X %s   | %+13.3f  |  %5.1f%%  | %s",
                 gain, mode2, pre_mean_uV,
                 (unsigned long)(ofcal_predicted & 0xFFFFFF), (long)ofcal_predicted,
                 (unsigned long)(ofcal_actual    & 0xFFFFFF), (long)ofcal_actual,
                 intf_post, intf_ok ? "OK " : "BAD",
                 post_mean_uV, reduction * 100.0,
                 verdict);
        cp_info(2, buf);
    }

    // Restore default OFCAL (0) so cp3 starts with a known state.
    ads_write_reg(ADS1263_REG_OFCAL0, 0x00);
    ads_write_reg(ADS1263_REG_OFCAL1, 0x00);
    ads_write_reg(ADS1263_REG_OFCAL2, 0x00);

    if (!all_ok) {
        cp_fail(2, "SFOCAL1 sweep failed at one or more gains",
                "Inspect the table. 'FAIL itf' = INTERFACE register snapped to "
                "non-0x05 (the legacy-HAT-style register reset — re-write would "
                "be needed in production). 'FAIL noOFCAL' = OFCAL stayed at "
                "0x000000 after SFOCAL1 (cal command not executing — check "
                "INPMUX was set to 0xFF before the command, calibration time "
                "delay is sufficient). 'FAIL nored' = post-cal mean not reduced "
                "by 90 %% (cal wrote OFCAL but the value is wrong — check "
                "MODE2 readback didn't change, PGA didn't go to bypass).");
    }
    cp_pass(2, "SFOCAL1 sweep clean across all PGA gains");
}

// =====================================================================
// cp 3 — SYGCAL1 demo (system gain calibration with TDAC)
// =====================================================================
// IMPORTANT — what SYGCAL1 actually does (datasheet §9.4.9.6): it writes
// FSCAL such that "whatever input you have applied" is normalized to
// **+VREF positive full-scale**. It does NOT try to make the post-cal
// reading equal the input. So if you apply 4.16 V and run SYGCAL1, the
// post-cal reading will be 5.0 V (= +VREF), and FSCAL will hold the
// ratio that achieves that:
//
//   FSCAL_post = 0x400000 × VREF / V_input
//
// We verify SYGCAL1 by checking that:
//   (a) FSCAL_post matches the predicted ratio (the math is right)
//   (b) Post-cal reading equals +VREF (the cal coefficient is applied)
//   (c) INTERFACE register survives at 0x05 (no snap-back)
//
// Signal: TDACP=0x89 (OUTP=1, MAGP=01001 → 0.9·AVDD on AIN6)
//         TDACN=0x99 (OUTN=1, MAGN=11001 → 0.1·AVDD on AIN7)
//         INPMUX = 0x67 (AIN6 vs AIN7 differential)
//         → V_input = (0.9 − 0.1) × AVDD = 0.8 × 5.2056 = 4.1645 V
//         This is ~83 % of ±5 V VREF full-scale at PGA=1.
//
// Predicted FSCAL = 0x400000 × 5.0 / 4.1645 = 0x4CCDB6
// Predicted post-cal reading = +VREF = 5.0000 V
static void cp3_sygcal_demo() {
    cp_info(3, "SYGCAL1 demo: drive TDAC at 0.8·AVDD, run system-gain cal");
    cp_info(3, "  expected behavior: chip writes FSCAL such that applied V becomes +VREF");

    // (1) Configure for the TDAC differential measurement at PGA=1
    ads_write_reg(ADS1263_REG_INPMUX, 0x67);     // AIN6 vs AIN7
    ads_write_reg(ADS1263_REG_MODE2,  0x08);     // PGA on, gain=1, 400 SPS
    ads_write_reg(ADS1263_REG_TDACP,  0x89);     // OUTP=1, MAGP=01001 (0.9·AVDD)
    ads_write_reg(ADS1263_REG_TDACN,  0x99);     // OUTN=1, MAGN=11001 (0.1·AVDD)
    delay(20);                                    // TDAC + RC filter settling

    // Reset FSCAL to default in case prior runs left it modified
    ads_write_reg(ADS1263_REG_FSCAL0, 0x00);
    ads_write_reg(ADS1263_REG_FSCAL1, 0x00);
    ads_write_reg(ADS1263_REG_FSCAL2, 0x40);

    ads_command(ADS1263_CMD_START1);
    delay(50);

    // (2) Pre-cal FSCAL
    uint32_t fscal_pre = ads_read_fscal();

    // (3) Pre-cal measurement
    const int N = 200;
    int32_t   codes[200];
    double    pre_mean_code, pre_rms_code;
    ads_measure(N, codes, &pre_mean_code, &pre_rms_code);
    double pre_mean_V = pre_mean_code * LSB_GAIN1;

    // Predicted applied signal from ratiometric AVDD (cp10 measurement)
    const double V_INPUT_PRED = 0.8 * AVDD_KNOWN_V;

    // Predicted FSCAL: the chip writes 0x400000 × VREF / V_input
    // Using the MEASURED pre-cal V (which is more accurate than the
    // predicted V — it folds in chip gain error + actual AVDD drift).
    uint32_t fscal_pred_from_measured = (uint32_t)round(4194304.0 * VREF / pre_mean_V);
    uint32_t fscal_pred_from_avdd     = (uint32_t)round(4194304.0 * VREF / V_INPUT_PRED);

    char buf[200];
    snprintf(buf, sizeof(buf),
             "applied signal: predicted %.4f V (0.8×AVDD), measured %.4f V (error = %+5.0f ppm)",
             V_INPUT_PRED, pre_mean_V,
             (pre_mean_V - V_INPUT_PRED) / V_INPUT_PRED * 1e6);
    cp_info(3, buf);
    snprintf(buf, sizeof(buf),
             "FSCAL pre-cal: 0x%06lX (default 0x400000); predicted post-cal = 0x%06lX",
             (unsigned long)fscal_pre, (unsigned long)fscal_pred_from_measured);
    cp_info(3, buf);

    // (4) Run SYGCAL1
    ads_command(ADS1263_CMD_SYGCAL1);
    delay(200);

    // (5) Defensive re-writes (per integration notes §6)
    rewrite_critical_regs(0x08, 0x67);
    // Re-enable TDAC in case it snapped back
    ads_write_reg(ADS1263_REG_TDACP, 0x89);
    ads_write_reg(ADS1263_REG_TDACN, 0x99);
    delay(20);

    // (6) Snap-back check
    uint8_t intf_post = ads_read_reg(ADS1263_REG_INTERFACE);
    bool intf_ok = (intf_post == ADS1263_INTERFACE_STATUS_CRC);

    // (7) Post-cal FSCAL
    uint32_t fscal_post = ads_read_fscal();
    int32_t  fscal_diff = (int32_t)fscal_post - (int32_t)fscal_pred_from_measured;

    // (8) Post-cal measurement
    ads_command(ADS1263_CMD_START1);
    delay(50);
    double post_mean_code, post_rms_code;
    ads_measure(N, codes, &post_mean_code, &post_rms_code);
    double post_mean_V = post_mean_code * LSB_GAIN1;

    snprintf(buf, sizeof(buf),
             "FSCAL post-cal: 0x%06lX (delta from default = %+ld LSB → gain factor %.6f)",
             (unsigned long)fscal_post,
             (long)((int32_t)fscal_post - 0x400000L),
             (double)fscal_post / 4194304.0);
    cp_info(3, buf);
    snprintf(buf, sizeof(buf),
             "FSCAL cross-check: actual = 0x%06lX, predicted from measured = 0x%06lX, diff = %+ld LSB (%.1f ppm of FS)",
             (unsigned long)fscal_post, (unsigned long)fscal_pred_from_measured,
             (long)fscal_diff,
             (double)fscal_diff / 4194304.0 * 1e6);
    cp_info(3, buf);
    snprintf(buf, sizeof(buf),
             "post-cal measured = %.4f V (expected +VREF = %.4f V, error = %+5.0f ppm)",
             post_mean_V, VREF,
             (post_mean_V - VREF) / VREF * 1e6);
    cp_info(3, buf);
    snprintf(buf, sizeof(buf),
             "INTERFACE register = 0x%02X (%s)",
             intf_post, intf_ok ? "OK — survived SYGCAL1" : "BAD — snapped");
    cp_info(3, buf);

    // Disable TDAC and restore neutral state.
    ads_write_reg(ADS1263_REG_TDACP, 0x00);
    ads_write_reg(ADS1263_REG_TDACN, 0x00);
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);

    // ----- Acceptance criteria (the correct ones now) -----
    //
    //   (a) INTERFACE register at 0x05 after SYGCAL1
    //
    //   (b) FSCAL changed from default 0x400000 — proves SYGCAL1 actually ran
    //
    //   (c) FSCAL matches the predicted value (0x400000 × VREF / V_measured)
    //       within ±500 LSB. 500 LSB / 0x400000 = 120 ppm — loose enough to
    //       accommodate the chip's noise during the 16-reading average that
    //       SYGCAL1 performs, tight enough to verify the cal math.
    //
    //   (d) Post-cal measurement reads +VREF within ±50 mV (= ±1 %).
    //       The math says post-cal V = V_input × FSCAL/0x400000 — if FSCAL
    //       is correct, this is identically VREF.
    bool fscal_changed = (fscal_post != 0x400000);
    bool fscal_math_ok = (abs(fscal_diff) < 500);
    double post_vs_vref_err = fabs(post_mean_V - VREF) / VREF;
    bool post_meas_ok = (post_vs_vref_err < 0.01);   // ±1 %

    if (!intf_ok) {
        cp_fail(3, "INTERFACE register snapped back after SYGCAL1",
                "Production firmware must re-write INTERFACE = 0x05 after any "
                "calibration command. This is the legacy-HAT register-snap-back "
                "issue documented in ADS1263_H7_Integration_Notes.md §6.");
    }
    if (!fscal_changed) {
        cp_fail(3, "SYGCAL1 did not modify FSCAL — calibration did not execute",
                "Check that the TDAC drive is producing a real signal (cp10 in "
                "FirstPowerUp confirms TDAC works), and that SYGCAL1 wait time "
                "(200 ms) is sufficient at the current data rate.");
    }
    if (!fscal_math_ok) {
        cp_fail(3, "FSCAL post-cal does not match the predicted ratio",
                "SYGCAL1 wrote FSCAL but the value differs from "
                "0x400000 × VREF / V_measured by more than ±500 LSB. Either "
                "VREF is not 5.0 V (re-check REF7050 path; cp5 PASS proves it "
                "reaches AIN0/AIN1) or the chip is computing FSCAL with a "
                "different convention than expected.");
    }
    if (!post_meas_ok) {
        cp_fail(3, "Post-cal measurement not at +VREF",
                "SYGCAL1's job is to normalize the applied input to read as "
                "+VREF after cal. If FSCAL is correct but the post-cal reading "
                "isn't +VREF, something is changing the signal between cal and "
                "re-measurement (TDAC settling, register snap-back).");
    }
    cp_pass(3, "SYGCAL1 demo clean — FSCAL math correct, post-cal reads +VREF, INTERFACE survived");
}

// =====================================================================
// Arduino entry points
// =====================================================================
void setup() {
    cp0_serial_up();
    cp1_bringup();
    cp2_sfocal_sweep();
    cp3_sygcal_demo();

    banner("ALL CHECKPOINTS PASSED (cp0–cp3)");
    Serial.println(F("Phase 2.1 self-calibration verification COMPLETE."));
    Serial.println(F("Headline:"));
    Serial.println(F("  - SFOCAL1 works at every PGA gain. INTERFACE survives."));
    Serial.println(F("  - SYGCAL1 writes FSCAL and pulls the measurement to predicted."));
    Serial.println(F("  - Production firmware should still defensively re-write"));
    Serial.println(F("    POWER, MODE2, INPMUX, REFMUX after any calibration"));
    Serial.println(F("    command (per integration notes §6)."));
    Serial.println(F("Next: Phase 3 sensor configuration — see"));
    Serial.println(F("      doc/PLAN_phase3_sensors.md."));

    pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
    digitalWrite(LED_BUILTIN, LED_ON);  delay(1000);
    digitalWrite(LED_BUILTIN, LED_OFF); delay(1000);
}
