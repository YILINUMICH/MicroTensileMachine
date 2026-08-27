# data/ — ADS131M04 evaluation captures

Flat on purpose. Per the root `CLAUDE.md`, a module's `data/` is converted to
the `raw/` + `derived/` split only once it starts mixing code, captures and
outputs — this one holds captures and their reports side by side and has not
earned the split.

One folder per sweep: `m04_<stamp>[_<tag>]/`, written by
`operator_m04_sweep.py`. Inside:

| file | written by | what it is |
|---|---|---|
| `<condition>.csv` | sweep | the raw stream: `src,hw_us,value,raw_code,seq` |
| `<condition>.meta.json` | sweep | the condition + UDP loss accounting |
| `<condition>.console.log` | sweep | serial text, including the `[STATUS]` frames the report reads `crc_err` / `frames` from |
| `qualify.json` | sweep | copy of the profile that produced the run |
| `run_meta.json` | sweep | port, transport, src map, start time |
| `report.txt` | report | per-condition verdicts against plan §7 |
| `summary.csv` | report | one row per condition × channel |

**`src` here is NOT the production meaning.** CH0/CH1 keep `src=1`/`src=2`
(laser/load) so the Stage-3 M4 swap is a no-op for the host, but CH2/CH3 borrow
`src=3`/`src=4`, which in production mean SMA voltage/current. Harmless because
this firmware is standalone — but it is why these captures must never be fed to
`Experiment_SMAThermalCharacterization/analysis/`.

Captures are committed, like every other module's results.
