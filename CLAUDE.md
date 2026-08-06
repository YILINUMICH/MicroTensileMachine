# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A benchtop micro tensile rig for characterizing Shape Memory Alloy (SMA / Flexinol) coils under Joule-heating actuation. It captures **force, displacement, electrical impedance, and stage position** as time-aligned streams (joined on a single host `time.time()` clock) so mechanical and electrical behaviour can be correlated offline. University of Michigan — Robotics, HDR Lab.

The system is split into **firmware** (PlatformIO, Arduino Portenta H7 dual-core + TI ADS1263 ADC EVM) and **host-side Python** (recorder + calibration + instrument drivers). The root `README.md` is the canonical project map — read it first; it carries the live module status table, the finalized ADC↔sensor mapping, the hardware BOM, and the cross-cutting TODO list.

## Critical conventions

- **`Archieve/` is read-only and excluded from project understanding** (the misspelling is intentional and preserved to avoid breaking path references). Do not read or edit it when reasoning about the live project — it holds superseded modules kept only for git-blame. Anything under it is **Archived**; never propose changes there.
- **One master `README.md` per module** + a short `STATUS.md` status header. Do not scatter Plan/Memo/Notes files. Per-module TODOs live in each `STATUS.md`; cross-cutting TODOs live in the root README's **Cross-cutting TODO** section.
- Every module carries a **status label**: Stable / WIP / To-Test / Diagnostic / Archived. "To-Test" means it builds but has not been bench-verified end-to-end — treat its output skeptically.
- Datasheets and operator memos live under `docs/` only. No per-module duplicates.
- **Session data under `*/data/` IS tracked on purpose** — committed so results are available on every machine. Do not propose gitignoring captures or analysed results. Only machine-local files are ignored: `__pycache__/`, `.venv/`, `.pio/`, `zaber_config.json`.
- **Raw captures stay separate from derived results** where a module's `data/` has outgrown a flat folder: `data/raw/` = what the instrument wrote (never hand-edited), `data/derived/` = what analysis computed, and analysis code lives *outside* `data/`. Currently only `Experiment_SMAThermalCharacterization/` is organized this way; V3, SpringSmokeTest and both `Calibrate_*` still have flat `data/` folders and that is fine — convert one only when it starts mixing code, captures, and outputs.

## Firmware (PlatformIO)

The Portenta H7 has two cores. **The M4 has no direct USB**, so its serial output is bridged through the M7 over Arduino RPC. All live firmware ships **two images compiled from the same `src/main.cpp`** (an `#ifdef CORE_CMx` guard selects per-core code) — flash the M7 image, then the M4 image:

```
pio run -e portenta_m7 -t upload          # flash the M7 bridge/controller
pio run -e portenta_m4 -t upload          # flash the M4 sampler
pio device monitor                        # 115200 baud; shows M4 output via M7
```

The live firmware projects (**env names vary — check each `platformio.ini`**):
- **`Firmware_SMASensorHub_PIO/`** — **production firmware.** M4 dual-ADC sensing **and** M7 SMA drive control on one H7 (the Phase-6 merge of the former `Firmware_SensorHub_PIO` + `Firmware_SMADriver_PIO`). Envs `portenta_m7` / `portenta_m4`; `portenta_m7_legacy100` is a rollback build.
- **`Firmware_SMAConstantCurrent_PIO/`** — **To-Test** fork of the production firmware adding a **closed-loop constant-current** controller on M7 (`cc`/`ccfire`/`cccycle`, `tau`, `src=6/7` telemetry). Port of `GelBot/PIConstantCurrent/CONTROL_SKELETON.md`, validated on an Uno with the same driver board. Develop CC work **here, not in `Firmware_SMASensorHub_PIO/`**.
- **`Firmware_SMARateTest_PIO/`** — **Diagnostic** fork used to raise the SMA stream rate (15 → 99 Hz, then a 1 kHz push). Carries a `portenta_m7_rate*` env ladder + `portenta_m7_cyc*` builds.
- **`Firmware_stable/`** — a frozen known-good snapshot of the pre-merge dual-ADC firmware, kept for A/B diagnosis (envs `portenta_m7_bridge` + `portenta_m4`).

**Two hardware gotchas baked into the rig:**
- **Power-cycle the rig (USB + EVM supply) after every upload.** The DFU reset does not cleanly re-power the EVM's analog rails; skip the cycle and the ADS1263 comes up with `ID=0x00`.
- **COM8 = Portenta H7, COM5 = Zaber stage.** `upload_port`/`monitor_port` are pinned to COM8 in every `platformio.ini` because PIO would otherwise auto-pick the Zaber and wedge the stage. On a different host, run `pio device list` and either edit the `.ini` or override on the CLI (`--upload-port COMx`), which wins over the file. **Port history — both numbers mean the same H7 role:** COM8 until 2026-07-24; COM13 from 2026-07-24 while the replacement board was in service; COM8 again from 2026-07-27, when the rig switched back to the original board. Logs, STATUS entries, and session data naming either port are consistent with this.

