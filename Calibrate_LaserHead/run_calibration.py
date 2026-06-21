#!/usr/bin/env python3
"""
run_calibration.py — IL-030 → ADS1263 static-sweep calibration orchestrator.

Implements Calibrate_LaserHead_Plan.md §4 (Procedure) and §5 (Data Outputs):

    For each target position in the sweep:
      1. stage.move_to(target_mm)
      2. wait until stage.is_moving() == False
      3. sleep settle_time_s
      4. drain the Portenta serial buffer
      5. read samples_per_point fresh samples
      6. record one aggregate row (mean, std, n, timestamps)

    Before & after the sweep: collect a ~500-sample baseline at home so we
    can detect thermal drift.

Outputs (under ./data/, shared timestamped prefix YYYY-MM-DD_runNN_*):
    * <prefix>_raw.csv     — every individual sample
    * <prefix>_points.csv  — one row per stage position (feeds analyze.py)
    * <prefix>_meta.json   — run metadata for 6-months-from-now reproducibility

Use --dry-run for the 10-point / 1-mm sanity pass from plan §9.3 before
committing hardware time to a full sweep.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from portenta_reader import PortentaReader, Sample

# Make the sibling ZaberStage package importable without requiring an install.
_THIS_DIR = Path(__file__).resolve().parent
_ZABER_DIR = _THIS_DIR.parent / "Driver_ZaberStage"
if str(_ZABER_DIR) not in sys.path:
    sys.path.insert(0, str(_ZABER_DIR))

from zaber_stage import ZaberStage  # noqa: E402


# =============================================================================
# Config
# =============================================================================
@dataclass
class SweepConfig:
    sweep_center_mm: float          # absolute stage position at IL-030 reference distance
    sweep_range_mm: List[float]     # relative to sweep_center_mm
    step_size_mm: float
    direction: str
    settle_time_s: float
    stage_velocity_mm_s: float
    samples_per_point: int
    baseline_samples: int
    drain_timeout_s: float
    portenta_port: Optional[str]
    portenta_baud: int
    zaber_config_path: str
    zaber_port: Optional[str]       # overrides port loaded from zaber_config_path
    operator: str
    notes: str
    passes: int = 1                 # number of full sweeps back-to-back
                                    # (>1 gives pass-to-pass repeatability;
                                    # default 1 preserves legacy behavior)
    xcompare: bool = False          # cross-compare mode: capture BOTH ADC1
                                    # and ADC2 simultaneously (requires
                                    # LaserHead_PIO flashed with
                                    # ENABLE_ADC1=ENABLE_ADC2=1 and both
                                    # ADCs muxed to AIN4/AIN5). Default
                                    # False preserves single-channel
                                    # behaviour for legacy / SensorHub
                                    # production firmware.

    @classmethod
    def from_yaml(cls, path: Path, dry_run: bool = False) -> "SweepConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
        if dry_run and "dry_run" in raw:
            raw.update(raw["dry_run"])
        raw.pop("dry_run", None)
        # Ignore any unknown keys rather than crashing — keeps the config
        # forward-compatible if we add a field we haven't wired up yet.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def target_positions_mm(self) -> List[float]:
        lo, hi = self.sweep_range_mm
        n_steps = int(round((hi - lo) / self.step_size_mm))
        # Build with integer arithmetic on step count, then rescale, to avoid
        # float accumulation drift over 80+ steps.
        forward = [lo + i * (hi - lo) / n_steps for i in range(n_steps + 1)]
        if self.direction == "forward_only":
            return forward
        if self.direction == "bidirectional":
            return forward + list(reversed(forward))
        raise ValueError(f"unknown direction: {self.direction!r}")


# =============================================================================
# Per-position aggregation
# =============================================================================
@dataclass
class PointAggregate:
    target_mm: float
    stage_actual_mm: float
    mean_V: float                                    # PRIMARY channel (ADC2 in xcompare mode)
    std_V: float
    n_samples: int
    timestamp_start_us: int
    timestamp_end_us: int
    sweep_index: int            # 0..N-1 in the order actually visited within a pass
    direction_tag: str = "fwd"  # "fwd" or "rev" for bidirectional runs
    pass_index: int = 0         # 0..passes-1 for sweep rows; -1 for baseline rows
    # Cross-compare secondary channel (ADC1 reading the same AIN4/AIN5).
    # All None in single-channel mode; populated only when xcompare is on.
    mean_V_adc1: Optional[float] = None
    std_V_adc1: Optional[float] = None
    n_samples_adc1: Optional[int] = None


def aggregate(samples: List[Sample], target_mm: float, stage_actual_mm: float,
              sweep_index: int, direction_tag: str,
              pass_index: int = 0) -> PointAggregate:
    if not samples:
        raise RuntimeError(
            f"no samples captured at target {target_mm:.3f} mm — "
            "check the Portenta stream")
    vs = [s.voltage_V for s in samples]
    mean_v = statistics.fmean(vs)
    std_v = statistics.pstdev(vs) if len(vs) > 1 else 0.0
    return PointAggregate(
        target_mm=target_mm,
        stage_actual_mm=stage_actual_mm,
        mean_V=mean_v,
        std_V=std_v,
        n_samples=len(vs),
        timestamp_start_us=samples[0].timestamp_us,
        timestamp_end_us=samples[-1].timestamp_us,
        sweep_index=sweep_index,
        direction_tag=direction_tag,
        pass_index=pass_index,
    )


def aggregate_dual(samples_adc1: List[Sample], samples_adc2: List[Sample],
                   target_mm: float, stage_actual_mm: float,
                   sweep_index: int, direction_tag: str,
                   pass_index: int = 0) -> PointAggregate:
    """
    Aggregate a dual-ADC capture into one PointAggregate.

    ADC2 is the primary channel (its mean_V is what gets fit into the
    calibration k/V₀ that propagates to production). ADC1 is the
    cross-compare secondary — its mean/std/n populate the *_adc1
    fields on the same row.

    Raises if either bucket is empty so a half-failed capture doesn't
    silently produce a one-channel row that looks like a normal run.
    """
    if not samples_adc1:
        raise RuntimeError(
            f"no ADC1 samples captured at target {target_mm:.3f} mm — "
            "is the firmware actually streaming dual-ADC (ENABLE_ADC1=1)?")
    if not samples_adc2:
        raise RuntimeError(
            f"no ADC2 samples captured at target {target_mm:.3f} mm — "
            "check the Portenta stream")

    # Primary aggregate from ADC2
    agg = aggregate(samples_adc2, target_mm=target_mm,
                    stage_actual_mm=stage_actual_mm,
                    sweep_index=sweep_index, direction_tag=direction_tag,
                    pass_index=pass_index)

    # Secondary stats from ADC1
    vs1 = [s.voltage_V for s in samples_adc1]
    agg.mean_V_adc1 = statistics.fmean(vs1)
    agg.std_V_adc1 = statistics.pstdev(vs1) if len(vs1) > 1 else 0.0
    agg.n_samples_adc1 = len(vs1)
    return agg


# =============================================================================
# Output paths
# =============================================================================
@dataclass
class RunPaths:
    raw_csv: Path
    points_csv: Path
    meta_json: Path
    prefix: str


def next_run_paths(data_dir: Path) -> RunPaths:
    """
    Build YYYY-MM-DD_runNN_* where NN is the next unused integer for today.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    n = 1
    while True:
        prefix = f"{today}_run{n:02d}"
        raw = data_dir / f"{prefix}_raw.csv"
        if not raw.exists():
            return RunPaths(
                raw_csv=raw,
                points_csv=data_dir / f"{prefix}_points.csv",
                meta_json=data_dir / f"{prefix}_meta.json",
                prefix=prefix,
            )
        n += 1


