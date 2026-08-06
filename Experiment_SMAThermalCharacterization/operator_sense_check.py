#!/usr/bin/env python3
"""operator_sense_check.py — is the SMA sense chain healthy RIGHT NOW?

WHY THIS EXISTS
    On 2026-08-05 the overnight campaign aborted on the sense guard at its
    first condition. The guard's message offers two explanations (corrupted
    sense / stale r_min) and neither was the fault: a reseated SMA clip had
    added contact resistance, which raised the BROADBAND NOISE on sma_v and
    sma_i by 3.3x. The guard fires on a FRACTION of impossible samples, so a
    wider noise floor trips it at ~1% even though nothing is "impossible" in
    the corrupted-sense sense. The number that actually diagnoses the rig is
    the noise floor itself, and nothing printed it.

    Reading it from a capture takes seconds. Re-running an 8-minute anchor to
    find out whether a clip reseat helped does not.

WHAT IT MEASURES (quiet window only: the cold-start settle, before any pulse,
where i_low=0 parks the wire at the v_idle bias)
    sma_i sigma   per-sample current noise. THE headline number.
    R             mean cold resistance from V/I. Tracks contact resistance.
    R @200 ms     R noise after averaging into the window the analysis uses.
                  Compare against the ~3% dR the self-sensing model must see
                  (order-of-magnitude anchor from the 08-05 fire transition):
                  if this is not well below that, the payload is buried.
    corr(V,I)     INFORMATIONAL ONLY. Common-mode noise cancels in V/I and
                  incoherent noise does not, so a drop between two captures of
                  the SAME condition is meaningful — but the absolute value
                  swings with pulse level (+0.09 healthy at 250 mA, +0.45
                  faulted at 650 mA), so it is not a health threshold. The
                  verdict below uses sigma and R @200 ms only.

HEALTHY BASELINE, measured on this rig 2026-08-04 through 2026-08-05 16:10
(three sweeps, two wires, stable):  sigma ~26 mA, R @200 ms ~1.7-2.0%.
A clip fault on 2026-08-05 21:05 read 86 mA and 4.0%.

USAGE
    python operator_sense_check.py                  # newest capture anywhere
    python operator_sense_check.py data/raw/sweep_20260805_211626
    python operator_sense_check.py path/to/one_capture.csv
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"

SRC_SMA_V, SRC_SMA_I = 3, 4
QUIET_S = 30.0          # cold-start settle is 40 s; stay clear of its end
WINDOW_S = 0.2          # the averaging window the analysis uses

SIGMA_OK, SIGMA_BAD = 35.0, 60.0        # mA
RPCT_OK, RPCT_BAD = 2.5, 3.5            # % of R, after 200 ms averaging


def newest_capture() -> Path | None:
    caps = [c for d in RAW.glob("*") if d.is_dir()
            for c in d.glob("c*.csv")
            if c.name not in ("cycles.csv", "summary.csv", "summary_report.csv")]
    return max(caps, key=lambda p: p.stat().st_mtime) if caps else None


def measure(path: Path):
    """(sigma_i_mA, R_ohm, R_window_pct, corr) from the pre-pulse quiet window."""
    cols: dict[int, list[tuple[float, float]]] = {SRC_SMA_V: [], SRC_SMA_I: []}
    with open(path) as fh:
        rd = csv.reader(fh)
        next(rd)
        for row in rd:
            s = int(row[0])
            if s in cols:
                cols[s].append((float(row[1]) * 1e-6, float(row[2])))
    arr = {}
    for k, rows in cols.items():
        rows.sort()
        arr[k] = (np.array([t for t, _ in rows]), np.array([v for _, v in rows]))
    tv, v = arr[SRC_SMA_V]
    _, i = arr[SRC_SMA_I]
    n = min(len(v), len(i))
    if n < 5000:
        raise SystemExit(f"{path.name}: only {n} SMA samples — not a capture")
    v, i, tv = v[:n], i[:n], tv[:n]
    m = (tv - tv[0]) < QUIET_S
    v, i = v[m], i[m]

    def detrend(x):
        j = np.arange(len(x))
        return x - np.poly1d(np.polyfit(j, x, 2))(j)

    sigma_i = detrend(i).std() * 1e3
    good = i > 0.02                       # guard the divide, not a data filter
    R = v[good] / i[good]
    dR = detrend(R)
    fs = len(R) / QUIET_S
    w = max(1, int(WINDOW_S * fs))
    blk = dR[:len(dR) // w * w].reshape(-1, w).mean(axis=1)
    corr = float(np.corrcoef(v - v.mean(), i - i.mean())[0, 1])
    return sigma_i, float(R.mean()), 100 * blk.std() / R.mean(), corr


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        cap = newest_capture()
        if cap is None:
            print("no captures under data/raw/", file=sys.stderr)
            return 2
    elif target.is_dir():
        caps = sorted(c for c in target.glob("c*.csv")
                      if c.name not in ("cycles.csv", "summary.csv",
                                        "summary_report.csv"))
        if not caps:
            print(f"no capture CSV in {target}", file=sys.stderr)
            return 2
        cap = caps[0]
    else:
        cap = target

    sigma, r, rpct, corr = measure(cap)
    print(f"\n  {cap.parent.name}/{cap.name}")
    print(f"  quiet window {QUIET_S:.0f} s (pre-pulse, wire at the idle bias)\n")
    print(f"    sma_i sigma   {sigma:7.1f} mA      healthy ~26")
    print(f"    R             {r:7.2f} ohm     contact resistance rides here")
    print(f"    R @{WINDOW_S*1e3:.0f} ms     {rpct:7.2f} %       healthy ~1.7-2.0")
    print(f"    corr(V,I)     {corr:+7.3f}         informational — see docstring")

    if sigma < SIGMA_OK and rpct < RPCT_OK:
        print("\n  HEALTHY — sense chain matches the 08-04/08-05 baseline.\n")
        return 0
    if sigma > SIGMA_BAD or rpct > RPCT_BAD:
        print(f"\n  BAD — the sense chain is {sigma/26:.1f}x its normal noise "
              f"floor.\n  R @{WINDOW_S*1e3:.0f} ms is {rpct:.1f} %, against a "
              f"~3 % dR transition: the payload is buried.\n  Mechanical, not "
              f"electrical — a power cycle does NOT fix this. Redo the SMA "
              f"clips\n  (clean bare metal, sense leads on the wire not the "
              f"jaw), then re-run.\n")
        return 1
    print("\n  MARGINAL — above baseline but not clearly faulted. Reseat and "
          "re-check\n  before committing a long run.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
