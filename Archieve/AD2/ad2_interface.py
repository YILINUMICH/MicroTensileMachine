#!/usr/bin/env python3
"""
Digilent Analog Discovery 2 (AD2) Interface Module
===================================================

This module provides a simplified interface to the Digilent Analog Discovery 2
oscilloscope for the micro tensile machine. It exposes two differential analog
input channels (CH1: load cell via LCA amplifier, CH2: Keyence IL-030 laser
displacement sensor) at user-configurable ranges.

Running this file directly acts as a system health check: it opens the device,
prints the WaveForms version / device serial, takes a short burst of readings
at 1 Hz, and prints them in millivolts. If that output looks sensible, the
AD2 is wired correctly and can be used by ad2_continuous_log.py.

Main Features:
- Automatic AD2 device discovery with clear error if not found
- Per-channel input range (defaults to +-5 V on both channels)
- Single-shot voltage readings via FDwfAnalogInStatusSample
- Context manager support for guaranteed device handle cleanup
- Clean separation of library code and standalone health check

Author: Yilin Ma
Date: April 2026
University of Michigan Robotics
HDR Lab
"""

import sys
import time
import ctypes
import logging
from ctypes import byref, c_int, c_uint, c_double, c_byte, create_string_buffer
from dataclasses import dataclass, asdict
from typing import Optional, Tuple


# ------------------------------------------------------------------
# WaveForms SDK constants (reproduced from dwfconstants.py so this
# file has no external Python dependency beyond ctypes + WaveForms
# runtime). See Digilent's WaveForms SDK reference for full listing.
# ------------------------------------------------------------------
_HDWF_NONE = c_int(0)

# enumfilterAll - return all known device types
_ENUMFILTER_ALL = c_int(0)

# FDwfAnalogInStatus states (we only use it to drive the single-sample API)
_DWF_STATE_READY = c_byte(0)
_DWF_STATE_DONE = c_byte(2)

# acqmodeSingle - single buffered acquisition
_ACQMODE_SINGLE = c_int(3)

# Analog input channel node type: 0 = standard (voltage) input
_ANALOG_IN_NODE_INPUT = c_int(0)


def _load_dwf_library():
    """
    Load the WaveForms dynamic library for the current platform.

    Returns:
        The loaded ctypes CDLL object.

    Raises:
        OSError: If the WaveForms runtime is not installed / the
                 shared library can't be located.
    """
    if sys.platform.startswith("win"):
        # On Windows the WaveForms installer puts dwf.dll on PATH
        return ctypes.cdll.dwf
    elif sys.platform.startswith("darwin"):
        return ctypes.cdll.LoadLibrary("/Library/Frameworks/dwf.framework/dwf")
    else:
        return ctypes.cdll.LoadLibrary("libdwf.so")


try:
    _dwf = _load_dwf_library()
except OSError as e:
    print(f"Error loading WaveForms SDK library: {e}")
    print("Install WaveForms (https://digilent.com/shop/software/digilent-waveforms/)")
    print("to get dwf.dll / libdwf.so on the system.")
    raise


