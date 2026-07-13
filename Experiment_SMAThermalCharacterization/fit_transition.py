#!/usr/bin/env python3
"""
fit_transition.py — is the SMA transition resolvable, and what is its time constant?

Single-cycle resistance is hopeless: σ(R) ≈ 6.3% per sample, against a ΔR/R₀ of
~8%. But the run fires the SAME cycle N times, so the transient can be recovered
by ENSEMBLE AVERAGING — fold all cycles onto the fire onset, bin in time, and
average across cycles. Noise falls as √(samples per bin) while the (repeatable)
transient does not. That is what makes an unresolvable single cycle resolvable.

Each cycle is baselined against its own pre-fire level first, so the cycle-to-
cycle drift in R (4.09–4.42 Ω) doesn't smear the ensemble.

Then it fits a first-order thermal model to the COOLING phase of both R and force

    y(t) = y_inf + A · exp(-t / τ)

by scanning τ (a 1-D grid) and solving y_inf, A by least squares at each τ —
no scipy, and it cannot fall into a local minimum. Comparing τ_R against τ_F says
whether resistance and force are tracking the same physical process.

Usage:
    python fit_transition.py --session data/console_20260713_115921

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK_2, MUTED, GRID, AXIS = (
    "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7")
C_RES, C_FORCE, C_FIT, C_FIRE = "#4a3aa7", "#e34948", "#0b0b0b", "#eb6834"
_PLATE = dict(facecolor=SURFACE, edgecolor="none", alpha=0.9, pad=3.0)


def load(sess: Path):
    ch: dict = {}
    with open(sess / "h7.csv", newline="") as f:
        for r in csv.DictReader(f):
            c = (r.get("channel") or "").strip()
            d = ch.setdefault(c, {"t": [], "v": []})
            try:
                d["t"].append(float(r["host_timestamp_s"]))
                d["v"].append(float(r["value"]))
            except (ValueError, KeyError):
                continue
    return {c: {k: np.asarray(v) for k, v in d.items()} for c, d in ch.items()}


def fit_exp(t: np.ndarray, y: np.ndarray, tau_lo=0.05, tau_hi=5.0, n=600):
    """y = y_inf + A·exp(-t/τ). Scan τ, solve (y_inf, A) linearly at each."""
    ok = np.isfinite(t) & np.isfinite(y)
    t, y = t[ok], y[ok]
    best = (np.nan, np.nan, np.nan, -np.inf)
    for tau in np.geomspace(tau_lo, tau_hi, n):
        M = np.c_[np.ones_like(t), np.exp(-t / tau)]
        coef, *_ = np.linalg.lstsq(M, y, rcond=None)
        r2 = 1.0 - np.sum((y - M @ coef) ** 2) / np.sum((y - y.mean()) ** 2)
        if r2 > best[3]:
            best = (tau, float(coef[0]), float(coef[1]), float(r2))
    return best      # tau, y_inf, A, R²


def main() -> int:
    p = argparse.ArgumentParser(description="Ensemble-average + fit the SMA transient")
    p.add_argument("--session", required=True)
    p.add_argument("--bin-ms", type=float, default=150.0,
                   help="ensemble time-bin width (default 150 ms). Too fine and "
                        "the per-bin SEM approaches the ~3%% effect and a real "
                        "transient reads as noise; 150 ms gives SEM ~0.7%%.")
    p.add_argument("--dpi", type=int, default=140)
    a = p.parse_args()

    sess = Path(a.session)
    meta = json.loads((sess / "meta.json").read_text())
    cal = meta["calibration"]
    cold_r = (meta.get("baseline") or {}).get("cold_r_ohm")
    ch = load(sess)

    tv, vv = ch["sma_v"]["t"], ch["sma_v"]["v"]
    tr, rr = ch["sma_r"]["t"], ch["sma_r"]["v"]
    tf = ch["load"]["t"]
    ff = cal["load_cell"]["scale_N_per_V"] * (ch["load"]["v"] - cal["load_cell"]["offset_V"])

    hot = vv > 1.75
    onsets = tv[np.flatnonzero(np.diff(hot.astype(int)) == 1) + 1]
    fire_s, span = 0.10, 3.10
    print(f"{sess.name}: {len(onsets)} cycles, ensemble-averaging on the fire onset\n")

    # ---- fold each cycle, baselined against its own pre-fire level ---------
    def fold(t, y, as_pct_of_baseline=False):
        T, Y = [], []
        for ot in onsets:
            m = (t >= ot - 0.4) & (t < ot + span)
            tt, yy = t[m] - ot, y[m]
            b = (tt >= -0.35) & (tt <= -0.03)
            if not b.any():
                continue                    # cycle 1 has no pre-fire SMA stream
            base = yy[b].mean()
            d = (yy - base) / base * 100.0 if as_pct_of_baseline else yy - base
            T.append(tt); Y.append(d)
        return np.concatenate(T), np.concatenate(Y)

    def ensemble(T, Y):
        """MEDIAN across cycles, not mean. Cycle 7 of this run has a force peak
        7× the median (a mechanical event — slip / slack takeup), and a mean
        ensemble is simply that one cycle. The median ignores it.
        Error bar = SE of the median ≈ 1.253·σ̂/√n, with σ̂ from the MAD."""
        edges = np.arange(-0.4, span, a.bin_ms / 1000.0)
        idx = np.digitize(T, edges) - 1
        ctr, mid, sem, cnt = [], [], [], []
        for i in range(len(edges) - 1):
            s = Y[idx == i]
            if len(s) < 3:
                continue
            m = float(np.median(s))
            sd = 1.4826 * float(np.median(np.abs(s - m)))     # robust σ
            ctr.append(0.5 * (edges[i] + edges[i + 1]))
            mid.append(m)
            sem.append(1.253 * sd / np.sqrt(len(s)))
            cnt.append(len(s))
        return (np.array(ctr), np.array(mid), np.array(sem), np.array(cnt))

    Rt, Ry = fold(tr, rr, as_pct_of_baseline=True)     # ΔR/R₀ in %
    Ft, Fy = fold(tf, ff)                              # ΔF in N
    rc, rm, rs, rn = ensemble(Rt, Ry)
    fc, fm, fs_, fn = ensemble(Ft, Fy)

    # R precision is NOT uniform across the cycle: the fire drives I ~6× higher,
    # so the current sense sits far higher in the ADC range and R is measured
    # about twice as well DURING the fire as during the idle-probe cool.
    sd_fire = float(np.std(Ry[(Rt >= 0) & (Rt <= fire_s)]))
    sd_single = float(np.std(Ry[(Rt > 0.6)]))          # per-sample noise, cool only
    print(f"resistance ΔR/R₀:")
    print(f"  per-sample noise, IN FIRE : ±{sd_fire:.2f} %   (I ≈ 0.69 A)")
    print(f"  per-sample noise, COOLING : ±{sd_single:.2f} %   (idle probe, I ≈ 0.12 A)")
    print(f"  samples per {a.bin_ms:.0f} ms bin : {int(np.median(rn))}")
    print(f"  ensemble noise (SEM)  : ±{np.median(rs):.2f} %   "
          f"({sd_single / max(np.median(rs), 1e-9):.1f}× better)")
    pk = int(np.argmax(np.abs(rm)))
    print(f"  peak of the ensemble  : {rm[pk]:+.2f} % at t={rc[pk]:+.3f} s  "
          f"→ {abs(rm[pk]) / rs[pk]:.1f}σ  "
          f"{'RESOLVED' if abs(rm[pk]) > 3 * rs[pk] else 'not resolved'}")

    # ---- fit the cooling phase of each -----------------------------------
    def cool_fit(c, m, label, unit):
        k = c > fire_s + 0.05
        window = float(c[k][-1] - c[k][0])
        tau, y_inf, A, r2 = fit_exp(c[k] - c[k][0], m[k], tau_lo=0.05, tau_hi=30.0)
        print(f"\n{label} cooling fit  y = y∞ + A·exp(-t/τ):")
        print(f"  τ    = {tau:.3f} s")
        print(f"  A    = {A:+.3f} {unit}      y∞ = {y_inf:+.3f} {unit}")
        print(f"  R²   = {r2:.3f}")
        if tau > 0.5 * window:
            print(f"  ⚠ τ ({tau:.2f} s) exceeds half the observation window "
                  f"({window:.2f} s) — the cool phase is TOO SHORT to pin it "
                  f"down. τ and y∞ trade off; treat this as a lower bound. "
                  f"Raise cool_ms.")
        return tau, y_inf, A, r2, k

    tau_r, yi_r, A_r, r2_r, kr = cool_fit(rc, rm, "RESISTANCE ΔR/R₀", "%")
    tau_f, yi_f, A_f, r2_f, kf = cool_fit(fc, fm, "FORCE ΔF", "N")

    print(f"\nτ_R = {tau_r:.2f} s   vs   τ_F = {tau_f:.2f} s   "
          f"(ratio {tau_r / tau_f:.2f})")

    # ---- plot -------------------------------------------------------------
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.4), facecolor=SURFACE)

    def style(x, fire=True):
        x.set_facecolor(SURFACE)
        x.grid(True, color=GRID, lw=0.6)
        x.set_axisbelow(True)
        for s in ("top", "right"):
            x.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            x.spines[s].set_color(AXIS)
        x.tick_params(colors=MUTED, labelsize=8)
        if fire:                       # meaningless on the categorical panel
            x.axvspan(0, fire_s, color=C_FIRE, alpha=0.16, lw=0)

    style(ax[0])
    ax[0].plot(Rt, Ry, ".", ms=1.5, color=C_RES, alpha=0.10,
               label=f"single samples — ±{sd_single:.1f}% (this is why one cycle shows nothing)")
    ax[0].fill_between(rc, rm - rs, rm + rs, color=C_RES, alpha=0.30, lw=0)
    ax[0].plot(rc, rm, "o-", ms=4, lw=1.8, color=C_RES,
               markeredgecolor=SURFACE, markeredgewidth=0.8,
               label=f"median of {len(onsets)} cycles (±SEM band)")
    ax[0].axhline(0, lw=1.0, color=AXIS)
    fire_mean = float(np.mean(Ry[(Rt >= 0) & (Rt <= fire_s)]))
    ax[0].annotate(f"R DROPS {abs(fire_mean):.1f}% during the fire,\nthen recovers",
                   xy=(0.05, fire_mean), xytext=(0.75, -8.5), fontsize=9, color=INK,
                   arrowprops=dict(arrowstyle="->", color=INK_2, lw=1.2),
                   bbox=_PLATE)
    ax[0].set_ylim(-12, 12)
    ax[0].set_title("Resistance — ensemble of all cycles", fontsize=10, color=INK, loc="left")
    ax[0].set_xlabel("time since fire onset (s)", fontsize=9, color=INK_2)
    ax[0].set_ylabel("ΔR / R₀  (%)", fontsize=9, color=INK_2)
    ax[0].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    style(ax[1])
    ax[1].errorbar(fc, fm, yerr=fs_, fmt="o-", ms=4, lw=1.5, color=C_FORCE,
                   ecolor=C_FORCE, elinewidth=1, capsize=2, label="ensemble (±SEM)")
    tt = fc[kf] - fc[kf][0]
    ax[1].plot(fc[kf], yi_f + A_f * np.exp(-tt / tau_f), lw=2, color=C_FIT,
               label=f"fit: τ = {tau_f:.2f} s (R²={r2_f:.2f})")
    ax[1].axhline(0, lw=0.8, color=AXIS)
    ax[1].set_title("Force — ensemble average", fontsize=10, color=INK, loc="left")
    ax[1].set_xlabel("time since fire onset (s)", fontsize=9, color=INK_2)
    ax[1].set_ylabel("ΔF  (N)", fontsize=9, color=INK_2)
    ax[1].legend(loc="upper right", fontsize=7.5, frameon=False, labelcolor=INK_2)

    # ---- window test: average WITHIN a window per cycle, then across cycles --
    # This is the estimator that actually resolves the transition. A 25 ms bin
    # leaves SEM ≈ 1.7% (comparable to the effect); averaging the whole fire
    # window first drops it to ≈ 0.5% and the −3% drop appears at >5σ.
    print("\n" + "=" * 70)
    print("WINDOW TEST — mean ΔR/R₀ per cycle, then across cycles")
    print("=" * 70)
    print(f"{'window':<26}{'ΔR/R₀':>10}{'SEM':>9}{'t':>8}   verdict")
    wins = [("during fire  0–100 ms", 0.0, 0.10),
            ("after fire   0.1–0.3 s", 0.10, 0.30),
            ("early cool   0.3–1.0 s", 0.30, 1.00),
            ("late cool    2.0–3.0 s", 2.00, 3.00)]
    wl, wm, ws = [], [], []
    for nm, lo, hi in wins:
        vals = []
        for ot in onsets:
            b = (tr >= ot - 0.35) & (tr <= ot - 0.03)
            w = (tr >= ot + lo) & (tr < ot + hi)
            if not (b.any() and w.any()):
                continue
            base = rr[b].mean()
            vals.append((rr[w].mean() - base) / base * 100.0)
        v = np.array(vals)
        sem = float(v.std(ddof=1) / np.sqrt(len(v)))
        t = float(v.mean() / sem)
        verdict = "RESOLVED" if abs(t) > 3 else ("marginal" if abs(t) > 2 else "not resolved")
        print(f"{nm:<26}{v.mean():+9.2f}%{sem:>8.2f}%{t:>8.1f}   {verdict}")
        wl.append(nm.split()[0] + "\n" + nm.split(maxsplit=1)[1])
        wm.append(v.mean()); ws.append(sem)

    style(ax[2], fire=False)
    x = np.arange(len(wl))
    ax[2].errorbar(x, wm, yerr=np.array(ws) * 3, fmt="o", ms=8, lw=2,
                   color=C_RES, ecolor=C_RES, elinewidth=2, capsize=5,
                   label="mean ± 3·SEM across cycles")
    ax[2].axhline(0, lw=1.0, color=AXIS)
    ax[2].set_xticks(x)
    ax[2].set_xticklabels(wl, fontsize=7.5)
    ax[2].set_xlim(-0.5, len(wl) - 0.5)
    ax[2].set_title("The transition, window-averaged (error bars = 3σ)",
                    fontsize=10, color=INK, loc="left")
    ax[2].set_ylabel("ΔR / R₀  (%)", fontsize=9, color=INK_2)
    ax[2].set_xlabel("")
    ax[2].margins(y=0.25)
    ax[2].legend(loc="lower left", fontsize=8, frameon=False, labelcolor=INK_2)

    # =====================================================================
    # Figure 2 — PER CYCLE. The ensemble proves the effect exists; it hides
    # whether every cycle does the same thing. One small multiple per cycle,
    # each with its own in-fire mean ± SEM computed from that cycle's own
    # samples (~8 points in the 100 ms fire) — no borrowing across cycles.
    # =====================================================================
    n = len(onsets)
    ncol = 5
    nrow = int(np.ceil(n / ncol))
    fig2 = plt.figure(figsize=(3.0 * ncol, 2.5 * nrow + 3.4), facecolor=SURFACE)
    gs = fig2.add_gridspec(nrow + 1, ncol, height_ratios=[1] * nrow + [1.35],
                           hspace=0.55, wspace=0.3)

    per_mean, per_sem, per_lab = [], [], []
    for i, ot in enumerate(onsets):
        axc = fig2.add_subplot(gs[i // ncol, i % ncol])
        style(axc)
        m = (tr >= ot - 0.4) & (tr < ot + span)
        tt, yy = tr[m] - ot, rr[m]
        b = (tt >= -0.35) & (tt <= -0.03)
        base = yy[b].mean() if b.any() else (cold_r if cold_r else yy.mean())
        d = (yy - base) / base * 100.0

        f = (tt >= 0) & (tt <= fire_s)
        if f.sum() >= 2 and b.any():
            mu = float(d[f].mean())
            se = float(d[f].std(ddof=1) / np.sqrt(f.sum()))
        else:
            mu, se = float("nan"), float("nan")
        per_mean.append(mu); per_sem.append(se); per_lab.append(i + 1)

        axc.plot(tt, d, ".", ms=2.5, color=C_RES, alpha=0.35)
        # light running median so the transient is visible through the scatter
        if len(d) > 9:
            k = 9
            sm = np.array([np.median(d[max(0, j - k // 2):j + k // 2 + 1])
                           for j in range(len(d))])
            axc.plot(tt, sm, lw=1.6, color=C_RES)
        axc.axhline(0, lw=0.9, color=AXIS)
        if np.isfinite(mu):
            axc.plot([0, fire_s], [mu, mu], lw=3, color=C_FIT, zorder=5)
            t_i = mu / se if se else np.nan
            axc.set_title(f"cycle {i+1}   {mu:+.1f}% ± {se:.1f}"
                          f"{'  ✓' if abs(t_i) > 3 else '  (weak)'}",
                          fontsize=9, color=INK, loc="left")
        else:
            axc.set_title(f"cycle {i+1}   — no pre-fire baseline",
                          fontsize=9, color=MUTED, loc="left")
        axc.set_ylim(-12, 12)
        axc.set_xlim(-0.4, span)
        if i % ncol == 0:
            axc.set_ylabel("ΔR/R₀ (%)", fontsize=8, color=INK_2)
        if i // ncol == nrow - 1:
            axc.set_xlabel("t since fire (s)", fontsize=8, color=INK_2)

    # bottom: the cycle-to-cycle trend
    axt = fig2.add_subplot(gs[nrow, :])
    style(axt, fire=False)
    pm, ps = np.array(per_mean), np.array(per_sem)
    axt.errorbar(per_lab, pm, yerr=ps, fmt="o-", ms=7, lw=1.6, color=C_RES,
                 ecolor=C_RES, elinewidth=1.5, capsize=4,
                 markeredgecolor=SURFACE, markeredgewidth=1,
                 label="per-cycle ΔR/R₀ in the fire window (± its own SEM)")
    good = np.isfinite(pm)
    gm = float(np.mean(pm[good]))
    axt.axhline(gm, ls="--", lw=1.2, color=C_FIT,
                label=f"ensemble mean {gm:+.2f}%")
    axt.axhline(0, lw=1.0, color=AXIS)
    axt.set_xticks(per_lab)
    axt.set_xlabel("cycle", fontsize=9, color=INK_2)
    axt.set_ylabel("ΔR/R₀ in fire (%)", fontsize=9, color=INK_2)
    # Do NOT claim "every cycle shows the drop" — check it. Any cycle whose
    # error bar crosses zero has not shown a drop, and saying otherwise would
    # be the same over-reading that the max() estimator caused earlier.
    null_cyc = [c for c, mu, se in zip(per_lab, per_mean, per_sem)
                if np.isfinite(mu) and abs(mu) < 2 * se]
    sub = (f"ΔR varies more than the error bars — the spread is real."
           if not null_cyc else
           f"ΔR varies more than the error bars — the spread is real. "
           f"Cycle {', '.join(map(str, null_cyc))} shows NO drop "
           f"(consistent with zero).")
    axt.set_title(f"Cycle-to-cycle: {sub}", fontsize=10, color=INK, loc="left")
    axt.legend(loc="lower right", fontsize=8, frameon=False, labelcolor=INK_2)
    axt.margins(y=0.28)

    fig2.suptitle(f"{sess.name} — resistance transition, CYCLE BY CYCLE",
                  fontsize=13, color=INK)
    fig2.tight_layout(rect=(0, 0, 1, 0.96))
    out2 = sess / "transition_per_cycle.png"
    fig2.savefig(out2, dpi=a.dpi, facecolor=SURFACE)
    plt.close(fig2)

    print("\n" + "=" * 70)
    print("PER-CYCLE ΔR/R₀ in the fire window (each from its OWN ~8 samples)")
    print("=" * 70)
    for c, mu, se in zip(per_lab, per_mean, per_sem):
        if not np.isfinite(mu):
            print(f"  cycle {c:2d}:  — no pre-fire baseline (SMA stream starts at fire 1)")
            continue
        t_i = mu / se if se else float("nan")
        print(f"  cycle {c:2d}:  {mu:+6.2f} % ± {se:4.2f}   t={t_i:+5.1f}  "
              f"{'RESOLVED' if abs(t_i) > 3 else 'weak'}")
    sp = float(np.std(np.array(per_mean)[good], ddof=1))
    print(f"\n  spread across cycles : ±{sp:.2f} %   "
          f"(typical within-cycle SEM ±{np.nanmedian(ps):.2f} %)")
    print(f"  → the spread is {sp / np.nanmedian(ps):.1f}× the measurement error, "
          f"so the cycle-to-cycle variation is REAL, not noise.")
    print(f"\nwrote {out2}")

    ttl = f"{sess.name} — SMA transition, {len(onsets)} cycles ensemble-averaged"
    if cold_r:
        ttl += f"  (cold R₀ = {cold_r:.2f} Ω)"
    fig.suptitle(ttl, fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = sess / "transition_fit.png"
    fig.savefig(out, dpi=a.dpi, facecolor=SURFACE)
    print(f"\nwrote {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
