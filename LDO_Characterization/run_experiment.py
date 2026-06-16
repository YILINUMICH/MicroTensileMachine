#!/usr/bin/env python3
"""
run_experiment.py — automated DAC->LDO settling + ripple characterization.

Runs the WHOLE experiment unattended:
  1. Connect to the Portenta H7 (SMA_Driver_PIO firmware) and the SDS2000X Plus.
  2. SETTLING pass: for each load (unloaded/loaded) x step (small/mid/large/down)
     x repeat, arm the scope single-shot, send `fire`, capture C1/C2/C3, save CSV.
  3. RIPPLE pass: hold a steady voltage and record PKPK + STDEV on the output.
  4. Write a manifest, then (optionally) run analyze_ldo.py to make plots + summary.

Channel map (config.yaml): C1 = trigger (D4 edge), C2 = DAC node, C3 = LDO output.

Usage:
    python run_experiment.py                 # full run, default config.yaml
    python run_experiment.py --config my.yaml
    python run_experiment.py --dry-run       # print the plan, touch no hardware
    python run_experiment.py --no-analyze    # capture only, skip plotting
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

# --- import the shared Siglent scope module (no packaging; sys.path shim) -----
_HERE = Path(__file__).resolve().parent
_SCOPE_DIR = _HERE.parent / "SiglentOscillosope"
sys.path.insert(0, str(_SCOPE_DIR))

from h7_serial import H7Serial, H7Config            # noqa: E402
import scope_trigger as st                          # noqa: E402


# ----------------------------------------------------------------------------
#  helpers
# ----------------------------------------------------------------------------
def volts_to_code(v: float, v_offset: float, vdd: float) -> int:
    """Inverse of the firmware model: code = (V - V_OFFSET)/VDD * 4095."""
    code = (v - v_offset) / vdd * 4095.0
    return int(max(0, min(4095, round(code))))


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def save_capture_csv(path: Path, columns: dict) -> None:
    """Write a capture as CSV. `columns` maps header name -> array; the first
    column should be 't_s'. An optional 'i_a' column carries SMA current."""
    names = list(columns.keys())
    n = min(len(columns[k]) for k in names)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(names)
        for i in range(n):
            row = []
            for k in names:
                fmt = "%.9g" if k == "t_s" else "%.6g"
                row.append(fmt % columns[k][i])
            w.writerow(row)


# ----------------------------------------------------------------------------
#  experiment passes
# ----------------------------------------------------------------------------
def run_settling(scope, h7, cfg, run_dir: Path, manifest: list) -> None:
    model = cfg["model"]
    exp = cfg["experiment"]
    chans = cfg["scope"]["channels"]
    cap = st.CaptureConfig(
        trigger_src=chans["trigger"],
        trigger_level_v=cfg["scope"]["trigger_level_v"],
        trigger_slope=cfg["scope"].get("trigger_slope", "POS"),
        timebase_s=cfg["scope"]["timebase_s"],
        codes_per_div=cfg["scope"]["codes_per_div"],
        memory_depth=cfg["scope"].get("memory_depth", "10K"),
    )
    st.configure_timebase(scope, cap)
    active = [chans[k] for k in ("trigger", "dac", "output")]
    if chans.get("current"):
        active.append(chans["current"])
    st.enable_channels(scope, active)          # traces ON or WF?/PAVA? return nothing
    hold_ms = int(exp["hold_ms"])

    for load in exp["loads"]:
        h7.set_mosfet(load == "loaded")
        for step in exp["steps"]:
            code_from = volts_to_code(step["from_v"], model["v_offset"], model["vdd"])
            code_to = volts_to_code(step["to_v"], model["v_offset"], model["vdd"])
            for rep in range(int(exp["repeats"])):
                tag = f"settle_{load}_{step['name']}_r{rep}"
                print(f"  [{tag}] {step['from_v']}->{step['to_v']} V "
                      f"(code {code_from}->{code_to})")
                st.arm_single(scope, cap)
                time.sleep(0.2)                      # let ARM take effect
                fire_reply = h7.fire(code_to, hold_ms, code_from)
                ok = st.wait_capture_complete(scope, timeout_s=hold_ms / 1000.0 + 5.0)
                if not ok:
                    print(f"    WARN: scope did not report capture complete for {tag}")
                st.stop(scope)                       # freeze the frame for a coherent multi-channel read
                t1, v_trig = st.capture_channel_volts(scope, chans["trigger"], cap.codes_per_div)
                _,  v_dac  = st.capture_channel_volts(scope, chans["dac"],     cap.codes_per_div)
                _,  v_out  = st.capture_channel_volts(scope, chans["output"],  cap.codes_per_div)
                cols = {"t_s": t1, "v_trig": v_trig, "v_dac": v_dac, "v_out": v_out}
                cur_ch = chans.get("current")
                if cur_ch:
                    _, v_ina = st.capture_channel_volts(scope, cur_ch, cap.codes_per_div)
                    isn = cfg["isense"]
                    scale = isn["gain_v_per_v"] * isn["shunt_ohm"]   # V/A
                    cols["i_a"] = (v_ina - isn["offset_v"]) / scale
                csv_path = run_dir / f"{tag}.csv"
                save_capture_csv(csv_path, cols)
                manifest.append({
                    "kind": "settling", "file": csv_path.name, "load": load,
                    "step": step["name"], "from_v": step["from_v"], "to_v": step["to_v"],
                    "code_from": code_from, "code_to": code_to, "repeat": rep,
                    "fire_reply": fire_reply[-1] if fire_reply else "",
                    "scope_complete": ok,
                })


def run_ripple(scope, h7, cfg, run_dir: Path, manifest: list) -> None:
    rip = cfg.get("ripple", {})
    if not rip.get("enabled", False):
        return
    from oscilloscope import ScopeConfig, MeasureParam
    chans = cfg["scope"]["channels"]
    out_ch = chans["output"]
    model = cfg["model"]
    code = volts_to_code(rip["hold_v"], model["v_offset"], model["vdd"])

    st.enable_channels(scope, [out_ch])      # trace ON or PAVA? times out (hangs)
    scope.write("TRMD AUTO")                  # free-run: measure LIVE ripple, not a frozen frame
    # AC-couple the output channel so we can zoom on the ripple (PKPK/STDEV
    # ignore the DC term, but AC coupling lets a tighter V/div if you also view it).
    try:
        scope.write(f"{out_ch}:CPL A1M")     # AC, 1 Mohm. VERIFY verb on your firmware.
    except Exception:
        pass

    scope.configure(ScopeConfig(
        source=out_ch,        param=MeasureParam.PKPK,
        second_source=out_ch, second_param=MeasureParam.STDEV,
    ))

    for load in rip.get("loads", ["unloaded"]):
        h7.set_mosfet(load == "loaded")
        h7.set_code(code)
        time.sleep(0.5)                       # let it settle
        # Manual read loop (instead of read_burst) so we can show live progress.
        n_reads = int(rip["n_reads"])
        results = []
        for k in range(n_reads):
            r = scope.read_single()
            if r:
                results.append(r)
            if (k + 1) % 10 == 0 or (k + 1) == n_reads:
                print(f"\r  [ripple_{load}] {k + 1}/{n_reads}", end="", flush=True)
        print()                               # newline after the progress line
        pkpk = [r.primary for r in results if r.primary == r.primary]
        std  = [r.secondary for r in results if r.secondary == r.secondary]
        row = {
            "kind": "ripple", "file": "", "load": load, "hold_v": rip["hold_v"],
            "code": code,
            "pkpk_mean_v": (sum(pkpk) / len(pkpk)) if pkpk else float("nan"),
            "pkpk_max_v":  max(pkpk) if pkpk else float("nan"),
            "std_mean_v":  (sum(std) / len(std)) if std else float("nan"),
            "n": len(results),
        }
        print(f"  [ripple_{load}] PKPK~{row['pkpk_mean_v']*1e3:.2f} mV  "
              f"STDEV~{row['std_mean_v']*1e3:.2f} mV  (n={row['n']})")
        manifest.append(row)

    try:
        scope.write(f"{out_ch}:CPL D1M")      # restore DC coupling
    except Exception:
        pass


# ----------------------------------------------------------------------------
#  main
# ----------------------------------------------------------------------------
def print_plan(cfg: dict) -> None:
    model, exp = cfg["model"], cfg["experiment"]
    print("DRY RUN — planned shots (no hardware touched):")
    for load in exp["loads"]:
        for step in exp["steps"]:
            cf = volts_to_code(step["from_v"], model["v_offset"], model["vdd"])
            ct = volts_to_code(step["to_v"], model["v_offset"], model["vdd"])
            print(f"  settling x{exp['repeats']}: {load:8s} {step['name']:9s} "
                  f"{step['from_v']}->{step['to_v']} V  code {cf}->{ct}")
    rip = cfg.get("ripple", {})
    if rip.get("enabled"):
        for load in rip.get("loads", []):
            print(f"  ripple: {load:8s} hold {rip['hold_v']} V, n={rip['n_reads']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(_HERE / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="print plan, no hardware")
    ap.add_argument("--no-analyze", action="store_true", help="skip plotting")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.dry_run:
        print_plan(cfg)
        return 0

    out_root = _HERE / cfg.get("output_dir", "data")
    run_dir = out_root / f"ldo_{datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run dir: {run_dir}")

    manifest: list = []

    # --- scope ---
    from oscilloscope import Oscilloscope
    sc = cfg["scope"]
    scope = Oscilloscope(host=sc.get("host"), port=sc.get("port", 5025), auto_open=False)
    ok = scope.connect(sc["host"]) if sc.get("host") else scope.auto_connect()
    if not ok:
        print("ERROR: could not connect to scope", file=sys.stderr)
        return 2
    print(f"Scope: {scope.idn}")

    # --- H7 ---
    s = cfg["serial"]
    h7cfg = H7Config(port=s["port"], baud=s["baud"],
                     timeout_s=s["timeout_s"], ack_timeout_s=s["ack_timeout_s"])
    try:
        with H7Serial(h7cfg) as h7:
            print(f"H7: {h7cfg.port} @ {h7cfg.baud}")
            print("== SETTLING pass ==")
            run_settling(scope, h7, cfg, run_dir, manifest)
            print("== RIPPLE pass ==")
            run_ripple(scope, h7, cfg, run_dir, manifest)
    finally:
        scope.close()

    # --- manifest + metadata ---
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (run_dir / "meta.json").write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "config": cfg,
        "scope_idn": scope.idn,
    }, indent=2))
    print(f"Wrote {len(manifest)} records -> {run_dir/'manifest.json'}")

    if not args.no_analyze:
        try:
            import analyze_ldo
            analyze_ldo.analyze_run(run_dir)
        except Exception as e:
            print(f"(analyze step failed: {e}; run analyze_ldo.py manually)")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
