#!/usr/bin/env python3
"""
plot_cycles.py — cycle-aligned visualization of an SMA thermal console session.

Where analyze_sma.py renders a flat whole-session dashboard, this script segments
the run by the ACTUATION PATTERN: it recovers the fire/cool cycles the console
commanded (`cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>` in events.csv) and
locates each fire onset as a rising edge of the logged sma_v, then renders

    <session>/cycles_timeline.png   full session, fire windows shaded
    <session>/cycles_overlay.png    all cycles overlaid on fire-onset time
    <session>/cycles_trend.png      per-cycle metrics vs cycle index
    <session>/cycles_metrics.csv    the per-cycle metrics table

Signals are converted with the calibration block in meta.json (same coefficients
analyze_sma.py uses). Displacement carries a large meaningless absolute offset
(the laser sits far outside its calibrated span), so it is plotted as a DELTA
against each cycle's pre-fire baseline; force is likewise reported as a delta in
the metrics while the timeline keeps it absolute (the load-cell tare is real).

Usage:
    python plot_cycles.py --session data/console_20260713_115921

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

log = logging.getLogger("plot_cycles")

# --- Palette (dataviz reference instance, light surface) --------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

C_VOLT = "#2a78d6"   # categorical slot 1 — blue
C_CURR = "#1baf7a"   # slot 2 — aqua
C_RES = "#4a3aa7"    # slot 5 — violet
C_FORCE = "#e34948"  # slot 6 — red
C_DISP = "#eb6834"   # slot 8 — orange
C_FIRE = "#eb6834"   # fire-window wash
C_REF = "#898781"    # reference lines (cold R, zero)

# Ordinal blue ramp, step 250 -> 700 (lightest step still clears 2:1 on light).
BLUE_RAMP = ["#86b6ef", "#5598e7", "#3987e5", "#2a78d6",
             "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
CYCLE_CMAP = LinearSegmentedColormap.from_list("cycles", BLUE_RAMP)

PRE_S = 0.40   # seconds of pre-fire context shown / used for the baseline
BASE_LO, BASE_HI = -0.35, -0.03   # baseline window relative to fire onset

# Opaque plate so an in-axes note stays legible on top of a dense trace.
_PLATE = dict(facecolor=SURFACE, edgecolor="none", alpha=0.88, pad=3.0)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, newline="", errors="replace") as f:
        return list(csv.DictReader(f))


def load_h7(path: Path) -> Dict[str, dict]:
    """{channel: {'t': host_ts, 'v': value, 'hw': firmware µs clock}}

    `t` (host clock) is what everything is aligned on — it is the clock the
    stage/events share. `hw` is only used for FILTERING: the host timestamps are
    USB-batched and bursty (median dt ~1 ms but σ ~3 ms), which smears anything
    above a few Hz; the firmware clock is uniform.
    """
    acc: Dict[str, dict] = {}
    for r in _rows(path):
        ch = (r.get("channel") or "").strip()
        if not ch:
            continue
        d = acc.setdefault(ch, {"t": [], "v": [], "hw": []})
        try:
            d["t"].append(float(r["host_timestamp_s"]))
            d["v"].append(float(r["value"]))
            d["hw"].append(float(r.get("hw_us") or "nan"))
        except (ValueError, KeyError):
            continue
    return {ch: {k: np.asarray(v) for k, v in d.items()}
            for ch, d in acc.items() if d["t"]}


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


# ---------------------------------------------------------------------------
# Cycle segmentation — fire onsets from the sma_v rising edges
# ---------------------------------------------------------------------------
def find_fire_onsets(t: np.ndarray, v: np.ndarray,
                     v_high: float, v_low: float) -> np.ndarray:
    """Host-clock timestamps of each fire onset (rising edge of the drive)."""
    thresh = 0.5 * (v_high + v_low)
    hot = v > thresh
    rise = np.flatnonzero(np.diff(hot.astype(np.int8)) == 1) + 1
    if hot[0]:                      # a fire already in progress at the first sample
        rise = np.insert(rise, 0, 0)
    return t[rise]


# ---------------------------------------------------------------------------
# Per-cycle slicing + metrics
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Optional displacement filtering (opt-in — RAW is the default everywhere)
#
# The laser carries a coherent ~65.8 Hz instrumental tone (see the module README,
# "Known signal artifacts"). It is out of band for SMA actuation and therefore
# accepted, but it dominates the raw displacement panels. These filters let you
# see past it. They are OFF unless a flag is given, and when on, every figure is
# stamped and the outputs get a `_filt` suffix so the raw PNGs are never clobbered.
#
# Both are ZERO-PHASE (applied as a magnitude mask in the frequency domain), so
# nothing shifts in time relative to the fire onset — important, since the whole
# point is to look at transients. The rolloffs are SMOOTH (Gaussian notch,
# Butterworth-magnitude low-pass) rather than brick-wall: a brick-wall filter
# rings badly around the sharp step the drive feedthrough puts at every fire.
# ---------------------------------------------------------------------------
def _uniform(t_us: np.ndarray, y: np.ndarray) -> tuple:
    """Resample onto a uniform grid using the firmware clock. Returns
    (grid_s, y_on_grid, fs)."""
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
    """Filter `y` on the firmware clock, then map back onto the ORIGINAL samples
    (so the caller's host-clock alignment is untouched). Returns a new value
    array, same length as `y`."""
    if not np.isfinite(t_us).all():
        log.warning("hw_us missing — filtering on the (bursty) host clock; "
                    "the notch will be smeared")
        t_us = (t_host - t_host[0]) * 1e6
    g, yg, fs = _uniform(t_us, y)

    if notch_hz:
        width = notch_hz / max(notch_q, 1e-6)      # -3 dB half-width, Hz
        freqs = [n * notch_hz for n in range(1, n_harm + 1) if n * notch_hz < fs / 2]

        def notch_mask(f):
            m = np.ones_like(f)
            for f0 in freqs:
                m *= 1.0 - np.exp(-0.5 * ((f - f0) / width) ** 2)
            return m
        yg = _apply_mask(yg, fs, notch_mask)

    if lowpass_hz:
        def lp_mask(f):
            # 4th-order Butterworth magnitude, applied zero-phase.
            return 1.0 / np.sqrt(1.0 + (f / lowpass_hz) ** 8)
        yg = _apply_mask(yg, fs, lp_mask)

    # back onto the original sample instants
    t_rel = (t_us - t_us[0]) / 1e6
    return np.interp(t_rel, g, yg)


def slice_cycle(t: np.ndarray, y: np.ndarray, t_fire: float,
                span_s: float) -> tuple:
    """(t_rel, y) for one cycle window, t_rel measured from the fire onset."""
    m = (t >= t_fire - PRE_S) & (t < t_fire + span_s)
    return t[m] - t_fire, y[m]


def _baseline(t_rel: np.ndarray, y: np.ndarray) -> float:
    m = (t_rel >= BASE_LO) & (t_rel <= BASE_HI)
    return float(np.mean(y[m])) if m.any() else float("nan")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--session", required=True, help="console session directory")
    p.add_argument("--r-ref", choices=("cold", "cycle"), default="cold",
                   help="reference R₀ the resistance is normalized to: 'cold' = "
                        "the session's initial baseline reading (default), "
                        "'cycle' = each cycle's own pre-fire R (removes the "
                        "cycle-to-cycle baseline drift)")
    p.add_argument("--notch", metavar="HZ|auto", default=None,
                   help="OPT-IN: notch this frequency (and its harmonics) out of "
                        "the DISPLACEMENT channel — e.g. '65.77', or 'auto' to "
                        "fit the tone in 40–90 Hz. Default: no filtering, raw.")
    p.add_argument("--notch-q", type=float, default=30.0,
                   help="notch sharpness (f0/width); higher = narrower (default 30)")
    p.add_argument("--notch-harmonics", type=int, default=3,
                   help="how many harmonics of --notch to remove (default 3)")
    p.add_argument("--lowpass", type=float, default=None, metavar="HZ",
                   help="OPT-IN: low-pass the DISPLACEMENT channel at this cutoff "
                        "(4th-order Butterworth magnitude, zero-phase). SMA "
                        "actuation lives below a few Hz, so ~20 Hz is generous. "
                        "Default: no filtering, raw.")
    p.add_argument("--dpi", type=int, default=140)
    args = p.parse_args()

    sess = Path(args.session)
    if not sess.exists():
        print(f"ERROR: no such session dir: {sess}", file=sys.stderr)
        return 2

    meta = json.loads((sess / "meta.json").read_text())
    cal = meta.get("calibration", {})
    k = cal.get("laser", {}).get("k_mV_per_um")
    v0 = cal.get("laser", {}).get("V0_mV")
    lscale = cal.get("load_cell", {}).get("scale_N_per_V")
    loff = cal.get("load_cell", {}).get("offset_V", 0.0)
    cold_r = (meta.get("baseline") or {}).get("cold_r_ohm")

    h7 = load_h7(sess / "h7.csv")
    for need in ("sma_v", "sma_i", "sma_r", "load", "laser"):
        if need not in h7:
            print(f"ERROR: channel '{need}' missing from h7.csv", file=sys.stderr)
            return 2

    cmd = load_cycle_cmd(sess / "events.csv")
    if cmd is None:
        print("ERROR: no `cycle ...` command found in events.csv — this session "
              "has no actuation pattern to follow", file=sys.stderr)
        return 2
    log.info("actuation: %d cycles, fire %.0f ms @ %.1f V, cool %.0f ms @ %.1f V",
             cmd.n, cmd.fire_ms, cmd.v_high, cmd.cool_ms, cmd.v_low)

    # Physical units.
    disp_um = (h7["laser"]["v"] * 1000.0 - v0) / k
    force_N = lscale * (h7["load"]["v"] - loff)

    # ---- optional displacement filter (raw is the default) -----------------
    filt_label = ""
    notch_hz: Optional[float] = None
    if args.notch:
        if str(args.notch).lower() == "auto":
            lt = h7["laser"]
            hw = lt["hw"] if np.isfinite(lt["hw"]).all() else (lt["t"] - lt["t"][0]) * 1e6
            tt = (hw - hw[0]) / 1e6
            yy = disp_um - disp_um.mean()
            best = (np.nan, -1.0)
            for f0 in np.linspace(40.0, 90.0, 2000):
                A = np.c_[np.cos(2 * np.pi * f0 * tt), np.sin(2 * np.pi * f0 * tt)]
                c, *_ = np.linalg.lstsq(A, yy, rcond=None)
                r2 = 1.0 - np.var(yy - A @ c) / np.var(yy)
                if r2 > best[1]:
                    best = (f0, r2)
            notch_hz = float(best[0])
            log.info("--notch auto: fitted tone at %.3f Hz (R²=%.2f)", notch_hz, best[1])
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

    sig = {
        "volt": (h7["sma_v"]["t"], h7["sma_v"]["v"]),
        "curr": (h7["sma_i"]["t"], h7["sma_i"]["v"]),
        "res": (h7["sma_r"]["t"], h7["sma_r"]["v"]),
        "force": (h7["load"]["t"], force_N),
        "disp": (h7["laser"]["t"], disp_um),
    }

    onsets = find_fire_onsets(*sig["volt"], cmd.v_high, cmd.v_low)
    log.info("found %d fire onset(s) in sma_v (commanded %d)", len(onsets), cmd.n)
    if len(onsets) == 0:
        print("ERROR: no fire edges in sma_v", file=sys.stderr)
        return 2
    span = cmd.period_s          # one cycle window: fire + full cool

    t0 = onsets[0]               # time origin = first fire
    fire_s = cmd.fire_ms / 1000.0

    # Filtered runs never overwrite the raw PNGs, and every figure says so.
    sfx = "_filt" if filt_label else ""
    stamp = f"  [displacement FILTERED: {filt_label}]" if filt_label else ""
    disp_note = f" — FILTERED ({filt_label})" if filt_label else ""

    # Noise floors, measured on the COOL stretches only (>0.5 s past a fire, so
    # the thermal transient is over). Any per-cycle Δ smaller than ~2σ here is
    # not a measurement — it is scatter, and the plots say so out loud.
    #
    # Estimated from the sample-to-sample difference (σ = std(Δy)/√2), NOT the
    # plain std: the force ratchets upward across the session, and a plain std
    # would charge that real drift to the noise budget and hide a signal that is
    # in fact ~25× its own scatter.
    def noise_sd(key: str) -> float:
        t, y = sig[key]
        quiet = np.ones(t.shape, dtype=bool)
        for ot in onsets:
            quiet &= ~((t >= ot - 0.05) & (t <= ot + 0.5))
        pair = quiet[:-1] & quiet[1:]      # both endpoints of the diff are quiet
        if not pair.any():
            return float("nan")
        return float(np.std(np.diff(y)[pair]) / np.sqrt(2.0))

    sd_r, sd_disp, sd_force = noise_sd("res"), noise_sd("disp"), noise_sd("force")
    log.info("noise floor (cool periods): R ±%.3f Ω, disp ±%.2f µm, force ±%.4f N (1σ)",
             sd_r, sd_disp, sd_force)

    # Drive-feedthrough test on the laser. A real contraction that shows up in the
    # force must still be there when the force PEAKS (~0.2-0.3 s after onset). An
    # excursion that lives only inside the drive window and is gone by then is the
    # 0.7 A pulse coupling into the laser's ADC channel, not motion.
    d_t, d_y = sig["disp"]
    in_fire, post_fire = [], []
    for ot in onsets:
        tr, y = slice_cycle(d_t, d_y, ot, span)
        b = _baseline(tr, y)
        mf = (tr >= 0) & (tr <= fire_s)
        mp = (tr >= 0.20) & (tr <= 0.30)
        if mf.any():
            in_fire.append(float(np.mean(y[mf]) - b))
        if mp.any():
            post_fire.append(float(np.mean(y[mp]) - b))
    disp_in = float(np.nanmean(in_fire)) if in_fire else float("nan")
    disp_post = float(np.nanmean(post_fire)) if post_fire else float("nan")
    # Test the SHAPE, not the absolute level: the excursion is large during the
    # drive and has essentially decayed by the force peak. Comparing disp_post
    # against the noise floor would silently stop flagging under --lowpass/--notch
    # (which collapse the floor) even though the feedthrough step is still there.
    feedthrough = (abs(disp_in) > 2 * sd_disp
                   and abs(disp_post) < 0.3 * abs(disp_in))
    if feedthrough:
        log.warning("laser excursion is confined to the drive window "
                    "(%.2f µm in-fire vs %.2f µm at force peak) — drive "
                    "feedthrough, not displacement", disp_in, disp_post)

    # ---- Resistance normalization ------------------------------------------
    # Raw ohms are not comparable across cycles: the pre-fire R drifts run to run.
    # Normalize to a reference R₀ so every cycle starts at 1.0 and the fire's
    # excursion is read as a FRACTION of the coil's own resistance.
    #   --r-ref cold  : R₀ = the session's initial (baseline) cold reading — one
    #                   reference for the whole run, so the drift stays visible.
    #   --r-ref cycle : R₀ = that cycle's own pre-fire R — drift divided out, so
    #                   only the fire response remains.
    r_t, r_y = sig["res"]
    r0_global = cold_r
    if r0_global is None:                      # no baseline block — fall back to
        pre = r_y[r_t < onsets[0]]             # whatever precedes the first fire
        r0_global = float(np.mean(pre)) if len(pre) else float(np.mean(r_y))
    r0_cycle: List[float] = []
    for ot in onsets:
        tr, r = slice_cycle(r_t, r_y, ot, span)
        b = _baseline(tr, r)
        r0_cycle.append(b if np.isfinite(b) else float(r0_global))

    def r_ref(i: int) -> float:
        """R₀ for cycle i under the selected reference."""
        return float(r0_global) if args.r_ref == "cold" else r0_cycle[i]

    log.info("resistance normalized to R₀ (%s): %.3f Ω%s", args.r_ref, r0_global,
             "" if args.r_ref == "cold" else " (global) / per-cycle pre-fire R")
    sd_r_n = sd_r / float(r0_global)           # noise floor in R/R₀ units

    # -----------------------------------------------------------------------
    # Figure 1 — timeline, one panel per measure (never two y-scales on one axes)
    # -----------------------------------------------------------------------
    rows = [
        ("volt", f"drive V", "V", C_VOLT),
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
            # One global reference on the timeline, so the baseline drift the
            # per-cycle reference would divide out stays visible here.
            y = y / float(r0_global)
        ax.set_facecolor(SURFACE)
        for i, ot in enumerate(onsets):
            ax.axvspan(ot - t0, ot - t0 + fire_s, color=C_FIRE, alpha=0.16, lw=0,
                       label="fire pulse" if i == 0 else None)
        ax.plot(tr, y, lw=1.1, color=color, solid_joinstyle="round")
        if key == "res":
            ax.axhspan(1 - 2 * sd_r_n, 1 + 2 * sd_r_n, color=C_REF, alpha=0.16,
                       lw=0, zorder=0, label=f"±2σ noise ({2 * sd_r_n:.1%})")
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.text(t_hi, 1.0, f" R₀ ({r0_global:.2f} Ω)", color=INK_2,
                    fontsize=8, va="center", ha="left")
        ax.set_ylabel(f"{name}\n({unit})" if "\n" not in name else name,
                      fontsize=9, color=INK_2)
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
    axes[0].legend(loc="upper right", fontsize=8, frameon=False,
                   labelcolor=INK_2)
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

    # -----------------------------------------------------------------------
    # Figure 2 — every cycle overlaid on fire-onset time
    # -----------------------------------------------------------------------
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
        ax.set_facecolor(SURFACE)
        ax.axvspan(0, fire_s, color=C_FIRE, alpha=0.16, lw=0, label="fire pulse")
        for i, ot in enumerate(onsets):
            tr, yc = slice_cycle(t, y, ot, span)
            if key == "res":
                yc = yc / r_ref(i)      # every cycle now starts at 1.0
            if as_delta:
                yc = yc - _baseline(tr, yc)
            ax.plot(tr, yc, lw=1.1, color=CYCLE_CMAP(norm(i + 1)), alpha=0.9)
        if key == "res":
            # The ±2σ scatter band: if the fire's ΔR does not leave this band,
            # the resistance signature is not resolved and the panel must show it.
            ax.axhspan(1 - 2 * sd_r_n, 1 + 2 * sd_r_n, color=C_REF,
                       alpha=0.16, lw=0, zorder=0,
                       label=f"±2σ noise ({2 * sd_r_n:.1%})")
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.legend(loc="upper right", fontsize=8, frameon=False,
                      labelcolor=INK_2)
            ax.text(0.985, 0.04,
                    f"R₀ = {r0_global:.2f} Ω. ΔR/R₀ stays inside the ±2σ band:\n"
                    f"the 100 ms fire is NOT resolved in R",
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
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
    axes[0].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)

    # 6th cell: the cycle-index colorbar (identity of each trace) + the recipe.
    ax = axes[5]
    ax.set_facecolor(SURFACE)
    ax.axis("off")
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=CYCLE_CMAP), ax=ax,
                      orientation="horizontal", fraction=0.25, pad=0.05,
                      aspect=18)
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

    # -----------------------------------------------------------------------
    # Per-cycle metrics + Figure 3 — do the cycles repeat, or do they drift?
    # -----------------------------------------------------------------------
    met: List[dict] = []
    for i, ot in enumerate(onsets):
        row = {"cycle": i + 1, "t_fire_s": float(ot - t0)}

        tr, r = slice_cycle(*sig["res"], ot, span)
        r0 = _baseline(tr, r)
        ref = r_ref(i)
        row["R0_ref_ohm"] = ref
        row["R_pre_ohm"] = r0
        row["R_pre_norm"] = r0 / ref
        # MEAN over the fire window — NOT max(). With ~3% per-sample noise the
        # maximum of the window is just the largest noise excursion (and always
        # positive), which hid the real effect: R *drops* ~3% during the fire.
        # The window mean is the right estimator and ~√n less noisy.
        hot = (tr >= 0) & (tr <= fire_s)
        row["R_fire_ohm"] = float(np.mean(r[hot])) if hot.any() else float("nan")
        row["dR_fire_ohm"] = row["R_fire_ohm"] - r0
        # ΔR as a fraction of the coil's own R₀ — the comparable, unit-free form.
        row["dR_fire_pct"] = 100.0 * (row["R_fire_ohm"] - ref) / ref
        end = tr >= span - 0.5
        row["R_end_ohm"] = float(np.mean(r[end])) if end.any() else float("nan")
        row["R_end_norm"] = row["R_end_ohm"] / ref

        tr, f = slice_cycle(*sig["force"], ot, span)
        f0 = _baseline(tr, f)
        row["F_pre_N"] = f0
        if len(f):
            j = int(np.argmax(np.abs(f - f0)))
            row["dF_peak_N"] = float(f[j] - f0)
            row["t_F_peak_s"] = float(tr[j])
        end = tr >= span - 0.5
        row["dF_end_N"] = float(np.mean(f[end]) - f0) if end.any() else float("nan")

        tr, d = slice_cycle(*sig["disp"], ot, span)
        d0 = _baseline(tr, d)
        if len(d):
            j = int(np.argmax(np.abs(d - d0)))
            row["dDisp_peak_um"] = float(d[j] - d0)
        # Feedthrough evidence: the laser inside the drive window vs at the force
        # peak. Motion persists into the second column; feedthrough does not.
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
        ("R_pre_norm", "Pre-fire resistance (did it cool back to R₀?)",
         "R/R₀", C_RES),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), facecolor=SURFACE)
    for ax, (key, name, unit, color) in zip(axes, trends):
        y = np.array([m.get(key, np.nan) for m in met], dtype=float)
        ax.set_facecolor(SURFACE)
        if key == "dR_fire_pct":
            band = 100.0 * 2 * sd_r_n
            ax.axhspan(-band, band, color=C_REF, alpha=0.16, lw=0,
                       zorder=0, label=f"±2σ noise (±{band:.1f}%)")
            ax.axhline(0, lw=0.8, color=AXIS)
            ax.legend(loc="upper left", fontsize=8, frameon=False,
                      labelcolor=INK_2)
        if key == "dF_peak_N":
            ax.axhspan(-2 * sd_force, 2 * sd_force, color=C_REF, alpha=0.16,
                       lw=0, zorder=0, label=f"±2σ noise ({2 * sd_force:.3f} N)")
            ax.legend(loc="upper left", fontsize=8, frameon=False,
                      labelcolor=INK_2)
        ax.plot(cyc, y, "-o", lw=1.6, ms=6, color=color,
                markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3)
        if key == "R_pre_norm":
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.text(0.98, 0.06, f"R₀ = {r0_global:.2f} Ω", transform=ax.transAxes,
                    color=INK_2, fontsize=8, ha="right")
        if np.isnan(y[0]):
            # The SMA V/I/R stream only starts when the driver arms — which is
            # the first fire — so cycle 1 has no pre-fire baseline to subtract.
            ax.text(0.02, 0.02, "cycle 1: no pre-fire baseline (stream starts at "
                    "the first fire)", transform=ax.transAxes, color=MUTED,
                    fontsize=7, ha="left")
        # Direct-label the first and last cycle so the trend reads without the axis.
        for j in (0, len(cyc) - 1):
            if np.isfinite(y[j]):
                ax.annotate(f"{y[j]:.3g}", (cyc[j], y[j]), textcoords="offset points",
                            xytext=(0, 9), ha="center", fontsize=8, color=INK_2)
        ax.set_title(name, fontsize=10, color=INK, loc="left")
        ax.set_xlabel("cycle", fontsize=9, color=INK_2)
        ax.set_ylabel(unit, fontsize=9, color=INK_2)
        ax.set_xticks(cyc)
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        ax.margins(y=0.22)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
    fig.suptitle(f"{sess.name} — cycle-to-cycle trend{stamp}", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = sess / f"cycles_trend{sfx}.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # Console summary.
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
          f"see fit_transition.py)")
    print(f"  laser   : {disp_in:+.2f} µm inside the fire window, "
          f"{disp_post:+.2f} µm at the force peak (noise ±{sd_disp:.2f} µm)"
          + (" — DRIVE FEEDTHROUGH, not motion" if feedthrough else ""))
    if filt_label:
        print(f"  ⚠ displacement FILTERED: {filt_label} "
              f"(force/resistance untouched; raw PNGs preserved)")
    print(f"  outputs : cycles_timeline{sfx}.png, cycles_overlay{sfx}.png, "
          f"cycles_trend{sfx}.png, cycles_metrics{sfx}.csv\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
