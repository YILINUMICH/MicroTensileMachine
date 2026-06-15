# PLAN — Phase 5: Spring smoke test + SMA-ready pipeline hardening

> **Self-contained handoff doc.** Designed to be picked up by a fresh AI agent or operator who has not seen the prior conversation.

**Status:** **planning — not yet started.** No firmware changes committed; no bench time scheduled.

**Owner:** Yilin.

**Last edited:** 2026-05-29.

---

## TL;DR

Before installing an SMA wire into the rig, run a **spring-as-SMA-surrogate smoke test** on `SensorHub_PIO/` to (a) validate dual-sensor integration (laser displacement + load cell) against ground truth from the load-cell calibration spring, and (b) prove the M4 sample pipeline has CPU and bandwidth headroom at **1 kSPS per ADC** — the rate we will need once the SMA state machine and on-chip ADC channels start competing for M4 cycles.

The test forces several architectural decisions that benefit the SMA integration regardless of how the smoke test turns out: DRDY-driven sampling, sample-slot instrumentation (sequence numbers + hardware timestamps), a `[STATUS]` telemetry frame, and a command channel from M7 to M4. All of those are scoped here so the SMA addition (Phase 6) is a mechanical+wiring task, not a firmware redesign.

The theoretical CPU budget says everything fits. This phase exists because we want to **see** the headroom, not assume it.

---

## Motivation

Three problems converge:

1. **Sensor integration check.** The laser-head and load-cell calibration runs each validated one sensor at a time. We have never run them together against a known mechanical input. The spring gives us Hooke's-law ground truth (F = kx) — a straight line on the F-vs-x plot if both channels are synchronized and scaled correctly. Any hysteresis loop is timing skew between channels.

2. **1 kSPS bring-up.** The SMA characterization plan calls for higher sample rate than the current production 400 SPS. We need to verify the M4 → ring buffer → M7 → USB-CDC path holds up at 1 kSPS combined ~2 kHz sample rate without dropping samples or jittering timestamps.

3. **SMA-readiness.** Adding SMA introduces a control state machine, two on-chip ADC channels (V/I for resistance), and a heater output — all on M4. We need the sample pipeline to be deterministic *before* M4 has those additional jobs. The spring test is the last chance to harden the pipeline against an isolated workload.

The spring test answers all three with one rig configuration.

---

## Test fixture and procedure

### Fixture

- **Spring**: same spring used in the load-cell calibration run. Already characterized — `k` is known, so it doubles as a load-cell sanity check.
- **Stage zero**: Zaber position at absolute 10 mm = mechanical zero (spring slack). Pre-tensioning was tried and proved impractical; instead the slack region is identified and removed in post-processing.
- **Sensors**: Keyence IL-030 laser (ADC1 path) + LCA-9PC load cell (ADC2 path), production wiring per `SensorHub_PIO/` current main.cpp.

### Procedure

Each step is a separate Zaber motion + matching firmware capture. All steps repeated at 400 SPS and 1 kSPS for comparison.

| # | Motion | Purpose | Pass criterion |
|---|---|---|---|
| 1 | Stage idle at 10 mm for 60 s | Static noise floor | σ on raw counts within tolerance of values from each sensor's calibration run |
| 2 | Quasi-static ramp at 0.1 mm/s through full stroke | Linearity + slope check | F-vs-x slope matches load-cell-cal `k` to within tolerance; laser-vs-Zaber slope ≈ 1.0 outside the slack region |
| 3 | Position step and hold | Settling time on both channels | Both channels settle within expected filter time; no ringing beyond spring-resonance frequency |
| 4 | Fast pull / slow return (mm/s vs 0.1 mm/s) | Sync diagnostic | Forward and reverse F-vs-x curves overlay |
| 5 | Pipeline stress: stage idle, both ADCs at max rate, 10 min | Pure pipeline endurance | Zero dropped samples; status frame shows ring high-water mark <50% |
| 6 | Profile from step 4 at 1 kSPS while logging Zaber over COM5 | Concurrent comms stress | Zero drops; no Zaber comm timeouts |

### Ground-truth cross-checks (per ramp)

The 10 mm zero + slack region means each ramp gives **three independent measurements** of the same mechanical event:

- Zaber encoder position (commanded + reported)
- Laser displacement
- Force, related to displacement via known `k`

Plotting all three lets us isolate which channel disagrees with the other two.

---

## Pipeline changes required for the test

These are scoped to land before the bench session. They are **also the foundation for Phase 6 (SMA)** — none of them are spring-specific.

### Change 1 — Grow `AdcSample` to 24 bytes

Current slot is exactly 16 bytes with no spare bits (see `SensorHub_PIO/src/sample_ring.h`). The SRAM4 partition allocated to the ring is 32 KB; current ring uses ~16 KB. We have room to grow.

