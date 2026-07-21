#!/usr/bin/env python3
"""
sma_console.py — single interactive console for an SMA THERMAL
characterization run.

Replaces the multi-entry-point recorder (sma_recorder.py + run_experiment.py +
the rigid OPEN -> SHORT -> RAW phase flow). One window controls the Zaber
stage and the SMA from a continuously-logging session, with live plots, a
startup full-system check, a mid-run staleness monitor, and an always-available
DISARM. (This thermal module has NO LCR — no connection, recording, or control.)

Two front ends over the SAME recording_core.RecordingCore:

    python sma_console.py                # GUI console (layout A)
    python sma_console.py --headless     # no-GUI scripted run (Ctrl+C to stop)
    python sma_console.py --config x.yaml --session-id flexinol_run01

PLAN §5 decisions baked in:
  * --headless mode is kept for scripted runs (no Qt needed).
  * entry point is sma_console.py.
  * the Zaber stage is AUXILIARY — a failure WARNS but never stops the SMA
    run; only the H7 sensor hub is critical.

Output (per session): data/console_<timestamp>/
    h7.csv  stage.csv  status.csv  events.csv  meta.json  session.log

Exit codes: 0 ok / 1 user-aborted / 2 system error (critical health, no streams).

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from lib_config import AppConfig
from lib_recording_core import (RecordingCore, StreamVerdict, make_console_paths,
                            HEALTH_TIMEOUT_S,
                            STREAM_H7, STREAM_STAGE, STREAM_CAMERA)
from lib_workers import (H7Worker, ZaberWorker, CameraWorker)
from lib_camera import make_camera


_THIS_DIR = Path(__file__).resolve().parent
QUEUE_MAXSIZE = 10_000

# Live-plot rolling window (seconds) and a hard cap on retained points so a
# long session can't grow the buffers without bound.
PLOT_WINDOW_S = 30.0
PLOT_MAX_POINTS = 6000
GUI_TICK_MS = 50          # ~20 Hz heartbeat (drain + plots + health)

# Hardware ceilings enforced on operator input fields.
SMA_MAX_V = 5.2           # LDO can't drive the SMA above this; clamp any input.
STAGE_MAX_MM = 300.0      # X-LSQ300A travel; no position/limit may exceed this.


# ===========================================================================
# Shared setup (used by both front ends)
# ===========================================================================
def _setup_logging(log_path: Path, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # encoding=utf-8 — Windows defaults to cp1252 and chokes on the box/arrow
    # glyphs the workers log otherwise (UnicodeEncodeError).
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)-18s  %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [fh]


def _resolve_output_dir(cfg_output_dir: str) -> Path:
    p = Path(cfg_output_dir)
    return p if p.is_absolute() else _THIS_DIR / p


def build_core(cfg: AppConfig, paths, stop_event: threading.Event,
               on_event=None, on_log=None):
    """Build the enabled workers + queues and the RecordingCore that owns them.

    Returns (core, workers) where workers is the list of started-able threads.
    A disabled stream is wired as (None, None) and skipped everywhere. Raises
    SystemExit(2) if nothing is enabled.
    """
    # LCR is intentionally NOT part of this thermal module — no worker, no
    # connection, no recording, no control. lcr_worker/lcr_q stay None so every
    # (guarded) LCR path in RecordingCore is inert.
    lcr_worker = lcr_q = None
    h7_worker = h7_q = None
    stage_worker = stage_q = None
    status_q = None

    if cfg.h7.enabled:
        h7_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        status_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        h7_worker = H7Worker(cfg.h7, h7_q, stop_event, status_queue=status_q)
    else:
        logging.info("H7 stream disabled in config")

    if cfg.stage.enabled:
        stage_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        stage_worker = ZaberWorker(cfg.stage, stage_q, stop_event)
    else:
        logging.info("Stage stream disabled in config")

    camera_worker = None
    if cfg.camera.enabled:
        camera_worker = make_camera(cfg.camera, stop_event)
    else:
        logging.info("Camera stream disabled in config")

    if not (h7_worker or stage_worker):
        logging.error("No streams enabled — nothing to record.")
        print("ERROR: no streams enabled in config (h7/stage both disabled).",
              file=sys.stderr)
        raise SystemExit(2)

    core = RecordingCore(
        cfg=cfg, paths=paths,
        lcr_worker=lcr_worker, h7_worker=h7_worker, stage_worker=stage_worker,
        lcr_queue=lcr_q, h7_queue=h7_q, stage_queue=stage_q,
        status_queue=status_q, camera_worker=camera_worker, stop_event=stop_event,
        on_event=on_event, on_log=on_log)
    if camera_worker is not None:
        core.bind_camera(camera_worker)     # wire recording gate / laser / dir
    workers = [w for w in (lcr_worker, h7_worker, stage_worker, camera_worker)
               if w is not None]
    return core, workers


def _wait_h7_ready(core: RecordingCore, timeout_s: float = 8.0) -> None:
    """Block briefly until the H7 reader has opened so the first SMA command
    isn't dropped. Harmless if the H7 is disabled."""
    w = core.h7_worker
    if w is None:
        return
    ready = getattr(w, "ready", None)
    if ready is not None:
        ready.wait(timeout=timeout_s)


# ===========================================================================
# Headless front end (scripted runs — no Qt)
# ===========================================================================
def _install_sigint_handler(stop_event: threading.Event) -> None:
    def _handler(_sig, _frm):
        if not stop_event.is_set():
            stop_event.set()
        else:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGINT, _handler)


