# LoadCell_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **Diagnostic** — single-path reference build (ADC1 / load cell only) on the old Hat Carrier |
| **Role** | Bring-up and isolation tool. Original "Step 1" data-path firmware that proved the M4-on-Portenta + ADS1263 path end-to-end. |
| **Superseded by** | `SensorHub_PIO/` for production use |
| **Last verified** | April 2026 on Hat Carrier — see `README.md` §Status (clean boot, ~55 ms cadence at 20 SPS, ~5 µV jitter with AIN0/AIN1 floating) |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7_bridge -t upload`, `pio run -e portenta_m4 -t upload`, power-cycle Hat Carrier, `pio device monitor` @ 115200. Expect `ID=0x23`, `streaming. format: t_ms\traw_code\tvoltage_V`. |

## Module TODOs

- [ ] **Mid Carrier port** if this module is going to keep being used; otherwise mark Archived once `SensorHub_PIO/` is bench-verified.
- [ ] **Sanity-check the scaling** — short AIN0↔AIN1 and confirm output collapses to ~0 V with ~5 µV RMS. Then apply known DC (0–5 V) via the LCA and confirm linearity. Verifies the external-5V-reference path (`REFMUX = 0x24`) introduced in this build.
- [ ] **Replace RPC transport with shared-SRAM ring buffer** (SRAM4, non-cacheable, 32-bit head/tail + `__DMB()` barriers). Needed before pushing the sample rate above ~1 kSPS.
- [ ] **Reroute DRDY off `PJ_11`** to a free GPIO, switch back to interrupt-driven reads. **Re-evaluate after Mid Carrier port** — the LoRa-on-`PJ_11` conflict may not still apply.
- [ ] **Re-introduce user commands (tare, calibrate, switch ref) on M7**, forwarded to M4 via a tiny command word in shared SRAM.

See [../TODO.md](../TODO.md) for cross-cutting items.
