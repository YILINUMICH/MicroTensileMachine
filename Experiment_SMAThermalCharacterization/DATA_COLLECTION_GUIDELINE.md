# Summary CSV Guideline for NN Self-Sensing Training Data

**Audience:** the acquisition/report side (MicroTensileMachine —
`operator_current_sweep.py` / `operator_sweep_report.py` / the
`make_heat_time_map_clean_*.py` merge scripts).
**Goal:** make each campaign's per-pulse summary table **self-sufficient for NN
training**, so the training repo never has to re-open the raw captures to
recover electrical channels (what `scripts/prepare_pulse_runs.py` currently
does for 2026-07-31, at real risk of window/label misalignment).

**Motivating failure:** the 2026-07-31 `summary_report.csv` dropped the
voltage columns that 2026-07-30 had (`baseline_V`, `peak_V`, `rise_V`).
Resistance and power — the network's ONLY inputs — had to be reconstructed
downstream by re-detecting heat windows in the raw current stream and joining
them to label rows by cycle index. That join is fixable but fragile; the fixes
below make it unnecessary.

---

## 1. Columns every per-pulse summary row must carry

Keep everything already in `heat_time_map_20260731_all.csv` (the flags are
valuable — see §4), and ADD the electrical hot-state columns:

| column        | unit | definition                                                        |
|---------------|------|-------------------------------------------------------------------|
| `v_hot_V`     | V    | median of `sma_v` over the pulse TAIL (see §2)                    |
| `i_hot_mA`    | mA   | median of `sma_i` over the pulse TAIL (same samples as `v_hot_V`) |
| `r_hot_ohm`   | Ohm  | `v_hot_V / (i_hot_mA/1000)` — hot-state resistance at pulse end   |
| `p_hot_W`     | W    | `v_hot_V * (i_hot_mA/1000)` — electrical power at pulse end       |
| `t_heat_start_s` | s | heat-window start on the UNWRAPPED M7 clock (see §3)              |
| `t_heat_end_s`   | s | heat-window end, same clock                                       |

Record BOTH the raw medians (`v_hot_V`, `i_hot_mA`) and the derived values
(`r_hot_ohm`, `p_hot_W`): the raw pair lets downstream re-derive and
cross-check; the derived pair is what training consumes.

Strongly recommended additions (cheap now, expensive to reconstruct later):

| column        | unit | definition                                                        |
|---------------|------|-------------------------------------------------------------------|
| `r_base_ohm`  | Ohm  | cold/baseline resistance: median `sma_v/sma_i` over the last ~1 s of the PRECEDING cool phase. Only valid when idle current flows (`i_low_mA = 100`); write empty/NaN when `i_low_mA = 0`. Restores the information `baseline_V`/`rise_V` carried on 2026-07-30. |
| `t_pulse_utc` | ISO-8601 | absolute wall-clock timestamp of heat onset. Removes the need to approximate the time axis as `cumsum(heat_ms + cool_s)` and makes cross-sweep ordering exact. |

## 2. How to compute the hot-state values (tail spec)

- Tail window: the **last 20 % of the heat window, but never less than 20 ms**
  (`tail = [t_end - max(0.02 s, 0.2*(t_end - t_start)), t_end]`).
- Use **medians**, not means, over the tail samples (immune to single-sample
  ADC glitches).
- Rationale for "tail": the wire is hottest at pulse end, and it matches the
  2026-07-30 `peak_V` convention, keeping campaigns comparable.
- Do NOT report the whole-window mean as the hot-state value: for ramped
  (bootstrap, pre-seed) pulses the mean sits far below the end state — this is
  exactly why the existing `i_mA` column (whole-window mean) disagrees with
  tail values by >15 % on ramp pulses. Keeping `i_mA` as-is alongside
  `i_hot_mA` is fine; they answer different questions.

## 3. Heat windows: derive from the COMMANDED schedule, not thresholding

This is the root fix for the `detect_ok = 0` problem. Threshold-detecting
pulses in the current trace fails two ways (both observed on 2026-07-31):

- 150/250 mA pulses sit inside the ~107 mA idle-bias noise band
  (p95 ≈ 153 mA, p99.9 ≈ 200 mA) → phantom or missed windows, 116 of 324
  rows unusable (`detect_ok = 0`).
- The firmware knows exactly when each heat pulse starts and ends (the
  cccycle schedule). Emit those times per cycle and thresholding becomes a
  cross-check instead of the source of truth.

If schedule-derived windows land, keep `detect_ok` as a *consistency* flag
(schedule vs. threshold agreement) rather than a data-loss flag.

## 4. Timestamps: unwrap the 32-bit counter before writing anything

`hw_us` is a 32-bit microsecond counter that wraps every 2^32 µs ≈ 4295 s.
Long sweeps DO straddle the wrap (the 2026-07-31 550 mA × 400 ms capture
does), and any time-ordered processing on the wrapped value silently corrupts
results. Rules:

- Any timestamp written to a summary/report file must be **unwrapped**
  (monotonic, 64-bit) — accumulate `+2^32` whenever the raw counter steps
  backward by more than 2^31.
- `get_cycle.py` currently does NOT unwrap and is wrong across a wrap — worth
  patching there too.
- Keep raw `hw_us` in the captures if you like, but never do arithmetic on it
  un-unwrapped.

## 5. Keep the existing philosophy and flags

These 2026-07-31 decisions are right — keep them:

- **Nothing is dropped at collection time.** Every cycle is written; quality
  flags (`bootstrap`, `clipped`, `railed`, `detect_ok`, `cc_pct`, `i_low_mA`,
  `seeded`) let the training pipeline decide what counts.
- `railed` computed from the endpoint-vs-rail check (the report column alone
  under-reports).
- `cc_pct` (achieved/commanded) for judging drive on achieved current.
- One row per pulse; `cool_s` recorded per row.

## 6. Calibration bias: record it, per run

`sma_v` and `sma_i` each carry a known ~+7 % conversion-duty bias (cancels in
R, scales P by ~+14 %). Constant scale factors are absorbed by training
normalization, but write the applicable calibration/scale factors into each
run's `meta.json` so future absolute-unit analysis is possible, and so a
calibration change between campaigns is detectable rather than silent.

## 7. Stability rules

- **Never remove a column** an earlier campaign carried (this guideline exists
  because `peak_V` vanished between 07-30 and 07-31). Add, don't replace;
  deprecate by documenting.
- Keep column names and units exactly as specified above; the training config
  maps them by name.
- Protocol changes that alter the physics (cool time, pre-load, wire spec)
  belong in `meta.json` and the campaign README — data with different
  protocols is trained separately, so it must be identifiable.

## 8. What the report should self-check before writing (fail loudly)

1. Windows per condition == commanded cycles (map 6 / probe 4, or whatever
   the schedule says).
2. `i_hot_mA` within ~10 % of commanded level for non-ramp, non-compliance
   pulses (else flag, don't drop).
3. Timestamps strictly monotonic after unwrap.
4. `r_hot_ohm` within a sane physical band (e.g. 1–30 Ω for this coil) —
   values outside indicate a broken join or sensor fault.

---

Once summaries carry these columns, `scripts/prepare_pulse_runs.py` in this
repo reads the label table directly (as it already does for 2026-07-30) and
its raw-capture join can be retired to a verification-only role.
