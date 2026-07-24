# Firmware_SMAConstantCurrent_PIO

**Constant-current SMA drive on the Portenta H7.** A fork of
[`Firmware_SMASensorHub_PIO/`](../Firmware_SMASensorHub_PIO/) that adds a
closed-loop current controller to the M7 drive path. The parent project stays
the production image; this is where the current loop is developed.

Status: **To-Test** — builds clean, never flashed. See [STATUS.md](STATUS.md)
for the bring-up ladder.

---

## Why constant current

Driving an SMA at constant **voltage** means the current — and therefore the
Joule heating — drifts as the wire's resistance changes with temperature and
phase. Holding **current** makes the heat input repeatable run-to-run, and the
controller's live plant-resistance estimate is itself a free actuation sensor
(the resistance dip *is* the phase transformation).

The control law is a port of the design in
`GelBot/PIConstantCurrent/CONTROL_SKELETON.md`, validated on an Arduino Uno
driving **this same MCP4728 → TPS7A57 → INA296A driver board**. The algorithm
is transcribed rather than re-derived; §8 of that document is the pitfalls list,
and every gate in the code exists because its absence was a real bug there.

---

## What carried over, and what the H7 changed

The hardware was already in place — the H7 has driven this exact board in
voltage mode since Phase 6 — so this is a **controller port, not a bring-up**.
No wiring changes.

| | Uno build | This build |
|---|---|---|
| Control law (§3) | feedforward + auto-gain PI | **identical, transcribed** |
| Command → code | 33-point EEPROM cal table | **analytical model** (see 1 below) |
| Control rate | ~200 Hz, serial-bound, jittery | **1 kHz**, scheduled off `micros()`, true `dt` measured per tick |
| `tau` default | 7 ms | **7 ms — deliberately unchanged** (see 2) |
| Current sense | 10-bit ADC + averaging | 16-bit ADC, 4× oversample |
| Telemetry | CSV over 250 kbaud UART | the existing `src=` sample stream, +`src=6/7` |

**1 — No calibration table.** The Uno linearized the DAC→LDO path with a stored
sweep. This firmware already models it analytically
(`V_LDO = V_OFFSET + (VDD_MCP/4095)·code`, inverted by `vldoToCode()`), and that
map *is* this port's `voltage_to_code`. Any gain/offset error in it is harmless
**here specifically**, because `R_est` is measured in the **command domain**
(`u/I`) — a wrong map is absorbed into `R_est` and the feedforward stays
correct. This is exactly why skeleton pitfall 2 insists on `u/I` and not
`V_ldo/I`; switching to the latter reintroduces overshoot that no `tau` fixes.

**2 — `tau` was NOT reduced despite the 480 MHz core.** The skeleton suggests a
faster loop on the H7, but the speed limit here is the **plant**, not the MCU:
the TPS7A57's CNR/SS soft-start capacitor dominates the response. The Uno
already reached ~15 ms settle against this same LDO, so 7 ms is near the
actuator's limit. What the H7 actually buys is a **faster, jitter-free control
rate** — better disturbance rejection and a much cleaner `R_est` — not a faster
plant. Re-tune `tau` only against real LDO settling numbers
(`docs/PLAN_phase6_ldo_characterization.md`), and only on a **load-change**
test: a step-from-zero looks good for almost any `tau`.

**3 — Not a timer ISR.** Skeleton §9 suggests running the control step from a
hardware timer. It isn't, for a concrete reason: the loop's output stage is an
**I²C write**, and mbed's `Wire` is not safe to call from interrupt context. The
cooperative loop already sustains >1 kHz, and *measuring* `dt` instead of
assuming it removes the jitter the ISR was meant to solve. `[STATUS] cc_hz`
reports the achieved rate so this assumption is checkable, not just asserted.

### The accuracy caveat — read this before trusting a number

`ADC_VREF_V` (3.145 V) is **~5 % above the true ~2.99 V**, and ADC conversion
duty sags the reference further. Voltage-mode drive never cared, and `R = V/I`
cancels the error *exactly* — which is why it went unnoticed for so long. **A
current controller does not get that immunity.** The loop faithfully holds the
*measured* number at target, so `cc 500` regulates to a true current of roughly
470 mA.

Targets are therefore **repeatable but not absolute** until the scale is fixed:
check `read` against a DMM in series and set `aref` (and `gain` / `shunt` /
`ioffset` for the INA path). Fix the scale, not the loop.

For the same reason, do **not** raise `ADC_SAMPLES_CYCLE` to "improve
precision" — more conversions raise the duty, which makes V and I read *higher*.
Average on the host instead.

---

## Commands

Everything from the parent project still works. Current mode is additive, and
requires `arm` first exactly like voltage mode.

