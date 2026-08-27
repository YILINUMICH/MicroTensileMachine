# Plan — evaluate the ADS131M04 as a replacement for the ADS1263

**Status:** PROPOSED 2026-08-27 (branch `feat/ads131m04`). **Owner:** Yilin.
**Scope:** a new `ADS131M04_Driver`, a standalone bench-test firmware to qualify it,
and — only if it passes — a swap into the M4 sampler slot of
`Firmware_SMAConstantCurrent_PIO`. The M7 UDP transport
(`docs/UDP_stream_migration_plan.md`) is kept exactly as it is.
**Rollback:** the whole evaluation lives on `feat/ads131m04`; `feat/udp-stream`
and `main` keep the ADS1263 path untouched. See §11.

Datasheets already filed: [`docs/ads131m04_datasheet.pdf`](ads131m04_datasheet.pdf)
(SBAS890D) and [`docs/ADS131M04_EVM_User_Guide.pdf`](ADS131M04_EVM_User_Guide.pdf)
(SBAU332A). Every clause cited below is from those two.

---

## 1. Why — what the ADS131M04 buys, and what it costs

**The win is simultaneous sampling on four channels.** The ADS1263 is two
independent converters on one die, polled on two independent 2 ms timers, and the
rig has been paying for that in three separate places:

- laser (`src=1`) and load (`src=2`) are acquired at *different instants* and the
  offset is not measured, only assumed small;
- SMA voltage and current (`src=3/4`) are read by the **H7's own 12-bit ADC**,
  sequentially through a mux with a 50 µs settle — so `R = V/I` divides two
  numbers taken at different times, from a converter whose reference sags with
  its own conversion duty (see `MEMORY: adc-conversion-duty-reference-droop`,
  `sma-r-noise-band-split`);
- the two cores stamp their samples off two clocks that differ by ~2.2 s
  (`MEMORY: m7-m4-clocks-differ-by-2s`).

Four channels sampled off one modulator clock, all 24-bit, all delivered in a
single 18-byte SPI frame, collapses the first two of those problems into nothing
and gives two spare channels that could eventually take SMA V and I off the
on-chip ADC entirely (§9, deliberately *not* in the initial scope).

**The cost is dynamic range, and it is not small.** The ADS131M04 has a fixed
**internal 1.2 V** reference and no external reference pin: full scale is
**±1.2 V / gain** (§8.3.3, Table 8-1). The ADS1263 runs today off an external
REF7050 at **5.000 V**, i.e. ±5 V at gain 1. Comparing like for like at the rates
we use:

| | ADS1263 (today) | ADS131M04 (proposed) |
|---|---|---|
| FSR at gain 1 | ±5 V (external REF7050) | ±1.2 V (internal, fixed) |
| Rate | 400 SPS, Sinc3 | 500 SPS (OSR 8192, HR) |
| Noise at gain 1 | ~1.3 µV rms | **2.39 µV rms** (Table 7-1) |
| Dynamic range | ~129 dB | **111 dB** (Table 7-2) |

That is roughly **17 dB — about 3 bits — of dynamic range given up**, and it lands
twice: once on the noise floor, and again because a 0–5 V sensor now has to be
attenuated ~4:1 before it can be presented at all, which multiplies the ADC's
sensor-referred noise by the same factor.

**Whether that matters is a per-channel question, and it is the first thing the
test plan has to answer.** For the laser it very likely does not: the IL-030 amp
maps ~10 mm onto 0–5 V (`MEMORY: laser-analog-window-vs-sensor-range`), so
1.3 µV ≈ 2.6 nm on the ADS1263 and ~12 µV ≈ 24 nm on the ADS131M04 behind a ÷5
divider — both are one to two orders of magnitude below the laser's *own*
documented artifacts (a ~3.3 µm drive-feedthrough step, a 65.8 Hz instrumental
tone carrying 74% of the idle variance; `MEMORY: sma-thermal-laser-drive-feedthrough`).
The laser channel is not ADC-limited either way.

