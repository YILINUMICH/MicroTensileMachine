# MEMO — ADS131M04 bring-up procedure

**For:** the operator at the bench. **Created:** 2026-08-27, branch `feat/ads131m04`.
**Plan:** [`ADS131M04_migration_plan.md`](ADS131M04_migration_plan.md) — read §2 before wiring.
**Firmware:** `Firmware_ADS131M04Test_PIO/` · **Host:** `Experiment_ADS131M04Eval/`

Work the steps **in order**. Each one is a gate: it exists because the step
after it cannot be diagnosed if this one is not known-good. Every step says
what you should see and what it means when you don't.

> **The one thing that will mislead you.** With CLKIN absent this chip still
> answers register reads perfectly — right ID, right register values — and
> simply never converts. So a good `id` and a good `regs` dump do **not** mean
> the ADC works. Step 6 is the first step that proves conversion, and it is
> not optional.

---

## Step 0 — before anything is connected

- [ ] ADS131M04 EVM, Portenta H7 on the Mid Carrier, the existing 8-wire Cable 1.
- [ ] **Do NOT connect the REF7050** (Cable 2). This part has no external
      reference input. Leave that cable on the ADS1263 EVM so that board stays
      ready for the A/B.
- [ ] **Do NOT connect any sensor yet.** Steps 1–9 run with inputs grounded.
      Sensors are Stage 2 and need the ÷6 divider board first.
- [ ] Nothing above 3.6 V may ever reach an AIN pin. Absolute maximum is
      AVDD + 0.3 V, and the current limit is ±10 mA — an undivided 5 V sensor
      on an input is a **destroyed part**, not a bad reading.

## Step 1 — wire to the Mid Carrier

Same eight wires as the ADS1263, so Cable 1 is re-terminated, not rebuilt.

| Signal | Mid Carrier J15 | EVM | Note |
|---|---|---|---|
| SCLK | J15-20 (`SPI1 SCLK`, PI_1) | J6[5] | |
| COPI / DIN | J15-24 (`SPI1 COPI`, PC_3) | J6[2] | |
| CIPO / DOUT | J15-22 (`SPI1 CIPO`, PC_2) | J6[7] | |
| /CS | J15-25 (`PWM 0`, PA_8) | J6[4] | GPIO, not SPI1 hardware CS |
| /DRDY | J15-27 (`PWM 1`, PC_6) | J6[6] | one DRDY covers all four channels |
| SYNC/RESET | J15-29 (`PWM 2`, PC_7) | J6[1] | active low |
| GND | J15-1 or J15-2 | J6[8] | **must be common** |
| 3V3 | J15-3 or J15-4 | EVM TP1 | DVDD — see Step 1b |

- [ ] **EVM J6[3] (CLK) — leave UNCONNECTED.** Y1 drives it on-board.
- [ ] **JP6 / J13 fitted at `[1-2]`** — selects Y1's 8.192 MHz. **CLKIN is mandatory.**
- [ ] **JP5 NOT fitted** — JP5 powers Y1 *down*.
- [ ] **JP1–JP4 left at the factory `[3-4]`** — grounds every input through
      1 kΩ. That *is* the shorted-input condition the noise test wants.
- [ ] Ethernet: H7 carrier RJ45 → the PC's USB GbE dongle. H7 is static
      `169.254.245.50`; the PC NIC sits on the same link-local segment
      (`169.254.245.100`).

## Step 1b — power the EVM (external, no PHI)

This rig runs the EVM **without the PHI controller board**, so neither rail
arrives on its own. The two are separate nets — TP1 and TP2 are *not* the input
and output of a regulator, and nothing on the board derives one from the other.

| Rail | Test point | Source | Wanted |
|---|---|---|---|
| **DVDD** | **TP1** | H7 `3V3`, J15-3/J15-4 | 3.0–3.3 V |
| **AVDD** | **TP2** | on-board LP5907 (U1) | 3.3 V |

- [ ] **R45 removed.** It is the 0 Ω that fed `DVDD` from the PHI. Lifting it is
      what frees TP1 to be driven externally (EVM guide §4).
- [ ] **5 V into `EVM_RAW_5V`** — the R46 pad on the U1 side, or the C16 `+` pad
      (same node, larger target; it beeps against U1 pin 1). This is U1's input.
      **Never onto TP1:** that is DVDD, whose absolute maximum is 3.9 V.
