# Firmware_stable — known-good pre-cleanup SensorHub baseline

Exact snapshot of `SensorHub_PIO` at git commit **09f0502**
("Refactor SensorHub_PIO for dual-ADC operation and implement lock-free ring buffer"),
the last dual-ADC firmware before the Phase-6 SMA merge / refactor.

Why it exists: diagnostic A/B. The current `Firmware_SMASensorHub_PIO` reads the
laser frozen (value latches at the boot-time input). This is the pre-cleanup
version, used to confirm whether the laser tracked before the merge.

Key facts (verified):
- `src/main.cpp` is byte-identical to the operator's archived "known-good" copy.
- ADS1263 driver writes **POWER = 0x13** (INTREF + VBIAS on) — the value the
  later commit 093469b changed to 0x02.
- ADC1 = laser on AIN4/AIN5 (INPMUX 0x45); ADC2 = load on AIN2/AIN3.
- Reads via `readADC1Direct()` / `readADC2Direct()` (no DRDY ISR, no SMA driver).
- External REF7050 on AIN0/AIN1 (REFMUX 0x09).

Flash (power-cycle USB + EVM after each upload):
    pio run -e portenta_m7_bridge -t upload    # once
    pio run -e portenta_m4        -t upload
    pio device monitor                         # 115200; expect ID=0x23

Stream format (single src column per line): t_ms \t src \t raw \t V   (src=1 laser, 2 load)
