# MicroTensileMachine

A benchtop micro tensile rig for characterizing **Shape Memory Alloy (SMA / Flexinol) coils** under Joule-heating actuation. The rig captures **force**, **displacement**, **electrical impedance**, and **stage position** as time-aligned streams (joined on a single host `time.time()` clock) so that mechanical and electrical behaviour can be correlated post-hoc.

> **University of Michigan — Robotics, HDR Lab**
> Author: Yilin Ma
> Last major update: 2026-07-14 — README reconciled with the on-disk tree: the sensing + SMA-drive firmware merged into `Firmware_SMASensorHub_PIO/` (former `Firmware_SensorHub_PIO/` + `Firmware_SMADriver_PIO/` retired); added `Firmware_SMARateTest_PIO/` (SMA stream-rate diagnostic), `Firmware_stable/` (frozen baseline), `Experiment_SMACharacterizationV3/`, and `Experiment_SMAThermalCharacterization/`; the SMA recorder moved V2 → V3; `TODO.md` stub removed.
> 2026-07-13 — SMA stream rate raised 15 → 99 Hz (batched M7 USB-CDC writes); a 1 kHz push is built in `Firmware_SMARateTest_PIO/` and ported to `Firmware_SMASensorHub_PIO/`, pending a bench run.
> 2026-06-17 — LDO **dynamic** performance (settling / overshoot / ripple) bench-verified via `Experiment_LDOCharacterization/`. Absolute-voltage accuracy deferred (see TODO).
> 2026-05-28 — ADC↔sensor mapping finalized, SensorHub ring-buffer IPC ported, `ADS1263/` retired to `Archieve/`.

---

## What the rig looks like (block view)

```
                              ┌───────────────────────────────────┐
                              │            Host PC                │
                              │  (Python: recorder + analyzers)   │
                              └───┬──────────┬──────────┬─────────┘
                                  │ USB-CDC  │ USB/VISA │ USB
                                  ▼          ▼          ▼
            ┌──────────────────────────┐  ┌──────────┐ ┌──────────────┐
            │   Portenta H7 (ABX00042) │  │ Keysight │ │ Zaber linear │
            │   + Mid Carrier (ASX00055)│  │ E4980AL  │ │   stage      │
            │   + ADS1263 24/32-bit ADC│  │ LCR meter│ │(X-LSQ300A-E01)│
            └──┬──────────────────┬────┘  └────┬─────┘ └──────────────┘
        ADC1  │ AIN4/AIN5         │ AIN2/AIN3  │
       (laser)▼            (load) ▼            ▼
      ┌──────────────┐    ┌──────────────┐  ┌──────────────────┐
      │ Keyence      │    │ Load cell    │  │ Bias-tee         │
      │ IL-030 laser │    │  + LCA-9PC   │  │ (0.22 µF + 47 µH)│
      │ displacement │    │    amplifier │  │       │          │
      └──────┬───────┘    └──────┬───────┘  │       ▼          │
             │                   │           │   SMA coil DUT   │
             ▼                   ▼           │   (Flexinol)     │
        ── Disp. ──         ── Force ──     │       ▲          │
                                            │       │          │
                                            │  MCP4728 DAC →   │
                                            │  TPS7A57 LDO →   │
                                            │  MOSFET          │
                                            │  (Joule-heating) │
                                            └──────────────────┘
```

The SMA Joule-heating supply is the **MCP4728 DAC → TPS7A57 LDO → MOSFET** drive path (now merged into [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/STATUS.md)); its dynamic response was characterized with the Siglent scope (see [`Experiment_LDOCharacterization/`](Experiment_LDOCharacterization/)). The whole machine produces, for one experiment: per-sample voltage streams from the ADCs (force + displacement), per-measurement impedance from the LCR (Ls + Rs), and per-command stage position — all timestamped on the same host clock so that they join cleanly during analysis.

---

## Production sensor mapping (finalized 2026-05-28)

After the dual-ADC cross-compare runs in `Calibrate_LaserHead/` and `Calibrate_LoadCell/` settled per-channel noise and linearity, the production ADC↔sensor mapping is:

