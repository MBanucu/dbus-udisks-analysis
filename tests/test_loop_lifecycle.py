"""Analyze signal ordering during loop device lifecycle.

What signals arrive, in what order, and with what timing gaps?
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
class TestLoopLifecycle(unittest.TestCase):
    """Deep analysis of signal sequence during loop-setup and loop-delete."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    def setUp(self):
        self.collector = SignalCollector()
        asyncio.run(self.collector.start())

    def tearDown(self):
        asyncio.run(self.collector.stop())

    # ── analysis helpers ──────────────────────────────────────────

    def _signal_sequence(self, signals):
        """Return ordered list of (interface, member, path_short) tuples."""
        seq = []
        for s in signals:
            short_path = s['path'].rsplit('/', 1)[-1] if s['path'] else ''
            seq.append((s['interface'], s['member'], short_path))
        return seq

    def _event_type(self, signal):
        """Return a human-readable event type for a signal."""
        iface = signal['interface']
        member = signal['member']
        path = signal['path']

        if iface == 'org.freedesktop.DBus.ObjectManager':
            if member == 'InterfacesAdded':
                if '/jobs/' in path:
                    return 'JobAdded'
                return 'InterfaceAdded'
            if member == 'InterfacesRemoved':
                if '/jobs/' in path:
                    return 'JobRemoved'
                return 'InterfaceRemoved'
        if iface == 'org.freedesktop.DBus.Properties':
            if member == 'PropertiesChanged':
                return 'PropertiesChanged'
        if iface == 'org.freedesktop.UDisks2.Job':
            if member == 'Completed':
                return 'JobCompleted'
        return f'{iface}.{member}'

    def _analyze_signals(self, label, signals):
        """Print detailed signal-by-signal analysis."""
        print(f'\n  ── {label} ({len(signals)} signals) ──')
        prev_elapsed = 0
        for i, s in enumerate(signals):
            gap = s['elapsed'] - prev_elapsed
            prev_elapsed = s['elapsed']
            etype = self._event_type(s)
            short_path = s['path'].rsplit('/', 1)[-1] if s['path'] else ''
            body_summary = ', '.join(
                str(item)[:80] for item in s['body'])
            print(f'  {i:3d}  +{gap*1000:7.1f}ms  {s["elapsed"]*1000:8.1f}ms  '
                  f'{etype:<22s}  {short_path:<30s}  {body_summary[:100]}')

        # Summary counts
        etypes = {}
        for s in signals:
            et = self._event_type(s)
            etypes[et] = etypes.get(et, 0) + 1
        print(f'\n  Event type counts:')
        for et, count in sorted(etypes.items()):
            print(f'    {count:3d}  {et}')

    # ── tests ─────────────────────────────────────────────────────

    def test_loop_setup_sequence(self):
        """Detailed signal sequence for loop-setup."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        self.collector.reset()
        t0 = mts()
        try:
            dev.create()
        except Exception as e:
            print(f'  loop-setup ERROR: {e}')
        time.sleep(1.0)

        self._analyze_signals('loop-setup sequence', self.collector.signals)

        # Full analysis to JSON
        analysis = {
            'operation': 'loop-setup',
            'total_signals': len(self.collector.signals),
            'duration_ms': round((mts() - t0) * 1000),
            'sequence': self._signal_sequence(self.collector.signals),
            'signals': [
                {
                    'i': i,
                    'elapsed_ms': round(s['elapsed'] * 1000, 1),
                    'event_type': self._event_type(s),
                    'interface': s['interface'],
                    'member': s['member'],
                    'path': s['path'],
                }
                for i, s in enumerate(self.collector.signals)
            ],
        }
        path = os.path.join(RESULT_DIR, 'loop_setup_sequence.json')
        with open(path, 'w') as f:
            json.dump(analysis, f, indent=2)

        # Observational checks (print, don't fail)
        etypes = set()
        for s in self.collector.signals:
            etypes.add(self._event_type(s))

        expected_types = {'InterfaceAdded', 'JobAdded', 'JobCompleted',
                          'JobRemoved', 'PropertiesChanged'}
        missing = expected_types - etypes
        if missing:
            print(f'\n  NOTE: Missing event types in loop-setup: {missing}')
        extra = etypes - expected_types
        if extra:
            print(f'  NOTE: Unexpected event types: {extra}')

    def test_loop_delete_sequence(self):
        """Detailed signal sequence for loop-delete."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        dev.create()
        time.sleep(1)
        self.collector.reset()

        t0 = mts()
        try:
            dev.delete()
        except Exception as e:
            print(f'  loop-delete ERROR: {e}')
        time.sleep(1.0)

        self._analyze_signals('loop-delete sequence', self.collector.signals)

        analysis = {
            'operation': 'loop-delete',
            'total_signals': len(self.collector.signals),
            'duration_ms': round((mts() - t0) * 1000),
            'sequence': self._signal_sequence(self.collector.signals),
            'signals': [
                {
                    'i': i,
                    'elapsed_ms': round(s['elapsed'] * 1000, 1),
                    'event_type': self._event_type(s),
                    'interface': s['interface'],
                    'member': s['member'],
                    'path': s['path'],
                }
                for i, s in enumerate(self.collector.signals)
            ],
        }
        path = os.path.join(RESULT_DIR, 'loop_delete_sequence.json')
        with open(path, 'w') as f:
            json.dump(analysis, f, indent=2)

    def test_inter_signal_gaps(self):
        """Measure gaps between consecutive signals."""
        dev = LoopDevice()
        self.addCleanup(dev.cleanup)

        self.collector.reset()
        dev.create()
        time.sleep(1)
        signals = self.collector.signals

        if len(signals) < 2:
            print('  Not enough signals for gap analysis')
            return

        gaps = []
        for i in range(1, len(signals)):
            gap = signals[i]['elapsed'] - signals[i - 1]['elapsed']
            gaps.append(round(gap * 1000, 1))

        print(f'\n  Inter-signal gaps (ms):')
        print(f'    min: {min(gaps):.1f}')
        print(f'    max: {max(gaps):.1f}')
        print(f'    mean: {sum(gaps) / len(gaps):.1f}')
        print(f'    gaps: {gaps}')
