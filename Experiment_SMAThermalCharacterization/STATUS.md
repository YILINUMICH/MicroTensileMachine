# Experiment_SMAThermalCharacterization — STATUS

| Field | Value |
|---|---|
| **Status** | **WIP / To-Test** — forked from `Experiment_SMACharacterizationV3`; inherits its verified state (imports + offline analyzer + offscreen GUI + headless flow on synthetic data), **not yet bench-run**. The thermal-specific stream/analysis is **not yet added** — currently identical to V3. |
| **Role** | Multi-instrument SMA **thermal** characterization **console** + analyzer. One config sets every instrument/sensor parameter; one continuously-logging session records raw LCR + H7 (sensors **and** SMA, src=1–5) + Zaber stage; offline analyzer converts raw→physical and renders dashboards. **Planned:** add a temperature stream (thermocouple / IR) to correlate SMA temperature with the Joule-heating drive. |
| **Builds on** | `Experiment_SMACharacterizationV3` (direct fork / architecture), the combined firmware `Firmware_SMASensorHub_PIO` (H7 stream), `Driver_KeysightLCR`, `Driver_ZaberStage`, and the extended `Calibrate_LaserHead/portenta_reader.py`. |
| **Owner** | Yilin |
| **Quick test (no hardware)** | `python -c "import config, workers, recording_core, sma_console, analyze_sma"` then run the analyzer on a synthetic console session (see README). GUI: `QT_QPA_PLATFORM=offscreen` + `run_gui(..., _build_only=True)`. |

## 2026-08-07 evening — USB wedge SOLVED and self-healing (verified); EVM hardware fault is now the campaign blocker

The wedge is **not** firmware-side and **not** the blocking-write mechanism
hypothesized at 03:46: the bench proved it is the **Windows `usbser.sys`
driver instance wedging** (manual driver disable/enable revived a wedged
port with the H7 untouched and 4 min of uptime on its clock). The
`Firmware_SMAConstantCurrent_PIO` nbtx image now detects a dead link by
windowed byte-throughput and heals it with a 1.5 s USB re-enumeration
(escalating to a self-reset if needed) — **verified twice: wedge to
flowing stream in ~41 s with no human intervention** (`usb_heal` counter in
[STATUS] records every event; an `IWDG WATCHDOG` boot line records crash
recoveries). Full experiment chain and the fix table:
`Firmware_SMAConstantCurrent_PIO/STATUS.md`, top entry.

What this changes operationally:
- **No more power-cycle ransoms mid-campaign**: a wedged step recovers
  itself within ~40-65 s; `run_step.py`'s existing 90 s silent-port retry
  will find a healed port. The `-BetweenS 300` spacing mitigation is no
  longer load-bearing (still harmless).
- Wedge-window data is dropped, not delayed (`tx_drop` + `seq` gaps make it
  visible); a wedged capture was always lost — now the *next* one isn't.
- The heat watchdog still guards the coil during a wedge (it disarms 5 s
  after pings stop), and the IWDG bounds a crashed core at 4 s.

**The campaign blocker is now the ADS1263 EVM, not USB:** post-verification
[STATUS] shows `rate2=0` (ADC2 dead), a ~630/s CRC storm, ADC1 sagging to
~390/512 — and power cycles no longer clear it. That is restart-checklist
item 1 (reseat EVM ribbon + supply), to be done before the nbtx acceptance
sweep (`tx_drop≈0`, `usb_heal=0`, `rate1/2=512`) and campaign attempt 4.
The sense-lead σ≈66 mA fault and the median-estimator analysis debt are
unchanged by tonight.

## 2026-08-07 — USB wedge: fix path from the 03:46 entry EXECUTED (firmware built, awaiting bench)

The wedge fix proposed below is implemented on branch **`fix/usb-cdc-wedge`**,
with the mechanism now confirmed by reading the installed ArduinoCore-mbed
source rather than inferred: the blocking chain is
`Serial.write → USBSerial::write → per-64-byte blocking USBCDC::send()`, and
`send_nb()` exists and is reachable — details, artifacts, bench sequence and
rollback are in **`Firmware_SMAConstantCurrent_PIO/STATUS.md` (2026-08-07
entry)**. Summary:

- `[env:portenta_m7_nbtx]` — the fix: ALL M7 serial output through a
  time-bounded `send_nb` sender; the loop (and therefore `wdt`/`hb`/disarm)
  can no longer hang on a dead CDC endpoint. Wire format unchanged.
- `[env:portenta_m7_wedgeled]` — baseline stock-TX build + a loop-liveness
  LED, so the wedge remains reproducible and the LED discriminates
  loop-hung vs endpoint-dead at a glance.
- `Firmware_SMAConstantCurrent_PIO/tools/torture_open_close.py` — the
  ~rapid-open/close torture harness (the ≤10 s reopen pattern that failed
  6/6 on 08-07), the before/after yardstick.
- The default `portenta_m7` env rebuilds **byte-identical** (sha256 checked)
  — flashing it is the rollback at any point.

The three suspects from the fault hunt, settled by code reading: the M4→M7
ring cannot hang (non-blocking by construction, overflow = counted drops);
the always-on stream is the *exposure* (no pause command exists — every close
abandons a live stream into the CDC endpoint) but not the mechanism; the
`wdt`/`hb` heartbeats only `disarm()` and are exonerated — the load-bearing
check was `hostUp()`'s stale-able DTR cache next door, which the bounded
sender now makes non-load-bearing.

**Step 4 of the restart checklist below is therefore ready to run** — bench
sequence in the firmware STATUS entry (wedgeled A/B → nbtx torture + soak →
one full sweep with `tx_drop=0` before the campaign trusts it).

## 2026-08-07 03:46 — third campaign attempt: 15 min, killed by the RIG (twice over). Night closed

The queue launched at 03:31 (NOTE: with the default 30 s gap — `-BetweenS 300`
was not on the command, so the wedge mitigation below was never actually in
play) and ended 15 minutes later, correctly:

| profile | outcome |
|---|---|
| n0_anchor_start | **1/2** — the 650 mA anchor landed (`sweep_20260807_033117`), then **ADC2 died mid-profile** at the 450 mA condition |
| n4a | 0 captures — port fully silent on open |
| n1a | 0 captures — **circuit breaker fired, queue abandoned** |

n0's one capture is questionable beyond the known sense fault: the pulse finder
sees 10 heat windows where 6 pulses were commanded, with ΔR outliers (−17 %,
−10.6 %) — the rig was degrading *during* the capture. Anchors are re-run at
campaign restart anyway; the partial stays filed under the queue manifest.

**The rig now carries two independent faults:**
1. The sense-lead glitching (σ ≈ 66 mA) — stable, characterized, payload
   median-recoverable. Livable (see the 03:30 decision below).
2. **ADC2 dying + total port silence — twice in ~90 min tonight**, against an
   occasional historical rate. Not livable: no estimator recovers data that was
   never captured. Likely the same bench disturbance as (1): EVM power/SPI
   seating is the first suspect.

### The USB wedge, mechanism found (and the heartbeat checks exonerated)

The operator suggested the heartbeat. Checked: `wdt` (HEAT-only) and `hb`
(armed-only, and disabled all night) both do exactly one thing — `disarm()` —
and cannot mute TX. But the check NEXT DOOR is the story. `hostUp()` gates
every stream write on a 250 ms-cached `(bool)Serial`, i.e. mbed's
terminal-connected flag, maintained by USB callbacks. The chain that fits every
observation:

1. Every session close abandons a live ~500 Hz stream (there is NO command to
   pause the src=1/2 sensor stream — checked).
2. If the stack misses the close's DTR-drop callback (a race → randomness),
   `hostUp()` keeps answering "host present".
3. The firmware keeps writing into a dead endpoint until the CDC buffer fills
   and `Serial.write` BLOCKS FOREVER — the same mbed per-write blocking this
   firmware fights everywhere else.
4. The M7 loop is hung inside one write: no samples, no `[STATUS]`, no command
   replies (RX never read again), immune to DTR toggles and reopens. Only a
   power cycle recovers. Verified signature at 01:45: total silence including
   `info`, which bypasses the stream gate.

Consistent with: strikes only after closes; random; identical on two different
H7 boards; gaps of 6–10 min between sessions survived while ≤2 min gaps
wedged; and the 08-05 log's two pre-runner occurrences.

**Fix path (firmware, before campaign attempt 4):** make the stream write
truly non-blocking — `USBCDC::send_nb` if ArduinoCore-mbed exposes it — with
an RX-freshness (`ping`) gate as belt-and-braces; then a torture test of ~50
rapid open/close cycles counting wedges before/after. A polling gate alone
cannot close the race; the write itself must be unable to block.

### Restart checklist (in order, daylight)

1. Reseat EVM ribbon + supply (the ADC2 deaths).
2. `diagnostics/wiggle_monitor.py` on the sense leads / ground return / INA296A
   supply (the σ 66 mA fault) — both faults may share one loose ground.
3. One `corner_probe_reseat` sweep: want 3/3 conditions and ADC2 alive
   end-to-end. (σ ≈ 25 mA if the wiggle test found the fault; σ ≈ 66 mA is
   acceptable-by-decision otherwise.)
4. Firmware wedge fix + torture test, or accept wedge risk and space port
   opens: `run_night.ps1 -BetweenS 300` — actually passing the flag this time.

## 2026-08-07 03:30 — DECISION: run the campaign THROUGH the fault. The payload is median-recoverable

With the fault pinned to the driver-board/harness side and stable at σ ≈ 66 mA
across two H7s and four power cycles, the operator decided to collect anyway.
That decision is backed by a measurement, not hope: **the corruption is
impulsive (single-sample, 20–30 % of samples), and a median estimator's 50 %
breakdown point shrugs that off.** Same conditions, healthy 08-05 capture vs
three faulted 08-07 captures, ΔR per cycle:

| condition | healthy, mean est | faulted, mean est | **faulted, median est** |
|---|---|---|---|
| 750 mA × 400 ms | −7.2 ± 0.7 % | −1.7 … −3.3 % (unstable) | **−7.0 … −8.7, ± 0.5** |
| 950 mA × 300 ms | −8.0 ± 0.4 % | −3.4 ± 0.8 % | **−8.6 ± 0.2** |

`median(V)/median(I)` per window lands on the healthy value with healthy-grade
scatter in every faulted capture. Mean-based estimates are destroyed (wrong by
2–4×, sign-flips at low energy) — which is also why `operator_pulse_capture`'s
in-run ΔR read wrong all night.

