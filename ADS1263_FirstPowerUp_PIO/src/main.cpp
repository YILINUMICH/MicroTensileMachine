/*
 * ADS1263_FirstPowerUp_PIO — six-checkpoint bring-up diagnostic
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
// ADS1263 commands & registers (from ADS1263 datasheet, doc/)
// =====================================================================
#define ADS1263_CMD_RESET   0x06   // soft reset
#define ADS1263_CMD_START1  0x08   // start ADC1 continuous conversion
#define ADS1263_CMD_STOP1   0x0A
#define ADS1263_CMD_RREG    0x20   // read register: | addr in low 5 bits
#define ADS1263_CMD_WREG    0x40   // write register
#define ADS1263_CMD_RDATA1  0x12   // read ADC1 conversion result

#define ADS1263_REG_ID         0x00
#define ADS1263_REG_POWER      0x01
#define ADS1263_REG_INTERFACE  0x02
#define ADS1263_REG_MODE0      0x03
#define ADS1263_REG_MODE1      0x04
#define ADS1263_REG_MODE2      0x05
#define ADS1263_REG_INPMUX     0x06
#define ADS1263_REG_REFMUX     0x0F

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
        digitalWrite(LED_BUILTIN, HIGH); delay(150);
        digitalWrite(LED_BUILTIN, LOW);  delay(150);
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
    digitalWrite(LED_BUILTIN, LOW);
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

    banner("ALL CHECKPOINTS PASSED");
    Serial.println(F("Hardware bring-up looks good. Next steps:"));
    Serial.println(F("  - port SensorHub_PIO to match what worked here:"));
    Serial.println(F("    pin defines (PA_8/PC_6/PC_7), REFMUX=0x09,"));
    Serial.println(F("    VREF=5.0V in any volts-per-code math."));
    Serial.println(F("  - update doc/MEMO_cable_map.md (load-cell channel"));
    Serial.println(F("    now needs an AIN pair OTHER than AIN0/AIN1)."));
    Serial.println(F("  - flip this module's STATUS.md from To-Test to"));
    Serial.println(F("    Diagnostic; keep it for re-runnable bring-up."));

    // Slow heartbeat LED to signal "alive and idle"
    pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
    digitalWrite(LED_BUILTIN, HIGH); delay(1000);
    digitalWrite(LED_BUILTIN, LOW);  delay(1000);
}
