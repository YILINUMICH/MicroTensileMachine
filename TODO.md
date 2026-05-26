# TODO — MicroTensileMachine

Cross-cutting and high-impact items only. Per-module TODOs live in each module's `STATUS.md`.

See [README.md](README.md) for the status legend used below.

---

## Major (blocks a working production rig)

- [x] **~~Run [`ADS1263_FirstPowerUp_PIO/`](ADS1263_FirstPowerUp_PIO/) — the first-power-up bring-up diagnostic.~~** ✅ Done 2026-05-24. All 11 checkpoints (cp0–cp10) PASS on the Mid Carrier + ADS1263 EVM. Pin defines (`PA_8/PC_6/PC_7`), REFMUX (`0x09`), VREF (5.0 V external REF7050), VBIAS-for-PGA, ADC2 path, DRDY interrupt on PC_6 — all verified. EVM AVDD measured at 5.2056 V (ratiometric, see cp10). The sketch is kept as a re-runnable diagnostic.
- [ ] **Port firmware from Hat Carrier to Mid Carrier (ASX00055).** Hardware has moved; firmware is partially ported.
    - [x] ~~`SensorHub_PIO/` — code-ported AND bench-verified on the Mid Carrier + bare TI EVM (2026-05-25).~~ Pin defines (`PA_8`/`PC_6`/`PC_7`), REFMUX (`0x09`), POWER (`0x13` for VBIAS), ADC1 on AIN2/3 + ADC2 on AIN4/5, REF7050 shared on AIN0/1, INTERFACE=0x05, RDATA2 6-byte frame, ADC2CFG correctly packed. Dual-stream output is clean and both channels track the bench multimeter. Two driver bugs were caught and fixed in the process — **RDATA2 frame layout** (was reading 5 bytes instead of 6 with the 00h zero-pad) and **ADC2CFG REF2/GAIN2 field swap** (silently ran ADC2 on internal 2.5 V ref @ 2× gain). See `SensorHub_PIO/STATUS.md` "Driver bugs caught" and `ADS1263/ADS1263_H7_Integration_Notes.md` §4 addenda for datasheet refs. Status flipped to To-Test; flips to Stable after `SMA_CharacterizationV2/` consumes the stream end-to-end.
    - [ ] `LaserHead_PIO/` — still on Hat Carrier pin defines, **and** still has the two ADC2 driver bugs that SensorHub_PIO just fixed (same driver lineage). When porting, lift the fixed `readRawData24()` and `writeADC2CFG()` from `SensorHub_PIO/lib/ADS1263/`.
    - [ ] `LoadCell_PIO/` — still on Hat Carrier pin defines. Doesn't touch ADC2, so the two ADC2 driver bugs don't bite — but the driver lineage is identical, so port the fixes anyway to keep the three drivers in sync.
    - The actual Mid Carrier ↔ ADS1263 EVM wiring is in [`doc/MEMO_cable_map.md`](doc/MEMO_cable_map.md) (J15 positions); cross-reference `doc/PortentaMidCarrier_ASX00055_Pinout.pdf` for the STM32 pin behind each J15 position, then cross-check the Hat-Carrier pin table in `ADS1263/ADS1263_H7_Integration_Notes.md` §2 to confirm which signals moved. Hat-Carrier values (for reference when porting the two remaining modules): CS was `PE_6`, DRDY was `PJ_11` (LoRa conflict), RESET was `PI_5`.
- [x] **~~Re-test ADC2/AIN2-AIN3 on the EVM.~~** ✅ Done 2026-05-24 via cp7 (AIN-pair scan) and cp8 (ADC2 enable+read). **The legacy AIN2/3 saturation issue does NOT reproduce on the bare EVM** — all four differential pairs (AIN2/3, 4/5, 6/7, 8/9) and four single-ended-vs-AINCOM configs PASS. ADC2 streams cleanly at 8.5 µV RMS (100 SPS Sinc3 gain=1, below datasheet typical). The legacy workaround routing the laser through ADC1/AIN0-AIN1 can be removed; `SensorHub_PIO` can run its intended dual-ADC production mode. Sensor AIN-pair assignment is now a free choice — pick during Phase 3 (see `doc/PLAN_phase3_sensors.md`).
- [ ] **Recalibrate the laser head on the EVM.** The current `k = -0.1171 mV/µm`, `V₀ = 566.957 mV` constants (in `SMA_CharacterizationV2/`) were derived through the Waveshare HAT's load-cell front-end, which applied ~4.4× input attenuation. The bare EVM doesn't have that front-end → those constants are invalid on the new hardware. Re-run `Calibrate_LaserHead/` after first power-up succeeds, update the defaults in `SMA_CharacterizationV2/` (and the laser_calibration_reference block in `session.py`).
- [ ] **Fill in the operator memos in `doc/`** (see `doc/README.md` — `MEMO_cable_map.md`, `MEMO_carrier_config.md`, `MEMO_sensor_setup.md`, `MEMO_bias_tee.md`, `MEMO_lcr_setup.md`). The rig is currently undocumented at the wiring level; this is the single biggest gap for someone else (or an AI agent) trying to reason about state.

## Important (improves reliability or experiment quality)

- [ ] **Bench-verify `SMA_CharacterizationV2/` after the LCR refactor.** The recorder now imports `LCRMeter` / `MeasurementConfig` / `MeasurementFunction` from `KeysightLCR/lcr_meter.py` via a `sys.path` shim in `workers.py`; the local `lcr_reader.py` has been deleted. Smoke-test as per `SMA_CharacterizationV2/STATUS.md` and flip its status back to Stable once verified.
- [x] **~~Reroute DRDY off `PJ_11` to a free GPIO.~~** ✅ Resolved 2026-05-24 by moving to Mid Carrier — DRDY now lives on **PC_6** (Mid Carrier J15-27), which is NOT shared with the LoRa IRQ. cp9 confirmed PC_6 supports falling-edge interrupts cleanly (4007/4000 expected edges in 10 s at 400 SPS). **Interrupt-driven DRDY reads are viable in production firmware** — `SensorHub_PIO`/`LoadCell_PIO`/`LaserHead_PIO` can switch back to edge-triggered when they're ported to the Mid Carrier.
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
