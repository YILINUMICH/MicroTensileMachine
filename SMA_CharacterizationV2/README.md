# SMA Characterization Recorder

A single-session recorder for SMA actuator electrical characterization. One
invocation walks the operator through three back-to-back phases — **OPEN
calibration → SHORT calibration → RAW experiment** — without ever closing
the LCR or H7 sessions in between, and produces all the files the offline
analyzer needs in a single timestamped session directory.

Companion to the methodology described in the project Notion page on
inductance-based SMA tracking with the Keysight E4980AL + Portenta H7
+ ADS1263 + bias-tee setup.

---

## Architecture

```
                     queue.Queue (LCR)
LCR  ──► LcrWorker ────────────────────►  ┐
                                          │  SessionController
H7   ──► H7Worker  ────────────────────►  ┘  (state machine + sole CSV writer)
                     queue.Queue (H7)
```

Two daemon worker threads run continuously across the entire session.
They push `LcrSample` / `H7Sample` dataclasses into bounded queues. The
controller, on the main thread, drains both queues at ~20 Hz, writes each
sample to whichever phase's CSV is currently open, and swaps active files
at phase boundaries. Workers are oblivious to phases — that decoupling is
what makes a single LCR/H7 session span all three recordings cleanly.

State machine:

```
boot → health_check ─┬─ fail → exit 2
                     │
                     └─ pass
                         │
                  prompt_open → record_open(20s) → confirm_open ┐
                                                                │
                  prompt_short → record_short(20s) → confirm_short
                                                                │
                  prompt_raw → READY banner → record_raw(Ctrl+C)
                                                                │
                                                          finalize
```

`confirm_*` accepts `[Enter]` keep, `[Space]` redo (overwrites the phase's
files), `[Esc]` abort.

---

## Files

| File              | Purpose                                                     |
| ----------------- | ----------------------------------------------------------- |
| `sma_recorder.py` | Entry point. Loads config, starts workers, runs session.    |
| `session.py`      | `SessionController`: state machine, health, meta.json.      |
| `workers.py`      | `LcrWorker`, `H7Worker`, sample dataclasses.                |
| `operator_io.py`  | Prompts, progress bar, banners (uses `readchar`).           |
| `config.py`       | Typed dataclass config loader.                              |
| `lcr_reader.py`   | Keysight E4980AL pyvisa wrapper. Unchanged from prior.      |
| `analyze_sma.py`  | Offline 2-term de-embedding + laser interpolation + plot.   |
| `config.yaml`     | YAML config consumed by both the recorder and analyzer.     |
| `requirements.txt`| Python deps.                                                |

---

## Install

```bash
pip install -r requirements.txt
```

`readchar` is the new dependency (cross-platform single-keypress reader).

You also need a working `pyvisa` backend. On Windows with the Keysight IO
Suite installed, the default `IVI` backend works out of the box. On Linux
install `pyvisa-py` and the appropriate USB backend.

---

## Hardware setup (recap)

- **Keysight E4980AL LCR meter** over USB or LAN VISA.
- **Portenta H7** flashed with the firmware in the `Calibrate_LaserHead/`
  sibling project, streaming ADS1263 samples over USB-CDC.
- **Bias-tee** between the LCR meter, the DC actuation supply, and the
  DUT pigtails. Cable routing must be identical between OPEN, SHORT, and
  RAW phases — this is what makes 2-term de-embedding remove the bias-tee
  + cable parasitics correctly.
