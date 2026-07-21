# Driver_SiglentOscilloscope — STATUS

| Field | Value |
|---|---|
| **Status** | **Stable** — bench-verified end-to-end 2026-06-15. |
| **Role** | Python wrapper around the Siglent SDS2000X Plus over a raw SCPI socket (port 5025, no VISA). Mirrors the `Driver_KeysightLCR/lcr_meter.py` API so it drops into the same worker / recorder architecture. PAVA? continuous reads, cross-channel MEAD? phase/skew, built-in AWG (BSWV), raw waveform capture (WF? DAT2). |
| **Used by** | Intended for the SRF / isolation sweep and a future `ScopeWorker` in `Experiment_SMACharacterizationV2/` (mirror of `LcrWorker`). Not yet wired into the recorder. |
| **Owner** | Yilin |
| **Quick test** | `python test_oscilloscope.py` (full suite) · `--quick` (connection + health) · `--bench` (read-rate). |
| **Connection** | Direct PC↔scope link-local cable. `auto_connect()` tries `DEFAULT_HOST` (169.254.111.100) then sweeps the link-local /24 for a Siglent answering `*IDN?`. Override with `SCOPE_IP` (exact host) or `SCOPE_SUBNET` (the /24 to scan); `SCOPE_PORT` for the port. |

## Verification (2026-06-15)

`test_oscilloscope.py` run against the live **SDS2204X Plus** (fw 5.4.1.5.2R2, discovered at 169.254.111.4) — **10/10 passing** after the fixes below. Verified: socket + `*IDN?`, health (SARA?/TDIV?/PAVA?), configuration, single + burst reads, PKPK/MEAN/FREQ params, cross-channel phase, AWG round-trip, error handling, context manager.

Fixes that landed during bring-up:

- **Auto-detect** — the scope cannot hold a static IP in the link-local `169.254/16` range, so it self-assigns an address that moves. `auto_connect()` now falls back to a fast concurrent /24 scan (`_scan_for_scope`) instead of failing on a fixed IP.
- **Cross-channel phase** — `PHA`/`SKEW` now use the `MEAD?` (MEASURE_DELAY) verb, not `PAVA?` (which gets no reply and times out). `_measure_query()` selects the verb per param/source.
- **Secondary-read isolation** — a failing/timed-out secondary query no longer discards a good primary reading.
- **Single SCPI socket** — the scope serves only one client on :5025 at a time (a 2nd concurrent connection's `*IDN?` times out); the context-manager test closes the main session first.

## Module TODOs

- [ ] **Wire a `ScopeWorker` into `Experiment_SMACharacterizationV2/`** mirroring `LcrWorker` (one socket, phase-oblivious daemon thread). Respect the single-socket limit — the worker must be the sole holder of the scope socket.
- [ ] **Investigate burst read rate** (~2.7 rd/s / ~370 ms per PAVA? read observed on 2026-06-15) — confirm whether it's scope measurement latency or a per-query round-trip cost, and whether it's adequate for the intended sweep cadence.
- [x] **`CODES_PER_DIV` MEASURED = 30.0, not 25.0** (2026-07-21, `Experiment_RNoise`). A 1M-point record gave `mean_code = 10.61` at 0.5 V/div while the scope's own `PAVA? MEAN` read 0.1769 V → `10.61 × 0.5 / 0.1769 = 29.99`. **The module default of 25.0 reads 20% high.** Callers that need absolute volts should pass `codes_per_div=30.0` to `codes_to_volts()`; the default is left at 25.0 pending a decision on whether to change it repo-wide (it would shift every existing consumer). Affects `Experiment_LDOCharacterization/` overshoot %.
- [ ] **Decide whether to change the `CODES_PER_DIV` default to 30.0** now that it is measured — it changes results for every existing caller, so it is a deliberate migration, not a drive-by edit.
- [ ] **Known SCPI quirks worth folding into the driver** (all bench-confirmed 2026-07-21, currently worked around in `Experiment_RNoise/capture_phase2.py`): back-to-back writes get dropped (space out + read back + retry); `MSIZ` is silently ignored while the acquisition is STOPPED and accepts only decade values (10K/100K/1M/10M/100M); bandwidth limit has asymmetric syntax — set with `BWL C1,OFF`, read with `C1:BWL?` (the per-channel `C1:BWL OFF` form is ignored); `SAST?` replies headered (`SAST Stop`, not `Stop`).

See [../README.md](../README.md) for project overview.
