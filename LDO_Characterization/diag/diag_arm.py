#!/usr/bin/env python3
"""
diag_arm.py — isolate WHY SCPI single-shot arms but never fires.

Front-panel single-shot triggers fine on the C1 edge, but the automated
(SCPI-armed) path leaves SAST stuck at READY. That means the SCPI ARMING
configures the trigger differently from the front panel. This script makes the
difference visible:

  1. Dumps the CURRENT trigger config — set this up on the FRONT PANEL first
     (single, edge, src C1, rising, your level) so step 1 shows a KNOWN-GOOD
     config that you confirmed triggers manually.
  2. Runs arm_single() exactly as run_experiment does.
  3. Dumps the trigger config AGAIN + SAST?.  Diff against step 1 — whatever
     arm_single changed that breaks triggering shows up here (usually SOURce).
  4. Waits while you fire ONE pulse, then reports SAST? — 'Stop' = it triggered,
     'Ready'/'Arm' = it missed.  Fire from a SEPARATE `pio device monitor`:
         fire 1631 800 141
     (COM8 is shared — this script only touches the scope, not the H7, so the
      monitor can stay open.)

    python diag_arm.py
"""
import sys
import time
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "SiglentOscillosope"))
from oscilloscope import Oscilloscope          # noqa: E402
import scope_trigger as st                      # noqa: E402

# Trigger fields to read back. Mix of modern (:TRIGger) and legacy (Cn:TR*)
# so we catch whichever set the firmware actually honours.
FIELDS = [
    ":TRIGger:TYPE?",
    ":TRIGger:EDGE:SOURce?",
    ":TRIGger:EDGE:SLOPe?",
    ":TRIGger:EDGE:LEVel?",
    ":TRIGger:EDGE:COUPling?",   # HFREJ/LFREJ here would block a clean edge
    ":TRIGger:MODE?",
    "C1:TRCP?",                  # legacy trigger coupling
    "C1:TRSL?",                  # legacy slope
    "C1:TRLV?",                  # legacy level
    "SAST?",
]


def dump(scope, title):
    print(f"\n--- {title} ---")
    for q in FIELDS:
        try:
            print(f"  {q:28s} -> {scope.query(q)!r}")
        except Exception as e:
            print(f"  {q:28s} -> <err {type(e).__name__}: {e}>")


def main():
    cfg = yaml.safe_load(open(_HERE / "config.yaml"))
    sc = cfg["scope"]
    scope = Oscilloscope(host=sc.get("host"), port=sc.get("port", 5025),
                         timeout=sc.get("timeout_s", 2.0), auto_open=False)
    ok = scope.connect(sc["host"]) if sc.get("host") else scope.auto_connect()
    print("connected:", ok, "| IDN:", scope.idn)
    if not ok:
        return 2

    print("\n>>> STEP 1: set up the trigger on the FRONT PANEL now (single, edge,")
    print(">>> src C1, rising, ~1 V) and confirm it triggers manually, THEN press Enter.")
    input(">>> [Enter to read the known-good front-panel config] ")
    dump(scope, "STEP 1: front-panel (known-good) config")

    chans = sc["channels"]
    cap = st.CaptureConfig(
        trigger_src=chans["trigger"],
        trigger_level_v=sc["trigger_level_v"],
        trigger_slope=sc.get("trigger_slope", "POS"),
        timebase_s=sc["timebase_s"],
        codes_per_div=sc["codes_per_div"],
        memory_depth=sc.get("memory_depth", "10K"),
    )
    print("\n>>> STEP 2: arming via arm_single() (the automated path) ...")
    st.arm_single(scope, cap)
    dump(scope, "STEP 2: config AFTER arm_single() — diff vs step 1")

    print("\n>>> STEP 3: FIRE ONE pulse now from `pio device monitor`:")
    print(">>>     fire 1631 800 141")
    input(">>> [press Enter AFTER the pulse fires] ")
    # Give the single-shot a moment, then report.
    triggered = st.wait_for_stop(scope, timeout_s=3.0)
    print(f"\nRESULT: scope {'TRIGGERED (SAST=Stop)' if triggered else 'MISSED (still armed)'}")
    dump(scope, "STEP 3: config after fire")

    scope.close()
    print("\ndone. If SOURce in step 2 != C1, that token form is the bug "
          "(set_trigger_source now falls back automatically).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
