"""FIG 2 — a 10 Hz low-pass on both channels, before vs after.

Left: the noise you actually see, in time. Right: what the filter removed.
Both channels get the SAME 10 Hz filter here so the comparison is like-for-like.
"""
import sys, csv, collections
import numpy as np
from scipy import signal
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PATH, PNG = sys.argv[1], sys.argv[2]
FC = 10.0
C_RAW, C_FILT = "#2a78d6", "#eb6834"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#d8d7d2"

raw = collections.defaultdict(list)
with open(PATH) as fh:
    for r in csv.DictReader(fh):
        raw[int(r["src"])].append(
            (int(r["hw_us"]), float(r["value"]), int(r["raw_code"])))


def prep(src):
    d = sorted(raw[src])
    t = np.array([x[0] for x in d], float) * 1e-6
    v = np.array([x[1] for x in d], float)
    c = np.array([x[2] for x in d], np.int64)
    keep = np.r_[True, np.diff(c) != 0]
    td, vd = t[keep], v[keep]
    fs = len(td) / (td[-1] - td[0])
    n = int((td[-1] - td[0]) * fs)
    g = td[0] + np.arange(n) / fs
    return g - g[0], np.interp(g, td, vd) - vd.mean(), fs


def asd(x, fs):
    f, p = signal.welch(x, fs=fs, nperseg=4096, noverlap=2048,
                        window="hann", detrend="linear")
    return f, 1e6 * np.sqrt(p)


plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "text.color": INK,
    "axes.labelcolor": INK2, "axes.edgecolor": GRID,
    "xtick.color": INK2, "ytick.color": INK2, "font.size": 10,
})
fig, ax = plt.subplots(2, 2, figsize=(13.5, 7.6),
                       gridspec_kw={"width_ratios": [1.35, 1]})

for k, (src, name) in enumerate([(1, "Laser (src=1)"), (2, "Load cell (src=2)")]):
    t, v, fs = prep(src)
    sos = signal.butter(4, FC, "low", fs=fs, output="sos")
    vf = signal.sosfiltfilt(sos, v)
    r0, r1 = 1e3 * v.std(), 1e3 * vf.std()

    # --- time, 3 s window ---
    A = ax[k][0]
    m = t <= 3.0
    A.plot(t[m], 1e3 * v[m], color=C_RAW, lw=.9, alpha=.8, label="raw")
    A.plot(t[m], 1e3 * vf[m], color=C_FILT, lw=2.0, label=f"{FC:.0f} Hz low-pass")
    A.set_title(f"{name} — RMS {r0:.3f} → {r1:.3f} mV   ({r0/r1:.1f}× quieter)",
                loc="left", color=INK, fontsize=11.5)
    A.set_ylabel("deviation [mV]")
    A.grid(True, color=GRID, lw=.6, alpha=.7)
    A.set_axisbelow(True)
    for s in ("top", "right"):
        A.spines[s].set_visible(False)
    A.legend(frameon=False, loc="upper right", ncol=2, labelcolor=INK2,
             fontsize=9)
    if k == 1:
        A.set_xlabel("time [s]")

    # --- spectrum ---
    A = ax[k][1]
    f0, a0 = asd(v, fs)
    f1, a1 = asd(vf, fs)
    A.loglog(f0[1:], a0[1:], color=C_RAW, lw=1.0, alpha=.85, label="raw")
    A.loglog(f1[1:], a1[1:], color=C_FILT, lw=1.5, label="filtered")
    A.axvline(FC, color=INK2, ls=":", lw=1.0)
    A.annotate(f"{FC:.0f} Hz", xy=(FC, A.get_ylim()[1]), xytext=(4, -12),
               textcoords="offset points", fontsize=9, color=INK2)
    A.set_title("what the filter removed", loc="left", color=INK, fontsize=11.5)
    A.set_ylabel(r"ASD [$\mu$V/$\sqrt{\mathrm{Hz}}$]")
    A.grid(True, which="both", color=GRID, lw=.6, alpha=.5)
    A.set_axisbelow(True)
    for s in ("top", "right"):
        A.spines[s].set_visible(False)
    A.legend(frameon=False, loc="lower left", ncol=2, labelcolor=INK2,
             fontsize=9)
    if k == 1:
        A.set_xlabel("frequency [Hz]")
    print(f"{name:20s} RMS {r0:7.3f} -> {r1:7.3f} mV  ({r0/r1:.2f}x)")

fig.suptitle(f"{FC:.0f} Hz low-pass — before vs after   "
             f"(rise time ≈ {0.35/FC*1e3:.0f} ms)",
             x=0.008, ha="left", fontsize=13.5, color=INK, weight="bold")
fig.tight_layout(rect=[0, 0, 1, 0.955])
fig.savefig(PNG, dpi=135, facecolor=SURFACE)
print(f"-> {PNG}")
