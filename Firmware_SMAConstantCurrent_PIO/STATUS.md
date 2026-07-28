# Firmware_SMAConstantCurrent_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP** — **first flashed and run on hardware 2026-07-27.** The closed-loop hold works: `cc 200` converged to 200.8/201.2 mA (0.6% of target) by the third pulse, with `R_est` adapting across cycles as designed. Getting there took three transport/scheduling fixes, two of them self-inflicted — see the **Bring-up log** in [README.md](README.md). Not yet Stable: the absolute current scale is still uncalibrated and steps 5–8 of the ladder below are unrun. |
| **Role** | Development fork of `Firmware_SMASensorHub_PIO` adding a **closed-loop constant-current** controller to the M7 SMA drive path. Everything else is carried over unchanged, so this image is a strict superset of the parent's behaviour. |
| **Supersedes** | Nothing. `Firmware_SMASensorHub_PIO/` remains the production / rollback image and must not be modified for CC work. |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7 -t upload`, power-cycle (USB + EVM supply), `pio device monitor` @ 115200. Expect the `[M7] Firmware_SMAConstantCurrent_PIO` banner and `[SMA] CC loop: 1000 Hz, tau=7.0 ms`. Then `arm` → `cc 200 2000` → watch `[SMA] [CC] start` and the `src=6/7` rows appear in the stream. |

## What this adds

- **`cc` / `ccfire` / `cccycle`** — current-mode twins of `drive` / `fire` / `cycle`.
  Same actuation engine, same arm/disarm, same heat watchdog, same `ping`/`stop`/`abort`.
- **`tau <ms>` / `ccgain <Kp>`** — the tuning knobs; `cc` (bare) prints controller state.
- **`src=6` (command `u`) and `src=7` (`R_est`)** on the sample stream — the adaptive
  state, logged so a controller problem can be told from a load problem offline.
- **Open-load fault** — command railed with no current ⇒ broken wire ⇒ auto-disarm.
  Voltage mode cannot detect this; a current loop actively ramps into it.

## Sense-path correction (2026-07-24) — changes recorded R

Schematic re-check found the A0 tap identified wrongly in the inherited code:

- **A0 is on `SMA_P`** (the SMA's high side, **after** the shunt), not the LDO
  output. So A0 measures `V_sma` directly and `R_sma = V_sma / I` — no shunt
  correction. `V_ldo` is now the *derived* quantity (`V_sma + I·R_shunt`) and is
  display-only. The old code subtracted the drop from an already-post-shunt
  reading, i.e. it double-counted it.
- **`R_SHUNT_OHM` 0.1 → 0.2** (200 mΩ, the part actually fitted). With
  `INA_GAIN = 10 V/V` the current scale is now **2.0 V/A**, not 1.0.
- **`src=3` now carries `V_sma`**, not `V_ldo`, so host-side `V/I` agrees with the
  firmware's own `src=5`.
- `readLDO()` → **`readSmaP()`**. The DAC-characterisation commands
  (`set`/`code`/`step`/`sweep`) still read this pin, so their `V_meas` is SMA_P
  and sits `I·R_shunt` under the `codeToVldo()` prediction while current flows —
  sweep **disarmed** for a clean DAC→LDO fit. Labels renamed to `V_smap*` to say so.

The CC loop itself is untouched: `R_est` is command-domain (`u/I`, skeleton
pitfall 2), so it never depended on the A0 node. Only the *measured* V/R change.
Any `src=3` / `src=5` data captured with an earlier build is wrong on both counts
(2× current-scale error and a double-subtracted shunt drop) — re-take it.

## Bring-up ladder — stop at the first failure

1. **Boot** — banner + `[SMA] MCP4728 OK`. `info` shows the CC block with
   `R_est = -- (not bootstrapped)`.
2. **Open-load fault fires** — `arm`, then `cc 200` with **the SMA disconnected**.
   Expect the command to ramp to the ceiling and `[CC] FAULT: open load` +
   `DISARMED` within ~250 ms of railing. Verify this *before* connecting a wire:
   it is the safety net for every test after it.
3. **Bootstrap + hold** — connect the load, `arm`, `cc 200 2000`. Expect `R_est` to
   latch within the first ~100 ms and settle near the wire's DC resistance, and
   the measured current to sit on target.
4. **`cc_hz` in `[STATUS]`** ≈ 1000. Below that the loop is starved by the rest of
   the pass and `tau` no longer means what it says — check `loop_hz` first.
5. **Accuracy** — cross-check `read` against a DMM in series. Expect the measured
   current to read **~5–7 % HIGH** (the known `ADC_VREF_V` / ADC-duty error). For a
   *current* controller that is a systematic setpoint error, not just noise: fix
   the scale with `aref` / `gain` / `shunt` / `ioffset`, then re-run this step.
6. **Step response** — `cc 200 10000`, then `cc 800`, then `cc 200` in one capture
   (retarget is live). Check overshoot and settling from the `src=4` stream.
7. **Disturbance test** — actuate a real SMA (`cccycle`) and confirm the current
   holds while `src=7` `R_est` moves with the transformation. Per the skeleton,
   **tune `tau` here**, not on the step-from-zero: the step looks good for almost
   any `tau`.
8. **No regression** — `dropped` / `crc_err` stay 0 and `hwm` < 50 % with the CC
   loop running. The current loop must not be bought with laser/load samples.

## Module TODOs

- [x] ~~Steps 1–3 of the bring-up ladder~~ — done 2026-07-27. Boot, MCP4728,
      bootstrap and hold all pass; `cc 200` holds within 0.6%.
- [x] ~~Step 2, the open-load fault~~ — done 2026-07-27, and it **FAILED first
      time**: the guard could not fire because the in-cycle current sense has
      sd 12.6 mA against a 20 mA floor, so 16.8 % of samples read above the
      floor with the DUT disconnected and the reset-on-excursion accumulator
      never reached 250 ms. Fixed with a leaky accumulator; re-tested and it now
      disarms ~380 ms after railing. **This is why the ladder says verify it
      before connecting a wire — assuming it worked would have left current mode
      with no open-load protection at all.**
- [x] ~~Step 5, the accuracy check~~ — done 2026-07-27 with a Fluke 17B+ and a
      4.8 Ω dummy load. **The predicted 5–7 % current error is NOT there**: the
      scale measures correct to ~1 %. The code→LDO map is DMM-verified to 1 mV
      (`vdd 5.067`); the *design* figure of 4.7 V turned out to be the wrong
      number, not the hardware. See the Calibration log in
      [README.md](README.md), including the series-ammeter attempt that failed
      and why.
- [ ] **PERSIST the measured constants.** `vdd 5.067` and `ioffset 0.0167` are
      runtime only and were *demonstrably* lost to a reflash mid-session. Bake
      both into the source defaults. Re-send them after every upload until then —
      nothing on screen tells you they are missing.
- [ ] **Resolve the +1.3 % A0 bias** — `aref` (cancels in `R`, squares in power)
      vs the 10k/10k divider (biases `R` by 1.3 %). A nominal-value resistor
      cannot settle it; needs a tolerance-known reference or a working series
      ammeter. Low priority: ~1.3 % on `R`, ~2.6 % on power.
- [ ] **Decide on UDP.** `Serial.write()` now measures ~300 µs in-cycle (11 µs
      idle) — genuine USB-CDC back-pressure at ~160 KB/s, and the current cap on
      the SMA rate (~650 Hz vs the 1000 Hz nominal). This is the FIRST time the
      transport has actually been the bottleneck; earlier suspicions of it were
      wrong (see README bring-up log). Cheaper things to try first: merge the
      `pumpSensors` and `streamSma` writes into one per pass (halves the write
      count), and confirm 650 Hz is actually insufficient for the science. The
      `portenta_m7_udp` env exists and has never been flashed.
- [x] ~~Chase the ADC2 checksum failures~~ — **SOLVED 2026-07-27, and it was not
      a fault.** It was the documented EVM power-cycle requirement, skipped
      across ~15 uploads in one session. See the ADC2 section in
      [README.md](README.md). **Rule: `crc_err` climbing or `rate2=0` after a
      flash means power-cycle the EVM, not debug the driver.**
- [ ] Steps 4–8 of the bring-up ladder (`cc_hz`, accuracy vs DMM, step response,
      disturbance test, no-regression).
- [ ] Decide whether to keep the `DBG_LOOP_PROFILE` / `portenta_m7_prof`
      instrumentation. It found two bugs in one session; it is off by default and
      the normal build is behaviourally identical.
- [ ] **Confirm the 2026-07-24 sense-path correction on the bench** — with a known
      resistor as the load, check `read`'s `V_sma`/`I`/`R` against a DMM. This is
      the change most likely to be still wrong, since it came from the schematic
      rather than a measurement.
- [ ] **Fix the absolute current scale** (step 5). Until then CC targets are
      repeatable but not absolute — the loop faithfully holds a number that is
      itself ~5–7 % off.
- [ ] **Re-tune `tau`** once the TPS7A57's real settling time is known
      (`docs/PLAN_phase6_ldo_characterization.md`). The default 7 ms is the Uno's,
      and the plant — not the MCU — is what limits it.
- [ ] Teach the recorder/analysis about `cc_u` / `cc_r`
      (`Experiment_SMACharacterizationV3/config.yaml` `h7.channels`). The shared
      parser already names them.
- [ ] Decide whether CC graduates into `Firmware_SMASensorHub_PIO/` or stays a
      separate image, once it is bench-proven.
- [ ] Inherited from the parent, still open here: bench-verify the ~1 kHz sensor
      stream, and `RPC.begin()` behaviour with `-D M4_IDLE`.

See [README.md](README.md) for the design and the port notes, and
[../README.md](../README.md) for the cross-cutting project map.
