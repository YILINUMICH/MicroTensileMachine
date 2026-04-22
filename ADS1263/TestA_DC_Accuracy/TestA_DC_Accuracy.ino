/**
 * @file TestA_DC_Accuracy.ino
 * @brief ADS1263 Verification — Test A: DC Voltage Reading Accuracy
 *
 * Section 2.3 of the test procedure.
 *
 * Setup:
 *   - Waveform generator DC output → AIN0
 *   - AIN1 (AINCOM) → GND
 *   - Multimeter in parallel with waveform generator for reference
 *
 * Serial commands (115200 baud):
 *   'r'  — take one reading (raw code + voltage)
 *   'n'  — take N_AVG averaged readings and report mean + std dev
 *   's'  — run full sweep (prompts you to step voltage, logs each point)
 *   'd'  — dump all registers
 *   'c'  — recalibrate (self-offset cal)
 *   'h'  — print this help
 *
 * ADS1263 configuration for this test:
 *   - Differential mode: AIN0 (+) vs AINCOM (−)  [INPMUX = 0x0A]
 *   - Internal 2.5 V reference                    [REFMUX = 0x24]
 *   - PGA bypassed (gain = 1)                     [MODE2 bit7 = 1]
 *   - Data rate: 20 SPS (maximum filtering, lowest noise for DC test)
 *   - Sinc3 filter                                [MODE1 = 0x40]
 *
 * Pass criteria (from test doc):
 *   ADC reading within ±2 mV of multimeter across 0–2.4 V.
 *   Negative input (−0.1 V) must return a negative voltage.
 *
 * Wiring (Arduino Uno):
 *   ADS1263 SCLK  → pin 13
 *   ADS1263 DIN   → pin 11
 *   ADS1263 DOUT  → pin 12
 *   ADS1263 CS    → pin 10
 *   ADS1263 DRDY  → pin 2
 *   ADS1263 RESET → pin 9
 */

#include "ADS1263_Driver.h"

// ── Test parameters ────────────────────────────────────────────────────────
static const int     N_AVG         = 20;    // Readings to average per point
static const float   PASS_ERROR_V  = 0.002; // ±2 mV pass criterion
static const float   VREF          = 2.5f;  // Internal reference (V)

// Sweep voltage set-points from test doc (Section 2.3, Table)
static const float SWEEP_POINTS[] = {
    0.000f, 0.500f, 1.000f, 1.500f, 2.000f, 2.400f, -0.100f
};
static const int N_SWEEP = sizeof(SWEEP_POINTS) / sizeof(SWEEP_POINTS[0]);

// ── Globals ────────────────────────────────────────────────────────────────
ADS1263_Driver adc;

// ── MeasResult struct ──────────────────────────────────────────────────────
struct MeasResult {
    float mean_V;
    float stddev_V;
    float min_V;
    float max_V;
    int   count;
};

// ── Forward declarations ───────────────────────────────────────────────────
void printHelp();
void cmdSingleReading();
void cmdAveragedReading();
void cmdSweep();
void cmdCalibrate();
MeasResult measureN(int n);

// ══════════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    while (!Serial) {}

    Serial.println(F("╔══════════════════════════════════════════╗"));
    Serial.println(F("║  ADS1263 — Test A: DC Accuracy           ║"));
    Serial.println(F("║  Section 2.3 — INA818 Verification       ║"));
    Serial.println(F("╚══════════════════════════════════════════╝"));
    Serial.println();

    // Init at 20 SPS for best noise rejection on DC
    if (!adc.begin(ADS1263_20SPS)) {
        Serial.println(F("FATAL: ADS1263 init failed. Check wiring."));
        while (1) {}
    }
    adc.printConfig();
    adc.printRegisters();  // Verify INPMUX (0x06) and REFMUX (0x0F) are correct

    // Short warm-up — 20 SPS settles quickly
    Serial.println(F("Warming up (3 s)..."));
    adc.startContinuous();
    delay(3000);
    adc.stopContinuous();

    printHelp();
}

// ══════════════════════════════════════════════════════════════════════════
void loop() {
    if (!Serial.available()) return;

    char c = (char)Serial.read();
    // Flush rest of line
    while (Serial.available()) Serial.read();

    switch (c) {
        case 'r': cmdSingleReading();   break;
        case 'n': cmdAveragedReading(); break;
        case 's': cmdSweep();           break;
        case 'd': adc.printRegisters(); break;
        case 'c': cmdCalibrate();       break;
        case 'h': printHelp();          break;
        default:
            Serial.print(F("Unknown command: "));
            Serial.println(c);
            Serial.println(F("Type 'h' for help."));
    }
}

// ── Commands ───────────────────────────────────────────────────────────────

/**
 * 'r' — Single raw reading with full detail.
 */
void cmdSingleReading() {
    ADC_Reading r = adc.readSingle();
    if (!r.valid) {
        Serial.println(F("[ERROR] Read timed out."));
        return;
    }

    Serial.println(F("── Single Reading ──────────────────────────"));
    Serial.print(F("  Raw code : ")); Serial.println(r.raw_code);
    Serial.print(F("  Voltage  : ")); Serial.print(r.voltage_V, 6); Serial.println(F(" V"));
    Serial.print(F("  Voltage  : ")); Serial.print(r.voltage_uV, 1); Serial.println(F(" µV"));
    Serial.println(F("────────────────────────────────────────────"));
}

/**
 * 'n' — Average N_AVG readings; report mean, std dev, min, max.
 */
