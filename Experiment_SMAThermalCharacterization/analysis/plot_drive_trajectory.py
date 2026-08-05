"""CYCLE TRAJECTORIES, ANY SWEEP — resistance, power, stroke, force vs time.

    python plot_drive_trajectory.py                          # newest sweep, auto-grouped
    python plot_drive_trajectory.py --sweep sweep_20260805_105318
    python plot_drive_trajectory.py --heat 400               # just one pulse length
    python plot_drive_trajectory.py --by heat --level 200    # one current, all pulse lengths

Writes  drive_<sweep>_<400ms|200mA>.png — 4 channels x 2 time scales, series
overlaid and colour-ramped.

═══ WHICH AXIS THE COLOUR RUNS OVER ═══════════════════════════════════════
A full heat-time map wants one figure per pulse length coloured by CURRENT
(`--by level`). But a sweep that walks the heat axis at a single current — the
2026-08-05 extremes run has a 200 mA row across 100-400 ms — degenerates to one
trace per figure under that grouping. `--by heat` transposes it: one figure per
current, coloured by PULSE LENGTH, with the heat bands drawn nested so the
shading darkens over the span where every trace is still being driven.

`--by auto` (the default) applies both and keeps whichever groups produce more
than one trace, so a sweep carrying both shapes gets one figure of each kind and
no single-line figures.

═══ WHY THIS EXISTS ALONGSIDE plot_trajectory.py ══════════════════════════
Same four channels, two differences that matter:

  * plot_trajectory.py is pinned to the July long-wire campaign through a
    hardcoded SRC_MAP. This one takes ANY sweep folder and discovers its
    (level, heat) grid off the capture filenames, so a fresh run plots with no
    code edit.
  * MECHANICAL UNITS ARE THE BENCH'S, not the pipeline's: stroke in mm and
    force in GRAMS-FORCE, rather than µm and mN. Purely a relabelling of the
    same measurement (1 gf = 9.80665 mN); it reads against the coil's spec
    sheet and the calibration dead weights without arithmetic.

Nothing is selected or filtered; see NO DATA SELECTION in the module CLAUDE.md.

═══ CHANNELS ══════════════════════════════════════════════════════════════
  dR/R0 (%)   self-sensing signal, normalized to THAT CYCLE's own pre-fire
              median R so baseline drift between conditions is removed rather
              than folded into the trace. Absolute ohms on the right-hand axis
              — the same axis relabelled, not a second scale.
  power (W)   the drive input, V*I per sample. Deliberately NOT normalized:
              its spread across levels IS the independent variable.
  stroke (mm) displacement, delta vs the pre-fire baseline. Watch the
              RIGHT-HAND axis note: the laser amp maps only ~10.0 mm onto
              0-5 V, so a trace that flattens near that span is the amplifier
              railing, not the wire stopping.
  force (gf)  load cell, delta vs the pre-fire baseline. Full scale is 493 mN
              = 50.3 gf; same caution about flat tops.

═══ NOISE HANDLING ════════════════════════════════════════════════════════
Same position as plot_trajectory.py: NO temporal filtering. Cycles are combined
by MEDIAN across the non-bootstrap repeats — that combines independent
measurements of the same condition, it does not reshape one in time. R at the
idle bias genuinely carries large per-sample noise and no filter creates
information that was not measured. Cycle 1 (bootstrap, fires into a fully
relaxed wire) is held out; it is a different initial condition, not bad data.
"""
import argparse
import glob
import json
import os
import re
import warnings

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import cm, colors

from get_cycle import get_cycle, list_cycles

_HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(_HERE, "..", "data", "raw")
DERIVED = os.path.join(_HERE, "..", "data", "derived")

SURFACE = "#fafafa"
INK, INK_MUTED = "#1a1a1a", "#6b7280"
PRE_S, POST_S = 2.0, 29.0
DETAIL_S = 1.5
GRID_HZ = 400.0

MN_PER_GF = 9.80665        # 1 gram-force = 9.80665 mN, exactly
# Both sensors are 0-5 V into the ADC. A sample within V_RAIL_EPS of either end
# is the AMPLIFIER at its limit, not the wire: the reading has stopped tracking
# and everything after it in that cycle is suspect, because a saturated
# instrumentation amp does not recover instantly (measured: the load cell sits
# latched below its own baseline for ~2 s after coming off the top rail).
V_FS, V_RAIL_EPS = 5.0, 5e-3

