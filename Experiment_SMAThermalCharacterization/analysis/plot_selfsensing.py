"""FIGURE B — can RESISTANCE stand in for the mechanical state?

    python plot_selfsensing.py     # -> self_sensing.html

═══ THE QUESTION ══════════════════════════════════════════════════════════
The wire is its own sensor: R falls as it heats, so R is free instrumentation
that a deployed actuator would actually have. The question is what R can be
trusted to tell you — is stroke a LINEAR function of R, some other fixed
function, or not a function of R at all?

═══ WHY THESE AXES ARE LINEAR ═════════════════════════════════════════════
Figure A is log-log, because there the job is to show a collapse across 2.5
decades. Here the job is to judge SHAPE, and a log axis makes a straight line
curve and a curve straight. So panels 1 and 2 are linear-linear with the
best straight line DRAWN THROUGH THEM: if the relationship were linear the
points would sit on it, and the way they bow away from it is the answer.

═══ ABSOLUTE OHMS, NOT ΔR/R₀ ══════════════════════════════════════════════
R is plotted as measured. Normalizing by each cycle's own baseline was tried and
is not worth it here: r_base varies by only sd 0.027 Ω across the campaign, and
absolute R is the BETTER predictor of stroke — R² 0.955, against 0.935 for
ΔR/R₀ and 0.929 for ΔR in ohms (all scored on raw microns over the same 176
cycles). Normalizing subtracts a quantity that barely moves and injects that
measurement's noise, so it costs accuracy rather than buying any. Absolute ohms
is also what a self-sensing controller would actually read, so it is the honest
axis. ΔR/R₀ is in the hover for every point.

CAVEAT worth knowing before using ΔR anywhere: `r_base` is measured at the
~107 mA idle bias and `r_hot` at the drive current, so a ΔR between them is
partly a comparison of two different bias points, not purely a resistance
change. It shows up as a non-physical −4 % intercept at E→0. The SLOPE is
unaffected, which is why panel 3 is still meaningful.
"""
import os

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import plot_style as ps
from energy_table import load

_HERE = os.path.dirname(os.path.abspath(__file__))
DERIVED = os.path.join(_HERE, "..", "data", "derived")   # pipeline outputs
OUT = os.path.join(DERIVED, "self_sensing.html")


def r2_of(y, pred):
    y = np.asarray(y, float)
    return 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()


def linfit(x, y):
    c = np.polyfit(x, y, 1)
    return c, np.polyval(c, x), r2_of(y, np.polyval(c, x))


def logquad(x, y):
    """Quadratic in x predicting log y — the best simple CURVE, used as the
    reference the residual panel measures against. Fitting in log keeps the
    prediction positive; a quadratic straight through stroke would dip below
    zero at the cold end and make the residuals meaningless there.

    NOTE the R² is returned in LINEAR y, not in log y. Scoring this fit in log
    space and the straight line in linear space would compare two different
    quantities — and it flatters whichever one is scored in log, because log
    compresses the large residuals at the top end. Both are scored on the raw
    stroke so "does the curve beat the line" is an answerable question."""
    c = np.polyfit(x, np.log(y), 2)
    pred = np.exp(np.polyval(c, x))
    return c, pred, r2_of(y, pred)


HOVER = (
    "<b>%{customdata[0]:.0f} mA · %{customdata[1]:.0f} ms</b>"
    "  <span style='color:#898781'>cycle %{customdata[2]:.0f}</span><br>"
    "R_hot  %{customdata[3]:.3f} Ω"
    "   <span style='color:#898781'>(ΔR/R₀ %{customdata[4]:+.1f} %)</span><br>"
    "energy  %{customdata[5]:.3f} J<br>"
    "stroke  %{customdata[6]:,.0f} µm   ·   force  %{customdata[7]:.1f} mN"
    "<extra></extra>"
)


