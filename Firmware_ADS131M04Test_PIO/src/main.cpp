/**
 * @file main.cpp
 * @brief ADS131M04 bench-test firmware — Portenta H7 M7 core.
 *
 * Qualifies ADS131M04_Driver against docs/ADS131M04_migration_plan.md §7.
 * Driven by the host sweep in ../Experiment_ADS131M04Eval/, not by a human at
 * a prompt: every acceptance number is a measurement over a HELD condition, so
 * the host sets a condition, captures, and moves on.
 *
 * ── Two channels, split by role (same shape as the CC fork) ───────────────
 *   USB-CDC : commands in, [STATUS] + boot banner + selftest verdicts out
 *   UDP     : the sample stream, once the host sends `netcfg <ip> <port>`
 * Text never rides UDP and samples never ride CDC.
 *
 * ── Host session contract (Experiment_SMAThermalCharacterization/lib_h7_session) ──
 * The sweep reuses that session, so this firmware must provide:
 *   - `[STATUS]` at 1 Hz on serial with NUMERIC key=value pairs, including
 *     `udp_on` (the session gates on it) plus `crc_err` and `frames` (the
 *     report takes deltas across a condition)
 *   - `netcfg <ip> <port>` and `ping`
 *   - unknown commands IGNORED, never wedging the parser — the session sends a
 *     few this firmware does not know
 *
 * ── Sampling is DRDY-GATED, and that is a deliberate change from the ADS1263 ──
 * The ADS1263 path used blind timed polling because its DRDY was ADC1-only and
 * an ISR that waits for edges freezes when they stop. Neither applies here:
 * one DRDY covers all four channels, and at the reset-default DRDY_FMT=0 it is
 * a LEVEL held low until the data is read — so a NON-BLOCKING level check in
 * the main loop cannot hang (if it never asserts we simply never read, and the
 * host's rate check catches it).
 *
 * The payoff is that every conversion is read exactly once. Blind timed polling
 * re-reads the same conversion whenever it runs faster than the data rate,
 * which is where the production stream's ~19% zero-order-hold duplicate rows
 * come from. It also makes T6 free: conversions counted == DRDY assertions.
 * `poll` switches to timed polling for an A/B.
 *
 * FLASH portenta_m4_idle FIRST — M4 shares SPI1 and the same CS pin.
 */

#include <Arduino.h>

#if defined(CORE_CM4)

// ── M4: do nothing at all ────────────────────────────────────────────────
// The whole point of this image is that M4 makes NO SPI traffic while the M7
// test owns the bus. Built by [env:portenta_m4_idle].
void setup() {}
void loop()  { __WFI(); }

#elif defined(CORE_CM7)

#include "ADS131M04_Driver.h"

#ifndef H7_TRANSPORT_UDP
#define H7_TRANSPORT_UDP 1          // default ON here; plan §6 keeps UDP
#endif
#if H7_TRANSPORT_UDP
#include <PortentaEthernet.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#endif

// ══════════════════════════════════════════════════════════════════════════
//  Configuration
// ══════════════════════════════════════════════════════════════════════════
#ifndef SPI_HZ_DEFAULT
#define SPI_HZ_DEFAULT 2000000      // conservative on a hand-wired harness
#endif
#define OSR_DEFAULT   ADS131M04_OSR_8192   // 500 SPS — the production candidate
#define STATUS_PERIOD_MS 1000
#define BATCH_FLUSH_BYTES 1400      // whole lines, under the Ethernet MTU

// How many pending conversions to drain back-to-back per loop pass. The FIFO is
// two deep (§8.5.1.9.1); 4 gives headroom to catch up after a stall without
// letting a stuck-low DRDY starve the rest of the loop.
#define DRDY_DRAIN_MAX 4

// Microseconds to wait AFTER DRDY asserts before starting the frame.
//
// DRDY-gated reading starts a frame at the exact instant a conversion
// completes, which is what §8.5.1.9 tells you not to do: "avoid reading ADC
// data during the time where new conversions complete". Waiting puts the frame
// in the middle of the conversion period instead — at 500 SPS the period is
// 2 ms and a frame costs ~177 µs, so there is room to sit clear of both ends.
//
// Runtime-settable via `dly <us>` so the CRC rate can be swept against it
// rather than guessed. 0 restores the original behaviour.
#define DRDY_DELAY_US_DEFAULT 0

static ADS131M04_Driver adc;

// ── src IDs (see plan §5 and lib_m04.SRC_FOR_CH) ─────────────────────────
// CH0/CH1 keep the production meanings so the Stage-3 M4 swap is a no-op for
// the host. CH2/CH3 borrow src=3/4, which in PRODUCTION are SMA voltage and
// current — safe only because this firmware is standalone. It is why these
// captures must never be fed to the thermal module's analysis pipeline.
static const uint8_t SRC_FOR_CH[ADS131M04_NUM_CH] = {1, 2, 3, 4};
static uint32_t seq_per_src[8] = {0};

// ── Counters, all published in [STATUS] ──────────────────────────────────
static uint32_t drdy_reads = 0;     // conversions actually consumed
static uint32_t samples_out = 0;
static uint32_t tx_drop = 0;
static uint32_t loop_passes = 0;
static bool     drdy_gated = true;  // false => blind timed polling (`poll`)
static uint32_t poll_us = 1500;
static uint32_t drdy_delay_us = DRDY_DELAY_US_DEFAULT;

// Both runtime-settable, purely to isolate Fault B. Bad frames arrive at a
// constant ~150/s regardless of frame rate, so the question is what else runs
// at that rate. `batch` varies packets/s while holding frames/s and samples/s
// fixed; `emit 0` removes the formatting and transmit path altogether.
static size_t batch_flush_bytes = BATCH_FLUSH_BYTES;
static bool   emit_on = true;

// FAST-READ + DE-DUPLICATE. Period in us between frames; 0 = off.
//
// Fault B is inherent to the device: bad frames arrive at a constant ~125/s
// against 500 conversions/s, and NOTHING the host does moves it — not the SPI
// clock (250 k-8 M), the frame duration, the conversion rate, DRDY-gating vs
// polling, the position within the conversion period, the UDP batch size, nor
// even switching the whole emit path off. It cannot be prevented, so absorb it:
// read several times per conversion and keep only the good frames.
//
// De-duplication is what makes that sound rather than a zero-order-hold mess.
// The device flags fresh data in STATUS[3:0] (DRDY per channel): the first read
// of a conversion carries 0x050F, re-reads carry 0x0500. Emitting only frames
// with those bits set delivers every conversion exactly once — which is what T5
// (rate accuracy) and T6 (DRDY count) require — while the extra reads dilute
// the ~125/s disturbance from ~25 % of delivered frames to a few percent.
static uint32_t fast_us = 0;            // 0 = one DRDY-gated read per conv
static uint32_t dedup_skipped = 0;
static bool     fast_auto = true;       // track the configured data rate

// How many reads per conversion when fast_auto is on. 4 takes the delivered
// frame-CRC failure rate from ~25 % to ~4 % (measured); beyond that it flattens,
// so more only costs bus time.
#define FAST_OVERSAMPLE 4
#define FAST_MIN_US     40
static bool     adc_ok = false;

static uint32_t last_status_ms = 0;
static uint32_t last_drdy_reads = 0;

// Re-probe cadence while the ADC has not been found — see reprobeAdc().
static const uint32_t ADC_REPROBE_MS = 1000;
static uint32_t last_probe_ms = 0;

// `hold` parks a bus line at a DC level for meter work. While set, the SPI
// peripheral is off and nothing may touch the bus — the re-probe and the
// sampler both stand down until `hold off`.
static bool bus_held = false;

// Is anything actively driving `pin`?
//
// A push-pull output beats the H7's ~40 k internal pull, so a level that
// FOLLOWS the pull in both directions means nothing is driving: an open wire,
// or a pad with no supply behind it. This is a static measurement — no timing,
// nothing to alias — which is why it is the only style of check that has held
// up on this bench. Leaves the pin as a plain INPUT.
static bool pinFloating(int pin) {
    pinMode(pin, INPUT_PULLUP);
    delayMicroseconds(200);
    const int with_pu = digitalRead(pin);
    pinMode(pin, INPUT_PULLDOWN);
    delayMicroseconds(200);
    const int with_pd = digitalRead(pin);
    pinMode(pin, INPUT);
    return (with_pu == HIGH && with_pd == LOW);
}

