# Current sweep + CC-noise investigation — 2026-07-28

Two threads that turned out to be the same thread. The sweep was meant to find
the **maximum current that actuates the SMA without saturating the load cell**
(the upper bound for RNN training data). It found something else, and chasing
why led into the CC loop's current sense.

Run: `cccycle <level> 100 100 12000 6`, six levels 150–650 mA, five judged
cycles each (cycle 1 excluded as the CC bootstrap), 2 s lead-in baseline.

## 1. The load cell never saturates — the premise was wrong

| commanded | achieved | Δx (displacement) | ΔF (force) |
|---|---|---|---|
| 150 mA | 225 mA | +4.9 µm | −0.18 mN |
| 250 mA | 288 mA | −11.3 µm | −0.96 mN |
| 350 mA | 426 mA | −44.2 µm | −2.28 mN |
| 450 mA | 582 mA | −74.2 µm | −3.29 mN |
| 550 mA | 747 mA | −96.7 µm | −4.18 mN |
| 650 mA | 928 mA | **−530.2 µm** | −14.39 mN |

Peak force reached **380 mN transient / 232 mN settled against the 490 mN
(50 gf) rating** — 78% at worst, even at 928 mA. `console_20260715_193936_5V0.5V`
saturated because it drove 5 V *sustained*; a 100 ms pulse never transforms
enough of the wire to get there.

**Displacement is the actuation signal on this rig, not force.** The fixture is
compliant, so a contracting coil mostly *moves*. Force changes by 1–3 mN per
pulse — inside the noise — while displacement changes by tens to hundreds of µm
and is cleanly monotonic. The sweep's original pass/fail keyed on force rise,
which is why every level read "SUB-THRESHOLD" while the wire was plainly
actuating. **Any future threshold test must use src=1, not src=2.**

Laser calibration used: `k = −0.49780 mV/µm`, `V0 = 2503.75 mV`
(`Calibrate_LaserHead/calibration.json`, r² = 0.999998). No clipping in any
level; the 650 mA level spans 7.4 mm of a ~10 mm sensor range.

## 2. The wire never cools, so nothing actuates below ~650 mA

Measured on-time **102.2 ms** (commanded 100) and off-time **12.40 s**
(commanded 12.0) — repeatable to ±0.6 ms and <10 ms across all 30 pulses. Duty
1.02%. At that duty the pulse is thermally negligible:

```
650 mA level:  heat 3649 mW x 1.02%  =  37 mW average
               cool   97 mW x 99%    =  96 mW average
```

The **cool phase puts ~2.6x more energy into the wire than the pulses**. Force
baseline never returns between cycles; at 650 mA it jumps 180 → 380 mN on pulse
1 then ratchets *down* to 232 across the run (see `fig1_timeline.png`), which is
not repeatable cycling — suspect wire damage or a slipping clamp above ~750 mA.

Two causes: `i_low = 100 mA` is below the reachable floor (`u_min 0.5 V / R
4.2 Ω ≈ 120 mA`), and with `offset 0.5` an **armed** wire always carries ~120 mA
regardless of `i_low` — `i_low = 0` opens the loop but not the circuit. The
120 mA floor is deliberate (it keeps the shunt sense alive); the loop models it
correctly as `ccUMin() == vldoMin() == codeToVldo(0)`.

## 0. RESOLVED (23:36) — this session had an intermittent contact fault

**Read this before trusting anything below.** Re-running the identical
`cccycle 550/100` profile 100 minutes later gives a completely different rig:

| | cool sd | per read | kurtosis | >mean+100 mA | heat pulses vs 550 mA |
|---|---|---|---|---|---|
| this sweep, 21:56 | 71.53 mA | 143.1 mA | 4.57 | 12.91% | 769–817 mA (140–148%) |
| re-run, 23:36 | **14.96 mA** | 29.9 mA | **3.11** | **0.00%** | **540.8 / 540.1 mA (98.2%)** |

`../isense_20260728_233618_sma-connected-cccycle`.

**The CC loop is fine — it holds 550 mA to 1.8%.** The causal chain was:

> intermittent clip contact → multimodal current readings → the `R_est`
> bootstrap latches 6.27 Ω instead of ~4.6 → feedforward overshoots 46% → the
> ±12% `near` gate can never open → stuck for the whole run

This sweep's own **pulse 5 proves it**: `R_est` fell to 4.73 Ω and that pulse
landed at **102%**. The loop self-corrected the moment the readings cleaned up.

