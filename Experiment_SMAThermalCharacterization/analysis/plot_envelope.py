"""PER-CYCLE TABLE -> ENVELOPE CHARTS.  Stage 2 of the standing pipeline.

    python plot_envelope.py [heat_time_map_20260731_all.csv]

Writes, next to the input:
    <stem>_envelope.csv            per-(level, heat) aggregates
    <stem>_stroke.png              stroke vs current, by pulse length
    <stem>_force.png               force rise vs current, by pulse length

═══ NO DATA SELECTION ═══════════════════════════════════════════════════════
EVERY cycle in the input is plotted as a dot, including clipped, railed,
sub-threshold and bootstrap cycles. The line is the median of the cycles at
that (level, heat) — a summary of what is plotted, not a filtered subset.

Saturated cycles are drawn as HOLLOW dots. That is annotation, not exclusion:
at the rails `dx`/`dF` are lower bounds (the wire moved at least that far), so
they are honest data points that must not be read as exact. The dashed ceiling
line shows where the laser instrument runs out, which is what makes those
points bend over.

Only the bootstrap cycle is held out of the MEDIAN, and it is still drawn. It
fires into a fully relaxed wire and takes a one-time set (~+370 um at 850x400),
so it measures a genuinely different initial condition; pooling it into a
central value would blur two populations. The dots show it, the line doesn't
average it.

═══ COLOR ══════════════════════════════════════════════════════════════════
Pulse length is ORDINAL, so it gets a single-hue light->dark sequential ramp,
not categorical hues. The four steps are validated, not eyeballed: adjacent
OKLab dE (x100) normal / protanope / deuteranope =
  100->200  19.1/19.2/19.1     200->300  21.6/22.7/21.4     300->400 15.0/15.5/15.0
against a >=15 normal floor and >=8 CVD target, lightness strictly monotonic,
contrast vs the surface 2.33:1 .. 17.9:1. The obvious ColorBrewer pick
(9ecae1/4292c6/2171b5/08306b) FAILS: its 200->300 pair separates by only 10.0,
which is why those two series are hard to tell apart by eye.

Identity is never carried by color alone — every series is direct-labeled at
its right end AND present in the legend.

Cool time is NOT encoded: the whole campaign ran 30 s, so the marker-shape
split used for the mixed 15/25/30 s data of 2026-07-30 would encode nothing.
"""
import os
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

_HERE = os.path.dirname(os.path.abspath(__file__))
DERIVED = os.path.join(_HERE, "..", "data", "derived")   # pipeline outputs

from analyze_raw import CAMPAIGNS, derived_dir  # noqa: E402
# HEAT TIMES COME FROM THE DATA, never from a constant. A hardcoded
# `HEATS = [100, 200, 300, 400]` silently dropped every cycle at any other heat
# time: the 2026-08-05 Dynalloy campaign added a 500 ms row, and `aggregate()`
# iterating the constant discarded all 48 of its cycles — 8 of 44 conditions —
# while this script printed "none excluded". That is exactly the data selection
# the module's NO DATA SELECTION rule forbids, so the heat list is now derived
# from the table and the ramp is built to fit it.
#
# Validated sequential ramp anchors — see the COLOR note above before changing.
# Sampling these at N points reproduces the four VALIDATED colours exactly when
# N == 4. A longer ramp keeps the single hue and stays monotone in lightness by
# construction, but has NOT been through validate_palette.py — re-run it before
# treating a 5+-step ramp as verified.
RAMP_ANCHORS = ["#6baed6", "#2171b5", "#08306b", "#041229"]
SURFACE = "#fafafa"
INK, INK_MUTED = "#1a1a1a", "#6b7280"
K_MV_PER_UM = -0.49779577092171906
V0_MV = 2503.7500968693835
LASER_RAIL_UM = (0.0 - V0_MV) / K_MV_PER_UM


def heats_of(d):
    """The heat times actually present, ascending. Ordinal, so order matters."""
    return sorted(int(h) for h in d.heat_ms.unique())


