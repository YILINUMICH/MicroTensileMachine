/**
 * @file main.cpp  (Calibrate_LaserHead — dual-ADC calibration firmware)
 *
 * Purpose-built firmware for the IL-030 → ADS1263 calibration workflow in
 * Calibrate_LaserHead/. BOTH ADC1 and ADC2 sample the Keyence IL-030 analog
 * output on AIN4/AIN5 at the same time. ADC2 is the primary channel — its
 * fit becomes the production k / V₀ that propagates into Experiment_SMACharacterizationV2/
 * and session.py. ADC1 is an independent digital-path cross-check; two ADCs
 * converging on the same fit catches ADC2-specific driver bugs and register-
 * config errors that a single-channel measurement cannot.
 *
 *
 * IPC architecture (2026-05-27 — ring buffer):
 *
 *   Previous: M4 → RPC.print() → M7 → Serial.write() → USB
 *     Problem: RPC.print() is synchronous. At ~660 msg/s (dual ADC, 3 ms
 *     poll) M4 blocks waiting for M7 to acknowledge each message. If M7
 *     stalls briefly on a USB interrupt, the back-pressure cascades and
 *     crashes M4 mid-run.
 *
 *   Current:  M4 → ring buffer (SRAM4) → M7 → Serial.print() → USB
 *     M4 writes ADC samples into a lock-free ring buffer in shared SRAM4.
 *     M7 drains the ring at its own pace and formats TSV for USB Serial.
 *     Neither core ever blocks on the other — USB back-pressure on M7
 *     is absorbed by the 1024-slot ring (~1.3 s at 800 SPS combined).
 *     RPC is retained for boot-time checkpoint messages only (infrequent,
 *     no throughput concern).
 *
 *   See sample_ring.h for the ring buffer implementation and SRAM4
 *   placement rationale.
 *
 *
 * Where this fits in the project firmware family (2026-05-26):
 *
 *   ┌──────────────────────────────┐
 *   │ Firmware_SensorHub_PIO/               │  Production rig: load (ADC1 on AIN2/3)
 *   │   + load + laser dual-stream │                  + laser (ADC2 on AIN4/5)
 *   └──────────────────────────────┘  Untouched by the calibration workflow.
 *
 *   ┌──────────────────────────────┐
 *   │ LaserHead_PIO/               │  Laser-only firmware sibling.
 *   │   ADC2 only on AIN4/5        │  (Currently flashed dual-ADC by default,
 *   └──────────────────────────────┘   but the production intent is single-ADC.)
 *
 *   ┌──────────────────────────────┐
 *   │ Calibrate_LaserHead/         │  ⟵ THIS PROJECT
 *   │   Calibrate_LaserHead_PIO/   │  Calibration-purpose firmware. Dual-ADC
 *   │   src/main.cpp               │  on AIN4/5. Ring-buffer IPC (M4→M7).
 *   │                              │  Consumed by run_calibration.py with
 *   │                              │  xcompare=true.
 *   └──────────────────────────────┘
 *
 *
 * Hardware target (post-port 2026-05-26):
 *   - Portenta H7 on the Mid Carrier (ASX00055)
 *   - Bare TI ADS1263 EVM
 *   - External REF7050 (+5 V) on AIN0(+) / AIN1(-)            [Cable 2]
 *   - Keyence IL-030 analog out  on AIN4(+) / AIN5(-)         [Cable 4]
 *     (per doc/MEMO_cable_map.md, matching Firmware_SensorHub_PIO production wiring,
 *      so calibration constants apply directly to the production rig.)
 *
 * Signal chain (this build):
 *   Keyence IL-030 (0–5 V single-ended) → AIN4(+) / AIN5(-)
 *     → ADC1 (32-bit, 400 SPS, Sinc3, PGA gain=1, REF7050)  [INPMUX  = 0x45]
 *     → ADC2 (24-bit, 400 SPS, Sinc3,     gain=1, REF7050)  [ADC2MUX = 0x45]
 *   Both ADCs free-run at the same SPS on independent clocks. Timestamp
 *   alignment between channels is bounded by jitter only.
 *
 * Driver provenance:
 *   lib/ADS1263/ was copied from LaserHead_PIO/lib/ADS1263/ on 2026-05-26.
 *   That driver in turn came from Firmware_SensorHub_PIO and carries both bug fixes
 *   (RDATA2 6-byte frame, ADC2CFG REF2/GAIN2 field order) and the Mid Carrier
 *   pin defines (PA_8/PC_6/PC_7). See doc/ADS1263_H7_Integration_Notes.md
 *   §4 addenda for bug write-ups.
 *
 * Output stream format (tab-separated, one line per sample):
 *   <t_ms>\t<src>\t<raw_code>\t<voltage_V>     (src=1 ADC1, src=2 ADC2)
 *
 * Consumed by Calibrate_LaserHead/portenta_reader.py
 *   → PortentaReader.read_samples_dual()
 *   → run_calibration.py with xcompare:true in config.yaml
 *   → analyze.py produces per-channel fits + agreement metric.
 *
 *
 * What cross-compare DOES catch:
 *   - ADC2-specific driver bugs (e.g. the RDATA2 5-vs-6 byte frame issue
 *     fixed in Firmware_SensorHub_PIO 2026-05-25, ADC2CFG field swap fixed same day)
 *   - ADC2 register-config errors (wrong DR2 / GAIN2, accidentally landing
 *     on the internal 2.5 V reference instead of REF7050)
 *   - Any asymmetric handling of the same physical signal
 *
 * What it does NOT catch:
 *   - REF7050 voltage error (both ADCs share the same external reference —
 *     they drift together)
 *   - Front-end wiring errors at AIN4/AIN5
 *   - Beam-axis ↔ stage-axis cosine error
 *   - IL-030 sensor itself
 *
 * Treat ADC2 ↔ ADC1 agreement as a digital-path sanity check, not an
 * end-to-end measurement validation. The Zaber stage is still the
 * ground truth for displacement.
 *
 *
 * To revert to single-ADC laser-only (e.g. for a quick read-only smoke
 * test): flip ENABLE_ADC1 back to 0 below. The stream then becomes the
 * 3-column form (`<t_ms>\t<raw>\t<V>`) and Calibrate_LaserHead's parser
 * handles it transparently.
 *
 *
 * Flash order (first time):
 *   pio run -e portenta_m7_bridge -t upload
 *   pio run -e portenta_m4        -t upload
 *   pio device monitor
 *
 * → Power-cycle the rig (USB + EVM supply) after every upload — the dfu
 *   reset does not cleanly re-power the EVM's analog rails (the on-board
 *   TPS7A4700 LDO needs a full power-on transient to settle), and the
 *   ADS1263 will come up with ID=0x00 if you skip it.
 */

