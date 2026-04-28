#!/usr/bin/env python3
"""
E4980A/AL LCR Meter Controlcd ule provides optimized control for E4980A/AL LCR meters with
high-speed measurement capabilities and modular design.

Main Features:
- Device discovery and auto-configuration
- Maximum reading rate optimization
- Multiple measurement functions (CPD, CPQ, CSRS, etc.)
- Continuous and burst measurement modes
- Thread-safe operation with callback support
- Robust error handling

Author: Yilin Ma
Date: November 2025
University of Michigan Robotics
HDR Lab
"""

import time
import logging
from typing import Optional, Tuple, Dict, List, Callable
from dataclasses import dataclass
from enum import Enum

try:
    import pyvisa
except ImportError:
    raise ImportError("pyvisa is required. Install with: pip install pyvisa pyvisa-py")


# Library-level logger. Do NOT call logging.basicConfig here - that
# would install a root handler at import time and steamroll any
# configuration the calling application (e.g. the Phase 2 integration
# runner) has already set up. Scripts that run this module standalone
# should configure logging themselves (see test_lcr_meter.py).
logger = logging.getLogger("LCRMeter")


class MeasurementFunction(Enum):
    """Supported measurement functions"""
    CPD = 'CPD'   # Parallel capacitance and dissipation factor
    CPQ = 'CPQ'   # Parallel capacitance and quality factor
    CSRS = 'CSRS' # Series capacitance and resistance
    CSD = 'CSD'   # Series capacitance and dissipation factor
    LSRS = 'LSRS' # Series inductance and resistance
    LSD = 'LSD'   # Series inductance and dissipation factor
    RX = 'RX'     # Resistance and reactance


@dataclass
class MeasurementConfig:
    """Configuration for LCR measurements"""
    frequency: float = 1000.0      # Hz (20 Hz to 2 MHz)
    voltage: float = 1.0           # Volts (0 to 20V)
    function: MeasurementFunction = MeasurementFunction.CPD
    averaging: int = 1             # Number of averages (1 for max speed)
    integration_time: str = 'SHOR' # SHORT, MED, or LONG


@dataclass
class MeasurementResult:
    """Single measurement result"""
    primary: float
    secondary: float
    status: int
    timestamp: float
    reading_number: int
    
    @property
    def is_valid(self) -> bool:
        return self.status == 0
    
    @property
    def status_str(self) -> str:
        if self.status == 0:
            return "OK"
        elif self.status & 0x01:
            return "WARN"
        else:
            return f"ERR:{self.status}"


