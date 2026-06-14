# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A benchtop micro tensile rig for characterizing Shape Memory Alloy (SMA / Flexinol) coils under Joule-heating actuation. It captures **force, displacement, electrical impedance, and stage position** as time-aligned streams (joined on a single host `time.time()` clock) so mechanical and electrical behaviour can be correlated offline. University of Michigan — Robotics, HDR Lab.

The system is split into **firmware** (PlatformIO, Arduino Portenta H7 dual-core + TI ADS1263 ADC EVM) and **host-side Python** (recorder + calibration + instrument drivers). The root `README.md` is the canonical project map — read it first; it carries the live module status table, the finalized ADC↔sensor mapping, the hardware BOM, and the cross-cutting TODO list.

## Critical conventions

- **`Archieve/` is read-only and excluded from project understanding** (the misspelling is intentional and preserved to avoid breaking path references). Do not read or edit it when reasoning about the live project — it holds superseded modules kept only for git-blame. Anything under it is **Archived**; never propose changes there.
- **One master `README.md` per module** + a short `STATUS.md` status header. Do not scatter Plan/Memo/Notes files. Per-module TODOs live in each `STATUS.md`; cross-cutting TODOs live in the root README. (`TODO.md` is a deprecated stub — its content moved into the root README.)
- Every module carries a **status label**: Stable / WIP / To-Test / Diagnostic / Archived. "To-Test" means it builds but has not been bench-verified end-to-end — treat its output skeptically.
- Datasheets and operator memos live under `doc/` only. No per-module duplicates.

## Firmware (PlatformIO)

The Portenta H7 has two cores. **The M4 has no direct USB**, so its serial output is bridged through the M7 over Arduino RPC. Most firmware projects therefore ship **two images compiled from the same `src/main.cpp`** (an `#ifdef CORE_CMx` guard selects per-core code):

```
pio run                                   # build default env (portenta_m4)
pio run -e portenta_m7_bridge -t upload   # flash the M7 bridge ONCE
pio run -e portenta_m4        -t upload   # flash the M4 application
pio device monitor                        # 115200 baud; shows M4 output via M7
```

`SMA_Driver_PIO/` is the exception — **M7-only** (no M4, no RPC, no ring), so it's just `pio run -t upload`. It ships a `portenta_m4_idle` env that flashes a do-nothing M4 image to wipe leftover M4 firmware before running an M7-only sketch:
```
pio run -e portenta_m4_idle -t upload     # wipe M4 (then power-cycle)
pio run -e portenta_m7      -t upload
```

**Two hardware gotchas baked into the rig:**
- **Power-cycle the rig (USB + EVM supply) after every upload.** The DFU reset does not cleanly re-power the EVM's analog rails; skip the cycle and the ADS1263 comes up with `ID=0x00`.
- **COM8 = Portenta H7, COM5 = Zaber stage.** `upload_port`/`monitor_port` are pinned to COM8 in every `platformio.ini` because PIO would otherwise auto-pick the Zaber and wedge the stage. On a different host, run `pio device list` and either edit the `.ini` or override on the CLI (`--upload-port COMx`), which wins over the file.

### M4 → M7 sample transport (`sample_ring.h`)

The production data path is a **lock-free SPSC ring buffer in SRAM4** (`SensorHub_PIO/src/sample_ring.h`, shared by copy into the `Calibrate_*` projects). M4 (producer, DRDY-driven) pushes `AdcSample`s without blocking; M7 (consumer) drains and formats for USB-CDC. This replaced a synchronous `RPC.print()` path that crashed under sustained dual-ADC throughput (~660 msg/s). RPC is retained for boot-time checkpoints only.

- `AdcSample` is a **fixed 24-byte struct** (`static_assert`-enforced) at a **fixed SRAM4 address** (`RING_BASE = 0x38008000`, clear of the OpenAMP/RPC region). If you change the struct layout or ring header, the host-side serial parser must change in lockstep.
- The **`src` ID column** identifies each stream and is a coordination point between firmware and host parser: `1`=laser displacement (ADC1), `2`=load cell force (ADC2), `3/4/5`=SMA voltage/current/resistance (Phase 6), `0xF0+`=state-machine events. See the reservation table at the top of `sample_ring.h` before adding channels.

