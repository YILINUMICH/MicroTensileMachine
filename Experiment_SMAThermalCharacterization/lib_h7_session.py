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
            raise RuntimeError(
                f"{self.port} is not streaming ({n} bytes in {probe_s:.0f}s). "
                f"Power-cycle USB + EVM and retry.")
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
def heat_windows(cap: Capture, gap_s: float = 0.20):
    """Heat phases, from src=6 (cc_u) presence.

    The firmware emits the CC command channel ONLY while the current loop is
    closed, i.e. during a heat phase — so clusters of src=6 separated by a gap
    are exactly the pulses. More reliable than thresholding current, which is
    noisy at low setpoints.
    """
    rows = sorted((s.hw_us * 1e-6 for s in cap.samples if s.src == SRC_CC_U))
    if not rows:
        return []
    out, start, prev = [], rows[0], rows[0]
    for t in rows[1:]:
        if t - prev > gap_s:
            out.append((start, prev))
            start = t
        prev = t
    out.append((start, prev))
    return out


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