- [ ] **JP9 at the factory `[1-2]`** — AVDD ← `3V3_LDO`. **JP8 NOT fitted** —
      fitting it pulls `/LDO_EN` low and disables U1.
- [ ] **Confirm TP2 = 3.3 V before anything else.** That one reading proves the
      5 V landed on the right node. TP2 sitting near ~1 V means U1 has no input
      and you are measuring backfeed through the ADC, not a supply.
- [ ] Common ground between the bench supply, the H7 and the EVM (J6[8] or J5[3]).

**Power the EVM's 5 V up before, or with, the H7 — not after.** DVDD tracks the
H7 because it comes off the H7's own 3V3, but AVDD does not: `t_POR` is
specified from *the ADC's* supplies reaching 90 % (datasheet §6.7), which the H7
cannot observe. The firmware re-probes once a second and will attach late
(Step 3), so a late supply is recoverable rather than fatal — but ordering it
correctly means the boot banner tells you the truth the first time.

> DVDD's recommended range is **2.7 / 3.0 / 3.6 V** (§6.3). The 1.65 V floor in
> that table is the *other* row — it applies only when CAP is tied to DVDD and
> the internal digital LDO is bypassed, which this EVM does not do.

## Step 2 — flash, in this order

```
pio run -e portenta_m4_idle -t upload     # FIRST
pio run -e portenta_m7      -t upload
#   >>> POWER-CYCLE USB + EVM, wait ~5 s, reapply <<<
pio device monitor                        # 115200
```

- [ ] **M4 idle goes first.** Whatever M4 image is resident drives the *same*
      SPI1 bus and the *same* CS pin. Leave it running and the two cores fight
      over the bus — which presents as intermittent CRC errors that look
      exactly like a cable fault, and will waste an afternoon.
- [ ] **Power-cycle after every upload.** Rig convention: the DFU reset does
      not cleanly re-power the EVM's analog rails.

## Step 3 — connection test (T1)

Watch the boot banner.

**Expect:**
```
*** Firmware_ADS131M04Test_PIO — M7 bench test ***
[T1] PASS: id=0x2400
```

**If not:**

| Symptom | Means |
|---|---|
| `id=0x0000` or `id=0xFFFF` | Nothing is driving DOUT. SPI wiring, EVM unpowered, or grounds not common. Check TP1 and TP2 (Step 1b) before touching the cable — an unpowered DVDD cannot drive DOUT, and reads exactly like a broken CIPO. |
| `id=0x24xx` but nothing else works | Link is fine — go on; the fault is downstream. |
| Anything else plausible-looking | Suspect a swapped COPI/CIPO or a CS on the wrong pad. |
| `[BOOT] ADS131M04 NOT FOUND` but `[STATUS]` keeps coming | Intentional — the firmware does not halt, so you can see `adc_ok=0` rather than a dead port you can't distinguish from a bad cable. |
| `[BOOT] ADS131M04 attached late, id=0x2400` | Normal, and a **passed** T1. The EVM's rails came up after the H7 booted; the once-a-second re-probe found it. Fix the ordering (Step 1b) if you would rather not see it. |

The probe does not give up at boot. It re-asks every second, so you can power
the EVM after the fact and watch it attach — no H7 reset needed. That also means
a persistent `NOT FOUND` with both test points confirmed is a *real* fault, not
a sequencing accident, which is the whole point of the retry.

Bits 15:12 of ID are always `0010b` and bits 11:8 are `CHANCNT = 0100b`, so the
**high byte is always `0x24`**. The low byte is "subject to change" — ignore it.

> **Measured on this part (2026-08-30): `id=0x2403`.** That is a valid ID — high
> byte `0x24` as §8.6.1 requires, low byte `0x03` the don't-care revision. T1
> passes repeatably.
>
> **If the TI EVM GUI shows `0xFF24`, that is not the ID register.** Table 8-11
> lists `1111 1111 0010 0100` = `0xFF24` as the **response to the RESET
> command**. Reading it as a corrupt or byte-swapped ID sent this bring-up down
> a blind alley for an afternoon; the value simply means the GUI had issued a
> reset and the link was healthy.
>
> **The PHI drives the same SPI bus.** Remove it before reconnecting the H7,
> and put the Step 1b supply arrangement back.

## Step 3b — if T1 fails: work the elimination, do not guess

This was run end to end on 2026-08-30 and took a full day. **Follow the order.**
Each step gives a *behavioural* confirmation — the ADC's own reaction — which is
worth far more than measuring a voltage on a connector.

