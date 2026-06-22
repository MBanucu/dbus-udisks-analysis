# Signal Reference

Complete catalog of all D-Bus signals observed from UDisks2.

## D-Bus Interfaces

UDisks2 exposes objects under `/org/freedesktop/UDisks2/` with
several interfaces:

| Interface | Purpose |
|-----------|---------|
| `org.freedesktop.UDisks2.Drive` | Physical drive properties |
| `org.freedesktop.UDisks2.Block` | Block device properties |
| `org.freedesktop.UDisks2.Partition` | Partition properties |
| `org.freedesktop.UDisks2.PartitionTable` | Partition table |
| `org.freedesktop.UDisks2.Filesystem` | Filesystem mount state |
| `org.freedesktop.UDisks2.Loop` | Loop device properties |
| `org.freedesktop.UDisks2.Job` | Long-running operation tracking |

## Signal Types

### `org.freedesktop.DBus.ObjectManager.InterfacesAdded`
Emitted when a new object appears or gains an interface.

**Body**: `(object_path: o, interfaces: a{sa{sv}})`

**Observed for**:
- `/org/freedesktop/UDisks2/block_devices/<name>` — block device added
- `/org/freedesktop/UDisks2/jobs/<n>` — job created
- Various paths for filesystem, partition, loop interfaces

### `org.freedesktop.DBus.ObjectManager.InterfacesRemoved`
Emitted when an object disappears or loses an interface.

**Body**: `(object_path: o, interfaces: as)`

### `org.freedesktop.DBus.Properties.PropertiesChanged`
Emitted when properties of an existing object change.

**Body**: `(interface_name: s, changed: a{sv}, invalidated: as)`

**Observed for**:
- `org.freedesktop.UDisks2.Filesystem` — `MountPoints` changes
- `org.freedesktop.UDisks2.Block` — `IdLabel`, `IdUUID`, etc.
- `org.freedesktop.UDisks2.Drive` — various properties

### `org.freedesktop.UDisks2.Job.Completed`
Emitted when a long-running job finishes.

**Body**: `(success: b, message: s)`

## Object Path Patterns

| Pattern | Meaning |
|---------|---------|
| `/org/freedesktop/UDisks2/block_devices/<name>` | Block device |
| `/org/freedesktop/UDisks2/drives/<name>` | Drive object |
| `/org/freedesktop/UDisks2/jobs/<n>` | Job (integer ID) |

## Expected Signal Sequences

### loop-setup

Typical signal order observed on a local machine:

1. `InterfacesAdded` — `/org/freedesktop/UDisks2/jobs/N` with `org.freedesktop.UDisks2.Job`
2. `InterfacesAdded` — `/org/freedesktop/UDisks2/block_devices/<name>` with `org.freedesktop.UDisks2.Block`
3. `InterfacesAdded` — `/org/freedesktop/UDisks2/block_devices/<name>` with `org.freedesktop.UDisks2.Loop`
4. `PropertiesChanged` — various property updates
5. `InterfacesAdded` — `/org/freedesktop/UDisks2/drives/<name>` with `org.freedesktop.UDisks2.Drive`
6. `Job.Completed` — success=true
7. `InterfacesRemoved` — job object

*(Exact sequence may vary — this document will be updated with CI observations)*

### loop-delete

1. `InterfacesAdded` — job created
2. `InterfacesRemoved` — block device, drive
3. `Job.Completed` — success=true
4. `InterfacesRemoved` — job object

### mount

1. `InterfacesAdded` — job
2. `InterfacesAdded` — `org.freedesktop.UDisks2.Filesystem` on block device
3. `PropertiesChanged` — `MountPoints`
4. `Job.Completed`
5. `InterfacesRemoved` — job

### unmount

1. `InterfacesAdded` — job
2. `PropertiesChanged` — `MountPoints` cleared
3. `InterfacesRemoved` — `org.freedesktop.UDisks2.Filesystem`
4. `Job.Completed`
5. `InterfacesRemoved` — job

## Caveats

- Signal ordering is NOT guaranteed by D-Bus specification.
- Some signals may be coalesced or reordered by the bus daemon under load.
- In CI environments with limited resources, UDisks2 may exhibit
  different timing and ordering than on desktop systems.
