# Controlling the Keysight E4980A/AL via the LAN Interface

A practical reference for connecting a host PC to the E4980A/AL Precision LCR
meter over LAN, handling the returned measurement data, and troubleshooting the
link. Page references point to the **E4980A/AL User's Guide**.

---

## 1. The LAN approach in brief

The E4980A/AL has a rear-panel **LAN port** (LXI Class C compliant from firmware
A.02.00) that lets a PC send SCPI commands and read results over TCP/IP. The same
SCPI command set works regardless of interface, so anything you do over LAN you
could also do over USB or GPIB — only the transport and the address string differ.

The meter exposes **three ways** to reach it over the network. Pick one based on
how much programming you want to do:

| Method | Transport | Best for | Manual section |
|---|---|---|---|
| **SICL-LAN / VXI-11** | VISA `TCPIP0::<ip>::inst0::INSTR` | Standard programmatic control via VISA (Python/C/VEE/Excel-VBA) | "Control over SICL-LAN server", p. 230 |
| **Telnet / raw socket** | TCP port **5024** (interactive telnet) or **5025** (program socket) | Quick manual checks (5024) or low-latency custom socket code (5025) | "Control over telnet server", p. 234 |
| **Web server** | HTTP, any browser | Zero-code control: virtual front panel, screenshots, data export | "Control via Web server", p. 237 |

**Recommendation:** use the **VISA / SICL-LAN** path for production automation — it
is the most portable and is what the manual's sample programs assume. Use the
**web server** for a quick check that the meter is alive and reachable.

---

## 2. Setting up the link

### 2.1 Configure the meter's IP address (instrument side)

On the front panel: **[System] → SYSTEM CONFIG**, then the **IP CONFIG** field.

- **AUTO** — obtains an address automatically. It tries DHCP first; if no DHCP
  server answers, it falls back to AUTO-IP in the `169.254.x.x` range.
- **MANUAL** — you enter `MANUAL IP ADDR`, `MANUAL SUBNET MASK`, and
  `MANUAL GATEWAY` yourself (press ENTER after each).

After setting, read the **CURRENT IP ADDR / SUBNET MASK / GATEWAY** monitor fields
to confirm the active address, and check that **CURRENT LAN STATUS** reads
`NORMAL` / `OK`.

> Full procedure: *"Configuring the LAN IP address"*, p. 164, and the
> *SYSTEM CONFIG Page* description, p. 158.

**Factory defaults** (p. 454–455): IP CONFIG `AUTO`, MANUAL IP `192.168.1.101`,
SUBNET `255.255.255.0`, GATEWAY `0.0.0.0`. `[Preset] → LAN RESET` (or
`:SYST:COMM:LAN:PRES`) returns LAN settings to these.

Equivalent SCPI for headless configuration (p. 371–375):

```
:SYST:COMM:LAN:CONF MANual          ' or AUTO
:SYST:COMM:LAN:CURRent:ADDRess?     ' query active IP
:SYST:COMM:LAN:CURRent:SMASk?       ' query active subnet
:SYST:COMM:LAN:CURRent:DGATeway?    ' query active gateway
:SYST:COMM:LAN:MAC?                  ' query MAC address
:SYST:COMM:LAN:RESTart               ' restart the network stack
```

### 2.2 Register the instrument on the PC

1. Install **Keysight I/O Libraries Suite (v14 or higher)** — this provides VISA
   and Keysight Connection Expert.
2. Open **Connection Expert** → select **LAN (TCPIP0)** → **+ Instrument**.
3. Enter the meter's IP (Enter Address tab) → **OK**.
4. Confirm it appears under **My Instruments**.

> Procedure: *"Preparing the external controller"*, p. 230.

The resulting VISA resource string is:

```
TCPIP0::192.168.1.101::inst0::INSTR
```

### 2.3 Quick connectivity checks (no code)

