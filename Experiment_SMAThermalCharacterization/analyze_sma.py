#!/usr/bin/env python3
"""
analyze_sma.py — offline analysis + visualization for a V3 SMA session.

Reads a session directory produced by sma_recorder.py (per-phase CSVs +
meta.json), applies the calibration coefficients from meta.json (the
RECORDER logs raw data only — all unit conversion happens here), joins the
streams on the host clock, and renders a multi-panel PNG dashboard plus a
joined CSV.

Dashboard panels: displacement, force, SMA R/V/I, de-embedded LCR R/L, and SMA
electrical power (P = V.I) vs time. All values are plotted RAW - no filtering.

Usage:
    python analyze_sma.py --session data/console_20260621_153000
    python analyze_sma.py --session <dir> --phase raw
    python analyze_sma.py --session <dir> --k -0.1171 --v0 566.957 \\
                          --load-scale 50.0 --load-offset 0.0

Streams (per phase CSV):
    *_lcr.csv    host_ts, monotonic, primary(Ls,H), secondary(Rs,Ω), status
    *_h7.csv     host_ts, monotonic, fw_us, src, channel, value, raw, hw_us, seq
                   channel ∈ {laser, load, sma_v, sma_i, sma_r}
                   value: V for laser/load/sma_v, A for sma_i, Ω for sma_r
    *_stage.csv  host_ts, monotonic, position_mm

De-embedding (LCR, auto-selected from available phases):
    OPEN+SHORT (2-term):
        Y_open  = mean(1/(R+jωL))            over OPEN
        Z_short = mean(R)+jω·mean(L)         over SHORT
        Z_meas  = R_raw + jω·L_raw           on RAW
        Z_dut   = 1/(1/(Z_meas−Z_short) − Y_open)
    SHORT-only:  Z_dut = Z_meas − Z_short
    Q = |Im/Re|,  phase = atan2(Im, Re)

Conversions (skipped, left raw, if the coefficient is null):
    displacement_um = (value·1000 − V0_mV) / k_mV_per_um      [laser]
    force_N         = scale_N_per_V · (value − offset_V)      [load cell]

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
from typing import Dict, List, Optional

import numpy as np

_HAS_MPL = True
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    _HAS_MPL = False

_HAS_CV2 = True
try:
    import cv2
except ImportError:
    _HAS_CV2 = False

log = logging.getLogger("analyze_sma")

H7_CHANNELS = ("laser", "load", "sma_v", "sma_i", "sma_r")


# ---------------------------------------------------------------------------
# CSV loaders
# ---------------------------------------------------------------------------
@dataclass
class LcrArrays:
    t: np.ndarray          # host_timestamp_s
    Ls: np.ndarray         # primary (H)
    Rs: np.ndarray         # secondary (Ω)
    status: np.ndarray


def _read_csv_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_lcr(path: Path) -> Optional[LcrArrays]:
    rows = _read_csv_rows(path)
    if not rows:
        return None
    t, Ls, Rs, st = [], [], [], []
    for r in rows:
        try:
            t.append(float(r["host_timestamp_s"]))
            Ls.append(float(r["primary"]))
            Rs.append(float(r["secondary"]))
            st.append(int(float(r.get("status", 0) or 0)))
        except (ValueError, KeyError):
            continue
    if not t:
        return None
    return LcrArrays(np.array(t), np.array(Ls), np.array(Rs), np.array(st))


def load_h7(path: Path) -> Dict[str, dict]:
    """Return {channel: {'t': array, 'value': array, 'raw': array}}."""
    rows = _read_csv_rows(path)
    out: Dict[str, dict] = {ch: {"t": [], "value": [], "raw": []} for ch in H7_CHANNELS}
    for r in rows:
        ch = (r.get("channel") or "").strip()
        if ch not in out:
            continue
        try:
            out[ch]["t"].append(float(r["host_timestamp_s"]))
            out[ch]["value"].append(float(r["value"]))
            raw = r.get("raw_code", "")
            out[ch]["raw"].append(float(raw) if raw not in ("", None) else math.nan)
        except (ValueError, KeyError):
            continue
    # numpy-ify, drop empty channels
    res: Dict[str, dict] = {}
    for ch, d in out.items():
        if d["t"]:
            res[ch] = {k: np.array(v) for k, v in d.items()}
    return res


def load_stage(path: Path) -> Optional[dict]:
    rows = _read_csv_rows(path)
    if not rows:
        return None
    t, pos = [], []
    for r in rows:
        try:
            t.append(float(r["host_timestamp_s"]))
            pos.append(float(r["position_mm"]))
        except (ValueError, KeyError):
            continue
    if not t:
        return None
    return {"t": np.array(t), "position_mm": np.array(pos)}


# ---------------------------------------------------------------------------
# events.csv — segmentation markers (console layout)
# ---------------------------------------------------------------------------
def load_events(path: Path) -> List[dict]:
    """Return events.csv rows as dicts with float host_timestamp_s/monotonic_s
    and str kind/detail. Empty list if the file is absent."""
    rows = _read_csv_rows(path)
    out: List[dict] = []
    for r in rows:
        try:
            out.append({
                "host_timestamp_s": float(r["host_timestamp_s"]),
                "monotonic_s": float(r.get("monotonic_s", "nan") or "nan"),
                "kind": (r.get("kind") or "").strip(),
                "detail": (r.get("detail") or "").strip(),
            })
        except (ValueError, KeyError):
            continue
    return out


def ref_windows(events: List[dict], kind: str,
                window_s: float) -> List[tuple]:
    """Build (t_start, t_end) host-clock windows for each marker of `kind`.

    Each marker opens a window of `window_s` seconds, but a window is cut short
    by the NEXT event of ANY kind so two back-to-back references never overlap.
    """
    marks = sorted(e["host_timestamp_s"] for e in events if e["kind"] == kind)
    all_ts = sorted(e["host_timestamp_s"] for e in events)
    windows: List[tuple] = []
    for t in marks:
        t_end = t + window_s
        nxt = [s for s in all_ts if s > t]
        if nxt:
            t_end = min(t_end, nxt[0])
        windows.append((t, t_end))
    return windows


def slice_lcr(arr: Optional[LcrArrays], windows: List[tuple],
              inside: bool) -> Optional[LcrArrays]:
    """Restrict an LcrArrays to (inside=True) or exclude (inside=False) a set of
    time windows. Returns None if nothing is left (or the input was None)."""
    if arr is None:
        return None
    if not windows:
        # No windows: "inside" selects nothing; "outside" keeps everything.
        return None if inside else arr
    mask = np.zeros(arr.t.shape, dtype=bool)
    for (t0, t1) in windows:
        mask |= (arr.t >= t0) & (arr.t < t1)
    if not inside:
        mask = ~mask
    if not mask.any():
        return None
    return LcrArrays(arr.t[mask], arr.Ls[mask], arr.Rs[mask], arr.status[mask])


# ---------------------------------------------------------------------------
# LCR de-embedding
# ---------------------------------------------------------------------------
@dataclass
class DeembedResult:
    method: str                 # "open_short" | "short_only" | "none"
    t: np.ndarray
    R_dut: np.ndarray
    X_dut: np.ndarray           # reactance Im(Z)
    L_dut: np.ndarray           # series inductance (H), X/ω
    Q: np.ndarray
    phase_rad: np.ndarray


def deembed(raw: LcrArrays,
            short: Optional[LcrArrays],
            open_: Optional[LcrArrays],
            freq_hz: float) -> DeembedResult:
    w = 2.0 * math.pi * freq_hz
    Z_meas = raw.Rs + 1j * w * raw.Ls

    if short is not None and open_ is not None:
        Z_short = np.mean(short.Rs) + 1j * w * np.mean(short.Ls)
        Y_open = np.mean(1.0 / (open_.Rs + 1j * w * open_.Ls))
        Y_dut = 1.0 / (Z_meas - Z_short) - Y_open
        Z_dut = 1.0 / Y_dut
        method = "open_short"
    elif short is not None:
        Z_short = np.mean(short.Rs) + 1j * w * np.mean(short.Ls)
        Z_dut = Z_meas - Z_short
        method = "short_only"
    else:
        Z_dut = Z_meas
        method = "none"

    R = np.real(Z_dut)
    X = np.imag(Z_dut)
    with np.errstate(divide="ignore", invalid="ignore"):
        Q = np.abs(X / R)
        L = X / w
    phase = np.arctan2(X, R)
    return DeembedResult(method, raw.t, R, X, L, Q, phase)


# ---------------------------------------------------------------------------
# Calibration conversions
# ---------------------------------------------------------------------------
def laser_to_um(value_V: np.ndarray, k_mV_per_um: Optional[float],
                V0_mV: Optional[float]) -> Optional[np.ndarray]:
    if k_mV_per_um is None or V0_mV is None or k_mV_per_um == 0:
        return None
    return (value_V * 1000.0 - V0_mV) / k_mV_per_um


def load_to_N(value_V: np.ndarray, scale_N_per_V: Optional[float],
              offset_V: float) -> Optional[np.ndarray]:
    if scale_N_per_V is None:
        return None
    return scale_N_per_V * (value_V - offset_V)


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------
def _rel(t: np.ndarray, t0: float) -> np.ndarray:
    return t - t0


_ADC_FS = 8_388_608   # ADS1263 full scale = 2^23 (raw code rails at ±this)


def load_saturation_mask(h7: Dict[str, dict]) -> Optional[np.ndarray]:
    """Boolean mask over the load samples that are pinned at the ADC rail
    (raw code at ±2^23), i.e. the load cell saturated. Falls back to a
    voltage test (|V| ≥ 4.99) if raw codes weren't logged. None if no load."""
    if "load" not in h7:
        return None
    ld = h7["load"]
    raw = ld.get("raw")
    if raw is not None and np.isfinite(raw).any():
        return np.abs(np.nan_to_num(raw, nan=0.0)) >= _ADC_FS - 8
    return np.abs(ld["value"]) >= 4.99


