"""Before/after filtering — time domain and spectrum, both channels.

The two channels need DIFFERENT treatments, and that is the point of the figure:

  laser  discrete 65.76 Hz tone + harmonic  -> NOTCH. Keeps full bandwidth.
  load   broadband, no tones                -> LOW-PASS. Costs bandwidth.
"""
import sys, csv, collections
import numpy as np
from scipy import signal
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PATH = sys.argv[1]
PNG = sys.argv[2]

# palette: reference instance, categorical slots 1 & 2, light mode, fixed order
C_RAW, C_FILT = "#2a78d6", "#eb6834"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#d8d7d2"

NOTCH_HZ = (65.76, 131.52)     # measured tone + 2nd harmonic
LP_HZ = 20.0                   # load-cell low-pass

raw = collections.defaultdict(list)
with open(PATH) as fh:
    for r in csv.DictReader(fh):
        raw[int(r["src"])].append(
            (int(r["hw_us"]), float(r["value"]), int(r["raw_code"])))


def prep(src):
    d = sorted(raw[src])
    t = np.array([x[0] for x in d], float) * 1e-6
    v = np.array([x[1] for x in d], float)
    code = np.array([x[2] for x in d], np.int64)
    keep = np.r_[True, np.diff(code) != 0]          # drop ZOH duplicates
    td, vd = t[keep], v[keep]
    fs = len(td) / (td[-1] - td[0])
    n = int((td[-1] - td[0]) * fs)
    grid = td[0] + np.arange(n) / fs
    vu = np.interp(grid, td, vd)
    return grid - grid[0], vu - vu.mean(), fs


def psd(x, fs):
    f, p = signal.welch(x, fs=fs, nperseg=4096, noverlap=2048,
                        window="hann", detrend="linear")
    return f, np.sqrt(p)


t1, v1, fs1 = prep(1)
t2, v2, fs2 = prep(2)

# laser: notch the two instrumental lines, full bandwidth retained
f1 = v1.copy()
for hz in NOTCH_HZ:
    b, a = signal.iirnotch(hz, Q=30.0, fs=fs1)
    f1 = signal.filtfilt(b, a, f1)

# load: broadband -> only a low-pass helps
sos = signal.butter(4, LP_HZ, "low", fs=fs2, output="sos")
f2 = signal.sosfiltfilt(sos, v2)

rows = [("Laser  (src=1)", t1, v1, f1, fs1,
         f"notch {NOTCH_HZ[0]:.1f} + {NOTCH_HZ[1]:.1f} Hz"),
        ("Load cell  (src=2)", t2, v2, f2, fs2,
         f"low-pass {LP_HZ:.0f} Hz")]

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "text.color": INK, "axes.labelcolor": INK2, "axes.edgecolor": GRID,
    "xtick.color": INK2, "ytick.color": INK2,
    "font.size": 9.5, "axes.titlesize": 10.5,
})
fig, ax = plt.subplots(2, 2, figsize=(13.5, 7.6))

for r, (name, t, v, vf, fs, how) in enumerate(rows):
    rms_r, rms_f = 1e3 * v.std(), 1e3 * vf.std()
    gain = rms_r / rms_f

    # ---- time domain, 2 s window ----
    a = ax[r][0]
    m = t <= 2.0
    a.plot(t[m], 1e3 * v[m], color=C_RAW, lw=1.0, alpha=.85, label="raw")
    a.plot(t[m], 1e3 * vf[m], color=C_FILT, lw=1.6, label="filtered")
    a.set_title(f"{name} — time domain    "
                f"RMS {rms_r:.3f} → {rms_f:.3f} mV  ({gain:.1f}× better)",
                loc="left", color=INK)
    a.set_ylabel("deviation [mV]")
    if r == 1:
        a.set_xlabel("time [s]")
    a.grid(True, color=GRID, lw=.6, alpha=.7)
    a.set_axisbelow(True)
    for s in ("top", "right"):
        a.spines[s].set_visible(False)
    a.legend(frameon=False, loc="upper right", ncol=2, labelcolor=INK2)

    # ---- spectrum ----
    a = ax[r][1]
    fr, ar = psd(v, fs)
    ff, af = psd(vf, fs)
    a.loglog(fr[1:], 1e6 * ar[1:], color=C_RAW, lw=1.0, alpha=.85, label="raw")
    a.loglog(ff[1:], 1e6 * af[1:], color=C_FILT, lw=1.4, label="filtered")
    a.set_title(f"{name} — spectrum    {how}", loc="left", color=INK)
    a.set_ylabel(r"ASD [$\mu$V/$\sqrt{\mathrm{Hz}}$]")
    if r == 1:
        a.set_xlabel("frequency [Hz]")
    a.grid(True, which="both", color=GRID, lw=.6, alpha=.55)
    a.set_axisbelow(True)
    for s in ("top", "right"):
        a.spines[s].set_visible(False)
    a.legend(frameon=False, loc="lower left", ncol=2, labelcolor=INK2)
    if r == 0:                      # direct-label the tone we removed
        i = int(np.argmin(abs(fr - NOTCH_HZ[0])))
        a.annotate(f"{NOTCH_HZ[0]:.1f} Hz\n128× floor",
                   xy=(fr[i], 1e6 * ar[i]), xytext=(9, -4),
                   textcoords="offset points", fontsize=8.5, color=INK2,
                   ha="left", va="top")

fig.suptitle("Sensor noise at rest — before and after filtering",
             x=0.008, ha="left", fontsize=13, color=INK, weight="bold")
fig.tight_layout(rect=[0, 0, 1, 0.955])
fig.savefig(PNG, dpi=135, facecolor=SURFACE)

print(f"laser  RMS {1e3*v1.std():7.3f} -> {1e3*f1.std():7.3f} mV   "
      f"({1e3*v1.std()/(1e3*f1.std()):.2f}x)   bandwidth KEPT (notch)")
print(f"load   RMS {1e3*v2.std():7.3f} -> {1e3*f2.std():7.3f} mV   "
      f"({1e3*v2.std()/(1e3*f2.std()):.2f}x)   bandwidth {LP_HZ:.0f} Hz (low-pass)")
print(f"\n-> {PNG}")
