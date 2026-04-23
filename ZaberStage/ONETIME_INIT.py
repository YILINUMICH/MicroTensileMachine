#!/usr/bin/env python3
"""
Zaber Stage One-Time Initialization Script
=========================================

This script checks for required dependencies, installs them if needed,
and performs initial device discovery and configuration for Zaber linear stages.

Run this script first before using the Zaber stage module.

Author: Yilin Ma
Date: November 2025
University of Michigan Robotics
HDR Lab
"""

import json
import time
import logging
import sys
import subprocess

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("FirstRun")


def check_and_install_dependencies():
    """Check for zaber_motion library and install if missing"""
    
    print("\n" + "="*60)
    print("CHECKING DEPENDENCIES")
    print("="*60)
    
    try:
        import zaber_motion
        print("✅ zaber-motion library is already installed")
        print(f"   Version: {zaber_motion.__version__ if hasattr(zaber_motion, '__version__') else 'unknown'}")
        return True
    except ImportError:
        print("❌ zaber-motion library not found")
        print("\nInstalling zaber-motion...")
        
        try:
            # Install using pip
            subprocess.check_call([
                sys.executable, 
                "-m", 
                "pip", 
                "install", 
                "zaber-motion"
            ])
            
            print("✅ zaber-motion installed successfully")
            print("\nPlease restart this script to complete initialization.")
            return False
            
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to install zaber-motion: {e}")
            print("\nPlease install manually:")
            print("  pip install zaber-motion")
            return False


# Check dependencies first
if not check_and_install_dependencies():
    print("\n" + "="*60)
    print("Dependency installation complete.")
    print("Please run this script again to continue with device setup.")
    print("="*60)
    sys.exit(0)

# Now import zaber_stage module
from zaber_stage import ZaberStage, discover_all_devices, load_stage_from_config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("DeviceDiscovery")


def discover_and_save():
    """Discover all Zaber devices and save their information"""
    
    print("\n" + "="*60)
    print("ZABER DEVICE DISCOVERY")
    print("="*60)
    
    print("\nScanning for Zaber devices...")
    
    # Discover all devices
    devices = discover_all_devices()
    
    if not devices:
        print("No Zaber devices found.")
        print("\nTroubleshooting:")
        print("1. Check USB/serial connections")
        print("2. Verify device power")
        print("3. Check device drivers")
        return None
    
    # Display discovered devices
    print(f"\nFound {len(devices)} device(s):")
    print("-" * 40)
    
    for i, device in enumerate(devices, 1):
        print(f"\nDevice {i}:")
        print(f"  Name: {device.name}")
        print(f"  Port: {device.port}")
        print(f"  Serial Number: {device.serial_number}")
        print(f"  Type: {device.device_type}")
        print(f"  Firmware: {device.firmware_version}")
        print(f"  Axes: {device.axis_count}")
    
    # Save device information
    filename = "discovered_devices.json"
    print(f"\nDevice information saved to: {filename}")
    
    return devices


def setup_first_device(devices):
    """Setup and configure the first discovered device"""
    
    if not devices:
        return None
    
    print("\n" + "="*60)
    print("DEVICE SETUP")
    print("="*60)
    
    device_info = devices[0]
    print(f"\nSetting up: {device_info.name} on {device_info.port}")
    
    # Create stage with specific configuration
    stage = ZaberStage(
        port=device_info.port,
        position_limit_mm=(0, 100),    # Adjust based on your stage
        max_velocity_mm_s=10.0,         # Adjust based on your needs
        reading_rate_hz=100.0           # Maximum reading rate
    )
    
    # Connect to the stage
    print("Connecting...")
    if not stage.connect():
        print("Failed to connect!")
        return None
    
    print("Connected successfully!")
    
    # Save configuration for future use
    config_file = "zaber_config.json"
    stage.save_config(config_file)
    print(f"Configuration saved to: {config_file}")
    
    return stage


