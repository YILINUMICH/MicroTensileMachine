# MEMO — Cable map

**Status:** active — keep updated as the rig changes.
**Last edited:** 2026-05-24 by Yilin (named the external reference as TI REF7050; previously left as TBD on Cable 2).
**Previously edited:** 2026-05-22 by Yilin (added Cable 2 — external reference wiring after the first power-up bring-up established AIN0/AIN1 as the reference pair).

Operator-maintained record of every cable in the rig. Datasheets tell you what each connector *can* be; this file tells you what it actually *is* on this bench.

---

## Cable 1 — SPI bus: Portenta Mid Carrier ↔ ADS1263 EVM

The SPI bus and control lines between the Arduino Portenta H7 (on the Mid Carrier, ASX00055) and the **TI ADS1263 EVM** ADC board. Six discrete wires, four of them twisted as adjacent pairs to keep the high-edge-rate SPI signals from radiating onto the slower control lines.

This connection replaces the earlier Hat-Carrier + Waveshare-HAT setup (J5 40-pin Pi-compatible header). The firmware in `LoadCell_PIO/`, `LaserHead_PIO/`, and `Firmware_SensorHub_PIO/` still references the Hat-Carrier pin names — see [../TODO.md](../TODO.md) "Port firmware from Hat Carrier to Mid Carrier" for the open work to update the pin defines to match the table below.

> **Mid Carrier pinout reference:** [PortentaMidCarrier_ASX00055_Pinout.pdf](PortentaMidCarrier_ASX00055_Pinout.pdf)
> **EVM pinout reference:** [ADS1263_EVM_User_Guide.pdf](ADS1263_EVM_User_Guide.pdf) (J2 header)

| # | Signal | Direction | **Mid Carrier side** | Color | **ADS1263EVM side** | Twisted? | Notes |
|---|---|---|---|---|---|---|---|
| 1 | **SPI SCLK** | Carrier → EVM | **J15-20** (silkscreen `SPI1 SCLK`) | **Green** | J2-9 (silkscreen `SCLK`) | Yes | 500 kHz, SPI mode 1. Fastest edges → primary EMI aggressor. |
| 2 | **SPI COPI** (MOSI / DIN) | Carrier → EVM | **J15-24** (silkscreen `SPI1 COPI`) | Purple | J2-5 (silkscreen `DIN`) | Yes | Synchronous with SCLK. |
| 3 | **SPI CIPO** (MISO / DOUT) | EVM → Carrier | **J15-22** (silkscreen `SPI1 CIPO`) | Blue | J2-13 (silkscreen `DOUT/DRDY`) | Yes | In SPI-only mode it's pure MISO. |
| 4 | **/CS** (chip select) | Carrier → EVM | **J15-25** (silkscreen `PWM 0`) | Brown | J2-7 (silkscreen `/CS`) | Yes | GPIO-driven, not SPI1 hardware CS. |
| 5 | **/DRDY** (data ready) | EVM → Carrier | **J15-27** (silkscreen `PWM 1`) | Yellow | J2-11 (silkscreen `/DRDY`) | NO | Driver doesn't gate on this — timed polling — but we wire it so it's available. |
| 6 | **/RESET** | Carrier → EVM | **J15-29** (silkscreen `PWM 2`) | Black | J2-1 (silkscreen `/RESET`) | NO | Pulse low to reset; driver also issues the soft `RESET` SPI command. |

### Twisting plan

- **Pair A — SCLK + COPI** (green + purple): twisted. Both are carrier-driven, switched together, same edge rate.
- **Pair B — CIPO + /CS** (blue + brown): twisted. Keeps the EVM's return signal near its own activate line.
- **/DRDY and /RESET** run as individual wires (no twist). They change rarely (DRDY) or essentially never during a session (RESET), so the EMI argument doesn't apply.

### Things to verify if the link goes silent

1. Mechanical: are all six pins seated on both ends? The Mid Carrier `J15` is a 0.1″ female header — single-pin Dupont sockets can lift slightly under cable strain.
2. Continuity: ohm-meter each wire end-to-end with the cable disconnected from both boards. A cold solder joint on the Mid Carrier side was the actual cause during one Hat-Carrier bring-up (per [`../ADS1263/ADS1263_H7_Integration_Notes.md`](../ADS1263/ADS1263_H7_Integration_Notes.md) §Known Hardware Issue) and the same failure mode applies here.
3. Power: confirm the EVM's 3.3 V and 5 V rails are present at J2 before suspecting SPI. The EVM is self-powered; the carrier doesn't supply it.

---

## Cable 2 — External 5 V reference: bench supply ↔ ADS1263 EVM AIN0/AIN1