> **The trap that cost that day.** On the Portenta H7,
> `digitalWrite(PI_1, HIGH)` through the Arduino **PinName** overload silently
> does nothing (0 V at D9), while `pinMode(9, OUTPUT)` through the **integer**
> path drives it correctly. PA_8, PC_2 and PC_3 work either way; PI_1 — SCK — does
> not. A hand-clocked SPI test therefore produced **no clock edges at all**, and
> its empty response was read as "SCLK never reaches the ADC". It nearly led to
> re-terminating a wire that was fine.
>
> **A test that produces no signal is not evidence about the wire.** Prove the
> pin drives, with a meter, before believing any negative result.

> **Run check 0 FIRST. It is what the 2026-08-30 elimination missed.** Every
> signal below is read single-ended with the meter's black lead on *EVM* ground
> — which is exactly the measurement that cannot see a broken **shared**
> reference. Two real faults were found that day and one of them was the ground.

| # | Check | Command | A pass means |
|---|---|---|---|
| **0** | **GND across the harness** | **meter, board UNPOWERED** | **`J15-1`/`J15-2` to `J6[8]` under ~1 Ω, and no worse than its siblings in the same cable.** Measure a second conductor the same way as a control. ~1 Ω of contact resistance in the ground return was one of the two real faults. Resistance readings taken on a *powered* board are meaningless — the same wire read 11.5 Ω live and 1.3 Ω unpowered |
| 1 | Rails | meter | TP1 ≈ 3.0 V, TP2 ≈ 3.3 V (Step 1b) |
| 2 | Clock + conversion | `drdyscan` | `driven=yes` and edges — CLKIN is running and the ADC converts. Proves Y1/JP6 without touching SPI |
| 3 | /CS at the die | `cipotest` | `cs_high=floating` → `cs_low=driven`. The ADC responds to chip-select, so /CS and DOUT both reach it |
| 4 | SCLK and DIN arrive | `clocktest 60`, then meter | J6[5] ≈ 0.8 V and J6[2] ≈ 2.2 V, matching D9 and D8. The two duty cycles differ, which is what makes it trustworthy |
| 5 | Signals reach the **die** | continuity | J6[5] → chip pin 14, J6[2] → chip pin 16. Use J6[4] → pin 12 as the control — that one is behaviourally proven, so it shows what a good reading looks like on 0.65 mm pitch |
| 6 | M4 not on the bus | `pio run -e portenta_m4_idle -t upload` | rules out SPI1 contention |
| 7 | Scope | J6[5] + J6[7] during a transfer | clock present, DOUT never shifts ⇒ **the part** |

**Chip pinout (TSSOP-20), for step 5:**

```
11 SYNC/RESET   16 DIN
12 CS           17 CLKIN
13 DRDY         18 VCAP
14 SCLK         19 DGND
15 DOUT         20 DVDD
```

**Already eliminated on this rig — do not re-test:** SPI mode (1, CPOL=0/CPHA=1),
SPI clock rate (identical at 250 k / 500 k / 2 MHz), command encoding
(RREG `0xA000`, WREG `0x6000`, ack `0x4000`, `addr << 7`, all per §8.5.1.10),
register address (ID at `0h`, reset `24xxh`, §8.6.1), and the two-frame read
sequence. The firmware protocol is verified correct.

**Bring-up commands** in this build — scaffolding, not part of the test suite:
`raw [n]`, `wtest <addr> <val>`, `drdyscan`, `cipotest`, `bitbang`, `pintest`,
`hold <line> <0|1>`, `clocktest <s>`, `xtalk`, `walk`. Their verdict text is more
confident than the evidence warranted; several were written against the broken
PinName premise. Trust the measurements, not the verdict lines.

The two most useful, because they print evidence rather than a verdict:

- **`raw [n]`** — whole DOUT frames as they arrived, with the chip's CRC beside
  ours. This is what distinguishes "bits corrupted on the wire" (garbled data,
  CRCs differ) from "intact frame, our CRC disagrees" (sane data, CRCs differ).
  It captures contiguously and prints afterwards, deliberately: printing between
  frames inserts ~1 ms gaps that fill the two-deep output FIFO (§8.5.1.9.1) and
  inflate the error rate ~9x. A dump with gaps in it measures the gaps.
- **`wtest <addr> <val>`** — all three frames of a register write, so the ack can
  be checked against Table 8-11's `010a aaaa ammm mmmm` instead of inferred.

