# SensorHub_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP** — needs port from Hat Carrier (ASX00049) to Mid Carrier (ASX00055) |
| **Role** | Current production firmware target — dual-ADC (load cell on ADC1 + laser on ADC2) on Portenta H7 M4 core |
| **Supersedes** | `LoadCell_PIO/`, `LaserHead_PIO/` (kept as diagnostic reference builds) |
| **Last verified** | Hat Carrier setup, April 2026 (per parent `README.md`) — has NOT been verified on Mid Carrier yet |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7_bridge -t upload` then `pio run -e portenta_m4 -t upload`, power-cycle Hat Carrier, `pio device monitor` @ 115200 |

## Module TODOs

- [ ] **Port pin defines to Mid Carrier** — `lib/ADS1263/ADS1263_Driver.h` `ADS1263_CS_PIN` / `ADS1263_DRDY_PIN` / `ADS1263_RESET_PIN`. Use the Mid Carrier ↔ ADS1263 EVM wiring table in [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) as the source of truth (J15-25 → /CS, J15-27 → /DRDY, J15-29 → /RESET); cross-reference `../doc/PortentaMidCarrier_ASX00055_Pinout.pdf` for the STM32 pin name behind each J15 position.
- [ ] **Re-verify SPI mapping** — Hat Carrier exposed SPI on J5 Pi-header pins 19/21/23. Mid Carrier connector layout is different; confirm which physical pads the H7's `SPI` object reaches.
- [ ] **Resolve ADC2/AIN2-AIN3 saturation** — currently working around it by routing the laser through ADC1/AIN0-AIN1 (single-ADC mode, `ENABLE_ADC2 = 0`). Until the root cause is fixed, the dual-ADC production mode can't actually run.
- [ ] **Bench-verify both streams concurrently** once the above two are done — expect `src=1` (load) and `src=2` (laser) lines arriving at their configured rates with no cross-talk.
- [ ] **Update the `## Wiring` and `## Expected boot output` sections of `README.md`** when carrier port is done; current text describes Hat Carrier.
- [ ] **Remove duplicate datasheets from `doc/`** once `README.md` links back to `../doc/` instead.

See [../TODO.md](../TODO.md) for cross-cutting items.
