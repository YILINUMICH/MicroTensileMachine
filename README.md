# MicroTensileMachine

A benchtop micro tensile rig for characterizing **Shape Memory Alloy (SMA / Flexinol) coils** under Joule-heating actuation. The rig captures **force**, **displacement**, **electrical impedance**, and **stage position** as time-aligned streams (joined on a single host `time.time()` clock) so that mechanical and electrical behaviour can be correlated post-hoc.

> **University of Michigan — Robotics, HDR Lab**
> Author: Yilin Ma
> Last major update: 2026-06-21 — README reconciled with the on-disk tree: added `Experiment_LDOCharacterization/`, `Driver_SiglentOscilloscope/`, `Driver_RedPitaya/`, `Experiment_SpringSmokeTest/`; modules regrouped by role (driver / calibration / test); `doc/` → `docs/`.
> 2026-06-17 — LDO **dynamic** performance (settling / overshoot / ripple) bench-verified via `Experiment_LDOCharacterization/`. Absolute-voltage accuracy deferred (see TODO).
> 2026-06-01 — added `Firmware_SMADriver_PIO/` (Phase 6 SMA drive-path bring-up: MCP4728 DAC → TPS7A57 LDO).
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

The SMA Joule-heating supply is the **MCP4728 DAC → TPS7A57 LDO → MOSFET** drive path (see [`Firmware_SMADriver_PIO/`](Firmware_SMADriver_PIO/README.md)); its dynamic response was characterized with the Siglent scope (see [`Experiment_LDOCharacterization/`](Experiment_LDOCharacterization/README.md)). The whole machine produces, for one experiment: per-sample voltage streams from the ADCs (force + displacement), per-measurement impedance from the LCR (Ls + Rs), and per-command stage position — all timestamped on the same host clock so that they join cleanly during analysis.

---

## Production sensor mapping (finalized 2026-05-28)

After the dual-ADC cross-compare runs in `Calibrate_LaserHead/` and `Calibrate_LoadCell/` settled per-channel noise and linearity, the production ADC↔sensor mapping is:

| Path | AIN pair | Sensor                     | SPS     | Gain | Filter | Resolution | Cable |
|------|----------|----------------------------|---------|------|--------|------------|-------|
| **ADC1** | **AIN4 (+) / AIN5 (−)** | **Keyence IL-030 (laser)** | **400 SPS** | PGA in path, gain = 1 | Sinc3 | 32-bit | Cable 4 |
| **ADC2** | **AIN2 (+) / AIN3 (−)** | **Load cell (LCA-9PC)**    | **400 SPS** | gain = 1 (PGA in path) | Sinc3 | 24-bit | Cable 3 |

External REF7050 (+5.000 V) on AIN0(+)/AIN1(-) is shared by both ADCs (REFMUX=0x09, REF2=001b). All wiring in [`docs/MEMO_cable_map.md`](docs/MEMO_cable_map.md).

