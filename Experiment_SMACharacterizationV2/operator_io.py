"""
operator_io.py — terminal I/O for the SMA recorder session.

The session has three kinds of console output:
  - Prompts: blocking multiple-choice keypresses ([Enter]/[Space]/[Esc]).
  - Progress: a single \\r-overwrite line during phase recording.
  - Banners: one-shot multi-line headers (health, READY-to-fire, session
    start/end).

All log messages from the workers and controller are sent to a session
log file (set up in sma_recorder.py), NOT to stdout — otherwise they
would garble the progress line.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

try:
    import readchar
    from readchar import key as _readchar_key
except ImportError:  # pragma: no cover
    print("Error: readchar is required (`pip install readchar`).",
          file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Key constants (re-exports of readchar.key, named for our use)
# ---------------------------------------------------------------------------
KEY_ENTER = _readchar_key.ENTER          # '\r' on most platforms
KEY_ESC = _readchar_key.ESC              # '\x1b'
KEY_SPACE = _readchar_key.SPACE          # ' '
KEY_CTRL_C = _readchar_key.CTRL_C        # '\x03'


# ---------------------------------------------------------------------------
# Keypress helpers
# ---------------------------------------------------------------------------
def wait_for_key(allowed: Iterable[str]) -> str:
    """
    Block until a keypress matching one of `allowed` arrives.

    Ctrl+C raises KeyboardInterrupt regardless of whether it's in
    `allowed`, so callers can treat it uniformly as an abort signal.
    Unrecognized keys are silently ignored (the prompt keeps waiting).
    """
    allowed_set = set(allowed)
    while True:
        try:
            ch = readchar.readkey()
        except KeyboardInterrupt:
            raise
        if ch == KEY_CTRL_C:
            raise KeyboardInterrupt
        if ch in allowed_set:
            return ch


def prompt(title: str,
           body: str,
           options: list[tuple[str, str, str]]) -> str:
    """
    Render a prompt block and block on a keypress matching one of `options`.

    Args:
        title: short header line, e.g. "OPEN calibration".
        body:  multi-line instructions for the operator.
        options: list of (key_value, label, description) tuples, e.g.
                 [(KEY_ENTER, "Enter", "start"), (KEY_ESC, "Esc", "abort")].
                 Order is preserved in the rendered list.

    Returns the pressed key value (matching one of options' first elements).
    Raises KeyboardInterrupt on Ctrl+C.
    """
    width = 60
    bar = "─" * (width - len(title) - 4)
    print()
    print(f"┌─ {title} {bar}")
    for line in body.splitlines():
        print(f"│ {line}")
    print("│")
    for _key_val, label, desc in options:
        print(f"│   [{label:<5}]  {desc}")
    print("└" + "─" * width)
    sys.stdout.flush()

    return wait_for_key(k for k, _, _ in options)


# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------
@dataclass
class PhaseProgress:
    """
    Single-line \\r-overwrite progress bar for a recording phase.

    Use:
        prog = PhaseProgress("open", duration_s=20.0,
                             lcr_target=2000, h7_target=8000)
        while recording:
            prog.update(elapsed, lcr_n, h7_n)
        prog.finalize(lcr_n, h7_n, duration_s)
    """
    phase_name: str
    duration_s: Optional[float]          # None for unbounded (RAW phase)
    lcr_target: int = 0                  # 0 disables LCR percentage display
    h7_target: int = 0                   # 0 disables H7 percentage display
    _last_render_t: float = field(default=0.0, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _line_width: int = field(default=0, init=False, repr=False)

    REFRESH_INTERVAL_S: float = 0.1      # cap render rate at ~10 Hz

    def update(self, elapsed_s: float, lcr_n: int, h7_n: int,
               *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_render_t) < self.REFRESH_INTERVAL_S:
            return
        self._last_render_t = now
        self._started = True

        if self.duration_s is not None:
            frac = max(0.0, min(1.0, elapsed_s / self.duration_s))
            bar_w = 18
            filled = int(round(frac * bar_w))
            bar = "▓" * filled + "░" * (bar_w - filled)
            time_part = f"{bar}  {elapsed_s:5.1f} / {self.duration_s:5.1f} s"
        else:
            time_part = f"  {elapsed_s:6.1f} s elapsed (Ctrl+C to stop)"

        if self.lcr_target > 0:
            pct = min(100, 100 * lcr_n // max(1, self.lcr_target))
            lcr_part = f"LCR {lcr_n:5d}/{self.lcr_target} ({pct:3d}%)"
        else:
            lcr_part = f"LCR {lcr_n:6d}"
        h7_part = f"H7 {h7_n:6d}"

        line = f"  {self.phase_name.upper():<6} {time_part}  {lcr_part}  {h7_part}"
        # Pad to the widest line we've drawn so trailing chars from a
        # longer previous render get cleared.
        if len(line) > self._line_width:
            self._line_width = len(line)
        sys.stdout.write("\r" + line.ljust(self._line_width))
        sys.stdout.flush()

    def finalize(self, lcr_n: int, h7_n: int, duration_s: float) -> None:
        if self._started:
            sys.stdout.write("\r" + " " * self._line_width + "\r")
        msg = (f"  {self.phase_name.upper():<6} complete — "
               f"{lcr_n} LCR / {h7_n} H7 samples in {duration_s:.2f} s")
        print(msg)
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------
def banner_session_start(session_id: str, output_dir: str) -> None:
    print()
    print("=" * 60)
    print(f"  SMA characterization session: {session_id}")
    print(f"  Output directory:             {output_dir}")
    print("=" * 60)
    sys.stdout.flush()


def banner_health(lcr_pass: bool, lcr_n: int,
                  h7_pass: bool, h7_n: int,
                  timeout_s: float) -> None:
    print()
    print("─" * 60)
    if lcr_pass and h7_pass:
        print(f"  ✔ Health check PASSED ({timeout_s:.1f} s window)")
        print(f"      LCR: {lcr_n} samples ✓")
        print(f"      H7 : {h7_n} samples ✓")
    else:
        print(f"  ✘ Health check FAILED ({timeout_s:.1f} s timeout):")
        if lcr_pass:
            print(f"      LCR: {lcr_n} samples  ✓")
        else:
            print(f"      LCR: {lcr_n} samples  ✘  "
                  f"(check VISA / instrument power / USB cable)")
        if h7_pass:
            print(f"      H7 : {h7_n} samples  ✓")
        else:
            print(f"      H7 : {h7_n} samples  ✘  "
                  f"(check serial port / H7 power / firmware)")
    print("─" * 60)
    sys.stdout.flush()


def banner_ready(lcr_n: int, h7_n: int) -> None:
    """Loud, impossible-to-miss READY-to-fire banner for the RAW phase."""
    width = 60
    print()
    print()
    print("╔" + "═" * width + "╗")
    print("║" + " " * width + "║")
    print("║" + "  READY — APPLY ACTUATION CURRENT NOW".ljust(width) + "║")
    print("║" + " " * width + "║")
    print("║" + f"  Live streams: LCR={lcr_n}, H7={h7_n}".ljust(width) + "║")
    print("║" + "  Press Ctrl+C to stop the recording.".ljust(width) + "║")
    print("║" + " " * width + "║")
    print("╚" + "═" * width + "╝")
    print()
    sys.stdout.flush()


def banner_done(session_id: str,
                completed: bool,
                aborted_at_phase: Optional[str]) -> None:
    print()
    print("=" * 60)
    if completed:
        print(f"  Session {session_id} — COMPLETE")
    else:
        where = aborted_at_phase or "unknown"
        print(f"  Session {session_id} — ABORTED at: {where}")
    print("=" * 60)
    print()
    sys.stdout.flush()
