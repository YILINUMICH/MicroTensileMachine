> **Status: Stable** — produces the `k` / `V₀` constants consumed by `../SMA_CharacterizationV2/`. See [STATUS.md](STATUS.md). See [../README.md](../README.md) for project overview.

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

**Wiring (post-port to Mid Carrier + bare TI ADS1263 EVM, 2026-05-26):**

- IL-030 analog signal out   → **EVM AIN4(+)**         (Cable 4 in `../doc/MEMO_cable_map.md`)
- IL-030 sensor ground       → **EVM AIN5(−)**
- IL-030 supply ground (0 V) → **EVM AVSS / GND**      (separate wire — do
  not rely on AIN5 alone to define the ground reference)
- External REF7050 (+5 V)    → **EVM AIN0 / AIN1**     (Cable 2 — should
  already be in place per SensorHub_PIO production wiring; do not disturb)

The IL-030 sees the ADC2 input directly — no HAT load-cell front-end
between sensor and ADC. The previous Waveshare-HAT routing through
AIN0/AIN1 / ADC1 is **retired**; legacy `k = −0.1171 mV/µm` from that
era is invalid on this signal path (see `STATUS.md` last-run notes).

1. Mount the IL-030 on the fixed frame; mount the diffuse reference plate on
   the Zaber carriage, aligned so the stage axis is parallel to the laser
   beam. A cosine error from axis misalignment compresses the apparent
   sensitivity (the fit still looks clean), so check parallelism against
   the existing fixture marks.

2. **Stage-to-sensor mapping on this rig** (verify before each session —
   the mounting fixture sets these absolute stage values):

   | Stage position | IL-030 measurement window | Voltage |
   |---:|:---|:---:|
   | 5 mm  | high end of the sensor window | **max** reading |
   | 10 mm | reference distance            | mid reading     |
   | 15 mm | low end of the sensor window  | **min** reading |

   So as stage position INCREASES (5 → 15), the output voltage
   DECREASES. The fit slope `k` is therefore NEGATIVE — see "Expected
   result" below.

3. With the stage at absolute **10 mm**, the IL-030 amplifier's
   "reference distance" LED must be **lit**. If it isn't, the
   mounting fixture has shifted — reseat the sensor or re-zero the
   stage before continuing.

4. Manually jog stage to 5 mm and to 15 mm; confirm the analog output
   stays valid at both extremes (no saturation, no "out of range"
   indicator on the IL-030 front panel).

5. The sweep runs ±5 mm around absolute stage position 10 mm, i.e.
   stage positions 5 mm → 15 mm. This is the full IL-030 measurement
   window. If the edges look nonlinear in the fit, shrink
   `sweep_range_mm` in `config.yaml` to e.g. ±4 mm.

6. **Zaber safety_config.json must permit at least [5, 15] mm**. The
   pre-2026-05-26 limit was [10, 40] which would FORBID the new
   low-end position at 5 mm and abort the sweep on its first move.
   Verify (`../ZaberStage/safety_config.json` → `position_limits_mm`)
   and widen if needed.

7. **30-minute thermal soak** with the laser on before sampling — the
   baseline-pre vs baseline-post delta is the only way you'll catch
   thermal drift during the run, and a cold start guarantees you'll
   see some.

## Firmware prerequisite

Flash the dedicated **`./Calibrate_LaserHead_PIO/`** project (created
2026-05-26 — see [STATUS.md](STATUS.md) and
[MEMO_session_2026-05-26.md](MEMO_session_2026-05-26.md)). This is the
dual-ADC cross-compare firmware purpose-built for this calibration
workflow: ADC2 produces the production k/V₀, ADC1 reads the same
AIN4/AIN5 pair as an independent digital-path check. The sibling
`../LaserHead_PIO/` is the production laser-only firmware and isn't
what you flash for calibration.

Configuration baked into `Calibrate_LaserHead_PIO/src/main.cpp`:

- `ENABLE_ADC1 = 1`, `ENABLE_ADC2 = 1` (both on AIN4/AIN5)
- ADC2MUX = `0x45`, INPMUX = `0x45` (both → AIN4(+) / AIN5(−))
- REF2 = `ADS1263_ADC2_REF_AIN01`, REFMUX = `0x09`
  (shared external REF7050 on AIN0/AIN1)
- Rate = 400 SPS on both, Sinc3 filter, gain = 1, PGA in path

Flash order (only first time, or after firmware changes):

```bash
cd Calibrate_LaserHead_PIO
pio run -e portenta_m7_bridge -t upload
pio run -e portenta_m4        -t upload
# Power-cycle the rig (USB + EVM supply) after EACH upload — dfu reset
# does not cleanly re-power the EVM analog rails.
```

At boot you should see a `[M4] *** Calibrate_LaserHead_PIO — dual-ADC
cross-compare on AIN4/AIN5 ***` banner over USB so you can confirm you
flashed the right firmware variant.

Confirm via a serial monitor (or our smoke test, below) that you see
streaming lines like:

```
<t_ms>\t<src>\t<raw_code>\t<voltage_V>     (src=1 ADC1, src=2 ADC2)
```

at ~400 SPS per ADC (so ~800 lines/s total), interleaved with a few
`[M4 cp N]` / `[M7]` banner lines at boot. Both `src=1` and `src=2`
rows should show approximately the same voltage at any static standoff
since both ADCs are reading the same physical input (AIN4/AIN5). The
`portenta_reader.py` parser discards the banner lines, and
`PortentaReader.read_samples_dual()` demuxes the two channels into
separate sample lists for parallel fitting in `analyze.py`.

> **Format note.** Plan §2 specifies a cleaner CSV format
> (`<timestamp_us>,<voltage_V>`). The current firmware still emits
> tab-separated milliseconds with an extra `raw_code` column. The
> parser accepts both, and converts timestamps to µs internally, so
> the firmware migration is decoupled from the calibration work. When
> firmware is updated, nothing here needs to change.
>
> The parser also handles the 4-column dual-stream format
> (`<t_ms>\t<src>\t<raw_code>\t<voltage_V>`) that SensorHub_PIO emits,
> selecting by `adc_source` — relevant if you point this calibration
> tool at SensorHub_PIO instead of LaserHead_PIO. With the current
> laser-only LaserHead_PIO build the `adc_source` filter is a no-op.

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

IL-030 in 0–5 V mode with 10 mm range ⇒ nominal **|k| ≈ 0.5 mV/µm**.
Measured `|k|` should land within ~5% of this on the EVM signal path
(no front-end attenuation between sensor and ADC2). Larger deviation =
something upstream (wiring, REF7050 voltage, polarity at AIN4/AIN5,
IL-030 front-panel range setting) is off and should be investigated
before trusting the number.

**Sign convention on this rig.** Per the stage-to-sensor mapping above
(stage 5 mm = max V, stage 15 mm = min V), voltage decreases as stage
position increases → the linear fit slope `k` is **negative** by
construction. The `analyze.py` sanity check compares `|k|` to the
0.5 mV/µm nominal; the sign just records which way the geometry +
sensor output wiring point. When propagating the constants into
`SMA_CharacterizationV2/`, keep `k` signed so downstream
displacement = `(V − V₀) / k` recovers the correct physical direction.
If the fit returns `k > 0` for this setup, the IL-030 output mapping
is inverted from expected — check the IL-030 front-panel
"polarity"/"slope" setting before re-running.

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