The SensorHub firmware uses a **shared-SRAM ring buffer (SRAM4)** for M4→M7 sample transport, replacing the synchronous per-sample `RPC.print()` path that was crashing under sustained dual-ADC throughput (~660 msg/s). See [`Firmware_SensorHub_PIO/src/sample_ring.h`](Firmware_SensorHub_PIO/src/sample_ring.h).

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
| [`Firmware_SensorHub_PIO/`](Firmware_SensorHub_PIO/STATUS.md) | **To-Test** (post-2026-05-28 swap + ring-buffer port) | **Production sensing firmware.** Dual-ADC stream — laser on **ADC1/AIN4-AIN5**, load cell on **ADC2/AIN2-AIN3** — single serial stream with `src` column. Ring-buffer IPC. Needs one bench re-verify run with the swapped pairing before flipping to Stable. |
| [`Firmware_SMADriver_PIO/`](Firmware_SMADriver_PIO/README.md) | **To-Test** (drive path + **dynamics verified** 2026-06-17; absolute-V trim deferred) → **superseded by the merge below** | **Phase 6 SMA drive-path firmware.** M7-only: I2C → MCP4728 DAC → TPS7A57 LDO (DAC-margining via a 6.2 kΩ REF-pin resistor) → MOSFET-gated SMA load; INA296A current sense; `fire` command emits a scope-trigger edge. Settling/overshoot/ripple confirmed via `Experiment_LDOCharacterization/`. Kept as a single-purpose reference build; its M7 control code now lives merged in `Firmware_SMASensorHub_PIO/`. Still to do: trim `vdd`/`offset` against a meter for absolute accuracy. |
| [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/STATUS.md) | **To-Test** (code-complete + reviewed 2026-06-21; bench-verify pending) | **Combined sensing + SMA drive firmware (the merge).** M4 = dual-ADC sampler (unchanged from SensorHub); M7 = ring→USB bridge **and** the SMA controller, restructured as a non-blocking state machine so the sensor stream keeps flowing during `drive`/`fire` (and an `abort` can interrupt a live op). One USB port, three line classes: untagged sensor TSV, `[STATUS]`, `[SMA]`. Pins don't overlap (M4 SPI vs M7 I2C/analog/GPIO). INA296A current sense (A1) enabled. |
| [`Calibrate_LaserHead/Calibrate_LaserHead_PIO/`](Calibrate_LaserHead/) | **Stable** | Calibration firmware: dual-ADC cross-compare on AIN4/AIN5 (laser). Ring-buffer IPC. |
| [`Calibrate_LoadCell/Calibrate_Loadcell_PIO/`](Calibrate_LoadCell/) | **Stable** | Calibration firmware: dual-ADC cross-compare on AIN2/AIN3 (load cell). Ring-buffer IPC. |

### Instrument drivers (host-side Python)

| Folder | Status | Purpose |
|---|---|---|
| [`Driver_ZaberStage/`](Driver_ZaberStage/STATUS.md) | **Stable** | Linear-stage control wrapper. Auto-discovery, JSON config, 100 Hz position reads, safety limits. v1.0 (Nov 2025). COM5. |
| [`Driver_KeysightLCR/`](Driver_KeysightLCR/STATUS.md) | **Stable** | E4980A/AL LCR meter wrapper. USB or LAN VISA, optimized for max read rate. The single LCR driver used by the recorder. |
| [`Driver_SiglentOscilloscope/`](Driver_SiglentOscilloscope/STATUS.md) | **Stable** (bench-verified 2026-06-15, 10/10 tests) | SDS2000X Plus wrapper over a raw SCPI socket (`:5025`, no VISA). Mirrors the `Driver_KeysightLCR` API so it drops into the same worker pattern. Used by `Experiment_LDOCharacterization/`; a `ScopeWorker` for `Experiment_SMACharacterizationV2/` is still TODO. |
| [`Driver_RedPitaya/`](Driver_RedPitaya/README.md) | **Draft / To-Test** (not yet HW-validated) | Thin SCPI driver for the STEMlab 125-14: generate a sine + capture two raw phase-coherent waveforms. Intended future replacement for the bench LCR (signal role on the board, R/L math on the host). Validate against the E4980 before production use. |

### Calibration tools (host-side Python)

| Folder | Status | Purpose |
|---|---|---|
| [`Calibrate_LaserHead/`](Calibrate_LaserHead/) | **Stable** | Walks the Zaber through a fixed displacement sweep, captures laser voltage at each point, fits `V = k·µm + V₀`. The resulting `k`/`V₀` feed the SMA analyzer. Dual-ADC cross-compare pipeline. |
| [`Calibrate_LoadCell/`](Calibrate_LoadCell/) | **Stable** | Applies known weights, captures the LCA-9PC output via ADC1+ADC2 cross-compare, fits force↔voltage. |

### Characterization / experiments (host-side Python)

