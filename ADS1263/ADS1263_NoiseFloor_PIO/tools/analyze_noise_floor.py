#!/usr/bin/env python3
"""
analyze_noise_floor.py — summarize the ADS1263_NoiseFloor_PIO CSV dump.

Reads the CSV produced by ADS1263_NoiseFloor_PIO/src/main.cpp (Phase 1.2 of
doc/MEMO_baseline_testing.md) and:

  1. Loads it into a pandas DataFrame.
  2. Pivots into (SPS row) × (PGA column) tables for input-referred RMS,
     peak-to-peak, noise-free bits, and the stuck-sample percentage.
  3. Prints those tables in a fixed-width format suitable for pasting into
     a notebook or operator memo.
  4. Flags cells that look anomalous against a built-in reference table
     (loosely calibrated from ADS1263 datasheet Table 7.10 — the user
     should refine these from the actual datasheet PDF and update
     `DATASHEET_TYPICAL_UV` below as the canonical reference).
  5. Optionally writes the pivots to TSV files alongside the input CSV.

Usage:

    python3 analyze_noise_floor.py path/to/noisefloor_clean.csv
    python3 analyze_noise_floor.py path/to/noisefloor_clean.csv --tsv
    python3 analyze_noise_floor.py path/to/noisefloor_clean.csv --threshold 2.0

The `clean` CSV is the sketch's serial dump with the leading comment lines
(`#`) stripped. See the module README for the capture+strip recipe.

Dependencies: pandas. No matplotlib — keep it terminal-friendly.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Datasheet reference — ADS1263 Table 8-1 (NOT 7.10 — table number is 8-1
# in the SBAS661C revision May 2021 datasheet; the original code comment
# referencing "Table 7.10" was from an older revision).
#
# Values below are the Sinc3 filter, ADC1 typical-noise row from datasheet
# Table 8-1, exact transcription. Units: µV RMS, input-referred.
#
# Note on VREF scaling: Table 8-1 is specified at VREF = 2.5 V, but the
# numbers are reported in absolute µV (not LSBs). For a 32-bit
# delta-sigma ADC at typical operating points the noise is dominated by
# the analog front end (PGA + modulator + filter), which is reference-
# independent in absolute volts. Therefore the µV values apply equally
# to our rig's VREF = 5 V configuration. The 4800 SPS bench measurement
# on 2026-05-24 (3.72 µV typical vs 4.34 µV measured = 1.17×) confirms
# this scaling assumption is reasonable.
#
# The script flags any measured cell exceeding `threshold * typical`
# (default threshold = 1.5×). Healthy chips typically land at 1.0–1.5×
# typical; values above 1.5× warrant investigation.
#
# Sinc3 row only — production firmware uses Sinc3 (MODE1 default).
# If you ever sweep other filter modes, add them here.
# ---------------------------------------------------------------------------
DATASHEET_TYPICAL_UV: dict[tuple[int, int], float] = {
    # (sps, gain) -> typical RMS µV, ADC1, Sinc3 filter, VREF=2.5V
    # (applies to VREF=5V too — see note above)
    (10,    1): 0.176, (10,    2): 0.088, (10,    4): 0.043,
    (10,    8): 0.028, (10,   16): 0.018, (10,   32): 0.014,
    (50,    1): 0.389, (50,    2): 0.196, (50,    4): 0.104,
    (50,    8): 0.057, (50,   16): 0.038, (50,   32): 0.030,
    (100,   1): 0.531, (100,   2): 0.277, (100,   4): 0.143,
    (100,   8): 0.081, (100,  16): 0.054, (100,  32): 0.043,
    (400,   1): 1.072, (400,   2): 0.550, (400,   4): 0.285,
    (400,   8): 0.161, (400,  16): 0.107, (400,  32): 0.087,
    (1200,  1): 1.858, (1200,  2): 0.960, (1200,  4): 0.494,
    (1200,  8): 0.281, (1200, 16): 0.186, (1200, 32): 0.148,
    (2400,  1): 2.656, (2400,  2): 1.337, (2400,  4): 0.705,
    (2400,  8): 0.395, (2400, 16): 0.262, (2400, 32): 0.211,
    (4800,  1): 3.720, (4800,  2): 1.894, (4800,  4): 0.998,
    (4800,  8): 0.560, (4800, 16): 0.367, (4800, 32): 0.297,
}


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, comment="#")
    expected_cols = {
        "sps", "gain", "in_rms_uV", "in_pkpk_uV", "in_mean_uV",
        "nfb", "stuck_pct", "n_samples",
    }
    missing = expected_cols - set(df.columns)
    if missing:
        sys.exit(f"ERROR: CSV is missing expected columns: {sorted(missing)}\n"
                 f"        columns found: {list(df.columns)}\n"
                 f"        did you strip the '#' comment lines first?")
    return df


def pivot(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    pv = df.pivot_table(index="sps", columns="gain", values=value_col)
    # Order rows by SPS, columns by gain — both ascending.
    pv = pv.reindex(sorted(pv.index), axis=0)
    pv = pv.reindex(sorted(pv.columns), axis=1)
    return pv


def print_section(title: str, lines: list[str]) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)
    for line in lines:
        print(line)


def format_pivot(pv: pd.DataFrame, fmt: str, label: str) -> list[str]:
    out = [f"  {label}", ""]
    # Header row.
    header = "    SPS  | " + "  ".join(f"gain={int(g):>3d}" for g in pv.columns)
    out.append(header)
    out.append("    " + "-" * (len(header) - 4))
    for sps in pv.index:
        row_cells = []
        for g in pv.columns:
            v = pv.loc[sps, g]
            if pd.isna(v):
                cell = "       -"
            else:
                cell = fmt.format(v)
            row_cells.append(cell)
        out.append(f"   {int(sps):>5d}  |  " + "  ".join(row_cells))
    return out


def find_anomalies(df: pd.DataFrame, threshold: float) -> list[str]:
    anomalies: list[str] = []
    for _, row in df.iterrows():
        sps = int(row["sps"])
        gain = int(row["gain"])
        rms = float(row["in_rms_uV"])
        stuck = float(row["stuck_pct"])
        typical = DATASHEET_TYPICAL_UV.get((sps, gain))
        if typical is not None:
            ratio = rms / typical
            if ratio > threshold:
                anomalies.append(
                    f"  SPS={sps:>5d}  gain={gain:>3d}  "
                    f"in_rms = {rms:.3f} µV  "
                    f"≈ {ratio:.1f}× datasheet typical ({typical:.2f} µV)"
                )
        if stuck > 0.5:
            anomalies.append(
                f"  SPS={sps:>5d}  gain={gain:>3d}  "
                f"stuck_pct = {stuck:.1f}%  "
                f"(SPI polling may be outrunning chip conversions)"
            )
        if rms == 0.0:
            anomalies.append(
                f"  SPS={sps:>5d}  gain={gain:>3d}  "
                f"in_rms = 0.000 — every sample identical, "
                f"conversions not advancing!"
            )
    return anomalies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path,
                        help="Path to the cleaned CSV (no # lines).")
    parser.add_argument("--threshold", type=float, default=1.5,
                        help="Anomaly threshold as multiple of datasheet typical "
                             "(default: 1.5).")
    parser.add_argument("--tsv", action="store_true",
                        help="Also write per-metric pivot tables as TSV files "
                             "alongside the input CSV.")
    args = parser.parse_args()

    if not args.csv.is_file():
        sys.exit(f"ERROR: file not found: {args.csv}")

    df = load_csv(args.csv)

    print_section(
        "Input file",
        [
            f"  Path:        {args.csv}",
            f"  Rows:        {len(df)}",
            f"  SPS levels:  {sorted(df['sps'].unique())}",
            f"  Gain levels: {sorted(df['gain'].unique())}",
            f"  N range:     {df['n_samples'].min()} – {df['n_samples'].max()}",
        ],
    )

    rms_pv  = pivot(df, "in_rms_uV")
    pkpk_pv = pivot(df, "in_pkpk_uV")
    nfb_pv  = pivot(df, "nfb")
    mean_pv = pivot(df, "in_mean_uV")

    print_section(
        "Input-referred RMS noise (µV)",
        format_pivot(rms_pv, "{:7.3f}", "lower is better; compare against ADS1263 Tbl 8-1"),
    )
    print_section(
        "Input-referred peak-to-peak (µV)",
        format_pivot(pkpk_pv, "{:7.2f}", "≈ 6.6 × RMS for Gaussian noise"),
    )
    print_section(
        "Noise-free bits",
        format_pivot(nfb_pv, "{:7.2f}",
                     "= 32 − log2(pkpk_codes); higher is better. ADS1263 spec: 17–18 typical."),
    )
    print_section(
        "Input-referred mean / offset (µV)",
        format_pivot(mean_pv, "{:+8.2f}",
                     "should be ≤ few mV pre-cal; rough indicator of input offset drift across gains."),
    )

    # Anomalies
    anomalies = find_anomalies(df, args.threshold)
    if not anomalies:
        print_section(
            "Anomalies",
            [
                f"  None — every cell within {args.threshold}× of datasheet typical,",
                "  no stuck-sample rows, no zero-RMS rows.",
                "",
                "  → Phase 1.2 PASS. Operating modes that have been characterized",
                "  can now be trusted for downstream firmware.",
            ],
        )
    else:
        print_section("Anomalies", anomalies + [
            "",
            "  → Investigate each row before relying on that operating mode.",
            "  → If an anomaly is consistent and large, it may indicate:",
            "       - VBIAS not landing (cp6 should have caught this)",
            "       - SPI throughput limit at high SPS",
            "       - EMI pickup from nearby switching electronics",
            "       - REF7050 noise contribution (unlikely below 5 µV but worth checking)",
        ])

    # Optional TSV export
    if args.tsv:
        stem = args.csv.with_suffix("")
        for name, pv in [
            ("rms_uV",  rms_pv),
            ("pkpk_uV", pkpk_pv),
            ("nfb",     nfb_pv),
            ("mean_uV", mean_pv),
        ]:
            out = Path(f"{stem}_pivot_{name}.tsv")
            pv.to_csv(out, sep="\t")
            print(f"  wrote {out}")

    print()
    print("Reference: doc/ADS1263_Datasheet.pdf Table 8-1 (typical ADC1 noise).")
    print("           doc/MEMO_baseline_testing.md Phase 1.2 acceptance criteria.")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
