> **Status: WIP / To-Test** — code-complete, offline analyzer verified on synthetic data; not yet bench-run. See [STATUS.md](STATUS.md). Project map: [../README.md](../README.md).

# Experiment_SMACharacterizationV3 — multi-instrument SMA recorder + analyzer

V3 extends the V2 recorder into a full multi-instrument session: **one
config file** sets every instrument and sensor parameter, the recorder logs
**raw** streams from the LCR, the combined-firmware H7 (sensors *and* SMA),
and the Zaber stage, and an **offline analyzer** converts raw→physical and
renders dashboards.

## Architecture (same backbone as V2)

```
config.yaml ─► startup: LCR.configure, stage.home/velocity, (optional H7 cmds)
                    │
   ┌──────────┬─────┴──────┬───────────┐
 LcrWorker  H7Worker     ZaberWorker     (threads → bounded queues)
 VISA       COM8         COM5
   └──────────┴────────────┴───────────┘
                    │ queues
         SessionController  (sole CSV writer, OPEN→SHORT→RAW state machine)
                    │
   per-phase CSVs + meta.json ─► analyze_sma.py ─► dashboards + joined CSV
```

Workers stream continuously across all phases; the controller is the only
file writer. Any stream can be disabled with its `enabled:` flag.

**Design rule:** the recorder logs **raw data only** — it configures
instruments but never converts units or pushes calibration to firmware.
Calibration coefficients live in `config.calibration`, are copied into
`meta.json`, and are used **only** by the offline analyzer.

## Configuration — `config.yaml`

| Section | Sets |
|---|---|
| `lcr` | E4980 function/frequency/voltage/integration/averaging/poll + `enabled` |
| `h7` | port/baud, `channels` (which of laser/load/sma_v/sma_i/sma_r to keep), `startup_commands` (inert hook) |
| `stage` | Zaber port, `position_limits_mm`, velocity, reading rate, home/zero options, poll |
| `phases` | OPEN / SHORT durations (RAW runs until Ctrl+C) |
| `calibration` | **analysis-only** coefficients: laser `{k_mV_per_um, V0_mV}`, load cell `{scale_N_per_V, offset_V}`, current sense (firmware defaults, for traceability) |
| `run` | operator, notes, output dir |

## The H7 stream

The combined firmware ([`Firmware_SMASensorHub_PIO`](../Firmware_SMASensorHub_PIO/))
emits one multiplexed stream: `src=1` laser, `2` load, `3` SMA V, `4` SMA I,
`5` SMA R. `H7Worker` reads it with the extended
[`Calibrate_LaserHead/portenta_reader.py`](../Calibrate_LaserHead/portenta_reader.py)
(`adc_source=None`), demuxes by channel, and logs every enabled channel
raw to one `*_h7.csv` per phase (with `src`/`channel` columns). For
`src=4/5` the `value` column carries **amps / ohms** (firmware-computed),
not volts.

## Run a session

```sh
pip install -r requirements.txt
python sma_recorder.py                       # uses config.yaml
python sma_recorder.py --session-id flexinol_run01
```

Output: `data/<session_id>/` with `{open,short,raw}_{lcr,h7,stage}.csv`,
`meta.json`, `session.log`. The operator is walked through OPEN → SHORT →
RAW; RAW records until Ctrl+C.

### SMA actuation — the state machine runs on M7

By default (`sma.enabled: false`) the recorder is a **pure logger** and you
drive the SMA manually from the H7 console (`drive`/`fire`/`cycle`).

Set `sma.enabled: true` to have the recorder drive the firmware's
**on-M7 cyclic actuation**: at RAW start it sends one
`cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>`, a `ping` heartbeat every
second, and `stop` at the end. **The PC only sends parameters + heartbeat —
M7 owns all phase timing**, so heat/cool durations are deterministic and
immune to USB/host-scheduling jitter. If the recorder crashes or the host
goes silent, M7's watchdog (`sma.wdt_ms`) safe-stops the SMA. Configure it
in the `sma:` block:

```yaml
sma:
  enabled: true
  v_high: 3.0
  v_low: 0.0
  fire_ms: 2000
  cool_ms: 8000
  n_cycles: 10      # 0 = continuous until RAW Ctrl+C
  wdt_ms: 5000
```

## Analyze + visualize

```sh
python analyze_sma.py --session data/sma_20260621_153000
python analyze_sma.py --session <dir> --phase raw \
                      --k -0.1171 --v0 566.957 --load-scale 50.0
```

Produces, in the session dir:

- `<phase>_dashboard.png` — multi-panel: displacement, force, SMA R/V/I,
  de-embedded LCR R/L, stage position, and force-vs-displacement.
- `<phase>_joined.csv` — all streams interpolated onto a uniform 100 Hz grid.

Conversions are applied only where the coefficient is present in
`meta.json` (or overridden on the CLI); otherwise the channel is plotted
raw. LCR de-embedding auto-selects OPEN+SHORT (2-term) or SHORT-only.

## Files

```
Experiment_SMACharacterizationV3/
├── README.md / STATUS.md / requirements.txt
├── config.yaml            every instrument + sensor parameter
├── config.py              typed dataclasses (lcr/h7/stage/phases/calibration/run)
├── workers.py             LcrWorker, H7Worker (multi-channel), ZaberWorker
├── session.py             OPEN→SHORT→RAW controller, sole CSV writer
├── sma_recorder.py        entry point (builds enabled streams)
├── operator_io.py         terminal prompts / progress / banners
└── analyze_sma.py         offline de-embed + raw→physical + dashboards
```

Cross-module drivers are imported via `sys.path` shims (canonical sources:
`Driver_KeysightLCR`, `Driver_ZaberStage`, `Calibrate_LaserHead`), not
re-implemented here.

## Relationship to V2

V3 supersedes `Experiment_SMACharacterizationV2` for the combined-firmware
rig (sensors + SMA on one port) and adds stage logging + a config-driven
calibration block. V2 remains as the single-ADC-stream reference.
