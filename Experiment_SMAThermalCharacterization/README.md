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

**On-disk capture format —** [`data/raw/README.md`](data/raw/README.md) is the
reference for reading a capture from scratch: column/stream semantics, the
`hw_us` unwrap and M4↔M7 clock offset, the laser **volts → µm** and load cell
**volts → mN** conversions with their constants and rails, and a verified
reader snippet.

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

Output: `data/raw/console_<session_id>/` with continuous `h7.csv` /
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

**`data/raw/console_20260713_122906_laserfix/` is the reference diagnostic sample.** It is a
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

## Condition sweeps — `operator_current_sweep.py` → `operator_sweep_report.py`

The scripted (non-console) path for mapping the SMA's actuation envelope:
ladders of CC conditions, each a `(i_ma, heat_ms, cool_s, cycles)` cell driven
via `cccycle` with a cold-start settle, a lead-in baseline, and an in-run
**clock-aligned** verdict (the M4 stamps laser/load ~2.2 s behind the M7's SMA
channels; the sweep parses the offset from the firmware's own STATUS line and
aligns live — see STATUS for the bug this killed).

**Workflow — profile → dry-run → run → report:**

```
python operator_current_sweep.py --profile profiles/heat_time_map.json --dry-run
python operator_current_sweep.py --profile profiles/heat_time_map.json
python operator_sweep_report.py  data/raw/sweep_<stamp>          # ALWAYS, right after
```

- **Profiles** (`profiles/*.json`) win over CLI flags and are copied into the
  output folder. Grid form (`levels_ma × heat_ms`) or explicit
  `conditions: [{i_ma, heat_ms, cool_s?, cycles?}, ...]` executed in profile
  order with repeats allowed — the same shape the planned RNN collector will
  generate. **Always `--dry-run` a profile first**: a profile carries its own
  `port`, so a bare "syntax check" drives the rig.
- **`operator_sweep_report.py <folder>`** re-analyses every capture from raw
  and emits `report.txt` (health verdicts), `summary_report.csv` (per-pulse
  flat table with a `railed` validity flag), `fig_envelope.png`, and
  `--timeline` strips. Its checks encode every failure mode this rig has
  produced: laser out-of-window rail, CC tracking >15% (R_est bootstrap /
  clip contact), physically-impossible current sense, load clip, baseline
  jumps, missing pulses.
- **`operator_profile_queue.py <folder|files...>`** runs a whole campaign of
  profiles back-to-back for **unattended** collection — `operator_current_sweep.py`
  handles exactly one profile per launch and exits. It validates every profile
  before the first pulse fires (parse, port present, nothing above `max_ma`),
  runs the report after each, honours `--deadline HH:MM`, and writes
  `queue_manifest.json` mapping each pre-registered role to the capture folder it
  produced. It judges each profile on **captures written, not exit code** (the
  sweep exits 0 on a dead port), and abandons the queue only after two
  consecutive zero-capture profiles — the signature of a rig needing a
  power-cycle. See `profiles/night_profiles_20260805/README.md` for a worked
  campaign.

### One pulse per condition (`cycles: 0`)

For randomized RNN training sets, `cycles: 0` fires **one** pulse per condition,
so consecutive pulses differ in condition — the thermal-history diversity
`NN_SelfSensing_Baseline/DATA_COLLECTION_GUIDELINE.md` §9.2 requires. With
`cycles: 1` the runner fires two pulses at the *same* `(i_ma, heat_ms)`, so every
measured pulse's predecessor is an identical command and the history collapses to
two command-determined classes. The sweep treats the single pulse as the
measurement rather than a bootstrap ramp to discard — which also keeps
`--abort-on-bad-sense` live, since the old code path `continue`d past the sense
check whenever no non-bootstrap pulse existed.

**Reference dataset: `data/raw/sweep_full_150-950mA/`** — two sessions merged and
uniformly re-analysed (`make_summary_and_curve.py` / `make_timeline.py` in the
folder). 100 ms pulses, 12 s cool: monotonic, superlinear, 21 → 864 µm and
2 → 29 mN across 156–939 mA achieved; the 650 mA cross-day repeat agrees to
1%; nothing clips and the binding ceiling is the LDO (~5.2 V / R_wire ≈ 1.1 A),
not any sensor. Judge actuation on DISPLACEMENT (signed mean) — the fixture is
compliant, so the coil moves rather than loading.

**Cautionary dataset: `data/raw/sweep_20260730_031337/`** — the first heat-time
map attempt. The 100 ms row is clean; the FIRST 200 ms cell (150 mA commanded,
301 mA achieved) shifted the coil ~5 mm, took the laser target out of its
window, and blinded 33/82 pulses. Longer pulses raise the stakes: re-check the
laser is mid-window at rest before every session, and treat a CC overshoot as
a clip-contact symptom, not a firmware bug.

