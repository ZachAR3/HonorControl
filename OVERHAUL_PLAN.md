# Honor Control overhaul plan

Status: implementation plan based on the repository at 2026-07-10. This document changes no runtime code.

## 1. Goal and implementation rules

Rebuild Honor Control into a safe, testable system service with thin GUI, tray, and CLI clients. Preserve the useful feature set; remove claims and controls that cannot be implemented or verified.

Rules for every work packet:

1. Keep hardware writes behind the root D-Bus service. Never add `sudo`, direct sysfs/EC writes, or an implicit in-process backend to a frontend.
2. Validate at the service boundary, verify writes by reading back when possible, and return a structured result. A persisted preference is not proof that hardware changed.
3. Treat unknown hardware, missing sensors, authorization failures, and timeouts as distinct states. Do not convert them to `False`, `0`, `""`, or `{}`.
4. Serialize hardware mutations. No two D-Bus requests or background controllers may write hardware/config concurrently.
5. Keep Qt's main thread free of D-Bus, filesystem, subprocess, and hardware I/O.
6. Add one focused test for each behavior changed. Run the smallest relevant test target while developing; run the full suite once at a work-packet gate.
7. Do not preserve pre-1.0 APIs solely for compatibility. Keep a temporary adapter only when it materially reduces migration risk.
8. Do not duplicate `honor-tools` algorithms. Put all interaction with that dependency behind one adapter and either use its public API or replace the dependency call with a small, tested local primitive.
9. Comments explain safety constraints and external quirks, not line-by-line mechanics.
10. A feature is “supported” only after its platform match, required resources, permissions, and safe read probe succeed.

## 2. Audit scope and baseline

Reviewed:

- all 7,177 lines of Python, shell, packaging, tests, and README content;
- the installed `honor-tools` 0.1.0 implementation used by the backend;
- the live service's read-only snapshot, systemd unit, file ownership, and recent service logs;
- one run each of tests, lint, compilation, and offscreen GUI construction.

Baseline results:

| Check | Result | Interpretation |
|---|---|---|
| `pytest -q` | 7 passed | Tests cover only polkit constant membership and charge-preset constants. |
| `ruff check honor_control tests` | passed | Style passes; architecture and behavior are largely untested. |
| `compileall` | passed | Modules parse/import. |
| offscreen `MainWindow` construction | passed | The installed service was online; this is not an interaction test. |
| live read-only snapshot/self-test | returned plausible data | D-Bus marshalling works on this machine; self-test checks mostly path existence. |
| recent fan logs | repeated EC writes every 2–4 seconds as temperature moved | Current speed-difference hysteresis does not adequately suppress write thrash. |

The directory has no Git metadata. Generated `.egg-info`, `__pycache__`, `.pytest_cache`, and `.ruff_cache` content exists. Treat cleanup as packaging work; do not delete user files casually during implementation.

## 3. Current system: responsibilities and failures

### 3.1 Runtime flow

```text
GUI pages ─┐
Tray       ├─ blocking dbus-python calls ─ system D-Bus ─ sdbus interfaces
CLI        ┘                                            │
   └─ when offline: constructs RealBackend directly ───┤
                                                       ▼
                           RealBackend (config + feature logic + diagnostics)
                              ├─ FanController asyncio task
                              ├─ GestureController asyncio task + executor select
                              ├─ PowerAutoSwitcher asyncio task
                              └─ honor-tools public/private calls + global monkeypatch
```

The intended privilege split is sound. The implementation bypasses or blurs it through direct CLI fallback, root-owned access to a selected user's home configuration, and feature logic mixed into the D-Bus process entry point.

### 3.2 Component inventory

| Current component | What it currently does | What it must own after the overhaul |
|---|---|---|
| `backend/service.py` | 1,392-line process entry point, config selection, hardware adapter, all feature services, three controllers, diagnostics | Process lifecycle and composition only; feature behavior moves into focused services/controllers. |
| `backend/dbus_api.py` | D-Bus signatures, variant codec, authorization calls, backend global, thread dispatch | Stable interface definitions, caller capture, authorization, error mapping, DTO codec, signals. No feature logic/global backend. |
| `backend/polkit.py` | Resolves sender through another bus, calls polkit synchronously, falls back to active-user authorization | Fail-closed async authorizer with explicit caller subject and stable action-to-method mapping. |
| `dbus_client.py` | Blocking system-bus client, catches all errors into empty values, exposes booleans | Typed transport with timeouts, error classification, reconnect, signal subscription, system/session bus injection. |
| `cli/honorctl.py` | Parser, presentation, commands, and implicit direct `RealBackend` fallback | Thin D-Bus client; deterministic exit codes and JSON; no hardware/config fallback. |
| GUI `main_window.py` | Performs eight sequential blocking calls at startup and three every five seconds | Window composition only; a controller/worker owns all I/O and state refresh. |
| GUI `state.py` | Untyped mutable caches that emit on every assignment | Typed immutable snapshots with loading/stale/error/pending metadata and change-only signals. |
| GUI pages | Mix rendering, direct D-Bus actions, extra polling, and status notifications | Render state and emit user intents. No transport calls. |
| `tray/tray.py` | Blocking polling and one D-Bus mutation per gesture | Snapshot-driven menu with one background transport and batch operations. |
| packaging/scripts | Installs an editable venv and patches absolute paths into root units | Reproducible staged system install/package plus isolated fake-hardware developer mode. |
| tests | Seven constant checks | Domain, controller, API, client, UI, CLI, packaging, and opt-in hardware coverage. |

### 3.3 Highest-risk findings

#### P0: safety/security

1. `honor.platform.detect()` falls back to an Honor 2024 platform for unknown CPUs. `RealBackend` then treats any non-null platform as gesture-capable and may expose EC writes on unsupported machines. Writes must be disabled unless a positive model match succeeds.
2. Polkit failure falls back to “active local user” for every action, including actions intended to require administrator authentication. A missing sender context is allowed. Remote D-Bus calls must fail closed; internal calls must bypass D-Bus authorization through a separate internal API, not a missing-sender shortcut.
3. The CLI constructs `RealBackend` when D-Bus is offline. This bypasses the architectural privilege boundary and can mutate user config or hardware depending on effective privileges/passwordless sudo.
4. The GUI and CLI expose WMI commands whose effect is explicitly unknown and which may hang ACPI until reboot. Availability of WMI plus `acpi_call` is incorrectly reported as “firmware control.” Remove execution from stable surfaces; retain read-only research metadata at most.
5. Fan curves accept malformed, unsorted, duplicate, out-of-range, decreasing, and single-point input. Manual speed has no service-side range check, persists an unclamped `last_speed`, has no timeout, and has no thermal fail-safe.
6. A fan-controller crash does not restore stock EC auto mode. A successful config save is reported as successful curve application even if the controller did not start.
7. GPU mitigation disables C-states and changes IRQ affinity without capturing/restoring previous state. The repository has no resume hook despite README claims.

#### P1: correctness/reliability

