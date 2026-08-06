# sweep_full_150-950mA — combined actuation curve, 150 → 950 mA

Two sweeps merged into one folder (2026-07-30). File suffixes give provenance:

- `*_20260729` — `sweep_20260729_002212` (150–650 mA, 6 levels, ~5 verdict
  cycles each). Its session README (mistake list) is `README_20260729.md`;
  its original figures are `fig*_20260729.png`.
- `*_20260730` — `sweep_20260730_023620` (650–950 mA, 4 levels, 3 verdict
  cycles each; `--max-ma 1000`).

Both runs: `cccycle` 100 ms heat / 12 s cool, i_low 100 mA (loop closed
through cool), cycle 1 = bootstrap ramp (excluded from all verdicts).

## Uniform re-analysis

`summary_combined.csv` was produced by re-analysing EVERY level from raw
through the same path the live sweep now uses — M4→M7 clock offset parsed from
each capture's own STATUS lines (+2.193…+2.195 s, all 10 files), `align_m4()`,
then `analyse_level()` — so the 0729 numbers come from identical code to the
0730 ones. Do not mix with the per-run `summary_2026072*.csv` (the 0729 one
predates the dx columns and the aligned analysis).

Conversions: laser `Calibrate_LaserHead` fit (k = −0.4978 µm/mV,
V0 = 2503.75 mV); force `Calibrate_LoadCell` fit (10.2009 mV/mN).
dx is the SIGNED mean over verdict cycles.

## The curve (level means, verdict cycles only)

| run | cmd (mA) | achieved (mA) | Δx (µm) | ΔF (mN) |
|---|---|---|---|---|
| 1 | 150 | 156 | +13.4 | 2.2 |
| 1 | 250 | 250 | +49.7 | 3.2 |
| 1 | 350 | 346 | +84.0 | 4.3 |
| 1 | 450 | 466 | +167.6 | 7.2 |
| 1 | 550 | 542 | +251.6 | 9.4 |
| 1 | 650 | 640 | **+367.3** | 12.8 |
| 2 | 650 | 644 | **+363.6** | 13.2 |
| 2 | 750 | 792 | +586.7 | 20.2 |
| 2 | 850 | 842 | +663.0 | 22.5 |
| 2 | 950 | 939 | +864.0 | 28.8 |

Figure: `fig_actuation_150-950mA.png` (per-pulse scatter + level means).

## Observations

- **Cross-day repeatability:** the 650 mA level was run in both sessions,
  independently, and agrees to 1% (367.3 vs 363.6 µm) — the two runs can be
  treated as one dataset.
- **Monotonic and superlinear on both channels across the full span.** No
  plateau by 939 mA: the transformation is not saturated even at ~0.86 mm
  stroke.
- **Nothing electrical or mechanical limits at 950 mA.** Load cell peaked at
  2.33 V (≪ 5 V rail) and ~29 mN (≪ 490 mN rating); CC achieved 99% of
  command at 850/950; **no force-baseline ratchet** at any level — the
  suspected >750 mA damage signature from 2026-07-28 did not reappear with
  100 ms pulses / 12 s cools.
- **The 750-command level is the outlier in CONTROL, not response:** achieved
  currents ran 757–857 mA across its cycles (overshoot converging downward,
  mean 792) where every other level held ±1%. Plotted against *achieved*
  current its points still sit on the same curve. Likely the `R_est`
  single-sample bootstrap gap (see STATUS: CC BOOTSTRAP + REACHABILITY).
- The RNN current range can therefore span **150–950 mA**; the binding
  constraint found so far is the LDO voltage ceiling (~5.2 V / R_wire
  ≈ 1.1 A), not any sensor.
