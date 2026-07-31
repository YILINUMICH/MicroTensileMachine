"""Merge every 2026-07-31 sweep into one 30 s-cool RNN training table.

SUPERSEDES heat_time_map_20260730_clean.csv rather than extending it: that
campaign ran 15 s cool (25 s at two cells), and cool time measurably changes the
response, so the two are not protocol-comparable. Everything here is 30 s cool.

  heat_time_map_20260731_all.csv       - EVERY cycle from every run, flagged
  heat_time_map_20260731_envelope.csv  - per-(level, heat) aggregates
  heat_time_map_20260731_envelope.png  - stroke envelope
  heat_time_map_20260731_force_envelope.png - force envelope

NOTHING IS DROPPED. This table is RNN training data, so deciding which pulses
"count" belongs to the training pipeline, not to this script — a network that
only ever sees clean pulses cannot learn that clipping, sub-threshold drive, or
a railed actuator are real states of the machine. Every cycle is written with
the flags needed to filter it downstream:

  bootstrap   cycle 1. Since the pre-run R seed (firmware 2026-07-31) this is a
              REAL full-energy pulse, not a ramp — but it starts from a fully
              relaxed wire and takes a one-time set (x_base moves ~+370 um at
              850x400, then holds to ~100 um). Valid data, DIFFERENT initial
              condition. Excluded from the envelope aggregates only.
  clipped     load cell peak at the 5.000 V rail (~490 mN). dF is a lower bound.
  railed      laser outside its measuring window — dx is a LOWER BOUND, the
              wire moved at least that far. Computed from the endpoint
              (x_base+dx at the 0 V rail), not taken from the report column,
              which under-reports; see hit_laser_rail().
  cc_pct      achieved/commanded current. Judge drive on THIS, not on the
              command — the 2026-07-30 750 mA level overshot to 792 mA and its
              points sit on the curve when plotted against achieved current.
  detect_ok   0 where pulse detection is not trustworthy — see below.
  i_low_mA    cool-phase protocol (100 = CC engaged, 0 = passive LDO floor).
  seeded      1 if the firmware had the pre-run R seed (all runs from 13:44 on).

DETECTION CAVEAT — the 100 ms low-current cells (detect_ok = 0)
  The cool phase carries a 0.5 V idle bias -> ~107 mA whose noise reaches
  p95 ~155 mA, p99.9 ~200 mA. A 150 or 250 mA heat pulse sits INSIDE that band,
  and at 100 ms there are too few samples to separate them. With 30 s cools
  there are ~2.5x more noise excursions than the 12-15 s cools of 2026-07-30,
  so threshold-based detection returns phantom cycles: 36 rows at 150x100
  against 6 commanded, 15 at 250x100, 1-2 at 350/450/550x100. The same 150 mA
  level detects cleanly at 400 ms (151 mA) because the window is 4x longer.

  The PULSES ARE IN THE RAW DATA — 550x100 shows exactly 6 bursts at a 300 mA
  threshold. They are mis-WINDOWED, not missing. The fix is to derive heat
  windows from the COMMANDED cccycle schedule (the firmware knows exactly when
  heat starts) instead of thresholding the current trace, then re-run this
  script; those cells should then yield 6 real cycles each. Until that lands,
  detect_ok = 0 marks rows whose cycle count disagrees with the command, so the
  training pipeline can hold them out without them being silently lost.
"""
import csv, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
COOL_S = 30.0

# (folder, i_low_mA, seeded, run_type)
RUNS = [
    ("sweep_20260731_134414", 100, 0, "probe"),   # corner probe, pre-seed fw
    ("sweep_20260731_141513", 100, 1, "probe"),   # post-seed; contact fault at 950x400
    ("sweep_20260731_144243", 100, 1, "probe"),   # post-reseat re-verify
    ("sweep_20260731_145838", 100, 1, "map"),     # grid part 1, aborted at 350x200
    ("sweep_20260731_155129",   0, 1, "map"),     # grid part 2 (resume), --i-low 0
]
LEVELS = [150, 250, 350, 450, 550, 650, 750, 850, 950]
HEATS = [100, 200, 300, 400]
CYCLES_COMMANDED = {"map": 6, "probe": 4}


def truthy(v):
    return str(v) in ("1", "1.0", "True", "true")


# Laser 0 V rail in displacement units: (0 mV - V0) / k with the
# Calibrate_LaserHead fit (k = -0.49779577 mV/um, V0 = 2503.75 mV).
LASER_RAIL_UM = (0.0 - 2503.7500968693835) / -0.49779577092171906   # +5030 um

def hit_laser_rail(x_base_um, dx_um):
    """The report's own `railed` column under-reports: at 950x400 the laser sat
    at 0.0010 V for 1228 samples yet cycle 2 came back unflagged. The endpoint
    is unambiguous — all three cycles land on x_base+dx = 5027.7 um, the same
    value to 0.1 um, because they are all pinned against the same rail. A
    genuine measurement does not repeat to that precision."""
    return (x_base_um + dx_um) >= LASER_RAIL_UM - 30.0


