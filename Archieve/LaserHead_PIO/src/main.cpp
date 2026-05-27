/**
 * @file main.cpp  (Portenta H7 dual-core — laser displacement, dual-ADC x-compare)
 *
 * Calibration-purpose firmware. BOTH ADCs of the ADS1263 sample the
 * Keyence IL-030 analog output simultaneously on AIN4/AIN5 — ADC2 is
 * the production-path measurement that becomes the calibration k/V₀,
 * ADC1 is an independent digital-path cross-check. Two ADCs agreeing
 * on the same physical signal is stronger evidence of a trustworthy
 * calibration than a single-channel measurement alone.
 *
 * The sibling SensorHub_PIO is the production firmware (load cell on
 * ADC1/AIN2-3, laser on ADC2/AIN4-5). This project exists so the
 * calibration can run dual-ADC on the SAME input without disturbing
 * production wiring or SensorHub_PIO's firmware state.
 *
 * Hardware target (post-port 2026-05-26):
 *   - Portenta H7 on the Mid Carrier (ASX00055)
 *   - Bare TI ADS1263 EVM
 *   - External REF7050 (+5 V) on AIN0(+) / AIN1(-)        [Cable 2]
 *   - Keyence IL-030 analog out  on AIN4(+) / AIN5(-)     [Cable 4]
 *     (per doc/MEMO_cable_map.md, matching SensorHub_PIO production)
 *
 * Signal chain (this build):
 *   Keyence IL-030 (0–5 V single-ended) → AIN4(+) / AIN5(-)
 *     → ADC1 (32-bit, 400 SPS, Sinc3, PGA gain=1, REF7050)  [INPMUX = 0x45]
 *     → ADC2 (24-bit, 400 SPS, Sinc3, gain=1,    REF7050)   [ADC2MUX = 0x45]
 *   Both ADCs free-run on independent clocks but at the same SPS, so
 *   timestamp alignment between channels is bounded by jitter only.
 *
 * Driver provenance:
 *   lib/ADS1263/ was copied wholesale from SensorHub_PIO on 2026-05-26
 *   to inherit both bug fixes (RDATA2 6-byte frame, ADC2CFG REF2/GAIN2
 *   field order) and the Mid Carrier pin defines (PA_8/PC_6/PC_7).
 *   See doc/ADS1263_H7_Integration_Notes.md §4 addenda for the bug
 *   write-ups. Old driver backed up under lib/ADS1263/.backup_pre_port_*.
 *
 * Output stream format (tab-separated, one line per sample):
 *   <t_ms>\t<src>\t<raw_code>\t<voltage_V>     (src=1 ADC1, src=2 ADC2)
 * Host parser: Calibrate_LaserHead/portenta_reader.py handles this
 * 4-column form; PortentaReader.read_samples_dual() demuxes the two
 * streams into separate sample lists for parallel fitting.
 *
 * What cross-compare DOES catch: ADC2-specific driver bugs, register-
 * config errors (wrong DR2, wrong GAIN2, accidentally landing on the
 * internal 2.5 V ref), any asymmetric handling of the same signal.
 * What it does NOT catch: REF7050 voltage error (both ADCs share the
 * same external reference — both drift in lockstep), front-end wiring
 * errors, beam-axis cosine error, sensor issues. Treat agreement as a
 * digital-path sanity check, not an end-to-end validation.
 *
 * To revert to single-ADC laser-only: flip ENABLE_ADC1 back to 0
 * below. The stream then becomes the 3-column form
 * (`<t_ms>\t<raw>\t<V>`) and the host parser handles it transparently.
 *
 * Flash order (first time):
 *   pio run -e portenta_m7_bridge -t upload
 *   pio run -e portenta_m4        -t upload
 *   pio device monitor
 *
 * → Power-cycle the rig (USB + EVM supply) after every upload — the
 *   dfu reset does not cleanly re-power the EVM's analog rails (the
 *   on-board TPS7A4700 LDO needs a full power-on transient to settle),
 *   and the ADS1263 will come up with ID=0x00 if you skip it.
 */

