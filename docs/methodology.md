# Methodology

## Philosophy

This test suite is designed around **observation, not verification**.
Tests do not assert "X events must happen" — they record what *does*
happen, measure it, and report.

## Test Environment

- **OS**: ubuntu-latest (GitHub Actions) / local Linux
- **Python**: 3.10 – 3.14
- **dbus-fast**: latest from PyPI
- **UDisks2**: OS-default (Ubuntu 24.04 apt) AND 2.10.2 (built from source)
- **Polkit**: All UDisks2 actions allowed for the test user (required for CI)

### Multi-Version Comparison

The CI matrix runs every test against **two UDisks2 versions** in parallel:
`os-default` (the apt package from the Ubuntu runner image) and `2.10.2`
(built from upstream source via `./configure --prefix=/usr && make install`).

This design serves two purposes:

1. **Regression detection** — upgrades to the UDisks2 package in the runner
   image won't silently change test outcomes; the pinned 2.10.2 source build
   acts as a stable reference point.
2. **Behavioral diffing** — `test_version_comparison` in `test_dbus_diagnostics`
   captures the full D-Bus API surface (interfaces, methods, signals, properties,
   managed object types) for each version, enabling side-by-side comparison.

The `UDISKS2_TEST_VERSION` env var is set per job and recorded in
`results/system_info.json` for automated downstream analysis.

## Test Categories

### 1. Raw Signal Capture (`test_raw_signals.py`)

Connects one dbus-fast `MessageBus`, subscribes to all
`org.freedesktop.UDisks2` signals via `AddMatch`, and logs
**every** message with:

- Timestamp (monotonic, high resolution)
- Message type, interface, member
- Object path
- Full body contents

Operations tested:
- Loop device setup
- Loop device delete
- Mount / unmount
- Filesystem creation

### 2. Lifecycle Analysis (`test_loop_lifecycle.py`)

Traces the exact sequence of signals during loop-setup and loop-delete.
Captures:
- Total signal count per operation
- Signal ordering (InterfaceAdded → PropertiesChanged → JobAdded → ...)
- Whether specific signals arrive or are missing
- Between-operation signal noise

### 3. Mount/Unmount (`test_mount_unmount.py`)

Same approach for filesystem mount and unmount operations on loop
devices.

### 4. Connection Stress (`test_connection_stress.py`)

Tests how UDisks2 behaves when multiple dbus-fast connections are
opened and closed:
- 1, 2, 5, 10 simultaneous connections
- Rapid connect/disconnect cycles
- Long-lived connections spanning many operations

### 5. Concurrent Operations (`test_concurrent_ops.py`)

Creates loop devices while signal handlers are actively processing
messages. Measures:
- Signal loss rate
- Operation success rate
- Timing degradation with concurrent monitoring

### 6. Timing (`test_timing.py`)

Measures:
- UDisks2 activation latency (cold start)
- End-to-end signal delivery latency
- loop-setup / loop-delete duration with and without monitoring

### 7. UDisks2 Limits (`test_zz_udisks2_limits.py`)

Discovers UDisks2's breaking point through systematic stress:
- Consecutive bare loop cycles (no D-Bus) — baseline for operation throughput
- Consecutive D-Bus monitor cycles — fresh connection each cycle
- Subprocess-to-D-Bus backend switching — simulates udisks-monitor parity tests
- Recovery time measurement after subprocess kill
- Max concurrent D-Bus monitors before UDisks2 becomes unresponsive

### 8. Crash Recovery (`test_zz_recovery.py`)

Tests whether UDisks2 recovers after being crashed by D-Bus + loop stress:
- Auto-recovery via systemd socket activation
- Manual restart via systemctl
- Retry loop strategy for CI test reliability
- systemd service status before/after crash
- D-Bus connection viability post-recovery

### 9. Crash Monitoring (`test_zz_crash_monitor.py`)

**Three-layer monitoring** to answer "when and why does UDisks2 crash on CI?":

| Layer | Tool | Frequency | Purpose |
|-------|------|-----------|---------|
| D-Bus | `SignalCollector` | Per-signal | Did signals arrive? |
| System logs | `LogMonitor` (journalctl -f, dmesg -w) | Per-line | Crash messages, OOM, kernel errors |
| Service state | `systemctl show udisks2` | Every 1 second | ActiveState transitions, PID changes |

The `LogMonitor` utility (`tools/log_monitor.py`) starts background
subprocesses for `journalctl -u udisks2 -f` and `dmesg -w`, polls
`systemctl show` every second, and records all events with microsecond
timestamps. This creates a correlated timeline where D-Bus operations
can be mapped to system-level events.

**Crash detection heuristics**:
1. `NameHasOwner(org.freedesktop.UDisks2)` returns false
2. `ActiveState` transitions from `active` to `inactive`/`failed`
3. Journal contains `SIGSEGV`, `SIGABRT`, `assertion failed`, `core dumped`
4. dmesg contains `Out of memory`, `Killed process`
5. `udisksctl loop-setup` fails with timeout or D-Bus error

**Stress vectors tested**:
- Combined D-Bus + loop stress (6 rapid cycles)
- Loop device exhaustion (20 concurrent devices)
- Resource exhaustion / OOM (8 rapid cycles)
- D-Bus connection leak (30 simultaneous connections)
- Signal storm (10 rapid mount/unmount cycles)
- Comprehensive crash + recovery lifecycle

## Data Collection

Tests write structured JSON output to `results/` for offline analysis.
CI jobs upload these as artifacts, including:

- `results/*.json` — Test output data
- `results/udisks2_version_comparison.json` — Runtime version, D-Bus API surface, managed object types (per version)
- `results/udisks2_introspect.xml` — Raw D-Bus introspection XML
- `results/crash_evidence.md` — Human-readable crash findings
- `results/system-logs/` — Raw journalctl, dmesg, systemctl output

### Post-test Forensics

The CI workflow runs a **forensics step** (always, even on failure) that:
1. Captures last 500 lines of `journalctl -u udisks2`
2. Captures last 500 lines of `journalctl -k` (kernel)
3. Dumps `dmesg`
4. Records `systemctl status udisks2`
5. Captures `busctl tree org.freedesktop.UDisks2`
6. Lists remaining loop devices (`losetup -a`)

This ensures evidence is preserved even if tests crash the runner.

## No Assumptions

Tests use `subTest()` for parametric variations and avoid hard
assertions where behavior is unknown. Instead they:

1. Capture everything
2. Count event types
3. Measure timing
4. Report anomalies as warnings, not failures
