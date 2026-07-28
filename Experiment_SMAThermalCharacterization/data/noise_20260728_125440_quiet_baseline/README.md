# Quiet-baseline noise characterisation — 2026-07-28 12:54

Sensor noise with the rig **at rest**: nothing actuating, no SMA connected, the
laser and load cell left in their neutral position. The point was to find where
the noise lives in frequency before designing any filtering.

## Provenance — read this first

**This is NOT the data from `console_20260728_115753`.** That session recorded
**nothing** (`h7.csv` header-only) because the H7 was wedged at boot, failed the
startup health check, and the console never began recording — see
[`../console_20260728_115753/NOTE.md`](../console_20260728_115753/NOTE.md).

This capture was taken afterwards with `capture_quiet.py`, a direct serial
reader, once a **force pull** revived the port (`portenta_reader.open()` now
does that automatically — see `Calibrate_LaserHead/portenta_reader.py`).

| | |
|---|---|
| captured | 2026-07-28 12:54:40, 60 s |
| firmware | `Firmware_SMAConstantCurrent_PIO` `portenta_m7` (commit `db96822`) |
| channels | `src=1` laser, `src=2` load cell — 29 751 / 29 750 rows |
| streamed rate | 495.8 Hz |
| true conversion rate | **400.8 Hz** (ADS1263) → Nyquist 200 Hz |
| SMA | disarmed, no DUT |

## Files

| file | what |
|---|---|
| `h7_quiet.csv` | raw capture — `src, hw_us, value, raw_code, seq` |
| `fig1_amplitude_spectrum.png` | **where the noise is** — amplitude vs frequency, linear axis |
| `fig2_lowpass_10hz_before_after.png` | a 10 Hz low-pass, before vs after |
| `fig3_targeted_filters_before_after.png` | per-channel filters (notch vs low-pass) |
| `capture_quiet.py` | the acquisition script |
| `make_fig1.py` / `make_fig2.py` / `make_fig3.py` | regenerate each figure from the CSV |

Reusable analysis lives at `../../operator_noise_psd.py`.

## Two things the analysis must do, or the answer is wrong

1. **Use `hw_us`, not `host_timestamp_s`.** Windows scheduler jitter on host
   timestamps smears the spectrum.
2. **Remove the zero-order-hold duplicates.** M4 polls each ADC every 2 ms
   (~500 Hz) but the ADS1263 converts at 400 SPS, so **19.2 %** of rows re-fetch
   the same data register. Spectrally those are held samples, not new ones.
   Deduplicating on `raw_code` change recovers the true ~400 Hz sequence. Skip
   this and the frequency axis is wrong by ~24 %.

## Results

### Laser (`src=1`) — discrete tones on a very low floor

mean **+2.724 V**, RMS **0.617 mV**, broadband floor above 100 Hz
**3.26 µV/√Hz**.

| line | amplitude | note |
|---|---|---|
| **65.76 Hz** | **0.550 mV** | dominant — **496×** the >100 Hz floor |
| 131.52 Hz | 0.222 mV | 2nd harmonic |
| 174.29 Hz | 0.083 mV | |
| 160.78 Hz | 0.072 mV | |
| 36.6 / 29.2 Hz | 0.088 / 0.066 mV | |

**Nothing at 50 or 60 Hz — this is not mains pickup.** 65.8 Hz is a
free-running instrumental rate, matching the tone recorded in earlier sessions.
58.9 % of the channel's variance sits in the 50–100 Hz band, essentially all of
it in that one line.

### Load cell (`src=2`) — broadband, no lines at all

mean **+0.0038 V** (as expected at neutral), RMS **4.749 mV**, broadband floor
above 100 Hz **162.85 µV/√Hz**. No peak anywhere exceeds 4× its local floor.
Variance: 42 % in 10–50 Hz, 31 % in 50–100 Hz.

### The finding worth acting on

**Both channels share the same ADS1263, the same REF7050 reference and the same
400 SPS rate. The laser reaches 3.26 µV/√Hz; the load cell sits at
162.85 µV/√Hz — 50× worse.**

The digitiser is demonstrably capable of ~3 µV/√Hz on this rig, so the
load-cell noise is entirely in its **analog front end** — sensor excitation, the
LCA-9PC, or shielding on that run. No ADC setting will improve it, and
`ADC_SAMPLES_CYCLE` will not touch it. Fixing the front end would beat any
filter *and* cost no bandwidth.

Next test to localise it: capture with the load-cell input **shorted at the
amplifier**. Noise stays → amplifier or its supply. Noise drops → sensor or
cabling.

## Filtering — and the trade-off

| cutoff | laser RMS | load RMS | rise time | usable across a 100 ms transient? |
|---|---|---|---|---|
| none | 0.617 mV | 4.75 mV | — | yes |
| 50 Hz | — | 3.49 mV | 7 ms | yes |
| 20 Hz | 0.422 mV | 2.18 mV | 18 ms | marginal |
| **10 Hz** | **0.355 mV** | **1.54 mV** | **35 ms** | **no — smears it** |
| notch 65.8+131.5 Hz | **0.422 mV** | n/a | — | **yes — full bandwidth** |

**The two channels want opposite treatments:**

* **Laser** — its noise is two spectral lines, so a **notch** removes them while
  keeping the full 200 Hz bandwidth. A low-pass is the wrong tool here: it
  costs transient fidelity to solve a problem a notch solves for free.
* **Load cell** — nothing to notch, so bandwidth is the only lever, and RMS
  falls as √bandwidth. Across a 100 ms heat window **50 Hz is about the limit**;
  beyond that the only remaining gain is **averaging over repeated cycles**
  (√N — ten cycles buys another 3.2×).

## Caveat

**The laser sits at 2.724 V, not the 2.5 V neutral that was expected** — 224 mV
off null. The load cell is fine at 3.8 mV ≈ 0. Worth checking the standoff
before taking real displacement data, since it shifts where you sit in the
sensor's linear range. It does not affect the noise conclusions above.
