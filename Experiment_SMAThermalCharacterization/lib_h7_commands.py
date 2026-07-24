"""
h7_commands.py — builders for the Firmware_SMASensorHub_PIO command set,
plus the constant-current extensions in Firmware_SMAConstantCurrent_PIO.

Single source of truth for the host → M7 command strings and their argument
ORDER, so the recorder can't silently drift from the firmware dispatcher.
Mirrors `Firmware_SMASensorHub_PIO/src/main.cpp::dispatch()`.

CONSTANT-CURRENT (`cc*`, `tau`, `ccgain`) requires the CC fork
(`Firmware_SMAConstantCurrent_PIO`). That image is a strict SUPERSET of the
sensor hub — every voltage-mode command below works there unchanged — but the
sensor hub does NOT understand the cc* commands and will simply reject them.
Argument UNITS differ between the twins: `cycle`/`fire`/`drive` take VOLTS,
`cccycle`/`ccfire`/`cc` take MILLIAMPS.

Actuation model (post-2026-06 rebuild): the low-side MOSFET is the master
enable — you must `arm()` before `drive`/`fire`/`cycle`, and `disarm()` is
the immediate hard cutoff. `drive`/`fire`/`cycle` are presets of one
heat/cool engine; the rest level between/after runs is the idle-low voltage,
NOT MOSFET-off. The heat watchdog (`wdt`) only guards a HEAT phase: host
silent > wdt_ms while heating → drop to idle-low (still armed, relaunchable).

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations


def arm() -> str:
    """Close the MOSFET return path (enable coil current)."""
    return "arm"


def disarm() -> str:
    """Open the return path immediately — hard cutoff (DAC→idle, TRIG low)."""
    return "disarm"


def ping() -> str:
    """Heartbeat — resets the heat watchdog. Send while a cycle runs."""
    return "ping"


def stop() -> str:
    """Graceful stop of a running actuation → idle-low (still armed)."""
    return "stop"


def abort() -> str:
    """Immediate disarm (MOSFET off, DAC idle) from any state."""
    return "abort"


def wdt(ms: int) -> str:
    """Set the heat-watchdog timeout in ms (0 = disabled)."""
    return f"wdt {int(ms)}"


def idle(v_idle: float) -> str:
    """Set the idle / cool / rest voltage level."""
    return f"idle {float(v_idle):g}"


def cycle(v_high: float, v_idle: float,
          t_high_ms: int, t_idle_ms: int, n: int) -> str:
    """
    Autonomous heat/cool actuation: repeat[ HEAT v_high for t_high_ms,
    COOL v_idle for t_idle_ms ] n times (n=0 = continuous until `stop`).
    Firmware order: cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>.
    Requires a prior `arm()`.
    """
    return (f"cycle {float(v_high):g} {float(v_idle):g} "
            f"{int(t_high_ms)} {int(t_idle_ms)} {int(n)}")


def fire(v_high: float, t_high_ms: int = 500) -> str:
    """Single heat (n=1) with a clean scope-trigger edge. Requires `arm()`."""
    return f"fire {float(v_high):g} {int(t_high_ms)}"


def drive(v: float, ms: int) -> str:
    """Single heat at v for ms, then return to idle. Requires `arm()`."""
    return f"drive {float(v):g} {int(ms)}"


# ── Constant-current mode (Firmware_SMAConstantCurrent_PIO ONLY) ──────────
# Current-mode twins of drive/fire/cycle. All take MILLIAMPS, not volts.
# Mirrors that fork's `dispatch()`; there is NO `ccdrive` — `cc <mA> [ms]` is
# the drive twin, and it RETARGETS in place if a run is already up.

def cc(ma: float, ms: int = 5000) -> str:
    """Hold `ma` milliamps for `ms`, or RETARGET a live CC run in place.

    Retarget keeps the adaptive state (R_est is a property of the wire, not of
    the setpoint), which is what makes a step response capturable in one run:
    cc(200) -> cc(800) -> cc(200). Unlike the other cc* builders this does not
    require a prior `arm()` at the dispatcher level, but current only flows
    once armed.
    """
    return f"cc {float(ma):g} {int(ms)}"


def cc_status() -> str:
    """Bare `cc` — print controller state (R_est, tau, Kp, achieved rate)."""
    return "cc"


def ccfire(ma: float, ms: int = 500) -> str:
    """Single current pulse (n=1) with a clean scope-trigger edge.

    Requires `arm()`. The cool phase is opened (i_low = 0).
    """
    return f"ccfire {float(ma):g} {int(ms)}"


def cccycle(i_high_ma: float, i_low_ma: float,
            t_high_ms: int, t_idle_ms: int, n: int) -> str:
    """Autonomous constant-current heat/cool cycle — the twin of `cycle()`.

    Firmware order: cccycle <i_high_mA> <i_low_mA> <t_high_ms> <t_idle_ms> <n>.
    `i_low_ma = 0` opens the loop for the cool phase and parks at the idle
    VOLTAGE (V_IDLE) rather than regulating a low current. n=0 = continuous
    until `stop`. Requires a prior `arm()`.
    """
    return (f"cccycle {float(i_high_ma):g} {float(i_low_ma):g} "
            f"{int(t_high_ms)} {int(t_idle_ms)} {int(n)}")


def tau(ms: float) -> str:
    """Closed-loop time constant [ms]. Firmware default 7 ms; range 3-1000 ms
    (the floor is 3 control periods at the 1 kHz loop rate). Safe mid-run;
    NOT persisted across a power cycle."""
    return f"tau {float(ms):g}"


def ccgain(kp: float) -> str:
    """Proportional term. Firmware default 0 = pure integral."""
    return f"ccgain {float(kp):g}"
