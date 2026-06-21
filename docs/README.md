# `doc/` — Datasheets, manuals, and operator memos

This folder holds the authoritative reference material for every physical part in the rig. **All datasheets live here**; sub-module `doc/` folders (e.g. `Firmware_SensorHub_PIO/doc/`) hold only build-specific supporting notes, not duplicate copies.

## Datasheets / manuals

### MCU and carrier

| File | Part | Used by | One-line summary |
|---|---|---|---|
| [`PortentaH7_ABX00042_Pinout.pdf`](PortentaH7_ABX00042_Pinout.pdf) | Arduino Portenta H7 (ABX00042) | All `*_PIO/` firmware, `ADS1263/` | Full pinout of the H7 module — STM32H747 pin names, HD-connector mapping, power rails. Authoritative source for the `PE_6`/`PI_5`/`PJ_11` etc. pin defines in the firmware drivers. |
| [`PortentaMidCarrier_ASX00055_Pinout.pdf`](PortentaMidCarrier_ASX00055_Pinout.pdf) | Arduino Portenta Mid Carrier (ASX00055) | All `*_PIO/` firmware (after Mid Carrier port) | Full pinout of the **current** carrier. Replaces the Hat Carrier (ASX00049) that the firmware READMEs still reference. **Cross-reference this PDF when porting the existing pin defines to the Mid Carrier — the J5 40-pin Pi header that the Hat Carrier provided is laid out differently here.** |

### ADC

| File | Part | Used by | One-line summary |
|---|---|---|---|
| [`ADS1263_Datasheet.pdf`](ADS1263_Datasheet.pdf) | TI ADS1263 (32-bit ADC1 + 24-bit ADC2 on one die) | All `*_PIO/` firmware, `ADS1263/` | Definitive register reference. Look here for `INPMUX`, `MODE2`, `ADC2CFG`, `REFMUX`, filter modes, SPI frame format. |
| [`ADS1263_EVM_User_Guide.pdf`](ADS1263_EVM_User_Guide.pdf) | TI ADS1263 EVM (evaluation module) | All `*_PIO/` firmware (current rig), `ADS1263/` (historical reference) | **EVM is the current ADC board on the rig.** Use this guide for the EVM's J1/J2 headers, jumper map, and on-board reference. The companion [`../Firmware_SensorHub_PIO/doc/ADS1263EVM_Modifications.md`](../Firmware_SensorHub_PIO/doc/ADS1263EVM_Modifications.md) records any non-default jumper / solder mods we've applied. Note: the legacy Waveshare ADS1263 HAT (used March–April 2026) carried the same silicon but with added input-stage circuitry — that's why old calibration numbers don't transfer to the bare EVM. |

### Sensors

| File | Part | Used by | One-line summary |
|---|---|---|---|
| [`KeyenceIL_LaserSensor_Manual.pdf`](KeyenceIL_LaserSensor_Manual.pdf) | Keyence IL-series laser displacement sensor (we use IL-030) | `LaserHead_PIO/`, `Firmware_SensorHub_PIO/`, `Calibrate_LaserHead/`, `SMA_Characterization*/` | IL-030 specs and amplifier setup: 30 mm reference distance, ±5 mm range, 0.5 mV/µm nominal in the 0–5 V output mode, voltage-vs-current output switch on the controller. |
| [`LCA9PC_LCARTC_LoadCellAmp_Manual.pdf`](LCA9PC_LCARTC_LoadCellAmp_Manual.pdf) | Omega LCA-9PC / LCA-RTC bridge amplifier | `LoadCell_PIO/`, `Firmware_SensorHub_PIO/`, `Archieve/AD2/` | Load-cell amplifier setup: bridge excitation, zero/span calibration, 0–5 V output. **Note: needs 30 min warm-up before calibration measurements.** Compression-only wiring in this rig. |

---

## Memos — operator notes

Memos capture the things the datasheets *don't* tell you: how this particular rig is cabled, what jumpers are set on this particular board, what configuration was used for this particular experiment. **These files are operator-maintained — please update them as the hardware changes.**

> The memos below are placeholders. Yilin: fill these in next time you're at the bench. If you'd rather have one big `RIG_MEMO.md` instead of several small files, say the word and we'll consolidate.

| Memo | What it should contain | Status |
|---|---|---|
| [`MEMO_cable_map.md`](MEMO_cable_map.md) | Every cable in the rig, from one end to the other. Connector type, wire colours, what it carries. Photo links if useful. | **Started** — Mid Carrier ↔ ADS1263 EVM SPI bus is documented (6 wires, J15 ↔ J2). Sensor and power cables still TODO. |
| `MEMO_carrier_config.md` | Current Mid Carrier (ASX00055) jumper / switch state, how the HAT/ADS1263 is mounted to it (Hat Carrier had J5 40-pin; document the Mid Carrier equivalent — adapter? wired-through?), and any solder mods done to the HAT. | **TODO — empty** |
| `MEMO_sensor_setup.md` | IL-030 controller front-panel settings actually in use (voltage vs current mode, span, zero), LCA-9PC zero/span procedure as last performed (date, calibration weights, resulting offset), load-cell mechanical mount. | **TODO — empty** |
| `MEMO_bias_tee.md` | Schematic of the double bias-tee (component values, layout, where the DC supply attaches), and the SMA DUT mounting jig. | **TODO — empty** |
| `MEMO_lcr_setup.md` | LCR meter VISA resource string in use (USB vs LAN), test fixture compensation that's currently loaded on the instrument, OPEN/SHORT/LOAD calibration history. | **TODO — empty** |

---

## Adding a new datasheet

1. Drop the PDF here. Use the convention `Part_Manufacturer_DocType.pdf` (descriptive, no spaces, includes the manufacturer's part code if relevant for search).
2. Add a row to the appropriate section above with a one-line summary and which module(s) consume it.
3. If the part is going into the rig (not just reference), add a row to the **Hardware bill of materials** table in the [root README](../README.md).

## Conventions

- **Datasheets only here.** Operator memos go here too (above), but per-module debugging notes belong in that module's folder.
- **Don't duplicate.** `Firmware_SensorHub_PIO/doc/` holds `PortentaH7_ABX00042_Pinout.pdf` etc. as a convenience during development — long-term, those should be removed in favour of links back to this folder.
- **Keep names descriptive.** Future-you and the AI agent both rely on filenames carrying meaning. `ABX00042-full-pinout.pdf` is ambiguous; `PortentaH7_ABX00042_Pinout.pdf` is not.
