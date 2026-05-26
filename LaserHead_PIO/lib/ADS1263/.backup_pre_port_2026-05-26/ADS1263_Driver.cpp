/**
 * @file ADS1263_Driver.cpp
 * @brief ADS1263 dual-ADC driver — Portenta H7 (M4 core)
 *
 * Both ADC paths share:
 *   - SPI at 500 kHz, MODE1
 *   - INTERFACE = 0x05 (STATUS + CHK enabled on RDATAx reads)
 *   - INPUT_PULLUP on the (unused) DRDY pin so it doesn't float
 *
 * begin() verifies the device ID and writes the chip-level registers
 * (POWER, INTERFACE, disable IDAC). Neither ADC starts until you call
 * configureADCx() + startADCx(). configureADC1() writes MODE0, MODE1,
 * MODE2, INPMUX, REFMUX; configureADC2() writes ADC2CFG, ADC2MUX.
 *
 * Read frames under INTERFACE = 0x05:
 *   RDATA1: 6 bytes — STATUS + DATA3(MSB) + DATA2 + DATA1 + DATA0 + CHK
 *   RDATA2: 5 bytes — STATUS + DATA3(MSB) + DATA2 + DATA1 + CHK
 *
 * DRDY is not used for gating. PJ_11 is held by the onboard LoRa IRQ on
 * the H7, and ADC2 has no DRDY output anyway. Timed polling in the
 * caller's loop is the supported pattern for both ADCs.
 */

#include "ADS1263_Driver.h"

// ── Log stream selection ──────────────────────────────────────────────
// On M4 the hardware UART (`Serial`) isn't initialized and writing to
// it hangs. Route driver logs over RPC so they reach the M7 bridge.
#if defined(CORE_CM4)
  #include "RPC.h"
  #define DRV_LOG RPC
#else
  #define DRV_LOG Serial
#endif

ADS1263_Driver::ADS1263_Driver()
    : _spi(500000, MSBFIRST, SPI_MODE1),
      // ADC1 defaults (inactive until configured)
      _adc1_rate(ADS1263_20SPS),
      _adc1_refmux(ADS1263_REFMUX_INTERNAL_2V5),
      _adc1_inpmux(0xAA),                // AINCOM / AINCOM = parked
      _adc1_vref_V(2.5f),
      _adc1_pga_bypass(true),
      _adc1_running(false),
      // ADC2 defaults (inactive until configured)
      _adc2_rate(ADS1263_ADC2_100SPS),
      _adc2_gain(ADS1263_ADC2_GAIN_1),
      _adc2_ref2(ADS1263_ADC2_REF_INTERNAL_2V5),
      _adc2_mux(0xAA),                   // AINCOM / AINCOM = parked
      _adc2_vref_V(2.5f),
      _adc2_running(false)
{
}

// ══════════════════════════════════════════════════════════════════════
//  Chip-level init
// ══════════════════════════════════════════════════════════════════════

