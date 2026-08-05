"""Generate the overnight collection profiles, to NN_SelfSensing_Baseline's
DATA_COLLECTION_GUIDELINE.md §9 (excitation protocol). Deterministic (seeded).

Run:  python gen_night_profiles.py        # rewrites n*.json next to this file

═══ WHAT §9 REQUIRES, AND HOW EACH REQUIREMENT IS MET ══════════════════════

§9.1 ENVELOPE — draw only where the response is measurable and the drive is
     trustworthy. `ENVELOPE_*` below is measured from the CURRENT coil
     (Dynalloy short wire fitted 2026-08-05) via the two 08-05 sweeps:
     median |stroke| >= 3x the cell's own sigma AND >= 50 um, not railed, not
     clipped-in-current, cc within 10 %. That admitted 36 of 44 cells and
     rejected the whole 200 mA column plus 250x100/200/300 and 350x100 —
     hence T_MIN_MS rising as current falls.

§9.2 RANDOMIZE — (a) draws are CONTINUOUS via Latin-hypercube sampling, not
     the 8x5 grid nodes; (b) execution order is shuffled so CONSECUTIVE PULSES
     DIFFER IN CONDITION. (b) is why the shuffle blocks use `cycles: 0` —
     ONE PULSE PER CONDITION. With `cycles: 1` the runner fires two pulses at
     the SAME (I,t) 30 s apart, so every measured pulse's predecessor is an
     identical command and the recurrent window sees exactly two thermal-
     history classes (cold start / 30 s after an identical pulse) — both a
     deterministic function of the command, which is the confound §9 exists to
     remove. Verified on 08-05 data: same cell, cold-start vs 30-s-later
     stroke differs consistently (250x400: 74.7 vs 52.1 um).

§9.3 REPEATS + SEED — REPEAT_FRAC of each pool is duplicated at identical
     (I, t, cool) and re-inserted far from its twin, to keep estimating the
     repeatability floor. The seed and the envelope ride in every profile
     (and, via operator_current_sweep.py, in every capture's meta.json).

§9.5 PARTIAL HEATING AND COOLING — `cool_s` is randomized PER CONDITION, so
     each pulse lands on a differently-recovered coil and "time since last
     pulse" stops being inferable from (I, t). The draw is floored by pulse
     energy (`cool_floor_s`) because a short cool after a high-energy pulse
     ratchets the baseline instead of actuating — the documented duty-cycle
     artefact in operator_current_sweep.py's header.

═══ SAFETY (unattended) ═════════════════════════════════════════════════════
  * 950 mA EXCLUDED overnight (asset protection) — pools top out at I_MAX.
  * r_min 1.6 ohm matches the fitted Dynalloy coil; abort_on_bad_sense true.
  * n4's short cools are capped at I4_MAX so the OOD cool rhythm cannot be
    combined with the highest pulse energies.

Blocks (roles PRE-REGISTERED here, not at training time). Big pools are split
in halves so one sense-abort at 03:00 costs half a block, not all of it:
  n0 / n3 / n9  anchors      drift bracket, 5 cycles, identical all night
  n1a / n1b     TRAINABLE    randomized envelope draws, one pulse each
  n2a / n2b     TEST-ONLY    same protocol, different seed (OOD duty)
  n4a / n4b     TEST-ONLY    short-cool OOD (warm coil, incomplete decay)
  n5            trainable    60 s cool, deep late-cool tails + tau re-check
  n6            TEST-ONLY    parent-map repeats, cross-session probe
  n7            trainable    steep-region ladder fill
  n8            trainable    near-threshold floor (DELIBERATELY sub-envelope,
                             kept per §9.4 — flagged, not filtered)
"""
import json
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent

BASE = dict(port="COM8", max_ma=1000, i_low_ma=0, v_idle=0.5,
            r_min_ohm=1.6, abort_on_bad_sense=True)

# ── §9.1 envelope, measured from the 2026-08-05 Dynalloy sweeps ─────────────
I_MIN, I_MAX = 250.0, 900.0        # 950 excluded overnight
T_MAX_MS = 500
# Lowest heat time that still clears the 50 um / 3-sigma floor, per level.
# Anchors of the measured boundary; linear in between.
T_MIN_ANCHORS = [(250.0, 400.0), (350.0, 200.0), (450.0, 100.0),
                 (900.0, 100.0)]
