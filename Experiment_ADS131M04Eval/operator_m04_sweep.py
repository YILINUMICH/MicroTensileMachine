#!/usr/bin/env python3
"""operator_m04_sweep.py — drive the ADS131M04 qualification as a parameter sweep.

WHAT THIS DOES
    Walks a ladder of device configurations — each a (spi_hz, osr, gain, secs)
    cell — capturing the full stream per cell to its own CSV. That is the shape
    the plan's T-list already has: T3 is an SPI clock ladder, T5 is an OSR
    ladder, T7 is a gain ladder at fixed OSR. Judging is NOT done here; run
    operator_m04_report.py on the folder afterwards.

USAGE
    # ALWAYS dry-run first: prints the plan WITHOUT opening the port.
    python operator_m04_sweep.py --profile profiles/qualify.json --dry-run
    python operator_m04_sweep.py --profile profiles/qualify.json

    # afterwards, ALWAYS:
    python operator_m04_report.py data/m04_<stamp>

BEFORE RUNNING
    - Flash portenta_m4_idle FIRST. M4 shares SPI1 and the same CS pin; leave
      the resident sampler running and the two cores fight over the bus, which
      presents as intermittent CRC errors that look like a cable fault.
    - Power-cycle USB + EVM after the upload.
    - EVM jumpers: JP6 fitted [1-2] (Y1 8.192 MHz — CLKIN is MANDATORY), JP5
      NOT fitted (it powers Y1 down). For the T7 noise cells leave JP1-JP4 at
      the factory [3-4], which grounds every input through 1 k — that IS the
      shorted-input condition T7 wants.

WHY NO INTERACTIVE CONSOLE
    Every acceptance number in plan §7 is a measurement over a held condition,
    not something to eyeball in scrollback. Captured to files, a run is
    reproducible, diffable against the next one, and committed with the rest of
    the results. This follows Experiment_SMAThermalCharacterization's sweep +
    report split for the same reason.

WHAT THE FIRMWARE MUST PROVIDE
    Beyond the M04 commands (spi / osr / gain / rst / selftest), the board must
    satisfy lib_h7_session's session contract: a [STATUS] frame at 1 Hz on
    serial carrying udp_on, a `netcfg <ip> <port>` command, and `ping` accepted
    as a no-op. Unknown commands must be ignored, never wedge the parser.
    This script passes cal=() so the SMA calibration commands are NOT sent, and
    never calls disarm() — there is no actuator here.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import lib_m04 as M


def build_conditions(a) -> "list[M.Condition]":
    """CLI ladder used when no profile is given. One axis at a time."""
    conds = []
    if a.spi_ladder:
        for hz in [int(float(x) * 1e6) for x in a.spi_ladder.split(",")]:
            conds.append(M.Condition(label=f"t3_spi_{hz//1000}k", spi_hz=hz,
                                     osr=a.osr, gain=a.gain, secs=a.secs,
                                     note="T3 SPI clock ladder"))
    if a.osr_ladder:
        for code in [int(x) for x in a.osr_ladder.split(",")]:
            conds.append(M.Condition(label=f"t5_osr_{M.OSR_DIV[code]}",
                                     spi_hz=a.spi_hz, osr=code, gain=a.gain,
                                     secs=a.secs, note="T5 rate accuracy"))
    if a.gain_ladder:
        for g in [int(x) for x in a.gain_ladder.split(",")]:
            conds.append(M.Condition(label=f"t7_gain_{g}", spi_hz=a.spi_hz,
                                     osr=a.osr, gain=g, secs=a.secs,
                                     note="T7 shorted-input noise"))
    if not conds:
        conds.append(M.Condition(label="single", spi_hz=a.spi_hz, osr=a.osr,
                                 gain=a.gain, secs=a.secs))
    return conds


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default=None,
                   help="profiles/*.json — WINS over CLI flags for what it "
                        "specifies; a copy is saved into the output folder")
    p.add_argument("--port", default="COM8")
    p.add_argument("--transport", choices=("usb", "udp"), default=None,
                   help="default: lib_h7_session's (udp since the Step-3 cutover)")
    p.add_argument("--pc-ip", default=None,
                   help="this PC's NIC on the H7 segment, e.g. 169.254.245.100")
    p.add_argument("--spi-ladder", default=None, metavar="MHz,MHz,...",
                   help="T3: e.g. 0.5,2,8,16")
    p.add_argument("--osr-ladder", default=None, metavar="CODE,CODE,...",
                   help="T5: OSR codes, e.g. 5,6,7 (=4096,8192,16256)")
    p.add_argument("--gain-ladder", default=None, metavar="G,G,...",
                   help="T7: e.g. 1,2,4")
    p.add_argument("--spi-hz", type=int, default=2_000_000,
                   help="fixed SPI clock for the non-T3 ladders")
    p.add_argument("--osr", type=int, default=6, help="fixed OSR code (6 = 500 SPS)")
    p.add_argument("--gain", type=int, default=1, help="fixed gain")
    p.add_argument("--secs", type=float, default=60.0, help="seconds per condition")
    p.add_argument("--settle-s", type=float, default=1.0,
                   help="settle after reconfiguring, before capturing. The "
                        "digital filter needs 3 conversion cycles after any "
                        "rate/gain change (datasheet 8.5.2); the driver already "
                        "waits, this is extra margin for the slowest OSR.")
    p.add_argument("--out", default=None, help="explicit output folder")
    p.add_argument("--tag", default=None, help="suffix for the auto-named folder")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit WITHOUT opening the port")
    a = p.parse_args()

    if a.profile:
        prof = M.Profile.load(Path(a.profile))
        conds = prof.conditions
        port = prof.port or a.port
        transport = prof.transport or a.transport
        pc_ip = prof.pc_ip or a.pc_ip
        name = prof.name
    else:
        conds = build_conditions(a)
        port, transport, pc_ip, name = a.port, a.transport, a.pc_ip, "cli"

    if not conds:
        print("no conditions — nothing to do", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(a.out) if a.out else M.DATA / f"m04_{stamp}{'_' + a.tag if a.tag else ''}"

    total = sum(c.secs + a.settle_s for c in conds)
    print(f"\nprofile   : {name}")
    print(f"port      : {port}   transport: {transport or 'default(udp)'}")
    print(f"output    : {out}")
    print(f"conditions: {len(conds)}   est. {total/60:.1f} min\n")
    for c in conds:
        print("  " + c.describe())
    print()

    if a.dry_run:
        print("DRY RUN — port not opened, nothing captured.")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    if a.profile:
        shutil.copy(a.profile, out / Path(a.profile).name)

    h7 = M.H7(port, transport=transport, pc_ip=pc_ip)
    run_meta = {
        "module": "Experiment_ADS131M04Eval",
        "profile": name,
        "started_utc": datetime.utcnow().isoformat() + "Z",
        "port": port,
        "transport": transport or "default",
        "settle_s": a.settle_s,
        "src_map": {str(v): f"ch{k}" for k, v in M.SRC_FOR_CH.items()},
    }

    try:
        # cal=() — the SMA calibration commands mean nothing to this firmware.
        h7.open(cal=())

        # T1/T2 live in firmware; `selftest` re-runs them and prints verdicts.
        # Captured into the console log so the report can read them.
        h7.send("selftest")
        time.sleep(2.0)

        for i, c in enumerate(conds, 1):
            print(f"[{i}/{len(conds)}] {c.label}")
            for cmd in c.commands():
                h7.send(cmd)
                time.sleep(0.15)
            time.sleep(a.settle_s)

            cap = h7.capture(c.secs)

            meta = dict(run_meta)
            meta["condition"] = {
                # Run order, so the report shows a ladder in the order it was
                # actually walked instead of alphabetically (t3_spi_16000k
                # sorts before t3_spi_2000k, which reads as nonsense).
                "seq": i,
                "label": c.label, "spi_hz": c.spi_hz, "osr_code": c.osr,
                "osr_div": M.OSR_DIV[c.osr], "gain": c.gain, "secs": c.secs,
                "pwr": c.pwr, "note": c.note,
                "nominal_sps": c.nominal_sps,
                "spec_noise_uv": c.spec_noise_uv,
                "fsr_v": M.fsr_v(c.gain),
            }
            M.save_capture(cap, out / f"{c.label}.csv", meta)

            n = len(cap.samples)
            print(f"       {n} samples in {c.secs:.0f} s "
                  f"({n / max(c.secs, 1e-9) / M.NUM_CH:.1f} SPS/ch)")

        (out / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        print(f"\ndone -> {out}")
        print(f"now run:  python operator_m04_report.py {out}")
        return 0

    except KeyboardInterrupt:
        print("\ninterrupted — captures already written are intact", file=sys.stderr)
        return 130
    finally:
        # No disarm(): nothing is actuated here, and `disarm` is not a command
        # this firmware knows.
        h7.close()


if __name__ == "__main__":
    raise SystemExit(main())