#include <Arduino.h>
#include "RPC.h"
#include "sample_ring.h"

// ── Enable/disable each ADC path at build time ─────────────────────────
// Shared between M7 (output formatting) and M4 (ADC control).
// Calibration default: BOTH ADCs sample AIN4/AIN5 simultaneously. ADC2
// produces the calibration k/V₀ that propagates to production; ADC1 is
// the independent digital-path cross-check.
//
// To revert to single-ADC laser-only: flip ENABLE_ADC1 to 0. The stream
// then becomes the 3-column form and Calibrate_LaserHead's parser handles
// it without changes (cross-compare metrics simply won't be reported).
#define ENABLE_ADC1   1
#define ENABLE_ADC2   1


// ══════════════════════════════════════════════════════════════════════
//  M7 CORE — drain ring buffer + RPC boot messages → USB Serial
// ══════════════════════════════════════════════════════════════════════
#if defined(CORE_CM7)

void setup() {
    // Zero the ring buffer BEFORE booting M4 (RPC.begin() starts the
    // M4 core). SRAM4 may contain garbage from a previous run; clearing
    // the header ensures M4 starts with write_idx == read_idx == 0.
    SAMPLE_RING->write_idx = 0;
    SAMPLE_RING->read_idx  = 0;
    SAMPLE_RING->dropped   = 0;
    __DMB();

    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}

    RPC.begin();     // boots M4 core

    // Banner identifies this as the calibration build so the operator
    // knows they didn't flash LaserHead_PIO or Firmware_SensorHub_PIO by mistake.
    Serial.println("[M7] bridge up — ring-buffer IPC, forwarding to USB Serial (Calibrate_LaserHead)");
}

static uint32_t last_drop_report = 0;

