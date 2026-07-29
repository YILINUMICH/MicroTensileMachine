"""Baseline vs shorted-input: where did the load-cell noise go?

Splits the load channel into:
  shorted  = amplifier + excitation + everything downstream of the input
  removed  = sensor + cable  (quadrature difference, if uncorrelated)

The laser is the CONTROL: it shares the ADC, reference and supply but not the
load path, so it flags any change in the ambient conditions between captures.
"""
import sys, csv, collections
import numpy as np
from scipy import signal
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE, SHORT, PNG = sys.argv[1], sys.argv[2], sys.argv[3]
C_A, C_B = "#2a78d6", "#eb6834"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#d8d7d2"


def load(path):
    raw = collections.defaultdict(list)
    with open(path) as fh:
        for r in csv.DictReader(fh):
            raw[int(r["src"])].append(
                (int(r["hw_us"]), float(r["value"]), int(r["raw_code"])))
    out = {}
    for src, d in raw.items():
        d.sort()
        t = np.array([x[0] for x in d], float) * 1e-6
        v = np.array([x[1] for x in d], float)
        c = np.array([x[2] for x in d], np.int64)
        keep = np.r_[True, np.diff(c) != 0]
        td, vd = t[keep], v[keep]
        fs = len(td) / (td[-1] - td[0])
        n = int((td[-1] - td[0]) * fs)
        g = td[0] + np.arange(n) / fs
        out[src] = (np.interp(g, td, vd) - vd.mean(), fs)
    return out


def asd(x, fs):
    f, p = signal.welch(x, fs=fs, nperseg=4096, noverlap=2048,
                        window="hann", detrend="linear")
    return f, np.sqrt(p), p


A, B = load(BASE), load(SHORT)
BANDS = [(0, 1), (1, 10), (10, 50), (50, 100), (100, 200)]

print("=" * 74)
print("BASELINE (sensor connected)  vs  SHORTED (SIG+/SIG- shorted at amp)")
print("=" * 74)

for src, name in [(2, "LOAD CELL"), (1, "LASER  (control)")]:
    va, fa = A[src]
    vb, fb = B[src]
    print(f"\n--- {name} ---")
    print(f"  baseline RMS {1e3*va.std():7.3f} mV     "
          f"shorted RMS {1e3*vb.std():7.3f} mV     "
          f"ratio {va.std()/vb.std():5.2f}x")
    if src == 2:
        d2 = va.var() - vb.var()
        if d2 > 0:
            print(f"  -> amplifier+excitation {1e3*vb.std():6.3f} mV "
                  f"({100*vb.var()/va.var():4.1f}% of variance)")
            print(f"  -> sensor+cable         {1e3*np.sqrt(d2):6.3f} mV "
                  f"({100*d2/va.var():4.1f}% of variance)")
    f1, a1, p1 = asd(va, fa)
    f2, a2, p2 = asd(vb, fb)
    print(f"  {'band [Hz]':>12}  {'base':>8}  {'short':>8}  {'ratio':>6}")
    for lo, hi in BANDS:
        m1 = (f1 >= lo) & (f1 < hi)
        m2 = (f2 >= lo) & (f2 < hi)
        r1 = np.sqrt(np.trapezoid(p1[m1], f1[m1]))
        r2 = np.sqrt(np.trapezoid(p2[m2], f2[m2]))
        print(f"  {lo:5.0f}-{hi:5.0f}  {1e3*r1:8.3f}  {1e3*r2:8.3f}  "
              f"{r1/max(r2,1e-12):6.2f}x")

# ---------------- figure ----------------
plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "text.color": INK,
    "axes.labelcolor": INK2, "axes.edgecolor": GRID,
    "xtick.color": INK2, "ytick.color": INK2, "font.size": 10})
fig, ax = plt.subplots(1, 2, figsize=(13.5, 5.4))

for k, (src, name) in enumerate([(2, "Load cell (src=2)"),
                                 (1, "Laser (src=1) — control")]):
    va, fa = A[src]
    vb, fb = B[src]
    f1, a1, _ = asd(va, fa)
    f2, a2, _ = asd(vb, fb)
    ax[k].loglog(f1[1:], 1e6 * a1[1:], color=C_A, lw=1.1,
                 label=f"sensor connected  ({1e3*va.std():.2f} mV RMS)")
    ax[k].loglog(f2[1:], 1e6 * a2[1:], color=C_B, lw=1.1,
                 label=f"input shorted  ({1e3*vb.std():.2f} mV RMS)")
    ax[k].set_title(name, loc="left", color=INK, fontsize=11.5)
    ax[k].set_xlabel("frequency [Hz]")
    ax[k].set_ylabel(r"ASD [$\mu$V/$\sqrt{\mathrm{Hz}}$]")
    ax[k].grid(True, which="both", color=GRID, lw=.6, alpha=.5)
    ax[k].set_axisbelow(True)
    for s in ("top", "right"):
        ax[k].spines[s].set_visible(False)
    ax[k].legend(frameon=False, loc="lower left", labelcolor=INK2, fontsize=9)

fig.suptitle("Shorted-input test — is the load-cell noise before or after the amplifier?",
             x=0.008, ha="left", fontsize=13, color=INK, weight="bold")
fig.tight_layout(rect=[0, 0, 1, 0.93])
fig.savefig(PNG, dpi=135, facecolor=SURFACE)
print(f"\n-> {PNG}")
