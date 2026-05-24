# MicroTensileMachine

A benchtop micro tensile rig for characterizing **Shape Memory Alloy (SMA / Flexinol) coils** under Joule-heating actuation. The rig captures **force**, **displacement**, **electrical impedance**, and **stage position** as time-aligned streams so that mechanical and electrical behaviour can be correlated post-hoc.

> **University of Michigan — Robotics, HDR Lab**
> Author: Yilin Ma

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
            │   + ADS1263 24/32-bit ADC│  │ LCR meter│ │  (X-LRM200A) │
            └──┬──────────────────┬────┘  └────┬─────┘ └──────────────┘
        ADC1  │ AIN0/AIN1         │ AIN2/AIN3  │
              ▼                   ▼            ▼
      ┌──────────────┐    ┌──────────────┐  ┌──────────────────┐
      │ Load cell    │    │ Keyence      │  │ Bias-tee         │
      │  + LCA-9PC   │    │ IL-030 laser │  │ (0.22 µF + 47 µH)│
      │    amplifier │    │ displacement │  │       │          │
      └──────┬───────┘    └──────┬───────┘  │       ▼          │
             │                   │           │   SMA coil DUT   │
             ▼                   ▼           │   (Flexinol)     │
        ── Force ──         ── Disp. ──     │       ▲          │
                                            │       │          │
                                            │  DC supply       │
                                            │  (Joule-heating) │
                                            └──────────────────┘