void loop() {
    // ── 1. Drain boot / diagnostic text from M4 via RPC ───────────────
    //    Once M4 enters its main loop, RPC traffic drops to zero —
    //    all sample data flows through the ring buffer instead.
    while (RPC.available()) {
        Serial.write(RPC.read());
    }

    // ── 2. Drain ADC samples from the shared-memory ring buffer ───────
    //    Format each sample as TSV, identical to the legacy RPC output
    //    Phase 5: per-sample TSV gained hw_us + seq columns. The host
    //    parser regex tolerates trailing columns, so older logs still
    //    parse identically.
    //      <t_ms>\t<src>\t<raw>\t<V>\t<hw_us>\t<seq>   (dual-ADC)
    //      <t_ms>\t<raw>\t<V>\t<hw_us>\t<seq>           (single-ADC)
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
        batch++;
    }

    // ── 3. Periodic overflow diagnostic ───────────────────────────────
    //    If M4 had to drop samples because M7 fell behind, report it.
    //    Checked every 10 s to avoid spamming the stream.
    uint32_t now = millis();
    if (now - last_drop_report > 10000) {
        last_drop_report = now;
        uint32_t d = SAMPLE_RING->dropped;
        if (d > 0) {
            Serial.print("[M7] WARNING: ring overflow — ");
            Serial.print(d);
            Serial.println(" samples dropped since boot");
        }
    }
}


// ══════════════════════════════════════════════════════════════════════
//  M4 CORE — drive the ADS1263 (both ADCs on AIN4/AIN5) and stream
//            to M7 via shared-memory ring buffer
// ══════════════════════════════════════════════════════════════════════
#elif defined(CORE_CM4)

#include <SPI.h>
#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// Checkpoint macro — same convention as the sibling *_PIO projects.
// Boot diagnostics still go through RPC (infrequent, no throughput
// concern). Only the ADC sample stream uses the ring buffer.
#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

// Sample periods for each ADC path (timed polling — no DRDY gating).
// Both ADCs run at 400 SPS (2.5 ms native period). Polling at 2 ms
// is faster than the conversion period, so every conversion is
// captured. Some polls re-read the previous data register value
// (no new conversion yet) — those are still valid and get pushed to
// the ring. The duplicates are harmless for calibration averaging
// and contribute to stress-testing the ring buffer at ~1000 msg/s
// combined (the rate that previously crashed the RPC path at 660).
//
// Without DRDY gating, this is the only way to guarantee the full
// 400 unique SPS per ADC. The ~25% duplicate overhead washes out
// in the per-point mean (identical values don't shift the average).
static const uint32_t ADC1_POLL_MS = 2;
static const uint32_t ADC2_POLL_MS = 2;