class LCRMeter:
    """
    Optimized E4980A/AL LCR Meter interface
    
    Features:
    - Maximum reading rate optimization
    - Modular design for integration
    - Robust error handling
    - Configurable measurement parameters
    """
    
    PARAMETER_MAP = {
        MeasurementFunction.CPD: ('Cp', 'D', 'F', ''),
        MeasurementFunction.CPQ: ('Cp', 'Q', 'F', ''),
        MeasurementFunction.CSRS: ('Cs', 'Rs', 'F', 'Ω'),
        MeasurementFunction.CSD: ('Cs', 'D', 'F', ''),
        MeasurementFunction.LSRS: ('Ls', 'Rs', 'H', 'Ω'),
        MeasurementFunction.LSD: ('Ls', 'D', 'H', ''),
        MeasurementFunction.RX: ('R', 'X', 'Ω', 'Ω'),
    }
    
    def __init__(self, resource_string: Optional[str] = None, timeout: int = 5000):
        """
        Initialize LCR meter connection
        
        Args:
            resource_string: VISA resource string (auto-detect if None)
            timeout: Communication timeout in milliseconds
        """
        self.instrument = None
        self.rm = None
        self.config = MeasurementConfig()
        self.timeout = timeout
        self._measurement_count = 0
        self._error_count = 0
        self._start_time = None
        
        if resource_string:
            self.connect(resource_string)
        else:
            self.auto_connect()
    
    def auto_connect(self) -> bool:
        """Automatically find and connect to E4980A/AL"""
        logger.info("Auto-detecting E4980A/AL LCR meter...")
        
        try:
            # Try installed VISA first, then PyVISA-py
            for backend in ['', '@py']:
                try:
                    self.rm = pyvisa.ResourceManager(backend)
                    logger.info(f"Using VISA backend: {backend if backend else 'system'}")
                    break
                except:
                    continue
            
            if not self.rm:
                raise RuntimeError("No VISA backend available")
            
            resources = self.rm.list_resources()
            logger.info(f"Found {len(resources)} instruments")
            
            for resource in resources:
                if self._try_connect(resource):
                    return True
            
            logger.error("No E4980A/AL found")
            return False
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False
    
    def connect(self, resource_string: str) -> bool:
        """Connect to specific resource"""
        try:
            if not self.rm:
                self.rm = pyvisa.ResourceManager()
            return self._try_connect(resource_string)
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False
    
    def _try_connect(self, resource_string: str) -> bool:
        """Try to connect to a specific resource"""
        try:
            logger.debug(f"Trying {resource_string}")
            instrument = self.rm.open_resource(resource_string)
            
            # Configure for optimal performance
            instrument.timeout = self.timeout
            instrument.read_termination = '\n'
            instrument.write_termination = '\n'
            
            # For maximum speed, configure buffers
            if hasattr(instrument, 'chunk_size'):
                instrument.chunk_size = 20480  # Larger buffer
            
            # Clear and identify
            instrument.write('*CLS')
            time.sleep(0.1)
            
            idn = instrument.query('*IDN?').strip()
            if 'E4980A' in idn or 'E4980AL' in idn:
                self.instrument = instrument
                logger.info(f"Connected: {idn}")
                return True
            else:
                instrument.close()
                return False
                
        except Exception as e:
            logger.debug(f"Failed: {e}")
            return False

    # --- Ethernet (LAN) helpers ---
    def connect_ethernet(self, ip: Optional[str] = None, mac: Optional[str] = None, protocol: str = 'vxi11') -> bool:
        """Connect to the meter over Ethernet/LAN.

        Args:
            ip: Device IP address. If None and mac provided, attempts ARP lookup.
            mac: Device MAC address (e.g., '80:09:02:18:62:03' or '80-09-02-18-62-03').
            protocol: 'vxi11' (default), 'hislip', or 'socket' for raw TCP (port 5025).

        Returns:
            True if connected, False otherwise.
        """
        try:
            if not self.rm:
                # Prefer system VISA; fall back to PyVISA-py
                for backend in ['', '@py']:
                    try:
                        self.rm = pyvisa.ResourceManager(backend)
                        logger.info(f"Using VISA backend: {backend if backend else 'system'}")
                        break
                    except Exception:
                        continue
            if not self.rm:
                raise RuntimeError("No VISA backend available")

            if not ip and mac:
                ip = self._find_ip_by_mac(mac)
                if ip:
                    logger.info(f"Resolved MAC to IP: {ip}")
                else:
                    logger.error("Could not resolve IP from MAC. Provide IP directly.")
                    return False

            if not ip:
                logger.error("IP address is required for Ethernet connection")
                return False

            # Build VISA resource string
            if protocol.lower() == 'vxi11':
                resource = f"TCPIP0::{ip}::inst0::INSTR"
            elif protocol.lower() == 'hislip':
                resource = f"TCPIP0::{ip}::hislip0::INSTR"
            elif protocol.lower() == 'socket':
                # Common SCPI socket port is 5025
                resource = f"TCPIP0::{ip}::5025::SOCKET"
            else:
                logger.error(f"Unsupported protocol: {protocol}")
                return False

            logger.info(f"Connecting over LAN using {protocol} to {ip}...")
            return self._try_connect(resource)
        except Exception as e:
            logger.error(f"Ethernet connection failed: {e}")
            return False

    def _find_ip_by_mac(self, mac: str) -> Optional[str]:
        """Attempt to resolve an IP address from a MAC using Windows ARP table.

        Accepts MAC in formats with ':', '-', or '.'. Returns IP string or None."""
        try:
            import subprocess
            # Normalize MAC to Windows ARP display format (hyphen-separated, lowercase)
            norm = mac.replace(':', '-').replace('.', '-').lower()
            # Some inputs may include dots as separators; ensure pairs
            # Windows ARP shows like '80-09-02-18-62-03'
            parts = [p for p in norm.split('-') if p]
            if len(parts) == 6:
                target = '-'.join(parts)
            else:
                target = norm

            proc = subprocess.run(['arp', '-a'], capture_output=True, text=True, timeout=5)
            output = (proc.stdout or '')
            for line in output.splitlines():
                # Typical line:  192.168.0.123   80-09-02-18-62-03   dynamic
                tokens = [t for t in line.split() if t]
                if len(tokens) >= 2:
                    ip_candidate, mac_candidate = tokens[0], tokens[1].lower()
                    if mac_candidate == target:
                        return ip_candidate
        except Exception:
            pass
        return None
    
    def configure(self, config: MeasurementConfig) -> bool:
        """
        Configure measurement parameters optimized for speed
        """
        if not self.instrument:
            raise RuntimeError("Not connected")
        
        self.config = config
        
        try:
            # Reset for clean state
            self.instrument.write('*RST')
            time.sleep(0.2)
            
            # Configure for maximum speed
            commands = [
                f':FREQ {config.frequency}',
                f':VOLT {config.voltage}',
                f':FUNC:IMP {config.function.value}',
                f':APER {config.integration_time}',  # SHORT for max speed
                f':AVER:COUNT {config.averaging}',   # 1 for max speed
                ':AVER OFF' if config.averaging == 1 else ':AVER ON',
                ':TRIG:SOUR INT',  # Internal trigger for continuous
                ':INIT:CONT ON',   # Continuous measurement
                ':DISP:PAGE MEAS', # Measurement display
                ':CALC:MATH:STAT OFF',  # Disable statistics for speed
                ':FORM:DATA ASC',  # ASCII format
            ]
            
            for cmd in commands:
                self.instrument.write(cmd)
                time.sleep(0.01)  # Minimal delay for stability
            
            # Verify configuration
            actual_func = self.instrument.query(':FUNC:IMP?').strip()
            logger.info(f"Configured: {config.frequency}Hz, {config.voltage}V, {actual_func}")
            
            return True
            
        except Exception as e:
            logger.error(f"Configuration failed: {e}")
            return False
    
    def read_single(self) -> Optional[MeasurementResult]:
        """
        Read a single measurement (optimized for speed)
        """
        if not self.instrument:
            raise RuntimeError("Not connected")
        
        try:
            # Use FETCH for maximum speed (doesn't trigger new measurement)
            data = self.instrument.query(':FETCH?')
            
            # Fast parsing
            values = data.strip().split(',')
            if len(values) >= 2:
                self._measurement_count += 1
                return MeasurementResult(
                    primary=float(values[0]),
                    secondary=float(values[1]),
                    status=int(values[2]) if len(values) > 2 else 0,
                    timestamp=time.time(),
                    reading_number=self._measurement_count
                )
        except Exception as e:
            self._error_count += 1
            logger.debug(f"Read error: {e}")
            return None
    
    def read_continuous(self, 
                       callback: Optional[Callable[[MeasurementResult], bool]] = None,
                       max_readings: Optional[int] = None,
                       max_duration: Optional[float] = None) -> List[MeasurementResult]:
        """
        Continuous high-speed reading with callback support
        
        Args:
            callback: Function called with each result. Return False to stop.
            max_readings: Maximum number of readings
            max_duration: Maximum duration in seconds
            
        Returns:
            List of all measurements
        """
        results = []
        self._start_time = time.time()
        self._measurement_count = 0
        self._error_count = 0
        
        logger.info("Starting continuous measurement (Ctrl+C to stop)")
        
        try:
            while True:
                # Check limits
                if max_readings and self._measurement_count >= max_readings:
                    break
                if max_duration and (time.time() - self._start_time) >= max_duration:
                    break
                
                # Read measurement
                result = self.read_single()
                
                if result:
                    results.append(result)
                    
                    # Call callback if provided
                    if callback and not callback(result):
                        break
                
                # Minimal delay for maximum speed
                # Remove this for absolute maximum speed
                # time.sleep(0.001)
                
        except KeyboardInterrupt:
            logger.info("Stopped by user")
        
        self._print_statistics()
        return results
    
    def read_burst(self, count: int) -> List[MeasurementResult]:
        """
        Read a burst of measurements at maximum speed
        """
        return self.read_continuous(max_readings=count)
    
    def _print_statistics(self):
        """Print measurement statistics"""
        if self._start_time:
            duration = time.time() - self._start_time
            rate = self._measurement_count / duration if duration > 0 else 0
            
            logger.info(f"\nStatistics:")
            logger.info(f"  Total readings: {self._measurement_count}")
            logger.info(f"  Duration: {duration:.2f} seconds")
            logger.info(f"  Rate: {rate:.1f} readings/second")
            logger.info(f"  Errors: {self._error_count}")
    
    def get_parameters(self) -> Tuple[str, str, str, str]:
        """Get parameter names and units for current function"""
        return self.PARAMETER_MAP.get(self.config.function, ('P1', 'P2', '', ''))
    
    def format_result(self, result: MeasurementResult) -> str:
        """Format measurement result for display"""
        p1, p2, u1, u2 = self.get_parameters()
        return (f"#{result.reading_number:04d}: "
                f"{p1}={result.primary:12.6e}{u1:2s}  "
                f"{p2}={result.secondary:10.6f}{u2:2s}  "
                f"[{result.status_str}]")
    
    def close(self):
        """Close connection and cleanup"""
        if self.instrument:
            try:
                self.instrument.write('*CLS')
                self.instrument.close()
                logger.info("Connection closed")
            except:
                pass
        
        if self.rm:
            try:
                self.rm.close()
            except:
                pass
    
    def __enter__(self):
        """Context manager support"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager cleanup"""
        self.close()
    
    def __del__(self):
        """Destructor cleanup"""
        self.close()


