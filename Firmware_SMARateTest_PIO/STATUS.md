# STATUS — Firmware_SMARateTest_PIO

**Label: Diagnostic** — a throwaway fork of `Firmware_SMASensorHub_PIO` built to
answer one question: why does the SMA V/I/R stream capture only ~2 points during
a 100 ms fire, and how do we fix it?

**Round 1 bottom line (2026-07-09): SOLVED (15 → 99 Hz).** Round 2 (2026-07-13) pushed toward 1 kHz. **Round 3 (2026-07-27) re-ran the whole ladder and settled it: `portenta_m7_rate1k_n4` gives 957 Hz at 12 % ADC duty** — fast *and* below the accuracy knee. Rung 8 additionally measured a per-tick DAC write at 365 µs (and found it does not scale with I²C clock).

**Original round-1 finding:** The bottleneck was NOT the ADC — it was the
M7 bridge loop doing many tiny USB-CDC writes. A batched `Serial.write()` took
the SMA stream from **15 Hz → 99 Hz (~1.5 → ~9.9 points per fire)**. The fix is
ready to port to production; see **Next**.

---

## The question

In session `console_20260708_145312`, `sma_v/i/r` streamed at ~7 Hz, so a 100 ms
fire captured just ~2 points — too few to resolve the actuation transient.
Initial hypothesis: `readSma()` (2×64 software-averaged mbed `analogRead()`s) was
too slow. That could not be confirmed offline — the SRAM4 ring decouples the M4
clock from the M7 block, and host timestamps carry ~15 ms Windows scheduler
jitter that swamps the signal. So this fork measures the timing **on-device**.

## What was built

- **`micros()` timing** around `readSma()`, emitted as a `[RATE] n=<N>
  readSma_us=<µs>` line (host parser drops any `[`-line, so the sample stream
  stays clean).
- **`ADC_SAMPLES` split** into `ADC_SAMPLES_IDLE` (64) / `ADC_SAMPLES_CYCLE`
  (build flag) — the lever for the *initial* hypothesis. **Proven irrelevant
  (see below); do not port.**
- **`rate_probe.py`** — drives the port programmatically (you cannot hand-type
  into the ~1 kHz sensor flood): sends `arm` + `cycle`, **pings 1/s to satisfy
  the cycle watchdog** (`wdt_ms=5000`), logs every raw line live, and prints a
  `readSma()` timing summary. This is the test harness; keep it.

## What was tested / measured

`rate_probe.py`, 10 cycles of `cycle 3.0 0.5 100 3000 10`:

**1. readSma() is ~2 ms and NOT the bottleneck** — halving-and-quartering the
sample count barely moves it (the 2 ms is the two `delay(1)` primes, not the
average):

| ADC_SAMPLES | readSma() median |
|---|---|
| 64 (idle baseline) | 2.47 ms |
| 16 (in-cycle)      | 2.08 ms |

**2. The real wall was the M7 bridge loop.** `pumpSensors()` printed ~64 sensor
lines/pass with ~6 `Serial.print()` calls each; every tiny USB-CDC write blocks
~1 ms on the mbed stack → ~66 ms/pass. `serviceSma()` streams once per pass, so
it inherited that ~15 Hz. At ~40 KB/s the link is <10 % utilized — it was
per-call overhead, not bandwidth (so a transport swap like UDP would NOT help).

## Fixes applied (M7 only; M4 untouched)

