# ADS1263_SelfCal_PIO — Phase 2.1 self-calibration verification

> **Status: Diagnostic — bench-verified 2026-05-24.** All 4 checkpoints PASS. SFOCAL1 reduces offset 94–100% across PGA gains; SYGCAL1 FSCAL within 1.4 ppm of predicted, post-cal reads exactly +VREF. INTERFACE register survived all 7 cals — legacy-HAT snap-back does NOT reproduce on this EVM. See [STATUS.md](STATUS.md). See [../README.md](../README.md) for project overview. See [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md) for the wider Phase 2 testing plan; this module is Phase 2.1.

Four-checkpoint sketch that exercises the ADS1263's built-in calibration commands and verifies they (a) actually do something to the calibration registers, (b) bring the measurement closer to the expected value, and (c) don't trigger the "INTERFACE register snaps back to default" issue documented in [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) §6.

**Hardware:** same as [`ADS1263_FirstPowerUp_PIO/`](../ADS1263_FirstPowerUp_PIO/) — Portenta H7 + Mid Carrier + ADS1263 EVM, REF7050 external 5 V reference on AIN0/AIN1, VBIAS biasing AINCOM to mid-supply.

## When to use this

- **After [`ADS1263_FirstPowerUp_PIO/`](../ADS1263_FirstPowerUp_PIO/) cp0–cp10 all PASS.** This sketch reuses the same pin defines and assumes the chip is alive on the bus.
- After any change that could affect calibration sensitivity: REF7050 swap, AVDD trim change, PGA path rework, etc.
- Before locking in production firmware's calibration strategy in `SensorHub_PIO/`.

## What each checkpoint does

| cp | What it does | Key registers / commands |
|---|---|---|
| `cp 0` | USB CDC serial up | — |
| `cp 1` | Bring-up: GPIO + /RESET pulse + SPI + ADS1263 ID check + configure REFMUX/INPMUX/MODE2/POWER (VBIAS on) | Same as FirstPowerUp cp1–cp4 + cp6 setup |
| `cp 2` | **SFOCAL1 sweep** — run self-offset cal at every PGA gain ∈ {1, 2, 4, 8, 16, 32}, verify OFCAL register written and post-cal mean is < 10 % of pre-cal mean. Defensive register re-write per integration notes §6. **INTERFACE register survival check at every gain.** | `SFOCAL1` (`0x19`), `OFCAL[2:0]` (`0x07..0x09`) |
| `cp 3` | **SYGCAL1 demo** — drive TDAC at 0.9·AVDD (AIN6) and 0.1·AVDD (AIN7) for an 0.8·AVDD ≈ 4.16 V near-full-scale differential, run system-gain cal, verify FSCAL register written and post-cal measurement within ±0.5 % of predicted | `SYGCAL1` (`0x17`), `FSCAL[2:0]` (`0x0A..0x0C`), `TDACP`/`TDACN` (`0x10`/`0x11`) |

The TDAC reference voltage (`AVDD_KNOWN_V = 5.2056 V`) is hard-coded from the `ADS1263_FirstPowerUp_PIO/` cp10 ratiometric measurement. If the EVM is replaced or its LDO trim changes, re-run that cp first and update `AVDD_KNOWN_V` at the top of `src/main.cpp`.

## Prerequisites — before you flash

1. **`ADS1263_FirstPowerUp_PIO/` cp0–cp10 must all PASS** on the current rig. cp10's AVDD measurement is a precondition for cp3's prediction.
2. **External reference (REF7050) on AIN0/AIN1**, `REFMUX = 0x09`.
3. **Power-cycle the EVM** before running — the DFU reset on the H7 doesn't cleanly re-power the ADC.

## Build + flash + capture

```sh
cd ADS1263_SelfCal_PIO/
mkdir -p data
pio run -t upload
```

Power-cycle the EVM after flash, then:

```sh
pio device monitor 2>&1 | tee data/selfcal_$(date +%Y%m%d_%H%M).log
```

PowerShell equivalent:

```powershell
$ts = Get-Date -Format "yyyyMMdd_HHmm"
pio device monitor 2>&1 | Tee-Object -FilePath "data\selfcal_$ts.log"
```

## Expected output (success)

