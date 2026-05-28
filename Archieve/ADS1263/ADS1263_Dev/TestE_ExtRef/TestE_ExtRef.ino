/**
 * @file TestE_ExtRef.ino
 * @brief Test E — External Reference Noise Comparison
 *
 * Compares noise floor between:
 *   A) Internal 2.5V reference (REFMUX=0x00) — baseline
 *   B) AVDD/AVSS reference    (REFMUX=0x08) — quick external ref test
 *
 * Built on the stable ADS1263_Driver — same as TestB/TestC.
 * Uses DRDY hardware mode (INPUT_PULLUP on PJ_11).
 *
 * Setup: AIN0 shorted to AIN1 for noise test
 *
 * Pin mapping — Portenta H7 Elite + Hat Carrier:
 *   CS    = PE_6  (Pi pin 15)
 *   DRDY  = PJ_11 (Pi pin 11) — INPUT_PULLUP
 *   RESET = PI_5  (Pi pin 12)
 *
 * Commands:
 *   'a'  — noise test: internal 2.5V ref (2000 samples)
 *   'b'  — noise test: AVDD/AVSS 5V ref  (2000 samples)
 *   'c'  — back-to-back comparison
 *   'd'  — single reading (internal ref)
 *   'e'  — single reading (AVDD ref)
 *   'h'  — help
 */

#include "ADS1263_Driver.h"

ADS1263_Driver adc;

#define REFMUX_INTERNAL  0x00
#define REFMUX_AVDD      0x08
#define VREF_INTERNAL    2.5f
#define VREF_AVDD        5.0f

static const int N_SAMPLES = 2000;

struct NoiseStats {
    int   count;
    float mean_V, stddev_V, min_V, max_V, pkpk_V, nfb;
    bool  valid;
};

// ── Switch REFMUX via SPI without disrupting driver state ──────────────────
// The driver doesn't expose setRefMux(), so we access SPI directly.
// We keep the driver's CS pin and SPI settings consistent.
void setRefMux(uint8_t refmux) {
    adc.stopContinuous();
    delay(20);

    // Write REFMUX register directly using same SPI settings as driver
    SPISettings s(500000, MSBFIRST, SPI_MODE1);
    SPI.beginTransaction(s);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(10);
    SPI.transfer(0x40 | 0x0F);  // WREG REFMUX
    SPI.transfer(0x00);
    SPI.transfer(refmux);
    delayMicroseconds(10);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();
    delay(100);  // ref settle

    // Verify
    SPI.beginTransaction(s);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(10);
    SPI.transfer(0x20 | 0x0F);  // RREG REFMUX
    SPI.transfer(0x00);
    delayMicroseconds(5);
    uint8_t rb = SPI.transfer(0xFF);
    delayMicroseconds(10);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();

    Serial.print(F("  REFMUX set=0x")); Serial.print(refmux, HEX);
    Serial.print(F("  readback=0x")); Serial.print(rb, HEX);
    Serial.println(rb == refmux ? F(" ✓") : F(" ✗ MISMATCH"));
}

