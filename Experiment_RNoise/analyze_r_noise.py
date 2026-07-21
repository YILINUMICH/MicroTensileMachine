#!/usr/bin/env python3
"""Diagnose SMA resistance (R = V/I) self-sensing noise from deployed-rate H7 captures.

Implements PHASE 4 / PHASE 5 of sma_resistance_noise_plan.md against data the
production recorder already writes -- no bench time required.

The H7 streams sma_v (src=3), sma_i (src=4) and sma_r (src=5) at ~980 Hz, which
is the plan's "Mode B -- deployed rate" capture. That is enough to answer
decision steps 1 (coherence) and 3 (FFT(R)) directly on the shipped signal path.

Step 2 (change-fs alias test) CANNOT be answered offline -- it needs two captures
at different sample rates. See STATUS.md.

Usage:
    python analyze_r_noise.py <session_dir> [<session_dir> ...] [--outdir DIR]

Each session_dir is a recorder session containing h7.csv.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# H7 sample_ring src IDs -- see Firmware_SMASensorHub_PIO/src/sample_ring.h
SRC_SMA_V, SRC_SMA_I, SRC_SMA_R = 3, 4, 5

NPERSEG = 4096          # bin width ~= 0.24 Hz at 980 Hz
THERMAL_HZ = 10.0       # upper edge of the band that carries real SMA signal

# Welch coherence with too few averaged segments is biased hard toward 1.0 --
# with a single segment it is *identically* 1 at every bin, which reads as
# "perfect common-mode rejection" when it is really "no data". Refuse to report
# coherence below this many segments (50% overlap => n_seg ~= 2*N/nperseg - 1).
MIN_SEGMENTS = 8


def load_longest_run(h7_csv: Path):
    """Return (fs, v, i, dur) for the longest gap-free SMA run, on a uniform grid.

    Time base is hw_us (the firmware clock), NOT host_timestamp_s -- host
    timestamps carry USB scheduling jitter that would smear the spectrum.
    """
    df = pd.read_csv(h7_csv, usecols=["src", "value", "hw_us"])
    sma = df[df.src.isin([SRC_SMA_V, SRC_SMA_I, SRC_SMA_R])]
    if sma.empty:
        raise ValueError(f"{h7_csv}: no sma_v/sma_i/sma_r rows (src 3/4/5)")

    piv = sma.pivot_table(index="hw_us", columns="src", values="value",
                          aggfunc="first").dropna()
    piv.columns = ["v", "i", "r"]
    t = piv.index.values / 1e6

    # split on gaps > 5x the median sample interval, keep the longest run
    dt = np.diff(t)
    breaks = np.where(dt > 5 * np.median(dt))[0]
    runs = np.split(np.arange(len(t)), breaks + 1)
    run = max(runs, key=len)
    t = t[run]
    v = np.asarray(piv.v.values, dtype=float)[run]
    i = np.asarray(piv.i.values, dtype=float)[run]

    fs = 1.0 / np.median(np.diff(t))
    grid = np.arange(t[0], t[-1], 1.0 / fs)
    return fs, np.interp(grid, t, v), np.interp(grid, t, i), grid[-1] - grid[0]


def relative_psd(x, fs):
    """PSD of the *fractional* fluctuation, so V, I and R are directly comparable."""
    return signal.welch(x / np.mean(x), fs, nperseg=NPERSEG, detrend="linear")


def band_rms(f, pxx, lo, hi):
    """Integrated relative RMS over [lo, hi) Hz."""
    m = (f >= lo) & (f < hi)
    return float(np.sqrt(np.trapezoid(pxx[m], f[m])))


def analyze(h7_csv: Path):
    fs, v, i, dur = load_longest_run(h7_csv)
    r = v / i

    f, p_v = relative_psd(v, fs)
    _, p_i = relative_psd(i, fs)
    _, p_r = relative_psd(r, fs)
    f_c, coh = signal.coherence(v, i, fs, nperseg=NPERSEG, detrend="linear")

    n_seg = max(1, 2 * len(v) // NPERSEG - 1)
    coh_ok = n_seg >= MIN_SEGMENTS

    bands = [(0.5, THERMAL_HZ), (THERMAL_HZ, 100.0), (100.0, fs / 2)]
    rows = []
    for lo, hi in bands:
        rv, ri, rr = (band_rms(f, p, lo, hi) for p in (p_v, p_i, p_r))
        m = (f_c >= lo) & (f_c < hi)
        rows.append(dict(lo=lo, hi=hi, v=rv, i=ri, r=rr,
                         quad=float(np.hypot(rv, ri)),
                         coh=float(coh[m].mean()),
                         reject=float(np.hypot(rv, ri) / rr) if rr else np.inf))

    return dict(name=h7_csv.parent.name, fs=fs, n=len(v), dur=dur,
                v=v, i=i, r=r, f=f, p_v=p_v, p_i=p_i, p_r=p_r,
                f_c=f_c, coh=coh, bands=rows,
                n_seg=n_seg, coh_ok=coh_ok)


def filter_r(r, fs, corner_hz=10.0, order=4):
    """INTERIM band-aid: low-pass R to suppress the ALIASED supply tones.

    Why this works despite aliasing being 'irreversible': the LDO's 24.414 kHz
    tone and its harmonics fold to 96 / 288 / 384 / 480 Hz at the deployed
    ~980 Hz rate -- ALL above the 0.5-10 Hz thermal signal band. Folded energy is
    only unrecoverable when it lands *inside* the band of interest. It does not
    here, so a 10 Hz low-pass removes it and costs no real signal.

    !! THIS IS LUCK, NOT DESIGN. The fold position depends on the ratio of the
    tone to fs. A drift of only 0.39% in fs (3.84 Hz of 980.4) puts the 24.414
    kHz fundamental at DC -- inside the thermal band, where NOTHING can remove
    it, and where it would masquerade as a slow real resistance change. The H7's
    rate is a software loop, not crystal-locked, so this margin is not
    guaranteed. Treat as a temporary measurement patch only; the durable fix is
    to stop the LDO oscillating and add the analog anti-alias RC (see STATUS.md).

    Use filtfilt (zero-phase) so R is not delayed relative to the other streams.
    """
    from scipy.signal import butter, filtfilt
    nyq = fs / 2.0
    if not (0 < corner_hz < nyq):
        raise ValueError(f"corner {corner_hz} Hz outside (0, {nyq}) for fs={fs}")
    b, a = butter(order, corner_hz / nyq)
    return filtfilt(b, a, r)


def alias_frequencies(tones_hz, fs):
    """Where each out-of-band tone lands after sampling at fs. <10 Hz == trouble."""
    out = []
    for f in tones_hz:
        a = abs(f - round(f / fs) * fs)
        if a > fs / 2:
            a = fs - a
        out.append((f, a, a < THERMAL_HZ))
    return out


def lpf_tradeoff(res, corners=(None, 50, 20, 10, 5, 2)):
    """sigma(R) vs low-pass corner. 'Truth' = the 0.5 Hz thermal trajectory."""
    fs, r = res["fs"], res["r"]
    b0, a0 = signal.butter(4, 0.5 / (fs / 2))
    out = []
    for fc in corners:
        x = r
        if fc:
            b, a = signal.butter(4, fc / (fs / 2))
            x = signal.filtfilt(b, a, x)
        sd = float(np.std(x - signal.filtfilt(b0, a0, x)))
        out.append((fc, sd, sd / np.mean(r) * 1e6))
    return out


def plot(res, outdir: Path):
    """The plan's deliverable P4: PSD + coherence + FFT(R)."""
    f, fs = res["f"], res["fs"]
    fig, ax = plt.subplots(3, 1, figsize=(9, 10), sharex=True)

    ax[0].loglog(f[1:], res["p_v"][1:], label="V", lw=0.8)
    ax[0].loglog(f[1:], res["p_i"][1:], label="I", lw=0.8)
    ax[0].loglog(f[1:], res["p_r"][1:], label="R = V/I", lw=0.9, color="k")
    ax[0].set_title(f"{res['name']} -- relative PSD "
                    f"(fs={fs:.0f} Hz, {res['dur']:.0f} s)")
    ax[0].set_ylabel("PSD [1/Hz, fractional]")
    ax[0].legend()

    ax[1].semilogx(res["f_c"][1:], res["coh"][1:], lw=0.8,
                   color="tab:blue" if res["coh_ok"] else "tab:gray")
    ax[1].set_ylim(0, 1)
    ax[1].set_ylabel("coherence")
    ax[1].set_title("Coherence(V, I)  -- ~1 cancels in R, ~0 survives")
    if not res["coh_ok"]:
        ax[1].text(0.5, 0.5, f"INVALID -- only {res['n_seg']} Welch segment(s)\n"
                             f"coherence is biased to 1.0, do not interpret",
                   transform=ax[1].transAxes, ha="center", va="center",
                   color="tab:red", fontsize=11, fontweight="bold")

    ax[2].loglog(f[1:], res["p_r"][1:], color="k", lw=0.9)
    ax[2].set_title("Spectrum of R  (what a low-pass would act on)")
    ax[2].set_xlabel("Hz")
    ax[2].set_ylabel("PSD [1/Hz, fractional]")

    for a in ax:
        a.axvline(THERMAL_HZ, ls="--", c="tab:red", lw=0.8)
        a.grid(True, which="both", alpha=0.25)

    fig.tight_layout()
    path = outdir / f"R_noise_diagnosis_{res['name']}.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--outdir", type=Path, default=Path(__file__).parent / "out")
    args = ap.parse_args(argv)
    args.outdir.mkdir(parents=True, exist_ok=True)

    for sess in args.sessions:
        h7 = sess / "h7.csv" if sess.is_dir() else sess
        if not h7.exists():
            print(f"!! {h7} not found, skipping", file=sys.stderr)
            continue

        res = analyze(h7)
        print(f"\n=== {res['name']} ===")
        print(f"fs={res['fs']:.1f} Hz  N={res['n']}  dur={res['dur']:.1f} s  "
              f"R_mean={np.mean(res['r']):.3f} ohm")
        print("  NOTE: V and I relRMS are dominated by the DRIVE waveform (the "
              "fire/cool\n        cycle and its harmonics -- the sinc lobes in the "
              "PSD plot), not by noise.\n        'reject' is therefore mostly "
              "drive rejection, which is expected and not\n        the interesting "
              "result. The column to read is R.")
        print(f"{'band':>16}  {'relRMS V':>9} {'I':>9} {'R':>9} "
              f"{'quad(V,I)':>10} {'reject':>7} {'coh':>6}")
        for b in res["bands"]:
            coh_s = f"{b['coh']:6.3f}" if res["coh_ok"] else "   n/a"
            print(f"{b['lo']:6.1f}-{b['hi']:6.0f} Hz  "
                  f"{b['v']*1e6:9.0f} {b['i']*1e6:9.0f} {b['r']*1e6:9.0f} "
                  f"{b['quad']*1e6:10.0f} {b['reject']:6.1f}x {coh_s}   [ppm]")
        if not res["coh_ok"]:
            print(f"  !! coherence suppressed: only {res['n_seg']} Welch segment(s), "
                  f"need >={MIN_SEGMENTS}. Too-short records bias coherence to 1.0.\n"
                  f"     Capture a longer continuous run to use this session for "
                  f"decision step 1.")

        # Where the measured LDO tones fold at THIS session's rate. If any lands
        # under 10 Hz the low-pass band-aid silently stops working.
        LDO_TONES = [24414.06, 73242.2, 97656.2, 122070.3, 460205.0]
        folds = alias_frequencies(LDO_TONES, res["fs"])
        print("\n  aliasing of the measured LDO tones at this session's fs:")
        for f, a, bad in folds:
            flag = "  << IN THERMAL BAND - UNREMOVABLE" if bad else ""
            print(f"    {f/1e3:9.3f} kHz -> {a:7.1f} Hz{flag}")
        if any(bad for _, _, bad in folds):
            print("    !! at least one tone folds into the signal band; the low-pass\n"
                  "       band-aid CANNOT remove it and it will look like real drift.")
        else:
            print(f"    all folds are above {THERMAL_HZ:g} Hz -> the low-pass below "
                  f"does remove them")

        print("\n  low-pass trade-off:")
        for fc, sd, ppm in lpf_tradeoff(res):
            tag = "raw" if fc is None else f"LPF {fc:>2} Hz"
            print(f"    {tag:10s} sigma_R = {sd*1e3:7.3f} mohm ({ppm:6.0f} ppm)")

        print(f"\n  wrote {plot(res, args.outdir)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
