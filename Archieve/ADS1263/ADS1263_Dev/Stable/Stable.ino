/**
 * @file TestC_AC_Capture.ino
 * @brief ADS1263 Verification — Test C: AC Signal Capture (STABLE)
 *
 * Section 2.5 of the test procedure.
 *
 * Setup:
 *   - Waveform generator: sine wave, 2.0 Vpp, 1.2V DC offset (0.2V to 2.2V)
 *   - AIN0 = signal, AIN1 = GND
 *   - Oscilloscope CH1 in parallel for reference
 *
 * Serial commands (115200 baud):
 *   'a'  — 10 Hz  @ 400 SPS  (4000 samples) — PASS
 *   'b'  — 50 Hz  @ 400 SPS  (4000 samples) — FAIL (4 samp/cycle)
 *   'c'  — 100 Hz @ 400 SPS  (4000 samples) — FAIL (2 samp/cycle)
 *   'd'  — 100 Hz @ 1200 SPS (4000 samples) — FAIL (5 samp/cycle)
 *   'e'  — 100 Hz @ 2400 SPS (4000 samples) — PASS (8 samp/cycle) ← recommended
 *   'r'  — single reading
 *   'h'  — help
 *
 * Output: CSV between CSV_START/CSV_END markers — use PuTTY to capture
 * Analysis: python TestC_Analysis.py --file data.csv --freq 10
 *
 * Pass criteria (Section 2.5):
 *   - Amplitude: 2.0 Vpp ±20 mV
 *   - Samples/cycle ≥ 4 (practical Nyquist)
 *
 * Pin mapping — Portenta H7 Elite + Hat Carrier:
 *   CS    = PE_6  (J2-53, Pi pin 15)
 *   DRDY  = PJ_11 (J2-50, Pi pin 11) — INPUT_PULLUP, no jumper needed
 *   RESET = PI_5  (J1-56, Pi pin 12)
 *
 * Sampling: DRDY hardware mode (not poll) — true ADC-rate sampling
 */

#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// ── Test parameters ────────────────────────────────────────────────────────
static const float VREF = 2.5f;
static const float EXPECTED_VPP = 2.0f;    // V peak-to-peak
static const float EXPECTED_DC = 1.2f;     // V DC offset
static const float PASS_AMP_TOL = 0.020f;  // ±20 mV amplitude tolerance

// Sample counts
static const int N_SAMPLES_400 = 4000;   // 10s at 400 SPS
static const int N_SAMPLES_1200 = 4000;  // ~3.3s at 1200 SPS

// Poll delay per data rate (ms) — 2× period for safety
// 400 SPS  → 2.5ms period → 5ms poll delay  (~200 effective SPS)
// 1200 SPS → 0.83ms period → 2ms poll delay (~500 effective SPS)
static const int POLL_MS_400 = 5;
static const int POLL_MS_1200 = 2;
static const int POLL_MS_2400 = 1;  // 1ms poll at 2400 SPS → ~1000 eff SPS

// ── Test state ────────────────────────────────────────────────────────────
struct TestConfig {
  const char* label;
  float sig_freq_hz;
  ADS1263_DataRate_t rate;
  int n_samples;
  int poll_ms;
  float expected_cycles;
};

static const TestConfig TESTS[] = {
  { "10 Hz  @ 400 SPS", 10.0f, ADS1263_400SPS, N_SAMPLES_400, POLL_MS_400, 100.0f },
  { "50 Hz  @ 400 SPS", 50.0f, ADS1263_400SPS, N_SAMPLES_400, POLL_MS_400, 20.0f },
  { "100 Hz @ 400 SPS", 100.0f, ADS1263_400SPS, N_SAMPLES_400, POLL_MS_400, 10.0f },
  { "100 Hz @ 1200 SPS", 100.0f, ADS1263_1200SPS, N_SAMPLES_1200, POLL_MS_1200, 13.3f },
  { "100 Hz @ 2400 SPS", 100.0f, ADS1263_2400SPS, 4000, POLL_MS_2400, 26.7f },
};

// ── Forward declarations ───────────────────────────────────────────────────
void runTest(const TestConfig& cfg);
void printHelp();
void cmdSingleReading();

