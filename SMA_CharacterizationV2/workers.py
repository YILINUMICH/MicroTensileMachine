"""
workers.py — LcrWorker and H7Worker for the SMA characterization recorder.

Both workers run continuously across the entire session — they are NOT
restarted between phases. They push timestamped sample dataclasses onto
queue.Queue instances, and the SessionController is the only consumer
that writes them to per-phase CSV files. Phase transitions in the
controller therefore don't disturb either worker or the underlying
hardware.

On hardware failure or unrecoverable error, a worker:
  - stores the exception in self.error,
  - sets the shared stop_event so the controller's recording loop exits,
  - exits its own run() loop.

Queue overflow is handled non-blockingly: the worker drops the sample
and increments self.n_dropped rather than back-pressuring the
instrument loop. Under nominal operation (controller drains both queues
every ~50 ms) overflow should never happen.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lcr_reader import LCRReader
from config import LcrConfig, H7Config


# Reuse PortentaReader from the sibling Calibrate_LaserHead package via
# a sys.path shim. When the M4/M7 firmware ships, swap this for an
# `h7_reader.py` in this folder.
_THIS_DIR = Path(__file__).resolve().parent
_CAL_DIR = _THIS_DIR.parent / "Calibrate_LaserHead"
if str(_CAL_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_DIR))

from portenta_reader import PortentaReader  # noqa: E402  (sys.path shim)


# ---------------------------------------------------------------------------
# Sample dataclasses (what gets pushed onto the queues)
# ---------------------------------------------------------------------------
@dataclass
class LcrSample:
    host_timestamp_s: float              # time.time() at fetch
    monotonic_s: float                   # time.monotonic() at fetch
    primary: float                       # Ls (H) in LSRS mode
    secondary: float                     # Rs (Ω) in LSRS mode
    status: int                          # E4980 status byte; 0 = normal


@dataclass
class H7Sample:
    host_timestamp_s: float              # time.time() at host parse
    monotonic_s: float                   # time.monotonic() at host parse
    firmware_timestamp_us: int           # uint32 from H7, wraps at ~71 min
    voltage_V: float                     # voltage reported by firmware
    raw_code: Optional[int]              # ADC raw code (int32), if firmware sends


# ---------------------------------------------------------------------------
# LCR worker
# ---------------------------------------------------------------------------
class LcrWorker(threading.Thread):
    """Polls Keysight E4980AL and pushes LcrSample onto out_queue."""

    def __init__(self, cfg: LcrConfig,
                 out_queue: "queue.Queue[LcrSample]",
                 stop_event: threading.Event):
        super().__init__(name="LcrWorker", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self.stop_event = stop_event

        # Stats / state visible to the controller
        self.n_pushed = 0
        self.n_dropped = 0
        self.error: Optional[BaseException] = None
        self.idn: Optional[str] = None

        self.logger = logging.getLogger("LcrWorker")

    # -- thread entry ------------------------------------------------------
    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("LCR worker crashed: %s", e)
            self.stop_event.set()
        finally:
            self.logger.info("LCR worker exit  (pushed=%d, dropped=%d)",
                             self.n_pushed, self.n_dropped)

    # -- main loop ---------------------------------------------------------
    def _main_loop(self) -> None:
        with LCRReader(
            resource=self.cfg.resource,
            function=self.cfg.function,
            frequency_hz=self.cfg.frequency_hz,
            voltage_V=self.cfg.voltage_V,
            integration=self.cfg.integration,
            averaging=self.cfg.averaging,
        ) as lcr:
            lcr.configure()
            self.idn = lcr.idn
            self.logger.info("LCR ready: %s", self.idn)

            for m in lcr.iter_measurements(
                    poll_interval_s=self.cfg.poll_interval_s):
                if self.stop_event.is_set():
                    break
                sample = LcrSample(
                    host_timestamp_s=m.timestamp,
                    monotonic_s=m.monotonic,
                    primary=m.primary,
                    secondary=m.secondary,
                    status=m.status,
                )
                try:
                    self.out_queue.put_nowait(sample)
                    self.n_pushed += 1
                except queue.Full:
                    self.n_dropped += 1
                    if self.n_dropped == 1 or self.n_dropped % 100 == 0:
                        self.logger.warning(
                            "LCR queue full — dropped %d samples (controller "
                            "may be stalled)", self.n_dropped)


# ---------------------------------------------------------------------------
# H7 worker (was LaserWorker)
# ---------------------------------------------------------------------------
class H7Worker(threading.Thread):
    """
    Streams ADS1263 samples from the Portenta H7 over USB-CDC and pushes
    H7Sample onto out_queue.
    """

    def __init__(self, cfg: H7Config,
                 out_queue: "queue.Queue[H7Sample]",
                 stop_event: threading.Event):
        super().__init__(name="H7Worker", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self.stop_event = stop_event

        self.n_pushed = 0
        self.n_dropped = 0
        self.error: Optional[BaseException] = None

        self.logger = logging.getLogger("H7Worker")

    # -- thread entry ------------------------------------------------------
    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("H7 worker crashed: %s", e)
            self.stop_event.set()
        finally:
            self.logger.info("H7 worker exit  (pushed=%d, dropped=%d)",
                             self.n_pushed, self.n_dropped)

    # -- main loop ---------------------------------------------------------
    def _main_loop(self) -> None:
        reader = PortentaReader(
            port=self.cfg.port,
            baud=self.cfg.baud,
            adc_source=self.cfg.adc_source,
        )
        with reader:
            reader.drain()
            self.logger.info("H7 ready: port=%s baud=%d adc=%d",
                             self.cfg.port, self.cfg.baud,
                             self.cfg.adc_source)
            for s in reader.iter_samples():
                if self.stop_event.is_set():
                    break
                sample = H7Sample(
                    host_timestamp_s=time.time(),
                    monotonic_s=time.monotonic(),
                    firmware_timestamp_us=s.timestamp_us,
                    voltage_V=s.voltage_V,
                    raw_code=s.raw_code,
                )
                try:
                    self.out_queue.put_nowait(sample)
                    self.n_pushed += 1
                except queue.Full:
                    self.n_dropped += 1
                    if self.n_dropped == 1 or self.n_dropped % 200 == 0:
                        self.logger.warning(
                            "H7 queue full — dropped %d samples (controller "
                            "may be stalled)", self.n_dropped)
