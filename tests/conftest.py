"""Shared helpers and fixtures for all analysis tests."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime

from dbus_fast.aio import MessageBus
from dbus_fast import BusType, Message, MessageType

try:
    from dbus_fast.signature import Variant
except ImportError:
    Variant = None

_ADD_MATCH_FILTER = 'type=signal,sender=org.freedesktop.UDisks2'


def add_match_message():
    """Return the AddMatch Message for UDisks2 signals."""
    return Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='AddMatch',
        signature='s',
        body=[_ADD_MATCH_FILTER],
    )


def remove_match_message():
    """Return the RemoveMatch Message for UDisks2 signals."""
    return Message(
        destination='org.freedesktop.DBus',
        path='/org/freedesktop/DBus',
        interface='org.freedesktop.DBus',
        member='RemoveMatch',
        signature='s',
        body=[_ADD_MATCH_FILTER],
    )


def ts() -> str:
    """Timestamp string for logging."""
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


def mts() -> float:
    """Monotonic timestamp (seconds) for measuring intervals."""
    return time.monotonic()


def dbus_version():
    """Return (dbus-fast version, dbus daemon info)."""
    try:
        from importlib.metadata import version as _pkg_version
        fast_ver = _pkg_version('dbus-fast')
    except Exception:
        fast_ver = 'unknown'
    r = subprocess.run(['busctl', '--system', 'call',
                        'org.freedesktop.DBus',
                        '/org/freedesktop/DBus',
                        'org.freedesktop.DBus',
                        'GetConnectionCredentials',
                        's', 'org.freedesktop.DBus'],
                       capture_output=True, text=True)
    r2 = subprocess.run(
        "ps --no-headers -eo comm,pid | grep -E 'dbus-(daemon|broker)' | head -3 || echo 'none'",
        shell=True, capture_output=True, text=True)
    return {
        'dbus_fast_version': fast_ver,
        'python_version': sys.version,
        'dbus_daemon': r2.stdout.strip(),
        'dbus_credentials': r.stdout.strip()[:300],
    }


def system_info():
    """Collect system information for test reports."""
    info = dbus_version()
    r = subprocess.run(['dpkg', '-l', 'udisks2'],
                       capture_output=True, text=True)
    info['udisks2_package'] = (
        r.stdout.splitlines()[-1].strip() if r.stdout.strip() else 'NOT INSTALLED')
    r2 = subprocess.run(['systemctl', 'show', 'udisks2',
                         '--property=ActiveState,SubState,MainPID'],
                        capture_output=True, text=True)
    info['udisks2_status'] = r2.stdout.strip()
    if 'CI' in os.environ:
        info['ci'] = os.environ.get('CI', '')
        info['github_workflow'] = os.environ.get('GITHUB_WORKFLOW', '')
        info['github_run_id'] = os.environ.get('GITHUB_RUN_ID', '')
    return info


def udisksctl_available():
    """Check if udisksctl is installed and accessible."""
    try:
        r = subprocess.run(['udisksctl', 'dump'], capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


class LoopDevice:
    """Create and manage a temporary loop device for testing."""

    def __init__(self):
        fd, path = tempfile.mkstemp(suffix='.img')
        os.close(fd)
        self.img_path = path
        self.device = None
        self.device_name = None

    def create(self, timeout=15):
        """Create image file and set up loop device."""
        subprocess.run(
            ['dd', 'if=/dev/zero', 'of=' + self.img_path,
             'bs=1M', 'count=1'],
            capture_output=True, check=True, timeout=timeout)
        subprocess.run(
            ['mkfs.vfat', self.img_path],
            capture_output=True, check=True, timeout=timeout)
        r = subprocess.run(
            ['udisksctl', 'loop-setup', '-f', self.img_path,
             '--no-user-interaction'],
            capture_output=True, text=True, timeout=timeout)
        r.check_returncode()
        for line in r.stdout.splitlines():
            if '/dev/' in line:
                self.device = line.strip().split()[-1].rstrip('.')
                self.device_name = self.device.split('/')[-1]
                return self.device
        raise RuntimeError(f'could not parse loop-setup output:\n{r.stdout}')

    def mount(self, timeout=15):
        """Mount the loop device."""
        r = subprocess.run(
            ['udisksctl', 'mount', '-b', self.device,
             '--no-user-interaction'],
            capture_output=True, text=True, timeout=timeout)
        r.check_returncode()
        return r.stdout.strip()

    def unmount(self, timeout=15):
        """Unmount the loop device."""
        r = subprocess.run(
            ['udisksctl', 'unmount', '-b', self.device,
             '--no-user-interaction'],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode

    def delete(self, timeout=15):
        """Delete the loop device."""
        r = subprocess.run(
            ['udisksctl', 'loop-delete', '-b', self.device,
             '--no-user-interaction'],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode

    def cleanup(self):
        """Best-effort cleanup."""
        if self.device:
            for _ in range(3):
                try:
                    subprocess.run(
                        ['udisksctl', 'unmount', '-b', self.device,
                         '--no-user-interaction'],
                        capture_output=True, timeout=10)
                except Exception:
                    pass
                try:
                    r = subprocess.run(
                        ['udisksctl', 'loop-delete', '-b', self.device,
                         '--no-user-interaction'],
                        capture_output=True, timeout=10)
                    if r.returncode == 0:
                        break
                except Exception:
                    pass
                time.sleep(0.1)
        if os.path.exists(self.img_path):
            os.unlink(self.img_path)


class SignalCollector:
    """Connect to D-Bus and collect all UDisks2 signals."""

    def __init__(self):
        self.signals: list[dict] = []
        self._bus = None
        self._started = False
        self._t0 = None

    async def start(self):
        """Connect and subscribe to UDisks2 signals."""
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        reply = await self._bus.call(add_match_message())
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(
                f'AddMatch failed: {reply.body[0] if reply.body else "unknown"}')
        self._bus.add_message_handler(self._handler)
        self._started = True
        self._t0 = mts()

    async def stop(self):
        """Disconnect and clean up."""
        if self._bus:
            try:
                await self._bus.call(remove_match_message())
            except Exception:
                pass
            self._bus.remove_message_handler(self._handler)
            self._bus.disconnect()

    def _handler(self, msg: Message):
        self.record(msg)

    def record(self, msg: Message):
        """Record a single signal."""
        if msg.message_type != MessageType.SIGNAL:
            return
        elapsed = mts() - self._t0 if self._t0 else 0

        body_repr = []
        for item in msg.body:
            if isinstance(item, dict):
                d = {}
                for k, v in item.items():
                    if Variant and isinstance(v, Variant):
                        d[k] = {'type': str(v.signature), 'value': repr(v.value)}
                    else:
                        d[k] = repr(v)
                body_repr.append(d)
            elif isinstance(item, list):
                body_repr.append([repr(x) for x in item])
            else:
                body_repr.append(repr(item))

        self.signals.append({
            'elapsed': round(elapsed, 6),
            'time': ts(),
            'path': msg.path or '',
            'interface': msg.interface or '',
            'member': msg.member or '',
            'sender': msg.sender or '',
            'body': body_repr,
        })

    def reset(self):
        """Clear all recorded signals."""
        self.signals.clear()

    def count_by_interface(self):
        """Count signals by interface.member."""
        counts = {}
        for s in self.signals:
            key = f"{s['interface']}.{s['member']}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def count_by_member(self):
        """Count signals by member only."""
        counts = {}
        for s in self.signals:
            counts[s['member']] = counts.get(s['member'], 0) + 1
        return counts

    def paths_seen(self):
        """Return set of all object paths observed."""
        return {s['path'] for s in self.signals}

    def interfaces_seen(self):
        """Return set of all interfaces observed."""
        result = set()
        for s in self.signals:
            result.add(s['interface'])
            for item in s['body']:
                if isinstance(item, dict):
                    result.update(item.keys())
                elif isinstance(item, list):
                    result.update(item)
        return result

    def signals_between(self, start, end):
        """Return signals whose elapsed time is between start and end."""
        return [s for s in self.signals if start <= s['elapsed'] <= end]

    def dump(self, filepath=None):
        """Write all collected signals as JSON."""
        data = {
            'total_signals': len(self.signals),
            'timing': {
                'first_elapsed': self.signals[0]['elapsed'] if self.signals else None,
                'last_elapsed': self.signals[-1]['elapsed'] if self.signals else None,
            },
            'counts_by_interface': self.count_by_interface(),
            'counts_by_member': self.count_by_member(),
            'paths_seen': sorted(self.paths_seen()),
            'signals': self.signals,
        }
        if filepath:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
        return json.dumps(data, indent=2)


def ensure_dir(path):
    """Ensure directory exists."""
    os.makedirs(path, exist_ok=True)


def print_system_info(stream=None):
    """Print system info for diagnostics."""
    info = system_info()
    out = stream or sys.stderr
    for k, v in info.items():
        print(f'  {k}: {v}', file=out)
