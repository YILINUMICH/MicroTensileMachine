"""FIGURE D — the resistance bias artifact, and why it blocks the hysteresis loop.

    python plot_r_bias.py        # -> r_bias_artifact.html

═══ THE CLAIM ═════════════════════════════════════════════════════════════
Measured R is not comparable across a change in drive current. At pulse end the
current drops from the commanded level to the ~107 mA idle bias and R jumps
+9 to +14% within ~50 ms — at a moment when the stroke is still at its maximum,
so the wire is unambiguously still hot. Cooling time constants here are 5-20 s.
Nothing physical changes resistance that fast.

The consequence is not cosmetic: it means the heating and cooling branches sit
on two different R scales, so the classic stroke-vs-temperature hysteresis loop
CANNOT be read from this dataset. Drawn naively the loop looks large and
convincing — the cool-vs-heat stroke gap at equal R comes out at 81-83% of peak,
near-identical across conditions with nothing else in common — but that number
is the offset, not the alloy.

═══ THE MODEL, AND ITS TWO INDEPENDENT TESTS ══════════════════════════════
A series voltage offset in the sense path gives

    R_meas = R_true + V_off / I

which inflates R more at low current. Two tests, using different data:

  1. THE STEP AT PULSE END. Temperature is continuous across that instant, so
     R_true is too, and the whole step must be the offset term. That makes each
     cycle solve for V_off:  V_off = (V2·I1 − V1·I2) / (I1 − I2). Panel 4 plots
     the measured step against the model's geometric factor (1/I2 − 1/I1); if
     the model holds this is a straight line through the ORIGIN with slope V_off.

  2. R AT PULSE ONSET. In the first 4-20 ms of a pulse the wire has not warmed
     yet, whatever current it was given — so R_true is the same ambient value
     across all nine current levels, and R_meas vs 1/I should be a straight line
     whose slope is again V_off and whose intercept is the true cold resistance.
     Panel 3.

Both land in the same place, 20-35 mV, which is why the mechanism is stated as
likely rather than proven. Neither is tight enough to CORRECT with: at the idle
bias the implied correction is 0.21-0.30 Ω with roughly +-0.15 Ω left over,
against a total heating excursion of 0.64 Ω.

═══ WHAT WOULD FIX IT ═════════════════════════════════════════════════════
Sense R at a CONSTANT current on both branches — hold a fixed sense bias through
the cool phase, or use Firmware_SMAConstantCurrent_PIO (closed-loop CC, streams
R_est as src=7). Separately, a room-temperature current sweep at fixed
temperature would pin V_off properly and retroactively correct every capture
already taken.
"""
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
OUT = os.path.join(DERIVED, "r_bias_artifact.html")
CACHE = os.path.join(DERIVED, "r_bias_points.csv")

ONSET = (0.004, 0.020)      # wire still at ambient, current settled
PRE_END = (-0.050, -0.005)  # last 45 ms of heat, relative to heat end
POST_END = (0.020, 0.100)   # first 80 ms of cool


def collect(refresh=False):
    """Per-cycle (V, I) in three windows. Cached — it needs the raw captures."""
    if os.path.exists(CACHE) and not refresh:
        return pd.read_csv(CACHE)
    d = load()
    d = d[d.usable]
    rows = []
    for (sw, lv, ht), g in d.groupby(["sweep", "level_mA", "heat_ms"]):
        for cyc in g.cycle:
            try:
                df = get_cycle(sw, lv, ht, cycle=int(cyc), pre_s=0.3, post_s=0.3)
            except Exception:
                continue
            t = df.t_s.to_numpy()
            v, i = df.sma_v.to_numpy(), df.sma_i.to_numpy()
            te = ht / 1000.0

            def win(a, b):
                m = (t >= a) & (t < b)
                if m.sum() < 8:
                    return None
                return float(np.nanmean(v[m])), float(np.nanmean(i[m]))

            on = win(*ONSET)
            a = win(te + PRE_END[0], te + PRE_END[1])
            b = win(te + POST_END[0], te + POST_END[1])
            if not (on and a and b):
                continue
            V1, I1 = a
            V2, I2 = b
            if not (I1 > 0.2 and 0.05 < I2 < 0.2 and on[1] > 0.15):
                continue
            rows.append(dict(
                sweep=sw, level_mA=lv, heat_ms=ht, cycle=int(cyc),
                V_on=on[0], I_on=on[1], R_on=on[0] / on[1],
                V1=V1, I1=I1, R1=V1 / I1, V2=V2, I2=I2, R2=V2 / I2,
                step_ohm=V2 / I2 - V1 / I1,
                geom=1.0 / I2 - 1.0 / I1,
                V_off=(V2 * I1 - V1 * I2) / (I1 - I2)))
    out = pd.DataFrame(rows)
    out.to_csv(CACHE, index=False)
    print(f"  -> {os.path.basename(CACHE)}  ({len(out)} cycles)")
    return out