1. Every backend method is run in the default thread pool while sharing a mutable config object. Concurrent requests can interleave full-file saves, lose updates, or race background tasks.
2. Controller `start()` methods schedule task creation and return before `_task` exists. Repeated start/stop can create duplicate tasks or close descriptors still used by an executor call.
3. Blocking `honor-tools` reads/writes run inside the fan and power asyncio tasks. They can stall D-Bus processing and other controllers.
4. Battery setters persist desired thresholds even after hardware writes fail. `set_charge_mode()` ignores both write results and save results. The UI deliberately prefers configured values, hiding hardware failure.
5. `custom` is displayed and accepted by the CLI but rejected by `set_charge_mode()`.
6. Power profile application ignores the detailed write-result map. Unknown profile names silently apply the balanced values while recording the unknown name as current.
7. Fan stock/manual/custom mode is not modeled or persisted. Restart re-enables custom mode whenever a curve string exists; the GUI cannot distinguish manual from stock mode.
8. `reload_config()` does not validate transactionally, clear old errors, emit state, or reconcile controllers.
9. The development service uses the session bus, but every client always opens the system bus. Polkit sender resolution also always uses the system bus.
10. Client calls have no explicit short timeout. Any D-Bus or ACPI stall can freeze the GUI/tray for the library default timeout.
11. The client marks the whole service offline for authorization, validation, and feature-specific errors, then discards error names/messages.
12. D-Bus signals are not consumed. External state changes and background controller changes are invisible until polling, and some changes never emit.
13. Dynamic `a{sv}` values have no documented required fields/schema. `None` becomes the string `"None"`; operation failure is often indistinguishable from a valid empty value.
14. Debug export creates a root-owned `0600` file under systemd `PrivateTmp`; the returned `/tmp` path is not usable in the caller's namespace.
15. Gesture row rebuilds append deleted widgets to `_controls`, risking leaks or `RuntimeError` when later enabling/disabling dead Qt objects.
16. Gesture disable removes the custom mapping and enable restores the default, silently losing user customization.
17. GUI startup/polling and most button handlers call D-Bus synchronously on the Qt thread. Gestures adds multiple nested calls whenever health updates.

#### P2: maintainability/product accuracy

1. `RealBackend` imports private `honor-tools` helpers and monkeypatches `honor.fan.acpi_call`, even though the inspected dependency already implements read-after-write. Dependency changes are unversioned and unisolated.
2. Hardware paths are hard-coded to `BAT0`/`ADP1` in several layers despite platform fields.
3. Charge modes, profile metadata, capability labels, paths, and API knowledge are duplicated across backend, client, GUI, CLI, README, and packaging.
4. Dashboard treats missing capability data as “No” instead of unknown; service health can render “running” from an empty snapshot.
5. Power and fan profile lists are hard-coded to three names despite configurable profiles.
6. The Settings page displays a fixed refresh interval but provides no setting. It performs more direct blocking calls.
7. Diagnostics overall result fails optional/unsupported checks instead of distinguishing pass/warn/skip/fail. “Desktop” is read from the root service environment.
8. README claims firmware touchpad enable/disable commands that do not exist, omits the GPU D-Bus object, overstates working/safety status, and disagrees with polkit policy tiers.
9. The systemd hardening comment claims capabilities are limited, but no capability bounding set is configured. The service can read users' homes and production units contain a developer venv path after install script patching.
10. Packaging files are not part of the wheel. The tray desktop file is installed as an application, not an XDG autostart entry. No CI, build validation, or hardware-test separation exists.

## 4. Target architecture

```text
GUI / Tray                    CLI
    │ intents + snapshots       │ command + structured output
    ▼                           ▼
Qt AppController worker     async D-Bus client
          └──────────┬──────────┘
                     ▼
          org.honorlinux.Control1
          typed contract + timeouts
                     │
             D-Bus interface layer
       caller capture → polkit → error mapping
                     │
                     ▼
              ApplicationService
       ┌─────────────┼────────────────────┐
       ▼             ▼                    ▼
  feature services  SnapshotStore    RuntimeSupervisor
 battery/power/...   sequence+events   fan/gesture/power/GPU
       └─────────────┼────────────────────┘
                     ▼
        serialized HardwareCommandQueue
                     │
        HonorToolsAdapter + safe OS probes
                     │
            sysfs / EC / uinput / PPD

 ConfigStore: /var/lib/honor-control/state.toml
 Policy config: /etc/honor-control/config.toml
 Per-user GUI settings: QSettings
```

### 4.1 Boundary decisions

1. **One hardware owner:** only the root system service may instantiate `HonorToolsAdapter`. Frontends depend only on the D-Bus client protocol.
2. **System-global state:** battery, power, fan, gesture-daemon, and GPU settings affect the machine, not one login. Persist them under `/var/lib/honor-control`; never select an “active” user's home at service startup.
3. **One D-Bus stack:** use `sdbus` for service, client proxy, and polkit proxy. Remove `dbus-python` after the replacement client lands.
4. **Async outside, blocking inside one queue:** application methods are async. Blocking dependency calls run through an injected bounded executor. Hardware/config mutations use one global async lock/queue; controllers never call hardware directly.
5. **Immutable snapshots:** services publish typed snapshots to `SnapshotStore`. D-Bus reads return cached state quickly. Monitors update it; frontends subscribe to one state-change signal and use low-frequency polling only as recovery.
6. **Desired/applied/observed are separate:** persisted user intent, last successfully applied state, and live hardware observation must not overwrite one another.
7. **Stable failure vocabulary:** authorization, invalid input, unsupported hardware, unavailable resource, timeout, partial application, dependency incompatibility, and internal fault have distinct error codes.
8. **No stable experimental writes:** unknown WMI execution and unimplemented firmware settings are absent from normal GUI/API/README. A research-only tool, if retained, lives outside the service API, requires an explicit build extra and typed confirmation, and is never packaged by default.

### 4.2 Proposed source layout

```text
honor_control/
├── contract.py                  # bus constants, API/schema versions, wire keys
├── core/
│   ├── errors.py                # stable domain/transport error codes
│   ├── models.py                # frozen DTOs, enums, OperationResult
│   └── validation.py            # thresholds, profiles, curves, gestures
├── backend/
│   ├── service.py               # argument parsing, composition, signal shutdown
│   ├── application.py           # async use cases; no D-Bus or Qt imports
│   ├── config_store.py          # versioned load/migrate/atomic save
│   ├── snapshot_store.py        # sequence, immutable snapshot, subscriptions
│   ├── hardware.py              # HardwarePort + HonorToolsAdapter
│   ├── supervisor.py            # controller lifecycle and health
│   ├── features/
│   │   ├── battery.py
│   │   ├── power.py
│   │   ├── fan.py
│   │   ├── gestures.py
│   │   ├── gpu.py
│   │   └── diagnostics.py
│   ├── controllers/
│   │   ├── fan.py
│   │   ├── gestures.py
│   │   ├── power_source.py
│   │   └── suspend.py
│   └── dbus/
│       ├── api.py               # exported interfaces only
│       ├── codec.py             # DTO ↔ D-Bus conversion
│       └── authorizer.py        # caller subject + polkit
├── client/
│   ├── protocol.py              # client interface used by CLI/frontends
│   ├── sdbus_client.py          # async transport and signal subscription
│   └── errors.py                # transport-to-domain error mapping
├── cli/honorctl.py
└── frontend/
    ├── gui/
    │   ├── controller.py        # worker loop, commands, refresh/reconnect
    │   ├── state.py             # typed Qt-facing store
    │   └── pages/...
    └── tray/tray.py
```

Do not create empty layers. Add a file only when its work packet moves real behavior into it.

## 5. Core contracts

### 5.1 Capability model

Replace boolean capability flags with:

```text
status: supported | unavailable | disabled | unsupported | experimental
reason_code: stable machine-readable string
message: short user-facing detail
resources: optional list of detected paths/devices
```

Examples:

- battery sysfs missing: `unavailable / battery_sysfs_missing`;
- known non-Honor model: `unsupported / platform_not_supported`;
- gesture service switched off by policy: `disabled / disabled_by_policy`;
- WMI transport present but semantics unknown: `experimental / semantics_unverified`.

Never infer write support from path existence alone. A capability probe must check platform identity, required resource discovery, access mode, dependency support, and a non-mutating read.

### 5.2 Snapshot model

`SystemSnapshot` required fields:

