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
 * SensorHub_PIO, etc.).
 */

#ifndef SAMPLE_RING_H
#define SAMPLE_RING_H

#include <stdint.h>

// ══════════════════════════════════════════════════════════════════════
//  Sample struct — one ADC reading
// ══════════════════════════════════════════════════════════════════════

struct AdcSample {
    uint32_t timestamp_ms;    // millis() at read time
    uint8_t  src;             // 1 = ADC1, 2 = ADC2
    // 3 bytes implicit padding (compiler aligns raw_code to 4)
    int32_t  raw_code;        // signed ADC code
    float    voltage_V;       // scaled voltage
};

// 4 + 1 + (3 pad) + 4 + 4 = 16 bytes — naturally aligned, no waste.
static_assert(sizeof(AdcSample) == 16, "AdcSample must be 16 bytes");


// ══════════════════════════════════════════════════════════════════════
//  Ring buffer — lives at a fixed address in SRAM4
// ══════════════════════════════════════════════════════════════════════

// Capacity must be a power of 2 for efficient masking.
// 1024 slots × 16 bytes = 16 KB of sample data.
static const uint32_t RING_CAPACITY = 1024;
static const uint32_t RING_MASK     = RING_CAPACITY - 1;

struct SampleRing {
    volatile uint32_t write_idx;   // only M4 increments
    volatile uint32_t read_idx;    // only M7 increments
    volatile uint32_t dropped;     // only M4 increments (overflow counter)
    uint32_t          _pad;        // align samples[] to 16-byte boundary
    AdcSample         samples[RING_CAPACITY];
};

// Header (16 bytes) + samples (16 384 bytes) = 16 400 bytes ≈ 16 KB.
static_assert(sizeof(SampleRing) <= 32768,
              "SampleRing must fit in the upper 32 KB of SRAM4");

// Ring base address: 32 KB into SRAM4.
// 0x38000000 – 0x38007FFF → reserved for OpenAMP / Arduino-RPC
// 0x38008000 – 0x3800FFFF → our ring buffer (32 KB available, need ~16 KB)
static const uintptr_t RING_BASE = 0x38008000;
static const uintptr_t SRAM4_END = 0x38010000;   // 0x38000000 + 64 KB

static_assert(RING_BASE + sizeof(SampleRing) <= SRAM4_END,
              "SampleRing overflows SRAM4");

#define SAMPLE_RING  (reinterpret_cast<volatile SampleRing*>(RING_BASE))


// ══════════════════════════════════════════════════════════════════════
//  Producer API — called from M4 loop() only
// ══════════════════════════════════════════════════════════════════════

/**
 * Push one sample into the ring.  Returns true on success, false if the
 * ring is full (sample is dropped and the overflow counter incremented).
 * Never blocks.
 */
inline bool ring_push(volatile SampleRing* r,
                      uint32_t ts, uint8_t src,
                      int32_t raw, float volts) {
    uint32_t wr = r->write_idx;
    // Full when the producer has lapped the consumer by RING_CAPACITY.
    if ((wr - r->read_idx) >= RING_CAPACITY) {
        r->dropped++;
        return false;
    }
    volatile AdcSample& slot = r->samples[wr & RING_MASK];
    slot.timestamp_ms = ts;
    slot.src           = src;
    slot.raw_code      = raw;
    slot.voltage_V     = volts;
    __DMB();                    // data committed before index is visible
    r->write_idx = wr + 1;
    return true;
}


// ══════════════════════════════════════════════════════════════════════
//  Consumer API — called from M7 loop() only
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
    out.timestamp_ms = slot.timestamp_ms;
    out.src           = slot.src;
    out.raw_code      = slot.raw_code;
    out.voltage_V     = slot.voltage_V;
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

#endif // SAMPLE_RING_H
