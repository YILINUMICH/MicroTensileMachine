"""
session.py — state machine controller for an SMA characterization session.

A single session walks the operator through three phases:
  1. OPEN  — DUT disconnected, fixed duration (e.g. 20 s)
  2. SHORT — DUT shorted at the bias-tee end, fixed duration
  3. RAW   — actual experiment, runs until Ctrl+C

Workers stream continuously across all three phases. The controller is
the only file writer: it drains both worker queues into per-phase CSV
files, swapping the active files at phase boundaries. An interactive
prompt between phases lets the operator [Enter] continue, [Space] redo
the previous phase (overwriting its files), or [Esc] abort.

On any failure the controller records the last functional step in
meta.json so the operator knows where to troubleshoot.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

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
from workers import H7Sample, H7Worker, LcrSample, LcrWorker


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
HEALTH_TIMEOUT_S = 10.0        # max wait for both streams to come online
HEALTH_MIN_LCR = 5             # min LCR samples within the window
HEALTH_MIN_H7 = 20             # min H7 samples within the window
DRAIN_TICK_S = 0.05            # recording-loop cadence (20 Hz)
WORKER_JOIN_TIMEOUT_S = 5.0    # graceful shutdown ceiling
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


# ---------------------------------------------------------------------------
# Phase metadata (one entry per recorded phase in meta.json)
# ---------------------------------------------------------------------------
@dataclass
class PhaseMeta:
    duration_s: float = 0.0              # actual recorded duration
    target_duration_s: Optional[float] = None  # configured target (None for raw)
    lcr_n: int = 0
    h7_n: int = 0
    redos: int = 0                       # how many times this phase was redone
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
    Owns the phase state machine and meta.json output.

    The caller is responsible for:
      - starting both workers BEFORE calling .run()
      - installing a SIGINT handler that sets stop_event
    """

    def __init__(self,
                 cfg: AppConfig,
                 paths: SessionPaths,
                 lcr_worker: LcrWorker,
                 h7_worker: H7Worker,
                 lcr_queue: "queue.Queue[LcrSample]",
                 h7_queue: "queue.Queue[H7Sample]",
                 stop_event: threading.Event):
        self.cfg = cfg
        self.paths = paths
        self.lcr_worker = lcr_worker
        self.h7_worker = h7_worker
        self.lcr_queue = lcr_queue
        self.h7_queue = h7_queue
        self.stop_event = stop_event

        # Session state
        self.last_functional_step: str = "init"
        self.phase_meta: dict[str, PhaseMeta] = {}
        self.completed: bool = False
        self.aborted_at_phase: Optional[str] = None
        self.errors: list[str] = []
        self.session_started_at: float = time.time()
        self.session_ended_at: Optional[float] = None

        self.logger = logging.getLogger("SessionController")

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def run(self) -> int:
        """
        Run the OPEN → SHORT → RAW state machine. Returns a process exit
        code: 0 = completed, 1 = operator abort, 2 = system error.
        """
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
            # Ctrl+C during a prompt
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
                self.paths.session_id,
                self.completed,
                self.aborted_at_phase,
            )

    # ------------------------------------------------------------------
    # Step tracking + abort accounting
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
        """Wait up to HEALTH_TIMEOUT_S for both streams to produce data."""
        self._step("health_check")
        self.logger.info("Running %.1f s health check...", HEALTH_TIMEOUT_S)
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        lcr_n = 0
        h7_n = 0
        while time.monotonic() < deadline:
            lcr_n += self._discard_drain(self.lcr_queue)
            h7_n += self._discard_drain(self.h7_queue)
            if lcr_n >= HEALTH_MIN_LCR and h7_n >= HEALTH_MIN_H7:
                break
            if self.lcr_worker.error or self.h7_worker.error:
                break
            time.sleep(DRAIN_TICK_S)
        # Final drain — capture anything that arrived in the last tick.
        lcr_n += self._discard_drain(self.lcr_queue)
        h7_n += self._discard_drain(self.h7_queue)

        lcr_pass = (lcr_n >= HEALTH_MIN_LCR
                    and self.lcr_worker.error is None)
        h7_pass = (h7_n >= HEALTH_MIN_H7
                   and self.h7_worker.error is None)

        operator_io.banner_health(
            lcr_pass, lcr_n, h7_pass, h7_n, HEALTH_TIMEOUT_S)

        if lcr_pass and h7_pass:
            return True

        # Surface specific failures into the meta.json error list.
        if not lcr_pass:
            err = (f"LCR health failure: n={lcr_n} "
                   f"(need ≥{HEALTH_MIN_LCR})")
            if self.lcr_worker.error:
                err += f"; worker_error={self.lcr_worker.error!r}"
            self.errors.append(err)
        if not h7_pass:
            err = (f"H7 health failure: n={h7_n} "
                   f"(need ≥{HEALTH_MIN_H7})")
            if self.h7_worker.error:
                err += f"; worker_error={self.h7_worker.error!r}"
            self.errors.append(err)
        self._record_abort("health_check")
        return False

    @staticmethod
    def _discard_drain(q: "queue.Queue[Any]") -> int:
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
        """
        Run a single phase end-to-end: pre-prompt, recording (with redo
        loop for OPEN/SHORT), confirmation prompt.

        Returns True if the phase completed and the operator confirmed
        (or, for RAW, recording finished cleanly). Returns False on
        operator abort or any unrecoverable error. The error case sets
        self.aborted_at_phase.
        """
        # ----- pre-phase prompt -----
        self._step(f"phase_{phase}_prompt")
        info = PHASE_PROMPTS[phase]
        if phase == "raw":
            options = [
                (operator_io.KEY_ENTER, "Enter", "arm recording"),
                (operator_io.KEY_ESC,   "Esc",   "abort session"),
            ]
        else:
            options = [
                (operator_io.KEY_ENTER, "Enter", "start recording"),
                (operator_io.KEY_ESC,   "Esc",   "abort session"),
            ]
        try:
            key = operator_io.prompt(info["title"], info["body"], options)
        except KeyboardInterrupt:
            self._record_abort(phase)
            return False
        if key == operator_io.KEY_ESC:
            self._record_abort(phase)
            return False

        # ----- recording (with redo loop for open/short) -----
        attempt = 0
        while True:
            self._step(f"phase_{phase}_recording")
            result = self._record_phase(phase, attempt)

            if result == _RESULT_ABORT_CRASH:
                self._record_abort(phase)
                return False
            if result == _RESULT_ABORT_USER:
                # SIGINT during open/short → abort session
                self._record_abort(phase)
                return False
            # _RESULT_COMPLETE — normal duration reached, or for RAW
            # SIGINT received as the standard stop signal.

            if phase == "raw":
                # No confirmation prompt for RAW — Ctrl+C is the canonical
                # stop and we always keep the data.
                return True

            # ----- confirmation for open/short -----
            self._step(f"phase_{phase}_confirm")
            pm = self.phase_meta[phase]
            confirm_body = (f"Recorded {pm.lcr_n} LCR / {pm.h7_n} H7 samples "
                            f"in {pm.duration_s:.2f} s.")
            try:
                key = operator_io.prompt(
                    f"{info['title']} — confirm",
                    confirm_body,
                    [
                        (operator_io.KEY_ENTER, "Enter", "keep & continue"),
                        (operator_io.KEY_SPACE, "Space",
                         "redo this phase (overwrites the files above)"),
                        (operator_io.KEY_ESC,   "Esc",   "abort session"),
                    ],
                )
            except KeyboardInterrupt:
                self._record_abort(phase)
                return False

            if key == operator_io.KEY_ENTER:
                return True
            if key == operator_io.KEY_SPACE:
                attempt += 1
                self.logger.info("Redoing phase '%s' (attempt %d)",
                                 phase, attempt + 1)
                continue
            # Esc
            self._record_abort(phase)
            return False

    # ------------------------------------------------------------------
    # Recording — drain queues to per-phase CSVs
    # ------------------------------------------------------------------
    def _record_phase(self, phase: str, attempt: int) -> str:
        """
        Drain both queues into the phase's CSV files. Returns one of:
          _RESULT_COMPLETE     — duration reached (or, for raw, SIGINT)
          _RESULT_ABORT_USER   — SIGINT during open/short
          _RESULT_ABORT_CRASH  — worker error or file IO error
        """
        # --- duration / targets ---
        if phase == "raw":
            duration_s: Optional[float] = None
            lcr_target = 0
            h7_target = 0
            # READY banner before the timer starts so the operator knows
            # exactly when to apply current.
            operator_io.banner_ready(self.lcr_worker.n_pushed,
                                     self.h7_worker.n_pushed)
        else:
            duration_s = (self.cfg.phases.open_duration_s
                          if phase == "open"
                          else self.cfg.phases.short_duration_s)
            poll = max(self.cfg.lcr.poll_interval_s, 1e-3)
            lcr_target = int(round(duration_s / poll))
            h7_target = int(round(duration_s * 400))   # rough — H7 ~400 SPS

        progress = operator_io.PhaseProgress(
            phase_name=phase, duration_s=duration_s,
            lcr_target=lcr_target, h7_target=h7_target)

        # --- discard pre-phase backlog ---
        # Workers have been streaming during the prompt; drop those samples
        # so the phase's first CSV row corresponds to a moment after the
        # operator pressed Enter. A few-ms gap is acceptable.
        self._discard_drain(self.lcr_queue)
        self._discard_drain(self.h7_queue)

        lcr_path = self.paths.lcr_csv(phase)
        h7_path = self.paths.h7_csv(phase)

        started_at_wall = time.time()
        started_at_mono = time.monotonic()
        lcr_n = 0
        h7_n = 0
        flush_counter = 0
        outcome = _RESULT_COMPLETE

        try:
            # 'w' mode truncates — important for the redo path.
            with open(lcr_path, "w", newline="") as lcr_f, \
                 open(h7_path,  "w", newline="") as h7_f:
                lcr_w = csv.writer(lcr_f)
                h7_w = csv.writer(h7_f)
                lcr_w.writerow([
                    "host_timestamp_s", "monotonic_s",
                    "primary", "secondary", "status",
                ])
                h7_w.writerow([
                    "host_timestamp_s", "monotonic_s",
                    "firmware_timestamp_us", "voltage_V", "raw_code",
                ])

                while True:
                    # Drain both queues
                    lcr_n += self._drain_lcr_to(lcr_w)
                    h7_n += self._drain_h7_to(h7_w)

                    elapsed = time.monotonic() - started_at_mono

                    # Worker crash → abort
                    if (self.lcr_worker.error is not None
                            or self.h7_worker.error is not None):
                        self.logger.error(
                            "Worker crashed during phase '%s' "
                            "(lcr_err=%r, h7_err=%r)",
                            phase, self.lcr_worker.error,
                            self.h7_worker.error)
                        self.errors.append(
                            f"worker_crash_in_{phase}: "
                            f"lcr={self.lcr_worker.error!r}, "
                            f"h7={self.h7_worker.error!r}")
                        outcome = _RESULT_ABORT_CRASH
                        break

                    # Duration reached (open/short)
                    if duration_s is not None and elapsed >= duration_s:
                        outcome = _RESULT_COMPLETE
                        break

                    # SIGINT (stop_event set)
                    if self.stop_event.is_set():
                        if phase == "raw":
                            # Normal stop for RAW
                            outcome = _RESULT_COMPLETE
                        else:
                            outcome = _RESULT_ABORT_USER
                        # Clear stop_event so we can still finalize
                        # workers cleanly later. The worker stop signal
                        # is re-asserted in _stop_workers().
                        # NOTE: we leave stop_event set here so the
                        # recording loop exits; _stop_workers() will
                        # ensure it stays set for shutdown.
                        break

                    # Progress update + periodic flush
                    progress.update(elapsed, lcr_n, h7_n)
                    flush_counter += 1
                    if flush_counter % 20 == 0:    # ~1 Hz at 50 ms tick
                        lcr_f.flush()
                        h7_f.flush()
                    time.sleep(DRAIN_TICK_S)

                # Final drain — pick up anything still in the queues.
                lcr_n += self._drain_lcr_to(lcr_w)
                h7_n += self._drain_h7_to(h7_w)
                lcr_f.flush()
                h7_f.flush()
        except OSError as e:
            self.logger.exception("File I/O error during phase '%s': %s",
                                  phase, e)
            self.errors.append(f"file_io_in_{phase}: {e}")
            outcome = _RESULT_ABORT_CRASH

        ended_at_wall = time.time()
        progress.finalize(lcr_n, h7_n, ended_at_wall - started_at_wall)

        # Always record what we got — even on abort, partial data is
        # useful for diagnostics.
        self.phase_meta[phase] = PhaseMeta(
            duration_s=ended_at_wall - started_at_wall,
            target_duration_s=duration_s,
            lcr_n=lcr_n,
            h7_n=h7_n,
            redos=attempt,
            started_at_utc=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at_wall)),
            ended_at_utc=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended_at_wall)),
        )
        return outcome

    # ------------------------------------------------------------------
    # Per-row CSV writers (pull from queue, write, count)
    # ------------------------------------------------------------------
    def _drain_lcr_to(self, writer: Any) -> int:
        n = 0
        try:
            while True:
                s: LcrSample = self.lcr_queue.get_nowait()
                writer.writerow([
                    f"{s.host_timestamp_s:.6f}",
                    f"{s.monotonic_s:.6f}",
                    f"{s.primary:.8e}",
                    f"{s.secondary:.8f}",
                    s.status,
                ])
                n += 1
        except queue.Empty:
            pass
        return n

    def _drain_h7_to(self, writer: Any) -> int:
        n = 0
        try:
            while True:
                s: H7Sample = self.h7_queue.get_nowait()
                writer.writerow([
                    f"{s.host_timestamp_s:.6f}",
                    f"{s.monotonic_s:.6f}",
                    s.firmware_timestamp_us,
                    f"{s.voltage_V:.8f}",
                    s.raw_code if s.raw_code is not None else "",
                ])
                n += 1
        except queue.Empty:
            pass
        return n

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _stop_workers(self) -> None:
        self.stop_event.set()
        self.lcr_worker.join(timeout=WORKER_JOIN_TIMEOUT_S)
        self.h7_worker.join(timeout=WORKER_JOIN_TIMEOUT_S)
        if self.lcr_worker.is_alive():
            self.logger.warning("LCR worker did not stop within %.1f s",
                                WORKER_JOIN_TIMEOUT_S)
        if self.h7_worker.is_alive():
            self.logger.warning("H7 worker did not stop within %.1f s",
                                WORKER_JOIN_TIMEOUT_S)

    # ------------------------------------------------------------------
    # meta.json writer
    # ------------------------------------------------------------------
    def _write_meta(self) -> None:
        meta: dict[str, Any] = {
            "session_id": self.paths.session_id,
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
                "idn": self.lcr_worker.idn,
                "n_dropped": self.lcr_worker.n_dropped,
            },
            "h7": {
                **asdict(self.cfg.h7),
                "n_dropped": self.h7_worker.n_dropped,
            },
            "run": asdict(self.cfg.run),
            "host": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
            },
            "laser_calibration_reference": {
                "k_mV_per_um": -0.1171,
                "V0_mV": 566.957,
                "source": "Calibrate_LaserHead/data/2026-04-24_run07_*",
                "conversion": "displacement_um = (V_mV - V0_mV) / k",
                "note": ("Applied in analyze_sma.py, not at record time. "
                         "Override with --k / --v0 if recalibrated."),
            },
        }
        with open(self.paths.meta_json, "w") as f:
            json.dump(meta, f, indent=2)
        self.logger.info("Wrote %s", self.paths.meta_json)
