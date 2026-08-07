# The 13-profile collection — ~9.2 h, unattended or step by step

> **STATUS: NOT YET COLLECTED — three attempts, all ended by the rig.**
> 08-05: sense chain faulted in pre-flight, four aborts. 08-06: runners rebuilt
> (sequential default, shared `run_order.txt`). 08-07: fault localized to the
> driver-board/harness side (coil, clips and BOTH H7 boards exonerated by
> direct test); the payload was shown median-recoverable, so
> **`abort_on_bad_sense` is flipped to `false` in all 13 profiles** — the
> conditions, seeds, roles and order are untouched. The 03:31 launch then died
> in 15 min on a SECOND, unrelated fault (ADC2 death + USB-CDC wedge). Before
> attempt 4, work the **Restart checklist** in `STATUS.md` *2026-08-07 03:46* —
> and if using `run_night.ps1`, pass `-BetweenS 300` (port reopens ≤2 min after
> a close can wedge the H7's USB TX; only a power cycle recovers it). Dates in
> this file that read "today" mean **2026-08-05**, when the envelope was
> measured.

Built to `NN_SelfSensing_Baseline/DATA_COLLECTION_GUIDELINE.md` §9 (excitation
protocol). Regenerate with `python gen_night_profiles.py`; that script's header
documents how each §9 clause is satisfied.

## Two ways to run it

Sorted order (n0..n9) is *not* the run order — that is the role numbering from
the generator. The real order lives in **`run_order.txt`**, which both runners
read, so it cannot drift between them.

**Sequential, one profile per invocation (default — start here).** Each launch
fires a fixed 650 mA × 300 ms sense probe, grades it with
`operator_sense_check.py`, and **will not start the profile on a BAD verdict**.
Progress lives in `data/raw/campaigns/<key>/steps/ledger.json`, so the campaign
can span several sessions and picks up where it left off:

```
# from the module root
python profiles\night_profiles_20260805\run_step.py            # board: what has run, what is next
python profiles\night_profiles_20260805\run_step.py next --dry-run
python profiles\night_profiles_20260805\run_step.py next       # run the next step
```

A bare invocation never drives the rig. `next` is the word that fires pulses.
Step 3 (`n1a`) and step 8 (`n1b`) are ~2 h each — those two want a sitting, the
rest are 8–48 min.

**Unattended, all 13 back-to-back (~9.2 h).** Only worth it on a rig whose
sense chain is proven, because the queue *cannot* detect the failure that
wasted 2026-08-05 (see pre-flight 6):

```
.\profiles\night_profiles_20260805\run_night.ps1 -DryRun   # ALWAYS first — opens no port
.\profiles\night_profiles_20260805\run_night.ps1           # the real run
```

Passing the *folder* to `operator_profile_queue.py` still works and still
validates, but it runs the sorted order and gives up the three properties in
**Run order**.

Either way each profile is followed by `operator_sweep_report.py`, and a failed
profile is one lost profile rather than a lost night. The queue writes
`queue_<stamp>/queue_manifest.json`; the stepper writes `steps/ledger.json`,
which additionally carries both sense verdicts per step.

**What sequential mode changes about the data.** Nothing inside a capture: a
profile is self-contained (settle, conditions, cool times) and runs byte-identical
either way. What changes is *between* profiles — the anchors `n0/n3/n9` bracket
the whole sequence rather than one night, so power cycles, re-clipping and
ambient swings land between them. They still measure drift, over days instead of
hours, and the ledger timestamps are what keep that interpretable. Run the
anchors in their pre-registered positions and do not re-run them casually. The
per-step probe also puts two identical 650 mA pulses in front of every block,
including each anchor — that is uniform preconditioning, and it is closer to
equal-state-going-in than the night's 30 s inter-profile gap was.

| # | profile | cond | pulses | ~min | role (FROZEN) |
|---|---|---|---|---|---|
| 1 | n0_anchor_start | 2 | 12 | 8 | drift bracket — repeats at n3, n9 |
| 2 | n4a_shortcool_test | 110 | 110 | 30 | **TEST-ONLY** — 5–11 s cool, warm coil |
| 3 | n1a_shuffle_train | 200 | 200 | 126 | **TRAINABLE** — randomized envelope draws |
| 4 | n2a_shuffle_test | 75 | 75 | 48 | **TEST-ONLY** — unseen sequence |
| 5 | n6_repeats | 8 | 48 | 30 | **TEST-ONLY** — cross-session probe |
| 6 | n8_threshold | 6 | 36 | 23 | trainable — sub-envelope stroke floor |
| 7 | n3_anchor_mid | 2 | 12 | 8 | drift bracket |
| 8 | n1b_shuffle_train | 200 | 200 | 128 | **TRAINABLE** — half B, different seed |
| 9 | n2b_shuffle_test | 75 | 75 | 47 | **TEST-ONLY** — half B |
| 10 | n4b_shortcool_test | 110 | 110 | 30 | **TEST-ONLY** — half B |
| 11 | n7_ladder | 8 | 48 | 31 | trainable — 550–900 mA × 500 ms fill |
| 12 | n5_longcool | 6 | 24 | 29 | trainable — 60 s deep cooling tails |
| 13 | n9_anchor_end | 2 | 12 | 8 | drift bracket + morning health check |

**962 pulses, ~9.2 h** (804 captures — the shuffle blocks are one capture per
pulse). Every pulse in n1/n2/n4 is a history-diverse sample.

## Run order

The filename numbering is the *role* numbering from `gen_night_profiles.py`;
the column above is the order to actually run, and it lives in
**`run_order.txt`** — one file, read by `run_night.ps1` and `run_step.py` alike.
Both refuse to start unless every `n*.json` on disk appears there exactly once,
so a profile added by a later `gen_night_profiles.py` run cannot be silently
dropped. Sequential mode does not change the order: the reasons below are about
what the coil has just been through, and they hold whether the gap between two
blocks is 30 seconds or a day.

**n0 + n4a are the attended head — ~40 min, then you can leave.** n4a is the
riskiest block in the campaign: 5–11 s cool is disjoint from every cool time
this rig has run before, and an incompletely recovered coil ratchets its
baseline instead of actuating. Running it first means the one block whose
failure mode is unproven happens while someone is watching, and it is only
30 min of the 9.2 h. `operator_sweep_report.py` runs automatically ~1 min
after n4a finishes — read it before walking away:

- **`!! base-jump: laser baseline moved`** on a growing fraction of conditions
  is the ratcheting failure. A few is normal at short cool; a monotone climb
  across all 110 is not.
- **`!! load-clip: force at the 5 V rail`** means the cold re-baseline (pre-
  flight 2) did not hold. n4a caps at 650 mA, so clipping *here* guarantees
  clipping all night in n1/n7.
- **dx should still scale with (I, t)** — collapsed stroke at high current
  means the coil never fully released.
- **`cc_pct` near 100 and `r_base` not climbing.** A drifting `r_base` with
  physically impossible values is the intermittent-clip signature, not a
  control problem — reseat and restart rather than letting it run.

If any of that looks wrong, Ctrl-C: the queue forwards it, the running sweep
disarms itself in its own `finally`, and n4a's captures are already on disk.
By then the queue will have started n1a — that is fine, it is interruptible
too.

The rest of the reorder buys three things:

1. **A failure at any hour leaves a usable night.** Sorted order puts both
   TRAINABLE halves (n1a + n1b, 254 min — 46 % of the night) before the first
   test block, so a rig that dies at 01:00 yields 400 training pulses and no
   test set, no OOD set, and no cross-session tie to the 07-31 / 08-05
   campaigns. Running one half of *each* pool first means the campaign is
   complete-in-miniature by ~01:50 and everything after n3 is a second helping.
   The A/B split already exists for exactly this reason — sorted order spends
   it on adjacent halves, which is the one arrangement that wastes it.
2. **n3 actually lands mid-night.** In sorted order it starts at 65 % of wall
   clock (357 of 552 min), so the "bracket" measures drift over 0–65–100
   instead of 0–50–100. Here it starts at 50.2 %.
3. **Both n3 and n9 are preceded by a low-energy block** (n8, then n5's 60 s
   cools) rather than by a randomized block that may have just fired 900 mA.
   Anchors are only comparable if the coil state going in is comparable, and
   the queue's 30 s inter-profile gap is not enough on its own. n0 gets this
   for free from the pre-flight.

A fourth, smaller effect: n4a and n4b no longer run back-to-back, so the B half
of the short-cool OOD set does not start on the heat left by the A half. With
n4a moved to the head the two halves now sit at opposite ends of the night —
n4a on a coil that has done 12 pulses, n4b after ~7 h of cycling. That is a
wider separation than the other pools get; it samples drift rather than
confounding it (the anchors measure the drift independently), but pool n4a and
n4b only after checking their anchors agree.

n7 sits second-to-last because it is the most redundant block in the campaign —
its 550–900 mA × 500 ms row is already covered by the attended 08-05 sweep, so
it is the right thing to lose to an overrun.

**On `--deadline`:** the queue skips *every* profile that would start after it,
and it iterates in order, so a deadline does not protect n9 — it deletes it,
along with whatever else is behind the clock. `run_night.ps1` therefore passes
no deadline by default. Nominal finish is start + 9.2 h + ~10 min of reports;
**start by 21:35 to be done before 07:00.** Pass `-Deadline 07:00` only if the
rig must be idle at a fixed time and you accept losing the tail.

## Before starting (10 min, do not skip)

> **2026-08-05: the first attempt at this campaign never started.** Four runs
> aborted on the sense guard between 21:05 and 21:51 and the night was deferred.
> Cause was a degraded SMA clip contact that **the pre-flight itself introduced**
> — step 4 below — and which none of steps 1–5 can detect, because they check
> baselines and levels, not noise. Step 6 is new and exists to close that gap.
> Full write-up in the module `STATUS.md`, *2026-08-05 night*.

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
4. **Reseat the SMA clips** — *only if they need it, and check afterwards.*
   Intermittent contact latches `R_est` wrong and CC overshoots up to 2×. But on
   2026-08-05 this step is what broke the rig: the reseat added ~0.3 Ω of
   contact resistance and tripled the sense noise, and three further reseats
   each left R **higher** (4.22 → 4.37 → 4.47 → 4.68 Ω). Disturbing a good
   contact is not free. Clips must bite clean bare metal and the sense leads
   must land on the wire, not the jaw.
5. **Disk: ~2.6 GB**, not the 19 GB an earlier draft of this file claimed. That
   figure multiplied 962 *pulses* by the size of a *condition* capture. A
   capture covers one condition, and the shuffle blocks' conditions are one
   ~30 s pulse, not six. Measured on `sweep_20260805_154528`: 18.4 MB for a
   ~200 s capture = **92 kB/s of CSV**, and the night records ~7.9 h of that.
   381 GB free.
6. **Verify the sense chain — the gate on the whole campaign.** `run_step.py`
   does this for you before every step and stops on a BAD verdict; run it by
   hand only before `run_night.ps1`, or after any work on the SMA wiring:

   ```powershell
   python operator_pulse_capture.py --ma 650 --heat-ms 300 --i-low 0 --cool-s 30 --cycles 2
   python operator_sense_check.py data\raw\pulse_<stamp>\h7.csv
   ```

   Wants **σ ≈ 25 mA** and **R @200 ms ≤ 2 %**, against the R transition of
   ~3 % the self-sensing model has to see. At 2× that floor the payload channel
   is buried, and the sweep's own guard starts straddling its 1 % limit — which
   means **profiles abort one or two conditions in while still writing captures,
   so the queue's circuit breaker never fires.** That failure mode runs the full
   9 h and yields ~20 captures of 804, reported as `ok` and `partial`. Do not
   start on a BAD or MARGINAL verdict; do not work around it by loosening
   `r_min_ohm`.

   **Name the capture file.** Three details make the obvious short form wrong:
   the flag is `--ma`, not `--i-ma`; a pulse folder holds `h7.csv`, not
   `cNN_level_*.csv`, so a bare `operator_sense_check.py` used to skip it
   silently and grade a *stale sweep* instead; and `--i-low 0` is what parks
   the wire at the idle bias the campaign runs at. (The check now searches both
   capture shapes, but pointing at the file is what makes it unambiguous.)

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

## When the campaign is complete — the standard figure set

Both runners already run `operator_sweep_report.py` after each profile. Then
register the campaign once — `run_step.py` files every capture under
`data/raw/campaigns/<key>/`, and its `steps/ledger.json` lists exactly which
sweep folder came from which pre-registered role, which is the `runs` list a
`CAMPAIGNS` entry needs:

```
cd analysis
python plot_drive_trajectory.py --all        # 4 channels x 2 time scales, per sweep
python analyze_raw.py                        # add the campaign's folders to CAMPAIGNS first
python plot_envelope.py ../data/derived/heat_time_map_<campaign>_all.csv
python make_index.py                          # refresh data/raw/INDEX.md
```

`plot_drive_trajectory.py` handles this campaign's one-pulse conditions: it uses
the single pulse and labels the figure `1 pulse per condition (randomized
protocol)` instead of claiming a median it could not take. Its `--by auto` will
emit per-current figures for the anchors and structured blocks, and the
randomized blocks draw ~200 distinct conditions, so expect a dense figure there
rather than a clean ordinal ramp — the envelope charts and `cycles.csv` are the
better read for n1/n2/n4.

## If something goes wrong

Sequential: `python profiles\night_profiles_20260805\run_step.py` — the board
shows every step's status and capture count, the reason the sweep printed, and
the sense-probe trend across the campaign (σ and R per step; **R climbing step
over step is the clip-contact signature**, +0.3 to +0.5 Ω on 2026-08-05). A step
that did not finish `ok` stays on the list, so `next` offers it again once the
cause is fixed — nothing to un-record by hand. Per-step logs sit in
`data/raw/campaigns/<key>/steps/`.

Unattended: read `data/raw/queue_<stamp>/queue_manifest.json` first — per profile
it records the sweep folder, capture count vs conditions commanded, status, and
the reason the sweep printed. Then the per-profile logs in the same folder.

Either way, every capture is written to disk *before* it is analysed, so a
mid-profile abort never loses the pulses already collected.

## Committing the results

Module convention is that `data/` is tracked so results travel with a clone.
At the corrected **~2.6 GB** (see pre-flight 5) this campaign is comparable to
the 3.8 GB of `data/raw/` already committed, so the convention holds and the
whole thing can be committed — it roughly doubles `data/raw/`, it does not
sextuple the repo as an earlier draft of this section assumed. Commit it in
per-profile chunks rather than one 2.6 GB commit.
