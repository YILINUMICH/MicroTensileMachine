# Firmware_SMASensorHub_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **To-Test** — code-complete and reviewed; **not yet bench-verified** on hardware. This is the Phase-6 merge of `Firmware_SensorHub_PIO` (sensing) + `Firmware_SMADriver_PIO` (SMA drive). Carries the **~1 kHz SMA stream** ported from `Firmware_SMARateTest_PIO` on 2026-07-13 (builds clean, **needs a bench run** — see the gates below). Flips to **Stable** after a combined bench run (dual-ADC stream + an SMA `drive`/`fire` with no dropped samples and `[SMA]`/`[STATUS]`/sensor lines cleanly separable). |
| **Role** | Combined production firmware: M4 dual-ADC sensing **and** M7 SMA drive-path control on one Portenta H7, sharing the USB-CDC port. |
| **Supersedes** | Will supersede running `Firmware_SensorHub_PIO` + `Firmware_SMADriver_PIO` as two separate flashes. Both kept as single-purpose reference builds. |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7 -t upload`, then `pio run -e portenta_m4 -t upload`, power-cycle the rig (USB + EVM supply), `pio device monitor` @ 115200. Expect: M4 boot checkpoints, `ID=0x23`, untagged dual stream (`src=1` laser, `src=2` load), `[STATUS]` once/sec, and `[SMA] MCP4728 OK ...`. Then try `read`, `set 2.0`, `drive 1.0 500` — sensor stream must keep flowing during the drive. |

## What changed vs. the two source modules

- **M4 is unchanged** from `Firmware_SensorHub_PIO` (pure sensing → SRAM4 ring). Verified byte-faithful in review.
- **M7 gained the SMA controller**, restructured from blocking commands into a **non-blocking state machine** so the sensor ring keeps draining during `drive`/`fire`/`step`/`sweep`. An `abort` command can interrupt a live op.
- **Shared serial, three line classes:** untagged sensor TSV, `[STATUS]` telemetry, `[SMA] `-tagged driver I/O (the host sensor parser drops `[`-containing lines).
- **Missing MCP4728 is non-fatal** — the sensor bridge runs even if the SMA board is absent (`sma_ok=false`).
- **INA296A current sense (A1)** is enabled, primed, read, and exposed (`read`/`info`/drive feedback; `gain`/`shunt`/`ioffset` tunable).
- **Cyclic actuation state machine on M7** (`cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>`): autonomous heat/cool profile timed entirely by M7's `millis()` — deterministic, host out of the timing loop. `ping` heartbeat + `wdt` watchdog safe-stops if the host goes silent; `stop`/`abort` end it. The PC only sends parameters + heartbeat.

## Review changes (2026-06-23)

Code-review pass over M4 + M7. Applied:

- **ADS1263 driver (all 4 live copies):** `POWER` `0x13` → **`0x02`** (external ref → INTREF off, VBIAS on, reset-flag cleared); per-read checksum-mismatch log **throttled to 1 Hz** (was unthrottled over RPC → could stall M4); stale channel-map header comment corrected.
- **M4 loss visibility:** fixed `m4_loops_per_s` (was reporting cumulative as a rate); checksum-invalid reads now counted (`crc_err`) and DRDY overruns published (`overrun`), both in `[STATUS]` — so `dropped=0` now means zero data loss.
- **Clock alignment:** M4 publishes a live clock; M7 stamps `src=3/4/5` with it so all stream lines share the M4 timeline. `[STATUS]` adds `m7_us`/`m4_us` for host verification.
- **LDO health:** `[STATUS]` adds `vdd`/`offset`/`aref` so the host can compute `V_pred` from the `src=3` DAC code and flag an abnormal LDO.
- **Cleanups:** `startDrive` warns on clamp; `static_assert` enforces both ADCs for the `src=3/4/5` format; redundant M4 init removed; corrected the state-machine blocking comment.

**SMA state-machine rebuild (consolidation).** `drive`/`fire`/`cycle` collapsed onto **one HEAT/COOL actuation engine** (`SMA_ACT_HEAT`/`SMA_ACT_COOL`); the old `SMA_DRIVING`/`SMA_FIRE_*`/`SMA_CYCLE_*` states and their per-op MOSFET/teardown code are gone. New model:

- **MOSFET = arm/disarm** (master enable). `arm` closes the return path; `disarm` is the immediate hard cutoff. All hardware writes go through `arm`/`disarm`/`setLevel` only.
- **Actuation = voltage modulation** between `v_high` (heat) and `V_IDLE` (cool/rest); the LDO never reaches 0 V, so MOSFET-off is the only true zero-current state.
- **Safety = the `wdt` heartbeat, generalized**: only while HEATing, no `ping` within `wdt_ms` → drop to **idle-low, still armed** (relaunch-able). `disarm`/`abort` = hard off.

**Breaking command changes** (operator + host `portenta_reader.py`/recorder): new `arm` / `disarm` / `idle <V>`; `drive`/`fire`/`cycle` now require `arm` first; **`fire` takes volts** now (`fire <v_high> [t_high_ms]`, was codes); `cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>` (renamed args); `mosfet on|off` kept as an arm/disarm alias.

Open: **compile pass** (`pio run`) — not built here; bench-verify the heat/cool/arm/wdt paths; optionally lighten `readADC`; propagate `crc_err`/`overrun` to `Firmware_SensorHub_PIO`; `V_IDLE` default (0.5 V) to confirm against the measured LDO idle.

## SMA stream-rate fix (2026-07-09)

**`pumpSensors()` batched write.** The M7 bridge printed each sensor line with
~6 `Serial.print()` calls; on the Portenta mbed core each small USB-CDC write
blocks ~1 ms (waits on the USB frame, not bandwidth — ~40 KB/s is <10 % of the
link). At ~64 lines/pass that gated the M7 loop, and the once-per-pass SMA
`src=3/4/5` stream inherited it → **~15 Hz, ~1.5 points per 100 ms fire.**

Fix: format the whole drain batch into one buffer and push it with a **single
`Serial.write()`** per pass (float formatted without printf-`%f`, not linked on
nano newlib). Also added an **RPC-newline guard** so the periodic `[ADC1]` PGA
alarm can't concatenate with the first sensor line. Measured result (in the
`Firmware_SMARateTest_PIO` fork, then ported here): **SMA stream 15 → 99 Hz,
~1.5 → ~9.9 points per fire**; `readSma()` unchanged (~2 ms — it was never the
bottleneck). `SENSOR_BATCH` is a build flag. Both cores build. **Bench-verify on
hardware; `CYCLE_LOG_MS` (10 ms) is now the ceiling and the next tuning knob.**
See `Firmware_SMARateTest_PIO/STATUS.md` for the full investigation.

## SMA stream to ~1 kHz (2026-07-13) — **BUILT, NOT YET BENCH-RUN**

Port of the winning rate-ladder env (`portenta_m7_rate1k_n4`) from
`Firmware_SMARateTest_PIO`, which reached **962 Hz with 96 points per 100 ms
fire** on the rig (runs 0–7, all clean). The 2026-07-09 fix above left
`CYCLE_LOG_MS` as the ceiling; this removes what was under it.

| what | was | now | why |
|---|---|---|---|
| `CYCLE_LOG_MS` | 10 ms | **1 ms** | the schedule ceiling; 1 ms is the floor `millis()` can express |
| `SMA_SETTLE_US` | `delay(1)` = 1000 µs | **50 µs** | ×2 per `readSma()` — this was **2 ms of pure delay** in every sample, the single biggest cost |
| in-cycle averaging | 64 | **4** (`ADC_SAMPLES_CYCLE`) | see below — this is the counterintuitive one |
| idle/manual averaging | 64 | **64** (`ADC_SAMPLES_IDLE`) | unchanged; precision reads stay precise |
| SMA emit | ~18 tiny `Serial.print()`s | **one `Serial.write()`** | the same bug 2026-07-09 fixed in `pumpSensors()`, never applied to the SMA path (~3 ms/sample) |
| SMA timestamp | M4's clock | **M7's `micros()`** (`SMA_STAMP_M7`) | M4's clock only ticks every ~2 ms, so at 1 kHz consecutive samples would share a timestamp |

**The key idea — and the one thing not to "fix" back.** Averaging moved *off* the
ADC and *onto* the host. Each sample is now built from 4 readings instead of 64,
so a single sample is noisier — but there are 10× more of them, and averaging on
the PC is free. The net is that the ADC performs **fewer conversions per second
than the old 100 Hz config** (9,620 vs 12,480) while reporting 10× the samples.

That matters because ADC **conversion duty sags the reference and inflates every
voltage read from it** (`V = 0.01508 × duty% + 2.988`, R² = 0.9996 across 8 bench
runs at a fixed DAC code). So raising `ADC_SAMPLES_CYCLE` to "improve precision"
would make **V and I read HIGH**. `R = V/I` is immune — both channels scale
together and the ratio cancels exactly, which is why this hid for so long (R sat
at 21.4 Ω while V drifted +33%). **Average in post, not on the ADC.**

Side finding, pre-existing and *not* introduced here: the old 100 Hz production
config runs at 14% duty and therefore reads **~+7% high on V and I (~15% on
power)**. `ADC_VREF_V = 3.145` is itself ~5% off (true ≈ 2.99 V). The new config
sits at 12% duty, so **1 kHz is slightly more accurate than 100 Hz was.** All
resistance results ever taken remain valid.

Host-side needs no change: the line format is byte-identical (verified against
`portenta_reader.parse_line`, including negatives and the fraction-carry edge),
`[STATUS]` parsing is a generic `key=value` regex, and the console drains the
whole H7 queue every 50 ms tick (~185 rows/tick at the new rate, queue 10 k).
Cross-stream alignment is on `host_timestamp_s`; `hw_us` is only ever used
*within* a channel, so the M4→M7 clock switch on src=3/4/5 is safe (and strictly
better — it finally has per-sample resolution).

New in `[STATUS]`: `loop_us_avg` / `loop_us_max` / `loop_hz` / `cycle_log_ms` /
`n_cycle`. `serviceSma()` streams at most **one sample per loop pass**, so
`loop_hz` is a **hard ceiling** on the SMA rate regardless of `CYCLE_LOG_MS` — if
the achieved rate ever plateaus below 1 kHz, check `loop_hz` first.

**Rollback:** `pio run -e portenta_m7_legacy100 -t upload` restores the 100 Hz
cadence and 64× averaging (keeping the batched write + M7 timestamp, which are
pure wins).

## Module TODOs

- [ ] **Bench-verify the 1 kHz SMA stream** (`portenta_m7`, flashed + power-cycled). Gates, in order — **stop at the first failure**:
  1. **Cadence** — `[STATUS] loop_hz` ≫ 1000, and src=3/4/5 arrive at ~1 kHz (~96 points inside a 100 ms fire, vs 8 before).
  2. **Stream health** — `dropped` and `crc_err` stay **0**, `hwm` < 50%. If the ring starts dropping, the SMA path is starving the sensor drain: you bought SMA rate with laser/load data, which is not a trade we want.
  3. **Accuracy — the silent one.** Compare the **V and I means** against an old 100 Hz session *at the same DAC code* (the `src=3` raw column carries `currentCode`, so you can confirm the drive is identical before blaming the measurement). Expect V/I to come out **~5% LOWER** than the old sessions — that is the duty error going away, not a regression. **Do not check R and call it a pass**: R = V/I is immune to exactly the failure this gate is looking for.
  4. **Idle telemetry** still streams at ~10 Hz with 64× averaging, and disarmed still streams nothing.
- [ ] **Bench-verify the combined image** end-to-end (dual stream + SMA `drive`/`fire`, zero drops, `[STATUS]` hwm < 50%).
- [ ] **Confirm `RPC.begin()` on M7 doesn't stall when M4 is flashed with `-D M4_IDLE`** (isolated SMA bring-up path). Low risk; untested.
- [x] ~~**SMA-feedback streaming**~~ — done 2026-06-21: SMA V/I/R emitted as untagged `src=3/4/5` sensor-TSV lines during `drive`/`fire` (M7 writes directly, no ring producer). **Host TODO:** extend `portenta_reader.py` to keep `src=3/4/5`.
- [ ] **Trim absolute-V accuracy** (`vdd`/`offset` against a meter) — inherited open item from `Firmware_SMADriver_PIO`.
- [ ] **Wire `Experiment_SMACharacterizationV2/` to this stream + send commands** over the same port (non-blocking reader already in place).

See [../README.md](../README.md) for the cross-cutting project map.
