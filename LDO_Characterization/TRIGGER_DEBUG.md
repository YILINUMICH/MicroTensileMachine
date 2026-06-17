# LDO_Characterization — Scope Trigger Debug Handoff

> **2026-06-17 (run #8) — CORRECTION: run #7 triggered fine but the VOLTAGES were
> garbage (channel-digit parse bug). "Faster settling" was an ARTIFACT.** The
> `C2/C3 V/div did not confirm` WARN exposed a deeper bug: both `_first_float`
> (scope_trigger, used to confirm V/div) AND `_extract_num` (oscilloscope, used in
> the CAPTURE path to read VDIV/OFST for codes->volts) grabbed the FIRST number in
> a headered reply — i.e. the channel digit. `C2:VDIV 1.00E+00V` parsed as **2.0**,
> `C3:OFST 0.00E+00V` as **3.0**. So today's run computed `volts = code*(3/25) - 3`
> for C3 → negative baselines, `v_final` 4.3 V for a 1.0 V step, and a fake ~2 ms
> rise. Compared with yesterday (`ldo_20260616_201006`): plausible `v_final` near
> target, rise ~110-220 ms, large_up settle_1pct ~140 ms — **that ~200 ms is the
> REAL settle; do NOT shrink the window based on today's run.**
> **Fix:** both parsers now take the LAST number (the value, after the channel
> header); PAVA/ripple comma-form still works; `configure_timebase` pins `CHDR ON`
> so the expect= self-heal and headered parse stay consistent (ripple sets it OFF).
> **TODO: RE-RUN `run_experiment.py`** — expect `v_final` ≈ 1.0/2.5/5.0 V and the
> real ~100-200 ms settle. THEN (and only then) decide on window/timebase. The
> `codes_per_div=25` absolute-scale calibration is still open (yesterday's v_final
> ran ~15% high on small/mid) — trim it once a clean capture's plateau is compared
> to the firmware `V_final`.

> **2026-06-17 (run #7) — FULL CHAIN GREEN.** `diag_loop.py --capture` (the full
> shot incl C1/C2/C3 WF? reads): **20/20 triggers, 20/20 non-empty 10000-sample
> captures.** So trigger + arm + timing + comms/desync + the WF? read path are all
> solid end-to-end. Then fixed the last known issue — **C3 vertical clipping**:
> `scope_trigger.set_channel_range()` sizes the DAC/output channels PER STEP at
> zero offset (no OFST-sign risk) via `fit_vdiv()`; `run_settling` calls it each
> step. Result: small_up 0.5 V/div, mid_up 1 V/div, large 2 V/div — nothing rails,
> and small steps get FINER resolution than the old fixed 1 V/div.
> **READY FOR A REAL RUN: `python run_experiment.py`.** Watch for: (1) all CSVs
> non-empty (the comms+trigger fixes), (2) `large_up`/`large_dn` no longer flat-
> topped (the vertical fix). Open polish items: `codes_per_div=25` absolute-volts
> constant still unverified (affects final-value magnitude, NOT settle time); the
> ripple pass is still untested end-to-end.

> **2026-06-17 (run #6) — TRIGGER MISS ROOT-CAUSED + FIXED: pre-trigger buffer
> fill.** `diag_loop.py` (arm→fire→check, NO capture) isolated it: settle=0.2 s →
> **14/20** hits, settle=1.0 s → **20/20**. So the misses were never comms/desync/
> arm-config — a DSO can't trigger until its PRE-TRIGGER buffer fills, and with the
> trigger centered (TRDL 0) at 100 ms/div that's ~0.5 s of acquisition after
> `:TRIGger:RUN`. The firmware's variable `settleWait` sometimes landed the edge
> inside that fill window → silently ignored → ~30% random misses.
> **Fix:** `scope_trigger.arm_to_fire_delay_s(cfg)` = `5*TDIV*2.0` (≈1.0 s at
> 100 ms/div, scales with timebase); `run_experiment` and `diag_loop` now wait
> that long between `arm_single()` and `h7.fire()` instead of a fixed 0.2 s.
> **STATUS: arm + trigger + timing all SOLVED end-to-end.** Next: re-run the full
> `run_experiment.py`; remaining risk is only the WF?/desync path on real captures
> (`diag_loop.py --capture` will show if it's clean). Then fix C3 vertical clipping
> (5 V step rails at +127 on 1 V/div) before trusting `large_up`/`large_dn` data.

> **2026-06-17 (run #5) — ARM + TRIGGER PROVEN CORRECT; misses are an integration
> race, not trigger config.** `diag_arm.py` showed `arm_single()` produces a
> trigger config byte-for-byte identical to the known-good front-panel one
> (SOURce=C1, TYPE=EDGE, SLOPe=RISing, LEVel=1.0, COUPling=DC, MODE=SINGle) and a
> single arm→fire→check **TRIGGERS** (SAST=Stop). Also confirmed `h7.fire()` BLOCKS
> until `[FIRE] done` (h7_serial.py:165), so the edge has already happened by the
> time `wait_for_stop` runs — the long poll really was unnecessary. So the source-
> token theory was WRONG; `set_trigger_source` (added, verifies SOURce readback +
> token fallback) is now just harmless defense.
> **Remaining suspect:** SCPI desync from the WF? reads corrupting the NEXT shot's
> arming (run #4's misses clustered first-of-group-OK-then-degrade — the desync
> signature, and run #4 still had the broken non-blocking resync).
> **`diag_loop.py` added to settle it:** loops arm→fire→check N×, with/without the
> capture. `python diag_loop.py` (no capture) should be ~100% hits; if
> `--capture` drops the hit rate, the WF?/desync path is the culprit (driver fix),
> not arm/timing. RUN BOTH; the delta is the answer.

> **2026-06-17 (run #4) — self-heal made it WORSE; fixed two regressions; comms
> now robust but the TRIGGER MISS is the real blocker.** The `expect=` validation
> from run #3 backfired: (a) `resync()` was non-blocking, so it couldn't flush a
> 10k-byte WF block still in flight (the `\x7f`*N flood = C3 output railed at +127,
> the vertical-clipping issue) — retries churned on mid-block garbage; (b) the
> diagnostic `SAST?`/`INR?` read sat OUTSIDE the per-shot try/except, so once it
> exhausted retries it raised and ABORTED the whole run.
> **Fixes:**
>   - `resync()` is now BLOCKING-QUIET: drains until the scope is silent for
>     `quiet_s` (0.25 s, bounded by `max_s`), so a 10k block flushes completely.
>     Verified offline against the exact 10k-`\x7f` case.
>   - Validation logs dropped to DEBUG + truncated to 40 chars (no more multi-KB
>     log vomit).
>   - The diagnostic `SAST?`/`INR?` read is wrapped — a desync there can never
>     abort the run.
> **STRATEGIC NOTE for next session:** comms hardening has hit diminishing returns.
> The blocker now is cause #1 from run #3 — the trigger ARMS but doesn't FIRE
> (`SAST READY`, `INR 0`). No socket fix yields data until the edge triggers. Do
> the **manual front-panel single-shot test (next-step #1)** to decide trigger vs.
> SCPI, and fix C3 vertical range (5 V step clips at +127 → garbage samples).
> Consider the C3-edge trigger fallback (#6) if the pin path stays flaky.

> **2026-06-17 (run #3) — buffered reader stopped the HANG; remaining mid-run
> WARNs split into TWO distinct causes, both now addressed.** The sweep now runs
> to completion (no more forever-hang), but mid-sweep WARNs persisted. The
> diagnostics finally separated them:
> 1. **Real trigger MISS (not comms):** `wait timeout: INR?='INR 0' SAST?='SAST
>    READY'` = armed but the edge never fired; STOP then returns a genuine
>    zero-length block `#9000000000`. This is the long-standing flaky-trigger
>    issue, independent of the socket. STILL OPEN — see next-steps (coupling/
>    holdoff, or trigger on C3 instead of the pin).
> 2. **Desync that no longer hangs but still corrupts:** `SAST?='C3:WF
>    DAT2,#9000000000'`, `INR?='\x08\t\t…'` — replies offset by one. Because the
>    garbage still PARSED (0-byte block, non-numeric INR), nothing raised, so it
>    leaked into the next shot's arming (`mid_up_r0 → r1`: `SAST?=''`).
> **Fixes this round (verified offline against these exact byte patterns):**
>   - `oscilloscope.query(cmd, expect=…)` and `_query_block` now SELF-HEAL: if a
>     reply doesn't echo the expected mnemonic / the block header names the wrong
>     channel, they `resync()` + re-issue (≤3×). Wired `expect` into the critical
>     readbacks (SAST/INR/VDIV/OFST/SARA). A desync now heals at detection instead
>     of cascading.
>   - `run_experiment` calls `scope.resync()` at the TOP of each shot (breaks the
>     shot→shot cascade) and replaced the ~100-query `wait_capture_complete` poll
>     with `scope_trigger.wait_for_stop` (≤2 s, a few queries) — `fire()` already
>     blocks through the hold, so the long poll only bred desyncs + false timeouts
>     (TRIGGER_DEBUG next-step #5).
> **TODO: bench re-run.** Expect: no hang, no shot→shot cascade. Any residual
> empties should now be HONEST trigger misses (cause #1) — confirm via the
> `wait_for_stop … likely missed trigger` line, then tackle trigger coupling/
> holdoff or switch to a C3-edge trigger.

> **2026-06-17 (later) — SCPI stream DESYNC: drain-before-query was NOT enough
> (still broke every 5-6 runs); replaced with a buffered stream reader.** Root
> cause confirmed host-side, not scope/network: `query()` returned only the first
> line and discarded trailing bytes, and `_query_block` drained the doubled `\n\n`
> after a WF? block on a timing heuristic. One stray byte shifts the stream → next
> query reads the previous reply → a few hops later a query blocks the socket
> timeout on an answer already consumed = "alive for a couple reads, then stops
> responding." Empty CSVs are the mild form.
> - **First attempt (insufficient):** drain the RX buffer *before* each query.
>   Only catches bytes ALREADY arrived — the doubled `\n\n` can still be in flight
>   when the non-blocking drain runs, so it slipped through ~1 run in 5-6. Removed.
> - **Real fix (`../SiglentOscillosope/oscilloscope.py`, shared driver):** the
>   socket is now framed as a continuous byte stream with a persistent residual
>   buffer (`self._rxbuf`). `_read_line()` skips LEADING CR/LF, so a stray newline
>   is harmless whenever it lands (TCP keeps it ahead of the next real reply).
>   `_query_block()` reads exactly `nbytes` and carries the trailing `\n\n` into
>   `_rxbuf` — no heuristic drain, nothing races, nothing lost between channels.
>   `query()`/`_query_block()` call `resync()` (drop residual + non-blocking socket
>   drain) on any read error so a timed-out reply can't desync the next query.
>   `_open_socket` clears `_rxbuf` so a fresh/reconnected socket starts clean.
> - **Run-level backstop (`run_experiment.py`):** each settling shot is wrapped —
>   on any exception it `scope.resync()`s, records a failed shot in the manifest,
>   and CONTINUES the sweep instead of aborting the whole run.
> - Socket timeout 5 s → 2 s (`config.yaml` `scope.timeout_s`) so a real stall
>   fails fast. Framing verified offline against doubled-`\n\n`, in-flight stray,
>   split-recv, and post-timeout cases (all pass). **TODO: bench re-run to confirm
>   the every-5-6-runs failure is gone and the empty-CSV rate drops.**
>
> ---
>
> **Status:** **trigger now FIRES on the bench** (the 2026-06-16 level fix
> worked) — confirmed by a real captured edge. The remaining issue is
> **intermittent empty captures**: roughly 1 in 3 shots writes a header-only
> (0-row) CSV. Hardened + instrumented 2026-06-17; needs a bench re-run to
> confirm the WARNs are gone.
>
> **2026-06-17 — trigger confirmed firing; new failure mode = flaky empty CSVs.**
> Run `data/ldo_20260617_140901/` (aborted after the first `small_up` triple):
>   - `r1` is a **good capture** — `v_trig` swings −1 → **2.8 V**, edge at
>     **t = 0.50 s**, 10000 rows. So the edge triggers and all 3 channels read.
>   - `r0` and `r2` are **header-only** (24 bytes, 0 data rows).
> A header-only CSV happens because `save_capture_csv` writes
> `min(len(col))` rows — if **any one** channel's `WF?` returns 0 samples the
> whole file collapses to the header. Two intermittent causes, indistinguishable
> from the saved file alone (the `min()` masks which channel was empty):
>   1. **Never armed** — `:TRIGger:RUN` was silently dropped (back-to-back-write
>      trap), so the scope stayed Stopped and `st.stop()` froze a never-acquired
>      buffer → `WF?` = 0 samples.
>   2. **`WF?` block desync** — the doubled-`\n\n` terminator trap (quirk #3),
>      run-to-run by timing; one channel desyncs the next → 0 samples.
> **Fixes applied (additive; Stable shared driver untouched):**
>   - `arm_single` now **confirms the arm**: `_arm_and_confirm()` reads `SAST?`
>     back after `:TRIGger:RUN` and retries RUN (≤4×) until `Arm`/`Ready`. Kills #1.
>   - `capture_channel_volts` **retries a 0-sample read** (re-`STOP` + re-read,
>     ≤2×). Recovers #2; a persistent 0 means the trigger really never fired.
>   - `run_settling` prints a **loud per-channel WARN** (which channel is empty +
>     `SAST?`/`INR?`/`scope_complete`) and records `samples`/`channel_counts` in
>     the manifest, so a flaky shot self-diagnoses instead of leaving a silent
>     24-byte file. Empty `v_trig` ⇒ never triggered (#1); empty data channel
>     only ⇒ read desync (#2).
> **TODO: bench re-run (even just the `small_up` triple) and read the WARN lines
> to confirm the empties are gone / identify any residual cause.**
>
> ---
>
> **2026-06-16 — level-clobber root cause FOUND + FIXED (this is what made the
> trigger fire at all).** The level set was being silently clobbered, so the
> scope armed with the threshold at **0 V**. With the trigger pin idling at 0 V
> (logic LOW = ground) there's no clean LOW→HIGH crossing → arms, never fires.
> Three compounding SCPI traps, all isolated by controlled A/B:
>   1. **`*OPC?` drops the level set.** Sending `*OPC?` right after `C1:TRLV`/
>      `:TRIGger:EDGE:LEVel` reverts the level to 0.00E+00. `arm_single` used
>      `*OPC?` to "sync" — that was THE bug. Now uses a short sleep; level sticks.
>   2. **Level clamps to the trigger channel's VDIV** (~±4 × V/div). `arm_single`
>      now pins `C1:VDIV 1V` (and verifies) before setting the level.
>   3. **Back-to-back writes get dropped** — config writes now have a ~0.12 s
>      settle each (`_w`/`_set_vdiv`) and critical values are read back.
> Verified: after `arm_single`, `C1:TRLV? = 1.00E+00`, `SAST? = Arm`. Also: INR?
> cleared at arm + `wait_capture_complete` no longer treats a never-armed
> `SAST?='Stop'` as success. Note `analyze_ldo` quirks #2/
> #5 below (TDIV/TRLV value+unit) still hold; see memory `sds2000x-plus-scpi-traps`.

/ scope = SDS2204X Plus (fw 5.4.1.5.2R2) @ 169.254.111.4:5025 / H7 = SMA_Driver_PIO on COM8 /

---

## TL;DR — the open issue

The trigger **fires** now (the 2026-06-16 level fix cured the arms-but-never-
triggers bug — see the captured edge in `r1` above). The open issue is that the
automated run is **flaky**: ~1 shot in 3 writes a header-only (0-row) CSV. Cause
is a 0-sample `WF?` on at least one channel, from either a dropped `:TRIGger:RUN`
(never armed) or the doubled-`\n\n` block desync. Both are now armored against +
instrumented in code; a bench re-run is needed to confirm.

**Most useful next test:** re-run `python run_experiment.py` (the `small_up`
triple alone is enough) and read the per-shot `WARN: empty capture …` lines.
- Empty `v_trig` ⇒ never armed/triggered → look at the arm path (#1).
- Empty data channel only ⇒ `WF?` desync (#2) → the retry should now absorb it.
If empties are gone, the issue is closed.

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

## What the CURRENT run produces (2026-06-17)

- **When a shot triggers**: a real frame — `r1` of `small_up` shows `v_trig`
  −1 → 2.8 V with the edge at t = 0.50 s, 10000 rows. Good data.
- **When a shot is flaky (~1 in 3)**: a **header-only CSV** (24 bytes, 0 rows),
  because one channel's `WF?` returned 0 samples and `save_capture_csv` writes
  `min(len(col))` rows.
- The new per-shot `WARN: empty capture …` line names the empty channel +
  `SAST?`/`INR?` so each flaky shot is self-diagnosing (see fixes above).

### Historical (pre-2026-06-16, the level-clobber era — kept for context)

- All 24 settling CSVs were written 10000 rows each, but every capture was a
  **stale floor frame**: `v_trig` ≈ ±40 mV (no edge), `v_out` ≈ 0.32–0.40 V (LDO
  floor, no step), `edge idx: NONE`. `summary.csv` showed `span_v ≈ 0.3 mV`,
  settle = NaN. `scope_complete=True` was misleading — an unarmed scope returns
  `SAST?='Stop'` instantly. (Both the level clobber and the false-positive poll
  are fixed.)

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
