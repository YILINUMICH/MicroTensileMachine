# SMA Console GUI — implementation plan

Handoff plan for building a single interactive GUI console that replaces the
multi-entry-point recorder. Written for an implementer with full repo +
compile + bench access. **Review this before coding.**

---

## 0. Status — already done (do NOT redo)

These changes are already in the tree from the review session. They were
edited via tools whose sandbox mount is unreliable for this repo, so the
**first action for any of them is `python -m py_compile <file>`** (and
`pio run` for firmware) to confirm they parse.

### Firmware — `Firmware_SMASensorHub_PIO/` (status: To-Test, needs `pio run` + bench)
- SMA control rebuilt into **one arm-gated HEAT/COOL engine**. States reduced
  to `SMA_ACT_HEAT`/`SMA_ACT_COOL` (+ characterization states); `drive`/`fire`/
  `cycle` are presets of it. Removed `SMA_DRIVING`/`SMA_FIRE_*`/`SMA_CYCLE_*`.
- MOSFET = **arm/disarm** (master enable). New commands: `arm`, `disarm`,
  `idle <V>`. `drive`/`fire`/`cycle` require `arm` first. `fire <v_high> [ms]`
  now takes **volts** (was DAC codes). `cycle <v_high> <v_idle> <t_high_ms>
  <t_idle_ms> <n>`. `mosfet on|off` kept as arm/disarm alias.
- All hardware writes go through `arm()`/`disarm()`/`setLevel()`; the only
  teardown is idle-low (still armed) or `disarm` (hard cutoff).
- Heat watchdog (`wdt`) generalized: host silent > `wdt_ms` **while heating**
  → drop to idle-low (still armed, relaunchable). COOL is unguarded.
- `[STATUS]` now also emits `crc_err`, `overrun`, `m7_us`, `m4_us`, `vdd`,
  `offset`, `aref`. `src=3/4/5` (SMA V/I/R) are stamped with **M4's live
  clock** so all stream lines share one timeline.
- ADS1263 driver (all 4 live copies): `POWER 0x13 → 0x02`; checksum-mismatch
  log throttled to 1 Hz; stale channel-map comment fixed.

### Host — `Experiment_SMACharacterizationV3/`
- `h7_commands.py` (NEW) — builders for the firmware command set
  (`arm/disarm/idle/cycle/fire/drive/ping/stop/wdt`). **Single source of
  truth**; everything host-side should build commands through it.
- `session.py` — sends `arm` before `cycle`, `stop`+`disarm` after; **startup
  full system check** (identity + streaming + sane values via `_assess_lcr/
  _assess_h7/_assess_stage`); **mid-run staleness monitor**
  (`_check_stream_health`: 1 s warn, 3 s → `disarm` + end phase + finalize,
  `_RESULT_ABORT_HEALTH`); writes `<phase>_status.csv`.
- `workers.py` — `H7Worker` reads via `reader.iter_events()` and routes
  `[STATUS]` → `StatusSample` → `status_queue` (new, optional).
- `sma_recorder.py` — creates + wires `status_q`.
- `config.py` — `SmaConfig.v_low` default `0.0 → 0.5` (the idle/cool level);
  `cycle_command()` delegates to `h7_commands`.
- `Calibrate_LaserHead/portenta_reader.py` (canonical reader) — added
  `iter_events()` (yields `("sample", Sample)` / `("status", dict)`);
  `parse_status_line()` now float-aware (vdd/offset/aref).
- `Experiment_SMACharacterizationV2/` — **deprecated** (README banner).

---

## 1. Goal

One interactive **GUI console** (PySide6 + pyqtgraph, **layout A**: left
control rail + right stacked plots + top status bar + bottom event log) that:

- controls the **Zaber stage, the Keysight LCR, and the SMA** from one window;
- **continuously logs** every enabled stream to CSV;
- shows **live health** (plots + connection/health indicators);
- **auto-disarms** the MOSFET between actuations;
- treats fire/cycle as the **same primitive** (`n` is the knob; `fire` = `n=1`);
- keeps LCR **de-embed references** (OPEN/SHORT) as on-demand commands;
- **replaces** `sma_recorder.py` + `run_experiment.py` + the rigid
  OPEN→SHORT→RAW phase flow, eliminating the multi-entry-point problem.

Non-goal: closed-loop control (still M7/host open-loop; the firmware owns
actuation timing).

---

## 2. Target architecture

