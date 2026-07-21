#!/usr/bin/env python3
"""Analyse PHASE 2 scope captures — coherence and the change-fs alias test.

Companion to capture_phase2.py. Two modes:

    python analyze_scope_capture.py out/scope/phase2_single.npz
        -> per-channel PSD + coherence(VLDO, Vsense) + spectrum of R

    python analyze_scope_capture.py --alias a.npz b.npz
        -> overlay two captures taken at different sample rates.
           Peaks that MOVE are aliases (Case C). Peaks that STAY are real.

Shunt/INA constants come from docs/PLAN_phase6_ldo_characterization.md:
    100 mOhm shunt + INA296A A1 (10 V/V)  =>  1 V/A
    A0 taps BEFORE the shunt, so  V_sma = V_ldo - I*Rshunt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy import signal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

VLDO_CH, VSENSE_CH = "C1", "C2"

INA_V_PER_A = 1.0     # INA296A A1: 10 V/V x 0.1 Ohm
R_SHUNT = 0.1         # Ohm

# C1 does NOT sit on the LDO output -- it sits on the Portenta A0 pad, which is
# fed through the 10k/10k feedback divider (Firmware_SMASensorHub_PIO/src/
# main.cpp:85 "LDO out via 10k/10k divider"). Bench-measured 2026-07-21 across
# three operating points (V_LDO 0.50 / 0.51 / 2.03 V) as 2.07 / 2.12 / 2.11.
# Without this, every V_LDO from the scope is half its true value.
VLDO_DIVIDER = 2.10

# C2 sits directly on the INA296A output (main.cpp:86, no divider). Measured
# 0.93 V/A against the firmware's own current -- i.e. the FIRMWARE reads ~7%
# high, consistent with the known ADC conversion-duty reference droop on this
# rig. The scope is the trustworthy one here, so keep the nominal 1 V/A.
VSENSE_SCALE = 1.0

NPERSEG = 8192
MIN_SEGMENTS = 8


# Codes per vertical division for the DAT2 byte format. MEASURED on the
# SDS2204X Plus (fw 5.4.1.5.2R2) by comparing a 1M-point record against the
# instrument's own PAVA? MEAN: 10.61 codes at 0.5 V/div = 0.1769 V -> 29.99.
# The driver's historical default of 25.0 reads 20% high.
CODES_PER_DIV = 30.0


def load(path: Path):
    """Re-derive volts from the RAW CODES rather than trusting the stored volts.

    Captures written before the codes/div constant was measured have `_volts`
    computed at 25.0 and are ~20% high. The raw int8 codes plus vdiv/offset are
    always stored, so the correct scaling can be applied after the fact -- which
    means old captures stay usable and the constant is never baked in.
    """
    z = np.load(path, allow_pickle=True)
    fs = float(z[f"{VLDO_CH}_fs"])

    out = []
    for ch in (VLDO_CH, VSENSE_CH):
        if f"{ch}_codes" in z.files:
            codes = np.asarray(z[f"{ch}_codes"], dtype=float)
            vdiv = float(z[f"{ch}_vdiv"])
            offset = float(z[f"{ch}_offset"])
            out.append(codes * (vdiv / CODES_PER_DIV) - offset)
        else:
            out.append(np.asarray(z[f"{ch}_volts"], float))
    return fs, out[0], out[1]


def segments(n):
    return max(1, 2 * n // NPERSEG - 1)


def analyze_one(path: Path, outdir: Path):
    fs, v_ldo, v_sense = load(path)
    n = len(v_ldo)
    n_seg = segments(n)
    coh_ok = n_seg >= MIN_SEGMENTS

    # Undo the A0 divider so V_LDO is the real rail voltage, not the pad voltage.
    v_ldo = v_ldo * VLDO_DIVIDER
    i = v_sense / (INA_V_PER_A * VSENSE_SCALE)

    print(f"{path.name}: fs={fs/1e6:.4f} MSa/s  N={n}  "
          f"span={n/fs*1e3:.3f} ms  bin={fs/NPERSEG:.1f} Hz  segments={n_seg}")

    # R = V/I needs ABSOLUTE volts. On an AC-coupled capture the DC is stripped,
    # so I has ~zero mean, crosses zero, and V/I explodes to +/-inf. Detect that
    # and skip R rather than emitting garbage (or crashing in detrend).
    r_valid = abs(i.mean()) > 5 * i.std() and abs(i.mean()) > 1e-3
    if r_valid:
        v_sma = v_ldo - i * R_SHUNT
        r = v_sma / i
        print(f"  mean V_ldo={v_ldo.mean():.4f} V  I={i.mean():.4f} A  "
              f"R={r.mean():.4f} ohm")
    else:
        r = None
        print(f"  mean V_ldo={v_ldo.mean():+.4f} V  I={i.mean():+.4f} A "
              f"(std {i.std():.4f})")
        print(f"  R = V/I SKIPPED: the current has no usable DC component, so the "
              f"ratio is\n"
              f"    meaningless here. This is expected for an AC-COUPLED capture "
              f"(the DC that\n"
              f"    R depends on is exactly what AC coupling removes). Coherence "
              f"and the PSDs\n"
              f"    below are unaffected and remain valid. For FFT(R), use a "
              f"DC-coupled capture\n"
              f"    (or the deployed-rate H7 data via analyze_r_noise.py).")

    f, p_v = signal.welch(v_ldo, fs, nperseg=NPERSEG, detrend="linear")
    _, p_i = signal.welch(v_sense, fs, nperseg=NPERSEG, detrend="linear")
    p_r = (signal.welch(r - r.mean(), fs, nperseg=NPERSEG, detrend="linear")[1]
           if r is not None else None)
    f_c, coh = signal.coherence(v_ldo, v_sense, fs, nperseg=NPERSEG,
                                detrend="linear")

    if coh_ok:
        print("  coherence by band:")
        for lo, hi in [(1e3, 1e4), (1e4, 1e5), (1e5, 2e5),
                       (2e5, 5.25e5), (5.25e5, fs / 2)]:
            if hi > fs / 2:
                continue
            m = (f_c >= lo) & (f_c < hi)
            if m.any():
                print(f"    {lo/1e3:8.1f}-{hi/1e3:8.1f} kHz : {coh[m].mean():.3f}")
    else:
        print(f"  !! coherence suppressed: {n_seg} segment(s) < {MIN_SEGMENTS}. "
              f"Welch coherence with too few segments is identically 1.0 at every "
              f"bin -- that is 'no data', not 'perfect rejection'.")

    # spurs in the plan's 200-525 kHz window of interest
    band = (f > 50e3) & (f < min(fs / 2, 1e6))
    if band.any():
        pk, _ = signal.find_peaks(10 * np.log10(p_v[band] + 1e-30), prominence=6)
        order = np.argsort(p_v[band][pk])[::-1][:6]
        if len(order):
            print("  top VLDO spurs 50 kHz - Nyquist:")
            for k in pk[order]:
                fk = f[band][k]
                c = np.interp(fk, f_c, coh) if coh_ok else float("nan")
                print(f"    {fk/1e3:9.3f} kHz   coh={c:.3f}")

    fig, ax = plt.subplots(3, 1, figsize=(9, 10))
    ax[0].loglog(f[1:], p_v[1:], lw=0.7, label="VLDO")
    ax[0].loglog(f[1:], p_i[1:], lw=0.7, label="Vsense")
    ax[0].set_title(f"{path.stem} — per-channel PSD (fs={fs/1e6:.3f} MSa/s)")
    ax[0].set_ylabel("V^2/Hz"); ax[0].legend()

    ax[1].semilogx(f_c[1:], coh[1:], lw=0.7,
                   color="tab:blue" if coh_ok else "tab:gray")
    ax[1].set_ylim(0, 1); ax[1].set_ylabel("coherence")
    ax[1].set_title("Coherence(VLDO, Vsense) — ~1 cancels in R, ~0 survives")
    if not coh_ok:
        ax[1].text(0.5, 0.5, f"INVALID — {n_seg} segment(s)",
                   transform=ax[1].transAxes, ha="center", va="center",
                   color="tab:red", fontsize=12, fontweight="bold")

    if p_r is not None:
        ax[2].loglog(f[1:], p_r[1:], color="k", lw=0.7)
    else:
        ax[2].text(0.5, 0.5, "R = V/I not computable on an AC-coupled capture\n"
                             "(no DC current). Coherence above is still valid.",
                   transform=ax[2].transAxes, ha="center", va="center",
                   color="tab:gray", fontsize=10)
        ax[2].set_xscale("log"); ax[2].set_yscale("log")
    ax[2].set_title("Spectrum of R = V_sma / I")
    ax[2].set_xlabel("Hz"); ax[2].set_ylabel("ohm^2/Hz")

    for a in ax:
        a.grid(True, which="both", alpha=0.25)
        a.axvspan(200e3, 525e3, color="tab:orange", alpha=0.12)

    fig.tight_layout()
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / f"scope_diagnosis_{path.stem}.png"
    fig.savefig(p, dpi=140); plt.close(fig)
    print(f"  wrote {p}")


def alias_test(paths, outdir: Path):
    """Overlay two captures on a frequency axis. Movement == aliasing."""
    fig, ax = plt.subplots(figsize=(10, 6))
    peaks = {}
    for path in paths:
        fs, v_ldo, _ = load(Path(path))
        f, p = signal.welch(v_ldo, fs, nperseg=NPERSEG, detrend="linear")
        ax.semilogy(f / 1e3, p, lw=0.7, label=f"{Path(path).stem} "
                                              f"({fs/1e6:.4f} MSa/s)")
        band = (f > 50e3) & (f < fs / 2)
        pk, _ = signal.find_peaks(10 * np.log10(p[band] + 1e-30), prominence=8)
        order = np.argsort(p[band][pk])[::-1][:6]
        peaks[Path(path).stem] = sorted(f[band][pk[order]])
        print(f"{Path(path).stem}: fs={fs/1e6:.4f} MSa/s  top spurs (kHz): "
              f"{[round(float(x)/1e3, 2) for x in peaks[Path(path).stem]]}")

    ax.axvspan(200, 525, color="tab:orange", alpha=0.15,
               label="200-525 kHz suspect band")
    ax.set_xlabel("kHz"); ax.set_ylabel("V^2/Hz")
    ax.set_title("Change-fs alias test — peaks that MOVE are aliases, "
                 "peaks that STAY are real")
    ax.grid(True, which="both", alpha=0.25); ax.legend()
    fig.tight_layout()
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / "alias_test.png"
    fig.savefig(p, dpi=140); plt.close(fig)
    print(f"\nwrote {p}")

    keys = list(peaks)
    if len(keys) == 2:
        a, b = peaks[keys[0]], peaks[keys[1]]
        matched_b = set()
        print("\nverdict per spur (matched within 2 kHz):")
        for fa in a:
            near = [fb for fb in b if abs(fb - fa) < 2e3]
            if near:
                matched_b.update(near)
                print(f"  {fa/1e3:9.2f} kHz  STAYS  -> real, correctly sampled "
                      f"(Case B territory)")
            else:
                print(f"  {fa/1e3:9.2f} kHz  MOVES  -> ALIAS (Case C): a digital "
                      f"filter cannot undo this; needs the analog RC")
        # Spurs seen ONLY in the second capture are equally diagnostic -- they are
        # out-of-band energy that folded to a different place at this rate.
        for fb in b:
            if fb not in matched_b:
                print(f"  {fb/1e3:9.2f} kHz  MOVES  -> ALIAS (Case C), seen only "
                      f"in {keys[1]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--alias", action="store_true",
                    help="treat the inputs as a change-fs pair and overlay them")
    ap.add_argument("--outdir", type=Path,
                    default=Path(__file__).parent / "out")
    args = ap.parse_args(argv)

    if args.alias:
        if len(args.captures) != 2:
            print("!! --alias needs exactly two captures", file=sys.stderr)
            return 2
        alias_test(args.captures, args.outdir)
    else:
        for c in args.captures:
            analyze_one(c, args.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
