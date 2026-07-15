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

import contextlib
import csv
import logging
import os
import queue
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from config import CameraConfig, LcrConfig, H7Config, StageConfig

# OpenCV is only needed for the camera worker; import defensively so the rest
# of the recorder runs even where opencv-python isn't installed.
try:
    import cv2  # noqa: E402
    _HAVE_CV2 = True
except Exception:  # noqa: BLE001
    cv2 = None
    _HAVE_CV2 = False


# Windows thread-priority constants (relative to the process priority class).
_WIN_THREAD_PRIORITY = {
    "idle": -15, "lowest": -2, "below_normal": -1, "normal": 0,
    "above_normal": 1, "highest": 2, "time_critical": 15,
}


def _pin_current_thread(core, priority, logger):
    """Best-effort: raise the CURRENT thread's scheduling priority and pin it to
    one CPU core, so heavy work on other threads (the camera) can't preempt it.
    MUST be called from inside the thread it should affect. Windows uses the
    Win32 API via ctypes; other platforms use sched_setaffinity where present.
    Never raises — a failure just logs and leaves the thread at defaults."""
    # priority == "normal"/None means "leave it"; core is None/<0 means "no pin".
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentThread.restype = ctypes.c_void_p
            k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            k32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            k32.SetThreadAffinityMask.restype = ctypes.c_size_t
            h = k32.GetCurrentThread()
            prio = _WIN_THREAD_PRIORITY.get(priority)
            if prio is not None and priority != "normal":
                if not k32.SetThreadPriority(h, prio):
                    logger.warning("SetThreadPriority(%s) failed", priority)
            if core is not None and core >= 0:
                if k32.SetThreadAffinityMask(h, ctypes.c_size_t(1 << core)) == 0:
                    logger.warning("SetThreadAffinityMask(core=%d) failed", core)
            logger.info("H7 reader thread pinned: priority=%s core=%s",
                        priority, core if (core is not None and core >= 0) else "any")
        except Exception as e:  # noqa: BLE001
            logger.warning("could not set reader thread priority/affinity: %s", e)
    else:
        try:
            if core is not None and core >= 0 and hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(0, {core})
                logger.info("H7 reader thread pinned to core %d", core)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not set reader thread affinity: %s", e)


def _resolve_reader_core(cfg):
    """Map cfg.reader_core to an actual core index or None (don't pin).
    -1 = auto (last logical core); >=0 = that core; <=-2 = disabled."""
    rc = getattr(cfg, "reader_core", -1)
    if rc is None or rc <= -2:
        return None
    if rc == -1:
        n = os.cpu_count() or 1
        return n - 1                       # last logical core, usually least busy
    return rc


# A big sensor (the 12MP) reports a >= this width when asked for 4000x3000; a
# webcam clamps far below. Used to tell the 12MP apart from a built-in cam when
# DSHOW indices shift (they are positional, not pinned to the device).
_BIG_SENSOR_MIN_WIDTH = 3000


def _dshow_device_names():
    """DSHOW device names in index order, or None if pygrabber isn't installed.
    OpenCV itself can't enumerate device names, so name-pinning is best-effort."""
    try:
        from pygrabber.dshow_graph import FilterGraph  # optional dep
        return list(FilterGraph().get_input_devices())
    except Exception:  # noqa: BLE001
        return None


def _probe_width(cap):
    """Ask an already-open capture for 4000x3000 MJPG; return the width granted.
    A big sensor grants ~4000; a webcam clamps far below."""
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 4000)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 3000)
    return int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))


