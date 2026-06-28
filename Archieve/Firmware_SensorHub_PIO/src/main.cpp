/**
 * @file main.cpp  (Portenta H7 dual-core — SensorHub: dual ADC)
 *
 * Runs BOTH ADS1263 ADCs simultaneously on the same chip. Production
 * channel assignment (swapped 2026-05-28 after the Calibrate_LaserHead
 * and Calibrate_LoadCell cross-compare runs — see root README):
 *
 *   - ADC1 (32-bit, 400 SPS, Sinc3, PGA=1) → AIN4(+) / AIN5(-)  [Keyence IL-030 laser]
 *   - ADC2 (24-bit, 400 SPS, Sinc3, gain=1) → AIN2(+) / AIN3(-) [LCA-9PC load cell]
 *
 * External REF7050 (+5 V) on AIN0(+)/AIN1(-) is shared by both ADCs
 * (REFMUX = 0x09 for ADC1, REF2 = 001 for ADC2).
 *
 *
 * ── Phase 5 changes (2026-06-01) ──────────────────────────────────────
 *
 * 1. AdcSample grew 16 → 24 bytes  (see sample_ring.h header notes).
 *    Slot now carries `hw_us` (microsecond timestamp at acquisition)
 *    and `seq` (per-src monotonic sequence number) so the host can
 *    measure timing jitter and detect dropped samples directly.
 *
 * 2. ADC1 reads driven by DRDY-edge interrupt on PC_6 (was millis()-
 *    gated polling). The ISR captures `hw_us` at entry and sets a
 *    "pending" flag; the M4 loop services the flag, performs the SPI
 *    read, and embeds the ISR-captured hw_us into the slot. Reading
 *    SPI from main loop (not the ISR itself) keeps the SPI driver out
 *    of interrupt context, but the recorded `hw_us` reflects the true
 *    sampling instant, not the read-completion time — so jitter
 *    measurements remain valid even if the loop is briefly delayed.
 *
 *    ADC2 has no DRDY pin on the ADS1263. Per-conversion availability
 *    is signalled by bit 7 (ADC2_NEW) of the STATUS byte returned with
 *    every RDATA1 frame. We piggy-back on the ADC1 read path: after
 *    each ADC1 fetch, check STATUS[7] and read ADC2 if it has new
 *    data. ADC1 runs at ≥ ADC2's rate (400 SPS each currently;
 *    1 kSPS / 800 SPS in Phase 6), so this never starves ADC2.
 *
 * 3. M7 emits a `[STATUS]` telemetry frame once per second:
 *      [STATUS] t_ms=… hwm=… dropped=… rate1=… rate2=… m4_loops_per_s=…
 *    The frame is non-breaking — the host parser already drops any
 *    line containing `[` — and is matched by `^\[STATUS\] ` in any
 *    consumer that wants the diagnostics.
 *
 * Per-sample TSV stream gained two columns (hw_us, seq):
 *   <t_ms>\t<src>\t<raw_code>\t<voltage_V>\t<hw_us>\t<seq>
 * The host parser regex in Calibrate_LaserHead/portenta_reader.py
 * was extended to tolerate trailing columns, so older 4-col logs
 * still parse identically.
 *
 *
 * IPC architecture (ring buffer):
 *
 *   Previous (legacy): M4 → RPC.print() → M7 → Serial.write() → USB
 *     Problem: synchronous RPC at ~660 msg/s back-pressured M4 and
 *     crashed it under sustained throughput.
 *
 *   Current:  M4 → ring buffer (SRAM4) → M7 → Serial.print() → USB
 *     M4 writes ADC samples into a lock-free ring buffer in shared SRAM4.
 *     M7 drains the ring at its own pace and formats TSV for USB Serial.
 *     Neither core ever blocks on the other — USB back-pressure on M7
 *     is absorbed by the 1024-slot ring (~0.5 s at 2 kSPS combined).
 *     RPC is retained for boot-time checkpoint messages only.
 *
 *
 * Flash order (first time):
 *   pio run -e portenta_m7_bridge -t upload
 *   pio run -e portenta_m4        -t upload
 *   pio device monitor
 *
 * → Power-cycle the rig (USB + EVM supply) after every upload — the
 *   dfu reset alone does not cleanly re-power the EVM's analog rails.
 */

#include <Arduino.h>
#include "RPC.h"
#include "sample_ring.h"

// ── Enable/disable each ADC path at build time ─────────────────────────
// Shared between M7 (output formatting) and M4 (ADC control). Keeping
// these as flags (not plain constants) lets individual paths be
// temporarily disabled for bring-up diagnostics without touching the
// loop() code.
#define ENABLE_ADC1   1
#define ENABLE_ADC2   1