rows = []
for folder, i_low, seeded, run_type in RUNS:
    path = os.path.join(BASE, folder, "summary_report.csv")
    if not os.path.exists(path):
        print(f"  SKIP {folder} (no summary_report.csv — run operator_sweep_report.py)")
        continue
    raw = list(csv.DictReader(open(path, newline="")))
    # Cycle count per condition, to flag mis-windowed cells.
    n_by_cond = {}
    for r in raw:
        k = (int(float(r["level_mA"])), int(float(r["heat_ms"])))
        n_by_cond[k] = n_by_cond.get(k, 0) + 1
    for r in raw:
        lvl = float(r["level_mA"])
        heat = int(float(r["heat_ms"]))
        i = float(r["i_mA"])
        n = n_by_cond[(int(lvl), heat)]
        rows.append({
            "level_mA": int(lvl), "heat_ms": heat, "sweep": folder,
            "run_type": run_type, "cycle": int(float(r["cycle"])),
            "bootstrap": int(truthy(r["bootstrap"])),
            "i_mA": round(i, 2), "cc_pct": round(100.0 * i / lvl, 1),
            "dx_um": float(r["dx_um"]), "x_base_um": float(r["x_base_um"]),
            "F_base_mN": float(r["F_base_mN"]), "dF_mN": float(r["dF_mN"]),
            "clipped": int(truthy(r.get("clipped"))),
            "railed": int(truthy(r.get("railed"))
                          or hit_laser_rail(float(r["x_base_um"]),
                                            float(r["dx_um"]))),
            "detect_ok": int(n == CYCLES_COMMANDED[run_type]),
            "cool_s": COOL_S, "i_low_mA": i_low, "seeded": seeded,
        })

rows.sort(key=lambda r: (r["heat_ms"], r["level_mA"], r["sweep"], r["cycle"]))
out = os.path.join(BASE, "heat_time_map_20260731_all.csv")
cols = ["level_mA", "heat_ms", "sweep", "run_type", "cycle", "bootstrap",
        "i_mA", "cc_pct", "dx_um", "x_base_um", "F_base_mN", "dF_mN",
        "clipped", "railed", "detect_ok", "cool_s", "i_low_mA", "seeded"]
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)

# ---- envelope ------------------------------------------------------------
# The CHART needs a defensible central value, so it aggregates the rows that
# are trustworthy for that purpose (detected correctly, not bootstrap, not at a
# sensor rail). The CSV above keeps everything regardless.
env = []
for h in HEATS:
    for lv in LEVELS:
        s = [r for r in rows if r["heat_ms"] == h and r["level_mA"] == lv
             and r["detect_ok"] and not r["bootstrap"]
             and not r["clipped"] and not r["railed"]]
        if not s:
            continue
        dx = np.array([r["dx_um"] for r in s])
        dF = np.array([r["dF_mN"] for r in s])
        env.append({
            "heat_ms": h, "level_mA": lv, "n_cycles": len(s),
            "i_mA_mean": round(float(np.mean([r["i_mA"] for r in s])), 1),
            "dx_um_median": round(float(np.median(dx)), 1),
            "dx_um_mean": round(float(np.mean(dx)), 1),
            "dx_um_std": round(float(np.std(dx, ddof=1)) if len(dx) > 1 else 0.0, 1),
            "dx_um_min": round(float(dx.min()), 1),
            "dx_um_max": round(float(dx.max()), 1),
            "dF_mN_median": round(float(np.median(dF)), 2),
            "dF_mN_std": round(float(np.std(dF, ddof=1)) if len(dF) > 1 else 0.0, 2),
            "cool_s": int(COOL_S),
        })
with open(os.path.join(BASE, "heat_time_map_20260731_envelope.csv"),
          "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(env[0].keys()))
    w.writeheader()
    w.writerows(env)


def chart(key_med, key_std, ylabel, title, fname):
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    for h in HEATS:
        s = [e for e in env if e["heat_ms"] == h]
        if not s:
            continue
        ax.errorbar([e["i_mA_mean"] for e in s], [e[key_med] for e in s],
                    yerr=[e[key_std] for e in s], marker="o", capsize=3,
                    label=f"{h} ms")
    ax.set_xlabel("achieved current (mA)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(title="heat")
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, fname), dpi=140)
    plt.close(fig)


chart("dx_um_median", "dx_um_std", "stroke (µm)",
      "SMA stroke envelope — 2026-07-31, 30 s cool",
      "heat_time_map_20260731_envelope.png")
chart("dF_mN_median", "dF_mN_std", "force rise (mN)",
      "SMA force envelope — 2026-07-31, 30 s cool",
      "heat_time_map_20260731_force_envelope.png")

n_ok = sum(r["detect_ok"] for r in rows)
print(f"{len(rows)} cycles written ({n_ok} detect_ok, "
      f"{len(rows) - n_ok} flagged detect_ok=0)")
print(f"  clipped={sum(r['clipped'] for r in rows)}  "
      f"railed={sum(r['railed'] for r in rows)}  "
      f"bootstrap={sum(r['bootstrap'] for r in rows)}")
print(f"  envelope covers {len(env)} of 35 conditions")
