# PLAN — Phase 6: DAC→LDO Characterization (settling + ripple)

> **Status: Plan / To-Test.** Goal: quantify how *fast* and how *clean* the
> MCP4728 → TPS7A57 "DAC-margining" structure is, using the SDS2000X Plus scope
> as the time-domain instrument. Module under test: [`../SMA_Driver_PIO/`](../SMA_Driver_PIO/).

## 1. Why scope, not ADC

The two questions need different instruments (see brainstorm context):

| Question | Right instrument | Reason |
|---|---|---|
| **How fast does V_LDO settle after a DAC step?** | **Scope (this plan)** | The on-chip ADC at 64-sample averaging is too slow/coarse to resolve the rising edge + overshoot. Scope sees the full transient. |
| **How clean is V_LDO (ripple / oscillation)?** | **Scope (this plan)** | mV-level AC ripple is invisible to a coarse DC readback; scope AC-couples and zooms. |
| **What current / SMA resistance during the step?** | **Scope C4 + firmware** | The INA296A (100 mΩ shunt, A1 10 V/V = 1 V/A) is now enabled. Scope C4 sees inrush; firmware reports steady I and R = V_sma/I. |
| How accurate is the code→voltage map? | ADC `sweep`/`csv` (already in firmware) | DC accuracy, not a scope job. **De-scoped** — SMA only needs ±0.1 V, the analytical model already meets that. Out of scope for this plan. |

> **Current sense (enabled).** After the LDO output a **100 mΩ shunt + INA296A
> (A1, 10 V/V)** gives a clean **1 V/A**. A0 taps *before* the shunt (= V_ldo), so
> firmware computes `V_sma = V_ldo − I·0.1` and `R_sma = V_sma / I`. This turns the
> loaded test into a real current/resistance measurement, not just a voltage one.

Priority order for this plan: **(1) settling time/speed, (2) ripple/noise.**
Load regulation is a secondary, optional add-on (Section 7).

The TPS7A57 datasheet (`tps7a57.pdf`) rates **1 % accuracy over line/load/temp**
and **2.45 µV_RMS** output noise (CNR/SS = 4.7 µF). Settling is dominated by the
**CNR/SS soft-start cap** — the firmware already notes a "~100 ms settle" floor.
This test measures the *actual* numbers on our board and confirms the rise is
clean soft-start, not ringing/instability.

## 2. What needs to be built (two pieces)

### 2.1 Firmware — add a hardware trigger GPIO + a `fire` command

The existing `step <code> [ms]` logs the transient over USB at 10 ms cadence —
fine for a sanity curve, useless for sub-ms edge timing. For the scope we need a
**hardware trigger edge emitted at the exact instant of the DAC write**, so the
scope can be armed in single-shot and catch the rising transient with proper
pre-trigger baseline.

Add to [`SMA_Driver_PIO/src/main.cpp`](../SMA_Driver_PIO/src/main.cpp):

- **`TRIG_PIN`** — a spare digital output, **`PJ_11`** = Mid Carrier silkscreen
  **PWM4** (J2-67), right next to the MOSFET's PWM3/PG_7. Driven with plain
  `digitalWrite()` as a GPIO, so PWM-timer mapping doesn't matter. Uses the explicit
  STM32 PinName (not a D-alias) because the `PWM_n` macros are unreliable on this
  core. Idle **LOW**.
- **`fire <code> [ms]`** command. Sequence (order matters):
  1. `digitalWrite(TRIG_PIN, HIGH)`  ← scope triggers on this rising edge = **t₀**
  2. *immediately* `setDACraw(code)`  (raw write, no `settleWait()`)
  3. hold `ms` (default ~500 ms — long enough to capture the full ~100 ms settle
     plus margin), optionally logging `readLDO()` over USB as a cross-check
  4. `setDACraw(0)` then `digitalWrite(TRIG_PIN, LOW)` to re-arm for the next shot
- Keep the trigger edge a few µs *before* the DAC write so there is clean
  pre-trigger baseline on the scope; the scope's horizontal delay shows the
  baseline left of t₀.
- `pinMode(TRIG_PIN, OUTPUT); digitalWrite(TRIG_PIN, LOW);` in `setup()`.

This is purely additive — `step`, `drive`, `sweep`, `code` stay as-is. Build/flash
per `SMA_Driver_PIO/README.md` (M7-only: `pio run -t upload`, then **power-cycle**).

### 2.2 Python — single-shot capture using the SiglentOscilloscope module

