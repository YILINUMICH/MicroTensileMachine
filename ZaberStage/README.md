# Zaber Linear Stage Control Module

A simplified Python module for controlling Zaber linear stages with high-speed position reading, velocity control, and device management capabilities.

**Author:** Yilin Ma  
**Date:** November 2025  
**University of Michigan Robotics - HDR Lab**

## Features

- **Device Discovery**: Automatic scanning and detection of Zaber devices
- **Configuration Persistence**: Save/load device settings to JSON files
- **High-Speed Position Reading**: Up to 100 Hz continuous position monitoring
- **Velocity Control**: Precise velocity control within defined limits
- **Position Control**: Absolute and relative positioning
- **Safety Features**: Position limits and velocity clamping
- **Thread-Safe**: All operations are thread-safe

## Installation

### Prerequisites

```bash
# Required: Install the Zaber Motion Library
pip install zaber-motion

# Or install all dependencies from requirements file
pip install -r requirements.txt
```

### First-Time Setup

**Run the initialization script first:**

```bash
python ONETIME_INIT.py
```

This script will:
1. Check and install `zaber-motion` if not present
2. Discover all connected Zaber devices
3. Create configuration files for quick setup
4. Generate multiple configuration profiles (high-speed, precision, safety)

### Module Files

- `zaber_stage.py` - Main control module
- `test_zaber_stage.py` - Comprehensive test suite
- `ONETIME_INIT.py` - First-time setup and device discovery
- `README.md` - This documentation file

## Quick Start

### Basic Usage

```python
from zaber_stage import ZaberStage

# Create and connect to stage
stage = ZaberStage(
    port="auto",                    # Auto-detect or specify "COM3"
    position_limit_mm=(0, 100),     # Set safe operating range
    max_velocity_mm_s=10.0,          # Maximum velocity
    reading_rate_hz=100.0            # Position reading rate
)

# Connect and initialize
if stage.connect():
    # Home the stage
    stage.home()
    
    # Move to position
    stage.move_to(50.0)  # Move to 50mm
    
    # Velocity control
    stage.set_velocity(5.0)  # Move at 5 mm/s
    time.sleep(2)
    stage.stop()
    
    # Read position
    position = stage.get_position()  # Current position in mm
    distance = stage.get_distance_from_home()  # Distance from home
    
    # Get full status
    status = stage.get_status()
    print(f"Position: {status.position_mm:.3f} mm")
    print(f"Moving: {status.is_moving}")
    
    stage.disconnect()
```

### Device Discovery

```python
from zaber_stage import discover_all_devices

# Scan for all Zaber devices
devices = discover_all_devices()

for device in devices:
    print(f"Found: {device.name}")
    print(f"  Port: {device.port}")
    print(f"  Serial: {device.serial_number}")
    print(f"  Type: {device.device_type}")
```

### Configuration Management

```python
# Save current configuration
stage.save_config("my_stage_config.json")

# Load from saved configuration
from zaber_stage import load_stage_from_config

stage = load_stage_from_config("my_stage_config.json")
if stage:
    print("Loaded and connected successfully")
```

## API Reference

### Class: `ZaberStage`

#### Constructor

```python
ZaberStage(port="auto", 
           position_limit_mm=(0, 100),
           max_velocity_mm_s=10.0,
           reading_rate_hz=100.0,
           config_file=None)
```

**Parameters:**
- `port`: Serial port name or "auto" for auto-detection
- `position_limit_mm`: Tuple of (min, max) position limits in mm
- `max_velocity_mm_s`: Maximum allowed velocity in mm/s
- `reading_rate_hz`: Position reading rate in Hz (max 100)
- `config_file`: Optional path to configuration file

#### Main Methods

##### Connection and Control

| Method | Description | Returns |
|--------|-------------|---------|
| `connect()` | Connect to the stage | `bool` - Success status |
| `disconnect()` | Disconnect from the stage | None |
| `home()` | Home the stage | `bool` - Success status |
| `stop()` | Emergency stop | `bool` - Success status |

##### Movement

| Method | Description | Parameters | Returns |
|--------|-------------|------------|---------|
| `move_to(position_mm)` | Move to absolute position | `position_mm`: Target position | `bool` - Success |
| `set_velocity(velocity_mm_s)` | Set continuous velocity | `velocity_mm_s`: Target velocity (-/+) | `bool` - Success |

##### Position Reading

| Method | Description | Returns |
|--------|-------------|---------|
| `get_position()` | Get current position | `float` - Position in mm |
| `get_distance_from_home()` | Get absolute distance from home | `float` - Distance in mm |
| `get_status()` | Get complete status | `StageStatus` object |

