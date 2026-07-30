#!/usr/bin/env python3
"""operator_sweep_report.py — standard analysis + health report for any sweep.

Run it on a sweep folder the moment a run ends (or mid-run on the partial
data — every capture is saved before it is analysed):

    python operator_sweep_report.py data/sweep_20260730_031337
    python operator_sweep_report.py data/sweep_full_150-950mA --timeline

It re-analyses EVERY capture from raw through the same clock-aligned path the
live sweep uses, so its numbers are the reference — never trust a stale
summary.csv over this. Outputs land in the sweep folder:

    report.txt            per-condition health verdicts + the checks that fired
    summary_report.csv    per-pulse table (the RNN-ready flat file)
    fig_envelope.png      dx vs achieved current, one series per heat time
    fig_timeline.png      (--timeline) fig1-style whole-run strips

HEALTH CHECKS — every failure mode this rig has actually produced:
    laser-rail      laser V pinned at its out-of-range output (~4.97 V) or at
                    0: the target LEFT THE MEASURING WINDOW and every dx from
                    that pulse is rail-to-reentry garbage, not motion
                    (2026-07-30: 33/82 pulses lost this way).
    cc-track        achieved current vs commanded. >±15% = the R_est
                    single-sample bootstrap latched wrong (or the clip contact
                    is intermittent — check for a multimodal current
                    distribution before blaming firmware).
    sense           measurement_sane(): cool-phase samples implying R < 2 ohm
                    are physically impossible (2026-07-28 corrupted sweep).
    load-clip       force samples at the 5 V rail.
    base-jump       laser/force baseline moved between the first and last
                    pulse of a condition — coil migration, mount slip, or a
                    cool time too short for the pulse energy.
    pulses          fewer pulses segmented than commanded.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_h7_session import (SAT_GUARD_V, SRC_CC_R, SRC_CC_U, SRC_LASER,  # noqa: E402
                            SRC_LOAD, SRC_SMA_I, Capture, Sample, align_m4,
                            heat_windows, m4_offset_from_capture,
                            measurement_sane)
from operator_current_sweep import analyse_level, disp_um  # noqa: E402

# Calibrate_LoadCell/calibration.json (2026-05-28).
MV_PER_MN, F_V0_MV = 10.200865238052671, -34.185523054186675
force_mN = lambda v: (v * 1e3 - F_V0_MV) / MV_PER_MN

# The IL-030 parks at ~4.97 V when the target is outside its window; the other
# rail is ~0 V. In µm (through the laser fit) those are ~-4954 and ~+5030.
# Thresholds sit just inside the rails (4.95 V / 0.05 V): a real target near
# the window edge (-4868 µm was measured, honestly) must NOT be flagged.
LASER_RAIL_LO_UM, LASER_RAIL_HI_UM = -4914.0, +4929.0
CC_TRACK_TOL = 0.15
BASE_JUMP_UM, BASE_JUMP_MN = 150.0, 15.0

# File shapes this understands: level_650mA.csv, level_650mA_h200ms.csv,
# level_650mA_20260729.csv, c07_level_650mA_h200ms.csv
PAT = re.compile(r"^(?:c(\d+)_)?level_(\d+)mA(?:_h(\d+)ms)?(?:_(\d{8}))?$")

INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
HEAT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
               "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def load_capture(csv_path: Path) -> Capture:
    cap = Capture()
    with open(csv_path) as fh:
        next(fh)
        for line in fh:
            f = line.rstrip("\n").split(",")
            cap.samples.append(Sample(int(f[0]), int(f[1]), float(f[2]),
                                      int(f[3]), int(f[4])))
    log = csv_path.parent / (csv_path.stem + ".console.log")
    if log.exists():
        with open(log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if len(line) > 11:
                    cap.console.append((0.0, line[11:].rstrip("\n")))
    return cap


def analyse_condition(csv_path: Path, meta: dict) -> dict:
    """One condition -> per-pulse rows + health flags."""
    lvl = float(meta.get("level_mA", 0))
    heat_ms = float(meta.get("heat_ms", 100))
    n_expect = int(meta.get("cycles", 0)) + 1 if meta.get("cycles") else None

    cap = load_capture(csv_path)
    off = float(meta.get("m4_clock_offset_s") or 0.0) or m4_offset_from_capture(cap)
    if off:
        cap = align_m4(cap, off)
    per = analyse_level(cap, heat_ms)
    ok_sense, why_sense = measurement_sane(cap, heat_windows(cap))

    flags = []
    if not off:
        flags.append("NO-CLOCK-OFFSET: channels misaligned ~2.2 s")
    if not ok_sense:
        flags.append(f"sense: {why_sense}")
    if n_expect and len(per) < n_expect:
        flags.append(f"pulses: {len(per)} segmented of {n_expect} commanded")

    for r in per:
        r["railed"] = not (LASER_RAIL_LO_UM < r["x_base_um"] < LASER_RAIL_HI_UM)
    verd = [r for r in per if r["cycle"] != 1]
    valid = [r for r in verd if not r["railed"]]
    n_rail = sum(r["railed"] for r in per)
    if n_rail:
        flags.append(f"laser-rail: {n_rail}/{len(per)} pulses blind "
                     f"(target out of the measuring window)")
    if any(r["clipped"] for r in per):
        flags.append("load-clip: force at the 5 V rail")
    if verd:
        i_ach = sum(r["i_mA"] for r in verd) / len(verd)
        if lvl and abs(i_ach - lvl) > CC_TRACK_TOL * lvl:
            flags.append(f"cc-track: achieved {i_ach:.0f} mA of {lvl:.0f} "
                         f"({100 * i_ach / lvl:.0f}%)")
    else:
        i_ach = float("nan")
    if len(per) >= 2:
        dxb = per[-1]["x_base_um"] - per[0]["x_base_um"]
        dfb = force_mN(per[-1]["baseline"]) - force_mN(per[0]["baseline"])
        if not per[-1]["railed"] and not per[0]["railed"] and abs(dxb) > BASE_JUMP_UM:
            flags.append(f"base-jump: laser baseline moved {dxb:+.0f} µm")
        if abs(dfb) > BASE_JUMP_MN:
            flags.append(f"base-jump: force baseline moved {dfb:+.1f} mN")

    dxs = [r["dx_um"] for r in valid if r["dx_um"] == r["dx_um"]]
    return {
        "level_mA": lvl, "heat_ms": heat_ms, "cool_s": meta.get("cool_s"),
        "seq": meta.get("seq"), "offset_s": off, "per": per,
        "i_ach": i_ach, "dx_mean": sum(dxs) / len(dxs) if dxs else float("nan"),
        "n_valid": len(valid), "n_pulses": len(per), "flags": flags,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", help="sweep folder (data/sweep_*)")
    p.add_argument("--timeline", action="store_true",
                   help="also render fig1-style whole-run strips (slow)")
    a = p.parse_args()
    d = Path(a.folder)
    if not d.is_dir():
        print(f"ERROR: {d} is not a folder", file=sys.stderr)
        return 2

    import json
    conds = []
    for csv_path in sorted(d.glob("*.csv")):
        m = PAT.match(csv_path.stem)
        if not m:
            continue
        meta_path = csv_path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        meta.setdefault("level_mA", float(m.group(2)))
        if m.group(3):
            meta.setdefault("heat_ms", float(m.group(3)))
        if m.group(1):
            meta.setdefault("seq", int(m.group(1)))
        meta["_run_tag"] = m.group(4) or ""
        print(f"  {csv_path.name} ...", flush=True)
        c = analyse_condition(csv_path, meta)
        c["file"], c["run_tag"] = csv_path.stem, meta["_run_tag"]
        conds.append(c)
    if not conds:
        print(f"ERROR: no level_*.csv captures in {d}", file=sys.stderr)
        return 2

    # ---- per-pulse flat file (the RNN-ready table) --------------------------
    with open(d / "summary_report.csv", "w", newline="") as fh:
        fh.write("file,level_mA,heat_ms,cycle,bootstrap,railed,i_mA,dx_um,"
                 "x_base_um,F_base_mN,dF_mN,clipped\n")
        for c in conds:
            for r in c["per"]:
                fh.write(f"{c['file']},{c['level_mA']:.0f},{c['heat_ms']:.0f},"
                         f"{r['cycle']},{int(r['cycle'] == 1)},"
                         f"{int(r['railed'])},{r['i_mA']:.2f},{r['dx_um']:.2f},"
                         f"{r['x_base_um']:.2f},{force_mN(r['baseline']):.3f},"
                         f"{(r['rise'] * 1e3 / MV_PER_MN):.3f},"
                         f"{int(r['clipped'])}\n")

    # ---- health report ------------------------------------------------------
    lines = [f"SWEEP REPORT — {d.name}", "=" * 64]
    n_clean = 0
    for c in conds:
        head = (f"{c['level_mA']:4.0f} mA / {c['heat_ms']:3.0f} ms"
                + (f" [{c['run_tag']}]" if c["run_tag"] else "")
                + (f" (seq {c['seq']})" if c.get("seq") is not None else ""))
        if c["flags"]:
            lines.append(f"{head}:  I {c['i_ach']:5.0f} mA, "
                         f"dx {c['dx_mean']:+8.1f} µm "
                         f"({c['n_valid']}/{c['n_pulses']} valid)")
            lines += [f"        !! {f}" for f in c["flags"]]
        else:
            n_clean += 1
            lines.append(f"{head}:  I {c['i_ach']:5.0f} mA, "
                         f"dx {c['dx_mean']:+8.1f} µm   OK")
    lines.append("=" * 64)
    lines.append(f"{n_clean}/{len(conds)} conditions clean; outputs: "
                 f"summary_report.csv, fig_envelope.png"
                 + (", fig_timeline.png" if a.timeline else ""))
    report = "\n".join(lines)
    (d / "report.txt").write_text(report, encoding="utf-8")
    print("\n" + report)

    # ---- envelope figure ----------------------------------------------------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.size": 10, "text.color": INK, "axes.edgecolor": GRID,
        "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.axisbelow": True, "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE, "axes.spines.top": False,
        "axes.spines.right": False, "savefig.dpi": 160})
    fig, ax = plt.subplots(figsize=(7.6, 5.2))
    heats = sorted({c["heat_ms"] for c in conds})
    hcol = {h: HEAT_COLORS[i % len(HEAT_COLORS)] for i, h in enumerate(heats)}
    for h in heats:
        cc = [c for c in conds if c["heat_ms"] == h]
        pts = []
        for c in cc:
            for r in c["per"]:
                if r["cycle"] == 1:
                    continue
                if r["railed"]:
                    ax.scatter(r["i_mA"], r["dx_um"], s=30, color=hcol[h],
                               marker="x", linewidths=1.4, zorder=2)
                else:
                    ax.scatter(r["i_mA"], r["dx_um"], s=24, color=hcol[h],
                               alpha=0.45, linewidths=0, zorder=2)
            if c["n_valid"] >= 2 and c["dx_mean"] == c["dx_mean"]:
                pts.append((c["i_ach"], c["dx_mean"]))
        pts.sort()
        if pts:
            ax.plot([x for x, _ in pts], [y for _, y in pts], "-o",
                    color=hcol[h], lw=2, ms=6, zorder=3,
                    label=f"{h:.0f} ms pulse")
    ax.set_xlabel("achieved current (mA)")
    ax.set_ylabel("displacement excursion Δx (µm)")
    ax.set_title(f"Performance envelope — {d.name}   (✕ = laser railed, "
                 f"excluded from means)", loc="left", color=INK, fontsize=10.5,
                 pad=12)
    if len(heats) > 1:
        ax.legend(frameon=False, loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(d / "fig_envelope.png")

    # ---- optional timeline strips ------------------------------------------
    if a.timeline:
        n = len(conds)
        fig2, axes = plt.subplots(4, n, figsize=(max(6, 3.2 * n), 12),
                                  sharex="col", sharey="row", squeeze=False)
        for col, c in enumerate(conds):
            cap = load_capture(d / f"{c['file']}.csv")
            if c["offset_s"]:
                cap = align_m4(cap, c["offset_s"])
            t0 = min(s.hw_us for s in cap.samples) * 1e-6
            wins = [(x - t0, y - t0) for x, y in heat_windows(cap)]
            def ser(src):
                r = sorted((s.hw_us, s.value) for s in cap.samples if s.src == src)
                return ([x * 1e-6 - t0 for x, _ in r], [v for _, v in r])
            a0, a1, a2, a3 = axes[:, col]
            for axx in (a0, a1, a2, a3):
                for w in wins:
                    axx.axvspan(*w, color="#2a78d6", alpha=0.10, lw=0)
            t, v = ser(SRC_SMA_I)
            a0.plot(t, [x * 1e3 for x in v], color="#2a78d6", lw=0.5)
            a0.axhline(c["level_mA"], color=INK2, ls="--", lw=1.1)
            t, v = ser(SRC_LASER)
            a1.plot(t, [disp_um(x) for x in v], color="#008300", lw=0.7)
            t, v = ser(SRC_LOAD)
            a2.plot(t, [force_mN(x) for x in v], color="#eb6834", lw=0.8)
            t, v = ser(SRC_CC_R)
            a3.plot(t, v, color=INK, lw=0.8)
            a0.set_title(f"{c['level_mA']:.0f} mA/{c['heat_ms']:.0f} ms",
                         fontsize=9, color=INK, loc="left")
            a3.set_xlabel("time [s]")
            if col == 0:
                for axx, lbl in ((a0, "I [mA]"), (a1, "x [µm]"),
                                 (a2, "F [mN]"), (a3, "R_est [Ω]")):
                    axx.set_ylabel(lbl)
        fig2.tight_layout()
        fig2.savefig(d / "fig_timeline.png")

    return 0


if __name__ == "__main__":
    sys.exit(main())
