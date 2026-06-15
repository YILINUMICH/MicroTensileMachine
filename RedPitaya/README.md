# Red Pitaya Driver (`redpitaya.py`)

> **Status: Draft — not yet hardware-validated.** The command sequences follow
> the documented STEMlab 125-14 SCPI set, but the binary-transfer path and the
> trigger-delay value should be confirmed on a real board before production use.
> Validate measured impedance against the Keysight E4980 on known standards.

A deliberately thin SCPI driver for the Red Pitaya STEMlab 125-14. It does two
things and nothing more: **generate a sine** and **capture two raw voltage
waveforms phase-coherently** with that sine. It knows nothing about impedance,
inductance, or resistance. All of that math lives on the host, where you control
the calibration and the model.

This is the intended replacement for the bench LCR meter in the SMA-coil
characterisation system. The board takes over the *signal* role (excite + digitise);
the *measurement* role (turn voltages into R and L) moves to host-side code.

## Why a raw-voltage driver instead of the board's LCR mode

The Red Pitaya ships an on-board `LCR:*` mode that returns `L_s` / `C_s` / `R_s`
already computed from impedance. We don't use it, for three reasons that matter
specifically for measuring a small-impedance SMA coil (a couple of ohms, a few
hundred nH):

- **Shunt control.** The packaged LCR/extension path bottoms out at a 10 Ω sense
  shunt, which is a poor match for a ~2 Ω DUT. With raw capture you pick your own
  small precision shunt.
- **Calibration ownership.** The board's de-embedding is fixed and opaque. You
  already de-embed post-hoc; doing the impedance math on the host means the
  short/load compensation is yours and matches the existing workflow.
- **Processing gain.** A long single-bin DFT over the 16384-sample buffer is a
  narrowband filter worth tens of dB. That's how a 200 µV tone on a small DUT
  becomes a clean reading — and it's a host-side decision, not a board mode.

The board, in short, is the front end. The brains stay on the PC.

## What it measures (and the topology it assumes)

The standard voltage/current method: drive a series chain of a **sense shunt**
`R_sh` and the **DUT**, with the same current through both.

```
   OUT1 ──[ R_sh ]──┬──[ DUT ]── GND
                    │            │
              IN2 ──┘      IN1 ──┘     (Kelvin-sense the DUT terminals)
```

- **IN2** reads the voltage across the shunt → proportional to current `I = V_shunt / R_sh`.
- **IN1** reads the voltage across the DUT (use a 4-wire / Kelvin tap; the 1 MΩ
  inputs load it negligibly).
- The host then computes `Z = R_sh · V_dut / V_shunt`, and from `Z(ω)` fits
  `R` and `L`.

`Capture.ch1` is IN1, `Capture.ch2` is IN2 — wire accordingly, or just track
which is which in your processing layer.

## Hardware setup notes

- **Input range / jumpers.** Use the **LV** jumper position (±1 V). For best
  small-signal resolution you can fit the divider-bypass jumpers (middle two
  pins on each input), which drops the input range to **±0.5 V** — if you do,
  keep generator `amplitude + offset ≤ 0.5 V` (absolute max 0.75 V).
- **Output drive.** The fast output is ±1 V into 50 Ω with ~50 Ω source
  impedance, so usable current is up to ~20 mA. That source impedance cancels in
  the V/I ratio, so it affects signal level but not the impedance result.
- **Shunt choice.** Pick `R_sh` near `|Z|` of the DUT for best sensitivity
  (a few ohms here), low-inductance, and calibrate its value.
- **Fixture inductance is the parasitic that bites.** A few cm of lead is
  tens of nH — a large fraction of a few-hundred-nH coil. Take a **SHORT**
  standard and subtract its residual R+L on the host. Take a clean resistive
  **LOAD** standard to calibrate channel-to-channel phase/gain (this is what
  makes the extracted `L` trustworthy).
- **Self-heating.** The driver disables the output immediately after each
  capture so drive current doesn't park on the thermally sensitive coil.

## Requirements

- A host with **Python 3.9+** and **NumPy**. No other dependencies.
- Red Pitaya **OS 2.00+** with the **SCPI server running** (start it from the
  web interface, or `systemctl start redpitaya_scpi` over SSH).
- Host and board reachable over the network (hostname like `rp-f0a235.local`
  or an IP).

```bash
pip install numpy
```

## Quick start

```python
from redpitaya import RedPitaya

with RedPitaya("rp-f0a235.local") as rp:
    cap = rp.capture(freq=1e6, ampl=0.5)   # phase-coherent dual-channel grab
    print(rp.idn, cap.freq, cap.fs, cap.n, cap.periods)
    # cap.ch1, cap.ch2 are NumPy voltage arrays -> feed your impedance code
```

Command-line smoke test:

```bash
python redpitaya.py rp-f0a235.local --freq 1e6 --ampl 0.5
# add --binary to test the fast transfer path
```

## API

### `RedPitaya(host, port=5000, timeout=10.0, gen_channel=1)`

Construct (does not connect). Use as a context manager, or call `.connect()`.

| Method | Purpose |
|---|---|
| `connect()` | Open the SCPI socket, read `*IDN?`. Returns self. |
| `close()` | Disable output, stop acquisition, close socket. |
| `reset()` | `GEN:RST` + `ACQ:RST`. |
| `capture(freq, ampl, offset=0.0, decimation=None, binary=False, settle_s=0.0)` | The one primitive — generate a tone and capture IN1/IN2 phase-coherently. Returns a `Capture`. |
| `capture_sweep(freqs, ampl, **kwargs)` | `capture` at each frequency; returns a list of `Capture`. |
| `snap_frequency(freq, fs, n=16384)` *(static)* | Nearest exact DFT-bin frequency (leakage-free). |
| `auto_decimation(freq, target_periods=100)` *(static)* | Smallest decimation whose window holds ≥ target periods. |

