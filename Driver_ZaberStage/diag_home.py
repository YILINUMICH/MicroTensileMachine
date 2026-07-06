#!/usr/bin/env python3
"""
diag_home.py — Zaber home-direction / coordinate diagnostic
===========================================================

Bench tool to answer: "why does the console's home look like it's on the
OPPOSITE end of the stage from what Zaber Launcher does?"

Zaber's `axis.home()` (what the driver calls) uses the SAME firmware homing
routine as Launcher's Home button, so if they truly land on opposite ends the
cause is a firmware setting or a coordinate/sign convention — not the Python
call. This script prints everything needed to decide, WITHOUT moving the stage
unless you explicitly pass --home.

Usage (from Driver_ZaberStage/):
    python diag_home.py                 # read-only: settings + current position
    python diag_home.py --home          # ALSO run axis.home(), then re-read
    python diag_home.py --port COM5     # override port (default: config / auto)

Recommended bench procedure for the "wrong side" question:
    1. Home the stage in Zaber Launcher. Note which physical end it parks at
       and the position Launcher reports (should be ~0).
    2. Run:  python diag_home.py --home
    3. Compare: does the stage park at the SAME physical end, and does this
       script report ~0 at that end? If the ends differ, the firmware home
       direction differs from what you expect; if the ends match but the
       numbers disagree, it's a coordinate offset in how the console uses pos.
    4. Note whether the workflow window (position_limits_mm, default [5, 40])
       sits near the home end or the far end of the printed full travel.

This tool does NOT change any firmware setting; it only reports. Apply the
actual fix (home direction setting or coordinate offset) after reviewing the
output with the operator.
"""

import argparse
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

import zaber_stage  # noqa: E402
from zaber_motion.units import Units  # noqa: E402


# Firmware settings worth printing. Names vary by product/firmware, so each is
# queried defensively; missing ones are simply skipped. `unit` is the length
# unit to request (None = native device units, no conversion).
_SETTINGS = [
    ("pos",              Units.LENGTH_MILLIMETRES),  # current position
    ("limit.min",        Units.LENGTH_MILLIMETRES),  # travel min (mm)
    ("limit.max",        Units.LENGTH_MILLIMETRES),  # travel max (mm)
    ("limit.home.pos",   Units.LENGTH_MILLIMETRES),  # position assigned at home
    ("limit.home.action", None),                     # what the home sensor does
    ("limit.home.state", None),
    ("limit.away.action", None),
    ("home.dir",         None),                       # home direction (if exposed)
    ("motion.index.dist", Units.LENGTH_MILLIMETRES),
    ("maxspeed",         Units.VELOCITY_MILLIMETRES_PER_SECOND),
]


def _get_setting(axis, name, unit):
    """Return (value, note) for one firmware setting, or (None, reason)."""
    try:
        if unit is None:
            return axis.settings.get(name), None
        return axis.settings.get(name, unit), None
    except Exception as e:  # noqa: BLE001
        return None, f"(unavailable: {type(e).__name__})"


def _dump(axis, tag):
    print(f"\n--- axis state {tag} ---")
    try:
        print(f"  is_homed : {axis.is_homed()}")
    except Exception as e:  # noqa: BLE001
        print(f"  is_homed : (error: {e})")
    try:
        print(f"  is_busy  : {axis.is_busy()}")
    except Exception as e:  # noqa: BLE001
        print(f"  is_busy  : (error: {e})")
    print("  settings:")
    for name, unit in _SETTINGS:
        val, note = _get_setting(axis, name, unit)
        u = "" if unit is None else f" [{unit}]"
        if note:
            print(f"    {name:<20} {note}")
        else:
            print(f"    {name:<20} {val}{u}")


def main() -> int:
    p = argparse.ArgumentParser(description="Zaber home-direction diagnostic")
    p.add_argument("--port", default=None,
                   help="serial port (default: from config / auto)")
    p.add_argument("--home", action="store_true",
                   help="actually run axis.home() and re-read (MOVES THE STAGE)")
    args = p.parse_args()

    # Load limits from the driver's own config so the printed workflow window
    # matches what the recorder uses.
    port = args.port if args.port else "auto"
    stage = zaber_stage.create_stage(port=port)
    if stage is None:
        print("ERROR: could not connect to the Zaber stage. "
              "Check COM port / cable / power.", file=sys.stderr)
        return 1

    # Stop the driver's background reader so raw settings reads below are the
    # only serial traffic (single-threaded, no interleave to reason about).
    stage._stop_position_reading()  # noqa: SLF001  (diagnostic, intentional)
    axis = stage.axis

    try:
        info = stage.get_device_info()
        print(f"Connected: {info}")
        print(f"Driver soft limits (position_limits_mm): "
              f"[{stage.min_pos}, {stage.max_pos}] mm")

        _dump(axis, "BEFORE (as found)")

        if args.home:
            print("\n>>> Homing (axis.home()) — watch which end it parks at...")
            axis.home()
            print(">>> Homing complete.")
            _dump(axis, "AFTER home()")
            print("\nCompare the parked physical end + reported pos against a "
                  "Zaber Launcher 'Home'. Same end & ~0 -> convention matches.")
        else:
            print("\n(read-only; pass --home to run axis.home() and re-read)")

        return 0
    finally:
        try:
            stage.disconnect()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
