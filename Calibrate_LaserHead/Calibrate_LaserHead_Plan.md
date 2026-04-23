# LaserHead_PIO Calibration Plan — Option A (Static Sweep)

**Goal:** Determine the sensitivity of the IL-030 → ADS1263 (ADC2) signal chain in **mV/µm**, using the Zaber linear stage as the ground-truth displacement reference.

Secondary quantities extracted from the same run:
- Linearity (residuals from linear fit, in % of full scale)
- Noise floor at each position (σ in mV)
- Zero-offset at the reference position
- Bidirectional repeatability, if a return sweep is included

---

## 1. Hardware Setup

- **Laser head:** Keyence IL-030, mounted on fixed frame.
- **Target:** diffuse reference plate mounted on the Zaber stage carriage, aligned so the stage motion axis is parallel to the laser beam.
- **Signal chain:** IL-030 analog out → ADS1263 ADC2 (AVDD = 5 V as reference, 0–5 V input range, already validated).
- **Stage:** Zaber linear stage, homed, controlled via `zaber_stage` Python library.
- **Host:** PC running `run_calibration.py`, connected to Portenta over USB-CDC and to Zaber over USB/serial.

### IL-030 Specs (set the achievable sweep range)

| Parameter | Value |
|---|---|
| Reference distance | 30 mm |
| Near limit | 25 mm |
| Far limit | 35 mm |
| Measurement range | 10 mm (±5 mm around reference) |
| Repeatability | 1 µm |
| Linearity | ±0.1 % FS |
| Nominal sensitivity (0–5 V mode) | 0.5 mV/µm |
| Nominal sensitivity (±5 V mode) | 1.0 mV/µm |

### Standoff Setup (do this before homing the stage for the run)

1. With stage at home, mechanically position the sensor head so the target sits at **~30 mm standoff** — verify the IL-030 "reference distance" LED is lit.
2. Confirm you can manually jog the stage ±5 mm and the analog output stays valid at both extremes (no saturation, no "out of range" indicator).
3. The calibration sweep then runs ±4 mm around home, leaving 1 mm of margin at each end of the sensor window to stay clear of the nonlinear edges.

---

## 2. Serial Format (input from Portenta H7)

The Portenta streams one line per ADC sample over USB-CDC:

```
<timestamp_us>,<voltage_v>\n
```

- `timestamp_us` — microseconds since firmware boot (uint32)
- `voltage_v` — already converted in firmware from raw ADC counts to volts (float, full scale 0–5 V)

The voltage conversion happens on the Portenta side, so the host only needs to parse, not scale. No ADC counts are transmitted.

---

## 3. Experiment Parameters (`config.yaml`)

| Parameter | Default | Notes |
|---|---|---|
| `sweep_range_mm` | `[-4.0, +4.0]` | Relative to stage home (= 30 mm standoff). Bounded by IL-030's ±5 mm window with 1 mm margin. |
| `step_size_mm` | `0.2` | → 41 points over 8 mm; use 0.1 mm (81 points) if finer linearity mapping is needed |
| `direction` | `forward_only` | `forward_only` for first pass, `bidirectional` later for hysteresis |
| `settle_time_s` | `0.5` | Mechanical ringdown after move complete |
| `samples_per_point` | `100` | ~250 ms at 400 SPS — well within the 17-bit noise-free regime |
| `stage_velocity_mm_s` | `1.0` | Slow enough that settle_time is comfortably sufficient |

Keep these in `config.yaml` alongside `run_calibration.py`, **not** in `zaber_config.json` (which is hardware config, not experiment config).

---

## 4. Procedure

For each target position in the sweep:

1. Command `stage.move_to(target_mm)`.
2. Wait until `stage.is_moving()` returns `False`.
3. Sleep `settle_time_s` to let mechanical ringdown decay.
4. Drain the Portenta serial buffer (discard samples captured during motion).
5. Read `samples_per_point` fresh samples.
6. Record one row: `(target_mm, stage_actual_mm, mean_V, std_V, n_samples, timestamp_start, timestamp_end)`.

Before the sweep starts: collect a baseline block of ~500 samples at home position to characterize the static noise floor. Repeat the same baseline collection at the end to check for drift.

---

