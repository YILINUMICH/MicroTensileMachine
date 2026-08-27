# STATUS — Firmware_ADS131M04Test_PIO

**Status:** **To-Test** — all three envs build clean, **nothing has been flashed
and no hardware exists yet**. Treat every number this firmware prints as
unverified until plan §7's T1–T9 have actually been run.

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

### Test progress — plan §7

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

### Next

Build out the plan §5 bench console (`spi`, `osr`, `gain`, `stream`, `noise`,
`rst`, `netcfg`) and `tools/m04_bench.py`, so T3–T9 can be run at all. Until
then this project can only demonstrate that the chip answers — not that it
measures anything correctly.

### Not started

Stage 2 (real sensors), Stage 3 (M4 swap + ring + UDP), Stage 4
(recalibration). No wiring exists; the attenuation question in plan §12 is still
open and blocks Stage 2.
