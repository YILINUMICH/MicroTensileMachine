# Calibrate_LaserHead — STATUS

| Field | Value |
|---|---|
| **Status** | **Stable** (tool) — but **calibration constants need re-running on the EVM**. The 2026-04-24 numbers were derived through the old Waveshare HAT's input-stage attenuation; on the bare TI ADS1263 EVM the signal chain is different and `k`/`V₀` will not match. |
| **Role** | Walks the Zaber stage through a fixed displacement sweep (±5 mm around the 30 mm IL-030 reference distance, 51 points × 100 samples by default), captures the laser ADC voltage at each point, fits `V = k·µm + V₀`, writes `points.csv` + `fit.png` + `meta.json`. |
| **Last run** | 2026-04-24 run07 — produced `k = -0.1171 mV/µm`, `V₀ = 566.957 mV` on the **Waveshare HAT** signal path (now superseded). **Currently baked into `SMA_CharacterizationV2/` defaults — flag as stale until re-run on the EVM.** |
| **Owner** | Yilin |
| **Quick test** | `python portenta_reader.py --port COM8 --duration 30` for a stream sanity check; `python run_calibration.py --dry-run` for a 1-minute end-to-end dry run. |
| **Dependencies on other modules** | Imports `zaber_stage.py` from `../ZaberStage/` via a `sys.path` shim in `run_calibration.py`. Needs `../SensorHub_PIO/` flashed with `ENABLE_ADC1 = 1`, `ENABLE_ADC2 = 0` (current workaround for the ADC2 saturation issue — see `README.md` §Firmware prerequisite). |

## Module TODOs

- [ ] **Recalibrate on the TI ADS1263 EVM.** The current `k`/`V₀` were derived through the legacy Waveshare HAT's load-cell front-end (~4.4× attenuation absorbed into `k`). The EVM doesn't have that front-end, so the constants are invalid on the new hardware. Re-run after the first power-up bring-up succeeds, then update `SMA_CharacterizationV2/` defaults and the `laser_calibration_reference` block in `session.py`. On the EVM, if AIN2/AIN3 works (it saturated on the HAT), re-run on ADC2 instead of the ADC1 workaround — see [`../TODO.md`](../TODO.md).
- [ ] **Document the operator setup steps in a memo** (laser standoff procedure, "reference distance LED lit" check). Currently lives only in `README.md` §Physical setup.
- [ ] **Migrate firmware serial format** to the cleaner `<timestamp_us>,<voltage_V>` per plan §2 (currently the parser accepts both the new and old tab-separated form).

See [../TODO.md](../TODO.md) for cross-cutting items.
