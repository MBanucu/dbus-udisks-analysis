# Findings

*This document is populated from CI test runs and manual investigation.*

## Current Status

*Last updated: pending first CI run*

## Observed Behaviors

### Signal Completeness

| Operation | Expected Signals | Actually Received | Missing |
|-----------|-----------------|-------------------|---------|
| loop-setup | (pending) | (pending) | (pending) |
| loop-delete | (pending) | (pending) | (pending) |
| mount | (pending) | (pending) | (pending) |
| unmount | (pending) | (pending) | (pending) |

### Timing

| Metric | Without Monitoring | With 1 Connection | With 5 Connections |
|--------|-------------------|-------------------|---------------------|
| loop-setup duration | (pending) | (pending) | (pending) |
| loop-delete duration | (pending) | (pending) | (pending) |
| Signal delivery latency | N/A | (pending) | (pending) |

### Connection Stress

| Connections | Operations OK | Signals Captured | Failures |
|-------------|---------------|------------------|----------|
| 1 | (pending) | (pending) | (pending) |
| 2 | (pending) | (pending) | (pending) |
| 5 | (pending) | (pending) | (pending) |
| 10 | (pending) | (pending) | (pending) |

### Known Issues / Anomalies

*None yet documented.*

## Root Cause Hypotheses

### H1: Match rule accumulation
Each `AddMatch` call adds a match rule to the D-Bus daemon. If
connections don't properly call `RemoveMatch` before disconnecting,
match rules accumulate and degrade bus performance.

### H2: Event loop blocking
If a signal handler blocks the asyncio event loop, incoming messages
queue up in the D-Bus socket buffer. This could delay processing of
reply messages needed by `udisksctl`.

### H3: Connection resource exhaustion
UDisks2 may have a limit on concurrent D-Bus connections or may
throttle when too many signal subscribers exist.

### H4: Activation race
When UDisks2 is not yet running (socket-activated), the first
connection triggers activation. Signal subscriptions established
during activation may miss early events or interfere with the
activation handshake.

### H5: the bus daemon itself
dbus-daemon vs. dbus-broker may handle match rules and signal
delivery differently, especially under load.
