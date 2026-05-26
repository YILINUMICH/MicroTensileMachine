/**
 * @file SPI_Diagnostic_H7.ino
 * @brief Bare-metal SPI diagnostic for ADS1263 on Portenta H7 + Hat Carrier
 *
 * Pin mapping (confirmed):
 *   Pi pin 19 → SPI MOSI → ADS1263 DIN
 *   Pi pin 21 → SPI MISO → ADS1263 DOUT
 *   Pi pin 23 → SPI SCK  → ADS1263 SCLK
 *   Pi pin 15 → PE_6     → ADS1263 CS
 *   Pi pin 11 → PJ_11    → ADS1263 DRDY  (may conflict with LoRa)
 *   Pi pin 12 → PI_5     → ADS1263 RESET
 */

#include <SPI.h>

#define CS_PIN    PE_6
#define DRDY_PIN  PJ_11
#define RESET_PIN PI_5

#define CMD_RESET  0x06
#define CMD_START1 0x08
#define CMD_STOP1  0x0A
#define CMD_RDATA1 0x12
#define CMD_RREG   0x20
#define CMD_WREG   0x40

#define SPI_HAT SPI
SPISettings spiSettings(500000, MSBFIRST, SPI_MODE1);

void csLow()  { digitalWrite(CS_PIN, LOW);  delayMicroseconds(5); }
void csHigh() { delayMicroseconds(5); digitalWrite(CS_PIN, HIGH); }
uint8_t spiXfer(uint8_t b) { return SPI_HAT.transfer(b); }

uint8_t readReg(uint8_t reg) {
    SPI_HAT.beginTransaction(spiSettings);
    csLow();
    spiXfer(CMD_RREG | reg);
    spiXfer(0x00);
    delayMicroseconds(5);
    uint8_t val = spiXfer(0xFF);
    csHigh();
    SPI_HAT.endTransaction();
    return val;
}

void writeReg(uint8_t reg, uint8_t val) {
    SPI_HAT.beginTransaction(spiSettings);
    csLow();
    spiXfer(CMD_WREG | reg);
    spiXfer(0x00);
    spiXfer(val);
    csHigh();
    SPI_HAT.endTransaction();
    delayMicroseconds(10);
}

void sendCmd(uint8_t cmd) {
    SPI_HAT.beginTransaction(spiSettings);
    csLow();
    spiXfer(cmd);
    csHigh();
    SPI_HAT.endTransaction();
    delayMicroseconds(10);
}

bool waitDRDY(uint32_t timeout_ms = 2000) {
    uint32_t t = millis();
    while (digitalRead(DRDY_PIN) == HIGH) {
        if (millis() - t > timeout_ms) return false;
    }
    return true;
}

void hwReset() {
    digitalWrite(RESET_PIN, LOW);  delay(10);
    digitalWrite(RESET_PIN, HIGH); delay(10);
    sendCmd(CMD_RESET);
    delay(200);
}

void configureADC() {
    sendCmd(CMD_STOP1);
    delay(10);
    writeReg(0x01, 0x11);
    delay(150);
    writeReg(0x02, 0x05);
    writeReg(0x03, 0x00);
    writeReg(0x04, 0x40);
    writeReg(0x05, 0x88);
    writeReg(0x06, 0x01);
    writeReg(0x0F, 0x00);
    delay(10);
}

void cmdReadID() {
    uint8_t id = readReg(0x00);
    Serial.print(F("REG_ID = 0x")); Serial.println(id, HEX);
    Serial.println((id & 0xE0) == 0x20 ? F("→ PASS") : F("→ FAIL"));
}

void cmdReadAllRegs() {
    for (uint8_t i = 0; i <= 0x14; i++) {
        Serial.print(F("  REG[0x"));
        if (i < 0x10) Serial.print('0');
        Serial.print(i, HEX);
        Serial.print(F("] = 0x"));
        uint8_t v = readReg(i);
        if (v < 0x10) Serial.print('0');
        Serial.println(v, HEX);
    }
}

