/**
 * @file main.cpp  (Portenta H7 dual-core — SensorHub: dual ADC)
 *
 * Runs BOTH ADS1263 ADCs simultaneously on the same chip:
 *   - ADC1 (32-bit, 400 SPS) → AIN0(+) / AIN1(-)   [load cell]
 *   - ADC2 (24-bit, 100 SPS) → AIN2(+) / AIN3(-)   [laser head]
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
#define ENABLE_ADC2   0    // disabled: AIN2/AIN3 path is being skipped while
                           // ADC2 stays in a saturated state. Laser is routed
                           // through ADC1/AIN0-AIN1 instead.

// Checkpoint macro — same convention as the sibling projects.
#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

// Sample periods for each ADC path (timed polling — no DRDY gating).
// ADC1 @ 400 SPS → 2.5 ms period; poll every 3 ms.
// ADC2 @ 100 SPS → 10  ms period; poll every 12 ms.
static const uint32_t ADC1_POLL_MS = 3;
static const uint32_t ADC2_POLL_MS = 12;

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
    CP(2, "pinMode CS (PE_6) done");

    pinMode(ADS1263_RESET_PIN, OUTPUT);
    CP(3, "pinMode RESET (PI_5) done");

    pinMode(ADS1263_DRDY_PIN, INPUT_PULLUP);
    CP(4, "pinMode DRDY (PJ_11) done");

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
    // Routing the LASER HEAD through ADC1 on AIN0(+)/AIN1(-) while the
    // AIN2/AIN3 ADC2 path is in a stuck/saturated state. The HAT's
    // load-cell front-end (inamp/divider) on AIN0/AIN1 applies a scale
    // factor that the calibration fit will absorb into the measured
    // sensitivity k.
    //   INPMUX = 0x01 → AIN0(+) / AIN1(-)
    //   REFMUX = AVDD/AVSS (5 V)
    //   rate   = 400 SPS
    //   PGA bypass = true (direct to ADC core, front-end is external)
#if ENABLE_ADC1
    adc.configureADC1(
        /*inpmux =*/ 0x01,
        /*refmux =*/ ADS1263_REFMUX_AVDD_AVSS,
        /*vref_V =*/ 5.0f,
        /*rate   =*/ ADS1263_400SPS,
        /*pga_bypass =*/ true
    );
    adc.startADC1();
    CP(9, "ADC1 started on AIN0/AIN1 (laser via load-cell front-end)");
#endif

    // ── Configure ADC2 ─────────────────────────────────────────────────
    // DIAGNOSTIC ROUTING: reading AIN6(+)/AIN7(-) instead of the default
    // AIN2(+)/AIN3(-). If ADC2 now reads correctly (≈0 V floating or the
    // laser signal if rewired), the AIN2/AIN3 pins/traces are damaged.
    // If ADC2 still saturates on AIN6/AIN7, the fault is internal to
    // ADC2, not the input pins.
    //   ADC2MUX = 0x67 → AIN6(+) / AIN7(-)
    //   REF2    = AVDD/AVSS (5 V external)
    //   rate    = 100 SPS
    //   gain    = 1x
#if ENABLE_ADC2
    adc.configureADC2(
        /*adc2mux =*/ 0x67,
        /*ref2    =*/ ADS1263_ADC2_REF_AVDD_AVSS,
        /*vref_V  =*/ 5.0f,
        /*rate    =*/ ADS1263_ADC2_100SPS,
        /*gain    =*/ ADS1263_ADC2_GAIN_1
    );
    adc.startADC2();
    CP(10, "ADC2 started on AIN6/AIN7");
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
