# PLAN — Phase 4: Production firmware port

> **Self-contained handoff doc.** Designed to be picked up by a fresh AI agent or operator who has not seen the prior conversation.

**Status:** **deferred** — gated on Phase 3 (sensor configuration) completion.

**Owner:** Yilin.

**Last edited:** 2026-05-24.

---

## TL;DR

Port the production firmware (`SensorHub_PIO/`, `LoadCell_PIO/`, `LaserHead_PIO/`) from the legacy Hat Carrier setup to the current Mid Carrier + ADS1263 EVM setup, validate it works end-to-end, then archive the single-path modules that `SensorHub_PIO/` supersedes.

Three sub-tasks:

| # | Sub-task | Effort | Risk |
|---|---|---|---|
| 4.1 | Port pin defines + chip configuration in `SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h` | 30 min code + test | low — well-understood mapping |
| 4.2 | Bench-verify `SensorHub_PIO/` with both sensors live | 1–2 hours | medium — dual-core + dual-ADC + RPC; failures here can be hard to localize |
| 4.3 | Move `LoadCell_PIO/` and `LaserHead_PIO/` into `Archieve/` | 15 min | low — paperwork |

---

## Why this is the last phase

`SensorHub_PIO/` is the convergence point — it's the production firmware that pulls together everything from Phase 1–3. It cannot land cleanly until:

- The chip is characterized (Phase 1) so the operating mode it uses is *known good*.
- Self-calibration is verified (Phase 2.1) so offset/gain trim is *known good*.
- Both sensors are wired and calibrated (Phase 3) so there's something to read.

If you start `SensorHub_PIO/` debugging before any of the above is settled, every bug becomes ambiguous: is it the chip config, the calibration, the firmware, or the sensor?

---

## Prerequisites

- All of Phase 1 (`MEMO_baseline_testing.md` rows 1.1–1.6 ticked) — chip operating modes characterized.
- Phase 2.1 (self-calibration verification) — calibration registers captured for the AIN pairs in use.
- Phase 3 (sensor configuration) — both sensors wired, both calibrations recorded in `MEMO_sensor_setup.md`.

If any of the above is incomplete, **stop and finish that first**. Don't try to do Phase 4 against an unverified chip / sensor stack.

---

## Background — what `SensorHub_PIO/` is and why this port matters

(For a fresh agent: read [`../SensorHub_PIO/README.md`](../SensorHub_PIO/README.md) and [`../SensorHub_PIO/STATUS.md`](../SensorHub_PIO/STATUS.md) before doing anything.)

`SensorHub_PIO/` is the **dual-ADC, dual-core production firmware**:

