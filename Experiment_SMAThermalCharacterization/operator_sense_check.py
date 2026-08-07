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

WHAT IT MEASURES — THE IDLE SAMPLES OF THE WHOLE CAPTURE, pulses excised.
(Rewritten 2026-08-06; the first version read "the first 30 s". See THE WINDOW
below for why that number moved without the rig changing.)
    sma_i sigma   per-sample current noise at the idle bias. THE headline
                  number, and now comparable BETWEEN CONDITIONS.
    R             cold resistance, as mean(V)/mean(I) — see the comment in
                  measure(). Tracks the WIRE, and it is stable: 4.01-4.06 ohm
                  across healthy and faulted captures alike, so it is a check
                  that the coil is the one you think it is, NOT a fault
                  detector. (An earlier version read it per-sample and reported
                  a rise that was the noise, not the wire.)
    R @200 ms     R noise after averaging into the window the analysis uses.
                  Compare against the ~3% dR the self-sensing model must see
                  (order-of-magnitude anchor from the 08-05 fire transition):
                  if this is not well below that, the payload is buried.
    corr(V,I)     INFORMATIONAL ONLY. Common-mode noise cancels in V/I and
                  incoherent noise does not, so a drop between two captures of
                  the SAME condition is meaningful — but the absolute value
                  swings with pulse level, so it is not a health threshold. The
                  verdict below uses sigma and R @200 ms only.

THE WINDOW — two reasons the original one was not measuring the rig
    1. The 40 s settle is NOT IN THE CAPTURE. Both operator_current_sweep.py
       and operator_pulse_capture.py discard the settle capture and save
       lead(2 s) + run, so "the first 30 s" was 2 s of idle plus a HEAT PULSE
       and its cool tail. Pulse amplitude then rides straight into sigma: the
       SAME healthy sweep read 26.0 mA on its 250 mA/100 ms capture and 57.5 mA
       on its 650 mA/300 ms one — one rig, one hour, 2.2x apart, straddling the
       old 35 mA HEALTHY limit. Any campaign probed at >=450 mA would have been
       graded MARGINAL or BAD while perfectly healthy. The 2026-08-05 fault WAS
       real and IS about the size first reported (2.8x on the metric below),
       but the number it was read off could not have shown that: it compared a
       250 mA healthy capture against a 650 mA night one.
    2. hw_us is a 32-bit microsecond counter and WRAPS every 71.6 min. Sorting
       the raw counter (what this script did) puts the post-wrap tail first, so
       on a capture that straddles a wrap the "first 30 s" was an arbitrary
       mid-capture span. analysis/analyze_raw.py has unwrapped per src since
       2026-07-31; this script now does too.
    So: unwrap, keep every sample within a robust +/-8 sigma of the idle
    median (which excises the pulses and nothing else), and measure there.
    ~180 000 samples over the full ~186 s instead of ~27 000 over one pulse.

STATES THIS RIG HAS BEEN IN, all measured on this window:

    sigma 7.0 mA                     THE ADC's OWN FLOOR, measured 2026-08-07
                                     with A0 and A1 jumpered to GND. NOT an
                                     operating figure — no source is attached,
                                     and any real source adds its own noise.
                                     Quoted here because it is the yardstick:
                                     everything below is a multiple of it.
    sigma 25.0 mA, R @200 ms 1.8 %   USABLE — the whole 2026-08-05 campaign.
                                     Called "healthy" until 08-07; it was
                                     already ~3.5x the floor, carrying the same
                                     fault at 1/100 the glitch rate. It still
                                     collected 264 valid rows at dR = -4 %
                                     +/- 0.6 pp.
    sigma 65-70 mA, R @200 ms 3.9 %  FAULTED — 08-05 night and 08-07 00:45.
                                     Payload buried, dR comes out the WRONG
                                     SIGN. See STATUS.md 2026-08-07.

The thresholds below sit between the second and third state, i.e. they gate on
"can this collect valid data", not on "is this rig at its best". Watch the
per-step sigma trend for drift AWAY from 25 mA — that early warning is what the
08-05 campaign did not have.

A capture with BOTH inputs grounded reads a plausible-looking R (4.30 ohm on
08-07) that is a pure artifact of the calibration mapping 0 V. This script
cannot detect that case — an open COIL it can (see OPEN_A), grounded INPUTS it
cannot. Check that the coil actually actuated before trusting any R.

Note how tight sigma is across a 4.75x range of commanded current (25.0 mA on
all six 08-05 captures, 200-950 mA and 100-500 ms) — that is the property the
old window did not have, and it is what makes a threshold meaningful.
The 2026-08-05 21:05-21:51 fault, and the 2026-08-07 00:45 recurrence, read
sigma 65.4-70.5 mA (2.6-2.8x) and R @200 ms 3.84-4.28 % (2.2x) — with
**R UNCHANGED at 3.79-4.00 ohm**. The noise moved; the wire did not.

