#!/usr/bin/env python3
"""operator_current_sweep.py — sweep SMA drive conditions (current x pulse time).

WHAT THIS DOES
    Runs a ladder of CC conditions — each a (current, heat_ms, cool_s, cycles)
    cell — capturing the full stream per condition, with an in-run clock-aligned
    verdict (achieved current, signed displacement, force rise) printed live.
    Originally built to find the current ceiling for the RNN set; now the
    general condition runner. The measured envelope (2026-07-30): 150-950 mA at
    100 ms is clean end to end (21 -> 837 um), the binding ceiling is the LDO
    (~5.2 V / R_wire ~= 1.1 A), not any sensor.

USAGE
    # ALWAYS dry-run a profile first: it prints the plan WITHOUT opening the
    # port. A profile carries its own `port`, so a "syntax check" without
    # --dry-run WILL drive the rig (six 150 mA pulses fired this way).
    python operator_current_sweep.py --profile profiles/heat_time_map.json --dry-run
    python operator_current_sweep.py --profile profiles/heat_time_map.json

    # CLI form (no profile): 2D sweep, heat as the OUTER loop
    python operator_current_sweep.py --port COM8 \
        --levels 150,250,350,450,550,650,750,850,950 \
        --heat-ms 100,200,300,400 --cycles 5 --cool-s 15 --max-ma 1000

    # afterwards, ALWAYS:
    python operator_sweep_report.py data/raw/sweep_<stamp>

PROFILES (profiles/*.json) — the profile WINS over CLI flags for what it
    specifies, and a copy is saved into the output folder. Two forms:
      grid      "levels_ma": [...], "heat_ms": [...]  (ascending both)
      explicit  "conditions": [{"i_ma", "heat_ms", "cool_s"?, "cycles"?}, ...]
                executed IN PROFILE ORDER, repeats allowed (files get a c##_
                sequence prefix) — the shape the RNN collector will generate.
    Scalar keys: port, out, max_ma, settle_s, cool_s, cycles, i_low_ma,
    v_idle, r_min_ohm, abort_on_bad_sense, stop_on_fail.

WHY A LIMIT IS A PAIR, NOT A SINGLE NUMBER
    Analysis of console_20260715_193936_5V0.5V showed the load cell saturating
    NOT from any single pulse — the per-pulse rise was only ~1.5 V — but from
    the pre-pulse BASELINE ratcheting 2.43 V -> 5.00 V across cycles because a
    3 s cool never let the coil return. By cycle ~15 the rise had collapsed to
    0.004 V: thermally soaked, no longer actuating at all. Any ceiling is set
    jointly by PULSE ENERGY and COOL TIME; a sweep that ignores recovery
    reports a limit that is really a duty-cycle artefact. Longer pulses raise
    the stakes: the FIRST 200 ms cell of sweep_20260730_031337 shifted the
    coil ~5 mm and took the laser target out of its window for the rest of
    the run.

BEFORE RUNNING
    - Power-cycle USB + EVM (crc storms accumulate; ADC2 dies silently first).
    - Reseat the SMA clips (intermittent contact -> R_est latches wrong ->
      CC overshoots up to 2x; the sense check flags the impossible readings).
    - Check the laser target sits MID-WINDOW at rest and the LCA-9PC zero pot
      leaves headroom at the preloaded condition.

SAFETY
    Disarms in a finally block on every exit path including Ctrl-C; refuses
    setpoints above max_ma. Guards REPORT by default (collect first, threshold
    afterwards); --stop-on-fail / --abort-on-bad-sense restore hard stops for
    unattended runs.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_h7_session import (H7, SAT_GUARD_V, SRC_CC_U, SRC_LASER,  # noqa: E402
                            SRC_LOAD, SRC_SMA_I, align_m4, heat_windows,
                            m4_offset_from_capture, measurement_sane,
                            save_capture, window_stats)

# Calibrate_LaserHead/calibration.json (2026-05-27, r2 = 0.999998).
K_UM_PER_MV, V0_MV = -0.49779577092171906, 2503.7500968693835
disp_um = lambda v: (v * 1e3 - V0_MV) / K_UM_PER_MV

# Baseline captured BEFORE the cycle starts, so pulse 1 has a pre-window.
LEAD_IN_S = 2.0


def _hms(sec: float) -> str:
    """h:mm:ss for a duration; minutes-only under an hour so short runs stay
    readable at a glance."""
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def analyse_level(cap, heat_ms: float):
    """Per-pulse response for one current level, on BOTH channels.

    DISPLACEMENT decides whether the SMA actuated; FORCE decides whether the
    load cell is running out of range. Judging actuation on force was wrong and
    cost a whole sweep: this fixture is compliant, so a contracting coil mostly
    MOVES. Measured 2026-07-28 across 150-650 mA, per pulse:

        force        -0.18 .. -14.4 mN   inside the ~0.5 mN noise for most levels
        displacement  +4.9 .. -530  um   cleanly monotonic in current

    The load cell never saturated at all (380 mN peak against a 490 mN rating),
    so it cannot set the ceiling on 100 ms pulses — but it is still the right
    channel for the saturation guard if a longer pulse ever gets there.
    """
    out = []
    wins = heat_windows(cap)
    load = sorted(((s.hw_us * 1e-6, s.value) for s in cap.samples
                   if s.src == SRC_LOAD))
    disp = sorted(((s.hw_us * 1e-6, s.value) for s in cap.samples
                   if s.src == SRC_LASER))
    if not load:
        return out
    for k, (t0, t1) in enumerate(wins, 1):
        pre = [v for t, v in load if t0 - 0.40 <= t < t0 - 0.02]
        # Force peaks AFTER the current stops (thermal + mechanical lag), so
        # look well past the pulse rather than only inside it.
        post = [v for t, v in load if t0 <= t <= t1 + 1.5]
        if not pre or not post:
            continue
        base, peak = sum(pre) / len(pre), max(post)
        cur = window_stats(cap, SRC_SMA_I, t0, t1)
        row = {
            "cycle": k,
            "i_mA": 1e3 * cur["mean"] if cur else float("nan"),
            "baseline": base,
            "peak": peak,
            "rise": peak - base,
            "clipped": peak >= SAT_GUARD_V,
            "dx_um": float("nan"),
            "x_base_um": float("nan"),
        }
        xpre = [v for t, v in disp if t0 - 0.40 <= t < t0 - 0.02]
        xpost = [v for t, v in disp if t0 <= t <= t1 + 1.5]
        if xpre and xpost:
            xb = sum(xpre) / len(xpre)
            # Signed: take the largest EXCURSION either way, so the sign shows
            # which direction the stage moved without assuming the geometry.
            far = max(xpost, key=lambda v: abs(v - xb))
            row["x_base_um"] = disp_um(xb)
            row["dx_um"] = disp_um(far) - disp_um(xb)
        out.append(row)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=None,
                   help="JSON test profile. WINS over CLI flags for whatever it "
                        "specifies (flags fill the rest); a copy is saved into "
                        "the output folder. Two forms, 'conditions' preferred: "
                        "explicit `conditions: [{i_ma, heat_ms, cool_s?, "
                        "cycles?}, ...]` executed IN PROFILE ORDER (the RNN-"
                        "collector shape), or grid `levels_ma: [...]` x "
                        "`heat_ms: [...]` expanded heat-outer/level-inner. "
                        "Scalar keys: port, out, max_ma, settle_s, cool_s, "
                        "cycles, i_low_ma, v_idle, r_min_ohm, "
                        "abort_on_bad_sense, stop_on_fail. See "
                        "profiles/heat_time_map.json.")
    p.add_argument("--port", default="COM8")
    p.add_argument("--levels", default="150,250,350,450,550",
                   help="CC setpoints in mA, ascending (default 150..550)")
    p.add_argument("--heat-ms", default="100",
                   help="actuation pulse length(s) in ms. A comma list sweeps "
                        "heat time as the OUTER loop (e.g. 100,200,300,400): "
                        "each heat time runs the full level ladder before the "
                        "next, so every (heat, current) cell is captured.")
    p.add_argument("--cool-s", type=float, default=12.0,
                   help="cool per cycle; 3 s was demonstrably too short")
    p.add_argument("--cycles", type=int, default=3)
    p.add_argument("--settle-s", type=float, default=20.0,
                   help="cold-start wait BETWEEN levels")
    p.add_argument("--max-ma", type=float, default=800.0)
    p.add_argument("--headroom", type=float, default=4.5,
                   help="peak must stay below this (V) to pass")
    p.add_argument("--recover-frac", type=float, default=0.15,
                   help="baseline drift across cycles, as a fraction of rise")
    p.add_argument("--min-rise", type=float, default=0.05,
                   help="V of load rise below which a level counts as SUB-"
                        "THRESHOLD (skipped, not failed). Default 0.05 V "
                        "= 4.9 mN = ~10x the ~5 mV load-cell noise floor.")
    p.add_argument("--i-low", type=float, default=100.0,
                   help="cool-phase CC target in mA. 100 is ELECTRICALLY the "
                        "'cool = DAC code 0' design: it is below the reachable "
                        "floor (u_min 0.5 V / R ~4.7 ohm = ~106 mA) so the loop "
                        "rails at u_min — code 0, 0.5 V bias. Measured on a "
                        "healthy rig: cool sat at code 8, 0.510 V. "
                        "DO NOT SET 0. It disengages the loop, so the next "
                        "ccEngage() reseeds cc_u_i = codeToVldo(0) = 0.5 V and "
                        "the bootstrap integral RESTARTS EVERY PULSE with only "
                        "100 ms of ramp. Measured 2026-07-29 "
                        "(sweep_20260729_000147): 150 mA cmd latched R_est on "
                        "pulse 2 and reached 104%%, but 250 mA cmd NEVER emitted "
                        "src=7 at all and sat at 118 mA (47%%) for the whole run. "
                        "Keeping the loop engaged lets R_est latch off the "
                        "railed cool point, which had the whole cool phase to "
                        "settle.")
    p.add_argument("--min-dx-um", type=float, default=20.0,
                   help="displacement excursion below which a level counts as "
                        "SUB-THRESHOLD (skipped, not failed). ACTUATION IS "
                        "JUDGED ON THIS, not on force — the fixture is "
                        "compliant so the coil moves rather than loading. "
                        "Default 20 um is ~14x the ~1.4 um laser noise; "
                        "measured 4.9 um at 225 mA (noise) vs 44 um at 426 mA.")
    p.add_argument("--stop-on-fail", action="store_true",
                   help="halt the escalation at the first level that raises a "
                        "NOTE (clip / headroom / CC non-convergence / failed "
                        "segmentation) instead of collecting every level. For "
                        "UNATTENDED runs; the default reports and keeps going.")
    p.add_argument("--abort-on-bad-sense", action="store_true",
                   help="halt the sweep when the current sense reads physically "
                        "impossible values, instead of just warning. For "
                        "UNATTENDED runs — at the bench you are the guard.")
    p.add_argument("--v-idle", type=float, default=0.5,
                   help="LDO output at DAC code 0 (the cool-phase bias). Must "
                        "match the firmware `offset`. Used by the sense check.")
    p.add_argument("--r-min", type=float, default=1.6,
                   help="lowest believable wire resistance, ohm. A cool sample "
                        "implying less than this is physically impossible and "
                        "means the current sense is corrupted — the sweep "
                        "ABORTS rather than record unusable data. MUST TRACK "
                        "THE WIRE ON THE RIG at ~45%% of its cold resistance: "
                        "long coil ~4.2-4.8 ohm -> 2.0; the 2026-08-04 short "
                        "coil ~1.8 ohm -> 0.8; the Dynalloy coil fitted "
                        "2026-08-05 ~3.5 ohm -> 1.6 (the default). Set it per "
                        "wire, and prefer `r_min_ohm` in the profile so it "
                        "rides with the data.")
    p.add_argument("--out", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="print the resolved condition plan and exit WITHOUT "
                        "opening the serial port. ALWAYS use this to check a "
                        "profile: a profile carries its own `port`, so a "
                        "\"test\" invocation without it WILL drive the rig "
                        "(learned 2026-07-30 — six 650 mA pulses fired by a "
                        "profile syntax check).")
    a = p.parse_args()

    # -- profile intake. The profile WINS over flags for what it specifies. ---
    profile = {}
    if a.profile:
        import json as _json
        profile = _json.loads(Path(a.profile).read_text())
        for pk, ak in (("port", "port"), ("out", "out"), ("max_ma", "max_ma"),
                       ("settle_s", "settle_s"), ("cool_s", "cool_s"),
                       ("cycles", "cycles"), ("i_low_ma", "i_low"),
                       ("v_idle", "v_idle"), ("r_min_ohm", "r_min"),
                       ("abort_on_bad_sense", "abort_on_bad_sense"),
                       ("stop_on_fail", "stop_on_fail")):
            if pk in profile:
                setattr(a, ak, profile[pk])

    # -- build the condition list: {i_ma, heat_ms, cool_s, cycles} each. ------
    if profile.get("conditions"):
        # Explicit list, executed IN PROFILE ORDER — the profile owns the
        # sequencing (the RNN collector will hand us shuffled conditions).
        conditions = [{"i_ma": float(c["i_ma"]), "heat_ms": int(c["heat_ms"]),
                       "cool_s": float(c.get("cool_s", a.cool_s)),
                       "cycles": int(c.get("cycles", a.cycles))}
                      for c in profile["conditions"]]
        shape = f"{len(conditions)} explicit conditions from {a.profile}"
    else:
        if profile.get("levels_ma"):
            levels = [float(x) for x in profile["levels_ma"]]
            heats = [int(x) for x in profile.get("heat_ms", [100])]
        else:
            levels = [float(x) for x in a.levels.split(",") if x.strip()]
            heats = [int(x) for x in str(a.heat_ms).split(",") if x.strip()]
        if levels != sorted(levels):
            return _die("levels must be ascending; the sweep stops on failure")
        if heats != sorted(heats):
            return _die("heat_ms must be ascending (longer pulses are the "
                        "unexplored regime; walk into it, not out of it)")
        # Heat OUTER, level inner: each heat time reproduces the familiar
        # ascending-current ladder, so a fault shows up against a known
        # baseline.
        conditions = [{"i_ma": l, "heat_ms": h,
                       "cool_s": a.cool_s, "cycles": a.cycles}
                      for h in heats for l in levels]
        shape = (f"levels {levels} mA x heat {heats} ms | cool {a.cool_s:.0f} s"
                 f" | {a.cycles} cycles/condition")
    if not conditions:
        return _die("no conditions to run")
    if any(c["i_ma"] > a.max_ma for c in conditions):
        return _die(f"a condition exceeds max_ma ({a.max_ma:.0f} mA)")

    if a.dry_run:
        print(f"\nDRY RUN — plan only, port NOT opened ({a.port})")
        t_total = 0.0
        for k, c in enumerate(conditions):
            t_c = (a.settle_s + LEAD_IN_S + 4
                   + (c["cycles"] + 1) * (c["heat_ms"] / 1e3 + c["cool_s"]))
            t_total += t_c
            print(f"  {k:3d}: {c['i_ma']:6.0f} mA  {c['heat_ms']:4d} ms  "
                  f"cool {c['cool_s']:5.1f} s  x{c['cycles']}+1  (~{t_c:.0f} s)")
        print(f"  total ~{t_total/60:.0f} min")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = (Path(a.out) if a.out
           else Path(__file__).parent / "data" / "raw" / f"sweep_{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    if profile:                     # provenance: the profile rides with the data
        (out / "profile.json").write_text(
            Path(a.profile).read_text(), encoding="utf-8")

    per_cond = [a.settle_s + LEAD_IN_S + 4
                + (c["cycles"] + 1) * (c["heat_ms"]/1e3 + c["cool_s"])
                for c in conditions]
    print(f"\ncurrent sweep -> {out}")
    print(f"  {shape}")
    print(f"  {len(conditions)} conditions, ~{sum(per_cond)/60:.0f} min total\n")

    t_start = time.time()           # wall clock the ETA below paces against
    h7 = H7(a.port)
    rows, ceiling, stop_reason = [], None, "all levels passed"
    try:
        h7.open()
        h7.send("disarm")
        time.sleep(0.5)

        explicit = bool(profile.get("conditions"))
        for k, cond in enumerate(conditions):
            lvl, heat_ms = cond["i_ma"], cond["heat_ms"]
            cool_s, cycles = cond["cool_s"], cond["cycles"]
            # Progress + ETA. The remaining estimate is SELF-CORRECTING: it
            # rescales by how the run is actually pacing against the plan, so
            # per-condition overhead that the formula does not model (port
            # retries, analysis time) is absorbed instead of accumulating.
            elapsed = time.time() - t_start
            est_done, est_left = sum(per_cond[:k]), sum(per_cond[k:])
            scale = (elapsed / est_done) if est_done > 30 else 1.0
            left = est_left * scale
            eta = datetime.now() + timedelta(seconds=left)
            print(f"--- [{k + 1}/{len(conditions)}] {lvl:.0f} mA / {heat_ms} ms "
                  + "-" * 28)
            print(f"  elapsed {_hms(elapsed)} · remaining ~{_hms(left)} · "
                  f"ETA {eta:%H:%M:%S}"
                  + (f" · pacing {scale:.2f}x plan" if est_done > 30 else ""))
            print(f"  cold-start settle {a.settle_s:.0f}s ...", flush=True)
            h7.capture(a.settle_s, ping=False)

            h7.send("arm")
            time.sleep(0.6)

            # LEAD-IN. The first cccycle pulse starts ~60 ms after the command,
            # so without this there is no pre-pulse window and cycle 1 gets
            # silently dropped from the analysis (observed: 5 commanded, 4
            # reported). Capture a baseline first, then merge.
            lead = h7.capture(LEAD_IN_S, ping=True)

            # Gate on the LOAD channel actually streaming. ADC2 has died
            # mid-session repeatedly; without this the level runs to completion,
            # actuates the SMA, and produces no force data at all.
            if not lead.by_src(SRC_LOAD):
                stop_reason = (f"{lvl:.0f} mA: load cell (ADC2) is not "
                               f"streaming — hub fault. Power-cycle USB + EVM.")
                print(f"  STOP — {stop_reason}")
                h7.disarm()
                break

            # One EXTRA cycle: cccycle heats FIRST, so pulse 1 still runs before
            # any cool phase has had a chance to bootstrap R_est. It is a ramp,
            # not a measurement, and is dropped from the verdict below.
            cool_ms = int(cool_s * 1000)
            n_cyc = cycles + 1
            h7.send(f"cccycle {lvl:.0f} {a.i_low:.0f} {heat_ms} {cool_ms} {n_cyc}")
            run_s = n_cyc * (heat_ms / 1000.0 + cool_s) + 3.0

            faults = []
            cap = h7.capture(
                run_s, ping=True,
                on_console=lambda t, s: faults.append(s) if "FAULT" in s else None)
            cap.samples = lead.samples + cap.samples      # hw_us stays monotonic
            cap.console = lead.console + cap.console
            h7.send("stop")
            time.sleep(0.3)
            h7.disarm()

            # M4→M7 CLOCK OFFSET, applied LIVE. laser/load (src=1/2) are
            # stamped by the M4, the heat windows (src=6) by the M7, which runs
            # ~2.2 s AHEAD — joined untreated, every pre/post window lands on
            # the PREVIOUS pulse's decay tail (the bug that hid a whole
            # session's actuation, 2026-07-29). The CSV keeps RAW timestamps;
            # only the in-run verdict uses the aligned copy, and the offset is
            # recorded in meta.json so offline analysis applies the same shift.
            m4_off = m4_offset_from_capture(cap)
            # Explicit profiles may repeat a condition (a shuffled RNN set), so
            # their files carry the sequence index to stay collision-free.
            stem = (f"c{k:02d}_level_{lvl:.0f}mA_h{heat_ms}ms" if explicit
                    else f"level_{lvl:.0f}mA_h{heat_ms}ms")
            # PROVENANCE (guideline §9.3 "log the RNG seed", §7 "protocol
            # changes belong in meta.json"): a randomized campaign is only
            # reproducible if the profile identity and its seed travel with
            # every capture, not just with the profile.json copy in the folder.
            save_capture(cap, out / f"{stem}.csv",
                         {"seq": k, "level_mA": lvl, "heat_ms": heat_ms,
                          "cool_s": cool_s, "cycles": cycles,
                          "m4_clock_offset_s": m4_off,
                          "profile_name": profile.get("name"),
                          "profile_seed": profile.get("seed"),
                          "protocol": profile.get("protocol"),
                          "i_low_mA": a.i_low,
                          "captured_utc": datetime.now(timezone.utc).isoformat()})
            if m4_off == 0.0:
                print("  WARNING: no m7_us/m4_us STATUS line captured — "
                      "laser/load stay ~2.2 s early and the numbers below "
                      "measure the PREVIOUS pulse's tail")
            else:
                print(f"  clock align: src=1/2 shifted {m4_off:+.3f} s onto "
                      f"the M7 clock")
                cap = align_m4(cap, m4_off)

            # Distinguish the failure modes — they need opposite responses.
            # src=6 missing  -> the CC loop never engaged (arming / command)
            # src=2 missing  -> the load channel died (hub fault, power-cycle)
            per = analyse_level(cap, heat_ms)
            if not per:
                n_cc, n_load = len(cap.by_src(SRC_CC_U)), len(cap.by_src(SRC_LOAD))
                if n_cc == 0:
                    stop_reason = (f"{lvl:.0f} mA: CC never engaged (no src=6) "
                                   f"— check arming / the cccycle command")
                elif n_load == 0:
                    stop_reason = (f"{lvl:.0f} mA: load cell (ADC2) produced no "
                                   f"data — hub fault. Power-cycle USB + EVM.")
                else:
                    stop_reason = (f"{lvl:.0f} mA: {n_cc} cc samples and "
                                   f"{n_load} load samples, but no pulse had a "
                                   f"usable pre/post window")
                print(f"  STOP — {stop_reason}")
                break

            print(f"  {'cyc':>3} {'I[mA]':>7} {'dx[um]':>8} {'base':>7} "
                  f"{'peak':>7} {'rise':>7}  {'clip':>5}")
            for r in per:
                tag = " (bootstrap, excluded)" if r["cycle"] == 1 else ""
                print(f"  {r['cycle']:3d} {r['i_mA']:7.1f} {r['dx_um']:8.1f} "
                      f"{r['baseline']:7.3f} {r['peak']:7.3f} {r['rise']:7.3f}  "
                      f"{'YES' if r['clipped'] else 'no':>5}{tag}")
                r["level_mA"] = lvl
                r["heat_ms"] = heat_ms
                r["bootstrap"] = (r["cycle"] == 1)
                rows.append(r)

            # Pulse 1 is the bootstrap ramp — judging the level on it would
            # report the loop's convergence, not the SMA's response.
            #
            # NO `or per` FALLBACK. That silently made a level PASS on the very
            # cycle it claimed to exclude when segmentation returned a single
            # window (2026-07-28). If only the bootstrap pulse survives, the
            # segmentation is wrong and the run must stop, not report a number.
            per_v = [r for r in per if not r["bootstrap"]]
            if n_cyc == 1:
                # ONE-PULSE CONDITIONS (`cycles: 0`) are the randomized-
                # excitation protocol of DATA_COLLECTION_GUIDELINE §9.2:
                # consecutive pulses must differ in condition, so a condition
                # fires exactly once and its single pulse IS the measurement,
                # not a ramp to discard. Without this the run would `continue`
                # below and SKIP THE SENSE CHECK — silently disabling
                # --abort-on-bad-sense for the whole campaign, which is the one
                # guard an unattended night depends on. Measured 2026-08-05:
                # first-after-arm pulses reach 97-102 % of command on this
                # coil, so they are full-energy measurements.
                per_v = per
            if not per_v:
                # Still never judge the level on the bootstrap cycle — but the
                # raw capture is already saved, so skip to the next level rather
                # than ending the run.
                print(f"  NOTE: only {len(per)} pulse(s) found for {n_cyc} "
                      f"commanded — segmentation failed, skipping this level\n")
                if a.stop_on_fail:
                    stop_reason = f"{lvl:.0f} mA: segmentation failed"
                    break
                continue
            if len(per) < n_cyc:
                print(f"  NOTE: {len(per)} pulses found, {n_cyc} commanded")
            # Before believing ANY number from this level, check the current
            # sense is physically possible. A corrupted sense produced a whole
            # bad sweep on 2026-07-28 and a wrong controller diagnosis with it.
            ok, why = measurement_sane(cap, heat_windows(cap),
                                       v_idle=a.v_idle, r_min_ohm=a.r_min)
            # WARN, don't abort. An operator at the bench is a better judge than
            # a threshold, and a guard that halts a run on a heuristic wastes
            # more time than the bad data it prevents. --abort-on-bad-sense
            # restores the hard stop for unattended runs.
            if not ok:
                print(f"  !! SENSE WARNING: {why}")
                if a.abort_on_bad_sense:
                    stop_reason = f"{lvl:.0f} mA: {why}"
                    print(f"  ABORT — {stop_reason}\n")
                    break
            else:
                print(f"  sense check: {why}")
            i_ach = sum(r["i_mA"] for r in per_v) / len(per_v)
            print(f"  achieved current {i_ach:.0f} mA of {lvl:.0f} mA commanded "
                  f"({100*i_ach/lvl:.0f}%)")

            # ---- OBSERVATIONS, not verdicts --------------------------------
            # Everything below REPORTS. Nothing stops the sweep unless
            # --stop-on-fail. The thresholds that used to gate this were set
            # before we had any healthy data to set them from, and they halted
            # runs on levels that were working: the force-based --min-rise
            # called every level from 150-650 mA "SUB-THRESHOLD" while the wire
            # was plainly actuating, and --min-dx-um was calibrated against
            # drift-contaminated numbers. Collect first, threshold afterwards.
            clipped = any(r["clipped"] for r in per_v)
            over = max(r["peak"] for r in per_v) >= a.headroom
            drift = per_v[-1]["baseline"] - per_v[0]["baseline"]
            mean_rise = sum(r["rise"] for r in per_v) / len(per_v)
            dxs = [r["dx_um"] for r in per_v if r["dx_um"] == r["dx_um"]]
            print(f"  force: mean rise {mean_rise:+.4f} V, baseline drift "
                  f"{drift:+.4f} V over {len(per)} cycles")
            if dxs:
                sg, rect = sum(dxs)/len(dxs), sum(abs(x) for x in dxs)/len(dxs)
                print(f"  laser: {sg:+.1f} um mean signed / {rect:.1f} rectified "
                      f"({'coherent' if abs(sg) > 0.6*rect else 'incoherent'})")
            notes = []
            if faults:
                notes.append(f"firmware FAULT — {faults[0][:70]}")
            if clipped:
                notes.append("load cell CLIPPED at 5.000 V")
            elif over:
                notes.append(f"peak above the {a.headroom:.1f} V headroom")
            if i_ach < 0.80 * lvl:
                notes.append(f"CC reached only {100*i_ach/lvl:.0f}% of target")
            for n in notes:
                print(f"  NOTE: {n}")

            if notes and a.stop_on_fail:
                stop_reason = f"{lvl:.0f} mA: {notes[0]}"
                print(f"  STOP — {stop_reason}\n")
                break
            ceiling = f"{lvl:.0f} mA / {heat_ms} ms"
            print()
    except KeyboardInterrupt:
        stop_reason = "interrupted by user"
        print("\n  interrupted", file=sys.stderr)
    except Exception as e:                                       # noqa: BLE001
        stop_reason = f"error: {e}"
        print(f"\n  ERROR: {e}", file=sys.stderr)
    finally:
        h7.disarm()
        h7.close()

    with open(out / "summary.csv", "w", newline="") as fh:
        fh.write("level_mA,heat_ms,cycle,bootstrap,i_mA,dx_um,x_base_um,"
                 "baseline_V,peak_V,rise_V,clipped\n")
        for r in rows:
            fh.write(f"{r['level_mA']:.0f},{r['heat_ms']},{r['cycle']},"
                     f"{int(r['bootstrap'])},"
                     f"{r['i_mA']:.2f},{r['dx_um']:.2f},{r['x_base_um']:.2f},"
                     f"{r['baseline']:.5f},{r['peak']:.5f},{r['rise']:.5f},"
                     f"{int(r['clipped'])}\n")

    print("=" * 62)
    if ceiling is not None:
        print(f"  LAST CLEAN CONDITION: {ceiling}")
        print(f"  stopped because: {stop_reason}")
        print(f"  cool {a.cool_s:.0f} s — any limit found is a PAIR with the")
        print(f"  duty cycle; a shorter cool lowers it.")
    else:
        print(f"  NO LEVEL PASSED — {stop_reason}")
        if rows and max(r["rise"] for r in rows) < a.min_rise:
            print("  Every level was SUB-THRESHOLD: the SMA never actuated.")
            print("  Raise the ladder — heating energy goes as I^2 x t, so")
            print("  lengthening the cool will not help; nothing got hot.")
        else:
            print("  Lengthen --cool-s, or re-check the preload, then retry.")
    print(f"  raw + summary -> {out}")
    print("=" * 62)
    return 0


def _die(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
