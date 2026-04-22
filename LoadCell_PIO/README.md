# LoadCell_PIO — Portenta H7 dual-core build

PlatformIO project that runs the ADS1263 load-cell reader on the **M4 core**
of the Portenta H7, with a tiny **M7 bridge** that forwards M4 output to
USB Serial via RPC.

Both cores build from the same `src/main.cpp` — a `#if defined(CORE_CMx)`
guard picks the right branch per environment.

```
┌──────────────────────┐       RPC       ┌────────────────────┐  USB   PC
│ M4  (CORE_CM4)       │◀───────────────▶│ M7 (CORE_CM7)      │◀────▶ Serial
│  - drives ADS1263    │                 │  - forwards RPC to │       monitor
│    on Hat Carrier J5 │                 │    USB Serial      │       115200
│  - polls DRDY,       │                 │  - boots M4 via    │
│    sends samples via │                 │      RPC.begin()   │
│    RPC to M7         │                 │                    │
└──────────────────────┘                 └────────────────────┘
```

## Flash order

First time:

```sh
pio run -e portenta_m7_bridge -t upload    # once — installs the M7 bridge
pio run -e portenta_m4        -t upload    # flashes the M4 sampler
pio device monitor                          # 115200 baud
```

Thereafter only re-flash `portenta_m4` while iterating on the sampler.

---

## Problem: M4 hangs after `[M4] booting...`

### Symptom

After successfully flashing both firmwares and opening the serial monitor,
the user sees:

```
[M7] bridge up — forwarding RPC to USB Serial
[M4] booting...
```

…and then nothing. The M4 is clearly alive (`[M4] booting...` proves RPC
is working both directions), but execution never reaches the "ADC ready"
or sample-stream lines.

### Root cause

The ADS1263 driver's internal status messages were written with plain
`Serial.print(...)`:

```cpp
// ADS1263_Driver.cpp (old)
Serial.print(F("ADS1263 found. ID=0x"));
Serial.println(id, HEX);
// ... etc
```

This works fine on the M7 — `Serial` on M7 is the USB CDC device and is
already enumerated by the time the driver runs. It also works on Arduino
Uno where `Serial` is the USB-UART bridge.

**On the M4 core, `Serial` is not USB.** It maps to a hardware UART
peripheral whose clock and TX line are not configured unless user code
explicitly calls `Serial.begin()`. Writing to that peripheral without
first initialising it puts the `write()` call into a spin loop waiting
for a TX-empty flag that never asserts — an effectively infinite hang.

The driver's first successful-path log line (`"ADS1263 found..."`) runs
right after the chip answers a register read, long before the sample
stream starts. That's exactly where execution stopped.

### Fix

**1. Driver log stream is now conditional.** In
`lib/ADS1263/ADS1263_Driver.cpp`:

```cpp
#if defined(CORE_CM4)
  #include "RPC.h"
  #define DRV_LOG RPC
#else
  #define DRV_LOG Serial
#endif
```

All internal `Serial.print` / `Serial.println` calls in the driver now
use `DRV_LOG` instead. On M4 they go over RPC to the M7 bridge; on M7
and on AVR builds they still go directly to `Serial` with no behavioural
change. No user-facing API changed.

**2. `Serial.begin(115200)` added to M4 `setup()`** as belt-and-braces,
so any stray `Serial.print` from other code (the Arduino framework, a
future library) also has a clocked peripheral to write to. It doesn't
reach the PC — that's still the RPC/USB path — but it prevents a hang.

**3. Diagnostic checkpoints** added in `src/main.cpp` around the
initialisation sequence. Every suspect step is wrapped with:

```cpp
CP(n, "what just finished");   // expands to RPC.println("[M4 cp n] ...")
```

If the boot log stops partway through, the last visible checkpoint
tells you exactly which call hung.

### Expected output after the fix

```
[M7] bridge up — forwarding RPC to USB Serial
[M4 cp 0] RPC up
[M4 cp 1] Serial.begin done
[M4 cp 2] pinMode CS (PE_6) done
[M4 cp 3] pinMode RESET (PI_5) done
[M4 cp 4] pinMode DRDY (PJ_11) done
[M4 cp 5] CS and RESET driven HIGH
[M4 cp 6] SPI.begin() returned
[M4 cp 7] calling adc.begin()
[M4 cp 8] adc.begin returned TRUE
[M4] ADC ready, ID=0x20
[M4] VREF=5.000 V
[M4 cp 9] startContinuous done
[M4] streaming. format: t_ms\traw_code\tvoltage_V
2147   1523    0.000002
2198   1611    0.000002
...
```

