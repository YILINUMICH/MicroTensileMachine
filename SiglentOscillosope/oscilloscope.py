#!/usr/bin/env python3
"""
oscilloscope.py — Siglent SDS2000X Plus control module.

A raw-TCP-socket SCPI driver for the SDS2000X Plus series, written to mirror
the API shape of the KeysightLCR `lcr_meter.py` module so it drops into the
same worker / recorder architecture (see ../SMA_CharacterizationV2/workers.py).

Why raw sockets instead of PyVISA: the SDS2000X Plus exposes a SCPI socket on
port 5025 and needs no VISA backend. On a direct PC<->scope link (static IP
169.254.111.100, PC on the same APIPA subnet) plain `socket` is the lowest-
latency, lowest-dependency path and is confirmed working in this lab.

The continuous-read path is built around the scope's automatic parameter
measurement query:

    <source>:PAVA? <param>      e.g.  C1:PAVA? PKPK   -> "C1:PAVA PKPK,3.00E+00V"
    C1-C2:PAVA? PHA             cross-channel phase  -> "C1-C2:PAVA PHA,1.17E+02"

so `read_single()` / `iter_measurements()` yield a scalar reading per poll,
exactly like the LCR FETCH? loop. Scope-specific helpers (waveform capture,
built-in AWG, sample-rate query, raw SCPI passthrough) are layered on top.

Main features:
- Raw-socket SCPI transport (no VISA), text + binary block reads
- Auto-connect to the lab default IP, or explicit host[:port]
- Continuous / burst / single automatic-measurement reads (PAVA)
- Cross-channel measurements (phase, skew) via the C1-C2 source syntax
- Built-in AWG (WaveGen) control via BSWV — for isolation / SRF sweeps
- Raw waveform capture (DAT2) returning ADC codes + preamble
- Robust error handling with soft reconnect for long recorder runs
- Context-manager / iterator API matching lcr_meter.LCRMeter

Author: Yilin Ma
University of Michigan Robotics — HDR Lab
"""

from __future__ import annotations

import os
import re
import socket
import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, List, Callable, Iterator

# Library-level logger only. Do NOT call logging.basicConfig here — that would
# install a root handler at import time and steamroll the calling application's
# logging setup (same rule as lcr_meter.py). Standalone scripts configure their
# own logging (see test_oscilloscope.py).
logger = logging.getLogger("Oscilloscope")


# --- Lab defaults (direct PC<->scope link, static IP on the scope) ----------
DEFAULT_HOST = "169.254.111.100"   # scope static IP; PC on same APIPA subnet
DEFAULT_PORT = 5025                # Siglent SCPI socket port
DEFAULT_TIMEOUT_S = 5.0

# Code->volt scaling for DAT2 byte data. The SDS2000X Plus returns one signed
# byte per sample over DAT2; volts = code * (vdiv / CODES_PER_DIV) - offset.
# 25 codes/div is the value used across the Siglent SDS examples, but VERIFY
# against your firmware's Programming Guide before trusting absolute volts —
# the long-record pipeline here normally goes web-server .bin -> Bin2CSV, and
# capture_waveform() is the convenience path for short on-socket grabs.
CODES_PER_DIV = 25.0


class MeasureParam(Enum):
    """Automatic-measurement parameters accepted by `<src>:PAVA? <param>`.

    Single-source params operate on one channel (C1..C4, MATH, etc.).
    Cross-channel params (PHA, SKEW) require a two-source query, e.g.
    `C1-C2:PAVA? PHA`; pass them via ScopeConfig.second_source/second_param
    or build the source string yourself.
    """
    # Amplitude / level
    PKPK = "PKPK"   # peak-to-peak
    MAX = "MAX"
    MIN = "MIN"
    AMPL = "AMPL"   # amplitude (top-base)
    TOP = "TOP"
    BASE = "BASE"
    MEAN = "MEAN"
    CMEAN = "CMEAN"  # cyclic mean
    RMS = "RMS"
    CRMS = "CRMS"   # cyclic RMS
    STDEV = "STDEV"
    # Timing
    FREQ = "FREQ"
    PER = "PER"     # period
    WID = "WID"     # +width
    NWID = "NWID"   # -width
    DUTY = "DUTY"
    NDUTY = "NDUTY"
    RISE = "RISE"
    FALL = "FALL"
    # Cross-channel (two-source)
    PHA = "PHA"     # phase  (C1-C2:PAVA? PHA)
    SKEW = "SKEW"   # skew   (C1-C2:PAVA? SKEW)


