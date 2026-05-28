# ADS1263_NoiseFloor_PIO — full SPS × PGA noise-floor sweep

> **Status: Diagnostic — bench-verified 2026-05-24.** All 42 (SPS × PGA) cells in-spec; data at [`data/noisefloor_20260524_1845.csv`](data/noisefloor_20260524_1845.csv). See [STATUS.md](STATUS.md). See [../README.md](../README.md) for project overview. See [../doc/MEMO_baseline_testing.md](../doc/MEMO_baseline_testing.md) for the wider testing plan this implements (this module is Phase 1.2).

Walks the ADS1263's noise-vs-mode surface by AINCOM-shorting both ADC differential inputs (`INPMUX = 0xAA`) and stepping through every combination of:

- **SPS** ∈ {10, 50, 100, 400, 1200, 2400, 4800}
- **PGA gain** ∈ {1, 2, 4, 8, 16, 32}

That's 42 cells. For each cell the sketch collects N samples (N scales with SPS — see below), computes mean / RMS / peak-to-peak / noise-free-bits, and prints one CSV row to USB serial.

A Python script in `tools/` reads the CSV and compares each cell against the ADS1263 datasheet's typical RMS noise (Table 7.10). Cells more than 1.5× above the typical value are flagged — these are operating modes that are *off-limits* for downstream firmware until the discrepancy is investigated.

## When to use this

- After `ADS1263_FirstPowerUp_PIO/` cp0–cp6 pass on the new hardware combination.
- After any change to the rig that could plausibly affect noise: cable rework, swap of the EVM, change of external reference, change of PSU rails, etc.
- Before locking in a chosen SPS / PGA operating point for a new sensor channel.

## Sample count rationale

Per cell: `N = clamp(10 × SPS, 200, 2000)`.

- The "10 × SPS" target means each cell takes roughly **10 seconds of wall time** to collect — enough for low-frequency 1/f noise to express, and enough for the RMS estimator's relative uncertainty (≈ 1/√(2N)) to be a few percent.
- The 200 floor catches the low-SPS rates where 10 × SPS would be tiny (10 SPS × 10 = 100, too few).
- The 2000 cap keeps the high-SPS rates from running forever (e.g., 4800 × 10 = 48000 samples would be 10 seconds but uses too much RAM for our M7 buffer).

Total sweep runtime: ≈ 5 minutes.

## Before you flash — prerequisites

1. **`ADS1263_FirstPowerUp_PIO/` cp0–cp6 must pass.** If cp6 (VBIAS mini-sweep) hasn't run yet, run that first — it's the precondition that this sketch assumes. Specifically: cp6 confirms that enabling VBIAS in the POWER register actually biases AINCOM to mid-supply such that PGA-enabled measurements produce sensible numbers at every gain.
2. **The REF7050 external reference** must be applied to the EVM's AIN0 (+REF) / AIN1 (−REF) screw terminals, and `REFMUX = 0x09` is what the sketch will write to the chip. See [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) Cable 2.
3. **Power-cycle the EVM** before running. The DFU reset on the H7 doesn't cleanly re-power the ADC, and stale chip state can confuse the sweep.

## Build + flash + capture

```sh
cd ADS1263_NoiseFloor_PIO/
mkdir -p data
pio run -t upload

# Capture serial to a timestamped CSV file:
pio device monitor 2>&1 | tee data/noisefloor_$(date +%Y%m%d_%H%M).csv
# Ctrl-C once you see "# Sweep complete." (≈ 5 minutes after start).

# Strip the comment / header lines so the file is pure CSV with one header row:
grep -v '^#' data/noisefloor_*.csv | tail -n +1 > data/noisefloor_clean.csv

# Run the analysis:
python3 tools/analyze_noise_floor.py data/noisefloor_clean.csv
```

## Expected output (excerpt)

The serial stream looks like:

```
# ============================================================
#   ADS1263 noise-floor sweep — Phase 1.2
# ============================================================
# Hardware:  Portenta H7 + Mid Carrier + ADS1263 EVM
# Reference: TI REF7050 (5.000 V) on AIN0/AIN1, REFMUX = 0x09
# Input:     AINCOM-shorted via INPMUX = 0xAA
# Bias:      VBIAS on (POWER bit 1) → AINCOM at +2.5 V
# Filter:    Sinc3 (MODE1 default), no chop, no FIR
# SPI:       500 kHz, MODE1
# Sweep:     SPS ∈ {10, 50, 100, 400, 1200, 2400, 4800}
#            × PGA ∈ {1, 2, 4, 8, 16, 32}
# Samples:   N = clamp(10·SPS, 200, 2000)
# ============================================================
# ADS1263 ID register = 0x23
# POWER: 0x11 → 0x13
# INTERFACE = 0x5
#
# CSV columns:
#   ...
sps,sps_code,gain,gain_code,mode2,n_samples,period_us,out_mean_uV,out_rms_uV,out_pkpk_uV,in_mean_uV,in_rms_uV,in_pkpk_uV,nfb,stuck_pct
10,0x02,1,0,0x02,200,100000,...
10,0x02,2,1,0x12,200,100000,...
...
4800,0x0B,32,5,0x5B,2000,208,...
# Sweep complete. 42 points emitted.
```

The LED on the Portenta H7 is solid ON during the sweep and goes back to a slow heartbeat once it's done — useful for a quick visual "still running?" check.

## Failure modes

| Symptom | Most-likely cause | What to check |
|---|---|---|
| `# FAIL: ADS1263 not responding correctly.` | SPI bus, /CS, or chip power problem | Run `ADS1263_FirstPowerUp_PIO/` — its cp0–cp4 will localize precisely where it broke. |
| `# FAIL: VBIAS bit did not stick` | POWER register write didn't land | Run `ADS1263_FirstPowerUp_PIO/` cp6 — same triage applies. Usually /CS timing or SPI mode mismatch. |
| Sweep starts, all rows have `out_rms_uV ≈ 0` and `stuck_pct ≈ 100` | Conversions not actually running — chip is returning the same idle value | Confirm `START1` command isn't blocked, check `MODE2` readback, verify the bring-up cp5 still passes. |
| `stuck_pct` non-zero on high-SPS rows only | SPI throughput limited (500 kHz, RDATA1 = 112 µs) vs chip conversion period | This is normal-ish at 4800 SPS; investigate before trusting those rows. Bumping SPI to 1 MHz fixes it. |
| Some rows have `in_rms_uV` much higher than datasheet typical | Could be EMI pickup, could be a real chip-config issue (wrong filter, wrong PGA bypass state, AINCOM bias problem) | Cross-check `MODE2` and `MODE1` printed by the sketch against expected. Re-run with the rig isolated from nearby switching supplies / motor drivers. |

## Related

- [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md) — full testing plan; this module is **Phase 1.2**.
- [`../doc/MEMO_cable_map.md`](../doc/MEMO_cable_map.md) — wiring source of truth.
- [`../doc/ADS1263_Datasheet.pdf`](../doc/ADS1263_Datasheet.pdf) — Table 7.10 (typical noise vs SPS × gain), §9.6.6 (MODE2 register), §9.6.2 (POWER register / VBIAS bit), §9.3.12 (VBIAS function).
- [`../ADS1263_FirstPowerUp_PIO/`](../ADS1263_FirstPowerUp_PIO/) — prerequisite bring-up sketch (cp0–cp6).
- [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) — historical Test B noise result (5.28 µV RMS at 400 SPS on the legacy HAT with 2.5 V int ref + 4.4× attenuator — not directly comparable to this sweep, but useful as a reasonableness check).