1. **Batched sensor write** — `pumpSensors()` now formats the whole drain batch
   into one buffer and pushes it with a **single `Serial.write()`** per pass
   (float formatted without printf-`%f`, which isn't linked on nano newlib).
   `SENSOR_BATCH` is a build flag.
2. **RPC-newline guard** — forces a newline after the RPC passthrough so the
   periodic `[ADC1]` PGA alarm (M4, ~1/s) can't concatenate with the next sensor
   line. (Pre-existing race, exposed by the tighter post-fix timing.)

## Result — verified on-device

| | before | after |
|---|---|---|
| SMA stream cadence | 66 ms | **10.1 ms** |
| SMA rate | 15 Hz | **99 Hz** (= `CYCLE_LOG_MS` ceiling) |
| points per 100 ms fire | ~1.5 | **~9.9** |
| readSma() median | 2.47 ms | 2.2 ms (unchanged, as expected) |

Float format spot-checked (laser/load/negatives, exactly 6 decimals; 51 k lines
validated). Both cores build clean.

---

# ROUND 2 (2026-07-13) — push toward 1 kHz.

> **STALE HEADER, CORRECTED 2026-07-27.** This section long said *"BUILT, NOT YET
> BENCH-RUN — every number below is a prediction."* That was already untrue:
> `platformio.ini`'s rungs 6-7 block documents runs 0-5 as **done**, hitting
> 962 Hz and failing the accuracy gate with duty correlating +1.000 against V
> error. Read the `.ini` alongside this file; where they disagree, the `.ini` is
> newer. Round 3 below re-ran the ladder and adds rung 8.

Round 1 raised the SMA stream to 99 Hz and left `CYCLE_LOG_MS` as the ceiling.
Round 2 attacks the three costs that gate it above that. **All six envs compile
clean; none has been on the rig yet — every number below is a prediction.**

## What was found in the code

| cost | what it was | fix |
|---|---|---|
| **~2000 µs** | `readADC()` did `delay(1)` — a FULL MILLISECOND — per channel, and `readSma()` calls it twice. This is the fixed ~2 ms that round 1 measured but never removed (it correctly proved the *averaging* wasn't the cost, then stopped). | `SMA_SETTLE_US` (default 50 µs) |
| **~3000 µs** | **`emitSmaSample()` never got round 1's batching fix.** It does 6 `Serial.print()` calls per line × 3 rows = ~18 tiny USB-CDC writes per sample, each blocking ~1 ms on the mbed stack. This is *literally the same bug* round 1 fixed in `pumpSensors()` — it was just never applied to the SMA path. | batch into one `Serial.write()` |
| **silent** | `streamSma()` stamped every line with `last_m4_hw_us` — M4's clock, which only advances every ~2 ms (500 SPS). Harmless at 100 Hz; above ~500 Hz consecutive samples would carry **identical timestamps** and the achieved cadence would be unmeasurable. | `SMA_STAMP_M7` (default 1) |
| ceiling | `CYCLE_LOG_MS` hard-coded to 10 | `CYCLE_LOG_MS_CFG` build flag |

Together those are ~5 ms of work per sample → a ~200 Hz ceiling, which is why
`CYCLE_LOG_MS = 10` (100 Hz) sat comfortably below it and never slipped.

## New instrumentation

- `[RATE] n=<N> readSma_us=<µs> emit_us=<µs> dt_us=<µs>` — **`dt_us` is the
  ACHIEVED cadence**, measured on-device between consecutive SMA samples. That is
  the number this whole exercise is about. The `[RATE]` line is appended into the
  same batch buffer as the sample rows, so it costs no extra USB write (at 1 kHz
  an unbatched diagnostic line would itself become the bottleneck — the very bug
  being fixed). `RATE_DECIM` thins it at high rates.
- `[STATUS] … loop_us_avg / loop_us_max / loop_hz` — the M7 loop period.
  **`serviceSma()` streams at most one sample per loop pass, so the loop rate is a
  hard ceiling on the SMA rate**, independent of `CYCLE_LOG_MS`.
- `rate_probe.py` now reports achieved cadence, where the time goes, stream health
  (`dropped`/`crc_err`), and an **accuracy check** on the V/I means.

## The rate ladder — climb IN ORDER, stop at the first failure

```
pio run -e portenta_m4              -t upload   # sampler, once (unchanged)
pio run -e portenta_m7_rate100      -t upload   # rung 0: control (today's behaviour)
pio run -e portenta_m7_rate100_fixed -t upload  # rung 1: both fixes, rate unchanged
pio run -e portenta_m7_rate200      -t upload   # rung 2: 200 Hz
pio run -e portenta_m7_rate500      -t upload   # rung 3: 500 Hz
pio run -e portenta_m7_rate1k       -t upload   # rung 4: 1 kHz, still 64x averaging
pio run -e portenta_m7_rate1k_n16   -t upload   # rung 5: 1 kHz, 16x (only if 4 fails)
python rate_probe.py --port COM8                # after EACH upload
```
Power-cycle USB + EVM after every upload (else the ADS1263 boots `ID=0x00`).

**Rung 0 first, and keep its output.** It is the control: the V/I means from rung
0 (settle = 1000 µs) are what every later rung must reproduce.

## What to watch — a bigger number is NOT automatically a win

1. **`dt_us` should approach `CYCLE_LOG_MS × 1000`.** If it plateaus *above* that,
   the M7 loop (`loop_us_avg`) is the wall, not the schedule.
2. **`dropped` / `crc_err` must stay 0.** If the ring starts dropping, the SMA path
   is starving the sensor drain — you bought SMA rate with laser/load data.
3. **The V and I MEANS must not move as `SMA_SETTLE_US` comes down.** This is the
   one failure mode that is silent: 1 ms of settling is wildly conservative for
   these low-impedance sources (LDO divider, INA296A op-amp output), but if the
   source has *not* settled, you get a faster reading that is simply **wrong**, and
   nothing in the stream will say so. `rate_probe.py` section [3] prints these
   means for exactly this comparison. If they shift, back `SMA_SETTLE_US` off.
4. **Rung 5's trade-off is a real question, not a formality.** Dropping 64× → 16×
   averaging costs √4 = 2× in noise *if the noise is white*. Our session data says
   it is **not** — σ(I) = 138 LSB even *after* 64× averaging, because the
   interferer is low-frequency and the 64 back-to-back reads all catch it at the
   same phase. So 16× may be nearly free. **Measure the V/I scatter, don't assume.**

## Honest expectation

readSma should fall ~2.47 ms → ~0.5 ms (64×) and the emit ~3 ms → well under
0.1 ms, which *should* clear a 1 ms budget. But the M7 loop period has never been
measured — that is what `loop_hz` is now for, and it is the most likely place for
1 kHz to fail. **500 Hz is the confident outcome; 1 kHz is the stretch.**

## Round 1 next-steps (superseded by the ladder above)

1. **Play with `CYCLE_LOG_MS` (`src/main.cpp`, currently 10 ms).** It is now the
   ceiling — the SMA stream sits right on it (99 Hz ≈ 1/10 ms). Lowering it (e.g.
   5 ms → ~200 Hz, ~20 pts/fire) is the next knob if you want finer resolution of
   the fire transient. Watch that (a) `readSma()` ~2 ms + loop work stays under
   the new period, and (b) the extra `[RATE]`/src=3/4/5 volume doesn't re-saturate
   USB-CDC. Re-run `rate_probe.py` after each change to confirm the cadence.
2. **Port to `Firmware_SMASensorHub_PIO`:** copy the batched-write `pumpSensors()`
   + the RPC-newline guard. **Do NOT port the `ADC_SAMPLES` split** — proven
   irrelevant. Leave `ADC_SAMPLES=64`.

## Reproduce

```
pio run -e portenta_m4       -t upload   # sampler (once) — power-cycle rig
pio run -e portenta_m7_cyc16 -t upload   # bridge + SMA   — power-cycle rig
python rate_probe.py --port COM8         # drive + capture + summarize
```
Rig gotchas inherited from SensorHub: power-cycle USB+EVM after every upload
(else ADS1263 boots `ID=0x00`); COM8 = H7, COM5 = Zaber.

---

# ROUND 3 (2026-07-27) — ladder re-run, plus rung 8 (per-tick DAC write)

Run with `rate_probe.py --vhigh 1.0 --thigh 100 --tidle 400 --cycles 8`
(gentler drive than the 3.0 V default: the respun LDO now outputs ~3.3 V for a
commanded 3.0 V, i.e. ~845 mA, and rate measurement does not need it).

| env | ADC_SAMPLES_CYCLE | readSma | dac_us | emit | cadence | rate | ADC duty |
|---|---|---|---|---|---|---|---|
| `portenta_m7_rate1k`     | 64 | 1.590 ms | -- | 0.059 ms | 1.723 ms | 580 Hz | 86% |
| `portenta_m7_rate1k_n16` | 16 | 0.494 ms | -- | 0.059 ms | 1.037 ms | **964 Hz** | 38% |
| `portenta_m7_rate1k_n4`  | 4  | 0.223 ms | -- | 0.059 ms | 1.045 ms | **957 Hz** | **12%** |
| `portenta_m7_rate1k_n4_dac`    | 4 | 0.223 ms | **365 us** | 0.060 ms | 1.040 ms | ~960 Hz | 12% |
| `portenta_m7_rate1k_n4_dac100` | 4 | 0.222 ms | **364 us** | 0.060 ms | 1.040 ms | ~960 Hz | 12% |

`readSma ~= 0.13 ms + n x 0.0228 ms` — the fixed part is two 50 us settles plus
the primes; 11.4 us/conversion matches round 2's measured 11.7 us.

**Rung 7 (n=4) is the config to ship.** 957 Hz AND 12% duty, below the 14% clean
reference point, so round 2's accuracy prediction holds: speed and accuracy align
here rather than trading off. `dropped=0`, `crc_err=0` throughout.

## Rung 8 — the per-tick DAC write is NOT expensive enough to matter

Added because `Firmware_SMAConstantCurrent_PIO` ran its loop at ~479 Hz with
ADC_SAMPLES_CYCLE=4 — *less* averaging than rung 7 — and the one thing CC does
every tick that this project never does is write the MCP4728.

It costs **365 us**, and the cadence stayed at 1.04 ms: still ~1 kHz with ~35%
headroom. So the DAC write did not explain CC's slowness. (It was
`Serial.connected()` at ~850 us a call — see that project's README.)

**`dac_us` did not change between 400 kHz and 100 kHz** (365 vs 364 us). So the
cost is fixed overhead in the Adafruit/Wire stack, not bus time — *or*
`Wire.setClock()` is a no-op on this core. Not separated. Worth knowing before
investing in a custom I2C driver, and it means CC's `portenta_m7_i2c100`
diagnostic env may be testing a knob that does nothing.

Uses `setDACrawNoSettle()`: this project's `setDACraw()` always blocks
`delay(2)`, so calling it per tick would have "reproduced" 479 Hz via the delay
and proved nothing. CC's control loop passes `settle=false`.

## Correction to round 1's conclusion

Round 1 said *"per-call overhead, not bandwidth (so a transport swap like UDP
would NOT help)"*. True for the bottleneck it faced (~384 tiny writes/pass) and
the batching fix was right. But it is not a general law: once batched, `emit`
is 59 us, and in `Firmware_SMAConstantCurrent_PIO` at ~160 KB/s the cost was
later measured to track BYTES, not calls (batching 4x fewer writes there left
total write time unchanged). **Check which regime you are in before batching.**
