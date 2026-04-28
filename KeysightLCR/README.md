# LCR Meter Module

A high-performance Python module for controlling E4980A/AL LCR meters, optimized for maximum reading speed and designed for easy integration into larger projects.

## Features

- **High-Speed Measurements**: Optimized for maximum reading rate (up to 100+ readings/second)
- **Modular Design**: Clean API for easy integration into larger projects
- **Multiple Measurement Functions**: Support for capacitance, inductance, resistance measurements
- **Flexible Data Acquisition**: Single, burst, or continuous measurements
- **Callback Support**: Real-time data processing with custom callbacks
- **Robust Error Handling**: Automatic recovery from communication errors
- **Context Manager**: Safe resource management with automatic cleanup

## Installation

### Requirements
- Python 3.6+
- PyVISA library
- E4980A or E4980AL LCR Meter
- USB or LAN connection to the instrument

### Install Dependencies
```bash
pip install pyvisa pyvisa-py
```

For better USB support, install Keysight IO Libraries Suite or NI-VISA drivers.

## Quick Start

### Basic Usage
```python
from lcr_meter import LCRMeter, MeasurementConfig, MeasurementFunction

# Auto-connect to LCR meter
with LCRMeter() as meter:
    # Configure measurement
    config = MeasurementConfig(
        frequency=1000,  # 1 kHz
        voltage=1.0,     # 1V
        function=MeasurementFunction.CPD  # Capacitance-Dissipation
    )
    meter.configure(config)
    
    # Single reading
    result = meter.read_single()
    print(meter.format_result(result))
    
    # Burst of 100 readings
    results = meter.read_burst(100)
```

### Quick Measurement Function
```python
from lcr_meter import quick_measure, MeasurementFunction

# Quick 100 capacitance measurements
results = quick_measure(
    function=MeasurementFunction.CPD,
    frequency=1000,
    voltage=1.0,
    count=100
)

for r in results:
    print(f"C={r.primary:.3e}F, D={r.secondary:.4f}")
```

## Module Structure

### Main Classes

#### `LCRMeter`
Main interface class for instrument control.

**Key Methods:**
- `__init__(resource_string=None)`: Initialize connection (auto-detect if None)
- `configure(config)`: Set measurement parameters
- `read_single()`: Read one measurement
- `read_burst(count)`: Read multiple measurements quickly
- `read_continuous(callback, max_readings, max_duration)`: Continuous acquisition
- `close()`: Clean shutdown

#### `MeasurementConfig`
Configuration dataclass for measurement parameters.

**Parameters:**
- `frequency`: Measurement frequency (20 Hz - 2 MHz)
- `voltage`: Signal level (0 - 20V)
- `function`: Measurement function (CPD, LSRS, etc.)
- `averaging`: Number of averages (1 for max speed)
- `integration_time`: 'SHOR', 'MED', or 'LONG'

#### `MeasurementResult`
Result dataclass for measurement data.

**Attributes:**
- `primary`: Primary measurement value
- `secondary`: Secondary measurement value
- `status`: Status code (0 = OK)
- `timestamp`: Unix timestamp
- `reading_number`: Sequential reading number

### Measurement Functions

| Function | Primary | Secondary | Use Case |
|----------|---------|-----------|----------|
| CPD | Parallel Capacitance | Dissipation Factor | General capacitors |
| CPQ | Parallel Capacitance | Quality Factor | High-Q capacitors |
| CSRS | Series Capacitance | Series Resistance | Electrolytic caps |
| CSD | Series Capacitance | Dissipation Factor | Series model caps |
| LSRS | Series Inductance | Series Resistance | General inductors |
| LSD | Series Inductance | Dissipation Factor | Inductor Q testing |
| RX | Resistance | Reactance | General impedance |

## Performance Optimization

### Maximum Speed Settings
```python
config = MeasurementConfig(
    frequency=1000,
    voltage=1.0,
    function=MeasurementFunction.CPD,
    averaging=1,              # No averaging (critical)
    integration_time='SHOR'   # Shortest integration (critical)
)

meter.configure(config)

# For absolute maximum speed: disable display
meter.instrument.write(':DISP:ENAB OFF')  # +18% speed boost
results = meter.read_burst(1000)
meter.instrument.write(':DISP:ENAB ON')   # Re-enable when done
```

### Measured Performance (E4980AL)

**USB Connection (Fastest):**
- **SHORT + Display OFF**: **24.5 readings/second** ⚡ **MAXIMUM**
- **SHORT + Display ON**: 19.2 readings/second

**Ethernet Connection (169.254.157.92):**
- **SHORT + Display OFF**: 23.4 readings/second (95.5% of USB speed)
- **SHORT + Display ON**: 19.9 readings/second

**Other Integration Times:**
- **MEDIUM integration**: 6.4 readings/second  
- **LONG integration**: 2.6 readings/second

**Comparison Tool:**
```powershell
$env:LCR_IP = '169.254.157.92'
python speed_comparison.py  # Compare USB vs Ethernet
```

See `SPEED_OPTIMIZATION.md` for detailed benchmarks and analysis.

## Advanced Usage

### Ethernet (LAN) Connection
You can connect to the E4980A/AL over LAN using VISA TCPIP resources.

- Ensure your PC and instrument are on the same network.
- Install a VISA backend that supports LAN (Keysight IO Libraries, NI-VISA, or PyVISA-py with VXI-11/HiSLIP).