## Step 3c — host-side rules, learned the hard way

- **Hold one long-lived reader that never stops draining** (see
  `Firmware_ADS131M04Test_PIO/STATUS.md` for `m04_daemon.py`). The sample stream
  saturates USB-CDC; any gap in host reading blocks the M7 in `Serial.write`
  **permanently** — it does not recover when draining resumes, and needs a USB
  force-pull. Discrete open/read/close scripts leave exactly such gaps.
- **Send `netcfg <pc_ip> 7777` first after every boot** to move the stream off
  USB-CDC entirely (Step 7). Do it before anything else.
- **Opening the port with DTR asserted resets the board**, which then costs ~60 s
  in `Ethernet.begin()`. DTR *low* is not a workaround: the Portenta CDC then
  treats the host as absent and transmits nothing at all.
- **`adc_ok` and `present` latch.** They are set when the part first attaches and
  are never re-checked, so they keep reading healthy after the link dies. Trust
  `drdy`, `samples` and `rate` instead.
- **The Arduino `SPI` object must never be used on this bus.** The mbed core
  silently drops the SPI mode across `SPI.end()`/`SPI.begin()`, leaving the
  peripheral in mode 0 while the caller believes mode 1 — every word then arrives
  right-shifted by one (STATUS `0x050F` reads as `0x0287`). The driver owns an
  `mbed::SPI` instead; use `adc.busRelease()` / `adc.busAcquire()` around any
  GPIO use of the SPI pins. Full write-up in the driver header.

## Step 4 — register reading

```
regs
```

**Expect** (reset defaults, except CLOCK which setup() has already programmed):

| Register | Addr | Expect | Meaning |
|---|---|---|---|
| `ID` | 0x00 | `0x24xx` | as Step 3 |
| `STATUS` | 0x01 | `0x0500` | bit10 `RESET`=1 (expected after a reset), WLENGTH=01b (24-bit) |
| `MODE` | 0x02 | `0x0510` | 24-bit words, CCITT CRC, DRDY low-level |
| `CLOCK` | 0x03 | `0x0F1A` | all 4 ch enabled, OSR 8192, HR — **not** the `0x0F0E` reset value, because setup() configured 500 SPS |
| `GAIN1` | 0x04 | `0x0000` | gain 1 on all channels |
| `CFG` | 0x06 | `0x0600` | current-detect off |

**If every register reads `0x0000` or `0xFFFF`** the link is dead — back to Step 3.
**If registers read plausibly but CLOCK is `0x0F0E`**, `configure()` failed;
check the `[CFG]` line in the boot text.

## Step 5 — configuration write / read-back (T2)

```
selftest
```

**Expect:**
```
[T1] PASS: id=0x2400
[T2] PASS: register round-trip
```

T2 writes four values to `CLOCK` and one to `GAIN1` and reads each back. It is
the test that catches the two things this protocol most plausibly gets wrong:
the WREG payload word sitting immediately after the command, and the fact that
a response answers the **previous** frame. If T2 fails but T1 passes, the fault
is in the driver's framing, not the wiring.

Then try a configuration by hand and watch the echo:

```
osr 5          ->  [CFG] osr=4096 rate=1000.00 SPS
osr 6          ->  [CFG] osr=8192 rate=500.00 SPS
gain 0 2       ->  [CFG] ch0 gain=2 fsr=+/-0.6000V
gain 0 1       ->  [CFG] ch0 gain=1 fsr=+/-1.2000V
spi 8000000    ->  [CFG] spi=8000000
```

## Step 6 — prove it is actually CONVERTING

**This is the step a register dump cannot give you.** Watch `[STATUS]`:

```
[STATUS] up=12 loop_hz=... frames=... crc_err=0 drdy=6012 rate=500.12 ... adc_ok=1 udp_on=0
```

- [ ] `rate` ≈ **500** at OSR 8192 (and ≈1000 at OSR 4096, ≈252 at 16256)
- [ ] `drdy` **increasing** every second
- [ ] `crc_err` **0** and staying 0

**`rate=0.00` with `drdy` frozen while `id` and `regs` are perfect = CLKIN is
absent.** Check JP6 is fitted at `[1-2]` and JP5 is *not* fitted. This is the
failure mode the memo warns about at the top; it presents as "the data is
frozen", never as an error.

`crc_err` climbing → drop the SPI clock (`spi 2000000`) and see if it stops.
That is Step 8's question, asked early.

