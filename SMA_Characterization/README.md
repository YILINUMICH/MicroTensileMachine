# SMA Characterization — concurrent LCR + laser recorder

Continuous, time-aligned raw data capture for SMA (Flexinol) coil
characterization under actuation. Two independent streams recorded
simultaneously:

1. **Keysight E4980AL LCR** — series inductance `Ls` and series resistance
   `Rs` at a fixed frequency (1 MHz default), raw values with all internal
   corrections OFF. Phase is derivable post-hoc from `Rs + jωLs`.
2. **IL-030 laser displacement head** — via the SensorHub_PIO firmware on
   the Portenta H7, through the ADS1263 ADC1 on AIN0/AIN1.

Both streams are timestamped with the same host wall-clock
(`time.time()`), so post-processing can join them on time.

**No de-embedding, calibration, or filtering is applied at record time.**
Analysis pipelines consume the raw CSVs and apply:

- LCR: open/short/load de-embedding per Notion
  [Bias-Tee + LCR Dummy DUT Characterization](https://www.notion.so/349d5b0603fb8055b233d55f477d00f5)
  methodology
- Laser: displacement conversion using the calibration from
  `../Calibrate_LaserHead/`: `k = -0.1171 mV/µm`, `V0 = 566.957 mV`,
  displacement_µm = (V_mV - V0_mV) / k

## Layout

```
SMA_Characterization/
├── README.md           — this file
├── requirements.txt    — pyvisa, pyserial, pyyaml, numpy
├── config.yaml         — LCR settings + laser port + run duration
├── lcr_reader.py       — Keysight E4980AL pyvisa wrapper (standalone smoke test)
├── sma_recorder.py     — concurrent two-thread recorder (main entry point)
├── analyze_sma.py      — offline de-embedding + laser join (short-only)
└── data/               — per-run outputs
```

## Install

From this folder:

```
python -m pip install -r requirements.txt
```

You also need a VISA backend on the host:
- **Keysight IO Libraries Suite** (preferred — ships with Connection Expert
  for instrument discovery), OR
- **NI-VISA**

The laser side reuses `../Calibrate_LaserHead/portenta_reader.py` via a
`sys.path` shim in `sma_recorder.py` — no separate install needed.

## Hardware setup

Per the validated config in the Notion run:

| Instrument | Connection | Role |
|---|---|---|
| Keysight E4980AL | USB (or GPIB/LAN via VISA) | LCR measurement |
| Double bias-tee (0.22 µF C0G + 47 µH) | between E4980AL front and DUT | DC actuation path + AC sense |
| DC power supply | → bias-tee DC port | Joule-heating current for SMA actuation |
| Flexinol SMA coil | DUT at bare-wire end of SMA pigtails | sample under test |
| IL-030 laser head | analog out → HAT AIN0/AIN1 → Portenta COM8 | displacement monitor |
| Portenta H7 + ADS1263 HAT | USB-CDC (COM8) | ADC bridge |

The Flexinol is mounted such that its contraction under Joule heating
produces a displacement that the IL-030 measures. The exact mount
geometry is experiment-specific and documented in the run's `notes`
field in `config.yaml`.

## LCR settings (validated in Notion run 2026-04-20)

Current defaults in `config.yaml`:

| Parameter | Value | Reason |
|---|---|---|
| Function | `LSRS` | series L + series R; raw values logged, de-embed post-hoc |
| Frequency | 1 MHz | Best SNR in the 100 kHz–1 MHz usable band |
| Test voltage | 0.5 V | Matches characterization run |
| Integration | `SHORT` | Max throughput (~10 ms/measurement) |
| Averaging | 1 | No instrument-side averaging — preserves raw noise for analysis |
| OPEN / SHORT / LOAD correction | OFF | De-embedding done in post-processing |
| Display | disabled at run | Saves a few ms/sample |

## Smoke tests

Verify each instrument before running a real recording.

**LCR standalone:**

```
python lcr_reader.py --duration 10
```

Should connect to the E4980AL, configure it, stream Ls/Rs readings for
10 s, and print summary stats. If it reports "No Keysight E4980 found",
check:
- USB cable connected, instrument powered
- Keysight IO Libraries installed
- If multiple instruments are on the bus, pass `--resource "USB0::..."`

**Laser standalone:** uses the same script as the calibration folder:

```
cd ../Calibrate_LaserHead
python portenta_reader.py --port COM8 --duration 5
cd ../SMA_Characterization
```

Should show ~400 samples/s at around ~0.57 V (the "target at 30 mm"
idle voltage for this signal path).

## Running a recording

```
python sma_recorder.py                    # 60 s, per config.yaml
python sma_recorder.py --duration 120     # override duration
python sma_recorder.py --until-ctrl-c     # run until user interrupts
```

Outputs land in `data/sma_YYYYMMDD_HHMMSS_*`:

- `{prefix}_lcr_raw.csv`: `host_timestamp_s, monotonic_s, primary (Ls), secondary (Rs), status`
- `{prefix}_laser_raw.csv`: `host_timestamp_s, monotonic_s, firmware_timestamp_us, voltage_V, raw_code`
- `{prefix}_meta.json`: run metadata (settings, hardware IDN, timestamps,
  counts, calibration constants for the laser path)

Both CSVs are flushed every 50 (LCR) / 200 (laser) rows so a Ctrl+C mid-run
keeps data.

## Typical workflow

1. **Record a SHORT-calibration run**: physically short the DUT pigtails
   at the bare-wire ends, then
   ```
   python sma_recorder.py --run-type short --duration 30
   ```
   Files are tagged with `sma_short_YYYYMMDD_HHMMSS_*` so they're easy to
   pick out later. `meta.json` records `"run_type": "short"` too.
2. **Set up DC current source** for Flexinol actuation (outside this
   script — runs independently via your bias-tee DC path).
3. **Run smoke tests** on the LCR/laser if either has been disturbed.
4. **Start the recorder** for the actual experiment:
   ```
   python sma_recorder.py --until-ctrl-c          # defaults to --run-type run
   ```
   Files get `sma_run_YYYYMMDD_HHMMSS_*` prefix.
5. **Apply the actuation schedule** (heater on / cool / cycle / whatever)
   while the recorder captures both streams continuously.
6. **Ctrl+C** to stop. Both CSVs and `meta.json` are written.
7. **Offline analysis** — feed both raw CSVs plus the SHORT reference to
   the de-embedder:
   ```
   python analyze_sma.py \
       --short data/sma_<short_run>_lcr_raw.csv \
       --run   data/sma_<experiment>_lcr_raw.csv \
       --laser data/sma_<experiment>_laser_raw.csv
   ```
   Outputs `<experiment>_processed.csv` and `<experiment>_processed.png`
   next to the run's CSVs. The processed CSV has columns:
   `host_timestamp_s, monotonic_s, rs_raw_ohm, ls_raw_H, rs_dut_ohm,
   ls_dut_H, q_dut, phase_deg, status, displacement_mm`.

## De-embedding (short-only)

Only short-subtraction is applied, per the Notion finding that open
correction doesn't materially help for this signal chain:

```
Z_raw   = Rs + j·ω·Ls                           (per measurement)
Z_short = mean(Rs_short) + j·ω·mean(Ls_short)   (once, from SHORT run)
Z_DUT   = Z_raw − Z_short
Ls_DUT  = Im(Z_DUT) / ω
Rs_DUT  = Re(Z_DUT)
Q       = |ω·Ls_DUT / Rs_DUT|
phase   = atan2(Im(Z_DUT), Re(Z_DUT))
```

The SHORT run captures the fixed series parasitic (cables, bias-tee
chokes, connector). Re-record the short any time the cable routing
changes — it drifts ~1% with mechanical disturbance per Notion §4.2.

## Laser displacement conversion

If a laser CSV is passed with `--laser`, the analyzer linearly
interpolates the laser voltage onto each LCR timestamp and converts to
displacement in mm using:

```
displacement_mm = (V_mV − V0_mV) / k_mV_per_um × 1e-3
```

Defaults match the `Calibrate_LaserHead/` 2026-04-24 run07 result:
`k = -0.1171 mV/µm`, `V0 = 566.957 mV`. Override with `--k` and `--v0`
if you re-run calibration with different signal conditioning.

## Timing model

LCR and laser operate at very different rates:

| Stream | Rate | Notes |
|---|---|---|
| LCR (SHORT integration) | ~100 measurements/s | Hardware-limited by ADC+filter settle |
| Laser (ADC1 @ 400 SPS) | ~400 samples/s | Firmware polling cadence |

For time-aligned post-hoc analysis, use the laser's higher-rate stream as
the "master clock" and interpolate LCR values to each laser timestamp
(or vice-versa, depending on which observable drives the analysis).

## Related directories

- `../Calibrate_LaserHead/` — provides `portenta_reader.py` (imported here)
  and the calibration constants in the run meta.
- `../SensorHub_PIO/` — the firmware running on the Portenta. Must be
  flashed with `ENABLE_ADC1 = 1`, `ENABLE_ADC2 = 0`, INPMUX = 0x01.

## Reference links

- [Notion: Bias-Tee + LCR Dummy DUT Characterization](https://www.notion.so/349d5b0603fb8055b233d55f477d00f5)
- [Notion: LCR + Bias-tee: Version 1](https://www.notion.so/2b5d5b0603fb8046bc4ec7189bbe7c86)
- [Keysight E4980AL programming guide](https://www.keysight.com/us/en/assets/9018-05014/programming-guides/9018-05014.pdf)