**The load cell is not ADC-limited either — resolved 2026-08-27 from existing
data, not from the bench.** `Calibrate_LoadCell/data/2026-05-28_run07_points.csv`
already holds what was needed:

- Sensitivity **10.2 mV/mN** (`calibration.json`), sweeping 0 → 447 mN as
  **0 → 4.53 V**. So the amp works on a 0–5 V rail and needs **÷5** to fit ±1.2 V
  (÷4.17 bare minimum). Undivided, the ADS131M04 would clip at **±118 mN** —
  well inside the 172–490 mN `F_base` the thermal campaigns actually run at.
- Per-point scatter over 500 samples: **302 µV rms median, 547 µV max**. The
  ADS1263's own noise is 1.3 µV; the ADS131M04 behind ÷5 would be **~12 µV**.
  Added in quadrature onto 302 µV that is **+0.08%**, or **1.2 µN rms** in force
  units — against the **~5 mN of mechanical hysteresis** already carried as a
  known uncertainty in `calibration.json`, a factor of ~4000.

What makes that conclusive rather than suggestive: run07 recorded the *same* load
cell through **both** ADS1263 converters at once (the `adc2_xcompare` path), and
both saw ~300–600 µV. The noise is upstream of the converter — the amp and the
mechanics — so changing the ADC cannot move it.

**Both sensor channels therefore absorb the 17 dB loss.** What Stage 2 still has
to prove is the *divider*: that ÷5 is built accurately, is stable, and does not
clip at peak force — not whether the converter is quiet enough.

