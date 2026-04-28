"""
config.py — typed config loader for the SMA characterization recorder.

Splits the YAML config into four dataclasses (lcr / h7 / phases / run) so the
session controller and workers can take exactly the slice they need without
tying themselves to the full AppConfig.

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------
@dataclass
class LcrConfig:
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
    Configuration for the Portenta-H7-over-USB-CDC ADC stream. Named "h7"
    to be future-proof for the upcoming M4/M7 dual-core firmware that will
    expose both ADCs of the ADS1263 through this same serial path.
    """
    port: str = "COM8"
    baud: int = 115200
    adc_source: int = 1                  # ADC1 or ADC2 (firmware-dependent)


@dataclass
class PhasesConfig:
    open_duration_s: float = 20.0
    short_duration_s: float = 20.0
    # RAW (experiment) phase has no fixed duration — runs until Ctrl+C.


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
    phases: PhasesConfig = field(default_factory=PhasesConfig)
    run: RunConfig = field(default_factory=RunConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> "AppConfig":
        with open(path) as f:
            d: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(
            lcr=LcrConfig(**(d.get("lcr") or {})),
            h7=H7Config(**(d.get("h7") or {})),
            phases=PhasesConfig(**(d.get("phases") or {})),
            run=RunConfig(**(d.get("run") or {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lcr": asdict(self.lcr),
            "h7": asdict(self.h7),
            "phases": asdict(self.phases),
            "run": asdict(self.run),
        }
