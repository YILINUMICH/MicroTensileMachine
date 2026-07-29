# Experiment_SMAThermalCharacterization — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP / To-Test** — forked from `Experiment_SMACharacterizationV3`; inherits its verified state (imports + offline analyzer + offscreen GUI + headless flow on synthetic data), **not yet bench-run**. The thermal-specific stream/analysis is **not yet added** — currently identical to V3. |
| **Role** | Multi-instrument SMA **thermal** characterization **console** + analyzer. One config sets every instrument/sensor parameter; one continuously-logging session records raw LCR + H7 (sensors **and** SMA, src=1–5) + Zaber stage; offline analyzer converts raw→physical and renders dashboards. **Planned:** add a temperature stream (thermocouple / IR) to correlate SMA temperature with the Joule-heating drive. |
| **Builds on** | `Experiment_SMACharacterizationV3` (direct fork / architecture), the combined firmware `Firmware_SMASensorHub_PIO` (H7 stream), `Driver_KeysightLCR`, `Driver_ZaberStage`, and the extended `Calibrate_LaserHead/portenta_reader.py`. |
| **Owner** | Yilin |
| **Quick test (no hardware)** | `python -c "import config, workers, recording_core, sma_console, analyze_sma"` then run the analyzer on a synthetic console session (see README). GUI: `QT_QPA_PLATFORM=offscreen` + `run_gui(..., _build_only=True)`. |

## Entry points

- **`operator_console.py`** — the primary entry point. One window controls the
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
- **"Can't reach H7" was a TRANSPORT MISMATCH, not a dead board (2026-07-28).**
  `console_20260728_112825`: `health H7 FAIL (0)`, empty `h7.csv`, `UDP reader:
  recv=0 samples=0 lost=0`, then **425** consecutive
  `H7 command 'disarm' failed: WriteFile failed (PermissionError(13, 'The device
  does not recognize the command.', 22))`. The H7 was streaming the whole time —
  reading COM8 directly gave **987 lines/s** of src=1 (laser, 2.502 V) / src=2
  (load). `config.yaml` had `transport: udp` while the board runs the plain
  **`portenta_m7`** image, which streams over USB — the trap already documented
  in the config. Set back to **`transport: usb`**.
  **Two things this taught us beyond the trap comment:**
  (1) The failure is *worse than logging nothing*. In `udp` the console holds
  COM8 open but never **drains** it, so the M7 blocks in `Serial.write` and
  **wedges**: no boot banner, then every command write fails
  `ERROR_BAD_COMMAND`. Reproduced deliberately (hold the port open unread ~30 s)
  — the board then emits zero bytes and answers nothing; DTR toggle, RTS+DTR,
  and a serial break all fail to recover it. **Only a power cycle (USB + EVM)
  brings it back.**
  (2) UDP could not have worked even with the right firmware: `pc_ip:
  169.254.245.100` matched **no interface on this host** (Ethernet was campus
  DHCP `141.212.82.60`; the only 169.254.x addresses were on disconnected
  Wi-Fi/Bluetooth adapters). Before going back to `udp`, verify BOTH the flashed
  env AND that a linked NIC actually holds `pc_ip`.