void cmdAveragedReading() {
    Serial.print(F("Averaging "));
    Serial.print(N_AVG);
    Serial.println(F(" readings..."));

    MeasResult m = measureN(N_AVG);

    Serial.println(F("── Averaged Reading ────────────────────────"));
    Serial.print(F("  Samples  : ")); Serial.println(m.count);
    Serial.print(F("  Mean     : ")); Serial.print(m.mean_V, 6);   Serial.println(F(" V"));
    Serial.print(F("  Std dev  : ")); Serial.print(m.stddev_V * 1e6f, 1); Serial.println(F(" µV"));
    Serial.print(F("  Min      : ")); Serial.print(m.min_V, 6);    Serial.println(F(" V"));
    Serial.print(F("  Max      : ")); Serial.print(m.max_V, 6);    Serial.println(F(" V"));
    Serial.print(F("  Pk-pk    : ")); Serial.print((m.max_V - m.min_V) * 1e6f, 1); Serial.println(F(" µV"));
    Serial.println(F("────────────────────────────────────────────"));
}

/**
 * 's' — Interactive sweep through all set-points in SWEEP_POINTS[].
 *
 * For each point the operator sets the generator voltage and types Enter.
 * The sketch reads N_AVG samples, reports mean and error vs. expected,
 * and marks PASS/FAIL.
 *
 * Output format matches the table in Section 2.3 of the test doc.
 */
void cmdSweep() {
    Serial.println();
    Serial.println(F("══ Test A Sweep — DC Accuracy ═══════════════════════════════"));
    Serial.println(F("  Set V    │ ADC Mean (V) │ Error (mV)  │ Pass?"));
    Serial.println(F("  ─────────┼──────────────┼─────────────┼──────"));

    for (int i = 0; i < N_SWEEP; i++) {
        float target = SWEEP_POINTS[i];

        // Prompt operator
        Serial.println();
        Serial.print(F("  >> Set generator to "));
        Serial.print(target, 3);
        Serial.println(F(" V, confirm with multimeter, then press Enter..."));

        // Wait for Enter key
        waitForEnter();

        // Measure
        MeasResult m = measureN(N_AVG);

        float error_V  = m.mean_V - target;
        float error_mV = error_V * 1000.0f;
        bool  pass     = (fabsf(error_mV) <= (PASS_ERROR_V * 1000.0f));

        // Print table row
        Serial.print(F("  "));
        printFixed(target, 3, 8);
        Serial.print(F(" │ "));
        printFixed(m.mean_V, 6, 12);
        Serial.print(F(" │ "));
        printSigned(error_mV, 3, 11);
        Serial.print(F(" │ "));
        Serial.println(pass ? F("PASS ✓") : F("FAIL ✗"));

        // Extra note for the negative polarity check
        if (target < 0.0f) {
            if (m.mean_V < 0.0f) {
                Serial.println(F("  [OK] Negative input reads negative → differential polarity correct."));
            } else {
                Serial.println(F("  [!!] Negative input reads POSITIVE → check INPMUX polarity!"));
            }
        }
    }

    Serial.println();
    Serial.println(F("══ Sweep complete. ══════════════════════════════════════════"));
    Serial.print(F("   Pass criterion: ADC within ±"));
    Serial.print(PASS_ERROR_V * 1000.0f, 0);
    Serial.println(F(" mV of set voltage."));
    Serial.println();
}

/**
 * 'c' — Trigger self-offset calibration.
 */
void cmdCalibrate() {
    Serial.println(F("Running self-offset calibration (SFOCAL1)..."));
    bool ok = adc.calibrate();
    Serial.println(ok ? F("Calibration complete.") : F("Calibration timed out."));
}

// ── Helpers ────────────────────────────────────────────────────────────────

/**
 * Take n single readings; compute mean, std dev, min, max.
 * Uses Welford's online algorithm for numerically stable std dev.
 */
MeasResult measureN(int n) {
    MeasResult res = {0, 0, 1e30f, -1e30f, 0};

    float mean = 0.0f;
    float M2   = 0.0f;

    for (int i = 0; i < n; i++) {
        ADC_Reading r = adc.readSingle();
        if (!r.valid) continue;

        res.count++;
        float x   = r.voltage_V;
        float delta = x - mean;
        mean  += delta / res.count;
        M2    += delta * (x - mean);

        if (x < res.min_V) res.min_V = x;
        if (x > res.max_V) res.max_V = x;
    }

    res.mean_V   = mean;
    res.stddev_V = (res.count > 1) ? sqrtf(M2 / (res.count - 1)) : 0.0f;
    return res;
}

/**
 * Block until the operator presses Enter (newline / carriage return).
 */
void waitForEnter() {
    // Flush any pending bytes first
    while (Serial.available()) Serial.read();
    while (true) {
        if (Serial.available()) {
            char c = Serial.read();
            if (c == '\n' || c == '\r') return;
        }
    }
}

/**
 * Print a float right-aligned in a field of `width` chars.
 * (Arduino lacks printf padding, so we roll our own.)
 */
void printFixed(float val, int decimals, int width) {
    // Build string in a temp buffer
    char buf[20];
    dtostrf(val, width, decimals, buf);
    Serial.print(buf);
}

void printSigned(float val, int decimals, int width) {
    char buf[20];
    dtostrf(val, width, decimals, buf);
    Serial.print(buf);
}

void printHelp() {
    Serial.println();
    Serial.println(F("── Commands ────────────────────────────────"));
    Serial.println(F("  r  Single reading (raw + voltage)"));
    Serial.println(F("  n  Averaged reading (mean, stddev, pk-pk)"));
    Serial.println(F("  s  Run full DC sweep (Test A procedure)"));
    Serial.println(F("  d  Dump all ADS1263 registers"));
    Serial.println(F("  c  Self-offset calibration"));
    Serial.println(F("  h  This help message"));
    Serial.println(F("────────────────────────────────────────────"));
    Serial.println(F("Set generator voltage, verify with DMM, press Enter in sweep."));
    Serial.println();
}
