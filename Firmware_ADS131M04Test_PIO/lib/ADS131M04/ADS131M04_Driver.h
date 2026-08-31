/**
 * @file ADS131M04_Driver.h
 * @brief ADS131M04 4-channel simultaneous-sampling ADC driver — Portenta H7 + Mid Carrier.
 *
 * Candidate replacement for the ADS1263. Written against
 * docs/ads131m04_datasheet.pdf (SBAS890D) and docs/ADS131M04_EVM_User_Guide.pdf
 * (SBAU332A); the evaluation plan is docs/ADS131M04_migration_plan.md.
 *
 * ── How this chip differs from the ADS1263 (read before porting anything) ──
 *
 * 1. FRAME PROTOCOL, NOT REGISTER READS. There is no "RDATA" command returning
 *    one channel. Every SPI transaction is a fixed 6-word FRAME (§8.5.1.7),
 *    18 bytes at the reset-default 24-bit word length:
 *
 *        DIN :  [ command ][ data-or-zero ][ 0 ][ 0 ][ 0 ][ 0 ]
 *        DOUT:  [ response ][  ch0  ][  ch1  ][  ch2  ][  ch3  ][ CRC ]
 *
 *    So one NULL frame yields STATUS *and* all four channels together.
 *
 * 2. THE RESPONSE WORD LAGS ONE FRAME. DOUT word 0 answers the command sent in
 *    the PREVIOUS frame. A register access is therefore always two frames, and
 *    readRegister()/writeRegister() below absorb the lag so that the frame a
 *    caller runs next always carries STATUS in word 0, never a stale ack.
 *
 * 3. ALL FOUR CHANNELS SAMPLE SIMULTANEOUSLY off one modulator clock, and one
 *    DRDY covers all of them — unlike the ADS1263, where DRDY was ADC1-only and
 *    ADC2 had no data-ready line at all. There is no inter-channel skew to
 *    correct in analysis.
 *
 * 4. AN EXTERNAL CLOCK IS MANDATORY. CLKIN must be fed a continuous free-running
 *    LVCMOS clock or the device never converts (§8.3.5) — there is no internal
 *    oscillator. On the EVM that is the on-board 8.192 MHz oscillator Y1,
 *    selected by the DEFAULT JP6 jumper: leave it alone, and do NOT install JP5
 *    (JP5 powers Y1 down). Every rate number here assumes f_CLKIN = 8.192 MHz,
 *    hence f_MOD = 4.096 MHz.
 *
 *    A missing CLKIN presents as "the data is frozen", not as an error: register
 *    reads still work. sps() and the caller's own rate check are what catch it.
 *
 * 5. THE REFERENCE IS INTERNAL, FIXED AND SMALL. 1.2 V, no external REF pin.
 *    Full scale is +/-1.2 V / gain (§8.3.3, Table 8-1) — NOT the +/-5 V the
 *    ADS1263 gets from the external REF7050. A 0-5 V sensor output WILL clip,
 *    and it clips into a flat rail that reads like a stuck sensor. Attenuate
 *    ahead of the ADC; gain only makes the range smaller.
 *
 * 6. THE OUTPUT CRC IS ALWAYS ON and cannot be disabled (§8.3.12). It replaces
 *    the ADS1263's checksum byte as the validity gate and is strictly better —
 *    it covers the command and status words too, not just the data. Input CRC
 *    stays disabled (the reset default): a corrupted command is caught by
 *    reading the register back, and enabling it costs a word on every frame.
 *
 * 7. SYNC/RESET IS ONE PIN DOING TWO JOBS, AND THE SHORT PULSE IS THE ONE THAT
 *    DOES NOT RESET (§6.6, §8.4.1.2, §8.5.2):
 *        >= 2048 t_CLKIN  (t_w(RSL))  -> RESET      = 250 us at 8.192 MHz
 *         1 .. 2047 t_CLKIN (t_w(SYL)) -> SYNCHRONISE (122 ns .. 250 us)
 *    A few-microsecond "reset" pulse therefore silently performs a sync: the
 *    chip keeps its configuration, keeps streaming, and looks healthy. reset()
 *    holds the line low for 1 ms to sit well clear of that boundary.
 *
 * ── Pinout — reuses the ADS1263 harness one-for-one ────────────────────────
 * The ADS131M04 needs exactly the same eight wires as the ADS1263 EVM, so
 * Cable 1 is re-terminated rather than redesigned and the two boards stay
 * swap-in-place for A/B. Portenta Mid Carrier (ASX00055) breakout header J15:
 *
 *   CS         = PA_8  (J15-25, silkscreen "PWM 0")  -> EVM J6[4]
 *   DRDY       = PC_6  (J15-27, silkscreen "PWM 1")  -> EVM J6[6]
 *   SYNC/RESET = PC_7  (J15-29, silkscreen "PWM 2")  -> EVM J6[1]
 *   SCLK       = J15-20 (SPI1 SCLK, STM32 PI_1)      -> EVM J6[5]
 *   CIPO/MISO  = J15-22 (SPI1 CIPO, STM32 PC_2)      -> EVM J6[7]  (DOUT)
 *   COPI/MOSI  = J15-24 (SPI1 COPI, STM32 PC_3)      -> EVM J6[2]  (DIN)
 *   GND        = J15-1/2                             -> EVM J6[8]
 *   3V3        = J15-3/4                             -> EVM 3V3 (if not self-powered)
 *
 * EVM J6[3] is the ADC's CLK pin — leave it UNCONNECTED, Y1 drives it.
 *
 * SPI mode 1 (CPOL=0, CPHA=1), MSB first, 25 MHz ceiling at 3.3 V DVDD
 * (t_c(SC) >= 40 ns, §6.6). begin() defaults far below that — see its comment.
 *
 * ── Why this driver owns an mbed::SPI instead of using Arduino `SPI` ──────
 * The Arduino mbed core SILENTLY DROPS THE SPI MODE across an end()/begin()
 * cycle (libraries/SPI/SPI.cpp):
 *
 *     end()              -> delete dev->obj      // hardware format lost
 *                        -> `settings` member NOT reset
 *     begin()            -> new mbed::SPI(...)   // constructed as MODE 0
 *     beginTransaction(s)-> if (s != settings) { obj->format(...); }
 *                                                // stale cache == s, so the
 *                                                // format is NEVER re-applied
 *
 * The peripheral then runs in mode 0 while the caller believes mode 1. Mode 0
 * samples on the rising edge instead of the falling one — one bit early — so
 * every word arrives right-shifted by one. Bench signature: STATUS 0x050F reads
 * back as 0x0287, and the DRDY nibble 0xF reads as 0x7.
 *
 * This is triggered by our OWN diagnostics: anything that hands the pins to
 * GPIO (`hold`, `pintest`, `bitbang`) must release the bus, and on re-acquiring
 * it the mode was gone. Owning the object makes acquiring the bus and applying
 * the format the same operation, which is the only way it cannot drift.
 *
 * It also lets a frame go out as ONE block transfer rather than 18 separate
 * SPI.transfer() calls, which is what the byte-at-a-time path cost us in rate.
 *
 * Use busRelease()/busAcquire() around any GPIO use of the SPI pins. Never call
 * the global Arduino `SPI` object on this bus — two owners, same pads.
 */