[`../SiglentOscillosope/oscilloscope.py`](../SiglentOscillosope/oscilloscope.py)
already has `capture_waveform(source)` (raw `WF? DAT2` → codes + preamble),
`codes_to_volts()`, and raw `write()`/`query()` passthrough. It does **not** yet
have single-shot trigger-arming helpers, so the capture script drives those over
raw SCPI:

```python
scope.write("TRSE EDGE,SR,C1,HT,OFF")   # edge trigger, source = C1 (the GPIO)
scope.write("C1:TRLV 1.0")              # ~1.0 V threshold (3.3 V logic edge)
scope.write("TRMD SINGL")               # single-shot
scope.write("ARM")                      # arm; now fire the firmware command
# ... send "fire <code> <ms>" to the H7 over its serial port (pyserial) ...
# poll INR? (bit0 = acquisition complete) until the shot is captured, then:
codes2, pre2 = scope.capture_waveform("C2")   # DAC node
codes3, pre3 = scope.capture_waveform("C3")   # LDO output
```

> Verify the exact `TRSE`/`TRMD`/`ARM`/`INR?` syntax against the SDS2000X Plus
> Programming Guide before trusting it — the module's existing SCPI (`PAVA?`,
> `WF? DAT2`, `BSWV`) is confirmed on this unit, but the trigger verbs above are
> not yet exercised in the module. Add them as helper methods (`arm_single()`,
> `wait_complete()`) once confirmed, mirroring the existing method style.

**One-socket rule:** the scope serves a single SCPI client on :5025. Close any
web-control page / second session first (per the module README).

## 3. Scope channel map

| Ch | Probe point | Purpose | Coupling / scale (start) |
|---|---|---|---|
| **C1** | `TRIG_PIN` (PJ_11 / PWM4) → GND | **Trigger** — rising edge = t₀ | DC, 1 V/div, edge trig @ ~1.0 V |
| **C2** | MCP4728 V_OUT (DAC node, into the 6.2 k) → GND | **DAC command** — see the DAC step itself | DC, 500 mV/div |
| **C3** | TPS7A57 V_OUT (LDO output) → GND | **LDO output** — the response under test | DC, 500 mV/div |
| **C4** | INA296A V_OUT → GND | **SMA current** — 1 V/A (A1 10 V/V × 0.1 Ω). Inrush + steady I | DC, 500 mV/div |

- All probe grounds to the **same** supply-return node (the bench supply's `-`
  feeding MCP4728 VDD and TPS7A57 V_IN). Avoid ground loops.
- Timebase: start ~**20 ms/div** (≈200 ms window) to frame the ~100 ms settle,
  with ~10–20 % pre-trigger. Zoom in to ~1 ms/div on the rising edge to inspect
  overshoot/slew; zoom the steady tail with AC coupling for ripple.
- Probes 10× to reduce loading on the high-impedance REF/DAC node; verify probe
  cal first.

## 4. Settling-time test (priority 1)

**Setup:** fixed power resistor as the load (chosen for repeatability — isolates
the LDO from SMA thermal drift). Size it to the SMA's nominal operating current
so the LDO sees a realistic load; pick wattage with margin (P = V²/R at the
highest test voltage). Enable the load MOSFET (`mosfet on`) so current actually
flows through the resistor during the step.

