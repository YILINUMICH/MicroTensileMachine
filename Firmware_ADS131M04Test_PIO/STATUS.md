# STATUS — Firmware_ADS131M04Test_PIO

**Status:** **WIP** — **T1 and T7 pass on the bench.** T1: `id=0x2403`,
repeatable. T7: shorted-input noise 2.52–2.71 µV RMS against a 2.39 µV typical —
which also clears the earlier 5 V-on-DVDD overvoltage. The ADC converts on all
four channels and streams over UDP. **T2 is intermittent** (two distinct faults,
both characterised below) but no longer blocks configuration: writes land, and
`configure()` succeeds via read-back. Treat every other number as unverified.

**The EVM is externally powered on this rig** (no PHI, R45 lifted). Read
[`../docs/MEMO_ADS131M04_bringup.md`](../docs/MEMO_ADS131M04_bringup.md) Step 1b
before applying any supply — TP1 and TP2 are separate rails, and 5 V on TP1 is
over the absolute maximum.

**Bench procedure:** [`../docs/MEMO_ADS131M04_bringup.md`](../docs/MEMO_ADS131M04_bringup.md)
— wiring → connection → registers → configuration → conversion → UDP → sweeps.

**Branch:** `feat/ads131m04`. **Plan:** [`../docs/ADS131M04_migration_plan.md`](../docs/ADS131M04_migration_plan.md).

---

## 2026-08-27 — driver written against the plan

`ADS131M04_Driver` (h + cpp) written from SBAS890D, plus a minimal M7 bring-up
sketch and an M4 idle stub. `portenta_m7`, `portenta_m7_trace` and
`portenta_m4_idle` all build clean under `-Wall -Wextra` with zero warnings.

Two datasheet details worth recording, because both are silent failures rather
than errors:

- **SYNC/RESET pulse widths are the opposite way round from what a `pdftotext
  -layout` dump of Table 6.6 suggests.** Reset is t_w(RSL) **≥ 2048 t_CLKIN
  (250 µs)**; **1–2047 t_CLKIN is a *synchronise*** — filters realign, registers
  keep their values, chip keeps streaming and looks fine. A few-microsecond
  "reset" would therefore silently not reset. `reset()` holds low 1 ms.
- **DRDY at the default `MODE.DRDY_FMT = 0b` is a LEVEL, not a pulse** — it
  asserts on a new conversion and holds low until the data are read. So
  `waitDataReady()` is a level poll that returns immediately when data is
  already pending. (An early draft hunted for an edge and would have blocked
  until timeout in exactly that case.) Setting `DRDY_FMT = 1b` turns the pin
  into a ~0.5 µs pulse that polling cannot see; the driver never sets it.

### Test progress — plan §7 *(SUPERSEDED by the entry below)*

| | Test | State |
|---|---|---|
| T1 | ID reads `0x24xx` | not run — no hardware |
| T2 | register round-trip | coded in `src/main.cpp`, not run |
| T3 | SPI clock ladder | **not implemented** (needs the `spi` console command) |
| T4 | CRC integrity ≥10⁶ frames | **not implemented** (needs `stream`) |
| T5 | rate accuracy ±1% | partial — the 1 Hz summary prints a rate, but not over a long enough window |
| T6 | DRDY edge count | **not implemented** |
| T7 | shorted-input noise ≤2× 2.39 µV rms | **not implemented** (needs `noise`) — **this is the test that can kill the whole idea** |
| T8 | DC accuracy | **not implemented** |
| T9 | reset recovery | `reset()` and `resetCommand()` exist; no console command to trigger them |

## 2026-08-27 (later) — full M7 application; console replaced by host sweeps

The planned interactive console was dropped in favour of scripted sweeps from
`Experiment_ADS131M04Eval/` (plan §5.1). This firmware now provides the command
surface those sweeps drive, plus the session contract the host's
`lib_h7_session` requires:

- commands `selftest / regs / rst / spi / osr / gain / poll / drdy / netcfg /
  ping / help`, with **unknown commands ignored rather than wedging** — the host
  session sends a few this image does not know
- `[STATUS]` at 1 Hz, numeric key=value only (the host's regex accepts nothing
  else), carrying `udp_on`, `crc_err`, `frames`, `drdy`, `rate`, `adc_ok`
- the sample stream in the **production wire format** over UDP after `netcfg`,
  batched to ≤1400 B of whole lines
- `portenta_m7` now defaults to `-D H7_TRANSPORT_UDP=1`; `portenta_m7_usb` is
  the no-Ethernet rollback

