# Plan: Diagnose SMA Resistance (R = V/I) Self-Sensing Noise

**Owner:** Yilin Ma — SMA in-situ impedance self-sensing
**For:** execution agent at the bench + host PC
**Type:** measurement + analysis campaign, no hardware redesign until the decision at the end

---

## 0. Context (read first)

We compute SMA resistance as **R = V / I**, where:

- **V** = SMA voltage, derived from **VLDO** (the LDO rail driving the SMA)
- **I** = current, derived from **Vsense** = INA296 shunt-amp output (`Vsense = G · I · Rshunt`)
- Both VLDO and Vsense are digitized by the **Portenta H7 (STM32H747) internal ADC** at a **~1 kHz** update rate.

The observed resistance estimate is noisy. There are **three physically different causes**, and they need **opposite fixes**. The entire point of this campaign is to tell them apart *before* changing anything:

| Cause | Signature | Correct fix |
|---|---|---|
| **A. Common-mode VLDO noise** | VLDO and Vsense noise are correlated (coherence ≈ 1) | **Nothing** — it cancels in R = V/I; do not chase it |
| **B. Per-channel broadband noise** | Low coherence; flat broadband floor in FFT(R) above the ~10 Hz thermal band | **Digital low-pass / decimation** (√N averaging, nearly lossless because R moves on the slow thermal timescale) |
| **C. Aliased structured supply noise** | Discrete spur(s); known **200–525 kHz** supply/choke noise folding into band | **Analog anti-alias RC per ADC input + kill the choke noise at source** — digital filtering cannot undo aliasing |

**Do not skip to a fix.** Run the measurements, read the three plots, then decide.

### Key physics to keep in mind
- For **simultaneously sampled** V and I, VLDO fluctuation moves numerator and denominator by the *same fraction* → cancels in R to first order (ratiometric). So VLDO noise is **not** automatically the culprit.
- What breaks that cancellation: (1) sample skew between channels, (2) independent per-channel noise (mostly the Vsense / INA chain), (3) aliasing of out-of-band noise.
- At ~1 kHz update rate the two channels are read back-to-back within a frame (µs-scale skew), so **skew is expected to be minor** — but confirm, don't assume.

---

## Instruments & roles

- **Siglent oscilloscope (with host-PC SCPI/USB or LAN capture)** → *analog ground truth.* Samples far above 525 kHz, so it sees the supply spur **un-aliased**. Answers: is the spur real, how big, at what frequency, and is it coherent between VLDO and Vsense?
- **Portenta H7** → *the deployed chain.* Answers a different question: does the **shipped 1 kHz ADC path alias** the spur into band, and what does FFT(R) look like on the real signal path?
- **Red Pitaya (optional)** → redundant with the scope for ground truth; only use if the scope path is blocked.

> The scope measures reality **upstream** of the ADC. It cannot tell you what the ADC does to that reality. That is why the Portenta captures are still required.

---

## PHASE 1 — Scope bench setup + live hunt

### 1.1 Physical setup
- [ ] CH1 → **VLDO**, CH2 → **Vsense**, probing **at the ADC input pins** (the exact nodes the Portenta samples), not back at the LDO.
- [ ] Use **short ground springs**, not long clip leads. At 200–525 kHz a clip-lead loop picks up its own noise.
- [ ] 10× probes; run probe compensation/cal first so CH1/CH2 are amplitude- and phase-matched (matters for coherence).

### 1.2 Scope acquisition settings
- [ ] **DC coupling** both channels (we care about operating point + low-freq drift). If mV ripple on the ~5 V rail is buried, add **vertical offset and zoom**, don't switch to AC.
- [ ] Sample rate **≥ ~5 MSPS** (plenty of margin over 525 kHz). Record the *actual* Sa/s the scope reports for the chosen timebase — needed exactly for the FFT axis.
- [ ] **Max memory depth** (Siglent "Mem Depth"), aim ≥ 100k points/channel for fine FFT bins. Pick a timebase that keeps Sa/s high.
- [ ] Trigger: free-run / auto is fine (characterizing noise, not an edge).

