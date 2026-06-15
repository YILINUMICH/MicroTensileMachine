> **Status: Stable**. See [STATUS.md](STATUS.md). See [../README.md](../README.md) for project overview.

# SDS2000X Plus Oscilloscope Module

A Python module for controlling the Siglent SDS2000X Plus oscilloscope over raw
TCP sockets (SCPI, port 5025) — **no VISA backend required**. Written to mirror
the API of the [KeysightLCR](../KeysightLCR/README.md) `lcr_meter.py` module so it
drops into the same worker / recorder architecture.

## Features

- **No-VISA transport**: Raw TCP socket SCPI on port 5025; nothing to install but Python
- **Continuous parameter reads**: Streams automatic measurements (`PAVA?`) like the LCR `FETCH?` loop
- **Cross-channel measurements**: Magnitude + phase in one poll (`C1` PKPK primary, `C1-C2` PHA secondary) — the SRF use case
- **Built-in AWG control**: Drive the WaveGen (`BSWV`) for isolation / SRF frequency sweeps
- **Raw waveform capture**: Pull `WF? DAT2` records as ADC codes + preamble
- **Worker-ready**: `iter_measurements()` yields the same sample shape as `LcrWorker`
- **Robust**: Soft reconnect on socket dropouts for long recorder runs
- **Context manager**: Safe socket cleanup

## Installation

### Requirements
- Python 3.7+
- `numpy` (only needed for `capture_waveform()` / `codes_to_volts()`)
- Siglent SDS2000X Plus reachable over LAN

### Install Dependencies
```bash
pip install numpy
```

No VISA libraries, no PyVISA — the driver talks to the scope's SCPI socket directly.

### Network Setup
On a direct scope↔PC cable both ends use link-local (APIPA, `169.254.x.x`)
addressing. The scope **cannot hold a manual static IP in `169.254.0.0/16`**
(that range is reserved for auto-assignment), so it self-assigns an address
that can move between sessions. The driver handles this:

- Leave the scope on `Utility > System Setting > I/O Setting > LAN Config` with
  *Automatic (DHCP)* checked — it will auto-assign a `169.254.x.x` address.
- On the PC: use an APIPA / link-local address in the same range (the default
  Windows behaviour on a direct cable — nothing to configure).
- `auto_connect()` tries `DEFAULT_HOST` (`169.254.111.100`) first; if that's
  down it **sweeps the link-local /24 for a Siglent answering `*IDN?`** and
  latches onto it automatically. No manual IP needed.

To skip discovery, pin the address explicitly:

- `SCOPE_IP` — exact host, e.g. `169.254.111.4` (fastest; no scan).
- `SCOPE_SUBNET` — the `/24` the auto-scan sweeps, e.g. `169.254.111` (use if
  the scope lands outside the default subnet).

Confirm reachability with `ping <scope-ip>`; the driver connects on port `5025`.

> For a permanent fixed address, move both ends off link-local onto a routable
> private subnet (e.g. scope `192.168.1.100/24`, PC NIC `192.168.1.50/24`) and
> set `DEFAULT_HOST`/`SCOPE_IP` to match — that's the only way to get a true
> static IP, since the scope rejects static addresses in the `169.254` range.

## Quick Start

### Basic Usage
```python
from oscilloscope import Oscilloscope, ScopeConfig, MeasureParam

# Defaults to 169.254.111.100:5025
with Oscilloscope() as scope:
    scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))

    # Single reading
    r = scope.read_single()
    print(scope.format_result(r))     # -> #0001: PKPK=3.00e+00 V  [OK]

    # Burst of 100 readings
    results = scope.read_burst(100)
```

### Explicit IP
```python
with Oscilloscope(host="169.254.111.100", port=5025) as scope:
    ...
```

### Quick Measurement Function
```python
from oscilloscope import quick_measure, MeasureParam

results = quick_measure(source="C1", param=MeasureParam.PKPK, count=100)
for r in results:
    print(f"Vpp = {r.primary:.4f} V")
```

## Module Structure

### Main Classes

#### `Oscilloscope`
Main interface class for instrument control.

