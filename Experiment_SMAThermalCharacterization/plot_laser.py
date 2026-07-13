#!/usr/bin/env python3
"""
plot_laser.py — laser-channel diagnostic for a console session.

The laser trace looks like a ±1.4 µm noise band on a whole-session plot. It is
not noise: most of it is a single coherent tone. This script pulls that tone out
and shows it four ways (zoom, spectrum, phase-fold, autocorrelation), and
separately exposes the zero-order-hold duplication in the H7 stream.

Two facts it is built to surface:

  1. A coherent periodic ripple in the laser reading (~66 Hz on the sessions seen
     so far), amplitude ~1.5 µm, which accounts for most of the channel's
     apparent noise. The ADC2/load channel does NOT carry it, so it is specific
     to the ADC1/laser path (Keyence IL-030 analog output or its wiring) rather
     than a shared reference/supply artifact.
  2. The stream is READ faster than the ADC CONVERTS: ~493 Hz read vs a 400 SPS
     ADC, so ~19% of rows in h7.csv are exact duplicates of the previous sample
     (a zero-order hold), on BOTH channels. The effective sample rate is 400 Hz.

Because the true conversion rate is 400 SPS (Nyquist 200 Hz), a tone reported at
f could equally be an alias of 400−f, 400+f, ... — the script prints the alias
candidates rather than asserting the physical frequency.

Usage:
    python plot_laser.py --session data/console_20260713_122906

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Dict

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

log = logging.getLogger("plot_laser")

# dataviz reference palette, light surface
SURFACE, INK, INK_2, MUTED, GRID, AXIS = (
    "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7")
C_LASER = "#eb6834"    # orange  — laser
C_LOAD = "#2a78d6"     # blue    — load (comparison)
C_TONE = "#4a3aa7"     # violet  — the fitted tone
C_DUP = "#e34948"      # red     — duplicated (held) samples
C_REF = "#898781"
_PLATE = dict(facecolor=SURFACE, edgecolor="none", alpha=0.9, pad=3.0)


def load_channels(path: Path) -> Dict[str, dict]:
    acc: Dict[str, dict] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            ch = (r.get("channel") or "").strip()
            if not ch:
                continue
            d = acc.setdefault(ch, {"v": [], "rc": [], "hw": []})
            try:
                d["v"].append(float(r["value"]))
                d["rc"].append(int(float(r["raw_code"])))
                d["hw"].append(int(float(r["hw_us"])))
            except (ValueError, KeyError):
                continue
    return {c: {k: np.asarray(v) for k, v in d.items()}
            for c, d in acc.items() if d["v"]}


def notch_fft(y: np.ndarray, fs: float, freqs, half_width: float) -> np.ndarray:
    """Zero the FFT bins within ±half_width of each frequency (and DC-preserve).
    A brick-wall notch is fine here: we are demonstrating what the channel is
    worth WITHOUT the interferer, not building a real-time filter."""
    Y = np.fft.rfft(y)
    f = np.fft.rfftfreq(len(y), 1.0 / fs)
    for f0 in freqs:
        Y[np.abs(f - f0) <= half_width] = 0.0
    return np.fft.irfft(Y, n=len(y))


def fit_tone(t: np.ndarray, y: np.ndarray, f_lo: float, f_hi: float,
             n: int = 4000) -> tuple:
    """Least-squares fit of a single sinusoid; scan frequency for the best fit.
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


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Laser-channel diagnostic")
    p.add_argument("--session", required=True)
    p.add_argument("--fmin", type=float, default=5.0,
                   help="low edge of the tone search (Hz)")
    p.add_argument("--fmax", type=float, default=195.0,
                   help="high edge of the tone search (Hz)")
    p.add_argument("--zoom-s", type=float, default=0.20,
                   help="width of the zoom window (s)")
    p.add_argument("--notch-hw", type=float, default=1.0,
                   help="half-width (Hz) of the notch applied to the tone and "
                        "its harmonics in the before/after demo")
    p.add_argument("--dpi", type=int, default=140)
    args = p.parse_args()

    sess = Path(args.session)
    if not sess.exists():
        print(f"ERROR: no such session: {sess}", file=sys.stderr)
        return 2
    meta = json.loads((sess / "meta.json").read_text())
    lc = meta["calibration"]["laser"]
    k, v0 = lc["k_mV_per_um"], lc["V0_mV"]

    ch = load_channels(sess / "h7.csv")
    if "laser" not in ch:
        print("ERROR: no laser channel in h7.csv", file=sys.stderr)
        return 2
    las = ch["laser"]

    t = (las["hw"] - las["hw"][0]) / 1e6          # firmware clock, not host
    um = (las["v"] * 1000.0 - v0) / k
    um = um - um.mean()

    # --- zero-order-hold duplication -------------------------------------
    held = np.r_[False, np.diff(las["rc"]) == 0]   # sample repeats the last code
    read_hz = 1e6 / np.median(np.diff(las["hw"]))
    dup_frac = float(held.mean())
    true_hz = read_hz * (1.0 - dup_frac)
    log.info("read rate %.2f Hz, %.1f%% duplicated codes -> true ADC update "
             "%.2f Hz (ADC1 is configured for 400 SPS)",
             read_hz, 100 * dup_frac, true_hz)
    for other in ("load",):
        if other in ch:
            f = float(np.mean(np.diff(ch[other]["rc"]) == 0))
            log.info("  (%s channel duplicates %.1f%% too — the over-read is "
                     "not laser-specific)", other, 100 * f)

    # --- the tone --------------------------------------------------------
    f0, amp, phase, r2 = fit_tone(t, um, args.fmin, args.fmax)
    log.info("dominant tone: %.3f Hz, amplitude %.3f µm (%.3f µm p-p), "
             "explains %.1f%% of the laser variance", f0, amp, 2 * amp, 100 * r2)
    nyq = true_hz / 2.0
    aliases = [abs(n * true_hz + s * f0)
               for n in (1, 2) for s in (-1, 1)]
    log.info("NOTE: true conversion rate is %.0f SPS (Nyquist %.0f Hz) — a tone "
             "reported at %.1f Hz could also be an alias of %s Hz",
             true_hz, nyq, f0, ", ".join(f"{a:.1f}" for a in aliases))

    tone = amp * np.cos(2 * np.pi * f0 * t + phase)
    resid = um - tone

    # =====================================================================
    fig, axes = plt.subplots(3, 2, figsize=(14, 11), facecolor=SURFACE)
    ax = axes.ravel()

    def style(a):
        a.set_facecolor(SURFACE)
        a.grid(True, color=GRID, lw=0.6)
        a.set_axisbelow(True)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            a.spines[s].set_color(AXIS)
        a.tick_params(colors=MUTED, labelsize=8)

    # (0) full trace — what it looks like before you look closely
    style(ax[0])
    ax[0].plot(t, um, lw=0.5, color=C_LASER)
    ax[0].set_title("Full session — reads as a noise band", fontsize=10,
                    color=INK, loc="left")
    ax[0].set_xlabel("t (s)", fontsize=9, color=INK_2)
    ax[0].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    ax[0].text(0.985, 0.04, f"σ = {um.std():.2f} µm, {np.ptp(um):.2f} µm p-p",
               transform=ax[0].transAxes, ha="right", va="bottom",
               fontsize=8, color=INK_2, bbox=_PLATE)

    # (1) zoom — the pattern
    style(ax[1])
    t_lo = t[len(t) // 2]
    m = (t >= t_lo) & (t < t_lo + args.zoom_s)
    ax[1].plot(t[m] - t_lo, um[m], "-o", lw=1.2, ms=3.5, color=C_LASER,
               markeredgecolor=SURFACE, markeredgewidth=0.6, label="laser sample")
    hm = m & held
    ax[1].plot(t[hm] - t_lo, um[hm], "o", ms=6, color=C_DUP, zorder=4,
               label=f"held / duplicate code ({100 * dup_frac:.0f}% of rows)")
    ax[1].plot(t[m] - t_lo, tone[m], lw=1.6, color=C_TONE, alpha=0.85,
               label=f"fitted {f0:.2f} Hz tone")
    ax[1].set_title(f"Zoom ({args.zoom_s * 1000:.0f} ms) — a coherent ripple, "
                    f"not noise", fontsize=10, color=INK, loc="left")
    ax[1].set_xlabel("t (s, from mid-session)", fontsize=9, color=INK_2)
    ax[1].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    ax[1].margins(y=0.28)
    ax[1].legend(loc="lower right", fontsize=7.5, frameon=True, labelcolor=INK_2,
                 facecolor=SURFACE, edgecolor=GRID, framealpha=0.9)

    # (2) spectrum
    style(ax[2])
    fs = read_hz
    g = np.arange(0, t[-1], 1 / fs)
    y = np.interp(g, t, um)
    y = y - y.mean()
    w = np.hanning(len(y))
    Y = np.abs(np.fft.rfft(y * w)) * 2 / w.sum()
    fr = np.fft.rfftfreq(len(y), 1 / fs)
    # Both spectra are normalized to their OWN maximum so the two channels can
    # legitimately share one y-axis (µm and volts have no common scale). The
    # laser's absolute amplitude is annotated instead.
    ax[2].plot(fr, Y / Y.max(), lw=0.9, color=C_LASER, label="laser / ADC1")
    if "load" in ch:
        ld = ch["load"]
        tl = (ld["hw"] - ld["hw"][0]) / 1e6
        lv = ld["v"] - ld["v"].mean()
        yl = np.interp(g, tl, lv)
        yl = yl - yl.mean()
        Yl = np.abs(np.fft.rfft(yl * w)) * 2 / w.sum()
        ax[2].plot(fr, Yl / max(Yl.max(), 1e-12), lw=0.9, color=C_LOAD,
                   alpha=0.75, label="load / ADC2 — no peak here")
    ax[2].axvline(nyq, ls="--", lw=1.0, color=C_REF)
    ax[2].text(nyq - 4, 0.60, f"Nyquist of the true {true_hz:.0f} SPS ADC",
               fontsize=7.5, color=INK_2, va="center", ha="right",
               rotation=90, bbox=_PLATE)
    ax[2].annotate(f"{f0:.2f} Hz — {amp:.2f} µm", (f0, 1.0),
                   textcoords="offset points", xytext=(14, -2), fontsize=8.5,
                   color=INK, bbox=_PLATE)
    for n in (2, 3):
        fh = n * f0
        if fh < fr[-1]:
            ax[2].annotate(f"{n}f", (fh, np.interp(fh, fr, Y) / Y.max()),
                           textcoords="offset points", xytext=(4, 6),
                           fontsize=8, color=INK_2)
    ax[2].set_xlim(0, min(250, fr[-1]))
    ax[2].set_ylim(0, 1.18)
    ax[2].set_title("Amplitude spectrum (firmware clock)", fontsize=10,
                    color=INK, loc="left")
    ax[2].set_xlabel("frequency (Hz)", fontsize=9, color=INK_2)
    ax[2].set_ylabel("amplitude ÷ that channel's own max", fontsize=9,
                     color=INK_2)
    ax[2].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    # (3) phase fold — the waveform shape
    style(ax[3])
    T = 1.0 / f0
    ph = (t % T) / T
    ax[3].plot(ph, um, ".", ms=1.2, color=C_LASER, alpha=0.25,
               label="all samples")
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
    ax[3].fill_between(ctr, q1, q3, color=C_TONE, alpha=0.22, lw=0)
    ax[3].plot(ctr, med, lw=2.0, color=C_TONE, label="median (IQR band)")
    ax[3].set_title(f"Folded on 1/{f0:.2f} Hz = {T * 1000:.2f} ms — the hidden "
                    f"waveform", fontsize=10, color=INK, loc="left")
    ax[3].set_xlabel("phase (cycles)", fontsize=9, color=INK_2)
    ax[3].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    ax[3].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    # (4) autocorrelation
    style(ax[4])
    a = np.correlate(y, y, "full")[len(y) - 1:]
    a = a / a[0]
    lags = np.arange(len(a)) / fs * 1000.0
    n = int(0.25 * fs)
    ax[4].plot(lags[:n], a[:n], lw=1.2, color=C_LASER)
    ax[4].axhline(0, lw=0.8, color=AXIS)
    for n_ in range(1, 5):
        ax[4].axvline(n_ * T * 1000, ls="--", lw=0.9, color=C_TONE, alpha=0.6)
    ax[4].set_title(f"Autocorrelation — peaks every {T * 1000:.2f} ms",
                    fontsize=10, color=INK, loc="left")
    ax[4].set_xlabel("lag (ms)", fontsize=9, color=INK_2)
    ax[4].set_ylabel("r", fontsize=9, color=INK_2)

    # (5) what's left after removing the tone
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

    # =====================================================================
    # Figure 2 — BEFORE vs AFTER the two corrections.
    #
    # This is a POST-PROCESSING demonstration of what the channel is worth once
    # the interferer is gone — no hardware change has been made. It is an upper
    # bound on the gain, not a measurement of a fixed rig.
    #   fix 1: drop the zero-order-hold duplicate rows -> the true 400 SPS series
    #   fix 2: notch the tone and its harmonics
    # =====================================================================
    keep = np.r_[True, np.diff(las["rc"]) != 0]        # fix 1
    t_true, um_true = t[keep], um[keep]

    fs_g = true_hz
    g2 = np.arange(0, t[-1], 1.0 / fs_g)
    before = np.interp(g2, t, um)                       # the stream as recorded
    before -= before.mean()
    true_on_grid = np.interp(g2, t_true, um_true)       # after fix 1 only
    true_on_grid -= true_on_grid.mean()

    harm = [n * f0 for n in (1, 2, 3) if n * f0 < fs_g / 2]
    after = notch_fft(true_on_grid, fs_g, harm, args.notch_hw)   # + fix 2
    after -= after.mean()

    log.info("before: σ %.3f µm | after dedup: σ %.3f µm | after notch: σ %.3f µm",
             before.std(), true_on_grid.std(), after.std())

    fig2, bx = plt.subplots(2, 2, figsize=(14, 8), facecolor=SURFACE)
    bx = bx.ravel()
    C_BEFORE, C_AFTER = C_LASER, "#1baf7a"   # orange -> aqua (slots 8 and 2)

    # (0) full trace, before vs after, one shared µm axis
    style(bx[0])
    bx[0].plot(g2, before, lw=0.5, color=C_BEFORE, alpha=0.85,
               label=f"before — σ {before.std():.2f} µm")
    bx[0].plot(g2, after, lw=0.5, color=C_AFTER,
               label=f"after — σ {after.std():.2f} µm")
    bx[0].set_title("Full session — same axis, same data", fontsize=10,
                    color=INK, loc="left")
    bx[0].set_xlabel("t (s)", fontsize=9, color=INK_2)
    bx[0].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    bx[0].legend(loc="upper right", fontsize=8, frameon=True, labelcolor=INK_2,
                 facecolor=SURFACE, edgecolor=GRID, framealpha=0.9)

    # (1) zoom
    style(bx[1])
    m2 = (g2 >= g2[len(g2) // 2]) & (g2 < g2[len(g2) // 2] + args.zoom_s)
    z = g2[m2] - g2[m2][0]
    bx[1].plot(z, before[m2], "-", lw=1.3, color=C_BEFORE, alpha=0.85,
               label="before")
    bx[1].plot(z, after[m2], "-", lw=1.6, color=C_AFTER, label="after")
    bx[1].axhline(0, lw=0.8, color=AXIS)
    bx[1].set_title(f"Zoom ({args.zoom_s * 1000:.0f} ms) — the ripple is gone",
                    fontsize=10, color=INK, loc="left")
    bx[1].set_xlabel("t (s)", fontsize=9, color=INK_2)
    bx[1].set_ylabel("displacement (µm)", fontsize=9, color=INK_2)
    bx[1].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)

    # (2) spectra, both in µm on one axis
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

    # (3) what it buys you — the resolution bar
    style(bx[3])
    stages = ["as recorded", "+ drop ZOH\nduplicates", "+ notch\nthe tone"]
    sds = [before.std(), true_on_grid.std(), after.std()]
    cols = [C_BEFORE, C_TONE, C_AFTER]
    bars = bx[3].bar(stages, sds, color=cols, width=0.6,
                     edgecolor=SURFACE, linewidth=2)
    for b, s in zip(bars, sds):
        bx[3].annotate(f"{s:.2f} µm", (b.get_x() + b.get_width() / 2, s),
                       textcoords="offset points", xytext=(0, 5), ha="center",
                       fontsize=10, color=INK)
    bx[3].set_title(f"Laser noise floor: {before.std():.2f} µm → "
                    f"{after.std():.2f} µm ({before.std() / after.std():.1f}× better)",
                    fontsize=10, color=INK, loc="left")
    bx[3].set_ylabel("σ (µm)", fontsize=9, color=INK_2)
    bx[3].margins(y=0.22)
    bx[3].set_xlabel("post-processing demo — no hardware change, so this is an "
                     "upper bound on the gain.\nAll σ computed on the common "
                     f"{fs_g:.0f} Hz grid.", fontsize=8, color=MUTED)

    fig2.suptitle(f"{sess.name} — laser channel before vs after the two "
                  f"corrections", fontsize=13, color=INK)
    fig2.tight_layout(rect=(0, 0, 1, 0.95))
    out2 = sess / "laser_before_after.png"
    fig2.savefig(out2, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig2)
    log.info("wrote %s", out2)

    print(f"\n{sess.name} — laser channel")
    print(f"  apparent noise : σ {um.std():.2f} µm, {np.ptp(um):.2f} µm p-p")
    print(f"  dominant tone  : {f0:.3f} Hz, {amp:.2f} µm amp ({2 * amp:.2f} µm "
          f"p-p) — {100 * r2:.0f}% of the variance")
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


if __name__ == "__main__":
    sys.exit(main())