// ══════════════════════════════════════════════════════════════════════
//  M7 CORE — drain ring buffer + RPC boot messages → USB Serial,
//            emit [STATUS] telemetry once per second
// ══════════════════════════════════════════════════════════════════════
#if defined(CORE_CM7)

void setup() {
    // Zero the ring buffer BEFORE booting M4 (RPC.begin() starts the
    // M4 core). SRAM4 may contain garbage from a previous run; clearing
    // the header ensures M4 starts with write_idx == read_idx == 0 and
    // hwm / dropped / seq counters at 0.
    SAMPLE_RING->write_idx = 0;
    SAMPLE_RING->read_idx  = 0;
    SAMPLE_RING->dropped   = 0;
    SAMPLE_RING->hwm       = 0;
    for (int i = 0; i < 8; i++) SAMPLE_RING->seq_per_src[i] = 0;
    __DMB();

    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}

    RPC.begin();     // boots M4 core
    Serial.println("[M7] bridge up — ring-buffer IPC, forwarding to USB Serial (SensorHub)");
    Serial.println("[M7] per-sample TSV format: t_ms\\tsrc\\traw_code\\tvoltage_V\\thw_us\\tseq");
    Serial.println("[M7] periodic [STATUS] line: hwm=… dropped=… rate1=… rate2=… m4_loops_per_s=…");
}

// ── M7 status-frame state ─────────────────────────────────────────────
// All counters are M7-local except the ring-header reads/clears.
static uint32_t last_status_ms      = 0;
static uint32_t last_dropped        = 0;
static uint32_t pop_count_src1      = 0;
static uint32_t pop_count_src2      = 0;
static uint32_t pop_count_other     = 0;
// M4 increments `seq_per_src[]` on every ring_push. Sample those at the
// start of every [STATUS] window; the delta gives M4-side production
// rate independent of how many samples M7 was able to drain.
static uint32_t last_seq_src1       = 0;
static uint32_t last_seq_src2       = 0;

static const uint32_t STATUS_PERIOD_MS = 1000;

