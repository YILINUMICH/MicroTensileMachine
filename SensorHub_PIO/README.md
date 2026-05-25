> **Status: WIP** — needs port from Hat Carrier → Mid Carrier (ASX00055). See [STATUS.md](STATUS.md) for module-level state and TODOs. See [../README.md](../README.md) for project overview.

# SensorHub_PIO — dual-ADC firmware (load cell + laser head)

PlatformIO project that runs **both** ADS1263 ADCs simultaneously on the
Portenta H7's M4 co-processor.

## Recommended configuration (Phase 4 production — bench-derived 2026-05-24)

This supersedes the legacy Hat-Carrier channel assignment (AIN0/AIN1 + AIN2/AIN3). On the bare TI EVM, AIN0/AIN1 are now consumed by the external REF7050 reference (Cable 2 in [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md)), and cp7 in `ADS1263_FirstPowerUp_PIO/` retired the legacy AIN2/AIN3 saturation question — all eight AIN-pair configs PASS on the EVM.

| Path | AIN pair | Sensor               | SPS     | Gain | Filter | Resolution |
|------|----------|----------------------|---------|------|--------|------------|
| **ADC1** | **AIN2 (+) / AIN3 (−)** | **Load cell (LCA-9PC)** | **400 SPS** | **PGA = 1** | **Sinc3** (MODE1 default) | 32-bit |
| **ADC2** | **AIN4 (+) / AIN5 (−)** | **Keyence IL-030**       | **400 SPS** | **gain = 1** | Sinc3 (ADC2's only option) | 24-bit |

**Why this assignment.** Load cell goes to ADC1 because (a) ADC1 has the 32-bit core and the programmable PGA, preserving flexibility if you ever drop the amp gain or move to a lower-output load cell, and (b) force is the primary measurement — its precision drives the quality of the extracted material properties. The laser controller already conditions its output and outputs at the full ADC input range, so ADC2's 8.5 µV RMS floor (cp8 result, *below* the 10.3 µV datasheet typical) is comfortably under the IL-030's own ~0.3 µm repeatability.

**Why 400 SPS on both.** Mechanical content in tensile work is well under 100 Hz, and 400 SPS sits at the noise-floor sweet spot we characterized in Phase 1.2: 1.29 µV input-referred RMS on ADC1, 19.96 noise-free bits, offset rock-stable across SPS. Matching SPS on both ADCs makes timestamp alignment trivial — the two ADCs free-run on independent clocks but at the same rate, so phase drift between conversions is bounded by their individual jitter (a few hundred µs, → ~0.05° phase error at 1 Hz mechanical).

**Register settings (ADS1263).**

```
REFMUX         = 0x09   external REF7050 on AIN0 (+REF), AIN1 (-REF)
POWER          = 0x13   INTREF on, VBIAS on (AINCOM @ +2.5 V)
MODE1          = 0x80   Sinc3, chop off, delay 8.7 µs

[ADC1 — load cell]
INPMUX         = 0x23   AIN2 (+), AIN3 (−)
MODE2          = 0x08   PGA enabled, gain = 1, 400 SPS

[ADC2 — laser]
ADC2MUX        = 0x45   AIN4 (+), AIN5 (−)
ADC2CFG        = 0x81   DR2 = 400 SPS, GAIN2 = 1, REF2 = external (shares AIN0/AIN1 ref)
```

**LCA-9PC amplifier jumpers.**

| Jumper | Position | Purpose |
|--------|----------|---------|
| **E3**, **E8** | **b** | **Two-pole LPF at 0.3 kHz** — 600 Hz alias-free input, two-pole roll-off puts 400 Hz content 24 dB down. At this BW the amp noise (~215 nV RMS) is below ADC1's 1.3 µV RMS floor — the chip dominates the noise budget, which is what you want. |
| E5, E6 | installed | 4-wire excitation (factory default) |
| E7     | installed | Standard 87.325 kΩ shunt-cal resistor (factory default) |

**Keyence IL-030 settings.**

| Setting | Value | Reason |
|---------|-------|--------|
| Sampling cycle | 1 ms (default for IL-030) | Internal 1 kHz update; 11.3 ms analog response time → ~90 Hz effective BW, fits cleanly inside the 400 SPS Nyquist |
| Averaging rate | 1 | More averaging slows response without adding precision the ADC can resolve |
| Analog output range | ±5 V mapped to ±5 mm around 30 mm reference | Stays within ADS1263 input range at VREF = 5 V; verify against IL-030 front-panel setting (cable map TODO note) |
| High-pass filter | OFF | DC accuracy matters for absolute displacement |
| Mutual-interference prevention | leave default | not needed — only one head |

**When to deviate.**

- *Transient capture* (sudden slip, fracture, impact): step both ADCs to **1200 SPS** and switch LCA-9PC jumpers E3/E8 to position **c** (1 kHz LPF). Noise penalty on ADC1 is ~1.7× RMS (1.29 → 2.20 µV) → 19.3 noise-free bits, still plenty for force; Nyquist becomes 600 Hz on both channels.
- *Maximum DC accuracy, slow experiments* (creep, slow ramps): drop ADC1 to **20 SPS with the FIR filter** (MODE1[6:4] = 0b100) — gives simultaneous 50/60 Hz mains notches. Pair with LCA-9PC jumpers at position **a** (100 Hz LPF).

---

## ADC paths at a glance

| Path | Channels     | Role               | Rate    | Resolution |
|------|--------------|--------------------|---------|------------|
| ADC1 | AIN2 / AIN3  | Load cell (LCA Vo) | 400 SPS | 32-bit     |
| ADC2 | AIN4 / AIN5  | Laser head (IL-030)| 400 SPS | 24-bit     |

This is the merge of the sibling single-ADC projects:
- `../LoadCell_PIO/`   — ADC1 only
- `../LaserHead_PIO/`  — ADC2 only

The shared driver in `lib/ADS1263/` is the dual-ADC-capable version from
LaserHead_PIO; both ADC paths use independent SPI transactions (CS
toggled per read) and independent polling timers.

## Why this project exists

Two purposes:

1. **Production firmware** — the full tensile rig needs both load and
   displacement captured from the same HAT with a single serial stream
   and a shared time base. This is that firmware.

2. **Diagnostic isolation** — if ADC2 is misbehaving on AIN2/AIN3 but
   ADC1 reads correctly on AIN0/AIN1 in this same firmware, the fault
   is **local to the ADC2 input path** (AIN2/AIN3 pins, input protection
   components on the HAT, sensor wiring, etc.), *not* the chip itself,
   the HAT's power rails, the SPI bus, or the driver. Swap load-cell
   wiring onto AIN2/AIN3 and vice versa to further bisect.

## Wiring

```
REF7050 +5 V         ──► AIN0 (+REF)        ┐ Cable 2 — reference
REF7050 GND          ──► AIN1 (-REF)        ┘ (REFMUX = 0x09)

LCA-9PC amp output   ──► AIN2 (+)           ┐ Cable 3 — load cell
LCA-9PC amp GND/sense──► AIN3 (-)           ┘ (differential pair)

IL-030 analog out    ──► AIN4 (+)           ┐ Cable 4 — laser head
IL-030 sensor GND    ──► AIN5 (-)           ┘ (differential pair)
```

AIN3 and AIN5 are dedicated differential returns, not generic carrier
ground. Tie each to the specific sensor's return path (LCA-9PC ground
sense for the load cell, IL-030 controller ground for the laser) for
best common-mode rejection. AIN0/AIN1 are now reserved for the REF7050
reference and must not be reused for sensors.

