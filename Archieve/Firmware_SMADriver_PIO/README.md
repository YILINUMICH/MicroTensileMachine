# Firmware_SMADriver_PIO

Phase 6 SMA drive-path bring-up firmware. Portenta H7 M7 talks I2C to an
externally-powered MCP4728 DAC. The DAC sets a **TPS7A5701 (TPS7A57)** LDO
via TI's **"DAC margining"** topology — the DAC drives the LDO's REF pin
through a 6.2 kΩ series resistor — and the LDO heats the SMA wire through a
MOSFET-gated load. The H7 reads the LDO output back through a 10k/10k
divider into an on-chip 16-bit ADC.

Standalone M7-only project. Once `set` / `drive` accuracy and linearity
are bench-verified, the M7 control code merges into `Firmware_SensorHub_PIO/` M7
alongside the dual-ADC sample stream.

**Status:** To-Test. Flashed and exercised on the bench — the **drive
path and its dynamic response (settling / overshoot / ripple) are
bench-verified** via [`../Experiment_LDOCharacterization/`](../Experiment_LDOCharacterization/)
(2026-06-17). **Absolute-voltage accuracy is deferred:** the `vdd`/`offset`
transfer-function trim against a meter, and the scope's `codes_per_div`
scaling, are still open. Merge of the M7 control code into
`Firmware_SensorHub_PIO/` is pending that trim. Treat absolute `set`/`drive`
voltages skeptically until then.

---

## Architecture

```
  External bench supply (≥ 5.5 V) powers all analog rails:
  MCP4728 VDD, TPS7A57 V_IN, and the I2C pull-up / level-shifter high side.
  The H7 sources only control + readback signals, never analog power.

  H7 SDA ──[LS]──┐
  H7 SCL ──[LS]──┤  MCP4728 12-bit DAC  (I2C 0x60, VDD-ref, 1× gain)
                 │      │
                 │      └── VA ──[ 6.2 kΩ ]──► TPS7A57 REF pin
                 │                                  │
   "DAC margining": the TPS7A57's internal 50 µA    │
   IREF flows OUT of the REF pin, through the        │  SNS tied to OUT
   6.2 kΩ series resistor, into the DAC node.        │  → error amp unity-gain
   With SNS tied to OUT the loop is unity-gain, so:  ▼
                                               TPS7A57 LDO
        V_OUT = V_DAC + IREF · R_SERIES              │
              = V_OFFSET + (VDD/4095)·code           │  V_OUT
        (V_OFFSET = IREF · 6.2 kΩ ≈ 0.31 V)          ▼
                                               [MOSFET] ──► SMA wire (Joule heat)
  H7 PG_7 ──► MOSFET gate (load-enable)              │
                                                     │
  H7 A0  ◄── V_OUT via 10k/10k readback divider ─────┘   (÷2 → on-chip 16-bit ADC)

  [LS] = bidirectional logic-level shifter (3.3 V ↔ 5 V) for the I2C bus.
```

> **Note:** the old front end (DAC VA → 2k/10k divider → LDO SET pin,
> `V_OUT ≈ V_mid`) was removed. The DAC now drives the **REF** pin through
> a 6.2 kΩ series resistor (margining), per `src/main.cpp`. Only the
> *readback* path is still a 10k/10k divider.

The H7 never sources analog power; it only sources I2C control, the
MOSFET gate, and reads the divided-down feedback. The bench supply
powers everything analog.

---

## Pin map (Mid Carrier J15 / Arduino mbed core)

| Signal       | mbed name | J15 silkscreen | STM32 pad |
|--------------|-----------|----------------|-----------|
| I2C SDA      | `Wire`    | `I2C0 SDA`     | PB_7      |
| I2C SCL      | `Wire`    | `I2C0 SCL`     | PB_6      |
| MOSFET gate  | `PG_7`    | `PWM 3`        | PG7 (J2-65)|
| Feedback ADC | `A0`      | `ANA0`         | PC_4*     |

\* PC_4 is the mbed-core default mapping for `A0` on the Portenta H7.
Verify against the Mid Carrier pinout PDF if your analog header is
labelled differently — `A0` in the Arduino sketch always refers to
whichever STM32 pad the core mapped it to.

Pins were chosen so they don't overlap with `Firmware_SensorHub_PIO/`'s M4 pins
(`PA_8` / `PC_6` / `PC_7`). The two firmwares can share the same
Portenta; only one M7 image runs at a time, and the M4 image keeps
running the last one flashed regardless of which M7 you boot.

---

## Wiring notes

### Bench supply

External supply (5.0 V typical) feeds:
- MCP4728 VDD
- TPS7A5701 V_IN (V_IN must be > V_OUT + dropout — at least ~5.5 V for
  a 5 V output, so plan for a ≥ 5.5 V rail or accept ~4.5 V V_OUT_max)
