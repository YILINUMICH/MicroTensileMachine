# Firmware_ADS131M04Test_PIO

Bench-test firmware for the **ADS131M04** evaluation — a candidate replacement
for the ADS1263. Holds the canonical `ADS131M04_Driver` for the duration of the
evaluation.

**Plan:** [`../docs/ADS131M04_migration_plan.md`](../docs/ADS131M04_migration_plan.md).
Read §2 (hardware preconditions) before wiring and §7 (the T1–T9 test list)
before flashing. **Status:** see [`STATUS.md`](STATUS.md).

---

## Why this project exists separately

The driver has to be qualified *before* it is allowed anywhere near the ring
buffer, the SMA controller, or a capture. So this project is standalone: no
`sample_ring.h`, no RPC, no SMA drive, no UDP yet. If a number here is wrong,
the driver is wrong — there is nothing else in the path to blame.

**It runs on M7, not M4.** In production the ADC lives on the M4, but the M4 has
no direct USB and no Ethernet — its output must be bridged through the M7 over
Arduino RPC, which is exactly the machinery you do not want in the loop while
deciding whether a driver works. On M7 this talks straight out USB-CDC.

---

## Running it

```
pio run -e portenta_m4_idle -t upload    # FIRST — park M4 in __WFI
pio run -e portenta_m7      -t upload    # then the test firmware
#   power-cycle USB + EVM supply, wait ~5 s, reapply
pio device monitor                       # 115200 baud
```

**Flash the M4 idle image first.** Whatever M4 firmware is currently resident
(SensorHub or the CC fork) drives the *same* SPI1 bus and the *same* CS pin.
Leave it running and the two cores fight over the bus; the symptom is
intermittent CRC errors that look exactly like a cable fault.

Envs:

| Env | Purpose |
|---|---|
| `portenta_m7` | the bench test |
| `portenta_m7_trace` | same, `-D ADS131M04_DEBUG=1` — one line per failed frame from inside the driver. Noisy; for diagnosing a bad ID or a WREG ack mismatch, not for taking data. |
| `portenta_m4_idle` | empty `setup()` + `__WFI()` loop, so M4 makes no SPI traffic |

---

## Before you blame the driver — three EVM jumpers

| Jumper | Wanted | Why |
|---|---|---|
| **JP6 / J13 `[1-2]`** | **fitted (factory default)** | Selects the 8.192 MHz on-board oscillator Y1. **CLKIN is mandatory** — with no clock the chip still answers register reads but *never converts*. It presents as frozen data, not as an error. |
| **JP5** | **NOT fitted** | JP5 powers Y1 **down**. Fitting it kills the clock. |
| **JP1–JP4 `[3-4]`** | fitted (factory default) | Both inputs of each channel grounded through 1 kΩ. That *is* the configuration the shorted-input noise test (T7) wants — leave it until real sensors go on. |

`EVM J6[3]` is the ADC's CLK pin — **leave it unconnected**. Y1 drives it.

---

## What the driver does differently from `ADS1263_Driver`

Detail lives in the header comment of
[`lib/ADS131M04/ADS131M04_Driver.h`](lib/ADS131M04/ADS131M04_Driver.h); the
short version, because these are the things that bite:

- **Every transaction is a fixed 6-word frame** (18 bytes at the default 24-bit
  word length). There is no per-channel read command — one NULL frame returns
  STATUS *and* all four channels together.
- **The response word lags one frame.** DOUT word 0 answers the *previous*
  frame's command. `readRegister()`/`writeRegister()` are therefore two frames
  each, and they absorb the lag so the next frame a caller runs always carries
  STATUS, never a stale ack.
- **Output CRC is always on** and is the driver's validity gate, replacing the
  ADS1263's checksum byte. `crcErrors()` counts failures.
- **SYNC/RESET: the short pulse is the one that does *not* reset.** ≥2048 t_CLKIN
  (250 µs) resets; 1–2047 t_CLKIN synchronises instead, leaving configuration
  intact and the chip looking healthy. `reset()` holds low for 1 ms.
- **Full scale is ±1.2 V/gain** from a fixed internal reference — not the ±5 V
  the REF7050 gives the ADS1263. A 0–5 V sensor clips into a flat rail that
  reads like a stuck sensor. See plan §2.2.

The rig's convention is that the ADC driver is **copied** between firmware
projects, not shared. This is the one copy that exists; it moves to the
production fork only if Stage 3 passes.

---

## Scope — what is *not* here yet

The smoke test in `src/main.cpp` covers plan §7 **T1** (ID) and **T2** (register
round-trip) and then prints a 1 Hz live summary. The full bench console of plan
§5 — the `spi` clock ladder (T3), `stream`/`noise` (T4–T7), `netcfg` and the UDP
sample stream, and the `tools/m04_bench.py` host script — is **not written yet**.
