"""
TestC_Analysis.py
Analyze and plot ADS1263 Test C (AC Signal Capture) data.

Usage:
    1. Run TestC_AC_Capture.ino, type 'a'/'b'/'c'/'d'
    2. Copy the CSV block between CSV_START and CSV_END from Serial Monitor
    3. Paste into the DATA string below (or load from file)
    4. Run: python TestC_Analysis.py

Or load from file:
    python TestC_Analysis.py --file data.csv --freq 10 --label "10Hz @ 400SPS"
"""

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import sys
import io

# ── Configuration ──────────────────────────────────────────────────────────
# Paste CSV data here (index,time_ms,voltage_V), or use --file argument
SAMPLE_DATA = """
index,time_ms,voltage_V
3959,19803,1.259540
3960,19808,0.948531
3961,19813,0.662403
3962,19818,0.428283
3963,19823,0.269336
3964,19828,0.201939
3965,19833,0.231595
3966,19838,0.356374
3967,19843,0.563282
3968,19848,0.832489
3969,19853,1.137261
3970,19858,1.448305
3971,19863,1.735129
3972,19868,1.968896
3973,19873,2.127416
3974,19878,2.195361
3975,19883,2.165547
3976,19888,2.040850
3977,19893,1.834169
3978,19898,1.565329
3979,19903,1.260223
3980,19908,0.949073
3981,19913,0.662665
3982,19918,0.428622
3983,19923,0.269607
3984,19928,0.201932
3985,19933,0.231517
3986,19938,0.356049
3987,19943,0.562912
3988,19948,0.832098
3989,19953,1.136863
3990,19958,1.447867
3991,19963,1.734601
3992,19968,1.968909
3993,19973,2.127415
3994,19978,2.195211
3995,19983,2.165418
3996,19988,2.041267
3997,19993,1.834368
3998,19998,1.565797
3999,20003,1.260480
"""
# Expected signal parameters
EXPECTED_VPP   = 2.0    # V
EXPECTED_DC    = 1.2    # V
PASS_AMP_TOL   = 0.020  # ±20 mV
VREF           = 2.5    # V (internal reference)
FSR            = 5.0    # V full-scale range

DARK  = '#0f1117'
PANEL = '#1a1d27'
GRID  = '#2a2d3a'
TXT   = '#e0e0e0'
BLUE  = '#4fa3e0'
PASS  = '#00c896'
FAIL  = '#ff4f4f'
AMBER = '#f0a030'

# ── Load data ──────────────────────────────────────────────────────────────
def load_data(csv_str):
    lines = [l.strip() for l in csv_str.strip().splitlines()
             if l.strip() and not l.startswith('index')]
    if not lines:
        return None, None, None
    idx, t_ms, v = [], [], []
    for line in lines:
        parts = line.split(',')
        if len(parts) < 3:
            continue
        try:
            idx.append(int(parts[0]))
            t_ms.append(float(parts[1]))
            v.append(float(parts[2]))
        except ValueError:
            continue
    return np.array(idx), np.array(t_ms) / 1000.0, np.array(v)

