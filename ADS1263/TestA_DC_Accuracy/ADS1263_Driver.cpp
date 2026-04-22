/**
 * @file ADS1263_Driver.cpp
 * @brief ADS1263 driver for Arduino Uno
 *
 * Configured for: GSO050 → INA818 (gain=200) → ADS1263 AIN0 single-ended
 * - PGA bypassed (gain=1), internal 2.5V reference
 * - Input: AIN0 vs AINCOM
 */

#include "ADS1263_Driver.h"

ADS1263_Driver::ADS1263_Driver()
    : _spi(2000000, MSBFIRST, SPI_MODE1),  // 2 MHz, mode 1 (CPOL=0, CPHA=1)
      _rate(ADS1263_20SPS),
      _continuous(false)
{
}

bool ADS1263_Driver::begin(ADS1263_DataRate_t rate) {
    _rate = rate;

    pinMode(ADS1263_CS_PIN, OUTPUT);
    pinMode(ADS1263_RESET_PIN, OUTPUT);
    pinMode(ADS1263_DRDY_PIN, INPUT);

    digitalWrite(ADS1263_CS_PIN, HIGH);
    digitalWrite(ADS1263_RESET_PIN, HIGH);

    SPI.begin();
    delay(10);

    // Hardware + software reset
    reset();
    delay(100);

    // Verify device ID
    uint8_t id = getDeviceID();
    if ((id & 0xF0) != 0x20) {
        Serial.print(F("ADS1263 not found. ID=0x"));
        Serial.println(id, HEX);
        return false;
    }
    Serial.print(F("ADS1263 found. ID=0x"));
    Serial.println(id, HEX);

    // --- Register configuration ---

    // POWER (0x01): Enable internal reference
    writeRegister(ADS1263_REG_POWER, 0x11);
    delay(150);  // Reference settling

    // INTERFACE (0x02): Default (no checksum, no status byte prepend)
    writeRegister(ADS1263_REG_INTERFACE, 0x00);

    // MODE0 (0x03): No chop, no delay
    writeRegister(ADS1263_REG_MODE0, 0x00);

    // MODE1 (0x04): Sinc3 filter (FILTER[7:5]=010), no simultaneous ADC2
    writeRegister(ADS1263_REG_MODE1, 0x40);

    // MODE2 (0x05): PGA bypassed (gain=1), data rate in DR[3:0]
    //   Bit 7 = 1 (bypass PGA), Bits [6:4] = 000 (gain=1), Bits [3:0] = DR
    writeRegister(ADS1263_REG_MODE2, 0x80 | (_rate & 0x0F));

    // INPMUX (0x06): AIN0 positive, AINCOM negative → 0x0A
    //   High nibble = 0 (AIN0), Low nibble = A (AINCOM)
    writeRegister(ADS1263_REG_INPMUX, 0x01);

    // REFMUX (0x0F): Use internal 2.5V reference
    //   RMUXP[5:3]=100 (internal 2.5V), RMUXN[2:0]=100 (internal AVSS)
    writeRegister(ADS1263_REG_REFMUX, 0x00);

    // Disable IDAC (not needed)
    writeRegister(ADS1263_REG_IDACMUX, 0xFF);
    writeRegister(ADS1263_REG_IDACMAG, 0x00);

    // Self-offset calibration
    if (!calibrate()) {
        Serial.println(F("Calibration failed (continuing)"));
    }

    Serial.println(F("ADS1263 ready"));
    return true;
}

void ADS1263_Driver::reset() {
    digitalWrite(ADS1263_RESET_PIN, LOW);
    delay(10);
    digitalWrite(ADS1263_RESET_PIN, HIGH);
    delay(10);

    sendCommand(ADS1263_CMD_RESET);
    delay(50);

    _continuous = false;
}

void ADS1263_Driver::setDataRate(ADS1263_DataRate_t rate) {
    _rate = rate;
    // DR is in MODE2[3:0], preserve PGA bypass bit
    writeRegister(ADS1263_REG_MODE2, 0x80 | (_rate & 0x0F));
}

float ADS1263_Driver::getCurrentDataRate() const {
    return rateToSPS(_rate);
}

void ADS1263_Driver::startContinuous() {
    sendCommand(ADS1263_CMD_START1);
    _continuous = true;
    delay(10);
}

void ADS1263_Driver::stopContinuous() {
    sendCommand(ADS1263_CMD_STOP1);
    _continuous = false;
}

