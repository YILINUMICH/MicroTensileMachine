# STATUS — Experiment_LDOCharacterization

**Status: To-Test — dynamics verified, absolute volts deferred** (bench runs
2026-06-16/17, `data/ldo_2026061*`). The **dynamic metrics — settling time,
overshoot, 10–90% rise, and ripple — are bench-verified** across loaded/unloaded
× small/mid/large steps. **Absolute voltage is NOT yet trusted:** `codes_per_div`
is still unconfirmed, so any absolute-volt number (and overshoot % that depends
on it) carries that caveat. Settle *time* is independent of the scaling and is
solid.

## What works (bench-verified)
- End-to-end capture: arm scope → H7 `fire` (PJ_11/PWM4 edge on C1) → wait
  capture-complete → pull waveforms. Scope-trigger SCPI path debugged during
  bring-up (see [`diag/TRIGGER_DEBUG.md`](diag/TRIGGER_DEBUG.md)).
- `analyze_ldo.py` — settle / overshoot / rise / ripple metrics + plots, run on
  the real captures (`summary.csv` populated for all 10 runs).
- `run_experiment.py` — full settling + ripple matrix; `--dry-run` for the plan.

## Folder layout
- Core experiment: `config.yaml`, `h7_serial.py`, `scope_trigger.py`,
  `run_experiment.py`, `analyze_ldo.py`, `data/`.
- One-off debug scripts moved to [`diag/`](diag/) (Diagnostic — not part of the
  flow): `scope_probe.py`, `diag_arm.py`, `diag_loop.py`, `TRIGGER_DEBUG.md`.

## Still open (the absolute-accuracy tail)
- [ ] Verify `codes_per_div = 25.0` against the SDS2000X Plus Programming Guide —
      this is the gate on trusting absolute volts (and overshoot %).
- [ ] Trim firmware `vdd`/`offset` against a meter so `set`/`drive` hit ±0.1 V.
- [ ] **INA296A current sense:** confirm A1 (10 V/V), 100 mΩ shunt, OUT ≈ 0 V at
      0 A; cross-check firmware `read` current vs a series DMM at one operating
      point. Trim with `gain` / `shunt` / `ioffset`.
- [ ] Size the fixed power resistor to the SMA nominal current at max test
      voltage; set wattage with margin.

## Cross-module deps
- Imports `Oscilloscope`, `ScopeConfig`, `MeasureParam`, `codes_to_volts` from
  `../Driver_SiglentOscilloscope/oscilloscope.py` via a `sys.path` shim (same pattern as
  the recorder). No local copy.
- Talks to `../Firmware_SMADriver_PIO` firmware over serial (COM8).
