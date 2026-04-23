#!/usr/bin/env python3
"""
Comprehensive Test Suite for Zaber Linear Stage Module
======================================================

This test suite validates all functionality of the zaber_stage module including
connection, motion control, position reading, and safety features.

Tests Performed:
1. Connection Test - Verify device connection and communication
2. Homing Test - Stage homing and home position verification
3. Position Reading Test - High-speed position reading at 100 Hz
4. Absolute Movement Test - Precise position moves with error checking
5. Velocity Control Test - Continuous velocity control in both directions
6. Position Limits Test - Software position limit enforcement
7. Status Reporting Test - Real-time status information retrieval
8. Stop Command Test - Emergency stop functionality
9. Performance Benchmark - Speed and response time measurements
10. Interactive Mode - Manual control for hands-on testing

Requirements:
- Physical Zaber linear stage connected via USB/serial
- zaber-motion library installed (pip install zaber-motion)
- Stage must be homed before running movement tests

Usage:
    python test_zaber_stage.py    # Run full test suite with interactive mode

Author: Yilin Ma
Date: November 2025
University of Michigan Robotics
HDR Lab
"""

import time
import logging
import sys
from typing import List

# Import the stage module
try:
    from zaber_stage import ZaberStage, create_stage
except ImportError as e:
    print("Error: Could not import zaber_stage module")
    print(f"Details: {e}")
    print("Make sure zaber_stage.py is in the same directory or in PYTHONPATH")
    print("And that zaber-motion is installed: pip install zaber-motion")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("TestScript")


class TestResults:
    """Track test results"""
    def __init__(self):
        self.passed = []
        self.failed = []
    
    def add_pass(self, test_name: str):
        self.passed.append(test_name)
        logger.info(f"✅ PASSED: {test_name}")
    
    def add_fail(self, test_name: str, error: str):
        self.failed.append((test_name, error))
        logger.error(f"❌ FAILED: {test_name} - {error}")
    
    def print_summary(self):
        print("\n" + "="*60)
        print("TEST SUMMARY")
        print("="*60)
        print(f"Passed: {len(self.passed)}/{len(self.passed) + len(self.failed)}")
        
        if self.passed:
            print("\n✅ Passed Tests:")
            for test in self.passed:
                print(f"  - {test}")
        
        if self.failed:
            print("\n❌ Failed Tests:")
            for test, error in self.failed:
                print(f"  - {test}: {error}")
        
        print("="*60)


def test_connection(stage: ZaberStage, results: TestResults):
    """Test basic connection"""
    try:
        assert stage.is_connected(), "Stage not connected"
        results.add_pass("Connection Test")
    except AssertionError as e:
        results.add_fail("Connection Test", str(e))


def test_homing(stage: ZaberStage, results: TestResults):
    """Test homing functionality"""
    try:
        logger.info("Testing homing...")
        success = stage.home()
        assert success, "Homing command failed"
        
        # Wait for homing to complete
        time.sleep(2.0)
        
        assert stage.is_homed(), "Stage not homed after homing"
        position = stage.get_position()
        logger.info(f"Position after homing: {position:.3f} mm")
        
        results.add_pass("Homing Test")
    except AssertionError as e:
        results.add_fail("Homing Test", str(e))


def test_position_reading(stage: ZaberStage, results: TestResults):
    """Test high-speed position reading"""
    try:
        logger.info("Testing position reading rate...")
        
        positions = []
        timestamps = []
        
        # Read positions for 1 second
        start_time = time.time()
        while time.time() - start_time < 1.0:
            position = stage.get_position()
            positions.append(position)
            timestamps.append(time.time())
            time.sleep(0.01)  # 100 Hz sampling
        
        # Calculate actual reading rate
        num_readings = len(positions)
        duration = timestamps[-1] - timestamps[0]
        reading_rate = num_readings / duration
        
        logger.info(f"Collected {num_readings} readings in {duration:.3f} seconds")
        logger.info(f"Effective reading rate: {reading_rate:.1f} Hz")
        
        assert num_readings > 50, f"Too few readings: {num_readings}"
        assert reading_rate > 50, f"Reading rate too low: {reading_rate:.1f} Hz"
        
        results.add_pass("Position Reading Test")
    except AssertionError as e:
        results.add_fail("Position Reading Test", str(e))