**Key Methods:**
- `__init__(host=None, port=5025, timeout=5.0, auto_open=True)` — open socket (auto-detect default IP if `host` is None)
- `connect(host, port=None)` / `auto_connect()` — establish + `*IDN?` check
- `configure(config)` — apply the read configuration
- `read_single()` — read one measurement
- `read_burst(count)` / `read_continuous(...)` — high-speed acquisition
- `iter_measurements(poll_interval_s, ...)` — generator for recorder loops
- `set_awg(...)` / `set_awg_frequency(f)` — built-in WaveGen control
- `clear_sweeps()` — `CLSW`, reset Math-average stats at each sweep step
- `get_sample_rate()` / `get_timebase()` — `SARA?` / `TDIV?`
- `capture_waveform(source)` — raw `WF? DAT2` → (codes, preamble)
- `write(cmd)` / `query(cmd)` — raw SCPI passthrough
- `close()` — release the socket

> **One socket at a time.** The SDS2000X Plus serves a single SCPI client on
> port 5025: a second concurrent connection accepts at the TCP layer but never
> answers `*IDN?` (it times out). Always `close()` one session before opening
> another — don't hold two `Oscilloscope` objects against the same scope.

#### `ScopeConfig`
Configuration dataclass for the read loop.

**Parameters:**
- `source`: channel/trace to measure (`"C1"`..`"C4"`, `"MATH"`, `"F1"`, ...)
- `param`: `MeasureParam` for the primary reading
- `second_source` / `second_param`: optional second reading (e.g. `"C1-C2"` + `PHA`)
- `settle_s`: optional dwell after configure
- `chdr_off`: request bare-value replies (`CHDR OFF`); default `True`

#### `ScopeMeasurement`
Result dataclass. Field names match `lcr_meter.MeasurementResult`.

**Attributes:**
- `primary`: value of `param` on `source` (NaN if scope returns `****`)
- `secondary`: value of `second_param`, or NaN if unused
- `status`: `0` = valid, `1` = no valid measurement
- `timestamp`: host wall-clock (`time.time()`)
- `monotonic`: host monotonic clock (jitter-safe spacing)
- `reading_number`: sequential counter
- `param` / `unit`: parameter name + unit parsed from the reply

### Measurement Parameters (`MeasureParam`)

| Param | Meaning | Typical Unit | Notes |
|-------|---------|--------------|-------|
| PKPK | Peak-to-peak | V | Isolation / SRF magnitude |
| AMPL | Amplitude (top−base) | V | |
| MAX / MIN | Max / min | V | |
| MEAN / CMEAN | Mean / cyclic mean | V | |
| RMS / CRMS | RMS / cyclic RMS | V | |
| STDEV | Std deviation | V | |
| FREQ / PER | Frequency / period | Hz / s | |
| WID / NWID | +width / −width | s | |
| DUTY / NDUTY | Duty cycle | % | |
| RISE / FALL | Rise / fall time | s | |
| PHA | **Phase** | deg | **Cross-channel** (`C1-C2:MEAD? PHA`) |
| SKEW | **Skew** | s | **Cross-channel** (`C1-C2:MEAD? SKEW`) |

> Cross-channel delay params (`PHA`, `SKEW`) use the **`MEAD?`** (MEASURE_DELAY)
> verb, not `PAVA?` — sending them via `PAVA?` gets no reply and the read times
> out. The driver selects the verb automatically (`_measure_query`), so just
> set `second_source="C1-C2"` + `second_param=PHA` and it does the right thing.

## Advanced Usage

### SRF-Style Read: Magnitude + Cross-Channel Phase
```python
cfg = ScopeConfig(
    source="C1",          param=MeasureParam.PKPK,    # magnitude
    second_source="C1-C2", second_param=MeasureParam.PHA,  # phase
)
with Oscilloscope() as scope:
    scope.configure(cfg)
    r = scope.read_single()
    magnitude, phase_deg = r.primary, r.secondary
```

### Built-in AWG Frequency Sweep
```python
import time
from oscilloscope import Oscilloscope, ScopeConfig, MeasureParam

with Oscilloscope() as scope:
    # High-Z load so the displayed amplitude matches the open-circuit output
    scope.set_awg(wavetype="SINE", amplitude=1.0, output=True, load="HZ")
    scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))

    for f in [1e3, 1e4, 1e5, 1e6, 5e6]:
        scope.set_awg_frequency(f)
        scope.clear_sweeps()      # reset Math-average stats at each step
        time.sleep(0.2)           # let the average settle
        print(f, scope.read_single().primary)
```

### Raw Waveform Capture
```python
from oscilloscope import Oscilloscope, codes_to_volts

with Oscilloscope() as scope:
    codes, pre = scope.capture_waveform(source="C1")
    volts = codes_to_volts(codes, pre.vdiv, pre.offset)
    t = pre.time_axis()           # seconds, derived from SARA?
```
> For **long records**, prefer the scope's web server: save the binary `*.bin`
> and convert with Bin2CSV. `capture_waveform()` is the convenience path for
> short on-socket grabs. Verify `CODES_PER_DIV` against your firmware's
> Programming Guide before trusting absolute volts.

