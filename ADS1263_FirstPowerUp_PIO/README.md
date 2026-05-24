> **Status: Diagnostic** — cp0–cp5 bench-verified on 2026-05-22 (noise floor 1.4 µV RMS at 400 SPS / PGA bypass). **cp6 (VBIAS + PGA mini-sweep) added 2026-05-24 — not yet bench-verified.** Kept around as a re-runnable bring-up tool. See [STATUS.md](STATUS.md) for the working pin defines and register settings. See [../README.md](../README.md) for project overview. See [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md) for the wider Phase 1 testing plan; cp6 is its Phase 1.1.

# ADS1263_FirstPowerUp_PIO — bring-up diagnostic

Seven-checkpoint sketch for the **first power-on** of the new hardware combination plus the Phase 1.1 PGA mini-sweep:

- Arduino Portenta H7 (ABX00042)
- Arduino Portenta Mid Carrier (ASX00055)
- TI ADS1263 EVM
- 6-wire SPI cable per [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md)

The sketch runs the six checkpoints **in order at boot**, prints `[cp N] PASS/FAIL/info <message>` lines for each, and **halts with a clear "look at X" hint on first failure**. M7-only — no M4 / no RPC / no shared driver — so any failure is provably wiring or hardware, not firmware complexity.

## When to use this

- **Now** — first time you apply power to the new hardware combination.
- After any cable change to the SPI bus.
- After replacing the EVM, the Mid Carrier, or the H7 module.
- Whenever `SensorHub_PIO` (or any other firmware) fails its boot ID check and you want to bisect "is the chip even there?" before debugging firmware.

After this passes, the pin defines that worked here become the source of truth for porting `SensorHub_PIO` / `LaserHead_PIO` / `LoadCell_PIO` from the legacy Hat Carrier setup to the Mid Carrier — see [`../TODO.md`](../TODO.md).

---

## Before you flash — once

There is **one** step the sketch can't do for you: verify that the Arduino-mbed Portenta H7 core in your install spells the J15-25 / J15-27 / J15-29 pin macros as `PWM0`, `PWM1`, `PWM2`. Recent cores do; older ones might not.

If `pio run` fails with `'PWM0' was not declared in this scope`, open:

```
<core install>/variants/PORTENTA_H7_M7/variant.h
```

(on Windows usually `%LOCALAPPDATA%\Arduino15\packages\arduino\hardware\mbed_portenta\<version>\variants\PORTENTA_H7_M7\variant.h`)

and look for whatever symbol your core uses for Portenta-HD `PWM_0`/`PWM_1`/`PWM_2` (alternatives include `D2`/`D3`/`D4` or raw `PA_0`-style STM32 names). Update the three `#define PIN_CS/DRDY/RESET` lines at the top of `src/main.cpp` accordingly, then update [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) and [`../SensorHub_PIO/STATUS.md`](../SensorHub_PIO/STATUS.md) to match.

## Build + flash

```sh
cd ADS1263_FirstPowerUp_PIO/
pio run -t upload
pio device monitor   # 115200 baud
```