**What this invalidates in the data below:**
- **All noise numbers from this session — discard them.** The 71 mA sd, the
  multimodal distribution, `fig5_spikes.png`: rig fault, not firmware.
- **The actuation curve is suspect.** Achieved currents were measured through
  the same contaminated sense, and the wire saw an unstable contact, so the
  displacement-vs-current mapping needs re-running on the healthy rig.
- **The firmware needs no change to unblock work.** The `R_est`-bootstrap and
  `cccycle`-reachability items are still real robustness gaps — a single bad
  sample should not be able to strand the loop for a whole run — but they are
  *not* the cause and are not blocking.

**What still stands:** the timing measurements (§2), the duty-cycle energy
argument, the 120 mA armed floor, and — most importantly — **that displacement,
not force, is the actuation signal** (§1). Those do not depend on the current
sense being clean.

### The cause is NOT identified

An earlier version of this file blamed reseating the SMA clips. **The timeline
refutes that**: the fault was gone by the first isense capture at 02:44 UTC, and
the SMA was not unclipped until 03:12 — 28 minutes later. Corrected 2026-07-28.

Excluded by measurement, every one an *operating-point* variable:

| ruled out | by |
|---|---|
| per-tick I²C DAC write | 1.05× |
| DAC command jitter | the quiet capture has *more* |
| heat-pulse aftermath | flat across all 12 s |
| operating point / current level | `cc 155` clean at the sweep's current |
| **drive voltage / DAC code** | sweep gap 4 sat at code 5 (the 0.5 V floor, the intended cool state) and was still 68 mA sd, 13% impossible |
| load current | disconnected equally quiet |
| heat-vs-cool code path | `ccEngage`/`serviceActuationPhase` identical |
| A0→A1 mux leakage | 272 vs 157 mA, uncorrelated |
| clip reseating | fault gone 28 min before it |
| progressive degradation | uniform across all six levels from the first |
| dropped samples / torn ring reads | 0 missing on src=3/4 |
| board uptime | sweep 23–101 s vs re-run 13–54 s — overlapping, opposite results |

**The fault tracks the session, not the operating point.** Identical conditions
give 68 mA sd in one session and 15 mA in another. Whatever it is lives in rig
state that persists across serial opens — supply/power state, a connection, or
temperature. Note that opening the port resets the H7 but **not** the EVM analog
rails, so board-level state survives what the firmware clock shows.

The shunt is not the limit either: 0.2 Ω × gain 10 = 2 V/A, and at 16-bit over
3.145 V that is 24 µA/LSB, so 155 mA is ~6460 LSB. Healthy noise is ~625 LSB and
the faulty noise ~3000 LSB — both far above the resolution floor. No shunt or
gain change would have helped; something was injecting signal.

**Suspect this fault first** when the rig behaves oddly: today's ADC2 dropouts
and crc storms may share the cause. Symptom to look for: a current distribution
with modes that are *physically impossible* — here, 270 and 400 mA readings when
`u = 0.625 V` into 4.2 Ω caps the current at 149 mA. That check now runs
automatically: `measurement_sane()` in `lib_h7_session.py`, wired into the sweep,
aborts the run rather than record unusable data.

## 3. The CC loop overshoots because `R_est` bootstraps wrong

`R_est` latches at **6.25 Ω** against a true `u/I` of **4.2 Ω**, so the
feedforward `u = I·R_est` overshoots by exactly that ratio (1.54 measured vs
1.51 predicted at 150 mA). It never recovers, because both the `R_est` update
and the integral trim are gated on `near = |err| < 12%` — at 50% overshoot the
gate never opens, so the loop runs open-loop on a bad estimate that keeps the
error too large to fix itself.

`tau` is **not** the knob: it only sets `Ki = R_est/tau`, and that integral is
gated off. Root cause is the bootstrap latching `u/I` from a **single** ADC
sample on a railed point during the current's rise: `0.5 V / 0.08 A = 6.25 Ω`.

Same bug inflates the cool current: `u_ff = 6.25 × 0.100 = 0.625 V` sits *above*
the 0.5 V clamp, so the loop never rails and delivers 162 mA / 101 mW instead of
the intended 119 mA / 60 mW.

**`cccycle` also has no reachability check.** The "target outside the reachable
band" warning exists only in the `cc <mA>` path and is gated on `cc_R_valid`,
which `startCycleCC()` clears via `ccReset()` on the line before — so it is
structurally dead for every cycle run. We asked for an unreachable `i_low` and
heard nothing.

## 4. The current-sense noise — what it is, and what it is NOT