### Continuous Monitoring with Callback
```python
def on_sample(r):
    if r.primary > 1.0:           # alert above 1 Vpp
        print(f"High: {r.primary:.3f} V")
    return r.reading_number < 1000  # stop after 1000

with Oscilloscope() as scope:
    scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
    scope.read_continuous(callback=on_sample)
```

### Worker Integration (mirrors `LcrWorker`)
`iter_measurements()` yields objects with `.primary .secondary .status
.timestamp .monotonic` — the same fields `LcrWorker` reads — so a `ScopeWorker`
in `../SMA_CharacterizationV2/workers.py` maps onto it identically:

```python
for m in scope.iter_measurements(poll_interval_s=0.05):
    if stop_event.is_set():
        break
    sample = ScopeSample(
        host_timestamp_s=m.timestamp,
        monotonic_s=m.monotonic,
        primary=m.primary,        # e.g. PKPK (V)
        secondary=m.secondary,    # e.g. phase (deg), or NaN
        status=m.status,
    )
    out_queue.put_nowait(sample)
```

### Data Logging to CSV
```python
import csv
from datetime import datetime
from oscilloscope import Oscilloscope, ScopeConfig, MeasureParam

with Oscilloscope() as scope:
    scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
    fname = f"scope_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["monotonic_s", "Vpp", "status"])
        for r in scope.read_burst(1000):
            w.writerow([r.monotonic, r.primary, r.status])
```

## Testing

`test_oscilloscope.py` lives next to the driver and does connection + health
checks plus functional tests.

```bash
# Run all tests (auto-detect default IP)
python test_oscilloscope.py

# Specify the scope IP / port
export SCOPE_IP=169.254.111.100      # bash
export SCOPE_PORT=5025
# $env:SCOPE_IP = '169.254.111.100'  # PowerShell

python test_oscilloscope.py --quick  # connection + health check only
python test_oscilloscope.py --bench  # read-rate benchmark
python test_oscilloscope.py --demo   # usage examples
```

The **health check** verifies the socket, `*IDN?` (expects an SDS), `SARA?`,
`TDIV?`, and one `PAVA?` read — enough to confirm the link is alive even with
no signal on the probed channel.

## Troubleshooting

### Connection Issues
- Confirm the scope's static IP and that the PC is on the same subnet
- `ping <scope IP>`; the SCPI socket is on port `5025`
- Set `SCOPE_IP` / `SCOPE_PORT` if not using the default IP
- Only one socket client at a time — close other sessions / the web control page
- The manual's LAN setup: `Utility > System Setting > I/O Setting > LAN Config`

### Measurement Returns NaN / `status=1`
- The scope returned `****` — no valid measurement on that source
- Check the probe / BNC connection and that a signal is actually present
- Make sure the trace is on-screen and triggered; PAVA measures the live acquisition

### Cross-Channel Phase Looks Wrong
- Both channels must be displayed and have a common, stable frequency
- Use the `C1-C2` source ordering deliberately — the sign depends on it

### Grounding (Isolation Tests)
- The AWG shares chassis ground with all probe clips; grounding to an isolated
  node shorts the isolation under test. Reference all clips to the same
  non-isolated node.

## API Reference

### Core Functions
- `quick_measure(source, param, count, host=None)` — connect, read `count`, disconnect
- `measure_with_callback(callback, source, param, ...)` — continuous with per-sample callback
- `codes_to_volts(codes, vdiv, offset, codes_per_div=25.0)` — DAT2 codes → volts

### Defaults (module constants)
- `DEFAULT_HOST = "169.254.111.100"`, `DEFAULT_PORT = 5025`
- `DEFAULT_TIMEOUT_S = 5.0`
- `CODES_PER_DIV = 25.0` *(verify against the Programming Guide)*

### Key SCPI Commands Used
`*IDN?`, `*CLS`, `*OPC?`, `CHDR OFF`, `<src>:PAVA? <param>`, `SARA?`, `TDIV?`,
`CLSW`, `<src>:BSWV ...`, `<src>:OUTP ...`, `WFSU`, `<src>:WF? DAT2`,
`<src>:VDIV?`, `<src>:OFST?`.

## License

Provided as-is for educational and research purposes.

## Author

Developed for instrumentation control at the University of Michigan Robotics — HDR Lab.