### The ADS1263 driver is copied, not shared

`lib/ADS1263/ADS1263_Driver.{h,cpp}` is **manually copied** between firmware projects (the canonical/live copy is in `SensorHub_PIO/`). The copies carry Mid-Carrier pin defines (PA_8 CS, PC_6 DRDY, PC_7 RESET) and non-obvious bug fixes (RDATA2 6-byte frame, ADC2CFG REF2/GAIN2 field order). **If you fix the driver in one project, propagate the fix to every sibling copy** — there is no shared library target.

### Production ADC↔sensor mapping (finalized 2026-05-28)

- **ADC1 → AIN4/AIN5 → Keyence IL-030 laser** (displacement), 400 SPS, Sinc3.
- **ADC2 → AIN2/AIN3 → LCA-9PC load cell** (force), 400 SPS, Sinc3.
- External REF7050 (+5 V) on AIN0/AIN1 shared by both ADCs.

Note the historical inversion: some firmware comments (and `SensorHub_PIO/platformio.ini`) describe an older "ADC1=load / ADC2=laser" assignment. The finalized mapping above (and the root README) is authoritative; the swap is a To-Test item pending one bench re-verify.

## Host-side Python

No repo-wide environment — each module has its own `requirements.txt` (`pip install -r requirements.txt` per module). Common stack: `pyserial`, `pyvisa` (LCR), `zaber-motion` (stage), `numpy`/`matplotlib`, `pyyaml`. `pyvisa` needs a working backend (Keysight IO Suite's IVI on Windows; `pyvisa-py` on Linux).

Module config is a per-module `config.yaml` (recorder/calibration) loaded into a typed dataclass.

Common entry points:
```
python SMA_CharacterizationV2/sma_recorder.py     # run an SMA characterization session
python SMA_CharacterizationV2/analyze_sma.py --session data/<id>   # offline de-embed + plot
python Calibrate_LaserHead/run_calibration.py     # laser calibration sweep (Zaber)
python Calibrate_LoadCell/run_calibration.py      # load-cell dead-weight calibration
python KeysightLCR/test_lcr_meter.py --quick      # LCR connection smoke test
python <module>/portenta_reader.py                # H7 serial-stream sanity check
```

### Recorder architecture (`SMA_CharacterizationV2/`)

Two daemon worker threads (`LcrWorker`, `H7Worker` in `workers.py`) run continuously for the whole session, pushing sample dataclasses into bounded `queue.Queue`s. The main-thread `SessionController` (`session.py`) is a state machine and the **sole CSV writer**: it drains both queues at ~20 Hz and swaps the active output file at phase boundaries. Workers are phase-oblivious — that decoupling is what lets a single LCR + H7 session span all three phases (OPEN → SHORT → RAW) without reconnecting instruments. One run produces per-phase CSVs + `meta.json` in a timestamped `data/sma_<timestamp>/` directory.

Cross-module imports use a `sys.path` shim (e.g. the recorder imports the LCR driver from `KeysightLCR/lcr_meter.py` and the H7 reader from `Calibrate_LaserHead/portenta_reader.py`) rather than packaging — there is no single local copy of those drivers.

### Calibration constants flow into analysis

`Calibrate_LaserHead/` fits `V = k·µm + V₀` and `Calibrate_LoadCell/` fits force↔voltage; the resulting `calibration.json` constants feed the SMA analyzer's displacement/force conversion. **Legacy constants (`k = -0.1171 mV/µm`, `V₀ = 566.957 mV`) came through a now-removed Waveshare HAT ~4.4× attenuator and are invalid on the bare EVM** — wiring the current calibration JSONs into `SMA_CharacterizationV2/session.py` is an open TODO.
