# SensorHub_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **To-Test (post-swap)** — bench-verified on Mid Carrier + bare TI EVM on 2026-05-25 with the previous ADC↔sensor pairing; ADC roles swapped on **2026-05-28** after `Calibrate_LaserHead/` and `Calibrate_LoadCell/` cross-compare results. Re-verification on the bench with the swapped firmware is pending. Flips to **Stable** after `SMA_CharacterizationV2/` consumes this stream end-to-end. |
| **Role** | Current production firmware target — dual-ADC (laser on **ADC1/AIN4-AIN5** + load cell on **ADC2/AIN2-AIN3**) on Portenta H7 M4 core |
| **Supersedes** | `LoadCell_PIO/`, `LaserHead_PIO/` (kept as diagnostic reference builds) |
| **Last verified** | 2026-05-25 (dual-stream on Mid Carrier + EVM, **pre-swap** pairing). Config: REF7050 5.0 V external on AIN0/AIN1 (REFMUX=0x09, REF2=001b); 400 SPS both; Sinc3 filter; INTERFACE=0x05 (STATUS + CHK on); RDATA2 6-byte frame including 00h zero-pad. EVM AVDD ≈ 5.2 V. Current production code now runs ADC1 PGA-in-path gain=1 on AIN4/AIN5 (laser), ADC2 gain=1 on AIN2/AIN3 (load) — re-verify before flipping to Stable. |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7_bridge -t upload` then `pio run -e portenta_m4 -t upload`, power-cycle the rig (USB + EVM supply), `pio device monitor` @ 115200. Expected: `ID=0x23`, dual stream with `src=1` (**laser**) and `src=2` (**load**) at ~3 ms intervals each, no alarm lines once wiring is settled. |

## Driver bugs caught during 2026-05-25 bring-up

Two independent bugs in `lib/ADS1263/ADS1263_Driver.cpp`, both ADC2-side, both fixed:

1. **RDATA2 frame layout** (`readRawData24`). The driver was clocking out 5 bytes after `CMD_RDATA2` (`STATUS | D3 | D2 | D1 | CHK`). Per datasheet §9.4.7.2 Figure 9-44 and §9.4.7.3, the chip emits **6 bytes** for ADC2 — there's a fixed `0x00` zero-pad byte between the 24-bit data and the CHK byte so the ADC2 frame lines up with the 6-byte ADC1 frame. Symptom: every ADC2 read was flagged invalid because the byte the driver called "CHK" was actually the zero-pad (always `0x00`), and the real CHK byte rolled into the next transaction. Fix: read the extra byte and exclude it from the checksum sum.
2. **ADC2CFG bit-field swap** (`writeADC2CFG`). The driver packed the register as `DR2 | GAIN2 | REF2`; datasheet §9.6 Table 9-52 specifies `DR2[7:6] | REF2[5:3] | GAIN2[2:0]`. The swap silently ran ADC2 on its **internal 2.5 V reference at 2× gain** while the host code thought it was on REF7050 at 1×. Symptom: ADC2 hard-saturates at `0x7FFFFF` on any input ≥ +1.25 V differential, and the driver's volts-per-code math (using the *intended* 5 V / 1×) reports a clean `+5.000000 V`. Hit the laser channel (+2.5 V actual) exactly. Fix: swap the bit shifts in `writeADC2CFG`.

The two bugs masked each other: with only fix #1 applied you'd get clean checksums but `+5.000000 V` instead of `+2.5 V`; with only fix #2 you'd get the right voltage but every read marked invalid. Diagnosis required hitting both. See [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) §4 (both 2026-05-25 addenda) for the datasheet refs.

## Module TODOs

- [x] ~~**Port pin defines to Mid Carrier**~~ — done 2026-05-25, bench-verified.
- [x] ~~**Re-verify SPI mapping**~~ — done 2026-05-25, bench-verified.
- [x] ~~**Resolve ADC2/AIN2-AIN3 saturation**~~ — RETIRED 2026-05-24 by cp7 in `ADS1263_FirstPowerUp_PIO/`. Production assignment **post-swap (2026-05-28)**: AIN4/5 (laser, ADC1) + AIN2/3 (load, ADC2), both pin pairs confirmed clean.
- [x] ~~**Bench-verify both streams concurrently**~~ — done 2026-05-25 with pre-swap pairing. Both `src=1` and `src=2` lines arrive at ~333 lines/s each (700 lines/s combined) over the USB CDC bridge with no cross-talk and no checksum errors.
- [ ] **Re-verify both streams after the 2026-05-28 ADC↔sensor swap.** Same expected line rate; confirm `src=1` now tracks the laser (AIN4/5) and `src=2` tracks the load cell (AIN2/3).
- [x] ~~**Update the `## Wiring` and `## Expected boot output` sections of `README.md`**~~ — done 2026-05-25; README reflects the bench-derived production config.
- [ ] **Re-calibrate the laser head** on the bare EVM (deferred from this session). The legacy `k = -0.1171 mV/µm`, `V₀ = 566.957 mV` constants in `SMA_CharacterizationV2/` came through the Waveshare HAT's ~4.4× input attenuator and are invalid on the EVM. Run `Calibrate_LaserHead/` with this firmware, then update the defaults. Tracked in [../TODO.md](../TODO.md).
- [ ] **Smoke-test `SMA_CharacterizationV2/`** against this stream. When the recorder consumes both `src=1` and `src=2` cleanly across an OPEN→SHORT→RAW session, flip this module's status To-Test → Stable.
- [ ] **Remove duplicate datasheets from `doc/`** once `README.md` links back to `../doc/` instead. (Forward-looking hygiene item; no local `doc/` exists under `SensorHub_PIO/` yet, but the parent TODO carries this.)
- [ ] **Port the two ADC2 driver fixes into `LaserHead_PIO/lib/ADS1263/`** when that module is ported to the Mid Carrier. Both bugs live there too — same driver lineage.

See [../TODO.md](../TODO.md) for cross-cutting items.