# Display unit hint per parameter (for format_result only; the scope already
# encodes a unit suffix in its reply, which read_single strips off).
_PARAM_UNIT = {
    MeasureParam.PKPK: "V", MeasureParam.MAX: "V", MeasureParam.MIN: "V",
    MeasureParam.AMPL: "V", MeasureParam.TOP: "V", MeasureParam.BASE: "V",
    MeasureParam.MEAN: "V", MeasureParam.CMEAN: "V", MeasureParam.RMS: "V",
    MeasureParam.CRMS: "V", MeasureParam.STDEV: "V",
    MeasureParam.FREQ: "Hz", MeasureParam.PER: "s", MeasureParam.WID: "s",
    MeasureParam.NWID: "s", MeasureParam.RISE: "s", MeasureParam.FALL: "s",
    MeasureParam.DUTY: "%", MeasureParam.NDUTY: "%",
    MeasureParam.PHA: "deg", MeasureParam.SKEW: "s",
}


@dataclass
class ScopeConfig:
    """Configuration for the continuous automatic-measurement read loop.

    The primary reading comes from `source:PAVA? param`. Optionally a second
    reading (e.g. cross-channel phase) is fetched each poll and stored in the
    sample's `secondary` field — handy for the SRF magnitude+phase workflow
    (PKPK on C1 as primary, C1-C2 PHA as secondary).
    """
    source: str = "C1"                          # C1..C4, MATH, F1, ...
    param: MeasureParam = MeasureParam.PKPK
    second_source: Optional[str] = None         # e.g. "C1-C2" for phase
    second_param: Optional[MeasureParam] = None  # e.g. MeasureParam.PHA
    settle_s: float = 0.0                       # optional dwell after configure
    chdr_off: bool = True                       # send "CHDR OFF" for bare replies


@dataclass
class ScopeMeasurement:
    """Single measurement result. Field names mirror lcr_meter.MeasurementResult
    so a ScopeWorker can map onto its sample dataclass identically to LcrWorker.
    """
    primary: float                  # value of config.param on config.source
    secondary: float                # value of second_param, or NaN if unused
    status: int                     # 0 = valid; 1 = scope returned "****"/no num
    timestamp: float                # host wall-clock (time.time()) at fetch
    reading_number: int
    monotonic: float = 0.0          # host monotonic clock at fetch (jitter-safe)
    param: str = ""                 # primary parameter name (context)
    unit: str = ""                  # unit suffix parsed from the scope reply

    @property
    def is_valid(self) -> bool:
        return self.status == 0

    @property
    def status_str(self) -> str:
        return "OK" if self.status == 0 else f"ERR:{self.status}"


@dataclass
class WaveformPreamble:
    """Acquisition parameters needed to scale/time a raw DAT2 capture."""
    source: str
    vdiv: float            # volts / division
    offset: float          # vertical offset (V)
    sample_rate: float     # Sa/s (from SARA?)
    points: int            # number of samples returned
    codes_per_div: float = CODES_PER_DIV

    def time_axis(self):
        """Return a numpy time vector (s) starting at 0 for the capture."""
        import numpy as np
        dt = 1.0 / self.sample_rate if self.sample_rate else 1.0
        return np.arange(self.points) * dt


