/**
 * @file sample_ring.h
 * @brief Lock-free SPSC ring buffer for M4 → M7 ADC sample streaming.
 *
 * Replaces the synchronous RPC.print() path that was causing mid-run
 * crashes under sustained dual-ADC throughput (~660 msg/s at 3 ms poll).
 * M4 (producer) writes ADC samples into the ring without ever blocking;
 * M7 (consumer) drains the ring at its own pace and formats for USB
 * Serial. The two cores are fully decoupled — USB back-pressure on M7
 * never stalls M4's SPI polling loop.
 *
 * Placement: SRAM4 (D3 domain, 0x38000000–0x3800FFFF, 64 KB).
 * The ring starts at offset 0x8000 (32 KB in) to stay clear of the
 * OpenAMP / Arduino-RPC shared-memory region at the base of SRAM4.
 *
 * Cache coherency: SRAM4 sits in the D3 low-power domain, which the
 * Arduino/Mbed MPU defaults map as non-cacheable. The Cortex-M4 has no
 * data cache. `volatile` qualifiers plus `__DMB()` memory barriers are
 * sufficient for correct ordering — no manual cache maintenance needed.
 *
 * Designed to be shared: include this header from any *_PIO/src/main.cpp
 * that needs the ring (Calibrate_LaserHead_PIO, Calibrate_Loadcell_PIO,
 * Firmware_SensorHub_PIO, etc.).
 *
 *
 * ── Phase 5 layout (2026-06-01) ────────────────────────────────────────
 *
 * Slot grew from 16 → 24 bytes to carry two diagnostics the Phase 5
 * spring smoke test needs and Phase 6 SMA control inherits:
 *
 *   - `hw_us`  — free-running microsecond timestamp captured at the
 *                instant the ADC sample was acquired (DRDY ISR entry
 *                on Firmware_SensorHub_PIO; micros() at read time on the
 *                Calibrate_*_PIO single-ADC variants). Lets the host
 *                measure per-channel timing jitter directly instead
 *                of inferring it from millis()-quantised timestamps.
 *
 *   - `seq`    — monotonic per-`src` sequence number, assigned by
 *                ring_push(). Lets the host detect dropped samples
 *                deterministically: any gap in seq for a given src
 *                = a sample lost between M4 production and M7 drain.
 *
 * The ring header also grew to carry an M4-tracked high-water mark
 * (`hwm`) which the M7 [STATUS] frame emits and resets every second.
 *
 * Slot later padded 24 → 32 B (one M7 cache line) and capacity set to 512
 * (2026-06-29 torn-read fix); 512 × 32 B = 16 KB, comfortably inside the
 * 32 KB SRAM4 partition. See the AdcSample comment below.
 */

#ifndef SAMPLE_RING_H
#define SAMPLE_RING_H

#include <stdint.h>

// ══════════════════════════════════════════════════════════════════════
//  `src` ID reservations  (Phase 5 — do not change without coordinating
//  with Phase 6 SMA integration and the host-side parser)
// ══════════════════════════════════════════════════════════════════════
//
//   src    | Meaning                                  | Path
//   -------+------------------------------------------+----------------
//     1    | Laser displacement  (ADS1263 ADC1)       | Existing
//     2    | Load cell force     (ADS1263 ADC2)       | Existing
//     3    | SMA drive voltage   (on-chip ADC)        | Phase 6
//     4    | SMA shunt current   (on-chip ADC, V-fmt) | Phase 6
//     5    | SMA resistance      (M4-computed = V/I)  | Phase 6
//     6    | CC command voltage u (M7 control state)  | CC fork
//     7    | CC plant-R estimate  (M7 control state)  | CC fork
//   0xF0+  | State-machine events (not sample data)   | Phase 6
//
// The ring's per-src seq counter array (see below) is sized for src
// indices 0..7. Adding src values ≥ 8 requires growing seq_per_src[].
// Phase 6 state-machine events at 0xF0+ live on a separate channel
// (RPC or a dedicated event ring) — they do not consume sample slots.
//
// src=6/7 are CONTROLLER STATE, not sensor readings — they exist only in
// Firmware_SMAConstantCurrent_PIO and are emitted only while the current
// loop is closed. They take the last two reserved slots, so seq_per_src[8]
// is now exactly full: any further channel must grow that array (and the
// ring header layout with it, in lockstep with the host parser).

#define SAMPLE_SRC_LASER     1
#define SAMPLE_SRC_LOAD      2
#define SAMPLE_SRC_SMA_V     3
#define SAMPLE_SRC_SMA_I     4
#define SAMPLE_SRC_SMA_R     5
#define SAMPLE_SRC_CC_U      6   // CC fork: command voltage u [V]; raw = DAC code
#define SAMPLE_SRC_CC_R      7   // CC fork: adaptive plant-R estimate [ohm]
#define SAMPLE_SRC_MAX       7   // largest src index that fits seq_per_src[]


// ══════════════════════════════════════════════════════════════════════
//  Sample struct — one ADC reading
// ══════════════════════════════════════════════════════════════════════

