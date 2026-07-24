#!/usr/bin/env python3
"""
operator_ccbringup.py — filtered serial console for CC firmware bring-up.

`pio device monitor` is unusable during bring-up: the M4 sensor stream alone is
~800 lines/s and an actuating CC run adds ~3000 more, so firmware replies scroll
past before they can be read and typing is hopeless. This console splits the two
line classes the firmware already separates on the wire:

  [SMA] / [STATUS] / [M7] / [M4] / [ADCx]  -> printed (this is what you read)
  untagged sample TSV (src=1..7)           -> CSV file + a 1 Hz summary line

so the screen carries the conversation and the samples still reach disk. It is a
DIAGNOSTIC tool for the bring-up ladder in
`Firmware_SMAConstantCurrent_PIO/STATUS.md`, not a recorder — for real sessions
use operator_console.py, which time-aligns the H7 against laser/load/camera.

Pairs with `pio run -e portenta_m4_idle -t upload`, which removes the src=1/2
flood at the source. With M4 idle and the SMA disarmed the port is silent.

Usage:
    python operator_ccbringup.py                    # COM8, CSV under data/
    python operator_ccbringup.py --port COM9
    python operator_ccbringup.py --no-csv           # screen only
    python operator_ccbringup.py --summary-hz 0     # no periodic summary

Type firmware commands at the prompt (`info`, `read`, `arm`, `cc 200`, ...).
Console-local commands start with '/':
    /quit          disarm and exit          /mark <text>   note into the CSV
    /help          firmware command summary /quiet         toggle the summary
    /status        print the next [STATUS] in full

SAFETY: the console sends `disarm` on exit (including Ctrl+C and on an
unhandled error). That is a convenience, NOT a substitute for the firmware
watchdog — it cannot fire if the host itself dies. Keep the EVM supply within
reach during bring-up.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# Sibling-module import via a sys.path shim — the canonical reader/parser lives
# in Calibrate_LaserHead and is shared, not copied (the same shim lib_workers
# uses). There is no packaged install of it.
_THIS_DIR = Path(__file__).resolve().parent
_CAL_DIR = _THIS_DIR.parent / "Calibrate_LaserHead"
if str(_CAL_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_DIR))

from portenta_reader import PortentaReader, parse_line  # noqa: E402

# Lines the firmware tags. parse_line() already drops anything containing '[',
# so this classification agrees with the host parser rather than second-guessing
# it: tagged -> screen, untagged+parseable -> CSV.
TAG_PREFIXES = ("[SMA]", "[STATUS]", "[M7]", "[M4]", "[ADC", "[CC]")

# Channels worth summarising, in display order. Others are still logged to CSV.
SUMMARY_CHANNELS = ("sma_v", "sma_i", "sma_r", "cc_u", "cc_r")

# Units for the summary line only — the CSV stays raw. NOTE src=4/5 carry
# amps/ohms in the voltage column; the shared parser documents this.
UNITS = {"sma_v": "V", "sma_i": "A", "sma_r": "ohm",
         "cc_u": "V", "cc_r": "ohm", "laser": "V", "load": "V"}

PROMPT = "h7> "

# [STATUS] is ~300 chars at 1 Hz — as unreadable as the sample flood it was
# meant to replace. By default we print a compressed digest of the fields that
# actually gate the bring-up ladder, and only every --status-every seconds.
# `/status` dumps the next full line verbatim when you need the rest.
STATUS_KEYS = ("sma_state", "loop_hz", "cc", "cc_hz", "cc_i_tgt", "cc_u",
               "cc_r", "cc_tau_ms", "dac_err", "dropped", "crc_err", "hwm",
               "m4_us")

HELP_TEXT = """\
  firmware:  info | read | arm | disarm | stop | abort | ping | reset
  voltage :  set <V> | drive <V> <ms> | fire <V> [ms] | cycle <vh> <vi> <th> <ti> <n>
  current :  cc <mA> [ms] | ccfire <mA> [ms] | cccycle <ih_mA> <il_mA> <th> <ti> <n>
             cc            (bare = controller status)
  tuning  :  tau <ms> | ccgain <Kp> | gain <V/V> | shunt <ohm> | ioffset <V> | aref <V>
  NOTE    :  there is NO `ccdrive` — `cc <mA> [ms]` is the drive twin, and it
             RETARGETS in place if a run is already up.
  console :  /quit | /mark <text> | /quiet | /status | /help
