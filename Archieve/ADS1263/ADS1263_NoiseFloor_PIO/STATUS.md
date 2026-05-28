# ADS1263_NoiseFloor_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **Diagnostic — bench-verified 2026-05-24.** All 42 (SPS × PGA) cells reported in-spec noise; offset stability across SPS within 12 nV at gain 1; NFB 17.0–22.8 bits across the surface (datasheet typical 17–18). Two cells flagged stuck_pct just above 0.5% threshold (50 SPS gain 2 = 0.6%, gain 4 = 0.8%) — judged counting-statistics noise, not operationally significant. Data: [`data/noisefloor_20260524_1845.csv`](data/noisefloor_20260524_1845.csv). |
| **Role** | Phase 1.2 characterization sketch. Walks every combination of (SPS ∈ {10, 50, 100, 400, 1200, 2400, 4800}) × (PGA ∈ {1, 2, 4, 8, 16, 32}) with AINCOM-shorted inputs (INPMUX = 0xAA) and VBIAS on. Streams a 42-row CSV over USB serial. M7-only, no M4, no shared driver. |
| **Created** | 2026-05-24 (with [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md)). |
| **Owner** | Yilin |
| **Prereq** | `ADS1263_FirstPowerUp_PIO/` cp0–cp6 must pass first. In particular cp6 (VBIAS + PGA mini-sweep) confirms that VBIAS biasing actually works at every PGA gain — this sketch assumes that's been verified. |
| **Quick test** | `pio run -t upload && pio device monitor 2>&1 \| tee data/noisefloor_$(date +%Y%m%d_%H%M).csv` then strip `#` lines and feed to `tools/analyze_noise_floor.py`. Total runtime ≈ 5 min. |
| **Dependencies on other modules** | None — fully standalone. Inline SPI helpers, no `lib/ADS1263/`, no V1/V2 imports. Deliberately duplicates the helpers from `ADS1263_FirstPowerUp_PIO/` so future driver consolidation can merge cleanly. |

## What success looks like

For each (SPS, PGA) cell, the input-referred RMS noise (`in_rms_uV` column) should sit within ±50% of the ADS1263 datasheet Table 7.10 typical value. Cells outside that envelope are flagged by `tools/analyze_noise_floor.py` and identify off-limits operating modes for downstream firmware.

Specific anchors:

- At (SPS=400, PGA=1): should match the bring-up baseline within counting-statistics error (bring-up: 1.4 µV RMS with only N=100; this sweep uses N=2000, so the value should be more precise, ≈ 1–2 µV).
- At higher gain, input-referred RMS should generally drop (the PGA's intrinsic noise becomes small compared to its gain).
- At higher SPS, RMS should rise (less filter averaging time per sample).
- `stuck_pct` column should be 0 for all rows. Non-zero means the SPI loop is polling faster than the chip is converting — at 500 kHz SPI this shouldn't happen below 4800 SPS, but it's worth catching.

## Module TODOs

- [ ] **Bench-run it.** Capture to `data/noisefloor_YYYYMMDD_HHMM.csv`. Update the result log in [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md).
- [ ] **Compare to datasheet Table 7.10.** Use `tools/analyze_noise_floor.py`. Any row flagged > 1.5× datasheet typical: investigate before trusting that operating mode in downstream code.
- [ ] **(Stretch) Extend SPS table** to 7200 / 14400 SPS — needs SPI bumped from 500 kHz to ≥ 1 MHz first. Hold off until the existing range is verified clean.
- [ ] **(Stretch) Sweep MODE1 filter mode** (Sinc1 / Sinc2 / Sinc4 / FIR) as a follow-up — Sinc3 default is what production firmware uses, but other modes might be useful for specific sensor characteristics.

See [`../TODO.md`](../TODO.md) for cross-cutting items.
