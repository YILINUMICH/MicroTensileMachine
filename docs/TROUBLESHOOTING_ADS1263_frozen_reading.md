# TROUBLESHOOTING — ADS1263 "frozen reading"

**Scope:** ADS1263 (on the bare TI EVM, driven by the Portenta H7 over SPI) returns a
value that does **not track the live input** — it latches at the boot-time reading and
stays there, even though the chip is clearly alive.

**Status:** ✅ RESOLVED (2026-06-29). Root cause = **SRAM4 address collision**: the RPC/OpenAMP
machinery writes memory ABOVE the `0x38007FFF` the code assumed, overlapping the ring at
`0x38008000`. Relocating `RING_BASE` to `0x3800D000` (high in SRAM4, above OpenAMP's active
footprint) eliminated the torn reads and the crash-loop — stream clean with `dropped=0`/`crc_err=0`/
`overrun=0`, `rate1=512`, `hwm=1`. The 32-byte cache-line alignment was NOT the fix (alignment alone
still corrupted); it's retained as good practice. Earlier theories (cache straddle, layout mismatch)
were falsified along the way — see ledger.

**Accepted design + residual risk (decision 2026-06-29):** keep the ring at `0x3800D000`, cap 256,
inside OpenAMP's *reserved* SRAM4 region (linker: `__OPENAMP_region_start__=0x38000400` ..
`__OPENAMP_region_end__=0x38010000` = all of SRAM4) but above its *active* vrings/buffer pool. We
chose to keep OpenAMP rather than disable it, because OpenAMP (a) boots M4 and (b) provides the
non-cacheable MPU region that makes the ring coherent for free. **Residual risk accepted:** if
OpenAMP's active footprint ever grows past `~0x3800D000` (mbed/RPC version bump, bigger rpmsg pool,
heavier RPC traffic), the collision can return. See §"IF THE COLLISION RECURS" below. The stray
`PGAL_ALM` is a separate ADC front-end item (not ring-related).

### IF THE COLLISION RECURS — where to look first
- **Canary at boot:** `[M7]/[M4] ring build-id: slot=32B cap=256 base=0x3800D000`. If base/size
  drift or the cores disagree, fix that first.
