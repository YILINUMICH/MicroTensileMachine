"""
workers.py — LcrWorker, H7Worker, ZaberWorker for the V3 SMA recorder.

All workers run continuously across the entire session — they are NOT
restarted between phases. They push timestamped sample dataclasses onto
queue.Queue instances; the SessionController is the only consumer that
writes them to per-phase CSV files. Phase transitions therefore don't
disturb any worker or the underlying hardware.

V3 changes vs V2:
  - H7Worker reads the COMBINED firmware stream (src=1 laser, 2 load,
    3 SMA V, 4 SMA I, 5 SMA R) via the canonical portenta_reader with
    adc_source=None, demuxes by channel, and filters by cfg.channels.
    For src=4/5 the `value` field carries amps / ohms (firmware-computed).
  - New ZaberWorker polls the linear stage position.

On hardware failure a worker stores the exception in self.error, sets the
shared stop_event, and exits. Queue overflow is non-blocking (drop +
count) so the instrument loop never back-pressures.

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

from config import LcrConfig, H7Config, StageConfig


# Sibling-module imports via sys.path shims. Each driver/ reader is the
# canonical source of truth — do not re-implement them locally.
_THIS_DIR = Path(__file__).resolve().parent
_KEYSIGHT_DIR = _THIS_DIR.parent / "Driver_KeysightLCR"
_CAL_DIR = _THIS_DIR.parent / "Calibrate_LaserHead"
_ZABER_DIR = _THIS_DIR.parent / "Driver_ZaberStage"
for _dir in (_KEYSIGHT_DIR, _CAL_DIR, _ZABER_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from lcr_meter import (  # noqa: E402  (sys.path shim)
    LCRMeter, MeasurementConfig, MeasurementFunction,
)
from portenta_reader import PortentaReader  # noqa: E402  (sys.path shim)
import zaber_stage  # noqa: E402  (sys.path shim)


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
    firmware_timestamp_us: int           # firmware t_ms*1000 (µs)
    src: Optional[int]                   # 1 laser 2 load 3 sma_v 4 sma_i 5 sma_r
    channel: Optional[str]               # SRC_NAMES[src] ('laser', 'sma_i', ...)
    value: float                         # voltage_V; A for src=4, ohm for src=5
    raw_code: Optional[int]              # ADC raw code (or DAC code for src=3)
    hw_us: Optional[int]                 # firmware µs at acquisition (jitter)
    seq: Optional[int]                   # per-src sequence number (drop detect)


@dataclass
class StageSample:
    host_timestamp_s: float              # time.time() at read
    monotonic_s: float                   # time.monotonic() at read
    position_mm: float                   # absolute stage position


# ---------------------------------------------------------------------------
# LCR worker (unchanged from V2)
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
        self.n_pushed = 0
        self.n_dropped = 0
        self.error: Optional[BaseException] = None
        self.idn: Optional[str] = None
        self.logger = logging.getLogger("LcrWorker")

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

    def _main_loop(self) -> None:
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
            disable_corrections=True,   # SMA workflow de-embeds post-hoc
            disable_display=True,
        )

        lcr = LCRMeter(resource_string=self.cfg.resource,
                       timeout=10_000, auto_open=False)
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
                            "LCR queue full — dropped %d samples", self.n_dropped)
        finally:
            lcr.close()


# ---------------------------------------------------------------------------
# H7 worker (combined sensor + SMA stream, multi-channel)
# ---------------------------------------------------------------------------
class H7Worker(threading.Thread):
    """
    Streams the combined-firmware output from the Portenta H7 over USB-CDC
    and pushes one H7Sample per src line. Keeps only the channels named in
    cfg.channels (laser/load/sma_v/sma_i/sma_r).
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
        self.n_filtered = 0
        self.error: Optional[BaseException] = None
        self.reader: Optional[PortentaReader] = None
        self.logger = logging.getLogger("H7Worker")

    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("H7 worker crashed: %s", e)
            self.stop_event.set()
        finally:
            self.logger.info("H7 worker exit  (pushed=%d, dropped=%d, filtered=%d)",
                             self.n_pushed, self.n_dropped, self.n_filtered)

    def _main_loop(self) -> None:
        keep = set(self.cfg.channels or [])
        # adc_source=None → keep ALL src; we filter by channel name below.
        reader = PortentaReader(
            port=self.cfg.port, baud=self.cfg.baud, adc_source=None)
        self.reader = reader
        with reader:
            reader.drain()
            # Optional inert hook: push any startup commands to the H7.
            for cmd in (self.cfg.startup_commands or []):
                try:
                    reader.send_command(cmd)
                    self.logger.info("H7 startup_command sent: %s", cmd)
                    time.sleep(0.05)
                except Exception as e:  # noqa: BLE001
                    self.logger.warning("H7 startup_command %r failed: %s", cmd, e)
            self.logger.info("H7 ready: port=%s baud=%d channels=%s",
                             self.cfg.port, self.cfg.baud, sorted(keep))
            for s in reader.iter_samples():
                if self.stop_event.is_set():
                    break
                ch = s.channel  # None for srcless 3-col legacy builds
                if keep and ch is not None and ch not in keep:
                    self.n_filtered += 1
                    continue
                sample = H7Sample(
                    host_timestamp_s=time.time(),
                    monotonic_s=time.monotonic(),
                    firmware_timestamp_us=s.timestamp_us,
                    src=s.adc_source,
                    channel=ch,
                    value=s.voltage_V,
                    raw_code=s.raw_code,
                    hw_us=s.hw_us,
                    seq=s.seq,
                )
                try:
                    self.out_queue.put_nowait(sample)
                    self.n_pushed += 1
                except queue.Full:
                    self.n_dropped += 1
                    if self.n_dropped == 1 or self.n_dropped % 200 == 0:
                        self.logger.warning(
                            "H7 queue full — dropped %d samples", self.n_dropped)


