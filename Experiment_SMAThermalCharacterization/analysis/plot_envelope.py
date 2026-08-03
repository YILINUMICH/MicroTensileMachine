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
HEATS = [100, 200, 300, 400]
# Validated sequential ramp — see the COLOR note above before changing these.
RAMP = {100: "#6baed6", 200: "#2171b5", 300: "#08306b", 400: "#041229"}
SURFACE = "#fafafa"
INK, INK_MUTED = "#1a1a1a", "#6b7280"
K_MV_PER_UM = -0.49779577092171906
V0_MV = 2503.7500968693835
LASER_RAIL_UM = (0.0 - V0_MV) / K_MV_PER_UM


def aggregate(d):
    rows = []
    for h in HEATS:
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
                # a measurement the instrument could not make
                "saturated": int(((s.clipped == 1) | (s.railed == 1)).sum() * 2 > len(s)),
                "cool_s": float(s.cool_s.median()),
            })
    return pd.DataFrame(rows)


def chart(d, env, ycol_pt, ycol_med, ylabel, title, subtitle, out,
          ceiling=None, ceiling_label=None):
    fig, ax = plt.subplots(figsize=(11, 7))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    if ceiling is not None:
        ax.axhline(ceiling, ls=(0, (6, 4)), lw=1.4, color="#9aa1ab", zorder=1)
        ax.annotate(ceiling_label, xy=(0.012, ceiling), xycoords=("axes fraction", "data"),
                    xytext=(0, 6), textcoords="offset points",
                    va="bottom", ha="left", fontsize=10, color=INK_MUTED)

    for h in HEATS:
        c = RAMP[h]
        pts = d[d.heat_ms == h]
        if pts.empty:
            continue
        sat = (pts.clipped == 1) | (pts.railed == 1)
        # every cycle is drawn; hollow = at a sensor rail, so a lower bound
        ax.scatter(pts.level_mA[~sat], pts[ycol_pt][~sat], s=26, color=c,
                   alpha=0.38, linewidths=0, zorder=2)
        ax.scatter(pts.level_mA[sat], pts[ycol_pt][sat], s=34,
                   facecolors="none", edgecolors=c, linewidths=1.3, zorder=2)

        e = env[env.heat_ms == h].sort_values("level_mA")
        xs, ys, sat_m = e.level_mA.to_numpy(), e[ycol_med].to_numpy(), e.saturated.to_numpy()
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
    src = argv[0] if argv else os.path.join(DERIVED, "heat_time_map_20260731_all.csv")
    stem = src[:-4]
    d = pd.read_csv(src)
    env = aggregate(d)
    env.to_csv(stem + "_envelope.csv", index=False)
    print(f"  -> {os.path.basename(stem)}_envelope.csv "
          f"({len(env)} conditions, {len(d)} cycles, none excluded)")

    n_sat = int(((d.clipped == 1) | (d.railed == 1)).sum())
    cool = d.cool_s.median()
    sub = (f"{len(d)} cycles over {len(env)} conditions, {cool:.0f} s cool throughout"
           f" — every cycle plotted, none removed\n"
           f"line = median, first-cycle set held out · hollow + dashed = at a "
           f"sensor rail ({n_sat} cycles): lower bounds, not measurements")

    ceil_um = LASER_RAIL_UM - float(d.x_base_um.median())
    chart(d, env, "dx_um", "dx_um_median", "contraction stroke  Δx  (µm)",
          "SMA actuation envelope — stroke vs current and pulse length", sub,
          stem + "_stroke.png",
          ceiling=ceil_um,
          ceiling_label="laser 0 V rail — stroke available from typical rest")
    chart(d, env, "dF_mN", "dF_mN_median", "force rise  ΔF  (mN)",
          "SMA actuation envelope — force vs current and pulse length", sub,
          stem + "_force.png",
          ceiling=490.0, ceiling_label="load cell full scale (490 mN)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
