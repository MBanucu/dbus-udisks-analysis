"""Diagnostic: does D-Bus daemon state leak across connection cycles?

UDisks2 signal emission degrades after ~25 dbus-fast connect/disconnect
cycles.  This test checks whether the D-Bus daemon itself accumulates
leaked state (connections, match rules, names) across cycles.

Uses `sudo busctl call ...Debug.Stats.GetStats` to sample daemon
statistics before and after repeated connect/disconnect cycles with
AddMatch/RemoveMatch.
"""

import asyncio
import json
import os
import re
import subprocess
import time
import unittest

from dbus_fast.aio import MessageBus as AioMessageBus
from dbus_fast import BusType, Message, MessageType

from tests.conftest import (
    ensure_dir,
    print_system_info,
    udisksctl_available,
)

RESULT_DIR = os.path.join(os.path.dirname(__file__), '..', 'results')

MATCH_RULE = "type=signal,path_namespace='/org/freedesktop/UDisks2'"
NUM_CYCLES = 30


# ── D-Bus daemon stats sampling ────────────────────────────────────

def _daemon_stats():
    """Sample D-Bus daemon statistics via Debug.Stats.GetStats.

    Returns a dict with parsed connection count, match rule count,
    and bus names count, or None if stats are unavailable.
    """
    r = subprocess.run(
        ['sudo', 'busctl', '--system', 'call',
         'org.freedesktop.DBus', '/org/freedesktop/DBus',
         'org.freedesktop.DBus.Debug.Stats', 'GetStats'],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return None

    raw = r.stdout.strip()
    m = re.search(r'"ActiveConnections"\s*u\s*(\d+)', raw)
    active = int(m.group(1)) if m else None
    m = re.search(r'"BusNames"\s*u\s*(\d+)', raw)
    names = int(m.group(1)) if m else None
    m = re.search(r'"MatchRules"\s*u\s*(\d+)', raw)
    rules = int(m.group(1)) if m else None
    m = re.search(r'"PeakMatchRules"\s*u\s*(\d+)', raw)
    peak_rules = int(m.group(1)) if m else None
    m = re.search(r'"PeakBusNames"\s*u\s*(\d+)', raw)
    peak_names = int(m.group(1)) if m else None

    return {
        'active_connections': active,
        'bus_names': names,
        'match_rules': rules,
        'peak_match_rules': peak_rules,
        'peak_bus_names': peak_names,
    }


def _conn_stats(conn_id):
    """Sample per-connection stats."""
    r = subprocess.run(
        ['sudo', 'busctl', '--system', 'call',
         'org.freedesktop.DBus', '/org/freedesktop/DBus',
         'org.freedesktop.DBus.Debug.Stats', 'GetConnectionStats',
         's', conn_id],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return None

    raw = r.stdout.strip()
    m = re.search(r'"MatchBytes"\s*u\s*(\d+)', raw)
    match_bytes = int(m.group(1)) if m else None
    m = re.search(r'"Matches"\s*u\s*(\d+)', raw)
    matches = int(m.group(1)) if m else None
    m = re.search(r'"NameObjects"\s*u\s*(\d+)', raw)
    name_objects = int(m.group(1)) if m else None
    m = re.search(r'"ReplyObjects"\s*u\s*(\d+)', raw)
    reply_objects = int(m.group(1)) if m else None

    return {
        'match_bytes': match_bytes,
        'matches': matches,
        'name_objects': name_objects,
        'reply_objects': reply_objects,
    }


def _list_connection_ids():
    """Return list of all :1.N connection IDs from bus ListNames."""
    r = subprocess.run(
        ['busctl', '--system', 'list'],
        capture_output=True, text=True, timeout=10)
    ids = []
    for line in r.stdout.splitlines():
        m = re.search(r'(:\d+\.\d+)\s', line)
        if m:
            ids.append(m.group(1))
    return ids


# ── cycle helpers ──────────────────────────────────────────────────

async def _connect_addmatch_disconnect():
    """Open a dbus-fast connection, AddMatch, RemoveMatch, disconnect."""
    bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
    reply = await bus.call(Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='AddMatch',
        signature='s',
        body=[MATCH_RULE],
    ))
    if reply.message_type != MessageType.ERROR:
        await bus.call(Message(
            destination='org.freedesktop.DBus',
            path='/org/freedesktop/DBus',
            interface='org.freedesktop.DBus',
            member='RemoveMatch',
            signature='s',
            body=[MATCH_RULE],
        ))
    bus.disconnect()


# ── test ───────────────────────────────────────────────────────────

@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestBusDaemonState(unittest.TestCase):
    """Check whether D-Bus daemon state leaks across connect/disconnect
    cycles with dbus-fast."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def test_daemon_state_across_cycles(self):
        stats_before = _daemon_stats()
        conn_ids_before = _list_connection_ids()

        if stats_before is None:
            self.skipTest('D-Bus Debug.Stats not available')

        print(f'\n  Before cycles:')
        print(f'    ActiveConnections: {stats_before["active_connections"]}')
        print(f'    BusNames:          {stats_before["bus_names"]}')
        print(f'    MatchRules:        {stats_before["match_rules"]}')
        print(f'    PeakMatchRules:    {stats_before["peak_match_rules"]}')

        # Run connect/disconnect cycles
        for cycle in range(NUM_CYCLES):
            asyncio.run(_connect_addmatch_disconnect())

        # Allow last disconnect to fully tear down on the daemon side
        time.sleep(2)

        stats_after = _daemon_stats()
        conn_ids_after = _list_connection_ids()

        if stats_after is None:
            self.skipTest('D-Bus Debug.Stats unavailable after cycles')

        print(f'\n  After {NUM_CYCLES} connect+AddMatch+RemoveMatch+disconnect:')
        print(f'    ActiveConnections: {stats_after["active_connections"]}')
        print(f'    BusNames:          {stats_after["bus_names"]}')
        print(f'    MatchRules:        {stats_after["match_rules"]}')
        print(f'    PeakMatchRules:    {stats_after["peak_match_rules"]}')

        conn_delta = (stats_after['active_connections']
                      - stats_before['active_connections'])
        rules_delta = (stats_after['match_rules']
                       - stats_before['match_rules'])

        print(f'\n  Delta:')
        print(f'    ActiveConnections: {conn_delta:+d}')
        print(f'    MatchRules:        {rules_delta:+d}')

        stale_conns = set(conn_ids_after) - set(conn_ids_before)
        if stale_conns:
            print(f'\n  Stale connections ({len(stale_conns)}):')
            for cid in sorted(stale_conns, key=lambda x: int(x.split('.')[1])):
                cs = _conn_stats(cid)
                if cs:
                    print(f'    {cid}: matches={cs["matches"]}, '
                          f'match_bytes={cs["match_bytes"]}, '
                          f'name_objects={cs["name_objects"]}, '
                          f'reply_objects={cs["reply_objects"]}')

        result = {
            'cycles': NUM_CYCLES,
            'before': stats_before,
            'after': stats_after,
            'delta': {
                'active_connections': conn_delta,
                'match_rules': rules_delta,
                'bus_names': stats_after['bus_names']
                             - stats_before['bus_names'],
            },
            'stale_connections': list(stale_conns) if stale_conns else [],
        }

        path = os.path.join(RESULT_DIR, 'bus_daemon_state.json')
        with open(path, 'w') as f:
            json.dump(result, f, indent=2)

        # ActiveConnections may vary ±2 from ambient process
        # connect/disconnect unrelated to our cycles.
        self.assertLessEqual(
            abs(conn_delta), 2,
            f'ActiveConnections changed by {conn_delta:+d} after {NUM_CYCLES} '
            f'cycles (before={stats_before["active_connections"]}, '
            f'after={stats_after["active_connections"]}). '
            f'Expected ±0 (our connections are all disconnected).')

        self.assertLessEqual(
            rules_delta, 1,
            f'MatchRules grew by {rules_delta} after {NUM_CYCLES} cycles '
            f'(before={stats_before["match_rules"]}, '
            f'after={stats_after["match_rules"]}). '
            f'Each cycle calls both AddMatch and RemoveMatch — '
            f'match rules should not accumulate.')

        self.assertFalse(
            len(stale_conns) > 2,
            f'{len(stale_conns)} stale connections found after '
            f'{NUM_CYCLES} cycles: {stale_conns}')
        if stale_conns:
            print(f'\n  NOTE: {len(stale_conns)} connections still tearing '
                  f'down (timing artifact): {stale_conns}')
