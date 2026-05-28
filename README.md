# MicroTensileMachine

A benchtop micro tensile rig for characterizing **Shape Memory Alloy (SMA / Flexinol) coils** under Joule-heating actuation. The rig captures **force**, **displacement**, **electrical impedance**, and **stage position** as time-aligned streams so that mechanical and electrical behaviour can be correlated post-hoc.

> **University of Michigan — Robotics, HDR Lab**
> Author: Yilin Ma
> Last major update: 2026-05-28 — ADC↔sensor mapping finalized, SensorHub ring-buffer IPC ported, `ADS1263/` retired to `Archieve/`.

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
                                            │  DC supply       │
                                            │  (Joule-heating) │
                                            └──────────────────┘
```

The whole machine produces, for one experiment: per-sample voltage streams from the ADCs (force + displacement), per-measurement impedance from the LCR (Ls + Rs), and per-command stage position — all timestamped on the same host clock so that they join cleanly during analysis.

---

## Production sensor mapping (finalized 2026-05-28)

After the dual-ADC cross-compare runs in `Calibrate_LaserHead/` and `Calibrate_LoadCell/` settled per-channel noise and linearity, the production ADC↔sensor mapping is:

| Path | AIN pair | Sensor                     | SPS     | Gain | Filter | Resolution | Cable |
|------|----------|----------------------------|---------|------|--------|------------|-------|
| **ADC1** | **AIN4 (+) / AIN5 (−)** | **Keyence IL-030 (laser)** | **400 SPS** | PGA in path, gain = 1 | Sinc3 | 32-bit | Cable 4 |
| **ADC2** | **AIN2 (+) / AIN3 (−)** | **Load cell (LCA-9PC)**    | **400 SPS** | gain = 1 (PGA in path) | Sinc3 | 24-bit | Cable 3 |

External REF7050 (+5.000 V) on AIN0(+)/AIN1(-) is shared by both ADCs (REFMUX=0x09, REF2=001b). All wiring in [`doc/MEMO_cable_map.md`](doc/MEMO_cable_map.md).

The SensorHub firmware was switched to a **shared-SRAM ring buffer (SRAM4)** for M4→M7 sample transport on the same date, replacing the synchronous per-sample `RPC.print()` path that was crashing under sustained dual-ADC throughput (~660 msg/s). See [`SensorHub_PIO/src/sample_ring.h`](SensorHub_PIO/src/sample_ring.h).

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

## Modules at a glance

### Firmware (PlatformIO, Portenta H7 + TI ADS1263 EVM)

| Folder | Status | Purpose |
|---|---|---|
| [`SensorHub_PIO/`](SensorHub_PIO/STATUS.md) | **To-Test** (post-2026-05-28 swap + ring-buffer port) | **Production firmware target.** Dual-ADC stream — laser on **ADC1/AIN4-AIN5**, load cell on **ADC2/AIN2-AIN3** — single serial stream with `src` column. Ring-buffer IPC. Needs one bench re-verify run with the swapped pairing before flipping to Stable. |
| [`Calibrate_LaserHead/Calibrate_LaserHead_PIO/`](Calibrate_LaserHead/) | **Stable** | Calibration firmware: dual-ADC cross-compare on AIN4/AIN5 (laser). Ring-buffer IPC. |
| [`Calibrate_LoadCell/Calibrate_Loadcell_PIO/`](Calibrate_LoadCell/) | **Stable** | Calibration firmware: dual-ADC cross-compare on AIN2/AIN3 (load cell). Ring-buffer IPC. |

### Host-side Python modules

| Folder | Status | Purpose |
|---|---|---|
| [`SMA_CharacterizationV2/`](SMA_CharacterizationV2/STATUS.md) | **To-Test** (refactor pending bench-verify) | **Current SMA recorder.** Single-session OPEN → SHORT → RAW state machine with worker threads. Produces per-phase CSVs + `meta.json` for the offline analyzer. Uses `KeysightLCR/` as the single LCR driver. Needs the new `Calibrate_LaserHead/` constants wired in. |
| [`Calibrate_LaserHead/`](Calibrate_LaserHead/) | **Stable** | Calibration tool. Walks the Zaber through a fixed displacement sweep, captures the laser voltage at each point, fits `V = k·µm + V₀`. The resulting `k` and `V₀` feed the SMA analyzer. Dual-ADC cross-compare pipeline. |
| [`Calibrate_LoadCell/`](Calibrate_LoadCell/) | **Stable** | Load-cell calibration. Applies known weights, captures the LCA-9PC output via ADC1+ADC2 cross-compare, fits force↔voltage. |
| [`ZaberStage/`](ZaberStage/STATUS.md) | **Stable** | Linear-stage control wrapper. Auto-discovery, JSON config, 100 Hz position reads, safety limits. v1.0 (Nov 2025). |
| [`KeysightLCR/`](KeysightLCR/STATUS.md) | **Stable** | E4980A/AL LCR meter wrapper. USB or LAN VISA, optimized for max read rate. |

### Archive — `Archieve/` (read-only, **excluded from project understanding**)

`Archieve/` contains fully superseded modules. Do not read or modify these when reasoning about the live project; they're kept only for git-blame / historical reference.

| Folder | Notes |
|---|---|
| `Archieve/ADS1263/` | Arduino-IDE era ADS1263 test sketches (TestA–E, SPI loopback, pin scanner, Stable.ino) plus the original `ADS1263_H7_Integration_Notes.md`. Driver lineage that fed `SensorHub_PIO/lib/ADS1263/`. Retired 2026-05-28. |
| `Archieve/LaserHead_PIO/` | Single-path reference build (ADC2 / laser only). Superseded by `SensorHub_PIO/`. |
| `Archieve/LoadCell_PIO/` | Single-path reference build (ADC1 / load cell only). Superseded by `SensorHub_PIO/`. |
| `Archieve/SMA_Characterization/` | v1 SMA recorder. Superseded by `SMA_CharacterizationV2/`. |
| `Archieve/AD2/` | Digilent Analog Discovery 2 substitute interface used during the H7 down-time. |

---

## Hardware bill of materials

| Subsystem | Part | Datasheet |
|---|---|---|
| MCU | Arduino Portenta H7 (ABX00042) | [doc/PortentaH7_ABX00042_Pinout.pdf](doc/PortentaH7_ABX00042_Pinout.pdf) |
| Carrier | Arduino Portenta Mid Carrier (ASX00055) | [doc/PortentaMidCarrier_ASX00055_Pinout.pdf](doc/PortentaMidCarrier_ASX00055_Pinout.pdf) |
| ADC board | **TI ADS1263 EVM** (32-bit ADC1 + 24-bit ADC2) — connected to the Mid Carrier via a 6-wire SPI cable, see [doc/MEMO_cable_map.md](doc/MEMO_cable_map.md) | [doc/ADS1263_Datasheet.pdf](doc/ADS1263_Datasheet.pdf), [doc/ADS1263_EVM_User_Guide.pdf](doc/ADS1263_EVM_User_Guide.pdf) |
| Voltage reference | **TI REF7050** (5.000 V precision reference) — feeds the ADS1263's external reference inputs on AIN0 / AIN1, see [doc/MEMO_cable_map.md](doc/MEMO_cable_map.md) Cable 2 | (vendor site) |
| Displacement sensor | Keyence IL-030 laser, 30 mm reference, ±5 mm range — wired to AIN4/AIN5 | [doc/KeyenceIL_LaserSensor_Manual.pdf](doc/KeyenceIL_LaserSensor_Manual.pdf) |
| Load cell amplifier | LCA-9PC / LCA-RTC — output wired to AIN2/AIN3 | [doc/LCA9PC_LCARTC_LoadCellAmp_Manual.pdf](doc/LCA9PC_LCARTC_LoadCellAmp_Manual.pdf) |
| LCR meter | Keysight E4980AL | (vendor site) |
| Linear stage | Zaber X-LSQ300A-E01 (300 mm travel, encoder, serial 143153) | (vendor site) |
| Bias-tee | Double bias-tee, 0.22 µF C0G + 47 µH | (custom) |

See [doc/README.md](doc/README.md) for the per-PDF index and the operator memos.

---

## Quick map: where do I go for X?

| If you want to… | Go to |
|---|---|
| Run an SMA characterization experiment | [`SMA_CharacterizationV2/`](SMA_CharacterizationV2/) — `python sma_recorder.py` |
| Re-calibrate the laser head before a run | [`Calibrate_LaserHead/`](Calibrate_LaserHead/) — `python run_calibration.py` |
| Re-calibrate the load cell before a run | [`Calibrate_LoadCell/`](Calibrate_LoadCell/) — `python run_calibration.py` |
| Flash/modify the production firmware on the H7 | [`SensorHub_PIO/`](SensorHub_PIO/) |
| Control the Zaber stage from Python | [`ZaberStage/`](ZaberStage/) |
| Talk to the LCR meter from Python | [`KeysightLCR/`](KeysightLCR/) |
| Look up an ADS1263 register or known issue | `SensorHub_PIO/lib/ADS1263/ADS1263_Driver.cpp` (live driver). Historical notes in `Archieve/ADS1263/ADS1263_H7_Integration_Notes.md`. |

---

## Top-level conventions

- **One master README per module** (not scattered Plan/Memo/Status files). The root README + each module's `README.md` is the canonical source; per-module `STATUS.md` is a short status header.
- **Datasheets** live under [`doc/`](doc/) only. No sub-module duplicates.
- **`Archieve/`** (sic — known misspelling, preserved to avoid breaking any path references) is read-only and excluded from project understanding.
- The `.gitignore` is intentionally minimal — `.DS_Store` only.

---

## TODO — Cross-cutting / major items

Per-module TODOs live in each module's `STATUS.md` (and in this section's "Module pointers" lines).

### Open

- **Bench re-verify `SensorHub_PIO/` after the 2026-05-28 ADC swap + ring-buffer port.** Same expected line rate as the pre-swap verify on 2026-05-25; confirm `src=1` now tracks the laser (AIN4/5) and `src=2` tracks the load cell (AIN2/3) within multimeter tolerance, no ring overflows, no checksum errors. Flips status to **Stable** once `SMA_CharacterizationV2/` consumes the stream end-to-end. *(SensorHub_PIO/STATUS.md)*
- **Wire the new `Calibrate_LaserHead/` and `Calibrate_LoadCell/` constants into `SMA_CharacterizationV2/`.** The legacy `k = -0.1171 mV/µm`, `V₀ = 566.957 mV` constants came through the Waveshare HAT's ~4.4× attenuator and are invalid on the bare EVM. Read the current `Calibrate_LaserHead/calibration.json` and `Calibrate_LoadCell/calibration.json` into `SMA_CharacterizationV2/session.py` (laser_calibration_reference block).
- **Bench-verify `SMA_CharacterizationV2/` after the LCR refactor.** The recorder now imports `LCRMeter` / `MeasurementConfig` / `MeasurementFunction` from `KeysightLCR/lcr_meter.py`; the local `lcr_reader.py` is gone. Smoke-test per `SMA_CharacterizationV2/STATUS.md` and flip the status back to Stable.
- **Fill in the operator memos in `doc/`** (`MEMO_cable_map.md` Cables 3/4 are partial; `MEMO_carrier_config.md`, `MEMO_sensor_setup.md`, `MEMO_bias_tee.md`, `MEMO_lcr_setup.md` are stubs). Single biggest gap for someone else (or an AI agent) reasoning about state.
- **Validate laser displacement linearity against physical reference across full ±5 mm range.** Currently `Calibrate_LaserHead/` runs a sweep but full-range linearity verification is still an open follow-up.
- **Re-record the LCR SHORT calibration any time the cable routing changes.** Per the Notion bias-tee writeup §4.2, the short drifts ~1% with mechanical disturbance. Bake into the operator procedure.

### Future / nice-to-have

- **Layer Ethernet streaming on M7** so the sample stream isn't tied to a USB cable.
- **Rename `Archieve/` → `Archive/`** (it's misspelled). Low priority — grep first to make sure no `sys.path` shim still points at the old name.

### Resolved (recent)

- ✅ **First power-up of the H7 + Mid Carrier + ADS1263 EVM** — 2026-05-24, all 11 checkpoints PASS.
- ✅ **Port firmware from Hat Carrier to Mid Carrier** (SensorHub_PIO) — 2026-05-25 bench-verified pre-swap.
- ✅ **Re-test ADC2/AIN2-AIN3 on the EVM** — 2026-05-24 cp7+cp8 PASS, all pairs clean.
- ✅ **DRDY off `PJ_11`** — resolved by Mid Carrier (DRDY now on PC_6, no LoRa conflict).
- ✅ **M4↔M7 slow-comm crash under sustained dual-ADC throughput** — resolved 2026-05-28 by porting the shared-SRAM (SRAM4) ring buffer from the Calibrate_* modules into `SensorHub_PIO/`. RPC retained for boot-time checkpoints only.
- ✅ **Finalize ADC↔sensor mapping** — 2026-05-28. ADC1 → AIN4/AIN5 laser, ADC2 → AIN2/AIN3 load cell, both 400 SPS. Cross-compare runs in `Calibrate_LaserHead/` and `Calibrate_LoadCell/` settled it.
- ✅ **Retire `LoadCell_PIO/`, `LaserHead_PIO/`, `ADS1263/`** — moved to `Archieve/` (2026-05-28 for `ADS1263/`).