- **Browser:** open `http://<meter-ip>/`. The web server start page appears; the
  *Control Instrument* page gives a virtual front panel and a SCPI send/read box.
  The *Configure LAN* page is password-protected (default password: `Keysight`,
  p. 239).
- **Telnet:** `telnet <meter-ip> 5024`, then type a command (e.g. `*IDN?`) and
  press Enter. `Ctrl+]` then `quit` to exit (p. 234).

---

## 3. How the data should be handled

This is the part most worth getting right. Reading data is a sequence of
**configure → trigger → fetch → parse**.

### 3.1 Trigger and fetch model

The trigger system has three states: *Idle*, *Waiting for Trigger*, *Measurement*
(p. 249). For deterministic reads, drive it explicitly with a bus trigger:

```
*RST;*CLS              ' reset, clear status
:TRIG:SOUR BUS         ' trigger on bus command
:INIT:CONT OFF         ' stop free-running
:INIT:IMM              ' arm one cycle
:TRIG                  ' fire trigger
:FETC?                 ' read the result back
```

Key rule (p. 250–252): `*TRG` is exactly equivalent to `:TRIG;:FETC?`. So you can
collapse the last two lines into a single `*TRG` to trigger-and-read in one round
trip.

> ⚠️ Calling a `:FETCh` query when no measurement data exist returns an error.
> Always trigger (or have a completed measurement) before fetching.

### 3.2 Choosing the data format

Set the transfer format **before** fetching (p. 264, command ref p. 347–349):

| Command | Effect |
|---|---|
| `:FORM:DATA ASC` | ASCII output (default). Human-readable, larger, slower. |
| `:FORM:DATA REAL,64` | 64-bit IEEE-754 binary block. Compact, faster, needs binary parsing. |
| `:FORM:ASC:LONG OFF` | 6 significant digits (default). |
| `:FORM:ASC:LONG ON` | 10 significant digits (use only if you need the resolution). |
| `:FORM:BORDer NORMal\|SWAPped` | Byte order for binary mode (MSB- vs LSB-first). |

For most automation, **ASCII / short** is simplest. Switch to **REAL,64** only
when throughput matters (high-rate or large list sweeps).

### 3.3 What a reading looks like

A single `:FETC?` returns comma-separated fields (p. 182, 264):

```
<Data A>,<Data B>,<Status>[,<Bin No.> or <IN/OUT>]
```

- **Data A** — primary parameter (e.g. Cp), Data B — secondary (e.g. D).
- **Status** — `0` = OK, `+1` = overload (data forced to `9.9E37`),
  `+3` = signal over source limit, `+4` = ALC failed. `-1` = no data.
- **Bin No. / IN/OUT** — present only when the comparator / list comparator is on.

Example ASCII line:

```
+1.059517689E-24,+1.954963777E+00,+0,+0
```

**Always check the Status field.** If it is `+1`, the numeric value is the dummy
`9.9E37`, not a real reading — discard or flag it.

### 3.4 Single readings vs. buffered batches

- **Point-by-point:** loop `*TRG` / `:FETC?`. Simple, but each read costs a full
  command round trip — fine for low rates.
- **Buffered (faster):** use the **data buffer memory** (p. 255) — let the meter
  accumulate up to ~201 results, then read them in one transfer with
  `:MEMory:READ?`. This minimizes round trips and is the right pattern for speed.
- **List sweep:** one trigger measures all sweep points; the fetched block repeats
  the `<Data A>,<Data B>,<Status>,<IN/OUT>` group per point (p. 269).

### 3.5 Knowing when a measurement is done

For long apertures or averaging, poll completion via the status register / SRQ
rather than guessing a delay (p. 262):

```
*SRE / :STAT:OPER:ENAB    ' enable SRQ on the operation-status bit going 1 -> 0
```

Configure an SRQ on the measuring-bit transition, trigger, then wait for the SRQ
before fetching. With averaging on, the SRQ fires only after the full average
count completes.

