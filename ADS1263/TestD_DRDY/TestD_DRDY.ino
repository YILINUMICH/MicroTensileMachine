/**
 * @file TestD_DRDY.ino
 * @brief Test D — DRDY Pin Verification and True Hardware Sampling
 *
 * Purpose:
 *   Verify that DRDY works correctly on PC_6 (D5, Pi header pin 31)
 *   and measure true effective SPS using hardware edge detection
 *   instead of poll mode delays.
 *
 * Hardware setup:
 *   - Run a jumper wire from ADS1263 DRDY pin → Pi header pin 31 (PWM3)
 *   - CS    = PE_6  (J2-53, Pi pin 15)
 *   - RESET = PI_5  (J1-56, Pi pin 12)
 *   - DRDY  = PC_6  (D5,    Pi pin 31)  ← NEW, replaces PJ_11
 *
 * Serial commands (115200 baud):
 *   'a'  — DRDY pin state check (is it toggling?)
 *   'b'  — DRDY timing — measure actual SPS
 *   'c'  — 1000 samples @ 400 SPS  via DRDY
 *   'd'  — 1000 samples @ 1200 SPS via DRDY
 *   'e'  — 1000 samples @ 2400 SPS via DRDY
 *   'f'  — compare DRDY vs poll mode at 400 SPS (100 samples each)
 *   'r'  — single reading via DRDY
 *   'h'  — help
 */

#include <SPI.h>

// ── Pin definitions ────────────────────────────────────────────────────────
#define CS_PIN    PE_6  // J2-53, Pi pin 15
#define RESET_PIN PI_5  // J1-56, Pi pin 12
#define DRDY_PIN  PC_6  // D5, Pi pin 31 (PWM3) — jumper from ADS1263 DRDY

// ── ADS1263 commands ───────────────────────────────────────────────────────
#define CMD_RESET  0x06
#define CMD_START1 0x08
#define CMD_STOP1  0x0A
#define CMD_RDATA1 0x12
#define CMD_RREG   0x20
#define CMD_WREG   0x40

SPISettings spiSettings(500000, MSBFIRST, SPI_MODE1);

// ── SPI helpers ────────────────────────────────────────────────────────────
void csLow()  { digitalWrite(CS_PIN, LOW);  delayMicroseconds(5); }
void csHigh() { delayMicroseconds(5); digitalWrite(CS_PIN, HIGH); }
uint8_t spiXfer(uint8_t b) { return SPI.transfer(b); }

uint8_t readReg(uint8_t reg) {
    SPI.beginTransaction(spiSettings);
    csLow();
    spiXfer(CMD_RREG | reg);
    spiXfer(0x00);
    delayMicroseconds(5);
    uint8_t val = spiXfer(0xFF);
    csHigh();
    SPI.endTransaction();
    return val;
}

void writeReg(uint8_t reg, uint8_t val) {
    SPI.beginTransaction(spiSettings);
    csLow();
    spiXfer(CMD_WREG | reg);
    spiXfer(0x00);
    spiXfer(val);
    csHigh();
    SPI.endTransaction();
    delayMicroseconds(10);
}

void sendCmd(uint8_t cmd) {
    SPI.beginTransaction(spiSettings);
    csLow();
    spiXfer(cmd);
    csHigh();
    SPI.endTransaction();
    delayMicroseconds(10);
}

void hwReset() {
    digitalWrite(RESET_PIN, LOW);  delay(10);
    digitalWrite(RESET_PIN, HIGH); delay(10);
    sendCmd(CMD_RESET);
    delay(200);
}

void configureADC(uint8_t mode2) {
    sendCmd(CMD_STOP1);
    delay(10);
    writeReg(0x01, 0x11);
    delay(150);
    writeReg(0x02, 0x05);
    writeReg(0x03, 0x00);
    writeReg(0x04, 0x40);
    writeReg(0x05, mode2);
    writeReg(0x06, 0x01);
    writeReg(0x0F, 0x00);
    delay(10);
}