| Field | Meaning |
|---|---|
| `api_version`, `schema_version` | Contract compatibility. |
| `sequence` | Monotonic state revision; increment only on meaningful changes. |
| `observed_at` | UTC timestamp generated by the service. |
| `service` | Version, uptime, overall health, controller health, last fault summary. |
| `platform` | Vendor/product/model match and detection confidence. |
| `capabilities` | Capability records keyed by feature. |
| `battery`, `power`, `fan`, `gestures`, `gpu` | Feature snapshots. |
| `stale_domains` | Domains whose live read failed; retain last known value. |
| `errors` | Bounded current fault records, not historical logs. |

Each feature snapshot contains `desired`, `applied`, `observed`, `available`, and `last_error` only where those concepts apply. Do not add placeholder keys with `None` unless the contract defines nullable data.

### 5.3 Operation result

Every mutation returns:

```text
status: success | partial | rejected | unavailable | failed
code: stable code, e.g. invalid_threshold or hardware_verify_failed
message: concise user-facing result
changed: whether desired or observed state changed
persisted: whether desired state was committed
applied: whether the complete hardware operation verified
sequence: resulting snapshot sequence
details: bounded feature-specific scalar/map data
```

Invalid input is a stable D-Bus error or `rejected` result; choose one convention and use it for all mutations. Recommended convention: D-Bus errors for authorization/API misuse; `OperationResult` for requested operations that reached the application layer, including partial hardware failure.

### 5.4 Service error names

Map exceptions explicitly:

```text
org.honorlinux.Control1.Error.NotAuthorized
org.honorlinux.Control1.Error.InvalidArgument
org.honorlinux.Control1.Error.Unsupported
org.honorlinux.Control1.Error.Unavailable
org.honorlinux.Control1.Error.Busy
org.honorlinux.Control1.Error.Timeout
org.honorlinux.Control1.Error.Dependency
org.honorlinux.Control1.Error.Internal
```

Log internal traceback and correlation ID server-side. Return only the stable name, correlation ID, and safe message to clients.

## 6. Ordered implementation work packets

Work packets are intentionally small enough for a coding agent to complete and verify in isolation. Do them in order unless a packet explicitly says it can run in parallel.

### WP-00 — Contain unsafe behavior before refactoring

**Depends on:** nothing.

**Current behavior:** unknown WMI commands are exposed; unsupported platforms inherit a default EC profile; polkit can downgrade admin actions; the CLI may instantiate the root backend.

**Implement:**

1. Remove firmware WMI execution buttons and setting `get/set` commands from normal GUI/CLI. Keep only a read-only explanation of unsupported firmware settings.
2. Make `firmware_run_wmi_command`, `firmware_get_setting`, and `firmware_set_setting` unavailable over the installed D-Bus API. If temporary compatibility methods remain, always raise `Unsupported` and never call ACPI.
3. Add a positive platform-support guard before every fan/firmware/GPU write. Until target detection exists, require recognized DMI vendor/product plus recognized CPU/model; a fallback `Platform` object is insufficient.
4. Change polkit failure behavior to deny. Remove `_is_local_active_user()` fallback for privileged methods and reject missing sender for exported calls.
5. Remove implicit `RealBackend` fallback from `honorctl`. When the service is absent, return an unavailable error and a single actionable start/install hint.
6. Correct README working-status and firmware/CLI claims immediately.

**Tests:** unsupported platform cannot call any EC/GPU write; polkit unavailable denies every mutation tier; no CLI command imports/constructs `RealBackend`; WMI compatibility endpoints execute zero hardware calls.

**Done when:** an installed unprivileged frontend has exactly one path to a hardware mutation: authorized D-Bus to the root service.

### WP-01 — Introduce domain DTOs, validation, and error vocabulary

**Depends on:** WP-00.

**Implement:**

1. Add `core/errors.py`, `core/models.py`, and `core/validation.py`.
2. Use frozen dataclasses and string enums. Keep models free of Qt, D-Bus, sdbus, and `honor-tools` types.
3. Implement strict constructors/parsers for:
   - threshold pair and charge mode;
   - profile identifier;
   - fan mode and curve points;
   - gesture identifier and exact key-combo tokens;
   - controller/capability health;
   - `OperationResult` and `SystemSnapshot`.
4. Centralize charge presets in the domain module. Delete backend/client duplicates after consumers migrate.
5. Validate unknown keys when decoding wire/config data. During the pre-1.0 migration, ignore only explicitly documented forward-compatible snapshot keys; never ignore mutation arguments.
6. Add `to_wire()`/`from_wire()` at one boundary module, not on every UI page.

**Tests:** table-driven valid/boundary/invalid values; DTO round trips; nullable fields; future-key policy; error-code stability.

**Done when:** feature code can communicate without raw untyped dicts or boolean mutation results.

### WP-02 — Replace active-user config with a versioned system state store

**Depends on:** WP-01.

**Target files:** new `backend/config_store.py`; simplify config logic in `service.py`; update systemd and install packaging later in WP-18.

**State split:**

- `/etc/honor-control/config.toml`: optional root-owned policy/hardware overrides; never rewritten by the service.
- `/var/lib/honor-control/state.toml`: service-owned desired state with `schema_version`.
- `QSettings` under the user's account: window geometry, refresh preference, notification preference only.

**Implement:**

1. Define a minimal state schema containing desired battery thresholds/mode, power profile and auto-switch policy, fan mode/curves, gesture mappings/enabled state, daemon enablement, and GPU mitigation enablement.
2. Load into a fresh immutable model, fully validate, then swap the active snapshot. A malformed file leaves the last-known-good state active and marks service health degraded.
3. Save by writing a same-directory temporary file, `flush` + `fsync`, mode `0640`, atomic `os.replace`, then fsync the directory. Keep one bounded `.bak` of the last valid file.
4. Serialize updates under one async lock. Mutators receive a snapshot, return a replacement, and cannot edit shared nested dicts.
5. Never create/write a file in a user's home as root.
6. Add an explicit one-time import command: client reads `~/.config/honor-tools/config.toml`, sends validated import data over D-Bus, and previews changes before commit. Do not auto-import based on the active seat.
7. On schema upgrade, apply ordered pure migration functions and rewrite only after the migrated state validates.
8. `ReloadConfig` reloads policy and state transactionally, computes a diff, reconciles controllers, and publishes one state revision.

**Tests:** missing file defaults; valid round trip; corrupt file recovery; atomic replacement failure; ownership/mode via mocked OS boundary; concurrent updates preserve both changes; migration fixtures; reload rollback; no home-directory access.

**Done when:** all runtime settings survive restart predictably and root never owns a user's configuration.

### WP-03 — Isolate `honor-tools` behind a hardware port

**Depends on:** WP-01. Can proceed alongside WP-02.

**Implement:**

1. Define a narrow `HardwarePort` protocol using domain models/results, grouped by feature. Tests use `FakeHardware`; production uses `HonorToolsAdapter`.
2. Move every `import honor.*` into `backend/hardware.py` or feature-specific adapter helpers. No other module may import `honor`.
3. Remove `_patch_acpi_call`. Add a compatibility test that verifies the installed `honor.fan.acpi_call` consumes results; require a compatible dependency version instead of mutating its global function.
4. Stop importing `_resolve_temp_hwmon`, `_resolve_battery_current_hwmon`, and `_read_cpu_model`. Request public upstream APIs or implement small local discovery functions against an injected filesystem root.
5. Translate dependency booleans/dicts into typed outcomes. Empty/missing reads become `Unavailable`, not numeric zero.
6. Inspect every detailed write result:
   - battery requires end and start write results plus readback;
   - power aggregates only resources actually present, identifies partial writes;
   - fan requires manual-mode success and a non-empty all-success command list;
   - GPU records each affinity/C-state write.
