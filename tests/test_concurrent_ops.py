"""Test concurrent operations + signal integrity.

Run multiple udisksctl operations simultaneously and observe
whether signals arrive correctly for each.
"""

import asyncio
import json
import os
import subprocess
import threading
import time
import unittest

from tests.conftest import (
    LoopDevice,
    SignalCollector,
    ensure_dir,
    mts,
    print_system_info,
    udisksctl_available,
)

RESULT_DIR = os.path.join(os.path.dirname(__file__), '..', 'results')


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestConcurrentOperations(unittest.TestCase):
    """What happens when multiple operations run at the same time?"""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def setUp(self):
        self.collector = SignalCollector()
        asyncio.run(self.collector.start())

    def tearDown(self):
        asyncio.run(self.collector.stop())

    def test_concurrent_loop_setups(self):
        """Create 3 loop devices simultaneously."""
        print('\n  Creating 3 loop devices simultaneously...')
        self.collector.reset()

        devices = [LoopDevice() for _ in range(3)]
        for d in devices:
            self.addCleanup(d.cleanup)

        threads = []
        errors = []

        def _setup(d, idx):
            try:
                d.create()
                print(f'    thread {idx}: setup OK -> {d.device}')
            except Exception as e:
                errors.append((idx, str(e)))
                print(f'    thread {idx}: FAILED -> {e}')

        t0 = mts()
        for i, d in enumerate(devices):
            t = threading.Thread(target=_setup, args=(d, i))
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=30)
        dt = mts() - t0

        time.sleep(2)

        print(f'\n  Total time: {dt*1000:.0f}ms')
        print(f'  Total signals: {len(self.collector.signals)}')
        print(f'  Thread errors: {len(errors)}')

        # Count signals by type
        counts = self.collector.count_by_interface()
        for iface, count in sorted(counts.items()):
            print(f'    {count:3d}  {iface}')

        paths = {s['path'] for s in self.collector.signals}
        device_paths = {p for p in paths if 'block_devices' in p}
        print(f'  Device paths seen: {sorted(device_paths)}')
        print(f'  Expected devices: {[d.device_name for d in devices]}')

        # Report
        path = os.path.join(RESULT_DIR, 'concurrent_setups.json')
        data = {
            'operation': '3 concurrent loop-setups',
            'duration_ms': round(dt * 1000),
            'total_signals': len(self.collector.signals),
            'errors': len(errors),
            'device_paths': sorted(device_paths),
            'expected_devices': [d.device_name for d in devices],
            'counts': counts,
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

        # Clean up all devices
        for d in devices:
            d.delete()

    def test_setup_during_delete(self):
        """Create one device while deleting another."""
        dev1 = LoopDevice()
        dev2 = LoopDevice()
        self.addCleanup(dev1.cleanup)
        self.addCleanup(dev2.cleanup)

        try:
            dev1.create()
        except Exception as e:
            self.skipTest(f'loop-setup failed, UDisks2 not responding: {e}')
        time.sleep(1)
        self.collector.reset()

        errors = []

        def _setup():
            try:
                dev2.create()
            except Exception as e:
                errors.append(f'setup: {e}')

        def _delete():
            try:
                dev1.delete()
            except Exception as e:
                errors.append(f'delete: {e}')

        t0 = mts()
        t1 = threading.Thread(target=_setup)
        t2 = threading.Thread(target=_delete)
        t1.start()
        # Small delay so delete starts slightly after setup
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        dt = mts() - t0

        time.sleep(2)

        print(f'\n  Setup+Delete concurrently: {dt*1000:.0f}ms')
        print(f'  Errors: {errors if errors else "none"}')
        print(f'  Total signals: {len(self.collector.signals)}')
        paths = {s['path'] for s in self.collector.signals}
        device_paths = {p for p in paths if 'block_devices' in p}
        print(f'  Device paths seen: {sorted(device_paths)}')

        path = os.path.join(RESULT_DIR, 'setup_during_delete.json')
        data = {
            'operation': 'setup during delete',
            'duration_ms': round(dt * 1000),
            'total_signals': len(self.collector.signals),
            'errors': errors,
            'device_paths': sorted(device_paths),
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

        dev2.delete()

    def test_operation_during_handler_load(self):
        """Run ops while a handler does CPU work on each signal."""
        self.collector.reset()

        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        # Override the handler to do some work
        busy_count = []

        def _busy_handler(msg):
            # Simulate processing work
            total = sum(i for i in range(10000))
            busy_count.append(total)
            self.collector.record(msg)

        self.collector._bus.remove_message_handler(self.collector._handler)
        self.collector._bus.add_message_handler(_busy_handler)

        try:
            t0 = mts()
            dev.create()
            dt = (mts() - t0) * 1000
            time.sleep(1)

            print(f'\n  loop-setup with busy handler: {dt:.0f}ms')
            print(f'  Handler invocations: {len(busy_count)}')
            print(f'  Signals recorded: {len(self.collector.signals)}')

            t0 = mts()
            dev.delete()
            dt = (mts() - t0) * 1000
            time.sleep(1)

            print(f'  loop-delete with busy handler: {dt:.0f}ms')
            print(f'  Handler invocations: {len(busy_count)}')
            print(f'  Signals recorded: {len(self.collector.signals)}')
        except Exception as e:
            print(f'  op error (UDisks2 may be down): {e}')
        finally:
            self.collector._bus.remove_message_handler(_busy_handler)
            self.collector._bus.add_message_handler(self.collector._handler)