// 'd' — read with DRDY polling (normal mode)
void cmdRawConversions() {
    Serial.println(F("── DRDY mode (PJ_11) ───────────────────────"));
    Serial.print(F("  DRDY pin state before START: "));
    Serial.println(digitalRead(DRDY_PIN));

    configureADC();
    sendCmd(CMD_START1);
    delay(10);

    Serial.print(F("  DRDY pin state after START:  "));
    Serial.println(digitalRead(DRDY_PIN));

    for (int i = 0; i < 5; i++) {
        bool ready = waitDRDY(500);
        Serial.print(F("  Sample ")); Serial.print(i);
        if (!ready) {
            Serial.println(F(" → TIMEOUT (DRDY never went LOW)"));
            continue;
        }
        SPI_HAT.beginTransaction(spiSettings);
        csLow();
        uint8_t b[6];
        spiXfer(CMD_RDATA1);
        for (int j = 0; j < 6; j++) b[j] = spiXfer(0xFF);
        csHigh();
        SPI_HAT.endTransaction();

        int32_t val = ((int32_t)b[1]<<24)|((int32_t)b[2]<<16)|
                      ((int32_t)b[3]<<8)|(int32_t)b[4];
        Serial.print(F(" STATUS=0x")); Serial.print(b[0], HEX);
        Serial.print(F("  → ")); Serial.print(val);
        Serial.print(F("  → ")); Serial.print(((float)val/2147483648.0f)*2500.0f, 2);
        Serial.println(F(" mV"));
        while (!digitalRead(DRDY_PIN)) {}
    }
    sendCmd(CMD_STOP1);
}

// 'p' — poll mode: ignore DRDY, just delay and read
void cmdPollConversions() {
    Serial.println(F("── Poll mode (no DRDY) ─────────────────────"));
    Serial.println(F("  Waiting 25ms per sample (400SPS = 2.5ms, using 10x margin)"));

    configureADC();
    sendCmd(CMD_START1);
    delay(50);  // let filter settle

    for (int i = 0; i < 10; i++) {
        delay(25);  // 400 SPS = 2.5ms per sample, wait 10x

        SPI_HAT.beginTransaction(spiSettings);
        csLow();
        uint8_t b[6];
        spiXfer(CMD_RDATA1);
        for (int j = 0; j < 6; j++) b[j] = spiXfer(0xFF);
        csHigh();
        SPI_HAT.endTransaction();

        int32_t val = ((int32_t)b[1]<<24)|((int32_t)b[2]<<16)|
                      ((int32_t)b[3]<<8)|(int32_t)b[4];
        float mV = ((float)val / 2147483648.0f) * 2500.0f;

        Serial.print(F("  "));
        for (int j = 0; j < 6; j++) {
            if (b[j] < 0x10) Serial.print('0');
            Serial.print(b[j], HEX);
            Serial.print(' ');
        }
        Serial.print(F("  → ")); Serial.print(val);
        Serial.print(F("  → ")); Serial.print(mV, 3);
        Serial.println(F(" mV"));
    }
    sendCmd(CMD_STOP1);
    Serial.println(F("────────────────────────────────────────────"));
}

void printHelp() {
    Serial.println(F("── Commands ────────────────────────────────"));
    Serial.println(F("  i  Read ID"));
    Serial.println(F("  r  Read all registers"));
    Serial.println(F("  d  Read with DRDY (PJ_11)"));
    Serial.println(F("  p  Read with poll (no DRDY — use if d times out)"));
    Serial.println(F("  1  SPI_MODE1  3  SPI_MODE3  h  Help"));
    Serial.println(F("────────────────────────────────────────────"));
    Serial.println();
}

void setup() {
    Serial.begin(115200);
    delay(3000);

    pinMode(CS_PIN,    OUTPUT); digitalWrite(CS_PIN, HIGH);
    pinMode(RESET_PIN, OUTPUT); digitalWrite(RESET_PIN, HIGH);
    pinMode(DRDY_PIN,  INPUT);

    SPI.begin();
    delay(10);

    Serial.println(F("ADS1263 Diagnostic — Portenta H7"));
    Serial.println(F("CS=PE_6  DRDY=PJ_11  RESET=PI_5"));
    Serial.println();

    hwReset();
    cmdReadID();
    Serial.println();
    printHelp();
}

void loop() {
    if (!Serial.available()) return;
    char c = Serial.read();
    while (Serial.available()) Serial.read();
    switch (c) {
        case 'i': cmdReadID();           break;
        case 'r': cmdReadAllRegs();      break;
        case 'd': cmdRawConversions();   break;
        case 'p': cmdPollConversions();  break;
        case '1': spiSettings = SPISettings(500000,MSBFIRST,SPI_MODE1); hwReset(); cmdReadID(); break;
        case '3': spiSettings = SPISettings(500000,MSBFIRST,SPI_MODE3); hwReset(); cmdReadID(); break;
        case 'h': printHelp();           break;
    }
}