7. Discover battery/AC/hwmon paths; do not assume `BAT0`/`ADP1`. Prefer platform-declared paths only after verifying them.
8. Add an injected `root_path`/filesystem accessor for tests; do not monkeypatch global `pathlib.Path.exists` throughout tests.
9. At startup, check `honor-tools` version and a small capability surface. Incompatibility disables writes and reports a dependency health fault; the service may still serve diagnostics.

**Tests:** adapter contract against fakes and representative dependency results; absent/partial resources; no private imports; unknown dependency version; no `sudo` subprocess; path discovery with `BAT1`/alternate AC.

**Done when:** replacing or upgrading `honor-tools` requires changes in one adapter and its contract tests only.

### WP-04 — Build the async application service and serialized command queue

**Depends on:** WP-01 through WP-03.

**Implement:**

1. Add `ApplicationService`, `SnapshotStore`, `RuntimeSupervisor`, and feature service shells.
2. D-Bus calls invoke async application methods directly. Delete the pattern that wraps the entire mutable `RealBackend` method in unconstrained `asyncio.to_thread`.
3. Create a bounded hardware executor (`max_workers=1`) and a global async mutation lock. All blocking hardware calls enter through `HardwareCommandQueue.run(name, timeout, callable)`.
4. Give commands a correlation ID, start time, deadline, and bounded result details. Reject or queue conflicting mutations; do not silently interleave them.
5. Serve normal reads from `SnapshotStore`. Schedule refresh probes; do not make UI snapshot reads wait for a full sysfs scan.
6. `SnapshotStore.update(domain, replacement)` compares values, increments a 64-bit sequence only on change, stamps UTC time, and notifies subscribers once per transaction.
7. Keep last-known-good data on refresh failure and mark that domain stale with the current error. Clear staleness after a successful probe.
8. Supervisor owns all task creation/cancellation on the event-loop thread. `start()`/`stop()` are awaited, idempotent state transitions: `stopped → starting → running → stopping/failed`.
9. Task exceptions are captured into controller health. Safety cleanup runs in `finally` with a bounded timeout.
10. Shutdown order: stop accepting mutations, stop monitors, restore temporary hardware modes, flush state, unexport D-Bus objects, close bus/executor.
11. Add signal handling for SIGTERM/SIGINT through the event loop; do not rely on an event that is never set.

**Tests:** mutation serialization; timeout; cancellation; stale snapshot behavior; one sequence per transaction; duplicate start/stop; controller exception health; deterministic shutdown order.

**Done when:** no mutable config or hardware object is concurrently accessed from arbitrary executor threads.

### WP-05 — Define the final D-Bus contract and polkit boundary

**Depends on:** WP-04.

**Contract:** retain `org.honorlinux.Control1` as the first supported contract because the current package is alpha; add `GetApiVersion()` and `GetSchemaVersion()`. Treat any later breaking change as a new major interface.

**Root API:**

- `GetApiVersion() -> u`
- `GetSchemaVersion() -> u`
- `GetSnapshot() -> a{sv}`
- `Reload() -> a{sv}` operation result
- `StateChanged(t sequence, as domains)` signal

**Feature mutations:**

- Battery: `SetThresholds(i end, i start)`, `SetMode(s mode)`.
- Power: `SetProfile(s profile)`, `SetAutoSwitch(b enabled)`.
- Fan: `SetStockAuto()`, `SetCurve(s profile, a(ii) points)`, `SetManual(i speed, u ttl_seconds)`.
- Gestures: `SetMapping(s id, s combo)`, `SetEnabled(s id, b enabled)`, `SetDaemonEnabled(b enabled)`, optional atomic `SetAllEnabled(b enabled)`.
- GPU: `SetMitigationEnabled(b enabled)` with reversible state.
- Diagnostics: `RunChecks()`, `GetDebugBundle() -> s` bounded JSON, and authorized `GetRecentLogs(u lines) -> as`.

Do not export raw WMI commands, arbitrary files/commands, unimplemented firmware setting methods, or setters that only return `b`.

**Authorization implementation:**

1. Capture sender unique name and credentials while still in the D-Bus method context. Pass an immutable caller subject to the authorizer before any executor hop.
2. Use an async sdbus polkit proxy. Bound authorization calls by a short timeout and map challenge/deny/unavailable separately.
3. Fail closed if sender, credentials, polkit, or start time cannot be resolved. Internal controller calls use explicit internal application methods and never forge/omit a sender.
4. Validate `action_id` against the method's fixed declaration; never accept an action ID from a client.
5. Align policy tiers:
   - active local user: battery, normal power profile, gesture mappings;
   - admin authentication: fan mode/curve/manual, GPU mitigation, config import/reload, restricted logs;
   - debug-bundle snapshot can be active-local if redacted and returned to the caller rather than written by root.
6. Record action, caller UID/PID, result status, and correlation ID in journald without logging configuration contents or key mappings unnecessarily.
7. Development session-bus mode must use `FakeHardware` and an explicit development authorizer. It must refuse to start with real hardware writes.

**Codec/API tests:** golden introspection XML; every method signature; nested DTO round trip; no `None` string coercion; stable error names; sender capture; each method maps to one expected polkit action; polkit timeout/unavailable denies; signals carry correct sequence/domains.

**Done when:** D-Bus is a thin, versioned transport and all mutation authority is attributable to the actual caller.

### WP-06 — Replace `dbus-python` client with a typed async sdbus client

**Depends on:** WP-05.

**Implement:**

1. Add generated/declared async proxy interfaces that mirror only the final API.
2. Constructor accepts bus kind/object and per-call deadlines. Production defaults to system bus; tests inject a bus; developer frontend can explicitly select the fake session service.
3. Perform `GetApiVersion` handshake. Reject unsupported major versions with a clear upgrade mismatch; tolerate documented forward snapshot fields.
4. Decode into domain DTOs and `OperationResult`. Invalid wire data is a protocol error, not empty state.
5. Classify errors into service unavailable/name absent, timeout, not authorized, invalid request, feature unavailable, API mismatch, and internal service error.
6. Subscribe to `StateChanged`; coalesce bursts and fetch one new snapshot. Keep a low-frequency recovery poll with exponential backoff and jitter only while disconnected.
7. Reconnect on name-owner changes. A feature rejection must not set transport state offline.
8. Close the bus and signal tasks explicitly. Do not cache stale proxy objects after owner change.
9. Delete `CHARGE_MODES` and all interface constants duplicated from backend internals; import the stable contract/domain definitions.
10. Remove `dbus-python` from dependencies after polkit and all clients migrate.

**Tests:** fake bus success; each error classification; timeout; owner disappearance/reappearance; version mismatch; signal burst coalescing; feature failure leaves connection online; close cancels tasks.

**Done when:** GUI, tray, and CLI consume one client protocol and never receive raw D-Bus values.

### WP-07 — Battery feature service

**Depends on:** WP-01 through WP-06.

**Current behavior:** reads and writes hard-coded `BAT0`; derives charge mode from config; persists failed writes; accepts a non-functional `custom` mode.

**Target state:**

```text
available
capacity_percent?          # absent when unreadable, never fake 0
status?                    # charging/discharging/full/not_charging/unknown
ac_online?
observed_start/end?
desired_start/end
mode                       # off/home/travel/storage/custom, derived consistently
last_apply                 # OperationResult summary
```

**Implement:**

1. Discover a power-supply device containing charge-control threshold files. Record its path in capabilities. Discover AC independently.
2. Validate `40 <= start <= end <= 100`; optionally require `start <= end - 2` if the kernel driver requires hysteresis. Keep this rule in one validator and document it.
3. `SetMode` accepts only preset names. “Custom” is a display state produced when the threshold pair does not equal a preset; selecting custom opens/enables threshold inputs and does not call `SetMode("custom")`.
4. Mutation flow:
   1. authorize and validate;
   2. capture old observed/desired state;
   3. write both thresholds through the command queue;
   4. read both back;
   5. return `success` only if both match;
   6. persist desired values according to explicit policy: persist on full success; on partial hardware write, retain prior desired state and report both observed values;
   7. publish one battery snapshot revision.
