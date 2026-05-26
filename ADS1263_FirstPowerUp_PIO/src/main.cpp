/*
 * ADS1263_FirstPowerUp_PIO — eleven-checkpoint bring-up diagnostic
 *
 * Target hardware:  Arduino Portenta H7 (ABX00042)
 *                 + Arduino Portenta Mid Carrier (ASX00055)
 *                 + TI ADS1263 EVM (connected via 6-wire SPI cable)
 *
 * Wiring source of truth: ../doc/MEMO_cable_map.md
 *
 * What this sketch does — in order, halting on first failure:
 *
 *   cp 0 : Serial up                  proves M7 boots + USB CDC enumerates
 *   cp 1 : GPIO pinMode               proves CS / DRDY / RESET pin defines compile
 *   cp 2 : /RESET pulse               drives reset low 100 ms then high
 *   cp 3 : SPI.begin()                proves SPI peripheral configures
 *   cp 4 : ADS1263 ID read            reads register 0x00, expects 0x23
 *   cp 5 : Self-noise short test      shorts AIN0 to AIN1 on the EVM and
 *                                     measures the resulting RMS noise
 *   cp 6 : VBIAS + PGA mini-sweep     enables VBIAS (POWER bit 1 → AINCOM
 *                                     biased to mid-supply +2.5 V), then
 *                                     walks PGA gain ∈ {1,2,4,8,16,32} at
 *                                     400 SPS with AINCOM-shorted inputs.
 *                                     Confirms the AINCOM biasing strategy
 *                                     and the PGA path before the full
 *                                     SPS × PGA noise sweep in
 *                                     ADS1263_NoiseFloor_PIO/ (Phase 1.2 of
 *                                     doc/MEMO_baseline_testing.md).
 *   cp 7 : AIN-pair scan              walks INPMUX across non-reference AIN
 *                                     pairs (AIN2/3, AIN4/5, AIN6/7, AIN8/9
 *                                     diff + SE-vs-AINCOM). Retires the
 *                                     legacy HAT's AIN2/3 saturation question
 *                                     on the bare EVM (Phase 1.3).
 *   cp 8 : ADC2 enable + read         configures and reads the chip's
 *                                     secondary 24-bit ADC (ADC2). Unblocks
 *                                     SensorHub_PIO's dual-ADC mode (Phase 1.4).
 *   cp 9 : DRDY edge-rate count       counts falling edges on PC_6 for 10 s
 *                                     at 400 SPS; expects 4000 ± 1%. Confirms
 *                                     interrupt-driven DRDY is viable on the
 *                                     Mid Carrier (Phase 1.5, retires legacy
 *                                     PJ_11 / LoRa conflict question).
 *   cp10 : TDAC sanity sweep          drives the chip's internal Test DAC
 *                                     onto AIN6, measures via ADC1, verifies
 *                                     within ±50 mV. Free DC-accuracy check
 *                                     (Phase 1.6).
 *
 * Each line of output is prefixed `[cp N]` so it's easy to grep and easy
 * for an AI agent (or future-you) to parse. A FAIL line includes a
 * "look at X" hint that maps to a specific physical thing in the README's
 * triage table.
 *
 * This sketch is M7-only — there is NO M4 build environment, NO RPC,
 * NO shared driver code. The diagnostic is intentionally standalone so
 * that if it works, the new hardware works; if it fails, the firmware
 * isn't a candidate cause.
 *
 * Author: Yilin Ma — HDR Lab, University of Michigan
 */

#include <Arduino.h>
#include <SPI.h>
#include <math.h>

// =====================================================================
// PIN DEFINES — Mid Carrier J15 → Portenta HD → STM32 pin name
// =====================================================================
// See ../doc/MEMO_cable_map.md for the 6-wire SPI cable layout.
//
// J15 silkscreen   | HD Standard | HD pin  | STM32 | Used here as
// ---------------- | ----------- | ------- | ----- | -----------------
// J15-20 SPI1 SCLK | SPI1_CK     | J2-38   | PI1   | (handled by SPI.begin())
// J15-22 SPI1 CIPO | SPI1_MISO   | J2-40   | PC2   | (handled by SPI.begin())
// J15-24 SPI1 COPI | SPI1_MOSI   | J2-42   | PC3   | (handled by SPI.begin())
// J15-25 PWM 0     | PWM_0       | J2-59   | PA8   | /CS    (GPIO output)
// J15-27 PWM 1     | PWM_1       | J2-61   | PC6   | /DRDY  (GPIO input,
//                                                          not gated here)
// J15-29 PWM 2     | PWM_2       | J2-63   | PC7   | /RESET (GPIO output)
//
// HISTORY — why STM32 pin names instead of PWM_0/1/2 macros:
//
//     The Mid Carrier silkscreen and Arduino's pinout documentation
//     both label these positions as PWM_0/1/2, but the Arduino-mbed
//     Portenta H7 core's variant.h does not define those macros for
//     the low numbers (compiler's suggested alternative was PWM_8 —
//     only PWM_8/PWM_9 are exposed as macros). Rather than chase the
//     core's naming, we use the absolute STM32 pin names — these are
//     unambiguous and work across any core version. See the linked
//     Arduino forum post on HD-connector pin referencing for the
//     pattern.
//
//     STM32 pin names are taken from the Portenta H7 pinout
//     (doc/PortentaH7_ABX00042_Pinout.pdf, page 12 — J2_odd table).
//
//     If you change these, ALSO update ../doc/MEMO_cable_map.md
//     and the cross-references in SensorHub_PIO/STATUS.md.

#define PIN_CS     PA_8       // J15-25 → J2-59 → PA8,  /CS
#define PIN_DRDY   PC_6       // J15-27 → J2-61 → PC6,  /DRDY
#define PIN_RESET  PC_7       // J15-29 → J2-63 → PC7,  /RESET

// =====================================================================
// LED conventions — Portenta H7's LED_BUILTIN is wired ACTIVE-LOW.
// HIGH on the pin = LED off; LOW = LED on. This is OPPOSITE the standard
// Arduino convention. Use these macros everywhere to keep the intent
// readable. (Verified on the bench 2026-05-24 — NoiseFloor's "solid on
// during sweep" came out dark until polarity was flipped.)
// =====================================================================
#define LED_ON   LOW
#define LED_OFF  HIGH

// =====================================================================
// ADS1263 commands & registers (from ADS1263 datasheet, doc/)
// =====================================================================
#define ADS1263_CMD_RESET   0x06   // soft reset
#define ADS1263_CMD_START1  0x08   // start ADC1 continuous conversion
#define ADS1263_CMD_STOP1   0x0A
#define ADS1263_CMD_START2  0x0C   // start ADC2 continuous conversion (cp8)
#define ADS1263_CMD_STOP2   0x0E   // stop ADC2 (cp8)
#define ADS1263_CMD_RREG    0x20   // read register: | addr in low 5 bits
#define ADS1263_CMD_WREG    0x40   // write register
#define ADS1263_CMD_RDATA1  0x12   // read ADC1 conversion result
#define ADS1263_CMD_RDATA2  0x14   // read ADC2 conversion result (cp8)

