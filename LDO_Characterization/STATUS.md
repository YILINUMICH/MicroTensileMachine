# STATUS — LDO_Characterization

**Status: To-Test** (builds/imports clean; analysis validated on synthetic data;
**not yet bench-verified end-to-end**). Treat captured numbers skeptically until
the first real run is sanity-checked.

## What works
- `analyze_ldo.py` — metric extraction + plots, validated offline on synthetic
  captures (settle time, overshoot, 10-90% rise, ripple bars).
- `run_experiment.py --dry-run` — prints the full shot plan with no hardware.

## TODO / to verify on the bench
- [ ] Flash the updated `SMA_Driver_PIO` firmware (adds `TRIG_PIN` PJ_11/PWM4 + `fire`).
- [ ] Trigger pin is **PJ_11** = Mid Carrier silkscreen **PWM4** (J2-67), next to
      PWM3/PG_7 (MOSFET) — confirmed on the pinout. Just verify the scope sees a
      clean 3.3 V edge on C1.
- [ ] Verify the scope trigger SCPI (`TRSE`/`TRMD`/`ARM`) and the
      `INR?`/`SAST?` completion poll in `scope_trigger.py` against the
      SDS2000X Plus Programming Guide. If `wait_capture_complete` never returns,
      the register bit/semantics differ — adjust there.
- [ ] Verify `codes_per_div = 25.0` for absolute volts (settle *time* is
      unaffected; overshoot % needs it right).
- [ ] Size the fixed power resistor to the SMA nominal current at the max test
      voltage; set wattage with margin.
- [ ] **INA296A current sense:** confirm A1 variant (10 V/V), 100 mΩ shunt, and
      OUT ≈ 0 V at 0 A (REF=GND). Trim with firmware `gain` / `shunt` / `ioffset`.
      Cross-check firmware `read` current vs a series DMM at one operating point.
      Confirm INA296A OUT is wired to **A1** (ANA1 pad) and, if probing inrush,
      to scope **C4**.
- [ ] First run: `--dry-run`, then a single loaded `mid_up` shot, eyeball the
      CSV/plot before the full matrix.

## Cross-module deps
- Imports `Oscilloscope`, `ScopeConfig`, `MeasureParam`, `codes_to_volts` from
  `../SiglentOscillosope/oscilloscope.py` via a `sys.path` shim (same pattern as
  the recorder). No local copy.
- Talks to `../SMA_Driver_PIO` firmware over serial (COM8).
