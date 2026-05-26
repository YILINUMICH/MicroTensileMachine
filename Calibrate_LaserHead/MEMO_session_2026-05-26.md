# MEMO — Laser-head recalibration session, 2026-05-26

Snapshot of where this work paused so the next session can resume cold.
Tracking issue: `../TODO.md` Major item "Recalibrate the laser head on
the EVM" + the `LaserHead_PIO/` bullet of the Mid Carrier port item.

## Decisions reached this session

- **Use LaserHead_PIO** (laser-only) for the calibration, not SensorHub_PIO.
  Rationale: SensorHub_PIO stays untouched as production dual-stream
  firmware; calibration-specific behaviour belongs in the laser-purpose
  sibling. Restores the original design intent of `Calibrate_LaserHead/`
  (its `run_calibration.py` metadata already targets LaserHead_PIO).
- **Signal path:** IL-030 → AIN4(+) / AIN5(−) → ADC2, 400 SPS, REF7050,
  PGA in path at gain=1. Matches SensorHub_PIO production exactly so the
  derived `k` / `V₀` carry over with no signal-chain translation.
- **Stage-to-sensor mapping (rig-specific, corrected late session):**
  - stage **5 mm** = IL-030 high end → **max** voltage reading
  - stage **10 mm** = IL-030 reference distance → mid voltage (sweep center)
  - stage **15 mm** = IL-030 low end → **min** voltage reading

  Voltage decreases as stage position increases → fit slope `k` is
  **negative** by construction. `analyze.py` sanity check now compares
  `|k|` (not `k`) to the 0.5 mV/µm nominal; the sign records geometry,
  not a fault. `config.yaml` `sweep_center_mm` accordingly set to 10.0
  (was 30.0 — that was the pre-rig spec value, not the actual mapping).
- **Sweep style:** `direction = bidirectional`, `passes = 2` →
  four traversals (fwd₁ rev₁ fwd₂ rev₂). Forward-vs-return for
  hysteresis, pass-1-vs-pass-2 for repeatability.
- **Cross-compare ENABLED** (scope expanded later in the session). Both
  ADCs sample AIN4/AIN5 simultaneously: ADC2 becomes the production
  k/V₀, ADC1 is the independent digital-path check. Two ADCs converging
  on the same fit is stronger evidence the digital path is clean than a
  single-channel measurement. What it catches: ADC2-specific driver
  bugs, ADC2 register-config errors. What it does NOT catch: REF7050
  voltage error (both ADCs share the same external reference), front-end
  wiring issues, IL-030 sensor problems, beam-axis cosine error.

## Done (Phase 1 — firmware port, 2026-05-26)

- Copied `SensorHub_PIO/lib/ADS1263/{ADS1263_Driver.h,.cpp}` over
  `LaserHead_PIO/lib/ADS1263/`. Inherits Mid Carrier pin defines
  (`PA_8/PC_6/PC_7`) and the two ADC2 driver bug fixes (RDATA2 6-byte
  frame, ADC2CFG `REF2`/`GAIN2` field order). Old driver backed up
  under `lib/ADS1263/.backup_pre_port_2026-05-26/`.
- Rewrote `LaserHead_PIO/src/main.cpp` top-of-file docstring (now
  describes dual-ADC cross-compare as the default); updated
  pin-checkpoint messages; bumped `ADC2_POLL_MS` 12→3 (400 SPS);
  updated ADC2 configure call (`ADC2MUX = 0x45`,
  `ref2 = ADS1263_ADC2_REF_AIN01`, `rate = ADS1263_ADC2_400SPS`).
- **Flipped `ENABLE_ADC1 = 0 → 1`** so both ADCs sample AIN4/AIN5
  simultaneously. ADC1's configure block uses `INPMUX = 0x45` and
  `pga_bypass = false` (PGA in path, gain=1) to mirror ADC2 as closely
  as possible.
- Refreshed `LaserHead_PIO/platformio.ini` header comments and added
  `[env] upload_port = COM8` / `monitor_port = COM8` pinning so PIO
  doesn't try to flash the Zaber on COM5 (matches the SensorHub_PIO
  pattern; see memory note `rig_com_port_assignment`).

## Done in this session — dedicated calibration PIO project

Created **`Calibrate_LaserHead/Calibrate_LaserHead_PIO/`** as a
self-contained PIO project for the dual-ADC cross-compare firmware.
Rationale: cross-compare is a calibration concern, not a production
one — so it lives next to the calibration scripts that consume it
rather than as a build flag on the production-purpose LaserHead_PIO.

