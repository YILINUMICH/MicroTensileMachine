# ZaberStage/safety_config.json — operator notes

These notes live alongside `safety_config.json` because JSON itself can't
carry comments. Read this before widening / narrowing the limits.

## Current state (2026-05-27)

```
position_limits_mm: [5, 15]              ← TIGHT (calibration-only)
max_velocity_mm_s:  5.0
reading_rate_hz:    100.0
device_info:        X-LSQ300A-E01        ← LIVE (serial 143153, fw 7.48.24004)
```

## Why `position_limits_mm = [5, 15]`

Set tight for the IL-030 laser-head calibration in
`../Calibrate_LaserHead/`. The mounting fixture on this rig maps Zaber
stage position to the IL-030 measurement window as:

| Stage position | IL-030 window | Voltage |
|---:|:---|:---:|
| 5 mm  | high end of sensor       | **max** reading |
| 10 mm | reference distance       | mid reading     |
| 15 mm | low end of sensor        | **min** reading |

`Calibrate_LaserHead/config.yaml` sweeps ±5 mm around `sweep_center_mm =
10`, i.e. absolute stage positions [5, 15]. The safety limits match
exactly — a tight envelope so an over-range stage command during
calibration aborts the run (`sys.exit(5)` in `run_calibration.py`)
instead of driving the carriage into something.

Pre-calibration limits were `[10, 40]` (the general travel envelope).
That would FORBID the new calibration low end at 5 mm.

## Widening for other workflows

Other workflows on the rig (load-cell tests in `../LoadCell_PIO/`,
material characterisation in `../SMA_CharacterizationV2/`) use a wider
travel envelope and need different limits. To widen back to the general
envelope:

```json
"position_limits_mm": [10, 40]
```

Or, if regenerating from the real connected hardware:

```json
"position_limits_mm": [0, <stage_max_travel_mm>]
```

The stage on the bench is a Zaber **X-LSQ300A-E01** (serial 143153) with
**300 mm** travel and a built-in encoder — confirmed from the live
device on 2026-05-27. So the absolute maximum envelope is:

```json
"position_limits_mm": [0, 300]
```

## device_info — verified live (2026-05-27)

`safety_config.json` now carries the real device identity:

```
name:             X-LSQ300A-E01
serial_number:    143153
device_id:        50138
firmware_version: 7.48.24004
device_type:      Linear Stage
axis_count:       1
```

The runner doesn't use `device_info` for safety enforcement (that's
purely `position_limits_mm` + `max_velocity_mm_s`) — it's recorded into
`meta.json` for traceability. If you ever see this block say
`serial_number: MOCK` again, that means `_get_device_info()` in
`zaber_stage.py` failed to read identity (logger warning will say why
since we replaced the bare `except:` with `except Exception as e` on
2026-05-27). Common cause: stage controller power off while the FTDI
bridge is up.