def test_absolute_movement(stage: ZaberStage, results: TestResults):
    """Test absolute position movement"""
    try:
        if not stage.is_homed():
            logger.warning("Stage not homed, skipping absolute movement test")
            return
        
        logger.info("Testing absolute movement...")
        
        test_positions = [10.0, 20.0, 15.0, 5.0, 0.0]
        
        for target_pos in test_positions:
            logger.info(f"Moving to {target_pos:.1f} mm...")
            success = stage.move_to(target_pos)
            assert success, f"Move command failed for {target_pos} mm"
            
            # Give command time to be processed
            time.sleep(0.1)
            
            # Wait for movement to complete
            timeout = 10.0
            start_time = time.time()
            while stage.is_moving() and (time.time() - start_time) < timeout:
                current_pos = stage.get_position()
                logger.debug(f"Current position: {current_pos:.3f} mm")
                time.sleep(0.1)
            
            # Wait a bit more to ensure position is stable
            time.sleep(0.2)
            
            # Check final position
            final_pos = stage.get_position()
            error = abs(final_pos - target_pos)
            logger.info(f"Final position: {final_pos:.3f} mm (error: {error:.3f} mm)")
            
            assert error < 1.0, f"Position error too large: {error:.3f} mm"
        
        results.add_pass("Absolute Movement Test")
    except AssertionError as e:
        results.add_fail("Absolute Movement Test", str(e))


def test_velocity_control(stage: ZaberStage, results: TestResults):
    """Test velocity control"""
    try:
        if not stage.is_homed():
            logger.warning("Stage not homed, skipping velocity control test")
            return
        
        logger.info("Testing velocity control...")
        
        # Move to middle position first
        stage.move_to(20.0)
        time.sleep(2.0)
        
        # Test forward velocity
        logger.info("Testing forward velocity (2 mm/s)...")
        start_pos = stage.get_position()
        stage.set_velocity(2.0)
        time.sleep(2.0)
        stage.stop()
        end_pos = stage.get_position()
        distance = end_pos - start_pos
        logger.info(f"Moved {distance:.3f} mm in 2 seconds (expected ~4 mm)")
        assert 3.0 < distance < 5.0, f"Unexpected distance: {distance:.3f} mm"
        
        # Test reverse velocity
        logger.info("Testing reverse velocity (-2 mm/s)...")
        start_pos = stage.get_position()
        stage.set_velocity(-2.0)
        time.sleep(2.0)
        stage.stop()
        end_pos = stage.get_position()
        distance = start_pos - end_pos
        logger.info(f"Moved {distance:.3f} mm in 2 seconds (expected ~4 mm)")
        assert 3.0 < distance < 5.0, f"Unexpected distance: {distance:.3f} mm"
        
        results.add_pass("Velocity Control Test")
    except AssertionError as e:
        results.add_fail("Velocity Control Test", str(e))


def test_position_limits(stage: ZaberStage, results: TestResults):
    """Test position limit enforcement"""
    try:
        if not stage.is_homed():
            logger.warning("Stage not homed, skipping position limits test")
            return
        
        logger.info("Testing position limits...")
        
        # Try to move beyond max limit
        logger.info("Testing maximum limit...")
        success = stage.move_to(150.0)  # Beyond 100mm limit
        assert success, "Move command failed"
        time.sleep(2.0)
        position = stage.get_position()
        assert position <= 100.0, f"Position exceeded max limit: {position:.3f} mm"
        logger.info(f"Position clamped to: {position:.3f} mm")
        
        # Try to move beyond min limit
        logger.info("Testing minimum limit...")
        success = stage.move_to(-10.0)  # Beyond 0mm limit
        assert success, "Move command failed"
        time.sleep(2.0)
        position = stage.get_position()
        assert position >= 0.0, f"Position exceeded min limit: {position:.3f} mm"
        logger.info(f"Position clamped to: {position:.3f} mm")
        
        results.add_pass("Position Limits Test")
    except AssertionError as e:
        results.add_fail("Position Limits Test", str(e))


