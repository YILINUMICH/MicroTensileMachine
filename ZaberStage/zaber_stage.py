#!/usr/bin/env python3
"""
Simplified Zaber Linear Stage Control Module
============================================

This module provides simplified control for Zaber linear stages with
high-speed position reading and velocity control capabilities.

Main Features:
- Device discovery and auto-configuration
- Velocity control within specified range
- High-speed position reading (up to 100 Hz)
- Absolute position and distance measurements
- Thread-safe operation
- Configuration persistence (JSON)

Author: Yilin Ma
Date: November 2025
University of Michigan Robotics
HDR Lab
"""

import time
import logging
import threading
import json
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass, asdict

# Import required Zaber libraries
try:
    from zaber_motion import Library
    from zaber_motion.ascii import Connection, Device, Axis
    from zaber_motion.units import Units
except ImportError as e:
    print(f"Error importing zaber_motion: {e}")
    print("Please install: pip install zaber-motion")
    raise


@dataclass
class DeviceInfo:
    """Information about a discovered Zaber device"""
    port: str
    device_id: int
    serial_number: str
    name: str
    firmware_version: str
    device_type: str
    axis_count: int
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return asdict(self)


@dataclass
class StageStatus:
    """Current status of the linear stage"""
    position_mm: float
    velocity_mm_s: float
    is_moving: bool
    is_homed: bool
    timestamp: float
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return asdict(self)


