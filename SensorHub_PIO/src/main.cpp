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
 * IPC architecture (ring buffer):
 *
 *   Previous: M4 → RPC.print() → M7 → Serial.write() → USB
 *     Problem: RPC.print() is synchronous. At ~660 msg/s (dual ADC, 3 ms
 *     poll) M4 blocks waiting for M7 to acknowledge each message. If M7
 *     stalls briefly on a USB interrupt, the back-pressure cascades and
 *     crashes M4 mid-run. Same failure mode the calibration modules hit.
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
 *   placement rationale (shared verbatim with the Calibrate_*_PIO
 *   firmware variants).
 *
 *
 * Output stream format (tab-separated, one line per sample):
 *   <t_ms>\t<src>\t<raw_code>\t<voltage_V>
 * where src = 1 for ADC1 (laser) and src = 2 for ADC2 (load). The
 * host-side parser in Calibrate_LaserHead/portenta_reader.py already
 * handles this 4-column form and filters by adc_source.
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
    Serial.println("[M7] bridge up — ring-buffer IPC, forwarding to USB Serial (SensorHub)");
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
    //    so the host-side parsers need no changes:
    //      <t_ms>\t<src>\t<raw_code>\t<voltage_V>   (dual-ADC)
    //      <t_ms>\t<raw_code>\t<voltage_V>           (single-ADC)
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
        Serial.println(s.voltage_V, 6);
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
//  M4 CORE — drive the ADS1263 (both ADCs) and stream to M7 via
//            shared-memory ring buffer
// ══════════════════════════════════════════════════════════════════════
#elif defined(CORE_CM4)

#include <SPI.h>
#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// Checkpoint macro — same convention as the sibling projects.
// Boot diagnostics still go through RPC (infrequent, no throughput
// concern). Only the ADC sample stream uses the ring buffer.
#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

// Sample periods for each ADC path (timed polling — no DRDY gating).
// Both ADCs run at 400 SPS (2.5 ms native period). Polling at 2 ms is
// faster than the conversion period, so every conversion is captured.
// Some polls re-read the previous data register value (no new
// conversion yet) — those are still valid and get pushed to the ring.
// The ring buffer absorbs the higher effective rate.
static const uint32_t ADC1_POLL_MS = 2;
static const uint32_t ADC2_POLL_MS = 2;

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
    RPC.println("[M4] *** SensorHub_PIO — dual-ADC production stream ***");
    RPC.println("[M4]   ADC1 → AIN4/AIN5 (Keyence IL-030 laser)");
    RPC.println("[M4]   ADC2 → AIN2/AIN3 (LCA-9PC load cell)");
    RPC.println("[M4] IPC: shared-memory ring buffer (sample_ring.h)");

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
    // REF7050 drives AIN0(+)/AIN1(-) as the external reference; the
    // Keyence IL-030 laser controller analog output sits on AIN4(+)/AIN5(-).
    // MODE1 = Sinc3 (set in the driver) — same filter as the noise-floor
    // characterisation.
    //
    // PGA in path at gain=1 (production config). IL-030 already drives the
    // full ±5 V analog range; the chip PGA is here for the high-Z input
    // buffer and the lower noise floor (~1.3 µV vs ~3.5 µV RMS at 400 SPS).
    // VBIAS keeps AINCOM at AVDD/2 ≈ 2.6 V; the IL-030 controller ground on
    // AIN5 sets a working common-mode safely inside the PGA's
    // [0.3, AVDD-0.3] window so PGAL_ALM stays clear.
    //   INPMUX = 0x45                       → AIN4(+) / AIN5(-)
    //   REFMUX = ADS1263_REFMUX_EXT_AIN01   → 0x09, REF7050 on AIN0/AIN1
    //   VREF   = 5.0 V                      → REF7050 nominal (NOT AVDD)
    //   rate   = 400 SPS                    → laser bandwidth
    //   PGA    = in path, gain=1 (pga_bypass=false) → MODE2 = 0x08
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
    // Production routing: LCA-9PC load-cell amplifier output on
    // AIN2(+) / AIN3(-) (Cable 3 in doc/MEMO_cable_map.md). ADC2 shares the
    // external REF7050 (+5 V) reference with ADC1 — REF2 = 001b selects
    // AIN0(+REF) / AIN1(-REF) so both ADCs use the same numerator and
    // the volts-per-code math is consistent. 400 SPS matches ADC1 for
    // trivial host-side timestamp alignment; ADC2's filter is hardwired
    // Sinc3.
    //   ADC2MUX = 0x23 → AIN2(+) / AIN3(-)
    //   REF2    = 001b (external REF7050 on AIN0/AIN1)
    //   rate    = 400 SPS
    //   gain    = 1x  (LCA-9PC already supplies the gain)
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

    // Output format line — with both ADCs active, every line carries a
    // src column so the host can demultiplex the two streams.
#if ENABLE_ADC1 && ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\tsrc\\traw_code\\tvoltage_V   (src=1 laser, src=2 load)");
#elif ENABLE_ADC1
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw_code\\tvoltage_V   (ADC1/laser only)");
#elif ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw_code\\tvoltage_V   (ADC2/load only)");
#else
    #error "Neither ENABLE_ADC1 nor ENABLE_ADC2 is set — nothing to do."
#endif
}

void loop() {
    // Independent timed polling for each enabled ADC. Each read is its
    // own CS-low → CS-high SPI transaction so interleaving on the bus
    // requires no arbitration.
    //
    // ring_push() never blocks — if the ring is full (M7 fell behind),
    // the sample is dropped and the overflow counter incremented. M7
    // reports the count periodically so the operator can see it.

#if ENABLE_ADC1
    static uint32_t t1_last = 0;
    if (millis() - t1_last >= ADC1_POLL_MS) {
        t1_last = millis();
        ADC_Reading r = adc.readADC1Direct();
        if (r.valid) {
            ring_push(SAMPLE_RING, millis(), 1, r.raw_code, r.voltage_V);
        }
    }
#endif

#if ENABLE_ADC2
    static uint32_t t2_last = 0;
    if (millis() - t2_last >= ADC2_POLL_MS) {
        t2_last = millis();
        ADC_Reading r = adc.readADC2Direct();
        if (r.valid) {
            ring_push(SAMPLE_RING, millis(), 2, r.raw_code, r.voltage_V);
        }
    }
#endif
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