New layout (target):

```c
struct AdcSample {
    uint32_t hw_us;        // free-running timer captured at SPI read start
    uint32_t seq;          // monotonic per-src sequence number
    uint32_t timestamp_ms; // retained for compatibility with current host parser
    uint8_t  src;          // 1=laser, 2=load, 3..=reserved (see src ID table below)
    // 3 bytes padding
    int32_t  raw_code;
    float    voltage_V;
};
// 4 + 4 + 4 + 4 + 4 + 4 = 24 bytes
```

At 1024 slots × 24 B = 24.6 KB total ring — still inside the 32 KB partition. Buffer-time at 1 kSPS combined drops from ~1.3 s to ~0.5 s, still ample. If we need more headroom we drop to 512 slots (~256 ms at 1 kSPS).

For `hw_us`: free-run TIM5 at 1 MHz (so values are directly µs) and capture at the start of each `readADC?Direct()`. M4 DWT cycle counter is not fully available, so a regular timer is the right path.

### Change 2 — DRDY-interrupt-driven ADC reads

Current `loop()` uses `millis()`-gated cooperative polling at 2 ms intervals. That is fine at 400 SPS with M4 doing nothing else. At 1 kSPS it is fragile. With the future SMA state machine *also* running on M4, cooperative polling will not survive — the state machine can defer the SPI poll by however long its work takes.

Switch the ADS1263 read path to DRDY-edge interrupt:
- DRDY rising edge fires an ISR
- ISR runs the SPI read (or schedules it on a high-priority task)
- ISR writes the slot, including `hw_us` captured at ISR entry, and `seq++`

This is the right path long-term and removes the dependency between sample timing and `loop()` scheduling. Required driver changes: make `readADC?Direct()` ISR-safe (no `Serial`/`RPC` calls in the read path; current driver appears to be clean here but verify).

### Change 3 — `[STATUS]` telemetry frame from M7

M7 emits one line per second:

```
[STATUS] t_ms=12345 hwm=237 dropped=0 rate1=999.8 rate2=1000.1 idle_m4=87
```

Fields: monotonic timestamp, ring high-water mark since last status, dropped-sample counter delta, measured per-src sample rate, and an M4-idle-cycles estimate (rough CPU headroom).

The existing host parser in `Calibrate_LaserHead/portenta_reader.py` already drops any line containing `[`, so the new line type is non-breaking. A separate consumer on the host explicitly matches `^\[STATUS\] ` and stores it as a parallel time series.

### Change 4 — `src` ID reservations

Reserve `src` values now so Phase 6 doesn't need a format-versioning conversation:

| `src` | Meaning | Path |
|---|---|---|
| 1 | Laser displacement (ADS1263 ADC1) | Existing |
| 2 | Load cell force (ADS1263 ADC2) | Existing |
| 3 | SMA voltage (on-chip ADC) | Phase 6 |
| 4 | SMA shunt current (on-chip ADC, voltage-format) | Phase 6 |
| 5 | SMA resistance (M4-computed) | Phase 6 |
| 0xF0–0xFF | Reserved for state-machine events (not sample data) | Phase 6 |

### Change 5 — Optional: binary frame format on the wire

TSV is human-readable but costs M7 several `Serial.print()` calls per sample, each scheduling a USB transmission. If the spring test reveals M7 saturation at 1 kSPS, switch to a length-prefixed binary frame per sample (~24 B raw → ~30 B with framing). UDP is a deferred fallback if binary serial still doesn't hold; not expected to be needed (USB-CDC has ~1 MB/s effective throughput vs the ~48 kB/s required).

---

## SMA-ready architecture (Phase 6 preview, decided now)

These are not implemented in Phase 5 but the IPC contracts and shared structures are designed now so Phase 6 is wiring + state-machine logic, not a redesign.

### M4 responsibilities (post-Phase-6)

```
M4 loop:
├─ DRDY1 ISR → read ADS1263 ADC1 (laser) → ring (src=1)
├─ DRDY2 ISR → read ADS1263 ADC2 (load)  → ring (src=2)
├─ On-chip ADC DMA buffer drain → SMA V (src=3), I (src=4)
├─ Compute R = V/I → ring (src=5), also feed control state machine
├─ State machine: idle → heating → hold → cooling → idle
│  ├─ Inputs: current R, V_drive setpoint, t_drive setpoint
│  ├─ Outputs: heater control (PWM or DAC), state events to M7
│  └─ Transition decisions use M4-local R (deterministic)
└─ Poll shared SMACommand struct for new setpoint from M7
```

