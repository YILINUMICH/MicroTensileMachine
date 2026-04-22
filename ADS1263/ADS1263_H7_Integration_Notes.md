# ADS1263 Waveshare HAT + Arduino Portenta H7 Integration Notes

**Date:** March 2026  
**Hardware:** Waveshare High-Precision AD HAT (ADS1263) + Arduino Portenta H7 + Portenta Hat Carrier (ASX00049)  
**Status:** Test B passed — 5.28 µV RMS, 17.13 noise-free bits at 400 SPS

---

## 1. Hardware Setup

### What Works
The Waveshare ADS1263 HAT plugs directly onto the Portenta Hat Carrier's J5 40-pin Pi-compatible header. This is the correct and only reliable way to connect the HAT — Dupont cables to an Arduino Uno introduce enough signal integrity degradation to make the ADC functionally unusable (see Section 5).

### Power
The HAT is self-powered:
- **AVDD (5V):** from Pi header pin 2/4 — supplied by Hat Carrier
- **DVDD (3.3V):** from onboard AMS1117-3.3 LDO on the HAT, fed from the 5V rail. Pin 1/17 (3.3V from Portenta) is **not** used for DVDD.
- The Hat Carrier must be powered via its J9 screw terminal (7-32V) or USB-C. USB-C alone from a laptop may be insufficient during upload.

### Known Hardware Issue
**Cold solder joint on the HAT's 40-pin header** caused intermittent ID detection (0x00 response). The ADS1263 would occasionally enumerate correctly and then disappear. If you see the ID reading correctly once then failing, inspect and reflow the HAT's 40-pin female header solder joints before debugging software.

---

## 2. Pin Mapping

The Portenta H7 Arduino core uses STM32 port names (`PE_6`, `PJ_11`, etc.), not Arduino D-pin numbers for HD connector pins. D15–D18 are explicitly marked `#error` in `pins_arduino.h` and cannot be used as digital pins.

| Signal | STM32 Pin | HD Connector | Pi Header Pin | BCM GPIO |
|--------|-----------|--------------|---------------|----------|
| CS | `PE_6` | J2-53 (SAI D0) | 15 | GPIO22 |
| DRDY | `PJ_11` | J2-50 (GPIO 2) | 11 | GPIO17 |
| RESET | `PI_5` | J1-56 (I2S MCK) | 12 | GPIO18 |
| MOSI | `PC_3` (D8) | J2-42 | 19 | GPIO10 |
| MISO | `PC_2` (D10) | J2-40 | 21 | GPIO9 |
| SCK | `PI_1` (D9) | J2-38 | 23 | GPIO11 |

**Key insight:** The Hat Carrier datasheet uses functional names (SPI1_CS, GPIO2, SAI_D0), which must be traced through the H7 HD connector schematic to get the actual STM32 port names. The Arduino D-pin numbers only cover D0–D14 and selected analog pins — most HD connector signals require raw STM32 port names.

### How to Find Unknown Pin Mappings
If a functional name doesn't compile, look it up in:
```
AppData\Local\Arduino15\packages\arduino\hardware\mbed_portenta\<ver>\variants\PORTENTA_H7_M7\variant.cpp
```
This file lists the complete `g_APinDescription[]` table mapping every STM32 port to its Arduino pin index.

---

## 3. SPI Configuration

```cpp
// Correct SPI settings for ADS1263 on Portenta H7
SPISettings spiSettings(500000, MSBFIRST, SPI_MODE1);

// Use the default SPI object — it maps to D7-D10 (J2-36/38/40/42)
// which is exactly the Hat Carrier J5 SPI bus
SPI.begin();
```

**SPI speed:** 500 kHz works reliably. The Waveshare Pi library uses 10 MHz but that requires the Pi's direct PCB connection. 500 kHz is appropriate for HAT-on-carrier use.

**SPI mode:** MODE1 (CPOL=0, CPHA=1) — confirmed working.

---

## 4. ADS1263 Register Configuration

The Waveshare HAT ships with `INTERFACE = 0x05` as its power-on default, meaning the chip **always prepends a STATUS byte and appends a CRC byte** to every conversion result. Every SPI read must account for this:

```
Transaction: CMD_RDATA1 → STATUS (1 byte) → DATA (4 bytes) → CRC (1 byte)
Total: 6 bytes read after sending CMD_RDATA1
```

Attempting to disable the STATUS/CRC bytes by writing `INTERFACE = 0x00` causes the chip to reset to defaults after calibration (`SFOCAL1`), silently re-enabling them. The correct approach is to keep `INTERFACE = 0x05` and always read 6 bytes.