| Path | AIN pair | Sensor                     | SPS     | Gain | Filter | Resolution | Cable |
|------|----------|----------------------------|---------|------|--------|------------|-------|
| **ADC1** | **AIN4 (+) / AIN5 (−)** | **Keyence IL-030 (laser)** | **400 SPS** | PGA in path, gain = 1 | Sinc3 | 32-bit | Cable 4 |
| **ADC2** | **AIN2 (+) / AIN3 (−)** | **Load cell (LCA-9PC)**    | **400 SPS** | gain = 1 (PGA in path) | Sinc3 | 24-bit | Cable 3 |

External REF7050 (+5.000 V) on AIN0(+)/AIN1(-) is shared by both ADCs (REFMUX=0x09, REF2=001b). All wiring in [`docs/MEMO_cable_map.md`](docs/MEMO_cable_map.md).

The firmware uses a **shared-SRAM ring buffer (SRAM4)** for M4→M7 sample transport, replacing the synchronous per-sample `RPC.print()` path that was crashing under sustained dual-ADC throughput (~660 msg/s). See [`Firmware_SMASensorHub_PIO/src/sample_ring.h`](Firmware_SMASensorHub_PIO/src/sample_ring.h).

---

## Status legend

Every module below carries a status. Same vocabulary in every README and in the per-module `STATUS.md`:

| Label | Meaning |
|---|---|
| **Stable** | Production-bound; bench-verified end-to-end; safe to use as-is. |
| **WIP** | Work In Progress — actively being built or modified. Expect things to change. |
| **To-Test** | Code/build exists but has **not** been bench-verified end-to-end yet. Treat output skeptically. |
| **Diagnostic** | Kept for debugging, bring-up, or as a single-purpose reference build. Not part of the production flow. |
| **Archived** | Frozen. Lives under [`Archieve/`](Archieve/). **Do not edit. Excluded from project understanding.** |

---

## How the modules are organized

The tree mixes several *kinds* of module. Read them in these buckets (this grouping is newer than some folder names — see the "formalize the taxonomy" TODO):

- **Firmware** — PlatformIO projects that run on the Portenta H7.
- **Instrument drivers** — thin host-side Python wrappers for one piece of bench hardware each (stage, LCR, scope, Red Pitaya). No experiment logic.
- **Calibration tools** — turn a known physical input into sensor constants (`calibration.json`) consumed downstream.
- **Characterization / experiments** — orchestrate hardware + drivers to record or characterize the system (the SMA recorder, the LDO dynamic study, the spring integration test).

### Firmware (PlatformIO, Portenta H7 + TI ADS1263 EVM)