def run_headless(cfg: AppConfig, paths, stop_event: threading.Event) -> int:
    core, workers = build_core(cfg, paths, stop_event)
    _install_sigint_handler(stop_event)

    print("=" * 60)
    print(f"  SMA console (headless): {paths.session_id}")
    print(f"  Output: {paths.session_dir}")
    print("=" * 60, flush=True)

    for w in workers:
        w.start()

    core.open_outputs()
    _wait_h7_ready(core)

    verdicts, critical_ok = core.startup_check(HEALTH_TIMEOUT_S)
    for v in verdicts:
        if not v.needed:
            continue
        mark = "OK " if v.ok else "BAD"
        aux = " (auxiliary)" if v.auxiliary else ""
        print(f"  [{mark}] {v.label:<5}{aux}: {v.reason}", flush=True)
    if not critical_ok:
        print("  CRITICAL health failure — aborting (see above).", flush=True)
        core.finalize("abort_health")
        return 2

    core.discard_backlog()
    core.reset_health_baseline()
    # Headless is a scripted run — start recording immediately (the GUI gates
    # this behind a manual "Start REC" button instead).
    core.start_recording()

    sma_drive = cfg.sma.enabled
    if sma_drive:
        if core.start_cycle_from_config():
            print(f"  SMA armed + cycle started: {cfg.sma.cycle_command()}",
                  flush=True)
        else:
            print("  WARNING: could not start SMA cycle (H7 reader).", flush=True)

    print("  Recording — press Ctrl+C to stop.", flush=True)
    outcome = "complete"
    try:
        while not stop_event.is_set():
            core.drain_tick()
            core.maybe_ping()
            ok, reason = core.check_health()
            if not ok:
                logging.error("HEALTH ABORT: %s", reason)
                print(f"  HEALTH ABORT: {reason}", flush=True)
                if sma_drive:
                    core.disarm()
                outcome = "abort_health"
                break
            time.sleep(GUI_TICK_MS / 1000.0)
    except KeyboardInterrupt:
        outcome = "abort_user"
    finally:
        if sma_drive:
            core.stop_actuation()
        core.finalize(outcome)

    print(f"  Session {paths.session_id} — {outcome} "
          f"({core.n_h7} H7 / {core.n_stage} stage)",
          flush=True)
    return {"complete": 0, "abort_user": 1}.get(outcome, 2)


