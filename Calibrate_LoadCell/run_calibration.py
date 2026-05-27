#!/usr/bin/env python3
"""
run_calibration.py — Load cell (LCA-9PC) calibration via spring transfer standard.

The Zaber stage applies known displacements to a characterised BMX spring
(k ≈ 30.86 mN/mm from Instron). At each position the LCA-9PC voltage is
captured via the ADS1263 on AIN2/AIN3. Force is computed as F = k_spring * dx.

Pipeline:
    1. Connect Portenta (ADC stream) and Zaber stage.
    2. Baseline at sweep_start_mm (near zero load).
    3. Step through the spring deflection range, capturing voltage at each point.
    4. Post-baseline at sweep_start_mm (drift check).
    5. Write raw.csv, points.csv, meta.json under ./data/.

The sweep starts a few mm before expected spring engagement so that
analyze.py can auto-detect the linear region and exclude the soft zone.

Use --dry-run for a quick 3-point sanity pass before committing bench time.

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

# Gravitational constant for gf → mN conversion
GF_TO_MN = 9.80665  # 1 gf = 9.80665 mN


# =============================================================================
# Config
# =============================================================================
@dataclass
class SweepConfig:
    # Spring
    spring_k_mN_per_mm: float
    spring_k_uncertainty_pct: float
    # Sweep geometry
    sweep_start_mm: float               # absolute Zaber position
    max_force_gf: float                 # target ceiling in gram-force
    step_size_mm: float
    direction: str
    passes: int
    # Cross-compare
    xcompare: bool
    # Timing
    settle_time_s: float
    stage_velocity_mm_s: float
    # Sampling
    samples_per_point: int
    baseline_samples: int
    drain_timeout_s: float
    # Serial
    portenta_port: Optional[str]
    portenta_baud: int
    zaber_config_path: str
    zaber_port: Optional[str]
    # Metadata
    operator: str
    notes: str

    @classmethod
    def from_yaml(cls, path: Path, dry_run: bool = False) -> "SweepConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)
        if dry_run and "dry_run" in raw:
            raw.update(raw["dry_run"])
        raw.pop("dry_run", None)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def max_force_mN(self) -> float:
        return self.max_force_gf * GF_TO_MN

    @property
    def sweep_length_mm(self) -> float:
        return self.max_force_mN / self.spring_k_mN_per_mm

    def target_displacements_mm(self) -> List[float]:
        """Relative displacements from sweep_start_mm (0 = start)."""
        n_steps = max(1, int(round(self.sweep_length_mm / self.step_size_mm)))
        forward = [i * self.step_size_mm for i in range(n_steps + 1)]
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
    displacement_mm: float              # relative to sweep_start
    stage_actual_mm: float              # absolute stage position
    expected_force_mN: float            # spring_k * displacement
    mean_V: float                       # PRIMARY channel (ADC1)
    std_V: float
    n_samples: int
    timestamp_start_us: int
    timestamp_end_us: int
    sweep_index: int
    direction_tag: str = "fwd"
    pass_index: int = 0
    # Cross-compare secondary (ADC2). None in single-channel mode.
    mean_V_adc2: Optional[float] = None
    std_V_adc2: Optional[float] = None
    n_samples_adc2: Optional[int] = None


def aggregate(samples: List[Sample], displacement_mm: float,
              stage_actual_mm: float, expected_force_mN: float,
              sweep_index: int, direction_tag: str,
              pass_index: int = 0) -> PointAggregate:
    if not samples:
        raise RuntimeError(
            f"no samples captured at displacement {displacement_mm:.3f} mm — "
            "check the Portenta stream")
    vs = [s.voltage_V for s in samples]
    return PointAggregate(
        displacement_mm=displacement_mm,
        stage_actual_mm=stage_actual_mm,
        expected_force_mN=expected_force_mN,
        mean_V=statistics.fmean(vs),
        std_V=statistics.pstdev(vs) if len(vs) > 1 else 0.0,
        n_samples=len(vs),
        timestamp_start_us=samples[0].timestamp_us,
        timestamp_end_us=samples[-1].timestamp_us,
        sweep_index=sweep_index,
        direction_tag=direction_tag,
        pass_index=pass_index,
    )


def aggregate_dual(samples_adc1: List[Sample], samples_adc2: List[Sample],
                   displacement_mm: float, stage_actual_mm: float,
                   expected_force_mN: float, sweep_index: int,
                   direction_tag: str, pass_index: int = 0) -> PointAggregate:
    """
    Aggregate a dual-ADC capture. ADC1 (32-bit) is the primary channel
    for the load cell; ADC2 (24-bit) is the cross-compare secondary.
    """
    if not samples_adc1:
        raise RuntimeError(
            f"no ADC1 samples at displacement {displacement_mm:.3f} mm — "
            "is ENABLE_ADC1=1 in the firmware?")
    if not samples_adc2:
        raise RuntimeError(
            f"no ADC2 samples at displacement {displacement_mm:.3f} mm — "
            "is ENABLE_ADC2=1 in the firmware?")

    # Primary aggregate from ADC1
    agg = aggregate(samples_adc1,
                    displacement_mm=displacement_mm,
                    stage_actual_mm=stage_actual_mm,
                    expected_force_mN=expected_force_mN,
                    sweep_index=sweep_index, direction_tag=direction_tag,
                    pass_index=pass_index)

    # Secondary stats from ADC2
    vs2 = [s.voltage_V for s in samples_adc2]
    agg.mean_V_adc2 = statistics.fmean(vs2)
    agg.std_V_adc2 = statistics.pstdev(vs2) if len(vs2) > 1 else 0.0
    agg.n_samples_adc2 = len(vs2)
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
def write_raw_rows(writer, samples: List[Sample], *, displacement_mm: float,
                   stage_actual_mm: float, expected_force_mN: float,
                   sweep_index: int, direction_tag: str,
                   pass_index: int = 0) -> None:
    for s in samples:
        writer.writerow([
            pass_index, sweep_index, direction_tag,
            f"{displacement_mm:.6f}", f"{stage_actual_mm:.6f}",
            f"{expected_force_mN:.4f}",
            s.timestamp_us, f"{s.voltage_V:.8f}",
            s.raw_code if s.raw_code is not None else "",
            s.adc_source if s.adc_source is not None else "",
        ])


def capture_point(reader: PortentaReader, n: int,
                  drain_timeout_s: float) -> List[Sample]:
    reader.drain(max_time_s=drain_timeout_s)
    timeout = max(10.0, n / 50.0)
    return reader.read_samples(n=n, timeout_s=timeout)


def capture_point_dual(reader: PortentaReader, n_per_adc: int,
                       drain_timeout_s: float
                       ) -> "tuple[List[Sample], List[Sample]]":
    reader.drain(max_time_s=drain_timeout_s)
    timeout = max(20.0, n_per_adc / 25.0)
    return reader.read_samples_dual(n_per_adc=n_per_adc, timeout_s=timeout)


# =============================================================================
# Metadata
# =============================================================================
def firmware_git_hash() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=_THIS_DIR.parent, stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
    except Exception:
        return None


def build_metadata(cfg: SweepConfig, paths: RunPaths, stage,
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
            "repo_git_hash": firmware_git_hash(),
            "firmware_path": "Calibrate_LoadCell/Calibrate_Loadcell_PIO/src/main.cpp",
            "firmware_build": (
                "dual-ADC xcompare (ENABLE_ADC1=1, ENABLE_ADC2=1) — "
                "Calibrate_Loadcell_PIO"
            ),
            "xcompare_mode": cfg.xcompare,
            "primary_adc": 1,
            "primary_adc_bits": 32,
            "nominal_sps": 400,
            "filter": "Sinc3",
            "inpmux_hex": "0x23",
            "adc2mux_hex": "0x23",
            "reference": "external REF7050 on AIN0/AIN1",
            "reference_V": 5.0,
            "gain": 1,
            "signal_chain": (
                "Load cell → LCA-9PC amp (0-5 V) → "
                "EVM AIN2(+) / AIN3(-) → ADC1 (32-bit, 400 SPS, Sinc3, "
                "REF7050 5V, gain=1) → M4 ring buffer → M7 USB-CDC → host"
            ),
        },
        "spring": {
            "k_mN_per_mm": cfg.spring_k_mN_per_mm,
            "k_uncertainty_pct": cfg.spring_k_uncertainty_pct,
            "source": "Instron 2530-100N characterisation (doc/spring loadcell/)",
            "max_force_gf": cfg.max_force_gf,
            "max_force_mN": cfg.max_force_mN,
            "sweep_length_mm": cfg.sweep_length_mm,
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

    port = cfg.portenta_port
    if not port:
        log.error("portenta_port is not set in config.yaml")
        sys.exit(2)

    # ---- Connect Zaber stage ------------------------------------------------
    log.info("Connecting Zaber stage (config: %s)", cfg.zaber_config_path)
    stage = ZaberStage(config_file=cfg.zaber_config_path)
    stage.load_config(cfg.zaber_config_path)
    if cfg.zaber_port:
        stage.port = cfg.zaber_port
        log.info("Zaber port override: %s", cfg.zaber_port)
    if not stage.connect():
        log.error("Could not connect to Zaber stage on %s", stage.port)
        sys.exit(3)

    try:
        if not stage.is_homed():
            log.info("Homing stage...")
            if not stage.home():
                log.error("Homing failed.")
                sys.exit(4)

        start_abs = cfg.sweep_start_mm
        end_abs = start_abs + cfg.sweep_length_mm
        log.info("Sweep: %.3f → %.3f mm absolute (%.1f mm travel, "
                 "target %.1f gf = %.1f mN)",
                 start_abs, end_abs, cfg.sweep_length_mm,
                 cfg.max_force_gf, cfg.max_force_mN)

        # Move to sweep start
        log.info("Moving to sweep start: %.3f mm", start_abs)
        if not stage.move_to(start_abs):
            log.error("stage.move_to(%.4f) failed", start_abs)
            sys.exit(4)
        while stage.is_moving():
            time.sleep(0.01)
        time.sleep(cfg.settle_time_s)

        # ---- Plan the sweep -------------------------------------------------
        targets = cfg.target_displacements_mm()
        is_bidirectional = cfg.direction == "bidirectional"
        n_fwd = (len(targets) // 2 + 1) if is_bidirectional else len(targets)
        n_passes = max(1, cfg.passes)
        total_points = n_passes * len(targets)

        log.info("  %d positions per pass, step=%.2f mm, %d pass(es), "
                 "%d total points",
                 len(targets), cfg.step_size_mm, n_passes, total_points)

        # ---- Connect Portenta -----------------------------------------------
        # ADC1 is primary for load cell. In single-channel mode, filter for
        # adc_source=1. In xcompare mode, pass adc_source=2 to
        # PortentaReader (it's only used by read_samples, not
        # read_samples_dual which captures both).
        adc_src = 1 if not cfg.xcompare else 1
        log.info("Opening Portenta on %s @ %d (adc_source=%s)",
                 port, cfg.portenta_baud, adc_src)
        with PortentaReader(port=port, baud=cfg.portenta_baud,
                            adc_source=adc_src) as reader:

            paths = next_run_paths(_THIS_DIR / "data")
            log.info("Run prefix: %s", paths.prefix)

            # ---- Open output files ------------------------------------------
            raw_f = open(paths.raw_csv, "w", newline="")
            raw_w = csv.writer(raw_f)
            raw_w.writerow([
                "pass_index", "sweep_index", "direction",
                "displacement_mm", "stage_actual_mm", "expected_force_mN",
                "timestamp_us", "voltage_V", "raw_code", "adc_source",
            ])

            points_f = open(paths.points_csv, "w", newline="")
            points_w = csv.writer(points_f)
            points_w.writerow([
                "pass_index", "sweep_index", "direction",
                "displacement_mm", "stage_actual_mm", "expected_force_mN",
                "mean_V", "std_V", "n_samples",
                "timestamp_start_us", "timestamp_end_us",
                "mean_V_adc2", "std_V_adc2", "n_samples_adc2",
            ])

            def _write_point_row(agg: PointAggregate) -> None:
                points_w.writerow([
                    agg.pass_index, agg.sweep_index, agg.direction_tag,
                    f"{agg.displacement_mm:.6f}", f"{agg.stage_actual_mm:.6f}",
                    f"{agg.expected_force_mN:.4f}",
                    f"{agg.mean_V:.8f}", f"{agg.std_V:.8f}",
                    agg.n_samples,
                    agg.timestamp_start_us, agg.timestamp_end_us,
                    (f"{agg.mean_V_adc2:.8f}" if agg.mean_V_adc2 is not None else ""),
                    (f"{agg.std_V_adc2:.8f}" if agg.std_V_adc2 is not None else ""),
                    (agg.n_samples_adc2 if agg.n_samples_adc2 is not None else ""),
                ])

            def _capture_aggregate_write(*, displacement_mm: float,
                                         stage_actual_mm: float,
                                         expected_force_mN: float,
                                         sweep_index: int,
                                         direction_tag: str,
                                         pass_index: int,
                                         n_samples: int) -> PointAggregate:
                raw_kwargs = dict(
                    displacement_mm=displacement_mm,
                    stage_actual_mm=stage_actual_mm,
                    expected_force_mN=expected_force_mN,
                    sweep_index=sweep_index,
                    direction_tag=direction_tag,
                    pass_index=pass_index,
                )
                agg_kwargs = dict(
                    displacement_mm=displacement_mm,
                    stage_actual_mm=stage_actual_mm,
                    expected_force_mN=expected_force_mN,
                    sweep_index=sweep_index,
                    direction_tag=direction_tag,
                    pass_index=pass_index,
                )
                if cfg.xcompare:
                    s1, s2 = capture_point_dual(reader, n_samples,
                                                cfg.drain_timeout_s)
                    write_raw_rows(raw_w, s1, **raw_kwargs)
                    write_raw_rows(raw_w, s2, **raw_kwargs)
                    return aggregate_dual(s1, s2, **agg_kwargs)
                else:
                    samples = capture_point(reader, n_samples,
                                            cfg.drain_timeout_s)
                    write_raw_rows(raw_w, samples, **raw_kwargs)
                    return aggregate(samples, **agg_kwargs)

            aggregates: List[PointAggregate] = []

            # ---- Baseline before sweep (at start position) ------------------
            log.info("Collecting %d baseline samples at start (%.3f mm)%s...",
                     cfg.baseline_samples, start_abs,
                     " [xcompare]" if cfg.xcompare else "")
            agg_pre = _capture_aggregate_write(
                displacement_mm=0.0,
                stage_actual_mm=stage.get_position(),
                expected_force_mN=0.0,
                sweep_index=-1, direction_tag="baseline_pre",
                pass_index=-1, n_samples=cfg.baseline_samples,
            )
            _write_point_row(agg_pre)
            aggregates.append(agg_pre)
            log.info("  baseline pre: mean=%.6f V  std=%.2e V  (n=%d)",
                     agg_pre.mean_V, agg_pre.std_V, agg_pre.n_samples)

            # ---- Main sweep -------------------------------------------------
            log.info("Running %d pass(es) × %d points = %d total",
                     n_passes, len(targets), total_points)

            for pass_idx in range(n_passes):
                log.info("──── PASS %d / %d ────", pass_idx + 1, n_passes)

                for i, disp_mm in enumerate(targets):
                    abs_mm = start_abs + disp_mm
                    force_mN = cfg.spring_k_mN_per_mm * disp_mm
                    tag = "fwd" if (not is_bidirectional or i < n_fwd) else "rev"
                    global_idx = pass_idx * len(targets) + i + 1
                    log.info("[%3d/%3d  p%d %s] → disp %+.2f mm  "
                             "(abs %.3f mm, F ≈ %.1f mN / %.1f gf)",
                             global_idx, total_points,
                             pass_idx + 1, tag,
                             disp_mm, abs_mm,
                             force_mN, force_mN / GF_TO_MN)

                    if not stage.move_to(abs_mm):
                        log.error("stage.move_to(%.4f) failed", abs_mm)
                        sys.exit(5)
                    t_move_start = time.monotonic()
                    while stage.is_moving():
                        if time.monotonic() - t_move_start > 30.0:
                            log.error("stage move timed out")
                            sys.exit(6)
                        time.sleep(0.01)

                    time.sleep(cfg.settle_time_s)
                    actual_abs = stage.get_position()

                    agg = _capture_aggregate_write(
                        displacement_mm=disp_mm,
                        stage_actual_mm=actual_abs,
                        expected_force_mN=force_mN,
                        sweep_index=i, direction_tag=tag,
                        pass_index=pass_idx,
                        n_samples=cfg.samples_per_point,
                    )
                    _write_point_row(agg)
                    aggregates.append(agg)
                    raw_f.flush()
                    points_f.flush()

            # ---- Return to start and post-baseline --------------------------
            log.info("Returning to start (%.3f mm)...", start_abs)
            stage.move_to(start_abs)
            while stage.is_moving():
                time.sleep(0.01)
            time.sleep(cfg.settle_time_s)

            log.info("Collecting %d post-baseline samples...",
                     cfg.baseline_samples)
            agg_post = _capture_aggregate_write(
                displacement_mm=0.0,
                stage_actual_mm=stage.get_position(),
                expected_force_mN=0.0,
                sweep_index=-2, direction_tag="baseline_post",
                pass_index=-1, n_samples=cfg.baseline_samples,
            )
            _write_point_row(agg_post)
            aggregates.append(agg_post)

            raw_f.close()
            points_f.close()

            # ---- Metadata ---------------------------------------------------
            meta = build_metadata(cfg, paths, stage, dry_run=dry_run)
            meta["baseline_drift_V"] = agg_post.mean_V - agg_pre.mean_V
            meta["baseline_noise_V_pre"] = agg_pre.std_V
            meta["baseline_noise_V_post"] = agg_post.std_V
            if cfg.xcompare and agg_pre.mean_V_adc2 is not None \
                    and agg_post.mean_V_adc2 is not None:
                meta["baseline_drift_V_adc2"] = (
                    agg_post.mean_V_adc2 - agg_pre.mean_V_adc2
                )
            with open(paths.meta_json, "w") as f:
                json.dump(meta, f, indent=2)
            drift = meta["baseline_drift_V"]
            log.info("Baseline drift: %+.6f V  (|drift|/sigma = %.2f)",
                     drift, abs(drift) / max(agg_pre.std_V, 1e-12))
            log.info("Done. Next: python analyze.py %s", paths.points_csv)

    finally:
        try:
            stage.disconnect()
        except Exception:
            pass


# =============================================================================
# Entry point
# =============================================================================
def _main() -> None:
    p = argparse.ArgumentParser(
        description="Run a load cell calibration sweep via spring transfer standard")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"),
                   help="path to config.yaml")
    p.add_argument("--dry-run", action="store_true",
                   help="use config.yaml dry_run overrides")
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
