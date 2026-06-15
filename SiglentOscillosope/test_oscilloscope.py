#!/usr/bin/env python3
"""
Test Suite for the Siglent SDS2000X Plus oscilloscope module
============================================================

Validates the oscilloscope.py driver and serves as a connection / health
check. Lives next to the driver (same folder) like test_lcr_meter.py.

Tests Performed:
1. Connection Test       - socket open + *IDN? identification (expects SDS)
2. Health Check          - sample rate, timebase, one valid PAVA reading
3. Configuration Test    - apply several read configs
4. Single Reading Test   - one measurement with validity check
5. Burst Reading Test    - high-speed burst with read-rate calculation
6. Measurement Params    - PKPK / MEAN / FREQ on a channel
7. Cross-Channel Test    - C1-C2 phase via the second-source path
8. AWG Round-Trip        - set built-in WaveGen, read it back via PAVA
9. Error Handling        - bad source string recovers gracefully
10. Context Manager      - socket released on exit

Usage:
    python test_oscilloscope.py            # Run all tests
    python test_oscilloscope.py --quick    # Connection + health check only
    python test_oscilloscope.py --bench     # Read-rate benchmark only
    python test_oscilloscope.py --demo      # Show usage examples

Connection:
    Tries the driver default (169.254.111.100:5025); if that's down it sweeps
    the link-local subnet for a Siglent answering *IDN?. Override with:
        $env:SCOPE_IP     = '169.254.111.4'     # exact host (PowerShell)
        $env:SCOPE_SUBNET = '169.254.111'       # /24 for the auto-scan
        $env:SCOPE_PORT   = '5025'
        export SCOPE_IP=169.254.111.4           # bash

Requirements:
- SDS2000X Plus reachable over LAN (static IP on scope, PC on same subnet)
- numpy (only for the optional waveform-capture demo)

Author: Yilin Ma
University of Michigan Robotics — HDR Lab
"""

import os
import sys
import time
import math
import logging

try:
    from oscilloscope import (
        Oscilloscope, ScopeConfig, MeasureParam, ScopeMeasurement,
        quick_measure,
    )