"""


class Accumulator:
    """Per-channel running mean + count over one summary window."""

    def __init__(self) -> None:
        self.n: Dict[str, int] = {}
        self.total: Dict[str, float] = {}

    def add(self, channel: str, value: float) -> None:
        self.n[channel] = self.n.get(channel, 0) + 1
        self.total[channel] = self.total.get(channel, 0.0) + value

    def reset(self) -> None:
        self.n.clear()
        self.total.clear()

    def render(self, window_s: float) -> Optional[str]:
        """One summary line, or None if nothing arrived this window."""
        if not self.n:
            return None
        parts = []
        for ch in SUMMARY_CHANNELS:
            n = self.n.get(ch, 0)
            if n:
                mean = self.total[ch] / n
                parts.append(f"{ch}={mean:.4g}{UNITS.get(ch, '')}")
        # Channels outside SUMMARY_CHANNELS (laser/load) only get a count —
        # their means are meaningless without calibration and would crowd
        # the line that matters.
        other = sum(n for ch, n in self.n.items() if ch not in SUMMARY_CHANNELS)
        total = sum(self.n.values())
        rate = total / window_s if window_s > 0 else 0.0
        head = f"    ~ {rate:6.0f} sample/s"
        if other:
            head += f" ({other} laser/load)"
        return head + ("  " + "  ".join(parts) if parts else "")


class BringupConsole:
    def __init__(self, port: str, baud: int, csv_path: Optional[Path],
                 summary_hz: float, status_every_s: float = 5.0,
                 status_full: bool = False) -> None:
        self.reader = PortentaReader(port=port, baud=baud, adc_source=None)
        self.csv_path = csv_path
        self.summary_period = (1.0 / summary_hz) if summary_hz > 0 else 0.0
        self.status_every_s = status_every_s
        self.status_full = status_full
        self.quiet = False
        self._stop = threading.Event()
        self._acc = Accumulator()
        self._csv = None
        self._n_logged = 0
        self._status_next = 0.0
        self._status_full_once = False

    # -- [STATUS] handling ----------------------------------------------
    def _handle_status(self, line: str) -> None:
        """Throttle + compress [STATUS]. Full text on demand (/status) or with
        --status-full; otherwise a digest of the fields that gate the ladder."""
        now = time.monotonic()
        if not (self._status_full_once or now >= self._status_next):
            return
        self._status_next = now + self.status_every_s
        if self.status_full or self._status_full_once:
            self._status_full_once = False
            self._emit(line)
            return
        # "[STATUS] k=v k=v ..." -> keep only STATUS_KEYS, in that order.
        fields = {}
        for tok in line[len("[STATUS]"):].split():
            k, _, v = tok.partition("=")
            if v:
                fields[k] = v
        kept = [f"{k}={fields[k]}" for k in STATUS_KEYS if k in fields]
        self._emit("[STATUS] " + " ".join(kept) if kept else line)

    # -- output ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        """Print one line and REDRAW THE PROMPT underneath it.

        Without the redraw the '\\r' erases the 'h7> ' that input() wrote, so
        after the first arriving line the console looks dead even though it is
        still accepting keystrokes. Characters already typed are not re-echoed
        (input() owns them, we can't see the partial buffer) — that is the one
        remaining wart, and it is much better than an invisible prompt.
        """
        sys.stdout.write("\r\x1b[K" + text + "\n" + PROMPT)
        sys.stdout.flush()

    # -- input thread ---------------------------------------------------
    def _input_loop(self) -> None:
        """Read stdin, send firmware commands. Runs on its own thread.

        send_command() is documented thread-safe (a lock serialises writers)
        and is bounded by write_timeout_s, so a stalled firmware raises here
        instead of wedging the reader thread.
        """
        while not self._stop.is_set():
            try:
                line = input(PROMPT).strip()
            except (EOFError, KeyboardInterrupt):
                self._stop.set()
                return
            if not line:
                continue
            if line.startswith("/"):
                self._local_command(line)
                continue
            try:
                self.reader.send_command(line)
                self._note_csv(f"# sent: {line}")
            except Exception as e:  # noqa: BLE001
                self._emit(f"!! send failed: {e}")

    def _local_command(self, line: str) -> None:
        cmd, _, rest = line[1:].partition(" ")
        cmd = cmd.lower()
        if cmd in ("quit", "q", "exit"):
            self._stop.set()
        elif cmd == "help":
            self._emit(HELP_TEXT)
        elif cmd == "quiet":
            self.quiet = not self.quiet
            self._emit(f"   summary {'OFF' if self.quiet else 'ON'}")
        elif cmd == "status":
            self._status_full_once = True
            self._status_next = 0.0        # show it on the very next frame
            self._emit("   next [STATUS] will print in full")
        elif cmd == "mark":
            self._note_csv(f"# mark: {rest}")
            self._emit(f"   marked: {rest}")
        else:
            self._emit(f"   unknown console command '/{cmd}' — try /help")

    # -- csv ------------------------------------------------------------
    def _open_csv(self) -> None:
        if self.csv_path is None:
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._csv = self.csv_path.open("w", encoding="utf-8", newline="")
        self._csv.write("host_time_s,src,channel,raw_code,value,hw_us,seq\n")

    def _note_csv(self, text: str) -> None:
        """Drop a comment row so commands/marks are recoverable alongside the
        samples — otherwise the CSV can't tell you which `cc` produced which
        transient."""
        if self._csv is not None:
            self._csv.write(f"# {time.time():.6f} {text}\n")

    def _log_sample(self, s) -> None:
        if self._csv is None:
            return
        self._csv.write(
            f"{time.time():.6f},{s.adc_source},{s.channel or ''},"
            f"{'' if s.raw_code is None else s.raw_code},{s.voltage_V:.8f},"
            f"{'' if s.hw_us is None else s.hw_us},"
            f"{'' if s.seq is None else s.seq}\n")
        self._n_logged += 1

    # -- main loop ------------------------------------------------------
    def run(self) -> int:
        self._open_csv()
        # boot_wait_s=0: the board is already running — we are attaching to a
        # live session mid-bring-up, not waiting out a fresh boot. open() warns
        # when it sees no SAMPLE lines in that window, which is both expected
        # here (zero-length window) and normal for this console (disarmed = no
        # samples at all). Silence it so it can't be mistaken for a fault.
        _rl = logging.getLogger("PortentaReader")
        _prev_level = _rl.level
        _rl.setLevel(logging.ERROR)
        self.reader.open(boot_wait_s=0.0)
        _rl.setLevel(_prev_level)
        try:
            self._emit(f"-- attached to {self.reader.port} @ {self.reader.baud}")
            if self.csv_path:
                self._emit(f"-- samples -> {self.csv_path}")
            self._emit("-- tagged firmware lines below; /help for commands\n")

            t_in = threading.Thread(target=self._input_loop, daemon=True)
            t_in.start()

            next_summary = time.monotonic() + self.summary_period
            window_start = time.monotonic()

            while not self._stop.is_set():
                line = self.reader._readline()   # noqa: SLF001 — see note below
                # _readline() rather than poll_event(): poll_event classifies a
                # line as status/sample and returns None for everything else,
                # which is exactly the [SMA] text this console exists to show.
                if line:
                    if line.startswith("[STATUS]"):
                        self._handle_status(line)
                    elif line.startswith(TAG_PREFIXES):
                        self._emit(line)
                    else:
                        s = parse_line(line, adc_source=None)
                        if s is not None:
                            self._log_sample(s)
                            if s.channel:
                                self._acc.add(s.channel, s.voltage_V)
                        elif line.strip():
                            self._emit(line)     # unrecognised: show, don't eat

                now = time.monotonic()
                if self.summary_period and now >= next_summary:
                    if not self.quiet:
                        text = self._acc.render(now - window_start)
                        if text:
                            self._emit(text)
                    self._acc.reset()
                    window_start = now
                    next_summary = now + self.summary_period
            return 0
        except KeyboardInterrupt:
            # Ctrl+C on the reader thread is a normal exit, not a crash — the
            # finally below still disarms. A traceback here would read as a
            # failure at exactly the moment the operator wants reassurance
            # that the coil was de-energised.
            self._emit("")
            return 0
        finally:
            # Safety net: never leave the coil energised because the operator
            # closed a window. Best-effort — a dead host cannot send this.
            try:
                self.reader.send_command("disarm")
                time.sleep(0.1)
                self._emit("-- sent disarm")
            except Exception as e:  # noqa: BLE001
                # Say so loudly: a silent failure here means the operator
                # believes the coil is safe when it may not be.
                self._emit(f"!! DISARM ON EXIT FAILED ({e}) — cut the EVM supply")
            self.reader.close()
            if self._csv is not None:
                self._csv.close()
                self._emit(f"-- {self._n_logged} samples -> {self.csv_path}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Filtered serial console for CC firmware bring-up.")
    p.add_argument("--port", default="COM8",
                   help="H7 serial port (COM8 on this rig; COM5 is the Zaber)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--csv", default=None,
                   help="sample CSV path (default: data/ccbringup_<ts>.csv)")
    p.add_argument("--no-csv", action="store_true",
                   help="discard the sample stream entirely")
    p.add_argument("--summary-hz", type=float, default=1.0,
                   help="periodic summary rate; 0 disables it")
    p.add_argument("--status-every", type=float, default=5.0,
                   help="seconds between [STATUS] prints (firmware emits 1 Hz)")
    p.add_argument("--status-full", action="store_true",
                   help="print [STATUS] verbatim instead of the digest")
    a = p.parse_args()

    if a.no_csv:
        csv_path = None
    elif a.csv:
        csv_path = Path(a.csv)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = _THIS_DIR / "data" / f"ccbringup_{stamp}.csv"

    return BringupConsole(a.port, a.baud, csv_path, a.summary_hz,
                          a.status_every, a.status_full).run()


if __name__ == "__main__":
    sys.exit(main())