- **Camera wouldn't start — a STRANDED subprocess owned it (2026-07-28, FIXED).**
  Symptom: `open failed: no capturable camera found (probed indices [0,1,2,3])`
  on every launch; one session (`console_20260728_110133`) silently recorded
  `cam[1] 640x480` — the **built-in webcam**, not the 12MP. Cause was a chain of
  three defects, all now fixed:
  1. **`reconnect_timeout_s` was 2.0 s, shorter than the camera's own
     open+first-frame latency (~4.5–5.4 s measured).** The watchdog fired before
     the stream ever started, and each reopen cost another 4.5 s — a
     self-sustaining reopen loop that never yields a frame. Now **6.0 s**
     (`lib_config.CameraConfig`, explicit in `config.yaml`).
  2. **The orphaned child could never exit.** `_camera_proc_main` waited on an
     `mp.Event` only the (dead) parent could set, so a console crash / force-close
     left it looping forever, holding the camera against every later run —
     Windows' consent store showed `python.exe … LastUsedTimeStop = 0` for 20 min.
     Now a **parent-liveness watchdog** (`mp.parent_process().is_alive()`, polled
     1 Hz) self-exits. **Note `os.getppid()` is useless here** — on Windows it
     keeps returning the dead pid. Second half of the same bug: the child then
     **hung in interpreter shutdown**, because an `mp.Queue`'s feeder thread is
     joined by an exit finalizer and nobody drains `out_q` once the parent dies
     (0% CPU, invisible, still holding the camera). Fixed with
     `cancel_join_thread()` + `close()` on the child side — the mirror of what
     `CameraProcessProxy.join()` already did on the parent side — plus an
     `os._exit(0)` backstop on the orphan path only.
  3. **`pygrabber` was imported but never declared**, so name-pinning to
     `"12MP U3 Camera"` silently no-opped and resolution always fell back to the
     capability probe — which would happily return a webcam. Added to
     `requirements.txt` (installed), and `_resolve_camera` now **raises** when the
     widest camera found is under `_BIG_SENSOR_MIN_WIDTH` instead of accepting it.
     Bypass with `camera.auto_detect: false`.
  Verified on the rig: name-pinned to index 0 in 0.08 s (was a 4.5 s probe),
  `cam[0] 1920x1080 MJPG`, clean `join()` in 0.5 s (exit 0), and a **force-killed**
  parent now leaves the child dead in **1.1 s** with the camera released.
  **Operator note:** DSHOW also enumerates an **OBS Virtual Camera** on this host,
  which shifts positional indices — one more reason index alone is not trustworthy.
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
- **Calibration coefficients** (`config.calibration`) are recorded in `meta.json` and consumed **only** by the offline analysis (`lib_analysis` / `operator_explore.ipynb`).
- **Any stream can be disabled** via its `enabled:` flag (lcr / h7 / stage).
- **SMA actuation runs on M7**, not the host. With `sma.enabled: true` the recorder sends `cycle …` params + a 1 Hz `ping` heartbeat + `stop`; M7 owns all phase timing (deterministic, host out of the loop) with a watchdog safe-stop. `sma.enabled: false` → pure logger, manual console actuation.

## TODOs

