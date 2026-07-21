> **Status: WIP / To-Test** — code-complete, offline analyzer + offscreen GUI + headless flow verified on synthetic data; not yet bench-run. See [STATUS.md](STATUS.md). Project map: [../README.md](../README.md).

# Experiment_SMAThermalCharacterization — multi-instrument SMA thermal console + analyzer

Forked from `Experiment_SMACharacterizationV3` to focus on **thermal**
characterization of the SMA (Joule-heating temperature response). It keeps
the same session backbone: **one config file** sets every instrument and
sensor parameter, a **single continuously-logging console** (`operator_console.py`)
records **raw** streams from the combined-firmware H7 (sensors *and* SMA) and
the Zaber stage while controlling both, and an **offline analyzer** converts
raw→physical and renders dashboards.

> **No LCR.** Unlike V3, this thermal module has **no LCR** — no connection,
> no `lcr.csv`, no LCR UI/control. The LCR config section and worker are
> hard-disabled (`build_core` never constructs an `LcrWorker`).
>
> **Thermal scope TODO:** the temperature-measurement stream (thermocouple /
> IR sensor) and any thermal-specific analysis are not yet wired — otherwise
> this module behaves like V3 minus the LCR. See STATUS.md.

The console (GUI or `--headless`) replaces the rigid OPEN→SHORT→RAW phase
flow with **continuous logging + event markers**: instead of swapping files
at phase boundaries, every command and reference is timestamped into
`events.csv`, which the analyzer uses to segment the run.

## Architecture (same backbone as V2)

