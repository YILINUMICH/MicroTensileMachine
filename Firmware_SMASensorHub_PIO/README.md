> **Status: To-Test** — code-complete + reviewed, not yet bench-verified. See [STATUS.md](STATUS.md). Project overview: [../README.md](../README.md).

# Firmware_SMASensorHub_PIO — combined sensing + SMA drive (M4 + M7)

The **Phase-6 merge**: one Portenta H7 image pair that runs the dual-ADC
sensor stream **and** the SMA Joule-heating drive path together. It folds
the two separately-verified firmwares into one:

| Source | What it contributed | Lands on |
|---|---|---|
| [`Firmware_SensorHub_PIO`](../Firmware_SensorHub_PIO/) | dual ADS1263 sampler → SRAM4 ring; ring→USB bridge; `[STATUS]` | **M4** (unchanged) + **M7** (bridge half) |
| [`Firmware_SMADriver_PIO`](../Firmware_SMADriver_PIO/) | MCP4728 DAC → TPS7A57 LDO → MOSFET; INA296A current sense; `set`/`drive`/`fire`/… | **M7** (controller half, restructured) |

Both originals remain in the tree as single-purpose reference builds.

## The one design problem this solves

M4 and M7 already cooperated in `Firmware_SensorHub_PIO` (M4 samples, M7
bridges). The merge gives **M7 a second job** — driving the SMA — and M7
is single-threaded. The SMA commands `drive`, `fire`, `sweep`, and the LDO
`settle` **block for 0.1–60 s** in the standalone firmware. If M7 blocks,
it stops draining the sensor ring → the force/displacement stream freezes
**exactly while the SMA actuates**, which is the worst possible moment to
go blind, and long ops would overflow the ring.

**Fix: the SMA controller is a non-blocking state machine.** Each `loop()`
pass does three quick things and returns:

```
loop():
  pumpSensors();              // drain RPC + sensor ring → USB, emit [STATUS]
  if (pollCommand(line))      // non-blocking char accumulator (no readStringUntil)
      dispatch(line);
  serviceSma();               // advance the active SMA op by ONE step
```

Long ops (`drive`/`fire`/`step`/`sweep`) are held as *state* and advanced
one step per pass, so:

- the sensor stream **never pauses** during SMA actuation (force + displacement
  are captured while the wire heats — the whole point of the rig);
- an **`abort`** command (and `read`/`info`/parameter sets) is accepted
  *during* a live `drive`;
- worst-case time between `pumpSensors()` calls is a single `readSma()`
  (~ a few ms) — far under the ~1.28 s ring headroom at 800 samples/s.

The tested electrical primitives (`readSma`, `setDACraw`, `vldoToCode`,
the analytical TPS7A57 transfer, INA296A math) are carried over unchanged;
only the control flow around them was restructured.

## Shared USB-CDC port: three line classes

One port now carries three kinds of line. The host sensor parser already
drops any line containing `[`, so all three demultiplex cleanly:

| Class | Example | Producer |
|---|---|---|
| **sensor TSV** (untagged) | `12⇥1⇥26214400⇥2.515000⇥10342⇥77` | M7 draining the M4 ring |
| **`[STATUS]`** (1 Hz) | `[STATUS] t_ms=… hwm=… dropped=… rate1=… sma_state=0 …` | M7 bridge telemetry |
| **`[SMA]`** | `[SMA] V_LDO=2.5012V I=0.00mA …` | M7 SMA controller |

`sma_state=` was added to the `[STATUS]` frame (0 = IDLE) so the host can
see when an SMA op is running.

## Pin map — why the merge is safe (no overlap)