def traj(sweep, lv, ht, grid):
    """Median R and stroke around pulse end, for the time panel."""
    try:
        n = len(list_cycles(sweep, lv, ht))
    except Exception:
        return None
    rs, xs, iss = [], [], []
    for c in range(2, n + 1):
        try:
            df = get_cycle(sweep, lv, ht, cycle=c, pre_s=0.4, post_s=1.2)
        except Exception:
            continue
        t = df.t_s.to_numpy()
        pre = df[df.phase == "pre"]
        if pre.empty:
            continue
        rs.append(np.interp(grid, t, df.sma_r.to_numpy(), np.nan, np.nan))
        iss.append(np.interp(grid, t, 1e3 * df.sma_i.to_numpy(), np.nan, np.nan))
        xs.append(np.interp(grid, t,
                            1e3 * (df.laser_mm.to_numpy() - pre.laser_mm.mean()),
                            np.nan, np.nan))
    if not rs:
        return None
    return (np.nanmedian(np.vstack(rs), 0), np.nanmedian(np.vstack(iss), 0),
            np.nanmedian(np.vstack(xs), 0))


def blk(t, y, n=8):
    k = (len(y) // n) * n
    with np.errstate(invalid="ignore"):
        return t[:k].reshape(-1, n).mean(1), np.nanmean(y[:k].reshape(-1, n), 1)


LEVELS = [350, 550, 750, 950]
RAMP = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]


