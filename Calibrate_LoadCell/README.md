# Calibrate_LoadCell — Master Document

> **Last updated:** 2026-05-27
>
> **Status:** Code complete (firmware + Python pipeline). Not yet bench-verified.
>
> **Goal:** Establish the voltage-to-force transfer function for the
> LCA-9PC load cell amplifier → ADS1263 signal chain. A characterised BMX
> spring (k ≈ 30.86 mN/mm from Instron) serves as the transfer standard;
> the Zaber linear stage provides ground-truth displacement. A static
> sweep produces the linear fit V(F) = sensitivity · F + V₀ whose slope
> (mV/mN) is the calibration constant that propagates into
> `Experiment_SMACharacterizationV2/` for production use.

This single document covers the full module: hardware, firmware, Python
pipeline, procedure, analysis, and troubleshooting.

---

## 1. What and Why

The load cell (via the Omega LCA-9PC amplifier) outputs 0–5 V proportional
to force. The exact sensitivity depends on the amplifier gain setting, load
cell characteristics, and the ADC signal chain. This calibration measures
that sensitivity on the actual rig hardware so downstream code can convert
voltages to Newtons.

Because the load cell is mounted sideways, gravity-based dead-weight
calibration isn't practical. Instead we use a **spring transfer standard**:
the BMX spring was characterised on an Instron 2530-100N cell
(k = 30.86 mN/mm, R² = 0.99995, 0.4% uncertainty). The Zaber stage pulls
the spring through a known displacement range; force at each point is
F = k × Δx. The fit of voltage vs. force gives the load cell's sensitivity.

A secondary benefit: the dual-ADC cross-compare (ADC1 and ADC2 both reading
AIN2/AIN3 simultaneously) catches ADC-specific driver bugs and register-config
errors that a single-channel measurement cannot.

---

## 2. Hardware Setup

### 2.1 Components

- **Load cell + amplifier:** Omega LCA-9PC, 0–5 V output.
- **Spring:** BMX spring, k ≈ 30.86 mN/mm (Instron-characterised, data in
  `doc/spring loadcell/`).
- **Stage:** Zaber X-LSQ300A-E01 linear stage (COM5), provides ground-truth
  displacement.
- **ADC:** Bare TI ADS1263 EVM, connected to Portenta H7 on the Mid Carrier
  (ASX00055) via SPI (Cable 1 in `doc/MEMO_cable_map.md`).
- **Reference:** External TI REF7050 (+5 V) on AIN0/AIN1 (Cable 2).
- **Host PC:** Windows, running the Python scripts in this directory.

### 2.2 Wiring

| Signal | EVM terminal | Cable | Notes |
|---|---|---|---|
| LCA-9PC analog out (+) | AIN2 | Cable 3 | Amplifier voltage output |
| LCA-9PC signal ground (−) | AIN3 | Cable 3 | True differential for ground-loop rejection |
| REF7050 (+5 V) | AIN0 (+) | Cable 2 | External reference, shared by both ADCs |
| REF7050 ground | AIN1 (−) | Cable 2 | |

Full cable map: `doc/MEMO_cable_map.md`.

### 2.3 Spring Specifications (from Instron)

| Parameter | Value | Source |
|---|---|---|
| Stiffness k | 30.86 mN/mm (pooled, 4 runs) | `doc/spring loadcell/spring_loadcell_summary.csv` |
| Run-to-run CV | 0.26% | |
| Linearity R² | 0.99995 | |
| Intercept | −1.85 mN (≈ 0, no initial tension) | |
| Usable range | 0–34 mm / 0–1050 mN | |

### 2.4 COM Port Assignments (Current Machine)

| Device | Port | Notes |
|---|---|---|
| Portenta H7 | COM8 | Arduino CDC, VID:PID = 2341:025B |
| Zaber X-LSQ300A-E01 | COM5 | FTDI bridge, VID:PID = 0403:6001 |

Pre-filled in `config.yaml` and `platformio.ini`.

---

## 3. Firmware — Calibrate_Loadcell_PIO

### 3.1 Architecture