@dataclass
class AD2DeviceInfo:
    """Information about a discovered AD2 device."""
    device_index: int
    serial_number: str
    device_name: str
    waveforms_version: str

    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class AD2Scope:
    """
    Digilent Analog Discovery 2 two-channel voltage reader.

    Usage:
        with AD2Scope() as scope:
            v_ch1, v_ch2 = scope.read_single()

    This class is intentionally narrow: it exposes only what the
    micro tensile machine needs (two differential voltage channels,
    one-shot reads, configurable range). If buffered / streaming
    acquisition is needed later, add a separate method rather than
    changing the read_single() contract.
    """

    # Default input range (volts, peak-to-peak is 2x this value since
    # AD2 channels are differential)
    DEFAULT_RANGE_V = 5.0

    def __init__(self,
                 device_index: int = -1,
                 ch1_range_v: float = DEFAULT_RANGE_V,
                 ch2_range_v: float = DEFAULT_RANGE_V,
                 settle_time_s: float = 0.1):
        """
        Initialize AD2 controller (does not open the device yet).

        Args:
            device_index: Index into FDwfEnum list, or -1 for "first available".
            ch1_range_v: Full-scale input range for CH1 in volts (default +-5 V).
            ch2_range_v: Full-scale input range for CH2 in volts (default +-5 V).
            settle_time_s: Delay after configure() before first read,
                           to let the analog front-end settle.
        """
        self.device_index = device_index
        self.ch1_range_v = ch1_range_v
        self.ch2_range_v = ch2_range_v
        self.settle_time_s = settle_time_s

        self._hdwf = c_int(0)       # WaveForms device handle
        self._connected = False
        self.device_info: Optional[AD2DeviceInfo] = None

        self.logger = logging.getLogger("AD2Scope")

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def open(self) -> bool:
        """
        Discover and open the AD2 device, then configure both channels.

        Returns:
            True on success, False otherwise.
        """
        try:
            # Count connected devices
            c_device_count = c_int(0)
            _dwf.FDwfEnum(_ENUMFILTER_ALL, byref(c_device_count))

            if c_device_count.value == 0:
                self.logger.error("No AD2 / WaveForms device detected. "
                                  "Check USB cable and that WaveForms runtime is installed.")
                return False

            self.logger.info(f"Found {c_device_count.value} WaveForms device(s)")

            # Open the requested device (-1 => first available)
            dev_idx = self.device_index if self.device_index >= 0 else 0
            if dev_idx >= c_device_count.value:
                self.logger.error(f"device_index {dev_idx} out of range "
                                  f"(only {c_device_count.value} devices)")
                return False

            _dwf.FDwfDeviceOpen(c_int(dev_idx), byref(self._hdwf))

            if self._hdwf.value == _HDWF_NONE.value:
                err = self._last_error_message()
                self.logger.error(f"FDwfDeviceOpen failed: {err}")
                return False

            self._connected = True
            self.device_info = self._get_device_info(dev_idx)
            self.logger.info(
                f"Connected: {self.device_info.device_name} "
                f"SN={self.device_info.serial_number} "
                f"(WaveForms {self.device_info.waveforms_version})"
            )

            # Configure analog-in channels
            self._configure_channels()
            return True

        except Exception as e:
            self.logger.error(f"AD2 open failed: {e}")
            self._connected = False
            return False

    def close(self):
        """Close the AD2 handle. Idempotent."""
        if self._hdwf.value != _HDWF_NONE.value:
            try:
                _dwf.FDwfAnalogInConfigure(self._hdwf, c_int(0), c_int(0))
            except Exception:
                pass
            try:
                _dwf.FDwfDeviceClose(self._hdwf)
                self.logger.info("AD2 connection closed")
            except Exception:
                pass
            self._hdwf = c_int(0)
        self._connected = False

    def __enter__(self):
        """Context manager: open on entry, raise if open() fails."""
        if not self.open():
            raise RuntimeError("AD2Scope.open() failed - see log for details")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager: always close device handle on exit."""
        self.close()

    def __del__(self):
        """Safety net in case caller forgot to close()."""
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def read_single(self) -> Tuple[float, float]:
        """
        Read one instantaneous sample from each channel.

        Returns:
            Tuple (v_ch1, v_ch2) in volts.

        Raises:
            RuntimeError: If the device is not open.
        """
        if not self._connected:
            raise RuntimeError("AD2Scope not connected - call open() first")

        # FDwfAnalogInStatus refreshes the internal register. Passing
        # fReadData=0 is enough to update the "last sample" registers.
        sts = c_byte(0)
        _dwf.FDwfAnalogInStatus(self._hdwf, c_int(0), byref(sts))

        v1 = c_double(0.0)
        v2 = c_double(0.0)
        _dwf.FDwfAnalogInStatusSample(self._hdwf, c_int(0), byref(v1))
        _dwf.FDwfAnalogInStatusSample(self._hdwf, c_int(1), byref(v2))

        return (v1.value, v2.value)

    def read_burst(self, count: int, interval_s: float = 0.01) -> list:
        """
        Read `count` samples spaced by `interval_s` seconds.

        This is a convenience wrapper around read_single() used by the
        health check; ad2_continuous_log.py has its own, timing-aware
        loop that should be preferred for long acquisitions.

        Returns:
            List of (v_ch1, v_ch2) tuples.
        """
        out = []
        for _ in range(count):
            out.append(self.read_single())
            time.sleep(interval_s)
        return out

    def get_device_info(self) -> Optional[AD2DeviceInfo]:
        """Return info captured at open() time (or None if not connected)."""
        return self.device_info

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _configure_channels(self):
        """Enable CH1/CH2 and apply the configured ranges."""
        # Acquisition mode: single-shot (we use FDwfAnalogInStatusSample so
        # this is mostly a formality).
        _dwf.FDwfAnalogInAcquisitionModeSet(self._hdwf, _ACQMODE_SINGLE)

        # Enable CH1 (index 0) and CH2 (index 1)
        _dwf.FDwfAnalogInChannelEnableSet(self._hdwf, c_int(0), c_int(1))
        _dwf.FDwfAnalogInChannelEnableSet(self._hdwf, c_int(1), c_int(1))

        # Apply requested ranges
        _dwf.FDwfAnalogInChannelRangeSet(self._hdwf, c_int(0), c_double(self.ch1_range_v))
        _dwf.FDwfAnalogInChannelRangeSet(self._hdwf, c_int(1), c_double(self.ch2_range_v))

        # Offset 0 V on both channels
        _dwf.FDwfAnalogInChannelOffsetSet(self._hdwf, c_int(0), c_double(0.0))
        _dwf.FDwfAnalogInChannelOffsetSet(self._hdwf, c_int(1), c_double(0.0))

        # Apply and arm. fReconfigure=1, fStart=1
        _dwf.FDwfAnalogInConfigure(self._hdwf, c_int(1), c_int(1))

        # Let the front-end settle
        time.sleep(self.settle_time_s)

        self.logger.info(
            f"Channels configured: CH1 range +-{self.ch1_range_v:g} V, "
            f"CH2 range +-{self.ch2_range_v:g} V"
        )

    def _get_device_info(self, device_index: int) -> AD2DeviceInfo:
        """Collect serial number, device name, and WaveForms version."""
        # Device name (e.g. "Analog Discovery 2")
        name_buf = create_string_buffer(32)
        _dwf.FDwfEnumDeviceName(c_int(device_index), name_buf)

        # Serial number
        sn_buf = create_string_buffer(32)
        _dwf.FDwfEnumSN(c_int(device_index), sn_buf)

        # WaveForms runtime version
        ver_buf = create_string_buffer(32)
        _dwf.FDwfGetVersion(ver_buf)

        return AD2DeviceInfo(
            device_index=device_index,
            serial_number=sn_buf.value.decode(errors="replace"),
            device_name=name_buf.value.decode(errors="replace"),
            waveforms_version=ver_buf.value.decode(errors="replace"),
        )

    def _last_error_message(self) -> str:
        """Fetch the last error string from the WaveForms runtime."""
        err_buf = create_string_buffer(512)
        try:
            _dwf.FDwfGetLastErrorMsg(err_buf)
            return err_buf.value.decode(errors="replace").strip()
        except Exception:
            return "<unknown error>"


# ----------------------------------------------------------------------
# Convenience factory
# ----------------------------------------------------------------------
def create_scope(**kwargs) -> AD2Scope:
    """
    Create and open an AD2Scope in one call.

    Caller is responsible for closing it (or using it inside a `with` block
    via AD2Scope(...) directly - this helper is for scripts that want an
    already-open object returned).
    """
    scope = AD2Scope(**kwargs)
    if not scope.open():
        raise RuntimeError("Failed to open AD2 device")
    return scope


# ----------------------------------------------------------------------
# Standalone health check
# ----------------------------------------------------------------------
def _health_check(num_samples: int = 10, interval_s: float = 1.0) -> int:
    """
    Run the standalone health check. Returns process exit code.

    Prints WaveForms version + AD2 serial, takes `num_samples` readings
    at roughly `interval_s` second spacing, and prints them in mV.
    """
    print("=" * 60)
    print("AD2 Interface - Health Check")
    print("=" * 60)

    with AD2Scope() as scope:
        info = scope.get_device_info()
        print()
        print(f"Device:           {info.device_name}")
        print(f"Serial number:    {info.serial_number}")
        print(f"WaveForms:        {info.waveforms_version}")
        print(f"CH1 range:        +-{scope.ch1_range_v:g} V  (load cell / LCA-9PC/RTC)")
        print(f"CH2 range:        +-{scope.ch2_range_v:g} V  (Keyence IL-030 laser)")
        print()
        print(f"Taking {num_samples} readings at {1.0/interval_s:g} Hz...")
        print("-" * 60)
        print(f"{'#':>3}  {'CH1 (mV)':>12}  {'CH2 (mV)':>12}")
        print("-" * 60)

        for i in range(num_samples):
            t0 = time.perf_counter()
            v1, v2 = scope.read_single()
            # mV for readability
            print(f"{i+1:>3}  {v1*1000:>12.3f}  {v2*1000:>12.3f}")
            # Sleep remainder of interval (corrects for read latency)
            elapsed = time.perf_counter() - t0
            remaining = interval_s - elapsed
            if remaining > 0:
                time.sleep(remaining)

        print("-" * 60)
        print("Health check complete.")
        print()
        print("If both columns look reasonable (load cell near 0 mV with no")
        print("force applied; laser near its analog center with target in")
        print("range), the AD2 wiring is correct and you can run")
        print("ad2_continuous_log.py for 100 Hz data capture.")
    return 0


if __name__ == "__main__":
    # Only set up logging when run standalone - library consumers should
    # configure their own handlers (match ZaberStage style).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    sys.exit(_health_check())