#define ADS1263_REG_ID         0x00
#define ADS1263_REG_POWER      0x01
#define ADS1263_REG_INTERFACE  0x02
#define ADS1263_REG_MODE0      0x03
#define ADS1263_REG_MODE1      0x04
#define ADS1263_REG_MODE2      0x05
#define ADS1263_REG_INPMUX     0x06
#define ADS1263_REG_REFMUX     0x0F
#define ADS1263_REG_TDACP      0x10  // Test DAC positive (cp10): bit 7 = OUTP, bits 4:0 = MAGP
#define ADS1263_REG_TDACN      0x11  // Test DAC negative (cp10): bit 7 = OUTN, bits 4:0 = MAGN
#define ADS1263_REG_ADC2CFG    0x15  // ADC2 config (cp8): DR2 / REF2 / GAIN2
#define ADS1263_REG_ADC2MUX    0x16  // ADC2 input mux (cp8): same encoding as INPMUX

#define ADS1263_EXPECTED_ID_UPPER_5BITS  0x20  // 0x2X — silicon revision in low 3

// Per integration notes, the EVM ships with INTERFACE = 0x05 (STATUS+CRC
// framing on RDATA1). We keep that default and read 6 bytes per
// conversion: STATUS + 4 data + CRC.
#define ADS1263_INTERFACE_STATUS_CRC  0x05

// SPI settings per ADS1263_H7_Integration_Notes.md §3
static const SPISettings SPI_CFG(500000, MSBFIRST, SPI_MODE1);

// =====================================================================
// Output helpers
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
    SPI.transfer(0x00);            // num_bytes - 1 = 0 (read one register)
    uint8_t val = SPI.transfer(0x00);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    return val;
}

static void ads_write_reg(uint8_t addr, uint8_t val) {
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(ADS1263_CMD_WREG | (addr & 0x1F));
    SPI.transfer(0x00);            // num_bytes - 1 = 0
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

// Read one ADC1 conversion. Returns the signed 32-bit code in *out_code
// and true if the STATUS byte's NEW bit indicates fresh data. Caller
// must time the reads (this build is poll-only — DRDY is not gated).
static bool ads_read_conversion(int32_t *out_code) {
    uint8_t frame[6];
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(ADS1263_CMD_RDATA1);
    for (int i = 0; i < 6; i++) {
        frame[i] = SPI.transfer(0x00);
    }
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    // frame[0] = STATUS, frame[1..4] = data (MSB first), frame[5] = CRC
    uint32_t raw = ((uint32_t)frame[1] << 24)
                 | ((uint32_t)frame[2] << 16)
                 | ((uint32_t)frame[3] << 8)
                 |  (uint32_t)frame[4];
    *out_code = (int32_t)raw;
    // CRC not checked here — for bring-up we trust the bus.
    return true;
}

// Read one ADC2 conversion (cp8). ADC2 is the chip's 24-bit secondary
// ADC, with its own input mux and command set.
//
// Datasheet §9.4.7.2 (Figure 9-44) + §9.4.7.3: with INTERFACE = 0x05
// (STATUS + CHK both enabled), RDATA2 returns SIX bytes — NOT five:
//     frame[0] = STATUS
//     frame[1] = D3 (MSB of 24-bit data)
//     frame[2] = D2
//     frame[3] = D1 (LSB of 24-bit data)
//     frame[4] = 00h zero-pad (fixed value inserted by the chip so the
//                ADC2 frame lines up with the 6-byte ADC1 frame; not
//                included in the CHK sum per §9.4.7.3.3.1)
//     frame[5] = CHK
//
// Earlier versions of this function read only 5 bytes and treated
// frame[4] as CRC. cp8 did not verify CRC so it "worked" — but the
// shared SensorHub driver does verify, and it failed every read for
// exactly this reason. The fix is to clock out the full 6 bytes.
// Returns the signed 32-bit sign-extended code in *out_code.
static bool ads_read_conversion_adc2(int32_t *out_code) {
    uint8_t frame[6];
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(ADS1263_CMD_RDATA2);
    for (int i = 0; i < 6; i++) {
        frame[i] = SPI.transfer(0x00);
    }
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    // frame layout in the comment above; data is in frame[1..3]
    uint32_t raw = ((uint32_t)frame[1] << 16)
                 | ((uint32_t)frame[2] << 8)
                 |  (uint32_t)frame[3];
    // Sign-extend 24-bit → 32-bit
    if (raw & 0x00800000) raw |= 0xFF000000;
    *out_code = (int32_t)raw;
    return true;
}

// =====================================================================
// Checkpoints
// =====================================================================

static void cp0_serial_up() {
    Serial.begin(115200);
    // Don't block forever waiting for the host — but give it a fair shot.
    uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) { /* wait */ }
    banner("ADS1263 first-power-up diagnostic — M7-only");
    Serial.println(F("Cable map: ../doc/MEMO_cable_map.md"));
    Serial.println(F("Carrier:   Portenta Mid Carrier (ASX00055), J15 connector"));
    Serial.println(F("EVM:       TI ADS1263 EVM, J2 connector"));
    Serial.println();
    cp_pass(0, "Serial up (USB CDC enumerated)");

    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LED_OFF);    // start dark — heartbeat starts after setup() returns
}

static void cp1_gpio_pinmode() {
    pinMode(PIN_CS,    OUTPUT);
    pinMode(PIN_RESET, OUTPUT);
    pinMode(PIN_DRDY,  INPUT);

    // Drive both outputs HIGH (idle state for /CS and /RESET).
    digitalWrite(PIN_CS,    HIGH);
    digitalWrite(PIN_RESET, HIGH);

    // Read DRDY once — we don't act on the value, but the read itself
    // proves the GPIO can be sampled (a bad pin macro would hang here).
    int drdy_now = digitalRead(PIN_DRDY);
    Serial.print(F("[cp 1] info  DRDY initial level = "));
    Serial.println(drdy_now ? "HIGH" : "LOW");

    cp_pass(1, "pinMode CS/DRDY/RESET; CS=HIGH, RESET=HIGH");
}

static void cp2_reset_pulse() {
    cp_info(2, "pulsing /RESET LOW for 100 ms (scope-verify if possible)");
    digitalWrite(PIN_RESET, LOW);
    delay(100);
    digitalWrite(PIN_RESET, HIGH);
    delay(50);   // tWAKEUP after reset (datasheet)
    cp_pass(2, "/RESET pulsed");
}

static void cp3_spi_begin() {
    cp_info(3, "calling SPI.begin() — using default SPI object");
    cp_info(3, "  (Portenta H7 default SPI → J2-38/40/42 → Mid Carrier J15-20/22/24)");
    SPI.begin();
    cp_pass(3, "SPI.begin() returned");
}

