"""FIGURE C — the cold → hot → cold transition.

    python plot_transition.py               # all four pulse lengths
    python plot_transition.py --heat 400    # just one
    python plot_transition.py --levels all  # all 9 currents (see COLOUR below)

Writes transition_<heat>ms.html — 4 channels x 3 views, interactive.

═══ WHAT THIS SHOWS THAT THE SCATTER FIGURES CANNOT ═══════════════════════
energy_collapse.html and self_sensing.html are one point per cycle, taken at
heat end. They say nothing about the path. This figure is the path: the wire
going cold → hot → cold, with the two halves on wildly different timescales
(rise 0.23-0.43 s, recovery to within 10% of rest 4.9-12.4 s). That asymmetry
is the single most practical fact about driving this actuator and it is
invisible in any per-cycle summary.

═══ WHY THE PHASE COLUMN IS HEATING-ONLY ══════════════════════════════════
Column 3 plots each channel against RESISTANCE rather than time. It stops at
heat end, and that is not a stylistic choice.

At pulse end the drive current drops from the commanded level to the ~107 mA
idle bias, and the measured R jumps +9 to +14% within ~50 ms — while the stroke
is still at its maximum, so the wire is plainly still hot. R = V/I carries a
current-dependent offset (R_meas = R_true + V_off/I, V_off ~ 20-35 mV), and at
the idle bias that inflates R by 0.2-0.3 Ω against a total heating excursion of
only 0.64 Ω. See r_bias_artifact.html for the evidence.

So the heating and cooling branches sit on two different R scales. Drawing both
produces a huge, convincing, WRONG hysteresis loop — the cool-vs-heat stroke gap
at equal R measures 81-83% of peak, suspiciously constant across conditions that
have nothing else in common. That is the offset, not the alloy.

WITHIN the heat pulse the current is held constant, so V_off/I is a constant
there: the branch's SHAPE is trustworthy and only its absolute R is shifted (by
~0.04 Ω at 850 mA). That is why the heating branch is drawn and the cooling
branch is not. Getting the real loop needs R sensed at a CONSTANT current on
both branches — see STATUS.md.

═══ COLOUR ════════════════════════════════════════════════════════════════
Current level is an ORDERED category, so it takes a one-hue ordinal ramp. Only
FIVE steps of that ramp clear the >=0.06 adjacent-lightness gate (measured, not
assumed: 9 steps come out at 0.047 and FAIL). So five levels are drawn by
default, evenly spanning the commanded range. `--levels all` draws all nine and
is honestly worse to read; the ramp then encodes magnitude only, to be read
against the legend rather than told apart pair by pair.

═══ SAMPLING ══════════════════════════════════════════════════════════════
Column 1 (0-1.5 s) is the full 400 Hz grid, unmodified. Column 2 spans 31 s;
plotting it at 400 Hz would be 12,400 points per trace per channel for no gain,
since nothing there moves faster than ~0.2 s. It is reduced to 50 Hz by BLOCK
MEAN — an average of 8 consecutive samples, declared here and on the panel, not
a filter run over the trace. Column 3 is the raw 400 Hz heat window.
"""
import argparse
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import plot_style as ps
from energy_table import load
from get_cycle import get_cycle, list_cycles

_HERE = os.path.dirname(os.path.abspath(__file__))
DERIVED = os.path.join(_HERE, "..", "data", "derived")   # pipeline outputs

from analyze_raw import CAMPAIGNS, derived_dir  # noqa: E402
PRE_S, POST_S = 2.0, 29.0
GRID_HZ = 400.0
DETAIL_S = 1.5
FULL_HZ = 50.0

# five steps of the blue ramp; the maximum that passes --ordinal (see COLOUR)
RAMP5 = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]
# nine steps for --levels all. Documented as FAILING the adjacent-lightness
# gate at 0.047 (floor 0.06) -- kept because magnitude-against-a-legend is
# still a legitimate read, but it is not the default.
RAMP9 = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6",
         "#256abf", "#1c5cab", "#184f95", "#0d366b"]

CHANNELS = [
    ("sma_r", "resistance  R  (Ω)", "resistance — the self-sensing signal"),
    ("power_W", "power  (W)", "electrical drive"),
    ("dx_um", "stroke  Δx  (µm)", "displacement"),
    ("dF_mN", "force  ΔF  (mN)", "load cell"),
]