bool waitDRDY(uint32_t timeout_ms = 100) {
    uint32_t t = millis();
    while (digitalRead(DRDY_PIN) == HIGH) {
        if (millis() - t > timeout_ms) return false;
    }
    return true;
}

float readOneSample(bool* ok = nullptr) {
    SPI.beginTransaction(spiSettings);
    csLow();
    spiXfer(CMD_RDATA1);
    uint8_t status = spiXfer(0xFF);
    uint8_t b0 = spiXfer(0xFF);
    uint8_t b1 = spiXfer(0xFF);
    uint8_t b2 = spiXfer(0xFF);
    uint8_t b3 = spiXfer(0xFF);
    spiXfer(0xFF);  // CRC discard
    csHigh();
    SPI.endTransaction();
    int32_t raw = ((int32_t)b0 << 24) | ((int32_t)b1 << 16) |
                  ((int32_t)b2 << 8)  |  (int32_t)b3;
    if (ok) *ok = (status & 0x40) != 0;
    return ((float)raw / 2147483648.0f) * 2500.0f;  // mV
}

// ── Pin diagnostic — test a pin with different modes ──────────────────────
void testPin(PinName pin, const char* name) {
    Serial.print(F("  ")); Serial.print(name);

    // Floating input
    pinMode(pin, INPUT);
    delay(5);
    int floating = digitalRead(pin);

    // With pullup
    pinMode(pin, INPUT_PULLUP);
    delay(5);
    int pullup = digitalRead(pin);

    // With pulldown
    pinMode(pin, INPUT_PULLDOWN);
    delay(5);
    int pulldown = digitalRead(pin);

    Serial.print(F(" | float=")); Serial.print(floating);
    Serial.print(F(" pullup=")); Serial.print(pullup);
    Serial.print(F(" pulldown=")); Serial.print(pulldown);

    // Interpret result
    if (pullup == LOW && pulldown == LOW) {
        Serial.println(F("  → actively driven LOW (DRDY asserted?)"));
    } else if (pullup == HIGH && pulldown == HIGH) {
        Serial.println(F("  → actively driven HIGH (something holds it)"));
    } else if (pullup == HIGH && pulldown == LOW) {
        Serial.println(F("  → floating (no active driver) ✓ usable as DRDY input"));
    } else {
        Serial.println(F("  → weak driver present"));
    }

    // Restore to plain input
    pinMode(pin, INPUT);
}

// ── Command: pin diagnostic ────────────────────────────────────────────────
void cmdPinDiagnostic() {
    Serial.println(F("\n── Test D-P: Pin Pull Diagnostic ───────────────"));
    Serial.println(F("  ADC stopped (idle state):"));
    sendCmd(CMD_STOP1);
    delay(20);

    testPin(PJ_11, "PJ_11 (orig DRDY, Pi pin 11)");
    testPin(PC_6,  "PC_6  (new  DRDY, Pi pin 31)");

    Serial.println();
    Serial.println(F("  ADC converting (START1):"));
    sendCmd(CMD_START1);
    delay(20);

    testPin(PJ_11, "PJ_11 (orig DRDY, Pi pin 11)");
    testPin(PC_6,  "PC_6  (new  DRDY, Pi pin 31)");

    sendCmd(CMD_STOP1);

    Serial.println();
    Serial.println(F("  Interpretation:"));
    Serial.println(F("  float=H pullup=H pulldown=L → floating → usable as DRDY"));
    Serial.println(F("  float=H pullup=H pulldown=H → driven HIGH → NOT usable"));
    Serial.println(F("  float=L pullup=L pulldown=L → driven LOW  → DRDY active!"));
    Serial.println(F("────────────────────────────────────────────────"));
}