void loop() {
    // ── 1. Drain boot / diagnostic text from M4 via RPC ───────────────
    while (RPC.available()) {
        Serial.write(RPC.read());
    }

    // ── 2. Drain ADC samples from the shared-memory ring buffer ───────
    //    Per-sample TSV (6 columns):
    //      <t_ms>\t<src>\t<raw_code>\t<voltage_V>\t<hw_us>\t<seq>
    //    Single-ADC builds collapse to 5 columns (no src):
    //      <t_ms>\t<raw_code>\t<voltage_V>\t<hw_us>\t<seq>
    AdcSample s;
    int batch = 0;
    while (ring_pop(SAMPLE_RING, s) && batch < 64) {
        Serial.print(s.timestamp_ms);
        Serial.print('\t');
#if ENABLE_ADC1 && ENABLE_ADC2
        Serial.print((int)s.src);
        Serial.print('\t');
#endif
        Serial.print(s.raw_code);
        Serial.print('\t');
        Serial.print(s.voltage_V, 6);
        Serial.print('\t');
        Serial.print(s.hw_us);
        Serial.print('\t');
        Serial.println(s.seq);

        // Per-src pop counts feed the [STATUS] rate1=/rate2= fields.
        if      (s.src == SAMPLE_SRC_LASER) pop_count_src1++;
        else if (s.src == SAMPLE_SRC_LOAD)  pop_count_src2++;
        else                                pop_count_other++;
        batch++;
    }

    // ── 3. Periodic [STATUS] telemetry frame (every 1 s) ──────────────
    //    Format (single line, space-separated key=value):
    //      [STATUS] t_ms=<ms> hwm=<peak occ> dropped=<delta>
    //               rate1=<pops/s> rate2=<pops/s>
    //               prod1=<seq delta/s> prod2=<seq delta/s>
    //               m4_loops_per_s=<delta>
    //
    //    rate*  = how many samples M7 drained per src in the window
    //             (host-side throughput)
    //    prod*  = how many samples M4 produced per src in the window
    //             (M4-side throughput). prod − rate ≈ samples
    //             that built up in the ring (or in the M7 USB queue).
    //    hwm    = peak ring occupancy since last [STATUS]; reset to 0
    //             after this read.
    //    dropped = ring-overflow drops *delta* this window (not
    //             cumulative — easier to spot bursts).
    //
    uint32_t now = millis();
    if (now - last_status_ms >= STATUS_PERIOD_MS) {
        uint32_t dt_ms      = now - last_status_ms;
        last_status_ms      = now;

        // Per-src M4 production deltas
        uint32_t cur_seq1   = SAMPLE_RING->seq_per_src[SAMPLE_SRC_LASER];
        uint32_t cur_seq2   = SAMPLE_RING->seq_per_src[SAMPLE_SRC_LOAD];
        uint32_t prod1      = cur_seq1 - last_seq_src1;
        uint32_t prod2      = cur_seq2 - last_seq_src2;
        last_seq_src1       = cur_seq1;
        last_seq_src2       = cur_seq2;

        // Overflow delta
        uint32_t cur_dropped = SAMPLE_RING->dropped;
        uint32_t dropped_delta = cur_dropped - last_dropped;
        last_dropped        = cur_dropped;

        // Peak ring occupancy since last frame (read-and-reset)
        uint32_t hwm        = ring_hwm_read_reset(SAMPLE_RING);

        // Scale window-counts to per-second (most useful at 1000 ms but
        // robust to scheduling jitter).
        auto per_s = [&](uint32_t n) -> uint32_t {
            // integer-only divide to avoid float in the hot path
            return (uint32_t)(((uint64_t)n * 1000UL) / (dt_ms ? dt_ms : 1));
        };
        uint32_t rate1 = per_s(pop_count_src1);
        uint32_t rate2 = per_s(pop_count_src2);
        uint32_t rate_other = per_s(pop_count_other);
        uint32_t prate1 = per_s(prod1);
        uint32_t prate2 = per_s(prod2);
        pop_count_src1 = 0;
        pop_count_src2 = 0;
        pop_count_other = 0;

        // M4 loop counter (lives in the upper seq_per_src slot we
        // reserved as a "scratch" — index 0 is unused since src=0 is
        // not a valid sample source). M4 increments it on every loop
        // iteration; we sample-and-reset here.
        uint32_t m4_loops = SAMPLE_RING->seq_per_src[0];
        SAMPLE_RING->seq_per_src[0] = 0;
        uint32_t m4_loops_per_s = per_s(m4_loops);

        Serial.print("[STATUS] t_ms=");      Serial.print(now);
        Serial.print(" hwm=");               Serial.print(hwm);
        Serial.print(" cap=");               Serial.print(RING_CAPACITY);
        Serial.print(" dropped=");           Serial.print(dropped_delta);
        Serial.print(" dropped_total=");     Serial.print(cur_dropped);
        Serial.print(" rate1=");             Serial.print(rate1);
        Serial.print(" rate2=");             Serial.print(rate2);
        if (rate_other) {
            Serial.print(" rate_other=");    Serial.print(rate_other);
        }
        Serial.print(" prod1=");             Serial.print(prate1);
        Serial.print(" prod2=");             Serial.print(prate2);
        Serial.print(" m4_loops_per_s=");    Serial.print(m4_loops_per_s);
        Serial.println();
    }
}

// ══════════════════════════════════════════════════════════════════════
//  M4 CORE — drive the ADS1263 (both ADCs) and stream to M7 via
//            shared-memory ring buffer
//
//  Phase 5: ADC1 sampling is now DRDY-edge driven. The DRDY ISR
//  captures `hw_us` (via micros()) and sets a pending flag; the loop
//  performs the SPI fetch and pushes the sample with the ISR-captured
//  timestamp. ADC2 is read piggy-backed on each ADC1 fetch if the
//  STATUS byte indicates ADC2_NEW.
// ══════════════════════════════════════════════════════════════════════
#elif defined(CORE_CM4)

#include <SPI.h>
#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// Checkpoint macro — same convention as the sibling projects.
#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

// ── DRDY ISR state ────────────────────────────────────────────────────
// The ISR is intentionally tiny: capture the timestamp, increment a
// DRDY-edge counter, set a pending flag. SPI access happens in loop()
// to keep the Arduino SPI driver out of interrupt context (mbed-os's
// SPI mutex is not ISR-reentrant on this core).
//
// Recording `drdy_us_latest` at ISR entry preserves the true sampling
// instant for jitter analysis even if the loop is briefly delayed by
// future Phase 6 work (SMA state machine).

static volatile uint32_t drdy_us_latest  = 0;   // hw_us at last DRDY edge
static volatile uint32_t drdy_edge_count = 0;   // total DRDY edges seen
static volatile uint32_t drdy_serviced   = 0;   // edges the main loop has handled
static volatile bool     adc1_pending    = false;

