"""Pull the raw time series for ONE cycle out of a heat-time-map capture.

heat_time_map_20260731_all.csv is one ROW PER PULSE — the label table. The
per-sample streams live in the raw captures; this bridges the two.

    from get_cycle import get_cycle, list_cycles
    df = get_cycle("sweep_20260731_155129", 850, 400, cycle=3)

Returns a tidy frame on ONE time base, t=0 at heat onset:

    t_s  sma_v  sma_i  sma_r  power_W  laser_mm  force_mN  phase

TWO THINGS THIS HANDLES THAT NAIVE JOINS GET WRONG
  1. M4/M7 clock offset. src=1/2 (laser, load) are stamped by the M4, src=3/4/5
     by the M7, which runs ~2.19 s AHEAD. Joined untreated, displacement appears
     to peak BEFORE the current pulse that causes it. The per-condition
     meta.json records the measured offset; it is applied here.
  2. Time base. Uses hw_us (firmware clock), never host timestamps — those are
     USB-batched (median dt 1.06 ms, sigma 3.4 ms) and smear any transient.

Sensor conversions use the Calibrate_* fits; sma_v/sma_i carry the known ~+7%
conversion-duty bias (sma_r is immune — both channels scale together).
"""
import glob
import json
import os

import numpy as np
import pandas as pd

from analyze_raw import (resolve_sweep, schedule_windows, refine,
                         unwrap_us as _unwrap)

_HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(_HERE, "..", "data", "raw")      # capture folders, as the rig wrote them

WRAP_S = 2 ** 32 / 1e6      # micros() is 32-bit: rolls over every 4294.97 s


def unwrap_us(us):
    """Undo the 32-bit micros() rollover before ANY arithmetic on hw_us.

    Flagged by the NN-side DATA_COLLECTION_GUIDELINE §4: this module used to do
    time arithmetic on the raw counter and was wrong across a wrap. Real case:
    c20_level_550mA_h400ms straddles one, so its apparent span became the full
    4295 s. Applied PER SRC — the streams interleave in the file, so a global
    unwrap would read the interleaving as backward jumps."""
    us = np.asarray(us, dtype=np.float64)
    if us.size < 2:
        return us
    return us + WRAP_S * 1e6 * np.cumsum(np.r_[0, np.diff(us) < -2 ** 31])

K_MV_PER_UM = -0.49779577092171906     # Calibrate_LaserHead
V0_MV = 2503.7500968693835
F_MN_PER_V = 1e3 / 10.200865238052671  # Calibrate_LoadCell
SRC = {"laser": 1, "load": 2, "sma_v": 3, "sma_i": 4, "sma_r": 5,
       "cc_u": 6, "cc_r_est": 7}


def _capture(sweep, level_mA, heat_ms):
    # `sweep` is a BARE FOLDER NAME (or a path relative to data/raw); the
    # resolver finds it wherever it is filed under campaigns/.
    pat = os.path.join(resolve_sweep(sweep),
                       f"c*_level_{int(level_mA)}mA_h{int(heat_ms)}ms.csv")
    hits = [f for f in glob.glob(pat) if not f.endswith("_report.csv")]
    if not hits:
        raise FileNotFoundError(f"no capture for {level_mA} mA / {heat_ms} ms in {sweep}")
    return hits[0]


def _heat_windows(path, t_i, v_i, heat_ms):
    """Heat windows from the firmware's OWN schedule, then matched-filter
    refined — the same path analyze_raw.py uses, imported rather than
    reimplemented so the two can never drift apart.

    This replaces threshold detection, which failed on exactly the cells that
    matter most at the low end: a 150-250 mA pulse sits inside the ~107 mA idle
    bias noise band (p95 ~155 mA), so thresholding found 1 window where 6 were
    commanded and this function used to raise
    `cycle 3 of 1 detected in c00_level_150mA_h100ms`."""
    heat_s = heat_ms / 1000.0
    approx, _, _, _ = schedule_windows(path[:-4] + ".console.log", heat_ms)
    out = []
    for t_ap in approx:
        t0 = refine(t_i, v_i, t_ap, heat_s)
        if t0 is not None:
            out.append((t0, t0 + heat_s))
    return out


