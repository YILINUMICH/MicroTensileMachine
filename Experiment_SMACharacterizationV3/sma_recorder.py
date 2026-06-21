#!/usr/bin/env python3
"""
sma_recorder.py — entry point for a V3 SMA characterization session.

Loads config.yaml, starts the enabled workers (LCR, H7, Zaber stage),
then hands off to SessionController which walks the operator through
OPEN → SHORT → RAW.

Usage:
    python sma_recorder.py
    python sma_recorder.py --config alt_config.yaml
    python sma_recorder.py --session-id flexinol_run01

Output (per session):
    data/<session_id>/
        open_lcr.csv   open_h7.csv   open_stage.csv
        short_lcr.csv  short_h7.csv  short_stage.csv
        raw_lcr.csv    raw_h7.csv    raw_stage.csv
        meta.json
        session.log

The H7 CSVs carry ALL enabled channels (src=1 laser, 2 load, 3 SMA V,
4 SMA I, 5 SMA R) with a `channel` column — analyze_sma.py demuxes them.

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
from workers import (H7Sample, H7Worker, LcrSample, LcrWorker,
                     StageSample, ZaberWorker)


_THIS_DIR = Path(__file__).resolve().parent
QUEUE_MAXSIZE = 10_000


def _setup_logging(log_path: Path, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fh = logging.FileHandler(log_path, mode="w")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)-18s  %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [fh]


def _install_sigint_handler(stop_event: threading.Event) -> None:
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
        description="OPEN/SHORT/RAW multi-instrument recorder (SMA char. V3)")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"))
    p.add_argument("--session-id", default=None,
                   help="custom session dir name (default: sma_<timestamp>)")
    p.add_argument("-v", "--verbose", action="store_true")
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

    # Build only the enabled streams. A disabled stream is passed to the
    # controller as (None, None) and skipped everywhere.
    lcr_worker = lcr_q = None
    h7_worker = h7_q = None
    stage_worker = stage_q = None

    if cfg.lcr.enabled:
        lcr_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        lcr_worker = LcrWorker(cfg.lcr, lcr_q, stop_event)
    else:
        logging.info("LCR stream disabled in config")

    if cfg.h7.enabled:
        h7_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        h7_worker = H7Worker(cfg.h7, h7_q, stop_event)
    else:
        logging.info("H7 stream disabled in config")

    if cfg.stage.enabled:
        stage_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        stage_worker = ZaberWorker(cfg.stage, stage_q, stop_event)
    else:
        logging.info("Stage stream disabled in config")

    if not (lcr_worker or h7_worker or stage_worker):
        logging.error("No streams enabled — nothing to record.")
        print("ERROR: no streams enabled in config (lcr/h7/stage all disabled).",
              file=sys.stderr)
        sys.exit(2)

    for w in (lcr_worker, h7_worker, stage_worker):
        if w is not None:
            w.start()

    session = SessionController(
        cfg=cfg, paths=paths,
        lcr_worker=lcr_worker, h7_worker=h7_worker, stage_worker=stage_worker,
        lcr_queue=lcr_q, h7_queue=h7_q, stage_queue=stage_q,
        stop_event=stop_event,
    )
    sys.exit(session.run())


if __name__ == "__main__":
    _main()