- **Signature:** torn sample fields — small/index-like values (e.g. `1024`, `1`) bleeding into
  `raw`/`hw_us`/`seq`/`t_ms`; `[STATUS]` `rate_other` > 0 and `dropped_total` climbing; and the tell
  of an M4 reboot loop — `t_ms` snapping back to ~3969 (M4's boot-delay value) every few seconds.
- **Confirm it's the collision (not something else):** flash `Firmware_stable` — if *it* still
  streams clean, the fault is in the merged build's ring placement, i.e. OpenAMP grew into us.
- **Map OpenAMP's real footprint:** `arm-none-eabi-readelf -s .pio/build/portenta_m7/firmware.elf
  | grep OPENAMP` for the reserved bounds; for the *active* top, run the sentinel scan (fill SRAM4
  with a pattern before `RPC.begin()`, then report the highest overwritten address).
- **Fixes, in order of effort:** raise `RING_BASE` further / shrink `RING_CAPACITY`; or disable
  OpenAMP entirely (`bootM4()` to start M4 + self-managed cache coherency via
  `SCB_CleanDCache_by_Addr`/`SCB_InvalidateDCache_by_Addr` — NOT MPU-non-cacheable, which has a
  documented "hang after a few seconds" failure on the H7) and own all 64 KB of SRAM4.

---

## 1. How to recognize this issue

- Reading sits at a fixed value regardless of the physical input (move the laser target /
  load the cell → no change).
- The value is **not** zero, full-scale, or obviously garbage — it equals a *plausible*
  reading of whatever the input was **at the moment of boot/config**.
- The stream is otherwise healthy: **fresh, incrementing timestamps and `seq`**, CRC passes
  (`crc_err` not climbing), `ID=0x23`.
- May present as "stable frozen" (PGA bypassed) or "jumping around a frozen mean" (PGA in path).

> Key tell: **fresh timestamps + frozen raw code that decodes to a real boot-time voltage.**
> That means the value was already frozen when it was read from the chip — the data pipeline
> (ring buffer, host parser) is downstream and faithful, not the cause.

---

## 2. Fast triage checklist (run top to bottom — cheapest first)

1. **Power-cycle properly.** Full USB + EVM supply off, ~5 s, on. DFU reset after a flash does
   NOT re-power the EVM analog rails → chip comes up `ID=0x00`. Confirm boot shows `ID=0x23`.
   (All-zeros stream = chip never initialized = power-cycle issue, not a freeze.)
2. **Read the boot `REGDUMP` / `printConfig`.** Confirm `INPMUX=0x45`, `REFMUX=0x09`,
   `MODE2` (bit7=0 in-path / 1 bypass), `POWER=0x13`, `ADC2MUX=0x23`. Wrong values → config bug.
3. **Watch the STATUS byte alarms.** Driver logs `REF_ALM` (bit4, reference low),
   `PGAL/PGAH/PGAD_ALM` (PGA overrange). Bit0 in the status byte = device RESET indicator
   (set when `POWER` bit4=1, i.e. `0x13`).
4. **A/B against `Firmware_stable/`.** Flash the known-good pre-merge build (§5). If *it* tracks
   and the current build freezes → it's a firmware regression, not hardware. **This is the
   single most useful test.**
5. **Cross-check with the TI EVM GUI.** Program the chip with our register values (§5) via the
   GUI and read the laser. If the GUI tracks → chip + wiring + reference are all good, and the
   fault is in how *our firmware* operates the chip. (Note: GUI uses the EVM's onboard
   controller as a SECOND SPI master — never have it connected at the same time as the Portenta.)
6. **DMM the chip-side pins.** Measure AIN4 vs AIN5 at the EVM screw terminals AND at the chip
   test points (TP18 = A4, TP19 = A5, after the R9/R10 1 kΩ filters) while moving the target.
   Signal present at the pin but ADC frozen → fault is internal to the chip's operation, not wiring.
7. **Bypass the ring buffer.** Add a throttled (1/s) RPC print of the raw value straight from
   `readADC1Direct()`, *before* `ring_push`. Frozen there too → freeze is at the chip read;
   tracks there → the buffer path is the culprit.

---

## 3. Diagnostic toolbox (techniques that worked here)

- **`Firmware_stable/` as a golden reference.** Exact snapshot of `SensorHub_PIO` @ `09f0502`,
  POWER=0x13, Direct reads, M7 = pure bridge. Reads the laser correctly. Use it to A/B anything.
- **TI EVM GUI** to prove the silicon + analog front end independently of our firmware.
- **`git log -S '<unique code line>'`** to find which commit introduced/removed a behavior, and
  `git show <commit>:<path>` to pull exact historical files.
- **Normalized diff** (strip comments/whitespace) to compare driver/firmware versions for *real*
  code differences vs. cosmetic churn.
- **The boot `REGDUMP`** (`[M4] REGDUMP ... POWER=0x.. INPMUX=0x.. MODE2=0x.. [pga_bypass_test=.. intref_test=..]`)
  to confirm what's actually programmed in silicon vs. what the source claims.

---

## 4. Hypothesis ledger

| # | Hypothesis | Status | Evidence |
|---|---|---|---|
| 1 | PGA common-mode violation (AIN5 at ground, Eq.12) | ❌ ruled out | Bypassing PGA didn't restore tracking; stable works PGA in-path |
| 2 | Wrong ADC register values | ❌ ruled out | GUI tracks with our exact registers |
| 3 | External reference (`REFMUX=0x09`, IR drops on R5/R6) | ❌ ruled out | GUI tracks with ext ref; `REF_ALM` never fired |
| 4 | Dual-ADC concurrency (ADC1+ADC2) | ❌ ruled out | GUI runs both ADCs, both track |
| 5 | `POWER` 0x13→0x02 / INTREF off | ❌ not the freeze | Real driver regression (commit 093469b) but restoring 0x13 didn't fix |
| 6 | DRDY interrupt interfering | ❌ ruled out | Disabled the ISR; still froze |
| 7 | Chip / EVM / cabling damage | ❌ ruled out | GUI reads fine; DMM shows signal at chip pins (A4 tracks) |
| 8 | START-pin contention (R22 pulls START high, firmware uses commands) | ❌ not the cause | Stable build has same floating START and works (version-independent) |
| 9 | M7 CPU-starved / "too busy" | ❌ not credible | 480 MHz M7 doing trivial I²C/analog/GPIO; idle |
| 10 | Ring/buffer **layout** (24-byte slot straddles M7 32-byte cache line → torn reads) | ❌ ruled out | 32-byte cache-line-aligned slots (confirmed running via build-id) did NOT fix it. Straddle was not the cause. |
| 11 | **SRAM4 address collision** — RPC/OpenAMP writes above `0x38007FFF`, overlapping ring at `0x38008000` | ✅ **CONFIRMED root cause** | 2026-06-29: moving `RING_BASE` to `0x3800D000` (cap 256, top of SRAM4) eliminated all torn reads + the crash-loop; stream clean (`dropped=0`, `rate1=512`, `hwm=1`). Old layout's bigger slot shifted live slots onto memory RPC actually uses; stable's smaller 16-byte ring sat clear of it. |
| A | Pin collision / physical short on J15 (M7 SMA pin tied to an ADS1263 SPI/control line) | ❌ ruled out | Bench continuity check 2026-06-29: DRDY confirmed on J15-27 (PC_6) end-to-end; no short among CS/DRDY/RESET (J15-25/27/29) and the M7 MOSFET/TRIG/I²C pins (J15-31/33/26/28); all six Cable-1 pins seated. Wiring is good. |
| B | M7 SMA path (GPIO/analogRead/I²C/loop-timing) reaches the converter | ❌ ruled out | Bench test 2026-06-29: M7 compiled bridge-only (`SMA_DISABLE=1`, just `pumpSensors()`), ADC2 also off (laser-only). Laser STILL frozen at boot value (~0.559 V) with live noise + incrementing seq. M7 doing nothing but draining the ring does not restore tracking. |
| — | **Regression from the SMA merge** | ✅ confirmed | `Firmware_stable` (pre-merge) tracks with byte-identical ADC config |

> **J15 odd-row adjacency (for reference):** J15-25 CS, -27 DRDY, -29 RESET (M4 control) sit
> next to J15-31 MOSFET, -33 TRIG (M7 SMA); even row interleaves SPI (SCLK/CIPO/COPI on 20/22/24)
> with M7 I²C (SDA-26, SCL-28). Verified no cross-shorts 2026-06-29.

### New symptom observed (2026-06-29, bridge-only run)
- The laser stream is **clean + frozen for ~1 s (~500 samples, seq 0→~499) then collapses into
  total garbage** — every column (raw, seq, hw_us, V→`ovf`) becomes random 32-bit values. The
  formatter is still running (5 tab-separated columns), so M7 is faithfully printing **garbage
  ring slots**: either `write_idx` got corrupted or M4 stalled ~1 s in, after which M7 drains
  uninitialised SRAM4. This localises the fault to the **M4 → ring path**, not M7.

### Open hypotheses to test next
- **A. ~~Pin collision (hardware).~~** ❌ **Ruled out 2026-06-29** by continuity check (see ledger).
- **B. ~~M7-bridge build.~~** ❌ **Ruled out 2026-06-29** — bridge-only M7 still freezes (see ledger).
- **C. M4 per-loop SRAM4 writes — NOW PRIME SUSPECT.** The merged M4 loop writes
  `seq_per_src[0]`/`overrun`/`m4_now_us`/`m4_now_ms` to non-cacheable SRAM4 (D3 domain) **every
  iteration**, unsynchronised, in the same header region as `write_idx`/`read_idx`. `Firmware_stable`
  does none of this. The loop has **no delay**, so these run millions of times/sec — a far higher
  store rate to the shared header than `ring_push` (~500/s). Prime candidate for both the freeze
  and the ~1 s garbage corruption.

### Fix applied (2026-06-29) — M4 rebuilt as pure sensor producer
Per the confirmed architecture (M4 = ADS1263 sensor collection ONLY; M7 owns SMA/USB/everything),
M4's per-loop header writes were **removed entirely** (`TEST_M4_SRAM4_PERLOOP=0` = rebuilt
pure-producer loop; `=1` restores the old per-loop writes for A/B). M4 now only polls the ADC and
`ring_push`es. M7 stays bridge-only (`SMA_DISABLE=1`) so M4 is the only variable.

**Verify on bench:** flash M4 only, power-cycle, monitor.
- garbage after ~1 s gone **and** laser tracks → root cause = the per-loop SRAM4 writes (C); confirm
  with the `TEST_M4_SRAM4_PERLOOP=1` A/B (symptom should return).
- garbage gone but still frozen → corruption was C, freeze is separate → Step D (bypass-the-ring).
- no change → revert, escalate to Step E (incremental rebuild from `Firmware_stable`).

**Follow-up when SMA is re-enabled:** M7 previously stamped SMA `src=3/4/5` with M4's published
`m4_now_us`/`m4_now_ms`. With M4 no longer publishing those, M7 must stamp those samples on its
**own** clock (or read the freshest sample's `hw_us` from the ring). `[STATUS]` will report
`m4_loops_per_s=0`/`overrun=0` until/unless that telemetry is re-derived on M7 — harmless.

### Investigation milestone (2026-06-29) — torn-read symptom + the cache-straddle hypothesis [SUPERSEDED]
> ⚠ **SUPERSEDED:** the cache-line-straddle theory below was **falsified** — 32-byte aligned slots
> did NOT fix it. The confirmed root cause is the **SRAM4 address collision** (see the top of this
> doc and ledger #11). This section is kept as the investigation record: it correctly nailed the
> *symptom* (periodic M4 crash-reboot loop from torn ring reads) but mis-attributed the *mechanism*.

The "frozen reading" was a misframe. The real fault is a **periodic M4 crash-reboot loop**:
M4 streams clean for ~1 s (~500 samples), the shared ring corrupts, and M4 resets — `t_ms`
snaps back to ~3969 (its full boot: 3000 ms power-up wait + RPC + settle), then repeats. The
"garbage" is byte-for-byte **identical** every cycle → deterministic, not random RAM.

**Two decisive bench checks (2026-06-29):**
- **Check 1 — merge vs hardware:** `Firmware_stable` streamed clean to **t_ms=106747** with no
  reset. → the crash-loop is in the **merge**, not power/USB/host.
- **Check 2 — corruption signature (merged, bridge-only + rebuilt M4):** even in the "good"
  windows, individual sample fields read as **`1024` (= `RING_CAPACITY`) or `1`** —
  ring index/header values bleeding into `raw`/`hw_us`/`seq`/`t_ms`. `[STATUS]` showed
  `rate_other=780` (M7 popping 780 samples/s whose `src` is neither 1 nor 2 = corrupt) and
  `dropped_total` climbing (M7 pops ~1328/s vs M4's ~500/s = phantom slots). Classic **torn
  reads of the SPSC ring**.

**Mechanism:** the merge grew the slot **16 → 24 bytes** and the ring **16 → 24 KB**. The
Cortex-M7 D-cache line is **32 bytes**. Stable's 16-byte slots pack 2-per-line and never cross a
line; the merge's **24-byte slots straddle 32-byte cache lines** and adjacent slots **false-share**
a line, so M7's cached view of a slot M4 just wrote (M4 has no cache, writes straight to RAM) is
partly stale → torn fields. `sample_ring.h` *assumes* SRAM4 is non-cacheable ("Mbed MPU defaults")
but that assumption fails for the merged layout at `0x38008000+`. Corruption escalates until a bad
index faults a core → the ~1 s reboot loop. The larger 24 KB footprint is a secondary risk
(pushes the ring deeper into SRAM4). The SPSC push/pop logic itself is correct (`__DMB` ordered).

**Fix (step 1, alignment):** pad `AdcSample` to **32 bytes** + `aligned(32)`, 32-byte align
`samples[]`, drop `RING_CAPACITY` to **512** (≈16 KB footprint, back in stable's proven range).
Each slot then owns exactly one cache line → no straddle, no false-sharing. Host TSV output is
unchanged (M7 formats to text), so no host-parser impact.

**Step 1 RESULT (2026-06-29): NOT sufficient.** Build-id banner (`[M7] ring build-id: slot=32B
cap=512`) confirmed the aligned image was actually running on both cores, yet the crash-loop +
torn reads persisted. New tell: M7 prints phantom `0 0 0.000000 0 0` sample lines **during M4's
3 s power-up wait — before M4 has pushed anything** → `write_idx` goes non-zero on its own. That is
M7↔M4 incoherence on the merged ring layout, not merely cache-line straddling. **Confirmed
independently that the laser/ADC is fine: `Firmware_stable` (16-byte slot, same `RING_BASE`)
tracks the voltage and runs 106 s+ with zero corruption.** So the ADS1263 "frozen reading" was
always a symptom of ring corruption + the crash-loop, never an ADC fault.

**Step 2 — what we did:** of the options weighed (A: revert to stable's 16-byte ring; B: keep
32-byte + explicit M7 D-cache maintenance; C: relocate `RING_BASE` clear of OpenAMP), we took **C**.
Relocating to `0x3800D000` fixed it outright and let us keep the richer 32-byte telemetry — which
also proved the cause was a *collision*, not cache coherence (B would have been needed if it were
coherence). The accepted final config and the recurrence playbook are at the **top of this doc**.

---

## 5. Reference data

**Known-good register config (laser, ADC1):**
`POWER=0x13`, `INTERFACE=0x05`, `MODE0=0x00`, `MODE1=0x40` (Sinc3), `MODE2=0x08` (in-path, gain 1,
400 SPS), `INPMUX=0x45` (AIN4/AIN5), `IDACMUX=0xFF` (IDAC off), `IDACMAG=0x00`, `REFMUX=0x09`
(ext REF7050 on AIN0/AIN1). ADC2 (load): `ADC2MUX=0x23` (AIN2/AIN3), `ADC2CFG=0x88`, gain 1.

**Known-good firmware:** `Firmware_stable/` = `SensorHub_PIO` @ git `09f0502`.
Flash: `pio run -e portenta_m7_bridge -t upload` (once) → power-cycle →
`pio run -e portenta_m4 -t upload` → power-cycle → `pio device monitor` (115200).

**Pin map (Mid Carrier → ADS1263 EVM):** CS=PA_8 (PWM0), DRDY=PC_6 (PWM1), RESET=PC_7 (PWM2),
SCLK/COPI/CIPO on SPI1. START pin **not wired** (pulled high by EVM R22). SMA controller pins:
MOSFET=PG_7 (D3/PWM3), TRIG=PJ_11 (PWM4 — *historically the ADS1263 DRDY pin before reroute to PC_6*).

**Datasheet refs:** Eq.12/13 + §9.3.6 (PGA input range / bypass), §9.4.1.1 (START pin: hold low
when using commands), Table 31 (STATUS byte). EVM guide §3 (R22 START pull-up; R9/R10 input filters
+ TP18/TP19).

---

## 6. Lessons / gotchas

- **Power-cycle the rig (USB + EVM) after every flash** — or `ID=0x00`.
- **"Fresh timestamps + frozen value" localizes the fault to the chip read, not the pipeline.**
- **The TI GUI + a known-good `Firmware_stable` are the two fastest A/B tools** — reach for them
  before theorizing.
- **Bring-up "PASS" ≠ laser verified** — cp6–cp10 tested internal sources (AINCOM-short, TDAC),
  not a live external signal on AIN4/AIN5.
- Don't conflate a register *value* being correct with the chip being *operated* correctly —
  the GUI proved the values were right while our firmware still froze.
- **The real cause was none of the early theories** — not PGA CM, not the ADC config, not cache-line
  straddle, not a layout mismatch. It was an **SRAM4 address collision**: OpenAMP/RPC writes memory
  above the `0x38007FFF` the code *assumed* it was confined to, and the merged ring sat in it.
  Lesson: don't trust a comment's claim about who owns a memory region — `readelf` the `.elf` for
  `__OPENAMP_region_start__/end__`, and when in doubt **relocate and observe** rather than theorize.
- **`Firmware_stable` "works" was the anchor** — it ran clean at the *same* `RING_BASE`, which is
  what proved the bug was the merged build's *layout/footprint* shifting slots onto live OpenAMP
  memory, not the address itself.
- **Identical, deterministic garbage ≠ random RAM** — byte-for-byte repeatable corruption pointed at
  a second writer (OpenAMP), not uninitialized memory or a timing race.
- **`t_ms` resetting to the boot-delay value = a core is crash-looping**, not a frozen reading.
- **Bake a build-id canary into the boot banner** (`slot=… cap=… base=…`). It instantly answered
  "is the new image really running on both cores?" and is now the first check if this recurs.
