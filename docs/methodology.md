# Methodology

## Philosophy

This test suite is designed around **observation, not verification**.
Tests do not assert "X events must happen" — they record what *does*
happen, measure it, and report.

## Test Environment

- **OS**: ubuntu-latest (GitHub Actions) / local Linux
- **Python**: 3.10 – 3.14
- **dbus-fast**: latest from PyPI
- **UDisks2**: system package from apt
- **Polkit**: All UDisks2 actions allowed for the test user (required for CI)

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

## Data Collection

Tests write structured JSON output to `results/` for offline analysis.
CI jobs upload these as artifacts.

## No Assumptions

Tests use `subTest()` for parametric variations and avoid hard
assertions where behavior is unknown. Instead they:

1. Capture everything
2. Count event types
3. Measure timing
4. Report anomalies as warnings, not failures
