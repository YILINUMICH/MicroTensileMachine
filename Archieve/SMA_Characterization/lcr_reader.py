#!/usr/bin/env python3
"""
lcr_reader.py — Keysight E4980AL LCR meter wrapper for continuous measurement.

Configures the instrument once (FUNC, FREQ, VOLT, APER, TRIG), then streams
measurements as fast as the instrument self-triggers. Returns raw values
(no de-embedding, no filtering) so post-hoc analysis has the full dataset.

Measurement settings default to what was validated in Notion:
  Shape Memory Alloy Coil/Experiment Log/Bias-Tee + LCR Dummy DUT Characterization

Usage:
    with LCRReader(resource="USB0::...", frequency_hz=1e6) as lcr:
        lcr.configure()
        while True:
            m = lcr.fetch()   # LcrMeasurement(ts, primary, secondary, status)
            ...

Standalone smoke test:
    python lcr_reader.py              # auto-detect, 30 s dump
    python lcr_reader.py --resource "USB0::..." --duration 10

Author: Yilin Ma - HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional

try:
    import pyvisa  # type: ignore
except ImportError:
    print("Error: pyvisa is required (`pip install pyvisa`).", file=sys.stderr)
    print("You also need a VISA backend: Keysight IO Libraries or NI-VISA.",
          file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class LcrMeasurement:
    """One FETCh result. Field names reflect the configured FUNC."""
    timestamp: float        # host wall-clock (time.time()) at fetch
    monotonic: float        # host monotonic clock for latency math
    primary: float          # e.g. Ls (H) in LSRS mode
    secondary: float        # e.g. Rs (ohm) in LSRS mode
    status: int             # E4980AL status: 0 = normal, non-zero = error


# ---------------------------------------------------------------------------
# Driver wrapper
# ---------------------------------------------------------------------------
class LCRReader:
    """
    Thin pyvisa wrapper around the Keysight E4980A / E4980AL.

    The constructor only stores settings. Call .open() (or use as a context
    manager) to connect, then .configure() to push the settings to the
    instrument, then .fetch() to read measurements.
    """

    # E4980AL "IDN" responses start with "Keysight Technologies,E4980"
    # or "Agilent Technologies,E4980A" depending on firmware vintage.
    IDN_PREFIXES = ("Keysight Technologies,E4980", "Agilent Technologies,E4980")

    def __init__(self,
                 resource: Optional[str] = None,
                 function: str = "LSRS",
                 frequency_hz: float = 1e6,
                 voltage_V: float = 0.5,
                 integration: str = "SHORT",
                 averaging: int = 1,
                 timeout_ms: int = 10000):   # 10 s — long enough to survive
                                              # an actuation transient
        self.resource = resource
        self.function = function.upper()
        self.frequency_hz = float(frequency_hz)
        self.voltage_V = float(voltage_V)
        self.integration = integration.upper()   # SHOR / MED / LONG
        self.averaging = int(averaging)
        self.timeout_ms = int(timeout_ms)
        self._rm: Optional[pyvisa.ResourceManager] = None
        self._inst = None    # pyvisa.resources.MessageBasedResource
        self._idn: Optional[str] = None
        self.logger = logging.getLogger("LCRReader")

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> None:
        """Open VISA resource manager and connect to the instrument."""
        if self._inst is not None:
            return
        self._rm = pyvisa.ResourceManager()
        resource = self.resource or self._find_e4980()
        if not resource:
            raise RuntimeError(
                "No Keysight E4980 found on the VISA bus. Install Keysight "
                "IO Libraries, verify the instrument is powered and "
                "connected via USB/LAN/GPIB, or specify --resource explicitly."
            )
        self._inst = self._rm.open_resource(resource)
        self._inst.timeout = self.timeout_ms
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        self._idn = self._inst.query("*IDN?").strip()
        self.logger.info("Connected: %s  (resource=%s)", self._idn, resource)

    def close(self) -> None:
        if self._inst is not None:
            try:
                # Restore display before disconnecting so the front panel
                # isn't stuck with "display disabled" for the next user.
                self._inst.write("DISP:ENAB ON")
                self._inst.close()
            except Exception:
                pass
            self._inst = None
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None

    def __enter__(self) -> "LCRReader":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def idn(self) -> Optional[str]:
        return self._idn

    # -- configuration ------------------------------------------------------
    def configure(self) -> None:
        """Push all configured settings to the instrument in one block."""
        self._require_open()
        inst = self._inst
        # Reset to known state before configuring — guarantees no stale
        # compensation/bias/mode from a previous user.
        inst.write("*CLS")
        inst.write("*RST")
        # Explicitly disable all internal corrections (per Notion §1.2).
        inst.write("CORR:OPEN:STAT OFF")
        inst.write("CORR:SHOR:STAT OFF")
        inst.write("CORR:LOAD:STAT OFF")
        # Primary measurement function.
        inst.write(f"FUNC:IMP {self.function}")
        # Sweep settings.
        inst.write(f"FREQ {self.frequency_hz:g}")
        inst.write(f"VOLT:LEV {self.voltage_V:g}")
        # APERture = <integration_time>,<averaging> — SHOR|MED|LONG, N
        inst.write(f"APER {self.integration},{self.averaging}")
        # Disable display redraws during acquisition (saves a few ms/sample).
        inst.write("DISP:ENAB OFF")
        # Free-running internal trigger so FETC? returns fresh data without
        # needing an INIT/TRIG cycle per read.
        inst.write("TRIG:SOUR INT")
        inst.write("INIT:CONT ON")
        inst.query("*OPC?")   # block until all writes committed
        self.logger.info(
            "Configured: FUNC=%s FREQ=%.3g Hz VOLT=%.3g V APER=%s,%d",
            self.function, self.frequency_hz, self.voltage_V,
            self.integration, self.averaging)

    # -- reading ------------------------------------------------------------
    def fetch(self) -> LcrMeasurement:
        """Read one measurement. Blocks up to `timeout_ms`."""
        self._require_open()
        ts = time.time()
        mono = time.monotonic()
        raw = self._inst.query("FETC?").strip()
        # E4980AL FETC? response: "<primary>,<secondary>,<status>"
        # Some firmware adds a 4th field (bin number) — ignore trailing.
        parts = raw.split(",")
        try:
            primary = float(parts[0])
            secondary = float(parts[1])
            status = int(float(parts[2])) if len(parts) > 2 else 0
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"Could not parse FETC? response: {raw!r}") from e
        return LcrMeasurement(timestamp=ts, monotonic=mono,
                              primary=primary, secondary=secondary,
                              status=status)

    def iter_measurements(self, poll_interval_s: float = 0.010,
                          max_consecutive_errors: int = 20,
                          reconnect_on_error: bool = True) \
            -> Iterator[LcrMeasurement]:
        """
        Yield measurements forever at approximately ``poll_interval_s``.

        Transient VISA errors (VI_ERROR_SYSTEM_ERROR etc. — common during
        SMA actuation transients) are caught, logged, and retried. After
        ``max_consecutive_errors`` consecutive failures the iterator
        reraises. If ``reconnect_on_error`` is True, a single reconnect +
        reconfigure attempt is made after the first error in a burst, so
        a USB session dropout doesn't end the whole run.
        """
        next_time = time.monotonic()
        consecutive_errors = 0
        reconnected_this_burst = False
        while True:
            now = time.monotonic()
            if now < next_time:
                time.sleep(next_time - now)
            try:
                yield self.fetch()
                consecutive_errors = 0
                reconnected_this_burst = False
            except pyvisa.errors.VisaIOError as e:
                consecutive_errors += 1
                self.logger.warning(
                    "VISA error #%d on FETC? — %s (retrying)",
                    consecutive_errors, e)
                if consecutive_errors >= max_consecutive_errors:
                    raise
                # First failure in the burst: try a soft reconnect. The
                # instrument usually recovers on its own, but if the USB
                # session was actually lost we need to reopen.
                if reconnect_on_error and not reconnected_this_burst:
                    try:
                        self._soft_reconnect()
                        reconnected_this_burst = True
                    except Exception as rc:
                        self.logger.warning("Reconnect failed: %s", rc)
                # Short sleep to let the transient pass.
                time.sleep(0.1)
            next_time += poll_interval_s

    def _soft_reconnect(self) -> None:
        """Close the current VISA session and re-open + reconfigure."""
        self.logger.info("Attempting soft reconnect...")
        try:
            if self._inst is not None:
                try:
                    self._inst.close()
                except Exception:
                    pass
                self._inst = None
        finally:
            pass
        # Reuse the originally-resolved resource if we already know it.
        resource = self.resource or self._find_e4980()
        if not resource:
            raise RuntimeError("Reconnect: no E4980 visible on VISA bus")
        assert self._rm is not None
        self._inst = self._rm.open_resource(resource)
        self._inst.timeout = self.timeout_ms
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        self.configure()
        self.logger.info("Soft reconnect succeeded.")

    # -- helpers ------------------------------------------------------------
    def _require_open(self) -> None:
        if self._inst is None:
            raise RuntimeError("LCRReader not opened; call .open() first")

    def _find_e4980(self) -> Optional[str]:
        """Scan VISA resources and return the first E4980* IDN match."""
        assert self._rm is not None
        resources = self._rm.list_resources()
        self.logger.debug("VISA resources visible: %s", resources)
        for r in resources:
            try:
                inst = self._rm.open_resource(r)
                inst.timeout = 1500
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                idn = inst.query("*IDN?").strip()
                inst.close()
                if idn.startswith(self.IDN_PREFIXES):
                    return r
            except Exception:
                continue
        return None



# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------
def _smoke_test(resource: Optional[str], duration_s: float,
                frequency_hz: float, voltage_V: float) -> None:
    """Run a short measurement dump to verify the LCR and settings."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    log = logging.getLogger("smoke")

    with LCRReader(resource=resource, frequency_hz=frequency_hz,
                   voltage_V=voltage_V) as lcr:
        lcr.configure()
        log.info("Streaming for %.1f s (primary=Ls, secondary=Rs)", duration_s)
        t_end = time.monotonic() + duration_s
        last_print = 0.0
        n = 0
        primaries: List[float] = []
        secondaries: List[float] = []
        for m in lcr.iter_measurements(poll_interval_s=0.010):
            n += 1
            primaries.append(m.primary)
            secondaries.append(m.secondary)
            now = time.monotonic()
            if now - last_print > 1.0:
                log.info("  n=%5d  Ls=%.4e H  Rs=%.4f ohm  status=%d",
                         n, m.primary, m.secondary, m.status)
                last_print = now
            if now >= t_end:
                break

    if not primaries:
        log.error("No measurements captured.")
        sys.exit(2)

    import statistics as stats
    log.info("-" * 60)
    log.info("captured: %d measurements over %.1f s  (%.1f /s)",
             n, duration_s, n / duration_s)
    log.info("Ls: mean=%.4e H  std=%.2e H",
             stats.fmean(primaries),
             stats.pstdev(primaries) if len(primaries) > 1 else 0.0)
    log.info("Rs: mean=%.4f ohm  std=%.4f ohm",
             stats.fmean(secondaries),
             stats.pstdev(secondaries) if len(secondaries) > 1 else 0.0)


def _main() -> None:
    p = argparse.ArgumentParser(description="LCR meter smoke test")
    p.add_argument("--resource", default=None,
                   help="VISA resource string (default: auto-detect)")
    p.add_argument("--duration", type=float, default=30.0,
                   help="seconds to record (default: 30)")
    p.add_argument("--frequency", type=float, default=1e6,
                   help="measurement frequency in Hz (default: 1e6)")
    p.add_argument("--voltage", type=float, default=0.5,
                   help="test-signal voltage (default: 0.5)")
    args = p.parse_args()
    _smoke_test(args.resource, args.duration, args.frequency, args.voltage)


if __name__ == "__main__":
    _main()