// CACHE-LINE SIZED (2026-06-29). The Cortex-M7 D-cache line is 32 bytes. A
// 24-byte slot straddles cache-line boundaries and lets adjacent slots
// false-share a line, so M7's cached view of a slot M4 just wrote (M4 is
// cacheless, writes straight to SRAM4) reads partly stale → TORN reads
// (the `1024`/`1` field bleed-through + crash-reboot loop documented in
// docs/TROUBLESHOOTING_ADS1263_frozen_reading.md). Padding each slot to a
// full 32-byte cache line + aligning the array (below) makes every slot own
// exactly one line: no straddle, no false-sharing. The text the host parses
// is emitted by M7 field-by-field, so this binary change is host-transparent.
struct __attribute__((aligned(32))) AdcSample {
    uint32_t hw_us;           // free-running µs at sample acquisition
    uint32_t seq;             // per-src monotonic sequence number
    uint32_t timestamp_ms;    // millis() at read time (legacy / host parser)
    uint8_t  src;             // see `src` ID reservations above
    uint8_t  _pad8[3];        // explicit padding (raw_code aligned to 4)
    int32_t  raw_code;        // signed ADC code
    float    voltage_V;       // scaled voltage
    uint8_t  _pad32[8];       // pad 24 → 32 so one slot == one M7 cache line
};

// 4 + 4 + 4 + 1 + 3 + 4 + 4 + 8 = 32 bytes — exactly one Cortex-M7 cache line.
static_assert(sizeof(AdcSample) == 32, "AdcSample must be 32 bytes (one M7 cache line)");


// ══════════════════════════════════════════════════════════════════════
//  Ring buffer — lives at a fixed address in SRAM4
// ══════════════════════════════════════════════════════════════════════

// Capacity must be a power of 2 for efficient masking.
// 256 (8 KB) — ACCEPTED config (2026-06-29). Fits the top ~12 KB of SRAM4 that
// is clear of OpenAMP's active footprint (see RING_BASE). hwm stays at 1 in
// practice (M7 never backs up), so 256 is already deep insurance — capacity is
// not a limiting factor. 512 would need a base ≤ 0x3800BFA0, back down toward
// OpenAMP's reserved/active memory, so we deliberately stay at 256 up high.
static const uint32_t RING_CAPACITY = 256;
static const uint32_t RING_MASK     = RING_CAPACITY - 1;

struct SampleRing {
    // Producer-only fields (M4 writes, M7 reads-only)
    volatile uint32_t write_idx;          // M4 increments after slot write
    volatile uint32_t dropped;            // M4 increments on ring overflow
    volatile uint32_t crc_err;            // M4 increments per checksum-invalid read
                                          //   (sample discarded, never pushed → no seq)
    volatile uint32_t overrun;            // M4 publishes its DRDY-overrun count
                                          //   (DRDY arrived before prior sample serviced)
    volatile uint32_t m4_now_us;          // M4 publishes live micros() each loop; M7
    volatile uint32_t m4_now_ms;          //   stamps src=3/4/5 with these → all lines
                                          //   share the M4 timeline (clock alignment)
    volatile uint32_t hwm;                // M4 tracks high-water mark;
                                          //   M7 reads-and-clears each [STATUS] window
    volatile uint32_t seq_per_src[8];     // M4-only seq counters (index by src)

    // Consumer-only fields (M7 writes, M4 reads-only)
    volatile uint32_t read_idx;           // M7 increments after slot consume

    // samples[] is 32-byte (cache-line) aligned so slot N sits at a line
    // boundary and never straddles. The compiler inserts padding here to push
    // the array to the next 32-byte boundary (header is 68 B → array at 96 B).
    // RING_BASE itself is 32-byte aligned, so the in-RAM slots are line-aligned.
    __attribute__((aligned(32))) AdcSample samples[RING_CAPACITY];
};

// Header 68 B → padded to 96 B (samples[] aligned to 32). Samples: 256 × 32 =
// 8 192 B. Total ≈ 8 288 B ≈ 8 KB.
static_assert(sizeof(SampleRing) <= 32768,
              "SampleRing must fit in SRAM4");

// ── Ring base address — ACCEPTED PLACEMENT (2026-06-29) ───────────────────
// SRAM4 is 0x38000000–0x3800FFFF (64 KB). The linker reserves ALL of it for
// OpenAMP/RPC (`__OPENAMP_region_start__=0x38000400` .. `__OPENAMP_region_end__
// =0x38010000`), but OpenAMP only *actively* writes the bottom (its vrings +
// rpmsg buffer pool, near 0x38000400). The ring sat at 0x38008000 and got
// corrupted by that active region (torn reads → M4 crash-loop). Moving it HIGH
// to 0x3800D000 — well above OpenAMP's active footprint — fixed it completely
// (dropped=0, rate1=512, hwm=1). We keep this; it rides OpenAMP's non-cacheable
// MPU region (which is why no cache maintenance is needed here).
//
//   ring : 0x3800D000 – 0x3800F060  (8 KB, cap 256)
//
// ⚠ RESIDUAL RISK (accepted): this is inside OpenAMP's *reserved* region. If
// OpenAMP's active footprint ever grows past ~0x3800D000 (e.g. an mbed/RPC
// version bump, larger rpmsg buffer pool, or much heavier RPC traffic), it can
// collide again. CANARY: the boot `ring build-id` line + dropped_total/rate_other
// in [STATUS]. SYMPTOM TO WATCH: torn sample fields (small/index-like values
// like 1024 bleeding into raw/hw_us/seq) + t_ms resetting (M4 reboot loop).
// FIX IF IT RECURS: raise RING_BASE further / shrink cap, or disable OpenAMP and
// own the whole SRAM4 (needs bootM4() + self-managed cache coherency). See
// docs/TROUBLESHOOTING_ADS1263_frozen_reading.md.
static const uintptr_t RING_BASE = 0x3800D000;
static const uintptr_t SRAM4_END = 0x38010000;   // 0x38000000 + 64 KB

