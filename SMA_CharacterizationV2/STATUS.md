# SMA_CharacterizationV2 — STATUS

| Field | Value |
|---|---|
| **Status** | **To-Test** — recently refactored to consume `../KeysightLCR/` directly (the local `lcr_reader.py` was deleted). Needs a smoke recording on real hardware to confirm the LCR stream still arrives as expected. |
| **Role** | Single-session OPEN → SHORT → RAW state machine that records LCR (E4980AL) and laser (H7+ADS1263) streams concurrently into one timestamped output directory, with operator confirm/redo at each phase. |
| **Supersedes** | `SMA_Characterization/` (v1) |
| **Last verified** | Pre-refactor: ~2000 LCR / ~8000 H7 per 20 s phase. Post-refactor: NOT YET — see `## Module TODOs` below. |
| **Owner** | Yilin |
| **Quick test (post-refactor smoke)** | `python sma_recorder.py --session-id smoke` — press Enter at OPEN, wait 20 s, Enter to confirm, then Esc to abort. Verify `data/smoke/open_lcr.csv` and `open_h7.csv` have ~2000 / ~8000 rows. If LCR row count is zero or much smaller than expected, the refactor regressed something. |
| **Dependencies on other modules** | Reads `../KeysightLCR/lcr_meter.py` (canonical LCR driver) **and** `../Calibrate_LaserHead/portenta_reader.py` (H7 serial reader) via `sys.path` shims in `workers.py`. Uses calibration constants from `../Calibrate_LaserHead/data/2026-04-24_run07_*`: `k = -0.1171 mV/µm`, `V₀ = 566.957 mV`. |

## Module TODOs

- [ ] **Bench-verify the post-refactor LCR path.** Run the smoke test above; confirm `idn` is captured in `meta.json`, sample counts match pre-refactor baselines, and no new transient-error reconnect warnings appear in `session.log`. After this passes, flip **Status** above from `To-Test` back to `Stable`.
- [ ] **Re-run calibration** any time the laser signal path changes (ADC board swap, cable change, controller setting), and update the defaults accordingly. **Currently overdue** — the baked-in `k`/`V₀` came from the legacy Waveshare HAT; the rig is now on the bare TI ADS1263 EVM, so they won't be accurate until re-run. See [`../Calibrate_LaserHead/STATUS.md`](../Calibrate_LaserHead/STATUS.md).
- [ ] **Decide on `--deembed open_short` vs `short_only` policy** — `analyze_sma.py` defaults to `auto`. Document which one is preferred for the current rig in the README.
- [ ] **Sample drop monitoring** — `n_dropped > 0` in `meta.json` indicates the controller stalled. Add a louder warning at finalize so the operator notices.
- [ ] **Bake the per-experiment metadata (DUT identity, actuation profile) into `config.yaml` or a CLI flag** rather than relying on the free-text `notes` field.

See [../TODO.md](../TODO.md) for cross-cutting items.
