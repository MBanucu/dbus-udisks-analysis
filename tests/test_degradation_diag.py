"""Diagnostic: does UDisks2 stop emitting loop-setup JobCompleted
after multiple D-Bus connection cycles?

Simulates the udisks-monitor test suite pattern: parity tests create
multiple D-Bus connections before the signal completeness tests run.
Each "prior" connection does a loop-setup+delete cycle.  After N
cycles, a final connection checks whether loop-setup still emits
JobCompleted.
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
    print_system_info,
    udisksctl_available,
)

RESULT_DIR = os.path.join(os.path.dirname(__file__), '..', 'results')

MATCH_RULE = "type=signal,path_namespace='/org/freedesktop/UDisks2'"


async def _dbus_cycle():
    """Open a D-Bus connection, do loop-setup+delete, close."""
    bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
    reply = await bus.call(Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='AddMatch',
        signature='s',
        body=[MATCH_RULE],
    ))
    match_ok = reply.message_type != MessageType.ERROR

    dev = LoopDevice()
    try:
        dev.create()
        time.sleep(0.5)
        dev.delete()
        time.sleep(0.5)
        ops_ok = True
    except Exception:
        ops_ok = False
    dev.cleanup()
    bus.disconnect()
    return match_ok, ops_ok


async def _check_job_completed():
    """Connect, do loop-setup only, collect signals, check for JobCompleted."""
    bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
    signals = []

    def handler(msg):
        if msg.message_type == MessageType.SIGNAL:
            signals.append({
                'path': msg.path or '',
                'interface': msg.interface or '',
                'member': msg.member or '',
            })

    bus.add_message_handler(handler)
    reply = await bus.call(Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='AddMatch',
        signature='s',
        body=[MATCH_RULE],
    ))
    if reply.message_type == MessageType.ERROR:
        bus.disconnect()
        return 0, 0, False

    ops_ok = False
    dev = LoopDevice()
    try:
        dev.create()
        ops_ok = True
        time.sleep(1.5)
    except Exception:
        pass
    dev.cleanup()
    bus.disconnect()

    jc = sum(1 for s in signals
             if s['interface'] == 'org.freedesktop.UDisks2.Job'
             and s['member'] == 'Completed')
    return jc, len(signals), ops_ok


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestDegradationDiagnostic(unittest.TestCase):
    """Check if UDisks2 degrades after D-Bus connection cycles."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def test_job_completed_after_connection_cycles(self):
        results = []

        # Cycle 0: baseline — check JobCompleted on current UDisks2
        jc, total, ops_ok = asyncio.run(_check_job_completed())
        results.append({
            'cycle': 0, 'phase': 'check',
            'job_completed': jc, 'total_signals': total,
            'ops_ok': ops_ok,
        })
        print(f'\n  cycle 0 (baseline): {jc} JC / {total} signals')

        # Cycles 1-4: simulate parity test D-Bus connections
        for cycle in range(1, 5):
            match_ok, ops_ok = asyncio.run(_dbus_cycle())
            results.append({
                'cycle': cycle, 'phase': 'stress',
                'ops_ok': ops_ok,
            })
            print(f'  cycle {cycle} (stress): match_ok={match_ok}, '
                  f'ops_ok={ops_ok}')

        # After stress: check JobCompleted again
        jc, total, ops_ok = asyncio.run(_check_job_completed())
        results.append({
            'cycle': 5, 'phase': 'check',
            'job_completed': jc, 'total_signals': total,
            'ops_ok': ops_ok,
        })
        print(f'  cycle 5 (after stress): {jc} JC / {total} signals')

        # Degradation: if baseline had JC but after stress doesn't
        degraded = (results[0]['job_completed'] > 0
                    and results[-1]['job_completed'] == 0)

        artifact = {
            'results': results,
            'conclusion': {
                'baseline_jc': results[0]['job_completed'],
                'after_stress_jc': results[-1]['job_completed'],
                'degraded': degraded,
                'stress_cycles': sum(
                    1 for r in results if r['phase'] == 'stress'),
            },
        }

        path = os.path.join(RESULT_DIR, 'degradation_diag.json')
        with open(path, 'w') as f:
            json.dump(artifact, f, indent=2)

        if degraded:
            print('\n  *** DEGRADATION CONFIRMED: UDisks2 stopped emitting '
                  'loop-setup JobCompleted after D-Bus connection cycling ***')