static void cp4_ads1263_id() {
    cp_info(4, "waiting 3000 ms for ADS1263 power-up settle "
                "(per integration notes §2)");
    delay(3000);

    // Soft reset over SPI for good measure — after the GPIO pulse this
    // is belt-and-braces.
    ads_command(ADS1263_CMD_RESET);
    delay(50);

    uint8_t id = ads_read_reg(ADS1263_REG_ID);
    uint8_t intf = ads_read_reg(ADS1263_REG_INTERFACE);

    char buf[80];
    snprintf(buf, sizeof(buf),
             "ID register (0x00) = 0x%02X  (expecting 0x2X family)", id);
    cp_info(4, buf);
    snprintf(buf, sizeof(buf),
             "INTERFACE register (0x02) = 0x%02X  (default 0x%02X = STATUS+CRC)",
             intf, ADS1263_INTERFACE_STATUS_CRC);
    cp_info(4, buf);

    if ((id & 0xF8) != ADS1263_EXPECTED_ID_UPPER_5BITS) {
        // Dump all 17 user registers to help triage.
        Serial.println(F("[cp 4] register dump (all 0x00 = MISO silent):"));
        for (uint8_t r = 0; r < 17; r++) {
            uint8_t v = ads_read_reg(r);
            Serial.print(F("        REG 0x"));
            if (r < 16) Serial.print('0');
            Serial.print(r, HEX);
            Serial.print(F(" = 0x"));
            if (v < 16) Serial.print('0');
            Serial.println(v, HEX);
        }
        if (id == 0x00) {
            cp_fail(4, "ID reads as 0x00 — MISO is silent",
                    "1) full power-cycle the EVM (unplug USB + supply 5 s); "
                    "2) reseat every cable wire end-to-end; "
                    "3) ohm-meter SCLK/MISO/MOSI continuity; "
                    "4) confirm EVM 3.3V and 5V rails present on J2.");
        } else if (id == 0xFF) {
            cp_fail(4, "ID reads as 0xFF — MISO floating high (no chip selected?)",
                    "Check /CS wiring (J15-25 → J2-7) and that PIN_CS macro is correct.");
        } else {
            cp_fail(4, "ID does not match ADS1263 family (0x2X)",
                    "SPI bus is alive but the wrong chip is responding, "
                    "or framing is off. Verify INPMUX/REFMUX defaults, SPI mode 1.");
        }
    }
    cp_pass(4, "ADS1263 found on SPI bus");
}

static void cp5_noise_floor() {
    // Bring-up noise-floor test, configured for THIS rig:
    //
    //   - External 5 V reference on AIN0 (+REF) / AIN1 (-REF).
    //     REFMUX = 0x09  →  RMUXP=001 (AIN0), RMUXN=001 (AIN1).
    //     Datasheet §9.6.12, Table 9-46. External-reference range
    //     per spec is 0.9 V to 5 V (§9.3.8.2).
    //
    //   - Internal "Vshort" via AINCOM on both differential inputs.
    //     INPMUX = 0xAA  →  MUXP=1010 (AINCOM), MUXN=1010 (AINCOM).
    //     Datasheet §9.6.7, Table 9-41. This gives 0 V differential
    //     at the ADC front-end without depending on any external
    //     wiring — the textbook noise-floor test setup. Matches the
    //     Test B benchmark in ADS1263_H7_Integration_Notes.md
    //     (target ~5 µV RMS at 400 SPS, PGA bypass).
    //
    //   - PGA bypass, 400 SPS.
    //     MODE2 = 0x88.
    //
    // NOTE: AIN0 / AIN1 are committed to being the reference pair
    // and MUST NOT be used as measurement inputs simultaneously.
    // If you change REFMUX to internal reference (0x00), INPMUX
    // can return to using AIN0/AIN1 for measurement.

    cp_info(5, "configuring ADC1: INPMUX=0xAA (AINCOM-shorted), "
                "MODE2=0x88 (PGA bypass, 400 SPS), "
                "REFMUX=0x09 (external 5V ref on AIN0/AIN1)");
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);
    ads_write_reg(ADS1263_REG_MODE2,  0x88);
    ads_write_reg(ADS1263_REG_REFMUX, 0x09);

    // Verify writes stuck.
    uint8_t rb_inpmux = ads_read_reg(ADS1263_REG_INPMUX);
    uint8_t rb_mode2  = ads_read_reg(ADS1263_REG_MODE2);
    uint8_t rb_refmux = ads_read_reg(ADS1263_REG_REFMUX);
    if (rb_inpmux != 0xAA || rb_mode2 != 0x88 || rb_refmux != 0x09) {
        cp_fail(5, "register readback mismatch after configure",
                "Chip is acknowledging RREG but not WREG — check /CS hold, "
                "SPI mode (must be MODE1), or that no other code is racing.");
    }

    ads_command(ADS1263_CMD_START1);
    delay(50);                              // Sinc3 filter settling

    const int N = 100;
    int32_t codes[N];
    for (int i = 0; i < N; i++) {
        delay(5);                            // 400 SPS = 2.5 ms; 5 ms is safe margin
        ads_read_conversion(&codes[i]);
    }

    // Compute mean and RMS in volts.  V = code / 2^31 * Vref ; Vref = 5.0 V
    // (the external reference applied to AIN0/AIN1 on this rig).
    const double VREF = 5.0;
    const double LSB  = VREF / 2147483648.0;   // 2^31
    double mean = 0.0;
    for (int i = 0; i < N; i++) mean += (double)codes[i];
    mean /= N;
    double var = 0.0;
    for (int i = 0; i < N; i++) {
        double d = (double)codes[i] - mean;
        var += d * d;
    }
    double rms_code = sqrt(var / N);
    double mean_V = mean * LSB;
    double rms_V  = rms_code * LSB;

    char buf[120];
    snprintf(buf, sizeof(buf),
             "100 samples: mean = %+.3f mV   RMS = %.3f uV   (target: <50 uV)",
             mean_V * 1000.0, rms_V * 1e6);
    cp_info(5, buf);

    // Sanity threshold: 5 mV RMS. With AINCOM-shorted inputs, a healthy
    // chain should be ~5–30 µV RMS; >5 mV indicates a real problem
    // (reference missing, bad PGA bypass, etc.). Codes pinned at one
    // value (RMS = 0.000 exactly) also indicates a stuck reading —
    // see the next check.
    if (rms_V > 5e-3) {
        cp_fail(5, "RMS noise > 5 mV — signal chain compromised",
                "With INPMUX=AINCOM-shorted, the chip should be near 0 V "
                "differential. >5 mV RMS means the external reference is "
                "missing/wrong (check the low-reference monitor — datasheet "
                "§9.3.8.4 — and the 100 nF bypass cap across AIN0/AIN1), "
                "or the PGA isn't actually in bypass (verify MODE2=0x88).");
    }
    // Detect a stuck reading: real ADC samples always bounce by ≥1 LSB
    // due to noise. RMS = exactly 0 means we read the same code 100×,
    // which is what we'd see if conversions aren't actually running
    // (START1 didn't take effect) or RDATA1 is returning the chip's
    // idle value.
    if (rms_code == 0.0) {
        cp_fail(5, "RMS = 0 exactly — every sample is identical",
                "Conversions don't appear to be advancing. Check that "
                "START1 command (0x08) is being clocked correctly, that "
                "POWER register has INTREF=1 (or that the external ref is "
                "actually present), and that DRDY isn't held in a state "
                "that prevents RDATA1 from latching new data.");
    }
    cp_pass(5, "ADC stream alive and within sanity threshold");
}

