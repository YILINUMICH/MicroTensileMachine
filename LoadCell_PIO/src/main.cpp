/**
 * @file main.cpp  (Portenta H7 dual-core — STEP 1: minimal data path)
 *
 * Goal of this step:
 *   - Prove that M4 can drive the ADS1263 over SPI and send samples to M7.
 *   - Prove that M7 forwards the stream cleanly to USB Serial.
 *   - No commands, no calibration, no tare, no ring buffer yet.
 *     Just: ADC → RPC → USB.
 *
 * Architecture:
 *   ┌──────────────────────┐       RPC       ┌────────────────────┐  USB   PC
 *   │ M4  (this file,      │◀───────────────▶│ M7 (this file,     │◀────▶ Serial
 *   │      CORE_CM4 branch)│                 │     CORE_CM7 branch│       monitor
 *   │  - ADS1263 @ 20 SPS  │                 │  - RPC ⇌ Serial    │       115200
 *   │  - startContinuous() │                 │  - boots M4 via    │
 *   │  - polls DRDY,       │                 │      RPC.begin()   │
 *   │    readDirect(),     │                 │                    │
 *   │    RPC.println(line) │                 │                    │
 *   └──────────────────────┘                 └────────────────────┘
 *
 * Flash order (first time):
 *   pio run -e portenta_m7_bridge -t upload    # flashes the M7 bridge
 *   pio run -e portenta_m4        -t upload    # flashes the M4 sampler
 *   pio device monitor                          # watch the stream
 *
 * Output format (tab-separated, one sample per line):
 *   t_ms    raw_code    voltage_V
 *
 * The default rate is 20 SPS, so expect one line every ~50 ms. Change the
 * rate passed to adc.begin() in the M4 setup() to speed up (but note that
 * RPC throughput is finite — see earlier notes).
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

    // RPC.begin() opens the shared-SRAM mailbox AND boots the M4 firmware.
    RPC.begin();

    Serial.println("[M7] bridge up — forwarding RPC to USB Serial");
}

void loop() {
    // One-way is enough for step 1 (no user commands yet). When we add
    // command handling later, mirror this in the other direction too.
    while (RPC.available()) {
        Serial.write(RPC.read());
    }
}

// ══════════════════════════════════════════════════════════════════════
//  M4 CORE — drive the ADS1263 and push each sample to M7 via RPC
// ══════════════════════════════════════════════════════════════════════
#elif defined(CORE_CM4)

#include <SPI.h>
#include "ADS1263_Driver.h"

ADS1263_Driver adc;

// Handy checkpoint macro — prints a labeled line via RPC and flushes.
// If the log stops at checkpoint N, the hang is between N and N+1.
#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

void setup() {
    // RPC first so we can report progress to the M7 bridge.
    RPC.begin();
    delay(500);                       // let M7 finish USB enumeration
    CP(0, "RPC up");

    // Belt-and-suspenders: initialise the hardware UART even though we
    // don't use it. This prevents a rogue Serial.print anywhere in the
    // toolchain from hanging on an un-clocked peripheral.
    Serial.begin(115200);
    CP(1, "Serial.begin done");

    // Drive the ADS1263 pins manually BEFORE calling adc.begin(), so we
    // can localise the hang to a specific pinMode/port clock if there is
    // one. pinMode is idempotent — adc.begin() will redo it harmlessly.
    pinMode(ADS1263_CS_PIN, OUTPUT);
    CP(2, "pinMode CS (PE_6) done");

    pinMode(ADS1263_RESET_PIN, OUTPUT);
    CP(3, "pinMode RESET (PI_5) done");

    pinMode(ADS1263_DRDY_PIN, INPUT_PULLUP);
    CP(4, "pinMode DRDY (PJ_11) done");

    digitalWrite(ADS1263_CS_PIN, HIGH);
    digitalWrite(ADS1263_RESET_PIN, HIGH);
    CP(5, "CS and RESET driven HIGH");

    // SPI.begin() on M4 is the most common suspect. If the log stops
    // right here, SPI peripheral ownership / clock isn't set up for M4.
    SPI.begin();
    CP(6, "SPI.begin() returned");

    // Try a raw ID read before trusting adc.begin() — this confirms the
    // SPI bus is actually clocking and the chip is answering.
    // We talk to the driver via its public API from here on.
    CP(7, "calling adc.begin()");
    bool ok = adc.begin(ADS1263_20SPS);
    CP(8, ok ? "adc.begin returned TRUE" : "adc.begin returned FALSE");

    if (!ok) {
        RPC.println("[M4] FATAL: ADS1263 init failed");
        while (1) { delay(1000); }
    }

    RPC.print("[M4] ADC ready, ID=0x");
    RPC.println(adc.getDeviceID(), HEX);
    RPC.print("[M4] VREF=");
    RPC.print(adc.getVrefV(), 3);
    RPC.println(" V");

    // Kick the ADC into continuous conversion — DRDY will now pulse LOW
    // each time a new sample is ready.
    adc.startContinuous();
    delay(100);                       // one filter-settle interval
    CP(9, "startContinuous done");

    RPC.println("[M4] streaming. format: t_ms\\traw_code\\tvoltage_V");
}

void loop() {
    // Poll DRDY. When a new sample is ready, grab it and forward.
    // readDirect() does no start/stop — just reads the frame. Caller
    // must have confirmed DRDY is LOW, which dataReady() does.
    if (adc.dataReady()) {
        ADC_Reading r = adc.readDirect();
        if (r.valid) {
            RPC.print(millis());
            RPC.print('\t');
            RPC.print(r.raw_code);
            RPC.print('\t');
            RPC.println(r.voltage_V, 6);
        }
    }
    // no other work in this step — loop spins and polls
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