ADC_Reading ADS1263_Driver::readSingle() {
    ADC_Reading r = {};

    bool was = _continuous;
    if (_continuous) stopContinuous();

    sendCommand(ADS1263_CMD_START1);

    if (waitForDataReady(1000)) {
        int32_t code = readRawData();
        r.valid = true;
        r.raw_code = code;
        r.voltage_V = codeToVoltage(code);
        r.voltage_uV = r.voltage_V * 1e6f;
        r.timestamp_us = micros();
    }

    if (was) startContinuous();
    return r;
}

ADC_Reading ADS1263_Driver::readContinuous() {
    ADC_Reading r = {};
    if (!dataReady()) return r;

    int32_t code = readRawData();
    r.valid = true;
    r.raw_code = code;
    r.voltage_V = codeToVoltage(code);
    r.voltage_uV = r.voltage_V * 1e6f;
    r.timestamp_us = micros();
    return r;
}

bool ADS1263_Driver::dataReady() const {
    return digitalRead(ADS1263_DRDY_PIN) == LOW;
}

bool ADS1263_Driver::calibrate() {
    sendCommand(ADS1263_CMD_SFOCAL1);
    return waitForDataReady(5000);
}

uint8_t ADS1263_Driver::getDeviceID() {
    return readRegister(ADS1263_REG_ID);
}

bool ADS1263_Driver::isConnected() {
    return (getDeviceID() & 0xF0) == 0x20;
}

void ADS1263_Driver::printConfig() {
    Serial.println(F("--- ADS1263 Config ---"));
    Serial.print(F("ID: 0x")); Serial.println(getDeviceID(), HEX);
    Serial.print(F("Rate: ")); Serial.print(getCurrentDataRate()); Serial.println(F(" SPS"));
    Serial.print(F("PGA: bypassed (gain=1, external INA818 gain=200)"));
    Serial.println();
    Serial.print(F("Input: AIN0 vs AINCOM"));
    Serial.println();
    Serial.print(F("Ref: internal 2.5V"));
    Serial.println();
    Serial.println(F("----------------------"));
}

void ADS1263_Driver::printRegisters() {
    Serial.println(F("--- Register Dump ---"));
    for (uint8_t i = 0; i <= 0x14; i++) {
        uint8_t val = readRegister(i);
        Serial.print(F("0x"));
        if (i < 0x10) Serial.print('0');
        Serial.print(i, HEX);
        Serial.print(F(": 0x"));
        if (val < 0x10) Serial.print('0');
        Serial.println(val, HEX);
    }
    Serial.println(F("---------------------"));
}

// --- Private ---

void ADS1263_Driver::writeRegister(uint8_t reg, uint8_t value) {
    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(10);

    SPI.transfer(ADS1263_CMD_WREG | reg);
    SPI.transfer(0x00);   // 1 byte
    SPI.transfer(value);

    delayMicroseconds(10);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();
    delayMicroseconds(10);
}

uint8_t ADS1263_Driver::readRegister(uint8_t reg) {
    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(10);

    SPI.transfer(ADS1263_CMD_RREG | reg);
    SPI.transfer(0x00);
    delayMicroseconds(10);
    uint8_t val = SPI.transfer(0xFF);

    delayMicroseconds(10);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();
    return val;
}

void ADS1263_Driver::sendCommand(uint8_t cmd) {
    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(10);

    SPI.transfer(cmd);

    delayMicroseconds(10);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();
    delayMicroseconds(10);
}

int32_t ADS1263_Driver::readRawData() {
    int32_t data = 0;

    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(10);

    SPI.transfer(ADS1263_CMD_RDATA1);
    delayMicroseconds(10);

    // Read 4 bytes MSB-first (32-bit signed)
    data  = ((int32_t)SPI.transfer(0xFF)) << 24;
    data |= ((int32_t)SPI.transfer(0xFF)) << 16;
    data |= ((int32_t)SPI.transfer(0xFF)) << 8;
    data |= ((int32_t)SPI.transfer(0xFF));

    delayMicroseconds(10);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();

    return data;
}

float ADS1263_Driver::codeToVoltage(int32_t code) const {
    // PGA bypassed → gain = 1
    // Vref = 2.5V (internal)
    // V = code / 2^31 * Vref
    return ((float)code / 2147483648.0f) * 2.5f;
}

bool ADS1263_Driver::waitForDataReady(uint32_t timeout_ms) {
    uint32_t start = millis();
    while (!dataReady()) {
        if (millis() - start > timeout_ms) return false;
        delayMicroseconds(100);
    }
    return true;
}

float ADS1263_Driver::rateToSPS(ADS1263_DataRate_t rate) const {
    const float table[] = {
        2.5, 5, 10, 16.6, 20, 50, 60, 100,
        400, 1200, 2400, 4800, 7200, 14400, 19200, 38400
    };
    if (rate <= ADS1263_38400SPS) return table[rate];
    return 20.0;
}
