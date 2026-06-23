"""Measure timing: activation latency, signal delivery, operation duration.

How fast is UDisks2? How much overhead does D-Bus monitoring add?
"""

import asyncio
import json
import os
import subprocess
import time
import unittest

from dbus_fast.aio import MessageBus as AioMessageBus
from dbus_fast import BusType, Message

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


@unittest.skipUnless(udisksctl_available(), 'udisksctl not available')
class TestTiming(unittest.TestCase):
    """Measure timing characteristics."""

    @classmethod
    def setUpClass(cls):
        ensure_dir(RESULT_DIR)
        print_system_info()

    # ── UDisks2 activation ────────────────────────────────────────

    def test_udisks2_activation_latency(self):
        """How long does UDisks2 take to activate from cold start?"""
        async def _measure():
            t0 = mts()
            bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
            t1 = mts()
            reply = await bus.call(Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='NameHasOwner',
                signature='s',
                body=['org.freedesktop.UDisks2'],
            ))
            t2 = mts()
            bus.disconnect()
            return {
                'connect_ms': round((t1 - t0) * 1000, 1),
                'name_check_ms': round((t2 - t1) * 1000, 1),
            }

        result = asyncio.run(_measure())
        print(f'\n  D-Bus connect: {result["connect_ms"]}ms')
        print(f'  NameHasOwner:  {result["name_check_ms"]}ms')

        has_owner = subprocess.run(
            "busctl --system call org.freedesktop.DBus "
            "/org/freedesktop/DBus org.freedesktop.DBus "
            "NameHasOwner s org.freedesktop.UDisks2",
            shell=True, capture_output=True, text=True)
        print(f'  Already running: {has_owner.stdout.strip()}')

    # ── Operation timing without monitoring ───────────────────────

    def test_loop_setup_timing_baseline(self):
        """Measure loop-setup timing with NO D-Bus monitoring."""
        times = []
        for i in range(5):
            dev = LoopDevice()
            t0 = mts()
            try:
                dev.create()
                times.append((mts() - t0) * 1000)
                dev.delete()
            except Exception as e:
                print(f'  FAIL cycle {i}: {e}')
                times.append(None)
            dev.cleanup()
            time.sleep(0.2)

        valid = [t for t in times if t is not None]
        print(f'\n  loop-setup (no monitoring):')
        if valid:
            print(f'    min: {min(valid):.0f}ms')
            print(f'    max: {max(valid):.0f}ms')
            print(f'    mean: {sum(valid) / len(valid):.0f}ms')
            print(f'    OK: {len(valid)}/5')
        else:
            print(f'    ALL FAILED')

    # ── Operation timing with 1 monitoring connection ─────────────

    def test_loop_setup_timing_with_monitoring(self):
        """Measure loop-setup with one dbus-fast monitoring connection."""
        async def _run():
            c = SignalCollector()
            await c.start()
            try:
                times = []
                for i in range(5):
                    dev = LoopDevice()
                    t0 = mts()
                    try:
                        dev.create()
                        times.append((mts() - t0) * 1000)
                        dev.delete()
                    except Exception as e:
                        print(f'  FAIL cycle {i}: {e}')
                        times.append(None)
                    dev.cleanup()
                    time.sleep(0.2)
                return times
            finally:
                await c.stop()

        times = asyncio.run(_run())
        valid = [t for t in times if t is not None]
        print(f'\n  loop-setup (with 1 monitor):')
        if valid:
            print(f'    min: {min(valid):.0f}ms')
            print(f'    max: {max(valid):.0f}ms')
            print(f'    mean: {sum(valid) / len(valid):.0f}ms')
            print(f'    OK: {len(valid)}/5')
        else:
            print(f'    ALL FAILED')

    # ── Signal delivery latency ───────────────────────────────────

    def test_signal_delivery_latency(self):
        """How long after an operation do the last signals arrive?"""
        async def _run():
            c = SignalCollector()
            await c.start()
            try:
                dev = LoopDevice()
                c.reset()
                t0 = mts()
                dev.create()
                op_done = mts() - t0
                time.sleep(2)
                c.stop_ts = mts() - t0

                if c.signals:
                    first_signal = c.signals[0]['elapsed']
                    last_signal = c.signals[-1]['elapsed']
                    print(f'\n  loop-setup took: {op_done*1000:.0f}ms')
                    print(f'  First signal after: {first_signal*1000:.0f}ms')
                    print(f'  Last signal after: {last_signal*1000:.0f}ms')
                    print(f'  Signal spread: {(last_signal - first_signal)*1000:.0f}ms')
                    print(f'  Total signals: {len(c.signals)}')
                else:
                    print(f'  NO SIGNALS received after loop-setup!')
                    print(f'  loop-setup took: {op_done*1000:.0f}ms')

                dev.delete()
                dev.cleanup()
            finally:
                await c.stop()

        asyncio.run(_run())

    # ── AddMatch latency ──────────────────────────────────────────

    def test_addmatch_latency(self):
        """How long does AddMatch take?"""
        async def _measure():
            bus = await AioMessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                times = []
                for _ in range(5):
                    t0 = mts()
                    reply = await bus.call(add_match_message())
                    dt = (mts() - t0) * 1000
                    times.append(dt)
                return times
            finally:
                bus.disconnect()

        times = asyncio.run(_measure())
        print(f'\n  AddMatch latency:')
        print(f'    min: {min(times):.1f}ms')
        print(f'    max: {max(times):.1f}ms')
        print(f'    mean: {sum(times) / len(times):.1f}ms')
