# FilterDesign — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP** — dataset built and cross-checked (2026-08-19). No filter designed, no hardware changed. |
| **Role** | Size the analog input filtering (anti-alias + noise) for the four ADC channels: laser, load cell, V_sma (A0), I_sense (A1). |
| **Owner** | Yilin |
| **Quick test** | `python prepare_filter_data.py` — regenerates `data/` from repo captures; no hardware needed. |

## 2026-08-19 — dataset assembled from existing captures

`prepare_filter_data.py` builds 34 CSVs + `index.csv` into `data/` (~146 MB,
full resolution). Sources: `Experiment_SMAThermalCharacterization/data/raw/`
(quiet-baseline noise, shorted-input test, six `isense_*` runs, two sweeps) and
`Experiment_RNoise/out/scope/` (four 10 MSa/s scope captures). Nothing new was
measured. See [`data/MANIFEST.md`](data/MANIFEST.md).

Corrections applied once, in the prep, rather than in every downstream notebook:
`hw_us` unwrapped per `src`; ZOH duplicates removed on `src=1/2` with `n_held`
kept; M4/M7 clock offset applied in dataset E; scope volts re-derived from raw
int8 codes at **30 codes/div**, not the 25 the idle-baseline file has stored.

**Verified against published numbers before use:**

| check | this dataset | reference |
|---|---|---|
| laser idle mean | 2.724416 V | 2.724 V (noise README) |
| load idle RMS (per-segment, uniform grid) | 4.755 mV | 4.749 mV |
| load shorted RMS | 3.106 mV | 3.122 mV |
| scope `supplyA` C2 | 105.07 mA rms | 105.0 mA (RNoise §3.5) |
| scope `ldoOut` C1 | 158.25 mV rms | 158–166 mV (RNoise §2.1) |
| E 850 mA × 400 ms cycle 3 | dx 6781 µm, dF 215.8 mN, r_hot 4.042 Ω | 6788 / 216.0 / 4.033 (`cycles.csv`) |

The E `_joined` files are interpolated onto a union index, which is why hot-state
medians land ~0.4 % off `cycles.csv`; the `_raw` files are un-interpolated and
are the ones to use for spectra.

## Open

- **Laser and load have no above-Nyquist measurement anywhere in the repo.** A/B/C
  stop at 200 Hz. Sizing an RC for the ADS1263 inputs on evidence rather than
  assumption needs a scope capture at AIN2/3 and AIN4/5 with the rig driving —
  the same shape as the `Experiment_RNoise` PHASE 2 campaign. Deferred by
  decision, not by oversight.
- **A0/A1 filter should not be finalised before the LDO source question closes.**
  `Experiment_RNoise/STATUS.md` §4 step 6: filtering a 158 mV oscillation would
  hide a real actuation problem behind a clean-looking measurement. Its step 1
  (parallel a second 22 µF at the LDO output) is a 2-minute decisive test and is
  still outstanding.
- Not registered in the root `README.md` module table yet.

See [../README.md](../README.md) for project overview.