CHANNELS = [
    ("dR_pct", "ΔR / R₀   (%)", "resistance — self-sensing"),
    ("power_W", "power  (W)", "electrical drive"),
    ("dx_mm", "stroke  Δx  (mm)", "displacement"),
    ("dF_gf", "force  ΔF  (gf)", "load cell"),
]

_NAME = re.compile(r"c\d+_level_([\d.]+)mA_h(\d+)ms\.csv$")


def discover(sweep):
    """{heat_ms: [levels]} straight off the capture filenames."""
    grid = {}
    for f in sorted(glob.glob(os.path.join(RAW, sweep, "c*_level_*mA_h*ms.csv"))):
        m = _NAME.search(os.path.basename(f))
        if m:
            grid.setdefault(int(m.group(2)), []).append(float(m.group(1)))
    return {h: sorted(v) for h, v in sorted(grid.items())}


def commanded_cool_s(sweep, default=30.0):
    """The COMMANDED cool time, off the first meta.json. Not the same as
    POST_S, which is only how much of it this figure draws."""
    for f in sorted(glob.glob(os.path.join(RAW, sweep, "c*.meta.json"))):
        try:
            return float(json.load(open(f))["cool_s"])
        except Exception:
            continue
    return default


def latest_sweep():
    hits = sorted(glob.glob(os.path.join(RAW, "sweep_*")))
    if not hits:
        raise SystemExit("no sweep_* folders under data/raw")
    return os.path.basename(hits[-1])


def railed(v):
    """Did this 0-5 V channel touch either rail anywhere in the cycle?"""
    v = np.asarray(v, dtype=float)
    return bool(np.nanmax(v) >= V_FS - V_RAIL_EPS or np.nanmin(v) <= V_RAIL_EPS)


def condition_trace(sweep, level_mA, heat_ms, grid):
    """Median trajectory over the non-bootstrap cycles, on a common grid.

    ONE-PULSE CONDITIONS. A repeated condition holds cycle 1 out: it fires into
    a fully relaxed wire, so it is a different initial condition, not bad data.
    But the randomized protocol (`cycles: 0`, DATA_COLLECTION_GUIDELINE §9.2)
    fires each condition EXACTLY ONCE so consecutive pulses differ in
    condition — there is no repeat to average and the single pulse IS the
    measurement. `range(2, n_cyc + 1)` is empty in that case and the old
    `n_cyc < 2` guard returned None, so every capture of a randomized campaign
    would have plotted as "no usable cycles" — silently, for the whole run.
    """
    try:
        n_cyc = len(list_cycles(sweep, level_mA, heat_ms))
    except Exception:
        return None
    if n_cyc < 1:
        return None
    single = (n_cyc == 1)
    use = [1] if single else range(2, n_cyc + 1)
    stack = {k: [] for k, _, _ in CHANNELS}
    r0s = []
    rail = {"dx_mm": 0, "dF_gf": 0}          # cycles in which that amp saturated
    for c in use:
        try:
            df = get_cycle(sweep, level_mA, heat_ms, cycle=c,
                           pre_s=PRE_S, post_s=POST_S)
        except Exception:
            continue
        if "power_W" not in df or "sma_r" not in df:
            continue
        t = df.t_s.to_numpy()
        pre = df[df.phase == "pre"]
        if pre.empty:
            continue
        r0 = float(np.median(pre.sma_r.dropna())) if pre.sma_r.notna().any() else np.nan
        r0s.append(r0)
        for key, raw in (("dx_mm", "laser"), ("dF_gf", "load")):
            if raw in df and railed(df[raw].to_numpy()):
                rail[key] += 1
        cols = {
            "dR_pct": 100.0 * (df.sma_r.to_numpy() / r0 - 1.0) if r0 == r0 else None,
            "power_W": df.power_W.to_numpy(),
            # Both mechanical channels are deltas vs the pre-fire MEAN, so a
            # constant grip preload drops out and only the actuation shows.
            "dx_mm": (df.laser_mm.to_numpy() - pre.laser_mm.mean())
                     if "laser_mm" in df else None,
            "dF_gf": (df.force_mN.to_numpy() - pre.force_mN.mean()) / MN_PER_GF
                     if "force_mN" in df else None,
        }
        for k, y in cols.items():
            if y is None:
                continue
            stack[k].append(np.interp(grid, t, y, left=np.nan, right=np.nan))
    out = {}
    for k, arr in stack.items():
        if arr:
            # All-NaN columns are expected at the grid edges: a cycle's capture
            # can end a few samples short of POST_S. They plot as gaps.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                out[k] = np.nanmedian(np.vstack(arr), axis=0)
    if out and r0s:
        out["_r0"] = float(np.nanmedian(r0s))
        out["_n"] = len(stack["power_W"])
        out["_rail"] = rail
        out["_single"] = single
    return out or None


