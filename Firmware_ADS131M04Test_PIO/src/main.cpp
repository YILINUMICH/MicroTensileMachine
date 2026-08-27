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
static bool     adc_ok = false;

static uint32_t last_status_ms = 0;
static uint32_t last_drdy_reads = 0;

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
    if (batch_off >= BATCH_FLUSH_BYTES || sizeof(batch) - batch_off < 96) flushBatch();

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
    Serial.println(F("[CMD] poll <us> | drdy | netcfg <ip> <port> | ping | help"));
}

static void applyConfig(ADS131M04_OSR_t osr) {
    if (!adc.configure(osr)) Serial.println(F("[CFG] FAIL: configure() rejected"));
    else {
        Serial.print(F("[CFG] osr=")); Serial.print(adc.osrDivisor());
        Serial.print(F(" rate="));     Serial.print(adc.sps(), 2);
        Serial.println(F(" SPS"));
    }
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
        // Hardware SYNC/RESET (>= 2048 t_CLKIN). Registers return to defaults,
        // so the condition must be re-applied by the host afterwards.
        const bool ok = adc.reset();
        Serial.print(F("[RST] ")); Serial.print(ok ? F("OK id=0x") : F("FAILED id=0x"));
        Serial.println(adc.deviceID(), HEX);
        adc_ok = ok;
        if (ok) applyConfig(OSR_DEFAULT);
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
#if H7_TRANSPORT_UDP
    Serial.print(F(" udp_on="));       Serial.print(udp_on ? 1 : 0);
#else
    Serial.print(F(" udp_on=0"));
#endif
    Serial.println();
    loop_passes = 0;
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
}

void loop() {
    loop_passes++;
    pumpCommands();

    if (adc_ok) {
        if (drdy_gated) {
            // NON-BLOCKING level check. DRDY_FMT=0 holds the line low until
            // the data is read, so this reads each conversion exactly once and
            // cannot stall: no edges simply means no reads.
            if (adc.dataReadyPin()) readAndEmit();
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
