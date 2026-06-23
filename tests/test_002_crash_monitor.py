"""Correlate D-Bus stress with system logs to determine UDisks2 crash
timing, root cause, and recovery behavior.

Uses three monitoring layers:
1. D-Bus signal collector (application-level visibility)
2. LogMonitor (system-level: journalctl, dmesg, systemctl snapshots)
3. udisksctl subprocess (control-plane: does UDisks2 respond?)

Output:
- results/crash_timeline.json  — full correlated timeline
- results/crash_evidence.md    — human-readable findings
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

try:
    from tools.log_monitor import LogMonitor
    HAVE_LOG_MONITOR = True
except ImportError:
    HAVE_LOG_MONITOR = False

RESULT_DIR = os.path.join(os.path.dirname(__file__), '..', 'results')


def _udisks_alive():
    """Check if UDisks2 responds to a D-Bus ping."""
    try:
        r = subprocess.run(
            ['busctl', '--system', 'call',
             'org.freedesktop.DBus', '/org/freedesktop/DBus',
             'org.freedesktop.DBus', 'NameHasOwner',
             's', 'org.freedesktop.UDisks2'],
            capture_output=True, text=True, timeout=10)
        return 'true' in r.stdout
    except Exception:
        return False


def _journal_available():
    """Check if we can read the system journal."""
    try:
        r = subprocess.run(
            ['journalctl', '-u', 'udisks2', '--no-pager', '-n', '1'],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _dmesg_available():
    """Check if dmesg is readable."""
    try:
        r = subprocess.run(['dmesg'], capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and len(r.stdout) > 0
    except Exception:
        return False


def _stress_udisks(cycles=20, cooldown=0.1):
    """Rapidly create/delete loop devices while connecting/disconnecting
    D-Bus monitors. Returns (ok, crash_at_cycle, signal_counts)."""
    signal_counts = []

    for i in range(cycles):
        async def _one_cycle():
            c = SignalCollector()
            await c.start()
            dev = LoopDevice()
            try:
                dev.create(timeout=10)
                dev.delete(timeout=10)
                dev.cleanup()
                sc = len(c.signals)
                await c.stop()
                return sc
            except Exception:
                dev.cleanup()
                try:
                    await c.stop()
                except Exception:
                    pass
                return None

        try:
            sc = asyncio.run(_one_cycle())
        except Exception:
            sc = None

        if sc is None:
            return False, i + 1, signal_counts
        signal_counts.append(sc)
        time.sleep(cooldown)

        # Periodically check if UDisks2 is still alive
        if i > 0 and i % 5 == 0:
            if not _udisks_alive():
                return False, i + 1, signal_counts

    return True, cycles, signal_counts


def _collect_pre_stress_journal(lines=50):
    """Capture recent journal entries for baseline."""
    try:
        r = subprocess.run(
            ['journalctl', '-u', 'udisks2', '--no-pager', '-n', str(lines),
             '-o', 'short-iso-precise'],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ''


def _collect_post_stress_journal(lines=100):
    """Capture journal since the stress test started."""
    return _collect_pre_stress_journal(lines=lines)


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestCrashMonitor(unittest.TestCase):
    """Monitor system logs during UDisks2 stress to find crash root cause."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()
        cls._log_monitor_available = HAVE_LOG_MONITOR and _journal_available()
        cls._dmesg_available = _dmesg_available()

        print(f'\n  System log monitoring: '
              f'{"AVAILABLE" if cls._log_monitor_available else "UNAVAILABLE"}')
        print(f'  dmesg: {"AVAILABLE" if cls._dmesg_available else "UNAVAILABLE"}')

        if not cls._log_monitor_available:
            print('  → Will run limited analysis without journal monitoring')

    def setUp(self):
        # Ensure UDisks2 is alive
        if not _udisks_alive():
            print('  Starting UDisks2...')
            subprocess.run(
                ['sudo', 'systemctl', 'start', 'udisks2'],
                capture_output=True, timeout=15)
            time.sleep(3)

    # ── baseline: journal contents before any stress ───────────────

    def test_journal_baseline(self):
        """Capture existing journal entries to establish baseline."""
        baseline = _collect_pre_stress_journal(100)
        lines = [l for l in baseline.split('\n') if l.strip()]
        error_lines = [l for l in lines
                       if any(kw in l.lower() for kw in
                              ['error', 'fail', 'crash', 'abort', 'segfault',
                               'signal', 'killed', 'oom', 'assert'])]

        print(f'\n  Journal baseline: {len(lines)} entries')
        print(f'  Error/crash indicators: {len(error_lines)}')
        for l in error_lines[:10]:
            print(f'    {l[:200]}')

        with open(os.path.join(RESULT_DIR, 'journal_baseline.json'), 'w') as f:
            json.dump({
                'total_entries': len(lines),
                'error_indicators': len(error_lines),
                'sample': error_lines[:20],
                'full': lines,
            }, f, indent=2)

        # Document what's already in the journal
        data = {
            'entries': len(lines),
            'error_count': len(error_lines),
            'errors': error_lines[:20],
        }
        with open(os.path.join(RESULT_DIR, 'journal_baseline.json'), 'w') as f:
            json.dump(data, f, indent=2, default=str)

    # ── correlated stress + log monitoring ─────────────────────────

    def test_crash_correlated_stress(self):
        """Stress UDisks2 while capturing all logs to correlate crash timing."""
        if not self._log_monitor_available:
            self.skipTest('Log monitoring not available')

        monitor = LogMonitor(tags={'test': 'crash_correlated_stress'})
        monitor.start()
        monitor.mark('before_first_operation')

        # Phase 1: Run stress test
        print('\n  Phase 1: Stressing UDisks2...')
        monitor.mark('stress_start')
        ok, crash_at, signal_counts = _stress_udisks(cycles=15, cooldown=0.05)
        monitor.mark('stress_end', ok=ok, crash_at=crash_at)

        if ok:
            print(f'  All {len(signal_counts)} cycles OK')
        else:
            print(f'  CRASHED at cycle {crash_at}/{len(signal_counts)}')

        # Phase 2: Wait and check recovery
        print('\n  Phase 2: Checking recovery...')
        time.sleep(2)
        alive = _udisks_alive()
        monitor.mark('recovery_check', alive=alive)

        # Phase 3: Try a normal operation
        if alive:
            print('  Phase 3: Post-stress operation...')
            monitor.mark('post_stress_op_start')
            try:
                dev = LoopDevice()
                dev.create(timeout=15)
                dev.delete(timeout=15)
                dev.cleanup()
                monitor.mark('post_stress_op_end', ok=True)
                print('  Post-stress operation: OK')
            except Exception as e:
                monitor.mark('post_stress_op_end', ok=False, error=str(e)[:200])
                print(f'  Post-stress operation: FAIL - {e}')

        monitor.stop()
        monitor.save_report(os.path.join(RESULT_DIR, 'crash_correlated.json'))

        # Print report
        print('\n' + monitor.report())

        # Verify we get meaningful data
        events = monitor.events()
        journals = [e for e in events if e['source'] == 'journal-udisks2']
        snapshots = monitor.snapshots()
        dead_snapshots = [s for s in snapshots
                          if s.get('activestate') not in ('active', None)]

        print(f'\n  Evidence summary:')
        print(f'    Journal entries captured: {len(journals)}')
        print(f'    State snapshots: {len(snapshots)}')
        print(f'    Non-active snapshots: {len(dead_snapshots)}')

        if crash_at:
            print(f'    UDisks2 became unresponsive at cycle {crash_at}')
            if dead_snapshots:
                print(f'    Systemd confirmed dead: {dead_snapshots[0]}')

    # ── loop device exhaustion ─────────────────────────────────────

    def test_loop_device_exhaustion(self):
        """Can we exhaust loop devices and crash UDisks2 that way?"""
        print('\n  Testing loop device exhaustion...')

        # Create many loop devices without deleting them
        devices = []
        failed_at = None
        uid = os.getuid()

        for i in range(20):
            dev = LoopDevice()
            try:
                dev.create(timeout=10)
                devices.append(dev)
                print(f'    [{i:2d}] OK  {dev.device}')
            except Exception as e:
                print(f'    [{i:2d}] FAIL  {str(e)[:120]}')
                failed_at = i
                break

        # Check UDisks2 status
        alive = _udisks_alive()
        print(f'\n  After {len(devices)} loop devices: '
              f'UDisks2 {"ALIVE" if alive else "DEAD"}')

        # Cleanup
        for dev in devices:
            try:
                dev.delete(timeout=10)
            except Exception:
                pass
            dev.cleanup()

        with open(os.path.join(RESULT_DIR, 'loop_exhaustion.json'), 'w') as f:
            json.dump({
                'devices_created': len(devices),
                'failed_at': failed_at,
                'udisks_alive': alive,
            }, f, indent=2)

    # ── OOM / resource monitoring ──────────────────────────────────

    def test_resource_exhaustion_crash(self):
        """Monitor resource usage during stress to detect OOM kills."""
        if not self._log_monitor_available:
            self.skipTest('Log monitoring not available')

        monitor = LogMonitor(tags={'test': 'resource_exhaustion'})
        monitor.start()
        monitor.mark('stress_start')

        ok, crash_at, signal_counts = _stress_udisks(cycles=30, cooldown=0.0)
        monitor.mark('stress_end', ok=ok, crash_at=crash_at,
                     cycles=len(signal_counts))

        monitor.stop()
        monitor.save_report(os.path.join(RESULT_DIR, 'resource_exhaustion.json'))

        events = monitor.events()
        dmesg_entries = [e for e in events if e['source'] == 'dmesg']
        oom_lines = [e for e in dmesg_entries
                     if 'oom' in e.get('text', '').lower() or
                     'killed' in e.get('text', '').lower()]

        print(f'\n  Stress result: {"OK" if ok else f"CRASHED at cycle {crash_at}"}')
        print(f'  Cycles completed: {len(signal_counts)}')
        print(f'  dmesg entries: {len(dmesg_entries)}')
        print(f'  OOM/kill indicators in dmesg: {len(oom_lines)}')

        if oom_lines:
            print('  OOM/kill dmesg entries:')
            for e in oom_lines[:10]:
                print(f'    [{e["elapsed_s"]:.2f}s] {e["text"][:200]}')

        # Snapshot analysis
        snapshots = monitor.snapshots()
        dead_after = None
        for s in snapshots:
            if s.get('activestate') not in ('active', None) and s.get('activestate', '') != '':
                if dead_after is None:
                    dead_after = s['elapsed_s']
                    print(f'  UDisks2 went {s["activestate"]} at {s["elapsed_s"]:.1f}s')

        if dead_after is None and ok:
            print(f'  UDisks2 survived all {len(signal_counts)} stress cycles')
        elif dead_after is None and not ok:
            print(f'  UDisks2 non-responsive but systemd showed active state')

    # ── D-Bus connection leak ──────────────────────────────────────

    def test_dbus_connection_leak(self):
        """Does opening many D-Bus connections without cleanup crash UDisks2?"""
        print('\n  Testing D-Bus connection leak...')

        async def _leak_test(count=30):
            from dbus_fast.aio import MessageBus as AioMessageBus
            from dbus_fast import BusType

            buses = []
            for i in range(count):
                try:
                    bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
                    buses.append(bus)
                except Exception as e:
                    print(f'    connection {i} failed: {e}')
                    break

            dev = LoopDevice()
            try:
                dev.create(timeout=10)
                dev.delete(timeout=10)
                dev.cleanup()
                ops_ok = True
            except Exception:
                ops_ok = False
                dev.cleanup()

            for bus in buses:
                try:
                    bus.disconnect()
                except Exception:
                    pass

            alive = _udisks_alive()
            return len(buses), ops_ok, alive

        count, ops_ok, alive = asyncio.run(_leak_test(30))
        print(f'  Connections opened: {count}')
        print(f'  Loop ops ok: {ops_ok}')
        print(f'  UDisks2 alive: {alive}')

        with open(os.path.join(RESULT_DIR, 'connection_leak.json'), 'w') as f:
            json.dump({
                'connections': count,
                'ops_ok': ops_ok,
                'udisks_alive': alive,
            }, f, indent=2)

    # ── signal storm crash ─────────────────────────────────────────

    def test_signal_storm_crash(self):
        """Rapid mount/unmount cycles to see if signal storm crashes UDisks2."""
        print('\n  Testing signal storm (rapid mount/unmount)...')

        dev = LoopDevice()
        try:
            dev.create(timeout=10)
        except Exception as e:
            self.skipTest(f'loop-setup failed: {e}')

        self.addCleanup(dev.cleanup)

        monitor = None
        if self._log_monitor_available:
            monitor = LogMonitor(tags={'test': 'signal_storm'})
            monitor.start()
            monitor.mark('storm_start')

        results = []
        for i in range(10):
            try:
                dev.mount(timeout=10)
                dev.unmount(timeout=10)
                results.append(True)
            except Exception as e:
                results.append(False)
                print(f'    cycle {i}: FAIL - {str(e)[:100]}')

        if monitor:
            monitor.mark('storm_end', ok=all(results),
                         cycles=len(results))
            monitor.stop()
            monitor.save_report(os.path.join(RESULT_DIR, 'signal_storm.json'))
            print('\n' + monitor.report())

        ok_count = sum(results)
        print(f'\n  Mount/unmount cycles: {ok_count}/{len(results)} OK')
        print(f'  UDisks2 alive after: {_udisks_alive()}')

    # ── comprehensive crash analysis ───────────────────────────────

    def test_comprehensive_crash_analysis(self):
        """Full analysis: D-Bus + log monitoring + stress + recovery.
        Generates a definitive crash-evidence report."""
        if not self._log_monitor_available:
            self.skipTest('Log monitoring not available')

        monitor = LogMonitor(tags={'test': 'comprehensive_crash'})
        monitor.start()

        # 1. Establish baseline
        monitor.mark('baseline_check')
        pre_alive = _udisks_alive()
        pre_journal = _collect_pre_stress_journal(30)

        # 2. Stress with D-Bus monitoring
        print('\n  Phase 1: D-Bus stress while logging...')
        monitor.mark('phase1_start')

        async def _stressed_collect(cycles=12):
            from dbus_fast.aio import MessageBus as AioMessageBus
            from dbus_fast import BusType, Message, MessageType

            ok_count = 0
            crash_at = None
            for i in range(cycles):
                monitor.mark(f'cycle_{i}_start')
                try:
                    bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
                    reply = await bus.call(Message(
                        destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='AddMatch',
                        signature='s',
                        body=['type=signal'],
                    ))
                    monitor.mark(f'cycle_{i}_connected',
                                 addmatch_ok=reply.message_type != MessageType.ERROR)

                    dev = LoopDevice()
                    dev.create(timeout=10)
                    dev.delete(timeout=10)
                    dev.cleanup()
                    ok_count += 1
                    monitor.mark(f'cycle_{i}_ok')
                except Exception as e:
                    crash_at = i + 1
                    monitor.mark(f'cycle_{i}_fail', error=str(e)[:200])
                    try:
                        bus.disconnect()
                    except Exception:
                        pass
                    break
                try:
                    bus.disconnect()
                except Exception:
                    pass
                time.sleep(0.05)
            return ok_count, crash_at

        ok, crash_at = asyncio.run(_stressed_collect(12))
        monitor.mark('phase1_end', ok=(crash_at is None), ok_count=ok,
                     crash_at=crash_at)

        print(f'  Phase 1 result: {ok}/12 cycles OK, '
              f'crashed at cycle {crash_at}' if crash_at else '')

        # 3. Check recovery
        print('\n  Phase 2: Recovery analysis...')
        monitor.mark('phase2_start')
        recovery_checks = []
        for check_at in [1, 3, 5, 10, 15, 30]:
            time.sleep(min(check_at, 5))
            actual_elapsed = time.monotonic() - monitor._t0
            alive = _udisks_alive()
            recovery_checks.append({
                'elapsed_s': round(actual_elapsed, 1),
                'udisks_alive': alive,
            })
            monitor.mark(f'recovery_at_{check_at}s', alive=alive)
        monitor.mark('phase2_end')

        # 4. Post-analysis operation
        if _udisks_alive():
            print('\n  Phase 3: Post-recovery operation...')
            monitor.mark('phase3_start')
            try:
                dev = LoopDevice()
                dev.create(timeout=10)
                dev.delete(timeout=10)
                dev.cleanup()
                monitor.mark('phase3_ok')
                print('  Post-recovery: OK')
            except Exception as e:
                monitor.mark('phase3_fail', error=str(e)[:200])
                print(f'  Post-recovery: FAIL - {e}')

        monitor.stop()

        # Save evidence
        evidence_path = os.path.join(RESULT_DIR, 'comprehensive_crash.json')
        monitor.save_report(evidence_path)
        print(f'\n  Evidence saved: {evidence_path}')

        # Print report
        print('\n' + monitor.report())

        # Build final analysis
        events = monitor.events()
        journals = [e for e in events if e['source'] == 'journal-udisks2']
        snapshots = monitor.snapshots()

        dead_snaps = [s for s in snapshots
                      if s.get('activestate') not in ('active', None, '')]

        # Extract crash cause hypotheses from journal
        crash_lines = []
        for e in journals:
            text = e.get('text', '')
            if any(kw in text.lower() for kw in
                   ['error', 'fail', 'crash', 'abort', 'segfault',
                    'killed', 'oom', 'assert', 'timeout', 'signal']):
                crash_lines.append(e)

        final = {
            'pre_alive': pre_alive,
            'cycles_ok': ok,
            'crashed_at_cycle': crash_at,
            'recovery_checks': recovery_checks,
            'journal_entries': len(journals),
            'crash_indicators_in_journal': len(crash_lines),
            'crash_line_samples': [l.get('text', '')[:300] for l in crash_lines[:10]],
            'dead_snapshots': dead_snaps[:5],
            'post_recovery_ok': _udisks_alive(),
        }

        with open(os.path.join(RESULT_DIR, 'crash_analysis_final.json'), 'w') as f:
            json.dump(final, f, indent=2, default=str)

        print(f'\n  Final analysis:')
        print(f'    UDisks2 pre-stress: {"ALIVE" if pre_alive else "DEAD"}')
        print(f'    Cycles completed: {ok}'
              f'{f" (crashed at {crash_at})" if crash_at else ""}')
        print(f'    Journal crash indicators: {len(crash_lines)}')
        print(f'    Systemd dead snapshots: {len(dead_snaps)}')
        if recovery_checks:
            recovered = any(c['udisks_alive'] for c in recovery_checks)
            print(f'    Auto-recovered: {recovered}')

        # Generate markdown evidence
        md_path = os.path.join(RESULT_DIR, 'crash_evidence.md')
        with open(md_path, 'w') as f:
            f.write('# UDisks2 Crash Evidence\n\n')
            f.write(f'Generated: {time.strftime("%Y-%m-%d %H:%M:%S UTC")}\n\n')
            f.write('## Pre-Condition\n\n')
            f.write(f'- UDisks2 alive: {pre_alive}\n')
            f.write(f'- Journal baseline entries: {len(pre_journal.splitlines())}\n\n')
            f.write('## Stress Test\n\n')
            f.write(f'- Cycles completed: {ok}\n')
            f.write(f'- Crashed at cycle: {crash_at or "N/A (survived)"} \n\n')
            f.write('## Crash Indicators\n\n')
            if crash_lines:
                for l in crash_lines[:20]:
                    f.write(f'- `{l.get("text", "")[:250]}`\n')
            else:
                f.write('- No crash indicators found in journal\n')
            f.write('\n## Recovery\n\n')
            if recovery_checks:
                for c in recovery_checks:
                    f.write(f'- {c["elapsed_s"]:.1f}s: '
                            f'{"ALIVE" if c["udisks_alive"] else "DEAD"}\n')
            f.write('\n## State Snapshots\n\n')
            if dead_snaps:
                for s in dead_snaps[:10]:
                    f.write(f'- {s["elapsed_s"]:.1f}s: {s.get("activestate")} '
                            f'(PID={s.get("mainpid", "?")})\n')
            else:
                f.write('- No dead snapshots observed\n')

        print(f'  Markdown evidence: {md_path}')
