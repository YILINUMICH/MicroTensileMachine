#!/usr/bin/env python3
"""
run_experiment.py — RETIRED (WI-4 of PLAN_sma_console.md).

The non-interactive one-shot runner has been superseded by sma_console.py.
Its drive/cycle one-shot is just the console with `n` set, and its
drain/meta/ping loop is now the shared recording_core.RecordingCore.

It is retired (not merely deprecated) because it built firmware command
strings inline and never sent `arm` — the rebuilt Firmware_SMASensorHub_PIO
REJECTS drive/fire/cycle while disarmed, so running the old code path would
silently do nothing (or worse). Use the console instead:

    python sma_console.py --headless      # scripted run (Ctrl+C to stop)
    python sma_console.py                  # interactive GUI console

Cycle parameters still come from config.yaml's `sma:` block (v_high, v_low,
fire_ms, cool_ms, n_cycles, wdt_ms); --headless arms, starts that cycle,
heartbeats, and disarms on exit automatically.

The original implementation remains in git history if you need to refer to it.
"""

from __future__ import annotations

import sys

_MESSAGE = __doc__.strip()


def _main() -> int:
    print(_MESSAGE, file=sys.stderr)
    print("\nrun_experiment.py is retired — exiting without touching hardware.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main())