Lines arrive roughly every 50 ms at the default 20 SPS.

### What each checkpoint rules out

If the log stops at a given checkpoint, the hang is in the **next** step:

| Last visible checkpoint | Likely cause |
|---|---|
| cp 0 (RPC up)         | Stray Serial.print from early code; UART not yet initialised |
| cp 1 (Serial.begin)   | `pinMode(PE_6)` — GPIOE clock not enabled on M4 |
| cp 2                  | `pinMode(PI_5)` — GPIOI clock not enabled on M4 |
| cp 3                  | `pinMode(PJ_11, INPUT_PULLUP)` — GPIOJ clock not enabled on M4 |
| cp 4 / cp 5           | `digitalWrite` on CS/RESET — unusual, check pin defines |
| cp 6 (SPI.begin)      | `adc.begin()` internals — see below |
| cp 7                  | Hang **inside** `adc.begin()` after SPI is up. Most likely an `SPI.transfer()` that never completes — chip not answering on MISO, or SPI peripheral not actually clocking |
| cp 8 FALSE            | SPI works but chip ID ≠ 0x2X. Wiring or power issue, unrelated to dual-core |

If the log stops at **cp 6** (SPI.begin doesn't return), the default
`SPI` object on Arduino Mbed Portenta is mapped to a peripheral M4
can't reach. Workarounds in order of effort:

- Keep SPI on M7 and push commands/samples across RPC (defeats the
  dual-core timing-isolation goal).
- Use a different SPI instance on M4 (e.g. `SPI1` if the default is
  `SPI`) — requires confirming which physical pads are wired to the
  HAT and swapping pin defines.
- Drop to HAL directly on M4 and skip the Arduino `SPI` wrapper.

At the time of writing, this hang has not been observed with the
driver-logging fix applied — the `Serial.print` hang was the actual
root cause.

---

## Why dual-core for this project

The M7 has plenty of cycles for ADC reading + Ethernet + SMA control.
The reason to push the sampler onto M4 isn't raw throughput — it's
**timing isolation**.

When the M7 is busy assembling an Ethernet packet or processing a
serial command, an ADC read on the M7 could be delayed by hundreds of
microseconds, and the sample timestamps would show that jitter. With
the M4 doing nothing but "wake on DRDY, read SPI, write to shared
buffer", its sample cadence is bounded only by interrupt latency
(~50 ns) and is immune to whatever M7 happens to be doing.

This is why the next architectural step (not yet implemented) is to
replace the "push every sample over RPC" transport with a **shared-SRAM
ring buffer in SRAM4**, which M4 writes and M7 drains at its leisure.
RPC's microsecond-scale per-call overhead is fine at 20 SPS but would
become the bottleneck at ≥ 1 kSPS.

---

## File layout

```
LoadCell_PIO/
├── README.md                       (this file)
├── platformio.ini                  two envs: portenta_m4, portenta_m7_bridge
├── .gitignore
├── src/
│   └── main.cpp                    both cores, #ifdef-guarded
└── lib/
    └── ADS1263/
        ├── ADS1263_Driver.h
        └── ADS1263_Driver.cpp      log stream is CORE_CM4-aware
```

## Current scope (step 1)

- M4 autonomously drives the ADS1263, pushes each sample over RPC.
- M7 forwards RPC to USB Serial one-way.
- No user commands, no calibration, no tare in this step — just prove
  the data path.

## Next steps

1. Replace RPC transport with a shared-SRAM ring buffer (SRAM4 with
   non-cacheable attribute, 32-bit head/tail + `__DMB()` barriers).
2. Move M4 reader onto a DRDY-triggered interrupt instead of a busy
   poll loop, so the M4 core is free between samples.
3. Re-introduce user commands (tare, calibrate, switch ref) on M7,
   forwarded to M4 via a tiny command word in shared SRAM.
4. Layer Ethernet TX on M7 for continuous streaming.