## Serial output format

With both ADCs enabled, every line carries a `src` column so the host
can demultiplex:

```
<t_ms>\t<src>\t<raw_code>\t<voltage_V>
   12    1      26214400     1.525000    ← load cell (src=1)
   15    2       4220760     2.515000    ← laser    (src=2)
   18    1      26214337     1.525003
   ...
```

The host-side parser in `../Calibrate_LaserHead/portenta_reader.py`
already handles this 4-column form — pass `adc_source=1` or `2` to
select which stream to keep.

## Flash order

```sh
pio run -e portenta_m7_bridge -t upload    # once — installs the M7 bridge
pio run -e portenta_m4        -t upload    # flashes the M4 sampler
pio device monitor                          # 115200 baud
```

Thereafter only re-flash `portenta_m4` while iterating.

> **Power-cycle the Hat Carrier after every flash.** The dfu reset
> does not cleanly re-power the HAT's 3.3 V LDO rail; without a full
> power cycle you may see `ID=0x00` / `adc.begin returned FALSE`.
> Unplug USB and J9 (if connected), wait ~5 seconds, reapply, reopen
> the monitor.

## Expected boot output

```
[M7] bridge up — forwarding RPC to USB Serial (SensorHub)
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
[M4 cp 9]  ADC1 started
[M4 cp 10] ADC2 started
--- ADS1263 Config (dual-ADC) ---
ID            : 0x23
[ADC1]
  INPMUX      : 0x23      ← AIN2 (+), AIN3 (-)
  REFMUX      : 0x09      ← external REF7050 on AIN0/AIN1
  VREF        : 5.000 V
  Rate        : 400 SPS
  PGA         : gain=1 (enabled)
  Running     : YES
[ADC2]
  ADC2MUX     : 0x45      ← AIN4 (+), AIN5 (-)
  REF2        : 0x1       ← external (shares ADC1 ref on AIN0/AIN1)
  VREF        : 5.000 V
  Rate        : 400 SPS
  Gain        : 1x
  Running     : YES
  ADC2CFG rb  : 0x81
Frame INTERFACE=0x05 → RDATA1=6B, RDATA2=5B
---------------------------------
[M4] streaming. format: t_ms\tsrc\traw_code\tvoltage_V   (src=1 load, src=2 laser)
12   1   <raw1>   <v1>
15   2   <raw2>   <v2>
...
```

## Disabling one path for diagnostics

Flip the flags at the top of `src/main.cpp`:

```cpp
#define ENABLE_ADC1   1
#define ENABLE_ADC2   1
```

Set either to `0` and re-flash to stream only one channel. The output
format line automatically drops the `src` column when only one path is
active, matching the single-ADC sibling projects.

## File layout

```
SensorHub_PIO/
├── README.md              (this file)
├── platformio.ini         two envs: portenta_m4, portenta_m7_bridge
├── .gitignore
├── src/
│   └── main.cpp           both cores, #ifdef-guarded; ENABLE_ADC1/ENABLE_ADC2 flags
└── lib/
    └── ADS1263/
        ├── ADS1263_Driver.h    dual-ADC API (configureADCx/startADCx/readADCx*)
        └── ADS1263_Driver.cpp  shared chip init + two independent data paths
```

## Relationship to sibling projects

When this project is confirmed working end-to-end, it supersedes
`LoadCell_PIO/src/main.cpp` and `LaserHead_PIO/src/main.cpp`. Those
remain in the tree as single-path reference builds for bring-up; the
production tensile rig will run this firmware.