5. If one of two writes succeeds, attempt a best-effort rollback to the old pair. Report rollback result; never claim atomic hardware behavior.
6. External threshold changes found by the monitor update observed state and set mode from observed values without overwriting desired state automatically.
7. Remove UI preference for configured thresholds over observed thresholds. Show both only when they differ, with a warning.

**Tests:** path discovery; missing start-threshold support; all boundary pairs; preset/custom derivation; end success/start failure; rollback; save failure after verified apply; readback mismatch; external change.

**Acceptance:** fake-hardware tests prove that no failed or partial write is rendered as successfully applied.

### WP-08 — Power profile and AC auto-switch service

**Depends on:** WP-04, WP-07 for shared AC observation.

**Current behavior:** accepts arbitrary profile names; ignores detailed RAPL/governor/EPP/PPD failures; stores configured rather than observed state; auto-switch is runtime-only, hard-coded to `ADP1`, and enabled through a nonexistent `services.power` field default.

**Target state:** available profile definitions, desired profile, last fully applied profile, observed governor/EPP/PPD/RAPL summary, AC source, persisted auto-switch policy, last apply details.

**Implement:**

1. Validate profile name against the loaded profile registry before any write. The API and GUI receive registry entries including name, display label, description, and whether each mechanism is applicable.
2. Define per-mechanism applicability from capability probes. Missing PPD is not failure when a profile has no PPD requirement; a present but failed required write is partial/failure.
3. Execute the dependency apply operation in the serialized command queue. Aggregate the returned map; verify observable governor/EPP/PPD/RAPL values after settle time.
4. Update `applied_profile` and persist `desired_profile` only after the defined critical mechanisms verify. On partial failure, keep the prior applied profile, retain observed details, and expose the incomplete mechanisms.
5. Do not silently substitute balanced for an unknown profile. Reject it before calling `honor-tools`.
6. Auto-switch policy contains `enabled`, `on_ac`, and `on_battery`; validate both referenced profiles. Persist it.
7. Use the shared battery/AC monitor event instead of polling a hard-coded file in a separate controller. Debounce source changes (for example 1–2 seconds) and apply only once per stable transition.
8. A manual profile selection while auto-switch is enabled lasts until the next actual AC transition; state/UI must say this explicitly.
9. Auto-switch application uses the same service method/result path as a manual change and emits the same snapshot update.
10. Prevent repeated failed auto-switch attempts from looping every poll. Record failure, back off, and retry only on a new source event or explicit user request.

**Tests:** unknown/custom profiles; mechanism applicability; partial per-CPU result; verification mismatch; persisted policy; stable AC transition/debounce; manual override; no repeated retry; restart reconciliation.

**Acceptance:** a profile card is selected as applied only after the service verifies its required mechanisms.

### WP-09 — Fan control and curve editor safety

**Depends on:** WP-03, WP-04, WP-08.

**Current behavior:** mode is inferred from whether a task runs; any parseable curve is accepted; the controller can thrash EC writes, block the event loop, duplicate tasks, or fail without restoring stock mode.

**Domain rules:**

1. Modes are `stock`, `curve`, and `manual_override`; mode is explicit in state.
2. A curve has 2–12 points, temperatures within the platform-safe range (default 40,000–100,000 m°C), speeds 0–100, strictly increasing temperatures, and non-decreasing speeds.
3. Platform policy defines a high-temperature requirement, e.g. at/above 95°C target must be 100%. Do not hard-code one rule for every future model; default to the conservative rule.
4. Manual override has a required TTL (recommended default 5 minutes, max 15) and is never persisted across restart. It cannot command below an emergency minimum at high temperature.

**Controller implementation:**

1. On startup always request stock auto first. Enter curve mode only after sensor and EC probes pass and desired mode is curve.
2. Resolve and retain sensor identifiers through the adapter. Treat missing/zero/out-of-range temperature as sensor failure, not 0°C.
3. Sample on the event loop but perform reads/writes through the hardware queue. Use temperature smoothing (small rolling median or EWMA), minimum target delta, and minimum write dwell time; constants live in policy/config with safe bounds.
4. Track target speed separately from last verified command and measured RPM. Never label target percentage as RPM/current fan speed.
5. After any temperature read failure, EC write failure, controller exception, manual TTL expiry, or service shutdown, attempt stock auto through a bounded safety cleanup. Mark failure prominently if restore cannot verify.
6. After N consecutive sensor/write failures, enter `failed_safe`, stop custom writes, and restore stock. Do not self-restart indefinitely.
7. `SetCurve` validates, saves the curve, and if that profile is active, restarts/transitions the controller. Result distinguishes `persisted=true` from `applied=true`.
8. `SetStockAuto` persists desired mode `stock`; restart must not re-enable a curve just because curves exist.
9. `SetManual` validates speed/TTL, transitions atomically from curve/stock, publishes expiry time, and returns to the persisted desired mode at expiry.
10. Add service/systemd stop cleanup. An `ExecStopPost` safety helper is acceptable only if it uses the same positively matched platform and fixed stock-auto operation; it must not duplicate arbitrary controller logic.

**GUI curve editor corrections:**

1. Use a `FanCurve` value model as the sole source of points. Graph and point list render it; they do not maintain two independently sorted lists.
2. Give each point a stable ID during editing so sorting cannot change the dragged/edited item.
3. Clamp pointer input to plot bounds. Align spinbox bounds with domain bounds.
4. Make the documented gesture match behavior: either single click adds or double click adds, not both. Keep keyboard-accessible Add/Delete controls.
5. Rebuild editor rows after a temperature edit changes ordering; preserve focus by point ID.
6. Display validation errors inline and disable Apply until valid. Show unsaved changes and reset confirmation.
7. Populate profiles from the service registry, not hard-coded names. Fetch/use the real default curve rather than copying the active override into `default`.
8. Render explicit stock/curve/manual/failed-safe modes and a manual-expiry countdown.

**Tests:** curve validation; interpolation boundaries; point identity/reorder; task lifecycle; write dwell/smoothing; sensor failure; EC failure; cleanup; manual expiry; restart mode; inactive-profile save; GUI offscreen add/drag/delete/apply.

**Hardware acceptance (explicit opt-in):** start from stock mode, apply one conservative curve, observe bounded writes for a fixed interval, simulate/trigger controller stop, verify stock restore, and record measured/target values. Never run this in normal CI.

### WP-10 — Gesture mapping and daemon lifecycle

**Depends on:** WP-03 through WP-06.

**Current behavior:** mappings are the enable flag; disabling deletes custom values; enabling restores defaults; device descriptors can race an executor `select`; firmware transport is mislabeled as usable control.

**State model:** each known gesture has ID, label, `enabled`, mapping, default mapping, and validation error. Persist enabled separately from mapping. Unknown custom gesture IDs are rejected unless a documented extension format is supported.

**Implement:**