// ── Collect noise samples using driver's DRDY-based readDirect() ───────────
NoiseStats collectNoise(uint8_t refmux, float vref, const char* label) {
    NoiseStats s = {0, 0, 0, 1e30f, -1e30f, 0, 0, false};

    Serial.print(F("\n══ Noise test: ")); Serial.println(label);
    setRefMux(refmux);

    adc.startContinuous();
    delay(50);

    // Discard first 5 samples using poll — avoids DRDY edge issue after ref switch
    Serial.print(F("  Settling: "));
    for (int i = 0; i < 5; i++) {
        delay(5);  // 400 SPS = 2.5ms, wait 5ms per sample
        adc.readDirect();
        Serial.print('.');
    }
    Serial.println();

    // Collect using poll mode — more reliable than DRDY after ref switch
    Serial.print(F("  Collecting ")); Serial.print(N_SAMPLES); Serial.println(F(" samples..."));
    float mean = 0, M2 = 0;

    for (int i = 0; i < N_SAMPLES; i++) {
        delay(5);  // 400 SPS = 2.5ms, 5ms = safe poll interval
        ADC_Reading r = adc.readDirect();

        // Scale to actual vref (driver uses 2.5V internally)
        float v = ((float)r.raw_code / 2147483648.0f) * vref;

        s.count++;
        float delta = v - mean;
        mean += delta / s.count;
        M2   += delta * (v - mean);
        if (v < s.min_V) s.min_V = v;
        if (v > s.max_V) s.max_V = v;

        if (s.count % 500 == 0) {
            Serial.print(F("  ...")); Serial.print(s.count); Serial.println(F(" samples"));
        }
    }

    adc.stopContinuous();

    s.mean_V   = mean;
    s.stddev_V = (s.count > 1) ? sqrtf(M2/(s.count-1)) : 0;
    s.pkpk_V   = s.max_V - s.min_V;
    float fsr  = vref * 2.0f;
    if (s.stddev_V > 0) s.nfb = log2f(fsr/(6.6f*s.stddev_V));
    s.valid    = (s.count == N_SAMPLES);
    return s;
}

// ── Print stats ────────────────────────────────────────────────────────────
void printStats(const NoiseStats& s, const char* label, float vref) {
    Serial.println(F("────────────────────────────────────────────"));
    Serial.print(F("  Reference      : ")); Serial.println(label);
    Serial.print(F("  VREF / FSR     : ±")); Serial.print(vref,1); Serial.println(F(" V"));
    Serial.print(F("  Samples        : ")); Serial.println(s.count);
    Serial.print(F("  Mean           : ")); Serial.print(s.mean_V*1000,4); Serial.println(F(" mV"));
    Serial.print(F("  RMS noise      : ")); Serial.print(s.stddev_V*1e6f,2); Serial.println(F(" µV"));
    Serial.print(F("  Peak-to-peak   : ")); Serial.print(s.pkpk_V*1e6f,2);  Serial.println(F(" µV"));
    Serial.print(F("  Noise-free bits: ")); Serial.print(s.nfb,2); Serial.println(F(" bits"));
    Serial.print(F("  RMS  <50µV     : ")); Serial.println(s.stddev_V*1e6f<50  ? F("PASS ✓") : F("FAIL ✗"));
    Serial.print(F("  Pk-pk<300µV    : ")); Serial.println(s.pkpk_V*1e6f<300  ? F("PASS ✓") : F("FAIL ✗"));
    Serial.print(F("  NFB  >15bit    : ")); Serial.println(s.nfb>15            ? F("PASS ✓") : F("FAIL ✗"));
    Serial.println(F("────────────────────────────────────────────"));
}

// ── Commands ───────────────────────────────────────────────────────────────
void cmdCompare() {
    Serial.println(F("\n══ Test E: Back-to-Back Comparison ═════════════"));
    NoiseStats si = collectNoise(REFMUX_INTERNAL, VREF_INTERNAL, "Internal 2.5V");
    printStats(si, "Internal 2.5V", VREF_INTERNAL);

    NoiseStats sa = collectNoise(REFMUX_AVDD, VREF_AVDD, "AVDD/AVSS 5V");
    printStats(sa, "AVDD/AVSS 5V", VREF_AVDD);

    // Restore internal ref
    setRefMux(REFMUX_INTERNAL);

    if (!si.valid || !sa.valid) {
        Serial.println(F("  Cannot compare — one or both tests failed"));
        return;
    }

    Serial.println(F("\n══ Summary ══════════════════════════════════════"));
    Serial.println(F("  Metric           Internal 2.5V   AVDD/AVSS 5V"));
    Serial.println(F("  ─────────────────────────────────────────────────"));
    Serial.print(F("  RMS (µV)       : "));
    Serial.print(si.stddev_V*1e6f,2); Serial.print(F("          "));
    Serial.println(sa.stddev_V*1e6f,2);
    Serial.print(F("  Pk-pk (µV)     : "));
    Serial.print(si.pkpk_V*1e6f,2);  Serial.print(F("         "));
    Serial.println(sa.pkpk_V*1e6f,2);
    Serial.print(F("  NFB (bits)     : "));
    Serial.print(si.nfb,2);          Serial.print(F("           "));
    Serial.println(sa.nfb,2);

    float penalty = sa.stddev_V / si.stddev_V;
    Serial.print(F("  AVDD penalty   : ")); Serial.print(penalty,2); Serial.println(F("×"));
    if      (penalty < 2.0f) Serial.println(F("  → AVDD acceptable (<2×) ✓"));
    else if (penalty < 5.0f) Serial.println(F("  → AVDD marginal — consider REF5050"));
    else                     Serial.println(F("  → AVDD too noisy — precision ref needed"));
    Serial.println(F("═════════════════════════════════════════════════"));
}

