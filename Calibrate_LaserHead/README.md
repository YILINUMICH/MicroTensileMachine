# LaserHead Calibration — Run Book

Implements the procedure in `Calibrate_LaserHead_Plan.md`. This directory
contains everything the host PC needs; nothing here modifies firmware.

## Layout

```
Calibrate_LaserHead/
├── portenta_reader.py      # serial parser (open / drain / read_samples)
├── run_calibration.py      # orchestrator — the script you run
├── analyze.py              # offline fit + plot for a saved points.csv
├── config.yaml             # experiment parameters (see plan §3)
├── requirements.txt        # pyserial, numpy, scipy, matplotlib, pyyaml, zaber-motion
├── README.md               # this file
└── data/                   # per-run outputs land here (git-ignored except .gitkeep)
```

## Install

From this directory:

```bash
python -m pip install -r requirements.txt
```

The scripts import `zaber_stage.py` from `../ZaberStage/` via a `sys.path`
shim in `run_calibration.py`, so there is no need to install the Zaber
module separately.

## Physical setup (plan §1)

**Wiring update** — with the ADC1/AIN0-AIN1 routing above, connect:

- IL-030 analog signal out → **HAT screw terminal AIN0**
- IL-030 sensor ground     → **HAT screw terminal AIN1**
- IL-030 supply ground (0 V) → **HAT AVSS / GND** (separate wire — do
  not rely on AIN1 alone to define the ground reference)

1. Mount the IL-030 on the fixed frame; mount the diffuse reference plate on
   the Zaber carriage, aligned so the stage axis is parallel to the laser
   beam.
2. With the stage at home, mechanically position the sensor so the target
   sits at **~30 mm standoff**. The IL-030 "reference distance" LED must be
   lit.
3. Manually jog ±5 mm and confirm the analog output stays valid at both
   extremes (no saturation, no "out of range").
4. The sweep runs ±5 mm around absolute stage position 30 mm (the
   IL-030 reference distance), i.e. stage positions 25 mm → 35 mm. This
   is the full IL-030 measurement window. If the edges look nonlinear
   in the fit, shrink `sweep_range_mm` in `config.yaml` to e.g. ±4 mm.

## Firmware prerequisite

**Current routing (temporary):** the laser head signal is read via
**ADC1 on AIN0/AIN1** (through the HAT's load-cell front-end). This is
because the ADC2 path (AIN2/AIN3) saturates under any non-zero input on
this particular HAT and is being skipped until the root cause is
resolved. Flash `../SensorHub_PIO/` with `ENABLE_ADC1 = 1` and
`ENABLE_ADC2 = 0` (the current state of its `src/main.cpp`).

The HAT's AIN0/AIN1 front-end applies a scale factor (measured ~4.4×
attenuation during bring-up with a 1.6 V battery). The calibration fit
will absorb this into the measured sensitivity `k`, so the analysis
still produces a valid line — just with a different `mV/µm` than the
IL-030 datasheet's 0.5 mV/µm nominal. The plan §7 sanity check against
0.5 mV/µm nominal will FAIL and that's expected for this configuration;
what matters is that R² is high and residuals are random.

Confirm via a serial monitor that you see streaming lines like:

```
<t_ms>\t<raw_code>\t<voltage_V>
```

interleaved with a few `[M4]`/`[M7]` banner lines at boot. The
`portenta_reader.py` parser discards the banner lines and keeps only
the sample rows.

> **Format note.** Plan §2 specifies a cleaner CSV format
> (`<timestamp_us>,<voltage_V>`). The current firmware emits tab-separated
> milliseconds with an extra `raw_code` column. The parser accepts both,
> and converts timestamps to µs internally, so the firmware migration is
> decoupled from the calibration work. When firmware is updated, nothing
> here needs to change.

## Running a calibration

Port assignments (current machine, pre-filled in `config.yaml`):

- **Portenta H7** (laser ADC): `COM8`
- **Zaber stage**: `COM5`

Update `operator` / `notes` in `config.yaml` so they land in `meta.json`.

### 1. Sanity-check the stream (plan §9.1)

Confirm timestamps are monotonic and voltages are sane *before* any stage
motion:

```bash
python portenta_reader.py --port COM8 --duration 30
```

Should end with lines like `captured: 3000 samples over 30.00 s (≈ 100.0 SPS)`
and a "monotonic=True" line.

### 2. Dry run (plan §9.3)

10 points over 1 mm — validates the full pipeline end-to-end in ~1 minute:

```bash
python run_calibration.py --dry-run
```

Produces `data/YYYY-MM-DD_run01_raw.csv`, `..._points.csv`, `..._meta.json`.

### 3. Full calibration run (plan §9.4)

```bash
python run_calibration.py
```

With defaults from `config.yaml` this is 51 points over 10 mm (stage
positions 25 → 35 mm absolute) at 100 samples each, ≈ 1 minute of
sampling + settle time, plus stage moves. Total wall time is dominated
by the mechanical settle windows.

### 4. Analyse (plan §9.5)

```bash
python analyze.py data/2026-04-23_run01_points.csv
```

Prints sensitivity, offset, R², max residual, and runs the sanity-check
list from plan §7. Writes `..._fit.png` alongside the input. Add `--json-out`
to also emit a machine-readable `..._fit.json`.

## Outputs explained (plan §5)

| File | Contents |
|---|---|
| `<prefix>_raw.csv` | every individual sample, tagged with its commanded target and actual stage position |
| `<prefix>_points.csv` | one row per stage position with `mean_V`, `std_V`, `n_samples`, start/end timestamps |
| `<prefix>_meta.json` | stage identity + firmware hash + config snapshot + baseline drift |
| `<prefix>_fit.png` | V-vs-position plot with fit line + residuals subplot |

The `points.csv` is what `analyze.py` consumes; `raw.csv` is there for
re-analysis and for computing per-point noise floors more carefully.

## Expected result (plan §6)

IL-030 in 0–5 V mode with 10 mm range ⇒ nominal **0.5 mV/µm**. Measured
`k` should land within ~5% of this. Larger deviation = something upstream
(wiring, reference, gain) is off and should be investigated before trusting
the number.

## Troubleshooting

**"no samples captured at target x mm"** — the serial stream went silent
during the drain/read window. Usually means the Portenta was reset or the
USB cable was jostled. Re-run.

**Residuals are S-shaped** — the sweep is running into the nonlinear edges
of the IL-030 window. Shrink `sweep_range_mm` in `config.yaml` (try
`[-3.0, 3.0]`).

**Per-point σ varies a lot across the sweep** — often a grounding or
shielding issue on the laser cable. Worth an oscilloscope check on the
IL-030 analog out before re-running.

**Baseline drift is large** — the room or the sensor warmed up during the
run. Let the setup equilibrate for 30 minutes with the laser on before
re-running.
