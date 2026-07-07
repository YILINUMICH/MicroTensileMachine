# Experiment_SMAThermalCharacterization — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP / To-Test** — forked from `Experiment_SMACharacterizationV3`; inherits its verified state (imports + offline analyzer + offscreen GUI + headless flow on synthetic data), **not yet bench-run**. The thermal-specific stream/analysis is **not yet added** — currently identical to V3. |
| **Role** | Multi-instrument SMA **thermal** characterization **console** + analyzer. One config sets every instrument/sensor parameter; one continuously-logging session records raw LCR + H7 (sensors **and** SMA, src=1–5) + Zaber stage; offline analyzer converts raw→physical and renders dashboards. **Planned:** add a temperature stream (thermocouple / IR) to correlate SMA temperature with the Joule-heating drive. |
| **Builds on** | `Experiment_SMACharacterizationV3` (direct fork / architecture), the combined firmware `Firmware_SMASensorHub_PIO` (H7 stream), `Driver_KeysightLCR`, `Driver_ZaberStage`, and the extended `Calibrate_LaserHead/portenta_reader.py`. |
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

- **Adaptive-FPS camera + live preview (2026-07-06).** A `CameraWorker` drives
  the 12MP USB3 camera (index 1) at a **fixed resolution, variable frame rate**:
  fast (`fps_fast`) while the SMA is moving, a slow **heartbeat** (`fps_heartbeat`)
  once settled. "Moving" = net **median-filtered** laser displacement ≥
  `change_threshold_mm` (robust to sensor noise/jumps); after `stop_dwell_s` with
  no full-mm change it drops to heartbeat. Each heat/idle event forces fast for
  `transient_guarantee_s`. Camera runs at native rate (grab-always), decoding
  only frames it keeps — the tail costs almost nothing. **Gated by the same
  Start/Stop REC**; auxiliary/isolated (a camera failure warns, never touches
  H7). **Console controls:** resolution + fast-fps **dropdowns** (fps options
  adapt to resolution; both locked while recording), live **transient** +
  **heartbeat** fields, a **● cam** reconnect dot, and a **live preview** pane.
  Verified end-to-end against the real camera (fast→heartbeat transition,
  per-cycle JPEGs, snapshots, `frames.csv`, preview). **Storage:**
  `<session>/video/{frames.csv, cycle_NN/*.jpg, snapshots/*.jpg}` —
  `frames.csv` (`frame_idx,host_ts,monotonic,cycle,mode,rel_path,laser_mm`) is
  the alignment key against `h7.csv`/`stage.csv`. Config: `camera:` block;
  requires `opencv-python` (import is guarded — absent → camera disabled).
- **No LCR (2026-07-06).** LCR is fully removed from this thermal module — no
  worker/connection, no `lcr.csv`, no LCR UI (status dot, `Ls/Rs` readout, plot
  row, and `ref open`/`ref short` are gone). `build_core` never constructs an
  `LcrWorker`; `config.yaml` keeps only `lcr: {enabled: false}` and the
  `LcrConfig` default is `enabled=False`. The engine's LCR paths remain but are
  inert (queue/worker are `None`). `meta.json` no longer emits an `lcr` block.
- **Stage NEVER moves at launch (2026-07-07, SAFETY).** Homing once drove the
  stage into the fixture and crushed it. Startup now issues **zero** motion:
  `home_on_start`/`move_to_zero_on_start` default **false**, and the worker's
  unconditional `set_velocity()` call was **removed** — `set_velocity` is a
  *continuous-motion* command (`axis.move_velocity`), not a speed setting, so it
  would have started the stage moving (and does so on a stage that retained
  homing). The stage stays exactly where the operator left it; jog it with the
  home/go buttons. Auto-motion is opt-in (`home_on_start: true`, which also
  gates `move_to_zero_on_start`) — use only when the travel is known clear.
