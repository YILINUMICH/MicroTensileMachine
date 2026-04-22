/**
 * @file ADS1263_Driver.cpp
 * @brief ADS1263 driver — Portenta H7 (M4 core), EXTERNAL 5V ref (AVDD/AVSS)
 *
 * Defaults:
 *   - REFMUX = 0x24 (AVDD/AVSS), VREF = 5.000 V
 *   - INTERFACE = 0x05 (STATUS + checksum, 6-byte read frame)
 *   - PGA bypassed, AIN0(+) / AIN1(-)
 *   - DRDY uses INPUT_PULLUP; no external pullup required
 *   - SPI clock = 500 kHz (matches the proven Stable H7 driver — higher
 *     speeds exposed mbed SPI timing jitter on the H7 during testing).
 *
 * SFOCAL1 is NOT run at begin() — only run calibrate() after AIN0/AIN1
 * are shorted, otherwise OFCAL will capture whatever offset is present.
 */

#include "ADS1263_Driver.h"

ADS1263_Driver::ADS1263_Driver()
    : _spi(500000, MSBFIRST, SPI_MODE1),
      _rate(ADS1263_20SPS),
      _continuous(false),
      _refmux(ADS1263_REFMUX_AVDD_AVSS),
      _vref_V(ADS1263_VREF_V)
{
}

bool ADS1263_Driver::begin(ADS1263_DataRate_t rate) {
    _rate = rate;

    pinMode(ADS1263_CS_PIN, OUTPUT);
    pinMode(ADS1263_RESET_PIN, OUTPUT);
    pinMode(ADS1263_DRDY_PIN, INPUT_PULLUP);

    digitalWrite(ADS1263_CS_PIN, HIGH);
    digitalWrite(ADS1263_RESET_PIN, HIGH);

    SPI.begin();
    delay(10);

    reset();
    delay(100);

    uint8_t id = getDeviceID();
    if ((id & 0xF0) != 0x20) {
        Serial.print(F("ADS1263 not found. ID=0x"));
        Serial.println(id, HEX);
        return false;
    }
    Serial.print(F("ADS1263 found. ID=0x"));
    Serial.println(id, HEX);

    writeRegister(ADS1263_REG_POWER, 0x11);
    delay(150);

    writeRegister(ADS1263_REG_INTERFACE, 0x05);
    writeRegister(ADS1263_REG_MODE0,     0x00);
    writeRegister(ADS1263_REG_MODE1,     0x40);
    writeRegister(ADS1263_REG_MODE2,     0x80 | (_rate & 0x0F));
    writeRegister(ADS1263_REG_INPMUX,    0x01);
    writeRegister(ADS1263_REG_REFMUX,    _refmux);

    writeRegister(ADS1263_REG_IDACMUX,   0xFF);
    writeRegister(ADS1263_REG_IDACMAG,   0x00);

    writeRegister(ADS1263_REG_OFCAL0,    0x00);
    writeRegister(ADS1263_REG_OFCAL1,    0x00);
    writeRegister(ADS1263_REG_OFCAL2,    0x00);

    Serial.println(F("ADS1263 ready (ext 5V ref, PGA bypassed)"));
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
    writeRegister(ADS1263_REG_MODE2, 0x80 | (_rate & 0x0F));
}

float ADS1263_Driver::getCurrentDataRate() const {
    return rateToSPS(_rate);
}

void ADS1263_Driver::setRefMux(uint8_t refmux, float vref_V) {
    bool was = _continuous;
    if (_continuous) stopContinuous();

    _refmux = refmux;
    _vref_V = vref_V;
    writeRegister(ADS1263_REG_REFMUX, _refmux);
    delay(100);

    uint8_t rb = readRegister(ADS1263_REG_REFMUX);
    Serial.print(F("REFMUX set=0x")); Serial.print(_refmux, HEX);
    Serial.print(F("  readback=0x")); Serial.print(rb, HEX);
    Serial.println(rb == _refmux ? F(" OK") : F(" MISMATCH"));
    Serial.print(F("VREF = ")); Serial.print(_vref_V, 3); Serial.println(F(" V"));

    if (was) startContinuous();
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

ADC_Reading ADS1263_Driver::readPoll(uint32_t poll_ms) {
    ADC_Reading r = {};

    bool was = _continuous;
    if (_continuous) stopContinuous();

    sendCommand(ADS1263_CMD_START1);
    delay(poll_ms);

    int32_t code = readRawData();
    r.valid = true;
    r.raw_code = code;
    r.voltage_V = codeToVoltage(code);
    r.voltage_uV = r.voltage_V * 1e6f;
    r.timestamp_us = micros();

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

ADC_Reading ADS1263_Driver::readDirect() {
    ADC_Reading r = {};
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
    bool ok = waitForDataReady(5000);

    delay(100);
    writeRegister(ADS1263_REG_INTERFACE, 0x05);
    writeRegister(ADS1263_REG_MODE1,     0x40);
    writeRegister(ADS1263_REG_MODE2,     0x80 | (_rate & 0x0F));
    writeRegister(ADS1263_REG_INPMUX,    0x01);
    writeRegister(ADS1263_REG_REFMUX,    _refmux);

    uint8_t m2 = readRegister(ADS1263_REG_MODE2);
    Serial.print(F("Post-cal MODE2: 0x")); Serial.println(m2, HEX);
    return ok;
}

uint8_t ADS1263_Driver::getDeviceID() {
    return readRegister(ADS1263_REG_ID);
}

bool ADS1263_Driver::isConnected() {
    return (getDeviceID() & 0xF0) == 0x20;
}

void ADS1263_Driver::printConfig() {
    Serial.println(F("--- ADS1263 Config ---"));
    Serial.print(F("ID   : 0x")); Serial.println(getDeviceID(), HEX);
    Serial.print(F("Rate : ")); Serial.print(getCurrentDataRate()); Serial.println(F(" SPS"));
    Serial.println(F("PGA  : bypassed (gain=1)"));
    Serial.println(F("Input: AIN0(+) vs AIN1(-)   [AIN0=LCA Vo, AIN1=LCA GND]"));
    Serial.print(F("Ref  : REFMUX=0x")); Serial.print(_refmux, HEX);
    Serial.print(F("   VREF=")); Serial.print(_vref_V, 3); Serial.println(F(" V"));
    Serial.println(F("Frame: STATUS+DATA+CRC (INTERFACE=0x05)"));
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
    SPI.transfer(0x00);
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
    uint8_t status, crc;
    uint8_t buf[4];
    uint32_t raw = 0;

    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(5);

    SPI.transfer(ADS1263_CMD_RDATA1);
    status = SPI.transfer(0xFF);
    buf[0] = SPI.transfer(0xFF);
    buf[1] = SPI.transfer(0xFF);
    buf[2] = SPI.transfer(0xFF);
    buf[3] = SPI.transfer(0xFF);
    crc    = SPI.transfer(0xFF);
    (void)status; (void)crc;

    delayMicroseconds(5);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();

    raw  = ((uint32_t)buf[0] << 24);
    raw |= ((uint32_t)buf[1] << 16);
    raw |= ((uint32_t)buf[2] << 8);
    raw |= (uint32_t)buf[3];

    return (int32_t)raw;
}

float ADS1263_Driver::codeToVoltage(int32_t code) const {
    return ((float)code / 2147483648.0f) * _vref_V;
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
