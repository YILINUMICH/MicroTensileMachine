#!/usr/bin/env python3
"""
AD2 Continuous Logger
=====================

Streams both AD2 channels to a CSV at a user-specified rate (default
100 Hz) until stopped. Uses a timed polling loop driven by
time.perf_counter() rather than AD2's buffered record mode, since
buffered mode is overkill for <=100 Hz and makes synchronization with
other instruments (LCR meter, Zaber stage) harder.

CSV columns:
    count, timestamp_iso, t_elapsed_s, v_ch1_mV, v_ch2_mV

Notes:
- timestamp_iso is wall-clock (ISO 8601 with microseconds) for later
  cross-instrument alignment in Phase 2.
- t_elapsed_s is high-resolution perf_counter elapsed time from the
  start of acquisition - use this for jitter / rate analysis.
- Values are stored in mV for readability; divide by 1000 to get volts.

CLI:
    python ad2_continuous_log.py                    # 100 Hz, untimed
    python ad2_continuous_log.py --rate 200         # 200 Hz
    python ad2_continuous_log.py --duration 30      # 30 seconds then stop
    python ad2_continuous_log.py --output foo.csv   # custom path

Stop at any time with Ctrl-C - the CSV will be flushed and the AD2
handle closed cleanly.

Author: Yilin Ma
Date: April 2026
University of Michigan Robotics
HDR Lab
"""

import argparse
import csv
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from ad2_interface import AD2Scope


