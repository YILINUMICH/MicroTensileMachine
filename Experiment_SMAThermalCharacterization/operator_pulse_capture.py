#!/usr/bin/env python3
"""operator_pulse_capture.py — drive one pulse train, record everything, plot it.

NO pass/fail, NO thresholds, NO aborts. It fires the pulses you ask for, saves
every channel, prints the per-pulse numbers, and draws the plots. Use this when
you want to SEE what the rig did; use operator_current_sweep.py only when you
want an automated ceiling search.

    python operator_pulse_capture.py --port COM8 --ma 300
    python operator_pulse_capture.py --port COM8 --ma 300 --cool-s 3 --cycles 5
    python operator_pulse_capture.py --replot data/raw/pulse_20260729_001500

WHY --i-low DEFAULTS TO 100 AND NOT 0
    Electrically these are the same thing. 100 mA is BELOW the reachable floor
    (u_min 0.5 V / R ~4.7 ohm = ~106 mA), so the loop rails at u_min: DAC code 0,
    0.5 V bias, exactly the "cooling = code 0" design. Measured on a healthy rig:
    cool sat at code 8, 0.510 V.

    The difference is only whether the CC loop stays ENGAGED through cool, and
    that decides whether the loop works at all. With i_low=0 the cool phase calls
    ccRelease(), so the next ccEngage() does cc_u_cmd = codeToVldo(0) = 0.5 V and
    cc_u_i = cc_u_cmd — the bootstrap integral RESETS EVERY PULSE and only ever
    gets 100 ms of ramp. Measured 2026-07-29 (sweep_20260729_000147):

        150 mA cmd, i_low=0:  R_est latched on pulse 2  ->  156 mA  (104%)
        250 mA cmd, i_low=0:  src=7 NEVER emitted       ->  118 mA   (47%)

    At 250 mA the 100 ms ramp cannot reach the +-12% gate, so R_est never
    validates and the loop is stuck at the floor forever. Keeping it engaged lets
    R_est latch off the railed cool point, which has had the whole cool phase to
    settle. Set --i-low 0 if you want to reproduce that failure.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from lib_h7_session import (H7, Capture, Sample, SRC_CC_R, SRC_CC_U,  # noqa: E402
                            SRC_LASER, SRC_LOAD, SRC_SMA_I, SRC_SMA_V,
                            heat_windows, measurement_sane, save_capture)

# Calibrate_LaserHead / Calibrate_LoadCell
K_UM, V0_MV = -0.49779577092171906, 2503.7500968693835
MV_PER_MN, V0_LOAD_MV = 10.2009, -34.19
disp_um = lambda v: (v * 1e3 - V0_MV) / K_UM
force_mn = lambda v: (v * 1e3 - V0_LOAD_MV) / MV_PER_MN

C_I, C_X, C_F, C_R = "#2a78d6", "#178a5a", "#eb6834", "#8a8880"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#d8d7d2"


def series(cap, src):
    r = sorted((s.hw_us * 1e-6, s.value) for s in cap.samples if s.src == src)
    if not r:
        return np.array([]), np.array([])
    return np.array([x[0] for x in r]), np.array([x[1] for x in r])


def smooth(y, n):
    return y if len(y) < n or n < 2 else np.convolve(y, np.ones(n) / n, "same")


def report(cap, ma):
    w = heat_windows(cap)
    t0 = min((series(cap, s)[0][0] for s in (1, 2, 3, 4, 6)
              if len(series(cap, s)[0])), default=0.0)
    print(f"\n  {len(w)} pulses found")
    print(f"  {'#':>2} {'t[s]':>7} {'dur[ms]':>8} {'I[mA]':>8} {'u[V]':>7} "
          f"{'R_est':>7} {'dx[um]':>8} {'dF[mN]':>8}")
    rows = []
    for k, (a, b) in enumerate(w, 1):
        row = {"cycle": k, "t_s": a - t0, "dur_ms": 1e3 * (b - a)}
        ti, vi = series(cap, SRC_SMA_I)
        tu, vu = series(cap, SRC_CC_U)
        tr, vr = series(cap, SRC_CC_R)
        row["i_mA"] = 1e3 * vi[(ti >= a) & (ti <= b)].mean() if len(ti) else np.nan
        row["u_V"] = vu[(tu >= a) & (tu <= b)].mean() if len(tu) else np.nan
        seg = vr[(tr >= a) & (tr <= b)] if len(tr) else np.array([])
        row["R_est"] = seg.mean() if len(seg) else np.nan
        for key, src, conv in [("dx_um", SRC_LASER, disp_um),
                               ("dF_mN", SRC_LOAD, force_mn)]:
            t, v = series(cap, src)
            pre = conv(v[(t >= a - 0.40) & (t < a - 0.02)]) if len(t) else []
            post = conv(v[(t >= a) & (t <= b + 1.5)]) if len(t) else []
            row[key] = (post[np.argmax(np.abs(post - pre.mean()))] - pre.mean()
                        if len(pre) and len(post) else np.nan)
        rows.append(row)
        print(f"  {k:>2} {row['t_s']:7.2f} {row['dur_ms']:8.1f} {row['i_mA']:8.1f} "
              f"{row['u_V']:7.3f} {row['R_est']:7.3f} {row['dx_um']:8.1f} "
              f"{row['dF_mN']:8.2f}")
    if rows:
        ach = np.nanmean([r["i_mA"] for r in rows])
        print(f"\n  achieved {ach:.0f} mA of {ma:.0f} commanded ({100*ach/ma:.0f}%)")
        dx = [r["dx_um"] for r in rows if r["dx_um"] == r["dx_um"]]
        if dx:
            print(f"  displacement {np.mean(dx):+.1f} um mean signed, "
                  f"{np.mean(np.abs(dx)):.1f} um rectified "
                  f"({'coherent — real motion' if abs(np.mean(dx)) > 0.6*np.mean(np.abs(dx)) else 'INCOHERENT — likely noise'})")
    ok, why = measurement_sane(cap, w)
    print(f"  sense check: {'OK' if ok else 'SUSPECT'} — {why.split('.')[0]}")
    return rows, w


def plot(cap, w, rows, ma, out: Path, heat_ms):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "text.color": INK,
        "axes.labelcolor": INK2, "axes.edgecolor": GRID, "savefig.facecolor": SURFACE,
        "xtick.color": INK2, "ytick.color": INK2, "font.size": 9})

    def strip(ax):
        ax.grid(True, color=GRID, lw=.5, alpha=.6)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)

    t0 = min((series(cap, s)[0][0] for s in (1, 2, 3, 4, 6)
              if len(series(cap, s)[0])), default=0.0)

    # ---- fig 1: full timeline, every channel ----
    fig, ax = plt.subplots(5, 1, figsize=(13, 12), sharex=True)
    for a_, b_ in w:
        for r in range(5):
            ax[r].axvspan(a_ - t0, b_ - t0, color=C_R, alpha=.25, lw=0)
    ti, vi = series(cap, SRC_SMA_I)
    ax[0].plot(ti - t0, 1e3 * vi, color=C_I, lw=.4, alpha=.25)
    ax[0].plot(ti - t0, 1e3 * smooth(vi, 15), color=C_I, lw=1.1)
    ax[0].axhline(ma, color=C_R, ls="--", lw=1.4, label=f"{ma:.0f} mA commanded")
    ax[0].set_ylabel("current [mA]"); ax[0].legend(frameon=False, fontsize=8)
    tx, vx = series(cap, SRC_LASER)
    ax[1].plot(tx - t0, disp_um(vx), color=C_X, lw=1.0)
    ax[1].set_ylabel("displacement [µm]")
    tf, vf = series(cap, SRC_LOAD)
    ax[2].plot(tf - t0, force_mn(vf), color=C_F, lw=.4, alpha=.3)
    ax[2].plot(tf - t0, smooth(force_mn(vf), 15), color=C_F, lw=1.1)
    ax[2].set_ylabel("force [mN]")
    tu, vu = series(cap, SRC_CC_U)
    ax[3].plot(tu - t0, vu, color=INK2, lw=1.0)
    ax[3].set_ylabel("CC command u [V]")
    tr, vr = series(cap, SRC_CC_R)
    if len(tr):
        ax[3].twinx().plot(tr - t0, vr, color=C_I, lw=1.0, alpha=.7)
    tv, vv = series(cap, SRC_SMA_V)
    ax[4].plot(tv - t0, vv, color=C_I, lw=.4, alpha=.25)
    ax[4].plot(tv - t0, smooth(vv, 15), color=C_I, lw=1.1)
    ax[4].set_ylabel("V_sma [V]"); ax[4].set_xlabel("time [s]")
    for a_ in ax:
        strip(a_)
    fig.suptitle(f"{ma:.0f} mA / {heat_ms:.0f} ms x {len(w)} pulses — full record"
                 f"   (shaded = heat)", x=.006, ha="left", fontsize=13,
                 color=INK, weight="bold")
    fig.tight_layout(rect=[0, 0, 1, .965])
    fig.savefig(out / "fig1_timeline.png", dpi=130)

    # ---- fig 2: pulses overlaid ----
    PRE, POST = 0.3, max(2.0, 0.5 * (w[1][0] - w[0][0]) if len(w) > 1 else 2.0)
    fig, ax = plt.subplots(1, 3, figsize=(14.5, 4.6))
    for k, (a_, b_) in enumerate(w):
        for j, (src, conv, col, lbl) in enumerate([
                (SRC_SMA_I, lambda x: 1e3 * x, C_I, "current [mA]"),
                (SRC_LASER, disp_um, C_X, "displacement change [µm]"),
                (SRC_LOAD, force_mn, C_F, "force change [mN]")]):
            t, v = series(cap, src)
            m = (t >= a_ - PRE) & (t <= a_ + POST)
            if not m.any():
                continue
            y = conv(v[m])
            base = y[t[m] <= a_][-30:]
            off = base.mean() if len(base) and j else 0.0
            ax[j].plot(t[m] - a_, y - off, lw=.9, alpha=.6, color=col,
                       label=f"cycle {k+1}" if j == 0 else None)
            ax[j].set_ylabel(lbl); ax[j].set_xlabel("time from heat onset [s]")
    for j in range(3):
        ax[j].axvspan(0, heat_ms / 1000.0, color=C_R, alpha=.25, lw=0)
        strip(ax[j])
    ax[0].axhline(ma, color=C_R, ls="--", lw=1.4)
    ax[0].legend(frameon=False, fontsize=8, ncol=2)
    fig.suptitle(f"{ma:.0f} mA — every pulse aligned on its onset",
                 x=.006, ha="left", fontsize=13, color=INK, weight="bold")
    fig.tight_layout(rect=[0, 0, 1, .93])
    fig.savefig(out / "fig2_pulses.png", dpi=130)
    print(f"  -> fig1_timeline.png, fig2_pulses.png")


def load(d: Path):
    cap = Capture()
    for r in csv.DictReader(open(d / "h7.csv")):
        cap.samples.append(Sample(int(r["src"]), int(r["hw_us"]),
                                  float(r["value"]), int(r["raw_code"]),
                                  int(r["seq"])))
    return cap, json.load(open(d / "h7.meta.json"))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="COM8")
    p.add_argument("--transport", choices=("usb", "udp"), default=None,
                   help="sample-stream transport (default: udp since the "
                        "2026-08-07 cutover; H7_TRANSPORT env overrides)")
    p.add_argument("--pc-ip", default=None,
                   help="THIS PC's address on the H7's segment, for UDP "
                        "(default 169.254.245.100; H7_PC_IP env overrides)")
    p.add_argument("--ma", type=float, default=300.0)
    p.add_argument("--heat-ms", type=int, default=100)
    p.add_argument("--cool-s", type=float, default=3.0)
    p.add_argument("--cycles", type=int, default=5)
    p.add_argument("--i-low", type=float, default=100.0,
                   help="cool target; 100 rails at u_min = code 0 = 0.5 V bias. "
                        "0 disengages the loop and breaks the bootstrap — see "
                        "the module docstring.")
    p.add_argument("--settle-s", type=float, default=15.0)
    p.add_argument("--outdir",
                   default=str(Path(__file__).resolve().parent / "data" / "raw"))
    p.add_argument("--replot", metavar="DIR",
                   help="re-analyse and re-plot an existing capture")
    a = p.parse_args()

    if a.replot:
        d = Path(a.replot)
        cap, meta = load(d)
        rows, w = report(cap, meta.get("ma", a.ma))
        plot(cap, w, rows, meta.get("ma", a.ma), d, meta.get("heat_ms", a.heat_ms))
        return 0

    out = Path(a.outdir) / f"pulse_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    run_s = a.cycles * (a.heat_ms / 1000.0 + a.cool_s) + 3.0
    print(f"\n{a.ma:.0f} mA | {a.heat_ms} ms heat | {a.cool_s:.1f} s cool | "
          f"{a.cycles} cycles | i_low {a.i_low:.0f} mA")
    print(f"  -> {out}   (~{run_s + a.settle_s:.0f} s total)\n")

    h7 = H7(a.port, transport=a.transport, pc_ip=a.pc_ip)
    try:
        h7.open()
        h7.send("disarm")
        time.sleep(0.4)
        print(f"  settle {a.settle_s:.0f}s ...", flush=True)
        h7.capture(a.settle_s, ping=False)
        h7.send("arm")
        time.sleep(0.6)
        lead = h7.capture(2.0)
        h7.send(f"cccycle {a.ma:.0f} {a.i_low:.0f} {a.heat_ms} "
                f"{int(a.cool_s*1000)} {a.cycles}")
        print(f"  running {run_s:.0f}s ...", flush=True)
        cap = h7.capture(run_s, on_console=lambda t, s: print(f"    {s}")
                         if ("FAULT" in s or "ERR" in s or "WARN" in s) else None)
        cap.samples = lead.samples + cap.samples
        cap.console = lead.console + cap.console
    finally:
        h7.disarm()
        h7.close()

    save_capture(cap, out / "h7.csv", {
        "ma": a.ma, "heat_ms": a.heat_ms, "cool_s": a.cool_s,
        "cycles": a.cycles, "i_low": a.i_low, "port": a.port,
        "captured_utc": datetime.now(timezone.utc).isoformat()})
    rows, w = report(cap, a.ma)
    with open(out / "pulses.csv", "w", newline="") as fh:
        if rows:
            wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
    plot(cap, w, rows, a.ma, out, a.heat_ms)
    print(f"\n  {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
