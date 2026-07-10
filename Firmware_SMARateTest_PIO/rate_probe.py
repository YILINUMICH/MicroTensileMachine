#!/usr/bin/env python3
"""rate_probe.py — drive the SMARateTest firmware and capture readSma() timing.

Why this exists: while the H7 floods the port with the sensor stream (~1 kHz of
src=1/2 lines) you cannot hand-type `arm` / `cycle` into `pio device monitor`.
This script sends the commands over the serial RX (independent of the output
flood) and logs EVERY raw line to a file — crucially the `[RATE]` diagnostic
lines, which the normal host parser drops because they contain '['.

The firmware runs a cycle WATCHDOG (wdt_ms=5000): during a `cycle` the host must
send `ping` heartbeats or the run aborts to idle after ~5 s. So this script
pings once a second while the cycle runs.

It reproduces the original run (v_high=3.0, v_idle=0.5, fire=100 ms, cool=3000
ms, 10 cycles), then sits armed-idle a few seconds so the n=64 baseline reads
also stream. The raw log is written LIVE (nothing is lost on Ctrl-C) and a
readSma() timing summary always prints at the end.

Prereq: pyserial. Close `pio device monitor` / sma_console first (COM8 free).

    python rate_probe.py --port COM8
"""
from __future__ import annotations
import argparse
import re
import statistics as st
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial required:  pip install pyserial")

RATE_RE = re.compile(r"\[RATE\]\s+n=(\d+)\s+readSma_us=(\d+)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe SMA readSma() timing on the rate-test firmware")
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--vhigh", type=float, default=3.0)
    ap.add_argument("--vidle", type=float, default=0.5)
    ap.add_argument("--thigh", type=int, default=100, help="fire ms")
    ap.add_argument("--tidle", type=int, default=3000, help="cool ms")
    ap.add_argument("--cycles", type=int, default=10)
    ap.add_argument("--settle", type=float, default=6.0,
                    help="extra armed-idle seconds after cycles (captures n=64 baseline)")
    ap.add_argument("--out", default="rate_probe_log.txt")
    args = ap.parse_args()

    run_s = args.cycles * (args.thigh + args.tidle) / 1000.0 + args.settle + 3.0
    lines: list[str] = []
    n_rate = [0]
    stop = threading.Event()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.2)
    except serial.SerialException as e:
        sys.exit(f"could not open {args.port}: {e}\n"
                 f"(close pio device monitor / sma_console — the port must be free)")

    logf = open(args.out, "w", encoding="utf-8", buffering=1)  # line-buffered = live

    def reader() -> None:
        buf = b""
        while not stop.is_set():
            try:
                data = ser.read(8192)
            except serial.SerialException as e:
                print(f"[reader] serial error: {e}")
                stop.set()
                return
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                s = raw.decode("ascii", "replace").rstrip("\r")
                lines.append(s)
                logf.write(s + "\n")                     # live write
                if s.startswith("[RATE]"):
                    n_rate[0] += 1
                elif s.startswith("[SMA]") or s.startswith("[STATUS]"):
                    print("  " + s)

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    def send(cmd: str) -> None:
        try:
            ser.write((cmd + "\n").encode())
            ser.flush()
        except serial.SerialException as e:
            print(f"[send] serial error on {cmd!r}: {e}")
            return
        if cmd != "ping":
            print(f">>> {cmd}")

    try:
        time.sleep(1.0)                     # let stream settle / flush banner
        send("arm")
        time.sleep(0.4)
        send(f"cycle {args.vhigh} {args.vidle} {args.thigh} {args.tidle} {args.cycles}")
        print(f"... running ~{run_s:.0f}s, pinging 1/s to hold the watchdog")
        t0 = time.monotonic()
        while time.monotonic() - t0 < run_s and not stop.is_set():
            send("ping")                    # heartbeat — resets wdt_last_ping
            time.sleep(1.0)
            el = time.monotonic() - t0
            print(f"    t=+{el:4.0f}s   [RATE] lines captured: {n_rate[0]}", end="\r")
        print()
        send("stop")
        time.sleep(0.3)
        send("disarm")
        time.sleep(0.5)
    except KeyboardInterrupt:
        print("\ninterrupted — summarizing what was captured")
        send("stop"); send("disarm")
    finally:
        stop.set()
        th.join(timeout=2.0)
        try:
            ser.close()
        except Exception:
            pass
        logf.close()
        _summarize(lines, args.out)

    return 0


def _summarize(lines: list[str], out: str) -> None:
    by_n: dict[int, list[int]] = defaultdict(list)
    for s in lines:
        m = RATE_RE.search(s)
        if m:
            by_n[int(m.group(1))].append(int(m.group(2)))
    n_v = sum(1 for s in lines if s.split("\t")[1:2] == ["3"])   # src=3 sma_v samples

    print(f"\nsaved {len(lines)} lines -> {out}")
    print(f"SMA sample points captured (src=3 sma_v): {n_v}")
    if not by_n:
        print("NO [RATE] lines — SMA never armed/cycled. Check that `arm` was accepted "
              "(MCP4728 DAC present?) and the port is the H7 (COM8).")
        return
    print("\nreadSma() duration by ADC_SAMPLES (n):")
    print(f"  {'n':>4}  {'count':>6}  {'median_ms':>10}  {'min_ms':>7}  {'max_ms':>7}  {'~pts/100ms fire':>16}")
    for N in sorted(by_n):
        v = by_n[N]
        med = st.median(v) / 1000.0
        pts = 100.0 / med if med > 0 else float("inf")
        print(f"  {N:>4}  {len(v):>6}  {med:>10.2f}  {min(v)/1000:>7.2f}  {max(v)/1000:>7.2f}  {pts:>16.1f}")
    print("\nInterpretation:")
    print("  - If n=16 median ~= n=64 median / 4  -> averaging dominates; lower N buys rate.")
    print("  - If n=16 median barely below n=64   -> M7 loop is the wall; needs HW oversampling.")


if __name__ == "__main__":
    sys.exit(main())
