# dbus-udisks-analysis

Comprehensive, independent analysis of the interaction between
[dbus-fast](https://github.com/bluetooth-devices/dbus-fast) and
[UDisks2](https://github.com/storaged-project/udisks) on Linux.

## Purpose

This repository exists to **observe and document** — not to test
assumptions. It captures every D-Bus signal emitted by UDisks2 during
disk operations (loop-setup, mount, unmount, delete), measures timing,
stress-tests concurrent connections, and reports raw data without
pre-baked expectations.

## Why

The `udisks-monitor` project experienced persistent CI failures when
using dbus-fast's D-Bus backend alongside `udisksctl` operations. This
repo isolates the interaction to find root causes.

## Structure

```
├── docs/
│   ├── methodology.md      — How tests are designed and run
│   ├── findings.md          — Observed behavior (populated by CI runs)
│   └── signal-reference.md  — Complete catalog of all observed signals
├── tests/
│   ├── conftest.py           — Shared setup, loop device helpers
│   ├── test_raw_signals.py   — Capture ALL signals, no filtering
│   ├── test_loop_lifecycle.py— Loop setup/delete signal patterns
│   ├── test_mount_unmount.py — Mount/unmount signal patterns
│   ├── test_connection_stress.py— Multiple connections, rapid connect/disconnect
│   ├── test_concurrent_ops.py— Concurrent operations + signal integrity
│   └── test_timing.py        — Activation latency, signal delay
├── tools/
│   └── signal_dumper.py      — CLI: dump all UDisks2 signals in real-time
├── results/
│   └── (test output dumps)
└── .github/workflows/
    └── test.yml              — CI that runs everything + collects artifacts
```

## Running

```bash
# Install
pip install -e .

# Run all tests
python -m unittest discover -s tests -v

# Dump live signals
python tools/signal_dumper.py

# Run specific test class
python -m unittest tests.test_raw_signals -v
```

## CI

Every push runs the full suite on ubuntu-latest. Test outputs and
captured signal traces are uploaded as build artifacts.
