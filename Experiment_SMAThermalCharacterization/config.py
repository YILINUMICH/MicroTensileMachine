"""
config.py — typed config loader for the SMA characterization recorder (V3).

Splits the YAML config into typed dataclasses so the session controller and
workers can take exactly the slice they need. V3 adds a `stage` section
(Zaber control), multi-channel `h7` selection, and a `calibration` block.

Design rule (V3): the recorder configures instruments + logs RAW streams.
The `calibration` block is metadata for the OFFLINE analyzer only — the
recorder never converts units or pushes calibration to firmware.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import yaml

import h7_commands as h7


# ---------------------------------------------------------------------------
# Instrument sub-configs
# ---------------------------------------------------------------------------
@dataclass
class LcrConfig:
    # LCR is removed from the thermal module → default OFF. Retained only so
    # legacy configs / the shared dataclass schema still load; the console
    # never constructs an LCR worker regardless of this flag.
    enabled: bool = False
    resource: Optional[str] = None       # None → auto-detect via VISA IDN
    function: str = "LSRS"               # series L + series R
    frequency_hz: float = 1.0e6
    voltage_V: float = 0.5
    integration: str = "SHORT"           # SHORT | MED | LONG
    averaging: int = 1
    poll_interval_s: float = 0.010       # ~100 Hz host-side poll cadence


@dataclass
class H7Config:
    """
    Portenta H7 over USB-CDC. The combined firmware
    (Firmware_SMASensorHub_PIO) streams src=1 laser, 2 load, 3 SMA V,
    4 SMA I, 5 SMA R on one port. `channels` selects which to keep;
    `startup_commands` is an inert hook for future scripted actuation.
    """
    enabled: bool = True
    port: str = "COM8"
    baud: int = 115200
    channels: List[str] = field(
        default_factory=lambda: ["laser", "load", "sma_v", "sma_i", "sma_r"])
    startup_commands: List[str] = field(default_factory=list)


@dataclass
class StageConfig:
    """Zaber X-LSQ300A-E01 linear stage."""
    enabled: bool = True
    port: Optional[str] = "COM5"         # None → auto-detect
    position_limits_mm: List[float] = field(default_factory=lambda: [5.0, 40.0])
    max_velocity_mm_s: float = 10.0
    reading_rate_hz: int = 100
    home_on_start: bool = False
    move_to_zero_on_start: bool = False
    zero_mm: float = 10.0
    poll_interval_s: float = 0.02        # ~50 Hz position poll

    def limits_tuple(self) -> tuple:
        lo, hi = self.position_limits_mm
        return (float(lo), float(hi))


@dataclass
class SmaConfig:
    """
    Parameters for the ON-M7 cyclic actuation state machine
    (Firmware_SMASensorHub_PIO `cycle` command). The recorder is NOT in the
    timing loop — it only sends these params + a 1 Hz heartbeat (`ping`);
    M7 owns all phase timing deterministically.

    `enabled=false` keeps the recorder a pure logger (operator drives the
    SMA manually). `enabled=true` makes the recorder send `cycle …` at the
    start of the RAW phase, `ping` each second, and `stop` at the end.
    """
    enabled: bool = False
    v_high: float = 3.0          # heating / actuation voltage
    v_low: float = 0.5           # idle / cooling level = v_idle (the LDO can't
                                 #   reach 0 V; firmware rests here between heats)
    fire_ms: int = 2000          # heat (t_high) duration per cycle
    cool_ms: int = 8000          # cool (t_idle) duration per cycle
    n_cycles: int = 10           # 0 = continuous until RAW stop
    wdt_ms: int = 5000           # M7 heat-watchdog timeout (0 = watchdog off)

    def cycle_command(self) -> str:
        # Firmware: cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>.
        # v_low is the idle/cooling level (v_idle); the arg order is unchanged.
        return h7.cycle(self.v_high, self.v_low,
                        self.fire_ms, self.cool_ms, self.n_cycles)


@dataclass
class PhasesConfig:
    open_duration_s: float = 20.0
    short_duration_s: float = 20.0
    # RAW (experiment) phase has no fixed duration — runs until Ctrl+C.


# ---------------------------------------------------------------------------
# Calibration (analysis-only — never applied by the recorder)
# ---------------------------------------------------------------------------
@dataclass
class LaserCal:
    k_mV_per_um: Optional[float] = None  # V = k·µm + V0
    V0_mV: Optional[float] = None

    def ready(self) -> bool:
        return self.k_mV_per_um is not None and self.V0_mV is not None


@dataclass
class LoadCellCal:
    scale_N_per_V: Optional[float] = None  # F[N] = scale·(V − offset)
    offset_V: float = 0.0

    def ready(self) -> bool:
        return self.scale_N_per_V is not None


@dataclass
class CurrentSenseCal:
    gain_V_per_V: float = 10.0
    shunt_ohm: float = 0.1
    ioffset_V: float = 0.0


@dataclass
class CalibrationConfig:
    laser: LaserCal = field(default_factory=LaserCal)
    load_cell: LoadCellCal = field(default_factory=LoadCellCal)
    current_sense: CurrentSenseCal = field(default_factory=CurrentSenseCal)


@dataclass
class RunConfig:
    operator: str = ""
    notes: str = ""
    output_dir: str = "data"             # relative to sma_recorder.py


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    lcr: LcrConfig = field(default_factory=LcrConfig)
    h7: H7Config = field(default_factory=H7Config)
    stage: StageConfig = field(default_factory=StageConfig)
    sma: SmaConfig = field(default_factory=SmaConfig)
    phases: PhasesConfig = field(default_factory=PhasesConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    run: RunConfig = field(default_factory=RunConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> "AppConfig":
        with open(path) as f:
            d: dict[str, Any] = yaml.safe_load(f) or {}
        cal = d.get("calibration") or {}
        return cls(
            lcr=LcrConfig(**(d.get("lcr") or {})),
            h7=H7Config(**(d.get("h7") or {})),
            stage=StageConfig(**(d.get("stage") or {})),
            sma=SmaConfig(**(d.get("sma") or {})),
            phases=PhasesConfig(**(d.get("phases") or {})),
            calibration=CalibrationConfig(
                laser=LaserCal(**(cal.get("laser") or {})),
                load_cell=LoadCellCal(**(cal.get("load_cell") or {})),
                current_sense=CurrentSenseCal(**(cal.get("current_sense") or {})),
            ),
            run=RunConfig(**(d.get("run") or {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lcr": asdict(self.lcr),
            "h7": asdict(self.h7),
            "stage": asdict(self.stage),
            "sma": asdict(self.sma),
            "phases": asdict(self.phases),
            "calibration": asdict(self.calibration),
            "run": asdict(self.run),
        }
