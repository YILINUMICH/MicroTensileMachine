"""Merge the four 2026-07-30 heat-time-map sweeps, drop power-corrupted cycles,
and render the current performance envelope.

Outputs (in Experiment_SMAThermalCharacterization/data/):
  heat_time_map_20260730_clean.csv     - every good cycle, with sweep provenance
  heat_time_map_20260730_envelope.csv  - per-(level, heat) aggregates
  heat_time_map_20260730_envelope.png  - envelope chart

Exclusion rules (same as check_sweeps.py):
  DEAD     measured current < 50% of commanded  -> supply OCP-starved
  OVERI    measured current > 110% of commanded -> supply sag/recovery transient
  CLIP     laser peak railed at 5.000 V         -> displacement out of range
  BASELOST baseline x_base > -3000 um           -> baseline never recovered
  BOOT     bootstrap (ramp-limited) first cycle -> not representative
"""
import csv, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
# Calibrate_LoadCell/calibration.json: 10.2009 mV/mN -> 98.03 mN/V.
# baseline_V/peak_V/rise_V in summary.csv are LOAD CELL volts (SRC_LOAD);
# `clipped` is the load cell hitting 5 V full scale (~490 mN), not the laser.
F_MN_PER_V = 1e3 / 10.200865238052671
SWEEPS = ["sweep_20260730_132601", "sweep_20260730_135311",
          "sweep_20260730_145051", "sweep_20260730_150846",
          "sweep_20260730_162137"]   # fill-in run (heat_time_map_fill profile)
LEVELS = [150, 250, 350, 450, 550, 650, 750, 850, 950]
HEATS = [100, 200, 300, 400]

def classify(r):
    lvl = float(r["level_mA"]); i = float(r["i_mA"])
    boot = r["bootstrap"] == "1"
    if i < 0.5 * lvl:
        return "DEAD"
    if i > 1.10 * lvl:
        return "OVERI"
    if r["clipped"] == "1":
        return "CLIP"
    if float(r["x_base_um"]) > -3000:
        return "BASELOST"
    if boot:
        return "BOOT"
    return "OK"

good, dropped = [], {}
for s in SWEEPS:
    with open(os.path.join(BASE, s, "summary.csv"), newline="") as f:
        for r in csv.DictReader(f):
            flag = classify(r)
            if flag == "OK":
                r["sweep"] = s
                good.append(r)
            else:
                dropped[flag] = dropped.get(flag, 0) + 1

clean_path = os.path.join(BASE, "heat_time_map_20260730_clean.csv")
cols = ["level_mA", "heat_ms", "sweep", "cycle", "i_mA", "dx_um",
        "x_base_um", "baseline_V", "peak_V", "rise_V"]
with open(clean_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(cols + ["force_mN"])
    for r in sorted(good, key=lambda r: (int(r["heat_ms"]), float(r["level_mA"]), int(r["cycle"]))):
        w.writerow([r[c] for c in cols] +
                   [round(float(r["rise_V"]) * F_MN_PER_V, 2)])

env_path = os.path.join(BASE, "heat_time_map_20260730_envelope.csv")
env = {}   # (heat, level) -> dict
with open(env_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["heat_ms", "level_mA", "n_cycles", "i_mA_mean",
                "dx_um_median", "dx_um_mean", "dx_um_std", "dx_um_min", "dx_um_max",
                "force_mN_median", "force_mN_std"])
    for h in HEATS:
        for lv in LEVELS:
            sel = [r for r in good if int(r["heat_ms"]) == h and float(r["level_mA"]) == lv]
            if not sel:
                w.writerow([h, lv, 0, "", "", "", "", "", "", "", ""])
                continue
            dx = np.array([float(r["dx_um"]) for r in sel])
            i = np.array([float(r["i_mA"]) for r in sel])
            fm = np.array([float(r["rise_V"]) * F_MN_PER_V for r in sel])
            env[(h, lv)] = dict(n=len(sel), dx=dx, i=i, f=fm)
            w.writerow([h, lv, len(sel), round(i.mean(), 1),
                        round(np.median(dx), 1), round(dx.mean(), 1),
                        round(dx.std(ddof=1), 1) if len(dx) > 1 else "",
                        round(dx.min(), 1), round(dx.max(), 1),
                        round(np.median(fm), 2),
                        round(fm.std(ddof=1), 2) if len(fm) > 1 else ""])

print(f"kept {len(good)} cycles; dropped: " +
      ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
print("wrote", clean_path)
print("wrote", env_path)

# ---------------------------------------------------------------- chart
SURFACE = "#fcfcfb"; GRID = "#e1e0d9"; AXIS = "#c3c2b7"
INK = "#0b0b0b"; INK2 = "#52514e"; MUTED = "#898781"
# ordinal single-hue blue ramp (reference palette steps 250/400/550/700, light mode)
SERIES = {100: "#86b6ef", 200: "#3987e5", 300: "#1c5cab", 400: "#0d366b"}

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans"],
    "axes.edgecolor": AXIS, "axes.linewidth": 1.0,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "text.color": INK, "axes.labelcolor": INK2,
})

fig, ax = plt.subplots(figsize=(9.2, 6.2), dpi=150)
fig.patch.set_facecolor(SURFACE)
ax.set_facecolor(SURFACE)

