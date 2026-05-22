# MEMO — Cable map

**Status:** active — keep updated as the rig changes.
**Last edited:** 2026-05-22 by Yilin.

Operator-maintained record of every cable in the rig. Datasheets tell you what each connector *can* be; this file tells you what it actually *is* on this bench.

---

## Cable 1 — SPI bus: Portenta Mid Carrier ↔ ADS1263 EVM

The SPI bus and control lines between the Arduino Portenta H7 (on the Mid Carrier, ASX00055) and the **TI ADS1263 EVM** ADC board. Six discrete wires, four of them twisted as adjacent pairs to keep the high-edge-rate SPI signals from radiating onto the slower control lines.

This connection replaces the earlier Hat-Carrier + Waveshare-HAT setup (J5 40-pin Pi-compatible header). The firmware in `LoadCell_PIO/`, `LaserHead_PIO/`, and `SensorHub_PIO/` still references the Hat-Carrier pin names — see [../TODO.md](../TODO.md) "Port firmware from Hat Carrier to Mid Carrier" for the open work to update the pin defines to match the table below.

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

## Other cables (to be documented)

Add an entry per cable as the rig is wired up. Suggested order of importance:

- [ ] **Load cell → LCA-9PC amplifier → ADS1263 EVM AIN0/AIN1.** Signal pair + amplifier excitation. Document amplifier supply rails and which screw terminal on the EVM the signal lands on.
- [ ] **Keyence IL-030 controller → ADS1263 EVM AIN2/AIN3.** Analog signal out (white) and analog ground (shield) per the IL-Series manual. Note the V↔mm scaling currently configured on the IL-030 controller (1 V/mm default; confirm what's actually set).
- [ ] **DC actuation supply → bias-tee DC port → SMA DUT pigtails.** Polarity, current limit setting, supply ground reference.
- [ ] **Bias-tee AC port → Keysight E4980AL front terminals.** Cable type (BNC? banana?), length (matters for the SHORT de-embedding stability per Notion §4.2).
- [ ] **USB cables.** Which physical USB-A port on the host PC each instrument uses, since some workflows hard-code `COM5` (Zaber) / `COM8` (H7) — those COM-port assignments are Windows-specific to the current host.

---

## Conventions

- One H2 section per cable. Title format: `## Cable N — <function>: <source> ↔ <sink>`.
- If you re-pin or re-color, update the row in place and add a one-line note under the table saying when/why.
- If a cable is removed, **don't delete the section** — strike it through and add a "Removed YYYY-MM-DD" note. The history matters more than tidiness.
