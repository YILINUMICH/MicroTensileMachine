"""CYCLE TRAJECTORIES — heating→cooling, overlaid by current level.

    python plot_trajectory.py                # both variants, all pulse lengths
    python plot_trajectory.py --norm shape   # just the shape variant
    python plot_trajectory.py --heat 400     # just one pulse length

Writes  trajectory_<heat>ms_<norm>.png  — one figure per pulse length per
variant, 4 channels x 2 time scales.

═══ WHAT EACH VARIANT ANSWERS ══════════════════════════════════════════════
They are not substitutes; neither one can answer the other's question.

  --norm abs    "HOW MUCH?"  Physical units. But the response spans ~400x
                across levels (16 -> 6800 um), so on a shared linear axis the
                low-current traces sit near zero. That is honest, not a defect:
                it is the same thing the envelope chart shows.

  --norm shape  "WHAT SHAPE?"  Every channel scaled to its own peak, so all
                levels start at 0, peak at 1, and decay. This is the only view
                that answers whether the RELAXATION TIME CONSTANT depends on
                drive level, or whether every level relaxes the same way and
                only the amplitude changes. For a network learning thermal
                dynamics that is the more informative question, and it is not
                recoverable from the per-pulse table at all.

═══ CHANNELS ══════════════════════════════════════════════════════════════
  dR/R0 (%)   self-sensing signal, normalized to THAT CYCLE's own r_base_ohm
              (they range 4.65-4.73), so baseline drift is removed rather than
              folded into the trace. Absolute ohms would be four flat lines at
              ~4.7 with a wiggle; the effect is a ~7% dip.
  power (W)   the drive input. In `abs` it is deliberately NOT normalized --
              it spans 0.05-3.1 W BY DESIGN and that spread is what separates
              the curves. Normalizing it would erase the independent variable.
  stroke      displacement, delta vs the pre-fire baseline
  force       load cell, delta vs the pre-fire baseline

═══ TIME ══════════════════════════════════════════════════════════════════
Two columns, same trace: DETAIL (0-1.5 s) and FULL (-2 to +30 s). Measured
rise-to-peak is 0.23-0.43 s while recovery to within 10% of rest takes
4.9-12.4 s, so no single axis serves both -- a 30 s axis compresses the rise
to about one pixel, and a 3 s axis (the 2026-07-15 convention) cuts the
recovery off entirely at high energy.

═══ NOISE HANDLING ════════════════════════════════════════════════════════
Per-sample R is 4.8-8.1% during heat but 24% during cool, because at the
~107 mA idle bias the current sense is near its floor. Two reductions, both
non-destructive: cycles are combined by MEDIAN across the 5 non-bootstrap
repeats (sqrt(5)), and a ~100 ms rolling median is applied to R in the cool
phase only (another ~sqrt(100)). Heat-phase R is left raw -- it does not need
help, and smoothing it would blunt the transition edge that matters.

The bootstrap cycle is excluded here (not hidden -- it is in the table, and it
is a different initial condition: it fires into a fully relaxed wire).

═══ COLOR ═════════════════════════════════════════════════════════════════
Current level is ORDINAL over 9 steps, so it gets a sequential ramp plus a
COLORBAR. Note the honest limitation: 9 steps of one hue cannot each clear the
>=15 OKLab adjacent-pair floor that a CATEGORICAL palette must -- the ramp only
spans ~0.75 in L*, giving ~0.09 per step. That floor governs identity-by-color;
here color encodes MAGNITUDE and is read against the colorbar, with the
extremes direct-labeled. Adjacent 100 mA steps are deliberately not meant to be
told apart by hue alone.
"""
import argparse
import os

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import cm, colors

from get_cycle import get_cycle, list_cycles

_HERE = os.path.dirname(os.path.abspath(__file__))
DERIVED = os.path.join(_HERE, "..", "data", "derived")   # pipeline outputs

