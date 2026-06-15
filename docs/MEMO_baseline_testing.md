# MEMO — ADS1263 baseline testing plan

**Status:** active — Phase 0 complete; **Phase 1.1–1.6 + Phase 2.1 all bench-verified 2026-05-24** (chip-level baseline + self-calibration verification COMPLETE). Phase 2.2 / 2.3 remain tabled. Ready to proceed to Phase 3 sensor configuration ([`PLAN_phase3_sensors.md`](PLAN_phase3_sensors.md)).
**Last edited:** 2026-05-24 by Yilin (Phase 2.1 SelfCal bench-verified — SFOCAL1 + SYGCAL1 both work, INTERFACE register survives, AVDD = 5.2056 V locked in from cp10).
**Owner:** Yilin.

The bring-up sketch (`ADS1263_FirstPowerUp_PIO/`) proves the chip is alive on the new hardware. It exercises **one operating point** (400 SPS, PGA bypass, AINCOM-short, external 5 V reference, 100 samples). This memo lays out what comes next — the work needed to **trust** the rig enough to mount sensors and call readings real.

## Document layout

This MEMO is the **near-term active plan**. Work that is deferred or that the operator wants to hand off to a separate agent lives in dedicated PLAN docs:

| Doc | Scope | Status |
|---|---|---|
| **MEMO_baseline_testing.md** (this file) | Phase 0 (housekeeping, done) + Phase 1.1–1.2 (active code work) + Phase 2.1 (next, after Phase 1 bench-test) | active |
| [`PLAN_phase1_followups.md`](PLAN_phase1_followups.md) | Phase 1.3–1.6: cp7 AIN-pair scan, cp8 ADC2 cp, cp9 DRDY edge count, cp10 TDAC sanity | **handoff** — ready when Phase 1.1+1.2 bench-tested clean |
| [`PLAN_phase3_sensors.md`](PLAN_phase3_sensors.md) | Phase 3: AIN-pair assignment, load cell calibration, laser recalibration | **deferred** — gated on Phase 1 + Phase 2.1 |
| [`PLAN_phase4_production.md`](PLAN_phase4_production.md) | Phase 4: SensorHub_PIO port to Mid Carrier, bench-verify, archive legacy modules | **deferred** — gated on Phase 3 |

**Phase 2.2 and 2.3 are TABLED** in the current cycle — see Phase 2 section below for rationale.

The full plan is split into phases that each retire specific TODOs and each have explicit pass/fail criteria. Phases are ordered so prerequisites flow downstream; you can stop or pause cleanly at any phase boundary.

---

## Phase 0 — Housekeeping (complete)

| # | Action | Status |
|---|---|---|
| 0.1 | Name the external reference (REF7050) in `MEMO_cable_map.md` Cable 2 | **done** 2026-05-24 |
| 0.2 | Confirm EVM analog supply config | **done** (desk research) — EVM is **unipolar by design**: AVDD = +5 V from on-board TPS7A4700 LDO, AVSS = GND. Mid-supply = +2.5 V. No operator jumper choice. |