##### Status Queries

| Method | Description | Returns |
|--------|-------------|---------|
| `is_connected()` | Check connection status | `bool` |
| `is_homed()` | Check if homed | `bool` |
| `is_moving()` | Check if moving | `bool` |

##### Device Management

| Method | Description | Returns |
|--------|-------------|---------|
| `scan_devices()` | Scan for all Zaber devices | `List[DeviceInfo]` |
| `save_config(filename)` | Save configuration to JSON | `bool` - Success |
| `load_config(filename)` | Load configuration from JSON | `bool` - Success |
| `get_device_info()` | Get connected device info | `DeviceInfo` or None |

### Data Classes

#### `StageStatus`

```python
@dataclass
class StageStatus:
    position_mm: float        # Current position in mm
    velocity_mm_s: float      # Current velocity in mm/s
    is_moving: bool          # Movement status
    is_homed: bool           # Homing status
    timestamp: float         # Unix timestamp
```

#### `DeviceInfo`

```python
@dataclass
class DeviceInfo:
    port: str                # Serial port
    device_id: int           # Device ID
    serial_number: str       # Serial number
    name: str               # Device name
    firmware_version: str    # Firmware version
    device_type: str        # Device type
    axis_count: int         # Number of axes
```

### Convenience Functions

```python
# Quick stage creation
stage = create_stage(port="auto", position_limits=(0, 100))

# Discover all devices
devices = discover_all_devices()

# Load from config
stage = load_stage_from_config("config.json")
```

## Configuration Files

### Configuration JSON Format

The module saves configurations in JSON format:

```json
{
  "port": "COM3",
  "position_limits_mm": [0, 100],
  "max_velocity_mm_s": 10.0,
  "reading_rate_hz": 100.0,
  "device_info": {
    "port": "COM3",
    "device_id": 1,
    "serial_number": "12345",
    "name": "X-LRM200A",
    "firmware_version": "7.15",
    "device_type": "Linear Stage",
    "axis_count": 1
  },
  "timestamp": 1234567890.123,
  "timestamp_readable": "2025-11-26 12:00:00"
}
```

### Discovered Devices JSON Format

Device discovery results are saved as:

```json
{
  "discovered_devices": [
    {
      "port": "COM3",
      "device_id": 1,
      "serial_number": "12345",
      "name": "X-LRM200A",
      "firmware_version": "7.15",
      "device_type": "Linear Stage",
      "axis_count": 1
    }
  ],
  "scan_timestamp": 1234567890.123,
  "scan_timestamp_readable": "2025-11-26 12:00:00",
  "device_count": 1
}
```

## Testing

Run the comprehensive test suite:

```bash
# Run full test suite with interactive mode
python test_zaber_stage.py
```

### Test Suite Includes:

1. **Connection Test** - Verify device connection and communication
2. **Homing Test** - Stage homing and home position verification
3. **Position Reading Test** - High-speed position reading at 100 Hz
4. **Absolute Movement Test** - Precise position moves with error checking
5. **Velocity Control Test** - Continuous velocity control in both directions
6. **Position Limits Test** - Software position limit enforcement
7. **Status Reporting Test** - Real-time status information retrieval
8. **Stop Command Test** - Emergency stop functionality
9. **Performance Benchmark** - Speed and response time measurements
10. **Interactive Mode** - Manual control for hands-on testing

### Test Coverage

- **Functional Tests**: Connection, homing, movement, velocity control
- **Performance Tests**: Reading rate, response time, tracking accuracy
- **Safety Tests**: Position limits, emergency stop
- **Interactive Mode**: Manual testing with commands (h, m<pos>, v<vel>, s, p, status, q)

## Performance Specifications

| Specification | Value |
|---------------|-------|
| Maximum Position Reading Rate | 100 Hz |
| Position Resolution | 0.001 mm |
| Thread Safety | Yes |
| Typical Command Response Time | < 10 ms |
| Position Update Latency | < 10 ms |

## Error Handling

The module includes comprehensive error handling:

```python
# Example with error handling
try:
    stage = ZaberStage()
    
    if not stage.connect():
        print("Failed to connect")
        # Check logs for details
    
    if not stage.home():
        print("Homing failed")
    
    # Safe movement with limits
    stage.move_to(200)  # Will be clamped to max limit
    
except Exception as e:
    print(f"Error: {e}")
finally:
    stage.disconnect()
```

## Logging

The module uses Python's logging system:

```python
import logging

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)

# Module logger name: "ZaberStage"
logger = logging.getLogger("ZaberStage")
```

