> **Status: WIP**. See [STATUS.md](STATUS.md). See [../README.md](../README.md) for project overview.

# Experiment_RNoise — SMA Resistance Self-Sensing Noise Diagnosis

Why is the SMA self-sensing resistance estimate `R = V / I` noisy, and which of
three physically different causes is responsible? They need **opposite fixes**, so
the whole point is to tell them apart *before* changing hardware.

| Cause | Signature | Correct fix |
|---|---|---|
| **A** common-mode VLDO noise | coherence(V, I) ≈ 1 **and** δV/V ≈ δI/I | **nothing** — it cancels in the ratio |
| **B** per-channel broadband | low coherence, flat floor above the ~10 Hz thermal band | digital low-pass / decimation |
| **C** aliased supply noise | out-of-band energy folding in; spurs **move** when fs changes | analog anti-alias RC + fix at source |

**Current conclusion:** Case C, driven by ~158 mV rms of ripple on the LDO output
(against a 2.45 µV rms datasheet spec) spread from ~12 to 400 kHz — essentially all
of it above the deployed ADC's 490 Hz Nyquist. Root cause not yet confirmed; leading
suspect is insufficient *effective* output capacitance on the TPS7A57. See
[STATUS.md](STATUS.md) §3.

## Contents

| File | What it is |
|---|---|
| [`sma_resistance_noise_plan.md`](sma_resistance_noise_plan.md) | The original 5-phase campaign plan. |
| [`SDS2000X-Plus_FFT_via_Python_usage.md`](SDS2000X-Plus_FFT_via_Python_usage.md) | Generic SCPI/FFT spec. **See the caveat below.** |
| `analyze_r_noise.py` | PHASE 4/5 analysis of recorded H7 sessions + the interim `filter_r()` mitigation. |
| `capture_phase2.py` | PHASE 2 scope capture: scope setup, H7 drive, both channels from one stopped acquisition. |
| `analyze_scope_capture.py` | Coherence / PSD / change-fs alias verdict for those captures. |
| `check_probe_dc.py` | One post-reset shot: confirm the H7 is armed and driving, then read what the scope actually sees. |
| `make_report_plots.py` | The three report figures. |
| `out/` | Generated plots; `out/scope/` holds the raw captures. |

## Quick start

```bash
pip install -r requirements.txt

# offline, no bench needed
python analyze_r_noise.py ../Experiment_SMAThermalCharacterization/data/console_20260715_193936_5V0.5V

# report figures
python make_report_plots.py
```

## Running PHASE 2 at the bench

**Order matters.** `--setup` takes ~30 s of scope writes, during which nothing drains
COM8 — long enough for the H7 to block. So configure the scope *first*, with the H7
uninvolved, then capture:

```bash
# 1. scope only (H7 untouched)
python capture_phase2.py --setup --coupling A1M --vdiv 50MV 50MV

# 2. capture (opens COM8 and starts draining immediately)
python capture_phase2.py --drive 0.85 --hold-ms 25000 --autorange --out out/scope

# 3. analyse
python analyze_scope_capture.py out/scope/phase2_single.npz
python capture_phase2.py --drive 0.85 --alias-test --out out/scope   # change-fs test
```

### Probe map

```
C1 -> V_LDO    (Portenta A0 pad = LDO out via a 10k/10k divider, main.cpp:85)
C2 -> Vsense   (Portenta A1 pad = INA296A OUT, no divider, main.cpp:86)
```

Short ground springs, both to the supply-return node. `--attn` **must match the
physical probe** — it is only a scale factor the scope applies and cannot detect the
real probe; a mismatch leaves frequencies and coherence correct while silently
scaling every voltage (and therefore R) by 10×.

Load for this phase is a **4.9 Ω power resistor** in place of the SMA — the SMA's
resistance climbs as it heats, so its operating point drifts *during* the capture,
and PSD/coherence assume stationarity. `I = v_drive / 5.0` (load + 0.1 Ω shunt).

### Why `--drive` is not optional

The low-side MOSFET is the master enable, so an un-armed board passes **zero**
current: Vsense ≈ 0, R = V/I is a divide-by-noise, and load-dependent supply noise is
absent entirely — a clean-looking result that means nothing. Every drive is confirmed
by a firmware `read` before the capture is trusted.

### Two hardware behaviours the scripts work around

- **The H7 goes deaf under USB back-pressure.** Its loop is
  `pumpSensors() → pollCommand() → serviceSma()`; `pumpSensors()` blocks in
  `Serial.write` when the host stops draining, so `arm`/`drive` are *silently never
  read*. `H7Drive` runs a continuous drain thread **and** sends
  `netcfg <pc_ip> <udp_port>` to move the sample stream to UDP — the same fix the
  working recorder uses (`Experiment_SMAThermalCharacterization/lib_workers.py`).
- **The scope silently rejects some settings.** Back-to-back writes get dropped
  (cured by space-out + read back + retry); `MSIZ` is ignored entirely while the
  acquisition is stopped and accepts only decade values; and the set/query syntax is
  asymmetric for bandwidth limit (`BWL C1,OFF` to set, `C1:BWL?` to read). Every
  setting is read back and retried, and failures are reported rather than assumed.

## Analysis conventions

- Time base is **`hw_us`, not `host_timestamp_s`** — host timestamps carry USB
  scheduling jitter that smears the spectrum.
- Spectra are of the **fractional** fluctuation (`x / mean(x)`), so V, I and R are
  directly comparable.
- Volts are re-derived from **raw ADC codes** at **30 codes/div** (measured, not the
  driver's assumed 25 — see STATUS §1.3), so captures stay correctable after the fact.
- **Coherence needs ≥ 8 Welch segments.** Below that it is biased to exactly 1.0 at
  every bin, which reads as perfect rejection when it means no data. Both the capture
  and analysis paths refuse to report it.

## Caveat on the SCPI usage doc

`SDS2000X-Plus_FFT_via_Python_usage.md` is written as a from-scratch pyvisa
implementation. The repo already has a Stable, bench-verified, **no-VISA** driver at
[`../Driver_SiglentOscilloscope/`](../Driver_SiglentOscilloscope/README.md) (raw SCPI
socket on port 5025, link-local auto-discovery, IEEE-488.2 block parsing, soft
reconnect). `capture_phase2.py` extends that driver rather than starting over.