#include <Arduino.h>
#include "RPC.h"

// ══════════════════════════════════════════════════════════════════════
//  M7 CORE — bridge RPC ↔ USB Serial
// ══════════════════════════════════════════════════════════════════════
#if defined(CORE_CM7)

void setup() {
    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}
    RPC.begin();
    Serial.println("[M7] bridge up — forwarding RPC to USB Serial (laser head)");
}

void loop() {
    while (RPC.available()) {
        Serial.write(RPC.read());
    }
}

// ══════════════════════════════════════════════════════════════════════
//  M4 CORE — drive the ADS1263 (ADC2 only in this build) and stream
//           to M7 via RPC
// ══════════════════════════════════════════════════════════════════════
#elif defined(CORE_CM4)

#include <SPI.h>
#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// ── Enable/disable each ADC path at build time ─────────────────────────
// Cross-compare configuration (2026-05-26): BOTH ADCs sample AIN4/AIN5
// simultaneously. ADC2 produces the calibration k/V₀ that propagates to
// production; ADC1 is the digital-path cross-check — its independent fit
// catches ADC2-specific driver bugs, register-config errors, or any
// asymmetric handling of the same physical signal. See
// Calibrate_LaserHead/MEMO_session_2026-05-26.md for the deferred-→active
// scope change. ADC1's INPMUX in the configure block below is set to
// 0x45 to match ADC2MUX = 0x45.
//
// To revert to single-ADC laser-only: flip ENABLE_ADC1 back to 0. The
// stream then becomes the 3-column form (`t_ms\traw\tV`) and the host
// parser handles it without changes.
#define ENABLE_ADC1   1
#define ENABLE_ADC2   1

// Checkpoint macro — same convention as LoadCell_PIO.
#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

// Sample periods for each ADC path (timed polling — no DRDY gating).
// Both ADCs run at 400 SPS (2.5 ms native period) when enabled. Polling
// at 3 ms keeps us one sample interval behind the chip without
// overrunning the data register; matches SensorHub_PIO so the two
// projects share register/timing assumptions.
static const uint32_t ADC1_POLL_MS = 3;
static const uint32_t ADC2_POLL_MS = 3;

