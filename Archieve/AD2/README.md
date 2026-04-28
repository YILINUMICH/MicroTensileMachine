# AD2 — Digilent Analog Discovery 2 Interface

Two-channel voltage acquisition for the micro tensile machine, replacing
the Arduino + ADS1263 during the hardware transition. Reads a load cell
(LCA-9PC/RTC amplifier) on CH1 and a Keyence IL-030 laser displacement
sensor on CH2.

**Author:** Yilin Ma
**Date:** April 2026
**University of Michigan Robotics — HDR Lab**

## Features

- **AD2Scope class**: context-manager friendly wrapper around the
  Digilent WaveForms SDK
- **Automatic device discovery**: clear error if no AD2 is attached
- **Per-channel input range**: defaults to ±5 V on both CH1 and CH2,
  overridable at construction time
- **Single-shot reads**: `read_single()` returns `(v_ch1, v_ch2)` in
  volts, suitable for loops up to ~100 Hz
- **100 Hz continuous logger**: `ad2_continuous_log.py` streams both
  channels to a CSV with wall-clock and elapsed timestamps
- **Graceful shutdown**: Ctrl-C flushes the CSV and closes the AD2
  handle
- **Matches sibling module style**: same class pattern as
  `ZaberStage/` and `KeysightLCR/` so the Phase 2 integration script
  can import all three the same way

## Hardware

### Wiring

| AD2 Pin | Connection | Source |
| ------- | ---------- | ------ |
| 1+ (CH1 positive) | LCA-9PC/RTC amplifier V_out (signal) | Load cell |
| 1− (CH1 negative) | LCA-9PC/RTC amplifier GND (signal ground) | Load cell |
| 2+ (CH2 positive) | IL-030 analog output (white) | Keyence |
| 2− (CH2 negative) | IL-030 analog ground (shield) | Keyence |
| ⏚ (scope GND) | Lab bench ground / shared DC common | — |

Both channels run in **differential mode** (AD2 default). Do not tie
the amplifier ground to the AD2 USB ground unless you have checked
there is no ground-loop hum — differential pairs are preferred.

Channel ranges default to ±5 V, matching:
- **LCA-9PC/RTC**: 0–5 V output (compression only, calibrated direction)
- **Keyence IL-030**: ±5 V analog output range (see IL-030 manual for
  exact V→mm scaling — the default is typically 1 V/mm with the 30 mm
  detection center being 0 V, but confirm against the current
  controller unit settings before you trust absolute numbers)

### LCA-9PC/RTC notes

- Allow **30 minutes of warmup** before calibration measurements —
  the bridge amp drifts noticeably during the first 10–20 min.
- Perform the zero/span procedure per `LCA9PCLCARTC.pdf` (in the
  project docs). Do not re-zero mid-experiment.
- Wiring is set up for **compression only**; tension will produce a
  bounded but unspecified signal and is not calibrated.

### Keyence IL-030 notes

- Confirm the amp is set to **voltage output mode**, not current
  output. The IL-030 controller has a switch / menu setting for this.
- Note the actual V→mm scaling stamped on the controller or derived
  during the Phase 2 `calibrate_laser` step. 1 V/mm is the factory
  default but mounting offsets and span settings change the absolute
  value.
- Measurement center is 30 mm from the laser face; useful range is
  ±5 mm (i.e. 25–35 mm) in the IL-030 variant.

## Installation

### Requirements

- Python 3.8+
- Digilent **WaveForms** runtime (ships with `dwf.dll` on Windows,
  `libdwf.so` on Linux, `dwf.framework` on macOS)
- A Digilent Analog Discovery 2 attached over USB

### Install WaveForms

Download and install the WaveForms runtime from Digilent:

<https://digilent.com/shop/software/digilent-waveforms/>

This puts the shared library (`dwf.dll` / `libdwf.so` /
`dwf.framework`) on the system search path, which is what this module
loads via `ctypes`. No Python wheel is required — we call the SDK
directly.

### Verify the runtime

After installation, run:

```bash
python AD2/ad2_interface.py
```

You should see a startup banner, the AD2 serial number, the WaveForms
runtime version, and 10 readings printed in mV. If you instead get
`Error loading WaveForms SDK library`, the runtime is not installed
or not on PATH.

## Quick Start

### System health check

```bash
cd AD2
python ad2_interface.py
```

This opens the AD2, takes 10 readings at 1 Hz, and prints CH1/CH2 in
mV. Use this whenever you suspect a cabling issue — if the numbers
are sensible, the AD2 is fine and any downstream problem is in
`ad2_continuous_log.py` or in consumer code.

### 100 Hz continuous logging

```bash
# Open-ended run, Ctrl-C to stop
python ad2_continuous_log.py

# Fixed duration
python ad2_continuous_log.py --duration 60

# Custom rate and output
python ad2_continuous_log.py --rate 100 --output mytest.csv
```

CLI flags:

| Flag | Default | Meaning |
| ---- | ------- | ------- |
| `--rate` | `100` | Target sample rate in Hz |
| `--duration` | *unset* | Run length in seconds; omit for Ctrl-C termination |
| `--output` | `data/ad2_log_<YYYYMMDD_HHMMSS>.csv` | Output CSV path |
| `--ch1-range` | `5.0` | CH1 full-scale in volts |
| `--ch2-range` | `5.0` | CH2 full-scale in volts |
| `--stats-interval` | `2.0` | Live stats print period in seconds; `0` disables |

### Library use

```python
from ad2_interface import AD2Scope

with AD2Scope(ch1_range_v=5.0, ch2_range_v=5.0) as scope:
    for _ in range(10):
        v_load, v_laser = scope.read_single()
        print(f"load={v_load*1000:.2f} mV  laser={v_laser*1000:.2f} mV")
```

## Output Format

CSV columns produced by `ad2_continuous_log.py`:

| Column | Units | Notes |
| ------ | ----- | ----- |
| `count` | — | 1-based sample index |
| `timestamp_iso` | ISO 8601 | Wall-clock with microseconds (for cross-instrument alignment in Phase 2) |
| `t_elapsed_s` | seconds | High-resolution elapsed time from start of acquisition (use for jitter analysis) |
| `v_ch1_mV` | millivolts | Load cell voltage |
| `v_ch2_mV` | millivolts | Laser voltage |

Values are stored in millivolts for readability (divide by 1000 to get
volts). The file is flushed to disk every `--stats-interval` seconds,
so a crash loses at most that many seconds of data.

## Performance

| Metric | Value |
| ------ | ----- |
| Target sample rate | 100 Hz |
| Sustained rate (observed) | ~100 Hz on USB 2.0 |
| Expected jitter (per-sample) | <1 ms on idle system |
| Sample interval pacing | `time.perf_counter()` deadline + short spin-wait |

At 100 Hz a simple timed polling loop is sufficient — we do not use
AD2's buffered record mode, which was overkill for this rate and
complicates synchronization with the LCR meter and Zaber stage.

## Troubleshooting

1. **"No AD2 / WaveForms device detected"** — check USB cable, try a
   different port, confirm WaveForms GUI can see the device. If the
   WaveForms GUI is open, close it; two processes cannot share the
   device.
2. **"Error loading WaveForms SDK library"** — the WaveForms runtime
   is not installed or `dwf.dll` / `libdwf.so` is not on PATH. Install
   from the Digilent link above and restart the shell.
3. **Readings saturated at the range limits** — reduce the channel
   range if your real signal is smaller (e.g. a 0–2 V load cell
   signal gets better resolution at `--ch1-range 2`), or check that
   the amplifier is powered and not clipping.
4. **Very noisy signal** — confirm both channels are wired
   differentially (1+/1−, 2+/2−) and not single-ended. Add a DC block
   or twist the signal pair with a ground wire. Check that the AD2
   scope GND is tied to the amp GND somewhere (differential does not
   mean floating).
5. **Lower-than-target effective rate in the live stats** — another
   process is holding the CPU; try closing the WaveForms GUI,
   browsers, etc. Sub-100 Hz indicates USB latency or scheduling
   issues, not the AD2 itself.
6. **USB disconnect mid-capture** — the logger prints a read error
   and continues trying. If the device reappears on the same handle
   it will resume; otherwise stop with Ctrl-C, reconnect, and restart.

## Module Structure

```
AD2/
├── ad2_interface.py       # AD2Scope library + standalone health check
├── ad2_continuous_log.py  # 100 Hz CSV logger
└── README.md              # this file
```

`ad2_interface.py` exposes:

| Symbol | Purpose |
| ------ | ------- |
| `AD2Scope` | Main class; context-manager friendly |
| `AD2Scope.open()` / `close()` | Lifecycle |
| `AD2Scope.read_single()` | Returns `(v_ch1, v_ch2)` in volts |
| `AD2Scope.read_burst(n, interval_s)` | Convenience for health-check-style loops |
| `AD2Scope.get_device_info()` | Serial / name / WaveForms version |
| `AD2DeviceInfo` | Dataclass returned by `get_device_info()` |
| `create_scope(**kwargs)` | Factory that returns an already-open scope |

## Related Modules

- `ZaberStage/` — linear stage control, same module pattern
- `KeysightLCR/` — E4980A/AL LCR meter control, same module pattern
- `ADS1263/` — predecessor (Arduino + ADS1263) being replaced by this module
- `Calibrate_LaserHead/` — Phase 2 will consume this module to build
  `laser_calibration.yaml`

## Version History

- **v0.1** (April 2026) — Initial release. Phase 1 of the AD2
  transition: standalone interface + 100 Hz logger. Phase 2
  (integration with `ZaberStage` and `KeysightLCR` under a YAML-driven
  test runner) is separate.
