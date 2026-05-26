# ADS1263 — STATUS

| Field | Value |
|---|---|
| **Status** | **Diagnostic** — Arduino-IDE era test sketches and the canonical integration notes |
| **Role** | Historical bring-up bench plus the authoritative reference doc (`ADS1263_H7_Integration_Notes.md`). The MD file is still load-bearing: register configuration, pin-mapping methodology, Test B noise results, the DRDY/LoRa conflict explanation. |
| **Superseded by** | The PlatformIO firmware (`SensorHub_PIO/`, `LaserHead_PIO/`, `LoadCell_PIO/`) for active development. The notes here are still authoritative. |
| **Last verified** | Test B passed March 2026 — 5.28 µV RMS, 17.13 noise-free bits at 400 SPS |
| **Owner** | Yilin |
| **Sketches inside** | `Stable/`, `LoadCell/`, `TestA_DC_Accuracy/`, `TestB_Noise/`, `TestC_AC_Capture/`, `TestD_DRDY/`, `TestE_ExtRef/`, `SPI_Diagnostic/`, `SPI_Loopback/`, `PinScanner/`, `D22_Blink/` |

## Module TODOs

- [ ] **Treat `ADS1263_H7_Integration_Notes.md` as read-only documentation** — keep it accurate as the rig evolves. Specifically: the §2 pin-mapping table and §5 DRDY conflict will need a note when the Mid Carrier port lands.
- [ ] **Decide which test sketches to keep, archive, or delete.** TestA–E carried bring-up; not all of them are still useful. Candidates for archival: `D22_Blink/`, `PinScanner/`, `SPI_Loopback/` (one-time bring-up artifacts).

See [../TODO.md](../TODO.md) for cross-cutting items.