```
config.yaml ─► startup: stage.home/velocity, (optional H7 cmds)   [no LCR]
                    │
   ┌────────────────┴───────────┐
 H7Worker                   ZaberWorker     (threads → bounded queues)
 COM8                       COM5
   └────────────────────────────┘
                    │ queues
         SessionController  (sole CSV writer, OPEN→SHORT→RAW state machine)
                    │
   session CSVs + meta.json ─► operator_explore.ipynb (lib_analysis) ─► interactive plots
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
| `lcr` | **removed in this module** — kept only as `enabled: false` (no LCR) |
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

## Run a session — `operator_console.py` (primary entry point)

```sh
pip install -r requirements.txt          # + PySide6 or PyQt5 for the GUI
python operator_console.py                     # GUI console (layout A)
python operator_console.py --headless          # scripted run, no GUI (Ctrl+C to stop)
python operator_console.py --session-id flexinol_run01
```

One window (or one headless loop) controls the **stage, SMA, and camera** and
continuously logs every enabled stream. Layout A: top status bar
(H7/stage/cam connection dots + `REC` + a persistent red **DISARM**), left
control rail (SMA arm/disarm, fire↔cycle, start/stop, live `V/I/R`; stage
position/target + home/go/STOP + limit window; **camera** resolution/fast-fps
dropdowns + transient/heartbeat fields + **live preview**), four stacked live
plots (laser displacement / load force / SMA V·I / SMA R), and a bottom event
log. (No LCR row.) The laser/load plots show **mm / N** when calibration is
present in `config.yaml`, else fall back to raw volts — display-only; CSVs stay
raw.

**Camera (adaptive-FPS video + preview).** The 12MP USB3 camera records at a
fixed resolution and **variable frame rate**: fast while the SMA moves, a slow
heartbeat once settled ("moving" = median-filtered net laser displacement ≥
`change_threshold_mm`, so sensor noise doesn't keep it recording). Each heat/idle
event forces fast for `transient_guarantee_s`. Same **Start/Stop REC** button;
auxiliary (a camera failure warns, never stops the SMA run). Resolution/fast-fps
apply on reconnect and lock while recording; transient/heartbeat are live.
Output → `video/{frames.csv, cycle_NN/*.jpg, snapshots/*.jpg}`; `frames.csv`
timestamps every frame on the shared clock for offline join. Needs
`opencv-python`.

A 20 Hz timer is the heartbeat: drain queues → append CSVs → update
readouts/plots → run the staleness monitor → auto-`disarm` on a critical
stall. **The MOSFET is armed only around an actuation** and `DISARM` is
always live.

Output: `data/console_<session_id>/` with continuous `h7.csv` /
`stage.csv` / `status.csv` (no `lcr.csv`), the **`video/`** dir
(`frames.csv` + `cycle_NN/*.jpg` + `snapshots/*.jpg`), plus **`events.csv`**
(`host_timestamp_s, monotonic_s, kind, detail`; `kind` ∈
`session/cmd/arm/disarm/camera/warn/error`), `meta.json`, and `session.log`.

**Critical vs auxiliary streams:** the **H7** sensor hub is critical — a
startup failure aborts and a >3 s mid-run stall auto-disarms. The **Zaber
stage** is **auxiliary**: a failure or stall WARNS (and is logged to
`events.csv` / `meta.errors`) but never stops the SMA run.

The startup **full-system check** (identity + streaming + sane values) runs
once before recording; `ref open` / `ref short` drop on-demand de-embed
reference markers — no separate OPEN/SHORT phases.

### Headless mode

`--headless` drives the same `recording_core.RecordingCore` with no Qt
dependency: it opens outputs, runs the startup check, optionally starts the
`config.sma` cycle (arm + cycle + 1 Hz `ping`), records until Ctrl+C, then
stops + disarms + writes `meta.json`. Use it for scripted/automated runs.

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

Scripted/automated runs use the console headless — the cycle comes from
`config.yaml`'s `sma:` block:

```sh
python operator_console.py --headless        # scripted drive+log (Ctrl+C to stop)
```

## Analyze + visualize — `operator_explore.ipynb`

Analysis now lives in an **interactive Plotly notebook**, `operator_explore.ipynb`
(open it with the repo-root `.venv` kernel). It imports every loader, calibration,
clock-alignment and segmentation helper from **`lib_analysis.py`** (the shared core
extracted from the former `sma_plots.py`), so the notebook stays thin and
single-sourced. Three parts:

1. **Raw sanity** — no conversion, in mV / mA: `V_LDO` (`sma_v`), `I_SMA`, `V_laser`,
   `V_load`. ADC-rail saturation flagged red. "Are the readings sane?"
2. **Converted + actuation-marked** — R [Ω], power P = V·I [W], displacement [mm],
   force [mN], and ΔR/R₀ [%], with fire windows shaded.
3. **Cross-plots** — displacement & force vs resistance (shared x) and vs power
   (shared x), colored by fire-vs-cool phase to reveal the hysteresis loop.

Save any zoomed view with the figure's camera-icon (high-DPI PNG). Calibration
constants come from `config.yaml` (or `meta.json` when present); the notebook
uses the firmware clock (`hw_us`) and shifts the M4 (laser/load) channels onto
the M7 (SMA) timeline so fire onsets line up.

> **Not yet ported (methods live in `lib_analysis.py`, no notebook cell renders
> them yet):** the **laser-tone diagnostic** (`fit_tone` / `notch_fft` / ZOH-dup
> marking → the old `laser_diagnostics.png`), the **per-cycle overlay**
> (fire-onset-folded cycles), and the **transition ensemble + first-order cooling
> fit** (`fit_exp` → the old `transition_fit.png`). The methods and findings below
> still apply — they describe what the analysis does; the CLI that rendered them
> was `sma_plots.py`, now removed. Porting these into notebook parts is a TODO.

### Analysis parameters

These were the `sma_plots.py` CLI flags; the knobs still exist as `lib_analysis`
function arguments and are documented here as the reference to wire up when the
`cycles` / `laser` / `transition` views are ported into notebook cells.

| view | knob | default | what it does |
|---|---|---|---|
| `cycles` | `--r-ref cold\|cycle` | `cold` | R₀ that resistance is normalized to. `cold` = the session's initial baseline reading (one reference for the run, so the cycle-to-cycle drift stays **visible**); `cycle` = each cycle's own pre-fire R (drift **divided out**). |
| `cycles` | `--notch HZ\|auto` | *off* | Notch this frequency + harmonics out of the **displacement** channel. `auto` fits the tone in 40–90 Hz. |
| `cycles` | `--notch-q` | `30` | Notch sharpness (f₀/width); higher = narrower. |
| `cycles` | `--notch-harmonics` | `3` | How many harmonics of `--notch` to remove. |
| `cycles` | `--lowpass HZ` | *off* | Low-pass the **displacement** channel (4th-order Butterworth magnitude, zero-phase). SMA actuation lives below a few Hz, so ~20 Hz is generous. |
| `laser` | `--fmin` / `--fmax` | `5` / `195` | Tone-search band (Hz). |
| `laser` | `--zoom-s` | `0.20` | Width of the zoom panel (s). |
| `laser` | `--notch-hw` | `1.0` | Notch half-width (Hz) in the before/after demo. |
| `transition` | `--bin-ms` | `150` | Ensemble time-bin width. **Don't go too fine** — at 25 ms the per-bin SEM (±1.7%) approaches the ~3% effect and a real transient reads as noise. |

**Filtering is opt-in and never silent.** With no notch/low-pass, everything is
**raw** (Parts 1–3 of the notebook are always raw). When a filter is applied
(`lib_analysis.filter_channel` / `notch_fft`): only the **displacement** channel
should be touched — force and resistance are never filtered — and any filtered
figure must be clearly stamped so the raw view is never silently replaced.

Note the filter cannot remove the **drive feedthrough** (artifact 3 below) — that
is synchronous with the fire, not at a distinct frequency, so no frequency filter
touches it. The view keeps flagging it even under `--lowpass`.

The `cycles` view recovers the `cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>`
command from `events.csv`, locates each fire onset as a rising edge of `sma_v`,
and writes `cycles_timeline.png`, `cycles_overlay.png` (all cycles folded onto
fire-onset time), `cycles_trend.png` and `cycles_metrics.csv`. Resistance is
plotted as **R/R₀** — raw ohms are not comparable across cycles because the
pre-fire R drifts — with `--r-ref cold` (default; R₀ = the session's initial
baseline reading, so the drift stays visible) or `--r-ref cycle` (R₀ = each
cycle's own pre-fire R, drift divided out). Every per-cycle metric is drawn
against its own **±2σ noise band**, so a "trend" that is really scatter reads as
scatter.

The `transition` view answers "is the SMA transition actually there?" Single-cycle
resistance is hopeless (σ ≈ 6% per sample vs a ~3% effect), but the run fires the
SAME cycle N times, so the transient is recovered by **ensemble averaging**: fold
every cycle onto the fire onset, average **within a window** and then **across
cycles**. That drops the error to ~0.5% and the transition appears at >5σ:

| window | ΔR/R₀ | verdict |
|---|---|---|
| during fire (0–100 ms) | **−3.13% ± 0.54%** | **RESOLVED** (t = −5.8) |
| after fire (0.1–0.3 s) | **−2.08% ± 0.50%** | **RESOLVED** (t = −4.2) |
| early cool (0.3–1.0 s) | −2.00% ± 0.79% | marginal |
| late cool (2–3 s) | −0.42% ± 0.69% | recovered to baseline |

**Resistance DROPS ~3% during the fire and recovers over the cool.** Note the
sign: an estimator that takes `max()` over the fire window finds only the largest
*noise* excursion (always positive) and reports a meaningless *rise* — which is
exactly what an earlier version of this code did. Use the window **mean**.

It also fits a first-order thermal model to the cooling phase. **τ_F ≳ 6 s while
`cool_ms` is only 3 s**, so the coil never returns to baseline before the next
fire — which is why the force baseline ratchets upward across the run. The fit
warns when τ exceeds half the observation window (it is then only a lower bound).
**Raise `cool_ms` to ≥ 3–5 × τ (~20–30 s) if you want clean, independent cycles.**

`transition_per_cycle.png` shows the same thing **cycle by cycle** rather than
pooled — one small multiple per cycle, each with an error bar from *that cycle's
own* ~8 in-fire samples (nothing borrowed from the ensemble), plus a trend panel.
Most cycles resolve on their own at >5σ, the spread across cycles (±1.6%) is 2.8×
the measurement error — so the **cycle-to-cycle variation is real** — and on this
run cycle 8 shows **no drop at all**, right after the cycle-7 mechanical event
that also spikes the force 7×. That is exactly the kind of thing an ensemble
average erases, which is why both figures exist.

The `laser` view is the instrument diagnostic: it pulls out the coherent ~65.8 Hz
tone, marks the zero-order-hold duplicate rows, and renders a before/after of the
two corrections applied in post. Point it at an **idle** recording — see the
reference sample below. Full write-up in the next section.

## Known signal artifacts — diagnose with `console_20260713_122906`

**`data/console_20260713_122906/` is the reference diagnostic sample.** It is a
~19 s recording with **the laser aimed at a rigid, immovable block** — no
actuation, stage held, nothing mechanically able to move. So anything that shows
up in it is **instrumentation, not SMA physics**, by construction. Re-run the
laser diagnostic (`lib_analysis.fit_tone` / `notch_fft` on this session — the
laser-view port is a TODO) after any change to the laser wiring / ADC config and
compare against the committed PNGs.

**Three artifacts are known. Ranked by how much they actually threaten a result:**

| # | Artifact | Threat | Why |
|---|---|---|---|
| **3** | **Drive feedthrough on the laser during a fire** | **HIGH — this is the dangerous one** | It is **synchronous with the actuation**, landing exactly where the real displacement signal should be. No frequency filter can separate it, because it is not at a different frequency — it is *at* the signal. This is the one that can be mistaken for SMA contraction and quietly become a wrong result. |
| 1 | 65.8 Hz laser tone | LOW — accepted | Out of band (66 Hz vs a DC–few-Hz signal) and stationary. Averages/filters away. Costs ~4× raw resolution, corrupts nothing. |
| 2 | ~19% zero-order-hold duplicate rows | LOW — accepted | Effective rate is 400 Hz not 493. Only matters for row-count-derived rates/σ/spectra. Harmless to a force or displacement reading. |

Underneath all three sits the harder open question: **in `115921` the real SMA
displacement appears to sit below the laser's noise floor entirely** — only the
*force* clearly responded to firing. So the live issue is not "the laser is
noisy", it is "is the laser currently measuring anything at all?" That deserves
more attention than artifacts 1 and 2 combined.

### 1. The laser's "noise" is a coherent ~65.8 Hz wave

> **Verdict (2026-07-13): known, characterized, and ACCEPTED — not fatal.** It is
> out of band and stationary; see "Why we are living with it" below. This section
> exists so that if it ever *does* bite us, we know where to start looking.

On a whole-session plot the laser looks like a ±1.4 µm noise band. It is not
noise. Zoom in and it is a **clean periodic ripple**:

| | |
|---|---|
| Frequency | **65.77 Hz** (period 15.21 ms, ~7.5 samples/cycle) |
| Amplitude | **1.72 µm** (3.44 µm peak-to-peak) — only **0.86 mV** on a 4.97 V signal |
| Share of the laser's variance | **74%** |
| σ with the tone removed | 1.41 µm → **0.72 µm** |
| Stability | frequency drifts **< 0.01%** over 19 s; amplitude within 1% |
| Present on load / ADC2? | **No** — no peak there at all |

#### The evidence that it is instrumental, not mechanical

Two independent facts, and together they are conclusive:

1. **It survives an immovable target.** `122906` *is* the "point the laser at
   something that cannot move" test. The tone is still there — so it is not the
   SMA specimen, and not the fixture flexing under load.
2. **It does not move when the mechanics change.** Fit the same tone in the
   *actuation* session, which has a completely different mechanical setup (a
   compliant SMA coil mounted and fired 10×, not a rigid block):

   | session | setup | tone |
   |---|---|---|
   | `console_20260713_122906` | laser on an **immovable block** | **65.7774 Hz**, 1.77 µm |
   | `console_20260713_115921` | **SMA specimen mounted, fired 10×** | **65.7724 Hz**, 1.62 µm |

   Same frequency to **0.005 Hz (0.008%)** across two different masses and
   stiffnesses. A real mechanical resonance would have shifted. It did not budge.
   (Control: the same fit on the **load** channel finds nothing coherent in either
   session — R² = 0.00, landing on a random frequency.)

So the tone **does not care what the laser is pointed at** — it is manufactured
inside the measurement chain, and it is locked to a crystal-grade clock (0.008%
over 80 s is not a motor or a fan). Combined with ADC2 being clean while both
ADCs share the REF7050 reference, that leaves exactly two places it can live:

- **inside the Keyence IL-030** — its internal sampling, or ripple on its analog output; or
- **in the AIN4/AIN5 wiring / the ADC1 front end**.

#### Why we are living with it (and why that is defensible)

The signal we actually care about — SMA contraction over a 100 ms fire and a 3 s
cool — lives from DC to a few Hz. The interferer sits at 65.8 Hz, an order of
magnitude above it, and is **stationary**: it does not change when we fire, and it
does not drift. That makes it benign:

- Averaging over the 3 s cool spans ~197 cycles of the tone → suppressed to nothing.
- Even averaging over the 100 ms fire spans ~6.6 cycles → 1.7 µm collapses to < 0.1 µm.
- A low-pass or notch recovers the channel fully in post: **σ 1.29 → 0.31 µm, ~4×**
  (see `laser_before_after.png`).

It costs ~4× in *raw displayed* resolution and makes raw plots ugly. It does not
corrupt any slow measurement. **Nuisance, not defect.**

#### If it bites us later — start looking here, in this order

1. **Take the laser out of the loop.** Disconnect the IL-030 from AIN4/AIN5 and
   feed ADC1 a steady DC voltage (battery + divider, or just short the inputs).
   Record 20 s, run the laser diagnostic (`lib_analysis`, laser-view port TODO).
   - Tone **survives** → it is the **ADC front end / wiring / Portenta**; the
     laser is innocent.
   - Tone **vanishes** → the **IL-030 is generating it**; next question is its
     analog output stage vs its internal sampling.

   This is a five-minute test, needs no firmware change, and halves the search space.
2. **Resolve the alias** (only if root-causing). At 400 SPS the Nyquist is 200 Hz,
   so 65.77 Hz is equally consistent with a real **335 Hz or 466 Hz** source, and
   those implicate very different culprits. Knowing the true frequency would likely
   name the clock outright. **This needs a real firmware change, not a config
   tweak** — see artifact 2 and the STATUS TODO: the M4's 2 ms `millis()` poll caps
   the read path at ~500 Hz, so merely raising `ADS1263_400SPS` → `ADS1263_1200SPS`
   would just discard conversions and change nothing.
3. **Watch for it becoming non-stationary.** The whole "it's benign" argument rests
   on the tone being out-of-band and steady. If it ever drifts in frequency, grows,
   or acquires a low-frequency sibling, the argument collapses and this moves to the
   top of the list.

### 2. ~19% of `h7.csv` rows are zero-order-hold duplicates

The stream is **read faster than the ADC converts**:

- read rate (from `hw_us`): **492.85 Hz**
- duplicate-`raw_code` fraction: **18.7% (laser/ADC1), 18.8% (load/ADC2)**
- unique-sample rate: 492.85 × (1 − 0.187) = **400.7 Hz** — exactly the
  configured 400 SPS

So roughly 1 row in 5 is an exact repeat of the previous conversion, on **both**
channels. **The effective sample rate is 400 Hz, not 493 Hz, and Nyquist is
200 Hz.** Any rate/σ/spectrum computed from the row count is off by ~23%, and
duplicated rows fake a smoother signal. Detect them with
`np.diff(raw_code) == 0` and decimate to changed-code samples to recover the true
400 SPS series.

**Always use `hw_us` (the firmware clock) as the time base, never
`host_timestamp_s`** — host timestamps are USB-batched and bursty (median dt
1.06 ms but σ 3.4 ms), which smears any spectrum badly enough to hide the tone
entirely. That is why this went unnoticed.

### Outputs

The `laser` view writes, into the session dir:

- `laser_diagnostics.png` — full trace, zoom (duplicates marked), amplitude
  spectrum (laser vs load), phase-fold, autocorrelation, and the residual once
  the tone is removed.
- `laser_before_after.png` — the same data before vs after the two corrections
  applied **in post** (drop the ZOH duplicates, notch the tone + harmonics):
  σ **1.29 µm → 0.31 µm, ~4× better**. This is a post-processing demonstration
  of what the channel is worth without the interferer — **no hardware change has
  been made**, so treat it as an upper bound on the gain, not a fixed rig.

### 3. The laser also picks up the SMA drive pulse

Separately, on actuation runs the laser steps **+3.2 to +3.6 µm inside every
fire window** and is back to 0 ± 0.3 µm by +0.25 s — exactly when the force is
*peaking*. Real contraction visible in the force would still be present at the
force peak; this isn't, and the step is unchanged even on a cycle where the force
was 8× larger. It tracks the 0.7 A drive, not the mechanics: **drive feedthrough,
not displacement.** The `cycles` view tests for this automatically and labels the
panel when it triggers.

## Files

Files carry a role prefix: **`operator_`** = a human launches it directly;
**`lib_`** = imported internals, never run on their own.

```
Experiment_SMAThermalCharacterization/
├── README.md / STATUS.md / requirements.txt
├── config.yaml                 every instrument + sensor parameter
│
├── operator_console.py         PRIMARY entry — GUI console + --headless (record + control)
├── operator_explore.ipynb      interactive Plotly analysis notebook (raw / converted / cross-plots)
│
├── lib_config.py               typed dataclasses (h7/stage/phases/calibration/run; no LCR)
├── lib_workers.py              H7Worker (multi-channel), ZaberWorker, CameraWorker
├── lib_h7_commands.py          firmware command builders (single source of truth)
├── lib_recording_core.py       RecordingCore — UI-agnostic continuous recorder/control
├── lib_camera.py               adaptive-FPS camera capture (threaded or spawn subprocess)
└── lib_analysis.py             loaders, calibration, clock-alignment, segmentation,
                                fitting/filtering — the shared core the notebook imports
```

The former CLI plotters (`analyze_sma.py`, `sma_plots.py`) and the legacy
OPEN→SHORT→RAW recorder (`sma_recorder.py` / `session.py` / `operator_io.py`)
and the retired `run_experiment.py` were **removed** — superseded by the console
+ notebook; they remain in git history.

Cross-module drivers are imported via `sys.path` shims (canonical sources:
`Driver_KeysightLCR`, `Driver_ZaberStage`, `Calibrate_LaserHead`), not
re-implemented here.

## Relationship to V3 / V2

Forked from `Experiment_SMACharacterizationV3` (which itself supersedes
`Experiment_SMACharacterizationV2` for the combined-firmware rig). This module
specializes V3 toward thermal characterization; the mechanical/electrical
console backbone is unchanged from V3 until the thermal stream is added.