# ---------------------------------------------------------------------------
# Zaber stage worker (position telemetry)
# ---------------------------------------------------------------------------
class ZaberWorker(threading.Thread):
    """
    Connects the Zaber linear stage, optionally homes / moves to zero, then
    polls absolute position at cfg.poll_interval_s and pushes StageSample.

    The stage is connected inside run() (this thread owns the serial
    session). The recorder does NOT command motion during a run in V3 —
    the operator drives the stage manually; this worker is telemetry-only.
    """

    def __init__(self, cfg: StageConfig,
                 out_queue: "queue.Queue[StageSample]",
                 stop_event: threading.Event):
        super().__init__(name="ZaberWorker", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self.stop_event = stop_event
        self.n_pushed = 0
        self.n_dropped = 0
        self.error: Optional[BaseException] = None
        self.info: Optional[str] = None
        self._stage = None
        self.logger = logging.getLogger("ZaberWorker")

    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("Zaber worker crashed: %s", e)
            self.stop_event.set()
        finally:
            try:
                if self._stage is not None:
                    self._stage.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.logger.info("Zaber worker exit  (pushed=%d, dropped=%d)",
                             self.n_pushed, self.n_dropped)

    def _main_loop(self) -> None:
        port = self.cfg.port if self.cfg.port else "auto"
        stage = zaber_stage.create_stage(
            port=port,
            position_limits=self.cfg.limits_tuple(),
            max_velocity=self.cfg.max_velocity_mm_s,
            reading_rate=self.cfg.reading_rate_hz,
        )
        if stage is None:
            raise RuntimeError(
                f"Could not connect to Zaber stage on port={port!r}. "
                "Check COM5 / FTDI cable / stage power.")
        self._stage = stage
        try:
            self.info = str(stage.get_device_info())
        except Exception:  # noqa: BLE001
            self.info = "Zaber (info unavailable)"

        if self.cfg.home_on_start:
            self.logger.info("Homing stage...")
            stage.home()
        stage.set_velocity(self.cfg.max_velocity_mm_s)
        if self.cfg.move_to_zero_on_start:
            self.logger.info("Moving to zero_mm=%.3f", self.cfg.zero_mm)
            stage.move_to(self.cfg.zero_mm)

        self.logger.info("Zaber ready: %s", self.info)
        poll = max(self.cfg.poll_interval_s, 1e-3)
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            try:
                pos = stage.get_position()
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Zaber position read failed: {e}") from e
            sample = StageSample(
                host_timestamp_s=time.time(),
                monotonic_s=time.monotonic(),
                position_mm=float(pos),
            )
            try:
                self.out_queue.put_nowait(sample)
                self.n_pushed += 1
            except queue.Full:
                self.n_dropped += 1
                if self.n_dropped == 1 or self.n_dropped % 100 == 0:
                    self.logger.warning(
                        "Stage queue full — dropped %d samples", self.n_dropped)
            # Pace the loop.
            dt = time.monotonic() - t0
            if dt < poll:
                time.sleep(poll - dt)
