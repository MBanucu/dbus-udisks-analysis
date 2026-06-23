#!/usr/bin/env python3
"""System log monitor for capturing UDisks2 crash evidence.

Monitors multiple log sources simultaneously during stress tests:
- journalctl -u udisks2  (UDisks2 service logs)
- journalctl -k           (kernel logs, for loop/block device errors)
- dmesg -w                (kernel ring buffer)
- systemctl status polls  (UDisks2 service state changes)
- D-Bus NameOwnerChanged  (UDisks2 name ownership changes)

Usage:
    from tools.log_monitor import LogMonitor
    monitor = LogMonitor()
    monitor.start()
    # ... run stress test ...
    monitor.stop()
    monitor.save_report('results/crash_evidence.json')
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime


def _utc_ts():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


class LogMonitor:
    """Capture system logs, D-Bus name ownership, and service state
    across time for correlating with UDisks2 crashes.
    """

    def __init__(self, tags=None):
        self._tags = tags or {}
        self._journal_proc = None
        self._dmesg_proc = None
        self._stop_event = threading.Event()
        self._t0 = None
        self._events: list[dict] = []
        self._lock = threading.Lock()

        # Per-second state snapshots
        self._snapshots: list[dict] = []
        self._snap_thread = None

    # ── public API ─────────────────────────────────────────────────

    def start(self):
        """Start all log monitors in background threads."""
        self._t0 = time.monotonic()
        self._record('monitor', 'started', {'tags': self._tags})

        self._start_journalctl()
        self._start_dmesg()
        self._start_state_snapshots()

    def stop(self):
        """Stop all monitors and collect final state."""
        self._stop_event.set()
        self._record('monitor', 'stopped', {'duration_s': round(time.monotonic() - self._t0, 3)})

        # Collect final service state
        self._snapshot_state('final')

        # Kill subprocesses
        for proc in (self._journal_proc, self._dmesg_proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()

        if self._snap_thread:
            self._snap_thread.join(timeout=5)

    def mark(self, label, **extra):
        """Insert a labeled marker into the event stream."""
        self._record('marker', label, extra)

    def events(self):
        """Return all captured events in chronological order."""
        with self._lock:
            return list(self._events)

    def snapshots(self):
        with self._lock:
            return list(self._snapshots)

    def save_report(self, filepath):
        """Write the full monitoring report as JSON."""
        with self._lock:
            data = {
                'monitor': {
                    'started_at': _utc_ts(),
                    'duration_s': round(time.monotonic() - self._t0, 3) if self._t0 else 0,
                    'tags': self._tags,
                },
                'event_count': len(self._events),
                'snapshot_count': len(self._snapshots),
                'events': self._events,
                'snapshots': self._snapshots,
            }
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2, default=str)

    def report(self):
        """Return a human-readable summary."""
        with self._lock:
            journals = [e for e in self._events if e['source'] == 'journal-udisks2']
            dmessages = [e for e in self._events if e['source'] == 'dmesg']
            state_changes = [e for e in self._events if e['source'] == 'state']
            markers = [e for e in self._events if e['source'] == 'marker']

            # Find errors/crash indicators
            crash_keywords = ['error', 'fail', 'crash', 'killed', 'signal',
                              'abort', 'segfault', 'core', 'oom', 'timeout',
                              'assert', 'abnormal', 'SIGSEGV', 'SIGABRT',
                              'SIGBUS', 'panic', 'BUG:', 'Oops:', 'Call Trace']
            crash_lines = []
            for e in journals + dmessages:
                text = e.get('text', '').lower()
                if any(kw in text for kw in crash_keywords):
                    crash_lines.append(e)

            dead_snapshots = [s for s in self._snapshots
                              if s.get('activestate') not in ('active', None)]

        lines = []
        lines.append(f'=== LogMonitor Report ===')
        lines.append(f'Duration: {self._snapshots[-1]["elapsed_s"] if self._snapshots else 0:.1f}s')
        lines.append(f'Journal entries (udisks2): {len(journals)}')
        lines.append(f'Kernel messages: {len(dmessages)}')
        lines.append(f'State snapshots: {len(self._snapshots)}')
        lines.append(f'User markers: {len(markers)}')
        lines.append(f'Crash-indicator entries: {len(crash_lines)}')

        if crash_lines:
            lines.append(f'\nPotential crash indicators:')
            for e in crash_lines:
                ts = e.get('elapsed_s', 0)
                text = e.get('text', '')[:200]
                lines.append(f'  [{ts:7.2f}s] [{e["source"]}] {text}')

        if dead_snapshots:
            lines.append(f'\nNon-active service states:')
            for s in dead_snapshots:
                lines.append(f'  [{s["elapsed_s"]:7.2f}s] '
                             f'ActiveState={s.get("active_state")} '
                             f'SubState={s.get("sub_state")} '
                             f'PID={s.get("pid")}')

        lines.append(f'\nTimeline (markers + state changes):')
        combined = markers + state_changes
        combined.sort(key=lambda e: e.get('elapsed_s', 0))
        for e in combined:
            ts = e.get('elapsed_s', 0)
            if e['source'] == 'marker':
                lines.append(f'  [{ts:7.2f}s] MARK: {e["detail"]}')
            else:
                lines.append(f'  [{ts:7.2f}s] STATE: {e["detail"]}')

        return '\n'.join(lines)

    # ── internals ──────────────────────────────────────────────────

    def _record(self, source, detail, extra=None):
        elapsed = round(time.monotonic() - self._t0, 6) if self._t0 else 0
        entry = {
            'elapsed_s': elapsed,
            'utc': _utc_ts(),
            'source': source,
            'detail': detail,
        }
        if extra:
            entry.update(extra)
        with self._lock:
            self._events.append(entry)

    def _start_journalctl(self):
        """Start `journalctl -u udisks2 -f` in background."""
        try:
            self._journal_proc = subprocess.Popen(
                ['journalctl', '-u', 'udisks2', '-f', '--no-pager',
                 '-o', 'short-iso-precise'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            t = threading.Thread(target=self._read_journal, daemon=True)
            t.start()
        except FileNotFoundError:
            self._record('monitor', 'journalctl not found')

    def _start_dmesg(self):
        """Start `dmesg -w` in background."""
        try:
            self._dmesg_proc = subprocess.Popen(
                ['dmesg', '-w'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            t = threading.Thread(target=self._read_dmesg, daemon=True)
            t.start()
        except (FileNotFoundError, PermissionError):
            # dmesg may require root
            self._record('monitor', 'dmesg not available')

    def _read_journal(self):
        while not self._stop_event.is_set():
            line = None
            try:
                line = self._journal_proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            line = line.strip()
            if line:
                self._record('journal-udisks2', 'entry', {'text': line})
        # Read remaining
        if self._journal_proc and self._journal_proc.stdout:
            for line in self._journal_proc.stdout:
                if line.strip():
                    self._record('journal-udisks2', 'entry', {'text': line.strip()})

    def _read_dmesg(self):
        while not self._stop_event.is_set():
            line = None
            try:
                line = self._dmesg_proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            line = line.strip()
            if line:
                self._record('dmesg', 'entry', {'text': line})
        if self._dmesg_proc and self._dmesg_proc.stdout:
            for line in self._dmesg_proc.stdout:
                if line.strip():
                    self._record('dmesg', 'entry', {'text': line.strip()})

    def _start_state_snapshots(self):
        self._snap_thread = threading.Thread(
            target=self._snapshot_loop, daemon=True)
        self._snap_thread.start()

    def _snapshot_loop(self):
        while not self._stop_event.is_set():
            self._snapshot_state('periodic')
            self._stop_event.wait(1.0)

    def _snapshot_state(self, kind):
        elapsed = round(time.monotonic() - self._t0, 3) if self._t0 else 0
        snap = {
            'elapsed_s': elapsed,
            'utc': _utc_ts(),
            'kind': kind,
        }

        # systemctl show udisks2
        try:
            r = subprocess.run(
                ['systemctl', 'show', 'udisks2',
                 '--property=ActiveState,SubState,MainPID,ExecMainPID,'
                 'Restart,WatchdogUSec,NRestarts,Result,StatusText,'
                 'ExecMainStatus,MemoryCurrent,TasksCurrent'],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    snap[k.lower()] = v
        except Exception:
            pass

        # sudo systemctl status for human-readable
        try:
            r = subprocess.run(
                ['systemctl', 'status', 'udisks2', '--no-pager', '-l',
                 '--lines=0'],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                snap['status_text'] = r.stdout.split('Active:')[1].split('\n')[0].strip() if 'Active:' in r.stdout else r.stdout.strip()[:300]
        except Exception:
            pass

        # busctl name-owner check
        try:
            r = subprocess.run(
                ['busctl', '--system', 'call',
                 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                 'org.freedesktop.DBus', 'NameHasOwner',
                 's', 'org.freedesktop.UDisks2'],
                capture_output=True, text=True, timeout=5)
            snap['dbus_has_owner'] = 'true' in r.stdout.lower()
        except Exception:
            snap['dbus_has_owner'] = None

        with self._lock:
            self._snapshots.append(snap)

        # Record state change events
        state = snap.get('activestate', 'unknown')
        with self._lock:
            prev = self._snapshots[-2] if len(self._snapshots) > 1 else None
        if prev and prev.get('activestate') != state:
            self._record('state', f'{prev.get("activestate")} -> {state}',
                         {'from': prev.get('activestate'),
                          'to': state,
                          'pid': snap.get('mainpid')})
