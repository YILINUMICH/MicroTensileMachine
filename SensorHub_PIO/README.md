# SensorHub_PIO — dual-ADC firmware (load cell + laser head)

PlatformIO project that runs **both** ADS1263 ADCs simultaneously on the
Portenta H7's M4 co-processor:

| Path | Channels     | Role               | Rate    | Resolution |
|------|--------------|--------------------|---------|------------|
| ADC1 | AIN0 / AIN1  | Load cell (LCA Vo) | 400 SPS | 32-bit     |
| ADC2 | AIN2 / AIN3  | Laser head (IL-030)| 100 SPS | 24-bit     |

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
Load cell + LCA amp  ──► AIN0 (+)
LCA amp GND / common ──► AIN1 (-)
IL-030 analog out    ──► AIN2 (+)
IL-030 sensor GND    ──► AIN3 (-)
```

AIN1 and AIN3 are dedicated differential returns, not generic HAT ground.
Tie each to the specific sensor's return path for best common-mode
rejection.

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
  INPMUX      : 0x01
  REFMUX      : 0x24
  VREF        : 5.000 V
  Rate        : 400 SPS
  PGA         : bypass (gain=1)
  Running     : YES
[ADC2]
  ADC2MUX     : 0x23
  REF2        : 0x4
  VREF        : 5.000 V
  Rate        : 100 SPS
  Gain        : 1x
  Running     : YES
  ADC2CFG rb  : 0x44
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