# Convenience functions for quick measurements
def quick_measure(function: MeasurementFunction = MeasurementFunction.CPD,
                 frequency: float = 1000,
                 voltage: float = 1.0,
                 count: int = 10) -> List[MeasurementResult]:
    """
    Quick measurement function for simple use cases
    
    Example:
        results = quick_measure(MeasurementFunction.CPD, count=100)
    """
    with LCRMeter() as meter:
        config = MeasurementConfig(
            frequency=frequency,
            voltage=voltage,
            function=function,
            averaging=1,  # No averaging for speed
            integration_time='SHOR'  # Short for speed
        )
        meter.configure(config)
        return meter.read_burst(count)


def measure_with_callback(callback: Callable[[MeasurementResult], bool],
                          max_readings: Optional[int] = None,
                          max_duration: Optional[float] = None,
                          **kwargs) -> List[MeasurementResult]:
    """
    Measure with a custom callback function.

    The callback receives each MeasurementResult as it arrives and
    should return True to keep measuring or False to stop. Any extra
    keyword arguments are forwarded to MeasurementConfig (frequency,
    voltage, function, averaging, integration_time).

    Args:
        callback: Function called with each result. Return False to stop.
        max_readings: Maximum number of readings (optional).
        max_duration: Maximum duration in seconds (optional).
        **kwargs: Forwarded to MeasurementConfig.

    Example:
        def print_callback(result):
            print(f"C={result.primary:.3e}F")
            return result.reading_number < 100  # Stop after 100

        results = measure_with_callback(print_callback)
    """
    with LCRMeter() as meter:
        config = MeasurementConfig(**kwargs)
        meter.configure(config)
        return meter.read_continuous(
            callback=callback,
            max_readings=max_readings,
            max_duration=max_duration,
        )
