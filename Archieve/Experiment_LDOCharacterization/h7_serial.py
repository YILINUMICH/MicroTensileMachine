"""
h7_serial.py — thin pyserial wrapper for the Firmware_SMADriver_PIO firmware.

Sends line commands (`mosfet on`, `fire <code> <ms> <from>`, `read`, ...) and
collects reply lines. Phase-oblivious; the orchestrator drives the sequencing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

import serial  # pyserial


@dataclass
class H7Config:
    port: str = "COM8"          # COM8 = Portenta H7 (see root README)
    baud: int = 115200
    timeout_s: float = 2.0
    ack_timeout_s: float = 5.0


class H7Serial:
    """Line-oriented serial link to the Portenta H7 SMA driver."""

    def __init__(self, cfg: H7Config):
        self.cfg = cfg
        self.ser: Optional[serial.Serial] = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self.ser = serial.Serial(self.cfg.port, self.cfg.baud,
                                 timeout=self.cfg.timeout_s)
        time.sleep(2.0)            # board reset on port open; let it boot
        self.flush_input()
        # The first command after a fresh enumeration is sometimes dropped (the
        # board is still finishing USB-CDC bring-up), which shows up downstream
        # as a marker timeout on the very first set_mosfet/fire. A cheap `info`
        # round-trip here both proves the link and absorbs that dropped-first.
        if not self.ping():
            time.sleep(1.0)
            self.flush_input()
            self.ping()            # one more attempt; real failures surface later

    def ping(self, timeout_s: float = 2.0) -> bool:
        """Best-effort 'is the firmware talking?' check. Sends `info` and waits
        briefly for any reply line. Returns True if the board answered."""
        if not self.ser:
            return False
        try:
            self.flush_input()
            self.send("info")
            t0 = time.time()
            while time.time() - t0 < timeout_s:
                if self.ser.readline():
                    return True
        except Exception:
            pass
        return False

    def hard_reset(self, settle_s: float = 2.5) -> bool:
        """Best-effort host-side reset by pulsing the serial control lines.

        WARNING: confirmed INEFFECTIVE on this Portenta H7 (2026-06-16) — unlike
        a classic Arduino, the H7's USB-CDC auto-reset is not wired to DTR/RTS,
        so a wedged board does NOT recover from this. It's kept as a harmless
        first try (works on boards that do honor DTR/RTS). If it returns False,
        a truly hung H7 needs the physical RST button or a power-cycle; there is
        no reliable host-only recovery. For a clean restart while the firmware is
        still RESPONSIVE, use soft_reset() (the `reset` command) instead.
        Returns True only if the board answers after the pulse.
        """
        if not self.ser:
            return False
        try:
            self.ser.setDTR(False); self.ser.setRTS(False)
            time.sleep(0.1)
            self.ser.setDTR(True); self.ser.setRTS(True)
        except Exception:
            pass
        time.sleep(settle_s)       # let it re-enumerate / boot
        self.flush_input()
        return self.ping()

    def soft_reset(self) -> None:
        """Ask the firmware to reboot itself (`reset` command -> NVIC_SystemReset).
        Clean restart between runs when the firmware is still responsive; for a
        hung board use hard_reset(). The USB-CDC port drops on reboot, so close
        and reopen the link afterwards."""
        try:
            self.send("reset")
        except Exception:
            pass

    def close(self) -> None:
        if self.ser and self.ser.is_open:
            try:
                self.send("mosfet off")   # leave the rig in a safe state
            except Exception:
                pass
            self.ser.close()

    def __enter__(self) -> "H7Serial":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- io ----------------------------------------------------------------
    def flush_input(self) -> None:
        if self.ser:
            self.ser.reset_input_buffer()

    def send(self, cmd: str) -> None:
        """Write a single command line (no reply collection)."""
        assert self.ser is not None, "port not open"
        self.ser.write((cmd.strip() + "\n").encode("ascii"))
        self.ser.flush()

    def read_lines_until(self, marker: str, timeout_s: Optional[float] = None
                         ) -> List[str]:
        """Read reply lines until one contains `marker` (or timeout).

        Returns every line collected, including the marker line. Used to wait
        for the firmware's `[FIRE] done ...` / `[STEP] done ...` sentinels.
        """
        assert self.ser is not None, "port not open"
        t_out = timeout_s if timeout_s is not None else self.cfg.ack_timeout_s
        out: List[str] = []
        t0 = time.time()
        while time.time() - t0 < t_out:
            raw = self.ser.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").rstrip("\r\n")
            if line:
                out.append(line)
                if marker in line:
                    return out
        raise TimeoutError(f"marker {marker!r} not seen in {t_out:.1f}s; got {out!r}")

    # -- high-level commands ----------------------------------------------
    def set_mosfet(self, on: bool) -> List[str]:
        self.flush_input()
        self.send(f"mosfet {'on' if on else 'off'}")
        return self.read_lines_until("MOSFET=")

    def set_code(self, code: int) -> List[str]:
        """Set a steady DAC code (settles); used for the ripple pass."""
        self.flush_input()
        self.send(f"code {int(code)}")
        return self.read_lines_until("V_LDO_meas=")

    def fire(self, code_to: int, hold_ms: int, code_from: int = 0,
             timeout_s: Optional[float] = None) -> List[str]:
        """Scope-triggered step. Returns firmware reply lines incl. V_final.

        Caller must arm the scope single-shot *before* calling this.
        """
        self.flush_input()
        self.send(f"fire {int(code_to)} {int(hold_ms)} {int(code_from)}")
        t_out = timeout_s if timeout_s is not None else (hold_ms / 1000.0 + 4.0)
        return self.read_lines_until("[FIRE] done", timeout_s=t_out)