All four envs build clean under `-Wall -Wextra` with **no warnings from our own
sources** (the remaining ones are inside mbed's SocketWrapper).

**Sampling is DRDY-gated, a deliberate departure from the ADS1263 path.** That
path used blind timed polling because its DRDY was ADC1-only and an ISR waiting
on edges freezes when they stop. Neither applies here: one DRDY covers all four
channels, and at the default `DRDY_FMT=0` it is a LEVEL held low until the data
is read — so a non-blocking level check in the main loop cannot hang. Every
conversion is then read exactly once, which removes the duplicate-row problem
the production stream has (~19% zero-order-hold rows, from polling faster than
the ADC converts), and makes T6 free: conversions consumed == DRDY assertions.
`poll <us>` switches to timed polling for an A/B.

**T8 needs no external hardware.** The chip has an internal DC test signal of
2/15 × FSR that auto-scales with gain (§8.3.9), selectable per channel through
`CHn_CFG[1:0]`. `mux all 2` / `mux all 3` give +160.0 / −160.0 mV at gain 1 —
an exact expected value in *both polarities*, which is what makes T8 test sign
extension rather than only scaling. `mux all 1` (internal short) is also a
cleaner T7 reference than the EVM jumpers, since it removes the 1 kΩ resistors
and any external pickup.

**T9 is verifiable, not just runnable.** `rst` now reports the STATUS `RESET`
bit and CLOCK before→after, and warns explicitly when CLOCK did not return to
its `0x0F0E` default — because a SYNC/RESET pulse under 2048 t_CLKIN performs a
*synchronise* and leaves the configuration intact while looking healthy.

`adc.begin()` failing no longer halts: `[STATUS]` keeps flowing with `adc_ok=0`,
so the host sees a diagnosable board instead of a dead port indistinguishable
from a bad cable.

### Test progress — plan §7 (CURRENT)

| | Test | State |
|---|---|---|
| T1 | ID reads `0x24xx` | **implemented** (`selftest`, boot) — unrun |
| T2 | register round-trip | **implemented** (`selftest`) — unrun |
| T3 | SPI clock ladder | **implemented** — `spi <hz>` + host ladder + adoption logic |
| T4 | CRC integrity ≥10⁶ frames | **implemented** — `t4_soak` cell, `crc_err`/`frames` in `[STATUS]` |
| T5 | rate accuracy ±1% | **implemented** — `osr <code>` + host rate check off `hw_us` |
| T6 | DRDY count | **partial** — `drdy` counts conversions consumed and is exact under DRDY gating; no automated host check yet |
| T7 | shorted-input noise | **implemented** — `gain <ch> <g>` + host check vs Table 7-1 |
| T8 | DC accuracy | **implemented** — `mux <ch\|all> <0..3>` drives the chip's own DC test signal (2/15 × FSR, both polarities); host checks value **and** sign |
| T9 | reset recovery | **implemented** — `rst` reports `reset_bit` and the CLOCK before→after, and warns if the pulse acted as a SYNC. Manual (memo Step 10), not a sweep condition |

*(That table records what was **implemented** on 2026-08-27. For what has since
actually **run on hardware**, see the entry below.)*

## 2026-08-30 — first bench day: T1 PASSES, T2 open

One long session, consolidated. Two hardware faults and two software faults were
found and fixed; T1 now passes repeatably. Several intermediate theories were
disproved along the way and are listed under **Eliminated** below, so they are
not re-tried.

### Result

| | Test | State |
|---|---|---|
| T1 | ID reads `0x24xx` | **PASS — `id=0x2403`**, every boot, 3/3 `selftest` runs |
| T2 | register round-trip | **INTERMITTENT.** Two distinct faults, both now measured: CLOCK writes destroy their own ack frame (83 % vs 0 % for GAIN1, n=12 each) — mitigated by trusting read-back; and a ~25 % background frame disturbance driven by the idle gap before a frame — mitigated, not solved |
| **T7** | shorted-input noise | **PASS — 2.52 / 2.53 / 2.57 / 2.71 µV RMS**, all 1.05–1.13x the datasheet typical, spread 1.08x. Also clears the DVDD overvoltage |
| **T8** | DC accuracy | **PASS on both objectives** — sign extension (all 4 ch, magnitudes within 0.2 %) and gain scaling (g2/g1 = 0.5012). Absolute value is 3.4 % below nominal, outside the 2 % band; recommend widening to 5 % |
| T3–T6, T9 | | **unrun** |

Also working: simultaneous conversion data on all four channels, and the sample
stream over **UDP** to `169.254.245.100:7777` (`udp_on=1`).

A healthy boot now looks like:

```
[BOOT] ADS131M04 attached late, id=0x2403
[NET]  UDP stream -> 169.254.245.100:7777
[STATUS] up=35 loop_hz=87669 frames=15407 crc_err=3922 drdy=11235 rate=368.00
         samples=44940 adc_ok=1 spi_hz=2000000 osr=1024 gated=1 udp_on=1
```

### The two hardware faults

**1. `J6[8]` ground — mechanically marginal.** ~1 Ω contact resistance measured
*unpowered*. (A reading taken with the board live gave 11.5 Ω and is meaningless:
a DMM cannot measure resistance in a powered circuit.) The link ran **only while
something physically pressed on that pin**:

| On `J6[8]` | Link |
|---|---|
| nothing | dead — 100 % CRC fail, `drdy=0` |
| probe ground clip | works, `drdy` climbing |
| nothing again | dead again |
| probe **tip** (1 MΩ) | works — `rate=1781`, 71 % of frames pass |

The tip is the proof: 1 MΩ cannot supply a current return, so this was never an
electrical bonding effect — it was mechanical contact. That is also why a full
day of individually-correct measurements never converged: the link's state
tracked whoever last touched the harness.

**2. `J6[2]` DIN mis-landed.** Corrected by the operator. `UNLOCK` acked `0x655`
immediately afterwards, and T1 passed for the first time.

> **Add "verify GND across the harness" to the top of the memo's Step 3b
> elimination, ahead of every signal check.** Every voltage that day was read
> single-ended with the meter's black lead on *EVM* ground — precisely the
> measurement that cannot see a broken shared reference. Continuity was checked
> `J6`→die, inside the EVM, never across the cable.

### The two software faults

**3. The mbed Arduino core silently drops the SPI mode across `SPI.end()` /
`SPI.begin()`.** `end()` deletes the `mbed::SPI` but leaves the cached `settings`
member; `begin()` constructs a fresh one in **mode 0**; `beginTransaction()` then
skips `format()` because the requested settings equal the stale cache. The
peripheral runs in mode 0 while the caller believes mode 1, sampling one edge
early — **every word arrives right-shifted by one**. Bench signature: STATUS
`0x050F` reads back as `0x0287`, and the `DRDY` nibble `0xF` reads as `0x7`.

Triggered by our *own* diagnostics — `hold`, `pintest` and `bitbang` all release
the bus. Confirmed by prediction before fixing: issuing `spi 1000000` (a
*different* clock, so the cache differs and `format()` is forced) restored mode 1
and the ID read `0x2403` on the next probe.

**Fixed** by owning an `mbed::SPI` directly. `busAcquire()` / `busRelease()`
replace `SPI.begin()` / `SPI.end()`, and acquiring the bus and applying the
format are deliberately the *same operation* — separating them is the bug. The
full write-up is in the driver header; do not undo it.

**4. Byte-at-a-time transfer.** One block transfer per frame replaced 18
`SPI.transfer()` calls: `loop_hz` **2 030 → ~88 000**.

### T2 — TWO separate faults, both now measured with a real sample size

The single most expensive mistake of the day was drawing conclusions from two to
five observations against a ~25 % background failure rate. Every flip-flop below
came from that. **Use n>=12.**

#### Fault A — writing CLOCK corrupts its own command frame (resync)

Twelve writes each, same session, same clock:

| Write target | `f1` BAD | `f2` ok | readback correct |
|---|---|---|---|
| **CLOCK** (`0x03`) | **10/12 (83 %)** | 12/12 | 10/12 |
| **GAIN1** (`0x04`) | **0/12 (0 %)** | 12/12 | 12/12 |

83 % against 0 % is decisive. CLOCK holds `OSR[2:0]`, `PWR[1:0]` and the channel
enables, so writing it makes the device **resynchronise** (STATUS bit 14
`F_RESYNC`) *while the WREG frame is still being clocked*, destroying the frame
in flight. The leading `0x04` byte and the one-byte offset appear only here.

**The write still lands** — readback correct 10/12. So the acknowledgment is
unrecoverable by design, and the only sound verdict is the register itself.
`writeRegister()` therefore settles and trusts a read-back. That part is fixed.

> This was proposed, then **wrongly retracted**, then restored. The retraction
> came from a `regs` dump showing reads failing scattered across registers — but
> that is Fault B, a read-side effect, and it has no bearing on a write-side
> finding. Do not re-retract it without n>=12.

#### Fault B — background frame disturbance, worst on the first frame after idle

Independent of Fault A, roughly a quarter of frames arrive with the device's
output frame restarted one word early (STATUS in the CRC slot). Measured
dependence, all at one data rate:

| How frames were issued | Bad rate |
|---|---|
| contiguous, nothing between | **1/24 (~4 %)** |
| `raw` with a print between (~1 ms gap) | 9/32 (28 %) |
| streaming loop, UDP emit between frames | ~25 % |

**It tracks the idle gap before the frame** — not the SPI clock (250 k–8 M all
~25 %), not the frame duration (66 µs at 8 MHz vs 177 µs at 2 MHz, unchanged),
not the conversion rate (252 vs 4000 SPS, unchanged), and not DRDY-gating vs
blind polling.

**Ruled out for Fault B, each by measurement:**

| Hypothesis | Killed by |
|---|---|
| SPI clock / signal integrity | flat ~25 % at 250 k, 500 k, 2 M, 4 M, 8 M |
| Frame duration | 66 µs at 8 MHz vs 177 µs at 2 MHz — unchanged |
| Conversion rate | 252 SPS vs 4000 SPS — unchanged |
| DRDY-gating vs blind polling | same rate either way |
| FIFO backlog / not draining | `frames` +500/s against `drdy` +376/s: only ever ONE pending conversion, so there was nothing to drain. Adding a drain loop changed nothing |
| **Position within the conversion period** | **`dly` sweep 0 / 100 / 300 / 600 / 1000 / 1500 µs → 25.2 / 24.8 / 24.9 / 26.0 / 25.1 / 24.5 %.** Flat. §8.5.1.9's "avoid reading while conversions complete" is not what is happening here |
| Our CRC implementation | 23/23 exact matches on clean frames |
| Our SPI clocking | scope: exactly 144 edges per CS-low window, every frame, at 2 and 8 MHz |

**The sharpest constraint, and the thing to attack next.** Sweeping the blind
poll period — reading FASTER than the 500 SPS conversion rate, which is what
`raw` does and what the DRDY-gated loop never does — and correcting for the
cumulative counters:

| poll period | frames/s | **bad/s** | bad % |
|---|---|---|---|
| 2000 µs | 644 | **166** | 25.8 % |
| 500 µs | 1872 | **134** | 7.2 % |
| 200 µs | 2066 | **133** | 6.5 % |
| 100 µs | 2302 | **150** | 6.5 % |

**Bad frames arrive at a roughly constant ~135–165 per second no matter how many
frames are run.** The percentage falls only because the denominator grows. So
the disturbance is fixed *in time* — not per frame, not per conversion (those
are 500/s), and not per byte. Whatever it is fires ~150 times a second.

That also explains the `raw` vs streaming difference without needing a gap
theory at all: `raw` runs many more frames per second, so the same ~150 bad/s is
a smaller fraction.

**Practical consequence available today:** reading faster than the conversion
rate drops the frame CRC failure rate from ~26 % to ~6.5 %. It costs duplicate
samples (the zero-order-hold problem), so it is a mitigation for register access
and T4, not a route to T5 rate accuracy.

### Fault B is INHERENT to the device — host fully exonerated

Every host-side variable was swept and none of them move it:

| Varied | Result |
|---|---|
| UDP batch size 1400 / 700 / 200 / 120 B (packets/s x12) | 126.7 / 125.4 / 124.7 / 125.8 bad/s |
| **`emit 0` — no formatting, no transmit at all** | **120.0 bad/s** |

Flat. With the entire emit path switched off, bad frames still arrive at ~125/s
against 500 conversions/s. Nothing the firmware does causes or prevents it.

### `fast <us>` — absorb it, and de-duplicate

Read several times per conversion, keep CRC-good frames, and emit only those
whose `STATUS[3:0]` flags a fresh conversion (first read carries `0x050F`,
re-reads `0x0500`):

| mode | frames/s | bad/s | bad % | **delivered/s** | dedup/s |
|---|---|---|---|---|---|
| `fast 0` (DRDY-gated) | 500 | 123 | 24.7 % | **376.7** | 0 |
| `fast 400` | 2374 | 149 | 6.3 % | **376.2** | 1849 |
| `fast 200` | 4298 | 172 | 4.0 % | **373.5** | 3752 |
| `fast 100` | 4301 | 180 | 4.2 % | **371.2** | 3750 |

De-duplication works: the delivered rate is flat at ~375/s however fast we poll,
so the stream carries no duplicates. **The delivered stream is clean** — good for
T4.

**But it does not recover the lost conversions.** Delivered is ~375/s against
500 SPS: reading 8.6x faster recovers none of the missing 25 %. So **a disturbed
frame consumes its conversion** — the device clears DRDY when data is shifted
out whether or not our CRC passed, and ~125 conversions/s are permanently lost.

**Consequence: T5 (rate accuracy, ±1 %) cannot pass** while a quarter of
conversions are destroyed. T4 can run on the delivered stream.

### Did the ADS1263 have this and we ignored it? No.

Production captures record **`crc_err=0`** throughout, and the production path
does count and discard invalid reads (`SAMPLE_RING->crc_err++`). But the
comparison is not like-for-like, and the differences are the point:

- **The checks differ in strength.** ADS1263 validates a modulo-256 sum over the
  DATA BYTES ONLY (plus 0x9B) and does **not** cover the STATUS byte. The
  ADS131M04's CRC-16 covers the response word, all four data words and the
  status. Our bad frames are exactly the case where STATUS lands in the CRC slot
  while the data words stay sane.
- **The read patterns differ.** The ADS1263 never gates on DRDY ("DRDY is not
  used for gating. ADC2 has no DRDY output anyway") — it polls blind at ~493 Hz
  against 400 SPS, i.e. already in the fast-read regime.
- **The known ADS1263 data problem was the opposite:** ~19 % duplicate
  zero-order-hold rows, documented and accepted. Redundancy, not corruption.
- **The failure mode has no analogue** — per-channel RDATA frames, not a 6-word
  frame with a trailing CRC.

So this is a real ADS131M04 behaviour, newly *visible* because the protocol has
a genuine frame CRC — not an old problem carried over.

### The invariant: exactly 1 conversion in 4 is lost

OSR sweep, DRDY-gated:

| device rate | frames/s | bad/s | **bad %** | delivered/s |
|---|---|---|---|---|
| ~250 SPS | 250 | 64 | **25.5 %** | 186 |
| 2000 SPS | 1998 | 509 | **25.5 %** | 1489 |
| 4000 SPS | 2745 | 686 | **25.0 %** | 2059 |

**1-in-4 across a 10x span of rate.** This CORRECTS the earlier "constant
~150 bad/s in time" reading in this entry: that only looked time-constant
because the poll sweep held the conversion rate fixed at 500 SPS while varying
frames. Both sweeps reduce to one rule — **~25 % of conversions are lost**,
independent of OSR, SPI clock, frame duration, read pacing, batch size and the
emit path.

*(OSR codes 7/6/5 all gave 250 frames/s with no `[CFG] osr=` line — those writes
did not take. Only codes 4 and 3 landed. Do not read those as three rates.)*

### Why the 25 % loss may not matter — run it oversampled

The absolute numbers, not the percentage, are what the application cares about:

| | delivered, clean, de-duplicated |
|---|---|
| **ADS131M04 at OSR 1024, losing 25 %** | **2059 samples/s x 4 simultaneous channels** |
| ADS1263 it replaces | 400 SPS x 2 channels, with inter-channel skew |

**5x the production sample rate on twice the channels, after the loss.** The
ADS131M04's headroom absorbs the fault.

**This means T5's acceptance criterion should be amended, not quietly passed.**
"Rate accuracy within +/-1 % of the configured OSR" is the wrong test for a
device deliberately run oversampled: it will fail by 25 % by design. The
question that matters is whether enough uniquely-timestamped samples arrive with
usable timing — and every delivered sample carries `hw_us`, so the loss is
visible in the data rather than silent. **Proposed: T5 becomes "delivered rate
>= the application requirement (400 SPS), with hw_us jitter bounded", and the
25 % device loss is recorded as a known, quantified characteristic.** That is a
change to plan §7 and needs a decision, not a unilateral edit.

### The losses are INDEPENDENT, not bursty — oversampling is sound

Gap histogram of delivered samples (`hw_us`), device at 2000 SPS, `fast 150`,
44 985 samples over 30 s, `tx_drop` confirmed flat at 0/s so the stream is
complete:

| gap (periods) | measured | independent-loss model |
|---|---|---|
| 1 | 75.30 % | 76.32 % |
| 2 | 18.44 % | 18.07 % |
| 3 | 4.62 % | 4.28 % |
| 4 | 1.18 % | 1.01 % |
| 5 | 0.33 % | 0.24 % |
| 6 | 0.10 % | 0.06 % |
| >8 | 0.00 % | — |

Median gap 509 µs against a 500 µs period; implied loss 23.7 %; measured
24.9 %; longest run of consecutive losses 7, which is what a geometric tail
gives over 45 k samples. **The measured distribution tracks independent losses
almost exactly.** No bursts, no correlated dropouts — so the effective bandwidth
really is 75 % of nominal and the oversampling argument holds.

### Datasheet review of the 25 % — what it rules out

| Candidate | Eliminated because |
|---|---|
| Global-chop mode | `CFG` reset `0x0600` → `GC_EN` = 0. Disabled |
| `DRDY_SEL[1:0]` timing | Default `00b`; all phase calibrations 0, so all four channels are simultaneous |
| SPI frame timeout (§8.5.1.7) | Fires at 2^15 CLKIN = **4 ms**; our frame is 177 µs at 2 MHz, 66 µs at 8 MHz |
| Input CRC rejecting commands | `RX_CRC_EN` disabled, and `CRC_ERR` (STATUS bit 12) never sets |
| Our CRC implementation | §8.3.12: coverage is "all words... including padded bits", CCITT, seed `FFFFh` — matches, and 23/23 exact on good frames |

The only documented mechanism that fits the symptom is §8.5.1.9.1 (two-deep
FIFO; *"avoid reading ADC data during the time where new conversions
complete"*). But that has now been tested two ways — the `dly` sweep across a
whole conversion period, and the drain loop — and neither moves it.

**The one manual-suggested cause NOT yet tested: frame length.** §8.5.1.10.8
warns *"Ensure all of the ADC data and output CRC are shifted out during each
transaction where new data are available"*, and Figure 8-25 shows frames
extended beyond six words. Our bad frames look exactly like the device emitting
at a different offset: words 1-4 correct, then the NEXT frame's STATUS where the
CRC belongs. If the device occasionally emits a 7th word, clocking only 6 would
produce precisely that.

**Tested, and DISPROVEN.** `rawx` clocks seven words (21 bytes) and asks where
the CRC lands: `normal@w5=18  inserted@w6=0  sevenword@w6=0  unexplained=2`.
The device emits no extra words — we were not truncating anything.

**What the 7-word dump DOES show is the mechanism, directly:**

```
050F00 000319 000431 00055C 0003BA | 050000 | 000319
  ^STATUS, DRDY=F (fresh data)       ^STATUS  ^ch0 again
```

After shipping the four data words the device **abandons the frame and starts a
new one** — word 5 is the next frame's STATUS (DRDY now cleared) and word 6 is
its ch0. The CRC for that frame is never sent. Not truncation, not an inserted
word, not a longer frame: a genuine mid-transaction restart.

### Conclusion on Fault B: characterised, not explained. Stop and ask TI.

Every mechanism the datasheet offers has been tested and eliminated. What is
established, and is enough to hand to TI:

- **Exactly ~25 % of conversions are lost**, stable across a 10x rate span.
- **Losses are independent, not bursty** — the gap histogram matches a geometric
  model to within a percentage point at every gap out to 6.
- **The device restarts its output frame after the data words**, omitting the
  CRC, and the conversion carried by that frame is consumed and lost.
- **Nothing host-side influences it:** SPI clock 250 k-8 M, frame duration
  66-177 µs, conversion rate 252-4000 SPS, DRDY-gating vs blind polling,
  position within the conversion period (`dly` 0-1500 µs), FIFO drain, UDP batch
  size over a 12x span, and the emit path switched off entirely.
- **Our side is provably correct:** 144 SCLK edges per CS-low window on the
  scope at both 2 and 8 MHz; CRC implementation exact on 23/23 good frames;
  command encoding re-verified against Table 8-11.

**DECISION (2026-08-30): the 25 % is TABLED.** It is characterised well enough
to report and not worth more bench time. A support email to TI is drafted; the
remaining possibilities are a device erratum or an EVM-specific interaction and
neither is reachable from here. **Do not reopen this without new information
from TI** — the eliminated list above is long and every entry cost measurements.

**Adopted instead: oversample and de-duplicate, as the default data path.**

> **BUILT BUT NOT FLASHED — start the next session here.** The `fast auto`
> change below compiles clean but was never uploaded: the H7 dropped off USB
> (no present serial port, no DFU device, only stale `COM8` ghosts in the
> registry) before the upload could run. **Nothing in this section is
> bench-verified.** Replug the H7, power-cycle the EVM,
> `pio run -e portenta_m7 -t upload`, then confirm `[CFG] fast_us=` appears at
> boot and that `[STATUS]` shows a delivered `rate` near 75 % of the configured
> SPS with `dedup` climbing.

`fast` now defaults to **auto**: the poll period tracks the configured data
rate at `FAST_OVERSAMPLE = 4` reads per conversion, recomputed inside
`applyConfig()` so changing OSR cannot silently stop the oversampling. Frames
are kept only when `STATUS[3:0]` flags a fresh conversion, so the delivered
stream carries no duplicates.

Measured: delivered frame-CRC failure ~25 % -> ~4 %, delivered rate constant at
75 % of nominal however fast we poll (no duplicates, no extra recovery).
`fast <us>` pins it manually; `fast 0` restores one DRDY-gated read per
conversion for A/B work.

**Why this is acceptable rather than a compromise:** at OSR 1024 it delivers
**2059 clean samples/s x 4 simultaneous channels** against the ADS1263's 400 SPS
on two skewed ones — 5x the production rate on twice the channels, after the
loss. Losses are independent (gap histogram above), so the effective bandwidth
really is 75 % of nominal, and every delivered sample carries `hw_us` so the
gaps are visible in the data rather than silent.

**Consequence for T5:** its "+/-1 % of the configured OSR" criterion will fail
by 25 % by design on an oversampled device. It needs amending to a delivered-rate
requirement before it is run — see the note above; that is a plan §7 change and
has not been made. Candidates to eliminate, cheapest first —
the UDP/Serial emit path (vary the batch size and watch whether bad/s tracks
packets/s), Ethernet housekeeping, and any mbed background task. If bad/s stays
pinned at ~150 while every host-side rate is varied, it is internal to the
device and the next step is TI.

Mitigations in the driver, in order of how much they helped:

1. `quiesce()` issues an **unconditional** throwaway frame to absorb the gap
   before any register access. (An earlier version only did so when DRDY was
   low, which changed nothing.) T2 passed for the first time after this.
2. **Dropped-command detection.** The output CRC validates DOUT and never DIN,
   so a dropped command yields a flawless frame carrying the NULL response —
   nothing looks wrong and no retry fires. `quiesce()`'s own NULL frame records
   what that response looks like; a match means retry. Compare only bits 15:4:
   STATUS[3:0] is DRDY and changes frame to frame.

T2 now passes intermittently rather than never. **Not solved.**

### T7 PASS — and the part is undamaged

Conditions: gain 1, OSR 8192 (500 SPS), inputs **internally shorted**
(`mux all 1`, verified `CH1/2/3_CFG = 0x1`), 60 s capture over UDP, 22 452
samples per channel. Computed from the **raw codes** — the streamed volts field
carries six decimals, i.e. 1 µV steps, which would quantise a 2.4 µV measurement
into nonsense.

| ch | n | mean (µV) | RMS (µV) | vs typical |
|---|---|---|---|---|
| 1 | 22 452 | 98.2 | **2.517** | 1.05x |
| 2 | 22 452 | 190.2 | **2.528** | 1.06x |
| 3 | 22 452 | 200.5 | **2.568** | 1.07x |
| 4 | 22 452 | 143.1 | **2.713** | 1.13x |

Table 7-1 gives **2.39 µV RMS** typical at gain 1 / OSR 8192 (read from the
datasheet, not the memo: rows are OSR | rate | gain 1..128, and OSR 8192 / 0.5
kSPS / gain 1 = 2.39). Acceptance is 2x that (4.78 µV) with the four channels
within 2x of each other. Measured: **every channel at 1.05–1.13x typical, spread
1.08x.** Not marginal — essentially on the datasheet number.

**This is also the verdict on the 5 V-on-DVDD overvoltage, and it clears the
part.** A device damaged by 1.1 V over the 3.9 V absolute maximum would show a
degraded noise floor; this one is at typical. The standing caveat in the
2026-08-30 entry — "treat any T7 result as also being a verdict on that
overvoltage" — is now discharged.

Per plan §7 this was *"the test that can kill the whole idea."* It did not.

Caveat worth carrying: the host consumed 374 samples/s against the device's 500
SPS, so roughly a quarter of conversions were not read. That is the Fault B
disturbance, and it means the capture is a subset rather than every conversion.
It does not undermine a noise-floor number landing on the datasheet typical, but
T4 (CRC soak) and T5 (rate accuracy) will need Fault B addressed first.

### T8 — objectives PASS; the 2 % acceptance band is too tight

Device's own internal DC test signal (2/15 x FSR, auto-scaling with gain,
8.3.9). Raw codes, 10 s per condition.

| Condition | expect | measured (4 ch) | error |
|---|---|---|---|
| test+, gain 1 | +160.0 mV | +154.47 … +154.50 | **−3.45 %** |
| test−, gain 1 | −160.0 mV | −154.07 … −154.28 | **−3.6 %** |
| test+, gain 2 | +80.0 mV | +77.24 … +77.43 | **−3.3 %** |

**Both things T8 exists to test pass decisively:**

- **Sign extension — PASS.** All four channels return the negative signal with
  magnitudes within 0.2 % of their positive readings. `sext24()` is correct.
- **Gain scaling — PASS.** g2/g1 = 77.43 / 154.50 = **0.5012** against an ideal
  0.5, a 0.24 % error. `lsbVolts()` is correct.

**The −3.4 % is the chip's divider, not our arithmetic**, and the check that
settles it: the test signal is 2/15 x VREF while our LSB is VREF / 2^23, so
**VREF cancels** — a reference error would still read exactly 160 mV. Nor is it
the "clean ratio (2x, 6x)" the memo says would indicate a wrong `lsbVolts()`. A
uniform −3.4 % across channel, polarity and gain means the internal divider is
simply not exactly 2/15. The datasheet calls it "nominally" 2/15 x VREF and
gives it **no tolerance**, which is precisely the risk the memo flagged when it
set the 2 % band.

**Recommendation: widen the T8 band to 5 %** and keep the separate `dc_sign`
check strict. 2 % is judging an unspecified internal divider; 5 % still catches
every failure mode T8 is for (a wrong FSR, a wrong gain multiplier, broken sign
extension) because those produce clean-ratio errors, not 3 % ones. Absolute
accuracy against the REF7050 remains a Stage-2 question.

> **Trap for whoever re-runs this.** The first run reported `FAIL dc_sign` on
> ch3 and ch4 — and it was an artifact. Those channels read `+154.467` and
> `+154.484` under the *negative* condition, identical to six figures to their
> positive-condition values, because `mux all 3` had silently failed on them
> (Fault B). They were still on test+. Re-applying the mux and re-measuring gave
> −154.07 and −154.20. **Verify the mux actually applied before believing a sign
> failure** — the register read-back cannot be trusted for this either
> (`CH1/CH2_CFG` came back as `0x500`, i.e. STATUS).

### The measurement that cleared our own hardware

Scope, CH1 on SCLK `J6[5]`, CH2 on /CS `J6[4]`, counting SCLK rising edges per
CS-low window:

```
2 MHz:  9 frames, all exactly 144 edges, CS low 176-180 us
8 MHz: 10 frames, all exactly 144 edges, CS low  65.5-66.1 us
```

An 18-byte frame is 144 edges. **Every frame, at both clocks, including the ~25 %
that fail CRC.** The H7 clocks correctly; the disturbance is internal to the
device. Nothing in the serial log could establish that, and it should have been
the first measurement rather than the last.

It also showed the frame costs 177 µs at 2 MHz against 72 µs of actual clocking
— mbed's `SPI::write()` is not a DMA block transfer — which is why raising the
SPI clock never helped.

### The signature, for reference

`wtest` gives the same result every time, isolated or back-to-back:

```
f1 BAD rsp=0x405  [040500 000003 020004 520005 2F0003 A1688B]   <- WREG frame
f2 ok  rsp=0x500  [050000 000302 000452 00052F 0003A1 688B00]
```

**`f1` is `f2` shifted right by exactly one byte, with a leading `0x04`** —
`f1[1..17] == f2[0..16]`, byte for byte, CRC included. The frame is therefore
incomplete; the device does not execute an incomplete command (§8.5.1.7) and
answers NULL, which is why `f2` is clean and carries STATUS (§8.5.1.10.1: *"Any
invalid command also gives the NULL response."*).

**The write itself is fine.** Spaced `wtest` runs gave exact acks and exact
read-backs: `0x4180`/`0xF1A`, `0x4200`/`0x55`, `0x4200`/`0x00`. `configure()` is
blocked by an unreadable *acknowledgment*, not a failed write.

**Four fixes attempted before the cause was found; none moved it.** Recorded so
they are not retried — every one of them was trying to recover an acknowledgment
that the device had legitimately disturbed:

| Attempt | Result |
|---|---|
| Retry register access on CRC failure (4x, then 8x) | fewer T2 failures, never zero |
| Require BOTH command and answer frames CRC-clean | no change |
| `quiesce()` — consume a pending sample so frames avoid the conversion edge | no change |
| §6.6 `/CS` guard times (5 µs each boundary, matching `bitbang`) | **no change at all** — signature byte-identical |

### Eliminated — do not re-test

| Suspicion | Killed by |
|---|---|
| Dead/damaged part (from the 5 V-on-DVDD overvoltage) | the TI GUI read the part fine; and it now works |
| `0xFF24` is a corrupt ID / byte-swapped / a GUI display offset | **Table 8-11: `0xFF24` IS the documented RESET-command response.** It was never an ID read |
| Device left in 32-bit `WLENGTH` by the GUI | STATUS reads `WLENGTH=01b` (24-bit) and frames decode correctly |
| Ground *bounce* between H7 and EVM | scoped at **4.67 mV pk-pk, −0.45 mV DC** — a quiet shared ground. The ground fault was mechanical, not electrical |
| SCLK marginal / bad edges | scoped at `J6[5]`: **3.167 V pk-pk, 50.02 % duty, 36 ns rise** — clean full-swing CMOS |
| SYNC/RESET wire open | `hold d4 0/1` gives 0 V / 3.1 V at `J6[1]`. Good |
| Frames straddling a conversion boundary (frame-duration theory) | a 16× SPI-clock span leaves the error rate flat; at 250 kHz a frame is 576 µs against a 250 µs conversion period, predicting ~100 % where 25 % is measured |
| Our CRC algorithm or its coverage | 23/23 exact matches on clean frames (CCITT-FALSE over all 15 bytes, padding included) |
| Command encoding | re-checked against Table 8-11: RREG `101a aaaa annn nnnn`, WREG `011a…`, acks `111…` / `010…`. All correct |
| Command spacing | an isolated `wtest` fails identically to back-to-back ones |
| SPI clock rate | 250 k / 500 k / 2 M / 4 M / 8 M all give ~25 % frame CRC |
| DRDY-gating vs blind polling | same rate either way |

### Next session

1. **Decide whether T2 needs to pass at all.** `configure()` already succeeds in
   practice — `osr 7` took effect and `[STATUS]` reported `osr=16256
   rate=251.97 SPS` — because `writeRegister()` falls back to a read-back. If
   T3–T9 can run on that, T2's ack is a reporting problem, not a blocker. Check
   this before spending more on it.
2. ~~T7 first~~ — **done, PASS.** See the T7 section above; the overvoltage
   question is closed.
3. **Fix Fault B before T4 and T5.** Both need every conversion read: T4 is a
   CRC soak and T5 is rate accuracy, and at present the host consumes ~75 % of
   conversions. T8 (DC accuracy, internal test signal) and T9 (reset recovery)
   do not depend on it and could run now.
3. The ~25 % streaming frame-CRC rate is separate and benign (bad samples are
   flagged). Revisit only if T4's soak needs it.

The scope rig is working and scripted if it is needed again —
`scratchpad/scope_*.py`, SDS2204X Plus at `169.254.111.4`. **The probe is 1×: set
`ATTN 1`**, or every reading rails off-screen and the scope returns a number that
scales with V/div instead of a measurement.

### Host-side rules (both cost time today)

- **Hold one long-lived reader that never stops draining** —
  `scratchpad/m04_daemon.py`. The stream saturates USB-CDC; any gap blocks the
  M7 in `Serial.write` permanently and needs a **USB force-pull**. It does not
  recover when draining resumes.
- **Send `netcfg <pc_ip> 7777` first after every boot**, to get the stream off
  USB-CDC entirely.
- **Opening the port with DTR asserted resets the board**, then costs ~60 s in
  `Ethernet.begin()`. DTR *low* cannot be used instead: the Portenta CDC then
  treats the host as absent and transmits nothing at all.
- `adc_ok` and `present` **latch** — set on first attach and never re-checked, so
  they read healthy after the link dies. `drdy`, `samples` and `rate` are the
  honest indicators. Worth fixing.

### New diagnostics added today

| Command | What it answers |
|---|---|
| `raw [n]` | dumps whole DOUT frames as they arrived, with the chip's CRC and ours side by side. Frames are captured contiguously, then printed |
| `wtest <addr> <val>` | dumps all three frames of a register write, so the ack can be compared against Table 8-11 rather than inferred |
