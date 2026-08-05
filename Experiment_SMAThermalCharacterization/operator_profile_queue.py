#!/usr/bin/env python3
"""operator_profile_queue.py — run a LIST of sweep profiles back-to-back, unattended.

WHY THIS EXISTS
    `operator_current_sweep.py` runs exactly ONE profile per launch and exits.
    An overnight campaign is a dozen profiles in a pre-registered order, and
    nobody is awake at 3 am to start the next one. This queues them, keeps a
    log per profile, runs the standard report after each, and — the part that
    matters at 3 am — TREATS A FAILED PROFILE AS ONE LOST PROFILE, not a lost
    night: the queue moves on to the next one.

USAGE
    # ALWAYS dry-run first. This chains each profile's own --dry-run, so it
    # never opens the port, and it prints the projected finish time.
    python operator_profile_queue.py profiles/night_profiles_20260805 --dry-run

    # the real run (explicit order, or a folder = sorted glob of *.json)
    python operator_profile_queue.py profiles/night_profiles_20260805 \
        --deadline 07:00 --between-s 30

    # explicit subset / resume after a crash: name the files, in order
    python operator_profile_queue.py \
        profiles/night_profiles_20260805/n4_shortcool_test.json \
        profiles/night_profiles_20260805/n5_longcool.json

WHAT IT GUARANTEES
    - Every profile is VALIDATED BEFORE THE FIRST PULSE FIRES: parsed, port
      checked against the ports actually present, conditions non-empty, no
      condition above the profile's own max_ma. A typo in the last profile is
      found at 20:00, not at 04:00.
    - One child process per profile, so `operator_current_sweep.py`'s own
      `finally: disarm()` runs on every exit path. Ctrl-C is FORWARDED to the
      child and the queue then stops — the child disarms itself.
    - `--deadline HH:MM` stops STARTING profiles after that wall-clock time
      (a profile already running is allowed to finish). Use it so the rig is
      idle before anyone walks in.
    - queue_manifest.json in the queue log folder records, per profile: the
      sweep folder it produced, exit code, start/end time, and the report
      exit code. That is the provenance for which captures came from which
      pre-registered role.

WHAT IT DOES NOT DO
    It does not power-cycle the rig, re-zero the load cell, or check the laser
    baseline. Those are the operator's pre-flight (see the campaign README);
    a queue that runs all night on a railed load cell just produces 220
    captures of nothing.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWEEP = HERE / "operator_current_sweep.py"
REPORT = HERE / "operator_sweep_report.py"
RAW = HERE / "data" / "raw"

# Matches operator_current_sweep.py's own per-condition overhead model, so the
# projection here and the ETA the child prints agree.
LEAD_IN_S = 2.0
PER_COND_OVERHEAD_S = LEAD_IN_S + 4.0


def est_profile_s(p: dict) -> float:
    """Wall-clock estimate for one profile, using the runner's actual model."""
    settle = float(p.get("settle_s", 20.0))
    cool_d = float(p.get("cool_s", 12.0))
    cyc_d = int(p.get("cycles", 3))
    if p.get("conditions"):
        conds = [(float(c["i_ma"]), int(c["heat_ms"]),
                  float(c.get("cool_s", cool_d)), int(c.get("cycles", cyc_d)))
                 for c in p["conditions"]]
    else:
        levels = [float(x) for x in p.get("levels_ma", [])]
        heats = [int(x) for x in p.get("heat_ms", [100])]
        conds = [(l, h, cool_d, cyc_d) for h in heats for l in levels]
    return sum(settle + PER_COND_OVERHEAD_S + (n + 1) * (h / 1e3 + cool)
               for _, h, cool, n in conds)


def available_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except Exception:                                            # noqa: BLE001
        return []
    return [p.device for p in list_ports.comports()]