def build_ramp(heats):
    """Map each heat time onto the sequential ramp, light -> dark.

    Linear interpolation along RAMP_ANCHORS: with four heats the sample points
    land exactly on the anchors, so existing figures are unchanged.
    """
    def _rgb(hx):
        return [int(hx[i:i + 2], 16) for i in (1, 3, 5)]

    anchors = [_rgb(c) for c in RAMP_ANCHORS]
    n = len(heats)
    out = {}
    for k, h in enumerate(heats):
        t = 0.0 if n == 1 else k / (n - 1) * (len(anchors) - 1)
        lo = min(int(t), len(anchors) - 2)
        f = t - lo
        rgb = [round(anchors[lo][j] + f * (anchors[lo + 1][j] - anchors[lo][j]))
               for j in range(3)]
        out[h] = "#{:02x}{:02x}{:02x}".format(*rgb)
    return out


def aggregate(d):
    rows = []
    for h in heats_of(d):
        for lv in sorted(d.level_mA.unique()):
            s = d[(d.heat_ms == h) & (d.level_mA == lv)]
            if s.empty:
                continue
            body = s[s.bootstrap == 0]
            if body.empty:
                body = s
            rows.append({
                "heat_ms": h, "level_mA": lv,
                "n_cycles": len(s), "n_in_median": len(body),
                "i_mA_mean": round(float(s.i_mA.mean()), 1),
                "dx_um_median": round(float(body.dx_um.median()), 1),
                "dx_um_min": round(float(s.dx_um.min()), 1),
                "dx_um_max": round(float(s.dx_um.max()), 1),
                "dF_mN_median": round(float(body.dF_mN.median()), 2),
                "n_clipped": int(s.clipped.sum()),
                "n_railed": int(s.railed.sum()),
                # majority of cycles at a rail -> the median is a LOWER BOUND,
                # drawn hollow with a dashed approach so the line never asserts
                # a measurement the instrument could not make.
                #
                # SATURATION IS PER CHANNEL. `railed` is the LASER leaving its
                # window, which bounds dx; `clipped` is the LOAD CELL at 5 V,
                # which bounds dF. They are not interchangeable: the 08-05
                # Dynalloy campaign railed the laser 0 times and clipped force
                # 28 times, so a combined flag drew the whole top of the STROKE
                # chart as "lower bounds, not measurements" when every one of
                # those displacements is exact. Keep `saturated` as the union
                # for backward compatibility with anything reading the CSV.
                "saturated": int(((s.clipped == 1) | (s.railed == 1)).sum() * 2 > len(s)),
                "saturated_dx": int((s.railed == 1).sum() * 2 > len(s)),
                "saturated_dF": int((s.clipped == 1).sum() * 2 > len(s)),
                "cool_s": float(s.cool_s.median()),
            })
    return pd.DataFrame(rows)