- **M4 core (the Cortex-M4 in the H7's dual-core MCU)** runs the ADC sample loop. It reads ADC1 (load cell) and ADC2 (laser) at independent rates, packs each sample with a source tag (`src` column = "L" for load, "D" for displacement, etc.).
- **M7 core** runs the USB CDC serial bridge that the host PC talks to. M4 → M7 sample transport is currently per-sample RPC (slow; a shared-SRAM ring buffer is a future optimization tracked in `LoadCell_PIO/README.md`).
- Replaces two predecessor builds (`LoadCell_PIO/`, `LaserHead_PIO/`) that each ran only one ADC. Both predecessors are kept around as bring-up isolation builds and will be archived after `SensorHub_PIO/` is bench-verified.

The firmware as it currently sits was written for the **legacy Portenta Hat Carrier (ASX00049) + Waveshare ADS1263 HAT**. Pin defines, register configuration, and one specific known issue (the DRDY-on-PJ_11 LoRa conflict) all reflect the legacy setup. The rig has since moved to the **Portenta Mid Carrier (ASX00055) + bare TI ADS1263 EVM**. None of the firmware has been ported.

---

## Sub-task 4.1 — Pin defines and chip configuration

**File to edit:** [`../SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h`](../SensorHub_PIO/lib/ADS1263/ADS1263_Driver.h) (and any `.cpp` that hard-codes the same values).

**Source of truth for the new values:** [`../ADS1263_FirstPowerUp_PIO/STATUS.md`](../ADS1263_FirstPowerUp_PIO/STATUS.md) — the "What the bring-up established" table.

**The mapping (Hat Carrier → Mid Carrier):**

| Macro | Hat Carrier (legacy) | Mid Carrier (current) | Notes |
|---|---|---|---|
| `ADS1263_CS_PIN`    | `PE_6` (J2-53)  | **`PA_8`**  (J15-25) | |
| `ADS1263_DRDY_PIN`  | `PJ_11` (J2-50) | **`PC_6`**  (J15-27) | **`PJ_11` had a LoRa-IRQ conflict** — `PC_6` doesn't. If `PLAN_phase1_followups.md` cp9 passed, switch from timed polling back to DRDY interrupts. |
| `ADS1263_RESET_PIN` | `PI_5` (J1-56)  | **`PC_7`**  (J15-29) | |
| SPI                 | default SPI object | same — default SPI object on the Mid Carrier | |

**Chip-side configuration changes (apply in `ADS1263_Driver` init code):**

| Setting | Old value | New value | Why |
|---|---|---|---|
| `REFMUX` | `0x00` (internal 2.5 V) | **`0x09`** (external 5 V on AIN0/AIN1) | The REF7050 is wired in. See [`MEMO_cable_map.md`](MEMO_cable_map.md) Cable 2. |
| `VREF` constant in volts-per-code math | `2.5 V` | **`5.0 V`** | Match the new reference. Code: `V = code * VREF / 2^31` for ADC1, `V = code * VREF / 2^23` for ADC2. |
| `POWER` | `0x11` (INTREF on) | **`0x13`** (INTREF + VBIAS) | VBIAS needed for any PGA gain > 1. Even if production firmware uses PGA=1, leave VBIAS on for safety. |
| ADC1 INPMUX | `0x01` (AIN0/AIN1) | **Phase 3 result** — likely `0x23` (AIN2/AIN3) | AIN0/AIN1 are reference. |
| ADC2 ADC2MUX | TBD (legacy) | **Phase 3 result** — likely `0x45` (AIN4/AIN5) | Whatever Phase 3 settled on for the laser. |
| ADC2CFG REF2 field | (whatever the legacy had) | **`001`** (external AIN0/1) | ADC2 shares the REF7050. Full ADC2CFG byte typically `0x48` (DR2=01=100 SPS, REF2=001, GAIN2=000). |

**DRDY strategy:**
- If `PLAN_phase1_followups.md` cp9 confirmed DRDY edge-counts cleanly on `PC_6` (10 s count = 4000 ± 40 at 400 SPS), switch the M4 reader to **edge-triggered** via `attachInterrupt(digitalPinToInterrupt(PC_6), isr, FALLING)`.
- If cp9 failed, stick with timed polling (5 ms delay at 400 SPS). This will work but caps throughput at ~200 effective SPS — fine for sub-100 Hz mechanical signals but a future TODO.

**Verification of pin-define change:** before flashing, grep the codebase for the old macro values (`PE_6`, `PJ_11`, `PI_5`) to make sure no stale reference remains. Also check `LoadCell_PIO/` and `LaserHead_PIO/` — if you're updating `SensorHub_PIO/` they're the next two to update if they're still around (but they're being archived in 4.3 so don't bother unless they're needed for fallback).

**Acceptance:** `pio run` succeeds in `SensorHub_PIO/` with the new pin defines. Verify by setting up an ASCII art comment or doc comment that says "Mid Carrier / EVM" at the top of `ADS1263_Driver.h` so anyone reading can immediately tell which generation of pin defines applies.

---

## Sub-task 4.2 — Bench-verify `SensorHub_PIO/` with both sensors

**Goal:** the dual-ADC firmware streams correct readings for both sensors over USB serial.

**Procedure:**

1. **Pre-flight.** Confirm `MEMO_cable_map.md` is fully updated (Cables 3 and 4 for sensors). Confirm both sensors are mechanically mounted and powered.

2. **Flash.** `pio run -t upload` in `SensorHub_PIO/`. Power-cycle the EVM after flashing.

3. **Boot-time validation.** The firmware should print a banner and a register-readback dump. Verify:
   - ADS1263 ID = `0x23` (silicon rev 3).
   - REFMUX = `0x09`.
   - ADC2CFG REF2 field = `001`.
   - INPMUX / ADC2MUX match the Phase 3 pair assignments.

4. **Static read test.** With no load and the laser at its calibration reference distance (30 mm), capture 30 s of samples. Expected:
   - Load channel: voltage ≈ V₀ from Phase 3.2 (no load), converted to force ≈ 0 N.
   - Displacement channel: voltage ≈ wherever the IL-030 sits at 30 mm reference, converted to displacement ≈ 0 µm (or whatever the calibration zero is set to).
   - Both channels stable to within Phase 1.2 noise floor.

5. **Dynamic test.** Apply a known load (a calibration weight). Verify load channel responds within calibrated tolerance. Move the Zaber stage by a known displacement. Verify displacement channel responds within calibrated tolerance. Both channels should respond simultaneously without one stalling or dropping samples.

6. **Sample rate verification.** Configured rates vs actual rates per channel — count samples per second in the host-side CSV output, compare to the firmware's configured SPS. They should match within 1%.

7. **End-to-end SMA characterization.** Run a short SMA characterization via `SMA_CharacterizationV2/sma_recorder.py`. Confirm the resulting log file has both channels populated with sensible values, the analyzer (`analyze_sma.py`) produces a sensible plot.