| Folder | Status | Purpose |
|---|---|---|
| [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/STATUS.md) | **To-Test** (code-complete + reviewed; 1 kHz SMA stream ported 2026-07-13; bench-verify pending) | **Production firmware — the Phase-6 merge** of the former `Firmware_SensorHub_PIO` (sensing) + `Firmware_SMADriver_PIO` (SMA drive). M4 = dual-ADC sampler (laser on **ADC1/AIN4-AIN5**, load on **ADC2/AIN2-AIN3**); M7 = ring→USB bridge **and** the SMA controller, a non-blocking state machine so the sensor stream keeps flowing during `drive`/`fire` (`abort` interrupts a live op). One USB port, three line classes: untagged sensor TSV, `[STATUS]`, `[SMA]`. INA296A current sense (A1). Carries the ~1 kHz SMA stream ported from the rate-test fork. |
| [`Firmware_SMAConstantCurrent_PIO/`](Firmware_SMAConstantCurrent_PIO/STATUS.md) | **To-Test** (all envs build clean; never flashed) | **Constant-current SMA drive.** Fork of the SensorHub adding a closed-loop **current** controller to the M7 drive path (`cc` / `ccfire` / `cccycle` alongside the inherited voltage-mode `drive`/`fire`/`cycle`, sharing one actuation engine and all its safety). Port of the Uno-validated design in `GelBot/PIConstantCurrent/CONTROL_SKELETON.md` — feedforward `u=I·R` + auto-gain `Ki=R/τ`, with `R` measured live in the **command domain**. Adds `src=6` (command `u`) / `src=7` (`R_est`) to the stream and an **open-load auto-disarm**. Holding current (not voltage) makes the Joule heat repeatable as the wire's resistance moves. |
| [`Firmware_SMARateTest_PIO/`](Firmware_SMARateTest_PIO/STATUS.md) | **Diagnostic** | Throwaway fork of the SensorHub built to answer one question: why the SMA V/I/R stream captured only ~2 points per 100 ms fire. **Round 1 (SOLVED):** the bottleneck was tiny per-call USB-CDC writes, not the ADC — a batched `Serial.write()` took the SMA stream 15 → 99 Hz. **Round 2:** a `portenta_m7_rate*` env ladder pushing toward 1 kHz (built, not yet bench-run). The winning fixes are ported to `Firmware_SMASensorHub_PIO/`. |
| [`Firmware_stable/`](Firmware_stable/STABLE_NOTE.md) | **Diagnostic** (frozen baseline) | Exact snapshot of the pre-merge dual-ADC `SensorHub` firmware (commit `09f0502`), kept for A/B diagnosis — e.g. confirming whether the laser tracked before the merge. Do not develop here. |
| [`Calibrate_LaserHead/Calibrate_LaserHead_PIO/`](Calibrate_LaserHead/) | **Stable** | Calibration firmware: dual-ADC cross-compare on AIN4/AIN5 (laser). Ring-buffer IPC. |
| [`Calibrate_LoadCell/Calibrate_Loadcell_PIO/`](Calibrate_LoadCell/) | **Stable** | Calibration firmware: dual-ADC cross-compare on AIN2/AIN3 (load cell). Ring-buffer IPC. |

### Instrument drivers (host-side Python)

| Folder | Status | Purpose |
|---|---|---|
| [`Driver_ZaberStage/`](Driver_ZaberStage/STATUS.md) | **Stable** | Linear-stage control wrapper. Auto-discovery, JSON config, 100 Hz position reads, safety limits. v1.0 (Nov 2025). COM5. |
| [`Driver_KeysightLCR/`](Driver_KeysightLCR/STATUS.md) | **Stable** | E4980A/AL LCR meter wrapper. USB or LAN VISA, optimized for max read rate. The single LCR driver used by the recorder. |
| [`Driver_SiglentOscilloscope/`](Driver_SiglentOscilloscope/STATUS.md) | **Stable** (bench-verified 2026-06-15, 10/10 tests) | SDS2000X Plus wrapper over a raw SCPI socket (`:5025`, no VISA). Mirrors the `Driver_KeysightLCR` API so it drops into the same worker pattern. Used by `Experiment_LDOCharacterization/`; a `ScopeWorker` for `Experiment_SMACharacterizationV3/` is still TODO. |
| [`Driver_RedPitaya/`](Driver_RedPitaya/README.md) | **Draft / To-Test** (not yet HW-validated) | Thin SCPI driver for the STEMlab 125-14: generate a sine + capture two raw phase-coherent waveforms. Intended future replacement for the bench LCR (signal role on the board, R/L math on the host). Validate against the E4980 before production use. |

### Calibration tools (host-side Python)

| Folder | Status | Purpose |
|---|---|---|
| [`Calibrate_LaserHead/`](Calibrate_LaserHead/) | **Stable** | Walks the Zaber through a fixed displacement sweep, captures laser voltage at each point, fits `V = k·µm + V₀`. The resulting `k`/`V₀` feed the SMA analyzer. Dual-ADC cross-compare pipeline. |
| [`Calibrate_LoadCell/`](Calibrate_LoadCell/) | **Stable** | Applies known weights, captures the LCA-9PC output via ADC1+ADC2 cross-compare, fits force↔voltage. |

### Characterization / experiments (host-side Python)

