# LDO_Characterization — Scope Trigger Debug Handoff

> **Status:** capture pipeline works; **single-shot trigger does not fire in the
> automated run** even though the trigger signal is physically present on C1.
> This is the one remaining blocker. Everything else end-to-end is verified.
>
> **2026-06-16 — ROOT CAUSE FOUND + FIXED IN CODE (level now arms at 1.0 V;
> end-to-end bench fire pending).** The level set was being silently clobbered,
> so the scope armed with the threshold at **0 V**. With the trigger pin idling
> at 0 V (logic LOW = ground) there's no clean LOW→HIGH crossing → arms, never
> fires. Three compounding SCPI traps, all isolated by controlled A/B:
>   1. **`*OPC?` drops the level set.** Sending `*OPC?` right after `C1:TRLV`/
>      `:TRIGger:EDGE:LEVel` reverts the level to 0.00E+00. `arm_single` used
>      `*OPC?` to "sync" — that was THE bug. Now uses a short sleep; level sticks.
>   2. **Level clamps to the trigger channel's VDIV** (~±4 × V/div). `arm_single`
>      now pins `C1:VDIV 1V` (and verifies) before setting the level.
>   3. **Back-to-back writes get dropped** — config writes now have a ~0.12 s
>      settle each (`_w`/`_set_vdiv`) and critical values are read back.
> Verified: after `arm_single`, `C1:TRLV? = 1.00E+00`, `SAST? = Arm`. Also: INR?
> cleared at arm + `wait_capture_complete` no longer treats a never-armed
> `SAST?='Stop'` as success. **TODO: one bench run with the H7 firing to confirm
> the edge actually triggers + captures the step.** Note `analyze_ldo` quirks #2/
> #5 below (TDIV/TRLV value+unit) still hold; see memory `sds2000x-plus-scpi-traps`.

/ scope = SDS2204X Plus (fw 5.4.1.5.2R2) @ 169.254.111.4:5025 / H7 = SMA_Driver_PIO on COM8 /

---

## TL;DR — the open issue

The firmware `fire` pulses a clean **0→3.3 V edge on scope C1** (visually
confirmed on the bench). But when `run_experiment.py` arms the scope single-shot
and fires, the scope **arms (`SAST?='Ready'`) and never triggers** → timeout WARN
→ it captures a *stale* frame (TRIG low, LDO at ~0.36 V floor) instead of the step.

Tried both trigger-config dialects, same result:
- Legacy `TRSE EDGE,SR,C1,HT,OFF` + `C1:TRLV 1.0V` + `TRMD SINGL` + `ARM`
- Modern `:TRIGger:TYPE EDGE` + `:TRIGger:EDGE:SOURce C1` + `:SLOPe RISing` +
  `:LEVel 1.0` + `:TRIGger:MODE SINGle` + `:TRIGger:RUN`  ← current code

**The single most useful untried test:** set the scope **manually** (front panel)
to single-shot, edge, source C1, rising, level 1 V, then `fire` from the serial
console and see if it triggers. That cleanly splits "scope can't trigger on this
edge" from "our SCPI arming sequence is wrong." It has not been done yet.

---

## What is CONFIRMED WORKING (do not re-debug)

1. **Firmware / LDO / DAC** — perfect. `fire` replies show `V_final` = 1.004,
   2.490, 5.019 V vs predicted 1.000/2.500/5.000 — the LDO steps and settles
   exactly as commanded. Analytical model is spot-on.
2. **Trigger signal on C1** — firing from the serial console makes **C1 visibly
   jump to ~3.3 V** for the hold duration, then drop. So PJ_11 (= Mid Carrier
   PWM4 = J2-67) is pulsing and is correctly wired to scope C1.
3. **Scope connection** — pinned `host: 169.254.111.4` in `config.yaml`, connects
   instantly, `*IDN?` OK.
4. **Multi-channel waveform capture** — C1/C2/C3 all return 10000 samples **when
   the scope is STOPPED** (free-running reads race the buffer; see fixes below).
5. **`scope_probe.py`** is the diagnostic harness — connects, enables channels,
   sets MDEP/TDIV, STOPs, reads all 3 channels, dumps status + trigger config.

## What the CURRENT (broken) run produces

