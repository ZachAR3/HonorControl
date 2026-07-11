# D-Bus API

## Interface: `org.honorlinux.Control1`

Object path: `/org/honorlinux/Control1`

### Methods

#### Read-only (no authorization)

| Method | Signature | Description |
|---|---|---|
| `GetApiVersion` | `() -> u` | D-Bus API version (currently 1) |
| `GetSchemaVersion` | `() -> u` | Snapshot schema version (currently 3) |
| `GetSnapshot` | `() -> a{sv}` | Current system snapshot |
| `RunChecks` | `() -> a{sv}` | Run diagnostic checks |

#### Battery (active local user)

| Method | Signature | Description |
|---|---|---|
| `SetThresholds` | `(i end, i start) -> a{sv}` | Set charge thresholds |
| `SetMode` | `(s mode) -> a{sv}` | Apply charge mode preset |

#### Power

| Method | Auth | Signature | Description |
|---|---|---|---|
| `SetProfile` | active user | `(s profile) -> a{sv}` | Apply power profile |
| `SetAutoSwitch` | admin | `(b enabled) -> a{sv}` | Enable/disable saved auto-switch policy |
| `SavePowerProfile` | admin | `(sss i i sss b i) -> a{sv}` | Create/update all profile variables |
| `DeletePowerProfile` | admin | `(s name) -> a{sv}` | Delete an unreferenced custom profile |
| `ConfigureAutoSwitch` | admin | `(b enabled, s on_ac, s on_battery, s ac_script, s battery_script) -> a{sv}` | Save complete transition policy |

#### Fan (admin auth)

| Method | Signature | Description |
|---|---|---|
| `SetStockAuto` | `() -> a{sv}` | Restore stock auto mode |
| `SetCurve` | `(s profile, a(ii) points) -> a{sv}` | Set fan curve |
| `SetManual` | `(i speed, u ttl_seconds) -> a{sv}` | Manual speed with TTL |

#### Gestures (active local user)

| Method | Signature | Description |
|---|---|---|
| `SetMapping` | `(s id, s combo) -> a{sv}` | Set gesture key combo |
| `SetEnabled` | `(s id, b enabled) -> a{sv}` | Enable/disable gesture |
| `SetAllEnabled` | `(b enabled) -> a{sv}` | Batch enable/disable |
| `SetDaemonEnabled` | `(b enabled) -> a{sv}` | Toggle gesture daemon |

#### GPU (admin auth)

| Method | Signature | Description |
|---|---|---|
| `SetMitigationEnabled` | `(b enabled) -> a{sv}` | Apply/restore GPU mitigation |

#### Config / diagnostics

| Method | Signature | Auth | Description |
|---|---|---|---|
| `Reload` | `() -> a{sv}` | admin | Reload config |
| `GetDebugBundle` | `() -> a{sv}` | active user | Bounded redacted debug bundle |
| `GetRecentLogs` | `(u lines) -> as` | admin | Recent log lines (1-500) |

### Signals

| Signal | Signature | Description |
|---|---|---|
| `StateChanged` | `(t sequence, as domains)` | Emitted on state change |

### Error names

| Error name | Meaning |
|---|---|
| `org.honorlinux.Control1.Error.NotAuthorized` | Polkit denied |
| `org.honorlinux.Control1.Error.InvalidArgument` | Validation failed |
| `org.honorlinux.Control1.Error.Unsupported` | Hardware not supported |
| `org.honorlinux.Control1.Error.Unavailable` | Resource unavailable |
| `org.honorlinux.Control1.Error.Busy` | Conflict with running operation |
| `org.honorlinux.Control1.Error.Timeout` | Operation timed out |
| `org.honorlinux.Control1.Error.Dependency` | honor-tools incompatible |
| `org.honorlinux.Control1.Error.Internal` | Internal fault |

## Polkit actions

| Action ID | Tier |
|---|---|
| `org.honorlinux.control.set-charge-limit` | active local user |
| `org.honorlinux.control.set-power-profile` | active local user |
| `org.honorlinux.control.configure-power` | admin auth |
| `org.honorlinux.control.set-gestures` | active local user |
| `org.honorlinux.control.export-debug` | active local user |
| `org.honorlinux.control.view-logs` | admin auth |
| `org.honorlinux.control.set-fan-curve` | admin auth |
| `org.honorlinux.control.set-gpu-irq` | admin auth |
| `org.honorlinux.control.reload-config` | admin auth |
