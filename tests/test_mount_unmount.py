"""Analyze signal patterns during mount and unmount operations."""

import asyncio
import json
import os
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
class TestMountUnmount(unittest.TestCase):
    """Capture and analyze mount/unmount signal patterns."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def setUp(self):
        self.collector = SignalCollector()
        asyncio.run(self.collector.start())

    def tearDown(self):
        asyncio.run(self.collector.stop())

    def _print_signals(self, label, signals):
        print(f'\n  ── {label} ({len(signals)} signals) ──')
        for i, s in enumerate(signals):
            short_path = s['path'].rsplit('/', 1)[-1] if s['path'] else ''
            gap = ''
            if i > 0:
                gap_ms = (s['elapsed'] - signals[i - 1]['elapsed']) * 1000
                gap = f'+{gap_ms:.1f}ms'
            print(f'  {i:2d}  {gap:>10s}  '
                  f'{s["interface"]}.{s["member"]:<40s}  '
                  f'{short_path}')

    def _json_report(self, label, signals, duration_ms):
        path = os.path.join(RESULT_DIR, f'{label}_signals.json')
        data = {
            'operation': label,
            'duration_ms': duration_ms,
            'total_signals': len(signals),
            'signals': [
                {
                    'i': i,
                    'elapsed_ms': round(s['elapsed'] * 1000, 1),
                    'interface': s['interface'],
                    'member': s['member'],
                    'path': s['path'],
                }
                for i, s in enumerate(signals)
            ],
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    def test_mount_signals(self):
        """Capture all signals during mount."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        try:
            dev.create()
        except Exception as e:
            self.skipTest(f'loop-setup failed, UDisks2 not responding: {e}')
        time.sleep(1)
        self.collector.reset()

        t0 = mts()
        try:
            dev.mount()
        except Exception as e:
            print(f'  mount ERROR: {e}')
            return
        time.sleep(1)
        duration_ms = round((mts() - t0) * 1000)

        self._print_signals('mount', self.collector.signals)
        self._json_report('mount', self.collector.signals, duration_ms)

    def test_unmount_signals(self):
        """Capture all signals during unmount."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        try:
            dev.create()
            dev.mount()
        except Exception as e:
            self.skipTest(f'loop-setup failed, UDisks2 not responding: {e}')
        time.sleep(1)
        self.collector.reset()

        t0 = mts()
        try:
            dev.unmount()
        except Exception as e:
            print(f'  unmount ERROR: {e}')
            return
        time.sleep(1)
        duration_ms = round((mts() - t0) * 1000)

        self._print_signals('unmount', self.collector.signals)
        self._json_report('unmount', self.collector.signals, duration_ms)

    def test_mount_unmount_cycle(self):
        """Full mount-unmount cycle signal trace."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)
        try:
            dev.create()
        except Exception as e:
            self.skipTest(f'loop-setup failed, UDisks2 not responding: {e}')

        all_signals = []
        for op_name, op_fn in [('mount', dev.mount), ('unmount', dev.unmount)]:
            time.sleep(0.5)
            self.collector.reset()
            t0 = mts()
            try:
                op_fn()
            except Exception as e:
                print(f'  {op_name} ERROR: {e}')
                continue
            time.sleep(1)
            self._print_signals(op_name, self.collector.signals)
            for s in self.collector.signals:
                all_signals.append((op_name, s))

        # Write combined
        path = os.path.join(RESULT_DIR, 'mount_unmount_cycle.json')
        data = {
            'operations': ['mount', 'unmount'],
            'signals': [
                {
                    'operation': op,
                    'interface': s['interface'],
                    'member': s['member'],
                    'path': s['path'],
                }
                for op, s in all_signals
            ],
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