// ── Test A: DRDY pin state check ──────────────────────────────────────────
void cmdDRDYCheck() {
    Serial.println(F("\n── Test D-A: DRDY Pin State Check ─────────────"));

    // Apply pulldown to DRDY_PIN before starting
    pinMode(DRDY_PIN, INPUT_PULLDOWN);
    delay(5);

    Serial.print(F("  DRDY pin (PC_6) state at rest (pulldown): "));
    Serial.println(digitalRead(DRDY_PIN) == LOW ? F("LOW") : F("HIGH"));

    sendCmd(CMD_START1);
    delay(10);

    Serial.println(F("  Sampling DRDY every 1ms for 50ms (ADC running):"));
    Serial.print(F("  "));
    for (int i = 0; i < 50; i++) {
        Serial.print(digitalRead(DRDY_PIN));
        Serial.print(' ');
        delay(1);
    }
    Serial.println();

    sendCmd(CMD_STOP1);
    delay(10);

    sendCmd(CMD_START1);
    delay(5);
    int transitions = 0;
    int last = digitalRead(DRDY_PIN);
    for (int i = 0; i < 1000; i++) {
        delayMicroseconds(500);
        int cur = digitalRead(DRDY_PIN);
        if (cur != last) { transitions++; last = cur; }
    }
    sendCmd(CMD_STOP1);

    Serial.print(F("  Transitions in 500ms: ")); Serial.println(transitions);
    if (transitions > 5)
        Serial.println(F("  → DRDY is toggling ✓"));
    else
        Serial.println(F("  → DRDY stuck — jumper not connected or wrong pin"));
    Serial.println(F("────────────────────────────────────────────────"));

    // Restore plain input
    pinMode(DRDY_PIN, INPUT);
}

// ── Test B: DRDY timing ───────────────────────────────────────────────────
void cmdDRDYTiming() {
    Serial.println(F("\n── Test D-B: DRDY Timing (Actual SPS) ─────────"));
    pinMode(DRDY_PIN, INPUT_PULLDOWN);

    uint8_t rates[] = { 0x88, 0x89, 0x8A };
    const char* labels[] = { "400 SPS ", "1200 SPS", "2400 SPS" };

    for (int r = 0; r < 3; r++) {
        configureADC(rates[r]);
        sendCmd(CMD_START1);
        delay(20);

        if (!waitDRDY(500)) {
            Serial.print(F("  ")); Serial.print(labels[r]);
            Serial.println(F("  → TIMEOUT — DRDY not working"));
            sendCmd(CMD_STOP1);
            delay(20);
            continue;
        }

        uint32_t t_start = micros();
        for (int i = 0; i < 100; i++) {
            while (digitalRead(DRDY_PIN) == LOW) {}
            while (digitalRead(DRDY_PIN) == HIGH) {}
        }
        uint32_t t_end = micros();
        sendCmd(CMD_STOP1);
        delay(20);

        float actual_sps = 100.0f / ((t_end - t_start) / 1e6f);
        Serial.print(F("  ")); Serial.print(labels[r]);
        Serial.print(F("  →  actual: ")); Serial.print(actual_sps, 1);
        Serial.println(F(" SPS"));
    }
    Serial.println(F("────────────────────────────────────────────────"));
    pinMode(DRDY_PIN, INPUT);
}