### 1.3 Live root-cause hunt (highest-value step — do it thoroughly)
- [ ] Turn on the scope's built-in **FFT on CH1 (VLDO)**. Locate the 200–525 kHz spur; note frequency + amplitude.
- [ ] While watching the live FFT, toggle bench variables one at a time and record what moves the spur:
  - enable/disable the LDO
  - swap / resize the RF choke
  - add a decoupling cap at the rail
- [ ] **Deliverable P1:** note which change drops the spur most — that is the root-cause lead the offline pipeline cannot give you.

---

## PHASE 2 — Scope PC capture (for coherence/PSD)

> The scope's built-in FFT gives per-channel spectra but usually **not coherence** (the VLDO↔Vsense cross-spectrum). Capture both channels to CSV and compute coherence offline.

### 2.1 Capture
- [ ] Connect scope to host (Siglent SDS over USB or LAN, SCPI via pyvisa, or Siglent's waveform/EasyScope tool). Confirm with `*IDN?`.
- [ ] Capture **CH1 and CH2 from the same single acquisition** (time-aligned). Coherence is meaningless if the two channels come from different grabs — single-shot/stop, then read both waveforms.
- [ ] Export to **CSV**; verify the header carries the **sample interval / time increment** (needed to reconstruct exact fs). Record vertical scale/offset if CSV is in counts, not volts.
- [ ] Take **two long records at two different sample rates** (e.g. change the timebase so Sa/s differs, say 5 MSPS and 4 MSPS) for the alias cross-check in analysis.

### 2.2 Capture gotchas
- [ ] **8-bit vertical resolution:** small ripple on a big DC rail is quantization-limited — zoom vertically as far as possible without clipping.
- [ ] **Export decimation:** some Siglent export modes downsample on export, which can alias *in the file*. Ensure export depth = acquisition depth.

---

## PHASE 3 — Portenta H7 captures (does the deployed chain alias?)

### 3.1 Hardware prep (mandatory before fast sampling)
- [ ] Add an **op-amp buffer + RC** in front of each ADC pin (VLDO, Vsense). At fast sampling the S/H cap won't charge through high source impedance → droop that masquerades as noise. The RC also doubles as the analog anti-alias filter (corner a few kHz).

### 3.2 Firmware config
- [ ] ADC1 = VLDO, ADC2 = Vsense, **dual regular simultaneous mode** (both S/H fire on one trigger → skew ≈ 0).
- [ ] **Timer-triggered at an exact, known fs** (do NOT free-run continuous — you need fs precise for the frequency axis).
- [ ] DMA a **64k–256k sample/channel** block into SRAM, dump raw over USB.
- [ ] Boost mode on, minimum sampling time. Use HAL/register access, not the Arduino mbed core, for deterministic fs.
- [ ] Verify the ADC1/ADC2 pinmux: the two signals must land on the **ADC1+ADC2 pair**, not scanned through one ADC's mux.

### 3.3 Two capture modes
- [ ] **Mode A — alias test (burst, two rates):** capture at **1.0 MSPS** and **0.8 MSPS**. (Nyquist > 525 kHz so the spur appears un-aliased *if the ADC can reach it*; the two rates reveal folding.)
- [ ] **Mode B — deployed rate:** capture at the real **1 kHz** update rate, form R, and FFT(R). This is exactly what a low-pass on R would act on.

---

## PHASE 4 — Analysis (host)

Save captures as `.npy` or point the loader at the Siglent/Portenta CSVs. Reference script:

```python
import numpy as np
from scipy import signal
import matplotlib.pyplot as plt

# ---- load (edit for your CSV/npy format; parse fs from the scope header) ----
fs      = 5_000_000          # EXACT sample rate of THIS capture, Hz
vldo    = np.load('vldo.npy')       # convert counts -> volts if needed
vsense  = np.load('vsense.npy')

# ---- INA / shunt constants (fill in actual values) ----
G, Rshunt = 100.0, 0.1              # INA gain, shunt ohms
I = vsense / (G * Rshunt)
V = vldo                            # adjust to actual topology (high/low-side)

nperseg = 8192                      # bin width = fs/nperseg

# 1) per-channel PSD
f, P_vldo   = signal.welch(vldo,   fs, nperseg=nperseg)
_, P_vsense = signal.welch(vsense, fs, nperseg=nperseg)

# 2) COHERENCE  <-- the money plot: what cancels vs what survives in R
f_c, Cxy = signal.coherence(vldo, vsense, fs, nperseg=nperseg)

# 3) spectrum of R itself: what a low-pass on R would remove
R = V / I
R = R - np.mean(R)
f_r, P_R = signal.welch(R, fs, nperseg=nperseg)

# ---- plots ----
fig, ax = plt.subplots(3, 1, figsize=(9, 10))
ax[0].semilogy(f, P_vldo, label='VLDO'); ax[0].semilogy(f, P_vsense, label='Vsense')
ax[0].set_title('Per-channel PSD'); ax[0].set_xlabel('Hz'); ax[0].legend()
ax[1].plot(f_c, Cxy); ax[1].set_ylim(0,1)
ax[1].set_title('Coherence VLDO<->Vsense  (≈1 cancels in R, ≈0 survives)'); ax[1].set_xlabel('Hz')
ax[2].semilogy(f_r, P_R); ax[2].set_title('Spectrum of R  (what a low-pass would act on)')
ax[2].set_xlabel('Hz'); ax[2].axvline(10, ls='--')  # ~thermal band edge
plt.tight_layout(); plt.savefig('R_noise_diagnosis.png', dpi=140)
```

### Change-fs alias test (two captures at different fs)
```python
def psd(path_npy, fs, nperseg=8192):
    x = np.load(path_npy); return signal.welch(x, fs, nperseg=nperseg)

f1, P1 = psd('vldo_fs1.npy', 5_000_000)
f2, P2 = psd('vldo_fs2.npy', 4_000_000)
plt.figure(); plt.semilogy(f1, P1, label='fs1'); plt.semilogy(f2, P2, label='fs2')
plt.xlabel('Hz'); plt.legend(); plt.title('Peaks that MOVE = aliases; peaks that STAY = real')
plt.savefig('alias_test.png', dpi=140)
```

---

## PHASE 5 — Decision (the whole point)

Read the three outputs in order:

1. **Coherence(VLDO, Vsense)** at the spur / noise band:
   - **≈ 1** → common-mode, **cancels in R = V/I → ignore it.** (Case A.)
   - **≈ 0** → per-channel, survives into R → continue.
2. **Change-fs test:** do the spurs **move** between the two sample rates?
   - **Move** → aliased out-of-band energy (Case C).
   - **Stay** → correctly sampled, real in-band (Case B).
3. **FFT(R)** above the ~10 Hz thermal band:
   - **Flat broadband floor, no peaks** → **Case B → digital low-pass / decimation wins.** Sample fast, average N, cut σ by √N; negligible lag because R moves on the slow thermal timescale.
   - **Discrete spur(s)** that moved in step 2 → **Case C → analog RC anti-alias filter per ADC input (corner a few kHz) + attack the choke noise at source.** A digital filter cannot recover this.

**One-line rule:** coherence says what cancels vs. survives; change-fs says what's aliased; FFT(R) says what a low-pass would remove.

---

## Deliverables checklist
- [ ] P1: live-FFT root-cause note (what bench change drops the spur)
- [ ] P2: two scope CSVs (two sample rates), fs recorded from headers
- [ ] P3: Portenta burst captures (1.0 & 0.8 MSPS) + 1 kHz deployed-rate capture
- [ ] P4: `R_noise_diagnosis.png` (PSD + coherence + FFT(R)) and `alias_test.png`
- [ ] P5: written decision — Case A / B / C — and the single recommended fix

## Guardrails
- Do **not** implement more than one fix at a time — attribute each change (staged, one-change-at-a-time validation).
- Do **not** trust averaging until the alias question is answered; averaging over aliased noise does nothing.
- Keep DMM-verified 0 V DC / correct operating point conventions consistent with the existing bias-tee validation protocol.
