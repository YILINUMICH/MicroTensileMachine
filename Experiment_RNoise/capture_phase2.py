#!/usr/bin/env python3
"""PHASE 2 — scope capture of VLDO + Vsense for coherence and the alias test.

Implements PHASE 2 of sma_resistance_noise_plan.md on top of the existing
Driver_SiglentOscilloscope module (raw SCPI socket, no VISA).

Two things this does that `Oscilloscope.capture_waveform()` alone cannot:

1. **Both channels from ONE stopped acquisition.** Coherence between two
   separately-grabbed records is meaningless -- they are different noise
   realisations and the cross-spectrum is garbage. So we STOP the scope first,
   then read C1 and C2 out of the frozen record without ever restarting it.

2. **Records the ACTUAL sample rate per capture**, which the change-fs alias
   test depends on entirely.

Probe map (see docs/PLAN_phase6_ldo_characterization.md and README.md):
    C1 -> Portenta A0 pad  = V_LDO   (tapped before the 100 mOhm shunt)
    C2 -> Portenta A1 pad  = Vsense  (INA296A OUT, 10 V/V x 0.1 Ohm = 1 V/A)

Probe AT THE PORTENTA PADS, not at the regulator/INA outputs -- the question is
what the *ADC* sees, so the trace into the ADC pin must be inside the loop.

Usage:
    python capture_phase2.py --out out/scope            # single capture
    python capture_phase2.py --alias-test --out out/scope   # two rates
    python capture_phase2.py --verify-tone              # known-tone sanity check

Analyse with:  python analyze_scope_capture.py out/scope/<file>.npz
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

# Cross-module import shims -- same pattern the recorder uses for its drivers.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "Driver_SiglentOscilloscope"))
sys.path.insert(0, str(_ROOT / "Calibrate_LaserHead"))          # portenta_reader
sys.path.insert(0, str(_ROOT / "Experiment_SMAThermalCharacterization"))  # h7 cmds
from oscilloscope import Oscilloscope, codes_to_volts  # noqa: E402

VLDO_CH, VSENSE_CH = "C1", "C2"

# --- H7 load / drive -------------------------------------------------------
# For PHASE 2 the SMA is replaced by a fixed power resistor. Rationale is the
# Phase-6 plan's: the SMA's resistance climbs as it heats, so the operating
# point drifts *during* the capture -- and PSD/coherence assume a stationary
# signal. A resistor holds the point rock-steady.
#
# The load sits in series with the 100 mOhm sense shunt, so the current the
# INA296A reports is  I = v_drive / (R_LOAD + R_SHUNT).
R_LOAD_DEFAULT = 4.9      # Ohm, the bench resistor
R_SHUNT = 0.1             # Ohm, INA296A sense shunt

H7_PORT_DEFAULT = "COM8"  # COM8 = Portenta H7, COM5 = Zaber (see CLAUDE.md)
H7_BAUD = 115200

# Moving the sample stream to UDP is what makes the command channel reliable.
# On USB the M7 loop (pumpSensors -> pollCommand -> serviceSma) BLOCKS inside
# pumpSensors' Serial.write whenever the host stops draining, so it never
# reaches pollCommand and arm/drive are silently never read -- and after enough
# blocked writes the board stops emitting entirely and needs a power cycle.
# `netcfg <ip> <port>` hands the src=1..5 flood to fire-and-forget UDP, leaving
# serial carrying only commands + [STATUS]. This is exactly what the working
# recorder does (Experiment_SMAThermalCharacterization/lib_workers.py ~L489).
# Values mirror that module's config.yaml.
H7_PC_IP_DEFAULT = "169.254.245.100"
H7_UDP_PORT_DEFAULT = 7777

# SDS2000X Plus grid is 8 vertical divisions (+/-4 from centre). With the
# driver's 25 codes/div that is +/-100 codes of usable screen; int8 saturates at
# 127. Warn well before the screen edge -- a clipped record silently fabricates
# harmonics that look exactly like the spurs we are hunting.
# MEASURED 2026-07-21 on this SDS2204X Plus (fw 5.4.1.5.2R2), not assumed: a
# 1M-point record gave mean_code=10.61 at 0.5 V/div while the scope's own
# PAVA? MEAN read 0.1769 V  ->  10.61 * 0.5 / 0.1769 = 29.99 codes/div.
# The driver's default of 25.0 (flagged "VERIFY" in its STATUS) inflates every
# voltage by 20%, so pass this explicitly to codes_to_volts().
CODES_PER_DIV = 30.0
CLIP_CODES = 95

MIN_SEGMENTS = 8      # must match analyze_r_noise.py
NPERSEG = 8192


def h7_read_cmd() -> str:
    """'read' — live V_LDO / I / V_sma / R straight from the firmware."""
    return "read"


def _num_after(line: str, key: str):
    """Pull the number immediately following `key` in a firmware status line."""
    i = line.find(key)
    if i < 0:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", line[i + len(key):])
    return float(m.group()) if m else None


class H7Drive:
    """Hold the H7 in a steady drive for the duration of a scope capture.

    Command strings come from lib_h7_commands so they cannot drift from the
    firmware dispatcher (Firmware_SMASensorHub_PIO/src/main.cpp::dispatch).

    Why a *steady* drive and not `cycle`: PSD and coherence assume a stationary
    signal. The fire/cool cycle is not stationary -- its harmonics show up as
    sinc lobes that contaminate every per-channel number. `drive` holds one
    operating point.

    Why drive at all: the low-side MOSFET is the master enable, so an un-armed
    board passes ZERO coil current. Vsense would sit at ~0, R = V/I would be a
    divide-by-noise, and -- worst of all -- load-dependent supply/choke noise
    would simply not be present, yielding a false clean result.
    """

    def __init__(self, port: str = H7_PORT_DEFAULT, r_load: float = R_LOAD_DEFAULT,
                 udp: bool = True, pc_ip: str = H7_PC_IP_DEFAULT,
                 udp_port: int = H7_UDP_PORT_DEFAULT):
        self.port = port
        self.r_load = r_load
        self.udp = udp
        self.pc_ip = pc_ip
        self.udp_port = udp_port
        self._reader = None
        self._thread = None
        self._stop = True
        self._rx = []
        self._sock = None

    def __enter__(self):
        import serial
        try:
            self._reader = serial.Serial(self.port, H7_BAUD, timeout=0.05,
                                         write_timeout=1.0)
        except Exception as e:      # noqa: BLE001 - turn into actionable advice
            self._reader = None
            raise RuntimeError(
                f"could not open the H7 on {self.port}: {e}\n"
                f"  - COM8 = Portenta H7, COM5 = Zaber stage (do not mix them up)\n"
                f"  - is the board powered and enumerated? (`pio device list`)\n"
                f"  - the port takes ONE owner: close sma_console / operator_console\n"
                f"    or any recorder session still holding it"
            ) from e
        try:
            self._reader.set_buffer_size(rx_size=4 * 1024 * 1024, tx_size=64 * 1024)
        except Exception:                              # noqa: BLE001
            pass

        # CRITICAL: the M7 loop is  pumpSensors() -> pollCommand() -> serviceSma().
        # pumpSensors() BLOCKS in Serial.write() when the host stops draining, so
        # the loop never reaches pollCommand() and commands are silently never
        # read -- no error, no reply, the drive simply does not happen. A
        # continuous drain keeps the loop turning so commands get serviced.
        self._stop = False
        self._rx = []
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()
        print(f"      draining {self.port} to unblock the M7 loop...")
        time.sleep(2.5)
        self._rx.clear()

        if self.udp:
            self._start_udp()
        return self

    def _start_udp(self):
        """Hand the sample stream to UDP so serial stays a clean command path.

        Bind the socket BEFORE netcfg: with nothing listening the host answers
        each datagram with an ICMP port-unreachable, which is noise we don't
        want on the link (and on some stacks disturbs the sender).
        """
        import socket
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            self._sock.bind(("", self.udp_port))
            self._sock.settimeout(0.2)
        except Exception as e:                         # noqa: BLE001
            print(f"      !! could not bind UDP :{self.udp_port} ({e}); "
                  f"staying on USB streaming")
            self._sock = None
            return

        replies = self._send(f"netcfg {self.pc_ip} {self.udp_port}", wait_s=0.8)
        net = [l for l in replies if "[NET]" in l]
        if net:
            print(f"      {net[0]}")
            print(f"      sample stream moved to UDP -- serial is now a quiet "
                  f"command channel (this is what stops the M7 wedging)")
        else:
            # netcfg lives inside #if H7_TRANSPORT_UDP; a non-UDP build simply
            # will not know the verb. Say so plainly instead of pretending.
            print(f"      !! no [NET] reply to netcfg -- this firmware is "
                  f"probably NOT the portenta_m7_udp build.\n"
                  f"         Staying on USB streaming; expect the M7 to block "
                  f"and stop serving commands\n"
                  f"         if the host ever falls behind.")

    def _drain(self):
        while not self._stop:
            try:
                n = self._reader.in_waiting
                if n:
                    self._rx.append(self._reader.read(n).decode("utf-8", "replace"))
                else:
                    time.sleep(0.002)
            except Exception:                          # noqa: BLE001
                break

    def _send(self, cmd: str, wait_s: float = 0.5) -> list:
        assert self._reader is not None
        self._rx.clear()
        self._reader.write((cmd + "\n").encode())
        self._reader.flush()
        time.sleep(wait_s)
        txt = "".join(self._rx)
        # Firmware messages are tagged ([SMA]/[STATUS]/[ADC1]) or start with a
        # letter; the sensor TSV rows start with a digit.
        lines = [l.strip() for l in txt.split("\n")
                 if l.strip() and not l.strip()[0].isdigit()]
        print(f"      H7 <- {cmd}")
        return lines

    def expected_current(self, v_drive: float) -> float:
        return v_drive / (self.r_load + R_SHUNT)

    def start(self, v_drive: float, hold_ms: int):
        """wdt 0 -> arm -> drive. Returns the expected steady current."""
        import lib_h7_commands as h7

        # The heat watchdog drops to idle-low after `wdt_ms` of host silence
        # while heating. Left at its 5 s default it would silently end the drive
        # mid-capture and the "steady" record quietly would not be.
        self._send(h7.wdt(0))
        self._send(h7.arm())
        self._send(h7.drive(v_drive, hold_ms))
        i_exp = self.expected_current(v_drive)
        print(f"      driving {v_drive:g} V for {hold_ms} ms into "
              f"{self.r_load:g}+{R_SHUNT:g} Ohm -> I_expected = {i_exp:.4f} A "
              f"(Vsense ~ {i_exp:.4f} V at 1 V/A), P_load = "
              f"{i_exp**2 * self.r_load:.2f} W")

        # Confirm from the firmware itself that the drive is REAL before we
        # capture. 'read' reports the live V_LDO / I straight off the board, so
        # this catches a dropped command, a failed arm, or a wrong load -- all of
        # which otherwise yield a perfectly clean capture of nothing happening.
        for line in self._send(h7_read_cmd(), wait_s=0.6):
            if "V_LDO=" in line:
                print(f"      H7 confirms: {line}")
                v = _num_after(line, "V_LDO=")
                i = _num_after(line, "I=")          # mA
                if v is not None and i is not None:
                    i_a = i / 1000.0
                    if abs(v - v_drive) > 0.15 * max(v_drive, 0.1):
                        print(f"    !! firmware reports V_LDO={v:.3f} V but "
                              f"{v_drive:g} V was commanded -- the drive did not "
                              f"take.")
                    elif abs(i_a - i_exp) > 0.25 * i_exp:
                        print(f"    !! firmware reports I={i_a:.4f} A vs "
                              f"{i_exp:.4f} A expected -- check the load really "
                              f"is {self.r_load:g} Ohm.")
                    else:
                        print(f"      drive verified: V_LDO={v:.3f} V, "
                              f"I={i_a:.4f} A -- matches the 4.9 Ohm prediction")
                break
        else:
            print("    !! no 'read' reply from the H7 -- cannot confirm the drive "
                  "is live. Do NOT trust the capture.")
        return i_exp

    def stop(self):
        import lib_h7_commands as h7
        self._send(h7.stop())
        self._send(h7.disarm())

    def __exit__(self, *exc):
        # Always disarm, even if the capture raised -- leaving the MOSFET closed
        # would keep dumping current into the load with nobody watching.
        try:
            if self._reader is not None:
                self.stop()
        finally:
            self._stop = True
            if self._thread is not None:
                self._thread.join(timeout=1.0)
            if self._sock is not None:
                try: self._sock.close()
                except Exception: pass
            if self._reader is not None:
                self._reader.close()
        return False


_SI = {"N": 1e-9, "U": 1e-6, "M": 1e-3, "K": 1e3, "MEG": 1e6, "G": 1e9}

# Bench-confirmed accepted memory depths (fw 5.4.1.5.2R2). Non-decade values
# like 1.4M / 2M / 14M are silently ignored -- the scope keeps its old depth.
_VALID_MSIZ = {"10K", "100K", "1M", "10M", "100M"}


def _si_to_float(tok: str, base_unit: str) -> float | None:
    """Parse a Siglent-style value token like '10MS', '20MV', '1M', '500US'.

    NOTE the collision: in 'MS'/'MV' the leading M means milli, but a bare
    trailing 'M' (memory depth '1M') means mega. Callers pass base_unit ('S',
    'V', or '' for counts) so we know which reading applies.
    """
    t = tok.strip().upper()
    if base_unit and t.endswith(base_unit):
        t = t[: -len(base_unit)]
    num = ""
    while t and (t[0].isdigit() or t[0] in ".+-"):
        num, t = num + t[0], t[1:]
    if not num:
        return None
    mult = 1.0
    if t:
        if not base_unit and t == "M":        # '1M' points = mega
            mult = 1e6
        else:
            mult = _SI.get(t, 1.0)
    return float(num) * mult


# Legal vertical steps (1-2-5 sequence) on this series.
_VDIV_STEPS = [("500UV", 500e-6), ("1MV", 1e-3), ("2MV", 2e-3), ("5MV", 5e-3),
               ("10MV", 10e-3), ("20MV", 20e-3), ("50MV", 50e-3),
               ("100MV", 100e-3), ("200MV", 200e-3), ("500MV", 500e-3),
               ("1V", 1.0), ("2V", 2.0), ("5V", 5.0), ("10V", 10.0)]


def _pick_vdiv(vpp: float, fit_div: float = 3.0) -> str:
    """Smallest legal V/div that fits a vpp signal within +/-fit_div divisions."""
    for name, v in _VDIV_STEPS:
        if vpp / 2.0 <= fit_div * v:
            return name
    return _VDIV_STEPS[-1][0]


def _parse_bswv(reply: str) -> dict:
    """'C1:BSWV WVTP,SINE,FRQ,1000HZ,...,AMP,1V,...' -> {'WVTP':'SINE', ...}."""
    body = reply.split("BSWV", 1)[-1].strip()
    toks = [t.strip() for t in body.split(",")]
    return {toks[i].upper(): toks[i + 1] for i in range(0, len(toks) - 1, 2)}


def setup_awg(scope: Oscilloscope, freq: float, vpp: float,
              source: str = VLDO_CH, tries: int = 3) -> bool:
    """Configure the built-in generator AND confirm it actually took.

    Oscilloscope.set_awg() fires BSWV and OUTP back-to-back with no readback,
    and this scope drops writes issued that way -- observed leaving the
    generator at its previous 1 kHz with the output OFF while the caller
    believed it was driving 100 kHz. A tone check that silently measures a dead
    generator is worse than no tone check, so verify both.
    """
    ok_bswv = False
    for _ in range(tries):
        scope.write(f"{source}:BSWV WVTP,SINE,FRQ,{freq},AMP,{vpp},OFST,0")
        time.sleep(0.25)
        try:
            got = _parse_bswv(scope.query(f"{source}:BSWV?"))
        except Exception:                              # noqa: BLE001
            scope.resync(); continue
        f_got = _si_to_float(got.get("FRQ", ""), "HZ")
        a_got = _si_to_float(got.get("AMP", ""), "V")
        if (f_got and abs(f_got - freq) <= 0.01 * freq
                and a_got and abs(a_got - vpp) <= 0.05 * vpp):
            ok_bswv = True
            break
    if not ok_bswv:
        print(f"  !! generator did not accept FRQ={freq} AMP={vpp} "
              f"(reads {got.get('FRQ')} / {got.get('AMP')})")

    ok_out = False
    for _ in range(tries):
        scope.write(f"{source}:OUTP ON,LOAD,HZ")
        time.sleep(0.25)
        try:
            reply = scope.query(f"{source}:OUTP?").strip()
        except Exception:                              # noqa: BLE001
            scope.resync(); continue
        if "ON" in reply.split(",")[0].upper():
            ok_out = True
            break
    if not ok_out:
        print(f"  !! generator output stayed OFF (reads {reply!r})")
    else:
        print(f"  generator confirmed: {freq/1e3:.3f} kHz, {vpp} Vpp, output ON")
    return ok_bswv and ok_out


def _matches(got: str, want: str, kind: str) -> bool:
    if kind == "skip":
        return True
    if kind == "str":
        return want.strip().upper() in str(got).strip().upper()
    base = "S" if kind == "time" else "V"
    w = _si_to_float(want, base)
    g = Oscilloscope._extract_num(str(got))
    if w is None or g is None:
        return False
    return abs(g - w) <= 0.02 * abs(w)


def _set_and_verify(scope: Oscilloscope, set_cmd: str, query: str,
                    want: str, kind: str, tries: int = 3) -> str:
    """Apply one setting, then confirm it actually landed. Retry if not."""
    got = ""
    for _ in range(tries):
        scope.write(set_cmd)
        time.sleep(0.20)          # this scope drops writes issued back-to-back
        try:
            got = scope.query(query).strip()
        except Exception as e:                    # noqa: BLE001
            got = f"<{e}>"
            scope.resync()
            continue
        if _matches(got, want, kind):
            return got
    return got


def setup_scope(scope: Oscilloscope, msiz: str = "1M", tdiv: str = "10MS",
                vdiv_c1: str = "20MV", vdiv_c2: str = "20MV",
                coupling: str = "A1M", attn: int = 1) -> dict:
    """Configure acquisition for noise characterisation and report what stuck.

    Defaults target the plan's requirements: Nyquist well above the 200-525 kHz
    suspect band, and enough record length for a meaningful Welch average.

    Coupling defaults to AC (A1M), deliberately. The plan says "DC couple, add
    offset, don't switch to AC" -- but this is an 8-bit scope, and a few mV of
    ripple riding on a ~0.85 V rail at a V/div coarse enough to fit the DC is
    quantisation-limited to near nothing. AC-coupling spends the whole 8 bits on
    the ripple, which is the signal of interest. Take one DC-coupled record
    separately if you want the operating point on the same trace.
    """
    # MSIZ is REJECTED while the acquisition is stopped -- silently, with no
    # error, leaving the old depth in place (bench-confirmed 2026-07-21 on fw
    # 5.4.1.5.2R2). Put the scope in RUN before touching memory depth.
    scope.write("TRMD AUTO")
    time.sleep(0.8)

    # Only decade values are accepted: 10K / 100K / 1M / 10M ... Anything else
    # (1.4M, 2M, 14M) is ignored just as silently and the previous depth stays.
    if msiz.strip().upper() not in _VALID_MSIZ:
        print(f"  !! MSIZ {msiz!r} is not one of {sorted(_VALID_MSIZ)} -- the scope "
              f"would ignore it silently and keep the old depth.")

    # This scope also DROPS back-to-back writes (see the driver's SCPI trap
    # notes): firing the whole config in one burst leaves settings silently
    # unapplied, and a stale readback then looks like success. So: one setting
    # at a time, spaced out, read back, retry.
    plan = [(f"MSIZ {msiz}", "MSIZ?", msiz, "str"),
            (f"TDIV {tdiv}", "TDIV?", tdiv, "time")]
    for ch, vd in ((VLDO_CH, vdiv_c1), (VSENSE_CH, vdiv_c2)):
        # ATTN first: changing attenuation rescales V/div, so setting it after
        # VDIV would silently undo the vertical scale we just chose.
        plan += [(f"{ch}:TRA ON", f"{ch}:TRA?", "ON", "str"),
                 # Bandwidth limit OFF: it defaults ON here, and we are hunting a
                 # 200-525 kHz spur -- a filter in the signal path would attenuate
                 # exactly what we came to measure.
                 # NOTE the asymmetric syntax: the SET is the global verb with the
                 # channel as an argument ("BWL C1,OFF"); the per-channel form
                 # "C1:BWL OFF" is silently ignored. The QUERY is per-channel.
                 (f"BWL {ch},OFF", f"{ch}:BWL?", "OFF", "str"),
                 (f"{ch}:ATTN {attn}", f"{ch}:ATTN?", str(attn), "str"),
                 (f"{ch}:CPL {coupling}", f"{ch}:CPL?", coupling, "str"),
                 (f"{ch}:VDIV {vd}", f"{ch}:VDIV?", vd, "volt"),
                 (f"{ch}:OFST 0", f"{ch}:OFST?", "0", "skip")]

    got, failed = {}, []
    for set_cmd, query, want, kind in plan:
        val = _set_and_verify(scope, set_cmd, query, want, kind)
        got[query] = val
        if kind != "skip" and not _matches(val, want, kind):
            failed.append((set_cmd, val))

    print("  scope now reports:")
    for k, v in got.items():
        print(f"    {k:12s} {v}")
    if failed:
        print("  !! these settings did NOT take (scope dropped or clamped them):")
        for cmd, val in failed:
            print(f"       '{cmd}' -> got {val!r}")
        print("     Re-run --setup, or set them on the front panel and re-check.")

    # The ATTN setting must MATCH the physical probe. It is only a scale factor
    # the scope applies to what it reads -- it cannot detect the real probe. Set
    # 1 with a 10x probe and every voltage reads 10x LOW; set 10 with a BNC cable
    # and everything reads 10x HIGH. Either way frequencies and coherence stay
    # correct while absolute volts (and therefore R = V/I) are silently wrong.
    print(f"  probe attenuation set to {attn}x on both channels -- confirm this "
          f"matches what is PHYSICALLY connected:")
    print(f"     1x = plain BNC cable or a 1x probe   |   "
          f"10x = the 10x probes the plan specifies for the rig")

    sara = scope.get_sample_rate()
    nyq = sara / 2
    if nyq < 2 * 525e3:
        print(f"  !! Nyquist {nyq/1e3:.0f} kHz gives little margin over the "
              f"525 kHz top of the suspect band -- raise MSIZ or use a faster TDIV")
    else:
        print(f"  Nyquist {nyq/1e6:.2f} MHz -- covers the 200-525 kHz band")
    return got


def autorange(scope: Oscilloscope, sources=(VLDO_CH, VSENSE_CH),
              fit_div: float = 3.0, max_steps: int = 9) -> dict:
    """Raise each channel's V/div until the signal stops clipping.

    Clipping is not a cosmetic problem here: a hard-limited waveform is a strong
    nonlinearity that generates harmonics AND drags two channels toward spurious
    correlation -- it can manufacture both the spurs and the coherence this
    campaign is trying to measure. So the vertical must be set from the actual
    signal, not guessed.

    Uses PAVA? PKPK (one query per step) rather than pulling a full record.
    """
    # Range against the ACTUAL captured record, not PAVA? PKPK. The scope
    # computes PKPK on the decimated display waveform, which misses the fast
    # spikes present at full rate -- bench-observed reporting 4.5 div while the
    # real record was hard-clipped at |code|=109. Use a small MSIZ so each
    # iteration transfers quickly, then restore it.
    prev_msiz = scope.query("MSIZ?").strip().split()[-1]
    scope.write("TRMD AUTO")          # MSIZ is ignored while stopped
    time.sleep(0.6)
    _set_and_verify(scope, "MSIZ 10K", "MSIZ?", "10K", "str")

    idx = {}
    for src in sources:
        cur_v = Oscilloscope._extract_num(scope.query(f"{src}:VDIV?")) or 20e-3
        idx[src] = min(range(len(_VDIV_STEPS)),
                       key=lambda i: abs(_VDIV_STEPS[i][1] - cur_v))

    chosen = {}
    for _ in range(max_steps):
        acquire_stopped(scope, settle_s=0.4)
        data = read_both_channels(scope, tuple(sources))
        bumped = False
        for src in sources:
            codes, _pre = data[src]
            pk = int(np.max(np.abs(codes)))
            name, v = _VDIV_STEPS[idx[src]]
            chosen[src] = (name, v, pk)
            if pk >= CLIP_CODES and idx[src] + 1 < len(_VDIV_STEPS):
                idx[src] += 1
                _set_and_verify(scope, f"{src}:VDIV {_VDIV_STEPS[idx[src]][0]}",
                                f"{src}:VDIV?", _VDIV_STEPS[idx[src]][0], "volt")
                bumped = True
        if not bumped:
            break
        scope.write("TRMD AUTO")
        time.sleep(0.4)

    scope.write("TRMD AUTO")
    time.sleep(0.6)
    _set_and_verify(scope, f"MSIZ {prev_msiz}", "MSIZ?", prev_msiz, "str")

    print("  autorange result (from the real record):")
    for src, (name, v, pk) in chosen.items():
        flag = "  << STILL CLIPPING" if pk >= CLIP_CODES else ""
        print(f"    {src}: {name:6s} ({v*1e3:g} mV/div)  peak |code| = {pk}"
              f"{flag}")
    return chosen


def wait_for_stop(scope: Oscilloscope, timeout_s: float = 10.0) -> str:
    """Poll SAST? until the scope reports a stopped acquisition.

    Poll, never sleep-and-hope: a fixed sleep either wastes time or reads a
    still-running record, and a still-running record breaks the single-acquisition
    guarantee that makes coherence meaningful.
    """
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout_s:
        try:
            last = scope.query("SAST?", expect="SAST").strip()
        except Exception as e:               # noqa: BLE001 - report and retry
            last = f"<query failed: {e}>"
            scope.resync()
        # Reply is HEADERED: 'SAST Stop', not bare 'Stop'. startswith() therefore
        # never matches and this loop times out on an already-stopped scope.
        # The other states (Arm / Ready / Trig'd / Auto) don't contain "stop".
        if "stop" in last.lower():
            return last
        time.sleep(0.05)
    raise TimeoutError(f"scope did not reach Stop within {timeout_s}s (SAST={last!r})")


def acquire_stopped(scope: Oscilloscope, settle_s: float | None = None) -> str:
    """Free-run, let a full record fill, then STOP.

    We deliberately do NOT use single-shot here. This is noise characterisation:
    there is no edge to trigger on, and the SDS ignores triggers until its
    pre-trigger buffer has filled (~5x TDIV), which makes armed single-shot both
    unnecessary and a source of random misses. Free-run + STOP always yields a
    complete, fully-populated record.
    """
    tdiv = scope.get_timebase()
    # Full screen is 14 horizontal divisions; wait several screens so the record
    # is filled with post-STOP-independent data, with a sane floor for fast tdiv.
    if settle_s is None:
        settle_s = max(0.5, 5.0 * 14.0 * tdiv)

    scope.write("TRMD AUTO")
    time.sleep(settle_s)
    scope.write("STOP")
    status = wait_for_stop(scope)
    return status


def read_both_channels(scope: Oscilloscope, sources=(VLDO_CH, VSENSE_CH)):
    """Read every channel out of the CURRENTLY STOPPED record.

    Caller must have stopped the scope already. Nothing in here restarts
    acquisition, so all channels come from the same time-aligned acquisition.
    """
    out = {}
    for src in sources:
        codes, pre = scope.capture_waveform(source=src)
        out[src] = (np.asarray(codes, dtype=np.int8), pre)
    return out


def check_capture(data) -> list[str]:
    """Return a list of human-readable warnings about the captured record."""
    warns = []
    lengths = {src: len(c) for src, (c, _) in data.items()}
    if len(set(lengths.values())) != 1:
        warns.append(f"channel lengths differ {lengths} -- records are NOT "
                     f"time-aligned; coherence would be invalid")

    rates = {src: pre.sample_rate for src, (_, pre) in data.items()}
    if len(set(rates.values())) != 1:
        warns.append(f"channels report different sample rates {rates}")

    for src, (codes, _) in data.items():
        peak = int(np.max(np.abs(codes)))
        if peak >= CLIP_CODES:
            warns.append(f"{src} peak |code|={peak} >= {CLIP_CODES} -- signal is "
                         f"at/over the screen edge. CLIPPING FABRICATES SPURS; "
                         f"reduce gain or add vertical offset and recapture")
        if peak < 10:
            warns.append(f"{src} peak |code|={peak} -- only ~{peak/CODES_PER_DIV:.1f} "
                         f"div of the 8-bit range in use; quantisation-limited. "
                         f"Zoom in vertically (AC-couple if DC is eating the range)")

    n = min(lengths.values())
    n_seg = max(1, 2 * n // NPERSEG - 1)
    if n_seg < MIN_SEGMENTS:
        warns.append(f"only {n_seg} Welch segment(s) at nperseg={NPERSEG} "
                     f"(need >={MIN_SEGMENTS}) -- coherence will be biased to 1.0. "
                     f"Increase memory depth (MSIZ) or lower nperseg")
    return warns


def save_capture(data, path: Path, note: str = "") -> Path:
    """Save codes + volts + per-channel scaling to a single .npz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"note": note, "codes_per_div": CODES_PER_DIV}
    for src, (codes, pre) in data.items():
        payload[f"{src}_codes"] = codes
        payload[f"{src}_volts"] = np.asarray(
            codes_to_volts(codes, pre.vdiv, pre.offset, CODES_PER_DIV), dtype=float)
        payload[f"{src}_vdiv"] = pre.vdiv
        payload[f"{src}_offset"] = pre.offset
        payload[f"{src}_fs"] = pre.sample_rate
    np.savez_compressed(path, **payload)
    return path