def make_dashboard(out_png: Path, title: str, t0: float,
                   h7: Dict[str, dict],
                   disp_um: Optional[np.ndarray],
                   force_N: Optional[np.ndarray],
                   deemb: Optional[DeembedResult],
                   stage: Optional[dict],
                   baseline: Optional[dict] = None) -> None:
    if not _HAS_MPL:
        log.warning("matplotlib not available — skipping dashboard PNG")
        return

    panels = []
    if "laser" in h7:
        panels.append("disp")
    if "load" in h7:
        panels.append("force")
    if "sma_r" in h7 or "sma_v" in h7 or "sma_i" in h7:
        panels.append("sma")
    if deemb is not None and deemb.method != "none":
        panels.append("lcr")
    # Stage panel dropped — the stage is held fixed for a thermal run, so its
    # trace is flat and uninformative. (position stays in the joined CSV.)
    #
    # Electrical power P = V·I into the coil. This replaced a load-vs-laser
    # voltage scatter: for a THERMAL run the Joule heating actually driving the
    # transformation is the quantity of interest, and the force-vs-displacement
    # cross-plot was near-meaningless anyway (the stage is held, so displacement
    # barely moves and what it does show is drive feedthrough).
    if "sma_v" in h7 and "sma_i" in h7:
        panels.append("power")

    n = len(panels)
    ncol = 2
    nrow = (n + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(13, 3.2 * nrow))
    axes = np.atleast_1d(axes).ravel()
    for ax in axes[n:]:
        ax.axis("off")

    for ax, key in zip(axes, panels):
        if key == "disp":
            # Raw laser voltage straight off the ADC — no sensitivity applied.
            if "laser" in h7:
                ax.plot(_rel(h7["laser"]["t"], t0), h7["laser"]["value"],
                        lw=0.8, color="C0")
                ax.set_ylabel("laser (V)")
            ax.set_title("Laser voltage"); ax.set_xlabel("t (s)")
        elif key == "force":
            # Raw load-cell voltage straight off the ADC — no sensitivity applied.
            yvals = None
            if "load" in h7:
                yvals = h7["load"]["value"]
                ax.plot(_rel(h7["load"]["t"], t0), yvals, lw=0.8, color="C3")
                ax.set_ylabel("load (V)")
            # Flag ADC-rail saturation — those force values are invalid.
            sat = load_saturation_mask(h7)
            if sat is not None and yvals is not None and sat.any():
                tl = _rel(h7["load"]["t"], t0)
                ax.plot(tl[sat], yvals[sat], ".", ms=3, color="red",
                        label="saturated (±5 V rail)")
                frac = 100.0 * int(sat.sum()) / sat.size
                ax.text(0.02, 0.04,
                        f"⚠ {frac:.0f}% saturated — null the LCA-9PC ZERO pot",
                        transform=ax.transAxes, color="red", fontsize=8,
                        va="bottom")
                ax.legend(loc="upper right", fontsize=7)
            ax.set_title("Load voltage"); ax.set_xlabel("t (s)")
        elif key == "sma":
            if "sma_r" in h7:
                ax.plot(_rel(h7["sma_r"]["t"], t0), h7["sma_r"]["value"],
                        lw=0.9, color="C2", label="R (Ω)")
                ax.set_ylabel("R (Ω)")
            ax2 = ax.twinx()
            if "sma_v" in h7:
                ax2.plot(_rel(h7["sma_v"]["t"], t0), h7["sma_v"]["value"],
                         lw=0.7, color="C1", alpha=0.7, label="V (V)")
            if "sma_i" in h7:
                ax2.plot(_rel(h7["sma_i"]["t"], t0), h7["sma_i"]["value"],
                         lw=0.7, color="C4", alpha=0.7, label="I (A)")
            ax2.set_ylabel("V (V) / I (A)")
            # Cold-R reference from the baseline phase (if measured).
            cr = (baseline or {}).get("cold_r_ohm")
            if cr is not None and "sma_r" in h7:
                ax.axhline(cr, ls="--", lw=0.9, color="C2", alpha=0.6)
                ax.text(0.02, 0.95, f"cold R = {cr:.3g} Ω",
                        transform=ax.transAxes, color="C2", fontsize=8,
                        va="top")
            ax.set_title("SMA drive (R / V / I)"); ax.set_xlabel("t (s)")
        elif key == "lcr" and deemb is not None:
            ax.plot(_rel(deemb.t, t0), deemb.R_dut, lw=0.8, color="C5", label="R_dut (Ω)")
            ax2 = ax.twinx()
            ax2.plot(_rel(deemb.t, t0), deemb.L_dut * 1e6, lw=0.8, color="C6",
                     alpha=0.7, label="L_dut (µH)")
            ax.set_ylabel("R_dut (Ω)"); ax2.set_ylabel("L_dut (µH)")
            ax.set_title(f"De-embedded Z ({deemb.method})"); ax.set_xlabel("t (s)")
        elif key == "stage" and stage is not None:
            ax.plot(_rel(stage["t"], t0), stage["position_mm"], lw=0.8, color="C7")
            ax.set_ylabel("position (mm)"); ax.set_title("Stage"); ax.set_xlabel("t (s)")
        elif key == "power":
            # Electrical power into the coil, P = V·I. V and I are already
            # physical (the firmware computes volts and amps), so no calibration
            # is applied — this stays as raw as every other panel.
            # sma_v and sma_i are separate rows of the same stream, so interp I
            # onto V's clock rather than assuming the timestamps line up.
            tv = h7["sma_v"]["t"]; ti = h7["sma_i"]["t"]
            i_on_v = np.interp(tv, ti, h7["sma_i"]["value"])
            p_W = h7["sma_v"]["value"] * i_on_v
            ax.plot(_rel(tv, t0), p_W, lw=0.9, color="C8")
            ax.set_ylabel("P = V·I  (W)")
            ax.set_xlabel("t (s)")
            # Energy is the integral — the quantity that actually heats the wire.
            # Split it by fire vs idle (threshold at half the peak, so no
            # dependence on events.csv): on a run with a long cool the "idle
            # probe" can quietly deliver MORE total heat than all the fires
            # combined, which is worth seeing on the panel.
            _trapz = getattr(np, "trapezoid", None) or np.trapz   # numpy 2 / 1
            if len(tv) > 1 and np.nanmax(p_W) > 0:
                hot = p_W > 0.5 * np.nanmax(p_W)
                e_tot = float(_trapz(p_W, tv))
                e_fire = float(_trapz(np.where(hot, p_W, 0.0), tv))
                e_idle = e_tot - e_fire
                frac = 100.0 * e_idle / e_tot if e_tot else 0.0
                ax.text(0.02, 0.95,
                        f"peak {np.nanmax(p_W):.2f} W   "
                        f"idle {np.nanmedian(p_W[~hot]):.3f} W\n"
                        f"energy {e_tot:.1f} J = {e_fire:.1f} J fires "
                        f"+ {e_idle:.1f} J idle ({frac:.0f}% idle)",
                        transform=ax.transAxes, fontsize=8, va="top", color="0.3")
            ax.set_title("SMA electrical power")

    fig.suptitle(title, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out_png, dpi=130)
    plt.close(fig)
    log.info("Wrote %s", out_png)


