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
| 3V3 | J15-3 or J15-4 | EVM 3V3 | only if the EVM is not separately powered |

- [ ] **EVM J6[3] (CLK) — leave UNCONNECTED.** Y1 drives it on-board.
- [ ] **JP6 / J13 fitted at `[1-2]`** — selects Y1's 8.192 MHz. **CLKIN is mandatory.**
- [ ] **JP5 NOT fitted** — JP5 powers Y1 *down*.
- [ ] **JP1–JP4 left at the factory `[3-4]`** — grounds every input through
      1 kΩ. That *is* the shorted-input condition the noise test wants.
- [ ] Ethernet: H7 carrier RJ45 → the PC's USB GbE dongle. H7 is static
      `169.254.245.50`; the PC NIC sits on the same link-local segment
      (`169.254.245.100`).

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
| `id=0x0000` or `id=0xFFFF` | Nothing is driving DOUT. SPI wiring, EVM unpowered, or grounds not common. |
| `id=0x24xx` but nothing else works | Link is fine — go on; the fault is downstream. |
| Anything else plausible-looking | Suspect a swapped COPI/CIPO or a CS on the wrong pad. |
| `[BOOT] ADS131M04 NOT FOUND` but `[STATUS]` keeps coming | Intentional — the firmware does not halt, so you can see `adc_ok=0` rather than a dead port you can't distinguish from a bad cable. |

Bits 15:12 of ID are always `0010b` and bits 11:8 are `CHANCNT = 0100b`, so the
**high byte is always `0x24`**. The low byte is "subject to change" — ignore it.

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

Covers T3 (clock), T4 (CRC soak), T5 (rate), T7 (shorted-input noise).

**T7 is the test that can kill the whole idea** — measured noise must be within
2× of Table 7-1 (2.39 µV rms at gain 1 / OSR 8192), and all four channels
within 2× of each other. If it comes in far worse, stop and report before
doing any integration work.

## Step 10 — what happens next

- All green → commit the sweep folder (captures are tracked on purpose) and
  update `Firmware_ADS131M04Test_PIO/STATUS.md` with the adopted SPI clock and
  the measured T7 numbers.
- Then, and only then, plan Stage 2: build the ÷6 divider board (plan §3.1) and
  connect real sensors.
- **T6 (DRDY count), T8 (DC accuracy) and T9 (reset recovery) are not yet
  automated.** T6 is readable by hand from `[STATUS]`'s `drdy` versus `rate`;
  T8 needs a known DC source; T9 is `rst` mid-capture.

---

## Quick failure table

| You see | Most likely |
|---|---|
| `id=0x0000` / `0xFFFF` | SPI wiring, EVM unpowered, grounds not common |
| `id` good, `rate=0`, `drdy` frozen | **CLKIN absent** — JP6 not fitted, or JP5 fitted |
| `crc_err` climbing with clock | SPI too fast for the harness — back off one step |
| `crc_err` climbing at *every* clock | M4 not idled and fighting for the bus; or a marginal ground |
| T1 passes, T2 fails | driver framing (WREG payload slot / response lag), not wiring |
| Noise perfect, `rate` wrong | a frozen converter reads as *perfect* noise — trust `rate` |
| `udp_on=1` but no samples arrive | PC NIC not on `169.254.245.x`, or a firewall on UDP 7777 |
| Host warns and falls back to serial | the flashed image is `portenta_m7_usb` (no UDP) |