// ── Bit-banged SPI, for `bitbang` ────────────────────────────────────────
// The Mid Carrier's SPI1 pins, driven as plain GPIO. Same nets as the memo's
// Step 1 table: PI_1 -> J6[5], PC_3 -> J6[2], PC_2 <- J6[7].
//
// ARDUINO PIN NUMBERS, NOT PinName CONSTANTS -- this distinction is load
// bearing. `digitalWrite(PI_1, HIGH)` through the core's PinName overload
// (overloads.h:24) silently fails to drive this pin: measured 0 V at D9 while
// PA_8, PC_2 and PC_3 on that same overload all drive correctly. The integer
// path goes through the variant's pin table instead and drives D9 properly --
// confirmed with a meter, 3.1 V, in an alternating D6..D9 pattern.
//
// That bug cost a full round of diagnosis: `bitbang` produced no clock edges
// at all and the zero response was read as "SCLK never reaches the ADC",
// which nearly led to re-terminating a wire that was fine. The SPI peripheral
// clocks PI_1 correctly (0.8 V averaged under `clocktest`); only this path
// was broken. Do not "tidy" these back into PinName constants.
static const int BB_SCLK = 9;    // PI_1  D9  SPI1 CK
static const int BB_COPI = 8;    // PC_3  D8  SPI1 COPI
static const int BB_CIPO = 10;   // PC_2  D10 SPI1 CIPO
static const int BB_CS   = 6;    // PA_8  D6  PWM_0, CS as GPIO
static const int BB_DRDY = 5;    // PC_6  D5  PWM_1, DRDY in

// Shift one 6-word frame by hand, mirroring transferFrame()'s wire format:
// command in word 0, MSB-aligned, everything else zero (the driver sends no
// input CRC, so the device is not expecting one).
//
// SPI mode 1 (CPOL=0, CPHA=1): SCLK idles low, DOUT is launched on the rising
// edge and DIN is latched on the falling edge (§8.5.1). So COPI is set before
// the rising edge and CIPO is sampled just after the falling one.
static void bbFrame(uint16_t cmd, uint32_t *out_words, uint32_t &ones) {
    uint8_t tx[ADS131M04_FRAME_BYTES];
    for (size_t i = 0; i < sizeof(tx); i++) tx[i] = 0;
    tx[0] = (uint8_t)(cmd >> 8);
    tx[1] = (uint8_t)(cmd & 0xFF);

    digitalWrite(BB_CS, LOW);
    delayMicroseconds(5);                        // >= t_d(CSSC)

    for (uint8_t w = 0; w < ADS131M04_FRAME_WORDS; w++) {
        uint32_t v = 0;
        for (uint8_t b = 0; b < 8 * ADS131M04_WORD_BYTES; b++) {
            const size_t i = (size_t)w * 8 * ADS131M04_WORD_BYTES + b;
            digitalWrite(BB_COPI, ((tx[i >> 3] >> (7 - (i & 7))) & 1) ? HIGH : LOW);
            digitalWrite(BB_SCLK, HIGH);
            delayMicroseconds(2);
            digitalWrite(BB_SCLK, LOW);
            delayMicroseconds(1);
            const int rb = digitalRead(BB_CIPO);
            v = (v << 1) | ((rb == HIGH) ? 1u : 0u);
            if (rb == HIGH) ones++;
            delayMicroseconds(1);
        }
        out_words[w] = v;
    }

    digitalWrite(BB_CS, HIGH);        // >= t_d(SCCS)
    delayMicroseconds(5);
}

// Drive `pin` both ways, read the pad back, then release it and see whether
// anything external holds it. Used by `pintest`.
//
// The readback while driving depends on the core reading the pad register
// rather than the output latch, which is why `pintest` runs /CS first as a
// control: /CS is known to reach the EVM, so if IT reports cleanly and another
// line does not, the difference is real.
static void reportPin(const __FlashStringHelper *name, int pin) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, HIGH);
    delayMicroseconds(200);
    const int rd_hi = digitalRead(pin);
    digitalWrite(pin, LOW);
    delayMicroseconds(200);
    const int rd_lo = digitalRead(pin);

    pinMode(pin, INPUT);
    const bool flt  = pinFloating(pin);
    const int  idle = digitalRead(pin);

    Serial.print(F("[PIN] "));         Serial.print(name);
    Serial.print(F(" drive_hi="));     Serial.print(rd_hi == HIGH ? F("H") : F("L"));
    Serial.print(F(" drive_lo="));     Serial.print(rd_lo == HIGH ? F("H") : F("L"));
    Serial.print(F(" released="));
    if (flt) Serial.println(F("floating"));
    else {
        Serial.print(F("held-"));
        Serial.println(idle == HIGH ? F("HIGH") : F("LOW"));
    }
}

// Count DRDY edges by polling for `win_ms`.
//
// NOT interrupt-driven, deliberately. An EXTI on this pin storms: when data is
// never read DRDY only deasserts "briefly" before each conversion (§8.4.3.1),
// and the edges of a sub-microsecond pulse ring enough to swamp the ISR and
// starve the main loop — tried it, the board stopped answering commands.
//
// The cost of polling is that the count is NOT a rate: a ~4.5 us sample period
// against a sub-us pulse catches a random, beat-dependent subset, so counts
// vary several-fold between identical runs. Use this only for the question it
// answers reliably — is the line moving AT ALL — never to compare rates.
static uint32_t drdyEdges(uint32_t win_ms) {
    const uint32_t t0 = millis();
    uint32_t edges = 0;
    bool last = adc.dataReadyPin();
    while (millis() - t0 < win_ms) {
        const bool now = adc.dataReadyPin();
        if (now != last) { edges++; last = now; }
    }
    return edges;
}

// ══════════════════════════════════════════════════════════════════════════
//  Transport
// ══════════════════════════════════════════════════════════════════════════
#if H7_TRANSPORT_UDP
static IPAddress      udp_h7_ip(169, 254, 245, 50);   // H7 static (direct link)
static const uint16_t udp_local_port = 7777;
static EthernetUDP    udp;
static IPAddress      udp_pc_ip;
static uint16_t       udp_pc_port = 0;
static bool           udp_on = false;
#endif

/** Sample-stream emitter: UDP when armed, else USB-CDC. Text never comes here. */
static void streamWrite(const uint8_t *buf, size_t len) {
    if (!len) return;
#if H7_TRANSPORT_UDP
    if (udp_on) {
        udp.beginPacket(udp_pc_ip, udp_pc_port);
        udp.write(buf, len);
        udp.endPacket();                  // non-blocking — the whole point
        return;
    }
#endif
    const size_t w = Serial.write(buf, len);
    if (w < len) tx_drop += (uint32_t)(len - w);
}

// ══════════════════════════════════════════════════════════════════════════
//  Sampling
// ══════════════════════════════════════════════════════════════════════════
static char   batch[4096];
static size_t batch_off = 0;

static void flushBatch() {
    if (batch_off) { streamWrite((const uint8_t *)batch, batch_off); batch_off = 0; }
}

/** Format one channel as the PRODUCTION wire line and append to the batch. */
static void emitSample(uint8_t src, int32_t raw, float volts,
                       uint32_t hw_us, uint32_t t_ms) {
    if (!emit_on) {                 // count it, but do no formatting or TX
        seq_per_src[src & 7]++;
        samples_out++;
        return;
    }
    if (batch_off >= batch_flush_bytes || sizeof(batch) - batch_off < 96) flushBatch();

    // printf %f is not linked on nano newlib — sign + integer + 6-digit
    // fraction by hand, exactly as the production firmware does.
    const bool  neg = volts < 0.0f;
    const float av  = neg ? -volts : volts;
    unsigned long ip = (unsigned long)av;
    unsigned long fp = (unsigned long)((av - (float)ip) * 1000000.0f + 0.5f);
    if (fp >= 1000000UL) { ip++; fp -= 1000000UL; }

    const int w = snprintf(batch + batch_off, sizeof(batch) - batch_off,
                           "%lu\t%u\t%ld\t%s%lu.%06lu\t%lu\t%lu\r\n",
                           (unsigned long)t_ms, (unsigned)src, (long)raw,
                           neg ? "-" : "", ip, fp,
                           (unsigned long)hw_us,
                           (unsigned long)seq_per_src[src & 7]);
    if (w > 0) batch_off += (size_t)w;
    seq_per_src[src & 7]++;
    samples_out++;
}

static void readAndEmit() {
    ADS131M04_Reading r;
    if (!adc.readChannels(r)) return;      // CRC failed; driver counted it
    drdy_reads++;
    const uint32_t t_ms = millis();
    for (uint8_t ch = 0; ch < ADS131M04_NUM_CH; ch++)
        emitSample(SRC_FOR_CH[ch], r.raw[ch], r.volts[ch], r.hw_us, t_ms);
}