```
============================================================
  ADS1263 self-calibration verification — Phase 2.1
============================================================
Prerequisite: ADS1263_FirstPowerUp_PIO/ cp0–cp10 all PASS
Cable map:    ../doc/MEMO_cable_map.md
AVDD assumed: 5.2056 V (from cp10 ratiometric measurement)

[cp 0] PASS  Serial up (USB CDC enumerated)
[cp 1] info  pulsing /RESET LOW for 100 ms
[cp 1] info  SPI.begin() — default object on Mid Carrier J15-20/22/24
[cp 1] info  ID = 0x23 (expect 0x2X), INTERFACE = 0x05 (expect 0x05)
[cp 1] info  configuring: REFMUX=0x09 ... POWER=0x13 ...
[cp 1] PASS  bring-up complete — chip ready for self-cal
[cp 2] info  SFOCAL1 sweep: AINCOM-shorted, run self-offset-cal at each PGA gain
[cp 2] info    gain | MODE2 | pre_mean (uV) | OFCAL (predicted)| OFCAL (actual)  | INTERFACE | post_mean (uV) | reduction | result
[cp 2] info    -----+-------+---------------+------------------+-----------------+-----------+----------------+-----------+--------
[cp 2] info       1 |  0x08 |     +739.xxx  | 0x???... (+...)  | 0x???... (+...) | 0x05 OK   |        +0.xxx  |    99.x%  | pass
[cp 2] info       2 |  0x18 |     ...
[cp 2] info      ...
[cp 2] info      32 |  0x58 |      +23.xxx  | 0x???... (+...)  | 0x???... (+...) | 0x05 OK   |        +0.xxx  |    99.x%  | pass
[cp 2] PASS  SFOCAL1 sweep clean across all PGA gains
[cp 3] info  SYGCAL1 demo: drive TDAC at 0.8·AVDD, run system-gain cal, verify FSCAL
[cp 3] info  predicted signal (0.8 × AVDD) = 4.1645 V; pre-cal measured = 4.1xx V (error = ...)
[cp 3] info  FSCAL pre-cal: 0x400000 (default 0x400000 = unity gain)
[cp 3] info  FSCAL post-cal: 0x3FFxxx (delta from default = -xxx LSB → gain factor 0.99xxx)
[cp 3] info  post-cal measured = 4.16xx V (error = ...)
[cp 3] info  INTERFACE register = 0x05 (OK — survived SYGCAL1)
[cp 3] PASS  SYGCAL1 demo clean — FSCAL written, INTERFACE survived, measurement close to predicted

============================================================
  ALL CHECKPOINTS PASSED (cp0–cp3)
============================================================
```

### Interpretation cheatsheet for the cp2 table

