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
import platform
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import operator_io
from config import AppConfig
from workers import (H7Sample, H7Worker, LcrSample, LcrWorker,
                     StageSample, ZaberWorker)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
HEALTH_TIMEOUT_S = 10.0
HEALTH_MIN_LCR = 5
HEALTH_MIN_H7 = 20
HEALTH_MIN_STAGE = 3
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
                 stop_event: threading.Event):
        self.cfg = cfg
        self.paths = paths
        self.lcr_worker = lcr_worker
        self.h7_worker = h7_worker
        self.stage_worker = stage_worker
        self.lcr_queue = lcr_queue
        self.h7_queue = h7_queue
        self.stage_queue = stage_queue
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
        self._step("health_check")
        self.logger.info("Running %.1f s health check...", HEALTH_TIMEOUT_S)
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        lcr_n = h7_n = stage_n = 0
        need_lcr = self.lcr_queue is not None
        need_h7 = self.h7_queue is not None
        need_stage = self._stage_active
        while time.monotonic() < deadline:
            if need_lcr:
                lcr_n += self._discard_drain(self.lcr_queue)
            if need_h7:
                h7_n += self._discard_drain(self.h7_queue)
            if need_stage:
                stage_n += self._discard_drain(self.stage_queue)
            done = ((not need_lcr or lcr_n >= HEALTH_MIN_LCR)
                    and (not need_h7 or h7_n >= HEALTH_MIN_H7)
                    and (not need_stage or stage_n >= HEALTH_MIN_STAGE))
            if done or self._any_worker_error():
                break
            time.sleep(DRAIN_TICK_S)
        if need_lcr:
            lcr_n += self._discard_drain(self.lcr_queue)
        if need_h7:
            h7_n += self._discard_drain(self.h7_queue)
        if need_stage:
            stage_n += self._discard_drain(self.stage_queue)

        lcr_pass = (not need_lcr) or (
            lcr_n >= HEALTH_MIN_LCR and self.lcr_worker.error is None)
        h7_pass = (not need_h7) or (
            h7_n >= HEALTH_MIN_H7 and self.h7_worker.error is None)
        stage_pass: Optional[bool] = None
        if need_stage:
            stage_pass = (stage_n >= HEALTH_MIN_STAGE
                          and self.stage_worker.error is None)

        operator_io.banner_health(
            lcr_pass, lcr_n, h7_pass, h7_n, HEALTH_TIMEOUT_S,
            stage_pass=stage_pass, stage_n=stage_n)

        all_pass = lcr_pass and h7_pass and (stage_pass is not False)
        if all_pass:
            return True

        if need_lcr and not lcr_pass:
            err = f"LCR health failure: n={lcr_n} (need ≥{HEALTH_MIN_LCR})"
            if self.lcr_worker.error:
                err += f"; worker_error={self.lcr_worker.error!r}"
            self.errors.append(err)
        if need_h7 and not h7_pass:
            err = f"H7 health failure: n={h7_n} (need ≥{HEALTH_MIN_H7})"
            if self.h7_worker.error:
                err += f"; worker_error={self.h7_worker.error!r}"
            self.errors.append(err)
        if need_stage and not stage_pass:
            err = f"Stage health failure: n={stage_n} (need ≥{HEALTH_MIN_STAGE})"
            if self.stage_worker.error:
                err += f"; worker_error={self.stage_worker.error!r}"
            self.errors.append(err)
        self._record_abort("health_check")
        return False

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

            if result in (_RESULT_ABORT_CRASH, _RESULT_ABORT_USER):
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

                # SMA cyclic actuation: PC sends params + 1 Hz heartbeat;
                # M7 owns the timing. Only during RAW, only if enabled.
                sma_drive = (phase == "raw" and self.cfg.sma.enabled)
                last_ping_mono = time.monotonic()
                if sma_drive:
                    self._sma_start_cycle()

                while True:
                    lcr_n += self._drain_lcr_to(lcr_w)
                    h7_n += self._drain_h7_to(h7_w)
                    stage_n += self._drain_stage_to(stage_w)

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
                    time.sleep(DRAIN_TICK_S)

                # Stop the SMA cycle (the M7 watchdog also safe-stops if the
                # host crashes and the heartbeat lapses).
                if sma_drive:
                    self._sma_stop()

                # Final drain.
                lcr_n += self._drain_lcr_to(lcr_w)
                h7_n += self._drain_h7_to(h7_w)
                stage_n += self._drain_stage_to(stage_w)
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
        self._sma_send(f"wdt {int(sma.wdt_ms)}")
        if self._sma_send(sma.cycle_command()):
            self.logger.info("SMA cycle started: %s (wdt=%d ms)",
                             sma.cycle_command(), sma.wdt_ms)
        else:
            self.errors.append("sma_cycle_start_failed: H7 reader unavailable")
            self.logger.warning("Could not start SMA cycle — H7 reader unavailable")

    def _sma_stop(self) -> None:
        self._sma_send("stop")
        self.logger.info("SMA cycle stop sent")

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
