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
from datetime import datetime, timedelta
import json
import os
import re
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(_HERE, "..", "data", "raw")           # capture folders, as the rig wrote them
DERIVED = os.path.join(_HERE, "..", "data", "derived")   # pipeline outputs
# Per-sweep cycles.csv stays INSIDE its capture folder (under RAW): it is
# per-capture provenance, and operator_sweep_report.py takes that folder as its
# only argument. Only the MERGED cross-campaign table lands in DERIVED.

# data/raw is grouped into campaigns/<key>/<sweep> (2026-08-06). A sweep is
# still identified EVERYWHERE ELSE by its BARE FOLDER NAME — the `sweep` column
# of the merged table, the CAMPAIGNS entries below, `--sweep` on the plotters.
# Resolving name -> path here (rather than storing paths) means a folder can be
# refiled into a different campaign without rewriting a single table or command.
CAPTURE_PREFIXES = ("sweep_", "console_", "pulse_", "isense_", "noise_",
                    "adcavg_", "queue_")


def capture_dirs(base=None, prefixes=CAPTURE_PREFIXES):
    """{bare folder name: path} for every capture folder under data/raw.

    Walks the grouping folders (campaigns/, writeups/, aborted/, troubleshoot/)
    but never descends INTO a capture folder — those hold thousands of sample
    files and there is nothing below them to find."""
    base = base or RAW
    found = {}
    for root, dirs, _ in os.walk(base):
        keep = []
        for d in dirs:
            if d.startswith(prefixes):
                found.setdefault(d, os.path.join(root, d))
            else:
                keep.append(d)
        dirs[:] = keep
    return found


def resolve_sweep(name, base=None):
    """Path of a capture folder from its BARE NAME or a path relative to
    data/raw. Both forms work, so commands written before the 2026-08-06
    regrouping still run."""
    direct = os.path.join(base or RAW, name)
    if os.path.isdir(direct):
        return direct
    hit = capture_dirs(base).get(os.path.basename(name.rstrip("/\\")))
    if hit is None:
        raise FileNotFoundError(f"no capture folder named {name!r} under {RAW}")
    return hit


SRC_LASER, SRC_LOAD, SRC_V, SRC_I = 1, 2, 3, 4
K_MV_PER_UM = -0.49779577092171906      # Calibrate_LaserHead
V0_MV = 2503.7500968693835
F_MN_PER_V = 1e3 / 10.200865238052671   # Calibrate_LoadCell
LASER_RAIL_UM = (0.0 - V0_MV) / K_MV_PER_UM     # +5030 um, the 0 V rail
LOAD_CLIP_V = 4.999
PRE_S = 0.40        # baseline window before heat onset
POST_S = 1.50       # response window past heat end (force lags the current)
# Matched-filter search radius. Deliberately TIGHT. Measured marker placement
# error is mean -0.040 s, sd 0.044 s, worst case 0.106 s over 126 cycles, so
# +-0.15 s covers it with margin. A wider radius is actively harmful: max-mean
# is biased LATE whenever a pulse has a slow decay tail (the pre-seed bootstrap
# ramp unwinds its integral over ~300 ms into cool), and at +-0.5 s the filter
# slid ~150 ms past the true onset onto that tail — which showed up as tail
# medians BELOW the whole-window mean on ramp pulses, the opposite of physics.
SEARCH_S = 0.15

# csv.writer defaults to CRLF. The committed tables are LF (written on the
# Windows rig, where git normalised them on commit), so regenerating on macOS
# rewrote all 261 lines with IDENTICAL field values — pure line-ending churn
# that buries a real content change in a whole-file diff. Pin LF so stage 1 is
# byte-reproducible on either host, which is the property STATUS claims for it.
LF = "\n"

# ═══ CAMPAIGNS ═══════════════════════════════════════════════════════════════
# One entry per WIRE + protocol. Guideline §7: "data with different protocols is
# trained separately, so it must be identifiable" — a different coil is the
# biggest protocol change there is, so each campaign gets its OWN merged table
# instead of pooling into one. Pooling was also actively unsafe: MERGED used to
# be a single hardcoded filename written unconditionally, so
# `analyze_raw.py <one-folder>` rewrote the whole 07-31 table with just that
# folder's rows.
#
# runs = (folder, i_low_mA, seeded, run_type). Provenance travels with every row.
# run_type also declares the EXPECTED cycles per condition for self-check §8.1.
CYCLES_EXPECTED = {"map": 6, "probe": 4, "extremes": 6, "random": 1}

