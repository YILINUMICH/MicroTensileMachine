#!/usr/bin/env python3
"""
Test Suite for E4980A/AL LCR Meter Module
==========================================

This comprehensive test suite validates all functionality of the lcr_meter module
and demonstrates proper usage patterns.

Tests Performed:
1. Connection Test - Auto-detection and device identification
2. Configuration Test - Multiple parameter configurations (frequency, voltage, function)
3. Single Reading Test - Individual measurement with validation
4. Burst Reading Test - High-speed burst measurements with rate calculation
5. Continuous with Callback Test - Streaming measurements with custom callbacks
6. Measurement Functions Test - Different measurement modes (CPD, LSRS, RX, etc.)
7. Error Handling Test - Invalid configuration recovery
8. Context Manager Test - Proper resource cleanup verification
9. Quick Functions Test - Convenience function wrappers
10. Performance Benchmark Test - Speed comparison across integration times (SHORT, MED, LONG)

Usage:
    python test_lcr_meter.py           # Run all tests
    python test_lcr_meter.py --quick   # Quick connection test only
    python test_lcr_meter.py --bench   # Performance benchmark only
    python test_lcr_meter.py --demo    # Show usage examples

Requirements:
- Physical E4980A/AL LCR meter connected via USB/GPIB/LAN
- pyvisa and pyvisa-py installed
- Proper VISA backend configured
- Keysight I/O Libraries installed

Author: Yilin Ma
Date: November 2025
University of Michigan Robotics
HDR Lab
"""

import sys
import time
import logging
import os
from pathlib import Path

# Import the LCR meter module
try:
    from lcr_meter import (
        LCRMeter, MeasurementConfig, MeasurementFunction, 
        MeasurementResult, quick_measure, measure_with_callback
    )
except ImportError:
    print("Error: lcr_meter.py module not found in current directory")
    sys.exit(1)

