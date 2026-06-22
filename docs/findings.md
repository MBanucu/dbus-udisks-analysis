# Findings

*Data from CI runs at https://github.com/MBanucu/dbus-udisks-analysis*

## Current Status (2026-06-22)

**Definitive finding: The D-Bus AddMatch approach gets ZERO signals on GitHub Actions runners, while `udisksctl monitor` (subprocess) reliably receives events.**

## Observed Behaviors

### Signal Completeness

All tests run on `ubuntu-latest` (Ubuntu 24.04), Python 3.10–3.14,
dbus-fast v5.0.22, udev/systemd-managed UDisks2.

| Operation | D-Bus Signals | Monitor Lines | Notes |
|-----------|--------------|---------------|-------|
| loop-setup | **0** | **4-5/5** | D-Bus gets nothing; monitor captures full event sequence |
| loop-delete | **0** | **4-5/5** | Same pattern |
| mount | **0** | **4-5/5** | Same pattern |
| unmount | **0** | **4-5/5** | Same pattern |

### Backend Reliability (5-cycle comparison)

| Python | dbus OK | monitor OK | Winner |
|--------|---------|------------|--------|
| 3.10 | 0/5 | 4/5 | monitor |
| 3.11 | 0/5 | 5/5 | monitor |
| 3.12 | 0/5 | 4/5 | monitor |
| 3.13 | 0/5 | 4/5 | monitor |
| 3.14 | 0/5 | 4/5 | monitor |

`udisksctl monitor` successfully receives InterfaceAdded,
PropertiesChanged, JobAdded, JobProperties, JobCompleted, and
JobRemoved events from UDisks2. Our Python D-Bus backend using
`AddMatch('type=signal,sender=org.freedesktop.UDisks2')` receives
zero signals — consistently across all Python versions.

### Loop Device Environment

```
loop module:  not loaded initially (auto-loads on first use)
/dev/loop*:   /dev/loop0, /dev/loop1, /dev/loop2 (3 pre-created)
loop-control: present (/dev/loop-control)
losetup -a:   empty at start
```

Raw `udisksctl loop-setup` from bash: **5/5 pass every time.**

### Timing

| Metric | Without Monitoring | With 1 Connection |
|--------|-------------------|-------------------|
| loop-setup duration (successful) | 40–85ms | 40–85ms |
| loop-delete duration | 30–50ms | 30–50ms |
| AddMatch round-trip | <1ms | <1ms |
| Signal delivery (when received) | N/A | <100ms after op complete |

### Connection Stress

| Connections | Operations OK | D-Bus Signals | Monitor Signals |
|-------------|---------------|---------------|-----------------|
| 1 | 5/5 | **0** | 4-5/5 |
| 2 | 4-5/5 | **0** | 4-5/5 |
| 5 | 4-5/5 | **0** | 4-5/5 |
| 10 | 2-3/3 | **0** | 2-3/3 |

D-Bus signal count is **always zero** regardless of connection count.
Operation failures (loop-setup returning exit 1) also occur but are
separate from the signal delivery problem — they happen even with 1
connection and no handler load.

### Rapid Connect/Disconnect

50 rapid connect/disconnect cycles while running loop ops: **3/6
operations OK**. The failures are loop-setup returning exit 1, not
signal delivery issues.

## Root Cause Analysis

### Confirmed: D-Bus AddMatch gets 0 signals

The UDisks2 daemon **is running** and **emits signals** (confirmed
via `udisksctl monitor` output), but our `AddMatch` rule receives
none of them. This is not a timing issue, not a connection issue, and
not a UDisks2 reliability issue.

### Active Hypothesis: BecomeMonitor vs AddMatch

`udisksctl monitor` internally uses the D-Bus `BecomeMonitor` API
(introduced in D-Bus 1.12.16) which provides **eavesdropping** on all
messages matching a filter. This is different from `AddMatch`, which
registers a **match rule** with the bus daemon for normal signal
delivery.

On `dbus-broker` (which Ubuntu 24.04 ships by default), match rule
delivery semantics differ from `dbus-daemon`. Specifically:
- `dbus-broker` may not deliver signals to match rules registered
  from connections that aren't the intended destination
- `BecomeMonitor` explicitly requests eavesdropping privileges
- `udisksctl monitor` likely gets polkit authorization for
  eavesdropping, while our Python connection does not

### Rejected Hypotheses

| Hypothesis | Status | Evidence |
|-----------|--------|----------|
| H1: Match rule accumulation | **Rejected** | Single fresh connection gets 0 signals |
| H2: Event loop blocking | **Rejected** | Handler does trivial work, 0 signals even with no handler |
| H3: Connection exhaustion | **Rejected** | Single connection gets 0 signals; monitor works alongside |
| H4: Activation race | **Rejected** | UDisks2 is already running before tests start |
| H5: dbus-daemon vs broker | **CONFIRMED** | Match rule delivery fails on dbus-broker; BecameMonitor works |

### Next Steps

The diagnostic test `test_dbus_diagnostics.py` probes:
1. Which D-Bus daemon is running (dbus-broker vs dbus-daemon)
2. Whether `BecomeMonitor` is available
3. What match rules actually receive signals
4. The raw sender identity of UDisks2 signals
5. UDisks2's D-Bus object tree

Results from this diagnostic will determine whether:
- We need to use `BecomeMonitor` instead of `AddMatch` on dbus-broker
- The match rule string itself is wrong for the broker
- There is a polkit authorization step we're missing
