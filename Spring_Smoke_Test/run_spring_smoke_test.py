#!/usr/bin/env python3
"""
run_spring_smoke_test.py — Phase 5 spring-as-SMA-surrogate smoke test runner.

Executes the six-step procedure from
``doc/PLAN_phase5_spring_smoke_test.md`` against the rig:

  Step 1 — Static noise floor (stage idle, 60 s)
  Step 2 — Quasi-static ramp (slow constant-velocity sweep through stroke)
  Step 3 — Step-and-hold (jump position, log settling)
  Step 4 — Fast pull / slow return (asymmetric profile, sync diagnostic)
  Step 5 — Pipeline endurance (stage idle, 10 min, watches for drops/jitter)
  Step 6 — Profile + concurrent Zaber comms (step-4 motion at 1 kSPS)

Each step is selectable via ``--step``. The default ``--step all`` runs
1–5 (step 6 is opt-in because it requires 1 kSPS firmware already loaded
AND assumes step 4 has passed).

Outputs are written to ``data/<YYYY-MM-DD>_run<NN>/``:

  - ``samples_stepN.csv`` — every captured Sample as a row
  - ``status_stepN.csv``  — every M7 [STATUS] frame seen during the step
  - ``stage_log_stepN.csv`` — Zaber position polled at ~50 Hz throughout
  - ``meta.json``         — run-wide metadata (firmware build, cal refs,
                            spring k, all step results, host info)
  - ``run.log``           — runner log

The test does NOT assume any particular firmware sample rate; the value
of ``--sample-rate`` is recorded in meta.json and used by analyze.py for
header annotation, but the runner captures whatever the firmware actually
emits.

Pre-tensioning of the spring was deliberately not added — the stage zero
is at absolute 10 mm with the spring slack, and analyze.py identifies
the engagement knee from the F-vs-x curve.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

from portenta_reader import PortentaReader, Sample, StatusFrame

# Make the sibling ZaberStage package importable without requiring an install.
_THIS_DIR = Path(__file__).resolve().parent
_ZABER_DIR = _THIS_DIR.parent / "ZaberStage"
if str(_ZABER_DIR) not in sys.path:
    sys.path.insert(0, str(_ZABER_DIR))

from zaber_stage import ZaberStage  # noqa: E402


# =============================================================================
# Config
# =============================================================================
@dataclass
class StepConfig:
    # Step 1: static noise
    static_duration_s: float = 60.0
    # Step 2: quasi-static ramp
    ramp_start_mm: float = 10.0
    ramp_end_mm: float = 16.0
    ramp_velocity_mm_s: float = 0.1
    # Step 3: step and hold
    step_target_mm: float = 14.0
    step_hold_pre_s: float = 2.0
    step_hold_post_s: float = 5.0
    # Step 4: fast pull / slow return
    profile_base_mm: float = 10.0
    profile_peak_mm: float = 16.0
    profile_pull_velocity_mm_s: float = 3.0
    profile_return_velocity_mm_s: float = 0.1
    profile_peak_hold_s: float = 1.0
    # Step 5: endurance
    endurance_duration_s: float = 600.0
    # Step 6: high-rate profile (repeats step 4 — same params)


@dataclass
class RunConfig:
    # Spring transfer standard
    spring_k_mN_per_mm: float
    spring_k_source: str
    # Reference noise floors (from earlier calibration runs)
    laser_noise_V_ref: Optional[float]    # σ on ADC1 voltage at rest (cal-run)
    load_noise_V_ref: Optional[float]     # σ on ADC2 voltage at rest (cal-run)
    # Sensor cal references
    laser_k_mV_per_um: Optional[float]
    laser_V0_mV: Optional[float]
    load_sensitivity_mV_per_mN: Optional[float]
    load_V0_mV: Optional[float]
    # Firmware annotation (not actively set — operator-loaded)
    sample_rate_sps: int                   # 400 or 1000 — annotation only
    firmware_path: str
    # Serial / stage
    portenta_port: Optional[str]
    portenta_baud: int
    zaber_config_path: str
    zaber_port: Optional[str]
    # Acquisition timing
    stage_poll_hz: float
    drain_timeout_s: float
    # Steps
    steps: StepConfig
    # Metadata
    operator: str
    notes: str

    @classmethod
    def from_yaml(cls, path: Path) -> "RunConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
        steps_raw = raw.pop("steps", {}) or {}
        steps = StepConfig(**{k: v for k, v in steps_raw.items()
                              if k in StepConfig.__dataclass_fields__})
        known = {f for f in cls.__dataclass_fields__} - {"steps"}
        return cls(steps=steps,
                   **{k: v for k, v in raw.items() if k in known})


# =============================================================================
# Run directory layout
# =============================================================================
@dataclass
class RunPaths:
    root: Path
    prefix: str

    def samples_csv(self, step: int) -> Path:
        return self.root / f"samples_step{step}.csv"

    def status_csv(self, step: int) -> Path:
        return self.root / f"status_step{step}.csv"

    def stage_log_csv(self, step: int) -> Path:
        return self.root / f"stage_log_step{step}.csv"

    @property
    def meta_json(self) -> Path:
        return self.root / "meta.json"

    @property
    def run_log(self) -> Path:
        return self.root / "run.log"


def next_run_paths(data_dir: Path) -> RunPaths:
    data_dir.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    n = 1
    while True:
        prefix = f"{today}_run{n:02d}"
        root = data_dir / prefix
        if not root.exists():
            root.mkdir()
            return RunPaths(root=root, prefix=prefix)
        n += 1


# =============================================================================
# CSV writers
# =============================================================================
_SAMPLE_HEADER = [
    "wall_us",       # host time.monotonic_ns() / 1000 at parse
    "step",
    "phase",         # within-step tag: 'static', 'ramp_fwd', 'pull', 'return', etc.
    "fw_t_us",       # firmware t_ms × 1000
    "src",
    "seq",
    "hw_us",
    "raw_code",
    "voltage_V",
    "stage_mm",      # most-recent polled stage position
]

_STATUS_HEADER = [
    "wall_us", "step", "phase",
    "fw_t_ms", "hwm", "dropped",
    "rate1", "rate2", "rate3", "rate4", "rate5",
    "idle_m4_pct", "last_cmd_seq", "extras", "raw",
]

_STAGE_HEADER = ["wall_us", "step", "phase", "stage_mm", "set_vel_mm_s"]


def _sample_row(s: Sample, *, wall_us: int, step: int, phase: str,
                stage_mm: float) -> List:
    return [
        wall_us, step, phase, s.timestamp_us,
        s.adc_source if s.adc_source is not None else "",
        s.seq if s.seq is not None else "",
        s.hw_us if s.hw_us is not None else "",
        s.raw_code if s.raw_code is not None else "",
        f"{s.voltage_V:.8f}",
        f"{stage_mm:.6f}",
    ]


def _status_row(sf: StatusFrame, *, wall_us: int, step: int, phase: str) -> List:
    return [
        wall_us, step, phase,
        sf.t_ms if sf.t_ms is not None else "",
        sf.hwm if sf.hwm is not None else "",
        sf.dropped if sf.dropped is not None else "",
        sf.rates.get(1, ""), sf.rates.get(2, ""), sf.rates.get(3, ""),
        sf.rates.get(4, ""), sf.rates.get(5, ""),
        sf.idle_m4_pct if sf.idle_m4_pct is not None else "",
        sf.last_cmd_seq if sf.last_cmd_seq is not None else "",
        json.dumps(sf.extras) if sf.extras else "",
        sf.raw,
    ]


# =============================================================================
# Streaming capture with concurrent stage polling
# =============================================================================
def stream_with_stage(
    reader: PortentaReader,
    stage: ZaberStage,
    duration_s: float,
    *,
    step: int,
    phase: str,
    samples_w: csv.writer,
    status_w: csv.writer,
    stage_w: csv.writer,
    stage_poll_hz: float,
    set_vel_mm_s: float = 0.0,
    log: Optional[logging.Logger] = None,
    progress_every_s: float = 5.0,
) -> Tuple[int, int]:
    """Capture all samples + status frames for ``duration_s``, while
    polling the Zaber stage at ``stage_poll_hz``. Each sample is tagged
    with the most-recent polled stage position.

    Returns (n_samples, n_status_frames).
    """
    stage_poll_dt = 1.0 / max(1.0, stage_poll_hz)
    log = log or logging.getLogger("stream")

    n_samples = 0
    n_status = 0
    status_start = len(reader.status_frames)

    last_stage_poll = 0.0
    last_progress = time.monotonic()
    cur_pos = stage.get_position()

    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        line = reader._readline()
        if line:
            parsed = reader._consume(line)
            wall_us = int(time.monotonic() * 1e6)
            if parsed is not None:
                samples_w.writerow(
                    _sample_row(parsed, wall_us=wall_us, step=step,
                                phase=phase, stage_mm=cur_pos))
                n_samples += 1

        now = time.monotonic()
        # Stage poll
        if now - last_stage_poll >= stage_poll_dt:
            last_stage_poll = now
            cur_pos = stage.get_position()
            stage_w.writerow([
                int(now * 1e6), step, phase,
                f"{cur_pos:.6f}", f"{set_vel_mm_s:.4f}",
            ])

        # Flush any newly-arrived status frames
        while status_start + n_status < len(reader.status_frames):
            sf = reader.status_frames[status_start + n_status]
            status_w.writerow(
                _status_row(sf, wall_us=int(now * 1e6),
                            step=step, phase=phase))
            n_status += 1

        # Progress
        if now - last_progress >= progress_every_s:
            log.info("  [step %d %s] %.1fs elapsed  samples=%d  status=%d  "
                     "pos=%.3f mm",
                     step, phase, now - (deadline - duration_s),
                     n_samples, n_status, cur_pos)
            last_progress = now

    return n_samples, n_status


def ramp_with_capture(
    reader: PortentaReader,
    stage: ZaberStage,
    *,
    start_mm: float,
    end_mm: float,
    velocity_mm_s: float,
    step: int,
    phase: str,
    samples_w, status_w, stage_w,
    stage_poll_hz: float,
    log: logging.Logger,
    position_epsilon_mm: float = 0.02,
    timeout_s: float = 120.0,
) -> Tuple[int, int]:
    """Constant-velocity ramp from start_mm to end_mm with sample capture.

    The runner is responsible for positioning the stage at start_mm before
    calling. Direction is inferred from end_mm > start_mm. Stops the stage
    when the polled position reaches within ``position_epsilon_mm`` of
    end_mm or after ``timeout_s``.
    """
    forward = end_mm > start_mm
    signed_v = abs(velocity_mm_s) if forward else -abs(velocity_mm_s)
    if not stage.set_velocity(signed_v):
        raise RuntimeError(f"stage.set_velocity({signed_v}) failed")

    stage_poll_dt = 1.0 / max(1.0, stage_poll_hz)
    n_samples = 0
    n_status = 0
    status_start = len(reader.status_frames)
    last_stage_poll = 0.0
    last_progress = time.monotonic()
    cur_pos = stage.get_position()

    t_start = time.monotonic()
    try:
        while True:
            line = reader._readline()
            if line:
                parsed = reader._consume(line)
                if parsed is not None:
                    samples_w.writerow(
                        _sample_row(parsed,
                                    wall_us=int(time.monotonic() * 1e6),
                                    step=step, phase=phase,
                                    stage_mm=cur_pos))
                    n_samples += 1

            now = time.monotonic()
            if now - last_stage_poll >= stage_poll_dt:
                last_stage_poll = now
                cur_pos = stage.get_position()
                stage_w.writerow([
                    int(now * 1e6), step, phase,
                    f"{cur_pos:.6f}", f"{signed_v:.4f}",
                ])
                # Stop conditions
                if forward and cur_pos >= end_mm - position_epsilon_mm:
                    break
                if not forward and cur_pos <= end_mm + position_epsilon_mm:
                    break

            while status_start + n_status < len(reader.status_frames):
                sf = reader.status_frames[status_start + n_status]
                status_w.writerow(
                    _status_row(sf, wall_us=int(time.monotonic() * 1e6),
                                step=step, phase=phase))
                n_status += 1

            if now - last_progress >= 2.0:
                log.info("  [step %d %s] pos=%.3f → %.3f mm  v=%+.3f mm/s  "
                         "samples=%d",
                         step, phase, cur_pos, end_mm, signed_v, n_samples)
                last_progress = now

            if now - t_start > timeout_s:
                log.warning("  ramp timed out after %.1fs (pos=%.3f, "
                            "target=%.3f)", timeout_s, cur_pos, end_mm)
                break
    finally:
        stage.stop()

    # Brief drain to capture trailing samples after stop
    drain_until = time.monotonic() + 0.5
    while time.monotonic() < drain_until:
        line = reader._readline()
        if line:
            parsed = reader._consume(line)
            if parsed is not None:
                samples_w.writerow(
                    _sample_row(parsed,
                                wall_us=int(time.monotonic() * 1e6),
                                step=step, phase=phase + "_drain",
                                stage_mm=stage.get_position()))
                n_samples += 1

    return n_samples, n_status


# =============================================================================
# Step implementations
# =============================================================================
def step1_static_noise(reader, stage, cfg, paths, log) -> Dict:
    log.info("──── STEP 1: static noise floor at %.3f mm for %.0fs ────",
             cfg.steps.ramp_start_mm, cfg.steps.static_duration_s)
    _move_and_settle(stage, cfg.steps.ramp_start_mm, log)

    with _open_writers(paths, 1) as (sw, stw, gw):
        reader.drain(max_time_s=cfg.drain_timeout_s)
        n_s, n_st = stream_with_stage(
            reader, stage, cfg.steps.static_duration_s,
            step=1, phase="static",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)
    return {"name": "static_noise",
            "duration_s": cfg.steps.static_duration_s,
            "position_mm": cfg.steps.ramp_start_mm,
            "n_samples": n_s, "n_status_frames": n_st}


def step2_quasi_static_ramp(reader, stage, cfg, paths, log) -> Dict:
    log.info("──── STEP 2: quasi-static ramp %.3f → %.3f mm @ %.3f mm/s ────",
             cfg.steps.ramp_start_mm, cfg.steps.ramp_end_mm,
             cfg.steps.ramp_velocity_mm_s)
    _move_and_settle(stage, cfg.steps.ramp_start_mm, log)

    with _open_writers(paths, 2) as (sw, stw, gw):
        reader.drain(max_time_s=cfg.drain_timeout_s)
        n_s, n_st = ramp_with_capture(
            reader, stage,
            start_mm=cfg.steps.ramp_start_mm,
            end_mm=cfg.steps.ramp_end_mm,
            velocity_mm_s=cfg.steps.ramp_velocity_mm_s,
            step=2, phase="ramp_fwd",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)
    return {"name": "quasi_static_ramp",
            "start_mm": cfg.steps.ramp_start_mm,
            "end_mm": cfg.steps.ramp_end_mm,
            "velocity_mm_s": cfg.steps.ramp_velocity_mm_s,
            "n_samples": n_s, "n_status_frames": n_st}


def step3_step_and_hold(reader, stage, cfg, paths, log) -> Dict:
    log.info("──── STEP 3: step %.3f → %.3f mm, hold pre=%.1fs post=%.1fs ────",
             cfg.steps.ramp_start_mm, cfg.steps.step_target_mm,
             cfg.steps.step_hold_pre_s, cfg.steps.step_hold_post_s)
    _move_and_settle(stage, cfg.steps.ramp_start_mm, log)

    with _open_writers(paths, 3) as (sw, stw, gw):
        reader.drain(max_time_s=cfg.drain_timeout_s)
        # Pre-step quiet hold
        stream_with_stage(
            reader, stage, cfg.steps.step_hold_pre_s,
            step=3, phase="pre_hold",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)
        # The step itself — command immediately, keep streaming as it moves
        log.info("  commanding step to %.3f mm", cfg.steps.step_target_mm)
        stage.move_to(cfg.steps.step_target_mm)
        # Stream long enough for the move + post-hold; the stage's internal
        # default velocity controls how fast the step actually executes.
        total_post = cfg.steps.step_hold_post_s + 2.0
        n_s, n_st = stream_with_stage(
            reader, stage, total_post,
            step=3, phase="post_hold",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)

    return {"name": "step_and_hold",
            "from_mm": cfg.steps.ramp_start_mm,
            "to_mm": cfg.steps.step_target_mm,
            "post_duration_s": total_post,
            "n_samples": n_s, "n_status_frames": n_st}


def step4_fast_pull_slow_return(reader, stage, cfg, paths, log,
                                step_num: int = 4) -> Dict:
    log.info("──── STEP %d: fast pull / slow return  "
             "%.3f → %.3f mm @ %.2f mm/s, "
             "→ %.3f mm @ %.2f mm/s ────",
             step_num,
             cfg.steps.profile_base_mm, cfg.steps.profile_peak_mm,
             cfg.steps.profile_pull_velocity_mm_s,
             cfg.steps.profile_base_mm,
             cfg.steps.profile_return_velocity_mm_s)
    _move_and_settle(stage, cfg.steps.profile_base_mm, log)

    with _open_writers(paths, step_num) as (sw, stw, gw):
        reader.drain(max_time_s=cfg.drain_timeout_s)
        # Pre-quiet
        stream_with_stage(
            reader, stage, 1.0,
            step=step_num, phase="pre_quiet",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)
        # Fast pull
        n_p, _ = ramp_with_capture(
            reader, stage,
            start_mm=cfg.steps.profile_base_mm,
            end_mm=cfg.steps.profile_peak_mm,
            velocity_mm_s=cfg.steps.profile_pull_velocity_mm_s,
            step=step_num, phase="pull",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)
        # Peak hold
        stream_with_stage(
            reader, stage, cfg.steps.profile_peak_hold_s,
            step=step_num, phase="peak_hold",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)
        # Slow return
        n_r, _ = ramp_with_capture(
            reader, stage,
            start_mm=cfg.steps.profile_peak_mm,
            end_mm=cfg.steps.profile_base_mm,
            velocity_mm_s=cfg.steps.profile_return_velocity_mm_s,
            step=step_num, phase="return",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)
        # Post-quiet
        n_post, n_st = stream_with_stage(
            reader, stage, 1.0,
            step=step_num, phase="post_quiet",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log)

    return {"name": "fast_pull_slow_return",
            "base_mm": cfg.steps.profile_base_mm,
            "peak_mm": cfg.steps.profile_peak_mm,
            "pull_velocity_mm_s": cfg.steps.profile_pull_velocity_mm_s,
            "return_velocity_mm_s": cfg.steps.profile_return_velocity_mm_s,
            "n_samples_total": n_p + n_r + n_post,
            "n_status_frames": n_st}


def step5_pipeline_endurance(reader, stage, cfg, paths, log) -> Dict:
    log.info("──── STEP 5: pipeline endurance — stage idle, %.0fs ────",
             cfg.steps.endurance_duration_s)
    _move_and_settle(stage, cfg.steps.ramp_start_mm, log)

    with _open_writers(paths, 5) as (sw, stw, gw):
        reader.drain(max_time_s=cfg.drain_timeout_s)
        n_s, n_st = stream_with_stage(
            reader, stage, cfg.steps.endurance_duration_s,
            step=5, phase="endurance",
            samples_w=sw, status_w=stw, stage_w=gw,
            stage_poll_hz=cfg.stage_poll_hz, log=log,
            progress_every_s=30.0)
    return {"name": "pipeline_endurance",
            "duration_s": cfg.steps.endurance_duration_s,
            "n_samples": n_s, "n_status_frames": n_st}


def step6_concurrent_zaber(reader, stage, cfg, paths, log) -> Dict:
    log.info("──── STEP 6: profile + concurrent Zaber comms (= step 4) ────")
    log.info("       Note: ensure 1 kSPS firmware build is loaded "
             "before running this step.")
    out = step4_fast_pull_slow_return(reader, stage, cfg, paths, log,
                                      step_num=6)
    out["name"] = "concurrent_zaber"
    return out


# =============================================================================
# Helpers
# =============================================================================
class _open_writers:
    """Context manager that opens 3 CSVs for a step and writes headers."""

    def __init__(self, paths: RunPaths, step: int):
        self.paths = paths
        self.step = step
        self._files: List = []

    def __enter__(self):
        sf = open(self.paths.samples_csv(self.step), "w", newline="")
        stf = open(self.paths.status_csv(self.step), "w", newline="")
        gf = open(self.paths.stage_log_csv(self.step), "w", newline="")
        self._files = [sf, stf, gf]
        sw = csv.writer(sf); sw.writerow(_SAMPLE_HEADER)
        stw = csv.writer(stf); stw.writerow(_STATUS_HEADER)
        gw = csv.writer(gf); gw.writerow(_STAGE_HEADER)
        return sw, stw, gw

    def __exit__(self, exc_type, exc, tb):
        for f in self._files:
            try: f.close()
            except Exception: pass


def _move_and_settle(stage: ZaberStage, target_mm: float,
                     log: logging.Logger, settle_s: float = 1.0,
                     timeout_s: float = 30.0) -> None:
    log.info("Moving stage to %.3f mm...", target_mm)
    if not stage.move_to(target_mm):
        raise RuntimeError(f"stage.move_to({target_mm}) failed")
    t0 = time.monotonic()
    while stage.is_moving():
        if time.monotonic() - t0 > timeout_s:
            raise RuntimeError(f"stage move to {target_mm} timed out")
        time.sleep(0.01)
    time.sleep(settle_s)


def firmware_git_hash() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=_THIS_DIR.parent,
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
    except Exception:
        return None


def build_metadata(cfg: RunConfig, paths: RunPaths, stage: ZaberStage,
                   steps_run: List[str], step_results: List[Dict]) -> Dict:
    info = stage.get_device_info()
    return {
        "run_prefix": paths.prefix,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": "Phase 5 — spring smoke test",
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "firmware": {
            "repo_git_hash": firmware_git_hash(),
            "firmware_path": cfg.firmware_path,
            "sample_rate_sps_annotation": cfg.sample_rate_sps,
        },
        "spring": {
            "k_mN_per_mm": cfg.spring_k_mN_per_mm,
            "source": cfg.spring_k_source,
        },
        "reference_noise_floors": {
            "laser_voltage_V_sigma": cfg.laser_noise_V_ref,
            "load_voltage_V_sigma": cfg.load_noise_V_ref,
        },
        "sensor_calibration": {
            "laser_k_mV_per_um": cfg.laser_k_mV_per_um,
            "laser_V0_mV": cfg.laser_V0_mV,
            "load_sensitivity_mV_per_mN": cfg.load_sensitivity_mV_per_mN,
            "load_V0_mV": cfg.load_V0_mV,
        },
        "stage": {
            "name": info.name if info else None,
            "serial_number": info.serial_number if info else None,
            "firmware_version": info.firmware_version if info else None,
            "port": info.port if info else None,
        },
        "config": {
            "sample_rate_sps": cfg.sample_rate_sps,
            "stage_poll_hz": cfg.stage_poll_hz,
            "drain_timeout_s": cfg.drain_timeout_s,
            "steps": asdict(cfg.steps),
            "operator": cfg.operator,
            "notes": cfg.notes,
        },
        "steps_run": steps_run,
        "step_results": step_results,
    }


# =============================================================================
# Main
# =============================================================================
STEP_FUNCS: Dict[int, Callable] = {
    1: step1_static_noise,
    2: step2_quasi_static_ramp,
    3: step3_step_and_hold,
    4: step4_fast_pull_slow_return,
    5: step5_pipeline_endurance,
    6: step6_concurrent_zaber,
}
DEFAULT_STEPS_ALL = [1, 2, 3, 4, 5]   # step 6 opt-in (requires 1 kSPS)


def parse_steps(arg: str) -> List[int]:
    arg = arg.strip().lower()
    if arg == "all":
        return DEFAULT_STEPS_ALL
    if arg == "all+6":
        return DEFAULT_STEPS_ALL + [6]
    out: List[int] = []
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            n = int(tok)
        except ValueError:
            raise SystemExit(f"--step: bad token {tok!r}")
        if n not in STEP_FUNCS:
            raise SystemExit(f"--step: unknown step {n} (1-6 valid)")
        out.append(n)
    return out


def run(cfg: RunConfig, steps: List[int]) -> None:
    paths = next_run_paths(_THIS_DIR / "data")
    # File log alongside the console log
    fh = logging.FileHandler(paths.run_log, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s"))
    logging.getLogger().addHandler(fh)
    log = logging.getLogger("run_spring_smoke_test")
    log.info("Run prefix: %s", paths.prefix)
    log.info("Steps to run: %s", steps)

    if not cfg.portenta_port:
        log.error("portenta_port is not set in config.yaml")
        sys.exit(2)

    # Zaber stage
    log.info("Connecting Zaber stage (config: %s)", cfg.zaber_config_path)
    stage = ZaberStage(config_file=cfg.zaber_config_path)
    stage.load_config(cfg.zaber_config_path)
    if cfg.zaber_port:
        stage.port = cfg.zaber_port
    if not stage.connect():
        log.error("Could not connect to Zaber stage on %s", stage.port)
        sys.exit(3)

    try:
        if not stage.is_homed():
            log.info("Homing stage...")
            if not stage.home():
                log.error("Homing failed.")
                sys.exit(4)

        log.info("Opening Portenta on %s @ %d (adc_source=None — keep all)",
                 cfg.portenta_port, cfg.portenta_baud)
        with PortentaReader(port=cfg.portenta_port, baud=cfg.portenta_baud,
                            adc_source=None) as reader:

            # Match _quick_probe: drain stale boot bytes, then verify the
            # stream is alive BEFORE step 1 starts. If the firmware paused
            # on a prior DTR drop (e.g. you ran portenta_reader.py first),
            # we want to fail fast with an actionable message instead of
            # spending 60 s reading silence.
            reader.drain()
            warmup_samples, _ = reader.read_streaming(
                duration_s=2.0, progress_every_s=10.0)
            if not warmup_samples:
                log.error("Portenta opened but produced 0 samples in 2.0 s "
                          "warmup. Power-cycle the Mid Carrier and retry. "
                          "(Closing/reopening COM%s without a power-cycle "
                          "can pause the firmware stream if the sketch "
                          "gates on USB-CDC connect state.)",
                          cfg.portenta_port)
                sys.exit(5)
            log.info("Portenta warmup OK: %d samples in 2.0 s "
                     "(%.0f SPS combined)",
                     len(warmup_samples), len(warmup_samples) / 2.0)

            step_results: List[Dict] = []
            steps_run: List[str] = []
            for n in steps:
                fn = STEP_FUNCS[n]
                t0 = time.monotonic()
                try:
                    result = fn(reader, stage, cfg, paths, log)
                except Exception:
                    log.exception("step %d FAILED", n)
                    step_results.append(
                        {"step": n, "status": "failed",
                         "elapsed_s": time.monotonic() - t0})
                    continue
                result["step"] = n
                result["status"] = "ok"
                result["elapsed_s"] = time.monotonic() - t0
                step_results.append(result)
                steps_run.append(result["name"])
                log.info("step %d done — %.1fs elapsed", n, result["elapsed_s"])

            # Return to base
            try:
                _move_and_settle(stage, cfg.steps.ramp_start_mm, log,
                                 settle_s=0.2)
            except Exception:
                log.warning("could not return to base position")

            meta = build_metadata(cfg, paths, stage, steps_run, step_results)
            with open(paths.meta_json, "w") as f:
                json.dump(meta, f, indent=2)
            log.info("Wrote %s", paths.meta_json)
            log.info("Done. Next: python analyze.py %s", paths.root)

    finally:
        try:
            stage.disconnect()
        except Exception:
            pass


def _main() -> None:
    p = argparse.ArgumentParser(
        description="Spring-as-SMA smoke test runner (Phase 5)")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"),
                   help="path to config.yaml")
    p.add_argument("--step", default="all",
                   help="which step(s) to run: 'all' (1-5), 'all+6', "
                        "or a comma list like '1,2,5'")
    p.add_argument("--sample-rate", type=int, choices=(400, 1000),
                   help="annotate meta.json with the currently-flashed "
                        "firmware rate (overrides config)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    # Force UTF-8 on console so box-drawing chars (────, →, —) don't
    # blow up the cp1252 default on Windows.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    )

    cfg = RunConfig.from_yaml(Path(args.config))
    if args.sample_rate:
        cfg.sample_rate_sps = args.sample_rate

    steps = parse_steps(args.step)
    run(cfg, steps)


if __name__ == "__main__":
    _main()
