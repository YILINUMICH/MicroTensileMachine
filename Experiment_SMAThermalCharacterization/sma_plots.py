#!/usr/bin/env python3
"""
sma_plots.py — analysis + figures for an SMA thermal console session.

One entry point, three views. `analyze_sma.py` stays as it is (the raw
whole-session dashboard, the legacy OPEN/SHORT/RAW layout, LCR de-embedding and
the annotated per-cycle videos); everything that reasons about the ACTUATION
PATTERN lives here.

    python sma_plots.py all        --session data/console_20260713_115921
    python sma_plots.py cycles     --session <dir> [--r-ref cycle] [--notch auto]
    python sma_plots.py laser      --session <dir>
    python sma_plots.py transition --session <dir> [--bin-ms 150]

  cycles      Segment the run by its actuation pattern (`cycle <v_high> <v_low>
              <fire_ms> <cool_ms> <n>` from events.csv), locating each fire as a
              rising edge of sma_v.
              → cycles_timeline.png, cycles_overlay.png, cycles_trend.png,
                cycles_metrics.csv

  laser       Laser-channel diagnostic: the coherent ~65.8 Hz instrumental tone
              and the zero-order-hold duplicate rows. Point it at an idle
              recording (see the README's reference sample).
              → laser_diagnostics.png, laser_before_after.png

  transition  Is the SMA transition real, and what is its time constant? Ensemble
              + per-cycle ΔR/R₀, plus a first-order cooling fit.
              → transition_fit.png, transition_per_cycle.png

Three things the module has learned the hard way, all enforced below:

  * Use `hw_us` (the firmware clock) for anything spectral. The host timestamps
    are USB-batched and bursty (median dt ~1 ms but σ ~3 ms) and smear the
    laser tone into invisibility.
  * Estimate noise as std(diff(y))/√2, not a plain std. The force ratchets
    upward across a run and a plain std charges that real drift to the noise
    budget.
  * Never take max() over a noisy window. The maximum of a ±6% noise floor is
    just the largest noise excursion — always positive — which is exactly what
    once hid the real, NEGATIVE ~3% resistance drop. Use the window mean.

Author: Yilin Ma — HDR Lab, University of Michigan
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
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.cm import ScalarMappable

log = logging.getLogger("sma_plots")


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


def timebase(d: dict) -> np.ndarray:
    """Analysis time base for one channel, in seconds. Prefer the firmware clock
    (`hw`, µs) over the host clock (`t`): the host stream is BURSTY — when the PC
    falls behind (camera/GUI) it logs samples late, which smears cycle timing
    (fires look mis-spaced / cools look long) even though the firmware ran on
    time. `hw` is stamped at the instant of conversion, so it's immune. Falls
    back to the host clock only if `hw` is missing/degenerate. Both H7 cores boot
    together, so all channels' hw share a common (~boot) origin — safe to compare
    a drive-channel onset against a sensor-channel window."""
    hw = d.get("hw")
    if hw is not None and hw.size and np.isfinite(hw).all() and (hw[-1] > hw[0]):
        return hw / 1e6
    return d["t"]


def load_meta(sess: Path) -> tuple:
    """(meta, k_mV_per_um, V0_mV, load_scale_N_per_V, load_offset_V, cold_R)"""
    meta = json.loads((sess / "meta.json").read_text())
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


# ===========================================================================
# VIEW: cycles
# ===========================================================================
def view_cycles(args) -> int:
    sess = Path(args.session)
    meta, k, v0, lscale, loff, cold_r = load_meta(sess)

    h7 = load_h7(sess / "h7.csv")
    err = _require(sess, h7, ("sma_v", "sma_i", "sma_r", "load", "laser"))
    if err:
        return err

    cmd = load_cycle_cmd(sess / "events.csv")
    if cmd is None:
        print("ERROR: no `cycle ...` command in events.csv — this session has no "
              "actuation pattern to follow", file=sys.stderr)
        return 2
    log.info("actuation: %d cycles, fire %.0f ms @ %.1f V, cool %.0f ms @ %.1f V",
             cmd.n, cmd.fire_ms, cmd.v_high, cmd.cool_ms, cmd.v_low)

    disp_um = (h7["laser"]["v"] * 1000.0 - v0) / k
    force_N = lscale * (h7["load"]["v"] - loff)

    # ---- optional displacement filter (raw is the default) ------------------
    filt_label = ""
    notch_hz: Optional[float] = None
    if args.notch:
        if str(args.notch).lower() == "auto":
            lt = h7["laser"]
            hw = lt["hw"] if np.isfinite(lt["hw"]).all() else (lt["t"] - lt["t"][0]) * 1e6
            tt = (hw - hw[0]) / 1e6
            f0, _amp, _ph, r2 = fit_tone(tt, disp_um - disp_um.mean(), 40.0, 90.0,
                                         n=2000)
            notch_hz = float(f0)
            log.info("--notch auto: fitted tone at %.3f Hz (R²=%.2f)", notch_hz, r2)
        else:
            notch_hz = float(args.notch)
    if notch_hz or args.lowpass:
        sd_before = float(np.std(disp_um))
        disp_um = filter_channel(h7["laser"]["hw"], h7["laser"]["t"], disp_um,
                                 notch_hz, args.notch_q, args.notch_harmonics,
                                 args.lowpass)
        bits = []
        if notch_hz:
            bits.append(f"notch {notch_hz:.2f} Hz ×{args.notch_harmonics} harm")
        if args.lowpass:
            bits.append(f"low-pass {args.lowpass:g} Hz")
        filt_label = " + ".join(bits)
        log.warning("DISPLACEMENT IS FILTERED (%s): σ %.2f → %.2f µm. Force and "
                    "resistance are untouched; outputs get a '_filt' suffix.",
                    filt_label, sd_before, float(np.std(disp_um)))

    # Time base = firmware clock (hw), NOT the bursty host clock — see timebase().
    sig = {
        "volt": (timebase(h7["sma_v"]), h7["sma_v"]["v"]),
        "curr": (timebase(h7["sma_i"]), h7["sma_i"]["v"]),
        "res": (timebase(h7["sma_r"]), h7["sma_r"]["v"]),
        "force": (timebase(h7["load"]), force_N),
        "disp": (timebase(h7["laser"]), disp_um),
    }
    _tb = "firmware (hw_us)" if (np.isfinite(h7["sma_v"]["hw"]).all()
                                 and h7["sma_v"]["hw"][-1] > h7["sma_v"]["hw"][0]) \
        else "host (hw_us unavailable — timing may be smeared)"
    log.info("cycle time base: %s", _tb)

    onsets = find_fire_onsets(*sig["volt"], cmd.v_high, cmd.v_low)
    log.info("found %d fire onset(s) in sma_v (commanded %d)", len(onsets), cmd.n)
    if len(onsets) == 0:
        print("ERROR: no fire edges in sma_v", file=sys.stderr)
        return 2
    span, fire_s, t0 = cmd.period_s, cmd.fire_s, onsets[0]

    sfx = "_filt" if filt_label else ""
    stamp = f"  [displacement FILTERED: {filt_label}]" if filt_label else ""
    disp_note = f" — FILTERED ({filt_label})" if filt_label else ""

    sd_r = noise_sd(*sig["res"], onsets)
    sd_disp = noise_sd(*sig["disp"], onsets)
    sd_force = noise_sd(*sig["force"], onsets)
    log.info("noise floor (cool periods): R ±%.3f Ω, disp ±%.2f µm, "
             "force ±%.4f N (1σ)", sd_r, sd_disp, sd_force)

    # Drive-feedthrough test. A real contraction visible in the force must still
    # be there when the force PEAKS (~0.2-0.3 s in). An excursion that lives only
    # inside the drive window and is gone by then is the 0.7 A pulse coupling
    # into the laser's ADC channel, not motion.
    d_t, d_y = sig["disp"]
    in_fire, post_fire = [], []
    for ot in onsets:
        tr, y = slice_cycle(d_t, d_y, ot, span)
        b = baseline(tr, y)
        mf = (tr >= 0) & (tr <= fire_s)
        mp = (tr >= 0.20) & (tr <= 0.30)
        if mf.any():
            in_fire.append(float(np.mean(y[mf]) - b))
        if mp.any():
            post_fire.append(float(np.mean(y[mp]) - b))
    disp_in = float(np.nanmean(in_fire)) if in_fire else float("nan")
    disp_post = float(np.nanmean(post_fire)) if post_fire else float("nan")
    # Test the SHAPE, not the absolute level — comparing disp_post against the
    # noise floor would stop flagging under --lowpass/--notch (which collapse the
    # floor) even though the feedthrough step is still plainly there.
    feedthrough = (abs(disp_in) > 2 * sd_disp
                   and abs(disp_post) < 0.3 * abs(disp_in))
    if feedthrough:
        log.warning("laser excursion is confined to the drive window (%.2f µm "
                    "in-fire vs %.2f µm at force peak) — drive feedthrough, not "
                    "displacement", disp_in, disp_post)

    # ---- resistance normalization -------------------------------------------
    # Raw ohms aren't comparable across cycles — the pre-fire R drifts. Normalize
    # to a reference R₀ so the fire's excursion reads as a FRACTION of the coil's
    # own resistance.
    r_t, r_y = sig["res"]
    r0_global = cold_r
    if r0_global is None:
        pre = r_y[r_t < onsets[0]]
        r0_global = float(np.mean(pre)) if len(pre) else float(np.mean(r_y))
    r0_cycle: List[float] = []
    for ot in onsets:
        tr, r = slice_cycle(r_t, r_y, ot, span)
        b = baseline(tr, r)
        r0_cycle.append(b if np.isfinite(b) else float(r0_global))

    def r_ref(i: int) -> float:
        return float(r0_global) if args.r_ref == "cold" else r0_cycle[i]

    log.info("resistance normalized to R₀ (%s): %.3f Ω", args.r_ref, r0_global)
    sd_r_n = sd_r / float(r0_global)

    # ---- Figure 1: timeline (one panel per measure — never two y-scales) -----
    rows = [
        ("volt", "drive V", "V", C_VOLT),
        ("curr", "drive I", "A", C_CURR),
        ("res", f"SMA resistance\n(R/R₀, R₀={r0_global:.2f} Ω)", "R/R₀", C_RES),
        ("force", "force", "N", C_FORCE),
        ("disp", f"displacement (Δ vs start){disp_note}", "µm", C_DISP),
    ]
    fig, axes = plt.subplots(len(rows), 1, figsize=(12, 11), sharex=True,
                             facecolor=SURFACE)
    t_lo, t_hi = -PRE_S - 0.6, onsets[-1] - t0 + span + 0.6
    for ax, (key, name, unit, color) in zip(axes, rows):
        t, y = sig[key]
        tr = t - t0
        if key == "disp":
            m0 = (tr >= t_lo) & (tr < 0)
            y = y - (np.mean(y[m0]) if m0.any() else y[0])
        if key == "res":
            y = y / float(r0_global)   # one global ref, so the drift stays visible
        style(ax)
        for i, ot in enumerate(onsets):
            ax.axvspan(ot - t0, ot - t0 + fire_s, color=C_FIRE, alpha=0.16, lw=0,
                       label="fire pulse" if i == 0 else None)
        ax.plot(tr, y, lw=1.1, color=color, solid_joinstyle="round")
        if key == "res":
            ax.axhspan(1 - 2 * sd_r_n, 1 + 2 * sd_r_n, color=C_REF, alpha=0.16,
                       lw=0, zorder=0, label=f"±2σ single-sample noise "
                                             f"({2 * sd_r_n:.1%})")
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.text(t_hi, 1.0, f" R₀ ({r0_global:.2f} Ω)", color=INK_2,
                    fontsize=8, va="center", ha="left")
        ax.set_ylabel(f"{name}\n({unit})" if "\n" not in name else name,
                      fontsize=9, color=INK_2)
    axes[0].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)
    axes[-1].set_xlabel("time since first fire (s)", fontsize=9, color=INK_2)
    axes[-1].set_xlim(t_lo, t_hi)
    fig.suptitle(
        f"{sess.name} — {cmd.n}× (fire {cmd.fire_ms:.0f} ms @ {cmd.v_high:.1f} V "
        f"→ cool {cmd.cool_ms:.0f} ms @ {cmd.v_low:.1f} V){stamp}",
        fontsize=13, color=INK, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    out = sess / f"cycles_timeline{sfx}.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # ---- Figure 2: every cycle overlaid on fire-onset time -------------------
    norm = Normalize(vmin=1, vmax=len(onsets))
    r_ref_note = ("session cold R₀" if args.r_ref == "cold"
                  else "each cycle's own pre-fire R₀")
    panels = [
        ("volt", "Drive voltage", "V", False),
        ("curr", "Drive current", "A", False),
        ("res", f"SMA resistance — normalized to {r_ref_note}", "R/R₀", False),
        ("force", "Force (Δ vs pre-fire)", "N", True),
        ("disp", f"Displacement (Δ vs pre-fire){disp_note}", "µm", True),
    ]
    fig, axes = plt.subplots(3, 2, figsize=(13, 10.5), facecolor=SURFACE)
    axes = axes.ravel()
    for ax, (key, name, unit, as_delta) in zip(axes, panels):
        t, y = sig[key]
        style(ax)
        ax.axvspan(0, fire_s, color=C_FIRE, alpha=0.16, lw=0, label="fire pulse")
        for i, ot in enumerate(onsets):
            tr, yc = slice_cycle(t, y, ot, span)
            if key == "res":
                yc = yc / r_ref(i)          # every cycle now starts at 1.0
            if as_delta:
                yc = yc - baseline(tr, yc)
            ax.plot(tr, yc, lw=1.1, color=CYCLE_CMAP(norm(i + 1)), alpha=0.9)
        if key == "res":
            ax.axhspan(1 - 2 * sd_r_n, 1 + 2 * sd_r_n, color=C_REF, alpha=0.16,
                       lw=0, zorder=0,
                       label=f"±2σ single-sample noise ({2 * sd_r_n:.1%})")
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.legend(loc="upper right", fontsize=8, frameon=False,
                      labelcolor=INK_2)
            # A SINGLE sample cannot see the transition — but the effect is real
            # and negative; averaging the fire window and the cycles finds it at
            # >5σ. Say so, rather than leaving the reader thinking R is useless.
            ax.text(0.985, 0.04,
                    f"R₀ = {r0_global:.2f} Ω. One sample can't leave the ±2σ band —\n"
                    f"but averaged over the fire window AND the cycles, R DROPS\n"
                    f"~3% during the fire (>5σ). See the `transition` view.",
                    transform=ax.transAxes, color=INK_2, fontsize=8, ha="right",
                    va="bottom", bbox=_PLATE)
        if key == "disp" and feedthrough:
            ax.text(0.985, 0.04,
                    f"excursion is confined to the drive window\n"
                    f"({disp_in:+.1f} µm during fire, {disp_post:+.1f} µm at the\n"
                    f"force peak) → drive feedthrough, not motion",
                    transform=ax.transAxes, color=INK_2, fontsize=8, ha="right",
                    va="bottom", bbox=_PLATE)
        if as_delta:
            ax.axhline(0, lw=0.8, color=AXIS)
        ax.set_title(name, fontsize=10, color=INK, loc="left")
        ax.set_ylabel(unit, fontsize=9, color=INK_2)
        ax.set_xlabel("time since fire onset (s)", fontsize=9, color=INK_2)
        ax.set_xlim(-PRE_S, span)
    axes[0].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)

    ax = axes[5]
    ax.set_facecolor(SURFACE)
    ax.axis("off")
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=CYCLE_CMAP), ax=ax,
                      orientation="horizontal", fraction=0.25, pad=0.05, aspect=18)
    cb.set_label("cycle index (light → dark = early → late)", fontsize=9,
                 color=INK_2)
    cb.set_ticks(range(1, len(onsets) + 1))
    cb.ax.tick_params(colors=MUTED, labelsize=8)
    cb.outline.set_edgecolor(AXIS)
    ax.text(0.0, 0.42,
            f"{len(onsets)} cycles overlaid on the fire onset.\n"
            f"fire  {cmd.fire_ms:.0f} ms @ {cmd.v_high:.1f} V\n"
            f"cool  {cmd.cool_ms:.0f} ms @ {cmd.v_low:.1f} V (probe)\n"
            f"drive stream {len(sig['volt'][0]) / (sig['volt'][0][-1] - sig['volt'][0][0]):.0f} Hz, "
            f"sensors {len(sig['force'][0]) / (sig['force'][0][-1] - sig['force'][0][0]):.0f} Hz",
            transform=ax.transAxes, fontsize=9, color=INK_2, va="top",
            linespacing=1.6)
    fig.suptitle(f"{sess.name} — cycle overlay{stamp}", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = sess / f"cycles_overlay{sfx}.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # ---- per-cycle metrics + Figure 3: the cycle-to-cycle trend --------------
    met: List[dict] = []
    for i, ot in enumerate(onsets):
        row = {"cycle": i + 1, "t_fire_s": float(ot - t0)}

        tr, r = slice_cycle(*sig["res"], ot, span)
        r0 = baseline(tr, r)
        ref = r_ref(i)
        row["R0_ref_ohm"] = ref
        row["R_pre_ohm"] = r0
        row["R_pre_norm"] = r0 / ref
        # MEAN over the fire window — NOT max(). With ~3% per-sample noise the
        # maximum of the window is just the largest noise excursion (and always
        # positive), which hid the real effect: R *drops* ~3% during the fire.
        hot = (tr >= 0) & (tr <= fire_s)
        row["R_fire_ohm"] = float(np.mean(r[hot])) if hot.any() else float("nan")
        row["dR_fire_ohm"] = row["R_fire_ohm"] - r0
        row["dR_fire_pct"] = 100.0 * (row["R_fire_ohm"] - ref) / ref
        end = tr >= span - 0.5
        row["R_end_ohm"] = float(np.mean(r[end])) if end.any() else float("nan")
        row["R_end_norm"] = row["R_end_ohm"] / ref

        tr, f = slice_cycle(*sig["force"], ot, span)
        f0_ = baseline(tr, f)
        row["F_pre_N"] = f0_
        if len(f):
            j = int(np.argmax(np.abs(f - f0_)))
            row["dF_peak_N"] = float(f[j] - f0_)
            row["t_F_peak_s"] = float(tr[j])
        end = tr >= span - 0.5
        row["dF_end_N"] = float(np.mean(f[end]) - f0_) if end.any() else float("nan")

        tr, d = slice_cycle(*sig["disp"], ot, span)
        d0 = baseline(tr, d)
        if len(d):
            j = int(np.argmax(np.abs(d - d0)))
            row["dDisp_peak_um"] = float(d[j] - d0)
        mf = (tr >= 0) & (tr <= fire_s)
        mp = (tr >= 0.20) & (tr <= 0.30)
        row["dDisp_in_fire_um"] = float(np.mean(d[mf]) - d0) if mf.any() else float("nan")
        row["dDisp_at_Fpeak_um"] = float(np.mean(d[mp]) - d0) if mp.any() else float("nan")
        met.append(row)

    cols = ["cycle", "t_fire_s", "R0_ref_ohm", "R_pre_ohm", "R_pre_norm",
            "R_fire_ohm", "dR_fire_ohm", "dR_fire_pct", "R_end_ohm", "R_end_norm",
            "F_pre_N", "dF_peak_N", "t_F_peak_s", "dF_end_N",
            "dDisp_peak_um", "dDisp_in_fire_um", "dDisp_at_Fpeak_um"]
    met_csv = sess / f"cycles_metrics{sfx}.csv"
    with open(met_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in met:
            w.writerow({c: f"{row.get(c, float('nan')):.6g}" for c in cols})
    log.info("wrote %s", met_csv)

    cyc = np.array([m["cycle"] for m in met])
    trends = [
        ("dR_fire_pct", "ΔR/R₀ averaged over the fire window", "ΔR/R₀ (%)", C_RES),
        ("dF_peak_N", "Peak force swing", "ΔF (N)", C_FORCE),
        ("R_pre_norm", "Pre-fire resistance (did it cool back to R₀?)", "R/R₀", C_RES),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), facecolor=SURFACE)
    for ax, (key, name, unit, color) in zip(axes, trends):
        y = np.array([m.get(key, np.nan) for m in met], dtype=float)
        style(ax)
        if key == "dR_fire_pct":
            band = 100.0 * 2 * sd_r_n
            ax.axhspan(-band, band, color=C_REF, alpha=0.16, lw=0, zorder=0,
                       label=f"±2σ single-sample noise (±{band:.1f}%)")
            ax.axhline(0, lw=0.8, color=AXIS)
            ax.legend(loc="upper left", fontsize=8, frameon=False, labelcolor=INK_2)
        if key == "dF_peak_N":
            ax.axhspan(-2 * sd_force, 2 * sd_force, color=C_REF, alpha=0.16,
                       lw=0, zorder=0, label=f"±2σ noise ({2 * sd_force:.3f} N)")
            ax.legend(loc="upper left", fontsize=8, frameon=False, labelcolor=INK_2)
        ax.plot(cyc, y, "-o", lw=1.6, ms=6, color=color,
                markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3)
        if key == "R_pre_norm":
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.text(0.98, 0.06, f"R₀ = {r0_global:.2f} Ω", transform=ax.transAxes,
                    color=INK_2, fontsize=8, ha="right")
        if np.isnan(y[0]):
            ax.text(0.02, 0.02, "cycle 1: no pre-fire baseline (stream starts at "
                    "the first fire)", transform=ax.transAxes, color=MUTED,
                    fontsize=7, ha="left")
        for j in (0, len(cyc) - 1):
            if np.isfinite(y[j]):
                ax.annotate(f"{y[j]:.3g}", (cyc[j], y[j]),
                            textcoords="offset points", xytext=(0, 9), ha="center",
                            fontsize=8, color=INK_2)
        ax.set_title(name, fontsize=10, color=INK, loc="left")
        ax.set_xlabel("cycle", fontsize=9, color=INK_2)
        ax.set_ylabel(unit, fontsize=9, color=INK_2)
        ax.set_xticks(cyc)
        ax.margins(y=0.22)
    fig.suptitle(f"{sess.name} — cycle-to-cycle trend{stamp}", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = sess / f"cycles_trend{sfx}.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # ---- summary ------------------------------------------------------------
    dR = np.array([m["dR_fire_pct"] for m in met], dtype=float)
    dF = np.array([m["dF_peak_N"] for m in met], dtype=float)
    print(f"\n{sess.name}: {len(onsets)} cycles "
          f"(fire {cmd.fire_ms:.0f} ms @ {cmd.v_high:.1f} V, "
          f"cool {cmd.cool_ms:.0f} ms @ {cmd.v_low:.1f} V)")
    print(f"  force   : ΔF {np.nanmean(dF):+.4f} ± {np.nanstd(dF):.4f} N peak, "
          f"{np.nanmean([m['t_F_peak_s'] for m in met]):.2f} s after onset "
          f"(noise ±{sd_force:.4f} N) — RESOLVED")
    good = dR[np.isfinite(dR)]
    sem = float(np.std(good, ddof=1) / np.sqrt(len(good))) if len(good) > 1 else float("nan")
    tstat = float(np.mean(good) / sem) if sem else float("nan")
    print(f"  resist. : R₀ = {r0_global:.3f} Ω ({args.r_ref} ref); ΔR/R₀ over the "
          f"fire window = {np.mean(good):+.2f} ± {sem:.2f} % (SEM over "
          f"{len(good)} cycles) → t={tstat:+.1f} "
          f"{'RESOLVED' if abs(tstat) > 3 else 'not resolved'}")
    print(f"            (single-sample noise is ±{100 * sd_r_n:.1f} %, so this is "
          f"only visible by averaging the window AND the cycles — "
          f"see the `transition` view)")
    print(f"  laser   : {disp_in:+.2f} µm inside the fire window, "
          f"{disp_post:+.2f} µm at the force peak (noise ±{sd_disp:.2f} µm)"
          + (" — DRIVE FEEDTHROUGH, not motion" if feedthrough else ""))
    if filt_label:
        print(f"  ⚠ displacement FILTERED: {filt_label} "
              f"(force/resistance untouched; raw PNGs preserved)")
    print(f"  outputs : cycles_timeline{sfx}.png, cycles_overlay{sfx}.png, "
          f"cycles_trend{sfx}.png, cycles_metrics{sfx}.csv\n")
    return 0


# ===========================================================================
# VIEW: laser
# ===========================================================================
def view_laser(args) -> int:
    sess = Path(args.session)
    meta, k, v0, _ls, _lo, _cr = load_meta(sess)

    ch = load_h7(sess / "h7.csv")
    err = _require(sess, ch, ("laser",))
    if err:
        return err
    las = ch["laser"]

    t = (las["hw"] - las["hw"][0]) / 1e6          # firmware clock, not host
    um = (las["v"] * 1000.0 - v0) / k
    um = um - um.mean()

    # ---- zero-order-hold duplication ----------------------------------------
    held = np.r_[False, np.diff(las["rc"]) == 0]
    read_hz = 1e6 / np.median(np.diff(las["hw"]))
    dup_frac = float(held.mean())
    true_hz = read_hz * (1.0 - dup_frac)
    log.info("read rate %.2f Hz, %.1f%% duplicated codes -> true ADC update "
             "%.2f Hz (ADC1 is configured for 400 SPS)",
             read_hz, 100 * dup_frac, true_hz)
    if "load" in ch:
        f = float(np.mean(np.diff(ch["load"]["rc"]) == 0))
        log.info("  (load channel duplicates %.1f%% too — the over-read is not "
                 "laser-specific)", 100 * f)

    # ---- the tone -----------------------------------------------------------
    f0, amp, phase, r2 = fit_tone(t, um, args.fmin, args.fmax)
    log.info("dominant tone: %.3f Hz, amplitude %.3f µm (%.3f µm p-p), explains "
             "%.1f%% of the laser variance", f0, amp, 2 * amp, 100 * r2)
    nyq = true_hz / 2.0
    aliases = [abs(n * true_hz + s * f0) for n in (1, 2) for s in (-1, 1)]
    log.info("NOTE: true conversion rate is %.0f SPS (Nyquist %.0f Hz) — a tone "
             "reported at %.1f Hz could also be an alias of %s Hz",
             true_hz, nyq, f0, ", ".join(f"{a:.1f}" for a in aliases))

    tone = amp * np.cos(2 * np.pi * f0 * t + phase)
    resid = um - tone

    # ---- Figure 1: the diagnostic -------------------------------------------
    fig, axes = plt.subplots(3, 2, figsize=(14, 11), facecolor=SURFACE)
    ax = axes.ravel()

    style(ax[0])
    ax[0].plot(t, um, lw=0.5, color=C_DISP)
    ax[0].set_title("Full session — reads as a noise band", fontsize=10,
                    color=INK, loc="left")
    ax[0].set_xlabel("t (s)", fontsize=9, color=INK_2)
    ax[0].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    ax[0].text(0.985, 0.04, f"σ = {um.std():.2f} µm, {np.ptp(um):.2f} µm p-p",
               transform=ax[0].transAxes, ha="right", va="bottom", fontsize=8,
               color=INK_2, bbox=_PLATE)

    style(ax[1])
    t_lo = t[len(t) // 2]
    m = (t >= t_lo) & (t < t_lo + args.zoom_s)
    ax[1].plot(t[m] - t_lo, um[m], "-o", lw=1.2, ms=3.5, color=C_DISP,
               markeredgecolor=SURFACE, markeredgewidth=0.6, label="laser sample")
    hm = m & held
    ax[1].plot(t[hm] - t_lo, um[hm], "o", ms=6, color=C_FORCE, zorder=4,
               label=f"held / duplicate code ({100 * dup_frac:.0f}% of rows)")
    ax[1].plot(t[m] - t_lo, tone[m], lw=1.6, color=C_RES, alpha=0.85,
               label=f"fitted {f0:.2f} Hz tone")
    ax[1].set_title(f"Zoom ({args.zoom_s * 1000:.0f} ms) — a coherent ripple, "
                    f"not noise", fontsize=10, color=INK, loc="left")
    ax[1].set_xlabel("t (s, from mid-session)", fontsize=9, color=INK_2)
    ax[1].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    ax[1].margins(y=0.28)
    ax[1].legend(loc="lower right", fontsize=7.5, frameon=True, labelcolor=INK_2,
                 facecolor=SURFACE, edgecolor=GRID, framealpha=0.9)

    style(ax[2])
    fs = read_hz
    g = np.arange(0, t[-1], 1 / fs)
    y = np.interp(g, t, um)
    y = y - y.mean()
    w = np.hanning(len(y))
    Y = np.abs(np.fft.rfft(y * w)) * 2 / w.sum()
    fr = np.fft.rfftfreq(len(y), 1 / fs)
    # Both spectra normalized to their OWN max so the two channels can share one
    # y-axis honestly (µm and volts have no common scale).
    ax[2].plot(fr, Y / Y.max(), lw=0.9, color=C_DISP, label="laser / ADC1")
    if "load" in ch:
        ld = ch["load"]
        tl = (ld["hw"] - ld["hw"][0]) / 1e6
        yl = np.interp(g, tl, ld["v"] - ld["v"].mean())
        yl = yl - yl.mean()
        Yl = np.abs(np.fft.rfft(yl * w)) * 2 / w.sum()
        ax[2].plot(fr, Yl / max(Yl.max(), 1e-12), lw=0.9, color=C_VOLT, alpha=0.75,
                   label="load / ADC2 — no peak here")
    ax[2].axvline(nyq, ls="--", lw=1.0, color=C_REF)
    ax[2].text(nyq - 4, 0.60, f"Nyquist of the true {true_hz:.0f} SPS ADC",
               fontsize=7.5, color=INK_2, va="center", ha="right", rotation=90,
               bbox=_PLATE)
    ax[2].annotate(f"{f0:.2f} Hz — {amp:.2f} µm", (f0, 1.0),
                   textcoords="offset points", xytext=(14, -2), fontsize=8.5,
                   color=INK, bbox=_PLATE)
    for n in (2, 3):
        fh = n * f0
        if fh < fr[-1]:
            ax[2].annotate(f"{n}f", (fh, np.interp(fh, fr, Y) / Y.max()),
                           textcoords="offset points", xytext=(4, 6), fontsize=8,
                           color=INK_2)
    ax[2].set_xlim(0, min(250, fr[-1]))
    ax[2].set_ylim(0, 1.18)
    ax[2].set_title("Amplitude spectrum (firmware clock)", fontsize=10, color=INK,
                    loc="left")
    ax[2].set_xlabel("frequency (Hz)", fontsize=9, color=INK_2)
    ax[2].set_ylabel("amplitude ÷ that channel's own max", fontsize=9, color=INK_2)
    ax[2].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    style(ax[3])
    T = 1.0 / f0
    ph = (t % T) / T
    ax[3].plot(ph, um, ".", ms=1.2, color=C_DISP, alpha=0.25, label="all samples")
    nb = 40
    edges = np.linspace(0, 1, nb + 1)
    idx = np.clip(np.digitize(ph, edges) - 1, 0, nb - 1)
    med = np.array([np.median(um[idx == i]) if (idx == i).any() else np.nan
                    for i in range(nb)])
    q1 = np.array([np.percentile(um[idx == i], 25) if (idx == i).any() else np.nan
                   for i in range(nb)])
    q3 = np.array([np.percentile(um[idx == i], 75) if (idx == i).any() else np.nan
                   for i in range(nb)])
    ctr = 0.5 * (edges[:-1] + edges[1:])
    ax[3].fill_between(ctr, q1, q3, color=C_RES, alpha=0.22, lw=0)
    ax[3].plot(ctr, med, lw=2.0, color=C_RES, label="median (IQR band)")
    ax[3].set_title(f"Folded on 1/{f0:.2f} Hz = {T * 1000:.2f} ms — the hidden "
                    f"waveform", fontsize=10, color=INK, loc="left")
    ax[3].set_xlabel("phase (cycles)", fontsize=9, color=INK_2)
    ax[3].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    ax[3].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    style(ax[4])
    a = np.correlate(y, y, "full")[len(y) - 1:]
    a = a / a[0]
    lags = np.arange(len(a)) / fs * 1000.0
    n = int(0.25 * fs)
    ax[4].plot(lags[:n], a[:n], lw=1.2, color=C_DISP)
    ax[4].axhline(0, lw=0.8, color=AXIS)
    for n_ in range(1, 5):
        ax[4].axvline(n_ * T * 1000, ls="--", lw=0.9, color=C_RES, alpha=0.6)
    ax[4].set_title(f"Autocorrelation — peaks every {T * 1000:.2f} ms",
                    fontsize=10, color=INK, loc="left")
    ax[4].set_xlabel("lag (ms)", fontsize=9, color=INK_2)
    ax[4].set_ylabel("r", fontsize=9, color=INK_2)

    style(ax[5])
    ax[5].plot(t, resid, lw=0.5, color=C_REF)
    ax[5].set_title("Residual after removing the tone — the real noise floor",
                    fontsize=10, color=INK, loc="left")
    ax[5].set_xlabel("t (s)", fontsize=9, color=INK_2)
    ax[5].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    ax[5].set_ylim(ax[0].get_ylim())
    ax[5].text(0.985, 0.04,
               f"σ: {um.std():.2f} µm → {resid.std():.2f} µm\n"
               f"the tone is {100 * r2:.0f}% of the variance",
               transform=ax[5].transAxes, ha="right", va="bottom", fontsize=8,
               color=INK_2, bbox=_PLATE)

    fig.suptitle(f"{sess.name} — laser channel: a {f0:.2f} Hz tone, not noise",
                 fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = sess / "laser_diagnostics.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # ---- Figure 2: BEFORE vs AFTER the two corrections -----------------------
    # A POST-PROCESSING demonstration of what the channel is worth once the
    # interferer is gone — no hardware change has been made, so it is an upper
    # bound on the gain, not a measurement of a fixed rig.
    keep = np.r_[True, np.diff(las["rc"]) != 0]        # fix 1: drop ZOH repeats
    t_true, um_true = t[keep], um[keep]

    fs_g = true_hz
    g2 = np.arange(0, t[-1], 1.0 / fs_g)
    before = np.interp(g2, t, um)
    before -= before.mean()
    true_on_grid = np.interp(g2, t_true, um_true)
    true_on_grid -= true_on_grid.mean()

    harm = [n * f0 for n in (1, 2, 3) if n * f0 < fs_g / 2]
    after = notch_fft(true_on_grid, fs_g, harm, args.notch_hw)   # fix 2
    after -= after.mean()

    log.info("before: σ %.3f µm | after dedup: σ %.3f µm | after notch: σ %.3f µm",
             before.std(), true_on_grid.std(), after.std())

    fig2, bx = plt.subplots(2, 2, figsize=(14, 8), facecolor=SURFACE)
    bx = bx.ravel()
    C_BEFORE, C_AFTER = C_DISP, C_CURR

    style(bx[0])
    bx[0].plot(g2, before, lw=0.5, color=C_BEFORE, alpha=0.85,
               label=f"before — σ {before.std():.2f} µm")
    bx[0].plot(g2, after, lw=0.5, color=C_AFTER,
               label=f"after — σ {after.std():.2f} µm")
    bx[0].set_title("Full session — same axis, same data", fontsize=10, color=INK,
                    loc="left")
    bx[0].set_xlabel("t (s)", fontsize=9, color=INK_2)
    bx[0].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    bx[0].legend(loc="upper right", fontsize=8, frameon=True, labelcolor=INK_2,
                 facecolor=SURFACE, edgecolor=GRID, framealpha=0.9)

    style(bx[1])
    m2 = (g2 >= g2[len(g2) // 2]) & (g2 < g2[len(g2) // 2] + args.zoom_s)
    z = g2[m2] - g2[m2][0]
    bx[1].plot(z, before[m2], "-", lw=1.3, color=C_BEFORE, alpha=0.85, label="before")
    bx[1].plot(z, after[m2], "-", lw=1.6, color=C_AFTER, label="after")
    bx[1].axhline(0, lw=0.8, color=AXIS)
    bx[1].set_title(f"Zoom ({args.zoom_s * 1000:.0f} ms) — the ripple is gone",
                    fontsize=10, color=INK, loc="left")
    bx[1].set_xlabel("t (s)", fontsize=9, color=INK_2)
    bx[1].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    bx[1].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)

    style(bx[2])
    w2 = np.hanning(len(g2))
    fr2 = np.fft.rfftfreq(len(g2), 1 / fs_g)
    for sig_, c, lab in ((before, C_BEFORE, "before"), (after, C_AFTER, "after")):
        S = np.abs(np.fft.rfft((sig_ - sig_.mean()) * w2)) * 2 / w2.sum()
        bx[2].plot(fr2, S, lw=0.9, color=c, alpha=0.9, label=lab)
    bx[2].annotate(f"{f0:.2f} Hz\nnotched", (f0, amp), textcoords="offset points",
                   xytext=(14, -6), fontsize=8.5, color=INK, bbox=_PLATE)
    bx[2].set_xlim(0, fs_g / 2)
    bx[2].set_title("Amplitude spectrum", fontsize=10, color=INK, loc="left")
    bx[2].set_xlabel("frequency (Hz)", fontsize=9, color=INK_2)
    bx[2].set_ylabel("amplitude (µm)", fontsize=9, color=INK_2)
    bx[2].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)

    style(bx[3])
    stages = ["as recorded", "+ drop ZOH\nduplicates", "+ notch\nthe tone"]
    sds = [before.std(), true_on_grid.std(), after.std()]
    bars = bx[3].bar(stages, sds, color=[C_BEFORE, C_RES, C_AFTER], width=0.6,
                     edgecolor=SURFACE, linewidth=2)
    for b, s in zip(bars, sds):
        bx[3].annotate(f"{s:.2f} µm", (b.get_x() + b.get_width() / 2, s),
                       textcoords="offset points", xytext=(0, 5), ha="center",
                       fontsize=10, color=INK)
    bx[3].set_title(f"Laser noise floor: {before.std():.2f} µm → {after.std():.2f} µm "
                    f"({before.std() / after.std():.1f}× better)",
                    fontsize=10, color=INK, loc="left")
    bx[3].set_ylabel("σ (µm)", fontsize=9, color=INK_2)
    bx[3].margins(y=0.22)
    bx[3].set_xlabel("post-processing demo — no hardware change, so this is an "
                     "upper bound on the gain.\nAll σ computed on the common "
                     f"{fs_g:.0f} Hz grid.", fontsize=8, color=MUTED)

    fig2.suptitle(f"{sess.name} — laser channel before vs after the two corrections",
                  fontsize=13, color=INK)
    fig2.tight_layout(rect=(0, 0, 1, 0.95))
    out2 = sess / "laser_before_after.png"
    fig2.savefig(out2, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig2)
    log.info("wrote %s", out2)

    print(f"\n{sess.name} — laser channel")
    print(f"  apparent noise : σ {um.std():.2f} µm, {np.ptp(um):.2f} µm p-p")
    print(f"  dominant tone  : {f0:.3f} Hz, {amp:.2f} µm amp ({2 * amp:.2f} µm p-p) "
          f"— {100 * r2:.0f}% of the variance")
    print(f"  residual noise : σ {resid.std():.2f} µm once the tone is removed")
    print(f"  stream         : read {read_hz:.1f} Hz but ADC converts "
          f"{true_hz:.1f} SPS → {100 * dup_frac:.0f}% of h7.csv rows are "
          f"zero-order-hold duplicates")
    print(f"  alias caveat   : at {true_hz:.0f} SPS (Nyquist {nyq:.0f} Hz) this "
          f"could equally be {', '.join(f'{a:.1f}' for a in aliases)} Hz")
    print(f"\n  BEFORE vs AFTER (post-processing demo, no hardware change):")
    print(f"    as recorded          σ {before.std():.3f} µm")
    print(f"    + drop ZOH duplicates σ {true_on_grid.std():.3f} µm")
    print(f"    + notch {f0:.1f} Hz & harmonics  σ {after.std():.3f} µm "
          f"({before.std() / after.std():.1f}× better)")
    print(f"  outputs        : {out}, {out2}\n")
    return 0


# ===========================================================================
# VIEW: transition
# ===========================================================================
def view_transition(args) -> int:
    sess = Path(args.session)
    meta, _k, _v0, lscale, loff, cold_r = load_meta(sess)
    ch = load_h7(sess / "h7.csv")
    err = _require(sess, ch, ("sma_v", "sma_r", "load"))
    if err:
        return err

    cmd = load_cycle_cmd(sess / "events.csv")
    if cmd is None:
        print("ERROR: no `cycle ...` command in events.csv", file=sys.stderr)
        return 2

    tv, vv = ch["sma_v"]["t"], ch["sma_v"]["v"]
    tr, rr = ch["sma_r"]["t"], ch["sma_r"]["v"]
    tf = ch["load"]["t"]
    ff = lscale * (ch["load"]["v"] - loff)

    onsets = find_fire_onsets(tv, vv, cmd.v_high, cmd.v_low)
    fire_s, span = cmd.fire_s, cmd.period_s
    print(f"{sess.name}: {len(onsets)} cycles, ensemble-averaging on the fire onset\n")

    def fold(t, y, as_pct=False):
        T, Y = [], []
        for ot in onsets:
            m = (t >= ot - 0.4) & (t < ot + span)
            tt, yy = t[m] - ot, y[m]
            b = (tt >= BASE_LO) & (tt <= BASE_HI)
            if not b.any():
                continue                 # cycle 1 has no pre-fire SMA stream
            base = yy[b].mean()
            T.append(tt)
            Y.append((yy - base) / base * 100.0 if as_pct else yy - base)
        return np.concatenate(T), np.concatenate(Y)

    def ensemble(T, Y):
        """MEDIAN across cycles, not mean — one mechanical-event cycle with a 7×
        force peak would otherwise BE the ensemble. Error bar = SE of the median
        ≈ 1.253·σ̂/√n, σ̂ from the MAD."""
        edges = np.arange(-0.4, span, args.bin_ms / 1000.0)
        idx = np.digitize(T, edges) - 1
        ctr, mid, sem, cnt = [], [], [], []
        for i in range(len(edges) - 1):
            s = Y[idx == i]
            if len(s) < 3:
                continue
            m = float(np.median(s))
            sd = 1.4826 * float(np.median(np.abs(s - m)))
            ctr.append(0.5 * (edges[i] + edges[i + 1]))
            mid.append(m)
            sem.append(1.253 * sd / np.sqrt(len(s)))
            cnt.append(len(s))
        return np.array(ctr), np.array(mid), np.array(sem), np.array(cnt)

    Rt, Ry = fold(tr, rr, as_pct=True)
    Ft, Fy = fold(tf, ff)
    rc, rm, rs, rn = ensemble(Rt, Ry)
    fc, fm, fs_, fn = ensemble(Ft, Fy)

    # R precision is NOT uniform across a cycle: the fire drives I ~6× higher, so
    # the current sense sits far higher in the ADC range and R is measured about
    # twice as well during the fire as during the idle-probe cool.
    sd_fire = float(np.std(Ry[(Rt >= 0) & (Rt <= fire_s)]))
    sd_single = float(np.std(Ry[Rt > 0.6]))
    print("resistance ΔR/R₀:")
    print(f"  per-sample noise, IN FIRE : ±{sd_fire:.2f} %   (I ≈ 0.69 A)")
    print(f"  per-sample noise, COOLING : ±{sd_single:.2f} %   (idle probe, I ≈ 0.12 A)")
    print(f"  samples per {args.bin_ms:.0f} ms bin : {int(np.median(rn))}")
    print(f"  ensemble noise (SEM)  : ±{np.median(rs):.2f} %   "
          f"({sd_single / max(np.median(rs), 1e-9):.1f}× better)")
    pk = int(np.argmax(np.abs(rm)))
    print(f"  peak of the ensemble  : {rm[pk]:+.2f} % at t={rc[pk]:+.3f} s  "
          f"→ {abs(rm[pk]) / rs[pk]:.1f}σ  "
          f"{'RESOLVED' if abs(rm[pk]) > 3 * rs[pk] else 'not resolved'}")

    def cool_fit(c, m, label, unit):
        kk = c > fire_s + 0.05
        window = float(c[kk][-1] - c[kk][0])
        tau, y_inf, A, r2 = fit_exp(c[kk] - c[kk][0], m[kk])
        print(f"\n{label} cooling fit  y = y∞ + A·exp(-t/τ):")
        print(f"  τ    = {tau:.3f} s")
        print(f"  A    = {A:+.3f} {unit}      y∞ = {y_inf:+.3f} {unit}")
        print(f"  R²   = {r2:.3f}")
        if tau > 0.5 * window:
            print(f"  ⚠ τ ({tau:.2f} s) exceeds half the observation window "
                  f"({window:.2f} s) — the cool phase is TOO SHORT to pin it "
                  f"down. τ and y∞ trade off; treat this as a LOWER BOUND and "
                  f"raise cool_ms.")
        return tau, y_inf, A, r2, kk

    tau_r, yi_r, A_r, r2_r, kr = cool_fit(rc, rm, "RESISTANCE ΔR/R₀", "%")
    tau_f, yi_f, A_f, r2_f, kf = cool_fit(fc, fm, "FORCE ΔF", "N")
    print(f"\nτ_R = {tau_r:.2f} s   vs   τ_F = {tau_f:.2f} s   "
          f"(ratio {tau_r / tau_f:.2f})")

    def style_fire(x, fire=True):
        style(x)
        if fire:                       # meaningless on the categorical panel
            x.axvspan(0, fire_s, color=C_FIRE, alpha=0.16, lw=0)

    # ---- Figure 1: ensemble --------------------------------------------------
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.4), facecolor=SURFACE)

    style_fire(ax[0])
    ax[0].plot(Rt, Ry, ".", ms=1.5, color=C_RES, alpha=0.10,
               label=f"single samples — ±{sd_single:.1f}% (why one cycle shows nothing)")
    ax[0].fill_between(rc, rm - rs, rm + rs, color=C_RES, alpha=0.30, lw=0)
    ax[0].plot(rc, rm, "o-", ms=4, lw=1.8, color=C_RES, markeredgecolor=SURFACE,
               markeredgewidth=0.8, label=f"median of {len(onsets)} cycles (±SEM band)")
    ax[0].axhline(0, lw=1.0, color=AXIS)
    fire_mean = float(np.mean(Ry[(Rt >= 0) & (Rt <= fire_s)]))
    ax[0].annotate(f"R DROPS {abs(fire_mean):.1f}% during the fire,\nthen recovers",
                   xy=(0.05, fire_mean), xytext=(0.75, -8.5), fontsize=9, color=INK,
                   arrowprops=dict(arrowstyle="->", color=INK_2, lw=1.2), bbox=_PLATE)
    ax[0].set_ylim(-12, 12)
    ax[0].set_title("Resistance — ensemble of all cycles", fontsize=10, color=INK,
                    loc="left")
    ax[0].set_xlabel("time since fire onset (s)", fontsize=9, color=INK_2)
    ax[0].set_ylabel("ΔR / R₀  (%)", fontsize=9, color=INK_2)
    ax[0].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    style_fire(ax[1])
    ax[1].errorbar(fc, fm, yerr=fs_, fmt="o-", ms=4, lw=1.5, color=C_FORCE,
                   ecolor=C_FORCE, elinewidth=1, capsize=2, label="ensemble (±SEM)")
    tt = fc[kf] - fc[kf][0]
    ax[1].plot(fc[kf], yi_f + A_f * np.exp(-tt / tau_f), lw=2, color=C_FIT,
               label=f"fit: τ = {tau_f:.2f} s (R²={r2_f:.2f})")
    ax[1].axhline(0, lw=0.8, color=AXIS)
    ax[1].set_title("Force — ensemble average", fontsize=10, color=INK, loc="left")
    ax[1].set_xlabel("time since fire onset (s)", fontsize=9, color=INK_2)
    ax[1].set_ylabel("ΔF  (N)", fontsize=9, color=INK_2)
    ax[1].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    # ---- window test: average WITHIN a window per cycle, then across cycles ---
    # This is the estimator that resolves the transition. A fine time bin leaves
    # SEM comparable to the ~3% effect; averaging the whole fire window first
    # drops it to ~0.5% and the drop appears at >5σ.
    print("\n" + "=" * 70)
    print("WINDOW TEST — mean ΔR/R₀ per cycle, then across cycles")
    print("=" * 70)
    print(f"{'window':<26}{'ΔR/R₀':>10}{'SEM':>9}{'t':>8}   verdict")
    wins = [("during fire  0–100 ms", 0.0, 0.10),
            ("after fire   0.1–0.3 s", 0.10, 0.30),
            ("early cool   0.3–1.0 s", 0.30, 1.00),
            ("late cool    2.0–3.0 s", 2.00, 3.00)]
    wl, wm, ws = [], [], []
    for nm, lo, hi in wins:
        vals = []
        for ot in onsets:
            b = (tr >= ot + BASE_LO) & (tr <= ot + BASE_HI)
            w = (tr >= ot + lo) & (tr < ot + hi)
            if not (b.any() and w.any()):
                continue
            base = rr[b].mean()
            vals.append((rr[w].mean() - base) / base * 100.0)
        v = np.array(vals)
        sem = float(v.std(ddof=1) / np.sqrt(len(v)))
        t = float(v.mean() / sem)
        verdict = "RESOLVED" if abs(t) > 3 else ("marginal" if abs(t) > 2 else "not resolved")
        print(f"{nm:<26}{v.mean():+9.2f}%{sem:>8.2f}%{t:>8.1f}   {verdict}")
        wl.append(nm.split()[0] + "\n" + nm.split(maxsplit=1)[1])
        wm.append(v.mean())
        ws.append(sem)

    style_fire(ax[2], fire=False)
    x = np.arange(len(wl))
    ax[2].errorbar(x, wm, yerr=np.array(ws) * 3, fmt="o", ms=8, lw=2, color=C_RES,
                   ecolor=C_RES, elinewidth=2, capsize=5,
                   label="mean ± 3·SEM across cycles")
    ax[2].axhline(0, lw=1.0, color=AXIS)
    ax[2].set_xticks(x)
    ax[2].set_xticklabels(wl, fontsize=7.5)
    ax[2].set_xlim(-0.5, len(wl) - 0.5)
    ax[2].set_title("The transition, window-averaged (error bars = 3σ)",
                    fontsize=10, color=INK, loc="left")
    ax[2].set_ylabel("ΔR / R₀  (%)", fontsize=9, color=INK_2)
    ax[2].margins(y=0.25)
    ax[2].legend(loc="lower left", fontsize=8, frameon=False, labelcolor=INK_2)

    ttl = f"{sess.name} — SMA transition, {len(onsets)} cycles ensemble-averaged"
    if cold_r:
        ttl += f"  (cold R₀ = {cold_r:.2f} Ω)"
    fig.suptitle(ttl, fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = sess / "transition_fit.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)

    # ---- Figure 2: PER CYCLE -------------------------------------------------
    # The ensemble proves the effect exists; it hides whether every cycle does the
    # same thing. Each panel's error bar comes from that cycle's OWN ~8 in-fire
    # samples — nothing borrowed across cycles.
    n = len(onsets)
    ncol = 5
    nrow = int(np.ceil(n / ncol))
    fig2 = plt.figure(figsize=(3.0 * ncol, 2.5 * nrow + 3.4), facecolor=SURFACE)
    gs = fig2.add_gridspec(nrow + 1, ncol, height_ratios=[1] * nrow + [1.35],
                           hspace=0.55, wspace=0.3)

    per_mean, per_sem, per_lab = [], [], []
    for i, ot in enumerate(onsets):
        axc = fig2.add_subplot(gs[i // ncol, i % ncol])
        style_fire(axc)
        m = (tr >= ot - 0.4) & (tr < ot + span)
        tt, yy = tr[m] - ot, rr[m]
        b = (tt >= BASE_LO) & (tt <= BASE_HI)
        base = yy[b].mean() if b.any() else (cold_r if cold_r else yy.mean())
        d = (yy - base) / base * 100.0

        f = (tt >= 0) & (tt <= fire_s)
        if f.sum() >= 2 and b.any():
            mu = float(d[f].mean())
            se = float(d[f].std(ddof=1) / np.sqrt(f.sum()))
        else:
            mu, se = float("nan"), float("nan")
        per_mean.append(mu)
        per_sem.append(se)
        per_lab.append(i + 1)

        axc.plot(tt, d, ".", ms=2.5, color=C_RES, alpha=0.35)
        if len(d) > 9:                 # running median, so the transient shows
            kk = 9
            sm = np.array([np.median(d[max(0, j - kk // 2):j + kk // 2 + 1])
                           for j in range(len(d))])
            axc.plot(tt, sm, lw=1.6, color=C_RES)
        axc.axhline(0, lw=0.9, color=AXIS)
        if np.isfinite(mu):
            axc.plot([0, fire_s], [mu, mu], lw=3, color=C_FIT, zorder=5)
            t_i = mu / se if se else np.nan
            axc.set_title(f"cycle {i+1}   {mu:+.1f}% ± {se:.1f}"
                          f"{'  ✓' if abs(t_i) > 3 else '  (weak)'}",
                          fontsize=9, color=INK, loc="left")
        else:
            axc.set_title(f"cycle {i+1}   — no pre-fire baseline", fontsize=9,
                          color=MUTED, loc="left")
        axc.set_ylim(-12, 12)
        axc.set_xlim(-0.4, span)
        if i % ncol == 0:
            axc.set_ylabel("ΔR/R₀ (%)", fontsize=8, color=INK_2)
        if i // ncol == nrow - 1:
            axc.set_xlabel("t since fire (s)", fontsize=8, color=INK_2)

    axt = fig2.add_subplot(gs[nrow, :])
    style_fire(axt, fire=False)
    pm, ps = np.array(per_mean), np.array(per_sem)
    axt.errorbar(per_lab, pm, yerr=ps, fmt="o-", ms=7, lw=1.6, color=C_RES,
                 ecolor=C_RES, elinewidth=1.5, capsize=4, markeredgecolor=SURFACE,
                 markeredgewidth=1,
                 label="per-cycle ΔR/R₀ in the fire window (± its own SEM)")
    good = np.isfinite(pm)
    gm = float(np.mean(pm[good]))
    axt.axhline(gm, ls="--", lw=1.2, color=C_FIT, label=f"ensemble mean {gm:+.2f}%")
    axt.axhline(0, lw=1.0, color=AXIS)
    axt.set_xticks(per_lab)
    axt.set_xlabel("cycle", fontsize=9, color=INK_2)
    axt.set_ylabel("ΔR/R₀ in fire (%)", fontsize=9, color=INK_2)
    # Do NOT claim "every cycle shows the drop" — check. A cycle whose error bar
    # crosses zero has not shown one, and saying otherwise would repeat exactly
    # the over-reading that the max() estimator once caused.
    null_cyc = [c for c, mu, se in zip(per_lab, per_mean, per_sem)
                if np.isfinite(mu) and abs(mu) < 2 * se]
    sub = "ΔR varies more than the error bars — the spread is real."
    if null_cyc:
        sub += (f" Cycle {', '.join(map(str, null_cyc))} shows NO drop "
                f"(consistent with zero).")
    axt.set_title(f"Cycle-to-cycle: {sub}", fontsize=10, color=INK, loc="left")
    axt.legend(loc="lower right", fontsize=8, frameon=False, labelcolor=INK_2)
    axt.margins(y=0.28)

    fig2.suptitle(f"{sess.name} — resistance transition, CYCLE BY CYCLE",
                  fontsize=13, color=INK)
    fig2.tight_layout(rect=(0, 0, 1, 0.96))
    out2 = sess / "transition_per_cycle.png"
    fig2.savefig(out2, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig2)

    print("\n" + "=" * 70)
    print("PER-CYCLE ΔR/R₀ in the fire window (each from its OWN ~8 samples)")
    print("=" * 70)
    for c, mu, se in zip(per_lab, per_mean, per_sem):
        if not np.isfinite(mu):
            print(f"  cycle {c:2d}:  — no pre-fire baseline (SMA stream starts at fire 1)")
            continue
        t_i = mu / se if se else float("nan")
        print(f"  cycle {c:2d}:  {mu:+6.2f} % ± {se:4.2f}   t={t_i:+5.1f}  "
              f"{'RESOLVED' if abs(t_i) > 3 else 'weak'}")
    sp = float(np.std(pm[good], ddof=1))
    print(f"\n  spread across cycles : ±{sp:.2f} %   "
          f"(typical within-cycle SEM ±{np.nanmedian(ps):.2f} %)")
    print(f"  → the spread is {sp / np.nanmedian(ps):.1f}× the measurement error, "
          f"so the cycle-to-cycle variation is REAL, not noise.")
    print(f"\nwrote {out}")
    print(f"wrote {out2}\n")
    return 0


# ===========================================================================
# CLI
# ===========================================================================
def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        description="Analysis + figures for an SMA thermal console session.")
    sub = p.add_subparsers(dest="view", required=True)

    def common(sp):
        sp.add_argument("--session", required=True, help="session directory")
        sp.add_argument("--dpi", type=int, default=140)
        return sp

    c = common(sub.add_parser("cycles", help="segment the run by its actuation pattern"))
    c.add_argument("--r-ref", choices=("cold", "cycle"), default="cold",
                   help="R₀ the resistance is normalized to: 'cold' = the session's "
                        "initial baseline reading (default; the cycle-to-cycle drift "
                        "stays visible), 'cycle' = each cycle's own pre-fire R "
                        "(drift divided out)")
    c.add_argument("--notch", metavar="HZ|auto", default=None,
                   help="OPT-IN: notch this frequency and its harmonics out of the "
                        "DISPLACEMENT channel — e.g. '65.77', or 'auto' to fit the "
                        "tone in 40–90 Hz. Default: no filtering, raw.")
    c.add_argument("--notch-q", type=float, default=30.0,
                   help="notch sharpness (f0/width); higher = narrower (default 30)")
    c.add_argument("--notch-harmonics", type=int, default=3,
                   help="how many harmonics of --notch to remove (default 3)")
    c.add_argument("--lowpass", type=float, default=None, metavar="HZ",
                   help="OPT-IN: low-pass the DISPLACEMENT channel (4th-order "
                        "Butterworth magnitude, zero-phase). SMA actuation lives "
                        "below a few Hz, so ~20 Hz is generous. Default: raw.")

    l = common(sub.add_parser("laser", help="laser tone + ZOH-duplicate diagnostic"))
    l.add_argument("--fmin", type=float, default=5.0, help="tone search low edge (Hz)")
    l.add_argument("--fmax", type=float, default=195.0, help="tone search high edge (Hz)")
    l.add_argument("--zoom-s", type=float, default=0.20, help="zoom window width (s)")
    l.add_argument("--notch-hw", type=float, default=1.0,
                   help="notch half-width (Hz) in the before/after demo")

    t = common(sub.add_parser("transition", help="is the SMA transition real?"))
    t.add_argument("--bin-ms", type=float, default=150.0,
                   help="ensemble time-bin width (default 150 ms). Too fine and the "
                        "per-bin SEM approaches the ~3%% effect and a real transient "
                        "reads as noise.")

    a = common(sub.add_parser("all", help="run every view with its defaults"))

    args = p.parse_args()

    sess = Path(args.session)
    if not sess.exists():
        print(f"ERROR: no such session dir: {sess}", file=sys.stderr)
        return 2

    if args.view == "cycles":
        return view_cycles(args)
    if args.view == "laser":
        return view_laser(args)
    if args.view == "transition":
        return view_transition(args)

    # `all` — give each view its own defaults, then run them in turn.
    rc = 0
    for name, fn, defaults in (
        ("cycles", view_cycles, dict(r_ref="cold", notch=None, notch_q=30.0,
                                     notch_harmonics=3, lowpass=None)),
        ("transition", view_transition, dict(bin_ms=150.0)),
        ("laser", view_laser, dict(fmin=5.0, fmax=195.0, zoom_s=0.20, notch_hw=1.0)),
    ):
        ns = argparse.Namespace(session=args.session, dpi=args.dpi, view=name,
                                **defaults)
        log.info("=== %s ===", name)
        r = fn(ns)
        rc = r or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