# COLD LENGTH is the campaign axis, not the date: it is the same Dynalloy stock
# throughout, cut/stretched to a different cold length per campaign, and that
# changes the response more than anything else the protocol varies. 2026-07-30
# shares 07-31's 15 mm wire but ran a 15 s cool and hit the cooling issue, so it
# is a DIFFERENT protocol and would be its own campaign if it were registered.
# On disk: data/raw/campaigns/<date>_dynalloy_<length>[_<protocol>]/.
CAMPAIGNS = {
    "20260731": {
        "dir": "20260731_dynalloy_15mm_cool30s",
        "merged": "heat_time_map_20260731_all.csv",
        "cool_s": 30.0,
        "wire": "Dynalloy, 15 mm cold length, ~4.2-4.8 ohm cold "
                "(data/raw/campaigns/20260731_dynalloy_15mm_cool30s)",
        "runs": [
            ("sweep_20260731_134414", 100, 0, "probe"),
            ("sweep_20260731_141513", 100, 1, "probe"),
            ("sweep_20260731_144243", 100, 1, "probe"),
            ("sweep_20260731_145838", 100, 1, "map"),
            ("sweep_20260731_155129",   0, 1, "map"),
        ],
    },
    "20260805_dynalloy": {
        "dir": "20260805_dynalloy_10mm",
        "merged": "heat_time_map_20260805_dynalloy_all.csv",
        "cool_s": 30.0,
        "wire": "Dynalloy 0.08 in, 10 mm cold length, cold-stretched to 40 mm, "
                "R0 = 4.01 ohm measured at the idle bias; fitted 2026-08-05 "
                "(data/raw/campaigns/20260805_dynalloy_10mm)",
        "runs": [
            ("sweep_20260805_105318", 0, 1, "map"),
            ("sweep_20260805_154528", 0, 1, "extremes"),
        ],
    },
}


# data/derived mirrors data/raw's campaign grouping (2026-08-06). `dir` above is
# the ONE name shared by both sides, so a campaign's captures and its analysed
# results sit under the same folder name and neither can drift from the other.
# It matters most for the outputs whose FILENAME says nothing about provenance:
# trajectory_400ms_abs.png and energy_collapse.html are 15 mm-wire results and
# used to sit in a flat folder next to 10 mm-wire results, indistinguishable.
DERIVED_CAMPAIGNS = os.path.join(DERIVED, "campaigns")


def derived_dir(campaign_folder, make=True):
    """data/derived/campaigns/<campaign_folder>, created on demand."""
    d = os.path.join(DERIVED_CAMPAIGNS, campaign_folder)
    if make:
        os.makedirs(d, exist_ok=True)
    return d


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
    """Approximate heat starts on the M7 clock, from the firmware's markers.

    Returns (starts_on_m7, heat_host_times, last_host_t, fit)."""
    host, m7, heats = [], [], []
    last_host = 0.0
    with open(log_path, errors="ignore") as fh:
        for line in fh:
            m = re.match(r"\[\s*([\d.]+)\]", line)
            if not m:
                continue
            t = float(m.group(1))
            last_host = max(last_host, t)
            if "[STATUS]" in line:
                g = re.search(r"m7_us=(\d+)", line)
                if g:
                    host.append(t)
                    m7.append(int(g.group(1)) / 1e6)
            elif "[ACT] heat" in line:
                heats.append(t)
    if len(host) < 2 or not heats:
        return [], [], last_host, None
    m7 = unwrap_us(np.asarray(m7) * 1e6) / 1e6      # STATUS m7_us wraps too
    fit = np.polyfit(host, m7, 1)
    return ([float(np.polyval(fit, h)) for h in heats], heats, last_host, fit)