| Folder | Status | Purpose |
|---|---|---|
| [`Experiment_SMACharacterizationV2/`](Experiment_SMACharacterizationV2/STATUS.md) | **To-Test** (refactor pending bench-verify) | **The SMA recorder.** Single-session OPEN → SHORT → RAW state machine with worker threads. Produces per-phase CSVs + `meta.json` for the offline analyzer. Uses `Driver_KeysightLCR/` as the single LCR driver. Needs the new `Calibrate_*` constants wired in. |
| [`Experiment_LDOCharacterization/`](Experiment_LDOCharacterization/STATUS.md) | **To-Test** — **dynamics verified** (2026-06-17), absolute-V deferred | Time-domain characterization of the MCP4728→TPS7A57 LDO drive path using the Siglent scope. **Settling time, overshoot, 10–90% rise, and ripple are bench-verified** across loaded/unloaded × small/mid/large steps (10 runs, 6/16–6/17). **Absolute voltage** from `capture_waveform()` is *not* trusted yet — `codes_per_div = 25.0` is still unverified against the Programming Guide (settle *time* is unaffected; overshoot % needs it). One-off debug scripts live in [`Experiment_LDOCharacterization/diag/`](Experiment_LDOCharacterization/diag/). |
| [`Experiment_SpringSmokeTest/`](Experiment_SpringSmokeTest/README.md) | **Diagnostic** (Phase 5 integration test; ran 2026-05-31) | Spring-as-SMA-surrogate joint test: validates the **laser-displacement and load-cell** channels against each other and Hooke's-law ground truth while stressing the M4→ring→M7→USB pipeline at 1 kSPS. A bring-up/integration test, not part of the production recording flow. |

### Archive — `Archieve/` (read-only, **excluded from project understanding**)

`Archieve/` contains fully superseded modules. Do not read or modify these when reasoning about the live project; they're kept only for git-blame / historical reference.

| Folder | Notes |
|---|---|
| `Archieve/ADS1263/` | Arduino-IDE era ADS1263 test sketches plus the original `ADS1263_H7_Integration_Notes.md`. Driver lineage that fed `Firmware_SensorHub_PIO/lib/ADS1263/`. Retired 2026-05-28. |
| `Archieve/LaserHead_PIO/` | Single-path reference build (laser only). Superseded by `Firmware_SensorHub_PIO/`. |
| `Archieve/LoadCell_PIO/` | Single-path reference build (load cell only). Superseded by `Firmware_SensorHub_PIO/`. |
| `Archieve/SMA_Characterization/` | v1 SMA recorder. Superseded by `Experiment_SMACharacterizationV2/`. |
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
| SMA drive DAC | Microchip MCP4728 (12-bit, I2C 0x60) — sets the LDO REF node via a 6.2 kΩ margining resistor, see [`Firmware_SMADriver_PIO/`](Firmware_SMADriver_PIO/README.md) | (vendor site) |
| SMA drive LDO | TI TPS7A5701 (TPS7A57) — programmable LDO (IREF·RREF, unity-gain), Joule-heats the SMA coil through a MOSFET | [docs/tps7a57.pdf](docs/tps7a57.pdf) |
| SMA current sense | TI INA296A (10 V/V) + 100 mΩ shunt = 1 V/A on H7 A1 | (vendor site) |

See [docs/README.md](docs/README.md) for the per-PDF index and the operator memos.

---

## Quick map: where do I go for X?

