"""Diagnostic: does path_namespace in AddMatch drop Job.Completed?

Compares two match rules side-by-side on the same operation sequence
(loop-setup + loop-delete):
  1. type=signal (bare, no filter) — captures everything
  2. type=signal,path_namespace=/org/freedesktop/UDisks2 — UDisks2 only

If Job.Completed appears in rule 1 but not rule 2, the D-Bus daemon
on this runner drops the signal when path_namespace is present.
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

MATCH_BARE = 'type=signal'
MATCH_PATH_NS = "type=signal,path_namespace='/org/freedesktop/UDisks2'"


async def _run_with_rule(rule_body):
    """Connect with one match rule, run loop-setup+delete, return signals."""
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
        body=[rule_body],
    ))
    match_ok = reply.message_type != MessageType.ERROR
    if not match_ok:
        print(f'  AddMatch FAILED: {reply.body}')

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
    return signals, match_ok, ops_ok


def _count_by_iface_member(signals):
    counts = {}
    for s in signals:
        key = f"{s['interface']}.{s['member']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _job_completed_count(signals):
    return sum(
        1 for s in signals
        if s['interface'] == 'org.freedesktop.UDisks2.Job'
        and s['member'] == 'Completed'
    )


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestPathNamespaceDiagnostic(unittest.TestCase):
    """Compare bare type=signal vs path_namespace match rules."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def test_path_namespace_vs_bare(self):
        bare_signals, bare_match_ok, bare_ops_ok = asyncio.run(
            _run_with_rule(MATCH_BARE))
        ns_signals, ns_match_ok, ns_ops_ok = asyncio.run(
            _run_with_rule(MATCH_PATH_NS))

        bare_jc = _job_completed_count(bare_signals)
        ns_jc = _job_completed_count(ns_signals)

        result = {
            'bare': {
                'rule': MATCH_BARE,
                'match_ok': bare_match_ok,
                'ops_ok': bare_ops_ok,
                'total_signals': len(bare_signals),
                'job_completed_count': bare_jc,
                'counts': _count_by_iface_member(bare_signals),
            },
            'path_namespace': {
                'rule': MATCH_PATH_NS,
                'match_ok': ns_match_ok,
                'ops_ok': ns_ops_ok,
                'total_signals': len(ns_signals),
                'job_completed_count': ns_jc,
                'counts': _count_by_iface_member(ns_signals),
            },
            'conclusion': {
                'bare_has_job_completed': bare_jc > 0,
                'path_namespace_has_job_completed': ns_jc > 0,
                'job_completed_dropped_by_path_namespace':
                    bare_jc > 0 and ns_jc == 0,
                'bare_to_path_namespace_ratio':
                    round(ns_jc / bare_jc, 3) if bare_jc > 0 else None,
            },
        }

        path = os.path.join(RESULT_DIR, 'path_namespace_comparison.json')
        with open(path, 'w') as f:
            json.dump(result, f, indent=2)

        print(f'\n  Match rule comparison:')
        print(f'    bare:              {bare_jc} JobCompleted / '
              f'{len(bare_signals)} total signals')
        print(f'    path_namespace:    {ns_jc} JobCompleted / '
              f'{len(ns_signals)} total signals')

        if not bare_ops_ok or not ns_ops_ok:
            self.skipTest('operations failed')

        if _job_completed_count(bare_signals) == 0:
            self.skipTest(
                'Bare type=signal got zero JobCompleted — '
                'UDisks2 not emitting signals on this runner')

        if ns_jc == 0 and bare_jc > 0:
            print('\n  *** BUG CONFIRMED: path_namespace match rule '
                  'drops org.freedesktop.UDisks2.Job.Completed ***')