## Safety Features

1. **Position Limits**: Automatic enforcement of min/max positions
2. **Velocity Clamping**: Velocities limited to safe maximum
3. **Emergency Stop**: Immediate stop function
4. **Thread Safety**: All position reads are thread-safe
5. **Boundary Checking**: Automatic stop at limits during velocity control

## Troubleshooting

### Common Issues

1. **Import Error: "cannot import name 'Connection'"**
   - Run `python first_run_initialization.py` to install dependencies
   - Or manually: `pip install zaber-motion`

2. **Device Not Found**
   - Check USB/serial connection
   - Run device discovery: `python first_run_initialization.py`
   - Check device power
   - Verify drivers are installed

3. **"'Axis' object has no attribute 'enable'" Error**
   - This is fixed in the current version (uses zaber-motion 7.x API)
   - Update your installation: `pip install --upgrade zaber-motion`

4. **Absolute Movement Test Fails**
   - Ensure stage is homed before testing
   - Allow sufficient time for movements (already fixed in test suite)
   - Check mechanical obstructions

5. **Permission Errors (Linux/Mac)**
   ```bash
   # Add user to dialout group
   sudo usermod -a -G dialout $USER
   # Logout and login again
   ```

6. **Slow Reading Rate**
   - Check USB connection quality
   - Reduce reading rate if needed: `reading_rate_hz=50`
   - Check system performance and CPU usage

## Example Applications

### 1. Tensile Testing

```python
# Constant velocity pull test
stage.home()
stage.move_to(0)  # Start position

# Pull at constant rate
stage.set_velocity(0.5)  # 0.5 mm/s

# Monitor position
while stage.get_position() < 50:
    position = stage.get_position()
    force = read_load_cell()  # Your force reading
    print(f"Position: {position:.3f} mm, Force: {force:.3f} N")
    time.sleep(0.01)

stage.stop()
```

### 2. Cyclic Testing

```python
# Cyclic movement between positions
for cycle in range(10):
    stage.move_to(0)
    while stage.is_moving():
        time.sleep(0.1)
    
    stage.move_to(10)
    while stage.is_moving():
        time.sleep(0.1)
    
    print(f"Cycle {cycle + 1} complete")
```

### 3. Position Synchronization

```python
# Synchronize with external sensor
target_positions = [10, 20, 30, 40, 50]

for target in target_positions:
    stage.move_to(target)
    
    # Wait for movement and sensor
    while stage.is_moving():
        time.sleep(0.01)
    
    sensor_reading = read_sensor()  # Your sensor
    actual_position = stage.get_position()
    
    print(f"Target: {target}, Actual: {actual_position:.3f}, Sensor: {sensor_reading}")
```

## Quick Start Workflow

### Step 1: First-Time Setup
```bash
python first_run_initialization.py
```

### Step 2: Run Tests
```bash
python test_zaber_stage.py
```

### Step 3: Use in Your Application
```python
from zaber_stage import load_stage_from_config

stage = load_stage_from_config('zaber_config.json')
stage.home()
stage.move_to(50.0)
```

## Configuration Files Created

After running `first_run_initialization.py`, you'll have:

- **`zaber_config.json`** - Default configuration
- **`high_speed_config.json`** - High-speed testing (0-50mm, 20mm/s)
- **`precision_config.json`** - Precision positioning (0-200mm, 1mm/s)
- **`safety_config.json`** - Safety-limited (10-40mm, 5mm/s)
- **`discovered_devices.json`** - List of all found devices

## License

This module is part of the University of Michigan Robotics HDR Lab project.

## Support

For issues or questions:
1. Run `first_run_initialization.py` for device discovery
2. Check test output: `python test_zaber_stage.py`
3. Enable debug logging for detailed trace
4. Use `scan_devices()` to verify device connectivity
5. Refer to Zaber Motion Library documentation for advanced features

## Version History

- **v1.0** (November 2025): Initial release with core functionality
  - Device discovery and configuration persistence
  - High-speed position reading (100 Hz)
  - Velocity and position control
  - Comprehensive test suite
  - First-run initialization script
  - Compatible with zaber-motion 7.x

## Project Information

**Author:** Yilin Ma  
**Institution:** University of Michigan  
**Lab:** Robotics - HDR Lab  
**Date:** November 2025

## Related Modules

This module is part of a larger project that includes:
- **LCR Module** - E4980A/AL LCR Meter control for electrical measurements
- **Zaber Module** - Linear stage control (this module)

Both modules follow similar design patterns for ease of integration.