"""Find UDisks2's breaking point on CI through systematic stress tests.

Measures:
- How many consecutive loop-setup/delete cycles before failure?
- How many back-to-back D-Bus connections before UDisks2 crashes?
- Recovery time needed between subprocess + D-Bus backend switches?
- Maximum concurrent monitors before UDisks2 becomes unresponsive?
"""

import asyncio
import json
import os
import subprocess
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


def _bare_loop_cycle():
    """One loop-setup + loop-delete via raw subprocess.  Returns (ok, duration_ms)."""
    dev = LoopDevice()
    t0 = mts()
    try:
        dev.create(timeout=15)
        dev.delete(timeout=15)
        dev.cleanup()
        return True, round((mts() - t0) * 1000)
    except Exception as e:
        dev.cleanup()
        return False, round((mts() - t0) * 1000)


def _dbus_monitor_cycle():
    """One loop-setup + loop-delete with a fresh D-Bus monitor.
    Returns (ok, duration_ms, signal_count)."""
    async def _run():
        c = SignalCollector()
        await c.start()
        dev = LoopDevice()
        t0 = mts()
        try:
            dev.create(timeout=15)
            time.sleep(0.5)
            dev.delete(timeout=15)
            time.sleep(0.3)
            dev.cleanup()
            await c.stop()
            return True, round((mts() - t0) * 1000), len(c.signals)
        except Exception as e:
            dev.cleanup()
            await c.stop()
            return False, round((mts() - t0) * 1000), len(c.signals)

    return asyncio.run(_run())


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestUDisks2Limits(unittest.TestCase):
    """Find how many operations UDisks2 can handle before failing."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    @classmethod
    def tearDownClass(cls):
        """Restore UDisks2 after stress testing so subsequent tests work."""
        print('\n  Restoring UDisks2 after limits stress...')
        subprocess.run(
            ['sudo', 'systemctl', 'stop', 'udisks2'],
            capture_output=True, timeout=10)
        subprocess.run(
            ['sudo', 'systemctl', 'reset-failed', 'udisks2'],
            capture_output=True, timeout=10)
        subprocess.run(
            ['sudo', 'systemctl', 'start', 'udisks2'],
            capture_output=True, timeout=10)
        time.sleep(2)

    # ── consecutive bare cycles ───────────────────────────────────

    def test_consecutive_bare_cycles(self):
        """How many bare loop-setup/delete cycles before failure?
        No Python D-Bus involvement at all."""
        print('\n  Consecutive bare loop cycles (no cooldown):')
        results = []
        for i in range(6):
            ok, dt = _bare_loop_cycle()
            results.append((i, ok, dt))
            status = 'OK' if ok else 'FAIL'
            print(f'    cycle {i:2d}: {status:4s}  {dt:5d}ms')
            if not ok:
                print(f'    → UDisks2 failed at cycle {i} (bare, no D-Bus)')
                break

        ok_count = sum(1 for _, ok, _ in results if ok)
        print(f'\n  Bare cycles: {ok_count}/{len(results)} OK')

        with open(os.path.join(RESULT_DIR, 'consecutive_bare.json'), 'w') as f:
            json.dump({'cycles': len(results), 'ok': ok_count,
                       'results': [(i, ok, dt) for i, ok, dt in results]}, f)

    def test_consecutive_bare_with_cooldown(self):
        """Same as above but with 1s cooldown between cycles."""
        print('\n  Consecutive bare loop cycles (1s cooldown):')
        results = []
        for i in range(6):
            ok, dt = _bare_loop_cycle()
            results.append((i, ok, dt))
            print(f'    cycle {i:2d}: {"OK" if ok else "FAIL":4s}  {dt:5d}ms')
            if not ok:
                print(f'    → UDisks2 failed at cycle {i}')
                break
            time.sleep(1)

        ok_count = sum(1 for _, ok, _ in results if ok)
        print(f'\n  Bare with cooldown: {ok_count}/{len(results)} OK')

        with open(os.path.join(RESULT_DIR, 'consecutive_bare_cooldown.json'), 'w') as f:
            json.dump({'cycles': len(results), 'ok': ok_count,
                       'results': [(i, ok, dt) for i, ok, dt in results]}, f)

    # ── consecutive D-Bus monitor cycles ──────────────────────────

    def test_consecutive_dbus_monitor_cycles(self):
        """How many loop cycles with a fresh D-Bus connection each time?"""
        print('\n  Consecutive D-Bus monitor cycles (fresh connection each):')
        results = []
        for i in range(8):
            ok, dt, sc = _dbus_monitor_cycle()
            results.append((i, ok, dt, sc))
            print(f'    cycle {i:2d}: {"OK" if ok else "FAIL":4s}  '
                  f'{dt:5d}ms  {sc:3d} signals')
            if not ok:
                print(f'    → D-Bus monitor failed at cycle {i}')
                break
            time.sleep(0.5)

        ok_count = sum(1 for _, ok, _, _ in results if ok)
        print(f'\n  D-Bus monitor cycles: {ok_count}/{len(results)} OK')

        with open(os.path.join(RESULT_DIR, 'consecutive_dbus.json'), 'w') as f:
            json.dump({'cycles': len(results), 'ok': ok_count,
                       'results': [(i, ok, dt, sc) for i, ok, dt, sc in results]}, f)

    # ── subprocess → D-Bus switch ─────────────────────────────────

    def test_subprocess_to_dbus_switch(self):
        """How many subprocess→D-Bus backend switches before UDisks2 breaks?
        This simulates the udisks-monitor parity test pattern."""
        print('\n  Subprocess → D-Bus backend switches:')

        async def _subprocess_cycle():
            """Spawn udisksctl monitor, run cycle, kill it."""
            proc = subprocess.Popen(
                ['udisksctl', 'monitor'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            time.sleep(1)
            dev = LoopDevice()
            try:
                dev.create(timeout=15)
                time.sleep(0.5)
                dev.delete(timeout=15)
                time.sleep(0.3)
                dev.cleanup()
                ok = True
            except Exception:
                ok = False
                dev.cleanup()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return ok

        async def _dbus_cycle():
            c = SignalCollector()
            await c.start()
            dev = LoopDevice()
            try:
                dev.create(timeout=15)
                time.sleep(0.5)
                dev.delete(timeout=15)
                time.sleep(0.3)
                dev.cleanup()
                ok = True
            except Exception:
                ok = False
                dev.cleanup()
            await c.stop()
            return ok

        results = []
        for i in range(6):
            # Subprocess first
            sub_ok = asyncio.run(_subprocess_cycle())
            time.sleep(0.5)
            # Then D-Bus
            try:
                dbus_ok = asyncio.run(_dbus_cycle())
            except Exception:
                dbus_ok = False
            results.append((i, sub_ok, dbus_ok))
            print(f'    switch {i:2d}: sub={"OK" if sub_ok else "FAIL":4s}  '
                  f'dbus={"OK" if dbus_ok else "FAIL":4s}')
            if not dbus_ok:
                print(f'    → D-Bus backend failed after {i+1} subprocess→D-Bus switches')
                break
            time.sleep(1)

        sub_ok_count = sum(1 for _, s, _ in results if s)
        dbus_ok_count = sum(1 for _, _, d in results if d)
        print(f'\n  Switches: sub {sub_ok_count}/{len(results)} OK, '
              f'dbus {dbus_ok_count}/{len(results)} OK')

        with open(os.path.join(RESULT_DIR, 'subprocess_dbus_switch.json'), 'w') as f:
            json.dump({'switches': len(results),
                       'sub_ok': sub_ok_count, 'dbus_ok': dbus_ok_count,
                       'results': [(i, s, d) for i, s, d in results]}, f)

    # ── recovery time ─────────────────────────────────────────────

    def test_recovery_time_after_subprocess(self):
        """How long must we wait after killing udisksctl monitor before
        a D-Bus backend can successfully connect?"""
        print('\n  Recovery time after subprocess kill:')
        delays = [0.5, 1, 2, 5]
        results = {}

        for delay in delays:
            proc = subprocess.Popen(
                ['udisksctl', 'monitor'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            time.sleep(1)

            dev = LoopDevice()
            try:
                dev.create(timeout=15)
                dev.delete(timeout=15)
                dev.cleanup()
            except Exception:
                dev.cleanup()

            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

            print(f'    waiting {delay}s...')
            time.sleep(delay)

            try:
                ok, dt, sc = _dbus_monitor_cycle()
                results[str(delay)] = {'ok': ok, 'duration_ms': dt, 'signals': sc}
                print(f'    delay {delay:3}s: {"OK" if ok else "FAIL":4s}  '
                      f'{dt:5d}ms  {sc:3d} signals')
            except Exception as e:
                results[str(delay)] = {'ok': False, 'error': str(e)[:200]}
                print(f'    delay {delay:3}s: FAIL - {e}')

        with open(os.path.join(RESULT_DIR, 'recovery_time.json'), 'w') as f:
            json.dump(results, f, indent=2)

    # ── cumulative connection count ───────────────────────────────

    def test_max_concurrent_monitors(self):
        """How many simultaneous D-Bus signal monitors before UDisks2
        becomes unresponsive?"""
        print('\n  Max concurrent D-Bus monitors:')

        async def _test_n(n):
            collectors = []
            for i in range(n):
                try:
                    c = SignalCollector()
                    await c.start()
                    collectors.append(c)
                except Exception as e:
                    print(f'    failed to start collector {i}: {e}')
                    break

            count = len(collectors)
            # Run one loop cycle — do all monitors receive signals?
            dev = LoopDevice()
            try:
                dev.create(timeout=15)
                time.sleep(1)
                dev.delete(timeout=15)
                time.sleep(0.5)
                dev.cleanup()
            except Exception as e:
                dev.cleanup()

            signals_per = [len(c.signals) for c in collectors]
            for c in collectors:
                await c.stop()
            return count, signals_per

        for n in [1, 2, 3, 5, 8]:
            try:
                count, sigs = asyncio.run(_test_n(n))
                zero = sum(1 for s in sigs if s == 0)
                print(f'    {n:2d} monitors: {count}/{n} started, '
                      f'{zero} got 0 signals, signals={sigs}')
            except Exception as e:
                print(f'    {n:2d} monitors: FAIL - {e}')
                break