1. Move static gesture metadata/default mappings into one domain registry or expose it through the hardware adapter. Backend, client, GUI, tray, and CLI consume the same records.
2. Validate gesture IDs exactly. Parse every key token; reject the whole mapping if any token is unknown. Return valid key names for UI completion/help.
3. `SetEnabled(false)` preserves the custom mapping. `SetEnabled(true)` reuses it; use the default only when no mapping has ever been set.
4. `SetMapping` does not implicitly enable unless the contract documents that behavior. Recommended: update mapping, preserve current enabled state.
5. Add atomic `SetAllEnabled` so the tray performs one authorized config transaction instead of N D-Bus calls.
6. Daemon controller owns descriptors on one execution context. Prefer event-loop `add_reader` for the nonblocking hidraw FD; if platform support requires a worker, that worker owns open/select/read/close and receives a stop event. Never close its FD from another thread while a blocking call is active.
7. Make start/stop awaited and idempotent. Publish device found/permission state, uinput state, running/failed state, last report time, and dispatch count.
8. Snapshot mappings immutably per dispatch or update through an atomic reference. Do not iterate a dict concurrently being mutated.
9. Rate-limit repeated identical reports if hardware bounce is observed; keep this behind tested policy rather than assumptions.
10. Define firmware fields honestly: `wmi_transport_present` and `firmware_settings_supported=false`. Do not use `firmware_control=true`.
11. GUI row reuse: keep rows keyed by gesture ID, update existing rows, delete/unregister removed rows, revert a toggle on failed result, and show per-row pending/error state.
12. Remove the large interactive WMI section from the standard page. Replace it with one compact unsupported-information card when relevant.

**Tests:** mapping token rejection; disable/enable preserves custom value; batch atomicity; concurrent mapping update during dispatch; descriptor shutdown; device permission errors; row reuse/control registry; failed toggle rollback.

**Hardware acceptance:** opt-in fake uinput or dedicated test account first; real test verifies a known report maps to press/sync/reverse-release events and all descriptors close on stop.

### WP-11 — Reversible GPU mitigation

**Depends on:** WP-03 through WP-06.

**Current behavior:** applies affinity and C-state changes once, cannot restore them, and is not re-applied by this repository after resume.

**Implement:**

1. Rename the feature to a model-specific “GPU latency mitigation”; do not present it as a universal fix.
2. Capability requires supported platform, matching GPU IRQs, an online valid target CPU, writable affinity, and writable C-state controls.
3. Before first apply, snapshot original IRQ affinity and every C-state disable value into `/run/honor-control/gpu-baseline.json` with strict permissions. Associate it with boot ID and discovered IRQ set.
4. Apply through the command queue; verify each value. Return partial detail and attempt restore on partial failure.
5. `SetMitigationEnabled(false)` restores the captured baseline and clears runtime state only after verification.
6. Persist desired enabled state, not the boot-specific baseline.
7. Subscribe to logind `PrepareForSleep`. After resume, rediscover IRQs, capture any new baseline as appropriate, and reapply once when desired. Do not install an unrelated shell hook and a service monitor simultaneously.
8. Expose the feature in an Advanced Power/Diagnostics section only on supported hardware, with effect, risk, current target CPU, and Restore action.

**Tests:** invalid/offline CPU; no IRQ; baseline capture; partial apply rollback; restore; boot-ID mismatch; resume reapply once; unsupported UI hidden.

**Acceptance:** enable → verify → disable returns every touched test value to its captured original state.

### WP-12 — Diagnostics, logs, and debug bundles

**Depends on:** WP-04 through WP-06.

**Current behavior:** checks path existence, fails optional items, reads root environment as desktop, returns inaccessible private `/tmp` paths, and exposes recent root-service logs without a clear redaction policy.

**Implement:**

1. Define diagnostic result severity: `pass`, `warning`, `skipped`, `fail`. Overall is fail only for required checks that fail; unsupported optional features are skipped.
2. Reuse real capability probes instead of duplicating path checks. Each check includes stable ID, severity, concise message, safe detail, remediation, and duration.
3. Split cheap cached checks from explicit active diagnostics. Active diagnostics remain non-mutating unless a separately named test explicitly says otherwise.
4. Remove desktop-session detection from the root service. GUI can add its desktop name locally when displaying/exporting a client section.
5. Build a bounded, schema-versioned JSON document in memory. Redact usernames/home paths where possible, environment variables, key mappings if unnecessary, and secrets. Cap logs/collection time/total payload.
6. D-Bus returns the JSON string/bytes. The unprivileged GUI/CLI selects and atomically writes the destination, so file ownership and namespaces are correct.
7. CLI adds `diagnostics export --output PATH`; GUI uses a save dialog. Never auto-copy a root path to clipboard.
8. Restrict recent logs by polkit or return a sanitized service-owned ring buffer. Enforce `1 <= lines <= 500` in the service, not only by clamping silently.
9. Health reports controller states, dependency compatibility, config validity, stale domains, last bounded faults, and uptime. A historical recovered error must clear from active health.
10. Avoid logging EC commands every polling iteration at INFO. INFO records transitions/failures; detailed telemetry belongs to DEBUG with rate limiting.

**Tests:** severity aggregation; optional skip; redaction fixtures; bundle size cap; caller writes output; no `/tmp` creation; log authorization/limits; health fault recovery.

**Acceptance:** an unprivileged caller can open its exported file, and the bundle contains no private-tmp path or unredacted test secret.

### WP-13 — GUI controller, worker, and typed state store

**Depends on:** WP-06. Individual pages can migrate after this packet.

**Implement:**

1. Add `frontend/gui/controller.py`. It owns an asyncio loop/client in a dedicated thread or a single-worker bridge. Create and use the sdbus connection on that worker context only.
2. Expose Qt signals for connection state, snapshot received, operation started/completed, and fatal protocol mismatch. Pages never receive the client object.
3. Startup performs one version handshake and one snapshot fetch. Subscribe to state signals, then use a slow recovery poll. Remove eight sequential calls in `MainWindow.__init__`.
4. Commands carry an operation ID/domain. Prevent duplicate command submissions per control while pending; do not block unrelated read rendering.
5. Typed GUI state stores last snapshot, connection status, stale metadata, and pending/error results by operation/domain. Emit only changed domains.
6. Transport offline preserves the last snapshot and marks it stale. Authorization/validation failure leaves connection online.
7. Main window owns controller start/stop, page registry, global banner, and navigation. Close waits a short bounded time for clean worker shutdown.
8. Replace manual `_controls` lists with state-derived enablement or a registry that supports unregister and uses safe weak Qt pointers.
9. Add a shared error/result presenter that maps codes to concise messages and optional detail. Remove every “(authorized?)” guess.
10. Store/restore window geometry and chosen page through QSettings. If refresh interval remains user-configurable, implement it; otherwise remove the fake setting.

**Tests:** assert client calls occur off Qt main thread; one startup fetch; signal refresh; reconnect; stale preservation; feature error does not show offline; pending deduplication; close cancels worker; dead control not referenced.

**Acceptance:** simulate a 10-second transport call and verify the Qt event loop continues processing a timer/input event while the operation times out in the worker.

### WP-14 — Migrate GUI pages one at a time

**Depends on:** WP-07 through WP-13 for the relevant feature. Each sub-packet is independently reviewable.

#### WP-14A — Dashboard

1. Render only `SystemSnapshot`; emit no commands.
2. Show platform match confidence and overall health.
3. Render capability `supported/unavailable/disabled/unsupported/experimental`; missing data is Unknown, never No.
4. Use observed battery/thermal/profile values and display stale timestamps.
5. Do not label target fan percentage as current RPM.

Tests: empty/loading/offline/stale/partial snapshot and dark/light palette smoke.

#### WP-14B — Battery

1. Show observed thresholds as primary; show desired mismatch warning.
2. Treat Custom as editor state, not an RPC preset.
3. Allow both start/end inputs if supported; show domain validation inline.
4. Pending state disables Apply only; success waits for returned verified snapshot sequence.
5. Revert controls or retain user draft predictably on failure; do not overwrite a dirty draft during polling without warning.

Tests: custom workflow, partial result, dirty-draft refresh, unavailable threshold file.

#### WP-14C — Power

1. Build accessible selectable controls from the service profile registry. Use buttons with keyboard/focus behavior rather than clickable frames.
2. Select the applied profile, not merely desired/current config.
3. Display partial mechanism failures and observed summary without dumping per-CPU maps by default.
4. Add persisted auto-switch toggle and on-AC/on-battery selectors; explain manual override duration.