// ══════════════════════════════════════════════════════════════════════════
//  Self-test — T1 (ID) and T2 (register round-trip), plan §7
// ══════════════════════════════════════════════════════════════════════════
static bool selftest() {
    const uint16_t id = adc.deviceID();
    const bool t1 = ((id & ADS131M04_ID_MASK) == ADS131M04_ID_EXPECTED);
    Serial.print(t1 ? F("[T1] PASS: id=0x") : F("[T1] FAIL: id=0x"));
    Serial.println(id, HEX);
    if (!t1) {
        Serial.println(F("[T1]   0x0000/0xFFFF => SPI link dead (check Cable 1)"));
        Serial.println(F("[T1]   plausible-but-wrong => check EVM JP6 fitted, JP5 NOT fitted"));
        Serial.println(F("[T1]   CLKIN absent => register reads still work, conversions never come"));
    }

    // T2 exercises the two things this protocol most plausibly gets wrong:
    // the WREG payload slot (§4.5) and the one-frame response lag (§4.2).
    bool t2 = true;
    static const uint16_t probes[] = {0x0F1A, 0x0F0E, 0x0F16, 0x0F1A};
    for (uint8_t i = 0; i < 4; i++) {
        if (!adc.writeRegister(ADS131M04_REG_CLOCK, probes[i])) {
            Serial.print(F("[T2] FAIL: WREG not acked for 0x"));
            Serial.println(probes[i], HEX);
            t2 = false; continue;
        }
        const uint16_t rb = adc.readRegister(ADS131M04_REG_CLOCK);
        if ((rb & 0x0F3F) != (probes[i] & 0x0F3F)) {
            Serial.print(F("[T2] FAIL: wrote 0x")); Serial.print(probes[i], HEX);
            Serial.print(F(" read 0x"));            Serial.println(rb, HEX);
            t2 = false;
        }
    }
    // GAIN1 too — different bit packing (4-bit stride, 3-bit fields), so it
    // exercises a different shift path than CLOCK does.
    if (!adc.setGain(1, ADS131M04_GAIN_2) || adc.getGain(1) != ADS131M04_GAIN_2) {
        Serial.println(F("[T2] FAIL: GAIN1 ch1 round-trip"));
        t2 = false;
    }
    adc.setGain(1, ADS131M04_GAIN_1);

    if (t2) Serial.println(F("[T2] PASS: register round-trip"));
    return t1 && t2;
}

// ══════════════════════════════════════════════════════════════════════════
//  Commands — driven by the host sweep, not typed
// ══════════════════════════════════════════════════════════════════════════
static void help() {
    Serial.println(F("[CMD] selftest | regs | rst | spi <hz> | osr <0-7> | gain <ch> <1..128>"));
    Serial.println(F("[CMD] mux <ch|all> <0=ain 1=short 2=test+ 3=test-> (T8)"));
    Serial.println(F("[CMD] poll <us> | fast <us|auto> | dly <us> | batch <n> | emit <0|1> | drdy | raw [n] | rawx [n] | wtest <a> <v> | drdyscan | cipotest | bitbang | pintest | hold <line> <0|1> | walk | clocktest <s> | xtalk | netcfg <ip> <port> | ping | help"));
}

// Keep the oversampled poll period tied to the configured data rate.
//
// The device loses ~25 % of conversions in a way nothing host-side influences
// (see STATUS.md). Reading several times per conversion and keeping only the
// frames whose STATUS[3:0] flags fresh data recovers a clean, duplicate-free
// stream; a fixed period would silently stop oversampling as soon as someone
// changed the OSR, so derive it.
static void updateFastPeriod() {
    if (!fast_auto) return;
    const float sps = adc.sps();
    if (sps <= 0.0f) return;
    uint32_t us = (uint32_t)(1000000.0f / sps / FAST_OVERSAMPLE);
    if (us < FAST_MIN_US) us = FAST_MIN_US;
    fast_us = us;
}

static void applyConfig(ADS131M04_OSR_t osr) {
    if (!adc.configure(osr)) Serial.println(F("[CFG] FAIL: configure() rejected"));
    else {
        Serial.print(F("[CFG] osr=")); Serial.print(adc.osrDivisor());
        Serial.print(F(" rate="));     Serial.print(adc.sps(), 2);
        Serial.println(F(" SPS"));
    }
    updateFastPeriod();
    Serial.print(F("[CFG] fast_us=")); Serial.print(fast_us);
    Serial.print(F(" ("));             Serial.print(FAST_OVERSAMPLE);
    Serial.println(F("x oversampled, de-duplicated on STATUS[3:0])"));
}

