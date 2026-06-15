# SDS2000X Plus — Host PC Connection Guide (Method A & Method C)

Controlling and reading from a Siglent SDS2000X Plus oscilloscope from a host PC.

This document covers two remote-control paths:

- **Method A** — PC ↔ **USB-to-Ethernet dongle** ↔ scope **LAN** port (network/SCPI-socket control)
- **Method C** — PC ↔ scope rear **USB Device** port (direct USBTMC, no IP)

> Source note: setup menu paths, port functions, and the web/SCPI feature list are from the **SDS2000X Plus User Manual (EN01C)**. Details of the SCPI command set, the raw-socket **port 5025**, USBTMC behaviour, and VISA resource strings live in the separate **Siglent Programming Guide** — the User Manual repeatedly defers to it (§31.2). Items below are tagged `[Manual]` or `[Prog. Guide / standard]` so you know where each fact originates.

---

## 0. Quick comparison

| | **Method A — USB-Ethernet → LAN** | **Method C — USB Device port** |
|---|---|---|
| Physical link | USB-Ethernet dongle on PC → RJ45 → scope LAN port | USB-A/B cable, PC → scope rear **USB Device** port |
| Addressing | IP / subnet (DHCP or static) | None (USBTMC enumerates as a VISA instrument) |
| PC software needed | None for web UI; raw sockets or VISA for SCPI | VISA layer (NI-VISA / Keysight IO / pyvisa backend) |
| Web browser UI | ✅ Yes | ❌ No |
| Raw TCP socket (port 5025) | ✅ Yes | ❌ No (USBTMC, not socket) |
| Best for | Scripted sweeps, large waveform pulls, your existing socket workflow | Single-bench tether where no Ethernet is wanted |
| Manual reference | §7.3.2, §30.6, §31.1, §31.2 | §7.2 (rear panel), §31 intro, §31.2 |

---

## Method A — USB-to-Ethernet dongle → scope LAN port

The scope's only network interface is the rear **RJ45 LAN port**; the "dongle" is a USB-to-Ethernet adapter on the *PC* side (for laptops without an RJ45 jack). Electrically and logically this is identical to a normal LAN connection — the dongle just presents a new NIC to the OS.

### A.1 What you need
- USB-to-Ethernet adapter (Gigabit recommended)
- Ethernet cable (Cat5e+). Modern PHYs auto-MDIX, so a straight cable works for a direct point-to-point link; a switch in between is also fine.
- Scope LAN port — rear panel, item **C** `[Manual §7.2]`

### A.2 Scope-side setup `[Manual §7.3.2, §30.6.1]`
1. Connect the cable: PC dongle → (optional switch) → scope rear LAN port.
2. On the scope: **Utility > System Setting > I/O Setting > LAN Config**.
3. Choose addressing:
   - **Static (recommended for a direct/point-to-point link):** leave *Automatic (DHCP)* **unchecked**, then set **IP address**, **Subnet mask**, and **Gateway**.
     - Example (point-to-point, your established scheme): `169.254.111.100 / 255.255.255.0`, gateway can be left default — it is unused on a direct link.
   - **DHCP:** check *Automatic (DHCP)* **only** if a DHCP server is present on the segment (i.e. you went through a router/switch with DHCP). A bare PC↔scope link has no DHCP server, so static is the right call there.
4. Note the **MAC address** (read-only field) if you later need a DHCP reservation. `[Manual §30.6.1]`
5. (Multi-instrument only) If you'll browse two or more Siglent units, give each a unique **VNC port** in the range **5900–5999**. `[Manual §30.6.1]`
6. (Optional) Set a Web Server password: **Utility > System Setting > I/O Setting > Web Server** (≤ 20 bytes). `[Manual §30.6.2]`

### A.3 PC-side setup (Windows)
1. The dongle enumerates as **its own NIC** — configure *that* adapter, not the onboard one.
   - *Control Panel > Network Connections > [dongle adapter] > Properties > IPv4 > Properties.*