Tests: custom profile registry, keyboard activation, partial apply, auto-switch validation.

#### WP-14D — Fan

Implement the editor/mode requirements from WP-09. Separate `FanCurveEditorModel` tests from paint-event smoke tests. Require explicit warning/TTL for manual mode.

#### WP-14E — Gestures

Implement row reuse, pending/error rollback, exact mapping validation/help, batch enable, and compact unsupported firmware information from WP-10. Do not trigger status calls from health render callbacks.

#### WP-14F — Diagnostics

1. Render severity-aware results and remediation.
2. Run active checks on the worker and allow cancel/timeout display.
3. Export through a save dialog in the user process.
4. Escape dynamic text or use plain-text widgets; do not concatenate untrusted detail into rich HTML.
5. Load logs on demand with bounded lines.

#### WP-14G — Settings

1. Keep only real user preferences and service metadata already in state.
2. Move machine config import/reload into a clearly privileged maintenance section.
3. Remove synchronous version/path calls and the fixed non-editable refresh “setting.”

**Page acceptance gate:** `PageBase` has no D-Bus client field, and `rg "self\.client|HonorClient" honor_control/frontend/gui/pages` returns no runtime use.

### WP-15 — Rebuild the tray as a snapshot client

**Depends on:** WP-06 through WP-10 and GUI controller worker primitives.

**Implement:**

1. Reuse a non-visual client worker rather than polling D-Bus on the Qt thread.
2. Retain a strong application-lifetime reference to the tray controller/icon/timer and close them explicitly.
3. Build profile and charge menus from snapshot registries/presets. Update check states with signals blocked.
4. Disable mutations while offline or pending; retain tooltip stale indication rather than formatting `None%`.
5. Gesture master toggle calls one atomic `SetAllEnabled`; result reflects full/partial failure and refreshes state.
6. Notifications use structured result messages. Restore the previous check state on rejection/failure.
7. Launch the GUI through the installed desktop application (`QDesktopServices`/desktop activation) or a single-instance mechanism. Avoid unlimited duplicate subprocesses and source-module assumptions.
8. Decide packaging behavior explicitly: opt-in XDG autostart entry or user service, not an application file that looks like autostart but is never activated.

**Tests:** offline/pending menu state; dynamic profiles; failed action rollback; batch gesture call count; lifetime/quit cleanup; GUI launch command in installed and dev modes.

**Acceptance:** delaying the service for 10 seconds does not freeze the tray menu or desktop shell interaction.

### WP-16 — Rebuild `honorctl` as a deterministic API client

**Depends on:** WP-06 and feature APIs.

**Implement:**

1. Parser functions return an exit code; command handlers never call `sys.exit()` internally.
2. Remove direct backend fallback and all imports of backend implementation classes.
3. Add global `--bus system|session` for explicit fake-development use, `--timeout`, and `--json`. Do not silently switch buses.
4. Use one command runner that maps client/domain errors to exit codes:
   - `0` success;
   - `2` usage/validation;
   - `3` unavailable/unsupported;
   - `4` operation partial/failed;
   - `13` not authorized;
   - `69` service unavailable;
   - `70` protocol/internal error.
5. JSON mode emits one documented object on stdout for success or failure and no human annotations. Human diagnostics go to stderr. Never embed `sys.exit` before JSON completes.
6. `status` fetches one coherent snapshot, not four sequential views.
7. Derive choices dynamically where argparse cannot know them: accept string then validate/display available values from service.
8. Remove `battery mode custom`; support `battery thresholds END START`.
9. Add complete safe fan mode commands, auto-switch policy, gesture batch, reversible GPU enable/disable, and diagnostics `--output` only after their service packets land.
10. Return nonzero for failed self-test required checks, failed/partial GPU operation, empty export, WMI removal, and all mutation failures.

**Tests:** parser matrix; stdout/stderr snapshots; every exit mapping; unavailable service; JSON failure; one snapshot call for status; no backend import.

**Acceptance:** CLI behavior is scriptable and identical whether invoked interactively or under CI; it never prompts for sudo.

### WP-17 — Dependency and package metadata cleanup

**Depends on:** WP-03 and WP-06.

**Implement:**

1. Pin a tested compatible `honor-tools` range after that package publishes a meaningful version/API. Until then, fail the adapter compatibility probe rather than assuming private behavior.
2. Remove `dbus-python`. Split optional dependencies if useful:
   - core/service: sdbus + honor-tools;
   - GUI/tray: PySide6;
   - dev: pytest, pytest-asyncio, ruff, build, packaging validators.
3. Add pytest/coverage/type-check configuration to `pyproject.toml`. Pick one supported type checker and keep strictness focused on core/contracts first.
4. Use one version source via `importlib.metadata` with a development fallback. Do not duplicate `0.1.0` in runtime code and package metadata.
5. Modernize license metadata and declare Python versions actually tested.
6. Add `.gitignore` for virtualenv/cache/build/egg-info artifacts. Remove generated artifacts from distribution inputs.
7. Ensure `python -m build` creates sdist/wheel with all Python runtime files and declared metadata. System packaging assets may remain outside the wheel if a staged installer/package owns them; document that boundary.

**Tests:** `pip check` in a clean project environment, wheel install/import/entry-point smoke, metadata assertions, no undeclared runtime imports. Ignore unrelated globally installed package breakage by using an isolated environment.

### WP-18 — Reproducible installation and systemd/D-Bus hardening

**Depends on:** stable service/client entry points from prior packets.

**Implement:**

1. Production units use stable `/usr/bin` or `/usr/libexec` paths. Never patch a developer's absolute venv/source path into `/etc`.
2. Add a staged installer/package layout with `DESTDIR` support for:
   - service executable/package;
   - systemd unit;
   - D-Bus service/policy;
   - polkit policy;
   - desktop application, icon, and optional tray autostart artifact.
3. Keep developer scripts non-invasive:
   - fake hardware + session bus, no root;
   - real hardware development uses the installed system service and explicit restart;
   - no session service that appears writable but cannot authorize.
4. Systemd unit uses `StateDirectory=honor-control`, `RuntimeDirectory=honor-control`, explicit `UMask`, start/stop timeouts, and clean SIGTERM handling.
5. Harden after measuring required access:
   - `ProtectSystem=strict` with only state/runtime writable;
   - no home access;
   - `PrivateTmp=true` is safe after debug export stops returning paths;
   - narrow address families;
   - device policy allowing only required hidraw/uinput nodes where practical;
   - capability bounding set derived from tested needs, not a misleading comment;
   - retain only kernel/proc access required for enabled features.
6. Do not use `MemoryDenyWriteExecute=false` without a verified dependency reason. Test setting true for the backend once PySide/dbus-python are absent.
7. D-Bus policy grants name ownership only to root/service identity and explicit interface calls to local users. Polkit remains the write decision point.
8. Polkit file descriptions/defaults exactly match the authorization table and code constants. Generate/test constants against policy XML to prevent drift.
9. Installer is idempotent, does not auto-enable/start without explicit documented choice, validates before reload, and uninstall removes only files it owns.
10. Add package-specific Arch/Debian metadata only if maintained in CI; do not keep multiple untested install paths.

**Validation:** `systemd-analyze verify`, D-Bus XML parse/introspection, polkit XML/action comparison, `desktop-file-validate`, staged install tree diff, install/upgrade/uninstall in a disposable VM/container, service start with missing optional hardware.

**Hardware acceptance:** verify service can access only required devices/sysfs/proc paths under the hardened unit and that disabled features do not require their access.

### WP-19 — Test architecture and CI

**Depends on:** incremental; add relevant layers with each packet, finalize here.

**Test layers:**