bool ADS1263_Driver::begin() {
    pinMode(ADS1263_CS_PIN, OUTPUT);
    pinMode(ADS1263_RESET_PIN, OUTPUT);
    // DRDY pin is not read by this driver, but park it so it doesn't float.
    pinMode(ADS1263_DRDY_PIN, INPUT_PULLUP);

    digitalWrite(ADS1263_CS_PIN, HIGH);
    digitalWrite(ADS1263_RESET_PIN, HIGH);

    SPI.begin();
    delay(10);

    reset();
    delay(100);

    // Read ID twice with a gap — if the first read catches the chip
    // mid-startup we give it one more chance before declaring failure.
    uint8_t id = getDeviceID();
    if ((id & 0xF0) != 0x20) {
        DRV_LOG.print(F("ADS1263 ID first read=0x")); DRV_LOG.print(id, HEX);
        DRV_LOG.println(F(" — retrying after 200 ms"));
        delay(200);
        id = getDeviceID();
    }
    if ((id & 0xF0) != 0x20) {
        DRV_LOG.print(F("ADS1263 not found. ID=0x"));
        DRV_LOG.println(id, HEX);
        // Diagnostic: dump every register the chip is willing to return
        // to us. If everything reads 0x00 the chip/SPI is unresponsive;
        // if other registers return their reset defaults (MODE2=0x04,
        // INTERFACE=0x05, MODE1=0x40, ADC2CFG=0x00, etc.) the bus IS
        // working and the ID read specifically is the problem.
        DRV_LOG.println(F("--- Post-fail register dump ---"));
        for (uint8_t i = 0; i <= 0x1C; i++) {
            uint8_t val = readRegister(i);
            DRV_LOG.print(F("  0x"));
            if (i < 0x10) DRV_LOG.print('0');
            DRV_LOG.print(i, HEX);
            DRV_LOG.print(F(": 0x"));
            if (val < 0x10) DRV_LOG.print('0');
            DRV_LOG.println(val, HEX);
        }
        DRV_LOG.println(F("-------------------------------"));
        return false;
    }
    DRV_LOG.print(F("ADS1263 found. ID=0x"));
    DRV_LOG.println(id, HEX);

    // POWER: internal reference on (harmless if we end up using AVDD/AVSS).
    writeRegister(ADS1263_REG_POWER, 0x11);
    delay(150);

    // INTERFACE: STATUS + CHK enabled for both ADC1 and ADC2 reads.
    writeRegister(ADS1263_REG_INTERFACE, 0x05);

    // Disable IDAC excitation on both pins.
    writeRegister(ADS1263_REG_IDACMUX, 0xFF);
    writeRegister(ADS1263_REG_IDACMAG, 0x00);

    // Park ADC1 on an inactive mux/ref. configureADC1() will reprogram
    // these if ADC1 is actually used.
    writeRegister(ADS1263_REG_MODE0,  0x00);
    writeRegister(ADS1263_REG_MODE1,  0x40);                       // Sinc3
    writeRegister(ADS1263_REG_MODE2,  0x80 | (ADS1263_20SPS & 0x0F));  // PGA bypass, 20 SPS
    writeRegister(ADS1263_REG_INPMUX, _adc1_inpmux);               // AINCOM/AINCOM
    writeRegister(ADS1263_REG_REFMUX, _adc1_refmux);               // internal 2.5 V
    writeRegister(ADS1263_REG_OFCAL0, 0x00);
    writeRegister(ADS1263_REG_OFCAL1, 0x00);
    writeRegister(ADS1263_REG_OFCAL2, 0x00);

    // Park ADC2 on an inactive mux/ref. configureADC2() will reprogram.
    writeADC2CFG();                                                 // internal 2.5 V ref
    writeRegister(ADS1263_REG_ADC2MUX,  _adc2_mux);                // AINCOM/AINCOM
    writeRegister(ADS1263_REG_ADC2OFC0, 0x00);
    writeRegister(ADS1263_REG_ADC2OFC1, 0x00);
    writeRegister(ADS1263_REG_ADC2OFC2, 0x00);

    DRV_LOG.println(F("ADS1263 ready (dual-ADC; both paths parked until configureADCx)"));
    return true;
}

void ADS1263_Driver::reset() {
    digitalWrite(ADS1263_RESET_PIN, LOW);
    delay(10);
    digitalWrite(ADS1263_RESET_PIN, HIGH);
    delay(10);

    sendCommand(ADS1263_CMD_RESET);
    delay(50);

    _adc1_running = false;
    _adc2_running = false;
}

uint8_t ADS1263_Driver::getDeviceID() {
    return readRegister(ADS1263_REG_ID);
}

bool ADS1263_Driver::isConnected() {
    return (getDeviceID() & 0xF0) == 0x20;
}

// ══════════════════════════════════════════════════════════════════════
//  ADC1 API
// ══════════════════════════════════════════════════════════════════════

void ADS1263_Driver::configureADC1(uint8_t inpmux,
                                   uint8_t refmux,
                                   float vref_V,
                                   ADS1263_ADC1_Rate_t rate,
                                   bool pga_bypass) {
    bool was_running = _adc1_running;
    if (_adc1_running) stopADC1();

    _adc1_inpmux   = inpmux;
    _adc1_refmux   = refmux;
    _adc1_vref_V   = vref_V;
    _adc1_rate     = rate;
    _adc1_pga_bypass = pga_bypass;

    writeRegister(ADS1263_REG_INPMUX, _adc1_inpmux);
    writeRegister(ADS1263_REG_REFMUX, _adc1_refmux);
    writeMODE2();
    // MODE1 = Sinc3 filter, no simultaneous ADC2 (per-ADC setting — ADC2 is
    // controlled independently by ADC2CFG so this doesn't affect it).
    writeRegister(ADS1263_REG_MODE1, 0x40);
    writeRegister(ADS1263_REG_MODE0, 0x00);

    DRV_LOG.print(F("ADC1 configured: INPMUX=0x"));
    DRV_LOG.print(_adc1_inpmux, HEX);
    DRV_LOG.print(F(" REFMUX=0x"));
    DRV_LOG.print(_adc1_refmux, HEX);
    DRV_LOG.print(F(" VREF=")); DRV_LOG.print(_adc1_vref_V, 3); DRV_LOG.print(F(" V"));
    DRV_LOG.print(F(" rate=")); DRV_LOG.print(getADC1DataRate(), 0); DRV_LOG.println(F(" SPS"));

    if (was_running) startADC1();
}

