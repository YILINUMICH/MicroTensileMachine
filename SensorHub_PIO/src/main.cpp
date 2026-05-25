/**
 * @file main.cpp  (Portenta H7 dual-core — SensorHub: dual ADC)
 *
 * Runs BOTH ADS1263 ADCs simultaneously on the same chip. Production
 * Phase 4 channel assignment (per README §Recommended configuration,
 * derived 2026-05-24 after cp7 retired the legacy AIN2/3 saturation
 * question and cp8 verified ADC2 clean):
 *
 *   - ADC1 (32-bit, 400 SPS, Sinc3, PGA=1) → AIN2(+) / AIN3(-)  [LCA-9PC load cell]
 *   - ADC2 (24-bit, 400 SPS, Sinc3, gain=1) → AIN4(+) / AIN5(-) [Keyence IL-030 laser]
 *
 * External REF7050 (+5 V) on AIN0(+)/AIN1(-) is shared by both ADCs
 * (REFMUX = 0x09 for ADC1, REF2 = 001 for ADC2).
 *
 * Merge of the sibling LoadCell_PIO (ADC1-only) and LaserHead_PIO
 * (ADC2-only) projects. Each ADC read is its own CS-low→CS-high SPI
 * transaction, so interleaving readADC1Direct() and readADC2Direct()
 * on independent timers requires no arbitration between the two paths.
 *
 * Output stream format (tab-separated, one line per sample):
 *   <t_ms>\t<src>\t<raw_code>\t<voltage_V>
 * where src = 1 for ADC1 (load) and src = 2 for ADC2 (laser). The
 * host-side parser in Calibrate_LaserHead/portenta_reader.py already
 * handles this 4-column form and filters by adc_source.
 *
 * Flash order (first time):
 *   pio run -e portenta_m7_bridge -t upload
 *   pio run -e portenta_m4        -t upload
 *   pio device monitor
 *
 * → Power-cycle the Hat Carrier after every upload.
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
    Serial.println("[M7] bridge up — forwarding RPC to USB Serial (SensorHub)");
}

void loop() {
    while (RPC.available()) {
        Serial.write(RPC.read());
    }
}

// ══════════════════════════════════════════════════════════════════════
//  M4 CORE — drive the ADS1263 (both ADCs) and stream to M7 via RPC
// ══════════════════════════════════════════════════════════════════════
#elif defined(CORE_CM4)

#include <SPI.h>
#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// ── Enable/disable each ADC path at build time ─────────────────────────
// Keeping these as flags (not plain constants) mirrors LaserHead_PIO so
// individual paths can be temporarily disabled for bring-up diagnostics
// without touching the loop() code.
#define ENABLE_ADC1   1
#define ENABLE_ADC2   1    // 2026-05-24: re-enabled. cp7 (AIN-pair scan) +
                           // cp8 (ADC2 cp) PASSED in ADS1263_FirstPowerUp_PIO/
                           // on the bare TI EVM — the legacy Waveshare-HAT
                           // AIN2/3 saturation does NOT reproduce here. ADC2
                           // measured 8.5 µV RMS at 100 SPS Sinc3 gain=1
                           // (datasheet typical 10.3 µV). Production assignment:
                           // load cell on ADC1/AIN2-AIN3, laser on ADC2/AIN4-AIN5.

// Checkpoint macro — same convention as the sibling projects.
#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

// Sample periods for each ADC path (timed polling — no DRDY gating).
// Both ADCs run at 400 SPS (2.5 ms native period). Polling at 3 ms keeps
// us one sample interval behind the chip without overrunning the data
// register; matching the SPS on both ADCs also makes host-side timestamp
// alignment trivial (see README §"Why 400 SPS on both").
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
    // reset doesn't cleanly re-power the HAT's 3.3 V LDO rail, so give
    // the chip time to come out of reset before we talk SPI to it.
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

    // ── Configure ADC1 ─────────────────────────────────────────────────
    // Post-EVM-modification configuration (see doc/ADS1263EVM_Modifications.md):
    // the REF7050 external reference now drives AIN0(+)/AIN1(-), so the
    // signal under test moves to AIN2(+)/AIN3(-). PGA stays in the path
    // for the low-noise input buffer; MODE1 is Sinc4 (set in the driver)
    // for best 50/60 Hz mains rejection at 400 SPS.
    //   INPMUX = 0x23                       → AIN2(+) / AIN3(-)
    //   REFMUX = ADS1263_REFMUX_EXT_AIN01   → 0x09, REF7050 on AIN0/AIN1
    //   VREF   = 5.0 V                      → matches the REF7050 output
    //   rate   = 400 SPS                    → load-cell bandwidth
    //   PGA    = enabled  (pga_bypass=false)
#if ENABLE_ADC1
    adc.configureADC1(
        /*inpmux     =*/ 0x23,                       // AIN2(+) / AIN3(-)
        /*refmux     =*/ ADS1263_REFMUX_EXT_AIN01,   // 0x09 — REF7050 on AIN0/AIN1
        /*vref_V     =*/ 5.0f,
        /*rate       =*/ ADS1263_400SPS,
        /*pga_bypass =*/ false                       // PGA enabled (do not rely on default)
    );
    adc.startADC1();
    CP(9, "ADC1 started on AIN2/AIN3, REF7050 on AIN0/AIN1, PGA enabled");
