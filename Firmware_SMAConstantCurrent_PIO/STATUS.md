# Firmware_SMAConstantCurrent_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **To-Test** — all five envs build clean under `-Wall -Wextra` with no warnings from `src/`; **never flashed**. The control law is a transcription of a design validated on an Arduino Uno driving *this same driver board*, so the algorithm is not speculative — but nothing here has run on the H7. Flips to **WIP/Stable** after the bring-up ladder below. |
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

- [ ] Walk the bring-up ladder above (nothing here has been on hardware).
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