void cmdSingle(uint8_t refmux, float vref, const char* label) {
    setRefMux(refmux);
    ADC_Reading r = adc.readSingle();
    setRefMux(REFMUX_INTERNAL);  // restore
    if (!r.valid) { Serial.println(F("[ERROR] read failed")); return; }
    float v = ((float)r.raw_code / 2147483648.0f) * vref;
    Serial.print(F("V = ")); Serial.print(v*1000,3);
    Serial.print(F(" mV  (")); Serial.print(label); Serial.println(F(")"));
}

void printHelp() {
    Serial.println(F("── Commands ────────────────────────────────────"));
    Serial.println(F("  a  Noise: internal 2.5V ref (~5s)"));
    Serial.println(F("  b  Noise: AVDD/AVSS 5V ref  (~5s)"));
    Serial.println(F("  c  Back-to-back comparison  (~12s)"));
    Serial.println(F("  d  Single reading (internal)"));
    Serial.println(F("  e  Single reading (AVDD)"));
    Serial.println(F("  h  Help"));
    Serial.println(F("────────────────────────────────────────────────"));
    Serial.println(F("AIN0 shorted to AIN1 for noise test"));
    Serial.println();
}

// ══════════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    delay(3000);

    Serial.println(F("╔══════════════════════════════════════════╗"));
    Serial.println(F("║  ADS1263 — Test E: External Reference    ║"));
    Serial.println(F("║  Internal 2.5V vs AVDD/AVSS 5V           ║"));
    Serial.println(F("╚══════════════════════════════════════════╝"));
    Serial.println();

    if (!adc.begin(ADS1263_400SPS)) {
        Serial.println(F("FATAL: ADS1263 init failed"));
        while (1) {}
    }
    adc.printConfig();
    Serial.println();
    printHelp();
}

void loop() {
    if (!Serial.available()) return;
    char c = Serial.read();
    while (Serial.available()) Serial.read();
    switch (c) {
        case 'a': { NoiseStats s = collectNoise(REFMUX_INTERNAL, VREF_INTERNAL, "Internal 2.5V"); printStats(s, "Internal 2.5V", VREF_INTERNAL); break; }
        case 'b': { NoiseStats s = collectNoise(REFMUX_AVDD,     VREF_AVDD,     "AVDD/AVSS 5V");  printStats(s, "AVDD/AVSS 5V",  VREF_AVDD);     break; }
        case 'c': cmdCompare(); break;
        case 'd': cmdSingle(REFMUX_INTERNAL, VREF_INTERNAL, "Internal 2.5V"); break;
        case 'e': cmdSingle(REFMUX_AVDD,     VREF_AVDD,     "AVDD/AVSS 5V");  break;
        case 'h': printHelp(); break;
        default:  Serial.print(F("Unknown: ")); Serial.println(c);
    }
}