Resistance is computed on M4 (not M7) because the **state machine uses R as feedback**. Pushing raw V/I to M7 and computing R there only to send it back is backwards. M4 also pushes raw V and I through the ring so the host has full diagnostics — the bandwidth cost is trivial.

### Command channel: M7 → M4

Single-slot shared-memory struct in SRAM4, alongside the sample ring:

```c
struct SMACommand {
    volatile uint32_t seq;          // M7 increments after fully writing fields
    volatile float    V_drive_V;
    volatile uint32_t t_drive_ms;
    volatile uint32_t flags;        // start_cycle, abort, fault_clear, ...
};
```

M4 reads each loop iteration; applies the command if `seq > last_applied_seq`. "Always use the latest setpoint, drop stale ones" semantics — appropriate for setpoint updates where queuing doesn't make sense. M4 echoes `last_applied_seq` in the `[STATUS]` frame so the host can confirm receipt.

For discrete events that must not be coalesced ("start cycle", "abort", "fault clear"), use a small second ring (M7 producer, M4 consumer) or just RPC — these are low-rate and RPC's back-pressure problem doesn't bite at command rates.

### State event channel: M4 → M7

State-machine events ("entered heating", "overtemp fault") must be **guaranteed delivery** — the sample ring is allowed to drop in overload, which would also drop fault notifications if mixed. Use either:

- A second small ring dedicated to state events, OR
- Reuse RPC for these (low-rate, back-pressure is fine for tens of events/sec)

These get distinct `src` values (0xF0+) when they appear on the host stream, or live on a separate channel — to be decided in Phase 6.

### Inference location (PC vs M7) — deferred, but IPC contract is the same

Whether trained models run on the PC or on M7 doesn't change the IPC contract: model output is `(V_drive, t_drive)`, applied via the `SMACommand` struct. We can build the rig with PC-side inference (USB-CDC: PC → M7 → M4, ~5 ms end-to-end) and migrate to M7-side inference later (no USB hop, sub-ms) without touching M4 firmware.

M7 has CMSIS-NN / TFLM capacity for models up to a few hundred KB given the Mid Carrier's external SDRAM. For SMA control, where thermal dynamics are 10s of ms to seconds, the location doesn't matter for closed-loop performance.

---

## What this phase does NOT cover

- **No actual SMA installed.** Spring only. Heater output, on-chip ADC wiring, and state machine are Phase 6.
- **No host-side ML inference.** Test produces logged sample data; ML training is downstream.
- **No new mechanical fixture beyond the spring.** Same Zaber stage, same grips as load-cell calibration.

---

## Definition of done

Phase 5 is complete when:

1. Sample slot grown to 24 bytes with `hw_us` + `seq`; static_assert updated; `Calibrate_*_PIO` modules that share the header still build and produce valid streams.
2. DRDY-interrupt-driven sampling lands in `SensorHub_PIO/`; sample timestamps show <100 µs jitter at 1 kSPS over a 10-minute pipeline-stress run.
3. `[STATUS]` frame emitted by M7 every 1 s; host-side parser stores it; existing TSV sample parsing unchanged.
4. Spring smoke test runs completed at 400 SPS and 1 kSPS; F-vs-x linearity confirms sensor integration; per-channel σ matches the calibration-run reference noise floors.
5. No dropped samples on the 10-minute 1 kSPS endurance run; status frame `hwm <50%` of ring capacity throughout.
6. `src` ID reservation table in `sample_ring.h` (as a comment) so Phase 6 has them documented.
7. Optional: `SMACommand` struct and command-ring scaffolding committed but unwired — Phase 6 only needs to populate and consume them.

---

## Open decisions deferred to bench / Phase 6

- Binary-framed vs TSV on the wire — decided after we see M7 CPU at 1 kSPS TSV.
- UDP fallback transport — deferred; only revisit if binary-framed USB-CDC still drops.
- Inference on PC vs M7 — deferred; not gating Phase 5.
- Whether on-chip ADC sampling uses DMA double-buffer or single-shot per loop — to be sized once SMA wiring and noise characteristics are known.
- State-event channel: dedicated ring vs RPC — to be decided in Phase 6 based on event rate.

---

## References

- `SensorHub_PIO/src/sample_ring.h` — current 16-byte slot, SRAM4 placement, ring API.
- `SensorHub_PIO/src/main.cpp` — current M4/M7 split, production ADC routing.
- `Calibrate_LaserHead/portenta_reader.py` — host parser, drops `[` lines (so `[STATUS]` is non-breaking).
- `Calibrate_LoadCell/` — source of the calibration spring used in this test and the reference noise floor.
- `doc/MEMO_cable_map.md` — current rig wiring; SMA additions will extend this.
- Prior phases: `PLAN_phase1_followups.md`, `PLAN_phase3_sensors.md`, `PLAN_phase4_production.md`.
