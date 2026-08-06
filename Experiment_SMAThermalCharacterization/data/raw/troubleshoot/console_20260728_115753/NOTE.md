# This session recorded NO DATA — 2026-07-28 11:57

`h7.csv`, `stage.csv` and `status.csv` are **header-only**. Do not analyse this
session; there is nothing in it.

## What happened

```
11:57:58  Opened COM8 but no sample lines seen in 4.0s boot window
11:58:08  health H7 FAIL (0) — only 0 samples (need >=20)
11:58:19  H7 command 'disarm' failed: Write timeout      x344
11:58:40  ERROR H7 worker crashed: ClearCommError (PermissionError 13)
11:58:44  H7: reconnect requested
11:58:49  H7 ready — streaming resumed
12:01:06  H7 worker exit (pushed=26562, dropped=0, filtered=0)
```

The H7 was wedged at launch, so the critical health check failed and
`operator_console.py` never called `start_recording()`. The worker then crashed,
auto-reconnected, and the H7 came back healthy — **26 562 samples, zero
dropped** — but the recording gate had been decided once, at launch, with no way
back. All of it was discarded after feeding the live plots. The stage lost
8 311 samples the same way.

The live plots kept working throughout (the drain feeds them regardless of the
recording gate), which is why this was invisible at the bench.

## Fixed

* **`39820ee`** — the core now DEFERS instead of abandoning: recording starts
  automatically once the H7 delivers samples, and every outcome is logged to
  `session.log` + `events.csv` rather than only the GUI panel. The DATA
  indicator shows a red `DATA ✗ WAITING` while deferred.
* **`dc5ed9d`** — `portenta_reader.open()` now **force-pulls** the CDC buffer
  before listening. A previous session that exits without draining leaves the
  M7's USB-CDC TX buffer full; the firmware finds no room and the port looks
  dead. Draining it revives the stream with no reset and no power cycle — which
  is exactly what recovered this port afterwards.

## The data you actually want

The noise characterisation this session was meant to produce was captured
separately once the port was revived:

→ [`../noise_20260728_125440_quiet_baseline/`](../noise_20260728_125440_quiet_baseline/)