| Core | Function | Pin |
|---|---|---|
| **M4** | ADS1263 CS | PA_8 |
| **M4** | ADS1263 DRDY | PC_6 |
| **M4** | ADS1263 RESET | PC_7 |
| **M4** | SPI | (SPI1 bus) |
| **M7** | I2C → MCP4728 | PB_6 (SCL) / PB_7 (SDA) |
| **M7** | LDO feedback (A0) | PC_4 / ANA0 |
| **M7** | INA296A current sense (A1) | ANA1 |
| **M7** | MOSFET (load enable) | PG_7 (= D3 / PWM3) |
| **M7** | scope TRIG out | PJ_11 (PWM4) |

Two different cores, two disjoint peripheral sets. The ADS1263 (M4, SPI)
and the SMA path (M7, I2C + on-chip ADC + GPIO) never touch the same pin.

## Current sense (INA296A) — enabled

The current-sense channel is live on **A1**: primed in `setup()`, read in
every `readSma()`, reported by `read`/`info` and the `drive`/`fire`
feedback rows, and trimmable at runtime:

```
gain <V/V>     INA296A gain        (default 10  = A1 variant)
shunt <ohm>    shunt resistance    (default 0.1 = 100 mOhm  → 1 V/A)
ioffset <V>    0 A output offset   (default 0   = REF→GND)
```

`I = (V_ina − ioffset) / (gain·shunt)`, `V_sma = V_ldo − I·shunt`,
`R = V_sma / I` (NaN below a 1 mA floor).

### Streaming SMA feedback — `src=3/4/5` (enabled)

During `drive` (every 10 ms feedback point) and at `drive`/`fire` end, M7
emits SMA feedback as **untagged sensor-TSV lines**, time-aligned in the
sample stream alongside laser/load:

| src | meaning | `raw` col | `voltage_V` col |
|---|---|---|---|
| **3** | SMA drive voltage | DAC code | V_ldo |
| **4** | SMA current (INA296A) | 0 | I [A] |
| **5** | SMA resistance V/I | 0 | R [ohm] (omitted when I < floor) |

These are the [`src/sample_ring.h`](src/sample_ring.h) reservations. M7 is
the sole USB writer, so it formats them directly — **no ring producer**,
the M4-owned SPSC ring is untouched. The per-row `[SMA]` text is dropped to
avoid duplicating the data; the human-readable `[DRIVE]`/`[FIRE]`
start/done banners stay.

> **Clock note:** `t_ms`/`hw_us` on `src=3/4/5` lines are M7's clock,
> distinct from the M4 laser/load lines (the cores boot at different
> times). The host joins all streams on its own arrival clock; the
> embedded stamps are for per-stream jitter/drop detection only.
>
> **Host TODO:** the parser in `Calibrate_LaserHead/portenta_reader.py`
> currently selects `src=1`/`2`; extend it to keep `src=3/4/5` to log SMA
> feedback.

## Commands (115200 baud, `[SMA]`-tagged replies)

```
set <V> | <number>     Set LDO output to V volts (analytical inverse)
code <N>               Set raw DAC code 0–4095 (predicted + measured)
read                   LDO out, SMA current (INA296A), V_sma, R_sma
drive <V> <ms>         Apply V for ms, then DAC→0 + MOSFET off. Logs every 10 ms.
fire <code>[ms][from]  Scope-triggered step: TRIG edge at the DAC write.
cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>
                       Autonomous heat/cool actuation cycle (see below). n=0 = continuous.
ping                   Heartbeat — resets the cycle watchdog (silent)
stop                   Graceful stop of a running cycle → safe state
wdt <ms>               Cycle watchdog timeout (0 = disabled)
step <code>[ms]        Log the LDO settle transient (10 ms cadence)
sweep|csv [step]       Raw-code sweep, predicted vs measured
mosfet on|off          Load-enable MOSFET
abort                  Interrupt any running op → safe state (DAC 0, MOSFET off)
gain|shunt|ioffset <x> INA296A current-sense trims
vdd|offset|aref <V>    Transfer-function / ADC-ref trims
info | reset           State dump / safe-state soft reboot
```