## Writing a test profile — rules, limits, template

A profile is the *whole* experiment definition: it carries its own port and
safety settings, so it can drive the rig on its own. Treat it as executable,
not as configuration.

### The one rule that has already cost us

**`--dry-run` EVERY profile before running it.** A profile carries `port`, so a
bare "let me just check the syntax" invocation opens COM8 and fires the coil —
six 150 mA pulses went off that way on 2026-07-30. `--dry-run` prints the full
plan and never opens the port.

### Put the safety settings IN the profile, not on the command line

These are profile keys, and a profile that carries them cannot be run unsafely
by someone who forgot a flag:

```json
"i_low_ma": 0,
"abort_on_bad_sense": true
```

- **`i_low_ma: 0` is mandatory** for `cccycle` runs. The default 100 mA sits
  *below* the reachable floor (0.5 V / 4.69 Ω = 106.6 mA), leaving 5.4 mA of
  margin against 12.6 mA of sense noise; the loop then latches off the floor
  and cool runs at 0.97 V / 208 mA instead of 0.50 V / 108 mA — 4× the idle
  heating, wire never cools, force baseline pins. It destroyed condition 12 of
  35 on the first attempt. With `0` the cool phase releases and parks passively
  at the LDO floor, which still carries ~107 mA so R stays observable.
  **Only safe because of the firmware R seed** — see the CC module STATUS.
- **`abort_on_bad_sense: true`** always. Note its two known defects: it
  mis-attributes the cool-phase latch to the clips, and it *passes* when it has
  too few cool samples to judge.

### Two forms

| form | keys | use when |
|---|---|---|
| **grid** | `levels_ma` × `heat_ms` | full factorial sweeps. Both lists must be **ascending** (enforced) — longer pulses are the unexplored regime, walk into it. Executes heat-OUTER / level-inner, so each pulse width replays the familiar current ladder against a known baseline. |
| **explicit** | `conditions: [{i_ma, heat_ms, cool_s?, cycles?}]` | anything needing per-cell overrides, repeats, a specific order, or a cell omitted. Runs **in profile order** — the profile owns sequencing. This is the shape the RNN collector emits. |

Grid form cannot omit a cell. Since 950×400 must be excluded (below), the
full-span map is written in explicit form.

### Keys

| key | default | notes |
|---|---|---|
| `port` | — | drives the rig; this is why `--dry-run` matters |
| `max_ma` | 800 | hard guard — a condition above it aborts before opening the port |
| `settle_s` | 20 | cold-start settle before each condition |
| `cool_s` | 12 | **an input variable, not bookkeeping** — 850×300 read ~10% larger stroke at 25 s than 15 s. Never mix cool times inside a campaign you intend to compare. |
| `cycles` | 3 | measured cycles; the tool fires `cycles + 1` |
| `i_low_ma` | 100 | **set 0** — see above |
| `abort_on_bad_sense` | false | **set true** |
| `stop_on_fail` | false | stops on *any* NOTE including merely marginal ones; leave false unless you want a ceiling search |

### The measured envelope — what is actually runnable

At 30 s cool, verified 2026-07-31:

| | limit | evidence |
|---|---|---|
| current | **150–950 mA** | CC holds 98–103% across the whole range; LDO rails ~1.1 A |
| pulse | **100–400 ms** | full grid measured |
| **energy ceiling** | **~1.24 J** (850×400) | not the ~1 J previously assumed |
| **950×400** | **excluded — unmeasurable** | pins the laser at the 0 V rail (1228 samples) *and* clips the load cell (5.000 V), at the correct 947 mA |
| 850×400 | the edge | 1.10 V laser margin, 4.08 V of 5 on the load cell |
| load cell | 490 mN full scale | clips from ~850 mA at 300–400 ms |
| laser | 0 V rail | **park the target at the top of its window before a session** — contraction drives the reading down, so parking high buys the full ~9 mm |

Sub-threshold cells (150 mA → 16 µm) are worth keeping: "power in, nothing out"
is a real machine state the network should see.

### Cycle structure

`cycles + 1` pulses fire. Cycle 1 is flagged `bootstrap`. Since the firmware R
seed it is a **real full-energy pulse**, not a ramp — but it fires into a fully
relaxed wire and takes a one-time set (~+370 µm at 850×400, then stable to
~100 µm). So it is a valid measurement of a *different initial condition*: keep
it, keep it flagged, do not pool it into per-condition means.