# =============================================================================
# Capture helpers
# =============================================================================
def write_raw_rows(writer, samples: List[Sample], *, target_mm: float,
                   stage_actual_mm: float, sweep_index: int,
                   direction_tag: str, pass_index: int = 0) -> None:
    """
    Write per-sample rows. Each row carries the sample's adc_source
    when known (4-col dual-stream firmware); the column is left empty
    for 3-col single-channel rows so legacy data still loads cleanly.
    """
    for s in samples:
        writer.writerow([
            pass_index, sweep_index, direction_tag,
            f"{target_mm:.6f}", f"{stage_actual_mm:.6f}",
            s.timestamp_us, f"{s.voltage_V:.8f}",
            s.raw_code if s.raw_code is not None else "",
            s.adc_source if s.adc_source is not None else "",
        ])


def capture_point(reader: PortentaReader, n: int,
                  drain_timeout_s: float) -> List[Sample]:
    """Drain stale data, then read `n` fresh samples (single channel)."""
    reader.drain(max_time_s=drain_timeout_s)
    # Use a generous timeout: at 100 SPS, 100 samples ≈ 1 s; allow 10×.
    timeout = max(10.0, (n / 50.0))
    return reader.read_samples(n=n, timeout_s=timeout)


def capture_point_dual(reader: PortentaReader, n_per_adc: int,
                       drain_timeout_s: float
                       ) -> "tuple[List[Sample], List[Sample]]":
    """
    Drain stale data, then read `n_per_adc` fresh samples from BOTH
    ADC1 and ADC2 (cross-compare mode). Returns (samples_adc1,
    samples_adc2).
    """
    reader.drain(max_time_s=drain_timeout_s)
    # Both buckets fill in parallel at the same SPS, but we still
    # double the budget vs. single-channel to absorb any per-ADC
    # timing skew at the start.
    timeout = max(20.0, (n_per_adc / 25.0))
    return reader.read_samples_dual(n_per_adc=n_per_adc, timeout_s=timeout)