#ifndef ADS131M04_DRIVER_H
#define ADS131M04_DRIVER_H

#include <Arduino.h>

namespace mbed { class SPI; }   // owned directly — see the note above

// ── Pins (identical to the ADS1263 driver's, deliberately) ────────────────
#define ADS131M04_CS_PIN     PA_8   // J15-25 (PWM_0)
#define ADS131M04_DRDY_PIN   PC_6   // J15-27 (PWM_1) — active low
#define ADS131M04_RESET_PIN  PC_7   // J15-29 (PWM_2) — SYNC/RESET, active low

// SPI1, driven through mbed::SPI directly rather than the Arduino wrapper —
// see the "why we own the SPI object" note above. Arduino pin numbers for the
// same pads are D9 / D8 / D10, which is what the GPIO diagnostics must use
// (digitalWrite(PI_1, ..) through the PinName overload drives nothing).
#define ADS131M04_SCLK_PIN   PI_1   // J15-20 -> EVM J6[5]   (D9)
#define ADS131M04_COPI_PIN   PC_3   // J15-24 -> EVM J6[2]   (D8)
#define ADS131M04_CIPO_PIN   PC_2   // J15-22 -> EVM J6[7]   (D10)

// ── Fixed chip constants ──────────────────────────────────────────────────
#define ADS131M04_NUM_CH        4
#define ADS131M04_VREF_V        1.2f        // internal band-gap, not adjustable
#define ADS131M04_FCLKIN_HZ     8192000.0f  // EVM Y1 (HR mode)
#define ADS131M04_FRAME_WORDS   6           // cmd + 4 data + CRC
#define ADS131M04_WORD_BYTES    3           // WLENGTH = 24-bit (reset default)
#define ADS131M04_FRAME_BYTES   (ADS131M04_FRAME_WORDS * ADS131M04_WORD_BYTES)
#define ADS131M04_SCLK_MAX_HZ   25000000UL  // t_c(SC) >= 40 ns at 2.7-3.6 V DVDD