void ADS1263_Driver::startADC1() {
    sendCommand(ADS1263_CMD_START1);
    _adc1_running = true;
    delay(10);
}

void ADS1263_Driver::stopADC1() {
    sendCommand(ADS1263_CMD_STOP1);
    _adc1_running = false;
}

ADC_Reading ADS1263_Driver::readADC1Direct() {
    ADC_Reading r = {};
    int32_t code = readRawData32();
    r.valid = true;
    r.raw_code = code;
    r.voltage_V = codeToVoltageADC1(code);
    r.voltage_uV = r.voltage_V * 1e6f;
    r.timestamp_us = micros();
    return r;
}

ADC_Reading ADS1263_Driver::readADC1Poll(uint32_t poll_ms) {
    ADC_Reading r = {};
    bool was_running = _adc1_running;
    if (_adc1_running) stopADC1();

    sendCommand(ADS1263_CMD_START1);
    delay(poll_ms);

    int32_t code = readRawData32();
    r.valid = true;
    r.raw_code = code;
    r.voltage_V = codeToVoltageADC1(code);
    r.voltage_uV = r.voltage_V * 1e6f;
    r.timestamp_us = micros();

    if (was_running) startADC1();
    return r;
}

bool ADS1263_Driver::adc1DataReadyPin() const {
    return digitalRead(ADS1263_DRDY_PIN) == LOW;
}

float ADS1263_Driver::getADC1DataRate() const {
    return rateToSPS_ADC1(_adc1_rate);
}

// ══════════════════════════════════════════════════════════════════════
//  ADC2 API
// ══════════════════════════════════════════════════════════════════════

void ADS1263_Driver::configureADC2(uint8_t adc2mux,
                                   uint8_t ref2,
                                   float vref_V,
                                   ADS1263_ADC2_Rate_t rate,
                                   ADS1263_ADC2_Gain_t gain) {
    bool was_running = _adc2_running;
    if (_adc2_running) stopADC2();

    _adc2_mux    = adc2mux;
    _adc2_ref2   = ref2 & 0x07;
    _adc2_vref_V = vref_V;
    _adc2_rate   = rate;
    _adc2_gain   = gain;

    writeADC2CFG();
    writeRegister(ADS1263_REG_ADC2MUX, _adc2_mux);

    DRV_LOG.print(F("ADC2 configured: ADC2MUX=0x"));
    DRV_LOG.print(_adc2_mux, HEX);
    DRV_LOG.print(F(" REF2=0x")); DRV_LOG.print(_adc2_ref2, HEX);
    DRV_LOG.print(F(" VREF=")); DRV_LOG.print(_adc2_vref_V, 3); DRV_LOG.print(F(" V"));
    DRV_LOG.print(F(" rate=")); DRV_LOG.print(getADC2DataRate(), 0); DRV_LOG.print(F(" SPS"));
    DRV_LOG.print(F(" gain=")); DRV_LOG.print(getADC2GainMultiplier()); DRV_LOG.println(F("x"));

    if (was_running) startADC2();
}

void ADS1263_Driver::startADC2() {
    sendCommand(ADS1263_CMD_START2);
    _adc2_running = true;
    delay(10);
}

void ADS1263_Driver::stopADC2() {
    sendCommand(ADS1263_CMD_STOP2);
    _adc2_running = false;
}

ADC_Reading ADS1263_Driver::readADC2Direct() {
    ADC_Reading r = {};
    int32_t code = readRawData24();
    r.valid = true;
    r.raw_code = code;
    r.voltage_V = codeToVoltageADC2(code);
    r.voltage_uV = r.voltage_V * 1e6f;
    r.timestamp_us = micros();
    return r;
}

ADC_Reading ADS1263_Driver::readADC2Poll(uint32_t poll_ms) {
    ADC_Reading r = {};
    bool was_running = _adc2_running;
    if (_adc2_running) stopADC2();

    sendCommand(ADS1263_CMD_START2);
    delay(poll_ms);

    int32_t code = readRawData24();
    r.valid = true;
    r.raw_code = code;
    r.voltage_V = codeToVoltageADC2(code);
    r.voltage_uV = r.voltage_V * 1e6f;
    r.timestamp_us = micros();

    if (was_running) startADC2();
    return r;
}

float ADS1263_Driver::getADC2DataRate() const {
    return rateToSPS_ADC2(_adc2_rate);
}

uint8_t ADS1263_Driver::getADC2GainMultiplier() const {
    return (uint8_t)(1u << (uint8_t)_adc2_gain);
}