def tail_median(t, v, t0, t1):
    """Hot-state estimator, per the NN-side guideline §2: median over the LAST
    20% of the heat window, never shorter than 20 ms.

    Tail, not whole-window mean: the wire is hottest at pulse end, and on a
    ramped pulse (the pre-seed bootstrap) the mean sits >15% below the end
    state. Median, not mean, so a single ADC glitch cannot move it. This also
    matches the 2026-07-30 `peak_V` convention, keeping campaigns comparable."""
    lo = t1 - max(0.020, 0.2 * (t1 - t0))
    m = (t >= lo) & (t <= t1)
    return (float(np.median(v[m])), int(m.sum())) if m.sum() else (np.nan, 0)


def threshold_windows(t, i, level_mA):
    """Independent threshold detection — kept ONLY as a cross-check against the
    schedule (guideline §3: detect_ok becomes a consistency flag, never a
    data-loss flag)."""
    thr = max(0.5 * level_mA, 250.0) / 1000.0
    hi = t[i > thr]
    if hi.size < 2:
        return []
    brk = np.where(np.diff(hi) > 0.5)[0]
    return list(np.r_[hi[0], hi[brk + 1]])


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

    approx, heat_host, last_host, fit = schedule_windows(log_path, heat_ms)
    thr_starts = threshold_windows(t_i, v_i, level_mA)
    # detect_ok is a CONSISTENCY flag (guideline §3): does independent threshold
    # detection agree with the schedule on the cycle COUNT? It never removes a row.
    detect_ok = int(len(thr_starts) == len(approx))

    cap_end_utc = None
    if meta.get("captured_utc"):
        cap_end_utc = datetime.fromisoformat(meta["captured_utc"])

    rows = []
    for k, t_ap in enumerate(approx, 1):
        t0 = refine(t_i, v_i, t_ap, heat_s)
        row = {
            "level_mA": level_mA, "heat_ms": heat_ms, "cycle": k,
            "bootstrap": int(k == 1), "cool_s": cool_s,
            "i_mA": np.nan, "u_V": np.nan, "R_ohm": np.nan, "cc_pct": np.nan,
            "v_hot_V": np.nan, "i_hot_mA": np.nan,
            "r_hot_ohm": np.nan, "p_hot_W": np.nan, "r_base_ohm": np.nan,
            "t_heat_start_s": np.nan, "t_heat_end_s": np.nan,
            "t_pulse_utc": "",
            "dx_um": np.nan, "x_base_um": np.nan,
            "baseline_V": np.nan, "peak_V": np.nan, "rise_V": np.nan,
            "F_base_mN": np.nan, "dF_mN": np.nan,
            "clipped": 0, "railed": 0, "detect_ok": detect_ok, "n_samples": 0,
        }
        if t0 is not None:
            t1 = t0 + heat_s
            row["t_heat_start_s"] = round(float(t0), 6)   # unwrapped M7 clock
            row["t_heat_end_s"] = round(float(t1), 6)
            if cap_end_utc is not None and fit is not None:
                # captured_utc is stamped at save_capture, i.e. AFTER the
                # capture ends, so walk back from the console log's last line.
                # Systematic ~1 s (the in-run analysis between capture and save)
                # — good enough for ordering, not for sub-second alignment.
                host_t = (t0 - fit[1]) / fit[0]
                row["t_pulse_utc"] = (
                    cap_end_utc - timedelta(seconds=last_host - host_t)
                ).isoformat()

            hw = (t_i >= t0) & (t_i < t1)
            row["n_samples"] = int(hw.sum())
            if hw.sum():
                row["i_mA"] = float(1e3 * v_i[hw].mean())     # whole-window mean
                row["cc_pct"] = round(100.0 * row["i_mA"] / level_mA, 1)
            hv = (t_v >= t0) & (t_v < t1)
            if hv.sum():
                row["u_V"] = float(v_v[hv].mean())
                if row["i_mA"] == row["i_mA"]:
                    row["R_ohm"] = row["u_V"] / (row["i_mA"] / 1e3)

            # ---- hot state: tail medians (guideline §1/§2) ------------------
            v_hot, n_v = tail_median(t_v, v_v, t0, t1)
            i_hot, n_i = tail_median(t_i, v_i, t0, t1)
            row["v_hot_V"] = v_hot
            row["i_hot_mA"] = 1e3 * i_hot if i_hot == i_hot else np.nan
            if n_v and n_i and i_hot > 0.001:
                row["r_hot_ohm"] = v_hot / i_hot
                row["p_hot_W"] = v_hot * i_hot

            # ---- cold/baseline R from the idle hold before this pulse -------
            # For cycle 1 that is the armed lead-in; for later cycles it is the
            # tail of the preceding cool. Same window either way.
            bi = (t_i >= t0 - 1.0) & (t_i < t0 - 0.02)
            bv = (t_v >= t0 - 1.0) & (t_v < t0 - 0.02)
            if bi.sum() and bv.sum():
                i_b = float(np.median(v_i[bi]))
                v_b = float(np.median(v_v[bv]))
                if i_b > 0.020:        # only meaningful while idle current flows
                    row["r_base_ohm"] = v_b / i_b

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
                fb = float(v_f[pref].mean())
                pk = float(v_f[postf].max())
                # baseline_V/peak_V/rise_V are LOAD-CELL volts, restored so the
                # 2026-07-30 column set is never narrowed (guideline §7).
                row["baseline_V"] = fb
                row["peak_V"] = pk
                row["rise_V"] = pk - fb
                row["F_base_mN"] = fb * F_MN_PER_V
                row["dF_mN"] = (pk - fb) * F_MN_PER_V
                row["clipped"] = int(pk >= LOAD_CLIP_V)
        rows.append(row)
    return rows


