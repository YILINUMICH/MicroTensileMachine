/**
 * @file LoadCell.ino
 * @brief Arduino Uno load cell reader — LCA amplifier + ADS1263
 *        with EXTERNAL 5V reference (AVDD/AVSS).
 *
 * Signal chain:
 *   Load cell (bridge)
 *       → LCA-9PC / LCA-RTC amplifier  (set for ±5V output, 4-wire or 6-wire)
 *       → ADS1263 AIN0(+) / AIN1(-)
 *
 * Only the COMPRESSION side (0 → +5V) is used. No tension readings.
 *
 * ── LCA jumper reminders (see PDF, p.5) ───────────────────────────────
 *   E4 : install    → 5V excitation   (or leave out for 10V exc.)
 *   E3 : NO 'f'     → voltage output (not 4-20 mA)
 *   E2 : set per your load cell's mV/V and the chosen excitation.
 *        Goal:  full-scale load → +5.000 V at J2 Vo
 *   E5,E6 : install → 4-wire mode (simplest); remove for 6-wire remote sense
 *   E7 : install    → on-board 87.325 kΩ shunt-cal resistor
 *   E1 a+a : normal polarity (compression → positive output)
 *
 * ── Wiring (Arduino Uno) ──────────────────────────────────────────────
 *   ADS1263 SCK   → D13            LCA J2 Pin 3 (Vo) → ADS1263 AIN0
 *   ADS1263 DIN   → D11 (MOSI)     LCA J2 Pin 2 (GND) → ADS1263 AIN1  AND  GND
 *   ADS1263 DOUT  → D12 (MISO)     LCA J2 Pin 4 → 12-24 V DC supply (+)
 *   ADS1263 CS    → D10            LCA J2 Pin 5 → Supply GND (common with Uno GND)
 *   ADS1263 DRDY  → D2 (INPUT_PULLUP, no external pullup needed)
 *   ADS1263 RESET → D9
 *   ADS1263 DVDD  → 3.3 V or 5 V (match Uno logic)
 *   ADS1263 AVDD  → clean 5.000 V  (this IS the reference — keep it quiet)
 *   ADS1263 AVSS  → GND
 *   ADS1263 DGND  → GND
 *
 *   NOTE: ADC AVDD must be a precision / low-noise 5V for ratiometric
 *   accuracy. If your 5V rail is noisy, either
 *     (a) feed AVDD from a REF5050 / LT1019-5, or
 *     (b) wire a REF5050 across AIN2(+)/AIN3(-) and send 'x2' command
 *         below to switch REFMUX to external AIN2/AIN3.
 *
 * ── Serial commands (115200 baud) ─────────────────────────────────────
 *   h   help
 *   i   print ADS1263 config + registers
 *   r   single raw reading (code + volts)
 *   s   stream continuous readings (press any key to stop)
 *   t   tare (zero) — run with NO load on cell
 *   c   two-point calibration — prompts for a known weight (grams)
 *   z   reset calibration to defaults
 *   x1  use AVDD/AVSS 5V reference  (default)
 *   x2  use external precision ref on AIN2/AIN3  (you supply the value)
 *   f   self-offset calibration (SFOCAL1) — short AIN0↔AIN1 first!
 */

#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// ── Calibration state (lives in RAM; Uno EEPROM could be added later) ──
struct CalState {
    float tare_V;        // voltage reading at zero load
    float scale_g_per_V; // grams per volt  (default = FS_grams / FS_volts)
};

// Defaults: 50 g full-scale → 5.000 V on the ADC → 10 g/V
// The 'c' command lets the user override scale_g_per_V with any load cell.
static CalState cal = { 0.0f, 10.0f };

// ── Helpers ────────────────────────────────────────────────────────────
static float readAveragedV(int n = 16) {
    float sum = 0.0f;
    int got = 0;
    for (int i = 0; i < n; i++) {
        ADC_Reading r = adc.readSingle();
        if (r.valid) { sum += r.voltage_V; got++; }
    }
    return (got > 0) ? (sum / got) : 0.0f;
}

static void printHelp() {
    Serial.println(F("── Commands ────────────────────────────────"));
    Serial.println(F("  h    help"));
    Serial.println(F("  i    print config + registers"));
    Serial.println(F("  r    single raw reading"));
    Serial.println(F("  s    stream (any key stops)"));
    Serial.println(F("  t    tare (no load on cell)"));
    Serial.println(F("  c    2-point calibration (prompts for known grams)"));
    Serial.println(F("  z    reset cal to defaults (tare=0, scale=10 g/V)"));
    Serial.println(F("  x1   use AVDD/AVSS 5V reference (default)"));
    Serial.println(F("  x2   use external ref on AIN2/AIN3"));
    Serial.println(F("  f    self offset-cal (short AIN0-AIN1 first)"));
    Serial.println(F("────────────────────────────────────────────"));
}

static void doTare() {
    Serial.println(F("Taring... keep cell unloaded."));
    delay(500);
    float v = readAveragedV(32);
    cal.tare_V = v;
    Serial.print(F("Tare V = "));
    Serial.print(cal.tare_V, 6);
    Serial.println(F(" V"));
}