The firmware lives in `Calibrate_Loadcell_PIO/` within this directory. It is
a self-contained PlatformIO project, separate from `Firmware_SensorHub_PIO/`
(production dual-stream). This keeps calibration-specific behaviour (dual-ADC
cross-compare on the load cell channel) isolated from production firmware.

Both cores compile from the same `src/main.cpp` using `#ifdef CORE_CMx` guards:

- **M7 core:** Boots first, drains the shared-memory ring buffer and formats
  TSV for USB Serial at 115200 baud.
- **M4 core:** Drives the ADS1263 via SPI, polls both ADCs at 2 ms intervals,
  pushes samples into the lock-free ring buffer in SRAM4.

### 3.2 ADC Configuration

Both ADCs sample AIN2(+) / AIN3(−) simultaneously:

| | ADC1 (primary) | ADC2 (cross-check) |
|---|---|---|
| Resolution | 32-bit | 24-bit |
| Rate | 400 SPS | 400 SPS |
| Filter | Sinc3 | Sinc3 |
| PGA | In path, gain = 1 | Unity buffer, gain = 1 |
| Reference | REF7050 on AIN0/AIN1 | REF7050 on AIN0/AIN1 |
| MUX register | INPMUX = 0x23 | ADC2MUX = 0x23 |

ADC1 is the primary channel — its 32-bit resolution makes it the natural
choice for the low-bandwidth, precision-critical load cell signal.

Build-time flags in `main.cpp`:
- `ENABLE_ADC1 = 1` — primary channel on (default)
- `ENABLE_ADC2 = 1` — cross-compare on (default)
- Set `ENABLE_ADC2 = 0` to revert to single-ADC mode (3-column stream).

### 3.3 Output Stream Format

Tab-separated, one line per sample:

```
<t_ms>\t<src>\t<raw_code>\t<voltage_V>
```

- `src = 1` → ADC1, `src = 2` → ADC2
- ~800 lines/s total (400 SPS per ADC)

In single-ADC mode the `src` column is dropped. The `portenta_reader.py`
parser handles both formats transparently.

### 3.4 IPC: Ring Buffer

The M4 → M7 data path uses a lock-free SPSC ring buffer in SRAM4
(`sample_ring.h`), replacing the synchronous RPC.print() path that caused
mid-run crashes at ~660 msg/s. The ring holds 1024 samples (~1.3 s at 800
SPS combined). RPC is retained only for boot-time checkpoint messages.

### 3.5 Flash Procedure