class ZaberStage:
    """
    Simplified Zaber Linear Stage Controller
    
    Provides high-speed position reading and velocity control for Zaber stages.
    """
    
    # Default configuration file path
    DEFAULT_CONFIG_FILE = "zaber_config.json"
    
    def __init__(self, 
                 port: str = "auto",
                 position_limit_mm: Tuple[float, float] = (0.0, 100.0),
                 max_velocity_mm_s: float = 10.0,
                 reading_rate_hz: float = 100.0,
                 config_file: Optional[str] = None):
        """
        Initialize Zaber Stage Controller
        
        Args:
            port: Serial port (e.g., 'COM3') or 'auto' for auto-detection
            position_limit_mm: (min, max) position limits in mm
            max_velocity_mm_s: Maximum allowed velocity in mm/s
            reading_rate_hz: Position reading rate in Hz (max 100)
            config_file: Path to configuration file (optional)
        """
        self.port = port
        self.min_pos, self.max_pos = position_limit_mm
        self.max_velocity = max_velocity_mm_s
        self.reading_interval = 1.0 / min(reading_rate_hz, 100.0)
        self.config_file = config_file or self.DEFAULT_CONFIG_FILE
        
        # Connection objects (use Any type to avoid import errors)
        self.connection = None
        self.device = None
        self.axis = None
        self.device_info: Optional[DeviceInfo] = None
        
        # State tracking
        self._connected = False
        self._current_position = 0.0
        self._current_velocity = 0.0
        self._is_moving = False
        self._is_homed = False
        
        # Thread control
        self._lock = threading.Lock()
        self._reading_thread = None
        self._stop_reading = threading.Event()
        
        # Logging
        self.logger = logging.getLogger("ZaberStage")
        
        # Initialize library
        try:
            Library.enable_device_db_store()
        except:
            pass
        
        # Try to load configuration if auto mode
        if port == "auto" and os.path.exists(self.config_file):
            self.load_config()
    
    def connect(self) -> bool:
        """
        Connect to the Zaber stage
        
        Returns:
            bool: True if connection successful
        """
        try:
            # Auto-detect port if needed
            if self.port == "auto":
                # Try to find from saved devices or scan
                saved_devices = self.scan_devices()
                if saved_devices:
                    # Use first available device
                    self.port = saved_devices[0].port
                    self.device_info = saved_devices[0]
                    self.logger.info(f"Using saved device on {self.port}")
                else:
                    self.port = self._find_zaber_port()
                    if not self.port:
                        self.logger.error("No Zaber device found")
                        return False
            
            # Open connection
            self.connection = Connection.open_serial_port(self.port)
            devices = self.connection.detect_devices()
            
            if not devices:
                self.logger.error(f"No devices on port {self.port}")
                return False
            
            # Get first device and axis
            self.device = devices[0]
            self.axis = self.device.get_axis(1)
            
            # Store device information if not already stored
            if not self.device_info:
                self.device_info = self._get_device_info(self.device)
                # Save configuration for future use
                self.save_config()
            
            # No need to explicitly enable axis in newer versions
            # The axis is ready to use after connection
            
            # Check if homed
            self._is_homed = self.axis.is_homed()
            
            # Read initial position
            try:
                self._current_position = self.axis.get_position(Units.LENGTH_MILLIMETRES)
            except:
                self._current_position = 0.0
            
            self._connected = True
            self._start_position_reading()
            
            self.logger.info(f"Connected to {self.device_info.name} on {self.port}")
            return True
            
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from the stage"""
        self._stop_position_reading()
        
        if self.axis:
            try:
                self.axis.stop()
            except:
                pass
        
        if self.connection:
            try:
                self.connection.close()
            except:
                pass
        
        self._connected = False
        self.logger.info("Disconnected")
    
    def home(self) -> bool:
        """
        Home the stage
        
        Returns:
            bool: True if homing successful
        """
        if not self._connected or not self.axis:
            return False
        
        try:
            self.logger.info("Homing stage...")
            self.axis.home()
            self._is_homed = True
            self.logger.info("Homing complete")
            return True
        except Exception as e:
            self.logger.error(f"Homing failed: {e}")
            return False
    
    def set_velocity(self, velocity_mm_s: float) -> bool:
        """
        Set stage velocity for continuous movement
        
        Args:
            velocity_mm_s: Target velocity in mm/s (negative for reverse)
                          Will be clamped to max_velocity and position limits
        
        Returns:
            bool: True if command successful
        """
        if not self._connected or not self.axis or not self._is_homed:
            return False
        
        try:
            # Clamp velocity to limits
            velocity = max(-self.max_velocity, min(velocity_mm_s, self.max_velocity))
            
            # Check position limits and adjust velocity if needed
            current_pos = self.get_position()
            if current_pos <= self.min_pos and velocity < 0:
                velocity = 0  # Stop at minimum limit
            elif current_pos >= self.max_pos and velocity > 0:
                velocity = 0  # Stop at maximum limit
            
            # Send velocity command
            self.axis.move_velocity(velocity, Units.VELOCITY_MILLIMETRES_PER_SECOND)
            
            self._current_velocity = velocity
            return True
            
        except Exception as e:
            self.logger.error(f"Set velocity failed: {e}")
            return False
    
    def stop(self) -> bool:
        """
        Stop stage movement
        
        Returns:
            bool: True if stop command successful
        """
        if not self._connected or not self.axis:
            return False
        
        try:
            self.axis.stop()
            self._current_velocity = 0.0
            return True
        except Exception as e:
            self.logger.error(f"Stop failed: {e}")
            return False
    
    def move_to(self, position_mm: float) -> bool:
        """
        Move to absolute position
        
        Args:
            position_mm: Target position in mm
        
        Returns:
            bool: True if move command successful
        """
        if not self._connected or not self.axis or not self._is_homed:
            return False
        
        try:
            # Clamp to limits
            position = max(self.min_pos, min(position_mm, self.max_pos))
            
            # Move to position
            self.axis.move_absolute(position, Units.LENGTH_MILLIMETRES, wait_until_idle=False)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Move to position failed: {e}")
            return False
    
    def get_position(self) -> float:
        """
        Get current absolute position
        
        Returns:
            float: Position in mm
        """
        with self._lock:
            return self._current_position
    
    def get_distance_from_home(self) -> float:
        """
        Get distance from home position
        
        Returns:
            float: Distance in mm (always positive)
        """
        return abs(self.get_position())
    
    def get_status(self) -> StageStatus:
        """
        Get complete stage status
        
        Returns:
            StageStatus: Current status object
        """
        with self._lock:
            return StageStatus(
                position_mm=self._current_position,
                velocity_mm_s=self._current_velocity,
                is_moving=self._is_moving,
                is_homed=self._is_homed,
                timestamp=time.time()
            )
    
    def is_connected(self) -> bool:
        """Check if stage is connected"""
        return self._connected
    
    def is_homed(self) -> bool:
        """Check if stage is homed"""
        return self._is_homed
    
    def is_moving(self) -> bool:
        """Check if stage is currently moving"""
        with self._lock:
            return self._is_moving
    
    def scan_devices(self) -> List[DeviceInfo]:
        """
        Scan all serial ports for Zaber devices
        
        Returns:
            List[DeviceInfo]: List of discovered devices
        """
        discovered_devices = []
        
        # Get list of ports to scan
        import platform
        if platform.system() == "Windows":
            ports = [f"COM{i}" for i in range(1, 20)]
        else:
            ports = [f"/dev/ttyUSB{i}" for i in range(10)]
            ports.extend([f"/dev/ttyACM{i}" for i in range(10)])
        
        self.logger.info("Scanning for Zaber devices...")
        
        for port in ports:
            try:
                conn = Connection.open_serial_port(port)
                devices = conn.detect_devices()
                
                for device in devices:
                    device_info = self._get_device_info(device, port)
                    discovered_devices.append(device_info)
                    self.logger.info(f"Found: {device_info.name} on {port}")
                
                conn.close()
            except:
                continue
        
        self.logger.info(f"Found {len(discovered_devices)} Zaber device(s)")
        return discovered_devices
    
    def save_config(self, filename: Optional[str] = None) -> bool:
        """
        Save current configuration and device info to JSON file
        
        Args:
            filename: Optional custom filename (uses default if not specified)
        
        Returns:
            bool: True if saved successfully
        """
        filename = filename or self.config_file
        
        try:
            config = {
                "port": self.port,
                "position_limits_mm": [self.min_pos, self.max_pos],
                "max_velocity_mm_s": self.max_velocity,
                "reading_rate_hz": 1.0 / self.reading_interval,
                "device_info": self.device_info.to_dict() if self.device_info else None,
                "timestamp": time.time(),
                "timestamp_readable": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(filename, 'w') as f:
                json.dump(config, f, indent=2)
            
            self.logger.info(f"Configuration saved to {filename}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to save configuration: {e}")
            return False
    
    def load_config(self, filename: Optional[str] = None) -> bool:
        """
        Load configuration from JSON file
        
        Args:
            filename: Optional custom filename (uses default if not specified)
        
        Returns:
            bool: True if loaded successfully
        """
        filename = filename or self.config_file
        
        try:
            with open(filename, 'r') as f:
                config = json.load(f)
            
            # Load settings
            self.port = config.get("port", "auto")
            limits = config.get("position_limits_mm", [0, 100])
            self.min_pos, self.max_pos = limits[0], limits[1]
            self.max_velocity = config.get("max_velocity_mm_s", 10.0)
            reading_rate = config.get("reading_rate_hz", 100.0)
            self.reading_interval = 1.0 / reading_rate
            
            # Load device info if available
            if config.get("device_info"):
                info = config["device_info"]
                self.device_info = DeviceInfo(
                    port=info["port"],
                    device_id=info["device_id"],
                    serial_number=info["serial_number"],
                    name=info["name"],
                    firmware_version=info["firmware_version"],
                    device_type=info["device_type"],
                    axis_count=info["axis_count"]
                )
            
            self.logger.info(f"Configuration loaded from {filename}")
            return True
            
        except FileNotFoundError:
            self.logger.warning(f"Configuration file {filename} not found")
            return False
        except Exception as e:
            self.logger.error(f"Failed to load configuration: {e}")
            return False
    
    def get_device_info(self) -> Optional[DeviceInfo]:
        """
        Get information about connected device
        
        Returns:
            Optional[DeviceInfo]: Device information or None if not connected
        """
        return self.device_info
    
    def save_discovered_devices(self, devices: List[DeviceInfo], filename: str = "discovered_devices.json") -> bool:
        """
        Save list of discovered devices to JSON file
        
        Args:
            devices: List of DeviceInfo objects
            filename: Output filename
        
        Returns:
            bool: True if saved successfully
        """
        try:
            data = {
                "discovered_devices": [device.to_dict() for device in devices],
                "scan_timestamp": time.time(),
                "scan_timestamp_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
                "device_count": len(devices)
            }
            
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
            
            self.logger.info(f"Saved {len(devices)} device(s) to {filename}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to save discovered devices: {e}")
            return False
    
    # Private methods
    
    def _get_device_info(self, device, port: Optional[str] = None) -> DeviceInfo:
        """Extract device information from a Zaber device"""
        try:
            identity = device.identity
            return DeviceInfo(
                port=port or self.port,
                device_id=device.device_id,
                serial_number=str(identity.serial_number),
                name=identity.name,
                firmware_version=str(identity.firmware_version),
                device_type=identity.device_type,
                axis_count=device.axis_count
            )
        except:
            # Fallback for mock or limited info
            return DeviceInfo(
                port=port or self.port,
                device_id=0,
                serial_number="MOCK",
                name="Mock Device",
                firmware_version="1.0",
                device_type="Mock",
                axis_count=1
            )
    
    def _find_zaber_port(self) -> Optional[str]:
        """Auto-detect Zaber device port"""
        # Try common ports
        import platform
        if platform.system() == "Windows":
            ports = [f"COM{i}" for i in range(1, 20)]
        else:
            ports = [f"/dev/ttyUSB{i}" for i in range(10)]
            ports.extend([f"/dev/ttyACM{i}" for i in range(10)])
        
        for port in ports:
            try:
                conn = Connection.open_serial_port(port)
                devices = conn.detect_devices()
                conn.close()
                if devices:
                    return port
            except:
                continue
        
        return None
    
    def _start_position_reading(self):
        """Start high-speed position reading thread"""
        if self._reading_thread and self._reading_thread.is_alive():
            return
        
        self._stop_reading.clear()
        self._reading_thread = threading.Thread(target=self._read_position_loop, daemon=True)
        self._reading_thread.start()
    
    def _stop_position_reading(self):
        """Stop position reading thread"""
        self._stop_reading.set()
        if self._reading_thread:
            self._reading_thread.join(timeout=1.0)
    
    def _read_position_loop(self):
        """High-speed position reading loop"""
        while not self._stop_reading.is_set():
            try:
                if self.axis:
                    # Read position from device
                    position = self.axis.get_position(Units.LENGTH_MILLIMETRES)
                    
                    is_moving = self.axis.is_busy()
                    
                    # Update state with lock
                    with self._lock:
                        self._current_position = position
                        self._is_moving = is_moving
                        if not is_moving:
                            self._current_velocity = 0.0
                
            except Exception as e:
                self.logger.debug(f"Position read error: {e}")
            
            # Wait for next reading
            time.sleep(self.reading_interval)


# Convenience functions for direct usage
def create_stage(port="auto", position_limits=(0, 100), max_velocity=10, reading_rate=100):
    """
    Create and connect to a Zaber stage
    
    Args:
        port: Serial port or 'auto'
        position_limits: (min, max) in mm
        max_velocity: Maximum velocity in mm/s
        reading_rate: Position reading rate in Hz
    
    Returns:
        ZaberStage: Connected stage object or None if failed
    """
    stage = ZaberStage(port, position_limits, max_velocity, reading_rate)
    if stage.connect():
        return stage
    return None


def discover_all_devices() -> List[DeviceInfo]:
    """
    Discover all Zaber devices on the system
    
    Returns:
        List[DeviceInfo]: List of all discovered devices
    """
    stage = ZaberStage()
    devices = stage.scan_devices()
    
    if devices:
        # Save to file for reference
        stage.save_discovered_devices(devices)
    
    return devices


def load_stage_from_config(config_file: str = "zaber_config.json"):
    """
    Load and connect to a stage using saved configuration
    
    Args:
        config_file: Path to configuration file
    
    Returns:
        ZaberStage: Connected stage object or None if failed
    """
    stage = ZaberStage(config_file=config_file)
    
    if stage.load_config():
        if stage.connect():
            return stage
    
    return None
