> **Status: WIP**. See [STATUS.md](STATUS.md). See [../README.md](../README.md) for project overview.

# FilterDesign — anti-alias / noise RC filters for the four ADC channels

Sizing the analog input filtering for the rig's four measurement channels:

| channel | ADC | pin | rate | the problem |
|---|---|---|---|---|
| laser `src=1` | ADS1263, Sinc3 | AIN4/5 | 400 SPS | a **65.8 Hz line** carrying 59 % of the variance — in-band |
| load `src=2` | ADS1263, Sinc3 | AIN2/3 | 400 SPS | **broadband**, 163 µV/√Hz — 50× the laser on the same ADC |
| V_sma `src=3` | H7 on-chip 16-bit | A0 (10k/10k) | ~1 kHz | ~100 % of AC energy **above Nyquist**, no anti-alias filter |
| I_sense `src=4` | H7 on-chip 16-bit | A1 (INA296A) | ~1 kHz | same, and δI/I is 3× δV/V so it does **not** cancel in R |

The two halves want different treatments and rest on different evidence, so
keep them apart when reasoning about a fix.

**A0/A1 — the case is made and quantified.** `Experiment_RNoise` measured the
rail with a scope: a 24.414 kHz tone with a harmonic series to 460 kHz,
158–166 mV rms against a 2.45 µV rms datasheet spec, growing with load current
and present on the LDO output itself. Essentially all of it is above the ~1 kHz
sampler's Nyquist and folds in. Its `STATUS.md` §3.5 already sized the filter
from that spectrum — a 10 kHz pole buys only 3×, **2-pole at 300 Hz buys 6600×**
— and flags two constraints: A0 sits behind 5 kΩ so it **needs a buffer** (a
bare series R starves the S/H), and **both channels must get identical filters**
or R is distorted during transients. Read that section before designing.

**Laser/load — partially evidenced, and the gap is real.** The in-band noise is
characterised (`noise_20260728_125440_quiet_baseline`, plus the shorted-input
split), but **nothing in the repo has ever looked at the ADS1263 inputs above
200 Hz**. So: an RC can be sized against what is known to be in-band, but
whether there is out-of-band energy worth filtering is currently unmeasured.
Two things the in-band data already says:

- The laser's dominant noise is a **spectral line at 65.8 Hz**. No RC that
  preserves a 100 ms transient will touch it — that wants a notch.
- The load cell's noise splits ~evenly sensor-side / amplifier-side (1.52×
  shorted vs connected), so a front-end fix caps out at ~1.5× on its own.

## Contents

| path | what |
|---|---|
| `prepare_filter_data.py` | builds the dataset below from captures already in the repo |
| `data/` | the dataset — see [`data/MANIFEST.md`](data/MANIFEST.md) |
| `data/index.csv` | machine-readable file index (rate, rows, drive condition, caveats) |

## The dataset

```
A  ADS1263 noise floor at rest ....... laser + load, 60 s, SMA disarmed
B  same, load cell shorted at the amp . the ceiling on any front-end fix
C  V/I at the deployed ~1 kHz ........ 6 operating points = residual after aliasing
D  scope at 10 MSa/s ................. the ONLY above-Nyquist data (A0/A1 only)
E  one heat cycle, all four channels .. the signal the filter must not destroy
```

D is the only set that can *size* a pole; E is the only set that can *price*
one. C shows what survives today; A/B show the floor you are working against.

```bash
python prepare_filter_data.py      # regenerates data/ (~146 MB, full resolution)
```

Requires `numpy` + `pandas`. Reads, but never writes,
`Experiment_SMAThermalCharacterization/` and `Experiment_RNoise/`; dataset E
imports `analysis/get_cycle.py` from the former rather than reimplementing the
clock alignment and heat-window detection.

## Related

- [`../Experiment_RNoise/STATUS.md`](../Experiment_RNoise/STATUS.md) — the scope
  campaign, the filter sizing table (§3.5), the interim 10 Hz digital mitigation
  (§3.6) and why it is a band-aid, and the open question of whether the TPS7A57
  is oscillating (§3). **Filtering a 158 mV oscillation would hide a real
  actuation problem behind a clean-looking measurement** — that module's step 6
  says size the RC *after* the source is resolved.
- `../Experiment_SMAThermalCharacterization/data/raw/troubleshoot/noise_*` — the
  two in-band noise write-ups for laser and load.