// Timing, §6.6 / §6.7. RESET_LOW_US is deliberately 4x the 250 us minimum —
// see note 7 above for what happens if it lands short.
#define ADS131M04_RESET_LOW_US  1000        // >= t_w(RSL) = 2048 t_CLKIN = 250 us
#define ADS131M04_TREGACQ_US    5           // register default acquisition time

// /CS frame-boundary guard times, §6.6: td(CSSC) first SCLK after CS falling,
// td(SCCS) CS rising after final SCLK, tw(CSH) CS high duration.
//
// NOT taken from the datasheet's numbers. That table extracts with its columns
// shifted, and this project has already lost a day to a timing constant read
// out of a mangled TI table. 5 us is instead the value `bitbang` uses, which is
// the one hand-clocked path on this bench that has always framed correctly —
// and it is orders of magnitude above any plausible spec at 8.192 MHz CLKIN.
//
// Omitting these is not benign. transferFrame() went straight from CS low into
// the transfer and straight back out, so consecutive frames were separated only
// by two digitalWrite() calls. The device then misses the frame boundary and
// the whole frame arrives ONE BYTE early — reproducibly, on every WREG command
// frame (see STATUS.md). At ~15 us per frame the cost is negligible: 7.5 ms/s
// at 500 SPS.
#define ADS131M04_CS_SETUP_US   5           // >= td(CSSC)
#define ADS131M04_CS_HOLD_US    5           // >= td(SCCS)
#define ADS131M04_CS_HIGH_US    5           // >= tw(CSH)

// Settling allowance after a write that makes the device RESYNCHRONISE.
// Table 8-3 gives filter startup times per OSR; the slowest setting needs a few
// conversion periods, which at 250 SPS is milliseconds. 20 ms covers every OSR
// and costs nothing: it is only ever spent on the failure path of a register
// write, and register writes are rare.
#define ADS131M04_RESYNC_SETTLE_MS  20
#define ADS131M04_TPOR_US       250         // power-on-reset time

// How long reset() will keep asking for the ID before it calls the part absent.
// This is NOT a chip spec — t_POR is 250 us and would be met by a blind delay
// IF the ADC's rails came up with the H7's. On this rig they do not: AVDD is
// generated on the EVM by U1 from an external 5 V bench supply, so the ADC can
// still be dark long after the H7 has booted. 250 ms covers an operator
// switching the supply on a beat late without stalling a healthy boot (a live
// part answers on the first read, and the loop exits immediately).
#define ADS131M04_READY_TIMEOUT_MS  250