R IS THE WIRE'S, NOT A CONSTANT. 4.2 ohm is THIS coil (Dynalloy, 10 mm cold).
Refit the wire and the healthy R moves; sigma and R @200 ms do not, which is
why the verdict uses those two.

USAGE
    python operator_sense_check.py                  # newest capture anywhere
    python operator_sense_check.py data/raw/sweep_20260805_211626
    python operator_sense_check.py path/to/one_capture.csv
    python operator_sense_check.py path/to/pulse_20260806_211500/h7.csv

    A pulse capture (operator_pulse_capture.py) works as well as a sweep
    capture — it is the same CSV schema and it now carries enough idle samples.
    It does NOT work by default though: `pulse_*/` holds h7.csv, not c*.csv,
    so a bare invocation used to skip it silently and grade a stale sweep.
    Both patterns are searched now, but NAME THE FILE when it matters.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"

SRC_SMA_V, SRC_SMA_I = 3, 4
WINDOW_S = 0.2          # the averaging window the analysis uses
HOT_A = 0.06            # a 50 ms mean this far above the idle median is a
                        # commanded pulse, not noise. Smoothing first is what
                        # makes so low a threshold safe: it divides the
                        # per-sample noise by ~7, so 60 mA is ~17 sigma of the
                        # smoothed idle when healthy and still ~6 sigma on the
                        # worst chain measured — a bad rig cannot excise itself
                        # down to no samples. It has to be this low because the
                        # smallest pulse the campaign commands (200 mA) is only
                        # ~75 mA above the idle bias: at 150 mA the 250 mA
                        # pulses of sweep_20260805_154528 stayed IN the window
                        # and read 29.6 mA against 25.0 for the same rig.
SMOOTH_S = 0.05
PRE_S, POST_S = 0.3, 3.0    # excised around each pulse. POST covers the
                            # thermal recovery: R falls ~3% during a fire and
                            # returns over seconds, and that is SIGNAL, not
                            # noise. 3 s is enough — raising it to 10 s moves
                            # nothing (25.0 -> 24.9 mA), which is the check
                            # that no pulse tail is left in the window.
WRAP_US = 2.0 ** 32     # hw_us is a 32-bit microsecond counter -> 71.6 min
OPEN_A = 0.03           # median idle current below this = no circuit attached
                        # (the coil at the v_idle bias draws ~0.12 A)

# The idle window reads the SAME on a healthy rig at every level 200-950 mA
# and every heat 100-500 ms (24.9-25.1 mA over six captures), so these limits
# are set against a genuinely tight baseline rather than a level-dependent one.
SIGMA_OK, SIGMA_BAD = 30.0, 40.0        # mA   healthy 24.9-25.1, faulted ~69
RPCT_OK, RPCT_BAD = 2.3, 2.8            # % of R after 200 ms averaging
                                        #      healthy 1.80-2.05, faulted 3.1-3.7

# Both capture generations: sweeps write cNN_level_*.csv, pulse captures h7.csv.
CAPTURE_GLOBS = ("**/c*_level_*mA_h*ms.csv", "**/h7.csv")


def newest_capture() -> Path | None:
    """Newest capture ANYWHERE under data/raw — including campaigns/<key>/,
    which the original one-level glob could not see, so a campaign-filed rig
    graded a loose inbox capture from days earlier."""
    caps = [c for g in CAPTURE_GLOBS for c in RAW.glob(g)]
    return max(caps, key=lambda p: p.stat().st_mtime) if caps else None


def unwrap_us(us):
    """Undo the 32-bit hw_us wrap. Per src, on file order — each stream is
    monotonic on its own, and a global unwrap would read the src interleave as
    backward jumps. Same treatment as analysis/analyze_raw.py."""
    us = np.asarray(us, dtype=np.float64)
    return us + WRAP_US * np.cumsum(
        np.concatenate(([0.0], np.diff(us) < -WRAP_US / 2)))


def _detrend(x):
    """Remove drift, keep noise. Quadratic over the whole capture: ambient and
    the coil's own thermal settle are slow, the noise being measured is not."""
    j = np.arange(len(x))
    return x - np.poly1d(np.polyfit(j, x, 2))(j)


