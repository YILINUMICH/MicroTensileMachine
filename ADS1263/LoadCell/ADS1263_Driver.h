/**
 * @file ADS1263_Driver.h
 * @brief ADS1263 32-bit ADC driver — Arduino Uno, EXTERNAL 5V reference
 *
 * Signal chain (new):
 *   Load cell (bridge) → LCA-9PC/LCA-RTC amplifier → ADS1263 AIN0 / AIN1
 *
 * LCA amplifier is configured for ±5V bipolar output; we only use the
 * compression half (0 to +5V). The full-scale LCA output (+5V) matches
 * the ADC reference (5V), so compression uses 0…+FS (positive codes only).
 *
 * ADS1263 configuration:
 *   - PGA BYPASSED (gain=1). All signal amplification is done by the LCA.
 *   - REFERENCE = EXTERNAL 5V via AVDD/AVSS (REFMUX = 0x08).
 *     AVDD must be a clean 5V rail (the LCA's signal ground shares this).
 *     An optional precision external ref (e.g. REF5050 on AIN2/AIN3,
 *     REFMUX = 0x02) may be used for better long-term stability.
 *   - Differential input: AIN0(+) vs AIN1(-)
 *       AIN0 = LCA Vo (pin 3 of J2)
 *       AIN1 = LCA GND (pin 2 / pin 4 of J2)
 *   - STATUS + CRC enabled on SPI (INTERFACE = 0x05) — robust 6-byte read.
 *
 * Scaling:  V_in = (code / 2^31) * VREF_EXTERNAL       (VREF = 5.0 V)
 *
 * Pinout is Arduino Uno (or any AVR Uno clone):
 *   CS    = D10    DRDY  = D2 (INPUT_PULLUP)    RESET = D9
 *   SCK   = D13    MOSI  = D11                  MISO  = D12
 */

#ifndef ADS1263_DRIVER_H
#define ADS1263_DRIVER_H

#include <Arduino.h>
#include <SPI.h>

// ── Pin definitions — Arduino Uno ─────────────────────────────────────────
#define ADS1263_CS_PIN    10   // SPI CS (SS)
#define ADS1263_DRDY_PIN  2    // Data ready (interrupt-capable pin)
#define ADS1263_RESET_PIN 9    // Hardware reset

// ── External reference value (volts) ──────────────────────────────────────
// Matches the LCA amplifier's ±5V full-scale output configuration.
// If you switch to a precision external ref on AIN2/AIN3, adjust to taste.
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

// Data rate settings
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

// ADC reading result
typedef struct {
    bool valid;
    int32_t raw_code;        // Raw 32-bit signed ADC code
    float voltage_V;         // Voltage at ADC input (already scaled to VREF)
    float voltage_uV;        // Voltage in microvolts
    uint32_t timestamp_us;
} ADC_Reading;

class ADS1263_Driver {
public:
    ADS1263_Driver();

    // Initialize ADS1263 for single-channel, PGA-bypassed operation with
    // AVDD/AVSS (5V) as the reference by default.
    bool begin(ADS1263_DataRate_t rate = ADS1263_20SPS);

    void reset();

    // Set data rate
    void setDataRate(ADS1263_DataRate_t rate);
    float getCurrentDataRate() const;

    // Change the voltage reference source at runtime.
    // Use one of the ADS1263_REFMUX_* constants.
    // Pass the corresponding VREF in volts so codeToVoltage() stays accurate.
    void setRefMux(uint8_t refmux, float vref_V);

    // Start/stop continuous conversion
    void startContinuous();
    void stopContinuous();

    // Read a single conversion (blocking, uses DRDY)
    ADC_Reading readSingle();

    // Read a single conversion via polling (no DRDY needed).
    ADC_Reading readPoll(uint32_t poll_ms = 5);

    // Read in continuous mode (non-blocking, returns valid=false if not ready)
    ADC_Reading readContinuous();

    // Read immediately after DRDY is known LOW — no start/stop/recheck.
    // Use inside tight continuous loops to avoid filter restarts.
    ADC_Reading readDirect();

    // Check if new data is available
    bool dataReady() const;

    // Self-offset calibration (short inputs before calling!)
    bool calibrate();

    // Device ID check
    uint8_t getDeviceID();
    bool isConnected();

    // Current VREF in volts (what codeToVoltage uses)
    float getVrefV() const { return _vref_V; }

    // Debug
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
