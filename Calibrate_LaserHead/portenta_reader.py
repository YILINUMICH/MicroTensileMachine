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
import threading
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
# `src` column values — the firmware sample_ring.h reservation table.
#   1,2 come from the M4 ADS1263 (laser / load).
#   3,4,5 come from the M7 SMA drive path (Firmware_SMASensorHub_PIO),
#   emitted as untagged sensor-TSV lines during `drive`/`fire`. For these,
#   the `voltage_V` column carries the channel's natural unit:
#     src=3 → volts (V_ldo),  src=4 → amps (I),  src=5 → ohms (R = V/I).
SRC_LASER = 1   # laser displacement  [V]   (ADS1263 ADC1, M4)
SRC_LOAD  = 2   # load cell force     [V]   (ADS1263 ADC2, M4)
SRC_SMA_V = 3   # SMA drive voltage   [V]   (M7 on-chip ADC; raw_code = DAC code)
SRC_SMA_I = 4   # SMA current         [A]   (INA296A; value in voltage_V)
SRC_SMA_R = 5   # SMA resistance      [ohm] (V/I;   value in voltage_V)

SRC_NAMES = {1: "laser", 2: "load", 3: "sma_v", 4: "sma_i", 5: "sma_r"}
SMA_SRCS  = (SRC_SMA_V, SRC_SMA_I, SRC_SMA_R)


@dataclass
class Sample:
    """One ADC sample parsed from the Portenta stream."""
    timestamp_us: int          # microseconds since firmware boot
    voltage_V: float           # already scaled by firmware (0–5 V range)
    raw_code: Optional[int] = None   # only set by the current (TSV) firmware;
                                     # for src=3 this is the DAC code.
    adc_source: Optional[int] = None # src column when the firmware emits the
                                     # 4-col dual/multi-stream form:
                                     #   1 laser, 2 load (M4 ADS1263);
                                     #   3 SMA V, 4 SMA I, 5 SMA R (M7 SMA path,
                                     #   Firmware_SMASensorHub_PIO). See SRC_*.
                                     # None for 3-col single-channel builds
                                     # or the plan-spec CSV format.
                                     # NOTE: for src=4/5, voltage_V holds the
                                     # current [A] / resistance [ohm], not volts.
    hw_us: Optional[int] = None      # Phase 5: M4-side microsecond
                                     # timestamp captured at DRDY ISR /
                                     # poll instant. ~µs resolution,
                                     # ground truth for jitter analysis.
                                     # None for pre-Phase-5 firmware.
    seq: Optional[int] = None        # Phase 5: per-src monotonic
                                     # sequence number assigned by
                                     # ring_push. Gaps = dropped
                                     # samples between M4 and M7.
                                     # None for pre-Phase-5 firmware.

    def as_csv_row(self) -> str:
        """Plan §2 canonical serialisation."""
        return f"{self.timestamp_us},{self.voltage_V:.8f}"

    @property
    def channel(self) -> Optional[str]:
        """Human-readable channel name for the src ('laser', 'sma_i', ...)."""
        return SRC_NAMES.get(self.adc_source) if self.adc_source is not None else None

    @property
    def is_sma(self) -> bool:
        """True for the M7 SMA-path channels (src=3/4/5)."""
        return self.adc_source in SMA_SRCS


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------
# Three accepted on-wire shapes:
#   1. "<t_ms>\t<raw>\t<voltage>[\t<hw_us>\t<seq>]"
#                                              — single-ADC build (ADC1 or ADC2
#                                                alone; src column suppressed).
#                                                Phase 5 firmware appends
#                                                hw_us + seq; older firmware
#                                                omits them — both parse.
#   2. "<t_ms>\t<src>\t<raw>\t<voltage>[\t<hw_us>\t<seq>]"
#                                              — dual/multi-stream build; src in
#                                                1..5 (1 laser, 2 load from M4;
#                                                3 SMA V, 4 SMA I, 5 SMA R from
#                                                M7's SMA path). Filter by
#                                                adc_source in the reader.
#                                                Same Phase 5 hw_us+seq tail.
#   3. "<t_us>,<voltage>"                       — plan-spec CSV format
#
# Anything with brackets or alphabetic chars other than '.', 'e', 'E', '+',
# '-' is treated as a log line and dropped. In particular every Phase 5
# [STATUS] frame starts with '[' and is therefore dropped here — see
# parse_status_line() below if a consumer needs the diagnostic counters.
_FLOAT_RE = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
# Optional 2-column tail: "\t<hw_us>\t<seq>" added by Phase 5 firmware.
_TAIL_RE  = r"(?:\s+(\d+)\s+(\d+))?"
_TSV_3COL = re.compile(rf"^\s*(\d+)\s+(-?\d+)\s+({_FLOAT_RE}){_TAIL_RE}\s*$")
_TSV_4COL = re.compile(rf"^\s*(\d+)\s+([1-5])\s+(-?\d+)\s+({_FLOAT_RE}){_TAIL_RE}\s*$")
_CSV_PLAN = re.compile(rf"^\s*(\d+)\s*,\s*({_FLOAT_RE})\s*$")


