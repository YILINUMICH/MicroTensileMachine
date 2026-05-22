# TODO — MicroTensileMachine

Cross-cutting and high-impact items only. Per-module TODOs live in each module's `STATUS.md`.

See [README.md](README.md) for the status legend used below.

---

## Major (blocks a working production rig)

- [ ] **Run [`ADS1263_FirstPowerUp_PIO/`](ADS1263_FirstPowerUp_PIO/) — the first-power-up bring-up diagnostic.** Six checkpoints (Serial, GPIO, /RESET, SPI.begin, ADS1263 ID read, self-noise). M7-only, no external deps. **Must pass before any production firmware port is attempted.** Once it passes, the pin defines that worked in this sketch become the source of truth for porting `SensorHub_PIO` etc.
- [ ] **Port firmware from Hat Carrier to Mid Carrier (ASX00055).** Hardware has moved; firmware has not. Affects `SensorHub_PIO/`, `LaserHead_PIO/`, `LoadCell_PIO/`, and the pin defines in their shared `lib/ADS1263/ADS1263_Driver.h`. The actual Mid Carrier ↔ ADS1263 EVM wiring is in [`doc/MEMO_cable_map.md`](doc/MEMO_cable_map.md) (J15 positions); cross-reference `doc/PortentaMidCarrier_ASX00055_Pinout.pdf` for the STM32 pin behind each J15 position, then cross-check the Hat-Carrier pin table in `ADS1263/ADS1263_H7_Integration_Notes.md` §2 to confirm which signals moved. Likely-affected: CS (was `PE_6`), DRDY (was `PJ_11` / LoRa conflict — may not exist on Mid Carrier), RESET (was `PI_5`), SPI bus mapping.
- [ ] **Re-test ADC2/AIN2-AIN3 on the EVM.** On the legacy Waveshare HAT, AIN2/AIN3 saturated under any non-zero input — root cause never resolved, and the workaround was to route the laser through ADC1/AIN0-AIN1 (see `Calibrate_LaserHead/README.md` §Firmware prerequisite). The EVM has different input-stage circuitry and may not exhibit the same issue. Confirm during first power-up; if AIN2/AIN3 works cleanly on the EVM, the workaround can be removed and `SensorHub_PIO` can run its intended dual-ADC production mode.
- [ ] **Recalibrate the laser head on the EVM.** The current `k = -0.1171 mV/µm`, `V₀ = 566.957 mV` constants (in `SMA_CharacterizationV2/`) were derived through the Waveshare HAT's load-cell front-end, which applied ~4.4× input attenuation. The bare EVM doesn't have that front-end → those constants are invalid on the new hardware. Re-run `Calibrate_LaserHead/` after first power-up succeeds, update the defaults in `SMA_CharacterizationV2/` (and the laser_calibration_reference block in `session.py`).
- [ ] **Fill in the operator memos in `doc/`** (see `doc/README.md` — `MEMO_cable_map.md`, `MEMO_carrier_config.md`, `MEMO_sensor_setup.md`, `MEMO_bias_tee.md`, `MEMO_lcr_setup.md`). The rig is currently undocumented at the wiring level; this is the single biggest gap for someone else (or an AI agent) trying to reason about state.

## Important (improves reliability or experiment quality)

- [ ] **Bench-verify `SMA_CharacterizationV2/` after the LCR refactor.** The recorder now imports `LCRMeter` / `MeasurementConfig` / `MeasurementFunction` from `KeysightLCR/lcr_meter.py` via a `sys.path` shim in `workers.py`; the local `lcr_reader.py` has been deleted. Smoke-test as per `SMA_CharacterizationV2/STATUS.md` and flip its status back to Stable once verified.
- [ ] **Reroute DRDY off `PJ_11` to a free GPIO.** Currently the firmware falls back to timed polling because `PJ_11` is owned by the onboard LoRa IRQ. Hardware rework, then switch the M4 reader back to edge-triggered. Tracked in `LoadCell_PIO/README.md` §Next-steps and `LaserHead_PIO/README.md` §Next-steps. **Re-evaluate after the Mid Carrier port — the LoRa-on-`PJ_11` conflict may or may not still apply on the Mid Carrier's connector layout.**
- [ ] **Validate laser displacement linearity against physical reference.** `LaserHead_PIO/README.md` §Next-steps item 1: point the IL-030 at known distances across its full ±5 mm window and confirm the voltage tracks linearly. Currently only verified at a single bench position.
- [ ] **Investigate the 100 → ~42 SPS discrepancy in ADC2.** Configured rate doesn't match the observed output cadence (`LaserHead_PIO/README.md` §Status). Probably a filter-overhead or DR2 bit-field issue. Cross-check against the Waveshare reference library.
- [ ] **Re-record the LCR SHORT calibration any time the cable routing changes.** Per the Notion bias-tee writeup §4.2, the short drifts ~1% with mechanical disturbance. Bake this into the operator procedure.

## Future / nice-to-have

- [ ] **Shared-SRAM ring buffer in SRAM4** to replace the per-sample RPC transport between M4 and M7. Needed before pushing the sample rate above ~1 kSPS. Tracked in `LoadCell_PIO/README.md` §Next-steps item 2.
- [ ] **Layer Ethernet streaming on M7** so the sample stream isn't tied to a USB cable. Tracked in `LoadCell_PIO/README.md` §Next-steps item 5.
- [ ] **Retire `LoadCell_PIO/` and `LaserHead_PIO/`** once `SensorHub_PIO/` is bench-verified on the Mid Carrier. Move them to `Archieve/` then.

## Hygiene

- [ ] **Remove duplicate datasheets from `SensorHub_PIO/doc/`** once the firmware READMEs link back to `../../doc/` instead. (Tracked in `doc/README.md` §Conventions.)
- [ ] **Decide whether to consolidate the five operator memos into a single `RIG_MEMO.md`** or keep them separate. Currently they're five separate TODO files in `doc/` — pick one and stick to it.
- [ ] **Verify `Archieve/` (sic) folder name.** It's misspelled (should be `Archive/`). Renaming will break any `sys.path` shims that point at `Archieve/AD2/` — grep before renaming.
