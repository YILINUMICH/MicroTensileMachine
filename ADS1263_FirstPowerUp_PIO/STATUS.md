# ADS1263_FirstPowerUp_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **Diagnostic** — **cp0–cp6 all bench-verified.** cp0–cp5 on 2026-05-22 (noise floor 1.4 µV RMS at 400 SPS / PGA bypass / VREF=5V). cp6 (VBIAS + PGA mini-sweep) verified on 2026-05-24 (all six gain points PASS; cp5 baseline reproduced at 1.257 µV RMS). Kept around as a re-runnable bring-up tool for any future hardware change. Logs: [`data/firstpowerup_20260522_1726.log`](data/firstpowerup_20260522_1726.log), [`data/firstpowerup_20260524_1759.log`](data/firstpowerup_20260524_1759.log). |
| **Role** | Bring-up diagnostic. Seven ordered checkpoints (Serial / GPIO / /RESET pulse / SPI.begin / ADS1263 ID read / self-noise short / **VBIAS + PGA mini-sweep**). Halts on first FAIL with a specific "look at X" hint. M7-only — no M4, no RPC, no shared driver. cp6 implements Phase 1.1 of [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md). |
| **Created** | 2026-05-22 (with the cable map in [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md)) |
| **Owner** | Yilin |
| **Quick test** | `pio run -t upload` then `pio device monitor` — expect six `[cp N] PASS` lines and an `ALL CHECKPOINTS PASSED` banner. |
| **Dependencies on other modules** | None — fully standalone by design. Inline SPI helpers, no `lib/ADS1263/`, no V1/V2 imports. |

## What the bring-up established (use this when porting other modules)

These are the working values that any other firmware on this rig should match:

| Setting | Value | Source of truth |
|---|---|---|
| `PIN_CS`    | `PA_8`  | Mid Carrier J15-25 → HD J2-59. **Do NOT use the macro `PWM_0`** — not defined in the arduino-mbed Portenta H7 core that PlatformIO downloads (`platform = ststm32`, `board = portenta_h7_m7`); compile fails with "PWM_0 was not declared in this scope." |
| `PIN_DRDY`  | `PC_6`  | J15-27 → J2-61. Same caveat. |
| `PIN_RESET` | `PC_7`  | J15-29 → J2-63. Same caveat. |
| `REFMUX`    | `0x09`  | External 5 V reference on AIN0 (+REF) / AIN1 (−REF). Datasheet §9.6.12. |
| `VREF`      | `5.0 V` | In any volts-per-code math (`V = code · VREF / 2^31` for ADC1). |
| `INPMUX` for noise-floor | `0xAA` | AINCOM-shorted both inputs. **AIN0/AIN1 are reference-only on this rig and MUST NOT be used as measurement inputs.** |
| `POWER` for PGA gain > 1 | `0x13` | INTREF on (bit 4) + **VBIAS on (bit 1)** → drives AINCOM to mid-supply (+2.5 V) so the PGA's common-mode range is satisfied. Without VBIAS, AINCOM floats and PGA-enabled measurements will rail or read garbage. Datasheet §9.3.12 Figure 9-26. |
| SPI         | mode 1, 500 kHz, default SPI object on the Mid Carrier | |
| ADS1263 ID  | `0x23` (silicon rev 3) | Read of register 0x00 after reset. |

## Module TODOs

- [x] **Run it.** ✅ Done 2026-05-22 — see [`data/firstpowerup_20260522_1726.log`](data/firstpowerup_20260522_1726.log).
- [x] **Confirm the pin macros.** ✅ `PWM_0/1/2` don't exist in this core — switched to STM32 pin names `PA_8/PC_6/PC_7`. Documented in the table above and in [`src/main.cpp`](src/main.cpp) lines 38-72.
- [ ] **Propagate the working pin defines + REFMUX/VREF scheme into `SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h`** and start the firmware port from the Hat Carrier setup. (Cross-cutting — also tracked in [`../TODO.md`](../TODO.md).)
- [ ] **Re-test ADC2 / AIN2-AIN3 on the EVM.** This diagnostic only exercised ADC1. The legacy HAT had a saturation issue on AIN2/AIN3; whether the EVM behaves the same is still unknown. Worth adding a `cp 6` for ADC2 before `SensorHub_PIO` assumes dual-ADC operation. (Cross-cutting — also tracked in [`../TODO.md`](../TODO.md).)
- [ ] **Decide which AIN pair the load cell migrates to**, since AIN0/AIN1 are now committed to reference duty (contradicts the existing cable-map note that has load cell on AIN0/AIN1). Update [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) accordingly.

See [../TODO.md](../TODO.md) for cross-cutting items.
