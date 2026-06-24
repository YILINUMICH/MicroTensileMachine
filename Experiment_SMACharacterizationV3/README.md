> **Status: WIP / To-Test** — code-complete, offline analyzer + offscreen GUI + headless flow verified on synthetic data; not yet bench-run. See [STATUS.md](STATUS.md). Project map: [../README.md](../README.md).

# Experiment_SMACharacterizationV3 — multi-instrument SMA console + analyzer

V3 extends the V2 recorder into a full multi-instrument session: **one
config file** sets every instrument and sensor parameter, a **single
continuously-logging console** (`sma_console.py`) records **raw** streams
from the LCR, the combined-firmware H7 (sensors *and* SMA), and the Zaber
stage while controlling all three, and an **offline analyzer** converts
raw→physical and renders dashboards.

The console (GUI or `--headless`) replaces the rigid OPEN→SHORT→RAW phase
flow with **continuous logging + event markers**: instead of swapping files
at phase boundaries, every command and reference is timestamped into
`events.csv`, which the analyzer uses to segment the run.

## Architecture (same backbone as V2)

```
config.yaml ─► startup: LCR.configure, stage.home/velocity, (optional H7 cmds)
                    │
   ┌──────────┬─────┴──────┬───────────┐
 LcrWorker  H7Worker     ZaberWorker     (threads → bounded queues)
 VISA       COM8         COM5
   └──────────┴────────────┴───────────┘
                    │ queues
         SessionController  (sole CSV writer, OPEN→SHORT→RAW state machine)
                    │
   per-phase CSVs + meta.json ─► analyze_sma.py ─► dashboards + joined CSV
```

Workers stream continuously across all phases; the controller is the only
file writer. Any stream can be disabled with its `enabled:` flag.

**Design rule:** the recorder logs **raw data only** — it configures
instruments but never converts units or pushes calibration to firmware.
Calibration coefficients live in `config.calibration`, are copied into
`meta.json`, and are used **only** by the offline analyzer.

## Configuration — `config.yaml`

| Section | Sets |
|---|---|
| `lcr` | E4980 function/frequency/voltage/integration/averaging/poll + `enabled` |
| `h7` | port/baud, `channels` (which of laser/load/sma_v/sma_i/sma_r to keep), `startup_commands` (inert hook) |
| `stage` | Zaber port, `position_limits_mm`, velocity, reading rate, home/zero options, poll |
| `phases` | OPEN / SHORT durations (RAW runs until Ctrl+C) |
| `calibration` | **analysis-only** coefficients: laser `{k_mV_per_um, V0_mV}`, load cell `{scale_N_per_V, offset_V}`, current sense (firmware defaults, for traceability) |
| `run` | operator, notes, output dir |

## The H7 stream

The combined firmware ([`Firmware_SMASensorHub_PIO`](../Firmware_SMASensorHub_PIO/))
emits one multiplexed stream: `src=1` laser, `2` load, `3` SMA V, `4` SMA I,
`5` SMA R. `H7Worker` reads it with the extended
[`Calibrate_LaserHead/portenta_reader.py`](../Calibrate_LaserHead/portenta_reader.py)
(`adc_source=None`), demuxes by channel, and logs every enabled channel
raw to one `*_h7.csv` per phase (with `src`/`channel` columns). For
`src=4/5` the `value` column carries **amps / ohms** (firmware-computed),
not volts.

## Run a session — `sma_console.py` (primary entry point)

```sh
pip install -r requirements.txt          # + PySide6 or PyQt5 for the GUI
python sma_console.py                     # GUI console (layout A)
python sma_console.py --headless          # scripted run, no GUI (Ctrl+C to stop)
python sma_console.py --session-id flexinol_run01
```

One window (or one headless loop) controls the **stage, LCR, and SMA** and
continuously logs every enabled stream. Layout A: top status bar
(H7/LCR/stage connection dots + `REC` + a persistent red **DISARM**), left
control rail (SMA arm/disarm, fire↔cycle, start/stop, live `V/I/R`; stage
position/target; LCR `Ls/Rs` + `ref open`/`ref short`), three stacked live
plots (displacement+force / SMA V·I·R / LCR Ls·Rs), and a bottom event log.

A 20 Hz timer is the heartbeat: drain queues → append CSVs → update
readouts/plots → run the staleness monitor → auto-`disarm` on a critical
stall. **The MOSFET is armed only around an actuation** and `DISARM` is
always live.

Output: `data/console_<session_id>/` with continuous `lcr.csv` / `h7.csv` /
`stage.csv` / `status.csv`, plus **`events.csv`** (`host_timestamp_s,
monotonic_s, kind, detail`; `kind` ∈ `session/cmd/arm/disarm/ref_open/
ref_short/warn/error`), `meta.json`, and `session.log`.

**Critical vs auxiliary streams:** the **H7** sensor hub is critical — a
startup failure aborts and a >3 s mid-run stall auto-disarms. The **LCR** and
**Zaber stage** are **auxiliary**: a failure or stall WARNS (and is logged to
`events.csv` / `meta.errors`) but never stops the SMA run.