**Procedure (per voltage step):**
1. Park DAC at 0 (idle), trigger LOW, scope armed `TRMD SINGL`.
2. `fire <code> 500` — emits trigger edge, steps the DAC, holds 500 ms.
3. Capture C1/C2/C3 waveforms; save to CSV (reuse the module's logging pattern).
4. From C3 compute: **settling time** to ±1 % and ±0.1 % of final value,
   **overshoot** (%), **rise/slew** (V/ms), and the C2→C3 lag (DAC edge → LDO
   start of motion).

**Step matrix** (codes chosen to span the usable range; convert via
`V_LDO = V_OFFSET + (VDD/4095)·code`, V_OFFSET ≈ 0.31 V, VDD ≈ 5.5 V):

| Step | From → To (approx V) | Tests |
|---|---|---|
| Small up | 0.5 → 1.0 V | small-signal settle |
| Mid up | 0.5 → 2.5 V | typical SMA actuation step |
| Large up | 0.5 → 5.0 V | full-scale slew, worst-case soft-start |
| Large down | 5.0 → 0.5 V | discharge / fall behaviour (COUT + load) |

Repeat each step ≥3× to confirm the settle time is repeatable (single-shot, so
re-arm between shots).

**Expected:** a smooth soft-start ramp dominated by CNR/SS, on the order of the
firmware's ~100 ms note, **with no overshoot/ringing**. If you see ringing or a
settle that depends strongly on load, that's a stability finding worth a memo.

> Sanity context for SMA: Flexinol thermal time constants are hundreds of ms to
> seconds, so even a ~100 ms LDO settle is already faster than the coil responds.
> The point of this test is to *confirm* that and to catch instability — not to
> chase a faster number we don't need.

## 5. Ripple / noise test (priority 2)

**Setup:** same fixed-resistor load, MOSFET on, DAC held at a steady mid code
(e.g. ~2.5 V) — use `code <N>` or `set <V>`, not `fire` (no transient needed).

**Procedure:**
1. C3 on the LDO output, **AC coupling**, zoom vertical to ~5–10 mV/div.
2. Measure **PKPK** and **RMS/STDEV** on C3 with the module's `PAVA?` reads
   (`MeasureParam.PKPK`, `MeasureParam.STDEV`) — `read_burst()` for a stable stat.
3. Look for periodic ripple (switching pickup, oscillation) vs broadband noise.
   Check both unloaded and loaded — a load-dependent oscillation is the failure
   mode that would inject current noise into the LCR/impedance measurement.
4. Optionally FFT (scope Math) to see if any ripple sits at a specific frequency.

**Reference:** datasheet output noise is 2.45 µV_RMS (10 Hz–100 kHz) — we won't
resolve that with a scope; the goal here is to catch **mV-scale** ripple or any
oscillation, not to verify the µV noise floor. A clean trace within a few mV PKPK
is a pass.

## 6. Loaded vs unloaded comparison

Run Sections 4 and 5 **twice**: MOSFET **off** (no load current, `mosfet off`)
and MOSFET **on** through the fixed resistor (`mosfet on`). Compare settle time,
overshoot, and ripple between the two. This is the cheap way to see whether the
load changes the LDO's dynamic behaviour at all.

## 7. (Optional) Load regulation — secondary

Not a priority for now, but with current sense enabled it's nearly free: at a fixed
DAC code, read the **steady** V_LDO and **I** (firmware `read` now prints
V_LDO / I / V_sma / R, or scope C3 MEAN + C4 MEAN) with MOSFET off vs on, and record
the droop (ΔV) against the measured ΔI. Datasheet rates load regulation inside the
1 % accuracy envelope. Skip unless the loaded/unloaded comparison in Section 6 shows
something surprising.

## 7b. SMA resistance bonus

Because R = V_sma/I is now a firmware readout, the same `fire`/`drive` runs also
log SMA resistance vs time — useful later for correlating Joule-heating phase
transformation with the mechanical streams. Not the focus of this LDO plan, but it
comes for free with the current-sense path enabled.

## 8. Deliverables

- Updated `SMA_Driver_PIO/main.cpp` with `TRIG_PIN` + `fire` command, and a
  `STATUS.md` note (To-Test).
- A capture script under `SMA_Driver_PIO/` (or a small `LDO_Characterization/`
  module) using `SiglentOscillosope/oscilloscope.py` + `pyserial`.
- Per-step CSVs (C1/C2/C3 waveforms) + a short results memo: settle times,
  overshoot, ripple, loaded-vs-unloaded delta.

## 9. Open items / to verify before bench

1. **`PJ_11` / PWM4 trigger pin** — confirmed on the Mid Carrier pinout (J2-67,
   next to PWM3/PG_7 MOSFET). Just verify the scope sees a clean 3.3 V edge there.
2. **`TRSE`/`TRMD`/`ARM`/`INR?`** exact syntax on this SDS2000X Plus firmware —
   confirm against the Programming Guide, then fold into the scope module as
   `arm_single()` / `wait_complete()` helpers.
3. **`CODES_PER_DIV`** in the module is noted "verify against Programming Guide" —
   confirm before trusting absolute volts from `capture_waveform()`; for settle
   *time* the code values don't matter, but for overshoot % they do.
4. **Resistor value + wattage** sized to SMA nominal current at the max test
   voltage.
5. **INA296A zero-current offset.** Confirm OUT ≈ 0 V at 0 A (REF=GND). If there's
   a small bias, set it via the firmware `ioffset <V>` command. Confirm `gain 10`
   and `shunt 0.1` match the populated parts. Cross-check firmware `read` current
   against a DMM in series at one operating point.
6. **A1 pin** for the INA296A OUT lands on a free Mid Carrier analog pad (ANA1).
