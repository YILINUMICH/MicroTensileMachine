# Calibrate_LaserHead — Master Document

> **Last updated:** 2026-05-27
>
> **Status:** Code complete (firmware + Python pipeline). Not yet bench-verified on the EVM.
>
> **Goal:** Establish the voltage-to-displacement transfer function for the
> IL-030 → ADS1263 signal chain. The Zaber linear stage provides ground-truth
> displacement; a static sweep produces the linear fit V(x) = k·x + V₀ whose
> slope k (mV/µm) and offset V₀ are the calibration constants that propagate
> into `SMA_CharacterizationV2/` for production use.

This single document replaces the previous `Calibrate_LaserHead_Plan.md`,
`MEMO_session_2026-05-26.md`, `STATUS.md`, and the old `README.md`.

---

## 1. What and Why

The Keyence IL-030 laser displacement sensor outputs 0–5 V over a 10 mm
measurement window. The datasheet gives a nominal sensitivity of **0.5 mV/µm**,
but the actual value on our signal chain (sensor → cable → ADS1263 EVM input →
ADC2 with external REF7050) needs to be measured, and the voltage offset V₀ at
the reference distance is rig-specific.

This calibration answers two questions:

1. **What is k (mV/µm) on this specific rig?** Should land within ~5% of the
   0.5 mV/µm nominal. Larger deviation means something in the signal chain
   (wiring, reference voltage, IL-030 range setting) needs investigation.
2. **What is V₀ (mV) at the reference distance?** Needed so downstream code
   can compute displacement = (V − V₀) / k.

A secondary benefit: the dual-ADC cross-compare (ADC1 and ADC2 both reading
AIN4/AIN5 simultaneously) catches ADC2-specific driver bugs and register-config
errors that a single-channel measurement cannot.

---

## 2. Hardware Setup

### 2.1 Components

- **Laser head:** Keyence IL-030, fixed-mounted on the rig frame.
- **Target:** Diffuse reference plate on the Zaber X-LSQ300A-E01 carriage, beam
  axis parallel to stage motion axis.
- **ADC:** Bare TI ADS1263 EVM, connected to the Portenta H7 on the Mid
  Carrier (ASX00055) via SPI (Cable 1 in `doc/MEMO_cable_map.md`).
- **Reference:** External TI REF7050 (+5 V) on AIN0/AIN1 (Cable 2).
- **Host PC:** Windows, running the Python scripts in this directory.

### 2.2 Wiring

| Signal | EVM terminal | Cable | Notes |
|---|---|---|---|
| IL-030 analog out (+) | AIN4 | Cable 4 | Keyence controller analog output |
| IL-030 signal ground (−) | AIN5 | Cable 4 | |
| IL-030 supply ground (0 V) | AVSS / GND | Separate wire | Do not rely on AIN5 alone for ground reference |
| REF7050 (+5 V) | AIN0 (+) | Cable 2 | External reference, shared by both ADCs |
| REF7050 ground | AIN1 (−) | Cable 2 | Should already be in place from SensorHub_PIO wiring |

Full cable map: `doc/MEMO_cable_map.md`.

### 2.3 IL-030 Specs

| Parameter | Value |
|---|---|
| Reference distance | 30 mm |
| Measurement range | ±5 mm (25–35 mm from sensor face) |
| Repeatability | 1 µm |
| Linearity | ±0.1% FS |
| Nominal sensitivity (0–5 V mode) | 0.5 mV/µm |

### 2.4 Stage-to-Sensor Mapping (Rig-Specific)

On this rig the IL-030 is mounted so that:

| Stage position (absolute) | IL-030 window | Output voltage |
|---:|---|:---:|
| 5 mm | High end of sensor window | **Maximum** |
| 10 mm | Reference distance | Mid |
| 15 mm | Low end of sensor window | **Minimum** |

Voltage **decreases** as stage position **increases** → the fit slope k is
**negative** by construction. When propagating constants downstream, keep k
signed so `displacement = (V − V₀) / k` recovers the correct physical
direction.

### 2.5 COM Port Assignments (Current Machine)

| Device | Port | Notes |
|---|---|---|
| Portenta H7 | COM8 | Arduino CDC, VID:PID = 2341:025B |
| Zaber X-LSQ300A-E01 | COM5 | FTDI bridge, VID:PID = 0403:6001; serial 143153 |

