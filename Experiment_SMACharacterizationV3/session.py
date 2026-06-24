"""
session.py — state-machine controller for a V3 SMA characterization session.

A single session walks the operator through three phases:
  1. OPEN  — DUT disconnected, fixed duration (LCR de-embed reference)
  2. SHORT — DUT shorted at the bias-tee end, fixed duration
  3. RAW   — actual experiment, runs until Ctrl+C

Workers (LCR, H7, Zaber stage) stream continuously across all three
phases. The controller is the ONLY file writer: it drains each worker
queue into per-phase CSV files, swapping the active files at phase
boundaries. Any stream can be disabled (worker/queue passed as None).

V3 logs RAW data only — no unit conversion here. Calibration coefficients
are recorded in meta.json for the offline analyzer.

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
from typing import Any, Optional

import h7_commands as h7
import operator_io
from config import AppConfig
from workers import (H7Sample, H7Worker, LcrSample, LcrWorker,
                     StageSample, StatusSample, ZaberWorker)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
HEALTH_TIMEOUT_S = 10.0
HEALTH_MIN_LCR = 5
HEALTH_MIN_H7 = 20
HEALTH_MIN_STAGE = 3
HEALTH_WARN_STALE_S = 1.0    # mid-run: warn if a live instrument goes this long with no new sample
HEALTH_ABORT_STALE_S = 3.0   # mid-run: disarm + end phase + finalize if it stays stale this long
DRAIN_TICK_S = 0.05
WORKER_JOIN_TIMEOUT_S = 5.0
PHASES = ("open", "short", "raw")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
@dataclass
class SessionPaths:
    session_dir: Path
    session_id: str
    meta_json: Path
    log_txt: Path

    def lcr_csv(self, phase: str) -> Path:
        return self.session_dir / f"{phase}_lcr.csv"

    def h7_csv(self, phase: str) -> Path:
        return self.session_dir / f"{phase}_h7.csv"

    def stage_csv(self, phase: str) -> Path:
        return self.session_dir / f"{phase}_stage.csv"

    def status_csv(self, phase: str) -> Path:
        return self.session_dir / f"{phase}_status.csv"


def make_session_paths(output_dir: Path,
                       session_id: Optional[str] = None) -> SessionPaths:
    if session_id is None:
        session_id = "sma_" + time.strftime("%Y%m%d_%H%M%S")
    session_dir = output_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return SessionPaths(
        session_dir=session_dir,
        session_id=session_id,
        meta_json=session_dir / "meta.json",
        log_txt=session_dir / "session.log",
    )


# ---------------------------------------------------------------------------
# Phase content
# ---------------------------------------------------------------------------
PHASE_PROMPTS: dict[str, dict[str, str]] = {
    "open": {
        "title": "OPEN calibration",
        "body": ("Disconnect the DUT.\n"
                 "Leave the bias-tee pigtails OPEN at the DUT end.\n"
                 "Verify nothing is bridging the leads."),
    },
    "short": {
        "title": "SHORT calibration",
        "body": ("Bring the bias-tee pigtails together at the DUT end and\n"
                 "create a clean SHORT (clip lead, solder bridge, etc.).\n"
                 "Keep cable routing identical to how it will be during RAW."),
    },
    "raw": {
        "title": "RAW experiment",
        "body": ("Install the SMA DUT at the bias-tee pigtail end.\n"
                 "Connect the DC actuation supply to the bias-tee DC port.\n"
                 "DO NOT energize yet — wait for the READY banner."),
    },
}


@dataclass
class PhaseMeta:
    duration_s: float = 0.0
    target_duration_s: Optional[float] = None
    lcr_n: int = 0
    h7_n: int = 0
    stage_n: int = 0
    redos: int = 0
    started_at_utc: str = ""
    ended_at_utc: str = ""


# Recording outcomes
_RESULT_COMPLETE = "complete"
_RESULT_ABORT_USER = "abort_user"
_RESULT_ABORT_CRASH = "abort_crash"
_RESULT_ABORT_HEALTH = "abort_health"   # a live instrument went stale mid-run


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class SessionController:
    """
    Owns the phase state machine and meta.json output. Any of the three
    streams may be None (disabled in config). The caller starts all active
    workers BEFORE calling .run() and installs a SIGINT handler that sets
    stop_event.
    """

    def __init__(self,
                 cfg: AppConfig,
                 paths: SessionPaths,
                 lcr_worker: Optional[LcrWorker],
                 h7_worker: Optional[H7Worker],
                 stage_worker: Optional[ZaberWorker],
                 lcr_queue: "Optional[queue.Queue[LcrSample]]",
                 h7_queue: "Optional[queue.Queue[H7Sample]]",
                 stage_queue: "Optional[queue.Queue[StageSample]]",
                 stop_event: threading.Event,
                 status_queue: "Optional[queue.Queue[StatusSample]]" = None):
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

        self.last_functional_step: str = "init"
        self.phase_meta: dict[str, PhaseMeta] = {}
        self.completed: bool = False
        self.aborted_at_phase: Optional[str] = None
        self.errors: list[str] = []
        self.session_started_at: float = time.time()
        self.session_ended_at: Optional[float] = None

        self.logger = logging.getLogger("SessionController")

    # ------------------------------------------------------------------
    @property
    def _stage_active(self) -> bool:
        return self.stage_worker is not None and self.stage_queue is not None

    def _any_worker_error(self) -> bool:
        return any(w is not None and w.error is not None
                   for w in (self.lcr_worker, self.h7_worker, self.stage_worker))

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def run(self) -> int:
        operator_io.banner_session_start(
            self.paths.session_id, str(self.paths.session_dir))
        try:
            if not self._health_check():
                return 2
            for phase in PHASES:
                ok = self._run_phase(phase)
                if not ok:
                    return 1 if self.aborted_at_phase else 2
            self._step("finalized")
            self.completed = True
            return 0
        except KeyboardInterrupt:
            self.logger.info(
                "KeyboardInterrupt at step '%s'", self.last_functional_step)
            self._record_abort(self.last_functional_step)
            return 1
        except BaseException as e:
            self.logger.exception("Session crashed at step '%s': %s",
                                  self.last_functional_step, e)
            self.errors.append(f"{type(e).__name__}: {e}")
            self._record_abort(self.last_functional_step)
            return 2
        finally:
            self.session_ended_at = time.time()
            self._stop_workers()
            self._write_meta()
            operator_io.banner_done(
                self.paths.session_id, self.completed, self.aborted_at_phase)

    # ------------------------------------------------------------------
    def _step(self, name: str) -> None:
        self.logger.info("step → %s", name)
        self.last_functional_step = name

    def _record_abort(self, where: str) -> None:
        if self.aborted_at_phase is None:
            self.aborted_at_phase = where

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    def _health_check(self) -> bool:
        """Full system check before recording: every enabled instrument must
        be connected with the expected identity, streaming, AND producing
        sane actual readings (not just a sample count). Aborts on any fail."""
        self._step("health_check")
        self.logger.info("Running %.1f s full system check...", HEALTH_TIMEOUT_S)
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
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
            done = ((not need_lcr or lcr_n >= HEALTH_MIN_LCR)
                    and (not need_h7 or h7_n >= HEALTH_MIN_H7)
                    and (not need_stage or stage_n >= HEALTH_MIN_STAGE))
            if done or self._any_worker_error():
                break
            time.sleep(DRAIN_TICK_S)
        if need_lcr:
            lcr_n += self._collect_drain(self.lcr_queue, lcr_s)
        if need_h7:
            h7_n += self._collect_drain(self.h7_queue, h7_s)
        if need_stage:
            stage_n += self._collect_drain(self.stage_queue, stage_s)

        # Per-instrument verdict: identity + streaming + actual readings sane.
        lcr_pass, lcr_why = self._assess_lcr(need_lcr, lcr_n, lcr_s)
        h7_pass, h7_why = self._assess_h7(need_h7, h7_n, h7_s)
        stage_pass, stage_why = self._assess_stage(need_stage, stage_n, stage_s)

        for label, ok, why in (("LCR", lcr_pass, lcr_why),
                               ("H7", h7_pass, h7_why),
                               ("stage", stage_pass, stage_why)):
            if why:
                self.logger.info("health %-5s %s — %s", label,
                                 "PASS" if ok else "FAIL", why)

        operator_io.banner_health(
            lcr_pass, lcr_n, h7_pass, h7_n, HEALTH_TIMEOUT_S,
            stage_pass=stage_pass, stage_n=stage_n)

        all_pass = ((lcr_pass is not False) and (h7_pass is not False)
                    and (stage_pass is not False))
        if all_pass:
            return True

        if lcr_pass is False:
            self.errors.append(f"LCR health failure: {lcr_why}")
        if h7_pass is False:
            self.errors.append(f"H7 health failure: {h7_why}")
        if stage_pass is False:
            self.errors.append(f"Stage health failure: {stage_why}")
        self._record_abort("health_check")
        return False

    def _assess_lcr(self, need: bool, n: int, samples: list):
        """(pass, reason) for the LCR: identity + count + finite readings."""
        if not need:
            return True, ""
        if self.lcr_worker.error is not None:
            return False, f"worker error {self.lcr_worker.error!r}"
        if n < HEALTH_MIN_LCR:
            return False, f"only {n} samples (need ≥{HEALTH_MIN_LCR})"
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
        """(pass, reason) for the H7: combined-firmware identity + per-channel
        readings finite and within the 0-5 V range."""
        if not need:
            return True, ""
        if self.h7_worker.error is not None:
            return False, f"worker error {self.h7_worker.error!r}"
        if n < HEALTH_MIN_H7:
            return False, f"only {n} samples (need ≥{HEALTH_MIN_H7})"
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
                self.logger.warning(
                    "HEALTH: %s reads a constant %.4f V — verify the sensor "
                    "is live and the cable is seated", chname, vals[0])
        return True, f"combined firmware, {n} samples"

    def _assess_stage(self, need: bool, n: int, samples: list):
        """(None if not needed, else (pass, reason)) for the Zaber stage."""
        if not need:
            return None, ""
        if self.stage_worker.error is not None:
            return False, f"worker error {self.stage_worker.error!r}"
        if n < HEALTH_MIN_STAGE:
            return False, f"only {n} reads (need ≥{HEALTH_MIN_STAGE})"
        pos = [s.position_mm for s in samples if math.isfinite(s.position_mm)]
        if not pos:
            return False, "no finite position reads"
        lo, hi = self.cfg.stage.limits_tuple()
        if min(pos) < lo - 1.0 or max(pos) > hi + 1.0:
            return False, (f"position {min(pos):.2f}..{max(pos):.2f} mm "
                           f"outside limits [{lo}, {hi}]")
        return True, f"{n} reads, pos≈{pos[-1]:.2f} mm"

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

    @staticmethod
    def _collect_drain(q: "Optional[queue.Queue[Any]]", bucket: list,
                       cap: int = 300) -> int:
        """Drain q like _discard_drain, but also append up to `cap` items to
        `bucket` so the startup check can inspect actual sample VALUES."""
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

    def _check_stream_health(self, sma_active: bool) -> "tuple[bool, str]":
        """Mid-run liveness monitor (per-instrument staleness).

        Warns LOUDLY (other streams keep running) when an instrument that
        should be streaming goes >HEALTH_WARN_STALE_S with no new sample, and
        returns ok=False at >HEALTH_ABORT_STALE_S so the caller disarms + ends
        the phase. Staleness is measured by diffing each worker's n_pushed.
        """
        now = time.monotonic()
        targets = []
        if self.lcr_worker is not None and self.lcr_queue is not None:
            targets.append(("LCR", self.lcr_worker))
        if self.h7_worker is not None and self.h7_queue is not None:
            # H7 streams continuously only if laser/load are enabled; an
            # SMA-only channel set is legitimately quiet until a cycle runs.
            ch = set(self.cfg.h7.channels or [])
            if (ch & {"laser", "load"}) or sma_active:
                targets.append(("H7", self.h7_worker))
        if self._stage_active and self.stage_worker is not None:
            targets.append(("stage", self.stage_worker))

        for name, w in targets:
            n = w.n_pushed
            if n > self._hc_last_n.get(name, -1):
                self._hc_last_n[name] = n
                self._hc_last_adv[name] = now
                continue
            stale = now - self._hc_last_adv.get(name, now)
            if stale >= HEALTH_ABORT_STALE_S:
                return False, f"{name} no samples for {stale:.1f}s (>{HEALTH_ABORT_STALE_S:.0f}s)"
            if stale >= HEALTH_WARN_STALE_S and (
                    now - self._hc_last_warn.get(name, 0.0) >= 1.0):
                self._hc_last_warn[name] = now
                self.logger.warning(
                    "HEALTH WARNING: %s silent %.1fs — no new samples "
                    "(other instruments still recording)", name, stale)
        return True, ""

    # ------------------------------------------------------------------
    # Phase loop
    # ------------------------------------------------------------------
    def _run_phase(self, phase: str) -> bool:
        self._step(f"phase_{phase}_prompt")
        info = PHASE_PROMPTS[phase]
        options = [
            (operator_io.KEY_ENTER, "Enter",
             "arm recording" if phase == "raw" else "start recording"),
            (operator_io.KEY_ESC, "Esc", "abort session"),
        ]
        try:
            key = operator_io.prompt(info["title"], info["body"], options)
        except KeyboardInterrupt:
            self._record_abort(phase)
            return False
        if key == operator_io.KEY_ESC:
            self._record_abort(phase)
            return False

        attempt = 0
        while True:
            self._step(f"phase_{phase}_recording")
            result = self._record_phase(phase, attempt)

            if result in (_RESULT_ABORT_CRASH, _RESULT_ABORT_USER,
                          _RESULT_ABORT_HEALTH):
                self._record_abort(phase)
                return False

            if phase == "raw":
                return True

            self._step(f"phase_{phase}_confirm")
            pm = self.phase_meta[phase]
            confirm_body = (
                f"Recorded {pm.lcr_n} LCR / {pm.h7_n} H7 / {pm.stage_n} stage "
                f"samples in {pm.duration_s:.2f} s.")
            try:
                key = operator_io.prompt(
                    f"{info['title']} — confirm", confirm_body,
                    [
                        (operator_io.KEY_ENTER, "Enter", "keep & continue"),
                        (operator_io.KEY_SPACE, "Space",
                         "redo this phase (overwrites the files above)"),
                        (operator_io.KEY_ESC, "Esc", "abort session"),
                    ])
            except KeyboardInterrupt:
                self._record_abort(phase)
                return False
            if key == operator_io.KEY_ENTER:
                return True
            if key == operator_io.KEY_SPACE:
                attempt += 1
                self.logger.info("Redo phase '%s' (attempt %d)", phase, attempt + 1)
                continue
            self._record_abort(phase)
            return False

    # ------------------------------------------------------------------
    # Recording — drain queues to per-phase CSVs
    # ------------------------------------------------------------------
    def _record_phase(self, phase: str, attempt: int) -> str:
        if phase == "raw":
            duration_s: Optional[float] = None
            lcr_target = h7_target = 0
            operator_io.banner_ready(
                self.lcr_worker.n_pushed if self.lcr_worker else 0,
                self.h7_worker.n_pushed if self.h7_worker else 0)
        else:
            duration_s = (self.cfg.phases.open_duration_s if phase == "open"
                          else self.cfg.phases.short_duration_s)
            poll = max(self.cfg.lcr.poll_interval_s, 1e-3)
            lcr_target = int(round(duration_s / poll))
            h7_target = int(round(duration_s * 800))   # ~laser+load at 400 SPS

        progress = operator_io.PhaseProgress(
            phase_name=phase, duration_s=duration_s,
            lcr_target=lcr_target, h7_target=h7_target)

        # Drop pre-phase backlog so the first CSV row follows the operator's
        # Enter press.
        self._discard_drain(self.lcr_queue)
        self._discard_drain(self.h7_queue)
        self._discard_drain(self.stage_queue)

        started_at_wall = time.time()
        started_at_mono = time.monotonic()
        lcr_n = h7_n = stage_n = 0
        flush_counter = 0
        outcome = _RESULT_COMPLETE

        # Open whichever stream files are active (ExitStack closes them all
        # on normal exit OR if an open() raises mid-way).
        try:
            with contextlib.ExitStack() as stack:
                lcr_w = h7_w = stage_w = None
                lcr_f = h7_f = stage_f = None
                if self.lcr_queue is not None:
                    lcr_f = stack.enter_context(
                        open(self.paths.lcr_csv(phase), "w", newline=""))
                    lcr_w = csv.writer(lcr_f)
                    lcr_w.writerow(["host_timestamp_s", "monotonic_s",
                                    "primary", "secondary", "status"])
                if self.h7_queue is not None:
                    h7_f = stack.enter_context(
                        open(self.paths.h7_csv(phase), "w", newline=""))
                    h7_w = csv.writer(h7_f)
                    h7_w.writerow(["host_timestamp_s", "monotonic_s",
                                   "firmware_timestamp_us", "src", "channel",
                                   "value", "raw_code", "hw_us", "seq"])
                if self._stage_active:
                    stage_f = stack.enter_context(
                        open(self.paths.stage_csv(phase), "w", newline=""))
                    stage_w = csv.writer(stage_f)
                    stage_w.writerow(
                        ["host_timestamp_s", "monotonic_s", "position_mm"])
                status_w = None
                status_f = None
                if self.status_queue is not None:
                    status_f = stack.enter_context(
                        open(self.paths.status_csv(phase), "w", newline=""))
                    status_w = csv.writer(status_f)
                    status_w.writerow(
                        ["host_timestamp_s", "monotonic_s", "fields_json"])

                # SMA cyclic actuation: PC sends params + 1 Hz heartbeat;
                # M7 owns the timing. Only during RAW, only if enabled.
                sma_drive = (phase == "raw" and self.cfg.sma.enabled)
                last_ping_mono = time.monotonic()
                self._hc_last_n = {}        # per-instrument staleness tracking,
                self._hc_last_adv = {}      #   reset fresh for each phase
                self._hc_last_warn = {}
                if sma_drive:
                    self._sma_start_cycle()

                while True:
                    lcr_n += self._drain_lcr_to(lcr_w)
                    h7_n += self._drain_h7_to(h7_w)
                    stage_n += self._drain_stage_to(stage_w)
                    self._drain_status_to(status_w)

                    elapsed = time.monotonic() - started_at_mono

                    if self._any_worker_error():
                        self.logger.error("Worker crashed during phase '%s'", phase)
                        self.errors.append(
                            f"worker_crash_in_{phase}: "
                            f"lcr={getattr(self.lcr_worker,'error',None)!r}, "
                            f"h7={getattr(self.h7_worker,'error',None)!r}, "
                            f"stage={getattr(self.stage_worker,'error',None)!r}")
                        outcome = _RESULT_ABORT_CRASH
                        break

                    hc_ok, hc_reason = self._check_stream_health(sma_drive)
                    if not hc_ok:
                        self.logger.error("HEALTH ABORT during '%s': %s", phase, hc_reason)
                        self.errors.append(f"health_stale_in_{phase}: {hc_reason}")
                        if sma_drive:
                            self._sma_send(h7.disarm())   # safety: open the return path now
                        outcome = _RESULT_ABORT_HEALTH
                        break

                    if duration_s is not None and elapsed >= duration_s:
                        outcome = _RESULT_COMPLETE
                        break

                    if self.stop_event.is_set():
                        outcome = (_RESULT_COMPLETE if phase == "raw"
                                   else _RESULT_ABORT_USER)
                        break

                    if sma_drive and (time.monotonic() - last_ping_mono) >= 1.0:
                        self._sma_send("ping")          # heartbeat (watchdog)
                        last_ping_mono = time.monotonic()

                    progress.update(elapsed, lcr_n, h7_n, stage_n)
                    flush_counter += 1
                    if flush_counter % 20 == 0:
                        if lcr_w: lcr_f.flush()
                        if h7_w: h7_f.flush()
                        if stage_w: stage_f.flush()
                        if status_w: status_f.flush()
                    time.sleep(DRAIN_TICK_S)

                # Stop the SMA cycle (the M7 watchdog also safe-stops if the
                # host crashes and the heartbeat lapses).
                if sma_drive:
                    self._sma_stop()

                # Final drain.
                lcr_n += self._drain_lcr_to(lcr_w)
                h7_n += self._drain_h7_to(h7_w)
                stage_n += self._drain_stage_to(stage_w)
                self._drain_status_to(status_w)
        except OSError as e:
            self.logger.exception("File I/O error during phase '%s': %s", phase, e)
            self.errors.append(f"file_io_in_{phase}: {e}")
            outcome = _RESULT_ABORT_CRASH

        ended_at_wall = time.time()
        progress.finalize(lcr_n, h7_n, ended_at_wall - started_at_wall, stage_n)

        self.phase_meta[phase] = PhaseMeta(
            duration_s=ended_at_wall - started_at_wall,
            target_duration_s=duration_s,
            lcr_n=lcr_n, h7_n=h7_n, stage_n=stage_n,
            redos=attempt,
            started_at_utc=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at_wall)),
            ended_at_utc=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended_at_wall)),
        )
        return outcome

    # ------------------------------------------------------------------
    # Per-row CSV writers
    # ------------------------------------------------------------------
    def _drain_lcr_to(self, writer: Any) -> int:
        if writer is None or self.lcr_queue is None:
            return 0
        n = 0
        try:
            while True:
                s: LcrSample = self.lcr_queue.get_nowait()
                writer.writerow([
                    f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                    f"{s.primary:.8e}", f"{s.secondary:.8f}", s.status,
                ])
                n += 1
        except queue.Empty:
            pass
        return n

    def _drain_h7_to(self, writer: Any) -> int:
        if writer is None or self.h7_queue is None:
            return 0
        n = 0
        try:
            while True:
                s: H7Sample = self.h7_queue.get_nowait()
                writer.writerow([
                    f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                    s.firmware_timestamp_us,
                    s.src if s.src is not None else "",
                    s.channel if s.channel is not None else "",
                    f"{s.value:.8f}",
                    s.raw_code if s.raw_code is not None else "",
                    s.hw_us if s.hw_us is not None else "",
                    s.seq if s.seq is not None else "",
                ])
                n += 1
        except queue.Empty:
            pass
        return n

    def _drain_stage_to(self, writer: Any) -> int:
        if writer is None or self.stage_queue is None:
            return 0
        n = 0
        try:
            while True:
                s: StageSample = self.stage_queue.get_nowait()
                writer.writerow([
                    f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                    f"{s.position_mm:.6f}",
                ])
                n += 1
        except queue.Empty:
            pass
        return n

    def _drain_status_to(self, writer: Any) -> int:
        if writer is None or self.status_queue is None:
            return 0
        n = 0
        try:
            while True:
                s = self.status_queue.get_nowait()
                writer.writerow([
                    f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                    json.dumps(s.fields, separators=(",", ":")),
                ])
                n += 1
        except queue.Empty:
            pass
        return n

    # ------------------------------------------------------------------
    # SMA cyclic actuation control (PC sends params + heartbeat only;
    # the M7 firmware owns all phase timing).
    # ------------------------------------------------------------------
    def _sma_send(self, cmd: str) -> bool:
        """Send one command to the H7 over the H7Worker's reader. Safe no-op
        if H7 is disabled or the reader hasn't opened yet."""
        reader = getattr(self.h7_worker, "reader", None)
        if reader is None:
            return False
        try:
            reader.send_command(cmd)
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.warning("SMA send_command(%r) failed: %s", cmd, e)
            return False

    def _sma_start_cycle(self) -> None:
        sma = self.cfg.sma
        # Arm (close the MOSFET return path) BEFORE any actuation — the
        # rebuilt firmware rejects drive/fire/cycle while disarmed.
        self._sma_send(h7.arm())
        self._sma_send(h7.wdt(sma.wdt_ms))
        if self._sma_send(sma.cycle_command()):
            self.logger.info("SMA armed + cycle started: %s (wdt=%d ms)",
                             sma.cycle_command(), sma.wdt_ms)
        else:
            self.errors.append("sma_cycle_start_failed: H7 reader unavailable")
            self.logger.warning("Could not start SMA cycle — H7 reader unavailable")

    def _sma_stop(self) -> None:
        # Graceful stop → idle-low (still armed), then disarm to fully
        # de-energize (open the return path).
        self._sma_send(h7.stop())
        self._sma_send(h7.disarm())
        self.logger.info("SMA cycle stop + disarm sent")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _stop_workers(self) -> None:
        self.stop_event.set()
        for w in (self.lcr_worker, self.h7_worker, self.stage_worker):
            if w is None:
                continue
            w.join(timeout=WORKER_JOIN_TIMEOUT_S)
            if w.is_alive():
                self.logger.warning("%s did not stop within %.1f s",
                                    w.name, WORKER_JOIN_TIMEOUT_S)

    # ------------------------------------------------------------------
    # meta.json writer
    # ------------------------------------------------------------------
    def _write_meta(self) -> None:
        meta: dict[str, Any] = {
            "session_id": self.paths.session_id,
            "schema": "sma_v3",
            "started_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.session_started_at)),
            "ended_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(self.session_ended_at or time.time())),
            "completed": self.completed,
            "aborted_at_phase": self.aborted_at_phase,
            "last_functional_step": self.last_functional_step,
            "errors": self.errors,
            "phases": {k: asdict(v) for k, v in self.phase_meta.items()},
            "phases_config": asdict(self.cfg.phases),
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
        self.logger.info("Wrote %s", self.paths.meta_json)