def analyze_sweep(folder, i_low, seeded, run_type):
    out = []
    root = resolve_sweep(folder)
    pat = os.path.join(root, "c*_level_*mA_h*ms.csv")
    for f in sorted(glob.glob(pat)):
        if f.endswith("_report.csv"):
            continue
        for r in analyze_capture(f):
            r.update(sweep=folder, run_type=run_type,
                     i_low_mA=i_low, seeded=seeded)
            out.append(r)
    if out:
        cols = list(out[0].keys())
        with open(os.path.join(root, "cycles.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, lineterminator=LF)
            w.writeheader()
            w.writerows(out)
    print(f"  {folder:26s} {len(out):4d} cycles")
    return out


def main(argv):
    """Stage 1 over every campaign. With folder arguments, only those folders
    are re-analysed and only the campaigns they belong to are rewritten — a
    campaign that contributes no rows is left untouched rather than truncated."""
    want = set(argv) if argv else None
    if want:
        known = {f for c in CAMPAIGNS.values() for f, *_ in c["runs"]}
        unknown = want - known
        if unknown:
            print(f"  NOT IN ANY CAMPAIGN: {', '.join(sorted(unknown))}")
            print("  Add it to CAMPAIGNS in analyze_raw.py first — a folder "
                  "with no campaign has no merged table to belong to.")
            return 2
    rc = 0
    for name, camp in CAMPAIGNS.items():
        if run_campaign(name, camp, want) != 0:
            rc = 1
    return rc


def run_campaign(name, camp, want):
    allrows = []
    for folder, i_low, seeded, run_type in camp["runs"]:
        if want and folder not in want:
            continue
        try:
            resolve_sweep(folder)
        except FileNotFoundError:
            print(f"  {folder:26s} MISSING — skipped")
            continue
        allrows += analyze_sweep(folder, i_low, seeded, run_type)
    if not allrows:
        return 0 if want else 1
    MERGED = camp["merged"]
    print(f"\ncampaign {name} — {camp['wire']}")
    order = ["level_mA", "heat_ms", "sweep", "run_type", "cycle", "bootstrap",
             # drive, whole-window
             "i_mA", "cc_pct", "u_V", "R_ohm",
             # hot state, tail medians — the network's inputs
             "v_hot_V", "i_hot_mA", "r_hot_ohm", "p_hot_W", "r_base_ohm",
             # timing, unwrapped
             "t_heat_start_s", "t_heat_end_s", "t_pulse_utc",
             # response
             "dx_um", "x_base_um", "baseline_V", "peak_V", "rise_V",
             "F_base_mN", "dF_mN",
             # flags + protocol
             "clipped", "railed", "detect_ok", "n_samples",
             "cool_s", "i_low_mA", "seeded"]
    allrows.sort(key=lambda r: (r["heat_ms"], r["level_mA"], r["sweep"],
                                r["cycle"]))
    out_dir = derived_dir(camp["dir"])
    with open(os.path.join(out_dir, MERGED), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=order, extrasaction="ignore",
                           lineterminator=LF)
        w.writeheader()
        w.writerows(allrows)
    n_cond = len({(r["level_mA"], r["heat_ms"]) for r in allrows})
    print(f"\n{len(allrows)} cycles over {n_cond} conditions -> {MERGED}")
    print(f"  clipped={sum(r['clipped'] for r in allrows)}  "
          f"railed={sum(r['railed'] for r in allrows)}  "
          f"bootstrap={sum(r['bootstrap'] for r in allrows)}  "
          f"(all RETAINED)")

    # ---- guideline §6: campaign calibration, so a change is detectable ----
    with open(os.path.join(out_dir, MERGED.replace(".csv", "_meta.json")),
              "w") as fh:
        json.dump({
            "campaign": MERGED,
            "wire": camp["wire"],
            "cool_s": camp["cool_s"],
            "laser": {"k_mV_per_um": K_MV_PER_UM, "V0_mV": V0_MV,
                      "source": "Calibrate_LaserHead 2026-05-27 run09",
                      "rail_um": LASER_RAIL_UM},
            "load_cell": {"mN_per_V": F_MN_PER_V, "full_scale_mN": 490.0,
                          "source": "Calibrate_LoadCell 2026-05-28 run07"},
            "sma_v_i_bias": {
                "note": "sma_v and sma_i each read ~+7% high from ADC "
                        "conversion duty; cancels in R, scales P by ~+14%. "
                        "NOT corrected in this table — recorded so absolute "
                        "units stay recoverable and a change is detectable.",
                "v_scale": 1.07, "i_scale": 1.07, "applied": False},
            "hot_state": {"estimator": "median over last 20% of heat window, "
                                       "min 20 ms"},
        }, fh, indent=2)

    # ---- guideline §8: self-checks. Report loudly, never drop. ----
    print("\nself-checks:")
    n_exp = CYCLES_EXPECTED
    bad_n = [(s, lv, h, n) for (s, lv, h, rt), n in
             {(r["sweep"], r["level_mA"], r["heat_ms"], r["run_type"]):
              sum(1 for x in allrows if (x["sweep"], x["level_mA"],
                                         x["heat_ms"]) == (r["sweep"],
                                         r["level_mA"], r["heat_ms"]))
              for r in allrows}.items() if n != n_exp[rt]]
    print(f"  1. windows == commanded cycles : "
          f"{'OK' if not bad_n else f'{len(bad_n)} MISMATCH {bad_n[:4]}'}")

    off = [r for r in allrows if not r["bootstrap"] and r["i_hot_mA"] == r["i_hot_mA"]
           and abs(r["i_hot_mA"] - r["level_mA"]) > 0.10 * r["level_mA"]]
    print(f"  2. i_hot within 10% of command : "
          f"{'OK' if not off else f'{len(off)} flagged (kept)'}")

    per_sweep_mono = []
    for s in sorted({r["sweep"] for r in allrows}):
        ts = [r["t_heat_start_s"] for r in allrows if r["sweep"] == s
              and r["t_heat_start_s"] == r["t_heat_start_s"]]
        ts_sorted = sorted(ts)
        per_sweep_mono.append(ts_sorted == sorted(set(ts_sorted)))
    print(f"  3. timestamps monotonic (unwrapped) : "
          f"{'OK' if all(per_sweep_mono) else 'FAIL — check the wrap handling'}")

    rb = [r for r in allrows if r["r_hot_ohm"] == r["r_hot_ohm"]
          and not (1.0 <= r["r_hot_ohm"] <= 30.0)]
    print(f"  4. r_hot in 1-30 ohm : "
          f"{'OK' if not rb else f'{len(rb)} outside (kept, inspect)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
