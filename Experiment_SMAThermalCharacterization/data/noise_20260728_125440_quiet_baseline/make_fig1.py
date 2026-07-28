"""FIG 1 — amplitude vs frequency, LINEAR axis, so tones are readable by position.

Amplitude spectrum (mV), not PSD: a peak's height IS the sine amplitude in mV,
so you can read "that tone is 0.8 mV" straight off the axis. Linear frequency
because the job is identifying WHERE the lines are.
"""
import sys, csv, collections
import numpy as np
from scipy import signal
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PATH, PNG = sys.argv[1], sys.argv[2]
C1, C2 = "#2a78d6", "#eb6834"
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
    return np.interp(g, td, vd) - vd.mean(), fs


def amp_spectrum(x, fs, nper=8192):
    """Averaged single-sided AMPLITUDE spectrum in mV."""
    w = np.hanning(nper)
    corr = 2.0 / w.sum()                     # Hann amplitude correction
    segs = []
    for i in range(0, len(x) - nper + 1, nper // 2):
        segs.append(np.abs(np.fft.rfft((x[i:i + nper] -
                                        x[i:i + nper].mean()) * w)) * corr)
    f = np.fft.rfftfreq(nper, 1 / fs)
    return f, 1e3 * np.mean(segs, axis=0)    # -> mV


plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "text.color": INK,
    "axes.labelcolor": INK2, "axes.edgecolor": GRID,
    "xtick.color": INK2, "ytick.color": INK2, "font.size": 10,
})
fig, ax = plt.subplots(2, 1, figsize=(13, 8))

for k, (src, name, col) in enumerate([(1, "Laser (src=1)", C1),
                                      (2, "Load cell (src=2)", C2)]):
    v, fs = prep(src)
    f, a = amp_spectrum(v, fs)
    A = ax[k]
    A.plot(f, a, color=col, lw=1.0)
    A.set_xlim(0, fs / 2)
    A.set_ylabel("amplitude [mV]")
    A.grid(True, color=GRID, lw=.6, alpha=.7)
    A.set_axisbelow(True)
    for s in ("top", "right"):
        A.spines[s].set_visible(False)
    A.set_xticks(np.arange(0, fs / 2 + 1, 10), minor=True)

    # label the significant lines
    med = signal.medfilt(a, 101)
    pk, _ = signal.find_peaks(a / np.maximum(med, 1e-12), height=6.0, distance=8)
    pk = pk[f[pk] > 1.0]
    pk = pk[np.argsort(a[pk])[::-1]][:6]
    # stagger labels on peaks that sit close together, else they collide
    prev_f, lift = -1e9, 0
    for i in sorted(pk, key=lambda j: f[j]):
        lift = 26 if (f[i] - prev_f) < 18 and lift == 6 else 6
        prev_f = f[i]
        A.annotate(f"{f[i]:.1f} Hz\n{a[i]:.3f} mV",
                   xy=(f[i], a[i]), xytext=(0, lift),
                   textcoords="offset points",
                   ha="center", va="bottom", fontsize=8.5, color=INK,
                   arrowprops=dict(arrowstyle="-", color=INK2, lw=.7))
    if len(pk):
        A.set_ylim(0, a[pk].max() * 1.32)
        sub = (f"dominant line {f[pk[0]]:.2f} Hz — "
               f"{a[pk[0]]/np.median(a[f > 100]):.0f}× the >100 Hz floor")
    else:
        A.set_ylim(0, np.percentile(a[f > 1], 99.9) * 1.3)
        sub = "no discrete lines — broadband"
    A.set_title(f"{name}    {sub}", loc="left", color=INK, fontsize=11.5)
    if k == 1:
        A.set_xlabel("frequency [Hz]   (linear — Nyquist 200 Hz)")

fig.suptitle("Where is the noise?  Amplitude spectrum at rest",
             x=0.008, ha="left", fontsize=13.5, color=INK, weight="bold")
fig.tight_layout(rect=[0, 0, 1, 0.955])
fig.savefig(PNG, dpi=135, facecolor=SURFACE)
print(f"-> {PNG}")
