"""operator_sweep_adcavg.py — find the knee of the noise/speed trade-off.

WHAT IT SWEEPS
--------------
ADC_SAMPLES_CYCLE: how many ADC reads the H7 averages per control tick. It is
the one lever that trades current-sense noise against loop rate:

    noise  ~ 144 mA / sqrt(n)          (144 mA = per-single-read noise)
    period ~ fixed + 2*n*t_read        (two channels per readSma)

Measured 2026-07-28 from the sweep lead-in (n=64 idle-hold) against the in-cycle
stream (n=4): sd 18.4 vs 71.0 mA, ratio 3.86 against an ideal sqrt(16)=4.00.
So averaging runs at ~96% efficiency and this lever is real. That estimate came
from only 23 idle samples though, which is why this script measures the curve
properly instead of extrapolating it.

WHY IT MATTERS
--------------
The CC loop's `near` gate is +-12 mA at the 100 mA cool target, and its R_est
bootstrap latches u/I from ONE sample. At the stock n=4 (72 mA sd) both are
decided by noise, which is why R_est latches at 6.25 ohm instead of ~4.2 and the
loop overshoots by up to 50%. The Uno running the SAME control law holds 0.1%
because its sense noise is 0.90 mA.

NO FIRMWARE FILES ARE EDITED
----------------------------
ADC_SAMPLES_CYCLE is `#ifndef`-guarded, so each point is built by APPENDING a
-D flag through the PLATFORMIO_BUILD_FLAGS environment variable. Nothing in
Firmware_SMAConstantCurrent_PIO/ changes on disk and nothing is committed. The
script re-flashes the stock build at the end, and does so in a `finally` so it
still happens if you interrupt it.

Every upload needs a POWER CYCLE of USB + EVM before the board is usable — the
DFU reset does not cleanly re-power the EVM analog rails and the ADS1263 comes
up as ID=0x00. The script stops and waits for you at each one.

USAGE
-----
    python operator_sweep_adcavg.py --port COM8
    python operator_sweep_adcavg.py --port COM8 --n 4,16,64,256 --secs 20
    python operator_sweep_adcavg.py --analyse data/raw/adcavg_20260729_101500
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib_h7_session import (H7, SRC_LASER, SRC_SMA_I,  # noqa: E402
                            SRC_SMA_V, save_capture)

_MODULE = Path(__file__).resolve().parent.parent   # Experiment_SMAThermalCharacterization/
RAW = _MODULE / "data" / "raw"
# parents[2] because this script lives one level down in diagnostics/ — the
# firmware project is a SIBLING OF THE MODULE, at the repo root.
FW = (_MODULE.parent / "Firmware_SMAConstantCurrent_PIO")
ENV = "portenta_m7"
GATE_BAND_MA = 12.0
UNO_SD_MA, UNO_HZ = 0.90, 197.0


def build_and_upload(n: int | None, port: str) -> None:
    """n=None -> stock build (no extra flags)."""
    env = dict(os.environ)
    if n is None:
        env.pop("PLATFORMIO_BUILD_FLAGS", None)
        what = "STOCK (ADC_SAMPLES_CYCLE=4 from source)"
    else:
        env["PLATFORMIO_BUILD_FLAGS"] = f"-DADC_SAMPLES_CYCLE={n}"
        what = f"ADC_SAMPLES_CYCLE={n}"
    print(f"\n  building + uploading {what} ...")
    r = subprocess.run(["pio", "run", "-e", ENV, "-t", "upload",
                        "--upload-port", port],
                       cwd=str(FW), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:]); print(r.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"upload failed for {what}")
    print(f"  uploaded.")


def power_cycle_prompt() -> None:
    print("\n  ** POWER-CYCLE THE RIG NOW: unplug USB *and* the EVM supply, "
          "wait 3 s, plug both back in. **")
    print("     (skip this and the ADS1263 comes up ID=0x00 and the run is junk)")
    while input("     type OK when the rig is back up: ").strip().upper() != "OK":
        pass
    time.sleep(2.0)


def measure(port: str, secs: float, volts: float):
    """Static-command voltage-mode capture: the DAC never moves, so everything
    that varies is measurement. Same readSma(ADC_SAMPLES_CYCLE) path the CC loop
    uses."""
    h7 = H7(port, verbose=False)
    h7.open()
    try:
        h7.send("arm")
        time.sleep(0.5)
        half = int(secs * 1000 / 2)
        h7.send(f"cycle {volts:.4f} {volts:.4f} {half} {half} 1")
        cap = h7.capture(secs + 1.0)
    finally:
        h7.disarm()
        h7.close()
    return cap


def stats(cap):
    v, i = {}, {}
    for s in cap.samples:
        (v if s.src == SRC_SMA_V else i if s.src == SRC_SMA_I else {})[s.hw_us] = s.value
    k = sorted(set(v) & set(i))
    if len(k) < 300:
        return None
    t = np.array(k, float) * 1e-6
    I = 1e3 * np.array([i[x] for x in k])
    V = np.array([v[x] for x in k])
    dt = lambda y: y - np.polyval(np.polyfit(t, y, 1), t)   # drop real R drift
    las = np.array([s.value for s in cap.samples if s.src == SRC_LASER])
    return {
        "n_samples": len(k),
        "rate_hz": len(k) / (t[-1] - t[0]),
        "I_mean_mA": float(I.mean()),
        "I_sd_mA": float(dt(I).std()),
        "V_sd_mV": float(1e3 * dt(V).std()),
        "corr": float(np.corrcoef(dt(V), dt(I))[0, 1]),
        "laser_sd_mV": float(1e3 * (las - las.mean()).std()) if len(las) > 100 else None,
    }


def report(rows):
    print("\n" + "=" * 78)
    print(f"{'n':>6}{'rate Hz':>10}{'I sd mA':>10}{'per-read':>10}"
          f"{'vs ideal':>10}{'3sig<gate':>11}{'laser mV':>10}")
    base = next((r for r in rows if r["n"] == min(x["n"] for x in rows)), None)
    for r in rows:
        s = r["stats"]
        if not s:
            print(f"{r['n']:>6}   capture failed"); continue
        per = s["I_sd_mA"] * np.sqrt(r["n"])
        ideal = (base["stats"]["I_sd_mA"] * np.sqrt(base["n"] / r["n"])
                 if base and base["stats"] else float("nan"))
        eff = ideal / s["I_sd_mA"] if s["I_sd_mA"] else float("nan")
        ok = "yes" if 3 * s["I_sd_mA"] < GATE_BAND_MA else "NO"
        print(f"{r['n']:>6}{s['rate_hz']:10.0f}{s['I_sd_mA']:10.2f}{per:10.1f}"
              f"{eff:10.2f}{ok:>11}"
              f"{(s['laser_sd_mV'] if s['laser_sd_mV'] else float('nan')):10.3f}")
    print(f"\n  Uno reference: {UNO_SD_MA:.2f} mA at {UNO_HZ:.0f} Hz "
          f"(same control law, same driver board)")
    print(f"  CC gate band: {GATE_BAND_MA:.0f} mA — 3-sigma must fit inside it "
          f"or the gate is decided by noise")
    print("  laser (ADS1263) is the CONTROL: if it moves between points, ambient "
          "changed and\n  the comparison is not clean.")

    good = [r for r in rows if r["stats"] and 3 * r["stats"]["I_sd_mA"] < GATE_BAND_MA]
    print("\nVERDICT")
    if good:
        best = max(good, key=lambda r: r["stats"]["rate_hz"])
        print(f"  Fastest setting that fits 3-sigma inside the gate: "
              f"ADC_SAMPLES_CYCLE={best['n']}")
        print(f"    {best['stats']['I_sd_mA']:.2f} mA sd at "
              f"{best['stats']['rate_hz']:.0f} Hz")
        if best["stats"]["rate_hz"] < UNO_HZ:
            print(f"    NOTE: slower than the Uno's {UNO_HZ:.0f} Hz, which held "
                  f"0.1% — so this buys\n          control quality at the cost of "
                  f"the 1 kHz stream we wanted for the 100 ms pulse.")
    else:
        print("  NO setting in this sweep fits 3-sigma inside the gate.")
        print("  Averaging alone cannot rescue the loop -> the analog path must be")
        print("  fixed (ADC sampling-time config, RC on the INA output before A1).")
        print("  Run operator_noise_isense.py to find whether the noise is the H7's")
        print("  own front end or pickup from the load.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--n", default="4,16,64,256",
                    help="ADC_SAMPLES_CYCLE values to sweep")
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--volts", type=float, default=0.5,
                    help="static LDO command; 0.5 = the idle floor (~155 mA)")
    ap.add_argument("--outdir", default=str(RAW))
    ap.add_argument("--analyse", metavar="DIR",
                    help="re-report an existing sweep directory")
    args = ap.parse_args()

    if args.analyse:
        rows = json.load(open(Path(args.analyse) / "summary.json"))
        report(rows)
        return

    ns = [int(x) for x in args.n.split(",")]
    out = Path(args.outdir) / f"adcavg_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"ADC_SAMPLES_CYCLE sweep -> {out}")
    print(f"  points: {ns}   |  {args.secs:.0f}s each at {args.volts:.2f} V static")
    print(f"  {len(ns)} builds + {len(ns)+1} power cycles (the last restores stock)")
    print(f"\n  Nothing in {FW.name}/ is edited — each point is a -D flag via")
    print(f"  PLATFORMIO_BUILD_FLAGS. Stock firmware is re-flashed at the end.")
    if input("\n  type GO to start: ").strip() != "GO":
        sys.exit("aborted")

    rows = []
    try:
        for n in ns:
            print(f"\n{'='*78}\nPOINT n={n}")
            build_and_upload(n, args.port)
            power_cycle_prompt()
            cap = measure(args.port, args.secs, args.volts)
            save_capture(cap, out / f"n{n:04d}.csv",
                         {"adc_samples_cycle": n, "volts": args.volts,
                          "secs": args.secs,
                          "captured_utc": datetime.now(timezone.utc).isoformat()})
            s = stats(cap)
            rows.append({"n": n, "stats": s})
            if s:
                print(f"  -> {s['rate_hz']:.0f} Hz, I sd {s['I_sd_mA']:.2f} mA, "
                      f"mean {s['I_mean_mA']:.1f} mA")
            else:
                print("  -> capture FAILED (too few paired samples)")
            json.dump(rows, open(out / "summary.json", "w"), indent=2)
    finally:
        print(f"\n{'='*78}\nRESTORING STOCK FIRMWARE")
        try:
            build_and_upload(None, args.port)
            power_cycle_prompt()
        except Exception as e:                                  # noqa: BLE001
            print(f"  !! STOCK RESTORE FAILED: {e}", file=sys.stderr)
            print(f"  !! Re-flash by hand:  cd {FW}  &&  "
                  f"pio run -e {ENV} -t upload", file=sys.stderr)

    if rows:
        report(rows)
        print(f"\n  raw captures + summary.json -> {out}")


if __name__ == "__main__":
    main()