| If you want to… | Go to |
|---|---|
| Run an SMA characterization experiment | [`Experiment_SMACharacterizationV2/`](Experiment_SMACharacterizationV2/) — `python sma_recorder.py` |
| Re-calibrate the laser head before a run | [`Calibrate_LaserHead/`](Calibrate_LaserHead/) — `python run_calibration.py` |
| Re-calibrate the load cell before a run | [`Calibrate_LoadCell/`](Calibrate_LoadCell/) — `python run_calibration.py` |
| Flash/modify the production sensing firmware | [`Firmware_SensorHub_PIO/`](Firmware_SensorHub_PIO/) |
| Drive / actuate the SMA coil (`arm`, then `drive <V> <ms>` / `fire <V>` / `cycle …`) | [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/) (combined w/ sensing) — or [`Firmware_SMADriver_PIO/`](Firmware_SMADriver_PIO/) (SMA-only reference) |
| Run sensing **and** SMA drive together on one H7 | [`Firmware_SMASensorHub_PIO/`](Firmware_SMASensorHub_PIO/) |
| Characterize the LDO dynamic response (settling/ripple) | [`Experiment_LDOCharacterization/`](Experiment_LDOCharacterization/) — `python run_experiment.py` |
| Validate laser + load cell together against a spring | [`Experiment_SpringSmokeTest/`](Experiment_SpringSmokeTest/) |
| Control the Zaber stage from Python | [`Driver_ZaberStage/`](Driver_ZaberStage/) |
| Talk to the LCR meter from Python | [`Driver_KeysightLCR/`](Driver_KeysightLCR/) |
| Talk to the Siglent scope from Python | [`Driver_SiglentOscilloscope/`](Driver_SiglentOscilloscope/) |
| Look up an ADS1263 register or known issue | `Firmware_SensorHub_PIO/lib/ADS1263/ADS1263_Driver.cpp` (live driver). Historical notes in `Archieve/ADS1263/ADS1263_H7_Integration_Notes.md`. |

---

## Top-level conventions

- **One master README per module** (not scattered Plan/Memo/Status files). The root README + each module's `README.md` is the canonical source; per-module `STATUS.md` is a short status header.
- **Datasheets and operator memos** live under [`docs/`](docs/) only. No sub-module duplicates — a module's own `doc/` folder holds only module-development notes, never copies of the datasheets. **Routing rule:** for a *system-level* question check the root `docs/`; for a *module-level* question check that module's own `doc/`.
- **`Archieve/` is read-only and excluded from project understanding.** The misspelling is intentional and preserved to avoid breaking path references. It holds fully superseded modules kept only for git-blame — never read, edit, or propose changes there.
- **Every module carries a status label** (Stable / WIP / To-Test / Diagnostic / Archived — see the legend above). "To-Test" means it builds but has not been bench-verified end-to-end; treat its output skeptically.
- **Per-module environment, not repo-wide.** Each host-side module has its own `requirements.txt` (`pip install -r requirements.txt`). Module configuration is a per-module `config.yaml` loaded into a typed dataclass. Common stack: `pyserial`, `pyvisa` (LCR), `zaber-motion` (stage), `numpy`/`matplotlib`, `pyyaml`.

### Firmware conventions (PlatformIO, Portenta H7)

- The H7 has two cores and **the M4 has no direct USB**, so M4 serial output is bridged through the M7 over Arduino RPC. Most firmware ships **two images compiled from the same `src/main.cpp`**, with an `#ifdef CORE_CMx` guard selecting per-core code. (`Firmware_SMADriver_PIO/` is the exception — M7-only, no M4/RPC/ring.)
- **Power-cycle the rig (USB + EVM supply) after every upload.** The DFU reset does not cleanly re-power the EVM's analog rails; skip the cycle and the ADS1263 comes up with `ID=0x00`.
- **COM8 = Portenta H7, COM5 = Zaber stage.** `upload_port`/`monitor_port` are pinned to COM8 in every `platformio.ini` so PIO doesn't auto-pick the Zaber and wedge the stage. On a different host run `pio device list` and edit the `.ini` or override with `--upload-port COMx`.
- **The ADS1263 driver (`lib/ADS1263/`) is manually copied, not shared.** The canonical/live copy is in `Firmware_SensorHub_PIO/`. If you fix the driver in one project, propagate the fix to every sibling copy — there is no shared library target.
- **M4→M7 sample transport is a lock-free SPSC ring buffer in SRAM4** (`sample_ring.h`). `AdcSample` is a fixed 24-byte struct at a fixed SRAM4 address; the `src` ID column identifies each stream (1=laser, 2=load cell, 3/4/5=SMA V/I/R [deferred], 0xF0+=state-machine events). Change the struct layout and the host serial parser must change in lockstep.

