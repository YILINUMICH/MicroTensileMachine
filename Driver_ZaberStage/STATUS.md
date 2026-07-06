# ZaberStage — STATUS

| Field | Value |
|---|---|
| **Status** | **Stable** — v1.0 (November 2025) |
| **Role** | Python wrapper around the Zaber Motion library: device auto-discovery, JSON config persistence, 100 Hz position reads, velocity + absolute position control, safety limits, thread-safe. |
| **Used by** | `Calibrate_LaserHead/` and `Experiment_SMACharacterizationV3/` (both import `zaber_stage.py` via a `sys.path` shim — single source, no copies). Will be the stage-control layer for any future closed-loop tensile test runner. |
| **Owner** | Yilin |
| **Quick test** | `python ONETIME_INIT.py` (first time) → `python test_zaber_stage.py` for the full test suite. Home-direction/coordinate check: `python diag_home.py` (read-only) / `python diag_home.py --home` (homes + re-reads). |
| **Devices supported** | In use on this rig: **Zaber X-LSQ300A-E01** (serial 143153, device_id 50138, firmware 7.48.24004) on COM5 — 300 mm travel, built-in encoder. Should work with any zaber-motion 7.x compatible device. |

## Recent changes

- **Serial-transaction lock (2026-07-06).** All axis/connection commands
  (`home` / `move_to` / `move_velocity` / `stop`) and the background position
  reader now run under a single `_serial_lock` (RLock). Previously the reader
  thread and any command shared one serial `Connection` with no lock, so their
  ASCII request/reply frames interleaved and commands were silently dropped
  ("move works only sometimes"). `move_to` also now logs a not-homed refusal
  and any limit clamp instead of returning a silent `False`. **To-Test on the
  bench.**
- **`diag_home.py` added** to investigate the "console home is on the opposite
  end vs Zaber Launcher" report — prints firmware home/limit settings + position
  before/after an optional `axis.home()`, without changing any setting.

## Module TODOs

- [ ] **Confirm home direction / coordinate convention** with `diag_home.py`
  against a Launcher home; decide the fix (firmware home-direction setting vs a
  console-side coordinate offset). Verify the `[5, 40] mm` workflow window is on
  the intended end of the 300 mm travel.
- [ ] **Document which COM port is currently the stage in `MEMO_cable_map.md`** under `../doc/`. (Currently hard-coded as `COM5` in `Calibrate_LaserHead/config.yaml`.)
- [ ] **Consider adding a "soft limit + hard limit" two-tier check** — currently `position_limit_mm` is enforced but there's no separate emergency-stop margin.

See [../TODO.md](../TODO.md) for cross-cutting items.
