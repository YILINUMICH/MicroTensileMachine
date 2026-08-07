"""USB-CDC open/close torture harness for the Portenta H7 (COM8).

Purpose
-------
A firmware bug wedges the H7's USB-CDC TX path when the host does rapid
close->reopen cycles: after a wedge the port still OPENS fine but delivers
0 bytes and does not reply to commands; only a physical power cycle (USB +
EVM supply) recovers it. Reopens <= 10 s after a close reproduced the wedge
reliably; reopens >= 90 s did not.

This script is the reproduction / verification harness. It deliberately does
rapid open/close cycles against the streaming firmware and reports how many
cycles complete before the port wedges.

  - Run BEFORE a firmware fix: expect an early wedge (exit code 2).
  - Run AFTER the fix: expect all cycles to pass (exit code 0).

Record the outcome (cycle count, gap used, firmware build) in STATUS.md.

Usage
-----
Fast reproduction attempt (short gap, few cycles):

    python torture_open_close.py --gap 2 --cycles 20

Post-fix soak (default-ish, longer):

    python torture_open_close.py --gap 5 --cycles 50

All arguments:

    --port    serial port           (default COM8)
    --baud    baud rate             (default 115200)
    --cycles  number of open/close cycles (default 50)
    --gap     seconds between close and next open (default 5.0)
    --read-s  seconds to read after each open (default 1.0)
    --log     log file path (default torture_<YYYYmmdd_HHMMSS>.log
              next to this script)

Exit codes
----------
    0   all cycles ALIVE (no wedge)
    2   WEDGED (port opens, 0 stream bytes, no reply to "info" probe)
    3   OPEN-FAIL (the open itself failed twice in one cycle)
    130 interrupted by Ctrl+C

SAFETY
------
This script sends NO command other than the read-only "info" probe (and only
when the stream is silent). It never sends arm, fire, cc, drive, or any other
actuation or configuration command. It is safe to run against a rig with an
SMA wire attached, but it is intended for bench/firmware verification -- do
not run it while a capture or sweep is in progress, since it repeatedly
opens and closes the H7's port.
"""

import argparse
import datetime
import os
import sys
import time

import serial


PROBE = b"info\n"  # the ONLY bytes this script ever writes to the port


def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_line(logf, msg):
    line = "[%s] %s" % (timestamp(), msg)
    print(line)
    logf.write(line + "\n")


def open_port(logf, cycle, port, baud):
    """Open the port, retrying once after 2 s on SerialException.

    Returns the open Serial object, or None if both attempts failed
    (OPEN-FAIL).
    """
    for attempt in (1, 2):
        try:
            return serial.Serial(port=port, baudrate=baud, timeout=0.5)
        except serial.SerialException as exc:
            log_line(logf, "cycle %d open attempt %d FAILED: %s"
                     % (cycle, attempt, exc))
            if attempt == 1:
                time.sleep(2.0)
    return None


def read_for(ser, seconds):
    """Read from ser for up to `seconds`, return everything received."""
    deadline = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < deadline:
        n = ser.in_waiting
        data = ser.read(n if n > 0 else 1)  # read(1) blocks up to timeout
        if data:
            chunks.append(data)
    return b"".join(chunks)


def probe_info(ser):
    """Write the read-only 'info' probe and read up to 2 s for any reply."""
    ser.reset_input_buffer()
    ser.write(PROBE)
    ser.flush()
    return read_for(ser, 2.0)