**Implication for everything downstream:** AINCOM should be biased to mid-supply (+2.5 V) for any test with PGA gain > 1. We do this via the **VBIAS bit (bit 1) of the POWER register**, not via the J5:1↔J5:2 jumper. Settling time per datasheet Table 9-7 is ≤ 0.22 ms at 0.1 µF (the EVM's 150 pF on AINCOM is much less, so faster).

---

## Phase 1 — Chip-level baseline

Goal: characterize the ADS1263 silicon on this rig, with no external sensors. After Phase 1 passes, you trust the chip as configured.

**Active here (code written 2026-05-24, bench-test pending):**

| Step | Deliverable | Success criterion |
|---|---|---|
| 1.1 | **VBIAS + PGA mini-sweep** as `cp6` in `ADS1263_FirstPowerUp_PIO/`. AINCOM-short via INPMUX=0xAA, VBIAS on, walk PGA ∈ {1,2,4,8,16,32} at 400 SPS, 200 samples per point. | At each gain, input-referred RMS noise < 50 µV. At gain=1, output-referred RMS ≈ bring-up baseline (1.4 µV). No `RMS == 0` (stuck readings). No saturated codes. |
| 1.2 | **Full noise-floor sweep** as new `ADS1263_NoiseFloor_PIO/`. AINCOM-short, VBIAS on, walk SPS ∈ {10, 50, 100, 400, 1200, 2400, 4800} × PGA ∈ {1, 2, 4, 8, 16, 32}. Sample count scales with SPS (200 min, 2000 max). CSV over serial. Python script in `tools/analyze_noise_floor.py` summarizes into a table comparable to datasheet Table 7.10. | Each (SPS, PGA) cell within ±50% of datasheet Table 7.10 typical RMS noise. Rows failing this become "off-limits operating modes." |

**Handed off to [`PLAN_phase1_followups.md`](PLAN_phase1_followups.md):**

| Step | Summary | Implements |
|---|---|---|
| 1.3 | AIN-pair scan (cp7) — confirm AIN2/3, AIN4/5, AIN6/7, AIN8/9 work on the bare EVM | TODO: "Re-test ADC2/AIN2-AIN3 on the EVM" |
| 1.4 | ADC2 checkpoint (cp8) — confirm the 24-bit secondary ADC streams cleanly | TODO: "Re-test ADC2" |
| 1.5 | DRDY edge-rate count (cp9) — confirm `PC_6` is interrupt-capable | TODO: "Reroute DRDY off PJ_11" |
| 1.6 | TDAC sanity (cp10) — drive a known internal voltage, verify ADC1 measures it | Free DC-accuracy sanity check |

The PLAN doc has register-level implementation hints, code skeletons, acceptance criteria, and per-cp failure triage so a separate agent can pick it up without conversation context.

**Exit criterion for Phase 1:** this MEMO's 1.1+1.2 bench-tested clean **and** PLAN_phase1_followups.md's cp7–cp10 all PASS.

---

## Phase 2 — Accuracy & linearity

**Active here:**

| Step | Deliverable | Success criterion |
|---|---|---|
| 2.1 | **Self-calibration verification.** New module [`../ADS1263_SelfCal_PIO/`](../ADS1263_SelfCal_PIO/) — sketch runs SFOCAL1 (across all PGA gains) + SYGCAL1 (with TDAC-driven 0.8·AVDD ≈ 4.16 V), reads back OFCAL[2:0] and FSCAL[2:0], computes manual offset/gain from raw codes for cross-check, and explicitly checks INTERFACE register survival. **Code written 2026-05-24, bench-test pending.** | Operational tolerance (rationale in module STATUS.md): per-gain offset reduction ≥ 90% after SFOCAL1; FSCAL register non-default after SYGCAL1 with post-cal measurement within ±0.5% of predicted 0.8·AVDD. INTERFACE register must stay at 0x05 after every calibration command (the "snap-back" check from `ADS1263_H7_Integration_Notes.md` §6). |

### Phase 2.2 — DC linearity ⊘ TABLED (2026-05-24)

**Original spec:** build a 10-tap resistor divider from REF7050 with ≤ 0.05% resistors, taps at ~0.5 V … 5 V, sweep all 10 at PGA=1 and PGA=8, compute INL residual.

**Why tabled:** the operator does not currently have the components (precision resistors at ≤ 0.05% tolerance, the divider build, the wiring harness to the EVM screw terminals). Without those, the test can't be executed.

**Re-open when:** precision resistors and a stable divider build are available. Phase 1.6 TDAC sanity (handed off to `PLAN_phase1_followups.md`) is a coarser substitute that doesn't need external components — so the absolute lack of DC-accuracy data is partially mitigated by cp10.

### Phase 2.3 — Long-term drift ⊘ TABLED (2026-05-24)

**Original spec:** AINCOM-short, 400 SPS, log for 60 min, plot moving mean and std in 10 s / 1 min / 10 min windows. Tells you practical drift ceiling for slow experiments.

**Why tabled:** tabled together with 2.2 per operator request.

**Note:** unlike 2.2, this step does *not* require new components — it just needs a 60-minute bench session where the rig is left alone. Worth picking up once there's a quiet bench hour.

**Exit criterion for Phase 2:** 2.1 passes (cal registers landed and verified). 2.2 and 2.3 not part of the current cycle; expected to remain tabled until components / time become available.

---

## Phase 3 — Sensor configuration

**Handed off to [`PLAN_phase3_sensors.md`](PLAN_phase3_sensors.md).** Covers:

- 3.1 AIN-pair assignment for load cell and laser (based on Phase 1.3 cp7 results)
- 3.2 Load cell wiring + calibration (LCA-9PC zero/span, 30 min warm-up, per amp manual)
- 3.3 Laser recalibration on the bare EVM — re-derive `k` and `V₀`, update `SMA_CharacterizationV2/` defaults (the legacy HAT's 4.4× attenuator is gone, so old constants are invalid)

**Status:** deferred — gated on Phase 1 + Phase 2.1 completion. PLAN doc has full procedure, acceptance criteria, and references to the existing `Calibrate_LaserHead/` module that gets reused.

---

## Phase 4 — Production firmware port

**Handed off to [`PLAN_phase4_production.md`](PLAN_phase4_production.md).** Covers:

- 4.1 Port `SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h` to Mid Carrier pin defines (`PA_8/PC_6/PC_7`) + `REFMUX=0x09` + VBIAS + Phase 3 AIN-pair assignments
- 4.2 Bench-verify `SensorHub_PIO/` end-to-end with both sensors live
- 4.3 Move `LoadCell_PIO/` and `LaserHead_PIO/` into `Archieve/` once SensorHub is verified

**Status:** deferred — gated on Phase 3 completion. PLAN doc has the legacy-to-new pin mapping table, configuration deltas, per-step failure triage, and the archive procedure.

---

## Critical path

```
Phase 0 ✓ → Phase 1.1 → Phase 1.2 ─┬─→ Phase 2.1 ────────────────────────┐
   (this MEMO)         (this MEMO) │  (this MEMO)                        │
                                   │                                     │
                                   └─→ Phase 1.3,1.4,1.5,1.6 ── Phase 3 ─┤→ Phase 4
                                       (PLAN_phase1_followups)  (PLAN)   │  (PLAN)
                                                                         │
                                       Phase 2.2, 2.3 ⊘ TABLED ──────────┘
```

Phase 1 steps 1.3–1.6 (in `PLAN_phase1_followups.md`) are mutually independent — they can be implemented in any order within that PLAN. Phase 2.1 is independent of Phase 1.3–1.6. Phase 3 needs Phase 1.3 (cp7) result for the AIN-pair assignment. Phase 4 is the convergence point.

---

## Time / effort estimate

| Phase | Where | Bench time | Firmware/script time |
|---|---|---|---|
| 0 | (done) | — | — |
| 1.1 + 1.2 | this MEMO | ~20 min to flash both + run sweep | done |
| 1.3–1.6 | PLAN_phase1_followups | ~10 min total (one flash) | ~3–4 hours for the agent to implement |
| 2.1 | this MEMO | ~15 min | ~1–2 hours |
| 2.2, 2.3 | tabled | — | — |
| 3 | PLAN_phase3_sensors | ~2–3 hours | ~1 hour |
| 4 | PLAN_phase4_production | depends on debugging | depends on debugging |

The active near-term path is ~2 hours of bench work (after the current code-writing is done): flash + run NoiseFloor sweep, flash + run cp7–cp10, run self-cal verification. Phases 3 and 4 are an afternoon each when the rig is ready for them.

---

## Conventions

- **One step = one bench session, at minimum.** Even if a step is "just" a flash and a log capture, treat it as a discrete event with its own dated data file in the relevant module's `data/` folder.
- **Capture everything.** Every bench run produces a `.log` or `.csv` named `<step>_<date>_<time>.{log,csv}`. The bring-up sketch's `data/firstpowerup_20260522_1726.log` is the pattern.
- **Update this memo as steps complete.** Tick off the checkboxes, add a "Result:" line under each step pointing to the data file. Future-you needs to be able to reconstruct what was run when.
- **If a step fails:** don't bury it. Add a `FAIL` note under the step with the symptom and what was tried before moving on or pivoting. The historical issues in `ADS1263_H7_Integration_Notes.md` §6 ("Lessons Learned from Arduino Uno Testing") are gold precisely because someone wrote them down.

---

## Result log (fill in as steps complete)

| Step | Date | Data file | Outcome | Notes |
|---|---|---|---|---|
| 0.1 | 2026-05-24 | `doc/MEMO_cable_map.md` Cable 2 | done | REF7050 named in cable map |
| 0.2 | 2026-05-24 | (this memo) | done | EVM is unipolar +5/0 V by design; VBIAS chosen as AINCOM bias method |
| 1.1 | 2026-05-24 | [`../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1759.log`](../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1759.log) | **PASS** | cp6 PGA sweep clean across all gains; cp5 baseline reproduced at 1.257 µV RMS. Notable: chip's residual offset is post-PGA (output-referred mean constant at ~740 µV) rather than input-referred. Healthy pattern, just different from README's illustrative example. |
| 1.2 | 2026-05-24 | [`../ADS1263_NoiseFloor_PIO/data/noisefloor_20260524_1845.csv`](../ADS1263_NoiseFloor_PIO/data/noisefloor_20260524_1845.csv) | **PASS** | All 42 cells in-spec; offset rock-stable across SPS (12 nV span at gain 1); NFB 17.0–22.8 bits (datasheet typical 17–18); anchor (400 SPS, gain 1) = 1.290 µV vs cp5's 1.257 µV. Two cells (50 SPS, gain 2/4) flagged stuck_pct = 0.6%/0.8% — counting-statistics noise, not operationally significant. |
| 1.3 | 2026-05-24 | [`../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log`](../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log) | **PASS** | cp7 AIN-pair scan: all 8 pair configs PASS, no saturation. Legacy AIN2/3 question RETIRED. |
| 1.4 | 2026-05-24 | [`../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log`](../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log) | **PASS** | cp8 ADC2 enable+read: 8.5 µV RMS at 100 SPS Sinc3 gain=1 (datasheet typical 10.3 µV). Unblocks SensorHub_PIO dual-ADC mode. Bug found+fixed: original ADC2MUX=0x4A read floating AIN4, picked up EMI; changed to 0xAA (AINCOM-shorted) to measure intrinsic noise floor. |
| 1.5 | 2026-05-24 | [`../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log`](../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log) | **PASS** | cp9 DRDY edge-rate: 4007/4000 falling edges in 10 s on PC_6. Interrupt-driven reads viable on Mid Carrier; legacy PJ_11/LoRa IRQ conflict does not apply here. |
| 1.6 | 2026-05-24 | [`../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log`](../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260524_1925.log) | **PASS** | cp10 ratiometric TDAC: AVDD derived = 5.2056 V mean, 24.9 mV span across 5 rows. EVM's TPS7A4700 LDO output is 5.2 V (in spec, trim choice). Bug found+fixed: original test assumed AVDD=5.0V exactly; per datasheet §9.3.14, TDAC outputs scale with AVDD, so we rewrote as a ratiometric AVDD derivation. |
| 2.1 | 2026-05-24 | [`../ADS1263_SelfCal_PIO/data/selfcal_20260524_1949.log`](../ADS1263_SelfCal_PIO/data/selfcal_20260524_1949.log) | **PASS** | SFOCAL1 sweep: 94–100% offset reduction across PGA gains; predicted vs actual OFCAL agree within ~80 LSB. SYGCAL1 demo: FSCAL math matches prediction to 1.4 ppm of FS (post-cal = 5.0000 V = exactly +VREF). **INTERFACE register survived all 7 calibration commands** — legacy-HAT snap-back issue does NOT reproduce on EVM. Defensive register re-write retained as production safety net. cp3 bug found+fixed: initial test misinterpreted SYGCAL1 as "make output match input"; correct behavior is "normalize input to +VREF" per §9.4.9.6. |
| 2.2 | — | — | ⊘ TABLED 2026-05-24 | no precision-resistor divider components on hand |
| 2.3 | — | — | ⊘ TABLED 2026-05-24 | deferred together with 2.2; doesn't need new components, just bench time |
| 3.1 | | | moved to PLAN_phase3_sensors.md | AIN-pair assignment |
| 3.2 | | | moved to PLAN_phase3_sensors.md | load cell wiring + calibration |
| 3.3 | | | moved to PLAN_phase3_sensors.md | laser recalibration on bare EVM |
| 4.1 | | | moved to PLAN_phase4_production.md | port `SensorHub_PIO` pin defines + config |
| 4.2 | | | moved to PLAN_phase4_production.md | bench-verify SensorHub end-to-end |
| 4.3 | | | moved to PLAN_phase4_production.md | archive LoadCell_PIO + LaserHead_PIO |

---

## Related

**Handoff PLAN docs (siblings of this MEMO):**

- [`PLAN_phase1_followups.md`](PLAN_phase1_followups.md) — Phase 1.3–1.6 (cp7 AIN-pair scan, cp8 ADC2, cp9 DRDY, cp10 TDAC). Pickup-ready for a separate agent.
- [`PLAN_phase3_sensors.md`](PLAN_phase3_sensors.md) — Phase 3 sensor configuration. Deferred — gated on Phase 1 + Phase 2.1.
- [`PLAN_phase4_production.md`](PLAN_phase4_production.md) — Phase 4 production firmware port. Deferred — gated on Phase 3.

**Other rig documentation:**

- [`MEMO_cable_map.md`](MEMO_cable_map.md) — wiring source of truth (Cable 1 = SPI bus, Cable 2 = REF7050 reference; Cables 3 and 4 for sensors will be added in Phase 3).
- [`ADS1263_Datasheet.pdf`](ADS1263_Datasheet.pdf) — Table 7.10 (noise vs SPS × PGA), §9.6.6 (MODE2), §9.6.2 (POWER → VBIAS bit 1), §9.3.12 (TDAC), §9.6.16 (ADC2 group).
- [`ADS1263_EVM_User_Guide.pdf`](ADS1263_EVM_User_Guide.pdf) — §3.1.1.5 (TDAC on AIN6/AIN7), §5 (supply rails / TPS7A4700 LDO).

**Active code modules:**

- [`../ADS1263_FirstPowerUp_PIO/`](../ADS1263_FirstPowerUp_PIO/) — where cp0–cp6 live; cp7–cp10 will be added per PLAN_phase1_followups.md.
- [`../ADS1263_NoiseFloor_PIO/`](../ADS1263_NoiseFloor_PIO/) — full noise-floor sweep (Phase 1.2 deliverable).
- [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) §4 (register config history), §6 (lessons learned), §5 (legacy DRDY/LoRa conflict context).
- [`../TODO.md`](../TODO.md) — cross-cutting items, especially "Re-test ADC2/AIN2-AIN3 on the EVM" and "Recalibrate the laser head on the EVM".