#endif

    // ── Configure ADC2 ─────────────────────────────────────────────────
    // Production routing: Keyence IL-030 laser controller analog output on
    // AIN4(+) / AIN5(-) (Cable 4 in doc/MEMO_cable_map.md). ADC2 shares the
    // external REF7050 (+5 V) reference with ADC1 — REF2 = 001b selects
    // AIN0(+REF) / AIN1(-REF) so both ADCs use the same numerator and
    // the volts-per-code math is consistent. 400 SPS matches ADC1 for
    // trivial host-side timestamp alignment (see README §"Why 400 SPS
    // on both"); ADC2's filter is hardwired Sinc3.
    //   ADC2MUX = 0x45 → AIN4(+) / AIN5(-)
    //   REF2    = 001b (external REF7050 on AIN0/AIN1)
    //   rate    = 400 SPS
    //   gain    = 1x  (IL-030 already drives the full ±5 V analog range)
#if ENABLE_ADC2
    adc.configureADC2(
        /*adc2mux =*/ 0x45,                           // AIN4(+) / AIN5(-)
        /*ref2    =*/ ADS1263_ADC2_REF_AIN01,         // external REF7050 on AIN0/AIN1
        /*vref_V  =*/ 5.0f,
        /*rate    =*/ ADS1263_ADC2_400SPS,
        /*gain    =*/ ADS1263_ADC2_GAIN_1
    );
    adc.startADC2();
    CP(10, "ADC2 started on AIN4/AIN5, REF7050 shared with ADC1, 400 SPS gain=1");
#endif

    delay(100);   // one filter-settle interval

    adc.printConfig();

    // Output format line — with both ADCs active, every line carries a
    // src column so the host can demultiplex the two streams.
#if ENABLE_ADC1 && ENABLE_ADC2
    RPC.println("[M4] streaming. format: t_ms\\tsrc\\traw_code\\tvoltage_V   (src=1 load, src=2 laser)");
#elif ENABLE_ADC1
    RPC.println("[M4] streaming. format: t_ms\\traw_code\\tvoltage_V   (ADC1/load only)");
#elif ENABLE_ADC2
    RPC.println("[M4] streaming. format: t_ms\\traw_code\\tvoltage_V   (ADC2/laser only)");
#else
    #error "Neither ENABLE_ADC1 nor ENABLE_ADC2 is set — nothing to do."
#endif
}

void loop() {
    // Independent timed polling for each enabled ADC. Each read is its
    // own CS-low → CS-high SPI transaction so interleaving on the bus
    // requires no arbitration.

#if ENABLE_ADC1
    static uint32_t t1_last = 0;
    if (millis() - t1_last >= ADC1_POLL_MS) {
        t1_last = millis();
        ADC_Reading r = adc.readADC1Direct();
        if (r.valid) {
  #if ENABLE_ADC2
            RPC.print(millis()); RPC.print('\t');
            RPC.print(1);        RPC.print('\t');       // src = 1 (ADC1/load)
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
            RPC.print(2);        RPC.print('\t');       // src = 2 (ADC2/laser)
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
