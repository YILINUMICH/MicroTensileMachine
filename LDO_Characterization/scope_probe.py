#!/usr/bin/env python3
"""
scope_probe.py — diagnose the waveform-capture path. No H7 needed.

Connects to the scope, enables C1-C3, free-runs, and tries to pull a waveform
off each channel, printing the sample count + preamble. If a channel reports
0 samples, that's why the run produced header-only CSVs.

    python scope_probe.py
"""
import sys
import time
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "SiglentOscillosope"))
from oscilloscope import Oscilloscope          # noqa: E402
from scope_trigger import siglent_time         # noqa: E402

cfg = yaml.safe_load(open(_HERE / "config.yaml"))
sc = cfg["scope"]
scope = Oscilloscope(host=sc.get("host"), port=sc.get("port", 5025), auto_open=False)
ok = scope.connect(sc["host"]) if sc.get("host") else scope.auto_connect()
print("connected:", ok, "| IDN:", scope.idn)

chans = [sc["channels"][k] for k in ("trigger", "dac", "output")]
print("channels under test:", chans)

# 1. report + force each channel ON (TRA = trace display)
for ch in chans:
    try:
        state = scope.query(f"{ch}:TRA?")
    except Exception as e:
        state = f"<query err {e}>"
    print(f"  {ch}:TRA? -> {state!r}")
    scope.write(f"{ch}:TRA ON")

# 2. free-run so the acquisition memory has fresh data
scope.write("TRMD AUTO")
scope.write(f"ACQ:MDEP {sc.get('memory_depth', '10K')}")   # set memory first ...
scope.write(f"TDIV {siglent_time(sc['timebase_s'])}")      # ... then timebase (value+unit!)
time.sleep(1.0)
print("SARA?:", scope.query("SARA?").strip(), "| TDIV?:", scope.query("TDIV?").strip(),
      "| MDEP?:", scope.query("ACQ:MDEP?").strip())

# Freeze the frame before reading — multi-channel WF? must come from a STOPPED
# acquisition, else channels read mid-overwrite come back empty.
scope.write("STOP")
time.sleep(0.3)

# 3. capture each channel in order — from the frozen frame all three should
#    now return ~10000 samples.
for ch in chans:
    try:
        codes, pre = scope.capture_waveform(ch)
        print(f"  {ch}: {len(codes)} samples  vdiv={pre.vdiv} ofst={pre.offset} sara={pre.sample_rate}")
    except Exception as e:
        print(f"  {ch}: CAPTURE ERROR -> {type(e).__name__}: {e}")

# 4. completion-status verbs — what does THIS scope return? (fixes the WARN)
print("--- trigger/acq status verbs (for wait_capture_complete) ---")
for q in ("SAST?", "INR?", "TRMD?"):
    try:
        print(f"  {q} -> {scope.query(q)!r}")
    except Exception as e:
        print(f"  {q} -> <err {e}>")

# 5. trigger config via the MODERN :TRIGger subsystem, then read back to confirm
#    the source is really C1 (the legacy TRSE wasn't selecting it).
print("--- trigger config (:TRIGger subsystem) + readback ---")
scope.write(":TRIGger:TYPE EDGE")
scope.write(":TRIGger:EDGE:SOURce C1")
scope.write(":TRIGger:EDGE:SLOPe RISing")
scope.write(":TRIGger:EDGE:LEVel 1.0V")   # value+UNIT — bare '1.0' is silently ignored
scope.write(":TRIGger:MODE SINGle")
for q in (":TRIGger:EDGE:SOURce?", ":TRIGger:EDGE:LEVel?", "C1:TRLV?",
          ":TRIGger:EDGE:SLOPe?", ":TRIGger:MODE?"):
    try:
        print(f"  {q} -> {scope.query(q)!r}")
    except Exception as e:
        print(f"  {q} -> <err {e}>")

scope.close()
print("done.")