def test_status_reporting(stage: ZaberStage, results: TestResults):
    """Test status reporting functionality"""
    try:
        logger.info("Testing status reporting...")
        
        status = stage.get_status()
        
        # Check status fields
        assert hasattr(status, 'position_mm'), "Missing position_mm field"
        assert hasattr(status, 'velocity_mm_s'), "Missing velocity_mm_s field"
        assert hasattr(status, 'is_moving'), "Missing is_moving field"
        assert hasattr(status, 'is_homed'), "Missing is_homed field"
        assert hasattr(status, 'timestamp'), "Missing timestamp field"
        
        logger.info(f"Status: Position={status.position_mm:.3f}mm, "
                   f"Velocity={status.velocity_mm_s:.3f}mm/s, "
                   f"Moving={status.is_moving}, Homed={status.is_homed}")
        
        # Test distance from home
        distance = stage.get_distance_from_home()
        assert distance >= 0, f"Distance should be positive: {distance}"
        logger.info(f"Distance from home: {distance:.3f} mm")
        
        results.add_pass("Status Reporting Test")
    except AssertionError as e:
        results.add_fail("Status Reporting Test", str(e))


def test_stop_command(stage: ZaberStage, results: TestResults):
    """Test emergency stop functionality"""
    try:
        if not stage.is_homed():
            logger.warning("Stage not homed, skipping stop command test")
            return
        
        logger.info("Testing stop command...")
        
        # Start moving
        stage.set_velocity(5.0)
        time.sleep(0.5)
        
        # Emergency stop
        success = stage.stop()
        assert success, "Stop command failed"
        
        # Check that movement stopped
        time.sleep(0.2)
        assert not stage.is_moving(), "Stage still moving after stop"
        
        # Verify velocity is zero
        status = stage.get_status()
        assert status.velocity_mm_s == 0.0, f"Velocity not zero: {status.velocity_mm_s}"
        
        results.add_pass("Stop Command Test")
    except AssertionError as e:
        results.add_fail("Stop Command Test", str(e))


def run_performance_test(stage: ZaberStage):
    """Run performance benchmarks"""
    print("\n" + "="*60)
    print("PERFORMANCE TEST")
    print("="*60)
    
    if not stage.is_homed():
        print("Stage not homed, skipping performance test")
        return
    
    # Test maximum reading rate
    print("\n1. Maximum Position Reading Rate Test")
    print("-" * 40)
    
    readings = []
    start_time = time.time()
    
    while time.time() - start_time < 1.0:
        readings.append(stage.get_position())
    
    print(f"Readings in 1 second: {len(readings)}")
    print(f"Effective rate: {len(readings):.1f} Hz")
    
    # Test response time
    print("\n2. Command Response Time Test")
    print("-" * 40)
    
    # Measure velocity command response
    start_time = time.time()
    stage.set_velocity(1.0)
    response_time = time.time() - start_time
    print(f"Velocity command time: {response_time*1000:.2f} ms")
    
    stage.stop()
    
    # Measure position command response
    start_time = time.time()
    stage.move_to(10.0)
    response_time = time.time() - start_time
    print(f"Position command time: {response_time*1000:.2f} ms")
    
    # Test position tracking during movement
    print("\n3. Position Tracking During Movement")
    print("-" * 40)
    
    stage.move_to(0.0)
    time.sleep(2.0)
    
    positions = []
    timestamps = []
    
    # Start movement and track position
    stage.set_velocity(5.0)
    start_time = time.time()
    
    while time.time() - start_time < 2.0:
        positions.append(stage.get_position())
        timestamps.append(time.time())
        time.sleep(0.01)
    
    stage.stop()
    
    # Calculate statistics
    num_samples = len(positions)
    duration = timestamps[-1] - timestamps[0]
    sample_rate = num_samples / duration
    distance_traveled = positions[-1] - positions[0]
    avg_velocity = distance_traveled / duration
    
    print(f"Samples collected: {num_samples}")
    print(f"Sampling rate: {sample_rate:.1f} Hz")
    print(f"Distance traveled: {distance_traveled:.3f} mm")
    print(f"Average velocity: {avg_velocity:.3f} mm/s")
    
    print("="*60)


