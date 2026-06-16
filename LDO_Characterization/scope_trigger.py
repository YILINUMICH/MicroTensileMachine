"""
scope_trigger.py — single-shot trigger + capture helpers for the SDS2000X Plus.

The shared SiglentOscillosope module (oscilloscope.py) has waveform capture and
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
    _w(scope, f":TRIGger:EDGE:SOURce {src}")
    _w(scope, f":TRIGger:EDGE:SLOPe {slope}")
    if not set_trigger_level(scope, src, cfg.trigger_level_v):
        print(f"      [WARN: trigger level on {src} did not read back "
              f"{cfg.trigger_level_v} V — check VDIV range / command form]")
    _w(scope, ":TRIGger:MODE SINGle")
    _w(scope, ":TRIGger:RUN")      # arm/start the single acquisition
    # Clear the INR latch AFTER arming so its bit0 ('acquisition complete')
    # reflects THIS shot's trigger, not a stale event — wait_capture_complete
    # keys off it. INR? is read-and-clear on this firmware. (A query is fine
    # here; it's the *OPC? specifically that clobbers the level set.)
    try:
        scope.query("INR?")
    except Exception:
        pass


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
            r = scope.query("INR?")
            last_inr = r
            val = _first_int(r)
            if val is not None and (val & 0x01):
                return True
        except Exception:
            pass
        # --- fallback: SAST?, guarded against the never-armed 'Stop' ---
        try:
            s = scope.query("SAST?").strip().upper()
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


def capture_channel_volts(scope, source: str, codes_per_div: float
                          ) -> Tuple["object", "object"]:
    """Grab one channel as (t_seconds, v_volts) numpy arrays.

    Uses the shared module's capture_waveform() + codes_to_volts(). Time axis is
    derived from the sample rate; t=0 is the record start (the trigger sits
    ~3 divisions in, per configure_timebase).
    """
    import numpy as np
    # Local import so this module doesn't hard-require the shared one until used.
    from oscilloscope import codes_to_volts

    codes, pre = scope.capture_waveform(source=source)
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
    """Pull the first (possibly scientific) float out of a SCPI reply like
    'TRLV 1.00E+00V' or '1.00E+00'. Returns None if there's no number."""
    import re
    m = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", reply or "")
    return float(m.group()) if m else None
