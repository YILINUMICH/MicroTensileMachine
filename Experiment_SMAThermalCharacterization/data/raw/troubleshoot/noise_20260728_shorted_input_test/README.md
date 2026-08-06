# Shorted-input test — is the load-cell noise before or after the amplifier?

2026-07-28. Follow-up to
[`../noise_20260728_125440_quiet_baseline/`](../noise_20260728_125440_quiet_baseline/),
which found the load cell running **50× noisier than the laser on the same
ADS1263** — so the digitiser was not the limit and the noise had to be in the
load path. This test splits that path in two.

## Method

Load cell **connected and powered**, with **SIG+ shorted to SIG− at the
amplifier's input terminals**. The bridge stays in circuit so the amplifier's
input bias currents keep their DC return path, but the sensor's *signal* is
removed. Everything downstream — amplifier, excitation, cabling to the EVM,
ADC — is untouched.

Compared against the baseline capture under identical settings: 60 s,
`capture_quiet.py`, SMA disarmed, `Firmware_SMAConstantCurrent_PIO`
`portenta_m7`.

## Result — the noise is split almost evenly

```
LOAD CELL   baseline 4.749 mV RMS  ->  shorted 3.122 mV RMS   (1.52x)

  amplifier + excitation + downstream    3.122 mV    43% of variance
  sensor + cable                         3.578 mV    57% of variance
```

**Neither side dominates.** Fixing only one caps the improvement at ~1.5×.

The reduction is uniform across the whole band, which argues against a discrete
interferer (mains, a switching supply) and for broadband pickup on both sides:

| band [Hz] | connected | shorted | ratio |
|---|---|---|---|
| 0–1 | 0.508 | 0.321 | 1.58× |
| 1–10 | 1.527 | 0.983 | 1.55× |
| 10–50 | 3.094 | 2.001 | 1.55× |
| 50–100 | 2.646 | 1.743 | 1.52× |
| 100–200 | 1.856 | 1.213 | 1.53× |

**The amplifier alone is off its own datasheet.** The manual specifies 8 mV p-p
at 1 kHz bandwidth ≈ 1.3 mV RMS. Shorted, we measure **3.122 mV RMS over only
0–200 Hz** — a narrower band with more noise, ~2.4× worse than spec. The `E2`
gain jumper is the prime suspect and has never been read.

## The control makes this trustworthy

The laser shares the ADC, reference and supply but not the load path, so it
flags any change in ambient conditions between captures:

```
laser baseline   raw 0.696 mV   |  above 0.2 Hz: 0.623 mV
laser shorted    raw 1.316 mV   |  above 0.2 Hz: 0.616 mV
```

Its raw RMS looks 1.9× worse, but **above 0.2 Hz the two are identical** and
the spectra lie exactly on top of each other (right panel of the figure). The
whole difference is slow drift below 0.2 Hz — thermal/mechanical settling, not
noise. So conditions were the same and the load-cell comparison is valid.

*Always check the control before trusting a difference; the raw RMS alone would
have suggested the whole rig had changed.*

## A test that FAILED, kept so it is not repeated

`h7_shorted_floating_INVALID.csv` — first attempt, with the **load cell
disconnected** and SIG+/SIG− shorted to each other.

Result: **30.85 mV RMS, 6× WORSE than baseline**, DC offset shifted from 3.8 mV
to 280 mV.

**Why it is invalid:** an instrumentation amplifier needs a DC return path for
its input bias currents (`Bias Current ±0.3 nA`, `Common Mode Resistance
100 GΩ`). With the bridge connected, each input sits at the midpoint of a
~350 Ω leg tied to the excitation rails. Disconnect it and short the inputs
*to each other only*, and the pair floats — the common-mode voltage wanders and
the amplifier picks up everything nearby. That is drift, not amplifier noise.

**If you ever need the load cell truly out of circuit**, add a resistor
(~350 Ω to 10 kΩ) from the shorted SIG pair to EXC− / signal common to restore
the bias path. Or better, substitute a dummy 4×350 Ω bridge, which also gives a
representative source impedance.

## Files

| file | what |
|---|---|
| `h7_shorted.csv` | the valid capture — bridge connected, SIG shorted at amp |
| `h7_shorted_floating_INVALID.csv` | the failed attempt, kept as a counter-example |
| `fig_shorted_vs_baseline.png` | spectra, load cell + laser control |
| `compare_shorted.py` | regenerates the figure and the band table |
| `capture_quiet.py` | acquisition (now force-pulls and aborts on a dead port) |

## Where this leaves the noise work

Ranked by payoff per effort:

1. **Ensemble-average across cycles.** Broadband noise averages as √N — 10
   cycles ≈ 3.2×, and unlike a low-pass it costs **no time resolution**. A 10 Hz
   filter gives the same 3.1× but leaves only ~2 independent points across a
   100 ms transient, against ~40 for averaging. Free, model-free, and the
   natural thing to do in cyclic characterisation.
2. **Read the `E2` gain jumper** — the amplifier is 2.4× off datasheet and we
   still do not know how it is configured.
3. **Shielding / routing / 6-wire** on the sensor run — 57% of the variance.
4. **Do NOT bother with the `E3`/`E8` filter jumper.** Its lowest setting is
   100 Hz but 73% of the variance is below that, so it buys ~1.2×.
5. **Do NOT chase the ADC.** The laser reaches 0.62 mV RMS on the same
   converter — ~5× headroom over the load channel.

**Still unanswered: what force resolution do you actually need?** Without the
load-cell calibration (mV → N) and the forces the coils produce, we cannot say
whether 4.75 mV matters at all. Worth settling before spending on 2 or 3.

## Rig note

The hub faulted twice during this session (`rate2=0` with a crc storm, then a
completely dead port) with **no flashing involved**, which does not fit the
"accumulated DFU resets" explanation recorded on 2026-07-27. Both times a
USB + EVM power cycle recovered it. `capture_quiet.py` now force-pulls on open
and aborts if the port is not streaming, rather than burning 60 s on nothing.
