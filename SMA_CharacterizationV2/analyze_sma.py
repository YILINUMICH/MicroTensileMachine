#!/usr/bin/env python3
"""
analyze_sma.py — offline de-embedding + LCR/H7 join for SMA sessions.

Two invocation modes:

  Session mode (preferred — for sessions recorded by sma_recorder.py):
      python analyze_sma.py --session data/sma_20260427_153000

      Auto-resolves the per-phase CSVs and meta.json from the session
      directory. Frequency and laser calibration constants are pulled
      from meta.json unless overridden on the command line.

  Legacy mode (for older / hand-assembled file sets):
      python analyze_sma.py --short SHORT.csv --run RAW.csv [--open OPEN.csv] \\
                            [--laser H7.csv] [--frequency 1e6]

De-embedding (auto-selected based on data available):

  OPEN + SHORT (2-term):
      Y_open  = mean over OPEN samples of  1 / (R + jωL)
      Z_short = mean(R_short) + jω·mean(L_short)
      Z_meas  = R_raw + jω·L_raw
      Y_dut   = 1 / (Z_meas − Z_short) − Y_open
      Z_dut   = 1 / Y_dut

  SHORT only (fallback when no OPEN data is present):
      Z_dut   = Z_meas − Z_short

  Q     = |Im(Z_dut) / Re(Z_dut)|
  phase = atan2(Im(Z_dut), Re(Z_dut))

If a laser/H7 CSV is supplied, displacement is interpolated onto each
de-embedded LCR sample's host_timestamp_s and converted via
displacement_um = (V_mV − V0_mV) / k_mV_per_um.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

_HAS_MPL = True
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    _HAS_MPL = False


# ---------------------------------------------------------------------------
# Defaults (from Calibrate_LaserHead/ 2026-04-24 run07)
# ---------------------------------------------------------------------------
DEFAULT_LASER_K_MV_PER_UM = -0.1171
DEFAULT_LASER_V0_MV = 566.957
DEFAULT_FREQUENCY_HZ = 1.0e6


# ---------------------------------------------------------------------------
# CSV row types
# ---------------------------------------------------------------------------
@dataclass
class LcrRow:
    host_timestamp_s: float
    monotonic_s: float
    primary: float          # Ls in H (LSRS mode)
    secondary: float        # Rs in Ω (LSRS mode)
    status: int


@dataclass
class H7Row:
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


def load_h7_csv(path: Path) -> List[H7Row]:
    out: List[H7Row] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("raw_code", "")
            out.append(H7Row(
                host_timestamp_s=float(row["host_timestamp_s"]),
                monotonic_s=float(row["monotonic_s"]),
                firmware_timestamp_us=int(row["firmware_timestamp_us"]),
                voltage_V=float(row["voltage_V"]),
                raw_code=int(raw) if raw not in ("", None) else None,
            ))
    return out


# ---------------------------------------------------------------------------
# Calibrations
# ---------------------------------------------------------------------------
@dataclass
class ShortCalibration:
    rs_mean_ohm: float
    ls_mean_H: float
    rs_std_ohm: float
    ls_std_H: float
    n_samples: int


@dataclass
class OpenCalibration:
    """Mean parallel admittance Y = G + jB, derived from 1/(R + jωL)."""
    g_mean_S: float
    b_mean_S: float
    g_std_S: float
    b_std_S: float
    n_samples: int

    @property
    def y_mean(self) -> complex:
        return complex(self.g_mean_S, self.b_mean_S)


@dataclass
class Calibration:
    short: ShortCalibration
    open_: Optional[OpenCalibration]      # None → short-only de-embed


def compute_short(short_rows: List[LcrRow]) -> ShortCalibration:
    if not short_rows:
        raise ValueError("SHORT CSV has no rows")
    rs = np.array([r.secondary for r in short_rows], dtype=float)
    ls = np.array([r.primary for r in short_rows], dtype=float)
    return ShortCalibration(
        rs_mean_ohm=float(rs.mean()),
        ls_mean_H=float(ls.mean()),
        rs_std_ohm=float(rs.std(ddof=0)),
        ls_std_H=float(ls.std(ddof=0)),
        n_samples=len(short_rows),
    )


def compute_open(open_rows: List[LcrRow], omega: float) -> OpenCalibration:
    """Average admittance Y_open = 1/Z_open across the OPEN run."""
    if not open_rows:
        raise ValueError("OPEN CSV has no rows")
    g_list: List[float] = []
    b_list: List[float] = []
    for r in open_rows:
        z = complex(r.secondary, omega * r.primary)
        if abs(z) > 1e-15:
            y = 1.0 / z
            g_list.append(y.real)
            b_list.append(y.imag)
    if not g_list:
        raise ValueError("OPEN samples had |Z|≈0 — cannot invert")
    g = np.array(g_list); b = np.array(b_list)
    return OpenCalibration(
        g_mean_S=float(g.mean()),
        b_mean_S=float(b.mean()),
        g_std_S=float(g.std(ddof=0)),
        b_std_S=float(b.std(ddof=0)),
        n_samples=len(g_list),
    )


# ---------------------------------------------------------------------------
# De-embedding
# ---------------------------------------------------------------------------
@dataclass
class DeembeddedSample:
    host_timestamp_s: float
    monotonic_s: float
    rs_raw_ohm: float
    ls_raw_H: float
    rs_dut_ohm: float
    ls_dut_H: float
    q_dut: float
    phase_deg: float
    status: int


_NAN = float("nan")


def deembed(run_rows: List[LcrRow],
            cal: Calibration,
            omega: float) -> List[DeembeddedSample]:
    """Apply 2-term (open+short) or short-only de-embedding."""
    z_short = complex(cal.short.rs_mean_ohm, omega * cal.short.ls_mean_H)
    has_open = cal.open_ is not None
    y_open = cal.open_.y_mean if has_open else complex(0.0, 0.0)

    out: List[DeembeddedSample] = []
    for r in run_rows:
        z_meas = complex(r.secondary, omega * r.primary)
        z_minus_short = z_meas - z_short
        if has_open:
            if abs(z_minus_short) < 1e-15:
                z_dut = complex(_NAN, _NAN)
            else:
                y_inter = 1.0 / z_minus_short - y_open
                if abs(y_inter) < 1e-15:
                    z_dut = complex(_NAN, _NAN)
                else:
                    z_dut = 1.0 / y_inter
        else:
            z_dut = z_minus_short

        rs_dut = z_dut.real
        ls_dut = z_dut.imag / omega
        if math.isnan(rs_dut) or abs(rs_dut) < 1e-12:
            q = _NAN
        else:
            q = abs(omega * ls_dut / rs_dut)
        if math.isnan(z_dut.real) or math.isnan(z_dut.imag):
            phase = _NAN
        else:
            phase = math.degrees(math.atan2(z_dut.imag, z_dut.real))

        out.append(DeembeddedSample(
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
# Laser interpolation
# ---------------------------------------------------------------------------
def interpolate_displacement(lcr_t: np.ndarray,
                             h7_rows: List[H7Row],
                             k_mV_per_um: float,
                             v0_mV: float) -> np.ndarray:
    """
    Linear-interpolate H7/laser voltage onto LCR sample times, then
    convert to mm. NaN where lcr_t is outside the laser coverage window.
    """
    if not h7_rows:
        return np.full_like(lcr_t, np.nan, dtype=float)
    t = np.array([r.host_timestamp_s for r in h7_rows], dtype=float)
    v = np.array([r.voltage_V for r in h7_rows], dtype=float)
    order = np.argsort(t)
    t = t[order]; v = v[order]
    v_interp_V = np.interp(lcr_t, t, v, left=np.nan, right=np.nan)
    v_interp_mV = v_interp_V * 1000.0
    disp_um = (v_interp_mV - v0_mV) / k_mV_per_um
    return disp_um * 1e-3


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_processed_csv(out_path: Path,
                        samples: List[DeembeddedSample],
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


def plot_summary(out_path: Path,
                 samples: List[DeembeddedSample],
                 displacement_mm: Optional[np.ndarray],
                 frequency_hz: float,
                 deembed_mode: str) -> None:
    if not _HAS_MPL:
        logging.warning("matplotlib not available — skipping plot")
        return
    t = np.array([s.host_timestamp_s for s in samples], dtype=float)
    if t.size == 0:
        logging.warning("No samples to plot")
        return
    t -= t[0]
    rs = np.array([s.rs_dut_ohm for s in samples])
    ls_nh = np.array([s.ls_dut_H * 1e9 for s in samples])
    phi = np.array([s.phase_deg for s in samples])

    n_panels = 4 if displacement_mm is not None else 3
    fig, axes = plt.subplots(n_panels, 1, figsize=(9, 2.2 * n_panels),
                             sharex=True)
    axes[0].plot(t, ls_nh, ".-", markersize=3, linewidth=0.6)
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

    axes[-1].set_xlabel("time (s, from RAW start)")
    fig.suptitle(
        f"SMA de-embedded ({deembed_mode}) @ {frequency_hz/1e6:.3f} MHz",
        fontsize=11)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI / mode resolution
# ---------------------------------------------------------------------------
@dataclass
class ResolvedInputs:
    short_csv: Path
    run_csv: Path
    open_csv: Optional[Path]
    h7_csv: Optional[Path]
    out_csv: Path
    out_png: Path
    frequency_hz: float
    k_mV_per_um: float
    v0_mV: float
    mode_label: str          # "session" | "legacy"


def _resolve_session(session_dir: Path,
                     args: argparse.Namespace) -> ResolvedInputs:
    if not session_dir.is_dir():
        raise SystemExit(f"--session: not a directory: {session_dir}")
    short_csv = session_dir / "short_lcr.csv"
    run_csv = session_dir / "raw_lcr.csv"
    open_csv: Optional[Path] = session_dir / "open_lcr.csv"
    h7_csv: Optional[Path] = session_dir / "raw_h7.csv"
    if open_csv is not None and not open_csv.exists():
        open_csv = None
    if h7_csv is not None and not h7_csv.exists():
        h7_csv = None
    if not short_csv.exists():
        raise SystemExit(f"--session: missing {short_csv}")
    if not run_csv.exists():
        raise SystemExit(f"--session: missing {run_csv}")

    # Pull frequency + laser calibration from meta.json if present.
    meta_path = session_dir / "meta.json"
    freq = args.frequency
    k = args.k
    v0 = args.v0
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if freq is None:
                freq = float(meta.get("lcr", {}).get("frequency_hz",
                                                     DEFAULT_FREQUENCY_HZ))
            laser_meta = meta.get("laser_calibration_reference", {})
            if k == DEFAULT_LASER_K_MV_PER_UM:
                k = float(laser_meta.get("k_mV_per_um", k))
            if v0 == DEFAULT_LASER_V0_MV:
                v0 = float(laser_meta.get("V0_mV", v0))
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logging.warning("Could not parse meta.json (%s); using defaults", e)
    if freq is None:
        freq = DEFAULT_FREQUENCY_HZ

    return ResolvedInputs(
        short_csv=short_csv, run_csv=run_csv,
        open_csv=open_csv, h7_csv=h7_csv,
        out_csv=session_dir / "processed.csv",
        out_png=session_dir / "processed.png",
        frequency_hz=freq, k_mV_per_um=k, v0_mV=v0,
        mode_label="session",
    )


def _resolve_legacy(args: argparse.Namespace) -> ResolvedInputs:
    if args.short is None or args.run is None:
        raise SystemExit("Legacy mode: --short and --run are required "
                         "(or use --session for a session directory)")
    run_stem = args.run.stem
    if run_stem.endswith("_lcr_raw"):
        out_stem = run_stem[: -len("_lcr_raw")] + "_processed"
    elif run_stem.endswith("_lcr"):
        out_stem = run_stem[: -len("_lcr")] + "_processed"
    else:
        out_stem = run_stem + "_processed"
    freq = args.frequency if args.frequency is not None else DEFAULT_FREQUENCY_HZ
    return ResolvedInputs(
        short_csv=args.short, run_csv=args.run,
        open_csv=args.open_csv, h7_csv=args.laser,
        out_csv=args.run.with_name(out_stem + ".csv"),
        out_png=args.run.with_name(out_stem + ".png"),
        frequency_hz=freq, k_mV_per_um=args.k, v0_mV=args.v0,
        mode_label="legacy",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--session", type=Path, default=None,
                     help="path to a session directory (preferred mode)")
    src.add_argument("--run", type=Path, default=None,
                     help="(legacy) path to experiment LCR CSV")
    p.add_argument("--short", type=Path, default=None,
                   help="(legacy) path to SHORT LCR CSV")
    p.add_argument("--open", dest="open_csv", type=Path, default=None,
                   help="(legacy) optional path to OPEN LCR CSV")
    p.add_argument("--laser", type=Path, default=None,
                   help="(legacy) optional H7/laser CSV for displacement axis")
    p.add_argument("--frequency", type=float, default=None,
                   help=f"LCR measurement frequency in Hz "
                        f"(default: read from meta.json or {DEFAULT_FREQUENCY_HZ})")
    p.add_argument("--k", type=float, default=DEFAULT_LASER_K_MV_PER_UM,
                   help=f"laser calibration slope mV/µm "
                        f"(default {DEFAULT_LASER_K_MV_PER_UM})")
    p.add_argument("--v0", type=float, default=DEFAULT_LASER_V0_MV,
                   help=f"laser calibration intercept mV "
                        f"(default {DEFAULT_LASER_V0_MV})")
    p.add_argument("--deembed", choices=["auto", "short_only", "open_short"],
                   default="auto",
                   help="auto: open_short if OPEN data present, else short_only")
    p.add_argument("--no-plot", action="store_true")
    return p


def _main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("analyze_sma")

    # Resolve inputs based on mode
    if args.session is not None:
        inp = _resolve_session(args.session, args)
    else:
        inp = _resolve_legacy(args)

    log.info("Mode: %s", inp.mode_label)
    log.info("  SHORT: %s", inp.short_csv)
    if inp.open_csv:
        log.info("  OPEN : %s", inp.open_csv)
    else:
        log.info("  OPEN : (none — short-only de-embed)")
    log.info("  RAW  : %s", inp.run_csv)
    if inp.h7_csv:
        log.info("  H7   : %s", inp.h7_csv)
    log.info("  f    : %.6g Hz", inp.frequency_hz)

    # Load LCR data
    short_rows = load_lcr_csv(inp.short_csv)
    run_rows = load_lcr_csv(inp.run_csv)
    log.info("Loaded %d SHORT, %d RAW LCR rows",
             len(short_rows), len(run_rows))

    # SHORT calibration
    short_cal = compute_short(short_rows)
    omega = 2.0 * math.pi * inp.frequency_hz
    log.info("SHORT cal: Rs=%.4f Ω, Ls=%.4e H  "
             "(σ_Rs=%.4f, σ_Ls=%.2e, n=%d)",
             short_cal.rs_mean_ohm, short_cal.ls_mean_H,
             short_cal.rs_std_ohm, short_cal.ls_std_H,
             short_cal.n_samples)

    # OPEN calibration (auto / forced)
    open_cal: Optional[OpenCalibration] = None
    deembed_mode = "short_only"
    want_open = (args.deembed in ("auto", "open_short"))
    if want_open and inp.open_csv is not None:
        open_rows = load_lcr_csv(inp.open_csv)
        log.info("Loaded %d OPEN LCR rows", len(open_rows))
        open_cal = compute_open(open_rows, omega)
        deembed_mode = "open_short"
        log.info("OPEN  cal: G=%.4e S, B=%.4e S  "
                 "(σ_G=%.2e, σ_B=%.2e, n=%d)",
                 open_cal.g_mean_S, open_cal.b_mean_S,
                 open_cal.g_std_S, open_cal.b_std_S,
                 open_cal.n_samples)
    elif args.deembed == "open_short" and inp.open_csv is None:
        log.warning("--deembed=open_short requested but no OPEN data found; "
                    "falling back to short_only.")

    # De-embed
    cal = Calibration(short=short_cal, open_=open_cal)
    samples = deembed(run_rows, cal, omega)
    log.info("De-embedded %d samples (mode=%s)", len(samples), deembed_mode)

    # H7 → displacement
    disp = None
    if inp.h7_csv is not None:
        h7_rows = load_h7_csv(inp.h7_csv)
        lcr_t = np.array([s.host_timestamp_s for s in samples])
        disp = interpolate_displacement(lcr_t, h7_rows,
                                         inp.k_mV_per_um, inp.v0_mV)
        n_valid = int(np.sum(~np.isnan(disp)))
        log.info("H7: %d samples, %d / %d LCR rows have valid displacement",
                 len(h7_rows), n_valid, len(samples))
        log.info("Laser cal: k=%.4f mV/µm, V0=%.3f mV",
                 inp.k_mV_per_um, inp.v0_mV)

    # Write outputs
    write_processed_csv(inp.out_csv, samples, disp)
    log.info("Wrote %s", inp.out_csv)
    if not args.no_plot:
        plot_summary(inp.out_png, samples, disp,
                     inp.frequency_hz, deembed_mode)
        log.info("Wrote %s", inp.out_png)

    # Print summary
    rs_dut = np.array([s.rs_dut_ohm for s in samples])
    ls_dut = np.array([s.ls_dut_H for s in samples])
    rs_dut = rs_dut[~np.isnan(rs_dut)]
    ls_dut = ls_dut[~np.isnan(ls_dut)]
    print()
    print(f"Summary ({len(samples)} samples, "
          f"f = {inp.frequency_hz/1e6:.3f} MHz, mode = {deembed_mode}):")
    if rs_dut.size:
        print(f"  Rs_DUT: mean={rs_dut.mean():.4f} Ω  "
              f"σ={rs_dut.std():.4f} Ω  "
              f"range=[{rs_dut.min():.4f}, {rs_dut.max():.4f}] Ω")
    if ls_dut.size:
        print(f"  Ls_DUT: mean={ls_dut.mean()*1e9:.3f} nH  "
              f"σ={ls_dut.std()*1e9:.3f} nH  "
              f"range=[{ls_dut.min()*1e9:.3f}, {ls_dut.max()*1e9:.3f}] nH")
    if disp is not None:
        valid = ~np.isnan(disp)
        if valid.any():
            d = disp[valid]
            print(f"  disp  : range=[{d.min():.3f}, {d.max():.3f}] mm  "
                  f"span={d.max() - d.min():.3f} mm")


if __name__ == "__main__":
    _main()