from analyze_raw import CAMPAIGNS, derived_dir  # noqa: E402
SURFACE = "#fafafa"
INK, INK_MUTED = "#1a1a1a", "#6b7280"
PRE_S, POST_S = 2.0, 29.0
GRID_HZ = 400.0
DETAIL_S = 1.5
# Rolling-median window for R outside the heat pulse. Measured on the idle
# phase: 0.25 s -> 4.35%, 0.50 s -> 3.18%, 1.00 s -> 2.33%, 2.00 s -> 1.76%.
# 1 s is the knee: cool dynamics run over 5-20 s so it costs 5-20% of the
# timescale, while 2 s starts blunting the fast end for little further gain.


# where each (level, heat) lives; map runs preferred, probe fills 950x400
SRC_MAP = {}
for lv in (150, 250, 350, 450, 550, 650, 750, 850, 950):
    SRC_MAP[(lv, 100)] = "sweep_20260731_145838"
for lv in (150, 250):
    SRC_MAP[(lv, 200)] = "sweep_20260731_145838"
for lv in (350, 450, 550, 650, 750, 850, 950):
    SRC_MAP[(lv, 200)] = "sweep_20260731_155129"
for lv in (150, 250, 350, 450, 550, 650, 750, 850, 950):
    SRC_MAP[(lv, 300)] = "sweep_20260731_155129"
for lv in (150, 250, 350, 450, 550, 650, 750, 850):
    SRC_MAP[(lv, 400)] = "sweep_20260731_155129"
SRC_MAP[(950, 400)] = "sweep_20260731_134414"

CHANNELS = [
    ("dR_pct", "ΔR / R₀   (%)", "resistance — self-sensing"),
    ("power_W", "power  (W)", "electrical drive"),
    ("dx_um", "stroke  Δx  (µm)", "displacement"),
    ("dF_mN", "force  ΔF  (mN)", "load cell"),
]


def rolling_median(y, n):
    if n < 3:
        return y
    s = pd.Series(y)
    return s.rolling(n, center=True, min_periods=1).median().to_numpy()


# ─── NO TEMPORAL FILTERING ────────────────────────────────────────────────
# These panels plot the measurement, not a reconstruction of it. Low-pass
# filtering was tried and REMOVED: it makes the trace stop reflecting what the
# rig actually did, which is the same reason nothing is dropped from the
# per-pulse table.
#
# Kept here as a record so it is not re-introduced by accident. Measured on an
# idealised -13% R dip plus real noise:
#
#     filter                    overshoot   idle sd   dip kept
#     butterworth 4 @1 Hz        +1.077%     1.47%      68%
#     butterworth 2 @1 Hz        +0.391%     1.44%      66%
#     bessel 4 @1 Hz             +0.046%     1.38%      63%
#     gaussian (equivalent)       0.000%     1.32%      61%
#
# Two things that cost more than they bought. Butterworth RINGS: the pulse is a
# step, and an 8th-order-effective filtfilt swings +1.08% ABOVE baseline after
# it. The true signal is strictly negative there, so that excursion is pure
# artifact sitting exactly where the cooling curve is read -- it looks like the
# wire oscillating when it is not. Gaussian has provably zero overshoot (its
# kernel is strictly positive, so the step response is monotonic) but still
# costs ~40% of the dip amplitude at the corner needed to quiet the idle.
#
# The honest position: R at the ~107 mA idle bias genuinely carries ~24%
# per-sample noise, and no filter creates information that was not measured.
# The trace is noisy because the measurement is noisy. If that noise matters
# for a given question, average over an explicit window and SAY the window --
# do not bake it into the picture.
#
# The one aggregation still applied is the MEDIAN ACROSS THE 5 REPEAT CYCLES,
# which is declared in the subtitle. That combines independent measurements of
# the same condition; it does not reshape a single one in time.


