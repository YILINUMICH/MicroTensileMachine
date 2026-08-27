/**
 * @file main.cpp
 * @brief ADS131M04 bring-up smoke test — Portenta H7 M7 core.
 *
 * Scope: enough to run T1 (ID), T2 (register round-trip) and a first look at
 * live data from docs/ADS131M04_migration_plan.md §7. The full bench console
 * of plan §5 (`spi` clock ladder, `stream`, `noise`, `netcfg`/UDP) is NOT here
 * yet — this exists so the driver can be compiled, flashed and shown to talk to
 * the chip before more is built on top of it.
 *
 * Flash the M4 idle image FIRST (see platformio.ini) — M4 shares this SPI bus.
 */

#include <Arduino.h>

#if defined(CORE_CM4)

// ── M4: do nothing at all ────────────────────────────────────────────────
// The whole point of this image is that M4 makes NO SPI traffic while the M7
// test owns the bus. Built by [env:portenta_m4_idle].
void setup() {}
void loop()  { __WFI(); }

#elif defined(CORE_CM7)

#include "ADS131M04_Driver.h"

static ADS131M04_Driver adc;

// Target configuration from plan §4.6: 500 SPS is the closest available rate to
// the ADS1263's current 400 SPS, and gain 1 is the only usable setting until the
// attenuation question (§2.2 / §12) is settled.
static const ADS131M04_OSR_t  CFG_OSR = ADS131M04_OSR_8192;   // 500 SPS
static const ADS131M04_PWR_t  CFG_PWR = ADS131M04_PWR_HR;     // matches EVM Y1
static const uint8_t          CFG_CH  = 0x0F;                 // all four

static uint32_t last_print_ms = 0;
static uint32_t reads = 0;

// T2 — register round-trip. This is the test that catches the WREG payload slot
// (§4.5) and the one-frame response lag (§4.2) being wrong, which are the two
// ways this protocol most plausibly bites us.
static bool registerRoundTrip() {
    static const uint16_t probes[] = { 0x0F1A, 0x0F0E, 0x0F16, 0x0F1A };
    bool ok = true;

    for (uint8_t i = 0; i < sizeof(probes) / sizeof(probes[0]); i++) {
        if (!adc.writeRegister(ADS131M04_REG_CLOCK, probes[i])) {
            Serial.print(F("[T2] FAIL: WREG not acked for 0x"));
            Serial.println(probes[i], HEX);
            ok = false;
            continue;
        }
        const uint16_t rb = adc.readRegister(ADS131M04_REG_CLOCK);
        if ((rb & 0x0F3F) != (probes[i] & 0x0F3F)) {   // [15:12] read as 0
            Serial.print(F("[T2] FAIL: wrote 0x")); Serial.print(probes[i], HEX);
            Serial.print(F(" read 0x"));            Serial.println(rb, HEX);
            ok = false;
        }
    }

    // GAIN1 too — different bit packing (4-bit stride, 3-bit fields), so it
    // exercises a different shift path than CLOCK does.
    if (!adc.setGain(1, ADS131M04_GAIN_2) || adc.getGain(1) != ADS131M04_GAIN_2) {
        Serial.println(F("[T2] FAIL: GAIN1 ch1 round-trip"));
        ok = false;
    }
    adc.setGain(1, ADS131M04_GAIN_1);

    Serial.println(ok ? F("[T2] PASS: register round-trip")
                      : F("[T2] FAIL: register round-trip"));
    return ok;
}

void setup() {
    Serial.begin(115200);
    const uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) { /* wait briefly for the host */ }

    Serial.println();
    Serial.println(F("*** Firmware_ADS131M04Test_PIO — M7 bring-up smoke test ***"));
    Serial.println(F("[BOOT] plan: docs/ADS131M04_migration_plan.md"));
    Serial.println(F("[BOOT] flash portenta_m4_idle first — M4 shares SPI1"));

    if (!adc.begin(2000000)) {
        // Two very different faults land here and the distinction matters:
        // a dead SPI link, or a missing CLKIN. Register reads need only SPI,
        // so a plausible-but-wrong ID points at wiring; 0x0000/0xFFFF usually
        // means nothing is driving DOUT at all.
        Serial.print(F("[BOOT] FATAL: ADS131M04 not found, id=0x"));
        Serial.println(adc.deviceID(), HEX);
        Serial.println(F("[BOOT]   check: SPI wiring (Cable 1 -> EVM J6)"));
        Serial.println(F("[BOOT]   check: EVM JP6 fitted [1-2], JP5 NOT fitted"));
        Serial.println(F("[BOOT]   check: EVM powered, grounds common"));
        while (1) { delay(1000); }
    }

    Serial.print(F("[T1] PASS: id=0x"));
    Serial.println(adc.deviceID(), HEX);

    registerRoundTrip();

    if (!adc.configure(CFG_OSR, CFG_PWR, CFG_CH)) {
        Serial.println(F("[BOOT] FATAL: configure() failed"));
        while (1) { delay(1000); }
    }

    adc.printConfig(Serial);
    adc.printRegisters(Serial);
    adc.resetCounters();

    Serial.println(F("[BOOT] streaming: ch0..ch3 as code + volts, 1 Hz summary"));
    last_print_ms = millis();
}

void loop() {
    // Timed poll, deliberately: the same model the M4 uses for the ADS1263, so
    // the proven loop shape carries over. DRDY is wired and readable but not
    // gated on — a DRDY-driven loop freezes outright if edges stop arriving.
    ADS131M04_Reading r;
    if (adc.readChannels(r)) reads++;

    const uint32_t now = millis();
    if (now - last_print_ms >= 1000) {
        const uint32_t dt = now - last_print_ms;
        last_print_ms = now;

        Serial.print(F("[M04] rate=")); Serial.print((reads * 1000.0f) / dt, 1);
        Serial.print(F("/s frames="));  Serial.print(adc.framesRead());
        Serial.print(F(" crc_err="));   Serial.print(adc.crcErrors());
        Serial.print(F(" status=0x"));  Serial.print(r.status, HEX);
        for (uint8_t c = 0; c < ADS131M04_NUM_CH; c++) {
            Serial.print(F("  ch")); Serial.print(c);
            Serial.print(F("="));    Serial.print(r.raw[c]);
            Serial.print(F(" ("));   Serial.print(r.volts[c] * 1000.0f, 4);
            Serial.print(F(" mV)"));
        }
        Serial.println();
        reads = 0;
    }

    // ~500 SPS configured; poll a little faster so we never miss a conversion.
    delayMicroseconds(1500);
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
