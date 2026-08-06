# `data/derived/` — what the pipeline computed

Everything here is **regenerable** from `../raw/` by the scripts in
`../../analysis/`. It is committed anyway so analysed results travel with a
clone, and so a change in a constant or a fixed bug shows up as a diff instead
of vanishing.

Nothing in this folder should ever be hand-edited. If a number here is wrong,
the fix belongs in the script that wrote it.

## Layout — mirrors `../raw/campaigns/` (reorganized 2026-08-06)

```
data/derived/
  README.md
  campaigns/
    20260730_dynalloy_15mm_cool15s/    superseded diagnostic outputs
    20260731_dynalloy_15mm_cool30s/    the 15 mm-wire campaign
    20260805_dynalloy_10mm/            the 10 mm-wire campaign
```

**The folder name is shared with `../raw/campaigns/`** — it comes from the same
`dir` field of `CAMPAIGNS` in `analysis/analyze_raw.py`, so a campaign's
captures and its analysed results can never drift apart.

This matters most for the outputs whose **filename says nothing about
provenance**. `trajectory_400ms_abs.png`, `energy_collapse.html`,
`self_sensing.html`, `transition_400ms.html` and `r_bias_artifact.html` are all
**15 mm-wire results**, and they used to sit flat in this folder next to 10 mm
results with no way to tell them apart. The `heat_time_map_*` files carry their
campaign in the filename; those five never did.

A figure follows its captures: `plot_drive_trajectory.py` reads the sweep's
parent folder under `../raw/` and writes beside it. A merge that **spans**
campaigns has no single home and lands at this folder's root — visibly, rather
than being filed under one of the campaigns it only half belongs to.

## What writes what

| file | written by | reads |
|---|---|---|
| `heat_time_map_<campaign>_all.csv` | `analyze_raw.py` | raw captures |
| `heat_time_map_<campaign>_all_meta.json` | `analyze_raw.py` | calibration snapshot — records the constants in force when the table was built |
| `*_envelope.csv`, `*_stroke.png`, `*_force.png` | `plot_envelope.py` | the table above |
| `drive_<sweeps>_<400ms\|200mA>.png` | `plot_drive_trajectory.py` | raw captures directly — **no table needed** |
| `trajectory_<heat>ms_<abs\|shape>.png` | `plot_trajectory.py` | table + raw · **July-pinned** |
| `*_all_energy.csv` | `energy_table.py` | table + raw — cached ∫P dt |
| `energy_collapse.html` | `plot_energy.py` | `energy_table.load()` · **July-pinned** |
| `self_sensing.html` | `plot_selfsensing.py` | `energy_table.load()` · **July-pinned** |
| `transition_<heat>ms.html` | `plot_transition.py` | `energy_table.load()` · **July-pinned** |
| `r_bias_artifact.html`, `r_bias_points.csv` | `plot_r_bias.py` | `energy_table.load()` · **July-pinned** |
| `heat_time_map_20260730_*` | `diagnostics/make_heat_time_map_clean.py` | **SUPERSEDED** — kept for its write-up only |

**"July-pinned" is a real limitation, not a label.** `energy_table.py` hardcodes
the 2026-07-31 table, and every script that imports its `load()` inherits that
pin — so those five outputs show 15 mm-wire results no matter which campaign was
analysed last. Pointing them at another campaign needs a `--campaign` argument
that does not exist yet. `plot_trajectory.py` is pinned separately, through a
hardcoded `SRC_MAP`.

## Regenerating

```
cd analysis
python analyze_raw.py                                    # tables, all campaigns
python plot_envelope.py ../data/derived/campaigns/<dir>/heat_time_map_<campaign>_all.csv
python plot_drive_trajectory.py --all                    # every sweep figure
```

`analyze_raw.py` is byte-reproducible (LF pinned), so re-running it on a
different host produces no diff. The PNG writers are **not**: matplotlib
rasterizes slightly differently across platforms and font versions, so
re-rendering an unchanged figure on another machine yields a few-KB binary diff
with identical content. Do not commit that churn.

See `../raw/INDEX.md` for which captures each campaign holds, and `../raw/README.md`
for the capture format.
