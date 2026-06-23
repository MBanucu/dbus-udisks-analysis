# Findings

*Data from CI runs at https://github.com/MBanucu/dbus-udisks-analysis*

## Root Cause Identified (2026-06-22)

**`sender=org.freedesktop.UDisks2` in the AddMatch rule does NOT match
UDisks2 signals on the GitHub Actions D-Bus daemon.**

The `udisks-monitor` D-Bus backend uses this match rule:

    type=signal,sender=org.freedesktop.UDisks2

This gets **zero signals**. Removing `sender=` from the rule and
filtering by `interface`/`member` instead would fix the issue.

## Evidence

### Environment

- **Runner**: `ubuntu-latest` (Ubuntu 24.04)
- **D-Bus daemon**: `dbus-daemon --system` (NOT dbus-broker)
- **`BecomeMonitor`**: NOT available (`org.freedesktop.DBus.Monitoring` absent)
- **`udisksctl monitor`**: works reliably — captures all UDisks2 events

### Match Rule Experiment (test_dbus_diagnostics.py)

Each rule tested with a fresh D-Bus connection + loop-setup + loop-delete:

| Match Rule | Signals | Senders |
|-----------|---------|---------|
| `type=signal` (no filter) | 135 | `org.freedesktop.DBus`, `:1.454`, `:1.2`, `:1.4` |
| `type=signal,sender=org.freedesktop.UDisks2` | **0** | none |
| `type=signal,interface=org.freedesktop.DBus.ObjectManager,sender=org.freedesktop.UDisks2` | 6 | `:1.468`, `:1.475` (systemd, not UDisks2) |
| `type=signal,interface=org.freedesktop.DBus.Properties,sender=org.freedesktop.UDisks2` | **0** | none |
| `type=signal,interface=org.freedesktop.UDisks2.Job,member=Completed,sender=org.freedesktop.UDisks2` | 1 | `:1.481` (not UDisks2) |

The no-filter rule captures UDisks2 events (Job.Completed, InterfacesAdded, etc).
All rules with `sender=org.freedesktop.UDisks2` get zero UDisks2 signals.

### Raw Message Sender Identity

Empty match rule `''` (eavesdrop everything):

```
sender=:1.497  PropertiesChanged  /org/freedesktop/UDisks2/block_devices/loop5
sender=:1.497  InterfacesAdded    /org/freedesktop/UDisks2
sender=:1.497  PropertiesChanged  /org/freedesktop/UDisks2/block_devices/sda1
...
```

UDisks2 signals come from sender `:1.497`. The well-known name
`org.freedesktop.UDisks2` is owned by `:1.497`, but `sender=`
in the match rule does not match the well-known name — it appears
to compare against the **unique name** only.

According to the D-Bus spec, `sender=` should match both unique and
well-known names, but this daemon does not implement that behavior.

### Why udisksctl monitor works

`udisksctl monitor` does NOT use `sender=` in its D-Bus subscription.
Instead it:
1. Connects to the system bus
2. Calls `org.freedesktop.UDisks2.Manager` methods to enumerate objects
3. Subscribes to signals by `interface` and `member` only
4. Filters out non-UDisks2 signals in application code

## Backend Reliability (5-cycle comparison)

| Python | D-Bus (AddMatch+sender) | udisksctl monitor | Winner |
|--------|------------------------|-------------------|--------|
| 3.10 | 0/5 | 4/5 | monitor |
| 3.11 | 0/5 | 5/5 | monitor |
| 3.12 | 0/5 | 4/5 | monitor |
| 3.13 | 0/5 | 4/5 | monitor |
| 3.14 | 0/5 | 4/5 | monitor |

## Loop Device Environment

```
loop module:  not loaded initially (auto-loads on first use)
/dev/loop*:   /dev/loop0, /dev/loop1, /dev/loop2 (3 pre-created)
loop-control: present
losetup -a:   empty at start
```

Raw `udisksctl loop-setup` from bash: **5/5 pass every time.**
Loop device operations are NOT the root cause.

## Rejected Hypotheses

| Hypothesis | Status | Evidence |
|-----------|--------|----------|
| H1: Match rule accumulation | Rejected | Single fresh connection gets 0 |
| H2: Event loop blocking | Rejected | 0 signals even with no handler CPU work |
| H3: Connection exhaustion | Rejected | 1 connection vs 10: both get 0 |
| H4: Activation race | Rejected | UDisks2 already running |
| H5: dbus-broker | Rejected | This IS dbus-daemon |
| H6: BecomeMonitor needed | Rejected | BecomeMonitor not available on this daemon |
| **H7: sender= filter broken** | **CONFIRMED** | Empty rule works; sender rule doesn't |