```bash
cd Calibrate_Loadcell_PIO

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
the EVM's analog rails. The ADS1263 will come up with ID=0x00 if you skip it.

At boot you should see:

```
[M7] bridge up — ring-buffer IPC, forwarding to USB Serial (Calibrate_LoadCell)
[M4] *** Calibrate_Loadcell_PIO — dual-ADC cross-compare on AIN2/AIN3 ***
```

---

## 4. Python Pipeline

### 4.1 Directory Layout

```
Calibrate_LoadCell/
├── Calibrate_Loadcell_PIO/    # Firmware (PlatformIO project)
│   ├── src/main.cpp
│   ├── src/sample_ring.h
│   ├── platformio.ini
│   └── lib/ADS1263/
│       ├── ADS1263_Driver.h
│       └── ADS1263_Driver.cpp
├── portenta_reader.py          # Serial parser (shared with Calibrate_LaserHead)
├── run_calibration.py          # Sweep orchestrator
├── analyze.py                  # Offline fit + SVG plot + calibration.json
├── config.yaml                 # Experiment parameters
├── calibration.json            # Output: calibrated sensitivity (written by analyze.py)
├── requirements.txt            # pyserial, numpy, matplotlib, pyyaml, zaber-motion
├── README.md                   # This file
└── data/                       # Per-run outputs (timestamped)
```

### 4.2 Install

```bash
python -m pip install -r requirements.txt
```

The scripts import `zaber_stage.py` from `../Driver_ZaberStage/` via a `sys.path`
shim in `run_calibration.py`.

### 4.3 portenta_reader.py

Provides the `PortentaReader` class and the `Sample` dataclass. Accepts
3-column TSV, 4-column TSV, and the plan-spec CSV format. Key methods:

- `open()` / `close()` — serial lifecycle with firmware boot detection
- `drain()` — discard stale samples before a capture
- `read_samples(n)` — single-channel capture (ADC1 primary)
- `read_samples_dual(n_per_adc)` → `(samples_adc1, samples_adc2)` — dual capture

### 4.4 run_calibration.py

Orchestrates the full calibration sweep:

1. Connect to Portenta (USB-CDC) and Zaber stage
2. Home stage if needed, move to `sweep_start_mm`
3. Collect baseline samples at start (pre-sweep thermal state)
4. For each pass (1..N):
   - For each target displacement (forward, then reverse if bidirectional):
     - Move stage → wait settle → drain serial → capture 500 samples
     - Compute expected force: F = spring_k × displacement
     - Write raw rows + one aggregate points row
5. Collect baseline at start (post-sweep drift check)
6. Write `points.csv`, `raw.csv`, `meta.json`

### 4.5 analyze.py

Offline fitting and validation:

- **Auto-detection:** With no argument, finds the latest `*_points.csv` in `data/`.
- **Linear region detection:** Iteratively trims low-force points from the soft
  zone (where the spring isn't fully engaged) until R² > 0.9999. Reports how
  many points were excluded and where the linear region starts.
- **Fit:** V(F) = sensitivity × F + V₀ via `numpy.polyfit`.
- **Sanity checks:** R², residual shape, sigma constancy, baseline drift,
  sensitivity sign.
- **Decompositions:** Per-pass (repeatability), per-direction (hysteresis),
  cross-compare (ADC1 vs ADC2 agreement).
- **Outputs:** Console report + `<prefix>_fit.svg` + `calibration.json`.
  Optional `--json-out` for machine-readable results.

### 4.6 calibration.json

Written by `analyze.py` after every successful run. This is the canonical
file that downstream modules read:

```json
{
  "sensitivity_mV_per_mN": 1.2345,
  "sensitivity_V_per_N": 1.2345,
  "V0_mV": 12.34,
  "r_squared": 0.999987,
  "spring_k_mN_per_mm": 30.86,
  "conversion": "force_mN = (V_mV - V0_mV) / sensitivity_mV_per_mN",
  ...
}
```

Import from other modules:

```python
from Calibrate_LoadCell.analyze import load_calibration
cal = load_calibration()
sensitivity = cal["sensitivity_mV_per_mN"]
v0 = cal["V0_mV"]
```

### 4.7 config.yaml — Key Parameters

**Spring:**

| Parameter | Default | Notes |
|---|---|---|
| `spring_k_mN_per_mm` | 30.86 | Instron pooled value |
| `spring_k_uncertainty_pct` | 0.4 | Combined run scatter + cell spec |

**Sweep geometry:**

| Parameter | Default | Notes |
|---|---|---|
| `sweep_start_mm` | 10.0 | Absolute Zaber position, a few mm before spring engagement (bench-verified) |
| `max_force_gf` | 45 | Target ceiling (~441 mN, ~14.3 mm travel) |
| `step_size_mm` | 0.5 | ~29 points over the range |
| `direction` | bidirectional | Forward + return for hysteresis |
| `passes` | 2 | Repeat for pass-to-pass repeatability |

**Sampling:**

| Parameter | Default | Notes |
|---|---|---|
| `samples_per_point` | 500 | ~1.25 s at 400 SPS |
| `settle_time_s` | 1.5 | Mechanical ringdown after stage move |
| `baseline_samples` | 500 | Pre- and post-sweep thermal reference |

**Dry-run overrides** (activated via `--dry-run`):
- `max_force_gf`: 5 (~1.6 mm, ~3 points)
- `direction`: forward_only, 1 pass
- `samples_per_point`: 20

---

## 5. Calibration Procedure

### 5.1 Pre-Checks

1. **Flash the firmware** (§3.5). Verify boot banner says
   `Calibrate_Loadcell_PIO`.
2. **Wiring check:** LCA-9PC → AIN2/AIN3, REF7050 → AIN0/AIN1. See §2.2.
3. **LCA-9PC warm-up:** Per the amplifier manual, allow **30 minutes** of
   powered warm-up before calibration measurements.
4. **Spring mounting:** Confirm the spring is physically connected between the
   load cell and the stage carriage. The spring should be near its relaxed
   length at `sweep_start_mm`.
5. **Zaber safety limits:** `../Driver_ZaberStage/safety_config.json`
   `position_limits_mm` must permit the full sweep range.

### 5.2 Determine sweep_start_mm

This is the one parameter you must set empirically. Jog the stage manually
until the spring is just barely relaxed (no tension on the load cell). Note
the Zaber position. Set `sweep_start_mm` in `config.yaml` a few mm below that
value — the sweep should start before engagement so `analyze.py` can
auto-detect the transition into the linear region.

### 5.3 Smoke Test

Confirm the ADC stream is healthy before involving the stage:

```bash
python portenta_reader.py --port COM8 --duration 30
```

Expect ~400 SPS per ADC (~800 lines/s total), monotonic timestamps, both
`src=1` and `src=2` lines showing approximately the same voltage.

### 5.4 Dry Run

Validates the full pipeline end-to-end in ~1 minute:

```bash
python run_calibration.py --dry-run
```

Check that the stage moved, samples were captured, and output files appeared
in `data/`.

### 5.5 Full Calibration Run

```bash
python run_calibration.py
```

With default settings: ~29 points, bidirectional, 2 passes. Wall time is
dominated by settle windows — expect roughly 20–30 minutes.

Update `operator` and `notes` in `config.yaml` before running.

### 5.6 Analyse

```bash
python analyze.py
```

No argument needed — it auto-detects the latest run in `data/`. Prints the
fit report, writes `<prefix>_fit.svg` and updates `calibration.json`.

For an explicit file:

```bash
python analyze.py data/2026-05-28_run01_points.csv
```

---

## 6. Analysis and Expected Results

### 6.1 The Fit

```
V(F) = sensitivity × F + V₀
```

where F is the expected force in mN and V is voltage in mV.

- **sensitivity** = mV/mN (the headline calibration constant)
- **V₀** = voltage at zero force (re-measured each production session)

### 6.2 Soft-Zone Handling

The sweep starts before the spring is fully engaged. The first few points
may show a "soft zone" where voltage doesn't track force linearly (contact
settling, slack take-up). `analyze.py` automatically detects and excludes
these points by iteratively trimming from the low-force end until
R² > 0.9999. The report shows how many points were trimmed and where the
linear region begins.

### 6.3 Cross-Compare (ADC1 vs ADC2)

When `xcompare: true`, both ADCs are fitted independently. The agreement
metric is `|sens_adc1 − sens_adc2| / mean(|sens|)`.

What cross-compare catches: ADC2 driver bugs, register-config errors,
asymmetric digital-path handling. What it does not catch: REF7050 error,
front-end wiring, LCA-9PC amplifier issues, spring stiffness error.

### 6.4 Sanity Checks

1. **R² > 0.9999** in the linear region.
2. **Residuals random, not S-shaped.**
3. **Per-point σ approximately constant** across the sweep.
4. **Baseline drift within noise** (pre vs. post).
5. **Sensitivity is positive** (force up → voltage up for LCA-9PC).

### 6.5 Accuracy Budget

The dominant uncertainty is the spring stiffness (~0.4% from Instron
characterisation). This propagates directly into the load cell sensitivity.
ADC noise (~1.3 µV RMS at 400 SPS / gain=1) and 500-sample averaging
contribute negligibly. The spring's linearity (R² = 0.99995) adds a small
systematic that washes out across the fit.

---

## 7. Troubleshooting

**"no samples captured at displacement x mm"** — Serial stream went silent.
Portenta reset or USB cable jostled. Re-run.

**Soft zone is very large (>30% of sweep trimmed)** — The spring isn't
engaging cleanly. Check: is the spring seated against the load cell at the
start position? Decrease `sweep_start_mm` to start further back, or add a
small preload by increasing `sweep_start_mm` past the engagement point.

**R² stays below 0.9999 even after trimming** — The load cell or amplifier
may be nonlinear over this range, or the spring has a nonlinear region. Try
reducing `max_force_gf` to stay in a narrower, more linear window.

**Sensitivity is negative** — The wiring polarity on AIN2/AIN3 is reversed
relative to the force direction. Swap the AIN2 and AIN3 connections, or
adjust the spring/stage geometry so that increasing stage position increases
force.

**Baseline drift is large** — The LCA-9PC wasn't warmed up (needs 30 min),
or the room temperature changed during the sweep. Let the setup equilibrate
and re-run.

**ADS1263 comes up with ID = 0x00** — Forgot to power-cycle after flashing.
Power-cycle the rig (USB + EVM supply) and try again.

**Zaber aborts at the first move** — `safety_config.json` position limits
are too narrow. Widen to permit the full sweep range.

---

## 8. Post-Calibration Integration

Once a good run is obtained:

1. `calibration.json` is automatically updated by `analyze.py` — downstream
   modules can read it directly.
2. Propagate the sensitivity into `Experiment_SMACharacterizationV2/` config if needed.
3. V₀ is informational here — it drifts with temperature and amplifier
   state, so production code should re-measure it at the start of each
   session.
4. Record the run in §10 below.

---

## 9. Known Gotchas

- **Spring stiffness is the ground truth.** The 0.4% uncertainty from the
  Instron propagates directly into the load cell sensitivity. There is no
  independent weight-check step because the load cell is mounted sideways.
- **REF7050 verification:** Cross-compare doesn't catch reference voltage
  errors — both ADCs share the same external reference and drift together.
  REF7050 is a precision 5.000 V reference (M-grade, 0.05% initial
  accuracy per `doc/MEMO_cable_map.md` Cable 2), so the firmware uses
  `vref_V = 5.0f` in `main.cpp` and `analyze.py` math derives volts from
  that. Re-verify with a bench multimeter across AIN0(+) and AIN1(−) on
  the EVM screw terminals — it should read **+5.000 V ± a few mV**. If it
  doesn't, the REF7050 is faulty or the bias resistors (100 nF cap + 100
  kΩ pull, see Cable 2) are missing/shorted.

- **Do NOT confuse REF7050 with AVDD.** `ADS1263_FirstPowerUp_PIO/` cp10
  measured the EVM's **AVDD analog supply rail** at **5.2056 V** (via the
  ratiometric TDAC method — see `ADS1263_FirstPowerUp_PIO/STATUS.md` and
  the `ads1263_tdac_ratiometric` memory note). AVDD comes from the EVM's
  on-board TPS7A4700 LDO and is independent of REF7050. **AVDD does not
  enter this calibration's volts-per-code math** — that math is purely
  `V_in = (code / 2^31) × VREF` with VREF = REF7050 = 5.000 V. AVDD only
  matters for: (a) confirming the PGA input common-mode window
  `[0.3, AVDD−0.3]` is satisfied, and (b) any future ratiometric TDAC
  diagnostic. **Never substitute 5.2056 V for VREF in firmware or
  analysis** — doing so introduces a ~4.1% systematic error into the
  load cell sensitivity.
- **Driver sync:** The ADS1263 driver in `Calibrate_Loadcell_PIO/lib/ADS1263/`
  is a copy from `Calibrate_LaserHead_PIO/lib/ADS1263/` as of 2026-05-27.
  If new fixes land in Firmware_SensorHub_PIO, copy them here too.
- **COM port drift:** If the Portenta renumbers after a USB replug, update
  COM8 in both `config.yaml` and `Calibrate_Loadcell_PIO/platformio.ini`.

---

## 10. Run History

| Date | Run | Sensitivity (mV/mN) | V₀ (mV) | R² | Trimmed | Notes |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | Pending first bench run on current hardware. |
