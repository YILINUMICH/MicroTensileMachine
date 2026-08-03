"""The per-pulse table, plus TRUE delivered energy — shared by the plots.

    from energy_table import load
    d = load()          # tidy frame, quality-filtered, with E_J

═══ WHY THIS EXISTS ════════════════════════════════════════════════════════
`heat_time_map_*_all.csv` already carries `p_hot_W`, so the obvious energy is
`p_hot_W * heat_ms/1000`. That is WRONG in a way that matters here.

`p_hot_W` is a TAIL MEDIAN (analyze_raw.py:278) — the power at the END of the
pulse, not its average. Power droops during a pulse as the wire heats and its
resistance climbs, so tail x duration under-reads the delivered energy, and it
under-reads MORE for longer pulses:

    850 mA / 100 ms   integral 0.3156 J   tail x t 0.3104 J   -1.7%
    850 mA / 400 ms   integral 1.2233 J   tail x t 1.1661 J   -4.9%

A ~3 point pulse-length-dependent bias is not tolerable in a chart whose whole
question is "does the response depend only on energy, or separately on duration
too?" — it would inject a fake duration dependence of about the same size as the
real one. So E is integrated from the actual power stream.

═══ COST ══════════════════════════════════════════════════════════════════
Integration needs the raw capture per cycle (~0.3 s each, ~1 min for the set),
so the result is CACHED next to the table as `*_energy.csv`. Delete that file
or pass refresh=True to rebuild. The cache is keyed by (sweep, level, heat,
cycle), so adding a sweep only costs the new cycles.

═══ NO DATA SELECTION ═════════════════════════════════════════════════════
`load()` DOES NOT DROP ROWS. The module rule (root README, "NO DATA SELECTION")
is that every commanded cycle produces exactly one row and every cycle is drawn;
quality is reported as COLUMNS, never applied as a filter, because the table is
RNN training data and a network that only ever sees clean pulses cannot learn
that saturation and non-response are real machine states.

So `load()` adds a `usable` flag instead, and the plots use it to decide what
goes into a FIT — not what goes on the chart. Rail-limited cycles are drawn
hollow, exactly as `plot_envelope.py` draws them.

  bootstrap    cycle 1 fires into a fully relaxed wire — a genuinely different
               initial condition, so it is drawn but held out of fits
  clipped      load cell hit its rail — dF is a LOWER BOUND, not a measurement
  railed       laser hit the end of its analog window — same
  detect_ok=0  the heat window could not be refined, so t_heat and therefore E
               are unreliable; these have no x coordinate on an energy axis and
               drop out for want of one. That is missing data, not selection —
               `load()` reports how many.

A fit over lower bounds is biased toward them, which is why `usable` exists at
all; it is a statement about the FIT, not about the data.
"""
import os

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
DERIVED = os.path.join(_HERE, "..", "data", "derived")   # pipeline outputs
TABLE = os.path.join(DERIVED, "heat_time_map_20260731_all.csv")
CACHE = os.path.join(DERIVED, "heat_time_map_20260731_all_energy.csv")
KEY = ["sweep", "level_mA", "heat_ms", "cycle"]


def _integrate_one(sweep, level_mA, heat_ms, cycle):
    """∫P dt over the refined heat window, in joules."""
    from get_cycle import get_cycle          # local: only needed on a rebuild
    df = get_cycle(sweep, level_mA, heat_ms, cycle=cycle, pre_s=0.2, post_s=0.2)
    h = df[df.phase == "heat"]
    if len(h) < 5 or "power_W" not in h:
        return np.nan, np.nan
    # np.trapezoid on the firmware clock. Not a Riemann sum over samples: the
    # SMA stream is ~1 kHz but not perfectly uniform, and weighting by dt is
    # what makes this an energy rather than a sample average.
    return float(np.trapezoid(h.power_W, h.t_s)), float(h.t_s.max() - h.t_s.min())


def build(refresh=False, verbose=True):
    """Integrate E for every cycle in the table; cache and return it."""
    t = pd.read_csv(TABLE)
    have = pd.read_csv(CACHE) if (os.path.exists(CACHE) and not refresh) \
        else pd.DataFrame(columns=KEY + ["E_int_J", "t_heat_s"])
    todo = t.merge(have[KEY], on=KEY, how="left", indicator=True)
    todo = todo[todo._merge == "left_only"]
    if todo.empty:
        return have
    if verbose:
        print(f"integrating {len(todo)} cycles (cached: {len(have)}) ...")
    rows, bad = [], 0
    for _, r in todo.iterrows():
        try:
            e, dur = _integrate_one(r.sweep, r.level_mA, r.heat_ms, int(r.cycle))
        except Exception:
            e, dur, bad = np.nan, np.nan, bad + 1
        rows.append({**{k: r[k] for k in KEY}, "E_int_J": e, "t_heat_s": dur})
    new = pd.DataFrame(rows)
    out = new if have.empty else pd.concat([have, new], ignore_index=True)
    out.to_csv(CACHE, index=False)
    if verbose:
        print(f"  -> {os.path.basename(CACHE)}  ({len(out)} cycles, "
              f"{bad} could not be integrated)")
    return out


def load(verbose=False):
    """The whole table + E_J + a `usable` flag. Nothing is dropped."""
    d = pd.read_csv(TABLE).merge(build(verbose=verbose), on=KEY, how="left")

    # TRUE energy where the integral exists; fall back to the tail-median
    # product where it does not, and SAY SO in a column rather than silently
    # mixing two different quantities in one axis.
    d["E_tail_J"] = d.p_hot_W * d.heat_ms / 1000.0
    d["E_J"] = d.E_int_J.where(d.E_int_J.notna(), d.E_tail_J)
    d["E_is_integrated"] = d.E_int_J.notna()

    d["dR_pct"] = 100.0 * (d.r_hot_ohm - d.r_base_ohm) / d.r_base_ohm
    d["dR_ohm"] = d.r_hot_ohm - d.r_base_ohm

    # A quality COLUMN, not a filter. `at_rail` is kept separate because it is
    # the one the charts encode visually (hollow marks).
    d["at_rail"] = (d.clipped == 1) | (d.railed == 1)
    d["usable"] = ((d.bootstrap == 0) & (d.detect_ok == 1) & ~d.at_rail
                   & d.E_J.notna())
    if verbose:
        print(f"{len(d)} cycles loaded, none dropped · {int(d.usable.sum())} "
              f"usable for fits "
              f"(held out: {int((d.bootstrap == 1).sum())} bootstrap, "
              f"{int(d.at_rail.sum())} at a sensor rail, "
              f"{int((d.detect_ok == 0).sum())} unrefined window)")
    return d.reset_index(drop=True)


if __name__ == "__main__":
    d = load(verbose=True)[lambda x: x.usable]
    frac = 100.0 * (d.E_J / d.E_tail_J - 1.0)
    print("\nintegrated vs tail-median energy, % by pulse length:")
    print(frac.groupby(d.heat_ms).agg(["mean", "std"]).round(2))