def measure(path: Path):
    """(sigma_i_mA, R_ohm, R_window_pct, corr) over the capture's idle samples.

    Also returns nothing about the pulses on purpose: a heat pulse is a
    commanded 500 mA step, and including it measures the command, not the
    sense chain (see THE WINDOW in the module docstring)."""
    cols: dict[int, list[tuple[int, float]]] = {SRC_SMA_V: [], SRC_SMA_I: []}
    with open(path) as fh:
        rd = csv.reader(fh)
        next(rd)
        for row in rd:
            s = int(row[0])
            if s in cols:
                cols[s].append((int(row[1]), float(row[2])))
    arr = {}
    for k, rows in cols.items():
        arr[k] = (unwrap_us([t for t, _ in rows]) * 1e-6,
                  np.array([v for _, v in rows]))
    tv, v = arr[SRC_SMA_V]
    _, i = arr[SRC_SMA_I]
    n = min(len(v), len(i))
    if n < 5000:
        raise SystemExit(f"{path.name}: only {n} SMA samples — not a capture")
    # V and I are pushed as one sample pair per conversion and share hw_us
    # exactly (verified: max |tv-ti| = 0 across the 08-05 captures), so index
    # pairing after the common truncation is exact.
    v, i, tv = v[:n], i[:n], tv[:n]

    fs = len(i) / max(tv[-1] - tv[0], 1e-6)
    segs = idle_segments(i, fs)
    if sum(b - a for a, b in segs) < 5000:
        raise SystemExit(f"{path.name}: no usable idle window "
                         f"({sum(b - a for a, b in segs)} samples)")
    # Everything is computed PER SEGMENT and concatenated: detrending or
    # block-averaging across an excised pulse would report the step across the
    # gap as noise.
    # OPEN CIRCUIT. Disconnecting the coil and re-probing is the test that
    # splits "the wire" from "the sense electronics" (2026-08-07: it proved the
    # glitching is entirely electronic). With no current path there is no R to
    # report — and the `i > 0.02` divide guard below would then keep ONLY the
    # glitch samples and compute a resistance from them, which is how that run
    # first reported a confident 5.51 ohm. sigma is still exactly the number
    # the test is asking for, so measure it and mark R unavailable.
    open_circuit = float(np.median(i)) < OPEN_A

    w = max(1, int(WINDOW_S * fs))
    di, dv, blocks, sum_v, sum_i = [], [], [], 0.0, 0.0
    for a, b in segs:
        ii, vv = i[a:b], v[a:b]
        if not open_circuit:
            good = ii > 0.02              # guard the divide, not a data filter
            ii, vv = ii[good], vv[good]
        if len(ii) < 2 * w:
            continue
        di.append(_detrend(ii))
        dv.append(_detrend(vv))
        sum_v += vv.sum()
        sum_i += ii.sum()
        if open_circuit:
            continue
        # RATIO OF MEANS, NEVER MEAN OF RATIOS — at these noise levels the
        # difference is the whole measurement. E[V/I] ~ R(1 + sigma_I^2/I^2),
        # so per-sample V/I reports a resistance that RISES WITH THE NOISE: 20%
        # noise inflates R by 4%, 45% noise by 20%. That artifact is what
        # "R climbed 4.22 -> 4.68 ohm with every reseat" was on 2026-08-05 —
        # by ratio of means the wire sat at 4.0 ohm throughout and never moved.
        # Block the CHANNELS, then divide: 200 ms of averaging cuts sigma_I/I
        # by ~sqrt(w), which drops the bias to parts per thousand.
        n_blk = len(ii) // w
        vb = vv[:n_blk * w].reshape(-1, w).mean(axis=1)
        ib = ii[:n_blk * w].reshape(-1, w).mean(axis=1)
        blocks.append(_detrend(vb / ib))
    if not di:
        raise SystemExit(f"{path.name}: no idle segment long enough to measure")
    di, dv = np.concatenate(di), np.concatenate(dv)
    corr = float(np.corrcoef(dv, di)[0, 1])
    if open_circuit:
        return di.std() * 1e3, float("nan"), float("nan"), corr
    R_mean = sum_v / sum_i
    blk = np.concatenate(blocks)
    return di.std() * 1e3, float(R_mean), 100 * blk.std() / R_mean, corr


