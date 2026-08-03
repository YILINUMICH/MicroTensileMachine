# Current sweep, guards off — the first clean actuation curve (2026-07-29)

`cccycle <level> 100 100 12000 6`, six levels 150–650 mA, five judged cycles per
level (cycle 1 excluded as the CC bootstrap). All guards reporting only, nothing
halting. **This is the reference dataset** — the 2026-07-28 sweep it supersedes
was taken through a corrupted current sense.

## The result

| commanded | achieved | Δx peak [µm] | sd | ΔF peak [mN] |
|---|---|---|---|---|
| 150 mA | 156 mA | 22.8 | 8.9 | 2.19 |
| 250 mA | 250 mA | 50.2 | 7.1 | 3.17 |
| 350 mA | 346 mA | 83.3 | 1.7 | 4.26 |
| 450 mA | 466 mA | 166.7 | 32.2 | 7.23 |
| 550 mA | 542 mA | 251.6 | 13.3 | 9.36 |
| 650 mA | 640 mA | 367.7 | 4.3 | 12.90 |

**The CC loop tracks.** Every level within a few percent, `R_est` stable ~5 Ω.
Both channels are monotonic and superlinear in current. Mechanical lag to peak is
**128 ms**, consistent at 350 mA and above.

Force peaks at 213 mN against the 490 mN (50 gf) rating — **still no saturation
at 640 mA**, so the load cell does not set the ceiling.

## THE BUG THAT HID ALL OF THIS: M7 and M4 do not share a clock

`src=1` (laser) and `src=2` (load) are stamped by the **M4**. `src=3/4/6/7`
(SMA V/I, CC command, R_est) by the **M7**, which boots first and runs
**+2.193 s ahead** — stable to 1 ms across an 8-minute run, read from the
firmware's own STATUS line (`m7_us=` / `m4_us=`).

Plot them on one axis untreated and the sensors land 2.2 s early, so the
displacement appears to **peak before the current pulse that causes it**:

```
current pulses     : 17.31, 29.82, 42.32, 54.82, 67.32 s
displacement peaks : 15.25, 27.75, 40.25, 52.75, 65.26 s   <- 2.06 s EARLIER
```

Every per-pulse displacement and force number taken before this correction was
measuring the **decay tail of the previous pulse**. That is why the signs looked
random, the amplitudes looked like noise, and levels that were plainly actuating
kept reading SUB-THRESHOLD. Corrected, the same raw data gives the clean curve
above.

`lib_h7_session.m4_clock_offset_s()` recovers the offset from a saved console
log; `align_m4()` applies it. **Any analysis joining sensor and SMA channels must
use them.**

## Sample rates (measured on level_650mA)

| src | channel | rate | note |
|---|---|---|---|
| 1 | laser (M4, ADS1263) | 495 Hz | 19% duplicate rows — ADC converts at 400 SPS |
| 2 | load (M4, ADS1263) | 495 Hz | same |
| 3 | sma_v (M7, `analogRead`) | **954 Hz** | 0% duplicates |
| 4 | **sma_i** (M7, `analogRead`) | **954 Hz** | 0% duplicates |
| 6 | cc_u (M7) | 993 Hz | 43% duplicates (command often unchanged) |
| 7 | cc_R_est (M7) | 993 Hz | only emitted once `cc_R_valid` |

**H7 current sample rate = 954 Hz**, one sample per CC control tick, median
interval 1.003 ms, each averaging `ADC_SAMPLES_CYCLE = 4` ADC reads. That gives
**~95 points across a 100 ms heat pulse**. Effective sensor bandwidth is 400 Hz
(Nyquist 200 Hz) despite the 495 Hz stream — use `hw_us`, and dedupe on
`raw_code` if the duplicates matter.

## Mistakes made getting here — do not repeat

1. **Judged actuation on force, and left the laser out of the plots entirely.**
   The fixture is compliant: the coil moves rather than loading. Force changes
   1–3 mN (noise); displacement changes 20–370 µm. Cost: a whole sweep reported
   "SUB-THRESHOLD" at every level while the wire worked fine.
2. **Missed the M4/M7 clock offset for an entire session.** The tell was there
   early — a response peaking before its cause is impossible.
3. **Generalised a number measured in a broken condition to the hardware.**
   Published "144 mA per ADC read, 57× worse than the Uno, averaging cannot fix
   it" from a contaminated capture. Healthy figure is ~30 mA/read (12×), and
   averaging is fine.
4. **Asserted a cause without checking timestamps.** Committed "reseating the
   clips fixed it" when the fault had cleared 28 minutes *before* the clips were
   touched.
5. **Talked myself out of a correct fix.** `i_low=100` → reverted to 0 on
   plausible-but-wrong reasoning → back to 100 when the bench disproved it. With
   `i_low=0` the cool phase releases the loop, so `ccEngage` reseeds
   `cc_u_i = 0.5 V` and the bootstrap restarts every pulse: 250 mA never
   bootstrapped at all and sat at 118 mA. `i_low=100` is *electrically* the same
   "cool = code 0" state (it rails at `u_min`) but keeps the loop engaged.
6. **Set thresholds from contaminated data and let them halt working runs.**
   Both `--min-rise` and `--min-dx-um` were calibrated against bad numbers.
   Guards now report; `--stop-on-fail` opts back in.
7. **Rectified the noise.** Averaged `|dx|` instead of signed `dx`, which turns
   a 4.9 µm noise response into 23.3 µm. Real actuation is coherent in sign —
   use that as the discriminator, not magnitude.
8. **Metric measured drift, not the step.** Taking the largest excursion over a
   1.5 s window on a drifting baseline reports the drift. Use peak-vs-pre-pulse
   over a short window.

## Files

`level_*.csv` raw (all six channels, `hw_us`), `summary.csv` per-cycle,
`fig1_timeline.png`, `fig2_pulses.png`, `fig3_summary.png` (all clock-corrected).

## Next session

- **Push higher than 650 mA.** Nothing has saturated: force is at 43% of rating,
  displacement at 368 µm of a ~10 mm sensor range, and the loop tracks. Watch
  for the force baseline ratcheting *down* across a run, which appeared above
  ~750 mA on 2026-07-28 and may be wire or clamp damage.
- Set real thresholds now that healthy numbers exist.
- Fix `operator_pulse_capture.py --run-s`: it closed before the last pulse.
- Open, unexplained: the 2026-07-28 sense corruption. Ten hypotheses refuted,
  all operating-point variables; it tracked the session, not the condition.
