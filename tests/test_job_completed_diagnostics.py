"""Diagnostic: does UDisks2 emit JobCompleted for loop-setup?

Tests loop-setup and loop-delete in separate capture windows to
determine whether org.freedesktop.UDisks2.Job.Completed is emitted
for each operation independently.  Produces JSON artifacts for
offline comparison across UDisks2 versions.
"""

import asyncio
import json
import os
import time
import unittest

from dbus_fast.aio import MessageBus as AioMessageBus
from dbus_fast import BusType, Message, MessageType

from tests.conftest import (
    LoopDevice,
    ensure_dir,
    mts,
    print_system_info,
    udisksctl_available,
)

RESULT_DIR = os.path.join(os.path.dirname(__file__), '..', 'results')

MATCH_NO_SENDER = 'type=signal'


async def _capture_setup():
    """Capture signals during loop-setup only. Returns (signals, ops_ok)."""
    bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
    signals = []

    def handler(msg):
        if msg.message_type == MessageType.SIGNAL:
            signals.append({
                'path': msg.path or '',
                'interface': msg.interface or '',
                'member': msg.member or '',
                'sender': msg.sender or '',
            })

    bus.add_message_handler(handler)
    reply = await bus.call(Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='AddMatch',
        signature='s',
        body=[MATCH_NO_SENDER],
    ))
    if reply.message_type == MessageType.ERROR:
        print(f'  AddMatch failed: {reply.body}')
        bus.disconnect()
        return signals, False

    ops_ok = False
    dev = LoopDevice()
    try:
        dev.create()
        ops_ok = True
        time.sleep(1.5)
    except Exception as e:
        print(f'  loop-setup error: {e}')
    dev.cleanup()

    bus.disconnect()
    return signals, ops_ok


async def _capture_delete():
    """Capture signals during loop-delete only. Returns (signals, ops_ok)."""
    bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
    signals = []

    def handler(msg):
        if msg.message_type == MessageType.SIGNAL:
            signals.append({
                'path': msg.path or '',
                'interface': msg.interface or '',
                'member': msg.member or '',
                'sender': msg.sender or '',
            })

    bus.add_message_handler(handler)
    reply = await bus.call(Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='AddMatch',
        signature='s',
        body=[MATCH_NO_SENDER],
    ))
    if reply.message_type == MessageType.ERROR:
        print(f'  AddMatch failed: {reply.body}')
        bus.disconnect()
        return signals, False

    ops_ok = False
    dev = LoopDevice()
    try:
        dev.create()
        time.sleep(1)
        dev.delete()
        ops_ok = True
        time.sleep(1.5)
    except Exception as e:
        print(f'  op error: {e}')
    dev.cleanup()

    bus.disconnect()
    return signals, ops_ok


def _count_job_completed(signals):
    """Count and return JobCompleted signals from a signal list."""
    return [s for s in signals
            if s['interface'] == 'org.freedesktop.UDisks2.Job'
            and s['member'] == 'Completed']


def _count_by_interface_member(signals):
    """Count signals by interface.member key."""
    counts = {}
    for s in signals:
        key = f"{s['interface']}.{s['member']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestJobCompletedDiagnostics(unittest.TestCase):
    """Check whether UDisks2 emits JobCompleted for loop-setup,
    loop-delete, or both."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    # ── individual operation tests ─────────────────────────────────

    def test_job_completed_in_loop_setup(self):
        """Capture signals during loop-setup only, count JobCompleted."""
        signals, ops_ok = asyncio.run(_capture_setup())

        jc = _count_job_completed(signals)
        counts = _count_by_interface_member(signals)

        result = {
            'operation': 'loop-setup',
            'ops_ok': ops_ok,
            'total_signals': len(signals),
            'job_completed_count': len(jc),
            'job_completed': jc,
            'interface_counts': counts,
        }

        path = os.path.join(RESULT_DIR, 'job_completed_setup.json')
        with open(path, 'w') as f:
            json.dump(result, f, indent=2)

        print(f'\n  loop-setup JobCompleted: {len(jc)} / {len(signals)} signals')
        for key, count in sorted(counts.items()):
            print(f'    {key}: {count}')

        if not ops_ok:
            self.skipTest('loop-setup failed — UDisks2 not responsive')

        if len(jc) == 0:
            print('  WARNING: JobCompleted NOT emitted for loop-setup '
                  'on this environment')

    def test_job_completed_in_loop_delete(self):
        """Capture signals during loop-delete, count JobCompleted."""
        signals, ops_ok = asyncio.run(_capture_delete())

        jc = _count_job_completed(signals)
        counts = _count_by_interface_member(signals)

        result = {
            'operation': 'loop-delete',
            'ops_ok': ops_ok,
            'total_signals': len(signals),
            'job_completed_count': len(jc),
            'job_completed': jc,
            'interface_counts': counts,
        }

        path = os.path.join(RESULT_DIR, 'job_completed_delete.json')
        with open(path, 'w') as f:
            json.dump(result, f, indent=2)

        print(f'\n  loop-delete JobCompleted: {len(jc)} / {len(signals)} signals')
        for key, count in sorted(counts.items()):
            print(f'    {key}: {count}')

        if not ops_ok:
            self.skipTest('loop-delete failed — UDisks2 not responsive')

        udisks_total = sum(
            v for k, v in counts.items()
            if 'UDisks2' in k or '/org/freedesktop/UDisks2' in k)
        if udisks_total == 0:
            self.skipTest(
                f'No UDisks2 signals received from loop-delete '
                f'(total={len(signals)}, counts={counts})')

        self.assertGreater(
            len(jc), 0,
            f'Expected at least 1 JobCompleted from loop-delete, got 0. '
            f'Total signals: {len(signals)}. '
            f'Interface counts: {counts}'
        )

    # ── comparison test — single unified artifact ──────────────────

    def test_job_completed_comparison(self):
        """Compare JobCompleted across setup and delete in one artifact."""
        setup_signals, setup_ok = asyncio.run(_capture_setup())
        delete_signals, delete_ok = asyncio.run(_capture_delete())

        setup_jc = _count_job_completed(setup_signals)
        delete_jc = _count_job_completed(delete_signals)

        result = {
            'setup': {
                'ops_ok': setup_ok,
                'total_signals': len(setup_signals),
                'job_completed_count': len(setup_jc),
                'job_completed_present': len(setup_jc) > 0,
                'counts': _count_by_interface_member(setup_signals),
            },
            'delete': {
                'ops_ok': delete_ok,
                'total_signals': len(delete_signals),
                'job_completed_count': len(delete_jc),
                'job_completed_present': len(delete_jc) > 0,
                'counts': _count_by_interface_member(delete_signals),
            },
            'conclusion': {
                'setup_has_job_completed': len(setup_jc) > 0,
                'delete_has_job_completed': len(delete_jc) > 0,
                'parity': len(setup_jc) == len(delete_jc),
            },
        }

        path = os.path.join(RESULT_DIR, 'job_completed_comparison.json')
        with open(path, 'w') as f:
            json.dump(result, f, indent=2)

        print(f'\n  JobCompleted comparison:')
        print(f'    loop-setup: {len(setup_jc)} / {len(setup_signals)} signals')
        print(f'    loop-delete: {len(delete_jc)} / {len(delete_signals)} signals')

        if not setup_ok or not delete_ok:
            self.skipTest('operations failed — UDisks2 not responsive')

        self.assertGreater(
            len(delete_jc), 0,
            f'Expected at least 1 JobCompleted from loop-delete, got 0. '
            f'Setup JC={len(setup_jc)}, Delete JC={len(delete_jc)}')
