#!/usr/bin/env python3
"""Dump all UDisks2 D-Bus signals in real-time to stdout.

Usage:
    python tools/signal_dumper.py              # dump forever
    python tools/signal_dumper.py --timeout 30 # dump for 30 seconds
    python tools/signal_dumper.py --json       # output as JSON lines
"""

import argparse
import asyncio
import json
import sys
import time

from dbus_fast.aio import MessageBus
from dbus_fast import BusType, Message, MessageType

try:
    from dbus_fast.signature import Variant
except ImportError:
    Variant = None


def ts():
    return time.strftime('%H:%M:%S')


def _format_value(v):
    """Recursively format D-Bus values for display."""
    if Variant and isinstance(v, Variant):
        return _format_value(v.value)
    if isinstance(v, dict):
        return {str(k): _format_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_format_value(item) for item in v]
    if isinstance(v, bytes):
        return v.hex()[:40]
    return v


async def main():
    parser = argparse.ArgumentParser(
        description='Dump all UDisks2 D-Bus signals in real-time')
    parser.add_argument('--timeout', type=float, default=0,
                        help='Stop after N seconds (0 = forever)')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON lines')
    parser.add_argument('--quiet', action='store_true',
                        help='Only print message count, not content')
    args = parser.parse_args()

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    reply = await bus.call(Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='AddMatch',
        signature='s',
        body=['type=signal,sender=org.freedesktop.UDisks2'],
    ))

    if reply.message_type == MessageType.ERROR:
        print(f'AddMatch failed: {reply.body}', file=sys.stderr)
        return 1

    if not args.quiet:
        print(
            f'{ts()} Listening for UDisks2 signals '
            f'(timeout={args.timeout or "infinite"})...'
        )
        print(f'{"=" * 80}')

    count = 0
    start = time.monotonic()

    def handler(msg: Message):
        nonlocal count
        if msg.message_type != MessageType.SIGNAL:
            return
        count += 1

        if args.quiet:
            if count % 50 == 0:
                elapsed = time.monotonic() - start
                print(f'{ts()}  {count} signals  '
                      f'({count / elapsed:.1f}/s)')
            return

        if args.json:
            body_repr = []
            for item in msg.body:
                if isinstance(item, dict):
                    d = {}
                    for k, v in item.items():
                        d[k] = _format_value(v)
                    body_repr.append(d)
                elif isinstance(item, list):
                    body_repr.append([_format_value(x) for x in item])
                else:
                    body_repr.append(_format_value(item))
            entry = {
                'time': ts(),
                'elapsed': round(time.monotonic() - start, 3),
                'count': count,
                'path': msg.path,
                'interface': msg.interface,
                'member': msg.member,
                'body': body_repr,
            }
            print(json.dumps(entry, default=str))
        else:
            header = (f'{ts()} [{count:4d}] {msg.interface}.{msg.member}'
                      f'  {msg.path}')
            print(header)
            print('-' * len(header))
            for i, item in enumerate(msg.body):
                if isinstance(item, dict):
                    for k, v in item.items():
                        print(f'  | {k}: {_format_value(v)}')
                elif isinstance(item, list):
                    for x in item:
                        print(f'  | - {_format_value(x)}')
                else:
                    print(f'  [{i}]: {_format_value(item)}')
            print()

    bus.add_message_handler(handler)

    if args.timeout > 0:
        await asyncio.sleep(args.timeout)
        print(f'\n{ts()} Stopped after {time.monotonic() - start:.1f}s, '
              f'{count} signals captured')
    else:
        try:
            stop_event = asyncio.Event()
            await stop_event.wait()
        except KeyboardInterrupt:
            print(f'\n{ts()} Interrupted after {time.monotonic() - start:.1f}s, '
                  f'{count} signals captured')

    bus.disconnect()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