# Regex that pulls the first signed scientific/decimal number out of a reply.
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class Oscilloscope:
    """Siglent SDS2000X Plus interface over a raw SCPI socket (port 5025)."""

    def __init__(self,
                 host: Optional[str] = None,
                 port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT_S,
                 auto_open: bool = True):
        """
        Args:
            host: Scope IP. If None, auto_connect() falls back to $SCOPE_IP
                  then DEFAULT_HOST.
            port: SCPI socket port (5025).
            timeout: Socket timeout in seconds. Long recorder runs that see
                     actuation transients can pass a larger value.
            auto_open: If True, connect from __init__ (backward-compatible).
                       If False, the caller invokes .connect()/.auto_connect()
                       explicitly — used by worker threads so construction is
                       cheap and the SCPI handshake happens inside .run().
        """
        self.sock: Optional[socket.socket] = None
        self.host = host
        self.port = port
        self.timeout = timeout
        self.config = ScopeConfig()
        self._measurement_count = 0
        self._error_count = 0
        self._start_time: Optional[float] = None
        self._idn: Optional[str] = None

        if auto_open:
            if host:
                self.connect(host, port)
            else:
                self.auto_connect()

    # -- transport ---------------------------------------------------------
    def _open_socket(self, host: str, port: int) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(self.timeout)
            s.connect((host, port))
            self.sock = s
            self.host, self.port = host, port
            return True
        except Exception as e:
            logger.debug("Socket open to %s:%d failed: %s", host, port, e)
            self.sock = None
            return False

    def _send(self, cmd: str) -> None:
        if not self.sock:
            raise RuntimeError("Not connected")
        self.sock.sendall((cmd.strip() + "\n").encode("ascii"))

    def _recv_some(self) -> bytes:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise RuntimeError("Socket closed by instrument")
        return chunk

    def query(self, cmd: str) -> str:
        """Send a query and return the text reply (newline-terminated, stripped)."""
        if not self.sock:
            raise RuntimeError("Not connected")
        self._send(cmd)
        buf = bytearray()
        while b"\n" not in buf:
            buf += self._recv_some()
        return buf.split(b"\n", 1)[0].decode("ascii", "replace").strip()

    def write(self, cmd: str) -> None:
        """Send a command with no reply (raw SCPI passthrough)."""
        self._send(cmd)

    def _query_block(self, cmd: str) -> bytes:
        """Send a query whose reply is an IEEE-488.2 definite-length block
        (`#<n><len><bytes>`), used for waveform DAT2 transfers."""
        self._send(cmd)
        buf = bytearray()
        while b"#" not in buf:
            buf += self._recv_some()
        idx = buf.index(b"#")
        while len(buf) < idx + 2:
            buf += self._recv_some()
        ndig = int(buf[idx + 1:idx + 2])
        while len(buf) < idx + 2 + ndig:
            buf += self._recv_some()
        nbytes = int(buf[idx + 2:idx + 2 + ndig])
        start = idx + 2 + ndig
        while len(buf) < start + nbytes:
            buf += self._recv_some()
        return bytes(buf[start:start + nbytes])

    @staticmethod
    def _extract_num(reply: str) -> Optional[float]:
        """Pull the measurement value out of a PAVA-style reply.

        Replies look like 'C1:PAVA PKPK,3.00E+00V' (header on) or '3.00E+00V'
        (CHDR OFF). The value lives after the comma when a header is present.
        Invalid measurements come back as '****' -> returns None.
        """
        tail = reply.split(",", 1)[1] if "," in reply else reply
        if "*" in tail:           # e.g. "****" when no valid signal
            return None
        m = _NUM_RE.search(tail)
        return float(m.group(0)) if m else None

    @staticmethod
    def _extract_unit(reply: str) -> str:
        tail = reply.split(",", 1)[1] if "," in reply else reply
        m = _NUM_RE.search(tail)
        if not m:
            return ""
        return tail[m.end():].strip()

    # -- connection --------------------------------------------------------
    def auto_connect(self) -> bool:
        """Connect to $SCOPE_IP if set, else the lab DEFAULT_HOST."""
        host = self.host or os.environ.get("SCOPE_IP") or DEFAULT_HOST
        port = self.port or int(os.environ.get("SCOPE_PORT", DEFAULT_PORT))
        logger.info("Auto-connecting to SDS2000X Plus at %s:%d ...", host, port)
        return self.connect(host, port)

    def connect(self, host: str, port: Optional[int] = None) -> bool:
        """Connect to an explicit host. `host` may be 'ip' or 'ip:port'."""
        if port is None:
            if ":" in host:
                host, p = host.rsplit(":", 1)
                port = int(p)
            else:
                port = self.port or DEFAULT_PORT
        if not self._open_socket(host, port):
            logger.error("Could not open socket to %s:%d", host, port)
            return False
        try:
            idn = self.query("*IDN?")
        except Exception as e:
            logger.error("No *IDN? response from %s:%d (%s)", host, port, e)
            self.close()
            return False
        # Siglent IDN: "Siglent Technologies,SDS2204X Plus,<serial>,<fw>"
        if "SDS" not in idn.upper() and "SIGLENT" not in idn.upper():
            logger.warning("Connected, but IDN doesn't look like an SDS: %s", idn)
        self._idn = idn
        logger.info("Connected: %s", idn)
        return True

    # -- configuration -----------------------------------------------------
    def configure(self, config: ScopeConfig) -> bool:
        """Apply the read configuration. Light by design: PAVA? works against
        the live acquisition, so this mainly records what to poll, clears the
        status byte, and (optionally) switches to bare-value replies."""
        if not self.sock:
            raise RuntimeError("Not connected")
        self.config = config
        try:
            self.write("*CLS")
            if config.chdr_off:
                # Bare numeric replies; harmless if firmware ignores it because
                # _extract_num handles both header-on and header-off forms.
                self.write("CHDR OFF")
            # Block until the scope has digested the above.
            try:
                self.query("*OPC?")
            except Exception:
                pass
            if config.settle_s:
                time.sleep(config.settle_s)
            logger.info("Configured read: %s:%s%s",
                        config.source, config.param.value,
                        f"  +{config.second_source}:{config.second_param.value}"
                        if config.second_source and config.second_param else "")
            return True
        except Exception as e:
            logger.error("Configuration failed: %s", e)
            return False

    # -- single / continuous reads ----------------------------------------
    def read_single(self) -> Optional[ScopeMeasurement]:
        """Fetch one automatic-measurement reading (primary, + secondary if set)."""
        if not self.sock:
            raise RuntimeError("Not connected")
        cfg = self.config
        try:
            reply = self.query(f"{cfg.source}:PAVA? {cfg.param.value}")
            val = self._extract_num(reply)
            unit = self._extract_unit(reply)
            status = 0 if val is not None else 1

            sec = float("nan")
            if cfg.second_source and cfg.second_param:
                r2 = self.query(f"{cfg.second_source}:PAVA? {cfg.second_param.value}")
                s2 = self._extract_num(r2)
                if s2 is None:
                    status = status or 1
                else:
                    sec = s2

            self._measurement_count += 1
            return ScopeMeasurement(
                primary=val if val is not None else float("nan"),
                secondary=sec,
                status=status,
                timestamp=time.time(),
                reading_number=self._measurement_count,
                monotonic=time.monotonic(),
                param=cfg.param.value,
                unit=unit,
            )
        except Exception as e:
            self._error_count += 1
            logger.debug("Read error: %s", e)
            return None

    def iter_measurements(self,
                          poll_interval_s: float = 0.050,
                          max_consecutive_errors: int = 20,
                          reconnect_on_error: bool = True) -> Iterator[ScopeMeasurement]:
        """Yield measurements forever at ~poll_interval_s.

        Mirrors lcr_meter.iter_measurements: transient socket/parse errors are
        caught, logged, and retried; after `max_consecutive_errors` in a row the
        iterator reraises. With `reconnect_on_error`, one soft reconnect is
        attempted after the first error in a burst so a dropped socket doesn't
        end a long recording.
        """
        if not self.sock:
            raise RuntimeError("Not connected; call .connect()/.auto_connect() first")

        next_time = time.monotonic()
        consecutive = 0
        reconnected = False
        while True:
            now = time.monotonic()
            if now < next_time:
                time.sleep(next_time - now)
            try:
                result = self.read_single()
                if result is None:
                    raise RuntimeError("read_single() returned None")
                yield result
                consecutive = 0
                reconnected = False
            except (OSError, RuntimeError) as e:
                consecutive += 1
                logger.warning("Read error #%d on PAVA? — %s (retrying)",
                               consecutive, e)
                if consecutive >= max_consecutive_errors:
                    raise
                if reconnect_on_error and not reconnected:
                    try:
                        self._soft_reconnect()
                        reconnected = True
                    except Exception as rc:
                        logger.warning("Reconnect failed: %s", rc)
                time.sleep(0.1)
            next_time += poll_interval_s

    def _soft_reconnect(self) -> None:
        """Tear down and re-open the socket, then re-apply the last config."""
        logger.info("Attempting soft reconnect ...")
        try:
            if self.sock:
                try:
                    self.sock.close()
                finally:
                    self.sock = None
        except Exception:
            pass
        if not self.host:
            raise RuntimeError("Soft reconnect: no host remembered")
        if not self._open_socket(self.host, self.port):
            raise RuntimeError(f"Soft reconnect: re-open failed for {self.host}:{self.port}")
        self.configure(self.config)
        logger.info("Soft reconnect succeeded.")

    def read_continuous(self,
                        callback: Optional[Callable[[ScopeMeasurement], bool]] = None,
                        max_readings: Optional[int] = None,
                        max_duration: Optional[float] = None,
                        poll_interval_s: float = 0.0) -> List[ScopeMeasurement]:
        """Continuous read with optional callback. Callback returns False to stop."""
        results: List[ScopeMeasurement] = []
        self._start_time = time.time()
        self._measurement_count = 0
        self._error_count = 0
        logger.info("Starting continuous measurement (Ctrl+C to stop)")
        try:
            while True:
                if max_readings and self._measurement_count >= max_readings:
                    break
                if max_duration and (time.time() - self._start_time) >= max_duration:
                    break
                result = self.read_single()
                if result:
                    results.append(result)
                    if callback and not callback(result):
                        break
                if poll_interval_s:
                    time.sleep(poll_interval_s)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
        self._print_statistics()
        return results

    def read_burst(self, count: int) -> List[ScopeMeasurement]:
        """Read a burst of `count` measurements as fast as the link allows."""
        return self.read_continuous(max_readings=count)

    def _print_statistics(self) -> None:
        if self._start_time:
            dur = time.time() - self._start_time
            rate = self._measurement_count / dur if dur > 0 else 0
            logger.info("Readings: %d in %.2fs  (%.1f rd/s, %d errors)",
                        self._measurement_count, dur, rate, self._error_count)

    # -- scope-specific helpers -------------------------------------------
    def get_sample_rate(self) -> float:
        """Current sample rate in Sa/s, from `SARA?`."""
        val = self._extract_num(self.query("SARA?"))
        return val if val is not None else float("nan")

    def get_timebase(self) -> float:
        """Time/div in seconds, from `TDIV?`."""
        val = self._extract_num(self.query("TDIV?"))
        return val if val is not None else float("nan")

    def clear_sweeps(self) -> None:
        """`CLSW` — reset averaging/persistence statistics. Call at each
        frequency step when using a Math-average trace (the SDS averages via
        the Math function, not the Acquire menu)."""
        self.write("CLSW")

    def set_awg(self,
                wavetype: str = "SINE",
                frequency: float = 1000.0,
                amplitude: float = 1.0,
                offset: float = 0.0,
                source: str = "C1",
                output: Optional[bool] = None,
                load: str = "HZ") -> None:
        """Drive the built-in WaveGen via `BSWV`. Used for isolation / SRF
        frequency sweeps.

        NOTE: the source prefix ('C1') and the BSWV field set are firmware-
        dependent on the built-in generator; this matches the command form
        confirmed on this unit. Adjust `source`/fields if your firmware differs.

        Args:
            wavetype: SINE, SQUARE, RAMP, PULSE, NOISE, DC, ...
            frequency: Hz
            amplitude: Vpp
            offset: V
            source: generator channel prefix
            output: True/False to toggle output, or None to leave unchanged
            load: 'HZ' (high-Z) or '50' — set High-Z so the displayed amplitude
                  matches the real open-circuit output.
        """
        self.write(
            f"{source}:BSWV WVTP,{wavetype},FRQ,{frequency},"
            f"AMP,{amplitude},OFST,{offset}"
        )
        if output is not None:
            self.write(f"{source}:OUTP {'ON' if output else 'OFF'},LOAD,{load}")

    def set_awg_frequency(self, frequency: float, source: str = "C1") -> None:
        """Set only the generator frequency (fast path for a sweep loop)."""
        self.write(f"{source}:BSWV FRQ,{frequency}")

    def capture_waveform(self, source: str = "C1"
                         ) -> Tuple["object", WaveformPreamble]:
        """Grab a single raw waveform over the socket via `WF? DAT2`.

        Returns (codes, preamble) where `codes` is a numpy int8 array of raw
        ADC codes. Convert to volts with codes_to_volts(...) — or, for long
        records, prefer the web-server .bin -> Bin2CSV pipeline.

        Requires numpy.
        """
        import numpy as np
        # Transfer setup: SP sparse interval, NP number of points (0 = all),
        # FP first point. SP,0 / NP,0 returns the full record.
        self.write("WFSU SP,0,NP,0,FP,0")
        vdiv = self._extract_num(self.query(f"{source}:VDIV?")) or 0.0
        ofst = self._extract_num(self.query(f"{source}:OFST?")) or 0.0
        sara = self.get_sample_rate()
        raw = self._query_block(f"{source}:WF? DAT2")
        codes = np.frombuffer(raw, dtype=np.int8)
        pre = WaveformPreamble(source=source, vdiv=vdiv, offset=ofst,
                               sample_rate=sara, points=len(codes))
        return codes, pre

    @property
    def idn(self) -> Optional[str]:
        """*IDN? response captured at connect time, or None if not connected."""
        return self._idn

    def format_result(self, result: ScopeMeasurement) -> str:
        """Human-readable one-liner for a measurement."""
        unit = result.unit or _PARAM_UNIT.get(self.config.param, "")
        s = (f"#{result.reading_number:04d}: "
             f"{result.param}={result.primary:12.6e} {unit}  [{result.status_str}]")
        import math
        if not math.isnan(result.secondary):
            s2 = _PARAM_UNIT.get(self.config.second_param, "") if self.config.second_param else ""
            s += f"   2nd={result.secondary:10.4f} {s2}"
        return s

    def close(self) -> None:
        """Close the socket and clean up."""
        if self.sock:
            try:
                self.write("*CLS")
            except Exception:
                pass
            try:
                self.sock.close()
                logger.info("Connection closed")
            except Exception:
                pass
            self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# --- module-level helpers ---------------------------------------------------
