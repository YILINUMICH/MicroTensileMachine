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

import lib_h7_commands as h7


# Mirrors CC_I_MAX_A in Firmware_SMAConstantCurrent_PIO/src/main.cpp — the
# firmware's hard ceiling on an accepted current target. Duplicated here only
# so a bad config fails at load time with a clear message instead of being
# rejected mid-session by the M7.
CC_I_MAX_MA = 2000.0


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
    # Isolate the serial-reader thread from the camera/GUI so heavy camera work
    # can't starve it (that starvation back-pressures the M7 and distorts cycle
    # timing). Windows-only (best-effort; no-op elsewhere).
    reader_priority: str = "above_normal"  # normal|above_normal|highest ("normal"=off)
    reader_core: int = -1                  # -1=auto (last logical core);
                                           #   >=0 pin to that core; <=-2 = don't pin
    # Sample-stream transport. "usb" = serial (default). "udp" = the src=1..5
    # stream arrives over Ethernet (fire-and-forget) from the portenta_m7_udp
    # firmware, so a busy host can't back-pressure the M7 and distort SMA timing.
    # Commands + [STATUS] stay on the serial `port` either way.
    transport: str = "usb"                 # "usb" | "udp"
    udp_port: int = 7777                   # host bind port for the UDP stream
    pc_ip: str = "169.254.245.100"         # host IP the H7 streams to (netcfg)


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
class CameraConfig:
    """12MP USB3 camera (adaptive-FPS video, fixed resolution). Recording is
    gated by the console's Start/Stop REC; the frame rate adapts to SMA motion
    measured from the laser: fast while moving, a slow heartbeat once settled.

    resolution/fps_fast take effect on (re)connect only (changing them reopens
    the camera). fps_heartbeat / transient_guarantee_s / thresholds are live.
    """
    enabled: bool = False
    use_subprocess: bool = False         # run the camera in its OWN process
                                         #   (separate GIL/core) so camera CPU
                                         #   can never stall the H7 reader/GUI.
                                         #   False = in-thread (default).
    index: int = 0                       # DirectShow device index — POSITIONAL,
                                         #   not pinned to the device (a built-in
                                         #   webcam at 0 pushes the 12MP to 1).
                                         #   auto_detect re-finds it by capability
                                         #   if this index is wrong.
    name_hint: str = "12MP U3 Camera"    # matched against DSHOW device names when
                                         #   pygrabber is installed (optional).
    auto_detect: bool = True             # if the configured index fails/looks
                                         #   wrong, scan indices and pick the
                                         #   highest-resolution sensor (the 12MP;
                                         #   a webcam can't do 4000x3000).
    resolution: List[int] = field(default_factory=lambda: [1920, 1080])
    fps_fast: float = 60.0               # capture rate while the SMA is moving.
                                         #   AVOID 1280x720: no MJPG mode -> YUY2
                                         #   fallback caps at 10 fps. 1080p=85 max.
    fps_heartbeat: float = 1.0           # sparse rate once settled (guard record)
    transient_guarantee_s: float = 10.0  # force fast this long after a heat/idle
    change_threshold_mm: float = 1.0     # net laser move that counts as "moving"
    median_window_ms: float = 200.0      # laser noise/jump rejection window
    stop_dwell_s: float = 4.0            # settled this long with no move -> slow
    jpeg_quality: int = 90
    # Live preview is decoupled from recording: the capture stream runs at
    # resolution/fps_fast (what recording uses); the on-screen preview just
    # samples it. Lower these to spend less CPU on the live view WITHOUT
    # slowing recording. preview_hz sets how many frames/s are decoded for the
    # view (the real CPU lever); preview_width is the downscaled display size.
    preview_hz: float = 10.0             # live-view refresh (was 15)
    preview_width: int = 400             # live-view downscale width px (was 480)
    reconnect_timeout_s: float = 6.0     # no good frame this long -> reopen the
                                         #   camera (watchdog; other streams have
                                         #   one, the camera used to just freeze).
                                         #   MUST stay well above the device's own
                                         #   open+first-frame latency (~4.5 s on
                                         #   the 12MP): a shorter window makes the
                                         #   watchdog fire before the stream ever
                                         #   starts, and each reopen costs another
                                         #   4.5 s -> a self-sustaining reopen loop
                                         #   that never yields a single frame.

    def res_tuple(self) -> tuple:
        w, h = self.resolution
        return (int(w), int(h))


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

    # ── Constant-current mode (Firmware_SMAConstantCurrent_PIO only) ──────
    # mode="voltage" (default) = `cycle`, the behaviour every existing session
    # was recorded with. mode="current" = `cccycle`, which the sensor-hub
    # image does NOT understand — flashing the wrong firmware with
    # mode="current" gets the command rejected, not silently mis-actuated.
    # The current-mode levels are SEPARATE fields rather than a reinterpretation
    # of v_high/v_low, so switching modes can't silently drive 3000 mA because
    # a field meant volts yesterday.
    mode: str = "voltage"        # "voltage" | "current"
    i_high_ma: float = 200.0     # heat current (mA); firmware ceiling 2000 mA
    i_low_ma: float = 0.0        # cool current (mA); 0 = open the loop, park
                                 #   at the idle VOLTAGE (v_low)
    tau_ms: float = 7.0          # closed-loop time constant; None/0 = leave the
                                 #   firmware default (7 ms) untouched
    ccgain: float = 0.0          # proportional term; 0 = pure integral

    def __post_init__(self) -> None:
        m = str(self.mode).strip().lower()
        if m not in ("voltage", "current"):
            raise ValueError(
                f"sma.mode must be 'voltage' or 'current', got {self.mode!r}")
        self.mode = m
        if self.i_high_ma <= 0.0 or self.i_high_ma > CC_I_MAX_MA:
            raise ValueError(
                f"sma.i_high_ma must be in (0, {CC_I_MAX_MA:g}] mA, "
                f"got {self.i_high_ma!r}")
        if self.i_low_ma < 0.0 or self.i_low_ma > CC_I_MAX_MA:
            raise ValueError(
                f"sma.i_low_ma must be in [0, {CC_I_MAX_MA:g}] mA, "
                f"got {self.i_low_ma!r}")

    @property
    def is_current_mode(self) -> bool:
        return self.mode == "current"

    def cycle_command(self) -> str:
        if self.is_current_mode:
            # Firmware: cccycle <i_high_mA> <i_low_mA> <t_high_ms> <t_idle_ms> <n>
            return h7.cccycle(self.i_high_ma, self.i_low_ma,
                              self.fire_ms, self.cool_ms, self.n_cycles)
        # Firmware: cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>.
        # v_low is the idle/cooling level (v_idle); the arg order is unchanged.
        return h7.cycle(self.v_high, self.v_low,
                        self.fire_ms, self.cool_ms, self.n_cycles)

    def tuning_commands(self) -> "list[str]":
        """CC tuning to send once, after arm, before the cycle. Empty in
        voltage mode. `tau_ms` falsy = keep the firmware default."""
        if not self.is_current_mode:
            return []
        cmds = []
        if self.tau_ms:
            cmds.append(h7.tau(self.tau_ms))
        if self.ccgain:
            cmds.append(h7.ccgain(self.ccgain))
        return cmds


