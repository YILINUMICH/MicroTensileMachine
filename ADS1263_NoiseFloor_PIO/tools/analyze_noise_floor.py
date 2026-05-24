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
# Datasheet reference — ADS1263 Table 7.10 typical input-referred RMS noise.
#
# These numbers are APPROXIMATE — please cross-check against the actual
# datasheet PDF (doc/ADS1263_Datasheet.pdf, §7.10) and refine. The script
# flags any measured cell that exceeds `threshold * datasheet_typical_uV`.
#
# Units: µV RMS, input-referred, VREF = 5 V, Sinc3 filter, PGA enabled.
#
# Filled in to match the order of magnitude of the bring-up baseline
# (~1.4 µV RMS at 400 SPS, PGA bypass, internal short). PGA-enabled
# values are roughly the same to slightly higher at gain=1 due to the
# PGA's own input-referred noise floor.
# ---------------------------------------------------------------------------
DATASHEET_TYPICAL_UV: dict[tuple[int, int], float] = {
    # (sps, gain) -> typical RMS µV
    # The numbers below are PLACEHOLDERS — please replace with values
    # read off the actual datasheet table.
    (10,    1): 0.35,  (10,    2): 0.25,  (10,    4): 0.18,
    (10,    8): 0.13,  (10,   16): 0.10,  (10,   32): 0.08,
    (50,    1): 0.70,  (50,    2): 0.50,  (50,    4): 0.35,
    (50,    8): 0.25,  (50,   16): 0.19,  (50,   32): 0.15,
    (100,   1): 1.00,  (100,   2): 0.70,  (100,   4): 0.50,
    (100,   8): 0.35,  (100,  16): 0.27,  (100,  32): 0.21,
    (400,   1): 2.00,  (400,   2): 1.40,  (400,   4): 1.00,
    (400,   8): 0.70,  (400,  16): 0.55,  (400,  32): 0.42,
    (1200,  1): 3.50,  (1200,  2): 2.50,  (1200,  4): 1.80,
    (1200,  8): 1.30,  (1200, 16): 1.00,  (1200, 32): 0.80,
    (2400,  1): 5.00,  (2400,  2): 3.60,  (2400,  4): 2.60,
    (2400,  8): 1.90,  (2400, 16): 1.50,  (2400, 32): 1.20,
    (4800,  1): 7.00,  (4800,  2): 5.10,  (4800,  4): 3.70,
    (4800,  8): 2.70,  (4800, 16): 2.10,  (4800, 32): 1.70,
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
        format_pivot(rms_pv, "{:7.3f}", "lower is better; compare against ADS1263 Tbl 7.10"),
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
    print("Reference: doc/ADS1263_Datasheet.pdf Table 7.10 (typical noise).")
    print("           doc/MEMO_baseline_testing.md Phase 1.2 acceptance criteria.")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