I_STEP_MA, T_STEP_MS = 5.0, 5       # quantisation only, not a grid
REPEAT_FRAC = 0.10

# Randomized cool, floored by normalised pulse energy e = (I/I_MAX)^2*(t/T_MAX)
COOL_LO, COOL_HI = 12.0, 45.0
COOL_FLOOR_LO, COOL_FLOOR_SPAN = 12.0, 23.0
# n4: OOD short-cool block, and the current cap that makes it safe unattended
COOL4_LO, COOL4_HI = 5.0, 11.0
COOL4_FLOOR_LO, COOL4_FLOOR_SPAN = 5.0, 6.0
I4_MAX = 650.0

SETTLE_SHUFFLE_S = 2.0    # tiny: the pulses must form ONE randomized train
SETTLE_BLOCK_S = 40.0     # structured blocks keep the cold-start lead-in


def t_min_ms(i_ma: float) -> float:
    """Envelope floor: the shortest heat time that still actuates at `i_ma`."""
    xs = [a[0] for a in T_MIN_ANCHORS]
    ys = [a[1] for a in T_MIN_ANCHORS]
    return float(np.interp(i_ma, xs, ys))


def lhs(rng, n: int) -> np.ndarray:
    """One Latin-hypercube column in [0,1): stratified, one sample per bin."""
    return (rng.permutation(n) + rng.random(n)) / n