def one_capture(scope: Oscilloscope, out: Path, tag: str,
                h7: "H7Drive | None" = None, v_drive: float = 0.0,
                hold_ms: int = 3000) -> Path:
    """Capture both channels, optionally inside a steady H7 drive window."""
    i_exp = None
    if h7 is not None:
        i_exp = h7.start(v_drive, hold_ms)
        # Let the LDO soft-start settle before sampling. The TPS7A57's CNR/SS cap
        # gives a ~100 ms settle (see docs/PLAN_phase6_ldo_characterization.md),
        # and sampling through the ramp would put a transient in a record we are
        # about to treat as stationary.
        time.sleep(0.25)

    try:
        status = acquire_stopped(scope)
        data = read_both_channels(scope)
    finally:
        if h7 is not None:
            h7.stop()

    if i_exp is not None:
        vs = np.asarray(codes_to_volts(data[VSENSE_CH][0], data[VSENSE_CH][1].vdiv,
                                       data[VSENSE_CH][1].offset, CODES_PER_DIV), dtype=float)
        i_meas = float(np.mean(vs))          # INA296A A1 = 1 V/A
        # AC coupling removes the DC that this check reads, so a ~0 mean means
        # "not measurable here", NOT "no current". Don't cry wolf.
        if abs(i_meas) < 0.2 * i_exp:
            print(f"      I_measured = {i_meas:+.4f} A -- essentially zero mean, "
                  f"which is EXPECTED on an AC-coupled\n"
                  f"      capture (the DC is filtered out). Current cannot be "
                  f"verified from this record;\n"
                  f"      take a DC-coupled capture to confirm the operating "
                  f"point.")
        else:
            print(f"      I_measured = {i_meas:.4f} A vs expected {i_exp:.4f} A "
                  f"({100*(i_meas/i_exp - 1):+.1f}%)")
            if abs(i_meas / i_exp - 1) > 0.20:
                print(f"    !! measured current is >20% off the resistor "
                      f"prediction. Check: did `arm` take, is the load really "
                      f"{h7.r_load:g} Ohm, and is C2 on the INA296A output?")
    fs = data[VLDO_CH][1].sample_rate
    n = len(data[VLDO_CH][0])

    print(f"  [{tag}] SAST={status}  fs={fs/1e6:.4f} MSa/s  N={n}  "
          f"bin={fs/NPERSEG:.1f} Hz  span={n/fs*1e3:.2f} ms")
    for src, (codes, pre) in data.items():
        print(f"        {src}: vdiv={pre.vdiv:.4g} V/div offset={pre.offset:+.4g} V "
              f"peak|code|={int(np.max(np.abs(codes)))}")

    for w in check_capture(data):
        print(f"    !! {w}")

    path = save_capture(data, out / f"phase2_{tag}.npz", note=tag)
    print(f"    saved {path}")
    return path