```
sma_console.py  (entry; PySide6 QMainWindow + QTimer)
   │  builds + owns
   ├── config.AppConfig                  (unchanged)
   ├── workers.{Lcr,H7,Zaber}Worker      (unchanged; push to queues + status_queue)
   ├── h7_commands                       (unchanged; all SMA command strings)
   └── recording_core.RecordingCore      (NEW — extracted from session.py)
          • drain queues → CSV writers
          • events.csv (command + ref markers)
          • SMA control (arm/cycle/fire/stop/disarm via h7_commands)
          • startup full system check
          • continuous staleness monitor
          • meta.json
```

Threading: worker threads push to `queue.Queue`s; the Qt **main thread** runs
a `QTimer` (~20 Hz) that drains the queues and updates widgets/plots/CSV.
**No Qt widget access from worker threads** (the queue hand-off enforces this).

---

## 3. Work items

### WI-1 — Extract `recording_core.py` from `session.py`
- [ ] Pull the UI-agnostic logic out of `SessionController` into a
      `RecordingCore` class usable by both a GUI and a headless caller:
  - queue drains + CSV writers (`_drain_*_to`, `_drain_status_to`),
  - SMA control (`_sma_send`, arm/cycle/fire/stop/disarm via `h7_commands`),
  - startup check (`_assess_lcr/_h7/_stage`, `_collect_drain`),
  - staleness monitor (`_check_stream_health`, the 1 s/3 s thresholds),
  - `meta.json` writer, `events.csv` writer (see WI-3).
- [ ] Keep it free of `operator_io`/Qt — it takes callbacks/returns values so
      the console (and a future headless runner) can drive it.
- [ ] `session.py` either becomes a thin headless wrapper over the core, or is
      retired once the console exists (decide in §5).

### WI-2 — `sma_console.py` (PySide6 + pyqtgraph, layout A)
- [ ] `QMainWindow` with `QHBoxLayout` central widget.
- [ ] **Top status bar**: connection dots (H7 / LCR / stage; green/red),
      `REC` indicator, **persistent red `DISARM`** button (always enabled).
- [ ] **Left control rail** (group boxes):
  - SMA: `arm`/`disarm`; mode toggle fire ↔ cycle; fields `V_high`, `t_high`,
    `V_idle`, `t_idle`, `n`; `start`/`stop`; trims (`vdd/offset/aref/gain/
    shunt/ioffset`, collapsible); live readout `V_LDO / I / R / code`.
  - Stage: position label; target field; `home`/`move`/jog `±`; velocity.
  - LCR: connection + live `Ls`/`Rs`; freq/voltage config; `ref open`/
    `ref short` buttons.
- [ ] **Right column**: 3 stacked `pyqtgraph.PlotWidget`s — (1) displacement
      (laser) + force (load), (2) SMA `V/I/R`, (3) LCR `Ls/Rs`.
- [ ] **Bottom**: read-only event-log pane (mirrors `events.csv` + warnings).
- [ ] **`QTimer` (~20 Hz)** = the heartbeat: drain queues → append CSVs →
      update readouts → push to rolling plot buffers → run `core.check_health()`
      → on a 3 s stale → `disarm` + banner.
- [ ] **Command handlers** → `h7_commands` → `core.sma_send()` → reader; each
      logged to `events.csv`. **Auto-`disarm`** when an actuation completes (or
      on a fire one-shot finishing). Arm implicitly before fire/cycle if needed.
- [ ] Guard: warn if `cycle`/`fire` is started for an LCR run without a recent
      `ref open`/`ref short`.
- [ ] Graceful shutdown: on window close → `stop` + `disarm`, join workers,
      write `meta.json`.

### WI-3 — Logging model (continuous + events)
- [ ] One session dir `data/console_<timestamp>/` with continuous CSVs
      (`h7.csv`, `lcr.csv`, `stage.csv`, `status.csv`) — schema identical to
      the current per-phase files so the analyzer reuses columns.
- [ ] `events.csv`: `host_timestamp_s, monotonic_s, kind, detail` where `kind`
      ∈ `{cmd, ref_open, ref_short, warn, arm, disarm}` and `detail` is the
      command string / reason. This **replaces phase-boundary file splitting** —
      the analyzer segments runs by these markers.
- [ ] `meta.json`: instruments, IDNs, config, host, errors, session window.