@dataclass
class BaselineConfig:
    """Quiescent "measure cold R + zero all sensors" phase, run once before an
    actuation session (see RecordingCore.measure_baseline()).

    It ARMS at a low, NON-heating probe voltage so a small current flows,
    streams V/I/R + laser/load for a short window, captures per-session zero
    references — cold SMA resistance, laser rest voltage, and the load-cell
    tare offset — then AUTO-DISARMS. `auto_on_start` is False by default so the
    rig still powers up DISARMED (the safe state) until the operator asks for
    it. Must run BEFORE recording (it drains the sample queues)."""
    enabled: bool = True
    auto_on_start: bool = False   # run automatically after the startup health check
    probe_v: float = 0.5          # non-heating probe level (V); ~= idle, ~0.12 A
    duration_s: float = 2.0       # averaging window
    settle_s: float = 0.3         # skip the initial transient before averaging
    apply_load_offset: bool = True   # write measured load rest V → load_cell.offset_V
    load_saturation_warn_frac: float = 0.8  # warn if |V_rest| exceeds this of ±5 V


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
    output_dir: str = "data/raw"         # relative to the module dir; RAW captures only


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    lcr: LcrConfig = field(default_factory=LcrConfig)
    h7: H7Config = field(default_factory=H7Config)
    stage: StageConfig = field(default_factory=StageConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    sma: SmaConfig = field(default_factory=SmaConfig)
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
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
            camera=CameraConfig(**(d.get("camera") or {})),
            sma=SmaConfig(**(d.get("sma") or {})),
            baseline=BaselineConfig(**(d.get("baseline") or {})),
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
            "camera": asdict(self.camera),
            "sma": asdict(self.sma),
            "baseline": asdict(self.baseline),
            "phases": asdict(self.phases),
            "calibration": asdict(self.calibration),
            "run": asdict(self.run),
        }
