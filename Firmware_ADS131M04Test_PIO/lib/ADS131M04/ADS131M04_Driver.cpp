/**
 * @file ADS131M04_Driver.cpp
 * @brief ADS131M04 driver implementation. See the header for the protocol notes
 *        and for how this chip differs from the ADS1263.
 */

#include "ADS131M04_Driver.h"

// Set to 1 to trace every frame on Serial. Very noisy — bring-up only.
#ifndef ADS131M04_DEBUG
#define ADS131M04_DEBUG 0
#endif

#if ADS131M04_DEBUG
  #define DRV_LOG(...)  do { Serial.print("[M04] "); Serial.println(__VA_ARGS__); } while (0)
#else
  #define DRV_LOG(...)  do {} while (0)
#endif

// SPI mode 1 (CPOL=0, CPHA=1): SCLK idles low, DOUT is launched on the rising
// edge and DIN is latched on the falling edge (§8.5.1, §6.6).
static const uint8_t ADS131M04_SPI_MODE = SPI_MODE1;

// OSR code -> decimation factor. Index is ADS131M04_OSR_t.
// Code 7 is 16256 per the CLOCK register table (8-17); the data-rate table
// (8-2) says 16384 for the same code. See the header note — measure it.
static const uint16_t kOsrDiv[8] = {
    128, 256, 512, 1024, 2048, 4096, 8192, 16256
};

// Power mode -> nominal f_CLKIN. The register must agree with the EVM's JP6
// jumper; if it does not, every derived rate is wrong by the ratio.
static float clkinForMode(ADS131M04_PWR_t pwr) {
    switch (pwr) {
        case ADS131M04_PWR_VLP: return 2048000.0f;
        case ADS131M04_PWR_LP:  return 4096000.0f;
        default:                return ADS131M04_FCLKIN_HZ;   // HR
    }
}