void setup() {
    // RPC first so we can report progress to the M7 bridge.
    RPC.begin();
    delay(500);
    CP(0, "RPC up");

    Serial.begin(115200);
    CP(1, "Serial.begin done");

    // Calibration-build banner — operator should see this BEFORE the
    // power-up settle delay below so they know they flashed the right
    // firmware variant.
    RPC.println("[M4] *** Calibrate_LaserHead_PIO — dual-ADC cross-compare on AIN4/AIN5 ***");
    RPC.println("[M4] IPC: shared-memory ring buffer (sample_ring.h)");
    RPC.println("[M4] consumed by Calibrate_LaserHead/run_calibration.py (xcompare:true)");

    // ADS1263 power-up settle — required on every cold boot. The dfu
    // reset doesn't cleanly re-power the EVM's analog rails (the
    // on-board TPS7A4700 LDO needs a full power-on transient to
    // settle), so give the chip time to come out of reset before we
    // talk SPI to it.
    RPC.println("[M4] waiting 3000 ms for ADS1263 to power up...");
    delay(3000);
    RPC.println("[M4] ADS1263 power-up settle done");

    // Drive the ADS1263 pins BEFORE adc.begin() so we can localise any
    // pinMode/port-clock hang. Pins per the driver header — Mid Carrier
    // J15 positions, matching Firmware_SensorHub_PIO + LaserHead_PIO.
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

    // ── Configure ADC1 ─────────────────────────────────────────────────
    // ADC1 set to the SAME input pair as ADC2 (AIN4/AIN5) so both ADCs
    // digitise the IL-030 signal in parallel. PGA in path at gain=1
    // (matches Firmware_SensorHub_PIO production load-cell config — same noise
    // floor characteristics, ~1.3 µV RMS at 400 SPS). VBIAS keeps AINCOM
    // at AVDD/2 ≈ 2.6 V so the IL-030 input common-mode sits inside
    // the PGA's [0.3, AVDD-0.3] window.
    //
    //   INPMUX = 0x45                       → AIN4(+) / AIN5(-)
    //   REFMUX = ADS1263_REFMUX_EXT_AIN01   → 0x09, REF7050 on AIN0/AIN1
    //   VREF   = 5.0 V                      → REF7050 nominal
    //   rate   = 400 SPS                    → match ADC2 for trivial alignment
    //   PGA    = in path, gain=1 (pga_bypass=false) → MODE2 = 0x08
#if ENABLE_ADC1
    adc.configureADC1(
        /*inpmux     =*/ 0x45,                       // AIN4(+) / AIN5(-) — same as ADC2 for x-compare
        /*refmux     =*/ ADS1263_REFMUX_EXT_AIN01,   // 0x09 — REF7050 on AIN0/AIN1 (shared with ADC2)
        /*vref_V     =*/ 5.0f,
        /*rate       =*/ ADS1263_400SPS,
        /*pga_bypass =*/ false                       // PGA in path, gain=1
    );
    adc.startADC1();
    CP(9, "ADC1 started on AIN4/AIN5 (x-compare partner of ADC2), REF7050, PGA in path gain=1");
#endif

    // ── Configure ADC2 ─────────────────────────────────────────────────
    // Production routing: Keyence IL-030 laser controller analog output on
    // AIN4(+) / AIN5(-) (Cable 4 in doc/MEMO_cable_map.md). Mirrors the
    // Firmware_SensorHub_PIO production firmware byte-for-byte so calibration
    // constants apply directly to the production rig with no signal-chain
    // translation.
    //   ADC2MUX = 0x45 → AIN4(+) / AIN5(-)
    //   REF2    = 001b (external REF7050 on AIN0/AIN1, shared with ADC1)
    //   rate    = 400 SPS
    //   gain    = 1x   (IL-030 already drives the full ±5 V analog range;
    //                   ADC2's PGA cannot be bypassed — runs as unity buffer)
#if ENABLE_ADC2
    adc.configureADC2(
        /*adc2mux =*/ 0x45,                           // AIN4(+) / AIN5(-)
        /*ref2    =*/ ADS1263_ADC2_REF_AIN01,         // external REF7050 on AIN0/AIN1
        /*vref_V  =*/ 5.0f,
        /*rate    =*/ ADS1263_ADC2_400SPS,
        /*gain    =*/ ADS1263_ADC2_GAIN_1
    );
    adc.startADC2();
    CP(10, "ADC2 started on AIN4/AIN5, REF7050, 400 SPS gain=1");
#endif

    delay(100);   // one filter-settle interval

    adc.printConfig();

    // Output format line — with both ADCs active, every line carries a
    // src column so the host can demultiplex the two streams via
    // PortentaReader.read_samples_dual().
#if ENABLE_ADC1 && ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\tsrc\\traw_code\\tvoltage_V   (src=1 ADC1, src=2 ADC2 — both AIN4/5)");
#elif ENABLE_ADC1
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw_code\\tvoltage_V   (ADC1 only on AIN4/5)");
#elif ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw_code\\tvoltage_V   (ADC2 only on AIN4/5)");
#else
    #error "Neither ENABLE_ADC1 nor ENABLE_ADC2 is set — nothing to do."
#endif
}

void loop() {
    // Independent timed polling for each enabled ADC. With both enabled
    // they interleave on the SPI bus; each read is its own CS-low →
    // CS-high SPI transaction so there is no arbitration to worry about.
    //
    // ring_push() never blocks — if the ring is full (M7 fell behind),
    // the sample is dropped and the overflow counter incremented. M7
    // reports the count periodically so the operator can see it.

    // Phase 5 ring layout: ring_push now takes (hw_us, ts_ms, src, ...).
    // We capture micros() right before the SPI fetch so the slot's
    // hw_us reflects the read instant within a few µs. Cross-compare
    // analysis can compute per-sample jitter from these timestamps.

#if ENABLE_ADC1
    static uint32_t t1_last = 0;
    if (millis() - t1_last >= ADC1_POLL_MS) {
        t1_last = millis();
        uint32_t hw_us = micros();
        ADC_Reading r = adc.readADC1Direct();
        if (r.valid) {
            ring_push(SAMPLE_RING, hw_us, millis(),
                      SAMPLE_SRC_LASER, r.raw_code, r.voltage_V);
        }
    }
#endif

#if ENABLE_ADC2
    static uint32_t t2_last = 0;
    if (millis() - t2_last >= ADC2_POLL_MS) {
        t2_last = millis();
        uint32_t hw_us = micros();
        ADC_Reading r = adc.readADC2Direct();
        if (r.valid) {
            ring_push(SAMPLE_RING, hw_us, millis(),
                      SAMPLE_SRC_LOAD, r.raw_code, r.voltage_V);
        }
    }
#endif
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