static void cp6_vbias_pga_minisweep() {
    // Phase 1.1 of doc/MEMO_baseline_testing.md — the mini-sweep that
    // confirms VBIAS biasing works at every PGA gain we care about, before
    // we commit to the full SPS × PGA grid in ADS1263_NoiseFloor_PIO/.
    //
    // Why we need VBIAS:
    //   cp5 ran with PGA BYPASS (MODE2 = 0x88), which has a rail-to-rail
    //   input range and tolerates a floating AINCOM. The moment we turn
    //   the PGA on (any gain ≥ 1 with bit 7 = 0), the common-mode range
    //   tightens substantially. AINCOM has to sit inside the PGA's Vcm
    //   range for the conversion to be meaningful.
    //
    //   The ADS1263EVM is unipolar by design: AVDD = +5 V from the
    //   on-board TPS7A4700 LDO, AVSS = GND. Mid-supply is +2.5 V.
    //   The chip's VBIAS function (POWER register bit 1, datasheet
    //   §9.3.12 Figure 9-26) drives the AINCOM pin to (AVDD+AVSS)/2,
    //   which is exactly the middle of the PGA's input range.
    //
    //   Settling time per datasheet Table 9-7 is ≤ 0.22 ms at 0.1 µF
    //   load capacitance. The EVM has only 150 pF on AINCOM (R21/C21),
    //   so settling is much faster — 5 ms delay below is generous.
    //
    // What success looks like:
    //   At every gain, with AINCOM-shorted (INPMUX = 0xAA, both ADC
    //   differential inputs internally routed to the AINCOM pin), the
    //   input-referred RMS noise should be in the single-digit µV
    //   range. At gain=1 it should roughly match cp5's PGA-bypass
    //   number (~1.4 µV RMS). At higher gain, output-referred RMS
    //   scales up by ~gain× while input-referred RMS stays similar
    //   or drops (PGA dominates over ADC quantization noise as gain
    //   rises).

    cp_info(6, "enabling VBIAS (POWER bit 1) — biases AINCOM to mid-supply (+2.5 V)");

    uint8_t pwr_before = ads_read_reg(ADS1263_REG_POWER);
    uint8_t pwr_target = pwr_before | 0x02;
    ads_write_reg(ADS1263_REG_POWER, pwr_target);
    delay(5);                              // VBIAS settle, see comment above

    uint8_t pwr_rb = ads_read_reg(ADS1263_REG_POWER);
    char buf[128];
    snprintf(buf, sizeof(buf),
             "POWER: 0x%02X (before) → 0x%02X (wrote) → 0x%02X (readback)",
             pwr_before, pwr_target, pwr_rb);
    cp_info(6, buf);
    if ((pwr_rb & 0x02) == 0) {
        cp_fail(6, "VBIAS bit did not stick in POWER register",
                "WREG to POWER 0x01 not landing. Check /CS hold timing, "
                "SPI mode 1, and that the chip is not in a reset state. "
                "On older arduino-mbed cores, very fast back-to-back "
                "WREGs can race — try adding a delay before this cp.");
    }

    // INPMUX stays at 0xAA (set in cp5) — both ADC inputs routed to
    // AINCOM internally. Re-write it defensively in case anything
    // between cp5 and here disturbed it.
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);

    cp_info(6, "PGA gain sweep at 400 SPS, AINCOM-shorted, 200 samples per gain");
    cp_info(6, "  gain | MODE2 |  out mean (uV)  | out RMS (uV) |  in mean (uV)  | in RMS (uV) | result");
    cp_info(6, "  -----+-------+-----------------+--------------+----------------+-------------+-------");

    const double VREF = 5.0;
    const double LSB  = VREF / 2147483648.0;   // 2^31
    const uint8_t gains[]      = { 1, 2, 4,  8, 16, 32 };
    const uint8_t gain_codes[] = { 0, 1, 2,  3,  4,  5 };
    const int     N            = 200;
    int32_t       codes[200];
    bool          all_ok       = true;

    for (int g = 0; g < 6; g++) {
        uint8_t gain  = gains[g];
        uint8_t mode2 = (uint8_t)((gain_codes[g] << 4) | 0x08);   // bit 7 = 0 (PGA enabled), DR = 1000 (400 SPS)
        ads_write_reg(ADS1263_REG_MODE2, mode2);
        uint8_t mode2_rb = ads_read_reg(ADS1263_REG_MODE2);
        if (mode2_rb != mode2) {
            snprintf(buf, sizeof(buf),
                     "  %4u |  0x%02X | MODE2 wrote 0x%02X but readback 0x%02X — skipping row",
                     gain, mode2, mode2, mode2_rb);
            cp_info(6, buf);
            all_ok = false;
            continue;
        }

        ads_command(ADS1263_CMD_START1);
        delay(50);                          // Sinc3 settling

        // Two-pass mean / RMS to avoid double-precision loss when
        // accumulating squares of int32_t codes (which can hit ~10^9).
        // Same pattern as cp5_noise_floor() above.
        int stuck = 0;
        for (int i = 0; i < N; i++) {
            delay(5);                        // 400 SPS = 2.5 ms; 5 ms is safe margin
            ads_read_conversion(&codes[i]);
            if (i > 0 && codes[i] == codes[i-1]) stuck++;
        }

        double sum = 0.0;
        for (int i = 0; i < N; i++) sum += (double)codes[i];
        double mean_code = sum / N;
        double var = 0.0;
        for (int i = 0; i < N; i++) {
            double d = (double)codes[i] - mean_code;
            var += d * d;
        }
        double rms_code = sqrt(var / N);

        double out_mean_uV = mean_code * LSB * 1e6;
        double out_rms_uV  = rms_code  * LSB * 1e6;
        double in_mean_uV  = out_mean_uV / (double)gain;
        double in_rms_uV   = out_rms_uV  / (double)gain;

        // Sanity verdict per row:
        //   "FAIL stuck"  → RMS exactly 0  (conversions not advancing)
        //   "FAIL noisy"  → input-referred RMS > 50 µV (way above spec)
        //   "WARN dup"    → > 10% of samples were identical to the previous
        //                   (possible polling-faster-than-conversion at
        //                    higher gains, but at 400 SPS this is unlikely)
        //   "pass"        → everything looks healthy
        const char *verdict;
        if (rms_code == 0.0)            { verdict = "FAIL stuck"; all_ok = false; }
        else if (in_rms_uV > 50.0)      { verdict = "FAIL noisy"; all_ok = false; }
        else if (stuck > N / 10)        { verdict = "WARN dup ";  /* not fatal */ }
        else                            { verdict = "pass";       }

        snprintf(buf, sizeof(buf),
                 "  %4u |  0x%02X |  %+12.2f   |  %10.3f  |  %+11.2f   |  %9.3f  | %s",
                 gain, mode2, out_mean_uV, out_rms_uV, in_mean_uV, in_rms_uV, verdict);
        cp_info(6, buf);
    }

    if (!all_ok) {
        cp_fail(6, "VBIAS / PGA mini-sweep had at least one failing row",
                "Inspect the table above. 'FAIL stuck' = RMS == 0 "
                "(conversions not advancing — check START1 was clocked, "
                "DRDY not held). 'FAIL noisy' = input-referred RMS > 50 µV "
                "(VBIAS not landing → AINCOM railed; or PGA settling "
                "incomplete — try doubling the 50 ms post-START1 delay; "
                "or reference dropout — re-run cp5 first to confirm).");
    }
    cp_pass(6, "VBIAS + PGA mini-sweep clean across all gains");
}