def quick_coherence(path: Path):
    """Bench-side answer: coherence in the band that matters, right now."""
    from scipy import signal
    z = np.load(path, allow_pickle=True)
    v, i = z[f"{VLDO_CH}_volts"], z[f"{VSENSE_CH}_volts"]
    fs = float(z[f"{VLDO_CH}_fs"])
    n_seg = max(1, 2 * len(v) // NPERSEG - 1)
    if n_seg < MIN_SEGMENTS:
        print(f"  coherence SUPPRESSED ({n_seg} segments < {MIN_SEGMENTS}); "
              f"a short record reports 1.0 everywhere regardless of the signal")
        return
    f, coh = signal.coherence(v, i, fs, nperseg=NPERSEG, detrend="linear")
    print("  coherence(VLDO, Vsense):")
    for lo, hi in [(1e3, 1e4), (1e4, 1e5), (1e5, 2e5), (2e5, 5.25e5), (5.25e5, fs/2)]:
        if hi > fs / 2:
            continue
        m = (f >= lo) & (f < hi)
        if m.any():
            print(f"    {lo/1e3:7.0f}-{hi/1e3:7.0f} kHz : {coh[m].mean():.3f}")


def verify_tone(scope: Oscilloscope, freq: float, amp_vpp: float):
    """Section 6 of the usage doc: known-tone check of the whole pipeline.

    Uses the scope's own AWG into C1. Validates connection, stop/read gating and
    -- the usual bug -- the raw->volts scale factor in one shot.
    """
    print(f"Driving AWG: {freq/1e3:.3f} kHz, {amp_vpp} Vpp")
    print(f"  NOTE: this needs a cable from the scope's Gen Out to {VLDO_CH}. "
          f"Without it you are measuring an open input, not the pipeline.")

    # The noise setup leaves C1 at 20 mV/div, where a 1 Vpp tone clips ~25x --
    # and a clipped sine reads back with the WRONG amplitude, which is exactly
    # the quantity this check exists to validate. Scale to fit, then restore.
    prev_vdiv = scope.query(f"{VLDO_CH}:VDIV?").strip()
    tone_vdiv = _pick_vdiv(amp_vpp)
    print(f"  temporarily setting {VLDO_CH} to {tone_vdiv} "
          f"(was {prev_vdiv}) so the tone fits on screen")
    _set_and_verify(scope, f"{VLDO_CH}:VDIV {tone_vdiv}", f"{VLDO_CH}:VDIV?",
                    tone_vdiv, "volt")

    if not setup_awg(scope, freq, amp_vpp, source=VLDO_CH):
        print("  !! generator not confirmed -- the numbers below would describe a "
              "dead generator, not the pipeline. Fix this before trusting them.")
    time.sleep(1.0)
    acquire_stopped(scope)
    codes, pre = scope.capture_waveform(source=VLDO_CH)

    peak_code = int(np.max(np.abs(np.asarray(codes, dtype=np.int8))))
    if peak_code >= CLIP_CODES:
        print(f"  !! |code| peaks at {peak_code} -- the tone is CLIPPING, so the "
              f"amplitude below is meaningless. Increase V/div and re-run.")
    elif peak_code < 10:
        print(f"  !! |code| peaks at only {peak_code} -- is the Gen Out cable "
              f"actually connected to {VLDO_CH}?")
    volts = np.asarray(codes_to_volts(codes, pre.vdiv, pre.offset, CODES_PER_DIV), dtype=float)
    fs, n = pre.sample_rate, len(volts)

    w = np.hanning(n)
    spec = np.abs(np.fft.rfft((volts - volts.mean()) * w)) / (n * np.mean(w))
    spec[1:] *= 2
    f = np.fft.rfftfreq(n, 1 / fs)
    k = int(np.argmax(spec[1:]) + 1)
    meas_vpp = spec[k] * 2

    print(f"  peak {f[k]/1e3:.3f} kHz (expected {freq/1e3:.3f}, bin={fs/n:.1f} Hz)")
    print(f"  amplitude {meas_vpp:.4f} Vpp (expected {amp_vpp})  "
          f"ratio={meas_vpp/amp_vpp:.3f}")
    ratio = meas_vpp / amp_vpp
    freq_ok = abs(f[k] - freq) <= max(5 * fs / n, 0.02 * freq)

    if ratio < 0.5 or not freq_ok:
        # A wrong scale factor mis-scales the tone; it does not make the tone
        # vanish or move. Landing at a different frequency with a tiny amplitude
        # means NO SIGNAL is arriving -- do not blame the volts conversion.
        print(f"  !! NO TONE DETECTED on {VLDO_CH}: the peak is at "
              f"{f[k]/1e3:.2f} kHz, not {freq/1e3:.2f} kHz, at {ratio*100:.1f}% "
              f"of the expected amplitude.\n"
              f"     The generator is confirmed ON, so the signal is not reaching "
              f"the channel:\n"
              f"       - connect a BNC cable from the scope's Gen Out to "
              f"{VLDO_CH}\n"
              f"       - if a probe is on {VLDO_CH} for the rig, what you are "
              f"seeing is the rig/ambient pickup, not the generator\n"
              f"     This is NOT the CODES_PER_DIV scale factor -- that would "
              f"mis-scale the tone, not remove it.")
    elif abs(ratio - 1) > 0.05:
        print(f"  !! amplitude off by {(ratio-1)*100:+.1f}% at the CORRECT "
              f"frequency -- this IS the scale factor.\n"
              f"     CODES_PER_DIV=25.0 is an unverified assumption in the driver "
              f"(see its STATUS.md).\n"
              f"     Frequencies and coherence stay trustworthy; absolute volts "
              f"and R = V/I do not.")
    else:
        print(f"  PASS: tone lands at the right frequency and within 5% of the "
              f"right amplitude. Volts scaling is validated.")
    scope.set_awg(output=False)
    # Put the vertical back where the noise capture needs it.
    _set_and_verify(scope, f"{VLDO_CH}:VDIV {prev_vdiv.split()[-1]}",
                    f"{VLDO_CH}:VDIV?", prev_vdiv.split()[-1], "volt")
    print(f"  restored {VLDO_CH} vertical to {prev_vdiv}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out" / "scope")
    ap.add_argument("--alias-test", action="store_true",
                    help="two captures at different timebases (the B-vs-C test)")
    # 10MS/20MS (not microseconds): with MSIZ fixed, SHORT timebases clamp SARA
    # to the scope's max rate, so both captures land at the SAME rate and the
    # alias test becomes vacuous. These two yield ~7.1 and ~3.6 MSa/s at MSIZ=1M.
    ap.add_argument("--tdiv", nargs=2, default=["10MS", "20MS"],
                    help="timebases for the alias test (default 10MS 20MS). Must "
                         "yield DIFFERENT SARA or the test proves nothing.")
    ap.add_argument("--setup", action="store_true",
                    help="configure MSIZ/TDIV/vertical for noise capture first")
    ap.add_argument("--msiz", default="1M", help="memory depth (default 1M)")
    ap.add_argument("--setup-tdiv", default="10MS")
    ap.add_argument("--vdiv", nargs=2, default=["20MV", "20MV"],
                    help="V/div for C1 C2 (default 20MV 20MV, AC-coupled)")
    ap.add_argument("--coupling", default="A1M",
                    help="A1M = AC 1M (default, best use of 8 bits), D1M = DC 1M")
    ap.add_argument("--autorange", action="store_true",
                    help="raise V/div until neither channel clips, before capturing")
    ap.add_argument("--attn", type=int, default=1, choices=[1, 10],
                    help="probe attenuation -- MUST match the physical probe. "
                         "1 for a BNC cable (tone check), 10 for the 10x probes "
                         "the plan specifies on the rig. Default 1.")
    ap.add_argument("--verify-tone", action="store_true",
                    help="known-tone pipeline check via the scope AWG")
    ap.add_argument("--tone-hz", type=float, default=100e3)
    ap.add_argument("--tone-vpp", type=float, default=1.0)
    # --- H7 drive ---
    ap.add_argument("--drive", type=float, metavar="VOLTS",
                    help="hold the H7 at this drive voltage during the capture "
                         "(wdt 0 -> arm -> drive). Omit to capture the board idle, "
                         "which measures the WRONG operating point -- see --help.")
    ap.add_argument("--hold-ms", type=int, default=3000,
                    help="drive duration (default 3000, matching a normal fire). "
                         "The capture needs only ms; do not hold far longer.")
    ap.add_argument("--r-load", type=float, default=R_LOAD_DEFAULT,
                    help=f"series load resistance in Ohm (default {R_LOAD_DEFAULT})")
    ap.add_argument("--h7-port", default=H7_PORT_DEFAULT)
    ap.add_argument("--no-udp", action="store_true",
                    help="keep the sample stream on USB (NOT recommended: the M7 "
                         "then blocks on Serial.write and stops serving commands)")
    ap.add_argument("--pc-ip", default=H7_PC_IP_DEFAULT)
    ap.add_argument("--udp-port", type=int, default=H7_UDP_PORT_DEFAULT)
    args = ap.parse_args(argv)

    if args.drive is not None:
        i = args.drive / (args.r_load + R_SHUNT)
        print(f"load: {args.r_load:g} Ohm + {R_SHUNT:g} Ohm shunt, "
              f"drive {args.drive:g} V -> I = {i:.4f} A, "
              f"P_load = {i**2 * args.r_load:.2f} W")
        if i ** 2 * args.r_load > 5.0:
            print(f"  !! {i**2 * args.r_load:.1f} W in the load resistor -- confirm "
                  f"its wattage rating has margin before running.")
    else:
        print("NOTE: no --drive given; the board will be un-armed and pass ZERO\n"
              "      current. Vsense ~ 0, R = V/I undefined, and load-dependent\n"
              "      supply noise may be absent entirely -> a false clean result.\n"
              "      This is only useful as a deliberate idle-baseline reference.")

    with Oscilloscope() as scope:
        if not scope.auto_connect():
            print("!! could not reach the scope. Check the link-local cable, that "
                  "LAN is on DHCP, and that NO other SCPI client (web page, second "
                  "session) holds port 5025 -- it serves one client at a time.",
                  file=sys.stderr)
            return 1
        print(f"connected: {scope.idn}")

        if args.setup:
            print("\nconfiguring acquisition for noise capture:")
            setup_scope(scope, msiz=args.msiz, tdiv=args.setup_tdiv,
                        vdiv_c1=args.vdiv[0], vdiv_c2=args.vdiv[1],
                        coupling=args.coupling, attn=args.attn)
            print()
            # --setup on its own is a configuration step, not a measurement.
            # Don't fall through into an un-driven capture nobody asked for.
            if not (args.verify_tone or args.alias_test or args.drive is not None):
                print("setup only (no --drive/--alias-test/--verify-tone given); "
                      "nothing captured.")
                return 0

        if args.verify_tone:
            verify_tone(scope, args.tone_hz, args.tone_vpp)
            return 0

        # contextlib.nullcontext keeps the two branches identical whether or not
        # the H7 is being driven (it yields None -> one_capture skips drive).
        h7_ctx = (H7Drive(port=args.h7_port, r_load=args.r_load,
                          udp=not args.no_udp, pc_ip=args.pc_ip,
                          udp_port=args.udp_port)
                  if args.drive is not None else contextlib.nullcontext())

        # ExitStack so a failure to OPEN the H7 is reported cleanly, without
        # swallowing errors raised later from inside the capture body.
        with contextlib.ExitStack() as stack:
            try:
                h7 = stack.enter_context(h7_ctx)
            except RuntimeError as e:
                print(f"!! {e}", file=sys.stderr)
                return 1

            if args.autorange:
                # Range against the DRIVEN signal when we are going to drive --
                # the idle waveform is not what the capture will see.
                if h7 is not None:
                    h7.start(args.drive, max(args.hold_ms, 8000))
                    time.sleep(0.3)
                print("\nautoranging vertical scale:")
                autorange(scope)
                if h7 is not None:
                    h7.stop()
                print()

            kw = dict(h7=h7, v_drive=args.drive or 0.0, hold_ms=args.hold_ms)

            if args.alias_test:
                print("\nCHANGE-FS ALIAS TEST -- peaks that MOVE are aliases, "
                      "peaks that STAY are real.\n")
                paths, rates = [], []
                for tdiv in args.tdiv:
                    scope.write(f"TDIV {tdiv}")
                    time.sleep(0.3)
                    paths.append(one_capture(scope, args.out, f"alias_{tdiv}", **kw))
                    rates.append(float(np.load(paths[-1])[f"{VLDO_CH}_fs"]))

                # The whole test is "did the peak move when fs changed". If the
                # scope clamped both timebases to the SAME rate (easy to do --
                # SARA saturates at the max rate for short timebases), then
                # nothing can move and every spur reports STAYS. That is a
                # vacuous pass, not evidence the spurs are real.
                if len(set(rates)) < 2:
                    print(f"\n!! BOTH captures ran at {rates[0]/1e6:.4f} MSa/s -- the "
                          f"alias test is VACUOUS.\n"
                          f"   Every spur will report STAYS regardless of the truth. "
                          f"Pick timebases\n"
                          f"   that actually yield different SARA (raise MSIZ and/or "
                          f"use slower TDIV),\n"
                          f"   then re-run.")
                else:
                    print(f"\nachieved rates: "
                          f"{[round(r/1e6, 4) for r in rates]} MSa/s -- ok, they differ")
                for p in paths:
                    print(f"\n{p.name}:")
                    quick_coherence(p)
                print("\nCompare the two PSDs on a FREQUENCY axis "
                      "(analyze_scope_capture.py) to finish decision step 2.")
            else:
                p = one_capture(scope, args.out, "single", **kw)
                print()
                quick_coherence(p)

        scope.write("TRMD AUTO")     # leave the scope running for the operator
    return 0


if __name__ == "__main__":
    sys.exit(main())
