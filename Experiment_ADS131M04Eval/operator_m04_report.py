#!/usr/bin/env python3
"""operator_m04_report.py — judge an ADS131M04 sweep against plan §7.

Run it on a sweep folder the moment a run ends (or mid-run on partial data —
every capture is written before the next condition starts):

    python operator_m04_report.py data/m04_20260827_141233

It re-derives every number from the raw capture CSVs, so its verdicts are the
reference; never trust a stale summary over this. Outputs land in the folder:

    report.txt     per-condition verdicts + which checks fired
    summary.csv    one row per condition x channel

CHECKS — one per acceptance criterion in docs/ADS131M04_migration_plan.md §7:

    T3/T4 crc     CRC errors over the condition. The output CRC is always on
                  and covers the command and status words as well as the data,
                  so a nonzero count means the SPI link is not trustworthy at
                  that clock. T3 adopts the fastest CLEAN rate, then backs off
                  one step; T4 wants zero over >= 1e6 frames.
    T5 rate       measured SPS per channel vs the OSR's nominal, +/-1%. Also
                  the only thing that settles the datasheet's own contradiction
                  at OSR code 7 (Table 8-17 says 16256, Table 8-2 says 16384).
    T7 noise      per-channel standard deviation vs Table 7-1, <= 2x. Uses
                  STDEV, not rms about zero: a DC offset is not noise, and the
                  part has an offset spec of its own.
    T7 spread     the four channels within 2x of each other. One deviant
                  channel is a wiring or PGA fault, not a noise floor.
    T8 dc         with the input mux on the internal DC test signal
                  (2/15 x FSR = 160 mV at gain 1), the measured mean vs that
                  expected value. Tolerance is a loose 2% on purpose: the
                  datasheet calls the signal "nominally" 2/15 x VREF with no
                  tolerance of its own, so this confirms lsbVolts() scaling and
                  SIGN EXTENSION -- which is T8's stated job -- not absolute
                  accuracy. `dc_sign` fires separately when a channel reads the
                  wrong polarity, because that is the specific defect T8 exists
                  to catch and it should not hide inside a large percentage.
    loss          UDP seq gaps. Reported, never hidden -- a capture that lost
                  datagrams can still pass the noise check while being wrong
                  about the rate.

A frozen sensor reads as a PERFECT noise result, so `rate` and `loss` are not
optional extras: they are what distinguishes "quiet" from "not converting".
That failure mode is real on this part -- with CLKIN absent the chip still
answers register reads and simply never produces new conversions.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics as stats
import sys
from pathlib import Path

import lib_m04 as M

STATUS_RE = re.compile(r"\[STATUS\]")


def load_capture_rows(csv_path: Path):
    """-> {src: [(hw_us, value_V), ...]} in file order."""
    by_src: dict = {}
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            by_src.setdefault(int(row["src"]), []).append(
                (int(row["hw_us"]), float(row["value"])))
    return by_src


def status_deltas(log_path: Path):
    """Parse [STATUS] frames from the console log -> summed crc_err, max frames.

    crc_err is reported by the firmware as a running total, so the delta across
    the condition is what matters; a board that arrived with a nonzero count
    must not fail a condition that added none.
    """
    if not log_path.exists():
        return {"crc_err": None, "frames": None, "n_status": 0}
    first_crc = last_crc = None
    first_fr = last_fr = None
    n = 0
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not STATUS_RE.search(line):
            continue
        st = M.parse_status(line)
        if not st:
            continue
        n += 1
        if "crc_err" in st:
            last_crc = st["crc_err"]
            if first_crc is None:
                first_crc = st["crc_err"]
        if "frames" in st:
            last_fr = st["frames"]
            if first_fr is None:
                first_fr = st["frames"]
    return {
        "crc_err": None if last_crc is None else int(last_crc - (first_crc or 0)),
        "frames": None if last_fr is None else int(last_fr - (first_fr or 0)),
        "n_status": n,
    }


def analyse(csv_path: Path) -> dict:
    meta_path = csv_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    cond = meta.get("condition", {})
    by_src = load_capture_rows(csv_path)
    st = status_deltas(csv_path.with_suffix(".console.log"))

    secs = float(cond.get("secs", 0)) or None
    nominal = float(cond.get("nominal_sps", 0)) or None
    spec_uv = float(cond.get("spec_noise_uv", 0)) or None

    chans = {}
    for src, rows in sorted(by_src.items()):
        ch = M.CH_FOR_SRC.get(src, src)
        vals = [v for _, v in rows]
        # Rate from the HARDWARE timestamps, not the wall clock: hw_us is the
        # ADC's own timeline and is what a frozen converter fails to advance.
        span_s = None
        if len(rows) > 1:
            span_s = (rows[-1][0] - rows[0][0]) * 1e-6
        rate = (len(rows) - 1) / span_s if span_s and span_s > 0 else None

        chans[ch] = {
            "src": src,
            "n": len(vals),
            "mean_v": stats.fmean(vals) if vals else None,
            "sd_uv": stats.stdev(vals) * 1e6 if len(vals) > 1 else None,
            "pp_uv": (max(vals) - min(vals)) * 1e6 if vals else None,
            "rate_sps": rate,
            "hw_span_s": span_s,
        }

    loss = (meta.get("transport") or {}).get("loss_pct", {})

    return {"label": cond.get("label", csv_path.stem), "cond": cond,
            "chans": chans, "status": st, "loss_pct": loss,
            "secs": secs, "nominal_sps": nominal, "spec_noise_uv": spec_uv,
            "mux": int(cond.get("mux", 0)),
            "expected_v": cond.get("expected_v")}


def verdicts(r: dict) -> "list[tuple[str, bool, str]]":
    """-> [(check, passed, detail)]. Only checks with data are emitted."""
    out = []
    chans, st = r["chans"], r["status"]

    if st["crc_err"] is not None:
        ok = st["crc_err"] <= M.ACC_CRC_ERR
        detail = f"{st['crc_err']} errors"
        if st["frames"] is not None:
            detail += f" over {st['frames']} frames"
            if st["frames"] < M.ACC_T4_FRAMES:
                detail += f" (T4 wants >= {M.ACC_T4_FRAMES:,})"
        out.append(("crc", ok, detail))
    else:
        out.append(("crc", False, "no [STATUS] frames in console log — "
                                  "cannot judge T3/T4"))

    if r["nominal_sps"]:
        rates = [c["rate_sps"] for c in chans.values() if c["rate_sps"]]
        if rates:
            worst = max(rates, key=lambda x: abs(x - r["nominal_sps"]))
            err = abs(worst - r["nominal_sps"]) / r["nominal_sps"]
            out.append(("rate", err <= M.ACC_RATE_TOL,
                        f"worst {worst:.1f} SPS vs {r['nominal_sps']:.1f} "
                        f"nominal ({err*100:+.2f}%)"))
        else:
            out.append(("rate", False, "no samples — converter not producing data"))

    if r["spec_noise_uv"]:
        sds = {ch: c["sd_uv"] for ch, c in chans.items() if c["sd_uv"] is not None}
        if sds:
            worst_ch = max(sds, key=lambda k: sds[k])
            limit = r["spec_noise_uv"] * M.ACC_NOISE_FACTOR
            out.append(("noise", sds[worst_ch] <= limit,
                        f"worst ch{worst_ch} {sds[worst_ch]:.2f} uV vs "
                        f"{limit:.2f} uV limit ({r['spec_noise_uv']:.2f} spec x"
                        f"{M.ACC_NOISE_FACTOR:g})"))
            lo, hi = min(sds.values()), max(sds.values())
            if lo > 0:
                out.append(("spread", hi / lo <= M.ACC_CH_SPREAD,
                            f"ch spread {hi/lo:.2f}x "
                            f"(lo {lo:.2f}, hi {hi:.2f} uV)"))

    # T8 — DC accuracy against the internal test signal. Only meaningful when
    # the mux is driving one; on real inputs or the internal short the expected
    # value is 0 and there is nothing to judge.
    if r["mux"] in (M.MUX_TEST_POS, M.MUX_TEST_NEG) and r["expected_v"]:
        exp = float(r["expected_v"])
        means = {ch: c["mean_v"] for ch, c in chans.items() if c["mean_v"] is not None}
        if means:
            worst_ch = max(means, key=lambda k: abs(means[k] - exp))
            err = abs(means[worst_ch] - exp) / abs(exp)
            out.append(("dc", err <= M.ACC_DC_TOL,
                        f"worst ch{worst_ch} {means[worst_ch]*1e3:+.3f} mV vs "
                        f"{exp*1e3:+.3f} mV expected ({err*100:+.2f}%, "
                        f"tol {M.ACC_DC_TOL*100:g}%)"))
            # A sign flip is the specific defect T8 exists to catch, so name it
            # rather than letting it hide inside a large percentage error.
            wrong_sign = [ch for ch, v in means.items() if v * exp < 0]
            if wrong_sign:
                out.append(("dc_sign", False,
                            f"ch{wrong_sign} read the WRONG SIGN — "
                            f"sign extension is broken in the driver"))

    if r["loss_pct"]:
        worst = max(float(v) for v in r["loss_pct"].values())
        out.append(("loss", worst == 0.0, f"worst src {worst:.4f}% UDP loss"))

    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", help="sweep folder (data/m04_*)")
    a = p.parse_args()

    folder = Path(a.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2

    csvs = sorted(x for x in folder.glob("*.csv") if not x.name.startswith("summary"))
    if not csvs:
        print(f"no captures in {folder}", file=sys.stderr)
        return 2

    lines = [f"ADS131M04 evaluation report — {folder.name}",
             "=" * 72, ""]

    # T1/T2 are firmware-side; surface whatever `selftest` printed.
    selftest = [ln for c in csvs
                for ln in (c.with_suffix(".console.log").read_text(encoding="utf-8", errors="replace")
                           .splitlines() if c.with_suffix(".console.log").exists() else [])
                if "[T1]" in ln or "[T2]" in ln]
    if selftest:
        lines.append("Firmware self-test (T1 ID / T2 register round-trip):")
        lines += [f"  {s.strip()}" for s in dict.fromkeys(selftest)]
    else:
        lines.append("Firmware self-test: NO [T1]/[T2] lines found — T1/T2 unjudged.")
    lines.append("")

    # Run order, not filename order — a ladder read alphabetically puts
    # 16000k before 2000k.
    analysed = sorted((analyse(c) for c in csvs),
                      key=lambda r: (r["cond"].get("seq", 1 << 30), r["label"]))

    rows, n_fail = [], 0
    for r in analysed:
        vs = verdicts(r)
        failed = [v for v in vs if not v[1]]
        n_fail += len(failed)
        mark = "PASS" if not failed else "FAIL"

        crc_ok = next((ok for name, ok, _ in vs if name == "crc"), False)
        cond = r["cond"]
        lines.append(f"[{mark}] {r['label']}")
        lines.append(f"       spi={cond.get('spi_hz', 0)/1e6:.2f} MHz  "
                     f"osr={cond.get('osr_div', '?')}  gain={cond.get('gain', '?')}  "
                     f"{cond.get('secs', '?')} s")
        for name, ok, detail in vs:
            lines.append(f"       {'ok ' if ok else 'FAIL'} {name:<7s} {detail}")
        for ch, cd in sorted(r["chans"].items()):
            lines.append(f"         ch{ch} (src{cd['src']}): n={cd['n']:<7d} "
                         f"mean={cd['mean_v']*1e3:+9.4f} mV  "
                         f"sd={cd['sd_uv']:8.2f} uV  "
                         f"pp={cd['pp_uv']:9.2f} uV  "
                         f"rate={cd['rate_sps'] or float('nan'):8.2f} SPS")
            rows.append({
                "condition": r["label"], "ch": ch, "src": cd["src"],
                "spi_hz": cond.get("spi_hz"), "osr_div": cond.get("osr_div"),
                "gain": cond.get("gain"), "n": cd["n"],
                "mean_v": cd["mean_v"], "sd_uv": cd["sd_uv"], "pp_uv": cd["pp_uv"],
                "rate_sps": cd["rate_sps"],
                "nominal_sps": r["nominal_sps"], "spec_noise_uv": r["spec_noise_uv"],
                "crc_err": r["status"]["crc_err"], "frames": r["status"]["frames"],
                "crc_ok": crc_ok, "verdict": mark,
            })
        lines.append("")

    # T3's answer is a choice, not a threshold: the fastest clean clock, backed
    # off one step. Surfaced explicitly so nobody has to squint at the table.
    # Keyed on the CRC check ALONE, not the overall verdict: a condition that
    # fails on noise or rate says nothing about whether its SPI clock is sound,
    # and letting it disqualify that clock would silently shorten the ladder.
    clean = [rr for rr in rows if rr["crc_ok"] and rr["spi_hz"]]
    if clean:
        by_hz = sorted({rr["spi_hz"] for rr in clean})
        lines.append(f"T3: clean SPI clocks {[f'{h/1e6:g}M' for h in by_hz]}")
        if len(by_hz) > 1:
            lines.append(f"    fastest clean = {by_hz[-1]/1e6:g} MHz -> "
                         f"ADOPT {by_hz[-2]/1e6:g} MHz (one step back)")
        else:
            lines.append(f"    only one clean clock ({by_hz[0]/1e6:g} MHz) — "
                         f"ladder too short to back off")
        lines.append("")

    lines.append("=" * 72)
    lines.append(f"{len(csvs)} conditions, {n_fail} failed checks")

    text = "\n".join(lines)
    print(text)
    (folder / "report.txt").write_text(text, encoding="utf-8")

    if rows:
        with open(folder / "summary.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(f"\nwrote {folder/'report.txt'} and {folder/'summary.csv'}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
