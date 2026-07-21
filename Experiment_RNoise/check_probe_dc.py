#!/usr/bin/env python3
"""One post-reset shot: confirm the H7 is ARMED and DRIVING, then read the DC
that the scope actually sees on C1/C2.

Answers one question only: with real current confirmed flowing, do the probes
see the LDO rail and the INA296A output?

Everything is done in a single run because the H7 reliably serves commands only
for a short window after reset. Scope readings are PAVA? MEAN, which come from
the instrument's own measurement engine and therefore do NOT depend on the
unverified CODES_PER_DIV constant in the driver.

Run the scope setup BEFORE this (it takes ~30 s and starves the H7):
    python capture_phase2.py --setup --coupling D1M --vdiv 500MV 200MV

Then press reset and immediately:
    python check_probe_dc.py [--drive 0.85]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Driver_SiglentOscilloscope"))

import capture_phase2 as cap                      # noqa: E402
from oscilloscope import Oscilloscope             # noqa: E402


def mean_v(scope, ch):
    reply = scope.query(f"{ch}:PAVA? MEAN").strip()
    return Oscilloscope._extract_num(reply)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drive", type=float, default=0.85)
    ap.add_argument("--r-load", type=float, default=4.9)
    ap.add_argument("--hold-ms", type=int, default=30000)
    args = ap.parse_args()

    with cap.H7Drive(port=cap.H7_PORT_DEFAULT, r_load=args.r_load) as d:
        with Oscilloscope() as scope:
            if not scope.auto_connect():
                print("!! scope unreachable", file=sys.stderr)
                return 1

            idle = {ch: mean_v(scope, ch) for ch in (cap.VLDO_CH, cap.VSENSE_CH)}
            print(f"\nBEFORE drive:  {cap.VLDO_CH}={idle[cap.VLDO_CH]:+.4f} V   "
                  f"{cap.VSENSE_CH}={idle[cap.VSENSE_CH]:+.4f} V")

            d.start(args.drive, args.hold_ms)

            # Prove the board is actually armed -- 'drive' is a no-op otherwise,
            # and an unconfirmed drive makes every reading below meaningless.
            armed = None
            for line in d._send("info", wait_s=0.8):
                if "armed" in line.lower():
                    armed = line
                    print(f"      {line}")
            if armed is None:
                print("!! no 'info' reply -- H7 is not serving commands. "
                      "Power-cycle and retry; do NOT interpret anything below.")
                return 2
            if "YES" not in armed.upper():
                print("!! board reports NOT ARMED -- no current can flow. "
                      "Readings below are of an idle rig.")
                return 3

            live = [l for l in d._send("read", wait_s=0.8) if "V_LDO=" in l]
            if not live:
                print("!! no 'read' reply -- cannot confirm the drive. Stop here.")
                return 4
            print(f"      firmware: {live[0]}")
            v_fw = cap._num_after(live[0], "V_LDO=")
            i_fw = cap._num_after(live[0], "I=")

            time.sleep(0.5)
            drv = {ch: mean_v(scope, ch) for ch in (cap.VLDO_CH, cap.VSENSE_CH)}
            print(f"DURING drive:  {cap.VLDO_CH}={drv[cap.VLDO_CH]:+.4f} V   "
                  f"{cap.VSENSE_CH}={drv[cap.VSENSE_CH]:+.4f} V")

            print("\n--- verdict (scope vs firmware, both with current CONFIRMED) ---")
            print(f"  firmware V_LDO = {v_fw:.4f} V, I = {i_fw/1000:.4f} A")
            for ch, exp, name in ((cap.VLDO_CH, v_fw, "V_LDO"),
                                  (cap.VSENSE_CH, i_fw / 1000.0, "Vsense (1 V/A)")):
                got = drv[ch] - idle[ch]          # change caused by the drive
                print(f"  {ch}: scope moved {got:+.4f} V, firmware says "
                      f"{exp:.4f} V for {name}", end="  ")
                if exp and abs(got) < 0.2 * abs(exp):
                    print("-> PROBE NOT ON THIS NODE (no response to real current)")
                elif exp and abs(got / exp - 1) > 0.15:
                    print(f"-> scaled by {got/exp:.2f}x (divider? or codes/div)")
                else:
                    print("-> matches")

            d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