Contents:
- `lib/ADS1263/ADS1263_Driver.{h,cpp}` — copied from
  `../../LaserHead_PIO/lib/ADS1263/` (Mid Carrier pin defines +
  RDATA2 6-byte frame fix + ADC2CFG REF2/GAIN2 field-order fix,
  originally lifted from SensorHub_PIO on 2026-05-25).
- `src/main.cpp` — dual-ADC on AIN4/AIN5 (ENABLE_ADC1 = ENABLE_ADC2 = 1).
  Calibration-flavored docstring + boot banner so the operator can
  confirm at flash time they have the right variant. Functionally
  mirrors the current LaserHead_PIO state.
- `platformio.ini` — own config with COM8 `upload_port`/`monitor_port`
  pinning (same VID:PID rationale as the SensorHub_PIO and updated
  LaserHead_PIO files).

LaserHead_PIO was NOT reverted — it stays at the dual-ADC default the
session reached, per user decision. The two PIO projects start out
functionally equivalent but can evolve independently (e.g. the
calibration project might later add operator-facing logs, more
checkpoints, or hold its ADC config still while LaserHead_PIO grows
production-only features).

`Calibrate_LaserHead/README.md` and `run_calibration.py` build_metadata
both updated to reference the new firmware location.

## Done in this session — cross-compare wiring

- `portenta_reader.py`:
  - Added `Sample.adc_source` field (None for single-channel formats,
    1 or 2 for 4-col dual-stream).
  - `parse_line` now accepts `adc_source=None` to disable filtering
    and return all samples with `adc_source` populated.
  - New `PortentaReader.read_samples_dual(n_per_adc)` method that
    captures ADC1 and ADC2 in parallel and returns
    `(samples_adc1, samples_adc2)`. Raises informatively if firmware
    is in 3-col single-channel mode (operator forgot to flash dual).
- `run_calibration.py`:
  - `SweepConfig.xcompare: bool = False` (default safe; config sets
    `true` for the production calibration).
  - `PointAggregate` carries optional `mean_V_adc1` / `std_V_adc1` /
    `n_samples_adc1` (None when not in xcompare).
  - New `aggregate_dual()` and `capture_point_dual()` helpers.
  - All three capture sites (baseline_pre, main sweep loop,
    baseline_post) routed through a single `_capture_aggregate_write`
    helper that branches on `cfg.xcompare`.
  - `raw.csv` gains an `adc_source` column; `points.csv` gains
    `mean_V_adc1` / `std_V_adc1` / `n_samples_adc1` columns. Both
    are empty for single-channel runs so legacy analyzers still load.
  - `build_metadata()` records `xcompare_mode` flag and (when on) an
    `xcompare_adc1` register-settings block.
- `analyze.py`:
  - `Point` carries the optional ADC1 columns with backward-compat
    parsing (`_try_float` / `_try_int`).
  - New `has_xcompare()`, `adc1_points()` helpers.
  - CLI report adds a "Cross-compare with ADC1" block when present:
    ADC2 vs ADC1 per-channel fits, Δk/ΔV0, `|Δk|/mean(k)` agreement
    percent, per-point ΔV bias + σ.
  - `--json-out` carries an `xcompare` sub-block with the ADC1 fit
    and agreement metrics.
- `config.yaml`: `xcompare: true` set as the default for production
  calibration runs.
- Verified behaviour via standalone test (`outputs/analyze_test.py`):
  legacy 2026-04-24 single-channel file loads cleanly with the old
  `k = -0.1171, V₀ = 566.957, R² = 0.965` reproduced; new xcompare
  synthetic file demuxes pass_index correctly and the agreement
  metric matches the 1% slope difference baked into the test data.

## Open — Phase 2 (host code changes, can be done without bench)

- **Flip `adc_source = 1 → 2`** in `run_calibration.py` line 313–314.
  Stale comment claims laser is on ADC1/AIN0-AIN1 via the HAT
  front-end; on the EVM with LaserHead_PIO it's on ADC2/AIN4-AIN5.
  Under the 3-col TSV stream this is parser-ignored, but defensive.
