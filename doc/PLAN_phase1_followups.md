# PLAN — Phase 1 followups (cp7–cp10)

> **Self-contained handoff doc.** Designed to be picked up by a fresh AI agent or operator who has not seen the prior conversation. Read top-to-bottom; everything you need to know is here or behind one of the links below.

**Status:** ready for implementation. Phase 1.1 (cp6) and Phase 1.2 (`ADS1263_NoiseFloor_PIO/`) were drafted on 2026-05-24 and must be **bench-verified clean** before starting this work.

**Owner:** Yilin (operator), with implementation delegated to an AI agent.

**Last edited:** 2026-05-24.

---

## TL;DR

Add four more checkpoints — `cp7`, `cp8`, `cp9`, `cp10` — to [`../ADS1263_FirstPowerUp_PIO/src/main.cpp`](../ADS1263_FirstPowerUp_PIO/src/main.cpp), matching the style of existing `cp0`–`cp6`. Each retires one open question about the ADS1263 EVM + Mid Carrier rig that the bring-up + Phase 1.1/1.2 didn't address:

| cp | What it checks | Retires |
|---|---|---|
| `cp7` | AIN-pair scan: does each non-reference AIN pair on the EVM work, or does the legacy HAT's AIN2/3 saturation reproduce? | TODO: "Re-test ADC2/AIN2-AIN3 on the EVM" (silicon-side, ADC1 path) |
| `cp8` | ADC2 (the chip's 24-bit secondary ADC) — does it stream cleanly? | TODO: "Re-test ADC2" (ADC2 path). Unblocks `SensorHub_PIO/` dual-ADC mode. |
| `cp9` | DRDY (Data Ready) interrupt — is `PC_6` a working edge-triggered DRDY on the Mid Carrier, or do we need to fall back to timed polling like the legacy HAT? | TODO: "Reroute DRDY off PJ_11" |
| `cp10` | TDAC (Test DAC) sanity — drive a known internal voltage onto AIN6, measure it through ADC1, confirm it matches. | Free DC-accuracy sanity check before Phase 2.1 self-calibration verification. |

After all four pass, Phase 1 is complete and you can proceed to **Phase 2.1 (self-calibration verification)** in [`MEMO_baseline_testing.md`](MEMO_baseline_testing.md).

---

## Project context (read this first if you're a fresh agent)

**Hardware:**
- Arduino Portenta H7 (ABX00042) + Mid Carrier (ASX00055) + TI ADS1263 EVM, 6-wire SPI cable.
- External 5 V reference: **TI REF7050** on EVM's AIN0 / AIN1 (`REFMUX = 0x09`). **AIN0/AIN1 are reference-only — never use them as measurement inputs.**
- EVM is unipolar: AVDD = +5 V (on-board TPS7A4700 LDO), AVSS = GND, mid-supply = +2.5 V.
- See [`MEMO_cable_map.md`](MEMO_cable_map.md) for wiring; [`PortentaMidCarrier_ASX00055_Pinout.pdf`](PortentaMidCarrier_ASX00055_Pinout.pdf) for the J15 connector; [`ADS1263_Datasheet.pdf`](ADS1263_Datasheet.pdf) for register reference.

**Firmware pin defines (already established, must match):**
```cpp
#define PIN_CS     PA_8       // Mid Carrier J15-25
#define PIN_DRDY   PC_6       // Mid Carrier J15-27
#define PIN_RESET  PC_7       // Mid Carrier J15-29
```

**Standing chip configuration (established by cp0–cp6):**
- SPI: 500 kHz, MODE1, MSBFIRST, default `SPI` object on the Mid Carrier
- `INTERFACE = 0x05` (chip default — STATUS byte + CRC byte appended to every `RDATA1` frame; `RDATA1` returns 6 bytes total)
- `REFMUX = 0x09` (external 5 V on AIN0/AIN1)
- `POWER = 0x13` (INTREF + VBIAS on — drives AINCOM to mid-supply)
- `MODE2 = 0x08` (PGA enabled, gain=1, 400 SPS) at the time `cp7` starts

**Style guide (match cp0–cp6 exactly):**
- M7-only — no M4, no RPC, no shared driver code, no `lib/ADS1263/` imports.
- Inline SPI helpers (`ads_read_reg`, `ads_write_reg`, `ads_command`, `ads_read_conversion`) — they already exist in `main.cpp`.
- Each cp is a `static void cpN_<name>()` function called from `setup()` in order.
- Output prefix `[cp N]` for every Serial line; `cp_pass(N, msg)`, `cp_fail(N, msg, hint)`, `cp_info(N, msg)` helpers exist.
- On `cp_fail`, the cp halts the M7 with a fast-blink LED (already handled by `cp_fail`).
- Two-pass mean/RMS for any noise stats (raw codes can hit ±2^31, summing squares of those overflows double precision; see how `cp5_noise_floor()` and `cp6_vbias_pga_minisweep()` do it).
- Keep the file under ~1000 lines total. If you need to share code between cps, add a helper near the existing ones (don't create a new file).

---

## Prerequisites

Before starting:

1. **Phase 1.1 + 1.2 must be bench-verified clean.** That means:
   - `ADS1263_FirstPowerUp_PIO/` flashed, all cp0–cp6 print `PASS`, the cp6 mini-sweep table shows reasonable input-referred RMS (single-digit µV) at every gain, with the input-referred mean approximately constant across the gain column.
   - `ADS1263_NoiseFloor_PIO/` flashed, full CSV captured, `tools/analyze_noise_floor.py` reports no anomalies (or known/acceptable anomalies only).
   - Both bench logs added to the result table in [`MEMO_baseline_testing.md`](MEMO_baseline_testing.md).
2. **Update the result log** in `MEMO_baseline_testing.md` so the work history is concrete before adding more.
3. If Phase 1.1 had any FAIL rows in the cp6 mini-sweep at higher PGA gains, **stop** and investigate before doing cp7–cp10 — those checkpoints all assume the PGA path is healthy.

---

## Implementation details — checkpoint by checkpoint

### `cp7` — AIN-pair scan

**Purpose.** The legacy Waveshare HAT had an unresolved issue where the AIN2/AIN3 pair saturated under any non-zero input. The bare TI EVM has different input-stage circuitry (passive RC filters on each pair, no front-end amplifier), so the issue may or may not reproduce. We need to confirm before assigning the load cell / laser to specific AIN pairs in Phase 3.

**Method.** For each candidate pair, configure `INPMUX` accordingly, run START1, collect 500 samples at PGA=1, 400 SPS, with the chip configuration otherwise unchanged from cp6's exit state. Confirm no saturation (code not pinned at ±2^31), and that the mean / RMS are in plausible ranges.

**INPMUX coding (datasheet §9.6.7, Table 9-41):**
- High nibble = MUXP, low nibble = MUXN.
- `0` = AIN0, `1` = AIN1, ..., `9` = AIN9, `A` = AINCOM, `B` = temp sensor, `C/D` = supply monitors, `E` = TDAC test, `F` = floating.

**Pairs to scan** (skip AIN0/1 — reference-committed):

| Config | INPMUX | What it measures |
|---|---|---|
| AIN2 vs AIN3 (differential) | `0x23` | Pair AIN2/AIN3 differential, no external input — should read ~0 if both inputs are unconnected (floating) and the EVM filter network biases them similarly. |
| AIN4 vs AIN5 | `0x45` | Same, for AIN4/AIN5. |
| AIN6 vs AIN7 | `0x67` | Same, for AIN6/AIN7 (also the TDAC test outputs — do this **before** cp10 which enables TDAC). |
| AIN8 vs AIN9 | `0x89` | Same, for AIN8/AIN9. |
| AIN2 vs AINCOM | `0x2A` | AIN2 single-ended against the VBIAS-biased AINCOM. Differential = (AIN2 voltage) − 2.5 V. With AIN2 floating, the EVM's RC filter pull-up/down behaviour will determine where it sits — useful diagnostic. |
| AIN4 vs AINCOM | `0x4A` | Same, AIN4. |
| AIN6 vs AINCOM | `0x6A` | Same, AIN6. |
| AIN8 vs AINCOM | `0x8A` | Same, AIN8. |

That's 8 configs × 500 samples × 5 ms/sample ≈ 20 s total. Acceptable.

**Acceptance criteria (per row):**
- Codes are not pinned at ±2^31 (no saturation). Concretely: `|max(code)| < 2^30` and `|min(code)| < 2^30`.
- RMS is non-zero (conversions are advancing).
- Mean is plausible: differential configs (`0xXY` with X, Y both < 0xA) should read near zero (a few mV at most — floating-input pickup is acceptable here). Single-ended (`0xXA`) can be anywhere in [−5 V, +5 V] depending on what the floating AIN sits at.

**Acceptance criteria (overall cp7):**
- Every pair reports non-saturated, non-zero-RMS readings → PASS.
- If any pair saturates → FAIL with a hint pointing at "possible AIN2/3-style issue on this EVM — investigate input-stage filter components for that pair" and naming which row failed.

**Restore state on exit:** set `INPMUX = 0xAA` (AINCOM-shorted) so downstream cps start from a known config.

**Skeleton:**
```cpp
static void cp7_ain_pair_scan() {
    cp_info(7, "AIN-pair scan: confirm each non-reference pair "
               "doesn't reproduce the legacy HAT AIN2/3 saturation");
    cp_info(7, "  inpmux | meaning            | mean (mV) |  max code  |  min code  |  RMS (uV) | result");

    struct PairCfg { uint8_t inpmux; const char *label; };
    const PairCfg pairs[] = {
        { 0x23, "AIN2 vs AIN3 diff " },
        { 0x45, "AIN4 vs AIN5 diff " },
        { 0x67, "AIN6 vs AIN7 diff " },
        { 0x89, "AIN8 vs AIN9 diff " },
        { 0x2A, "AIN2 vs AINCOM SE " },
        { 0x4A, "AIN4 vs AINCOM SE " },
        { 0x6A, "AIN6 vs AINCOM SE " },
        { 0x8A, "AIN8 vs AINCOM SE " },
    };
    // Loop, set MODE2=0x08 to keep PGA=1, 400 SPS; capture 500 samples per row.
    // Use the same two-pass mean/RMS pattern as cp5 / cp6.
    // ...
    // Restore INPMUX = 0xAA on exit.
}
```

**Hints:**
- Use `MODE2 = 0x08` (PGA enabled, gain=1, 400 SPS) for all rows. cp6's exit state is `0x58` (gain=32, 400 SPS); write `0x08` once at the top of cp7.
- The differential pairs may sit near 0 V or may sit at non-zero from floating-input pickup. Both are acceptable as long as not railed. **The test is not about what the floating value is — it's about whether the pair *works at all***.

---

### `cp8` — ADC2 enable + read

**Purpose.** The ADS1263 has a 24-bit ΔΣ secondary ADC (ADC2) on the same chip, with its own input mux, filter, and command set. Production firmware (`SensorHub_PIO/`) plans to use ADC2 for the laser channel so load cell + laser can run concurrently on different rates. The bring-up never exercised ADC2 — we don't know if it works on the EVM.

**Method.** Configure `ADC2CFG` and `ADC2MUX` registers, issue `START2`, wait for settling, read with `RDATA2`. Verify codes are non-stuck, non-saturated.

**Relevant ADC2 commands and registers** (datasheet §9.5 and §9.6.16):

| Symbol | Value | Notes |
|---|---|---|
| `ADS1263_CMD_START2` | `0x0C` | Start ADC2 conversions |
| `ADS1263_CMD_STOP2`  | `0x0E` | Stop ADC2 |
| `ADS1263_CMD_RDATA2` | `0x14` | Read ADC2 conversion result |
| Reg `ADC2CFG`   | `0x15` | Bits 7:6 = DR2 (00=10, 01=100, 10=400, 11=800 SPS); 5:3 = REF2; 2:0 = GAIN2 (000=1V/V default) |
| Reg `ADC2MUX`   | `0x16` | Same encoding as `INPMUX` (bits 7:4 = MUXP2, 3:0 = MUXN2). Default `0x01` (AIN0/AIN1) — must change because those are the reference. |
| Reg `ADC2OFC0-2` | `0x17/18` | ADC2 offset calibration registers (read-only check) |
| Reg `ADC2FSC0-2` | `0x19/1A` | ADC2 full-scale calibration registers |

**Configuration to use:**
- `ADC2CFG = 0x40` → DR2=01 (100 SPS), REF2=000 (internal 2.5 V — keep ADC2 on internal ref for this test to decouple from the REF7050 path), GAIN2=000 (1 V/V).
- Wait — REF2 selects the reference for ADC2. Datasheet §9.6.16 Table 9-50: 000 = internal 2.5 V, 001 = external AIN0/1, 010 = external AIN2/3, 100 = AVDD/AVSS (5V). Pick **001** to share the REF7050 with ADC1: `ADC2CFG = 0x48` (DR2=01, REF2=001, GAIN2=000).
- `ADC2MUX = 0x4A` → AIN4 vs AINCOM, single-ended (assuming cp7 confirmed AIN4 works). If cp7 found AIN4 saturated, use whichever pair did work.

**RDATA2 frame format.** ADC2 returns 24-bit data (3 bytes), MSB first, signed two's complement. With `INTERFACE = 0x05` (chip default), each `RDATA2` transaction returns `STATUS (1) + DATA0..2 (3) + CRC (1) = 5 bytes`. Code this similarly to `ads_read_conversion()` but with 5-byte frame and 24-bit sign extension:

```cpp
static bool ads_read_conversion_adc2(int32_t *out_code) {
    uint8_t f[5];
    SPI.beginTransaction(SPI_CFG);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(ADS1263_CMD_RDATA2);
    for (int i = 0; i < 5; i++) f[i] = SPI.transfer(0x00);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    // f[0]=STATUS, f[1..3]=data (24-bit MSB first), f[4]=CRC
    uint32_t raw = ((uint32_t)f[1] << 16) | ((uint32_t)f[2] << 8) | (uint32_t)f[3];
    // Sign-extend 24-bit to 32-bit
    if (raw & 0x00800000) raw |= 0xFF000000;
    *out_code = (int32_t)raw;
    return true;
}
```

**Volts-per-code for ADC2 at gain=1:** `V = code * VREF / 2^23` (since ADC2 is 24-bit, full-scale = ±2^23). With VREF=5V, LSB ≈ 0.596 µV. ADC2 is intrinsically noisier than ADC1 (~8 LSB roughly = ~5 µV at gain=1, 100 SPS — order-of-magnitude estimate, check datasheet Table 7.11).

**Acceptance:**
- ADC2 returns non-stuck (RMS > 0), non-saturated codes.
- Mean ≈ 0 (or ≈ wherever cp7 saw AIN4 sit), within a few mV.
- RMS within an order of magnitude of expected (single-digit to tens of µV at gain=1, 100 SPS).

**Hints:**
- ADC1 and ADC2 share the chip but are independent. Don't STOP1 — leave ADC1 running. Just configure ADC2 and START2.
- At 100 SPS the sample period is 10 ms — use `delay(15)` between reads (5 ms margin) and collect 100 samples = ~1.5 s.
- Restore state on exit: STOP2, optionally restore `ADC2MUX = 0x01` to default.

---

### `cp9` — DRDY edge-rate count

**Purpose.** The bring-up uses **timed polling** to read conversions — `delay(5)` between reads at 400 SPS. The legacy HAT setup did this because the original DRDY pin (`PJ_11`) was tied to the LoRa IRQ on the Portenta H7 module and never went LOW (see `ADS1263/ADS1263_H7_Integration_Notes.md` §5). On the Mid Carrier, DRDY moved to `PC_6` — which is **not** shared with the LoRa peripheral — so DRDY should be usable as an interrupt source. We need to confirm by counting edges.

**Method.** Configure ADC1 at 400 SPS, attach a falling-edge interrupt on `PC_6`, increment a counter in the ISR, run for 10 seconds, compare count to expected (4000 ± 1%).

**Skeleton:**
```cpp
volatile uint32_t drdy_count = 0;

static void drdy_isr() {
    drdy_count++;
}

static void cp9_drdy_edge_count() {
    cp_info(9, "DRDY edge-rate count: 10 s at 400 SPS, expect 4000 ± 40 edges");

    // ADC1 should already be configured from cp7 exit state (MODE2 = 0x08, INPMUX = 0xAA)
    // Make sure conversions are running:
    ads_command(ADS1263_CMD_START1);
    delay(50);

    drdy_count = 0;
    attachInterrupt(digitalPinToInterrupt(PIN_DRDY), drdy_isr, FALLING);
    uint32_t t0 = millis();
    while (millis() - t0 < 10000) { /* spin */ }
    detachInterrupt(digitalPinToInterrupt(PIN_DRDY));

    uint32_t count = drdy_count;
    char buf[80];
    snprintf(buf, sizeof(buf), "counted %lu edges in 10 s (expected ~4000)", (unsigned long)count);
    cp_info(9, buf);

    if (count < 3960 || count > 4040) {
        cp_fail(9, "DRDY edge count outside ±1% of expected",
                "DRDY on PC_6 may not be wired correctly, the chip "
                "may not be in continuous-conversion mode, or PC_6 "
                "isn't supported as an interrupt source by this core. "
                "Fall back to timed polling for now and revisit.");
    }
    cp_pass(9, "DRDY interrupt-capable on PC_6");
}
```

**Hints:**
- `digitalPinToInterrupt(PC_6)` should resolve correctly on the Portenta H7 mbed core. If the compiler complains, use `PC_6` directly — the mbed core treats it as a valid pin number.
- The ISR must be `volatile`-protected on the counter. The chip drives DRDY LOW when data is ready; reading via `RDATA1` clears it, but in this test we're not reading — DRDY will pulse for each conversion regardless.
- Wait, actually — does DRDY pulse autonomously, or does it stay LOW until read? Per datasheet §9.4.4 ("Data Ready"), DRDY goes LOW when a new conversion is available, and goes HIGH at the next `RDATA1`. **If we're not reading**, DRDY stays LOW continuously after the first conversion → no edges. So we need to either (a) keep reading conversions while counting edges, or (b) use a different signaling mode. Option (a) is simpler:

```cpp
// Better: poll RDATA1 in the loop so DRDY actually toggles.
attachInterrupt(digitalPinToInterrupt(PIN_DRDY), drdy_isr, FALLING);
uint32_t t0 = millis();
int32_t throwaway;
while (millis() - t0 < 10000) {
    if (digitalRead(PIN_DRDY) == LOW) {
        ads_read_conversion(&throwaway);
    }
}
detachInterrupt(digitalPinToInterrupt(PIN_DRDY));
```

(This is hybrid: interrupt counts edges, polling reads data to clear DRDY. Cleaner alternatives — e.g. setting `MODE1` for pulse-mode DRDY — are possible; see datasheet §9.6.4 if you want to go there.)

**Acceptance:** 3960 ≤ count ≤ 4040.

---

### `cp10` — TDAC sanity check

**Purpose.** The ADS1263 has an internal Test DAC (TDAC) that drives a known voltage onto AIN6 (TDACP) and/or AIN7 (TDACN). This is a chip-internal calibrated source — datasheet §9.3.12. Using it for a quick DC-accuracy sanity check tells us whether the entire ADC1 signal chain (input mux + PGA + ΔΣ + filter + reference) is producing the right answer for a known input, without needing an external precision voltage source.

**Relevant registers (datasheet §9.6.13–9.6.14):**

| Reg | Addr | Layout |
|---|---|---|
| `TDACP` | `0x10` | Bit 7 = OUTP (connect TDACP to AIN6); bits 4:0 = MAGP magnitude code |
| `TDACN` | `0x11` | Bit 7 = OUTN (connect TDACN to AIN7); bits 4:0 = MAGN magnitude code |

**MAGP voltage table** (values are absolute, referenced to AVSS = GND):

| MAGP code | Voltage |
|---|---|
| `00000` | 2.5 V |
| `00001` | 2.5078 V |
| `00010` | 2.516 V |
| ... | (small increments) ... |
| `00100` | 2.563 V |
| `00111` | 3.0 V |
| `01000` | 3.5 V |
| `01001` | 4.5 V |
| `10001` | 2.492 V |
| ... | (small increments below 2.5) ... |
| `10110` | 2.25 V |
| `10111` | 2.0 V |
| `11000` | 1.5 V |
| `11001` | 0.5 V |

**Method.** Set `TDACP = 0x88` → OUTP=1, MAGP=01000 = 3.5 V. Read AIN6 vs AINCOM (`INPMUX = 0x6A`). Expected differential = 3.5 V − 2.5 V (VBIAS) = **+1.0 V**. At PGA=1, VREF=5 V, expected code = +1.0/5.0 × 2^31 ≈ +429 million.

Repeat for `TDACP = 0x99` → OUTP=1, MAGP=11001 = 0.5 V → expected differential = 0.5 − 2.5 = **−2.0 V** → expected code ≈ −858 million.

Sweep a few TDAC settings and verify the measured voltage matches the configured value within tolerance.

**Acceptance per row:** measured differential within ±50 mV of expected (1% of typical test voltage — generous, accommodates TDAC inaccuracy + chip offset + reference tolerance).

**Acceptance for cp10:** all sweep points within tolerance, sample RMS reasonable (single-digit mV, expected from PGA=1 noise floor).

**Hints:**
- **TDAC overrides AIN6/AIN7 driver.** Datasheet §3.1.1.5: "Do not load AIN6 and AIN7 when the test signals are enabled because the TDAC outputs are unbuffered." On the EVM, the only load is the input filter (R10/C10-style — see EVM user guide). Fine. But if you ever wire something external to AIN6/AIN7, **disable TDAC first** (write `TDACP = 0x00`, `TDACN = 0x00`).
- TDAC requires a few ms to settle after enable — `delay(10)` after each `TDACP` write is plenty.
- Use INPMUX = `0x6A` (AIN6 vs AINCOM). AIN6's filter network adds RC settling on top of TDAC's; total combined settling well under 10 ms.

**Skeleton:**
```cpp
static void cp10_tdac_sanity() {
    cp_info(10, "TDAC sanity: drive AIN6 to known voltages, read via ADC1, "
                "expect measured ≈ configured ± 50 mV");

    // Switch ADC1 input to AIN6 vs AINCOM, PGA=1, 400 SPS
    ads_write_reg(ADS1263_REG_INPMUX, 0x6A);
    ads_write_reg(ADS1263_REG_MODE2,  0x08);

    struct TdacPoint { uint8_t tdacp_reg; double expected_v; const char *label; };
    const TdacPoint pts[] = {
        { 0x80, +0.0, "TDACP=2.5 V" },   // MAGP=00000 = 2.5V, expected diff = 0
        { 0x88, +1.0, "TDACP=3.5 V" },   // MAGP=01000 = 3.5V, expected diff = +1.0V
        { 0x89, +2.0, "TDACP=4.5 V" },   // MAGP=01001 = 4.5V, expected diff = +2.0V
        { 0x97, -0.5, "TDACP=2.0 V" },   // MAGP=10111 = 2.0V, expected diff = -0.5V
        { 0x98, -1.0, "TDACP=1.5 V" },   // MAGP=11000 = 1.5V, expected diff = -1.0V
        { 0x99, -2.0, "TDACP=0.5 V" },   // MAGP=11001 = 0.5V, expected diff = -2.0V
    };

    cp_info(10, "  TDAC config   | expected diff | measured | error  | result");

    bool all_ok = true;
    for (size_t i = 0; i < sizeof(pts)/sizeof(pts[0]); i++) {
        ads_write_reg(/* TDACP addr 0x10 */ 0x10, pts[i].tdacp_reg);
        delay(10);  // TDAC + filter settling

        ads_command(ADS1263_CMD_START1);
        delay(50);

        // Collect ~100 samples, two-pass mean
        const int N = 100;
        int32_t codes[100];
        for (int j = 0; j < N; j++) { delay(5); ads_read_conversion(&codes[j]); }
        double sum = 0.0;
        for (int j = 0; j < N; j++) sum += (double)codes[j];
        double mean_code = sum / N;
        double measured_v = mean_code * (5.0 / 2147483648.0);

        double err = measured_v - pts[i].expected_v;
        const char *verdict = (fabs(err) < 0.050) ? "pass" : "FAIL";
        if (verdict[0] == 'F') all_ok = false;

        char buf[128];
        snprintf(buf, sizeof(buf), "  %s | %+6.3f V      | %+7.3f V | %+5.3f V | %s",
                 pts[i].label, pts[i].expected_v, measured_v, err, verdict);
        cp_info(10, buf);
    }

    // Disable TDAC and restore INPMUX
    ads_write_reg(0x10, 0x00);   // TDACP off
    ads_write_reg(ADS1263_REG_INPMUX, 0xAA);

    if (!all_ok) {
        cp_fail(10, "TDAC measurements off-spec",
                "One or more rows exceeded ±50 mV. Investigate: VREF wrong "
                "(should be 5.0 V on REF7050), PGA bypass mode left on, or "
                "TDAC didn't enable. Re-run cp5 to confirm reference path.");
    }
    cp_pass(10, "TDAC sanity passed across the sweep");
}
```

**Note:** TDACP register address is `0x10`. Add to the register `#define` block at the top of `main.cpp`:
```cpp
#define ADS1263_REG_TDACP  0x10
#define ADS1263_REG_TDACN  0x11
```

---

## Call order in `setup()`

After the existing `cp0_serial_up()` ... `cp6_vbias_pga_minisweep()` block, add:

```cpp
cp7_ain_pair_scan();
cp8_adc2_check();
cp9_drdy_edge_count();
cp10_tdac_sanity();
```

The "ALL CHECKPOINTS PASSED" banner should be updated to say "cp0–cp10 passed" and the "Next steps" lines should point to **Phase 2.1 (self-calibration verification)** in [`MEMO_baseline_testing.md`](MEMO_baseline_testing.md).

---

## Testing the new code

1. `pio run` in `ADS1263_FirstPowerUp_PIO/` — must compile.
2. `pio run -t upload` — must upload via DFU.
3. **Power-cycle the EVM** before reopening the serial monitor (the DFU reset on the H7 doesn't cleanly re-power the ADC).
4. `pio device monitor 2>&1 | tee data/firstpowerup_$(date +%Y%m%d_%H%M).log` — capture for the record.
5. All 11 checkpoints should PASS. If any FAIL, the in-line hint plus this PLAN doc and the existing `README.md`'s failure-triage table should localize the cause.

---

## Documentation updates required after this work lands

Same operator who runs the bench tests should update:

- `ADS1263_FirstPowerUp_PIO/STATUS.md` — "cp0–cp10 bench-verified [date]"
- `ADS1263_FirstPowerUp_PIO/README.md` — extend expected-output block, extend failure-triage table for cp7–cp10
- [`MEMO_baseline_testing.md`](MEMO_baseline_testing.md) — tick off Phase 1.3, 1.4, 1.5, 1.6 in the result table with the log file path
- [`../TODO.md`](../TODO.md) — strike through "Re-test ADC2/AIN2-AIN3 on the EVM" and "Reroute DRDY off PJ_11" if cp7/cp8/cp9 confirm they're no-ops
- Top-of-file comment block in `main.cpp` — extend the `cp 0 ... cp 6` summary list to include `cp 7 ... cp 10`

---

## What I deliberately did NOT include

- **Cross-talk testing between ADC1 and ADC2 channels.** Possible Phase 1 followup, but it's a system-level concern that's better evaluated once both sensors are physically wired (Phase 3).
- **Frequency response / AC SNR sweep.** Needs an external waveform generator. Deferred — see the original Phase 1.6 discussion in `MEMO_baseline_testing.md` (an earlier draft had this as a separate step).
- **Filter mode (Sinc1/2/3/4/FIR) sweep.** All cps here use the default Sinc3. Other filter modes are valid future work but not in this PLAN.
- **Long-term drift (Phase 2.3) and DC linearity (Phase 2.2).** These are TABLED in the current cycle — see `MEMO_baseline_testing.md` for the rationale.

---

## References

- [`MEMO_baseline_testing.md`](MEMO_baseline_testing.md) — parent plan; this PLAN implements its Phase 1.3–1.6 rows.
- [`MEMO_cable_map.md`](MEMO_cable_map.md) — wiring; Cable 1 = SPI bus, Cable 2 = REF7050.
- [`ADS1263_Datasheet.pdf`](ADS1263_Datasheet.pdf) — §9.4.4 (DRDY), §9.5 (commands), §9.6 (registers, esp. 9.6.6 MODE2, 9.6.7 INPMUX, 9.6.13–14 TDAC, 9.6.16 ADC2 group), §9.3.12 (TDAC details), §9.3.7 (ADC2 architecture).
- [`ADS1263_EVM_User_Guide.pdf`](ADS1263_EVM_User_Guide.pdf) — §3.1.1 (input options), §3.1.1.5 (TDAC on AIN6/AIN7).
- [`../ADS1263_FirstPowerUp_PIO/src/main.cpp`](../ADS1263_FirstPowerUp_PIO/src/main.cpp) — the file you're modifying; the existing cp0–cp6 are the style template.
- [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) — §5 (the legacy DRDY/LoRa conflict, for context on why cp9 exists).