def draw_pool(rng, n: int, cool_lo: float, cool_hi: float,
              floor_lo: float, floor_span: float,
              i_max: float = I_MAX) -> list[tuple[float, int, float]]:
    """§9.2 continuous LHS draws inside the §9.1 envelope, + §9.3 repeats.

    Returns [(i_ma, heat_ms, cool_s), ...] in EXECUTION order.
    """
    n_base = n - int(REPEAT_FRAC * n)
    ui, ut, uc = lhs(rng, n_base), lhs(rng, n_base), lhs(rng, n_base)
    pool = []
    for a, b, c in zip(ui, ut, uc):
        i = I_MIN + a * (i_max - I_MIN)
        i = round(i / I_STEP_MA) * I_STEP_MA
        tlo = t_min_ms(i)
        t = tlo + b * (T_MAX_MS - tlo)
        t = int(round(t / T_STEP_MS) * T_STEP_MS)
        e = (i / I_MAX) ** 2 * (t / T_MAX_MS)          # normalised energy
        floor = floor_lo + floor_span * e
        cool = max(cool_lo + c * (cool_hi - cool_lo), floor)
        pool.append((float(i), t, round(float(cool), 1)))

    # §9.3 repeats: exact duplicates, deliberately spread across the run. The
    # twins must be FAR APART or they measure nothing useful — a pair three
    # pulses apart cannot separate the repeatability floor from slow drift,
    # which is the whole reason the guideline asks for them. So sources are
    # drawn from the FIRST third of the sequence and their copies inserted
    # across the LAST third, forcing a separation of roughly n/3.
    rng.shuffle(pool)
    n_rep = n - n_base
    if n_rep:
        head = max(1, len(pool) // 3)
        idx = rng.choice(head, min(n_rep, head), replace=False)
        reps = [pool[k] for k in idx]
        tail_start = max(head + 1, (2 * len(pool)) // 3)
        step = max(1, (len(pool) - tail_start) // (len(reps) + 1))
        for j, r in enumerate(reps):
            pool.insert(min(len(pool), tail_start + (j + 1) * step), r)

    # §9.2(b): consecutive pulses must DIFFER in condition. Push any adjacent
    # identical level further down the sequence.
    for k in range(1, len(pool)):
        if pool[k][0] == pool[k - 1][0]:
            for j in range(k + 1, len(pool)):
                if pool[j][0] != pool[k - 1][0] and (
                        k + 1 >= len(pool) or pool[j][0] != pool[k + 1][0]):
                    pool[k], pool[j] = pool[j], pool[k]
                    break
    return pool


ENVELOPE_META = {
    "source": "sweep_20260805_105318 + sweep_20260805_154528 (Dynalloy short "
              "wire fitted 2026-08-05)",
    "rule": "guideline 9.1: median |dx| >= max(50 um, 3*sigma), not railed, "
            "cc within 10%",
    "i_ma": [I_MIN, I_MAX],
    "heat_ms_max": T_MAX_MS,
    "heat_ms_min_vs_i_ma": [[i, t] for i, t in T_MIN_ANCHORS],
    "excluded_overnight": "950 mA (asset protection, unattended)",
    "rejected_cells": ["200x100", "200x200", "200x300", "200x400",
                       "250x100", "250x200", "250x300", "350x100"],
}


def write(name: str, desc: str, conds, *, cycles: int, settle_s: float,
          seed=None, protocol: str, cool_s=None) -> float:
    """Write one profile. `conds` is [(i, t)] or [(i, t, cool)]."""
    out = []
    for c in conds:
        d = {"i_ma": c[0], "heat_ms": c[1]}
        if len(c) > 2:
            d["cool_s"] = c[2]
        out.append(d)
    p = dict(name=name, description=desc, protocol=protocol, **BASE,
             settle_s=settle_s, cycles=cycles, conditions=out)
    if cool_s is not None:
        p["cool_s"] = float(cool_s)
    if seed is not None:
        p["seed"] = int(seed)
        p["envelope"] = ENVELOPE_META
    (OUT / f"{name}.json").write_text(json.dumps(p, indent=2) + "\n")
    # Match operator_current_sweep.py's own model: per condition it waits
    # settle_s, then LEAD_IN_S + ~4 s of overhead, then (cycles+1) pulses.
    return sum(settle_s + 2.0 + 4.0
               + (cycles + 1) * (c[1] / 1e3 + (c[2] if len(c) > 2
                                               else (cool_s or 0.0)))
               for c in conds)


ANCHOR = [(650.0, 300), (450.0, 400)]
RANDOM_PROTO = ("guideline 9.2 randomized excitation: continuous LHS draws "
                "inside the 08-05 envelope, ONE pulse per condition, "
                "cool_s randomized per condition")
BLOCK_PROTO = "structured characterization block, repeated cycles per condition"

total = 0.0
plan = []


def add(*a, **k):
    global total
    t = write(*a, **k)
    total += t
    plan.append((a[0], t))


# ── anchors: identical all night, so drift is measured, not inferred ────────
for nm, when in (("n0_anchor_start", "start"), ("n3_anchor_mid", "mid-night"),
                 ("n9_anchor_end", "end of night")):
    add(nm, f"Overnight drift bracket, {when}. The SAME two conditions run at "
            f"n0/n3/n9; compare dx and r_base across the three to measure "
            f"drift and fatigue directly. Excluded from training (measurement, "
            f"not data).",
        ANCHOR, cycles=5, settle_s=SETTLE_BLOCK_S, cool_s=30.0,
        protocol=BLOCK_PROTO)

# ── n1: TRAINABLE randomized pool, split in halves ─────────────────────────
for tag, seed, n in (("a", 20260805, 200), ("b", 20260806, 200)):
    rng = np.random.default_rng(seed)
    add(f"n1{tag}_shuffle_train",
        f"TRAINABLE randomized excitation, half {tag.upper()} of 2. One pulse "
        f"per condition so every pulse follows a DIFFERENT predecessor; "
        f"cool_s randomized so thermal history is not inferable from the "
        f"command. Role pre-registered: TRAINABLE.",
        draw_pool(rng, n, COOL_LO, COOL_HI, COOL_FLOOR_LO, COOL_FLOOR_SPAN),
        cycles=0, settle_s=SETTLE_SHUFFLE_S, seed=seed, protocol=RANDOM_PROTO)

# ── n2: TEST-ONLY, same protocol, unseen sequence ──────────────────────────
for tag, seed, n in (("a", 20260807, 75), ("b", 20260808, 75)):
    rng = np.random.default_rng(seed)
    add(f"n2{tag}_shuffle_test",
        f"OOD duty TEST set, half {tag.upper()} of 2 — same protocol as n1, "
        f"different seed, so the SEQUENCE is unseen. Role pre-registered: "
        f"TEST-ONLY, never in train or validation.",
        draw_pool(rng, n, COOL_LO, COOL_HI, COOL_FLOOR_LO, COOL_FLOOR_SPAN),
        cycles=0, settle_s=SETTLE_SHUFFLE_S, seed=seed, protocol=RANDOM_PROTO)

# ── n4: TEST-ONLY short-cool OOD (5-11 s, disjoint from n1/n2's 12-45 s) ───
for tag, seed, n in (("a", 20260809, 110), ("b", 20260810, 110)):
    rng = np.random.default_rng(seed)
    add(f"n4{tag}_shortcool_test",
        f"Interrupted-cooling OOD TEST, half {tag.upper()} of 2. Cool 5-11 s "
        f"— DISJOINT from n1/n2's 12-45 s — so pulses land on a partially "
        f"recovered coil and the estimator must track incomplete decay; a "
        f"command-lookup model fails here first. Current capped at "
        f"{I4_MAX:.0f} mA so a short cool never meets the highest pulse "
        f"energy unattended. Role pre-registered: TEST-ONLY.",
        draw_pool(rng, n, COOL4_LO, COOL4_HI, COOL4_FLOOR_LO,
                  COOL4_FLOOR_SPAN, i_max=I4_MAX),
        cycles=0, settle_s=SETTLE_SHUFFLE_S, seed=seed, protocol=RANDOM_PROTO)

# ── structured blocks (unchanged roles) ────────────────────────────────────
add("n5_longcool",
    "Deep cooling tails: 60 s cool for extra late-cool data in the SNR-starved "
    "regime, plus tau re-check on the fitted coil. Trainable.",
    [(350.0, 500), (550.0, 500), (750.0, 500), (850.0, 500),
     (650.0, 300), (750.0, 200)],
    cycles=3, settle_s=SETTLE_BLOCK_S, cool_s=60.0, protocol=BLOCK_PROTO)

add("n6_repeats",
    "Cross-session probe cells: exact repeats of parent-map conditions, so "
    "session-to-session transfer can be measured against the 07-31 and 08-05 "
    "campaigns. Role pre-registered: TEST-ONLY.",
    [(450.0, 300), (750.0, 200), (650.0, 100), (550.0, 400),
     (350.0, 300), (850.0, 200), (250.0, 400), (650.0, 400)],
    cycles=5, settle_s=SETTLE_BLOCK_S, cool_s=30.0, protocol=BLOCK_PROTO)

add("n7_ladder",
    "Steep-region thickening: 500 ms row, 50 mA steps, 950 mA excluded "
    "unattended (asset protection). Trainable.",
    [(float(i), 500) for i in range(550, 901, 50)],
    cycles=5, settle_s=SETTLE_BLOCK_S, cool_s=30.0, protocol=BLOCK_PROTO)

add("n8_threshold",
    "Near-threshold floor: long low-current pulses. DELIBERATELY BELOW the "
    "9.1 envelope (200-250 mA fails the 50 um floor on this coil) and kept "
    "per 9.4 — the network should see non-response as a real machine state. "
    "Flagged by its own low stroke, never filtered here.",
    [(200.0, 500), (200.0, 650), (200.0, 800), (250.0, 500),
     (250.0, 650), (250.0, 800)],
    cycles=5, settle_s=SETTLE_BLOCK_S, cool_s=30.0, protocol=BLOCK_PROTO)

# ── report ─────────────────────────────────────────────────────────────────
print(f"\n{'profile':26s} {'cond':>5} {'pulses':>7} {'~min':>6}")
files = sorted(OUT.glob("n*.json"))
n_pulses = 0
for f in files:
    p = json.loads(f.read_text())
    n = len(p["conditions"])
    pl = n * (p["cycles"] + 1)
    n_pulses += pl
    t = dict(plan)[p["name"]]
    print(f"{f.name:26s} {n:5d} {pl:7d} {t/60:6.0f}")
print(f"\n{len(files)} profiles, {n_pulses} pulses, "
      f"~{total/3600:.2f} h (+ queue gaps)")
print("run with:  python operator_profile_queue.py "
      "profiles/night_profiles_20260805 --dry-run")