for h in HEATS:
    c = SERIES[h]
    med = []
    for lv in LEVELS:
        cell = env.get((h, lv))
        med.append(np.median(cell["dx"]) if cell else np.nan)
        if cell:
            dx = cell["dx"]
            ax.scatter([lv] * len(dx), dx,
                       s=16, color=c, alpha=0.45, edgecolors=SURFACE,
                       linewidths=0.8, zorder=3)
    med = np.array(med, dtype=float)
    ax.plot(LEVELS, med, color=c, linewidth=2,
            marker="o", markersize=7, markeredgecolor=SURFACE,
            markeredgewidth=1.2, zorder=4, label=f"{h} ms pulse")
    # direct label at the last valid point
    last = max(i for i in range(len(LEVELS)) if not np.isnan(med[i]))
    ax.annotate(f"{h} ms", (LEVELS[last], med[last]),
                xytext=(10, 0), textcoords="offset points",
                va="center", fontsize=9, color=INK2)

# Laser output is 0-5 V over ~10 mm (k = -0.498 mV/um): it rails at 0 V
# = +5.03 mm reading. From the ~-3.6 mm rest baseline that is ~8.6 mm of
# stroke; railed cycles read dx = 5030 - x_base exactly.
ax.axhline(8600, color=AXIS, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
ax.text(130, 8600, "≈ laser 0 V rail (stroke from typical rest)", fontsize=8.5,
        color=MUTED, va="bottom", ha="left")

# missing corner annotation
ax.annotate("no valid data: 750–950 mA × 400 ms\n(current-sense fault / load-cell clip)",
            xy=(760, 5800), fontsize=8.5, color=MUTED, ha="center", va="center")

ax.set_ylim(-200, 9300)
ax.set_xticks(LEVELS)
ax.set_xlim(115, 1050)
ax.grid(axis="y", color=GRID, linewidth=0.8)
ax.set_axisbelow(True)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)

ax.set_xlabel("Commanded current (mA)")
ax.set_ylabel("Contraction stroke Δx (µm)")
ax.set_title("SMA actuation envelope — stroke vs current and pulse length",
             fontsize=13, loc="left", pad=14)
sub = (f"2026-07-30, {len(SWEEPS)} sweeps merged · {len(good)} clean cycles "
       f"(OCP/over-current/clipped/baseline-lost/bootstrap cycles removed) · "
       f"median line, individual cycles as dots")
ax.text(0, 1.012, sub, transform=ax.transAxes, fontsize=8.5, color=MUTED)

leg = ax.legend(loc="upper left", bbox_to_anchor=(0.02, 0.90),
                frameon=False, fontsize=9,
                title="Heat pulse", title_fontsize=9)
leg.get_title().set_color(INK2)
for t in leg.get_texts():
    t.set_color(INK2)

png_path = os.path.join(BASE, "heat_time_map_20260730_envelope.png")
fig.tight_layout()
fig.savefig(png_path, facecolor=SURFACE, bbox_inches="tight")
print("wrote", png_path)

# ------------------------------------------------------- force chart
fig2, ax2 = plt.subplots(figsize=(9.2, 6.2), dpi=150)
fig2.patch.set_facecolor(SURFACE)
ax2.set_facecolor(SURFACE)

for h in HEATS:
    c = SERIES[h]
    med = []
    for lv in LEVELS:
        cell = env.get((h, lv))
        med.append(np.median(cell["f"]) if cell else np.nan)
        if cell:
            fm = cell["f"]
            ax2.scatter([lv] * len(fm), fm,
                        s=16, color=c, alpha=0.45, edgecolors=SURFACE,
                        linewidths=0.8, zorder=3)
    med = np.array(med, dtype=float)
    ax2.plot(LEVELS, med, color=c, linewidth=2,
             marker="o", markersize=7, markeredgecolor=SURFACE,
             markeredgewidth=1.2, zorder=4, label=f"{h} ms pulse")
    last = max(i for i in range(len(LEVELS)) if not np.isnan(med[i]))
    ax2.annotate(f"{h} ms", (LEVELS[last], med[last]),
                 xytext=(10, 0), textcoords="offset points",
                 va="center", fontsize=9, color=INK2)

ax2.axhline(490, color=AXIS, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
ax2.text(1040, 490, "load-cell full scale (5 V ≈ 490 mN)", fontsize=8.5,
         color=MUTED, va="bottom", ha="right")
ax2.annotate("no valid data: 750–950 mA × 400 ms\n(current-sense fault / load-cell clip)",
             xy=(550, 380), fontsize=8.5, color=MUTED, ha="center", va="center")

ax2.set_ylim(-10, 520)
ax2.set_xticks(LEVELS)
ax2.set_xlim(115, 1050)
ax2.grid(axis="y", color=GRID, linewidth=0.8)
ax2.set_axisbelow(True)
for side in ("top", "right"):
    ax2.spines[side].set_visible(False)

ax2.set_xlabel("Commanded current (mA)")
ax2.set_ylabel("Force rise (mN)")
ax2.set_title("SMA force envelope — load-cell force rise vs current and pulse length",
              fontsize=13, loc="left", pad=14)
sub2 = (f"2026-07-30, same {len(good)} clean cycles · 98.0 mN/V "
        f"(Calibrate_LoadCell 2026-05-28, ±5 mN hysteresis) · "
        f"median line, individual cycles as dots")
ax2.text(0, 1.012, sub2, transform=ax2.transAxes, fontsize=8.5, color=MUTED)

leg2 = ax2.legend(loc="upper left", bbox_to_anchor=(0.02, 0.90),
                  frameon=False, fontsize=9,
                  title="Heat pulse", title_fontsize=9)
leg2.get_title().set_color(INK2)
for t in leg2.get_texts():
    t.set_color(INK2)

png2_path = os.path.join(BASE, "heat_time_map_20260730_force_envelope.png")
fig2.tight_layout()
fig2.savefig(png2_path, facecolor=SURFACE, bbox_inches="tight")
print("wrote", png2_path)
