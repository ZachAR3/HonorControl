# Architecture

## Boundaries

1. **One hardware owner:** only the root system service may instantiate
   `HonorToolsAdapter`. Frontends depend only on the D-Bus client
   protocol.
2. **System-global state:** battery, power, fan, gesture-daemon, and GPU
   settings affect the machine, not one login. Persisted under
   `/var/lib/honor-control`; never select an "active" user's home.
3. **One D-Bus stack:** `sdbus` for service, client proxy, and polkit
   proxy. `dbus-python` is removed.
4. **Async outside, blocking inside one queue:** application methods are
   async. Blocking dependency calls run through one dedicated daemon worker.
   Hardware/config mutations use one global async lock/queue; controllers
   never call hardware directly.
5. **Immutable snapshots:** services publish typed snapshots to
   `SnapshotStore`. D-Bus reads return cached state quickly. Monitors
   update it; frontends subscribe to one state-change signal.
6. **Desired/applied/observed are separate:** persisted user intent, last
   successfully applied state, and live hardware observation must not
   overwrite one another.
7. **Stable failure vocabulary:** authorization, invalid input,
   unsupported hardware, unavailable resource, timeout, partial
   application, dependency incompatibility, and internal fault have
   distinct error codes.

## Data flow

```
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
   use cases + state transitions + lifecycle policy
        ┌─────────────┼────────────────────┐
        ▼             ▼                    ▼
    ConfigStore   SnapshotStore      RuntimeSupervisor
 atomic desired   sequence+events   refresh/fan/power/gesture
        └─────────────┼────────────────────┘
                      ▼
         serialized HardwareCommandQueue
                      │
         HonorToolsAdapter + safe OS probes
                      │
             sysfs / EC / uinput / PPD
```

## State semantics

- **Desired:** what the user asked for (persisted in
  `/var/lib/honor-control/state.toml`).
- **Applied:** the profile definition that matches the current complete
  hardware observation. A cached logical name never replaces live state.
- **Observed:** what the live hardware reports right now.

These are never conflated. A successful config save is not proof that
hardware changed, and a PPD label alone is not proof that a full definition
is active.

## Why hardware mutations are serialized

The EC (Embedded Controller) does not support concurrent writes. The
`acpi_call` kernel module requires reading the result after each write
before the next write can succeed (otherwise EBUSY). The
`HardwareCommandQueue` ensures:

- One daemon worker: no two hardware operations run concurrently and a wedged
  kernel/ACPI call cannot keep the Python process alive during shutdown.
- Global async mutation lock: no interleaving at the application level.
- Per-command timeout: callers return promptly; the queue rejects new work as
  busy until the non-cancellable timed-out call has really completed.
- Safety-critical fan calls reserve stock-auto restoration as the next worker
  item on timeout; later work remains blocked until that recovery completes.
- Correlation ID: every operation is auditable.

## Source layout

```
honor_control/
├── contract.py                  # bus constants, API/schema versions, wire keys
├── core/
│   ├── errors.py                # stable domain/transport error codes
│   ├── gestures.py              # gesture definitions and decoding
│   ├── models.py                # frozen DTOs, enums, OperationResult
│   ├── touchpad.py              # typed firmware protocol constants/encoding
│   └── validation.py            # thresholds, profiles, curves, gestures
├── backend/
│   ├── service.py               # argument parsing, composition, signal shutdown
│   ├── application.py           # async use cases; no D-Bus or Qt imports
│   ├── config_store.py          # versioned load/migrate/atomic save
│   ├── snapshot_store.py        # sequence, immutable snapshot, subscriptions
│   ├── hardware.py              # HardwarePort + HonorToolsAdapter + FakeHardware
│   ├── supervisor.py            # controller lifecycle and health
│   ├── command_queue.py         # serialized hardware command queue
│   ├── gesture_runtime.py       # hidraw reader + uinput virtual keyboard
│   ├── touchpad_firmware.py     # exact-gated HID firmware transport
│   └── dbus/
│       ├── api.py               # exported interfaces only
│       ├── codec.py             # DTO ↔ D-Bus conversion
│       └── authorizer.py        # caller subject + polkit
├── client/
│   ├── protocol.py              # client interface used by CLI/frontends
│   ├── sdbus_client.py          # async transport and signal subscription
│   └── errors.py                # transport-to-domain error mapping
├── cli/
│   ├── honorctl.py              # unprivileged typed D-Bus CLI
│   └── touchpadctl.py           # standalone exact-gated firmware CLI
└── frontend/
    ├── gui/
    │   ├── controller.py        # worker loop, commands, refresh/reconnect
    │   ├── state.py             # typed Qt-facing store
    │   └── pages/...
    └── tray/tray.py              # integrated + optional tray-only frontend
```
