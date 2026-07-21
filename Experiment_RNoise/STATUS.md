# Experiment_RNoise — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP** — PHASE 2 bench session completed 2026-07-21. Offline analysis (PHASE 4/5) done. Root cause NOT yet confirmed; leading suspect is LDO instability, see below. |
| **Role** | Diagnose why the SMA self-sensing resistance estimate `R = V/I` is noisy, and decide between three causes with opposite fixes (common-mode / broadband / aliased) *before* changing hardware. |
| **Owner** | Yilin |
| **Quick test** | `python analyze_r_noise.py ../Experiment_SMAThermalCharacterization/data/console_20260715_193936_5V0.5V` |
| **Bench run** | scope setup → press H7 reset → `python capture_phase2.py --drive 0.85 --hold-ms 25000 --autorange --coupling A1M --out out/scope` |

---

## 1. What has been performed

### 1.1 Deployed-rate analysis (no bench time)

The plan assumed a PHASE 3 firmware+hardware lift for a deployed-rate capture. **It already existed in recorded data** — the H7 streams `sma_v`/`sma_i` at **~980 Hz** (not the ~99 Hz assumed), with 25 s gap-free runs. `analyze_r_noise.py` answers decision steps 1 and 3 from existing sessions.

Reproduced across two sessions (5V0.5V, 3V0.6V):

| Band | coherence(V, I) | R relative RMS |
|---|---|---|
| 0.5–10 Hz (thermal) | 0.98 / 0.95 | 23 400 / 18 300 ppm |
| 10–100 Hz | 0.81 / 0.69 | 66 000 / 54 900 ppm |
| 100 Hz–Nyquist | **0.15 / 0.18** | **102 700 / 86 200 ppm** |

R noise is dominated by the **100 Hz–Nyquist band where coherence collapses**. A 10 Hz low-pass gives ≈5.4× reduction (518 → 96 mΩ), above the SMA thermal bandwidth so near-lossless.

### 1.2 PHASE 2 scope campaign (bench, 2026-07-21)