def parse_line(line: str,
               adc_source: Optional[int] = 2) -> Optional[Sample]:
    """
    Parse one line of serial output into a Sample, or None if the line is
    a log message / garbage / wrong ADC source.

    Args:
        line: raw line from the Portenta, newline already stripped.
        adc_source: filter for the 4-column dual/multi-stream form.
                    - 1 → keep only laser samples (ADC1)
                    - 2 → keep only load samples (ADC2)
                    - 3/4/5 → keep only SMA V / I / R samples
                              (Firmware_SMASensorHub_PIO)
                    - None → keep ALL samples; caller demuxes via
                             Sample.adc_source (or .channel). Use this to
                             log sensors + SMA feedback together, or for
                             the cross-compare mode where both ADCs read
                             the same input.
                    Ignored for the 3-col TSV and CSV-plan formats
                    (those don't carry a src tag).
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
    # Phase 5: an optional "\t<hw_us>\t<seq>" tail may follow.
    m = _TSV_4COL.match(line)
    if m:
        src = int(m.group(2))
        if adc_source is not None and src != adc_source:
            return None
        try:
            return Sample(
                timestamp_us=int(m.group(1)) * 1000,   # ms → µs
                voltage_V=float(m.group(4)),
                raw_code=int(m.group(3)),
                adc_source=src,
                hw_us=int(m.group(5)) if m.group(5) else None,
                seq=int(m.group(6)) if m.group(6) else None,
            )
        except ValueError:
            return None

    # 3-column TSV (single-ADC build, ADC1 or ADC2 alone): "<t_ms>\t<raw>\t<V>"
    # Phase 5: an optional "\t<hw_us>\t<seq>" tail may follow.
    m = _TSV_3COL.match(line)
    if m:
        try:
            return Sample(
                timestamp_us=int(m.group(1)) * 1000,   # ms → µs
                voltage_V=float(m.group(3)),
                raw_code=int(m.group(2)),
                hw_us=int(m.group(4)) if m.group(4) else None,
                seq=int(m.group(5)) if m.group(5) else None,
            )
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------------------
# [STATUS] frame parsing (Phase 5)
# ---------------------------------------------------------------------------
# Firmware_SensorHub_PIO M7 emits one diagnostic frame per second:
#   "[STATUS] t_ms=12345 hwm=237 cap=1024 dropped=0 dropped_total=0
#             rate1=400 rate2=400 prod1=400 prod2=400 m4_loops_per_s=120000"
# The main parse_line() drops these (they contain '['). Consumers that
# want the diagnostics can match this prefix and call parse_status_line.

_STATUS_PREFIX = "[STATUS] "
# key=value where value is an int OR a float — vdd/offset/aref are floats.
_STATUS_KV     = re.compile(r"(\w+)=(-?\d+(?:\.\d+)?)")

def parse_status_line(line: str) -> Optional[dict]:
    """Parse a [STATUS] diagnostic frame into a dict of numbers, or None.

    Returns e.g. {'t_ms': 12345, 'hwm': 237, 'dropped': 0, 'crc_err': 0,
                  'overrun': 0, 'm7_us': 9123456, 'm4_us': 9120011,
                  'vdd': 5.5, 'offset': 0.31, 'aref': 3.145, ...}.
    Integer fields come back as int; float fields (vdd/offset/aref) as float.
    Unknown keys are kept as-is so this stays forward-compatible with future
    field additions in the firmware.
    """
    if not line or not line.startswith(_STATUS_PREFIX):
        return None
    out: dict = {}
    for k, v in _STATUS_KV.findall(line[len(_STATUS_PREFIX):]):
        out[k] = float(v) if ("." in v) else int(v)
    return out or None


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
                 adc_source: int = 2,
                 write_timeout_s: float = 0.5,
                 rx_buffer_bytes: int = 4 * 1024 * 1024):
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.adc_source = adc_source
        # Large OS driver RX buffer so a busy host (camera/GUI) can fall seconds
        # behind WITHOUT the firmware's USB-CDC write blocking. The M7 services
        # its heat/cool timer in the same loop as the stream write, so a blocked
        # write stalls the cycle and stretches the cool. At ~130 KB/s this buffer
        # bridges ~30 s of host stall — no back-pressure, no dropped samples.
        # (SetupComm is a request; Windows may cap it — verify what it grants.)
        self.rx_buffer_bytes = rx_buffer_bytes
        # Bound every write so a stalled firmware (USB-CDC RX not being drained)
        # cannot block the caller indefinitely. send_command() runs on the GUI /
        # CSV-writer thread, so an unbounded write() there freezes the whole
        # console; with a timeout it raises SerialTimeoutException instead, which
        # send_command's callers already handle. Commands are a few bytes, so
        # 0.5 s is enormous headroom for a healthy port.
        self.write_timeout_s = write_timeout_s
        self._ser: Optional[serial.Serial] = None
        self._write_lock = threading.Lock()
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
            write_timeout=self.write_timeout_s,  # never block the writer forever
        )
        # Enlarge the driver RX buffer so the M7 never back-pressures (see note
        # in __init__). Best-effort — SetupComm can be capped by the OS driver.
        if self.rx_buffer_bytes:
            try:
                self._ser.set_buffer_size(rx_size=self.rx_buffer_bytes,
                                          tx_size=64 * 1024)
                self.logger.info("Requested RX buffer %.1f MB (driver may cap it)",
                                 self.rx_buffer_bytes / (1024 * 1024))
            except Exception as e:  # noqa: BLE001
                self.logger.warning("set_buffer_size failed: %s", e)
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

    # -- writing (command channel to the combined firmware) ----------------
    def send_command(self, cmd: str) -> None:
        """
        Send one newline-terminated command to the Portenta over the same
        USB-CDC port used for reading (e.g. 'gain 10', 'drive 1.0 500').

        Thread-safe: USB-CDC TX is independent of RX, so this may be called
        from a different thread than the one running iter_samples(); a lock
        serialises concurrent writers. No-op if the port is not open.

        The combined firmware (Firmware_SMASensorHub_PIO) replies with
        '[SMA] '-tagged lines, which parse_line() drops — so command
        responses never pollute the sample stream.
        """
        if self._ser is None or not self._ser.is_open:
            self.logger.warning("send_command(%r) ignored — port not open", cmd)
            return
        payload = (cmd.rstrip("\r\n") + "\n").encode("utf-8")
        with self._write_lock:
            self._ser.write(payload)
            self._ser.flush()
        self.logger.debug("sent command: %s", cmd.strip())

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

    def poll_event(self):
        """Read ONE line and classify it: returns ('status', dict),
        ('sample', Sample), or None on a read timeout / non-data line. Lets a
        caller interleave its own work (e.g. transmitting queued commands on the
        same thread) between reads instead of being trapped in iter_events()'s
        internal loop — the key to keeping all port I/O single-threaded."""
        line = self._readline()
        if not line:
            return None
        st = parse_status_line(line)
        if st is not None:
            return ("status", st)
        s = parse_line(line, adc_source=self.adc_source)
        if s is not None:
            return ("sample", s)
        return None

    def iter_events(self):
        """Yield ('sample', Sample) and ('status', dict) tuples so a single
        consumer can capture BOTH the sample stream and the [STATUS] frames
        from the one USB port. Additive — iter_samples() is unchanged."""
        while True:
            ev = self.poll_event()
            if ev is not None:
                yield ev

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

    def read_samples_dual(self, n_per_adc: int,
                          timeout_s: Optional[float] = None
                          ) -> "tuple[List[Sample], List[Sample]]":
        """
        Block until at least ``n_per_adc`` samples have been collected from
        BOTH ADC1 and ADC2, or ``timeout_s`` elapses. Returns
        ``(samples_adc1, samples_adc2)``.

        Use this when the firmware emits the 4-column dual-stream form
        (``<t_ms>\\t<src>\\t<raw>\\t<V>``), e.g. LaserHead_PIO compiled
        with ENABLE_ADC1 = ENABLE_ADC2 = 1 for the AIN4/AIN5
        cross-compare, or Firmware_SensorHub_PIO in production. The reader's
        ``adc_source`` filter is bypassed (parse_line is called with
        ``adc_source=None``) and the two streams are demuxed on
        ``Sample.adc_source``.

        Both buckets are filled in lockstep — we keep reading until the
        SLOWER channel reaches ``n_per_adc``. The faster channel will
        end up with a few extra samples; the caller can trim or use them
        as-is.

        Raises:
            RuntimeError if every valid sample so far is missing
                ``adc_source`` — that means the firmware is in a
                3-column single-channel mode and cross-compare isn't
                possible. The message points at the firmware build
                flag the operator needs to flip.
            TimeoutError if the slower channel doesn't reach
                ``n_per_adc`` within ``timeout_s``.
        """
        adc1: List[Sample] = []
        adc2: List[Sample] = []
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        last_report = time.monotonic()
        skipped_nonsample = 0
        single_channel_warnings = 0
        while len(adc1) < n_per_adc or len(adc2) < n_per_adc:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"got ADC1={len(adc1)}/{n_per_adc}  "
                    f"ADC2={len(adc2)}/{n_per_adc} samples in "
                    f"{timeout_s:.1f} s (saw {skipped_nonsample} non-sample "
                    "lines). Check: Portenta flashed with ENABLE_ADC1=1 "
                    "AND ENABLE_ADC2=1, both ADCs configured for "
                    "AIN4/AIN5, REF7050 powered."
                )
            line = self._readline()
            if not line:
                continue
            # adc_source=None → keep all samples, demux on Sample.adc_source
            s = parse_line(line, adc_source=None)
            if s is None:
                skipped_nonsample += 1
                continue
            if s.adc_source is None:
                # Single-channel firmware (3-col TSV or CSV-plan format).
                # Track these but don't crash on the first one — the
                # boot banner might emit a stray pre-config sample.
                single_channel_warnings += 1
                if single_channel_warnings >= 5 and not (adc1 or adc2):
                    raise RuntimeError(
                        "Portenta is streaming the 3-column single-channel "
                        "format; dual-ADC cross-compare requires the "
                        "4-column form. Flash LaserHead_PIO/src/main.cpp "
                        "with #define ENABLE_ADC1 1 and #define ENABLE_ADC2 1 "
                        "(both must be set), then power-cycle the rig."
                    )
                continue
            if s.adc_source == 1:
                adc1.append(s)
            elif s.adc_source == 2:
                adc2.append(s)
            # Other src values are unexpected → silently drop (defensive)

            now = time.monotonic()
            if now - last_report > 2.0:
                self.logger.info(
                    "read_samples_dual progress: ADC1=%d/%d  ADC2=%d/%d  "
                    "(%d non-sample lines)",
                    len(adc1), n_per_adc, len(adc2), n_per_adc,
                    skipped_nonsample)
                last_report = now
        return adc1, adc2


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
