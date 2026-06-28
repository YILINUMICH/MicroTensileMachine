"""
scope_trigger.py — single-shot trigger + capture helpers for the SDS2000X Plus.

The shared Driver_SiglentOscilloscope module (oscilloscope.py) has waveform capture and
raw SCPI passthrough but no single-shot arming helper. These functions add it
without editing the Stable shared driver. They operate on an `Oscilloscope`
object via its `write()` / `query()` methods.

SCPI verbs (TRSE / TRMD / ARM / INR?) follow the SDS2000X Plus Programming Guide
convention. VERIFY against your firmware revision the first time on the bench —
if INR? polling never completes, see `wait_capture_complete` notes for the
`SAST?` fallback.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Tuple


@dataclass
class CaptureConfig:
    trigger_src: str = "C1"
    trigger_level_v: float = 1.0
    trigger_slope: str = "POS"      # POS | NEG
    timebase_s: float = 0.02        # TDIV
    codes_per_div: float = 25.0
    memory_depth: str = "10K"       # ACQ:MDEP — caps WF? size. {10K|100K|1M|10M|100M}


def siglent_time(seconds: float) -> str:
    """Format a time in seconds as a Siglent value+unit string (e.g. 0.02 -> '20MS').
    The legacy TDIV/TRDL set commands reject a bare float — they need the unit.
    NOTE: TDIV must land on a 1-2-5 step (20MS ok); TRDL may be an arbitrary delay."""
    a = abs(seconds)
    if a >= 1.0:
        v, u = seconds, "S"
    elif a >= 1e-3:
        v, u = seconds * 1e3, "MS"
    elif a >= 1e-6:
        v, u = seconds * 1e6, "US"
    else:
        v, u = seconds * 1e9, "NS"
    # integer mantissa keeps it on a clean step for typical values
    return f"{int(round(v))}{u}"


def stop(scope) -> None:
    """Freeze acquisition. Multi-channel WF? reads must come from a STOPPED frame
    — while free-running (AUTO) the buffer is overwritten mid-read and the 2nd/3rd
    channel comes back empty. Single-shot stops on its own, but call this to be sure."""
    scope.write("STOP")


def enable_channels(scope, channels) -> None:
    """Turn on the trace display for each channel. Required before capture or
    triggering: an undisplayed channel returns an empty `WF?` block (header-only
    CSVs) and makes `PAVA?` time out (the ripple-pass hang). SDS verb: `<ch>:TRA ON`."""
    for ch in channels:
        if ch:
            scope.write(f"{ch}:TRA ON")


def configure_timebase(scope, cfg: CaptureConfig) -> None:
    """Set time/div and a left-of-center trigger so the rising edge sits early
    in the record with a short pre-trigger baseline visible."""
    # Force SHORT headers on so the self-heal validation (query expect=...) has a
    # mnemonic to match ('SAST READY', 'C2:VDIV ...'). The Siglent command is
    # `CHDR OFF|SHORT|LONG` — NOT 'CHDR ON' (that's a no-op, which left the scope
    # in CHDR OFF and made every expect= check fail). The ripple pass sets OFF, so
    # a stale OFF would otherwise break the next run. Verify it took; the expect=
    # checks are non-fatal either way (a bare value is accepted) but headers give
    # the stronger desync detection.
    _w(scope, "CHDR SHORT")
    try:
        if "SAST" not in scope.query("SAST?").upper():
            print("      [WARN: CHDR SHORT did not enable headers — desync "
                  "detection weaker, but bare replies still parse]")
    except Exception:
        pass
    # Cap acquisition memory FIRST (setting MDEP can perturb the timebase), then
    # set TDIV last so it wins. TDIV needs a value+unit (e.g. '20MS'), not a bare
    # float. With 10K over a ~280 ms window that's ~28 us/sample.
    #
    # CRITICAL: use settled writes (_w), NOT bursted scope.write(). This firmware
    # silently DROPS back-to-back writes (the same trap arm_single hit). A dropped
    # TDIV leaves the scope at ~1 ns/div, so a capture returns only ~10 samples
    # around the edge instead of the full settle. Read TDIV back to confirm.
    _w(scope, f"ACQ:MDEP {cfg.memory_depth}")
    for _ in range(3):
        _w(scope, f"TDIV {siglent_time(cfg.timebase_s)}")
        try:
            got = _first_float(scope.query("TDIV?"))
            if got is not None and abs(got - cfg.timebase_s) <= cfg.timebase_s * 0.05:
                break
        except Exception:
            pass
    else:
        print(f"      [WARN: TDIV did not read back {cfg.timebase_s}s — "
              f"capture window will be wrong]")
    # Trigger centered (TRDL 0): ~half the screen is pre-trigger baseline, half is
    # the transient — the ~100 ms settle fits comfortably in the post-trigger half.
    _w(scope, "TRDL 0S")


# SDS2000X Plus display grid is 10 horizontal x 8 vertical divisions.
H_DIVISIONS = 10
V_DIVISIONS = 8

# Standard Siglent V/div ladder (1-2-5 sequence), in volts.
_VDIV_LADDER = [0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]


def fit_vdiv(vmax: float, margin: float = 1.15) -> float:
    """Smallest standard V/div that shows 0..vmax WITHOUT clipping at zero offset.

    A channel at OFST 0 spans +/-(V_DIVISIONS/2)*vdiv around screen centre, so the
    top half reaches +(V_DIVISIONS/2)*vdiv; we need that to cover vmax (+ margin).
    Using offset 0 avoids the Cn:OFST sign ambiguity entirely — the trade is that
    only the top half of the screen is used, but the per-step sizing keeps the
    resolution close to optimal for each step.
    """
    need = abs(vmax) * margin / (V_DIVISIONS / 2.0)
    for v in _VDIV_LADDER:
        if v >= need:
            return v
    return _VDIV_LADDER[-1]


def set_channel_range(scope, src: str, vmax: float) -> float:
    """Size a data channel's V/div to fit 0..vmax at zero offset and confirm it
    applied. This is the fix for the +127 clipping that railed C3 on the 5 V step
    when every channel sat at a fixed 1 V/div (+/-4 V). Returns the V/div used."""
    _w(scope, f"{src}:OFST 0V")          # zero offset -> no OFST-sign ambiguity
    vdiv = fit_vdiv(vmax)
    if not _set_vdiv(scope, src, vdiv):
        print(f"      [WARN: {src} V/div -> {vdiv} V did not confirm — capture "
              f"may clip or mis-scale]")
    return vdiv


def arm_to_fire_delay_s(cfg: CaptureConfig, margin: float = 2.0,
                        floor_s: float = 0.3) -> float:
    """How long to wait AFTER arm_single() before raising the trigger edge.

    A DSO cannot trigger until it has acquired its PRE-TRIGGER buffer (the data
    left of the trigger point); an edge that arrives during that fill is silently
    ignored. With the trigger centered (TRDL 0, set in configure_timebase) on a
    10-division screen, that's ~5 divisions = 5 x TDIV of acquisition after
    :TRIGger:RUN. On a slow timebase this is significant (0.5 s at 100 ms/div),
    and firing too soon caused ~30% RANDOM trigger misses (the firmware's variable
    settleWait sometimes landed the edge before the buffer filled — confirmed on
    the bench 2026-06-17: settle=0.2 s gave 14/20 hits, settle=1.0 s gave 20/20).

    Returns max(floor, 5*TDIV*margin) so OUR delay alone covers the fill even if
    the firmware settle is ~0. Scales automatically with timebase_s.
    """
    pretrigger_s = cfg.timebase_s * (H_DIVISIONS / 2.0)
    return max(floor_s, pretrigger_s * margin)


# Short dwell after a trigger-config write, in place of `*OPC?`. See
# set_trigger_level for why *OPC? must NOT be used around the level set. This
# firmware also DROPS commands that arrive back-to-back too fast over the socket
# (a `VDIV`/`TRLV` set can be silently ignored), so each config write gets a
# small settle instead of being fired in a burst.
_SCPI_SETTLE_S = 0.12


def _w(scope, cmd: str, settle_s: float = _SCPI_SETTLE_S) -> None:
    """Write a config command and dwell briefly so the scope actually applies it
    (NOT via *OPC? — that clobbers the trigger level; see set_trigger_level)."""
    scope.write(cmd)
    time.sleep(settle_s)


def _set_vdiv(scope, src: str, vdiv_v: float, tries: int = 3) -> bool:
    """Set a channel's V/div and confirm it applied (back-to-back writes can be
    dropped). The trigger level is clamped to ~±4 x VDIV, so this must land
    before the level is set or a 1 V request silently clamps toward 0."""
    for _ in range(tries):
        _w(scope, f"{src}:VDIV {vdiv_v}V")
        try:
            got = _first_float(scope.query(f"{src}:VDIV?"))
            if got is not None and abs(got - vdiv_v) <= vdiv_v * 0.05:
                return True
        except Exception:
            pass
    return False


def _src_matches(reply: str, src: str) -> bool:
    """True if a `:TRIGger:EDGE:SOURce?` reply names channel `src` ('C1').
    The scope may echo 'C1', 'CHANnel1', or '...,C1' depending on form."""
    r = (reply or "").upper()
    n = src.upper().lstrip("C")              # '1' for 'C1'
    return src.upper() in r or f"CHANNEL{n}" in r or r.strip().endswith(n)


def set_trigger_source(scope, src: str, tries: int = 3) -> bool:
    """Select the edge-trigger source and CONFIRM it actually landed.

    THIS is the arms-but-never-fires culprit when the manual front-panel trigger
    works but the SCPI one doesn't: if `:TRIGger:EDGE:SOURce C1` is the wrong
    token for the firmware it's silently ignored, the scope arms on the WRONG
    source, and no edge on C1 ever fires it (SAST stays READY). The fix is to set
    it, read `:TRIGger:EDGE:SOURce?` back, and — if it didn't take — fall back to
    the SCPI long form (CHANnel1) and the legacy TRSE select, then re-verify.
    Returns True once the readback names `src`.
    """
    n = src.upper().lstrip("C")              # '1'
    forms = [
        f":TRIGger:EDGE:SOURce {src}",       # modern, short token
        f":TRIGger:EDGE:SOURce CHANnel{n}",  # modern, SCPI long token
        f"TRSE EDGE,SR,{src},HT,OFF",        # legacy SDS trigger-select
    ]
    for _ in range(tries):
        for cmd in forms:
            _w(scope, cmd)
            try:
                if _src_matches(scope.query(":TRIGger:EDGE:SOURce?"), src):
                    return True
            except Exception:
                pass
    return False


def set_trigger_level(scope, src: str, level_v: float) -> bool:
    """Set the edge trigger level and confirm it landed.

    THE BUG that kept the single-shot from ever firing was here: the arming
    code bracketed the level write with a `*OPC?` query, and on this SDS2204X
    Plus firmware an `*OPC?` immediately after a `TRLV` / `:TRIGger:EDGE:LEVel`
    write makes the level set get DROPPED — it reverts to 0.00E+00. With the
    level at 0 V and the trigger pin idling at 0 V (logic LOW = ground) there is
    no clean LOW->HIGH crossing, so the scope ARMS but NEVER TRIGGERS. Replacing
    the `*OPC?` with a plain short sleep makes both the modern and legacy level
    commands stick (verified A/B/C on the bench, 2026-06-16). NEVER reintroduce
    an `*OPC?` around the level write.

    Second gotcha: the level is CLAMPED to the trigger channel's vertical range
    (~±4 x VDIV). If VDIV is tiny, a 1 V request silently clamps toward 0 (same
    arms-but-never-triggers symptom). arm_single pins the trigger channel to a
    sane V/div before calling this.

    Sends the level with a unit (no `*OPC?`), reads it back, and returns True if
    the readback is within 50 mV of the request.
    """
    def _level_ok() -> bool:
        for q in (f"{src}:TRLV?", ":TRIGger:EDGE:LEVel?"):
            try:
                got = _first_float(scope.query(q))
                if got is not None and abs(got - level_v) <= 0.05:
                    return True
            except Exception:
                pass
        return False

    for _ in range(3):
        _w(scope, f":TRIGger:EDGE:LEVel {level_v}V")   # NO *OPC? — settle instead
        if _level_ok():
            return True
        # Belt and suspenders: also try the legacy verb (also works without *OPC?).
        _w(scope, f"{src}:TRLV {level_v}V")
        if _level_ok():
            return True
    return False


def arm_single(scope, cfg: CaptureConfig) -> None:
    """Configure an edge trigger on the trigger channel and arm single-shot.

    Call this immediately BEFORE firing the firmware `fire` command.

    NOTE: this path deliberately uses short sleeps instead of `*OPC?`. On this
    firmware an `*OPC?` right after the trigger-level write silently drops the
    level (see set_trigger_level) — that was the root cause of the scope arming
    but never triggering.
    """
    src = cfg.trigger_src
    slope = "RISing" if cfg.trigger_slope.upper().startswith("P") else "FALLing"
    # Pin the trigger channel to a vertical scale that can REPRESENT the level:
    # the trigger level is clamped to ~±4 x VDIV, so a tiny V/div pins a 1 V
    # request near 0 and the scope arms but never fires. 1 V/div comfortably
    # holds the 3.3 V logic pulse and a ~1 V threshold. (Data channels C2/C3 are
    # untouched; capture_channel_volts reads each channel's own VDIV.)
    _w(scope, f"{src}:TRA ON")
    if not _set_vdiv(scope, src, 1.0):
        print(f"      [WARN: could not set {src} V/div to 1 V — trigger level "
              f"may clamp]")
    # Modern :TRIGger subsystem — sets type/source/slope explicitly.
    _w(scope, ":TRIGger:TYPE EDGE")
    # SOURCE must be CONFIRMED: if the token form is wrong the scope arms on the
    # wrong source and never fires on C1 even though the edge is there (manual
    # front-panel trigger works, SCPI doesn't = this bug). set_trigger_source
    # reads it back and falls back to alternate token forms until it sticks.
    if not set_trigger_source(scope, src):
        print(f"      [WARN: trigger SOURCE did not read back {src} — scope may "
              f"be armed on the wrong source; it will arm but never fire]")
    _w(scope, f":TRIGger:EDGE:SLOPe {slope}")
    if not set_trigger_level(scope, src, cfg.trigger_level_v):
        print(f"      [WARN: trigger level on {src} did not read back "
              f"{cfg.trigger_level_v} V — check VDIV range / command form]")
    _w(scope, ":TRIGger:MODE SINGle")
    # Arm and CONFIRM. This firmware silently drops back-to-back writes, so a
    # `:TRIGger:RUN` can be a no-op — the scope stays Stopped, never triggers,
    # and `st.stop()` later freezes a never-acquired buffer whose `WF?` returns
    # 0 samples -> a header-only CSV. Read SAST? back and retry RUN until the
    # scope reports Arm/Ready (or warn after a few tries).
    if not _arm_and_confirm(scope):
        print(f"      [WARN: scope did not confirm ARM after :TRIGger:RUN — "
              f"SAST?={scope.query('SAST?').strip()!r}; capture may be empty]")
    # Clear the INR latch AFTER arming so its bit0 ('acquisition complete')
    # reflects THIS shot's trigger, not a stale event — wait_capture_complete
    # keys off it. INR? is read-and-clear on this firmware. (A query is fine
    # here; it's the *OPC? specifically that clobbers the level set.)
    try:
        scope.query("INR?")
    except Exception:
        pass


def _arm_and_confirm(scope, tries: int = 4) -> bool:
    """Send `:TRIGger:RUN` and verify the scope actually armed (SAST? in
    Arm/Ready/Trig'd). Retries the RUN write — which this firmware can silently
    drop — until armed or `tries` exhausted. Returns True once armed."""
    for _ in range(tries):
        _w(scope, ":TRIGger:RUN")
        try:
            s = scope.query("SAST?", expect="SAST").strip().upper()
            if any(k in s for k in ("ARM", "READY", "TRIG")):
                return True
        except Exception:
            pass
    return False


def wait_capture_complete(scope, timeout_s: float) -> bool:
    """Poll until the single-shot acquisition has actually triggered+stopped.

    Primary signal: INR? bit0 ('acquisition complete'), which arm_single clears
    right after arming so a set bit means THIS shot fired.

    Fallback signal: SAST? == 'Stop' — but ONLY once an armed state (Arm/Ready/
    Trig'd) has first been observed. A scope that was never armed also reports
    'Stop' instantly; the old code accepted that and reported a phantom capture
    complete (`scope_complete=True` on a stale floor frame). Requiring a prior
    armed observation closes that false-positive.

    Returns True on a real completion, False on timeout.
    """
    t0 = time.time()
    last_inr, last_sast = "?", "?"
    seen_armed = False
    while time.time() - t0 < timeout_s:
        # --- primary: INR? bit0 (cleared at arm) ---
        try:
            r = scope.query("INR?", expect="INR")
            last_inr = r
            val = _first_int(r)
            if val is not None and (val & 0x01):
                return True
        except Exception:
            pass
        # --- fallback: SAST?, guarded against the never-armed 'Stop' ---
        try:
            s = scope.query("SAST?", expect="SAST").strip().upper()
            last_sast = s
            if any(k in s for k in ("ARM", "READY", "TRIG")):
                seen_armed = True
            elif "STOP" in s and seen_armed:
                return True
        except Exception:
            pass
        time.sleep(0.05)
    print(f"      [wait timeout: last INR?={last_inr!r} SAST?={last_sast!r}]")
    return False


def wait_for_stop(scope, timeout_s: float = 2.0) -> bool:
    """Lightweight 'did the single-shot finish?' check for use AFTER `h7.fire()`.

    `fire()` blocks until the firmware completes the whole hold, so by the time
    it returns the armed single-shot has already triggered+auto-stopped (or
    missed the edge). A successful capture lands the scope in SAST?='Stop' almost
    immediately; a missed trigger stays 'Ready'/'Arm' until we force STOP. So a
    SHORT poll for 'Stop' is all that's needed here — and it fires only a handful
    of queries instead of the ~100 `wait_capture_complete` sprays over a multi-
    second window (each query a desync opportunity + a source of false timeouts).
    See TRIGGER_DEBUG.md next-step #5. Returns True if 'Stop' was seen.
    """
    t0 = time.time()
    last = "?"
    while time.time() - t0 < timeout_s:
        try:
            s = scope.query("SAST?", expect="SAST").strip().upper()
            last = s
            if "STOP" in s:
                return True
        except Exception:
            pass
        time.sleep(0.05)
    print(f"      [wait_for_stop: still {last!r} after {timeout_s}s "
          f"(likely a missed trigger — capture will be empty)]")
    return False


def capture_channel_volts(scope, source: str, codes_per_div: float,
                          retries: int = 2) -> Tuple["object", "object"]:
    """Grab one channel as (t_seconds, v_volts) numpy arrays.

    Uses the shared module's capture_waveform() + codes_to_volts(). Time axis is
    derived from the sample rate; t=0 is the record start (the trigger sits
    ~3 divisions in, per configure_timebase).

    A `WF?` read can return 0 samples for two reasons: the block reader desynced
    on the doubled-`\\n\\n` terminator (run-to-run by timing), or the frame was
    never acquired (single-shot never triggered). The first is recoverable by
    re-STOPping and re-reading, so retry a couple of times before giving up; a
    persistent 0 means the trigger genuinely never fired.
    """
    import numpy as np
    # Local import so this module doesn't hard-require the shared one until used.
    from oscilloscope import codes_to_volts

    codes = None
    pre = None
    for attempt in range(retries + 1):
        codes, pre = scope.capture_waveform(source=source)
        if len(codes) > 0:
            break
        if attempt < retries:
            scope.write("STOP")            # re-freeze; clears a desynced read
            time.sleep(0.1)
    volts = codes_to_volts(codes, pre.vdiv, pre.offset, codes_per_div=codes_per_div)
    n = len(volts)
    sr = pre.sample_rate if pre.sample_rate and pre.sample_rate > 0 else float("nan")
    t = np.arange(n) / sr if sr == sr else np.arange(n, dtype=float)
    return t, np.asarray(volts, dtype=float)


def _first_int(reply: str):
    """Pull the first integer out of a SCPI reply like 'INR 1' or '#1'."""
    import re
    m = re.search(r"-?\d+", reply or "")
    return int(m.group()) if m else None


def _first_float(reply: str):
    """Pull the VALUE float out of a SCPI readback like 'C2:VDIV 1.00E+00V',
    'TRLV 1.00E+00V', or a bare '1.00E+00'. Returns None if there's no number.

    Takes the LAST number in the string, not the first: Siglent headers the reply
    with the mnemonic, and for a channel query that header contains the channel
    digit ('C2:VDIV ...'). Grabbing the first number returned the '2' from 'C2'
    instead of the 1.00 V/div value — which made `_set_vdiv`/`set_trigger_level`
    falsely 'fail' to confirm for every channel whose number != its value (C1 at
    1 V/div passed only by the coincidence 1 == 1.0). The value is always last.
    """
    import re
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", reply or "")
    return float(nums[-1]) if nums else None
