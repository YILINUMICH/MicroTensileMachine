"""RAW CAPTURES -> PER-CYCLE TABLE.  Stage 1 of the standing analysis pipeline.

    python analyze_raw.py                    # all 2026-07-31 sweeps
    python analyze_raw.py sweep_20260731_155129 ...

Writes <sweep>/cycles.csv per sweep and the merged heat_time_map_<date>_all.csv.

═══ NO DATA SELECTION ═══════════════════════════════════════════════════════
Every commanded cycle produces exactly one row. Nothing is dropped, thresholded,
or averaged away — not clipped pulses, not railed ones, not sub-threshold ones,
not the bootstrap cycle. This table is RNN training data: deciding which pulses
"count" belongs to the training pipeline, and a network that only ever sees
clean pulses cannot learn that saturation and non-response are real machine
states. Quality is reported as COLUMNS (clipped / railed / cc_pct / bootstrap),
never as a filter.

The row count is therefore fixed by the COMMAND, not by what the detector
happened to find: 6 rows for a 5+1 condition, always.

═══ HOW HEAT WINDOWS ARE FOUND (this is the part that used to be wrong) ══════
Thresholding the current trace does not work and silently corrupts low levels.
The cool phase carries a 0.5 V idle bias -> ~107 mA whose noise reaches p95
~155 mA and p99.9 ~200 mA, so a 150-250 mA heat pulse sits INSIDE that band. At
100 ms there are too few samples to separate them, and with 30 s cools there are
~2.5x more noise excursions than the 12-15 s cools of 2026-07-30. Thresholding
returned 36 phantom cycles at 150x100 against 6 commanded, and 1-2 at
350/450/550x100 — cells that then looked "missing" from the envelope.

Instead, windows come from the firmware's OWN phase markers, in two steps:

  1. APPROXIMATE — the console log carries `[ACT] heat n=k/N` for every cycle,
     timestamped on the HOST clock, and `[STATUS] ... m7_us=` lines that pin the
     host clock to the M7 sample clock. A linear fit over the STATUS pairs maps
     each marker onto hw_us. Good to ~0.1 s, which is fine for a 400 ms pulse
     and NOT fine for a 100 ms one (console lines inherit USB batching).

  2. REFINE — a matched filter: slide a heat_ms-wide window +-0.5 s around the
     approximate start and take the position of maximum mean current. Even a
     150 mA pulse is a clear local maximum against the ~108 mA idle baseline
     once you know where to look. Recovers 150x100 at 150.4-154.7 mA over all
     six cycles, against the 36 phantom rows thresholding produced.

Validated against threshold detection where thresholding IS reliable (850x400):
window starts agree to within 0.1 s, and the refined values match the previous
per-cycle numbers to ~0.2%.

═══ OTHER CORRECTNESS POINTS ════════════════════════════════════════════════
  * hw_us time base, never host timestamps (USB-batched, sigma 3.4 ms).
  * M4/M7 clock offset from each capture's meta.json: src=1/2 (laser, load) are
    M4-stamped, src=3/4 M7-stamped, M7 runs ~2.19 s AHEAD. Untreated,
    displacement appears to peak BEFORE the current pulse that causes it.
  * Force peaks AFTER the current stops (thermal + mechanical lag), so the
    response window runs to heat_end + POST_S, not to heat_end.
  * `railed` is computed from the ENDPOINT (x_base+dx at the 0 V rail), not
    taken from any upstream flag — at 950x400 the laser sat at 0.0010 V for
    1228 samples yet came back unflagged. Pinned cycles all land on the same
    5027.7 um to 0.1 um; a real measurement does not repeat to that precision.
"""
import csv
import glob
import json
import os
import re
import sys

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))

SRC_LASER, SRC_LOAD, SRC_V, SRC_I = 1, 2, 3, 4
K_MV_PER_UM = -0.49779577092171906      # Calibrate_LaserHead
V0_MV = 2503.7500968693835
F_MN_PER_V = 1e3 / 10.200865238052671   # Calibrate_LoadCell
LASER_RAIL_UM = (0.0 - V0_MV) / K_MV_PER_UM     # +5030 um, the 0 V rail
LOAD_CLIP_V = 4.999
PRE_S = 0.40        # baseline window before heat onset
POST_S = 1.50       # response window past heat end (force lags the current)
SEARCH_S = 0.50     # matched-filter search radius

