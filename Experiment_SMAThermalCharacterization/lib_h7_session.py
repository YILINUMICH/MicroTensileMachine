"""lib_h7_session.py — shared H7 drive + capture plumbing.

Imported by `operator_current_sweep.py` and (next) the randomised RNN data
collector, so both share one implementation of the things that are easy to get
wrong: opening a port that looks dead, restoring runtime calibration, feeding
the watchdog, and disarming no matter how the run ends.

Everything here is UI-agnostic and hardware-facing. No plotting, no analysis.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import serial

# ── Stream channels (see Firmware_SMAConstantCurrent_PIO/src/sample_ring.h) ──
SRC_LASER, SRC_LOAD = 1, 2
SRC_SMA_V, SRC_SMA_I = 3, 4
SRC_CC_U, SRC_CC_R = 6, 7
# src=5 (sma_r) was RETIRED from the wire 2026-07-27 — derive it on the host.

# The ADS1263 runs against a 5.000 V REF7050, so a channel pinned here is
# CLIPPED, not measured. Detect it rather than silently recording a flat top.
ADC_FULL_SCALE_V = 5.0
SAT_GUARD_V = 4.9990

# Runtime-only calibration. These revert on EVERY reset or flash with nothing on
# screen to say so, which has already cost one session — see the calibration log
# in Firmware_SMAConstantCurrent_PIO/README.md. Re-send them on every connect.
DEFAULT_CAL = ("vdd 5.067", "offset 0.5", "ioffset 0.0167")

# Firmware heat watchdog is 5000 ms; ping well inside it.
PING_PERIOD_S = 1.0


@dataclass
class Sample:
    src: int
    hw_us: int
    value: float
    raw: int
    seq: int


@dataclass
class Capture:
    samples: list = field(default_factory=list)
    console: list = field(default_factory=list)   # (t_rel, text)

    def by_src(self, src: int) -> "list[Sample]":
        return [s for s in self.samples if s.src == src]

    def series(self, src: int):
        """(t_seconds, values) on the FIRMWARE clock. hw_us, never host time —
        host timestamps carry Windows scheduler jitter."""
        rows = sorted(((s.hw_us, s.value) for s in self.samples if s.src == src))
        if not rows:
            return [], []
        t0 = rows[0][0]
        return [(t - t0) * 1e-6 for t, _ in rows], [v for _, v in rows]


class H7:
    """One owner of the serial port. Open it once, keep it, always disarm."""

    def __init__(self, port: str, baud: int = 115200, verbose: bool = True):
        self.port, self.baud, self.verbose = port, baud, verbose
        self.ser: Optional[serial.Serial] = None
        self._buf = b""

    # ---------------------------------------------------------------- open --
    def open(self, force_pull_s: float = 1.0, probe_s: float = 2.0,
             cal: "tuple[str, ...]" = DEFAULT_CAL) -> None:
        self.ser = serial.Serial(self.port, self.baud, timeout=0.05,
                                 write_timeout=2.0)
        try:
            self.ser.set_buffer_size(rx_size=8 * 1024 * 1024, tx_size=64 * 1024)
        except Exception:
            pass
        time.sleep(0.3)

        # FORCE PULL. A session that exited without draining leaves the M7's
        # USB-CDC TX buffer full; the firmware finds no room and the port looks
        # dead. Big raw reads give it room again — readline() cannot shift a
        # backlog fast enough. Verified 2026-07-28 on a port dead for a session.
        pulled, t0 = 0, time.time()
        while time.time() - t0 < force_pull_s:
            pulled += len(self.ser.read(65536))
        self.ser.reset_input_buffer()
        self._say(f"force pull: drained {pulled/1024:.1f} kB")

        # Refuse to start a run against a dead port.
        n, t0 = 0, time.time()
        while time.time() - t0 < probe_s and n < 2000:
            n += len(self.ser.read(65536))
        if n < 2000:
            # ORDER MATTERS. On Windows a second handle on this CDC port OPENS
            # FINE and simply receives nothing — the other reader drains the
            # stream — so "port opened, no bytes" looks exactly like dead
            # hardware and reads as a rig fault. Cost 20 min on 2026-08-07,
            # chasing an M4 that was producing perfectly. Check the cheap,
            # reversible cause first.
            raise RuntimeError(
                f"{self.port} opened but is not streaming ({n} bytes in "
                f"{probe_s:.0f}s).\n"
                f"  1. Is another reader holding it? pio device monitor, the "
                f"Arduino IDE monitor, sma_console, another script. The H7 is "
                f"SINGLE-OWNER: a second handle opens and gets 0 bytes.\n"
                f"  2. Otherwise power-cycle USB + EVM and retry. If the "
                f"[STATUS] line then shows rate1=0 rate2=0 prod1=0 prod2=0 "
                f"with crc_err=0, nothing is entering the ring at all — that "
                f"is the M4/ADC side, not the link.")
        self._say(f"port live ({n} bytes in probe)")

        for c in cal:
            self.send(c)
            time.sleep(0.25)
        self._say(f"calibration restored: {', '.join(cal)}")

    def close(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()

    # ------------------------------------------------------------- command --
    def send(self, cmd: str) -> None:
        assert self.ser is not None
        self.ser.write((cmd + "\n").encode())
        self.ser.flush()

    def disarm(self) -> None:
        """Best-effort, never raises — this runs in `finally` blocks."""
        try:
            self.send("code 0")
            time.sleep(0.05)
            self.send("disarm")
            time.sleep(0.3)
        except Exception as e:                                  # noqa: BLE001
            print(f"  WARNING: disarm failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------- capture --
    def capture(self, secs: float, ping: bool = True,
                on_console: Optional[Callable[[float, str], None]] = None
                ) -> Capture:
        """Read the stream for `secs`, feeding the watchdog. Non-sample lines
        are kept separately so `[ACT] heat` / `[CC] FAULT` stay visible."""
        assert self.ser is not None
        cap = Capture()
        t0 = last_ping = time.time()
        while time.time() - t0 < secs:
            if ping and time.time() - last_ping >= PING_PERIOD_S:
                try:
                    self.send("ping")
                except Exception:
                    pass
                last_ping = time.time()
            chunk = self.ser.read(65536)
            if not chunk:
                continue
            self._buf += chunk
            *lines, self._buf = self._buf.split(b"\n")
            for raw in lines:
                line = raw.rstrip(b"\r")
                f = line.split(b"\t")
                if len(f) >= 6:
                    try:
                        cap.samples.append(Sample(int(f[1]), int(f[4]),
                                                  float(f[3]), int(f[2]),
                                                  int(f[5])))
                        continue
                    except ValueError:
                        pass
                txt = line.decode("utf-8", "replace").strip()
                if txt and "PGAL" not in txt and txt != "LM":
                    cap.console.append((time.time() - t0, txt))
                    if on_console:
                        on_console(time.time() - t0, txt)
        return cap

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(f"  {msg}", flush=True)


# ─────────────────────────── analysis helpers ────────────────────────────────
def heat_windows(cap: Capture, gap_s: float = 0.20, min_ms: float = 20.0):
    """Heat phases from the CC command channel (src=6). Handles BOTH cccycle
    forms, which look completely different on the wire:

      i_low == 0  -> the loop OPENS during cool, so src=6 exists only during
                     heat. Clusters separated by a time GAP are the pulses.
      i_low  > 0  -> the loop stays CLOSED through cool, so src=6 streams
                     CONTINUOUSLY. Gap-based clustering then returns ONE window
                     spanning the whole run (measured 2026-07-28: 75 s of
                     samples, max gap 4 ms, 1 "pulse" found for 6 commanded —
                     every downstream number was an average over heat AND cool).
                     Segment on cc_u VALUE instead.

    Thresholding the COMMAND rather than the measured current is deliberate: the
    controller always drives u higher during heat than cool even when the
    current fails to follow, so it marks the phase correctly regardless of
    whether the loop converges — which is exactly the case being diagnosed.
    """
    rows = sorted(((s.hw_us * 1e-6, s.value) for s in cap.samples
                   if s.src == SRC_CC_U))
    if not rows:
        return []
    t = [r[0] for r in rows]

    # Dispatch on whether the stream is continuous.
    if max((b - a for a, b in zip(t, t[1:])), default=0.0) > gap_s:
        out, start, prev = [], t[0], t[0]
        for x in t[1:]:
            if x - prev > gap_s:
                out.append((start, prev))
                start = x
            prev = x
        out.append((start, prev))
        return [w for w in out if (w[1] - w[0]) * 1e3 >= min_ms or len(out) == 1]

    # Continuous: cool dominates the timeline, so the median IS the cool level.
    v = sorted(r[1] for r in rows)
    cool = v[len(v) // 2]
    top = v[int(0.999 * (len(v) - 1))]
    if top - cool < 1e-3:
        return []                      # command never rose — no heat phase
    thr = cool + 0.25 * (top - cool)

    out, start, prev = [], None, None
    for tt, vv in rows:
        if vv >= thr:
            if start is None:
                start = tt
            prev = tt
        elif start is not None:
            if (prev - start) * 1e3 >= min_ms:
                out.append((start, prev))
            start = None
    if start is not None and (prev - start) * 1e3 >= min_ms:
        out.append((start, prev))
    return out


def m4_offset_from_capture(cap: Capture) -> float:
    """M4→M7 clock offset from a LIVE capture's console lines.

    Same STATUS-line source as `m4_clock_offset_s`, but parsed from
    `cap.console` so an in-progress run can align its own analysis instead of
    only fixing it offline. Takes the LAST STATUS line seen (most recent
    counters; the offset is stable to ~1 ms anyway). Returns 0.0 if no STATUS
    line was captured — callers should warn, not silently join misaligned
    channels.
    """
    import re
    for _, txt in reversed(cap.console):
        m = re.search(r"m7_us=(\d+)\s+m4_us=(\d+)", txt)
        if m:
            return (int(m.group(1)) - int(m.group(2))) * 1e-6
    return 0.0


def m4_clock_offset_s(console_path) -> float:
    """Seconds to ADD to src=1/2 hw_us to put them on the M7 clock.

    THE TWO CORES DO NOT SHARE A CLOCK. src=1 (laser) and src=2 (load) are
    stamped by the M4; src=3/4/6/7 (SMA V/I, CC command, R_est) by the M7. The
    M7 boots first, so its micros() runs AHEAD — measured 2026-07-29 at
    +2.193 s, stable to 1 ms across an 8-minute run.

    Plotting them on one axis without this correction puts the sensors ~2.2 s
    EARLY, which makes the displacement response appear to PEAK BEFORE the
    current pulse that causes it. That cost a whole session: every per-pulse
    displacement and force number measured the decay tail of the PREVIOUS pulse,
    which is why the signs looked random and the amplitudes looked like noise.
    Corrected, the same sweep gives a clean monotonic curve (22.8 -> 367.7 um
    from 150 to 650 mA) and a consistent 128 ms mechanical lag.

    The firmware prints both counters in its STATUS line, so the offset is
    recoverable per session from the saved console log. Returns 0.0 if no STATUS
    line is present — callers that need it should say so rather than silently
    plotting misaligned channels.
    """
    import re
    try:
        with open(console_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"m7_us=(\d+)\s+m4_us=(\d+)", line)
                if m:
                    return (int(m.group(1)) - int(m.group(2))) * 1e-6
    except OSError:
        pass
    return 0.0


def align_m4(cap: Capture, offset_s: float) -> Capture:
    """Return a copy of `cap` with src=1/2 shifted onto the M7 clock."""
    out = Capture(console=list(cap.console))
    for s in cap.samples:
        if s.src in (SRC_LASER, SRC_LOAD):
            out.samples.append(Sample(s.src, s.hw_us + int(offset_s * 1e6),
                                      s.value, s.raw, s.seq))
        else:
            out.samples.append(s)
    return out


def measurement_sane(cap: Capture, windows, v_idle: float = 0.5,
                     r_min_ohm: float = 1.6, max_frac: float = 0.01):
    """Is the current sense telling the truth? Returns (ok, message).

    On 2026-07-28 a whole 9-minute sweep was recorded through a corrupted
    current sense and every downstream number was wrong — including a
    "the CC loop overshoots 50%" conclusion that sent a session chasing the
    controller. The rig fault was never identified; it was present for the
    entire sweep and gone 40 minutes later, and every operating-point variable
    (drive voltage, DAC code, current, load connected, loop open or closed,
    heat or cool) was ruled out. So this does NOT try to diagnose it. It just
    refuses to record data that cannot be physically true.

    The test: during COOL with i_low=0 the DAC parks at code 0, so the wire sees
    a known `v_idle`. Any sample implying R = v_idle/I below `r_min_ohm` is
    impossible. In the corrupted sweep 13-14% of cool samples implied R below
    half the wire's cold resistance; in six healthy captures it is 0.00%.

    `r_min_ohm` MUST TRACK THE WIRE ON THE RIG — it is a fraction (~45%) of the
    cold resistance, not a constant:

        long coil, through 2026-08-04      ~4.2-4.8 ohm  -> guard 2.0
        1.8 ohm coil, 2026-08-04 evening   ~1.8 ohm      -> guard 0.8
        Dynalloy coil, 2026-08-05 (fitted) ~3.5 ohm      -> guard 1.6

    A stale guard aborts every healthy sweep on the first condition: the 1.8 ohm
    coil's idle bias alone draws v_idle/R = 0.5/1.8 = 278 mA, which the 2.0
    guard read as "impossible" and killed sweep_20260804_213705 on condition 1.

    Cheap, needs no root cause, and would have killed that sweep in its first
    second instead of after nine minutes of unusable data.
    """
    i_max = v_idle / r_min_ohm
    rows = sorted((s.hw_us * 1e-6, s.value) for s in cap.samples
                  if s.src == SRC_SMA_I)
    if not rows:
        return True, "no current samples to check"
    hot = [(a, b) for a, b in windows]
    cool = [v for t, v in rows if not any(a <= t <= b for a, b in hot)]
    if len(cool) < 200:
        return True, f"only {len(cool)} cool samples — not enough to judge"
    bad = sum(1 for v in cool if v > i_max)
    frac = bad / len(cool)
    if frac > max_frac:
        return False, (
            f"{100*frac:.2f}% of cool samples exceed {1e3*i_max:.0f} mA, which "
            f"at the {v_idle:.3f} V idle bias implies R < {r_min_ohm:.1f} ohm — "
            f"physically impossible for this wire. EITHER the current sense is "
            f"corrupted (data NOT usable — power-cycle USB + EVM, reseat the "
            f"SMA clips, re-run) OR --r-min is stale for the wire now fitted: "
            f"check its cold resistance and set r_min_ohm to ~45% of it. "
            f"See data/raw/sweep_20260728_215606/README.md.")
    return True, f"{100*frac:.2f}% impossible cool samples (limit {100*max_frac:.0f}%)"


def window_stats(cap: Capture, src: int, t0: float, t1: float):
    rows = [(s.hw_us * 1e-6, s.value) for s in cap.samples if s.src == src]
    vals = [v for t, v in rows if t0 <= t <= t1]
    if not vals:
        return None
    return {"n": len(vals), "min": min(vals), "max": max(vals),
            "mean": sum(vals) / len(vals),
            "clipped": sum(1 for v in vals if v >= SAT_GUARD_V)}


def save_capture(cap: Capture, path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        fh.write("src,hw_us,value,raw_code,seq\n")
        for s in cap.samples:
            fh.write(f"{s.src},{s.hw_us},{s.value:.8f},{s.raw},{s.seq}\n")
    with open(path.with_suffix(".meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    with open(path.with_suffix(".console.log"), "w") as fh:
        for t, txt in cap.console:
            fh.write(f"[{t:8.3f}] {txt}\n")
