# `Experiment_SpringSmokeTest/` — Phase 5 sensor-integration + 1 kSPS pipeline test

Spring-as-SMA-surrogate smoke test for the SensorHub firmware. Uses the load-cell-calibration spring as a known mechanical input so the laser-displacement and load-cell channels can be validated against each other and against Hooke's-law ground truth, while at the same time stressing the M4 → ring buffer → M7 → USB-CDC sample pipeline at 1 kSPS to confirm headroom before Phase 6 (SMA addition).

Motivating plan: [`../doc/PLAN_phase5_spring_smoke_test.md`](../doc/PLAN_phase5_spring_smoke_test.md).

---

## What this module contains

| File | Purpose |
|---|---|
| `portenta_reader.py` | USB-CDC reader; parses sample TSV (3/4/5/6-col) and `[STATUS]` telemetry frames; same import surface as the calibrate-module readers, with `read_streaming(duration_s)` added for the smoke test. |
| `run_spring_smoke_test.py` | Six-step test orchestrator. `--step` selects which to run. Writes raw sample/status/stage logs and a `meta.json` under `data/<YYYY-MM-DD>_run<NN>/`. |
| `analyze.py` | Per-step post-run analysis. Computes noise σ vs cal reference, F-vs-x linearity + slope vs spring k, laser-vs-Zaber slope, settling time, fast/slow overlay, sequence-gap detection, status-frame timeline. One SVG per executed step + `summary.json`. |
| `config.yaml` | All test parameters: spring k, sensor cal references, COM ports, per-step motion/duration, sample-rate annotation. |
| `requirements.txt` | Pinned to the same versions as the sibling calibrate modules. |
| `data/` | Per-run output directories. Each run creates its own subdirectory; nothing is shared between runs. |

---

## Test procedure (six steps)

Detailed pass criteria live in the plan doc; the short version:

| # | Motion | What it checks | Plot |
|---|---|---|---|
| 1 | Stage idle at 10 mm for 60 s | Static noise σ per channel vs cal-run reference noise floors | `step1.svg` |
| 2 | Quasi-static ramp 10 → 16 mm at 0.1 mm/s | F-vs-x slope ≈ spring `k_cal`; laser-vs-Zaber slope ≈ 1.0 outside the slack region | `step2.svg` |
| 3 | Step from 10 mm to 14 mm and hold | Settling time per channel; no overshoot ringing | `step3.svg` |
| 4 | Fast pull (3 mm/s) / slow return (0.1 mm/s) | Forward and reverse F-vs-x curves overlay (low hysteresis ⇒ no channel sync skew) | `step4.svg` |
| 5 | Stage idle, 10-minute endurance | Zero sequence gaps; ring HWM < 50%; zero dropped samples | `step5.svg` |
| 6 | Step-4 motion at 1 kSPS firmware | Same diagnostics as step 5 but with concurrent Zaber serial traffic | `step6.svg` |

Steps 1–5 are the default `--step all`. Step 6 is opt-in (`--step all+6`) because it requires the 1 kSPS firmware build to be loaded first.

---

## Running the test

Prerequisites:

1. SensorHub firmware flashed and the rig power-cycled (per `Firmware_SensorHub_PIO/README.md`).
2. Spring installed in the same fixture as the load-cell calibration. Zaber stage at or near absolute 10 mm with spring slack — pre-tensioning is intentionally not used; the slack region is identified in post-processing.
3. `config.yaml` checked: COM ports, `sample_rate_sps` set to match the flashed firmware, cal-reference values up to date with the latest `Calibrate_*` runs.

```bash
# Install deps once
pip install -r requirements.txt

# Probe the serial stream — confirms firmware is talking before you commit
# to a real run. Reports per-source sample rate, whether seq/hw_us are
# present, and any status frames seen.
python portenta_reader.py --port COM8 --duration 30

# Full smoke test (steps 1–5)
python run_spring_smoke_test.py

# Single step
python run_spring_smoke_test.py --step 2

# Run including the 1 kSPS concurrent-comms stress (requires 1 kSPS firmware)
python run_spring_smoke_test.py --step all+6 --sample-rate 1000

# Analyze
python analyze.py                  # picks up the latest run automatically
python analyze.py data/2026-05-29_run01
```

---

## Output layout

Each invocation creates a fresh directory under `data/`:

```
data/2026-05-29_run01/
├── meta.json               # config, firmware annotation, cal references, per-step results
├── run.log                 # runner log (same content as stderr)
├── samples_step1.csv       # every Sample row captured during step 1
├── status_step1.csv        # every [STATUS] frame seen during step 1
├── stage_log_step1.csv     # Zaber position log at ~50 Hz
├── samples_step2.csv
├── status_step2.csv
├── stage_log_step2.csv
├── ...
└── (after analyze.py)
    ├── step1.svg
    ├── step2.svg
    ├── ...
    └── summary.json        # PASS/FAIL per check, key numbers
```

Sample CSV columns: `wall_us, step, phase, fw_t_us, src, seq, hw_us, raw_code, voltage_V, stage_mm`. The `seq` and `hw_us` columns are empty until the Phase 5 firmware change lands — analyze.py handles both states.

---

## What this test does NOT do

- It does not install or actuate any SMA wire. SMA addition is Phase 6.
- It does not change the firmware sample rate at runtime. The operator must flash the 400 or 1000 SPS build before running; `--sample-rate` is annotation only.
- It does not pre-tension the spring. Slack region is handled in analyze.py via the F-vs-x knee detection.

---

## Updating the cal references in `config.yaml`

Three values need to stay in sync with the cal modules:

| `config.yaml` key | Source |
|---|---|
| `spring_k_mN_per_mm` | `../Calibrate_LoadCell/config.yaml` (the spring is the same) |
| `laser_k_mV_per_um`, `laser_V0_mV` | `../Calibrate_LaserHead/calibration.json` |
| `load_sensitivity_mV_per_mN`, `load_V0_mV` | `../Calibrate_LoadCell/calibration.json` |
| `laser_noise_V_ref`, `load_noise_V_ref` | static σ from each cal run's baseline rows (see `*_meta.json` → `baseline_noise_V_*`) |

If a value is unknown, set it to `null` — analyze.py will skip the comparison with an INFO note rather than failing.

---

## Phase 6 hooks already present

The plan calls for the slot to grow from 16 → 24 bytes with `hw_us` + `seq`, and for a `[STATUS]` frame from M7. `portenta_reader.py` already parses all of these — when the firmware change lands, the same scripts here will pick up the new fields without modification. `analyze.py`'s pipeline analysis (steps 5 & 6) keys directly on `seq` for gap detection, so the 1 kSPS verification depends on the firmware change being in place.

Author: Yilin Ma — HDR Lab, University of Michigan
