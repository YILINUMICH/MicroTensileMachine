/**
 * @file ADS1263_Driver.h
 * @brief ADS1263 dual-ADC driver — Portenta H7 (M4 core)
 *
 * The ADS1263 has two independent ADCs on one die:
 *
 *   ADC1 — 32-bit, Sinc1-4 or FIR, up to 38 400 SPS. Used here for the
 *          load cell front end (via an LCA amplifier) on AIN0/AIN1.
 *
 *   ADC2 — 24-bit, Sinc3 only, up to 800 SPS. Used here for a laser
 *          displacement head on AIN2/AIN3. The sensor already drives a
 *          0–5 V single-ended output, so no external amplifier, ADC2
 *          gain = 1.
 *
 * Both ADCs share the same SPI bus, CS pin, reset line, and chip-level
 * registers (POWER, INTERFACE), but have disjoint configuration registers
 * (MODE0/1/2 + INPMUX + REFMUX for ADC1;  ADC2CFG + ADC2MUX for ADC2)
 * and disjoint start/stop/read commands. They can run concurrently: the
 * chip maintains two independent conversion pipelines internally.
 *
 * This driver exposes two parallel APIs — `configureADC1 / startADC1 /
 * readADC1*` and the same for ADC2 — sharing one chip init in `begin()`.
 *
 * Pinout — Portenta H7 + Hat Carrier (J5 Pi-compatible header):
 *   CS    = PE_6   (J2-53, Pi pin 15)
 *   DRDY  = PJ_11  (J2-50, Pi pin 11) — ADC1 data-ready line, but the
 *                  onboard LoRa IRQ is wired to the same pad on the H7
 *                  so it never actually asserts. Timed polling is used
 *                  for both ADCs. ADC2 has no DRDY signal at all.
 *   RESET = PI_5   (J1-56, Pi pin 12)
 *
 * Scaling:
 *   ADC1:  V_in = (code32 / 2^31) * (ADC1 VREF) / (ADC1 gain=1 here)
 *   ADC2:  V_in = (code24 / 2^23) * (ADC2 VREF) / (ADC2 gain)
 */

#ifndef ADS1263_DRIVER_H
#define ADS1263_DRIVER_H

#include <Arduino.h>
#include <SPI.h>

// ── Pin definitions — Portenta H7 + Hat Carrier ──────────────────────────
#define ADS1263_CS_PIN    PE_6   // J2-53, Pi pin 15
#define ADS1263_DRDY_PIN  PJ_11  // J2-50, Pi pin 11 (LoRa collision; unused)
#define ADS1263_RESET_PIN PI_5   // J1-56, Pi pin 12

// ── External reference (volts) used by both ADC paths when they select
//   AVDD/AVSS as the reference source.
#define ADS1263_VREF_V    5.0f

// ══════════════════════════════════════════════════════════════════════════
//  Commands
// ══════════════════════════════════════════════════════════════════════════
#define ADS1263_CMD_NOP     0x00
#define ADS1263_CMD_RESET   0x06
// ADC1
#define ADS1263_CMD_START1  0x08
#define ADS1263_CMD_STOP1   0x0A
#define ADS1263_CMD_RDATA1  0x12
#define ADS1263_CMD_SYOCAL1 0x16
#define ADS1263_CMD_SYGCAL1 0x17
#define ADS1263_CMD_SFOCAL1 0x19
// ADC2
#define ADS1263_CMD_START2  0x0C
#define ADS1263_CMD_STOP2   0x0E
#define ADS1263_CMD_RDATA2  0x14
#define ADS1263_CMD_SYOCAL2 0x1B
#define ADS1263_CMD_SYGCAL2 0x1C
#define ADS1263_CMD_SFOCAL2 0x1E
// Register access
#define ADS1263_CMD_RREG    0x20
#define ADS1263_CMD_WREG    0x40