### M4 → M7 sample transport (`sample_ring.h`)

The production data path is a **lock-free SPSC ring buffer in SRAM4** (`Firmware_SMASensorHub_PIO/src/sample_ring.h`, shared by copy into the `Firmware_SMARateTest_PIO/` and `Calibrate_*` projects). M4 (producer, DRDY-driven) pushes `AdcSample`s without blocking; M7 (consumer) drains and formats for USB-CDC. This replaced a synchronous `RPC.print()` path that crashed under sustained dual-ADC throughput (~660 msg/s). RPC is retained for boot-time checkpoints only.

- `AdcSample` is a **fixed 24-byte struct** (`static_assert`-enforced) at a **fixed SRAM4 address** (`RING_BASE = 0x38008000`, clear of the OpenAMP/RPC region). If you change the struct layout or ring header, the host-side serial parser must change in lockstep.
- The **`src` ID column** identifies each stream and is a coordination point between firmware and host parser: `1`=laser displacement (ADC1), `2`=load cell force (ADC2), `3/4/5`=SMA voltage/current/resistance (now streamed by `Firmware_SMASensorHub_PIO/`), `6/7`=CC command `u` / `R_est` (**`Firmware_SMAConstantCurrent_PIO/` only** — these fill the last reserved slots, so `seq_per_src[8]` is now exactly full), `0xF0+`=state-machine events. See the reservation table at the top of `sample_ring.h` before adding channels.

### The ADS1263 driver is copied, not shared

`lib/ADS1263/ADS1263_Driver.{h,cpp}` is **manually copied** between firmware projects (the canonical/live copy is in `Firmware_SMASensorHub_PIO/`). The copies carry Mid-Carrier pin defines (PA_8 CS, PC_6 DRDY, PC_7 RESET) and non-obvious bug fixes (RDATA2 6-byte frame, ADC2CFG REF2/GAIN2 field order). **If you fix the driver in one project, propagate the fix to every sibling copy** — there is no shared library target.

### Production ADC↔sensor mapping (finalized 2026-05-28)

- **ADC1 → AIN4/AIN5 → Keyence IL-030 laser** (displacement), 400 SPS, Sinc3.
- **ADC2 → AIN2/AIN3 → LCA-9PC load cell** (force), 400 SPS, Sinc3.
- External REF7050 (+5 V) on AIN0/AIN1 shared by both ADCs.

Note the historical inversion: some firmware comments describe an older "ADC1=load / ADC2=laser" assignment. The finalized mapping above (and the root README) is authoritative; the swap is a To-Test item pending one bench re-verify.

## Host-side Python