- **Keyence IL-030 laser** (or whichever H7 input you've configured) on
  the DUT's free end if you want a displacement axis in the analysis.

---

## Procedure

### One-shot session

```bash
python sma_recorder.py
```

You'll see something like this on the console (logs go to a file):

```
============================================================
  SMA characterization session: sma_20260427_153000
  Output directory:             .../data/sma_20260427_153000
============================================================

────────────────────────────────────────────────────────────
  ✔ Health check PASSED (10.0 s window)
      LCR: 41 samples ✓
      H7 : 481 samples ✓
────────────────────────────────────────────────────────────

┌─ OPEN calibration ──────────────────────────────────────
│ Disconnect the DUT.
│ Leave the bias-tee pigtails OPEN at the DUT end.
│ Verify nothing is bridging the leads.
│
│   [Enter]  start recording
│   [Esc  ]  abort session
└────────────────────────────────────────────────────────────
  OPEN   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓   20.0 / 20.0 s  LCR  2003/2000 (100%)  H7   8051
  OPEN   complete — 2003 LCR / 8051 H7 samples in 20.04 s

┌─ OPEN calibration — confirm ────────────────────────────
│ Recorded 2003 LCR / 8051 H7 samples in 20.04 s.
│
│   [Enter]  keep & continue
│   [Space]  redo this phase (overwrites the files above)
│   [Esc  ]  abort session
└────────────────────────────────────────────────────────────

[... SHORT phase, same shape ...]

┌─ RAW experiment ────────────────────────────────────────
│ Install the SMA DUT at the bias-tee pigtail end.
│ Connect the DC actuation supply to the bias-tee DC port.
│ DO NOT energize yet — wait for the READY banner.
│
│   [Enter]  arm recording
│   [Esc  ]  abort session
└────────────────────────────────────────────────────────────


╔════════════════════════════════════════════════════════════╗
║                                                            ║
║  READY — APPLY ACTUATION CURRENT NOW                       ║
║                                                            ║
║  Live streams: LCR=2031, H7=8092                           ║
║  Press Ctrl+C to stop the recording.                       ║
║                                                            ║
╚════════════════════════════════════════════════════════════╝

  RAW       45.2 s elapsed (Ctrl+C to stop)  LCR   4521  H7  18078
  ^C
  RAW    complete — 4534 LCR / 18103 H7 samples in 45.32 s

============================================================
  Session sma_20260427_153000 — COMPLETE
============================================================
```

### CLI options

```
python sma_recorder.py [--config PATH] [--session-id NAME] [-v]
```

- `--config`: path to `config.yaml` (default: alongside the script).
- `--session-id`: override the auto-generated `sma_<timestamp>` directory
  name.
- `-v`: DEBUG-level logging in `session.log`.

### Keyboard controls

| Where                          | Key       | Effect                                |
| ------------------------------ | --------- | ------------------------------------- |
| Pre-phase prompt               | `Enter`   | Start (or arm, for RAW) the phase.    |
| Pre-phase prompt               | `Esc`     | Abort the whole session.              |
| Post-phase confirm (OPEN/SHORT)| `Enter`   | Keep the recording, advance.          |
| Post-phase confirm (OPEN/SHORT)| `Space`   | Redo this phase (overwrites files).   |
| Post-phase confirm (OPEN/SHORT)| `Esc`     | Abort the session.                    |
| OPEN / SHORT recording         | `Ctrl+C`  | Treat as abort (partial files kept).  |
| RAW recording                  | `Ctrl+C`  | Normal stop. Always keeps the data.   |
| Anywhere, second press         | `Ctrl+C`  | Hard kill (bypasses graceful exit).   |

### Exit codes

| Code | Meaning                                        |
| ---- | ---------------------------------------------- |
| 0    | Session completed.                             |
| 1    | Operator aborted (Esc or Ctrl+C in OPEN/SHORT).|
| 2    | System error (worker crash, file IO, config…). |

---

## LCR settings

These come from `config.yaml > lcr:`. Defaults match the values used in
the SMA inductance Notion writeup:

| Field            | Default   | Meaning                                   |
| ---------------- | --------- | ----------------------------------------- |
| `function`       | `LSRS`    | Series L / series R; `primary`=Ls, `secondary`=Rs. |
| `frequency_hz`   | 1 000 000 | 1 MHz — best SNR for our SMA samples.     |
| `voltage_V`      | 0.5       | Test-signal RMS.                          |
| `integration`    | `SHORT`   | E4980 integration time mode.              |
| `averaging`      | 1         | No on-instrument averaging.               |
| `poll_interval_s`| 0.010     | ~100 Hz host-side poll cadence.           |

Switch to `CPCS` (parallel C / parallel R) by changing `function`; the
columns in the CSV are still labeled `primary`/`secondary` (kept generic
because the meaning depends on `function`). The analyzer assumes `LSRS`.

---

## Smoke tests

Each module's standalone test is unchanged from before:

```bash
python lcr_reader.py        # connect to LCR, print 5 measurements, quit
python ../Calibrate_LaserHead/portenta_reader.py   # H7 stream sanity check
```

For a recorder dry run with both instruments hooked up but no DUT:

```bash
python sma_recorder.py --session-id smoke
# At the OPEN prompt: press Enter, wait 20 s, press Enter to confirm.
# At the SHORT prompt: press Esc to abort.
# Verify data/smoke/open_lcr.csv and open_h7.csv have ~2000 / 8000 rows.
```

---

## Output layout

```
data/sma_20260427_153000/
    open_lcr.csv      open_h7.csv
    short_lcr.csv     short_h7.csv
    raw_lcr.csv       raw_h7.csv
    meta.json
    session.log
```

CSV schemas (unchanged from prior versions):

`*_lcr.csv`:

```
host_timestamp_s, monotonic_s, primary, secondary, status
```

- `host_timestamp_s`: `time.time()` at LCR fetch (POSIX seconds).
- `monotonic_s`: `time.monotonic()` at fetch (drift-free clock for diff).
- `primary`: Ls in henries (LSRS mode).
- `secondary`: Rs in ohms (LSRS mode).
- `status`: E4980 status byte; `0` is normal.

`*_h7.csv`:

```
host_timestamp_s, monotonic_s, firmware_timestamp_us, voltage_V, raw_code
```

- `firmware_timestamp_us`: H7-side uint32 microsecond counter (wraps
  ~71 minutes; use for inter-sample gap detection).
- `raw_code`: int32 ADC code if firmware sends it, otherwise empty.

### `meta.json`

```jsonc
{
  "session_id": "sma_20260427_153000",
  "started_at_utc": "2026-04-27T19:30:00Z",
  "ended_at_utc":   "2026-04-27T19:32:14Z",
  "completed": true,
  "aborted_at_phase": null,
  "last_functional_step": "finalized",
  "errors": [],
  "phases": {
    "open":  {"duration_s": 20.04, "target_duration_s": 20.0,
              "lcr_n": 2003, "h7_n": 8051, "redos": 0,
              "started_at_utc": "...", "ended_at_utc": "..."},
    "short": {... "redos": 1},
    "raw":   {... "target_duration_s": null, "duration_s": 45.32}
  },
  "phases_config": {"open_duration_s": 20.0, "short_duration_s": 20.0},
  "lcr": {"frequency_hz": 1000000.0, ..., "idn": "Keysight ...",
          "n_dropped": 0},
  "h7":  {"port": "COM8", ..., "n_dropped": 0},
  "run": {"operator": "", "notes": "", "output_dir": "data"},
  "host": {"platform": "Windows-10-...", "python": "3.11.5"},
  "laser_calibration_reference": {
    "k_mV_per_um": -0.1171, "V0_mV": 566.957,
    "source": "Calibrate_LaserHead/data/2026-04-24_run07_*",
    "conversion": "displacement_um = (V_mV - V0_mV) / k"
  }
}
```

`last_functional_step` is updated on every state transition. If a session
crashes, this field tells you exactly which step was active when the
failure happened (`"phase_open_recording"`, `"phase_raw_prompt"`, etc.).

`n_dropped` counts samples a worker had to drop because the queue was
full — should be `0` under normal operation. Non-zero values indicate
the controller stalled, usually because the operator stayed on a confirm
prompt for a long time with samples still streaming in.

---

## Analysis

```bash
python analyze_sma.py --session data/sma_20260427_153000
```

Auto-resolves the per-phase CSVs and pulls `frequency_hz` and the laser
calibration constants from `meta.json`. Outputs go in the same directory:

```
data/sma_20260427_153000/
    processed.csv       # de-embedded Rs_DUT, Ls_DUT, Q, phase, displacement
    processed.png       # 4-panel summary (Ls, Rs, phase, displacement)
```

### De-embedding

When `open_lcr.csv` is present (the default for sessions recorded with
this recorder), the analyzer applies parallel-then-series 2-term
de-embedding:

```
Y_open  = mean of  1 / (R + jωL)        over OPEN samples
Z_short = mean(R) + jω·mean(L)          over SHORT samples
Z_meas  = R_raw + jω·L_raw              from RAW
Y_dut   = 1 / (Z_meas − Z_short) − Y_open
Z_dut   = 1 / Y_dut
```

When OPEN data is absent (legacy mode, or `--deembed short_only`), the
analyzer falls back to series-only:

```
Z_dut   = Z_meas − Z_short
```

Override with `--deembed {auto,short_only,open_short}`.

### Legacy mode

For data assembled by hand or recorded with the previous version:

```bash
python analyze_sma.py --short SHORT.csv --run RAW.csv \
                      [--open OPEN.csv] [--laser H7.csv] \
                      [--frequency 1e6]
```

### Laser conversion

Displacement is interpolated onto each LCR sample's `host_timestamp_s`
and converted via:

```
displacement_um = (V_mV - V0_mV) / k_mV_per_um
```

Defaults from `Calibrate_LaserHead/data/2026-04-24_run07_*`:

```
k_mV_per_um = -0.1171
V0_mV       = 566.957
```

Override with `--k` / `--v0`. Out-of-window LCR rows (before the first
H7 sample or after the last one) get `NaN` for displacement and are
excluded from the displacement plot.

---

## Timing model

- LCR is poll-driven: the host sends `FETC?` at `poll_interval_s` cadence.
  No FIFO on the instrument — a host-side stall just means fewer samples
  in the window, never duplicates or out-of-order data.
- H7 is push-driven: the firmware streams continuously; OS serial buffer
  is ~4 KB. Bytes are silently dropped if the buffer fills, so check
  `n_dropped == 0` and watch for gaps in `firmware_timestamp_us` if
  precise H7 sample counts matter.
- Joint timeline: both streams' `host_timestamp_s` are from the same
  `time.time()` clock, so `analyze_sma.py` can interpolate the H7
  voltage onto LCR sample times directly.

---

## Related directories

- `../Calibrate_LaserHead/` — IL-030 laser calibration scripts and the
  `portenta_reader.py` H7 serial interface (imported via a `sys.path`
  shim from `workers.py`).
- `../AD2/` — temporary Analog Discovery 2 substitute interface for when
  the H7 hardware path is offline.

---

## Reference

Project context, methodology rationale, and run logs:
https://www.notion.so/349d5b0603fb8055b233d55f477d00f5
