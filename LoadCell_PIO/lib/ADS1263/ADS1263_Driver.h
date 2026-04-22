/**
 * @file ADS1263_Driver.h
 * @brief ADS1263 32-bit ADC driver — Portenta H7 (M4 core), EXTERNAL 5V reference
 *
 * Signal chain:
 *   Load cell (bridge) → LCA-9PC/LCA-RTC amplifier → ADS1263 AIN0 / AIN1
 *
 * LCA amplifier is configured for ±5V bipolar output; we only use the
 * compression half (0 to +5V). The full-scale LCA output (+5V) matches
 * the ADC reference (5V), so compression uses 0…+FS (positive codes only).
 *
 * ADS1263 configuration:
 *   - PGA BYPASSED (gain=1). All signal amplification is done by the LCA.
 *   - REFERENCE = EXTERNAL 5V via AVDD/AVSS (REFMUX = 0x24).
 *     AVDD must be a clean 5V rail.
 *   - Differential input: AIN0(+) vs AIN1(-)
 *       AIN0 = LCA Vo (pin 3 of J2)
 *       AIN1 = LCA GND (pin 2 / pin 4 of J2)
 *   - STATUS + CRC enabled on SPI (INTERFACE = 0x05) — robust 6-byte read.
 *
 * Scaling:  V_in = (code / 2^31) * VREF_EXTERNAL       (VREF = 5.0 V)
 *
 * Pinout — Portenta H7 Elite + Hat Carrier (J5 Pi-compatible header).
 * Waveshare ADS1263 HAT plugs directly onto J5. DRDY uses INPUT_PULLUP,
 * no jumper wire required. The M4 core reads the SAME pads/peripherals
 * as the M7 — this works because the STM32H747 SPI peripheral and GPIO
 * pads are shared across both cores.
 *
 *   CS    = PE_6   (J2-53, Pi pin 15)
 *   DRDY  = PJ_11  (J2-50, Pi pin 11) — INPUT_PULLUP required
 *   RESET = PI_5   (J1-56, Pi pin 12)
 */

#ifndef ADS1263_DRIVER_H
#define ADS1263_DRIVER_H

#include <Arduino.h>
#include <SPI.h>

// ── Pin definitions — Portenta H7 + Hat Carrier ──────────────────────────
#define ADS1263_CS_PIN    PE_6   // J2-53, Pi pin 15
#define ADS1263_DRDY_PIN  PJ_11  // J2-50, Pi pin 11 — INPUT_PULLUP
#define ADS1263_RESET_PIN PI_5   // J1-56, Pi pin 12

// ── External reference value (volts) ──────────────────────────────────────
#define ADS1263_VREF_V    5.0f

// ── REFMUX options (per ADS1263 datasheet 7.6.1.15) ───────────────────────
// Layout: RMUXP = bits [5:3], RMUXN = bits [2:0]
//   000 = internal 2.5V        001 = AIN0/AIN1
//   010 = AIN2/AIN3            011 = AIN4/AIN5
//   100 = VAVDD / VAVSS
#define ADS1263_REFMUX_INTERNAL_2V5  0x00   // internal 2.5V ref (both P and N)
#define ADS1263_REFMUX_EXT_AIN01     0x09   // external ref on AIN0(+)/AIN1(-)
#define ADS1263_REFMUX_EXT_AIN23     0x12   // external ref on AIN2(+)/AIN3(-)
#define ADS1263_REFMUX_EXT_AIN45     0x1B   // external ref on AIN4(+)/AIN5(-)
#define ADS1263_REFMUX_AVDD_AVSS     0x24   // VAVDD(+) / VAVSS(-) — 5V external

