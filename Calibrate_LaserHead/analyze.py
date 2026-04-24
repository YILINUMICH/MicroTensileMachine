#!/usr/bin/env python3
"""
analyze.py - offline fit + plot for a calibration run.

Reads one points.csv produced by run_calibration.py, fits a line
V(x) = k*x + V0 in mV vs um, reports the sanity checks from
Calibrate_LaserHead_Plan.md section 7, and writes <prefix>_fit.png next
to the input.

Developed against already-saved data so it can be iterated without the
hardware present (plan section 9.5).

Usage:

    python analyze.py data/2026-04-23_run01_points.csv

Author: Yilin Ma - HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import numpy as np

# Matplotlib only at plot time; allow --no-plot runs on machines without a
# display backend installed.
_HAS_MPL = True
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    _HAS_MPL = False


# ---------------------------------------------------------------------------
# IL-030 reference numbers from Calibrate_LaserHead_Plan.md section 1
# ---------------------------------------------------------------------------
IL030_NOMINAL_K_MV_PER_UM = 0.5        # 0-5 V mode
IL030_FS_MV = 5000.0                   # 10 mm range * 0.5 mV/um = 5000 mV
IL030_LINEARITY_SPEC_PCT_FS = 0.1


# ---------------------------------------------------------------------------
# Load points.csv
# ---------------------------------------------------------------------------
@dataclass
class Point:
    target_mm: float
    stage_actual_mm: float
    mean_V: float
    std_V: float
    n_samples: int
    direction: str


def load_points(path: Path) -> Tuple[List[Point], List[Point]]:
    """Return (sweep_points, baseline_points)."""
    sweep: List[Point] = []
    baseline: List[Point] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            p = Point(
                target_mm=float(row["target_mm"]),
                stage_actual_mm=float(row["stage_actual_mm"]),
                mean_V=float(row["mean_V"]),
                std_V=float(row["std_V"]),
                n_samples=int(row["n_samples"]),
                direction=row["direction"],
            )
            if p.direction.startswith("baseline"):
                baseline.append(p)
            else:
                sweep.append(p)
    return sweep, baseline


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------
@dataclass
class FitResult:
    k_mV_per_um: float     # slope, i.e. sensitivity
    v0_mV: float           # intercept at x=0
    r_squared: float
    residuals_mV: np.ndarray
    x_um: np.ndarray
    v_mV: np.ndarray
    max_abs_residual_mV: float
    linearity_pct_fs: float


def linear_fit(points: List[Point], use_stage_actual: bool = True) -> FitResult:
    x_mm = np.array([p.stage_actual_mm if use_stage_actual else p.target_mm
                     for p in points], dtype=float)
    v_v = np.array([p.mean_V for p in points], dtype=float)

    x_um = x_mm * 1000.0
    v_mv = v_v * 1000.0

    slope, intercept = np.polyfit(x_um, v_mv, deg=1)
    predicted = slope * x_um + intercept
    residuals = v_mv - predicted

    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((v_mv - v_mv.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    max_abs = float(np.max(np.abs(residuals))) if residuals.size else 0.0
    linearity_pct = 100.0 * max_abs / IL030_FS_MV

    return FitResult(
        k_mV_per_um=float(slope),
        v0_mV=float(intercept),
        r_squared=r2,
        residuals_mV=residuals,
        x_um=x_um,
        v_mV=v_mv,
        max_abs_residual_mV=max_abs,
        linearity_pct_fs=linearity_pct,
    )


# ---------------------------------------------------------------------------
# Sanity checks (plan section 7)
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def sanity_checks(fit: FitResult, baseline: List[Point],
                  points: List[Point]) -> List[Check]:
    checks: List[Check] = []

    # (a) sensitivity within ~5% of 0.5 mV/um
    pct_dev = 100.0 * abs(fit.k_mV_per_um - IL030_NOMINAL_K_MV_PER_UM) \
        / IL030_NOMINAL_K_MV_PER_UM
    checks.append(Check(
        "sensitivity within 5% of 0.5 mV/um",
        pct_dev <= 5.0,
        f"k = {fit.k_mV_per_um:.4f} mV/um  ({pct_dev:+.2f}% vs nominal)"))

    # (b) R^2 > 0.9999
    checks.append(Check(
        "R^2 > 0.9999",
        fit.r_squared > 0.9999,
        f"R^2 = {fit.r_squared:.6f}"))

    # (c) residuals not obviously S-shaped - approximate via sign changes:
    # a random pattern should have many sign changes; a systematic S-curve
    # has very few.
    signs = np.sign(fit.residuals_mV)
    sign_changes = int(np.sum(signs[1:] != signs[:-1]))
    expected_min_changes = max(2, len(fit.residuals_mV) // 4)
    checks.append(Check(
        "residuals not S-shaped",
        sign_changes >= expected_min_changes,
        f"{sign_changes} sign changes across {len(fit.residuals_mV)} "
        f"points (expect >= {expected_min_changes})"))

    # (d) per-point sigma roughly constant - CV of std across sweep < 50%
    stds = np.array([p.std_V for p in points], dtype=float)
    if len(stds) > 1 and stds.mean() > 0:
        cv = stds.std(ddof=0) / stds.mean()
        checks.append(Check(
            "sigma roughly constant across sweep",
            cv < 0.5,
            f"sigma_mean = {stds.mean():.2e} V  CV = {cv*100:.1f}%"))
    else:
        checks.append(Check(
            "sigma roughly constant across sweep", False,
            "not enough points to assess"))

    # (e) baselines match within noise
    pre = next((p for p in baseline if p.direction == "baseline_pre"), None)
    post = next((p for p in baseline if p.direction == "baseline_post"), None)
    if pre and post:
        drift = post.mean_V - pre.mean_V
        ref_sigma = max(pre.std_V, 1e-9)
        checks.append(Check(
            "baseline drift within noise",
            abs(drift) <= 3.0 * ref_sigma,
            f"drift = {drift*1000:+.3f} mV  "
            f"(|drift|/sigma_pre = {abs(drift)/ref_sigma:.2f})"))
    else:
        checks.append(Check(
            "baseline drift within noise", False,
            "pre/post baselines not found"))

    return checks


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------
def plot_fit(fit: FitResult, out_path: Path, title: str) -> None:
    if not _HAS_MPL:
        logging.warning("matplotlib not available - skipping plot")
        return

    fig, (ax_main, ax_res) = plt.subplots(
        2, 1, figsize=(8, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05})

    ax_main.plot(fit.x_um, fit.v_mV, "o", markersize=4, label="measured")
    x_line = np.array([fit.x_um.min(), fit.x_um.max()])
    sign = "+" if fit.v0_mV >= 0 else "-"
    ax_main.plot(x_line, fit.k_mV_per_um * x_line + fit.v0_mV,
                 "-", linewidth=1.5,
                 label=f"fit: {fit.k_mV_per_um:.4f} mV/um * x "
                       f"{sign} {abs(fit.v0_mV):.2f} mV")
    ax_main.set_ylabel("voltage (mV)")
    ax_main.set_title(title)
    ax_main.legend(loc="best")
    ax_main.grid(True, alpha=0.3)

    ax_res.axhline(0, color="black", linewidth=0.6)
    ax_res.plot(fit.x_um, fit.residuals_mV, "o-", markersize=3, linewidth=0.8)
    ax_res.set_xlabel("stage position (um)")
    ax_res.set_ylabel("residual (mV)")
    ax_res.grid(True, alpha=0.3)

    note = (f"R^2 = {fit.r_squared:.6f}\n"
            f"max |residual| = {fit.max_abs_residual_mV:.3f} mV "
            f"({fit.linearity_pct_fs:.3f}% FS)")
    ax_main.text(0.02, 0.98, note, transform=ax_main.transAxes,
                 va="top", fontsize=9,
                 bbox=dict(facecolor="white", alpha=0.8, edgecolor="gray"))

    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    p = argparse.ArgumentParser(description="analyze a calibration run")
    p.add_argument("points_csv", help="path to <prefix>_points.csv")
    p.add_argument("--use-target", action="store_true",
                   help="fit using commanded target_mm instead of "
                        "stage_actual_mm (default: stage_actual_mm)")
    p.add_argument("--no-plot", action="store_true",
                   help="skip the PNG output")
    p.add_argument("--json-out", action="store_true",
                   help="also emit <prefix>_fit.json with numeric results")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("analyze")

    points_path = Path(args.points_csv)
    if not points_path.exists():
        log.error("File not found: %s", points_path)
        sys.exit(1)

    sweep, baseline = load_points(points_path)
    if len(sweep) < 3:
        log.error("Need at least 3 sweep points to fit a line (got %d).",
                  len(sweep))
        sys.exit(2)

    fit = linear_fit(sweep, use_stage_actual=not args.use_target)

    print()
    print("=" * 64)
    print(f"  Calibration fit - {points_path.name}")
    print("=" * 64)
    print(f"  sensitivity k       : {fit.k_mV_per_um:.4f} mV/um")
    print(f"                      : {fit.k_mV_per_um:.4f} V/mm")
    print(f"  offset V0           : {fit.v0_mV:.3f} mV")
    print(f"  R^2                 : {fit.r_squared:.6f}")
    print(f"  max |residual|      : {fit.max_abs_residual_mV:.3f} mV  "
          f"({fit.linearity_pct_fs:.3f}% of 5000 mV FS)")
    print(f"  points used         : {len(sweep)}")
    print()
    print("  Sanity checks (plan section 7):")
    for c in sanity_checks(fit, baseline, sweep):
        mark = "PASS" if c.passed else "FAIL"
        print(f"    [{mark}]  {c.name}")
        print(f"            {c.detail}")
    print()

    if not args.no_plot:
        png_path = points_path.with_name(
            points_path.name.replace("_points.csv", "_fit.png"))
        plot_fit(fit, png_path, title=points_path.stem)
        log.info("Wrote %s", png_path)

    if args.json_out:
        json_path = points_path.with_name(
            points_path.name.replace("_points.csv", "_fit.json"))
        with open(json_path, "w") as f:
            json.dump({
                "k_mV_per_um": fit.k_mV_per_um,
                "v0_mV": fit.v0_mV,
                "r_squared": fit.r_squared,
                "max_abs_residual_mV": fit.max_abs_residual_mV,
                "linearity_pct_fs": fit.linearity_pct_fs,
                "n_points": len(sweep),
                "source": points_path.name,
            }, f, indent=2)
        log.info("Wrote %s", json_path)


if __name__ == "__main__":
    _main()
