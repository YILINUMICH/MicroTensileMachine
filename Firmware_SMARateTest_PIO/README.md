# Firmware_SMARateTest_PIO

**Diagnostic fork of `Firmware_SMASensorHub_PIO`.** Single purpose: find and fix
why the SMA `V/I/R` stream captured only ~2 points per 100 ms fire, in isolation,
so production was never at risk. **See `STATUS.md` for the full session record**
(question → measurements → fix → result → next steps).

## Outcome (2026-07-09): solved

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

## Rig gotchas (inherited)

- **Power-cycle USB + EVM supply after every upload** or the ADS1263 boots
  `ID=0x00`.
- **COM8 = Portenta H7, COM5 = Zaber.** Override with `--upload-port COMx`.