Instruments: SDS2204X Plus (fw 5.4.1.5.2R2) over link-local SCPI socket; Portenta H7 on COM8. Load: **4.9 Ω power resistor in place of the SMA** (stationary operating point — the SMA's resistance drifts as it heats, and PSD/coherence assume stationarity).

Captures taken (all in `out/scope/`, drive confirmed live by firmware `read` in each case):

| file | condition |
|---|---|
| `phase2_idle_baseline.npz` | un-armed, zero current |
| `phase2_supplyA_0p85V.npz` | driven 0.85 V / 186 mA, C1 on Portenta A0 pad |
| `phase2_supplyB_0p85V.npz` | same, **different bench power supply** |
| `phase2_ldoOut_0p85V.npz` | same, **C1 moved to the LDO output** (before the divider) |

### 1.3 Measurement-chain defects found and fixed

These invalidated earlier numbers; all are now corrected in code:

- **`CODES_PER_DIV` is 30, not 25.** Measured: 10.61 codes × 0.5 V/div ÷ 0.1769 V (scope's own `PAVA? MEAN`) = 29.99. The driver's default of 25 (flagged "VERIFY" in `Driver_SiglentOscilloscope/STATUS.md`) reads **20% high**. `analyze_scope_capture.py` now re-derives volts from raw codes so old captures stay usable.
- **`MSIZ` was 10k** (factory default) → **one** Welch segment, and single-segment coherence is *identically 1.0 at every bin*. That reads as "perfect common-mode rejection, nothing to fix" when it means "no data". Both the capture and analysis paths now refuse coherence below 8 segments.
- **Clipping fabricates spurs.** First captures were hard-clipped (|code| = 127). Auto-ranging now works off the real record — `PAVA? PKPK` is computed on the *decimated display* waveform and under-reported by ~2×, reporting 4.5 div while the record was clipped.
- **`BWL` (bandwidth limit) was ON** on both channels while hunting a 200–525 kHz spur. Now off. (Measured effect on these signals: only 0.7% — it was not the cause of anything, but it had no business being in the path.)
- **H7 command channel.** The M7 loop is `pumpSensors() → pollCommand() → serviceSma()`; `pumpSensors()` blocks in `Serial.write` when the host stops draining, so **`arm`/`drive` were silently never read** — no error, no reply, no drive, and eventually the board stopped emitting entirely (DTR reset did not recover it). Fixed by mirroring the working recorder (`Experiment_SMAThermalCharacterization/lib_workers.py` ~L489): a continuous drain thread **plus `netcfg <pc_ip> <udp_port>`** to move the sample flood to fire-and-forget UDP. Since that change the H7 has served commands reliably with no wedging. **Every drive is now verified by a firmware `read` before the capture is trusted.**

### 1.4 Probe chain validated

Transfer ratios measured across three operating points (V_LDO 0.50 / 0.51 / 2.03 V):

- **C1 = V_LDO / 2.10** at DC — the `10k/10k` divider on A0 (`main.cpp:85`).
- **C2 = I × 0.93 V/A** — INA296A nominal 1 V/A. The 7% gap means the **firmware reads ~7% high**, independently corroborating the known ADC conversion-duty reference droop on this rig.

---

## 2. Results

### 2.1 The dominant signal is a deterministic tone, not noise

At 0.871 V / 186 mA, AC-coupled, auto-ranged, 1 M points @ 10 MSa/s (243 Welch segments):

- **VLDO: 166 mV rms**, 1.13 V pk-pk — **19% of the rail**
- **Vsense: 105 mA rms** — **56% of the current**
- Structure: **24.414 kHz fundamental with a clean harmonic series** (73.2 / 97.7 / 122.1 / 146.5 kHz) plus a separate line at 460.2 kHz
- Coherence(VLDO, Vsense) = **0.996–0.999 from 10 kHz to 525 kHz**

A TPS7A57 is specified at **2.45 µV rms** output noise. We measure **158–166 mV rms** — five orders of magnitude out.

### 2.2 Coherence ≈ 1 does NOT imply cancellation

```
dV/V = 190 000 ppm     dI/I = 607 000 ppm
dR/R = 514 000 ppm     cancellation = 1.2x only
```

**Correction to the plan's decision rule.** The plan states coherence ≈ 1 → common-mode → cancels in R → ignore. That holds only when the *fractional* amplitudes match. Here dI/I is 3× dV/V, so `dR/R = dV/V − dI/I` leaves most of dI/I standing despite near-perfect correlation. Correlation is necessary but not sufficient — **compare δV/V against δI/I, not just coherence.**

### 2.3 Eliminations

| suspect | test | result |
|---|---|---|
| **Bench power supply** | swapped to a different supply, identical capture | **ELIMINATED** — 24.414 kHz went 53.85 → 52.86 mV (0.98×); all harmonics within 1.0–1.2×; total AC 165.7 → 182.5 mV |
| **MOSFET PWM chopping** | `main.cpp:570` | **ELIMINATED** — `digitalWrite(MOSFET_PIN, HIGH)`, pure DC, not `analogWrite` |
| **Scope sampling artifact** | tone scales 4.5× with load current | **ELIMINATED** — load-dependent, so it is real rig behaviour |
| **Probe/ADC-node pickup only** | moved C1 from A0 pad to LDO output | **ELIMINATED** — 27.5 mV at 24.4 kHz measured directly at the LDO output; the tone is genuinely on the rail |

### 2.4 New finding: the A0 divider is uncompensated

Raw pad measurement 25.2 mV vs true rail 27.5 mV → at 24 kHz the divider passes **≈0.92, essentially 1:1**, while dividing DC by 2.10. Parasitic capacitance across the top resistor. **Consequence: the ADC pin sees half the DC but the full high-frequency ripple** — the junk-to-signal ratio at the ADC input is twice what the DC ratio suggests.

---

## 3. What we suspect

### 3.1 Primary: the TPS7A57 is oscillating, from insufficient effective COUT

A fixed low-frequency tone with a harmonic series, amplitude growing with load current, present on the regulator output itself, five orders of magnitude above the noise spec — that is a regulator oscillating, not a noise problem.

Datasheet Recommended Operating Conditions (`docs/tps7a57.pdf` p6):

```
COUT      Output capacitor       22    22   3000  µF
          (1) Effective output capacitance of 15 µF minimum required for stability
COUT_ESR  Output capacitor ESR    2         20    mΩ
ZOUT_ESL  Total impedance ESL    0.2         1    nH
CNR/SS    Noise-reduction cap    0.1  4.7    10   µF
```

**Fitted COUT is `CL10A226MO7JZNC` = 22 µF X5R in an 0603 (CL10) case, ±20%.** A 22 µF X5R in 0603 is near the maximum capacitance density available in that package and derates severely under DC bias — commonly 60–80% loss. Stacking −20% tolerance, X5R temperature drift, and DC-bias derating, the **effective capacitance plausibly sits at or below the 15 µF stability floor**, and degrades further as drive voltage rises. That matches the tone growing with load.

*Uncertainty:* the `O7` voltage-rating field was not decoded with confidence, and derating depends on it. The concern holds for any rating available at 22 µF in 0603.

### 3.2 Secondary: output ESL

`ZOUT_ESL ≤ 1 nH` is a brutal spec — a few cm of wire is 10–20 nH. This rig has a **MOSFET plus wiring between the LDO output and the load**. Datasheet p65 warns about exactly this mechanism:

> *"Because of the wide bandwidth, the LDO error amplifier potentially reacts faster than the output capacitor... minimize both ESR and ESL present on the output."*

### 3.3 Not a suspect: CNR/SS = 100 nF

100 nF is the datasheet **minimum** (0.1 µF), so it is in spec — just the worst end for noise, and it explains exceeding the 2.45 µV rms figure (which assumes 4.7 µF). But CNR/SS forms a low-pass with `RREF` on the *reference* (p66); it is a noise/soft-start element, **not a loop-stability element**. Changing it buys perhaps 10–20 dB, not the ~100 dB in question. Worth doing for the noise floor, will not fix the tone.

### 3.4 Unexplained

The current channel shows ~145 mA rms AC, but a 158 mV rail ripple into 5 Ω can only produce ~32 mA — **C2 carries ~4.5× more than the rail can account for**. C2's DC scaling is verified (0.93 V/A against the firmware's 453 mA), so this is not a scaling error. No explanation yet; deliberately not guessed at. May be the INA296A's own response or coupling. Isolate separately.

### 3.5 Case C still stands regardless of source

**~100% of the AC energy is above 500 Hz**, i.e. above the deployed ~1 kHz sampler's Nyquist, with no anti-alias filter. That energy folds into the measurement band and is then mathematically unrecoverable — which matches the incoherent broadband floor that dominates R noise in the deployed data. Anti-alias filtering will be needed **whatever the source turns out to be**.

Filter sizing computed from the measured spectrum:

| RC corner | V rms | I rms | atten @24.4 kHz | energy >500 Hz (folds) |
|---|---|---|---|---|
| none | 165.7 mV | 105.0 mA | 1× | **165.7 mV** |
| 10 kHz, 1-pole | 41.5 mV | 16.5 mA | 3× | 41.5 mV |
| 1 kHz, 1-pole | 4.98 mV | 1.99 mA | 24× | 4.91 mV |
| **300 Hz, 2-pole** | **0.80 mV** | **0.30 mA** | **6600×** | **0.071 mV** |

**The plan's suggested "corner a few kHz" is far too gentle** — 10 kHz gives only 3×. Residual floors at ~0.8 mV below ~300 Hz, so 2-pole at 300 Hz is the sweet spot; the SMA thermal band is 0.5–10 Hz so it costs nothing in signal. Buffer required (A0 sits behind a 5 k source; series R alone starves the ADC S/H). **Both channels must get identical filters** or R is distorted during transients.

---

## 3.6 INTERIM MITIGATION (in place, use with the caveat)

`analyze_r_noise.filter_r(r, fs, corner_hz=10)` — zero-phase 10 Hz low-pass on R.
Measured **≈5.4× reduction** (518 → 96 mΩ; 423 → 72 mΩ on the second session), no
hardware, no real signal lost (thermal band is 0.5–10 Hz).

**Why it works despite "aliasing is irreversible":** folded energy is only
unrecoverable when it lands *inside* the signal band. At the deployed rate the LDO
tones fold to 96 / 288 / 384 / 480 / 397 Hz — all above 10 Hz — so the low-pass
genuinely removes them.

**Why it is a band-aid, not a fix:** the fold position depends on the tone/fs ratio,
and fs is a software loop. Between two real sessions 1 Hz apart, the 460.205 kHz
tone moved from **397 Hz to 50 Hz**. A 0.39% drift (3.84 Hz) parks the 24.414 kHz
fundamental at DC — inside the thermal band, unremovable, and looking exactly like a
slow real resistance change. **Silent failure mode: the data looks cleaner, not
wrong.** `analyze_r_noise.py` now prints each tone's fold frequency per session and
warns if any falls under 10 Hz — check that line every run.

## 4. Next steps

1. **[decisive, 2 min] Parallel a second 22 µF in 0805/1206 (X7R preferred) directly at the LDO output pins**, then re-run the identical capture and compare against `phase2_ldoOut_0p85V.npz`:
   - tone collapses → confirmed insufficient effective COUT; fix with a part holding ≥15 µF effective at bias (larger case, higher voltage rating, or parallel devices — verify against the manufacturer's DC-bias curve at the actual VOUT, not the marked value)
   - tone unchanged → COUT value is not it; move to the ESL path (§3.2)
2. **Check COUT placement** relative to the LDO pins vs the MOSFET/load wiring (§3.2).
3. **CNR/SS 100 nF → 4.7 µF** for the noise floor once the tone is resolved. Side effect: `tSS = VOUT × CNR/SS / ISS` ⇒ ~117 ms soft-start at 5 V (vs ~2.5 ms today) — harmless, and the origin of the firmware's existing "~100 ms settle" assumption.
4. **Re-measure after each single change** (plan guardrail: one change at a time).
5. **Change-fs alias test** (`--alias-test`) — still outstanding, confirms the folding directly.
6. **Then** size the anti-alias RC against whatever ripple remains, per §3.5. Do this *last*: filtering a 158 mV oscillation would hide a real actuation problem behind a clean-looking measurement.
7. **Isolate the C2 excess** (§3.4).

## 5. Open risks

- Filtering fixes the *measurement*, not the *actuation*. If the rail really swings ±0.4 V, the SMA's Joule heating is not what a DC model assumes — a clean R reading would mask a real physical problem.
- The firmware reads A0 then A1 **sequentially**. The plan assumed µs-scale skew was negligible; with 24 kHz content and fast edges it is not, and V/I sampled at different points on the waveform gives a meaningless ratio. The anti-alias filter also fixes this by slowing the signals down.

See [../README.md](../README.md) for project overview.