// ── Register addresses (§8.6, Table 8-12) ─────────────────────────────────
#define ADS131M04_REG_ID          0x00
#define ADS131M04_REG_STATUS      0x01
#define ADS131M04_REG_MODE        0x02
#define ADS131M04_REG_CLOCK       0x03
#define ADS131M04_REG_GAIN1       0x04
#define ADS131M04_REG_CFG         0x06
#define ADS131M04_REG_THRSHLD_MSB 0x07
#define ADS131M04_REG_THRSHLD_LSB 0x08
#define ADS131M04_REG_CH0_CFG     0x09
#define ADS131M04_REG_CH1_CFG     0x0E
#define ADS131M04_REG_CH2_CFG     0x13
#define ADS131M04_REG_CH3_CFG     0x18

// Per-channel blocks are 5 registers apart: CFG, OCAL_MSB/LSB, GCAL_MSB/LSB.
#define ADS131M04_CH_CFG(n)  ((uint8_t)(0x09 + 5 * (n)))

// ── Commands (§8.5.1.10, Table 8-11) ──────────────────────────────────────
#define ADS131M04_CMD_NULL     0x0000
#define ADS131M04_CMD_RESET    0x0011
#define ADS131M04_CMD_STANDBY  0x0022
#define ADS131M04_CMD_WAKEUP   0x0033
#define ADS131M04_CMD_LOCK     0x0555
#define ADS131M04_CMD_UNLOCK   0x0655
#define ADS131M04_CMD_RREG     0xA000   // | (addr << 7) | (count - 1)
#define ADS131M04_CMD_WREG     0x6000   // | (addr << 7) | (count - 1)
#define ADS131M04_RSP_WREG     0x4000   // | (addr << 7) | (written - 1)
#define ADS131M04_RSP_RESET    0xFF24   // response in the frame after a RESET

// ── STATUS register bits (§8.6.2) ─────────────────────────────────────────
#define ADS131M04_ST_LOCK      (1u << 15)
#define ADS131M04_ST_F_RESYNC  (1u << 14)
#define ADS131M04_ST_REG_MAP   (1u << 13)
#define ADS131M04_ST_CRC_ERR   (1u << 12)
#define ADS131M04_ST_RESET     (1u << 10)
#define ADS131M04_ST_DRDY_MASK 0x000Fu   // DRDY3..DRDY0

// ── ID register (§8.6.1) — bits 15:12 = 0010b, 11:8 = CHANCNT = 0100b ─────
#define ADS131M04_ID_MASK      0xFF00u
#define ADS131M04_ID_EXPECTED  0x2400u

// ── OSR codes — CLOCK[4:2] (§8.6.4, Table 8-17) ───────────────────────────
// Output rate = f_MOD / OSR, with f_MOD = f_CLKIN / 2 = 4.096 MHz in HR mode.
//
// NOTE — the datasheet contradicts itself on code 7: the CLOCK register table
// (8-17) says 16256, the data-rate table (8-2) says 16384. sps() uses 16256,
// the register table's value. If the exact rate at that code ever matters,
// MEASURE it rather than trusting either number (plan §7, T5).
typedef enum {
    ADS131M04_OSR_128   = 0,   // 32 kSPS
    ADS131M04_OSR_256   = 1,   // 16 kSPS
    ADS131M04_OSR_512   = 2,   //  8 kSPS
    ADS131M04_OSR_1024  = 3,   //  4 kSPS (reset default)
    ADS131M04_OSR_2048  = 4,   //  2 kSPS
    ADS131M04_OSR_4096  = 5,   //  1 kSPS
    ADS131M04_OSR_8192  = 6,   //  500 SPS  <- closest to the ADS1263's 400 SPS
    ADS131M04_OSR_16256 = 7,   //  ~252 SPS (see note above)
} ADS131M04_OSR_t;