// Loop counter — surfaced by the M7 [STATUS] frame as a rough M4
// headroom indicator. Higher loop rate ⇒ more headroom for Phase 6
// SMA control work to live on M4.
static uint32_t          m4_loop_counter = 0;

// Diagnostic: count of DRDY edges that arrived while the previous
// sample was still pending (i.e. loop was too slow). Surfaced via
// the missed_edges field in the M4 boot log; nonzero means we're
// near the throughput limit of the loop-services-ISR pattern.
static volatile uint32_t drdy_overrun_count = 0;

static void drdy_isr() {
    uint32_t t = micros();
    drdy_us_latest = t;
    drdy_edge_count++;
    if (adc1_pending) {
        // Previous sample wasn't serviced yet — main loop fell behind.
        drdy_overrun_count++;
    }
    adc1_pending = true;
}

void setup() {
    // RPC first so we can report progress to the M7 bridge.
    RPC.begin();
    delay(500);
    CP(0, "RPC up");

    Serial.begin(115200);
    CP(1, "Serial.begin done");

    // Production-build banner — operator should see this BEFORE the
    // power-up settle delay below so they know they flashed the right
    // firmware variant (production vs. one of the Calibrate_*_PIO).
    RPC.println("[M4] *** Firmware_SensorHub_PIO — dual-ADC production stream (Phase 5) ***");
    RPC.println("[M4]   ADC1 → AIN4/AIN5 (Keyence IL-030 laser)");
    RPC.println("[M4]   ADC2 → AIN2/AIN3 (LCA-9PC load cell)");
    RPC.println("[M4] IPC: shared-memory ring buffer (sample_ring.h, 24-byte slot)");
    RPC.println("[M4] sampling: ADC1 DRDY-ISR on PC_6; ADC2 piggy-back on STATUS.ADC2_NEW");

    // ADS1263 power-up settle — required on every cold boot. The dfu
    // reset doesn't cleanly re-power the EVM's analog rails (the
    // on-board TPS7A4700 LDO needs a full power-on transient to
    // settle), so give the chip time to come out of reset before we
    // talk SPI to it.
    RPC.println("[M4] waiting 3000 ms for ADS1263 to power up...");
    delay(3000);
    RPC.println("[M4] ADS1263 power-up settle done");

    // Drive the pins BEFORE adc.begin() so we can localise any hang.
    pinMode(ADS1263_CS_PIN, OUTPUT);
    CP(2, "pinMode CS (PA_8 / J15-25 / PWM_0) done");

    pinMode(ADS1263_RESET_PIN, OUTPUT);
    CP(3, "pinMode RESET (PC_7 / J15-29 / PWM_2) done");

    pinMode(ADS1263_DRDY_PIN, INPUT_PULLUP);
    CP(4, "pinMode DRDY (PC_6 / J15-27 / PWM_1) done");

    digitalWrite(ADS1263_CS_PIN, HIGH);
    digitalWrite(ADS1263_RESET_PIN, HIGH);
    CP(5, "CS and RESET driven HIGH");

    SPI.begin();
    CP(6, "SPI.begin() returned");

    CP(7, "calling adc.begin()");
    bool ok = adc.begin();
    CP(8, ok ? "adc.begin returned TRUE" : "adc.begin returned FALSE");

    if (!ok) {
        RPC.println("[M4] FATAL: ADS1263 init failed");
        while (1) { delay(1000); }
    }

    RPC.print("[M4] ADC ready, ID=0x");
    RPC.println(adc.getDeviceID(), HEX);

    // ── Configure ADC1 — LASER HEAD (post-2026-05-28 swap) ────────────
    // (config block unchanged from pre-Phase-5)
#if ENABLE_ADC1
    adc.configureADC1(
        /*inpmux     =*/ 0x45,                       // AIN4(+) / AIN5(-) — laser
        /*refmux     =*/ ADS1263_REFMUX_EXT_AIN01,   // 0x09 — REF7050 on AIN0/AIN1
        /*vref_V     =*/ 5.0f,
        /*rate       =*/ ADS1263_400SPS,
        /*pga_bypass =*/ false                       // PGA in path, gain=1
    );
    adc.startADC1();
    CP(9, "ADC1 started on AIN4/AIN5 (laser), REF7050 on AIN0/AIN1, PGA in path gain=1");
#endif

    // ── Configure ADC2 — LOAD CELL (post-2026-05-28 swap) ─────────────
#if ENABLE_ADC2
    adc.configureADC2(
        /*adc2mux =*/ 0x23,                           // AIN2(+) / AIN3(-) — load cell
        /*ref2    =*/ ADS1263_ADC2_REF_AIN01,         // external REF7050 on AIN0/AIN1
        /*vref_V  =*/ 5.0f,
        /*rate    =*/ ADS1263_ADC2_400SPS,
        /*gain    =*/ ADS1263_ADC2_GAIN_1
    );
    adc.startADC2();
    CP(10, "ADC2 started on AIN2/AIN3 (load), REF7050 shared with ADC1, 400 SPS gain=1");
#endif

    delay(100);   // one filter-settle interval

    adc.printConfig();

    // ── Attach DRDY interrupt AFTER the ADC is converting ─────────────
    // The chip drives DRDY LOW when a new ADC1 conversion is ready, and
    // releases it HIGH on RDATA1. We trigger on the falling edge so the
    // ISR fires at the *start* of each new sample being available.
    //
    // Note: this MUST come after adc.startADC1() — attaching while DRDY
    // is statically LOW (chip in reset or stopped) means we'd miss the
    // first few edges as the chip warms into continuous-conversion mode.
    attachInterrupt(digitalPinToInterrupt(ADS1263_DRDY_PIN), drdy_isr, FALLING);
    CP(11, "DRDY interrupt attached on PC_6 (FALLING)");

    // Output format line — with both ADCs active, every line carries a
    // src column so the host can demultiplex the two streams.
#if ENABLE_ADC1 && ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\tsrc\\traw\\tV\\thw_us\\tseq   (src=1 laser, src=2 load)");
#elif ENABLE_ADC1
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw\\tV\\thw_us\\tseq   (ADC1/laser only)");
#elif ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw\\tV\\thw_us\\tseq   (ADC2/load only)");
#else
    #error "Neither ENABLE_ADC1 nor ENABLE_ADC2 is set — nothing to do."
#endif
}