# ===========================================================================
# GUI front end (layout A: left rail + right stacked plots + status bar + log)
# ===========================================================================
def run_gui(cfg: AppConfig, paths, stop_event: threading.Event,
            _build_only: bool = False) -> int:
    """GUI front end. `_build_only` (test hook) constructs and shows the window
    without starting workers or entering the event loop, so the UI-construction
    code can be verified off the bench."""
    # Lazy Qt imports so --headless never needs a GUI stack. pyqtgraph.Qt
    # abstracts the binding (PyQt5/6, PySide2/6) — whichever is installed.
    import numpy as np
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

    # Fast-FPS options offered per resolution (from the measured caps — see
    # README). Resolution dropdown order matches this dict.
    _CAM_MODES = {
        (640, 360): [30, 60, 120],
        (1280, 720): [30, 60, 120],
        (1920, 1080): [15, 30, 60],
        (2560, 1440): [15, 30, 45],
        (3264, 2448): [10, 15, 20],
        (3840, 2160): [10, 15, 20],
        (4000, 3000): [5, 10, 15],
    }

    # ---- small UI helpers --------------------------------------------------
    def _dot(ok: Optional[bool]) -> str:
        # `border:none` keeps the clickable indicator buttons reading as plain
        # status dots; harmless on the plain-label REC indicator.
        if ok is None:
            return "border:none; color:#888"          # not enabled
        return ("border:none; color:#2ecc71" if ok
                else "border:none; color:#e74c3c")

    def _labeled_field(default: str, width: int = 70):
        e = QtWidgets.QLineEdit(str(default))
        e.setFixedWidth(width)
        return e

    def _normalize_field(edit, decimals: int,
                         lo: "float | None" = None,
                         hi: "float | None" = None):
        """Parse, clamp to [lo, hi], round to `decimals`, and write the tidy
        value back into the field. Returns the float value, or None if the
        field isn't a number (left untouched so the user can fix it).

        This is what enforces the hardware ceilings (voltage ≤ SMA_MAX_V,
        stage ≤ STAGE_MAX_MM) and the "100 -> 100.00" decimal formatting.
        """
        txt = edit.text().strip()
        try:
            val = float(txt)
        except ValueError:
            return None
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        edit.setText(f"{val:.{decimals}f}")
        return val

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"SMA Thermal Console — {paths.session_id}")
            self.core: Optional[RecordingCore] = None
            self.workers: list = []
            self._started = False
            self._closing = False
            self._buf = {k: deque(maxlen=PLOT_MAX_POINTS)
                         for k in ("laser", "load", "sma_v", "sma_i", "sma_r")}
            self._t0 = time.monotonic()
            # Live plots show the raw laser / load-cell voltages straight off
            # the ADC — no sensitivity applied — so what's on screen matches the
            # (already-raw) CSVs 1:1. Set these flags True to re-enable the
            # displacement_mm / force_N display conversions below.
            lc = cfg.calibration.laser
            fc = cfg.calibration.load_cell
            self._laser_k = lc.k_mV_per_um
            self._laser_v0 = lc.V0_mV
            self._load_scale = fc.scale_N_per_V
            self._load_off = fc.offset_V
            self._laser_cal = False
            self._load_cal = False
            self._build_ui()

        def _laser_mm(self, v_volts: float) -> float:
            """Raw laser volts → displacement in mm (assumes _laser_cal)."""
            return (v_volts * 1000.0 - self._laser_v0) / self._laser_k / 1000.0

        def _load_N(self, v_volts: float) -> float:
            """Raw load-cell volts → force in N (assumes _load_cal)."""
            return self._load_scale * (v_volts - self._load_off)

        # --- layout ------------------------------------------------------
        def _build_ui(self):
            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            outer = QtWidgets.QVBoxLayout(central)

            outer.addWidget(self._build_status_bar())

            split = QtWidgets.QHBoxLayout()
            outer.addLayout(split, stretch=1)
            split.addWidget(self._build_left_rail(), stretch=0)
            split.addWidget(self._build_plots(), stretch=1)

            outer.addWidget(self._build_event_log(), stretch=0)

        def _build_status_bar(self):
            bar = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(bar)
            h.setContentsMargins(6, 2, 6, 2)
            # The stream indicators double as RECONNECT buttons: click a red
            # (offline/failed) stream to rebuild its worker and retry the
            # hardware connection. Flat buttons so they read like status dots.
            self.lbl_h7 = QtWidgets.QPushButton("● H7")
            self.lbl_stage = QtWidgets.QPushButton("● stage")
            self.lbl_cam = QtWidgets.QPushButton("● cam")
            self.lbl_h7.clicked.connect(lambda: self.on_reconnect(STREAM_H7))
            self.lbl_stage.clicked.connect(lambda: self.on_reconnect(STREAM_STAGE))
            self.lbl_cam.clicked.connect(lambda: self.on_reconnect(STREAM_CAMERA))
            for w in (self.lbl_h7, self.lbl_stage, self.lbl_cam):
                w.setFlat(True)
                w.setCursor(QtCore.Qt.PointingHandCursor)
                w.setToolTip("click to (re)connect this instrument")
                w.setStyleSheet(_dot(None))
                h.addWidget(w)
            h.addStretch(1)
            self.lbl_rec = QtWidgets.QLabel("DATA ●")   # data recording (auto-on)
            self.lbl_rec.setStyleSheet(_dot(None))
            h.addWidget(self.lbl_rec)
            self.btn_rec = QtWidgets.QPushButton("Rec Video")  # camera video only
            self.btn_rec.setToolTip("Record camera VIDEO (data is always recorded)")
            self.btn_rec.setStyleSheet(
                "background:#27ae60; color:white; font-weight:bold; padding:4px 16px;")
            self.btn_rec.clicked.connect(self.on_toggle_record)
            h.addWidget(self.btn_rec)
            h.addStretch(1)
            self.btn_disarm = QtWidgets.QPushButton("DISARM")
            self.btn_disarm.setStyleSheet(
                "background:#c0392b; color:white; font-weight:bold; padding:4px 16px;")
            self.btn_disarm.clicked.connect(self.on_disarm)
            h.addWidget(self.btn_disarm)
            return bar

        def _build_left_rail(self):
            rail = QtWidgets.QWidget()
            rail.setFixedWidth(280)
            v = QtWidgets.QVBoxLayout(rail)

            # --- SMA group ---
            sma_box = QtWidgets.QGroupBox("SMA")
            f = QtWidgets.QFormLayout(sma_box)
            self.e_vhigh = _labeled_field(cfg.sma.v_high)
            self.e_thigh = _labeled_field(cfg.sma.fire_ms)
            self.e_vidle = _labeled_field(cfg.sma.v_low)
            self.e_tidle = _labeled_field(cfg.sma.cool_ms)
            self.e_n = _labeled_field(cfg.sma.n_cycles)
            self.chk_cycle = QtWidgets.QCheckBox("cycle (else single fire, n=1)")
            self.chk_cycle.setChecked(True)
            f.addRow("V_high (V)", self.e_vhigh)
            f.addRow("t_high (ms)", self.e_thigh)
            f.addRow("V_idle (V)", self.e_vidle)
            f.addRow("t_idle (ms)", self.e_tidle)
            f.addRow("n", self.e_n)
            f.addRow(self.chk_cycle)
            row = QtWidgets.QHBoxLayout()
            self.btn_arm = QtWidgets.QPushButton("arm")
            self.btn_start = QtWidgets.QPushButton("start")
            self.btn_stop = QtWidgets.QPushButton("stop")
            self.btn_arm.clicked.connect(self.on_arm)
            self.btn_start.clicked.connect(self.on_start_actuation)
            self.btn_stop.clicked.connect(self.on_stop_actuation)
            for b in (self.btn_arm, self.btn_start, self.btn_stop):
                row.addWidget(b)
            f.addRow(row)
            self._refresh_arm_button()   # initial (disarmed) colour
            # (No separate "measure baseline" button: the first actuation arms
            #  the board for 3 s of idle-hold first — that recorded idle window
            #  is the cold baseline for cycle 1. See on_start_actuation.)
            self.lbl_sma_read = QtWidgets.QLabel("V — / I — / R — / code —")
            f.addRow(self.lbl_sma_read)
            v.addWidget(sma_box)

            # --- Stage group ---
            st_box = QtWidgets.QGroupBox("Stage")
            sf = QtWidgets.QFormLayout(st_box)
            self.lbl_stage_pos = QtWidgets.QLabel("— mm")
            self.e_target = _labeled_field(cfg.stage.zero_mm)
            sf.addRow("position", self.lbl_stage_pos)
            sf.addRow("target (mm)", self.e_target)
            # Manual, operator-initiated motion. The recorder pipeline stays
            # telemetry-only; these buttons just drive the stage directly.
            # Absolute go-to needs a homed stage, so Home is offered too.
            st_row = QtWidgets.QHBoxLayout()
            self.btn_stage_home = QtWidgets.QPushButton("home")
            self.btn_stage_go = QtWidgets.QPushButton("go")
            self.btn_stage_stop = QtWidgets.QPushButton("STOP")
            self.btn_stage_stop.setStyleSheet(
                "background:#c0392b; color:white; font-weight:bold;")
            self.btn_stage_home.clicked.connect(self.on_stage_home)
            self.btn_stage_go.clicked.connect(self.on_stage_move)
            self.btn_stage_stop.clicked.connect(self.on_stage_stop)
            self.e_target.returnPressed.connect(self.on_stage_move)
            st_row.addWidget(self.btn_stage_home)
            st_row.addWidget(self.btn_stage_go)
            st_row.addWidget(self.btn_stage_stop)
            sf.addRow(st_row)
            # Configurable soft-limit window (clamps go-to; also the health
            # "workflow window"). Prefilled from config; "set" applies at runtime.
            lo0, hi0 = cfg.stage.limits_tuple()
            self.e_lim_lo = _labeled_field(lo0, width=52)
            self.e_lim_hi = _labeled_field(hi0, width=52)
            lim_row = QtWidgets.QHBoxLayout()
            lim_row.setSpacing(4)
            lim_row.addWidget(QtWidgets.QLabel("min"))
            lim_row.addWidget(self.e_lim_lo)
            lim_row.addSpacing(8)
            lim_row.addWidget(QtWidgets.QLabel("max"))
            lim_row.addWidget(self.e_lim_hi)
            lim_row.addStretch(1)
            self.btn_stage_setlim = QtWidgets.QPushButton("set")
            self.btn_stage_setlim.clicked.connect(self.on_stage_set_limits)
            lim_row.addWidget(self.btn_stage_setlim)
            sf.addRow("limits (mm)", lim_row)
            st_box.setEnabled(cfg.stage.enabled)
            v.addWidget(st_box)

            # --- Camera group ---
            # resolution + fast-fps are dropdowns (apply on reconnect, locked
            # while recording); transient + heartbeat are live fields. A live
            # preview sits below. (LCR group removed — no LCR in this module.)
            cam_box = QtWidgets.QGroupBox("Camera")
            camf = QtWidgets.QFormLayout(cam_box)
            self._cam_loading = True
            self.cmb_cam_res = QtWidgets.QComboBox()
            for (cw, ch) in _CAM_MODES:
                self.cmb_cam_res.addItem(f"{cw}×{ch}", (cw, ch))
            cur_res = tuple(cfg.camera.res_tuple())
            ri = self.cmb_cam_res.findData(cur_res)
            if ri >= 0:
                self.cmb_cam_res.setCurrentIndex(ri)
            self.cmb_cam_fps = QtWidgets.QComboBox()
            self._repopulate_fps_combo(cur_res, cfg.camera.fps_fast)
            self.e_cam_transient = _labeled_field(cfg.camera.transient_guarantee_s)
            self.e_cam_hb = _labeled_field(cfg.camera.fps_heartbeat)
            self.cmb_cam_res.currentIndexChanged.connect(self.on_cam_res_changed)
            self.cmb_cam_fps.currentIndexChanged.connect(self.on_cam_fps_changed)
            self.e_cam_transient.editingFinished.connect(self.on_cam_transient)
            self.e_cam_hb.editingFinished.connect(self.on_cam_heartbeat)
            camf.addRow("resolution", self.cmb_cam_res)
            camf.addRow("fast fps", self.cmb_cam_fps)
            camf.addRow("transient (s)", self.e_cam_transient)
            camf.addRow("heartbeat fps", self.e_cam_hb)
            self.cam_preview = QtWidgets.QLabel("no preview")
            self.cam_preview.setFixedSize(256, 144)
            self.cam_preview.setAlignment(QtCore.Qt.AlignCenter)
            self.cam_preview.setStyleSheet(
                "background:#111; color:#888; border:1px solid #333;")
            # Click the thumbnail to pop out a large live view (for focusing).
            self.cam_preview.setCursor(QtCore.Qt.PointingHandCursor)
            self.cam_preview.setToolTip("Click to open a large live view (focus)")
            self.cam_preview.mousePressEvent = self.on_preview_click
            self._preview_popup = None
            self._preview_popup_label = None
            camf.addRow(self.cam_preview)
            cam_box.setEnabled(cfg.camera.enabled)
            v.addWidget(cam_box)
            self._cam_loading = False

            # Per-field normalization specs: (widget, decimals, min, max).
            # Voltages are clamped to the LDO ceiling, stage positions to the
            # travel; time (ms) and cycle count are integers (0 decimals). Each
            # field self-normalizes when focus leaves it (editingFinished), so a
            # typed "100" becomes "100.00" and out-of-range values snap to the
            # ceiling immediately.
            self._field_specs = [
                (self.e_vhigh, 2, 0.0, SMA_MAX_V),
                (self.e_vidle, 2, 0.0, SMA_MAX_V),
                (self.e_thigh, 0, 0.0, None),
                (self.e_tidle, 0, 0.0, None),
                (self.e_n,     0, 0.0, None),
                (self.e_target, 2, 0.0, STAGE_MAX_MM),
                (self.e_lim_lo, 2, 0.0, STAGE_MAX_MM),
                (self.e_lim_hi, 2, 0.0, STAGE_MAX_MM),
            ]
            for widget, decimals, flo, fhi in self._field_specs:
                widget.editingFinished.connect(
                    lambda w=widget, d=decimals, a=flo, b=fhi:
                    _normalize_field(w, d, a, b))
                _normalize_field(widget, decimals, flo, fhi)  # tidy initial value

            v.addStretch(1)
            return rail

        def _build_plots(self):
            # Laser (displacement) and load (force) are different physical
            # quantities on very different scales — sharing one y-axis squashes
            # one flat, so each gets its OWN auto-ranging plot. SMA V/I and the
            # SMA resistance R also live on separate rows (R spans a very
            # different range than V/I). All rows share the time axis (x-linked)
            # so they pan/zoom together.
            #
            # Laser/load y-axes show physical units (mm / N) when calibration is
            # available in config; otherwise they fall back to raw volts. The
            # conversion is DISPLAY-ONLY — the CSVs stay raw. (See _cal_* below.)
            col = pg.GraphicsLayoutWidget()
            self.p_laser = col.addPlot(row=0, col=0, title="laser (displacement)")
            col.nextRow()
            self.p_load = col.addPlot(row=1, col=0, title="load cell (force)")
            col.nextRow()
            self.p_sma = col.addPlot(row=2, col=0, title="SMA  V / I")
            col.nextRow()
            self.p_smar = col.addPlot(row=3, col=0, title="SMA resistance (R)")
            for p in (self.p_laser, self.p_load, self.p_sma, self.p_smar):
                p.showGrid(x=True, y=True, alpha=0.3)
                p.setLabel("bottom", "t", units="s")
                p.addLegend(offset=(10, 5))
                if p is not self.p_laser:
                    p.setXLink(self.p_laser)
            if self._laser_cal:
                self.p_laser.setLabel("left", "displacement", units="mm")
            else:
                self.p_laser.setLabel("left", "laser", units="V")
            if self._load_cal:
                self.p_load.setLabel("left", "force", units="N")
            else:
                self.p_load.setLabel("left", "load", units="V")
            # SMA V and I share one row but have different units (V vs A), so the
            # axis carries both and the legend names spell out each unit.
            self.p_sma.setLabel("left", "V / A")
            self.p_smar.setLabel("left", "R", units="Ω")
            self.c_laser = self.p_laser.plot(pen="y", name="laser")
            self.c_load = self.p_load.plot(pen="c", name="load")
            self.c_smav = self.p_sma.plot(pen="r", name="V [V]")
            self.c_smai = self.p_sma.plot(pen="g", name="I [A]")
            self.c_smar = self.p_smar.plot(pen="m", name="R [Ω]")
            return col

        def _build_event_log(self):
            self.log_view = QtWidgets.QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumBlockCount(500)
            self.log_view.setFixedHeight(120)
            return self.log_view

        # --- lifecycle ---------------------------------------------------
        def begin(self):
            """Start workers, open outputs, run the startup check, then arm the
            heartbeat timer. Runs once, after the window is shown."""
            self.core, self.workers = build_core(
                cfg, paths, stop_event,
                on_event=self._on_event_threadsafe,
                on_log=self._on_log_threadsafe)
            for w in self.workers:
                w.start()
            self.core.open_outputs()
            _wait_h7_ready(self.core)
            self.append_log("Running startup full-system check...")
            verdicts, critical_ok = self.core.startup_check(HEALTH_TIMEOUT_S)
            self._apply_verdicts(verdicts)
            if not critical_ok:
                self.append_log("CRITICAL health failure — recording anyway is "
                                "unsafe; fix the H7 and restart.")
                QtWidgets.QMessageBox.critical(
                    self, "Health check failed",
                    "The H7 sensor hub failed the startup check.\n"
                    "See the event log. Disarm is still available.")
            self.core.discard_backlog()
            self.core.reset_health_baseline()
            self._started = True
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.on_tick)
            self.timer.start(GUI_TICK_MS)
            # DATA recording auto-starts on launch — h7/stage/status stream to
            # disk from the moment the session opens. The REC button is now for
            # the CAMERA video only. Skip only on a critical health failure.
            if critical_ok and self.core.start_recording():
                self.append_log("Data recording STARTED automatically.")
            else:
                self.append_log("Data recording NOT started (critical health "
                                "failure) — fix the H7 and restart.")
            self.append_log(f"Output: {paths.session_dir}")
            self.append_log("REC button records CAMERA video only.")

        def _apply_verdicts(self, verdicts: "list[StreamVerdict]"):
            by = {v.label: v for v in verdicts}
            self.lbl_h7.setStyleSheet(_dot(by["H7"].ok if by["H7"].needed else None))
            self.lbl_stage.setStyleSheet(
                _dot(by["stage"].ok if by["stage"].needed else None))
            for v in verdicts:
                if v.needed:
                    self.append_log(
                        f"[{'OK' if v.ok else 'BAD'}] {v.label}: {v.reason}")

        # --- the 20 Hz heartbeat ----------------------------------------
        def on_tick(self):
            if self.core is None:
                return
            batch = self.core.drain_tick()
            self.core.maybe_ping()
            self._ingest(batch)
            self._refresh_readouts()
            self._refresh_plots()
            self._refresh_preview()
            self._refresh_indicators()
            ok, reason = self.core.check_health()
            if not ok:
                self.append_log(f"HEALTH ABORT: {reason} — auto-disarming.")
                self.core.disarm()

        def _refresh_indicators(self):
            """Live-update the per-stream dots (so a reconnect turns a red dot
            green) and the REC indicator from the recording state."""
            self.lbl_h7.setStyleSheet(_dot(self.core.stream_status(STREAM_H7)))
            self.lbl_stage.setStyleSheet(
                _dot(self.core.stream_status(STREAM_STAGE)))
            self.lbl_cam.setStyleSheet(
                _dot(self.core.stream_status(STREAM_CAMERA)))
            # Data recording (auto-on). Green = data streaming to disk. The
            # camera video is a separate toggle (the "Rec Video" button).
            if self.core.recording:
                self.lbl_rec.setText("DATA ●")
                self.lbl_rec.setStyleSheet(_dot(True))
            else:
                self.lbl_rec.setText("DATA ○")
                self.lbl_rec.setStyleSheet(_dot(None))
            self._refresh_arm_button()

        def _refresh_arm_button(self):
            """Colour the arm button by the live SMA arm state: amber "● ARMED"
            when the MOSFET is closed (coil current can flow), green "arm" when
            disarmed (the safe, zero-current state). The red DISARM button is
            always the master cutoff."""
            if not hasattr(self, "btn_arm"):
                return
            armed = bool(self.core and self.core.armed)
            if armed:
                self.btn_arm.setText("● ARMED")
                self.btn_arm.setStyleSheet(
                    "background:#e67e22; color:white; font-weight:bold;")
                self.btn_arm.setToolTip(
                    "SMA ARMED — coil current can flow. Use DISARM to cut off.")
            else:
                self.btn_arm.setText("arm")
                self.btn_arm.setStyleSheet(
                    "background:#2e7d32; color:white; font-weight:bold;")
                self.btn_arm.setToolTip("SMA disarmed (safe). Click to arm.")

        def _ingest(self, batch):
            now = time.monotonic() - self._t0
            for s in batch.h7:
                ch = s.channel
                if ch == "laser":
                    # Store display units: mm when calibrated, else raw V.
                    val = self._laser_mm(s.value) if self._laser_cal else s.value
                    self._buf["laser"].append((now, val))
                elif ch == "load":
                    val = self._load_N(s.value) if self._load_cal else s.value
                    self._buf["load"].append((now, val))
                elif ch == "sma_v":
                    self._buf["sma_v"].append((now, s.value))
                elif ch == "sma_i":
                    self._buf["sma_i"].append((now, s.value))
                elif ch == "sma_r":
                    self._buf["sma_r"].append((now, s.value))
            if batch.stage:
                self._last_stage = batch.stage[-1].position_mm

        def _refresh_readouts(self):
            def last(key):
                return self._buf[key][-1][1] if self._buf[key] else None
            v, i, r = last("sma_v"), last("sma_i"), last("sma_r")
            fmt = lambda x, u: f"{x:.4g}{u}" if x is not None else "—"
            self.lbl_sma_read.setText(
                f"V {fmt(v,'V')} / I {fmt(i,'A')} / R {fmt(r,'Ω')}")
            pos = getattr(self, "_last_stage", None)
            if pos is not None:
                self.lbl_stage_pos.setText(f"{pos:.3f} mm")

        def _refresh_plots(self):
            tmin = (time.monotonic() - self._t0) - PLOT_WINDOW_S

            def xy(key):
                pts = [(t, val) for (t, val) in self._buf[key] if t >= tmin]
                if not pts:
                    return [], []
                xs, ys = zip(*pts)
                return list(xs), list(ys)

            self.c_laser.setData(*xy("laser"))
            self.c_load.setData(*xy("load"))
            self.c_smav.setData(*xy("sma_v"))
            self.c_smai.setData(*xy("sma_i"))
            self.c_smar.setData(*xy("sma_r"))

        # --- command handlers -------------------------------------------
        def _sma_params(self):
            # Normalize first so voltages are clamped to SMA_MAX_V and the
            # fields show tidy values before we act on them.
            vh = _normalize_field(self.e_vhigh, 2, 0.0, SMA_MAX_V)
            vi = _normalize_field(self.e_vidle, 2, 0.0, SMA_MAX_V)
            th = _normalize_field(self.e_thigh, 0, 0.0, None)
            ti = _normalize_field(self.e_tidle, 0, 0.0, None)
            n = _normalize_field(self.e_n, 0, 0.0, None)
            if None in (vh, vi, th, ti, n):
                QtWidgets.QMessageBox.warning(
                    self, "Bad input", "SMA fields must be numbers.")
                return None
            return (vh, vi, int(th), int(ti), int(n))

        def on_arm(self):
            if self.core:
                self.core.arm()
                # Hold at the configured idle level (0.5 V). The firmware streams
                # V/I/R while armed+idle, so the readout populates from the hold
                # itself — no drive/heat needed.
                self.core.set_idle(cfg.sma.v_low)
                self._refresh_arm_button()

        def on_disarm(self):
            if self.core:
                self.core.disarm()
                self._refresh_arm_button()

        def on_toggle_record(self):
            # DATA recording is auto-on; this button toggles CAMERA VIDEO only.
            if not self.core:
                return
            if self.core.camera_recording:
                self.core.stop_camera_recording()
                self.btn_rec.setText("Rec Video")
                self.btn_rec.setStyleSheet(
                    "background:#27ae60; color:white; font-weight:bold; "
                    "padding:4px 16px;")
                self.append_log("Camera video recording stopped.")
            else:
                if self.core.start_camera_recording():
                    self.btn_rec.setText("Stop Video")
                    self.btn_rec.setStyleSheet(
                        "background:#7f8c8d; color:white; font-weight:bold; "
                        "padding:4px 16px;")
                    self.append_log("Camera video recording started.")
                else:
                    self.append_log("Camera video: no session dir / camera off.")
            # Resolution/fps reopen the camera, so lock them while it records.
            locked = self.core.camera_recording
            if hasattr(self, "cmb_cam_res"):
                self.cmb_cam_res.setEnabled(not locked)
                self.cmb_cam_fps.setEnabled(not locked)
            self._refresh_indicators()

        def on_reconnect(self, label: str):
            """Click handler for the H7/LCR/stage status dots — rebuild a
            dead/offline worker and retry the hardware connection."""
            if not self.core:
                return
            status = self.core.stream_status(label)
            if status is None:
                self.append_log(f"{label} is disabled for this session.")
                return
            if status is True:
                self.append_log(f"{label} is already connected.")
                return
            ok, msg = self.core.restart_worker(label)
            self.append_log(msg if ok else f"{label} reconnect: {msg}")

        def on_start_actuation(self):
            if not self.core:
                return
            p = self._sma_params()
            if p is None:
                return
            v_high, v_idle, t_high, t_idle, n = p

            def _go():
                if self.chk_cycle.isChecked():
                    self.core.cycle(v_high, v_idle, t_high, t_idle, n)
                else:
                    self.core.fire(v_high, t_high)

            # First actuation: arm + hold idle for 3 s so the run has a cold
            # (armed-idle) baseline for cycle 1 — it's recorded because data
            # recording is auto-on — THEN start. If already armed/actuating
            # (re-fire), start immediately.
            if not (self.core.armed or self.core.actuating):
                self.core.arm()
                self.core.set_idle(v_idle)
                self._refresh_arm_button()
                self.core.log_event("baseline", "3s armed-idle baseline before cycle 1")
                self.append_log("Arming 3 s idle baseline before first cycle…")
                QtCore.QTimer.singleShot(3000, _go)
            else:
                _go()

        def on_stop_actuation(self):
            if self.core:
                self.core.stop_actuation()

        def on_stage_home(self):
            if not self.core:
                return
            ok, msg = self.core.stage_home()
            self.append_log(f"stage home: {msg}" if ok
                            else f"stage home FAILED: {msg}")

        def on_stage_move(self):
            if not self.core:
                return
            target = _normalize_field(self.e_target, 2, 0.0, STAGE_MAX_MM)
            if target is None:
                QtWidgets.QMessageBox.warning(
                    self, "Bad input", "Target must be a number (mm).")
                return
            ok, msg = self.core.stage_move(target)
            self.append_log(f"stage move: {msg}" if ok
                            else f"stage move FAILED: {msg}")

        def on_stage_stop(self):
            if not self.core:
                return
            ok, msg = self.core.stage_stop()
            self.append_log(f"stage: {msg}" if ok
                            else f"stage STOP FAILED: {msg}")

        def on_stage_set_limits(self):
            if not self.core:
                return
            lo = _normalize_field(self.e_lim_lo, 2, 0.0, STAGE_MAX_MM)
            hi = _normalize_field(self.e_lim_hi, 2, 0.0, STAGE_MAX_MM)
            if lo is None or hi is None:
                QtWidgets.QMessageBox.warning(
                    self, "Bad input", "Limits must be numbers (mm).")
                return
            ok, msg = self.core.stage_set_limits(lo, hi)
            self.append_log(f"stage {msg}" if ok
                            else f"stage limits FAILED: {msg}")

        # --- camera controls ---------------------------------------------
        def _repopulate_fps_combo(self, res, want_fps):
            """Fill the fast-fps dropdown with the options valid for `res`,
            selecting the closest to want_fps."""
            was = self._cam_loading
            self._cam_loading = True
            self.cmb_cam_fps.clear()
            opts = _CAM_MODES.get(tuple(res), [30])
            for f in opts:
                self.cmb_cam_fps.addItem(str(f), f)
            closest = min(opts, key=lambda f: abs(f - float(want_fps)))
            self.cmb_cam_fps.setCurrentIndex(opts.index(closest))
            self._cam_loading = was

        def _apply_camera_reconnect(self, what: str):
            """Push the current resolution/fps into cfg and restart the camera
            (unless recording). Called from the res/fps dropdown handlers."""
            if self._cam_loading:
                return
            res = self.cmb_cam_res.currentData()
            fps = self.cmb_cam_fps.currentData()
            if res is not None:
                cfg.camera.resolution = [int(res[0]), int(res[1])]
            if fps is not None:
                cfg.camera.fps_fast = float(fps)
            if not self.core:
                return
            ok, msg = self.core.reconnect_camera()
            self.append_log(f"camera {what}: {msg}" if ok
                            else f"camera {what}: {msg}")

        def on_cam_res_changed(self):
            if self._cam_loading:
                return
            res = self.cmb_cam_res.currentData()
            self._repopulate_fps_combo(res, cfg.camera.fps_fast)
            self._apply_camera_reconnect("resolution")

        def on_cam_fps_changed(self):
            self._apply_camera_reconnect("fps")

        def on_cam_transient(self):
            val = _normalize_field(self.e_cam_transient, 1, 0.0, 120.0)
            if val is not None:
                cfg.camera.transient_guarantee_s = val  # live (no reconnect)

        def on_cam_heartbeat(self):
            val = _normalize_field(self.e_cam_hb, 2, 0.05, 30.0)
            if val is not None:
                cfg.camera.fps_heartbeat = val          # live (no reconnect)

        def _refresh_preview(self):
            if self.core is None or self.core.camera_worker is None:
                return
            frame = self.core.camera_worker.latest_preview()
            if frame is None:
                return
            h, w = frame.shape[:2]
            rgb = np.ascontiguousarray(frame[:, :, ::-1])  # BGR->RGB
            img = QtGui.QImage(rgb.data, w, h, 3 * w,
                               QtGui.QImage.Format_RGB888)
            src = QtGui.QPixmap.fromImage(img)
            self.cam_preview.setPixmap(src.scaled(
                self.cam_preview.width(), self.cam_preview.height(),
                QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
            # Mirror the same frame into the pop-out live view if it's open.
            popup = getattr(self, "_preview_popup", None)
            lbl = getattr(self, "_preview_popup_label", None)
            if popup is not None and lbl is not None:
                lbl.setPixmap(src.scaled(
                    lbl.width(), lbl.height(),
                    QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
            self._preview_ref = rgb  # keep the buffer alive for the QImage

        def on_preview_click(self, _event=None):
            """Pop out a large, live camera view for focusing. Clicking again
            (or clicking the thumbnail) just re-focuses the existing window;
            closing it returns to the thumbnail."""
            if getattr(self, "_preview_popup", None) is not None:
                self._preview_popup.raise_()
                self._preview_popup.activateWindow()
                return
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Camera — live view (close to return)")
            dlg.setMinimumSize(640, 360)
            dlg.resize(960, 540)
            lay = QtWidgets.QVBoxLayout(dlg)
            lay.setContentsMargins(0, 0, 0, 0)
            lbl = QtWidgets.QLabel("waiting for frames…")
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            lbl.setStyleSheet("background:#000; color:#888;")
            lay.addWidget(lbl)
            self._preview_popup = dlg
            self._preview_popup_label = lbl
            dlg.finished.connect(self._on_preview_popup_closed)
            dlg.show()
            self._refresh_preview()  # paint immediately, don't wait for a tick

        def _on_preview_popup_closed(self, *_):
            self._preview_popup = None
            self._preview_popup_label = None

        # --- event/log plumbing ------------------------------------------
        # The core invokes these only from inside its own methods, which in
        # GUI mode are ALL called on the main thread (begin / on_tick / button
        # handlers). Workers never touch these — they log via Python logging.
        # So a direct append is safe; no cross-thread marshalling needed.
        def _on_event_threadsafe(self, ev: dict):
            self.append_log(f"[{ev['kind']}] {ev['detail']}")

        def _on_log_threadsafe(self, level: str, msg: str):
            if level in ("warning", "error", "critical"):
                self.append_log(f"[{level}] {msg}")

        def append_log(self, text: str):
            self.log_view.appendPlainText(time.strftime("%H:%M:%S  ") + text)

        # --- shutdown ----------------------------------------------------
        def closeEvent(self, e):
            if not self._closing:
                self._closing = True
                if hasattr(self, "timer"):
                    self.timer.stop()
                if self.core is not None:
                    self.append_log("Closing — stop + disarm + finalize.")
                    self.core.finalize("complete")
            e.accept()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.resize(1100, 720)
    win.show()
    if _build_only:
        win.append_log("(_build_only) window constructed; skipping begin()/loop")
        app.processEvents()
        win.close()
        return 0
    # Defer begin() to the event loop so the window paints before the (blocking)
    # startup check runs.
    QtCore.QTimer.singleShot(0, win.begin)
    return int(app.exec() if hasattr(app, "exec") else app.exec_())


# ===========================================================================
# Entry
# ===========================================================================
def _main() -> None:
    p = argparse.ArgumentParser(
        description="Single-window SMA thermal characterization console.")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"))
    p.add_argument("--session-id", default=None,
                   help="custom session dir name (default: console_<timestamp>)")
    p.add_argument("--headless", action="store_true",
                   help="run without the GUI (scripted; Ctrl+C to stop)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    cfg = AppConfig.from_yaml(Path(args.config))
    output_dir = _resolve_output_dir(cfg.run.output_dir)
    paths = make_console_paths(output_dir, session_id=args.session_id)

    _setup_logging(paths.log_txt, args.verbose)
    logging.info("Session: %s", paths.session_id)
    logging.info("Output:  %s", paths.session_dir)
    logging.info("Config:  %s  headless=%s", args.config, args.headless)

    stop_event = threading.Event()
    if args.headless:
        sys.exit(run_headless(cfg, paths, stop_event))
    else:
        sys.exit(run_gui(cfg, paths, stop_event))


if __name__ == "__main__":
    # Required before any multiprocessing (the optional camera subprocess) so a
    # spawned child re-importing this module doesn't relaunch the GUI.
    import multiprocessing
    multiprocessing.freeze_support()
    _main()
