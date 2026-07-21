# SDS2000X Plus — Triggered FFT from a Python Script

**Purpose:** A task spec for an agent (or engineer) to remotely control a Siglent
SDS2000X Plus oscilloscope from Python, arm a single trigger, capture a waveform,
and produce an FFT spectrum. Written to be handed off and executed end-to-end.

**Target instrument:** Siglent SDS2000X Plus series digital oscilloscope.
**Interfaces:** SCPI over LAN (VISA `TCPIP::INSTR`, or raw socket port **5025**), USB-TMC, or GPIB.
The user manual confirms remote control via SCPI over NI‑VISA, Telnet, or Socket and points
to the *Programming Guide* for the command list (see References).

---

## 0. TL;DR / Recommended approach

There are two ways to get an FFT. **Default to Option B unless the task explicitly
requires the scope's own FFT trace.**

| | Option A — scope computes FFT | Option B — Python computes FFT (recommended) |
|---|---|---|
| What you read back | The MATH/FFT trace (`F1`/`F2`) | The raw time‑domain channel waveform |
| Pros | Matches on‑screen spectrum exactly (window, span, dBVrms/dBm, peak table) | Robust, portable across firmware; full control of window, zero‑pad, scaling, averaging |
| Cons | Exact FFT SCPI mnemonics are firmware‑dependent and finicky to read back | You reproduce windowing/scaling yourself |
| Use when | You must mirror the instrument's displayed spectrum | Almost everything else |

Option B is the primary reference implementation below. Option A is documented as a variant.

---

## 1. Prerequisites

- Instrument reachable on the network. Confirm the IP under `Utility ▸ I/O ▸ LAN`
  on the scope (or set a static IP). Ping it first.
- Python 3.9+.
- Packages:
  ```bash
  pip install pyvisa numpy
  # Backendless option (no NI-VISA install needed):
  pip install pyvisa-py
  ```
  With `pyvisa-py` you need no vendor VISA runtime. For USB‑TMC also add `pyusb`;
  for pure‑socket you can skip VISA entirely (see §3.3).
- Know the probe channel (e.g. `C1`) and roughly the signal frequency so you can
  set timebase/sample rate to satisfy Nyquist with headroom.

---

## 2. Connection strings

Pick one resource form for `pyvisa`:

- **VISA / LAN (recommended):** `TCPIP0::<IP>::inst0::INSTR`
- **Raw socket / LAN:** `TCPIP0::<IP>::5025::SOCKET`  (set `read_termination='\n'`, `write_termination='\n'`)
- **USB‑TMC:** `USB0::0xF4EC::<PID>::<SERIAL>::INSTR`  (get exact string from `pyvisa` `list_resources()`)

Sanity check the link with `*IDN?` before anything else — it should return a
`Siglent Technologies,SDS2...` identity string.

Recommended session setup once connected:
```python
scope.timeout = 15000          # ms; waveform reads can be slow
scope.write("CHDR OFF")        # strip command headers from query responses (easier parsing)
```

---

## 3. Core workflow (Option B — trigger, read raw waveform, FFT in numpy)

### 3.1 SCPI sequence for a single, deterministic acquisition

Use these mnemonics (current SDS SCPI set, EN11x). **Verify against the Programming
Guide for the unit's firmware — a few names differ between the legacy and the newer
command set; both are listed where relevant.**

1. Configure vertical/horizontal for the signal (volts/div, timebase, sample rate).
   - `:ACQuire:SRATe?` — query the actual sample rate `Fs` (needed for the frequency axis).
   - `:ACQuire:POINts?` — query the memory depth actually captured.
