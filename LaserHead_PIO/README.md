# LaserHead_PIO — Portenta H7 dual-core build (dual-ADC-capable)

PlatformIO project built around a **dual-ADC driver for the ADS1263**.
The chip has two independent ADCs on one die, and this project's
driver exposes both as parallel sub-APIs so one firmware can read a
load cell (ADC1, 32-bit, on AIN0/AIN1) and a laser displacement head
(ADC2, 24-bit, on AIN2/AIN3) simultaneously from the same HAT.

In **this build** only ADC2 is enabled at compile time — the laser
head is the subject — but ADC1 is fully wired up in the driver and
only needs `#define ENABLE_ADC1 1` in `src/main.cpp` to come online.

The M4 core does the sampling; a tiny M7 bridge forwards samples to
USB Serial via RPC. Same build/flash flow as the sibling
**LoadCell_PIO** project.

```
┌──────────────────────────┐       RPC       ┌────────────────────┐  USB   PC
│ M4  (CORE_CM4)           │◀───────────────▶│ M7 (CORE_CM7)      │◀────▶ Serial
│  - ADS1263 dual-ADC      │                 │  - forwards RPC to │       monitor
│    • ADC2 @ 100 SPS      │                 │    USB Serial      │       115200
│      AIN2(+) / AIN3(-)   │                 │  - boots M4 via    │
│    • ADC1 (available,    │                 │      RPC.begin()   │
│      disabled in build)  │                 │                    │
│  - timed-polls both      │                 │                    │
│  - RPC → M7              │                 │                    │
└──────────────────────────┘                 └────────────────────┘
```

## Wiring (this build)

The laser head's signal and ground both land on the HAT:

| Sensor pin       | HAT input |
|------------------|-----------|
| 0–5 V signal out | AIN2      |
| GND (sensor)     | AIN3      |

AIN3 goes to the sensor's own ground return, not a generic HAT ground.
The read is pseudo-differential (AIN2 − AIN3), so common-mode noise on
the sensor's return path is rejected by the ADC front end rather than
picked up as offset.

When ADC1 is enabled (load cell path, future merge):

| Sensor                       | HAT input |
|------------------------------|-----------|
| Load cell + LCA amp Vo       | AIN0      |
| LCA GND (sensor common)      | AIN1      |

## Dual-ADC model

ADC1 and ADC2 share only the chip-wide registers (POWER, INTERFACE,
IDAC). Their configuration and data streams are independent:

| Resource                          | ADC1                           | ADC2                         |
|-----------------------------------|--------------------------------|------------------------------|
| Resolution                        | 32-bit                         | 24-bit                       |
| Filter                            | Sinc1 / Sinc2 / Sinc3 / Sinc4 / FIR | Sinc3 only              |
| Max rate                          | 38 400 SPS                     | 800 SPS                      |
| Mux register                      | `INPMUX`  (0x06)               | `ADC2MUX` (0x16)             |
| Rate / gain / ref register        | `MODE2` (0x05) + `REFMUX` (0x0F) | `ADC2CFG` (0x15)            |
| Start / stop / read commands      | `START1 / STOP1 / RDATA1`      | `START2 / STOP2 / RDATA2`    |
| Read frame (INTERFACE = 0x05)     | 6 bytes (STATUS+4 data+CHK)    | 5 bytes (STATUS+3 data+CHK)  |
| DRDY output                       | pin PJ_11 (unusable — LoRa)    | none (no DRDY for ADC2)      |
| Code-to-voltage divisor           | 2³¹                            | 2²³ × gain                   |

Because the two pipelines are independent on-chip, `startADC1()` and
`startADC2()` can both be active at the same time. In the main loop,
each read is its own CS-low → CS-high SPI transaction, so interleaving
`readADC1Direct()` and `readADC2Direct()` on timers is safe with no
arbitration.

## Driver API (summary)

```cpp
ADS1263_Driver adc;

// chip-level init — verify ID, write POWER/INTERFACE, park both paths
bool ok = adc.begin();

// --- ADC1 path ---
adc.configureADC1(/*inpmux*/ 0x01,                       // AIN0(+)/AIN1(-)
                  /*refmux*/ ADS1263_REFMUX_AVDD_AVSS,   // 5 V external
                  /*vref_V*/ 5.0f,
                  /*rate  */ ADS1263_400SPS,
                  /*pga_bypass*/ true);
adc.startADC1();
ADC_Reading r1 = adc.readADC1Direct();   // 32-bit signed in r1.raw_code

// --- ADC2 path ---
adc.configureADC2(/*adc2mux*/ 0x23,                       // AIN2(+)/AIN3(-)
                  /*ref2   */ ADS1263_ADC2_REF_AVDD_AVSS, // 5 V external
                  /*vref_V */ 5.0f,
                  /*rate   */ ADS1263_ADC2_100SPS,
                  /*gain   */ ADS1263_ADC2_GAIN_1);
adc.startADC2();
ADC_Reading r2 = adc.readADC2Direct();   // sign-extended 24-bit in r2.raw_code
```

