"""fig1-style whole-run timeline for the combined 150-950 mA sweep.

Rows: SMA current / displacement / force / resistance; one column per level
(10, including the Jul-29 AND Jul-30 650 mA runs). Same analysis path as
summary_combined.csv: per-capture clock offset -> align_m4; force/displacement
via the calibration fits.
"""
import re
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MOD = Path(__file__).resolve().parents[2]     # Experiment_SMAThermalCharacterization
sys.path.insert(0, str(MOD))
from lib_h7_session import (SRC_CC_R, SRC_CC_U, SRC_LASER, SRC_LOAD,  # noqa: E402
                            SRC_SMA_I, align_m4, heat_windows,
                            m4_offset_from_capture)
from operator_current_sweep import disp_um                            # noqa: E402
sys.path.insert(0, str(Path(__file__).parent))
from make_summary_and_curve import load_capture, MV_PER_MN            # noqa: E402

D = MOD / "data" / "sweep_full_150-950mA"
force_mN = lambda v: (v * 1e3 - (-34.185523054186675)) / MV_PER_MN

C_CUR, C_DISP, C_FORCE = "#2a78d6", "#008300", "#eb6834"
C_REST, C_CMD, C_RATE = "#0b0b0b", "#52514e", "#e34948"
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
DATE_LBL = {"20260729": "Jul 29", "20260730": "Jul 30"}

files = []
for p in sorted(D.glob("level_*mA_*.csv")):
    m = re.match(r"level_(\d+)mA_(\d+)$", p.stem)
    files.append((int(m.group(1)), m.group(2), p))
files.sort()

plt.rcParams.update({
    "font.size": 9, "text.color": INK, "axes.edgecolor": GRID,
    "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.5,
    "axes.axisbelow": True, "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE, "axes.spines.top": False,
    "axes.spines.right": False, "savefig.dpi": 110,
})
fig, axes = plt.subplots(4, len(files), figsize=(34, 13),
                         sharex="col", sharey="row")

def series(cap, src, t0):
    r = sorted((s.hw_us, s.value) for s in cap.samples if s.src == src)
    return (np.array([x[0] for x in r]) * 1e-6 - t0,
            np.array([x[1] for x in r]))

for col, (lvl, run, path) in enumerate(files):
    cap = load_capture(path)
    cap = align_m4(cap, m4_offset_from_capture(cap))
    t0 = min(s.hw_us for s in cap.samples) * 1e-6
    wins = [(a - t0, b - t0) for a, b in heat_windows(cap)]

    ti, vi = series(cap, SRC_SMA_I, t0)
    tx, vx = series(cap, SRC_LASER, t0)
    tf, vf = series(cap, SRC_LOAD, t0)
    tu, vu = series(cap, SRC_CC_U, t0)
    tr, vr = series(cap, SRC_CC_R, t0)

    a0, a1, a2, a3 = axes[:, col]
    for ax in (a0, a1, a2, a3):
        for w in wins:
            ax.axvspan(*w, color=C_CUR, alpha=0.10, lw=0)

    a0.plot(ti, vi * 1e3, color=C_CUR, lw=0.5)
    a0.axhline(lvl, color=C_CMD, ls="--", lw=1.2)
    a1.plot(tx, disp_um(vx), color=C_DISP, lw=0.7)
    a2.plot(tf, force_mN(vf), color=C_FORCE, lw=0.8)
    a2.axhline(490, color=C_RATE, ls=":", lw=1.2)
    # true u/I on the current channel's time grid
    if len(tu) and len(ti):
        u_on_i = np.interp(ti, tu, vu)
        with np.errstate(divide="ignore", invalid="ignore"):
            r_true = np.where(vi > 0.02, u_on_i / vi, np.nan)
        a3.plot(ti, r_true, color=C_CUR, lw=0.35, alpha=0.45,
                label="true $u/I$")
    a3.plot(tr, vr, color=C_REST, lw=0.9, label="$R_{est}$")

    a0.set_title(f"{lvl} mA · {DATE_LBL[run]}", fontsize=10,
                 color=INK, fontweight="bold", loc="left")
    a3.set_xlabel("time [s]")
    if col == 0:
        a0.set_ylabel("SMA current [mA]")
        a1.set_ylabel("displacement [µm]")
        a2.set_ylabel("force [mN]")
        a3.set_ylabel("resistance [Ω]")
        a3.legend(frameon=False, fontsize=8, loc="lower left")

axes[0, 0].set_ylim(0, 1050)
axes[1, 0].margins(y=0.08)
axes[2, 0].set_ylim(100, 520)
axes[3, 0].set_ylim(0, 9)

fig.suptitle("Current sweep 150–950 mA — whole run per level   "
             "(shaded = commanded heat phase, dashed = commanded current, "
             "dotted red = load-cell rating)",
             x=0.008, ha="left", fontsize=13, fontweight="bold", color=INK)
fig.tight_layout(rect=(0, 0, 1, 0.965))
out = D / "fig_timeline_150-950mA.png"
fig.savefig(out)
print("wrote", out)