void setup() {
    // RPC first so we can report progress to the M7 bridge.
    RPC.begin();
    delay(500);
    CP(0, "RPC up");

    Serial.begin(115200);
    CP(1, "Serial.begin done");

    // ADS1263 power-up settle — required on every cold boot. The dfu
    // reset doesn't cleanly re-power the EVM's analog rails (the
    // on-board TPS7A4700 LDO needs a full power-on transient to
    // settle), so give the chip time to come out of reset before we
    // talk SPI to it.
    RPC.println("[M4] waiting 3000 ms for ADS1263 to power up...");
    delay(3000);
    RPC.println("[M4] ADS1263 power-up settle done");

    // Drive the ADS1263 pins BEFORE adc.begin() so we can localise any
    // pinMode/port-clock hang. Pins per the post-port driver header
    // (Mid Carrier J15 positions, matching SensorHub_PIO).
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

    // ── Configure ADC1 (cross-compare diagnostic — disabled by default) ─
    // In production this firmware is laser-only; ADC1 is parked on
    // AINCOM/AINCOM by the driver's begin() call. The block below is
    // here for the future ENABLE_ADC1_XCOMPARE follow-on: both ADCs
    // sampling AIN4/AIN5 simultaneously gives a digital-path cross-
    // check on the IL-030 signal chain.
    //
    // To do a cross-compare run: flip ENABLE_ADC1 to 1 above and set
    // INPMUX = 0x45 below so ADC1 sees the same pair as ADC2. The
    // host parser will then see 4-column rows tagged with src=1/src=2
    // (both pointing at AIN4/AIN5) and Calibrate_LaserHead can fit
    // both streams for an agreement metric.
    //
    //   INPMUX = 0x45                       → AIN4(+) / AIN5(-)
    //   REFMUX = ADS1263_REFMUX_EXT_AIN01   → 0x09, REF7050 on AIN0/AIN1
    //   VREF   = 5.0 V                      → REF7050 nominal
    //   rate   = 400 SPS                    → match ADC2 for trivial alignment
    //   PGA    = in path, gain=1 → MODE2 = 0x08
#if ENABLE_ADC1
    adc.configureADC1(
        /*inpmux     =*/ 0x45,                       // AIN4(+) / AIN5(-) — match ADC2 for x-compare
        /*refmux     =*/ ADS1263_REFMUX_EXT_AIN01,   // 0x09 — REF7050 on AIN0/AIN1
        /*vref_V     =*/ 5.0f,
        /*rate       =*/ ADS1263_400SPS,
        /*pga_bypass =*/ false                       // PGA in path, gain=1 (matches SensorHub_PIO production)
    );
    adc.startADC1();
    CP(9, "ADC1 started on AIN4/AIN5 (cross-compare with ADC2), REF7050, PGA in path gain=1");
#endif

    // ── Configure ADC2 (laser head — primary path) ─────────────────────
    // Production routing: Keyence IL-030 laser controller analog output on
    // AIN4(+) / AIN5(-) (Cable 4 in doc/MEMO_cable_map.md), matching the
    // SensorHub_PIO production firmware so the calibration constants
    // derived here apply directly to the production rig with no
    // signal-chain translation.
    //   ADC2MUX = 0x45 → AIN4(+) / AIN5(-)
    //   REF2    = 001b (external REF7050 on AIN0/AIN1, shared if ADC1 on)
    //   rate    = 400 SPS (matches SensorHub_PIO; previously 100)
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

    // Output format lines — describe only the channels we enabled.
#if ENABLE_ADC1 && ENABLE_ADC2
    RPC.println("[M4] streaming. format: t_ms\\tsrc\\traw_code\\tvoltage_V   (src=1 or 2)");
#elif ENABLE_ADC2
    RPC.println("[M4] streaming. format: t_ms\\traw_code\\tvoltage_V   (ADC2/laser)");
#elif ENABLE_ADC1
    RPC.println("[M4] streaming. format: t_ms\\traw_code\\tvoltage_V   (ADC1/load)");
#else
    #error "Neither ENABLE_ADC1 nor ENABLE_ADC2 is set — nothing to do."
#endif
}

void loop() {
    // Independent timed polling for each enabled ADC. With both enabled
    // they interleave on the SPI bus; each read is its own CS-low→CS-high
    // transaction so there is no arbitration to worry about.

#if ENABLE_ADC1
    static uint32_t t1_last = 0;
    if (millis() - t1_last >= ADC1_POLL_MS) {
        t1_last = millis();
        ADC_Reading r = adc.readADC1Direct();
        if (r.valid) {
  #if ENABLE_ADC2
            RPC.print(millis()); RPC.print('\t');
            RPC.print(1);        RPC.print('\t');       // src = 1 (ADC1)
  #else
            RPC.print(millis()); RPC.print('\t');
  #endif
            RPC.print(r.raw_code);
            RPC.print('\t');
            RPC.println(r.voltage_V, 6);
        }
    }
#endif

#if ENABLE_ADC2
    static uint32_t t2_last = 0;
    if (millis() - t2_last >= ADC2_POLL_MS) {
        t2_last = millis();
        ADC_Reading r = adc.readADC2Direct();
        if (r.valid) {
  #if ENABLE_ADC1
            RPC.print(millis()); RPC.print('\t');
            RPC.print(2);        RPC.print('\t');       // src = 2 (ADC2)
  #else
            RPC.print(millis()); RPC.print('\t');
  #endif
            RPC.print(r.raw_code);
            RPC.print('\t');
            RPC.println(r.voltage_V, 6);
        }
    }
#endif
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
