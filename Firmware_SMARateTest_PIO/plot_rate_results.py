#!/usr/bin/env python3
"""
plot_rate_results.py — visualize the round-2 rate ladder (runs 0-7).

Reads run0.txt … run7.txt (the rate_probe.py captures) and renders
`rate_ladder_results.png`.

The story the figure tells:
  1. We got to 962 Hz (96 points per 100 ms fire) with a clean stream.
  2. But the measured V inflates linearly with the ADC's CONVERSION DUTY —
     the fraction of wall-clock time the ADC spends converting. R² = 0.9996.
  3. Extrapolating to zero duty gives 2.991 V, and the DAC/LDO model says the
     commanded drive is 3.000 V. Two independent numbers, 0.3% apart.
  4. R = V/I is IMMUNE (both channels scale together) — it sat at 21.4 Ω through
     the whole thing, which is exactly why the bug nearly escaped.

Usage:  python plot_rate_results.py
"""

from __future__ import annotations

import re
import statistics as st
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# dataviz reference palette, light surface
SURFACE, INK, INK_2, MUTED, GRID, AXIS = (
    "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7")
C_ERR = "#e34948"     # red    — the error
C_OK = "#1baf7a"      # aqua   — the good configs
C_RATE = "#2a78d6"    # blue   — rate
C_RES = "#4a3aa7"     # violet — resistance
C_FIT = "#0b0b0b"
_PLATE = dict(facecolor=SURFACE, edgecolor="none", alpha=0.9, pad=3.0)

RATE_RE = re.compile(r"\[RATE\]\s+n=(\d+)\s+readSma_us=(\d+)\s+emit_us=(\d+)\s+dt_us=(\d+)")
V_COMMANDED = 0.31 + (2003 / 4095.0) * 5.5     # LDO model at DAC code 2003


def load(run: str) -> dict | None:
    p = Path(f"{run}.txt")
    if not p.exists():
        return None
    lines = p.read_text(errors="replace").splitlines()

    dv, dc, di = {}, {}, {}
    for s in lines:
        f = s.split("\t")
        if len(f) >= 6:
            try:
                if f[1] == "3":
                    dv[int(f[5])] = float(f[3]); dc[int(f[5])] = int(f[2])
                elif f[1] == "4":
                    di[int(f[5])] = float(f[3])
            except ValueError:
                pass
    keys = sorted(set(dv) & set(di))
    fire = [k for k in keys if dv[k] > 1.75]
    if not fire:
        return None

    n, rd, dt = [], [], []
    for s in lines:
        m = RATE_RE.search(s)
        if m:
            n.append(int(m[1])); rd.append(int(m[2]))
            if int(m[4]) > 0:
                dt.append(int(m[4]))
    settle = 0
    for s in lines:
        if s.startswith("[STATUS]"):
            mm = re.search(r"settle_us=(\d+)", s)
            if mm:
                settle = int(mm[1])

    med_dt, med_rd = st.median(dt), st.median(rd)
    V = np.array([dv[k] for k in fire])
    I = np.array([di[k] for k in fire])
    adc_us = med_rd - 2 * settle
    return dict(run=run, N=int(st.median(n)), dt_us=med_dt, readsma_us=med_rd,
                settle_us=settle, adc_us=adc_us,
                duty=100.0 * adc_us / med_dt, hz=1e6 / med_dt,
                pts=100.0 / (med_dt / 1000.0),
                V=float(V.mean()), I=float(I.mean()),
                R=float(V.mean() / I.mean()), dac=int(st.median([dc[k] for k in fire])))