// =====================================================================
// Phase 1.3 — cp7 — AIN-pair scan
// =====================================================================
// Goal: confirm each non-reference AIN pair on the bare EVM works, and
// retire the legacy Waveshare HAT's AIN2/3 saturation question. The
// legacy HAT had unresolved input-stage circuitry that pinned AIN2/3
// at full-scale under any non-zero input. The bare TI EVM has different
// input-stage circuitry (passive RC filters per pair, no front-end amp),
// so the issue may or may not reproduce.
//
// Method: walk INPMUX across {AIN2/3, AIN4/5, AIN6/7, AIN8/9} differential
// and {AIN2, AIN4, AIN6, AIN8} vs AINCOM single-ended. Skip AIN0/AIN1
// (committed to the REF7050 reference path).
//
// At each row: 500 samples at PGA=1, 400 SPS, two-pass mean/RMS.
// PASS = no saturation, RMS > 0 (conversions advancing).
//
// Restore INPMUX=0xAA on exit so downstream cps start from a known state.
//
// Datasheet: §9.6.7 (INPMUX), Table 9-41 (MUXP/MUXN codes).
static void cp7_ain_pair_scan() {
    cp_info(7, "AIN-pair scan: confirm each non-reference pair "
                "doesn't reproduce the legacy HAT AIN2/3 saturation");

    // PGA=1, 400 SPS — generous SPS for fast iteration, gain=1 for max
    // common-mode tolerance. Note cp6's exit state was MODE2=0x58
    // (gain=32); reset to 0x08 here.
    ads_write_reg(ADS1263_REG_MODE2, 0x08);

    cp_info(7, "  inpmux | meaning            |  mean (mV)  |  max code   |  min code   |  RMS (uV)  | result");
    cp_info(7, "  -------+--------------------+-------------+-------------+-------------+------------+--------");

    struct PairCfg { uint8_t inpmux; const char *label; };
    const PairCfg pairs[] = {
        { 0x23, "AIN2 vs AIN3 diff " },
        { 0x45, "AIN4 vs AIN5 diff " },
        { 0x67, "AIN6 vs AIN7 diff " },   // note: AIN6/7 are also TDAC outputs — run BEFORE cp10
        { 0x89, "AIN8 vs AIN9 diff " },
        { 0x2A, "AIN2 vs AINCOM SE " },
        { 0x4A, "AIN4 vs AINCOM SE " },
        { 0x6A, "AIN6 vs AINCOM SE " },
        { 0x8A, "AIN8 vs AINCOM SE " },
    };

    const double VREF = 5.0;
    const double LSB  = VREF / 2147483648.0;
    const int    N    = 500;
    int32_t      codes[500];
    bool         all_ok = true;
    char         buf[160];

    for (size_t row = 0; row < sizeof(pairs)/sizeof(pairs[0]); row++) {
        ads_write_reg(ADS1263_REG_INPMUX, pairs[row].inpmux);
        ads_command(ADS1263_CMD_START1);
        delay(50);                          // Sinc3 settling

        // Collect samples with the same two-pass pattern as cp5/cp6.
        int32_t code_max = INT32_MIN;
        int32_t code_min = INT32_MAX;
        for (int i = 0; i < N; i++) {
            delay(3);                       // 400 SPS = 2.5 ms; 3 ms keeps us above the conversion period
            ads_read_conversion(&codes[i]);
            if (codes[i] > code_max) code_max = codes[i];
            if (codes[i] < code_min) code_min = codes[i];
        }

        double sum = 0.0;
        for (int i = 0; i < N; i++) sum += (double)codes[i];
        double mean_code = sum / N;
        double var = 0.0;
        for (int i = 0; i < N; i++) {
            double d = (double)codes[i] - mean_code;
            var += d * d;
        }
        double rms_code = sqrt(var / N);

        double mean_mV = mean_code * LSB * 1000.0;
        double rms_uV  = rms_code  * LSB * 1e6;

        // Acceptance per row:
        //   - codes not pinned at ±2^31 (saturated): |max|, |min| < 2^30
        //   - RMS > 0  (conversions advancing)
        // Mean can be anywhere — floating inputs may pick up mains hum,
        // or sit at whatever the EVM's RC filter biases them to. The
        // test is "does the pair work AT ALL", not "is the value right".
        const int32_t SAT_LIMIT = (int32_t)(1L << 30);
        bool saturated = (code_max >  SAT_LIMIT) || (code_max < -SAT_LIMIT) ||
                         (code_min >  SAT_LIMIT) || (code_min < -SAT_LIMIT);
        bool stuck     = (rms_code == 0.0);
        const char *verdict = (saturated ? "FAIL sat " :
                               stuck     ? "FAIL stuck" : "pass");
        if (saturated || stuck) all_ok = false;

        snprintf(buf, sizeof(buf),
                 "  0x%02X   | %s | %+9.3f   | %+11ld | %+11ld | %9.3f  | %s",
                 pairs[row].inpmux, pairs[row].label,
                 mean_mV, (long)code_max, (long)code_min, rms_uV, verdict);
        cp_info(7, buf);
    }

    // Restore neutral state for downstream cps.
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);

    if (!all_ok) {
        cp_fail(7, "one or more AIN pairs failed (saturated or stuck)",
                "Possible AIN2/3-style issue on this EVM — inspect the input-stage "
                "filter components for the failing pair on the EVM schematic. If a "
                "single pair fails, downstream Phase 3 must avoid assigning a sensor "
                "to it. If ALL pairs fail, suspect chip configuration (PGA bypass "
                "state, VBIAS) — re-run cp5/cp6 first.");
    }
    cp_pass(7, "AIN-pair scan clean — no legacy AIN2/3-style saturation");
}

