"""Verify the fix: AddMatch without sender=org.freedesktop.UDisks2.

Tests that match rules using interface/member filtering (without
sender=) correctly receive UDisks2 signals on GitHub Actions.
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

MATCH_INTERFACES = (
    "type=signal,"
    "interface=org.freedesktop.DBus.ObjectManager,"
    "member=InterfacesAdded"
)

MATCH_PROPERTIES = (
    "type=signal,"
    "interface=org.freedesktop.DBus.Properties,"
    "member=PropertiesChanged"
)

MATCH_INTERFACES_REMOVED = (
    "type=signal,"
    "interface=org.freedesktop.DBus.ObjectManager,"
    "member=InterfacesRemoved"
)

MATCH_JOB = (
    "type=signal,"
    "interface=org.freedesktop.UDisks2.Job,"
    "member=Completed"
)


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestFixVerification(unittest.TestCase):
    """Verify that removing sender= fixes signal delivery."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    async def _collect_with_rules(self, rule_bodies):
        """Subscribe with multiple match rules, run loop-setup, return (signals, ops_ok)."""
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

        for body in rule_bodies:
            reply = await bus.call(Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='AddMatch',
                signature='s',
                body=[body],
            ))
            if reply.message_type == MessageType.ERROR:
                print(f'  AddMatch FAILED: {body[:100]}')

        # Run operations
        ops_ok = False
        dev = LoopDevice()
        try:
            dev.create()
            ops_ok = True
            time.sleep(1.5)
            dev.delete()
            time.sleep(1)
        except Exception as e:
            print(f'  op error: {e}')
        dev.cleanup()

        bus.disconnect()
        return signals, ops_ok

    def test_no_sender_filter_receives_udisks_signals(self):
        """Match rule without sender= should receive UDisks2 signals."""
        signals, ops_ok = asyncio.run(
            self._collect_with_rules([MATCH_NO_SENDER]))

        udisks_signals = [s for s in signals if
                          'UDisks2' in (s.get('interface', '') +
                                        s.get('path', ''))]
        print(f'\n  Total signals: {len(signals)}')
        print(f'  UDisks2-related: {len(udisks_signals)}')
        for s in udisks_signals[:10]:
            print(f'    {s["interface"]}.{s["member"]}  {s["path"]}')

        if not ops_ok:
            self.skipTest('loop-setup failed — UDisks2 not responding on this runner')
        self.assertGreater(len(udisks_signals), 0,
                           'No UDisks2 signals received even without sender filter!')

    def test_interface_member_filter_receives_signals(self):
        """Specific interface+member filters (no sender=) should work."""
        signals, ops_ok = asyncio.run(
            self._collect_with_rules([
                MATCH_INTERFACES,
                MATCH_PROPERTIES,
                MATCH_INTERFACES_REMOVED,
                MATCH_JOB,
            ]))

        interfaces_added = [s for s in signals
                            if s['member'] == 'InterfacesAdded']
        interfaces_removed = [s for s in signals
                             if s['member'] == 'InterfacesRemoved']
        properties_changed = [s for s in signals
                             if s['member'] == 'PropertiesChanged']
        job_completed = [s for s in signals
                          if s['member'] == 'Completed']

        print(f'\n  Signals received:')
        print(f'    InterfacesAdded:   {len(interfaces_added)}')
        print(f'    InterfacesRemoved: {len(interfaces_removed)}')
        print(f'    PropertiesChanged: {len(properties_changed)}')
        print(f'    Job.Completed:     {len(job_completed)}')

        # Filter to only UDisks2-related
        udisks_ia = [s for s in interfaces_added
                     if '/org/freedesktop/UDisks2' in s['path']]
        udisks_pc = [s for s in properties_changed
                     if '/org/freedesktop/UDisks2' in s['path']]

        print(f'    UDisks2 InterfacesAdded:   {len(udisks_ia)}')
        print(f'    UDisks2 PropertiesChanged: {len(udisks_pc)}')

        if not ops_ok:
            self.skipTest('loop-setup failed — UDisks2 not responding on this runner')
        self.assertGreater(len(udisks_ia), 0,
                           'No UDisks2 InterfacesAdded received')
        self.assertGreater(len(udisks_pc), 0,
                           'No UDisks2 PropertiesChanged received')

    def test_broad_match_filter_works(self):
        """Catch-all: type=signal with filter in handler matches udisks-monitor approach."""
        signals, ops_ok = asyncio.run(
            self._collect_with_rules([MATCH_NO_SENDER]))

        # Filter in handler (matching udisks-monitor's _on_message logic)
        udisks_sigs = []
        for s in signals:
            iface = s.get('interface', '')
            member = s.get('member', '')
            path = s.get('path', '')

            # Match UDisks2 paths
            if '/org/freedesktop/UDisks2' not in path:
                continue

            if iface == 'org.freedesktop.DBus.ObjectManager':
                if member in ('InterfacesAdded', 'InterfacesRemoved'):
                    udisks_sigs.append(s)
            elif iface == 'org.freedesktop.DBus.Properties':
                if member == 'PropertiesChanged':
                    udisks_sigs.append(s)
            elif iface == 'org.freedesktop.UDisks2.Job':
                if member == 'Completed':
                    udisks_sigs.append(s)

        expected = {'InterfacesAdded', 'InterfacesRemoved',
                    'PropertiesChanged', 'Completed'}
        seen = {s['member'] for s in udisks_sigs}
        missing = expected - seen

        print(f'\n  UDisks2 signals (filtered in handler): {len(udisks_sigs)}')
        print(f'  Expected members: {expected}')
        print(f'  Seen: {seen}')
        if missing:
            print(f'  Missing: {missing}')

        # Write results
        data = {
            'approach': 'broad match + handler filter',
            'total_signals': len(signals),
            'udisks_signals': len(udisks_sigs),
            'seen_members': list(seen),
            'missing_members': list(missing),
        }
        with open(os.path.join(RESULT_DIR, 'fix_verification.json'), 'w') as f:
            json.dump(data, f, indent=2)

        if not ops_ok:
            self.skipTest('loop-setup failed — UDisks2 not responding on this runner')
        self.assertGreater(len(udisks_sigs), 5,
                           f'Only {len(udisks_sigs)} UDisks2 signals '
                           f'(expected >5). Missing: {missing}')