def build():
    pts = collect()
    heat_ms = 400
    table = load()
    grid = np.arange(-0.4, 1.2, 1 / 400.0)

    fig = make_subplots(
        rows=2, cols=2, vertical_spacing=0.155, horizontal_spacing=0.085,
        subplot_titles=[
            "<b>1 · R at pulse end</b>  — a step no temperature can make",
            "<b>2 · the drive current</b>  — what actually changed",
            "<b>3 · R at pulse onset, wire still cold</b>  — R vs 1/I",
            "<b>4 · does the offset model hold?</b>  — step vs 1/I₂ − 1/I₁",
        ])

    # ---- panels 1 & 2: the step, and the current that caused it -----------
    col = dict(zip(LEVELS, RAMP))
    for lv in LEVELS:
        sw = table[(table.level_mA == lv) & (table.heat_ms == heat_ms)]
        if sw.empty:
            continue
        got = traj(sw.sweep.value_counts().idxmax(), lv, heat_ms, grid)
        if not got:
            continue
        r, i, x = got
        tb, rb = blk(grid, r)
        fig.add_trace(go.Scattergl(
            x=tb, y=rb, mode="lines", name=f"{lv} mA", legendgroup=str(lv),
            line=dict(color=col[lv], width=1.6),
            hovertemplate=f"<b>{lv} mA</b><br>t = %{{x:.3f}} s<br>"
                          "R = %{y:.3f} Ω<extra></extra>"), row=1, col=1)
        ti, ib = blk(grid, i)
        fig.add_trace(go.Scattergl(
            x=ti, y=ib, mode="lines", name=f"{lv} mA", legendgroup=str(lv),
            showlegend=False, line=dict(color=col[lv], width=1.6),
            hovertemplate=f"<b>{lv} mA</b><br>t = %{{x:.3f}} s<br>"
                          "I = %{y:.0f} mA<extra></extra>"), row=1, col=2)
    for c in (1, 2):
        fig.add_vline(x=heat_ms / 1000.0, row=1, col=c,
                      line=dict(color=ps.AXIS, width=1, dash="dot"))

    # ---- panel 3: R at ambient vs 1/I -------------------------------------
    g = pts.groupby("level_mA").agg(I=("I_on", "mean"), R=("R_on", "mean"),
                                    sd=("R_on", "std"), n=("R_on", "size"))
    g["inv"] = 1.0 / g.I
    c3 = np.polyfit(g.inv, g.R, 1)
    pred3 = np.polyval(c3, g.inv)
    r2_3 = 1 - ((g.R - pred3) ** 2).sum() / ((g.R - g.R.mean()) ** 2).sum()
    fig.add_trace(go.Scatter(
        x=g.inv, y=g.R, mode="markers", name="condition mean",
        error_y=dict(type="data", array=g.sd / np.sqrt(g.n), color=ps.AXIS,
                     thickness=1, width=4),
        marker=dict(size=10, color=ps.RAMP[2], symbol="circle",
                    line=dict(width=2, color=ps.SURFACE)),
        customdata=np.c_[g.index, g.I * 1e3, g.n],
        hovertemplate="<b>%{customdata[0]:.0f} mA</b> (n=%{customdata[2]:.0f})"
                      "<br>I = %{customdata[1]:.0f} mA<br>R = %{y:.3f} Ω"
                      "<extra></extra>"), row=2, col=1)
    gx = np.linspace(g.inv.min(), g.inv.max(), 40)
    fig.add_trace(go.Scatter(x=gx, y=np.polyval(c3, gx), mode="lines",
                             name="offset model", line=dict(color=ps.FIT, width=1.5),
                             hoverinfo="skip"), row=2, col=1)

    # ---- panel 4: the model test -----------------------------------------
    # slope through the ORIGIN: the model has no constant term here, so fitting
    # one would let a nonzero intercept hide a failure of the model itself
    slope = float((pts.geom * pts.step_ohm).sum() / (pts.geom ** 2).sum())
    ss = 1 - ((pts.step_ohm - slope * pts.geom) ** 2).sum() / \
        ((pts.step_ohm - pts.step_ohm.mean()) ** 2).sum()
    for lv in sorted(pts.level_mA.unique()):
        s = pts[pts.level_mA == lv]
        fig.add_trace(go.Scatter(
            x=s.geom, y=s.step_ohm, mode="markers", showlegend=False,
            marker=dict(size=7, color=ps.RAMP[2], opacity=0.55,
                        line=dict(width=1, color=ps.SURFACE)),
            customdata=np.c_[s.level_mA, s.heat_ms, s.V_off * 1e3],
            hovertemplate="<b>%{customdata[0]:.0f} mA · %{customdata[1]:.0f} ms"
                          "</b><br>step = %{y:.3f} Ω<br>1/I₂−1/I₁ = %{x:.2f} A⁻¹"
                          "<br>implied V_off = %{customdata[2]:.0f} mV"
                          "<extra></extra>"), row=2, col=2)
    gx4 = np.linspace(0, pts.geom.max() * 1.05, 30)
    fig.add_trace(go.Scatter(x=gx4, y=slope * gx4, mode="lines",
                             name="offset model", showlegend=False,
                             line=dict(color=ps.FIT, width=1.5),
                             hoverinfo="skip"), row=2, col=2)

    ps.axes(fig)
    fig.update_xaxes(title_text="time since heat onset (s)", row=1, col=1)
    fig.update_xaxes(title_text="time since heat onset (s)", row=1, col=2)
    fig.update_yaxes(title_text="resistance  R  (Ω)", row=1, col=1)
    fig.update_yaxes(title_text="drive current  I  (mA)", row=1, col=2)
    fig.update_xaxes(title_text="1 / I   (A⁻¹)", row=2, col=1)
    fig.update_yaxes(title_text="R at ambient  (Ω)", row=2, col=1)
    fig.update_xaxes(title_text="1/I₂ − 1/I₁   (A⁻¹)", row=2, col=2)
    fig.update_yaxes(title_text="measured R step  (Ω)", row=2, col=2)

    def note(txt, r, c, x=0.03, y=0.97, ha="left", va="top"):
        fig.add_annotation(text=txt, row=r, col=c, xref="x domain",
                           yref="y domain", x=x, y=y, xanchor=ha, yanchor=va,
                           showarrow=False, align=ha,
                           font=dict(size=11.5, color=ps.INK_2),
                           bgcolor="rgba(252,252,251,0.96)", borderpad=4)

    note("dotted line = drive off.<br>The stroke is still at its<br>"
         "maximum here — the wire<br>has not cooled.", 1, 1, y=0.30)
    note("the whole cause: I falls to<br>the ~107 mA idle bias", 1, 2, y=0.55)
    note(f"slope = V_off = <b>{c3[0]*1e3:.0f} mV</b><br>"
         f"intercept = R at ambient = {c3[1]:.3f} Ω<br>R² = {r2_3:.2f}", 2, 1)
    note(f"slope through origin = <b>{slope*1e3:.0f} mV</b><br>R² = {ss:.2f}"
         f"<br>per-cycle V_off = {pts.V_off.mean()*1e3:.0f} ± "
         f"{pts.V_off.std()*1e3:.0f} mV", 2, 2)

    ps.layout(fig, 900)
    return fig, pts, dict(voff_onset=c3[0], r_amb=c3[1], r2_onset=r2_3,
                          voff_step=slope, r2_step=ss,
                          voff_mean=pts.V_off.mean(), voff_sd=pts.V_off.std(),
                          step_mean=pts.step_ohm.mean())