void loop() {
    // M4 loop counter — surfaced by M7 [STATUS] as m4_loops_per_s.
    // Stashed into seq_per_src[0] (slot reserved; src=0 not a valid
    // sample source). M7 reads-and-clears once per second.
    m4_loop_counter++;
    SAMPLE_RING->seq_per_src[0] = m4_loop_counter;

    // ── Service pending DRDY edge ────────────────────────────────────
    // The ISR captures hw_us at the true sampling instant; we perform
    // the SPI fetch here (out of interrupt context, where SPI is safe
    // to call) and embed the ISR-captured timestamp into the slot.
#if ENABLE_ADC1
    if (adc1_pending) {
        // Atomically snapshot ISR state. Disabling interrupts for the
        // few cycles needed to copy two u32s avoids a torn read if a
        // new DRDY edge fires mid-snapshot.
        noInterrupts();
        uint32_t hw_us  = drdy_us_latest;
        adc1_pending    = false;
        drdy_serviced++;
        interrupts();

        uint32_t ts_ms  = millis();

        ADC_Reading r1 = adc.readADC1Direct();
        if (r1.valid) {
            ring_push(SAMPLE_RING, hw_us, ts_ms, SAMPLE_SRC_LASER,
                      r1.raw_code, r1.voltage_V);
        }

#if ENABLE_ADC2
        // ── Piggy-back ADC2 read on ADC2_NEW status bit ──────────────
        // STATUS byte layout (datasheet §9.4.7.1):
        //   bit 7 = ADC2 new data ready (1 = RDATA2 will return fresh)
        //   bit 6 = ADC1 new data ready  (cleared by the RDATA1 we just did)
        //   bits 4..0 = alarms / reset
        // ADC1 runs at ≥ ADC2's rate, so every ADC2 conversion is caught
        // within at most one ADC1 sample period of latency. The same
        // hw_us is reused — that's acceptable: ADC2's conversion
        // completed at most one ADC1 period before this ISR fired, and
        // the host can recover precise ADC2 timing from the seq counter
        // if needed.
        if (r1.status & 0x80) {
            ADC_Reading r2 = adc.readADC2Direct();
            if (r2.valid) {
                ring_push(SAMPLE_RING, hw_us, ts_ms, SAMPLE_SRC_LOAD,
                          r2.raw_code, r2.voltage_V);
            }
        }
#endif
    }
#else
    // ADC1 disabled — poll ADC2 directly on a timer.
#if ENABLE_ADC2
    static uint32_t t2_last = 0;
    if (millis() - t2_last >= 2) {
        t2_last = millis();
        uint32_t hw_us = micros();
        ADC_Reading r = adc.readADC2Direct();
        if (r.valid) {
            ring_push(SAMPLE_RING, hw_us, millis(), SAMPLE_SRC_LOAD,
                      r.raw_code, r.voltage_V);
        }
    }
#endif
#endif
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
