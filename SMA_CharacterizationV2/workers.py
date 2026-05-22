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

from config import LcrConfig, H7Config


# Sibling-module imports via sys.path shims. KeysightLCR/ owns the LCR
# driver; Calibrate_LaserHead/ owns the H7 serial reader. Both are
# canonical sources of truth — do not re-implement them locally.
_THIS_DIR = Path(__file__).resolve().parent
_KEYSIGHT_DIR = _THIS_DIR.parent / "KeysightLCR"
_CAL_DIR = _THIS_DIR.parent / "Calibrate_LaserHead"
for _dir in (_KEYSIGHT_DIR, _CAL_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from lcr_meter import (  # noqa: E402  (sys.path shim)
    LCRMeter, MeasurementConfig, MeasurementFunction,
)
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
        # Build the canonical KeysightLCR API objects from this recorder's
        # config dataclass. Function name in the YAML is a string ("LSRS");
        # MeasurementFunction(value) maps it back to the enum.
        try:
            func = MeasurementFunction(self.cfg.function.upper())
        except ValueError as e:
            raise RuntimeError(
                f"Unknown LCR function {self.cfg.function!r} in config — "
                f"valid values: {[f.value for f in MeasurementFunction]}"
            ) from e

        meas_cfg = MeasurementConfig(
            frequency=self.cfg.frequency_hz,
            voltage=self.cfg.voltage_V,
            function=func,
            averaging=self.cfg.averaging,
            integration_time=self.cfg.integration,
            disable_corrections=True,  # SMA workflow de-embeds post-hoc
            disable_display=True,      # save a few ms/sample
        )

        # Defer SCPI connect until inside this thread's run() (auto_open=False).
        # Construction stays cheap; the worker thread owns the VISA session.
        # Long timeout to survive SMA actuation transients.
        lcr = LCRMeter(resource_string=self.cfg.resource,
                       timeout=10_000,
                       auto_open=False)
        try:
            connected = (lcr.connect(self.cfg.resource)
                         if self.cfg.resource else lcr.auto_connect())
            if not connected or not lcr.instrument:
                raise RuntimeError(
                    "Could not connect to Keysight E4980 LCR meter. "
                    "Check USB/LAN cable, instrument power, and VISA backend.")
            lcr.configure(meas_cfg)
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
        finally:
            lcr.close()


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