# ---------------------------------------------------------------------------
# Annotated per-cycle video (stitches the camera JPEGs into a scrubbable MP4
# with a burned-in overlay of time + synced displacement/force, so a frame is
# self-explanatory without cross-referencing frames.csv).
# ---------------------------------------------------------------------------
def _annotate_frame(img, cyc: int, t_rel: float, mode: str,
                    disp_um: Optional[float], force_N: Optional[float]) -> None:
    """Burn a two-line overlay onto a BGR frame (in place)."""
    h, w = img.shape[:2]
    line1 = f"cycle {cyc}   t=+{t_rel:5.2f}s   {mode}"
    dtxt = f"{disp_um:+.0f} um" if disp_um is not None else "-- um"
    ftxt = f"{force_N:+.3f} N" if force_N is not None else "-- N"
    line2 = f"disp {dtxt}   F {ftxt}"
    bar = max(28, h // 12)
    ov = img.copy()
    cv2.rectangle(ov, (0, 0), (w, bar * 2 + 6), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.45, img, 0.55, 0, img)
    fs = max(0.5, w / 1100.0)
    cv2.putText(img, line1, (10, int(bar * 0.9)), cv2.FONT_HERSHEY_SIMPLEX,
                0.7 * fs, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(img, line2, (10, int(bar * 1.9)), cv2.FONT_HERSHEY_SIMPLEX,
                0.7 * fs, (0, 255, 255), 1, cv2.LINE_AA)


def make_cycle_videos(video_dir: Path, disp_t, disp_um, force_t, force_N,
                      fps: float = 15.0) -> None:
    """Stitch each cycle's JPEGs (from video/frames.csv) into an annotated MP4
    next to them (video/cycle_NN.mp4). Overlay shows cycle-relative time, mode,
    and the displacement/force interpolated onto each frame's host clock."""
    if not _HAS_CV2:
        log.warning("opencv (cv2) not available — skipping cycle videos")
        return
    frames_csv = video_dir / "frames.csv"
    if not frames_csv.exists():
        log.info("no %s — skipping cycle videos", frames_csv.name)
        return
    from collections import defaultdict
    by_cycle: "dict[int, list]" = defaultdict(list)
    with open(frames_csv) as f:
        for row in csv.DictReader(f):
            try:
                by_cycle[int(row["cycle_idx"])].append(row)
            except (KeyError, ValueError):
                continue
    if not by_cycle:
        log.info("frames.csv had no usable rows — skipping cycle videos")
        return

    n_vids = 0
    for cyc in sorted(by_cycle):
        if cyc < 0:
            continue
        rows = sorted(by_cycle[cyc], key=lambda r: float(r["host_timestamp_s"]))
        t_start = float(rows[0]["host_timestamp_s"])
        out_path = video_dir / f"cycle_{cyc:02d}.mp4"
        writer = None
        size = None
        n_frames = 0
        for row in rows:
            img = cv2.imread(str(video_dir / row["rel_path"]))
            if img is None:
                continue
            if size is None:
                size = (img.shape[1], img.shape[0])
                writer = cv2.VideoWriter(
                    str(out_path), cv2.VideoWriter_fourcc(*"mp4v"),
                    float(fps), size)
            elif (img.shape[1], img.shape[0]) != size:
                img = cv2.resize(img, size)
            host = float(row["host_timestamp_s"])
            d = None
            if disp_um is not None and disp_t is not None and len(disp_t):
                d = float(np.interp(host, disp_t, disp_um))
            elif row.get("laser_mm"):
                try:
                    d = float(row["laser_mm"]) * 1000.0
                except ValueError:
                    pass
            fN = None
            if force_N is not None and force_t is not None and len(force_t):
                fN = float(np.interp(host, force_t, force_N))
            _annotate_frame(img, cyc, host - t_start, row.get("mode", ""), d, fN)
            writer.write(img)
            n_frames += 1
        if writer is not None:
            writer.release()
            n_vids += 1
            log.info("Wrote %s (%d frames @ %g fps)", out_path, n_frames, fps)
    if n_vids == 0:
        log.info("no cycle videos written (no readable frames)")


# ---------------------------------------------------------------------------
# Joined CSV (resampled onto a uniform grid)
# ---------------------------------------------------------------------------
def write_joined_csv(out_csv: Path, t0: float, n: int,
                     t_grid: np.ndarray,
                     series: Dict[str, tuple]) -> None:
    """series: name -> (t_array, value_array). Each is interpolated onto t_grid."""
    cols = {"t_rel_s": t_grid - t0}
    for name, (t, v) in series.items():
        if t is None or v is None or len(t) < 2:
            continue
        order = np.argsort(t)
        cols[name] = np.interp(t_grid, t[order], v[order])
    names = list(cols.keys())
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(names)
        for i in range(len(t_grid)):
            w.writerow([f"{cols[name][i]:.8g}" for name in names])
    log.info("Wrote %s (%d rows, cols=%s)", out_csv, len(t_grid), names)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _cfg_get(meta: dict, *keys, default=None):
    d = meta
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def _read_meta_and_cal(session_dir: Path, args):
    meta_path = session_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
    else:
        # No meta.json (session didn't finalize — e.g. crashed). Fall back to the
        # module's config.yaml: it has the same calibration/lcr key structure and
        # the recorder writes meta's calibration straight from it, so the values
        # match a finalized session. Lets orphaned sessions still be analyzed.
        try:
            import yaml
            cfg_path = Path(__file__).resolve().parent / "config.yaml"
            meta = yaml.safe_load(cfg_path.read_text()) or {}
            log.warning("No meta.json in %s — falling back to config.yaml "
                        "calibration", session_dir.name)
        except Exception as e:  # noqa: BLE001
            log.warning("No meta.json in %s and config.yaml fallback failed (%s) "
                        "— using CLI args / raw output", session_dir.name, e)
            meta = {}
    freq = args.frequency or _cfg_get(meta, "lcr", "frequency_hz", default=1.0e6)
    k = args.k if args.k is not None else _cfg_get(meta, "calibration", "laser", "k_mV_per_um")
    v0 = args.v0 if args.v0 is not None else _cfg_get(meta, "calibration", "laser", "V0_mV")
    lscale = (args.load_scale if args.load_scale is not None
              else _cfg_get(meta, "calibration", "load_cell", "scale_N_per_V"))
    loff = (args.load_offset if args.load_offset is not None
            else _cfg_get(meta, "calibration", "load_cell", "offset_V", default=0.0))
    return meta, freq, k, v0, lscale, loff


def _run_analysis(out_dir: Path, label: str, args,
                  raw_lcr: Optional[LcrArrays],
                  short_lcr: Optional[LcrArrays],
                  open_lcr: Optional[LcrArrays],
                  h7: Dict[str, dict],
                  stage: Optional[dict],
                  freq: float, k, v0, lscale, loff,
                  session_dir: Optional[Path] = None,
                  meta: Optional[dict] = None) -> int:
    """Shared downstream: conversions -> de-embed -> dashboard -> joined CSV ->
    summary. Both the per-phase and the continuous-console loaders feed this."""
    if not h7 and raw_lcr is None and stage is None:
        log.error("No data found for '%s'", label)
        return 2

    # Time origin = earliest sample across all streams.
    t0_candidates = []
    if raw_lcr is not None:
        t0_candidates.append(raw_lcr.t.min())
    for ch in h7.values():
        t0_candidates.append(ch["t"].min())
    if stage is not None:
        t0_candidates.append(stage["t"].min())
    t0 = min(t0_candidates) if t0_candidates else 0.0

    # Conversions.
    disp_um = laser_to_um(h7["laser"]["value"], k, v0) if "laser" in h7 else None
    force_N = load_to_N(h7["load"]["value"], lscale, loff) if "load" in h7 else None

    # De-embed.
    deemb = None
    if raw_lcr is not None and not args.no_deembed:
        deemb = deembed(raw_lcr, short_lcr, open_lcr, freq)
        log.info("LCR de-embed method: %s (f=%.3g Hz)", deemb.method, freq)

    baseline = (meta or {}).get("baseline")
    out_dir.mkdir(parents=True, exist_ok=True)
    title = f"{out_dir.name} — {label}"
    make_dashboard(out_dir / f"{label}_dashboard.png", title, t0,
                   h7, disp_um, force_N, deemb, stage, baseline=baseline)

    # Annotated per-cycle video from the camera JPEGs (video/ lives in the
    # recording dir, not necessarily out_dir). OPT-IN: off unless --video, since
    # it's slow and the analysis/plots don't need it.
    vid_src = (session_dir or out_dir) / "video"
    if args.video and not args.no_video and vid_src.exists():
        make_cycle_videos(
            vid_src,
            h7["laser"]["t"] if "laser" in h7 else None, disp_um,
            h7["load"]["t"] if "load" in h7 else None, force_N,
            fps=args.video_fps)

    # Joined CSV on a uniform 100 Hz grid spanning the data.
    t_end_candidates = []
    if raw_lcr is not None:
        t_end_candidates.append(raw_lcr.t.max())
    for ch in h7.values():
        t_end_candidates.append(ch["t"].max())
    if stage is not None:
        t_end_candidates.append(stage["t"].max())
    if t_end_candidates:
        t_end = max(t_end_candidates)
        grid = np.arange(t0, t_end, 0.01)  # 100 Hz
        if len(grid) >= 2:
            series: Dict[str, tuple] = {}
            if disp_um is not None:
                series["displacement_um"] = (h7["laser"]["t"], disp_um)
            elif "laser" in h7:
                series["laser_V"] = (h7["laser"]["t"], h7["laser"]["value"])
            if force_N is not None:
                series["force_N"] = (h7["load"]["t"], force_N)
            elif "load" in h7:
                series["load_V"] = (h7["load"]["t"], h7["load"]["value"])
            for ch in ("sma_v", "sma_i", "sma_r"):
                if ch in h7:
                    series[ch] = (h7[ch]["t"], h7[ch]["value"])
            # Electrical power into the coil — the Joule heating that drives the
            # transformation. Same P = V·I the dashboard plots.
            if "sma_v" in h7 and "sma_i" in h7:
                tv = h7["sma_v"]["t"]
                i_on_v = np.interp(tv, h7["sma_i"]["t"], h7["sma_i"]["value"])
                series["sma_power_W"] = (tv, h7["sma_v"]["value"] * i_on_v)
            if deemb is not None and deemb.method != "none":
                series["R_dut_ohm"] = (deemb.t, deemb.R_dut)
                series["L_dut_uH"] = (deemb.t, deemb.L_dut * 1e6)
                series["Q"] = (deemb.t, deemb.Q)
            if stage is not None:
                series["stage_mm"] = (stage["t"], stage["position_mm"])
            write_joined_csv(out_dir / f"{label}_joined.csv", t0, len(grid),
                             grid, series)

    # Console summary.
    print(f"\nSession: {out_dir.name}  ({label})")
    print(f"  H7 channels present: {sorted(h7.keys())}")
    print(f"  displacement: {'calibrated (µm)' if disp_um is not None else 'raw (no k/V0)'}")
    print(f"  force:        {'calibrated (N)' if force_N is not None else 'raw (no scale)'}")
    # Load-cell saturation report.
    sat = load_saturation_mask(h7)
    if sat is not None and sat.any():
        frac = 100.0 * int(sat.sum()) / sat.size
        print(f"  ⚠ load cell SATURATED: {int(sat.sum())}/{sat.size} samples "
              f"({frac:.1f}%) at the ±5 V ADC rail — force invalid there; "
              f"null the LCA-9PC ZERO pot.")
    # Baseline / cold-R report.
    if baseline:
        cr = baseline.get("cold_r_ohm")
        print(f"  baseline: cold R = "
              f"{cr:.4g} Ω" if cr is not None else "  baseline: cold R = n/a")
        off = baseline.get("applied_load_offset_V")
        if off is not None:
            print(f"            load tare offset = {off:.4g} V (applied)")
        for w in (baseline.get("warnings") or []):
            print(f"            ⚠ {w}")
    if deemb is not None:
        print(f"  LCR de-embed: {deemb.method}  (f={freq:.4g} Hz)")
        if deemb.method != "none" and len(deemb.R_dut):
            print(f"    R_dut mean={np.nanmean(deemb.R_dut):.4g} Ω  "
                  f"L_dut mean={np.nanmean(deemb.L_dut)*1e6:.4g} µH")
    if stage is not None:
        print(f"  stage: {len(stage['t'])} pts, "
              f"{stage['position_mm'].min():.3f}–{stage['position_mm'].max():.3f} mm")
    print(f"  outputs in: {out_dir}\n")
    return 0


def analyze_session(session_dir: Path, phase: str, args) -> int:
    """Legacy per-phase layout (sma_recorder.py): {phase}_{lcr,h7,stage}.csv
    plus dedicated open_lcr.csv / short_lcr.csv for de-embed references."""
    meta, freq, k, v0, lscale, loff = _read_meta_and_cal(session_dir, args)
    raw_lcr = load_lcr(session_dir / f"{phase}_lcr.csv")
    short_lcr = load_lcr(session_dir / "short_lcr.csv")
    open_lcr = load_lcr(session_dir / "open_lcr.csv")
    h7 = load_h7(session_dir / f"{phase}_h7.csv")
    stage = load_stage(session_dir / f"{phase}_stage.csv")
    out_dir = Path(args.out) if args.out else session_dir
    return _run_analysis(out_dir, phase, args, raw_lcr, short_lcr, open_lcr,
                         h7, stage, freq, k, v0, lscale, loff,
                         session_dir=session_dir, meta=meta)


def analyze_console_session(session_dir: Path, args) -> int:
    """Continuous-console layout (sma_console.py): one lcr.csv / h7.csv /
    stage.csv for the whole run, segmented by events.csv markers. The OPEN and
    SHORT de-embed references are the LCR samples in the `--ref-window` seconds
    that follow each `ref_open` / `ref_short` marker; the RAW (actuation) trace
    is every LCR sample OUTSIDE those reference windows."""
    meta, freq, k, v0, lscale, loff = _read_meta_and_cal(session_dir, args)
    events = load_events(session_dir / "events.csv")
    open_win = ref_windows(events, "ref_open", args.ref_window)
    short_win = ref_windows(events, "ref_short", args.ref_window)
    log.info("events.csv: %d markers — %d OPEN window(s), %d SHORT window(s)",
             len(events), len(open_win), len(short_win))

    full_lcr = load_lcr(session_dir / "lcr.csv")
    open_lcr = slice_lcr(full_lcr, open_win, inside=True)
    short_lcr = slice_lcr(full_lcr, short_win, inside=True)
    # The actuation trace excludes the reference windows so de-embedded RAW
    # values aren't polluted by the OPEN/SHORT captures.
    raw_lcr = slice_lcr(full_lcr, open_win + short_win, inside=False)

    h7 = load_h7(session_dir / "h7.csv")
    stage = load_stage(session_dir / "stage.csv")
    out_dir = Path(args.out) if args.out else session_dir
    return _run_analysis(out_dir, "console", args, raw_lcr, short_lcr, open_lcr,
                         h7, stage, freq, k, v0, lscale, loff,
                         session_dir=session_dir, meta=meta)


def latest_session(base=None):
    """Newest data/console_* session that actually HAS data (h7.csv beyond a
    header) — skips empty sessions. Falls back to the newest even if empty."""
    if base is None:
        base = Path(__file__).resolve().parent / "data"
    cands = sorted(p for p in base.glob("console_*") if p.is_dir())
    for p in reversed(cands):
        h7 = p / "h7.csv"
        if (h7.exists() and h7.stat().st_size > 200
                and (p / "meta.json").exists()):     # finalized (has meta)
            return p
    return cands[-1] if cands else None


def resolve_session(arg):
    """The --session dir, or auto-pick the latest console_* when omitted."""
    if arg:
        return Path(arg)
    s = latest_session()
    if s is None:
        print("ERROR: no --session given and no data/console_* session found",
              file=sys.stderr)
        sys.exit(2)
    log.info("no --session given — using latest: %s", s.name)
    return s


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Analyze + visualize a V3 SMA session")
    p.add_argument("--session", default=None,
                   help="session directory (default: latest data/console_*)")
    p.add_argument("--mode", default="auto", choices=("auto", "console", "phase"),
                   help="layout: 'console' (continuous + events.csv), 'phase' "
                        "(legacy OPEN/SHORT/RAW files), or 'auto' (default)")
    p.add_argument("--phase", default="raw", choices=("raw", "open", "short"),
                   help="phase-mode only: which phase to analyze (default: raw)")
    p.add_argument("--ref-window", type=float, default=10.0,
                   help="console-mode only: seconds of LCR after each "
                        "ref_open/ref_short marker to use as the de-embed "
                        "reference (default: 10)")
    p.add_argument("--out", default=None, help="output dir (default: session dir)")
    p.add_argument("--no-deembed", action="store_true")
    p.add_argument("--frequency", type=float, default=None)
    p.add_argument("--k", type=float, default=None, help="laser k_mV_per_um override")
    p.add_argument("--v0", type=float, default=None, help="laser V0_mV override")
    p.add_argument("--load-scale", type=float, default=None)
    p.add_argument("--load-offset", type=float, default=None)
    p.add_argument("--video", action="store_true",
                   help="ALSO build annotated per-cycle camera videos from the "
                        "JPEG frames (off by default — the plots/analysis don't "
                        "need it, and it's slow)")
    p.add_argument("--no-video", action="store_true",
                   help="(deprecated; video is already off by default) force-skip "
                        "video even if --video is given")
    p.add_argument("--video-fps", type=float, default=15.0,
                   help="playback fps for --video (default: 15)")
    args = p.parse_args()

    session_dir = resolve_session(args.session)   # auto-latest when omitted
    if not session_dir.exists():
        print(f"ERROR: session dir not found: {session_dir}", file=sys.stderr)
        return 2

    mode = args.mode
    if mode == "auto":
        # Console layout has events.csv + continuous h7.csv; legacy has
        # per-phase files like raw_h7.csv. Prefer console when present.
        is_console = ((session_dir / "events.csv").exists()
                      and (session_dir / "h7.csv").exists())
        is_phase = any((session_dir / f"{ph}_h7.csv").exists()
                       for ph in ("raw", "open", "short"))
        mode = "console" if (is_console and not is_phase) else (
            "phase" if is_phase else "console")
        log.info("auto-detected layout: %s", mode)

    if mode == "console":
        return analyze_console_session(session_dir, args)
    return analyze_session(session_dir, args.phase, args)


if __name__ == "__main__":
    sys.exit(_main())