# ── FFT analysis ───────────────────────────────────────────────────────────
def analyze(t_s, v, label, sig_freq):
    n = len(v)
    dt_mean = np.mean(np.diff(t_s))
    fs = 1.0 / dt_mean          # effective sample rate

    vpp     = v.max() - v.min()
    dc      = (v.max() + v.min()) / 2.0
    t_total = t_s[-1] - t_s[0]
    spc     = fs / sig_freq     # samples per cycle
    cycles  = sig_freq * t_total

    # FFT
    freqs = np.fft.rfftfreq(n, d=dt_mean)
    fft_mag = np.abs(np.fft.rfft(v - dc)) * 2.0 / n  # two-sided to one-sided

    # Find peak near expected frequency
    search_mask = (freqs >= sig_freq * 0.5) & (freqs <= sig_freq * 2.0)
    if search_mask.any():
        peak_idx = np.argmax(fft_mag[search_mask])
        all_idx  = np.where(search_mask)[0]
        peak_freq = freqs[all_idx[peak_idx]]
        peak_amp  = fft_mag[all_idx[peak_idx]]
    else:
        peak_freq = 0
        peak_amp  = 0

    amp_error_mV = (vpp - EXPECTED_VPP) * 1000.0
    pass_amp     = abs(amp_error_mV) <= PASS_AMP_TOL * 1000.0
    pass_spc     = spc >= 4.0
    pass_cycles  = abs(cycles - round(cycles)) < 1.0 or cycles > 3.0

    print(f"\n{'═'*52}")
    print(f"  {label}")
    print(f"{'═'*52}")
    print(f"  Samples          : {n}")
    print(f"  Total time       : {t_total:.3f} s")
    print(f"  Effective SPS    : {fs:.1f}")
    print(f"  Samples/cycle    : {spc:.1f}")
    print(f"  Captured cycles  : {cycles:.1f}")
    print(f"  Vpp measured     : {vpp*1000:.1f} mV  (expect {EXPECTED_VPP*1000:.0f} mV)")
    print(f"  Vpp error        : {amp_error_mV:+.1f} mV  ({'PASS ✓' if pass_amp else 'FAIL ✗'})")
    print(f"  DC offset        : {dc*1000:.1f} mV  (expect {EXPECTED_DC*1000:.0f} mV)")
    print(f"  FFT peak         : {peak_freq:.2f} Hz  (amp {peak_amp*1000:.1f} mV)")
    print(f"  Samples/cycle    : {'PASS ✓' if pass_spc else 'FAIL ✗'} ({spc:.1f} ≥ 4 required)")
    print(f"  Overall          : {'PASS ✓' if pass_amp and pass_spc else 'FAIL ✗'}")

    return {
        'n': n, 'fs': fs, 'spc': spc, 'cycles': cycles,
        'vpp': vpp, 'dc': dc, 'amp_error_mV': amp_error_mV,
        'peak_freq': peak_freq, 'peak_amp': peak_amp,
        'freqs': freqs, 'fft_mag': fft_mag,
        'pass_amp': pass_amp, 'pass_spc': pass_spc
    }