- All 24 settling CSVs are written, 10000 rows each.
- But every capture is a **stale floor frame**: `v_trig` ≈ ±40 mV (no edge),
  `v_out` ≈ 0.32–0.40 V (LDO floor, no step), `edge idx: NONE`.
- `summary.csv`: `span_v ≈ 0.3 mV`, settle = NaN, overshoot = garbage — because
  there's no transient in the data.
- `scope_complete=True` is **misleading**: when the scope is just sitting
  stopped/unarmed, `SAST?` returns `Stop` instantly and the poll "succeeds".

---

## Bugs already fixed along the way (SCPI gotchas — keep these!)

All of these are real and already corrected in the code. They're also saved in
the user's memory file `sds2000x_plus_scpi_quirks.md`.

1. **Channels must be enabled** before capture/trigger: `Cn:TRA ON`. An
   undisplayed channel returns an empty `WF?` block and makes `PAVA?` hang.
   → `scope_trigger.enable_channels()`.
2. **`TDIV` set needs value+UNIT**, not a bare float. `TDIV 0.02` is silently
   ignored (scope stuck at 20 s/div → never fills → never triggers). Must send
   `TDIV 20MS`. → `scope_trigger.siglent_time()`. (Same trap as the trigger
   level — see #5.) Set `ACQ:MDEP` **first**, `TDIV` last (MDEP perturbs TDIV).
3. **`WF? DAT2` block ends in a DOUBLED newline `\n\n`** — the binary-block
   reader must drain the full terminator or the next query desyncs and the
   2nd/3rd channel `WF?` returns 0 samples. → fixed in
   `../SiglentOscillosope/oscilloscope.py` `_query_block` (non-blocking drain).
4. **Multi-channel `WF?` must be read from a STOPPED frame.** Free-running
   (`AUTO`) overwrites the buffer mid-read; whichever channel is read first wins,
   the rest come back empty (flips run-to-run by timing). → `scope_trigger.stop()`
   + `STOP` before capture; the probe STOPs before reading.
5. **`Cn:TRLV` set needs value+UNIT** too: bare `C1:TRLV 1.0` was rejected and
   left the level at ~17 mV (in the noise floor → erratic/no trigger). With
   `1.0V` it reads back `1.00E+00`. (Now using `:TRIGger:EDGE:LEVel` instead.)
6. **Memory depth**: `ACQ:MDEP {10K|100K|1M|10M|100M}` (multi-channel set), else
   `WF?` pulls the full 10 Mpt. Using `10K` → ~28 µs/sample over the 280 ms window.
7. **`ACQ:MDEP?` returns `10k`, `SAST?` returns `Arm`/`Ready`/`Trig'd`/`Stop`,
   `INR?` bit0 = "new signal acquired" (`8193` = bits 0 and 13).**
8. **Firmware must be flashed with `TRIG_PIN = PJ_11`** (was `D4` earlier). It is
   now. Power-cycle after every upload.

---

## Diagnostic evidence (most recent good probe run)

```
SARA?: 5.00E+04 | TDIV?: 2.00E-02 | MDEP?: 10k
  C1: 10000 samples   C2: 10000 samples   C3: 10000 samples   (after STOP)
SAST? -> 'Stop'   INR? -> '8193'   TRMD? -> 'STOP'
# legacy trigger readback:
TRSE? -> <err timed out>            <-- legacy TRSE query does NOT work on this fw
C1:TRLV? -> '1.67E-02'              <-- bare '1.0' rejected; '1.0V' -> '1.00E+00'
C1:TRSL? -> 'POSitive'
```

Firmware `fire` replies (all correct):
```
small_up r0: V_final=1.0037V V_pred=1.0004V
mid_up   r0: V_final=2.4896V V_pred=2.5006V
large_up r0: V_final=5.0187V V_pred=5.0001V
```

---

## Next steps to try (in priority order)

1. **Manual bench test (do this first).** Front-panel: single-shot, edge, src C1,
   rising, 1 V. Run `fire 1631 3000 141` from `pio device monitor`. Does the scope
   trigger and freeze on the edge?
   - **Yes** → the SCPI *arming sequence* is the problem (go to #2/#3).
   - **No** → deeper trigger issue: check trigger **coupling** (DC, not HFREJ/LFREJ),
     trigger **holdoff** off, and that C1 isn't on some weird coupling. The edge is
     fast (<1 µs) but stays high 500 ms, so an edge trigger must catch it.

2. **Verify the SCPI source actually selects C1.** Run `scope_probe.py` (already
   updated to print `:TRIGger:EDGE:SOURce?` etc.). If `SOURce?` is not `C1`, the
   source command form is wrong for this fw — try `:TRIGger:EDGE:SOURce CHANnel1`
   or check the exact token in the Programming Guide PG01-E11A.

3. **Arming sequence.** Confirm `:TRIGger:MODE SINGle` + `:TRIGger:RUN` actually
   arms (read `:TRIGger:STATus?` / `SAST?` right after — should be `Ready`/`Arm`).
   Try `:TRIGger:MODE SINGle` alone (no RUN), or `:TRIGger:MODE NORMal` to see if
   it triggers continuously (NORMal is easier to debug than single).

4. **Timing race.** `arm_single` → `sleep(0.2)` → `h7.fire()`. Inside `fire` the
   firmware does `settleWait()` (~0.1–2 s) BEFORE raising the edge, so the scope
   is armed well before the edge — *should* be fine, but worth confirming the arm
   persists (read status just before the edge).

5. **Drop the poll (user's suggestion).** `h7.fire()` BLOCKS until the firmware
   finishes the whole hold, so by the time it returns the single-shot has already
   triggered+stopped (if it was going to). `wait_capture_complete`'s long poll is
   pointless and produces false WARNs/timeouts. Once triggering works, replace it
   with: fire → short wait for `SAST?=Stop` (≤2 s) → `STOP` → capture.

6. **Fallback if the trigger pin path stays uncooperative:** trigger on the **LDO
   output (C3) or DAC node (C2)** rising edge instead of the separate pin. Needs a
   per-step level/slope (e.g. level 0.7 V rising for up-steps; falling for
   `large_dn`), but removes all dependence on the trigger-pin arming.

---

## Known TODOs AFTER triggering works

- **Vertical clipping:** channels are at **1 V/div, 0 offset** → only ±4 V on
  screen, so the **5 V `large_up` step clips** (~4 V flat-top). Set per-channel
  V/div + offset in `configure_timebase` so 0–5 V fits (verify the `Cn:OFST` sign
  from a readback). `small_up`/`mid_up` are fine.
- **`codes_per_div`** in the scope module is `25` ("verify against Programming
  Guide"). Once a real step is captured, compare its height to the firmware
  `V_final` and trim if the absolute volts are off. (Settle *time* is unaffected.)
- **Current channel** is intentionally OFF (`channels.current: null`, INA296A
  physically disconnected). Re-enable (`C4`) when the current sense is wired.
- **Ripple pass** uses `AUTO` + `read_burst` (PAVA, free-running is fine there) and
  prints `k/N` progress — untested end-to-end because the run never reached it.

---

## Key files

| File | Role |
|---|---|
| `scope_trigger.py` | `arm_single` (trigger config — **the thing under debug**), `enable_channels`, `stop`, `siglent_time`, `wait_capture_complete`, `capture_channel_volts` |
| `run_experiment.py` | orchestrator: per shot → `arm_single` → `h7.fire` → wait → `stop` → capture C1/C2/C3 |
| `scope_probe.py` | bench diagnostic (no H7 needed): channels, MDEP/TDIV, STOP, capture, status + trigger readback |
| `h7_serial.py` | serial wrapper (`mosfet`, `code`, `fire`) |
| `config.yaml` | host/port, channel map (C1 trig / C2 dac / C3 out / current null), step matrix |
| `../SiglentOscillosope/oscilloscope.py` | shared driver; `_query_block` terminator-drain fix lives here |
| `../SMA_Driver_PIO/src/main.cpp` | firmware: `fire <code_to> [ms] [code_from]`, `TRIG_PIN = PJ_11` |

## Commands

```
# bench diagnostic (no firmware needed)
python scope_probe.py

# full run
python run_experiment.py
python analyze_ldo.py data/ldo_<timestamp>     # re-plot a run

# firmware console (close before running Python — COM8 is shared)
pio device monitor --echo
#   mosfet off
#   fire 1631 3000 141      # 0.5V baseline -> 2.5V, 3s hold (watch C1 for the edge)
```
