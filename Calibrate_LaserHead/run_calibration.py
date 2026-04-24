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
_ZABER_DIR = _THIS_DIR.parent / "ZaberStage"
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
    mean_V: float
    std_V: float
    n_samples: int
    timestamp_start_us: int
    timestamp_end_us: int
    sweep_index: int            # 0..N-1 in the order actually visited
    direction_tag: str = "fwd"  # "fwd" or "rev" for bidirectional runs


def aggregate(samples: List[Sample], target_mm: float, stage_actual_mm: float,
              sweep_index: int, direction_tag: str) -> PointAggregate:
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
    )


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
def capture_point(reader: PortentaReader, n: int,
                  drain_timeout_s: float) -> List[Sample]:
    """Drain stale data, then read `n` fresh samples."""
    reader.drain(max_time_s=drain_timeout_s)
    # Use a generous timeout: at 100 SPS, 100 samples ≈ 1 s; allow 10×.
    timeout = max(10.0, (n / 50.0))
    return reader.read_samples(n=n, timeout_s=timeout)


def write_raw_rows(writer, samples: List[Sample], *, target_mm: float,
                   stage_actual_mm: float, sweep_index: int,
                   direction_tag: str) -> None:
    for s in samples:
        writer.writerow([
            sweep_index, direction_tag,
            f"{target_mm:.6f}", f"{stage_actual_mm:.6f}",
            s.timestamp_us, f"{s.voltage_V:.8f}",
            s.raw_code if s.raw_code is not None else "",
        ])


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
            # since firmware is tracked in-repo under LaserHead_PIO/.
            "repo_git_hash": firmware_git_hash(),
            "firmware_path": "LaserHead_PIO/src/main.cpp",
            "adc_source": 2,
            "nominal_sps": 100,
            "reference_V": 5.0,
            "inpmux_hex": "0x23",   # AIN2+ / AIN3-
            "gain": 1,
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
        with PortentaReader(port=port, baud=cfg.portenta_baud) as reader:

            paths = next_run_paths(_THIS_DIR / "data")
            log.info("Writing run outputs with prefix %s", paths.prefix)

            # ---- 5. Open output files --------------------------------------
            raw_f = open(paths.raw_csv, "w", newline="")
            raw_w = csv.writer(raw_f)
            raw_w.writerow([
                "sweep_index", "direction", "target_mm", "stage_actual_mm",
                "timestamp_us", "voltage_V", "raw_code",
            ])

            points_f = open(paths.points_csv, "w", newline="")
            points_w = csv.writer(points_f)
            points_w.writerow([
                "sweep_index", "direction", "target_mm", "stage_actual_mm",
                "mean_V", "std_V", "n_samples",
                "timestamp_start_us", "timestamp_end_us",
            ])

            aggregates: List[PointAggregate] = []

            # ---- 6. Baseline before sweep (at sweep center) ---------------
            log.info("Collecting %d baseline samples at center (%.3f mm abs)...",
                     cfg.baseline_samples, center_abs)
            baseline_pre = capture_point(reader, cfg.baseline_samples,
                                         cfg.drain_timeout_s)
            write_raw_rows(raw_w, baseline_pre,
                           target_mm=0.0,
                           stage_actual_mm=stage.get_position() - center_abs,
                           sweep_index=-1, direction_tag="baseline_pre")
            agg_pre = aggregate(baseline_pre, target_mm=0.0,
                                stage_actual_mm=stage.get_position() - center_abs,
                                sweep_index=-1, direction_tag="baseline_pre")
            points_w.writerow([
                agg_pre.sweep_index, agg_pre.direction_tag,
                f"{agg_pre.target_mm:.6f}", f"{agg_pre.stage_actual_mm:.6f}",
                f"{agg_pre.mean_V:.8f}", f"{agg_pre.std_V:.8f}",
                agg_pre.n_samples,
                agg_pre.timestamp_start_us, agg_pre.timestamp_end_us,
            ])
            aggregates.append(agg_pre)
            log.info("  baseline pre: mean=%.6f V  std=%.2e V  (n=%d)",
                     agg_pre.mean_V, agg_pre.std_V, agg_pre.n_samples)

            # ---- 7. Main sweep --------------------------------------------
            is_bidirectional = cfg.direction == "bidirectional"
            n_fwd = (len(targets) // 2 + 1) if is_bidirectional else len(targets)

            for i, rel_mm in enumerate(targets):
                abs_mm = center_abs + rel_mm
                tag = "fwd" if (not is_bidirectional or i < n_fwd) else "rev"
                log.info("[%3d/%3d  %s] move → %+.3f mm (abs %.4f mm)",
                         i + 1, len(targets), tag, rel_mm, abs_mm)

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

                pt_samples = capture_point(reader, cfg.samples_per_point,
                                           cfg.drain_timeout_s)
                write_raw_rows(raw_w, pt_samples,
                               target_mm=rel_mm,
                               stage_actual_mm=stage_actual,
                               sweep_index=i, direction_tag=tag)
                agg = aggregate(pt_samples, target_mm=rel_mm,
                                stage_actual_mm=stage_actual,
                                sweep_index=i, direction_tag=tag)
                points_w.writerow([
                    agg.sweep_index, agg.direction_tag,
                    f"{agg.target_mm:.6f}", f"{agg.stage_actual_mm:.6f}",
                    f"{agg.mean_V:.8f}", f"{agg.std_V:.8f}",
                    agg.n_samples,
                    agg.timestamp_start_us, agg.timestamp_end_us,
                ])
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

            log.info("Collecting %d baseline samples at center (post)...",
                     cfg.baseline_samples)
            baseline_post = capture_point(reader, cfg.baseline_samples,
                                          cfg.drain_timeout_s)
            write_raw_rows(raw_w, baseline_post,
                           target_mm=0.0,
                           stage_actual_mm=stage.get_position() - center_abs,
                           sweep_index=-2, direction_tag="baseline_post")
            agg_post = aggregate(baseline_post, target_mm=0.0,
                                 stage_actual_mm=stage.get_position() - center_abs,
                                 sweep_index=-2, direction_tag="baseline_post")
            points_w.writerow([
                agg_post.sweep_index, agg_post.direction_tag,
                f"{agg_post.target_mm:.6f}", f"{agg_post.stage_actual_mm:.6f}",
                f"{agg_post.mean_V:.8f}", f"{agg_post.std_V:.8f}",
                agg_post.n_samples,
                agg_post.timestamp_start_us, agg_post.timestamp_end_us,
            ])
            aggregates.append(agg_post)

            raw_f.close()
            points_f.close()

            # ---- 9. Metadata ----------------------------------------------
            meta = build_metadata(cfg, paths, stage, dry_run=dry_run)
            meta["baseline_drift_V"] = agg_post.mean_V - agg_pre.mean_V
            meta["baseline_noise_V_pre"] = agg_pre.std_V
            meta["baseline_noise_V_post"] = agg_post.std_V
            with open(paths.meta_json, "w") as f:
                json.dump(meta, f, indent=2)
            drift = meta["baseline_drift_V"]
            log.info("Baseline drift: %+.6f V (|drift|/sigma = %.2f)",
                     drift, abs(drift) / max(agg_pre.std_V, 1e-12))
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
