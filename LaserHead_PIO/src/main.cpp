/**
 * @file main.cpp  (Portenta H7 dual-core — laser displacement via ADC2)
 *
 * Goal of this build:
 *   - Prove that M4 can drive the ADS1263's ADC2 path and stream laser
 *     head samples to M7 via RPC.
 *   - ADC1 is wired up in the driver but not started here. When we
 *     merge this project with LoadCell_PIO, uncomment the ADC1 block
 *     below and the two ADCs will run concurrently on the same chip.
 *
 * Signal chain (this build):
 *   Laser displacement head (0–5 V single-ended) → AIN2 / AIN3
 *   AIN2 = sensor signal, AIN3 = sensor GND.
 *
 * Future (when merged with LoadCell_PIO):
 *   Load cell → LCA amp → AIN0 / AIN1  → ADC1 (32-bit, 400 SPS)
 *   Laser head →           AIN2 / AIN3 → ADC2 (24-bit, 100 SPS)
 *
 * Flash order (first time):
 *   pio run -e portenta_m7_bridge -t upload
 *   pio run -e portenta_m4        -t upload
 *   pio device monitor
 *
 * → Power-cycle the Hat Carrier after every upload (see README).
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
// Flip ENABLE_ADC1 to 1 when you're ready to run both ADCs simultaneously
// (i.e. when merging the load cell front end into this firmware).
#define ENABLE_ADC1   0
#define ENABLE_ADC2   1

// Checkpoint macro — same convention as LoadCell_PIO.
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

    // ADS1263 power-up settle (see LoadCell_PIO README for why this is
    // required on every cold boot).
    RPC.println("[M4] waiting 3000 ms for ADS1263 to power up...");
    delay(3000);
    RPC.println("[M4] ADS1263 power-up settle done");

    // Drive the ADS1263 pins BEFORE adc.begin() so we can localise any
    // pinMode/port-clock hang.
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

    // ── Configure ADC1 (load cell) ─────────────────────────────────────
    // Disabled in this build; ADC1 stays parked on AINCOM/AINCOM.
    // To enable: set ENABLE_ADC1 = 1 above. Example wiring:
    //   INPMUX = 0x01    → AIN0(+) / AIN1(-)
    //   REFMUX = 0x24    → AVDD / AVSS (5 V)
    //   rate   = 400 SPS
#if ENABLE_ADC1
    adc.configureADC1(
        /*inpmux =*/ 0x01,
        /*refmux =*/ ADS1263_REFMUX_AVDD_AVSS,
        /*vref_V =*/ 5.0f,
        /*rate   =*/ ADS1263_400SPS,
        /*pga_bypass =*/ true
    );
    adc.startADC1();
    CP(9, "ADC1 started");
#endif

    // ── Configure ADC2 (laser head) ────────────────────────────────────
#if ENABLE_ADC2
    adc.configureADC2(
        /*adc2mux =*/ 0x23,                             // AIN2(+) / AIN3(-)
        /*ref2    =*/ ADS1263_ADC2_REF_AVDD_AVSS,       // 5 V external
        /*vref_V  =*/ 5.0f,
        /*rate    =*/ ADS1263_ADC2_100SPS,
        /*gain    =*/ ADS1263_ADC2_GAIN_1
    );
    adc.startADC2();
    CP(9, "ADC2 started");
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