// ── Collect N samples via DRDY ─────────────────────────────────────────────
void cmdCollectDRDY(uint8_t mode2, const char* label, int n) {
    Serial.println();
    Serial.print(F("── Test D: DRDY collect — ")); Serial.println(label);

    pinMode(DRDY_PIN, INPUT_PULLDOWN);
    configureADC(mode2);

    static float v_buf[1000];
    static uint32_t t_buf[1000];
    if (n > 1000) n = 1000;

    sendCmd(CMD_START1);
    delay(20);

    for (int i = 0; i < 5; i++) {
        if (!waitDRDY(200)) { Serial.println(F("  TIMEOUT — DRDY not working")); sendCmd(CMD_STOP1); pinMode(DRDY_PIN, INPUT); return; }
        readOneSample();
        while (digitalRead(DRDY_PIN) == LOW) {}
    }

    uint32_t t_start = millis();
    int collected = 0;
    for (int i = 0; i < n; i++) {
        if (!waitDRDY(200)) { Serial.print(F("  TIMEOUT at sample ")); Serial.println(i); break; }
        t_buf[i] = millis() - t_start;
        v_buf[i] = readOneSample();
        collected++;
        while (digitalRead(DRDY_PIN) == LOW) {}
    }
    sendCmd(CMD_STOP1);

    uint32_t t_total = millis() - t_start;
    float actual_sps = collected * 1000.0f / t_total;
    float vmin = 1e30f, vmax = -1e30f, vsum = 0;
    for (int i = 0; i < collected; i++) {
        if (v_buf[i] < vmin) vmin = v_buf[i];
        if (v_buf[i] > vmax) vmax = v_buf[i];
        vsum += v_buf[i];
    }

    Serial.print(F("  Collected : ")); Serial.println(collected);
    Serial.print(F("  Total time: ")); Serial.print(t_total); Serial.println(F(" ms"));
    Serial.print(F("  Actual SPS: ")); Serial.println(actual_sps, 1);
    Serial.print(F("  Mean: ")); Serial.print(vsum/collected, 3); Serial.println(F(" mV"));
    Serial.print(F("  Pk-pk: ")); Serial.print(vmax-vmin, 3); Serial.println(F(" mV"));
    Serial.println(F("────────────────────────────────────────────────"));
    pinMode(DRDY_PIN, INPUT);
}

// ── DRDY vs Poll comparison ────────────────────────────────────────────────
void cmdCompare() {
    Serial.println(F("\n── Test D-F: DRDY vs Poll @ 400 SPS ───────────"));
    const int N = 100;

    // Poll mode
    configureADC(0x88);
    sendCmd(CMD_START1);
    delay(50);
    uint32_t t0 = millis();
    for (int i = 0; i < N; i++) { delay(5); readOneSample(); }
    uint32_t poll_time = millis() - t0;
    sendCmd(CMD_STOP1);
    delay(20);
    Serial.print(F("  Poll mode:  ")); Serial.print(poll_time);
    Serial.print(F(" ms  →  ")); Serial.print(N*1000.0f/poll_time, 1); Serial.println(F(" eff SPS"));

    // DRDY mode
    pinMode(DRDY_PIN, INPUT_PULLDOWN);
    configureADC(0x88);
    sendCmd(CMD_START1);
    delay(20);
    for (int i = 0; i < 3; i++) { waitDRDY(200); readOneSample(); while(digitalRead(DRDY_PIN)==LOW){} }

    t0 = millis();
    bool drdy_ok = true;
    for (int i = 0; i < N; i++) {
        if (!waitDRDY(200)) { drdy_ok = false; break; }
        readOneSample();
        while (digitalRead(DRDY_PIN) == LOW) {}
    }
    uint32_t drdy_time = millis() - t0;
    sendCmd(CMD_STOP1);
    pinMode(DRDY_PIN, INPUT);

    if (drdy_ok) {
        float drdy_sps = N*1000.0f/drdy_time;
        Serial.print(F("  DRDY mode:  ")); Serial.print(drdy_time);
        Serial.print(F(" ms  →  ")); Serial.print(drdy_sps, 1); Serial.println(F(" eff SPS"));
        Serial.print(F("  Improvement: ")); Serial.print(drdy_sps/(N*1000.0f/poll_time), 2); Serial.println(F("×"));
    } else {
        Serial.println(F("  DRDY mode:  TIMEOUT — DRDY not working"));
    }
    Serial.println(F("────────────────────────────────────────────────"));
}

// ── Single reading ─────────────────────────────────────────────────────────
void cmdSingle() {
    pinMode(DRDY_PIN, INPUT_PULLDOWN);
    sendCmd(CMD_START1);
    if (!waitDRDY(500)) {
        Serial.println(F("[ERROR] DRDY timeout"));
        sendCmd(CMD_STOP1);
        pinMode(DRDY_PIN, INPUT);
        return;
    }
    bool ok;
    float v = readOneSample(&ok);
    sendCmd(CMD_STOP1);
    pinMode(DRDY_PIN, INPUT);
    Serial.print(F("V = ")); Serial.print(v, 3);
    Serial.print(F(" mV  ADC1_RDY=")); Serial.println(ok ? F("1 ✓") : F("0 ✗"));
}

