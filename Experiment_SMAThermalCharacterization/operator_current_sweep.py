#!/usr/bin/env python3
"""operator_current_sweep.py — find the usable current ceiling for the rig.

WHAT THIS ANSWERS
    The highest CC setpoint that still actuates the SMA while keeping the load
    cell inside its 0-5 V window AND letting the force return to baseline
    between cycles. That current is the upper limit for the RNN data set.

WHY IT IS A PAIR, NOT A SINGLE NUMBER
    Analysis of console_20260715_193936_5V0.5V showed the load cell saturating
    NOT from any single pulse — the per-pulse rise was only ~1.5 V — but from
    the pre-pulse BASELINE ratcheting 2.43 V -> 5.00 V across cycles because a
    3 s cool never let the coil return. By cycle ~15 the rise had collapsed to
    0.004 V: thermally soaked, no longer actuating at all. So the ceiling is set
    jointly by CURRENT and COOL TIME, and a sweep that ignores recovery will
    report a limit that is really a duty-cycle artefact.

    That session also predates the 2026-07-24 sense fix, so its currents are 2x
    too high as recorded. This script measures fresh.

BEFORE RUNNING — re-zero the amplifier
    In that session the load cell sat at a 2.43 V BASELINE on a 0-5 V channel
    before anything was driven: static preload ate half the range. The LCA-9PC
    has a 25-turn zero pot. Zero it at the preloaded condition first, or this
    sweep will find an artificially low ceiling and you will redo it.

USAGE
    python operator_current_sweep.py --port COM8
    python operator_current_sweep.py --port COM8 --levels 150,250,350,450 \
                                     --heat-ms 100 --cool-s 12 --cycles 3

SAFETY
    Disarms in a finally block on every exit path including Ctrl-C; refuses
    setpoints above --max-ma; and STOPS ESCALATING the moment a level saturates
    or fails to recover, so it never climbs further into a bad regime.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib_h7_session import (H7, SAT_GUARD_V, SRC_LOAD, SRC_SMA_I,   # noqa: E402
                            heat_windows, save_capture, window_stats)


def analyse_level(cap, heat_ms: float):
    """Per-pulse baseline / peak / rise / recovery for one current level."""
    out = []
    wins = heat_windows(cap)
    load = sorted(((s.hw_us * 1e-6, s.value) for s in cap.samples
                   if s.src == SRC_LOAD))
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
        out.append({
            "cycle": k,
            "i_mA": 1e3 * cur["mean"] if cur else float("nan"),
            "baseline": base,
            "peak": peak,
            "rise": peak - base,
            "clipped": peak >= SAT_GUARD_V,
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="COM8")
    p.add_argument("--levels", default="150,250,350,450,550",
                   help="CC setpoints in mA, ascending (default 150..550)")
    p.add_argument("--heat-ms", type=int, default=100)
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
    p.add_argument("--out", default=None)
    a = p.parse_args()

    levels = [float(x) for x in a.levels.split(",") if x.strip()]
    if any(l > a.max_ma for l in levels):
        return _die(f"a level exceeds --max-ma ({a.max_ma:.0f} mA)")
    if levels != sorted(levels):
        return _die("--levels must be ascending; the sweep stops on failure")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(a.out) if a.out else Path(__file__).parent / "data" / f"sweep_{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"\ncurrent sweep -> {out}")
    print(f"  levels {levels} mA | heat {a.heat_ms} ms | cool {a.cool_s:.0f} s "
          f"| {a.cycles} cycles/level\n")

    h7 = H7(a.port)
    rows, ceiling, stop_reason = [], None, "all levels passed"
    try:
        h7.open()
        h7.send("disarm")
        time.sleep(0.5)

        for lvl in levels:
            print(f"--- {lvl:.0f} mA " + "-" * 46)
            print(f"  cold-start settle {a.settle_s:.0f}s ...", flush=True)
            h7.capture(a.settle_s, ping=False)

            h7.send("arm")
            time.sleep(0.6)
            cool_ms = int(a.cool_s * 1000)
            h7.send(f"cccycle {lvl:.0f} 0 {a.heat_ms} {cool_ms} {a.cycles}")
            run_s = a.cycles * (a.heat_ms / 1000.0 + a.cool_s) + 3.0

            faults = []
            cap = h7.capture(
                run_s, ping=True,
                on_console=lambda t, s: faults.append(s) if "FAULT" in s else None)
            h7.send("stop")
            time.sleep(0.3)
            h7.disarm()

            save_capture(cap, out / f"level_{lvl:.0f}mA.csv",
                         {"level_mA": lvl, "heat_ms": a.heat_ms,
                          "cool_s": a.cool_s, "cycles": a.cycles,
                          "captured_utc": datetime.utcnow().isoformat() + "Z"})

            per = analyse_level(cap, a.heat_ms)
            if not per:
                stop_reason = f"{lvl:.0f} mA: no heat pulses seen — check arming"
                print(f"  {stop_reason}")
                break

            print(f"  {'cyc':>3} {'I[mA]':>7} {'base':>7} {'peak':>7} "
                  f"{'rise':>7}  {'clip':>5}")
            for r in per:
                print(f"  {r['cycle']:3d} {r['i_mA']:7.1f} {r['baseline']:7.3f} "
                      f"{r['peak']:7.3f} {r['rise']:7.3f}  "
                      f"{'YES' if r['clipped'] else 'no':>5}")
                r["level_mA"] = lvl
                rows.append(r)

            # ---- verdict for this level ------------------------------------
            clipped = any(r["clipped"] for r in per)
            over = max(r["peak"] for r in per) >= a.headroom
            drift = per[-1]["baseline"] - per[0]["baseline"]
            mean_rise = sum(r["rise"] for r in per) / len(per)
            no_recover = mean_rise > 0 and drift > a.recover_frac * mean_rise
            collapsed = per[-1]["rise"] < 0.25 * per[0]["rise"]

            print(f"  baseline drift {drift:+.3f} V over {len(per)} cycles "
                  f"(mean rise {mean_rise:.3f} V)")
            if faults:
                stop_reason = f"{lvl:.0f} mA: firmware FAULT — {faults[0][:70]}"
            elif clipped:
                stop_reason = f"{lvl:.0f} mA: load cell CLIPPED at 5.000 V"
            elif over:
                stop_reason = (f"{lvl:.0f} mA: peak exceeded the "
                               f"{a.headroom:.1f} V headroom limit")
            elif no_recover:
                stop_reason = (f"{lvl:.0f} mA: baseline did not recover "
                               f"(drift {drift:+.3f} V) — lengthen --cool-s")
            elif collapsed:
                stop_reason = (f"{lvl:.0f} mA: response collapsed "
                               f"({per[0]['rise']:.3f} -> {per[-1]['rise']:.3f} V) "
                               f"— thermally soaked")
            else:
                ceiling = lvl
                print(f"  PASS\n")
                continue
            print(f"  STOP — {stop_reason}\n")
            break
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
        fh.write("level_mA,cycle,i_mA,baseline_V,peak_V,rise_V,clipped\n")
        for r in rows:
            fh.write(f"{r['level_mA']:.0f},{r['cycle']},{r['i_mA']:.2f},"
                     f"{r['baseline']:.5f},{r['peak']:.5f},{r['rise']:.5f},"
                     f"{int(r['clipped'])}\n")

    print("=" * 62)
    if ceiling is not None:
        print(f"  HIGHEST PASSING LEVEL: {ceiling:.0f} mA")
        print(f"  stopped because: {stop_reason}")
        print(f"\n  Use {ceiling:.0f} mA as the RNN upper current bound.")
        print(f"  Verified at heat {a.heat_ms} ms / cool {a.cool_s:.0f} s — the")
        print(f"  limit is a PAIR; a shorter cool lowers it.")
    else:
        print(f"  NO LEVEL PASSED — {stop_reason}")
        print("  Re-zero the amplifier and/or lengthen --cool-s, then retry.")
    print(f"  raw + summary -> {out}")
    print("=" * 62)
    return 0


def _die(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