Pre-filled in `config.yaml` and `platformio.ini`. If ports change after
replugging, check `pio device list` and update both files.

---

## 3. Firmware — Calibrate_LaserHead_PIO

### 3.1 Architecture

The firmware lives in `Calibrate_LaserHead_PIO/` within this directory. It is
a self-contained PlatformIO project, separate from `LaserHead_PIO/`
(production laser-only) and `SensorHub_PIO/` (production dual-stream). This
keeps calibration-specific behaviour (dual-ADC cross-compare) isolated from
production firmware.

The Portenta H7 is a dual-core chip. Both cores compile from the same
`src/main.cpp` using `#ifdef CORE_CMx` guards:

- **M7 core:** Boots first, bridges RPC ↔ USB Serial at 115200 baud.
- **M4 core:** Drives the ADS1263 via SPI, polls both ADCs, streams readings
  to M7 via RPC.

### 3.2 ADC Configuration

Both ADCs sample AIN4(+) / AIN5(−) simultaneously:

| | ADC1 (cross-check) | ADC2 (primary / production) |
|---|---|---|
| Resolution | 32-bit | 24-bit |
| Rate | 400 SPS | 400 SPS |
| Filter | Sinc3 | Sinc3 |
| PGA | In path, gain = 1 | Unity buffer, gain = 1 |
| Reference | REF7050 on AIN0/AIN1 | REF7050 on AIN0/AIN1 |
| MUX register | INPMUX = 0x45 | ADC2MUX = 0x45 |
| REF register | REFMUX = 0x09 | REF2 = external AIN0/1 |

ADC2 is the production channel — its fit becomes the k / V₀ that propagates
to `SMA_CharacterizationV2/`. ADC1 is the independent digital-path cross-check.

Build-time flags in `main.cpp`:
- `ENABLE_ADC1 = 1` — cross-compare on (default for calibration)
- `ENABLE_ADC2 = 1` — primary channel on
- Set `ENABLE_ADC1 = 0` to revert to single-ADC mode (3-column stream).

### 3.3 Output Stream Format

Tab-separated, one line per sample:

```
<t_ms>\t<src>\t<raw_code>\t<voltage_V>
```

- `src = 1` → ADC1, `src = 2` → ADC2
- ~800 lines/s total (400 SPS per ADC)

In single-ADC mode (ENABLE_ADC1 = 0), the stream drops the `src` column:

```
<t_ms>\t<raw_code>\t<voltage_V>
```

The `portenta_reader.py` parser handles both formats transparently.

### 3.4 ADS1263 Driver Provenance

`lib/ADS1263/` was copied from `SensorHub_PIO/lib/ADS1263/` (via
`LaserHead_PIO/`) on 2026-05-26. It carries the Mid Carrier pin defines
(CS = PA_8, DRDY = PC_6, RESET = PC_7) and two critical bug fixes from
2026-05-25:

1. **RDATA2 6-byte frame:** ADC2 read was only taking 5 bytes; added the
   required zero-pad byte.
2. **ADC2CFG REF2/GAIN2 field swap:** The REF2 and GAIN2 bit fields were
   transposed in the register write, causing ADC2 to land on the wrong
   reference and misread above ~1.25 V differential.

If new driver fixes land in SensorHub_PIO, copy them here too.

### 3.5 Flash Procedure

```bash
cd Calibrate_LaserHead_PIO

# Step 1: Flash M7 bridge (one-time, or after firmware changes)
pio run -e portenta_m7_bridge -t upload
# Power-cycle the rig (USB + EVM supply)

# Step 2: Flash M4 application
pio run -e portenta_m4 -t upload
# Power-cycle the rig again

# Step 3: Verify
pio device monitor    # 115200 baud
```

**Power-cycle after every upload** — the DFU reset does not cleanly re-power
the EVM's analog rails (the on-board TPS7A4700 LDO needs a full power-on
transient). The ADS1263 will come up with ID=0x00 if you skip this.

At boot you should see:

```
[M7] bridge up — forwarding RPC to USB Serial (Calibrate_LaserHead)
[M4] *** Calibrate_LaserHead_PIO — dual-ADC cross-compare on AIN4/AIN5 ***
```