def build():
    # Every cycle with the coordinates is drawn; `usable` governs the FITS
    # only. See energy_table.py, "NO DATA SELECTION".
    all_d = load(verbose=True)
    ps.check_heats(all_d, __file__.split('/')[-1])
    d = all_d[(all_d.dx_um > 1) & (all_d.dF_mN > 0) & all_d.r_hot_ohm.notna()
              & all_d.E_J.notna()].copy()
    R = d.r_hot_ohm.to_numpy()
    fit = d[d.usable]
    Rf = fit.r_hot_ohm.to_numpy()

    _, _, r2_lin_x = linfit(Rf, fit.dx_um)
    cq_x, _, r2_cur_x = logquad(Rf, fit.dx_um)
    _, _, r2_lin_f = linfit(Rf, fit.dF_mN)
    _, _, r2_cur_f = logquad(Rf, fit.dF_mN)
    ce, _, r2_e = linfit(fit.E_J.to_numpy(), Rf)

    # Residual in MICRONS, not percent. Percent divides by the prediction, and
    # at the cold end the prediction is a few tens of microns -- so a 30 um miss
    # on a 40 um stroke reads as +75%, and one cycle reached +400% and set the
    # axis range for all 176. That is the same trap that made the old
    # `--norm shape` trajectory panels unreadable: a ratio is only meaningful
    # where its denominator is.
    from plot_energy import logfit as _elogfit
    d = d.assign(resid_um=d.dx_um - np.exp(np.polyval(cq_x, R)))
    fit = d[d.usable]
    ce_x, _, _ = _elogfit(fit.E_J.to_numpy(), fit.dx_um.to_numpy())
    pred_e = np.exp(np.polyval(ce_x, np.log(fit.E_J.to_numpy())))
    sd_r = fit.resid_um.std()
    sd_e = (fit.dx_um.to_numpy() - pred_e).std()

    cd = np.column_stack([d.level_mA, d.heat_ms, d.cycle, d.r_hot_ohm,
                          d.dR_pct, d.E_J, d.dx_um, d.dF_mN])

    fig = make_subplots(
        rows=2, cols=2, vertical_spacing=0.145, horizontal_spacing=0.085,
        subplot_titles=[
            "<b>1 · stroke vs RESISTANCE</b>  — straight line drawn for comparison",
            "<b>2 · force vs RESISTANCE</b>  — same bow",
            "<b>3 · resistance vs ENERGY</b>  — this one IS linear",
            "<b>4 · what R alone gets wrong</b>  — fitted cycles only",
        ])

    for h in ps.HEATS:
        for hollow in (False, True):
            m = ((d.heat_ms == h) & (d.at_rail == hollow)).to_numpy()
            s = d[m]
            if s.empty:
                continue
            c = cd[m]
            for (r, col), (xk, yk) in zip([(1, 1), (1, 2), (2, 1)],
                                          [("r_hot_ohm", "dx_um"),
                                           ("r_hot_ohm", "dF_mN"),
                                           ("E_J", "r_hot_ohm")]):
                fig.add_trace(go.Scatter(
                    x=s[xk], y=s[yk], mode="markers", name=f"{h} ms",
                    legendgroup=str(h),
                    showlegend=(r == 1 and col == 1 and not hollow),
                    marker=ps.marker(h, hollow=hollow), customdata=c,
                    hovertemplate=HOVER), row=r, col=col)
        # panel 4 is a residual OF THE FIT, so it shows the fitted cycles only
        sf = fit[fit.heat_ms == h]
        if not sf.empty:
            fig.add_trace(go.Scatter(
                x=sf.r_hot_ohm, y=sf.resid_um, mode="markers", name=f"{h} ms",
                legendgroup=str(h), showlegend=False, marker=ps.marker(h),
                customdata=cd[(d.usable & (d.heat_ms == h)).to_numpy()],
                hovertemplate=HOVER), row=2, col=2)

    if d.at_rail.any():
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers",
            name="at a sensor rail →<br>lower bound, excluded from fits",
            marker=dict(size=9, color="rgba(0,0,0,0)", symbol="circle-open",
                        line=dict(width=1.6, color=ps.INK_2)),
            hoverinfo="skip"), row=1, col=1)

    # straight-line references. Named "straight line", NOT "fit" — the point of
    # panels 1-2 is that this line does not describe the data.
    for (r, col), yv in [((1, 1), fit.dx_um), ((1, 2), fit.dF_mN)]:
        c, _, _ = linfit(Rf, yv)
        gx = np.linspace(R.min(), R.max(), 50)
        fig.add_trace(go.Scatter(
            x=gx, y=np.polyval(c, gx), mode="lines", name="straight line",
            legendgroup="lin", showlegend=(r == 1 and col == 1),
            line=dict(color=ps.FIT, width=1.5), hoverinfo="skip"),
            row=r, col=col)
        # and the curve that does
        cq, _, _ = logquad(Rf, yv)
        fig.add_trace(go.Scatter(
            x=gx, y=np.exp(np.polyval(cq, gx)), mode="lines", name="best curve",
            legendgroup="cur", showlegend=(r == 1 and col == 1),
            line=dict(color=ps.RAMP[2], width=1.5, dash="dot"),
            hoverinfo="skip"), row=r, col=col)

    gx = np.linspace(fit.E_J.min(), fit.E_J.max(), 50)
    fig.add_trace(go.Scatter(x=gx, y=np.polyval(ce, gx), mode="lines",
                             name="straight line", legendgroup="lin",
                             showlegend=False,
                             line=dict(color=ps.FIT, width=1.5),
                             hoverinfo="skip"), row=2, col=1)

    for h in ps.HEATS:
        s = fit[fit.heat_ms == h]
        if s.empty:
            continue
        m, se = s.resid_um.mean(), s.resid_um.std() / np.sqrt(len(s))
        fig.add_trace(go.Scatter(
            x=[R.min(), R.max()], y=[m, m], mode="lines",
            legendgroup=str(h), showlegend=False,
            line=dict(color=ps.COLOR[h], width=1.5),
            hovertemplate=f"{h} ms mean {m:+.0f} ± {se:.0f} µm<extra></extra>",
        ), row=2, col=2)
    fig.add_hline(y=0, row=2, col=2, line=dict(color=ps.AXIS, width=1))

    ps.axes(fig)
    fig.update_xaxes(title_text="resistance at pulse end  R  (Ω)", row=1, col=1)
    fig.update_xaxes(title_text="resistance at pulse end  (Ω)", row=1, col=2)
    fig.update_xaxes(title_text="delivered energy  E = ∫P dt  (J)", row=2, col=1)
    fig.update_xaxes(title_text="resistance at pulse end  (Ω)", row=2, col=2)
    fig.update_yaxes(title_text="stroke  Δx  (µm)", row=1, col=1)
    fig.update_yaxes(title_text="force  ΔF  (mN)", row=1, col=2)
    fig.update_yaxes(title_text="resistance  R  (Ω)", row=2, col=1)
    fig.update_yaxes(title_text="stroke miss  (µm)   actual − predicted",
                     row=2, col=2)

    def note(txt, r, c, x=0.03, y=0.97, ha="left", va="top"):
        fig.add_annotation(text=txt, row=r, col=c, xref="x domain",
                           yref="y domain", x=x, y=y, xanchor=ha, yanchor=va,
                           showarrow=False, align=ha,
                           font=dict(size=12, color=ps.INK_2),
                           bgcolor="rgba(252,252,251,0.96)", borderpad=4)

    note(f"straight line R² = {r2_lin_x:.3f}<br>best curve R² = {r2_cur_x:.3f}",
         1, 1)
    note(f"straight line R² = {r2_lin_f:.3f}<br>best curve R² = {r2_cur_f:.3f}",
         1, 2)
    note(f"R = {ce[0]:.3f}·E {ce[1]:+.3f}   ·   R² = {r2_e:.3f}", 2, 1,
         x=0.97, ha="right")
    g = fit.groupby("heat_ms").resid_um
    off, se = g.mean().sort_index(), (g.std() / np.sqrt(g.count())).sort_index()
    note("predicting stroke — residual sd:<br>"
         f"from <b>R</b> {sd_r:.0f} µm  ·  from <b>energy</b> {sd_e:.0f} µm"
         "<br>" + "  ".join(f"{h:.0f}ms {v:+.0f}±{e:.0f}"
                            for (h, v), e in zip(off.items(), se)),
         2, 2)

    # direction of travel — without this the reader has to work out that the
    # wire moves RIGHT to LEFT as it heats
    fig.add_annotation(text="← hotter", row=1, col=1, xref="x domain",
                       yref="y domain", x=0.03, y=0.06, xanchor="left",
                       showarrow=False, font=dict(size=11.5, color=ps.MUTED))

    ps.layout(fig, 880)
    return fig, d, fit, dict(lin_x=r2_lin_x, cur_x=r2_cur_x, lin_f=r2_lin_f,
                        cur_f=r2_cur_f, e=r2_e, slope_e=ce[0],
                        sd_r=sd_r, sd_e=sd_e)


