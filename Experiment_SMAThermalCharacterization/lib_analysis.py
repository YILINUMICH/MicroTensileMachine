#!/usr/bin/env python3
"""
lib_analysis.py — shared analysis helpers for SMA thermal console sessions.

Loaders, calibration, firmware/host clock alignment, actuation segmentation, and
signal fitting/filtering: the reusable core extracted from the former
sma_plots.py. Imported by operator_explore.ipynb (Plotly) and any offline
analysis. Carries no plotting code of its own; the colour constants are kept so
callers can share one palette.

Author: Yilin Ma - HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import matplotlib
# Interactive backend when --show is requested (zoom/pan + cursor coordinate
# readout via the toolbar); headless Agg otherwise (just writes PNGs). Decided
# from argv here because the backend must be chosen before pyplot is imported.
_SHOW = ("--show" in sys.argv)
if _SHOW:
    for _bk in ("QtAgg", "Qt5Agg", "TkAgg"):
        try:
            matplotlib.use(_bk)
            break
        except Exception:  # noqa: BLE001
            continue
else:
    matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _close(fig) -> None:
    """Close the figure unless we're going to show it interactively (--show),
    in which case keep it open for the final plt.show()."""
    if not _SHOW:
        plt.close(fig)


def _attach_cursor(fig) -> None:
    """--show only: add hover tooltips showing the (t, value) at the nearest
    data point, if `mplcursors` is installed (pip install mplcursors). Without
    it, the matplotlib toolbar's bottom-bar coordinate readout still shows the
    value under the cursor — so zoom + value-readout work either way."""
    if not _SHOW:
        return
    try:
        import mplcursors
    except Exception:  # noqa: BLE001
        return
    lines = [ln for ax in fig.axes for ln in ax.get_lines()]
    if not lines:
        return
    cur = mplcursors.cursor(lines, hover=True)
    cur.connect("add", lambda sel: sel.annotation.set_text(
        f"t={sel.target[0]:.4f}s\ny={sel.target[1]:.4g}"))
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.cm import ScalarMappable

log = logging.getLogger("lib_analysis")


# ===========================================================================
# Palette + axis style  (dataviz reference instance, light surface)
# ===========================================================================
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

C_VOLT = "#2a78d6"    # slot 1 blue    — drive voltage / load (ADC2)
C_CURR = "#1baf7a"    # slot 2 aqua    — drive current / "after"
C_RES = "#4a3aa7"     # slot 5 violet  — resistance
C_FORCE = "#e34948"   # slot 6 red     — force / duplicate samples
C_DISP = "#eb6834"    # slot 8 orange  — displacement / laser
C_POWER = "#c9349b"   # magenta        — drive power (P = V·I)
C_FIRE = "#eb6834"    # fire-window wash
C_REF = "#898781"     # reference lines and noise bands
C_FIT = "#0b0b0b"     # fitted curves

# Ordinal blue ramp, step 250 -> 700 (lightest step still clears 2:1 on light).
BLUE_RAMP = ["#86b6ef", "#5598e7", "#3987e5", "#2a78d6",
             "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
CYCLE_CMAP = LinearSegmentedColormap.from_list("cycles", BLUE_RAMP)

PRE_S = 0.40                      # pre-fire context shown / used for a baseline
BASE_LO, BASE_HI = -0.35, -0.03   # baseline window relative to the fire onset

# Opaque plate so an in-axes note stays legible on top of a dense trace.
_PLATE = dict(facecolor=SURFACE, edgecolor="none", alpha=0.88, pad=3.0)


def style(ax) -> None:
    """The one axis style. Recessive grid, no top/right spines, muted ticks."""
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=8)


# ===========================================================================
# I/O
# ===========================================================================
def _rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, newline="", errors="replace") as f:
        return list(csv.DictReader(f))


def load_h7(path: Path) -> Dict[str, dict]:
    """{channel: {'t': host_s, 'v': value, 'rc': raw_code, 'hw': firmware µs}}

    `t` (host clock) is the clock the stage and events share — align on it.
    `hw` (firmware clock) is uniform — filter and take spectra on it.
    `rc` is the raw ADC code — `diff(rc) == 0` marks a zero-order-hold repeat.
    """
    acc: Dict[str, dict] = {}
    for r in _rows(path):
        ch = (r.get("channel") or "").strip()
        if not ch:
            continue
        d = acc.setdefault(ch, {"t": [], "v": [], "rc": [], "hw": []})
        try:
            d["t"].append(float(r["host_timestamp_s"]))
            d["v"].append(float(r["value"]))
            d["rc"].append(float(r.get("raw_code") or "nan"))
            d["hw"].append(float(r.get("hw_us") or "nan"))
        except (ValueError, KeyError):
            continue
    return {ch: {k: np.asarray(v) for k, v in d.items()}
            for ch, d in acc.items() if d["t"]}


def timebase(d: dict, offset_us: float = 0.0) -> np.ndarray:
    """Analysis time base for one channel, in seconds. Prefer the firmware clock
    (`hw`, µs) over the host clock (`t`): the host stream is BURSTY — when the PC
    falls behind (camera/GUI) it logs samples late, which smears cycle timing
    (fires look mis-spaced / cools look long) even though the firmware ran on
    time. `hw` is stamped at conversion, so it's immune. Falls back to the host
    clock only if `hw` is missing/degenerate.

    IMPORTANT: laser/load run on the M4 core, SMA on M7 — two DIFFERENT clocks
    with a constant offset (M7 boots M4, so M7's µs runs ahead by ~2 s). Pass
    `offset_us = m7_us - m4_us` (from [STATUS]) for the M4 channels to shift them
    onto the M7 timeline; otherwise a fire onset in sma_v (M7) is compared against
    a force/disp window (M4) that is seconds off. SMA channels use offset_us=0."""
    hw = d.get("hw")
    if hw is not None and hw.size and np.isfinite(hw).all() and (hw[-1] > hw[0]):
        return hw / 1e6 + offset_us / 1e6
    return d["t"]


def load_m4_to_m7_offset_us(sess: Path) -> float:
    """Median (m7_us - m4_us) over the session's [STATUS] frames, in µs — the
    constant offset to add to M4 (laser/load) hw to put it on the M7 timeline.
    0.0 if status.csv is absent or carries no m7_us/m4_us (then M4/M7 stay on
    their own clocks — cross-core slices may be seconds off; a warning is
    logged)."""
    p = sess / "status.csv"
    if not p.exists():
        return 0.0
    diffs = []
    for r in _rows(p):
        try:
            f = json.loads(r.get("fields_json") or "{}")
            m7, m4 = f.get("m7_us"), f.get("m4_us")
            if m7 is not None and m4 is not None:
                diffs.append(float(m7) - float(m4))
        except (ValueError, KeyError, TypeError):
            continue
    if not diffs:
        return 0.0
    diffs.sort()
    return diffs[len(diffs) // 2]


def latest_session(base: Optional[Path] = None) -> Optional[Path]:
    """Newest data/raw/console_* session that actually HAS data (h7.csv beyond a
    header) — skips empty sessions (e.g. opened then closed without recording).
    Names are timestamped, so name-sort is chronological. Falls back to the
    newest session even if empty."""
    if base is None:
        base = Path(__file__).resolve().parent / "data" / "raw"
    # data/raw is grouped into subfolders (2026-08-06): a NEW session still
    # lands loose in data/raw/, but archived ones sit under troubleshoot/ or
    # campaigns/<key>/. Search one level down as well, or the picker reports
    # "no console_* session found" for a folder that is simply filed.
    cands = sorted((p for p in [*base.glob("console_*"), *base.glob("*/console_*")]
                    if p.is_dir()), key=lambda p: p.name)
    for p in reversed(cands):
        h7 = p / "h7.csv"
        if (h7.exists() and h7.stat().st_size > 200
                and (p / "meta.json").exists()):     # finalized (has meta)
            return p
    return cands[-1] if cands else None


def resolve_session(arg: Optional[str]) -> Path:
    """Return the --session dir, or auto-pick the latest console_* when omitted."""
    if arg:
        return Path(arg)
    s = latest_session()
    if s is None:
        print("ERROR: no --session given and no data/raw/console_* session found",
              file=sys.stderr)
        sys.exit(2)
    log.info("no --session given — using latest: %s", s.name)
    return s


def _config_meta_fallback() -> dict:
    """Minimal meta dict from the module's config.yaml — for sessions with NO
    meta.json (didn't finalize, e.g. a crash). The recorder writes meta's
    calibration straight from config.yaml, so these values match a finalized
    session's; the SMA V/I/R and timing don't need meta at all."""
    import yaml
    cfg = Path(__file__).resolve().parent / "config.yaml"
    try:
        d = yaml.safe_load(cfg.read_text()) or {}
    except Exception:  # noqa: BLE001
        d = {}
    return {"calibration": d.get("calibration") or {},
            "sma": d.get("sma") or {}, "baseline": {}}


def load_meta(sess: Path) -> tuple:
    """(meta, k_mV_per_um, V0_mV, load_scale_N_per_V, load_offset_V, cold_R)"""
    mp = sess / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text())
    else:
        log.warning("%s has no meta.json (session didn't finalize) — falling "
                    "back to config.yaml calibration", sess.name)
        meta = _config_meta_fallback()
    cal = meta.get("calibration", {})
    return (meta,
            cal.get("laser", {}).get("k_mV_per_um"),
            cal.get("laser", {}).get("V0_mV"),
            cal.get("load_cell", {}).get("scale_N_per_V"),
            cal.get("load_cell", {}).get("offset_V", 0.0),
            (meta.get("baseline") or {}).get("cold_r_ohm"))


# ===========================================================================
# Segmentation — the ONE definition of "where does a fire start"
# ===========================================================================
@dataclass
class CycleCmd:
    v_high: float
    v_low: float
    fire_ms: float
    cool_ms: float
    n: int

    @property
    def period_s(self) -> float:
        return (self.fire_ms + self.cool_ms) / 1000.0

    @property
    def fire_s(self) -> float:
        return self.fire_ms / 1000.0


def load_cycle_cmd(events_path: Path) -> Optional[CycleCmd]:
    """Recover the last `cycle v_high v_low fire_ms cool_ms n` command issued."""
    pat = re.compile(r"^cycle\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)")
    found = None
    for r in _rows(events_path):
        if (r.get("kind") or "").strip() != "cmd":
            continue
        m = pat.match((r.get("detail") or "").strip())
        if m:
            found = CycleCmd(float(m[1]), float(m[2]),
                             float(m[3]), float(m[4]), int(m[5]))
    return found


def find_fire_onsets(t: np.ndarray, v: np.ndarray,
                     v_high: float, v_low: float) -> np.ndarray:
    """Host-clock timestamps of each fire onset (rising edge of the drive)."""
    thresh = 0.5 * (v_high + v_low)
    hot = v > thresh
    rise = np.flatnonzero(np.diff(hot.astype(np.int8)) == 1) + 1
    if hot[0]:                     # a fire already in progress at the first sample
        rise = np.insert(rise, 0, 0)
    return t[rise]


def slice_cycle(t: np.ndarray, y: np.ndarray, t_fire: float,
                span_s: float) -> tuple:
    """(t_rel, y) for one cycle window, t_rel measured from the fire onset."""
    m = (t >= t_fire - PRE_S) & (t < t_fire + span_s)
    return t[m] - t_fire, y[m]


def baseline(t_rel: np.ndarray, y: np.ndarray) -> float:
    m = (t_rel >= BASE_LO) & (t_rel <= BASE_HI)
    return float(np.mean(y[m])) if m.any() else float("nan")


def noise_sd(t: np.ndarray, y: np.ndarray, onsets: np.ndarray) -> float:
    """1σ noise on the COOL stretches (>0.5 s past a fire).

    From the sample-to-sample difference (σ = std(Δy)/√2), NOT a plain std: the
    force ratchets upward across a session and a plain std would charge that real
    drift to the noise budget and hide a signal that is ~80× its own scatter.
    """
    quiet = np.ones(t.shape, dtype=bool)
    for ot in onsets:
        quiet &= ~((t >= ot - 0.05) & (t <= ot + 0.5))
    pair = quiet[:-1] & quiet[1:]
    if not pair.any():
        return float("nan")
    return float(np.std(np.diff(y)[pair]) / np.sqrt(2.0))


# ===========================================================================
# Fitting + filtering
# ===========================================================================
def fit_tone(t: np.ndarray, y: np.ndarray, f_lo: float, f_hi: float,
             n: int = 4000) -> tuple:
    """Least-squares fit of one sinusoid; scan frequency for the best fit.
    Returns (f, amplitude, phase, fraction of variance explained)."""
    best = (np.nan, 0.0, 0.0, -1.0)
    var = np.var(y)
    for f0 in np.linspace(f_lo, f_hi, n):
        A = np.c_[np.cos(2 * np.pi * f0 * t), np.sin(2 * np.pi * f0 * t)]
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        r2 = 1.0 - np.var(y - A @ coef) / var
        if r2 > best[3]:
            best = (f0, float(np.hypot(*coef)),
                    float(np.arctan2(-coef[1], coef[0])), float(r2))
    return best


def fit_exp(t: np.ndarray, y: np.ndarray, tau_lo=0.05, tau_hi=30.0, n=600):
    """y = y_inf + A·exp(-t/τ). Scan τ, solve (y_inf, A) linearly at each — no
    scipy, and it cannot fall into a local minimum."""
    ok = np.isfinite(t) & np.isfinite(y)
    t, y = t[ok], y[ok]
    best = (np.nan, np.nan, np.nan, -np.inf)
    for tau in np.geomspace(tau_lo, tau_hi, n):
        M = np.c_[np.ones_like(t), np.exp(-t / tau)]
        coef, *_ = np.linalg.lstsq(M, y, rcond=None)
        r2 = 1.0 - np.sum((y - M @ coef) ** 2) / np.sum((y - y.mean()) ** 2)
        if r2 > best[3]:
            best = (tau, float(coef[0]), float(coef[1]), float(r2))
    return best      # tau, y_inf, A, R²


def notch_fft(y: np.ndarray, fs: float, freqs, half_width: float) -> np.ndarray:
    """Zero the FFT bins within ±half_width of each frequency. A brick wall is
    fine here: this only ever runs on the quiet before/after DEMO, to show what
    the channel is worth without the interferer."""
    Y = np.fft.rfft(y)
    f = np.fft.rfftfreq(len(y), 1.0 / fs)
    for f0 in freqs:
        Y[np.abs(f - f0) <= half_width] = 0.0
    return np.fft.irfft(Y, n=len(y))


def _uniform(t_us: np.ndarray, y: np.ndarray) -> tuple:
    t = (t_us - t_us[0]) / 1e6
    fs = 1.0 / float(np.median(np.diff(t)))
    g = np.arange(0.0, t[-1], 1.0 / fs)
    return g, np.interp(g, t, y), fs


def _apply_mask(y: np.ndarray, fs: float, mask_of) -> np.ndarray:
    Y = np.fft.rfft(y - y.mean())
    f = np.fft.rfftfreq(len(y), 1.0 / fs)
    return np.fft.irfft(Y * mask_of(f), n=len(y)) + y.mean()


def filter_channel(t_us: np.ndarray, t_host: np.ndarray, y: np.ndarray,
                   notch_hz: Optional[float], notch_q: float,
                   n_harm: int, lowpass_hz: Optional[float]) -> np.ndarray:
    """Opt-in displacement filter. Zero-phase (a magnitude mask in the frequency
    domain), so nothing shifts relative to the fire onset. Rolloffs are SMOOTH
    (Gaussian notch, Butterworth-magnitude low-pass) — a brick wall rings badly
    around the sharp step the drive feedthrough puts at every fire.

    Filters on the firmware clock, then maps back onto the ORIGINAL samples so
    the caller's host-clock alignment is untouched.
    """
    if not np.isfinite(t_us).all():
        log.warning("hw_us missing — filtering on the (bursty) host clock; "
                    "the notch will be smeared")
        t_us = (t_host - t_host[0]) * 1e6
    g, yg, fs = _uniform(t_us, y)

    if notch_hz:
        width = notch_hz / max(notch_q, 1e-6)
        freqs = [n * notch_hz for n in range(1, n_harm + 1)
                 if n * notch_hz < fs / 2]

        def notch_mask(f):
            m = np.ones_like(f)
            for f0 in freqs:
                m *= 1.0 - np.exp(-0.5 * ((f - f0) / width) ** 2)
            return m
        yg = _apply_mask(yg, fs, notch_mask)

    if lowpass_hz:
        def lp_mask(f):
            return 1.0 / np.sqrt(1.0 + (f / lowpass_hz) ** 8)
        yg = _apply_mask(yg, fs, lp_mask)

    return np.interp((t_us - t_us[0]) / 1e6, g, yg)


def _require(sess: Path, h7: Dict[str, dict], needed) -> Optional[int]:
    for need in needed:
        if need not in h7:
            print(f"ERROR: channel '{need}' missing from {sess}/h7.csv",
                  file=sys.stderr)
            return 2
    return None