1. **Pure unit:** validation, DTOs, operation aggregation, curve/editor model, state reducer, CLI formatting.
2. **Feature service:** injected fake config/hardware/clock; success/partial/error/cancellation.
3. **Controller:** fake executor/file descriptors/events; lifecycle, backoff, cleanup, suspend.
4. **D-Bus contract:** isolated bus, fake application, caller/auth fixtures, introspection golden files, error mapping/signals.
5. **Client:** fake/isolated service, deadlines, reconnect, schema mismatch.
6. **Qt:** `QT_QPA_PLATFORM=offscreen`, controller fake, page state/actions, main-thread responsiveness.
7. **CLI:** invoke `main(argv)` or console entry point against fake service; snapshot stdout/stderr/exit.
8. **Packaging:** build artifacts, staged files, XML/desktop/systemd validators.
9. **Hardware:** pytest marker `hardware`, excluded by default, explicit model allowlist and read-only/write submarkers.

**Fixtures/fakes:**

- fake filesystem tree for power supplies, hwmon, IRQ, cpuidle, WMI;
- `FakeHardware` with queued outcomes and call trace;
- fake monotonic/UTC clock;
- fake authorizer with caller/action trace;
- representative config versions and corrupt files;
- D-Bus snapshot fixtures including nullable/stale/partial states.

**CI gates:**

1. Run format/lint/type/unit tests on supported Python versions.
2. Run D-Bus integration on Linux with required service packages.
3. Run Qt offscreen on one supported Python/Qt combination.
4. Build wheel/sdist and staged system files.
5. Upload test logs/artifacts only on failure; never include local hardware debug data.
6. Hardware suite remains a documented manual/pre-release gate, not a flaky hosted-CI job.

**Coverage priorities:** authorization and unknown-platform denial; config atomicity; write verification/partial failures; controller cleanup; API error preservation; GUI nonblocking behavior. Do not chase coverage percentage in paint-only code.

### WP-20 — Documentation and release truthfulness

**Depends on:** all implemented features; update safety corrections earlier in WP-00.

**Implement:**

1. Rewrite README from verified behavior and generated CLI `--help`; remove personal absolute paths.
2. Add `docs/architecture.md` containing boundaries, data flow, state semantics, and why hardware mutations are serialized.
3. Add `docs/dbus-api.md` generated/checked from contract definitions, including versions, required fields, errors, signals, and polkit actions.
4. Add `docs/hardware-support.md` with positive model identifiers, capability requirements, known limitations, and no fallback claims.
5. Add `docs/development.md` for fake session-bus workflow, focused test commands, full gate, and explicit hardware-test procedure.
6. Add `docs/safety.md` for fan fail-safe, GPU restore, authorization tiers, config locations, debug redaction, and recovery.
7. Add `CHANGELOG.md` and a migration note from `~/.config/honor-tools/config.toml` and old CLI/API.
8. Use status labels `verified`, `available but unverified`, `unsupported`, and `experimental`; never equate a path existing with a working feature.
9. Document that WMI firmware settings are unsupported. Do not publish unknown commands as user controls.

**Validation:** every README CLI example is exercised against the fake service; file paths match staged packaging; API/action tables are generated or contract-tested; no claim contradicts feature capability code.

## 7. Migration and delivery sequence

### Phase A — Safety and foundations

WP-00 → WP-01 → WP-02/WP-03 → WP-04. Ship only after unsafe endpoints and direct fallback are contained. It is acceptable for the GUI to expose fewer controls during this phase.

### Phase B — Contract and transport

WP-05 → WP-06 → WP-13. Land service and client contract changes in one coordinated branch/release. Add version handshake before removing old client behavior.

### Phase C — Feature vertical slices

Implement and ship one complete service-to-UI slice at a time:

1. WP-07 + WP-14B battery;
2. WP-08 + WP-14C power;
3. WP-09 + WP-14D fan;
4. WP-10 + WP-14E gestures;
5. WP-11 GPU advanced UI;
6. WP-12 + WP-14F diagnostics;
7. WP-14A/14G dashboard/settings cleanup.

For each slice: domain → adapter → application → D-Bus → client → UI/CLI → tests → docs. Do not build all backends first and defer all user-visible error handling.

### Phase D — Secondary clients and distribution

WP-15 tray → WP-16 CLI completion → WP-17 dependencies → WP-18 packaging → WP-19 CI finalization → WP-20 documentation.

## 8. Per-packet handoff template

Every coding agent should leave this information in its PR/commit notes:

```text
Packet:
Behavior before:
Behavior after:
Files intentionally changed:
Contract/schema changes:
Safety invariants affected:
Focused tests run:
Full gate run (once, at packet end):
Hardware tests run/not run:
Known follow-up explicitly deferred:
```

Review rejects a packet when it mixes unrelated formatting/reorganization, adds a second source of constants, suppresses an error into an empty value, or changes hardware behavior without a fake-hardware failure test.

## 9. Traceability: current files to destination

| Current file | Planned disposition |
|---|---|
| `honor_control/backend/service.py` | Reduce to composition/lifecycle; move feature methods/controllers/config. |
| `honor_control/backend/dbus_api.py` | Move to `backend/dbus/api.py`; replace global backend and generic thread wrapper. |
| `honor_control/backend/polkit.py` | Replace with async fail-closed `backend/dbus/authorizer.py`. |
| `honor_control/dbus_client.py` | Replace with `client/sdbus_client.py`; remove after all consumers migrate. |
| `honor_control/cli/honorctl.py` | Keep entry module; split parser/format helpers only if tests justify it. |
| `honor_control/frontend/gui/main_window.py` | Remove I/O; own controller and layout. |
| `honor_control/frontend/gui/state.py` | Replace dict caches with typed snapshot projection/pending state. |
| `honor_control/frontend/gui/widgets.py` | Keep presentation-only widgets; replace custom controls with native accessible widgets where possible. |
| `honor_control/frontend/gui/pages/*.py` | Remove client dependency; migrate one page per WP-14 sub-packet. |
| `honor_control/frontend/tray/tray.py` | Reuse async worker/snapshot; batch mutations. |
| `tests/test_validation.py` | Expand/split by layer; keep constant-policy consistency tests in contract suite. |
| `scripts/install-local.sh` | Replace with staged install/package workflow; no absolute-path patching. |
| `scripts/dev-run-*.sh` | Keep concise wrappers for explicit fake session mode or installed system service. |
| `scripts/smoke-test.sh` | Replace broad “real hardware if available” failures with deterministic software smoke plus opt-in hardware script. |
| `packaging/*` | Align with final contract/actions/paths and validate automatically. |
| `README.md` | Rewrite claims/examples from verified implementation. |

## 10. Final completion criteria

The overhaul is complete only when all are true:

1. Unknown hardware cannot perform any write.
2. All hardware mutations go through authorized system D-Bus and one serialized command queue.
3. Polkit failure/missing caller fails closed; policy/code/action tests agree.
4. No frontend imports backend implementation or writes config/hardware directly.
5. GUI and tray remain responsive through service absence and forced transport timeout.
6. Mutations return structured verified/partial results; no page treats config persistence as hardware success.
7. Fan controller has strict validation, write-rate control, manual TTL, crash cleanup, and stock-mode recovery.
8. Gesture custom mappings survive disable/enable; daemon start/stop cannot race descriptors.
9. GPU changes are reversible and resume behavior is owned/tested by one mechanism.
10. Debug bundles are caller-owned, accessible, bounded, and redacted.
11. System state is atomic under `/var/lib/honor-control`; root never writes the active user's home.
12. API handshake, error names, DTO codec, signals, reconnect, and introspection are contract-tested.
13. Feature services/controllers run against fake hardware in normal CI; real writes require explicit hardware test selection.
14. Production installation contains stable paths, validated systemd/D-Bus/polkit/desktop assets, and no developer venv path.
15. README support/CLI/security claims match the shipped code and tests.