- **Refresh `build_metadata()`** in `run_calibration.py` line 222–232:
  - `firmware_path` → `LaserHead_PIO/src/main.cpp` (already correct,
    but verify against the port)
  - `inpmux_hex` field is misnamed for ADC2 → replace with
    `adc2mux_hex = "0x45"`
  - `nominal_sps` 100 → 400
  - Add `ref2 = "external REF7050 on AIN0/AIN1"`, `gain = 1`,
    `pga_bypass = false` (ADC2 PGA can't be bypassed; gain=1 buffer)
- **Add `passes: N` config knob** to `run_calibration.py` for the
  forward+return+repeat sweep. Wrap the existing sweep block in an
  outer loop driven by `cfg.passes` (default 1 preserves current
  behaviour). Add `pass_index` column to `points.csv` and `raw.csv`.
  Update `analyze.py` to fit per-pass and report a pass-to-pass
  repeatability metric alongside the main fit.
- **Update `Calibrate_LaserHead/README.md`** wiring section: replace
  "HAT screw terminal AIN0/AIN1" with "EVM AIN4(+) / AIN5(−)" and
  remove the "expected to FAIL the 0.5 mV/µm sanity check" caveat (we
  now bypass the old HAT attenuation, so the sanity check should land
  within ~5% of nominal).

## ⚠ Zaber safety_config.json blocks the new low-end position

`../ZaberStage/safety_config.json` currently has
`position_limits_mm: [10, 40]`. With the corrected sweep_center_mm=10
and sweep_range_mm=[-5, +5], the runner will command the stage to
absolute 5 mm on the first move — which is BELOW the lower limit
and will abort the run. Before the dry-run, widen the limits to
something like `[0, 40]` or at minimum `[5, 40]`. The JSON also has
`"device_type": "Mock"` (timestamp 2026-04-23) — if the real device
is now connected, regenerate safety_config.json from the connected
hardware so the device_info matches the actual Zaber + the limits
reflect the real travel envelope.

## Open — Phase 3 (bench session, requires hardware)

In order:

0. Widen `../ZaberStage/safety_config.json` `position_limits_mm` to
   permit [5, 15] mm at minimum (the new sweep range).
1. Flash `Calibrate_LaserHead_PIO`:
   `pio run -e portenta_m7_bridge -t upload`,
   then `pio run -e portenta_m4 -t upload`. Power-cycle USB + EVM
   supply after each upload.
2. Wire IL-030 analog out to **EVM AIN4(+)**, sensor signal ground to
   **AIN5(−)**, supply ground separately to **AVSS/GND**.
3. Mount the diffuse target on the Zaber carriage, align so the beam
   axis is parallel to the stage motion axis. Verify with a square
   against the existing fixture marks.
4. With stage at absolute **10 mm**, the IL-030 "reference distance"
   LED must be **lit**. Jog stage to 5 mm (expect max voltage / IL-030
   high end) and to 15 mm (expect min voltage / IL-030 low end) and
   confirm no saturation / no out-of-range indicator.
5. **30-minute thermal soak** with the laser on before any sampling
   (baseline drift between pre/post-sweep blocks is the only way to
   catch drift; a cold start guarantees you'll see some).
6. Smoke-test the stream:
   `python portenta_reader.py --port COM8 --duration 30`
   → expect ~400 SPS, monotonic timestamps, voltage tracks IL-030.
7. Dry run: `python run_calibration.py --dry-run` (10 points × 1 mm,
   ~1 min, validates full pipeline).
8. Full run: `python run_calibration.py`
   (51 points × 10 mm, `direction=bidirectional`, `passes=2`,
   ≈ 1.5–2 hours wall time at current `config.yaml` defaults).
9. Analyse: `python analyze.py data/<prefix>_points.csv`.
   Sanity checks (Plan §7):
   - **`|k|`** within ~5% of **0.5 mV/µm** nominal (sign is **negative**
     on this rig by construction — stage 5 mm = max V, stage 15 mm =
     min V — and the analyzer compares magnitudes)
   - `R²` > 0.9999 inside the linear window
   - residuals random, not S-shaped (S-shape → shrink `sweep_range_mm`)
   - per-pass spread < per-point σ
   - forward-vs-return hysteresis below IL-030 1 µm spec
   - baseline-pre ↔ baseline-post drift ≪ per-point σ
   - **xcompare:** |k_adc2 − k_adc1| / mean(|k|) ≪ 1% (clean digital
     path); per-point ΔV bias ≪ per-point σ (no systematic offset
     between channels)

## Open — Phase 4 (integration)

- Propagate new `k` / `V₀` into `SMA_CharacterizationV2/` defaults and
  the `laser_calibration_reference` block in `session.py`.
- Update `Calibrate_LaserHead/STATUS.md` "Last run" row and clear the
  "stale Waveshare HAT constants" warning.
- Mark `../TODO.md` Major items:
  - "Recalibrate the laser head on the EVM" → done
  - "`LaserHead_PIO/` — still on Hat Carrier pin defines…" → done
- Update memory: add a `laserhead_pio_evm_port` note capturing the
  driver-lineage decision (use SensorHub_PIO driver as source of
  truth for any future port).

## Known gotchas to remember

- **Sign of `k`.** The previous (HAT-path) run produced `k < 0`. On the
  EVM with no front-end inversion, sign depends purely on AIN4/AIN5
  polarity. Decide before wiring whether you want `+k` or `−k` as the
  canonical convention so `SMA_CharacterizationV2/` reads the intuitive
  sign.
- **REF7050 verification.** Cross-ADC cross-compare doesn't catch
  reference voltage errors (both ADCs share the same REF7050). REF was
  validated at 5.2056 V in `ADS1263_FirstPowerUp_PIO/` cp10 — if that
  result is older than a month, re-measure with the bench multimeter
  before trusting absolute voltages.
- **Cosine error.** Beam-axis ↔ stage-axis misalignment compresses
  apparent sensitivity by `cos(θ)` and the fit still looks clean
  (high R²). Worth a physical alignment check before the run.

## Files touched this session

- `LaserHead_PIO/lib/ADS1263/ADS1263_Driver.h` — replaced from
  SensorHub_PIO
- `LaserHead_PIO/lib/ADS1263/ADS1263_Driver.cpp` — replaced from
  SensorHub_PIO
- `LaserHead_PIO/lib/ADS1263/.backup_pre_port_2026-05-26/` — backup of
  the old driver, in case revert is needed
- `LaserHead_PIO/src/main.cpp` — edited (docstring + pin checkpoints +
  ADC2 config + 400 SPS poll + **`ENABLE_ADC1 = 1`** xcompare + ADC1
  configure block on AIN4/5)
- `LaserHead_PIO/platformio.ini` — edited (header docs, COM8 pinning)
- `Calibrate_LaserHead/Calibrate_LaserHead_PIO/` — **NEW** dedicated
  calibration firmware project:
    - `lib/ADS1263/ADS1263_Driver.{h,cpp}` — copied from
      `../../LaserHead_PIO/lib/ADS1263/`
    - `src/main.cpp` — dual-ADC, calibration-flavored docstring + banner
    - `platformio.ini` — own config with COM8 pinning
- `Calibrate_LaserHead/portenta_reader.py` — added
  `Sample.adc_source`, made parse filter optional, added
  `read_samples_dual`
- `Calibrate_LaserHead/run_calibration.py` — added `xcompare` config
  knob, `aggregate_dual`, `capture_point_dual`, dual-channel CSV
  columns, `_capture_aggregate_write` helper, xcompare metadata block,
  firmware_path now points at Calibrate_LaserHead_PIO when xcompare on
- `Calibrate_LaserHead/analyze.py` — added ADC1 column parsing,
  `has_xcompare`/`adc1_points`/agreement metric, cross-compare report
  in CLI + JSON output
- `Calibrate_LaserHead/config.yaml` — `direction: bidirectional`,
  `passes: 2`, `xcompare: true` set as production defaults
- `Calibrate_LaserHead/README.md` — wiring + firmware sections updated
  for EVM, firmware prerequisite now points at Calibrate_LaserHead_PIO
- `Calibrate_LaserHead/MEMO_session_2026-05-26.md` — this memo

## ⚠ Bash mount sync issue (environmental, not code-side)

During this session the Cowork sandbox's bash mount for the
`MicroTensileMachine/` folder fell behind the file-tool view —
`stat` and `wc -l` on `run_calibration.py` showed an 18:23:57 snapshot
(22563 bytes, 507 lines, truncated mid-statement), even though the
file-tool Read consistently returned the latest 684-line, fully
balanced version. The `outputs/` mount propagated file-tool writes
immediately, which is how analyze.py was end-to-end verified.

The user-side editor (VS Code etc.) reads from the actual Windows
filesystem, not the Linux bash mount, so it should see the file-tool's
view. If you open `run_calibration.py` after this session and it
appears truncated, the changes may not have persisted — in that case
revert from `.backup_pre_port_2026-05-26/` (driver only; the rest of
the host code did not exist in this state before this session and can
be recovered from git or this memo).

Quick verification step at session start: open `run_calibration.py`,
search for `xcompare`, expect to see the field in `SweepConfig`, the
`aggregate_dual` function, the `_capture_aggregate_write` helper, and
the `xcompare_adc1` metadata block. If any of those are absent, the
sync didn't take and the edits need to be re-applied.
