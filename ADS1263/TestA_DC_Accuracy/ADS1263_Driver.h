/**
 * @file ADS1263_Driver.h
 * @brief ADS1263 32-bit ADC driver for Arduino Uno
 *
 * Signal chain: GSO050 load cell → INA818 (gain=200) → ADS1263 (single channel)
 * - INA818 provides all signal amplification (gain=200)
 * - ADS1263 PGA is bypassed (gain=1)
 * - Single-ended input: INA818 output → AIN0, INA818 REF → AINCOM
 */

#ifndef ADS1263_DRIVER_H
#define ADS1263_DRIVER_H

#include <Arduino.h>
#include <SPI.h>

// Pin definitions for Arduino Uno
#define ADS1263_CS_PIN    10   // SPI CS (SS)
#define ADS1263_DRDY_PIN  2    // Data ready (interrupt-capable pin)
#define ADS1263_RESET_PIN 9    // Hardware reset

// ADS1263 Commands
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

// ADS1263 Registers
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
    float voltage_V;         // Voltage at ADC input (after INA818)
    float voltage_uV;        // Voltage in microvolts
    uint32_t timestamp_us;
} ADC_Reading;

class ADS1263_Driver {
public:
    ADS1263_Driver();

    // Initialize ADS1263 for single-channel, PGA-bypassed operation
    bool begin(ADS1263_DataRate_t rate = ADS1263_20SPS);

    void reset();

    // Set data rate
    void setDataRate(ADS1263_DataRate_t rate);
    float getCurrentDataRate() const;

    // Start/stop continuous conversion
    void startContinuous();
    void stopContinuous();

    // Read a single conversion (blocking)
    ADC_Reading readSingle();

    // Read in continuous mode (non-blocking, returns valid=false if not ready)
    ADC_Reading readContinuous();

    // Check if new data is available
    bool dataReady() const;

    // Self-offset calibration
    bool calibrate();

    // Device ID check
    uint8_t getDeviceID();
    bool isConnected();

    // Debug
    void printConfig();
    void printRegisters();

private:
    SPISettings _spi;
    ADS1263_DataRate_t _rate;
    bool _continuous;

    void writeRegister(uint8_t reg, uint8_t value);
    uint8_t readRegister(uint8_t reg);
    void sendCommand(uint8_t cmd);
    int32_t readRawData();
    float codeToVoltage(int32_t code) const;
    bool waitForDataReady(uint32_t timeout_ms = 500);
    float rateToSPS(ADS1263_DataRate_t rate) const;
};

#endif // ADS1263_DRIVER_H