static void handleCommand(String in) {
    in.trim();
    if (!in.length()) return;
    String low = in; low.toLowerCase();

    if (low == "ping") return;                       // heartbeat, no reply needed
    if (low == "help") { help(); return; }
    if (low == "selftest") { selftest(); return; }
    if (low == "regs") { adc.printRegisters(Serial); adc.printConfig(Serial); return; }

    if (low == "rst") {
        // T9. Hardware SYNC/RESET, held 1 ms — comfortably past t_w(RSL)'s
        // 2048 t_CLKIN (250 us), because a SHORTER pulse is a SYNC and would
        // silently leave the configuration intact while looking like a reset.
        // Registers return to defaults, so the caller must re-apply.
        const uint16_t clock_before = adc.readRegister(ADS131M04_REG_CLOCK);
        const bool ok = adc.reset();
        const uint16_t st    = adc.readRegister(ADS131M04_REG_STATUS);
        const uint16_t clock = adc.readRegister(ADS131M04_REG_CLOCK);

        Serial.print(F("[RST] ")); Serial.print(ok ? F("OK id=0x") : F("FAILED id=0x"));
        Serial.print(adc.deviceID(), HEX);
        Serial.print(F(" status=0x"));       Serial.print(st, HEX);
        Serial.print(F(" reset_bit="));      Serial.print((st & ADS131M04_ST_RESET) ? 1 : 0);
        Serial.print(F(" clock 0x"));        Serial.print(clock_before, HEX);
        Serial.print(F("->0x"));             Serial.println(clock, HEX);
        // A real reset returns CLOCK to its 0x0F0E default. If it still reads
        // the configured value, the pulse was a SYNC, not a reset.
        if (ok && clock != 0x0F0E)
            Serial.println(F("[RST] WARNING: CLOCK not at reset default — pulse may have been a SYNC"));

        adc_ok = ok;
        if (ok) applyConfig(OSR_DEFAULT);
        return;
    }

    if (low.startsWith("mux ")) {
        // T8. The internal DC test signal is 2/15 x FSR and auto-scales with
        // gain, so it needs no external hardware and exists in both polarities
        // — which is what makes it test SIGN EXTENSION, not just scaling.
        String rest = in.substring(4); rest.trim();
        const int sp = rest.indexOf(' ');
        if (sp < 0) { Serial.println(F("[CFG] usage: mux <ch|all> <0..3>")); return; }
        const String chStr = rest.substring(0, sp);
        const int m = rest.substring(sp + 1).toInt();
        if (m < 0 || m > 3) { Serial.println(F("[CFG] bad mux (0..3)")); return; }

        const bool all = chStr.equalsIgnoreCase("all");
        const int ch0 = all ? 0 : chStr.toInt();
        const int ch1 = all ? ADS131M04_NUM_CH - 1 : ch0;
        if (ch0 < 0 || ch1 >= ADS131M04_NUM_CH) { Serial.println(F("[CFG] bad ch")); return; }

        for (int ch = ch0; ch <= ch1; ch++) {
            if (!adc.setInputMux((uint8_t)ch, (ADS131M04_Mux_t)m)) {
                Serial.print(F("[CFG] FAIL: setInputMux ch")); Serial.println(ch);
                continue;
            }
            Serial.print(F("[CFG] ch")); Serial.print(ch);
            Serial.print(F(" mux=")); Serial.print(m);
            Serial.print(F(" expect=")); Serial.print(adc.expectedVolts((uint8_t)ch) * 1000.0f, 4);
            Serial.println(F(" mV"));
        }
        return;
    }

    if (low.startsWith("spi ")) {
        const long hz = in.substring(4).toInt();
        if (hz < 100000 || hz > (long)ADS131M04_SCLK_MAX_HZ) {
            Serial.println(F("[CFG] bad spi (100k..25M)")); return;
        }
        adc.setSpiHz((uint32_t)hz);
        adc.resetCounters();                 // T3 judges THIS clock, not history
        Serial.print(F("[CFG] spi=")); Serial.println(adc.spiHz());
        return;
    }

    if (low.startsWith("osr ")) {
        const int code = in.substring(4).toInt();
        if (code < 0 || code > 7) { Serial.println(F("[CFG] bad osr (0..7)")); return; }
        applyConfig((ADS131M04_OSR_t)code);
        return;
    }

    if (low.startsWith("gain ")) {
        String rest = in.substring(5); rest.trim();
        const int sp = rest.indexOf(' ');
        if (sp < 0) { Serial.println(F("[CFG] usage: gain <ch> <1..128>")); return; }
        const int ch = rest.substring(0, sp).toInt();
        const int g  = rest.substring(sp + 1).toInt();
        int code = -1;
        for (int i = 0; i < 8; i++) if ((1 << i) == g) code = i;
        if (ch < 0 || ch >= ADS131M04_NUM_CH || code < 0) {
            Serial.println(F("[CFG] bad gain args")); return;
        }
        if (!adc.setGain((uint8_t)ch, (ADS131M04_Gain_t)code))
            Serial.println(F("[CFG] FAIL: setGain rejected"));
        else {
            Serial.print(F("[CFG] ch")); Serial.print(ch);
            Serial.print(F(" gain=")); Serial.print(g);
            Serial.print(F(" fsr=+/-")); Serial.print(adc.fsrVolts((uint8_t)ch), 4);
            Serial.println(F("V"));
        }
        return;
    }

    if (low.startsWith("fast")) {
        const String a = low.substring(4);
        if (a.indexOf("auto") >= 0) {
            fast_auto = true;
            updateFastPeriod();
        } else {
            const long us = a.toInt();
            fast_auto = false;
            fast_us = (us > 0 && us <= 100000) ? (uint32_t)us : 0;
        }
        Serial.print(F("[CFG] fast_us=")); Serial.println(fast_us);
        if (fast_us) {
            Serial.println(F("[CFG]   read every fast_us, keep CRC-good frames"));
            Serial.println(F("[CFG]   whose STATUS[3:0] flags a fresh conversion"));
        } else {
            Serial.println(F("[CFG]   0 = back to one DRDY-gated read per conv"));
        }
        adc.resetCounters();
        dedup_skipped = 0;
        Serial.println(F("[CFG] counters cleared"));
        return;
    }

    if (low.startsWith("batch")) {
        const long n = low.substring(5).toInt();
        if (n >= 120 && n <= 1400) batch_flush_bytes = (size_t)n;
        Serial.print(F("[CFG] batch_flush_bytes="));
        Serial.println((unsigned long)batch_flush_bytes);
        adc.resetCounters();
        Serial.println(F("[CFG] frame/crc counters cleared"));
        return;
    }

    if (low.startsWith("emit")) {
        const String a = low.substring(4);
        emit_on = !(a.indexOf('0') >= 0);
        Serial.print(F("[CFG] emit=")); Serial.println(emit_on ? 1 : 0);
        Serial.println(F("[CFG]   emit 0 = read frames but do NOT format or"));
        Serial.println(F("[CFG]   transmit them; isolates the emit path"));
        adc.resetCounters();
        Serial.println(F("[CFG] frame/crc counters cleared"));
        return;
    }

    if (low.startsWith("dly")) {
        const long us = low.substring(3).toInt();
        if (us >= 0 && us <= 100000) drdy_delay_us = (uint32_t)us;
        Serial.print(F("[CFG] drdy_delay_us=")); Serial.println(drdy_delay_us);
        Serial.println(F("[CFG]   0 = read the instant DRDY asserts (original)"));
        Serial.println(F("[CFG]   >0 = step off the conversion boundary first"));
        adc.resetCounters();
        Serial.println(F("[CFG] frame/crc counters cleared for a clean sweep"));
        return;
    }

    if (low.startsWith("poll")) {                    // blind timed polling (A/B)
        const long us = (low.length() > 4) ? in.substring(5).toInt() : 0;
        if (us > 0) poll_us = (uint32_t)us;
        drdy_gated = false;
        Serial.print(F("[CFG] timed poll, period_us=")); Serial.println(poll_us);
        return;
    }
    if (low == "drdy") {
        drdy_gated = true;
        Serial.println(F("[CFG] DRDY-gated sampling (default)"));
        return;
    }

    // ── drdyscan ─────────────────────────────────────────────────────────
    // Read the DRDY pin as a pin, with no SPI involved. This is the only
    // bench check that separates "the ADC is running and we cannot hear it"
    // from "the ADC is not running at all" without a scope, because DRDY is
    // the one ADC output whose meaning does not depend on the CIPO path.
    //
    // Deliberately a separate command from `drdy`, which sets a sampling
    // mode — a diagnostic should not change how the rig samples.
    if (low == "drdyscan") {
        // Without this, every verdict below could just be reading our own
        // internal resistor on an unconnected pin.
        const bool floating = pinFloating(BB_DRDY);

        const uint32_t edges = drdyEdges(500);
        const bool level_low = adc.dataReadyPin();     // true == asserted (LOW)

        Serial.print(F("[DRDY] edges_500ms="));  Serial.print(edges);
        Serial.print(F(" level="));              Serial.print(level_low ? F("LOW") : F("HIGH"));
        Serial.print(F(" driven="));             Serial.println(floating ? F("NO") : F("yes"));

        if (floating) {
            Serial.println(F("[DRDY]   FLOATING -- nothing drives PC_6. Check the"));
            Serial.println(F("[DRDY]   J6[6] wire first; DVDD-less pads also float."));
        } else if (edges > 0) {
            Serial.println(F("[DRDY]   TOGGLING -- ADC is powered, clocked and converting."));
            // This proves the ADC is alive. It does NOT prove our frames reach
            // it: with data never read, DRDY pulses on its own schedule
            // whether or not CS/SCLK/DIN land. Run `outbound` to settle that.
            Serial.println(F("[DRDY]   Says nothing yet about CS/SCLK/DIN -- run `cipotest`."));
            Serial.println(F("[DRDY]   (edge COUNT is aliased, not a rate -- do not compare)"));
        } else if (level_low) {
            Serial.println(F("[DRDY]   STUCK LOW -- converted at least once, then stopped."));
            Serial.println(F("[DRDY]   CLKIN died after POR, or AVDD sagged (check TP2)."));
        } else {
            Serial.println(F("[DRDY]   STUCK HIGH -- no conversion has ever completed."));
            Serial.println(F("[DRDY]   CLKIN absent (JP6/JP5), AVDD absent (check TP2),"));
            Serial.println(F("[DRDY]   or the part is not alive."));
        }
        return;
    }

    // ── cipotest ─────────────────────────────────────────────────────────
    // Is the ADC driving DOUT, and does that reach PC_2?
    //
    // Static, deliberately. Every timing-based check on this bench proved
    // untrustworthy: the DRDY deassert pulse is sub-microsecond, a polling
    // loop aliases it, and the resulting counts swing several-fold run to run.
    // This measures a LEVEL instead. DOUT is high-impedance whenever CS is
    // high and actively driven whenever CS is low (t_p(CSDO) / t_p(CSDOZ),
    // §6.7) — so toggling CS by hand and asking "is anything driving PC_2"
    // answers the CIPO question with no clocking, no frames, nothing to alias.
    //
    // CS-high is the built-in control and it MUST read floating. If it does
    // not, this method is not measuring what it claims and the CS-low result
    // is meaningless — say so rather than reporting a verdict.
    if (low == "cipotest") {
        const int CIPO_PIN = BB_CIPO;                // D10 / PC_2 -> EVM J6[7]

        adc.busRelease();                            // free PC_2 from SPI1 AF

        digitalWrite(BB_CS, HIGH);
        delayMicroseconds(500);
        const bool float_cs_hi = pinFloating(CIPO_PIN);

        digitalWrite(BB_CS, LOW);
        delayMicroseconds(500);
        const bool float_cs_lo = pinFloating(CIPO_PIN);
        const int  lvl_cs_lo   = digitalRead(CIPO_PIN);

        digitalWrite(BB_CS, HIGH);
        adc.busAcquire();                            // restore bus AND mode

        Serial.print(F("[CIPO] cs_high="));
        Serial.print(float_cs_hi ? F("floating") : F("driven"));
        Serial.print(F(" cs_low="));
        Serial.print(float_cs_lo ? F("floating") : F("driven"));
        Serial.print(F(" level_cs_low="));
        Serial.println(lvl_cs_lo == HIGH ? F("HIGH") : F("LOW"));

        if (!float_cs_hi) {
            Serial.println(F("[CIPO]   CONTROL FAILED -- PC_2 reads driven even with CS"));
            Serial.println(F("[CIPO]   high, where DOUT must be high-Z. Something else is"));
            Serial.println(F("[CIPO]   holding the line: M4 not idled, or a short. The"));
            Serial.println(F("[CIPO]   CS-low result below cannot be trusted."));
        } else if (!float_cs_lo) {
            Serial.println(F("[CIPO]   WIRE GOOD -- the ADC drives DOUT when selected and"));
            Serial.println(F("[CIPO]   it reaches PC_2. So J6[7] and /CS are both fine and"));
            Serial.println(F("[CIPO]   the fault is in clocking: SCLK not reaching J6[5],"));
            Serial.println(F("[CIPO]   or SPI mode/bit order, not the harness."));
        } else {
            Serial.println(F("[CIPO]   NOT DRIVEN -- PC_2 floats even with CS asserted."));
            Serial.println(F("[CIPO]   Either J6[7] -> PC_2 is open, or /CS is not reaching"));
            Serial.println(F("[CIPO]   J6[4] so the ADC never leaves high-Z. Ohm both."));
        }
        return;
    }

    // ── bitbang ──────────────────────────────────────────────────────────
    // Read the ID register with the SPI peripheral switched OFF, clocking
    // PI_1 by hand.
    //
    // cipotest proved /CS and J6[7] are good and that DOUT sits driven-LOW
    // when selected -- exactly how an ADS131M04 looks when it is never
    // clocked. This is the test that says whether SCLK actually arrives,
    // and it splits the two remaining failures cleanly:
    //
    //   DOUT shifts  -> the harness carries SCLK and CIPO. The fault is the
    //                   H7's hardware SPI setup, and it is a firmware fix.
    //   DOUT frozen  -> SCLK is genuinely not reaching J6[5]. Soldering iron.
    //
    // Two frames because the response lags one frame (§8.5.1.2): frame 1
    // sends RREG(ID), frame 2 sends NULL and carries the answer back.
    if (low == "bitbang") {
        adc.busRelease();                            // release PI_1/PC_2/PC_3

        pinMode(BB_SCLK, OUTPUT); digitalWrite(BB_SCLK, LOW);   // mode 1 idles low
        pinMode(BB_COPI, OUTPUT); digitalWrite(BB_COPI, LOW);
        pinMode(BB_CIPO, INPUT);
        digitalWrite(BB_CS, HIGH);

        // A full conversation, not one read. The response lags a frame
        // (§8.5.1.2), so every command needs a following NULL to collect its
        // answer, and RESET first puts the device in a known state -- its
        // FF24h acknowledge is the one response with a documented exact value,
        // which makes it the only self-checking step in the sequence.
        static const struct { uint16_t cmd; const char *name; } SEQ[] = {
            { ADS131M04_CMD_RESET,  "RESET " },
            { ADS131M04_CMD_NULL,   "null  " },      // expect FF24
            { ADS131M04_CMD_UNLOCK, "UNLOCK" },
            { ADS131M04_CMD_NULL,   "null  " },      // expect 0655 echo
            { 0xA000,               "RREG0 " },
            { ADS131M04_CMD_NULL,   "null  " },      // expect ID 24xx
        };
        const uint8_t NSEQ = sizeof(SEQ) / sizeof(SEQ[0]);

        uint32_t ones = 0;
        uint32_t f[ADS131M04_FRAME_WORDS];
        uint16_t resp[8] = {0};

        for (uint8_t s = 0; s < NSEQ; s++) {
            bbFrame(SEQ[s].cmd, f, ones);
            resp[s] = (uint16_t)((f[0] >> 8) & 0xFFFF);   // word is MSB-aligned
            Serial.print(F("[BB] "));      Serial.print(SEQ[s].name);
            Serial.print(F(" tx=0x"));     Serial.print(SEQ[s].cmd, HEX);
            Serial.print(F("  rx:"));
            for (uint8_t w = 0; w < ADS131M04_FRAME_WORDS; w++) {
                Serial.print(F(" ")); Serial.print(f[w], HEX);
            }
            Serial.println();
            delayMicroseconds(50);
        }

        adc.busAcquire();                            // restore bus AND mode

        const uint16_t id = resp[5];
        Serial.print(F("[BB] reset_ack=0x")); Serial.print(resp[1], HEX);
        Serial.print(F(" (want FF24)  unlock_ack=0x")); Serial.print(resp[3], HEX);
        Serial.print(F(" (want 655)  id=0x"));        Serial.print(id, HEX);
        Serial.print(F(" ones="));        Serial.print(ones);
        Serial.print(F("/"));             Serial.println((uint32_t)NSEQ * 8 * ADS131M04_FRAME_BYTES);

        if (ones == 0) {
            Serial.println(F("[BB]   DOUT FROZEN -- not one bit came back high across"));
            Serial.println(F("[BB]   288 hand-clocked edges. SCLK is not reaching the"));
            Serial.println(F("[BB]   ADC: ohm J15-20 (PI_1) -> J6[5]."));
        } else if ((id & ADS131M04_ID_MASK) == ADS131M04_ID_EXPECTED) {
            Serial.println(F("[BB]   ID READ OK -- the harness is fine end to end and the"));
            Serial.println(F("[BB]   ADC answers. The fault is the H7 hardware SPI setup,"));
            Serial.println(F("[BB]   not the wiring. Compare against the driver's SPI pins."));
        } else {
            Serial.println(F("[BB]   DOUT SHIFTS but the ID is wrong -- SCLK does reach the"));
            Serial.println(F("[BB]   ADC, so the harness carries all four SPI lines. Suspect"));
            Serial.println(F("[BB]   framing: bit order, word length, or CS timing."));
        }
        return;
    }

    // ── hold ─────────────────────────────────────────────────────────────
    // Park one bus line at a DC level so a multimeter can find the break.
    //
    // In normal operation the bus is active ~150 us per second (one re-probe
    // register read), a 0.015% duty cycle, so a meter reads nothing but the
    // idle level whether the wire is connected or not. Parking the line makes
    // it a DC measurement, which is the one thing a DMM is actually good at:
    //
    //   hold sclk 1   then measure EVM J6[5] against GND
    //     ~3.0 V at the EVM end  -> the wire carries
    //     0 V at the EVM end, 3.0 V at J15-20 -> the wire is open
    //
    // One probe, at the far end, no need to reach both ends at once.
    // ── raw ──────────────────────────────────────────────────────────────
    // Dump whole DOUT frames as they arrived, with BOTH CRC values.
    //
    // Why this exists: a CRC mismatch is undiagnosable from the decoded fields.
    // `crc_err` counts failures but throws away the evidence, so it cannot
    // distinguish
    //     (a) bits corrupted on the wire      -> data looks wrong, CRCs differ
    //     (b) an intact frame our CRC rejects -> data looks sane, CRCs differ
    // and those have completely different fixes. Printing crc_rx, crc_calc and
    // the 18 bytes side by side settles it in one look.
    //
    // Frames are taken back to back so the sample is contiguous, and BAD ones
    // are marked, so a run of good frames punctuated by bad ones is visible as
    // a pattern rather than as a rate.
    // ── rawx [n] ─────────────────────────────────────────────────────────
    // FRAME-LENGTH TEST. Clock SEVEN words instead of six and ask where the
    // output CRC actually lands.
    //
    // Every frame we currently call corrupt has words 1-4 correct and then the
    // NEXT frame's STATUS where the CRC belongs. That is exactly what clocking
    // six words would produce if the device sometimes emits seven — §8.5.1.10.8
    // warns to "ensure all of the ADC data and output CRC are shifted out", and
    // Figure 8-25 shows frames extended past six words.
    //
    // Three checks per frame, which between them separate the possibilities:
    //   crc(w0..w4) == w5  -> ordinary 6-word frame, nothing wrong
    //   crc(w0..w4) == w6  -> a word was INSERTED before the CRC
    //   crc(w0..w5) == w6  -> the frame is genuinely 7 words this time
    if (low.startsWith("rawx")) {
        long n = low.substring(4).toInt();
        if (n <= 0 || n > 40) n = 16;
        if (bus_held) { adc.busAcquire(); bus_held = false; }

        const size_t W = 7;
        const size_t LEN = W * ADS131M04_WORD_BYTES;      // 21 bytes
        static uint8_t tx[7 * ADS131M04_WORD_BYTES];
        static uint8_t cap[40][7 * ADS131M04_WORD_BYTES];
        memset(tx, 0, sizeof(tx));                        // NULL command

        for (long k = 0; k < n; k++) adc.transferRaw(tx, cap[k], LEN);

        Serial.print(F("[RAWX] ")); Serial.print(n);
        Serial.println(F(" x 7-word frames -- where does the CRC land?"));
        uint32_t at5 = 0, at6 = 0, at6long = 0, none = 0;
        for (long k = 0; k < n; k++) {
            const uint8_t *r = cap[k];
            const uint16_t w5 = (uint16_t)(r[15] << 8 | r[16]);
            const uint16_t w6 = (uint16_t)(r[18] << 8 | r[19]);
            const uint16_t c5 = ADS131M04_Driver::crc16(r, 15);
            const uint16_t c6 = ADS131M04_Driver::crc16(r, 18);
            const char *verdict = "none";
            if (c5 == w5)      { verdict = "crc@w5 (normal)";      at5++; }
            else if (c5 == w6) { verdict = "crc@w6 INSERTED WORD"; at6++; }
            else if (c6 == w6) { verdict = "crc@w6 7-WORD FRAME";  at6long++; }
            else               { none++; }

            Serial.print(F("[RAWX] "));
            for (size_t b = 0; b < LEN; b++) {
                if (b && (b % ADS131M04_WORD_BYTES) == 0) Serial.print(' ');
                if (r[b] < 0x10) Serial.print('0');
                Serial.print(r[b], HEX);
            }
            Serial.print(F("  ")); Serial.println(verdict);
        }
        Serial.print(F("[RAWX] normal@w5=")); Serial.print(at5);
        Serial.print(F(" inserted@w6="));     Serial.print(at6);
        Serial.print(F(" sevenword@w6="));    Serial.print(at6long);
        Serial.print(F(" unexplained="));     Serial.println(none);
        return;
    }

    if (low.startsWith("raw")) {
        long n = low.substring(3).toInt();
        if (n <= 0 || n > 64) n = 8;

        if (bus_held) { adc.busAcquire(); bus_held = false; }

        Serial.print(F("[RAW] ")); Serial.print(n);
        Serial.print(F(" frames @ ")); Serial.print(adc.spiHz());
        Serial.println(F(" Hz -- crc_rx is the CHIP's, crc_calc is OURS"));

        // CAPTURE FIRST, PRINT AFTER. Printing between frames inserts ~1 ms of
        // dead time, during which several conversions complete unread -- and
        // §8.5.1.9.1 says the two-deep output FIFO fills when data "are not read
        // for a period of time", after which DRDY and the frame structure stop
        // behaving predictably. A dump taken with gaps in it is therefore
        // measuring the gaps, not the link. These frames are contiguous.
        static ADS131M04_Frame cap[64];
        for (long k = 0; k < n; k++) {
            adc.transferFrame(ADS131M04_CMD_NULL, nullptr, 0, cap[k]);
        }

        uint32_t bad = 0;
        for (long k = 0; k < n; k++) {
            const ADS131M04_Frame &f = cap[k];
            if (!f.crc_ok) bad++;

            Serial.print(F("[RAW] "));
            Serial.print(f.crc_ok ? F("ok  ") : F("BAD "));
            Serial.print(F("rx=0x"));   Serial.print(f.crc_rx, HEX);
            Serial.print(F(" calc=0x")); Serial.print(f.crc_calc, HEX);
            Serial.print(F("  ["));
            for (size_t i = 0; i < ADS131M04_FRAME_BYTES; i++) {
                if (i && (i % ADS131M04_WORD_BYTES) == 0) Serial.print(' ');
                if (f.rx[i] < 0x10) Serial.print('0');
                Serial.print(f.rx[i], HEX);
            }
            Serial.println(']');
        }
        Serial.print(F("[RAW] bad=")); Serial.print(bad);
        Serial.print('/');             Serial.println(n);
        Serial.println(F("[RAW] word0=STATUS  word1..4=ch0..ch3  word5=CRC"));
        Serial.println(F("[RAW] sane data + mismatched CRC => OUR crc16 or its"));
        Serial.println(F("[RAW]   coverage is wrong, not the wire."));
        return;
    }

    // ── wtest <addr> <val> ───────────────────────────────────────────────
    // Show BOTH frames of a WREG exactly as they went out and came back.
    //
    // T1 (RREG) passes every time while T2 (WREG) fails every time -- 3/3
    // versus 12/12 on the bench. A shared ~25 % frame disturbance cannot do
    // that; it would break reads just as often. So reads and writes differ in
    // something specific, and the only difference in transferFrame() is the
    // payload word. This prints the transmitted command, the payload slot, and
    // the response word of both frames, so the ack can be compared against the
    // Table 8-11 form 010a aaaa ammm mmmm instead of guessed at.
    if (low.startsWith("wtest")) {
        String a = low.substring(5); a.trim();
        const int sp = a.indexOf(' ');
        if (sp < 0) {
            Serial.println(F("[WT] usage: wtest <addr_hex> <val_hex>"));
            return;
        }
        const uint8_t  addr = (uint8_t)strtoul(a.substring(0, sp).c_str(), nullptr, 16);
        const uint16_t val  = (uint16_t)strtoul(a.substring(sp + 1).c_str(), nullptr, 16);

        if (bus_held) { adc.busAcquire(); bus_held = false; }

        const uint16_t cmd    = ADS131M04_CMD_WREG | ((uint16_t)(addr & 0x3F) << 7);
        const uint16_t expect = ADS131M04_RSP_WREG | ((uint16_t)(addr & 0x3F) << 7);

        ADS131M04_Frame f1, f2, f3;
        adc.transferFrame(cmd, &val, 1, f1);                  // WREG + payload
        adc.transferFrame(ADS131M04_CMD_NULL, nullptr, 0, f2);// ack lands here
        adc.transferFrame(ADS131M04_CMD_NULL, nullptr, 0, f3);// one frame later

        Serial.print(F("[WT] addr=0x"));  Serial.print(addr, HEX);
        Serial.print(F(" val=0x"));       Serial.print(val, HEX);
        Serial.print(F("  cmd=0x"));      Serial.print(cmd, HEX);
        Serial.print(F("  want_ack=0x")); Serial.println(expect, HEX);

        const ADS131M04_Frame *fs[3] = { &f1, &f2, &f3 };
        for (int i = 0; i < 3; i++) {
            Serial.print(F("[WT] f")); Serial.print(i + 1);
            Serial.print(fs[i]->crc_ok ? F(" ok  ") : F(" BAD "));
            Serial.print(F("rsp=0x")); Serial.print(fs[i]->response, HEX);
            Serial.print(F("  ["));
            for (size_t b = 0; b < ADS131M04_FRAME_BYTES; b++) {
                if (b && (b % ADS131M04_WORD_BYTES) == 0) Serial.print(' ');
                if (fs[i]->rx[b] < 0x10) Serial.print('0');
                Serial.print(fs[i]->rx[b], HEX);
            }
            Serial.println(']');
        }
        Serial.print(F("[WT] readback 0x"));
        Serial.println(adc.readRegister(addr), HEX);
        Serial.println(F("[WT] f2.rsp == want_ack  => the write was accepted"));
        Serial.println(F("[WT] f2.rsp == 0x05xx    => STATUS, i.e. the device saw"));
        Serial.println(F("[WT]   NULL or an invalid command -- the command word"));
        Serial.println(F("[WT]   or its payload slot did not arrive (§8.5.1.10.1)"));
        return;
    }

    if (low.startsWith("hold")) {
        String arg = low.substring(4); arg.trim();

        if (arg.length() == 0 || arg == "off") {
            if (bus_held) {
                digitalWrite(BB_SCLK, LOW);
                digitalWrite(BB_COPI, LOW);
                pinMode(BB_CS, OUTPUT);
                digitalWrite(BB_CS, HIGH);
                adc.busAcquire();
                bus_held = false;
            }
            Serial.println(F("[HOLD] released -- SPI restored, re-probing resumes"));
            return;
        }

        const int sp = arg.indexOf(' ');
        if (sp < 0) {
            Serial.println(F("[HOLD] usage: hold <sclk|copi|cs> <0|1> | hold off"));
            return;
        }
        const String which = arg.substring(0, sp);
        const int    lvl   = arg.substring(sp + 1).toInt() ? HIGH : LOW;

        // `hold d<n>` parks an Arduino digital pin by NUMBER. That is not just
        // a convenience: it goes through the variant's integer pin table,
        // whereas the named forms below go through the PinName overloads in
        // the core (overloads.h). PI_1 refuses to source high via the PinName
        // path while PA_8 and PC_3 on that same path work, so running the same
        // pin down BOTH paths is the test that says whether the fault is the
        // silicon or the core's pin plumbing:
        //   `hold d9 1` drives  but `hold sclk 1` does not  -> core, fixable
        //   neither drives                                  -> the PI_1 pad
        if (which.length() >= 2 && which[0] == 'd' && isDigit(which[1])) {
            const int dn = which.substring(1).toInt();
            if (dn < 0 || dn > 14) {
                Serial.println(F("[HOLD] d<n> out of range (0..14)"));
                return;
            }
            if (!bus_held) { adc.busRelease(); bus_held = true; }
            pinMode(dn, OUTPUT);
            digitalWrite(dn, lvl);
            Serial.print(F("[HOLD] D"));  Serial.print(dn);
            Serial.print(F("="));         Serial.print(lvl == HIGH ? F("HIGH") : F("LOW"));
            Serial.println(F("  (integer pin path, not PinName)"));
            Serial.println(F("[HOLD] bus is PARKED: no probing until `hold off`."));
            return;
        }

        // Integer pins here too, for the reason spelled out at BB_SCLK: the
        // named forms used to resolve to PinName constants, and `hold sclk 1`
        // therefore reported a parked-HIGH clock that measured 0 V.
        int pin;
        const __FlashStringHelper *where;
        if      (which == "sclk") { pin = BB_SCLK;  where = F("PI_1 D9 J15-20 -> EVM J6[5]"); }
        else if (which == "copi") { pin = BB_COPI;  where = F("PC_3 D8 J15-24 -> EVM J6[2]"); }
        else if (which == "cs")   { pin = BB_CS;    where = F("PA_8 D6 J15-25 -> EVM J6[4]"); }
        else if (which == "pi0")  { pin = 7;        where = F("PI_0 D7 SPI1 CS -- unused, port-I probe"); }
        else {
            Serial.println(F("[HOLD] usage: hold <sclk|copi|cs|pi0|d<n>> <0|1> | hold off"));
            return;
        }

        if (!bus_held) { adc.busRelease(); bus_held = true; }
        pinMode(pin, OUTPUT);
        digitalWrite(pin, lvl);

        Serial.print(F("[HOLD] "));   Serial.print(which);
        Serial.print(F("="));         Serial.print(lvl == HIGH ? F("HIGH") : F("LOW"));
        Serial.print(F("  "));        Serial.println(where);
        Serial.println(F("[HOLD] measure DC at the EVM end against GND."));
        Serial.println(F("[HOLD] /CS is the control -- `hold cs 1` must read ~3 V at"));
        Serial.println(F("[HOLD] J6[4], because that wire is already proven good."));
        Serial.println(F("[HOLD] bus is PARKED: no probing until `hold off`."));
        return;
    }

    // ── walk ─────────────────────────────────────────────────────────────
    // Identify which Arduino pin a physical wire is attached to, by driving
    // each one HIGH in turn while the operator holds a meter on the wire.
    //
    // Every label-based attempt to locate SCLK has now disagreed with the
    // meter: the core says SCK is PI_1/D9, the carrier pinout says J15-20, the
    // ADS1263 header agrees with both, and the pin still reads 0 V. Rather
    // than re-derive the mapping a fourth time, let the hardware answer:
    // whichever D-number makes the meter jump IS the pin that wire is on.
    //
    // Run this from a terminal you can SEE (`pio device monitor`), because the
    // answer is the pairing of a printed label with a meter reading.
    //
    // D6 (/CS) is held HIGH throughout so the ADC stays deselected, and D10
    // (CIPO) is skipped entirely — driving it would fight the ADC's DOUT.
    if (low == "walk") {
        if (!bus_held) { adc.busRelease(); bus_held = true; }

        Serial.println(F("[WALK] Put the meter on the SCLK wire, CARRIER end, vs GND."));
        Serial.println(F("[WALK] Each pin is HIGH for 2.5 s. Note which D reads ~3 V."));
        Serial.println(F("[WALK] D6 (/CS) stays HIGH throughout; D10 (CIPO) is skipped."));

        for (int k = 0; k <= 13; k++) {
            if (k == 10) continue;
            pinMode(k, OUTPUT);
            digitalWrite(k, (k == 6) ? HIGH : LOW);
        }
        for (int d = 0; d <= 13; d++) {
            if (d == 10 || d == 6) continue;
            digitalWrite(d, HIGH);
            Serial.print(F("[WALK] D")); Serial.print(d); Serial.println(F(" HIGH"));
            delay(2500);
            digitalWrite(d, LOW);
        }
        Serial.println(F("[WALK] done -- all LOW except D6. `hold off` to restore."));
        return;
    }

    // ── xtalk ────────────────────────────────────────────────────────────
    // Does COPI reach CIPO when it should not?
    //
    // The bit-banged frames come back as the TRANSMIT word with every 1
    // stretched into an 8-bit run, aligned to the same bit position -- and
    // tracking the same frame, where a real response lags one (§8.5.1.2).
    // That is COPI coupling into CIPO, not the ADC answering.
    //
    // Run with /CS HIGH so the ADC's DOUT is guaranteed high-impedance: any
    // correlation left is between our own two wires. Reading immediately and
    // again after 2 ms separates the two causes, which need different repairs:
    //   follows and HOLDS      -> a hard short (bridge at J6, or in the cable)
    //   follows then decays    -> capacitive coupling on a floating CIPO,
    //                             which also means DOUT is not being driven
    if (low == "xtalk") {
        if (!bus_held) { adc.busRelease(); bus_held = true; }

        pinMode(BB_CS,   OUTPUT); digitalWrite(BB_CS, HIGH);   // ADC deselected
        pinMode(BB_COPI, OUTPUT);
        pinMode(BB_CIPO, INPUT);

        for (int lvl = 1; lvl >= 0; lvl--) {
            digitalWrite(BB_COPI, lvl ? HIGH : LOW);
            delayMicroseconds(50);
            const int fast = digitalRead(BB_CIPO);
            delay(2);
            const int slow = digitalRead(BB_CIPO);
            Serial.print(F("[XT] copi=")); Serial.print(lvl);
            Serial.print(F("  cipo@50us=")); Serial.print(fast ? F("H") : F("L"));
            Serial.print(F("  cipo@2ms="));  Serial.println(slow ? F("H") : F("L"));
        }

        digitalWrite(BB_COPI, LOW);
        Serial.println(F("[XT] /CS was HIGH throughout: DOUT is high-Z, so any"));
        Serial.println(F("[XT] correlation above is COPI -> CIPO, not the ADC."));
        Serial.println(F("[XT]   follows and holds -> hard short, J6[2] to J6[7]"));
        Serial.println(F("[XT]   follows then decays -> CIPO floating + coupling"));
        Serial.println(F("[XT]   no correlation -> coupling only under fast edges"));
        return;
    }

    // ── clocktest ────────────────────────────────────────────────────────
    // Does the SPI PERIPHERAL drive SCLK, even though digitalWrite(PI_1,..)
    // does not?
    //
    // This matters because the two reach the pin by different routes. The
    // Arduino PinName path is demonstrably broken for PI_1 (it drives PA_8,
    // PC_2 and PC_3 fine, and D9 only responds via the integer path), but
    // mbed::SPI configures its pins through the peripheral's own pinmap and
    // never touches that code. So the hardware SPI may have been clocking
    // correctly the whole time.
    //
    // Normal traffic is ~0.015% duty and invisible to a meter. Hammering the
    // bus makes SCLK a near-square wave, so a DMM reads roughly a third to a
    // half of the rail. That is the difference between "clocking" and "dead".
    if (low.startsWith("clocktest")) {
        long secs = low.substring(9).toInt();
        if (secs <= 0 || secs > 60) secs = 15;

        if (bus_held) {                    // clocktest needs the bus back
            pinMode(BB_CS, OUTPUT);
            digitalWrite(BB_CS, HIGH);
            adc.busAcquire();
            bus_held = false;
        }

        Serial.print(F("[CLK] hammering SPI for ")); Serial.print(secs);
        Serial.println(F(" s -- measure D9 (SCLK) and D8 (COPI) as DC volts."));
        Serial.println(F("[CLK]   ~1 V or more  = the peripheral IS clocking the pin"));
        Serial.println(F("[CLK]   ~0 V          = the peripheral drives nothing either"));
        Serial.println(F("[CLK] /CS stays high throughout, so the ADC ignores all of it."));

        const uint32_t t0 = millis();
        adc.busAcquire();                       // also (re-)applies mode 1
        while (millis() - t0 < (uint32_t)secs * 1000UL) {
            adc.clockBurst(0x55, 512);          // 0101_0101, /CS held HIGH
        }

        Serial.println(F("[CLK] done -- pins back to idle"));
        return;
    }

    // ── pintest ──────────────────────────────────────────────────────────
    // Can the H7 actually drive each outbound line, and is any of them
    // shorted? `bitbang` says SCLK never reaches the ADC; this says whether
    // the H7 end is at fault before you reach for the soldering iron, because
    // an open wire and a solder bridge to ground are different repairs.
    //
    // /CS runs FIRST as the control. cipotest already proved /CS reaches the
    // EVM, so it establishes what a healthy line looks like on this core. A
    // difference between /CS and SCLK is then a real difference, not an
    // artifact of how digitalRead treats an output pin.
    //
    // The honest limit: from the H7 alone, an OPEN wire and a wire correctly
    // landed on a high-impedance CMOS input BOTH read as floating. This
    // command can prove a short and prove the pad drives; it cannot prove
    // continuity. Only a meter does that.
    if (low == "pintest") {
        adc.busRelease();

        Serial.println(F("[PIN] /CS first as the control -- known to reach the EVM."));
        reportPin(F("/CS  PA_8 D6"), BB_CS);
        reportPin(F("SCLK PI_1 D9"), BB_SCLK);
        reportPin(F("COPI PC_3 D8"), BB_COPI);

        pinMode(BB_CS, OUTPUT);
        digitalWrite(BB_CS, HIGH);      // park CS idle again
        adc.busAcquire();

        Serial.println(F("[PIN] hi=H lo=L         -> pad drives, no short"));
        Serial.println(F("[PIN] hi=L              -> shorted to GND (bridge at J6?)"));
        Serial.println(F("[PIN] released=held-*   -> something external clamps the net"));
        Serial.println(F("[PIN] released=floating -> no short. But an OPEN wire and a"));
        Serial.println(F("[PIN]   correctly-landed CMOS input look IDENTICAL from here;"));
        Serial.println(F("[PIN]   only a meter separates them: J15-20 -> J6[5]."));
        return;
    }

#if H7_TRANSPORT_UDP
    if (low.startsWith("netcfg ")) {                 // netcfg <a.b.c.d> <port>
        String rest = in.substring(7); rest.trim();
        const int sp = rest.indexOf(' ');
        if (sp < 0) { Serial.println(F("[NET] usage: netcfg <ip> <port>")); return; }
        const String ipStr = rest.substring(0, sp);
        const long port = rest.substring(sp + 1).toInt();
        int oct[4], parts = 0, start = 0;
        for (int i = 0; i <= (int)ipStr.length() && parts < 4; i++) {
            if (i == (int)ipStr.length() || ipStr[i] == '.') {
                oct[parts++] = ipStr.substring(start, i).toInt();
                start = i + 1;
            }
        }
        if (parts != 4 || port <= 0 || port > 65535) {
            Serial.println(F("[NET] bad netcfg args")); return;
        }
        udp_pc_ip   = IPAddress(oct[0], oct[1], oct[2], oct[3]);
        udp_pc_port = (uint16_t)port;
        udp_on      = true;
        Serial.print(F("[NET] UDP stream -> ")); Serial.print(udp_pc_ip);
        Serial.print(':'); Serial.println(udp_pc_port);
        return;
    }
#endif

    // UNKNOWN: ignore silently-ish. The host session sends commands this
    // firmware does not know; wedging on them would break every capture.
    Serial.print(F("[CMD] ignored: ")); Serial.println(in);
}