This banner confirms you flashed the calibration firmware, not
`LaserHead_PIO` or `SensorHub_PIO`.

---

## 4. Python Pipeline

### 4.1 Directory Layout

```
Calibrate_LaserHead/
├── Calibrate_LaserHead_PIO/   # Firmware (PlatformIO project)
│   ├── src/main.cpp
│   ├── platformio.ini
│   └── lib/ADS1263/
│       ├── ADS1263_Driver.h
│       └── ADS1263_Driver.cpp
├── portenta_reader.py         # Serial parser (open / drain / read_samples / read_samples_dual)
├── run_calibration.py         # Sweep orchestrator
├── analyze.py                 # Offline linear fit + sanity checks + cross-compare report
├── config.yaml                # Experiment parameters
├── requirements.txt           # pyserial, numpy, scipy, matplotlib, pyyaml, zaber-motion
├── README.md                  # This file
└── data/                      # Per-run outputs (git-ignored except .gitkeep)
```

### 4.2 Install

```bash
python -m pip install -r requirements.txt
```

The scripts import `zaber_stage.py` from `../ZaberStage/` via a `sys.path`
shim in `run_calibration.py`.

### 4.3 portenta_reader.py

Provides the `PortentaReader` class and the `Sample` dataclass:

```python
@dataclass
class Sample:
    timestamp_us: int
    voltage_V: float
    raw_code: Optional[int] = None
    adc_source: Optional[int] = None   # 1 = ADC1, 2 = ADC2 (dual-ADC mode)
```

Key methods:
- `open(port, baud)` — connect to Portenta USB-CDC
- `drain(max_time_s)` — discard stale samples in the serial buffer
- `read_samples(n, timeout_s)` — single-ADC capture
- `read_samples_dual(n_per_adc, timeout_s)` — returns `(samples_adc1, samples_adc2)`

Accepts both 3-column (single-ADC) and 4-column (dual-ADC) TSV formats.
Timestamps are converted from ms to µs internally.

### 4.4 run_calibration.py

Orchestrates the full calibration sweep:

1. Connect to Portenta (USB-CDC) and Zaber stage
2. Home stage if needed
3. Move to sweep center (10 mm absolute = IL-030 reference distance)
4. Collect baseline samples at home (pre-sweep thermal state)
5. For each pass (1..N):
   - For each target position (forward, then reverse if bidirectional):
     - Move stage → wait settle → drain serial buffer → capture samples
     - Write raw rows + aggregate one points row
6. Collect baseline samples at home (post-sweep thermal state)
7. Write `points.csv`, `meta.json`

Outputs per run (under `data/`, timestamped prefix):

| File | Contents |
|---|---|
| `<prefix>_raw.csv` | Every individual sample with target/actual/direction/pass/adc_source |
| `<prefix>_points.csv` | One row per position: mean_V, std_V, n_samples (+ ADC1 columns if xcompare) |
| `<prefix>_meta.json` | Firmware path, ADC config, stage info, baseline drift, xcompare settings |

### 4.5 analyze.py

Offline fitting and validation:

- Linear fit: V(x) = k·x + V₀ via `scipy.stats.linregress`
- 7 sanity checks (see §6.3 below)
- Per-pass and per-direction decomposition for repeatability / hysteresis
- Cross-compare report when xcompare data present (ADC2 vs ADC1 agreement)
- Outputs: console report + `<prefix>_fit.png` + optional `--json-out`

### 4.6 config.yaml — Key Parameters

**Sweep geometry:**

| Parameter | Default | Notes |
|---|---|---|
| `sweep_center_mm` | 10.0 | Absolute stage position at IL-030 reference distance |
| `sweep_range_mm` | [−5.0, +5.0] | Relative to center → visits 5–15 mm absolute |
| `step_size_mm` | 0.2 | 51 points over 10 mm |
| `direction` | bidirectional | Forward + return for hysteresis detection |
| `passes` | 2 | 2 full sweeps → 4 traversals (fwd₁, rev₁, fwd₂, rev₂) |

**Sampling:**

