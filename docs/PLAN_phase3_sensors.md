# PLAN — Phase 3: Sensor configuration

> **Self-contained handoff doc.** Designed to be picked up by a fresh AI agent or operator who has not seen the prior conversation. Read top-to-bottom.

**Status:** **deferred** — not yet ready to execute. Schedule once Phase 1 + Phase 2.1 are bench-complete.

**Owner:** Yilin.

**Last edited:** 2026-05-24.

---

## TL;DR

Get the two physical sensors (Omega LCA-9PC load cell amplifier, Keyence IL-030 laser displacement) wired into the ADS1263 EVM, calibrated, and producing readings in engineering units (Newtons and micrometers). Document the calibration constants and procedures so they can be re-derived later when the rig drifts.

Three sub-tasks:

| # | Sub-task | Bench effort | Firmware/script effort |
|---|---|---|---|
| 3.1 | Decide which AIN pairs the load cell and laser go on | 5 min | doc updates only |
| 3.2 | Wire load cell to the EVM and calibrate (zero + span) | ~1 hour bench + 30 min amp warm-up | minor — record constants in operator memo |
| 3.3 | Recalibrate the laser head on the bare EVM (the old constants are invalid) | ~1 hour | update `Experiment_SMACharacterizationV2/` defaults |

---

## Why this is deferred

Phase 1 (chip characterization) and Phase 2.1 (self-calibration verification) must pass first. Reason:

- If the chip's noise floor or DC accuracy is off, you can't tell whether a "weird" sensor reading is the sensor, the cabling, or the ADC.
- Sensor calibration constants are sensitive to where the sensor sits on the ADC's transfer curve. If the ADC isn't linear, the calibration will be wrong in a way that masks itself.
- Phase 1.3 (AIN-pair scan, in `PLAN_phase1_followups.md`) is a hard prerequisite for **3.1** — it tells you which AIN pairs are actually usable on this EVM. Without it you'd be guessing.

---

## Prerequisites

- `PLAN_phase1_followups.md` complete — specifically, cp7 (AIN-pair scan) has identified which non-reference AIN pairs work on the EVM.
- Phase 2.1 (self-calibration verification) complete — captured `OFCAL` / `FSCAL` values for the AIN pair the load cell will use.
- Load cell mechanically mounted in the test fixture, LCA-9PC amplifier physically connected and powered.
- Keyence IL-030 controller wired to its amplifier and powered.
- Zaber stage operational and reachable from the host PC (`Driver_ZaberStage/` module passes its self-test).

---

## Sub-task 3.1 — AIN pair assignment

**Goal:** decide on the AIN pair for each sensor and document it.

**Inputs:**
- cp7 results: which non-reference AIN pairs read cleanly on this EVM.
- Sensor characteristics:
  - **Load cell (via LCA-9PC):** unipolar 0–5 V output, low-bandwidth (mechanical < 100 Hz). Wants high resolution and stability. ADC1 is the right ADC for this.
  - **Keyence IL-030:** voltage output, low-bandwidth (< 100 Hz mechanical). Wants enough resolution to resolve sub-µm. ADC1 or ADC2 — `Firmware_SensorHub_PIO/` design puts it on ADC2 to allow simultaneous load + displacement at different rates.
- Constraints:
  - AIN0 / AIN1 = REF7050 reference → off-limits.
  - Both sensors must be **differential** in the connection (use a pair like AIN2/3 or AIN4/5), not single-ended against AINCOM (AINCOM is reserved for VBIAS during PGA-enabled modes).
  - If cp7 found any pair saturated or misbehaving on the EVM (e.g., AIN2/3 reproduces the legacy HAT issue), exclude that pair.

**Default recommendation** (subject to cp7 results):

| Sensor | AIN pair | ADC | Notes |
|---|---|---|---|
| Load cell (LCA-9PC out) | **AIN2 / AIN3** | ADC1 | If cp7 shows AIN2/3 healthy. If not, fall back to AIN4/5. |
| Keyence IL-030 (analog out) | **AIN4 / AIN5** | ADC2 | Sharing the REF7050 reference (`ADC2CFG.REF2 = 001`). Standalone if load cell takes AIN4/5. |