# =============================================================================
# Metadata
# =============================================================================
def firmware_git_hash() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=_THIS_DIR.parent, stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode().strip()
    except Exception:
        return None


def build_metadata(cfg: SweepConfig, paths: RunPaths, stage: ZaberStage,
                   dry_run: bool) -> dict:
    info = stage.get_device_info()
    return {
        "run_prefix": paths.prefix,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": dry_run,
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "firmware": {
            # Plan §5: capture the firmware git hash. We record the REPO hash
            # since firmware is tracked in-repo under
            # Calibrate_LaserHead/Calibrate_LaserHead_PIO/ (the calibration-
            # purpose dual-ADC firmware created 2026-05-26).
            "repo_git_hash": firmware_git_hash(),
            "firmware_path": (
                "Calibrate_LaserHead/Calibrate_LaserHead_PIO/src/main.cpp"
                if cfg.xcompare else
                "LaserHead_PIO/src/main.cpp"
            ),
            "firmware_build": (
                "dual-ADC xcompare (ENABLE_ADC1=1, ENABLE_ADC2=1) — "
                "Calibrate_LaserHead_PIO"
                if cfg.xcompare else
                "laser-only (ENABLE_ADC1=0, ENABLE_ADC2=1) — LaserHead_PIO"
            ),
            "xcompare_mode": cfg.xcompare,
            # ADC2 path — Keyence IL-030 → AIN4(+) / AIN5(-) on the bare
            # TI ADS1263 EVM, mirroring Firmware_SensorHub_PIO production. Values
            # below are the literal register settings written by
            # LaserHead_PIO/src/main.cpp setup() — if you change those,
            # update here too.
            "adc_source": 2,                                # ADC2 (24-bit)
            "nominal_sps": 400,                             # ADS1263_ADC2_400SPS
            "filter": "Sinc3",                              # ADC2's only option
            "adc2mux_hex": "0x45",                          # AIN4(+) / AIN5(-)
            "ref2": "external REF7050 on AIN0/AIN1",        # ADS1263_ADC2_REF_AIN01
            "reference_V": 5.0,                             # REF7050 nominal
            "refmux_hex_adc1": "0x09",                      # ADS1263_REFMUX_EXT_AIN01 (shared ref)
            "gain": 1,                                      # ADS1263_ADC2_GAIN_1
            "pga_bypass": False,                            # ADC2's PGA cannot be bypassed; runs as unity buffer
            "signal_chain": (
                "Keyence IL-030 (0-5 V single-ended) → "
                "EVM AIN4(+) / AIN5(-) → ADC2 (24-bit, 400 SPS, Sinc3, "
                "REF7050 5V, gain=1) → M4 polling (3 ms period) → "
                "RPC → M7 USB-CDC bridge → host @ 115200"
            ),
            # When xcompare is on, ADC1 also samples AIN4/AIN5 — its config
            # mirrors ADC2 except for the 32-bit core and the PGA being in
            # path with bypassable gain.
            "xcompare_adc1": ({
                "inpmux_hex": "0x45",                        # AIN4(+) / AIN5(-)
                "refmux_hex": "0x09",                        # ADS1263_REFMUX_EXT_AIN01
                "nominal_sps": 400,                          # ADS1263_400SPS
                "filter": "Sinc3",                           # MODE1 default
                "gain": 1,
                "pga_bypass": False,                         # PGA in path, unity gain
                "purpose": "cross-compare digital-path check on IL-030 signal",
            } if cfg.xcompare else None),
        },
        "stage": {
            "name": info.name if info else None,
            "serial_number": info.serial_number if info else None,
            "firmware_version": info.firmware_version if info else None,
            "port": info.port if info else None,
        },
        "config": asdict(cfg),
        "outputs": {
            "raw_csv": paths.raw_csv.name,
            "points_csv": paths.points_csv.name,
        },
    }