No repo-wide environment — each module has its own `requirements.txt` (`pip install -r requirements.txt` per module). Common stack: `pyserial`, `pyvisa` (LCR), `zaber-motion` (stage), `numpy`/`matplotlib`, `pyyaml`. `pyvisa` needs a working backend (Keysight IO Suite's IVI on Windows; `pyvisa-py` on Linux).

Module config is a per-module `config.yaml` (recorder/calibration) loaded into a typed dataclass.

Common entry points:
```
python Experiment_SMACharacterizationV3/sma_console.py      # SMA characterization console (primary; --headless for scripted runs)
python Experiment_SMACharacterizationV3/analyze_sma.py --session data/<id>   # offline de-embed + plot

# SMA THERMAL module — capture, then the standing analysis pipeline
python Experiment_SMAThermalCharacterization/operator_console.py       # primary entry (--headless for scripted runs)
python Experiment_SMAThermalCharacterization/operator_current_sweep.py --dry-run --profile profiles/<p>.json
python Experiment_SMAThermalCharacterization/operator_sweep_report.py data/raw/sweep_<stamp>   # after EVERY sweep
cd Experiment_SMAThermalCharacterization/analysis && python analyze_raw.py && python plot_envelope.py
#   (the analysis/ scripts resolve ../data/raw and ../data/derived off __file__, so any CWD works)

python Calibrate_LaserHead/run_calibration.py     # laser calibration sweep (Zaber)
python Calibrate_LoadCell/run_calibration.py      # load-cell dead-weight calibration
python Driver_KeysightLCR/test_lcr_meter.py --quick      # LCR connection smoke test
python <module>/portenta_reader.py                # H7 serial-stream sanity check
```

### `Experiment_SMAThermalCharacterization/` — four-bucket layout (reorganized 2026-08-03)

A fork of V3 adding an SMA-thermal focus (adaptive-FPS camera, LCR removed). **The bucket a file sits in tells you how to treat it** — check this before proposing changes:

```
module root/     LIVE RIG CODE. Role prefixes apply HERE ONLY:
                 operator_ = run directly (operator_console.py PRIMARY entry,
                 operator_explore.ipynb, operator_current_sweep.py,
                 operator_sweep_report.py, operator_pulse_capture.py)
                 lib_      = imported internals, never run alone (7 files)
analysis/        THE STANDING PIPELINE. No prefixes — the filename names the
                 stage. analyze_raw.py (stage 1) -> plot_envelope.py /
                 plot_energy.py / plot_selfsensing.py / plot_transition.py /
                 plot_r_bias.py / plot_trajectory.py (stage 2);
                 energy_table.py / get_cycle.py / plot_style.py are imported.
diagnostics/     CLOSED one-off investigations, kept for their write-ups.
                 Do NOT extend these or re-derive their conclusions — read the
                 STATUS entry instead. make_heat_time_map_clean.py here is
                 SUPERSEDED and its exclusion rules are what the live pipeline
                 deliberately rejects (see NO DATA SELECTION below).
data/raw/        What the RIG wrote. Never hand-edit. Capture folders keep
                 their original names (console_* sweep_* pulse_* isense_* noise_*)
                 but are GROUPED (2026-08-06): campaigns/<date>_<wire>_<cold-len>/
                 holds the captures, plus writeups/ aborted/ troubleshoot/ logs/.
                 INDEX.md (generated by analysis/make_index.py) says which
                 folder holds what; README.md documents the capture format.
                 A sweep is identified by its BARE FOLDER NAME everywhere -- the
                 merged table's `sweep` column, CAMPAIGNS, --sweep on the
                 plotters -- and analyze_raw.resolve_sweep() maps name -> path,
                 so refiling a folder rewrites no table. data/raw/ itself is an
                 INBOX: a run without --campaign lands loose and the index
                 flags it as unfiled.
data/derived/    What the PIPELINE computed. Regenerable, but committed so
                 analysed results travel with a clone. MIRRORS data/raw's
                 campaign grouping (2026-08-06) and shares the folder name via
                 the `dir` field of CAMPAIGNS, so a campaign's captures and its
                 results cannot drift apart: data/derived/campaigns/<dir>/.
                 See data/derived/README.md for what writes each output --
                 including which ones are still pinned to the July campaign
                 through energy_table.py.
```

**Path constants, not CWD.** Every script in `analysis/` and `diagnostics/` that touches data resolves `RAW` and/or `DERIVED` off its own `__file__` (`../data/raw`, `../data/derived`), so they run from any working directory. (`plot_style.py` touches no paths; `make_rnn_profile.py` writes `../profiles`.) If you add a script there, follow that pattern — do not use bare relative paths.

**`cycles.csv` is the one derived file that stays under `data/raw/`**, next to the capture it came from: it is per-capture provenance and `operator_sweep_report.py` takes that sweep folder as its only argument. Only merged, cross-campaign tables go to `data/derived/`.

**NO DATA SELECTION.** `analyze_raw.py` emits exactly one row per commanded cycle — nothing dropped, thresholded, or averaged away. Quality is reported as *columns* (`clipped` / `railed` / `cc_pct` / `bootstrap`). This table is RNN training data; deciding which pulses "count" belongs to the training pipeline, not here. Do not add filtering to the pipeline.

**Five raw writers** decide where new captures land — keep them pointed at `data/raw/`: `config.yaml` `run.output_dir`, `lib_config.RunConfig.output_dir`, `operator_current_sweep.py`, `operator_pulse_capture.py`, and the two diagnostics' `--outdir`. `lib_analysis.latest_session()` globs `data/raw/console_*` to auto-pick a session for the notebook.

The former CLI plotters (`analyze_sma.py`/`sma_plots.py`) and legacy recorder (`sma_recorder.py`/`session.py`/`operator_io.py`/`run_experiment.py`) were removed here (they remain in V3 and git history).

### Recorder architecture (`Experiment_SMACharacterizationV3/`)

The current entry point is `sma_console.py` — a GUI console driving a **continuously-logging session** (`recording_core.py`). Daemon worker threads (`LcrWorker`, `H7Worker` in `workers.py`) run for the whole session, pushing sample dataclasses into bounded `queue.Queue`s; the main-thread controller is the **sole CSV writer**, draining the queues at ~20 Hz. Workers are phase-oblivious, so one LCR + H7 session records continuously without reconnecting instruments. `--headless` runs the same `RecordingCore` with no GUI. The older OPEN → SHORT → RAW recorder (`sma_recorder.py`, backed by `session.py`/`operator_io.py`) is still present but superseded by the console.

Cross-module imports use a `sys.path` shim (e.g. the recorder imports the LCR driver from `Driver_KeysightLCR/lcr_meter.py` and the H7 reader from `Calibrate_LaserHead/portenta_reader.py`) rather than packaging — there is no single local copy of those drivers.

### Calibration constants flow into analysis

`Calibrate_LaserHead/` fits `V = k·µm + V₀` and `Calibrate_LoadCell/` fits force↔voltage; the resulting `calibration.json` constants feed the SMA analyzer's displacement/force conversion. **Legacy constants (`k = -0.1171 mV/µm`, `V₀ = 566.957 mV`) came through a now-removed Waveshare HAT ~4.4× attenuator and are invalid on the bare EVM** — wiring the current calibration JSONs into the recorder (`Experiment_SMACharacterizationV3/`) is an open TODO.