// ══════════════════════════════════════════════════════════════════════
//  Diagnostics
// ══════════════════════════════════════════════════════════════════════

void ADS1263_Driver::printConfig() {
    DRV_LOG.println(F("--- ADS1263 Config (dual-ADC) ---"));
    DRV_LOG.print(F("ID            : 0x")); DRV_LOG.println(getDeviceID(), HEX);
    DRV_LOG.println(F("[ADC1]"));
    DRV_LOG.print(F("  INPMUX      : 0x")); DRV_LOG.println(_adc1_inpmux, HEX);
    DRV_LOG.print(F("  REFMUX      : 0x")); DRV_LOG.println(_adc1_refmux, HEX);
    DRV_LOG.print(F("  VREF        : ")); DRV_LOG.print(_adc1_vref_V, 3); DRV_LOG.println(F(" V"));
    DRV_LOG.print(F("  Rate        : ")); DRV_LOG.print(getADC1DataRate(), 0); DRV_LOG.println(F(" SPS"));
    DRV_LOG.print(F("  PGA         : ")); DRV_LOG.println(_adc1_pga_bypass ? F("bypass (gain=1)") : F("in path"));
    DRV_LOG.print(F("  Running     : ")); DRV_LOG.println(_adc1_running ? F("YES") : F("no"));
    DRV_LOG.println(F("[ADC2]"));
    DRV_LOG.print(F("  ADC2MUX     : 0x")); DRV_LOG.println(_adc2_mux, HEX);
    DRV_LOG.print(F("  REF2        : 0x")); DRV_LOG.println(_adc2_ref2, HEX);
    DRV_LOG.print(F("  VREF        : ")); DRV_LOG.print(_adc2_vref_V, 3); DRV_LOG.println(F(" V"));
    DRV_LOG.print(F("  Rate        : ")); DRV_LOG.print(getADC2DataRate(), 0); DRV_LOG.println(F(" SPS"));
    DRV_LOG.print(F("  Gain        : ")); DRV_LOG.print(getADC2GainMultiplier()); DRV_LOG.println(F("x"));
    DRV_LOG.print(F("  Running     : ")); DRV_LOG.println(_adc2_running ? F("YES") : F("no"));
    DRV_LOG.print(F("  ADC2CFG rb  : 0x")); DRV_LOG.println(readRegister(ADS1263_REG_ADC2CFG), HEX);
    DRV_LOG.println(F("Frame INTERFACE=0x05 → RDATA1=6B, RDATA2=5B"));
    DRV_LOG.println(F("---------------------------------"));
}

void ADS1263_Driver::printRegisters() {
    DRV_LOG.println(F("--- Register Dump ---"));
    for (uint8_t i = 0; i <= 0x1C; i++) {
        uint8_t val = readRegister(i);
        DRV_LOG.print(F("0x"));
        if (i < 0x10) DRV_LOG.print('0');
        DRV_LOG.print(i, HEX);
        DRV_LOG.print(F(": 0x"));
        if (val < 0x10) DRV_LOG.print('0');
        DRV_LOG.println(val, HEX);
    }
    DRV_LOG.println(F("---------------------"));
}

// ══════════════════════════════════════════════════════════════════════
//  Private — register packing helpers
// ══════════════════════════════════════════════════════════════════════

void ADS1263_Driver::writeMODE2() {
    // MODE2[7] = PGA bypass (1 = bypassed, gain forced to 1)
    // MODE2[6:4] = GAIN (ignored when bypassed)
    // MODE2[3:0] = DR (ADC1 data rate)
    uint8_t v = (_adc1_pga_bypass ? 0x80 : 0x00)
              | ((uint8_t)_adc1_rate & 0x0F);
    writeRegister(ADS1263_REG_MODE2, v);
}

void ADS1263_Driver::writeADC2CFG() {
    // ADC2CFG[7:6] = DR2
    // ADC2CFG[5:3] = GAIN2
    // ADC2CFG[2:0] = REF2
    uint8_t v = (((uint8_t)_adc2_rate & 0x03) << 6)
              | (((uint8_t)_adc2_gain & 0x07) << 3)
              | ( _adc2_ref2 & 0x07);
    writeRegister(ADS1263_REG_ADC2CFG, v);
}

// ══════════════════════════════════════════════════════════════════════
//  Private — low-level SPI
// ══════════════════════════════════════════════════════════════════════