```

The whole machine produces, for one experiment: per-sample voltage streams from the ADCs (force + displacement), per-measurement impedance from the LCR (Ls + Rs), and per-command stage position — all timestamped on the same host clock so that they join cleanly during analysis.

---

## Status legend

Every module below carries a status. Same vocabulary in every README and in the per-module `STATUS.md`:

| Label | Meaning |
|---|---|
| **Stable** | Production-bound; bench-verified end-to-end; safe to use as-is. |
| **WIP** | Work In Progress — actively being built or modified. Expect things to change. |
| **To-Test** | Code/build exists but has **not** been bench-verified end-to-end yet. Treat output skeptically. |
| **Diagnostic** | Kept for debugging, bring-up, or as a single-purpose reference build. Not part of the production flow. |
| **Archived** | Frozen. Superseded by something newer. **Do not edit.** |

---

## Modules at a glance

> ⚠️ **Hardware change in flight:** the rig has moved from the **Hat Carrier** (ASX00049) to the **Mid Carrier** (ASX00055). All PIO firmware READMEs and pin defines were written for the Hat Carrier and are **not yet ported**. The firmware modules below are flagged accordingly.

### Firmware (PlatformIO, Portenta H7 + TI ADS1263 EVM)

| Folder | Status | Purpose |
|---|---|---|
| [`ADS1263_FirstPowerUp_PIO/`](ADS1263_FirstPowerUp_PIO/STATUS.md) | **Diagnostic** (cp0–cp5 passed 2026-05-22; cp6 added 2026-05-24, not yet bench-verified) | **Seven-checkpoint diagnostic** for the first power-up of the new H7 + Mid Carrier + ADS1263 EVM combination. M7-only, no external deps. cp0–cp5 are bring-up; cp6 (VBIAS + PGA mini-sweep) implements Phase 1.1 of [doc/MEMO_baseline_testing.md](doc/MEMO_baseline_testing.md). Working pin defines (`PA_8/PC_6/PC_7`) and register config (`REFMUX=0x09`, external 5 V ref, **VBIAS on for PGA gain > 1**) live in its [STATUS.md](ADS1263_FirstPowerUp_PIO/STATUS.md) — use those when porting `SensorHub_PIO`. |
| [`ADS1263_NoiseFloor_PIO/`](ADS1263_NoiseFloor_PIO/STATUS.md) | **Diagnostic** (created 2026-05-24, not yet bench-verified) | **Phase 1.2 noise-floor sweep.** Walks SPS ∈ {10, 50, 100, 400, 1200, 2400, 4800} × PGA ∈ {1, 2, 4, 8, 16, 32} with AINCOM-shorted inputs, streams a 42-row CSV over USB serial. Python script in `tools/` compares to ADS1263 datasheet Table 7.10 and flags anomalies. Run after `ADS1263_FirstPowerUp_PIO/` cp0–cp6 pass. |
| [`SensorHub_PIO/`](SensorHub_PIO/STATUS.md) | **WIP** (needs Mid Carrier port) | **Current production firmware target.** Dual-ADC driver — load cell on ADC1, laser on ADC2 — single serial stream with `src` column. Supersedes the two single-path builds below. |
| [`LaserHead_PIO/`](LaserHead_PIO/STATUS.md) | **Diagnostic** | Single-path reference build (ADC2 / laser only). Kept for bring-up isolation. |
| [`LoadCell_PIO/`](LoadCell_PIO/STATUS.md) | **Diagnostic** | Single-path reference build (ADC1 / load cell only). Kept for bring-up isolation. |
| [`ADS1263/`](ADS1263/STATUS.md) | **Diagnostic** | Arduino-IDE era test sketches (TestA–E, SPI loopback, pin scanner, Stable.ino) plus the canonical [integration notes](ADS1263/ADS1263_H7_Integration_Notes.md). Historical, but the notes are still authoritative for register configuration. |

### Host-side Python modules

| Folder | Status | Purpose |
|---|---|---|
| [`SMA_CharacterizationV2/`](SMA_CharacterizationV2/STATUS.md) | **To-Test** (refactor pending bench-verify) | **Current SMA recorder.** Single-session OPEN → SHORT → RAW state machine with worker threads. Produces per-phase CSVs + `meta.json` for the offline analyzer. Just refactored to use `KeysightLCR/` as the single LCR driver (local `lcr_reader.py` deleted). |
| [`SMA_Characterization/`](SMA_Characterization/STATUS.md) | **Archived** (superseded by V2) | v1 two-thread recorder. Worked, but lacks the phase state machine. |
| [`Calibrate_LaserHead/`](Calibrate_LaserHead/STATUS.md) | **Stable** | Calibration tool. Walks the Zaber through a fixed displacement sweep, captures the laser voltage at each point, fits `V = k·µm + V₀`. The resulting `k` and `V₀` feed the SMA analyzer. |
| [`ZaberStage/`](ZaberStage/STATUS.md) | **Stable** | Linear-stage control wrapper. Auto-discovery, JSON config, 100 Hz position reads, safety limits. v1.0 (Nov 2025). |
| [`KeysightLCR/`](KeysightLCR/STATUS.md) | **Stable** | E4980A/AL LCR meter wrapper. USB or LAN VISA, optimized for max read rate. |

### Archive

| Folder | Status | Purpose |
|---|---|---|
| [`Archieve/AD2/`](Archieve/AD2/STATUS.md) | **Archived** | Digilent Analog Discovery 2 substitute interface used during the H7 down-time. Replaced by the H7 + ADS1263 path. |

---

## Hardware bill of materials

| Subsystem | Part | Datasheet |
|---|---|---|
| MCU | Arduino Portenta H7 (ABX00042) | [doc/PortentaH7_ABX00042_Pinout.pdf](doc/PortentaH7_ABX00042_Pinout.pdf) |
| Carrier | Arduino Portenta Mid Carrier (ASX00055) | [doc/PortentaMidCarrier_ASX00055_Pinout.pdf](doc/PortentaMidCarrier_ASX00055_Pinout.pdf) |
| ADC board | **TI ADS1263 EVM** (32-bit ADC1 + 24-bit ADC2) — connected to the Mid Carrier via a 6-wire SPI cable, see [doc/MEMO_cable_map.md](doc/MEMO_cable_map.md) | [doc/ADS1263_Datasheet.pdf](doc/ADS1263_Datasheet.pdf), [doc/ADS1263_EVM_User_Guide.pdf](doc/ADS1263_EVM_User_Guide.pdf) |
| Voltage reference | **TI REF7050** (5.000 V precision reference) — feeds the ADS1263's external reference inputs on AIN0 / AIN1, see [doc/MEMO_cable_map.md](doc/MEMO_cable_map.md) Cable 2 | (vendor site) |
| ~~ADC HAT (legacy)~~ | ~~Waveshare High-Precision AD HAT (ADS1263)~~ — used during March–April 2026 bring-up with the Hat Carrier; **superseded by the EVM**. Many firmware READMEs still reference this setup; the body content remains accurate for historical context but does not describe the current rig. | (same datasheets as the EVM — the silicon is identical) |
| Displacement sensor | Keyence IL-030 laser, 30 mm reference, ±5 mm range | [doc/KeyenceIL_LaserSensor_Manual.pdf](doc/KeyenceIL_LaserSensor_Manual.pdf) |
| Load cell amplifier | LCA-9PC / LCA-RTC | [doc/LCA9PC_LCARTC_LoadCellAmp_Manual.pdf](doc/LCA9PC_LCARTC_LoadCellAmp_Manual.pdf) |
| LCR meter | Keysight E4980AL | (vendor site) |
| Linear stage | Zaber X-LRM200A | (vendor site) |
| Bias-tee | Double bias-tee, 0.22 µF C0G + 47 µH | (custom) |

See [doc/README.md](doc/README.md) for the per-PDF index with one-line descriptions and the Memo section (cable diagram, configuration notes) — to be filled in by the operator.

---

## Quick map: where do I go for X?

| If you want to… | Go to |
|---|---|
| Run an SMA characterization experiment | [`SMA_CharacterizationV2/`](SMA_CharacterizationV2/) — `python sma_recorder.py` |
| Re-calibrate the laser head before a run | [`Calibrate_LaserHead/`](Calibrate_LaserHead/) — `python run_calibration.py` |
| First-time power up the H7 + Mid Carrier + ADS1263 EVM | [`ADS1263_FirstPowerUp_PIO/`](ADS1263_FirstPowerUp_PIO/) — start here for any new hardware combination |
| Characterize the ADS1263 noise floor across all SPS × PGA modes | [`ADS1263_NoiseFloor_PIO/`](ADS1263_NoiseFloor_PIO/) — run after first-power-up cp0–cp6 pass; see [doc/MEMO_baseline_testing.md](doc/MEMO_baseline_testing.md) |
| Flash/modify the firmware on the H7 | [`SensorHub_PIO/`](SensorHub_PIO/) (current target, but bring-up must pass first) |
| Bring up / debug the ADC stand-alone | [`ADS1263_FirstPowerUp_PIO/`](ADS1263_FirstPowerUp_PIO/) first, then [`LoadCell_PIO/`](LoadCell_PIO/) or [`LaserHead_PIO/`](LaserHead_PIO/) |
| Look up an ADS1263 register or known issue | [`ADS1263/ADS1263_H7_Integration_Notes.md`](ADS1263/ADS1263_H7_Integration_Notes.md) |
| Control the Zaber stage from Python | [`ZaberStage/`](ZaberStage/) |
| Talk to the LCR meter from Python | [`KeysightLCR/`](KeysightLCR/) |
| See open work / what's next | [TODO.md](TODO.md) |

---

## Top-level conventions

- **Status flags** live in two places per module: a banner at the top of the existing `README.md`, and a machine-readable [`STATUS.md`](#status-legend) next to it. Update both when status changes.
- **Per-module TODOs** live inside each module's `STATUS.md`. The root [TODO.md](TODO.md) carries only cross-cutting and major items.
- **Datasheets** live under [`doc/`](doc/) only. Sub-module `doc/` folders (currently `SensorHub_PIO/doc/`) hold supporting notes specific to that build, not duplicate datasheets long-term.
- **`Archieve/`** (sic) is for fully superseded modules. Anything in there is read-only.
- The `.gitignore` is intentionally minimal — `.DS_Store` only.