static void pumpCommands() {
    static String line;
    while (Serial.available()) {
        const char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (line.length()) { handleCommand(line); line = ""; }
        } else if (line.length() < 120) {
            line += c;
        }
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  [STATUS] — numeric key=value only (lib_h7_session's STATUS_KV regex)
// ══════════════════════════════════════════════════════════════════════════
static void emitStatus() {
    const uint32_t now = millis();
    const uint32_t dt = now - last_status_ms;
    if (dt < STATUS_PERIOD_MS) return;
    last_status_ms = now;

    // Drain buffered samples first so [STATUS] cannot jump ahead of rows
    // generated before it (only matters on the CDC path).
    flushBatch();

    const uint32_t d_drdy = drdy_reads - last_drdy_reads;
    last_drdy_reads = drdy_reads;
    const float rate = (dt > 0) ? (d_drdy * 1000.0f / dt) : 0.0f;

    Serial.print(F("[STATUS] up="));   Serial.print(now / 1000);
    Serial.print(F(" loop_hz="));      Serial.print((uint32_t)(loop_passes * 1000UL / (dt ? dt : 1)));
    Serial.print(F(" frames="));       Serial.print(adc.framesRead());
    Serial.print(F(" crc_err="));      Serial.print(adc.crcErrors());
    Serial.print(F(" drdy="));         Serial.print(drdy_reads);
    Serial.print(F(" rate="));         Serial.print(rate, 2);
    Serial.print(F(" samples="));      Serial.print(samples_out);
    Serial.print(F(" tx_drop="));      Serial.print(tx_drop);
    Serial.print(F(" adc_ok="));       Serial.print(adc_ok ? 1 : 0);
    Serial.print(F(" spi_hz="));       Serial.print(adc.spiHz());
    Serial.print(F(" osr="));          Serial.print(adc.osrDivisor());
    Serial.print(F(" gated="));        Serial.print(drdy_gated ? 1 : 0);
    Serial.print(F(" fast_us="));      Serial.print(fast_us);
    Serial.print(F(" dedup="));        Serial.print(dedup_skipped);
#if H7_TRANSPORT_UDP
    Serial.print(F(" udp_on="));       Serial.print(udp_on ? 1 : 0);
#else
    Serial.print(F(" udp_on=0"));
#endif
    Serial.println();
    loop_passes = 0;
}

// ══════════════════════════════════════════════════════════════════════════
//  Late attach
// ══════════════════════════════════════════════════════════════════════════
// The EVM is externally powered on this rig — R45 lifted, DVDD off the H7's
// 3V3, AVDD made on-board by U1 from a bench 5 V — so its analog rail can come
// up well AFTER the H7 has booted. t_POR is specified from the ADC's own
// supplies reaching 90% (§6.7), not from anything the H7 can observe, so no
// boot-time delay can cover this. A one-shot probe would latch NOT FOUND
// forever with good hardware on the bench, and the operator's only recourse
// would be resetting the H7 in lockstep with the supply. Ask again instead.
static void reprobeAdc() {
    const uint32_t now = millis();
    if (now - last_probe_ms < ADC_REPROBE_MS) return;
    last_probe_ms = now;

    // Timeout 0 = one ID read, ~150 us at 2 MHz. Only pay for a full reset
    // once something actually answers, so an absent EVM never stalls the
    // command pump for the 250 ms that reset()'s own poll is allowed to spend.
    if (!adc.waitInterfaceReady(0)) return;
    if (!adc.reset()) return;

    adc_ok = true;
    Serial.print(F("[BOOT] ADS131M04 attached late, id=0x"));
    Serial.println(adc.deviceID(), HEX);
    selftest();
    applyConfig(OSR_DEFAULT);
    adc.resetCounters();
}

// ══════════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    const uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) { /* brief wait for the host */ }

    Serial.println();
    Serial.println(F("*** Firmware_ADS131M04Test_PIO — M7 bench test ***"));
    Serial.println(F("[BOOT] plan: docs/ADS131M04_migration_plan.md"));
    Serial.println(F("[BOOT] host: Experiment_ADS131M04Eval/operator_m04_sweep.py"));
    Serial.println(F("[BOOT] flash portenta_m4_idle first — M4 shares SPI1"));

    adc_ok = adc.begin(SPI_HZ_DEFAULT);
    if (!adc_ok) {
        Serial.print(F("[BOOT] ADS131M04 NOT FOUND, id=0x"));
        Serial.println(adc.deviceID(), HEX);
        Serial.println(F("[BOOT]   check Cable 1 -> EVM J6, EVM powered, grounds common"));
        Serial.println(F("[BOOT]   check EVM JP6 fitted [1-2], JP5 NOT fitted (CLKIN!)"));
        Serial.println(F("[BOOT]   re-probing every 1 s — power the EVM now and it attaches"));
        // Deliberately NOT a halt: [STATUS] must keep flowing so the host sees
        // adc_ok=0 rather than a dead port it cannot tell from a bad cable.
    } else {
        selftest();
        applyConfig(OSR_DEFAULT);
    }
    adc.printConfig(Serial);
    adc.resetCounters();

#if H7_TRANSPORT_UDP
    Ethernet.begin(udp_h7_ip);
    udp.begin(udp_local_port);
    Serial.print(F("[NET] H7 IP ")); Serial.print(Ethernet.localIP());
    Serial.print(F("  link ")); Serial.println(Ethernet.linkStatus() == LinkON ? F("ON") : F("?"));
    Serial.println(F("[NET] send 'netcfg <pc_ip> 7777' to move the stream to UDP"));
#endif

    help();
    last_status_ms = millis();
    last_probe_ms  = millis();
}

void loop() {
    loop_passes++;
    pumpCommands();

    // `hold` owns the bus while it is parked — nothing else may drive it.
    if (!adc_ok && !bus_held) reprobeAdc();

    if (adc_ok && !bus_held) {
        if (drdy_gated) {
            // DRAIN back-to-back, and do no other work in between.
            //
            // This is Fault B's fix. Reading ONE frame per loop pass put a gap
            // in front of every frame — flushBatch() sends a UDP datagram,
            // emitStatus() and pumpCommands() run — and the first frame after a
            // gap is the one that gets disturbed. Measured at one and the same
            // data rate: frames issued back-to-back fail CRC ~4 % of the time,
            // frames issued after a gap ~25 %.
            //
            // §8.5.1.9.1 prescribes exactly this: the output FIFO holds two
            // samples per channel, DRDY stays asserted until both are read, and
            // after any gap in reading the remedy is to "quickly read two data
            // packets". Draining while DRDY is asserted does that and keeps the
            // FIFO empty so the gap never opens in the first place.
            //
            // CAPPED, deliberately: a DRDY stuck low must not starve
            // flushBatch()/pumpCommands() and wedge the board — that is the
            // freeze mode this rig's DRDY-ISR history warns about.
            if (fast_us) {
                static uint32_t last_fast_us = 0;
                const uint32_t now_us = micros();
                if (now_us - last_fast_us >= fast_us) {
                    last_fast_us = now_us;
                    ADS131M04_Reading r;
                    if (adc.readChannels(r)) {          // CRC-good frames only
                        if (r.status & 0x000F) {        // fresh conversion
                            drdy_reads++;
                            const uint32_t t_ms = millis();
                            for (uint8_t ch = 0; ch < ADS131M04_NUM_CH; ch++)
                                emitSample(SRC_FOR_CH[ch], r.raw[ch],
                                           r.volts[ch], r.hw_us, t_ms);
                        } else {
                            dedup_skipped++;            // re-read of the same
                        }
                    }
                }
            } else if (adc.dataReadyPin()) {
                // Step off the conversion boundary before clocking (see
                // DRDY_DELAY_US_DEFAULT). Cheap: this is idle time we would
                // otherwise spend spinning on the level check anyway.
                if (drdy_delay_us) delayMicroseconds(drdy_delay_us);
                uint8_t drained = 0;
                do {
                    readAndEmit();
                    drained++;
                } while (adc.dataReadyPin() && drained < DRDY_DRAIN_MAX);
            }
        } else {
            static uint32_t last_us = 0;
            const uint32_t now_us = micros();
            if (now_us - last_us >= poll_us) { last_us = now_us; readAndEmit(); }
        }
    }

    flushBatch();
    emitStatus();
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