def validate(paths: list[Path]) -> tuple[list[tuple[Path, dict]], list[str],
                                         list[str]]:
    """Parse + sanity-check every profile up front.

    Returns (loaded, problems, port_problems). The port check is split out
    because it is the one check that legitimately fails on a machine that is
    not the rig: `--dry-run` from a laptop should still validate a whole
    campaign, while a REAL run must not start against a port that is absent.
    """
    loaded, problems = [], []
    ports = available_ports()
    want_ports: dict[str, list[str]] = {}
    for path in paths:
        if not path.is_file():
            problems.append(f"{path}: not a file")
            continue
        try:
            p = json.loads(path.read_text())
        except Exception as e:                                   # noqa: BLE001
            problems.append(f"{path.name}: unparseable JSON — {e}")
            continue
        if not p.get("conditions") and not p.get("levels_ma"):
            problems.append(f"{path.name}: neither 'conditions' nor 'levels_ma'")
            continue
        max_ma = float(p.get("max_ma", 800.0))
        if p.get("conditions"):
            hot = [c for c in p["conditions"] if float(c["i_ma"]) > max_ma]
            if hot:
                problems.append(f"{path.name}: {len(hot)} condition(s) above "
                                f"max_ma {max_ma:.0f} — the sweep would refuse "
                                f"to start")
        port = p.get("port")
        if port and ports and port not in ports:
            want_ports.setdefault(port, []).append(path.name)
        loaded.append((path, p))
    # One message per absent port, not one per profile.
    port_problems = [
        f"port {port} is absent (present: {', '.join(ports)}) — wanted by "
        f"{len(names)} profile(s): {names[0]}"
        + (f" .. {names[-1]}" if len(names) > 1 else "")
        for port, names in want_ports.items()]
    return loaded, problems, port_problems


def n_conditions(p: dict) -> int:
    if p.get("conditions"):
        return len(p["conditions"])
    return len(p.get("levels_ma") or []) * len(p.get("heat_ms") or [1])


def sweep_dir_for(p: dict, before: set[Path]) -> Path | None:
    """The folder the child just wrote into.

    A profile may name its own `out`; otherwise the sweep mints
    data/raw/sweep_<stamp>/ and we find it by SET DIFFERENCE (not mtime — the
    report step rewrites files inside older folders)."""
    if p.get("out"):
        d = Path(p["out"])
        return d if d.is_dir() else None
    fresh = {d for d in RAW.glob("sweep_*") if d.is_dir()} - before
    return max(fresh, key=lambda d: d.name) if fresh else None


def count_captures(d: Path) -> int:
    """Per-condition capture CSVs, excluding the sweep's own two summary files."""
    return len([c for c in d.glob("*.csv")
                if c.name not in ("summary.csv", "cycles.csv",
                                  "summary_report.csv")])