// ══════════════════════════════════════════════════════════════════════════
//  Registers
// ══════════════════════════════════════════════════════════════════════════
#define ADS1263_REG_ID         0x00
#define ADS1263_REG_POWER      0x01
#define ADS1263_REG_INTERFACE  0x02
// ADC1 config
#define ADS1263_REG_MODE0      0x03
#define ADS1263_REG_MODE1      0x04
#define ADS1263_REG_MODE2      0x05
#define ADS1263_REG_INPMUX     0x06
#define ADS1263_REG_OFCAL0     0x07
#define ADS1263_REG_OFCAL1     0x08
#define ADS1263_REG_OFCAL2     0x09
#define ADS1263_REG_FSCAL0     0x0A
#define ADS1263_REG_FSCAL1     0x0B
#define ADS1263_REG_FSCAL2     0x0C
#define ADS1263_REG_IDACMUX    0x0D
#define ADS1263_REG_IDACMAG    0x0E
#define ADS1263_REG_REFMUX     0x0F
#define ADS1263_REG_TDACP      0x10
#define ADS1263_REG_TDACN      0x11
#define ADS1263_REG_GPIOCON    0x12
#define ADS1263_REG_GPIODIR    0x13
#define ADS1263_REG_GPIODAT    0x14
// ADC2 config
#define ADS1263_REG_ADC2CFG    0x15   // DR2[7:6] | GAIN2[5:3] | REF2[2:0]
#define ADS1263_REG_ADC2MUX    0x16   // MUXP2[7:4] | MUXN2[3:0]
#define ADS1263_REG_ADC2OFC0   0x17
#define ADS1263_REG_ADC2OFC1   0x18
#define ADS1263_REG_ADC2OFC2   0x19
#define ADS1263_REG_ADC2FSC0   0x1A
#define ADS1263_REG_ADC2FSC1   0x1B
#define ADS1263_REG_ADC2FSC2   0x1C

// ══════════════════════════════════════════════════════════════════════════
//  ADC1 data-rate enum (MODE2[3:0])
// ══════════════════════════════════════════════════════════════════════════
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
} ADS1263_ADC1_Rate_t;

// REFMUX values for ADC1 (REFMUX register layout: RMUXP[5:3] | RMUXN[2:0])
#define ADS1263_REFMUX_INTERNAL_2V5   0x00  // internal 2.5 V
#define ADS1263_REFMUX_EXT_AIN01      0x09  // external ref on AIN0(+)/AIN1(-)
#define ADS1263_REFMUX_EXT_AIN23      0x12  // external ref on AIN2(+)/AIN3(-)
#define ADS1263_REFMUX_EXT_AIN45      0x1B  // external ref on AIN4(+)/AIN5(-)
#define ADS1263_REFMUX_AVDD_AVSS      0x24  // AVDD(+) / AVSS(-)  (5 V external)

// ══════════════════════════════════════════════════════════════════════════
//  ADC2 data-rate / gain / reference enums (ADC2CFG fields)
// ══════════════════════════════════════════════════════════════════════════
typedef enum {
    ADS1263_ADC2_10SPS   = 0x00,  // period 100 ms
    ADS1263_ADC2_100SPS  = 0x01,  // period  10 ms
    ADS1263_ADC2_400SPS  = 0x02,  // period 2.5 ms
    ADS1263_ADC2_800SPS  = 0x03   // period 1.25 ms
} ADS1263_ADC2_Rate_t;

typedef enum {
    ADS1263_ADC2_GAIN_1   = 0x00,
    ADS1263_ADC2_GAIN_2   = 0x01,
    ADS1263_ADC2_GAIN_4   = 0x02,
    ADS1263_ADC2_GAIN_8   = 0x03,
    ADS1263_ADC2_GAIN_16  = 0x04,
    ADS1263_ADC2_GAIN_32  = 0x05,
    ADS1263_ADC2_GAIN_64  = 0x06,
    ADS1263_ADC2_GAIN_128 = 0x07
} ADS1263_ADC2_Gain_t;

// REF2 field values for ADC2 (ADC2CFG[2:0])
#define ADS1263_ADC2_REF_INTERNAL_2V5  0x00
#define ADS1263_ADC2_REF_AIN01         0x01
#define ADS1263_ADC2_REF_AIN23         0x02  // conflicts with laser-head input!
#define ADS1263_ADC2_REF_AIN45         0x03
#define ADS1263_ADC2_REF_AVDD_AVSS     0x04

// ══════════════════════════════════════════════════════════════════════════
//  ADC reading struct (used by both ADC1 and ADC2 reads)
// ══════════════════════════════════════════════════════════════════════════
typedef struct {
    bool valid;
    int32_t raw_code;       // ADC1: full 32-bit signed; ADC2: sign-extended 24-bit
    float voltage_V;
    float voltage_uV;
    uint32_t timestamp_us;
} ADC_Reading;

// ══════════════════════════════════════════════════════════════════════════
//  Driver class
// ══════════════════════════════════════════════════════════════════════════
class ADS1263_Driver {
public:
    ADS1263_Driver();

