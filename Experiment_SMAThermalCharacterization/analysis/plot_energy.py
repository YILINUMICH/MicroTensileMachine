"""FIGURE A — does the response depend only on DELIVERED ENERGY?

    python plot_energy.py          # -> energy_collapse.html

═══ THE QUESTION ══════════════════════════════════════════════════════════
The RNN work found that feeding P·t as an input trained better than feeding
power and duration separately. This figure is the measurement behind that: if
energy is the state variable the wire actually responds to, then conditions with
the same E must produce the same stroke REGARDLESS of how that energy was
split between power and time — a short hot pulse and a long gentle one land on
the same point. Plotted, that means all four pulse lengths COLLAPSE onto one
curve. If duration mattered independently, they would separate into four.

The power panel is the control. Without it "stroke rises with energy" is not a
finding — everything rises with everything here. What makes the collapse mean
something is that the same data against POWER does not collapse at all.

═══ WHY E IS INTEGRATED, NOT p_hot_W × t ══════════════════════════════════
See energy_table.py. `p_hot_W` is the power at the END of the pulse, so the
product under-reads the delivered energy, and it under-reads MORE at longer
pulses — a pulse-length-dependent bias of the same size as the effect being
measured. E here is ∫P dt over the refined heat window.

═══ HOW TO READ THE RESIDUAL PANEL ════════════════════════════════════════
Collapse is never perfect, and the interesting part is HOW it fails. Panel 4 is
each cycle's deviation from the pooled fit, grouped by pulse length. The scatter
is cycle-to-cycle repeatability; the offset between the four groups is whatever
duration still explains after energy has had its say.
"""
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import plot_style as ps
from energy_table import load

_HERE = os.path.dirname(os.path.abspath(__file__))
DERIVED = os.path.join(_HERE, "..", "data", "derived")   # pipeline outputs
OUT = os.path.join(DERIVED, "energy_collapse.html")

# the log-scaled axes, in plotly's naming — panel 4's y is a % residual and
# stays linear
AX = ("xaxis", "yaxis", "xaxis2", "yaxis2", "xaxis3", "yaxis3", "xaxis4")


def logfit(x, y, deg=2):
    """Pooled fit in log-log. Quadratic, not linear: the response is not a
    single power law -- it is shallow until the transformation starts and steep
    after, which is a curve in log-log, and forcing a straight line through it
    would put structure into the residual that belongs to the fit, not the wire."""
    c = np.polyfit(np.log(x), np.log(y), deg)
    pred = np.exp(np.polyval(c, np.log(x)))
    ss = 1 - ((np.log(y) - np.log(pred)) ** 2).sum() / \
        ((np.log(y) - np.log(y).mean()) ** 2).sum()
    return c, pred, ss


def curve(c, x):
    g = np.geomspace(x.min(), x.max(), 200)
    return g, np.exp(np.polyval(c, np.log(g)))


HOVER = (
    "<b>%{customdata[0]:.0f} mA · %{customdata[1]:.0f} ms</b>"
    "  <span style='color:#898781'>cycle %{customdata[2]:.0f}</span><br>"
    "energy  %{customdata[3]:.3f} J   ·   power  %{customdata[4]:.2f} W<br>"
    "stroke  %{customdata[5]:,.0f} µm   ·   force  %{customdata[6]:.1f} mN<br>"
    "R_hot  %{customdata[7]:.3f} Ω<br>"
    "<span style='color:#898781'>%{customdata[8]}</span><extra></extra>"
)


def cdata(d):
    return np.column_stack([d.level_mA, d.heat_ms, d.cycle, d.E_J, d.p_hot_W,
                            d.dx_um, d.dF_mN, d.r_hot_ohm, d.sweep])


