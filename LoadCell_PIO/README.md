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
│  - timed-polls ADC   │                 │  - boots M4 via    │
│    (DRDY unusable —  │                 │      RPC.begin()   │
│    see §PJ_11/LoRa), │                 │                    │
│    sends samples via │                 │                    │
│    RPC to M7         │                 │                    │
└──────────────────────┘                 └────────────────────┘
```

## Status

**Step 1 data path — proven end-to-end.** After the three fixes in the
Troubleshooting section below (M4-aware driver logging, 3 s ADS1263
power-up settle, timed polling instead of DRDY), a clean boot produces:

```
[M7] bridge up — forwarding RPC to USB Serial
[M4 cp 0] RPC up
[M4 cp 1] Serial.begin done
[M4] waiting 3000 ms for ADS1263 to power up...
[M4] ADS1263 power-up settle done
[M4 cp 2..6] pinModes / SPI.begin
[M4 cp 7] calling adc.begin()
ADS1263 found. ID=0x23
ADS1263 ready (ext 5V ref, PGA bypassed)
[M4 cp 8] adc.begin returned TRUE
[M4] ADC ready, ID=0x23
[M4] VREF=5.000 V
[M4 cp 9] startContinuous done
[M4] streaming. format: t_ms\traw_code\tvoltage_V
4012   211853309   0.493259
4067   166040793   0.386594
...
4617   -459082     -0.001069
```

One sample every ~55 ms at the default 20 SPS. The leading ramp is the
Sinc3 filter settling; the steady-state ~ −1 mV with tens-of-µV jitter
is the expected input-stage offset / noise floor with AIN0/AIN1 floating.

## Flash order

First time:

```sh
pio run -e portenta_m7_bridge -t upload    # once — installs the M7 bridge
pio run -e portenta_m4        -t upload    # flashes the M4 sampler
pio device monitor                          # 115200 baud
```

Thereafter only re-flash `portenta_m4` while iterating on the sampler.

> **Power-cycle the stack after every flash.** The dfu upload resets the
> H7 MCU but does not cleanly re-power the HAT's 3.3V LDO rail, and the
> ADS1263 reliably comes up in a latched bad state — you will see
> `ID=0x00` and `adc.begin returned FALSE` even with the 3 s settle
> delay in `setup()`. Unplug the Hat Carrier (USB-C and/or J9 screw
> terminal), wait ~5 s, reapply power, then reopen the serial monitor.
> Don't start debugging register/SPI issues until you've done this.

---

## Troubleshooting

Three failure modes have been observed during bring-up. In boot order:
(1) M4 hangs before any checkpoint prints, (2) boot gets to `cp 7` but
`adc.begin` returns FALSE, (3) boot is clean all the way through but no
samples appear. Each one has a distinct root cause and fix.

---

### 1. M4 hangs after `[M4] booting...` (no checkpoints ever print)

#### Symptom

After successfully flashing both firmwares and opening the serial monitor,
the user sees:

```
[M7] bridge up — forwarding RPC to USB Serial
[M4] booting...
```

…and then nothing. The M4 is clearly alive (`[M4] booting...` proves RPC
is working both directions), but execution never reaches the "ADC ready"
or sample-stream lines.

#### Root cause

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

#### Fix

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

#### What each checkpoint rules out

If the log stops at a given checkpoint, the hang is in the **next** step:

| Last visible checkpoint | Likely cause |
|---|---|
| cp 0 (RPC up)         | Stray Serial.print from early code; UART not yet initialised |
| cp 1 (Serial.begin)   | `pinMode(PE_6)` — GPIOE clock not enabled on M4 |
| cp 2                  | `pinMode(PI_5)` — GPIOI clock not enabled on M4 |
| cp 3                  | `pinMode(PJ_11, INPUT_PULLUP)` — GPIOJ clock not enabled on M4 |
| cp 4 / cp 5           | `digitalWrite` on CS/RESET — unusual, check pin defines |
| cp 6 (SPI.begin)      | `adc.begin()` internals — most likely an `SPI.transfer()` that never completes (chip not answering on MISO, or SPI peripheral not actually clocking) |
| cp 7                  | Hang **inside** `adc.begin()` after SPI is up — same causes as cp 6 |
| cp 8 FALSE            | SPI works but chip ID ≠ 0x2X — see §2 below (power-cycle first!) |

If the log stops at **cp 6** (SPI.begin doesn't return), the default
`SPI` object on Arduino Mbed Portenta may be mapped to a peripheral M4
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

### 2. `ADS1263 not found. ID=0x0` / `adc.begin returned FALSE` at cp 8

#### Symptom

Boot progresses cleanly through `cp 0 … cp 7`, then:

```
[M4 cp 7] calling adc.begin()
ADS1263 not found. ID=0x0
[M4 cp 8] adc.begin returned FALSE
[M4] FATAL: ADS1263 init failed
```

The SPI bus is working (the driver successfully reads register 0x00 —
it just gets `0x00` back), but the ADS1263 is not yet responding.

#### Root cause — two separate things, check both

**(a) Stale HAT power state after flashing.** The dfu upload at end of
`pio run -t upload` resets the H7 MCU but does *not* cleanly re-power
the Waveshare HAT's on-board 3.3V LDO rail. The ADS1263 latches into a
state where it won't answer SPI until its power is fully removed and
reapplied. **Always power-cycle the Hat Carrier after a flash** (unplug
USB-C / J9 screw-terminal, wait ~5 s, reapply). This is the single
most common cause and should be the first thing tried.

**(b) Insufficient power-up settle time in firmware.** Even on a fresh
cold boot, the HAT's AMS1117-3.3 LDO and the ADS1263's internal
oscillator/reference need a substantial margin before the chip will
reliably answer SPI. The Stable M7 sketch (`ADS1263/Stable/Stable.ino`)
works around this with `delay(3000)` right after `Serial.begin`; the
same pattern is now in M4 `setup()` between cp 1 and cp 2:

```cpp
RPC.println("[M4] waiting 3000 ms for ADS1263 to power up...");
delay(3000);
RPC.println("[M4] ADS1263 power-up settle done");
```

Without this delay the symptom is identical to (a) — `ID=0x00`,
`begin returned FALSE` — but no amount of power-cycling fixes it,
because the firmware simply polls the chip too early on every boot.

#### How to triage in practice

1. Power-cycle the carrier. Re-open monitor. If boot goes green → (a).
2. If it still fails with ID=0x00 after a clean power cycle, the
   3000 ms settle is the next suspect — confirm it's present in the
   compiled binary (look for the `waiting 3000 ms...` log line).
3. Only after both are ruled out should you start suspecting wiring,
   REFMUX, or the external 5V rail.

---

### 3. Boot is clean all the way through but no samples stream

#### Symptom

```
[M4 cp 9] startContinuous done
[M4] streaming. format: t_ms\traw_code\tvoltage_V
```

…and then nothing. The ADC initialised, `startContinuous` ran, but the
streaming loop produces no lines.

#### Root cause — DRDY pin collides with onboard LoRa

DRDY is wired to `PJ_11` via the Hat Carrier's Pi-compatible header
(pin 11). `PJ_11` is *also* the Portenta H7's onboard LoRa module IRQ
line (`LORA_IRQ_DUMB` in the Arduino core). The LoRa chip is physically
tied to that pad on both cores, and it holds the line HIGH — the
ADS1263's DRDY falling edge never reaches the STM32 GPIO input. Any
`digitalRead(PJ_11) == LOW` check (which is what `adc.dataReady()` is)
returns FALSE forever, so the `if (adc.dataReady())` gate in the M4
`loop()` never fires.

This matches the behaviour documented in
`ADS1263/ADS1263_H7_Integration_Notes.md` §5.

#### Fix — timed polling instead of DRDY

The M4 `loop()` no longer gates on `dataReady()`. It sleeps a bit longer
than one conversion period and reads unconditionally, same pattern as
the Stable M7 sketch's AC capture loop:

```cpp
static const uint32_t SAMPLE_POLL_MS = 55;   // 20 SPS → 50 ms period