def _resolve_camera(cfg, logger, prefer=None, max_probe=4):
    """Find the 12MP camera, robust to DSHOW index shuffling. Returns
    (index, open_cap_or_None) — when the cap is not None the caller OWNS it and
    should reconfigure rather than reopen (a DSHOW open of this camera is ~4 s,
    so reusing the probe handle roughly halves startup / reconnect time).

    Order of trust:
      1. pygrabber name match against cfg.name_hint (if pygrabber is present).
      2. capability probe: the configured/last-used index wins immediately if it
         is the big sensor; otherwise scan indices and take the widest one.
    Raises RuntimeError if nothing capturable is found."""
    names = _dshow_device_names()
    if names:
        logger.info("DSHOW cameras: %s", names)
        hint = (getattr(cfg, "name_hint", "") or "").lower()
        if hint:
            for i, nm in enumerate(names):
                if hint in nm.lower():
                    logger.info("camera matched name '%s' at index %d", nm, i)
                    return i, None

    if not getattr(cfg, "auto_detect", True):
        return cfg.index, None

    # Try the preferred / configured index first so the common case is one probe.
    order, seen = [], set()
    for i in [prefer, cfg.index, *range(max_probe)]:
        if i is not None and i not in seen and i >= 0:
            order.append(i)
            seen.add(i)

    best_idx, best_w = None, 0
    for i in order:
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if not cap.isOpened():
            with contextlib.suppress(Exception):
                cap.release()
            continue
        w = _probe_width(cap)
        logger.info("camera index %d: max width %d", i, w)
        if i in (prefer, cfg.index) and w >= _BIG_SENSOR_MIN_WIDTH:
            return i, cap                  # configured index IS the 12MP — reuse
        with contextlib.suppress(Exception):
            cap.release()
        if w > best_w:
            best_idx, best_w = i, w

    if best_idx is None:
        raise RuntimeError(
            f"no capturable camera found (probed indices {order})")
    logger.info("camera auto-detected at index %d (max width %d)",
                best_idx, best_w)
    return best_idx, None


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


