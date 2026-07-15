#!/usr/bin/env python3
"""
Step 1 UDP connectivity check (host side). Listens for the H7's heartbeat
broadcast and reports arrival + any sequence gaps (packet loss).

    python Firmware_EthUDPTest_PIO/udp_listen.py

Binds 0.0.0.0:7777 so it catches the H7's 169.254.255.255 broadcast on the
link-local segment. Ctrl+C to stop. If you see heartbeats with seq incrementing
and no (or rare) gaps, Ethernet + UDP work and we can proceed to Step 2.
"""
import socket
import re
import time

PORT = 7777

def main() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # large receive buffer (rehearsal for the real stream — host-side drops rare)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    s.bind(("0.0.0.0", PORT))
    print(f"listening on UDP :{PORT}  (Ctrl+C to stop)")

    n = 0
    lost = 0
    last_seq = None
    seq_re = re.compile(r"seq=(\d+)")
    t0 = time.time()
    try:
        while True:
            data, addr = s.recvfrom(2048)
            n += 1
            msg = data.decode(errors="replace").strip()
            m = seq_re.search(msg)
            gap = ""
            if m:
                seq = int(m.group(1))
                if last_seq is not None and seq > last_seq + 1:
                    missing = seq - last_seq - 1
                    lost += missing
                    gap = f"  <-- GAP: {missing} lost"
                last_seq = seq
            dt = time.time() - t0
            print(f"[{n:4d}] {dt:6.1f}s  from {addr[0]}:{addr[1]}  {msg}{gap}"
                  f"   (total lost={lost})")
    except KeyboardInterrupt:
        print(f"\nstopped. received {n} packets, {lost} lost "
              f"({100*lost/(n+lost) if (n+lost) else 0:.1f}% loss)")

if __name__ == "__main__":
    main()