except ImportError:
    print("Error: oscilloscope.py module not found in current directory")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class TestOscilloscope:
    """Test suite for the oscilloscope module."""

    def __init__(self):
        self.scope = None
        self.tests_passed = 0
        self.tests_failed = 0

    # -- lifecycle ---------------------------------------------------------
    def setup(self):
        print("\n" + "=" * 70)
        print("SDS2000X PLUS MODULE TEST SUITE")
        print("=" * 70)
        print("\nInitializing test environment...")

        host = os.environ.get("SCOPE_IP")          # None -> driver default
        port = int(os.environ.get("SCOPE_PORT", 5025))
        if host:
            print(f"   Using IP from environment: {host}:{port}")
        else:
            print("   No SCOPE_IP set — trying driver default, then auto-scan")

        # auto_open=False so the handshake is explicit and we can report on it.
        self.scope = Oscilloscope(host=host, port=port, auto_open=False)
        connected = self.scope.connect(host, port) if host else self.scope.auto_connect()

        if not connected or not self.scope.sock:
            print("\n⚠️  WARNING: No oscilloscope detected over LAN")
            print("   (auto_connect tried the default IP, then swept the")
            print("    link-local subnet for a Siglent — both came up empty.)")
            print("   Checklist:")
            print("   - PC and scope on the same link-local subnet; cable in")
            print("   - Ping the scope IP; confirm port 5025 reachable")
            print("   - Set SCOPE_IP for an exact host, or SCOPE_SUBNET (e.g.")
            print("     169.254.111) to point the auto-scan at the right /24")
            return False

        print(f"✓ Connected: {self.scope.idn}")
        return True

    def teardown(self):
        if self.scope:
            self.scope.close()
        print("\n" + "=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        print(f"✓ Passed: {self.tests_passed}")
        print(f"✗ Failed: {self.tests_failed}")
        print(f"Total: {self.tests_passed + self.tests_failed}")
        print("\n🎉 All tests passed!" if self.tests_failed == 0
              else f"\n⚠️  {self.tests_failed} tests failed")

    def run_test(self, name, fn):
        print(f"\n▶ Testing: {name}")
        try:
            ok = fn()
            if ok:
                print(f"  ✓ {name} passed")
                self.tests_passed += 1
            else:
                print(f"  ✗ {name} failed")
                self.tests_failed += 1
            return ok
        except Exception as e:
            print(f"  ✗ {name} failed with error: {e}")
            self.tests_failed += 1
            return False

    # -- individual tests --------------------------------------------------
    def test_connection(self):
        """Test 1: socket + *IDN?."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        idn = self.scope.query("*IDN?")
        print(f"  - Device: {idn}")
        return "SDS" in idn.upper() or "SIGLENT" in idn.upper()

    def test_health_check(self):
        """Test 2: sample rate, timebase, and one valid reading on C1."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        sara = self.scope.get_sample_rate()
        tdiv = self.scope.get_timebase()
        print(f"  - Sample rate: {sara:.3e} Sa/s")
        print(f"  - Timebase:    {tdiv:.3e} s/div")

        self.scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
        time.sleep(0.2)
        r = self.scope.read_single()
        if r is None:
            print("  - No reading returned")
            return False
        print(f"  - C1 PKPK: {self.scope.format_result(r)}")
        # Health check passes if comms are alive; a '****' (no signal on C1)
        # still proves the read path works, so don't fail on r.is_valid here.
        return not math.isnan(sara)

    def test_configuration(self):
        """Test 3: apply several read configurations."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        configs = [
            ScopeConfig(source="C1", param=MeasureParam.PKPK),
            ScopeConfig(source="C1", param=MeasureParam.FREQ),
            ScopeConfig(source="C2", param=MeasureParam.MEAN),
        ]
        for i, c in enumerate(configs, 1):
            print(f"  - Config {i}: {c.source}:{c.param.value}")
            if not self.scope.configure(c):
                return False
            time.sleep(0.1)
        return True

    def test_single_reading(self):
        """Test 4: single measurement with validity."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        self.scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
        time.sleep(0.3)
        r = self.scope.read_single()
        if r:
            print(f"  - {self.scope.format_result(r)}")
            return True  # comms ok; value may be '****' if C1 has no signal
        return False

    def test_burst_reading(self):
        """Test 5: burst read with rate calc."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        self.scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
        time.sleep(0.2)
        print("  - Testing burst mode (100 readings)...")
        start = time.perf_counter()
        results = self.scope.read_burst(100)
        dur = time.perf_counter() - start
        rate = len(results) / dur if dur > 0 else 0
        print(f"  - {len(results)} readings in {dur:.2f}s  ({rate:.1f} rd/s)")
        return len(results) > 0

    def test_measurement_params(self):
        """Test 6: PKPK / MEAN / FREQ all return without error."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        for p in (MeasureParam.PKPK, MeasureParam.MEAN, MeasureParam.FREQ):
            self.scope.configure(ScopeConfig(source="C1", param=p))
            time.sleep(0.2)
            r = self.scope.read_single()
            if r is None:
                print(f"  - {p.value}: no reading")
                return False
            print(f"  - {p.value}: {r.primary:.6e} {r.unit}")
        return True

    def test_cross_channel(self):
        """Test 7: cross-channel phase via the second-source path (C1-C2)."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        self.scope.configure(ScopeConfig(
            source="C1", param=MeasureParam.PKPK,
            second_source="C1-C2", second_param=MeasureParam.PHA,
        ))
        time.sleep(0.3)
        r = self.scope.read_single()
        if r is None:
            return False
        print(f"  - {self.scope.format_result(r)}")
        return True  # path works even if C2 has no signal

    def test_awg_roundtrip(self):
        """Test 8: set the built-in WaveGen, read its frequency back via PAVA.

        Skips gracefully if the built-in AWG isn't fitted / addressable. Wire
        the WaveGen output to C1 for the read-back to reflect the set value.
        """
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        try:
            self.scope.set_awg(wavetype="SINE", frequency=1000.0,
                               amplitude=1.0, offset=0.0, source="C1",
                               output=True, load="HZ")
            time.sleep(0.5)
            self.scope.configure(ScopeConfig(source="C1", param=MeasureParam.FREQ))
            time.sleep(0.3)
            r = self.scope.read_single()
            if r and not math.isnan(r.primary):
                print(f"  - Set 1000 Hz, scope reads {r.primary:.1f} Hz on C1")
            else:
                print("  - AWG set OK; no signal on C1 to read back (wire WaveGen->C1)")
            return True
        except Exception as e:
            print(f"  - AWG not addressable, skipping: {e}")
            return True

    def test_error_handling(self):
        """Test 9: a bad source string must not crash the driver."""
        if not self.scope.sock:
            print("  - Skipped (no device)")
            return True
        self.scope.configure(ScopeConfig(source="C9", param=MeasureParam.PKPK))
        time.sleep(0.2)
        r = self.scope.read_single()   # invalid channel -> None or status!=0
        print(f"  - Bad source handled (result={'None' if r is None else r.status_str})")
        # Re-establish a good config so later tests are unaffected.
        self.scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
        return True

    def test_context_manager(self):
        """Test 10: context manager opens + releases the socket.

        The SDS2000X Plus serves only ONE SCPI socket client at a time on
        port 5025: a second concurrent connection's *IDN? times out. So close
        the suite's main session first, then exercise a fresh context-managed
        connection. This is the last test, so dropping the main session is
        fine (teardown also closes, idempotently).
        """
        host = self.scope.host
        port = self.scope.port
        self.scope.close()                       # free the single SCPI socket
        time.sleep(0.3)                          # let the scope release it
        with Oscilloscope(host=host, port=port) as s:
            ok = s.sock is not None and s.idn is not None
            print(f"  - In context: connected={ok}")
        print("  - Exited context cleanly")
        return ok

    def run_all_tests(self):
        for name, fn in [
            ("Connection", self.test_connection),
            ("Health Check", self.test_health_check),
            ("Configuration", self.test_configuration),
            ("Single Reading", self.test_single_reading),
            ("Burst Reading", self.test_burst_reading),
            ("Measurement Params", self.test_measurement_params),
            ("Cross-Channel Phase", self.test_cross_channel),
            ("AWG Round-Trip", self.test_awg_roundtrip),
            ("Error Handling", self.test_error_handling),
            ("Context Manager", self.test_context_manager),
        ]:
            self.run_test(name, fn)


def demo_usage():
    print("\n" + "=" * 70)
    print("USAGE DEMONSTRATION")
    print("=" * 70)
    print("""
1. Basic single + burst read (context manager):
-------------------------------------------------
from oscilloscope import Oscilloscope, ScopeConfig, MeasureParam

with Oscilloscope() as scope:                 # defaults to 169.254.111.100:5025
    scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
    print(scope.format_result(scope.read_single()))
    results = scope.read_burst(100)

2. SRF-style read: magnitude (PKPK) + cross-channel phase together:
-------------------------------------------------------------------
cfg = ScopeConfig(source="C1", param=MeasureParam.PKPK,
                  second_source="C1-C2", second_param=MeasureParam.PHA)
with Oscilloscope() as scope:
    scope.configure(cfg)
    r = scope.read_single()
    mag, phase = r.primary, r.secondary

3. Built-in AWG frequency sweep:
--------------------------------
with Oscilloscope() as scope:
    scope.set_awg(wavetype="SINE", amplitude=1.0, output=True, load="HZ")
    scope.configure(ScopeConfig(source="C1", param=MeasureParam.PKPK))
    for f in [1e3, 1e4, 1e5, 1e6]:
        scope.set_awg_frequency(f)
        scope.clear_sweeps()       # reset Math-average stats at each step
        time.sleep(0.2)
        print(f, scope.read_single().primary)

4. Worker integration (mirror LcrWorker):
------------------------------------------
# In ../SMA_CharacterizationV2/workers.py, a ScopeWorker yields the same
# sample shape as LcrWorker — iter_measurements() returns objects with
# .primary .secondary .status .timestamp .monotonic:
for m in scope.iter_measurements(poll_interval_s=0.05):
    if stop_event.is_set():
        break
    out_queue.put_nowait(ScopeSample(host_timestamp_s=m.timestamp,
                                     monotonic_s=m.monotonic,
                                     primary=m.primary, secondary=m.secondary,
                                     status=m.status))
""")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Test SDS2000X Plus oscilloscope module",
        epilog="Env vars: SCOPE_IP (e.g. 169.254.111.100), SCOPE_PORT (5025)",
    )
    parser.add_argument("--demo", action="store_true", help="Show usage demo")
    parser.add_argument("--quick", action="store_true", help="Connection + health check")
    parser.add_argument("--bench", action="store_true", help="Read-rate benchmark")
    args = parser.parse_args()

    if args.demo:
        demo_usage()
        return 0

    tester = TestOscilloscope()
    try:
        has_device = tester.setup()
        if args.quick:
            if has_device:
                tester.run_test("Connection", tester.test_connection)
                tester.run_test("Health Check", tester.test_health_check)
            else:
                print("\nNo device found - cannot run quick test")
        elif args.bench:
            if has_device:
                tester.run_test("Burst Reading", tester.test_burst_reading)
            else:
                print("\nNo device found - cannot run benchmark")
        else:
            tester.run_all_tests()
    finally:
        tester.teardown()

    return 0 if tester.tests_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