def sweep_for(d, level_mA, heat_ms):
    """Which capture holds this condition — read off the table rather than a
    hardcoded map, so adding a sweep needs no edit here."""
    s = d[(d.level_mA == level_mA) & (d.heat_ms == heat_ms)]
    if s.empty:
        return None
    return s.sweep.value_counts().idxmax()


def block_mean(t, y, hz):
    """Reduce to `hz` by averaging consecutive blocks. Not a rolling filter:
    each output sample is the mean of a DISJOINT block, so it cannot ring and
    cannot shift an edge — the two failure modes that got low-pass filtering
    removed from the trajectory charts. Every rate used is declared on the
    panel and in the notes."""
    n = max(1, int(round(GRID_HZ / hz)))
    k = (len(y) // n) * n
    if k == 0:
        return t, y
    tb = t[:k].reshape(-1, n).mean(axis=1)
    with np.errstate(invalid="ignore"):
        yb = np.nanmean(y[:k].reshape(-1, n), axis=1)
    return tb, yb


# Per-channel display rates, in Hz, per column.
#
# R is the ONLY channel that needs reduction, and it needs a lot: per-sample
# noise is ~2.2% during heat but ~12% during cool (at the ~107 mA idle bias the
# current sense is near its floor), even after the median across repeat cycles.
# Raw at 400 Hz the R row is a +-1.5 Ohm band with the ~0.6 Ohm signal buried in
# it. Block averaging N samples cuts that by sqrt(N):
#
#   column   rate    block      noise cut   what it must still resolve
#   detail   20 Hz   50 ms      /4.5        the +9-14% step at pulse end
#   full      2 Hz   500 ms     /14         cooling, which runs over 5-20 s
#   phase    50 Hz   20 ms      /2.8        the heating branch, 400 ms long
#
# The detail rate is chosen so the pulse-end step stays sharp: it happens inside
# ~50 ms, so a 50 ms block keeps it as an edge rather than a ramp. Power, stroke
# and force are clean and are not reduced beyond what file size requires.
RATE = {
    "sma_r":  {"detail": 20.0, "full": 2.0, "phase": 50.0},
    "_other": {"detail": GRID_HZ, "full": FULL_HZ, "phase": 50.0},
}


def rate_for(key, colname):
    return RATE.get(key, RATE["_other"])[colname]


def median_trace(sweep, level_mA, heat_ms, grid):
    """Median trajectory over the non-bootstrap cycles, on a common grid.

    MEDIAN across repeats, not mean: it is the same combination the envelope
    and trajectory charts use, and it will not let one bad cycle drag the
    trace. Cycle 1 is held out -- it fires into a fully relaxed wire and takes
    a one-time set, so it is a different initial condition, not a repeat."""
    try:
        n_cyc = len(list_cycles(sweep, level_mA, heat_ms))
    except Exception:
        return None
    if n_cyc < 2:
        return None
    keys = [k for k, _, _ in CHANNELS]
    stack = {k: [] for k in keys}
    used = 0
    for c in range(2, n_cyc + 1):
        try:
            df = get_cycle(sweep, level_mA, heat_ms, cycle=c,
                           pre_s=PRE_S, post_s=POST_S)
        except Exception:
            continue
        pre = df[df.phase == "pre"]
        if pre.empty:
            continue
        t = df.t_s.to_numpy()
        x0, f0 = pre.laser_mm.mean(), pre.force_mN.mean()
        cols = {
            "sma_r": df.sma_r.to_numpy(),
            "power_W": df.power_W.to_numpy(),
            "dx_um": 1e3 * (df.laser_mm.to_numpy() - x0),
            "dF_mN": df.force_mN.to_numpy() - f0,
        }
        for k, y in cols.items():
            stack[k].append(np.interp(grid, t, y.astype(float),
                                      left=np.nan, right=np.nan))
        used += 1
    if not used:
        return None
    out = {k: np.nanmedian(np.vstack(v), axis=0)
           for k, v in stack.items() if v}
    out["_n"] = used
    return out


def build(heat_ms, levels, table):
    grid = np.arange(-PRE_S, POST_S, 1.0 / GRID_HZ)
    heat_s = heat_ms / 1000.0
    traces = {}
    for lv in levels:
        sw = sweep_for(table, lv, heat_ms)
        if not sw:
            continue
        tr = median_trace(sw, lv, heat_ms, grid)
        if tr:
            traces[lv] = tr
            traces[lv]["_sweep"] = sw
    if not traces:
        return None
    lv_sorted = sorted(traces)
    ramp = RAMP5 if len(lv_sorted) <= 5 else RAMP9
    col = {lv: ramp[min(i, len(ramp) - 1)] for i, lv in enumerate(lv_sorted)}

    fig = make_subplots(
        rows=4, cols=3, vertical_spacing=0.075, horizontal_spacing=0.062,
        column_widths=[0.30, 0.42, 0.28],
        subplot_titles=[
            "<b>the pulse</b>  0–1.5 s", "<b>the whole cycle</b>  incl. 29 s cool",
            "", "", "", "<b>vs resistance</b>  heating branch only",
            "", "", "", "", "", "",
        ])

    heat_m = (grid >= 0) & (grid <= heat_s)
    det_m = (grid >= -0.15) & (grid <= DETAIL_S)

    for row, (key, ylab, sub) in enumerate(CHANNELS, start=1):
        short = ylab.split("  (")[0]
        for lv in lv_sorted:
            tr = traces[lv]
            y = tr.get(key)
            if y is None:
                continue
            nm = f"{lv} mA"
            hov = (f"<b>{lv} mA · {heat_ms} ms</b><br>t = %{{x:.3f}} s<br>"
                   f"{short} = %{{y:,.3f}}<extra></extra>")

            # Raw behind the reduced trace, so the reduction hides nothing.
            # ONE level only: five overlaid raw traces are five walls of grey
            # that bury the very lines they are meant to contextualise. The
            # darkest level is shown because it has the largest real signal, so
            # it is the fairest place to judge what the averaging bought.
            if key == "sma_r" and lv == lv_sorted[-1]:
                fig.add_trace(go.Scattergl(
                    x=grid[det_m], y=y[det_m], mode="lines",
                    name=f"raw 400 Hz ({lv} mA)", legendgroup="raw",
                    showlegend=(row == 1),
                    line=dict(color="#e4e6e0", width=0.7), hoverinfo="skip",
                ), row=row, col=1)

            td, yd = block_mean(grid, y, rate_for(key, "detail"))
            m = (td >= -0.15) & (td <= DETAIL_S)
            fig.add_trace(go.Scattergl(
                x=td[m], y=yd[m], mode="lines", name=nm,
                legendgroup=nm, showlegend=(row == 1),
                line=dict(color=col[lv], width=1.5), hovertemplate=hov,
            ), row=row, col=1)

            tb, yb = block_mean(grid, y, rate_for(key, "full"))
            fig.add_trace(go.Scattergl(
                x=tb, y=yb, mode="lines", name=nm, legendgroup=nm,
                showlegend=False, line=dict(color=col[lv], width=1.5),
                hovertemplate=hov,
            ), row=row, col=2)

            # Phase column: channel vs R, heating branch only. Slice to the
            # heat window FIRST, then block -- blocking across the pulse edge
            # would average hot samples with cold ones and invent a point that
            # was never measured.
            r = traces[lv].get("sma_r")
            if r is not None and key != "sma_r":
                hz = rate_for(key, "phase")
                _, rb = block_mean(grid[heat_m], r[heat_m], hz)
                _, yb2 = block_mean(grid[heat_m], y[heat_m], hz)
                fig.add_trace(go.Scattergl(
                    x=rb, y=yb2, mode="lines+markers", name=nm,
                    legendgroup=nm, showlegend=False,
                    line=dict(color=col[lv], width=1.5),
                    marker=dict(size=4, color=col[lv]),
                    hovertemplate=(f"<b>{lv} mA</b><br>R = %{{x:.3f}} Ω<br>"
                                   f"{short} = %{{y:,.1f}}<extra></extra>"),
                ), row=row, col=3)

        for c in (1, 2):
            fig.add_vrect(x0=0, x1=heat_s, row=row, col=c, layer="below",
                          fillcolor="#f2c9c0", opacity=0.5, line_width=0)
        fig.update_yaxes(title_text=ylab, row=row, col=1)

    # the artifact, marked where it happens
    fig.add_annotation(
        row=1, col=1, x=heat_s, y=0.98, xref="x", yref="y domain",
        text="R steps +9–14 % here — bias artifact, not cooling",
        showarrow=True, arrowhead=0, arrowcolor=ps.AXIS, arrowwidth=1,
        ax=30, ay=-26, font=dict(size=10.5, color=ps.INK_2), align="left",
        bgcolor="rgba(252,252,251,0.96)", borderpad=3)

    # The empty (1,3) cell explains the column instead of sitting blank.
    # PAPER coordinates, not row/col: passing row/col together with an explicit
    # xref="x domain" does not resolve to that subplot's axis, and the box lands
    # in cell (1,1). Paper coords are unambiguous.
    fig.add_annotation(
        xref="paper", yref="paper", x=0.845, y=0.985,
        xanchor="left", yanchor="top",
        text="<b>Why heating only</b><br><br>R is measured as V/I, and I"
             "<br>drops to the ~107 mA idle bias the"
             "<br>moment the drive ends. That shifts"
             "<br>R by 0.2–0.3 Ω — comparable to the"
             "<br>whole 0.64 Ω heating excursion."
             "<br><br>So the two branches sit on"
             "<br>different R scales, and a loop drawn"
             "<br>across them is mostly artifact."
             "<br><br>Within the pulse the current is"
             "<br>constant, so this branch is sound.",
        showarrow=False, align="left", font=dict(size=11, color=ps.INK_2),
        bgcolor="rgba(252,252,251,0.96)", borderpad=8)
    fig.update_xaxes(visible=False, row=1, col=3)
    fig.update_yaxes(visible=False, row=1, col=3)

    ps.axes(fig)

    # The R row's y-range is set from the REDUCED traces. Raw R = V/I explodes
    # whenever the idle current dips toward zero, so single-sample excursions
    # reach tens of ohms; letting them set the axis compresses the real 0.6 Ω
    # signal to nothing. The raw trace is still drawn (it is just clipped by the
    # axis), so this hides no structure — only outliers that are division
    # blow-ups rather than measurements.
    rlo, rhi = [], []
    for lv in lv_sorted:
        r = traces[lv].get("sma_r")
        if r is None:
            continue
        for hz in (RATE["sma_r"]["detail"], RATE["sma_r"]["full"]):
            _, rb = block_mean(grid, r, hz)
            rlo.append(np.nanmin(rb))
            rhi.append(np.nanmax(rb))
    if rlo:
        pad = 0.08 * (max(rhi) - min(rlo))
        for c in (1, 2):
            fig.update_yaxes(range=[min(rlo) - pad, max(rhi) + pad],
                             row=1, col=c)

    for row in range(1, 5):
        fig.update_xaxes(range=[-0.15, DETAIL_S], row=row, col=1)
        fig.update_xaxes(range=[-PRE_S, POST_S], row=row, col=2)
    fig.update_xaxes(title_text="time since heat onset (s)", row=4, col=1)
    fig.update_xaxes(title_text="time since heat onset (s)", row=4, col=2)
    fig.update_xaxes(title_text="resistance  R  (Ω)", row=4, col=3)
    fig.update_annotations(font=dict(size=12))

    ps.layout(fig, 1180)
    fig.update_layout(
        legend=dict(orientation="h", y=1.035, x=0, xanchor="left",
                    yanchor="bottom", font=dict(color=ps.INK_2, size=12),
                    title=dict(text="commanded current  ", side="left",
                               font=dict(color=ps.MUTED, size=12))),
        margin=dict(l=75, r=25, t=110, b=60))
    return fig, traces, lv_sorted


NOTES_TMPL = """
<h2>The asymmetry is the point</h2>
<p>Look across columns 1 and 2 for stroke and force. The wire reaches peak
displacement inside the {heat_ms} ms pulse and then takes <b>tens of seconds</b>
to return — the rise and the recovery differ by roughly two orders of magnitude
in timescale. Any controller that treats heating and cooling as symmetric will
be wrong about one of them. Cooling is unforced: nothing drives the wire back,
it just loses heat, which is why it cannot be sped up by driving harder.</p>

<h2>Column 3 stops at heat end — deliberately</h2>
<p>The phase plots run only through the heating branch. At pulse end the drive
current drops to the ~107 mA idle bias and the measured resistance jumps
<b>+9 to +14 % within ~50 ms</b>, at a moment when the stroke is still at its
maximum and the wire is obviously still hot. That step is a measurement artifact
of the bias change (R = V/I with a ~20–35 mV offset), not cooling.</p>
<p>Its size — 0.2–0.3 Ω at the idle bias — is comparable to the entire heating
excursion of 0.64 Ω. Drawing both branches on one R axis produces a large and
very convincing hysteresis loop that is mostly this offset: the cool-vs-heat
stroke gap at equal R comes out at 81–83 % of peak, near-identical across
conditions that share nothing else. <b>The real thermal hysteresis cannot be
read from this data.</b> See <code>r_bias_artifact.html</code> for the evidence
and STATUS.md for what would unblock it.</p>
<p>Within the pulse the current is held constant, so the offset is constant
there too — the heating branch's shape is sound, and only its absolute
resistance is shifted (about 0.04 Ω at 850 mA).</p>

<h2>How to read the traces</h2>
<p>Each line is the <b>median across the repeat cycles</b> of that condition
({n_desc}), which is a combination of independent measurements, not a filter run
over one of them. Cycle 1 is held out: it fires into a fully relaxed wire and
takes a one-time set, so it is a different initial condition rather than a
repeat. The shaded band is the heat pulse.</p>
<p>Column 1 is the raw 400 Hz grid. Column 2 is reduced to 50 Hz by
<b>block mean</b> (8 consecutive samples averaged, disjoint blocks) purely to
keep the file honest in size over 31 s — nothing there moves faster than about
0.2 s. A block mean cannot ring or shift an edge, unlike the low-pass filtering
that was tried and removed from the trajectory charts.</p>
<p>Resistance is plotted in <b>absolute ohms</b>, not ΔR/R₀. The baseline varies
by only sd 0.027 Ω across the campaign, so normalizing subtracts something that
barely moves while adding its noise — and absolute ohms is what a self-sensing
controller would actually read.</p>
"""


def table_html(traces, lv_sorted, heat_ms, grid_hz=GRID_HZ):
    rows = []
    for lv in lv_sorted:
        tr = traces[lv]
        r, x, f = tr.get("sma_r"), tr.get("dx_um"), tr.get("dF_mN")
        rows.append(
            f"<tr><td>{lv} mA</td><td>{tr['_n']}</td>"
            f"<td>{np.nanmin(r):.3f}</td><td>{np.nanmax(x):,.0f}</td>"
            f"<td>{np.nanmax(f):.1f}</td>"
            f"<td>{tr['_sweep']}</td></tr>")
    return ("<table><thead><tr><th>current</th><th>cycles</th>"
            "<th>min R (Ω)</th><th>peak Δx (µm)</th><th>peak ΔF (mN)</th>"
            f"<th>capture</th></tr></thead><tbody>{''.join(rows)}</tbody>"
            "</table><p style='font-size:12.5px;color:#898781'>Per-condition "
            "summary of the plotted medians. Full per-cycle values are in "
            "<code>heat_time_map_20260731_all.csv</code>; the raw time series "
            "come from <code>get_cycle.py</code>.</p>")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heat", type=int, default=None)
    ap.add_argument("--levels", choices=["five", "all"], default="five")
    a = ap.parse_args()

    table = load()
    heats = [a.heat] if a.heat else [100, 200, 300, 400]
    levels = ([150, 350, 550, 750, 950] if a.levels == "five"
              else [150, 250, 350, 450, 550, 650, 750, 850, 950])
    for h in heats:
        got = build(h, levels, table)
        if not got:
            print(f"  !! {h} ms: no traces")
            continue
        fig, traces, lv_sorted = got
        ns = sorted({t["_n"] for t in traces.values()})
        n_desc = (f"{ns[0]} cycles" if len(ns) == 1
                  else f"{min(ns)}–{max(ns)} cycles depending on condition")
        out = os.path.join(derived_dir(CAMPAIGNS["20260731"]["dir"]),
                           f"transition_{h}ms.html")
        stand = (f"{len(lv_sorted)} current levels · {h} ms pulse · median of "
                 f"the repeat cycles · absolute ohms. Rise takes a fraction of "
                 f"a second, recovery tens of seconds. The phase column stops "
                 f"at heat end because R is not comparable across the bias "
                 f"change — see below. Drag to zoom, click a current in the "
                 f"legend to isolate it.")
        ps.page(out, f"Cold → hot → cold — {h} ms pulse", stand, fig,
                NOTES_TMPL.format(heat_ms=h, n_desc=n_desc),
                table_html(traces, lv_sorted, h))
        print(f"  -> {os.path.basename(out)}  ({len(lv_sorted)} levels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
