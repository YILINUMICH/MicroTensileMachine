#!/usr/bin/env python3
"""Generate the three report figures for the R-noise investigation.

  fig1_noise_source_24kHz.png  -- the 24.414 kHz tone found on the LDO output,
                                  and why it aliases into the deployed band
  fig2_VIR_before_filter.png   -- V / I / R at the deployed ~980 Hz rate, raw
  fig3_VIR_after_filter.png    -- same data, 10 Hz zero-phase low-pass

Figs 2 and 3 are drawn on IDENTICAL axes so the improvement is a like-for-like
comparison rather than a rescaling illusion.

V, I and R carry different units, so they are stacked small multiples sharing one
time axis -- never a dual-axis chart.

Usage:
    python make_report_plots.py [--session <dir>] [--scope <npz>] [--corner 10]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from scipy import signal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from analyze_r_noise import load_longest_run, filter_r, alias_frequencies

# Categorical slots 1-3 from the reference palette (certified all-pairs safe).
C_V, C_I, C_R = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK_2, INK_MUTED = "#0b0b0b", "#52514e", "#8a8984"
SURFACE = "#fcfcfb"
ACCENT = "#e34948"        # status/critical slot, used only for the callout

CODES_PER_DIV = 30.0
LDO_TONES = [24414.06, 73242.2, 97656.2, 122070.3, 460205.0]


def style(ax, grid_axis="both"):
    """Recessive grid and axes; the data carries the ink."""
    ax.set_facecolor(SURFACE)
    ax.grid(True, which="major", axis=grid_axis, color=INK_MUTED,
            alpha=0.22, lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK_MUTED)
        ax.spines[s].set_linewidth(0.8)
    ax.tick_params(colors=INK_2, labelsize=9, length=3, width=0.8)


# ---------------------------------------------------------------- figure 1
def fig_noise_source(scope_npz: Path, out: Path, deployed_fs: float):
    z = np.load(scope_npz, allow_pickle=True)
    fs = float(z["C1_fs"])
    v = np.asarray(z["C1_codes"], float) * (float(z["C1_vdiv"]) / CODES_PER_DIV)
    v = v - v.mean()
    f, P = signal.welch(v, fs, nperseg=8192)
    amp = np.sqrt(P * (f[1] - f[0])) * 1e3          # mV rms per bin

    fig, ax = plt.subplots(figsize=(10, 6.0), facecolor=SURFACE)
    fig.subplots_adjust(top=0.76)          # room for title + subtitle, no overlap
    nyq = deployed_fs / 2.0
    total = np.sqrt(np.trapezoid(P, f)) * 1e3

    # Everything right of the deployed Nyquist folds back into the measurement.
    ax.axvspan(nyq, f[-1], color=ACCENT, alpha=0.055, lw=0)
    ax.axvline(nyq, color=ACCENT, lw=1.4, ls="--", alpha=0.85)

    ax.loglog(f[1:], amp[1:], color=C_V, lw=1.0, solid_joinstyle="round")

    # Label only the genuinely largest lines, found from the data -- not a
    # hand-picked list, which is how you end up captioning a peak that isn't
    # actually the biggest one.
    pk, _ = signal.find_peaks(20 * np.log10(amp + 1e-12), prominence=3)
    top = pk[np.argsort(amp[pk])[::-1][:4]]
    # These lines sit close together on a log axis, so a fixed offset stacks the
    # labels on top of each other. Stagger height and side, and draw a leader.
    offsets = [(-34, 34), (2, 14), (30, 40), (44, 16)]
    for j, k in enumerate(sorted(top, key=lambda k: f[k])):
        ax.plot([f[k]], [amp[k]], "o", ms=7.5, color=C_V,
                markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
        ax.annotate(f"{f[k]/1e3:.1f} kHz · {amp[k]:.0f} mV", (f[k], amp[k]),
                    textcoords="offset points", xytext=offsets[j], ha="center",
                    fontsize=8.5, color=INK_2, zorder=6,
                    arrowprops=dict(arrowstyle="-", color=INK_MUTED,
                                    lw=0.7, alpha=0.8,
                                    shrinkA=0, shrinkB=5))

    ax.text(nyq * 1.4, amp[1:].min() * 1.6,
            f"deployed ADC Nyquist  {nyq:.0f} Hz\n"
            f"→ essentially ALL of this ripple\n   aliases into the measurement band",
            color=ACCENT, fontsize=9.5, va="bottom", ha="left", linespacing=1.5)

    ax.set_xlabel("frequency (Hz)", color=INK_2, fontsize=10)
    ax.set_ylabel("V$_{LDO}$ ripple (mV rms per bin)", color=INK_2, fontsize=10)
    fig.text(0.055, 0.94,
             f"The LDO output carries {total:.0f} mV rms of ripple — "
             f"5 orders of magnitude above spec",
             color=INK, fontsize=13.5, fontweight="bold", ha="left")
    fig.text(0.055, 0.895,
             "TPS7A57 output, 0.87 V / 186 mA into 4.9 $\\Omega$   ·   "
             "datasheet output noise: 2.45 µV rms",
             color=INK_2, fontsize=9.5, ha="left")
    fig.text(0.055, 0.855,
             "A dense comb of tones from ~12 to 400 kHz (6.1 kHz spacing at the "
             "low end) — no single tone dominates.",
             color=INK_2, fontsize=9.5, ha="left")
    style(ax)
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return out


# ------------------------------------------------------------- figures 2/3
def _vir_panel(t, v, i, r, out: Path, title: str, subtitle: str, lims=None):
    fig, axes = plt.subplots(3, 1, figsize=(10, 7.6), sharex=True,
                             facecolor=SURFACE,
                             gridspec_kw={"hspace": 0.30})
    fig.subplots_adjust(top=0.855, left=0.095, right=0.975, bottom=0.085)
    series = [
        (axes[0], v, C_V, "voltage", "V$_{sma}$  (V)", "V"),
        (axes[1], i, C_I, "current", "I  (A)", "A"),
        (axes[2], r, C_R, "resistance", "R = V/I  ($\\Omega$)", "$\\Omega$"),
    ]
    for ax, y, c, name, ylab, unit in series:
        ax.plot(t, y, color=c, lw=0.7, solid_joinstyle="round")
        ax.set_ylabel(ylab, color=INK_2, fontsize=10)
        # Direct label instead of a legend box -- one series per panel. Both
        # annotations go top-right and stacked, clear of the tick labels.
        ax.text(0.995, 0.95, name, transform=ax.transAxes, ha="right", va="top",
                color=c, fontsize=10.5, fontweight="bold")
        sd = float(np.std(y - signal.savgol_filter(y, 1001, 2)))
        # Sits over the trace in the noisy panel -- give it a surface plate so it
        # stays legible without hiding much data.
        ax.text(0.995, 0.80, f"$\\sigma$ = {sd:.4g} {unit}",
                transform=ax.transAxes, ha="right", va="top",
                color=INK_2, fontsize=9.5, zorder=6,
                bbox=dict(facecolor=SURFACE, edgecolor="none",
                          alpha=0.82, pad=2.5))
        style(ax)
    if lims:
        for ax, (lo, hi) in zip(axes, lims):
            ax.set_ylim(lo, hi)
    axes[-1].set_xlabel("time (s)", color=INK_2, fontsize=10)
    fig.text(0.055, 0.955, title, color=INK, fontsize=13.5,
             fontweight="bold", ha="left")
    fig.text(0.055, 0.915, subtitle, color=INK_2, fontsize=9.5, ha="left")
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return [ax.get_ylim() for ax in axes]


def main():
    ap = argparse.ArgumentParser()
    here = Path(__file__).parent
    ap.add_argument("--session", type=Path,
                    default=here.parent / "Experiment_SMAThermalCharacterization"
                    / "data" / "console_20260715_193936_5V0.5V")
    ap.add_argument("--scope", type=Path,
                    default=here / "out" / "scope" / "phase2_ldoOut_0p85V.npz")
    ap.add_argument("--corner", type=float, default=10.0)
    ap.add_argument("--outdir", type=Path, default=here / "out" / "report")
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    fs, v, i, dur = load_longest_run(args.session / "h7.csv")
    t = np.arange(len(v)) / fs
    r = v / i
    print(f"deployed run: {len(v)} pts @ {fs:.1f} Hz ({dur:.1f} s)")

    print(f"\nfigure 1  <- {args.scope.name}")
    print(f"  {fig_noise_source(args.scope, args.outdir / 'fig1_noise_source_24kHz.png', fs)}")

    for f_, a, bad in alias_frequencies(LDO_TONES, fs):
        print(f"    {f_/1e3:9.3f} kHz folds to {a:7.1f} Hz"
              + ("   << IN BAND" if bad else ""))

    vf = filter_r(v, fs, args.corner)
    if_ = filter_r(i, fs, args.corner)
    rf = filter_r(r, fs, args.corner)

    # Draw the FILTERED figure first to fix the axes, then reuse those limits for
    # the raw figure -- so the two are compared on identical scales. (Raw limits
    # would be set by outliers and squash the filtered trace to a flat line.)
    lims_raw = [(np.percentile(x, 0.2), np.percentile(x, 99.8)) for x in (v, i, r)]
    pad = [(lo - 0.06 * (hi - lo), hi + 0.06 * (hi - lo)) for lo, hi in lims_raw]

    print("\nfigure 2 (before)")
    _vir_panel(t, v, i, r, args.outdir / "fig2_VIR_before_filter.png",
               "Before filtering — V, I and R at the deployed 980 Hz rate",
               f"fire/cool cycling; LDO ripple (12–400 kHz) aliases down into "
               f"50–480 Hz and dominates R   ·   "
               f"$\\sigma_R$ = {np.std(r-signal.savgol_filter(r,1001,2))*1e3:.0f} m$\\Omega$",
               lims=pad)
    print(f"  {args.outdir/'fig2_VIR_before_filter.png'}")

    print("\nfigure 3 (after)")
    _vir_panel(t, vf, if_, rf, args.outdir / "fig3_VIR_after_filter.png",
               f"After filtering — {args.corner:g} Hz zero-phase low-pass",
               f"folded tones sit above {args.corner:g} Hz so the filter removes them; "
               f"thermal signal (0.5–10 Hz) is untouched   ·   "
               f"$\\sigma_R$ = {np.std(rf-signal.savgol_filter(rf,1001,2))*1e3:.0f} m$\\Omega$",
               lims=pad)
    print(f"  {args.outdir/'fig3_VIR_after_filter.png'}")

    s0 = np.std(r - signal.savgol_filter(r, 1001, 2))
    s1 = np.std(rf - signal.savgol_filter(rf, 1001, 2))
    print(f"\nsigma_R  {s0*1e3:.1f} -> {s1*1e3:.1f} mohm   ({s0/s1:.1f}x better)")


if __name__ == "__main__":
    main()
