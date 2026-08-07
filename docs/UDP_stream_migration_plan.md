# Plan — move the H7 sensor/SMA stream to UDP

**Status:** ADOPTED 2026-08-07 (branch `feat/udp-stream`); was Proposed 2026-07-15. **Owner:** Yilin.
**2026-08-07 update:** the USB wedge investigation (see
`Firmware_SMAConstantCurrent_PIO/STATUS.md`) proved the CDC path can wedge
HOST-side (usbser.sys) — the firmware now self-heals that, but this plan is
the structural fix and is now in motion. `[env:portenta_m7_nbtx_udp]`
(wedge-fix stack + UDP transport) is built and flashed, link readiness
verified (PC `Ethernet 5` Up at 10 Mbps, APIPA 169.254.245.100/16 matching
the H7's static 169.254.245.50/16 — the zero-host-config variant of §3).
First UDP datagram not yet observed; bring-up steps in the firmware STATUS.
**Scope:** `Firmware_SMASensorHub_PIO` (M7) + host `Experiment_SMAThermalCharacterization` / `Calibrate_LaserHead/portenta_reader.py`.

## 1. Why

The M7 runs a **cooperative** control loop: `serviceSma()` checks the heat/cool
timer in the *same* super-loop that streams samples over **USB-CDC**. USB-CDC has
**flow control** — when the host reader falls behind, the M7's `Serial.write`
**blocks**, `serviceSma()` is serviced late, and the SMA cool phase overshoots
(measured 4–5 s stalls; a 3 s cool ran to 7 s). This is real actuation
distortion + M4→M7 ring overflow, not just a plotting artifact.

Host-side mitigations (4 MB RX buffer, reader core-pin + priority, camera in its
own process) cut stalls 4–5 s → 1–2 s and cools 7 s → ~3.3 s, but the bottleneck
just **moves to the next main-thread GIL hog** (currently the GUI plot). As long
as the transport has flow control, the M7's timing is hostage to host
scheduling. The GUI plot cannot be shrunk (it loses meaning), and a firmware
non-blocking write gated on `Serial.availableForWrite()` **failed** — that call
returns ≈0 on the Portenta mbed USB-CDC, so it dropped nearly all data.

**UDP is fire-and-forget:** the M7 hands a datagram to the network stack and
moves on. No flow control → the send never blocks → **the control loop can never
be stalled by the host**, regardless of what the GUI/camera/plot does. Lost
packets just drop (recoverable in analysis via `hw_us` + `seq`); the *actuation*
stays on time. This is the structural fix.

## 2. Architecture — two channels, split by role

```
          ┌────────────────────────── PC (host) ──────────────────────────┐
          │  commands + ping heartbeat  ──►  USB-CDC (COM8)  ──►  M7 reads  │
          │  [STATUS] + boot banner     ◄──  USB-CDC (COM8)  ◄──  M7 emits  │
          │  sensor + SMA sample stream ◄══  UDP :7777       ◄══  M7 sends  │
          └────────────────────────────────────────────────────────────────┘
```

- **USB-CDC (unchanged):** PC→M7 commands (`arm`, `cycle …`, `disarm`, `ping`
  heartbeat, `netcfg`), and M7→PC low-rate text (`[STATUS]`, boot banner). This
  is low-rate and never caused the stall, so it stays exactly as-is — including
  the single-owner command path and the heartbeat.
- **UDP (new, one direction M7→PC):** the high-rate `src=1..5` sample stream.
  Fire-and-forget. This is the only path that must never block the M7.

Rationale for the split: only the **high-rate stream** blocks the loop; commands
are sparse. Keeping commands on the proven serial path means minimal disruption
and no new command-reliability problems (serial is reliable/ordered).

## 3. Network setup

- Direct point-to-point link: PC's USB 2.5 GbE dongle (`Ethernet 5`) ↔ H7 carrier
  RJ45. Confirmed present: a live link exists (10 Mbps; plenty — stream is
  ~1 Mbps). No switch/router → negligible physical loss.
- **Static IPs (recommended)** so nothing depends on APIPA/DHCP:
  - PC NIC `Ethernet 5`: `192.168.7.1 / 255.255.255.0` (user sets this once).
  - H7: `192.168.7.2 / 255.255.255.0`.
  - (Interim alt with zero host config: leave the PC on its APIPA `169.254.x`
    and give the H7 a static `169.254.245.50/16` — same link-local segment.)
- UDP data port **7777** (H7 → PC). PC binds and receives.
- **Target discovery:** the PC sends its `ip:port` to the H7 over the serial
  command channel at startup — `netcfg 192.168.7.1 7777` — so the H7 streams to
  whoever asked. No hardcoded PC IP in firmware. (Fallback: a build-flag default.)

## 4. Firmware changes (M7 only; M4 untouched)

1. **Ethernet bring-up** in `setup()` (M7 branch): `Ethernet.begin(mac, ip)`
   static (Portenta mbed `Ethernet`/`PortentaEthernet`). Verify MAC handling and
   that the carrier PHY links.
2. **UDP socket:** `EthernetUDP udp; udp.begin(localPort);`.
3. **`netcfg` command:** parse `netcfg <ip> <port>` from the serial command
   dispatcher; store the PC endpoint; enable streaming once set.
4. **Replace the two stream writes** (`smaFlush()` / `streamSma`, and the
   `pumpSensors()` sensor batch) with UDP sends: accumulate lines into a datagram
   buffer ≤ ~1400 B (under the 1500 MTU to avoid IP fragmentation), then
   `udp.beginPacket(pcIp, pcPort); udp.write(buf, len); udp.endPacket();`.
5. **Keep `serviceSma()` timing in the loop as-is** — with a non-blocking UDP
   send, the loop returns promptly and the cool timer fires on time. **This is
   the whole fix.**
6. **Keep `[STATUS]` + commands + boot on `Serial`** (USB-CDC), unchanged.
7. Guard with a build flag (`-D H7_TRANSPORT_UDP`) so the USB-only build is
   preserved for rollback.

**⚠ Must verify:** that `EthernetUDP::endPacket()` on the Portenta mbed stack is
effectively non-blocking (UDP has no flow control, but confirm the TX path
doesn't stall when the descriptor ring is briefly full). This is the premise of
the whole plan — validate it in Step 1 before the full cutover.

## 5. Packet format

**v1 — reuse the existing TSV line format** (minimal host parser change):
- Each datagram = a short header line + N sample lines, e.g.
  `#PKT\t<pkt_seq>\t<n_lines>\n` followed by the exact same
  `t_ms\tsrc\traw\tV\thw\tseq\r\n` lines the host already parses.
- Host splits on `\n`, drops the `#PKT` header (using it for packet-loss stats),
  and feeds the rest to the **unchanged** `parse_line()`.
- Per-sample `seq` (already emitted, per src) → sample-level loss detection.
  `pkt_seq` → coarse packet-level loss stats.

**v2 (optional later):** binary-packed fixed structs for higher efficiency —
only if TSV bandwidth/CPU becomes a limit (it won't at ~1 Mbps).

## 6. Host changes

- **`UdpReader`** (new, mirrors `PortentaReader`'s `poll_event()`): `recvfrom`
  on a non-blocking/timeout UDP socket, split datagram into lines, return one
  `('sample'|'status', item)` per call. Set a large `SO_RCVBUF` (e.g. 4 MB) so
  host-side drops are rare.
- **`H7Worker` uses both:** a `PortentaReader` (serial) for **commands +
  `[STATUS]` + boot**, and a `UdpReader` for the **sample stream**. Its loop
  drains UDP samples, occasionally drains serial `[STATUS]`, and sends commands
  to serial (existing single-owner path). Consider a thin `DualTransport`
  wrapper to keep `H7Worker` tidy.
- **Config:** `h7.transport: usb | udp` (default `usb` until validated),
  `h7.udp_bind_port: 7777`, `h7.pc_ip`, `h7.h7_ip`. On `udp`, the worker sends
  `netcfg <pc_ip> <port>` over serial at startup.
- **Drain-only UDP receiver thread** (recommended): one thread does only
  `recvfrom` into a queue; parsing happens elsewhere. UDP drops don't
  back-pressure the M7, but this keeps host-side loss low under GUI load.

## 7. OPEN QUESTION — do we need packet-loss checking? (recommendation)

**Detect, don't retransmit.**

- **Do NOT add ACK/retransmit or switch to TCP.** Any reliability handshake
  re-couples the M7 to the host (an ACK the host is slow to send re-introduces
  exactly the back-pressure we're escaping). That defeats the purpose.
- **Do detect + report loss — it's nearly free.** Every sample already carries a
  per-src `seq`; the host tracks the expected next `seq` per src and counts gaps.
  Add `pkt_seq` for packet-level stats. Report loss % per src in `meta.json` and
  live in the status bar.
- **Why loss is tolerable:** every sample carries `hw_us`, so lost samples are
  just gaps in the time series — the survivors keep exact timing. Loss degrades
  *resolution*, not *correctness*. SMA metrics (cool timing, R transition
  averaged over a window) tolerate modest loss.
- **Ground-truth loss %:** the M7 already emits per-src produced counts in
  `[STATUS]` (over serial). Host loss % = `(produced − received) / produced`,
  exact and independent of UDP.
- **Decision rule after Step 2:** measure loss. `< ~1 %` → ignore. Higher → apply
  mitigations in order: (1) bigger `SO_RCVBUF`, (2) drain-only receiver thread,
  (3) larger datagrams / fewer packets, (4) confirm the direct link (no
  switch/congestion). Only if loss is catastrophic *and* unavoidable revisit the
  transport — but the direct 10 Mbps link for a 1 Mbps stream should see ~0 %.

## 8. Rollout (incremental — prove each stage)

- **Step 0.** User sets the PC NIC static (`192.168.7.1/24`). Confirm link.
- **Step 1 — connectivity proof.** Minimal firmware: Ethernet up + static IP +
  a 1 Hz UDP heartbeat to the PC. Host: a tiny UDP listener confirms receipt,
  measures RTT/loss, and confirms the H7 is pingable. **Validates that
  `endPacket()` is non-blocking and the path works — before touching the data
  path.** Additive/build-flagged.
- **Step 2 — stream over UDP.** Move `src=1..5` to UDP (TSV + `#PKT` header) +
  host `UdpReader`. Run `cycle 3 0.5 100 3000 10`; verify: cool intervals flat
  ~3.1 s on the `hw_us` timeline with **zero firmware-clock gaps**, loss % low,
  sensor rate ~400 Hz sustained. Commands/`[STATUS]`/heartbeat still on serial.
- **Step 3 — cutover.** Make `transport: udp` the default; keep `usb` as a
  fallback via the config flag + the preserved USB firmware build.

## 9. Verification

- **Timing (the goal):** `sma_v` fire intervals on `hw_us` are flat ~3.1 s; no
  firmware-clock gaps > 0.3 s across a 50-cycle soak.
- **Loss:** per-src received `seq` vs `[STATUS]` produced counts → loss %.
- **No regression:** commands still land (`arm`/`cycle`/`disarm`), heartbeat
  keeps the watchdog fed, `[STATUS]` still arrives.

## 10. Risks / open items

- **`endPacket()` blocking** — the core premise; validate in Step 1.
- **Portenta Ethernet library/API + carrier PHY** — confirm the right include
  (`Ethernet` vs `PortentaEthernet`), MAC handling, and that the carrier breaks
  out the PHY correctly. Build will surface API issues.
- **10 Mbps link** — enough for bandwidth, but investigate why not GbE (PHY /
  dongle negotiation); not a blocker.
- **MTU/fragmentation** — keep datagram payload < ~1472 B.
- **M7 image size** — adding lwIP/Ethernet grows flash/RAM; check headroom
  (currently M7 ~28 % flash, ~15 % RAM — ample).
- **Two transports on the host** — `H7Worker` juggling serial + UDP adds
  complexity; the `DualTransport` wrapper contains it.
- **Campus network** — use the *dedicated* dongle/direct link, not the
  campus-connected `Ethernet` (141.212.x), to avoid firewall/broadcast issues.

## 11. Rollback

The USB-CDC firmware and `transport: usb` host path are preserved (git +
build flag + config). Revert by flashing the USB build and setting
`h7.transport: usb`. Nothing about the UDP work is destructive to the working
serial path.
