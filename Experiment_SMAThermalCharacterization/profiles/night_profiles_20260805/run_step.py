#!/usr/bin/env python3
"""run_step.py — the 2026-08-05 campaign, ONE PROFILE PER INVOCATION.

WHY THIS EXISTS
    The campaign was built as an unattended 9.2 h queue (run_night.ps1 ->
    operator_profile_queue.py). The 2026-08-05 attempt collected nothing: the
    SMA sense chain had degraded during pre-flight, and that failure is
    invisible to the queue's own circuit breaker — `abort_on_bad_sense` stops
    each profile at its first bad condition but still WRITES a capture or two,
    so "two consecutive profiles captured nothing" never trips. The projected
    yield of running it anyway was ~20 captures of 804, over a full night,
    reported as a mix of `ok` and `partial`.

    Running the same 13 profiles one at a time puts a person between the
    blocks. Nothing runs for hours on a rig that is already faulted, and the
    campaign can span several sessions instead of needing one clear 12 h night.

WHAT IS THE SAME AS THE NIGHT, AND WHAT IS NOT
    Same   the profiles, byte for byte, and the frozen run order
           (run_order.txt, shared with run_night.ps1). A profile is
           self-contained — settle, conditions, cool times — so the data
           inside a capture is what the queue would have written.
    New    every step is gated on the sense chain: the LAST recorded sense
           verdict in the ledger blocks a new step (a faulted rig is not
           driven again by habit), each completed step's own first capture is
           graded into the ledger (sense_after) at zero extra cost, and
           --probe adds an explicit 650 mA x 300 ms probe capture before the
           profile. The probe is OPT-IN because it is a second port open per
           step, and rapid reopens wedge the H7's USB-CDC TX (2026-08-07;
           power-cycle to revive). The profiles' own abort_on_bad_sense guard
           is live at every condition either way.
    Changed  the anchors (n0/n3/n9) now bracket the whole SEQUENCE rather than
           one night, so session boundaries — power cycles, re-clipping,
           ambient swings — land between them. They still measure drift; it is
           drift over days. The ledger timestamps say where the boundaries
           are, which is what makes that interpretable, so run the anchors in
           their pre-registered positions and do not re-run them casually.

USAGE  (from the module root)
    python profiles/night_profiles_20260805/run_step.py            # status board
    python profiles/night_profiles_20260805/run_step.py next       # run the next step
    python profiles/night_profiles_20260805/run_step.py 7          # run step 7
    python profiles/night_profiles_20260805/run_step.py n3         # ... or name it
    python profiles/night_profiles_20260805/run_step.py next --dry-run

    A BARE INVOCATION NEVER DRIVES THE RIG. It prints the board and exits;
    `next` is the word that fires pulses.

WHERE THINGS LAND
    data/raw/campaigns/<key>/sweep_<stamp>/   the captures
    data/raw/campaigns/<key>/pulse_<stamp>/   the per-step sense probes
    data/raw/campaigns/<key>/steps/           per-step logs + ledger.json

    ledger.json is the campaign's provenance and this script's memory of what
    has run: one appended record per attempt, with the sweep folder, the
    capture count against the conditions commanded, both sense verdicts and
    the wall-clock times. Delete it and the campaign forgets where it was.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parents[1]
sys.path.insert(0, str(MODULE))

import operator_profile_queue as queue          # noqa: E402  (needs the path)
import operator_sense_check as sense            # noqa: E402

RAW = MODULE / "data" / "raw"
SWEEP = MODULE / "operator_current_sweep.py"
REPORT = MODULE / "operator_sweep_report.py"
PULSE = MODULE / "operator_pulse_capture.py"
ORDER_FILE = HERE / "run_order.txt"

# The coil the campaign was designed against (Dynalloy, 10 mm cold length).
# Refit the wire and it is a different campaign: pass --campaign explicitly.
WIRE = "dynalloy_10mm"

# The sense probe. FIXED at every step — the whole point is comparability, and
# 650 mA / 300 ms is the condition the 08-05 diagnosis was read on. Two cycles
# at a 30 s cool give ~60 s of idle samples, which is what the check measures.
PROBE = ["--ma", "650", "--heat-ms", "300", "--i-low", "0",
         "--cool-s", "30", "--cycles", "2", "--settle-s", "10"]

# Rapid close->reopen cycles can wedge the H7's USB-CDC TX outright: the port
# opens fine, delivers 0 bytes, and even ungated command replies go silent —
# only a power cycle revives it (measured 2026-08-07; the 08-05 log shows the
# same signature pre-dating this runner). So (a) the probe is opt-in, keeping
# the default at ONE open per step, and (b) when a child does report a silent
# port, retry ONCE after a long wait — tonight's 6 s retries failed 6/6, while
# gaps >= 90 s succeeded. Short retries only roll the dice faster.
PORT_SETTLE_S = 6.0      # gap between probe close and profile open (--probe)
PORT_RETRY_WAIT_S = 90.0
PORT_TRIES = 2
_SILENT = "is not streaming"


def load_order() -> list[tuple[str, str]]:
    """[(filename, role)] from run_order.txt, and every n*.json on disk must
    appear exactly once — a profile added by a later gen_night_profiles.py run
    would otherwise be silently dropped from the campaign."""
    order = []
    for line in ORDER_FILE.read_text(encoding="utf-8").splitlines():
        name, _, role = line.partition("#")
        if name.strip():
            order.append((name.strip(), role.strip()))
    listed = {n for n, _ in order}
    on_disk = {p.name for p in HERE.glob("n*.json")}
    if on_disk - listed:
        sys.exit(f"ERROR: on disk but not in run_order.txt: "
                 f"{', '.join(sorted(on_disk - listed))}")
    if listed - on_disk:
        sys.exit(f"ERROR: in run_order.txt but not on disk: "
                 f"{', '.join(sorted(listed - on_disk))}")
    return order


def find_campaign(explicit: str | None) -> str:
    """The campaign folder to file into. Minted on the first step and then
    DISCOVERED, so the key is not a constant that goes stale in the source the
    moment the campaign starts a day later than planned."""
    if explicit:
        return explicit
    found = sorted(d.name for d in (RAW / "campaigns").glob(f"*_{WIRE}_night")
                   if d.is_dir())
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        sys.exit(f"ERROR: {len(found)} candidate campaigns ({', '.join(found)})"
                 f" — pass --campaign to say which")
    return f"{date.today():%Y%m%d}_{WIRE}_night"


class Ledger:
    """Append-only record of every attempt. Progress is DERIVED from it rather
    than stored as a cursor: a re-run appends, and the latest record for a
    profile is what counts, so nothing has to be hand-edited after a failure."""

    def __init__(self, path: Path):
        self.path = path
        self.data = (json.loads(path.read_text(encoding="utf-8"))
                     if path.exists() else {"campaign": path.parent.parent.name,
                                            "attempts": []})

    def latest(self, profile: str) -> dict | None:
        got = [a for a in self.data["attempts"] if a["profile"] == profile]
        return got[-1] if got else None

    def append(self, record: dict) -> None:
        self.data["attempts"].append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def probes(self) -> list[dict]:
        return [a["sense_before"] for a in self.data["attempts"]
                if a.get("sense_before")]


def board(order, ledger, loaded) -> None:
    print(f"\n{'='*74}")
    print(f"campaign {ledger.data['campaign']}   "
          f"ledger {ledger.path.relative_to(MODULE)}")
    print(f"{'='*74}")
    print(f"  {'#':>2}  {'profile':24s} {'cond':>5} {'~min':>5}  "
          f"{'status':16s} captures")
    done_min = todo_min = 0.0
    for k, (name, role) in enumerate(order, 1):
        prof = loaded[name]
        est = queue.est_profile_s(prof) / 60
        rec = ledger.latest(name)
        st = rec["status"] if rec else "-- not run"
        caps = (f"{rec['captures']}/{rec['conditions']}" if rec else "")
        if rec and rec["status"] == "ok":
            done_min += est
        else:
            todo_min += est
        mark = "ok " if rec and rec["status"] == "ok" else "   "
        print(f"  {k:2d}. {name:24s} {queue.n_conditions(prof):5d} {est:5.0f}  "
              f"{mark}{st:13s} {caps:>9s}")
        if rec and rec.get("why"):
            print(f"      {rec['why'][:90]}")
    n_ok = sum(1 for n, _ in order if (ledger.latest(n) or {}).get("status") == "ok")
    print(f"\n  {n_ok}/{len(order)} profiles complete · ~{done_min/60:.1f} h "
          f"collected · ~{todo_min/60:.1f} h remaining")

    probes = ledger.probes()
    if probes:
        print(f"\n  sense probe trend (idle window, 650 mA/300 ms, every step):")
        for p in probes[-8:]:
            print(f"    {p['when'][:16].replace('T', ' ')}  "
                  f"sigma {p['sigma_ma']:5.1f} mA   R {p['r_ohm']:5.2f} ohm   "
                  f"R@200ms {p['r_pct']:4.2f} %   {p['verdict']}")
        # sigma is the trend that matters. R is the WIRE and barely moves; the
        # 2026-08-05 "R climbed with every reseat" was an estimator artifact
        # (STATUS 2026-08-07), so do not invite that reading again.
        ok = [p for p in probes if p["sigma_ma"] is not None]
        if len(ok) > 1:
            print(f"    sigma {ok[0]['sigma_ma']:.1f} -> {ok[-1]['sigma_ma']:.1f} mA "
                  f"across {len(ok)} probes. 25 mA collected valid data on "
                  f"08-05; 65 mA buries\n    the payload; ~7 mA is the ADC's "
                  f"floor with nothing attached, not a passing rig.")


def find_new(parent: Path, prefix: str, before: set[Path]) -> Path | None:
    """The folder a child just wrote, by SET DIFFERENCE — not mtime, because
    the report step rewrites files inside older folders."""
    fresh = {d for d in parent.glob(f"{prefix}_*") if d.is_dir()} - before
    return max(fresh, key=lambda d: d.name) if fresh else None


def run_silent_retry(cmd: list[str], log: Path, what: str) -> tuple[int, list[str]]:
    """run_child, but a port that opens silent is retried instead of reported.
    Anything else — a real rig fault, a bad profile — returns on the first try."""
    for k in range(PORT_TRIES):
        rc, tail = queue.run_child(cmd, log)
        if not any(_SILENT in ln for ln in tail):
            return rc, tail
        if k + 1 < PORT_TRIES:
            print(f"\n  {what}: COM8 opened silent — waiting "
                  f"{PORT_RETRY_WAIT_S:.0f}s and retrying "
                  f"({k + 2}/{PORT_TRIES}). Short retries never recover this "
                  f"state; if the retry fails too, power-cycle USB + EVM.",
                  flush=True)
            time.sleep(PORT_RETRY_WAIT_S)
    return rc, tail


def run_probe(campaign_dir: Path, logdir: Path, tag: str) -> dict | None:
    """Fire the fixed sense probe and grade it. Returns None if it produced
    nothing gradeable — which is itself a stop condition, not a pass."""
    before = {d for d in campaign_dir.glob("pulse_*") if d.is_dir()}
    print(f"\n  --- sense probe ({' '.join(PROBE[:4])}) ---", flush=True)
    rc, _ = run_silent_retry([sys.executable, str(PULSE), *PROBE,
                              "--outdir", str(campaign_dir)],
                             logdir / f"{tag}.probe.log", "probe")
    d = find_new(campaign_dir, "pulse", before)
    if rc != 0 or d is None or not (d / "h7.csv").exists():
        print(f"  !! the probe produced no capture (exit {rc}). Read the child's"
              f" error above — a port that\n     opens but delivers nothing is "
              f"usually a serial monitor still holding COM8, not a rig fault.")
        return None
    try:
        sigma, r, rpct, corr = sense.measure(d / "h7.csv")
    except SystemExit as e:     # too few samples: the stream, not the wire
        print(f"  !! the probe capture is not gradeable: {e}\n"
              f"     That is a streaming fault, not a verdict — power-cycle "
              f"USB + EVM and try again.")
        return None
    v, driver = sense.verdict(sigma, rpct)
    print(f"\n    sigma {sigma:6.1f} mA (healthy 24.9-25.1)   "
          f"R {r:5.2f} ohm   R@200ms {rpct:4.2f} % (healthy 1.80-2.05)")
    print(sense.explain(v, driver, sigma, rpct))
    return {"when": datetime.now().isoformat(timespec="seconds"),
            "capture": d.name, "sigma_ma": round(sigma, 1), "r_ohm": round(r, 3),
            "r_pct": round(rpct, 2), "corr": round(corr, 3), "verdict": v}


def grade_sweep(sweep_dir: Path) -> dict | None:
    """Sense verdict from the sweep's OWN first capture. Free — no extra pulses
    — and it is the reading that says whether the block that just ran was
    collected on a healthy chain."""
    caps = sorted(sweep_dir.glob("c*_level_*mA_h*ms.csv"))
    if not caps:
        return None
    try:
        sigma, r, rpct, corr = sense.measure(caps[0])
    except SystemExit as e:                                      # too short
        print(f"  (post-run sense read skipped: {e})")
        return None
    v, _ = sense.verdict(sigma, rpct)
    return {"when": datetime.now().isoformat(timespec="seconds"),
            "capture": caps[0].name, "sigma_ma": round(sigma, 1),
            "r_ohm": round(r, 3), "r_pct": round(rpct, 2),
            "corr": round(corr, 3), "verdict": v}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default=None,
                    help="'next', a step number (1-13), or a profile name "
                         "prefix such as n3. Omit to print the board and run "
                         "nothing.")
    ap.add_argument("--campaign", default=None, metavar="KEY",
                    help="campaign folder under data/raw/campaigns/ "
                         "(default: the existing *_%s_night one, else minted "
                         "from today's date)" % WIRE)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and chain the profile's own --dry-run. "
                         "Opens no port, fires no probe.")
    ap.add_argument("--probe", action="store_true",
                    help="fire a separate 650 mA sense probe before the "
                         "profile. OFF BY DEFAULT since 2026-08-07: the probe "
                         "is a SECOND port open per step, and rapid reopens "
                         "wedge the Portenta's USB-CDC TX (total silence, "
                         "power-cycle to revive — see STATUS). The gate does "
                         "not need it: every night profile carries "
                         "abort_on_bad_sense, and the step's own first capture "
                         "is graded into the ledger as sense_after.")
    ap.add_argument("--no-probe", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-report", action="store_true",
                    help="skip operator_sweep_report.py after the step")
    ap.add_argument("--force", action="store_true",
                    help="run despite a MARGINAL/BAD probe, or re-run a step "
                         "already recorded ok")
    a = ap.parse_args()

    order = load_order()
    loaded = {}
    for name, _ in order:
        loaded[name] = json.loads((HERE / name).read_text(encoding="utf-8"))

    campaign = find_campaign(a.campaign)
    campaign_dir = RAW / "campaigns" / campaign
    logdir = campaign_dir / "steps"
    ledger = Ledger(logdir / "ledger.json")

    board(order, ledger, loaded)

    if a.target is None:
        nxt = next((k for k, (n, _) in enumerate(order, 1)
                    if (ledger.latest(n) or {}).get("status") != "ok"), None)
        print(f"\n  next: step {nxt} ({order[nxt-1][0]})" if nxt
              else "\n  every step is complete.")
        print(f"  run it with:  python {Path(__file__).relative_to(MODULE)} "
              f"next\n")
        return 0

    # -- resolve which step ---------------------------------------------------
    if a.target == "next":
        k = next((k for k, (n, _) in enumerate(order)
                  if (ledger.latest(n) or {}).get("status") != "ok"), None)
        if k is None:
            print("\n  every step is complete — nothing to run.\n")
            return 0
    elif a.target.isdigit():
        k = int(a.target) - 1
        if not 0 <= k < len(order):
            sys.exit(f"ERROR: step {a.target} is outside 1..{len(order)}")
    else:
        hits = [i for i, (n, _) in enumerate(order) if n.startswith(a.target)]
        if len(hits) != 1:
            sys.exit(f"ERROR: '{a.target}' matches {len(hits)} profiles")
        k = hits[0]

    name, role = order[k]
    path, prof = HERE / name, loaded[name]
    prev = ledger.latest(name)
    if prev and prev["status"] == "ok" and not a.force:
        sys.exit(f"\nERROR: step {k+1} ({name}) already ran ok on "
                 f"{prev['started'][:16]} -> {prev['sweep_dir']}.\n"
                 f"Re-running it duplicates a pre-registered role. "
                 f"Pass --force if that is what you want.\n")

    # -- validate BEFORE anything moves --------------------------------------
    _, problems, port_problems = queue.validate([path])
    fatal = problems + ([] if a.dry_run else port_problems)
    if fatal:
        print("\n  PROBLEM(S) — nothing will run:")
        for m in fatal:
            print(f"    - {m}")
        return 2

    est = queue.est_profile_s(prof)
    n_cond = queue.n_conditions(prof)
    print(f"\n{'='*74}")
    print(f"=== STEP {k+1}/{len(order)}  {name}")
    print(f"    {role}")
    print(f"    {n_cond} conditions · ~{est/60:.0f} min · "
          f"ends ~{datetime.now() + timedelta(seconds=est + 90):%H:%M}"
          f"{' (+ ~1.5 min probe)' if a.probe else ''}")
    print(f"    -> data/raw/campaigns/{campaign}/")
    print(f"{'='*74}", flush=True)

    if a.dry_run:
        import subprocess
        subprocess.run([sys.executable, str(SWEEP), "--profile", str(path),
                        "--dry-run"], cwd=str(MODULE))
        print("\nDRY RUN — no port opened, no pulse fired.\n")
        return 0

    campaign_dir.mkdir(parents=True, exist_ok=True)
    logdir.mkdir(parents=True, exist_ok=True)
    tag = f"{k+1:02d}_{path.stem}"
    record = {"step": k + 1, "profile": name, "role": role,
              "conditions": n_cond, "campaign": campaign,
              "started": datetime.now().isoformat(timespec="seconds")}

    # -- the gate -------------------------------------------------------------
    # DEFAULT PATH (no --probe): one port open per step, exactly the flow the
    # standalone sweep has always used. The gate is then (a) the profile's own
    # abort_on_bad_sense guard, live at every condition, and (b) the LAST
    # recorded sense verdict from the ledger — probe or sense_after — checked
    # here so a rig known to be faulted is not driven again by habit.
    if a.probe:
        probe = run_probe(campaign_dir, logdir, tag)
        record["sense_before"] = probe
        if probe is None or probe["verdict"] != "HEALTHY":
            if not a.force:
                record["status"] = "blocked: " + (probe["verdict"].lower()
                                                  if probe else "no probe")
                record["ended"] = datetime.now().isoformat(timespec="seconds")
                record["captures"] = 0
                ledger.append(record)
                print(f"  STEP NOT RUN. Fix the sense chain, then re-run this "
                      f"same command.\n  (--force runs it anyway; the 08-05 "
                      f"night is what that produces.)\n")
                return 1
            print("  --force: running on a non-HEALTHY sense chain.\n")
    else:
        last = None
        for att in ledger.data["attempts"]:
            for key in ("sense_after", "sense_before"):
                if att.get(key) and att[key].get("verdict"):
                    last = (att[key], key, att.get("profile"))
        if last and last[0]["verdict"] != "HEALTHY" and not a.force:
            s0 = last[0]
            record["status"] = "blocked: last sense " + s0["verdict"].lower()
            record["ended"] = datetime.now().isoformat(timespec="seconds")
            record["captures"] = 0
            ledger.append(record)
            print(f"  STEP NOT RUN — the most recent sense reading "
                  f"({s0['when'][:16]}, {s0.get('sigma_ma')} mA) was "
                  f"{s0['verdict']}.\n  Fix the chain (STATUS 2026-08-07), or "
                  f"--probe to re-measure, or --force to collect anyway.\n")
            return 1

    # -- the profile ----------------------------------------------------------
    # The probe (if any) just held the port. Give the CDC time before the
    # profile opens it — rapid reopens are what wedge the H7's USB TX.
    if a.probe:
        print(f"\n  port settle {PORT_SETTLE_S:.0f}s ...", flush=True)
        time.sleep(PORT_SETTLE_S)
    before = {d for d in campaign_dir.glob("sweep_*") if d.is_dir()}
    t0 = time.time()
    interrupted = False
    try:
        rc, tail = run_silent_retry(
            [sys.executable, str(SWEEP), "--profile", str(path),
             "--campaign", campaign], logdir / f"{tag}.log", "profile")
    except KeyboardInterrupt:
        rc, tail, interrupted = 130, [], True
        print("\n  interrupted — the sweep disarmed itself in its own finally; "
              "captures already written are safe", file=sys.stderr)

    # A sweep mints its folder BEFORE opening the port, so every silent-port
    # attempt leaves an empty one behind. Name them — data/raw is what the rig
    # wrote and this runner does not delete from it, but an empty sweep folder
    # is indistinguishable from a lost capture when you come back to it later.
    fresh = sorted({d for d in campaign_dir.glob("sweep_*") if d.is_dir()} - before)
    empty = [d for d in fresh if queue.count_captures(d) == 0]
    sweep_dir = find_new(campaign_dir, "sweep", before)
    got = queue.count_captures(sweep_dir) if sweep_dir else 0
    if empty and got:
        print(f"\n  note: {len(empty)} empty sweep folder(s) from retried port "
              f"opens — no pulse fired in them, safe to delete:")
        for d in empty:
            print(f"        {d.relative_to(MODULE)}")
    status, why = queue.classify(rc, tail, got, n_cond)
    if interrupted:
        status = f"interrupted {got}/{n_cond}"
    record.update(ended=datetime.now().isoformat(timespec="seconds"),
                  wall_s=round(time.time() - t0, 1), exit_code=rc,
                  sweep_dir=sweep_dir.name if sweep_dir else None,
                  captures=got, status=status)
    if why:
        record["why"] = why

    # -- report + the free post-run sense read --------------------------------
    if sweep_dir and got:
        record["sense_after"] = grade_sweep(sweep_dir)
        if not a.no_report:
            print(f"\n  --- report {sweep_dir.name} ({got}/{n_cond}) ---",
                  flush=True)
            rrc, _ = queue.run_child([sys.executable, str(REPORT),
                                      str(sweep_dir)],
                                     logdir / f"{tag}.report.log")
            record["report_exit_code"] = rrc
            if rrc != 0:
                print(f"  !! report exited {rrc} (the captures are safe; "
                      f"re-run it by hand)")
    ledger.append(record)

    print(f"\n{'='*74}")
    print(f"STEP {k+1} {name}: {status}   {got}/{n_cond} captures   "
          f"{timedelta(seconds=int(time.time()-t0))}")
    if why:
        print(f"  {why}")
    after = record.get("sense_after")
    if after:
        print(f"  sense after: sigma {after['sigma_ma']:.1f} mA, "
              f"R {after['r_ohm']:.2f} ohm, R@200ms {after['r_pct']:.2f} % "
              f"-> {after['verdict']}")
    print(f"{'='*74}")
    if status != "ok":
        print(f"  log: {(logdir / (tag + '.log')).relative_to(MODULE)}")
        print(f"  The step is recorded as NOT ok, so `next` will offer it "
              f"again once the cause is fixed.")
    rest = [n for n, _ in order if (ledger.latest(n) or {}).get("status") != "ok"]
    print(f"\n  {len(order)-len(rest)}/{len(order)} complete."
          + (f" next: {rest[0]}\n" if rest else
             "\n  Campaign done — register the sweep folders in CAMPAIGNS "
             "(analysis/analyze_raw.py), then run the standing pipeline.\n"))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