def build(sweep, by, fixed, series):
    """One figure. `by` names the axis the SERIES (and colour) runs over:

        by="level"  fixed = heat_ms, series = current levels   (the usual view)
        by="heat"   fixed = level_mA, series = pulse lengths

    The second is what a single-current row needs — a sweep that walks the heat
    axis at one current has one trace per figure under the usual grouping, which
    shows nothing. Here the pulse length becomes the ordinal variable instead.
    """
    lvl_axis = (by == "level")
    grid = np.arange(-PRE_S, POST_S, 1.0 / GRID_HZ)
    unit = "mA" if lvl_axis else "ms"
    traces = {}
    for s in series:
        lv, heat_ms = (s, fixed) if lvl_axis else (fixed, s)
        tr = condition_trace(sweep, lv, heat_ms, grid)
        if tr:
            traces[s] = tr
            r = tr.get("_rail", {})
            flag = "  RAILED: " + ", ".join(k for k, n in r.items() if n) \
                if any(r.values()) else ""
            print(f"     {s:>5.0f} {unit}  ok  ({tr.get('_n', 0)} cycles){flag}")
        else:
            print(f"     {s:>5.0f} {unit}  --  no usable cycles")
    if not traces:
        return None

    lv_sorted = sorted(traces)
    base = matplotlib.colormaps["Blues"]
    # Sample the ramp by INDEX, not by value: starting at 0.22 keeps the
    # lightest step visible against the surface, and the bar has no dead range.
    lut = [base(0.22 + 0.78 * i / max(1, len(lv_sorted) - 1))
           for i in range(len(lv_sorted))]
    cmap = colors.ListedColormap(lut)
    # A single-series figure has no spacing to derive; any positive half-width
    # works, it just has to be non-zero or BoundaryNorm gets a degenerate edge.
    step = ((lv_sorted[-1] - lv_sorted[0]) / (len(lv_sorted) - 1)
            if len(lv_sorted) > 1 else (100.0 if lvl_axis else 50.0))
    bounds = ([lv_sorted[0] - step / 2]
              + [(a + b) / 2 for a, b in zip(lv_sorted, lv_sorted[1:])]
              + [lv_sorted[-1] + step / 2])
    norm_c = colors.BoundaryNorm(bounds, cmap.N)

    fig, axes = plt.subplots(len(CHANNELS), 2, figsize=(13.5, 13.6),
                             gridspec_kw={"width_ratios": [1, 1.35]})
    fig.patch.set_facecolor(SURFACE)
    fig.subplots_adjust(top=0.895, bottom=0.085, left=0.075, right=0.945,
                        hspace=0.42, wspace=0.20)

    r0_all = [t["_r0"] for t in traces.values() if t.get("_r0") == t.get("_r0")]
    r0_med = float(np.median(r0_all)) if r0_all else np.nan
    # One band per pulse length present. When the series IS pulse length they
    # nest, and the shading darkens over the span where every trace is still
    # being driven — which is exactly the region the traces should be compared
    # over.
    heat_bands = [fixed / 1000.0] if lvl_axis else [s / 1000.0 for s in lv_sorted]
    for row, (key, ylab, subtitle) in enumerate(CHANNELS):
        for col, (t_lo, t_hi) in enumerate([(-0.15, DETAIL_S), (-PRE_S, POST_S)]):
            ax = axes[row, col]
            ax.set_facecolor(SURFACE)
            a_band = 0.55 if len(heat_bands) == 1 else 0.20
            for hb in heat_bands:
                ax.axvspan(0, hb, color="#f2c9c0", alpha=a_band, lw=0, zorder=0)
            for lv in lv_sorted:
                y = traces[lv].get(key)
                if y is None:
                    continue
                m = (grid >= t_lo) & (grid <= t_hi)
                # A level whose amp saturated is drawn DOTTED, not dropped.
                # Same position as the pipeline's `railed` column: report the
                # defect, do not decide for the reader that the cycle is gone.
                bad = traces[lv].get("_rail", {}).get(key, 0) > 0
                ax.plot(grid[m], y[m], lw=1.5, color=cmap(norm_c(lv)),
                        ls=":" if bad else "-", zorder=3 if bad else 2)
            bad_lv = [lv for lv in lv_sorted
                      if traces[lv].get("_rail", {}).get(key, 0) > 0]
            if bad_lv and col == 0:
                ax.text(0.985, 0.06,
                        "dotted = amp railed at 0/5 V — "
                        + ", ".join(f"{v:.0f}" for v in bad_lv) + f" {unit}",
                        transform=ax.transAxes, ha="right", va="bottom",
                        fontsize=9, color="#b4472e", zorder=4)
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
                ax.set_ylabel(ylab, fontsize=10.5, color=INK)
                ax.set_title(subtitle, loc="left", fontsize=11, color=INK, pad=6)
            else:
                ax.set_title("full cycle incl. cooling", loc="left",
                             fontsize=10, color=INK_MUTED, pad=6)
            if key == "dR_pct" and r0_med == r0_med:
                # Absolute ohms on the right. NOT a second scale -- the same
                # axis relabelled, since R = R0*(1 + dR/R0). The percentage is
                # what keeps levels comparable when R0 drifts between them.
                axr = ax.twinx()
                axr.set_ylim([r0_med * (1 + v / 100.0) for v in ax.get_ylim()])
                axr.set_ylabel(f"R  (Ω)   ·   R₀ = {r0_med:.2f} Ω",
                               fontsize=9.5, color=INK_MUTED)
                axr.tick_params(colors=INK_MUTED, labelsize=9)
                for sd in ("top", "left", "bottom"):
                    axr.spines[sd].set_visible(False)
                axr.spines["right"].set_color("#c9ced6")
            if row == len(CHANNELS) - 1:
                ax.set_xlabel("time since heat onset (s)", fontsize=10.5, color=INK)

    sm = cm.ScalarMappable(cmap=cmap, norm=norm_c)
    cb = fig.colorbar(sm, ax=axes, orientation="horizontal", fraction=0.028,
                      pad=0.070, aspect=55)
    cb.set_label(("commanded current (mA)" if lvl_axis else "pulse length (ms)")
                 + "  ·  light → dark = low → high",
                 fontsize=10.5, color=INK)
    cb.set_ticks(lv_sorted)
    cb.ax.tick_params(colors=INK_MUTED, labelsize=9.5)
    cb.outline.set_visible(False)

    n_cyc = max((t.get("_n", 0) for t in traces.values()), default=0)
    head = (f"{fixed} ms pulse" if lvl_axis else f"{fixed:.0f} mA drive")
    what = ("current levels" if lvl_axis else "pulse lengths")
    # Never claim a median that was not taken. Under the randomized protocol
    # each condition fires once, so the trace is that one pulse.
    single = all(t.get("_single") for t in traces.values())
    how = ("1 pulse per condition (randomized protocol)" if single else
           f"median of the {n_cyc} repeat cycles per condition "
           f"(bootstrap held out)")
    # Both anchored va="top" from an explicit y. Without va the default is
    # "baseline", which puts a multi-line block's FIRST line ABOVE its y and
    # straight through the title.
    fig.text(0.075, 0.990,
             f"SMA electrical drive — {head}, {commanded_cool_s(sweep):.0f} s cool",
             ha="left", va="top", fontsize=16, weight="semibold", color=INK)
    fig.text(0.075, 0.966,
             f"{sweep} · {len(lv_sorted)} {what} · RAW, no temporal "
             f"filtering · {how} · shaded = heat pulse\n"
             f"physical units — stroke in mm, force in grams-force; power "
             f"deliberately NOT normalized, its spread is the independent "
             f"variable",
             ha="left", va="top", fontsize=10.5, color=INK_MUTED)

    tag = f"{fixed}ms" if lvl_axis else f"{fixed:.0f}mA"
    out = os.path.join(DERIVED, f"drive_{sweep}_{tag}.png")
    fig.savefig(out, dpi=140, facecolor=SURFACE)
    plt.close(fig)
    print(f"  -> {os.path.basename(out)}  ({len(lv_sorted)} series)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", default=None, help="capture folder under data/raw")
    ap.add_argument("--by", choices=["level", "heat", "auto"], default="auto",
                    help="what the colour ramp runs over. 'level' = one figure "
                         "per pulse length, coloured by current (the usual "
                         "view). 'heat' = one figure per current, coloured by "
                         "pulse length — what a single-current row needs. "
                         "'auto' picks whichever grouping actually yields "
                         "multi-trace figures, so a sweep carrying both shapes "
                         "(a heat row AND a current row) gets both.")
    ap.add_argument("--heat", type=int, default=None, help="one pulse length only")
    ap.add_argument("--level", type=float, default=None, help="one current only")
    ap.add_argument("--all", action="store_true",
                    help="every sweep_* folder under data/raw, so the standard "
                         "figure set can be regenerated in one command (an "
                         "overnight campaign lands as a dozen folders)")
    a = ap.parse_args()

    if a.all:
        folders = sorted(os.path.basename(p) for p in
                         glob.glob(os.path.join(RAW, "sweep_*"))
                         if os.path.isdir(p))
        if not folders:
            raise SystemExit(f"no sweep_* folders under {RAW}")
        rc, made = 0, 0
        for f in folders:
            print(f"\n=== {f}")
            try:
                made += run_one(f, a)
            except SystemExit as e:            # keep going over a bad folder
                print(f"  skipped — {e}")
                rc = 1
            except Exception as e:             # noqa: BLE001
                print(f"  ERROR — {e}")
                rc = 1
        print(f"\n{made} figure(s) over {len(folders)} sweep folder(s)")
        return rc
    return 0 if run_one(a.sweep or latest_sweep(), a) else 1


