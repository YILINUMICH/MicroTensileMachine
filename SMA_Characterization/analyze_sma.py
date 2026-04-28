#!/usr/bin/env python3
"""
analyze_sma.py — offline de-embedding + LCR/laser join for SMA runs.

Consumes the raw CSVs produced by `sma_recorder.py`:

    <short_prefix>_lcr_raw.csv      (LCR pigtails shorted at the DUT end)
    <run_prefix>_lcr_raw.csv        (actual experiment)
    <run_prefix>_laser_raw.csv      (optional — displacement monitor)

Applies SHORT-only de-embedding (open correction was found not to help in
the Notion Bias-Tee/LCR characterization; series-subtraction of the
parasitic path dominates):

    Z_raw   = Rs + j · ω · Ls                    # per sample
    Z_short = mean(Rs_short) + j · ω · mean(Ls_short)
    Z_DUT   = Z_raw − Z_short
    Ls_DUT  = Im(Z_DUT) / ω
    Rs_DUT  = Re(Z_DUT)
    Q       = |ω · Ls_DUT / Rs_DUT|
    phase_deg = atan2(Im(Z_DUT), Re(Z_DUT)) * 180 / π

If a laser CSV is supplied, each de-embedded LCR sample is tagged with an
interpolated displacement (in mm) using the calibration:

    displacement_mm = (V_mV − V0_mV) / k_mV_per_um × 1e-3

where V0 and k come from either `--v0` / `--k` command-line flags or the
defaults matching `Calibrate_LaserHead/` 2026-04-24 run07
(V0 = 566.957 mV, k = -0.1171 mV/µm).

Usage:
    python analyze_sma.py \\
        --short data/sma_20260424_140000_lcr_raw.csv \\
        --run   data/sma_20260424_150000_lcr_raw.csv \\
        --laser data/sma_20260424_150000_laser_raw.csv

Output (in same folder as --run):
    <run_prefix>_processed.csv
    <run_prefix>_processed.png        (unless --no-plot)

Author: Yilin Ma - HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

_HAS_MPL = True
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    _HAS_MPL = False


# ---------------------------------------------------------------------------
# Default laser calibration (from Calibrate_LaserHead/ 2026-04-24 run07)
# ---------------------------------------------------------------------------
DEFAULT_LASER_K_MV_PER_UM = -0.1171     # signed slope; negative because HAT
                                        # divider + inverted IL-030 polarity
DEFAULT_LASER_V0_MV = 566.957           # intercept at x=0 (ADC voltage in mV)


# ---------------------------------------------------------------------------
# CSV loaders
# ---------------------------------------------------------------------------
@dataclass
class LcrRow:
    host_timestamp_s: float
    monotonic_s: float
    primary: float          # Ls in H (LSRS mode)
    secondary: float        # Rs in ohm (LSRS mode)
    status: int


@dataclass
class LaserRow:
    host_timestamp_s: float
    monotonic_s: float
    firmware_timestamp_us: int
    voltage_V: float
    raw_code: Optional[int]


def load_lcr_csv(path: Path) -> List[LcrRow]:
    out: List[LcrRow] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            out.append(LcrRow(
                host_timestamp_s=float(row["host_timestamp_s"]),
                monotonic_s=float(row["monotonic_s"]),
                primary=float(row["primary"]),
                secondary=float(row["secondary"]),
                status=int(row["status"]),
            ))
    return out


def load_laser_csv(path: Path) -> List[LaserRow]:
    out: List[LaserRow] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("raw_code", "")
            out.append(LaserRow(
                host_timestamp_s=float(row["host_timestamp_s"]),
                monotonic_s=float(row["monotonic_s"]),
                firmware_timestamp_us=int(row["firmware_timestamp_us"]),
                voltage_V=float(row["voltage_V"]),
                raw_code=int(raw) if raw else None,
            ))
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
@dataclass
class ShortCalibration:
    rs_mean_ohm: float
    ls_mean_H: float
    n_samples: int
    rs_std_ohm: float
    ls_std_H: float


def compute_short(short_rows: List[LcrRow]) -> ShortCalibration:
    if not short_rows:
        raise ValueError("Short CSV has no rows")
    rs = np.array([r.secondary for r in short_rows], dtype=float)
    ls = np.array([r.primary for r in short_rows], dtype=float)
    return ShortCalibration(
        rs_mean_ohm=float(rs.mean()),
        ls_mean_H=float(ls.mean()),
        n_samples=len(short_rows),
        rs_std_ohm=float(rs.std(ddof=0)),
        ls_std_H=float(ls.std(ddof=0)),
    )


@dataclass
class DembededSample:
    host_timestamp_s: float
    monotonic_s: float
    rs_raw_ohm: float
    ls_raw_H: float
    rs_dut_ohm: float
    ls_dut_H: float
    q_dut: float
    phase_deg: float
    status: int


def deembed(run_rows: List[LcrRow], short: ShortCalibration,
            omega_rad_s: float) -> List[DembededSample]:
    """
    Z_DUT = Z_raw − Z_short  (short-only)
    """
    out: List[DembededSample] = []
    z_short = complex(short.rs_mean_ohm, omega_rad_s * short.ls_mean_H)
    for r in run_rows:
        z_raw = complex(r.secondary, omega_rad_s * r.primary)
        z_dut = z_raw - z_short
        rs_dut = z_dut.real
        ls_dut = z_dut.imag / omega_rad_s
        # Q and phase — guard against divide-by-zero when Rs_DUT ~ 0
        if abs(rs_dut) > 1e-12:
            q = abs(omega_rad_s * ls_dut / rs_dut)
        else:
            q = float("nan")
        phase = math.degrees(math.atan2(z_dut.imag, z_dut.real))
        out.append(DembededSample(
            host_timestamp_s=r.host_timestamp_s,
            monotonic_s=r.monotonic_s,
            rs_raw_ohm=r.secondary,
            ls_raw_H=r.primary,
            rs_dut_ohm=rs_dut,
            ls_dut_H=ls_dut,
            q_dut=q,
            phase_deg=phase,
            status=r.status,
        ))
    return out


# ---------------------------------------------------------------------------
# Laser interpolation + calibration → displacement
# ---------------------------------------------------------------------------
def interpolate_displacement(
        lcr_timestamps: np.ndarray,
        laser_rows: List[LaserRow],
        k_mV_per_um: float,
        v0_mV: float) -> np.ndarray:
    """
    Linear-interpolate laser voltage onto LCR sample times, then convert to
    displacement in mm using (V_mV − V0) / k.
    Returns array of displacement_mm, same length as lcr_timestamps.
    NaN for LCR timestamps outside the laser coverage window.
    """
    if not laser_rows:
        return np.full_like(lcr_timestamps, np.nan, dtype=float)

    laser_t = np.array([r.host_timestamp_s for r in laser_rows], dtype=float)
    laser_v = np.array([r.voltage_V for r in laser_rows], dtype=float)

    # Sort by timestamp in case the CSV writer interleaved with flushes
    order = np.argsort(laser_t)
    laser_t = laser_t[order]
    laser_v = laser_v[order]

    # np.interp returns endpoints for out-of-range; we NaN those instead.
    v_interp_V = np.interp(lcr_timestamps, laser_t, laser_v,
                           left=np.nan, right=np.nan)
    v_interp_mV = v_interp_V * 1000.0
    # displacement in µm, then convert to mm
    disp_um = (v_interp_mV - v0_mV) / k_mV_per_um
    return disp_um * 1e-3


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_processed_csv(out_path: Path,
                        samples: List[DembededSample],
                        displacement_mm: Optional[np.ndarray]) -> None:
    has_disp = displacement_mm is not None
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        header = [
            "host_timestamp_s", "monotonic_s",
            "rs_raw_ohm", "ls_raw_H",
            "rs_dut_ohm", "ls_dut_H", "q_dut", "phase_deg",
            "status",
        ]
        if has_disp:
            header.append("displacement_mm")
        w.writerow(header)
        for i, s in enumerate(samples):
            row = [
                f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                f"{s.rs_raw_ohm:.8f}", f"{s.ls_raw_H:.8e}",
                f"{s.rs_dut_ohm:.8f}", f"{s.ls_dut_H:.8e}",
                f"{s.q_dut:.6f}", f"{s.phase_deg:.4f}",
                s.status,
            ]
            if has_disp:
                row.append(f"{displacement_mm[i]:.6f}")
            w.writerow(row)


def plot_summary(out_path: Path, samples: List[DembededSample],
                 displacement_mm: Optional[np.ndarray],
                 frequency_hz: float) -> None:
    if not _HAS_MPL:
        logging.warning("matplotlib not available — skipping plot")
        return
    t = np.array([s.host_timestamp_s for s in samples], dtype=float)
    t -= t[0]
    rs = np.array([s.rs_dut_ohm for s in samples])
    ls = np.array([s.ls_dut_H * 1e9 for s in samples])  # to nH
    phi = np.array([s.phase_deg for s in samples])

    n_panels = 4 if displacement_mm is not None else 3
    fig, axes = plt.subplots(n_panels, 1, figsize=(9, 2.2 * n_panels),
                             sharex=True)
    axes[0].plot(t, ls, ".-", markersize=3, linewidth=0.6)
    axes[0].set_ylabel("Ls_DUT (nH)")
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(t, rs, ".-", markersize=3, linewidth=0.6, color="C1")
    axes[1].set_ylabel("Rs_DUT (Ω)")
    axes[1].grid(True, alpha=0.3)
    axes[2].plot(t, phi, ".-", markersize=3, linewidth=0.6, color="C2")
    axes[2].set_ylabel("phase (deg)")
    axes[2].grid(True, alpha=0.3)
    if displacement_mm is not None:
        axes[3].plot(t, displacement_mm, ".-", markersize=3, linewidth=0.6,
                     color="C3")
        axes[3].set_ylabel("disp (mm)")
        axes[3].grid(True, alpha=0.3)
    axes[-1].set_xlabel("time (s, from run start)")
    fig.suptitle(f"SMA de-embedded (short only) @ {frequency_hz/1e6:.3f} MHz",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--short", required=True, type=Path,
                   help="path to short-circuit _lcr_raw.csv")
    p.add_argument("--run", required=True, type=Path,
                   help="path to experiment _lcr_raw.csv")
    p.add_argument("--laser", type=Path, default=None,
                   help="optional _laser_raw.csv for displacement axis")
    p.add_argument("--frequency", type=float, default=1e6,
                   help="LCR measurement frequency in Hz (default 1e6)")
    p.add_argument("--k", type=float, default=DEFAULT_LASER_K_MV_PER_UM,
                   help=f"laser calibration slope mV/µm "
                        f"(default {DEFAULT_LASER_K_MV_PER_UM})")
    p.add_argument("--v0", type=float, default=DEFAULT_LASER_V0_MV,
                   help=f"laser calibration intercept mV "
                        f"(default {DEFAULT_LASER_V0_MV})")
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("analyze_sma")

    # Load
    short_rows = load_lcr_csv(args.short)
    run_rows = load_lcr_csv(args.run)
    log.info("Loaded %d short rows, %d run rows", len(short_rows), len(run_rows))

    # Short calibration
    short = compute_short(short_rows)
    omega = 2.0 * math.pi * args.frequency
    log.info("Short calibration: Rs=%.4f Ω  Ls=%.4e H  "
             "(σ_Rs=%.4f Ω, σ_Ls=%.2e H, n=%d)",
             short.rs_mean_ohm, short.ls_mean_H,
             short.rs_std_ohm, short.ls_std_H, short.n_samples)
    log.info("Z_short at ω = 2π · %.3f MHz: (%.4f + j·%.4f) Ω",
             args.frequency / 1e6,
             short.rs_mean_ohm, omega * short.ls_mean_H)

    # De-embed
    samples = deembed(run_rows, short, omega)

    # Laser interpolation (optional)
    disp = None
    if args.laser is not None:
        laser_rows = load_laser_csv(args.laser)
        log.info("Loaded %d laser samples", len(laser_rows))
        lcr_t = np.array([s.host_timestamp_s for s in samples])
        disp = interpolate_displacement(lcr_t, laser_rows, args.k, args.v0)
        n_valid = int(np.sum(~np.isnan(disp)))
        log.info("Laser coverage: %d / %d LCR samples have valid displacement",
                 n_valid, len(samples))
        log.info("Using calibration: k=%.4f mV/µm, V0=%.3f mV",
                 args.k, args.v0)

    # Output paths — drop "_lcr_raw" suffix and tack on "_processed"
    run_stem = args.run.stem
    if run_stem.endswith("_lcr_raw"):
        out_stem = run_stem[:-len("_lcr_raw")] + "_processed"
    else:
        out_stem = run_stem + "_processed"
    out_csv = args.run.with_name(out_stem + ".csv")
    out_png = args.run.with_name(out_stem + ".png")

    write_processed_csv(out_csv, samples, disp)
    log.info("Wrote %s", out_csv)

    if not args.no_plot:
        plot_summary(out_png, samples, disp, args.frequency)
        log.info("Wrote %s", out_png)

    # Summary numbers
    rs_dut = np.array([s.rs_dut_ohm for s in samples])
    ls_dut = np.array([s.ls_dut_H for s in samples])
    print()
    print(f"Summary ({len(samples)} samples, f = {args.frequency/1e6:.3f} MHz):")
    print(f"  Rs_DUT: mean={rs_dut.mean():.4f} Ω  σ={rs_dut.std():.4f} Ω  "
          f"range=[{rs_dut.min():.4f}, {rs_dut.max():.4f}]")
    print(f"  Ls_DUT: mean={ls_dut.mean()*1e9:.3f} nH  "
          f"σ={ls_dut.std()*1e9:.3f} nH  "
          f"range=[{ls_dut.min()*1e9:.3f}, {ls_dut.max()*1e9:.3f}] nH")
    if disp is not None:
        valid = ~np.isnan(disp)
        if valid.any():
            d = disp[valid]
            print(f"  disp   : range=[{d.min():.3f}, {d.max():.3f}] mm  "
                  f"span={d.max() - d.min():.3f} mm")


if __name__ == "__main__":
    _main()