`cycles: 5` therefore yields 5 comparable cycles + 1 first-cycle sample.

### Duration budget

```
per condition ≈ settle_s + 2.6 s lead-in + (cycles+1) × (heat_ms/1000 + cool_s)
30 s cool, 5+1 cycles         ≈ 207 s
35 conditions                 ≈ 121 min
```

Anything past ~72 min straddles the 32-bit `micros()` wrap. Analysis unwraps it
now, but split very long campaigns anyway — a mid-run failure costs less.

### Ordering

- **Full sweeps:** level-ascending within each pulse width (grid form does this
  for you). Thermally gentle, and a fault shows against a known baseline.
- **Corner probes:** order by **energy ascending**, not current — you meet the
  ceiling at the cheapest cell that reveals it. `profiles/corner_probe_400ms.json`
  runs 750×400 → 950×300 → 850×400 → 950×400 for this reason.
- **RNN collection:** shuffled, in explicit form, so drift cannot correlate with
  condition.

### Template

```json
{
  "name": "my_campaign",
  "description": "What this measures and WHY, plus anything a future reader needs to not repeat a mistake. This field is copied into the output folder.",
  "port": "COM8",
  "max_ma": 1000,
  "settle_s": 20,
  "cool_s": 30,
  "cycles": 5,
  "i_low_ma": 0,
  "abort_on_bad_sense": true,
  "conditions": [
    { "i_ma": 350, "heat_ms": 200 },
    { "i_ma": 850, "heat_ms": 400, "cycles": 3 }
  ]
}
```

### Before you run

```
python operator_current_sweep.py --profile profiles/<name>.json --dry-run
```

Check the condition count, the total time, and that no cell exceeds the
envelope above. Then power-cycle USB + EVM and launch **immediately** — do not
open COM8 with anything else first, including a diagnostic read. Opening and
closing the port wedges the M7, and only a power cycle recovers it.

In the first ~30 s confirm `force pull: drained NN kB` with **NN > 0** (0.0 kB
means wedged), `port live`, and `clock align: src=1/2 shifted +2.19 s`.

### After

```
python operator_sweep_report.py data/raw/campaigns/<key>/sweep_<stamp>   # ALWAYS
python analysis/make_index.py                            # refresh data/raw/INDEX.md
python analysis/plot_drive_trajectory.py --sweep sweep_<stamp>   # the standard figure
python analysis/analyze_raw.py          # add the sweep to CAMPAIGNS first
python analysis/plot_envelope.py ../data/derived/heat_time_map_<campaign>_all.csv
```

## Standing analysis pipeline — raw → table → charts (2026-07-31)

Once a campaign's raw captures exist, **everything downstream is two scripts and
no hand steps.** Re-run them any time; they are deterministic and overwrite in
place, so a fixed bug or a new sweep is one command away from updated outputs.

```
cd analysis
python make_index.py                  # capture folders -> data/raw/INDEX.md
python analyze_raw.py                 # raw captures  -> per-cycle table (all campaigns)
python plot_envelope.py               # per-cycle table -> envelope CSV + charts
python plot_drive_trajectory.py --all # THE STANDARD PER-SWEEP FIGURE, any sweep
python plot_trajectory.py             # per-cycle traces, July campaign only (pinned)
python plot_energy.py                 # -> energy_collapse.html   (interactive)
python plot_selfsensing.py            # -> self_sensing.html      (interactive)
python plot_transition.py             # -> transition_<heat>ms.html (interactive)
python plot_r_bias.py                 # -> r_bias_artifact.html   (interactive)
```

**Every path below is relative to the module root.** The scripts resolve
`../data/raw` and `../data/derived` off their own location, so they work from any
CWD — `cd analysis` is a convenience, not a requirement.

