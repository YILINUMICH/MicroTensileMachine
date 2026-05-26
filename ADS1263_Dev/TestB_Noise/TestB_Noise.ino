/**
 * @file TestB_Noise.ino
 * @brief ADS1263 Verification — Test B: Noise and Stability at 400 SPS
 *
 * Section 2.4 of the test procedure.
 *
 * Purpose:
 *   Characterize the ADC noise floor at 400 SPS — the operating rate needed
 *   for 100–200 Hz dual-channel SMA actuation measurements. This is the
 *   metric that actually matters for the application, not the DC accuracy
 *   from Test A.
 *
 * Setup:
 *   - Stable DC voltage (~1.200 V) on AIN0  (use battery divider or
 *     voltage reference — NOT a waveform generator, which adds its own noise)
 *   - AIN1 → GND  (differential negative input)
 *   - No load cell or INA818 connected yet
 *
 * Serial commands (115200 baud):
 *   'b'  — run full Test B (2000 samples, compute all metrics, PASS/FAIL)
 *   'r'  — single reading
 *   'l'  — live stream (continuous readings, press any key to stop)
 *   'd'  — dump registers
 *   'c'  — self-offset calibration
 *   'h'  — help
 *
 * Pass criteria (Section 2.4 table):
 *   Std deviation  < 50 µV
 *   Peak-to-peak   < 300 µV
 *   Noise-free bits > 15
 *   Mean vs input  < 5 mV
 *
 * ADS1263 configuration:
 *   - AIN0(+) vs AIN1(−)   [INPMUX = 0x01]
 *   - Internal 2.5 V ref   [REFMUX = 0x00]
 *   - PGA bypassed (gain=1)[MODE2  = 0x88]  (0x80 | ADS1263_400SPS)
 *   - 400 SPS, Sinc3 filter
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
static const int    N_SAMPLES       = 2000;   // Section 2.4: 2000 samples @ 400 SPS = 5 s
static const float  VREF            = 2.5f;   // Internal reference (V)
static const float  FSR             = 2.0f * VREF;  // Full-scale range = 5.0 V

// Pass criteria from test doc
static const float  PASS_STDDEV_UV  = 50.0f;   // µV
static const float  PASS_PKPK_UV    = 300.0f;  // µV
static const float  PASS_NFB        = 15.0f;   // noise-free bits
static const float  PASS_MEAN_MV    = 5.0f;    // mV vs input

// ── Globals ────────────────────────────────────────────────────────────────
ADS1263_Driver adc;

// Sample buffer — 2000 floats × 4 bytes = 8 kB
// Arduino Uno has 2 kB SRAM, so we can't buffer all samples.
// Instead we compute statistics online (Welford) and stream raw codes to Serial.
// If you're on a Mega/Portenta, you can enable the buffer by setting BUFFER_SAMPLES 1.
#define BUFFER_SAMPLES 0

#if BUFFER_SAMPLES
  static float sample_buf[N_SAMPLES];
#endif

// ── Stats struct ───────────────────────────────────────────────────────────
struct NoiseStats {
    int     count;
    float   mean_V;
    float   stddev_V;     // RMS noise
    float   min_V;
    float   max_V;
    float   pkpk_V;
    float   noise_free_bits;
    bool    valid;
};

// ── Forward declarations ───────────────────────────────────────────────────
void     printHelp();
void     cmdRunTestB();
void     cmdSingleReading();
void     cmdLiveStream();
void     cmdCalibrate();
NoiseStats collectAndAnalyze(int n, bool print_progress);
void     printStats(const NoiseStats& s, float input_V);
void     printPassFail(const NoiseStats& s, float input_V);
void     waitForEnter();
void     waitForAnyKey();
void     printFixed(float val, int decimals, int width);

// ══════════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    while (!Serial) {}

    Serial.println(F("╔══════════════════════════════════════════╗"));
    Serial.println(F("║  ADS1263 — Test B: Noise & Stability     ║"));
    Serial.println(F("║  Section 2.4 — 400 SPS Characterization  ║"));
    Serial.println(F("╚══════════════════════════════════════════╝"));
    Serial.println();

    // Init at 400 SPS — this is the application operating rate
    if (!adc.begin(ADS1263_400SPS)) {
        Serial.println(F("FATAL: ADS1263 init failed. Check wiring."));
        while (1) {}
    }

    adc.printConfig();
    adc.printRegisters();

    // Warm up: 1 second at 400 SPS
    Serial.println(F("Warming up (2 s)..."));
    adc.startContinuous();
    delay(2000);
    adc.stopContinuous();

    // Self-offset calibration with input connected
    Serial.println(F("Running offset calibration..."));
    adc.calibrate();
    Serial.println(F("Done."));
    Serial.println();

    printHelp();
}

// ══════════════════════════════════════════════════════════════════════════
void loop() {
    if (!Serial.available()) return;

    char c = (char)Serial.read();
    while (Serial.available()) Serial.read();

    switch (c) {
        case 'b': cmdRunTestB();      break;
        case 'r': cmdSingleReading(); break;
        case 'l': cmdLiveStream();    break;
        case 'd': adc.printRegisters(); break;
        case 'c': cmdCalibrate();     break;
        case 'h': printHelp();        break;
        default:
            Serial.print(F("Unknown: '"));
            Serial.print(c);
            Serial.println(F("'  — type 'h' for help"));
    }
}

// ── Commands ───────────────────────────────────────────────────────────────

/**
 * 'b' — Full Test B procedure.
 *
 * Prompts the operator to enter the actual input voltage (from DMM),
 * then collects N_SAMPLES at 400 SPS, computes all metrics, and
 * reports PASS/FAIL against Section 2.4 criteria.
 *
 * Raw codes are streamed to Serial during collection so you can
 * copy-paste into Python/Excel for plotting if needed.
 */
