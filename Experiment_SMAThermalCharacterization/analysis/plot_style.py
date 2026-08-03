"""Shared chart chrome for the interactive HTML figures.

Both plots import this rather than each carrying a copy — the ADS1263 driver in
this repo is copied between projects and drifts, and there is no reason to
repeat that here for something as easy to diverge as a palette.

═══ PALETTE ═══════════════════════════════════════════════════════════════
Pulse length is an ORDERED category (100/200/300/400 ms), so it gets a one-hue
ORDINAL ramp, not categorical hues. Validated with the data-viz validator:

    validate_palette.py "#86b6ef,#3987e5,#1c5cab,#0d366b" --mode light --ordinal
      [PASS] Lightness monotone   steps read light->dark
      [PASS] Adjacent dL          all gaps >= 0.06
      [PASS] Light-end contrast   #86b6ef at 2.06:1 vs surface
      [PASS] Single hue           hue spread 4 deg

These are SCATTER panels, so identity is read pairwise (all-pairs), not just
between neighbours. Under CVD the ramp is comfortable — worst pair protan 14.6 /
deutan 14.2, against a target of 8 — but the worst NORMAL-vision pair is 14.4,
just under the 15 floor a categorical palette would have to clear. Rather than
argue the ordinal gate exempts it, every series also carries a distinct MARKER
SHAPE, so no pair is ever separated by hue alone.
"""

# ordinal ramp: 100 -> 400 ms, light -> dark
RAMP = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
SYMBOL = ["circle", "square", "diamond", "triangle-up"]
HEATS = [100, 200, 300, 400]
COLOR = dict(zip(HEATS, RAMP))
SHAPE = dict(zip(HEATS, SYMBOL))

SURFACE = "#fcfcfb"
PLANE = "#f9f9f7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
FIT = "#898781"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def axes(fig, **kw):
    """Recessive hairline grid + axes, solid (never dashed)."""
    fig.update_xaxes(gridcolor=GRID, gridwidth=1, zeroline=False,
                     linecolor=AXIS, linewidth=1, ticks="outside",
                     tickcolor=AXIS, ticklen=4,
                     tickfont=dict(color=MUTED, size=11), **kw)
    fig.update_yaxes(gridcolor=GRID, gridwidth=1, zeroline=False,
                     linecolor=AXIS, linewidth=1, ticks="outside",
                     tickcolor=AXIS, ticklen=4,
                     tickfont=dict(color=MUTED, size=11), **kw)


def log_ticks(lo, hi):
    """A 1-2-5 tick ladder spanning [lo, hi], for log axes.

    Plotly's log default labels every minor tick (2,3,...,9 per decade) — ~25
    numbers on a 2.5-decade axis. Its dtick=1 alternative labels DECADES only,
    which on an axis spanning 1.7 decades (the power panel) leaves a single
    number. 1-2-5 is the readable middle: 3-4 labels per decade, at values a
    reader can interpolate between.
    """
    import math
    vals, e = [], math.floor(math.log10(lo))
    while 10.0 ** e <= hi * 10:
        for m in (1, 2, 5):
            v = m * 10.0 ** e
            if lo <= v <= hi:
                vals.append(v)
        e += 1
    txt = [(f"{v:,.0f}" if v >= 1 else f"{v:g}") for v in vals]
    return dict(tickmode="array", tickvals=vals, ticktext=txt)


def marker(heat_ms, size=9, hollow=False):
    """Mark spec: >=8px, with a 2px SURFACE RING so overlapping points separate
    without drawing a border around them.

    hollow=True is the module's standing convention for a cycle sitting at a
    sensor rail (see plot_envelope.py and the root README): the value is a LOWER
    BOUND, not a measurement, so it is drawn — never dropped — but must not read
    as an exact point. The shape still carries pulse length, so the open variant
    costs no encoding.
    """
    if hollow:
        return dict(size=size, color="rgba(0,0,0,0)",
                    symbol=SHAPE[heat_ms] + "-open",
                    line=dict(width=1.6, color=COLOR[heat_ms]), opacity=0.95)
    return dict(size=size, color=COLOR[heat_ms], symbol=SHAPE[heat_ms],
                line=dict(width=2, color=SURFACE), opacity=0.95)