def condition_trace(sweep, level_mA, heat_ms, grid):
    """Median trajectory over the non-bootstrap cycles, on a common grid."""
    try:
        n_cyc = len(list_cycles(sweep, level_mA, heat_ms))
    except Exception:
        return None
    if n_cyc < 2:
        return None
    stack = {k: [] for k, _, _ in CHANNELS}
    r0s = []
    for c in range(2, n_cyc + 1):            # cycle 1 = bootstrap, held out
        try:
            df = get_cycle(sweep, level_mA, heat_ms, cycle=c,
                           pre_s=PRE_S, post_s=POST_S)
        except Exception:
            continue
        t = df.t_s.to_numpy()
        pre = df[df.phase == "pre"]
        if pre.empty:
            continue
        r0 = float(np.median(pre.sma_r.dropna())) if pre.sma_r.notna().any() else np.nan
        r0s.append(r0)
        x0, f0 = pre.laser_mm.mean(), pre.force_mN.mean()
        cols = {
            "dR_pct": 100.0 * (df.sma_r.to_numpy() / r0 - 1.0) if r0 == r0 else None,
            "power_W": df.power_W.to_numpy(),
            "dx_um": 1e3 * (df.laser_mm.to_numpy() - x0),
            "dF_mN": df.force_mN.to_numpy() - f0,
        }
        heat_end = df.t_s[df.phase == "heat"].max() if (df.phase == "heat").any() else 0.0
        for k, y in cols.items():
            if y is None:
                continue
            g = np.interp(grid, t, y, left=np.nan, right=np.nan)
            stack[k].append(g)
    out = {}
    for k, arr in stack.items():
        if arr:
            out[k] = np.nanmedian(np.vstack(arr), axis=0)
    if out and r0s:
        out["_r0"] = float(np.nanmedian(r0s))
    return out or None