### WI-4 — Retire the rigid phases / second entry point
- [ ] `run_experiment.py` → **retire** (its drive/cycle one-shot is just the
      console with `n` set; its own drain/meta/ping loop is now duplicated by
      the core). **Note**: it currently lacks `arm` and builds command strings
      inline — do not copy that; it would be rejected by the new firmware.
- [ ] OPEN/SHORT/RAW linear flow → gone; OPEN/SHORT become `ref open`/
      `ref short` markers in `events.csv`.

### WI-5 — `analyze_sma.py` update
- [ ] Read references from `events.csv` (`ref_open`/`ref_short` segments)
      instead of `*_open.csv`/`*_short.csv`.
- [ ] Segment the continuous log into actuation windows by `events.csv` `cmd`
      markers; produce a dashboard per actuation (or one combined).
- [ ] Honor the M4-clock `src=3/4/5` timestamps (now aligned) and the new
      `[STATUS]` fields if useful (e.g. annotate `dropped`/`crc_err`).

### WI-6 — Reader consolidation (shared package) — separate, lower priority
- [ ] **Blocker today**: 4 copies of `portenta_reader.py` share the *same
      module name* across dirs (sys.path shims), so a `from portenta_reader
      import *` shim is circular. And the APIs have diverged (SpringSmokeTest
      folds a `StatusFrame` dataclass into `parse_line`; the canonical keeps
      `parse_line`=samples + `parse_status_line`=dict).
- [ ] Proper fix: create a uniquely-named shared package `h7_io/` (canonical
      reader + `StatusFrame` + `iter_events` + command helpers); migrate
      `Calibrate_LaserHead`, `Calibrate_LoadCell`, `Experiment_SpringSmokeTest`,
      and the console to import it; delete the copies.
- [ ] **Bench-test the Stable calibration modules** after repointing — do not
      do this blind.

### WI-7 — Retire V2
- [ ] Move `Experiment_SMACharacterizationV2/` to `Archieve/` (already marked
      deprecated). V3/console is a strict superset.

### WI-8 — Naming (optional, do last)
- [ ] If kept separate: `operator_io.py → console_ui.py`. The new entry is
      `sma_console.py`. After the core extraction, `session.py` is either
      `recording_core.py` (shared) or retired.

---

## 4. Build sequence

1. **Phase 1 — core + console shell (no plots).** WI-1 + WI-2 (controls,
   logging, health, auto-disarm) + WI-3. Prove it connects, passes the health
   check, records continuous CSV + events, and arms/fires/cycles/disarms
   correctly on the bench. **This is the milestone that makes the rig usable.**
2. **Phase 2 — live plots.** Feed rolling buffers into the 3 pyqtgraph panels.
3. **Phase 3 — cleanup.** WI-4 (retire run_experiment + phases), WI-5
   (analyzer), WI-7 (archive V2), then WI-6 (reader package, bench-tested),
   WI-8 (rename).

---

## 5. Open decisions (resolve at review)

- [ ] Keep `session.py` as a headless wrapper over `recording_core`, or fully
      retire it (console becomes the only front end)? Recommend: retire after
      the console is proven; keep a `--headless` console mode for scripted runs.
- [ ] Final entry-point name: `sma_console.py` (recommended) vs `run.py`.
- [ ] Dockable Qt panels (user can rearrange/pop-out plots) — nice-to-have,
      default to layout A docked.
- [ ] Plot update rate + downsampling (800 SPS in, display ~10–20 Hz rolling
      window of N seconds; decimate per panel).
- [ ] LCR/stage as **auxiliary** (a failure warns but doesn't stop the SMA run,
      like `run_experiment.py` did) vs critical. Recommend auxiliary.

---

## 6. Dependencies

```
pip install PySide6 pyqtgraph
```
(`pyserial`, `pyvisa`, `zaber-motion`, `numpy`, `matplotlib`, `pyyaml` already
in the per-module requirements.)

---

## 7. Testing / verification

- [ ] `python -m py_compile` every changed/new file (the §0 host edits too).
- [ ] Headless smoke: start the workers without the GUI, confirm the queues
      fill and the core writes CSV + events.
- [ ] Firmware: `pio run -e portenta_m7` / `-e portenta_m4`, power-cycle,
      `arm` → `fire 3.0` / `cycle …`, confirm `[STATUS]` fields, `disarm`.
- [ ] GUI on the rig: health check pass/fail, live readouts, `DISARM`,
      auto-disarm between actuations, 1 s warn / 3 s disarm on unplugging a
      stream, plots update, clean shutdown writes `meta.json`.