| Command | Action |
|---|---|
| `cc <mA> [ms]` | Hold a constant current. **Retargets live** if a run is already up — this is what makes step tests possible, and it keeps `R_est` (a property of the wire, not the setpoint). |
| `ccfire <mA> [ms]` | Single current-mode shot with the scope trigger edge (`PJ_11`). |
| `cccycle <i_high_mA> <i_low_mA> <t_high_ms> <t_idle_ms> <n>` | Current-mode actuation cycles; `n=0` = continuous. `i_low = 0` opens the loop and parks at `V_IDLE` during cool; a nonzero `i_low` keeps the loop closed so `R` stays observable through the cool phase. |
| `cc` | Print controller state (loop open/closed, target, `u`, `R_est`, `tau`, ticks). |
| `tau <ms>` | Closed-loop time constant. **The one knob.** |
| `ccgain <Kp>` | Proportional term; default 0 = pure integral trim. |
| `stop` / `abort` / `disarm` | `stop` → idle, still armed. `abort`/`disarm` → hard cutoff. All release the loop. |

Safety inherited unchanged: the MOSFET is the master enable, the `wdt`
heartbeat stops a run if the host goes silent, and `disarm` is always available.

**Added here:** an **open-load fault**. If the command rails at the ceiling while
essentially no current flows for 250 ms, the return path is broken (snapped wire,
loose clip) and the firmware disarms. Voltage mode just sits there when a wire
breaks; a current loop *ramps to the rail trying to force current through the
break*, so this is not optional. Test it deliberately, with the load
disconnected, before trusting any run — see STATUS step 2.

---

## Telemetry

The sample stream gains two rows, emitted only while the loop is closed, on the
same timestamp and in the same single write as the existing ones:

| src | Meaning | Unit | `raw` column |
|---|---|---|---|
| 3 | SMA drive voltage (measured) | V | DAC code |
| 4 | SMA current (measured) | A | 0 |
| 5 | SMA resistance, `V_sma/I` (measured) | Ω | 0 |
| **6** | **CC command `u`** (controller output) | **V** | **DAC code** |
| **7** | **CC `R_est`** (adaptive state, command-domain `u/I`) | **Ω** | **0** |

`src=5` and `src=7` are *not* duplicates: 5 is the measured `V_sma/I`, 7 is the
controller's filtered command-domain estimate. Divergence between them is a
diagnostic in itself. Skeleton §7 is emphatic about logging `R_est` — it is the
only way to tell a controller problem from a load problem after the fact.

`[STATUS]` gains `cc`, `cc_hz` (**achieved** control rate), `cc_i_tgt`, `cc_u`,
`cc_r`, `cc_tau_ms`.

**Host side:** `Calibrate_LaserHead/portenta_reader.py` now names these
`cc_u` / `cc_r`. That naming is required, not cosmetic — the recorder filters by
channel name, so an unnamed `src` is silently dropped. Add them to
`h7.channels` in the recorder config to log them.

---

## Build / flash

```
pio run -e portenta_m7 -t upload      # bridge + SMA + CC loop
pio run -e portenta_m4 -t upload      # dual-ADC sampler (identical to parent)
pio device monitor                    # 115200
```

**Power-cycle the rig (USB + EVM supply) after every upload** — the DFU reset
does not cleanly re-power the EVM's analog rails and the ADS1263 comes up with
`ID=0x00`. Day to day this is an **M7-only reflash**; all the CC work is on M7.

Envs: `portenta_m7` (1 kHz control) · `portenta_m7_cc200` (**5 ms control,
matching the Uno's rate — the A/B build for bring-up**, isolates "the port is
wrong" from "the rate is wrong") · `portenta_m7_legacy100` · `portenta_m7_udp` ·
`portenta_m4` · `portenta_m4_idle`.

> The ADS1263 driver in `lib/` is a **copy**, per the project-wide convention.
> Fix it here and you must propagate to every sibling project — the canonical
> copy lives in `Firmware_SMASensorHub_PIO/`.

---

## Relationship to the other firmware projects

| Project | Keep using it for |
|---|---|
| [`Firmware_SMASensorHub_PIO/`](../Firmware_SMASensorHub_PIO/) | **Production.** Voltage-mode actuation and every existing experiment. Do not develop CC there. |
| **this project** | Constant-current development and any CC experiment. |
| [`Firmware_SMARateTest_PIO/`](../Firmware_SMARateTest_PIO/) | Stream-rate diagnosis. |
| [`Firmware_stable/`](../Firmware_stable/) | Frozen pre-merge baseline for A/B. |

Whether CC eventually graduates into the production image or stays a separate
build is an open decision — see STATUS.