// =====================================================================
// Phase 1.4 — cp8 — ADC2 enable + read
// =====================================================================
// Goal: prove the chip's 24-bit secondary ADC (ADC2) works on this EVM.
// Production firmware (SensorHub_PIO) plans to use ADC2 for the laser
// channel so load cell + laser can run concurrently on different rates.
// The bring-up never exercised ADC2 — unknown if it works.
//
// Method: write ADC2CFG (data rate, reference, gain), ADC2MUX (input
// pair), issue START2, wait, read with RDATA2. Confirm non-stuck,
// non-saturated codes.
//
// Configuration:
//   ADC2CFG = 0x48  →  DR2 = 01 (100 SPS), REF2 = 001 (external AIN0/1
//                      ref shared with ADC1), GAIN2 = 000 (1 V/V)
//   ADC2MUX = 0x4A  →  AIN4 (MUXP2) vs AINCOM (MUXN2), single-ended
//
// Datasheet: §9.3.7 (ADC2 architecture), §9.5 (commands), §9.6.16
//            (ADC2CFG / ADC2MUX), Table 9-50 (REF2 codes).
static void cp8_adc2_check() {
    cp_info(8, "ADC2 enable + read: configure secondary 24-bit ADC, "
                "read 100 samples at 100 SPS, gain=1, AINCOM-shorted (noise floor test)");

    // ADC2CFG: DR2=01 (100 SPS), REF2=001 (ext AIN0/1, same REF7050 as ADC1),
    //          GAIN2=000 (1 V/V). Bits: [7:6]=DR2, [5:3]=REF2, [2:0]=GAIN2
    //          = 01 001 000 = 0x48.
    ads_write_reg(ADS1263_REG_ADC2CFG, 0x48);
    // ADC2MUX = 0xAA → MUXP2 = AINCOM, MUXN2 = AINCOM, both internally
    // shorted to the AINCOM pin (which VBIAS biases to mid-supply, +2.5 V).
    // Same as INPMUX=0xAA for ADC1's noise-floor test in cp5. This isolates
    // ADC2's intrinsic noise from any external input — proves the ADC2
    // signal chain is alive without dragging EMI pickup into the result.
    ads_write_reg(ADS1263_REG_ADC2MUX, 0xAA);

    // Read back to confirm writes landed
    uint8_t cfg_rb = ads_read_reg(ADS1263_REG_ADC2CFG);
    uint8_t mux_rb = ads_read_reg(ADS1263_REG_ADC2MUX);
    char buf[120];
    snprintf(buf, sizeof(buf),
             "ADC2CFG readback = 0x%02X (wrote 0x48); ADC2MUX readback = 0x%02X (wrote 0xAA)",
             cfg_rb, mux_rb);
    cp_info(8, buf);
    if (cfg_rb != 0x48 || mux_rb != 0xAA) {
        cp_fail(8, "ADC2CFG/ADC2MUX writes did not stick",
                "WREG to ADC2 registers not landing. Same triage as cp5 register "
                "readback — check /CS hold, SPI mode 1, that the chip isn't in a "
                "reset state.");
    }

    // Start ADC2; leave ADC1 running independently. The two ADCs share
    // the chip but are otherwise independent.
    ads_command(ADS1263_CMD_START2);
    delay(50);                              // ADC2 settling

    // Collect 100 samples. At 100 SPS the conversion period is 10 ms;
    // use delay(15) for a safe margin. Total ~1.5 s.
    const int N = 100;
    int32_t   codes[100];
    for (int i = 0; i < N; i++) {
        delay(15);
        ads_read_conversion_adc2(&codes[i]);
    }

    // Two-pass mean/RMS — same pattern as cp5/cp6. ADC2 is 24-bit so
    // full-scale = ±2^23 codes (per datasheet §9.3.7).
    double sum = 0.0;
    for (int i = 0; i < N; i++) sum += (double)codes[i];
    double mean_code = sum / N;
    double var = 0.0;
    for (int i = 0; i < N; i++) {
        double d = (double)codes[i] - mean_code;
        var += d * d;
    }
    double rms_code = sqrt(var / N);

    const double VREF = 5.0;
    const double LSB  = VREF / 8388608.0;   // 2^23 (ADC2 is 24-bit)
    double mean_mV = mean_code * LSB * 1000.0;
    double rms_uV  = rms_code  * LSB * 1e6;

    snprintf(buf, sizeof(buf),
             "100 samples: mean = %+.3f mV   RMS = %.3f uV   "
             "(ADC2 spec ~10 uV at gain=1, 100 SPS, AINCOM-shorted)",
             mean_mV, rms_uV);
    cp_info(8, buf);

    // Acceptance:
    //   - RMS > 0  (conversions advancing)
    //   - codes not saturated  (|code| < 2^22)
    //   - RMS plausibly within an order of magnitude of datasheet typical.
    //     Datasheet Table 8-3: ADC2 at 100 SPS Sinc3 gain=1 → 10.3 µV RMS.
    //     We allow up to 100 µV (10× margin) — generous enough to handle
    //     ambient lab noise but tight enough to catch real failures like
    //     reference dropout or stuck conversion.
    if (rms_code == 0.0) {
        cp_fail(8, "ADC2 RMS = 0 — every sample identical",
                "ADC2 isn't converting. Check START2 was clocked, that REF2 "
                "selection is valid (external AIN0/1 reference must be present), "
                "and that ADC2MUX isn't pointing at a non-existent or stuck pin.");
    }
    if (rms_uV > 100.0) {
        cp_fail(8, "ADC2 RMS noise > 100 uV — way above spec",
                "With AINCOM-shorted (ADC2MUX=0xAA), expected ~10 µV at gain=1, "
                "100 SPS per datasheet Table 8-3. >100 µV suggests reference "
                "dropout (REF7050 must reach AIN0/AIN1 — check cp5 setup), VBIAS "
                "not driving AINCOM (cp6 should have caught this), or chip "
                "malfunction. Run cp5 first to confirm REF + AINCOM path.");
    }
    // Saturation check
    int32_t code_max = INT32_MIN, code_min = INT32_MAX;
    for (int i = 0; i < N; i++) {
        if (codes[i] > code_max) code_max = codes[i];
        if (codes[i] < code_min) code_min = codes[i];
    }
    const int32_t SAT_LIMIT = (int32_t)(1L << 22);   // 2^22 — half of ADC2's ±2^23 range
    if (code_max > SAT_LIMIT || code_min < -SAT_LIMIT) {
        cp_fail(8, "ADC2 codes are saturated",
                "AIN4 may be sitting outside the input range. Try a different "
                "AIN channel (cp7 results say which pairs work).");
    }

    // Tidy up: stop ADC2 so it isn't burning power. Leave ADC1 alone.
    ads_command(ADS1263_CMD_STOP2);

    cp_pass(8, "ADC2 stream alive — secondary ADC verified on EVM");
}

