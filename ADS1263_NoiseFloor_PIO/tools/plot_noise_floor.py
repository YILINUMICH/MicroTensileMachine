#!/usr/bin/env python3
"""
plot_noise_floor.py — publication-quality figures from the Phase 1.2 sweep.

Reads the same CSV format as analyze_noise_floor.py and emits four PNG
figures intended for slides / paper figures:

  1. <stem>_rms_vs_sps.png      Log-log lines: input-referred RMS vs SPS,
                                one curve per PGA gain. Datasheet typical
                                overlay (dashed).
  2. <stem>_ratio_vs_sps.png    Measured / datasheet ratio vs SPS. Healthy
                                chips sit in the 1.0–1.5× band.
  3. <stem>_rms_heatmap.png     Heatmap of input-referred RMS over the
                                (SPS, gain) surface. Log color scale.
  4. <stem>_nfb_heatmap.png     Heatmap of noise-free bits. Linear scale.

Usage:
    python3 plot_noise_floor.py path/to/noisefloor_clean.csv
    python3 plot_noise_floor.py path/to/noisefloor_clean.csv --outdir figures/

Dependencies: pandas, numpy, matplotlib.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LogNorm

# Reuse the datasheet typicals so the plots can overlay them without a
# second source of truth.
sys.path.insert(0, str(Path(__file__).parent))
from analyze_noise_floor import DATASHEET_TYPICAL_UV   # noqa: E402


# Tableau-10 palette, picked so adjacent gains are visually distinct.
GAIN_COLORS = {
    1:  "#1f77b4",
    2:  "#ff7f0e",
    4:  "#2ca02c",
    8:  "#d62728",
    16: "#9467bd",
    32: "#8c564b",
}

PLOT_STYLE = {
    "font.family":          "DejaVu Sans",
    "font.size":            11,
    "axes.titlesize":       12,
    "axes.labelsize":       11,
    "legend.fontsize":      9,
    "xtick.labelsize":      10,
    "ytick.labelsize":      10,
    "figure.dpi":           120,
    "savefig.dpi":          200,
    "savefig.bbox":         "tight",
    "axes.grid":            True,
    "grid.alpha":           0.3,
    "grid.linestyle":       "--",
}


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, comment="#")
    needed = {"sps", "gain", "in_rms_uV", "nfb"}
    if not needed.issubset(df.columns):
        sys.exit(f"ERROR: CSV missing required columns. Found: {list(df.columns)}")
    return df


def plot_rms_vs_sps(df: pd.DataFrame, out: Path, title_suffix: str = "") -> None:
    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    sps_levels = sorted(df["sps"].unique())
    gain_levels = sorted(df["gain"].unique())

    for gain in gain_levels:
        sub = df[df["gain"] == gain].sort_values("sps")
        ax.plot(
            sub["sps"], sub["in_rms_uV"],
            marker="o", markersize=6, linewidth=1.6,
            color=GAIN_COLORS.get(gain, "#333"),
            label=f"gain = {gain}",
        )

        # Datasheet overlay (dashed, same colour, fainter)
        ds_sps = [s for s in sps_levels if (s, gain) in DATASHEET_TYPICAL_UV]
        ds_vals = [DATASHEET_TYPICAL_UV[(s, gain)] for s in ds_sps]
        if ds_vals:
            ax.plot(
                ds_sps, ds_vals,
                linestyle="--", linewidth=1.0,
                color=GAIN_COLORS.get(gain, "#333"),
                alpha=0.5,
            )

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Sample rate (SPS)")
    ax.set_ylabel("Input-referred RMS noise (µV)")
    ax.set_title(
        f"ADS1263 noise floor: measured (solid) vs datasheet typical (dashed){title_suffix}"
    )
    ax.legend(loc="upper left", ncol=2, frameon=True, framealpha=0.9)
    ax.set_xticks(sps_levels)
    ax.set_xticklabels([str(s) for s in sps_levels])

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def plot_ratio_vs_sps(df: pd.DataFrame, out: Path, title_suffix: str = "") -> None:
    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    df = df.copy()
    df["typical_uV"] = df.apply(
        lambda r: DATASHEET_TYPICAL_UV.get((int(r["sps"]), int(r["gain"]))),
        axis=1,
    )
    df = df.dropna(subset=["typical_uV"])
    df["ratio"] = df["in_rms_uV"] / df["typical_uV"]

    sps_levels = sorted(df["sps"].unique())
    for gain in sorted(df["gain"].unique()):
        sub = df[df["gain"] == gain].sort_values("sps")
        ax.plot(
            sub["sps"], sub["ratio"],
            marker="o", markersize=6, linewidth=1.6,
            color=GAIN_COLORS.get(gain, "#333"),
            label=f"gain = {gain}",
        )

    # Reference bands: 1× (perfect), 1.5× (anomaly threshold)
    ax.axhline(1.0, color="black", linewidth=1.0, linestyle="-", alpha=0.6,
               label="1× datasheet typical")
    ax.axhline(1.5, color="red", linewidth=1.0, linestyle=":", alpha=0.6,
               label="1.5× anomaly threshold")
    ax.fill_between(sps_levels, 1.0, 1.5, color="green", alpha=0.05)

    ax.set_xscale("log")
    ax.set_xlabel("Sample rate (SPS)")
    ax.set_ylabel("Measured / datasheet typical")
    ax.set_title(
        f"ADS1263 noise: measured / datasheet typical{title_suffix}"
    )
    ax.set_xticks(sps_levels)
    ax.set_xticklabels([str(s) for s in sps_levels])
    ax.legend(loc="best", ncol=2, frameon=True, framealpha=0.9)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def plot_rms_heatmap(df: pd.DataFrame, out: Path, title_suffix: str = "") -> None:
    pv = df.pivot_table(index="sps", columns="gain", values="in_rms_uV")
    pv = pv.reindex(sorted(pv.index), axis=0)
    pv = pv.reindex(sorted(pv.columns), axis=1)

    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    im = ax.imshow(
        pv.values,
        aspect="auto",
        cmap="viridis",
        norm=LogNorm(vmin=pv.values.min(), vmax=pv.values.max()),
        origin="lower",
    )
    ax.set_xticks(range(len(pv.columns)))
    ax.set_xticklabels([str(g) for g in pv.columns])
    ax.set_yticks(range(len(pv.index)))
    ax.set_yticklabels([str(s) for s in pv.index])
    ax.set_xlabel("PGA gain (V/V)")
    ax.set_ylabel("Sample rate (SPS)")
    ax.set_title(f"Input-referred RMS noise (µV) — log color scale{title_suffix}")

    # Annotate every cell with its value
    for i, sps in enumerate(pv.index):
        for j, gain in enumerate(pv.columns):
            v = pv.loc[sps, gain]
            txt = f"{v:.2f}" if v >= 0.1 else f"{v:.3f}"
            # Choose text color for contrast — light on dark, dark on light
            lo, hi = np.log10(pv.values.min()), np.log10(pv.values.max())
            t = (np.log10(v) - lo) / (hi - lo) if hi > lo else 0.5
            color = "white" if t < 0.55 else "black"
            ax.text(j, i, txt, ha="center", va="center",
                    fontsize=9, color=color)

    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("input-referred RMS (µV, log)")

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def plot_nfb_heatmap(df: pd.DataFrame, out: Path, title_suffix: str = "") -> None:
    pv = df.pivot_table(index="sps", columns="gain", values="nfb")
    pv = pv.reindex(sorted(pv.index), axis=0)
    pv = pv.reindex(sorted(pv.columns), axis=1)

    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    im = ax.imshow(
        pv.values,
        aspect="auto",
        cmap="plasma",
        origin="lower",
        vmin=pv.values.min(),
        vmax=pv.values.max(),
    )
    ax.set_xticks(range(len(pv.columns)))
    ax.set_xticklabels([str(g) for g in pv.columns])
    ax.set_yticks(range(len(pv.index)))
    ax.set_yticklabels([str(s) for s in pv.index])
    ax.set_xlabel("PGA gain (V/V)")
    ax.set_ylabel("Sample rate (SPS)")
    ax.set_title(f"Noise-free bits — higher is better{title_suffix}")

    for i, sps in enumerate(pv.index):
        for j, gain in enumerate(pv.columns):
            v = pv.loc[sps, gain]
            lo, hi = pv.values.min(), pv.values.max()
            t = (v - lo) / (hi - lo) if hi > lo else 0.5
            color = "white" if t < 0.55 else "black"
            ax.text(j, i, f"{v:.1f}", ha="center", va="center",
                    fontsize=9, color=color)

    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("noise-free bits")

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path, help="Path to cleaned CSV.")
    parser.add_argument("--outdir", type=Path, default=None,
                        help="Directory for output PNGs (default: alongside CSV).")
    parser.add_argument("--title-suffix", type=str, default="",
                        help="Optional suffix added to each plot title (e.g., a date).")
    args = parser.parse_args()

    if not args.csv.is_file():
        sys.exit(f"ERROR: file not found: {args.csv}")

    plt.rcParams.update(PLOT_STYLE)

    df = load_csv(args.csv)
    outdir = args.outdir if args.outdir is not None else args.csv.parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = args.csv.stem

    suffix = (" — " + args.title_suffix) if args.title_suffix else ""
    print(f"Plotting {len(df)} rows to {outdir}/ ...")
    plot_rms_vs_sps  (df, outdir / f"{stem}_rms_vs_sps.png",   title_suffix=suffix)
    plot_ratio_vs_sps(df, outdir / f"{stem}_ratio_vs_sps.png", title_suffix=suffix)
    plot_rms_heatmap (df, outdir / f"{stem}_rms_heatmap.png",  title_suffix=suffix)
    plot_nfb_heatmap (df, outdir / f"{stem}_nfb_heatmap.png",  title_suffix=suffix)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