- **pre_mean (uV)** — chip's raw input-referred offset at this gain before SFOCAL1. Should match the cp6 column from `ADS1263_FirstPowerUp_PIO/` (~740 µV at gain 1, scaling down with gain since the post-PGA offset divides by gain when input-referred).
- **OFCAL (predicted)** — what OFCAL "should" be analytically: `round(pre_mean_code / 256)` (the 24-bit OFCAL is left-shifted to align with the 32-bit ADC output, per datasheet §9.6.8).
- **OFCAL (actual)** — what SFOCAL1 actually wrote. Should match the prediction within a few LSB (the chip averages 16 readings during cal, so there's some statistical room).
- **INTERFACE** — must be `0x05` after the SFOCAL1 + register re-writes. `BAD` means the register snapped to a non-default value during calibration — production firmware would need to re-write it.
- **post_mean (uV)** — input-referred offset after cal. Should be tiny (single-digit µV at most).
- **reduction** — `1 − |post_mean|/|pre_mean|`. PASS threshold is 90 %; on a healthy chip we usually see 99 %+.

### Interpretation cheatsheet for cp3

**Important — what SYGCAL1 actually does** (datasheet §9.4.9.6, learned the hard way during 2026-05-24 bench-test):

> SYGCAL1 assumes "whatever input you have applied IS positive full-scale" and writes FSCAL such that the applied signal becomes **exactly +VREF** after cal. It does NOT try to make the post-cal reading equal the pre-cal reading.

So for our test (V_input ≈ 4.16 V, VREF = 5.0 V):

- **pre-cal measured** — what ADC1 reads with `FSCAL = 0x400000` (unity). Should be close to `0.8 × AVDD = 4.1645 V`, off by whatever the chip's intrinsic gain error is (usually a few hundred ppm).
- **FSCAL post-cal** — should equal `0x400000 × VREF / V_input_measured`. With V_input ≈ 4.167 V and VREF = 5.0 V, that's about `0x4CCB63` (1.200× gain factor). The chip writes this ratio so that the applied signal would be interpreted as +VREF.
- **FSCAL cross-check** — actual SYGCAL1 result vs the predicted-from-measured ratio. Should match within ±500 LSB (~120 ppm of FS); larger disagreement means SYGCAL1 is computing FSCAL with a different convention than expected.
- **post-cal measured** — should equal `+VREF = 5.0000 V` (because the chip has been told "this signal IS my new full-scale, normalize accordingly"). PASS threshold is ±1 % from VREF.
- **gain factor** — `FSCAL / 0x400000`. For an 0.8·AVDD input, expect ≈ 1.2; for an actual full-scale input, expect ≈ 1.0.

**This means SYGCAL1 in production firmware** only does what you want ("calibrate ADC to read true voltages") if you actually present a precision +VREF input. For this rig (REF7050 committed to AIN0/AIN1, no other 5 V precision source available), SYGCAL1's practical use is limited; **user calibration** (manually compute and write FSCAL from a known applied voltage) is the realistic path.

## Failure modes

| Symptom | Most-likely cause | What to check |
|---|---|---|
| `[cp 1] FAIL ADS1263 ID register reads wrong family` | Chip not alive on bus | Run `ADS1263_FirstPowerUp_PIO/` — its cp4 will localize. |
| `[cp 1] FAIL INTERFACE not at 0x05` | Chip not in default state | Power-cycle EVM completely (unplug 5 s, replug). |
| `[cp 1] FAIL VBIAS bit did not stick` | POWER register write failed | Run `ADS1263_FirstPowerUp_PIO/` cp6 — same triage. |
| `[cp 2] FAIL itf` at any row | INTERFACE register snapped to non-0x05 after SFOCAL1 | This is the legacy-HAT issue documented in [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) §6. The chip behavior is real; production firmware must defensively re-write INTERFACE after every calibration command. **This row failing isn't actually a chip problem — it's an expected behavior we need to know about.** |
| `[cp 2] FAIL noOFCAL` at any row | SFOCAL1 didn't write OFCAL | (1) Verify INPMUX was set to 0xFF before the command (datasheet §9.4.9.2 requires this); (2) increase the post-command delay from 200 ms; (3) check MODE2 readback at that gain matches what we wrote. |
| `[cp 2] FAIL nored` at any row | OFCAL was written but didn't actually cancel the offset | The OFCAL value is wrong direction or magnitude. Compare the actual vs predicted column. If they disagree, the chip may have done the cal in a different state than we set (e.g., PGA went to bypass during cal). |
| `[cp 3] FAIL INTERFACE snapped` | Same as cp2 `FAIL itf` — but at the SYGCAL1 stage | Same fix. |
| `[cp 3] FAIL FSCAL not modified` | SYGCAL1 didn't execute | Increase post-command delay (200 ms may be tight at lower DR2). Also confirm the TDAC is actually driving (cp10 in FirstPowerUp will tell you). |
| `[cp 3] FAIL FSCAL math` | SYGCAL1 wrote FSCAL but not at the expected ratio | Difference > ±500 LSB from `0x400000 × VREF / V_measured`. Either VREF isn't 5.0 V (verify REF7050 reaches AIN0/AIN1; cp5 in FirstPowerUp confirms this) or SYGCAL1 uses a different normalization convention than expected (re-check datasheet §9.4.9.6). |
| `[cp 3] FAIL post not at VREF` | FSCAL is correct but post-cal reading isn't ±1 % of VREF | Something is changing the signal between cal and re-measure: TDAC outputs slewed, register snap-back of TDAC enable bits, INPMUX changed. Confirm TDACP/TDACN are still 0x89 / 0x99 after the defensive re-writes. |

## What this sketch deliberately does NOT do

- **No long-term cal-drift study.** Cal-once-then-forget is what production firmware will do; this sketch is the verification, not the durability test.
- **No SYOCAL1 (system offset cal with external short).** SFOCAL1 (self offset cal, internal short) covers the same registers; the only difference is whether you want to subtract external bias too. We're not measuring real sensors yet, so no external bias to remove.
- **No ADC2 self-cal.** ADC2's SFOCAL2 / SYGCAL2 are analogous; deferred until Phase 3/4 firmware port when ADC2 is actually used in production.
- **No FIR/Sinc1/Sinc2/Sinc4 filter sweep.** All cps use Sinc3 (chip default), matching the production firmware target.

## File layout

```
ADS1263_SelfCal_PIO/
├── README.md          (this file)
├── STATUS.md
├── platformio.ini     M7-only single env
├── .gitignore
└── src/
    └── main.cpp       4 checkpoints, ~630 lines
```

## Related

- [`../doc/MEMO_baseline_testing.md`](../doc/MEMO_baseline_testing.md) — parent plan; this is its Phase 2.1
- [`../ADS1263_FirstPowerUp_PIO/`](../ADS1263_FirstPowerUp_PIO/) — prerequisite (cp0–cp10), source of `AVDD_KNOWN_V`
- [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) — §4 INTERFACE register behavior, §6 lessons-learned table (the "SFOCAL1 resets registers" entry)
- [`../doc/ADS1263_Datasheet.pdf`](../doc/ADS1263_Datasheet.pdf) — §9.4.9 (Calibration), §9.4.9.2 (SFOCAL1), §9.4.9.6 (SYGCAL1), §9.6.8 (OFCAL), §9.6.9 (FSCAL), Table 9-28 (calibration time vs SPS), Table 9-33 (command opcodes)