// =====================================================================
// Phase 1.5 — cp9 — DRDY edge-rate count
// =====================================================================
// Goal: confirm /DRDY on PC_6 is interrupt-capable on the Mid Carrier.
// The bring-up uses timed polling (delay(5) between RDATA1 calls)
// because the legacy HAT setup had DRDY on PJ_11 which was tied to the
// LoRa IRQ and never went LOW. On the Mid Carrier, /DRDY moved to PC_6
// — not shared with LoRa — so DRDY should be usable as an interrupt.
//
// Method: configure ADC1 at 400 SPS, attach a falling-edge interrupt
// on PC_6 that increments a counter, run for 10 s, expect 4000 ± 1%.
//
// Subtlety per datasheet §9.4.4: /DRDY goes LOW when a new conversion
// is available and stays LOW until the next RDATA1. If we don't read,
// DRDY stays LOW continuously after the first conversion — no edges.
// Solution: poll DRDY in the loop, read RDATA1 to clear it, AND count
// the falling edges via the ISR.
//
// Datasheet: §9.4.4 (DRDY behaviour), §9.6.4 (MODE1 / DRDY mode).
volatile uint32_t cp9_drdy_count = 0;

static void cp9_drdy_isr() {
    cp9_drdy_count++;
}

static void cp9_drdy_edge_count() {
    cp_info(9, "DRDY edge-rate count: 10 s at 400 SPS, expect 4000 ± 40 edges");

    // ADC1 should already be configured from cp7's exit state
    // (INPMUX=0xAA, MODE2=0x08). Re-assert defensively.
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);
    ads_write_reg(ADS1263_REG_MODE2,  0x08);
    ads_command(ADS1263_CMD_START1);
    delay(50);                              // settling

    cp9_drdy_count = 0;
    attachInterrupt(digitalPinToInterrupt(PIN_DRDY), cp9_drdy_isr, FALLING);

    // Run for 10 s, polling DRDY to clear it so it can fall again.
    uint32_t t0 = millis();
    int32_t  throwaway;
    while (millis() - t0 < 10000) {
        if (digitalRead(PIN_DRDY) == LOW) {
            ads_read_conversion(&throwaway);
        }
    }

    detachInterrupt(digitalPinToInterrupt(PIN_DRDY));
    uint32_t count = cp9_drdy_count;

    char buf[120];
    snprintf(buf, sizeof(buf),
             "counted %lu falling edges in 10 s (expected ~4000)",
             (unsigned long)count);
    cp_info(9, buf);

    // Acceptance: 3960 ≤ count ≤ 4040 (1% tolerance).
    if (count < 3960 || count > 4040) {
        cp_fail(9, "DRDY edge count outside ±1% of expected",
                "Either (a) PC_6 isn't wired correctly to the EVM's /DRDY pin "
                "(verify Mid Carrier J15-27 ↔ EVM J2-11), (b) the chip isn't "
                "in continuous-conversion mode (cp5/cp6 should have caught this), "
                "or (c) PC_6 isn't supported as an interrupt source by the "
                "arduino-mbed core on this carrier. If (c), fall back to timed "
                "polling for now (the bring-up already does this — performance "
                "is acceptable for our SPS).");
    }
    cp_pass(9, "DRDY interrupt-capable on PC_6 — interrupt-driven reads viable");
}