// ── Help ───────────────────────────────────────────────────────────────────
void printHelp() {
    Serial.println(F("── Commands ────────────────────────────────────"));
    Serial.println(F("  p  Pin pull diagnostic (PJ_11 and PC_6)"));
    Serial.println(F("  a  DRDY pin state check"));
    Serial.println(F("  b  DRDY timing — measure actual SPS"));
    Serial.println(F("  c  1000 samples @ 400 SPS  via DRDY"));
    Serial.println(F("  d  1000 samples @ 1200 SPS via DRDY"));
    Serial.println(F("  e  1000 samples @ 2400 SPS via DRDY"));
    Serial.println(F("  f  DRDY vs poll comparison @ 400 SPS"));
    Serial.println(F("  r  Single reading via DRDY"));
    Serial.println(F("  h  Help"));
    Serial.println(F("────────────────────────────────────────────────"));
    Serial.println(F("DRDY pin: PC_6 (D5, Pi pin 31)"));
    Serial.println(F("Jumper: ADS1263 DRDY → Pi header pin 31"));
    Serial.println();
}

// ══════════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    delay(3000);

    pinMode(CS_PIN,    OUTPUT); digitalWrite(CS_PIN, HIGH);
    pinMode(RESET_PIN, OUTPUT); digitalWrite(RESET_PIN, HIGH);

    SPI.begin();
    delay(10);

    Serial.println(F("╔══════════════════════════════════════════╗"));
    Serial.println(F("║  ADS1263 — Test D: DRDY Verification     ║"));
    Serial.println(F("║  Portenta H7 Elite + Hat Carrier         ║"));
    Serial.println(F("╚══════════════════════════════════════════╝"));
    Serial.println();
    Serial.println(F("Pin mapping:"));
    Serial.println(F("  CS    = PE_6  (Pi pin 15)"));
    Serial.println(F("  RESET = PI_5  (Pi pin 12)"));
    Serial.println(F("  DRDY  = PC_6  (Pi pin 31) ← jumper required"));
    Serial.println();

    hwReset();
    configureADC(0x88);

    uint8_t id = readReg(0x00);
    Serial.print(F("ADS1263 ID = 0x")); Serial.print(id, HEX);
    Serial.println((id & 0xE0) == 0x20 ? F("  → PASS ✓") : F("  → FAIL ✗"));
    Serial.println();

    // ── Pin pull diagnostic at boot ────────────────────────────────────────
    Serial.println(F("── Boot Pin Diagnostic ─────────────────────────"));
    Serial.println(F("  [ADC idle]"));
    testPin(PJ_11, "PJ_11 (Pi pin 11)");
    testPin(PC_6,  "PC_6  (Pi pin 31)");

    sendCmd(CMD_START1);
    delay(20);
    Serial.println(F("  [ADC converting]"));
    testPin(PJ_11, "PJ_11 (Pi pin 11)");
    testPin(PC_6,  "PC_6  (Pi pin 31)");
    sendCmd(CMD_STOP1);
    Serial.println(F("────────────────────────────────────────────────"));
    Serial.println();

    printHelp();
}

void loop() {
    if (!Serial.available()) return;
    char c = Serial.read();
    while (Serial.available()) Serial.read();
    switch (c) {
        case 'p': cmdPinDiagnostic();                      break;
        case 'a': cmdDRDYCheck();                          break;
        case 'b': cmdDRDYTiming();                         break;
        case 'c': cmdCollectDRDY(0x88, "400 SPS",  1000); break;
        case 'd': cmdCollectDRDY(0x89, "1200 SPS", 1000); break;
        case 'e': cmdCollectDRDY(0x8A, "2400 SPS", 1000); break;
        case 'f': cmdCompare();                            break;
        case 'r': cmdSingle();                             break;
        case 'h': printHelp();                             break;
        default: Serial.print(F("Unknown: ")); Serial.println(c);
    }
}
