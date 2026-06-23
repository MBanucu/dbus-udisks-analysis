"""Capture ALL UDisks2 signals without filtering or expectations.

This test connects to D-Bus, subscribes to UDisks2 signals, runs
operations, and reports everything it sees — no "should", only "did".
"""

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
class TestRawSignalCapture(unittest.TestCase):
    """Connect once, capture everything during loop operations."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def setUp(self):
        self.collector = SignalCollector()
        asyncio.run(self.collector.start())
        self.addCleanup(lambda: asyncio.run(self.collector.stop()))

    # ── helpers ───────────────────────────────────────────────────

    def _do_op(self, label, fn, settle=0.5):
        """Run *fn* while collecting, tag signals, dump JSON."""
        self.collector.reset()
        t0 = mts()
        try:
            result = fn()
        except Exception as e:
            result = f'ERROR: {e}'
        t1 = mts()
        time.sleep(settle)
        duration = round((t1 - t0) * 1000)
        filename = os.path.join(RESULT_DIR, f'{label}.json')
        self.collector.dump(filename)
        return {
            'label': label,
            'duration_ms': duration,
            'result': str(result)[:200],
            'signal_count': len(self.collector.signals),
            'counts': self.collector.count_by_interface(),
            'paths': sorted(self.collector.paths_seen()),
            'dump': filename,
        }

    # ── tests ─────────────────────────────────────────────────────

    def test_loop_setup_signal_firehose(self):
        """Collect everything during a single loop-setup."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        info = self._do_op('loop_setup', dev.create)
        self._print_op(info)

    def test_loop_delete_signal_firehose(self):
        """Collect everything during loop-delete."""
        dev = LoopDevice()
        try:
            dev.create()
        except Exception as e:
            self.skipTest(f'loop-setup failed, UDisks2 not responding: {e}')
        self.addCleanup(dev.cleanup)
        self.collector.reset()

        info = self._do_op('loop_delete', dev.delete)
        self._print_op(info)

    def test_full_lifecycle(self):
        """Capture the full lifecycle: setup -> mount -> unmount -> delete."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        results = []
        results.append(self._do_op('lifecycle_setup', dev.create))

        time.sleep(1)
        results.append(self._do_op('lifecycle_mount', dev.mount))

        time.sleep(1)
        results.append(self._do_op('lifecycle_unmount', dev.unmount))

        time.sleep(1)
        results.append(self._do_op('lifecycle_delete', dev.delete))

        # Write combined report
        report = {
            'operations': results,
            'total_signals': sum(r['signal_count'] for r in results),
            'total_duration_ms': sum(r['duration_ms'] for r in results),
        }
        path = os.path.join(RESULT_DIR, 'full_lifecycle.json')
        with open(path, 'w') as f:
            json.dump(report, f, indent=2)

        for r in results:
            self._print_op(r)
        print(f'\n  Combined report: {path}')

    def test_repeated_loop_ops(self):
        """Run 5 loop-setup/delete cycles, collect all signals for each."""
        results = []
        for i in range(5):
            dev = LoopDevice()
            info = {
                'cycle': i,
                'setup': None,
                'delete': None,
            }
            try:
                info['setup'] = self._do_op(
                    f'repeat_{i}_setup', dev.create, settle=0.3)
                time.sleep(0.2)
                info['delete'] = self._do_op(
                    f'repeat_{i}_delete', dev.delete, settle=0.3)
            finally:
                dev.cleanup()
            results.append(info)
            time.sleep(0.3)
            self._print_op_summary(i, info)

        path = os.path.join(RESULT_DIR, 'repeated_cycles.json')
        report = {
            'cycles': len(results),
            'details': [],
        }
        for i, r in enumerate(results):
            detail = {'cycle': i}
            for op in ('setup', 'delete'):
                if r[op]:
                    detail[op] = {
                        'signal_count': r[op]['signal_count'],
                        'duration_ms': r[op]['duration_ms'],
                        'error': r[op]['result'].startswith('ERROR'),
                    }
            report['details'].append(detail)
        with open(path, 'w') as f:
            json.dump(report, f, indent=2)
        print(f'\n  Repeated cycles report: {path}')

    # ── output ────────────────────────────────────────────────────

    def _print_op(self, info):
        print(f'\n  [{info["label"]}] {info["duration_ms"]}ms  '
              f'{info["signal_count"]} signals')
        for iface, count in sorted(info['counts'].items()):
            print(f'    {count:3d}  {iface}')
        print(f'    paths: {info["paths"]}')

    def _print_op_summary(self, i, info):
        for op in ('setup', 'delete'):
            if info[op]:
                d = info[op]
                ok = not d['result'].startswith('ERROR')
                print(f'  cycle {i:2d}  {op:6s}  '
                      f'{"OK" if ok else "FAIL":4s}  '
                      f'{d["duration_ms"]:5d}ms  '
                      f'{d["signal_count"]:3d} signals')