| stage | script | reads | writes |
|---|---|---|---|
| 1 | `analyze_raw.py` | `data/raw/sweep_*/c*_level_*.csv` + `.console.log` + `.meta.json` | `data/raw/sweep_*/cycles.csv`, `data/derived/heat_time_map_<date>_all.csv` |
| 2 | `plot_envelope.py` | `data/derived/heat_time_map_<date>_all.csv` | `data/derived/*_envelope.csv`, `*_stroke.png`, `*_force.png` |
| 2 | `plot_drive_trajectory.py` | raw captures, one or more sweep folders (discovers the grid off filenames — **no table needed**) | `data/derived/drive_<sweep[+stamp…]>_<400ms\|200mA>.png` |
| 2 | `plot_trajectory.py` | per-cycle table + raw captures | `data/derived/trajectory_<heat>ms_<norm>.png` |
| 2 | `plot_energy.py` | per-cycle table + `energy_table.py` | `data/derived/energy_collapse.html` |
| 2 | `plot_selfsensing.py` | per-cycle table + `energy_table.py` | `data/derived/self_sensing.html` |
| 2 | `plot_transition.py` | per-cycle table + raw captures | `data/derived/transition_<heat>ms.html` |
| 2 | `plot_r_bias.py` | per-cycle table + raw captures | `data/derived/r_bias_artifact.html` + `r_bias_points.csv` (cache) |
| — | `make_index.py` | every capture folder + `CAMPAIGNS` | `data/raw/INDEX.md` (which folder holds what) |
| — | `get_cycle.py` | one capture | one cycle's raw time series, on one clock |
| — | `energy_table.py` | per-cycle table + raw captures | `data/derived/*_all_energy.csv` (cached ∫P dt) |
| — | `plot_style.py` | — | shared chart chrome for the HTML figures |

### The standard per-sweep figure — `plot_drive_trajectory.py`

**This is the default way to look at a sweep.** Four channels (resistance /
power / stroke / force) × two time scales (the pulse, and the full cycle
including cooling), series overlaid and colour-ramped, in the bench's units —
stroke in **mm**, force in **grams-force**.

```
python analysis/plot_drive_trajectory.py                       # newest sweep
python analysis/plot_drive_trajectory.py --sweep sweep_<stamp>
python analysis/plot_drive_trajectory.py --sweep sweep_A sweep_B   # MERGE folders
python analysis/plot_drive_trajectory.py --all                 # every sweep folder
```

It reads **raw captures directly** and discovers the `(level, heat)` grid off
the capture filenames, so it needs no per-cycle table, no `CAMPAIGNS` entry, and
no code edit for a new run — unlike `plot_trajectory.py`, which is pinned to the
July long-wire campaign through a hardcoded `SRC_MAP`.

- **`--by auto`** (default) picks the axis the colour ramp runs over and emits
  **both** shapes when a sweep carries both: one figure per pulse length
  coloured by current (`drive_<sweep>_400ms.png`) for a heat-time map, plus one
  figure per current coloured by pulse length (`drive_<sweep>_200mA.png`) for a
  single-current row, which would otherwise be one trace per figure.
- **`--sweep A B ...` merges folders into ONE grid**, which is how the
  *operating envelope* gets read when a campaign was split across sessions.
  2026-08-05 is the case: `sweep_20260805_105318` holds the 250–950 mA ×
  100–400 ms block and `sweep_20260805_154528` holds the **extremes** (a 200 mA
  row, a 500 ms column). Neither spans the envelope alone — the 200 mA row is a
  single current and degenerates to one trace per figure. Merged, every pulse
  length carries the full **200–950 mA** ramp:
  `drive_sweep_20260805_105318+20260805_154528_{100,200,300,400,500}ms.png`.
  The merge is a **union over cells, never over repeats of one cell**: a cell
  present in two folders resolves to the newer one and the drop is printed.
  Cross-session merging is sound here because every channel is referenced to
  **its own cycle's pre-fire baseline** (ΔR/R₀ to that cycle's R₀, stroke and
  force to that cycle's pre-fire mean), so drift between sessions cancels
  instead of showing up as an offset between traces. The subtitle names every
  folder and how many series came from each.
- **Rails are annotated, not hidden.** A channel that touched 0 or 5 V is drawn
  **dotted** and called out in red (`dotted = amp railed at 0/5 V — 750, 850,
  950 mA`), because a flat top there is the amplifier, not the wire.
- **Cycle handling.** A repeated condition is the **median of its non-bootstrap
  cycles** (cycle 1 fires into a fully relaxed wire — a different initial
  condition, held out). A condition that fired **once** — the randomized
  protocol, `cycles: 0` — uses that single pulse and says so in the subtitle;
  there is no repeat to average and the pulse *is* the measurement.
- **No temporal filtering, ever.** Cycles are combined across repeats, never
  smoothed in time. R at the idle bias genuinely carries large per-sample noise
  and a filter would not create information that was not measured.

**`cycles.csv` is the one derived file that stays under `data/raw/`** — it is
per-capture provenance, written next to its source, and `operator_sweep_report.py`
takes that sweep folder as its only argument. Only the merged, cross-campaign
tables land in `data/derived/`.

### The two interactive figures (2026-08-02)

Self-contained HTML — plotly.js is **inlined**, so they open with no internet
and can be emailed as one file (~4.8 MB). Zoom, pan, click-legend to isolate a
pulse length, per-point hover, and a **table view** under each chart. Both read
the same per-cycle table the PNG charts do.

