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

BASE = os.path.dirname(os.path.abspath(__file__))
SURFACE = "#fafafa"
INK, INK_MUTED = "#1a1a1a", "#6b7280"
PRE_S, POST_S = 2.0, 29.0
GRID_HZ = 400.0
DETAIL_S = 1.5
SMOOTH_S = 0.25     # rolling-median window for R outside the heat pulse
SMOOTH_HEAT_S = 0.020   # lighter window DURING heat — see condition_trace()

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


def condition_trace(sweep, level_mA, heat_ms, grid):
    """Median trajectory over the non-bootstrap cycles, on a common grid."""
    try:
        n_cyc = len(list_cycles(sweep, level_mA, heat_ms))
    except Exception:
        return None
    if n_cyc < 2:
        return None
    stack = {k: [] for k, _, _ in CHANNELS}
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
            if k == "dR_pct":
                # Smooth ON THE GRID, where the window length is honest. The
                # raw frame is a mixed-rate union (SMA ~970 Hz interleaved with
                # laser/load 490 Hz, ~2000 rows/s), so an N-row window there
                # spans an unpredictable and much shorter time.
                # HEAT is left raw: 4.8% per sample needs no help, and
                # smoothing would blunt the transition edge that matters.
                # Everything else (pre + cool) is the ~107 mA idle bias at 24%
                # per sample, which is hash without it.
                # R noise scales as 1/I, so the heat phase is NOT uniformly
                # clean: 4.8% at 850 mA but ~17% at 150 mA (same absolute sense
                # noise against a 5.7x smaller signal). A light 20 ms window
                # brings the low levels to ~6% while costing only 5% of a
                # 400 ms pulse edge; the idle phases get the full 250 ms.
                idle = (grid < 0) | (grid > heat_end)
                sm = g.copy()
                sm[idle] = rolling_median(g[idle], max(3, int(SMOOTH_S * GRID_HZ)))
                sm[~idle] = rolling_median(g[~idle],
                                           max(3, int(SMOOTH_HEAT_S * GRID_HZ)))
                g = sm
            stack[k].append(g)
    out = {}
    for k, arr in stack.items():
        if arr:
            out[k] = np.nanmedian(np.vstack(arr), axis=0)
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
    fig.subplots_adjust(top=0.895, bottom=0.085, left=0.075, right=0.985,
                        hspace=0.42, wspace=0.20)

    heat_s = heat_ms / 1000.0
    for row, (key, ylab, subtitle) in enumerate(CHANNELS):
        for col, (t_lo, t_hi) in enumerate([(-0.15, DETAIL_S), (-PRE_S, POST_S)]):
            ax = axes[row, col]
            ax.set_facecolor(SURFACE)
            ax.axvspan(0, heat_s, color="#f2c9c0", alpha=0.55, lw=0, zorder=0)
            for lv in lv_sorted:
                y = traces[lv].get(key)
                if y is None:
                    continue
                if norm == "shape" and key != "power_W":
                    # POWER is never shape-normalized: it is the drive input,
                    # not a response, and dividing it by its own peak turns the
                    # low levels into hash around the idle bias (0.05 W idle
                    # against a 0.13 W peak at 150 mA reads as 0.4, not 0).
                    #
                    # Responses get a 100 ms rolling median FIRST. Dividing by
                    # a small peak amplifies noise with it -- at 150 mA the
                    # stroke is 42 um, so raw normalization swings +-0.5 and
                    # buries the shape being compared. The mechanical bandwidth
                    # here is ~1 Hz (rise 0.23-0.43 s), so 100 ms removes
                    # nothing real.
                    y = rolling_median(y, max(3, int(0.100 * GRID_HZ)))
                    pk = np.nanmax(np.abs(y))
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
             f"{len(lv_sorted)} current levels · median of the 5 non-bootstrap "
             f"cycles per level · shaded band = heat pulse\n{what}",
             ha="left", va="top", fontsize=10.5, color=INK_MUTED)

    out = os.path.join(BASE, f"trajectory_{heat_ms}ms_{norm}.png")
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
