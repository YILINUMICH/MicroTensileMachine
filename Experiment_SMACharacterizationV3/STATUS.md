# Experiment_SMACharacterizationV3 — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP / To-Test** — code-complete, imports + offline analyzer verified on synthetic data; **not yet bench-run** against real instruments. |
| **Role** | Multi-instrument SMA characterization recorder + analyzer. One config sets every instrument/sensor parameter; records raw LCR + H7 (sensors **and** SMA, src=1–5) + Zaber stage; offline analyzer converts raw→physical and renders dashboards. |
| **Builds on** | `Experiment_SMACharacterizationV2` (architecture), the combined firmware `Firmware_SMASensorHub_PIO` (H7 stream), `Driver_KeysightLCR`, `Driver_ZaberStage`, and the extended `Calibrate_LaserHead/portenta_reader.py`. |
| **Owner** | Yilin |
| **Quick test (no hardware)** | `python -c "import config, workers, session, sma_recorder, analyze_sma"` then run the analyzer on a synthetic session (see README). |

## Design rules (V3)

- **Recorder logs RAW data only.** It configures instruments at startup but never converts units or pushes calibration to firmware.
- **Calibration coefficients** (`config.calibration`) are recorded in `meta.json` and consumed **only** by `analyze_sma.py`.
- **Any stream can be disabled** via its `enabled:` flag (lcr / h7 / stage).
- **SMA actuation runs on M7**, not the host. With `sma.enabled: true` the recorder sends `cycle …` params + a 1 Hz `ping` heartbeat + `stop`; M7 owns all phase timing (deterministic, host out of the loop) with a watchdog safe-stop. `sma.enabled: false` → pure logger, manual console actuation.

## TODOs

- [ ] **Bench-run** a full OPEN→SHORT→RAW session against the real rig (LCR + combined-firmware H7 + Zaber).
- [ ] **Fill calibration** in `config.yaml` from `Calibrate_LaserHead` / `Calibrate_LoadCell` fits.
- [ ] **Verify H7 channel rates** — confirm `[STATUS]` shows no drops with all 5 src streaming during a `drive`.
- [x] ~~**SMA scripted actuation**~~ — done 2026-06-21: recorder drives the on-M7 `cycle` state machine (params + heartbeat) when `sma.enabled`. Bench-verify the heat/cool timing + watchdog.
- [ ] **Scripted STAGE profile** — optional RAW-phase stage motion (still telemetry-only).
- [ ] **Stage motion during RAW** — currently telemetry-only; decide whether the recorder should command the stage.
- [ ] Flip to **Stable** once a real session records cleanly and the analyzer produces a sensible dashboard.

See [../README.md](../README.md) for the project map.
