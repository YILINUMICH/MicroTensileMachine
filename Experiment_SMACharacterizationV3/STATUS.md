# Experiment_SMACharacterizationV3 — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP / To-Test** — code-complete, imports + offline analyzer + offscreen GUI construct + headless control-flow verified on synthetic data; **not yet bench-run** against real instruments. |
| **Role** | Multi-instrument SMA characterization **console** + analyzer. One config sets every instrument/sensor parameter; one continuously-logging session records raw LCR + H7 (sensors **and** SMA, src=1–5) + Zaber stage; offline analyzer converts raw→physical and renders dashboards. |
| **Builds on** | `Experiment_SMACharacterizationV2` (architecture), the combined firmware `Firmware_SMASensorHub_PIO` (H7 stream), `Driver_KeysightLCR`, `Driver_ZaberStage`, and the extended `Calibrate_LaserHead/portenta_reader.py`. |
| **Owner** | Yilin |
| **Quick test (no hardware)** | `python -c "import config, workers, recording_core, sma_console, analyze_sma"` then run the analyzer on a synthetic console session (see README). GUI: `QT_QPA_PLATFORM=offscreen` + `run_gui(..., _build_only=True)`. |

## Entry points

- **`sma_console.py`** — the primary entry point. One window controls the
  stage, LCR, and SMA from a continuously-logging session (live plots, startup
  full-system check, mid-run staleness monitor, always-available `DISARM`).
  `--headless` runs the same `RecordingCore` with no GUI for scripted runs.
  Built on `pyqtgraph.Qt` (binding-agnostic: PyQt5/6 or PySide2/6).
- `sma_recorder.py` — the older interactive OPEN→SHORT→RAW recorder (still
  present; `session.py`/`operator_io.py` back it). The console supersedes it.
- `run_experiment.py` — **RETIRED** (stub): it built firmware commands inline
  and never `arm`ed, which the rebuilt firmware rejects. Use `--headless`.

## Console controls (GUI)

- **Manual recording.** On launch the console runs the startup health check and
  shows live plots/readouts, but writes **nothing to disk** until the operator
  clicks **Start REC** (queues are still drained so the buffers never overflow).
  Click again to **Stop REC**. The `--headless` runner auto-starts recording.
  `events.csv` boundaries: `recording start` / `recording stop`.
- **Click-to-reconnect.** The H7 / LCR / stage status dots are buttons — click a
  red (offline/failed) stream to rebuild its worker and retry the hardware
  connection (reuses the same queue). Dots update live each tick.
- **Auxiliary failures are isolated.** An LCR or Zaber worker crash no longer
  trips the shared `stop_event` (which previously cascaded and killed the
  critical H7 stream + whole session). Only the health monitor decides aborts.
- **Stage health.** A connected, streaming Zaber **passes** even when parked
  outside the workflow window `[lo, hi]` (it's telemetry-only) — that's now a
  warning, not a `FAIL`/"offline" verdict.
- **Laser/load voltage-glitch filter (host-side).** The combined firmware emits
  one laser/load sample with `value==0 V` on ~every 32nd ADC1 frame while its
  `raw_code` is a normal non-zero value (the paired load sample is also dropped
  on those frames). It shows up as a huge periodic spike to 0 on the plot.
  `H7Worker` drops these self-inconsistent samples (V=0 with non-zero raw),
  counted in `n_glitch` and logged. Measured impact on a real run: removing the
  108/8888 (1.2%) glitch samples cut laser σ from 162 mV → **0.83 mV**. **The
  underlying firmware voltage-field bug is still open** (raw stream is correct);
  see TODO — needs a bench rebuild to fix at the source.

## Design rules (V3)

- **Recorder logs RAW data only.** It configures instruments at startup but never converts units or pushes calibration to firmware.
- **Calibration coefficients** (`config.calibration`) are recorded in `meta.json` and consumed **only** by `analyze_sma.py`.
- **Any stream can be disabled** via its `enabled:` flag (lcr / h7 / stage).
- **SMA actuation runs on M7**, not the host. With `sma.enabled: true` the recorder sends `cycle …` params + a 1 Hz `ping` heartbeat + `stop`; M7 owns all phase timing (deterministic, host out of the loop) with a watchdog safe-stop. `sma.enabled: false` → pure logger, manual console actuation.

## TODOs

- [ ] **Bench-run the console** (`sma_console.py`) against the real rig: health-check pass/fail, live readouts/plots, `DISARM`, auto-disarm + 1 s warn / 3 s critical-disarm on unplugging the H7, clean shutdown writes `meta.json`. LCR/stage are **auxiliary** (warn-only); H7 is critical.
- [ ] **Bench-run** a full OPEN→SHORT→RAW session against the real rig (LCR + combined-firmware H7 + Zaber).
- [ ] **Fill calibration** in `config.yaml` from `Calibrate_LaserHead` / `Calibrate_LoadCell` fits.
- [ ] **Verify H7 channel rates** — confirm `[STATUS]` shows no drops with all 5 src streaming during a `drive`.
- [ ] **FIRMWARE BUG: laser/load V-field zero-glitch** (`Firmware_SMASensorHub_PIO`) — ~every 32nd ADC1 frame emits `voltage_V==0` despite a valid `raw_code` (and skips the paired ADC2/load sample). Raw codes are correct, so it's in the M4 voltage path / ADC1↔ADC2 interleave, not the ADC read. Currently masked host-side by the `H7Worker` glitch filter; fix at the source on the bench (suspect the `r1.status & 0x80` ADC2-piggyback branch around `main.cpp:1198-1214`).
- [x] ~~**SMA scripted actuation**~~ — done 2026-06-21: recorder drives the on-M7 `cycle` state machine (params + heartbeat) when `sma.enabled`. Bench-verify the heat/cool timing + watchdog.
- [ ] **Scripted STAGE profile** — optional RAW-phase stage motion (still telemetry-only).
- [ ] **Stage motion during RAW** — currently telemetry-only; decide whether the recorder should command the stage.
- [ ] Flip to **Stable** once a real session records cleanly and the analyzer produces a sensible dashboard.

See [../README.md](../README.md) for the project map.