`read`, `info`, `abort`, `ping`, `stop`, `wdt`, `mosfet`, and the trim
setters work **any time** (including mid-`drive`/`cycle`). The motion
commands (`set`/`code`/`drive`/`fire`/`step`/`sweep`/`cycle`) are rejected
while another op runs (`abort`/`stop` first) or if no MCP4728 is present.

## Cyclic actuation — the experiment state machine on M7

`cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>` runs the heat/cool profile
**autonomously on M7**:

```
IDLE → HEAT (V_high for fire_ms, TRIG high)
     → COOL (V_low for cool_ms, TRIG low) → repeat n times → IDLE (safe)
                                              (n = 0 → forever until `stop`)
```

All phase timing comes from M7's own `millis()`, so it is **deterministic
and independent of host/USB latency** — the PC is never in the timing loop.
The host only sets the parameters (`cycle …`) and sends a periodic `ping`
heartbeat. V/I/R keep streaming as `src=3/4/5` throughout, and each
transition is emitted as a `[SMA] [CYCLE] …` event.

**Heartbeat watchdog (safety).** While cycling, if no `ping` arrives within
`wdt <ms>` (default 5000 ms; `wdt 0` disables), M7 aborts to a safe state
(DAC 0, MOSFET off). This protects an unattended SMA wire if the host
crashes or disconnects mid-run. For manual bench use, either `ping`
periodically, raise/disable the timeout with `wdt`, or just let it
safe-stop. `Experiment_SMACharacterizationV3` drives this automatically:
it sends `cycle` at RAW start, `ping` every second, and `stop` at the end.

Example: `cycle 3.0 0.0 2000 8000 10` — ten cycles of 2 s heat at 3 V then
8 s cool at 0 V; `stop` to end early, `abort` for an emergency stop.

## Flash & run

```sh
pio run -e portenta_m7 -t upload     # M7: bridge + SMA controller
pio run -e portenta_m4 -t upload     # M4: dual-ADC sampler
pio device monitor                    # 115200 baud
```

Re-flash whichever core you changed. **M7 is no longer "flash once"** — it
carries the SMA logic now.

> **Power-cycle the rig (USB + EVM supply) after every upload.** The dfu
> reset does not cleanly re-power the EVM's analog rails; skip the cycle
> and the ADS1263 comes up with `ID=0x00`.

### Isolated SMA bring-up (optional)

To test the SMA path with no SPI/ring traffic from M4:

```sh
pio run -e portenta_m4_idle -t upload   # wipe M4 to an empty __WFI() loop
# power-cycle, then flash/monitor M7
```

(`-D M4_IDLE` compiles the M4 branch to a do-nothing image. The sensor
stream will be empty in this mode — SMA commands only.)

## File layout

```
Firmware_SMASensorHub_PIO/
├── README.md            (this file)
├── STATUS.md            To-Test status + module TODOs
├── platformio.ini       3 envs: portenta_m7, portenta_m4, portenta_m4_idle
├── src/
│   ├── main.cpp         both cores, #ifdef-guarded; M7 = bridge + SMA state machine
│   └── sample_ring.h    SRAM4 ring (copy of the SensorHub canonical)
└── lib/
    └── ADS1263/         ADS1263 driver (copy of the SensorHub canonical)
```

> The `sample_ring.h` and `lib/ADS1263/` here are **copies** of the
> `Firmware_SensorHub_PIO` canonical versions (the repo has no shared
> library target). If you fix either upstream, propagate the fix here too.

## Deferred to a later Phase-6 step (not in this merge)

Per the SMA-ready architecture in [`../docs/PLAN_phase5_spring_smoke_test.md`](../docs/PLAN_phase5_spring_smoke_test.md):
the `SMACommand` SRAM4 setpoint struct, the M4-side closed-loop state
machine (R-as-feedback), and migrating SMA control onto M4. This merge
keeps SMA control on **M7** by design — lowest-risk reuse of the verified
drive code — and is structured as a state machine so that migration is a
restructure, not a rewrite.