static_assert(RING_BASE + sizeof(SampleRing) <= SRAM4_END,
              "SampleRing overflows SRAM4");

#define SAMPLE_RING  (reinterpret_cast<volatile SampleRing*>(RING_BASE))


// ══════════════════════════════════════════════════════════════════════
//  Producer API — called from M4 only  (loop() or DRDY ISR)
// ══════════════════════════════════════════════════════════════════════

/**
 * Push one sample into the ring.  Returns true on success, false if the
 * ring is full (sample is dropped and the overflow counter incremented).
 * Never blocks.
 *
 * `hw_us`  — free-running µs timestamp captured at the sample-acquisition
 *            instant (ISR entry on DRDY-driven paths; micros() at read
 *            time on polled paths).
 * `ts_ms`  — millis() at the moment ring_push is called (retained for
 *            the legacy host parser; coarser than hw_us).
 * `src`    — see `src` ID reservations table at the top of this header.
 * `raw`    — signed ADC code (sign-extended 24-bit for ADC2).
 * `volts`  — pre-scaled voltage.
 *
 * The per-src seq counter is assigned by ring_push and embedded in the
 * slot. ISR callers do not need to manage seq.
 */
inline bool ring_push(volatile SampleRing* r,
                      uint32_t hw_us, uint32_t ts_ms,
                      uint8_t src,
                      int32_t raw, float volts) {
    uint32_t wr  = r->write_idx;
    uint32_t occ = wr - r->read_idx;
    if (occ >= RING_CAPACITY) {
        r->dropped++;
        return false;
    }

    // Per-src sequence number (M4-only writer → no atomic needed).
    // Out-of-range src defaults to slot 0 to avoid array-OOB on
    // misconfigured callers.
    uint8_t  s_idx = (src < 8) ? src : 0;
    uint32_t s_no  = r->seq_per_src[s_idx]++;

    // High-water mark (count BEFORE this push lands → matches what M7
    // will see when it pops).
    uint32_t occ_after = occ + 1;
    if (occ_after > r->hwm) r->hwm = occ_after;

    volatile AdcSample& slot = r->samples[wr & RING_MASK];
    slot.hw_us        = hw_us;
    slot.seq          = s_no;
    slot.timestamp_ms = ts_ms;
    slot.src          = src;
    slot.raw_code     = raw;
    slot.voltage_V    = volts;
    __DMB();                    // data committed before index is visible
    r->write_idx = wr + 1;
    return true;
}


// ══════════════════════════════════════════════════════════════════════
//  Consumer API — called from M7 only  (loop() drain)
// ══════════════════════════════════════════════════════════════════════

/**
 * Pop one sample from the ring.  Returns true if a sample was available,
 * false if the ring was empty.  Never blocks.
 */
inline bool ring_pop(volatile SampleRing* r, AdcSample& out) {
    uint32_t rd = r->read_idx;
    if (rd == r->write_idx) return false;   // empty
    __DMB();                    // see index before reading data
    const volatile AdcSample& slot = r->samples[rd & RING_MASK];
    out.hw_us        = slot.hw_us;
    out.seq          = slot.seq;
    out.timestamp_ms = slot.timestamp_ms;
    out.src          = slot.src;
    out.raw_code     = slot.raw_code;
    out.voltage_V    = slot.voltage_V;
    __DMB();                    // data consumed before advancing index
    r->read_idx = rd + 1;
    return true;
}

/**
 * Number of unread samples (approximate — indices may change between
 * reads of write_idx and read_idx).
 */
inline uint32_t ring_count(volatile SampleRing* r) {
    return r->write_idx - r->read_idx;
}

/**
 * Read-and-clear the high-water mark. Called by M7 once per [STATUS]
 * window so each frame reports peak occupancy since the previous frame.
 *
 * Race: M4 may write hwm in between the read and the store-to-zero,
 * losing one update. For diagnostic-only use the race is acceptable;
 * the host will still see any sustained backlog.
 */
inline uint32_t ring_hwm_read_reset(volatile SampleRing* r) {
    uint32_t h = r->hwm;
    r->hwm = 0;
    return h;
}

#endif // SAMPLE_RING_H