// ── Power modes — CLOCK[1:0] ──────────────────────────────────────────────
// Each mode expects its own f_CLKIN (HR 8.192 / LP 4.096 / VLP 2.048 MHz). The
// EVM's JP6 jumper picks the physical clock; this register must agree with it,
// or every rate is wrong by the ratio between them.
typedef enum {
    ADS131M04_PWR_VLP = 0,
    ADS131M04_PWR_LP  = 1,
    ADS131M04_PWR_HR  = 2,   // reset default; matches the EVM's default clock
} ADS131M04_PWR_t;

// ── PGA gain codes — GAIN1, 3 bits per channel (§8.6.5) ───────────────────
// FSR = +/-1.2 V / gain. Gain 1 is the only setting a 0-5 V sensor can use, and
// only after external attenuation — raising gain shrinks the range further.
typedef enum {
    ADS131M04_GAIN_1   = 0,
    ADS131M04_GAIN_2   = 1,
    ADS131M04_GAIN_4   = 2,
    ADS131M04_GAIN_8   = 3,
    ADS131M04_GAIN_16  = 4,
    ADS131M04_GAIN_32  = 5,
    ADS131M04_GAIN_64  = 6,
    ADS131M04_GAIN_128 = 7,
} ADS131M04_Gain_t;

// ── Input multiplexer — CHn_CFG[1:0] (§8.3.9, §8.6.10) ────────────────────
// The internal DC test signal is nominally 2/15 x VREF and AUTO-SCALES with
// gain, so it is always 2/15 of full scale: 160 mV at gain 1, 80 mV at gain 2.
// It needs no external hardware, which is what makes T8 runnable on a bare
// board, and it exists in both polarities — so it exercises sign extension,
// not just scaling. MUX_SHORTED is a cleaner noise reference than grounding
// the inputs through the EVM's jumpers, because it removes the 1 k resistors
// and any external pickup from the measurement.
typedef enum {
    ADS131M04_MUX_AIN      = 0,   // AINnP / AINnN (default)
    ADS131M04_MUX_SHORTED  = 1,   // ADC inputs shorted internally
    ADS131M04_MUX_TEST_POS = 2,   // +2/15 x FSR
    ADS131M04_MUX_TEST_NEG = 3,   // -2/15 x FSR
} ADS131M04_Mux_t;

// Test-signal amplitude as a fraction of FSR (§8.3.9).
#define ADS131M04_TEST_NUM  2
#define ADS131M04_TEST_DEN  15

// ── One simultaneous sample of all four channels ──────────────────────────
struct ADS131M04_Reading {
    bool     valid;                      // output CRC matched
    uint16_t status;                     // STATUS reg (word 0 of a NULL frame)
    uint32_t hw_us;                      // micros() captured at end of transfer
    int32_t  raw[ADS131M04_NUM_CH];      // sign-extended 24-bit codes
    float    volts[ADS131M04_NUM_CH];    // scaled by each channel's own gain
};

// ── Raw frame, for callers that need the un-decoded words ─────────────────
struct ADS131M04_Frame {
    bool     crc_ok;
    uint16_t response;                   // answers the PREVIOUS frame's command
    int32_t  data[ADS131M04_NUM_CH];
    uint16_t crc_rx;
    uint16_t crc_calc;
    // Un-decoded DOUT bytes exactly as they arrived. Kept because a CRC
    // mismatch is undiagnosable from the decoded fields alone: it cannot tell
    // "bits were corrupted on the wire" from "the frame is intact and our CRC
    // disagrees with the chip's". The `raw` command prints these.
    uint8_t  rx[ADS131M04_FRAME_BYTES];
};

class ADS131M04_Driver {
public:
    ADS131M04_Driver();

    /**
     * Bring the chip up: pins, SPI, hardware reset, ID check.
     *
     * @param spi_hz SPI clock. Defaults to 2 MHz, NOT the 25 MHz the chip
     *        allows. Cable 1 is a hand-wired unshielded harness that the
     *        ADS1263 drives at 500 kHz, and SCLK is the rig's primary EMI
     *        aggressor into the laser channel. Raise it on the bench with the
     *        clock ladder in plan §7/T3 and watch crcErrors() — do not guess.
     * @return false if the ID register does not read 0x24xx.
     */
    bool begin(uint32_t spi_hz = 2000000);