def build(heat_ms, norm, levels):
    grid = np.arange(-PRE_S, POST_S, 1.0 / GRID_HZ)
    traces = {}
    for lv in levels:
        sweep = SRC_MAP.get((lv, heat_ms))
        if not sweep:
            continue
        tr = condition_trace(sweep, lv, heat_ms, grid)
        if tr:
            traces[lv] = tr
    if not traces:
        return None

    lv_sorted = sorted(traces)
    base = matplotlib.colormaps["Blues"]
    # Sample the ramp by INDEX, not by mA: starting at 0.22 keeps the lightest
    # level visible against the surface, and the bar then has no dead range.
    lut = [base(0.22 + 0.78 * i / max(1, len(lv_sorted) - 1))
           for i in range(len(lv_sorted))]
    cmap = colors.ListedColormap(lut)
    bounds = [lv_sorted[0] - 50] + [(a + b) / 2 for a, b in
                                    zip(lv_sorted, lv_sorted[1:])] + [lv_sorted[-1] + 50]
    norm_c = colors.BoundaryNorm(bounds, cmap.N)

    fig, axes = plt.subplots(len(CHANNELS), 2, figsize=(13.5, 13.6),
                             gridspec_kw={"width_ratios": [1, 1.35]})
    fig.patch.set_facecolor(SURFACE)
    fig.subplots_adjust(top=0.895, bottom=0.085, left=0.075, right=0.945,
                        hspace=0.42, wspace=0.20)

    r0_all = [t["_r0"] for t in traces.values() if "_r0" in t]
    r0_med = float(np.median(r0_all)) if r0_all else np.nan
    heat_s = heat_ms / 1000.0
    for row, (key, ylab, subtitle) in enumerate(CHANNELS):
        for col, (t_lo, t_hi) in enumerate([(-0.15, DETAIL_S), (-PRE_S, POST_S)]):
            ax = axes[row, col]
            ax.set_facecolor(SURFACE)
            ax.axvspan(0, heat_s, color="#f2c9c0", alpha=0.55, lw=0, zorder=0)
            for lv in lv_sorted:
                y = traces[lv].get(key)
                if key.startswith("_"):
                    continue
                if y is None:
                    continue
                if norm == "shape" and key != "power_W":
                    # POWER is never shape-normalized: it is the drive input,
                    # not a response, and dividing it by its own peak turns the
                    # low levels into hash around the idle bias (0.05 W idle
                    # against a 0.13 W peak at 150 mA reads as 0.4, not 0).
                    #
                    # The DIVISOR is a robust scale (99.5th percentile), not
                    # the raw max -- with a noisy channel the max is a noise
                    # excursion, so scaling by it would shrink the whole trace
                    # by however bad the worst sample happened to be. The
                    # plotted data itself is untouched.
                    pk = np.nanpercentile(np.abs(y), 99.5)
                    if pk > 0:
                        y = y / pk
                m = (grid >= t_lo) & (grid <= t_hi)
                ax.plot(grid[m], y[m], lw=1.5, color=cmap(norm_c(lv)), zorder=2)
            ax.axhline(0, color="#c9ced6", lw=0.9, zorder=1)
            ax.grid(axis="y", color="#e8ebee", lw=0.8)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color("#c9ced6")
            ax.tick_params(colors=INK_MUTED, labelsize=9.5)
            ax.set_xlim(t_lo, t_hi)
            if col == 0:
                lab = ylab if (norm == "abs" or key == "power_W")                     else "normalized  " + ylab.split("  (")[0]
                ax.set_ylabel(lab,
                              fontsize=10.5, color=INK)
                ax.set_title(subtitle, loc="left", fontsize=11,
                             color=INK, pad=6)
            else:
                ax.set_title("full cycle incl. cooling", loc="left",
                             fontsize=10, color=INK_MUTED, pad=6)
            if key == "dR_pct" and norm == "abs" and r0_med == r0_med:
                # Absolute ohms on the right. NOT a second scale -- it is the
                # same axis relabelled, since R = R0*(1 + dR/R0). Normalizing
                # does not add noise (R0 is known to 0.38% against 24%
                # per-sample), so this is purely "read it in ohms if you
                # prefer"; the percentage keeps levels comparable when R0
                # drifts between conditions.
                axr = ax.twinx()
                axr.set_ylim([r0_med * (1 + v / 100.0) for v in ax.get_ylim()])
                axr.set_ylabel(f"R  (Ω)   ·   R₀ = {r0_med:.2f} Ω",
                               fontsize=9.5, color=INK_MUTED)
                axr.tick_params(colors=INK_MUTED, labelsize=9)
                for sd in ("top", "left", "bottom"):
                    axr.spines[sd].set_visible(False)
                axr.spines["right"].set_color("#c9ced6")
            if row == len(CHANNELS) - 1:
                ax.set_xlabel("time since heat onset (s)", fontsize=10.5,
                              color=INK)

    sm = cm.ScalarMappable(cmap=cmap, norm=norm_c)
    cb = fig.colorbar(sm, ax=axes, orientation="horizontal", fraction=0.028,
                      pad=0.070, aspect=55)
    cb.set_label("commanded current (mA)  ·  light → dark = low → high",
                 fontsize=10.5, color=INK)
    cb.set_ticks(lv_sorted)
    cb.ax.tick_params(colors=INK_MUTED, labelsize=9.5)
    cb.outline.set_visible(False)

    what = ("every channel scaled to its own peak — compares SHAPE and "
            "relaxation time, not amplitude"
            if norm == "shape" else
            "physical units — power deliberately NOT normalized, its 0.05–3.1 W "
            "spread is the independent variable")
    # Both anchored va="top" from an explicit y. Without va the default is
    # "baseline", which puts a multi-line block's FIRST line ABOVE its y and
    # straight through the title.
    fig.text(0.075, 0.990, f"SMA cycle trajectories — {heat_ms} ms pulse, 30 s cool",
             ha="left", va="top", fontsize=16, weight="semibold", color=INK)
    fig.text(0.075, 0.966,
             f"{len(lv_sorted)} current levels · RAW, no temporal filtering · "
             f"median of the 5 repeat cycles per level · shaded band = heat "
             f"pulse\n{what}",
             ha="left", va="top", fontsize=10.5, color=INK_MUTED)

    # plot_trajectory is pinned to the July campaign through SRC_MAP, so
    # its figures belong in that campaign folder.
    out = os.path.join(derived_dir(CAMPAIGNS["20260731"]["dir"]),
                       f"trajectory_{heat_ms}ms_{norm}.png")
    fig.savefig(out, dpi=140, facecolor=SURFACE)
    plt.close(fig)
    print(f"  -> {os.path.basename(out)}  ({len(lv_sorted)} levels)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--norm", choices=["abs", "shape", "both"], default="both")
    ap.add_argument("--heat", type=int, default=None)
    a = ap.parse_args()
    heats = [a.heat] if a.heat else [100, 200, 300, 400]
    norms = ["abs", "shape"] if a.norm == "both" else [a.norm]
    levels = [150, 250, 350, 450, 550, 650, 750, 850, 950]
    for h in heats:
        for n in norms:
            build(h, n, levels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