def build():
    # Every cycle that HAS the coordinates is drawn. `usable` decides only what
    # the fits are computed from -- see energy_table.py, "NO DATA SELECTION".
    all_d = load(verbose=True)
    ps.check_heats(all_d, __file__.split('/')[-1])
    d = all_d[(all_d.dx_um > 1) & (all_d.dF_mN > 0) & all_d.E_J.notna()
              & all_d.p_hot_W.notna()].copy()
    fit = d[d.usable]

    cx, _, r2x = logfit(fit.E_J, fit.dx_um)
    cf, _, r2f = logfit(fit.E_J, fit.dF_mN)
    cp, _, r2p = logfit(fit.p_hot_W, fit.dx_um)
    d = d.assign(resid_pct=100.0 * (d.dx_um /
                 np.exp(np.polyval(cx, np.log(d.E_J))) - 1.0))
    fit = d[d.usable]

    fig = make_subplots(
        rows=2, cols=2, vertical_spacing=0.145, horizontal_spacing=0.085,
        subplot_titles=[
            "<b>1 · stroke vs ENERGY</b>  — four pulse lengths, one curve",
            "<b>2 · force vs ENERGY</b>  — same collapse",
            "<b>3 · stroke vs POWER</b>  — the control: no collapse",
            "<b>4 · what energy leaves unexplained</b>  — fitted cycles only",
        ])

    # Panels 1-3 draw EVERY cycle. Panel 4 draws only the cycles the fit was
    # computed from -- it is a residual plot, and a residual against a fit that
    # never saw the point is not a residual, it is just a subtraction. Including
    # them stretched the axis to +-250% and buried the group offsets that panel
    # exists to show. Stated in the panel caption, not left implicit.
    for h in ps.HEATS:
        for hollow in (False, True):
            s = d[(d.heat_ms == h) & (d.at_rail == hollow)]
            if s.empty:
                continue
            cd = cdata(s)
            for (r, c), (xk, yk) in zip(
                    [(1, 1), (1, 2), (2, 1)],
                    [("E_J", "dx_um"), ("E_J", "dF_mN"), ("p_hot_W", "dx_um")]):
                fig.add_trace(go.Scatter(
                    x=s[xk], y=s[yk], mode="markers", name=f"{h} ms",
                    legendgroup=str(h),
                    showlegend=(r == 1 and c == 1 and not hollow),
                    marker=ps.marker(h, hollow=hollow), customdata=cd,
                    hovertemplate=HOVER,
                ), row=r, col=c)
        sf = fit[fit.heat_ms == h]
        if not sf.empty:
            fig.add_trace(go.Scatter(
                x=sf.E_J, y=sf.resid_pct, mode="markers", name=f"{h} ms",
                legendgroup=str(h), showlegend=False, marker=ps.marker(h),
                customdata=cdata(sf), hovertemplate=HOVER), row=2, col=2)

    # one legend entry explaining the hollow marks, drawn off-data
    if d.at_rail.any():
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers", name="at a sensor rail →<br>"
            "value is a lower bound, excluded from fits",
            marker=dict(size=9, color="rgba(0,0,0,0)", symbol="circle-open",
                        line=dict(width=1.6, color=ps.INK_2)),
            hoverinfo="skip"), row=1, col=1)

    # pooled fits — solid hairlines, drawn UNDER nothing but named, so the
    # curve is never mistaken for data
    for (r, c), (cc, xv) in zip([(1, 1), (1, 2), (2, 1)],
                                [(cx, d.E_J), (cf, d.E_J), (cp, d.p_hot_W)]):
        gx, gy = curve(cc, xv)
        fig.add_trace(go.Scatter(x=gx, y=gy, mode="lines", name="pooled fit",
                                 legendgroup="fit", showlegend=(r == 1 and c == 1),
                                 line=dict(color=ps.FIT, width=1.5),
                                 hoverinfo="skip"), row=r, col=c)

    # per-pulse-length mean offset in the residual panel — from the USABLE
    # cycles only, since a mean that includes lower bounds is not a mean
    for h in ps.HEATS:
        s = fit[fit.heat_ms == h]
        if s.empty:
            continue
        m = s.resid_pct.mean()
        fig.add_trace(go.Scatter(
            x=[d.E_J.min(), d.E_J.max()], y=[m, m], mode="lines",
            legendgroup=str(h), showlegend=False,
            line=dict(color=ps.COLOR[h], width=1.5),
            hovertemplate=f"{h} ms mean offset {m:+.1f}%<extra></extra>",
        ), row=2, col=2)
    fig.add_hline(y=0, row=2, col=2, line=dict(color=ps.AXIS, width=1))

    ps.axes(fig)
    # Minor gridlines are OFF. On a log axis plotly draws them at every
    # 2,3,...,9 of each decade; with 1-2-5 majors already gridded that is ~30
    # lines per axis, and the panel background washes to grey. The grid must
    # stay one shade off the surface, not become a texture.
    minor = dict(minor=dict(showgrid=False, ticks=""))
    E, P, X, F = d.E_J, d.p_hot_W, d.dx_um, d.dF_mN
    for (r, c), xv, yv in [((1, 1), E, X), ((1, 2), E, F), ((2, 1), P, X)]:
        fig.update_xaxes(type="log", row=r, col=c, **minor,
                         **ps.log_ticks(xv.min(), xv.max()))
        fig.update_yaxes(type="log", row=r, col=c, **minor,
                         **ps.log_ticks(yv.min(), yv.max()))
    fig.update_xaxes(type="log", row=2, col=2, **minor,
                     **ps.log_ticks(E.min(), E.max()))

    fig.update_xaxes(title_text="delivered energy  E = ∫P dt  (J)", row=1, col=1)
    fig.update_xaxes(title_text="delivered energy  (J)", row=1, col=2)
    fig.update_xaxes(title_text="power at pulse end  (W)", row=2, col=1)
    fig.update_xaxes(title_text="delivered energy  (J)", row=2, col=2)
    fig.update_yaxes(title_text="stroke  Δx  (µm)", row=1, col=1)
    fig.update_yaxes(title_text="force  ΔF  (mN)", row=1, col=2)
    fig.update_yaxes(title_text="stroke  Δx  (µm)", row=2, col=1)
    fig.update_yaxes(title_text="stroke deviation from fit  (%)", row=2, col=2)

    def note(txt, r, c, x=0.97, y=0.055, ha="right", va="bottom"):
        fig.add_annotation(text=txt, row=r, col=c,
                           xref="x domain", yref="y domain",
                           x=x, y=y, xanchor=ha, yanchor=va,
                           showarrow=False, align=ha,
                           font=dict(size=12, color=ps.INK_2),
                           bgcolor="rgba(252,252,251,0.96)", borderpad=4)

    # Every number in these captions is COMPUTED, never typed. A hardcoded
    # statistic silently becomes a lie the first time a sweep is added.
    sd = fit.resid_pct.std()
    off = fit.groupby("heat_ms").resid_pct.mean()
    ladder = "  →  ".join(f"{v:+.1f}%" for v in off.sort_index())
    mono = list(off.sort_index()) == sorted(off, reverse=True)
    note(f"R² = {r2x:.3f}   ·   residual sd {sd:.1f}%", 1, 1)
    note(f"R² = {r2f:.3f}", 1, 2)
    note(f"R² = {r2p:.3f}   ·   <b>spreads, not a curve</b>", 2, 1)
    # top-left: the residual panel's bottom-right is where the small-stroke
    # cycles land, and a caption there sits on top of them
    note(f"100→400 ms offset:  {ladder}"
         + ("   ·   <b>monotone in duration</b>" if mono else ""),
         2, 2, x=0.03, y=0.97, ha="left", va="top")

    # selective direct labels — the two extremes of panel 1, so the axis range
    # is anchored to real conditions without a number on every point
    for row, ann in [(d.loc[d.E_J.idxmin()], dict(ax=34, ay=-26)),
                     (d.loc[d.E_J.idxmax()], dict(ax=-40, ay=30))]:
        fig.add_annotation(x=np.log10(row.E_J), y=np.log10(row.dx_um),
                           text=f"{row.level_mA:.0f} mA · {row.heat_ms:.0f} ms",
                           row=1, col=1, showarrow=True, arrowhead=0,
                           arrowcolor=ps.AXIS, arrowwidth=1,
                           font=dict(size=11, color=ps.INK_2),
                           bgcolor="rgba(252,252,251,0.96)", borderpad=3, **ann)

    ps.layout(fig, 880)
    fig.update_layout(updatemenus=[dict(
        type="buttons", direction="right", x=1.0, xanchor="right",
        y=1.055, yanchor="bottom", showactive=True, bgcolor=ps.SURFACE,
        bordercolor=ps.AXIS, borderwidth=1,
        font=dict(size=11.5, color=ps.INK_2), pad=dict(l=4, r=4, t=3, b=3),
        buttons=[
            # tickmode flips with the scale: the stored 1-2-5 tickvals are
            # placed for a log axis and bunch up against zero on a linear one,
            # so linear hands ticking back to plotly.
            dict(label="log axes", method="relayout",
                 args=[{**{f"{a}.type": "log" for a in AX},
                        **{f"{a}.tickmode": "array" for a in AX}}]),
            dict(label="linear", method="relayout",
                 args=[{**{f"{a}.type": "linear" for a in AX},
                        **{f"{a}.tickmode": "auto" for a in AX}}]),
        ])])
    return fig, d, fit, (r2x, r2f, r2p)