**What was changed to let the night run (2026-08-07 03:30):**
- `abort_on_bad_sense` flipped `true → false` in all 13 night profiles. The
  conditions, seeds, roles and run order are UNTOUCHED — only the guard no
  longer kills a profile at its first straddling condition (the glitch
  population sits right at the guard's 1 % limit, so it fires on a coin flip).
  The sense WARNING still prints per condition; quality is still recorded.
- Steps run via `run_step.py next --force` (the ledger's last-sense gate is
  BAD by design — the override is the documented decision, and the ledger
  records `--force` runs like any other).

**Debt this creates, to pay before training:**
1. `analyze_raw.py` window stats use means → its `r_hot`/`r_base`/ΔR columns
   are WRONG for every capture taken in this state. Patch stage 1 to
   median-based window estimators + add a per-window glitch-fraction column
   (quality as a column, per the NO DATA SELECTION rule). Raw CSVs keep every
   sample, so nothing is lost by collecting first and patching after.
2. Firmware `R_est` / CC telemetry still average internally — treat `src=7`
   as approximate for these captures. Achieved current is unaffected (CC
   tracked 99–101 % all night; the loop's 7 ms tau rides through 1 ms spikes).
3. The anchors (n0/n3/n9) measure drift on the SAME estimator — fine, as long
   as the median patch lands before anyone reads them.

## 2026-08-07 02:08 — board swap A/B: the fault SURVIVES a brand-new H7

Old H7 replaced with a new board (new serial, reassigned to COM8; both cores
flashed fresh from `Firmware_SMAConstantCurrent_PIO`, boot verified clean —
`rate1/2=prod1/2=512`, `crc_err=0`, `dac_err=0`; note the fresh M7 image also
publishes `m4_us`, which the old board's stale image never did). Same
`corner_probe_reseat` sweep on each board, minutes apart:

| | old H7 (`sweep_20260807_015514`) | new H7 (`sweep_20260807_020840`) |
|---|---|---|
| σ idle | 69.4 mA | **66.8 mA** |
| R @200 ms | 3.72 % | 3.57 % |
| guard abort | 1.30 % > 1 % | 1.28 % > 1 % |
| actuation | 755 mA, ~1.2 mm | 755 mA, ~1.2 mm |

**The Portenta is exonerated entirely — board, ADC, reference, and its
USB/ground path** (the one thing the AGND test could not rule out). Combined
with the disconnect and AGND tests below, the fault can only live in the
**driver board's analog section and the two sense leads**: INA296A OUT → A1,
the 10k/10k FB divider → A0, their supply, and the ground return they share.
The 08-05 16:10–21:05 window now reads as the pre-flight physically disturbing
that harness, not anything on any H7.

Next: wiggle test with a live glitch-rate monitor (leads, ground return, INA
supply), and meter the INA296A rail.

## 2026-08-07 01:19 — AGND test: the fault is UPSTREAM of the ADC

`pulse_20260807_011920` was taken with **A0 and A1 jumpered to GND** — the split
test from the entry below, not a repaired rig. With both ADC inputs held at a
hard 0 V:

| capture | median I | σ I | max I | sd V |
|---|---|---|---|---|
| 08-05 "healthy" baseline | 124.7 mA | 25.0 mA | 241 mA | 0.073 V |
| 08-07 00:45 faulted, coil attached | 119.7 mA | 63.5 mA | 442 mA | 0.189 V |
| 08-07 01:11 coil disconnected | −4.6 mA | 62.8 mA | 322 mA | 0.201 V |
| **08-07 01:19 A0+A1 to GND** | 103.7 mA | **7.0 mA** | **128.3 mA** | **0.048 V** |

**Every glitch disappears when the inputs are grounded.** σ collapses 62.8 → 7.0
mA and the maximum idle sample lands 3σ off the median instead of +200 mA out.

**So the Portenta's ADC, its reference and its analog supply are all EXONERATED.
The fault is upstream of the ADC pins** — the INA296A, the 10k/10k FB divider,
or the two leads and their ground return. Combined with the finding below that
the ADS1263 is clean and that the glitches survive with the coil disconnected,
what is left is: the driver board's analog section and the harness between it
and the Portenta. Independent glitches on two separate channels that share only
that path point at the shared **ground return** first.

**7.0 mA is the grounded-input noise floor, not an achievable operating figure.**
It is what the ADC does with no source attached; any real source adds its own
noise. It does NOT mean the rig can run at 7 mA, and it does not revise the
25 mA figure of the 08-05 campaign — the correct reading is that the ADC itself
has ~7 mA of intrinsic noise, so the 25 mA seen on 08-05 was already ~3.5× the
floor and consistent with this same fault at 1/100 the glitch rate.

Nothing about the coil can be read from this capture: with both inputs grounded,
the 4.30 Ω "R", the 103.7 mA "current" and the two "pulses" in `pulses.csv` are
all fixed artifacts of the calibration mapping 0 V. The capture confirms as
much — current never exceeded 128 mA against a 650 mA command with `u` driven to
3.16 V, and the laser moved 10 mV, i.e. the coil never actuated.

### Next: which side of the harness

1. **Reconnect A0 and A1 and confirm the glitches return.** The control for the
   above.
2. **Then add a solid, short ground strap between the driver board's analog GND
   and the Portenta GND.** If the glitches go, it is the ground return — the
   single most likely cause given two independent channels are affected.
3. If not, ground ONE input at a time (A1 grounded / A0 live, then the reverse).
   If the still-connected channel keeps glitching on its own, each channel picks
   up independently and the harness or the board's ground is implicated rather
   than one component; if the glitching follows one specific lead, that lead or
   its source (INA296A vs divider) is the culprit.

### The USB-CDC wedge — why steps kept dying on `0 bytes in 2s`, and the fix

Every failed step tonight shares one signature: the port OPENS fine, `force
pull: drained 0.0 kB`, then 0 bytes — while successful opens drain 47–77 kB
(that is ~1 s of live stream, i.e. the firmware writing normally). Measured at
01:45 with the rig untouched: **total CDC silence — no samples, no [STATUS],
and no reply to `info`, which bypasses the `hostUp()` stream gate entirely.**
DTR toggles, baud changes, and reopen cycles held 30 s did not revive it; only
a power cycle does. So this is not the firmware's DTR cache (`HOST_CHECK_MS` is
250 ms) and not a host-side reader conflict: the mbed USB-CDC TX path wedges
outright. **The trigger is rapid close→reopen cycles.** Tonight: reopens ≤10 s
after a close failed 6/6; reopens ≥90 s after succeeded. The 08-05 log carries
the same signature twice (`0 bytes in 2s` / `drained 0.0 kB`, recovering on a
later launch) — it pre-dates the step runner, but the runner's probe-then-sweep
pattern (two opens per step) multiplied the exposure and the sweep child's
open-fail-close retries rolled the dice faster.

`run_step.py` changes (2026-08-07):
- **The separate sense probe is now opt-in (`--probe`).** The default path is
  ONE port open per step — exactly the standalone-sweep flow that never hit
  this. The gate survives without it: the LAST recorded sense verdict in the
  ledger blocks a new step, each step's own first capture is graded in as
  `sense_after` for free, and every night profile carries
  `abort_on_bad_sense`, live at every condition.
- Silent-port retries: one retry after **90 s** (6 s retries are pure dice).
- A sweep mints its folder before opening the port, so each silent attempt
  leaves an EMPTY `sweep_*` folder (`sweep_20260807_0120*/0137*/0141*` — no
  pulse fired in any); the runner names them rather than deleting from
  `data/raw/`.

Meanwhile the 01:40 probe (A0/A1 reconnected) is the **control result for the
AGND test**: 660 mA achieved, coil actuated, and the glitches returned exactly
(σ 68.0 mA, R @200 ms 3.84 %). Fault confirmed upstream of the ADC pins, in
the harness / driver-board analog section.

## 2026-08-07 — the sense fault is NOT the SMA clips. It is the H7 analog front end

The 2026-08-05 fault reproduced exactly on the first sequential step
(`pulse_20260807_004530`: σ 65.4 mA, R @200 ms 4.00 %, verdict BAD, step
correctly blocked before any profile ran). Re-analysed with the corrected
metric, **the mechanical diagnosis in the 2026-08-05 entry is wrong.** Nothing
is wrong with the wire or the clips.

### What the capture actually shows

| | healthy 08-05 16:07 | tonight 08-07 00:45 |
|---|---|---|
| wire R (mean V / mean I) | 4.01 Ω | **3.88 Ω — unchanged** |
| idle current samples > 200 mA | 42 (0.2 %) | **4232 (21.3 %)** |
| glitch duration | 1 sample | 1 sample (median), 7 max |
| interval | 434 ms | **4.7 ms, CV 0.87 — Poisson, not periodic** |
| V during a current glitch | — | **0.609 V, implying 2.37 Ω** |

The last row is the whole diagnosis. When `sma_i` reads 257 mA, `sma_v` still
reads 0.54–0.61 V. A real 257 mA through a 4 Ω coil is 1.03 V. **The current is
not physically happening** — the glitch is in the measurement. A series contact
in the power loop cannot do this: it would raise R (lowering I) and move V and I
together. R is unchanged and the two channels glitch *independently* (1.62×
chance coincidence, so 84 % of current glitches have no voltage glitch at all).

Both `sma_v` and `sma_i` are read by the **H7's internal ADC, and nothing else
is** — the ADS1263 channels (laser, load) are clean in the same captures, and
the mechanical trace of this very capture is textbook. Independent single-sample
glitches on two channels sharing one ADC point at that shared path: the analog
ground return, AREF, or the analog supply. Not the SMA clips, not the coil.

Firmware is identical across healthy and faulted captures (`n_cycle=4`,
`aref=3.145`, `vdd=5.067`, 981 vs 940 Hz stream rate), so this is not a build
difference.

**The healthy captures already carry the same defect at 1/100 the rate** (42
events per 20 s on 08-05 at 16:07, versus 4232). It is progressive, which is
why it read as a step change appearing between 16:10 and 21:05.

### Two estimator artifacts that produced the wrong diagnosis

1. **"R climbed +0.3–0.5 Ω with every reseat" is not real.** The check computed
   `mean(V/I)`, and `E[V/I] ≈ R(1 + σ_I²/I²)` — a channel that only gets NOISIER
   reports a HIGHER R. At 20 % noise that is +4 %, at 46 % it is +20 %. By
   `mean(V)/mean(I)` the wire sat at 4.01–4.06 Ω healthy and 3.79–4.00 Ω
   faulted: **it never moved.** Fixed — `measure()` now blocks the channels and
   then divides. *(`analyze_raw.py` computes window R the same way; at the
   healthy 20 % idle noise the bias is ~4 % and roughly common-mode across
   cells, but it is worth a look before R is used quantitatively.)*
2. **"corr(V,I) fell 0.815 → 0.572" was measured over the whole record**, so it
   was dominated by the heat pulses. On the idle window corr is ≈ 0 in healthy
   AND faulted captures alike (−0.02 vs +0.14) — the noise is incoherent in both,
   which is itself evidence it is instrumentation rather than the wire.

Of the three pillars of the 08-05 write-up, only the σ rise survives, and it is
2.6× rather than 3.3×.

### Next session — do NOT reseat the SMA clips

Four reseats on 08-05 could not have helped, and the R rise that justified each
one was an artifact.

**CONFIRMED the same night by an open-circuit probe.** `pulse_20260807_010155`,
run with the **SMA disconnected**:

| | median I | sigma I | max I | glitched |
|---|---|---|---|---|
| healthy 08-05, coil attached | 124.7 mA | 25.0 mA | 241 mA | 0.7 % |
| 08-07 00:45, coil attached | 119.7 mA | 63.5 mA | 442 mA | 21.0 % |
| **08-07 01:01, DISCONNECTED** | **−4.7 mA** | **61.0 mA** | **+320.8 mA** | **28.4 %** |

The current channel reports excursions to **+320 mA with no circuit attached**,
and removing the coil changed the noise by 4 % (63.5 → 61.0 mA). The wire, the
clips and every contact in the SMA loop are exonerated outright. `sma_v` behaves
the same (sd 0.203 V disconnected vs 0.189 V attached). Reproduced immediately
in `pulse_20260807_011108` (σ 60.7 mA against 60.5).

**And it is confined to the H7's own ADC.** The ADS1263 channels in the SAME
captures are untouched — laser and load show a **0.00 % glitch rate** in all
three (healthy, faulted-attached, faulted-disconnected), and are quieter now
than on 08-05. So this is not the bench ground, not the EVM, not the supply
everything shares: it is specifically the Portenta's internal ADC path, which
reads `FB_PIN`/`A0` (SMA_P via the 10k/10k divider) and `ISENSE_PIN`/`A1`
(INA296A OUT) and nothing else. The remaining question is only *where* in that
path:

1. **Lift A0 and A1 off the driver board and jumper them to the Portenta's GND,
   then re-probe.** Both inputs then read a hard 0 V. Glitches that SURVIVE are
   inside the Portenta (its ADC, AREF, the analog supply); glitches that VANISH
   are upstream — the INA296A, the 10k/10k divider, or the two leads. That is
   the next split, and as cheap as the disconnect test was.
2. Then, in order: the ground return between the SMA driver board and the
   Portenta analog ground, AREF and its decoupling, the INA296A supply and its
   lead to A1, the FB divider lead. Wiggle each while streaming — the glitch
   rate should respond to the culprit. Independent glitches on two separate
   channels sharing one ADC is a shared-ground / reference signature.
3. Also worth trying: a different USB port. The ADS1263 staying clean argues
   against a bench-wide ground problem, but the Portenta's own analog ground
   rides its USB ground.
4. What changed physically around 2026-08-05 16:10–21:05 is still the best lead;
   it is now a question about the two analog inputs and their supply, not the
   clips.

`operator_sense_check.py` handles the disconnect test directly: below a 30 mA
median it reports R as `n/a` and judges on sigma alone. Without that guard the
`i > 0.02` divide guard kept only the GLITCH samples and reported a confident
**5.51 ohm** for an open circuit.

**Data taken in this state is not worthless, but the campaign's payload is.**
Laser and load ride a different ADC and are unaffected — stroke and force would
be fine. Self-sensing R is exactly what the 21 % corrupted current samples
destroy, and that is what the campaign exists to measure. Keep the gate closed.

## 2026-08-06 — the campaign runs SEQUENTIALLY; `operator_sense_check` rewritten

The 2026-08-05 campaign is now collected **one profile per invocation** rather
than as one unattended 9.2 h queue. Building the gate that makes that worthwhile
turned up two measurement bugs in `operator_sense_check.py`, so the numbers in
the *2026-08-05 night* entry below are restated here — **its conclusion stands,
its headline number was not measuring what it claimed.**

### `profiles/night_profiles_20260805/run_step.py` (new)

```
python profiles\night_profiles_20260805\run_step.py            # board — runs nothing
python profiles\night_profiles_20260805\run_step.py next       # run the next step
python profiles\night_profiles_20260805\run_step.py 7|n3       # a specific step
```

- **The order is unchanged and now lives in `run_order.txt`**, read by
  `run_night.ps1` too, so the two runners cannot drift. Both refuse to start
  unless every `n*.json` on disk is listed exactly once.
- **Every step is gated on the sense chain before it starts**: a fixed
  650 mA × 300 ms probe capture, graded by `operator_sense_check.verdict()`, and
  a non-HEALTHY verdict stops the step (`--force` overrides). This is the check
  the night did not have, and the probe being *identical at every step* makes
  its σ/R series a degradation trend across the campaign — R climbing step over
  step is the clip-contact signature.
- **Progress is derived from `steps/ledger.json`, not stored as a cursor.** One
  appended record per attempt (sweep folder, captures vs conditions, both sense
  verdicts, times). A step that did not finish `ok` is simply offered again;
  nothing has to be un-recorded by hand. The ledger is also the `runs` list a
  `CAMPAIGNS` entry needs.
- Captures are filed into `data/raw/campaigns/<key>/` (key discovered, not
  hardcoded, so it cannot go stale if collection starts a day late). The queue
  passes no `--campaign` and still lands in the inbox.
- **A bare invocation never drives the rig** — it prints the board. `next` is
  the word that fires pulses.

**What sequential mode changes about the data:** nothing inside a capture. A
profile is self-contained, so it runs byte-identical either way. What changes is
between profiles — the anchors `n0/n3/n9` bracket the whole sequence rather than
one night, so they measure drift over days, with power cycles and re-clipping
inside the bracket. The ledger timestamps are what keep that interpretable.

### The sense check was reading a heat pulse, not the noise floor

`operator_sense_check.py` measured "the first 30 s, the cold-start settle,
before any pulse". **The settle is not in the capture** — both
`operator_current_sweep.py` and `operator_pulse_capture.py` discard the settle
capture and save `lead(2 s) + run`. So the window was 2 s of idle plus a heat
pulse and its cool tail, and pulse amplitude rode straight into σ:

| same healthy sweep, one hour apart | old σ |
|---|---|
| `c00_level_250mA_h100ms` | 26.0 mA |
| `c20_level_650mA_h300ms` | 57.5 mA |

One rig, 2.2× apart, straddling the script's own 35 mA HEALTHY limit — **any
campaign probed at ≥ 450 mA would have been graded MARGINAL or BAD while
perfectly healthy.** Second bug: `hw_us` is a 32-bit µs counter that wraps every
71.6 min, and the script sorted the raw counter (`analyze_raw.py` has unwrapped
per src since 2026-07-31). On a capture straddling a wrap, "the first 30 s" was
an arbitrary mid-capture span — `c20` above is exactly such a capture.

**Rewritten to measure the idle samples of the whole capture**, unwrapped, with
every pulse and 3 s of its thermal recovery excised (the recovery is signal: R
falls ~3 % during a fire). ~180 000 samples over ~186 s instead of ~27 000 over
one pulse. The result is level-independent, which is what a threshold needs:

| | σ (mA) | R (Ω) | R @200 ms (%) |
|---|---|---|---|
| healthy 08-05, 6 captures, 200–950 mA × 100–500 ms | 24.9–25.1 | 4.18–4.24 | 1.80–2.05 |
| 08-05 night fault, 6 captures, 2 conditions | 68.3–70.5 | 4.37–4.69 | 3.07–3.74 |

σ varies by 0.2 mA across a 4.75× range of commanded current, so limits moved to
`SIGMA_OK/BAD 30/40` and `RPCT_OK/BAD 2.3/2.8`. Raising the excision from 3 s to
10 s moves σ by 0.1 mA — the check that no pulse tail is left in the window.

**The 08-05 diagnosis survives, with corrected magnitudes.** The fault was real
and about the size reported: **2.8× the noise floor** (not 3.3×, and not the
1.4× a naive level-matched comparison suggests — that estimate clips the very
distribution it is measuring). R rose 4.20 → 4.37–4.69 Ω and R @200 ms 1.9 →
3.2–3.7 %, both level-independent and both moved. What was wrong was the number
it was read off: it compared a 250 mA healthy capture against a 650 mA night
one, so it could not have distinguished a fault from a change of probe current.

### Also fixed

- **Pre-flight step 6's command could not work**: `--i-ma` is not a flag (it is
  `--ma`), and a pulse folder holds `h7.csv`, not `cNN_level_*.csv`, so a bare
  `operator_sense_check.py` skipped it silently and graded a *stale sweep*. The
  check now searches both capture shapes and recurses into `campaigns/`, which
  the old one-level glob could not see. The README now names the file explicitly.
- `verdict()` / `explain()` are importable, so the runner gates on exactly the
  thresholds the script prints.

## 2026-08-06 — `data/raw/` grouped by campaign; generated capture INDEX

20 `sweep_*` folders named by timestamp and nothing else, 3.8 GB, and no way to
tell which wire a run used without opening `meta.json` — which does not record
the wire at all. Reorganized ahead of the next collection round.

**Cold length is the campaign axis**, not the date: it is the same Dynalloy
stock throughout, cut/stretched to a different cold length per campaign, and
that changes the response more than anything else the protocol varies.

```
data/raw/
  INDEX.md         GENERATED — which folder holds what
  campaigns/
    20260730_dynalloy_15mm_cool15s/   7 sweeps, 1.6 GB   (cooling issue)
    20260731_dynalloy_15mm_cool30s/   5 sweeps, 1.1 GB   -> heat_time_map_20260731_all.csv
    20260804_dynalloy_4mm/            1 sweep,  18 MB
    20260805_dynalloy_10mm/           2 sweeps, 842 MB   -> heat_time_map_20260805_dynalloy_all.csv
  writeups/ aborted/ troubleshoot/ logs/
```

2026-07-30 and 07-31 are the SAME 15 mm wire; 07-30 ran a 15 s cool and hit the
cooling issue, so it is a different protocol and a separate campaign.

**Folder names are unchanged, and a sweep is still identified by its BARE NAME
everywhere** — the `sweep` column of the merged table, `CAMPAIGNS`, `--sweep` on
the plotters. `analyze_raw.resolve_sweep()` maps a name to wherever it is filed,
so refiling a folder rewrites no table and breaks no command. Both merged tables
regenerate byte-identical after the move; only the two `_meta.json` files
changed, and only because the `wire` descriptions were corrected to name the
cold length.

New/changed:

- **`analysis/make_index.py`** (new) — generates `data/raw/INDEX.md` from the
  files: grid off the capture filenames, protocol off `meta.json`, campaign off
  the parent folder, registration off `CAMPAIGNS`. `--check` exits 1 when stale.
  It flags **unfiled** runs (loose in `data/raw/`, wire unrecorded),
  **unregistered** folders (in no campaign, so in no table), and **old naming**.
- **`operator_current_sweep.py --campaign KEY`** — a run files itself into
  `data/raw/campaigns/KEY/` at capture time, so the folder names its wire from
  the moment it is written. Without it a run still lands loose in `data/raw/`,
  which is now an INBOX; the index lists it as unfiled. NOT bench-verified —
  `pyserial` was unavailable on the machine this was written on.
- **`analyze_raw.capture_dirs()` / `resolve_sweep()`** — name → path, walking
  the grouping folders but never descending into a capture folder.
- **`plot_drive_trajectory.all_sweeps()`** replaces the old
  `sorted(glob("sweep_*"))[-1]` for "latest". That sort was lexicographic, so
  `sweep_full_150-950mA` beat every `sweep_<stamp>` ('f' > '2') and a bare run
  had been plotting that folder rather than the newest capture. Now sorts on the
  parsed stamp, falling back to mtime for unstamped names, and skips
  `aborted/` + `writeups/` so `--all` stops emitting a failure line per folder.
- **`lib_analysis.latest_session()`** searches one level down as well. The
  `troubleshoot/` move had already broken it: `data/raw/console_*` matched
  nothing, so the notebook's session auto-pick reported "no console_* session
  found" for sessions that were merely filed.

**`data/derived/` mirrors the same grouping**, sharing the `dir` name so a
campaign's captures and its analysed results cannot drift apart. This matters
most for the five outputs whose FILENAME carries no provenance —
`trajectory_*.png` and the four interactive HTMLs are all 15 mm-wire results and
used to sit flat next to 10 mm-wire results, indistinguishable.
`plot_drive_trajectory.py` reads a sweep's parent folder and writes beside it;
a merge that SPANS campaigns lands at the `data/derived` root rather than being
filed under one campaign it only half belongs to.

The seven `console_*`/`noise_*`/`pulse_*` folders that had been staged for
deletion were **restored and filed under `troubleshoot/`** instead — 46 files,
now renames rather than deletions, nothing lost. `troubleshoot/` holds 19
investigations.

**`energy_table.py`'s July pin is unchanged and now visible.** It hardcoded
`heat_time_map_20260731_all.csv`; it now resolves the same table through
`CAMPAIGNS["20260731"]`, so `plot_energy` / `plot_selfsensing` /
`plot_transition` / `plot_r_bias` still show 15 mm-wire results no matter which
campaign was analysed last. That is a real limitation, documented in
`data/derived/README.md`, and the one place a `--campaign` argument would go.

**Known gap, unchanged by this:** the seven 2026-07-30 folders (1.6 GB) use the
older `level_850mA_h400ms.csv` naming, and every pipeline glob matches
`c*_level_*`. They are on disk and invisible to every stage. `INDEX.md` reports
them under "Not readable by the pipeline" rather than letting them look analysed.

## 2026-08-06 — `plot_drive_trajectory.py` merges sweep folders; 08-05 envelope figures

`--sweep A B ...` now reads several capture folders as ONE grid. The 2026-08-05
campaign was split by the bench: `sweep_20260805_105318` is the 250–950 mA ×
100–400 ms block, `sweep_20260805_154528` is the **extremes** — a 200 mA row and
a 500 ms column. Plotted separately neither shows the operating envelope; the
200 mA row is one current, so its figure was a single trace per channel and the
`--by heat` transpose was standing in for a current ramp it did not have.

Merged, the grid is 200–950 mA × 100–500 ms and every pulse length gets 8–9
current traces. Written:

```
data/derived/drive_sweep_20260805_105318+20260805_154528_{100,200,300,400,500}ms.png
```

What the merge does and does not do:

- **Union over CELLS, never over repeats of one cell.** Two folders holding the
  same `(current, pulse length)` are different sessions on a wire that has since
  cycled; averaging them would blend two wire states. The newer folder wins and
  the drop is **printed**, not silent. (No overlap in the 08-05 pair.)
- Each cell remembers its source folder, and the subtitle names every folder
  with how many series came from each. Per-series provenance prints to stdout.
- Cross-session merging is sound for these channels because each is referenced
  to **its own cycle's pre-fire baseline** — ΔR/R₀ to that cycle's R₀, stroke and
  force to that cycle's pre-fire mean — so drift between sessions cancels rather
  than appearing as an offset between traces. Absolute R₀ still varies; the
  right-hand ohm axis prints the median over the merged set, read it as a scale.
- Nothing else changed: no filtering, no selection, rails still dotted.

Superseded and removed: the five single-folder 08-05 figures
(`drive_sweep_20260805_105318_{100..400}ms.png`,
`drive_sweep_20260805_154528_{200mA,500ms}.png`) — every cell they showed is in
the merged set with the missing end of the current axis filled in. Regenerate
either form at any time; the script is deterministic.

Reading the merged 400 ms figure: stroke is monotone in current out to 4.8 mm at
950 mA with no sign of saturating, while the **load cell rails at 0/5 V from
850 mA** (750 mA at 500 ms) — those force traces are dotted and their flat tops
are the amplifier, not the wire. Force headroom, not wire capability, is what
currently bounds the top of the envelope.

## 2026-08-03 — module reorganized into four buckets

Main code, one-off diagnostics, raw data and derived data are now separated. The
module root had grown to 15 flat `.py` files with three closed investigations
carrying the same `operator_` prefix as the primary entry point, and `data/` had
become a code directory holding the entire 12-script analysis pipeline interleaved
with 38 capture folders and 21 loose result files.

| bucket | holds |
|---|---|
| module root | `operator_console.py` + 4 standing operator tools + 7 `lib_*` |
| `analysis/` | the standing raw → table → charts pipeline (11 scripts) |
| `diagnostics/` | closed one-off investigations + the superseded 07-30 merge script |
| `data/raw/` | 38 capture folders — what the rig wrote, never hand-edited |
| `data/derived/` | merged tables + figures — what the pipeline computed |

**Capture folder names did not change**, and every script reaches them by name
joined onto a path constant, so `analyze_raw.py`'s `RUNS` list and
`plot_trajectory.py`'s `SRC_MAP` were untouched. The single `BASE` in each script
became `RAW` and/or `DERIVED` resolved off `__file__`, so the pipeline runs from any
CWD. **Verified byte-identical**: stage 1 regenerates `heat_time_map_20260731_all.csv`
and all five per-sweep `cycles.csv` with zero content change (git reports `R100`),
and the superseded `make_heat_time_map_clean.py` reproduces its 07-30 outputs.

Three things that had to move with it, easy to miss:

- **`lib_analysis.latest_session()`** globs `console_*` to auto-pick a session when
  the notebook is run without `--session`. It now points at `data/raw/`; left alone
  it would have silently reported "no session found".
- **Five raw writers** decide where new captures land: `config.yaml` `output_dir`,
  `lib_config.RunConfig.output_dir`, `operator_current_sweep.py`,
  `operator_pulse_capture.py`, and the two diagnostics' `--outdir`. All now resolve
  to `data/raw/`.
- **`operator_sweep_adcavg.py`** reached the firmware project via
  `Path(__file__).parents[1]`, which pointed at the repo root only while the script
  sat at the module root. Moving it into `diagnostics/` broke that by one level; it
  now derives `FW` from the module dir explicitly.

`data/derived/` is committed in full, HTML figures included, so analysed results
travel with a clone. The module's entire ignore set stays machine-local: `.claude/`,
`__pycache__/`, `zaber_config.json`.

## 2026-08-05 — `plot_drive_trajectory.py` promoted to THE standard sweep figure

The 4-channel × 2-time-scale drive figure is now documented in the README as the
default way to look at any sweep (its own section, plus the pipeline table, the
command list, and the tree). It already read raw captures directly and
discovered the grid off capture filenames, so it needs no per-cycle table and no
code edit per run — the gap was that it was undocumented and per-sweep manual.

- **`--all`** added: every `sweep_*` folder under `data/raw/` in one command. An
  overnight campaign lands as a dozen folders, so the per-sweep invocation was
  the thing standing between "standard step" and "bookkeeping". A folder with no
  captures is skipped with a reason and the batch continues; the exit code still
  reports that not everything worked.
- **One-pulse conditions now plot.** `condition_trace()` looped
  `range(2, n_cyc + 1)` to hold out the bootstrap cycle, and bailed on
  `n_cyc < 2`. Tonight's randomized protocol fires each condition **exactly
  once**, so that range is empty and every one of the 962 overnight captures
  would have plotted as "no usable cycles" — silently, for the whole campaign.
  A single-cycle condition now uses its one pulse, and the subtitle says
  `1 pulse per condition (randomized protocol)` rather than claiming a median it
  did not take. Verified by forcing the single-cycle path over real captures:
  6-cycle traces unchanged (n=5, peak 1.108 mm), 1-cycle path returns a trace
  (n=1, peak 1.129 mm) where it previously returned `None`.

The two `sweep_20260805_154528` figures already existed and were already correct
— including the red `dotted = amp railed at 0/5 V — 750, 850, 950 mA` callout on
the load-cell panel. They were regenerated to confirm identical content.

## 2026-08-05 — the 08-05 sweeps analysed; stage 1 is now campaign-aware

Ran the standing pipeline on the two Dynalloy sweeps, which had only ever been
plotted by `plot_drive_trajectory.py` — the mandatory
`operator_sweep_report.py` step had been skipped on both, so neither had a
`report.txt`, a `cycles.csv`, or an envelope figure.

**`analyze_raw.py` now keys on CAMPAIGNS, not one global `RUNS`/`MERGED`.**
`MERGED` was a single hardcoded filename written unconditionally, so
`analyze_raw.py sweep_20260805_154528` would have **rewritten the committed
07-31 table with only that folder's rows**. Each wire now gets its own merged
table (guideline §7: a different coil is the largest protocol change there is),
and a campaign contributing no rows is left untouched rather than truncated.
`run_type` also declares expected cycles per condition, so tonight's
one-pulse-per-condition captures self-check correctly (`random`: 1).
Verified: the 07-31 table and all five of its `cycles.csv` regenerate
**byte-identical**.

**Two real bugs in `plot_envelope.py`, both found by pointing it at this data:**

- **`HEATS = [100, 200, 300, 400]` was hardcoded and `aggregate()` looped it**,
  so every cycle at any other heat time was silently discarded. The 08-05
  campaign's whole 500 ms row — 48 cycles, 8 of 44 conditions — vanished while
  the script printed *"none excluded"*, in direct violation of the module's NO
  DATA SELECTION rule. The heat list is now derived from the table and the
  ramp is built to fit it; sampling the validated anchors at 4 points
  reproduces the four validated colours **exactly**, so existing figures are
  unchanged. A 5+-step ramp stays single-hue and monotone in lightness by
  construction but has **not** been through `validate_palette.py`.
- **Saturation was a single flag from `clipped | railed`, used on both charts.**
  `railed` (laser out of window) bounds `dx`; `clipped` (load cell at 5 V)
  bounds `dF`. This campaign railed the laser **0** times and clipped force
  **28** times, so the entire top of the stroke chart was drawn hollow and
  dashed as *"lower bounds, not measurements"* when every one of those
  displacements is exact. Now per-channel (`saturated_dx` / `saturated_dF`,
  added alongside `saturated` — add, don't remove), and each chart names its
  own rail and count in its subtitle.

`plot_energy.py` and `plot_selfsensing.py` share the latent version of the first
bug via `ps.HEATS`, but they are pinned to the 07-31 `TABLE` and their prose
hardcodes "150–950 mA × 100–400 ms", so porting them to a second campaign is
authorship, not plumbing. They now call `ps.check_heats()`, which turns the
silent drop into a loud stderr warning if that pin ever moves.

**What the 08-05 Dynalloy campaign shows** (44 conditions, 264 cycles):
stroke is steeply super-linear in current and nowhere near saturating — 950 mA
× 500 ms reaches 7.6 mm against a ~10 mm laser window, and nothing railed.
Force is the binding channel, not displacement: it clips from 750 mA upward on
the long pulses, and because the cold baseline sat at 3.23 V the usable *rise*
tops out near 190 mN, far below the 490 mN full scale. Re-zeroing the load cell
cold is what makes the top of the 500 ms row measurable.

**Platform note.** `csv.writer` defaults to CRLF while the committed tables are
LF (written on the Windows rig, normalised on commit), so regenerating on macOS
rewrote all 261 lines with identical values. Stage 1 now pins `lineterminator`
to LF and is byte-reproducible on either host. The plotly HTML pages and the
matplotlib PNGs still carry host-specific churn (a per-render div UUID, last-ULP
float differences, and macOS font fallback for weights `medium`/`semibold`), so
**the 07-31 figures were left at their committed versions** — regenerate them on
the rig host to pick up the per-channel rail fix.

## 2026-08-05 — overnight campaign runner + the shuffle protocol was not shuffling

Prepared the 2026-08-05/06 overnight collection (13 profiles, 962 pulses,
~9.2 h). Campaign and its rationale:
`profiles/night_profiles_20260805/README.md`.

**New: `operator_profile_queue.py`** (module root, `operator_` = run directly).
`operator_current_sweep.py` runs one profile per launch and exits, so a
multi-profile campaign needed someone awake at 03:00. The queue validates every
profile before the first pulse fires, runs the report after each, honours
`--deadline HH:MM`, and writes `queue_manifest.json` mapping pre-registered role
→ capture folder. Two behaviours worth knowing:

- **It judges a profile on captures written, not exit code.** `operator_current_sweep.py`
  returns 0 on nearly every failure path — dead port, hub fault, sense abort —
  after writing `summary.csv`. A first cut of the queue reported "2 ok" for two
  profiles that had captured *nothing*. It now scrapes the sweep's own stop
  reason and counts capture CSVs.
- **Circuit breaker:** two consecutive zero-capture profiles abandon the queue.
  A rig that has dropped off USB is not fixed by trying the next profile.

**The confound the shuffle blocks existed to remove was still present.** The
first draft of the campaign used `cycles: 1` + `settle_s: 40`. That fires two
pulses at the *same* `(i_ma, heat_ms)` 30 s apart, then waits ~70 s before the
next condition — so the recurrent window sees exactly **two thermal-history
classes** (cold start / 30 s after an identical pulse), both a deterministic
function of the command, and shuffling the condition *order* changes nothing.
`DATA_COLLECTION_GUIDELINE.md` §9.2 requires the opposite: *consecutive pulses
must differ in condition*. Confirmed on 08-05 data — same cell, cold-start vs
30 s later: 250×400 → 74.7 vs 52.1 µm, 250×300 → 53.6 vs 43.9.

The shuffle blocks now use **`cycles: 0`, one pulse per condition**, with
`settle_s: 2` so the block is one randomized excitation train. Two changes to
`operator_current_sweep.py` were required:

- **`n_cyc == 1` treats the single pulse as the measurement.** Otherwise `per_v`
  (non-bootstrap pulses) is empty and the code `continue`s — **skipping the
  sense check**, silently disabling `--abort-on-bad-sense` for the entire
  campaign. That guard is the one thing standing between an unattended night and
  a repeat of the 2026-07-28 corrupted-sense sweep. Safe because
  first-after-arm pulses measure 97–102 % of command on this coil (the
  "bootstrap is a ramp" warning describes pre-seed firmware on the long coil),
  and `prepare_pulse_runs.py` explicitly *keeps* `bootstrap == 1` rows.
- **`meta.json` now carries `profile_name` / `profile_seed` / `protocol` /
  `i_low_mA`** per capture, so a randomized campaign is reproducible from the
  captures alone (guideline §9.3 seed logging, §7 protocol identifiability).

**Profiles are drawn to the §9.1 envelope measured on the current coil** — a cell
is admitted only if median |stroke| ≥ max(50 µm, 3σ) and the drive is
trustworthy. 36 of 44 cells admitted; the whole 200 mA column, 250×100/200/300
and 350×100 rejected, which sets a current-dependent minimum heat time
(250 mA → ≥400 ms, 350 → ≥200, ≥450 → ≥100). Draws are continuous
Latin-hypercube inside it, not grid nodes; `cool_s` is randomized per condition
(12–45 s, floored by pulse energy; n4 uses a disjoint 5–11 s for the OOD
short-cool test, current-capped at 650 mA). Nothing railed — the laser window is
healthy across the full ~10 mm.

**Two open downstream items:** every night pulse carries `bootstrap = 1`
(semantically correct, one pulse per capture, but no longer discriminating — use
`cc_pct`), and `prepare_pulse_runs.py`'s per-cell median/σ filter becomes a
no-op on this data since it reads `bootstrap == 0` rows. Mitigated by applying
the envelope at draw time; the filter should be revisited before training on
randomized campaigns.

**Not yet bench-verified:** the `cycles: 0` path has been dry-run and
compile-checked but never driven against the rig. Fire `n0_anchor_start` and one
short slice of `n1a` attended before committing to the night.

## 2026-08-05 — Dynalloy short-wire campaign: 44 cells + a sweep-agnostic plotter

New coil fitted (0.08 in, 10 mm solid, cold-stretched to 40 mm). Three captures,
all committed under `data/raw/`:

| capture | grid | profile |
|---|---|---|
| `sweep_20260805_105318` | 8 levels (250–950 mA) × 100/200/300/400 ms, 32 cells | `heat_time_map_30s_shortwire` |
| `sweep_20260805_154528` | 200 mA × 100–400 ms **+** 250–950 mA × 500 ms, 12 cells | `extremes_200mA_500ms_shortwire` |
| `sweep_20260804_213705` | aborted on condition 1 — stale `r_min` guard, see below | — |

**The cold-resistance guard has to track the wire.** `r_min_ohm` is ~45 % of the
wire's cold R, not a constant. The 1.8 Ω coil's idle bias alone draws
`v_idle/R = 0.5/1.8 = 278 mA`, which the long wire's 2.0 guard read as
"impossible" and used to kill `sweep_20260804_213705` on its first condition.
Default is now 1.6 (the Dynalloy coil at ~3.5 Ω estimated / **4.01 Ω measured**),
and the abort message now names a stale guard as a cause alongside a corrupted
sense. Prefer `r_min_ohm` in the profile so it rides with the data.

**200 mA is the actuation floor, and it is a real floor.** The parent map stopped
at 250 mA because `v_idle` at DAC code 0 sets an idle current of `v_idle/R`; that
floor was computed against the *estimated* 3.5 Ω, but the measured 4.01 Ω puts it
at 125 mA, so 200 mA clears it by 1.6×. The row runs, and the answer is that
200 mA barely actuates: 20–40 µm of stroke and 0.1–0.3 gf, with the cool phase
dominated by drift rather than any SMA relaxation. Recorded as a result, not a
failed cell.

**500 ms has not saturated the stroke.** 950 mA × 500 ms delivers ~1.68 J — ~35 %
past the ~1.24 J cap seen on the long wire — and still buys travel: 7.6 mm
against 5.3 mm at 400 ms. The laser survived it with ~2.2 mm to spare (min
1.11 V of a 0–5 V window).

**The load cell is preload-limited, not range-limited.** Cold baseline sat at
3.23 V of 5 V, leaving only 1.77 V ≈ 18 gf of headroom against the cell's own
50.3 gf full scale. Force rails at 850/950 mA × 400 ms and 750/850/950 mA ×
500 ms. Two things make this worse than ordinary clipping: the amp does **not**
recover cleanly — it latches ~0.2 V *below* its own baseline for ~2 s after
coming off the rail, so the whole cycle's force is lost, not just the clipped
span. `heat_time_map_30s_shortwire.json` had already specified a cold baseline of
1.8–2.0 V; it was not set there. **Deliberately not corrected mid-campaign:**
preload changes the wire's mechanical operating point, so re-setting it would
break comparability with the 44 cells already captured. Re-set it as its own
step, record it, and re-run the affected cells. Resistance, power and stroke are
valid on every railed cell.

**`analysis/plot_drive_trajectory.py`** (new) — 4 channels × 2 time scales, the
same conventions as `plot_trajectory.py` (Blues ordinal ramp + colorbar, median
over the non-bootstrap repeats, no temporal filtering) with three differences:
it discovers a sweep's grid off the capture filenames instead of a hardcoded
`SRC_MAP`, so a fresh run plots with no code edit; mechanical channels are in
**mm and grams-force** rather than µm/mN; and it detects amplifier rails on the
raw 0–5 V channels, drawing a railed series **dotted with an in-panel note**
rather than dropping it — the plot-side equivalent of the pipeline's `railed`
column. `--by heat` transposes the colour axis onto pulse length for
single-current rows; `--by auto` (default) emits by-level figures and falls back
to by-heat only for cells those cannot show.

## 2026-08-05 night — CAMPAIGN NOT RUN. SMA sense chain degraded during pre-flight

**The overnight campaign did not start.** Four attempts between 21:05 and 21:51
all aborted on the sense guard; the rig was powered off at ~22:00 and the
campaign deferred. **Nothing is wrong with the campaign, the profiles, or the
guard — the rig's SMA sense chain is faulted, and it was faulted before the
first pulse.**

### The measurement

> **Restated 2026-08-06 — see the entry above.** The σ column below is not a
> noise floor: the window it was read from contains a heat pulse, so it also
> tracks the commanded current, and the healthy rows here were taken at 250 mA
> against the night's 650 mA. The fault is real and the conclusions in this
> entry stand; on the corrected metric it is 2.8× (69 vs 25 mA), R 4.20 →
> 4.37–4.69 Ω, R @200 ms 1.9 → 3.2–3.7 %.

Quiet-window numbers (cold-start settle, wire at the 0.5 V idle bias), from
`operator_sense_check.py`:

| capture | sma_i σ | R | R @200 ms |
|---|---|---|---|
| `sweep_20260804_213705` | 26.5 mA | 1.74 Ω | 1.65 % |
| `sweep_20260805_105318` (10:53) | 26.0 mA | 4.22 Ω | 1.85 % |
| `sweep_20260805_154528` (15:45) | 25.2 mA | 4.23 Ω | 2.03 % |
| `sweep_20260805_210507` (21:05) | 85.7 mA | 4.37 Ω | 4.01 % |
| `sweep_20260805_211626` (21:16, post power-cycle) | 86.6 mA | 4.47 Ω | 3.89 % |
| `sweep_20260805_212503` (21:25, post reseat) | 87.6 mA | 4.45 Ω | 3.80 % |
| `sweep_20260805_213329` (21:33, post reseat) | 86.6 mA | 4.68 Ω | 4.15 % |
| `sweep_20260805_214317` (21:43, c00 / c01) | 85.9 / 77.6 mA | 4.53 / 4.51 Ω | 3.71 / 3.56 % |

**Stable for two days across two wires, then a 3.3× step while the rig sat idle
between 16:10 and 21:05.** The only event in that window is the campaign
pre-flight, whose step 4 is *reseat the SMA clips*.

### What it is, and what it is not

- **Confined to the SMA sense chain.** On the same quiet window the ADS1263 side
  is unchanged: laser 44.6 → 40.3 mV, load 67.6 → 63.6 mV. Only `sma_v`
  (194.8 → 325.1 mV) and `sma_i` (54.0 → 96.5 mA) moved, both ~1.75× — they
  share the H7's internal ADC, and nothing else does. *(An earlier read of this
  said all channels had doubled; that was measured over the whole record, so the
  laser and load figures were counting the pulse motion itself. On the quiet
  window they are clean.)*
- **It does not cancel in R = V/I.** The added noise is broadband above 100 Hz
  (77.6 % of `sma_i` variance vs 13.0 % in the afternoon capture) and
  *incoherent* between V and I — corr(V,I) fell 0.815 → 0.572 on the same
  condition. Coherent noise divides out; this does not. R noise at the 200 ms
  window the analysis actually uses roughly doubled, 1.9 % → ~3.9 %.
- **Mechanical, and progressive.** A full USB + EVM power cycle changed nothing
  (86.6 mA after). Three clip reseats changed nothing *except* that R climbed
  each time — 4.22 → 4.37 → 4.47 → 4.68 Ω, ~+11 % of added series resistance —
  so the interface appears to be degrading as it is disturbed. Added series R
  plus incoherent broadband noise appearing the moment the clips were touched is
  a micro-contact, not an electrical or firmware state.
- **Secondary symptom, same night:** COM8 intermittently returned
  `not streaming (0 bytes in 2s)` with `force pull: drained 0.0 kB` (so not the
  known full-CDC-buffer case), twice, recovering on the next launch.

### The guard is NOT too tight — do not relax `r_min`

`r_min_ohm 1.6` sets the limit at `0.5/1.6` = 312 mA. At the healthy σ ≈ 26 mA
that sits **7.5σ** above the ~121 mA idle mean and never fires. At tonight's
σ ≈ 86 mA it sits **~2σ** out, where ~1 % of samples land by construction. The
five fractions measured tonight — 1.24, 1.12, 1.11, 0.99, 1.07 % — are one
distribution straddling the 1 % `max_frac`, which is why one condition "passed".
Passing `r_min ≈ 1.4` would clear the guard and record a degrading contact into
the one channel the campaign exists to measure.

**Had the night been launched in this state it would have looked like it worked.**
`abort_on_bad_sense: true` stops a sweep at its first failing condition, and
~1 condition in 10 passes, so each profile would have died after its first or
second capture — but each *would* write captures, so the queue's
two-consecutive-zero-capture circuit breaker never trips. Expected yield: **~20
captures of 804**, over a full 9 h, reported as a mix of `ok` and `partial`.
The one profile that completed tonight (`sweep_20260805_214317`) shows exactly
this: `1 ok, 0 not ok, 2 captures` with the sense fault on one of its two
conditions.

### New: `operator_sense_check.py` (module root, `operator_` = run directly)

The guard reports a *fraction of impossible samples*, which is a threshold
crossing on a noisy quantity — it fires on a wider noise floor without saying
so, and its message names two causes (corrupted sense / stale `r_min`) that were
both wrong here. Nothing printed the number that actually diagnoses the rig.
This reads it from any capture in seconds: `sma_i` σ, R, R at the 200 ms
analysis window, and a verdict against the measured 08-04/08-05 baseline
(~26 mA, ~1.7–2.0 %). Validated on a known-good capture (26.0 mA → HEALTHY) and
tonight's (86.6 mA → BAD). `corr(V,I)` is printed but is **informational only** —
its absolute value swings with pulse level (+0.09 healthy at 250 mA, +0.45
faulted at 650 mA), so the verdict uses σ and R @200 ms alone.

### Next session

1. **Localize with a meter before touching anything.** Wire end-to-end should
   read ~4.2 Ω; the extra ~0.3–0.5 Ω is in one junction. Measure each
   clip-to-wire contact and the lead run to the driver. Four blind
   power-cycle/reseat attempts did not find it and R rose each time.
2. **Verify with `operator_sense_check.py`, not an 8-minute anchor** — target
   σ ≈ 25 mA and R @200 ms ≤ 2 % *(2026-08-06: on the rewritten metric; the
   verdict is now built into the runner, so this is the check you get for free
   before each step)*.
3. Then run the campaign unchanged, **sequentially**:
   `python profiles\night_profiles_20260805\run_step.py next` — the profiles and
   the order are the same; see the 2026-08-06 entry.

### Also settled tonight (survives the deferral)

- **Run order changed and is no longer sorted order** — see *Run order* in
  `profiles/night_profiles_20260805/README.md`. Sorted order ran both TRAINABLE
  halves back-to-back before any test block, put the "mid" anchor at 65 % of
  wall clock, and ran n4a/n4b adjacent. New order runs one half of each pool
  first, so the campaign is complete-in-miniature at the midpoint;
  `n4a_shortcool_test` is second as the attended head. Driven by
  `run_night.ps1`, which passes the queue an explicit ordered list — no profile
  was renamed, so `gen_night_profiles.py` remains the source of truth.
- **`--deadline` does not protect the end anchor.** The queue skips every
  profile that would start after it and iterates in order, so a deadline deletes
  `n9_anchor_end` along with the rest of the tail. `run_night.ps1` passes none.
- **Campaign disk is ~2.6 GB, not the 19 GB the campaign README claimed** — that
  figure multiplied 962 *pulses* by the size of a *condition* capture, but a
  shuffle-block condition is one ~30 s pulse. Measured 92 kB/s of CSV over ~7.9 h
  recorded. The "committing this sextuples the repo" note was corrected with it;
  the module's commit-the-captures convention holds.
- **Report cost is not a scheduling factor:** `operator_sweep_report.py` runs at
  ~0.13 s/MB (30.7 s on a 230 MB sweep), so all 13 reports add ~10 min to the
  9.2 h queue.

### Data on disk from tonight

Eight `sweep_20260805_21*` folders and six `queue_20260805_21*` folders, ~108 MB
total. `_210933`, `_211007` and `_214302` are empty (the COM8 dropouts); the
rest hold one or two captures each. Only `_214317` c00 passed the guard, at
0.99 % against a 1.00 % limit. **None of it is campaign data** — do not add
these to `analyze_raw.py`'s CAMPAIGNS. They are kept as the diagnostic record
behind this entry, and they are what `operator_sense_check.py` was validated
against.

## Analysis findings

- **Delivered energy E = ∫P dt is the state variable, not power** (2026-08-02,
  n=176 usable cycles, 250–950 mA × 100–400 ms). Stroke and force collapse onto
  one curve against E (R² 0.992) and do not against power (R² 0.707). R is
  linear in E (−0.379 Ω/J, R² 0.928) but a weaker, non-linear predictor of
  stroke (residual sd 458 µm vs energy's 336 µm). Evidence for the RNN's P·t
  input; see `data/derived/energy_collapse.html` + `data/derived/self_sensing.html`.
- **Open caveat:** `r_base_ohm` is sampled at the ~107 mA idle bias while
  `r_hot_ohm` is at drive current, so ΔR between them mixes two bias points —
  it shows as a non-physical −4 % intercept at E→0. Slopes are unaffected.
  Absolute R is the better axis and the better predictor; prefer it over ΔR/R₀.
- **The high-energy end is instrument-limited.** 950 mA · 400 ms clips the load
  cell and rails the laser on every cycle, so nothing above ~1.2 J is measured.
  Those cycles are drawn hollow, never dropped.

### BLOCKER — thermal hysteresis is not measurable from this data (2026-08-02)

At pulse end the drive drops from 250–950 mA to the ~107 mA idle bias, and
**R = V/I jumps +9 to +14 % within ~50 ms** (e.g. 850 mA/400 ms: 4.058 → 4.534 Ω).
That cannot be physical — cooling constants are 5–20 s and the stroke is still at
maximum at that instant. It is consistent with a voltage offset,
`R_meas = R_true + V_off/I`, which inflates R more at low current.

`V_off` estimates from two independent routes agree in magnitude but not tightly:
**33 ± 19 mV** from the pulse-end step (temperature is continuous there, so the
step solves for it) and **23 mV, R² 0.57** from R at pulse *onset* vs 1/I across
the 8 levels (the wire is still at ambient in the first 4–20 ms). The implied
correction at the idle bias is 0.21–0.30 Ω ± ~0.15 Ω, against a total heating
excursion of only **0.64 Ω**.

**Consequence:** the heating and cooling branches cannot be put on a common R
axis to better than ~25–30 % of the signal, so the classic stroke-vs-temperature
hysteresis loop cannot be extracted. A naive loop looks huge — cool-vs-heat
stroke gap of 81–83 % of peak, suspiciously constant across very different
conditions — but that is the offset, not the wire. **Do not plot it as
hysteresis.** (The mechanical force-vs-stroke loop is clean but nearly
single-valued, 3–6 % of peak, so it is not a substitute.)

**To unblock:** sense R at a CONSTANT current on both branches — hold a fixed
sense bias through the cool phase, or use `Firmware_SMAConstantCurrent_PIO`
(closed-loop CC, streams `R_est` as src=7). Separately, a room-temperature
current sweep at fixed temperature would pin `V_off` properly and retroactively
correct every existing capture.

## Entry points

- **`operator_current_sweep.py`** — condition sweeps (current × pulse length),
  CLI flags or a **JSON test profile** (`--profile profiles/*.json`; the
  profile WINS over flags for what it specifies and is copied into the output
  folder). Two profile forms: `levels_ma × heat_ms` grid, or explicit
  `conditions: [{i_ma, heat_ms, cool_s?, cycles?}]` executed in order with
  repeats allowed — the shape the RNN collector will generate. **ALWAYS
  `--dry-run` first to check a profile** — it prints the plan without opening
  the port; a profile carries its own `port`, so a "syntax check" without
  `--dry-run` WILL drive the rig (six 150 mA pulses fired this way
  2026-07-30).
- **`operator_sweep_report.py <folder>`** — standard post-sweep analysis:
  re-analyses every capture from raw through the clock-aligned path and emits
  `report.txt` (health verdicts: laser-rail, cc-track, sense, load-clip,
  base-jump, missing pulses), `summary_report.csv` (per-pulse flat table),
  `fig_envelope.png`, and `--timeline` strips. Run it the moment a sweep ends;
  it is the reference over any in-run summary.csv. Validated against the
  compromised `sweep_20260730_031337` (flags all 17 bad conditions, keeps the
  2 clean ones; near-window-edge baselines are NOT flagged).

- **`operator_console.py`** — the primary entry point. One window controls the
  stage, LCR, and SMA from a continuously-logging session (live plots, startup
  full-system check, mid-run staleness monitor, always-available `DISARM`).
  `--headless` runs the same `RecordingCore` with no GUI for scripted runs.
  Built on `pyqtgraph.Qt` (binding-agnostic: PyQt5/6 or PySide2/6).
- `sma_recorder.py` — the older interactive OPEN→SHORT→RAW recorder (still
  present; `session.py`/`operator_io.py` back it). The console supersedes it.
- `run_experiment.py` — **RETIRED** (stub): it built firmware commands inline
  and never `arm`ed, which the rebuilt firmware rejects. Use `--headless`.

## Console controls (GUI)

- **Adaptive-FPS camera + live preview (2026-07-06).** A `CameraWorker` drives
  the 12MP USB3 camera (index 1) at a **fixed resolution, variable frame rate**:
  fast (`fps_fast`) while the SMA is moving, a slow **heartbeat** (`fps_heartbeat`)
  once settled. "Moving" = net **median-filtered** laser displacement ≥
  `change_threshold_mm` (robust to sensor noise/jumps); after `stop_dwell_s` with
  no full-mm change it drops to heartbeat. Each heat/idle event forces fast for
  `transient_guarantee_s`. Camera runs at native rate (grab-always), decoding
  only frames it keeps — the tail costs almost nothing. **Gated by the same
  Start/Stop REC**; auxiliary/isolated (a camera failure warns, never touches
  H7). **Console controls:** resolution + fast-fps **dropdowns** (fps options
  adapt to resolution; both locked while recording), live **transient** +
  **heartbeat** fields, a **● cam** reconnect dot, and a **live preview** pane.
  Verified end-to-end against the real camera (fast→heartbeat transition,
  per-cycle JPEGs, snapshots, `frames.csv`, preview). **Storage:**
  `<session>/video/{frames.csv, cycle_NN/*.jpg, snapshots/*.jpg}` —
  `frames.csv` (`frame_idx,host_ts,monotonic,cycle,mode,rel_path,laser_mm`) is
  the alignment key against `h7.csv`/`stage.csv`. Config: `camera:` block;
  requires `opencv-python` (import is guarded — absent → camera disabled).
- **"Can't reach H7" was a TRANSPORT MISMATCH, not a dead board (2026-07-28).**
  `console_20260728_112825`: `health H7 FAIL (0)`, empty `h7.csv`, `UDP reader:
  recv=0 samples=0 lost=0`, then **425** consecutive
  `H7 command 'disarm' failed: WriteFile failed (PermissionError(13, 'The device
  does not recognize the command.', 22))`. The H7 was streaming the whole time —
  reading COM8 directly gave **987 lines/s** of src=1 (laser, 2.502 V) / src=2
  (load). `config.yaml` had `transport: udp` while the board runs the plain
  **`portenta_m7`** image, which streams over USB — the trap already documented
  in the config. Set back to **`transport: usb`**.
  **Two things this taught us beyond the trap comment:**
  (1) The failure is *worse than logging nothing*. In `udp` the console holds
  COM8 open but never **drains** it, so the M7 blocks in `Serial.write` and
  **wedges**: no boot banner, then every command write fails
  `ERROR_BAD_COMMAND`. Reproduced deliberately (hold the port open unread ~30 s)
  — the board then emits zero bytes and answers nothing; DTR toggle, RTS+DTR,
  and a serial break all fail to recover it. **Only a power cycle (USB + EVM)
  brings it back.**
  (2) UDP could not have worked even with the right firmware: `pc_ip:
  169.254.245.100` matched **no interface on this host** (Ethernet was campus
  DHCP `141.212.82.60`; the only 169.254.x addresses were on disconnected
  Wi-Fi/Bluetooth adapters). Before going back to `udp`, verify BOTH the flashed
  env AND that a linked NIC actually holds `pc_ip`.
- **Camera wouldn't start — a STRANDED subprocess owned it (2026-07-28, FIXED).**
  Symptom: `open failed: no capturable camera found (probed indices [0,1,2,3])`
  on every launch; one session (`console_20260728_110133`) silently recorded
  `cam[1] 640x480` — the **built-in webcam**, not the 12MP. Cause was a chain of
  three defects, all now fixed:
  1. **`reconnect_timeout_s` was 2.0 s, shorter than the camera's own
     open+first-frame latency (~4.5–5.4 s measured).** The watchdog fired before
     the stream ever started, and each reopen cost another 4.5 s — a
     self-sustaining reopen loop that never yields a frame. Now **6.0 s**
     (`lib_config.CameraConfig`, explicit in `config.yaml`).
  2. **The orphaned child could never exit.** `_camera_proc_main` waited on an
     `mp.Event` only the (dead) parent could set, so a console crash / force-close
     left it looping forever, holding the camera against every later run —
     Windows' consent store showed `python.exe … LastUsedTimeStop = 0` for 20 min.
     Now a **parent-liveness watchdog** (`mp.parent_process().is_alive()`, polled
     1 Hz) self-exits. **Note `os.getppid()` is useless here** — on Windows it
     keeps returning the dead pid. Second half of the same bug: the child then
     **hung in interpreter shutdown**, because an `mp.Queue`'s feeder thread is
     joined by an exit finalizer and nobody drains `out_q` once the parent dies
     (0% CPU, invisible, still holding the camera). Fixed with
     `cancel_join_thread()` + `close()` on the child side — the mirror of what
     `CameraProcessProxy.join()` already did on the parent side — plus an
     `os._exit(0)` backstop on the orphan path only.
  3. **`pygrabber` was imported but never declared**, so name-pinning to
     `"12MP U3 Camera"` silently no-opped and resolution always fell back to the
     capability probe — which would happily return a webcam. Added to
     `requirements.txt` (installed), and `_resolve_camera` now **raises** when the
     widest camera found is under `_BIG_SENSOR_MIN_WIDTH` instead of accepting it.
     Bypass with `camera.auto_detect: false`.
  Verified on the rig: name-pinned to index 0 in 0.08 s (was a 4.5 s probe),
  `cam[0] 1920x1080 MJPG`, clean `join()` in 0.5 s (exit 0), and a **force-killed**
  parent now leaves the child dead in **1.1 s** with the camera released.
  **Operator note:** DSHOW also enumerates an **OBS Virtual Camera** on this host,
  which shifts positional indices — one more reason index alone is not trustworthy.
- **No LCR (2026-07-06).** LCR is fully removed from this thermal module — no
  worker/connection, no `lcr.csv`, no LCR UI (status dot, `Ls/Rs` readout, plot
  row, and `ref open`/`ref short` are gone). `build_core` never constructs an
  `LcrWorker`; `config.yaml` keeps only `lcr: {enabled: false}` and the
  `LcrConfig` default is `enabled=False`. The engine's LCR paths remain but are
  inert (queue/worker are `None`). `meta.json` no longer emits an `lcr` block.
- **Stage NEVER moves at launch (2026-07-07, SAFETY).** Homing once drove the
  stage into the fixture and crushed it. Startup now issues **zero** motion:
  `home_on_start`/`move_to_zero_on_start` default **false**, and the worker's
  unconditional `set_velocity()` call was **removed** — `set_velocity` is a
  *continuous-motion* command (`axis.move_velocity`), not a speed setting, so it
  would have started the stage moving (and does so on a stage that retained
  homing). The stage stays exactly where the operator left it; jog it with the
  home/go buttons. Auto-motion is opt-in (`home_on_start: true`, which also
  gates `move_to_zero_on_start`) — use only when the travel is known clear.
- **Idle voltage default 0.5 V + live readout at idle-hold (2026-07-07,
  firmware To-Test).** `sma.v_low` now defaults to **0.5 V** (≈0.12 A,
  non-heating) instead of 0 V, so the coil carries a small rest current whose
  V/I/R is measurable. **Firmware** (`Firmware_SMASensorHub_PIO`, `SMA_IDLE`
  case): while **armed and resting at idle**, it now **streams src=3/4/5 at
  ~10 Hz** (`IDLE_LOG_MS`) — previously telemetry streamed only during a
  drive/cycle, so a bare `arm` showed nothing. Now `arm` simply **holds 0.5 V**
  and the readout populates from the hold itself; the console `on_arm` just
  `arm()` + `set_idle(v_low)` (no `drive`). `measure_baseline` likewise reads
  the idle-hold stream instead of issuing a `drive`. **Needs a firmware
  flash + bench verify** (idle streaming rate, no drops); disarmed still streams
  nothing (no current).
- **Arm-button status colour + click-to-focus preview (2026-07-07).** The SMA
  **arm** button now reflects live state: green **"arm"** when disarmed (safe,
  zero current), amber **"● ARMED"** when the MOSFET is closed — refreshed every
  tick + immediately on click (the red **DISARM** stays the master cutoff). The
  camera **live preview thumbnail is click-to-pop-out**: clicking opens a large
  resizable live view (updated on the same tick) for focusing; closing it
  returns to the thumbnail.
- **Baseline / sensor-zero phase — "measure cold R + zero" (2026-07-07, To-Test).**
  A quiescent companion to the go-to-defined-start behaviour: the operator
  button **"measure baseline (cold R + zero)"** (or `baseline.auto_on_start`)
  calls `RecordingCore.measure_baseline()`, which **arms at a low, non-heating
  probe** (`baseline.probe_v`, ~0.5 V ≈ 0.12 A), issues one `drive` so the
  firmware streams src=3/4/5 for `duration_s`, averages the window, then
  **auto-disarms**. It captures **cold SMA resistance**, the **laser rest
  voltage**, and the **load-cell rest voltage** — the latter written into
  `calibration.load_cell.offset_V` (per-session **tare**) unless the channel is
  saturated. Rationale: disarm (MOSFET-open) is the safe start state but gives
  *zero* current, so R is unmeasurable there; the idle-armed probe is the
  self-cooling middle state where R can be read without heating. **Guards:** the
  load channel is checked for ADC-rail saturation (`|raw|≥2²³`) and % of ±5 V
  range — a saturated/near-rail load cell **blocks the tare** and warns to null
  the LCA-9PC ZERO pot first. Results + `baseline_config` are recorded in
  `meta.json`; `events.csv` gets `baseline start`/`done` markers. Refused while
  recording (it drains the queues). **Not yet bench-run** — the reduction/tare/
  saturation logic is unit-tested on synthetic samples; the arm→drive→stream
  timing needs a real-rig verify.
- **Manual recording.** On launch the console runs the startup health check and
  shows live plots/readouts, but writes **nothing to disk** until the operator
  clicks **Start REC** (queues are still drained so the buffers never overflow).
  Click again to **Stop REC**. The `--headless` runner auto-starts recording.
  `events.csv` boundaries: `recording start` / `recording stop`.
- **Click-to-reconnect.** The H7 / stage status dots are buttons — click a
  red (offline/failed) stream to rebuild its worker and retry the hardware
  connection (reuses the same queue). Dots update live each tick.
- **Auxiliary failures are isolated.** A Zaber worker crash no longer trips the
  shared `stop_event` (which previously cascaded and killed the critical H7
  stream + whole session). Only the health monitor decides aborts.
- **Stage health.** A connected, streaming Zaber **passes** even when parked
  outside the workflow window `[lo, hi]` (it's telemetry-only) — that's now a
  warning, not a `FAIL`/"offline" verdict.
- **Manual stage motion (2026-07-06).** The Stage group now has **home** + **go**
  + **STOP** buttons (and Enter in the `target (mm)` field triggers **go**), plus
  editable **min/max limit fields** with a **set** button. These are
  operator-initiated only — the recording pipeline stays telemetry-only and never
  autonomously commands motion. Motion is routed through the worker that owns the
  serial session (`RecordingCore.stage_home/stage_move/stage_stop/stage_set_limits`
  → `ZaberWorker`), and the driver now serializes every serial transaction with a
  lock, so a move issued while the poll loop reads position no longer gets
  dropped/garbled. **STOP** is an e-stop (halts motion immediately, no homing
  required). The **limit window** clamps go-to moves and also drives the health
  "workflow window"; editing it applies at runtime to both the driver and config.
  Absolute go-to requires a homed stage (click **home** first, since
  `home_on_start` is `false` by default); clamps and not-homed refusals are
  surfaced in the log. **To-Test on the bench.**
- **Input-field normalization (2026-07-06).** Every numeric field self-tidies
  when focus leaves it (and again when its button is clicked): values are parsed,
  clamped to a per-field range, and reformatted to fixed precision — voltages to 2
  dp clamped to `SMA_MAX_V = 5.2 V` (LDO ceiling), stage target/limit fields to 2
  dp clamped to `STAGE_MAX_MM = 300 mm` (travel), time (ms) and cycle count as
  integers. So typing `100` shows `100.00`, and an over-range `6 V` snaps to
  `5.20`. The limits row was also re-spaced (the `max` field no longer overlaps
  the `set` button).
- **Laser/load voltage-glitch filter (host-side).** The combined firmware emits
  one laser/load sample with `value==0 V` on ~every 32nd ADC1 frame while its
  `raw_code` is a normal non-zero value (the paired load sample is also dropped
  on those frames). It shows up as a huge periodic spike to 0 on the plot.
  `H7Worker` drops these self-inconsistent samples (V=0 with non-zero raw),
  counted in `n_glitch` and logged. Measured impact on a real run: removing the
  108/8888 (1.2%) glitch samples cut laser σ from 162 mV → **0.83 mV**. **The
  underlying firmware voltage-field bug is still open** (raw stream is correct);
  see TODO — needs a bench rebuild to fix at the source.

## Design rules (V3)

- **Recorder logs RAW data only.** It configures instruments at startup but never converts units or pushes calibration to firmware.
- **Calibration coefficients** (`config.calibration`) are recorded in `meta.json` and consumed **only** by the offline analysis (`lib_analysis` / `operator_explore.ipynb`).
- **Any stream can be disabled** via its `enabled:` flag (lcr / h7 / stage).
- **SMA actuation runs on M7**, not the host. With `sma.enabled: true` the recorder sends `cycle …` params + a 1 Hz `ping` heartbeat + `stop`; M7 owns all phase timing (deterministic, host out of the loop) with a watchdog safe-stop. `sma.enabled: false` → pure logger, manual console actuation.

## TODOs

> ### ▶ NEXT SESSION — RNN DATASET B, THERMAL MEMORY (~2 h)
>
> Everything collected so far is **dataset A**: 12–30 s gaps, quasi-independent
> pulses. That does not need a recurrent model. Dataset B — short gaps, where
> the coil carries thermal state between pulses — is the RNN's reason to exist
> and has never been collected.
>
> **PHASE 1 first, non-negotiable: `profiles/soak_ladder.json` (~23 min).**
> The bulk profile cannot be written without its result. At short gaps the
> binding constraint stops being per-pulse energy and becomes **duty-average
> power**, `P_avg = E/(t+gap)`:
>
> | | 30 s gap (all data so far) | 2 s gap |
> |---|---|---|
> | 850×400 (1.24 J) | 0.041 W | **0.52 W** |
> | 550×200 (0.30 J) | 0.010 W | 0.136 W |
>
> So the whole short-gap regime is **5–12× outside anything characterized**.
> Two energies × six gaps (20→2 s), bursts of 8, low energy fully before high.
> **Watch three things and stop at the first:** force baseline ratcheting across
> the burst, stroke decaying pulse-to-pulse, and whether R returns to cold R₀ in
> the gap. The last one is the point — if R does not recover, the coil is
> carrying state into the next pulse, which is the regime we want *and* where
> damage starts.
>
> **PHASE 2: generate the bulk profile from what phase 1 measured** —
> `python analysis/make_rnn_profile.py --minutes 95 --gaps <safe set> --p-avg-max <W>`,
> then **`--dry-run` it**. ~68 sequences of 6+1 pulses, stratified by gap,
> shuffled so slow drift (fatigue, ambient, a degrading contact) cannot
> correlate with condition. Both caps enforced: 1.24 J per pulse and the
> measured `P_avg`.
>
> **A TOOL LIMIT TO KNOW BEFORE INTERPRETING THIS DATA.** One condition = one
> training sequence, and **current and pulse width cannot vary inside it** — the
> sweep calls `h7.disarm()` after every condition, then holds `settle_s`
> disarmed, so the coil leaves the armed state and R stops being observable
> across a condition boundary. Thermal memory therefore appears here only as
> pulse-to-pulse change within a **constant-drive** burst; the network never
> sees an amplitude change mid-sequence. Lifting this needs the inter-condition
> disarm suppressed — a tool change, deliberately deferred until after this
> session so tomorrow carries no code risk.
>
> **Enabling fact, checked:** R *is* usable as the between-pulse temperature
> proxy despite 24% per-sample noise. Averaged over 100 ms it is ~2.4% against a
> thermal signal of ~14% (4.03 Ω hot → 4.70 Ω cold) — SNR ≈ 6.
>
> `settle_s: 30` disarmed between sequences, chosen so every sequence provably
> starts cold (worst-case recovery measured 12.4 s).

> ### ▶ 30 s-COOL HEAT-TIME MAP — DONE, 28/35 CELLS (2026-07-31)
>
> **Dataset: `data/derived/heat_time_map_20260731_all.csv`** — 324 cycles across 36
> conditions and 5 sweeps, 30 s cool throughout. Regenerate with
> `analysis/analyze_raw.py` (the one-off `make_heat_time_map_clean_20260731.py`
> named here previously was deleted when that pipeline replaced it); envelopes
> via `analysis/plot_envelope.py` in
> `data/derived/heat_time_map_20260731_all_{stroke,force}.png`. **This supersedes
> `heat_time_map_20260730_clean.csv`** — that campaign ran 15 s cool (25 s at two
> cells) and is not protocol-comparable.
>
> **NOTHING IS FILTERED OUT.** This is RNN training data, so deciding which
> pulses "count" belongs to the training pipeline — a network that only sees
> clean pulses cannot learn that clipping, sub-threshold drive, or a railed
> actuator are real machine states. Every cycle carries flags instead:
> `bootstrap` / `clipped` / `railed` / `cc_pct` / `detect_ok` / `i_low_mA` /
> `seeded`. The envelope CHART aggregates the trustworthy subset; the CSV keeps
> everything.
>
> **Cross-validates against 2026-07-30 to 0.4–3.4%** at 100 ms (650/750/850/950
> mA), across a different day and a different cool protocol.
>
> **Coverage.** 100 ms row: only 650–950 mA (see the detection caveat below).
> 200 ms: 350–950. 300 ms: all 9 levels. 400 ms: 150–850.
> **950×400 is excluded by measurement, not omission** — at the correct 947 mA it
> pins the laser at the 0 V rail (1228 samples) *and* clips the load cell
> (5.000 V). Characterized in `sweep_20260731_134414`. The measurable energy
> ceiling is **~1.24 J (850×400)**, not the ~1 J assumed in the RNN plan below.
>
> **STILL OPEN — 7 cells, and the data may already be on disk.** 150/250/350/450/
> 550×100 and 150/250×200 come back mis-windowed (`detect_ok=0`): the cool phase
> carries a 0.5 V idle bias → ~107 mA whose noise reaches p95 ~155 mA and p99.9
> ~200 mA, so a 150–250 mA heat pulse sits INSIDE that band, and at 100 ms there
> are too few samples to separate them. With 30 s cools there are ~2.5× more
> noise excursions than the 12–15 s cools of 2026-07-30, so threshold detection
> returns phantom cycles — **36 rows at 150×100 against 6 commanded**. The same
> 150 mA level detects cleanly at 400 ms. **The pulses ARE in the raw data**
> (550×100 shows exactly 6 bursts at a 300 mA threshold) — they are mis-WINDOWED,
> not missing. Fix: derive heat windows from the **commanded `cccycle` schedule**
> instead of thresholding the current trace, then re-run the merge. No rig time
> needed. Do this before spending 25 min re-running them.

> ### ▶ COOL-PHASE LATCH — RUN THE GRID WITH `--i-low 0` (2026-07-31)
>
> **`operator_current_sweep.py … --i-low 0` is now the required protocol** for
> `cccycle` runs. With the default `i_low 100` the first grid attempt
> (`sweep_20260731_145838`) corrupted itself at condition 12 of 35.
>
> **The fault.** `i_low = 100 mA` is **below the reachable floor**: the LDO cannot
> go under 0.5 V, and 0.5/4.69 = **106.6 mA**. So the loop chases a target it can
> never reach, sitting permanently 6.6 mA outside it against a `near` band of
> 12 mA — **5.4 mA of margin against 12.6 mA of sense noise**, so ~40% of cool
> ticks fall outside the band on noise alone. Both `R_est` adaptation and the
> integral are gated on `near`. Once `R_est` drifts above ~5.0 Ω, `u_ff =
> i_low × R_est` exceeds 0.5 V, the loop lifts off the floor, current rises past
> the band, and **everything freezes** — with `cc_Kp = 0` there is no ungated term
> left to pull it back. The gating assumes "outside the band = a transient that
> will come back"; at the floor nothing brings it back.
>
> Measured: cool sat at **0.972 V / 208 mA instead of 0.500 V / 108 mA** — 0.20 W
> vs 0.053 W, ~4× the idle heating. The wire never cooled, sat ~1.5 mm contracted
> (laser rest 4.58 → 3.85 V), the force baseline climbed and pinned at 5.000 V.
>
> **With `--i-low 0`** the cool phase calls `ccRelease()` and parks passively at
> the LDO floor: no setpoint to miss, no band to exit, no estimator running on a
> noisy low-current point. Verified over 24 conditions — cool held **0.4998–
> 0.5009 V / 106.0–107.1 mA / 4.671–4.721 Ω, zero latch events**, tighter than
> regulated cool ever was. V/I still stream at ~1 kHz during cool
> (`serviceActuationPhase` non-CC branch), so **R stays observable** as the
> self-sensing baseline. Safe **only because of the pre-run R seed** — the old
> i_low=0 failure is gated on `!cc_R_valid`.
>
> **Two diagnostic signatures, easy to confuse, both seen today:**
> | | contact fault | cool-phase latch |
> |---|---|---|
> | `R = u/I` | wildly impossible, 1.2–30 Ω | **stable and correct** (4.68 Ω) |
> | cool `u` | drops *below* the 0.5 V floor | sits *above* the floor |
>
> **`--abort-on-bad-sense` misattributes the latch** — it fires on "cool current
> implies R < 2 Ω" and tells you to reseat the clips, which is wrong when R
> measures 4.681 Ω. Right verdict, wrong reason. It also **silently PASSES when
> it has too few cool samples to judge** ("only 23 cool samples — not enough to
> judge"), which let a corrupted 950×400 condition through earlier the same day.
> A guard that cannot evaluate should stop, not continue. **TODO: fix both.**

> ### ▶ SUPERSEDED — FIX HIGH-ENERGY CURRENT SENSE, THEN RERUN FULL SPAN @ 30 s COOL (2026-07-30)
>
> Done 2026-07-31, see above. The "current sense dies at high energy" diagnosis
> was **confirmed as an intermittent contact**: it recurred at 950×400
> (`sweep_20260731_141513`) with `u` dropping below the 0.5 V LDO floor and
> implied R ranging 1.2–30 Ω, and **reseating the SMA clips fixed it** — the same
> cell then ran 4/4 cycles on target. Cost of not catching it immediately: four
> ~2 J overdrive pulses at 1.07–1.10 A. `--abort-on-bad-sense` did not stop it
> (see the guard TODO above).
>
> **Where the heat-time map stands.** Five 2026-07-30 sweeps merged into
> `data/derived/heat_time_map_20260730_clean.csv` (219 clean cycles; regenerate with
> `diagnostics/make_heat_time_map_clean.py`, envelopes in
> `heat_time_map_20260730_{envelope,force_envelope}.png`). Coverage:
> 100/200/300 ms rows complete over 150–950 mA (950×300 is n=1, needs one
> confirming retry); **400 ms row missing above 650 mA**. Cycles were dropped
> for: supply-OCP dead (i pinned ~39 mA, `u` railed 5 V — the PSU current
> limit was set too low; raised since, keep it ≥1.5 A), over-current
> transients, load-cell clip, baseline ratchet, bootstrap. `cool_s` is now a
> recorded per-cycle column — at 850×300 the 25 s cycles read ~10% larger
> stroke than the 15 s ones, so **cool time is an input variable, not
> bookkeeping**.
>
> **1) TROUBLESHOOT FIRST — current sense dies at high energy.** Twice
> (sweep_150846 during 750×300; sweep_162137 from 950×300 onward) the sense
> read a flat, wandering **111–131 mA regardless of commanded current**
> (~0.25 V at A1; true current ~1.1 A should read 2.2 V at the 2 V/A scale)
> while the coil visibly actuated — the blind CC loop then railed the LDO and
> overdrove everything after. **It is hardware, in the sense CHAIN, not the
> shunt element and not firmware:** the 200 mΩ / 1 W shunt dissipates only
> 0.24 W even railed (4× margin, ~2% duty); an open shunt would have stopped
> actuation entirely; and in sweep_150846 the fault **self-healed mid-run**
> within one firmware session (dead for 750×300, correct 846 mA by 850×300) —
> latched software doesn't recover, loose contacts do. Both onsets followed
> the most violent conditions (heat + multi-mm yank on the wiring). Check in
> order: (a) INA296A OUT → A1 jumper + ground return, (b) INA296A supply pin,
> (c) shunt Kelvin sense taps; then verify with the CC firmware `read`
> command against a series DMM. Run retries with `--abort-on-bad-sense` so a
> dead sense aborts instead of firing railed 1.1 A pulses through the rest of
> the sweep (that is what burned the fill run's tail).
>
> **2) THEN rerun the full span at 30 s cool** —
> `profiles/heat_time_map_30s.json` (9 levels × 4 heats, cool 30 s, ~2 h;
> `--dry-run` first). One protocol for the whole grid supersedes the mixed
> 15/25/30 s data. Expected instrument limits, from the current envelope:
> **750×400** should come back clean; **850×400** marginal (force peak
> ~4.0 V of 5, stroke ~7.5 of ~8.6 mm); **950×400 is beyond the
> instrumentation** — force rise ≥3 V clips the 490 mN load cell from a cold
> baseline, and stroke hits the laser rail. The laser rail is the amp's
> **default ±5 mm analog scaling, not the sensor** (IL-030 measures
> 20–45 mm; IL-E manual p.4-27): free-range rescaling would recover the
> displacement at the cost of ~2× noise and a **fresh laser calibration**
> (k/V₀ die with the scaling). Force above 490 mN is unmeasurable with this
> load cell regardless. Note this supersedes the 2026-07-28 "load cell never
> saturates" claim below — that held for 100 ms pulses; at 300–400 ms it
> saturates from ~850 mA.

> ### ▶ M7 AND M4 DO NOT SHARE A CLOCK — +2.193 s (2026-07-29)
>
> `src=1` (laser) and `src=2` (load) are stamped by the **M4**; `src=3/4/6/7`
> (SMA V/I, CC command, R_est) by the **M7**, which boots first and runs
> **2.193 s AHEAD**, stable to 1 ms over an 8-minute run. Untreated, sensors plot
> 2.2 s early and displacement appears to **peak before the current pulse that
> causes it**. Every per-pulse number taken before this was measuring the decay
> tail of the PREVIOUS pulse.
>
> `lib_h7_session.m4_clock_offset_s()` reads it from a saved console log
> (`m7_us=` / `m4_us=` in the firmware STATUS line); `align_m4()` applies it.
> **Any analysis joining sensor and SMA channels must use them** —
> `operator_console.py` and `operator_explore.ipynb` have NOT been audited yet.
> `operator_current_sweep.py` now applies it LIVE (2026-07-30):
> `m4_offset_from_capture()` parses the offset from the run's own console
> stream, the in-run verdict analyses the aligned copy, the CSV keeps raw
> timestamps, and `meta.json` records `m4_clock_offset_s`. Verified by
> replaying `level_650mA` through the live path: +367.3 µm mean signed dx
> (reference 367.7) vs −86 µm incoherent unaligned. Same commit fixes
> `--stop-on-fail`, which 9d71b76 referenced but never added to argparse —
> any NOTE (clip / CC <80% / FAULT) raised `AttributeError` and killed the
> sweep, so the >650 mA session would have died at its first marginal level.
> `summary.csv` now also carries `dx_um`/`x_base_um`/`bootstrap`.

> ### ▶ REFERENCE DATASET + ACTUATION CURVE — FULL SPAN 150–950 mA (2026-07-30)
>
> **`data/raw/sweep_full_150-950mA/`** — the 2026-07-29 (150–650) and 2026-07-30
> (650–950) sweeps merged, every level re-analysed from raw through one
> identical clock-aligned path (`summary_combined.csv`,
> `fig_actuation_150-950mA.png`; per-file `_YYYYMMDD` suffixes give
> provenance; folder README has the method + full table). Level means
> (signed dx over verdict cycles):
>
> | cmd | achieved | Δx | ΔF |
> |---|---|---|---|
> | 150 | 156 mA | 13.4 µm | 2.2 mN |
> | 650 (run 1 / run 2) | 640 / 644 mA | **367.3 / 363.6 µm** | 12.8 / 13.2 mN |
> | 950 | 939 mA | 864.0 µm | 28.8 mN |
>
> Monotonic and superlinear across the whole decade; the 650 mA cross-day
> repeat agrees to **1%**. No clipping, no force-baseline ratchet even at
> 950 mA (the >750 mA damage signature of 2026-07-28 did NOT reappear at
> 100 ms / 12 s duty), CC at 99% of command at 850/950. Exception: the
> **750-command level overshot** (757–857 mA across cycles, mean 792) — a
> control anomaly, not a response one; its points sit on the curve when
> plotted against achieved current. Likely the `R_est` single-sample
> bootstrap (below). **RNN range: 150–950 mA**; the binding ceiling is the
> LDO (~5.2 V / R_wire ≈ 1.1 A), not any sensor.
>
> **Sample rates:** sma_i/sma_v **954 Hz** (one per CC tick, `ADC_SAMPLES_CYCLE=4`
> reads averaged, ~95 points per 100 ms pulse); cc_u/R_est 993 Hz; laser/load
> 495 Hz streamed but **400 SPS converted** (19% duplicate rows, Nyquist 200 Hz).
>
> **Judge actuation on DISPLACEMENT.** The fixture is compliant, so the coil
> moves rather than loading: force moves 1–3 mN per pulse (noise) while
> displacement moves 20–370 µm. Use the SIGNED mean across cycles — real
> actuation is coherent in sign; averaging magnitudes rectifies the noise and
> turns a 4.9 µm non-response into 23.3 µm.

> ### ✔ RESOLVED — "CC OVERSHOOT" WAS AN INTERMITTENT CONTACT (2026-07-28)
>
> **The CC loop is fine.** Re-running the identical `cccycle 550/100` profile
> 100 minutes later holds **540.8 / 540.1 mA against 550 commanded
> (98.2%)** with `R_est` at 4.68 Ω. The failing sweep held 769–817 mA (140–148%)
> with `R_est` stranded at 6.27 Ω. Cool-phase noise fell 71.53 → 14.96 mA sd,
> kurtosis 4.57 → 3.11, samples >mean+100 mA 12.91% → 0.00%.
> `data/raw/isense_20260728_233618_sma-connected-cccycle`.
>
> **Chain:** intermittent clip contact → multimodal current readings → the
> `R_est` bootstrap latches `u/I` from ONE bad sample → feedforward overshoots
> 46% → the ±12% `near` gate can never open → stuck all run. The failing sweep's
> own **pulse 5 proves it**: `R_est` fell to 4.73 Ω and that pulse landed at
> **102%**.
>
> **THE RIG FAULT ITSELF IS NOT DIAGNOSED.** I first blamed reseating the clips;
> the timeline refutes it (fault gone 02:44 UTC, unclipped 03:12). Every
> *operating-point* variable is excluded — drive voltage, DAC code (sweep gap 4
> sat at the 0.5 V floor and was still corrupt), current level, load connected,
> loop open or closed, heat or cool phase, uptime, dropped samples, mux leakage.
> It tracks the SESSION, not the condition. Opening the port resets the H7 but
> not the EVM analog rails, so board state survives across captures.
>
> **Diagnostic tell:** a current distribution with *physically impossible* modes.
> Readings of 270 and 400 mA when `u = 0.625 V` into 4.2 Ω caps current at
> 149 mA. Suspect this before suspecting firmware — today's ADC2 dropouts and crc
> storms may share the cause. Now checked automatically by `measurement_sane()`,
> which ABORTS a sweep rather than record unusable data.
>
> **Two numbers I published during this were wrong, both from the contaminated
> phase:** 144 mA/read (real: ~30 mA/read, 12× the Uno not 57×) and "averaging
> cannot fix this". Seven hypotheses were chased and refuted before the rig
> itself turned out to be the variable — the sweep README lists them with the
> measurement that killed each, so nobody re-runs them.
>
> **Still worth doing, but NOT blocking:** the `R_est` bootstrap should latch
> from a *settled* railed point rather than one sample, and `cccycle` should be
> able to emit the reachability warning (it is gated on `cc_R_valid`, which
> `startCycleCC()` clears via `ccReset()` on the line before). A single bad
> sample stranding the loop for an entire run is a real robustness gap.

> ### ▶ JUDGE ACTUATION ON DISPLACEMENT, NOT FORCE (2026-07-28)
>
> The fixture is compliant, so a contracting coil mostly **moves**. Per pulse,
> force changes 1–3 mN (inside the noise) while displacement changes 5–530 µm and
> is cleanly monotonic in current. The load cell **never saturates** — 380 mN
> peak against a 490 mN rating even at 928 mA, so "max current before the load
> cell saturates" is not the binding constraint for the RNN upper bound.
> `operator_current_sweep.py` still judges on src=2 and must be moved to src=1.
> Above ~750 mA the force baseline ratchets *down* across a run (180 → 380 → 232
> mN), which is not repeatable cycling — cap at 550 mA until that is understood.

> ### ▶ CC BOOTSTRAP + REACHABILITY (firmware — ASK BEFORE EDITING) (2026-07-28)
>
> In `Firmware_SMAConstantCurrent_PIO`, unchanged pending approval:
> 1. `R_est` bootstraps from a **single** ADC sample on a railed point taken mid
>    rise (`0.5 V / 0.08 A = 6.25 Ω`). Latch from a *settled* railed point.
> 2. `cccycle` emits **no** reachability warning: it lives only in the `cc <mA>`
>    path and is gated on `cc_R_valid`, which `startCycleCC()` clears via
>    `ccReset()` on the line before — structurally dead for every cycle run.
> 3. Runtime-only workaround, untested: `ccgain 25`. `cc_Kp` defaults to 0, so
>    there is no proportional term at all; with `Kp > 0` the P term pulls the
>    error inside the gate, which lets `R_est` self-correct. Survives `ccReset`.

> ### ▶ OPEN ISSUE — CYCLE TIMING DISTORTED BY USB-CDC BACK-PRESSURE (2026-07-15)
>
> **Symptom.** In firmware-timed `cycle` runs the cool phase overshoots: the
> first ~2 cools are correct (~3.1 s) then cools stretch to 5–8 s. Also seen:
> "fire N early", force plot shows vertical lines, sensor rate drops. Sessions:
> `console_20260715_150641`, `console_20260715_160458`.
>
> **Root cause (confirmed from data).** The M7 runs a *cooperative* state machine
> — `serviceSma()` checks the cool timer (`t_rel >= cyc_cool_ms`) in the **same
> super-loop** that does the **blocking** `Serial.write` stream. When the host PC
> falls behind reading the serial (the camera/GUI starving the H7 reader thread),
> the M7's write blocks; `millis()` keeps running, so the cool-timer check is
> serviced late → cool overshoots. Measured: **8 M7 stalls of 4–5 s each**
> (firmware clock jumps with zero samples produced). During a stall the M4→M7
> sensor **ring overflows → lost laser/load samples** (92 Hz vs 400), and the
> backlog arrives in a **burst** (→ compressed host timestamps → vertical lines).
> The `ping` heartbeat stays regular because that's the *opposite* USB direction.
>
> **Two distinct problems:** (a) REAL actuation distortion (the wire genuinely
> cooled 5–8 s) + REAL sensor-sample loss (ring overflow); (b) MEASUREMENT
> smearing (host timestamps logged late). Confirm with firmware clock: fire
> intervals on `hw_us` read 3.22 s (cycles 2–3, correct) then 5.7–7.8 s (real).
>
> **Tried / done (host-side, no dropped samples):**
> - `hw_us` time base in `lib_analysis` (`timebase()`): analysis reads the
>   firmware clock, not the bursty host clock → fixes (b) only. (Measurement.)
> - `portenta_reader.py`: `write_timeout=0.5 s` (a stalled write can't freeze the
>   UI) + `set_buffer_size(rx=4 MB)` (bridge ~30 s of host stall so the M7 never
>   back-pressures — targets (a) WITHOUT dropping samples).
> - Single serial owner: SMA commands enqueued to the H7 reader thread, never
>   written from the GUI thread (no read/write race on one COM handle).
> - H7 reader thread self-pins to a dedicated core + `above_normal` priority.
> - Camera: loop pacing (grab() busy-spin fix), decoupled cheap preview
>   (`preview_hz`/`preview_width`), and OPT-IN `camera.use_subprocess: true`
>   (camera in its OWN process → its GIL/core can't stall the H7 reader).
>
> **Believed cause of the ~4–5 s reader starvation:** the in-thread camera's
> fast-capture window (≈ `stop_dwell_s`) holding the GIL / saturating a core.
>
> **FAILED (2026-07-15, reverted):** firmware non-blocking write gated on
> `Serial.availableForWrite()` **dropped ALL data** — on the Portenta mbed
> USB-CDC that call returns ≈0 almost always (immediate endpoint capacity, not a
> buffered free-space count), so `nbWrite` dropped nearly every sample
> (`tx_drop` climbed at the full sample rate; host got only `[STATUS]`). **Do NOT
> gate firmware writes on `availableForWrite()`.** A real firmware non-blocking
> write needs a SOFTWARE TX ring buffer (or mbed `send_nb()`), a bigger change.
>
> **NEXT STEP (in order):**
> 1. **Bench-test the host-side fix with the CONSOLE** (not `pio device
>    monitor` — it has no big buffer): `camera.use_subprocess: true` + the 4 MB
>    RX buffer + reader priority, on the WORKING (blocking) firmware. If
>    `usbser.sys` keeps filling the 4 MB buffer while the app is busy, the M7's
>    blocking write returns fast → no stall, **no firmware change, no drops**.
>    Verify: cool ~3.1 s on the `hw_us` timeline, ~400 Hz sensor rate, no
>    firmware-clock gaps.
> 2. If stalls persist → **firmware SOFTWARE TX RING** (NOT `availableForWrite`).
> 3. Long-term ideal: **UDP over the H7 Ethernet** — fire-and-forget, the control
>    loop structurally cannot block. Biggest lift; only if 1–2 are insufficient.
>
> Note: this is SEPARATE from the "`cool_ms` too short vs τ_F≈6 s" thermal TODO
> below — that's about physics (independence of cycles); this is about the
> firmware not holding the *commanded* 3 s in the first place.

> ### ▶ NEXT SESSION — START HERE (2026-07-13 EOD)
>
> The 1 kHz SMA config is **ported into `Firmware_SMASensorHub_PIO` and builds
> clean, but has never been on the rig.** Everything else below is unchanged.
>
> ```
> cd ../Firmware_SMASensorHub_PIO
> pio run -e portenta_m7 -t upload     # then POWER-CYCLE USB + EVM (else ADS1263 = ID 0x00)
> ```
> Then walk the **4 gates in `Firmware_SMASensorHub_PIO/STATUS.md`, in order,
> stopping at the first failure**: (1) cadence — ~96 src=3/4/5 points inside a
> 100 ms fire, was 8; (2) `dropped`/`crc_err` still **0**; (3) **the V and I
> means, NOT R** — R is immune to the exact bug this gate looks for, and expect
> V/I to land **~5% LOWER** than old sessions (that's the duty error going away,
> not a regression); (4) idle telemetry still ~10 Hz.
>
> Rollback if it misbehaves: `pio run -e portenta_m7_legacy100 -t upload`.
>
> **Loose end:** this repo tracks `.pio/build/` in git with no `.gitignore`, and
> the rebuild pruned 179 stale object files (they show as deleted; nothing is
> committed). Decide: `git rm -r --cached */.pio && echo ".pio/" >> .gitignore`
> (recommended — they're regenerable and now stale), or `git checkout -- */.pio`
> to put them back.

- [ ] **Bench-run the console** (`operator_console.py`) against the real rig: health-check pass/fail, live readouts/plots, `DISARM`, auto-disarm + 1 s warn / 3 s critical-disarm on unplugging the H7, clean shutdown writes `meta.json`. LCR/stage are **auxiliary** (warn-only); H7 is critical.
- [ ] **Bench-run** a full OPEN→SHORT→RAW session against the real rig (LCR + combined-firmware H7 + Zaber).
- [ ] **Fill calibration** in `config.yaml` from `Calibrate_LaserHead` / `Calibrate_LoadCell` fits.
- [ ] **Flash + bench-verify idle telemetry streaming** (`Firmware_SMASensorHub_PIO`, `SMA_IDLE` case): after `arm`, confirm src=3/4/5 stream at ~10 Hz while holding 0.5 V idle (readout populates), that disarm stops the stream, and that it doesn't perturb the M4 laser/load ring rates. Requires reflash + power-cycle (EVM rails).
- [ ] **Bench-verify the baseline phase** (`measure_baseline`): confirm the arm→`drive` at `probe_v` streams src=3/4/5 for the window (idle current, no heating), the cold-R / laser-rest / load-rest means are sane, auto-disarm fires, and the load-saturation guard trips when the LCA-9PC ZERO pot is deliberately off. Then decide whether `baseline.auto_on_start` should default true.
- [ ] **Verify H7 channel rates** — confirm `[STATUS]` shows no drops with all 5 src streaming during a `drive`.
- [ ] **FIRMWARE BUG: laser/load V-field zero-glitch** (`Firmware_SMASensorHub_PIO`) — ~every 32nd ADC1 frame emits `voltage_V==0` despite a valid `raw_code` (and skips the paired ADC2/load sample). Raw codes are correct, so it's in the M4 voltage path / ADC1↔ADC2 interleave, not the ADC read. Currently masked host-side by the `H7Worker` glitch filter; fix at the source on the bench (suspect the `r1.status & 0x80` ADC2-piggyback branch around `main.cpp:1198-1214`).
- [ ] **LASER 65.8 Hz TONE — ACCEPTED, not fatal; do not chase unless it changes** (found 2026-07-13; reference sample `data/raw/console_20260713_122906_laserfix` = laser on an **immovable block**; diagnose with the laser view (`lib_analysis`; notebook port TODO); full write-up in README). The laser's apparent ±1.4 µm "noise" is a coherent **65.77 Hz / 1.72 µm** ripple carrying **74%** of the channel's variance. **It is instrumental, not mechanical:** it survives an immovable target, and it sits at the *same* 65.77 Hz (to 0.008%) in the actuation session `console_20260713_115921` despite a completely different mass/stiffness — a real resonance would have shifted. Load/ADC2 is clean at that frequency. **Why we accept it:** 66 Hz is an order of magnitude above our DC–few-Hz signal band and is stationary, so averaging over a fire (6.6 cycles) or a cool (197 cycles) suppresses it, and a notch recovers σ 1.29 → 0.31 µm in post. It costs raw plot resolution, not correctness. **If it bites us:** (1) feed ADC1 a DC voltage with the IL-030 disconnected — tone survives ⇒ ADC/wiring, tone vanishes ⇒ IL-030; (2) resolve the alias (could really be 335/466 Hz) — but that needs the read-path fix below, not just a rate constant; (3) re-open immediately if the tone ever drifts, grows, or gains a low-frequency sibling.
- [x] ~~**SMA resistance transition unresolvable**~~ — **WRONG, corrected 2026-07-13.** The transition IS resolved: **ΔR/R₀ = −3.13% ± 0.54% during the fire (t = −5.8)**, recovering to baseline by the end of the 3 s cool. It only looked unresolvable because the metric took `max()` over the fire window, which on a ±6% single-sample noise floor returns the largest *noise* excursion — always positive — and hid a real effect that is *negative*. The right estimator is the window **mean**, averaged across cycles (`lib_analysis`; transition-view notebook port TODO). The `cycles` view now reports `dR_fire_pct` (mean), not `dR_peak_pct` (max).
- [ ] **`cool_ms` is far too short.** The force cooling fit gives **τ_F ≳ 6 s** against a `cool_ms` of only **3 s**, so the coil never returns to baseline before the next fire — this is the cause of the ratcheting force baseline across the run, and it means the 10 cycles are **not independent**. Raise `cool_ms` to ≥ 3–5 × τ (~20–30 s) for clean cycles, or accept and model the accumulation. τ itself is only a **lower bound** until the cool window exceeds it.
- [ ] **SMA `sma_v`/`sma_i` read +7% HIGH; power/energy ~15% high** (found 2026-07-13 on the bench, `Firmware_SMARateTest_PIO` runs 0-7). The H7's on-chip ADC reads high **in proportion to its conversion duty** — `V = 0.01508 × duty% + 2.988`, R² = 0.9996 across 8 runs with the DAC code held fixed. Production (`CYCLE_LOG_MS=10`, `ADC_SAMPLES=64` → 14% duty) therefore inflates V and I by ~7%, and **power by ~15%** (P = V·I squares it). **What this does and does not affect:** `sma_r` is **EXACTLY immune** (both channels scale together, R = V/I cancels — R sat at 21.4 Ω while V drifted +33%), so **every resistance result stands**; laser/load are unaffected (ADS1263 + external REF7050). Only the **absolute** power/energy numbers on the dashboard move (2.18 W → ~1.9 W; 6.5 J → ~5.6 J) — the **fire-vs-idle ratio is unchanged**, so "the idle probe delivers more heat than all ten fires" still holds. **Partly fixed 2026-07-13:** the `portenta_m7_rate1k_n4` port (above) drops the in-cycle duty 14% → 12%, which removes most of it — pending the bench run that confirms V/I come back down. **Still open:** `ADC_VREF_V = 3.145` is itself ~5% off (true ≈ 2.99 V), a *standing* mis-calibration independent of duty. Fix it by reading the STM32's internal **VREFINT** and self-correcting, rather than trusting a hard-coded constant — that would also confirm the droop mechanism outright (it is currently inferred from behaviour; Vref was never measured directly). **All existing sessions' V/I/power are affected; all R results are not.**
- [x] ~~**Port the 1 kHz SMA config to production.**~~ — **PORTED 2026-07-13 into `Firmware_SMASensorHub_PIO` (builds clean; NOT yet flashed/bench-run).** `portenta_m7_rate1k_n4` carried over verbatim: `CYCLE_LOG_MS=1`, `ADC_SAMPLES_CYCLE=4` (idle/manual stay at 64), `SMA_SETTLE_US=50` (was `delay(1)`), batched SMA emit, M7 timestamps, plus `loop_hz` in `[STATUS]`. Key idea: each answer is built from 4 ADC readings instead of 64, so a single answer is noisier — but you get 10× more of them and average on the PC, where averaging is free and doesn't drain the ADC's reference. This is what makes each per-cycle ΔR error bar shrink instead of relying on the 10-cycle ensemble. **Host needs no change** — line format is byte-identical (verified against `portenta_reader.parse_line`), and the console drains the whole queue each 50 ms tick. **Next: flash + power-cycle + run the 4 gates in `Firmware_SMASensorHub_PIO/STATUS.md`** (cadence → no drops → V/I means → idle telemetry). Rollback if needed: `pio run -e portenta_m7_legacy100 -t upload`.
- [ ] **Bench-run the 1 kHz stream** and confirm the payoff: ~96 src=3/4/5 points inside each 100 ms fire (was 8), `dropped`/`crc_err` still 0, and V/I means ~5% **lower** than the old sessions (the duty error going away — see the `+7%` item below). Then re-run the transition analysis (`lib_analysis`): with ~96 points per fire the per-cycle ΔR error bar should shrink enough to see the transition **within a single cycle**, instead of only across the 10-cycle ensemble.
- [ ] **Improve SMA current-sense precision** (this, not sampling rate, is what limits R). σ(I)/I = 5.66% contributes **91%** of the 6.3% noise on R; corr(ΔV, ΔI) = +0.02 proves it is *measurement* noise, not real drive fluctuation. ~~σ(I) = 138 ADC LSB **after 64× averaging** — so the interferer is low-frequency and the back-to-back averaging loop samples it at the same phase 64× and cancels nothing.~~ **CORRECTED 2026-08-01 — that was wrong, and it changes which fix is worth doing.** Measured on a 16 s idle segment (996 Hz, 108.1 mA, sd 22.2%), the noise is **broadband and essentially white**, with no coherent interferer at all: 0.1% of variance below 1 Hz, 2.1% in 1–10 Hz, **70% above 75 Hz**, and the strongest single line is 356 Hz carrying 0.11%. Block averaging follows **1/√N to within a few percent** (N=4 → 12.00 mA against ideal 11.98; N=64 → 2.89 vs 3.00; N=256 → 1.38 vs 1.50). So averaging *does* work — this joins the other two numbers from the contaminated 2026-07-28 phase already flagged above. **Consequence: option (a) is pointless** — there is no hum to null, and spreading the reads buys nothing that plain averaging doesn't. Prefer averaging **on the host**, where it is free and unbiased; averaging in firmware raises ADC conversion duty and therefore the reference droop (`V = 0.01508 × duty% + 2.988`, R² = 0.9996), trading random noise for systematic bias. Options, now: ~~(a) spread the 64 reads over a window~~ **(dead — noise is white)**; (b) **raise the current-sense scale** — now `INA_GAIN 10 × 0.1 Ω = 1.0 V/A`, so the fire peak reaches only **23%** of ADC range and the idle probe just **3.7%**; (c) **move V/I onto the ADS1263** (AIN6–AIN9 are free) — the structural fix. **NOTE: the rate is NOT the problem** — and as of the 2026-07-13 1 kHz port it is even less so (~962 Hz, ~96 points/fire, was 88 Hz / ~8). Rate buys transient **shape**, not resistance **precision**: every one of those 96 points still carries the same ~6% noise, and it comes from the current-sense front end. ~~Option (a) is *unaffected* by the port~~ — dead, see the correction above; **(b) and (c) are the real levers**, and they are the only ones, because they raise SNR *per sample*. Averaging cannot substitute: it buys √N per unit **time**, so at the ~107 mA idle bias 24% per-sample becomes ~2.3% at 1 s and ~1% at 5 s. Fine between pulses where there are 30 s to spend; useless if R is ever wanted at 10 ms resolution inside a short pulse.

  **Do NOT "sample slower" to fix this — both ADCs get worse, not better.** The
  H7 would trade random noise for the duty-droop bias above. The ADS1263 is
  more subtle: it runs 400 SPS against a ~1 Hz mechanical signal, so lowering
  it is tempting, but the laser carries a **65.8 Hz instrumental tone** (74% of
  that channel's variance) and below ~132 SPS it folds into the signal band —
  at 60 SPS it aliases to 5.8 Hz, right where the actuation lives. Sinc3
  notches sit at multiples of the data rate, so 65.8 Hz lands between them and
  is attenuated, not nulled. Decimate in post, after notching the tone, if the
  ENOB is ever wanted.
- [ ] **The laser may not be measuring anything** (higher priority than the tone). In `console_20260713_115921` the real SMA displacement sits **below the laser noise floor** — only the *force* clearly responds to firing, and the only laser excursion is the drive feedthrough (see below). Establish whether the IL-030 is resolving real SMA contraction at all before investing in noise cleanup.
- [ ] **LASER DRIVE FEEDTHROUGH during a fire — the one that can produce a WRONG RESULT.** The laser steps **+3.2…+3.6 µm inside every fire window** and returns to 0 ± 0.3 µm by +0.25 s, exactly when the force peaks; on a cycle where the force was 8× larger the step was unchanged. It tracks the 0.7 A drive, not the mechanics. Unlike the 65.8 Hz tone this is **synchronous with the actuation**, so **no frequency filter can remove it** — it lands precisely where the displacement signal should be and can be mistaken for SMA contraction. the cycles view flags it (`lib_analysis`; notebook port TODO). Fix at the source (shielding / grounding / routing of the laser signal away from the drive loop).
- [ ] **H7 over-read: ~19% of `h7.csv` rows are zero-order-hold duplicates** (found 2026-07-13). The stream is read at **492.85 Hz** while the ADS1263 converts at **400 SPS**, so ~1 row in 5 on **both** laser and load is an exact repeat of the previous conversion (unique-sample rate = 400.7 Hz, matching the config). Effective rate is **400 Hz, Nyquist 200 Hz** — row-count-derived rates/σ/spectra are off by ~23%. Fix at the source (read on DRDY / drop the repeat) or decimate host-side on `diff(raw_code)==0`. Related: analysis must use **`hw_us`, not `host_timestamp_s`** — host timestamps are USB-batched (median dt 1.06 ms, σ 3.4 ms) and smear any spectrum enough to hide the tone entirely.
- [x] ~~**SMA scripted actuation**~~ — done 2026-06-21: recorder drives the on-M7 `cycle` state machine (params + heartbeat) when `sma.enabled`. Bench-verify the heat/cool timing + watchdog.
- [ ] **Bench-verify manual stage motion** — home + go buttons issue reliable
  commands now that the driver serializes serial I/O (`_serial_lock`); confirm on
  the rig that a go-to lands where expected and no commands are dropped.
- [ ] **Confirm stage home direction** — see `Driver_ZaberStage/diag_home.py`;
  the console home appeared to be on the opposite end vs Zaber Launcher.
- [ ] **Scripted STAGE profile** — optional RAW-phase stage motion (recorder-driven, still deferred; manual motion is now available).
- [ ] Flip to **Stable** once a real session records cleanly and the analyzer produces a sensible dashboard.

See [../README.md](../README.md) for the project map.