def chart(d, env, ycol_pt, ycol_med, ylabel, title, subtitle, out,
          ceiling=None, ceiling_label=None, satcol="railed",
          satcol_med="saturated_dx"):
    fig, ax = plt.subplots(figsize=(11, 7))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    if ceiling is not None:
        ax.axhline(ceiling, ls=(0, (6, 4)), lw=1.4, color="#9aa1ab", zorder=1)
        ax.annotate(ceiling_label, xy=(0.012, ceiling), xycoords=("axes fraction", "data"),
                    xytext=(0, 6), textcoords="offset points",
                    va="bottom", ha="left", fontsize=10, color=INK_MUTED)

    ramp = build_ramp(heats_of(d))
    for h in heats_of(d):
        c = ramp[h]
        pts = d[d.heat_ms == h]
        if pts.empty:
            continue
        sat = pts[satcol] == 1          # the rail that bounds THIS channel
        # every cycle is drawn; hollow = at a sensor rail, so a lower bound
        ax.scatter(pts.level_mA[~sat], pts[ycol_pt][~sat], s=26, color=c,
                   alpha=0.38, linewidths=0, zorder=2)
        ax.scatter(pts.level_mA[sat], pts[ycol_pt][sat], s=34,
                   facecolors="none", edgecolors=c, linewidths=1.3, zorder=2)

        e = env[env.heat_ms == h].sort_values("level_mA")
        xs, ys = e.level_mA.to_numpy(), e[ycol_med].to_numpy()
        sat_m = e[satcol_med].to_numpy()
        for k in range(len(xs) - 1):        # dash any segment reaching a rail
            style = "--" if (sat_m[k] or sat_m[k + 1]) else "-"
            ax.plot(xs[k:k + 2], ys[k:k + 2], style, color=c, lw=2.0, zorder=3)
        ax.plot(xs[~sat_m.astype(bool)], ys[~sat_m.astype(bool)], "o", color=c,
                ms=6.5, markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=4)
        ax.plot(xs[sat_m.astype(bool)], ys[sat_m.astype(bool)], "o", ms=7,
                markerfacecolor=SURFACE, markeredgecolor=c, markeredgewidth=1.8,
                zorder=4)
        ax.plot([], [], "-o", color=c, lw=2.0, ms=6.5, label=f"{h} ms")
        last = e.iloc[-1]
        ax.annotate(f"{h} ms", xy=(last.level_mA, last[ycol_med]),
                    xytext=(9, 0), textcoords="offset points",
                    va="center", ha="left", fontsize=11, color=c, weight="medium")

    ax.set_xlabel("commanded current (mA)", fontsize=11.5, color=INK)
    ax.set_ylabel(ylabel, fontsize=11.5, color=INK)
    ax.set_xticks(sorted(d.level_mA.unique()))
    ax.set_xlim(min(d.level_mA) - 60, max(d.level_mA) + 105)
    # headroom so the ceiling line + its label clear the top series
    top = max(ax.get_ylim()[1], (ceiling or 0) * 1.10)
    ax.set_ylim(ax.get_ylim()[0], top)
    ax.grid(axis="y", color="#e3e6ea", lw=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c9ced6")
    ax.tick_params(colors=INK_MUTED, labelsize=10.5)

    # anchored BELOW the ceiling annotation so the two never collide
    leg = ax.legend(title="heat pulse", loc="upper left",
                    bbox_to_anchor=(0.012, 0.86), frameon=False,
                    fontsize=10.5, title_fontsize=10.5)
    leg.get_title().set_color(INK_MUTED)
    for t in leg.get_texts():
        t.set_color(INK)

    fig.suptitle(title, x=0.055, y=0.972, ha="left", fontsize=15.5,
                 weight="semibold", color=INK)
    ax.set_title(subtitle, loc="left", fontsize=10.5, color=INK_MUTED, pad=14)
    fig.tight_layout(rect=(0, 0, 1, 0.955))
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"  -> {os.path.basename(out)}")


def main(argv):
    # Outputs land NEXT TO THE INPUT, so a campaign's envelope files follow
    # its table into data/derived/campaigns/<dir>/ with no path logic here.
    # The no-argument default is still the JULY campaign: passing the table
    # you actually want is not optional for any other one.
    _J = CAMPAIGNS["20260731"]
    src = argv[0] if argv else os.path.join(derived_dir(_J["dir"]), _J["merged"])
    stem = src[:-4]
    d = pd.read_csv(src)
    env = aggregate(d)
    env.to_csv(stem + "_envelope.csv", index=False)
    print(f"  -> {os.path.basename(stem)}_envelope.csv "
          f"({len(env)} conditions, {len(d)} cycles, none excluded)")

    cool = d.cool_s.median()
    cools = sorted(d.cool_s.unique())
    cool_txt = (f"{cool:.0f} s cool throughout" if len(cools) == 1
                else f"cool {min(cools):.0f}-{max(cools):.0f} s")

    def sub_for(n_sat, rail):
        """Each chart names ITS OWN rail and count — a shared subtitle
        attributed force clipping to the laser and vice versa."""
        return (f"{len(d)} cycles over {len(env)} conditions, {cool_txt}"
                f" — every cycle plotted, none removed\n"
                f"line = median, first-cycle set held out · hollow + dashed = "
                f"{rail} ({n_sat} cycles): lower bounds, not measurements")

    ceil_um = LASER_RAIL_UM - float(d.x_base_um.median())
    chart(d, env, "dx_um", "dx_um_median", "contraction stroke  Δx  (µm)",
          "SMA actuation envelope — stroke vs current and pulse length",
          sub_for(int((d.railed == 1).sum()), "laser out of window"),
          stem + "_stroke.png",
          ceiling=ceil_um,
          ceiling_label="laser 0 V rail — stroke available from typical rest",
          satcol="railed", satcol_med="saturated_dx")
    chart(d, env, "dF_mN", "dF_mN_median", "force rise  ΔF  (mN)",
          "SMA actuation envelope — force vs current and pulse length",
          sub_for(int((d.clipped == 1).sum()), "load cell at the 5 V rail"),
          stem + "_force.png",
          ceiling=490.0, ceiling_label="load cell full scale (490 mN)",
          satcol="clipped", satcol_med="saturated_dF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
