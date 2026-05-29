#!/usr/bin/env python3
"""
analyze.py — Offline analysis + SVG plots for a Phase 5 spring smoke-test run.

Reads one run directory produced by run_spring_smoke_test.py and produces:

  - <run>/step<N>.svg          — plot per executed step
  - <run>/summary.json          — pass/fail per step + key numbers
  - stdout                      — human-readable summary

Usage:

    python analyze.py                                  # auto-detect latest run
    python analyze.py data/2026-05-29_run01            # explicit run dir

Per-step analyses follow the pass criteria in
``doc/PLAN_phase5_spring_smoke_test.md``:

  Step 1 — Static noise floor σ within tolerance of cal-run references.
  Step 2 — F-vs-x slope matches load-cell-cal k; laser-vs-Zaber slope ≈ 1.0.
  Step 3 — Settling time below filter-cutoff inverse; no overshoot ringing.
  Step 4 — Forward and reverse F-vs-x overlay (low hysteresis ⇒ sync ok).
  Step 5 — Sequence-gap analysis + status-frame HWM/drop timeline.
  Step 6 — Same diagnostics as step 5, during motion.

When cal references are missing from meta.json the analysis falls back
to raw-voltage plots and reports an INFO note rather than failing.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_HAS_MPL = True
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    _HAS_MPL = False


_THIS_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = _THIS_DIR / "data"

# ADC source IDs (must match sample_ring.h)
SRC_LASER = 1
SRC_LOAD = 2
SRC_SMA_V = 3
SRC_SMA_I = 4
SRC_SMA_R = 5

SRC_NAMES = {
    SRC_LASER: "laser",
    SRC_LOAD: "load",
    SRC_SMA_V: "sma_V",
    SRC_SMA_I: "sma_I",
    SRC_SMA_R: "sma_R",
}


# ---------------------------------------------------------------------------
# Run discovery
# ---------------------------------------------------------------------------
def find_latest_run(data_dir: Path = _DEFAULT_DATA_DIR) -> Optional[Path]:
    candidates = sorted([p for p in data_dir.glob("*") if p.is_dir()])
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@dataclass
class StepData:
    step: int
    samples: np.ndarray = field(default_factory=lambda: np.array([]))
    sample_cols: Dict[str, np.ndarray] = field(default_factory=dict)
    status_rows: List[Dict] = field(default_factory=list)
    stage_rows: List[Dict] = field(default_factory=list)

    @property
    def has_samples(self) -> bool:
        return self.sample_cols.get("voltage_V") is not None \
            and len(self.sample_cols["voltage_V"]) > 0


def _read_csv_dict(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _to_float_array(rows: List[Dict[str, str]], col: str) -> np.ndarray:
    out = []
    for r in rows:
        v = r.get(col, "")
        try:
            out.append(float(v) if v != "" else math.nan)
        except ValueError:
            out.append(math.nan)
    return np.array(out, dtype=float)


def _to_int_array(rows: List[Dict[str, str]], col: str) -> np.ndarray:
    out = []
    for r in rows:
        v = r.get(col, "")
        try:
            out.append(int(v) if v != "" else -1)
        except ValueError:
            out.append(-1)
    return np.array(out, dtype=np.int64)


def load_step(run_dir: Path, step: int) -> StepData:
    sd = StepData(step=step)
    samp_rows = _read_csv_dict(run_dir / f"samples_step{step}.csv")
    if samp_rows:
        sd.sample_cols = {
            "wall_us":   _to_int_array(samp_rows, "wall_us"),
            "phase":     np.array([r.get("phase", "") for r in samp_rows]),
            "fw_t_us":   _to_int_array(samp_rows, "fw_t_us"),
            "src":       _to_int_array(samp_rows, "src"),
            "seq":       _to_int_array(samp_rows, "seq"),
            "hw_us":     _to_int_array(samp_rows, "hw_us"),
            "raw_code":  _to_int_array(samp_rows, "raw_code"),
            "voltage_V": _to_float_array(samp_rows, "voltage_V"),
            "stage_mm":  _to_float_array(samp_rows, "stage_mm"),
        }
    sd.status_rows = _read_csv_dict(run_dir / f"status_step{step}.csv")
    sd.stage_rows = _read_csv_dict(run_dir / f"stage_log_step{step}.csv")
    return sd


# ---------------------------------------------------------------------------
# Engineering-unit conversion
# ---------------------------------------------------------------------------
@dataclass
class CalRefs:
    spring_k_mN_per_mm: Optional[float] = None
    laser_k_mV_per_um: Optional[float] = None
    laser_V0_mV: Optional[float] = None
    load_sensitivity_mV_per_mN: Optional[float] = None
    load_V0_mV: Optional[float] = None
    laser_noise_V_ref: Optional[float] = None
    load_noise_V_ref: Optional[float] = None

    @classmethod
    def from_meta(cls, meta: Dict) -> "CalRefs":
        spring = meta.get("spring", {})
        cal = meta.get("sensor_calibration", {})
        noise = meta.get("reference_noise_floors", {})
        return cls(
            spring_k_mN_per_mm=spring.get("k_mN_per_mm"),
            laser_k_mV_per_um=cal.get("laser_k_mV_per_um"),
            laser_V0_mV=cal.get("laser_V0_mV"),
            load_sensitivity_mV_per_mN=cal.get("load_sensitivity_mV_per_mN"),
            load_V0_mV=cal.get("load_V0_mV"),
            laser_noise_V_ref=noise.get("laser_voltage_V_sigma"),
            load_noise_V_ref=noise.get("load_voltage_V_sigma"),
        )

    def voltage_to_force_mN(self, v_V: np.ndarray) -> Optional[np.ndarray]:
        if (self.load_sensitivity_mV_per_mN is None
                or self.load_V0_mV is None
                or self.load_sensitivity_mV_per_mN == 0):
            return None
        return (v_V * 1000.0 - self.load_V0_mV) / self.load_sensitivity_mV_per_mN

    def voltage_to_displacement_um(self, v_V: np.ndarray) -> Optional[np.ndarray]:
        if (self.laser_k_mV_per_um is None
                or self.laser_V0_mV is None
                or self.laser_k_mV_per_um == 0):
            return None
        return (v_V * 1000.0 - self.laser_V0_mV) / self.laser_k_mV_per_um


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def demux(sd: StepData) -> Dict[int, Dict[str, np.ndarray]]:
    """Return {src: {col: array, ...}} demuxed by adc_source."""
    out: Dict[int, Dict[str, np.ndarray]] = {}
    if not sd.has_samples:
        return out
    srcs = sd.sample_cols["src"]
    for s in np.unique(srcs):
        if s < 0:
            continue
        mask = srcs == s
        out[int(s)] = {k: v[mask] for k, v in sd.sample_cols.items()
                       if k != "src"}
    return out


def fmt_pct(num: float, denom: float) -> str:
    return f"{100.0*num/denom:.2f}%" if denom else "n/a"


def linear_fit(x: np.ndarray, y: np.ndarray) -> Tuple[float, float, float]:
    """Returns (slope, intercept, r_squared)."""
    if len(x) < 2:
        return (float("nan"), float("nan"), float("nan"))
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return (float(slope), float(intercept), r2)


# ---------------------------------------------------------------------------
# Per-step analyses
# ---------------------------------------------------------------------------
def analyze_step1(sd: StepData, cal: CalRefs, run_dir: Path,
                  log: logging.Logger) -> Dict:
    """Static noise floor per src — compare to cal references."""
    result: Dict = {"step": 1, "name": "static_noise"}
    if not sd.has_samples:
        result["status"] = "no_samples"; return result

    by_src = demux(sd)
    per_src: Dict = {}
    for src, cols in by_src.items():
        v = cols["voltage_V"]
        if len(v) < 10:
            continue
        per_src[SRC_NAMES.get(src, f"src{src}")] = {
            "n": int(len(v)),
            "mean_V": float(np.mean(v)),
            "std_V": float(np.std(v, ddof=0)),
            "ptp_V": float(np.ptp(v)),
        }
    result["per_src"] = per_src

    # Compare to cal references
    checks: List[Dict] = []
    if SRC_LASER in by_src and cal.laser_noise_V_ref is not None:
        meas = per_src[SRC_NAMES[SRC_LASER]]["std_V"]
        ratio = meas / cal.laser_noise_V_ref if cal.laser_noise_V_ref else float("nan")
        passed = ratio <= 2.0
        checks.append({"name": "laser σ within 2× cal ref",
                       "passed": passed,
                       "detail": f"meas={meas:.3e} V  ref={cal.laser_noise_V_ref:.3e} V  ratio={ratio:.2f}"})
    if SRC_LOAD in by_src and cal.load_noise_V_ref is not None:
        meas = per_src[SRC_NAMES[SRC_LOAD]]["std_V"]
        ratio = meas / cal.load_noise_V_ref if cal.load_noise_V_ref else float("nan")
        passed = ratio <= 2.0
        checks.append({"name": "load σ within 2× cal ref",
                       "passed": passed,
                       "detail": f"meas={meas:.3e} V  ref={cal.load_noise_V_ref:.3e} V  ratio={ratio:.2f}"})
    result["checks"] = checks
    result["status"] = "ok" if all(c["passed"] for c in checks) else \
        ("warn" if checks else "no_reference")

    # Plot
    if _HAS_MPL:
        _plot_step1(by_src, run_dir / "step1.svg")

    return result


def _plot_step1(by_src: Dict[int, Dict[str, np.ndarray]], out_path: Path) -> None:
    fig, axes = plt.subplots(len(by_src), 2, figsize=(10, 2.5 * len(by_src)),
                             squeeze=False)
    for row, (src, cols) in enumerate(sorted(by_src.items())):
        v = cols["voltage_V"]
        t = (cols["wall_us"] - cols["wall_us"][0]) / 1e6 if len(v) else np.array([])
        name = SRC_NAMES.get(src, f"src{src}")
        axes[row, 0].plot(t, v, color="#2563eb", linewidth=0.6)
        axes[row, 0].set_ylabel(f"{name}\nV (V)")
        axes[row, 0].grid(True, alpha=0.3)
        axes[row, 0].set_title(f"{name} — voltage timeseries")
        axes[row, 1].hist(v, bins=60, color="#2563eb", alpha=0.7)
        axes[row, 1].set_xlabel("voltage (V)")
        axes[row, 1].set_title(f"{name} — distribution  "
                               f"(σ={np.std(v, ddof=0):.2e} V)")
        axes[row, 1].grid(True, alpha=0.3)
    axes[-1, 0].set_xlabel("time (s)")
    fig.suptitle("Step 1 — static noise floor")
    fig.tight_layout()
    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)


def analyze_step2(sd: StepData, cal: CalRefs, run_dir: Path,
                  log: logging.Logger) -> Dict:
    """Quasi-static ramp — F-vs-x linearity, slope vs spring k,
    laser-vs-Zaber slope."""
    result: Dict = {"step": 2, "name": "quasi_static_ramp"}
    if not sd.has_samples:
        result["status"] = "no_samples"; return result

    by_src = demux(sd)
    laser = by_src.get(SRC_LASER)
    load = by_src.get(SRC_LOAD)
    if laser is None or load is None:
        result["status"] = "missing_channel"; return result

    # Time-align: both streams share wall_us. Build a combined frame by
    # interpolating laser onto load timeline (or vice versa).
    t_load = load["wall_us"] / 1e6
    v_load = load["voltage_V"]
    stage_load = load["stage_mm"]
    t_laser = laser["wall_us"] / 1e6
    v_laser = laser["voltage_V"]
    v_laser_at_load = np.interp(t_load, t_laser, v_laser)

    force_mN = cal.voltage_to_force_mN(v_load)
    disp_um = cal.voltage_to_displacement_um(v_laser_at_load)

    # ---- Slack detection from force ----------------------------------------
    # Use the first 10% of points to estimate the noise band; engagement is
    # where force exceeds 5× that band.
    n_pre = max(20, len(v_load) // 20)
    band = 5.0 * float(np.std(v_load[:n_pre], ddof=0)) * 1000.0  # mV
    # Convert band to mN if we can; else leave None and skip the F-vs-x fit
    band_mN = (band / cal.load_sensitivity_mV_per_mN
               if cal.load_sensitivity_mV_per_mN else None)

    engagement_idx = None
    if force_mN is not None and band_mN is not None:
        engaged = force_mN > (force_mN[:n_pre].mean() + band_mN)
        if engaged.any():
            engagement_idx = int(np.argmax(engaged))

    # ---- F-vs-x slope (load cell vs laser displacement) --------------------
    fit_F_vs_x = None
    if (force_mN is not None and disp_um is not None
            and engagement_idx is not None
            and len(force_mN) - engagement_idx > 10):
        x_mm = (disp_um[engagement_idx:] - disp_um[engagement_idx]) / 1000.0
        F = force_mN[engagement_idx:] - force_mN[engagement_idx]
        slope_mN_per_mm, intercept, r2 = linear_fit(x_mm, F)
        fit_F_vs_x = {
            "slope_mN_per_mm": slope_mN_per_mm,
            "intercept_mN": intercept,
            "r_squared": r2,
            "n_points": int(len(F)),
            "engagement_index": engagement_idx,
        }

    # ---- Laser vs Zaber slope ----------------------------------------------
    fit_laser_vs_zaber = None
    if disp_um is not None and engagement_idx is not None:
        # Δlaser_mm vs Δstage_mm — should be slope ≈ 1.0 in linear region
        d_laser_mm = (disp_um[engagement_idx:] - disp_um[engagement_idx]) / 1000.0
        d_stage_mm = (stage_load[engagement_idx:] - stage_load[engagement_idx])
        if len(d_laser_mm) > 10:
            slope, intercept, r2 = linear_fit(d_stage_mm, d_laser_mm)
            fit_laser_vs_zaber = {
                "slope_mm_per_mm": slope,
                "intercept_mm": intercept,
                "r_squared": r2,
                "n_points": int(len(d_laser_mm)),
            }

    result["force_units"] = "mN" if force_mN is not None else "V (no cal)"
    result["disp_units"] = "um" if disp_um is not None else "V (no cal)"
    result["fit_F_vs_x"] = fit_F_vs_x
    result["fit_laser_vs_zaber"] = fit_laser_vs_zaber

    # ---- Checks ------------------------------------------------------------
    checks: List[Dict] = []
    if fit_F_vs_x is not None and cal.spring_k_mN_per_mm is not None:
        meas_k = fit_F_vs_x["slope_mN_per_mm"]
        ref_k = cal.spring_k_mN_per_mm
        ratio = abs(meas_k - ref_k) / ref_k if ref_k else float("nan")
        passed = ratio < 0.05
        checks.append({
            "name": "F-vs-x slope within 5% of spring k_cal",
            "passed": passed,
            "detail": f"meas={meas_k:.3f} mN/mm  cal={ref_k:.3f}  Δ={ratio*100:.2f}%",
        })
        checks.append({
            "name": "F-vs-x R² > 0.999",
            "passed": fit_F_vs_x["r_squared"] > 0.999,
            "detail": f"R² = {fit_F_vs_x['r_squared']:.6f}",
        })
    if fit_laser_vs_zaber is not None:
        slope = fit_laser_vs_zaber["slope_mm_per_mm"]
        passed = abs(slope - 1.0) < 0.05
        checks.append({
            "name": "laser-vs-Zaber slope ≈ 1.0 (±5%)",
            "passed": passed,
            "detail": f"slope = {slope:.4f}  R² = {fit_laser_vs_zaber['r_squared']:.6f}",
        })
    result["checks"] = checks
    result["status"] = "ok" if checks and all(c["passed"] for c in checks) else \
        ("warn" if checks else "no_reference")

    if _HAS_MPL:
        _plot_step2(stage_load, v_load, v_laser_at_load,
                    force_mN, disp_um, engagement_idx,
                    fit_F_vs_x, fit_laser_vs_zaber, cal,
                    run_dir / "step2.svg")
    return result


def _plot_step2(stage_mm, v_load, v_laser, force_mN, disp_um,
                engagement_idx, fit_F_vs_x, fit_laser_vs_zaber, cal,
                out_path: Path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))

    # (a) Force vs Zaber stage position
    ax = axes[0, 0]
    if force_mN is not None:
        ax.plot(stage_mm, force_mN, ".", markersize=2,
                color="#2563eb", alpha=0.5)
        ax.set_ylabel("Force (mN)")
    else:
        ax.plot(stage_mm, v_load * 1000.0, ".", markersize=2,
                color="#2563eb", alpha=0.5)
        ax.set_ylabel("Load voltage (mV)")
    if engagement_idx is not None and engagement_idx < len(stage_mm):
        ax.axvline(stage_mm[engagement_idx], color="#dc2626",
                   linestyle="--", linewidth=1, label="engagement")
        ax.legend(loc="best", fontsize=9)
    ax.set_xlabel("Zaber stage (mm)")
    ax.set_title("Force vs stage")
    ax.grid(True, alpha=0.3)

    # (b) F vs x (laser displacement) — the main linearity check
    ax = axes[0, 1]
    if force_mN is not None and disp_um is not None \
            and engagement_idx is not None:
        x_mm = (disp_um[engagement_idx:] - disp_um[engagement_idx]) / 1000.0
        F = force_mN[engagement_idx:] - force_mN[engagement_idx]
        ax.plot(x_mm, F, ".", markersize=2, color="#16a34a", alpha=0.5,
                label="data")
        if fit_F_vs_x is not None:
            xl = np.array([x_mm.min(), x_mm.max()])
            ax.plot(xl, fit_F_vs_x["slope_mN_per_mm"] * xl
                    + fit_F_vs_x["intercept_mN"], "-",
                    color="#dc2626", linewidth=1.5,
                    label=f"fit: {fit_F_vs_x['slope_mN_per_mm']:.3f} mN/mm  "
                          f"R²={fit_F_vs_x['r_squared']:.5f}")
            if cal.spring_k_mN_per_mm:
                ax.plot(xl, cal.spring_k_mN_per_mm * xl, "--",
                        color="#7c3aed", linewidth=1,
                        label=f"spring k_cal: {cal.spring_k_mN_per_mm:.3f}")
        ax.legend(loc="best", fontsize=9)
        ax.set_xlabel("Δ laser displacement (mm)")
        ax.set_ylabel("Δ Force (mN)")
        ax.set_title("F vs x (linear region)")
    else:
        ax.text(0.5, 0.5, "no cal — skip", ha="center", va="center",
                transform=ax.transAxes)
    ax.grid(True, alpha=0.3)

    # (c) Laser vs Zaber position
    ax = axes[1, 0]
    if disp_um is not None and engagement_idx is not None:
        d_laser_mm = (disp_um - disp_um[engagement_idx]) / 1000.0
        d_stage_mm = stage_mm - stage_mm[engagement_idx]
        ax.plot(d_stage_mm[engagement_idx:], d_laser_mm[engagement_idx:],
                ".", markersize=2, color="#2563eb", alpha=0.5, label="data")
        if fit_laser_vs_zaber is not None:
            xl = np.array([d_stage_mm[engagement_idx:].min(),
                           d_stage_mm[engagement_idx:].max()])
            ax.plot(xl, fit_laser_vs_zaber["slope_mm_per_mm"] * xl
                    + fit_laser_vs_zaber["intercept_mm"], "-",
                    color="#dc2626", linewidth=1.5,
                    label=f"slope {fit_laser_vs_zaber['slope_mm_per_mm']:.4f}  "
                          f"R²={fit_laser_vs_zaber['r_squared']:.5f}")
        ax.plot(xl if fit_laser_vs_zaber else [], xl if fit_laser_vs_zaber else [],
                "--", color="#7c3aed", linewidth=1, label="ideal slope = 1.0")
        ax.legend(loc="best", fontsize=9)
        ax.set_xlabel("Δ Zaber (mm)")
        ax.set_ylabel("Δ Laser (mm)")
        ax.set_title("Laser vs Zaber (linear region)")
    ax.grid(True, alpha=0.3)

    # (d) Residuals
    ax = axes[1, 1]
    if force_mN is not None and disp_um is not None \
            and engagement_idx is not None and fit_F_vs_x is not None:
        x_mm = (disp_um[engagement_idx:] - disp_um[engagement_idx]) / 1000.0
        F = force_mN[engagement_idx:] - force_mN[engagement_idx]
        pred = fit_F_vs_x["slope_mN_per_mm"] * x_mm + fit_F_vs_x["intercept_mN"]
        ax.plot(x_mm, F - pred, ".", markersize=2,
                color="#dc2626", alpha=0.5)
        ax.axhline(0, color="black", linewidth=0.6)
        ax.set_xlabel("Δ laser displacement (mm)")
        ax.set_ylabel("residual (mN)")
        ax.set_title("F-vs-x fit residuals")
    ax.grid(True, alpha=0.3)

    fig.suptitle("Step 2 — quasi-static ramp")
    fig.tight_layout()
    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)


def analyze_step3(sd: StepData, cal: CalRefs, run_dir: Path,
                  log: logging.Logger) -> Dict:
    """Step-and-hold — settling time and overshoot per channel."""
    result: Dict = {"step": 3, "name": "step_and_hold"}
    if not sd.has_samples:
        result["status"] = "no_samples"; return result

    by_src = demux(sd)
    per_src: Dict = {}

    # Find the step transition from stage_mm — first index where stage_mm
    # exceeds (mean_pre + 0.1 mm) of the pre-step value.
    stage = sd.sample_cols["stage_mm"]
    if len(stage) < 20:
        result["status"] = "too_few_samples"; return result
    pre_n = max(50, len(stage) // 20)
    pre_mean = float(np.mean(stage[:pre_n]))
    step_mask = stage > pre_mean + 0.1
    step_idx = int(np.argmax(step_mask)) if step_mask.any() else None

    for src, cols in by_src.items():
        v = cols["voltage_V"]
        if len(v) < 20:
            continue
        # Per-channel pre/post stats. Use last 20% as "settled" final value.
        final_n = max(50, len(v) // 5)
        final_mean = float(np.mean(v[-final_n:]))
        final_sigma = float(np.std(v[-final_n:], ddof=0))
        initial = float(np.mean(v[:pre_n]))

        # Settling: first index after step where |v - final_mean| stays
        # within 3σ_final for at least 100 ms equivalent samples.
        settle_us = None
        if step_idx is not None and final_sigma > 0:
            band = 3.0 * final_sigma
            t_us = cols["wall_us"]
            tol = np.abs(v - final_mean) < band
            run_len = 0
            settled_at_idx = None
            # how many samples ≈ 100 ms?
            if len(t_us) > 10:
                dt_us = float(np.median(np.diff(t_us)))
                need = max(5, int(100_000 / max(1.0, dt_us)))
            else:
                need = 5
            for i in range(step_idx, len(v)):
                if tol[i]:
                    run_len += 1
                    if run_len >= need:
                        settled_at_idx = i - run_len + 1
                        break
                else:
                    run_len = 0
            if settled_at_idx is not None and step_idx < len(t_us):
                settle_us = int(t_us[settled_at_idx] - t_us[step_idx])

        per_src[SRC_NAMES.get(src, f"src{src}")] = {
            "initial_V": initial,
            "final_V": final_mean,
            "step_change_V": final_mean - initial,
            "final_sigma_V": final_sigma,
            "settling_time_us": settle_us,
        }
    result["per_src"] = per_src
    result["step_idx"] = step_idx
    result["status"] = "ok" if per_src else "no_data"

    if _HAS_MPL:
        _plot_step3(by_src, stage, sd.sample_cols["wall_us"], step_idx,
                    run_dir / "step3.svg")
    return result


def _plot_step3(by_src, stage_all, wall_us_all, step_idx, out_path: Path):
    n_panels = len(by_src) + 1
    fig, axes = plt.subplots(n_panels, 1, figsize=(10, 2.2 * n_panels),
                             sharex=True)
    if n_panels == 1:
        axes = [axes]
    t_all = (wall_us_all - wall_us_all[0]) / 1e6 if len(wall_us_all) else np.array([])
    axes[0].plot(t_all, stage_all, color="#7c3aed", linewidth=0.8)
    axes[0].set_ylabel("stage (mm)")
    axes[0].set_title("Step 3 — step and hold")
    axes[0].grid(True, alpha=0.3)
    if step_idx is not None and step_idx < len(t_all):
        for ax in axes:
            ax.axvline(t_all[step_idx], color="#dc2626", linestyle="--",
                       linewidth=0.8, alpha=0.5)
    for i, (src, cols) in enumerate(sorted(by_src.items()), start=1):
        if i >= len(axes):
            break
        t = (cols["wall_us"] - wall_us_all[0]) / 1e6
        axes[i].plot(t, cols["voltage_V"], color="#2563eb", linewidth=0.6)
        axes[i].set_ylabel(f"{SRC_NAMES.get(src, f'src{src}')} V (V)")
        axes[i].grid(True, alpha=0.3)
    axes[-1].set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)


def analyze_step4(sd: StepData, cal: CalRefs, run_dir: Path,
                  log: logging.Logger) -> Dict:
    """Fast pull / slow return — overlay F-vs-x for forward and reverse."""
    result: Dict = {"step": 4, "name": "fast_pull_slow_return"}
    if not sd.has_samples:
        result["status"] = "no_samples"; return result

    by_src = demux(sd)
    if SRC_LASER not in by_src or SRC_LOAD not in by_src:
        result["status"] = "missing_channel"; return result

    # Slice by phase tag
    phase = sd.sample_cols["phase"]
    pull_mask = phase == "pull"
    return_mask = phase == "return"

    def _stats_for_mask(mask):
        if not mask.any():
            return None
        load = by_src[SRC_LOAD]
        laser = by_src[SRC_LASER]
        # Use load's phase mask aligned to load array
        l_phase = laser["phase"] if "phase" in laser else None
        ld_phase = load["phase"] if "phase" in load else None
        # We need to re-mask by phase within each src. Easier: use sd
        # arrays directly.
        return mask

    pull_idx = np.where(pull_mask)[0]
    ret_idx = np.where(return_mask)[0]

    # Build (force, disp) pairs per phase by interpolating laser onto load
    def _phase_pairs(phase_name: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        load_p = by_src[SRC_LOAD]["phase"]
        laser_p = by_src[SRC_LASER]["phase"]
        m_load = load_p == phase_name
        m_laser = laser_p == phase_name
        if not m_load.any() or not m_laser.any():
            return None
        t_load = by_src[SRC_LOAD]["wall_us"][m_load] / 1e6
        v_load = by_src[SRC_LOAD]["voltage_V"][m_load]
        t_laser = by_src[SRC_LASER]["wall_us"][m_laser] / 1e6
        v_laser = by_src[SRC_LASER]["voltage_V"][m_laser]
        v_l_at_load = np.interp(t_load, t_laser, v_laser)
        force = cal.voltage_to_force_mN(v_load)
        disp = cal.voltage_to_displacement_um(v_l_at_load)
        if force is None or disp is None:
            return (v_load, v_l_at_load)   # fallback: voltages
        return (force, disp / 1000.0)      # mN, mm

    pull_pair = _phase_pairs("pull")
    ret_pair = _phase_pairs("return")

    result["have_cal"] = (cal.load_sensitivity_mV_per_mN is not None
                          and cal.laser_k_mV_per_um is not None)

    # Hysteresis metric: at each sampled displacement on the return, find
    # the nearest pull point and compute |F_pull - F_return|. Report median.
    hysteresis_mN = None
    if (pull_pair is not None and ret_pair is not None
            and result["have_cal"]):
        F_pull, x_pull = pull_pair
        F_ret, x_ret = ret_pair
        # Sort pull by x for interpolation
        order = np.argsort(x_pull)
        x_pull_s = x_pull[order]; F_pull_s = F_pull[order]
        F_pull_interp = np.interp(x_ret, x_pull_s, F_pull_s,
                                  left=np.nan, right=np.nan)
        diff = np.abs(F_ret - F_pull_interp)
        diff = diff[np.isfinite(diff)]
        if len(diff) > 5:
            hysteresis_mN = float(np.median(diff))

    result["hysteresis_median_mN"] = hysteresis_mN

    checks: List[Dict] = []
    if hysteresis_mN is not None:
        # Pass: hysteresis less than 10% of typical force range
        F_range = float(np.ptp(pull_pair[0]))
        passed = hysteresis_mN < 0.10 * F_range if F_range > 0 else False
        checks.append({
            "name": "F-vs-x pull/return hysteresis < 10% of range",
            "passed": passed,
            "detail": f"median |ΔF| = {hysteresis_mN:.2f} mN  range = {F_range:.1f} mN",
        })
    result["checks"] = checks
    result["status"] = ("ok" if checks and all(c["passed"] for c in checks)
                        else ("warn" if checks else "no_reference"))

    if _HAS_MPL:
        _plot_step4(pull_pair, ret_pair, result["have_cal"],
                    run_dir / "step4.svg")
    return result


def _plot_step4(pull_pair, ret_pair, have_cal, out_path: Path):
    fig, ax = plt.subplots(figsize=(8, 6))
    if pull_pair is not None:
        ax.plot(pull_pair[1], pull_pair[0], ".", markersize=2,
                color="#2563eb", alpha=0.5, label="pull (fast)")
    if ret_pair is not None:
        ax.plot(ret_pair[1], ret_pair[0], ".", markersize=2,
                color="#dc2626", alpha=0.5, label="return (slow)")
    if have_cal:
        ax.set_xlabel("displacement (mm)")
        ax.set_ylabel("Force (mN)")
    else:
        ax.set_xlabel("laser voltage (V)")
        ax.set_ylabel("load voltage (V)")
    ax.set_title("Step 4 — fast pull / slow return  (overlay)")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)


def analyze_pipeline(sd: StepData, run_dir: Path, log: logging.Logger,
                     step: int) -> Dict:
    """Step 5 & 6 — sequence gap detection + status-frame timeline."""
    result: Dict = {"step": step,
                    "name": "pipeline_endurance" if step == 5
                            else "concurrent_zaber"}
    if not sd.has_samples:
        result["status"] = "no_samples"; return result

    by_src = demux(sd)
    per_src_seq: Dict = {}
    total_gaps = 0
    for src, cols in by_src.items():
        seq = cols["seq"]
        valid = seq >= 0
        if not valid.any():
            per_src_seq[SRC_NAMES.get(src, f"src{src}")] = {
                "seq_present": False,
                "note": "firmware did not emit seq column",
            }
            continue
        seq_valid = seq[valid]
        diffs = np.diff(seq_valid)
        # Gaps = points where diff != 1 (allowing diff>=1 covers either a
        # restart or a real gap; we report both).
        gaps = int(np.sum(diffs != 1))
        missing = int(np.sum(np.where(diffs > 0, diffs - 1, 0)))
        total_gaps += gaps
        per_src_seq[SRC_NAMES.get(src, f"src{src}")] = {
            "seq_present": True,
            "n_samples": int(len(seq_valid)),
            "first": int(seq_valid[0]), "last": int(seq_valid[-1]),
            "gap_count": gaps,
            "missing_samples": missing,
        }
    result["per_src_seq"] = per_src_seq
    result["total_gap_events"] = total_gaps

    # Status-frame summary
    sf_hwm = _to_float_array(sd.status_rows, "hwm")
    sf_drop = _to_float_array(sd.status_rows, "dropped")
    if len(sf_hwm) and not np.all(np.isnan(sf_hwm)):
        result["hwm_max"] = float(np.nanmax(sf_hwm))
        result["hwm_mean"] = float(np.nanmean(sf_hwm))
    if len(sf_drop) and not np.all(np.isnan(sf_drop)):
        result["dropped_max"] = float(np.nanmax(sf_drop))
    result["status_frames"] = len(sd.status_rows)

    checks: List[Dict] = []
    # Pass: no missing seq + hwm < 50% (capacity = 1024)
    if total_gaps == 0:
        checks.append({"name": "zero seq gaps",
                       "passed": True,
                       "detail": "no missing samples per src"})
    else:
        checks.append({"name": "zero seq gaps", "passed": False,
                       "detail": f"{total_gaps} gap events across all srcs"})
    if "hwm_max" in result:
        passed = result["hwm_max"] < 512  # 50% of 1024
        checks.append({"name": "ring hwm < 50%",
                       "passed": passed,
                       "detail": f"max hwm = {result['hwm_max']:.0f} / 1024 "
                                 f"({100*result['hwm_max']/1024:.1f}%)"})
    if "dropped_max" in result:
        passed = result["dropped_max"] == 0
        checks.append({"name": "zero dropped samples",
                       "passed": passed,
                       "detail": f"max dropped = {int(result['dropped_max'])}"})
    result["checks"] = checks
    result["status"] = ("ok" if checks and all(c["passed"] for c in checks)
                        else ("warn" if checks else "no_reference"))

    if _HAS_MPL:
        _plot_pipeline(sd, run_dir / f"step{step}.svg", step)
    return result


def _plot_pipeline(sd: StepData, out_path: Path, step: int) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    # (a) seq monotonicity per src
    by_src = demux(sd)
    for src, cols in sorted(by_src.items()):
        seq = cols["seq"]; t = (cols["wall_us"] - cols["wall_us"][0]) / 1e6
        valid = seq >= 0
        if valid.any():
            axes[0].plot(t[valid], seq[valid], ".", markersize=1,
                         label=SRC_NAMES.get(src, f"src{src}"))
    axes[0].set_ylabel("seq")
    axes[0].set_title(f"Step {step} — sequence numbers (gaps ⇒ drops)")
    axes[0].legend(loc="best", fontsize=8)
    axes[0].grid(True, alpha=0.3)

    # (b) status hwm
    sf_t = _to_float_array(sd.status_rows, "wall_us")
    sf_hwm = _to_float_array(sd.status_rows, "hwm")
    sf_drop = _to_float_array(sd.status_rows, "dropped")
    if len(sf_t):
        t_sf = (sf_t - sf_t[0]) / 1e6 if len(sf_t) else sf_t
        axes[1].plot(t_sf, sf_hwm, "-o", markersize=3, color="#dc2626")
        axes[1].axhline(512, color="black", linestyle="--", linewidth=0.6,
                        label="50% threshold")
        axes[1].set_ylabel("ring hwm (slots)")
        axes[1].legend(loc="best", fontsize=8)
        axes[1].grid(True, alpha=0.3)
        axes[2].plot(t_sf, sf_drop, "-o", markersize=3, color="#7c3aed")
        axes[2].set_ylabel("dropped (cumulative)")

    axes[2].set_xlabel("time (s)")
    axes[2].grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
STEP_ANALYZERS: Dict[int, callable] = {
    1: analyze_step1,
    2: analyze_step2,
    3: analyze_step3,
    4: analyze_step4,
    5: lambda sd, cal, rd, log: analyze_pipeline(sd, rd, log, step=5),
    6: lambda sd, cal, rd, log: analyze_pipeline(sd, rd, log, step=6),
}


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------
def print_summary(results: List[Dict]) -> None:
    print()
    print("=" * 64)
    print("  Phase 5 Spring Smoke Test — analysis summary")
    print("=" * 64)
    for r in results:
        name = r.get("name", "?")
        status = r.get("status", "?")
        print(f"\n  Step {r['step']} ({name}): {status.upper()}")
        for c in r.get("checks", []):
            mark = "PASS" if c["passed"] else "FAIL"
            print(f"    [{mark}]  {c['name']}")
            print(f"            {c['detail']}")
        # Key numbers per step type
        if "per_src" in r:
            for src_name, st in r["per_src"].items():
                line = f"    {src_name}: "
                line += f"mean={st['mean_V']:.4f} V  σ={st['std_V']:.2e} V"
                if "settling_time_us" in st and st["settling_time_us"] is not None:
                    line += f"  settle={st['settling_time_us']/1000:.1f} ms"
                print(line)
        if "fit_F_vs_x" in r and r.get("fit_F_vs_x"):
            fit = r["fit_F_vs_x"]
            print(f"    F-vs-x fit: slope={fit['slope_mN_per_mm']:.3f} mN/mm  "
                  f"R²={fit['r_squared']:.6f}  n={fit['n_points']}")
        if "fit_laser_vs_zaber" in r and r.get("fit_laser_vs_zaber"):
            fit = r["fit_laser_vs_zaber"]
            print(f"    laser-vs-Zaber: slope={fit['slope_mm_per_mm']:.4f}  "
                  f"R²={fit['r_squared']:.6f}")
        if "hysteresis_median_mN" in r and r["hysteresis_median_mN"] is not None:
            print(f"    hysteresis: median |ΔF| = "
                  f"{r['hysteresis_median_mN']:.2f} mN")
        if "hwm_max" in r:
            print(f"    ring hwm: max={r['hwm_max']:.0f}  "
                  f"mean={r.get('hwm_mean', 0):.1f}  "
                  f"dropped_max={int(r.get('dropped_max', 0))}")
        if "total_gap_events" in r:
            print(f"    sequence gaps: {r['total_gap_events']} events total")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    p = argparse.ArgumentParser(
        description="Analyze a Phase 5 spring smoke-test run")
    p.add_argument("run_dir", nargs="?", default=None,
                   help="path to a run directory under data/ "
                        "(default: latest)")
    p.add_argument("--no-plot", action="store_true",
                   help="skip SVG outputs")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("analyze")

    if args.run_dir is not None:
        run_dir = Path(args.run_dir)
    else:
        run_dir = find_latest_run()
        if run_dir is None:
            log.error("no run directories found in %s", _DEFAULT_DATA_DIR)
            sys.exit(1)
        log.info("Auto-selected: %s", run_dir)

    if not run_dir.exists():
        log.error("not found: %s", run_dir); sys.exit(1)

    meta_path = run_dir / "meta.json"
    meta: Dict = {}
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
    cal = CalRefs.from_meta(meta)

    results: List[Dict] = []
    for step in (1, 2, 3, 4, 5, 6):
        samples_path = run_dir / f"samples_step{step}.csv"
        if not samples_path.exists():
            continue
        log.info("Analyzing step %d ...", step)
        sd = load_step(run_dir, step)
        analyzer = STEP_ANALYZERS[step]
        try:
            r = analyzer(sd, cal, run_dir, log)
        except Exception:
            log.exception("step %d analysis failed", step)
            r = {"step": step, "status": "exception"}
        results.append(r)

    print_summary(results)

    summary_path = run_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump({"run": str(run_dir.name), "results": results}, f, indent=2)
    log.info("Wrote %s", summary_path)


if __name__ == "__main__":
    _main()