Power-cycle the EVM after flashing (per the integration notes — the dfu reset on the H7 doesn't cleanly re-power the ADC). Then reopen the monitor.

## Expected output (success)

```
============================================================
  ADS1263 first-power-up diagnostic — M7-only
============================================================
Cable map: ../doc/MEMO_cable_map.md
Carrier:   Portenta Mid Carrier (ASX00055), J15 connector
EVM:       TI ADS1263 EVM, J2 connector

[cp 0] PASS  Serial up (USB CDC enumerated)
[cp 1] info  DRDY initial level = HIGH
[cp 1] PASS  pinMode CS/DRDY/RESET; CS=HIGH, RESET=HIGH
[cp 2] info  pulsing /RESET LOW for 100 ms (scope-verify if possible)
[cp 2] PASS  /RESET pulsed
[cp 3] info  calling SPI.begin() — using default SPI object
[cp 3] info    (Portenta H7 default SPI → J2-38/40/42 → Mid Carrier J15-20/22/24)
[cp 3] PASS  SPI.begin() returned
[cp 4] info  waiting 3000 ms for ADS1263 power-up settle (per integration notes §2)
[cp 4] info  ID register (0x00) = 0x23  (expecting 0x2X family)
[cp 4] info  INTERFACE register (0x02) = 0x05  (default 0x05 = STATUS+CRC)
[cp 4] PASS  ADS1263 found on SPI bus
[cp 5] info  configuring ADC1: INPMUX=0xAA (AINCOM-shorted), MODE2=0x88 (PGA bypass, 400 SPS), REFMUX=0x09 (external 5V ref on AIN0/AIN1)
[cp 5] info  100 samples: mean = +0.708 mV   RMS = 1.416 uV   (target: <50 uV)
[cp 5] PASS  ADC stream alive and within sanity threshold
[cp 6] info  enabling VBIAS (POWER bit 1) — biases AINCOM to mid-supply (+2.5 V)
[cp 6] info  POWER: 0x11 (before) → 0x13 (wrote) → 0x13 (readback)
[cp 6] info  PGA gain sweep at 400 SPS, AINCOM-shorted, 200 samples per gain
[cp 6] info    gain | MODE2 |  out mean (uV)  | out RMS (uV) |  in mean (uV)  | in RMS (uV) | result
[cp 6] info    -----+-------+-----------------+--------------+----------------+-------------+-------
[cp 6] info       1 |  0x08 |       +700.00   |      1.500   |     +700.00    |     1.500   | pass
[cp 6] info       2 |  0x18 |      +1400.00   |      2.000   |     +700.00    |     1.000   | pass
[cp 6] info       4 |  0x28 |      +2800.00   |      3.000   |     +700.00    |     0.750   | pass
[cp 6] info       8 |  0x38 |      +5600.00   |      5.000   |     +700.00    |     0.625   | pass
[cp 6] info      16 |  0x48 |     +11200.00   |      9.000   |     +700.00    |     0.563   | pass
[cp 6] info      32 |  0x58 |     +22400.00   |     17.000   |     +700.00    |     0.531   | pass
[cp 6] PASS  VBIAS + PGA mini-sweep clean across all gains

============================================================
  ALL CHECKPOINTS PASSED
============================================================
Hardware bring-up + PGA mini-sweep look good. Next steps:
  - run ADS1263_NoiseFloor_PIO/ for the full SPS × PGA
    sweep (Phase 1.2 in doc/MEMO_baseline_testing.md).
  - port SensorHub_PIO to match what worked here:
    pin defines (PA_8/PC_6/PC_7), REFMUX=0x09,
    VREF=5.0V in any volts-per-code math, and
    POWER bit 1 (VBIAS) set when PGA gain > 1.
  - update doc/MEMO_cable_map.md (load-cell channel
    now needs an AIN pair OTHER than AIN0/AIN1).
```

**Important caveat on the cp6 example numbers above:** the cp6 row values shown are *illustrative* — they show roughly what a healthy chain *should* produce (input-referred mean stays approximately constant across gains, input-referred RMS drops slightly with gain, output-referred values scale with gain). The actual numbers on your bench will depend on REF7050 trim, chip-to-chip offset variation, and the EVM's exact assembly. What matters is the *pattern*: the input-referred mean should be roughly constant across the gain column, and input-referred RMS should be in the single-digit µV range at every gain.

Built-in LED blinks slowly (~1 Hz) when sitting in the post-success loop. Fast blink (~3 Hz) when halted on a FAIL.

---

## Failure triage

The "hint" line on every FAIL gives you the most likely first thing to check. Below is the full table mapping checkpoint failures to physical things.

| Stops at | Most-likely cause | What to check, in order |
|---|---|---|
| Nothing prints (no banner) | M7 not booting, or USB CDC not enumerating | (1) Power: blue LED on the carrier? Green LED on the H7 module? (2) Try a different USB cable / port. (3) `pio device list` from another terminal — is the port even visible? (4) Confirm `upload_protocol = dfu` was actually used. |
| `[cp 1] FAIL pinMode` (build error or hang) | Pin macros `PWM0`/`PWM1`/`PWM2` not defined in your mbed core version | See [Before you flash — once](#before-you-flash--once). |
| `[cp 2] PASS` but you have a scope and don't see the reset pulse | Wrong physical pin on the carrier OR wire #6 (black, /RESET) not seated | (1) Scope the actual J15-29 pin while watching the boot — should see a 100 ms low pulse ~50 ms after `[cp 2]` prints. (2) Continuity-test the black wire end-to-end. (3) If both look right, verify `PIN_RESET` macro maps to J15-29 in your core's variant.h. |
| `[cp 3] FAIL SPI.begin()` (hang) | The mbed core's default `SPI` object isn't mapped to the J15-20/22/24 group on your hardware/core version | Try declaring an explicit `MbedSPI` instance pinned to PI_1/PC_2/PC_3 (the STM32 pins behind J2-38/40/42). Per the integration notes, this is the same H7 SPI bus that worked on the Hat Carrier. |
| `[cp 4] FAIL ID = 0x00` (MISO silent) | Cable, power, or seating problem | (1) Full power-cycle the EVM (unplug USB and any external supply, wait 5 s, reapply). (2) Reseat **every wire** end-to-end on both connectors. (3) Ohm-meter SCLK / MISO / MOSI / /CS continuity, end-to-end, off-board. (4) Confirm EVM 3.3 V and 5 V rails present at J2 with a multimeter. The integration notes (§Known Hardware Issue) describe a cold-solder-joint failure mode that produced this exact symptom on the legacy HAT — same triage applies here. |
| `[cp 4] FAIL ID = 0xFF` (MISO floating high) | No chip is being selected — /CS line broken | The brown wire (cable #4, J15-25 → J2-7) is open. Reseat or replace. Also verify `PIN_CS` macro matches J15-25. |
| `[cp 4] FAIL ID = 0xXX` (wrong family) | SPI bus alive but the wrong chip is answering, or framing is off | (1) Confirm SPI mode 1 is being used (CPOL=0, CPHA=1 — already set in `SPI_CFG`). (2) Check for any other SPI device on the same bus that might be answering. |
| `[cp 5] FAIL register readback mismatch` | WREG not landing — usually a /CS hold timing issue | This shouldn't happen on a fresh, clean bring-up. If it does, capture a scope trace of /CS during the WREG transaction — `/CS` should stay LOW for the full 3-byte WREG sequence. |
| `[cp 5] FAIL RMS > 5 mV` | Floating inputs picking up mains hum, OR reference voltage problem | (1) Verify `INPMUX=0xAA` (AINCOM-shorted internally) — should give differential = 0 regardless of external wiring. (2) If still high: check `REFMUX` is `0x09` and the REF7050 is actually present at AIN0/AIN1 (multimeter to confirm +5 V). |
| `[cp 6] FAIL VBIAS bit did not stick` | WREG to POWER register not landing | Same triage as cp5 register readback. Sometimes a delay before cp6 helps if back-to-back WREGs are racing. |
| `[cp 6] FAIL stuck` row at any gain | Conversions not advancing after MODE2 change | START1 command may not be clocking through cleanly when MODE2 is written while the chip is in continuous-conversion mode. Try stopping (STOP1), writing MODE2, then START1 again. |
| `[cp 6] FAIL noisy` row at gain ≥ 2 | VBIAS didn't land → AINCOM railed → PGA Vcm out of range | (1) Confirm cp6's `POWER readback` line shows bit 1 set (value 0x13 not 0x11). (2) If POWER bit 1 stuck, try the J5:1↔J5:2 jumper on the EVM as a fallback (ties AINCOM to REFOUT = 2.5 V externally). (3) If both bias methods produce noisy gain ≥ 2 rows, suspect the PGA settling delay — try doubling the post-`START1` `delay(50)` to `delay(100)`. |
| `[cp 6] FAIL noisy` row at gain = 1 only | Either the cp5 baseline was wrong (something changed between cp5 and cp6), or the PGA itself is noisier than spec | Re-run cp5 — its number should be within ~2× of cp6's gain=1 output-referred RMS. A big disagreement means something physical changed during the sketch. |

---

## What this sketch deliberately does NOT do

- **No M4 / no RPC.** First power-up of new hardware should not also be the first run of the dual-core stack. Once `cp 4` passes here, you have proof the chip works on the bus; only then is it worth chasing M4-side issues in `SensorHub_PIO`.
- **No external reference.** Uses the chip's internal 2.5 V reference (REFMUX=0x00), not the 5 V external path that `LoadCell_PIO` / `SensorHub_PIO` use. Removing one variable.
- **No PGA.** PGA bypass mode (MODE2=0x88). Removing another variable.
- **No DRDY gating.** Timed polling at 5 ms intervals (400 SPS = 2.5 ms conversion). Whether DRDY is wired correctly or whether the legacy `PJ_11` / LoRa conflict exists on the Mid Carrier is irrelevant to this sketch — checked separately if needed.
- **No driver dependency.** Inline SPI helpers, no `lib/ADS1263/` import. If `SensorHub_PIO`'s driver has a bug, this sketch is unaffected.

## File layout

```
ADS1263_FirstPowerUp_PIO/
├── README.md          (this file)
├── STATUS.md
├── platformio.ini     M7-only single env
├── .gitignore
└── src/
    └── main.cpp       6 checkpoints, ~300 lines, no external deps
```

## Related

- [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) — wiring source of truth
- [`../doc/PortentaMidCarrier_ASX00055_Pinout.pdf`](../doc/PortentaMidCarrier_ASX00055_Pinout.pdf) — J15 pinout reference
- [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) — register reference (§3, §4) and historical Hat Carrier triage that still applies
- [`../SensorHub_PIO/`](../SensorHub_PIO/) — what this bring-up unblocks