- **Idle voltage default 0.5 V + live readout at idle-hold (2026-07-07,
  firmware To-Test).** `sma.v_low` now defaults to **0.5 V** (≈0.12 A,
  non-heating) instead of 0 V, so the coil carries a small rest current whose
  V/I/R is measurable. **Firmware** (`Firmware_SMASensorHub_PIO`, `SMA_IDLE`
  case): while **armed and resting at idle**, it now **streams src=3/4/5 at
  ~10 Hz** (`IDLE_LOG_MS`) — previously telemetry streamed only during a
  drive/cycle, so a bare `arm` showed nothing. Now `arm` simply **holds 0.5 V**
  and the readout populates from the hold itself; the console `on_arm` just
  `arm()` + `set_idle(v_low)` (no `drive`). `measure_baseline` likewise reads
  the idle-hold stream instead of issuing a `drive`. **Needs a firmware
  flash + bench verify** (idle streaming rate, no drops); disarmed still streams
  nothing (no current).
- **Arm-button status colour + click-to-focus preview (2026-07-07).** The SMA
  **arm** button now reflects live state: green **"arm"** when disarmed (safe,
  zero current), amber **"● ARMED"** when the MOSFET is closed — refreshed every
  tick + immediately on click (the red **DISARM** stays the master cutoff). The
  camera **live preview thumbnail is click-to-pop-out**: clicking opens a large
  resizable live view (updated on the same tick) for focusing; closing it
  returns to the thumbnail.
- **Baseline / sensor-zero phase — "measure cold R + zero" (2026-07-07, To-Test).**
  A quiescent companion to the go-to-defined-start behaviour: the operator
  button **"measure baseline (cold R + zero)"** (or `baseline.auto_on_start`)
  calls `RecordingCore.measure_baseline()`, which **arms at a low, non-heating
  probe** (`baseline.probe_v`, ~0.5 V ≈ 0.12 A), issues one `drive` so the
  firmware streams src=3/4/5 for `duration_s`, averages the window, then
  **auto-disarms**. It captures **cold SMA resistance**, the **laser rest
  voltage**, and the **load-cell rest voltage** — the latter written into
  `calibration.load_cell.offset_V` (per-session **tare**) unless the channel is
  saturated. Rationale: disarm (MOSFET-open) is the safe start state but gives
  *zero* current, so R is unmeasurable there; the idle-armed probe is the
  self-cooling middle state where R can be read without heating. **Guards:** the
  load channel is checked for ADC-rail saturation (`|raw|≥2²³`) and % of ±5 V
  range — a saturated/near-rail load cell **blocks the tare** and warns to null
  the LCA-9PC ZERO pot first. Results + `baseline_config` are recorded in
  `meta.json`; `events.csv` gets `baseline start`/`done` markers. Refused while
  recording (it drains the queues). **Not yet bench-run** — the reduction/tare/
  saturation logic is unit-tested on synthetic samples; the arm→drive→stream
  timing needs a real-rig verify.
- **Manual recording.** On launch the console runs the startup health check and
  shows live plots/readouts, but writes **nothing to disk** until the operator
  clicks **Start REC** (queues are still drained so the buffers never overflow).
  Click again to **Stop REC**. The `--headless` runner auto-starts recording.
  `events.csv` boundaries: `recording start` / `recording stop`.
- **Click-to-reconnect.** The H7 / stage status dots are buttons — click a
  red (offline/failed) stream to rebuild its worker and retry the hardware
  connection (reuses the same queue). Dots update live each tick.
- **Auxiliary failures are isolated.** A Zaber worker crash no longer trips the
  shared `stop_event` (which previously cascaded and killed the critical H7
  stream + whole session). Only the health monitor decides aborts.
