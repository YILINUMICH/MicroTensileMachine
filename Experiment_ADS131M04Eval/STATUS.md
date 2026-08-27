# STATUS — Experiment_ADS131M04Eval

**Status:** **To-Test** — the sweep and report run end to end against synthetic
captures, but **no hardware exists and nothing has been captured**. The firmware
side of the contract is not written yet, so a real run is not yet possible.

**Branch:** `feat/ads131m04`. **Plan:** [`../docs/ADS131M04_migration_plan.md`](../docs/ADS131M04_migration_plan.md) §5, §7.

---

## 2026-08-27 — created; sweep + report replace the planned console

The plan originally specified an interactive bench console. Replaced with a
sweep runner plus a report, following `Experiment_SMAThermalCharacterization`:
the T-list is already a set of parameter ladders, and every acceptance criterion
is a measurement over a held condition rather than something to eyeball.

Verified against synthetic captures covering three deliberate failures — all
three were caught, and the T3 adoption logic picked the right clock:

```
[PASS] t3_spi_0500k   [PASS] t3_spi_2000k   [PASS] t3_spi_8000k
[FAIL] t3_spi_16000k    FAIL crc    137 errors over 40000 frames
[FAIL] t5_osr_16256     FAIL rate   worst 267.1 SPS vs 252.0 nominal (+6.00%)
[FAIL] t7_gain_1        FAIL noise  worst ch0 9.08 uV vs 4.78 uV limit
T3: clean SPI clocks ['0.5M', '2M', '8M']
    fastest clean = 8 MHz -> ADOPT 2 MHz (one step back)
```

Two defects found and fixed during that check, both of which would have produced
quietly wrong reports rather than errors:

- **Report order was alphabetical**, so a clock ladder printed `16000k` before
  `2000k`. Conditions now carry a `seq` stamped at run time and sort on it.
- **T3's "clean clock" list keyed on the OVERALL verdict.** A condition failing
  on noise or rate would have removed its SPI clock from the clean list, even
  though neither says anything about link integrity — silently shortening the
  ladder and biasing the adopted clock downward. Now keyed on the `crc` check
  alone.

Also made every `read_text`/`write_text` explicitly UTF-8: they default to
cp1252 on this host, which would mangle or throw on any non-ASCII byte the
firmware emits into a console log.

### Test progress — plan §7

| | Test | State |
|---|---|---|
| T1 | ID reads `0x24xx` | firmware-side; report surfaces `[T1]` from `selftest`, unrun |
| T2 | register round-trip | firmware-side; report surfaces `[T2]`, unrun |
| T3 | SPI clock ladder | **profile + adoption logic ready**, unrun |
| T4 | CRC integrity ≥10⁶ frames | **ready** (`t4_soak`, 900 s), unrun |
| T5 | rate accuracy ±1% | **ready**, unrun |
| T6 | DRDY edge count | **not implemented** — needs a DRDY counter in `[STATUS]` |
| T7 | shorted-input noise | **ready** — the deciding test, unrun |
| T8 | DC accuracy | **not implemented** — needs a known DC source and a nominal |
| T9 | reset recovery | **not implemented** — needs `rst` mid-capture |

### Blocking

The firmware. It must provide `[STATUS]` (with `udp_on`, `crc_err`, `frames`),
`netcfg`, `ping`, `selftest`, `spi`, `osr`, `gain`, `rst`, and the UDP sample
stream. Until then `--dry-run` is the only thing that runs.

### Not started

T6/T8/T9 conditions; anything involving real sensors (that is plan Stage 2, and
needs the ÷6 divider board built first).