- **`energy_collapse.html`** — *does the wire respond to delivered energy?*
  Stroke and force from all four pulse lengths collapse onto one curve against
  **E = ∫P dt** (R² 0.992); against power alone they do not (R² 0.707). This is
  the measurement behind using **P·t as an RNN input**. Residual panel shows
  what energy leaves unexplained: a monotone +6.9 % (100 ms) → −3.1 % (400 ms)
  ladder — short pulses are slightly more effective per joule.
- **`self_sensing.html`** — *how much can resistance tell you?* R is **linear in
  delivered energy** (−0.379 Ω/J, R² 0.928, no pulse-length dependence) but a
  **non-linear and weaker predictor of stroke** (straight line R² 0.882 vs 0.955
  for the best curve; residual sd 458 µm against energy's 336 µm). R and E carry
  different information — feed the network both.

**Energy is integrated, not `p_hot_W × heat_ms`.** `p_hot_W` is a tail median
(the power at pulse *end*), so the product under-reads delivered energy by 1.7 %
at 100 ms but 4.9 % at 400 ms — a duration-dependent bias in the one chart whose
question is whether duration matters. `energy_table.py` integrates ∫P dt per
cycle and caches it; delete `*_all_energy.csv` to rebuild (~1 min).

**These charts obey the no-selection rule below.** Every cycle is drawn; hollow
marks are rail-limited cycles (lower bounds). The `usable` flag governs only
what the **fits** are computed from — a regression over lower bounds is biased
toward them. The residual panels show fitted cycles only, and say so.

### NO DATA SELECTION — the rule the pipeline is built around

**Every commanded cycle produces exactly one row. Nothing is dropped.** Not
clipped pulses, not railed ones, not sub-threshold ones, not the bootstrap
cycle. The row count is fixed by the **command** (6 rows for a 5+1 condition),
never by what a detector happened to find.

This is deliberate: the table is RNN training data, so deciding which pulses
"count" belongs to the training pipeline. A network that only ever sees clean
pulses cannot learn that saturation and non-response are real machine states.
Quality is therefore reported as **columns**, never applied as a filter:

`bootstrap` · `clipped` · `railed` · `cc_pct` · `n_samples` · `i_low_mA` · `seeded`

The charts follow the same rule — **every cycle is drawn as a dot.** Two
annotations, both non-destructive:

- **hollow marks + dashed segments = at a sensor rail.** There `dx`/`dF` are
  *lower bounds* (the wire moved at least that far), so they must not be read as
  exact. They are still plotted, and the dashed ceiling line shows where the
  instrument ran out — that is what makes those points bend over.
- **the median line holds out only the first cycle**, which is still drawn as a
  dot. It fires into a fully relaxed wire and takes a one-time set (~+370 µm at
  850×400), so it measures a genuinely *different initial condition*; pooling it
  into a central value would blur two populations.

### How heat windows are found — and why thresholding was wrong

Thresholding the current trace silently corrupts low levels, and did. The cool
phase carries a 0.5 V idle bias → ~107 mA whose noise reaches **p95 ~155 mA,
p99.9 ~200 mA**, so a 150–250 mA heat pulse sits *inside* that band; at 100 ms
there are too few samples to separate them. With 30 s cools there are ~2.5×
more noise excursions than the 12–15 s cools of 2026-07-30, so detection
returned **36 phantom cycles at 150×100 against 6 commanded**, and 1–2 at
350/450/550×100 — which made those cells look "missing" from the envelope when
they were merely mis-windowed.

`analyze_raw.py` uses the firmware's own markers instead, in two steps:

1. **Approximate** — the console log carries `[ACT] heat n=k/N` per cycle on the
   host clock, and `[STATUS] … m7_us=` lines pin the host clock to the M7 sample
   clock. A least-squares fit over ~180 STATUS pairs maps each marker onto
   `hw_us`. Individual lines are delayed by USB batching (sd 0.21 s, up to 2 s),
   but the fit averages that out: residual **mean −0.040 s, sd 0.044 s**, worst
   case 0.106 s over 126 cycles.
2. **Refine** — a matched filter: slide a `heat_ms`-wide window ±0.5 s around
   the approximate start and take the position of maximum mean current.

0.1 s of placement error is harmless for a 400 ms pulse and fatal for a 100 ms
one — a window misplaced by its own width measures the *cool baseline*, which is
exactly how 350/450/550 mA commands came back reading 109–113 mA. The marker's
job is only to say which ~1 s of a 30 s cycle to look in; restricting the search
that way cuts false-alarm opportunities ~30×, so a simple max-mean criterion
becomes reliable at low SNR. **This is not selection** — the *number* of cycles
comes from the firmware markers; the filter only locates each one. Recovered
150×100 at 150.4–154.7 mA across all six cycles.

### Conforms to the NN-side data contract

The contract lives in the **NN repo and only there** —
`NN_SelfSensing_Baseline/DATA_COLLECTION_GUIDELINE.md` (sibling checkout;
`../../NN_SelfSensing_Baseline/` from this module). It is deliberately NOT
copied here: it is owned by the consumer, and a duplicate would drift silently
the first time they revise it. Read it at the source.

It specifies what the per-pulse table must carry so the training repo never has
to re-open raw captures. `heat_time_map_20260731_all.csv` now satisfies it —
**32 columns**:

| group | columns |
|---|---|
| identity | `level_mA` `heat_ms` `sweep` `run_type` `cycle` `bootstrap` |
| drive (whole-window) | `i_mA` `cc_pct` `u_V` `R_ohm` |
| **hot state (tail medians)** | `v_hot_V` `i_hot_mA` `r_hot_ohm` `p_hot_W` `r_base_ohm` |
| timing (unwrapped) | `t_heat_start_s` `t_heat_end_s` `t_pulse_utc` |
| response | `dx_um` `x_base_um` `baseline_V` `peak_V` `rise_V` `F_base_mN` `dF_mN` |
| flags + protocol | `clipped` `railed` `detect_ok` `n_samples` `cool_s` `i_low_mA` `seeded` |

**Hot state** is the median over the **last 20% of the heat window, min 20 ms** —
not the whole-window mean. The wire is hottest at pulse end, and on a ramped
pulse the two differ by far more than the ~15% the guideline warned about: the
pre-seed bootstrap at 950×400 reads `i_mA` **402 mA** against `i_hot_mA` **535 mA**.
Both are kept; they answer different questions. `r_hot_ohm`/`p_hot_W` are the
network's inputs, and the raw `v_hot_V`/`i_hot_mA` pair is kept alongside so
they can be re-derived and cross-checked.

`baseline_V`/`peak_V`/`rise_V` (load-cell volts) are **restored** — the guideline
exists because those vanished between 07-30 and 07-31. Never narrow the column
set; add and deprecate by documenting.

`detect_ok` changed meaning: it is now a **consistency** flag (does independent
threshold detection agree with the schedule on the cycle count?), never a
data-loss flag. Windows come from the schedule regardless.

`heat_time_map_20260731_all_meta.json` records the campaign's calibration
constants and the **uncorrected** ~+7% `sma_v`/`sma_i` conversion-duty bias, so
absolute units stay recoverable and a calibration change between campaigns is
detectable rather than silent.

**One deliberate deviation.** The guideline says to write `r_base_ohm` as NaN
when `i_low_mA = 0`, assuming no idle current flows. On this rig that is not
true: with `i_low 0` the cool phase calls `ccRelease()` and parks at the **LDO
floor, which still pushes ~107 mA** — measured 0.4998–0.5009 V / 106.0–107.1 mA
/ 4.671–4.721 Ω across all 24 conditions. So `r_base_ohm` is computed whenever
the idle current exceeds 20 mA, which covers both protocols. Writing NaN there
would discard 144 valid baseline measurements, and the passive baseline is in
fact the *tighter* of the two (median 4.716 Ω vs 4.702 Ω, 1.1 mV of spread vs 5).

**Self-checks run on every build** (guideline §8) and report loudly without ever
dropping a row: window count vs commanded, `i_hot` within 10% of command,
timestamps monotonic after unwrap, `r_hot` inside 1–30 Ω. Current state: all
pass, with 7 cycles flagged on check 2 (the known CC excursions, kept).

### Correctness points baked in (do not re-derive them by hand)

- **`hw_us` time base, never host timestamps** (USB-batched, σ 3.4 ms).
- **32-bit `micros()` unwrap.** `hw_us` rolls over every **4294.97 s (~71.6 min)**
  and a 2 h campaign straddles it. Hit for real: `c20_level_550mA_h400ms` wrapped
  mid-record, the host→M7 fit came out with a *negative* slope, and all six
  cycles returned NaN. Unwrapped **per src** (streams interleave in the file, so
  a global unwrap sees false backward jumps).
- **M4/M7 clock offset** read per capture from `meta.json` (`m4_clock_offset_s`,
  ~2.19 s). src=1/2 are M4-stamped, src=3/4 M7-stamped; untreated, displacement
  appears to peak *before* the current pulse that causes it.
- **Force peaks after the current stops** (thermal + mechanical lag), so the
  response window runs to `heat_end + 1.5 s`.
- **`railed` is computed from the endpoint**, not trusted from an upstream flag.
  At 950×400 the laser sat at 0.0010 V for 1228 samples yet came back unflagged;
  pinned cycles all land on `x_base+dx = 5027.7 µm` to 0.1 µm, and a real
  measurement does not repeat to that precision.
- **`src=5` is not emitted by the CC fork** (it streams 3/4 plus its own 6/7), so
  R is derived as `V/I` — which is also the one quantity immune to the ~+7%
  conversion-duty bias, since both channels scale together.

### Chart style

Pulse length is **ordinal**, so it gets a single-hue light→dark sequential ramp,
not categorical hues. The four steps are validated, not eyeballed — adjacent
OKLab ΔE (×100) for normal / protanope / deuteranope vision:

| pair | normal | protan | deutan |
|---|---|---|---|
| 100→200 ms | 19.1 | 19.2 | 19.1 |
| 200→300 ms | 21.6 | 22.7 | 21.4 |
| 300→400 ms | 15.0 | 15.5 | 15.0 |

against a ≥15 normal floor and ≥8 CVD target, lightness strictly monotonic.
The obvious ColorBrewer pick (`9ecae1/4292c6/2171b5/08306b`) **fails** — its
200→300 pair separates by only 10.0, which is why those two series are hard to
tell apart by eye in the 2026-07-30 chart. Identity is never color-alone: every
series is direct-labeled at its right end *and* in the legend.

**Cool time is not encoded.** The whole campaign ran 30 s, so the marker-shape
split used for the mixed 15/25/30 s data of 2026-07-30 would encode nothing.

## RNN training ranges — plan as of 2026-07-30 (sweep still incomplete)

Target model: inputs **SMA resistance + electrical power**, output
**displacement**; training data = randomly sampled drive conditions via the
explicit-`conditions` profile form; labels = `summary_report.csv` (per-pulse,
`railed`-flagged). What the measured data supports so far:

| knob | range | why |
|---|---|---|
| current | **150–950 mA** (+ a few sub-threshold 110–150 mA samples) | <150 mA: no learnable signal (13–21 µm ≈ noise) and the CC floor is ~106 mA (u_min 0.5 V / 4.7 Ω); 950 mA validated clean at 100 ms; the LDO rails at ~1.1 A — don't sample onto the rail. Sub-threshold samples teach the network that "power in, nothing out" is a real state. |
| pulse width | **100–400 ms**, subject to the energy cap | validated 100 ms fully, 200 ms partially. |
| **energy cap** | start **~1 J** (∝ I²·t); calibrate from the finished sweep | stroke tracks pulse ENERGY to first order (0.39 J → 1.13 mm at 643 mA/200 ms vs 0.42 J → 0.86 mm at 939 mA/100 ms), and the **±5 mm laser window — not the SMA — is the binding stroke ceiling**. Sample (I, t) pairs under the cap, not the full rectangle: the (950 mA, 400 ms) corner is ~1.7 J, 4× anything tested. |
| inter-pulse gap | dataset **A: 12–20 s** (quasi-independent pulses, clean backbone); dataset **B: 2–12 s** (thermal memory — the RNN's reason to exist) | τ_cool ≳ 6 s. With gaps < τ the coil carries state between pulses and **R is the temperature proxy** — the 0.5 V idle bias keeps ~106 mA flowing so R stays observable BETWEEN pulses. Don't go below ~2 s until soak is characterized: a soaked coil stops actuating (2026-07-15) and a ratcheting force baseline is the damage tell. |

Two input-side cautions from the known artifacts: **power carries the +7%
conversion-duty bias** (consistent → the network absorbs it, but don't mix
data across firmware configs that change ADC duty), and the **laser drive
feedthrough (~3.3 µm, synchronous with the pulse)** is correlated noise on the
output exactly when the input is active — negligible at 100+ µm strokes, real
at the threshold region.

## Files

Four buckets, and the boundary between them is the point: **module root** = the
live rig code, **`analysis/`** = the standing pipeline, **`diagnostics/`** = closed
one-off investigations, **`data/`** = split into what the rig wrote and what the
pipeline computed.

At the module root files carry a role prefix: **`operator_`** = a human launches it
directly; **`lib_`** = imported internals, never run on their own. That rule governs
the root only — inside `analysis/` the filename names the pipeline stage instead.

```
Experiment_SMAThermalCharacterization/
├── README.md / STATUS.md / requirements.txt
├── config.yaml                 every instrument + sensor parameter
│
├── operator_console.py         PRIMARY entry — GUI console + --headless (record + control)
├── operator_explore.ipynb      interactive Plotly analysis notebook (raw / converted / cross-plots)
│
├── operator_current_sweep.py   condition sweeps (current × pulse length); --profile / --dry-run
├── operator_sweep_report.py    post-sweep analysis + health report (run after EVERY sweep)
├── operator_profile_queue.py   runs a LIST of profiles back-to-back, unattended (overnight
│                               campaigns): validates all up front, reports after each,
│                               --deadline, per-profile logs + queue_manifest.json
├── operator_pulse_capture.py   plain single-pulse capture tool
├── profiles/                   JSON test profiles (grid + explicit-conditions forms)
│                               night_profiles_*/ = a whole campaign + its generator
│
├── lib_h7_session.py           shared H7 drive/capture plumbing: port revival, calibration
│                               restore, watchdog ping, clock offset, sanity checks
├── lib_config.py               typed dataclasses (h7/stage/phases/calibration/run; no LCR)
├── lib_workers.py              H7Worker (multi-channel), ZaberWorker, CameraWorker
├── lib_h7_commands.py          firmware command builders (single source of truth)
├── lib_recording_core.py       RecordingCore — UI-agnostic continuous recorder/control
├── lib_camera.py               adaptive-FPS camera capture (threaded or spawn subprocess)
├── lib_analysis.py             loaders, calibration, clock-alignment, segmentation,
│                               fitting/filtering — the shared core the notebook imports
│
├── analysis/                   THE STANDING PIPELINE — raw → table → charts
│   ├── analyze_raw.py          stage 1: raw captures -> per-cycle table
│   ├── plot_envelope.py        stage 2: table -> envelope CSV + stroke/force PNGs
│   ├── plot_drive_trajectory.py  stage 2: THE STANDARD PER-SWEEP FIGURE — 4 channels
│   │                             x 2 time scales, ANY sweep, --all for a campaign
│   ├── plot_trajectory.py      stage 2: per-cycle traces, July campaign only (pinned)
│   ├── plot_energy.py          FIGURE A -> energy_collapse.html
│   ├── plot_selfsensing.py     FIGURE B -> self_sensing.html
│   ├── plot_transition.py      FIGURE C -> transition_<heat>ms.html
│   ├── plot_r_bias.py          FIGURE D -> r_bias_artifact.html
│   ├── energy_table.py         imported: per-cycle table + TRUE ∫P dt (not p_hot·t)
│   ├── get_cycle.py            imported: one cycle's raw series, on one clock
│   ├── plot_style.py           imported: shared chart chrome + validated palette
│   └── make_rnn_profile.py     generates ../profiles/rnn_datasetB_<stamp>.json
│
├── diagnostics/                ONE-OFF INVESTIGATIONS — closed, kept for the write-ups
│   ├── operator_noise_psd.py   ┐ 2026-07-28 CC current-sense noise investigation
│   ├── operator_noise_isense.py┘ (conclusions in STATUS — do not re-derive)
│   ├── operator_sweep_adcavg.py  ADC_SAMPLES_CYCLE averaging sweep (rate-vs-noise)
│   └── make_heat_time_map_clean.py  SUPERSEDED 2026-07-30 merge+clean pipeline;
│                               its exclusion rules are what NO DATA SELECTION rejects
│
└── data/
    ├── raw/                    WHAT THE RIG WROTE. Never hand-edited.
    │   ├── console_*  (7)      console sessions
    │   ├── sweep_*    (17)     condition sweeps  (+ their per-sweep cycles.csv)
    │   ├── pulse_*    (6)      single-pulse captures
    │   ├── isense_*   (6)      CC current-sense captures   [diagnostic campaign]
    │   ├── noise_*    (2)      quiet-baseline PSD captures [diagnostic campaign]
    │   └── logs/               sweep console transcripts
    └── derived/                WHAT THE PIPELINE COMPUTED. All regenerable.
        ├── heat_time_map_<date>_all.csv          per-cycle table (the label table)
        ├── heat_time_map_<date>_all_energy.csv   ∫P dt cache
        ├── heat_time_map_<date>_all_envelope.csv per-(level, heat) aggregates
        ├── *_stroke.png / *_force.png / trajectory_*.png
        └── energy_collapse.html / self_sensing.html / transition_*.html /
            r_bias_artifact.html
```

**Everything in `data/derived/` is committed**, HTML figures included, so analysed
results are available on any machine by clone alone. The module's whole ignore set
is machine-local: `.claude/`, `__pycache__/`, and `zaber_config.json` (regenerated
per host on every `ZaberStage.connect()`).

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
