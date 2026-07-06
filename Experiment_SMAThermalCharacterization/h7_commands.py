"""
h7_commands.py — builders for the Firmware_SMASensorHub_PIO command set.

Single source of truth for the host → M7 command strings and their argument
ORDER, so the recorder can't silently drift from the firmware dispatcher.
Mirrors `Firmware_SMASensorHub_PIO/src/main.cpp::dispatch()`.

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