def table_html(d):
    g = (d.groupby(["level_mA", "heat_ms"])
           .agg(n=("dx_um", "size"), rail=("at_rail", "sum"),
                E_J=("E_J", "median"), P_W=("p_hot_W", "median"),
                dx_um=("dx_um", "median"), dF_mN=("dF_mN", "median"),
                R_hot=("r_hot_ohm", "median"))
           .reset_index().sort_values(["heat_ms", "level_mA"]))
    rows = "".join(
        f"<tr><td>{r.level_mA:.0f} mA · {r.heat_ms:.0f} ms</td><td>{r.n:.0f}</td>"
        f"<td>{'—' if not r.rail else f'{r.rail:.0f}'}</td>"
        f"<td>{r.E_J:.3f}</td><td>{r.P_W:.2f}</td>"
        f"<td>{r.dx_um:,.0f}{'*' if r.rail else ''}</td>"
        f"<td>{r.dF_mN:.1f}{'*' if r.rail else ''}</td>"
        f"<td>{r.R_hot:.3f}</td></tr>"
        for r in g.itertuples())
    return ("<table><thead><tr><th>condition</th><th>n</th><th>at rail</th>"
            "<th>E (J)</th><th>P (W)</th><th>Δx (µm)</th><th>ΔF (mN)</th>"
            f"<th>R_hot (Ω)</th></tr></thead><tbody>{rows}</tbody></table>"
            "<p style='font-size:12.5px;color:#898781'>Median over the cycles of "
            "each condition; nothing is excluded. <b>*</b> marks a condition "
            "with rail-limited cycles — its Δx/ΔF median is a lower bound. "
            "Per-cycle values are in "
            "<code>heat_time_map_20260731_all.csv</code> joined to "
            "<code>heat_time_map_20260731_all_energy.csv</code>.</p>")