NOTES = """
<h2>What panel 1 shows</h2>
<p>Resistance jumps by <b>0.2–0.6 Ω in under 50 ms</b> at the instant the drive
turns off. At that same instant the stroke is still at its maximum (see
<code>transition_400ms.html</code>) and the recovery that follows takes 5–20 s.
There is no physical process in this wire that changes resistance that fast, so
this is the instrument, not the alloy. Panel 2 shows the only thing that
actually changed: the current.</p>

<h2>The model, and how far to trust it</h2>
<p>A series voltage offset, <code>R_meas = R_true + V_off/I</code>, would do
exactly this — it inflates R more the smaller the current. Two independent tests
agree:</p>
<ul>
<li><b>Panel 3</b> — in the first 4–20 ms of a pulse the wire has not warmed yet
whatever current it got, so R at ambient should be a straight line against 1/I.
It is, with slope <b>{voff_onset:.0f} mV</b>, though only R² {r2_onset:.2f}.</li>
<li><b>Panel 4</b> — the pulse-end step plotted against the model's geometric
factor. Forced through the origin (the model has no constant term, and fitting
one would let a nonzero intercept mask a failure) the slope is
<b>{voff_step:.0f} mV</b>, R² {r2_step:.2f}.</li>
</ul>
<p>Per-cycle the implied offset is <b>{voff_mean:.0f} ± {voff_sd:.0f} mV</b>.
Same order from every route, which is why the mechanism is stated as likely.
It is <i>not</i> pinned down well enough to correct with: at the 110 mA idle
bias that spread is a correction of ~0.2–0.3 Ω with ±0.15 Ω left over, against a
total heating excursion of 0.64 Ω.</p>

<h2>Why this matters</h2>
<p>The heating and cooling branches are measured at different currents, so they
sit on different R scales. Since R is the only temperature proxy on this rig,
<b>the stroke-vs-temperature hysteresis loop cannot be extracted from this
dataset.</b> Plotted naively it looks convincing — the cool-vs-heat stroke gap
at equal R measures 81–83 % of peak stroke, and that number is near-identical
across conditions that share nothing else, which is the tell. That is the
offset, not the alloy. The mechanical force-vs-stroke loop is clean but nearly
single-valued (3–6 % of peak), so it is not a substitute.</p>

<h2>What would fix it</h2>
<p>Sense R at a <b>constant current on both branches</b>: hold a fixed sense bias
through the cool phase, or use <code>Firmware_SMAConstantCurrent_PIO</code>,
which already runs closed-loop CC and streams <code>R_est</code> as src=7.
Separately, a room-temperature current sweep at fixed temperature would pin
<code>V_off</code> directly — and if it is a fixed instrumental offset, that one
measurement retroactively corrects every capture already taken.</p>
"""


def table_html(pts):
    g = (pts.groupby(["level_mA", "heat_ms"])
            .agg(n=("step_ohm", "size"), I1=("I1", "mean"), I2=("I2", "mean"),
                 R1=("R1", "mean"), R2=("R2", "mean"),
                 step=("step_ohm", "mean"), Voff=("V_off", "mean"))
            .reset_index().sort_values(["heat_ms", "level_mA"]))
    rows = "".join(
        f"<tr><td>{r.level_mA:.0f} mA · {r.heat_ms:.0f} ms</td><td>{r.n:.0f}</td>"
        f"<td>{r.I1*1e3:.0f}</td><td>{r.I2*1e3:.0f}</td><td>{r.R1:.3f}</td>"
        f"<td>{r.R2:.3f}</td><td>{r.step:+.3f}</td><td>{r.Voff*1e3:.0f}</td></tr>"
        for r in g.itertuples())
    return ("<table><thead><tr><th>condition</th><th>n</th><th>I heat (mA)</th>"
            "<th>I cool (mA)</th><th>R heat (Ω)</th><th>R cool (Ω)</th>"
            "<th>step (Ω)</th><th>implied V_off (mV)</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            "<p style='font-size:12.5px;color:#898781'>Means over the cycles of "
            "each condition. R heat is the last 45 ms of the pulse; R cool the "
            "first 20–100 ms after it. Raw per-cycle values are cached in "
            "<code>r_bias_points.csv</code>.</p>")


def main():
    fig, pts, s = build()
    stand = (f"{len(pts)} cycles · resistance jumps "
             f"{s['step_mean']:+.2f} Ω on average the instant the drive turns "
             f"off, while the wire is still hot. It is a bias-current artifact "
             f"(V_off ≈ {s['voff_step']*1e3:.0f} mV), and it is why the thermal "
             f"hysteresis loop cannot be measured from this data.")
    ps.page(OUT, "Why resistance is not comparable across the drive change",
            stand, fig,
            NOTES.format(voff_onset=s["voff_onset"] * 1e3,
                         r2_onset=s["r2_onset"],
                         voff_step=s["voff_step"] * 1e3, r2_step=s["r2_step"],
                         voff_mean=s["voff_mean"] * 1e3,
                         voff_sd=s["voff_sd"] * 1e3),
            table_html(pts))
    print(f"  -> {os.path.basename(OUT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
