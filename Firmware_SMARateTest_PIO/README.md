# Firmware_SMARateTest_PIO

**Diagnostic fork of `Firmware_SMASensorHub_PIO`.** Single purpose: find and fix
why the SMA `V/I/R` stream captured too few points per 100 ms fire, in isolation,
so production was never at risk. **See `STATUS.md` for the full session record**
(question → measurements → fix → result → next steps).

| round | goal | status |
|---|---|---|
| **1** (2026-07-09) | why only ~2 points per fire? | **SOLVED** — 15 → 99 Hz via a batched `Serial.write()` in `pumpSensors()`. Ported to production (`dde71ea`). |
| **2** (2026-07-13) | **push to 1 kHz** | **SOLVED — 962 Hz, 96 points per fire, accuracy restored** (`portenta_m7_rate1k_n4`). Along the way it exposed an ADC reference-droop bug that inflates V and I (but *not* R) with conversion duty — and which **production has been carrying all along (+7%)**. [Details](#round-2-2026-07-13--the-1-khz-test). |

> ### ⭐ The one idea to take away
>
> **Each individual answer is now built from 4 readings instead of 64, so any
> single answer is noisier. But you get 10× more of them, and you average them
> afterward on the PC — where averaging is free and doesn't drain the reference.**
>
> The averaging moved from *inside* the measurement (where it overworked the ADC
> and sagged its voltage reference) to *after* it (where it costs nothing). Same
> total averaging, 10× the time resolution — and, counterintuitively, the ADC ends
> up doing **fewer conversions per second than before** (9,620 vs 12,480) while
> reporting **ten times more samples**. [The full explanation](#-the-key-idea--move-the-averaging-off-the-adc).

## Round 1 outcome (2026-07-09): solved

The bottleneck was NOT the ADC (`readSma()` is only ~2 ms). It was the M7 bridge
loop doing ~64 tiny USB-CDC writes per pass (~1 ms each → ~66 ms/pass), which
gated the SMA stream to ~15 Hz. A **batched `Serial.write()`** in `pumpSensors()`
took it to **99 Hz (~9.9 points per fire)**. Next lever is `CYCLE_LOG_MS`; port
the batched write (NOT the ADC-sample split) back to production. Details in
`STATUS.md`.

## The problem it probed

Production streamed `sma_v/i/r` at ~7 Hz → a 100 ms fire captured only ~2 points.
The exact `readSma()` duration can't be recovered from logged sessions (the ring
buffer decouples the M4 clock; host timestamps carry ~15 ms Windows jitter), so
this fork times it on-device with `micros()` and drives it with `rate_probe.py`.

## What's different (M7 only — M4 is the stock sampler)

| | production | this fork |
|---|---|---|
| ADC sample count | fixed `ADC_SAMPLES=64` | `ADC_SAMPLES_IDLE=64` / `ADC_SAMPLES_CYCLE` (build flag) |
| in-cycle read | `streamSma(readSma())` | `streamSmaTimed(ADC_SAMPLES_CYCLE)` |
| timing | none | `[RATE] n=<N> readSma_us=<µs>` per read (monitor-only) |

`readADC/readLDO/readSma` gained an `nsamples` param (default 64, so every
manual/settle path is byte-for-byte unchanged). The `[RATE]` line contains `[`,
which the host sensor parser drops — the sample stream stays clean.

## Quick start

```
pio run -e portenta_m4       -t upload   # sampler (once) — then power-cycle
pio run -e portenta_m7_cyc16 -t upload   # 16-sample in-cycle — power-cycle
pio device monitor                       # 115200; arm+fire; watch [RATE] lines
```
Sweep `_cyc8 / _cyc4 / _cyc32` to map rate vs `sma_r` noise. Port the winning
`ADC_SAMPLES_CYCLE` back into `Firmware_SMASensorHub_PIO` — **do not run the rig
on this fork.**

---

# ROUND 2 (2026-07-13) — the 1 kHz test

**Status: DONE. Runs 0-7 all on the rig (2026-07-13).**

**Outcome: 962 Hz with 96 points per 100 ms fire, clean stream, accuracy restored
— `portenta_m7_rate1k_n4`.** The test also earned its keep by exposing an **ADC
reference-droop bug** that inflates V and I (but *not* R) with conversion duty, and
which **production has been carrying all along (+7% on V and I, ~15% on power)**.

Read [THE KEY IDEA](#-the-key-idea--move-the-averaging-off-the-adc) first — it is
the transferable lesson. Then
[runs 0-5](#results--runs-0-5-2026-07-13-on-the-rig) and
[runs 6-7](#rungs-6-7--results-1-khz-accuracy-restored).

## What round 2 changes

Round 1 got the stream to 99 Hz and left `CYCLE_LOG_MS` as the ceiling. Reading
the code turned up **~5 ms of work per SMA sample**, which is why 100 Hz never
slipped — it was sitting comfortably under a ~200 Hz wall:

| cost | what it was | flag |
|---|---|---|
| **~2000 µs** | `readADC()` does `delay(1)` — a *full millisecond* — per channel, and `readSma()` calls it twice. Round 1 measured this fixed ~2 ms and correctly proved the *averaging* wasn't the cost, then stopped without removing it. | `SMA_SETTLE_US` (50) |
| **~3000 µs** | **`emitSmaSample()` never got round 1's batching fix.** 6 `Serial.print()` calls × 3 rows = ~18 tiny USB-CDC writes per sample, each blocking ~1 ms. This is the *same bug* round 1 fixed in `pumpSensors()`, never applied to the SMA path. | (always on) |
| **silent** | `streamSma()` stamped lines with `last_m4_hw_us` — M4's clock, which only advances every ~2 ms (500 SPS). Fine at 100 Hz; above ~500 Hz consecutive samples carry **identical timestamps** and the cadence becomes unmeasurable. | `SMA_STAMP_M7` (1) |
| ceiling | `CYCLE_LOG_MS` hard-coded to 10 | `CYCLE_LOG_MS_CFG` |

## New instrumentation

- **`[RATE] n=<N> readSma_us=<µs> emit_us=<µs> dt_us=<µs>`** — `dt_us` is the
  **achieved cadence**, measured on-device between consecutive SMA samples. This
  is the number the test is about. The line is appended into the *same* batch
  buffer as the sample rows, so it costs no extra USB write (at 1 kHz an
  unbatched diagnostic line would itself become the bottleneck — the very bug
  being fixed). `RATE_DECIM` thins it at high rates.
- **`[STATUS] … loop_us_avg loop_us_max loop_hz`** — the M7 loop period.
  `serviceSma()` streams at most **one sample per loop pass**, so the loop rate is
  a *hard ceiling* on the SMA rate regardless of `CYCLE_LOG_MS`. This has never
  been measured, and it is the most likely place 1 kHz fails.

## Test procedure

**Setup:** rig powered, EVM supply on, SMA connected, COM8 = H7. Nothing else
needs to be running — `rate_probe.py` drives the port itself (you cannot hand-type
into the ~1 kHz sensor flood; it sends `arm` + `cycle`, pings 1/s to satisfy the
`wdt_ms=5000` watchdog, and logs every line).

Flash the sampler **once**:
```
pio run -e portenta_m4 -t upload      # then POWER-CYCLE USB + EVM
```

Then climb the ladder **in order**, and after **every** upload power-cycle USB +
EVM (else the ADS1263 boots `ID=0x00`):

```
pio run -e <env> -t upload
python rate_probe.py --port COM8 --out round2_<env>.log
```

| # | env | rate | what it isolates |
|---|---|---|---|
| **0** | `portenta_m7_rate100` | 100 Hz | **CONTROL.** Today's behaviour (settle 1000 µs, unbatched-equivalent), but with the new instrumentation. **Keep this output — it is the reference every later rung is checked against.** |
| 1 | `portenta_m7_rate100_fixed` | 100 Hz | The two fixes only, rate unchanged. `readSma_us` and `emit_us` should both collapse while `dt_us` stays at 10000. Proves the saving without changing anything else. |
| 2 | `portenta_m7_rate200` | 200 Hz | First real rate increase. |
| 3 | `portenta_m7_rate500` | 500 Hz | Still full 64× averaging — precision preserved. |
| 4 | `portenta_m7_rate1k` | 1 kHz | The target, still 64× averaging. |
| 5 | `portenta_m7_rate1k_n16` | 1 kHz | Only if rung 4 cannot hold the cadence. Trades averaging for time. |

**Stop at the first rung that fails a check below.** A rung that "works" but fails
check 3 is worse than no change at all.

## Pass / fail — a bigger number is NOT automatically a win

Read these off `rate_probe.py`'s summary after each rung:

1. **Cadence.** `dt_us` should approach `CYCLE_LOG_MS × 1000`. If it plateaus
   *above* that, the schedule is not the wall — look at `loop_us_avg`. If
   `loop_hz` ≈ the achieved rate, **the M7 loop is the ceiling** and no further
   `CYCLE_LOG_MS` reduction will help.
2. **Stream health.** `dropped` and `crc_err` must stay **0**. If the ring starts
   dropping, the SMA path is starving the sensor drain — you bought SMA rate with
   laser/load data, which is not a trade we want.
3. **Accuracy — the silent failure. THIS IS THE ONE THAT BIT US.** ⚠ **The V and I
   *means* must not move.** Compare against rung 0 at the same DAC code — the
   `src=3` raw column carries `currentCode`, so you can confirm the drive is
   identical before blaming the measurement.

   Runs 0-5 showed the settle reduction is **safe** (rung 1: −0.1%), but that V and
   I inflate by up to **+33%** as the **ADC conversion duty** rises. **Do not check
   R and call it a pass** — R = V/I is *immune* (both channels scale together and
   the ratio cancels), and it sat at a rock-steady 21.4 Ω through every run while
   V was drifting by a third. Check **V and I separately, against the DAC code.**
4. **Rung 5's trade is a real question, not a formality.** Dropping 64× → 16×
   averaging costs √4 = **2× noise if the noise is white**. Our session data says
   it is **not**: σ(I) = 138 ADC LSB even *after* 64× averaging, because the
   interferer is low-frequency and all 64 back-to-back reads catch it at the same
   phase. So 16× may be nearly free. **Compare the V/I scatter (`sd`) against rung
   4 — measure it, don't assume it.**

## RESULTS — runs 0-5 (2026-07-13, on the rig)

Logs: `run0.txt` … `run5.txt`. Bench load was ~21.4 Ω (not the 4.3 Ω coil).

### Gates 1 & 2 — PASSED. 962 Hz achieved, stream perfectly clean.

| rung | config | achieved | pts/fire | readSma | emit | dropped | crc |
|---|---|---|---|---|---|---|---|
| 0 control | 10 ms, settle 1000, N=64 | 96 Hz | 9.6 | 3.49 ms | 0.06 ms | 0 | 0 |
| 1 fixed | 10 ms, settle 50, N=64 | 96 Hz | 9.6 | **1.60 ms** | 0.06 ms | 0 | 0 |
| 2 | 5 ms | 193 Hz | 19.3 | 1.60 ms | 0.06 ms | 0 | 0 |
| 3 | 2 ms | 482 Hz | 48.2 | 1.59 ms | 0.06 ms | 0 | 0 |
| 4 | 1 ms, N=64 | 580 Hz | 58.0 | 1.59 ms | 0.06 ms | 0 | 0 |
| **5** | **1 ms, N=16** | **962 Hz** | **96.2** | 0.50 ms | 0.06 ms | 0 | 0 |

Both fixes did exactly what was predicted: the two `delay(1)` calls were worth
**1.9 ms** (readSma 3.49 → 1.60 ms), the batched emit is negligible (0.06 ms), and
**nothing was dropped at any rate**. Rung 4 stalled at 580 Hz only because readSma
at N=64 (1.59 ms) does not fit a 1 ms budget — rung 5 (N=16, 0.50 ms) clears it.

Empirical cost model, fits all six runs to within 2%:
```
readSma_us  =  2 x SMA_SETTLE_US  +  2 x N x 11.7 us
```
(so `analogRead()` costs ~11.7 µs, not the ~4 µs estimated from round 1's delta.)

### Gate 3 — **FAILED.** The measurement inflates with sample rate.

**The DAC code was IDENTICAL (2003) in every run** — the drive never changed. Yet:

| rung | cadence | ADC duty | V measured | error |
|---|---|---|---|---|
| 0 / 1 | 10.4 ms | 14% | 3.211 V | — (reference) |
| 2 | 5.2 ms | 29% | 3.427 V | **+6.7%** |
| 5 | 1.0 ms | 38% | 3.561 V | **+10.9%** |
| 3 | 2.1 ms | 72% | 4.082 V | **+27.1%** |
| 4 | 1.7 ms | 86% | 4.279 V | **+33.3%** |

**correlation(ADC conversion duty, voltage error) = +1.000.**

Rungs 4 and 5 share the **same 1 ms cadence** but differ in N — and differ in
error. So the error tracks **conversions, not rate**.

**Mechanism.** `code = Vin × FS / Vref_actual`, but the firmware divides by an
*assumed* `ADC_VREF_V = 3.145 V`. Under heavy conversion duty the reference sags,
codes read high, and the computed voltage inflates.

**Why it nearly escaped notice: `R = V/I` stayed pinned at 21.2–21.5 Ω in every
single run.** Both channels inflate by the *same* factor, so the ratio cancels
exactly (`ISENSE_OFFSET_V = 0`). Checking R alone would have called this a pass.

**What is and isn't affected:**
- ✅ **Existing 100 Hz production data is FINE** — 14% duty, in the clean zone.
- ✅ **The settle fix alone is SAFE** (rung 1: −0.1% shift). Portable to production today.
- ✅ **R is immune** — exact common-mode cancellation. Rung 5's resistance data is usable.
- ❌ **V, I are wrong** by up to +33%.
- ❌ **POWER is badly wrong** — P = V·I squares the error: **+33% V → +78% P.**

## Rungs 6-7 — RESULTS: 1 kHz, accuracy restored

Logs: `run6.txt`, `run7.txt`. Figure: `rate_ladder_results.png`
(regenerate with `python plot_rate_results.py`).

| run | N | cadence | pts/fire | ADC duty | V (fire) | vs run0 | dropped/crc |
|---|---|---|---|---|---|---|---|
| run0/1 | 64 | 10.4 ms | 9.6 | 14% | 3.211 V | reference | 0 / 0 |
| run5 | 16 | 1.04 ms | 96 | 38% | 3.561 V | +10.9% | 0 / 0 |
| run6 | 8 | 1.04 ms | 96 | 20% | 3.292 V | +2.5% | 0 / 0 |
| **run7** | **4** | **1.04 ms** | **96** | **12%** | **3.156 V** | **−1.7%** | 0 / 0 |

Predictions were +2.2% and "≤ 3.21 V". Measured **+2.5%** and **3.156 V**. Both hold
**962 Hz with 96 points per fire**, stream perfectly clean.

**`portenta_m7_rate1k_n4` (run7) is the winner.**

### The mechanism, now proven

Fitting all eight runs (DAC code 2003 throughout — the drive never changed):

```
V_measured = 0.01508 x duty% + 2.988          R2 = 0.9996
```

Extrapolate to **zero ADC duty -> 2.988 V**. The DAC/LDO model says code 2003
commands **3.000 V**. Two independent numbers agreeing to **0.3%**. The line passes
through the true value exactly where the ADC stops being busy.

### ⚠ The finding we were only half-expecting

**Production (14% duty) reads +7% high on V and I.** This is *pre-existing*, not
something the rate work introduced — it was invisible because R cancels it. For
run7 to read the commanded 3.000 V, `ADC_VREF_V` would have to be **~2.99 V**, not
the **3.145 V** the firmware assumes. So there are two stacked errors: a standing
~5% mis-calibration of `ADC_VREF_V`, plus the duty-dependent sag on top.

**run7 at 1 kHz is therefore MORE accurate than production at 100 Hz** (12% duty
vs 14%).

---

# ⭐ THE KEY IDEA — move the averaging off the ADC

**This is the one thing to remember from round 2.**

`readADC()` never took *one* reading — it took **N readings and averaged them**
(software oversampling, to beat down noise), and `readSma()` calls it twice (V on
A0, I on A1). So per SMA sample:

```
N=64:  2 channels x (1 throwaway + 64 readings) = 130 conversions  -> 1.5 ms ADC on-time
N=4:   2 channels x (1 throwaway +  4 readings) =  10 conversions  -> 0.12 ms
```

**Each individual answer is now built from 4 readings instead of 64, so any single
answer is noisier. But you get 10x more of them, and you average them afterward on
the PC — where averaging is FREE and doesn't drain the reference.**

That is the whole trick. The averaging moved from **inside** the measurement (where
it overworked the ADC and sagged the reference) to **after** it (where it costs
nothing). Same total averaging, 10x the time resolution, and a reference that gets
to recover between conversions.

### The counterintuitive part: the ADC is doing LESS work, not more

Two rates move in **opposite** directions. Do not conflate them:

| | production | run7 |
|---|---|---|
| answers reported per second | 96 | **962** (10x faster) |
| conversions *per answer* | **130** | **10** |
| => conversions per second | 12,480 | **9,620** (slightly FEWER) |
| => ADC busy | 14.6% of the time | **11.3%** |

**run7 samples 10x faster than production while performing FEWER conversions per
second than production does.** Each individual conversion still takes the same
~11.7 us — nothing about the ADC got faster. Each *answer* just got 13x cheaper.

### Why the ADC being busy inflates the reading

A SAR ADC is a balance scale doing a binary search against reference "weights" (a
capacitor array charged from Vref). Every conversion **pulls charge out of the
reference**, which sits behind a decoupling cap and a regulator — a bucket that
refills at a fixed rate.

- Convert occasionally -> the bucket refills between conversions -> weights full-size -> reading correct.
- Convert back-to-back -> draining faster than it refills -> **Vref sags** -> the weights are *lighter than stamped*.

**Lighter weights means you need more of them to balance — so the object reads
HEAVY.** The ADC emits a bigger code; the firmware multiplies by the Vref it
*assumes* (3.145 V); out comes an inflated voltage.

We did **not** slow the ADC down and we did **not** change the reference — the
refill rate is fixed by the hardware. We reduced how fast we **drain** it.

And because it scales BOTH channels identically, **R = V/I cancels it exactly** —
which is why R sat at a rock-steady 21.4 Ω through all eight runs while V drifted
by a third, and why checking R alone would have called this a clean pass.

**Honesty note:** the *behaviour* is nailed down (error ∝ ADC on-time, R² = 0.9996,
fix verified on the bench). The *mechanism* — reference/supply droop specifically —
is inferred from that behaviour; Vref was never measured directly. Reading the
STM32's internal **VREFINT** channel would confirm it outright, and would let the
firmware self-correct instead of trusting a hard-coded 3.145 V. **That is the
natural next rung.**

---

## The framing that still holds

**Rate buys transient SHAPE, not resistance PRECISION.** At 1 kHz you get ~96
points inside a 100 ms fire instead of 8 — enough to see the rise and any R-phase
feature. Every one of those points still carries the same ~6% noise, which comes
from the current-sense front end (only 3.7% of ADC range at the idle probe) and is
a **separate** fix.

## Rig gotchas (inherited)

- **Power-cycle USB + EVM supply after every upload** or the ADS1263 boots
  `ID=0x00`.
- **COM8 = Portenta H7, COM5 = Zaber.** Override with `--upload-port COMx`.
- **Do not run the rig on this fork.** It is a diagnostic. Port the winning flags
  back into `Firmware_SMASensorHub_PIO` (which both
  `Experiment_SMAThermalCharacterization` and `Experiment_SMACharacterizationV3`
  share).
