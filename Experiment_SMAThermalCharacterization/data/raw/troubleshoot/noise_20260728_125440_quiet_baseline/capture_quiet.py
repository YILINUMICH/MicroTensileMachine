"""Capture a quiet baseline of laser (src=1) and load (src=2) for noise work.

Saves raw rows so the analysis can be re-run without touching the rig again.
Uses hw_us as the time base -- host timestamps carry Windows scheduler jitter
that would smear the spectrum.
"""
import sys, time, csv, collections
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM8"
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
OUT  = sys.argv[3] if len(sys.argv) > 3 else "quiet.csv"

ser = serial.Serial(PORT, 115200, timeout=0.05)
try:
    ser.set_buffer_size(rx_size=8 * 1024 * 1024, tx_size=64 * 1024)
except Exception:
    pass
time.sleep(0.3)
ser.reset_input_buffer()

print(f"capturing {SECS:.0f}s from {PORT} — keep the rig UNDISTURBED")
rows = []
buf = b""
t0 = time.time()
last_note = 0.0
while time.time() - t0 < SECS:
    b = ser.read(65536)
    if not b:
        continue
    buf += b
    *lines, buf = buf.split(b"\n")
    for line in lines:
        f = line.rstrip(b"\r").split(b"\t")
        if len(f) < 6:
            continue
        try:
            src = int(f[1]); raw = int(f[2]); val = float(f[3])
            hw = int(f[4]);  seq = int(f[5])
        except ValueError:
            continue
        if src in (1, 2):
            rows.append((src, hw, val, raw, seq))
    el = time.time() - t0
    if el - last_note >= 10:
        last_note = el
        print(f"  t+{el:5.1f}s  {len(rows)} rows")
ser.close()

with open(OUT, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["src", "hw_us", "value", "raw_code", "seq"])
    w.writerows(rows)

by = collections.Counter(r[0] for r in rows)
print(f"\nsaved {len(rows)} rows -> {OUT}")
NAME = {1: "laser", 2: "load"}
for s in sorted(by):
    v = [r[2] for r in rows if r[0] == s]
    hw = [r[1] for r in rows if r[0] == s]
    span_s = (max(hw) - min(hw)) * 1e-6
    dup = sum(1 for a, b in zip(v, v[1:]) if a == b)
    mean = sum(v) / len(v)
    sd = (sum((x - mean) ** 2 for x in v) / len(v)) ** 0.5
    print(f"  src={s} {NAME[s]:6s} n={len(v):6d}  {len(v)/span_s:6.1f} Hz streamed"
          f"  mean={mean:9.6f} V  sd={1000*sd:7.3f} mV"
          f"  p-p={1000*(max(v)-min(v)):8.3f} mV"
          f"  exact-repeat rows={100*dup/len(v):4.1f}%")
