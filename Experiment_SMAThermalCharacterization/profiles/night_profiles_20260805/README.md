# Overnight collection — 2026-08-05/06, ~9.2 h in a 12 h window

Built to `NN_SelfSensing_Baseline/DATA_COLLECTION_GUIDELINE.md` §9 (excitation
protocol). Regenerate with `python gen_night_profiles.py`; that script's header
documents how each §9 clause is satisfied.

**Run the whole night with one command** (profiles execute in sorted order,
which is the intended order):

```
# 1. ALWAYS dry-run first — validates all 13 profiles, opens no port
python operator_profile_queue.py profiles/night_profiles_20260805 --dry-run

# 2. the real run
python operator_profile_queue.py profiles/night_profiles_20260805 --deadline 07:00
```

The queue runs each profile back-to-back, runs `operator_sweep_report.py` after
each, writes `queue_<stamp>/queue_manifest.json` (which capture folder came from
which pre-registered role), and **treats a failed profile as one lost profile,
not a lost night**. It abandons the queue only if two profiles in a row capture
nothing — the signature of a rig that needs a power-cycle.

| profile | cond | pulses | ~min | role (FROZEN) |
|---|---|---|---|---|
| n0_anchor_start | 2 | 12 | 8 | drift bracket — repeats at n3, n9 |
| n1a_shuffle_train | 200 | 200 | 126 | **TRAINABLE** — randomized envelope draws |
| n1b_shuffle_train | 200 | 200 | 128 | **TRAINABLE** — half B, different seed |
| n2a_shuffle_test | 75 | 75 | 48 | **TEST-ONLY** — unseen sequence |
| n2b_shuffle_test | 75 | 75 | 47 | **TEST-ONLY** — half B |
| n3_anchor_mid | 2 | 12 | 8 | drift bracket |
| n4a_shortcool_test | 110 | 110 | 30 | **TEST-ONLY** — 5–11 s cool, warm coil |
| n4b_shortcool_test | 110 | 110 | 30 | **TEST-ONLY** — half B |
| n5_longcool | 6 | 24 | 29 | trainable — 60 s deep cooling tails |
| n6_repeats | 8 | 48 | 30 | **TEST-ONLY** — cross-session probe |
| n7_ladder | 8 | 48 | 31 | trainable — 550–900 mA × 500 ms fill |
| n8_threshold | 6 | 36 | 23 | trainable — sub-envelope stroke floor |
| n9_anchor_end | 2 | 12 | 8 | drift bracket + morning health check |

**962 pulses, ~9.2 h.** Every pulse in n1/n2/n4 is a history-diverse sample.

## Before starting (10 min, do not skip)

1. **Power-cycle USB + EVM.** Non-negotiable after any upload, and it clears
   accumulated CRC storms (ADC2 dies silently first).
2. **Re-baseline the load cell cold to ~1.8–2.0 V.** Confirmed necessary:
   today's two sweeps clipped 14/160 and 11/60 pulses at the 5 V rail. Force
   truth is lost above ~750 mA all night without this. (Force is a held-out
   channel downstream, so clipping does not invalidate a pulse — but it does
   waste the channel.)
3. **Confirm the laser cold baseline near the 5 V end** (~4.86 V today). The
   full contracting window must be available: n1 draws up to 900 mA × 500 ms,
   and 950 × 500 moved 7.6 mm today against a 10.0 mm window.
4. **Reseat the SMA clips.** Intermittent contact latches `R_est` wrong and CC
   overshoots up to 2×.
5. **Disk:** ~962 captures at ~20 MB ≈ **19 GB**. 381 GB free, so the run fits —
   but see *Committing the results* below before `git add`.

## Why the protocol changed from the first draft

The first draft used `cycles: 1` with `settle_s: 40`, which fires **two pulses
at the same (I,t) 30 s apart** and then waits ~70 s before the next condition.
That gives the recurrent window exactly **two thermal-history classes** — cold
start, and 30 s after an identical pulse — both a deterministic function of the
command. Shuffling the condition order buys nothing, because the inter-condition
gap erases the history being shuffled. Guideline §9.2 asks for the opposite:
*"shuffle the execution order so consecutive pulses differ in condition."*

Evidence the reset is real (08-05 data, same cell, cold-start vs 30 s later):
250×400 → 74.7 vs 52.1 µm, 250×300 → 53.6 vs 43.9, 250×200 → 43.0 vs 33.0.
Cold-start pulses are consistently larger and tightly grouped within class.

So the shuffle blocks now use **`cycles: 0` — one pulse per condition** with
`settle_s: 2`, making the whole block one randomized excitation train. Two
supporting facts, both measured on today's captures:

- **First-after-arm pulses are full-energy measurements on this rig**: cc_pct
  97–102 %, indistinguishable from later cycles. `operator_current_sweep.py`'s
  "bootstrap is a ramp, not a measurement" warning describes the pre-seed
  firmware on the long coil and does not apply here.
