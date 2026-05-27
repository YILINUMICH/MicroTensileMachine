#!/usr/bin/env python3
"""
analyze.py — Offline fit + SVG plot for a load cell calibration run.

Reads one points.csv produced by run_calibration.py, fits a line
V(F) = sensitivity * F + V0  (mV vs mN), auto-detects the linear region
(excluding the soft-zone at low force where the spring isn't fully engaged),
and writes <prefix>_fit.svg next to the input.

After every run, writes (or overwrites) calibration.json so downstream
modules can import the calibrated sensitivity.

Usage:

    python analyze.py                                      # auto-detect latest
    python analyze.py data/2026-05-28_run01_points.csv     # explicit path

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# Matplotlib — SVG backend only.
_HAS_MPL = True
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    _HAS_MPL = False


_THIS_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = _THIS_DIR / "data"
_CALIBRATION_JSON = _THIS_DIR / "calibration.json"


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------
def find_latest_points_csv(data_dir: Path = _DEFAULT_DATA_DIR) -> Optional[Path]:
    candidates = sorted(data_dir.glob("*_points.csv"))
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Calibration persistence
# ---------------------------------------------------------------------------
def save_calibration(fit: "FitResult", checks_passed: bool,
                     source_csv: Path, trimmed_count: int,
                     spring_k_mN_per_mm: float,
                     out_path: Path = _CALIBRATION_JSON) -> Path:
    """
    Write calibration.json. This is the canonical file that downstream
    modules (SMA_CharacterizationV2, SensorHub_PIO host scripts) read
    to convert load cell voltages to force.
    """
    payload = {
        "sensitivity_mV_per_mN": fit.sensitivity_mV_per_mN,
        "sensitivity_V_per_N": fit.sensitivity_mV_per_mN,  # numerically same
        "V0_mV": fit.v0_mV,
        "r_squared": fit.r_squared,
        "max_abs_residual_mV": fit.max_abs_residual_mV,
        "n_points_used": fit.n_points,
        "n_points_trimmed": trimmed_count,
        "spring_k_mN_per_mm": spring_k_mN_per_mm,
        "all_checks_passed": checks_passed,
        "source": source_csv.name,
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "conversion": "force_mN = (V_mV - V0_mV) / sensitivity_mV_per_mN",
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    return out_path


def load_calibration(cal_path: Path = _CALIBRATION_JSON) -> dict:
    """
    Load the canonical calibration.json.

    Importable by other modules::

        from Calibrate_LoadCell.analyze import load_calibration
        cal = load_calibration()
        sensitivity = cal["sensitivity_mV_per_mN"]
        v0 = cal["V0_mV"]
    """
    with open(cal_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Load points.csv
# ---------------------------------------------------------------------------
@dataclass
class Point:
    displacement_mm: float
    stage_actual_mm: float
    expected_force_mN: float
    mean_V: float                        # PRIMARY (ADC1)
    std_V: float
    n_samples: int
    direction: str
    pass_index: int = 0
    # Cross-compare secondary (ADC2)
    mean_V_adc2: Optional[float] = None
    std_V_adc2: Optional[float] = None
    n_samples_adc2: Optional[int] = None


def _try_float(s) -> Optional[float]:
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _try_int(s) -> Optional[int]:
    if s in (None, ""):
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def load_points(path: Path) -> Tuple[List[Point], List[Point]]:
    """Return (sweep_points, baseline_points)."""
    sweep: List[Point] = []
    baseline: List[Point] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_pi = row.get("pass_index", "0")
            try:
                pi = int(raw_pi) if raw_pi not in (None, "") else 0
            except ValueError:
                pi = 0
            p = Point(
                displacement_mm=float(row["displacement_mm"]),
                stage_actual_mm=float(row["stage_actual_mm"]),
                expected_force_mN=float(row["expected_force_mN"]),
                mean_V=float(row["mean_V"]),
                std_V=float(row["std_V"]),
                n_samples=int(row["n_samples"]),
                direction=row["direction"],
                pass_index=pi,
                mean_V_adc2=_try_float(row.get("mean_V_adc2")),
                std_V_adc2=_try_float(row.get("std_V_adc2")),
                n_samples_adc2=_try_int(row.get("n_samples_adc2")),
            )
            if p.direction.startswith("baseline"):
                baseline.append(p)
            else:
                sweep.append(p)
    return sweep, baseline


def has_xcompare(sweep: List[Point]) -> bool:
    return any(p.mean_V_adc2 is not None for p in sweep)


def adc2_points(sweep: List[Point]) -> List[Point]:
    """Build a Point list where mean_V comes from ADC2 (cross-compare)."""
    out: List[Point] = []
    for p in sweep:
        if p.mean_V_adc2 is None:
            continue
        out.append(Point(
            displacement_mm=p.displacement_mm,
            stage_actual_mm=p.stage_actual_mm,
            expected_force_mN=p.expected_force_mN,
            mean_V=p.mean_V_adc2,
            std_V=p.std_V_adc2 if p.std_V_adc2 is not None else 0.0,
            n_samples=p.n_samples_adc2 if p.n_samples_adc2 is not None
                else p.n_samples,
            direction=p.direction,
            pass_index=p.pass_index,
        ))
    return out


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------
@dataclass
class FitResult:
    sensitivity_mV_per_mN: float       # slope
    v0_mV: float                        # intercept
    r_squared: float
    residuals_mV: np.ndarray
    force_mN: np.ndarray
    v_mV: np.ndarray
    max_abs_residual_mV: float
    n_points: int


def linear_fit(points: List[Point]) -> FitResult:
    force = np.array([p.expected_force_mN for p in points], dtype=float)
    v_v = np.array([p.mean_V for p in points], dtype=float)
    v_mV = v_v * 1000.0

    slope, intercept = np.polyfit(force, v_mV, deg=1)
    predicted = slope * force + intercept
    residuals = v_mV - predicted

    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((v_mV - v_mV.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    max_abs = float(np.max(np.abs(residuals))) if residuals.size else 0.0

    return FitResult(
        sensitivity_mV_per_mN=float(slope),
        v0_mV=float(intercept),
        r_squared=r2,
        residuals_mV=residuals,
        force_mN=force,
        v_mV=v_mV,
        max_abs_residual_mV=max_abs,
        n_points=len(points),
    )


# ---------------------------------------------------------------------------
# Linear region auto-detection (soft-zone trimming)
# ---------------------------------------------------------------------------
def find_linear_region(sweep: List[Point],
                       r2_target: float = 0.9999,
                       max_trim_frac: float = 0.30
                       ) -> Tuple[List[Point], List[Point]]:
    """
    Iteratively trim the lowest-force points until R² exceeds r2_target
    or we've removed more than max_trim_frac of the data.

    Returns (linear_points, trimmed_points).

    Strategy: sort by force, try fitting all points. If R² is already
    high enough, done. Otherwise remove the lowest-force point and refit.
    Repeat until the threshold is met or we've trimmed too many.
    """
    sorted_pts = sorted(sweep, key=lambda p: p.expected_force_mN)
    max_trim = max(1, int(len(sorted_pts) * max_trim_frac))

    # Try with all points first
    fit = linear_fit(sorted_pts)
    if fit.r_squared >= r2_target:
        return sorted_pts, []

    # Iteratively trim from the low-force end
    for trim_count in range(1, max_trim + 1):
        candidate = sorted_pts[trim_count:]
        if len(candidate) < 3:
            break
        fit = linear_fit(candidate)
        if fit.r_squared >= r2_target:
            return candidate, sorted_pts[:trim_count]

    # Couldn't reach the target — return the best we got
    # (still trimmed to the point of best R²)
    best_r2 = -1.0
    best_trim = 0
    for t in range(len(sorted_pts) - 3):
        if t > max_trim:
            break
        f = linear_fit(sorted_pts[t:])
        if f.r_squared > best_r2:
            best_r2 = f.r_squared
            best_trim = t
    return sorted_pts[best_trim:], sorted_pts[:best_trim]


# ---------------------------------------------------------------------------
# Multi-pass / multi-direction decompositions
# ---------------------------------------------------------------------------
def per_pass_fits(sweep: List[Point]) -> Dict[int, FitResult]:
    pass_ids = sorted({p.pass_index for p in sweep})
    out: Dict[int, FitResult] = {}
    for pid in pass_ids:
        subset = [p for p in sweep if p.pass_index == pid]
        if len(subset) >= 3:
            out[pid] = linear_fit(subset)
    return out


def per_direction_fits(sweep: List[Point]) -> Dict[str, FitResult]:
    tags = sorted({p.direction for p in sweep})
    out: Dict[str, FitResult] = {}
    for tag in tags:
        subset = [p for p in sweep if p.direction == tag]
        if len(subset) >= 3:
            out[tag] = linear_fit(subset)
    return out


def pass_to_pass_spread(fits: Dict[int, FitResult]) -> Tuple[float, float]:
    if len(fits) < 2:
        return (0.0, 0.0)
    ks = [f.sensitivity_mV_per_mN for f in fits.values()]
    spread = max(ks) - min(ks)
    mean_k = sum(ks) / len(ks)
    pct = 100.0 * spread / abs(mean_k) if mean_k != 0 else float("nan")
    return (spread, pct)


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def sanity_checks(fit: FitResult, baseline: List[Point],
                  points: List[Point]) -> List[Check]:
    checks: List[Check] = []

    # (a) R² > 0.9999 in the linear region
    checks.append(Check(
        "R² > 0.9999",
        fit.r_squared > 0.9999,
        f"R² = {fit.r_squared:.6f}"))

    # (b) Residuals not S-shaped
    signs = np.sign(fit.residuals_mV)
    sign_changes = int(np.sum(signs[1:] != signs[:-1]))
    expected_min = max(2, len(fit.residuals_mV) // 4)
    checks.append(Check(
        "residuals not S-shaped",
        sign_changes >= expected_min,
        f"{sign_changes} sign changes across {len(fit.residuals_mV)} "
        f"points (expect >= {expected_min})"))

    # (c) Per-point sigma roughly constant
    stds = np.array([p.std_V for p in points], dtype=float)
    if len(stds) > 1 and stds.mean() > 0:
        cv = stds.std(ddof=0) / stds.mean()
        checks.append(Check(
            "sigma roughly constant across sweep",
            cv < 0.5,
            f"sigma_mean = {stds.mean():.2e} V  CV = {cv*100:.1f}%"))
    else:
        checks.append(Check(
            "sigma roughly constant", False,
            "not enough points"))

    # (d) Baseline drift within noise
    pre = next((p for p in baseline if p.direction == "baseline_pre"), None)
    post = next((p for p in baseline if p.direction == "baseline_post"), None)
    if pre and post:
        drift = post.mean_V - pre.mean_V
        ref_sigma = max(pre.std_V, 1e-9)
        checks.append(Check(
            "baseline drift within noise",
            abs(drift) <= 3.0 * ref_sigma,
            f"drift = {drift*1000:+.3f} mV  "
            f"(|drift|/sigma = {abs(drift)/ref_sigma:.2f})"))
    else:
        checks.append(Check(
            "baseline drift within noise", False,
            "pre/post baselines not found"))

    # (e) Sensitivity is positive (force up → voltage up for LCA-9PC)
    checks.append(Check(
        "sensitivity is positive",
        fit.sensitivity_mV_per_mN > 0,
        f"sensitivity = {fit.sensitivity_mV_per_mN:.4f} mV/mN"))

    return checks


# ---------------------------------------------------------------------------
# SVG plot
# ---------------------------------------------------------------------------
def plot_fit(fit: FitResult, trimmed: List[Point],
             out_path: Path, title: str) -> None:
    if not _HAS_MPL:
        logging.warning("matplotlib not available — skipping plot")
        return

    fig, (ax_main, ax_res) = plt.subplots(
        2, 1, figsize=(8, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05})

    # Main: voltage vs force
    ax_main.plot(fit.force_mN, fit.v_mV, "o", markersize=4,
                 color="#2563eb", label="linear region")
    if trimmed:
        trim_f = np.array([p.expected_force_mN for p in trimmed])
        trim_v = np.array([p.mean_V for p in trimmed]) * 1000.0
        ax_main.plot(trim_f, trim_v, "x", markersize=5,
                     color="#dc2626", label=f"soft zone ({len(trimmed)} trimmed)")

    f_line = np.array([fit.force_mN.min(), fit.force_mN.max()])
    sign = "+" if fit.v0_mV >= 0 else "−"
    ax_main.plot(f_line,
                 fit.sensitivity_mV_per_mN * f_line + fit.v0_mV,
                 "-", linewidth=1.5, color="#16a34a",
                 label=f"fit: {fit.sensitivity_mV_per_mN:.4f} × F "
                       f"{sign} {abs(fit.v0_mV):.2f} mV")
    ax_main.set_ylabel("voltage (mV)")
    ax_main.set_title(title)
    ax_main.legend(loc="best", fontsize=9)
    ax_main.grid(True, alpha=0.3)

    note = (f"R² = {fit.r_squared:.6f}\n"
            f"sensitivity = {fit.sensitivity_mV_per_mN:.4f} mV/mN\n"
            f"max |residual| = {fit.max_abs_residual_mV:.3f} mV")
    ax_main.text(0.02, 0.98, note, transform=ax_main.transAxes,
                 va="top", fontsize=9,
                 bbox=dict(facecolor="white", alpha=0.8, edgecolor="gray"))

    # Residuals
    ax_res.axhline(0, color="black", linewidth=0.6)
    ax_res.plot(fit.force_mN, fit.residuals_mV, "o-", markersize=3,
                linewidth=0.8, color="#2563eb")
    ax_res.set_xlabel("expected force (mN)")
    ax_res.set_ylabel("residual (mV)")
    ax_res.grid(True, alpha=0.3)

    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Read spring k from meta.json (if available alongside points.csv)
# ---------------------------------------------------------------------------
def _read_spring_k(points_path: Path) -> float:
    """Try to read spring_k from the meta.json next to the points file."""
    meta_path = points_path.with_name(
        points_path.name.replace("_points.csv", "_meta.json"))
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
        spring = meta.get("spring", {})
        k = spring.get("k_mN_per_mm")
        if k is not None:
            return float(k)
    # Fallback: Instron pooled value
    return 30.86


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    p = argparse.ArgumentParser(
        description="Analyze a load cell calibration run")
    p.add_argument("points_csv", nargs="?", default=None,
                   help="path to <prefix>_points.csv  "
                        "(default: latest in data/)")
    p.add_argument("--no-plot", action="store_true",
                   help="skip SVG output")
    p.add_argument("--json-out", action="store_true",
                   help="also write <prefix>_fit.json")
    p.add_argument("--no-save-cal", action="store_true",
                   help="skip writing calibration.json")
    p.add_argument("--r2-target", type=float, default=0.9999,
                   help="R² threshold for linear region detection (default 0.9999)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("analyze")

    # Resolve the points file
    if args.points_csv is not None:
        points_path = Path(args.points_csv)
    else:
        points_path = find_latest_points_csv()
        if points_path is None:
            log.error("No *_points.csv files found in %s. "
                      "Run a calibration first or supply a path.",
                      _DEFAULT_DATA_DIR)
            sys.exit(1)
        log.info("Auto-selected latest: %s", points_path)

    if not points_path.exists():
        log.error("File not found: %s", points_path)
        sys.exit(1)

    sweep_all, baseline = load_points(points_path)
    if len(sweep_all) < 3:
        log.error("Need at least 3 sweep points (got %d).", len(sweep_all))
        sys.exit(2)

    spring_k = _read_spring_k(points_path)

    # ---- Linear region detection -------------------------------------------
    linear_pts, trimmed_pts = find_linear_region(
        sweep_all, r2_target=args.r2_target)
    fit = linear_fit(linear_pts)

    print()
    print("=" * 64)
    print(f"  Load Cell Calibration — {points_path.name}")
    print("=" * 64)
    print(f"  sensitivity         : {fit.sensitivity_mV_per_mN:.4f} mV/mN")
    print(f"                      : {fit.sensitivity_mV_per_mN:.4f} V/N")
    print(f"  offset V₀           : {fit.v0_mV:.3f} mV")
    print(f"  R²                  : {fit.r_squared:.6f}")
    print(f"  max |residual|      : {fit.max_abs_residual_mV:.3f} mV")
    print(f"  points used         : {fit.n_points}")
    if trimmed_pts:
        print(f"  soft-zone trimmed   : {len(trimmed_pts)} points "
              f"(< {trimmed_pts[-1].expected_force_mN:.1f} mN)")
    else:
        print(f"  soft-zone trimmed   : 0 (all points in linear region)")
    print(f"  spring k            : {spring_k:.2f} mN/mm")
    print(f"  conversion          : F(mN) = (V_mV - {fit.v0_mV:.3f}) "
          f"/ {fit.sensitivity_mV_per_mN:.4f}")
    print()

    # ---- Sanity checks -----------------------------------------------------
    print("  Sanity checks:")
    checks = sanity_checks(fit, baseline, linear_pts)
    all_passed = all(c.passed for c in checks)
    for c in checks:
        mark = "PASS" if c.passed else "FAIL"
        print(f"    [{mark}]  {c.name}")
        print(f"            {c.detail}")
    print()

    # ---- Save calibration.json ---------------------------------------------
    if not args.no_save_cal:
        cal_path = save_calibration(fit, all_passed, points_path,
                                    len(trimmed_pts), spring_k)
        log.info("Wrote %s  (all_checks_passed=%s)", cal_path, all_passed)

    # ---- Per-pass decomposition --------------------------------------------
    pp_fits = per_pass_fits(linear_pts)
    if len(pp_fits) > 1:
        print("  Per-pass fits (repeatability):")
        for pid in sorted(pp_fits):
            pf = pp_fits[pid]
            print(f"    pass {pid}: sens = {pf.sensitivity_mV_per_mN:.4f} mV/mN"
                  f"   V₀ = {pf.v0_mV:+.3f} mV   R² = {pf.r_squared:.6f}"
                  f"   (n = {pf.n_points})")
        k_spread, k_spread_pct = pass_to_pass_spread(pp_fits)
        print(f"    spread: {k_spread:.4f} mV/mN ({k_spread_pct:.2f}% of mean)")
        print()

    # ---- Per-direction decomposition (hysteresis) --------------------------
    pd_fits = per_direction_fits(linear_pts)
    if len(pd_fits) > 1:
        print("  Per-direction fits (hysteresis):")
        for tag in sorted(pd_fits):
            df = pd_fits[tag]
            print(f"    {tag:>3}: sens = {df.sensitivity_mV_per_mN:.4f} mV/mN"
                  f"   V₀ = {df.v0_mV:+.3f} mV   R² = {df.r_squared:.6f}"
                  f"   (n = {df.n_points})")
        if "fwd" in pd_fits and "rev" in pd_fits:
            dk = pd_fits["fwd"].sensitivity_mV_per_mN - pd_fits["rev"].sensitivity_mV_per_mN
            dv0 = pd_fits["fwd"].v0_mV - pd_fits["rev"].v0_mV
            # Convert V₀ difference to force equivalent
            hyst_mN = abs(dv0 / fit.sensitivity_mV_per_mN) \
                if fit.sensitivity_mV_per_mN != 0 else float("nan")
            print(f"    fwd − rev: Δsens = {dk:+.4f} mV/mN   "
                  f"ΔV₀ = {dv0:+.3f} mV   "
                  f"(≈ {hyst_mN:.2f} mN force equivalent)")
        print()

    # ---- Cross-compare (ADC2 vs ADC1) --------------------------------------
    fit_adc2: Optional[FitResult] = None
    if has_xcompare(sweep_all):
        a2_all = adc2_points(sweep_all)
        # Apply the same linear-region mask (by force threshold)
        if trimmed_pts:
            trim_threshold = trimmed_pts[-1].expected_force_mN
            a2 = [p for p in a2_all if p.expected_force_mN > trim_threshold]
        else:
            a2 = a2_all
        if len(a2) >= 3:
            fit_adc2 = linear_fit(a2)
            print("  Cross-compare (both ADCs on AIN2/AIN3):")
            print(f"    ADC1 (primary, 32-bit): sens = "
                  f"{fit.sensitivity_mV_per_mN:.4f} mV/mN   "
                  f"V₀ = {fit.v0_mV:+.3f} mV   R² = {fit.r_squared:.6f}")
            print(f"    ADC2 (xcheck,  24-bit): sens = "
                  f"{fit_adc2.sensitivity_mV_per_mN:.4f} mV/mN   "
                  f"V₀ = {fit_adc2.v0_mV:+.3f} mV   R² = {fit_adc2.r_squared:.6f}")
            dk = fit.sensitivity_mV_per_mN - fit_adc2.sensitivity_mV_per_mN
            mean_k = 0.5 * (fit.sensitivity_mV_per_mN + fit_adc2.sensitivity_mV_per_mN)
            agree_pct = (100.0 * abs(dk) / abs(mean_k)
                         if mean_k != 0 else float("nan"))
            print(f"    Δsens = {dk:+.4f} mV/mN   "
                  f"|Δ|/|mean| = {agree_pct:.3f}%")
            # Per-point voltage agreement
            xc_pairs = [(p.mean_V, p.mean_V_adc2)
                        for p in sweep_all
                        if p.mean_V_adc2 is not None
                        and (not trimmed_pts
                             or p.expected_force_mN > trimmed_pts[-1].expected_force_mN)]
            if xc_pairs:
                diffs = np.array([(v1 - v2) * 1000.0 for v1, v2 in xc_pairs])
                print(f"    per-point ΔV (ADC1 − ADC2): "
                      f"mean = {diffs.mean():+.3f} mV   "
                      f"σ = {diffs.std(ddof=0):.3f} mV   "
                      f"(over {len(diffs)} points)")
            print()

    # ---- SVG plot ----------------------------------------------------------
    if not args.no_plot:
        svg_path = points_path.with_name(
            points_path.name.replace("_points.csv", "_fit.svg"))
        plot_fit(fit, trimmed_pts, svg_path, title=points_path.stem)
        log.info("Wrote %s", svg_path)

    # ---- Optional JSON output ----------------------------------------------
    if args.json_out:
        json_path = points_path.with_name(
            points_path.name.replace("_points.csv", "_fit.json"))
        out: dict = {
            "sensitivity_mV_per_mN": fit.sensitivity_mV_per_mN,
            "v0_mV": fit.v0_mV,
            "r_squared": fit.r_squared,
            "max_abs_residual_mV": fit.max_abs_residual_mV,
            "n_points_used": fit.n_points,
            "n_points_trimmed": len(trimmed_pts),
            "spring_k_mN_per_mm": spring_k,
            "source": points_path.name,
        }
        if len(pp_fits) > 1:
            k_spread, k_spread_pct = pass_to_pass_spread(pp_fits)
            out["per_pass"] = {
                str(pid): {
                    "sensitivity_mV_per_mN": pf.sensitivity_mV_per_mN,
                    "v0_mV": pf.v0_mV,
                    "r_squared": pf.r_squared,
                    "n_points": pf.n_points,
                }
                for pid, pf in pp_fits.items()
            }
            out["pass_to_pass_spread_pct"] = k_spread_pct
        if len(pd_fits) > 1:
            out["per_direction"] = {
                tag: {
                    "sensitivity_mV_per_mN": df.sensitivity_mV_per_mN,
                    "v0_mV": df.v0_mV,
                    "r_squared": df.r_squared,
                    "n_points": df.n_points,
                }
                for tag, df in pd_fits.items()
            }
        if fit_adc2 is not None:
            out["xcompare"] = {
                "adc2_fit": {
                    "sensitivity_mV_per_mN": fit_adc2.sensitivity_mV_per_mN,
                    "v0_mV": fit_adc2.v0_mV,
                    "r_squared": fit_adc2.r_squared,
                },
                "delta_sensitivity": (
                    fit.sensitivity_mV_per_mN - fit_adc2.sensitivity_mV_per_mN
                ),
            }
        with open(json_path, "w") as f:
            json.dump(out, f, indent=2)
        log.info("Wrote %s", json_path)


if __name__ == "__main__":
    _main()
