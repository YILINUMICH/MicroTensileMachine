#!/usr/bin/env python3
"""wiggle_monitor.py — live glitch-rate display for the 2026-08-07 sense-fault hunt.

OPEN INVESTIGATION (unusually for diagnostics/, which holds closed ones): the
SMA sense chain glitches upward on both sma_v and sma_i — single-sample,
Poisson-random, ~200/s — and the fault is pinned to the DRIVER-BOARD side:
  - survives the coil being disconnected        (not the wire/clips)
  - vanishes with A0+A1 grounded at the pins    (not the H7 ADC/ref/supply)
  - identical on a brand-new H7                 (not the board at all)
Remaining suspects: INA296A OUT -> A1 lead, 10k/10k FB divider -> A0 lead,
their supply, and the shared ground return. Full chain: STATUS.md 2026-08-07.

WHAT THIS DOES
    Arms the drive at the 0.5 V idle bias (~125 mA through the coil — the same
    state every capture idles in) and prints one line per second with the
    glitch count. Wiggle ONE element at a time; the element whose movement
    swings the count is the culprit. Faulted baseline: ~200 glitches/s.
    Healthy: ~0-2. Ctrl-C stops and disarms.

USAGE (from anywhere; port defaults to COM8)
    python diagnostics/wiggle_monitor.py [COMx]

After a fix, verify with a real capture, not this monitor:
    python operator_current_sweep.py --profile profiles/corner_probe_reseat.json
    python operator_sense_check.py data/raw/sweep_<stamp>
"""
import sys
import time
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))

import numpy as np                      # noqa: E402
from lib_h7_session import H7           # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM8"

h7 = H7(PORT, verbose=False)
print(f"opening {PORT} ...")
h7.open()
h7.send("disarm")
time.sleep(0.4)
h7.send("arm")
time.sleep(0.6)
print("armed at idle bias. Wiggle one element at a time. Ctrl-C stops.\n")
print(f"{'t':>4s} | {'i n':>5s} {'i sig':>6s} {'i>200mA':>8s} {'i max':>6s}"
      f" | {'v n':>5s} {'v sig':>6s} {'v>1V':>6s}")

t0 = time.time()
try:
    while True:
        cap = h7.capture(1.0, ping=True)
        i = np.array([s.value for s in cap.by_src(4)])
        v = np.array([s.value for s in cap.by_src(3)])
        gi = int((i > 0.200).sum()) if len(i) else 0
        gv = int((v > 1.0).sum()) if len(v) else 0
        print(f"{time.time()-t0:4.0f} | {len(i):5d} "
              f"{i.std()*1e3 if len(i) else 0:6.1f} {gi:8d} "
              f"{i.max()*1e3 if len(i) else 0:6.0f}"
              f" | {len(v):5d} {v.std()*1e3 if len(v) else 0:6.1f} {gv:6d}"
              + ("   <-- QUIET" if gi < 5 and len(i) > 500 else ""))
except KeyboardInterrupt:
    print("\nstopping.")
finally:
    h7.disarm()
    h7.close()
