# SMA_Driver_PIO

Phase 6 SMA drive-path bring-up firmware. Portenta H7 M7 talks I2C to an
externally-powered MCP4728 DAC, the DAC drives a TPS7A5701 LDO, the LDO
heats the SMA wire through a MOSFET-gated load. The H7 reads the LDO
output back through a divider into an on-chip 16-bit ADC.

Standalone M7-only project. Once `set` / `drive` accuracy and linearity
are bench-verified, the M7 control code merges into `SensorHub_PIO/` M7
alongside the dual-ADC sample stream.

**Status:** bring-up draft (2026-06-01). Not yet flashed; not yet
bench-verified.

---

## Architecture

```
            ┌────────────── External 5 V bench supply ──────────────┐
            │                                                       │
            │            ┌── VDD ──┐         ┌── V_IN ──┐            │
            │            │         │         │          │            │
  H7 SDA ◄──┼──► [LS] ◄──┤ MCP4728 │── VA ──►│  2k/10k  │            │
  H7 SCL ◄──┼──► [LS] ◄──┤  DAC    │         │  divider │            │
            │            └─────────┘         └─► V_mid ─┘            │
            │                                          │             │
            │                                          ▼             │
            │                                  ┌───────────────┐     │
            │                                  │  TPS7A5701    │     │
            │                                  │  LDO  (SET=V_mid)   │
            │                                  │      V_OUT ≈ V_mid  │
            │                                  └──────┬────────┘     │
            │                                         │              │
            │                       ┌─────────────────┴───────┐      │
            │                       ▼                         │      │
            │                  [MOSFET] ───── SMA wire ───────┤      │
  H7 PG_7 ──┼──► gate                                         │      │
            │                                                 │      │
            │                       ┌── 10k/10k feedback ─────┘      │
            │                       │   divider                      │
            │                       ▼                                │
  H7 A0  ◄──┼─── V_LDO/2                                             │
            │                                                        │
            └────────────────────────────────────────────────────────┘

  [LS] = bidirectional logic-level shifter (3.3 V ↔ 5 V) for the I2C bus.
```

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

Pins were chosen so they don't overlap with `SensorHub_PIO/`'s M4 pins
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

### Feedback divider sizing

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
| `read`             | Read LDO output (averaged 64×). |
| `drive <V> <ms>`   | Apply `V` for `ms` milliseconds, then return to 0. Logs feedback every 10 ms during the hold. SMA actuation primitive. |
| `mosfet on \| off` | Load-enable MOSFET (default `off` at boot). |
| `sweep [mV]`       | DAC sweep across the open-loop V range, TSV. |
| `csv [mV]`         | Same as sweep, CSV format. |
| `vdd <V>`          | Override assumed MCP4728 VDD (default 5.0). Affects open-loop V↔code math. Session-scoped — not persisted. |
| `info`             | Print current state. |

### `drive` output format

```
[DRIVE] start V=2.500 t_ms=4000
t_rel_ms\tV_set\tV_meas
0\t2.5000\t2.4870
10\t2.5000\t2.4985
...
[DRIVE] done V_final=0.012 max_err=+3.2mV elapsed_ms=4007
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
3. **DAC sanity:** `code 4095` → `read` should show ~4.58 V at the
   MCP4728 VA pad (probe it). Confirms VDD ref + 1x gain are active.
4. **Reconnect LDO V_IN.** `code 4095` → `read` should show ~4.58 V at
   the LDO output. Confirms divider + LDO SET-pin gain (≈1).
5. **Linearity check:** `csv 100` → 46-row CSV from 0.5 V to 5.0 V in
   100 mV steps. Plot `v_ldo_meas` vs `v_ldo_nominal`. Acceptable if
   `R² > 0.999` and worst-case residual < 50 mV (the same threshold
   used in the Uno-version cal). If much worse, the cause is usually:
   - `VDD_MCP` mismatch (measure the bench supply at the chip and
     `vdd <actual>` to correct).
   - Wrong divider resistors (measure them in-circuit).
   - LDO loading effect (the bench Verify uses no SMA load — the open-
     loop model assumes the LDO is unloaded except for the divider).
6. **`drive` smoke test:** `drive 3.0 1000` with no SMA wired (open
   circuit at MOSFET drain). Should print `start`, ~100 TSV rows at
   2.5 V (or wherever the LDO settles), then `done`. Confirms timing
   loop is honest.
7. **SMA in the loop:** wire the SMA across the MOSFET. Start very
   conservative: `drive 1.0 500` first. Inch up while watching the
   load cell and laser readings from `SensorHub_PIO/`.

---

## Open questions for the merge into `SensorHub_PIO`

These don't gate the bring-up but should be settled before the merge:

- **Ring-buffer push during `drive`.** Once integrated with M4's sample
  ring, the M7 should also push SMA feedback samples (src=3, per
  `SensorHub_PIO/src/sample_ring.h` reservation table) so the host
  sees SMA V time-aligned with laser/load samples. M7 is currently
  consumer-only on that ring — making it a producer needs a small lock
  or a per-producer ring slot. Probably easiest: a tiny dedicated
  M7→host event ring rather than mixing producers in the same ring.

- **Command channel multiplexing.** `SensorHub_PIO`'s M7 already drains
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
  2026-06-01) — source for the transfer function and SET-pin gain
  assumption.
- `doc/PLAN_phase5_spring_smoke_test.md` §"SMA-ready architecture
  (Phase 6 preview)" — IPC contract this firmware eventually
  implements (`SMACommand` setpoint struct, src ID reservations).
- `SensorHub_PIO/src/sample_ring.h` — src=3/4/5 reservations for the
  Phase 6 SMA channels (drive V, shunt I, computed R).
- MCP4728 datasheet — 12-bit DAC, I2C 0x60 default, VDD-ref + 1× gain
  needed for full 0..VDD range.
- TPS7A5701 datasheet — ANY-OUT pin programming, SET-pin gain.