def run_child(cmd: list[str], log: Path, tail_n: int = 60) -> tuple[int, list[str]]:
    """Run a child, tee-ing its output to console and `log`. Returns
    (exit_code, last `tail_n` lines) — the tail is how the queue tells a sweep
    that COLLECTED DATA from one that exited 0 having collected nothing.

    Ctrl-C reaches the child too (same process group), so its finally-disarm
    runs; we wait for it rather than killing the rig mid-pulse.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    tail: list[str] = []
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"$ {' '.join(cmd)}\n\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1, errors="replace")
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                fh.write(line)
                tail.append(line.rstrip())
                if len(tail) > tail_n:
                    tail.pop(0)
            return proc.wait(), tail
        except KeyboardInterrupt:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.terminate()
            raise


def classify(rc: int, tail: list[str], got: int, want: int) -> tuple[str, str]:
    """(status, why) for one profile, judged on WHAT LANDED ON DISK.

    `operator_current_sweep.py` exits 0 on almost every failure path — a dead
    port, a hub fault, an aborted sense check all return 0 after writing
    summary.csv. Trusting the exit code reports a green night that collected
    nothing, so the verdict comes from the capture count and the reason the
    sweep printed."""
    why = ""
    for line in reversed(tail):
        s = line.strip()
        for marker in ("NO LEVEL PASSED —", "stopped because:", "ABORT —",
                       "STOP —", "ERROR:"):
            if s.startswith(marker):
                why = s
                break
        if why:
            break
    if rc != 0:
        return f"exit {rc}", why or f"child exited {rc}"
    if got == 0:
        return "no captures", why or "no capture CSV was written"
    if got < want:
        return f"partial {got}/{want}", why or "sweep ended early"
    return "ok", why


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+",
                    help="profile .json files IN RUN ORDER, or a folder "
                         "(expanded to sorted *.json — the n0..n9 naming "
                         "convention makes sorted order the run order)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate every profile, chain each one's own "
                         "--dry-run, print the projected finish. Opens no port.")
    ap.add_argument("--deadline", default=None, metavar="HH:MM",
                    help="stop STARTING profiles after this wall-clock time "
                         "(next occurrence). A running profile finishes.")
    ap.add_argument("--between-s", type=float, default=30.0,
                    help="idle gap between profiles (default 30) — the coil "
                         "sits at ambient while the report runs")
    ap.add_argument("--no-report", action="store_true",
                    help="skip operator_sweep_report.py after each profile")
    ap.add_argument("--stop-on-error", action="store_true",
                    help="abandon the queue if a profile exits non-zero "
                         "(default: log it and run the next profile)")
    ap.add_argument("--logdir", default=None,
                    help="where per-profile logs + queue_manifest.json go "
                         "(default data/raw/queue_<stamp>/)")
    a = ap.parse_args()

    paths: list[Path] = []
    for t in a.targets:
        p = Path(t)
        if not p.is_absolute():
            p = (HERE / p) if (HERE / p).exists() else p.resolve()
        if p.is_dir():
            paths.extend(sorted(p.glob("*.json")))
        else:
            paths.append(p)
    if not paths:
        print("ERROR: no profiles resolved from the targets", file=sys.stderr)
        return 2

    loaded, problems, port_problems = validate(paths)
    print(f"\n{'='*66}\nprofile queue — {len(loaded)} profile(s)\n{'='*66}")
    t_run = 0.0
    for path, p in loaded:
        n = n_conditions(p)
        est = est_profile_s(p)
        t_run += est + a.between_s
        # A randomized profile carries cool PER CONDITION and no scalar, so
        # report the actual range — printing the absent scalar as "0 s" reads
        # like a zero-cool run, which is the one thing that must never happen.
        cools = [float(c["cool_s"]) for c in (p.get("conditions") or [])
                 if "cool_s" in c]
        if cools and len({round(c, 1) for c in cools}) > 1:
            cool_s = f"cool {min(cools):4.0f}-{max(cools):.0f}s"
        else:
            cool_s = f"cool {float(p.get('cool_s', cools[0] if cools else 0)):4.0f}s"
        print(f"  {path.name:26s} {n:4d} cond  cycles={p.get('cycles')}  "
              f"{cool_s:16s} ~{est/60:5.0f} min")
    print(f"\n  queue total ~{t_run/3600:.2f} h "
          f"(incl. {a.between_s:.0f} s between profiles)")
    print(f"  projected finish if started now: "
          f"{datetime.now() + timedelta(seconds=t_run):%a %H:%M}")

    deadline = None
    if a.deadline:
        hh, mm = (int(x) for x in a.deadline.split(":"))
        deadline = datetime.now().replace(hour=hh, minute=mm, second=0,
                                          microsecond=0)
        if deadline <= datetime.now():
            deadline += timedelta(days=1)
        print(f"  deadline {deadline:%a %H:%M} — no profile STARTS after it")
        if datetime.now() + timedelta(seconds=t_run) > deadline:
            over = (datetime.now() + timedelta(seconds=t_run) - deadline)
            print(f"  !! the queue overruns the deadline by "
                  f"{over.total_seconds()/60:.0f} min — later profiles will be "
                  f"SKIPPED. Trim the queue or move the deadline.")

    # A missing port is fatal for a real run and merely a note for --dry-run,
    # so a campaign can be validated away from the rig.
    fatal = list(problems) + ([] if a.dry_run else port_problems)
    if port_problems and a.dry_run:
        print("\n  note (dry-run, not fatal):")
        for m in port_problems:
            print(f"    - {m}")
    if fatal:
        print(f"\n  {len(fatal)} PROBLEM(S) — nothing will run:")
        for m in fatal:
            print(f"    - {m}")
        print()
        return 2
    print("  validation: all profiles parse, no condition above max_ma"
          + ("" if a.dry_run else ", every port present"))

    if a.dry_run:
        for path, _ in loaded:
            print(f"\n{'-'*66}\n--- dry-run {path.name}\n{'-'*66}")
            subprocess.run([sys.executable, str(SWEEP), "--profile", str(path),
                            "--dry-run"], cwd=str(HERE))
        print("\nDRY RUN COMPLETE — no port was opened, no pulse fired.\n")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logdir = Path(a.logdir) if a.logdir else RAW / f"queue_{stamp}"
    logdir.mkdir(parents=True, exist_ok=True)
    manifest = {"started": datetime.now().isoformat(), "logdir": str(logdir),
                "deadline": deadline.isoformat() if deadline else None,
                "profiles": []}

    def flush_manifest() -> None:
        (logdir / "queue_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

    flush_manifest()
    print(f"\n  logs + manifest -> {logdir}\n")

    t0 = time.time()
    dead_streak = 0
    try:
        for k, (path, _p) in enumerate(loaded):
            entry = {"profile": path.name, "path": str(path), "order": k}
            if deadline and datetime.now() >= deadline:
                entry["status"] = "skipped: past deadline"
                manifest["profiles"].append(entry)
                flush_manifest()
                print(f"=== SKIP [{k+1}/{len(loaded)}] {path.name} — "
                      f"past the {deadline:%H:%M} deadline")
                continue

            before = {d for d in RAW.glob("sweep_*") if d.is_dir()}
            print(f"\n{'='*66}")
            print(f"=== [{k+1}/{len(loaded)}] {path.name}   "
                  f"queue elapsed {timedelta(seconds=int(time.time()-t0))}")
            print(f"{'='*66}", flush=True)
            entry["started"] = datetime.now().isoformat()
            rc, tail = run_child(
                [sys.executable, str(SWEEP), "--profile", str(path)],
                logdir / f"{path.stem}.log")
            entry["ended"] = datetime.now().isoformat()
            entry["exit_code"] = rc
            sweep_dir = sweep_dir_for(_p, before)
            want = n_conditions(_p)
            got = count_captures(sweep_dir) if sweep_dir else 0
            status, why = classify(rc, tail, got, want)
            entry.update(sweep_dir=sweep_dir.name if sweep_dir else None,
                         captures=got, conditions=want, status=status)
            if why:
                entry["why"] = why
            if status != "ok":
                print(f"\n  !! {path.name}: {status}"
                      + (f" — {why}" if why else ""))
                print(f"     log: {logdir / (path.stem + '.log')}")

            # A rig that has gone down (USB dropped, ADC2 dead, EVM rails off)
            # cannot be fixed by trying the next profile. Two zero-capture
            # profiles in a row means stop burning the night on it.
            dead_streak = dead_streak + 1 if got == 0 else 0
            if dead_streak >= 2:
                entry["status"] = status + " (rig appears down)"
                manifest["aborted"] = ("two consecutive profiles produced no "
                                       "captures — rig down")
                manifest["profiles"].append(entry)
                flush_manifest()
                print("\n  ABANDONING THE QUEUE — two consecutive profiles "
                      "captured nothing.\n  The rig needs a power-cycle "
                      "(USB + EVM); remaining profiles are NOT run.")
                break

            if sweep_dir and got and not a.no_report:
                print(f"\n  --- report {sweep_dir.name} "
                      f"({got}/{want} captures) ---", flush=True)
                rrc, _ = run_child([sys.executable, str(REPORT), str(sweep_dir)],
                                   logdir / f"{path.stem}.report.log")
                entry["report_exit_code"] = rrc
                if rrc != 0:
                    print(f"  !! report exited {rrc} (captures are safe; "
                          f"re-run the report by hand)")

            manifest["profiles"].append(entry)
            flush_manifest()

            if status != "ok" and a.stop_on_error:
                print("  --stop-on-error: abandoning the queue")
                break
            if k + 1 < len(loaded) and a.between_s > 0:
                print(f"\n  idle {a.between_s:.0f}s before the next profile ...",
                      flush=True)
                time.sleep(a.between_s)
    except KeyboardInterrupt:
        manifest["interrupted"] = datetime.now().isoformat()
        print("\n  queue interrupted — the running sweep disarmed itself",
              file=sys.stderr)

    manifest["ended"] = datetime.now().isoformat()
    manifest["wall_s"] = round(time.time() - t0, 1)
    flush_manifest()

    print(f"\n{'='*66}\nQUEUE DONE — {timedelta(seconds=int(time.time()-t0))} "
          f"wall clock\n{'='*66}")
    for e in manifest["profiles"]:
        caps = (f"{e['captures']}/{e['conditions']}"
                if "captures" in e else "-")
        print(f"  {e['profile']:26s} {e.get('status','?'):22s} "
              f"{caps:>8s}  {e.get('sweep_dir') or '(none)'}")
        if e.get("why"):
            print(f"      {e['why'][:96]}")
    print(f"\n  manifest -> {logdir / 'queue_manifest.json'}")
    bad = [e for e in manifest["profiles"] if e.get("status") != "ok"]
    total_caps = sum(e.get("captures", 0) for e in manifest["profiles"])
    print(f"  {len(manifest['profiles']) - len(bad)} ok, {len(bad)} not ok, "
          f"{total_caps} captures total")
    print("\n  NEXT: cd analysis && python analyze_raw.py "
          + " ".join(e["sweep_dir"] for e in manifest["profiles"]
                     if e.get("sweep_dir") and e.get("captures")) + "\n")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
