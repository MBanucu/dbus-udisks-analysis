"""Compare D-Bus backend vs udisksctl monitor for signal completeness.

This directly tests whether the subprocess backend (parsing udisksctl
monitor stdout) is more reliable than the D-Bus backend in CI.
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


class UdisksctlMonitorCollector:
    """Spawn `udisksctl monitor` and collect its raw output lines."""

    def __init__(self):
        self._proc = None
        self._lines: list[str] = []
        self._started = False
        self._stop = threading.Event()

    def start(self):
        self._proc = subprocess.Popen(
            ['udisksctl', 'monitor'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._started = True

    def _read(self):
        while not self._stop.is_set():
            try:
                line = self._proc.stdout.readline()
                if not line:
                    break
                self._lines.append(line.rstrip('\n'))
            except Exception:
                break

    def reset(self):
        self._lines.clear()

    def stop(self):
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    @property
    def line_count(self):
        return len(self._lines)

    def interesting_lines(self):
        """Return lines likely containing event data."""
        keywords = ['Added', 'Removed', 'Properties', 'Completed',
                    'block_devices', 'Operation:', 'Objects:']
        return [l for l in self._lines
                if any(kw in l for kw in keywords)]


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestBackendComparison(unittest.TestCase):
    """Compare D-Bus signal collection vs udisksctl monitor output."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def setUp(self):
        self.dbus = SignalCollector()
        self.monitor = UdisksctlMonitorCollector()
        asyncio.run(self.dbus.start())
        self.monitor.start()

    def tearDown(self):
        asyncio.run(self.dbus.stop())
        self.monitor.stop()

    def _run_cycle(self, label, operation_fn):
        """Run one operation, collect from both backends."""
        for c in (self.dbus, self.monitor):
            c.reset()

        t0 = mts()
        try:
            result = operation_fn()
        except Exception as e:
            result = f'ERROR: {e}'
        dt = round((mts() - t0) * 1000)

        time.sleep(1)  # Let signals settle

        dbus_count = len(self.dbus.signals)
        monitor_lines = self.monitor.line_count
        interesting = self.monitor.interesting_lines()

        print(f'\n  [{label}] {dt}ms  '
              f'dbus: {dbus_count} signals  '
              f'monitor: {monitor_lines} lines ({len(interesting)} interesting)')

        return {
            'label': label,
            'duration_ms': dt,
            'result': str(result)[:200],
            'dbus_signals': dbus_count,
            'dbus_counts': self.dbus.count_by_interface(),
            'monitor_lines': monitor_lines,
            'monitor_interesting': len(interesting),
            'monitor_sample': interesting[:15],
        }

    def test_loop_setup_both_backends(self):
        """Compare D-Bus vs udisksctl monitor for loop-setup."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)
        info = self._run_cycle('loop-setup', dev.create)

        # Write report
        path = os.path.join(RESULT_DIR, 'backend_compare_setup.json')
        with open(path, 'w') as f:
            json.dump(info, f, indent=2)

        if info['dbus_signals'] == 0 and info['monitor_interesting'] > 0:
            print(f'\n  KEY FINDING: udisksctl monitor captured events '
                  f'but D-Bus got none!')

    def test_loop_delete_both_backends(self):
        """Compare D-Bus vs udisksctl monitor for loop-delete."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)
        dev.create()
        time.sleep(1)
        info = self._run_cycle('loop-delete', dev.delete)

        path = os.path.join(RESULT_DIR, 'backend_compare_delete.json')
        with open(path, 'w') as f:
            json.dump(info, f, indent=2)

    def test_five_cycles_both_backends(self):
        """Run 5 loop-setup/delete cycles, compare both backends each time."""
        results = []
        for i in range(5):
            dev = LoopDevice()
            setup_info = self._run_cycle(f'cycle_{i}_setup', dev.create)
            time.sleep(0.5)
            delete_info = self._run_cycle(f'cycle_{i}_delete', dev.delete)
            dev.cleanup()

            dbus_ok = setup_info['dbus_signals'] > 0 and delete_info['dbus_signals'] > 0
            monitor_ok = (setup_info['monitor_interesting'] > 0 and
                          delete_info['monitor_interesting'] > 0)

            results.append({
                'cycle': i,
                'setup': setup_info,
                'delete': delete_info,
                'dbus_ok': dbus_ok,
                'monitor_ok': monitor_ok,
            })

            print(f'  cycle {i}: dbus={"OK" if dbus_ok else "MISS":4s}  '
                  f'monitor={"OK" if monitor_ok else "MISS":4s}')

            time.sleep(0.3)

        path = os.path.join(RESULT_DIR, 'backend_compare_cycles.json')
        dbus_ok_count = sum(1 for r in results if r['dbus_ok'])
        monitor_ok_count = sum(1 for r in results if r['monitor_ok'])

        summary = {
            'cycles': 5,
            'dbus_ok': dbus_ok_count,
            'monitor_ok': monitor_ok_count,
            'dbus_better': dbus_ok_count > monitor_ok_count,
            'monitor_better': monitor_ok_count > dbus_ok_count,
            'results': results,
        }
        with open(path, 'w') as f:
            json.dump(summary, f, indent=2)

        print(f'\n  SUMMARY: dbus {dbus_ok_count}/5 OK, '
              f'monitor {monitor_ok_count}/5 OK')

    def test_udisksctl_monitor_reliability(self):
        """Does udisksctl monitor itself ever fail to start or crash?"""
        for i in range(3):
            proc = subprocess.Popen(
                ['udisksctl', 'monitor'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.5)
            poll = proc.poll()
            print(f'\n  monitor instance {i}: pid={proc.pid} poll={poll}')
            if poll is not None:
                _, stderr = proc.communicate()
                print(f'    EXITED with {poll}, stderr: {stderr[:300]}')
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc.communicate()
                print(f'    running OK, terminated')