    // ── Chip-level init ────────────────────────────────────────────────
    // Verifies device ID, writes POWER / INTERFACE, parks ADC1 mux+ref and
    // ADC2 config at inactive defaults, and disables IDAC. Neither ADC
    // starts converting until you call configureADCx() + startADCx().
    bool begin();
    void reset();

    uint8_t getDeviceID();
    bool isConnected();

    // ── ADC1 path (32-bit, load-cell etc.) ─────────────────────────────
    // `inpmux`  = INPMUX register value, e.g. 0x01 for AIN0(+)/AIN1(-).
    // `refmux`  = one of ADS1263_REFMUX_*, or a raw byte.
    // `vref_V`  = numeric reference voltage used by codeToVoltage math.
    // `pga_bypass` = true keeps PGA out of the signal path (gain=1).
    void configureADC1(uint8_t inpmux,
                       uint8_t refmux,
                       float vref_V,
                       ADS1263_ADC1_Rate_t rate,
                       bool pga_bypass = true);

    void startADC1();
    void stopADC1();

    // readADC1Direct() assumes continuous mode is running and a fresh
    // sample is available; caller is responsible for timing.
    ADC_Reading readADC1Direct();

    // readADC1Poll() runs START1 → delay → read → (optionally restore
    // continuous). Useful for one-shot reads.
    ADC_Reading readADC1Poll(uint32_t poll_ms = 5);

    // DRDY pin state (for completeness; on this board the onboard LoRa
    // chip holds PJ_11 HIGH so this rarely reports LOW).
    bool adc1DataReadyPin() const;

    float getADC1VrefV() const   { return _adc1_vref_V; }
    float getADC1DataRate() const;

    // ── ADC2 path (24-bit, laser head etc.) ────────────────────────────
    // `adc2mux` = ADC2MUX register value, e.g. 0x23 for AIN2(+)/AIN3(-).
    // `ref2`    = one of ADS1263_ADC2_REF_*, 3-bit REF2 field.
    // `vref_V`  = numeric reference voltage used by codeToVoltage math.
    void configureADC2(uint8_t adc2mux,
                       uint8_t ref2,
                       float vref_V,
                       ADS1263_ADC2_Rate_t rate,
                       ADS1263_ADC2_Gain_t gain = ADS1263_ADC2_GAIN_1);

    void startADC2();
    void stopADC2();

    ADC_Reading readADC2Direct();
    ADC_Reading readADC2Poll(uint32_t poll_ms = 12);

    float getADC2VrefV() const   { return _adc2_vref_V; }
    float getADC2DataRate() const;
    uint8_t getADC2GainMultiplier() const;

    // ── Diagnostics ────────────────────────────────────────────────────
    void printConfig();
    void printRegisters();   // dumps registers 0x00…0x1C

private:
    SPISettings _spi;

    // ADC1 state
    ADS1263_ADC1_Rate_t _adc1_rate;
    uint8_t _adc1_refmux;
    uint8_t _adc1_inpmux;
    float   _adc1_vref_V;
    bool    _adc1_pga_bypass;
    bool    _adc1_running;

    // ADC2 state
    ADS1263_ADC2_Rate_t _adc2_rate;
    ADS1263_ADC2_Gain_t _adc2_gain;
    uint8_t _adc2_ref2;
    uint8_t _adc2_mux;
    float   _adc2_vref_V;
    bool    _adc2_running;

    // Low-level SPI
    void writeRegister(uint8_t reg, uint8_t value);
    uint8_t readRegister(uint8_t reg);
    void sendCommand(uint8_t cmd);

    // ADC1 helpers
    int32_t readRawData32();                   // RDATA1 → 6-byte frame
    float codeToVoltageADC1(int32_t code) const;
    void writeMODE2();                          // packs PGA bypass + rate

    // ADC2 helpers
    int32_t readRawData24();                   // RDATA2 → 5-byte frame
    float codeToVoltageADC2(int32_t code) const;
    void writeADC2CFG();                        // packs DR2|GAIN2|REF2

    // Shared
    bool waitForDRDY(uint32_t timeout_ms = 500);
    float rateToSPS_ADC1(ADS1263_ADC1_Rate_t rate) const;
    float rateToSPS_ADC2(ADS1263_ADC2_Rate_t rate) const;
};

#endif // ADS1263_DRIVER_H