# =============================================================================
# Main run
# =============================================================================
def run(cfg: SweepConfig, dry_run: bool) -> None:
    log = logging.getLogger("run_calibration")

    # ---- 1. Resolve the Portenta port ---------------------------------------
    port = cfg.portenta_port
    if not port:
        log.error("portenta_port is not set in config.yaml — please set it "
                  "explicitly (e.g. COM8). Auto-detect is not implemented yet "
                  "to avoid accidentally talking to the Zaber port.")
        sys.exit(2)

    # ---- 2. Connect the Zaber stage -----------------------------------------
    log.info("Connecting Zaber stage (config: %s)", cfg.zaber_config_path)
    stage = ZaberStage(config_file=cfg.zaber_config_path)
    stage.load_config(cfg.zaber_config_path)
    # Override the port from config.yaml — safety_config.json has "auto".
    if cfg.zaber_port:
        stage.port = cfg.zaber_port
        log.info("Zaber port override from config.yaml: %s", cfg.zaber_port)
    if not stage.connect():
        log.error("Could not connect to Zaber stage on %s. Aborting.",
                  stage.port)
        sys.exit(3)

    try:
        if not stage.is_homed():
            log.info("Homing stage...")
            if not stage.home():
                log.error("Homing failed. Aborting.")
                sys.exit(4)

        # Move to the sweep center — the IL-030 reference distance. All
        # subsequent positions are expressed as offsets from this center,
        # so x=0 in the recorded data means "at reference distance".
        center_abs = cfg.sweep_center_mm
        lo_abs = center_abs + cfg.sweep_range_mm[0]
        hi_abs = center_abs + cfg.sweep_range_mm[1]
        log.info("Moving to sweep center: absolute %.3f mm", center_abs)
        if not stage.move_to(center_abs):
            log.error("stage.move_to(%.4f) failed", center_abs)
            sys.exit(4)
        while stage.is_moving():
            time.sleep(0.01)
        time.sleep(cfg.settle_time_s)
        actual_center = stage.get_position()
        if abs(actual_center - center_abs) > 0.05:
            log.warning("stage landed at %.4f mm (wanted %.4f) — check "
                        "position_limits_mm in zaber config",
                        actual_center, center_abs)

        # ---- 3. Plan the sweep ---------------------------------------------
        targets = cfg.target_positions_mm()
        log.info("Sweep: %d positions, step=%.3f mm, range=[%+.2f, %+.2f] mm "
                 "relative to center (absolute %.2f → %.2f mm)",
                 len(targets), cfg.step_size_mm, *cfg.sweep_range_mm,
                 lo_abs, hi_abs)

        # ---- 4. Connect the Portenta ---------------------------------------
        log.info("Opening Portenta on %s @ %d", port, cfg.portenta_baud)
        # adc_source=2: laser is on ADC2 (AIN4/AIN5) on the EVM, per the
        # post-port LaserHead_PIO firmware (laser-only build) and per
        # Firmware_SensorHub_PIO production routing. For LaserHead_PIO's 3-column
        # TSV stream this filter argument is ignored at parse time; for a
        # 4-column dual-ADC stream (e.g. Firmware_SensorHub_PIO, or LaserHead_PIO
        # with the ENABLE_ADC1 cross-compare flag) it correctly selects
        # the src=2 laser rows. The prior `adc_source=1` setting was a
        # Waveshare-HAT workaround when the laser was routed through
        # ADC1/AIN0-AIN1 via the HAT's load-cell front-end — that signal
        # path no longer exists on the bare TI EVM.
        with PortentaReader(port=port, baud=cfg.portenta_baud,
                            adc_source=2) as reader:

            paths = next_run_paths(_THIS_DIR / "data")
            log.info("Writing run outputs with prefix %s", paths.prefix)

            # ---- 5. Open output files --------------------------------------
            # New in 2026-05-26:
            #   - pass_index column (first), so multi-pass runs can be demuxed
            #     in analyze.py. Baseline rows use pass_index=-1.
            #   - raw.csv carries adc_source per sample (1 or 2 in xcompare
            #     dual-stream mode; empty in single-channel mode).
            #   - points.csv carries optional mean_V_adc1 / std_V_adc1 /
            #     n_samples_adc1 columns (populated only when xcompare is on;
            #     empty otherwise, so legacy analyzers still read mean_V).
            raw_f = open(paths.raw_csv, "w", newline="")
            raw_w = csv.writer(raw_f)
            raw_w.writerow([
                "pass_index", "sweep_index", "direction",
                "target_mm", "stage_actual_mm",
                "timestamp_us", "voltage_V", "raw_code", "adc_source",
            ])

            points_f = open(paths.points_csv, "w", newline="")
            points_w = csv.writer(points_f)
            points_w.writerow([
                "pass_index", "sweep_index", "direction",
                "target_mm", "stage_actual_mm",
                "mean_V", "std_V", "n_samples",
                "timestamp_start_us", "timestamp_end_us",
                "mean_V_adc1", "std_V_adc1", "n_samples_adc1",
            ])

            def _write_point_row(agg: PointAggregate) -> None:
                """Write one aggregate row in the post-2026-05-26 column order."""
                points_w.writerow([
                    agg.pass_index, agg.sweep_index, agg.direction_tag,
                    f"{agg.target_mm:.6f}", f"{agg.stage_actual_mm:.6f}",
                    f"{agg.mean_V:.8f}", f"{agg.std_V:.8f}",
                    agg.n_samples,
                    agg.timestamp_start_us, agg.timestamp_end_us,
                    # ADC1 secondary (cross-compare). Format with same
                    # precision as the primary mean_V/std_V when present;
                    # leave empty when in single-channel mode.
                    (f"{agg.mean_V_adc1:.8f}" if agg.mean_V_adc1 is not None else ""),
                    (f"{agg.std_V_adc1:.8f}" if agg.std_V_adc1 is not None else ""),
                    (agg.n_samples_adc1 if agg.n_samples_adc1 is not None else ""),
                ])

            def _capture_aggregate_write(*, target_mm: float,
                                         stage_actual_mm: float,
                                         sweep_index: int,
                                         direction_tag: str,
                                         pass_index: int,
                                         n_samples: int) -> PointAggregate:
                """
                Capture one point (single-channel or dual-channel per
                cfg.xcompare), write the raw rows, and return the aggregate.
                Used by both baseline blocks and the main sweep so the
                xcompare branching lives in exactly one place.
                """
                if cfg.xcompare:
                    s1, s2 = capture_point_dual(reader, n_samples,
                                                cfg.drain_timeout_s)
                    write_raw_rows(raw_w, s1,
                                   target_mm=target_mm,
                                   stage_actual_mm=stage_actual_mm,
                                   sweep_index=sweep_index,
                                   direction_tag=direction_tag,
                                   pass_index=pass_index)
                    write_raw_rows(raw_w, s2,
                                   target_mm=target_mm,
                                   stage_actual_mm=stage_actual_mm,
                                   sweep_index=sweep_index,
                                   direction_tag=direction_tag,
                                   pass_index=pass_index)
                    return aggregate_dual(s1, s2,
                                          target_mm=target_mm,
                                          stage_actual_mm=stage_actual_mm,
                                          sweep_index=sweep_index,
                                          direction_tag=direction_tag,
                                          pass_index=pass_index)
                else:
                    samples = capture_point(reader, n_samples,
                                            cfg.drain_timeout_s)
                    write_raw_rows(raw_w, samples,
                                   target_mm=target_mm,
                                   stage_actual_mm=stage_actual_mm,
                                   sweep_index=sweep_index,
                                   direction_tag=direction_tag,
                                   pass_index=pass_index)
                    return aggregate(samples,
                                     target_mm=target_mm,
                                     stage_actual_mm=stage_actual_mm,
                                     sweep_index=sweep_index,
                                     direction_tag=direction_tag,
                                     pass_index=pass_index)

            aggregates: List[PointAggregate] = []

            # ---- 6. Baseline before sweep (at sweep center) ---------------
            log.info("Collecting %d baseline samples at center (%.3f mm abs)%s...",
                     cfg.baseline_samples, center_abs,
                     " [xcompare]" if cfg.xcompare else "")
            agg_pre = _capture_aggregate_write(
                target_mm=0.0,
                stage_actual_mm=stage.get_position() - center_abs,
                sweep_index=-1, direction_tag="baseline_pre",
                pass_index=-1,
                n_samples=cfg.baseline_samples,
            )
            _write_point_row(agg_pre)
            aggregates.append(agg_pre)
            if cfg.xcompare:
                log.info("  baseline pre: ADC2 mean=%.6f V std=%.2e V (n=%d) | "
                         "ADC1 mean=%.6f V std=%.2e V (n=%d)",
                         agg_pre.mean_V, agg_pre.std_V, agg_pre.n_samples,
                         agg_pre.mean_V_adc1, agg_pre.std_V_adc1,
                         agg_pre.n_samples_adc1)
            else:
                log.info("  baseline pre: mean=%.6f V  std=%.2e V  (n=%d)",
                         agg_pre.mean_V, agg_pre.std_V, agg_pre.n_samples)

            # ---- 7. Main sweep — outer loop over passes, inner over targets ----
            is_bidirectional = cfg.direction == "bidirectional"
            n_fwd = (len(targets) // 2 + 1) if is_bidirectional else len(targets)
            n_passes = max(1, cfg.passes)
            total_points = n_passes * len(targets)

            log.info("Running %d pass(es) × %d points = %d total sweep points",
                     n_passes, len(targets), total_points)

            for pass_idx in range(n_passes):
                log.info("──── PASS %d / %d ────", pass_idx + 1, n_passes)

                for i, rel_mm in enumerate(targets):
                    abs_mm = center_abs + rel_mm
                    tag = "fwd" if (not is_bidirectional or i < n_fwd) else "rev"
                    global_idx = pass_idx * len(targets) + i + 1
                    log.info("[%3d/%3d  p%d %s] move → %+.3f mm (abs %.4f mm)",
                             global_idx, total_points,
                             pass_idx + 1, tag, rel_mm, abs_mm)

                    if not stage.move_to(abs_mm):
                        log.error("stage.move_to(%.4f) failed", abs_mm)
                        sys.exit(5)

                    # Wait for motion to complete. The stage reader polls at
                    # 100 Hz, so we just poll is_moving() here with a cap.
                    t_move_start = time.monotonic()
                    while stage.is_moving():
                        if time.monotonic() - t_move_start > 30.0:
                            log.error("stage move timed out")
                            sys.exit(6)
                        time.sleep(0.01)

                    time.sleep(cfg.settle_time_s)
                    stage_actual = stage.get_position() - center_abs

                    agg = _capture_aggregate_write(
                        target_mm=rel_mm,
                        stage_actual_mm=stage_actual,
                        sweep_index=i, direction_tag=tag,
                        pass_index=pass_idx,
                        n_samples=cfg.samples_per_point,
                    )
                    _write_point_row(agg)
                    aggregates.append(agg)
                    # Flush so a crash mid-sweep doesn't lose earlier rows.
                    raw_f.flush()
                    points_f.flush()

            # ---- 8. Return to center and baseline again -------------------
            log.info("Returning to sweep center (%.3f mm abs)...", center_abs)
            stage.move_to(center_abs)
            while stage.is_moving():
                time.sleep(0.01)
            time.sleep(cfg.settle_time_s)

            log.info("Collecting %d baseline samples at center (post)%s...",
                     cfg.baseline_samples,
                     " [xcompare]" if cfg.xcompare else "")
            agg_post = _capture_aggregate_write(
                target_mm=0.0,
                stage_actual_mm=stage.get_position() - center_abs,
                sweep_index=-2, direction_tag="baseline_post",
                pass_index=-1,
                n_samples=cfg.baseline_samples,
            )
            _write_point_row(agg_post)
            aggregates.append(agg_post)

            raw_f.close()
            points_f.close()

            # ---- 9. Metadata ----------------------------------------------
            meta = build_metadata(cfg, paths, stage, dry_run=dry_run)
            # Baseline drift / noise on the primary (ADC2) channel — same
            # fields as before so existing analyzers keep working.
            meta["baseline_drift_V"] = agg_post.mean_V - agg_pre.mean_V
            meta["baseline_noise_V_pre"] = agg_pre.std_V
            meta["baseline_noise_V_post"] = agg_post.std_V
            # When xcompare is on, also record ADC1's baseline drift/noise
            # so the cross-channel comparison includes thermal/drift parity.
            if cfg.xcompare and agg_pre.mean_V_adc1 is not None \
                    and agg_post.mean_V_adc1 is not None:
                meta["baseline_drift_V_adc1"] = (
                    agg_post.mean_V_adc1 - agg_pre.mean_V_adc1
                )
                meta["baseline_noise_V_pre_adc1"] = agg_pre.std_V_adc1
                meta["baseline_noise_V_post_adc1"] = agg_post.std_V_adc1
            with open(paths.meta_json, "w") as f:
                json.dump(meta, f, indent=2)
            drift = meta["baseline_drift_V"]
            log.info("Baseline drift (ADC2): %+.6f V (|drift|/sigma = %.2f)",
                     drift, abs(drift) / max(agg_pre.std_V, 1e-12))
            if cfg.xcompare and "baseline_drift_V_adc1" in meta:
                drift1 = meta["baseline_drift_V_adc1"]
                log.info("Baseline drift (ADC1): %+.6f V (|drift|/sigma = %.2f)",
                         drift1, abs(drift1) / max(agg_pre.std_V_adc1, 1e-12))
            log.info("Done. Next step: python analyze.py %s", paths.points_csv)

    finally:
        try:
            stage.disconnect()
        except Exception:
            pass


# =============================================================================
# Entry point
# =============================================================================
def _main() -> None:
    p = argparse.ArgumentParser(description="Run a laser-head calibration sweep")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"),
                   help="path to config.yaml (default: alongside this script)")
    p.add_argument("--dry-run", action="store_true",
                   help="use the config.yaml 'dry_run' overrides "
                        "(~11 points over 1 mm per plan section 9.3)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    )

    cfg = SweepConfig.from_yaml(Path(args.config), dry_run=args.dry_run)
    run(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    _main()
