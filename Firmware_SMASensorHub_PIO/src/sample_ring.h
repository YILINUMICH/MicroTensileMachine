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
 * Ring capacity unchanged (1024 slots). 1024 × 24 B = 24 KB, still
 * comfortably inside the 32 KB SRAM4 partition.
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
//   6..7   | Reserved for additional sensor channels  | —
//   0xF0+  | State-machine events (not sample data)   | Phase 6
//
// The ring's per-src seq counter array (see below) is sized for src
// indices 0..7. Adding src values ≥ 8 requires growing seq_per_src[].
// Phase 6 state-machine events at 0xF0+ live on a separate channel
// (RPC or a dedicated event ring) — they do not consume sample slots.

#define SAMPLE_SRC_LASER     1
#define SAMPLE_SRC_LOAD      2
#define SAMPLE_SRC_SMA_V     3
#define SAMPLE_SRC_SMA_I     4
#define SAMPLE_SRC_SMA_R     5
#define SAMPLE_SRC_MAX       7   // largest src index that fits seq_per_src[]


// ══════════════════════════════════════════════════════════════════════
//  Sample struct — one ADC reading
// ══════════════════════════════════════════════════════════════════════

struct AdcSample {
    uint32_t hw_us;           // free-running µs at sample acquisition
    uint32_t seq;             // per-src monotonic sequence number
    uint32_t timestamp_ms;    // millis() at read time (legacy / host parser)
    uint8_t  src;             // see `src` ID reservations above
    uint8_t  _pad8[3];        // explicit padding (raw_code aligned to 4)
    int32_t  raw_code;        // signed ADC code
    float    voltage_V;       // scaled voltage
};

// 4 + 4 + 4 + 1 + 3 + 4 + 4 = 24 bytes — naturally aligned, no waste.
static_assert(sizeof(AdcSample) == 24, "AdcSample must be 24 bytes");


// ══════════════════════════════════════════════════════════════════════
//  Ring buffer — lives at a fixed address in SRAM4
// ══════════════════════════════════════════════════════════════════════

// Capacity must be a power of 2 for efficient masking.
// 1024 slots × 24 bytes = 24 KB of sample data.
// At 2 kSPS combined (1 kSPS per ADC) the ring buffers ~0.5 s of data
// before overflow — plenty of headroom for USB-CDC hiccups on M7.
static const uint32_t RING_CAPACITY = 1024;
static const uint32_t RING_MASK     = RING_CAPACITY - 1;

struct SampleRing {
    // Producer-only fields (M4 writes, M7 reads-only)
    volatile uint32_t write_idx;          // M4 increments after slot write
    volatile uint32_t dropped;            // M4 increments on overflow
    volatile uint32_t hwm;                // M4 tracks high-water mark;
                                          //   M7 reads-and-clears each [STATUS] window
    volatile uint32_t seq_per_src[8];     // M4-only seq counters (index by src)

    // Consumer-only fields (M7 writes, M4 reads-only)
    volatile uint32_t read_idx;           // M7 increments after slot consume

    // Padding to keep samples[] aligned to 8 bytes (AdcSample's largest
    // field is 4 bytes, so 4-byte alignment is required; 8 is safer).
    uint32_t          _pad[1];

    AdcSample         samples[RING_CAPACITY];
};

// Header: 4 + 4 + 4 + 32 + 4 + 4 = 52 bytes. Samples: 24 576 bytes.
// Total: 24 628 bytes ≈ 24 KB. Fits in the 32 KB SRAM4 partition.
static_assert(sizeof(SampleRing) <= 32768,
              "SampleRing must fit in the upper 32 KB of SRAM4");

// Ring base address: 32 KB into SRAM4.
// 0x38000000 – 0x38007FFF → reserved for OpenAMP / Arduino-RPC
// 0x38008000 – 0x3800FFFF → our ring buffer (32 KB available, need ~24 KB)
static const uintptr_t RING_BASE = 0x38008000;
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
