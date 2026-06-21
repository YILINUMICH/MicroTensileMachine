#!/usr/bin/env python3
"""
analyze.py - offline fit + plot for a calibration run.

Reads one points.csv produced by run_calibration.py, fits a line
V(x) = k*x + V0 in mV vs um, reports the sanity checks from
Calibrate_LaserHead_Plan.md section 7, and writes <prefix>_fit.svg next
to the input.

Developed against already-saved data so it can be iterated without the
hardware present (plan section 9.5).

Usage:

    python analyze.py                                  # auto-detect latest
    python analyze.py data/2026-04-23_run01_points.csv  # explicit path

After every run, writes (or overwrites) calibration.json next to this
script so downstream modules can ``from analyze import load_calibration``
or just read the JSON directly.

Author: Yilin Ma - HDR Lab, University of Michigan
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

_THIS_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = _THIS_DIR / "data"
_CALIBRATION_JSON = _THIS_DIR / "calibration.json"


# ---------------------------------------------------------------------------
# Auto-discovery: find the latest *_points.csv
# ---------------------------------------------------------------------------
def find_latest_points_csv(data_dir: Path = _DEFAULT_DATA_DIR) -> Optional[Path]:
    """
    Glob for ``*_points.csv`` under *data_dir* and return the one with the
    lexicographically greatest name. Because run_calibration.py names files
    ``YYYY-MM-DD_runNN_points.csv``, the sort order is chronological.

    Returns ``None`` if no points files exist.
    """
    candidates = sorted(data_dir.glob("*_points.csv"))
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Calibration persistence
# ---------------------------------------------------------------------------
def save_calibration(fit: "FitResult",
                     checks_passed: bool,
                     source_csv: Path,
                     out_path: Path = _CALIBRATION_JSON) -> Path:
    """
    Write (or overwrite) the canonical calibration.json used by downstream
    modules (e.g. Experiment_SMACharacterizationV2).

    Fields
    ------
    k_mV_per_um     : fitted sensitivity (slope).
    V0_mV           : fitted intercept at x = 0.
    r_squared       : goodness of fit.
    max_abs_residual_mV, linearity_pct_fs : worst-case residual stats.
    all_checks_passed : True iff every sanity check from §7 passed.
    source          : basename of the points.csv that produced this fit.
    updated_utc     : ISO-8601 timestamp of this write.
    conversion      : human-readable formula reminder.
    """
    payload = {
        "k_mV_per_um": fit.k_mV_per_um,
        "V0_mV": fit.v0_mV,
        "r_squared": fit.r_squared,
        "max_abs_residual_mV": fit.max_abs_residual_mV,
        "linearity_pct_fs": fit.linearity_pct_fs,
        "all_checks_passed": checks_passed,
        "source": source_csv.name,
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "conversion": "displacement_um = (V_mV - V0_mV) / k_mV_per_um",
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    return out_path


def load_calibration(cal_path: Path = _CALIBRATION_JSON) -> dict:
    """
    Load the canonical calibration.json.

    Returns a dict with at least ``k_mV_per_um`` and ``V0_mV``.
    Raises FileNotFoundError if the file does not exist (i.e. no calibration
    has been run yet).

    Importable by other modules::

        from Calibrate_LaserHead.analyze import load_calibration
        cal = load_calibration()
        k   = cal["k_mV_per_um"]
        v0  = cal["V0_mV"]
    """
    with open(cal_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Load points.csv
# ---------------------------------------------------------------------------
@dataclass
class Point:
    target_mm: float
    stage_actual_mm: float
    mean_V: float                            # PRIMARY channel — ADC2 in xcompare runs
    std_V: float
    n_samples: int
    direction: str
    pass_index: int = 0     # 0..passes-1 for sweep rows; -1 for baseline
                            # rows. Defaulted for backward compat with
                            # points.csv files written before 2026-05-26.
    # Cross-compare secondary (ADC1 reading the same AIN4/AIN5). Populated
    # only when the points.csv was written by a run with xcompare:true.
    mean_V_adc1: Optional[float] = None
    std_V_adc1: Optional[float] = None
    n_samples_adc1: Optional[int] = None


def _try_float(s: object) -> Optional[float]:
    if s in (None, ""):
        return None
    try:
        return float(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _try_int(s: object) -> Optional[int]:
    if s in (None, ""):
        return None
    try:
        return int(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def load_points(path: Path) -> Tuple[List[Point], List[Point]]:
    """Return (sweep_points, baseline_points)."""
    sweep: List[Point] = []
    baseline: List[Point] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # pass_index is optional — pre-2026-05-26 runs don't have it.
            # Fall back to 0 so legacy single-pass files behave as before.
            raw_pi = row.get("pass_index", "0")
            try:
                pi = int(raw_pi) if raw_pi not in (None, "") else 0
            except ValueError:
                pi = 0
            p = Point(
                target_mm=float(row["target_mm"]),
                stage_actual_mm=float(row["stage_actual_mm"]),
                mean_V=float(row["mean_V"]),
                std_V=float(row["std_V"]),
                n_samples=int(row["n_samples"]),
                direction=row["direction"],
                pass_index=pi,
                # ADC1 cross-compare columns are optional — present only in
                # xcompare runs. _try_float/_try_int return None for missing
                # or empty cells so single-channel files load unchanged.
                mean_V_adc1=_try_float(row.get("mean_V_adc1")),
                std_V_adc1=_try_float(row.get("std_V_adc1")),
                n_samples_adc1=_try_int(row.get("n_samples_adc1")),
            )
            if p.direction.startswith("baseline"):
                baseline.append(p)
            else:
                sweep.append(p)
    return sweep, baseline


def has_xcompare(sweep: List[Point]) -> bool:
    """True if at least one sweep point carries ADC1 cross-compare data."""
    return any(p.mean_V_adc1 is not None for p in sweep)


def adc1_points(sweep: List[Point]) -> List[Point]:
    """
    Build a Point list where mean_V/std_V/n_samples come from ADC1 instead
    of ADC2, so the existing fit/sanity machinery can run on the ADC1
    channel unchanged. Only includes points that actually have ADC1 data.
    """
    out: List[Point] = []
    for p in sweep:
        if p.mean_V_adc1 is None:
            continue
        out.append(Point(
            target_mm=p.target_mm,
            stage_actual_mm=p.stage_actual_mm,
            mean_V=p.mean_V_adc1,
            std_V=p.std_V_adc1 if p.std_V_adc1 is not None else 0.0,
            n_samples=p.n_samples_adc1 if p.n_samples_adc1 is not None
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
# Multi-pass / multi-direction decompositions
# ---------------------------------------------------------------------------
def per_pass_fits(sweep: List[Point], use_stage_actual: bool = True
                  ) -> Dict[int, FitResult]:
    """One linear fit per pass_index. Empty dict if no points."""
    pass_ids = sorted({p.pass_index for p in sweep})
    out: Dict[int, FitResult] = {}
    for pid in pass_ids:
        subset = [p for p in sweep if p.pass_index == pid]
        if len(subset) >= 3:
            out[pid] = linear_fit(subset, use_stage_actual=use_stage_actual)
    return out


def per_direction_fits(sweep: List[Point], use_stage_actual: bool = True
                       ) -> Dict[str, FitResult]:
    """One linear fit per direction tag (e.g. 'fwd', 'rev')."""
    tags = sorted({p.direction for p in sweep})
    out: Dict[str, FitResult] = {}
    for tag in tags:
        subset = [p for p in sweep if p.direction == tag]
        if len(subset) >= 3:
            out[tag] = linear_fit(subset, use_stage_actual=use_stage_actual)
    return out


def pass_to_pass_spread(fits: Dict[int, FitResult]) -> Tuple[float, float]:
    """Return (k_spread_mV_per_um, k_spread_pct_of_mean) across passes."""
    if len(fits) < 2:
        return (0.0, 0.0)
    ks = [f.k_mV_per_um for f in fits.values()]
    spread = max(ks) - min(ks)
    mean_k = sum(ks) / len(ks)
    pct = 100.0 * spread / abs(mean_k) if mean_k != 0 else float("nan")
    return (spread, pct)


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

    # (a) sensitivity within ~5% of 0.5 mV/um (magnitude only — sign just
    # records the geometry convention; on this rig stage 5 mm = max V and
    # stage 15 mm = min V, so k is negative by construction).
    abs_k = abs(fit.k_mV_per_um)
    pct_dev = 100.0 * abs(abs_k - IL030_NOMINAL_K_MV_PER_UM) \
        / IL030_NOMINAL_K_MV_PER_UM
    checks.append(Check(
        "|sensitivity| within 5% of 0.5 mV/um",
        pct_dev <= 5.0,
        f"k = {fit.k_mV_per_um:+.4f} mV/um  "
        f"(|k| = {abs_k:.4f},  {pct_dev:+.2f}% vs nominal)"))

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

    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    p = argparse.ArgumentParser(description="analyze a calibration run")
    p.add_argument("points_csv", nargs="?", default=None,
                   help="path to <prefix>_points.csv  "
                        "(default: latest file in data/)")
    p.add_argument("--use-target", action="store_true",
                   help="fit using commanded target_mm instead of "
                        "stage_actual_mm (default: stage_actual_mm)")
    p.add_argument("--no-plot", action="store_true",
                   help="skip the SVG output")
    p.add_argument("--json-out", action="store_true",
                   help="also emit <prefix>_fit.json with numeric results")
    p.add_argument("--no-save-cal", action="store_true",
                   help="skip writing/updating calibration.json")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("analyze")

    # Resolve the points file — explicit arg or latest auto-discovery.
    if args.points_csv is not None:
        points_path = Path(args.points_csv)
    else:
        points_path = find_latest_points_csv()
        if points_path is None:
            log.error("No *_points.csv files found in %s. "
                      "Run a calibration sweep first or supply a path.",
                      _DEFAULT_DATA_DIR)
            sys.exit(1)
        log.info("Auto-selected latest: %s", points_path)

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
    checks = sanity_checks(fit, baseline, sweep)
    all_passed = all(c.passed for c in checks)
    for c in checks:
        mark = "PASS" if c.passed else "FAIL"
        print(f"    [{mark}]  {c.name}")
        print(f"            {c.detail}")
    print()

    # -- Save calibration.json ---------------------------------------------
    if not args.no_save_cal:
        cal_path = save_calibration(fit, all_passed, points_path)
        log.info("Wrote %s  (all_checks_passed=%s)", cal_path, all_passed)

    # -- Per-pass decomposition (repeatability) ----------------------------
    pp_fits = per_pass_fits(sweep, use_stage_actual=not args.use_target)
    if len(pp_fits) > 1:
        print("  Per-pass fits (for repeatability):")
        for pid in sorted(pp_fits):
            pf = pp_fits[pid]
            print(f"    pass {pid}: k = {pf.k_mV_per_um:.4f} mV/um   "
                  f"V0 = {pf.v0_mV:+.3f} mV   R^2 = {pf.r_squared:.6f}   "
                  f"(n = {len(pf.x_um)})")
        k_spread, k_spread_pct = pass_to_pass_spread(pp_fits)
        print(f"    pass-to-pass spread: |max - min| k = "
              f"{k_spread:.4f} mV/um ({k_spread_pct:.2f}% of mean)")
        print()

    # -- Per-direction decomposition (hysteresis) --------------------------
    pd_fits = per_direction_fits(sweep, use_stage_actual=not args.use_target)
    if len(pd_fits) > 1:
        print("  Per-direction fits (for hysteresis):")
        for tag in sorted(pd_fits):
            df = pd_fits[tag]
            print(f"    {tag:>3}: k = {df.k_mV_per_um:.4f} mV/um   "
                  f"V0 = {df.v0_mV:+.3f} mV   R^2 = {df.r_squared:.6f}   "
                  f"(n = {len(df.x_um)})")
        if "fwd" in pd_fits and "rev" in pd_fits:
            dk = pd_fits["fwd"].k_mV_per_um - pd_fits["rev"].k_mV_per_um
            dv0 = pd_fits["fwd"].v0_mV - pd_fits["rev"].v0_mV
            # Hysteresis at sweep center: V_fwd(0) - V_rev(0) = ΔV0 (in mV).
            # Convert to a position equivalent using the overall k for intuition.
            hyst_um = abs(dv0 / fit.k_mV_per_um) if fit.k_mV_per_um != 0 \
                else float("nan")
            print(f"    fwd - rev: Δk = {dk:+.4f} mV/um   "
                  f"ΔV0 = {dv0:+.3f} mV   "
                  f"(≈ {hyst_um:.2f} um position equivalent)")
        print()

    # -- Cross-compare with ADC1 (when present) ----------------------------
    # Independent linear fit on the ADC1 channel that sampled the SAME
    # physical signal (AIN4/AIN5) as ADC2. Two ADCs converging on the
    # same k/V0 is strong evidence the digital path is clean.
    fit_adc1: Optional[FitResult] = None
    if has_xcompare(sweep):
        a1 = adc1_points(sweep)
        if len(a1) >= 3:
            fit_adc1 = linear_fit(a1, use_stage_actual=not args.use_target)
            print("  Cross-compare with ADC1 (both ADCs on AIN4/AIN5):")
            print(f"    ADC2 (primary): k = {fit.k_mV_per_um:.4f} mV/um   "
                  f"V0 = {fit.v0_mV:+.3f} mV   R^2 = {fit.r_squared:.6f}")
            print(f"    ADC1 (xcheck):  k = {fit_adc1.k_mV_per_um:.4f} mV/um   "
                  f"V0 = {fit_adc1.v0_mV:+.3f} mV   R^2 = {fit_adc1.r_squared:.6f}")
            dk = fit.k_mV_per_um - fit_adc1.k_mV_per_um
            dv0 = fit.v0_mV - fit_adc1.v0_mV
            mean_k = 0.5 * (fit.k_mV_per_um + fit_adc1.k_mV_per_um)
            k_agree_pct = (100.0 * abs(dk) / abs(mean_k)
                           if mean_k != 0 else float("nan"))
            print(f"    ADC2 - ADC1:    Δk = {dk:+.4f} mV/um   "
                  f"ΔV0 = {dv0:+.3f} mV   "
                  f"|Δk|/|mean(k)| = {k_agree_pct:.3f}%")
            # Per-point mean-V agreement: if the two ADCs report the same
            # voltage at each stage position, the cross-check is also clean
            # at the sample level, not just at the slope level.
            diffs = np.array(
                [(p.mean_V - p.mean_V_adc1) * 1000.0
                 for p in sweep if p.mean_V_adc1 is not None], dtype=float
            )
            if diffs.size:
                bias_mV = float(diffs.mean())
                spread_mV = float(diffs.std(ddof=0))
                print(f"    per-point ΔV (ADC2 - ADC1): "
                      f"mean = {bias_mV:+.3f} mV   "
                      f"σ = {spread_mV:.3f} mV   "
                      f"(over {diffs.size} points)")
            print()

    if not args.no_plot:
        svg_path = points_path.with_name(
            points_path.name.replace("_points.csv", "_fit.svg"))
        plot_fit(fit, svg_path, title=points_path.stem)
        log.info("Wrote %s", svg_path)

    if args.json_out:
        json_path = points_path.with_name(
            points_path.name.replace("_points.csv", "_fit.json"))
        out: dict = {
            "k_mV_per_um": fit.k_mV_per_um,
            "v0_mV": fit.v0_mV,
            "r_squared": fit.r_squared,
            "max_abs_residual_mV": fit.max_abs_residual_mV,
            "linearity_pct_fs": fit.linearity_pct_fs,
            "n_points": len(sweep),
            "source": points_path.name,
        }
        if len(pp_fits) > 1:
            k_spread, k_spread_pct = pass_to_pass_spread(pp_fits)
            out["per_pass"] = {
                str(pid): {
                    "k_mV_per_um": pf.k_mV_per_um,
                    "v0_mV": pf.v0_mV,
                    "r_squared": pf.r_squared,
                    "n_points": len(pf.x_um),
                }
                for pid, pf in pp_fits.items()
            }
            out["pass_to_pass_k_spread_mV_per_um"] = k_spread
            out["pass_to_pass_k_spread_pct"] = k_spread_pct
        if len(pd_fits) > 1:
            out["per_direction"] = {
                tag: {
                    "k_mV_per_um": df.k_mV_per_um,
                    "v0_mV": df.v0_mV,
                    "r_squared": df.r_squared,
                    "n_points": len(df.x_um),
                }
                for tag, df in pd_fits.items()
            }
            if "fwd" in pd_fits and "rev" in pd_fits:
                out["fwd_minus_rev_k_mV_per_um"] = (
                    pd_fits["fwd"].k_mV_per_um - pd_fits["rev"].k_mV_per_um
                )
                out["fwd_minus_rev_v0_mV"] = (
                    pd_fits["fwd"].v0_mV - pd_fits["rev"].v0_mV
                )
        if fit_adc1 is not None:
            out["xcompare"] = {
                "adc1_fit": {
                    "k_mV_per_um": fit_adc1.k_mV_per_um,
                    "v0_mV": fit_adc1.v0_mV,
                    "r_squared": fit_adc1.r_squared,
                    "max_abs_residual_mV": fit_adc1.max_abs_residual_mV,
                    "linearity_pct_fs": fit_adc1.linearity_pct_fs,
                },
                "delta_k_mV_per_um": fit.k_mV_per_um - fit_adc1.k_mV_per_um,
                "delta_v0_mV": fit.v0_mV - fit_adc1.v0_mV,
                "k_agreement_pct": (
                    100.0 * abs(fit.k_mV_per_um - fit_adc1.k_mV_per_um) /
                    abs(0.5 * (fit.k_mV_per_um + fit_adc1.k_mV_per_um))
                    if (fit.k_mV_per_um + fit_adc1.k_mV_per_um) != 0
                    else None
                ),
            }
        with open(json_path, "w") as f:
            json.dump(out, f, indent=2)
        log.info("Wrote %s", json_path)


if __name__ == "__main__":
    _main()