## 5. Data Outputs

One calibration run produces four files under `data/`, sharing a timestamped prefix:

- `YYYY-MM-DD_runNN_raw.csv` — every individual sample with its Portenta timestamp and the stage position it corresponds to. Large file, useful for re-analysis.
- `YYYY-MM-DD_runNN_points.csv` — one row per stage position with aggregates (mean, std, n). This is what `analyze.py` consumes.
- `YYYY-MM-DD_runNN_meta.json` — run metadata: stage serial + firmware, Portenta firmware git hash, ADC config (SPS, gain, reference), config.yaml contents, ambient conditions if logged, operator note.
- `YYYY-MM-DD_runNN_fit.png` — output of `analyze.py`: V vs position with fit line, plus residuals subplot.

The metadata JSON is what makes a result trustworthy six months later. Don't skip it.

---

## 6. Analysis — Computing Sensitivity

Fit a straight line to the `points.csv`:

$$V(x) = k \cdot x + V_0$$

where $x$ is stage position in µm and $V$ is the mean voltage at that position in mV.

| Quantity | Meaning |
|---|---|
| $k$ | **Sensitivity in mV/µm** — the headline number |
| $V_0$ | Offset at $x=0$ |
| $R^2$ | Overall linearity of the sensor-in-its-window |
| residuals | $V_\text{measured} - V_\text{predicted}$, plotted vs $x$ |
| max residual / FS | Linearity spec in % FS |

Implementation: `numpy.polyfit(x_um, v_mV, deg=1)` or `scipy.stats.linregress` — both sufficient. No weighting needed since σ at each point is roughly constant (verify this from the `points.csv` std column).

**Reference value:** IL-030 has a 10 mm measurement range with a 0–5 V analog output, giving a nominal sensitivity of **0.5 mV/µm**. The measured $k$ should land within ~5% of this if the signal chain is healthy. A larger deviation means something upstream (LCA-RTC gain, ADS1263 reference, wiring) warrants investigation before trusting the result.

---

## 7. Sanity Checks Before Trusting the Calibration

- Sensitivity within ~5% of datasheet nominal (0.5 mV/µm).
- $R^2 > 0.9999$ within the IL-030 linear range.
- Residuals vs position look random, not S-shaped. An S-shape means the sweep is running into the nonlinear edges of the sensor window — shrink the range.
- Per-point σ is approximately constant across the sweep and near the ADS1263 noise floor at your chosen SPS.
- Baseline at home before and after the sweep matches within noise → no thermal drift corrupted the run.
- Stage reports `is_homed == True` for the entire sweep.

---

## 8. File Deliverables for This Phase

```
Calibrate_LaserHead/
├── portenta_reader.py      # serial parser class (open/close/read_sample/drain)
├── run_calibration.py      # orchestrator — the script you run
├── analyze.py              # offline: load points.csv → fit → plot
├── config.yaml             # experiment parameters
├── requirements.txt        # pyserial, numpy, scipy, matplotlib, pyyaml
├── README.md               # how to run, expected outputs
└── data/                   # raw + points + meta + fit, per run
```

---

## 9. Build Order

1. **`portenta_reader.py` first**, verified standalone with a 30-second dump test — confirm timestamps are monotonic and voltage values are sane before any stage motion.
2. **`run_calibration.py` without analysis** — just sweep and save the three data files. Runs end-to-end with no fitting.
3. **Dry run:** 10 points over 1 mm to validate the full pipeline end-to-end.
4. **Full calibration run** with target parameters.
5. **`analyze.py`** — written against already-saved `points.csv`, so it can be developed and iterated without touching hardware.
6. **Iterate parameters** if residuals or noise look off: shrink range, increase N, tune settle time.

---

## 10. Open Questions to Resolve Before First Run

- Exact IL-030 standoff and orientation — confirm target stays inside the linear window across the full sweep range.
- Is there an LCA-RTC in this chain, or does ADC2 read IL-030 directly? Affects the expected nominal sensitivity.
- Does the existing `LaserHead_PIO` firmware already stream in the `timestamp_us, voltage_v` format, or does it need a small addition (e.g. a `STREAM`/`STOP` command, or a dedicated build flag) to produce clean machine-parseable output?