**Deliverables:**
- Update [`MEMO_cable_map.md`](MEMO_cable_map.md): add **Cable 3 — Load cell amp output ↔ EVM AIN?/AIN?** and **Cable 4 — Keyence IL controller ↔ EVM AIN?/AIN?**. Use the existing Cable 1 / Cable 2 entries as the template.
- Update [`MEMO_sensor_setup.md`](MEMO_sensor_setup.md) (currently TODO/empty per `doc/README.md`) with the actual pair assignments and any cable color codes.
- Update `Firmware_SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h` constants for the assigned pairs (deferred to Phase 4 — just note them here).

**Acceptance:** both sensors have a documented AIN pair, both pair assignments cross-reference cp7 results, both rows added to the cable map.

---

## Sub-task 3.2 — Load cell wiring and calibration

**Goal:** load cell reads force in Newtons, with documented offset and slope.

**Procedure:**

1. **Pre-power checks.** Confirm LCA-9PC excitation voltage is correct for the load cell (typically 10 V). Confirm load cell mechanical mount is rigid (no slop, no preload). Note both in `MEMO_sensor_setup.md`.

2. **Wire it.** From LCA-9PC analog out (0–5 V referenced to amp ground) to the assigned AIN pair on the EVM. The amp output is single-ended; route to AIN_P, with AIN_N either tied to the amp ground at the EVM, or used as the amp ground sense input (true differential). The latter is preferred — it rejects ground-loop noise. Add to `MEMO_cable_map.md` Cable 3 with wire colors / connector types.

3. **Warm up.** **Per LCA-9PC manual (in [`LCA9PC_LCARTC_LoadCellAmp_Manual.pdf`](LCA9PC_LCARTC_LoadCellAmp_Manual.pdf)) the amp needs 30 minutes of warm-up before calibration measurements.** Set a timer.

4. **Zero offset.** With **no load** on the cell, capture ~10 s of samples at 400 SPS / PGA=1 (use `ADS1263_FirstPowerUp_PIO/` or write a tiny calibration sketch). Compute the mean. This is the **offset voltage** at zero load — typically a few mV.

5. **Span.** Place a calibration weight of known mass `m_cal` on the cell. Capture ~10 s of samples. Compute the mean. This is the **loaded voltage** at known force `F_cal = m_cal × 9.80665 N/kg`.

6. **Compute constants.** Slope `k = (V_loaded − V_offset) / F_cal` in V/N. Offset `V₀ = V_offset` in V.

7. **Convert in software.** `F (Newtons) = (V_measured − V₀) / k`.