ADS131M04_Driver::ADS131M04_Driver()
    : _spi(2000000, MSBFIRST, ADS131M04_SPI_MODE),
      _spi_hz(2000000),
      _present(false),
      _osr(ADS131M04_OSR_1024),        // chip reset defaults
      _pwr(ADS131M04_PWR_HR),
      _ch_mask(0x0F),
      _crc_err(0),
      _frames(0)
{
    for (uint8_t i = 0; i < ADS131M04_NUM_CH; i++) {
        _gain[i] = ADS131M04_GAIN_1;
        _mux[i]  = ADS131M04_MUX_AIN;
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  CRC-16/CCITT-FALSE — poly 0x1021, seed 0xFFFF, MSB-first, no final XOR.
//  §8.3.12 / Table 8-7. Coverage is EVERY word preceding the CRC word,
//  including the zero-padding inside each 24-bit word — so this runs over
//  whole bytes, not over the 16 significant bits.
// ══════════════════════════════════════════════════════════════════════════
uint16_t ADS131M04_Driver::crc16(const uint8_t *data, size_t len) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
                                 : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

// 24-bit two's complement -> int32.
int32_t ADS131M04_Driver::sext24(uint32_t v) {
    return (v & 0x00800000UL) ? (int32_t)(v | 0xFF000000UL) : (int32_t)v;
}

void ADS131M04_Driver::setSpiHz(uint32_t hz) {
    if (hz > ADS131M04_SCLK_MAX_HZ) hz = ADS131M04_SCLK_MAX_HZ;
    _spi_hz = hz;
    _spi    = SPISettings(hz, MSBFIRST, ADS131M04_SPI_MODE);
}

// ══════════════════════════════════════════════════════════════════════════
//  Frame transfer — the one primitive everything else is built on.
//
//  DIN :  [ cmd ][ wdata[0] ][ wdata[1] ] ... zero-filled to 6 words
//  DOUT:  [ response ][ ch0 ][ ch1 ][ ch2 ][ ch3 ][ CRC ]
//
//  `response` answers the command sent in the PREVIOUS frame, never this one.
// ══════════════════════════════════════════════════════════════════════════
bool ADS131M04_Driver::transferFrame(uint16_t cmd,
                                     const uint16_t *wdata, uint8_t nwdata,
                                     ADS131M04_Frame &out) {
    uint8_t tx[ADS131M04_FRAME_BYTES];
    uint8_t rx[ADS131M04_FRAME_BYTES];

    memset(tx, 0, sizeof(tx));

    // Words are MSB-aligned; the low byte of a 24-bit word is zero padding for
    // commands, register data and CRC alike (§8.5.1.8).
    tx[0] = (uint8_t)(cmd >> 8);
    tx[1] = (uint8_t)(cmd & 0xFF);

    // WREG payload goes IMMEDIATELY after the command word (§8.5.1.10.8) —
    // it occupies the slot that would hold the input CRC if that were enabled,
    // because the input CRC goes AFTER the data, not before it.
    if (wdata && nwdata > 0) {
        if (nwdata > ADS131M04_FRAME_WORDS - 1) nwdata = ADS131M04_FRAME_WORDS - 1;
        for (uint8_t i = 0; i < nwdata; i++) {
            const size_t o = (size_t)(i + 1) * ADS131M04_WORD_BYTES;
            tx[o]     = (uint8_t)(wdata[i] >> 8);
            tx[o + 1] = (uint8_t)(wdata[i] & 0xFF);
        }
    }

    SPI.beginTransaction(_spi);
    digitalWrite(ADS131M04_CS_PIN, LOW);
    for (size_t i = 0; i < ADS131M04_FRAME_BYTES; i++) rx[i] = SPI.transfer(tx[i]);
    digitalWrite(ADS131M04_CS_PIN, HIGH);
    SPI.endTransaction();

    out.response = (uint16_t)((uint16_t)rx[0] << 8 | rx[1]);
    for (uint8_t c = 0; c < ADS131M04_NUM_CH; c++) {
        const size_t o = (size_t)(c + 1) * ADS131M04_WORD_BYTES;
        const uint32_t u = ((uint32_t)rx[o] << 16) |
                           ((uint32_t)rx[o + 1] << 8) |
                            (uint32_t)rx[o + 2];
        out.data[c] = sext24(u);
    }

    const size_t crc_off = (size_t)(ADS131M04_FRAME_WORDS - 1) * ADS131M04_WORD_BYTES;
    out.crc_rx   = (uint16_t)((uint16_t)rx[crc_off] << 8 | rx[crc_off + 1]);
    out.crc_calc = crc16(rx, crc_off);       // all five preceding words
    out.crc_ok   = (out.crc_rx == out.crc_calc);

    _frames++;
    if (!out.crc_ok) _crc_err++;
    return out.crc_ok;
}

// ══════════════════════════════════════════════════════════════════════════
//  Register access
//
//  Both of these run TWO frames, because the response to a command appears in
//  the frame after it. That has a useful side effect: because the second frame
//  always carries NULL, the NEXT frame a caller runs gets STATUS in word 0 —
//  never a stale register value or a stale write ack.
// ══════════════════════════════════════════════════════════════════════════
uint16_t ADS131M04_Driver::readRegister(uint8_t addr) {
    ADS131M04_Frame f;
    const uint16_t cmd = ADS131M04_CMD_RREG | ((uint16_t)(addr & 0x3F) << 7);

    transferFrame(cmd, nullptr, 0, f);                 // issue RREG
    transferFrame(ADS131M04_CMD_NULL, nullptr, 0, f);  // collect the answer
    return f.response;
}

bool ADS131M04_Driver::writeRegister(uint8_t addr, uint16_t value) {
    ADS131M04_Frame f;
    const uint16_t cmd = ADS131M04_CMD_WREG | ((uint16_t)(addr & 0x3F) << 7);

    transferFrame(cmd, &value, 1, f);                  // command + payload
    transferFrame(ADS131M04_CMD_NULL, nullptr, 0, f);  // collect the ack

    // Ack is 010a aaaa ammm mmmm, where mmm mmmm is the count ACTUALLY written
    // minus one — which the datasheet warns can be less than what was asked
    // (§8.5.1.10.8). For a single-register write anything but 0 means the chip
    // did not do what we told it.
    const uint16_t expect = ADS131M04_RSP_WREG | ((uint16_t)(addr & 0x3F) << 7);
    if (f.response != expect) {
        DRV_LOG(String("WREG ack mismatch: got 0x") + String(f.response, HEX) +
                " want 0x" + String(expect, HEX));
        return false;
    }
    return true;
}

// ══════════════════════════════════════════════════════════════════════════
//  Bring-up
// ══════════════════════════════════════════════════════════════════════════
bool ADS131M04_Driver::begin(uint32_t spi_hz) {
    pinMode(ADS131M04_CS_PIN, OUTPUT);
    digitalWrite(ADS131M04_CS_PIN, HIGH);

    pinMode(ADS131M04_RESET_PIN, OUTPUT);
    digitalWrite(ADS131M04_RESET_PIN, HIGH);

    // Input, not INPUT_PULLUP: DRDY is push-pull driven by the ADC. Parking it
    // as a plain input keeps the pad from floating without fighting the driver.
    pinMode(ADS131M04_DRDY_PIN, INPUT);

    setSpiHz(spi_hz);
    SPI.begin();

    // A cold board may still be inside t_POR; the device ignores all SPI before
    // its first DRDY rising edge (§8.4.1).
    delayMicroseconds(ADS131M04_TPOR_US * 2);

    return reset();
}

bool ADS131M04_Driver::reset() {
    _present = false;

    // >= t_w(RSL) = 2048 t_CLKIN = 250 us at 8.192 MHz. Anything shorter is a
    // SYNC, not a reset — see header note 7. 1 ms leaves no room for doubt.
    digitalWrite(ADS131M04_RESET_PIN, LOW);
    delayMicroseconds(ADS131M04_RESET_LOW_US);
    digitalWrite(ADS131M04_RESET_PIN, HIGH);

    // "The host must wait for at least t_REGACQ after SYNC/RESET is brought
    // high or for the DRDY rising edge before communicating" (§8.4.1.2).
    // t_REGACQ is 5 us; 1 ms is free here and covers a slow supply too.
    delay(1);

    // Registers are back at defaults now, so mirror that in our shadow state
    // rather than letting a stale cached config lie to sps()/lsbVolts().
    _osr     = ADS131M04_OSR_1024;
    _pwr     = ADS131M04_PWR_HR;
    _ch_mask = 0x0F;
    for (uint8_t i = 0; i < ADS131M04_NUM_CH; i++) {
        _gain[i] = ADS131M04_GAIN_1;
        _mux[i]  = ADS131M04_MUX_AIN;
    }

    const uint16_t id = readRegister(ADS131M04_REG_ID);
    _present = ((id & ADS131M04_ID_MASK) == ADS131M04_ID_EXPECTED);

    if (!_present) {
        DRV_LOG(String("bad ID 0x") + String(id, HEX) +
                " — check SPI wiring AND that CLKIN is running (EVM JP6/Y1)");
    }
    return _present;
}

bool ADS131M04_Driver::resetCommand() {
    ADS131M04_Frame f;

    // The command is not latched until the whole 6-word frame completes;
    // terminating early makes the chip ignore it (§8.4.1.3). transferFrame()
    // always runs a full frame, so that hazard is structurally excluded.
    transferFrame(ADS131M04_CMD_RESET, nullptr, 0, f);
    delayMicroseconds(ADS131M04_TREGACQ_US * 10);

    _osr     = ADS131M04_OSR_1024;
    _pwr     = ADS131M04_PWR_HR;
    _ch_mask = 0x0F;
    for (uint8_t i = 0; i < ADS131M04_NUM_CH; i++) {
        _gain[i] = ADS131M04_GAIN_1;
        _mux[i]  = ADS131M04_MUX_AIN;
    }

    const uint16_t id = readRegister(ADS131M04_REG_ID);
    _present = ((id & ADS131M04_ID_MASK) == ADS131M04_ID_EXPECTED);
    return _present;
}

// ══════════════════════════════════════════════════════════════════════════
//  Configuration
// ══════════════════════════════════════════════════════════════════════════
bool ADS131M04_Driver::configure(ADS131M04_OSR_t osr,
                                 ADS131M04_PWR_t pwr,
                                 uint8_t ch_mask) {
    // CLOCK (§8.6.4): [11:8] channel enables, [7:6] reserved (write 00b),
    // [5] TBM (0 = OSR comes from [4:2]), [4:2] OSR, [1:0] power mode.
    const uint16_t clock = (uint16_t)((ch_mask & 0x0F) << 8) |
                           (uint16_t)((osr & 0x07) << 2) |
                           (uint16_t)(pwr & 0x03);

    if (!writeRegister(ADS131M04_REG_CLOCK, clock)) return false;

    // Read back rather than trust the ack: a wrong OSR silently changes every
    // rate downstream, and it is cheap to catch here.
    const uint16_t rb = readRegister(ADS131M04_REG_CLOCK);
    if ((rb & 0x0F3F) != (clock & 0x0F3F)) {          // ignore reserved [15:12]
        DRV_LOG(String("CLOCK readback 0x") + String(rb, HEX) +
                " != 0x" + String(clock, HEX));
        return false;
    }

    _osr     = osr;
    _pwr     = pwr;
    _ch_mask = ch_mask & 0x0F;

    // A rate/mux change restarts the digital filter; sinc3 needs three
    // conversion cycles to settle (§8.5.2), so the first few frames after this
    // are not trustworthy. Wait them out here rather than making every caller
    // remember to.
    const float rate = sps();
    if (rate > 0.0f) delay((uint32_t)(3000.0f / rate) + 2);

    return true;
}

bool ADS131M04_Driver::setGain(uint8_t ch, ADS131M04_Gain_t gain) {
    if (ch >= ADS131M04_NUM_CH) return false;

    // GAIN1 (§8.6.5): 3-bit fields at bits 14:12 (ch3), 10:8 (ch2), 6:4 (ch1),
    // 2:0 (ch0) — i.e. 4 bits of stride with the top bit of each nibble
    // reserved. Read-modify-write so the other three channels survive.
    uint16_t reg = readRegister(ADS131M04_REG_GAIN1);
    const uint8_t shift = (uint8_t)(4 * ch);
    reg = (uint16_t)((reg & ~(uint16_t)(0x7u << shift)) |
                     (uint16_t)((gain & 0x07u) << shift));

    if (!writeRegister(ADS131M04_REG_GAIN1, reg)) return false;

    const uint16_t rb = readRegister(ADS131M04_REG_GAIN1);
    if (((rb >> shift) & 0x07u) != (uint16_t)(gain & 0x07u)) {
        DRV_LOG(String("GAIN1 readback 0x") + String(rb, HEX));
        return false;
    }

    _gain[ch] = (uint8_t)gain;

    const float rate = sps();
    if (rate > 0.0f) delay((uint32_t)(3000.0f / rate) + 2);   // filter re-settle
    return true;
}

ADS131M04_Gain_t ADS131M04_Driver::getGain(uint8_t ch) const {
    if (ch >= ADS131M04_NUM_CH) return ADS131M04_GAIN_1;
    return (ADS131M04_Gain_t)_gain[ch];
}

bool ADS131M04_Driver::setInputMux(uint8_t ch, ADS131M04_Mux_t mux) {
    if (ch >= ADS131M04_NUM_CH) return false;

    // CHn_CFG (§8.6.10): MUXn[1:0] are bits 1:0; PHASEn and DCBLKn_DIS live
    // above them, so this is read-modify-write, not a blind store.
    const uint8_t addr = ADS131M04_CH_CFG(ch);
    uint16_t reg = readRegister(addr);
    reg = (uint16_t)((reg & ~0x0003u) | ((uint16_t)mux & 0x0003u));

    if (!writeRegister(addr, reg)) return false;
    if ((readRegister(addr) & 0x0003u) != ((uint16_t)mux & 0x0003u)) {
        DRV_LOG(String("CH") + ch + "_CFG mux readback mismatch");
        return false;
    }
    _mux[ch] = (uint8_t)mux;

    // Changing the mux resets the digital filter (§8.5.2) — sinc3 needs three
    // conversion cycles to settle, so the first frames after this are garbage.
    const float rate = sps();
    if (rate > 0.0f) delay((uint32_t)(3000.0f / rate) + 2);
    return true;
}

ADS131M04_Mux_t ADS131M04_Driver::getInputMux(uint8_t ch) const {
    if (ch >= ADS131M04_NUM_CH) return ADS131M04_MUX_AIN;
    return (ADS131M04_Mux_t)_mux[ch];
}

float ADS131M04_Driver::expectedVolts(uint8_t ch) const {
    if (ch >= ADS131M04_NUM_CH) return 0.0f;
    const float mag = fsrVolts(ch) * (float)ADS131M04_TEST_NUM / (float)ADS131M04_TEST_DEN;
    switch (_mux[ch]) {
        case ADS131M04_MUX_TEST_POS: return  mag;
        case ADS131M04_MUX_TEST_NEG: return -mag;
        default:                     return 0.0f;   // AIN and SHORTED alike
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  Sampling
// ══════════════════════════════════════════════════════════════════════════
bool ADS131M04_Driver::readChannels(ADS131M04_Reading &out) {
    ADS131M04_Frame f;
    const bool ok = transferFrame(ADS131M04_CMD_NULL, nullptr, 0, f);

    out.hw_us  = micros();
    out.valid  = ok;
    out.status = f.response;      // NULL's response is the STATUS register

    for (uint8_t c = 0; c < ADS131M04_NUM_CH; c++) {
        out.raw[c]   = f.data[c];
        out.volts[c] = (float)f.data[c] * lsbVolts(c);
    }
    return ok;
}

bool ADS131M04_Driver::waitDataReady(uint32_t timeout_ms) {
    // LEVEL poll, not an edge hunt — and that is only correct because MODE's
    // DRDY_FMT is left at its reset default of 0b, "logic low": DRDY asserts
    // when a conversion is available and STAYS low until the data are read.
    // So "already low" means "data is waiting", which is a success, not a
    // state to wait out.
    //
    // Setting DRDY_FMT = 1b switches the pin to a fixed-width low pulse of
    // t_w(DRL) = 4 t_CLKIN (~0.5 us at 8.192 MHz). Polling cannot see that
    // reliably from the Arduino layer — anything relying on pulse mode needs
    // an interrupt, not this function. The driver never sets that bit.
    const uint32_t t0 = millis();
    while (digitalRead(ADS131M04_DRDY_PIN) == HIGH) {
        if (millis() - t0 > timeout_ms) return false;
    }
    return true;
}

// ══════════════════════════════════════════════════════════════════════════
//  Derived numbers
// ══════════════════════════════════════════════════════════════════════════
uint16_t ADS131M04_Driver::osrDivisor() const {
    return kOsrDiv[_osr & 0x07];
}

float ADS131M04_Driver::sps() const {
    // f_MOD = f_CLKIN / 2 (§8.3.6); output rate = f_MOD / OSR (§8.3.7).
    return (clkinForMode(_pwr) / 2.0f) / (float)osrDivisor();
}

uint8_t ADS131M04_Driver::gainMultiplier(uint8_t ch) const {
    if (ch >= ADS131M04_NUM_CH) return 1;
    return (uint8_t)(1u << _gain[ch]);         // codes 0..7 -> 1..128
}

float ADS131M04_Driver::fsrVolts(uint8_t ch) const {
    return ADS131M04_VREF_V / (float)gainMultiplier(ch);
}

float ADS131M04_Driver::lsbVolts(uint8_t ch) const {
    // +FSR corresponds to 2^23 codes (§8.5.1.9, Equation 10).
    return fsrVolts(ch) / 8388608.0f;
}

// ══════════════════════════════════════════════════════════════════════════
//  Diagnostics
// ══════════════════════════════════════════════════════════════════════════
void ADS131M04_Driver::printConfig(Stream &s) {
    s.print(F("[M04] present="));  s.print(_present ? F("yes") : F("NO"));
    s.print(F(" id=0x"));          s.print(readRegister(ADS131M04_REG_ID), HEX);
    s.print(F(" spi="));           s.print(_spi_hz);
    s.print(F("Hz mode=SPI1 osr=")); s.print(osrDivisor());
    s.print(F(" pwr="));           s.print((int)_pwr);
    s.print(F(" rate="));          s.print(sps(), 2);
    s.print(F("SPS chmask=0x"));   s.print(_ch_mask, HEX);
    s.println();

    for (uint8_t c = 0; c < ADS131M04_NUM_CH; c++) {
        s.print(F("[M04]   ch")); s.print(c);
        s.print(F(" gain="));     s.print(gainMultiplier(c));
        s.print(F(" fsr=+/-"));   s.print(fsrVolts(c), 4);
        s.print(F("V lsb="));     s.print(lsbVolts(c) * 1e9f, 1);
        s.println(F("nV"));
    }

    const uint16_t st = readRegister(ADS131M04_REG_STATUS);
    s.print(F("[M04] status=0x")); s.print(st, HEX);
    if (st & ADS131M04_ST_LOCK)     s.print(F(" LOCK"));
    if (st & ADS131M04_ST_F_RESYNC) s.print(F(" F_RESYNC"));
    if (st & ADS131M04_ST_REG_MAP)  s.print(F(" REG_MAP_ERR"));
    if (st & ADS131M04_ST_CRC_ERR)  s.print(F(" CRC_ERR"));
    if (st & ADS131M04_ST_RESET)    s.print(F(" RESET"));
    s.print(F(" drdy=0x")); s.print(st & ADS131M04_ST_DRDY_MASK, HEX);
    s.print(F(" frames=")); s.print(_frames);
    s.print(F(" crc_err=")); s.println(_crc_err);
}

void ADS131M04_Driver::printRegisters(Stream &s) {
    static const uint8_t regs[] = {
        ADS131M04_REG_ID,      ADS131M04_REG_STATUS, ADS131M04_REG_MODE,
        ADS131M04_REG_CLOCK,   ADS131M04_REG_GAIN1,  ADS131M04_REG_CFG,
        ADS131M04_REG_CH0_CFG, ADS131M04_REG_CH1_CFG,
        ADS131M04_REG_CH2_CFG, ADS131M04_REG_CH3_CFG,
    };
    static const char *names[] = {
        "ID", "STATUS", "MODE", "CLOCK", "GAIN1", "CFG",
        "CH0_CFG", "CH1_CFG", "CH2_CFG", "CH3_CFG",
    };

    s.println(F("[M04] REGDUMP"));
    for (uint8_t i = 0; i < sizeof(regs); i++) {
        s.print(F("[M04]   "));  s.print(names[i]);
        s.print(F(" (0x"));      s.print(regs[i], HEX);
        s.print(F(") = 0x"));    s.println(readRegister(regs[i]), HEX);
    }
}
