# ADS1263_FirstPowerUp_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **To-Test** — has not been flashed or run yet. The actual first power-up of the new H7 + Mid Carrier + ADS1263 EVM hardware is what this sketch is for. |
| **Role** | Bring-up diagnostic. Six ordered checkpoints (Serial / GPIO / /RESET pulse / SPI.begin / ADS1263 ID read / self-noise short). Halts on first FAIL with a specific "look at X" hint. M7-only — no M4, no RPC, no shared driver. |
| **Created** | 2026-05-22 (with the cable map in [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md)) |
| **Owner** | Yilin |
| **Quick test** | `pio run -t upload` then `pio device monitor` — expect six `[cp N] PASS` lines and an `ALL CHECKPOINTS PASSED` banner. |
| **Dependencies on other modules** | None — fully standalone by design. Inline SPI helpers, no `lib/ADS1263/`, no V1/V2 imports. |

## Module TODOs

- [ ] **Run it.** First-power-up the EVM and capture the full serial output. Save the log to `data/firstpowerup_YYYYMMDD_HHMMSS.log` for the record.
- [ ] **Confirm the pin macros.** Before first flash, verify `PWM0` / `PWM1` / `PWM2` are valid macros in your Arduino-mbed Portenta H7 core version. If not, change `PIN_CS` / `PIN_DRDY` / `PIN_RESET` in `src/main.cpp` to whatever your core uses for J15-25 / J15-27 / J15-29, AND update [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) and [`../SensorHub_PIO/STATUS.md`](../SensorHub_PIO/STATUS.md) so the production firmware port uses the same macros.
- [ ] **Once it passes**, propagate the working pin defines into `SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h` and start the firmware port from the Hat Carrier setup. Flip this module's status from `To-Test` to `Diagnostic` (it stays around as a re-runnable bring-up tool, but is no longer "the active question").
- [ ] **Add a re-run for the noise floor with shorted inputs** — the default `[cp 5]` is generous (5 mV threshold) because it doesn't require the operator to short AIN0/AIN1. The integration notes' Test B baseline is ~5 µV RMS with shorted inputs — re-run once with the short installed and capture the result.

See [../TODO.md](../TODO.md) for cross-cutting items.