void cmdRunTestB() {
    Serial.println();
    Serial.println(F("══ Test B: Noise & Stability @ 400 SPS ══════════════════════"));
    Serial.println();
    Serial.println(F("  Connect a stable DC source (~1.2 V) to AIN0."));
    Serial.println(F("  AIN1 → GND.  Measure exact voltage with DMM."));
    Serial.println();
    Serial.println(F("  Enter DMM reading in mV (e.g. 1200) then press Enter:"));
    Serial.print(F("  > "));

    // Read DMM value from serial
    float input_mV = readFloatFromSerial();
    float input_V  = input_mV / 1000.0f;

    Serial.println();
    Serial.print(F("  Input reference: "));
    Serial.print(input_V, 4);
    Serial.println(F(" V"));
    Serial.println();
    Serial.println(F("  Collecting 2000 samples at 400 SPS (~5 seconds)..."));
    Serial.println(F("  Streaming raw codes (copy for offline analysis):"));
    Serial.println(F("  ── RAW_START ──────────────────────────────────"));

    NoiseStats s = collectAndAnalyze(N_SAMPLES, true);

    Serial.println(F("  ── RAW_END ────────────────────────────────────"));
    Serial.println();

    printStats(s, input_V);
    Serial.println();
    printPassFail(s, input_V);

    Serial.println();
    Serial.println(F("══ Test B complete. ══════════════════════════════════════════"));
    Serial.println();
}

/**
 * 'r' — Single reading.
 */
void cmdSingleReading() {
    ADC_Reading r = adc.readSingle();
    if (!r.valid) { Serial.println(F("[ERROR] Read timed out.")); return; }

    Serial.println(F("── Single Reading ──────────────────────────"));
    Serial.print(F("  Raw  : ")); Serial.println(r.raw_code);
    Serial.print(F("  V    : ")); Serial.print(r.voltage_V, 6); Serial.println(F(" V"));
    Serial.print(F("  µV   : ")); Serial.print(r.voltage_uV, 1); Serial.println(F(" µV"));
    Serial.println(F("────────────────────────────────────────────"));
}

/**
 * 'l' — Live stream: print voltage once per conversion until key pressed.
 * Useful for watching noise in real time.
 */
