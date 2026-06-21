#!/usr/bin/env python3
"""
sma_recorder.py — entry point for an SMA characterization session.

Loads config.yaml, starts the LCR + H7 workers, then hands off to
SessionController which walks the operator through OPEN → SHORT → RAW.

Usage:
    python sma_recorder.py
    python sma_recorder.py --config alt_config.yaml
    python sma_recorder.py --session-id flexinol_run01      # custom dir name

Output (per session):
    data/<session_id>/
        open_lcr.csv      open_h7.csv
        short_lcr.csv     short_h7.csv
        raw_lcr.csv       raw_h7.csv
        meta.json
        session.log

Exit codes:
    0 — session completed
    1 — operator aborted
    2 — system error (worker crash, file IO, config, etc.)

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
from pathlib import Path

from config import AppConfig
from session import SessionController, make_session_paths
from workers import H7Sample, H7Worker, LcrSample, LcrWorker


_THIS_DIR = Path(__file__).resolve().parent

# Bounded queue: ~10000 samples ≈ 100 s of LCR or 25 s of H7. Comfortably
# more than any individual phase's duration. If the controller stalls
# longer than this, the workers drop samples (logged + counted in meta).
QUEUE_MAXSIZE = 10_000


def _setup_logging(log_path: Path, verbose: bool) -> None:
    """
    Send all logging to the session log file. The console is reserved
    for operator I/O — interleaving log lines with the \\r-overwrite
    progress bar would garble the display.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fh = logging.FileHandler(log_path, mode="w")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)-18s  %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    # Replace any existing handlers (e.g. from a prior import).
    root.handlers = [fh]


def _install_sigint_handler(stop_event: threading.Event) -> None:
    """
    First Ctrl+C: graceful stop (sets stop_event).
    Second Ctrl+C: restore default handler so the next Ctrl+C kills the
    process — escape hatch for an unresponsive session.
    """
    def _handler(_sig, _frm):
        if not stop_event.is_set():
            stop_event.set()
        else:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGINT, _handler)


def _resolve_output_dir(cfg_output_dir: str) -> Path:
    p = Path(cfg_output_dir)
    return p if p.is_absolute() else _THIS_DIR / p


def _main() -> None:
    p = argparse.ArgumentParser(
        description="OPEN/SHORT/RAW session recorder for SMA characterization")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"),
                   help="path to config.yaml")
    p.add_argument("--session-id", default=None,
                   help="custom session directory name "
                        "(default: sma_<YYYYMMDD_HHMMSS>)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG-level logging in session.log")
    args = p.parse_args()

    cfg = AppConfig.from_yaml(Path(args.config))
    output_dir = _resolve_output_dir(cfg.run.output_dir)
    paths = make_session_paths(output_dir, session_id=args.session_id)

    _setup_logging(paths.log_txt, args.verbose)
    logging.info("Session: %s", paths.session_id)
    logging.info("Output:  %s", paths.session_dir)
    logging.info("Config:  %s", args.config)

    stop_event = threading.Event()
    _install_sigint_handler(stop_event)

    lcr_q: "queue.Queue[LcrSample]" = queue.Queue(maxsize=QUEUE_MAXSIZE)
    h7_q: "queue.Queue[H7Sample]" = queue.Queue(maxsize=QUEUE_MAXSIZE)

    lcr_worker = LcrWorker(cfg.lcr, lcr_q, stop_event)
    h7_worker = H7Worker(cfg.h7, h7_q, stop_event)

    lcr_worker.start()
    h7_worker.start()

    session = SessionController(
        cfg=cfg, paths=paths,
        lcr_worker=lcr_worker, h7_worker=h7_worker,
        lcr_queue=lcr_q, h7_queue=h7_q,
        stop_event=stop_event,
    )
    sys.exit(session.run())


if __name__ == "__main__":
    _main()