The ADS1263 EVM is configured for an **external 5 V reference** on this rig (not the chip's internal 2.5 V reference). The reference is fed into the AIN0/AIN1 pin pair on the EVM and selected in firmware via `REFMUX = 0x09` (RMUXP=001=External AIN0, RMUXN=001=External AIN1, per datasheet §9.6.12, Table 9-46).

> **Implication for measurement channels:** Once AIN0/AIN1 are committed to reference duty, **they cannot also be used as measurement inputs**. Any firmware that selects `INPMUX = 0x01` (or any encoding that picks AIN0/AIN1 as a measurement pair) is wrong on this rig. The load-cell channel — which the legacy Hat-Carrier setup had on AIN0/AIN1 — needs to move to a different AIN pair (see the open TODO below).

| # | Signal | Direction | **ADS1263 EVM pin** | Color | **Source side** | Notes |
|---|---|---|---|---|---|---|
| 1 | **+REF (5 V)** | Supply → EVM | AIN0 screw terminal | TBD (operator: fill in) | **TI REF7050** (5.000 V precision voltage reference IC). M-grade: 0.05% initial accuracy, 3 ppm/°C typical drift, ~5 µVpp 0.1–10 Hz noise. | Range per datasheet §9.3.8.2: 0.9 V to 5 V. We're at the upper bound. REF7050 noise is ~7× below the ADS1263's intrinsic noise floor at 400 SPS / PGA=1, so the reference is not the limiting factor for noise; thermal drift (~15 µV/°C on a 5 V ref) sets the long-term ceiling. |
| 2 | **−REF (return)** | EVM → Supply | AIN1 screw terminal | TBD | REF7050 ground / return | Differential pair with wire #1. |

### Bypass / hold-down components (per datasheet §9.3.8.2 and §9.3.8.4)

- **100 nF bypass capacitor across AIN0 ↔ AIN1**, mounted close to the EVM screw terminals. Filters reference noise into the on-chip reference buffer.
- **100 kΩ resistor across AIN0 ↔ AIN1.** If the reference cable is intermittently disconnected (operator wiring change, cold solder joint), this pull keeps the inputs biased relative to each other so the low-reference monitor (REF_ALM, status byte bit 4) doesn't false-trigger as a transient.

### Bench-verified

Bring-up on 2026-05-22 confirmed the reference is stable: with `INPMUX = 0xAA` (AINCOM-shorted) and PGA bypass at 400 SPS, ADC1 reads **mean = +0.708 mV, RMS = 1.416 µV** — well below the chip's intrinsic noise spec, indicating the external reference is clean and the reference buffer path is working. See [`../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260522_1726.log`](../ADS1263_FirstPowerUp_PIO/data/firstpowerup_20260522_1726.log).

### Things to verify if conversions look broken

1. Reference voltage present at AIN0/AIN1 with a multimeter — should be **+5.000 V ± a few mV** referenced to EVM AGND.
2. The 100 nF bypass cap is actually installed and not shorted/open.
3. `REFMUX` reads back as `0x09` after firmware writes it (the `ADS1263_FirstPowerUp_PIO` cp5 register-readback check catches this).
4. If the low-reference alarm (REF_ALM, status byte bit 4) triggers, the differential VREFP − VREFN has fallen below ~0.4 V — check the supply and the cable.

---

## Other cables (to be documented)

Add an entry per cable as the rig is wired up. Suggested order of importance:

- [ ] ~~**Load cell → LCA-9PC amplifier → ADS1263 EVM AIN0/AIN1.**~~ **Superseded** — AIN0/AIN1 are now the external 5 V reference inputs (see Cable 2 above). The load cell needs a new AIN pair. Candidates: AIN2/AIN3 (worked on the EVM during bring-up? — actually unknown, still TODO to test ADC2's AIN2/AIN3 — see [`../TODO.md`](../TODO.md)), or AIN4/AIN5. **Decide and document.**
- [ ] **Keyence IL-030 controller → ADS1263 EVM AIN?/AIN?** — Channel assignment pending the load-cell decision above. Analog signal out (white) and analog ground (shield) per the IL-Series manual. Note the V↔mm scaling currently configured on the IL-030 controller (1 V/mm default; confirm what's actually set).
- [ ] **DC actuation supply → bias-tee DC port → SMA DUT pigtails.** Polarity, current limit setting, supply ground reference.
- [ ] **Bias-tee AC port → Keysight E4980AL front terminals.** Cable type (BNC? banana?), length (matters for the SHORT de-embedding stability per Notion §4.2).
- [ ] **USB cables.** Which physical USB-A port on the host PC each instrument uses. **Current host (Yilin's Windows machine, 2026-05-25):**
    - **COM5** — Zaber **X-LSQ300A-E01** stage, serial 143153, firmware 7.48.24004 (FTDI bridge, USB VID:PID = `0403:6001`). 300 mm travel, built-in encoder. Confirmed from live device 2026-05-27.
    - **COM13** — Portenta H7 (Arduino CDC, USB VID:PID = `2341:025B` in normal mode; `2341:035B` in DFU mode). **Was COM8 until 2026-07-24**, when the original H7 was replaced (its M7 flash was left partially written by a DFU download failure). The replacement enumerated on COM13; every `platformio.ini` and module `config.yaml` was repointed. If you are reading an older session log or STATUS entry that says COM8, it means this same H7 port, on the previous board.
    - `Firmware_SensorHub_PIO/platformio.ini` pins `upload_port = COM13` and `monitor_port = COM13` (in a shared `[env]` block) so PIO doesn't grab the Zaber by COM-number race. COM-numbers are Windows-specific and may renumber when the H7 is replugged into a different USB-A port — if that happens, check `pio device list`, update the .ini, or override at the CLI with `--upload-port COMx`. The VID:PIDs above are stable across hosts; PIO's `upload_port` is a glob over port paths and doesn't accept VID:PID directly.

---

## Conventions

- One H2 section per cable. Title format: `## Cable N — <function>: <source> ↔ <sink>`.
- If you re-pin or re-color, update the row in place and add a one-line note under the table saying when/why.
- If a cable is removed, **don't delete the section** — strike it through and add a "Removed YYYY-MM-DD" note. The history matters more than tidiness.