def idle_segments(i, fs):
    """[(start, stop)] index ranges of the capture with the pulses — and their
    thermal recovery — cut out. Everything left is the wire sitting at the
    v_idle bias, which is the only part of a capture that measures the SENSE
    CHAIN rather than the command."""
    med = float(np.median(i))
    w = max(1, int(SMOOTH_S * fs))
    smooth = np.convolve(i, np.ones(w) / w, mode="same")
    hot = smooth > med + HOT_A
    edges = np.flatnonzero(np.diff(hot.astype(np.int8)))
    # A capture can START or END mid-pulse, so the run boundaries are not
    # always an even number of edges — close them explicitly.
    bounds = np.concatenate(([0] if hot[0] else [], edges + 1,
                             [len(i)] if hot[-1] else [])).astype(int)
    pre, post = int(PRE_S * fs), int(POST_S * fs)
    blocked = np.zeros(len(i), dtype=bool)
    for a, b in zip(bounds[0::2], bounds[1::2]):
        blocked[max(0, a - pre):min(len(i), b + post)] = True
    segs, k = [], 0
    while k < len(i):
        if blocked[k]:
            k += 1
            continue
        j = k
        while j < len(i) and not blocked[j]:
            j += 1
        if j - k >= 2 * int(WINDOW_S * fs):
            segs.append((k, j))
        k = j
    return segs


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        cap = newest_capture()
        if cap is None:
            print("no captures under data/raw/", file=sys.stderr)
            return 2
    elif target.is_dir():
        # A sweep folder: grade its FIRST condition, the one closest to the
        # state the rig was in when the operator walked away from it. A pulse
        # folder holds exactly one capture, h7.csv.
        caps = sorted(c for c in target.glob("c*_level_*mA_h*ms.csv"))
        caps += [c for c in target.glob("h7.csv")]
        if not caps:
            print(f"no capture CSV in {target}", file=sys.stderr)
            return 2
        cap = caps[0]
    else:
        cap = target

    sigma, r, rpct, corr = measure(cap)
    print(f"\n  {cap.parent.name}/{cap.name}")
    print(f"  idle samples of the whole capture — every pulse excised, "
          f"with {POST_S:.0f} s of its recovery\n")
    print(f"    sma_i sigma   {sigma:7.1f} mA      usable 25 / faulted 65+ "
          f"(ADC floor 7)")
    if r != r:
        print(f"    R                 n/a         OPEN CIRCUIT — no coil "
              f"attached, so no R")
        print(f"    R @{WINDOW_S*1e3:.0f} ms         n/a         sigma alone "
              f"decides the verdict below")
    else:
        print(f"    R             {r:7.2f} ohm     the WIRE — 4.0-4.3 on this "
              f"coil, not a fault signal")
        print(f"    R @{WINDOW_S*1e3:.0f} ms     {rpct:7.2f} %       "
              f"usable 1.8 / faulted 3.9")
    print(f"    corr(V,I)     {corr:+7.3f}         informational — see docstring")

    print("")
    v, driver = verdict(sigma, rpct)
    print(explain(v, driver, sigma, rpct))
    return 0 if v == "HEALTHY" else 1


def verdict(sigma: float, rpct: float) -> tuple[str, str]:
    """(HEALTHY|MARGINAL|BAD, the metric that decided it). Importable, so a
    campaign runner gates on the same thresholds this prints."""
    if rpct != rpct:                      # NaN: open circuit, sigma only
        if sigma < SIGMA_OK:
            return "HEALTHY", ""
        return ("BAD" if sigma > SIGMA_BAD else "MARGINAL"), "sigma"
    if sigma < SIGMA_OK and rpct < RPCT_OK:
        return "HEALTHY", ""
    if sigma > SIGMA_BAD or rpct > RPCT_BAD:
        return "BAD", "sigma" if sigma > SIGMA_BAD else "R @200 ms"
    return "MARGINAL", "sigma" if sigma >= SIGMA_OK else "R @200 ms"


def explain(v: str, driver: str, sigma: float, rpct: float) -> str:
    if v == "HEALTHY":
        extra = ("" if sigma > 15 else
                 "  sigma is at the ADC's own floor (~7 mA). If the coil did "
                 "not actuate, the inputs\n  are grounded or the source is "
                 "disconnected — this is not a passing rig.\n")
        return "  HEALTHY — good enough to collect.\n" + extra
    where = (f"sigma {sigma:.1f} mA" if rpct != rpct
             else f"sigma {sigma:.1f} mA, R @{WINDOW_S*1e3:.0f} ms {rpct:.2f} %")
    if v == "BAD":
        return (f"  BAD — {driver} is out of band ({where}).\n"
                f"  The 2026-08-07 fault reads as single-sample UPWARD glitches "
                f"on sma_i and sma_v,\n  Poisson-random at ~200/s, and it is "
                f"NOT the coil: with the SMA DISCONNECTED\n  the current "
                f"channel still showed sigma 61 mA and excursions to +320 mA "
                f"with no\n  circuit attached. Do not reseat the clips — four "
                f"reseats on 08-05 could not have\n  helped, and the R rise "
                f"that justified them was an estimator artifact.\n"
                f"  Look at the shared analog path instead: the ground return "
                f"between the driver\n  board and the Portenta analog ground, "
                f"AREF and its decoupling, the INA296A\n  supply and its lead "
                f"to A1. See STATUS.md, 2026-08-07.\n")
    return (f"  MARGINAL — above baseline ({where}) but not clearly faulted.\n"
            f"  Check {driver} again on a second capture before committing a "
            f"long run.\n")


if __name__ == "__main__":
    sys.exit(main())