- Pull-up rail on the I2C bus (or the level-shifter's high side)

H7 sources nothing on this rail.

### I2C level shifter

H7 GPIO is 3.3 V; MCP4728 at VDD=5 V drives I2C lines to 5 V logic.
**Connect the H7 I2C pins to the MCP4728 only through a bidirectional
level shifter** (a generic BSS138-based board works). Pull-ups on both
sides (typ. 4.7 kΩ each).

If you skip the shifter the H7 sees ~5 V on SDA/SCL during ACKs —
out-of-spec for the STM32 inputs and a reliable way to damage the pad.

### REF-pin margining resistor (DAC → LDO)

The MCP4728 `VA` output drives the TPS7A57 **REF** pin through a single
**6.2 kΩ series resistor** (`R_SERIES` in `main.cpp`). This is TI's
"DAC margining" topology: the LDO's internal 50 µA IREF flows out the REF
pin and through this resistor into the DAC node, so the REF node — and,
with SNS tied to OUT, the output — sits at `V_OUT = V_DAC + IREF·R_SERIES`.
The `IREF·R_SERIES` term (~0.31 V) is the output floor / intercept
(`V_OFFSET`). There is **no** 2k/10k input divider and **no** SET-pin
connection on this board — if you find one on an older harness, it's the
retired topology and won't match the firmware's transfer function.

### Feedback divider sizing (LDO → ADC readback)

LDO output 0..5 V → H7 ADC range 0..3.3 V. We use 10k/10k (FB_DIV_RATIO
= 0.5) → ADC sees 0..2.5 V, comfortably under the 3.3 V Vref. If your
LDO output stays under 3.0 V you can drop the divider entirely and wire
directly; update `R_FB_TOP`/`R_FB_BOT` constants in `main.cpp`.

### MOSFET

N-channel logic-level (e.g. AO3400, IRLML2502). H7 drives the gate at
3.3 V — confirm V_GS(th) is well under 3 V. The MOSFET sources current
to the SMA from the bench supply; the H7 only swings the gate.

---

## Commands

All commands terminated by newline. Output formats are either
human-readable lines or TSV (parsable). 115200 baud.

| Command            | Action |
|--------------------|--------|
| `<voltage>`        | Open-loop set LDO output. Bare-number shortcut for `set`. |
| `set <V>`          | Same as above. |
| `code <N>`         | Set raw DAC code 0..4095 (debug). |
| `read`             | Read LDO output + **SMA current (INA296A), V_sma, R_sma** (averaged 64×). |
| `drive <V> <ms>`   | Apply `V` for `ms` milliseconds, then return to 0. Logs V/I/R every 10 ms during the hold. SMA actuation primitive. |
| `fire <code> [ms] [from]` | **Scope-triggered step**: pulse `TRIG_PIN` (PJ_11 / PWM4) at the DAC write (= scope t₀), hold `ms`, return to 0. Optional `from` sets the pre-step baseline code. MOSFET left as-is. Used by `Experiment_LDOCharacterization/`. |
| `mosfet on \| off` | Load-enable MOSFET (default `off` at boot). |
| `sweep [codestep]` | Raw-code diagnostic sweep across the V range, TSV (`dac_code`, `v_pred`, `v_ldo_meas`). |
| `csv [codestep]`   | Same as sweep, CSV format. |
| `step <code> [ms]` | Log the LDO settle transient at 10 ms cadence (default 1200 ms). MOSFET untouched. |
| `vdd <V>`          | Set MCP4728 VDD — the **slope** of `V_LDO` vs code (default 5.5). Session-scoped, not persisted. |
| `offset <V>`       | Set `V_OFFSET = IREF·R_SERIES` — the **intercept** of the transfer function (default ≈ 0.31 V). Trim to the meter. |
| `aref <V>`         | Set the H7 ADC Vref+ (1-pt readback cal; default 3.145 V). |
| `gain <V/V>`       | Set INA296A gain (default 10 = A1 variant). |
| `shunt <ohm>`      | Set shunt resistance (default 0.1 = 100 mΩ). |
| `ioffset <V>`      | Set INA296A 0 A output offset (default 0; REF=GND). |
| `info`             | Print current state (incl. I / V_sma / R). |

### Current / resistance sense (INA296A)

After the LDO output a **100 mΩ shunt + INA296A (A1, 10 V/V)** reads SMA current on
**A1** at **1 V/A**. `A0` taps *before* the shunt (= `V_ldo`), so firmware reports
`I`, `V_sma = V_ldo − I·R_shunt`, and `R_sma = V_sma / I` (NaN below 1 mA). The
`gain` / `shunt` / `ioffset` commands trim the conversion to the meter.

### `drive` output format

```
[DRIVE] start V=2.500 t_ms=4000
t_rel_ms\tV_set\tV_meas\tI_mA\tR_ohm
0\t2.5000\t2.4870\t497.40\t4.952
10\t2.5000\t2.4985\t499.10\t4.957
...
[DRIVE] done V_final=0.012 I_final=2.40mA R_final=-- ohm max_err=+3.2mV elapsed_ms=4007
```

Lines containing `[` are dropped by the standard host parser in
`Calibrate_LaserHead/portenta_reader.py`, so a downstream consumer can
treat `[DRIVE] start ...` / `[DRIVE] done ...` as event markers and
match the TSV rows in between.

---

## Bring-up checklist

1. **External supply set to 5.00 V, current limit 100 mA** for first
   power-on. Anything wrong burns the limit, not the chip.
2. **MCP4728 alone first.** Disconnect the LDO V_IN. Flash. `info`
   should show DAC code 0; I2C scan should find `0x60`.
3. **DAC sanity:** `code 4095` → probe the MCP4728 `VA` pad. With VDD-ref
   + 1× gain it should read ≈ VDD (the bench supply measured at the chip,
   e.g. ~5.5 V). Confirms the DAC reference and gain are active.
4. **Reconnect the 6.2 kΩ → LDO REF.** `code 4095` → `read`: the LDO
   output should rise toward its ceiling (`V_IN` − dropout), since
   `V_pred = V_OFFSET + VDD` exceeds what the rail can deliver at full
   scale. Confirms the margining path and unity-gain REF (IREF·RREF) are
   live. Use mid-range codes for the actual linearity check below, where
   `V_OUT = V_OFFSET + V_DAC` isn't rail-clamped.
5. **Linearity check:** `csv 100` → CSV across the code range. Plot
   `v_ldo_meas` vs `v_pred`. Acceptable if `R² > 0.999` and worst-case
   residual < 50 mV (the same threshold used in the Uno-version cal). If
   much worse, the cause is usually:
   - `VDD_MCP` (slope) mismatch — measure the bench supply at the chip
     and `vdd <actual>` to correct.
   - `R_SERIES` / `V_OFFSET` (intercept) off — measure the 6.2 kΩ
     in-circuit and trim with `offset <V>`.
   - LDO loading effect (the bench verify uses no SMA load — the open-
     loop model assumes the LDO is unloaded except for the readback
     divider).
6. **`drive` smoke test:** `drive 3.0 1000` with no SMA wired (open
   circuit at MOSFET drain). Should print `start`, ~100 TSV rows at
   2.5 V (or wherever the LDO settles), then `done`. Confirms timing
   loop is honest.
7. **SMA in the loop:** wire the SMA across the MOSFET. Start very
   conservative: `drive 1.0 500` first. Inch up while watching the
   load cell and laser readings from `Firmware_SensorHub_PIO/`.

---

## Open questions for the merge into `Firmware_SensorHub_PIO`

These don't gate the bring-up but should be settled before the merge:

- **Ring-buffer push during `drive`.** Once integrated with M4's sample
  ring, the M7 should also push SMA feedback samples (src=3, per
  `Firmware_SensorHub_PIO/src/sample_ring.h` reservation table) so the host
  sees SMA V time-aligned with laser/load samples. M7 is currently
  consumer-only on that ring — making it a producer needs a small lock
  or a per-producer ring slot. Probably easiest: a tiny dedicated
  M7→host event ring rather than mixing producers in the same ring.

- **Command channel multiplexing.** `Firmware_SensorHub_PIO`'s M7 already drains
  samples to USB Serial. Mixing operator commands on the same Serial
  is fine (input is rare; output is high-rate but line-based — the
  parser already tolerates non-sample lines). The host-side reader
  needs a small write API to send commands. Trivial; just call it out.

- **State events.** `[DRIVE] start` / `[DRIVE] done` lines are the
  current event channel. For Phase 6 closed-loop control via the
  `SMACommand` struct in `PLAN_phase5_spring_smoke_test.md`, the
  events should also be emitted as `src=0xF0+` entries through a
  dedicated event channel. Defer until SMACommand is in place.

---

## References

- Original Uno code: `doc/ArduinoUnoMCP4728LDO.ino` (uploaded
  2026-06-01) — source for the transfer function and the REF-pin
  margining (IREF·RREF, unity-gain) assumption.
- `doc/PLAN_phase5_spring_smoke_test.md` §"SMA-ready architecture
  (Phase 6 preview)" — IPC contract this firmware eventually
  implements (`SMACommand` setpoint struct, src ID reservations).
- `Firmware_SensorHub_PIO/src/sample_ring.h` — src=3/4/5 reservations for the
  Phase 6 SMA channels (drive V, shunt I, computed R).
- MCP4728 datasheet — 12-bit DAC, I2C 0x60 default, VDD-ref + 1× gain
  needed for full 0..VDD range.
- TPS7A5701 (TPS7A57) datasheet (SBVS395) — REF-pin programming via
  IREF·RREF (Eq. 5; IREF ≈ 50 µA from Table 7-4), CNR/SS soft-start
  (the ~100 ms REF-node settle).