## Step 7 — move the stream to UDP

```
netcfg 169.254.245.100 7777
```

**Expect** `[NET] UDP stream -> 169.254.245.100:7777`, and `udp_on=1` in the
next `[STATUS]`. The boot text also prints the H7's own IP and link state.

If the link is unavailable, flash `portenta_m7_usb` instead and pass
`--transport usb` to the sweep — everything else in this memo is unchanged.

## Step 8 — SPI clock ladder (T3)

From `Experiment_ADS131M04Eval/`:

```
python operator_m04_sweep.py --spi-ladder 0.5,2,8,16 --secs 60 --dry-run
python operator_m04_sweep.py --spi-ladder 0.5,2,8,16 --secs 60
python operator_m04_report.py data/m04_<stamp>
```

The report ends with the answer:

```
T3: clean SPI clocks ['0.5M', '2M', '8M']
    fastest clean = 8 MHz -> ADOPT 2 MHz (one step back)
```

**Adopt that number** and use it for everything after. Reference point: the
ADS1263 runs this same harness at 500 kHz, and SCLK is the rig's primary EMI
aggressor into the laser channel — so faster is not automatically better once
sensors are attached.

## Step 9 — the full qualification sweep

Edit `profiles/qualify.json` so the non-T3 cells use the clock Step 8 adopted, then:

```
python operator_m04_sweep.py --profile profiles/qualify.json --dry-run
python operator_m04_sweep.py --profile profiles/qualify.json      # ~25 min
python operator_m04_report.py data/m04_<stamp>
```

Covers **T3** (clock), **T4** (CRC soak), **T5** (rate), **T7** (noise) and
**T8** (DC accuracy) — 15 conditions.

**T7 is the test that can kill the whole idea** — measured noise must be within
2× of Table 7-1 (2.39 µV rms at gain 1 / OSR 8192), and all four channels
within 2× of each other. If it comes in far worse, stop and report before
doing any integration work.

### T8 inside that sweep — DC accuracy with no external hardware

The chip has an **internal DC test signal** of 2/15 × FSR that auto-scales with
gain, selected through each channel's input mux (§8.3.9). So T8 needs no bench
supply and no divider: the expected value is exact, and it exists in **both
polarities** — which is what makes it test *sign extension*, not just scaling.

| Condition | Mux | Expect |
|---|---|---|
| `t8_dc_pos` | test+ , gain 1 | **+160.0 mV** |
| `t8_dc_neg` | test− , gain 1 | **−160.0 mV** |
| `t8_dc_pos_g2` | test+ , gain 2 | **+80.0 mV** (signal follows the gain) |
| `t7_shorted` | internal short | ~0 V — a *cleaner* noise floor than the EVM jumpers, with the 1 kΩ resistors and external pickup removed |

The report checks the measured mean against those, and raises a **separate
`dc_sign` failure** when a channel reads the wrong polarity, so a broken sign
extension is named rather than hidden inside a large percentage error.

Tolerance is a deliberately loose **2%**: the datasheet calls the signal
"nominally" 2/15 × VREF and gives it no tolerance of its own, so a tight bound
would be judging an unspecified internal divider. T8's job per plan §7 is to
confirm `lsbVolts()` scaling and sign extension, which 2% does decisively.
Absolute accuracy against the REF7050 is a Stage-2 question.

By hand, without the sweep:

```
mux all 2      ->  [CFG] ch0 mux=2 expect=160.0000 mV   (x4)
mux all 3      ->  [CFG] ch0 mux=3 expect=-160.0000 mV  (x4)
mux all 0      ->  back to the real inputs
```

## Step 10 — reset recovery (T9)

With the board streaming, issue:

```
rst
```

**Expect:**
```
[RST] OK id=0x2400 status=0x0500 reset_bit=1 clock 0x0F1A->0x0F0E
[CFG] osr=8192 rate=500.00 SPS
```

Check all four:

- [ ] `reset_bit=1` — STATUS bit 10, set by a genuine reset
- [ ] `clock 0x0F1A->0x0F0E` — registers actually returned to defaults
- [ ] `[STATUS]` resumes within a second or two, `drdy` advancing, `rate` ≈ 500
- [ ] **no power cycle was needed**