def codes_to_volts(codes, vdiv: float, offset: float,
                   codes_per_div: float = CODES_PER_DIV):
    """Convert raw DAT2 ADC codes to volts: code*(vdiv/codes_per_div) - offset.

    See the CODES_PER_DIV note at the top of this module — verify the constant
    against your firmware's Programming Guide for absolute accuracy.
    """
    import numpy as np
    return np.asarray(codes, dtype=np.float64) * (vdiv / codes_per_div) - offset


def quick_measure(source: str = "C1",
                  param: MeasureParam = MeasureParam.PKPK,
                  count: int = 10,
                  host: Optional[str] = None) -> List[ScopeMeasurement]:
    """One-shot convenience: connect, configure, read `count`, disconnect.

    Example:
        from oscilloscope import quick_measure, MeasureParam
        rds = quick_measure("C1", MeasureParam.PKPK, count=100)
        for r in rds:
            print(r.primary)
    """
    with Oscilloscope(host=host) as scope:
        scope.configure(ScopeConfig(source=source, param=param))
        return scope.read_burst(count)


def measure_with_callback(callback: Callable[[ScopeMeasurement], bool],
                          source: str = "C1",
                          param: MeasureParam = MeasureParam.PKPK,
                          max_readings: Optional[int] = None,
                          max_duration: Optional[float] = None,
                          host: Optional[str] = None) -> List[ScopeMeasurement]:
    """Continuous measure with a per-sample callback (return False to stop)."""
    with Oscilloscope(host=host) as scope:
        scope.configure(ScopeConfig(source=source, param=param))
        return scope.read_continuous(callback=callback,
                                     max_readings=max_readings,
                                     max_duration=max_duration)