def notes_html(d, fit, r2p):
    """Prose with the numbers computed in. Same rule as the panel captions."""
    off = fit.groupby("heat_ms").resid_pct.mean().sort_index()
    ladder = " → ".join(f"<b>{v:+.1f} %</b> ({h:.0f} ms)"
                        for h, v in off.items())
    sd = fit.resid_pct.std()
    # the widest stroke range sharing one power decade — the "no collapse" claim
    band = fit[(fit.p_hot_W > 2.5) & (fit.p_hot_W < 3.5)]
    lo, hi = band.dx_um.min(), band.dx_um.max()
    rail = sorted({(int(r.level_mA), int(r.heat_ms))
                   for r in d.itertuples() if r.at_rail})
    return f"""
<h2>What this shows</h2>
<p>Stroke and force from all four pulse lengths fall on <b>one curve</b> against
delivered energy — the four marker shapes interleave rather than separating into
bands. Against <b>power alone</b> (panel 3) the same cycles spread into arms:
between 2.5 and 3.5 W the stroke ranges from <b>{lo:,.0f}</b> to
<b>{hi:,.0f} µm</b> depending only on how long the power was held, and the best
single curve through it manages R² {r2p:.3f}. Energy is doing the work; power
is not.</p>

<h2>What energy does not explain</h2>
<p>Panel 4: the group offsets run {ladder}. That ladder is monotone in duration
and is a real residual effect — short pulses are slightly <i>more</i> effective
per joule, which is what you would expect if a longer pulse loses more heat to
the surroundings while it is still heating. It is small against the
{sd:.1f} % cycle-to-cycle scatter, but it is systematic, so energy is very
nearly — not exactly — sufficient.</p>

<h2>Nothing is dropped — read the hollow marks</h2>
<p>Every cycle with a valid energy and response is plotted, following the
module's standing rule that quality is reported, never filtered. <b>Hollow marks
are cycles sitting at a sensor rail</b> — the load cell clipped or the laser ran
out of analog window. Their stroke and force are <i>lower bounds</i>: the wire
moved at least that far. They are drawn because non-response and saturation are
real machine states, and they are excluded from the <i>fits</i> only, because a
regression over lower bounds is biased toward them.</p>
<p>{len(rail)} condition(s) are affected:
{", ".join(f"{lv} mA · {h} ms" for lv, h in rail)}. That includes the strongest
condition in the campaign, 950 mA · 400 ms — so <b>the high-energy end of this
figure is limited by the instruments, not by the wire</b>. Where the hollow
marks flatten out near the top, that is the laser's range ending, not the SMA
saturating.</p>
<p>Also held out of the fits: bootstrap cycles (cycle 1 of each condition fires
into a fully relaxed wire and takes a one-time set) and cycles whose heat window
could not be refined. <b>150 mA</b> has no usable cycles at all — its pulses sit
inside the ~107 mA idle-current noise band — so the fitted range is 250–950 mA.</p>
<p>The fits are descriptive, not a model — a quadratic in log-log, used only to
give the residual panel something to be a residual of.</p>
"""


def main():
    fig, d, fit, (r2x, r2f, r2p) = build()
    stand = (f"{len(d)} cycles drawn, {len(fit)} used for the fits (hollow = at "
             f"a sensor rail, a lower bound) · 150–950 mA × 100–400 ms · energy "
             f"integrated as ∫P dt over each refined heat window. Stroke and "
             f"force collapse onto one curve against energy "
             f"(R² {r2x:.3f} / {r2f:.3f}); against power they do not "
             f"(R² {r2p:.3f}). Drag to zoom, double-click to reset, click a "
             f"pulse length in the legend to isolate it.")
    p = ps.page(OUT, "Does the SMA respond to delivered energy?", stand, fig,
                notes_html(d, fit, r2p), table_html(d))
    print(f"  -> {os.path.basename(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
