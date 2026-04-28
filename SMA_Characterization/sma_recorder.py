#!/usr/bin/env python3
"""
sma_recorder.py — concurrent LCR + laser recorder for SMA characterization.

Runs two independent threads:
  • LCR thread — polls Keysight E4980AL at ~10 ms intervals, writes raw
    (timestamp, primary, secondary, status) rows to <prefix>_lcr_raw.csv
  • Laser thread — streams ADS1263 samples via PortentaReader, writes raw
    (timestamp, firmware_timestamp_us, voltage_V, raw_code) rows to
    <prefix>_laser_raw.csv

Both timestamp columns are host wall-clock (time.time(), UTC seconds since
epoch) so the two streams can be joined on time in post-processing. The
laser CSV also retains the firmware-side microsecond timestamp for fine
temporal alignment within the laser stream.

No de-embedding, filtering, or calibration is applied — raw data only.
Post-hoc analysis applies:
  • LCR: open/short/load de-embedding (Notion methodology)
  • Laser: displacement conversion via k = -0.1171 mV/µm, V0 = 566.957 mV

Usage:
    python sma_recorder.py                  # uses config.yaml, duration=60 s
    python sma_recorder.py --duration 120   # override duration
    python sma_recorder.py --until-ctrl-c   # run until user interrupts

Author: Yilin Ma - HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import platform
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import yaml

from lcr_reader import LCRReader, LcrMeasurement

# Re-use the PortentaReader from the sibling Calibrate_LaserHead package.
_THIS_DIR = Path(__file__).resolve().parent
_CAL_DIR = _THIS_DIR.parent / "Calibrate_LaserHead"
if str(_CAL_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_DIR))

from portenta_reader import PortentaReader, Sample  # noqa: E402


# =============================================================================
# Config
# =============================================================================
@dataclass
class LcrConfig:
    resource: Optional[str]
    function: str
    frequency_hz: float
    voltage_V: float
    integration: str
    averaging: int
    poll_interval_s: float


@dataclass
class LaserConfig:
    port: str
    baud: int
    adc_source: int


@dataclass
class RunConfig:
    duration_s: Optional[float]
    operator: str
    notes: str
    output_prefix: Optional[str]


@dataclass
class AppConfig:
    lcr: LcrConfig
    laser: LaserConfig
    run: RunConfig

    @classmethod
    def from_yaml(cls, path: Path) -> "AppConfig":
        with open(path) as f:
            d = yaml.safe_load(f)
        return cls(
            lcr=LcrConfig(**d["lcr"]),
            laser=LaserConfig(**d["laser"]),
            run=RunConfig(**d["run"]),
        )


# =============================================================================
# Output path planning
# =============================================================================
@dataclass
class RunPaths:
    prefix: str
    lcr_csv: Path
    laser_csv: Path
    meta_json: Path


def make_run_paths(data_dir: Path, explicit_prefix: Optional[str],
                   run_type: str = "run") -> RunPaths:
    data_dir.mkdir(parents=True, exist_ok=True)
    if explicit_prefix:
        prefix = explicit_prefix
    else:
        # Prefix includes run_type so SHORT references are distinguishable
        # at a glance (e.g., sma_short_20260424_140000 vs sma_run_20260424_150000).
        prefix = f"sma_{run_type}_" + time.strftime("%Y%m%d_%H%M%S")
    return RunPaths(
        prefix=prefix,
        lcr_csv=data_dir / f"{prefix}_lcr_raw.csv",
        laser_csv=data_dir / f"{prefix}_laser_raw.csv",
        meta_json=data_dir / f"{prefix}_meta.json",
    )


# =============================================================================
# Worker threads
# =============================================================================
class LcrWorker(threading.Thread):
    """Polls the LCR in a loop; writes each measurement to the CSV."""

    def __init__(self, cfg: LcrConfig, csv_path: Path,
                 stop_event: threading.Event):
        super().__init__(name="LcrWorker", daemon=True)
        self.cfg = cfg
        self.csv_path = csv_path
        self.stop_event = stop_event
        self.n_samples = 0
        self.error: Optional[BaseException] = None
        self.logger = logging.getLogger("LcrWorker")

    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("LCR worker crashed: %s", e)

    def _main_loop(self) -> None:
        with LCRReader(
            resource=self.cfg.resource,
            function=self.cfg.function,
            frequency_hz=self.cfg.frequency_hz,
            voltage_V=self.cfg.voltage_V,
            integration=self.cfg.integration,
            averaging=self.cfg.averaging,
        ) as lcr:
            lcr.configure()
            with open(self.csv_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "host_timestamp_s",       # time.time()
                    "monotonic_s",            # time.monotonic()
                    "primary",                # Ls (H) in LSRS mode
                    "secondary",              # Rs (Ω) in LSRS mode
                    "status",                 # E4980 status byte
                ])
                for m in lcr.iter_measurements(
                        poll_interval_s=self.cfg.poll_interval_s):
                    w.writerow([
                        f"{m.timestamp:.6f}",
                        f"{m.monotonic:.6f}",
                        f"{m.primary:.8e}",
                        f"{m.secondary:.8f}",
                        m.status,
                    ])
                    self.n_samples += 1
                    # Flush periodically so a Ctrl+C mid-run keeps data.
                    if self.n_samples % 50 == 0:
                        f.flush()
                    if self.stop_event.is_set():
                        break
                f.flush()
        self.logger.info("LCR thread stopped after %d measurements",
                         self.n_samples)


class LaserWorker(threading.Thread):
    """Streams Portenta samples; writes each to the CSV."""

    def __init__(self, cfg: LaserConfig, csv_path: Path,
                 stop_event: threading.Event):
        super().__init__(name="LaserWorker", daemon=True)
        self.cfg = cfg
        self.csv_path = csv_path
        self.stop_event = stop_event
        self.n_samples = 0
        self.error: Optional[BaseException] = None
        self.logger = logging.getLogger("LaserWorker")

    def run(self) -> None:
        try:
            self._main_loop()
        except BaseException as e:
            self.error = e
            self.logger.exception("Laser worker crashed: %s", e)

    def _main_loop(self) -> None:
        reader = PortentaReader(
            port=self.cfg.port,
            baud=self.cfg.baud,
            adc_source=self.cfg.adc_source,
        )
        with reader:
            # Clear any stale/banner bytes left over from open().
            reader.drain()
            with open(self.csv_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "host_timestamp_s",       # time.time() at host-side parse
                    "monotonic_s",            # time.monotonic()
                    "firmware_timestamp_us",  # t_us from Portenta (uint32, wraps)
                    "voltage_V",              # voltage reported by firmware
                    "raw_code",               # ADC1 raw code (int32)
                ])
                for s in reader.iter_samples():
                    ts = time.time()
                    mono = time.monotonic()
                    w.writerow([
                        f"{ts:.6f}",
                        f"{mono:.6f}",
                        s.timestamp_us,
                        f"{s.voltage_V:.8f}",
                        s.raw_code if s.raw_code is not None else "",
                    ])
                    self.n_samples += 1
                    if self.n_samples % 200 == 0:
                        f.flush()
                    if self.stop_event.is_set():
                        break
                f.flush()
        self.logger.info("Laser thread stopped after %d samples",
                         self.n_samples)


# =============================================================================
# Metadata
# =============================================================================
def build_metadata(cfg: AppConfig, paths: RunPaths,
                   lcr_idn: Optional[str],
                   workers_started_at: float, ready_at: float,
                   ended_at: float, lcr_n: int, laser_n: int,
                   run_type: str = "run") -> dict:
    return {
        "run_prefix": paths.prefix,
        "run_type": run_type,
        "workers_started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(workers_started_at)),
        "ready_to_fire_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(ready_at)),
        "ended_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(ended_at)),
        "startup_s": ready_at - workers_started_at,
        "duration_s": ended_at - ready_at,
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "lcr": {**asdict(cfg.lcr), "idn": lcr_idn},
        "laser": asdict(cfg.laser),
        "run": asdict(cfg.run),
        "counts": {"lcr_measurements": lcr_n, "laser_samples": laser_n},
        "outputs": {
            "lcr_csv": paths.lcr_csv.name,
            "laser_csv": paths.laser_csv.name,
        },
        "notes_from_calibration": {
            "laser_k_mV_per_um": -0.1171,
            "laser_V0_mV": 566.957,
            "conversion": "displacement_um = (V_mV - V0_mV) / k",
            "calibration_source": "Calibrate_LaserHead/data/2026-04-24_run07_*",
        },
    }


# =============================================================================
# Main
# =============================================================================
def run(cfg: AppConfig, duration_override_s: Optional[float],
        until_ctrl_c: bool, run_type: str = "run") -> None:
    log = logging.getLogger("sma_recorder")

    # Resolve paths.
    paths = make_run_paths(_THIS_DIR / "data", cfg.run.output_prefix,
                           run_type=run_type)
    log.info("Run type: %s", run_type)
    log.info("Writing run outputs with prefix %s", paths.prefix)
    log.info("  LCR   → %s", paths.lcr_csv.name)
    log.info("  Laser → %s", paths.laser_csv.name)

    # Duration handling.
    if until_ctrl_c:
        duration = None
    elif duration_override_s is not None:
        duration = duration_override_s
    else:
        duration = cfg.run.duration_s
    if duration is None:
        log.info("Duration: until Ctrl+C")
    else:
        log.info("Duration: %.1f s", duration)

    stop_event = threading.Event()

    def _sigint(_sig, _frm):
        log.info("SIGINT received — stopping workers...")
        stop_event.set()
    signal.signal(signal.SIGINT, _sigint)

    # Start workers.
    lcr_worker = LcrWorker(cfg.lcr, paths.lcr_csv, stop_event)
    laser_worker = LaserWorker(cfg.laser, paths.laser_csv, stop_event)
    workers_started_at = time.time()
    lcr_worker.start()
    laser_worker.start()

    # Wait until BOTH streams are producing real samples before declaring
    # ready. This absorbs LCR connection + laser boot-wait overhead; only
    # after we see live data do we print the "fire" banner and start the
    # duration timer, so actuation is synced with actual recording.
    log.info("Waiting for both streams to come online...")
    ready_deadline = time.monotonic() + 20.0
    while time.monotonic() < ready_deadline:
        if (lcr_worker.n_samples > 0 and laser_worker.n_samples > 0):
            break
        if stop_event.is_set():
            break
        if lcr_worker.error is not None or laser_worker.error is not None:
            log.error("A worker crashed before becoming ready.")
            stop_event.set()
            break
        time.sleep(0.1)
    else:
        log.error("Timed out waiting for both streams (lcr=%d, laser=%d). "
                  "Check instruments and connections.",
                  lcr_worker.n_samples, laser_worker.n_samples)
        stop_event.set()

    # Load the "go" gun before the banner.
    started_at = time.time()   # this is the ready-to-fire wall-clock time

    if not stop_event.is_set():
        # Loud, obvious banner — visible even at a glance on a cluttered log.
        log.info("")
        log.info("┌" + "─" * 58 + "┐")
        log.info("│%s│" % "  READY — APPLY ACTUATION CURRENT NOW".center(58))
        log.info("│%s│" % (f"  LCR n={lcr_worker.n_samples}, laser n={laser_worker.n_samples} "
                            f"at t=0").center(58))
        log.info("└" + "─" * 58 + "┘")
        log.info("")

    # Main loop: wait for duration or for a worker to die.
    try:
        t_start = time.monotonic()
        while not stop_event.is_set():
            if duration is not None and (time.monotonic() - t_start) >= duration:
                log.info("Duration reached — stopping.")
                stop_event.set()
                break
            # Abort if either worker crashed.
            if lcr_worker.error is not None or laser_worker.error is not None:
                log.error("A worker crashed — stopping.")
                stop_event.set()
                break
            time.sleep(0.2)
    finally:
        stop_event.set()
        lcr_worker.join(timeout=5.0)
        laser_worker.join(timeout=5.0)
        ended_at = time.time()

    # Write meta.json.
    meta = build_metadata(
        cfg, paths,
        lcr_idn=None,
        workers_started_at=workers_started_at,
        ready_at=started_at,
        ended_at=ended_at,
        lcr_n=lcr_worker.n_samples, laser_n=laser_worker.n_samples,
        run_type=run_type,
    )
    with open(paths.meta_json, "w") as f:
        json.dump(meta, f, indent=2)

    log.info("Done. Wrote %d LCR rows, %d laser samples.",
             lcr_worker.n_samples, laser_worker.n_samples)

    # Surface worker errors at exit so they aren't buried in the log.
    for worker in (lcr_worker, laser_worker):
        if worker.error is not None:
            log.error("%s error: %r", worker.name, worker.error)
            sys.exit(2)


def _main() -> None:
    p = argparse.ArgumentParser(
        description="Concurrent LCR + laser recorder for SMA characterization")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"),
                   help="path to config.yaml")
    p.add_argument("--duration", type=float, default=None,
                   help="override config.run.duration_s (seconds)")
    p.add_argument("--until-ctrl-c", action="store_true",
                   help="ignore duration and run until SIGINT")
    p.add_argument("--run-type", default="run",
                   choices=["run", "short", "open"],
                   help="labels the output files and meta.json. "
                        "'short' = short-circuit reference for de-embedding; "
                        "'open' = open-circuit reference; "
                        "'run' = actual experiment (default).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    )

    cfg = AppConfig.from_yaml(Path(args.config))
    run(cfg, duration_override_s=args.duration, until_ctrl_c=args.until_ctrl_c,
        run_type=args.run_type)


if __name__ == "__main__":
    _main()
