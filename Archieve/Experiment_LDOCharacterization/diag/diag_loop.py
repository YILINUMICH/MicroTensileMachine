#!/usr/bin/env python3
"""
diag_loop.py — separate "timing miss" from "desync miss".

diag_arm.py proved a single arm->fire->check triggers correctly. The full run
still misses on some shots. Two suspects remain:
  (A) timing/arm flakiness — would miss even with NO waveform reads, and
  (B) SCPI desync from the WF? reads corrupting the NEXT shot's arming.

This loops arm->fire->check many times and reports the hit rate. Run it BOTH ways:

    python diag_loop.py            # NO capture — isolates (A). Expect ~100% hits.
    python diag_loop.py --capture  # full shot incl C1/C2/C3 WF reads — adds (B).

If plain is ~100% but --capture drops hits, the capture/desync path is the cause
(and the fix lives in the driver/resync). If plain ALSO misses, it's timing/arm.

Needs the H7 on COM8 — close `pio device monitor` first (shared port).

    python diag_loop.py [-n 20] [--capture] [--settle 0.2]
"""
import argparse
import sys
import time
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "Driver_SiglentOscilloscope"))
from oscilloscope import Oscilloscope          # noqa: E402
from h7_serial import H7Serial, H7Config        # noqa: E402
import scope_trigger as st                      # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=20, help="iterations")
    ap.add_argument("--capture", action="store_true",
                    help="also read C1/C2/C3 each iteration (adds the desync path)")
    ap.add_argument("--settle", type=float, default=None,
                    help="arm->fire delay (s); default = pre-trigger fill from timebase")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(_HERE.parent / "config.yaml"))
    sc, model, exp = cfg["scope"], cfg["model"], cfg["experiment"]
    chans = sc["channels"]
    # use the small_up step (least likely to clip)
    step = next(s for s in exp["steps"] if s["name"] == "small_up")

    def v2c(v):
        return int(max(0, min(4095, round((v - model["v_offset"]) / model["vdd"] * 4095))))
    code_to, code_from = v2c(step["to_v"]), v2c(step["from_v"])
    hold_ms = int(exp["hold_ms"])

    scope = Oscilloscope(host=sc.get("host"), port=sc.get("port", 5025),
                         timeout=sc.get("timeout_s", 2.0), auto_open=False)
    ok = scope.connect(sc["host"]) if sc.get("host") else scope.auto_connect()
    print("scope:", scope.idn if ok else "FAILED")
    if not ok:
        return 2

    cap = st.CaptureConfig(
        trigger_src=chans["trigger"], trigger_level_v=sc["trigger_level_v"],
        trigger_slope=sc.get("trigger_slope", "POS"), timebase_s=sc["timebase_s"],
        codes_per_div=sc["codes_per_div"], memory_depth=sc.get("memory_depth", "10K"),
    )
    st.configure_timebase(scope, cap)
    st.enable_channels(scope, [chans[k] for k in ("trigger", "dac", "output")])
    settle = args.settle if args.settle is not None else st.arm_to_fire_delay_s(cap)

    s = cfg["serial"]
    h7cfg = H7Config(port=s["port"], baud=s["baud"],
                     timeout_s=s["timeout_s"], ack_timeout_s=s["ack_timeout_s"])
    hits = 0
    samples_ok = 0
    with H7Serial(h7cfg) as h7:
        h7.set_mosfet(False)
        print(f"H7: {h7cfg.port}  |  mode: {'capture' if args.capture else 'no-capture'}"
              f"  |  n={args.n}  settle={settle:.2f}s\n")
        for i in range(args.n):
            scope.resync()
            st.arm_single(scope, cap)
            armed = scope.query("SAST?", expect="SAST").strip()
            time.sleep(settle)
            h7.fire(code_to, hold_ms, code_from)
            triggered = st.wait_for_stop(scope, timeout_s=2.0)
            hits += triggered
            note = ""
            if args.capture:
                st.stop(scope)
                t, v = st.capture_channel_volts(scope, chans["trigger"], cap.codes_per_div)
                _, _ = st.capture_channel_volts(scope, chans["dac"], cap.codes_per_div)
                _, vo = st.capture_channel_volts(scope, chans["output"], cap.codes_per_div)
                n = min(len(v), len(vo))
                samples_ok += (n > 0)
                note = f"  trig_samples={len(v)} out_samples={len(vo)}"
            print(f"  [{i:02d}] armed={armed!r:16s} -> "
                  f"{'HIT ' if triggered else 'MISS'}{note}")

    print(f"\nTRIGGER hits: {hits}/{args.n}", end="")
    if args.capture:
        print(f"   |   non-empty captures: {samples_ok}/{args.n}")
    else:
        print()
    print("interpretation: with the timebase-derived settle, no-capture should be "
          "~all hits (pre-trigger fill solved). If --capture now drops hits, the "
          "WF?/desync path is the remaining culprit.")
    scope.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