# (folder, i_low_mA, seeded, run_type). Provenance travels with every row.
RUNS = [
    ("sweep_20260731_134414", 100, 0, "probe"),
    ("sweep_20260731_141513", 100, 1, "probe"),
    ("sweep_20260731_144243", 100, 1, "probe"),
    ("sweep_20260731_145838", 100, 1, "map"),
    ("sweep_20260731_155129",   0, 1, "map"),
]
MERGED = "heat_time_map_20260731_all.csv"


WRAP_S = 2 ** 32 / 1e6          # micros() is 32-bit: rolls over every 4294.97 s


def unwrap_us(us):
    """Undo the 32-bit micros() rollover.

    hw_us wraps every ~71.6 min, and a 2 h campaign straddles it. Hit for real
    on 2026-07-31: c20_level_550mA_h400ms wrapped mid-record, its apparent span
    became the full 4295 s, the host->M7 fit came out with a NEGATIVE slope, and
    every window landed outside the data — six all-NaN cycles. The firmware
    guards its own due-checks against this ('a run can straddle that'); the
    host-side analysis did not.

    Counts backward jumps larger than half the range and adds a period each."""
    us = np.asarray(us, dtype=np.float64)
    if us.size < 2:
        return us
    return us + WRAP_S * 1e6 * np.cumsum(np.r_[0, np.diff(us) < -2 ** 31])


def disp_um(v_series):
    return (v_series * 1e3 - V0_MV) / K_MV_PER_UM


def schedule_windows(log_path, heat_ms):
    """Approximate heat starts on the M7 clock, from the firmware's markers."""
    host, m7, heats = [], [], []
    with open(log_path, errors="ignore") as fh:
        for line in fh:
            m = re.match(r"\[\s*([\d.]+)\]", line)
            if not m:
                continue
            t = float(m.group(1))
            if "[STATUS]" in line:
                g = re.search(r"m7_us=(\d+)", line)
                if g:
                    host.append(t)
                    m7.append(int(g.group(1)) / 1e6)
            elif "[ACT] heat" in line:
                heats.append(t)
    if len(host) < 2 or not heats:
        return []
    m7 = unwrap_us(np.asarray(m7) * 1e6) / 1e6      # STATUS m7_us wraps too
    fit = np.polyfit(host, m7, 1)
    return [float(np.polyval(fit, h)) for h in heats]


def refine(t, i, t_approx, heat_s):
    """Matched filter: the heat_s window with the highest mean current."""
    m = (t > t_approx - SEARCH_S) & (t < t_approx + SEARCH_S + heat_s)
    tt, ii = t[m], i[m]
    if len(tt) < 10:
        return None
    best_v, best_t = -np.inf, None
    for c in tt[tt <= t_approx + SEARCH_S]:
        w = (tt >= c) & (tt < c + heat_s)
        if w.sum() < 5:
            continue
        v = ii[w].mean()
        if v > best_v:
            best_v, best_t = v, c
    return best_t


