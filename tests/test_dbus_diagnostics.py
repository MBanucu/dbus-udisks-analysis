"""Diagnostic: why does AddMatch get 0 signals but udisksctl monitor works?

Probes the D-Bus daemon directly to find the root cause.
"""

import asyncio
import json
import os
import subprocess
import time
import unittest

from dbus_fast.aio import MessageBus as AioMessageBus
from dbus_fast import BusType, Message, MessageType

from tests.conftest import (
    LoopDevice,
    SignalCollector,
    add_match_message,
    ensure_dir,
    mts,
    print_system_info,
    udisksctl_available,
)

RESULT_DIR = os.path.join(os.path.dirname(__file__), '..', 'results')

MATCH_NO_SENDER = 'type=signal'
MATCH_UDISKS_SENDER = 'type=signal,sender=org.freedesktop.UDisks2'
MATCH_ALL_MEMBERS = (
    "type=signal,"
    "interface=org.freedesktop.DBus.ObjectManager,"
    "sender=org.freedesktop.UDisks2"
)
MATCH_ALL_PROPERTIES = (
    "type=signal,"
    "interface=org.freedesktop.DBus.Properties,"
    "sender=org.freedesktop.UDisks2"
)
MATCH_JOB_COMPLETED = (
    "type=signal,"
    "interface=org.freedesktop.UDisks2.Job,"
    "member=Completed,"
    "sender=org.freedesktop.UDisks2"
)


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestMatchRuleDiagnostics(unittest.TestCase):
    """Probe why our AddMatch rules get zero signals."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    # ── which D-Bus daemon? ───────────────────────────────────────

    def test_dbus_daemon_identity(self):
        """Identify the D-Bus daemon (dbus-daemon vs dbus-broker)."""
        info = {}
        for cmd, label in [
            ("busctl --system call org.freedesktop.DBus "
             "/org/freedesktop/DBus org.freedesktop.DBus "
             "GetId 2>/dev/null || echo 'FAIL'", 'bus_id'),
            ("ps --no-headers -eo comm,pid,args | "
             "grep -E 'dbus-(daemon|broker)' | grep -v grep | head -3 || echo 'none'",
             'ps_output'),
            ("busctl --system call org.freedesktop.DBus "
             "/org/freedesktop/DBus org.freedesktop.DBus "
             "ListNames 2>/dev/null | tr ' ' '\n' | "
             "grep -E 'UDisks2|udisks' | head -5 || echo 'none'",
             'udisks_names'),
            ("busctl --system call org.freedesktop.DBus "
             "/org/freedesktop/DBus org.freedesktop.DBus "
             "GetConnectionUnixProcessID s org.freedesktop.UDisks2 2>/dev/null || echo 'FAIL'",
             'udisks_pid'),
        ]:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            info[label] = r.stdout.strip()[:500]
            print(f'\n  {label}:\n    {r.stdout.strip()[:300]}')

        # Try BecomeMonitor (eavesdropping) — this is what udisksctl monitor uses
        r = subprocess.run(
            "busctl --system call org.freedesktop.DBus "
            "/org/freedesktop/DBus org.freedesktop.DBus.Monitoring "
            "BecomeMonitor asus 1 '' 0 2>/dev/null || echo 'FAIL (no Monitoring interface)'",
            shell=True, capture_output=True, text=True)
        info['become_monitor'] = r.stdout.strip()[:500]
        print(f'\n  BecomeMonitor:\n    {r.stdout.strip()[:300]}')

        with open(os.path.join(RESULT_DIR, 'dbus_daemon_info.json'), 'w') as f:
            json.dump(info, f, indent=2)

    # ── try different match rules ─────────────────────────────────

    async def _try_match_rule(self, match_body: str, label: str):
        """Subscribe with a specific match rule, run loop-setup, return signals."""
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
            body=[match_body],
        ))

        addmatch_ok = reply.message_type != MessageType.ERROR

        print(f'\n  [{label}] AddMatch: {"OK" if addmatch_ok else "FAIL"} '
              f'(rule: {match_body[:120]})')

        # Run loop-setup
        dev = LoopDevice()
        try:
            dev.create()
            time.sleep(1.5)
            dev.delete()
            time.sleep(0.5)
        except Exception as e:
            print(f'    loop-setup error: {e}')
        dev.cleanup()

        bus.disconnect()
        return signals, addmatch_ok

    def test_compare_match_rules(self):
        """Compare different match rule strings."""
        rules = [
            (MATCH_NO_SENDER, 'no_sender'),
            (MATCH_UDISKS_SENDER, 'udisks_sender'),
            (MATCH_ALL_MEMBERS, 'objectmanager'),
            (MATCH_ALL_PROPERTIES, 'properties'),
            (MATCH_JOB_COMPLETED, 'job_completed'),
        ]

        results = {}
        for rule_body, label in rules:
            signals, ok = asyncio.run(self._try_match_rule(rule_body, label))
            counts = {}
            for s in signals:
                key = f"{s['interface']}.{s['member']}"
                counts[key] = counts.get(key, 0) + 1
            results[label] = {
                'match_ok': ok,
                'signal_count': len(signals),
                'counts': counts,
                'senders': list({s['sender'] for s in signals}),
            }
            print(f'    signals: {len(signals)}  '
                  f'senders: {results[label]["senders"]}  '
                  f'counts: {counts}')

        with open(os.path.join(RESULT_DIR, 'match_rule_comparison.json'), 'w') as f:
            json.dump(results, f, indent=2, default=str)

    # ── raw message dump ──────────────────────────────────────────

    def test_raw_message_sender(self):
        """Capture ALL signal messages (no match filter) to see actual senders."""
        async def _dump():
            bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
            all_msgs = []

            def handler(msg):
                all_msgs.append({
                    'type': str(msg.message_type),
                    'path': msg.path or '',
                    'interface': msg.interface or '',
                    'member': msg.member or '',
                    'sender': msg.sender or '',
                    'destination': msg.destination or '',
                })

            bus.add_message_handler(handler)

            # AddMatch with NO filter at all — catch everything
            reply = await bus.call(Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='AddMatch',
                signature='s',
                body=[''],  # empty = match everything
            ))
            print(f'\n  AddMatch (empty rule): '
                  f'{"OK" if reply.message_type != MessageType.ERROR else "FAIL"}')

            time.sleep(0.5)
            dev = LoopDevice()
            try:
                dev.create()
            except Exception:
                pass
            time.sleep(2)
            dev.cleanup()

            bus.disconnect()
            return all_msgs

        all_msgs = asyncio.run(_dump())
        signals = [m for m in all_msgs if m['type'] == 'MessageType.SIGNAL']
        udisks_signals = [m for m in signals if
                          'UDisks2' in (m.get('interface', '') + m.get('path', '') + m.get('sender', ''))]

        print(f'\n  Total messages: {len(all_msgs)}')
        print(f'  Total signals: {len(signals)}')
        print(f'  UDisks2-related: {len(udisks_signals)}')

        senders = {}
        for s in signals:
            senders[s['sender']] = senders.get(s['sender'], 0) + 1
        print(f'\n  Signal senders:')
        for sender, count in sorted(senders.items(), key=lambda x: -x[1])[:20]:
            print(f'    {count:4d}  {sender}')

        if udisks_signals:
            print(f'\n  UDisks2 signals (first 20):')
            for s in udisks_signals[:20]:
                print(f'    sender={s["sender"]:40s}  '
                      f'{s["interface"]}.{s["member"]}  {s["path"]}')
        else:
            print(f'\n  NO UDisks2-related signals received with empty match rule!')

        data = {
            'total_messages': len(all_msgs),
            'total_signals': len(signals),
            'udisks_signals': len(udisks_signals),
            'senders': senders,
            'udisks_sample': udisks_signals[:20],
        }
        with open(os.path.join(RESULT_DIR, 'raw_signal_dump.json'), 'w') as f:
            json.dump(data, f, indent=2, default=str)

    # ── BecomeMonitor vs AddMatch ─────────────────────────────────

    def test_become_monitor_vs_addmatch(self):
        """Check if the D-Bus daemon supports BecomeMonitor.

        udisksctl monitor internally uses BecomeMonitor which is an
        eavesdropping mechanism that bypasses match rule delivery.
        """
        async def _check():
            bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()

            # Check if org.freedesktop.DBus.Monitoring interface exists
            reply = await bus.call(Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus.Introspectable',
                member='Introspect',
            ))
            introspect = reply.body[0] if reply.body else ''
            has_monitoring = 'Monitoring' in introspect

            # Try MatchRulesChanged signal
            reply2 = await bus.call(Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='GetConnectionCredentials',
                signature='s',
                body=['org.freedesktop.UDisks2'],
            ))

            bus.disconnect()
            return has_monitoring, reply2.body if reply2.body else ''

        has_mon, creds = asyncio.run(_check())
        print(f'\n  D-Bus Supports Monitoring interface: {has_mon}')
        print(f'  UDisks2 connection credentials: {str(creds)[:500]}')

        # Check udisksctl monitor strace to see what D-Bus calls it makes
        if has_mon:
            print(f'\n  EXPLANATION: udisksctl monitor uses BecomeMonitor '
                  f'which provides eavesdropping. Our AddMatch uses normal '
                  f'signal matching. On dbus-broker, signal delivery to '
                  f'match rules may differ from eavesdropping.')

    # ── UDisks2 signal introspection ──────────────────────────────

    def test_udisks2_object_manager_signals(self):
        """Introspect UDisks2 to see what interfaces it exports."""
        async def _introspect():
            bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
            reply = await bus.call(Message(
                destination='org.freedesktop.UDisks2',
                path='/org/freedesktop/UDisks2',
                interface='org.freedesktop.DBus.Introspectable',
                member='Introspect',
            ))
            bus.disconnect()
            return reply.body[0] if reply.body else ''

        xml = asyncio.run(_introspect())
        print(f'\n  UDisks2 introspection (first 2000 chars):')
        print(xml[:2000])

        with open(os.path.join(RESULT_DIR, 'udisks2_introspect.xml'), 'w') as f:
            f.write(xml)
