# STATUS — Firmware_ADS131M04Test_PIO

**Status:** **To-Test** — all four envs build clean, **nothing has been flashed
and no hardware exists yet**. Treat every number this firmware prints as
unverified until plan §7's T1–T9 have actually been run.

**Bench procedure:** [`../docs/MEMO_ADS131M04_bringup.md`](../docs/MEMO_ADS131M04_bringup.md)
— wiring → connection → registers → configuration → conversion → UDP → sweeps.

**Branch:** `feat/ads131m04`. **Plan:** [`../docs/ADS131M04_migration_plan.md`](../docs/ADS131M04_migration_plan.md).

---

## 2026-08-27 — driver written against the plan

`ADS131M04_Driver` (h + cpp) written from SBAS890D, plus a minimal M7 bring-up
sketch and an M4 idle stub. `portenta_m7`, `portenta_m7_trace` and
`portenta_m4_idle` all build clean under `-Wall -Wextra` with zero warnings.

Two datasheet details worth recording, because both are silent failures rather
than errors:

- **SYNC/RESET pulse widths are the opposite way round from what a `pdftotext
  -layout` dump of Table 6.6 suggests.** Reset is t_w(RSL) **≥ 2048 t_CLKIN
  (250 µs)**; **1–2047 t_CLKIN is a *synchronise*** — filters realign, registers
  keep their values, chip keeps streaming and looks fine. A few-microsecond
  "reset" would therefore silently not reset. `reset()` holds low 1 ms.
- **DRDY at the default `MODE.DRDY_FMT = 0b` is a LEVEL, not a pulse** — it
  asserts on a new conversion and holds low until the data are read. So
  `waitDataReady()` is a level poll that returns immediately when data is
  already pending. (An early draft hunted for an edge and would have blocked
  until timeout in exactly that case.) Setting `DRDY_FMT = 1b` turns the pin
  into a ~0.5 µs pulse that polling cannot see; the driver never sets it.

### Test progress — plan §7 *(SUPERSEDED by the entry below)*

| | Test | State |
|---|---|---|
| T1 | ID reads `0x24xx` | not run — no hardware |
| T2 | register round-trip | coded in `src/main.cpp`, not run |
| T3 | SPI clock ladder | **not implemented** (needs the `spi` console command) |
| T4 | CRC integrity ≥10⁶ frames | **not implemented** (needs `stream`) |
| T5 | rate accuracy ±1% | partial — the 1 Hz summary prints a rate, but not over a long enough window |
| T6 | DRDY edge count | **not implemented** |
| T7 | shorted-input noise ≤2× 2.39 µV rms | **not implemented** (needs `noise`) — **this is the test that can kill the whole idea** |
| T8 | DC accuracy | **not implemented** |
| T9 | reset recovery | `reset()` and `resetCommand()` exist; no console command to trigger them |

## 2026-08-27 (later) — full M7 application; console replaced by host sweeps

The planned interactive console was dropped in favour of scripted sweeps from
`Experiment_ADS131M04Eval/` (plan §5.1). This firmware now provides the command
surface those sweeps drive, plus the session contract the host's
`lib_h7_session` requires:

- commands `selftest / regs / rst / spi / osr / gain / poll / drdy / netcfg /
  ping / help`, with **unknown commands ignored rather than wedging** — the host
  session sends a few this image does not know
- `[STATUS]` at 1 Hz, numeric key=value only (the host's regex accepts nothing
  else), carrying `udp_on`, `crc_err`, `frames`, `drdy`, `rate`, `adc_ok`
- the sample stream in the **production wire format** over UDP after `netcfg`,
  batched to ≤1400 B of whole lines
- `portenta_m7` now defaults to `-D H7_TRANSPORT_UDP=1`; `portenta_m7_usb` is
  the no-Ethernet rollback

All four envs build clean under `-Wall -Wextra` with **no warnings from our own
sources** (the remaining ones are inside mbed's SocketWrapper).

**Sampling is DRDY-gated, a deliberate departure from the ADS1263 path.** That
path used blind timed polling because its DRDY was ADC1-only and an ISR waiting
on edges freezes when they stop. Neither applies here: one DRDY covers all four
channels, and at the default `DRDY_FMT=0` it is a LEVEL held low until the data
is read — so a non-blocking level check in the main loop cannot hang. Every
conversion is then read exactly once, which removes the duplicate-row problem
the production stream has (~19% zero-order-hold rows, from polling faster than
the ADC converts), and makes T6 free: conversions consumed == DRDY assertions.
`poll <us>` switches to timed polling for an A/B.

`adc.begin()` failing no longer halts: `[STATUS]` keeps flowing with `adc_ok=0`,
so the host sees a diagnosable board instead of a dead port indistinguishable
from a bad cable.

### Test progress — plan §7 (CURRENT)

| | Test | State |
|---|---|---|
| T1 | ID reads `0x24xx` | **implemented** (`selftest`, boot) — unrun |
| T2 | register round-trip | **implemented** (`selftest`) — unrun |
| T3 | SPI clock ladder | **implemented** — `spi <hz>` + host ladder + adoption logic |
| T4 | CRC integrity ≥10⁶ frames | **implemented** — `t4_soak` cell, `crc_err`/`frames` in `[STATUS]` |
| T5 | rate accuracy ±1% | **implemented** — `osr <code>` + host rate check off `hw_us` |
| T6 | DRDY count | **partial** — `drdy` counts conversions consumed and is exact under DRDY gating; no automated host check yet |
| T7 | shorted-input noise | **implemented** — `gain <ch> <g>` + host check vs Table 7-1 |
| T8 | DC accuracy | **not implemented** — needs a known DC source and a nominal |
| T9 | reset recovery | **partial** — `rst` exists; no host condition drives it mid-capture |

### Next

Flash it and walk [`../docs/MEMO_ADS131M04_bringup.md`](../docs/MEMO_ADS131M04_bringup.md).
**Nothing here is bench-verified** — every T above is unrun.

### Not started

Stage 2 (real sensors), Stage 3 (M4 swap + ring + UDP), Stage 4
(recalibration). Stage 2 needs the ÷6 divider board built first — the
attenuation question is **settled** (plan §3.1: one 10 kΩ series resistor per
channel, no capacitor), it just is not built.
