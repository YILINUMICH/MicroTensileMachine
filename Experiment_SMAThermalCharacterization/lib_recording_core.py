"""
lib_recording_core.py — UI-agnostic recording engine for the SMA console.

Extracted from the former session.py (WI-1 of PLAN_sma_console.md). RecordingCore owns
everything that is NOT a UI:

  * the single set of CONTINUOUS CSV writers (h7.csv / lcr.csv / stage.csv /
    status.csv) — there are no OPEN/SHORT/RAW phase files anymore;
  * events.csv — the command + reference + warn markers that the analyzer
    uses to segment the continuous log into actuation windows;
  * SMA control (arm / disarm / idle / cycle / fire / stop) routed through
    h7_commands so the host can't drift from the firmware dispatcher;
  * the startup full-system check (identity + streaming + sane values);
  * the mid-run per-instrument staleness monitor;
  * meta.json.

It is free of Qt and operator_io: it takes optional `on_event` / `on_log`
callbacks and RETURNS values, so both the GUI console and the --headless
runner in sma_console.py can drive it the same way.

Auxiliary vs critical streams (PLAN §5 decision): the H7 sensor hub is
CRITICAL (it carries laser/load + the SMA stream). The LCR and the Zaber
stage are AUXILIARY — a failure or stall WARNS but never aborts the SMA run.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import math
import platform
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from collections import deque

import lib_h7_commands as h7
from lib_config import AppConfig
from lib_camera import make_camera
from lib_workers import (CameraWorker, H7Sample, H7Worker, LcrSample, LcrWorker,
                     StageSample, StatusSample, ZaberWorker)


# ---------------------------------------------------------------------------
# Tunables (mirrors session.py so behaviour is unchanged across front ends)
# ---------------------------------------------------------------------------
HEALTH_TIMEOUT_S = 10.0
HEALTH_MIN_LCR = 5
HEALTH_MIN_H7 = 20
HEALTH_MIN_STAGE = 3
HEALTH_WARN_STALE_S = 1.0     # mid-run: warn if a live instrument goes silent this long
HEALTH_ABORT_STALE_S = 3.0    # mid-run: a CRITICAL stream silent this long -> abort
WORKER_JOIN_TIMEOUT_S = 5.0

# Stream labels (used as keys everywhere). H7 is critical; the rest auxiliary.
STREAM_H7 = "H7"
STREAM_LCR = "LCR"
STREAM_STAGE = "stage"
STREAM_CAMERA = "camera"
AUXILIARY_STREAMS = frozenset({STREAM_LCR, STREAM_STAGE, STREAM_CAMERA})


# ---------------------------------------------------------------------------
# Paths — one continuous session directory, no per-phase files
# ---------------------------------------------------------------------------
@dataclass
class ConsolePaths:
    session_dir: Path
    session_id: str
    meta_json: Path
    log_txt: Path
    lcr_csv: Path
    h7_csv: Path
    stage_csv: Path
    status_csv: Path
    events_csv: Path


def make_console_paths(output_dir: Path,
                       session_id: Optional[str] = None) -> ConsolePaths:
    if session_id is None:
        session_id = "console_" + time.strftime("%Y%m%d_%H%M%S")
    session_dir = output_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return ConsolePaths(
        session_dir=session_dir,
        session_id=session_id,
        meta_json=session_dir / "meta.json",
        log_txt=session_dir / "session.log",
        lcr_csv=session_dir / "lcr.csv",
        h7_csv=session_dir / "h7.csv",
        stage_csv=session_dir / "stage.csv",
        status_csv=session_dir / "status.csv",
        events_csv=session_dir / "events.csv",
    )


# ---------------------------------------------------------------------------
# What a single drain tick hands back to the caller (for plots / readouts)
# ---------------------------------------------------------------------------
@dataclass
class DrainBatch:
    lcr: list = field(default_factory=list)
    h7: list = field(default_factory=list)
    stage: list = field(default_factory=list)
    status: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.lcr) + len(self.h7) + len(self.stage) + len(self.status)


# ---------------------------------------------------------------------------
# Startup verdict
# ---------------------------------------------------------------------------
@dataclass
class StreamVerdict:
    label: str
    needed: bool
    ok: Optional[bool]      # None = not needed
    reason: str
    n: int
    auxiliary: bool


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
class RecordingCore:
    """
    Continuous multi-instrument recorder. The caller starts the workers, then
    drives the core with:

        core.open_outputs()
        verdicts, critical_ok = core.startup_check()
        # ... loop (GUI QTimer or headless while) ...
        batch = core.drain_tick()
        core.maybe_ping()
        ok, reason = core.check_health()
        # ... SMA control on demand: core.arm(), core.fire(...), etc. ...
        core.finalize("complete")
    """

    def __init__(self,
                 cfg: AppConfig,
                 paths: ConsolePaths,
                 lcr_worker: Optional[LcrWorker],
                 h7_worker: Optional[H7Worker],
                 stage_worker: Optional[ZaberWorker],
                 lcr_queue: "Optional[queue.Queue[LcrSample]]",
                 h7_queue: "Optional[queue.Queue[H7Sample]]",
                 stage_queue: "Optional[queue.Queue[StageSample]]",
                 stop_event: threading.Event,
                 status_queue: "Optional[queue.Queue[StatusSample]]" = None,
                 camera_worker: Optional[CameraWorker] = None,
                 on_event: Optional[Callable[[dict], None]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self.paths = paths
        self.lcr_worker = lcr_worker
        self.h7_worker = h7_worker
        self.stage_worker = stage_worker
        self.camera_worker = camera_worker
        self.lcr_queue = lcr_queue
        self.h7_queue = h7_queue
        self.stage_queue = stage_queue
        self.status_queue = status_queue

        # Recent laser displacement (mm) history for the camera's motion-driven
        # frame-rate trigger. Filled during H7 drains when calibration is set;
        # empty otherwise (the camera then falls back to event-driven capture).
        self._laser_hist: "deque[tuple[float, float]]" = deque(maxlen=1000)
        self._laser_lock = threading.Lock()
        self.stop_event = stop_event
        self._on_event = on_event
        self._on_log = on_log

        self.logger = logging.getLogger("RecordingCore")

        # File handles / writers (opened in open_outputs()).
        self._stack: Optional[contextlib.ExitStack] = None
        self._lcr_w = self._h7_w = self._stage_w = None
        self._status_w = self._events_w = None
        self._lcr_f = self._h7_f = self._stage_f = None
        self._status_f = self._events_f = None
        self._flush_counter = 0

        # Recording gate. Queues are ALWAYS drained (so plots stay live and the
        # queues never overflow while idle), but sample rows are written to the
        # CSVs only while `recording` is True. The console flips this on with an
        # explicit "Start REC"; the headless runner turns it on automatically.
        self.recording = False           # DATA (h7/stage/status CSV) — auto-on
        self.recording_started_at: Optional[float] = None
        self.camera_recording = False    # camera VIDEO — the REC button only

        # Sample tallies (for meta + readouts). Count only RECORDED rows.
        self.n_lcr = self.n_h7 = self.n_stage = self.n_status = 0
        self.n_events = 0

        # SMA actuation state (drives the heartbeat ping).
        self.armed = False
        self.actuating = False
        self._last_ping_mono = 0.0

        # Result of the last measure_baseline() run (cold R + sensor zeros),
        # recorded into meta.json. None until the baseline phase is run.
        self.baseline: Optional[dict] = None

        # Per-instrument staleness tracking (reset by reset_health_baseline()).
        self._hc_last_n: dict[str, int] = {}
        self._hc_last_adv: dict[str, float] = {}
        self._hc_last_warn: dict[str, float] = {}

        # De-embed reference freshness (for the GUI's "no recent ref" guard).
        self.last_ref_open_mono: Optional[float] = None
        self.last_ref_short_mono: Optional[float] = None

        # Session bookkeeping.
        self.session_started_at = time.time()
        self.session_ended_at: Optional[float] = None
        self.finalized = False
        self.errors: list[str] = []
        self.outcome: str = "running"

    # ------------------------------------------------------------------
    @property
    def _stage_active(self) -> bool:
        return self.stage_worker is not None and self.stage_queue is not None

    def _any_worker_error(self) -> bool:
        return any(w is not None and w.error is not None
                   for w in (self.lcr_worker, self.h7_worker, self.stage_worker))

    def _log(self, level: str, msg: str) -> None:
        getattr(self.logger, level, self.logger.info)(msg)
        if self._on_log is not None:
            try:
                self._on_log(level, msg)
            except Exception:  # noqa: BLE001  — a UI callback must never kill the core
                self.logger.exception("on_log callback raised")

    # ------------------------------------------------------------------
    # Output files
    # ------------------------------------------------------------------
    def open_outputs(self) -> None:
        """Open the continuous CSVs + events.csv and write headers. The schema
        of the per-stream files is IDENTICAL to the old per-phase files so the
        analyzer reuses the same columns."""
        stack = contextlib.ExitStack()
        self._stack = stack
        if self.lcr_queue is not None:
            self._lcr_f = stack.enter_context(
                open(self.paths.lcr_csv, "w", newline=""))
            self._lcr_w = csv.writer(self._lcr_f)
            self._lcr_w.writerow(["host_timestamp_s", "monotonic_s",
                                  "primary", "secondary", "status"])
        if self.h7_queue is not None:
            self._h7_f = stack.enter_context(
                open(self.paths.h7_csv, "w", newline=""))
            self._h7_w = csv.writer(self._h7_f)
            self._h7_w.writerow(["host_timestamp_s", "monotonic_s",
                                 "firmware_timestamp_us", "src", "channel",
                                 "value", "raw_code", "hw_us", "seq"])
        if self._stage_active:
            self._stage_f = stack.enter_context(
                open(self.paths.stage_csv, "w", newline=""))
            self._stage_w = csv.writer(self._stage_f)
            self._stage_w.writerow(
                ["host_timestamp_s", "monotonic_s", "position_mm"])
        if self.status_queue is not None:
            self._status_f = stack.enter_context(
                open(self.paths.status_csv, "w", newline=""))
            self._status_w = csv.writer(self._status_f)
            self._status_w.writerow(
                ["host_timestamp_s", "monotonic_s", "fields_json"])
        # events.csv is always written — it is the segmentation key.
        self._events_f = stack.enter_context(
            open(self.paths.events_csv, "w", newline=""))
        self._events_w = csv.writer(self._events_f)
        self._events_w.writerow(
            ["host_timestamp_s", "monotonic_s", "kind", "detail"])
        self._events_f.flush()
        self.log_event("session", f"start {self.paths.session_id}")

    # ------------------------------------------------------------------
    # events.csv — the segmentation markers
    # ------------------------------------------------------------------
    def log_event(self, kind: str, detail: str) -> None:
        """Append one row to events.csv. kind ∈ {session, cmd, arm, disarm,
        ref_open, ref_short, warn, error}. Always flushed (events are rare and
        the analyzer must see them even after a crash)."""
        ts = time.time()
        mono = time.monotonic()
        if self._events_w is not None:
            self._events_w.writerow([f"{ts:.6f}", f"{mono:.6f}", kind, detail])
            if self._events_f is not None:
                self._events_f.flush()
        self.n_events += 1
        if self._on_event is not None:
            try:
                self._on_event({"host_timestamp_s": ts, "monotonic_s": mono,
                                "kind": kind, "detail": detail})
            except Exception:  # noqa: BLE001
                self.logger.exception("on_event callback raised")

    # ------------------------------------------------------------------
    # Startup full-system check (identity + streaming + sane values)
    # ------------------------------------------------------------------
    def startup_check(self, timeout_s: float = HEALTH_TIMEOUT_S
                      ) -> "tuple[list[StreamVerdict], bool]":
        """Collect a window of samples, assess each enabled stream, and return
        (verdicts, critical_ok). critical_ok is False only when a CRITICAL
        (non-auxiliary) stream fails — an auxiliary failure is logged as a warn
        event but leaves critical_ok True so the SMA run can proceed."""
        self._log("info", "Running %.1f s full system check..." % timeout_s)
        deadline = time.monotonic() + timeout_s
        lcr_n = h7_n = stage_n = 0
        lcr_s: list = []
        h7_s: list = []
        stage_s: list = []
        need_lcr = self.lcr_queue is not None
        need_h7 = self.h7_queue is not None
        need_stage = self._stage_active

        while time.monotonic() < deadline:
            if need_lcr:
                lcr_n += self._collect_drain(self.lcr_queue, lcr_s)
            if need_h7:
                h7_n += self._collect_drain(self.h7_queue, h7_s)
            if need_stage:
                stage_n += self._collect_drain(self.stage_queue, stage_s)
            # A stream is "satisfied" once it has its samples OR its worker has
            # already errored (no point waiting on a dead worker). Counting an
            # errored aux worker as satisfied is what lets the window end as
            # soon as the CRITICAL H7 has its 20 samples — without it, a failed
            # LCR/Zaber (which never reaches its minimum) would stall the check
            # for the full timeout.
            def _done(need, n, minn, w):
                return (not need or n >= minn
                        or (w is not None and w.error is not None))
            done = (_done(need_lcr, lcr_n, HEALTH_MIN_LCR, self.lcr_worker)
                    and _done(need_h7, h7_n, HEALTH_MIN_H7, self.h7_worker)
                    and _done(need_stage, stage_n, HEALTH_MIN_STAGE,
                              self.stage_worker))
            if done:
                break
            time.sleep(0.05)
        if need_lcr:
            lcr_n += self._collect_drain(self.lcr_queue, lcr_s)
        if need_h7:
            h7_n += self._collect_drain(self.h7_queue, h7_s)
        if need_stage:
            stage_n += self._collect_drain(self.stage_queue, stage_s)

        lcr_pass, lcr_why = self._assess_lcr(need_lcr, lcr_n, lcr_s)
        h7_pass, h7_why = self._assess_h7(need_h7, h7_n, h7_s)
        stage_pass, stage_why = self._assess_stage(need_stage, stage_n, stage_s)

        verdicts = [
            StreamVerdict(STREAM_H7, need_h7, h7_pass, h7_why, h7_n, False),
            StreamVerdict(STREAM_LCR, need_lcr, lcr_pass, lcr_why, lcr_n, True),
            StreamVerdict(STREAM_STAGE, need_stage, stage_pass, stage_why,
                          stage_n, True),
        ]

        critical_ok = True
        for v in verdicts:
            if not v.needed:
                continue
            tag = "PASS" if v.ok else "FAIL"
            self._log("info" if v.ok else "warning",
                      f"health {v.label:<5} {tag} ({v.n}) — {v.reason}")
            if v.ok is False:
                if v.auxiliary:
                    # Auxiliary: warn + record, but do NOT block the run.
                    self.log_event("warn", f"{v.label} startup: {v.reason}")
                    self.errors.append(f"{v.label} health (auxiliary): {v.reason}")
                else:
                    self.errors.append(f"{v.label} health failure: {v.reason}")
                    critical_ok = False
        return verdicts, critical_ok

    def _assess_lcr(self, need: bool, n: int, samples: list):
        if not need:
            return None, "not enabled"
        if self.lcr_worker.error is not None:
            return False, f"worker error {self.lcr_worker.error!r}"
        if n < HEALTH_MIN_LCR:
            return False, f"only {n} samples (need >={HEALTH_MIN_LCR})"
        idn = (self.lcr_worker.idn or "")
        if "E4980" not in idn.upper():
            return False, f"unexpected IDN {idn!r} (want an E4980)"
        finite = [s for s in samples
                  if math.isfinite(s.primary) and math.isfinite(s.secondary)]
        if not finite:
            return False, "all readings non-finite (NaN/inf) — not measuring"
        if samples and all(s.status != 0 for s in samples):
            return False, "every sample reports LCR status != 0"
        return True, f"E4980 ok, {n} samples, {len(finite)} finite"

    def _assess_h7(self, need: bool, n: int, samples: list):
        if not need:
            return None, "not enabled"
        if self.h7_worker.error is not None:
            return False, f"worker error {self.h7_worker.error!r}"
        if n < HEALTH_MIN_H7:
            return False, f"only {n} samples (need >={HEALTH_MIN_H7})"
        if samples and all(s.src is None for s in samples):
            return False, ("stream has no src column — wrong/legacy firmware "
                           "(need Firmware_SMASensorHub_PIO)")
        keep = set(self.cfg.h7.channels or [])
        for chname in ("laser", "load"):
            if chname not in keep:
                continue
            vals = [s.value for s in samples
                    if s.channel == chname and math.isfinite(s.value)]
            if not vals:
                return False, f"{chname}: no finite samples in the window"
            if min(vals) < -0.1 or max(vals) > 5.2:
                return False, (f"{chname}: {min(vals):.3f}..{max(vals):.3f} V "
                               "outside the 0-5 V range")
            if len(vals) >= 5 and min(vals) == max(vals):
                self._log("warning",
                          f"HEALTH: {chname} reads a constant {vals[0]:.4f} V "
                          "— verify the sensor is live and the cable is seated")
        return True, f"combined firmware, {n} samples"

    def _assess_stage(self, need: bool, n: int, samples: list):
        if not need:
            return None, "not enabled"
        if self.stage_worker.error is not None:
            return False, f"worker error {self.stage_worker.error!r}"
        if n < HEALTH_MIN_STAGE:
            return False, f"only {n} reads (need >={HEALTH_MIN_STAGE})"
        pos = [s.position_mm for s in samples if math.isfinite(s.position_mm)]
        if not pos:
            return False, "no finite position reads"
        # The Zaber worker is TELEMETRY-ONLY in V3 — it never commands motion,
        # so the stage being parked outside the workflow window [lo, hi] is an
        # operating state, not a hardware fault. A connected stage that streams
        # finite positions PASSES; we only warn so the operator knows to move
        # it into range before actuating. (Previously this returned False and
        # the indicator showed the stage as "offline" even though COM5 was up.)
        lo, hi = self.cfg.stage.limits_tuple()
        if min(pos) < lo - 1.0 or max(pos) > hi + 1.0:
            self._log("warning",
                      f"HEALTH: stage parked at {pos[-1]:.2f} mm, outside the "
                      f"workflow window [{lo}, {hi}] — move it into range "
                      "before actuating")
            return True, (f"{n} reads, pos~{pos[-1]:.2f} mm "
                          f"(outside [{lo}, {hi}] — move into range)")
        return True, f"{n} reads, pos~{pos[-1]:.2f} mm"

    @staticmethod
    def _collect_drain(q: "Optional[queue.Queue[Any]]", bucket: list,
                       cap: int = 300) -> int:
        if q is None:
            return 0
        n = 0
        try:
            while True:
                item = q.get_nowait()
                n += 1
                if len(bucket) < cap:
                    bucket.append(item)
        except queue.Empty:
            pass
        return n

    @staticmethod
    def _discard_drain(q: "Optional[queue.Queue[Any]]") -> int:
        if q is None:
            return 0
        n = 0
        try:
            while True:
                q.get_nowait()
                n += 1
        except queue.Empty:
            pass
        return n

    def discard_backlog(self) -> None:
        """Drop whatever queued up during the startup check so recording
        starts clean. Call once between startup_check() and the drain loop."""
        self._discard_drain(self.lcr_queue)
        self._discard_drain(self.h7_queue)
        self._discard_drain(self.stage_queue)
        self._discard_drain(self.status_queue)

    # ------------------------------------------------------------------
    # Mid-run staleness monitor
    # ------------------------------------------------------------------
    def reset_health_baseline(self) -> None:
        self._hc_last_n = {}
        self._hc_last_adv = {}
        self._hc_last_warn = {}

    def check_health(self) -> "tuple[bool, str]":
        """Per-instrument liveness. Returns ok=False ONLY when a CRITICAL
        stream (the H7 hub) has been silent >HEALTH_ABORT_STALE_S; auxiliary
        streams (LCR/stage) only ever warn. Staleness = no advance in the
        worker's n_pushed."""
        now = time.monotonic()
        targets: list[tuple[str, Any]] = []
        if self.lcr_worker is not None and self.lcr_queue is not None:
            targets.append((STREAM_LCR, self.lcr_worker))
        if self.h7_worker is not None and self.h7_queue is not None:
            # The H7 streams continuously only if laser/load are enabled; an
            # SMA-only channel set is legitimately quiet until a cycle runs.
            ch = set(self.cfg.h7.channels or [])
            if (ch & {"laser", "load"}) or self.actuating:
                targets.append((STREAM_H7, self.h7_worker))
        if self._stage_active and self.stage_worker is not None:
            targets.append((STREAM_STAGE, self.stage_worker))

        for name, w in targets:
            n = w.n_pushed
            if n > self._hc_last_n.get(name, -1):
                self._hc_last_n[name] = n
                self._hc_last_adv[name] = now
                continue
            stale = now - self._hc_last_adv.get(name, now)
            auxiliary = name in AUXILIARY_STREAMS
            if stale >= HEALTH_ABORT_STALE_S and not auxiliary:
                return False, (f"{name} no samples for {stale:.1f}s "
                               f"(>{HEALTH_ABORT_STALE_S:.0f}s)")
            warn_floor = (HEALTH_ABORT_STALE_S if auxiliary
                          else HEALTH_WARN_STALE_S)
            if stale >= warn_floor and (
                    now - self._hc_last_warn.get(name, 0.0) >= 2.0):
                self._hc_last_warn[name] = now
                kind = "auxiliary" if auxiliary else "critical"
                msg = (f"{name} silent {stale:.1f}s — no new samples "
                       f"({kind}; other instruments still recording)")
                self._log("warning", "HEALTH WARNING: " + msg)
                self.log_event("warn", msg)
        return True, ""

    # ------------------------------------------------------------------
    # Drain tick — the heartbeat that writes CSV and returns samples
    # ------------------------------------------------------------------
    def drain_tick(self) -> DrainBatch:
        """Drain all queues to their CSVs and return the drained samples so the
        caller can update plots/readouts. Flushes every ~20 calls."""
        batch = DrainBatch()
        batch.lcr = self._drain_lcr()
        batch.h7 = self._drain_h7()
        batch.stage = self._drain_stage()
        batch.status = self._drain_status()
        self._flush_counter += 1
        if self._flush_counter % 20 == 0:
            for f in (self._lcr_f, self._h7_f, self._stage_f, self._status_f):
                if f is not None:
                    f.flush()
        return batch

    # ------------------------------------------------------------------
    # Recording gate (manual start/stop)
    # ------------------------------------------------------------------
    def start_recording(self) -> bool:
        """Begin writing drained samples to the CSVs. Idempotent. Requires
        open_outputs() to have run. Returns True if recording is now on."""
        if self.recording:
            return True
        if self._events_w is None:
            self._log("warning",
                      "start_recording() called before open_outputs() — ignored")
            return False
        self.recording = True
        self.recording_started_at = time.time()
        self.log_event("session", "recording start")
        self._log("info", "Recording STARTED")
        return True

    def start_camera_recording(self) -> bool:
        """Start writing camera VIDEO (frames.csv + per-cycle JPEGs). Independent
        of data recording (which auto-starts on launch); the REC button drives
        only this. The camera worker opens/closes its own files off is_recording."""
        if self.paths.session_dir is None:
            return False
        self.camera_recording = True
        if self.camera_worker is not None:
            self.camera_worker.mark_fast()
        return True

    def stop_camera_recording(self) -> None:
        self.camera_recording = False

    def stop_recording(self) -> None:
        """Stop writing samples to the CSVs (queues keep draining for plots).
        Does NOT touch the SMA / actuation state — use disarm() for safety."""
        if not self.recording:
            return
        self.recording = False
        self.log_event("session", "recording stop")
        self._log("info", "Recording STOPPED")
        for f in (self._lcr_f, self._h7_f, self._stage_f, self._status_f):
            if f is not None:
                with contextlib.suppress(Exception):
                    f.flush()

    # ------------------------------------------------------------------
    # Per-stream reconnect (rebuild a dead/offline worker, reuse its queue)
    # ------------------------------------------------------------------
    def stream_status(self, label: str) -> Optional[bool]:
        """Live status for an indicator dot:
            None  -> stream disabled (not part of this session)
            False -> worker missing, crashed (error set), or thread dead
            True  -> worker thread alive and not errored (connected/streaming)
        """
        if label == STREAM_CAMERA:
            # The camera has no queue (it writes its own files), so key off the
            # worker alone.
            w = self.camera_worker
            if w is None:
                return None
            if w.error is not None:
                return False
            return bool(w.is_alive())
        w, q = {
            STREAM_H7: (self.h7_worker, self.h7_queue),
            STREAM_LCR: (self.lcr_worker, self.lcr_queue),
            STREAM_STAGE: (self.stage_worker, self.stage_queue),
        }.get(label, (None, None))
        if w is None or q is None:
            return None
        if w.error is not None:
            return False
        return bool(w.is_alive())

    def restart_worker(self, label: str) -> "tuple[bool, str]":
        """Rebuild and restart one stream's worker after a startup failure or a
        mid-run disconnect, reusing its existing queue. Only acts on a stream
        whose worker is dead/errored. Returns (started, message)."""
        if self.finalized or self.stop_event.is_set():
            return False, "session is shutting down"

        if label == STREAM_LCR:
            if not self.cfg.lcr.enabled or self.lcr_queue is None:
                return False, "LCR is disabled for this session"
            if self.lcr_worker is not None and self.lcr_worker.is_alive():
                return False, "LCR already connected"
            self.lcr_worker = LcrWorker(self.cfg.lcr, self.lcr_queue,
                                        self.stop_event)
            self.lcr_worker.start()
        elif label == STREAM_STAGE:
            if not self.cfg.stage.enabled or self.stage_queue is None:
                return False, "stage is disabled for this session"
            if self.stage_worker is not None and self.stage_worker.is_alive():
                return False, "stage already connected"
            self.stage_worker = ZaberWorker(self.cfg.stage, self.stage_queue,
                                            self.stop_event)
            self.stage_worker.start()
        elif label == STREAM_H7:
            if not self.cfg.h7.enabled or self.h7_queue is None:
                return False, "H7 is disabled for this session"
            if self.h7_worker is not None and self.h7_worker.is_alive():
                return False, "H7 already connected"
            self.h7_worker = H7Worker(self.cfg.h7, self.h7_queue,
                                      self.stop_event,
                                      status_queue=self.status_queue)
            self.h7_worker.start()
        elif label == STREAM_CAMERA:
            if not self.cfg.camera.enabled:
                return False, "camera is disabled for this session"
            if self.camera_worker is not None and self.camera_worker.is_alive():
                return False, "camera already connected"
            cam = make_camera(self.cfg.camera, self.stop_event)
            self.bind_camera(cam)
            cam.start()
        else:
            return False, f"unknown stream {label!r}"

        # Clear this stream's staleness baseline so the freshly-restarted
        # worker isn't instantly flagged silent by check_health().
        self._hc_last_n.pop(label, None)
        self._hc_last_adv.pop(label, None)
        self._hc_last_warn.pop(label, None)
        self.log_event("reconnect", f"{label} restart requested")
        self._log("info", f"{label}: reconnect requested")
        return True, f"{label}: reconnecting…"

    # -- operator-initiated stage motion (manual) -------------------------
    # The recorder pipeline stays telemetry-only; these are façade passthroughs
    # so the GUI can drive the stage manually without reaching into the worker
    # (which restart_worker() may replace). Always resolve the CURRENT worker.
    def stage_home(self) -> "tuple[bool, str]":
        if not self._stage_active or self.stage_worker is None:
            return False, "stage is disabled for this session"
        return self.stage_worker.request_home()

    def stage_move(self, target_mm: float) -> "tuple[bool, str]":
        if not self._stage_active or self.stage_worker is None:
            return False, "stage is disabled for this session"
        return self.stage_worker.request_move(target_mm)

    def stage_stop(self) -> "tuple[bool, str]":
        if not self._stage_active or self.stage_worker is None:
            return False, "stage is disabled for this session"
        return self.stage_worker.request_stop()

    def stage_set_limits(self, lo_mm: float, hi_mm: float) -> "tuple[bool, str]":
        if not self._stage_active or self.stage_worker is None:
            return False, "stage is disabled for this session"
        return self.stage_worker.set_limits(lo_mm, hi_mm)

    # -- camera support ---------------------------------------------------
    def _laser_to_mm(self, v_volts: float) -> Optional[float]:
        """Raw laser volts → displacement mm using config calibration, or None
        if the laser isn't calibrated (same formula as analyze_sma / console)."""
        lc = self.cfg.calibration.laser
        if lc.k_mV_per_um in (None, 0) or lc.V0_mV is None:
            return None
        return (v_volts * 1000.0 - lc.V0_mV) / lc.k_mV_per_um / 1000.0

    def get_laser_hist(self) -> "list[tuple[float, float]]":
        """Snapshot of recent (monotonic_s, displacement_mm) — for the camera
        worker's motion trigger. Thread-safe."""
        with self._laser_lock:
            return list(self._laser_hist)

    def camera_mark_fast(self) -> None:
        """Nudge the camera into fast capture (heat/idle transitions)."""
        if self.camera_worker is not None:
            self.camera_worker.mark_fast()

    def reconnect_camera(self) -> "tuple[bool, str]":
        """Restart the camera worker to apply a new resolution/fps. Refused
        while the CAMERA is recording video (would interrupt it). Data recording
        is auto-on and does not block this. Stops only the camera."""
        if not self.cfg.camera.enabled:
            return False, "camera is disabled for this session"
        if self.camera_recording:
            return False, "stop camera video before changing resolution/fps"
        old = self.camera_worker
        if old is not None:
            old.stop_local()
            old.join(timeout=3.0)
        cam = CameraWorker(self.cfg.camera, self.stop_event)
        self.bind_camera(cam)
        cam.start()
        self.log_event("reconnect", "camera reconfigured")
        return True, "camera reconnecting…"

    def bind_camera(self, cam: CameraWorker) -> None:
        """Wire a CameraWorker's hooks to this core (recording gate, laser
        history, session dir, event log). Called at build and on reconnect."""
        cam.laser_provider = self.get_laser_hist
        cam.is_recording = lambda: self.camera_recording   # REC button, not data
        cam.session_dir = lambda: self.paths.session_dir
        cam.on_event = self.log_event
        self.camera_worker = cam

    def _drain_lcr(self) -> list:
        out: list = []
        if self.lcr_queue is None:
            return out
        write = self.recording and self._lcr_w is not None
        try:
            while True:
                s: LcrSample = self.lcr_queue.get_nowait()
                if write:
                    self._lcr_w.writerow([
                        f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                        f"{s.primary:.8e}", f"{s.secondary:.8f}", s.status,
                    ])
                out.append(s)
        except queue.Empty:
            pass
        if write:
            self.n_lcr += len(out)
        return out

    def _drain_h7(self) -> list:
        out: list = []
        if self.h7_queue is None:
            return out
        write = self.recording and self._h7_w is not None
        cam_on = self.camera_worker is not None
        try:
            while True:
                s: H7Sample = self.h7_queue.get_nowait()
                # Feed the camera's motion trigger with calibrated laser mm.
                if cam_on and s.channel == "laser":
                    mm = self._laser_to_mm(s.value)
                    if mm is not None:
                        with self._laser_lock:
                            self._laser_hist.append((s.monotonic_s, mm))
                if write:
                    self._h7_w.writerow([
                        f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                        s.firmware_timestamp_us,
                        s.src if s.src is not None else "",
                        s.channel if s.channel is not None else "",
                        f"{s.value:.8f}",
                        s.raw_code if s.raw_code is not None else "",
                        s.hw_us if s.hw_us is not None else "",
                        s.seq if s.seq is not None else "",
                    ])
                out.append(s)
        except queue.Empty:
            pass
        if write:
            self.n_h7 += len(out)
        return out

    def _drain_stage(self) -> list:
        out: list = []
        if self.stage_queue is None:
            return out
        write = self.recording and self._stage_w is not None
        try:
            while True:
                s: StageSample = self.stage_queue.get_nowait()
                if write:
                    self._stage_w.writerow([
                        f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                        f"{s.position_mm:.6f}",
                    ])
                out.append(s)
        except queue.Empty:
            pass
        if write:
            self.n_stage += len(out)
        return out

    def _drain_status(self) -> list:
        out: list = []
        if self.status_queue is None:
            return out
        write = self.recording and self._status_w is not None
        try:
            while True:
                s: StatusSample = self.status_queue.get_nowait()
                if write:
                    self._status_w.writerow([
                        f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                        json.dumps(s.fields, separators=(",", ":")),
                    ])
                out.append(s)
        except queue.Empty:
            pass
        if write:
            self.n_status += len(out)
        return out

    # ------------------------------------------------------------------
    # SMA control — every command routed through h7_commands + logged
    # ------------------------------------------------------------------
    def sma_send(self, cmd: str, *, kind: str = "cmd") -> bool:
        """Queue one command for the H7Worker to transmit and log it to
        events.csv. Returns False only if the H7 is disabled / has no worker.

        The command is ENQUEUED (non-blocking) and physically written by the
        reader thread — this thread (GUI + sole CSV writer) never touches the
        serial port, so a stalled firmware can neither freeze the console nor
        race the reader on the shared handle."""
        w = self.h7_worker
        if w is None or not hasattr(w, "send_command"):
            self._log("warning", f"SMA command {cmd!r} dropped — H7 unavailable")
            self.log_event("error", f"dropped {cmd!r}: H7 unavailable")
            return False
        try:
            w.send_command(cmd)          # enqueue -> sent on the H7 reader thread
            self.log_event(kind, cmd)
            return True
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"SMA send_command({cmd!r}) enqueue failed: {e}")
            self.log_event("error", f"enqueue failed {cmd!r}: {e}")
            return False

    def arm(self) -> bool:
        ok = self.sma_send(h7.arm(), kind="arm")
        if ok:
            self.armed = True
        return ok

    def disarm(self) -> bool:
        # Disarm is the master safety cutoff — record the attempt regardless.
        ok = self.sma_send(h7.disarm(), kind="disarm")
        self.armed = False
        self.actuating = False
        return ok

    def set_idle(self, v_idle: float) -> bool:
        return self.sma_send(h7.idle(v_idle))

    def set_wdt(self, ms: int) -> bool:
        return self.sma_send(h7.wdt(ms))

    def stop_actuation(self) -> bool:
        """Graceful stop -> idle-low (still armed). This is the drop toward the
        idle voltage where the cooling elongation begins — force fast capture."""
        ok = self.sma_send(h7.stop())
        self.actuating = False
        self.camera_mark_fast()
        return ok

    def _ensure_armed(self) -> bool:
        if self.armed:
            return True
        return self.arm()

    def fire(self, v_high: float, t_high_ms: int = 500) -> bool:
        """Single heat (n=1). Arms first if needed."""
        if not self._ensure_armed():
            return False
        ok = self.sma_send(h7.fire(v_high, t_high_ms))
        if ok:
            self.actuating = True
            self._last_ping_mono = time.monotonic()
            self.camera_mark_fast()
        return ok

    def cycle(self, v_high: float, v_idle: float,
              t_high_ms: int, t_idle_ms: int, n: int) -> bool:
        """Autonomous heat/cool cycle. Arms first if needed."""
        if not self._ensure_armed():
            return False
        ok = self.sma_send(h7.cycle(v_high, v_idle, t_high_ms, t_idle_ms, n))
        if ok:
            self.actuating = True
            self._last_ping_mono = time.monotonic()
            self.camera_mark_fast()
        return ok

    def cycle_cc(self, i_high_ma: float, i_low_ma: float,
                 t_high_ms: int, t_idle_ms: int, n: int) -> bool:
        """Constant-current heat/cool cycle (`cccycle`). Arms first if needed.

        Requires Firmware_SMAConstantCurrent_PIO — the sensor-hub image rejects
        the command (visible as an `[SMA]` line, not as silent inaction).
        """
        if not self._ensure_armed():
            return False
        ok = self.sma_send(h7.cccycle(i_high_ma, i_low_ma,
                                      t_high_ms, t_idle_ms, n))
        if ok:
            self.actuating = True
            self._last_ping_mono = time.monotonic()
            self.camera_mark_fast()
        return ok

    def ccfire(self, ma: float, t_high_ms: int = 500) -> bool:
        """Single constant-current pulse (n=1). Arms first if needed."""
        if not self._ensure_armed():
            return False
        ok = self.sma_send(h7.ccfire(ma, t_high_ms))
        if ok:
            self.actuating = True
            self._last_ping_mono = time.monotonic()
            self.camera_mark_fast()
        return ok

    def start_cycle_from_config(self) -> bool:
        """Headless convenience: arm + wdt + the cycle described by cfg.sma.

        Honours cfg.sma.mode: "voltage" -> `cycle`, "current" -> `cccycle`
        preceded by the CC tuning commands (tau / ccgain). Tuning is sent AFTER
        arm and BEFORE the cycle so it is in effect for the first heat phase,
        and it lands in events.csv so a run's gains are recoverable offline.
        """
        sma = self.cfg.sma
        self.arm()
        self.set_wdt(sma.wdt_ms)
        for cmd in sma.tuning_commands():
            self.sma_send(cmd)
        if sma.is_current_mode:
            ok = self.cycle_cc(sma.i_high_ma, sma.i_low_ma, sma.fire_ms,
                               sma.cool_ms, sma.n_cycles)
        else:
            ok = self.cycle(sma.v_high, sma.v_low, sma.fire_ms,
                            sma.cool_ms, sma.n_cycles)
        if not ok:
            self.errors.append("sma_cycle_start_failed: H7 reader unavailable")
        return ok

    # ------------------------------------------------------------------
    # Baseline / sensor-zero phase — "measure cold R + zero all sensors"
    # ------------------------------------------------------------------
    def measure_baseline(self) -> dict:
        """Quiescent baseline phase: capture per-session zero references while
        the coil is cold and at rest.

        Arms at the (low, non-heating) ``baseline.probe_v`` and issues a single
        ``drive`` at that level so the firmware streams src=3/4/5 (V/I/R) for a
        short window WITHOUT heating; laser/load stream continuously anyway.
        Averages the window to get the cold SMA resistance and the laser/load
        rest voltages, then AUTO-DISARMS. The load rest voltage is written into
        ``calibration.load_cell.offset_V`` (the tare) unless the channel is
        saturated; the laser rest voltage is recorded as a tare reference
        WITHOUT touching the absolute (k, V0) calibration.

        Must run BEFORE recording — it drains the sample queues, so any samples
        it consumes would not reach the CSVs. Returns (and stores on
        ``self.baseline``) a result dict; it is written into meta.json.
        """
        cfg = self.cfg.baseline
        started = time.time()

        def _fail(reason: str) -> dict:
            res = {"ok": False, "reason": reason,
                   "measured_at_utc": time.strftime(
                       "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))}
            self.baseline = res
            self._log("warning", f"baseline aborted: {reason}")
            self.log_event("baseline", f"aborted: {reason}")
            return res

        if self.recording:
            return _fail("stop recording before measuring baseline")
        if self.h7_queue is None or getattr(self.h7_worker, "reader", None) is None:
            return _fail("H7 reader unavailable")

        self.log_event("baseline",
                       f"start probe_v={cfg.probe_v:g} dur={cfg.duration_s:g}s")
        self._log("info",
                  "Baseline: arming at %.3f V probe (non-heating) for %.1f s..."
                  % (cfg.probe_v, cfg.duration_s))

        # Arm and HOLD at the low probe level (no drive/heat). The firmware
        # streams V/I/R while armed+idle, so we simply read that idle stream.
        self.arm()
        self.set_idle(cfg.probe_v)

        # Drop the pre-probe backlog, let the hold settle, then collect a window.
        self._discard_drain(self.h7_queue)
        self._discard_drain(self.stage_queue)
        time.sleep(max(0.0, cfg.settle_s))
        h7_s: list = []
        stage_s: list = []
        deadline = time.monotonic() + cfg.duration_s
        while time.monotonic() < deadline:
            self._collect_drain(self.h7_queue, h7_s, cap=1_000_000)
            self._collect_drain(self.stage_queue, stage_s, cap=1_000_000)
            time.sleep(0.02)
        self._collect_drain(self.h7_queue, h7_s, cap=1_000_000)
        self._collect_drain(self.stage_queue, stage_s, cap=1_000_000)

        # Safety: always return to the disarmed (zero-current) state.
        self.disarm()

        # Reduce the window.
        def _mean(xs):
            return (sum(xs) / len(xs)) if xs else None

        laser = [s.value for s in h7_s if s.channel == "laser"]
        load = [s.value for s in h7_s if s.channel == "load"]
        load_raw = [s.raw_code for s in h7_s
                    if s.channel == "load" and s.raw_code is not None]
        r_vals = [s.value for s in h7_s
                  if s.channel == "sma_r" and math.isfinite(s.value)]
        stage_pos = [s.position_mm for s in stage_s]

        laser_v = _mean(laser)
        load_v = _mean(load)
        cold_r = _mean(r_vals)

        # Load-cell saturation: raw pinned at ±2^23 (ADC full scale).
        FS = 8_388_608
        n_sat = sum(1 for r in load_raw if abs(r) >= FS - 8)
        load_sat_frac = (n_sat / len(load_raw)) if load_raw else 0.0
        load_range_frac = (abs(load_v) / 5.0) if load_v is not None else None

        warnings: list[str] = []
        if not load:
            warnings.append("no load-cell samples in the window")
        if not laser:
            warnings.append("no laser samples in the window")
        if not r_vals:
            warnings.append("no finite SMA resistance samples — check the "
                            "arm/drive path and the coil current")
        if load_sat_frac > 0.0:
            warnings.append(
                f"LOAD CELL SATURATED — {load_sat_frac*100:.0f}% of samples at "
                f"the ADC rail (±5 V). Force is invalid; null the LCA-9PC ZERO "
                f"pot before trusting/taring the load cell.")
        elif (load_range_frac is not None
              and load_range_frac > cfg.load_saturation_warn_frac):
            warnings.append(
                f"load rest voltage at {load_range_frac*100:.0f}% of the ±5 V "
                f"range — little headroom before saturation; consider nulling "
                f"the LCA-9PC ZERO pot.")

        # Tare the load cell (write measured rest V into the offset) unless it
        # is saturated (taring a railed channel is meaningless) or disabled.
        applied_load_offset = None
        if (cfg.apply_load_offset and load_v is not None
                and load_sat_frac == 0.0):
            self.cfg.calibration.load_cell.offset_V = load_v
            applied_load_offset = load_v

        res = {
            "ok": True,
            "measured_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "probe_v": cfg.probe_v,
            "duration_s": cfg.duration_s,
            "cold_r_ohm": cold_r,
            "sma_r_n": len(r_vals),
            "laser_rest_V": laser_v,       # tare reference (absolute cal untouched)
            "laser_n": len(laser),
            "load_rest_V": load_v,
            "load_n": len(load),
            "load_saturated_frac": load_sat_frac,
            "load_range_frac": load_range_frac,
            "applied_load_offset_V": applied_load_offset,
            "stage_mm": _mean(stage_pos),
            "warnings": warnings,
        }
        self.baseline = res

        def _g(x):
            return f"{x:.4g}" if isinstance(x, (int, float)) else "—"
        self.log_event(
            "baseline",
            f"done cold_R={_g(cold_r)}ohm laser={_g(laser_v)}V "
            f"load={_g(load_v)}V load_offset={_g(applied_load_offset)} "
            f"warn={len(warnings)}")
        self._log("info",
                  "Baseline: cold_R=%s Ω, laser_rest=%s V, load_rest=%s V "
                  "(offset %s), %d warning(s)"
                  % (_g(cold_r), _g(laser_v), _g(load_v),
                     "applied" if applied_load_offset is not None else "NOT applied",
                     len(warnings)))
        for w in warnings:
            self._log("warning", f"baseline: {w}")
        return res

    def maybe_ping(self, interval_s: float = 1.0) -> None:
        """Send the 1 Hz heartbeat that resets the firmware heat watchdog,
        but only while an actuation is in flight."""
        if not self.actuating:
            return
        now = time.monotonic()
        if now - self._last_ping_mono >= interval_s:
            self.sma_send(h7.ping(), kind="cmd")
            self._last_ping_mono = now

    # ------------------------------------------------------------------
    # De-embed reference markers (OPEN / SHORT, on demand)
    # ------------------------------------------------------------------
    def ref_open(self, detail: str = "") -> None:
        self.last_ref_open_mono = time.monotonic()
        self.log_event("ref_open", detail or "OPEN reference marked")

    def ref_short(self, detail: str = "") -> None:
        self.last_ref_short_mono = time.monotonic()
        self.log_event("ref_short", detail or "SHORT reference marked")

    def has_recent_refs(self, max_age_s: float = 3600.0) -> bool:
        now = time.monotonic()
        return (self.last_ref_open_mono is not None
                and self.last_ref_short_mono is not None
                and (now - self.last_ref_open_mono) <= max_age_s
                and (now - self.last_ref_short_mono) <= max_age_s)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def stop_workers(self) -> None:
        self.stop_event.set()
        for w in (self.lcr_worker, self.h7_worker, self.stage_worker,
                  self.camera_worker):
            if w is None:
                continue
            # Robust: a worker that's slow to stop (esp. the camera SUBPROCESS,
            # which is not a Thread and has no .name) must NEVER abort finalize
            # before meta.json is written.
            name = getattr(w, "name", type(w).__name__)
            with contextlib.suppress(Exception):
                w.join(timeout=WORKER_JOIN_TIMEOUT_S)
            with contextlib.suppress(Exception):
                if w.is_alive():
                    self._log("warning",
                              f"{name} did not stop within {WORKER_JOIN_TIMEOUT_S:.1f} s")

    def finalize(self, outcome: str = "complete") -> None:
        """Idempotent teardown: disarm, stop workers, final drain, meta.json,
        close files. Safe to call from a window-close handler or a finally."""
        if self.finalized:
            return
        self.finalized = True
        self.outcome = outcome
        # Safety first: open the MOSFET return path.
        with contextlib.suppress(Exception):
            self.disarm()
        self.stop_workers()
        # One last drain so nothing in the queues is lost.
        with contextlib.suppress(Exception):
            self.drain_tick()
        self.session_ended_at = time.time()
        self.log_event("session", f"end {outcome}")
        self._write_meta()
        if self._stack is not None:
            with contextlib.suppress(Exception):
                self._stack.close()
            self._stack = None

    # ------------------------------------------------------------------
    def _write_meta(self) -> None:
        meta: dict[str, Any] = {
            "session_id": self.paths.session_id,
            "schema": "sma_v3_console",
            "started_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.session_started_at)),
            "ended_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(self.session_ended_at or time.time())),
            "outcome": self.outcome,
            "errors": self.errors,
            "counts": {
                "h7": self.n_h7, "stage": self.n_stage,
                "status": self.n_status, "events": self.n_events,
            },
            # LCR intentionally omitted — removed from this thermal module.
            "h7": {
                **asdict(self.cfg.h7),
                "n_dropped": getattr(self.h7_worker, "n_dropped", None),
                "active": self.h7_queue is not None,
            },
            "stage": {
                **asdict(self.cfg.stage),
                "info": getattr(self.stage_worker, "info", None),
                "n_dropped": getattr(self.stage_worker, "n_dropped", None),
                "active": self._stage_active,
            },
            "camera": {
                **asdict(self.cfg.camera),
                "info": getattr(self.camera_worker, "info", None),
                "actual_res": getattr(self.camera_worker, "actual_res", None),
                "n_written": getattr(self.camera_worker, "n_written", None),
                "active": self.camera_worker is not None,
            },
            "sma": asdict(self.cfg.sma),
            "baseline_config": asdict(self.cfg.baseline),
            "baseline": self.baseline,   # measured cold R + sensor zeros (or null)
            "calibration": asdict(self.cfg.calibration),
            "run": asdict(self.cfg.run),
            "host": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
            },
        }
        with open(self.paths.meta_json, "w") as f:
            json.dump(meta, f, indent=2)
        self._log("info", f"Wrote {self.paths.meta_json}")
