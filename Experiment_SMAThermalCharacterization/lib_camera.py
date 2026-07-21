"""
camera_process.py — run the camera in its OWN OS process (separate Python
interpreter, separate GIL, its own core) so no amount of camera CPU can stall
the H7 serial reader or the GUI. Purely software: same USB cable/port.

Design: the child process builds the SAME `CameraWorker` used in-thread and
wires its hooks to inter-process queues, so all the tested capture / decode /
adaptive-fps / JPEG-write / reconnect logic is reused verbatim. The main
process holds a `CameraProcessProxy` that presents the exact CameraWorker
surface (start / stop_local / join / is_alive / latest_preview / mark_fast /
error / actual_res …) so `recording_core` and `sma_console` need no changes
beyond choosing the proxy via `make_camera()`.

Control flow (main -> child): a 'state' message (~20 Hz) carries is_recording,
session_dir and the recent laser history; 'mark_fast' / 'stop_local' / 'stop'
are one-shots. Data flow (child -> main): 'preview' frames (~10 Hz), periodic
'status', and forwarded 'event' tuples for events.csv.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
import time
from typing import Callable, List, Optional, Tuple

from lib_config import CameraConfig


# ---------------------------------------------------------------------------
# Child process entry — builds a real CameraWorker and bridges it to IPC.
# ---------------------------------------------------------------------------
def _camera_proc_main(cfg: CameraConfig, cmd_q, out_q, stop_evt) -> None:
    # Imported here so the heavy cv2/workers import happens in the CHILD only.
    from lib_workers import CameraWorker

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  CameraProc  %(message)s")
    log = logging.getLogger("CameraProc")

    # Shared control state updated from the main process via cmd_q.
    state = {"recording": False, "session_dir": None, "laser": []}
    state_lock = threading.Lock()

    worker_stop = threading.Event()
    worker = CameraWorker(cfg, worker_stop)
    worker.laser_provider = lambda: (lambda: state["laser"])()  # snapshot ref
    worker.is_recording = lambda: state["recording"]
    worker.session_dir = lambda: state["session_dir"]
    worker.on_event = lambda k, d: _safe_put(out_q, ("event", k, d))
    worker.start()

    def drain_cmds() -> None:
        while not stop_evt.is_set():
            try:
                msg = cmd_q.get(timeout=0.1)
            except (queue.Empty, EOFError, OSError):
                continue
            kind = msg[0]
            if kind == "state":
                _, rec, sd, laser = msg
                with state_lock:
                    state["recording"] = rec
                    state["session_dir"] = sd
                    state["laser"] = laser
            elif kind == "mark_fast":
                worker.mark_fast()
            elif kind == "stop_local":
                worker_stop.set()
            elif kind == "stop":
                stop_evt.set()
        worker_stop.set()

    def push_out() -> None:
        last = None
        period = 1.0 / max(getattr(cfg, "preview_hz", 10.0), 1.0)
        nxt = 0.0
        while not stop_evt.is_set():
            now = time.monotonic()
            if now >= nxt:
                nxt = now + period
                p = worker.latest_preview()
                if p is not None and p is not last:
                    last = p
                    _safe_put(out_q, ("preview", p))
                _safe_put(out_q, ("status", worker.actual_res, worker.actual_fps,
                                  worker.info,
                                  repr(worker.error) if worker.error else None,
                                  worker.n_written))
            time.sleep(0.02)

    dt = threading.Thread(target=drain_cmds, name="CamCmd", daemon=True)
    pt = threading.Thread(target=push_out, name="CamOut", daemon=True)
    dt.start(); pt.start()

    stop_evt.wait()
    worker_stop.set()
    worker.join(timeout=5.0)
    log.info("camera process exiting (written=%d)", worker.n_written)


def _safe_put(q, item) -> None:
    """Non-blocking put that drops the item if the pipe/queue is full or closed."""
    try:
        q.put_nowait(item)
    except (queue.Full, ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# Main-process proxy — looks like a CameraWorker, drives the child process.
# ---------------------------------------------------------------------------
class CameraProcessProxy:
    """Drop-in stand-in for CameraWorker that runs the camera in a separate
    process. Presents the same attributes/methods the rest of the app uses."""

    PREVIEW_WIDTH = 480     # compat constant (unused here; child owns preview)
    PREVIEW_HZ = 15.0

    def __init__(self, cfg: CameraConfig, stop_event: threading.Event):
        self.cfg = cfg
        self.stop_event = stop_event
        # Hooks (bound by RecordingCore.bind_camera) — stay in THIS process.
        self.laser_provider: Callable[[], List[Tuple[float, float]]] = lambda: []
        self.is_recording: Callable[[], bool] = lambda: False
        self.session_dir: Callable[[], Optional[object]] = lambda: None
        self.on_event: Optional[Callable[[str, str], None]] = None

        self.name = "CameraProcess"      # so Thread-style callers (.name) work
        self.error: Optional[BaseException] = None
        self.info: Optional[str] = None
        self.actual_res: Optional[Tuple[int, int]] = None
        self.actual_fps: Optional[float] = None
        self.n_written = 0

        self._preview = None
        self._preview_lock = threading.Lock()

        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._proc_stop = self._ctx.Event()
        self._cmd_q = self._ctx.Queue(maxsize=16)
        self._out_q = self._ctx.Queue(maxsize=8)
        self._fwd = None
        self._local_stop = threading.Event()
        self.logger = logging.getLogger("CameraProxy")

    # -- CameraWorker-compatible surface ---------------------------------
    def start(self) -> None:
        self._proc = self._ctx.Process(
            target=_camera_proc_main, name="CameraProcess", daemon=True,
            args=(self.cfg, self._cmd_q, self._out_q, self._proc_stop))
        self._proc.start()
        self._fwd = threading.Thread(target=self._forward, name="CamProxyFwd",
                                     daemon=True)
        self._fwd.start()
        self.logger.info("camera process started (pid=%s)",
                         self._proc.pid if self._proc else "?")

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def latest_preview(self):
        with self._preview_lock:
            return self._preview

    def mark_fast(self) -> None:
        self._send(("mark_fast",))

    def stop_local(self) -> None:
        self._local_stop.set()
        self._send(("stop_local",))

    def join(self, timeout: Optional[float] = None) -> None:
        self._local_stop.set()
        self._proc_stop.set()
        self._send(("stop",))
        if self._fwd is not None:
            self._fwd.join(timeout=timeout)
        if self._proc is not None:
            self._proc.join(timeout=timeout)
            if self._proc.is_alive():
                self.logger.warning("camera process did not exit; terminating")
                self._proc.terminate()
                self._proc.join(timeout=2.0)   # let SIGTERM land so is_alive()→False
        # Release the mp.Queue feeder threads so the PARENT (console) exits
        # cleanly — a terminated child can otherwise leave the parent hung on
        # queue flush, which was blocking finalize / meta.json.
        for q in (self._cmd_q, self._out_q):
            try:
                q.close()
                q.cancel_join_thread()
            except Exception:  # noqa: BLE001
                pass

    # -- internal --------------------------------------------------------
    def _send(self, msg) -> None:
        try:
            self._cmd_q.put_nowait(msg)
        except Exception:  # noqa: BLE001
            pass

    def _forward(self) -> None:
        """Main-side bridge: push control state to the child (~20 Hz) and drain
        preview/status/event messages from it. Runs as a light daemon thread."""
        last_state = 0.0
        while not (self.stop_event.is_set() or self._local_stop.is_set()):
            now = time.monotonic()
            if now - last_state >= 0.05:
                last_state = now
                self._send(("state", self._safe_recording(),
                            self._safe_session_dir(), self._safe_laser()))
            drained = 0
            while drained < 64:
                try:
                    msg = self._out_q.get_nowait()
                except (queue.Empty, OSError):
                    break
                self._handle_out(msg)
                drained += 1
            time.sleep(0.02)
        self._proc_stop.set()
        self._send(("stop",))

    def _handle_out(self, msg) -> None:
        kind = msg[0]
        if kind == "preview":
            with self._preview_lock:
                self._preview = msg[1]
        elif kind == "status":
            _, res, fps, info, err, nwritten = msg
            self.actual_res = res
            self.actual_fps = fps
            self.info = info
            self.error = RuntimeError(err) if err else None
            self.n_written = nwritten
        elif kind == "event" and self.on_event is not None:
            try:
                self.on_event(msg[1], msg[2])
            except Exception:  # noqa: BLE001
                pass

    def _safe_recording(self) -> bool:
        try:
            return bool(self.is_recording())
        except Exception:  # noqa: BLE001
            return False

    def _safe_session_dir(self):
        try:
            sd = self.session_dir()
            return str(sd) if sd is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _safe_laser(self):
        try:
            return list(self.laser_provider())
        except Exception:  # noqa: BLE001
            return []


# ---------------------------------------------------------------------------
# Factory — pick thread or process camera per config.
# ---------------------------------------------------------------------------
def make_camera(cfg: CameraConfig, stop_event: threading.Event):
    """Return a CameraProcessProxy if cfg.use_subprocess else a threaded
    CameraWorker. Both expose the same interface to the rest of the app."""
    if getattr(cfg, "use_subprocess", False):
        return CameraProcessProxy(cfg, stop_event)
    from lib_workers import CameraWorker
    return CameraWorker(cfg, stop_event)
