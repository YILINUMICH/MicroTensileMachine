"""PHASE 2 of the RNN dataset-B session — generate the bulk collection profile.

    python make_rnn_profile.py --minutes 95 --gaps 3,5,8,12 --p-avg-max 0.25
    python make_rnn_profile.py --minutes 95 --gaps 5,8,12 --p-avg-max 0.15 --seed 2

Writes ../profiles/rnn_datasetB_<stamp>.json. ALWAYS --dry-run the result.

═══ WHY THIS IS GENERATED, NOT HAND-WRITTEN ════════════════════════════════
The gap set and the average-power cap are OUTPUTS of the soak ladder
(profiles/soak_ladder.json), which has to run first. Everything measured up to
2026-07-31 used 12-30 s gaps, i.e. 0.04-0.09 W duty-average. The same pulse at
a 2 s gap reaches 0.14-0.45 W -- 5-12x outside anything characterized. Writing
the bulk profile before that ladder runs would be guessing at the one number
that decides whether the coil survives the session.

═══ THE CAP THAT MATTERS HERE IS AVERAGE POWER, NOT PULSE ENERGY ═══════════
For the 30 s-cool campaign the binding limit was per-pulse energy (~1.24 J,
above which the instruments saturate). For short-gap sequences that limit still
applies, but it stops being the one that bites: what accumulates is

    P_avg = E_pulse / (heat_s + gap_s)

850x400 is 1.24 J either way, but at a 30 s gap that is 0.041 W and at a 2 s
gap it is 0.52 W. Both caps are enforced below; --p-avg-max is the soak number.

═══ SAMPLING ═══════════════════════════════════════════════════════════════
Stratified by gap (each gap gets an equal share of sequences), then (i, t)
sampled uniformly from the admissible cells within each stratum, then the whole
list SHUFFLED. Shuffling matters: if conditions ran in any systematic order,
slow drift -- coil fatigue, ambient, a degrading contact -- would correlate with
condition and the network would learn the drift.

Sub-threshold cells (150 mA, ~16 um) are deliberately admissible. "Power in,
nothing out" is a real machine state and the network should see it.

950x400 is excluded by the energy cap automatically (1.55 J), which is correct:
it saturates the laser AND the load cell at the commanded current, so it can
only ever contribute lower bounds.

═══ WHAT ONE SEQUENCE IS ═══════════════════════════════════════════════════
One condition = one training sequence = N+1 pulses of IDENTICAL current and
width at a fixed gap, coil armed throughout so R stays observable in the gaps.
Amplitude cannot vary inside a sequence -- the sweep disarms between conditions
-- so thermal memory appears here as pulse-to-pulse change within a constant-
drive burst. Lifting that needs a tool change (suppress the inter-condition
disarm), planned after this session.
"""
import argparse
import json
import os
import random

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(BASE, "..", "profiles")

LEVELS = [150, 250, 350, 450, 550, 650, 750, 850, 950]
HEATS = [100, 200, 300, 400]
R_OHM = 4.3                 # hot-state resistance, measured 4.03-4.7
E_MAX_J = 1.24              # per-pulse ceiling: 850x400, above which sensors rail
SETTLE_S = 30.0             # disarmed reset; worst-case recovery measured 12.4 s
LEAD_S = 2.6                # arm + lead-in overhead per condition


def energy_j(i_ma, heat_ms):
    return (i_ma / 1000.0) ** 2 * R_OHM * (heat_ms / 1000.0)


