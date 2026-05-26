# ZaberStage/safety_config.json — operator notes

These notes live alongside `safety_config.json` because JSON itself can't
carry comments. Read this before widening / narrowing the limits.

## Current state (2026-05-26)

```
position_limits_mm: [5, 15]   ← TIGHT (calibration-only)
max_velocity_mm_s:  5.0
reading_rate_hz:    100.0
device_info:        Mock      ← stale; regenerate when real Zaber is wired
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

Check the Zaber X-LRM200A datasheet for the real maximum (likely
200 mm, but verify against the connected unit).

## ⚠ device_info is stale (Mock)

`device_info` still says `serial_number: MOCK`, `device_type: Mock`,
timestamp 2026-04-23. If the real Zaber X-LRM200A is now connected,
regenerate `safety_config.json` from the live device (e.g. via the
`save_discovered_devices()` helper in `zaber_stage.py`) so the
`device_info` block matches the actual unit, and re-derive
`position_limits_mm` from the real travel envelope before widening.

The runner doesn't actually use `device_info` for safety enforcement
(that's purely `position_limits_mm` + `max_velocity_mm_s`), so a stale
`Mock` block won't break anything during calibration — it just records
the wrong identity in `meta.json`.
