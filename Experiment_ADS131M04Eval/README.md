# Experiment_ADS131M04Eval

Host-side qualification of the **ADS131M04** driver — the sweep runner and the
report that turns `docs/ADS131M04_migration_plan.md` §7's T-list into computed
pass/fail verdicts.

**Firmware lives separately** in [`../Firmware_ADS131M04Test_PIO/`](../Firmware_ADS131M04Test_PIO/).
**Plan:** [`../docs/ADS131M04_migration_plan.md`](../docs/ADS131M04_migration_plan.md).
**Status:** [`STATUS.md`](STATUS.md).

---

## Why a sweep and not a console

The T-list is already a set of parameter ladders — T3 sweeps the SPI clock, T5
sweeps OSR, T7 sweeps gain — and every acceptance criterion is a *measurement
over a held condition*, not something to read off scrollback. Capturing to files
makes a run reproducible, diffable against the next one, and committed with the
results. This mirrors `Experiment_SMAThermalCharacterization`'s sweep + report
split, for the same reasons.

## Running

```
# ALWAYS dry-run first — prints the plan without opening the port
python operator_m04_sweep.py --profile profiles/qualify.json --dry-run
python operator_m04_sweep.py --profile profiles/qualify.json

# then ALWAYS
python operator_m04_report.py data/m04_<stamp>
```

Ad-hoc ladders without a profile:

```
python operator_m04_sweep.py --spi-ladder 0.5,2,8,16 --secs 60      # T3
python operator_m04_sweep.py --osr-ladder 5,6,7 --secs 60           # T5
python operator_m04_sweep.py --gain-ladder 1,2,4 --secs 60          # T7
```

## Before every run

- **Flash `portenta_m4_idle` first.** M4 drives the same SPI1 bus and the same
  CS pin; leave the resident sampler running and the cores fight over the bus.
  It presents as intermittent CRC errors that look exactly like a cable fault.
- **Power-cycle USB + EVM** after the upload.
- **EVM jumpers:** JP6 fitted `[1-2]` (Y1 8.192 MHz — CLKIN is *mandatory*),
  JP5 **not** fitted (it powers Y1 down). For the T7 cells leave JP1–JP4 at the
  factory `[3-4]`, which grounds every input through 1 kΩ — that *is* the
  shorted-input condition T7 wants.

## Files

| | |
|---|---|
| `operator_m04_sweep.py` | walks a ladder of conditions, one capture each |
| `operator_m04_report.py` | judges a sweep folder against plan §7 |
| `lib_m04.py` | datasheet specs + acceptance thresholds, in one place so the sweep and the report can never disagree |
| `profiles/qualify.json` | the full T3 + T5 + T7 + T4 ladder (~25 min) |
| `data/` | captures and reports — see [`data/README.md`](data/README.md) |

## What the firmware must provide

The sweep reuses `lib_h7_session` from the thermal module (via a `sys.path`
shim), so the board has to satisfy that session contract:

- a `[STATUS]` frame at 1 Hz on serial carrying `udp_on` — plus `crc_err` and
  `frames`, which the report reads
- `netcfg <ip> <port>`
- `ping` accepted as a no-op
- unknown commands ignored, never wedging the parser
- samples as TSV over UDP in the production wire format

Plus the M04-specific commands the sweep drives: `spi <hz>`, `osr <code>`,
`gain <ch> <g>`, `rst`, `selftest`.

The sweep passes `cal=()` so the SMA calibration commands are never sent, and
never calls `disarm()` — there is no actuator here.

## A frozen converter reads as a *perfect* noise result

Which is why `rate` and `loss` are checked alongside `noise` and are not
optional extras. The failure is real on this part: with CLKIN absent the chip
still answers register reads and simply never produces new conversions. The
rate check derives SPS from `hw_us` — the ADC's own timeline — not from the
wall clock, so a stalled converter cannot hide behind a healthy-looking capture
duration.
