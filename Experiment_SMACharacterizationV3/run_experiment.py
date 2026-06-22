#!/usr/bin/env python3
"""
run_experiment.py — non-interactive SMA actuation experiment runner (V3).

The one-shot complement to sma_recorder.py: no OPEN/SHORT phases, no
operator prompts. It configures the instruments, drives the firmware's
on-M7 actuation (a `cycle` profile or a single `drive`), logs every enabled
stream to CSV with a watchdog heartbeat, then runs the analyzer.

It REUSES the existing machinery (config, workers, the reader's
send_command, the analyzer) — it is orchestration only, not a parallel
recorder. The CSV schema matches sma_recorder.py so analyze_sma.py works
unchanged.

Streams: H7 is always recorded (it carries the SMA data + the command
channel). LCR and the Zaber stage are recorded only if their `enabled:` is
true in config — so the same script scales from an H7-only resistor smoke
test to a full LCR + stage + SMA experiment with no code change.

Usage:
    # cycle defined in config.yaml's sma: block
    python run_experiment.py

    # override the cycle on the CLI
    python run_experiment.py --v-high 3.0 --fire-ms 1500 --cool-ms 6000 --n 5

    # a single drive instead of a cycle (good for the resistor smoke test)
    python run_experiment.py --drive 2.0 --hold-ms 1000

    # continuous cycle (n=0) for a fixed wall-clock window
    python run_experiment.py --n 0 --duration 60

    # see what would run without touching hardware
    python run_experiment.py --drive 2.0 --hold-ms 1000 --dry-run

Output: data/exp_<timestamp>/  with raw_h7.csv (+ raw_lcr.csv / raw_stage.csv
if those streams are enabled), meta.json, session.log, and the analyzer's
raw_dashboard.png + raw_joined.csv.

Exit codes: 0 ok · 1 aborted (Ctrl+C) · 2 system error (no H7, worker crash).

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import platform
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from config import AppConfig
from session import make_session_paths
from workers import (H7Sample, H7Worker, LcrSample, LcrWorker,
                     StageSample, ZaberWorker)


_THIS_DIR = Path(__file__).resolve().parent
QUEUE_MAXSIZE = 10_000
DRAIN_TICK_S = 0.05            # 20 Hz log loop
PING_PERIOD_S = 1.0           # heartbeat cadence (watchdog default is 5 s)
SETTLE_MARGIN_S = 2.0         # extra logging tail after the programmed run
HEALTH_TIMEOUT_S = 10.0
HEALTH_MIN_H7 = 10


# ---------------------------------------------------------------------------
# Planning (mode + command + duration) — pure, unit-testable, no hardware
# ---------------------------------------------------------------------------
def build_plan(cfg: AppConfig, args) -> dict:
    """Resolve config + CLI overrides into the actuation command + duration."""
    wdt_ms = args.wdt if args.wdt is not None else cfg.sma.wdt_ms

    if args.drive is not None:
        hold_ms = args.hold_ms if args.hold_ms is not None else 1000
        return {
            "mode": "drive",
            "command": f"drive {args.drive} {int(hold_ms)}",
            "planned_s": hold_ms / 1000.0 + SETTLE_MARGIN_S,
            "wdt_ms": wdt_ms,
            "params": {"v": args.drive, "hold_ms": int(hold_ms)},
        }

    # cycle (default) — overrides fall back to the config sma: block
    vh = args.v_high if args.v_high is not None else cfg.sma.v_high
    vl = args.v_low if args.v_low is not None else cfg.sma.v_low
    fire = args.fire_ms if args.fire_ms is not None else cfg.sma.fire_ms
    cool = args.cool_ms if args.cool_ms is not None else cfg.sma.cool_ms
    n = args.n if args.n is not None else cfg.sma.n_cycles
    if n > 0:
        planned = n * (fire + cool) / 1000.0 + SETTLE_MARGIN_S
    else:
        planned = float(args.duration) if args.duration else None  # None → until Ctrl+C
    return {
        "mode": "cycle",
        "command": f"cycle {vh} {vl} {int(fire)} {int(cool)} {int(n)}",
        "planned_s": planned,
        "wdt_ms": wdt_ms,
        "params": {"v_high": vh, "v_low": vl, "fire_ms": int(fire),
                   "cool_ms": int(cool), "n_cycles": int(n)},
    }


# ---------------------------------------------------------------------------
# CSV drains (mirror sma_recorder.py columns so analyze_sma.py is unchanged)
# ---------------------------------------------------------------------------
def _drain_h7(q, writer) -> int:
    if writer is None or q is None:
        return 0
    n = 0
    try:
        while True:
            s: H7Sample = q.get_nowait()
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


def _drain_lcr(q, writer) -> int:
    if writer is None or q is None:
        return 0
    n = 0
    try:
        while True:
            s: LcrSample = q.get_nowait()
            writer.writerow([f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                             f"{s.primary:.8e}", f"{s.secondary:.8f}", s.status])
            n += 1
    except queue.Empty:
        pass
    return n


def _drain_stage(q, writer) -> int:
    if writer is None or q is None:
        return 0
    n = 0
    try:
        while True:
            s: StageSample = q.get_nowait()
            writer.writerow([f"{s.host_timestamp_s:.6f}", f"{s.monotonic_s:.6f}",
                             f"{s.position_mm:.6f}"])
            n += 1
    except queue.Empty:
        pass
    return n


def _discard(q) -> int:
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


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------
def _setup_logging(log_path: Path, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # encoding=utf-8 — the default on Windows is cp1252, which can't encode
    # non-ASCII (e.g. arrows/box chars) and raises UnicodeEncodeError.
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)-16s  %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [fh]


def _install_sigint(stop_event: threading.Event) -> None:
    def _handler(_sig, _frm):
        if not stop_event.is_set():
            stop_event.set()
        else:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGINT, _handler)


def _resolve_output_dir(cfg_output_dir: str) -> Path:
    p = Path(cfg_output_dir)
    return p if p.is_absolute() else _THIS_DIR / p


def _h7_send(h7_worker: Optional[H7Worker], cmd: str, log) -> bool:
    reader = getattr(h7_worker, "reader", None)
    if reader is None:
        return False
    try:
        reader.send_command(cmd)
        log.info("H7 <- %s", cmd)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("send_command(%r) failed: %s", cmd, e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Non-interactive SMA actuation experiment (drive/log/analyze)")
    p.add_argument("--config", default=str(_THIS_DIR / "config.yaml"))
    p.add_argument("--session-id", default=None,
                   help="custom dir name (default: exp_<timestamp>)")
    # cycle overrides
    p.add_argument("--v-high", type=float, default=None)
    p.add_argument("--v-low", type=float, default=None)
    p.add_argument("--fire-ms", type=int, default=None)
    p.add_argument("--cool-ms", type=int, default=None)
    p.add_argument("--n", type=int, default=None, help="cycles (0 = continuous)")
    p.add_argument("--duration", type=float, default=None,
                   help="wall-clock seconds for n=0 continuous cycling")
    # single-drive mode
    p.add_argument("--drive", type=float, default=None,
                   help="single drive at this voltage instead of a cycle")
    p.add_argument("--hold-ms", type=int, default=None, help="drive hold (with --drive)")
    # misc
    p.add_argument("--wdt", type=int, default=None, help="M7 watchdog ms (0=off)")
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit (no hardware)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    cfg = AppConfig.from_yaml(Path(args.config))
    plan = build_plan(cfg, args)

    # ---- dry run: show the plan, touch nothing ----
    if args.dry_run:
        print("\n=== run_experiment plan (dry run) ===")
        print(f"  mode      : {plan['mode']}")
        print(f"  command   : {plan['command']}")
        print(f"  wdt_ms    : {plan['wdt_ms']}")
        dur = plan["planned_s"]
        print(f"  duration  : {('%.1f s' % dur) if dur else 'until Ctrl+C (n=0)'}")
        streams = ["h7"] + (["lcr"] if cfg.lcr.enabled else []) \
                         + (["stage"] if cfg.stage.enabled else [])
        print(f"  streams   : {streams}")
        print("=====================================\n")
        return 0

    if not cfg.h7.enabled:
        print("ERROR: h7.enabled is false — run_experiment needs the H7 stream "
              "(it carries the SMA data + command channel).", file=sys.stderr)
        return 2

    output_dir = _resolve_output_dir(cfg.run.output_dir)
    sid = args.session_id or ("exp_" + time.strftime("%Y%m%d_%H%M%S"))
    paths = make_session_paths(output_dir, session_id=sid)
    _setup_logging(paths.log_txt, args.verbose)
    log = logging.getLogger("run_experiment")
    log.info("Experiment %s -> %s", paths.session_id, paths.session_dir)
    log.info("Plan: %s | wdt=%s | planned=%s s",
             plan["command"], plan["wdt_ms"], plan["planned_s"])

    stop_event = threading.Event()
    _install_sigint(stop_event)

    # ---- build enabled workers (H7 required; LCR/stage if enabled) ----
    # H7 is critical and uses the main stop_event. LCR + stage are
    # AUXILIARY: they get a SEPARATE event so that if one crashes (e.g. the
    # LCR isn't connected during a resistor test) it does NOT abort the SMA
    # run — its CSV just stays empty and we log a warning.
    aux_stop = threading.Event()
    h7_q: "queue.Queue[H7Sample]" = queue.Queue(maxsize=QUEUE_MAXSIZE)
    h7_worker = H7Worker(cfg.h7, h7_q, stop_event)
    lcr_q = lcr_worker = None
    stage_q = stage_worker = None
    if cfg.lcr.enabled:
        lcr_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        lcr_worker = LcrWorker(cfg.lcr, lcr_q, aux_stop)
    if cfg.stage.enabled:
        stage_q = queue.Queue(maxsize=QUEUE_MAXSIZE)
        stage_worker = ZaberWorker(cfg.stage, stage_q, aux_stop)

    workers = [w for w in (h7_worker, lcr_worker, stage_worker) if w]
    for w in workers:
        w.start()

    rc = 0
    try:
        # ---- health: wait for the H7 reader to be ready ----
        # NB: we do NOT gate on sample count. The combined firmware emits no
        # sample lines while idle (sensor src=1/2 only when M4 is sampling;
        # SMA src=3/4/5 only during actuation) — data appears once we send
        # the drive/cycle below.
        log.info("Waiting up to %.0f s for the H7 port to open...", HEALTH_TIMEOUT_S)
        if not h7_worker.ready.wait(timeout=HEALTH_TIMEOUT_S):
            log.error("H7 not ready (err=%r)", h7_worker.error)
            print("ERROR: H7 did not come up — check COM port / firmware / power "
                  "(power-cycle the rig after flashing).", file=sys.stderr)
            return 2
        pre = _discard(h7_q)
        _discard(lcr_q); _discard(stage_q)
        log.info("H7 port ready (idle pre-samples=%d). Starting actuation.", pre)
        # Note for the operator if M4 isn't streaming sensors.
        if pre == 0:
            log.info("No idle sensor samples — M4 sampler not streaming "
                     "(fine for a resistor/SMA-only test; flash portenta_m4 "
                     "for laser/load).")

        # ---- send trims, arm watchdog, fire the actuation ----
        for cmd in (cfg.h7.startup_commands or []):
            _h7_send(h7_worker, cmd, log)
            time.sleep(0.05)
        _h7_send(h7_worker, f"wdt {int(plan['wdt_ms'])}", log)
        time.sleep(0.05)
        if not _h7_send(h7_worker, plan["command"], log):
            log.error("Failed to send actuation command")
            return 2

        # ---- log loop ----
        planned_s = plan["planned_s"]
        print(f"\nRunning: {plan['command']}  "
              f"({'%.1f s' % planned_s if planned_s else 'until Ctrl+C'})")
        t0 = time.monotonic()
        last_ping = t0
        h7_total = lcr_total = stage_total = 0
        aux_warned: set = set()
        with contextlib.ExitStack() as stack:
            h7_f = stack.enter_context(open(paths.h7_csv("raw"), "w", newline=""))
            h7_w = csv.writer(h7_f)
            h7_w.writerow(["host_timestamp_s", "monotonic_s",
                           "firmware_timestamp_us", "src", "channel",
                           "value", "raw_code", "hw_us", "seq"])
            lcr_w = stage_w = None
            if lcr_q is not None:
                lcr_f = stack.enter_context(open(paths.lcr_csv("raw"), "w", newline=""))
                lcr_w = csv.writer(lcr_f)
                lcr_w.writerow(["host_timestamp_s", "monotonic_s",
                                "primary", "secondary", "status"])
            if stage_q is not None:
                stage_f = stack.enter_context(open(paths.stage_csv("raw"), "w", newline=""))
                stage_w = csv.writer(stage_f)
                stage_w.writerow(["host_timestamp_s", "monotonic_s", "position_mm"])

            while True:
                h7_total += _drain_h7(h7_q, h7_w)
                lcr_total += _drain_lcr(lcr_q, lcr_w)
                stage_total += _drain_stage(stage_q, stage_w)

                now = time.monotonic()
                elapsed = now - t0

                # Only an H7 failure is fatal — it carries the SMA data +
                # command channel. LCR/stage failures are logged once and
                # the run continues (their CSVs just stay empty).
                if h7_worker.error is not None:
                    log.error("H7 worker crashed: %r", h7_worker.error)
                    rc = 2
                    break
                for w in (lcr_worker, stage_worker):
                    if w is not None and w.error is not None and w.name not in aux_warned:
                        log.warning("%s failed (continuing without it): %r",
                                    w.name, w.error)
                        aux_warned.add(w.name)
                if planned_s is not None and elapsed >= planned_s:
                    break
                if stop_event.is_set():
                    # Continuous (n=0) → Ctrl+C is the normal stop (0).
                    # Timed run interrupted before its planned end → aborted (1).
                    rc = 0 if planned_s is None else 1
                    break

                if now - last_ping >= PING_PERIOD_S:
                    _h7_send(h7_worker, "ping", log)
                    last_ping = now
                if int(elapsed) != int(elapsed - DRAIN_TICK_S):
                    print(f"\r  t={elapsed:6.1f}s  H7={h7_total}  "
                          f"LCR={lcr_total}  STG={stage_total}", end="", flush=True)
                time.sleep(DRAIN_TICK_S)

            # stop the actuation + final drain
            _h7_send(h7_worker, "stop", log)
            time.sleep(0.1)
            h7_total += _drain_h7(h7_q, h7_w)
            lcr_total += _drain_lcr(lcr_q, lcr_w)
            stage_total += _drain_stage(stage_q, stage_w)
        print()  # newline after the progress line

        # ---- meta.json ----
        meta = {
            "session_id": paths.session_id,
            "schema": "sma_v3_experiment",
            "kind": "run_experiment",
            "actuation": {"mode": plan["mode"], "command": plan["command"],
                          "wdt_ms": plan["wdt_ms"], "params": plan["params"],
                          "planned_s": plan["planned_s"]},
            "counts": {"h7": h7_total, "lcr": lcr_total, "stage": stage_total},
            "lcr": {**_asdict(cfg.lcr), "active": lcr_q is not None,
                    "idn": getattr(lcr_worker, "idn", None)},
            "h7": {**_asdict(cfg.h7), "active": True,
                   "n_dropped": getattr(h7_worker, "n_dropped", None)},
            "stage": {**_asdict(cfg.stage), "active": stage_q is not None,
                      "info": getattr(stage_worker, "info", None)},
            "calibration": _asdict(cfg.calibration),
            "run": _asdict(cfg.run),
            "ended_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": {"platform": platform.platform(),
                     "python": sys.version.split()[0]},
        }
        with open(paths.meta_json, "w") as f:
            json.dump(meta, f, indent=2)
        log.info("Wrote %s (H7=%d LCR=%d STG=%d)",
                 paths.meta_json, h7_total, lcr_total, stage_total)
        print(f"  logged: H7={h7_total} LCR={lcr_total} STG={stage_total}")
        print(f"  -> {paths.session_dir}")

    except KeyboardInterrupt:
        rc = 1
    finally:
        # Safety: ensure the SMA is parked even on an error path. The M7
        # watchdog also safe-stops once these heartbeats cease.
        _h7_send(h7_worker, "stop", log)
        stop_event.set()
        aux_stop.set()          # stop the auxiliary (LCR/stage) workers too
        for w in workers:
            w.join(timeout=5.0)

    # ---- analyze ----
    if rc == 0 and not args.no_analyze:
        try:
            import analyze_sma
            ns = SimpleNamespace(frequency=None, k=None, v0=None,
                                 load_scale=None, load_offset=None,
                                 no_deembed=False, out=None, phase="raw")
            analyze_sma.analyze_session(paths.session_dir, "raw", ns)
        except Exception as e:  # noqa: BLE001
            log.warning("analyze step failed: %s", e)
            print(f"  (analyze skipped: {e})")

    return rc


def _asdict(obj) -> dict:
    from dataclasses import asdict
    return asdict(obj)


if __name__ == "__main__":
    sys.exit(main())