Secondary benefits, none of which alone would justify the swap: the REF7050 and
its cable disappear; one frame replaces two multi-byte register transactions, so
SPI duty (the rig's primary EMI aggressor into the laser channel) drops; rates up
to 64 kSPS become available if ever wanted.

---

## 2. What must be true at the hardware before anything is flashed

Three of these are new failure modes that the ADS1263 simply did not have. Get
them wrong and the chip looks dead or, worse, looks alive and lies.

**2.1 CLKIN is mandatory.** The ADS131M04 does not convert at all without a
continuous free-running LVCMOS clock on CLKIN (§8.3.5) — there is no internal
oscillator. On the EVM this comes from the on-board 8.192 MHz crystal oscillator
**Y1**, selected by the **default JP6/J13 jumper at [1-2]** (EVM guide §2.2,
Table 4). So: **leave JP6 at its factory position and do NOT install JP5** (JP5
powers Y1 *down*, and is only for feeding an external clock). Every data-rate
number in this plan assumes f_CLKIN = 8.192 MHz → f_MOD = 4.096 MHz.

**2.2 There is no attenuation on the EVM by default.** The EVM's divider
resistors **R17/R18 are not installed** from the factory, and R1/R2 only form a
divider if the external source already has a series resistor (EVM guide §2.1).
Out of the box the terminal blocks go essentially straight to the ADC pins.
**Applying the laser's 0–5 V output directly will clip at ±1.2 V** — it will read
a hard rail across most of the stroke, and the failure looks like "the sensor
stopped moving", not like an over-range error. Attenuation is a prerequisite,
not a refinement (§3).

**2.3 The default jumper state grounds every input.** JP1–JP4 ship at **[3-4]**,
which ties both inputs of each channel to ground through 1 kΩ (EVM guide Table 3).
That is exactly what Stage 1's shorted-input noise test wants — so leave them
alone until Stage 2, then move them for the channels being driven.

**2.4 Supplies and grounds.** AVDD/DVDD are 2.7–3.6 V, so 3V3 from the Mid
Carrier's J15 works, and the logic is directly H7-compatible. The EVM and the
carrier must share ground. An integrated negative charge pump lets absolute
input voltages sit slightly below AGND, so a single-ended sensor referenced to
its own ground is fine on AINnP with AINnN at ground.

---

## 3. Wiring — Cable 1 is re-terminated, not redesigned

The ADS131M04 needs **the same eight wires as the ADS1263 EVM**, so the existing
harness and pin assignment carry over unchanged. This is deliberate: it keeps the
two boards swap-in-place on the same cable, which is what makes the A/B
comparison in §8 cheap.

| Signal | Mid Carrier (ASX00055) | ADS131M04 EVM | Note |
|---|---|---|---|
| SCLK | J15-20 (`SPI1 SCLK`, PI_1) | J6[5] | primary EMI aggressor — see §5 on clock rate |
| COPI / DIN | J15-24 (`SPI1 COPI`, PC_3) | J6[2] | |
| CIPO / DOUT | J15-22 (`SPI1 CIPO`, PC_2) | J6[7] | |
| /CS | J15-25 (`PWM 0`, PA_8) | J6[4] | GPIO, not SPI1 hardware CS |
| /DRDY | J15-27 (`PWM 1`, PC_6) | J6[6] | one DRDY covers **all four** channels |
| SYNC/RESET | J15-29 (`PWM 2`, PC_7) | J6[1] | active low, dual-purpose (§4.4) |
| GND | J15-1/2 | J6[8] | must be common |
| 3V3 | J15-3/4 | EVM 3V3 | only if the EVM is not separately powered |

**EVM J6[3] is the ADC's CLK pin — leave it unconnected.** Y1 drives it (§2.1).
Do not tie it to anything on the carrier.

Analog side: channel *n* is terminal block J(n+1), pin 1 = AINnP, pin 2 = EVM
ground, pin 3 = AINnN (EVM guide Table 2). Initial assignment mirrors production
so the `src` IDs do not move:

- **CH0 → laser (Keyence IL-030)** — replaces ADS1263 ADC1/AIN4-AIN5, stays `src=1`
- **CH1 → load cell (LCA-9PC)** — replaces ADS1263 ADC2/AIN2-AIN3, stays `src=2`
- **CH2, CH3 → unused for now.** Left grounded via JP3/JP4 [3-4]. §9 covers what
  they are being held for.

The REF7050 (Cable 2) is **not connected to this board** — it has no external
reference input. Leave the reference wired to the ADS1263 EVM so that board stays
functional for A/B without a rebuild.

`docs/MEMO_cable_map.md` gets a "Cable 1-M04" section when the wiring is actually
made, filled in with the colours used — not before.

---

## 4. The driver — what is genuinely different from `ADS1263_Driver`

This is not a port. The register-and-command model is similar enough to be
misleading, and the data path is not the same shape at all.

**4.1 Everything is a fixed-length frame.** There is no per-channel read command.
Every transaction is 6 words (§8.5.1.7); at the reset-default 24-bit word length
that is **18 bytes**:

```
DIN :  [ command ][ data-or-zero ][ 0 ][ 0 ][ 0 ][ 0 ]
DOUT:  [ response ][  ch0  ][  ch1  ][  ch2  ][  ch3  ][ CRC ]
```

A single **NULL** frame therefore returns the STATUS register *and* all four
channels at once. Steady-state streaming is: send NULL, take four channels, check
CRC, repeat.

**4.2 The response word lags one frame.** DOUT word 0 answers the command sent in
the *previous* frame (§8.5.1.7). So a register read is two frames, and the frame
immediately after a WREG carries the write acknowledgement where STATUS normally
sits. The driver hides this by issuing a settling NULL frame after every register
write, so callers never decode a stale response as status.

**4.3 The output CRC is always on and cannot be disabled** (§8.3.12) —
CRC-16/CCITT, polynomial 0x1021, seed 0xFFFF, covering every word in the frame
including pad bits. It replaces the ADS1263's checksum byte as the driver's
validity gate, and it is strictly better: it covers the command and status words
too, not just the data. Input CRC stays **disabled** (the reset default); a
corrupted command is caught by reading the register back, and enabling it would
cost a word on every frame.

**4.4 SYNC/RESET is one pin doing two jobs, distinguished only by pulse width**
(§6.6, §8.4.1.2, §8.5.2) — and **the short pulse is the one that does NOT reset**:

- **≥ 2048 t_CLKIN** (t_w(RSL)) → **reset**. At 8.192 MHz that is **250 µs**.
- **1 … 2047 t_CLKIN** (t_w(SYL)) → **synchronise** — filters realigned, registers
  left alone. That is 122 ns … 250 µs.

So a "reset" pulse of a few microseconds silently performs a *sync* instead: the
chip keeps whatever configuration it had, keeps streaming, and looks fine. The
driver holds the line low for 1 ms to sit clear of the boundary, and then waits
t_REGACQ (5 µs) or a DRDY rising edge before talking to it — the device ignores
all SPI traffic before that point. Power-on reset takes t_POR = 250 µs.

*(Noted because the `-layout` text extraction of Table 6.6 mangles these two rows
into each other and reads as though reset were 8 t_CLKIN. It is not. Values above
are read off the raw table: t_w(RSL) min 2048 t_CLKIN, t_w(SYL) 1–2047 t_CLKIN.)*

**4.5 WREG payload sits immediately after the command word**, and the input CRC —
when enabled — goes *after* the data, not in a fixed slot (§8.5.1.10.8). With
input CRC off, DIN word 1 is the register value for a WREG and zero for
everything else. This is precisely the class of detail that cost us the
`RDATA2` 6-byte-frame and `ADC2CFG` field-order bugs on the ADS1263; it is
called out here so it gets tested rather than assumed (§7, T2).

**4.6 Configuration is three registers, not seven.** `CLOCK` (0x03) carries the
channel enables, OSR and power mode; `GAIN1` (0x04) carries all four PGA gains,
3 bits each; `MODE` (0x02) carries word length, CRC type and DRDY behaviour.
Target configuration for the rig:

- `CLOCK` = **0x0F1A** — all four channels enabled, HR mode, OSR 8192 → **500 SPS**
  (the closest available rate to the ADS1263's current 400 SPS; 1 kSPS at OSR 4096
  is one step away if wanted)
- `GAIN1` = **0x0000** — gain 1 on all channels. Gain cannot help here: it shrinks
  FSR further (§1), and every rig sensor is already too big for ±1.2 V.
- `MODE` — left at its 24-bit / CCITT / DRDY-low default.

**Datasheet contradiction to be aware of:** OSR code `111b` is documented as
**16256** in the CLOCK register table (Table 8-17) and as **16384** in the data
rate table (Table 8-2). We do not plan to use that code, but if the exact rate
ever matters, **measure it** (Stage 1, T5) rather than trusting either number.

**4.7 API shape.** Close enough to `ADS1263_Driver` that the M4 integration in
Stage 3 is a small diff, but honest about the frame model:

```cpp
bool     begin(uint32_t spi_hz = 2000000);
bool     reset();
uint16_t deviceID();                                  // 0x24xx on a healthy part
bool     configure(OSR, PWR = HR, ch_mask = 0x0F);
bool     setGain(uint8_t ch, Gain);
bool     readChannels(ADS131M04_Reading &out);        // one NULL frame -> 4 ch + STATUS
bool     dataReadyPin() const;
uint16_t readRegister(uint8_t addr);
bool     writeRegister(uint8_t addr, uint16_t value);
float    sps(), fsrVolts(ch), lsbVolts(ch);
uint32_t crcErrors(), framesRead();
```

`readChannels()` does **not** wait for DRDY — the caller owns timing, exactly as
`readADC1Direct()` did, so the proven timed-poll loop in the M4 stays proven.
DRDY is wired and readable, so a DRDY-gated mode can be A/B'd later; it is not
the default, because DRDY-driven firmware freezes outright if edges stop arriving
(`MEMORY: ads1263-drdy-ti-evm`).

**4.8 Copy convention.** The repo's rule is that the ADC driver is *copied*
between firmware projects, not shared (`CLAUDE.md`). The canonical
`ADS131M04_Driver` copy is **`Firmware_ADS131M04Test_PIO/lib/ADS131M04/`** for
the duration of the evaluation, and moves to the production fork if and only if
Stage 3 passes. Until then there is exactly one copy, and that is the point.

---

## 5. The test module — `Firmware_ADS131M04Test_PIO/`

A standalone bench project whose only job is to qualify the driver in isolation,
before any of it is allowed near the ring, the SMA controller, or a capture.

**Runs on M7, not M4.** M4 is where the ADC lives in production, but M4 has no
USB and no Ethernet — its output has to be bridged through M7 over RPC, which is
exactly the machinery we do not want in the loop while deciding whether a driver
works. On M7 the test firmware talks straight out USB-CDC and straight out UDP.
The project therefore also ships an **M4 idle image** (`portenta_m4_idle`, the
`-D M4_IDLE` stub pattern already used in the CC fork) which must be flashed
first, so that whatever M4 firmware is currently resident is not hammering SPI1
underneath the test.

**Commands** (USB-CDC, same single-owner text channel as the CC fork):

| Command | Does |
|---|---|
| `id` | read ID / STATUS / MODE / CLOCK / GAIN1 and print decoded |
| `regs` | full register dump |
| `read` | one frame, print four channels as code + volts + CRC state |
| `spi <hz>` | change SPI clock at runtime — this is how T3 is run |
| `osr <n>` / `gain <ch> <g>` | reconfigure without a reflash |
| `stream <sec>` | free-run frames, report achieved rate, CRC error count, DRDY edge count |
| `noise <sec>` | stream and report per-channel mean / rms / peak-to-peak in µV |
| `netcfg <ip> <port>` | arm the UDP stream — identical syntax and semantics to the CC fork |
| `rst` | hardware SYNC/RESET pulse + re-init |

**Wire format is deliberately identical to the production stream** —
`t_ms \t src \t raw \t volts \t hw_us \t seq`, with CH0 emitted as `src=1` and CH1
as `src=2`. That is what lets the existing host tooling read this firmware with no
changes at all, and it means Stage 2 can be captured and analysed with
`lib_h7_session` rather than a throwaway script.

**Host side:** `Firmware_ADS131M04Test_PIO/tools/m04_bench.py` — drives the
commands over serial, receives the UDP stream, and prints the acceptance numbers
from §7 directly (rate, CRC error rate, per-channel noise in µV rms, seq-gap
loss). It reuses `Experiment_SMAThermalCharacterization/lib_h7_session.py` through
the same `sys.path` shim the rest of the repo uses, rather than reimplementing a
parser that could disagree with production.

---

## 6. What does **not** change

Worth stating explicitly, because it is the reason this swap is affordable:

- **`sample_ring.h` is untouched.** `AdcSample` stays 32 bytes at the same SRAM4
  base; CH0/CH1 fill `src=1`/`src=2` with the same `raw_code` + `voltage_V`
  semantics.
- **The M7 is untouched.** `pumpSensors()`, `txEmit()`, `streamWrite()`, the
  batching, the `netcfg` command, the nbtx wedge-fix stack, `[STATUS]` — all of it
  is downstream of the ring and cannot tell which ADC filled it.
- **The UDP transport is untouched** and stays the default, per
  `docs/UDP_stream_migration_plan.md`. This plan does not reopen that decision.
- **The host is untouched.** `lib_h7_session`, `analysis/`, the sweep report, the
  merged tables — none of them see a wire-format change.

The one thing that unavoidably changes is **calibration**: new converter, new
reference, new attenuation network means `Calibrate_LaserHead`'s `k`/`V₀` and
`Calibrate_LoadCell`'s force fit are all invalid and must be re-run (§8, Stage 4).

---

## 7. Stage 1 — qualify the driver alone (no rig, no sensors)

M4 idle, M7 running the test firmware, EVM inputs left grounded by the default
JP1–JP4 [3-4] jumpers. Every test has a number attached; "it printed something"
is not a pass.

| # | Test | Pass criterion |
|---|---|---|
| **T1** | `id` | ID reads `0x24xx` (bits 15:12 = 0010b, CHANCNT = 4). A read of `0x0000` or `0xFFFF` means the SPI link or CLKIN is dead — check JP6 before touching the driver. |
| **T2** | register round-trip | Write and read back `CLOCK` and `GAIN1` across several values. This is the test that catches §4.5 getting the WREG payload slot wrong, and §4.2 getting the one-frame response lag wrong. |
| **T3** | SPI clock ladder | `spi 500000` → `2000000` → `8000000` → `16000000`, 60 s of `stream` at each. Adopt the **fastest rate with zero CRC errors, then back off one step.** Record the number; the ADS1263 runs this same harness at 500 kHz. |
| **T4** | CRC integrity | ≥10⁶ frames at the adopted clock with **0** CRC errors and STATUS `CRC_ERR` never set. |
| **T5** | rate accuracy | `stream 60` at OSR 8192 reports 500 SPS ±1%. Also run OSR 16256 once — it settles the Table 8-2 vs 8-17 contradiction (§4.6) empirically. |
| **T6** | DRDY | DRDY edge count over `stream 60` equals the conversion count to within one. Confirms one DRDY really does cover all four channels. |
| **T7** | shorted-input noise | Inputs grounded, gain 1, OSR 8192: per-channel noise **≤ 2× the 2.39 µV rms of Table 7-1**, and all four channels within 2× of each other. This is the number §1's whole trade rests on — if it comes in far worse, stop here. |
| **T8** | DC accuracy | A known stable DC input (bench supply, or REF7050 through a divider) on CH0 reads within the datasheet's offset+gain spec. Confirms `lsbVolts()` and the sign extension. |
| **T9** | reset recovery | `rst` mid-`stream` returns to streaming without a power cycle, and STATUS `RESET` is set then cleared. The ADS1263 could not do this without the `adcreset` workaround; if the M04 can, say so. |

**Gate:** T1–T8 must pass before any sensor is connected. T7 is the one that can
kill the whole idea, so run it early.

---

## 8. Stages 2–4 — sensors, then the rig

**Stage 2 — real sensors, still standalone.** Laser on CH0, load cell on CH1,
through whatever attenuation §2.2 forced. Still M7-only, still no ring, still no
SMA drive.

- ~~Measure the LCA-9PC output span and its noise.~~ **Answered from
  `Calibrate_LoadCell/` run07 (§1): 10.2 mV/mN, 0–4.53 V rail, ÷5 divider,
  amp noise ~302 µV rms vs the ADC's ~12 µV.** What is left is to verify the
  divider as built — ratio accuracy, tempco, and no clipping at peak force
  (`F_base` reaches 490 mN, `dF` up to 309 mN on top).
- Zaber displacement sweep: laser volts must be monotonic in µm with the same
  slope sign and a sane residual. Compare directly against a `Calibrate_LaserHead`
  sweep run on the ADS1263 **the same day, same setup** — this is what the
  swap-in-place cable of §3 is for.
- Verify no clipping anywhere across full stroke and full load.

**Stage 3 — into the rig, UDP intact.** Fork `Firmware_SMAConstantCurrent_PIO` →
`Firmware_ADS131M04SensorHub_PIO`, replacing only the M4 ADC block: same
`adcBringUp()` shape, same timed-poll loop, same `ring_push` with `src=1`/`src=2`.
M7 and host are literally unmodified (§6).

- **Acceptance is the existing campaign gate, unchanged:** one full sweep with
  `tx_drop ≈ 0`, `usb_heal = 0`, `crc_err = 0`, per-src rates at the configured
  500 SPS, UDP seq-gap loss ≈ 0, and a clean `operator_sweep_report.py`.
- Plus one thing the ADS1263 could never be asked for: **CH0 and CH1 come from the
  same conversion instant**, so laser/load skew should be identically zero. Verify
  it is, rather than assuming.

**Stage 4 — recalibrate, if and only if Stages 1–3 pass.** `Calibrate_LaserHead`
and `Calibrate_LoadCell` both carry their own copies of the ADS1263 driver and
both need the M04 driver and a re-run to produce valid `calibration.json`
constants. Until that happens, ADS131M04 captures are **uncalibrated** and must
not be fed to the analysis pipeline as if they were comparable to the July/August
campaigns.

---

## 9. Deliberately out of scope — the two spare channels

CH2 and CH3 are the most interesting thing about this board and they are **not**
part of this evaluation. Moving SMA voltage and current onto them would take
`src=3/4` off the H7's 12-bit on-chip ADC and onto a 24-bit converter that samples
V and I *simultaneously* — which is exactly the right instrument for `R = V/I` and
would address the conversion-duty droop and the V/I timing skew in one move.

It is out of scope because it is a **wire-format change**, not a drop-in: `src=3/4`
would move from the M7 clock to the M4 clock (arguably a fix — every sensor would
finally share one time base — but a change the host's clock-offset handling in
`lib_h7_session` has to be taught), and `seq_per_src[8]` is **exactly full**
(`sample_ring.h`), so nothing can be added without growing that array and the ring
header in lockstep with the host parser. Both SMA V and the INA296A current output
would also need their own attenuation study against ±1.2 V.

Decide it after Stage 3, on its own merits, with its own plan.

---

## 10. Risks

| Risk | Handling |
|---|---|
| **Load-cell channel is DR-limited by the 1.2 V reference.** The credible way this whole idea fails. | Stage 2 measures it before any integration work. Fallback: keep the load cell on the ADS1263 and run both boards — the SPI bus supports it with a second CS. |
| **Attenuation network adds its own drift.** A resistive divider's tempco lands directly on the calibration constant. | Use a stable divider, characterise it as part of the Stage 4 recalibration, and log the ratio in `calibration.json` rather than burying it in firmware. |
| **CLKIN silently absent** (JP6 moved, JP5 installed, Y1 unpowered) → the chip answers register reads but never converts. | T1 checks ID *and* T5/T6 check that conversions actually advance. Called out explicitly in §2.1 because it presents as "data is frozen", which we have misdiagnosed as a driver bug before (`docs/TROUBLESHOOTING_ADS1263_frozen_reading.md`). |
| **SPI clock raised too far on an unshielded harness** → CRC errors, or worse, EMI into the laser channel. | T3 adopts the fastest *clean* rate minus one step. The laser feedthrough check belongs to Stage 2, with the real sensor connected. |
| **Wire-format drift between firmware and host.** | §6: the format does not change. Any change to it is a separate, explicit decision. |
| **Evaluation contaminates the production path.** | Branch isolation, §11. |

---

## 11. Rollback

The ADS1263 path is not modified by any stage of this plan. Stages 1 and 2 add a
**new** firmware project and touch nothing existing. Stage 3 adds a **new fork**
rather than editing `Firmware_SMAConstantCurrent_PIO`.

To abandon the evaluation: stay on `feat/udp-stream` (or `main`), flash
`portenta_m7_nbtx_udp` + `portenta_m4`, move Cable 1 back to the ADS1263 EVM,
reconnect Cable 2 (REF7050). Nothing else to undo. The `feat/ads131m04` branch can
be kept unmerged indefinitely for the write-up.

---

## 12. Open questions for the bench

1. **What attenuation, and where?** The *ratio* is now known — **÷5 on both
   channels** (load cell 0–4.53 V per §1; laser 0–5 V). What is still open is
   *where*: a divider on the EVM inputs is quickest and its noise cost is
   provably negligible (§1), while re-ranging the sensors' own analog outputs
   would be cleaner still but invalidates `k`/`V₀`
   (`MEMORY: laser-analog-window-vs-sensor-range`) — a weaker objection than
   usual here, since Stage 4 recalibrates regardless.
   **Recommendation: divider on the EVM inputs.** It is reversible, it keeps the
   two boards swap-in-place for A/B, and the SNR argument for re-ranging buys
   nothing when the amp is already 25× noisier than the converter.
2. **500 SPS or 1 kSPS?** OSR 8192 matches today's 400 SPS most closely; OSR 4096
   doubles the rate for a 1.4× noise cost (3.38 vs 2.39 µV rms). The M4 poll loop
   and the ring absorb either. Cheap to A/B once T5 passes.
3. **Run both boards concurrently during Stage 2/3?** A second CS pin makes the
   A/B a single capture rather than two, which is far stronger evidence — at the
   cost of bus contention risk and one more wire.