**If CLOCK still reads `0x0F1A` after the reset**, the pulse was a **SYNC, not a
reset** — the firmware prints an explicit warning for this. SYNC/RESET is one
pin doing two jobs separated only by pulse width: ≥2048 t_CLKIN (250 µs) resets,
1–2047 t_CLKIN synchronises and leaves the configuration intact while looking
perfectly healthy. The driver holds the line low 1 ms to sit clear of that
boundary, so this warning firing means something is wrong with the pin or the
clock, not with the choice of width.

**Why this test matters:** the ADS1263 could not recover from a wedged state
without a power cycle — that is what the `adcreset` command in the CC fork
exists to work around, and what the "crc storm = skipped EVM power cycle"
failure is about. If the ADS131M04 recovers from a pin reset alone, that is a
real operational improvement and should be recorded in
`Firmware_ADS131M04Test_PIO/STATUS.md`.

**After a reset the mux and gain are back at defaults** — the firmware re-applies
only the OSR. Re-issue any `mux` / `gain` you were using before continuing.

## Step 11 — what happens next

- All green → commit the sweep folder (captures are tracked on purpose) and
  update `Firmware_ADS131M04Test_PIO/STATUS.md` with the adopted SPI clock and
  the measured T7 numbers.
- Then, and only then, plan Stage 2: build the ÷6 divider board (plan §3.1) and
  connect real sensors.
- **T6 (DRDY count) has no automated host check** — read it by hand from
  `[STATUS]`: under DRDY gating `drdy` counts conversions consumed, so it should
  advance by exactly `rate` each second. T8 and T9 are covered by Steps 9 and 10.

---

## Quick failure table

| You see | Most likely |
|---|---|
| `id=0x0000` / `0xFFFF` | SPI wiring, EVM unpowered, grounds not common — read TP1 and TP2 first (Step 1b) |
| `NOT FOUND` at boot, never attaches, TP1 + TP2 both good | Real fault. The re-probe rules out a late supply — work the Step 3b elimination. **Not a dead part** — the TI GUI reads this part fine (Step 3) |
| Board silent after boot, `[STATUS]` never appears | **Not a wedge.** `Ethernet.begin()` blocks ~60 s with no link (first `[STATUS]` at `up=61` unplugged vs `up=3` plugged). Wait, or plug the cable in |
| `attached late` on every boot | 5 V is coming up after the H7 — reorder, not a fault (Step 1b) |
| TP2 ≈ 1 V instead of 3.3 V | U1 has no input: 5 V is not on `EVM_RAW_5V`, or JP8 is fitted. You are reading backfeed, not a rail |
| `id` good, `rate=0`, `drdy` frozen | **CLKIN absent** — JP6 not fitted, or JP5 fitted |
| `crc_err` climbing with clock | SPI too fast for the harness — back off one step |
| `crc_err` climbing at *every* clock | M4 not idled and fighting for the bus; or a marginal ground |
| T1 passes, T2 fails | **Known OPEN as of 2026-08-30 — read STATUS.md before theorising.** Writes land, but acks and ~40 % of reads come back as the NULL response (`0x05xx`). Two tidy explanations were proposed and both were disproved the same day (a CLOCK-resync effect; the frame pair not fitting between conversions). The key fact: **the DOUT CRC does not validate DIN**, so a clean frame carrying the NULL response is indistinguishable from a good answer — which is why no retry strategy helps. Next step is the scope, not another theory |
| Every register reads the same value | You are reading STATUS as a NULL response — the command is not reaching the device (§8.5.1.10.1). `0x050F` is STATUS with `RESET=1`, 24-bit words, all four DRDY set |
| Every register reads the expected value **right-shifted by one bit** | The SPI peripheral is in mode 0, not mode 1 — see Step 3c. `0x050F` appearing as `0x0287` is the signature |
| `[STATUS]` stops, `COM8` still enumerates, reads throw `ClearCommError` | USB-CDC wedged by the sample stream. Force-pull the USB; nothing else clears it |
| Noise perfect, `rate` wrong | a frozen converter reads as *perfect* noise — trust `rate` |
| `udp_on=1` but no samples arrive | PC NIC not on `169.254.245.x`, or a firewall on UDP 7777 |
| Host warns and falls back to serial | the flashed image is `portenta_m7_usb` (no UDP) |
| T8 `dc_sign` fires | sign extension broken in the driver — `sext24()` |
| T8 `dc` off by a clean ratio (2×, 6×…) | `lsbVolts()` / FSR wrong for that gain |
| T9 warns "may have been a SYNC" | SYNC/RESET pulse landed under 2048 t_CLKIN, or the pin is not reaching the EVM |