# ── Plot ───────────────────────────────────────────────────────────────────
def plot(t_s, v, stats, label, sig_freq, out_path):
    fig = plt.figure(figsize=(14, 9))
    fig.patch.set_facecolor(DARK)
    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.40, wspace=0.32)

    ax_wave  = fig.add_subplot(gs[0, :])   # full-width waveform
    ax_fft   = fig.add_subplot(gs[1, 0])   # FFT
    ax_stats = fig.add_subplot(gs[1, 1])   # stats table

    for ax in [ax_wave, ax_fft, ax_stats]:
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=TXT, labelsize=9)
        ax.xaxis.label.set_color(TXT)
        ax.yaxis.label.set_color(TXT)
        ax.title.set_color(TXT)
        for spine in ax.spines.values():
            spine.set_edgecolor(GRID)

    # Waveform — show first 5 cycles max for clarity
    t_show = min(5.0 / sig_freq, t_s[-1] - t_s[0])
    mask   = (t_s - t_s[0]) <= t_show
    ax_wave.plot(t_s[mask] - t_s[0], v[mask] * 1000, color=BLUE, lw=1.2,
                 label='ADC readings')
    # Ideal sine overlay
    t_ideal = np.linspace(0, t_show, 2000)
    v_ideal = stats['dc'] + (stats['vpp']/2) * np.sin(2*np.pi*sig_freq*t_ideal)
    ax_wave.plot(t_ideal, v_ideal * 1000, color=AMBER, lw=1.0, ls='--',
                 alpha=0.6, label='Ideal sine')
    ax_wave.set_xlabel('Time (s)', fontsize=10)
    ax_wave.set_ylabel('Voltage (mV)', fontsize=10)
    ax_wave.set_title(f'Captured Waveform — {label}', fontsize=11, fontweight='bold')
    ax_wave.legend(facecolor=DARK, edgecolor=GRID, labelcolor=TXT, fontsize=9)
    ax_wave.grid(color=GRID, lw=0.5)

    # FFT — show up to 3× signal frequency
    fmax   = min(sig_freq * 6, stats['fs'] / 2)
    f_mask = stats['freqs'] <= fmax
    ax_fft.plot(stats['freqs'][f_mask], stats['fft_mag'][f_mask] * 1000,
                color=BLUE, lw=1.2)
    ax_fft.axvline(sig_freq, color=PASS, lw=1.2, ls='--',
                   label=f'Expected {sig_freq:.0f} Hz')
    ax_fft.axvline(stats['peak_freq'], color=AMBER, lw=1.0, ls=':',
                   label=f'Peak {stats["peak_freq"]:.1f} Hz')
    ax_fft.set_xlabel('Frequency (Hz)', fontsize=10)
    ax_fft.set_ylabel('Amplitude (mV)', fontsize=10)
    ax_fft.set_title('FFT Spectrum', fontsize=10, fontweight='bold')
    ax_fft.legend(facecolor=DARK, edgecolor=GRID, labelcolor=TXT, fontsize=8)
    ax_fft.grid(color=GRID, lw=0.5)

    # Stats table
    ax_stats.axis('off')
    rows = [
        ['Samples',        f"{stats['n']}"],
        ['Effective SPS',  f"{stats['fs']:.1f}"],
        ['Samples/cycle',  f"{stats['spc']:.1f}"],
        ['Captured cycles',f"{stats['cycles']:.1f}"],
        ['Vpp measured',   f"{stats['vpp']*1000:.1f} mV"],
        ['Vpp expected',   f"{EXPECTED_VPP*1000:.0f} mV"],
        ['Vpp error',      f"{stats['amp_error_mV']:+.1f} mV"],
        ['DC offset',      f"{stats['dc']*1000:.1f} mV"],
        ['FFT peak',       f"{stats['peak_freq']:.2f} Hz"],
        ['Amp (PASS?)',     '✓ PASS' if stats['pass_amp'] else '✗ FAIL'],
        ['SPC  (PASS?)',    '✓ PASS' if stats['pass_spc'] else '✗ FAIL'],
    ]
    col_colors = [[PANEL, PANEL]] * len(rows)
    # Colour pass/fail rows
    for i, row in enumerate(rows):
        if '✓' in row[1]:
            col_colors[i][1] = '#1a3a2a'
        elif '✗' in row[1]:
            col_colors[i][1] = '#3a1a1a'

    tbl = ax_stats.table(
        cellText=rows,
        colLabels=['Metric', 'Value'],
        cellLoc='left',
        loc='center',
        cellColours=col_colors,
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.4)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_text_props(color=TXT)
        if r == 0:
            cell.set_facecolor('#2a2d3a')

    ax_stats.set_title('Test C Summary', fontsize=10, fontweight='bold')

    overall = stats['pass_amp'] and stats['pass_spc']
    fig.suptitle(
        f'ADS1263 Test C — AC Signal Capture  │  {label}  │  Overall: {"PASS ✓" if overall else "FAIL ✗"}',
        fontsize=12, fontweight='bold', color=TXT, y=1.01
    )

    plt.savefig(out_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    print(f"  → Saved: {out_path}")

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse, os

    parser = argparse.ArgumentParser(description='TestC analysis')
    parser.add_argument('--file',  help='CSV file path')
    parser.add_argument('--freq',  type=float, default=10.0, help='Signal frequency Hz')
    parser.add_argument('--label', default='10 Hz @ 400 SPS', help='Plot label')
    parser.add_argument('--out',   default='TestC_result.png', help='Output PNG')
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            csv_str = f.read()
    else:
        csv_str = SAMPLE_DATA

    idx, t_s, v = load_data(csv_str)
    if idx is None or len(idx) < 10:
        print("No data found. Paste CSV between CSV_START/CSV_END into SAMPLE_DATA,")
        print("or use: python TestC_Analysis.py --file data.csv --freq 10")
        sys.exit(1)

    print(f"Loaded {len(idx)} samples")
    stats = analyze(t_s, v, args.label, args.freq)
    plot(t_s, v, stats, args.label, args.freq, args.out)
    print("Done.")
