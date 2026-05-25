# SensorHub_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP** — **code port from Hat Carrier (ASX00049) → Mid Carrier (ASX00055) is done in source; not yet bench-verified.** Pin defines (`PA_8`/`PC_6`/`PC_7`), REFMUX (`0x09`), POWER (`0x13`, VBIAS on for PGA), ADC1 on AIN2/3 + ADC2 on AIN4/5, REF7050 (+5 V) shared on AIN0/1 — all match `ADS1263_FirstPowerUp_PIO/` cp0–cp10. Flips to **To-Test** after a clean dual-stream run on the Mid Carrier + bare TI EVM, and to **Stable** after the SMA recorder consumes it end-to-end. |
| **Role** | Current production firmware target — dual-ADC (load cell on ADC1/AIN2-AIN3 + laser on ADC2/AIN4-AIN5) on Portenta H7 M4 core |
| **Supersedes** | `LoadCell_PIO/`, `LaserHead_PIO/` (kept as diagnostic reference builds) |
| **Last verified** | Hat Carrier setup, April 2026 (per parent `README.md`) — **has NOT been verified on Mid Carrier yet** |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7_bridge -t upload` then `pio run -e portenta_m4 -t upload`, power-cycle the rig (USB + EVM supply), `pio device monitor` @ 115200 |

## Module TODOs

- [x] ~~**Port pin defines to Mid Carrier**~~ — done in source 2026-05-25. `lib/ADS1263/ADS1263_Driver.h` defines `ADS1263_CS_PIN = PA_8` (J15-25), `ADS1263_DRDY_PIN = PC_6` (J15-27), `ADS1263_RESET_PIN = PC_7` (J15-29), matching the Cable 1 row in [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) and the cp0–cp10 baseline in `ADS1263_FirstPowerUp_PIO/STATUS.md`. Bench-verify still pending.
- [x] ~~**Re-verify SPI mapping**~~ — done in source. Driver header documents SPI1 on J15-20 (SCLK / PI_1), J15-22 (CIPO / PC_2), J15-24 (COPI / PC_3); SPI1 hardware CS pad (J15-18 / PI_0) intentionally unused — CS is GPIO-driven via `ADS1263_CS_PIN`. Same arrangement that worked for `ADS1263_FirstPowerUp_PIO/` cp0–cp10. Bench-verify still pending.
- [x] ~~**Resolve ADC2/AIN2-AIN3 saturation**~~ — **RETIRED 2026-05-24** by cp7 (AIN-pair scan) in `ADS1263_FirstPowerUp_PIO/`: all 8 AIN-pair configs PASS on the bare EVM, no saturation reproduces. Production assignment is now AIN2/3 (load cell, ADC1) + AIN4/5 (laser, ADC2) per README §Recommended configuration.
- [ ] **Bench-verify both streams concurrently** on Mid Carrier + bare TI EVM — expect `ID=0x23`, `src=1` (load) and `src=2` (laser) lines arriving at 400 SPS each with no cross-talk and no `REF_ALM` in STATUS bytes. This is the one outstanding item before flipping status to To-Test.
- [x] ~~**Update the `## Wiring` and `## Expected boot output` sections of `README.md`**~~ — done. Wiring block now reflects REF7050 on AIN0/1 + AIN2/3 load + AIN4/5 laser; Expected boot output reflects the dual-ADC code path.
- [ ] **Remove duplicate datasheets from `doc/`** once `README.md` links back to `../doc/` instead. (No local `doc/` exists yet under `SensorHub_PIO/` — this is a forward-looking hygiene item from the parent TODO.)

See [../TODO.md](../TODO.md) for cross-cutting items.
