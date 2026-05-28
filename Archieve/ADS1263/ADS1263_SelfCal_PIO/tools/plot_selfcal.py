#!/usr/bin/env python3
"""
plot_selfcal.py — publication-quality figures from the Phase 2.1
ADS1263_SelfCal_PIO bench output.

Parses the serial log emitted by the sketch and produces two PNG
figures suitable for slides / paper figures:

  1. <stem>_sfocal_offset.png   Bar chart: pre-cal vs post-cal
                                input-referred offset at each PGA gain.
                                Shows the offset cancellation visually.
  2. <stem>_sfocal_ofcal.png    Scatter: OFCAL predicted vs actual,
                                across PGA gains, with the 1:1 line and
                                a ±100 LSB band. Confirms SFOCAL1
                                computes OFCAL the way the datasheet says.

Usage:
    python3 plot_selfcal.py path/to/selfcal_YYYYMMDD_HHMM.log

Dependencies: numpy, matplotlib.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List

import matplotlib.pyplot as plt
import numpy as np


# ----- Plot style ---------------------------------------------------------
PLOT_STYLE = {
    "font.family":          "DejaVu Sans",
    "font.size":            11,
    "axes.titlesize":       12,
    "axes.labelsize":       11,
    "legend.fontsize":      10,
    "xtick.labelsize":      10,
    "ytick.labelsize":      10,
    "figure.dpi":           120,
    "savefig.dpi":          200,
    "savefig.bbox":         "tight",
    "axes.grid":            True,
    "grid.alpha":           0.3,
    "grid.linestyle":       "--",
}


# ----- Parse cp2 table from log ------------------------------------------
# Example line we're parsing (one PGA gain row):
#   [cp 2] info       1 |  0x08 |     +739.244  | 0x0004D8 (    +1240) |
#       0x0004D8 (    +1240)| 0x05 OK    |        +0.091  |  100.0%  | pass
#
# We extract: gain, pre_mean_uV, ofcal_predicted, ofcal_actual, post_mean_uV
CP2_ROW_RE = re.compile(
    r"\[cp 2\]\s+info\s+"
    r"(?P<gain>\d+)\s*\|\s*0x\w+\s*\|\s*"
    r"(?P<pre>[+\-][0-9.]+)\s*\|\s*"
    r"0x\w+\s*\(\s*(?P<ofcal_pred>[+\-]?\d+)\)\s*\|\s*"
    r"0x\w+\s*\(\s*(?P<ofcal_act>[+\-]?\d+)\)\s*\|\s*"
    r"0x\w+\s+(?P<intf>OK|BAD)\s*\|\s*"
    r"(?P<post>[+\-][0-9.]+)\s*\|"
)


def parse_cp2_rows(log_text: str) -> List[dict]:
    rows = []
    for m in CP2_ROW_RE.finditer(log_text):
        rows.append({
            "gain":         int(m.group("gain")),
            "pre_mean_uV":  float(m.group("pre")),
            "ofcal_pred":   int(m.group("ofcal_pred")),
            "ofcal_act":    int(m.group("ofcal_act")),
            "post_mean_uV": float(m.group("post")),
            "intf_ok":      m.group("intf") == "OK",
        })
    return rows


def plot_offset_bars(rows: List[dict], out: Path, title_suffix: str = "") -> None:
    """Two-bar chart per gain: pre-cal vs post-cal input-referred mean."""
    fig, ax = plt.subplots(figsize=(8.0, 4.8))
    gains = [r["gain"] for r in rows]
    pre   = [r["pre_mean_uV"]  for r in rows]
    post  = [r["post_mean_uV"] for r in rows]

    x = np.arange(len(gains))
    width = 0.36

    bars_pre  = ax.bar(x - width/2, pre,  width, label="pre-cal",
                       color="#d62728", alpha=0.85, edgecolor="black", linewidth=0.5)
    bars_post = ax.bar(x + width/2, post, width, label="post-cal (SFOCAL1)",
                       color="#2ca02c", alpha=0.85, edgecolor="black", linewidth=0.5)

    # Value annotations above each bar
    for b, v in zip(bars_pre, pre):
        ax.annotate(f"{v:+.1f}", xy=(b.get_x() + b.get_width()/2, v),
                    ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=8, xytext=(0, 2 if v >= 0 else -2),
                    textcoords="offset points")
    for b, v in zip(bars_post, post):
        ax.annotate(f"{v:+.2f}", xy=(b.get_x() + b.get_width()/2, v),
                    ha="center", va="bottom" if v >= 0 else "top",
                    fontsize=8, xytext=(0, 2 if v >= 0 else -2),
                    textcoords="offset points")

    ax.axhline(0, color="black", linewidth=0.7, alpha=0.6)
    ax.set_xticks(x)
    ax.set_xticklabels([f"gain {g}" for g in gains])
    ax.set_ylabel("Input-referred mean offset (µV)")
    ax.set_title(f"ADS1263 SFOCAL1: input-referred offset before vs after self-cal{title_suffix}")
    ax.legend(loc="upper right", frameon=True, framealpha=0.9)

    # Symlog scale so the small post-cal bars stay visible next to the
    # large pre-cal ones at low gain (~740 µV vs ~0 µV).
    ax.set_yscale("symlog", linthresh=10, linscale=0.5)
    ax.set_ylim(-50, 1500)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def plot_ofcal_scatter(rows: List[dict], out: Path, title_suffix: str = "") -> None:
    """Scatter: OFCAL predicted (from raw mean) vs OFCAL actual (from chip).
       Includes 1:1 line and ±100 LSB band."""
    fig, ax = plt.subplots(figsize=(7.0, 5.0))

    pred = np.array([r["ofcal_pred"] for r in rows], dtype=float)
    act  = np.array([r["ofcal_act"]  for r in rows], dtype=float)
    gains = [r["gain"] for r in rows]

    # 1:1 reference and ±100 LSB band
    lo = min(pred.min(), act.min()) - 50
    hi = max(pred.max(), act.max()) + 50
    ax.plot([lo, hi], [lo, hi], color="black", linewidth=1.0, linestyle="-",
            alpha=0.5, label="1:1 (perfect agreement)")
    ax.fill_between([lo, hi], [lo - 100, hi - 100], [lo + 100, hi + 100],
                    color="green", alpha=0.08, label="±100 LSB band")

    # Color points by gain
    palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    for i, (p, a, g) in enumerate(zip(pred, act, gains)):
        ax.scatter(p, a, s=80, color=palette[i % len(palette)],
                   edgecolor="black", linewidth=0.7, zorder=5,
                   label=f"gain {g}")
        ax.annotate(f"+{int(a - p):d} LSB",
                    xy=(p, a), xytext=(8, 4), textcoords="offset points",
                    fontsize=8, alpha=0.85)

    ax.set_xlabel("OFCAL predicted from pre-cal mean (LSB)")
    ax.set_ylabel("OFCAL actual after SFOCAL1 (LSB)")
    ax.set_title(f"ADS1263 SFOCAL1: predicted vs actual OFCAL register{title_suffix}")
    ax.legend(loc="lower right", frameon=True, framealpha=0.9, ncol=2)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_aspect("equal")

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  wrote {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path,
                        help="Path to SelfCal bench log (selfcal_*.log).")
    parser.add_argument("--outdir", type=Path, default=None,
                        help="Directory for output PNGs (default: alongside log).")
    parser.add_argument("--title-suffix", type=str, default="",
                        help="Optional suffix added to each plot title (e.g., a date).")
    args = parser.parse_args()

    if not args.log.is_file():
        sys.exit(f"ERROR: file not found: {args.log}")

    plt.rcParams.update(PLOT_STYLE)

    log_text = args.log.read_text(encoding="utf-8", errors="replace")
    rows = parse_cp2_rows(log_text)
    if not rows:
        sys.exit("ERROR: no cp2 SFOCAL1 rows parsed from log. "
                 "Check that the log contains the gain table.")

    outdir = args.outdir if args.outdir is not None else args.log.parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = args.log.stem

    suffix = (" — " + args.title_suffix) if args.title_suffix else ""
    print(f"Parsed {len(rows)} cp2 rows from {args.log.name}; writing to {outdir}/ ...")
    plot_offset_bars  (rows, outdir / f"{stem}_sfocal_offset.png", title_suffix=suffix)
    plot_ofcal_scatter(rows, outdir / f"{stem}_sfocal_ofcal.png",   title_suffix=suffix)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