# Configure logging for tests
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TestLCRMeter:
    """Test suite for LCR meter module"""
    
    def __init__(self):
        self.meter = None
        self.tests_passed = 0
        self.tests_failed = 0
        self.test_results = []
    
    def setup(self):
        """Setup test environment"""
        print("\n" + "="*70)
        print("LCR METER MODULE TEST SUITE")
        print("="*70)
        print("\nInitializing test environment...")
        
        # Try to connect to meter (auto-detect USB/GPIB first)
        self.meter = LCRMeter()
        
        # If auto-detect failed, try Ethernet connection
        if not self.meter.instrument:
            print("\n⚠️  No device found via USB/GPIB")
            print("   Attempting Ethernet (LAN) connection...")
            
            # Read from environment variables
            lcr_ip = os.environ.get('LCR_IP', None)
            lcr_mac = os.environ.get('LCR_MAC', '80.09.02.18.62.03')
            
            if lcr_ip:
                print(f"   Using IP from environment: {lcr_ip}")
            elif lcr_mac:
                print(f"   Using MAC from environment: {lcr_mac}")
            
            # Try to connect via Ethernet
            if self.meter.connect_ethernet(ip=lcr_ip, mac=lcr_mac, protocol='vxi11'):
                print("✓ Connected to LCR meter via Ethernet")
                return True
            else:
                print("\n⚠️  WARNING: No physical LCR meter detected")
                print("   Tried: USB/GPIB and Ethernet (LAN)")
                print("   Set LCR_IP or LCR_MAC environment variable for Ethernet")
                print("   Example: $env:LCR_IP = '169.254.157.92'")
                print("   Running in simulation mode (limited tests)")
                return False
        
        print("✓ Connected to LCR meter")
        return True
    
    def teardown(self):
        """Cleanup after tests"""
        if self.meter:
            self.meter.close()
        
        # Print summary
        print("\n" + "="*70)
        print("TEST SUMMARY")
        print("="*70)
        print(f"✓ Passed: {self.tests_passed}")
        print(f"✗ Failed: {self.tests_failed}")
        print(f"Total: {self.tests_passed + self.tests_failed}")
        
        if self.tests_failed == 0:
            print("\n🎉 All tests passed!")
        else:
            print(f"\n⚠️  {self.tests_failed} tests failed")
    
    def run_test(self, test_name: str, test_func):
        """Run a single test with error handling"""
        print(f"\n▶ Testing: {test_name}")
        try:
            result = test_func()
            if result:
                print(f"  ✓ {test_name} passed")
                self.tests_passed += 1
            else:
                print(f"  ✗ {test_name} failed")
                self.tests_failed += 1
            return result
        except Exception as e:
            print(f"  ✗ {test_name} failed with error: {e}")
            self.tests_failed += 1
            return False
    
    # --- Individual Tests ---
    
    def test_connection(self):
        """Test 1: Connection and identification"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True  # Don't fail if no device
        
        try:
            idn = self.meter.instrument.query('*IDN?')
            print(f"  - Device: {idn.strip()}")
            return 'E4980' in idn
        except:
            return False
    
    def test_configuration(self):
        """Test 2: Configuration with different parameters"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True
        
        configs = [
            MeasurementConfig(frequency=1000, voltage=1.0, function=MeasurementFunction.CPD),
            MeasurementConfig(frequency=10000, voltage=0.5, function=MeasurementFunction.LSRS),
            MeasurementConfig(frequency=100000, voltage=2.0, function=MeasurementFunction.RX),
        ]
        
        for i, config in enumerate(configs, 1):
            print(f"  - Config {i}: {config.frequency}Hz, {config.voltage}V, {config.function.value}")
            if not self.meter.configure(config):
                return False
            time.sleep(0.1)
        
        return True
    
    def test_single_reading(self):
        """Test 3: Single measurement reading"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True
        
        # Configure for capacitance measurement with maximum speed
        config = MeasurementConfig(
            frequency=1000,
            voltage=1.0,
            function=MeasurementFunction.CPD,
            averaging=1,              # No averaging for max speed
            integration_time='SHOR'   # Shortest integration time
        )
        
        if not self.meter.configure(config):
            return False
        
        # Wait for stabilization
        time.sleep(0.5)
        
        # Read single measurement
        result = self.meter.read_single()
        if result:
            print(f"  - Measurement: {self.meter.format_result(result)}")
            return result.is_valid
        
        return False
    
    def test_burst_reading(self):
        """Test 4: Burst measurement for speed testing"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True
        
        # Configure for MAXIMUM speed
        config = MeasurementConfig(
            frequency=1000,
            voltage=1.0,
            function=MeasurementFunction.CPD,
            averaging=1,              # No averaging
            integration_time='SHOR'   # Shortest integration
        )
        
        if not self.meter.configure(config):
            return False
        
        # Disable display for speed boost
        try:
            self.meter.instrument.write(':DISP:ENAB OFF')
            print("  - Display disabled for max speed")
        except:
            pass
        
        time.sleep(0.2)  # Settle
        
        # Measure reading rate
        print("  - Testing burst mode (100 readings)...")
        start = time.time()
        results = self.meter.read_burst(100)
        duration = time.time() - start
        
        # Re-enable display
        try:
            self.meter.instrument.write(':DISP:ENAB ON')
        except:
            pass
        
        valid_count = sum(1 for r in results if r.is_valid)
        rate = len(results) / duration if duration > 0 else 0
        
        print(f"  - Readings: {len(results)}")
        print(f"  - Valid: {valid_count}")
        print(f"  - Duration: {duration:.2f}s")
        print(f"  - Rate: {rate:.1f} readings/sec (MAX SPEED)")
        
        return len(results) > 0
    
    def test_continuous_with_callback(self):
        """Test 5: Continuous measurement with callback"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True
        
        # Configure for maximum speed
        config = MeasurementConfig(
            frequency=1000,
            voltage=1.0,
            function=MeasurementFunction.CPD,
            averaging=1,
            integration_time='SHOR'
        )
        
        if not self.meter.configure(config):
            return False
        
        # Define callback that stops after 10 readings
        readings_received = []
        
        def test_callback(result: MeasurementResult) -> bool:
            readings_received.append(result)
            if result.reading_number <= 3:  # Print first 3
                print(f"  - Callback: {self.meter.format_result(result)}")
            return result.reading_number < 10  # Stop after 10
        
        print("  - Testing continuous mode with callback...")
        results = self.meter.read_continuous(callback=test_callback)
        
        print(f"  - Total readings: {len(results)}")
        print(f"  - Callback received: {len(readings_received)}")
        
        return len(results) == 10 and len(readings_received) == 10
    
    def test_measurement_functions(self):
        """Test 6: Different measurement functions"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True
        
        functions = [
            MeasurementFunction.CPD,
            MeasurementFunction.LSRS,
            MeasurementFunction.RX,
        ]
        
        for func in functions:
            config = MeasurementConfig(
                frequency=1000,
                voltage=1.0,
                function=func
            )
            
            if not self.meter.configure(config):
                return False
            
            time.sleep(0.2)
            result = self.meter.read_single()
            
            if result:
                print(f"  - {func.value}: {self.meter.format_result(result)}")
            else:
                return False
        
        return True
    
    def test_error_handling(self):
        """Test 7: Error handling and recovery"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True
        
        print("  - Testing invalid configuration handling...")
        
        # Test invalid frequency (out of range)
        try:
            config = MeasurementConfig(frequency=10)  # Too low
            self.meter.configure(config)
            # Should handle gracefully
        except Exception as e:
            print(f"  - Caught expected error: {e}")
        
        # Verify meter still works after error
        config = MeasurementConfig(frequency=1000)
        if self.meter.configure(config):
            result = self.meter.read_single()
            if result:
                print("  - Recovery successful")
                return True
        
        return False
    
    def test_context_manager(self):
        """Test 8: Context manager functionality"""
        print("  - Testing context manager...")
        
        try:
            with LCRMeter() as temp_meter:
                # Try Ethernet if auto-connect failed
                if not temp_meter.instrument:
                    lcr_ip = os.environ.get('LCR_IP', None)
                    lcr_mac = os.environ.get('LCR_MAC', '80.09.02.18.62.03')
                    temp_meter.connect_ethernet(ip=lcr_ip, mac=lcr_mac, protocol='vxi11')
                
                if temp_meter.instrument:
                    result = temp_meter.read_single()
                    print(f"  - Read in context: {result is not None}")
                else:
                    print("  - No device for context manager test")
            print("  - Context manager closed properly")
            return True
        except Exception as e:
            print(f"  - Context manager failed: {e}")
            return False
    
    def test_quick_functions(self):
        """Test 9: Quick measurement convenience functions"""
        print("  - Testing quick measurement functions...")
        
        try:
            # Test quick_measure (will fail gracefully if no device)
            print("  - Attempting quick_measure...")
            results = quick_measure(
                function=MeasurementFunction.CPD,
                frequency=1000,
                voltage=1.0,
                count=5
            )
            
            if results:
                print(f"  - Quick measure got {len(results)} readings")
                for r in results[:2]:  # Show first 2
                    print(f"    • C={r.primary:.3e}F, D={r.secondary:.4f}")
            else:
                print("  - No device available for quick measure")
            
            return True
            
        except Exception as e:
            if "Not connected" in str(e):
                print("  - No device (expected)")
                return True
            return False
    
    def test_performance_benchmark(self):
        """Test 10: Performance benchmark - Tests MAXIMUM speed optimization"""
        if not self.meter.instrument:
            print("  - Skipped (no device)")
            return True
        
        print("  - Running MAXIMUM SPEED performance benchmark...")
        print("  - Creating fresh connection for benchmark...")
        
        # Save the resource name and create fresh connection
        try:
            resource_string = self.meter.instrument.resource_name
        except:
            print("  - Cannot get resource name, skipping")
            return True
        
        # Close old connection and create fresh one
        self.meter.close()
        time.sleep(1.0)
        
        # Create new meter instance
        self.meter = LCRMeter(resource_string=resource_string)
        if not self.meter.instrument:
            print("  - Failed to create fresh connection")
            return False
        
        print("  - Fresh connection established")
        
        # Configure once with SHORT integration time and NO averaging
        base_config = MeasurementConfig(
            frequency=1000,
            voltage=1.0,
            function=MeasurementFunction.CPD,
            averaging=1,              # No averaging for max speed
            integration_time='SHOR'   # Shortest integration
        )
        
        if not self.meter.configure(base_config):
            print("  - Configuration failed")
            return False
        
        time.sleep(1.0)  # Let it stabilize after reset
        
        # Test 1: Normal speed (display on)
        print("\n  - Test 1: Display ON (normal)")
        try:
            self.meter.instrument.write(':DISP:ENAB ON')
            time.sleep(0.3)
            start = time.time()
            results = self.meter.read_burst(100)
            duration = time.time() - start
            rate = len(results) / duration if duration > 0 else 0
            print(f"    Display ON:  {rate:.1f} readings/sec ({len(results)} readings in {duration:.2f}s)")
        except Exception as e:
            print(f"    Failed - {e}")
            return False
        
        # Test 2: MAXIMUM speed (display off)
        print("\n  - Test 2: Display OFF (maximum speed optimization)")
        try:
            self.meter.instrument.write(':DISP:ENAB OFF')
            time.sleep(0.3)
            start = time.time()
            results = self.meter.read_burst(100)
            duration = time.time() - start
            rate = len(results) / duration if duration > 0 else 0
            print(f"    Display OFF: {rate:.1f} readings/sec ({len(results)} readings in {duration:.2f}s) ⚡ MAX")
        except Exception as e:
            print(f"    Failed - {e}")
        finally:
            # Re-enable display
            try:
                self.meter.instrument.write(':DISP:ENAB ON')
            except:
                pass
        
        # Test 3: Compare integration times
        print("\n  - Test 3: Integration time comparison (display on)")
        integration_times = ['SHOR', 'MED', 'LONG']
        
        for int_time in integration_times:
            try:
                self.meter.instrument.write(f':APER {int_time}')
                time.sleep(0.5)
                
                start = time.time()
                results = self.meter.read_burst(50)
                duration = time.time() - start
                rate = len(results) / duration if duration > 0 else 0
                
                print(f"    {int_time:4s}: {rate:.1f} readings/sec ({len(results)} readings in {duration:.2f}s)")
                
            except Exception as e:
                print(f"    {int_time:4s}: Failed - {e}")
                return False
        
        print("\n  💡 TIP: For absolute maximum speed, use SHORT integration + Display OFF")
        return True


    
    def run_all_tests(self):
        """Run all tests"""
        tests = [
            ("Connection", self.test_connection),
            ("Configuration", self.test_configuration),
            ("Single Reading", self.test_single_reading),
            ("Burst Reading", self.test_burst_reading),
            ("Continuous with Callback", self.test_continuous_with_callback),
            ("Measurement Functions", self.test_measurement_functions),
            ("Error Handling", self.test_error_handling),
            ("Context Manager", self.test_context_manager),
            ("Quick Functions", self.test_quick_functions),
            ("Performance Benchmark", self.test_performance_benchmark),
        ]
        
        for name, test_func in tests:
            self.run_test(name, test_func)


def demo_usage():
    """Demonstrate basic usage of the module"""
    print("\n" + "="*70)
    print("USAGE DEMONSTRATION")
    print("="*70)
    
    print("\n1. Basic Usage with Context Manager:")
    print("-"*40)
    print("""