2. For the static example above, set the dongle NIC to a **different host in the same subnet**:
   - IP `169.254.111.50`, mask `255.255.255.0`, gateway blank.
3. Pin it manually. If Windows leaves the dongle on "Obtain automatically" it may self-assign a *random* 169.254.x host (APIPA), which works but is non-deterministic — set it explicitly so the address never moves.
4. **Avoid subnet collisions:** make sure no *other* active NIC (onboard Ethernet, Wi-Fi) is also sitting in the scope's subnet, or the OS routing table may send packets out the wrong interface.

### A.4 Verify the link
1. `ping <scope-IP>` (e.g. `ping 169.254.111.100`). No reply → see A.6.
2. Open a browser to `http://<scope-IP>` — the built-in web server needs no installed software. `[Manual §31.1]`
   - You should see the **instrument control interface** (a live mirror of the display you can drive with the mouse).
3. Only after the web UI works, move to SCPI — that isolates IP/cabling problems from command-syntax problems.

### A.5 Control & read methods
- **Web browser** `[Manual §31.1]` — mirror display + mouse control; **screenshot**; **save waveform as binary (`*.bin`)** and download; download the **Bin2CSV** converter; firmware upgrade.
- **Raw TCP socket** `[Prog. Guide / standard]` — connect to **port 5025**, send SCPI (`*IDN?`, `WFSU`, `SARA?`, `PAVA? PKPK`, `C1:WF? DAT2`, …). This is the lightest path for scripted sweeps and large binary waveform pulls; no VISA layer required.
- **NI-VISA / Telnet** `[Manual §31.2]` — the manual explicitly lists NI-VISA, Telnet, and Socket as supported transports and points to the Programming Guide for command detail.

### A.6 Method A troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ping` times out | PC dongle and scope not in same subnet | Re-check both IPv4 settings share subnet (e.g. both `…111.x / 255.255.255.0`) |
| `ping` times out, IPs look correct | Wrong NIC configured / another NIC in same subnet | Confirm you edited the **dongle** adapter; disable or re-address any other NIC in that subnet |
| Web UI loads but is sluggish | 10/100 dongle or marginal cable | Use a Gigabit dongle, swap cable, avoid USB hubs |
| Web UI unreachable but ping OK | Web server / VNC port conflict | Check VNC port (5900–5999) is unique if multiple units; restart scope |
| Web UI prompts for password | Web Server password set | Enter it, or clear it under §30.6.2 |
| DHCP picked, no address | No DHCP server on a direct link | Switch scope and PC to **static** |
| SCPI connects, web works, but commands return nothing | Wrong port / command syntax | Confirm **port 5025**; verify against Programming Guide; test `*IDN?` first |
| Address changes between sessions | APIPA auto-assignment | Set static IPs on **both** ends |
| General LAN setup reference | — | `[Manual §7.3.2 LAN, §30.6 I/O Setting]` |

---

## Method C — Direct USB Device port (USBTMC)

The rear panel has **two USB roles**: one **USB Device** port "to connect with a PC for remote control," plus a **USB Host** port for storage/mouse/keyboard. `[Manual §7.2, item D]` Method C uses the **Device** port. There is no IP — the PC sees the scope as a USBTMC instrument through a VISA layer.

> The User Manual is light on this port (it states only that it exists for PC remote control). USBTMC class behaviour, the VISA resource string, and the SCPI set come from the **Programming Guide**. `[Manual §31 intro, §31.2]`

