"""Combine the 2026-07-29 (150-650 mA) and 2026-07-30 (650-950 mA) sweeps into
one uniformly-analysed dataset + actuation-curve figure.

Both runs are re-analysed from RAW through the same path the live sweep now
uses (offset -> align_m4 -> analyse_level), so the 0729 numbers are produced by
identical code to the 0730 ones rather than trusting either old summary.csv.
"""
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MOD = Path(__file__).resolve().parents[2]     # Experiment_SMAThermalCharacterization
sys.path.insert(0, str(MOD))
from lib_h7_session import (Capture, Sample, align_m4,          # noqa: E402
                            m4_offset_from_capture)
from operator_current_sweep import analyse_level                # noqa: E402

D = MOD / "data" / "sweep_full_150-950mA"
HEAT_MS = 100.0
MV_PER_MN = 10.200865238052671          # Calibrate_LoadCell/calibration.json

# Categorical slots 1+2 (validated all-pairs, light mode).
RUN_COLOR = {"20260729": "#2a78d6", "20260730": "#eb6834"}
RUN_LABEL = {"20260729": "run 1 · Jul 29 (150–650 mA)",
             "20260730": "run 2 · Jul 30 (650–950 mA)"}
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"


def load_capture(csv_path: Path) -> Capture:
    cap = Capture()
    with open(csv_path) as fh:
        next(fh)
        for line in fh:
            f = line.rstrip("\n").split(",")
            cap.samples.append(Sample(int(f[0]), int(f[1]), float(f[2]),
                                      int(f[3]), int(f[4])))
    log = csv_path.with_suffix("").with_suffix("")  # strip .csv only
    log = csv_path.parent / (csv_path.stem + ".console.log")
    with open(log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if len(line) > 11:
                cap.console.append((0.0, line[11:].rstrip("\n")))
    return cap


rows = []
for csv_path in sorted(D.glob("level_*mA_*.csv")):
    m = re.match(r"level_(\d+)mA_(\d+)$", csv_path.stem)
    level, run = int(m.group(1)), m.group(2)
    cap = load_capture(csv_path)
    off = m4_offset_from_capture(cap)
    if off == 0.0:
        print(f"  !! {csv_path.name}: no STATUS line — skipping")
        continue
    per = analyse_level(align_m4(cap, off), HEAT_MS)
    for r in per:
        r.update(run=run, level_mA=level, bootstrap=(r["cycle"] == 1),
                 dF_mN=r["rise"] * 1e3 / MV_PER_MN)
        rows.append(r)
    v = [r for r in per if r["cycle"] != 1]
    dx = [r["dx_um"] for r in v]
    print(f"  {run} {level:4d} mA: offset {off:+.3f} s, {len(per)} pulses, "
          f"I {sum(r['i_mA'] for r in v)/len(v):5.0f} mA, "
          f"dx {sum(dx)/len(dx):+7.1f} um")

# ---- combined summary --------------------------------------------------------
with open(D / "summary_combined.csv", "w", newline="") as fh:
    fh.write("run,level_mA,cycle,bootstrap,i_mA,dx_um,x_base_um,"
             "baseline_V,peak_V,rise_V,dF_mN,clipped\n")
    for r in rows:
        fh.write(f"{r['run']},{r['level_mA']},{r['cycle']},{int(r['bootstrap'])},"
                 f"{r['i_mA']:.2f},{r['dx_um']:.2f},{r['x_base_um']:.2f},"
                 f"{r['baseline']:.5f},{r['peak']:.5f},{r['rise']:.5f},"
                 f"{r['dF_mN']:.3f},{int(r['clipped'])}\n")

# ---- figure ------------------------------------------------------------------
verd = [r for r in rows if not r["bootstrap"]]
plt.rcParams.update({
    "font.size": 10, "text.color": INK, "axes.edgecolor": GRID,
    "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True, "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE, "axes.spines.top": False,
    "axes.spines.right": False, "savefig.dpi": 160,
})
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.6, 8.4), sharex=True)

for run in ("20260729", "20260730"):
    c = RUN_COLOR[run]
    rv = [r for r in verd if r["run"] == run]
    # per-pulse scatter
    ax1.scatter([r["i_mA"] for r in rv], [r["dx_um"] for r in rv],
                s=22, color=c, alpha=0.45, linewidths=0, zorder=2)
    ax2.scatter([r["i_mA"] for r in rv], [r["dF_mN"] for r in rv],
                s=22, color=c, alpha=0.45, linewidths=0, zorder=2)
    # level means
    levels = sorted({r["level_mA"] for r in rv})
    mi, mdx, mdf = [], [], []
    for lv in levels:
        g = [r for r in rv if r["level_mA"] == lv]
        mi.append(sum(r["i_mA"] for r in g) / len(g))
        mdx.append(sum(r["dx_um"] for r in g) / len(g))
        mdf.append(sum(r["dF_mN"] for r in g) / len(g))
    ax1.plot(mi, mdx, "-o", color=c, lw=2, ms=6, zorder=3,
             label=RUN_LABEL[run])
    ax2.plot(mi, mdf, "-o", color=c, lw=2, ms=6, zorder=3)

ax1.set_ylabel("displacement excursion Δx (µm)")
ax2.set_ylabel("force rise ΔF (mN)")
ax2.set_xlabel("achieved current (mA)")
ax1.set_title("SMA actuation curve — 100 ms pulse, 12 s cool, signed per-pulse response",
              color=INK, fontsize=11, loc="left", pad=12)
ax1.legend(frameon=False, loc="upper left", fontsize=9)
for ax in (ax1, ax2):
    ax.margins(x=0.04)
fig.tight_layout()
fig.savefig(D / "fig_actuation_150-950mA.png")
print(f"\nwrote {D / 'summary_combined.csv'}")
print(f"wrote {D / 'fig_actuation_150-950mA.png'}")