The startup **full-system check** (identity + streaming + sane values) runs
once before recording; `ref open` / `ref short` drop on-demand de-embed
reference markers — no separate OPEN/SHORT phases.

### Headless mode

`--headless` drives the same `recording_core.RecordingCore` with no Qt
dependency: it opens outputs, runs the startup check, optionally starts the
`config.sma` cycle (arm + cycle + 1 Hz `ping`), records until Ctrl+C, then
stops + disarms + writes `meta.json`. Use it for scripted/automated runs.

### Legacy recorder — `sma_recorder.py`

The older interactive OPEN→SHORT→RAW recorder is still present (backed by
`session.py` / `operator_io.py`) and writes the per-phase
`{open,short,raw}_{lcr,h7,stage}.csv` layout. The console supersedes it.

### SMA actuation — the state machine runs on M7

By default (`sma.enabled: false`) the recorder is a **pure logger** and you
drive the SMA manually from the H7 console (`drive`/`fire`/`cycle`).

Set `sma.enabled: true` to have the recorder drive the firmware's
**on-M7 cyclic actuation**: at RAW start it sends one
`cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>`, a `ping` heartbeat every
second, and `stop` at the end. **The PC only sends parameters + heartbeat —
M7 owns all phase timing**, so heat/cool durations are deterministic and
immune to USB/host-scheduling jitter. If the recorder crashes or the host
goes silent, M7's watchdog (`sma.wdt_ms`) safe-stops the SMA. Configure it
in the `sma:` block:

```yaml
sma:
  enabled: true
  v_high: 3.0
  v_low: 0.0
  fire_ms: 2000
  cool_ms: 8000
  n_cycles: 10      # 0 = continuous until RAW Ctrl+C
  wdt_ms: 5000
```

## One-shot automated experiment — `run_experiment.py` (RETIRED)

`run_experiment.py` is **retired** — it is now a stub that refuses to run. It
built firmware command strings inline and never sent `arm`, which the rebuilt
`Firmware_SMASensorHub_PIO` rejects. Its drive/cycle one-shot is just the
console with `n` set; use:

```sh
python sma_console.py --headless        # scripted drive+log (Ctrl+C to stop)
```

The cycle still comes from `config.yaml`'s `sma:` block. The original
implementation remains in git history.

## Analyze + visualize

```sh
# console layout (events.csv) and legacy per-phase layout both auto-detect
python analyze_sma.py --session data/console_20260624_153000
python analyze_sma.py --session <dir> --ref-window 8          # console refs
python analyze_sma.py --session <dir> --mode phase --phase raw  # legacy
python analyze_sma.py --session <dir> --k -0.1171 --v0 566.957 --load-scale 50.0
```

`--mode auto` (default) picks **console** when `events.csv` + `h7.csv` are
present, else the **legacy per-phase** layout. Produces, in the session dir:

- `<label>_dashboard.png` — multi-panel: displacement, force, SMA R/V/I,
  de-embedded LCR R/L, stage position, and force-vs-displacement
  (`<label>` = `console` or the phase name).
- `<label>_joined.csv` — all streams interpolated onto a uniform 100 Hz grid.

In **console mode** the OPEN/SHORT de-embed references are the LCR samples in
the `--ref-window` seconds (default 10) after each `ref_open`/`ref_short`
marker in `events.csv`; the actuation trace is every LCR sample *outside*
those windows. Conversions are applied only where the coefficient is present
in `meta.json` (or overridden on the CLI); otherwise the channel is plotted
raw. LCR de-embedding auto-selects OPEN+SHORT (2-term) or SHORT-only.

## Files

```
Experiment_SMACharacterizationV3/
├── README.md / STATUS.md / requirements.txt
├── config.yaml            every instrument + sensor parameter
├── config.py              typed dataclasses (lcr/h7/stage/phases/calibration/run)
├── workers.py             LcrWorker, H7Worker (multi-channel), ZaberWorker
├── h7_commands.py         firmware command builders (single source of truth)
├── recording_core.py      RecordingCore — UI-agnostic continuous recorder/control
├── sma_console.py         PRIMARY entry — GUI console + --headless
├── session.py             legacy OPEN→SHORT→RAW controller, sole CSV writer
├── sma_recorder.py        legacy interactive OPEN→SHORT→RAW entry point
├── run_experiment.py      RETIRED stub (use sma_console.py --headless)
├── operator_io.py         terminal prompts / progress / banners (legacy recorder)
└── analyze_sma.py         offline de-embed + raw→physical + dashboards (console + phase)
```

Cross-module drivers are imported via `sys.path` shims (canonical sources:
`Driver_KeysightLCR`, `Driver_ZaberStage`, `Calibrate_LaserHead`), not
re-implemented here.

## Relationship to V2

V3 supersedes `Experiment_SMACharacterizationV2` for the combined-firmware
rig (sensors + SMA on one port) and adds stage logging + a config-driven
calibration block. V2 remains as the single-ADC-stream reference.
