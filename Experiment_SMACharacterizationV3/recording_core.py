"""
recording_core.py — UI-agnostic recording engine for the SMA console (V3).

Extracted from session.py (WI-1 of PLAN_sma_console.md). RecordingCore owns
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

import h7_commands as h7
from config import AppConfig
from workers import (H7Sample, H7Worker, LcrSample, LcrWorker,
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
AUXILIARY_STREAMS = frozenset({STREAM_LCR, STREAM_STAGE})


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
                 on_event: Optional[Callable[[dict], None]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self.paths = paths
        self.lcr_worker = lcr_worker
        self.h7_worker = h7_worker
        self.stage_worker = stage_worker
        self.lcr_queue = lcr_queue
        self.h7_queue = h7_queue
        self.stage_queue = stage_queue
        self.status_queue = status_queue
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
        self.recording = False
        self.recording_started_at: Optional[float] = None

        # Sample tallies (for meta + readouts). Count only RECORDED rows.
        self.n_lcr = self.n_h7 = self.n_stage = self.n_status = 0
        self.n_events = 0

        # SMA actuation state (drives the heartbeat ping).
        self.armed = False
        self.actuating = False
        self._last_ping_mono = 0.0

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
        try:
            while True:
                s: H7Sample = self.h7_queue.get_nowait()
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
        """Send one command to the H7 over the H7Worker's reader and log it to
        events.csv. Safe no-op (returns False) if the H7 is disabled or the
        reader hasn't opened yet."""
        reader = getattr(self.h7_worker, "reader", None)
        if reader is None:
            self._log("warning", f"SMA command {cmd!r} dropped — H7 reader unavailable")
            self.log_event("error", f"dropped {cmd!r}: H7 reader unavailable")
            return False
        try:
            reader.send_command(cmd)
            self.log_event(kind, cmd)
            return True
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"SMA send_command({cmd!r}) failed: {e}")
            self.log_event("error", f"send failed {cmd!r}: {e}")
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
        """Graceful stop -> idle-low (still armed)."""
        ok = self.sma_send(h7.stop())
        self.actuating = False
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
        return ok

    def start_cycle_from_config(self) -> bool:
        """Headless convenience: arm + wdt + the cycle described by cfg.sma."""
        sma = self.cfg.sma
        self.arm()
        self.set_wdt(sma.wdt_ms)
        ok = self.cycle(sma.v_high, sma.v_low, sma.fire_ms,
                        sma.cool_ms, sma.n_cycles)
        if not ok:
            self.errors.append("sma_cycle_start_failed: H7 reader unavailable")
        return ok

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
        for w in (self.lcr_worker, self.h7_worker, self.stage_worker):
            if w is None:
                continue
            w.join(timeout=WORKER_JOIN_TIMEOUT_S)
            if w.is_alive():
                self._log("warning",
                          f"{w.name} did not stop within {WORKER_JOIN_TIMEOUT_S:.1f} s")

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
                "lcr": self.n_lcr, "h7": self.n_h7, "stage": self.n_stage,
                "status": self.n_status, "events": self.n_events,
            },
            "lcr": {
                **asdict(self.cfg.lcr),
                "idn": getattr(self.lcr_worker, "idn", None),
                "n_dropped": getattr(self.lcr_worker, "n_dropped", None),
                "active": self.lcr_queue is not None,
            },
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
            "sma": asdict(self.cfg.sma),
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