with LCRMeter() as meter:
    config = MeasurementConfig(
        frequency=1000,
        voltage=1.0,
        function=MeasurementFunction.CPD
    )
    meter.configure(config)
    
    # Single reading
    result = meter.read_single()
    print(meter.format_result(result))
    
    # Burst of 100 readings
    results = meter.read_burst(100)
    """)
    
    print("\n2. Continuous Measurement with Callback:")
    print("-"*40)
    print("""
def my_callback(result):
    print(f"C={result.primary:.3e}F")
    return result.reading_number < 100  # Stop after 100
    
with LCRMeter() as meter:
    meter.configure(config)
    results = meter.read_continuous(callback=my_callback)
    """)
    
    print("\n3. Quick Measurement:")
    print("-"*40)
    print("""
from lcr_meter import quick_measure, MeasurementFunction

results = quick_measure(
    function=MeasurementFunction.CPD,
    frequency=1000,
    voltage=1.0,
    count=100
)

for r in results:
    print(f"C={r.primary:.3e}F, D={r.secondary:.4f}")
    """)
    
    print("\n4. Integration with Larger Project:")
    print("-"*40)
    print("""
class MySystem:
    def __init__(self):
        self.lcr = LCRMeter()
        self.lcr.configure(MeasurementConfig(
            frequency=10000,
            function=MeasurementFunction.LSRS
        ))
    
    def measure_inductance(self):
        result = self.lcr.read_single()
        return result.primary if result else None
    
    def cleanup(self):
        self.lcr.close()
    """)


def main():
    """Main test runner"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Test LCR Meter Module',
        epilog='Environment variables: LCR_IP (e.g., 169.254.157.92) or LCR_MAC (e.g., 80.09.02.18.62.03) for Ethernet connection'
    )
    parser.add_argument('--demo', action='store_true', help='Show usage demonstration')
    parser.add_argument('--quick', action='store_true', help='Run quick test only')
    parser.add_argument('--bench', action='store_true', help='Run performance benchmark')
    args = parser.parse_args()
    
    if args.demo:
        demo_usage()
        return
    
    # Create test instance
    tester = TestLCRMeter()
    
    try:
        # Setup
        has_device = tester.setup()
        
        if args.quick:
            # Quick test - just try to connect and read
            if has_device:
                tester.run_test("Quick Connection Test", tester.test_connection)
                tester.run_test("Quick Reading Test", tester.test_single_reading)
            else:
                print("\nNo device found - cannot run quick test")
        
        elif args.bench:
            # Performance benchmark only
            if has_device:
                tester.run_test("Performance Benchmark", tester.test_performance_benchmark)
            else:
                print("\nNo device found - cannot run benchmark")
        
        else:
            # Run all tests
            tester.run_all_tests()
        
    finally:
        # Cleanup
        tester.teardown()
    
    # Return exit code based on test results
    return 0 if tester.tests_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