Companion captures: `../isense_20260728_2*`. All use voltage or CC mode with a
static/steady command, so anything that moves is measurement.

| condition | I mean | sd | per read |
|---|---|---|---|
| SMA disconnected, voltage | 0.4 mA | 12.10 mA | 24.2 mA |
| connected, voltage | 106 mA | 14.44 mA | 28.9 mA |
| connected, `cc 106` | 112 mA | 15.11 mA | 30.2 mA |
| connected, `cc 155` | 161 mA | 15.60 mA | 31.2 mA |
| **Uno, same control law + driver board** | 180 mA | **0.90 mA** | **2.55 mA** |

**The H7 front end is ~12x the Uno, not 57x.** An earlier figure of 144 mA/read
was taken from the sweep's cool phase and wrongly generalised to the hardware —
see below. The real steady-state figure is ~31 mA/read, flat in current and
identical in CC vs voltage mode.

That makes averaging a viable fix. Burst-averaging efficiency measured at 96%
(n=4 in-cycle 71.0 mA vs n=64 idle-hold 18.4 mA, ratio 3.86 against an ideal
4.00), and firmware reads are ~13 µs apart vs 1 ms for stream samples, so
in-firmware averaging is ~80x more rate-efficient than post-averaging:

```
n=4    15.6 mA sd   950 Hz    3sigma 47 mA   breaks the 12 mA gate
n=64    3.9 mA sd   383 Hz    3sigma 12 mA   fits, and 2x the Uno's rate
```

`operator_sweep_adcavg.py` measures this curve for real (it sweeps
`ADC_SAMPLES_CYCLE` via `PLATFORMIO_BUILD_FLAGS`, editing no firmware file).

### The sweep's cool phase is contaminated — UNEXPLAINED

Its current distribution is **multimodal** (discrete levels near 65/100/135/270/
400 mA, 22% of samples >220 mA, skew +1.37, kurtosis 4.50) — not Gaussian noise.
Its clean sub-population sits at 28 mA sd, close to the steady captures; the
extra modes inflate the apparent sd to 71 mA. See `fig5_spikes.png`: 250 ms of
raw current beside an equivalent clean capture at the same current.

**Hypotheses tested and REFUTED — do not re-run these:**

| hypothesis | test | result |
|---|---|---|
| per-tick I²C DAC write injects noise | CC vs voltage at matched current | 30.2 vs 28.9 mA/read — **1.05x** |
| DAC command jitter | tick-to-tick command activity | the *quiet* capture has more (66% vs 17% of ticks move) |
| heat-pulse aftermath | noise vs time since pulse | flat ~70 mA across all 12 s |
| operating point / current level | `cc 155` at the sweep's current | 15.6 mA — clean |
| load current | SMA disconnected | 12.1 mA — equally quiet |
| different code path in cool | read `ccEngage` / `serviceActuationPhase` | identical for heat and cool |
| A0→A1 mux not settling (leaked reads) | compare high samples against `V_sma/2` | 272 vs 157 mA, uncorrelated |

**Only untested difference left:** in the sweep's cool phase the target
(100 mA) is below the ~120 mA floor, so the loop rails and the `near` gate stays
shut; every clean capture had the gate open. Next test is one capture:
`operator_noise_isense.py --mode cc --ma 100`.

## Files

| file | what |
|---|---|
| `level_*.csv` | raw stream per level (src 1,2,3,4,6,7), `hw_us` time base |
| `summary.csv` | per-cycle achieved current, baseline/peak/rise, clip flag |
| `fig1_timeline.png` | whole run per level: current, displacement, force, resistance |
| `fig2_pulses.png` | every pulse aligned on its onset, baseline-subtracted |
| `fig3_summary.png` | commanded vs achieved; displacement and force vs current |
| `fig4_noise_vs_rate.png` | the noise/speed trade-off, host vs firmware averaging |
| `fig5_spikes.png` | the multimodal contamination, beside a clean capture |

## What to do next

1. Run the `--ma 100` capture to close the multimodal question.
2. Re-run the sweep judging on **displacement**, not force, and stop at 550 mA
   until the >750 mA force decay is understood.
3. Fix the `R_est` bootstrap (latch from a *settled* railed point, not one
   sample mid-rise) and move the reachability warning after `ccReset()`. Both
   are in `Firmware_SMAConstantCurrent_PIO` — **ask before editing.**
4. `ccgain 25` is a runtime-only workaround: with `Kp > 0` the proportional term
   pulls the error inside the gate, which lets `R_est` self-correct. Untested.