### Working Register Configuration (400 SPS, PGA bypass, internal 2.5V ref)

| Register | Value | Description |
|----------|-------|-------------|
| POWER (0x01) | 0x11 | Internal reference enabled |
| INTERFACE (0x02) | 0x05 | STATUS + CRC enabled |
| MODE0 (0x03) | 0x00 | No chop, no delay |
| MODE1 (0x04) | 0x40 | Sinc3 filter, no ADC2 |
| MODE2 (0x05) | 0x88 | PGA bypass, 400 SPS |
| INPMUX (0x06) | 0x01 | AIN0(+) vs AIN1(−) |
| REFMUX (0x0F) | 0x00 | Internal 2.5V reference |

---

## 5. DRDY Pin Conflict

`PJ_11` (DRDY, Pi header pin 11) is also defined as `LORA_IRQ_DUMB` in the H7 Arduino core — the onboard LoRa module holds this pin and it never goes LOW, causing all `waitDRDY()` calls to time out.

**Workaround: poll mode.** At 400 SPS each conversion takes 2.5ms. A 5ms delay between reads guarantees a fresh sample without needing DRDY:

```cpp
adc.startContinuous();
delay(50);  // filter settling

for (int i = 0; i < N; i++) {
    delay(5);                    // 400 SPS = 2.5ms, 5ms = safe margin
    ADC_Reading r = adc.readDirect();  // read directly, no DRDY check
    // process r...
}
```

This achieves ~200 effective SPS rather than 400 SPS, which is sufficient for the 100 Hz measurement target (Nyquist: 2× = 200 SPS minimum).

**Permanent fix (future work):** Wire DRDY to a free GPIO pin not used by any onboard peripheral and update `ADS1263_DRDY_PIN` accordingly.

---

## 6. Lessons Learned from Arduino Uno Testing

Before moving to the Portenta H7, extensive testing was done with an Arduino Uno connected via 15cm Dupont cables. This setup produced ~900 µV RMS noise regardless of any software fix — making Test B impossible to pass. Root causes identified:

| Issue | Symptom | Resolution |
|-------|---------|------------|
| STATUS/CRC bytes not read | Random ±millions count jumps | Read 6 bytes: STATUS+4data+CRC |
| Wrong REFMUX value | ADC stuck at 2.05V | `REFMUX = 0x00` (internal 2.5V) |
| Wrong INPMUX polarity | All readings negated | `INPMUX = 0x01` (AIN0 vs AIN1) |
| SFOCAL1 resets registers | PGA re-enables after calibration | Re-write critical registers post-calibration |
| Dupont cable capacitance | ~900 µV RMS noise floor | Direct HAT-on-carrier connection |
| SPI DRDY race condition | Corrupted readings at 400 SPS | Edge-detect or poll mode |

The Dupont cable issue deserves emphasis: at 15cm, cable capacitance loads the MISO line enough that lower bits are unreliable. The effective resolution dropped to ~10 bits regardless of the 32-bit ADC. **The HAT must be used with a direct Pi-compatible carrier — not Dupont cables.**

---

## 7. Test B Results

Conditions: AIN0 shorted to AIN1, 400 SPS (poll mode), Sinc3 filter, internal 2.5V reference, 2000 samples.

| Metric | Result | Datasheet Typical | Limit |
|--------|--------|-------------------|-------|
| RMS noise | 5.28 µV | ~5 µV | < 50 µV ✓ |
| Peak-to-peak | 38.92 µV | ~33 µV | < 300 µV ✓ |
| Noise-free bits | 17.13 | 17–18 | > 15 bits ✓ |
| Mean error | 0.03 mV | — | < 5 mV ✓ |

Performance matches datasheet spec, confirming the signal chain is working correctly and the HAT hardware is sound.

---

## 8. Quick Reference — Driver Pin Definitions

Update `ADS1263_Driver.h` for Portenta H7 + Hat Carrier:

```cpp
// Portenta H7 + Hat Carrier (ASX00049)
#define ADS1263_CS_PIN    PE_6   // J2-53, Pi pin 15
#define ADS1263_DRDY_PIN  PJ_11  // J2-50, Pi pin 11 (LoRa conflict — use poll)
#define ADS1263_RESET_PIN PI_5   // J1-56, Pi pin 12
```

For Arduino Uno (Dupont — not recommended for production):
```cpp
// Arduino Uno (reference only — noise floor too high for precision use)
#define ADS1263_CS_PIN    10
#define ADS1263_DRDY_PIN  2
#define ADS1263_RESET_PIN 9
```