| Parameter | Default | Notes |
|---|---|---|
| `samples_per_point` | 500 | ~1.25 s at 400 SPS |
| `settle_time_s` | 1.5 | Mechanical ringdown after stage move |
| `baseline_samples` | 500 | Pre- and post-sweep thermal reference |
| `stage_velocity_mm_s` | 0.5 | Conservative speed |

**Cross-compare:**

| Parameter | Default | Notes |
|---|---|---|
| `xcompare` | true | Requires dual-ADC firmware. Set false for single-channel runs. |

**Dry-run overrides** (activated via `--dry-run` flag):
- `sweep_range_mm`: [−0.5, +0.5] (1 mm, 11 points)
- `direction`: forward_only
- `passes`: 1
- `samples_per_point`: 20

---

## 5. Calibration Procedure

### 5.1 Pre-Checks

Before any calibration run:

1. **Flash the firmware** (§3.5) if not already done. Verify the boot banner
   says `Calibrate_LaserHead_PIO`.
2. **Wiring check:** IL-030 → AIN4/AIN5, REF7050 → AIN0/AIN1, ground
   wired separately to AVSS/GND. See §2.2.
3. **Mechanical alignment:** Beam axis parallel to stage axis. Cosine error
   from misalignment compresses apparent sensitivity — the fit still looks
   clean (high R²) but k is biased low. Check against existing fixture marks.
4. **Stage at 10 mm → IL-030 "reference distance" LED lit.** If not, the
   mounting fixture has shifted — reseat sensor or re-zero stage.
5. **Jog to 5 mm and 15 mm** — confirm IL-030 output is valid at both
   extremes (no saturation, no out-of-range indicator).
6. **Zaber safety limits:** `../ZaberStage/safety_config.json`
   `position_limits_mm` must permit at least [5, 15]. The pre-2026-05-26
   default was [10, 40] which would abort the sweep at 5 mm.
7. **30-minute thermal soak** with the laser powered on before sampling.
   Baseline-pre vs baseline-post drift is the only way to catch thermal
   effects during the run; a cold start guarantees visible drift.

### 5.2 Smoke Test

Confirm stream is healthy before involving the stage:

```bash
python portenta_reader.py --port COM8 --duration 30
```

Expect ~400 SPS per ADC (~800 lines/s total), monotonic timestamps, both
`src=1` and `src=2` lines showing approximately the same voltage.

### 5.3 Dry Run

Validates the full pipeline end-to-end in ~1 minute:

```bash
python run_calibration.py --dry-run
```

Produces `data/<prefix>_raw.csv`, `_points.csv`, `_meta.json`. Check that the
stage moved, samples were captured, and the output files look reasonable.

### 5.4 Full Calibration Run

```bash
python run_calibration.py
```

With default `config.yaml` settings: 51 points × 10 mm, bidirectional,
2 passes, 500 samples/point. Wall time is dominated by settle windows and
stage moves — expect roughly 1.5–2 hours.

Update `operator` and `notes` in `config.yaml` before running so they land
in `meta.json`.

### 5.5 Analyse

```bash
python analyze.py data/<prefix>_points.csv
```

Prints sensitivity, offset, R², residual stats, and runs sanity checks.
Writes `<prefix>_fit.png`. Add `--json-out` for machine-readable results.

---

## 6. Analysis and Expected Results

### 6.1 The Fit

```
V(x) = k · x + V₀
```

where x is stage position in µm and V is the mean voltage in mV.

- **k** = sensitivity in mV/µm (the headline calibration constant)
- **V₀** = voltage offset at x = 0

Expected: **|k| ≈ 0.5 mV/µm** (negative on this rig per the stage-to-sensor
mapping). Larger than ~5% deviation from nominal warrants investigation of the
signal chain before trusting the number.

### 6.2 Cross-Compare (ADC1 vs ADC2)

When `xcompare: true`, both ADCs are fitted independently. The agreement
metric is `|k_adc2 − k_adc1| / mean(|k|)`.

What cross-compare catches: ADC2-specific driver bugs, ADC2 register-config
errors (wrong DR2/GAIN2, accidentally using internal 2.5 V reference instead
of REF7050), any asymmetric handling of the same physical signal.