```python
from lcr_meter import LCRMeter, MeasurementConfig, MeasurementFunction

meter = LCRMeter()

# If auto-detect didn't find USB/GPIB, connect via LAN explicitly:
# Option 1: Provide IP directly
meter.connect_ethernet(ip="192.168.1.120", protocol="vxi11")

# Option 2: Provide MAC (Windows ARP lookup used to resolve IP)
meter.connect_ethernet(mac="80.09.02.18.62.03", protocol="vxi11")

# Alternative protocols: 'hislip' or 'socket' (raw SCPI on port 5025)
# meter.connect_ethernet(ip="192.168.1.120", protocol="hislip")
# meter.connect_ethernet(ip="192.168.1.120", protocol="socket")

config = MeasurementConfig(frequency=1000, voltage=1.0, function=MeasurementFunction.CPD)
meter.configure(config)
result = meter.read_single()
print(meter.format_result(result))
meter.close()
```

Notes:
- MAC formats accepted: `80:09:02:18:62:03`, `80-09-02-18-62-03`, or `80.09.02.18.62.03`.
- On Windows, IP resolution uses `arp -a`. If the MAC isn't present in the ARP table, ping the subnet or assign a static IP using Keysight Connection Expert.
- Typical VISA resource strings:
    - VXI-11: `TCPIP0::<ip>::inst0::INSTR`
    - HiSLIP: `TCPIP0::<ip>::hislip0::INSTR`
    - Socket: `TCPIP0::<ip>::5025::SOCKET`

### Continuous Monitoring with Callback
```python
def process_data(result):
    """Called for each measurement"""
    if result.primary > 1e-6:  # Alert if > 1µF
        print(f"High capacitance: {result.primary*1e6:.2f}µF")
    return result.reading_number < 1000  # Stop after 1000

with LCRMeter() as meter:
    meter.configure(config)
    results = meter.read_continuous(callback=process_data)
```

### Integration into Larger System
```python
class TestSystem:
    def __init__(self):
        self.lcr = LCRMeter()
        self.lcr.configure(MeasurementConfig(
            frequency=10000,
            function=MeasurementFunction.LSRS
        ))
        self.data = []
    
    def test_component(self, component_id):
        """Test a single component"""
        result = self.lcr.read_single()
        if result and result.is_valid:
            self.data.append({
                'id': component_id,
                'inductance': result.primary,
                'resistance': result.secondary,
                'timestamp': result.timestamp
            })
            return True
        return False
    
    def cleanup(self):
        self.lcr.close()
```

### Data Logging to CSV
```python
import csv
from datetime import datetime

with LCRMeter() as meter:
    meter.configure(config)
    
    filename = f"lcr_data_{datetime.now():%Y%m%d_%H%M%S}.csv"
    
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Time', 'Capacitance', 'Dissipation'])
        
        for result in meter.read_burst(1000):
            writer.writerow([
                result.timestamp,
                result.primary,
                result.secondary
            ])
```

## Testing

The test suite automatically tries USB/GPIB connection first, then falls back to Ethernet if needed.

### Run Complete Test Suite
```bash
# Auto-detect connection (USB/GPIB first, then Ethernet)
python test_lcr_meter.py

# Specify Ethernet connection via IP
$env:LCR_IP = '169.254.157.92'
python test_lcr_meter.py

# Or via MAC address (will resolve to IP via ARP)
$env:LCR_MAC = '80.09.02.18.62.03'
python test_lcr_meter.py
```

### Quick Connection Test
```bash
python test_lcr_meter.py --quick
```

### Performance Benchmark
```bash
python test_lcr_meter.py --bench
```

### View Usage Examples
```bash
python test_lcr_meter.py --demo
```

## Example Applications

Run the examples file to see various use cases:
```bash
# Show all examples
python examples_lcr.py

# Run specific example (1-5)
python examples_lcr.py 1
```

Examples include:
1. Basic measurements
2. High-speed data logging
3. Real-time monitoring with thresholds
4. Automated component testing
5. Frequency sweep measurements

## Troubleshooting

### Connection Issues
- Ensure LCR meter is powered on
- Check USB/LAN cable connection
- Install Keysight IO Libraries Suite for better USB support
- On Linux/Mac, may need sudo for USB access
- For LAN: Verify instrument IP, same subnet, and firewall rules allow VXI-11/HiSLIP/socket ports.

### Performance Issues
- Use shortest integration time ('SHOR')
- Disable averaging (set to 1)
- Use USB instead of LAN for lower latency
- Close other programs using the instrument

### Communication Errors
- Module automatically handles timeouts and retries
- If persistent errors, check cable/connection
- Verify no other software is using the instrument

## API Reference

### Core Functions

#### `quick_measure(function, frequency, voltage, count)`
Quick measurement without explicit meter setup.

#### `measure_with_callback(callback, **config_kwargs)`
Measurement with custom processing callback.

### Configuration Options

All frequency values in Hz, voltage in Volts.

**Frequency Range:** 20 Hz to 2 MHz (E4980A)  
**Voltage Range:** 0 to 20V  
**Current Range:** 0 to 0.1A (optional)

## License

This module is provided as-is for educational and research purposes.

## Author

Developed for Python programming instruction and instrumentation control at the University of Michigan.