def run(args, logf):
    total_bytes = 0
    per_cycle = []

    for cycle in range(1, args.cycles + 1):
        ser = open_port(logf, cycle, args.port, args.baud)
        if ser is None:
            log_line(logf, "cycle %d classification: OPEN-FAIL" % cycle)
            print("")
            print("=" * 64)
            print("OPEN-FAIL: the port failed to open twice on cycle %d."
                  % cycle)
            print("This is also an interesting result -- record it in")
            print("STATUS.md (port, gap=%.1fs, cycles completed=%d)."
                  % (args.gap, cycle - 1))
            print("=" * 64)
            return 3, cycle - 1, total_bytes, per_cycle

        try:
            data = read_for(ser, args.read_s)
            nbytes = len(data)
            total_bytes += nbytes
            per_cycle.append(nbytes)
            preview = repr(data[:80])

            if nbytes > 0:
                classification = "ALIVE"
                note = ""
            else:
                # SUSPECT: stream silent. Probe with the read-only "info"
                # command -- the only command this script ever sends.
                log_line(logf, "cycle %d: 0 stream bytes, SUSPECT -- "
                               "sending read-only 'info' probe" % cycle)
                reply = probe_info(ser)
                if reply:
                    classification = "ALIVE"
                    note = " (stream silent but commands alive)"
                    preview = repr(reply[:80])
                else:
                    classification = "WEDGED"
                    note = " (no stream bytes, no reply to 'info' probe)"

            log_line(logf, "cycle %d/%d: bytes=%d class=%s%s data=%s"
                     % (cycle, args.cycles, nbytes, classification, note,
                        preview))
        finally:
            try:
                ser.close()
            except serial.SerialException:
                pass

        if classification == "WEDGED":
            print("")
            print("=" * 64)
            print("PORT WEDGED on cycle %d of %d (gap=%.1fs)."
                  % (cycle, args.cycles, args.gap))
            print("The port opens but delivers 0 bytes and does not reply")
            print("to commands. A POWER CYCLE (USB + EVM supply) is required")
            print("to recover the H7 -- reopening the port will NOT fix it.")
            print("Record this result (cycle number, gap, firmware build)")
            print("in STATUS.md.")
            print("=" * 64)
            return 2, cycle, total_bytes, per_cycle

        if cycle < args.cycles:
            time.sleep(args.gap)

    return 0, args.cycles, total_bytes, per_cycle


def main():
    parser = argparse.ArgumentParser(
        description="Rapid open/close torture test for the H7 USB-CDC wedge "
                    "bug. Sends only the read-only 'info' probe; never sends "
                    "arm/fire/cc/drive.")
    parser.add_argument("--port", default="COM8",
                        help="serial port (default COM8)")
    parser.add_argument("--baud", type=int, default=115200,
                        help="baud rate (default 115200)")
    parser.add_argument("--cycles", type=int, default=50,
                        help="number of open/close cycles (default 50)")
    parser.add_argument("--gap", type=float, default=5.0,
                        help="seconds between close and next open "
                             "(default 5.0)")
    parser.add_argument("--read-s", type=float, default=1.0, dest="read_s",
                        help="seconds to read after each open (default 1.0)")
    parser.add_argument("--log", default=None,
                        help="log file path (default torture_<stamp>.log "
                             "next to this script)")
    args = parser.parse_args()

    if args.log is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "torture_%s.log" % stamp)

    # Line-buffered so a hang mid-run still leaves a complete trail.
    logf = open(args.log, "a", buffering=1, encoding="ascii",
                errors="replace")

    completed = 0
    try:
        log_line(logf, "start: port=%s baud=%d cycles=%d gap=%.1fs "
                       "read_s=%.1fs log=%s"
                 % (args.port, args.baud, args.cycles, args.gap,
                    args.read_s, args.log))
        code, completed, total_bytes, per_cycle = run(args, logf)
        if code == 0:
            log_line(logf, "PASS: %d cycles, total bytes=%d, "
                           "min/max bytes per cycle=%d/%d"
                     % (completed, total_bytes,
                        min(per_cycle) if per_cycle else 0,
                        max(per_cycle) if per_cycle else 0))
        else:
            log_line(logf, "STOPPED with exit code %d after cycle %d"
                     % (code, completed))
        return code
    except KeyboardInterrupt:
        log_line(logf, "interrupted by operator (Ctrl+C); "
                       "cycles completed=%d" % completed)
        print("Interrupted. Cycles completed: %d" % completed)
        return 130
    finally:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