def test_saved_configuration():
    """Test loading from saved configuration"""
    
    print("\n" + "="*60)
    print("TESTING SAVED CONFIGURATION")
    print("="*60)
    
    print("\nLoading configuration from file...")
    
    # Load stage from configuration
    stage = load_stage_from_config("zaber_config.json")
    
    if not stage:
        print("Failed to load configuration!")
        return None
    
    print("Configuration loaded and connected successfully!")
    
    # Display loaded configuration
    device_info = stage.get_device_info()
    if device_info:
        print(f"\nLoaded Device:")
        print(f"  Name: {device_info.name}")
        print(f"  Port: {device_info.port}")
        print(f"  Serial: {device_info.serial_number}")
    
    print(f"\nStage Settings:")
    print(f"  Position Limits: {stage.min_pos} to {stage.max_pos} mm")
    print(f"  Max Velocity: {stage.max_velocity} mm/s")
    print(f"  Reading Rate: {1/stage.reading_interval:.1f} Hz")
    
    return stage


def demonstrate_multi_config():
    """Demonstrate managing multiple configuration files"""
    
    print("\n" + "="*60)
    print("MULTIPLE CONFIGURATION MANAGEMENT")
    print("="*60)
    
    # Example: Different configurations for different tests
    configs = {
        "high_speed_config.json": {
            "description": "High-speed testing configuration",
            "position_limits": (0, 50),
            "max_velocity": 20.0,
            "reading_rate": 100.0
        },
        "precision_config.json": {
            "description": "Precision positioning configuration",
            "position_limits": (0, 200),
            "max_velocity": 1.0,
            "reading_rate": 50.0
        },
        "safety_config.json": {
            "description": "Safety-limited configuration",
            "position_limits": (10, 40),
            "max_velocity": 5.0,
            "reading_rate": 100.0
        }
    }
    
    print("\nCreating multiple configuration profiles...")
    
    for filename, settings in configs.items():
        print(f"\n{filename}:")
        print(f"  {settings['description']}")
        
        # Create stage with specific settings
        stage = ZaberStage(
            port="auto",
            position_limit_mm=settings['position_limits'],
            max_velocity_mm_s=settings['max_velocity'],
            reading_rate_hz=settings['reading_rate']
        )
        
        # Save configuration
        if stage.connect():
            stage.save_config(filename)
            print(f"  ✅ Configuration saved")
            stage.disconnect()
        else:
            # Save configuration even without connection
            config = {
                "port": "auto",
                "position_limits_mm": list(settings['position_limits']),
                "max_velocity_mm_s": settings['max_velocity'],
                "reading_rate_hz": settings['reading_rate'],
                "description": settings['description'],
                "device_info": None,
                "timestamp": time.time(),
                "timestamp_readable": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(filename, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"  ✅ Template configuration saved")


def view_configuration_file(filename="zaber_config.json"):
    """Display contents of a configuration file"""
    
    print("\n" + "="*60)
    print(f"CONFIGURATION FILE: {filename}")
    print("="*60)
    
    try:
        with open(filename, 'r') as f:
            config = json.load(f)
        
        print(json.dumps(config, indent=2))
        
    except FileNotFoundError:
        print(f"File not found: {filename}")
    except Exception as e:
        print(f"Error reading file: {e}")


def main():
    """Main demonstration routine"""
    
    print("\n" + "="*60)
    print("ZABER DEVICE DISCOVERY AND CONFIGURATION DEMO")
    print("="*60)
    
    # Step 1: Discover devices
    devices = discover_and_save()
    
    if devices:
        # Step 2: Setup first device
        stage = setup_first_device(devices)
        
        if stage:
            # Test the connection
            print("\nTesting device...")
            
            # Check if homed
            if not stage.is_homed():
                print("Homing stage...")
                stage.home()
            
            # Get current position
            position = stage.get_position()
            print(f"Current position: {position:.3f} mm")
            
            # Disconnect
            stage.disconnect()
            print("Disconnected from device")
        
        # Step 3: Test loading from configuration
        test_stage = test_saved_configuration()
        if test_stage:
            test_stage.disconnect()
    
    # Step 4: Demonstrate multiple configurations
    demonstrate_multi_config()
    
    # Step 5: View saved configuration
    view_configuration_file("zaber_config.json")
    
    print("\n" + "="*60)
    print("DEMONSTRATION COMPLETE")
    print("="*60)
    print("\nCreated files:")
    print("  - discovered_devices.json (device scan results)")
    print("  - zaber_config.json (main configuration)")
    print("  - high_speed_config.json (high-speed profile)")
    print("  - precision_config.json (precision profile)")
    print("  - safety_config.json (safety-limited profile)")
    print("\nYou can now use these configurations to quickly connect to your device:")
    print("  stage = load_stage_from_config('zaber_config.json')")


if __name__ == "__main__":
    main()