void loop() {
    delay(SAMPLE_POLL_MS);
    ADC_Reading r = adc.readDirect();
    if (r.valid) {
        RPC.print(millis());
        RPC.print('\t');
        RPC.print(r.raw_code);
        RPC.print('\t');
        RPC.println(r.voltage_V, 6);
    }
}
```

If you change the rate passed to `adc.begin()`, update `SAMPLE_POLL_MS`
accordingly (~ 2× the conversion period is a safe margin). The
permanent fix is hardware: reroute DRDY off `PJ_11` to a GPIO no
onboard peripheral claims, then switch back to edge-driven reads —
see Next steps.

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

## Next steps

1. **Sanity-check the scaling.** Short AIN0↔AIN1 and confirm the output
   collapses to ~0 V with RMS close to the Test B figure (~5 µV). Then
   apply a known DC (0–5 V) via the LCA and confirm linearity — this
   also verifies the external-reference path (`REFMUX = 0x24`, AVDD as
   5 V ref), which is new in this build versus Stable's internal 2.5 V.
2. Replace RPC transport with a shared-SRAM ring buffer (SRAM4 with
   non-cacheable attribute, 32-bit head/tail + `__DMB()` barriers).
3. **Reroute DRDY off `PJ_11`** (solder-wire from the HAT's DRDY pad to
   a free HD-connector pin with no onboard peripheral), update
   `ADS1263_DRDY_PIN` in the driver header, then move the M4 reader to
   a DRDY-triggered interrupt. Step 3 is blocked on the hardware rework
   — until it's done, timed polling is the only way samples reach the
   loop.
4. Re-introduce user commands (tare, calibrate, switch ref) on M7,
   forwarded to M4 via a tiny command word in shared SRAM.
5. Layer Ethernet TX on M7 for continuous streaming.
