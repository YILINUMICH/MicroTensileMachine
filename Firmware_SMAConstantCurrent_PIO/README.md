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

**Sense topology (corrected 2026-07-24).** The drive path is
`LDO out → 200 mΩ shunt → SMA_P → SMA → MOSFET → GND`. **A0 taps `SMA_P`**, so it
measures `V_sma` directly and `R_sma = V_sma / I` with no shunt correction; `V_ldo`
is derived for display only. A1 is the INA296A output at `10 V/V × 0.2 Ω = 2.0 V/A`.
The inherited code had A0 *before* the shunt and `R_shunt = 0.1 Ω` — both wrong.
See [STATUS.md](STATUS.md) for what that changed and what still needs bench proof.

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

---

## Bring-up log — 2026-07-27 (first time on hardware)

This project had never been flashed. It was, in one session, flashed, found
completely mute, debugged, and brought to a working closed-loop hold. Two of the
three bugs were **self-inflicted and self-concealing**, so the sequence is worth
recording: the wrong diagnosis was reached twice by reasoning from mechanism
instead of measuring, and each time the measurement contradicted it.

### Symptom 1 — the entire sample stream was dead

`info` and `read` replied normally; **no** `src=1..7` rows and **no** `[STATUS]`
frames ever appeared. It looked exactly like "the ADCs are muted."

**Suspected, in order:** M4 crashed; the ADS1263 not powered (the documented
`ID=0x00`-after-upload trap); the SRAM4 ring layout diverging from the parent.

**All wrong.** M4 was alive and converting on *both* ADCs the whole time — the
proof was the periodic `[ADC1] STATUS` line arriving via `DRV_LOG`→RPC→M7, and
STATUS bit 7 (ADC2-data-ready) toggling in the `0x49`/`0xC9` values. A
comments-only diff of `sample_ring.h` against the parent ruled out the ring.

**Actual cause:** commit d574b34 guarded `streamWrite()` and the `[STATUS]` frame
with `Serial.availableForWrite()`. mbed's `USBSerial` never overrides that
method, so it falls through to `cores/arduino/api/Print.h` — `virtual int
availableForWrite() { return 0; }` — and returns **0 unconditionally**. Both
guards were therefore always true and dropped 100% of both paths, while the
unguarded `Serial.print` command replies kept working. The bug also hid its own
evidence: `tx_drop` is published *inside* the `[STATUS]` frame it suppressed.

**Diagnostic that cracked it:** `Firmware_SMASensorHub_PIO` has zero occurrences
of `availableForWrite` / `tx_drop`. Diffing the fork against the parent localized
it in one grep. *When a fork misbehaves and the parent does not, diff them first.*

Fixed in **fd1478b** by gating on `connected()` instead.

### Symptom 2 — the M7 loop ran at ~479 Hz (the fix above caused it)

With the stream alive, `cccycle` showed `loop_us_avg = 2085 µs`. Suspicious
because CC uses `ADC_SAMPLES_CYCLE=4` — *less* averaging than
`Firmware_SMARateTest_PIO`, which sustains 957 Hz at n=16.

**Suspected, in order:** USB write latency (~1 ms per CDC write, from the Round-1
batching era); the per-tick MCP4728 I²C write; `setDACraw`'s `delay(2)`.

**All three refuted by measurement**, in the rate-test project so this one stayed
untouched (rungs 8a/8b, `dac_us` instrumentation):

| suspect | measured | verdict |
|---|---|---|
| `emit` (batched USB write) | 59 µs | not it — 3.5% of budget |
| per-tick DAC write @400 kHz | 365 µs | not it — still hit 1 kHz with it |
| per-tick DAC write @100 kHz | 364 µs | **identical** — not bus-bound either |
| `setDACraw` settle delay | n/a | CC's control path already passes `settle=false` |

**Actual cause:** `Serial.connected()` costs **~850 µs per call** on this core.
`fd1478b` called it once per `streamWrite`, and `pumpSensors()` writes roughly
once per pass, so it consumed **82–96% of all CPU time** (`conn_tot` 818–960 ms
of every second). The loop rate was literally `1 / connected()`.

Note the trap: hoisting the check to "once per loop pass" does **not** help — at
~1000 passes/s that is still ~1000 calls/s. It must be sampled on a **time**
basis. `hostUp()` caches it on a 250 ms timer: 4 calls/s ≈ 0.34% overhead,
with the same dead-host protection `fd1478b` was written for.

### Symptom 3 — cooling ran 3× slower than heating while doing less work

Cool phases sat at 479 Hz where heat phases reached 961–1501 Hz, despite cool
doing strictly less (no control math, no DAC write, 3 stream rows instead of 5).
Doing less work *and* taking longer is a scheduling bug, not a cost.

**Cause:** the voltage-mode branch of `serviceActuationPhase()` advanced
`cyc_next_log += CYCLE_LOG_MS` **without** the snap-forward guard the CC branch
has. Once a pass overran the 1 ms log period (which Symptom 2 guaranteed),
`cyc_next_log` fell permanently behind, `t_rel >= cyc_next_log` stayed true
forever, and the cool phase sampled on *every* pass. Confirmed by instrumenting
`log_fires` against `phase_passes`: **498 / 498** in cool, 38 / 942 in heat.

The guard's own comment on the CC branch describes this exact failure mode — it
was simply never backported to the `else` branch. The two bugs compounded:
Symptom 2 made passes exceed 1 ms, which triggered Symptom 3, which added a
second `connected()` call per pass, which halved the rate again.

### Measured before / after

| | before | after |
|---|---|---|
| loop, idle | 1,037 passes/s (990 µs) | **35,155 passes/s (20 µs)** |
| loop, in-cycle | 498 passes/s (2,068 µs) | **13,759–14,505 passes/s (63 µs)** |
| `connected()` time per second | 948 ms | **2 ms** |
| cool-phase `log_fires`/`phase_passes` | 498 / 498 (runaway) | **643 / 13,759 (scheduled)** |
| `src=3/4/5` capture rate | 317 Hz | **431 Hz** |

`src=7` (`cc_R_est`) went from never appearing to streaming — the runaway had
been starving the bootstrap.

### The CC loop itself was never the problem

Once the transport was fixed, `cc 200` converged to **200.8 / 201.2 mA** against
a 200 mA target (0.6%) by the third pulse, with `R_est` adapting 2.92 → 3.86 Ω
across cycles and persisting between them exactly as designed. `ccStep` measured
**593 µs**, against 585 µs predicted from the rate-test project's independent
`readSma` (220 µs) + `dac` (365 µs) — an 8 µs agreement.

### Still open after this session

- **`Serial.write()` now costs ~300 µs in-cycle** (11 µs idle) — real USB-CDC
  back-pressure at ~160 KB/s. This is the current limit on the SMA sample rate
  (~650 Hz against a 1000 Hz nominal), and the first time the transport has
  genuinely been the bottleneck. See STATUS for the UDP decision.
- **ADC2 checksum failures** — appeared while the LDO was powered, cleared on a
  reflash. Cause unknown; it silently zeroes the load-cell channel.
- **The LDO slope** — an open-circuit sweep measured
  `V_ldo = 5.187·(code/4095) + 0.5048` against the 4.7 V design span. The
  intercept matches the respin to 5 mV; the slope does not. Needs a DMM to say
  whether the error is in the LDO or the A0 sense chain, since both were measured
  through A0 and would agree with each other either way.
