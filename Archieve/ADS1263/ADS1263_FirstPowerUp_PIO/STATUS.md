# ADS1263_FirstPowerUp_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **Diagnostic** — **cp0–cp10 all bench-verified.** cp0–cp5 on 2026-05-22; cp6 on 2026-05-24 17:59 UTC; cp7–cp10 on 2026-05-24 19:25 UTC. Phase 1 chip-level baseline COMPLETE. Headline: 1.23 µV RMS noise floor at 400 SPS / PGA bypass, AIN2/3 saturation question retired (no pair fails), ADC2 verified, DRDY interrupt-capable on PC_6, EVM AVDD measured ratiometrically at 5.2056 V. Logs: [`data/firstpowerup_20260522_1726.log`](data/firstpowerup_20260522_1726.log), [`data/firstpowerup_20260524_1759.log`](data/firstpowerup_20260524_1759.log), [`data/firstpowerup_20260524_1925.log`](data/firstpowerup_20260524_1925.log). |
| **Role** | Bring-up diagnostic. Eleven ordered checkpoints — Serial / GPIO / /RESET pulse / SPI.begin / ADS1263 ID read / self-noise short / **VBIAS + PGA mini-sweep** / **AIN-pair scan** / **ADC2 enable+read** / **DRDY edge-rate count** / **TDAC ratiometric AVDD check**. Halts on first FAIL with a specific "look at X" hint. M7-only — no M4, no RPC, no shared driver. Implements Phase 1.1 + Phase 1.3–1.6 of [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md). |
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
| EVM AVDD    | **5.2056 V** (24.9 mV span across 5 cp10 rows) | Ratiometrically derived via TDAC on 2026-05-24. Use this — not the nominal 5.0 V — for any downstream calibration math that depends on the EVM's analog supply. The TPS7A4700 LDO is in spec; 5.2 V is just where the trim resistors land. |

## Module TODOs

- [x] **Run it.** ✅ Done 2026-05-22 (cp0–cp5), 2026-05-24 17:59 (cp6), 2026-05-24 19:25 (cp7–cp10).
- [x] **Confirm the pin macros.** ✅ `PWM_0/1/2` don't exist in this core — switched to STM32 pin names `PA_8/PC_6/PC_7`.
- [x] **Re-test ADC2 / AIN2-AIN3 on the EVM.** ✅ cp7 (AIN-pair scan) and cp8 (ADC2 enable+read) PASS. EVM does NOT reproduce the legacy HAT AIN2/3 saturation; ADC2 works clean (8.5 µV RMS at 100 SPS Sinc3 gain=1).
- [x] **Verify DRDY interrupt path on Mid Carrier.** ✅ cp9 (DRDY edge-rate count on PC_6) PASS — 4007/4000 edges. Legacy `PJ_11` / LoRa IRQ conflict does NOT exist on Mid Carrier; interrupt-driven reads are viable.
- [x] **DC accuracy sanity check.** ✅ cp10 ratiometric TDAC sweep PASS. AVDD derived consistent within 25 mV across 5 rows; mean 5.2056 V, in LDO spec.
- [ ] **Propagate the working pin defines + REFMUX/VREF/AVDD findings into `SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h`** and start the firmware port from the Hat Carrier setup. (Cross-cutting — also tracked in [`../TODO.md`](../TODO.md).)
- [ ] **Decide which AIN pair the load cell migrates to**, since AIN0/AIN1 are now committed to reference duty and cp7 has confirmed AIN2–AIN9 all work. Update [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) accordingly.

See [../TODO.md](../TODO.md) for cross-cutting items.