> ### ▶ OPEN ISSUE — SWEEP COOL PHASE IS MULTIMODAL, CAUSE UNKNOWN (2026-07-28)
>
> **Symptom.** In `data/sweep_20260728_215606` the cool-phase current sense reads
> 71 mA sd, while four steady captures (`data/isense_20260728_2*`) read 12–16 mA
> under every condition tried. The sweep distribution is **not Gaussian** —
> discrete modes near 65/100/135/270/400 mA, 22% of samples >220 mA, skew +1.37,
> kurtosis 4.50. Its clean sub-population is 28 mA sd. See `fig5_spikes.png`.
>
> **Why it matters.** This is the condition in which the CC loop actually fails.
> The `near` gate is ±12 mA and the `R_est` bootstrap latches `u/I` from ONE
> sample, so a contaminated stream is what strands `R_est` at 6.25 Ω against a
> true 4.2 and drives the ≤50% overshoot.
>
> **REFUTED — do not re-run** (each killed by measurement, detail in the sweep
> README): per-tick I²C DAC write (1.05×); DAC command jitter (the quiet capture
> has *more*); heat-pulse aftermath (flat across 12 s); operating point (`cc 155`
> is clean); load current (disconnected is equally quiet); a different code path
> in cool (`ccEngage`/`serviceActuationPhase` are identical); A0→A1 mux leakage
> (272 vs 157 mA, uncorrelated).
>
> **Only untested difference.** The sweep's cool target (100 mA) is below the
> reachable floor (~120 mA), so the loop rails and the gate stays shut; every
> clean capture had it open. Next step is one capture:
> `python operator_noise_isense.py --port COM8 --mode cc --ma 100`.
>
> **Corrected number.** An earlier claim of 144 mA/read (and "averaging cannot
> fix this") came from this contaminated phase and was wrong. The real front end
> is ~31 mA/read — 12× the Uno, not 57×. At that level `ADC_SAMPLES_CYCLE=64`
> gives 3.9 mA at 383 Hz, 3σ inside the gate and 2× the Uno's rate, so averaging
> IS viable. `operator_sweep_adcavg.py` measures the curve.

> ### ▶ JUDGE ACTUATION ON DISPLACEMENT, NOT FORCE (2026-07-28)
>
> The fixture is compliant, so a contracting coil mostly **moves**. Per pulse,
> force changes 1–3 mN (inside the noise) while displacement changes 5–530 µm and
> is cleanly monotonic in current. The load cell **never saturates** — 380 mN
> peak against a 490 mN rating even at 928 mA, so "max current before the load
> cell saturates" is not the binding constraint for the RNN upper bound.
> `operator_current_sweep.py` still judges on src=2 and must be moved to src=1.
> Above ~750 mA the force baseline ratchets *down* across a run (180 → 380 → 232
> mN), which is not repeatable cycling — cap at 550 mA until that is understood.

> ### ▶ CC BOOTSTRAP + REACHABILITY (firmware — ASK BEFORE EDITING) (2026-07-28)
>
> In `Firmware_SMAConstantCurrent_PIO`, unchanged pending approval:
> 1. `R_est` bootstraps from a **single** ADC sample on a railed point taken mid
>    rise (`0.5 V / 0.08 A = 6.25 Ω`). Latch from a *settled* railed point.
> 2. `cccycle` emits **no** reachability warning: it lives only in the `cc <mA>`
>    path and is gated on `cc_R_valid`, which `startCycleCC()` clears via
>    `ccReset()` on the line before — structurally dead for every cycle run.
> 3. Runtime-only workaround, untested: `ccgain 25`. `cc_Kp` defaults to 0, so
>    there is no proportional term at all; with `Kp > 0` the P term pulls the
>    error inside the gate, which lets `R_est` self-correct. Survives `ccReset`.

> ### ▶ OPEN ISSUE — CYCLE TIMING DISTORTED BY USB-CDC BACK-PRESSURE (2026-07-15)
>
> **Symptom.** In firmware-timed `cycle` runs the cool phase overshoots: the
> first ~2 cools are correct (~3.1 s) then cools stretch to 5–8 s. Also seen:
> "fire N early", force plot shows vertical lines, sensor rate drops. Sessions:
> `console_20260715_150641`, `console_20260715_160458`.
>
> **Root cause (confirmed from data).** The M7 runs a *cooperative* state machine
> — `serviceSma()` checks the cool timer (`t_rel >= cyc_cool_ms`) in the **same
> super-loop** that does the **blocking** `Serial.write` stream. When the host PC
> falls behind reading the serial (the camera/GUI starving the H7 reader thread),
> the M7's write blocks; `millis()` keeps running, so the cool-timer check is
> serviced late → cool overshoots. Measured: **8 M7 stalls of 4–5 s each**
> (firmware clock jumps with zero samples produced). During a stall the M4→M7
> sensor **ring overflows → lost laser/load samples** (92 Hz vs 400), and the
> backlog arrives in a **burst** (→ compressed host timestamps → vertical lines).
> The `ping` heartbeat stays regular because that's the *opposite* USB direction.
>
> **Two distinct problems:** (a) REAL actuation distortion (the wire genuinely
> cooled 5–8 s) + REAL sensor-sample loss (ring overflow); (b) MEASUREMENT
> smearing (host timestamps logged late). Confirm with firmware clock: fire
> intervals on `hw_us` read 3.22 s (cycles 2–3, correct) then 5.7–7.8 s (real).
>
> **Tried / done (host-side, no dropped samples):**
> - `hw_us` time base in `lib_analysis` (`timebase()`): analysis reads the
>   firmware clock, not the bursty host clock → fixes (b) only. (Measurement.)
> - `portenta_reader.py`: `write_timeout=0.5 s` (a stalled write can't freeze the
>   UI) + `set_buffer_size(rx=4 MB)` (bridge ~30 s of host stall so the M7 never
>   back-pressures — targets (a) WITHOUT dropping samples).
> - Single serial owner: SMA commands enqueued to the H7 reader thread, never
>   written from the GUI thread (no read/write race on one COM handle).
> - H7 reader thread self-pins to a dedicated core + `above_normal` priority.
> - Camera: loop pacing (grab() busy-spin fix), decoupled cheap preview
>   (`preview_hz`/`preview_width`), and OPT-IN `camera.use_subprocess: true`
>   (camera in its OWN process → its GIL/core can't stall the H7 reader).
>
> **Believed cause of the ~4–5 s reader starvation:** the in-thread camera's
> fast-capture window (≈ `stop_dwell_s`) holding the GIL / saturating a core.
>
> **FAILED (2026-07-15, reverted):** firmware non-blocking write gated on
> `Serial.availableForWrite()` **dropped ALL data** — on the Portenta mbed
> USB-CDC that call returns ≈0 almost always (immediate endpoint capacity, not a
> buffered free-space count), so `nbWrite` dropped nearly every sample
> (`tx_drop` climbed at the full sample rate; host got only `[STATUS]`). **Do NOT
> gate firmware writes on `availableForWrite()`.** A real firmware non-blocking
> write needs a SOFTWARE TX ring buffer (or mbed `send_nb()`), a bigger change.
>
> **NEXT STEP (in order):**
> 1. **Bench-test the host-side fix with the CONSOLE** (not `pio device
>    monitor` — it has no big buffer): `camera.use_subprocess: true` + the 4 MB
>    RX buffer + reader priority, on the WORKING (blocking) firmware. If
>    `usbser.sys` keeps filling the 4 MB buffer while the app is busy, the M7's
>    blocking write returns fast → no stall, **no firmware change, no drops**.
>    Verify: cool ~3.1 s on the `hw_us` timeline, ~400 Hz sensor rate, no
>    firmware-clock gaps.
> 2. If stalls persist → **firmware SOFTWARE TX RING** (NOT `availableForWrite`).
> 3. Long-term ideal: **UDP over the H7 Ethernet** — fire-and-forget, the control
>    loop structurally cannot block. Biggest lift; only if 1–2 are insufficient.
>
> Note: this is SEPARATE from the "`cool_ms` too short vs τ_F≈6 s" thermal TODO
> below — that's about physics (independence of cycles); this is about the
> firmware not holding the *commanded* 3 s in the first place.

> ### ▶ NEXT SESSION — START HERE (2026-07-13 EOD)
>
> The 1 kHz SMA config is **ported into `Firmware_SMASensorHub_PIO` and builds
> clean, but has never been on the rig.** Everything else below is unchanged.
>
> ```
> cd ../Firmware_SMASensorHub_PIO
> pio run -e portenta_m7 -t upload     # then POWER-CYCLE USB + EVM (else ADS1263 = ID 0x00)
> ```
> Then walk the **4 gates in `Firmware_SMASensorHub_PIO/STATUS.md`, in order,
> stopping at the first failure**: (1) cadence — ~96 src=3/4/5 points inside a
> 100 ms fire, was 8; (2) `dropped`/`crc_err` still **0**; (3) **the V and I
> means, NOT R** — R is immune to the exact bug this gate looks for, and expect
> V/I to land **~5% LOWER** than old sessions (that's the duty error going away,
> not a regression); (4) idle telemetry still ~10 Hz.
>
> Rollback if it misbehaves: `pio run -e portenta_m7_legacy100 -t upload`.
>
> **Loose end:** this repo tracks `.pio/build/` in git with no `.gitignore`, and
> the rebuild pruned 179 stale object files (they show as deleted; nothing is
> committed). Decide: `git rm -r --cached */.pio && echo ".pio/" >> .gitignore`
> (recommended — they're regenerable and now stale), or `git checkout -- */.pio`
> to put them back.

- [ ] **Bench-run the console** (`operator_console.py`) against the real rig: health-check pass/fail, live readouts/plots, `DISARM`, auto-disarm + 1 s warn / 3 s critical-disarm on unplugging the H7, clean shutdown writes `meta.json`. LCR/stage are **auxiliary** (warn-only); H7 is critical.
- [ ] **Bench-run** a full OPEN→SHORT→RAW session against the real rig (LCR + combined-firmware H7 + Zaber).
- [ ] **Fill calibration** in `config.yaml` from `Calibrate_LaserHead` / `Calibrate_LoadCell` fits.
- [ ] **Flash + bench-verify idle telemetry streaming** (`Firmware_SMASensorHub_PIO`, `SMA_IDLE` case): after `arm`, confirm src=3/4/5 stream at ~10 Hz while holding 0.5 V idle (readout populates), that disarm stops the stream, and that it doesn't perturb the M4 laser/load ring rates. Requires reflash + power-cycle (EVM rails).
- [ ] **Bench-verify the baseline phase** (`measure_baseline`): confirm the arm→`drive` at `probe_v` streams src=3/4/5 for the window (idle current, no heating), the cold-R / laser-rest / load-rest means are sane, auto-disarm fires, and the load-saturation guard trips when the LCA-9PC ZERO pot is deliberately off. Then decide whether `baseline.auto_on_start` should default true.
- [ ] **Verify H7 channel rates** — confirm `[STATUS]` shows no drops with all 5 src streaming during a `drive`.
- [ ] **FIRMWARE BUG: laser/load V-field zero-glitch** (`Firmware_SMASensorHub_PIO`) — ~every 32nd ADC1 frame emits `voltage_V==0` despite a valid `raw_code` (and skips the paired ADC2/load sample). Raw codes are correct, so it's in the M4 voltage path / ADC1↔ADC2 interleave, not the ADC read. Currently masked host-side by the `H7Worker` glitch filter; fix at the source on the bench (suspect the `r1.status & 0x80` ADC2-piggyback branch around `main.cpp:1198-1214`).
- [ ] **LASER 65.8 Hz TONE — ACCEPTED, not fatal; do not chase unless it changes** (found 2026-07-13; reference sample `data/console_20260713_122906` = laser on an **immovable block**; diagnose with the laser view (`lib_analysis`; notebook port TODO); full write-up in README). The laser's apparent ±1.4 µm "noise" is a coherent **65.77 Hz / 1.72 µm** ripple carrying **74%** of the channel's variance. **It is instrumental, not mechanical:** it survives an immovable target, and it sits at the *same* 65.77 Hz (to 0.008%) in the actuation session `console_20260713_115921` despite a completely different mass/stiffness — a real resonance would have shifted. Load/ADC2 is clean at that frequency. **Why we accept it:** 66 Hz is an order of magnitude above our DC–few-Hz signal band and is stationary, so averaging over a fire (6.6 cycles) or a cool (197 cycles) suppresses it, and a notch recovers σ 1.29 → 0.31 µm in post. It costs raw plot resolution, not correctness. **If it bites us:** (1) feed ADC1 a DC voltage with the IL-030 disconnected — tone survives ⇒ ADC/wiring, tone vanishes ⇒ IL-030; (2) resolve the alias (could really be 335/466 Hz) — but that needs the read-path fix below, not just a rate constant; (3) re-open immediately if the tone ever drifts, grows, or gains a low-frequency sibling.
- [x] ~~**SMA resistance transition unresolvable**~~ — **WRONG, corrected 2026-07-13.** The transition IS resolved: **ΔR/R₀ = −3.13% ± 0.54% during the fire (t = −5.8)**, recovering to baseline by the end of the 3 s cool. It only looked unresolvable because the metric took `max()` over the fire window, which on a ±6% single-sample noise floor returns the largest *noise* excursion — always positive — and hid a real effect that is *negative*. The right estimator is the window **mean**, averaged across cycles (`lib_analysis`; transition-view notebook port TODO). The `cycles` view now reports `dR_fire_pct` (mean), not `dR_peak_pct` (max).
- [ ] **`cool_ms` is far too short.** The force cooling fit gives **τ_F ≳ 6 s** against a `cool_ms` of only **3 s**, so the coil never returns to baseline before the next fire — this is the cause of the ratcheting force baseline across the run, and it means the 10 cycles are **not independent**. Raise `cool_ms` to ≥ 3–5 × τ (~20–30 s) for clean cycles, or accept and model the accumulation. τ itself is only a **lower bound** until the cool window exceeds it.
- [ ] **SMA `sma_v`/`sma_i` read +7% HIGH; power/energy ~15% high** (found 2026-07-13 on the bench, `Firmware_SMARateTest_PIO` runs 0-7). The H7's on-chip ADC reads high **in proportion to its conversion duty** — `V = 0.01508 × duty% + 2.988`, R² = 0.9996 across 8 runs with the DAC code held fixed. Production (`CYCLE_LOG_MS=10`, `ADC_SAMPLES=64` → 14% duty) therefore inflates V and I by ~7%, and **power by ~15%** (P = V·I squares it). **What this does and does not affect:** `sma_r` is **EXACTLY immune** (both channels scale together, R = V/I cancels — R sat at 21.4 Ω while V drifted +33%), so **every resistance result stands**; laser/load are unaffected (ADS1263 + external REF7050). Only the **absolute** power/energy numbers on the dashboard move (2.18 W → ~1.9 W; 6.5 J → ~5.6 J) — the **fire-vs-idle ratio is unchanged**, so "the idle probe delivers more heat than all ten fires" still holds. **Partly fixed 2026-07-13:** the `portenta_m7_rate1k_n4` port (above) drops the in-cycle duty 14% → 12%, which removes most of it — pending the bench run that confirms V/I come back down. **Still open:** `ADC_VREF_V = 3.145` is itself ~5% off (true ≈ 2.99 V), a *standing* mis-calibration independent of duty. Fix it by reading the STM32's internal **VREFINT** and self-correcting, rather than trusting a hard-coded constant — that would also confirm the droop mechanism outright (it is currently inferred from behaviour; Vref was never measured directly). **All existing sessions' V/I/power are affected; all R results are not.**
- [x] ~~**Port the 1 kHz SMA config to production.**~~ — **PORTED 2026-07-13 into `Firmware_SMASensorHub_PIO` (builds clean; NOT yet flashed/bench-run).** `portenta_m7_rate1k_n4` carried over verbatim: `CYCLE_LOG_MS=1`, `ADC_SAMPLES_CYCLE=4` (idle/manual stay at 64), `SMA_SETTLE_US=50` (was `delay(1)`), batched SMA emit, M7 timestamps, plus `loop_hz` in `[STATUS]`. Key idea: each answer is built from 4 ADC readings instead of 64, so a single answer is noisier — but you get 10× more of them and average on the PC, where averaging is free and doesn't drain the ADC's reference. This is what makes each per-cycle ΔR error bar shrink instead of relying on the 10-cycle ensemble. **Host needs no change** — line format is byte-identical (verified against `portenta_reader.parse_line`), and the console drains the whole queue each 50 ms tick. **Next: flash + power-cycle + run the 4 gates in `Firmware_SMASensorHub_PIO/STATUS.md`** (cadence → no drops → V/I means → idle telemetry). Rollback if needed: `pio run -e portenta_m7_legacy100 -t upload`.
- [ ] **Bench-run the 1 kHz stream** and confirm the payoff: ~96 src=3/4/5 points inside each 100 ms fire (was 8), `dropped`/`crc_err` still 0, and V/I means ~5% **lower** than the old sessions (the duty error going away — see the `+7%` item below). Then re-run the transition analysis (`lib_analysis`): with ~96 points per fire the per-cycle ΔR error bar should shrink enough to see the transition **within a single cycle**, instead of only across the 10-cycle ensemble.
- [ ] **Improve SMA current-sense precision** (this, not sampling rate, is what limits R). σ(I)/I = 5.66% contributes **91%** of the 6.3% noise on R; corr(ΔV, ΔI) = +0.02 proves it is *measurement* noise, not real drive fluctuation. σ(I) = 138 ADC LSB **after 64× averaging** — so the interferer is low-frequency and the back-to-back averaging loop in `readADC()` (`main.cpp:116-124`) samples it at the same phase 64× and cancels nothing. Options, cheapest first: (a) **spread** the 64 reads over a window (one 60 Hz period nulls hum, but costs cadence — `readSma()` is 2× `readADC()`); (b) **raise the current-sense scale** — now `INA_GAIN 10 × 0.1 Ω = 1.0 V/A`, so the fire peak reaches only **23%** of ADC range and the idle probe just **3.7%**; (c) **move V/I onto the ADS1263** (AIN6–AIN9 are free) — the structural fix. **NOTE: the rate is NOT the problem** — and as of the 2026-07-13 1 kHz port it is even less so (~962 Hz, ~96 points/fire, was 88 Hz / ~8). Rate buys transient **shape**, not resistance **precision**: every one of those 96 points still carries the same ~6% noise, and it comes from the current-sense front end. Option (a) is *unaffected* by the port (it spreads the reads in time rather than adding them, so it does not raise ADC duty); note (b) and (c) remain the real levers.
- [ ] **The laser may not be measuring anything** (higher priority than the tone). In `console_20260713_115921` the real SMA displacement sits **below the laser noise floor** — only the *force* clearly responds to firing, and the only laser excursion is the drive feedthrough (see below). Establish whether the IL-030 is resolving real SMA contraction at all before investing in noise cleanup.
- [ ] **LASER DRIVE FEEDTHROUGH during a fire — the one that can produce a WRONG RESULT.** The laser steps **+3.2…+3.6 µm inside every fire window** and returns to 0 ± 0.3 µm by +0.25 s, exactly when the force peaks; on a cycle where the force was 8× larger the step was unchanged. It tracks the 0.7 A drive, not the mechanics. Unlike the 65.8 Hz tone this is **synchronous with the actuation**, so **no frequency filter can remove it** — it lands precisely where the displacement signal should be and can be mistaken for SMA contraction. the cycles view flags it (`lib_analysis`; notebook port TODO). Fix at the source (shielding / grounding / routing of the laser signal away from the drive loop).
- [ ] **H7 over-read: ~19% of `h7.csv` rows are zero-order-hold duplicates** (found 2026-07-13). The stream is read at **492.85 Hz** while the ADS1263 converts at **400 SPS**, so ~1 row in 5 on **both** laser and load is an exact repeat of the previous conversion (unique-sample rate = 400.7 Hz, matching the config). Effective rate is **400 Hz, Nyquist 200 Hz** — row-count-derived rates/σ/spectra are off by ~23%. Fix at the source (read on DRDY / drop the repeat) or decimate host-side on `diff(raw_code)==0`. Related: analysis must use **`hw_us`, not `host_timestamp_s`** — host timestamps are USB-batched (median dt 1.06 ms, σ 3.4 ms) and smear any spectrum enough to hide the tone entirely.
- [x] ~~**SMA scripted actuation**~~ — done 2026-06-21: recorder drives the on-M7 `cycle` state machine (params + heartbeat) when `sma.enabled`. Bench-verify the heat/cool timing + watchdog.
- [ ] **Bench-verify manual stage motion** — home + go buttons issue reliable
  commands now that the driver serializes serial I/O (`_serial_lock`); confirm on
  the rig that a go-to lands where expected and no commands are dropped.
- [ ] **Confirm stage home direction** — see `Driver_ZaberStage/diag_home.py`;
  the console home appeared to be on the opposite end vs Zaber Launcher.
- [ ] **Scripted STAGE profile** — optional RAW-phase stage motion (recorder-driven, still deferred; manual motion is now available).
- [ ] Flip to **Stable** once a real session records cleanly and the analyzer produces a sensible dashboard.

See [../README.md](../README.md) for the project map.