| Folder | Status | Purpose |
|---|---|---|
| [`Experiment_SMACharacterizationV3/`](Experiment_SMACharacterizationV3/STATUS.md) | **WIP / To-Test** (imports + offline analyzer + offscreen GUI + headless flow verified on synthetic data; not yet bench-run) | **The SMA recorder/console.** `sma_console.py` drives a continuously-logging session (live plots, startup health-check, always-available `DISARM`); `--headless` for scripted runs. Records raw LCR + H7 (sensors **and** SMA, src=1–5) + Zaber stage → CSVs + `meta.json` for the offline analyzer. Uses `Driver_KeysightLCR/` + the combined `Firmware_SMASensorHub_PIO/`. Needs the new `Calibrate_*` constants wired in. |
| [`Experiment_SMAThermalCharacterization/`](Experiment_SMAThermalCharacterization/STATUS.md) | **WIP / To-Test** (fork of V3; not yet bench-run) | **SMA thermal characterization console** — a fork of V3 focused on correlating SMA temperature with the Joule-heating drive. Adds an adaptive-FPS camera + live preview; **LCR removed** from this module. Planned: a temperature stream (thermocouple / IR). |
| [`Experiment_LDOCharacterization/`](Experiment_LDOCharacterization/) | **To-Test** — **dynamics verified** (2026-06-17), absolute-V deferred | Time-domain characterization of the MCP4728→TPS7A57 LDO drive path using the Siglent scope. **Settling time, overshoot, 10–90% rise, and ripple are bench-verified** across loaded/unloaded × small/mid/large steps (10 runs, 6/16–6/17). **Absolute voltage** from `capture_waveform()` was blocked on `codes_per_div`; **this is now MEASURED as 30.0, not 25.0** (`Experiment_RNoise`, 2026-07-21, cross-checked against the scope's own `PAVA? MEAN`) — the old value reads **20% high**. Re-derive any absolute volts / overshoot % with 30.0; settle *time* is unaffected. |
| [`Experiment_RNoise/`](Experiment_RNoise/STATUS.md) | **WIP** (PHASE 2 bench session 2026-07-21; root cause not yet confirmed) | **Why `R = V/I` self-sensing is noisy**, and which of three causes with opposite fixes is responsible. Deployed-rate analysis (the H7 already streams `sma_v`/`sma_i` at **~980 Hz**, not 99 Hz) + scope coherence/PSD at the ADC pins. **Found ~158 mV rms of ripple on the TPS7A57 output vs a 2.45 µV spec**, spread 12–400 kHz — essentially all above the deployed 490 Hz Nyquist, so it aliases in (**Case C**). Supply swap and MOSFET-PWM eliminated; leading suspect is insufficient *effective* COUT. Ships an interim 10 Hz low-pass (5.8× on σ_R) with a documented silent-failure mode. |
| [`Experiment_SpringSmokeTest/`](Experiment_SpringSmokeTest/README.md) | **Diagnostic** (Phase 5 integration test; ran 2026-05-31) | Spring-as-SMA-surrogate joint test: validates the **laser-displacement and load-cell** channels against each other and Hooke's-law ground truth while stressing the M4→ring→M7→USB pipeline at 1 kSPS. A bring-up/integration test, not part of the production recording flow. |

### Archive — `Archieve/` (read-only, **excluded from project understanding**)

`Archieve/` contains fully superseded modules. Do not read or modify these when reasoning about the live project; they're kept only for git-blame / historical reference.

| Folder | Notes |
|---|---|
| `Archieve/ADS1263/` | Arduino-IDE era ADS1263 test sketches plus the original `ADS1263_H7_Integration_Notes.md`. Driver lineage that fed the live `Firmware_SMASensorHub_PIO/lib/ADS1263/`. Retired 2026-05-28. |
| `Archieve/LaserHead_PIO/` | Single-path reference build (laser only). Superseded by the SensorHub firmware (now merged into `Firmware_SMASensorHub_PIO/`). |
| `Archieve/LoadCell_PIO/` | Single-path reference build (load cell only). Superseded by the SensorHub firmware (now merged into `Firmware_SMASensorHub_PIO/`). |
| `Archieve/SMA_Characterization/` | v1 SMA recorder. Superseded by `Experiment_SMACharacterizationV3/`. |
| `Archieve/AD2/` | Digilent Analog Discovery 2 substitute interface used during the H7 down-time. |

---

## Hardware bill of materials

| Subsystem | Part | Datasheet |
|---|---|---|
| MCU | Arduino Portenta H7 (ABX00042) | [docs/PortentaH7_ABX00042_Pinout.pdf](docs/PortentaH7_ABX00042_Pinout.pdf) |
| Carrier | Arduino Portenta Mid Carrier (ASX00055) | [docs/PortentaMidCarrier_ASX00055_Pinout.pdf](docs/PortentaMidCarrier_ASX00055_Pinout.pdf) |
| ADC board | **TI ADS1263 EVM** (32-bit ADC1 + 24-bit ADC2) — connected to the Mid Carrier via a 6-wire SPI cable, see [docs/MEMO_cable_map.md](docs/MEMO_cable_map.md) | [docs/ADS1263_Datasheet.pdf](docs/ADS1263_Datasheet.pdf), [docs/ADS1263_EVM_User_Guide.pdf](docs/ADS1263_EVM_User_Guide.pdf) |
| Voltage reference | **TI REF7050** (5.000 V precision reference) — feeds the ADS1263's external reference inputs on AIN0 / AIN1, see [docs/MEMO_cable_map.md](docs/MEMO_cable_map.md) Cable 2 | (vendor site) |
| Displacement sensor | Keyence IL-030 laser, 30 mm reference, ±5 mm range — wired to AIN4/AIN5 | [docs/KeyenceIL_LaserSensor_Manual.pdf](docs/KeyenceIL_LaserSensor_Manual.pdf) |
| Load cell amplifier | LCA-9PC / LCA-RTC — output wired to AIN2/AIN3 | [docs/LCA9PC_LCARTC_LoadCellAmp_Manual.pdf](docs/LCA9PC_LCARTC_LoadCellAmp_Manual.pdf) |
| LCR meter | Keysight E4980AL | (vendor site) |
| Oscilloscope | Siglent SDS2000X Plus (SDS2204X Plus on the bench) — LDO dynamic capture | [Driver_SiglentOscilloscope/](Driver_SiglentOscilloscope/) |
| Impedance analyzer (future) | Red Pitaya STEMlab 125-14 — intended LCR replacement | [Driver_RedPitaya/](Driver_RedPitaya/) |
| Linear stage | Zaber X-LSQ300A-E01 (300 mm travel, encoder, serial 143153) | (vendor site) |
| Bias-tee | Double bias-tee, 0.22 µF C0G + 47 µH | (custom) |
| SMA drive DAC | Microchip MCP4728 (12-bit, I2C 0x60) — sets the LDO REF node via a 6.2 kΩ margining resistor, see [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/STATUS.md) | (vendor site) |
| SMA drive LDO | TI TPS7A5701 (TPS7A57) — programmable LDO (IREF·RREF, unity-gain), Joule-heats the SMA coil through a MOSFET | [docs/tps7a57.pdf](docs/tps7a57.pdf) |
| SMA current sense | TI INA296A (10 V/V) + 100 mΩ shunt = 1 V/A on H7 A1 | (vendor site) |

See [docs/README.md](docs/README.md) for the per-PDF index and the operator memos.

---

## Quick map: where do I go for X?

| If you want to… | Go to |
|---|---|
| Run an SMA characterization experiment | [`Experiment_SMACharacterizationV3/`](Experiment_SMACharacterizationV3/) — `python sma_console.py` |
| Run an SMA **thermal** characterization experiment | [`Experiment_SMAThermalCharacterization/`](Experiment_SMAThermalCharacterization/) — `python operator_console.py` (analysis: `operator_explore.ipynb`) |
| Re-calibrate the laser head before a run | [`Calibrate_LaserHead/`](Calibrate_LaserHead/) — `python run_calibration.py` |
| Re-calibrate the load cell before a run | [`Calibrate_LoadCell/`](Calibrate_LoadCell/) — `python run_calibration.py` |
| Flash/modify the production firmware (sensing **and** SMA drive) | [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/) |
| Drive / actuate the SMA coil (`arm`, then `drive <V> <ms>` / `fire <V>` / `cycle …`) | [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/) |
| Actuate at **constant current** (`arm`, then `cc <mA>` / `ccfire` / `cccycle …`) | [`Firmware_SMAConstantCurrent_PIO/`](Firmware_SMAConstantCurrent_PIO/) |
| Push the SMA stream rate higher / A-B a firmware regression | [`Firmware_SMARateTest_PIO/`](Firmware_SMARateTest_PIO/) (rate diagnostic) · [`Firmware_stable/`](Firmware_stable/) (frozen baseline) |
| Characterize the LDO dynamic response (settling/ripple) | [`Experiment_LDOCharacterization/`](Experiment_LDOCharacterization/) — `python run_experiment.py` |
| Diagnose why `R = V/I` self-sensing is noisy | [`Experiment_RNoise/`](Experiment_RNoise/) — `python analyze_r_noise.py <session>` (offline) · `python capture_phase2.py --drive 0.85 --autorange` (bench) |
| Validate laser + load cell together against a spring | [`Experiment_SpringSmokeTest/`](Experiment_SpringSmokeTest/) |
| Control the Zaber stage from Python | [`Driver_ZaberStage/`](Driver_ZaberStage/) |
| Talk to the LCR meter from Python | [`Driver_KeysightLCR/`](Driver_KeysightLCR/) |
| Talk to the Siglent scope from Python | [`Driver_SiglentOscilloscope/`](Driver_SiglentOscilloscope/) |
| Look up an ADS1263 register or known issue | `Firmware_SMASensorHub_PIO/lib/ADS1263/ADS1263_Driver.cpp` (live driver). Historical notes in `Archieve/ADS1263/ADS1263_H7_Integration_Notes.md`. |

---

## Top-level conventions

- **One master README per module** (not scattered Plan/Memo/Status files). The root README + each module's `README.md` is the canonical source; per-module `STATUS.md` is a short status header.
- **Datasheets and operator memos** live under [`docs/`](docs/) only. No sub-module duplicates — a module's own `doc/` folder holds only module-development notes, never copies of the datasheets. **Routing rule:** for a *system-level* question check the root `docs/`; for a *module-level* question check that module's own `doc/`.
- **`Archieve/` is read-only and excluded from project understanding.** The misspelling is intentional and preserved to avoid breaking path references. It holds fully superseded modules kept only for git-blame — never read, edit, or propose changes there.
- **Every module carries a status label** (Stable / WIP / To-Test / Diagnostic / Archived — see the legend above). "To-Test" means it builds but has not been bench-verified end-to-end; treat its output skeptically.
- **Per-module environment, not repo-wide.** Each host-side module has its own `requirements.txt` (`pip install -r requirements.txt`). Module configuration is a per-module `config.yaml` loaded into a typed dataclass. Common stack: `pyserial`, `pyvisa` (LCR), `zaber-motion` (stage), `numpy`/`matplotlib`, `pyyaml`.

### Firmware conventions (PlatformIO, Portenta H7)

- The H7 has two cores and **the M4 has no direct USB**, so M4 serial output is bridged through the M7 over Arduino RPC. All live firmware ships **two images compiled from the same `src/main.cpp`**, with an `#ifdef CORE_CMx` guard selecting per-core code.
- **Power-cycle the rig (USB + EVM supply) after every upload.** The DFU reset does not cleanly re-power the EVM's analog rails; skip the cycle and the ADS1263 comes up with `ID=0x00`.
- **COM8 = Portenta H7, COM5 = Zaber stage.** `upload_port`/`monitor_port` are pinned to COM8 in every `platformio.ini` so PIO doesn't auto-pick the Zaber and wedge the stage. On a different host run `pio device list` and edit the `.ini` or override with `--upload-port COMx`.
- **The ADS1263 driver (`lib/ADS1263/`) is manually copied, not shared.** The canonical/live copy is in `Firmware_SMASensorHub_PIO/`. If you fix the driver in one project, propagate the fix to every sibling copy (`Firmware_SMARateTest_PIO/`, the `Calibrate_*` builds) — there is no shared library target.
- **M4→M7 sample transport is a lock-free SPSC ring buffer in SRAM4** (`sample_ring.h`). `AdcSample` is a fixed 24-byte struct at a fixed SRAM4 address; the `src` ID column identifies each stream (1=laser, 2=load cell, 3/4/5=SMA V/I/R, 0xF0+=state-machine events). Change the struct layout and the host serial parser must change in lockstep.

---

## Build & run quickstart

**Firmware — production (`Firmware_SMASensorHub_PIO/`; env names vary per project, check each `platformio.ini`):**
```
pio run -e portenta_m7 -t upload          # flash the M7 bridge/controller
pio run -e portenta_m4 -t upload          # flash the M4 sampler
pio device monitor                        # 115200 baud; M4 output via M7
# Power-cycle the rig (USB + EVM supply) after every upload.
```

**Host-side Python (common entry points):**
```
python Experiment_SMACharacterizationV3/sma_console.py            # run an SMA characterization session (--headless for scripted)
python Experiment_SMACharacterizationV3/analyze_sma.py --session data/<id>   # offline de-embed + plot
python Calibrate_LaserHead/run_calibration.py                      # laser calibration sweep (Zaber)
python Calibrate_LoadCell/run_calibration.py                       # load-cell dead-weight calibration
python Driver_KeysightLCR/test_lcr_meter.py --quick                # LCR connection smoke test
python <module>/portenta_reader.py                                 # H7 serial-stream sanity check
```

---

## Cross-cutting TODO

Module-local TODOs live in each module's `STATUS.md`; the items below span more than one module.

- **Wire the current calibration JSONs into the recorder.** The recorder (`Experiment_SMACharacterizationV3/`, `session.py`) still carries the legacy constants (`k = -0.1171 mV/µm`, `V₀ = 566.957 mV`) that came through a now-removed Waveshare HAT ~4.4× attenuator and are **invalid on the bare EVM**. Feed `Calibrate_LaserHead/` and `Calibrate_LoadCell/` `calibration.json` outputs in instead.
- **Bench-verify `Firmware_SMASensorHub_PIO/` end-to-end** (the combined sensing + SMA-drive merge is code-complete/reviewed but not yet run on hardware). This also settles the last **ADC↔sensor mapping** re-verify (ADC1=laser/AIN4-5, ADC2=load/AIN2-3) — some firmware comments still describe the older "ADC1=load / ADC2=laser" assignment.
- **Bench-run the ~1 kHz SMA stream.** The rate fix (15 → 99 Hz) is verified; the 1 kHz push is built in `Firmware_SMARateTest_PIO/` and ported into `Firmware_SMASensorHub_PIO/` but **never flashed** — walk the rate ladder / the 4 gates (cadence → no drops → V/I means → idle telemetry), then confirm the recorder resolves the fire transient within a single cycle.
- **Add a `ScopeWorker` for `Experiment_SMACharacterizationV3/`** so the Siglent scope drops into the same worker pattern as the LCR + H7.
- **Trim the SMA LDO for absolute-voltage accuracy** — calibrate `vdd`/`offset` against a meter (dynamics are already verified; absolute V deferred). Related: the H7 on-chip ADC reads V/I ~7% high in proportion to conversion duty (resistance R = V/I is immune).
- **Verify `codes_per_div = 25.0`** in `Experiment_LDOCharacterization/` against the scope Programming Guide before trusting absolute voltage / overshoot %.
- **Validate the `Driver_RedPitaya/` SCPI driver against the E4980** before using it as an LCR replacement.
- **Fix the H7 sensor over-read** — the stream is read at ~493 Hz while the ADS1263 converts at 400 SPS, so ~19% of `h7.csv` rows are zero-order-hold duplicates. Read on DRDY / drop the repeat at the source, or decimate host-side on `diff(raw_code)==0`.
- **Fill in the operator memos** under `docs/` (`MEMO_cable_map.md` partial; `MEMO_carrier_config.md`, `MEMO_sensor_setup.md`, `MEMO_bias_tee.md`, `MEMO_lcr_setup.md` are empty placeholders).
- **Formalize the module taxonomy** — the role buckets (firmware / driver / calibration / experiment) are newer than some folder names; reconcile naming.
