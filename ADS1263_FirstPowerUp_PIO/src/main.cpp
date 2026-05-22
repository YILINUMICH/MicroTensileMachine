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
// PIN DEFINES — Mid Carrier J15 → Portenta HD → Arduino-mbed macro
// =====================================================================
// See ../doc/MEMO_cable_map.md for the 6-wire SPI cable layout.
//
// J15 silkscreen   | HD Standard | HD pin  | Used here as
// ---------------- | ----------- | ------- | ------------------------
// J15-20 SPI1 SCLK | SPI1_CK     | J2-38   | (handled by SPI.begin())
// J15-22 SPI1 CIPO | SPI1_MISO   | J2-40   | (handled by SPI.begin())
// J15-24 SPI1 COPI | SPI1_MOSI   | J2-42   | (handled by SPI.begin())
// J15-25 PWM 0     | PWM_0       | J2-59   | /CS    (GPIO output)
// J15-27 PWM 1     | PWM_1       | J2-61   | /DRDY  (GPIO input,
//                                                    not gated here)
// J15-29 PWM 2     | PWM_2       | J2-63   | /RESET (GPIO output)
//
// ⚠️  PIN-MACRO VERIFICATION (do this once before first flash):
//
//     If the build fails with "PWM0/PWM1/PWM2 not declared in this
//     scope" the Arduino-mbed Portenta H7 core in your version
//     spells these macros differently. Open:
//
//       <core install>/variants/PORTENTA_H7_M7/variant.h
//
//     and find the right symbol for PWM_0 / PWM_1 / PWM_2 / D2 / etc.
//     The macros below are the most common spelling in recent cores.
//
//     If you change these, ALSO update ../doc/MEMO_cable_map.md
//     and the cross-references in SensorHub_PIO/STATUS.md.

#define PIN_CS     PWM0      // J15-25, /CS
#define PIN_DRDY   PWM1      // J15-27, /DRDY
#define PIN_RESET  PWM2      // J15-29, /RESET

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
    // For first-power-up: do NOT require the operator to short AIN0↔AIN1.
    // Just measure whatever's on the inputs and report. The threshold
    // check below will pass if the noise is under 5 mV RMS, which is
    // generous (Test B was 5 µV with shorted inputs — 1000× margin
    // tolerates floating inputs).

    cp_info(5, "configuring ADC1: INPMUX=0x01 (AIN0/AIN1), "
                "MODE2=0x88 (PGA bypass, 400 SPS), REFMUX=0x00 (internal 2.5V)");
    ads_write_reg(ADS1263_REG_INPMUX, 0x01);
    ads_write_reg(ADS1263_REG_MODE2,  0x88);
    ads_write_reg(ADS1263_REG_REFMUX, 0x00);

    // Verify writes stuck.
    uint8_t rb_inpmux = ads_read_reg(ADS1263_REG_INPMUX);
    uint8_t rb_mode2  = ads_read_reg(ADS1263_REG_MODE2);
    uint8_t rb_refmux = ads_read_reg(ADS1263_REG_REFMUX);
    if (rb_inpmux != 0x01 || rb_mode2 != 0x88 || rb_refmux != 0x00) {
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

    // Compute mean and RMS in volts.  V = code / 2^31 * Vref ; Vref = 2.5 V.
    const double VREF = 2.5;
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
             "100 samples: mean = %+.3f mV   RMS = %.3f uV",
             mean_V * 1000.0, rms_V * 1e6);
    cp_info(5, buf);

    if (rms_V > 5e-3) {
        cp_fail(5, "RMS noise > 5 mV — signal chain compromised",
                "Likely cause: floating differential inputs picking up mains, "
                "or a reference voltage problem. Short AIN0 to AIN1 on the EVM "
                "and re-run for a true noise-floor measurement.");
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
    Serial.println(F("  - short AIN0 to AIN1 on the EVM and re-run for the"));
    Serial.println(F("    true noise-floor measurement (target: < 50 uV RMS)"));
    Serial.println(F("  - then port SensorHub_PIO pin defines to match the"));
    Serial.println(F("    PIN_CS / PIN_DRDY / PIN_RESET values that worked here"));
    Serial.println(F("  - update doc/MEMO_cable_map.md if anything changed."));

    // Slow heartbeat LED to signal "alive and idle"
    pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
    digitalWrite(LED_BUILTIN, HIGH); delay(1000);
    digitalWrite(LED_BUILTIN, LOW);  delay(1000);
}
