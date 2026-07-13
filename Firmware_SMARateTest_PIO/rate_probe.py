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
EMIT_RE = re.compile(r"\bemit_us=(\d+)")
DT_RE = re.compile(r"\bdt_us=(\d+)")     # achieved cadence, measured on-device


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
    by_n: dict[int, list[int]] = defaultdict(list)      # readSma_us
    emit_us: list[int] = []
    dt_us: list[int] = []                               # ACHIEVED cadence
    for s in lines:
        m = RATE_RE.search(s)
        if not m:
            continue
        by_n[int(m.group(1))].append(int(m.group(2)))
        m2 = EMIT_RE.search(s)
        if m2:
            emit_us.append(int(m2.group(1)))
        m3 = DT_RE.search(s)
        if m3 and int(m3.group(1)) > 0:
            dt_us.append(int(m3.group(1)))

    # src=3/4/5 sample rows, and the V/I values — the accuracy check.
    v_vals, i_vals = [], []
    for s in lines:
        f = s.split("\t")
        if len(f) >= 4:
            try:
                if f[1] == "3":
                    v_vals.append(float(f[3]))
                elif f[1] == "4":
                    i_vals.append(float(f[3]))
            except ValueError:
                pass

    # [STATUS] loop ceiling / health
    st_loop, st_drop, st_crc, st_cfg = [], [], [], {}
    for s in lines:
        if not s.startswith("[STATUS]"):
            continue
        for k, store in (("loop_hz", st_loop), ("dropped", st_drop),
                         ("crc_err", st_crc)):
            mm = re.search(rf"\b{k}=(\d+)", s)
            if mm:
                store.append(int(mm.group(1)))
        for k in ("loop_us_avg", "loop_us_max", "cycle_log_ms", "settle_us"):
            mm = re.search(rf"\b{k}=(\d+)", s)
            if mm:
                st_cfg[k] = int(mm.group(1))

    print(f"\nsaved {len(lines)} lines -> {out}")
    print(f"SMA sample points captured (src=3 sma_v): {len(v_vals)}")
    if not by_n:
        print("NO [RATE] lines — SMA never armed/cycled. Check that `arm` was accepted "
              "(MCP4728 DAC present?) and the port is the H7 (COM8).")
        return

    if st_cfg:
        print(f"\nbuild: CYCLE_LOG_MS={st_cfg.get('cycle_log_ms','?')} "
              f"SMA_SETTLE_US={st_cfg.get('settle_us','?')}")

    # ---- 1. THE HEADLINE: did we actually get the rate? ---------------------
    print("\n[1] ACHIEVED SMA CADENCE (dt between consecutive samples, on-device)")
    if dt_us:
        med = st.median(dt_us)
        target = st_cfg.get("cycle_log_ms", 0) * 1000
        print(f"  median dt = {med/1000:.3f} ms  ->  {1e6/med:.0f} Hz "
              f"   ({100.0/(med/1000):.1f} points per 100 ms fire)")
        if target:
            if med > target * 1.25:
                print(f"  ** MISSED the {1000/st_cfg['cycle_log_ms']:.0f} Hz target "
                      f"({target/1000:.0f} ms) — something below is the wall, not the schedule.")
            else:
                print(f"  ** HIT the {1000/st_cfg['cycle_log_ms']:.0f} Hz target.")
    else:
        print("  (no dt_us field — firmware predates the rate-ladder build)")

    # ---- 2. where the time goes ---------------------------------------------
    print("\n[2] WHERE THE TIME GOES (per sample)")
    print(f"  {'n':>4}  {'count':>6}  {'readSma_ms':>11}  {'min':>6}  {'max':>6}")
    for N in sorted(by_n):
        v = by_n[N]
        print(f"  {N:>4}  {len(v):>6}  {st.median(v)/1000:>11.3f}  "
              f"{min(v)/1000:>6.3f}  {max(v)/1000:>6.3f}")
    if emit_us:
        print(f"  emit (3 rows, one batched USB write): "
              f"median {st.median(emit_us)/1000:.3f} ms")
    if st_loop:
        print(f"  M7 loop: {st.median(st_loop):.0f} Hz "
              f"(avg {st_cfg.get('loop_us_avg','?')} us, "
              f"max {st_cfg.get('loop_us_max','?')} us)")
        print("     serviceSma() streams at most ONE sample per loop pass, so the")
        print("     loop rate is a HARD ceiling on the SMA rate.")

    # ---- 3. the silent failure mode -----------------------------------------
    # Runs 0-5 (2026-07-13) found V and I inflating up to +33% as the ADC
    # CONVERSION DUTY rose, with the DAC code unchanged. R = V/I is IMMUNE (both
    # channels scale together, the ratio cancels) and sat at a rock-steady 21.4 Ω
    # through the whole thing — so checking R would have called it a clean pass.
    # Always compare V and I against the DAC CODE, phase by phase.
    print("\n[3] ACCURACY CHECK — is the measurement still telling the truth?")

    # Pair V (src=3, raw column = DAC currentCode) with I (src=4) by seq.
    dv, dc, di = {}, {}, {}
    for s in lines:
        f = s.split("\t")
        if len(f) >= 6:
            try:
                if f[1] == "3":
                    dv[int(f[5])] = float(f[3]); dc[int(f[5])] = int(f[2])
                elif f[1] == "4":
                    di[int(f[5])] = float(f[3])
            except ValueError:
                pass
    keys = sorted(set(dv) & set(di))
    if not keys:
        print("  (no paired V/I samples)")
    else:
        fire = [k for k in keys if dv[k] > 1.75]
        if fire:
            V = [dv[k] for k in fire]
            I = [di[k] for k in fire]
            C = [dc[k] for k in fire]
            vm, im = st.mean(V), st.mean(I)
            print(f"  IN FIRE (n={len(fire)}), DAC code = {st.mean(C):.0f}")
            print(f"    V = {vm:8.4f} V  (sd {st.pstdev(V):.4f})")
            print(f"    I = {im:8.4f} A  (sd {st.pstdev(I):.4f}, "
                  f"{100*st.pstdev(I)/abs(im) if im else 0:.2f}% scatter)")
            print(f"    R = V/I = {vm/im if im else float('nan'):.3f} ohm")
            print()
            print("  >> Compare V and I against RUNG 0 at the SAME DAC code.")
            print("     If the DAC code matches but V has moved, the MEASUREMENT is")
            print("     wrong, not the drive.")
            print("  >> R is NOT a valid check — it cancels the error. Runs 0-5 had R")
            print("     pinned at 21.4 ohm while V drifted +33%.")
        # The diagnostic that actually caught it: error vs ADC conversion duty.
        if by_n and dt_us:
            N = st.median(sorted(by_n))
            rd = st.median(by_n[sorted(by_n)[0]])
            settle = st_cfg.get("settle_us", 0)
            adc_us = rd - 2 * settle
            duty = 100.0 * adc_us / st.median(dt_us)
            print(f"\n  ADC conversion duty = {duty:.0f}%  "
                  f"(adc {adc_us:.0f} us of a {st.median(dt_us):.0f} us cadence)")
            if duty > 20:
                print(f"  ** WARNING: duty > 20%. Runs 0-5 showed V inflating "
                      f"~0.46% per 1% of duty above ~14%.")
                print(f"     Predicted V error here: ~{0.46*(duty-14):+.0f}%. "
                      f"Reduce ADC_SAMPLES_CYCLE.")

    # ---- 4. stream health ----------------------------------------------------
    if st_drop or st_crc:
        d, c = sum(st_drop), sum(st_crc)
        print(f"\n[4] STREAM HEALTH: dropped={d}  crc_err={c}   "
              f"{'OK' if d == 0 and c == 0 else '** NOT CLEAN — the SMA path is starving the sensor drain'}")


if __name__ == "__main__":
    sys.exit(main())