---

## Build & run quickstart

**Firmware (dual-core projects):**
```
pio run                                   # build default env (portenta_m4)
pio run -e portenta_m7_bridge -t upload   # flash the M7 bridge ONCE
pio run -e portenta_m4        -t upload   # flash the M4 application
pio device monitor                        # 115200 baud; M4 output via M7
# Power-cycle the rig after every upload.
```

**Firmware (`Firmware_SMADriver_PIO/`, M7-only):**
```
pio run -e portenta_m4_idle -t upload     # wipe leftover M4 image (then power-cycle)
pio run -e portenta_m7      -t upload
```

**Host-side Python (common entry points):**
```
python Experiment_SMACharacterizationV2/sma_recorder.py            # run an SMA characterization session
python Experiment_SMACharacterizationV2/analyze_sma.py --session data/<id>   # offline de-embed + plot
python Calibrate_LaserHead/run_calibration.py                      # laser calibration sweep (Zaber)
python Calibrate_LoadCell/run_calibration.py                       # load-cell dead-weight calibration
python Driver_KeysightLCR/test_lcr_meter.py --quick                # LCR connection smoke test
python <module>/portenta_reader.py                                 # H7 serial-stream sanity check
```

---

## Cross-cutting TODO

Module-local TODOs live in each module's `STATUS.md`; the items below span more than one module.

- **Wire the current calibration JSONs into the recorder.** `Experiment_SMACharacterizationV2/session.py` still carries the legacy constants (`k = -0.1171 mV/µm`, `V₀ = 566.957 mV`) that came through a now-removed Waveshare HAT ~4.4× attenuator and are **invalid on the bare EVM**. Feed `Calibrate_LaserHead/` and `Calibrate_LoadCell/` `calibration.json` outputs in instead.
- **Bench re-verify the finalized ADC↔sensor mapping** (ADC1=laser/AIN4-5, ADC2=load/AIN2-3) before flipping `Firmware_SensorHub_PIO/` to Stable. Some firmware comments still describe the older "ADC1=load / ADC2=laser" assignment — the swap is the one remaining To-Test item.
- **Bench-verify `Firmware_SMASensorHub_PIO/` end-to-end** (the combined sensing + SMA-drive merge is code-complete/reviewed but not yet run on hardware).
- **Add a `ScopeWorker` for `Experiment_SMACharacterizationV2/`** so the Siglent scope drops into the same worker pattern as the LCR + H7.
- **Trim the SMA LDO for absolute-voltage accuracy** — calibrate `vdd`/`offset` against a meter (dynamics are already verified; absolute V deferred).
- **Verify `codes_per_div = 25.0`** in `Experiment_LDOCharacterization/` against the scope Programming Guide before trusting absolute voltage / overshoot %.
- **Validate the `Driver_RedPitaya/` SCPI driver against the E4980** before using it as an LCR replacement.
- **Wire the host parser to `src = 3/4/5`** — the firmware now streams SMA V/I/R (stamped on the M4 clock) and reports `crc_err`/`overrun`/`m7_us`/`m4_us`/`vdd`/`offset`/`aref` in `[STATUS]`; `portenta_reader.py`/the recorder need to consume them (the M7 SMA state machine was also consolidated onto one `arm`-gated heat/cool engine — see `Firmware_SMASensorHub_PIO/`).
- **Fill in the operator memos** under `docs/` (`MEMO_cable_map.md` partial; `MEMO_carrier_config.md`, `MEMO_sensor_setup.md`, `MEMO_bias_tee.md`, `MEMO_lcr_setup.md` are empty placeholders).
- **Formalize the module taxonomy** — the role buckets (firmware / driver / calibration / experiment) are newer than some folder names; reconcile naming.