def list_cycles(sweep, level_mA, heat_ms):
    """(start, end) heat windows on the unwrapped M7 clock."""
    path = _capture(sweep, level_mA, heat_ms)
    d = pd.read_csv(path)
    cur = d[d["src"] == SRC["sma_i"]]
    t_i = unwrap_us(cur["hw_us"].to_numpy()) / 1e6
    return _heat_windows(path, t_i, cur["value"].to_numpy(), heat_ms)


def get_cycle(sweep, level_mA, heat_ms, cycle=1, pre_s=2.0, post_s=8.0):
    """One cycle as a tidy frame. `cycle` is 1-based and matches the `cycle`
    column of heat_time_map_20260731_all.csv. pre_s/post_s extend the window
    before heat onset and after heat end (the force peak LAGS the current by
    up to ~1.5 s, so keep post_s generous)."""
    path = _capture(sweep, level_mA, heat_ms)
    meta_path = path.replace(".csv", ".meta.json")
    off = 0.0
    if os.path.exists(meta_path):
        off = float(json.load(open(meta_path)).get("m4_clock_offset_s", 0.0))

    d = pd.read_csv(path)
    d["t"] = np.nan
    for _s, grp in d.groupby("src", sort=False):
        d.loc[grp.index, "t"] = unwrap_us(grp["hw_us"].to_numpy()) / 1e6
    # src=1/2 are M4-stamped: shift ONTO the M7 clock.
    d.loc[d["src"].isin((SRC["laser"], SRC["load"])), "t"] += off

    cur = d[d["src"] == SRC["sma_i"]]
    wins = _heat_windows(path, cur["t"].to_numpy(), cur["value"].to_numpy(),
                         heat_ms)
    if not 1 <= cycle <= len(wins):
        raise IndexError(f"cycle {cycle} of {len(wins)} detected in "
                         f"{os.path.basename(path)}")
    t0, t1 = wins[cycle - 1]

    lo, hi = t0 - pre_s, t1 + post_s
    out = {}
    for name, s in SRC.items():
        x = d[(d["src"] == s) & (d["t"] >= lo) & (d["t"] <= hi)]
        if not x.empty:
            out[name] = pd.Series(x["value"].to_numpy(),
                                  index=np.round(x["t"].to_numpy() - t0, 6))

    df = pd.DataFrame(out).sort_index()
    df.index.name = "t_s"
    # Streams arrive at different rates (SMA ~1 kHz, laser/load 400 SPS
    # converted); interpolate onto the union rather than dropping rows.
    df = df.interpolate(limit_direction="both")

    if "laser" in df:
        df["laser_mm"] = ((df["laser"] * 1e3 - V0_MV) / K_MV_PER_UM) / 1e3
    if "load" in df:
        df["force_mN"] = df["load"] * F_MN_PER_V
    if {"sma_v", "sma_i"} <= set(df):
        df["power_W"] = df["sma_v"] * df["sma_i"]
        # The CC fork does NOT emit src=5 — it streams 3/4 plus its own 6/7 —
        # so resistance is derived. This is the self-sensing temperature proxy
        # and the RNN's headline input. It is also the ONE quantity immune to
        # the ~+7% conversion-duty bias on sma_v/sma_i: both channels scale
        # together, so the ratio cancels it exactly.
        if "sma_r" not in df:
            df["sma_r"] = np.where(df["sma_i"] > 0.02,
                                   df["sma_v"] / df["sma_i"], np.nan)
    df["phase"] = np.where(df.index < 0, "pre",
                           np.where(df.index <= (t1 - t0), "heat", "cool"))
    return df.reset_index()


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if len(a) < 3:
        print(__doc__)
        raise SystemExit(1)
    sweep, lvl, heat = a[0], float(a[1]), int(a[2])
    cyc = int(a[3]) if len(a) > 3 else 1
    df = get_cycle(sweep, lvl, heat, cyc)
    print(df.to_string(index=False))