static void doCalibrate() {
    Serial.println(F("── 2-point calibration ─────────────"));
    Serial.println(F("Step 1: remove all load, press ENTER"));
    while (!Serial.available()) {}
    while (Serial.available()) Serial.read();
    float v_zero = readAveragedV(32);
    Serial.print(F("  V @ zero = ")); Serial.print(v_zero, 6); Serial.println(F(" V"));

    Serial.println(F("Step 2: place known weight on cell,"));
    Serial.println(F("        type weight in GRAMS then ENTER (e.g. 45.0)"));
    String buf;
    while (true) {
        if (Serial.available()) {
            char c = Serial.read();
            if (c == '\n' || c == '\r') {
                if (buf.length() > 0) break;
            } else {
                buf += c;
            }
        }
    }
    float known_g = buf.toFloat();
    if (known_g <= 0.0f) {
        Serial.println(F("Invalid weight; cal aborted."));
        return;
    }

    delay(500);
    float v_known = readAveragedV(32);
    Serial.print(F("  V @ ")); Serial.print(known_g, 3);
    Serial.print(F(" g = ")); Serial.print(v_known, 6); Serial.println(F(" V"));

    float dV = v_known - v_zero;
    if (fabsf(dV) < 1e-6f) {
        Serial.println(F("Delta V too small — check wiring / LCA gain."));
        return;
    }

    cal.tare_V        = v_zero;
    cal.scale_g_per_V = known_g / dV;

    Serial.println(F("── Calibration result ─────────────"));
    Serial.print(F("  tare_V        = ")); Serial.print(cal.tare_V, 6);        Serial.println(F(" V"));
    Serial.print(F("  scale_g_per_V = ")); Serial.print(cal.scale_g_per_V, 4); Serial.println(F(" g/V"));
    Serial.println(F("──────────────────────────────────"));
}

static void resetCal() {
    cal.tare_V = 0.0f;
    cal.scale_g_per_V = 10.0f;
    Serial.println(F("Cal reset to defaults (tare=0, scale=10 g/V)."));
}

static void printReading(const ADC_Reading& r) {
    float v_load = r.voltage_V - cal.tare_V;
    if (v_load < 0.0f) v_load = 0.0f;  // compression-only
    float grams = v_load * cal.scale_g_per_V;

    Serial.print(r.raw_code);
    Serial.print(F("\t"));
    Serial.print(r.voltage_V, 6);
    Serial.print(F(" V\t"));
    Serial.print(v_load, 6);
    Serial.print(F(" V (net)\t"));
    Serial.print(grams, 3);
    Serial.println(F(" g"));
}

static void cmdSingle() {
    ADC_Reading r = adc.readSingle();
    if (!r.valid) { Serial.println(F("[ERROR] read failed")); return; }
    printReading(r);
}

static void cmdStream() {
    Serial.println(F("Streaming… press any key to stop."));
    Serial.println(F("raw_code\tV_abs\tV_net\tgrams"));
    adc.startContinuous();
    while (!Serial.available()) {
        ADC_Reading r = adc.readContinuous();
        if (r.valid) printReading(r);
    }
    adc.stopContinuous();
    while (Serial.available()) Serial.read();
    Serial.println(F("Stopped."));
}

static void cmdSwitchRef(char which) {
    if (which == '1') {
        adc.setRefMux(ADS1263_REFMUX_AVDD_AVSS, 5.0f);
        Serial.println(F("Reference → AVDD/AVSS, VREF = 5.000 V"));
    } else if (which == '2') {
        // External precision ref (e.g. REF5050) on AIN2(+)/AIN3(-):
        // RMUXP[5:3]=010, RMUXN[2:0]=010 → 0x12
        adc.setRefMux(ADS1263_REFMUX_EXT_AIN23, 5.0f);
        Serial.println(F("Reference → ext AIN2(+)/AIN3(-), VREF = 5.000 V"));
        Serial.println(F("(If your precision ref is not 5.000 V, edit cmdSwitchRef)"));
    }
}

static void cmdSelfCal() {
    Serial.println(F("Running SFOCAL1… make sure AIN0 and AIN1 are SHORTED."));
    delay(1500);
    if (adc.calibrate()) Serial.println(F("SFOCAL1 OK"));
    else                 Serial.println(F("SFOCAL1 timeout"));
}

// ══════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    while (!Serial) {}

    Serial.println(F("╔══════════════════════════════════════════╗"));
    Serial.println(F("║  LCA + ADS1263 Load Cell Reader          ║"));
    Serial.println(F("║  External 5V ref (AVDD/AVSS)             ║"));
    Serial.println(F("║  Compression only (0…+5V range)          ║"));
    Serial.println(F("╚══════════════════════════════════════════╝"));

    if (!adc.begin(ADS1263_20SPS)) {
        Serial.println(F("FATAL: ADS1263 init failed"));
        while (1) {}
    }
    adc.printConfig();

    Serial.println(F("Warming up (15s)..."));
    adc.startContinuous();
    delay(15000);
    adc.stopContinuous();

    doTare();
    Serial.println();
    printHelp();
}

void loop() {
    if (!Serial.available()) return;

    char c = Serial.read();
    // allow 2-char commands starting with 'x'
    if (c == 'x') {
        // wait briefly for the second char
        uint32_t t0 = millis();
        while (!Serial.available() && (millis() - t0) < 500) {}
        char c2 = Serial.available() ? Serial.read() : 0;
        while (Serial.available()) Serial.read();
        cmdSwitchRef(c2);
        return;
    }

    while (Serial.available()) Serial.read();

    switch (c) {
        case 'h': printHelp();            break;
        case 'i': adc.printConfig();
                  adc.printRegisters();   break;
        case 'r': cmdSingle();            break;
        case 's': cmdStream();            break;
        case 't': doTare();               break;
        case 'c': doCalibrate();          break;
        case 'z': resetCal();             break;
        case 'f': cmdSelfCal();           break;
        case '\n': case '\r':             break;
        default:
            Serial.print(F("Unknown: '")); Serial.print(c);
            Serial.println(F("'  — press 'h' for help"));
    }
}