    /** Hardware SYNC/RESET (1 ms low — see note 7), settle, re-check the ID. */
    bool reset();

    /**
     * Poll the ID register until it reads 0x24xx, or `timeout_ms` elapses.
     * Sets present() either way and returns the same answer.
     *
     * Why polling and not a delay: §8.4.1 says the device ignores ALL SPI until
     * its first DRDY rising edge, and §6.7 specifies t_POR "measured from
     * supplies at 90%" — i.e. from the ADC's OWN rails, which on an externally
     * powered EVM have nothing to do with when the H7 started counting. A fixed
     * wait measures the wrong clock. Asking the chip is the only check that
     * stays honest whatever order the supplies come up in.
     *
     * NOT gated on the DRDY pin, deliberately. At the reset-default
     * DRDY_FMT = 0b the line goes low on the first conversion and stays there,
     * rising only "briefly" between conversions (§8.4.3.1) — a pulse the
     * datasheet itself warns "may be too narrow for some microcontrollers to
     * detect" (§8.5.1.9). At OSR=1024 the post-t_POR high window is one sample
     * period, ~250 us, so a level check that starts late never sees it.
     */
    bool waitInterfaceReady(uint32_t timeout_ms = ADS131M04_READY_TIMEOUT_MS);

    /** SPI RESET command (0x0011). Softer than reset(); same register effect. */
    bool resetCommand();

    /** ID register. 0x24xx on a healthy part (0010b + CHANCNT=4). */
    uint16_t deviceID() { return readRegister(ADS131M04_REG_ID); }

    /** True if the last begin()/reset() read a plausible ID. */
    bool present() const { return _present; }

    /**
     * Set rate, power mode and which channels convert. Reads the register back
     * and returns false on mismatch, so a silently-ignored write is caught here
     * rather than three stages later.
     *
     * @param ch_mask bit n enables channel n. A disabled channel still occupies
     *        its word in the frame — the ADC just stops converting it.
     */
    bool configure(ADS131M04_OSR_t osr,
                   ADS131M04_PWR_t pwr = ADS131M04_PWR_HR,
                   uint8_t ch_mask = 0x0F);

    /** Per-channel PGA gain. Read-modify-writes GAIN1 so channels stay free. */
    bool setGain(uint8_t ch, ADS131M04_Gain_t gain);
    ADS131M04_Gain_t getGain(uint8_t ch) const;

    /** Per-channel input mux: real inputs, internal short, or DC test signal. */
    bool setInputMux(uint8_t ch, ADS131M04_Mux_t mux);
    ADS131M04_Mux_t getInputMux(uint8_t ch) const;

    /** Expected volts for the current mux setting: +/-2/15 x FSR, else 0. */
    float expectedVolts(uint8_t ch) const;

    /**
     * One NULL frame: STATUS + all four channels + CRC check.
     *
     * Does NOT wait for DRDY — the caller owns timing, exactly as the ADS1263
     * driver's readADC1Direct() did, so the proven timed-poll loop on the M4
     * stays proven. Reading faster than the data rate simply re-fetches the
     * same conversion (harmless; the ring absorbs the rate).
     */
    bool readChannels(ADS131M04_Reading &out);

    /**
     * DRDY pin, active low. Wired but not gated on by readChannels().
     * At the reset-default MODE.DRDY_FMT = 0b ("logic low") the pin asserts on
     * a new conversion and HOLDS low until the data are read — so this is a
     * level, not a pulse, and polling it is reliable.
     */
    bool dataReadyPin() const { return digitalRead(ADS131M04_DRDY_PIN) == LOW; }

    /**
     * Block until a conversion is available (DRDY low). Returns immediately if
     * one is already pending. false on timeout.
     * Valid only while DRDY_FMT = 0b — see the implementation comment.
     */
    bool waitDataReady(uint32_t timeout_ms = 50);