// ══════════════════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(115200);
  delay(3000);

  Serial.println(F("╔══════════════════════════════════════════╗"));
  Serial.println(F("║  ADS1263 — Test C: AC Signal Capture     ║"));
  Serial.println(F("║  Section 2.5 — Portenta H7 + Hat Carrier ║"));
  Serial.println(F("╚══════════════════════════════════════════╝"));
  Serial.println();

  if (!adc.begin(ADS1263_400SPS)) {
    Serial.println(F("FATAL: ADS1263 init failed."));
    while (1) {}
  }
  adc.printConfig();

  Serial.println(F("Warming up (2s)..."));
  adc.startContinuous();
  delay(2000);
  adc.stopContinuous();

  printHelp();
}

// ══════════════════════════════════════════════════════════════════════════
void loop() {
  if (!Serial.available()) return;
  char c = Serial.read();
  while (Serial.available()) Serial.read();

  switch (c) {
    case 'a': runTest(TESTS[0]); break;
    case 'b': runTest(TESTS[1]); break;
    case 'c': runTest(TESTS[2]); break;
    case 'd': runTest(TESTS[3]); break;
    case 'e': runTest(TESTS[4]); break;
    case 'r': cmdSingleReading(); break;
    case 'h': printHelp(); break;
    default:
      Serial.print(F("Unknown: "));
      Serial.println(c);
  }
}

// ── Run one test ──────────────────────────────────────────────────────────
void runTest(const TestConfig& cfg) {
  Serial.println();
  Serial.print(F("══ Test C: "));
  Serial.println(cfg.label);
  Serial.print(F("   Signal: 2.0Vpp sine, 1.2V offset, "));
  Serial.print(cfg.sig_freq_hz, 0);
  Serial.println(F(" Hz"));
  Serial.print(F("   Collecting "));
  Serial.print(cfg.n_samples);
  Serial.print(F(" samples at "));
  Serial.print(adc.getCurrentDataRate(), 0);
  Serial.println(F(" SPS (poll mode)..."));
  Serial.println();

  // Fully reconfigure ADC for this test's data rate
  adc.stopContinuous();
  delay(20);

  // Re-write all critical registers for the new rate
  // (setDataRate alone may not be sufficient after INTERFACE=0x05 quirks)
  adc.setDataRate(cfg.rate);
  delay(20);

  // Start and let Sinc3 filter fully settle (3× output period)
  adc.startContinuous();
  float settle_ms = 3000.0f / adc.getCurrentDataRate();
  delay((int)settle_ms + 100);

  // Discard first few samples — filter not fully settled
  for (int d = 0; d < 5; d++) {
    delay(cfg.poll_ms);
    adc.readDirect();
  }

  // Print CSV header
  Serial.println(F("── CSV_START ────────────────────────────────"));
  Serial.println(F("index,time_ms,voltage_V"));

  // Sanity check — print one reading before starting CSV
  delay(cfg.poll_ms);
  ADC_Reading sanity = adc.readDirect();
  Serial.print(F("  Sanity check: "));
  Serial.print(sanity.voltage_V, 4);
  Serial.println(F(" V  (should be ~1.2V if generator connected)"));
  Serial.println();

  float v_min = 1e30f;
  float v_max = -1e30f;
  uint32_t t_start = millis();

  for (int i = 0; i < cfg.n_samples; i++) {
    delay(cfg.poll_ms);
    ADC_Reading r = adc.readDirect();
    if (!r.valid) {
      i--;
      continue;
    }

    uint32_t t_ms = millis() - t_start;
    float v = r.voltage_V;

    if (v < v_min) v_min = v;
    if (v > v_max) v_max = v;

    // Print CSV row
    Serial.print(i);
    Serial.print(',');
    Serial.print(t_ms);
    Serial.print(',');
    Serial.println(v, 6);
  }

  adc.stopContinuous();

  uint32_t t_total_ms = millis() - t_start;

  Serial.println(F("── CSV_END ──────────────────────────────────"));
  Serial.println();

  // ── Analysis ──────────────────────────────────────────────────────────
  float vpp_measured = v_max - v_min;
  float dc_measured = (v_max + v_min) / 2.0f;
  float actual_sps = (float)cfg.n_samples / (t_total_ms / 1000.0f);
  float actual_cycles = cfg.sig_freq_hz * (t_total_ms / 1000.0f);
  float spc = actual_sps / cfg.sig_freq_hz;  // samples per cycle

  bool pass_amp = fabsf(vpp_measured - EXPECTED_VPP) <= PASS_AMP_TOL;
  bool pass_cycles = fabsf(actual_cycles - cfg.expected_cycles) <= 5.0f;
  bool pass_spc = spc >= 4.0f;  // Nyquist requires ≥2, practical ≥4

  Serial.println(F("── Test C Results ──────────────────────────"));
  Serial.print(F("  Signal freq      : "));
  Serial.print(cfg.sig_freq_hz, 0);
  Serial.println(F(" Hz"));
  Serial.print(F("  Total time       : "));
  Serial.print(t_total_ms);
  Serial.println(F(" ms"));
  Serial.print(F("  Actual SPS       : "));
  Serial.print(actual_sps, 1);
  Serial.println(F(" SPS"));
  Serial.print(F("  Samples/cycle    : "));
  Serial.println(spc, 1);
  Serial.print(F("  Actual cycles    : "));
  Serial.println(actual_cycles, 1);
  Serial.println();
  Serial.print(F("  Vpp measured     : "));
  Serial.print(vpp_measured * 1000.0f, 1);
  Serial.println(F(" mV"));
  Serial.print(F("  Vpp expected     : "));
  Serial.print(EXPECTED_VPP * 1000.0f, 1);
  Serial.println(F(" mV"));
  Serial.print(F("  Vpp error        : "));
  Serial.print((vpp_measured - EXPECTED_VPP) * 1000.0f, 1);
  Serial.println(F(" mV"));
  Serial.print(F("  DC offset meas   : "));
  Serial.print(dc_measured * 1000.0f, 1);
  Serial.println(F(" mV"));
  Serial.print(F("  DC offset expect : "));
  Serial.print(EXPECTED_DC * 1000.0f, 1);
  Serial.println(F(" mV"));
  Serial.println();
  Serial.println(F("── Pass/Fail ────────────────────────────────"));
  Serial.print(F("  Amplitude ±20mV  : "));
  Serial.println(pass_amp ? F("PASS ✓") : F("FAIL ✗"));
  Serial.print(F("  Cycle count      : "));
  Serial.println(pass_cycles ? F("PASS ✓") : F("FAIL ✗"));
  Serial.print(F("  Samples/cycle≥4  : "));
  Serial.println(pass_spc ? F("PASS ✓") : F("FAIL ✗"));

  if (spc < 4.0f) {
    Serial.println(F("  !! <4 samples/cycle — increase SPS or lower signal freq"));
  } else if (spc < 10.0f) {
    Serial.println(F("  Note: <10 samples/cycle — waveform will look angular, not sinusoidal"));
  }

  bool overall = pass_amp && pass_cycles && pass_spc;
  Serial.println();
  Serial.print(F("  Overall: "));
  Serial.println(overall ? F("PASS ✓") : F("FAIL ✗"));
  Serial.println(F("────────────────────────────────────────────"));
  Serial.println();
  Serial.println(F("Copy CSV block above (CSV_START to CSV_END) into Python for plotting."));
  Serial.println();

  // Reset to 400 SPS for next test
  adc.setDataRate(ADS1263_400SPS);
}