void cmdLiveStream() {
    Serial.println(F("Live stream at 400 SPS. Press any key to stop."));
    Serial.println(F("Format: index, voltage_V, voltage_uV"));

    adc.startContinuous();
    delay(50);

    uint32_t idx = 0;
    while (true) {
        if (Serial.available()) { Serial.read(); break; }

        delay(5);  // poll mode — 400 SPS = 2.5ms, wait 5ms
        ADC_Reading r = adc.readDirect();
        if (!r.valid) continue;

        Serial.print(idx++);
        Serial.print(F(", "));
        Serial.print(r.voltage_V, 6);
        Serial.print(F(", "));
        Serial.println(r.voltage_uV, 1);
    }

    adc.stopContinuous();
    Serial.println(F("Stream stopped."));
}

/**
 * 'c' — Self-offset calibration.
 */
void cmdCalibrate() {
    Serial.println(F("Running SFOCAL1..."));
    Serial.println(adc.calibrate() ? F("Done.") : F("Timed out."));
}

// ── Core: collect N samples, compute stats online (Welford) ───────────────

NoiseStats collectAndAnalyze(int n, bool print_progress) {
    NoiseStats s = {0, 0, 0, 1e30f, -1e30f, 0, 0, false};

    float mean = 0.0f;
    float M2   = 0.0f;

    adc.startContinuous();
    delay(50);  // filter settle

    // Poll mode — DRDY pin (PJ_11) is occupied by LoRa on Portenta H7.
    // At 400 SPS each conversion takes 2.5ms; wait 5ms per sample.
    for (int i = 0; i < n; i++) {
        delay(5);
        ADC_Reading r = adc.readDirect();
        if (!r.valid) { i--; continue; }

        float x = r.voltage_V;

        // Welford online mean + variance
        s.count++;
        float delta = x - mean;
        mean += delta / s.count;
        M2   += delta * (x - mean);

        if (x < s.min_V) s.min_V = x;
        if (x > s.max_V) s.max_V = x;

        // Stream raw code for offline analysis
        if (print_progress) {
            Serial.println(r.raw_code);
        }
    }

    adc.stopContinuous();

    s.mean_V   = mean;
    s.stddev_V = (s.count > 1) ? sqrtf(M2 / (s.count - 1)) : 0.0f;
    s.pkpk_V   = s.max_V - s.min_V;

    if (s.stddev_V > 0) {
        s.noise_free_bits = logf(FSR / (6.6f * s.stddev_V)) / logf(2.0f);
    }

    s.valid = (s.count > 0);
    return s;
}

// ── Reporting ──────────────────────────────────────────────────────────────

void printStats(const NoiseStats& s, float input_V) {
    float mean_err_mV = (s.mean_V - input_V) * 1000.0f;

    Serial.println(F("── Test B Results ──────────────────────────────────────────"));
    Serial.print(F("  Samples collected : ")); Serial.println(s.count);
    Serial.print(F("  Input (DMM)       : ")); Serial.print(input_V, 4); Serial.println(F(" V"));
    Serial.println();
    Serial.print(F("  Mean              : ")); Serial.print(s.mean_V, 6);            Serial.println(F(" V"));
    Serial.print(F("  Mean error        : ")); Serial.print(mean_err_mV, 3);          Serial.println(F(" mV"));
    Serial.print(F("  Std deviation     : ")); Serial.print(s.stddev_V * 1e6f, 2);   Serial.println(F(" µV  (RMS noise)"));
    Serial.print(F("  Min               : ")); Serial.print(s.min_V, 6);             Serial.println(F(" V"));
    Serial.print(F("  Max               : ")); Serial.print(s.max_V, 6);             Serial.println(F(" V"));
    Serial.print(F("  Peak-to-peak      : ")); Serial.print(s.pkpk_V * 1e6f, 2);    Serial.println(F(" µV"));
    Serial.print(F("  Noise-free bits   : ")); Serial.print(s.noise_free_bits, 2);   Serial.println(F(" bits"));
    Serial.println(F("────────────────────────────────────────────────────────────"));
}

