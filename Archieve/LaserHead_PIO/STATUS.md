# LaserHead_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **Diagnostic** — single-path reference build (ADC2 / laser only) on the old Hat Carrier |
| **Role** | Bring-up and isolation tool. Lets you bisect "is the problem the laser path or the load-cell path?" by flashing just one ADC at a time. |
| **Superseded by** | `SensorHub_PIO/` for production use |
| **Last verified** | April 2026 on Hat Carrier — see `README.md` §Status |
| **Known quirks** | At `ADS1263_ADC2_100SPS`, the chip's output register only updates every ~24 ms (effective ~42 SPS, not 100). Not blocking for displacement measurement. |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7_bridge -t upload`, `pio run -e portenta_m4 -t upload`, power-cycle Hat Carrier, `pio device monitor` @ 115200. Expect `ID=0x23` and tab-separated samples. |

## Module TODOs

- [ ] **Mid Carrier port** if this module is going to keep being used; otherwise mark Archived once `SensorHub_PIO/` is bench-verified on Mid Carrier. (Cheaper to fix in `SensorHub_PIO/` first and then either port-then-retire here or just retire.)
- [ ] **Validate readings against physical setup** — point IL-030 at known distances and confirm linearity. (Inherited goal; not done yet.)
- [ ] **Encode the laser calibration curve in firmware or `main.cpp`** so the stream carries µm/mm instead of raw volts.
- [ ] **Investigate 100 → ~42 SPS discrepancy.** Try 400 / 800 SPS, see whether the "value printed twice" pattern scales with rate or is fixed at ~24 ms.

See [../TODO.md](../TODO.md) for cross-cutting items.