- **`prepare_pulse_runs.py` keeps `bootstrap == 1` rows** explicitly ("real
  full-energy pulse, different initial condition"), so a one-pulse protocol is
  not silently dropped downstream.

`i_low_ma: 0` is kept despite the `--i-low` docstring's "DO NOT SET 0": today's
two sweeps ran with 0 and reached 97–103 % of command.

## Design rationale (what each block buys)

- **Randomized pool (n1/n2/n4)** — continuous Latin-hypercube draws inside the
  measured envelope, not the old 8×5 grid nodes (§9.2a). 180 of 200 conditions
  in n1a are distinct; essentially none land on a grid node.
- **`cool_s` randomized per condition** (§9.5 partial heating and cooling) so
  "time since last pulse" is not inferable from (I, t). n1/n2 draw 12–45 s;
  **n4 draws 5–11 s, disjoint from n1/n2**, which is what makes it a genuine
  OOD cool rhythm rather than a resample. The draw is floored by pulse energy,
  because a short cool after a high-energy pulse ratchets the baseline instead
  of actuating.
- **n4 caps current at 650 mA** so the OOD short cool never meets the highest
  pulse energy unattended.
- **Repeats** — 10 % of each pool duplicated at identical (I, t, cool), twins
  forced 32–76 pulses apart so the repeatability floor can be separated from
  slow drift.
- **Anchors (n0/n3/n9)** measure overnight drift directly: same command, three
  times. If n9 dx differs from n0 by ≫ the within-condition spread (~±5 %), the
  night needs a drift covariate or a trim.
- **950 mA excluded overnight** (asset protection, unattended); the ladder tops
  at 900. Today's attended sweep already covers 950.
- **n8 is deliberately sub-envelope** (200–250 mA fails the 50 µm floor on this
  coil) and kept per §9.4 — the network should see non-response as a real
  machine state.
- Overnight HVAC ambient drift is free robustness variation; `r_base` tracks it.

## The envelope (§9.1), measured on the current coil

From `sweep_20260805_105318` + `sweep_20260805_154528` (Dynalloy short wire
fitted 2026-08-05): a cell is admitted if median |stroke| ≥ max(50 µm, 3σ), not
railed, not off-drive. **36 of 44 cells admitted.** The rejected cells set the
lower boundary, so draws respect a current-dependent minimum heat time:

| level | admissible heat |
|---|---|
| 250 mA | ≥ 400 ms |
| 350 mA | ≥ 200 ms |
| ≥ 450 mA | ≥ 100 ms |

Rejected: the whole 200 mA column, 250×100/200/300, 350×100. Nothing railed —
the laser window is healthy across the full ~10 mm.

The envelope rides in each randomized profile under `envelope`, with the seed
under `seed`; `operator_current_sweep.py` now also writes `profile_name`,
`profile_seed`, `protocol` and `i_low_mA` into **every capture's `meta.json`**,
so a randomized campaign is reproducible from the captures alone (§9.3, §7).

## Split rules downstream

- **n2, n4, n6 never appear in any training or validation config.**
- n1/n5/n7/n8 join the existing sweeps as the training pool (split by capture,
  stratified).
- Anchors are excluded from training — they are measurement, not data.

## Two known downstream consequences

1. **Every night pulse carries `bootstrap = 1`** (`analyze_raw.py` flags the
   first window in a capture, and each capture now holds one pulse). The flag is
   semantically right — every pulse *is* first-after-engage — but it stops
   discriminating. Use `cc_pct` for drive quality, as §8.2 and §9.1 both
   specify.
2. **`prepare_pulse_runs.py`'s cell-statistics filter becomes a no-op** for this
   data: it computes per-cell median/σ from `bootstrap == 0` rows, of which the
   night has none, and continuous draws give most cells n = 1 anyway. This is
   largely mitigated because **the §9.1 envelope is now applied at draw time**
   instead of downstream — but the filter should be revisited before training on
   randomized campaigns.

## In the morning — the standard figure set

The queue already runs `operator_sweep_report.py` after each profile. Then:

```
cd analysis
python plot_drive_trajectory.py --all        # 4 channels x 2 time scales, per sweep
python analyze_raw.py                        # add the night's folders to CAMPAIGNS first
python plot_envelope.py ../data/derived/heat_time_map_<campaign>_all.csv
```

`plot_drive_trajectory.py` handles this campaign's one-pulse conditions: it uses
the single pulse and labels the figure `1 pulse per condition (randomized
protocol)` instead of claiming a median it could not take. Its `--by auto` will
emit per-current figures for the anchors and structured blocks, and the
randomized blocks draw ~200 distinct conditions, so expect a dense figure there
rather than a clean ordinal ramp — the envelope charts and `cycles.csv` are the
better read for n1/n2/n4.

## If something goes wrong overnight

Read `data/raw/queue_<stamp>/queue_manifest.json` first — per profile it records
the sweep folder, capture count vs conditions commanded, status, and the reason
the sweep printed. Then the per-profile logs in the same folder. Every capture is
written to disk *before* it is analysed, so a mid-profile abort never loses the
pulses already collected.

## Committing the results

Module convention is that `data/` is tracked so results travel with a clone, but
this campaign is ~19 GB against a 2.6 GB `.git` and 3.8 GB of existing
`data/raw/`. **Decide before `git add`** — committing it as-is roughly
sextuples the repository. Options: commit `cycles.csv` + reports + the merged
derived table and keep the raw CSVs local; or commit raw for the trainable
blocks only. This is a deliberate exception to the "always commit captures"
rule, not an oversight.