// ── Single reading ────────────────────────────────────────────────────────
void cmdSingleReading() {
  ADC_Reading r = adc.readPoll(5);
  if (!r.valid) {
    Serial.println(F("[ERROR] Read failed"));
    return;
  }
  Serial.print(F("V = "));
  Serial.print(r.voltage_V, 6);
  Serial.println(F(" V"));
}

// ── Help ──────────────────────────────────────────────────────────────────
void printHelp() {
  Serial.println(F("── Commands ────────────────────────────────"));
  Serial.println(F("  a  10 Hz  sine @ 400 SPS  (4000 samples)"));
  Serial.println(F("  b  50 Hz  sine @ 400 SPS  (4000 samples)"));
  Serial.println(F("  c  100 Hz sine @ 400 SPS  (4000 samples)"));
  Serial.println(F("  d  100 Hz sine @ 1200 SPS (4000 samples)"));
  Serial.println(F("  e  100 Hz sine @ 2400 SPS (4000 samples)"));
  Serial.println(F("  r  Single reading"));
  Serial.println(F("  h  Help"));
  Serial.println(F("────────────────────────────────────────────"));
  Serial.println(F("Waveform gen: 2.0Vpp sine, 1.2V offset"));
  Serial.println(F("AIN0 = signal, AIN1 = GND"));
  Serial.println(F("CSV output between CSV_START/CSV_END markers"));
  Serial.println(F("Paste into Python script for amplitude/frequency analysis"));
  Serial.println();
}