void ADS1263_Driver::writeRegister(uint8_t reg, uint8_t value) {
    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(10);

    SPI.transfer(ADS1263_CMD_WREG | reg);
    SPI.transfer(0x00);                 // write 1 byte
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
    SPI.transfer(0x00);                 // read 1 byte
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

// ══════════════════════════════════════════════════════════════════════
//  Private — RDATA1 (ADC1, 32-bit)
// ══════════════════════════════════════════════════════════════════════

int32_t ADS1263_Driver::readRawData32() {
    // CMD → STATUS → D3(MSB) → D2 → D1 → D0(LSB) → CHK
    uint8_t status, chk;
    uint8_t d3, d2, d1, d0;

    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(5);

    SPI.transfer(ADS1263_CMD_RDATA1);
    status = SPI.transfer(0xFF);
    d3 = SPI.transfer(0xFF);
    d2 = SPI.transfer(0xFF);
    d1 = SPI.transfer(0xFF);
    d0 = SPI.transfer(0xFF);
    chk = SPI.transfer(0xFF);
    (void)status; (void)chk;

    delayMicroseconds(5);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();

    uint32_t raw = ((uint32_t)d3 << 24)
                 | ((uint32_t)d2 << 16)
                 | ((uint32_t)d1 <<  8)
                 |  (uint32_t)d0;
    return (int32_t)raw;
}

// ══════════════════════════════════════════════════════════════════════
//  Private — RDATA2 (ADC2, 24-bit sign-extended)
// ══════════════════════════════════════════════════════════════════════

int32_t ADS1263_Driver::readRawData24() {
    // CMD → STATUS → D3(MSB) → D2 → D1(LSB) → CHK
    uint8_t status, chk;
    uint8_t d3, d2, d1;

    SPI.beginTransaction(_spi);
    digitalWrite(ADS1263_CS_PIN, LOW);
    delayMicroseconds(5);

    SPI.transfer(ADS1263_CMD_RDATA2);
    status = SPI.transfer(0xFF);
    d3 = SPI.transfer(0xFF);
    d2 = SPI.transfer(0xFF);
    d1 = SPI.transfer(0xFF);
    chk = SPI.transfer(0xFF);
    (void)status; (void)chk;

    delayMicroseconds(5);
    digitalWrite(ADS1263_CS_PIN, HIGH);
    SPI.endTransaction();

    // Pack into bits 31:8 of a uint32 and arithmetic-shift right by 8
    // on the signed int — this sign-extends the 24-bit value cleanly.
    uint32_t raw_u =  ((uint32_t)d3 << 24)
                    | ((uint32_t)d2 << 16)
                    | ((uint32_t)d1 <<  8);
    return ((int32_t)raw_u) >> 8;
}

// ══════════════════════════════════════════════════════════════════════
//  Private — code → voltage
// ══════════════════════════════════════════════════════════════════════

float ADS1263_Driver::codeToVoltageADC1(int32_t code) const {
    // ADC1 is 32-bit signed. V = code / 2^31 * VREF (PGA bypassed → gain=1).
    return ((float)code / 2147483648.0f) * _adc1_vref_V;
}

float ADS1263_Driver::codeToVoltageADC2(int32_t code) const {
    // ADC2 is 24-bit signed. V = code / 2^23 * VREF / gain.
    const float gain = (float)getADC2GainMultiplier();
    return ((float)code / 8388608.0f) * (_adc2_vref_V / gain);
}

// ══════════════════════════════════════════════════════════════════════
//  Private — misc
// ══════════════════════════════════════════════════════════════════════

bool ADS1263_Driver::waitForDRDY(uint32_t timeout_ms) {
    uint32_t start = millis();
    while (digitalRead(ADS1263_DRDY_PIN) != LOW) {
        if (millis() - start > timeout_ms) return false;
        delayMicroseconds(100);
    }
    return true;
}

float ADS1263_Driver::rateToSPS_ADC1(ADS1263_ADC1_Rate_t rate) const {
    const float table[] = {
        2.5f, 5.0f, 10.0f, 16.6f, 20.0f, 50.0f, 60.0f, 100.0f,
        400.0f, 1200.0f, 2400.0f, 4800.0f, 7200.0f, 14400.0f, 19200.0f, 38400.0f
    };
    uint8_t i = (uint8_t)rate;
    return (i <= 0x0F) ? table[i] : 20.0f;
}

float ADS1263_Driver::rateToSPS_ADC2(ADS1263_ADC2_Rate_t rate) const {
    switch (rate) {
        case ADS1263_ADC2_10SPS:   return 10.0f;
        case ADS1263_ADC2_100SPS:  return 100.0f;
        case ADS1263_ADC2_400SPS:  return 400.0f;
        case ADS1263_ADC2_800SPS:  return 800.0f;
    }
    return 100.0f;
}