8. **Verify with a second weight.** Place a different calibration weight, predict the voltage from the constants, compare to measured. Should agree within 1% (or whatever the load cell's specified accuracy is).

9. **Document.** Record in `MEMO_sensor_setup.md`:
   - Calibration date
   - Operator
   - Reference weights used (mass, certification class if any)
   - Resulting `k` and `V₀`
   - Cross-check measurement (point 8)
   - Any anomalies

**Deliverables:**
- Calibration constants in `MEMO_sensor_setup.md`.
- Updated `Firmware_SensorHub_PIO/` config (deferred to Phase 4 — just record the constants here for now).
- Bench log saved under (somewhere appropriate — TBD, suggest `Firmware_SensorHub_PIO/data/loadcell_cal_YYYYMMDD.log`).

**Acceptance:** load cell reads expected force values within 1% across the load range you've calibrated, after 30 min warm-up.

---

## Sub-task 3.3 — Laser head recalibration

**Goal:** laser reads displacement in µm with a slope and offset valid for the bare EVM hardware path.

**Why this is needed (background for the agent):** the existing calibration constants in `Experiment_SMACharacterizationV2/` were derived through the **legacy Waveshare HAT**, which had a 4.4× input attenuation network on the load-cell front end. The IL-030 calibration was indirectly affected because all sensor readings went through the same conditioned path. On the bare TI EVM there is no input attenuator — the AIN inputs have only the small RC filters built into the EVM. So the old constants (`k ≈ −0.1171 mV/µm`, `V₀ ≈ 566.957 mV`) are **invalid** on the new hardware and will produce ~4.4× wrong displacement readings.

**Existing tooling:** the [`../Calibrate_LaserHead/`](../Calibrate_LaserHead/) module already does this calibration. It walks the Zaber stage through a known displacement sweep, captures the IL-030 voltage at each point via the H7+ADS1263, and fits `V = k·µm + V₀`. Reuse it — don't reinvent.

**Procedure:**

1. **Confirm `Calibrate_LaserHead/` works on the new hardware.** The module's `portenta_reader.py` talks to the H7 over serial. Make sure:
   - The pin defines in whatever firmware it's flashed against match the Mid Carrier (`PA_8 / PC_6 / PC_7`).
   - The `INPMUX` selected matches the laser's new AIN pair (from Phase 3.1).
   - `REFMUX = 0x09` (external REF7050), `VREF = 5.0 V` in the volts-per-code math.
   - VBIAS is on if PGA gain > 1 (it shouldn't be needed for IL-030 — single-ended low-voltage signal, fine at PGA=1 PGA bypass or PGA=1 enabled with VBIAS).

2. **Set up the IL-030 mechanically.** Mount at its 30 mm reference distance. Confirm the controller's V/mm scaling — default is 1 V/mm in voltage-output mode but should be re-checked against the manual setting on the controller front panel. Record in `MEMO_sensor_setup.md`.

3. **Run the calibration.** Follow [`../Calibrate_LaserHead/README.md`](../Calibrate_LaserHead/README.md). The module produces a fit report (`k`, `V₀`, R², residuals).

4. **Update `Experiment_SMACharacterizationV2/`.** Replace the default `k` and `V₀` in:
   - `Experiment_SMACharacterizationV2/config.yaml` (the operator-tunable defaults)
   - `Experiment_SMACharacterizationV2/config.py` (if the laser constants are also baked into Python defaults)
   - `Experiment_SMACharacterizationV2/session.py` — search for `laser_calibration_reference` block

5. **Sanity-check the new constants.** With the new `k`/`V₀` in place, run a short SMA characterization and confirm displacement readings match a known stage position to within a few µm.

**Deliverables:**
- New calibration constants in `Experiment_SMACharacterizationV2/` config files.
- Bench log + fit plot in `Calibrate_LaserHead/data/`.
- Note in `MEMO_sensor_setup.md` with date, operator, and the IL-030 controller's V/mm setting in use.

**Acceptance:** SMA recorder produces displacement readings within ±5 µm of the Zaber's commanded position across the ±5 mm IL-030 range. R² of the laser calibration fit should be > 0.999 (linear sensor over its specified range).

---

## After Phase 3 — what unblocks

Once all three sub-tasks pass:

- `MEMO_sensor_setup.md` has real content (currently TODO/empty).
- `Firmware_SensorHub_PIO/` can be configured with correct pin defines + AIN pairs + calibration constants for both sensors. This is **Phase 4**, in `PLAN_phase4_production.md`.
- The full SMA characterization workflow can be re-validated end-to-end on the new hardware.

---

## References

- [`MEMO_baseline_testing.md`](MEMO_baseline_testing.md) — parent plan; this PLAN implements its Phase 3.
- [`MEMO_cable_map.md`](MEMO_cable_map.md) — wiring source of truth. Add Cable 3 (load cell) and Cable 4 (laser) here.
- [`LCA9PC_LCARTC_LoadCellAmp_Manual.pdf`](LCA9PC_LCARTC_LoadCellAmp_Manual.pdf) — load cell amplifier setup, 30 min warm-up note.
- [`KeyenceIL_LaserSensor_Manual.pdf`](KeyenceIL_LaserSensor_Manual.pdf) — IL-030 specifications, voltage-output mode.
- [`../Calibrate_LaserHead/README.md`](../Calibrate_LaserHead/README.md) — laser calibration procedure (existing module to reuse).
- [`../Experiment_SMACharacterizationV2/config.yaml`](../Experiment_SMACharacterizationV2/config.yaml), [`../Experiment_SMACharacterizationV2/session.py`](../Experiment_SMACharacterizationV2/session.py) — where the laser calibration constants live.
- [`../TODO.md`](../TODO.md) — cross-cutting items: "Recalibrate the laser head on the EVM" and the load-cell AIN-pair decision are tracked there too.
- `PLAN_phase4_production.md` — what Phase 3 unblocks.