- **Stage health.** A connected, streaming Zaber **passes** even when parked
  outside the workflow window `[lo, hi]` (it's telemetry-only) — that's now a
  warning, not a `FAIL`/"offline" verdict.
- **Manual stage motion (2026-07-06).** The Stage group now has **home** + **go**
  + **STOP** buttons (and Enter in the `target (mm)` field triggers **go**), plus
  editable **min/max limit fields** with a **set** button. These are
  operator-initiated only — the recording pipeline stays telemetry-only and never
  autonomously commands motion. Motion is routed through the worker that owns the
  serial session (`RecordingCore.stage_home/stage_move/stage_stop/stage_set_limits`
  → `ZaberWorker`), and the driver now serializes every serial transaction with a
  lock, so a move issued while the poll loop reads position no longer gets
  dropped/garbled. **STOP** is an e-stop (halts motion immediately, no homing
  required). The **limit window** clamps go-to moves and also drives the health
  "workflow window"; editing it applies at runtime to both the driver and config.
  Absolute go-to requires a homed stage (click **home** first, since
  `home_on_start` is `false` by default); clamps and not-homed refusals are
  surfaced in the log. **To-Test on the bench.**
- **Input-field normalization (2026-07-06).** Every numeric field self-tidies
  when focus leaves it (and again when its button is clicked): values are parsed,
  clamped to a per-field range, and reformatted to fixed precision — voltages to 2
  dp clamped to `SMA_MAX_V = 5.2 V` (LDO ceiling), stage target/limit fields to 2
  dp clamped to `STAGE_MAX_MM = 300 mm` (travel), time (ms) and cycle count as
  integers. So typing `100` shows `100.00`, and an over-range `6 V` snaps to
  `5.20`. The limits row was also re-spaced (the `max` field no longer overlaps
  the `set` button).
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
- [ ] **Flash + bench-verify idle telemetry streaming** (`Firmware_SMASensorHub_PIO`, `SMA_IDLE` case): after `arm`, confirm src=3/4/5 stream at ~10 Hz while holding 0.5 V idle (readout populates), that disarm stops the stream, and that it doesn't perturb the M4 laser/load ring rates. Requires reflash + power-cycle (EVM rails).
- [ ] **Bench-verify the baseline phase** (`measure_baseline`): confirm the arm→`drive` at `probe_v` streams src=3/4/5 for the window (idle current, no heating), the cold-R / laser-rest / load-rest means are sane, auto-disarm fires, and the load-saturation guard trips when the LCA-9PC ZERO pot is deliberately off. Then decide whether `baseline.auto_on_start` should default true.
- [ ] **Verify H7 channel rates** — confirm `[STATUS]` shows no drops with all 5 src streaming during a `drive`.
- [ ] **FIRMWARE BUG: laser/load V-field zero-glitch** (`Firmware_SMASensorHub_PIO`) — ~every 32nd ADC1 frame emits `voltage_V==0` despite a valid `raw_code` (and skips the paired ADC2/load sample). Raw codes are correct, so it's in the M4 voltage path / ADC1↔ADC2 interleave, not the ADC read. Currently masked host-side by the `H7Worker` glitch filter; fix at the source on the bench (suspect the `r1.status & 0x80` ADC2-piggyback branch around `main.cpp:1198-1214`).
- [x] ~~**SMA scripted actuation**~~ — done 2026-06-21: recorder drives the on-M7 `cycle` state machine (params + heartbeat) when `sma.enabled`. Bench-verify the heat/cool timing + watchdog.
- [ ] **Bench-verify manual stage motion** — home + go buttons issue reliable
  commands now that the driver serializes serial I/O (`_serial_lock`); confirm on
  the rig that a go-to lands where expected and no commands are dropped.
- [ ] **Confirm stage home direction** — see `Driver_ZaberStage/diag_home.py`;
  the console home appeared to be on the opposite end vs Zaber Launcher.
- [ ] **Scripted STAGE profile** — optional RAW-phase stage motion (recorder-driven, still deferred; manual motion is now available).
- [ ] Flip to **Stable** once a real session records cleanly and the analyzer produces a sensible dashboard.

See [../README.md](../README.md) for the project map.
