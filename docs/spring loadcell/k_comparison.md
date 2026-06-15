# Spring constant: Instron 100N-cell vs GSO-50

Same BMX die spring measured on two load cells; the question is whether they
agree, with the caveat that the 100N-cell is below its in-spec floor for most
of the GSO-50's range.

## Numbers

| Source            | k (mN/mm) | R²       | RMS resid | Notes                          |
|-------------------|-----------|----------|-----------|--------------------------------|
| Instron run 1     | 30.738    | 0.999998 | 0.29 mN   | in-spec only (F > 400 mN)      |
| Instron run 2     | 30.891    | 0.999998 | 0.26 mN   |                                |
| Instron run 3     | 30.900    | 0.999998 | 0.25 mN   |                                |
| Instron run 4     | 30.909    | 0.999998 | 0.24 mN   |                                |
| **Instron pooled**| **30.859 ± 0.081** | — | — | run-to-run CV 0.26%        |
| GSO-50 (all)      | 31.020    | 0.999990 | 0.42 mN   | step means, fwd+rev            |
| GSO-50 fwd        | 31.154    | 0.999966 | 0.79 mN   |                                |
| GSO-50 rev        | 30.893    | 0.999990 | 0.40 mN   |                                |

**Δ = +0.16 mN/mm (+0.52%)** GSO-50 high vs Instron pooled.

Per-direction GSO-50 brackets Instron: fwd +0.95%, rev +0.11%. The fwd/rev
spread (~0.85%, ≈4–5 mN over the 14.5 mm sweep) is the
hysteresis already logged in `Calibrate_LoadCell/calibration.json`
(`hysteresis_mN_equivalent = 4.98 mN`) — that is spring + fixture, not the
amp (LCA-RTC nonlinearity spec is 120× tighter).

## Honesty about circularity

The GSO-50 sensitivity (`10.201 mV/mN`) was fit from this very run, using
`expected_force = 30.86 · displacement` as the truth column. So k_GSO ≈
k_Instron is partly by construction — what is *not* circular and what we
actually learn:

- **Linearity**: GSO-50 voltage-vs-displacement R² = 0.99999, residual RMS
  0.4–0.8 mN over 0–14.5 mm. Same as Instron in-spec.
- **Hysteresis**: ~5 mN, mechanical, not the amp. Propagate as known
  uncertainty downstream.
- **Slope mismatch**: 0.5% combined / 0.6% ADC1-vs-ADC2 sits inside the
  Instron run scatter (0.26%) plus 100N-cell in-spec uncertainty (~0.3%).
  No evidence the GSO-50 disagrees with Instron beyond what either
  instrument's own variation explains.

## Where each cell wins (and the floor problem)

The 100N-cell's in-spec floor is **400 mN ≈ 40.8 gf** (0.4% of capacity).
For this spring that floor is hit at **~12.96 mm**. Below that point:

- Instron force readings have ≥1% scale error and the residuals plot shows
  visible drift in the 0–13 mm band (left red strip in the residuals
  panel).
- GSO-50 (50 gf = 490 mN FS) is fully in-spec across 0–14.5 mm, with sub-mN
  step-mean noise.

The two instruments are complementary, not redundant:

- **0–13 mm (0–400 mN)**: trust GSO-50.
- **13–34 mm (400–1050 mN)**: trust Instron; GSO-50 saturates above ~50 gf.
- **Cross-check band 13–14.5 mm**: both in-spec, slope agreement 0.5%.

## Recommendation

Report k = **30.86 mN/mm** as the spring constant (Instron pooled, in-spec).
Keep the GSO-50 ±5 mN hysteresis as the uncertainty budget for any
downstream force-from-GSO50 reading. Re-running the GSO-50 calibration
against an independent reference (dead-weight, or NIST-traceable mass) is
the only way to break the present circular anchor; until then the agreement
is a consistency check, not an independent verification.

## Files

- `k_comparison.png` — 3-panel plot (overlay, low-force zoom, residuals)
- `k_comparison.txt` — raw numerical output from `compare_k.py`
- `compare_k.py` — analysis script (in outputs)
