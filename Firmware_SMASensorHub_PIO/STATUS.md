# Firmware_SMASensorHub_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **To-Test** — code-complete and reviewed; **not yet bench-verified** on hardware. This is the Phase-6 merge of `Firmware_SensorHub_PIO` (sensing) + `Firmware_SMADriver_PIO` (SMA drive). Flips to **Stable** after a combined bench run (dual-ADC stream + an SMA `drive`/`fire` with no dropped samples and `[SMA]`/`[STATUS]`/sensor lines cleanly separable). |
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

## Module TODOs

- [ ] **Bench-verify the combined image** end-to-end (dual stream + SMA `drive`/`fire`, zero drops, `[STATUS]` hwm < 50%).
- [ ] **Confirm `RPC.begin()` on M7 doesn't stall when M4 is flashed with `-D M4_IDLE`** (isolated SMA bring-up path). Low risk; untested.
- [x] ~~**SMA-feedback streaming**~~ — done 2026-06-21: SMA V/I/R emitted as untagged `src=3/4/5` sensor-TSV lines during `drive`/`fire` (M7 writes directly, no ring producer). **Host TODO:** extend `portenta_reader.py` to keep `src=3/4/5`.
- [ ] **Trim absolute-V accuracy** (`vdd`/`offset` against a meter) — inherited open item from `Firmware_SMADriver_PIO`.
- [ ] **Wire `Experiment_SMACharacterizationV2/` to this stream + send commands** over the same port (non-blocking reader already in place).

See [../README.md](../README.md) for the cross-cutting project map.