def main():
    """Main test routine"""
    print("\n" + "="*60)
    print("ZABER LINEAR STAGE MODULE TEST")
    print("="*60)
    
    print("✅ Zaber Motion library available")
    
    # Initialize test results
    results = TestResults()
    
    # Create stage instance
    print("\nCreating stage instance...")
    stage = ZaberStage(
        port="auto",
        position_limit_mm=(0, 100),
        max_velocity_mm_s=10.0,
        reading_rate_hz=100.0
    )
    
    # Connect to stage
    print("Connecting to stage...")
    if not stage.connect():
        print("❌ Failed to connect to stage")
        return
    
    print("✅ Connected successfully")
    
    try:
        # Run tests
        print("\n" + "="*60)
        print("RUNNING FUNCTIONAL TESTS")
        print("="*60)
        
        test_connection(stage, results)
        test_homing(stage, results)
        test_position_reading(stage, results)
        test_absolute_movement(stage, results)
        test_velocity_control(stage, results)
        test_position_limits(stage, results)
        test_status_reporting(stage, results)
        test_stop_command(stage, results)
        
        # Print test summary
        results.print_summary()
        
        # Run performance test if all tests passed
        if not results.failed:
            run_performance_test(stage)
        
        # Interactive test mode
        print("\n" + "="*60)
        print("INTERACTIVE TEST MODE")
        print("="*60)
        print("Commands:")
        print("  h     - Home stage")
        print("  m<pos> - Move to position (e.g., m25.5)")
        print("  v<vel> - Set velocity (e.g., v2.0 or v-2.0)")
        print("  s     - Stop")
        print("  p     - Print current position")
        print("  status - Print full status")
        print("  q     - Quit")
        print("-"*60)
        
        while True:
            try:
                cmd = input("Enter command: ").strip().lower()
                
                if cmd == 'q':
                    break
                elif cmd == 'h':
                    stage.home()
                    print("Homing...")
                elif cmd == 's':
                    stage.stop()
                    print("Stopped")
                elif cmd == 'p':
                    print(f"Position: {stage.get_position():.3f} mm")
                elif cmd == 'status':
                    status = stage.get_status()
                    print(f"Position: {status.position_mm:.3f} mm")
                    print(f"Velocity: {status.velocity_mm_s:.3f} mm/s")
                    print(f"Moving: {status.is_moving}")
                    print(f"Homed: {status.is_homed}")
                elif cmd.startswith('m'):
                    try:
                        pos = float(cmd[1:])
                        stage.move_to(pos)
                        print(f"Moving to {pos:.3f} mm...")
                    except ValueError:
                        print("Invalid position")
                elif cmd.startswith('v'):
                    try:
                        vel = float(cmd[1:])
                        stage.set_velocity(vel)
                        print(f"Velocity set to {vel:.3f} mm/s")
                    except ValueError:
                        print("Invalid velocity")
                else:
                    print("Unknown command")
                    
            except KeyboardInterrupt:
                print("\nInterrupted")
                break
    
    finally:
        # Disconnect
        print("\nDisconnecting...")
        stage.disconnect()
        print("✅ Test complete")


if __name__ == "__main__":
    main()