logger = logging.getLogger("AD2Logger")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _default_output_path() -> Path:
    """Return default CSV path: data/ad2_log_YYYYMMDD_HHMMSS.csv."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("data") / f"ad2_log_{stamp}.csv"


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuous AD2 data logger (2 channels, voltage)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--rate", type=float, default=100.0,
        help="Target sample rate in Hz",
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Stop after this many seconds (default: run until Ctrl-C)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output CSV path (default: data/ad2_log_<timestamp>.csv)",
    )
    parser.add_argument(
        "--ch1-range", type=float, default=5.0,
        help="CH1 full-scale range in volts",
    )
    parser.add_argument(
        "--ch2-range", type=float, default=5.0,
        help="CH2 full-scale range in volts",
    )
    parser.add_argument(
        "--stats-interval", type=float, default=2.0,
        help="Print live stats every N seconds (0 to disable)",
    )
    return parser.parse_args(argv)


# ----------------------------------------------------------------------
# Logger
# ----------------------------------------------------------------------
class ContinuousLogger:
    """
    Paced polling loop that writes AD2 samples to CSV.

    Separated into a class mainly so the SIGINT handler can flip a
    shared `_running` flag without relying on module globals.
    """

    CSV_HEADER = [
        "count",
        "timestamp_iso",
        "t_elapsed_s",
        "v_ch1_mV",
        "v_ch2_mV",
    ]

    def __init__(self,
                 scope: AD2Scope,
                 output_path: Path,
                 rate_hz: float,
                 duration_s=None,
                 stats_interval_s: float = 2.0):
        self.scope = scope
        self.output_path = output_path
        self.rate_hz = rate_hz
        self.duration_s = duration_s
        self.stats_interval_s = stats_interval_s

        self._period_s = 1.0 / rate_hz
        self._running = True
        self._sample_count = 0
        self._error_count = 0

    def request_stop(self, *_args):
        """SIGINT / SIGTERM handler: graceful shutdown."""
        if self._running:
            logger.info("Stop requested - finishing current sample and closing CSV...")
        self._running = False

    def run(self):
        """Main acquisition loop. Blocks until duration elapses or stop is requested."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Writing to {self.output_path}")
        logger.info(f"Target rate: {self.rate_hz:g} Hz "
                    f"(period {self._period_s*1000:.3f} ms)")
        if self.duration_s is not None:
            logger.info(f"Duration:    {self.duration_s:g} s")
        else:
            logger.info("Duration:    open-ended (Ctrl-C to stop)")

        with open(self.output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(self.CSV_HEADER)

            # Use a monotonic, high-resolution clock for pacing AND
            # elapsed time. time.sleep() alone drifts because it gives
            # "at least N seconds" - we correct for that below.
            t_start = time.perf_counter()
            next_deadline = t_start
            last_stats_t = t_start
            last_stats_count = 0

            while self._running:
                # Duration check
                now = time.perf_counter()
                if self.duration_s is not None and (now - t_start) >= self.duration_s:
                    break

                # Take a sample
                try:
                    v1, v2 = self.scope.read_single()
                except Exception as e:
                    self._error_count += 1
                    logger.warning(f"Read error #{self._error_count}: {e}")
                    # Schedule next deadline so one bad read doesn't
                    # cause a burst of catch-up samples.
                    next_deadline += self._period_s
                    # Short sleep to avoid spinning on persistent failure
                    time.sleep(self._period_s)
                    continue

                sample_t = time.perf_counter()
                elapsed = sample_t - t_start
                iso = datetime.now().isoformat(timespec="microseconds")

                self._sample_count += 1
                writer.writerow([
                    self._sample_count,
                    iso,
                    f"{elapsed:.6f}",
                    f"{v1*1000:.6f}",
                    f"{v2*1000:.6f}",
                ])

                # Live stats
                if self.stats_interval_s > 0 and \
                        (sample_t - last_stats_t) >= self.stats_interval_s:
                    window = sample_t - last_stats_t
                    window_count = self._sample_count - last_stats_count
                    eff_rate = window_count / window if window > 0 else 0.0
                    logger.info(
                        f"[live] n={self._sample_count}  "
                        f"rate={eff_rate:.2f} Hz  "
                        f"errors={self._error_count}  "
                        f"ch1={v1*1000:+8.2f} mV  "
                        f"ch2={v2*1000:+8.2f} mV"
                    )
                    # Flush so a crash doesn't lose the last few minutes
                    f.flush()
                    last_stats_t = sample_t
                    last_stats_count = self._sample_count

                # Pace: compute the next target deadline, sleep until
                # just before it, then spin briefly for sub-ms accuracy.
                next_deadline += self._period_s
                slack = next_deadline - time.perf_counter()
                if slack > 0.002:
                    time.sleep(slack - 0.001)
                while time.perf_counter() < next_deadline:
                    pass

            f.flush()

        # Summary
        total_elapsed = time.perf_counter() - t_start
        eff_rate = self._sample_count / total_elapsed if total_elapsed > 0 else 0.0
        logger.info("-" * 60)
        logger.info(f"Done. {self._sample_count} samples written to {self.output_path}")
        logger.info(f"Elapsed: {total_elapsed:.3f} s")
        logger.info(f"Effective rate: {eff_rate:.3f} Hz "
                    f"(target {self.rate_hz:g} Hz)")
        if self._error_count:
            logger.warning(f"{self._error_count} read errors during acquisition")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def main(argv=None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.rate <= 0:
        logger.error("--rate must be positive")
        return 2
    if args.rate > 1000:
        logger.warning(f"Requested rate {args.rate} Hz exceeds the design target "
                       f"of 100 Hz - USB latency may cause large jitter")

    output_path = Path(args.output) if args.output else _default_output_path()

    scope = AD2Scope(
        ch1_range_v=args.ch1_range,
        ch2_range_v=args.ch2_range,
    )
    if not scope.open():
        logger.error("Could not open AD2 - aborting")
        return 1

    cont_logger = ContinuousLogger(
        scope=scope,
        output_path=output_path,
        rate_hz=args.rate,
        duration_s=args.duration,
        stats_interval_s=args.stats_interval,
    )

    # Graceful Ctrl-C (SIGINT) and SIGTERM
    signal.signal(signal.SIGINT, cont_logger.request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, cont_logger.request_stop)
        except (ValueError, OSError):
            # SIGTERM can't be installed on Windows from non-main threads
            pass

    try:
        cont_logger.run()
    finally:
        scope.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