    // ── Register access (two frames each — the response lags one frame) ───
    uint16_t readRegister(uint8_t addr);
    bool     writeRegister(uint8_t addr, uint16_t value);

    /** Low-level: run one frame. `wdata`/`nwdata` carry any WREG payload. */
    bool transferFrame(uint16_t cmd, const uint16_t *wdata, uint8_t nwdata,
                       ADS131M04_Frame &out);

    /**
     * Clock an ARBITRARY number of bytes as one CS-low transaction.
     *
     * Exists to test frame length. transferFrame() hard-codes six words, so it
     * cannot see a seventh if the device ever emits one — it would simply chop
     * it off, which is indistinguishable from the frame we currently call
     * corrupt. Diagnostic only; nothing in the data path uses it.
     */
    bool transferRaw(const uint8_t *tx, uint8_t *rx, size_t len);

    // ── Derived numbers ───────────────────────────────────────────────────
    float   sps() const;                     // nominal output data rate
    float   fsrVolts(uint8_t ch) const;      // +/-FSR for that channel's gain
    float   lsbVolts(uint8_t ch) const;      // one code, in volts
    uint8_t gainMultiplier(uint8_t ch) const;
    uint16_t osrDivisor() const;

    // ── Diagnostics ───────────────────────────────────────────────────────
    void printConfig(Stream &s);
    void printRegisters(Stream &s);

    // Running counters, cleared by resetCounters().
    uint32_t crcErrors() const  { return _crc_err; }
    uint32_t framesRead() const { return _frames; }
    void     resetCounters()    { _crc_err = 0; _frames = 0; }

    uint32_t spiHz() const { return _spi_hz; }
    /** Change the SPI clock without re-initialising the chip (T3's ladder). */
    void setSpiHz(uint32_t hz);

    /**
     * Hand SCLK/COPI/CIPO back to GPIO so a diagnostic can drive them by hand.
     * Destroys the mbed::SPI, which releases the pins.
     */
    void busRelease();

    /**
     * Take the pins back and re-apply mode + clock. Acquiring the bus and
     * setting the format are deliberately the SAME operation — separating them
     * is exactly the bug in the Arduino wrapper described in the header note.
     * Safe to call when already held.
     */
    void busAcquire();

    /** True while the driver holds the SPI pins. */
    bool busHeld() const { return _spi_dev != nullptr; }

    /**
     * Clock `count` bytes out with /CS held HIGH, so the ADC ignores all of it.
     * For putting a known waveform on SCLK/COPI for a meter or a scope.
     */
    void clockBurst(uint8_t pattern, uint32_t count);

    /** CRC-16/CCITT-FALSE (poly 0x1021, seed 0xFFFF), MSB-first over bytes. */
    static uint16_t crc16(const uint8_t *data, size_t len);

private:
    mbed::SPI  *_spi_dev;       // owned; nullptr while the bus is released
    uint32_t    _spi_hz;
    bool        _present;

    ADS131M04_OSR_t _osr;
    ADS131M04_PWR_t _pwr;
    uint8_t         _ch_mask;
    uint8_t         _gain[ADS131M04_NUM_CH];   // ADS131M04_Gain_t codes
    uint8_t         _mux[ADS131M04_NUM_CH];    // ADS131M04_Mux_t codes

    uint32_t _crc_err;
    uint32_t _frames;

    static int32_t sext24(uint32_t v);

    /** Push mode + clock into the peripheral. Called on every busAcquire(). */
    void applyFormat();

    /**
     * Consume a pending conversion so the frames that follow do not start on a
     * conversion boundary. See the implementation comment — this is the
     * difference between register access working and working 75 % of the time.
     */
    void quiesce();

    /** STATUS as seen by the last quiesce() frame — i.e. what a NULL response
     *  looks like right now. Used to recognise a command that was silently
     *  dropped, which the output CRC cannot detect. */
    uint16_t _last_status = 0;
};

#endif // ADS131M04_DRIVER_H