Both `readADCxDirect()` calls assume continuous-conversion mode is
active and the caller times the reads (either by `millis()` math or
a hardware timer). No DRDY check — `PJ_11` is held by the onboard LoRa
chip on the H7 and ADC2 has no DRDY output of its own.

## Enabling / disabling each path at build time

`src/main.cpp` has two flags at the top of the M4 branch:

```cpp
#define ENABLE_ADC1   0    // load cell path
#define ENABLE_ADC2   1    // laser head path (this build)
```

Flip `ENABLE_ADC1` to `1` to bring ADC1 online. The output line format
automatically changes when both are on — each sample line gets a `src`
column (`1` for ADC1, `2` for ADC2) so the host side can demultiplex.

## ADC2CFG / MODE2 byte reference

### `ADC2CFG (0x15)`

```
 7 6 | 5 4 3 | 2 1 0
  DR2  GAIN2   REF2
```

Default set by `configureADC2(..., 100SPS, GAIN_1)` with REF2 =
AVDD/AVSS: `0b01_000_100 = 0x44`.

| Field  | Value | Meaning                                |
|--------|-------|----------------------------------------|
| DR2    | `01`  | 100 SPS                                |
| GAIN2  | `000` | gain = 1                               |
| REF2   | `100` | reference = AVDD / AVSS (external 5 V) |

### `MODE2 (0x05)` (only matters if ADC1 is enabled)

```
 7    | 6 5 4 | 3 2 1 0
 BYPASS  GAIN    DR
```

With `pga_bypass = true` (recommended for the load cell front end
since the LCA already amplifies): `BYPASS = 1`, gain field ignored.

## Flash order

First time:

```sh
pio run -e portenta_m7_bridge -t upload    # once — installs the M7 bridge
pio run -e portenta_m4        -t upload    # flashes the M4 sampler
pio device monitor                          # 115200 baud
```

Thereafter only re-flash `portenta_m4` while iterating.

> **Power-cycle the stack after every flash.** Same rule as LoadCell_PIO
> and same reason: the dfu upload resets the H7 MCU but doesn't cleanly
> re-power the HAT's 3.3 V LDO rail. If you see `ID=0x00` / `adc.begin
> returned FALSE` after an upload, unplug the Hat Carrier (USB-C and/or
> J9 screw terminal), wait ~5 s, reapply power, and reopen the monitor
> *before* starting to debug anything else.

## Expected output (this build — ADC2 only)

```
[M7] bridge up — forwarding RPC to USB Serial (laser head)
[M4 cp 0] RPC up
[M4 cp 1] Serial.begin done
[M4] waiting 3000 ms for ADS1263 to power up...
[M4] ADS1263 power-up settle done
[M4 cp 2..6] pinModes / SPI.begin
[M4 cp 7] calling adc.begin()
ADS1263 found. ID=0x23
ADS1263 ready (dual-ADC; both paths parked until configureADCx)
[M4 cp 8] adc.begin returned TRUE
[M4] ADC ready, ID=0x23
ADC2 configured: ADC2MUX=0x23 REF2=0x4 VREF=5.000 V rate=100 SPS gain=1x
[M4 cp 9] ADC2 started
--- ADS1263 Config (dual-ADC) ---
...
[ADC2]
  ADC2MUX     : 0x23
  REF2        : 0x4
  VREF        : 5.000 V
  Rate        : 100 SPS
  Gain        : 1x
  Running     : YES
  ADC2CFG rb  : 0x44