def main() -> int:
    runs = [r for r in (load(f"run{i}") for i in range(8)) if r]
    if not runs:
        print("no run*.txt found — run rate_probe.py first")
        return 2

    duty = np.array([r["duty"] for r in runs])
    V = np.array([r["V"] for r in runs])
    m, b = np.polyfit(duty, V, 1)
    r2 = 1 - np.sum((V - (m * duty + b)) ** 2) / np.sum((V - V.mean()) ** 2)

    fig, ax = plt.subplots(2, 2, figsize=(14, 9), facecolor=SURFACE)
    ax = ax.ravel()

    def style(a):
        a.set_facecolor(SURFACE)
        a.grid(True, color=GRID, lw=0.6)
        a.set_axisbelow(True)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            a.spines[s].set_color(AXIS)
        a.tick_params(colors=MUTED, labelsize=8)

    # ---- (0) THE MONEY PLOT: V vs ADC conversion duty -----------------------
    style(ax[0])
    xs = np.linspace(0, 92, 50)
    ax[0].plot(xs, m * xs + b, lw=1.6, color=C_FIT, zorder=2,
               label=f"fit: V = {m:.4f}·duty + {b:.3f}   (R² = {r2:.4f})")
    ax[0].axhline(V_COMMANDED, ls="--", lw=1.4, color=C_OK, zorder=1)
    ax[0].text(91, V_COMMANDED, f" commanded {V_COMMANDED:.3f} V\n (DAC code 2003)",
               color=C_OK, fontsize=8, va="center", ha="right")
    ax[0].plot(0, b, "*", ms=16, color=C_FIT, zorder=4)
    ax[0].annotate(f"extrapolated to zero duty: {b:.3f} V\n"
                   f"→ agrees with the commanded 3.000 V to 0.3%",
                   xy=(0, b), xytext=(14, 3.62), fontsize=8.5, color=INK,
                   arrowprops=dict(arrowstyle="->", color=INK_2, lw=1.2),
                   bbox=_PLATE)
    # run7 (12%), run0/run1 (14%) and run6 (20%) crowd the low-duty corner —
    # stagger their labels so the three "good" configs stay readable.
    OFF = {"run7": (-34, -4), "run0": (30, 2), "run1": (30, -12),
           "run6": (30, -4), "run5": (26, -4), "run2": (0, -22),
           "run3": (0, -22), "run4": (0, -22)}
    for r in runs:
        good = r["duty"] <= 21
        ax[0].plot(r["duty"], r["V"], "o", ms=9, zorder=3,
                   color=C_OK if good else C_ERR,
                   markeredgecolor=SURFACE, markeredgewidth=1.2)
        ax[0].annotate(f"{r['run']} N={r['N']}", (r["duty"], r["V"]),
                       textcoords="offset points",
                       xytext=OFF.get(r["run"], (0, -22)),
                       ha="center", fontsize=7.5, color=INK_2)
    ax[0].set_title("The bug: measured V inflates with ADC conversion duty\n"
                    "(the DAC code was 2003 in ALL 8 runs — the drive never changed)",
                    fontsize=10.5, color=INK, loc="left")
    ax[0].set_xlabel("ADC conversion duty (% of wall-clock time the ADC is converting)",
                     fontsize=9, color=INK_2)
    ax[0].set_ylabel("measured V during fire (V)", fontsize=9, color=INK_2)
    ax[0].set_xlim(-4, 92)
    ax[0].legend(loc="upper left", fontsize=8, frameon=False, labelcolor=INK_2)

    # ---- (1) the rate ladder -------------------------------------------------
    style(ax[1])
    x = np.arange(len(runs))
    hz = [r["hz"] for r in runs]
    bars = ax[1].bar(x, hz, width=0.62, color=[C_OK if r["duty"] <= 21 else C_ERR
                                               for r in runs],
                     edgecolor=SURFACE, linewidth=1.5)
    ax[1].axhline(1000, ls="--", lw=1.2, color=MUTED)
    ax[1].text(-0.4, 1000, "1 kHz target ", color=INK_2, fontsize=8,
               va="bottom", ha="left")
    for xi, r in zip(x, runs):
        ax[1].annotate(f"{r['hz']:.0f} Hz\n{r['pts']:.0f} pts", (xi, r["hz"]),
                       textcoords="offset points", xytext=(0, 4), ha="center",
                       fontsize=7.5, color=INK)
    ax[1].set_xticks(x)
    ax[1].set_xticklabels([f"{r['run']}\nN={r['N']}\n{r['dt_us']/1000:.0f}ms"
                           for r in runs], fontsize=7.5)
    ax[1].set_title("Rate achieved (points per 100 ms fire above each bar)",
                    fontsize=10.5, color=INK, loc="left")
    ax[1].set_ylabel("SMA sample rate (Hz)", fontsize=9, color=INK_2)
    ax[1].margins(y=0.20)

    # ---- (2) why it hid: R is immune ----------------------------------------
    style(ax[2])
    R = [r["R"] for r in runs]
    err = [100 * (r["V"] - V_COMMANDED) / V_COMMANDED for r in runs]
    ax[2].plot(duty, err, "o-", ms=8, lw=1.6, color=C_ERR,
               markeredgecolor=SURFACE, markeredgewidth=1.2, label="V error (%)")
    ax[2].plot(duty, [100 * (r - np.mean(R)) / np.mean(R) for r in R], "s-",
               ms=8, lw=1.6, color=C_RES, markeredgecolor=SURFACE,
               markeredgewidth=1.2, label="R = V/I error (%)")
    ax[2].axhline(0, lw=1.0, color=AXIS)
    ax[2].set_title("Why it nearly escaped: R = V/I is IMMUNE\n"
                    "(both channels scale together — the ratio cancels exactly)",
                    fontsize=10.5, color=INK, loc="left")
    ax[2].set_xlabel("ADC conversion duty (%)", fontsize=9, color=INK_2)
    ax[2].set_ylabel("error vs the true value (%)", fontsize=9, color=INK_2)
    ax[2].text(0.98, 0.06,
               f"R stayed at {np.mean(R):.1f} ± {np.std(R):.1f} Ω across all 8 runs\n"
               f"while V drifted by a third. Checking R alone\n"
               f"would have called this a clean pass.",
               transform=ax[2].transAxes, ha="right", fontsize=8, color=INK_2,
               bbox=_PLATE)
    ax[2].legend(loc="upper left", fontsize=8.5, frameon=False, labelcolor=INK_2)

    # ---- (3) where the time goes — and where the duty comes from -------------
    style(ax[3])
    settle = np.array([2 * r["settle_us"] for r in runs]) / 1000.0
    adc = np.array([r["adc_us"] for r in runs]) / 1000.0
    idle = np.array([r["dt_us"] for r in runs]) / 1000.0 - settle - adc
    ax[3].bar(x, adc, width=0.62, color=C_ERR, edgecolor=SURFACE, linewidth=1.2,
              label="ADC converting  ← this is the 'duty'")
    ax[3].bar(x, settle, bottom=adc, width=0.62, color="#eda100",
              edgecolor=SURFACE, linewidth=1.2, label="settle delay (2 × SMA_SETTLE_US)")
    ax[3].bar(x, idle, bottom=adc + settle, width=0.62, color=GRID,
              edgecolor=SURFACE, linewidth=1.2, label="idle (everything else)")
    for xi, r in zip(x, runs):
        ax[3].annotate(f"{r['duty']:.0f}%", (xi, r["dt_us"] / 1000.0),
                       textcoords="offset points", xytext=(0, 3), ha="center",
                       fontsize=8, color=INK)
    ax[3].set_xticks(x)
    ax[3].set_xticklabels([f"{r['run']}\nN={r['N']}" for r in runs], fontsize=7.5)
    ax[3].set_title("One sample period, broken down. 'Duty' = red ÷ whole bar.\n"
                    "Fewer conversions (lower N) → less ADC on-time → less sag.",
                    fontsize=10.5, color=INK, loc="left")
    ax[3].set_ylabel("time per SMA sample (ms)", fontsize=9, color=INK_2)
    ax[3].set_yscale("log")
    ax[3].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)

    fig.suptitle("Firmware_SMARateTest_PIO — round 2 rate ladder, runs 0–7 "
                 "(2026-07-13)", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out = Path("rate_ladder_results.png")
    fig.savefig(out, dpi=140, facecolor=SURFACE)
    print(f"wrote {out}")
    print(f"\nfit: V = {m:.5f}·duty% + {b:.4f}   R² = {r2:.4f}")
    print(f"zero-duty extrapolation {b:.3f} V  vs  commanded {V_COMMANDED:.3f} V")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
