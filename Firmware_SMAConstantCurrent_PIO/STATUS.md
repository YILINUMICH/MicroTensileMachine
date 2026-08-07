# Firmware_SMAConstantCurrent_PIO — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP** — **first flashed and run on hardware 2026-07-27.** The closed-loop hold works: `cc 200` converged to 200.8/201.2 mA (0.6% of target) by the third pulse, with `R_est` adapting across cycles as designed. Getting there took three transport/scheduling fixes, two of them self-inflicted — see the **Bring-up log** in [README.md](README.md). Not yet Stable: the absolute current scale is still uncalibrated and steps 5–8 of the ladder below are unrun. |
| **Role** | Development fork of `Firmware_SMASensorHub_PIO` adding a **closed-loop constant-current** controller to the M7 SMA drive path. Everything else is carried over unchanged, so this image is a strict superset of the parent's behaviour. |
| **Supersedes** | Nothing. `Firmware_SMASensorHub_PIO/` remains the production / rollback image and must not be modified for CC work. |
| **Owner** | Yilin |
| **Quick test** | `pio run -e portenta_m7 -t upload`, power-cycle (USB + EVM supply), `pio device monitor` @ 115200. Expect the `[M7] Firmware_SMAConstantCurrent_PIO` banner and `[SMA] CC loop: 1000 Hz, tau=7.0 ms`. Then `arm` → `cc 200 2000` → watch `[SMA] [CC] start` and the `src=6/7` rows appear in the stream. |

## 2026-08-07 later — UDP BRING-UP DONE: Steps 1+2 pass, transport SPLIT, 0.000% loss (VERIFIED on the bench)

Supersedes the "UDP dormant, UNTESTED" entry below. `netcfg` was sent for the
first time and the stream moved. Measured, `portenta_m7_nbtx_udp`:

| check | result |
|---|---|
| `endPacket()` non-blocking (the plan's premise) | **holds** — no loop stall |
| handoff | serial samples **975/s → 0**, UDP picks up **983 lines/s** |
| sample loss, 75 s, per-src `seq` gaps | **0.000%** both srcs (491/s each) |
| firmware-clock (`hw_us`) gaps > 10 ms | **none** |
| M7 loop | `loop_hz` 12.8–14.5 k, `loop_us_avg` ~70 µs |
| host inter-datagram | p50 1 ms, p99 2 ms |

Link: PC dongle `Ethernet 5` now negotiates **100 Mbps** (was 10), APIPA
`169.254.245.100/16` ↔ H7 static `169.254.245.50/16`. H7 answers ping 4/4 sub-ms.

**THE TRANSPORT SPLIT (the real work).** First bring-up showed *all* text —
`[STATUS]`, banner, `[ADC1]` alarms, every command reply — leaving over UDP,
with CDC going fully silent. Cause: `#define Serial NbSerial` (`main.cpp:200`,
the wedge-fix shim) funnels all 326 `Serial.print` sites into the SAME buffer
the sample writers use, and the UDP/CDC switch sat *downstream* of it in
`txEmit()`. The comment claiming "UDP mode still emits [STATUS] on serial"
predated the shim and was wrong. `txEmit()` is now three functions:

- `cdcEmit()` — the one physical CDC emitter (bounded non-blocking sender)
- `txEmit()` — **samples only**: UDP when armed, else `cdcEmit`
- `textWrite()` → `txt_buf[2048]` → `txtFlush()` → `cdcEmit` — **text, always CDC**

M4's RPC passthrough in `pumpSensors()` was the one genuine non-sample caller
in the sample path (it called `streamWrite` directly) and now uses `textWrite`.
`textWrite` only diverges once `udp_on` is set, so **the USB rollback path is
byte-for-byte unchanged**. This matches what the host already expected:
`UdpReader` + `PortentaReader.poll_status_nb()` were written for exactly this.

**A — `udp_on=0|1` in `[STATUS]`.** The board can revert to CDC without telling
anyone (a self-heal MCU reset clears `udp_on`), stranding a host on the UDP
socket — the failure that forced the `46e3f42` revert, reproduced here. Numeric
on purpose: the host regex is `(\w+)=(-?\d+(?:\.\d+)?)` and drops string values.

**B — the self-heal no longer kills a UDP session.** Observed live: with the
stream on UDP the CDC endpoint accepts ~nothing, so the windowed link-health
detector scored dead windows and **MCU-reset the board mid-measurement**
(`t_ms` 124520 → 82909, all since-boot counters restarted). The window verdict
gains a third branch — CDC quiet **but UDP delivered ≥4096 B this window** →
*idle CDC*, not *dead CDC*: no re-enum, no reset. Healing there was actively
harmful (the 1.5 s soft re-enum stalls the loop — the exact distortion UDP
exists to remove — and the escalation clears `udp_on`).
Verified: 75 s with the serial port closed (two full heal windows), stream flat
at 983/s, `t_ms` 27141 → 103141, `usb_heal` 0.

⚠ **Deliberate trade-off in B:** if CDC genuinely wedges *during* a UDP session
the heal is suppressed and commands stay dead until a power cycle. Detectable
host-side — UDP samples arriving while `[STATUS]` stops on serial *is* the wedge
signature — and better than a reset that kills stream and commands both.

⚠ **Threshold margin shrank.** The 4096 B/window test was calibrated against a
~1 MB/window sample stream; CDC now carries ~400 B/s of text ≈ 12 KB/window.
Still ~3× the threshold, but re-check it if `[STATUS]` is ever made less chatty.

**Not yet done** (was items 2–3 of the list below): wiring `UdpReader` into
`lib_h7_session.H7.capture()`, and reworking `H7.open()`'s gate — it aborts on
<2000 serial bytes in 2 s, which no longer arrives now that samples are on UDP.
Step 3 (default `transport: udp`) waits on those.

**Bench note, unrelated to this work:** ADC2 died mid-run on the first attempt
(`prod2=0`, `crc_err` 512/s, `crc_err_total` climbing) — the documented
skipped-EVM-power-cycle signature. A proper EVM cycle restored `prod1=prod2=512`,
`crc_err_total=0`. The 0.000% seq-gap loss is what proved it was a production
fault, not a transport one.

## 2026-08-07 session close — UDP migration adopted; board left on `portenta_m7_nbtx_udp` (UDP dormant, UNTESTED)

Decision at session end: the stream moves to UDP per
`docs/UDP_stream_migration_plan.md` — the wedge self-heal (below) makes
CDC survivable, but UDP removes the high-rate stream from the fragile
path entirely. Work continues on branch **`feat/udp-stream`**.

State as left: `[env:portenta_m7_nbtx_udp]` (full nbtx fix stack +
`H7_TRANSPORT_UDP=1`) is **built and flashed but the link is untested** —
no `netcfg` has ever been sent, so the stream still rides USB-CDC and the
board behaves exactly like nbtx until the UDP bring-up. Link readiness
was verified host-side: the PC dongle (`Ethernet 5`, Realtek USB 2.5GbE)
is Up at 10 Mbps on APIPA `169.254.245.100/16` — the same link-local
segment as the firmware's static `169.254.245.50/16`, so the plan's
zero-host-config path applies. First moves for the UDP branch:
1. Boot-banner `[NET]` check + ping `169.254.245.50`, then
   `netcfg 169.254.245.100 7777` and count datagrams/lines on UDP :7777.
2. Host ingest: `UdpReader` exists in `Calibrate_LaserHead/portenta_reader.py`;
   the thermal module's `lib_h7_session.H7.capture()` path is serial-only.
3. **Rework the host open-gate**: `H7.open()` aborts on <2000 serial bytes
   in 2 s — with the stream on UDP that gate must move to the UDP socket.
4. Self-heal refinement for UDP mode: with the stream off CDC, a
   closed-port idle window looks "dead" to the byte-count trigger; after a
   real session (`usb_had_link` set) the second idle window causes one
   spurious self-reset. Harmless but ugly — gate the dead-window verdict
   on UDP-armed state or count UDP sends as link evidence.

## 2026-08-07 evening — WEDGE SOLVED: it is a HOST-side driver fault, and the firmware now self-heals it (VERIFIED twice on the bench)

**Supersedes the diagnosis in the entry below.** The blocking-write mechanism
described there is real as a *hazard* (a 718 ms loop stall from CDC
back-pressure was observed live) but it is NOT the wedge. The bench walk
tonight, five flash-test iterations with the RGB LED diagnostic:

1. **Loop-hang hypothesis falsified** — wedge reproduced with the liveness
   LED still blinking: the M7 loop was alive while the port was silent.
2. **One wedge WAS an M7 crash** — red LED in mbed-os's death-blink, green
   dead. A rarer second mode; the IWDG (4 s) covers it.
3. **The decisive experiment**: with the port wedged, a manual Windows
   driver disable/enable revived it — board untouched. Recovered [STATUS]:
   `t_ms=252871` (H7 healthy through the whole wedge), `tx_drop=8 MB`
   (streaming into the void the whole time). **The wedge is the Windows
   `usbser.sys` device instance wedging.** Opens hand back a handle from
   the dead instance; reopens/DTR toggles touch the same instance, which is
   why only a power cycle (= re-attach = instance rebuilt) ever worked.
4. **Two self-heal triggers failed before one stuck**: the connected()/DTR
   flag wedges stale in either direction; a time-since-last-accepted-byte
   stamp is defeated by reopen-polling (each open of the dead instance
   swallows exactly one 64 B endpoint buffer — that fake progress kept
   resetting the window; it also explains why `usb_heal=6` fired only
   after polling stopped). The trigger that works judges 30 s WINDOWS:
   accepted TX+RX < 4 KB/window = dead (healthy streams move ~1 MB).
5. **The heal that works**: `PluggableUSBD().disconnect()` → **1.5 s**
   detach → `connect()`. A 100 ms detach was never registered by Windows
   (six invisible heals); at 1.5 s the bench observed COM8 leave the port
   list and the driver instance rebuild. Escalation: a second consecutive
   dead window → `disarm()` + `NVIC_SystemReset()` (gated on a
   healthy-window-since-boot flag so an idle board never reboot-loops).

**Verified end-to-end, twice:** wedge → dead window detected → detach
registered ("COM8 GONE") → **stream flowing again at +41 s, `usb_heal=1`,
no reset, no human.** Worst case ≈ 65 s via the escalation path.

The `[env:portenta_m7_nbtx]` fix stack, all flag-gated (default env still
byte-identical, sha256 `21e2811f…`):

| flag | role |
|---|---|
| `TX_NONBLOCK=1` | bounded send_nb TX — removes the 718 ms loop stalls and the coil-stuck-energized hazard; provides the per-window byte accounting the heal trigger runs on |
| `H7_LOOP_WDT_S=4` | IWDG — auto-reset for the crash mode (and any future loop hang); boot banner reports reset cause |
| `H7_USB_SELFHEAL_S=30` | windowed link-health check + 1.5 s re-enum heal + reset escalation; `usb_heal=N` in [STATUS] counts events |
| `DBG_LOOP_LED=1` | green = loop alive, blue = cached DTR flag, red = RX activity (and mbed's death-blink shows through on a crash) |

**Still open, and now the campaign blocker:** the ADS1263 EVM. After the
heal verification the stream shows `rate2=0` (ADC2 dead), a CRC storm at
~630/s, and ADC1 sagging to ~390/512 — and tonight power cycles no longer
clear it. That is the physical seating fault from the night write-up
(restart checklist item 1: reseat EVM ribbon + supply), not the wedge.

**Acceptance still owed before nbtx becomes the campaign image:** after the
EVM reseat, one full sweep on nbtx with `tx_drop≈0`, `usb_heal=0`,
`rate1/2=512` throughout, and a clean sweep report.

## 2026-08-07 — USB-CDC wedge: mechanism confirmed at source level, fix built (branch `fix/usb-cdc-wedge`, NOT yet bench-verified — **diagnosis superseded by the entry above**)

The silent-port wedge (see `Experiment_SMAThermalCharacterization/STATUS.md`
2026-08-07 entries: port opens, 0 bytes, no reply even to `info`, only a power
cycle recovers; trigger = rapid close→reopen) is now traced through the
installed core (`framework-arduino-mbed`, ArduinoCore-mbed 4.x):

- `Serial.write` → `UART::write` → `_SerialUSB.write()`
  (`cores/arduino/Serial.cpp:229`) → `USBSerial::write`
  (`PluggableUSBSerial.h:256`), which checks `connected()` once and then loops
  **blocking `USBCDC::send()` per 64-byte packet** — the CDC endpoint buffer
  is only `CDC_MAX_PACKET_SIZE = 64` B (`USBCDC.h:224`).
- `send()` "blocks until the full contents have been sent", released only by a
  completion ISR or a **connection state change**. If the DTR-drop of a port
  close is missed/raced, `_terminal_connected` stays stale-TRUE, the host
  never drains the endpoint, and the write blocks FOREVER. The M7 loop hangs
  inside that one call: no samples, no [STATUS], no command replies, no
  watchdogs — and the coil stays at its last DAC code. `hostUp()` cannot
  close this race: it is a 250 ms-cached read of the very flag that is stale.
- Every exonerated suspect, for the record: the M4→M7 ring cannot hang
  (push/pop are non-blocking by construction, overflow only increments
  `dropped`), and `wdt`/`hb` only ever `disarm()` — they cannot mute TX.

**Fix + diagnostics, all flag-gated — the default `portenta_m7` build is
byte-identical to pre-change (sha256 `21e2811f…` verified equal before/after):**

| artifact | what it is |
|---|---|
| `-D TX_NONBLOCK=1` (in `[env:portenta_m7_nbtx]`) | (1) `txEmit()` sends via `USBCDC::send_nb()` in a **time-bounded** retry loop (~2 µs/byte budget; healthy throughput unchanged, dead endpoint costs one bounded spin, then drops the chunk into `tx_drop` with a 250 ms back-off after 3 consecutive failures). (2) `NbSerialShim` reroutes **every** `Serial.print` in the M7 TU ([STATUS]'s ~40 prints, all command replies) through the accumulate buffer → bounded sender, so no blocking entry point remains. Print's own formatters are used → wire format byte-identical (checked against the host-parser contract: 6-field TSV, `[STATUS] ` prefix, `m7_us=` `m4_us=` adjacency all untouched). |
| `-D DBG_LOOP_LED=1` (in both new envs) | ~4 Hz green-LED blink driven from `loop()`. The wedge discriminator: silent port + **frozen LED** = loop hung in a blocking write (mechanism confirmed); silent port + **blinking LED** = CDC endpoint died with the loop alive (different bug — nbtx keeps the rig safe but won't recover the port). |
| `[env:portenta_m7_wedgeled]` | stock blocking TX + the LED — the **baseline** build that keeps the wedge reproducible for the A/B. |
| `tools/torture_open_close.py` | reproduction/verification harness: N rapid open/close cycles, classifies ALIVE / SUSPECT (probes with read-only `info`) / WEDGED, logs every cycle, stops at first wedge with power-cycle instructions. Sends nothing but `info`. Exit 0 = clean soak, 2 = wedged, 3 = open failed. |

**Bench sequence (attempt 4 pre-flight — in order):**
1. Flash `portenta_m7_wedgeled` (+ `portenta_m4` if changed), power-cycle.
2. `python tools/torture_open_close.py --gap 2 --cycles 20` → expect a wedge
   within a few cycles; note the LED state. Power-cycle.
3. Flash `portenta_m7_nbtx`, power-cycle. Same torture run → expect exit 0.
   Then the soak: `--gap 5 --cycles 50` → expect exit 0.
4. One full sweep on the nbtx image: `tx_drop=0` in [STATUS] throughout, and
   `rate1/2` / `prod1/2` at stock values, sweep report clean.
5. Record all four results here. Only then does nbtx become the campaign image.

**ROLLBACK at any point:** flash `[env:portenta_m7]` — proven byte-identical
to the image that ran every capture to date. Or `git revert` the commits on
`fix/usb-cdc-wedge`; the branch touches only this project + docs.

**Known behavioural change (intended, nbtx only):** severe host-side reader
starvation (e.g. the camera-starved-reader case in the 2026-07 bring-up log)
used to STALL the M7 loop and distort actuation timing; under nbtx it DROPS
stream chunks instead (visible as `tx_drop` + host-side `seq` gaps). Timing
integrity now wins over sample completeness — the correct trade for a
control loop.

## What this adds

- **`cc` / `ccfire` / `cccycle`** — current-mode twins of `drive` / `fire` / `cycle`.
  Same actuation engine, same arm/disarm, same heat watchdog, same `ping`/`stop`/`abort`.
- **`tau <ms>` / `ccgain <Kp>`** — the tuning knobs; `cc` (bare) prints controller state.
- **`src=6` (command `u`) and `src=7` (`R_est`)** on the sample stream — the adaptive
  state, logged so a controller problem can be told from a load problem offline.
- **Open-load fault** — command railed with no current ⇒ broken wire ⇒ auto-disarm.
  Voltage mode cannot detect this; a current loop actively ramps into it.

## Sense-path correction (2026-07-24) — changes recorded R

Schematic re-check found the A0 tap identified wrongly in the inherited code:

- **A0 is on `SMA_P`** (the SMA's high side, **after** the shunt), not the LDO
  output. So A0 measures `V_sma` directly and `R_sma = V_sma / I` — no shunt
  correction. `V_ldo` is now the *derived* quantity (`V_sma + I·R_shunt`) and is
  display-only. The old code subtracted the drop from an already-post-shunt
  reading, i.e. it double-counted it.
- **`R_SHUNT_OHM` 0.1 → 0.2** (200 mΩ, the part actually fitted). With
  `INA_GAIN = 10 V/V` the current scale is now **2.0 V/A**, not 1.0.
- **`src=3` now carries `V_sma`**, not `V_ldo`, so host-side `V/I` agrees with the
  firmware's own `src=5`.
- `readLDO()` → **`readSmaP()`**. The DAC-characterisation commands
  (`set`/`code`/`step`/`sweep`) still read this pin, so their `V_meas` is SMA_P
  and sits `I·R_shunt` under the `codeToVldo()` prediction while current flows —
  sweep **disarmed** for a clean DAC→LDO fit. Labels renamed to `V_smap*` to say so.

The CC loop itself is untouched: `R_est` is command-domain (`u/I`, skeleton
pitfall 2), so it never depended on the A0 node. Only the *measured* V/R change.
Any `src=3` / `src=5` data captured with an earlier build is wrong on both counts
(2× current-scale error and a double-subtracted shunt drop) — re-take it.

## Pre-run R seed (2026-07-31) — cycle 1 is now a real pulse

`startCycleCC()` now enters **`SMA_CC_SEED`** before the first heat: it averages
the idle hold the coil is already parked at for `CC_SEED_MS` (100 ms) and seeds
`cc_R_est` from `u/I`, so cycle 1 runs on feedforward instead of discovering R
the hard way. Prints `[CC] seed R=4.694 ohm  I=106.5 mA  u=0.500 V  n=98`.

**The problem.** With no valid `R_est` the loop runs the BOOTSTRAP branch — pure
integral, no feedforward — whose closed-loop rise is `tau = R / CC_KI_BOOT =
4.7/8 = 590 ms`. A 300–400 ms pulse only climbs ~45% of the way, so the first
fire of every condition was a ramp, not a measurement. Measured 2026-07-31 before
the change: **354/750, 389/950, 393/850, 431/950 mA** on cycle 1 against ~100% on
every later cycle. `operator_current_sweep.py` fires `cycles+1` purely to throw
this pulse away.

It could not latch during that first heat either: `ccEngage()` starts the
integral AT the applied idle command, so tick 1 lifts `u` off `u_min`, `railed`
goes false, `near` is far away, and **no valid latch point exists until the first
COOL phase** — by which time cycle 1 is already spent.

**Why 100 ms and not one sample.** The sense carries **sd ~12.6 mA** per read,
which at ~107 mA idle is **~12% on a single reading**, and `R = u/I` passes it
straight through — a one-sample latch lands anywhere in **3.6–5.8 Ω**. That is
the real mechanism behind the long-standing "R_est bootstraps from a single ADC
sample" TODO. ~100 ticks of averaging cuts it to **~1.2%**, well inside the ±12%
`near` gate. Costs ~5 mJ against ~1.2 J for a fire, and does not heat the wire.

Implemented as a **state, not a blocking wait** — a 100 ms busy-wait in the
command handler would stall the M7 super-loop, stop servicing USB, and overflow
the M4 sensor ring. `SMA_CC_SEED` is appended LAST in the enum because
`[STATUS] sma_state=` prints it as an int.

**Verified on the rig (2026-07-31), two independent runs.** Cycle 1 came back at
**99.6–99.8%** of command at 750×400, 950×300 and 850×400, and across the full
35-condition grid at **99.2–101.2%**. Seed values 4.55–4.90 Ω, n = 98–99.

**Consequence worth knowing:** cycle 1 is now a *valid measurement of a different
initial condition*, not an interchangeable extra sample. It fires into a fully
relaxed wire and takes a one-time set (`x_base` moves ~+370 µm at 850×400, then
holds to ~100 µm), which `operator_sweep_report.py` flags as `base-jump`. Keep
it flagged; do not pool it with cycles 2+.

**It also makes `--i-low 0` viable, which is the fix for the cool-phase latch**
(see `Experiment_SMAThermalCharacterization/STATUS.md`). The documented i_low=0
failure — "the bootstrap integral resets every pulse" — is gated on
`if (!cc_R_valid)`; the seed makes it true before cycle 1, so that path is now
structurally unreachable. The seed and `i_low 0` only work together.

## Bring-up ladder — stop at the first failure

1. **Boot** — banner + `[SMA] MCP4728 OK`. `info` shows the CC block with
   `R_est = -- (not bootstrapped)`.
2. **Open-load fault fires** — `arm`, then `cc 200` with **the SMA disconnected**.
   Expect the command to ramp to the ceiling and `[CC] FAULT: open load` +
   `DISARMED` within ~250 ms of railing. Verify this *before* connecting a wire:
   it is the safety net for every test after it.
3. **Bootstrap + hold** — connect the load, `arm`, `cc 200 2000`. Expect `R_est` to
   latch within the first ~100 ms and settle near the wire's DC resistance, and
   the measured current to sit on target.
4. **`cc_hz` in `[STATUS]`** ≈ 1000. Below that the loop is starved by the rest of
   the pass and `tau` no longer means what it says — check `loop_hz` first.
5. **Accuracy** — cross-check `read` against a DMM in series. Expect the measured
   current to read **~5–7 % HIGH** (the known `ADC_VREF_V` / ADC-duty error). For a
   *current* controller that is a systematic setpoint error, not just noise: fix
   the scale with `aref` / `gain` / `shunt` / `ioffset`, then re-run this step.
6. **Step response** — `cc 200 10000`, then `cc 800`, then `cc 200` in one capture
   (retarget is live). Check overshoot and settling from the `src=4` stream.
7. **Disturbance test** — actuate a real SMA (`cccycle`) and confirm the current
   holds while `src=7` `R_est` moves with the transformation. Per the skeleton,
   **tune `tau` here**, not on the step-from-zero: the step looks good for almost
   any `tau`.
8. **No regression** — `dropped` / `crc_err` stay 0 and `hwm` < 50 % with the CC
   loop running. The current loop must not be bought with laser/load samples.

## Module TODOs

- [x] ~~Steps 1–3 of the bring-up ladder~~ — done 2026-07-27. Boot, MCP4728,
      bootstrap and hold all pass; `cc 200` holds within 0.6%.
- [x] ~~Step 2, the open-load fault~~ — done 2026-07-27, and it **FAILED first
      time**: the guard could not fire because the in-cycle current sense has
      sd 12.6 mA against a 20 mA floor, so 16.8 % of samples read above the
      floor with the DUT disconnected and the reset-on-excursion accumulator
      never reached 250 ms. Fixed with a leaky accumulator; re-tested and it now
      disarms ~380 ms after railing. **This is why the ladder says verify it
      before connecting a wire — assuming it worked would have left current mode
      with no open-load protection at all.**
- [x] ~~Step 5, the accuracy check~~ — done 2026-07-27 with a Fluke 17B+ and a
      4.8 Ω dummy load. **The predicted 5–7 % current error is NOT there**: the
      scale measures correct to ~1 %. The code→LDO map is DMM-verified to 1 mV
      (`vdd 5.067`); the *design* figure of 4.7 V turned out to be the wrong
      number, not the hardware. See the Calibration log in
      [README.md](README.md), including the series-ammeter attempt that failed
      and why.
- [ ] **PERSIST the measured constants.** `vdd 5.067` and `ioffset 0.0167` are
      runtime only and were *demonstrably* lost to a reflash mid-session. Bake
      both into the source defaults. Re-send them after every upload until then —
      nothing on screen tells you they are missing.
- [ ] **Resolve the +1.3 % A0 bias** — `aref` (cancels in `R`, squares in power)
      vs the 10k/10k divider (biases `R` by 1.3 %). A nominal-value resistor
      cannot settle it; needs a tolerance-known reference or a working series
      ammeter. Low priority: ~1.3 % on `R`, ~2.6 % on power.
- [ ] **Decide on UDP.** `Serial.write()` now measures ~300 µs in-cycle (11 µs
      idle) — genuine USB-CDC back-pressure at ~160 KB/s, and the current cap on
      the SMA rate (~650 Hz vs the 1000 Hz nominal). This is the FIRST time the
      transport has actually been the bottleneck; earlier suspicions of it were
      wrong (see README bring-up log). Cheaper things to try first: merge the
      `pumpSensors` and `streamSma` writes into one per pass (halves the write
      count), and confirm 650 Hz is actually insufficient for the science. The
      `portenta_m7_udp` env exists and has never been flashed.
- [x] ~~Chase the ADC2 checksum failures~~ — **SOLVED 2026-07-27, and it was not
      a fault.** It was the documented EVM power-cycle requirement, skipped
      across ~15 uploads in one session. See the ADC2 section in
      [README.md](README.md). **Rule: `crc_err` climbing or `rate2=0` after a
      flash means power-cycle the EVM, not debug the driver.**
- [ ] Steps 4–8 of the bring-up ladder (`cc_hz`, accuracy vs DMM, step response,
      disturbance test, no-regression).
- [ ] Decide whether to keep the `DBG_LOOP_PROFILE` / `portenta_m7_prof`
      instrumentation. It found two bugs in one session; it is off by default and
      the normal build is behaviourally identical.
- [ ] **Confirm the 2026-07-24 sense-path correction on the bench** — with a known
      resistor as the load, check `read`'s `V_sma`/`I`/`R` against a DMM. This is
      the change most likely to be still wrong, since it came from the schematic
      rather than a measurement.
- [ ] **Fix the absolute current scale** (step 5). Until then CC targets are
      repeatable but not absolute — the loop faithfully holds a number that is
      itself ~5–7 % off.
- [ ] **Re-tune `tau`** once the TPS7A57's real settling time is known
      (`docs/PLAN_phase6_ldo_characterization.md`). The default 7 ms is the Uno's,
      and the plant — not the MCU — is what limits it.
- [ ] Teach the recorder/analysis about `cc_u` / `cc_r`
      (`Experiment_SMACharacterizationV3/config.yaml` `h7.channels`). The shared
      parser already names them.
- [ ] Decide whether CC graduates into `Firmware_SMASensorHub_PIO/` or stays a
      separate image, once it is bench-proven.
- [ ] Inherited from the parent, still open here: bench-verify the ~1 kHz sensor
      stream, and `RPC.begin()` behaviour with `-D M4_IDLE`.

See [README.md](README.md) for the design and the port notes, and
[../README.md](../README.md) for the cross-cutting project map.
