#!/usr/bin/env python3
"""
portenta_reader.py — USB-CDC serial reader for the Portenta H7 laser-head stream.

The firmware in ``LaserHead_PIO/src/main.cpp`` currently emits ADC2 samples
with this per-sample format (tab-separated)::

    <t_ms>\t<raw_code>\t<voltage_V>\n

with interleaved status/log lines that begin with ``[M4]``, ``[M7]``, or
``[M4 cp N]``. Those lines MUST be ignored by the calibration host.

The calibration plan (Calibrate_LaserHead_Plan.md §2) specifies a cleaner
CSV-in-µs format::

    <timestamp_us>,<voltage_V>\n

This reader parses the *current* firmware output and normalises each sample
to the plan's canonical shape: ``(timestamp_us: int, voltage_V: float)``.
Once the firmware is updated to emit the cleaner CSV format, the parser
below also accepts it directly — both paths are kept live so we can switch
without touching the calibration script.

Run standalone for a 30-second smoke test (plan §9.1)::

    python portenta_reader.py --port COM5 --duration 30

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional

try:
    import serial  # pyserial
except ImportError as e:
    print("Error: pyserial is required (`pip install pyserial`)", file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class Sample:
    """One ADC sample parsed from the Portenta stream."""
    timestamp_us: int          # microseconds since firmware boot
    voltage_V: float           # already scaled by firmware (0–5 V range)
    raw_code: Optional[int] = None   # only set by the current (TSV) firmware

    def as_csv_row(self) -> str:
        """Plan §2 canonical serialisation."""
        return f"{self.timestamp_us},{self.voltage_V:.8f}"


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------
# Three accepted on-wire shapes:
#   1. "<t_ms>\t<raw>\t<voltage>"              — single-ADC build (ADC1 or ADC2
#                                                alone; src column suppressed)
#   2. "<t_ms>\t<src>\t<raw>\t<voltage>"        — dual-ADC build (ADC1 & ADC2
#                                                interleaved; filter by
#                                                adc_source in the reader)
#   3. "<t_us>,<voltage>"                       — plan-spec CSV format
#
# Anything with brackets or alphabetic chars other than '.', 'e', 'E', '+',
# '-' is treated as a log line and dropped.
_FLOAT_RE = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TSV_3COL = re.compile(rf"^\s*(\d+)\s+(-?\d+)\s+({_FLOAT_RE})\s*$")
_TSV_4COL = re.compile(rf"^\s*(\d+)\s+([12])\s+(-?\d+)\s+({_FLOAT_RE})\s*$")
_CSV_PLAN = re.compile(rf"^\s*(\d+)\s*,\s*({_FLOAT_RE})\s*$")


def parse_line(line: str, adc_source: int = 2) -> Optional[Sample]:
    """
    Parse one line of serial output into a Sample, or None if the line is
    a log message / garbage / wrong ADC source.

    Args:
        line: raw line from the Portenta, newline already stripped.
        adc_source: which ADC to keep when the firmware emits both (1 = load
                    cell, 2 = laser head). Only meaningful for the 4-column
                    TSV form.
    """
    if not line or "[" in line:
        return None

    # Plan-spec CSV: "<t_us>,<V>"
    m = _CSV_PLAN.match(line)
    if m:
        try:
            return Sample(timestamp_us=int(m.group(1)),
                          voltage_V=float(m.group(2)))
        except ValueError:
            return None

    # 4-column TSV (ADC1+ADC2 interleaved): "<t_ms>\t<src>\t<raw>\t<V>"
    m = _TSV_4COL.match(line)
    if m:
        src = int(m.group(2))
        if src != adc_source:
            return None
        try:
            return Sample(
                timestamp_us=int(m.group(1)) * 1000,   # ms → µs
                voltage_V=float(m.group(4)),
                raw_code=int(m.group(3)),
            )
        except ValueError:
            return None

    # 3-column TSV (single-ADC build, ADC1 or ADC2 alone): "<t_ms>\t<raw>\t<V>"
    m = _TSV_3COL.match(line)
    if m:
        try:
            return Sample(
                timestamp_us=int(m.group(1)) * 1000,   # ms → µs
                voltage_V=float(m.group(3)),
                raw_code=int(m.group(2)),
            )
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------
class PortentaReader:
    """
    Thin wrapper around pyserial that yields parsed Samples.

    Typical usage::

        with PortentaReader(port="COM5") as r:
            r.drain()                          # throw away stale data
            samples = r.read_samples(n=100)    # block until 100 are captured
    """

    def __init__(self,
                 port: str,
                 baud: int = 115200,
                 timeout_s: float = 1.0,
                 adc_source: int = 2):
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.adc_source = adc_source
        self._ser: Optional[serial.Serial] = None
        self.logger = logging.getLogger("PortentaReader")

    # -- lifecycle ----------------------------------------------------------
    def open(self, boot_wait_s: float = 4.0) -> None:
        """
        Open the serial port and wait for the Portenta firmware to finish
        booting. Opening USB-CDC on Windows can toggle DTR and trigger an
        MCU reset; the M7 bridge then needs ~500 ms to start and the M4
        core waits another 3 s for the ADS1263 power-up before streaming.

        During ``boot_wait_s`` we passively absorb any banner text
        ([M7]/[M4 cp N]/[M4] lines) and log it, so "silent port" vs
        "booting" can be distinguished. After this call, the port is
        ready for drain() + read_samples().
        """
        if self._ser is not None and self._ser.is_open:
            return
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            timeout=0.1,            # short reads — we're polling a boot window
        )
        self.logger.info("Opened %s @ %d baud, waiting %.1fs for firmware boot...",
                         self.port, self.baud, boot_wait_s)

        deadline = time.monotonic() + boot_wait_s
        banner = []
        last_sample_seen = False
        while time.monotonic() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            banner.append(line)
            # Parse attempt — if we see an actual sample, streaming is up
            if parse_line(line, adc_source=self.adc_source) is not None:
                last_sample_seen = True
                break

        # Restore the normal long-ish read timeout for subsequent reads
        self._ser.timeout = self.timeout_s

        if banner:
            self.logger.info("boot banner (%d lines):", len(banner))
            for b in banner[-8:]:   # last few lines is enough
                self.logger.info("  %s", b)
        if not last_sample_seen:
            self.logger.warning(
                "Opened %s but no sample lines seen in %.1fs boot window. "
                "If this persists: power-cycle Hat Carrier, re-seat HAT.",
                self.port, boot_wait_s)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def __enter__(self) -> "PortentaReader":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- reading ------------------------------------------------------------
    def _readline(self) -> str:
        """Blocking readline, returns '' on timeout."""
        assert self._ser is not None, "PortentaReader not opened"
        raw = self._ser.readline()
        if not raw:
            return ""
        try:
            return raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except Exception:
            return ""

    def drain(self, settle_s: float = 0.05, max_time_s: float = 2.0) -> int:
        """
        Throw away everything currently buffered, plus anything that arrives
        within a short ``settle_s`` quiescent window, so subsequent reads
        contain only samples taken after this call. Returns how many bytes
        were discarded (useful for telemetry).
        """
        assert self._ser is not None, "PortentaReader not opened"
        self._ser.reset_input_buffer()
        discarded = 0
        deadline = time.monotonic() + max_time_s
        quiet_until = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            waiting = self._ser.in_waiting
            if waiting:
                discarded += waiting
                self._ser.read(waiting)
                quiet_until = time.monotonic() + settle_s
            elif time.monotonic() >= quiet_until:
                break
            else:
                time.sleep(0.005)
        return discarded

    def iter_samples(self) -> Iterator[Sample]:
        """
        Yield Samples forever. Log lines and malformed rows are silently
        skipped. Caller is responsible for breaking out (see read_samples).
        """
        while True:
            line = self._readline()
            if not line:
                continue
            s = parse_line(line, adc_source=self.adc_source)
            if s is not None:
                yield s

    def read_samples(self, n: int, timeout_s: Optional[float] = None) -> List[Sample]:
        """
        Block until ``n`` valid samples have been collected, or ``timeout_s``
        elapses. Raises TimeoutError if fewer than ``n`` arrive in time.

        Unlike iter_samples(), this enforces the deadline even when NO valid
        samples are yielded — important because a silent/misconfigured
        Portenta produces empty reads forever, and a generator-driven loop
        would never reach its own deadline check.
        """
        out: List[Sample] = []
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        last_report = time.monotonic()
        skipped_nonsample = 0
        while len(out) < n:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"got {len(out)}/{n} samples in {timeout_s:.1f} s "
                    f"(saw {skipped_nonsample} non-sample lines). "
                    "Check: Portenta powered & flashed, correct COM port, "
                    "M7 bridge running (power-cycle Hat Carrier after upload)."
                )
            line = self._readline()
            if not line:
                continue  # serial read timeout — go round, re-check deadline
            s = parse_line(line, adc_source=self.adc_source)
            if s is not None:
                out.append(s)
            else:
                skipped_nonsample += 1
            # Progress ping every 2 s so a slow stream is visible in logs
            # rather than silent until done.
            now = time.monotonic()
            if now - last_report > 2.0:
                self.logger.info(
                    "read_samples progress: %d/%d valid (%d non-sample lines)",
                    len(out), n, skipped_nonsample)
                last_report = now
        return out


# ---------------------------------------------------------------------------
# Standalone smoke test  (plan §9.1)
# ---------------------------------------------------------------------------
def _smoke_test(port: str, baud: int, duration: float) -> None:
    """
    30-second dump test: open port, print every Nth sample, and at the end
    report basic stats so we can verify timestamps are monotonic and
    voltages are sane BEFORE any stage motion.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    log = logging.getLogger("smoke")

    with PortentaReader(port=port, baud=baud) as r:
        r.drain()
        t_start = time.monotonic()
        samples: List[Sample] = []
        last_print = 0.0
        for s in r.iter_samples():
            samples.append(s)
            now = time.monotonic()
            if now - last_print > 1.0:
                log.info("  n=%5d  t_us=%10d  V=%.6f",
                         len(samples), s.timestamp_us, s.voltage_V)
                last_print = now
            if now - t_start >= duration:
                break

    if not samples:
        log.error("NO SAMPLES CAPTURED — check the port, baud rate, and that "
                  "the Portenta is streaming.")
        sys.exit(2)

    ts = [s.timestamp_us for s in samples]
    vs = [s.voltage_V for s in samples]
    monotonic = all(b >= a for a, b in zip(ts, ts[1:]))
    elapsed = (ts[-1] - ts[0]) / 1e6
    rate = len(samples) / elapsed if elapsed > 0 else float("nan")

    log.info("-" * 60)
    log.info("captured:    %d samples over %.2f s  (approx %.1f SPS)",
             len(samples), elapsed, rate)
    log.info("timestamps:  monotonic=%s  first=%d us  last=%d us",
             monotonic, ts[0], ts[-1])
    log.info("voltage:     min=%.6f V  max=%.6f V  mean=%.6f V",
             min(vs), max(vs), sum(vs) / len(vs))

    if not monotonic:
        log.error("TIMESTAMPS NOT MONOTONIC - firmware bug or dropped bytes.")
        sys.exit(3)
    if not (0.0 <= min(vs) and max(vs) <= 5.1):
        log.warning("voltages outside expected 0-5 V range - check wiring.")



