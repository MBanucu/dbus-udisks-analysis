"""Stress-test: multiple concurrent dbus-fast connections + UDisks2 ops.

Tests how UDisks2 behaves when many D-Bus connections subscribe to
its signals simultaneously.
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
class TestConnectionStress(unittest.TestCase):
    """How does UDisks2 handle multiple signal subscribers?"""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    async def _make_collectors(self, count):
        """Create N collectors, all subscribed to UDisks2."""
        collectors = []
        for i in range(count):
            c = SignalCollector()
            await c.start()
            collectors.append(c)
        return collectors

    async def _stop_collectors(self, collectors):
        """Stop all collectors."""
        for c in collectors:
            await c.stop()

    # ── tests ─────────────────────────────────────────────────────

    def test_single_connection_operations(self):
        """Baseline: 1 dbus connection, 5 loop cycles."""
        self._run_stress_test(1, 5, 'single_connection')

    def test_two_connections_operations(self):
        """2 simultaneous connections, 5 loop cycles."""
        self._run_stress_test(2, 5, 'two_connections')

    def test_five_connections_operations(self):
        """5 simultaneous connections, 5 loop cycles."""
        self._run_stress_test(5, 5, 'five_connections')

    def test_ten_connections_operations(self):
        """10 simultaneous connections, 5 loop cycles."""
        self._run_stress_test(10, 3, 'ten_connections')

    def _run_stress_test(self, num_connections, num_cycles, label):
        print(f'\n  {"=" * 60}')
        print(f'  {label}: {num_connections} connections, {num_cycles} cycles')
        print(f'  {"=" * 60}')

        async def _run():
            collectors = await self._make_collectors(num_connections)
            try:
                results = []
                for cycle in range(num_cycles):
                    dev = LoopDevice()
                    cycle_info = {'cycle': cycle, 'setup': None, 'delete': None}

                    # Reset all collectors
                    for c in collectors:
                        c.reset()

                    try:
                        t0 = mts()
                        dev.create()
                        dt = mts() - t0
                        cycle_info['setup'] = {
                            'ok': True,
                            'device': dev.device_name,
                            'duration_ms': round(dt * 1000),
                            'signals_per_collector': [len(c.signals) for c in collectors],
                        }
                    except Exception as e:
                        cycle_info['setup'] = {
                            'ok': False,
                            'error': str(e)[:200],
                            'signals_per_collector': [len(c.signals) for c in collectors],
                        }

                    time.sleep(0.5)

                    # Reset all collectors again
                    for c in collectors:
                        c.reset()

                    try:
                        t0 = mts()
                        dev.delete()
                        dt = mts() - t0
                        cycle_info['delete'] = {
                            'ok': True,
                            'duration_ms': round(dt * 1000),
                            'signals_per_collector': [len(c.signals) for c in collectors],
                        }
                    except Exception as e:
                        cycle_info['delete'] = {
                            'ok': False,
                            'error': str(e)[:200],
                            'signals_per_collector': [len(c.signals) for c in collectors],
                        }

                    dev.cleanup()
                    results.append(cycle_info)

                    # Print cycle summary
                    s_ok = cycle_info['setup']['ok'] if cycle_info['setup'] else False
                    d_ok = cycle_info['delete']['ok'] if cycle_info['delete'] else False
                    s_sc = sum(cycle_info['setup']['signals_per_collector']) if cycle_info['setup'] else 0
                    d_sc = sum(cycle_info['delete']['signals_per_collector']) if cycle_info['delete'] else 0
                    print(f'  cycle {cycle:2d}  '
                          f'setup={"OK" if s_ok else "FAIL":4s} '
                          f'({s_sc:3d} total signals)  '
                          f'delete={"OK" if d_ok else "FAIL":4s} '
                          f'({d_sc:3d} total signals)')

                    time.sleep(0.3)

                return results
            finally:
                await self._stop_collectors(collectors)

        results = asyncio.run(_run())

        # Summary
        setup_ok = sum(1 for r in results if r['setup'] and r['setup']['ok'])
        delete_ok = sum(1 for r in results if r['delete'] and r['delete']['ok'])
        print(f'\n  SUMMARY: setup {setup_ok}/{len(results)} OK, '
              f'delete {delete_ok}/{len(results)} OK')

        # Report
        path = os.path.join(RESULT_DIR, f'{label}.json')
        report = {
            'connections': num_connections,
            'cycles': num_cycles,
            'setup_success': setup_ok,
            'delete_success': delete_ok,
            'results': results,
        }
        with open(path, 'w') as f:
            json.dump(report, f, indent=2)

        # Report failures but don't assert (observational)
        if setup_ok < num_cycles or delete_ok < num_cycles:
            print(f'\n  WARNING: Some operations failed with '
                  f'{num_connections} connections')

    def test_rapid_connect_disconnect(self):
        """Open and close connections rapidly while running loop ops."""
        async def _connect_disconnect(count):
            for i in range(count):
                c = SignalCollector()
                await c.start()
                await asyncio.sleep(0.01)
                await c.stop()
                await asyncio.sleep(0.01)

        print('\n  Rapid connect/disconnect (50 cycles) + concurrent loop ops')
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        async def _test():
            # Start rapid connect/disconnect
            cd_task = asyncio.create_task(_connect_disconnect(50))

            # Run loop ops concurrently
            results = []
            for i in range(3):
                dev2 = LoopDevice()
                try:
                    t0 = mts()
                    dev2.create()
                    dt = (mts() - t0) * 1000
                    results.append(('setup', 'OK', dt))
                except Exception as e:
                    results.append(('setup', str(e)[:80], 0))
                time.sleep(0.3)
                try:
                    t0 = mts()
                    dev2.delete()
                    dt = (mts() - t0) * 1000
                    results.append(('delete', 'OK', dt))
                except Exception as e:
                    results.append(('delete', str(e)[:80], 0))
                dev2.cleanup()
                time.sleep(0.3)

            await cd_task
            return results

        results = asyncio.run(_test())

        for op, status, dt in results:
            print(f'  {op:6s}  {status:5s}  {dt:6.0f}ms')
        ok = sum(1 for _, s, _ in results if s == 'OK')
        print(f'\n  {ok}/{len(results)} operations OK during rapid connect/disconnect')
