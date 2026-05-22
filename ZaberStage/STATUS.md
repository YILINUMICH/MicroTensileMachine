# ZaberStage — STATUS

| Field | Value |
|---|---|
| **Status** | **Stable** — v1.0 (November 2025) |
| **Role** | Python wrapper around the Zaber Motion library: device auto-discovery, JSON config persistence, 100 Hz position reads, velocity + absolute position control, safety limits, thread-safe. |
| **Used by** | `Calibrate_LaserHead/` (imports `zaber_stage.py` via `sys.path` shim). Will be the stage-control layer for any future closed-loop tensile test runner. |
| **Owner** | Yilin |
| **Quick test** | `python ONETIME_INIT.py` (first time) → `python test_zaber_stage.py` for the full test suite. |
| **Devices supported** | Tested with Zaber X-LRM200A linear stage. Should work with any zaber-motion 7.x compatible device. |

## Module TODOs

- [ ] **Document which COM port is currently the stage in `MEMO_cable_map.md`** under `../doc/`. (Currently hard-coded as `COM5` in `Calibrate_LaserHead/config.yaml`.)
- [ ] **Consider adding a "soft limit + hard limit" two-tier check** — currently `position_limit_mm` is enforced but there's no separate emergency-stop margin.

See [../TODO.md](../TODO.md) for cross-cutting items.