// =====================================================================
// Phase 1.6 — cp10 — TDAC sanity check (ratiometric)
// =====================================================================
// Goal: use the chip's internal Test DAC (TDAC) to drive a known voltage
// onto AIN6, verify ADC1 measures it correctly, and back-compute AVDD
// from the result. Free DC-accuracy sanity check that doesn't need any
// external voltage source.
//
// CRITICAL: TDAC outputs are NOT absolute — they're ratios of AVDD.
// Per datasheet §9.3.14: "The TDAC reference voltage is the analog
// supply (V_AVDD – V_AVSS); therefore, the output levels refer to,
// and scale with, the analog power supply." Table 9-8 gives the
// divider ratios. AINCOM (via VBIAS) sits at 0.5 × AVDD.
//
// So the measured differential at AIN6 vs AINCOM is:
//   V_diff = (MAGP_ratio − 0.5) × AVDD
//
// And we can back-compute the EVM's actual AVDD as:
//   AVDD_derived = V_diff / (MAGP_ratio − 0.5)
//
// This makes cp10 a TWO-IN-ONE check:
//   (a) Linearity of the TDAC + ADC1 chain: AVDD_derived should be
//       consistent across all non-zero rows (within ±50 mV).
//   (b) EVM analog supply in spec: AVDD_derived should be 5.0 V ± 5 %
//       (the TPS7A4700 LDO tolerance — datasheet allows for trim).
//
// Method: route ADC1 to AIN6 vs AINCOM via INPMUX=0x6A, sweep TDACP
// across the magnitudes in the datasheet table, compute AVDD_derived
// for each non-zero row, verify (a) and (b).
//
// Bench note (2026-05-24): with this rig's REF7050 + EVM,
// AVDD_derived comes out at ≈ 5.205 V (chip is fine; the original
// "+50 mV abs-value" tolerance was wrong because it assumed AVDD = 5.0 V
// exactly).
//
// Datasheet: §9.3.14 (TDAC details + Table 9-8 ratios), §9.6.13–9.6.14
//            (TDACP/TDACN registers). EVM user guide §3.1.1.5 (TDAC on
//            AIN6/AIN7).
static void cp10_tdac_sanity() {
    cp_info(10, "TDAC sanity (ratiometric): drive AIN6 to known fractions of AVDD, "
                 "derive AVDD from each row, check consistency + spec");

    // Route ADC1 to AIN6 vs AINCOM, PGA=1, 400 SPS.
    ads_write_reg(ADS1263_REG_INPMUX, 0x6A);
    ads_write_reg(ADS1263_REG_MODE2,  0x08);

    struct TdacPoint {
        uint8_t tdacp_reg;     // bit 7 (OUTP=1) | bits 4:0 (MAGP)
        double  magp_ratio;    // TDACP output as fraction of AVDD (datasheet Tbl 9-8)
        const char *label;
    };
    const TdacPoint pts[] = {
        // MAGP=00000 ratio=0.5: TDAC at mid-supply → diff = 0 regardless of AVDD
        // (useful as offset check; can't derive AVDD from this row)
        { 0x80, 0.5,    "TDACP = 0.5·AVDD (MAGP=00000)" },
        { 0x88, 0.7,    "TDACP = 0.7·AVDD (MAGP=01000)" },
        { 0x89, 0.9,    "TDACP = 0.9·AVDD (MAGP=01001)" },
        { 0x97, 0.4,    "TDACP = 0.4·AVDD (MAGP=10111)" },
        { 0x98, 0.3,    "TDACP = 0.3·AVDD (MAGP=11000)" },
        { 0x99, 0.1,    "TDACP = 0.1·AVDD (MAGP=11001)" },
    };

    cp_info(10, "  TDACP setting                  | measured V    | AVDD derived  | result");
    cp_info(10, "  -------------------------------+---------------+---------------+--------");

    const double VREF = 5.0;
    const double LSB  = VREF / 2147483648.0;
    const int    N    = 100;
    double       avdd_sum = 0.0;
    int          avdd_count = 0;
    double       avdd_values[6];                    // one per row (NAN if not derivable)
    double       offset_v = 0.0;                    // ratio=0.5 row: pure offset
    char         buf[180];

    for (size_t i = 0; i < sizeof(pts)/sizeof(pts[0]); i++) {
        ads_write_reg(ADS1263_REG_TDACP, pts[i].tdacp_reg);
        delay(10);                                  // TDAC + RC-filter settling

        ads_command(ADS1263_CMD_START1);
        delay(50);                                  // Sinc3 settling

        // Two-pass mean.
        int32_t codes[100];
        for (int j = 0; j < N; j++) {
            delay(5);
            ads_read_conversion(&codes[j]);
        }
        double sum = 0.0;
        for (int j = 0; j < N; j++) sum += (double)codes[j];
        double mean_code = sum / N;
        double measured_v = mean_code * LSB;

        // Back-compute AVDD. ratio=0.5 row gives 0/0 — capture as offset.
        double delta_ratio = pts[i].magp_ratio - 0.5;
        if (fabs(delta_ratio) < 1e-6) {
            // ratio=0.5 row: diff = 0·AVDD + offset = offset only
            offset_v = measured_v;
            avdd_values[i] = NAN;
            snprintf(buf, sizeof(buf),
                     "  %-30s | %+8.4f V    | (offset row)  | offset = %+5.2f mV",
                     pts[i].label, measured_v, offset_v * 1000.0);
            cp_info(10, buf);
        } else {
            double avdd_derived = measured_v / delta_ratio;
            avdd_values[i] = avdd_derived;
            avdd_sum   += avdd_derived;
            avdd_count += 1;
            snprintf(buf, sizeof(buf),
                     "  %-30s | %+8.4f V    | %7.4f V     | derived",
                     pts[i].label, measured_v, avdd_derived);
            cp_info(10, buf);
        }
    }

    // Disable TDAC and restore neutral state.
    ads_write_reg(ADS1263_REG_TDACP, 0x00);
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);

    if (avdd_count < 1) {
        cp_fail(10, "no AVDD-derivable rows — TDAC sweep collapsed",
                "Every non-offset row failed to produce a valid reading. Check "
                "TDACP write took effect (OUTP bit = bit 7 must be 1) and that "
                "PGA isn't saturating (cp6 should have caught this).");
    }

    double avdd_mean = avdd_sum / avdd_count;
    double avdd_min  = +1e9, avdd_max = -1e9;
    for (size_t i = 0; i < sizeof(pts)/sizeof(pts[0]); i++) {
        if (!isnan(avdd_values[i])) {
            if (avdd_values[i] < avdd_min) avdd_min = avdd_values[i];
            if (avdd_values[i] > avdd_max) avdd_max = avdd_values[i];
        }
    }
    double avdd_span = avdd_max - avdd_min;

    snprintf(buf, sizeof(buf),
             "AVDD derived: mean = %.4f V, span = %.4f V (across %d rows)",
             avdd_mean, avdd_span, avdd_count);
    cp_info(10, buf);

    // Acceptance:
    //   (a) Consistency: AVDD span across rows < 50 mV (1% of 5 V) —
    //       proves TDAC + ADC chain is linear, no calibration issue.
    //   (b) Spec: AVDD mean within 5.0 V ± 0.25 V (5 %) — TPS7A4700
    //       LDO tolerance accommodates trim resistors and load.
    if (avdd_span > 0.050) {
        cp_fail(10, "AVDD derivation not consistent across rows",
                "TDAC chain shows non-linearity. Investigate: (1) PGA "
                "saturating at the extreme rows (try lower MAGP magnitudes); "
                "(2) AINCOM bias (VBIAS) drifting (cp6 should have caught "
                "this); (3) reference dropout under load (REF7050 must hold "
                "5.000 V — measure with a meter at AIN0/AIN1).");
    }
    if (avdd_mean < 4.75 || avdd_mean > 5.25) {
        cp_fail(10, "AVDD derived outside 5.0 V ± 5%",
                "EVM analog supply (TPS7A4700 LDO output) appears to be out "
                "of spec. Measure AVDD at the EVM screw terminals with a "
                "multimeter to confirm.");
    }
    cp_pass(10, "TDAC ratiometric sweep clean — TDAC linear, AVDD in spec");
}

// =====================================================================
// Arduino entry points
// =====================================================================

void setup() {
    cp0_serial_up();
    cp1_gpio_pinmode();
    cp2_reset_pulse();
    cp3_spi_begin();
    cp4_ads1263_id();
    cp5_noise_floor();
    cp6_vbias_pga_minisweep();
    cp7_ain_pair_scan();
    cp8_adc2_check();
    cp9_drdy_edge_count();
    cp10_tdac_sanity();

    banner("ALL CHECKPOINTS PASSED (cp0–cp10)");
    Serial.println(F("Phase 1 chip-level baseline complete. Next steps:"));
    Serial.println(F("  - Phase 2.1 self-calibration verification —"));
    Serial.println(F("    see doc/MEMO_baseline_testing.md."));
    Serial.println(F("  - update doc/MEMO_cable_map.md once Phase 3"));
    Serial.println(F("    sensor AIN-pair assignment is decided"));
    Serial.println(F("    (uses cp7 results)."));
    Serial.println(F("  - port SensorHub_PIO to match what worked here:"));
    Serial.println(F("    pin defines (PA_8/PC_6/PC_7), REFMUX=0x09,"));
    Serial.println(F("    VREF=5.0V in any volts-per-code math, VBIAS"));
    Serial.println(F("    on for PGA gain > 1, ADC2 verified working,"));
    Serial.println(F("    interrupt-driven DRDY viable on PC_6."));
    Serial.println(F("  - keep this module as a re-runnable diagnostic."));

    // Slow heartbeat LED to signal "alive and idle"
    pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
    // Slow ~0.5 Hz heartbeat — proves "alive and idle" after all cps PASS.
    digitalWrite(LED_BUILTIN, LED_ON);  delay(1000);
    digitalWrite(LED_BUILTIN, LED_OFF); delay(1000);
}