What it does **not** catch: REF7050 voltage error (both ADCs share it),
front-end wiring errors at AIN4/AIN5, beam-axis cosine error, IL-030 sensor
issues. The Zaber stage remains the ground truth for displacement.

### 6.3 Sanity Checks

1. **|k| within ~5% of 0.5 mV/µm** — datasheet nominal for 0–5 V mode,
   10 mm range.
2. **R² > 0.9999** — within the IL-030 linear window.
3. **Residuals are random, not S-shaped** — S-shape means the sweep hits the
   nonlinear edges. Fix: shrink `sweep_range_mm` to e.g. [−3.0, +3.0].
4. **Per-point σ approximately constant** across the sweep — large variation
   suggests a grounding or shielding issue on the laser cable.
5. **Baseline drift small** — baseline_pre ↔ baseline_post delta ≪ per-point
   σ. Large drift → extend thermal soak time.
6. **Hysteresis < 1 µm** — forward vs return sweeps should agree within the
   IL-030 repeatability spec.
7. **Cross-compare agreement** (when xcompare on):
   - `|k_adc2 − k_adc1| / mean(|k|)` ≪ 1%
   - Per-point ΔV bias ≪ per-point σ

---

## 7. Troubleshooting

**"no samples captured at target x mm"** — Serial stream went silent during
the drain/read window. Usually means the Portenta was reset or USB cable was
jostled. Re-run.

**Residuals are S-shaped** — Sweep is hitting the nonlinear edges of the
IL-030 window. Shrink `sweep_range_mm` in `config.yaml` (try [−3.0, +3.0]).

**Per-point σ varies a lot** — Grounding or shielding issue on the laser
cable. Check with an oscilloscope on the IL-030 analog out before re-running.

**Baseline drift is large** — Room or sensor warmed up during the run. Let
the setup equilibrate for 30+ minutes with the laser on before re-running.

**ADS1263 comes up with ID = 0x00** — Forgot to power-cycle after flashing.
Power-cycle the rig (USB + EVM supply) and try again.

**Zaber aborts at the first move** — `safety_config.json` position limits are
too narrow. Widen `position_limits_mm` to at least [5, 15].

**k is positive** — On this rig k should be negative (voltage decreases as
stage position increases). A positive k means the IL-030 output polarity is
inverted from expected. Check the IL-030 front-panel polarity/slope setting
and verify AIN4/AIN5 wiring polarity.

---

## 8. Post-Calibration Integration

Once a good run is obtained:

1. Propagate k and V₀ into `SMA_CharacterizationV2/` defaults and the
   `laser_calibration_reference` block in `session.py`.
2. Keep k signed so `displacement = (V − V₀) / k` recovers the correct
   physical direction downstream.
3. Record the run prefix, date, and key results here in §9 below.

---

## 9. Run History

| Date | Run | k (mV/µm) | V₀ (mV) | R² | Signal path | Notes |
|---|---|---|---|---|---|---|
| 2026-04-24 | legacy | −0.1171 | 566.957 | 0.965 | Waveshare HAT → AIN0/1 → ADC1 | **Stale** — old HAT path, retired. Do not use. |
| — | — | — | — | — | EVM AIN4/5 → ADC2 | Pending first bench run on current hardware. |

---

## 10. Known Gotchas

- **REF7050 verification:** Cross-compare does not catch reference voltage
  errors (both ADCs share the same REF7050). The reference was measured at
  5.2056 V in `ADS1263_FirstPowerUp_PIO/` cp10. If that measurement is older
  than a month, re-verify with a bench multimeter before trusting absolute
  voltages.
- **Cosine error:** Beam-axis ↔ stage-axis misalignment compresses apparent
  sensitivity by cos(θ). The fit still looks clean (high R²) but k is biased
  low. Worth a physical alignment check before each run.
- **Driver sync:** The ADS1263 driver in `Calibrate_LaserHead_PIO/lib/ADS1263/`
  is a copy from `SensorHub_PIO/lib/ADS1263/` as of 2026-05-26. If new fixes
  land in SensorHub_PIO, manually copy them here.
- **COM port drift:** If the Portenta renumbers after a USB replug (or the rig
  moves to a different PC), update COM8 in both `config.yaml` and
  `Calibrate_LaserHead_PIO/platformio.ini`. Check `pio device list`.
