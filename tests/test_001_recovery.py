"""Test whether UDisks2 recovers after being crashed by D-Bus+loop stress.

Measures:
- Does UDisks2 auto-restart (systemd socket activation)?
- How long does recovery take?
- Is recovery reliable enough for a retry strategy?
"""

import subprocess
import time
import unittest

from tests.conftest import (
    LoopDevice,
    ensure_dir,
    mts,
    print_system_info,
    udisksctl_available,
)

try:
    import dbus_fast  # noqa
    HAVE_DBUS = True
except ImportError:
    HAVE_DBUS = False


def _udisksctl_ok():
    """Check if UDisks2 is responsive via a simple D-Bus ping."""
    r = subprocess.run(
        ['busctl', '--system', 'call',
         'org.freedesktop.DBus', '/org/freedesktop/DBus',
         'org.freedesktop.DBus', 'NameHasOwner',
         's', 'org.freedesktop.UDisks2'],
        capture_output=True, text=True, timeout=10)
    # Output format: "b true" or "b false"
    return 'true' in r.stdout


def _wait_for_udisks(timeout=30):
    """Wait for UDisks2 to become responsive. Returns (ok, elapsed_s)."""
    t0 = mts()
    while (mts() - t0) < timeout:
        if _udisksctl_ok():
            # Give it a moment to fully initialize
            time.sleep(0.5)
            return True, round(mts() - t0, 1)
        time.sleep(0.5)
    return False, round(mts() - t0, 1)


def _crash_udisks():
    """Stress UDisks2 until it becomes unresponsive. Returns how many
    cycles it took to crash."""
    import asyncio
    from tests.conftest import SignalCollector

    async def _stress():
        for i in range(10):
            c = SignalCollector()
            await c.start()
            dev = LoopDevice()
            try:
                dev.create(timeout=15)
                time.sleep(0.3)
                dev.delete(timeout=15)
                time.sleep(0.1)
                dev.cleanup()
                await c.stop()
            except Exception:
                dev.cleanup()
                await c.stop()
                return i + 1
            if not _udisksctl_ok():
                await c.stop()
                return i + 1
            await c.stop()
        return 10

    return asyncio.run(_stress())


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestUDisks2Recovery(unittest.TestCase):
    """Does UDisks2 auto-recover after being stressed to death?"""

    @classmethod
    def setUpClass(cls):
        ensure_dir(__import__('os').path.join(
            __import__('os').path.dirname(__file__), '..', 'results'))
        print_system_info()
        # Ensure UDisks2 is alive before we start
        if not _udisksctl_ok():
            subprocess.run(
                "sudo systemctl restart udisks2 2>/dev/null || "
                "sudo systemctl start udisks2 2>/dev/null || true",
                shell=True)
            time.sleep(3)

    @classmethod
    def tearDownClass(cls):
        """Restart UDisks2 so subsequent test classes aren't affected."""
        print('\n  Restoring UDisks2 for subsequent tests...')
        subprocess.run(
            ['sudo', 'systemctl', 'stop', 'udisks2'],
            capture_output=True, timeout=10)
        time.sleep(1)
        subprocess.run(
            ['sudo', 'systemctl', 'reset-failed', 'udisks2'],
            capture_output=True, timeout=10)
        subprocess.run(
            ['sudo', 'systemctl', 'start', 'udisks2'],
            capture_output=True, timeout=10)
        time.sleep(2)
        alive = _udisksctl_ok()
        print(f'  UDisks2 after restore: {"ALIVE" if alive else "DEAD"}')

    def test_crash_and_auto_recover(self):
        """Crash UDisks2 via D-Bus stress, then wait for auto-recovery."""
        print('\n  Phase 1: Crashing UDisks2...')
        cycles_to_crash = _crash_udisks()
        print(f'  Crashed after {cycles_to_crash} stress cycles')

        # Check immediately
        alive = _udisksctl_ok()
        print(f'  Immediately after crash: {"ALIVE" if alive else "DEAD"}')

        if not alive:
            print('\n  Phase 2: Waiting for auto-recovery...')
            recovered, elapsed = _wait_for_udisks(timeout=60)
            if recovered:
                print(f'  Auto-recovered after {elapsed}s')
                # Verify it actually works
                dev = LoopDevice()
                try:
                    dev.create(timeout=15)
                    dev.delete(timeout=15)
                    dev.cleanup()
                    print(f'  Post-recovery loop-setup: OK')
                except Exception as e:
                    print(f'  Post-recovery loop-setup: FAIL - {e}')
            else:
                print(f'  Did NOT auto-recover within 60s')

    def test_crash_and_manual_restart(self):
        """Crash UDisks2, then manually restart via systemctl."""
        print('\n  Phase 1: Crashing UDisks2...')
        cycles = _crash_udisks()
        print(f'  Crashed after {cycles} stress cycles')

        print('  Phase 2: Manual restart via systemctl...')
        r = subprocess.run(
            ['sudo', 'systemctl', 'restart', 'udisks2'],
            capture_output=True, text=True, timeout=30)
        print(f'  systemctl restart: rc={r.returncode}')
        if r.stderr:
            print(f'  stderr: {r.stderr.strip()[:200]}')

        time.sleep(2)
        alive = _udisksctl_ok()
        print(f'  After restart: {"ALIVE" if alive else "DEAD"}')

        if alive:
            dev = LoopDevice()
            try:
                dev.create(timeout=15)
                dev.delete(timeout=15)
                dev.cleanup()
                print(f'  Post-restart loop-setup: OK')
            except Exception as e:
                print(f'  Post-restart loop-setup: FAIL - {e}')

    def test_crash_and_retry_loop(self):
        """After a crash, keep retrying loop-setup until it works.
        This simulates a test retry strategy."""
        print('\n  Phase 1: Crashing UDisks2...')
        cycles = _crash_udisks()
        print(f'  Crashed after {cycles} stress cycles')

        print('  Phase 2: Retry loop-setup every 2s until success...')
        for attempt in range(10):
            dev = LoopDevice()
            try:
                dev.create(timeout=10)
                dev.delete(timeout=10)
                dev.cleanup()
                print(f'  Attempt {attempt + 1}: OK')
                break
            except Exception as e:
                dev.cleanup()
                err = str(e)[:100]
                print(f'  Attempt {attempt + 1}: FAIL — {err}')
                time.sleep(2)
        else:
            print('  All 10 retry attempts FAILED')

    def test_udisks2_service_status(self):
        """Check systemd service status before and after crash."""
        def _status():
            r = subprocess.run(
                ['systemctl', 'show', 'udisks2',
                 '--property=ActiveState,SubState,MainPID,Restart,WatchdogUSec'],
                capture_output=True, text=True)
            return r.stdout.strip()

        print(f'\n  Before crash:\n    {_status()}')
        _crash_udisks()
        time.sleep(1)
        print(f'  After crash:\n    {_status()}')
        time.sleep(10)
        print(f'  After 10s wait:\n    {_status()}')

    def test_crash_recovery_with_dbus(self):
        """After crash+recovery, does a dbus-fast connection work?"""
        if not HAVE_DBUS:
            self.skipTest('dbus-fast not available')

        import asyncio
        from tests.conftest import SignalCollector

        # Crash it
        _crash_udisks()
        print('\n  Waiting for recovery...')
        _wait_for_udisks(timeout=60)
        time.sleep(2)

        # Try D-Bus connection
        async def _test():
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
            sc = len(c.signals)
            await c.stop()
            return ok, sc

        ok, sc = asyncio.run(_test())
        print(f'  Post-recovery D-Bus cycle: {"OK" if ok else "FAIL"}  '
              f'{sc} signals')