def layout(fig, height):
    fig.update_layout(
        height=height, paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family=FONT, size=12, color=INK_2),
        margin=dict(l=70, r=30, t=95, b=70),
        hovermode="closest",
        hoverlabel=dict(bgcolor="#ffffff", bordercolor=AXIS,
                        font=dict(family=FONT, size=11, color=INK)),
        legend=dict(orientation="h", y=1.055, x=0, xanchor="left",
                    yanchor="bottom", bgcolor="rgba(0,0,0,0)",
                    font=dict(color=INK_2, size=12),
                    title=dict(text="pulse length  ", side="left",
                               font=dict(color=MUTED, size=12))),
    )
    for a in fig.layout.annotations:
        if a.text and a.font and a.font.size is None:
            a.font.size = 12.5
    return fig


def page(path, title, standfirst, fig, notes_html, table_html):
    """Write the standalone page: figure + what-it-means + a TABLE VIEW twin.

    The table is not decoration. A chart that can only be read by hovering
    gates its values behind a pointer; the table is the same numbers in a form
    that copies, searches, and survives being printed.
    """
    import plotly.io as pio
    # plotly.js is INLINED, not pulled from a CDN. These pages get opened on the
    # bench machine and emailed around; a CDN reference renders a blank white
    # box the moment there is no internet, which is the worst possible failure
    # for a figure someone is trying to read. Costs ~3.5 MB per file.
    body = pio.to_html(fig, full_html=False, include_plotlyjs=True,
                       config={"displaylogo": False, "scrollZoom": True,
                               "toImageButtonOptions": {"format": "png",
                                                        "scale": 2}})
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:{PLANE}; color:{INK};
         font-family:{FONT}; line-height:1.55; }}
  .wrap {{ max-width:1280px; margin:0 auto; padding:32px 24px 72px; }}
  h1 {{ font-size:23px; font-weight:600; margin:0 0 6px; letter-spacing:-0.01em; }}
  .standfirst {{ color:{INK_2}; font-size:14.5px; margin:0 0 22px; max-width:76ch; }}
  .card {{ background:{SURFACE}; border:1px solid rgba(11,11,11,0.10);
           border-radius:10px; padding:8px; }}
  .notes {{ margin-top:28px; max-width:76ch; font-size:14px; color:{INK_2}; }}
  .notes h2 {{ font-size:15px; color:{INK}; margin:22px 0 6px; font-weight:600; }}
  .notes p {{ margin:0 0 10px; }}
  .notes code {{ background:#f0efec; padding:1px 5px; border-radius:4px;
                 font-size:12.5px; }}
  details {{ margin-top:28px; }}
  summary {{ cursor:pointer; font-size:14px; color:{INK_2}; font-weight:500;
             padding:8px 0; }}
  table {{ border-collapse:collapse; font-size:12.5px;
           font-variant-numeric:tabular-nums; margin-top:10px; }}
  th, td {{ text-align:right; padding:5px 11px;
            border-bottom:1px solid {GRID}; }}
  th {{ color:{MUTED}; font-weight:500; text-align:right;
        border-bottom:1px solid {AXIS}; }}
  td:first-child, th:first-child {{ text-align:left; }}
  .tw {{ overflow-x:auto; }}
</style></head>
<body><div class="wrap">
<h1>{title}</h1>
<p class="standfirst">{standfirst}</p>
<div class="card">{body}</div>
<div class="notes">{notes_html}</div>
<details><summary>Table view — the same numbers, per condition</summary>
<div class="tw">{table_html}</div></details>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