### 3.6 Throughput tips

- Prefer `:FORM:DATA REAL,64` and `:FORM:ASC:LONG OFF`.
- Batch through buffer memory instead of per-point fetches.
- Shorten the aperture (`:APER`) and averaging — the **measurement time usually
  dominates**, far outweighing any LAN-vs-USB transport difference.
- For lowest LAN latency in custom code, use the **raw socket on port 5025**
  rather than VXI-11.

> Worked examples for all of the above (ASCII & binary; comparator, buffer, list)
> are in *Section 9, Sample Program — "Read Measurement Results"*, p. 290–308.

---

## 4. Troubleshooting

> Primary reference: *"Check Items When Trouble Occurs During Remote Control"*,
> p. 430.

### Instrument does not respond / malfunctions

- **Wrong address.** Confirm the IP on the meter's SYSTEM CONFIG screen matches
  the address registered in Connection Expert.
- **Cabling.** Verify the LAN cable is seated and in good condition; check
  **CURRENT LAN STATUS**.
- **Address conflict.** Make sure no other device on the network shares the
  meter's IP.

### CURRENT LAN STATUS values (Table 5-2, p. 165)

| Status | Meaning | Action |
|---|---|---|
| `NORMAL` / `OK` | Link is good | — |
| `FAULT` / `FAILED` | Disconnected or link failed | Check cable, switch port, IP settings |
| `---` / `INITIALIZING` | Link still coming up | Wait; re-check after a few seconds |

### Can't read the measured value back

- **Format mismatch.** Make sure the reader's expected format matches
  `:FORM:DATA` (ASCII vs binary) and `:FORM:ASC:LONG`.
- **Fetch with no data.** Don't `:FETC?` before a measurement has completed —
  trigger first, or use `*TRG`.

### Connection drops or settings won't take

- Restart the network with `:SYST:COMM:LAN:RESTart` (or reboot via
  `:SYST:RESTart` on supported firmware).
- As a last resort, `[Preset] → LAN RESET` to restore factory LAN defaults
  (p. 455), then reconfigure.

### Errors / warnings on screen

- Query the queue with `:SYST:ERR?` and look up the code in *Appendix B, Error
  Messages*, p. 435.

### General

- Confirm **I/O Libraries Suite v14+** is installed; older or mismatched VISA can
  prevent discovery.
- LXI Class C, DHCP/AUTO-IP integration, and the LAN-reset softkey were added at
  firmware **A.02.00** — verify firmware if a feature is missing (*Manual Changes*,
  p. 431–433).

---

## 5. Related user-manual sections (quick index)

| Topic | Page |
|---|---|
| Types of remote control system (overview table) | 225 |
| LAN remote control system (system config, equipment) | 228 |
| Control over SICL-LAN server / preparing the PC | 230 |
| Control over telnet server (ports 5024 / 5025) | 234 |
| Control via Web server (+ default password) | 237 / 239 |
| Rear-panel LAN port description | 37 |
| SYSTEM CONFIG page | 158 |
| Configuring the LAN IP address | 164 |
| LAN connection status (Table 5-2) | 165 |
| Trigger system | 249 |
| Waiting for end of measurement (SRQ) | 262 |
| Data Transfer (ASCII & BINARY formats) | 264 / 267 |
| Data buffer memory | 255 |
| Sample programs — Read Measurement Results | 290 |
| `:FETCh` command reference | 346 |
| `:FORMat` command reference | 347 |
| `:SYSTem:COMMunicate:LAN…` command reference | 371 |
| Troubleshooting remote control | 430 |
| Error messages (Appendix B) | 435 |
| LAN factory default settings | 455 |

---

*Prepared from the Keysight E4980A/AL Precision LCR Meter User's Guide. Page
numbers refer to that document; confirm against your firmware revision, since a
few LAN features depend on firmware A.02.00 or later.*
