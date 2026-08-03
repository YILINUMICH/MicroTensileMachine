"""Noise spectrum of laser (src=1) and load (src=2) at rest.

Two things must be handled or the spectrum is wrong:

  1. hw_us is the time base, NOT host timestamps (Windows scheduler jitter
     would smear everything).
  2. The stream carries ~19% zero-order-hold DUPLICATES: M4 polls each ADC
     every 2 ms (~500 Hz) but the ADS1263 converts at 400 SPS, so some reads
     re-fetch the same data register. Spectrally that is a held sample, not a
     new one. Deduplicating on raw_code recovers the true ~400 Hz conversion
     sequence -> real Nyquist 200 Hz.
"""
import sys, csv, collections
import numpy as np
from scipy import signal

PATH = sys.argv[1]
PNG  = sys.argv[2] if len(sys.argv) > 2 else None
NAME = {1: "laser (src=1)", 2: "load cell (src=2)"}

raw = collections.defaultdict(list)
with open(PATH) as fh:
    for r in csv.DictReader(fh):
        raw[int(r["src"])].append(
            (int(r["hw_us"]), float(r["value"]), int(r["raw_code"])))

print("=" * 78)
print("NOISE SPECTRUM — rig at rest")
print("=" * 78)

results = {}
for src in sorted(raw):
    d = sorted(raw[src])
    t = np.array([x[0] for x in d], float) * 1e-6
    v = np.array([x[1] for x in d], float)
    code = np.array([x[2] for x in d], np.int64)

    # --- dedupe: keep only rows where the ADC produced a NEW conversion -----
    keep = np.r_[True, np.diff(code) != 0]
    td, vd = t[keep], v[keep]
    dup_pct = 100.0 * (1 - keep.mean())

    fs_stream = len(t) / (t[-1] - t[0])
    fs_true = len(td) / (td[-1] - td[0])

    # --- uniform resample onto the true conversion grid ---------------------
    n = int((td[-1] - td[0]) * fs_true)
    grid = td[0] + np.arange(n) / fs_true
    vu = np.interp(grid, td, vd)
    vu = vu - vu.mean()

    # --- Welch PSD ----------------------------------------------------------
    nper = 4096
    f, pxx = signal.welch(vu, fs=fs_true, nperseg=nper,
                          noverlap=nper // 2, window="hann", detrend="linear")
    asd = np.sqrt(pxx)                       # V/sqrt(Hz)

    print(f"\n--- {NAME[src]} ---")
    print(f"  streamed {fs_stream:6.1f} Hz | ZOH duplicates {dup_pct:4.1f}% | "
          f"true conversion {fs_true:6.1f} Hz -> Nyquist {fs_true/2:5.1f} Hz")
    print(f"  mean {v.mean():+.6f} V   total RMS {1e3*vu.std():7.3f} mV   "
          f"p-p {1e3*(vu.max()-vu.min()):7.3f} mV")

    # --- band breakdown (RMS by integrating the PSD) ------------------------
    bands = [(0.0, 1.0), (1.0, 10.0), (10.0, 50.0),
             (50.0, 100.0), (100.0, fs_true / 2)]
    print(f"  {'band [Hz]':>14}  {'RMS [mV]':>9}  {'% of variance':>13}")
    tot_var = np.trapezoid(pxx, f)
    for lo, hi in bands:
        m = (f >= lo) & (f < hi)
        var = np.trapezoid(pxx[m], f[m]) if m.sum() > 1 else 0.0
        print(f"  {lo:6.1f}-{hi:6.1f}  {1e3*np.sqrt(var):9.3f}  "
              f"{100*var/tot_var:12.1f}%")

    # --- discrete peaks above the local median floor ------------------------
    med = signal.medfilt(asd, 51)
    ratio = asd / np.maximum(med, 1e-20)
    pk, _ = signal.find_peaks(ratio, height=4.0, distance=5)
    pk = pk[f[pk] > 0.5]
    pk = pk[np.argsort(ratio[pk])[::-1]][:8]
    if len(pk):
        print(f"  discrete tones (>4x local floor):")
        for i in sorted(pk, key=lambda j: f[j]):
            print(f"    {f[i]:7.2f} Hz   {1e3*asd[i]:8.4f} mV/sqrt(Hz)   "
                  f"{ratio[i]:5.1f}x floor")
    else:
        print("  discrete tones: none above 4x the local floor (broadband)")

    hf = (f > 100)
    print(f"  broadband floor above 100 Hz: "
          f"{1e6*np.median(asd[hf]):8.2f} uV/sqrt(Hz)")
    results[src] = (f, asd, fs_true)

if PNG:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
    for k, src in enumerate(sorted(results)):
        f, asd, fs = results[src]
        ax[k].loglog(f[1:], 1e6 * asd[1:], lw=0.8)
        ax[k].set_title(f"{NAME[src]} — amplitude spectral density "
                        f"(fs={fs:.0f} Hz)")
        ax[k].set_ylabel(r"ASD [$\mu$V/$\sqrt{Hz}$]")
        ax[k].grid(True, which="both", alpha=0.3)
        for hz in (50, 60):
            ax[k].axvline(hz, color="r", ls=":", lw=0.8, alpha=0.6)
    ax[-1].set_xlabel("frequency [Hz]")
    fig.tight_layout()
    fig.savefig(PNG, dpi=130)
    print(f"\nplot -> {PNG}")