def seq_seconds(heat_ms, gap_s, cycles):
    return SETTLE_S + LEAD_S + (cycles + 1) * (heat_ms / 1000.0 + gap_s)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=95.0,
                   help="session budget for THIS profile (default 95)")
    p.add_argument("--gaps", default="3,5,8,12",
                   help="inter-pulse gaps in s, comma-separated — FROM THE SOAK LADDER")
    p.add_argument("--p-avg-max", type=float, default=0.25,
                   help="duty-average power ceiling in W — FROM THE SOAK LADDER")
    p.add_argument("--cycles", type=int, default=6,
                   help="measured pulses per sequence (tool fires cycles+1)")
    p.add_argument("--seed", type=int, default=1, help="reproducible sampling")
    p.add_argument("--out", default=None)
    a = p.parse_args()

    gaps = [float(g) for g in a.gaps.split(",") if g.strip()]
    rng = random.Random(a.seed)

    # admissible (level, heat, gap) under BOTH caps
    pool = {g: [] for g in gaps}
    for g in gaps:
        for lv in LEVELS:
            for h in HEATS:
                e = energy_j(lv, h)
                if e > E_MAX_J:
                    continue
                if e / (h / 1000.0 + g) > a.p_avg_max:
                    continue
                pool[g].append((lv, h))
    empty = [g for g in gaps if not pool[g]]
    for g in empty:
        print(f"  WARNING: gap {g:g} s admits NO cell at p_avg_max={a.p_avg_max} W")
        gaps.remove(g)
    if not gaps:
        raise SystemExit("no admissible conditions — raise --p-avg-max or the gaps")

    budget = a.minutes * 60.0
    conditions, used, per_gap = [], 0.0, {g: 0 for g in gaps}
    # round-robin over gaps so each stratum fills evenly even if we run out of time
    guard = 0
    while guard < 10000:
        guard += 1
        g = min(gaps, key=lambda x: per_gap[x])
        lv, h = rng.choice(pool[g])
        dt = seq_seconds(h, g, a.cycles)
        if used + dt > budget:
            break
        conditions.append({"i_ma": lv, "heat_ms": h, "cool_s": g,
                           "cycles": a.cycles})
        used += dt
        per_gap[g] += 1
    rng.shuffle(conditions)                       # decorrelate drift from condition

    stamp = a.out or f"rnn_datasetB_gap{'-'.join('%g' % g for g in gaps)}_s{a.seed}"
    path = os.path.abspath(os.path.join(PROFILES, stamp + ".json"))
    prof = {
        "name": stamp,
        "description": (
            f"RNN dataset-B bulk collection, generated by make_rnn_profile.py "
            f"(seed {a.seed}). {len(conditions)} sequences, {a.cycles}+1 pulses each, "
            f"shuffled. One condition = one training sequence: identical current and "
            f"width repeated at a fixed gap, coil armed throughout so R stays "
            f"observable between pulses. Gaps {gaps} s and the average-power cap "
            f"{a.p_avg_max} W COME FROM THE SOAK LADDER (profiles/soak_ladder.json) "
            f"— do not reuse these numbers for a different coil or preload without "
            f"re-running it. Both caps enforced: per-pulse energy <= {E_MAX_J} J "
            f"(above which the laser and load cell saturate) AND duty-average power "
            f"E/(t+gap) <= {a.p_avg_max} W, which is the one that binds at short "
            f"gaps. Shuffled so slow drift cannot correlate with condition. "
            f"Sub-threshold cells are deliberately included — 'power in, nothing "
            f"out' is a real state. 950x400 is excluded automatically by the energy "
            f"cap. Estimated {used/60:.0f} min."),
        "port": "COM8",
        "max_ma": 1000,
        "settle_s": SETTLE_S,
        "cool_s": gaps[0],
        "cycles": a.cycles,
        "i_low_ma": 0,
        "abort_on_bad_sense": True,
        "conditions": conditions,
    }
    with open(path, "w") as fh:
        json.dump(prof, fh, indent=2)

    print(f"  -> {os.path.relpath(path, BASE)}")
    print(f"  {len(conditions)} sequences, ~{used/60:.0f} min of {a.minutes:.0f}")
    print(f"  per gap: " + ", ".join(f"{g:g}s x{per_gap[g]}" for g in gaps))
    cells = sorted({(c['i_ma'], c['heat_ms']) for c in conditions})
    print(f"  {len(cells)} distinct (current, width) cells covered")
    pk = max(energy_j(c['i_ma'], c['heat_ms']) /
             (c['heat_ms'] / 1000.0 + c['cool_s']) for c in conditions)
    print(f"  worst duty-average power in the set: {pk:.3f} W "
          f"(cap {a.p_avg_max} W)")
    print("\n  NOW DRY-RUN IT:")
    print(f"  python operator_current_sweep.py --profile profiles/{stamp}.json --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
