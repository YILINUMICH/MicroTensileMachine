#!/usr/bin/env python3
"""lib_m04.py — shared constants and datasheet specs for the ADS131M04 evaluation.

Imported, never run alone. Holds the numbers the sweep commands and the report
judges against, in ONE place so a threshold can never disagree between them.

Datasheet: docs/ads131m04_datasheet.pdf (SBAS890D).
Plan + acceptance criteria: docs/ADS131M04_migration_plan.md §7.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Cross-module import shim ──────────────────────────────────────────────
# The rig has no packaging; modules reach each other by sys.path, the same way
# the recorder reaches Driver_KeysightLCR. We reuse the thermal module's H7
# session rather than writing a second stream parser that could disagree with
# production about what a sample line means.
_REPO = Path(__file__).resolve().parent.parent
_THERMAL = _REPO / "Experiment_SMAThermalCharacterization"
if str(_THERMAL) not in sys.path:
    sys.path.insert(0, str(_THERMAL))

import lib_h7_session as h7s            # noqa: E402  (after the shim, by design)

H7 = h7s.H7
Capture = h7s.Capture
save_capture = h7s.save_capture
parse_status = h7s.parse_status

# ── Paths (resolved off __file__, never off CWD) ──────────────────────────
MODULE = Path(__file__).resolve().parent
DATA = MODULE / "data"
PROFILES = MODULE / "profiles"

# ══════════════════════════════════════════════════════════════════════════
#  Device constants
# ══════════════════════════════════════════════════════════════════════════
NUM_CH = 4
VREF_V = 1.2                     # internal, fixed — no external reference pin
FCLKIN_HZ = 8_192_000.0          # EVM Y1, HR mode
SCLK_MAX_HZ = 25_000_000         # t_c(SC) >= 40 ns at 2.7-3.6 V DVDD

# OSR code -> decimation factor (CLOCK[4:2], §8.6.4 Table 8-17).
# Code 7: the register table says 16256, the data-rate table (8-2) says 16384.
# We use the register table's value and T5 settles it empirically.
OSR_DIV = {0: 128, 1: 256, 2: 512, 3: 1024, 4: 2048, 5: 4096, 6: 8192, 7: 16256}

PWR_HR, PWR_LP, PWR_VLP = 2, 1, 0
CLKIN_FOR_PWR = {PWR_HR: 8_192_000.0, PWR_LP: 4_096_000.0, PWR_VLP: 2_048_000.0}

# Table 7-1 — input-referred noise in uV rms at TA=25C, one row per OSR code,
# columns gain = 1, 2, 4, 8, 16, 32, 64, 128.
NOISE_UV = {
    0: [21.31, 15.26, 13.52, 7.89, 5.21, 3.41, 3.42, 3.42],   # OSR 128,   32 kSPS
    1: [10.68, 9.56, 9.09, 5.42, 3.63, 2.39, 2.39, 2.40],     # OSR 256,   16 kSPS
    2: [7.56, 6.62, 6.37, 3.82, 2.55, 1.69, 1.69, 1.69],      # OSR 512,    8 kSPS
    3: [5.35, 4.68, 4.52, 2.70, 1.82, 1.20, 1.20, 1.20],      # OSR 1024,   4 kSPS
    4: [4.25, 3.91, 3.79, 2.27, 1.52, 1.00, 1.00, 1.00],      # OSR 2048,   2 kSPS
    5: [3.38, 2.99, 2.88, 1.74, 1.17, 0.77, 0.77, 0.77],      # OSR 4096,   1 kSPS
    6: [2.39, 2.13, 2.13, 1.29, 0.86, 0.57, 0.57, 0.57],      # OSR 8192,  500 SPS
    7: [1.90, 1.69, 1.56, 0.95, 0.64, 0.42, 0.42, 0.42],      # OSR 16256, 250 SPS
}

GAIN_CODES = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5, 64: 6, 128: 7}

# ── Input multiplexer — CHn_CFG[1:0] (§8.3.9) ─────────────────────────────
# The internal DC test signal is 2/15 x VREF and AUTO-SCALES with gain, so it
# is always 2/15 of full scale (160 mV at gain 1). No external hardware, an
# exact expected value, and BOTH polarities — which is what lets T8 test sign
# extension rather than only scaling.
MUX_AIN, MUX_SHORTED, MUX_TEST_POS, MUX_TEST_NEG = 0, 1, 2, 3
MUX_NAMES = {0: "ain", 1: "shorted", 2: "test+", 3: "test-"}
TEST_FRAC = 2.0 / 15.0


def expected_volts(mux: int, gain: int) -> float:
    """Expected reading for a mux setting. 0 for real inputs and the short."""
    mag = fsr_v(gain) * TEST_FRAC
    if mux == MUX_TEST_POS:
        return mag
    if mux == MUX_TEST_NEG:
        return -mag
    return 0.0

# ── src IDs as this TEST firmware emits them ──────────────────────────────
# CH0/CH1 keep the production meanings (laser / load) so the M4 swap in Stage 3
# is a no-op for the host. CH2/CH3 borrow src=3/4, which in PRODUCTION mean SMA
# voltage / current — harmless here because this firmware is standalone and its
# captures live in this module, but it is why these captures must never be fed
# to the thermal module's analysis pipeline.
SRC_FOR_CH = {0: 1, 1: 2, 2: 3, 3: 4}
CH_FOR_SRC = {v: k for k, v in SRC_FOR_CH.items()}


def nominal_sps(osr_code: int, pwr: int = PWR_HR) -> float:
    """Output data rate = f_MOD / OSR, with f_MOD = f_CLKIN / 2 (§8.3.6-7)."""
    return (CLKIN_FOR_PWR[pwr] / 2.0) / OSR_DIV[osr_code]


def spec_noise_uv(osr_code: int, gain: int) -> float:
    """Datasheet input-referred noise, uV rms."""
    return NOISE_UV[osr_code][GAIN_CODES[gain]]


def fsr_v(gain: int) -> float:
    """+/-FSR in volts. FSR = 1.2 V / gain (§8.3.3, Table 8-1)."""
    return VREF_V / gain


# ══════════════════════════════════════════════════════════════════════════
#  Acceptance thresholds — plan §7. Changing a number here changes the verdict
#  everywhere; that is the point of it living in one file.
# ══════════════════════════════════════════════════════════════════════════
ACC_NOISE_FACTOR = 2.0       # T7: measured <= 2x Table 7-1
ACC_CH_SPREAD = 2.0          # T7: all four channels within 2x of each other
ACC_RATE_TOL = 0.01          # T5: measured SPS within 1% of nominal
ACC_CRC_ERR = 0              # T4: zero CRC errors
ACC_T4_FRAMES = 1_000_000    # T4: over at least this many frames
# T8: fraction of the expected test-signal amplitude. Deliberately loose at 2%.
# The datasheet calls the internal signal "nominally" 2/15 x VREF and gives it
# no tolerance of its own, so a tight bound would be judging an unspecified
# divider. T8's job per plan §7 is confirming lsbVolts() and SIGN EXTENSION,
# which 2% does decisively; absolute accuracy against the REF7050 is Stage 2.
ACC_DC_TOL = 0.02


@dataclass
class Condition:
    """One sweep cell: a device configuration held for `secs`."""
    label: str
    spi_hz: int = 2_000_000
    osr: int = 6                       # code 6 = OSR 8192 = 500 SPS
    gain: int = 1
    secs: float = 60.0
    pwr: int = PWR_HR
    mux: int = MUX_AIN                 # 0 ain, 1 shorted, 2 test+, 3 test-
    note: str = ""

    @property
    def nominal_sps(self) -> float:
        return nominal_sps(self.osr, self.pwr)

    @property
    def spec_noise_uv(self) -> float:
        return spec_noise_uv(self.osr, self.gain)

    @property
    def expected_v(self) -> float:
        return expected_volts(self.mux, self.gain)

    def commands(self) -> "list[str]":
        """Firmware commands that put the board into this condition."""
        cmds = [f"spi {self.spi_hz}", f"osr {self.osr}"]
        cmds += [f"gain {ch} {self.gain}" for ch in range(NUM_CH)]
        cmds.append(f"mux all {self.mux}")
        return cmds

    def describe(self) -> str:
        return (f"{self.label:<22s} spi={self.spi_hz/1e6:5.2f} MHz  "
                f"osr={OSR_DIV[self.osr]:<5d} ({self.nominal_sps:7.1f} SPS)  "
                f"gain={self.gain:<3d}  mux={MUX_NAMES[self.mux]:<7s} "
                f"{self.secs:5.1f} s"
                + (f"   # {self.note}" if self.note else ""))


@dataclass
class Profile:
    name: str = "adhoc"
    port: str = "COM8"
    transport: str | None = None       # None -> lib_h7_session default (udp)
    pc_ip: str | None = None
    conditions: list = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "Profile":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        conds = [Condition(**c) for c in raw.get("conditions", [])]
        return Profile(
            name=raw.get("name", Path(path).stem),
            port=raw.get("port", "COM8"),
            transport=raw.get("transport"),
            pc_ip=raw.get("pc_ip"),
            conditions=conds,
        )