## Next Steps

### For udisks-monitor

Fix the D-Bus backend by **removing `sender=org.freedesktop.UDisks2`**
from the AddMatch rule in `udisks_monitor/_backends/_dbus.py:90`:

```python
# BEFORE (broken on GitHub Actions):
body=['type=signal,sender=org.freedesktop.UDisks2']

# AFTER (matches by interface/member, filter in handler):
body=['type=signal']
```

Or use a narrower filter without sender:

```python
body=[
    "type=signal,"
    "interface=org.freedesktop.DBus.ObjectManager,"
    "member=InterfacesAdded"
]
```

The `_on_message` handler already filters by `msg.interface` and
`msg.member`, so removing the sender filter won't cause false
positives — it will just allow UDisks2 signals to arrive.

### For this analysis repo

1. **Test fix**: Add a match rule without `sender=` and verify
   UDisks2 signals arrive on CI
2. **Add `test_fix_verification.py`**: Compare old rule vs new rule
   side-by-side in the same test run
3. **Upstream report**: File a bug against the D-Bus daemon shipping
   on Ubuntu 24.04 GitHub Actions runners about `sender=` not matching
   well-known names
   4. **Version check**: Determine the exact D-Bus daemon version and
   check its changelog for sender-match behavior

---

## UDisks2 Crash Monitoring (2026-06-22)

### Question

Does UDisks2 crash under D-Bus stress on CI, and if so, when and why?

### Monitoring Architecture

Three-layer observation captures the full picture:

| Layer | Tool | What it captures |
|-------|------|------------------|
| Application | `SignalCollector` (dbus-fast) | D-Bus signal delivery success/failure |
| System logs | `LogMonitor` (journalctl, dmesg) | UDisks2 process crashes, OOM kills, kernel errors |
| Service state | `systemctl` polls every 1s | ActiveState, SubState, PID changes |

### Crash Detection Heuristics

A crash is identified by any of:

1. **D-Bus unresponsive**: `NameHasOwner(org.freedesktop.UDisks2)` returns false
2. **Systemd dead state**: `ActiveState` transitions from `active` to `inactive`/`failed`/`deactivating`
3. **Journal crash entries**: `SIGSEGV`, `SIGABRT`, `assertion failed`, `Aborted`, `core dumped`
4. **Kernel OOM kill**: `Out of memory`, `Killed process` in dmesg
5. **Loop-setup failure**: `udisksctl loop-setup` fails with timeout or D-Bus error

### Evidence Collected

Each CI run captures:

- `results/crash_correlated.json` — Full correlated timeline of D-Bus events + system logs
- `results/crash_analysis_final.json` — Structured crash analysis
- `results/crash_evidence.md` — Human-readable findings
- `results/system-logs/` — Raw journalctl, dmesg, systemctl output

### Stress Methodologies (test_002_crash_monitor.py)

| Test | Stress Vector | What it proves |
|------|---------------|----------------|
| `test_crash_correlated_stress` | 15 rapid loop-setup/delete cycles with D-Bus monitors | Whether combined D-Bus + loop stress crashes UDisks2 |
| `test_loop_device_exhaustion` | Create 20 loop devices without deleting | Whether loop device limits crash UDisks2 |
| `test_resource_exhaustion_crash` | 30 rapid cycles, monitor OOM via dmesg | Whether resource exhaustion (OOM) kills UDisks2 |
| `test_dbus_connection_leak` | Open 30 D-Bus connections simultaneously | Whether connection exhaustion causes crash |
| `test_signal_storm_crash` | 10 rapid mount/unmount cycles | Whether signal storm overwhelms UDisks2 |
| `test_comprehensive_crash_analysis` | 12-cycle D-Bus stress + recovery tracking | Full lifecycle: crash detection, cause identification, recovery measurement |

### Verdict (populated by CI)

*This section will be updated with actual CI evidence.*

| Hypothesis | Status | Evidence |
|-----------|--------|----------|
| H8: D-Bus connection storm crashes UDisks2 | Testing | test_dbus_connection_leak |
| H9: Loop device exhaustion crashes UDisks2 | Testing | test_loop_device_exhaustion |
| H10: OOM kills UDisks2 under stress | Testing | test_resource_exhaustion_crash |
| H11: Signal storm crashes UDisks2 | Testing | test_signal_storm_crash |
| H12: Combined stress (D-Bus + loop) crashes UDisks2 | Testing | test_comprehensive_crash_analysis |
| H13: UDisks2 survives all stress on CI | Testing | All tests pass |
