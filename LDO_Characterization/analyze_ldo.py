#!/usr/bin/env python3
"""
analyze_ldo.py — metrics + plots for an LDO_Characterization run.

Reads the capture CSVs + manifest.json written by run_experiment.py and produces:
  - summary.csv : settle time (+/-1%, +/-0.1%), overshoot, 10-90% rise, per shot
  - settling_<step>.png : loaded vs unloaded transient overlay with settle markers
  - settling_overview.png : all steps in a grid
  - ripple.png : steady-state PKPK / STDEV bar chart

Runs standalone on any run dir, so it also works on data captured earlier:
    python analyze_ldo.py data/ldo_20260616_120000
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ----------------------------------------------------------------------------
#  capture-level metrics
# ----------------------------------------------------------------------------
def load_capture(path: Path):
    """Return (t, v_trig, v_dac, v_out, i_a). i_a is None if not captured."""
    t, vtr, vdac, vout, i = [], [], [], [], []
    has_i = False
    with open(path, "r") as f:
        r = csv.DictReader(f)
        has_i = "i_a" in (r.fieldnames or [])
        for row in r:
            t.append(float(row["t_s"]))
            vtr.append(float(row["v_trig"]))
            vdac.append(float(row["v_dac"]))
            vout.append(float(row["v_out"]))
            if has_i:
                i.append(float(row["i_a"]))
    i_a = np.array(i) if has_i else None
    return (np.array(t), np.array(vtr), np.array(vdac), np.array(vout), i_a)


def find_t0(t, v_trig, level: float = 1.0) -> Optional[float]:
    """Time of the first rising crossing of `level` on the trigger channel.
    Falls back to None if no clean edge (caller then uses record start)."""
    above = v_trig > level
    idx = np.where(~above[:-1] & above[1:])[0]
    if len(idx) == 0:
        return None
    return float(t[idx[0] + 1])


def settle_metrics(t, v_out, t0: float, settle_frac: float = 0.15) -> dict:
    """Settling time, overshoot and 10-90% rise relative to the step at t0."""
    # final value = mean of the trailing `settle_frac` of the record
    n = len(v_out)
    tail = v_out[int(n * (1 - settle_frac)):]
    v_final = float(np.mean(tail)) if len(tail) else float(v_out[-1])

    # baseline = mean of samples before t0 (pre-trigger), else first 5%
    pre = v_out[t < t0]
    v_base = float(np.mean(pre)) if len(pre) >= 3 else float(np.mean(v_out[:max(3, n // 20)]))

    span = v_final - v_base
    direction = np.sign(span) if span != 0 else 1.0
    aspan = abs(span)

    # Noise floor: std of the settled tail. A settle band tighter than the
    # noise can never be "reached", so report NaN rather than a record-end clamp.
    tail = v_out[int(n * (1 - settle_frac)):]
    noise = float(np.std(tail)) if len(tail) else 0.0

    def settle_time(tol_frac: float) -> float:
        if aspan == 0:
            return 0.0
        tol = tol_frac * aspan
        if tol < 2.0 * noise:
            return float("nan")           # band below noise floor — unresolvable
        outside = np.abs(v_out - v_final) > tol
        post = outside & (t >= t0)
        idx = np.where(post)[0]
        if len(idx) == 0:
            return 0.0
        return float(t[idx[-1]] - t0)     # last time it left the band

    # overshoot: max excursion beyond v_final in the step direction
    post_mask = t >= t0
    if aspan > 0 and post_mask.any():
        seg = v_out[post_mask]
        if direction > 0:
            peak = float(np.max(seg))
            overshoot = max(0.0, (peak - v_final)) / aspan * 100.0
        else:
            trough = float(np.min(seg))
            overshoot = max(0.0, (v_final - trough)) / aspan * 100.0
    else:
        overshoot = 0.0

    # 10-90% rise time (in step direction)
    rise = float("nan")
    if aspan > 0 and post_mask.any():
        seg_t = t[post_mask]
        seg_v = v_out[post_mask]
        frac = (seg_v - v_base) / span      # 0 at base, 1 at final
        try:
            i10 = np.where(frac >= 0.1)[0][0]
            i90 = np.where(frac >= 0.9)[0][0]
            rise = float(seg_t[i90] - seg_t[i10])
        except IndexError:
            rise = float("nan")

    return {
        "v_base": v_base, "v_final": v_final, "span_v": span,
        "settle_1pct_ms": settle_time(0.01) * 1e3,
        "settle_0p1pct_ms": settle_time(0.001) * 1e3,
        "overshoot_pct": overshoot,
        "rise_10_90_ms": rise * 1e3 if rise == rise else float("nan"),
    }


def crossing_time(t, v, t0: float, frac: float = 0.5,
                  settle_frac: float = 0.15) -> Optional[float]:
    """Absolute time at which channel `v` first reaches `frac` of its
    base->final transition after t0. Returns None if there's no resolvable step.
    Each channel uses its OWN baseline/final (the DAC node and LDO output sit at
    different absolute voltages)."""
    n = len(v)
    if n == 0:
        return None
    v_final = float(np.mean(v[int(n * (1 - settle_frac)):]))
    pre = v[t < t0]
    v_base = float(np.mean(pre)) if len(pre) >= 3 else float(np.mean(v[:max(3, n // 20)]))
    span = v_final - v_base
    if abs(span) < 1e-9:
        return None
    target = v_base + frac * span
    post = t >= t0
    seg_t, seg_v = t[post], v[post]
    if len(seg_t) == 0:
        return None
    idx = np.where(seg_v >= target)[0] if span > 0 else np.where(seg_v <= target)[0]
    if len(idx) == 0:
        return None
    return float(seg_t[idx[0]])


def cascade_metrics(t, v_dac, v_out, t0: float) -> dict:
    """Decompose the response into its two stages, using the 50% crossing of each
    channel as the 'it moved' instant:

      trigger (t0)  --[I2C write latency]-->  DAC steps  --[regulator]-->  LDO follows

    `trig_to_dac_ms` is the firmware-fires-edge -> DAC-output-changes delay (the
    MCP4728 is written over I2C, so this is dominated by the I2C transaction +
    firmware overhead). `dac_to_ldo_ms` is the DAC-moves -> LDO-output-follows
    delay (the regulator's response, 50%->50%). Both are NaN if a step isn't
    resolvable. NOTE: at a slow timebase the I2C delay can be < 1 sample — shrink
    `timebase_s` to resolve it (the LDO follow is the slow part and stays visible).
    """
    t_dac = crossing_time(t, v_dac, t0, 0.5)
    t_ldo = crossing_time(t, v_out, t0, 0.5)
    trig_to_dac = (t_dac - t0) if t_dac is not None else float("nan")
    dac_to_ldo = (t_ldo - t_dac) if (t_dac is not None and t_ldo is not None) else float("nan")
    return {
        "t_dac": t_dac, "t_ldo": t_ldo,      # absolute (for plotting); not in summary
        "trig_to_dac_ms": trig_to_dac * 1e3 if trig_to_dac == trig_to_dac else float("nan"),
        "dac_to_ldo_ms": dac_to_ldo * 1e3 if dac_to_ldo == dac_to_ldo else float("nan"),
    }


def current_metrics(t, i_a, t0: float, settle_frac: float = 0.15) -> dict:
    """Steady current, inrush peak, and inrush ratio from the INA296A channel."""
    n = len(i_a)
    tail = i_a[int(n * (1 - settle_frac)):]
    i_final = float(np.mean(tail)) if len(tail) else float(i_a[-1])
    post = i_a[t >= t0]
    i_peak = float(np.max(post)) if len(post) else float("nan")
    ratio = (i_peak / i_final) if i_final not in (0.0,) else float("nan")
    return {"i_final_a": i_final, "inrush_peak_a": i_peak, "inrush_ratio": ratio}


# ----------------------------------------------------------------------------
#  run-level driver
# ----------------------------------------------------------------------------
def analyze_run(run_dir: Path, trig_level: float = 1.0) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    settling = [m for m in manifest if m["kind"] == "settling"]
    ripple = [m for m in manifest if m["kind"] == "ripple"]

    rows = []
    captures = {}     # (step, load, repeat) -> arrays
    for m in settling:
        path = run_dir / m["file"]
        if not path.exists():
            continue
        t, vtr, vdac, vout, i_a = load_capture(path)
        t0 = find_t0(t, vtr, trig_level)
        if t0 is None:
            t0 = float(t[0])
        mets = settle_metrics(t, vout, t0)
        casc = cascade_metrics(t, vdac, vout, t0)
        mets.update({k: v for k, v in casc.items() if k in ("trig_to_dac_ms", "dac_to_ldo_ms")})
        if i_a is not None:
            mets.update(current_metrics(t, i_a, t0))
        captures[(m["step"], m["load"], m["repeat"])] = {
            "t": t, "vdac": vdac, "vout": vout, "t0": t0,
            "t_dac": casc["t_dac"], "t_ldo": casc["t_ldo"], "mets": mets, "i_a": i_a,
        }
        rows.append({**{k: m[k] for k in ("step", "load", "from_v", "to_v",
                                          "code_from", "code_to", "repeat")},
                     **{k: round(v, 4) for k, v in mets.items()}})

    # --- summary.csv ---
    summary_path = run_dir / "summary.csv"
    if rows:
        keys = list(rows[0].keys())
        with open(summary_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        print(f"  summary -> {summary_path}")

    _plot_per_step(run_dir, captures, settling)
    _plot_cascade(run_dir, captures, settling)
    _plot_overview(run_dir, captures, settling)
    if ripple:
        _plot_ripple(run_dir, ripple)
    return summary_path


def _step_names(settling):
    seen = []
    for m in settling:
        if m["step"] not in seen:
            seen.append(m["step"])
    return seen


def _plot_one(ax, captures, step, settling):
    colors = {"unloaded": "tab:blue", "loaded": "tab:red"}
    loads = [l for l in ("unloaded", "loaded")
             if any(m["step"] == step and m["load"] == l for m in settling)]
    ax_i = None     # lazily created twin axis for current
    have_current = False
    for load in loads:
        reps = sorted({m["repeat"] for m in settling
                       if m["step"] == step and m["load"] == load})
        for j, rep in enumerate(reps):
            key = (step, load, rep)
            if key not in captures:
                continue
            c = captures[key]
            t, vout, t0, mets, i_a = c["t"], c["vout"], c["t0"], c["mets"], c["i_a"]
            ax.plot((t - t0) * 1e3, vout, color=colors.get(load, "gray"),
                    alpha=0.5 if j else 0.9, lw=1.0,
                    label=load if j == 0 else None)
            if j == 0:
                ax.axhline(mets["v_final"], color=colors.get(load), ls=":", lw=0.7)
                if mets["settle_1pct_ms"] > 0:
                    ax.axvline(mets["settle_1pct_ms"], color=colors.get(load),
                               ls="--", lw=0.7)
            if i_a is not None:
                if ax_i is None:
                    ax_i = ax.twinx()
                have_current = True
                ax_i.plot((t - t0) * 1e3, i_a, color=colors.get(load, "gray"),
                          alpha=0.35 if j else 0.6, lw=0.8, ls="-.")
    ax.set_title(step)
    ax.set_xlabel("t since step (ms)")
    ax.set_ylabel("V_out (V)")
    if have_current and ax_i is not None:
        ax_i.set_ylabel("I_sma (A)  [-. dashed]")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="lower right")


def _plot_per_step(run_dir, captures, settling):
    for step in _step_names(settling):
        fig, ax = plt.subplots(figsize=(7, 4.2))
        _plot_one(ax, captures, step, settling)
        fig.tight_layout()
        out = run_dir / f"settling_{step}.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"  plot -> {out}")


def _fmt_ms(x):
    return f"{x:.1f} ms" if x == x else "n/a"


def _plot_cascade(run_dir, captures, settling):
    """One readable plot per step showing the CASCADE: trigger -> DAC (I2C) ->
    LDO follow, with the two delays marked. Uses one representative shot
    (unloaded, lowest repeat) so the sequence is clear instead of overlaid."""
    for step in _step_names(settling):
        key = None
        for load in ("unloaded", "loaded"):
            cand = sorted([k for k in captures if k[0] == step and k[1] == load],
                          key=lambda k: k[2])
            if cand:
                key = cand[0]
                break
        if key is None:
            continue
        c = captures[key]
        t, t0, mets = c["t"], c["t0"], c["mets"]
        x = (t - t0) * 1e3
        fig, ax = plt.subplots(figsize=(8, 4.4))
        ax.plot(x, c["vdac"], color="tab:green", lw=1.3, label="DAC node (C2)")
        ax.plot(x, c["vout"], color="tab:blue", lw=1.3, label="LDO out (C3)")
        ax.axvline(0, color="k", lw=1.0, alpha=0.7)
        ax.text(0, ax.get_ylim()[1], " trigger", color="k", va="top", fontsize=8)
        for tx, col, lbl in ((c["t_dac"], "tab:green", "DAC steps"),
                             (c["t_ldo"], "tab:blue", "LDO 50%")):
            if tx is not None:
                xm = (tx - t0) * 1e3
                ax.axvline(xm, color=col, ls="--", lw=0.9, alpha=0.8)
                ax.text(xm, ax.get_ylim()[1], f" {lbl}", color=col, va="top",
                        rotation=90, fontsize=7)
        td, dl = mets.get("trig_to_dac_ms"), mets.get("dac_to_ldo_ms")
        ax.set_title(f"{step} [{key[1]}]:  trig→DAC = {_fmt_ms(td)} (I²C),  "
                     f"DAC→LDO = {_fmt_ms(dl)}")
        ax.set_xlabel("t since trigger (ms)")
        ax.set_ylabel("V")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="lower right")
        fig.tight_layout()
        out = run_dir / f"cascade_{step}.png"
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"  plot -> {out}")


def _plot_overview(run_dir, captures, settling):
    steps = _step_names(settling)
    if not steps:
        return
    ncol = 2
    nrow = (len(steps) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(11, 3.6 * nrow), squeeze=False)
    for i, step in enumerate(steps):
        _plot_one(axes[i // ncol][i % ncol], captures, step, settling)
    for k in range(len(steps), nrow * ncol):
        axes[k // ncol][k % ncol].axis("off")
    fig.suptitle("DAC->LDO settling: loaded vs unloaded", y=1.0)
    fig.tight_layout()
    out = run_dir / "settling_overview.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"  plot -> {out}")


def _plot_ripple(run_dir, ripple):
    loads = [r["load"] for r in ripple]
    pkpk = [r.get("pkpk_mean_v", float("nan")) * 1e3 for r in ripple]
    std = [r.get("std_mean_v", float("nan")) * 1e3 for r in ripple]
    x = np.arange(len(loads))
    w = 0.35
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(x - w / 2, pkpk, w, label="PKPK (mV)", color="tab:purple")
    ax.bar(x + w / 2, std, w, label="STDEV (mV)", color="tab:green")
    ax.set_xticks(x)
    ax.set_xticklabels(loads)
    ax.set_ylabel("mV")
    ax.set_title("Steady-state output ripple/noise")
    ax.grid(alpha=0.3, axis="y")
    ax.legend()
    fig.tight_layout()
    out = run_dir / "ripple.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"  plot -> {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python analyze_ldo.py <run_dir>", file=sys.stderr)
        raise SystemExit(1)
    analyze_run(Path(sys.argv[1]))
# end of analyze_ldo.py