def table_html(d):
    g = (d.groupby(["level_mA", "heat_ms"])
           .agg(n=("dx_um", "size"), R_hot=("r_hot_ohm", "median"),
                dR=("dR_pct", "median"), E_J=("E_J", "median"),
                dx_um=("dx_um", "median"), dF_mN=("dF_mN", "median"))
           .reset_index().sort_values("R_hot"))
    rows = "".join(
        f"<tr><td>{r.level_mA:.0f} mA · {r.heat_ms:.0f} ms</td><td>{r.n:.0f}</td>"
        f"<td>{r.R_hot:.3f}</td><td>{r.dR:+.1f}</td><td>{r.E_J:.3f}</td>"
        f"<td>{r.dx_um:,.0f}</td><td>{r.dF_mN:.1f}</td></tr>"
        for r in g.itertuples())
    return ("<table><thead><tr><th>condition</th><th>n</th><th>R_hot (Ω)</th>"
            "<th>ΔR/R₀ (%)</th><th>E (J)</th><th>Δx (µm)</th><th>ΔF (mN)</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
            "<p style='font-size:12.5px;color:#898781'>Sorted by resistance, so "
            "rows adjacent here are conditions a resistance-only reading would "
            "confuse. Median over the surviving cycles of each condition.</p>")


def notes_html(d, fit, s):
    g = fit.groupby("heat_ms").resid_um
    off = g.mean().sort_index()
    se = (g.std() / np.sqrt(g.count())).sort_index()
    # which groups are separated by more than their combined standard error --
    # stated rather than asserted, because "the groups differ" is exactly the
    # kind of claim that is easy to read off a scatter and hard to defend
    resolved = [f"<b>{h:.0f} ms {v:+.0f} ± {e:.0f} µm</b>"
                for (h, v), e in zip(off.items(), se) if abs(v) > 2 * e]
    return f"""
<h2>Is it linear? No — and not by a little</h2>
<p>A straight line through stroke-vs-R manages R² {s['lin_x']:.3f}; the best
simple curve gets {s['cur_x']:.3f}. The data bows clearly away from the line:
above about 4.5 Ω almost nothing moves, then stroke takes off. That knee is the
transformation starting — the wire is not doing anything mechanical until it is
hot enough, and R keeps falling smoothly through the whole range either way.
Force behaves the same ({s['lin_f']:.3f} → {s['cur_f']:.3f}).</p>

<h2>Resistance is an excellent ENERGY readout</h2>
<p>Panel 3 is the clean one: R falls linearly with delivered energy at
<b>{s['slope_e']:.3f} Ω/J</b>, R² {s['e']:.3f}, and the four pulse lengths sit on
top of each other. R is, to good accuracy, a linear thermometer for the joules
you put in. That is a genuinely useful thing to have for free.</p>

<h2>But R is the weaker predictor of stroke</h2>
<p>Panel 4, and this is the practical result. Predicting stroke from resistance
leaves a residual sd of <b>{s['sd_r']:.0f} µm</b>; predicting the same 176
cycles from delivered energy leaves <b>{s['sd_e']:.0f} µm</b>. Energy wins by
about {s['sd_r'] / s['sd_e']:.1f}×.</p>
<p>The residual also is not independent of pulse length:
{" and ".join(resolved)} sit clear of zero by more than twice their standard
error. So two cycles at the <i>same</i> resistance can end at measurably
different strokes depending on how they got there — R does not fully determine
the mechanical state. Sort the table view by resistance and the adjacent rows
show it directly. Be careful how hard you lean on this though: the offsets are
not a monotone trend in duration, and the 300 and 400 ms groups are not
separable from each other at all.</p>

<h2>What this means for the network</h2>
<p>R and E are not redundant inputs, and neither replaces the other. Energy
predicts stroke better (Figure A, R² 0.992) but is an open-loop quantity — it is
what you commanded, not what happened. R reports the wire's actual thermal
state and would catch a disturbance that energy alone cannot see, but on its own
it is the worse predictor. Feeding both is not belt-and-braces; they carry
different information.</p>

<h2>Read with care</h2>
<p>Same quality filter as Figure A (bootstrap, unrefined windows, and clipped or
railed cycles removed; 150 mA gone entirely). Every point here is one cycle at
the END of its heat pulse — this figure says nothing about the cooling branch,
where R carries roughly 24 % per-sample noise at the ~107 mA idle bias. Whether
heating and cooling trace the <i>same</i> R↔stroke path or a hysteresis loop is
a different measurement, and it needs the full trajectories rather than one
point per cycle.</p>
"""


def main():
    fig, d, fit, s = build()
    stand = (f"{len(d)} cycles drawn, {len(fit)} used for the fits (hollow = at "
             f"a sensor rail, a lower bound) · resistance measured "
             f"at the end of each heat pulse. Stroke is a clearly non-linear "
             f"function of R (straight line R² {s['lin_x']:.3f}), while R itself "
             f"is very nearly linear in delivered energy (R² {s['e']:.3f}). "
             f"Drag to zoom, click a pulse length to isolate it.")
    p = ps.page(OUT, "How much can resistance tell you?", stand, fig,
                notes_html(d, fit, s), table_html(d))
    print(f"  -> {os.path.basename(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