def analyze_capture(csv_path):
    stem = csv_path[:-4]
    log_path = stem + ".console.log"
    meta_path = stem + ".meta.json"
    name = os.path.basename(stem)
    g = re.search(r"level_(\d+)mA_h(\d+)ms", name)
    if not g or not os.path.exists(log_path):
        return []
    level_mA, heat_ms = int(g.group(1)), int(g.group(2))
    heat_s = heat_ms / 1000.0

    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    off = float(meta.get("m4_clock_offset_s", 0.0))
    cool_s = float(meta.get("cool_s", np.nan))

    d = pd.read_csv(csv_path)
    # Unwrap per src: each stream is monotonic in hw_us on its own, but they
    # interleave in the file, so a global unwrap would see false backward jumps.
    d["t"] = np.nan
    for s_id, grp in d.groupby("src", sort=False):
        d.loc[grp.index, "t"] = unwrap_us(grp["hw_us"].to_numpy()) / 1e6
    d.loc[d["src"].isin((SRC_LASER, SRC_LOAD)), "t"] += off   # onto the M7 clock

    cur = d[d["src"] == SRC_I]
    t_i, v_i = cur["t"].to_numpy(), cur["value"].to_numpy()
    volt = d[d["src"] == SRC_V]
    t_v, v_v = volt["t"].to_numpy(), volt["value"].to_numpy()
    las = d[d["src"] == SRC_LASER]
    t_x, v_x = las["t"].to_numpy(), las["value"].to_numpy()
    ld = d[d["src"] == SRC_LOAD]
    t_f, v_f = ld["t"].to_numpy(), ld["value"].to_numpy()

    rows = []
    for k, t_ap in enumerate(schedule_windows(log_path, heat_ms), 1):
        t0 = refine(t_i, v_i, t_ap, heat_s)
        row = {
            "level_mA": level_mA, "heat_ms": heat_ms, "cycle": k,
            "bootstrap": int(k == 1), "cool_s": cool_s,
            "i_mA": np.nan, "u_V": np.nan, "R_ohm": np.nan, "cc_pct": np.nan,
            "dx_um": np.nan, "x_base_um": np.nan,
            "F_base_mN": np.nan, "dF_mN": np.nan,
            "clipped": 0, "railed": 0, "n_samples": 0,
        }
        if t0 is not None:
            t1 = t0 + heat_s
            hw = (t_i >= t0) & (t_i < t1)
            row["n_samples"] = int(hw.sum())
            if hw.sum():
                row["i_mA"] = float(1e3 * v_i[hw].mean())
                row["cc_pct"] = round(100.0 * row["i_mA"] / level_mA, 1)
            hv = (t_v >= t0) & (t_v < t1)
            if hv.sum():
                row["u_V"] = float(v_v[hv].mean())
                if row["i_mA"] and row["i_mA"] == row["i_mA"]:
                    row["R_ohm"] = row["u_V"] / (row["i_mA"] / 1e3)
            # laser: baseline before onset, farthest excursion through the tail
            pre = (t_x >= t0 - PRE_S) & (t_x < t0 - 0.02)
            post = (t_x >= t0) & (t_x <= t1 + POST_S)
            if pre.sum() and post.sum():
                xb = v_x[pre].mean()
                far = v_x[post][np.argmax(np.abs(v_x[post] - xb))]
                row["x_base_um"] = float(disp_um(xb))
                row["dx_um"] = float(disp_um(far) - disp_um(xb))
                row["railed"] = int(row["x_base_um"] + row["dx_um"]
                                    >= LASER_RAIL_UM - 30.0)
            pref = (t_f >= t0 - PRE_S) & (t_f < t0 - 0.02)
            postf = (t_f >= t0) & (t_f <= t1 + POST_S)
            if pref.sum() and postf.sum():
                fb = v_f[pref].mean()
                pk = v_f[postf].max()
                row["F_base_mN"] = float(fb * F_MN_PER_V)
                row["dF_mN"] = float((pk - fb) * F_MN_PER_V)
                row["clipped"] = int(pk >= LOAD_CLIP_V)
        rows.append(row)
    return rows


def analyze_sweep(folder, i_low, seeded, run_type):
    out = []
    pat = os.path.join(BASE, folder, "c*_level_*mA_h*ms.csv")
    for f in sorted(glob.glob(pat)):
        if f.endswith("_report.csv"):
            continue
        for r in analyze_capture(f):
            r.update(sweep=folder, run_type=run_type,
                     i_low_mA=i_low, seeded=seeded)
            out.append(r)
    if out:
        cols = list(out[0].keys())
        with open(os.path.join(BASE, folder, "cycles.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(out)
    print(f"  {folder:26s} {len(out):4d} cycles")
    return out


def main(argv):
    want = set(argv) if argv else None
    allrows = []
    for folder, i_low, seeded, run_type in RUNS:
        if want and folder not in want:
            continue
        if not os.path.isdir(os.path.join(BASE, folder)):
            print(f"  {folder:26s} MISSING — skipped")
            continue
        allrows += analyze_sweep(folder, i_low, seeded, run_type)
    if not allrows:
        return 1
    order = ["level_mA", "heat_ms", "sweep", "run_type", "cycle", "bootstrap",
             "i_mA", "cc_pct", "u_V", "R_ohm", "dx_um", "x_base_um",
             "F_base_mN", "dF_mN", "clipped", "railed", "n_samples",
             "cool_s", "i_low_mA", "seeded"]
    allrows.sort(key=lambda r: (r["heat_ms"], r["level_mA"], r["sweep"],
                                r["cycle"]))
    with open(os.path.join(BASE, MERGED), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=order, extrasaction="ignore")
        w.writeheader()
        w.writerows(allrows)
    n_cond = len({(r["level_mA"], r["heat_ms"]) for r in allrows})
    print(f"\n{len(allrows)} cycles over {n_cond} conditions -> {MERGED}")
    print(f"  clipped={sum(r['clipped'] for r in allrows)}  "
          f"railed={sum(r['railed'] for r in allrows)}  "
          f"bootstrap={sum(r['bootstrap'] for r in allrows)}  "
          f"(all RETAINED)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