---------------------------------
[M4] streaming. format: t_ms\traw_code\tvoltage_V   (ADC2/laser)
3218   4293412    2.558...
3230   4293198    2.558...
...
```

`ADC2CFG rb: 0x44` confirms the config register took the write. With
AIN2/AIN3 floating you'll see something near 0 V with small offset.

## Troubleshooting

The three failure modes documented in `../LoadCell_PIO/README.md`
apply here verbatim — the fixes are already in this project:

1. **M4 hangs before any checkpoint prints** → driver logs route
   through `RPC` on M4 (same `DRV_LOG` macro as LoadCell_PIO).
2. **`ID=0x0` / `adc.begin returned FALSE` at cp 8** → see the
   expanded triage list below; this has now been seen with two
   independent causes on this exact hardware, not just the original
   post-flash power-cycle one.
3. **Boot clean but no stream** → loop uses timed polling, not DRDY.

### ID=0x0 triage (in order — don't skip to firmware)

When `begin()` fails the ID check, the register-dump diagnostic now
prints all 29 registers. Every one reading `0x00` means MISO is
silent; no amount of firmware tweaking fixes that. In decreasing
frequency:

1. **Post-flash HAT power state.** The dfu reset doesn't cleanly
   re-power the HAT's 3.3 V LDO rail. Full power cycle of the
   Hat Carrier (unplug USB-C *and* J9 screw-terminal, 30 s, reapply).
2. **Loose physical seating on the carrier.** The HAT can be
   electrically-almost-connected but mechanically lifted a fraction of
   a millimeter — enough that MISO pin contact becomes intermittent.
   Symptom is indistinguishable from (1): register dump all `0x00`.
   Fix: pop the HAT off, check for bent pins, reseat squarely, then
   **tighten the standoff nuts** so cable strain from the sensor
   wiring can't lift the HAT back off J5. *This was the actual cause
   during April bring-up of this project* — repeated power cycles and
   re-flashes didn't help until the nut was tightened.
3. **Cold solder joint on the HAT's 40-pin female header.** Flagged
   in `../ADS1263/ADS1263_H7_Integration_Notes.md` §Known Hardware
   Issue. Only suspect this if (1) and (2) don't help and the fault
   is intermittent across power cycles.
4. Only after all three → suspect firmware.

### If ADC2 readings look wrong (not ID=0x0)

Call `adc.printConfig()` (already called once in `setup()`) and
confirm the `ADC2CFG rb: 0x44` line. A readback that doesn't match
the written value means the chip silently reset somewhere — almost
always `SFOCAL2` if anyone added calibration calls. This driver
does NOT run `SFOCAL2` automatically for that reason.

## File layout

```
LaserHead_PIO/
├── README.md                       (this file)
├── platformio.ini                  two envs: portenta_m4, portenta_m7_bridge
├── .gitignore
├── src/
│   └── main.cpp                    both cores, #ifdef-guarded;
│                                   ENABLE_ADC1/ENABLE_ADC2 flags pick paths
└── lib/
    └── ADS1263/
        ├── ADS1263_Driver.h        dual-ADC API (configureADCx/startADCx/readADCx*)
        └── ADS1263_Driver.cpp      shared chip init + two independent data paths
```

## Status

**Bring-up verified on bench (April 2026).** Dual-ADC driver flashes
clean, ADC2 path streams laser-head samples end-to-end:

1. `ID=0x23` after the 3 s settle ✓
2. `ADC2CFG rb: 0x44` (configured DR2=01 / GAIN2=000 / REF2=100,
   readback matches) ✓
3. Tab-separated `t_ms\traw_code\tvoltage_V` lines arrive cleanly
   from M4 → RPC → M7 → USB ✓
4. Code-to-voltage math checks out end-to-end (e.g. raw `611864` →
   `611864 / 2²³ × 5.000 V = 0.3647 V`, matches the printed
   `0.364699` to six digits) ✓

**Known quirk not yet chased:** with `ADS1263_ADC2_100SPS` selected,
each conversion value appears twice in a row in the stream. The read
cadence is a clean 12 ms (matches `SAMPLE_POLL_MS`), but the chip
output register only updates every ~24 ms → effective ~42 SPS, not
the configured 100 SPS. Not blocking for mechanical displacement
measurement; worth a future follow-up (see Next steps item 3).

When ready to merge the load-cell path into this firmware:

5. Set `ENABLE_ADC1 = 1` in `src/main.cpp`, wire LCA Vo / GND into
   AIN0 / AIN1, re-flash. Expect interleaved `src=1` (load) and
   `src=2` (laser) lines in the stream.

## Next steps

1. **Validate readings against the physical setup.** Point the laser
   head at known distances across its measurement range and confirm
   the voltage tracks linearly. Current working stream shows ~0.365 V
   at the bench state used during bring-up — sanity-check that value
   matches what the head should be outputting at that position.
2. **Encode laser calibration curve.** The sensor reports displacement
   as a linear voltage in a documented range (e.g. 1.0 V = near,
   4.0 V = far). Convert raw voltage → mm in the driver or in
   `main.cpp` so the stream can carry engineering units instead of
   raw volts.
3. **Investigate the 100 → ~42 SPS discrepancy.** Try
   `ADS1263_ADC2_400SPS` and `ADS1263_ADC2_800SPS` to see whether the
   "each value printed twice" pattern scales with the configured rate
   (filter overhead) or is fixed at ~24 ms (rate-encoding bug).
   Cross-check the DR2 bit field against the Waveshare reference
   library for sanity.
4. **Enable ADC1 and merge the load-cell front end** into this
   firmware (set `ENABLE_ADC1 = 1`, retire `LoadCell_PIO/src/main.cpp`
   in favour of this one). Verify both streams arrive at their
   expected rates with no cross-talk.
5. Reroute DRDY off `PJ_11` to a free GPIO so ADC1 can go
   interrupt-driven (inherited goal from LoadCell_PIO). ADC2 stays on
   timed polling — it has no DRDY output regardless.
6. Shared-SRAM ring buffer + Ethernet streaming on M7 (inherited
   from LoadCell_PIO's next-steps list).