### C.1 What you need
- USB cable matching the rear **USB Device** connector (commonly USB-A → USB-B; confirm against your unit's port).
- A VISA runtime on the PC: **NI-VISA**, Keysight IO Libraries, or a `pyvisa` backend (`pyvisa-py` works without full NI-VISA for USBTMC in many setups). `[Prog. Guide / standard]`

### C.2 Scope-side setup
1. Plug the USB cable into the rear **USB Device** port (not the Host port, and not a front USB port). `[Manual §7.2]`
2. No menu configuration is required for USBTMC — there is no LAN-style I/O dialog for the device port.

### C.3 PC-side setup (Windows)
1. Install the VISA runtime **before** plugging in, so the USBTMC driver binds on enumeration.
2. Plug in; confirm the device appears:
   - NI MAX → *Devices and Interfaces* → look for a USB instrument, **or**
   - `pyvisa`: `rm = pyvisa.ResourceManager(); rm.list_resources()` → expect a `USB0::0xF4EC::…::INSTR` style string (Siglent VID `0xF4EC`). `[Prog. Guide / standard]`

### C.4 Verify
1. Open the resource and query identity:
   ```python
   import pyvisa
   rm = pyvisa.ResourceManager()
   scope = rm.open_resource('USB0::0xF4EC::0x????::SN::INSTR')  # from list_resources()
   print(scope.query('*IDN?'))
   ```
2. A valid `*IDN?` string confirms the USBTMC link end-to-end.

### C.5 Control & read methods
- Same SCPI command set as LAN, transported over USBTMC instead of a socket. Your existing per-command logic ports directly; you lose the raw-socket convenience and the **web browser UI** (web UI is LAN-only). `[Manual §31]`
- For large/binary waveform transfers, set the VISA read termination and chunk size appropriately for binary blocks (no LF terminator inside the block payload). `[Prog. Guide / standard]`

### C.6 Method C troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Device not in NI MAX / `list_resources()` | VISA installed after plug-in, or wrong port | Install VISA first; re-plug into the rear **USB Device** port; try another cable |
| Enumerates then drops | Cable / hub power | Use a short, direct cable; avoid passive hubs |
| Cable plugged but nothing happens | Plugged into USB **Host** port | Move to the **Device** port (§7.2 item D) |
| `*IDN?` errors out | Wrong resource string or VISA backend | Re-copy the exact `USB0::…::INSTR` string; verify backend supports USBTMC |
| Binary waveform truncated/garbled | Read termination set for ASCII | Disable termination char for binary `WF?` reads; size buffer to the block length |
| Conflicts with NI-VISA + pyvisa-py | Two backends fighting for the device | Use one backend; specify it explicitly in `ResourceManager('@py')` or `('@ni')` |
| Port itself suspect | Hardware | Cross-check with a known-good USB device; see general USB notes in **Troubleshooting §32** |

> Note: User Manual **Troubleshooting §32** item 7 ("USB storage device cannot be recognized") concerns the **Host** port and FAT32 storage media — it does **not** describe the USB *Device* (PC control) port. Don't apply those steps to Method C.

---

## Appendix — User Manual reference map

Sections/pages to pull when configuring or troubleshooting (SDS2000X Plus User Manual, EN01C):

| Topic | Section | Page |
|---|---|---|
| Rear panel — LAN port (C) and USB Device/Host ports (D) | §7.2 Rear Panel Overview | 36 |
| Physical LAN connection + menu path | §7.3.2 LAN | 37 |
| USB peripherals (Host port — storage/HID) | §7.3.3 USB Peripherals | 37 |
| LAN Config — DHCP vs static, IP/subnet/gateway, VNC port (5900–5999), MAC | §30.6.1 LAN | 333 |
| Web Server password setup | §30.6.2 Web Server | 334 |
| Remote control overview (LAN port + USB Device port) | §31 Remote Control intro | 345 |
| Built-in web browser control (screenshot, .bin save, Bin2CSV, firmware) | §31.1 Web Browser | 345–346 |
| Other connectivity — NI-VISA / Telnet / Socket, defers to Programming Guide | §31.2 Other Connectivity | 346–347 |
| Troubleshooting (USB **storage**, LAN-adjacent issues) | §32 Troubleshooting | 348–351 |

**Not in the User Manual — see the Siglent Programming Guide (siglent.com):**
- SCPI command reference (`WFSU`, `SARA?`, `BSWV`, `CLSW`, `PAVA?`, `C1:WF? DAT2`, `C1-C2:PAVA? PHA`, …)
- Raw-socket **port 5025**
- USBTMC VISA resource string format and Siglent USB VID (`0xF4EC`)
- Binary waveform block structure / descriptor parsing
