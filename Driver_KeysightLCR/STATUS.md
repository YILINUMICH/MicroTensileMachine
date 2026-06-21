# Driver_KeysightLCR — STATUS

| Field | Value |
|---|---|
| **Status** | **Stable** |
| **Role** | Python wrapper around the Keysight E4980A / E4980AL LCR meter over PyVISA. Supports USB and LAN (VXI-11 / HiSLIP / raw socket). Optimized for maximum read rate (24.5 readings/s with display off, short integration). |
| **Used by** | `Experiment_SMACharacterizationV2/workers.py` imports `LCRMeter`, `MeasurementConfig`, `MeasurementFunction` directly via a `sys.path` shim — this module is the canonical LCR driver, no duplicates. `SMA_Characterization/` (v1, archived) still has its own legacy `lcr_reader.py` — left alone since the folder is frozen. |
| **Owner** | Yilin |
| **Quick test** | `python test_lcr_meter.py --quick` for a connection check; `python test_lcr_meter.py --bench` for a performance benchmark. |
| **Connection** | USB (Keysight IO Libraries preferred) or LAN; set `LCR_IP` or `LCR_MAC` env var for explicit LAN. |

## Module TODOs

- [ ] **Pin the OPEN / SHORT / LOAD test fixture compensation state of the actual instrument** in `MEMO_lcr_setup.md` under `../doc/`. The Python wrapper turns instrument-side correction OFF for the SMA workflow, but if someone else uses this module standalone they need to know what's loaded on the meter.
- [ ] **Document expected throughput** at the LAN IP currently in use (`169.254.157.92` per README) under a `BENCHMARK.md` so regressions are obvious.

See [../TODO.md](../TODO.md) for cross-cutting items.