2. Prefer **AC coupling** on the source if you don't care about DC — a large DC bin
   near 0 Hz otherwise dominates the spectrum (manual's explicit FFT note):
   - `C1:CPL A1M` (legacy)  **or**  `:CHANnel1:COUPling AC` (new set).
3. Arm a single shot and wait for completion:
   - `:TRIGger:MODE SINGle`  (legacy equivalent: `TRMD SINGLE`)
   - `:TRIGger:RUN`  (or `ARM`) — start the single acquisition.
   - Poll `:TRIGger:STATus?` until it returns `Stop` (acquisition captured and halted),
     **or** gate on `INR?` bit 0 (a fresh trigger since last `INR?` read),
     **or** use `*OPC?` after arming if the firmware honors it.
   - Do **not** just sleep — poll. Add a wall‑clock timeout so the script can't hang
     if no trigger ever arrives.

### 3.2 Read the waveform

Set the read window, then transfer:

- `:WAVeform:SOURce C1`
- `:WAVeform:STARt 0`
- `:WAVeform:POINt 0`      → `0` means "all"; or set an explicit count.
- `:WAVeform:WIDTh WORD`   → **16‑bit** samples. Use WORD for FFT dynamic range
  (BYTE = 8‑bit only gives ~48 dB). SDS2000X Plus ADC is 8‑bit, but WORD packs the
  full internal code and reads back cleaner for spectral work.
- `:WAVeform:PREamble?`    → returns the binary descriptor (WAVEDESC). Parse from it:
  `vertical_gain`, `vertical_offset`, `horizontal_interval`, `horizontal_offset`.
- `:WAVeform:DATA?`        → returns an IEEE‑488.2 definite‑length binary block
  (`#` + digit count + byte count + payload). Strip the header, then decode.

Legacy equivalent (works on virtually all firmware, useful fallback):
- `WFSU SP,0,NP,0,FP,0`  then  `C1:WF? DAT2`  — same binary‑block format.

**Raw → engineering units:**
```
volts[i]  = raw_code[i] * vertical_gain - vertical_offset
time[i]   = horizontal_offset + i * horizontal_interval        # dt = horizontal_interval
Fs        = 1 / horizontal_interval                             # cross-check vs :ACQuire:SRATe?
```
> The exact byte offsets of the WAVEDESC fields are documented in the Programming Guide's
> waveform/template section. Rather than hardcode offsets, prefer reading the fields the
> `:WAVeform:PREamble?` response exposes; if parsing the raw descriptor, confirm offsets
> against the guide for the installed firmware.

### 3.3 Compute the FFT

```python
import numpy as np

def compute_fft(volts, fs, window="hann"):
    n = len(volts)
    v = volts - np.mean(volts)                 # remove DC so it doesn't smear bin 0
    if window == "hann":
        w = np.hanning(n)
    elif window == "flattop":                  # best amplitude accuracy for tones
        from scipy.signal import windows
        w = windows.flattop(n)
    else:
        w = np.ones(n)
    # Coherent gain correction so amplitudes read true
    cg = np.mean(w)
    spec = np.fft.rfft(v * w) / (n * cg)
    spec[1:] *= 2                              # single-sided: fold negative freqs
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    amp_vpk   = np.abs(spec)                    # volts peak per bin
    amp_vrms  = amp_vpk / np.sqrt(2)
    amp_dbvrms = 20 * np.log10(np.maximum(amp_vrms, 1e-15))
    return freqs, amp_vpk, amp_vrms, amp_dbvrms
```

Notes:
- **Window choice:** Hann for general spectral shape; flat‑top when you need accurate
  *amplitude* of discrete tones; rectangular (`ones`) only for exactly‑periodic captures.
- **Frequency resolution** = `Fs / N`. Increase capture length (memory depth) or lower
  `Fs` for finer bins; keep `Fs > 2×` the highest frequency of interest (Nyquist).
- **dBm:** `dBm = 10*log10( (Vrms^2 / R) / 1e-3 )`, with `R` the load (50 Ω typical).

---

## 4. Reference script (Option B, end‑to‑end)

> Fill in `SCOPE` and `CHANNEL`. This uses the new SCPI set with a legacy fallback for
> the waveform read. Test on the bench and adjust mnemonics per the Programming Guide if
> a command errors.

```python
import time
import numpy as np
import pyvisa

SCOPE   = "TCPIP0::192.168.1.100::inst0::INSTR"   # <-- your scope IP
CHANNEL = "C1"
TIMEOUT_S = 10.0

def parse_ieee_block(raw: bytes) -> bytes:
    assert raw[:1] == b"#", "not a definite-length block"
    ndig = int(raw[1:2])
    nbytes = int(raw[2:2 + ndig])
    start = 2 + ndig
    return raw[start:start + nbytes]

def arm_single(scope, timeout_s=TIMEOUT_S):
    scope.write(":TRIGger:MODE SINGle")
    scope.write(":TRIGger:RUN")
    t0 = time.time()
    while True:
        status = scope.query(":TRIGger:STATus?").strip()
        if status.lower().startswith("stop"):     # captured and halted
            return
        if time.time() - t0 > timeout_s:
            raise TimeoutError(f"no trigger within {timeout_s}s (status={status})")
        time.sleep(0.05)

def read_channel(scope, ch):
    scope.write(f":WAVeform:SOURce {ch}")
    scope.write(":WAVeform:STARt 0")
    scope.write(":WAVeform:POINt 0")
    scope.write(":WAVeform:WIDTh WORD")
    # --- descriptor (query the fields your firmware exposes) ---
    vgain   = float(scope.query(f"{ch}:INSPECT? 'VERTICAL_GAIN'").split()[-1].strip("'\""))    \
              if False else None   # placeholder; prefer :WAVeform:PREamble? parse
    # Robust path: use the legacy INSPECT helpers if present, else parse PREamble.
    # Query real values directly where supported:
    vdiv   = float(scope.query(f"{ch}:VDIV?").split()[-1].rstrip("V"))
    voffs  = float(scope.query(f"{ch}:OFST?").split()[-1].rstrip("V"))
    tdiv   = float(scope.query("TDIV?").split()[-1].rstrip("S"))     # if supported
    fs     = float(scope.query(":ACQuire:SRATe?").split()[-1].rstrip("Sa/s"))
    # --- data ---
    scope.write(":WAVeform:DATA?")
    raw = scope.read_raw()
    payload = parse_ieee_block(raw)
    codes = np.frombuffer(payload, dtype="<i2")           # WORD, little-endian
    # SDS2000X Plus: 30 codes per division for 8-bit path scaled to 16-bit container.
    # Convert with the descriptor-provided gain when available; the vdiv/code form:
    volts = codes * (vdiv / 30.0 / 256.0) - voffs         # VERIFY factor vs Programming Guide
    return volts, fs

def main():
    rm = pyvisa.ResourceManager()
    scope = rm.open_resource(SCOPE)
    scope.timeout = 15000
    scope.write("CHDR OFF")
    print(scope.query("*IDN?").strip())

    scope.write(f"{CHANNEL}:CPL A1M")     # AC coupling: kill the DC bin (optional)
    arm_single(scope)
    volts, fs = read_channel(scope, CHANNEL)

    freqs, vpk, vrms, dbv = compute_fft(volts, fs, window="flattop")
    peak = int(np.argmax(vpk[1:]) + 1)
    print(f"Fs={fs/1e6:.3f} MHz  N={len(volts)}  bin={fs/len(volts):.1f} Hz")
    print(f"Peak: {freqs[peak]/1e3:.3f} kHz  {vrms[peak]*1e3:.2f} mVrms  {dbv[peak]:.1f} dBVrms")

    # Optional: save spectrum
    np.savetxt("fft.csv", np.column_stack([freqs, vrms, dbv]),
               delimiter=",", header="freq_Hz,Vrms,dBVrms", comments="")

if __name__ == "__main__":
    main()
```

> ⚠️ **The raw→volts scale factor is the one thing to verify on the bench.** Siglent's
> published conversion is `volts = code * (vdiv / <codes_per_div>) - offset`. The
> `codes_per_div` and the WORD scaling depend on model/firmware. The bulletproof method
> is to read `vertical_gain`/`vertical_offset` straight from `:WAVeform:PREamble?` and use
> `volts = code * vertical_gain - vertical_offset` — no magic constants. Confirm with the
> verification step in §6 before trusting amplitudes.

---

## 5. Option A variant — read the scope's own FFT trace

If the task requires the instrument-computed spectrum (matching the display, peak table,
dBm calc, chosen window/span):

1. Turn on a MATH function and set its operator to FFT via the `:FUNCtion` command tree
   (select `F1`/`F2`, operation = FFT, source = `C1`, window, center/span or start/end,
   unit = `dBVrms|Vrms|dBm`). **Look up the exact `:FUNCtion` FFT mnemonics in the
   Programming Guide — they are firmware‑specific and not reliably guessable.** You can
   also set it up once by hand on the scope and only *read* it from Python.
2. Arm/trigger as in §3.1.
3. Read the math trace:
   - `:WAVeform:SOURce F1` (or `MATH`), then `:WAVeform:PREamble?` and `:WAVeform:DATA?`
     as in §3.2. The FFT preamble carries the frequency‑domain scaling (Hz/point,
     dB or Vrms per code) — use it, don't assume time‑domain scaling.

This path is more fragile than Option B; prefer it only when "must equal the on‑screen
spectrum" is a hard requirement.

---

## 6. Verification (do this before trusting results)

1. **Known‑tone test:** Feed a known sine (e.g. scope's built‑in cal/AWG, or a signal
   generator) at a known frequency and amplitude into `CHANNEL`.
   - The FFT peak bin must land at the injected frequency (±1 bin = `Fs/N`).
   - With a **flat‑top** window, `vrms[peak]` must match the injected RMS within ~1–2 %.
     If amplitude is off by a constant factor, the §4 raw→volts scale is wrong — switch
     to the `:WAVeform:PREamble?` gain/offset method.
2. **Nyquist sanity:** Confirm `Fs/2` exceeds your highest frequency of interest; if the
   tone appears at a *lower* mirror frequency, you're aliasing — raise `Fs`.
3. **DC check:** With DC coupling and no mean removal, expect a large bin 0; confirm AC
   coupling / mean subtraction removes it.
4. **Cross-check bin math:** Print `Fs`, `N`, and `Fs/N`; confirm the resolution matches
   the timebase/memory settings you commanded.

---

## 7. Pitfalls & gotchas

- **DC spike near 0 Hz** — set source coupling to AC or subtract the mean (manual's note).
- **Poll, don't sleep** — gate the read on `:TRIGger:STATus?`/`INR?`, with a timeout.
- **Binary block header** — `:WAVeform:DATA?`/`C1:WF?` returns `#9<len>...`; strip it
  before `np.frombuffer`. Use `read_raw()`, never `query()` (which assumes text).
- **Byte width/order** — WORD is little‑endian 16‑bit (`<i2`); match dtype to `:WAVeform:WIDTh`.
- **Memory depth vs FFT** — huge captures give fine bins but slow transfers and heavy FFTs;
  size `N` to the resolution you need.
- **Timeout too short** — long records over LAN can exceed the default VISA timeout; set
  `scope.timeout` generously (10–30 s).
- **Scale factor** — the single most common amplitude bug; verify via §6 and prefer
  preamble‑derived gain/offset over hardcoded constants.
- **Firmware command drift** — if a `:` SCPI command errors, try the legacy form
  (`TRMD`, `C1:WF?`, `WFSU`, `SARA?`, `TDIV?`, `VDIV?`, `OFST?`) or check the guide.

---

## 8. References

- Siglent **SDS Series Programming Guide** (SCPI command reference — trigger, waveform,
  function/FFT): EN11F — https://www.siglenteu.com/wp-content/uploads/dlm_uploads/2024/03/ProgrammingGuide_EN11F.pdf
- SDS2000X Plus Programming Guide PG01‑E11A (mirror): https://wiki.hackhitchin.org.uk/images/5/51/SDS2000X-Plus_ProgrammingGuide_PG01-E11A.pdf
- SDS2000X Plus **User Manual** (FFT / Frequency Analysis §19.4; remote‑control §31): https://www.batronix.com/files/Siglent/Oszilloskope/SDS2000X+/SDS2000X-Plus_UserManual_EN01C.pdf
- Siglent PyVISA/LAN programming examples (application notes): https://siglentna.com/application-notes/digital-oscilloscopes/sds2000xp/

---

*Handoff note for the executing agent:* Start with §6 verification against a known tone —
it validates the connection, the trigger gating, and the amplitude scaling in one shot.
Once a known sine reads back at the right frequency and RMS, the rest of the pipeline is
trustworthy. Treat every SCPI mnemonic marked "verify" as a bench check against the
Programming Guide for the exact firmware on the instrument.