def run_one(sweep, a):
    """Figures for ONE sweep folder. Returns how many were written."""
    gridmap = discover(sweep)
    if not gridmap:
        raise SystemExit(f"no captures found in data/raw/{sweep}")

    # by-level groups: heat_ms -> levels.   by-heat groups: level_mA -> heats.
    by_lvl = {h: v for h, v in gridmap.items()}
    by_heat = {}
    for h, lvs in gridmap.items():
        for lv in lvs:
            by_heat.setdefault(lv, []).append(h)
    by_heat = {lv: sorted(v) for lv, v in sorted(by_heat.items())}

    if a.by == "auto":
        # Prefer the by-level view — it is the module's established reading of a
        # heat-time map — and fall back to by-heat ONLY for cells it cannot
        # show. A full 32-cell grid is covered entirely by its four by-level
        # figures, so it gets exactly those and no transposed duplicates. The
        # extremes sweep's 200 mA row is one trace per pulse length, so those
        # four cells stay uncovered and become a single by-heat figure.
        by_lvl = {h: v for h, v in by_lvl.items() if len(v) > 1}
        covered = {(lv, h) for h, lvs in by_lvl.items() for lv in lvs}
        by_heat = {lv: [h for h in v if (lv, h) not in covered]
                   for lv, v in by_heat.items()}
        by_heat = {lv: v for lv, v in by_heat.items() if len(v) > 1}
        if not by_lvl and not by_heat:          # nothing groups; plot it flat
            by_lvl = gridmap
    elif a.by == "level":
        by_heat = {}
    else:
        by_lvl = {}

    if a.heat is not None:
        by_lvl = {h: v for h, v in by_lvl.items() if h == a.heat}
        by_heat = {lv: [h for h in v if h == a.heat] for lv, v in by_heat.items()}
        by_heat = {lv: v for lv, v in by_heat.items() if v}
    if a.level is not None:
        by_heat = {lv: v for lv, v in by_heat.items() if lv == a.level}
        by_lvl = {h: [l for l in v if l == a.level] for h, v in by_lvl.items()}
        by_lvl = {h: v for h, v in by_lvl.items() if v}

    made = 0
    for h in sorted(by_lvl):
        print(f"  {h} ms pulse:")
        made += bool(build(sweep, "level", h, by_lvl[h]))
    for lv in sorted(by_heat):
        print(f"  {lv:.0f} mA drive:")
        made += bool(build(sweep, "heat", lv, by_heat[lv]))
    if not made:
        print("  no figure written — no condition yielded a usable trace")
    return made


if __name__ == "__main__":
    raise SystemExit(main())