**Failure modes (and what to investigate):**

| Symptom | Most-likely cause | What to check |
|---|---|---|
| Firmware doesn't compile | Stale pin reference or library mismatch | Grep for old pin macros; check `platformio.ini` for the right `framework` and `board`. |
| Banner prints but ID register reads `0x00` or `0xFF` | SPI bus broken | Re-run `ADS1263_FirstPowerUp_PIO/` to localize — its cp triage is more targeted than `SensorHub_PIO/`'s. |
| Load channel reads garbage but laser channel is fine (or vice versa) | INPMUX / ADC2MUX confusion | Verify the AIN pair assignments in the driver match `MEMO_cable_map.md` Cables 3 / 4. |
| Sample rate is roughly half of configured | DRDY-vs-polling mode confusion | Check whether the driver gates reads on DRDY or runs free; check that DRDY is wired and edge-counts cleanly (Phase 1.5). |
| Load reads 4.4× too small (or too large) | Old laser calibration constants still in software somewhere | Grep `SMA_CharacterizationV2/` for `0.1171` and `566.957`; the new constants from Phase 3.3 should replace them everywhere. |
| One channel works briefly then dies | RPC starvation / M4-M7 transport issue | Open `SensorHub_PIO/` source — known optimization TODO is the shared-SRAM ring buffer to replace per-sample RPC. May or may not be load-bearing for this rig depending on rates. |

**Acceptance:** SMA recorder runs to completion, log file has both channels, characterization plot looks like prior runs (within calibration drift). Bench log saved under `SensorHub_PIO/data/` with date and operator.

---

## Sub-task 4.3 — Archive the superseded single-path modules

**Files to move:**

```
LoadCell_PIO/   → Archieve/LoadCell_PIO/
LaserHead_PIO/  → Archieve/LaserHead_PIO/
```

(Note the misspelling `Archieve/` — that's deliberate per the existing repo convention. Don't fix the spelling here; doing so would break `sys.path` shims elsewhere. There's a separate hygiene TODO in `TODO.md` to address the rename comprehensively.)

**Steps:**

1. Confirm `SensorHub_PIO/` has bench-verified end-to-end and the operator is confident it's the production target.
2. `git mv LoadCell_PIO Archieve/LoadCell_PIO` and same for `LaserHead_PIO`.
3. Each module's `STATUS.md` gets an "Archived YYYY-MM-DD — superseded by `SensorHub_PIO/`" banner.
4. Update the root [`../README.md`](../README.md) module table — move both rows from the firmware section to the archive section.
5. Update the [`../TODO.md`](../TODO.md) — strike through the "Retire `LoadCell_PIO/` and `LaserHead_PIO/`" item with a date and a pointer to the relocation commit.
6. Grep the codebase for any `import` / `#include` / path reference to `LoadCell_PIO/` or `LaserHead_PIO/` from outside their own directories. Update to the new `Archieve/` path or remove if dead.

**Acceptance:** root README's firmware section no longer lists the archived modules; both have "Archived" status in their `STATUS.md`; no stale references in active code.

---

## What Phase 4 does NOT include

- **Shared-SRAM ring buffer** between M4 and M7 (a known performance follow-up; tracked in `LoadCell_PIO/README.md` → still applicable after the move to `Archieve/`).
- **Ethernet streaming** off the M7 — long-term goal; not in this PLAN.
- **Production-mode laser linearity verification across full ±5 mm range** — `LaserHead_PIO/README.md` §Next-steps item 1 covers this; currently only verified at a single bench position. Could be added to Phase 3.3 or carried as a follow-up.
- **The 100 → ~42 SPS ADC2 discrepancy investigation** (TODO in root) — pre-existing oddity, may or may not still apply on the EVM. Worth re-checking after Phase 1.4 (ADC2 cp); if it persists, separate investigation.

---

## References

- [`MEMO_baseline_testing.md`](MEMO_baseline_testing.md) — parent plan; this PLAN implements its Phase 4.
- [`../SensorHub_PIO/STATUS.md`](../SensorHub_PIO/STATUS.md), [`../SensorHub_PIO/README.md`](../SensorHub_PIO/README.md) — module documentation.
- [`../ADS1263_FirstPowerUp_PIO/STATUS.md`](../ADS1263_FirstPowerUp_PIO/STATUS.md) — source of truth for the Mid Carrier pin defines and chip configuration.
- [`MEMO_cable_map.md`](MEMO_cable_map.md) — wiring; Cables 3 and 4 (sensors) will exist after Phase 3.
- [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) §5 — historical context on the DRDY/LoRa conflict.
- [`../TODO.md`](../TODO.md) — cross-cutting items, especially "Port firmware from Hat Carrier to Mid Carrier".
- `PLAN_phase1_followups.md`, `PLAN_phase3_sensors.md` — prerequisite PLANs.