def _raw_dump(port: str, baud: int, duration: float) -> None:
    """
    Pure-bytes diagnostic: open the port and print everything that comes
    in, with no parsing.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ser = serial.Serial(port=port, baudrate=baud, timeout=0.5)
    print(f"Listening raw on {port} @ {baud} for {duration:.0f} s...")
    t_end = time.monotonic() + duration
    total = 0
    while time.monotonic() < t_end:
        chunk = ser.read(256)
        if chunk:
            total += len(chunk)
            out = chunk.decode("utf-8", errors="backslashreplace")
            print(out, end="", flush=True)
    ser.close()
    print(f"\n--- total bytes: {total} ---")
    if total == 0:
        print("Port is SILENT. See LaserHead_PIO/README.md 'ID=0x0 triage'.")


def _main() -> None:
    p = argparse.ArgumentParser(
        description="Portenta H7 serial reader - smoke test or raw dump")
    p.add_argument("--port", required=True,
                   help="serial port, e.g. COM8 or /dev/ttyACM0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--duration", type=float, default=30.0,
                   help="seconds to capture (default 30)")
    p.add_argument("--raw", action="store_true",
                   help="raw-bytes mode: dump everything with no parsing")
    args = p.parse_args()
    if args.raw:
        _raw_dump(args.port, args.baud, args.duration)
    else:
        _smoke_test(args.port, args.baud, args.duration)


if __name__ == "__main__":
    _main()