@dataclass
class StatusSample:
    host_timestamp_s: float              # time.time() at host parse
    monotonic_s: float                   # time.monotonic() at host parse
    fields: dict                         # parsed [STATUS] key=value: dropped,
                                         #   crc_err, overrun, m7_us, m4_us,
                                         #   vdd, offset, aref, rate1/2, ...


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
            # Do NOT set the shared stop_event: the LCR is auxiliary, and
            # tripping the global stop here would cascade and kill the critical
            # H7 stream (and the whole session). The worker just records
            # self.error and exits; the console rebuilds it on demand via
            # RecordingCore.restart_worker().
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
                 stop_event: threading.Event,
                 status_queue: "Optional[queue.Queue[StatusSample]]" = None):
        super().__init__(name="H7Worker", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self.status_queue = status_queue
        self.stop_event = stop_event
        self.n_pushed = 0
        self.n_status = 0
        self.n_dropped = 0
        self.n_filtered = 0
        self.n_glitch = 0
        self.error: Optional[BaseException] = None
        self.reader: Optional[PortentaReader] = None
        # Outbound SMA commands. The GUI/core thread only ENQUEUES here; the
        # reader thread drains and actually writes them (see _pump_commands),
        # so the serial port has exactly one thread touching it. Unbounded so a
        # safety 'disarm' can never be refused; commands are tiny and rare.
        self.cmd_queue: "queue.Queue[str]" = queue.Queue()
        # Set once the port is open + drained and the reader is ready to
        # accept commands / stream. Consumers wait on this instead of
        # gating on sample count (the combined firmware emits NO sample
        # lines while idle — SMA src=3/4/5 only appear during actuation).
        self.ready = threading.Event()
        self.logger = logging.getLogger("H7Worker")

    def send_command(self, cmd: str) -> None:
        """Enqueue one command for the reader thread to transmit. Thread-safe,
        non-blocking — safe to call from the GUI/CSV-writer thread. The write
        itself happens on the reader thread so it can never race the read or
        stall the caller (a stalled write only delays this queue, not the UI)."""
        self.cmd_queue.put_nowait(cmd)

    def _pump_commands(self, reader: "PortentaReader") -> None:
        """Drain and transmit queued commands. Runs ONLY on the reader thread."""
        while True:
            try:
                cmd = self.cmd_queue.get_nowait()
            except queue.Empty:
                return
            try:
                reader.send_command(cmd)
            except Exception as e:  # noqa: BLE001
                # A bounded write_timeout turns a stalled port into this, rather
                # than a UI freeze. Log and move on; the command is dropped.
                self.logger.warning("H7 command %r failed: %s", cmd, e)

    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("H7 worker crashed: %s", e)
            # The H7 is critical, but DON'T tear down the shared stop_event
            # here — that would stop the auxiliary streams too and make the
            # worker unrecoverable. A dead/stale H7 is caught by
            # RecordingCore.check_health() (which auto-disarms / aborts), and
            # the operator can rebuild this worker via restart_worker().
        finally:
            self.logger.info("H7 worker exit  (pushed=%d, dropped=%d, filtered=%d)",
                             self.n_pushed, self.n_dropped, self.n_filtered)

    def _main_loop(self) -> None:
        # Runs on the H7 reader thread. Give it priority + a dedicated core so
        # the camera/GUI can't starve it — a starved reader stops draining the
        # serial, which back-pressures the M7 and distorts cycle timing.
        _pin_current_thread(_resolve_reader_core(self.cfg),
                            getattr(self.cfg, "reader_priority", "above_normal"),
                            self.logger)
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
            self.ready.set()
            while not self.stop_event.is_set():
                # This thread is the SOLE owner of the serial port: send any
                # queued outbound commands here (NOT from the GUI thread) so a
                # write can never race the read on the same handle (that caused
                # the ClearCommError crash) nor block the UI/CSV-writer thread.
                self._pump_commands(reader)
                ev = reader.poll_event()
                if ev is None:
                    continue                 # read timeout — loop, recheck stop
                kind, item = ev
                if kind == "status":
                    if self.status_queue is not None:
                        try:
                            self.status_queue.put_nowait(StatusSample(
                                host_timestamp_s=time.time(),
                                monotonic_s=time.monotonic(),
                                fields=item))
                            self.n_status += 1
                        except queue.Full:
                            pass
                    continue
                s = item
                ch = s.channel  # None for srcless 3-col legacy builds
                if keep and ch is not None and ch not in keep:
                    self.n_filtered += 1
                    continue
                # Firmware voltage-glitch guard. The combined firmware emits a
                # laser/load sample with voltage==0 on ~every 32nd ADC1 frame
                # while its raw ADC code is a normal non-zero value — i.e. the
                # reported voltage is internally inconsistent with the raw code
                # (V = code*vref/full_scale can't be 0 for code!=0). It shows
                # up as a huge periodic spike to 0 on the plot. Drop it here;
                # the raw code is correct, only the firmware's V field is bad.
                # Restricted to laser/load: SMA src=4/5 legitimately carry
                # raw_code=0, and src=3 idle voltage can be ~0.
                if (ch in ("laser", "load") and s.voltage_V == 0.0
                        and s.raw_code not in (None, 0)):
                    self.n_glitch += 1
                    if self.n_glitch == 1 or self.n_glitch % 500 == 0:
                        self.logger.warning(
                            "H7 dropped %d laser/load voltage-glitch sample(s) "
                            "(V=0 with non-zero raw code — firmware bug; raw "
                            "stream is otherwise fine)", self.n_glitch)
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

    # -- operator-initiated motion (manual, not part of the phase pipeline) --
    # These are called from the GUI thread. The underlying driver serializes
    # every serial transaction with its own lock, so it is safe to invoke them
    # while this worker's poll loop is reading position on its own thread.
    def request_home(self) -> "tuple[bool, str]":
        """Home the stage. Blocks until the driver returns (homing is a
        firmware routine)."""
        stage = self._stage
        if stage is None:
            return False, "stage not connected"
        ok = stage.home()
        return (True, "homed") if ok else (False, "home command failed")

    def request_move(self, target_mm: float) -> "tuple[bool, str]":
        """Command an absolute go-to. Returns (accepted, message). The move
        itself runs asynchronously on the stage (wait_until_idle=False)."""
        stage = self._stage
        if stage is None:
            return False, "stage not connected"
        if not stage.is_homed():
            return False, "stage not homed — click Home first"
        ok = stage.move_to(target_mm)
        if not ok:
            return False, "move command failed (see session log)"
        lo, hi = self.cfg.limits_tuple()
        clamped = max(lo, min(target_mm, hi))
        if clamped != target_mm:
            return True, (f"moving to {clamped:.3f} mm "
                          f"(clamped from {target_mm:.3f}, limits [{lo}, {hi}])")
        return True, f"moving to {target_mm:.3f} mm"

    def request_stop(self) -> "tuple[bool, str]":
        """Emergency stop: halt any motion immediately. Does not require a
        homed stage — safe to hit at any time."""
        stage = self._stage
        if stage is None:
            return False, "stage not connected"
        ok = stage.stop()
        return (True, "STOPPED") if ok else (False, "stop command failed")

    def set_limits(self, lo_mm: float, hi_mm: float) -> "tuple[bool, str]":
        """Update the soft position-limit window used to clamp go-to moves and
        to judge the workflow-window health warning. Applies to both the live
        driver and this worker's config so clamp messages stay consistent."""
        if not (hi_mm > lo_mm):
            return False, f"invalid window: max ({hi_mm}) must exceed min ({lo_mm})"
        self.cfg.position_limits_mm = [float(lo_mm), float(hi_mm)]
        stage = self._stage
        if stage is not None:
            stage.min_pos = float(lo_mm)
            stage.max_pos = float(hi_mm)
        return True, f"limits set to [{lo_mm:.3f}, {hi_mm:.3f}] mm"

    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("Zaber worker crashed: %s", e)
            # Auxiliary worker — do NOT set the shared stop_event (see LcrWorker
            # note). A stage that fails to open COM5 must not take the session
            # down with it; the console can reconnect it via restart_worker().
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

        # SAFETY: do NOT move the stage at launch. Homing once drove the stage
        # into the fixture and crushed it, so startup motion is opt-in only and
        # off by default. NOTE: `set_velocity()` is a *continuous-motion*
        # command (`axis.move_velocity`), NOT a speed setting — calling it here
        # would start the stage moving, so it is deliberately NOT called at
        # startup. The stage stays exactly where the operator left it; jog it
        # with the console home/go buttons.
        if self.cfg.home_on_start:
            self.logger.info("Homing stage...")
            stage.home()
            if self.cfg.move_to_zero_on_start:
                self.logger.info("Moving to zero_mm=%.3f", self.cfg.zero_mm)
                stage.move_to(self.cfg.zero_mm)
        elif self.cfg.move_to_zero_on_start:
            self.logger.warning(
                "move_to_zero_on_start ignored: needs home_on_start (absolute "
                "move requires a homed stage); leaving stage in place.")

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


# ---------------------------------------------------------------------------
# Camera worker (adaptive-FPS video, fixed resolution)
# ---------------------------------------------------------------------------
class CameraWorker(threading.Thread):
    """Owns the 12MP USB camera. Streams continuously for a live preview, and
    while the console is recording, writes JPEG frames at an ADAPTIVE rate:
    fast while the SMA is moving (net laser displacement > change_threshold_mm,
    median-filtered), a slow heartbeat once it settles. A transient-guarantee
    window forces fast right after each heat/idle command.

    The camera is grabbed at its native rate (grab() paces the loop and keeps
    the buffer fresh); frames are only DECODED (retrieve) when a frame is
    actually needed for preview or writing — so the slow tail costs almost
    nothing. Output: <session>/video/{frames.csv, cycle_NN/*.jpg, snapshots/}.

    Callable hooks (bound by build_core after RecordingCore exists):
      laser_provider()  -> list[(monotonic_s, displacement_mm)]  (recent)
      is_recording()    -> bool
      session_dir()     -> Path | None
      on_event(kind,detail) -> None   (optional, logs to events.csv)
    Auxiliary worker: a failure sets .error but never trips the shared
    stop_event (mirrors LcrWorker/ZaberWorker).
    """

    PREVIEW_WIDTH = 480     # fallback defaults if cfg lacks the fields
    PREVIEW_HZ = 15.0

    def __init__(self, cfg: CameraConfig, stop_event: threading.Event):
        super().__init__(name="CameraWorker", daemon=True)
        self.cfg = cfg
        # Live-view sampling — independent of the capture/recording rate so a
        # cheap preview never limits recording fps (and vice versa).
        self.preview_hz = float(getattr(cfg, "preview_hz", None) or self.PREVIEW_HZ)
        self.preview_width = int(getattr(cfg, "preview_width", None)
                                 or self.PREVIEW_WIDTH)
        self.stop_event = stop_event
        # Hooks (safe defaults; rebound after core creation).
        self.laser_provider: Callable[[], List[Tuple[float, float]]] = lambda: []
        self.is_recording: Callable[[], bool] = lambda: False
        self.session_dir: Callable[[], Optional[Path]] = lambda: None
        self.on_event: Optional[Callable[[str, str], None]] = None

        self.error: Optional[BaseException] = None
        self.info: Optional[str] = None
        self.actual_res: Optional[Tuple[int, int]] = None
        self.actual_fps: Optional[float] = None
        self.n_written = 0
        self.mode = "idle"

        self._cap = None
        self._resolved_index: Optional[int] = None  # last index that opened OK
        self._preview = None
        self._preview_lock = threading.Lock()
        self._fast_until = 0.0
        self._anchor: Optional[float] = None
        self._last_move = 0.0
        self._last_write = 0.0
        self._last_preview = 0.0

        # Recording-file state.
        self._rec_active = False
        self._frames_f = None
        self._frames_w = None
        self._video_dir: Optional[Path] = None
        self._cycle_idx = -1
        self._frame_in_cycle = 0
        self._frame_idx = 0
        self._cur_cycle_dir: Optional[Path] = None
        self._snapshot_pending = False
        # Per-worker stop (separate from the shared stop_event) so resolution/
        # fps changes can restart JUST the camera without touching other streams.
        self._local_stop = threading.Event()
        self.logger = logging.getLogger("CameraWorker")

    def stop_local(self) -> None:
        """Stop only this worker (for a resolution/fps reconnect)."""
        self._local_stop.set()

    def _stop_requested(self) -> bool:
        return self.stop_event.is_set() or self._local_stop.is_set()

    def _interruptible_sleep(self, dur: float) -> bool:
        """Sleep up to `dur` seconds, returning True early if a stop is set."""
        end = time.monotonic() + dur
        while time.monotonic() < end:
            if self._stop_requested():
                return True
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return self._stop_requested()

    # -- public API (called from the GUI / core thread) -------------------
    def mark_fast(self) -> None:
        """Force fast capture for transient_guarantee_s (heat/idle events)."""
        self._fast_until = time.monotonic() + self.cfg.transient_guarantee_s

    def latest_preview(self):
        """Return the most recent BGR frame (ndarray) for the GUI, or None."""
        with self._preview_lock:
            return None if self._preview is None else self._preview

    # -- thread body ------------------------------------------------------
    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:  # noqa: BLE001
            self.error = e
            self.logger.exception("Camera worker crashed: %s", e)
        finally:
            self._close_files()
            if self._cap is not None:
                with_release = getattr(self._cap, "release", None)
                if with_release:
                    try:
                        self._cap.release()
                    except Exception:  # noqa: BLE001
                        pass
            self.logger.info("Camera worker exit (written=%d)", self.n_written)

    def _open(self):
        if not _HAVE_CV2:
            raise RuntimeError("opencv-python (cv2) not installed — camera off")
        # Resolve the device by capability/name (robust to DSHOW index shuffling)
        # rather than trusting a fixed cfg.index. Prefers the last good index so
        # a reconnect is one probe in the common case, and reuses the probe's
        # open handle when possible (a DSHOW open of this camera is ~4 s).
        idx, cap = _resolve_camera(self.cfg, self.logger,
                                   prefer=self._resolved_index)
        if cap is None:
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap is None or not cap.isOpened():
            with contextlib.suppress(Exception):
                if cap is not None:
                    cap.release()
            raise RuntimeError(
                f"cannot open camera index {idx} (check USB/driver)")
        self._resolved_index = idx
        w, h = self.cfg.res_tuple()
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, float(self.cfg.fps_fast))
        aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.actual_res = (aw, ah)
        self.actual_fps = self.cfg.fps_fast
        self.info = f"cam[{idx}] {aw}x{ah} MJPG"
        if (aw, ah) != (int(w), int(h)):
            self.logger.warning("camera granted %dx%d, not requested %dx%d "
                                 "(mode not supported?)", aw, ah, w, h)
        # Warm up (the first couple of reads on DSHOW can be ~1 s each).
        for _ in range(2):
            cap.read()
        return cap

    def _main_loop(self) -> None:
        """Capture loop with a watchdog + auto-reconnect. A USB hiccup used to
        either freeze the loop forever (grab() returning False) or kill the
        thread (a raised exception) with no recovery — unlike every other
        stream. Now a stalled or erroring device is released and reopened, and
        recording state (open frames.csv) survives the reconnect."""
        cap = None
        backoff = 0.5
        last_good = time.monotonic()     # last time the device proved alive
        while not self._stop_requested():
            # (Re)open the camera if we don't currently hold a live handle.
            if cap is None:
                try:
                    cap = self._open()
                    self._cap = cap
                    self.error = None
                    last_good = time.monotonic()
                    backoff = 0.5
                    self.logger.info("Camera ready: %s", self.info)
                except Exception as e:  # noqa: BLE001
                    self.error = e
                    self.logger.warning("camera open failed: %s (retry in %.1fs)",
                                        e, backoff)
                    self._emit("camera", f"open failed: {e}")
                    if self._interruptible_sleep(backoff):
                        break
                    backoff = min(backoff * 2.0, 5.0)
                    continue

            # Grab paces to camera fps and keeps the buffer fresh. A successful
            # grab is our liveness signal; a stall past reconnect_timeout_s or a
            # raised error triggers a reopen.
            try:
                got = cap.grab()
            except Exception as e:  # noqa: BLE001
                self.logger.warning("camera grab raised: %s -> reconnect", e)
                got = False
            now = time.monotonic()
            if not got:
                if now - last_good > self.cfg.reconnect_timeout_s:
                    self.logger.warning("no frame for %.1fs -> reconnecting camera",
                                        now - last_good)
                    self._emit("camera", "stalled -> reconnecting")
                    self._release(cap)
                    cap = None
                    continue
                time.sleep(0.005)
                continue
            last_good = now

            try:
                # Recording edges (driven by the console's Start/Stop REC).
                rec = False
                try:
                    rec = bool(self.is_recording())
                except Exception:  # noqa: BLE001
                    rec = False
                if rec and not self._rec_active:
                    self._open_files()
                elif not rec and self._rec_active:
                    self._close_files()

                mode = self._decide_mode(now)
                entering_fast = (mode == "fast" and self.mode != "fast")
                if self._rec_active and entering_fast:
                    self._begin_cycle()
                self.mode = mode

                interval = (1.0 / self.cfg.fps_fast if mode == "fast"
                            else 1.0 / max(self.cfg.fps_heartbeat, 1e-3))
                need_write = self._rec_active and (now - self._last_write) >= interval
                need_preview = (now - self._last_preview) >= (1.0 / max(self.preview_hz, 1e-3))
                need_snap = self._rec_active and self._snapshot_pending

                if need_write or need_preview or need_snap:
                    ok, frame = cap.retrieve()
                    if ok and frame is not None:
                        if need_snap:
                            self._write_snapshot(frame, now)
                            self._snapshot_pending = False
                        if need_preview:
                            self._update_preview(frame)
                            self._last_preview = now
                        if need_write:
                            self._write_frame(frame, now, mode)
                            self._last_write = now
            except Exception as e:  # noqa: BLE001
                # A decode/write error shouldn't kill the worker; drop the handle
                # and reconnect (recording files stay open across the reopen).
                self.logger.warning("camera loop error: %s -> reconnect", e)
                self._emit("camera", f"loop error: {e}")
                self._release(cap)
                cap = None
                continue

            # Pace the loop. cv2.VideoCapture.grab() on CAP_DSHOW does NOT block
            # to the frame rate (measured ~millions of calls/s) — without this
            # sleep the loop busy-spins a whole CPU core, starving the Qt event
            # loop and the H7 serial reader (the "console lags once the camera is
            # online" symptom). grab() is ~free, so pacing to fps_fast keeps the
            # driver buffer fresh (low-latency preview) at negligible CPU.
            period = 1.0 / max(float(self.cfg.fps_fast), 1.0)
            elapsed = time.monotonic() - now
            if elapsed < period:
                time.sleep(period - elapsed)

    def _release(self, cap) -> None:
        """Safely release a capture handle (ignore driver errors on teardown)."""
        if cap is None:
            return
        with contextlib.suppress(Exception):
            cap.release()
        if self._cap is cap:
            self._cap = None

    # -- adaptive-rate decision ------------------------------------------
    def _filtered_laser(self, now: float) -> Optional[float]:
        try:
            hist = self.laser_provider()
        except Exception:  # noqa: BLE001
            hist = []
        if not hist:
            return None
        win = self.cfg.median_window_ms / 1000.0
        recent = [mm for (t, mm) in hist if (now - t) <= win]
        if not recent:
            recent = [hist[-1][1]]
        return statistics.median(recent)

    def _decide_mode(self, now: float) -> str:
        if now < self._fast_until:
            return "fast"
        cur = self._filtered_laser(now)
        if cur is None:
            return "heartbeat"          # no laser signal -> event-driven only
        if self._anchor is None:
            self._anchor = cur
            self._last_move = now
            return "fast"
        if abs(cur - self._anchor) >= self.cfg.change_threshold_mm:
            self._anchor = cur          # re-anchor on each real move
            self._last_move = now
            return "fast"
        if (now - self._last_move) < self.cfg.stop_dwell_s:
            return "fast"               # still within the settle dwell
        return "heartbeat"

    # -- file output ------------------------------------------------------
    def _open_files(self) -> None:
        sd = None
        try:
            sd = self.session_dir()
        except Exception:  # noqa: BLE001
            sd = None
        if sd is None:
            return
        self._video_dir = Path(sd) / "video"
        (self._video_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        self._frames_f = open(self._video_dir / "frames.csv", "w", newline="")
        self._frames_w = csv.writer(self._frames_f)
        self._frames_w.writerow(["frame_idx", "host_timestamp_s", "monotonic_s",
                                 "cycle_idx", "mode", "rel_path", "laser_mm"])
        self._cycle_idx = -1
        self._frame_idx = 0
        self._snapshot_pending = False
        self._rec_active = True
        self.mode = "idle"              # force a fresh cycle on first fast frame
        self.mark_fast()                # start recording fast
        self._emit("camera", "video recording start")

    def _close_files(self) -> None:
        if self._frames_f is not None:
            with contextlib.suppress(Exception):
                self._frames_f.flush()
                self._frames_f.close()
        self._frames_f = self._frames_w = None
        if self._rec_active:
            self._emit("camera", f"video recording stop ({self.n_written} frames)")
        self._rec_active = False

    def _begin_cycle(self) -> None:
        self._cycle_idx += 1
        self._frame_in_cycle = 0
        self._cur_cycle_dir = self._video_dir / f"cycle_{self._cycle_idx:02d}"
        self._cur_cycle_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_pending = True   # capture a reference frame at onset

    def _write_frame(self, frame, now: float, mode: str) -> None:
        if self._cur_cycle_dir is None:
            self._begin_cycle()
        path = self._cur_cycle_dir / f"{self._frame_in_cycle:06d}.jpg"
        self._save_jpeg(frame, path)
        self._index_row(path, now, self._cycle_idx, mode)
        self._frame_in_cycle += 1
        self.n_written += 1
        if self.n_written % 30 == 0 and self._frames_f is not None:
            self._frames_f.flush()

    def _write_snapshot(self, frame, now: float) -> None:
        ts = int(now * 1000)
        path = (self._video_dir / "snapshots"
                / f"cyc{self._cycle_idx:02d}_{ts}.jpg")
        self._save_jpeg(frame, path)
        self._index_row(path, now, self._cycle_idx, "snap")

    def _index_row(self, path: Path, now: float, cycle: int, mode: str) -> None:
        if self._frames_w is None:
            return
        laser = self._filtered_laser(now)
        rel = path.relative_to(self._video_dir).as_posix()
        self._frames_w.writerow([
            self._frame_idx, f"{time.time():.6f}", f"{now:.6f}",
            cycle, mode, rel, "" if laser is None else f"{laser:.4f}"])
        self._frame_idx += 1

    def _save_jpeg(self, frame, path: Path) -> None:
        cv2.imwrite(str(path), frame,
                    [cv2.IMWRITE_JPEG_QUALITY, int(self.cfg.jpeg_quality)])

    def _update_preview(self, frame) -> None:
        h, w = frame.shape[:2]
        if w > self.preview_width:
            scale = self.preview_width / float(w)
            small = cv2.resize(frame, (self.preview_width, int(h * scale)))
        else:
            small = frame.copy()
        with self._preview_lock:
            self._preview = small

    def _emit(self, kind: str, detail: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, detail)
            except Exception:  # noqa: BLE001
                pass