`capture()` parameters worth knowing:

- **`decimation`** — `None` auto-picks for ~100 periods in the window. Set
  explicitly to control time-window length and bin spacing.
- **`binary`** — `False` (ASCII) is the robust default; `True` transfers
  float32 little-endian, much faster on the wire. **Validate the binary path on
  your board first.**
- The requested `freq` is **snapped to the nearest DFT bin**; the actual value
  is in `Capture.freq`. **Always analyse at `Capture.freq`, not what you asked
  for** — that's what keeps the single-bin DFT leakage-free.

### `Capture`

| Field | Meaning |
|---|---|
| `freq` | Exact generated frequency (bin-snapped). Analyse here. |
| `requested_freq` | What the caller asked for. |
| `ampl` | Generator amplitude (V). |
| `fs` | Effective sample rate after decimation (Hz). |
| `decimation`, `n` | Decimation factor; samples per channel. |
| `ch1`, `ch2` | IN1 / IN2 voltage arrays (NumPy float64). |
| `host_timestamp_s` | `time.time()` at read — for cross-instrument joins. |
| `monotonic_s` | `time.monotonic()` at read — drift-free timing. |
| `t` *(property)* | Sample time vector (s). |
| `periods` *(property)* | Excitation periods in the window. |

## How phase-coherent capture works

Impedance needs the **phase** between voltage and current, so the ADC window
must start at a known point in the generated waveform. The driver arms
acquisition on the **AWG start edge** (`ACQ:TRig AWG_PE`), then triggers the
generator — so every capture begins at the same phase of the excitation. It also
sets the trigger delay to the buffer mid-point so that all 16384 samples are
*post-trigger* (pure steady tone), rather than the default where the first half
is pre-generation baseline. Combined with bin-snapping the frequency, this gives
a clean rectangular-window DFT with no leakage and a stable phase reference.

## Where the impedance math plugs in (host side)

The driver stops at raw volts. A separate processing layer turns a `Capture`
into impedance. The seam looks like this (illustrative — the real module adds
de-embedding and a multi-frequency `R + jωL` fit):

```python
import numpy as np

def goertzel(x, f, fs):
    """Single-bin DFT -> complex amplitude at exactly f."""
    n = np.arange(x.size)
    return np.sum(x * np.exp(-2j * np.pi * f * n / fs))

def impedance(cap, r_shunt):
    V_dut = goertzel(cap.ch1, cap.freq, cap.fs)   # IN1
    V_sh  = goertzel(cap.ch2, cap.freq, cap.fs)   # IN2
    I = V_sh / r_shunt
    Z = V_dut / I                                  # source impedance cancels here
    return Z

# across a sweep:  Z(ω) -> fit R = Re, L = Im/ω  (after short/load de-embedding)
```

Because the impedance is reconstructed from the V/I ratio, the DAC's ~50 Ω output
impedance and any amplitude error cancel. What you *do* calibrate out on the host
is the fixture (SHORT standard → residual R+L) and the inter-channel phase/gain
(LOAD standard). Stepping several frequencies and fitting `Z(ω) = R + jωL` gives
robust `R` and `L` and reveals skin-effect rise in `R` rather than hiding it.

## Integration with the recorder workers

This driver is the producer side of an LCR-style worker. The existing
`LcrWorker` pattern (a thread that pulls samples and pushes timestamped
dataclasses onto a queue) maps directly, with the impedance step inserted
between capture and push:

```
RedPitaya.capture(f) ── raw volts ──► impedance()/de-embed ──► R, L ──► queue
```

If you want a drop-in for the current pipeline, wrap this so the worker still
emits `(primary=L, secondary=R, status=0)` samples and nothing downstream
(controller, CSV writer) changes. If you'd rather keep the raw buffers for later
reprocessing, push a richer sample type and move the math fully into
post-processing — your call. Either way, `redpitaya.py` stays unchanged; only
the processing/worker layer differs.

## Performance notes

- **Per-capture cost** ≈ configure + arm + wait-for-fill + transfer two 16384
  buffers + (host DFT). The transfer dominates; flip `binary=True` to cut it
  substantially once validated.
- **Decimation** trades window length (periods, bin resolution) against capture
  time and self-heating. For MHz tones `dec=1` already gives 100+ periods.
- **Benchmark the real per-sample rate.** It may land below the E4980's ~24/s.
  The recorder's queue/drop model tolerates a slower producer, but check it
  against the H7 stream cadence.

## Troubleshooting

- **`acquisition trigger timeout (no AWG edge?)`** — the generator never
  produced a start edge the acquisition could see. Confirm the output enabled
  (`OUTPUT1:STATE ON`) and that OS supports `ACQ:TRig AWG_PE` (2.00+).
- **Connection refused / closed** — SCPI server not running on the board, wrong
  host/IP, or another web app holding the FPGA. Never run a Red Pitaya web app
  (scope, etc.) at the same time as the SCPI server.
- **Garbled binary data** — confirm `ACQ:DATA:BYTE:ORDER LEND` and float32
  framing on your OS version; fall back to `binary=False` to isolate.
- **Implausible R or L** — almost always fixture parasitics or channel phase
  mismatch. Re-run the SHORT and LOAD standards and check the de-embedding.

## License / provenance

Drafted for instrumentation control in the SMA-coil characterisation work,
HDR Lab. Provided as-is; validate against a reference instrument before trusting
measurements.