void printPassFail(const NoiseStats& s, float input_V) {
    float mean_err_mV  = fabsf((s.mean_V - input_V) * 1000.0f);
    float stddev_uV    = s.stddev_V * 1e6f;
    float pkpk_uV      = s.pkpk_V  * 1e6f;

    bool p1 = stddev_uV        < PASS_STDDEV_UV;
    bool p2 = pkpk_uV          < PASS_PKPK_UV;
    bool p3 = s.noise_free_bits > PASS_NFB;
    bool p4 = mean_err_mV      < PASS_MEAN_MV;
    bool all = p1 && p2 && p3 && p4;

    Serial.println(F("── Pass/Fail (Section 2.4 criteria) ───────────────────────"));

    // Metric | Measured | Limit | Pass?
    auto row = [](const __FlashStringHelper* metric,
                  float measured, float limit, bool pass,
                  const __FlashStringHelper* unit) {
        Serial.print(F("  "));
        Serial.print(metric);
        Serial.print(F(" : "));
        char buf[12]; sprintf(buf, "%8.2f", measured); Serial.print(buf);
        Serial.print(unit);
        Serial.print(F("  (limit "));
        sprintf(buf, "%6.1f", limit); Serial.print(buf);
        Serial.print(unit);
        Serial.print(F(")  "));
        Serial.println(pass ? F("PASS ✓") : F("FAIL ✗"));
    };

    row(F("Std dev  "), stddev_uV,          PASS_STDDEV_UV, p1, F(" µV"));
    row(F("Pk-pk    "), pkpk_uV,            PASS_PKPK_UV,   p2, F(" µV"));
    row(F("NFB      "), s.noise_free_bits,  PASS_NFB,       p3, F(" bit"));
    row(F("Mean err "), mean_err_mV,        PASS_MEAN_MV,   p4, F(" mV "));

    Serial.println(F("  ──────────────────────────────────────────────────────────"));
    Serial.print(F("  Overall: "));
    Serial.println(all ? F("PASS ✓  — ADC noise floor acceptable for 100-200 Hz use")
                       : F("FAIL ✗  — review failures above"));
    Serial.println(F("────────────────────────────────────────────────────────────"));
}

// ── Utility ────────────────────────────────────────────────────────────────

/**
 * Read a float from Serial (typed by operator, terminated by Enter).
 * Blocks until a valid number is received.
 */
float readFloatFromSerial() {
    char buf[16];
    uint8_t idx = 0;
    memset(buf, 0, sizeof(buf));

    while (true) {
        if (!Serial.available()) continue;
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            if (idx > 0) break;
        } else if ((c >= '0' && c <= '9') || c == '.' || c == '-') {
            if (idx < sizeof(buf) - 1) {
                buf[idx++] = c;
                Serial.print(c);  // echo
            }
        }
    }
    return atof(buf);
}

void waitForEnter() {
    while (Serial.available()) Serial.read();
    while (true) {
        if (Serial.available()) {
            char c = Serial.read();
            if (c == '\n' || c == '\r') return;
        }
    }
}

void waitForAnyKey() {
    while (!Serial.available()) {}
    while (Serial.available()) Serial.read();
}

void printFixed(float val, int decimals, int width) {
    char buf[20];
    sprintf(buf, "%width.decimalsf", val);
    Serial.print(buf);
}

void printHelp() {
    Serial.println(F("── Commands ────────────────────────────────"));
    Serial.println(F("  b  Run full Test B (2000 samples, all metrics)"));
    Serial.println(F("  r  Single reading"));
    Serial.println(F("  l  Live stream (press any key to stop)"));
    Serial.println(F("  d  Dump ADS1263 registers"));
    Serial.println(F("  c  Self-offset calibration"));
    Serial.println(F("  h  This help"));
    Serial.println(F("────────────────────────────────────────────"));
    Serial.println(F("Connect stable ~1.2V DC to AIN0, AIN1 to GND."));
    Serial.println(F("Use a battery divider or voltage ref, not a"));
    Serial.println(F("waveform generator (adds its own noise)."));
    Serial.println();
}
