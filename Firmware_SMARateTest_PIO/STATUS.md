# STATUS — Firmware_SMARateTest_PIO

**Label: Diagnostic** — a throwaway fork of `Firmware_SMASensorHub_PIO` built to
answer one question: why does the SMA V/I/R stream capture only ~2 points during
a 100 ms fire, and how do we fix it?

**Bottom line (2026-07-09): SOLVED.** The bottleneck was NOT the ADC — it was the
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

## Next

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