// ── ADS1263 Commands ──────────────────────────────────────────────────────
#define ADS1263_CMD_NOP     0x00
#define ADS1263_CMD_RESET   0x06
#define ADS1263_CMD_START1  0x08
#define ADS1263_CMD_STOP1   0x0A
#define ADS1263_CMD_RDATA1  0x12
#define ADS1263_CMD_SYOCAL1 0x16
#define ADS1263_CMD_SYGCAL1 0x17
#define ADS1263_CMD_SFOCAL1 0x19
#define ADS1263_CMD_RREG    0x20
#define ADS1263_CMD_WREG    0x40

// ── ADS1263 Registers ─────────────────────────────────────────────────────
#define ADS1263_REG_ID        0x00
#define ADS1263_REG_POWER     0x01
#define ADS1263_REG_INTERFACE 0x02
#define ADS1263_REG_MODE0     0x03
#define ADS1263_REG_MODE1     0x04
#define ADS1263_REG_MODE2     0x05
#define ADS1263_REG_INPMUX    0x06
#define ADS1263_REG_OFCAL0    0x07
#define ADS1263_REG_OFCAL1    0x08
#define ADS1263_REG_OFCAL2    0x09
#define ADS1263_REG_FSCAL0    0x0A
#define ADS1263_REG_FSCAL1    0x0B
#define ADS1263_REG_FSCAL2    0x0C
#define ADS1263_REG_IDACMUX   0x0D
#define ADS1263_REG_IDACMAG   0x0E
#define ADS1263_REG_REFMUX    0x0F
#define ADS1263_REG_TDACP     0x10
#define ADS1263_REG_TDACN     0x11
#define ADS1263_REG_GPIOCON   0x12
#define ADS1263_REG_GPIODIR   0x13
#define ADS1263_REG_GPIODAT   0x14

typedef enum {
    ADS1263_2_5SPS   = 0x00,
    ADS1263_5SPS     = 0x01,
    ADS1263_10SPS    = 0x02,
    ADS1263_16_6SPS  = 0x03,
    ADS1263_20SPS    = 0x04,
    ADS1263_50SPS    = 0x05,
    ADS1263_60SPS    = 0x06,
    ADS1263_100SPS   = 0x07,
    ADS1263_400SPS   = 0x08,
    ADS1263_1200SPS  = 0x09,
    ADS1263_2400SPS  = 0x0A,
    ADS1263_4800SPS  = 0x0B,
    ADS1263_7200SPS  = 0x0C,
    ADS1263_14400SPS = 0x0D,
    ADS1263_19200SPS = 0x0E,
    ADS1263_38400SPS = 0x0F
} ADS1263_DataRate_t;

typedef struct {
    bool valid;
    int32_t raw_code;
    float voltage_V;
    float voltage_uV;
    uint32_t timestamp_us;
} ADC_Reading;

class ADS1263_Driver {
public:
    ADS1263_Driver();

    bool begin(ADS1263_DataRate_t rate = ADS1263_20SPS);

    void reset();

    void setDataRate(ADS1263_DataRate_t rate);
    float getCurrentDataRate() const;

    void setRefMux(uint8_t refmux, float vref_V);

    void startContinuous();
    void stopContinuous();

    ADC_Reading readSingle();
    ADC_Reading readPoll(uint32_t poll_ms = 5);
    ADC_Reading readContinuous();
    ADC_Reading readDirect();

    bool dataReady() const;

    bool calibrate();

    uint8_t getDeviceID();
    bool isConnected();

    float getVrefV() const { return _vref_V; }

    void printConfig();
    void printRegisters();

private:
    SPISettings _spi;
    ADS1263_DataRate_t _rate;
    bool _continuous;
    uint8_t _refmux;
    float _vref_V;

    void writeRegister(uint8_t reg, uint8_t value);
    uint8_t readRegister(uint8_t reg);
    void sendCommand(uint8_t cmd);
    int32_t readRawData();
    float codeToVoltage(int32_t code) const;
    bool waitForDataReady(uint32_t timeout_ms = 500);
    float rateToSPS(ADS1263_DataRate_t rate) const;
};

#endif // ADS1263_DRIVER_H
