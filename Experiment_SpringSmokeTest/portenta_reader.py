#!/usr/bin/env python3
"""
portenta_reader.py — USB-CDC serial reader for the Portenta H7 ADC stream
                     (Phase 5 spring smoke test variant).

Extends the calibration-module readers with:

  1. ``[STATUS]`` telemetry-frame parsing. Phase 5 firmware emits one per
     second from M7 with ring high-water mark, dropped-sample count,
     per-src measured sample rate, and an M4-idle-cycles estimate. See
     ``doc/PLAN_phase5_spring_smoke_test.md`` §"Change 3".

  2. Optional ``seq`` and ``hw_us`` columns in the sample TSV. The
     24-byte slot defined in the Phase 5 plan carries a per-src
     monotonic sequence number and an M4-side TIM5 timestamp captured
     at the SPI read — both are essential for proving that 1 kSPS
     actually delivers every sample with bounded jitter. The reader
     accepts whichever column subset the firmware currently emits, so
     this code runs unchanged against current production firmware
     (4-col) and against Phase 5 firmware (5- or 6-col) without a
     gate.

Accepted on-wire shapes (sample lines):

  - ``<t_ms>\\t<raw>\\t<V>``                        (3-col, single-ADC)
  - ``<t_ms>\\t<src>\\t<raw>\\t<V>``                (4-col, current production)
  - ``<t_ms>\\t<src>\\t<seq>\\t<raw>\\t<V>``        (5-col, Phase 5 if hw_us deferred)
  - ``<t_ms>\\t<src>\\t<seq>\\t<hw_us>\\t<raw>\\t<V>``  (6-col, Phase 5 target)
  - ``<t_us>,<V>``                                  (CSV-plan format)

Accepted status-frame shape (one per second):

  ``[STATUS] t_ms=<n> hwm=<n> dropped=<n> rate1=<f> rate2=<f> idle_m4=<n>``

Anything else starting with ``[`` is treated as a log line and dropped
silently (boot banners, RPC checkpoint messages, etc.).

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

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
    """One ADC sample parsed from the Portenta stream.

    ``timestamp_us`` is host-canonical: 4-/5-/6-col TSV firmware emits
    ``t_ms`` (millis on the MCU), which we multiply by 1000 to normalise.
    When the firmware also provides ``hw_us`` (Phase 5 6-col), it is
    preserved separately — that is the field to use for jitter analysis
    because it's captured at SPI-read time on M4, not at the M7 print
    boundary.
    """
    timestamp_us: int                  # ms→µs from firmware (canonical wall-clock proxy)
    voltage_V: float
    raw_code: Optional[int] = None
    adc_source: Optional[int] = None   # 1=laser, 2=load, 3=SMA V, 4=SMA I, 5=SMA R
    seq: Optional[int] = None          # per-src monotonic (Phase 5+)
    hw_us: Optional[int] = None        # M4 TIM5 capture (Phase 5+)

    def as_csv_row(self) -> str:
        return f"{self.timestamp_us},{self.voltage_V:.8f}"


@dataclass
class StatusFrame:
    """Telemetry line emitted by M7 once per second (Phase 5+).

    Unknown keys are preserved in ``extras`` so future firmware additions
    don't need parser changes.
    """
    t_ms: Optional[int] = None         # firmware millis() at frame emission
    hwm: Optional[int] = None          # ring buffer high-water mark, slots
    dropped: Optional[int] = None      # dropped-sample counter (cumulative since boot)
    rates: Dict[int, float] = field(default_factory=dict)   # src → samples/sec
    idle_m4_pct: Optional[int] = None  # rough M4 CPU headroom estimate
    last_cmd_seq: Optional[int] = None # Phase 6: M7→M4 last applied command seq
    raw: str = ""                      # original line (for unparsed-fields debugging)
    extras: Dict[str, str] = field(default_factory=dict)

    @property
    def total_rate(self) -> float:
        return sum(self.rates.values())


Parsed = Union[Sample, StatusFrame]


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------
_FLOAT_RE = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"

# Sample-line formats. Order matters: try the most-specific (longest) first
# so a 6-col line never gets matched as 5- or 4-col.
_TSV_6COL = re.compile(
    rf"^\s*(\d+)\s+([12345])\s+(\d+)\s+(\d+)\s+(-?\d+)\s+({_FLOAT_RE})\s*$"
)
_TSV_5COL = re.compile(
    rf"^\s*(\d+)\s+([12345])\s+(\d+)\s+(-?\d+)\s+({_FLOAT_RE})\s*$"
)
_TSV_4COL = re.compile(
    rf"^\s*(\d+)\s+([12345])\s+(-?\d+)\s+({_FLOAT_RE})\s*$"
)
_TSV_3COL = re.compile(rf"^\s*(\d+)\s+(-?\d+)\s+({_FLOAT_RE})\s*$")
_CSV_PLAN = re.compile(rf"^\s*(\d+)\s*,\s*({_FLOAT_RE})\s*$")

# Status frame. We only require the [STATUS] prefix — any whitespace-
# separated key=val pairs after it are parsed defensively.
_STATUS_PREFIX = re.compile(r"^\s*\[STATUS\]\s*(.*?)\s*$")
_KV_PAIR = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([-+]?[\w.+-]+)")
_RATE_KEY = re.compile(r"^rate(\d+)$")


def _try_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None


def _try_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_status_line(line: str) -> Optional[StatusFrame]:
    """Parse one ``[STATUS] ...`` line or return None."""
    m = _STATUS_PREFIX.match(line)
    if not m:
        return None
    body = m.group(1)
    sf = StatusFrame(raw=line)
    for kv in _KV_PAIR.finditer(body):
        key, val = kv.group(1), kv.group(2)
        if key == "t_ms":
            sf.t_ms = _try_int(val)
        elif key == "hwm":
            sf.hwm = _try_int(val)
        elif key == "dropped":
            sf.dropped = _try_int(val)
        elif key == "idle_m4":
            sf.idle_m4_pct = _try_int(val)
        elif key == "last_cmd_seq":
            sf.last_cmd_seq = _try_int(val)
        else:
            rk = _RATE_KEY.match(key)
            if rk:
                fv = _try_float(val)
                if fv is not None:
                    sf.rates[int(rk.group(1))] = fv
            else:
                sf.extras[key] = val
    return sf


def parse_line(line: str,
               adc_source: Optional[int] = None) -> Optional[Parsed]:
    """Parse one line into a Sample or StatusFrame, or None.

    ``adc_source`` filters sample lines that carry a ``src`` tag (4/5/6-col).
    Pass None to keep every src; the caller demuxes via ``Sample.adc_source``.
    Status frames are always returned regardless of ``adc_source``.
    """
    if not line:
        return None

    # Status frame is the only line type we extract from the ``[`` family.
    # Every other bracketed line ([M4], [M7], [M4 cp N]) is log noise.
    if line.lstrip().startswith("["):
        return parse_status_line(line)

    # Plan-spec CSV
    m = _CSV_PLAN.match(line)
    if m:
        return Sample(timestamp_us=int(m.group(1)),
                      voltage_V=float(m.group(2)))

    # 6-col: <t_ms> <src> <seq> <hw_us> <raw> <V>   (Phase 5 target)
    m = _TSV_6COL.match(line)
    if m:
        src = int(m.group(2))
        if adc_source is not None and src != adc_source:
            return None
        return Sample(
            timestamp_us=int(m.group(1)) * 1000,
            voltage_V=float(m.group(6)),
            raw_code=int(m.group(5)),
            adc_source=src,
            seq=int(m.group(3)),
            hw_us=int(m.group(4)),
        )

    # 5-col: <t_ms> <src> <seq> <raw> <V>
    m = _TSV_5COL.match(line)
    if m:
        src = int(m.group(2))
        if adc_source is not None and src != adc_source:
            return None
        return Sample(
            timestamp_us=int(m.group(1)) * 1000,
            voltage_V=float(m.group(5)),
            raw_code=int(m.group(4)),
            adc_source=src,
            seq=int(m.group(3)),
        )

    # 4-col: <t_ms> <src> <raw> <V>   (current production)
    m = _TSV_4COL.match(line)
    if m:
        src = int(m.group(2))
        if adc_source is not None and src != adc_source:
            return None
        return Sample(
            timestamp_us=int(m.group(1)) * 1000,
            voltage_V=float(m.group(4)),
            raw_code=int(m.group(3)),
            adc_source=src,
        )

    # 3-col: <t_ms> <raw> <V>   (single-ADC builds)
    m = _TSV_3COL.match(line)
    if m:
        return Sample(
            timestamp_us=int(m.group(1)) * 1000,
            voltage_V=float(m.group(3)),
            raw_code=int(m.group(2)),
        )

    return None


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------
StatusCallback = Callable[[StatusFrame], None]


class PortentaReader:
    """Wraps pyserial, yielding parsed Samples + accumulating StatusFrames.

    Status frames received during any read method are stored on
    ``self.status_frames`` and (optionally) dispatched to
    ``status_callback`` synchronously. They never appear in the Sample
    iterators — they are a side channel.
    """

    def __init__(self,
                 port: str,
                 baud: int = 115200,
                 timeout_s: float = 1.0,
                 adc_source: Optional[int] = None,
                 status_callback: Optional[StatusCallback] = None):
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.adc_source = adc_source
        self.status_callback = status_callback
        self.status_frames: List[StatusFrame] = []
        self._ser: Optional[serial.Serial] = None
        self.logger = logging.getLogger("PortentaReader")

    # -- lifecycle ----------------------------------------------------------
    def open(self, boot_wait_s: float = 4.0) -> None:
        if self._ser is not None and self._ser.is_open:
            return
        self._ser = serial.Serial(
            port=self.port, baudrate=self.baud, timeout=0.1,
        )
        self.logger.info(
            "Opened %s @ %d baud, waiting %.1fs for firmware boot...",
            self.port, self.baud, boot_wait_s)

        deadline = time.monotonic() + boot_wait_s
        banner: List[str] = []
        last_sample_seen = False
        while time.monotonic() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            banner.append(line)
            parsed = parse_line(line, adc_source=self.adc_source)
            if isinstance(parsed, StatusFrame):
                self._record_status(parsed)
            elif isinstance(parsed, Sample):
                last_sample_seen = True
                break

        self._ser.timeout = self.timeout_s

        if banner:
            self.logger.info("boot banner (%d lines):", len(banner))
            for b in banner[-8:]:
                self.logger.info("  %s", b)
        if not last_sample_seen:
            self.logger.warning(
                "Opened %s but no sample lines seen in %.1fs boot window. "
                "Power-cycle Mid Carrier or check COM port.",
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

    # -- internal -----------------------------------------------------------
    def _record_status(self, sf: StatusFrame) -> None:
        self.status_frames.append(sf)
        if self.status_callback is not None:
            try:
                self.status_callback(sf)
            except Exception:
                self.logger.exception("status_callback raised; ignored")

    def _readline(self) -> str:
        assert self._ser is not None, "PortentaReader not opened"
        raw = self._ser.readline()
        if not raw:
            return ""
        try:
            return raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except Exception:
            return ""

    def _consume(self, line: str) -> Optional[Sample]:
        """Parse one line; route status frames; return Sample or None."""
        parsed = parse_line(line, adc_source=self.adc_source)
        if isinstance(parsed, StatusFrame):
            self._record_status(parsed)
            return None
        return parsed   # Sample or None

    # -- existing API (preserved, status-aware) -----------------------------
    def drain(self, settle_s: float = 0.05, max_time_s: float = 2.0) -> int:
        assert self._ser is not None
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
        while True:
            line = self._readline()
            if not line:
                continue
            s = self._consume(line)
            if s is not None:
                yield s

    def read_samples(self, n: int,
                     timeout_s: Optional[float] = None) -> List[Sample]:
        out: List[Sample] = []
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        last_report = time.monotonic()
        skipped = 0
        while len(out) < n:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"got {len(out)}/{n} samples in {timeout_s:.1f} s "
                    f"({skipped} non-sample lines, {len(self.status_frames)} "
                    "status frames seen)."
                )
            line = self._readline()
            if not line:
                continue
            s = self._consume(line)
            if s is not None:
                out.append(s)
            else:
                skipped += 1
            now = time.monotonic()
            if now - last_report > 2.0:
                self.logger.info("read_samples: %d/%d  (%d non-sample lines)",
                                 len(out), n, skipped)
                last_report = now
        return out

    # -- streaming capture (new, for the smoke test) ------------------------
    def read_streaming(self, duration_s: float,
                       progress_every_s: float = 5.0
                       ) -> Tuple[List[Sample], List[StatusFrame]]:
        """Drain the port for ``duration_s`` seconds, capturing all samples
        and the status frames received in the window.

        Returns ``(samples, status_frames_in_window)``. The internal
        ``self.status_frames`` accumulator also grows; the returned list
        is a slice limited to frames received in *this* window so callers
        don't have to track an offset themselves.
        """
        samples: List[Sample] = []
        status_start = len(self.status_frames)
        t_end = time.monotonic() + duration_s
        last_report = time.monotonic()
        skipped = 0
        while time.monotonic() < t_end:
            line = self._readline()
            if not line:
                continue
            s = self._consume(line)
            if s is not None:
                samples.append(s)
            else:
                # non-sample line: could be a status (already routed) or noise
                skipped += 1
            now = time.monotonic()
            if now - last_report > progress_every_s:
                self.logger.info(
                    "streaming: %d samples, %d status frames, %d skipped (%.1fs)",
                    len(samples), len(self.status_frames) - status_start,
                    skipped, t_end - now)
                last_report = now
        return samples, self.status_frames[status_start:]


# ---------------------------------------------------------------------------
# Standalone smoke probe
# ---------------------------------------------------------------------------
def _quick_probe(port: str, baud: int, duration: float) -> None:
    """30-second probe: capture samples + status frames, dump summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    log = logging.getLogger("probe")

    with PortentaReader(port=port, baud=baud) as r:
        r.drain()
        samples, statuses = r.read_streaming(duration_s=duration)

    if not samples:
        log.error("NO SAMPLES CAPTURED — check port, baud, firmware.")
        sys.exit(2)

    # Demux by source
    by_src: Dict[Optional[int], List[Sample]] = {}
    for s in samples:
        by_src.setdefault(s.adc_source, []).append(s)

    elapsed = (samples[-1].timestamp_us - samples[0].timestamp_us) / 1e6
    log.info("-" * 64)
    log.info("captured %d samples over %.2f s (%.1f SPS combined)",
             len(samples), elapsed,
             len(samples) / elapsed if elapsed > 0 else float("nan"))
    for src, ss in sorted(by_src.items(),
                          key=lambda kv: -1 if kv[0] is None else kv[0]):
        vs = [s.voltage_V for s in ss]
        rate = len(ss) / elapsed if elapsed > 0 else float("nan")
        has_seq = any(s.seq is not None for s in ss)
        has_hw = any(s.hw_us is not None for s in ss)
        log.info("  src=%s  n=%d  rate=%.1f SPS  V mean=%.4f  σ=%.2e  "
                 "seq=%s  hw_us=%s",
                 src, len(ss), rate, sum(vs) / len(vs),
                 (max(vs) - min(vs)) / max(2, len(vs)),
                 "yes" if has_seq else "no",
                 "yes" if has_hw else "no")

    log.info("status frames: %d", len(statuses))
    if statuses:
        last = statuses[-1]
        log.info("  last: hwm=%s dropped=%s rates=%s idle_m4=%s",
                 last.hwm, last.dropped, last.rates, last.idle_m4_pct)


def _raw_dump(port: str, baud: int, duration: float) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ser = serial.Serial(port=port, baudrate=baud, timeout=0.5)
    print(f"Listening raw on {port} @ {baud} for {duration:.0f} s...")
    t_end = time.monotonic() + duration
    total = 0
    while time.monotonic() < t_end:
        chunk = ser.read(256)
        if chunk:
            total += len(chunk)
            print(chunk.decode("utf-8", errors="backslashreplace"),
                  end="", flush=True)
    ser.close()
    print(f"\n--- total bytes: {total} ---")
    if total == 0:
        print("Port is SILENT. Power-cycle the rig, check COM assignment.")


def _main() -> None:
    p = argparse.ArgumentParser(
        description="Portenta H7 reader — Phase 5 probe / raw dump")
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
        _quick_probe(args.port, args.baud, args.duration)


if __name__ == "__main__":
    _main()
