# Windows support conversion plan

Status: planning only, reviewed 2026-09-07. This document does not claim a
Windows application, Windows hardware adapter, or Windows release exists. The
current product is a Linux system service and Qt 6 application. Windows work
starts only after the gates below have produced reproducible evidence.

## Decision summary

The Windows implementation will preserve the current ownership invariant: one
machine service owns hardware and state, while GUI, tray, CLI, and any future
session agent remain unprivileged clients. The Windows service will use the
Service Control Manager (SCM), a local named pipe, Windows security tokens,
SetupAPI/Configuration Manager, and fixed typed native adapters. It will not
reuse Linux D-Bus, polkit, systemd, sysfs, hidraw, uinput, `acpi_call`, shell
hooks, or `/var/lib` paths.

The first Windows deliverable is read-only. It may report exact platform and
device facts, capabilities, telemetry that has passed comparison, service
health, and redacted diagnostics. A stable release needs at least one
reversible, target-validated control. The likely first control is a documented
Windows power operation or the recovered touchpad HID setting writer. Neither
is selected until its scope, permissions, readback, lifecycle, and recovery
tests pass.

The OEM touchpad evidence resolves two separate paths:

```text
ordinary settings:
  typed 9-byte HID Output report -> vendor collection -> overlapped WriteFile
master enable:
  ROOT\WMI OemWMIMethod.OemWMIfun -> typed OEM WMI call
```

The old HNOs2EC IOCTL and guessed WMI paths remain research material. They are
not an alternate implementation route.

## Evidence and repository baseline

Use these evidence labels in issues, code, and release notes:

- **Repository-confirmed:** represented by tracked source, tests, or the
  compact protocol documents and reproduced from them.
- **Platform-documented:** specified by Microsoft but not exercised on the
  supported Honor laptop under the final service identity.
- **Target-validated:** executed on an allowlisted laptop, Windows build,
  driver set, and final service identity with captured input, output, and
  restore evidence.
- **Experimental:** a candidate path exists but permissions, semantics,
  readback, coexistence, or recovery are incomplete.
- **Unavailable:** a required predicate failed; no fallback or guessed API is
  allowed.

The current tracked protocol sources are:

- `docs/protocol/touchpad.json` — machine-readable endpoint, commands, values,
  reports, and support bits;
- `docs/protocol/touchpad-porting-spec.md` — discovery, ordering, failure, and
  lifecycle contract;
- `docs/gesture-linux-remaining-work.md` — current Linux implementation and
  unexecuted physical validation gates;
- `experimental/touchpad-wmi/` — optional Linux WMI bridge, explicitly not
  qualified or installed by the main product.

The `Reverse engineering/` directory is ignored by `.gitignore` and is not a
portable release input. It contains local captures, proprietary/OEM binaries,
duplicate artifacts, and Windows-only research scripts. A future Windows
implementation may use that directory on the investigator's machine, but the
build, tests, and plan must not require it. Before relying on a local claim,
preserve a sanitized, tracked evidence record with source path, SHA-256,
capture date, machine/build/driver versions, method, and confidence. Do not
commit OEM binaries, drivers, or raw user data without a license and review.

Current implementation facts that affect this plan:

| Area | Current state | Windows implication |
|---|---|---|
| Runtime | Python `>=3.11`; shared domain models and async application service | Keep neutral code importable; add Windows composition separately |
| Profile loading | `core/touchpad_profile.py` validates the shared TOML shape and deliberately opens a caller-supplied profile file | Keep protocol/validation code neutral, but treat profile file I/O as an explicit CLI boundary; do not assume every `core` module is I/O-free |
| Transport | `sdbus==0.14.2` is Linux-only and now environment-marked | A Windows client factory must avoid importing D-Bus modules |
| GUI | PySide6 optional; controller constructs `SdbusClient` directly | Inject a platform client before claiming Windows GUI support |
| Backend | `ApplicationService` and `HardwarePort` import Linux gesture/touchpad/hardware code | Extract ports and platform composition first |
| Persistence | ConfigStore uses Linux defaults and POSIX atomic/permission operations | Inject paths, ACL, and replacement policy |
| Logs | Recent logs use `journalctl` | Add a Windows Event Log/rolling-file reader |
| Packaging | Bash installers, systemd, D-Bus, polkit, `/opt`, `/usr`, and `/etc` | Keep these Linux-only; add a Windows build/install stack |
| Touchpad | Typed HID encoder and Linux transport are available; physical replay/readback gates remain separate | Port the typed encoder, implement a Windows endpoint adapter, and preserve accepted/unknown semantics |
| WMI bridge | Linux kernel prototype is static-only and not installed or runtime-qualified | Windows WMI is an undocumented OEM path and needs its own query/change/restore gate |

The package metadata intentionally retains its Linux description and classifier
until a Windows implementation exists. The Linux D-Bus dependency marker is a
preparatory install fix; it is not a Windows support claim.

## Current architecture and portability blockers

The Linux flow is:

```text
GUI/tray/CLI -> async sdbus client -> system D-Bus -> polkit
  -> ApplicationService -> ConfigStore/SnapshotStore/Supervisor
  -> serialized HardwareCommandQueue -> HonorToolsAdapter
  -> sysfs, EC, PPD, hidraw, uinput, or Linux helpers
```

The shared invariants worth preserving are one hardware owner, serialized
mutations, desired/applied/observed separation, immutable snapshots, explicit
capability status, stable errors, and no automatic replay of an ambiguous
mutation. Protocol and validation helpers should stay import-safe and free of
hardware access. `core/touchpad_profile.py` is a deliberate boundary
exception: its parser is neutral, while its public loader performs file I/O on
a caller-supplied path. A future Windows client should inject a bounded path
and size policy rather than moving that file access into a privileged adapter.

The following imports and assumptions block a Windows import-safe build:

1. `backend/hardware.py` imports `gesture_runtime` and
   `touchpad_firmware`, which import `fcntl`, hidraw, uinput, and Linux paths.
   Move `HardwarePort` and neutral result types to an import-safe ports module;
   retain compatibility re-exports while Linux code migrates.
2. `backend/application.py` imports the Linux gesture runtime, Linux WMI probe,
   touchpad lock, `signal`, `stat`, `shlex`, and journal-oriented transitions.
   Inject gesture, diagnostics, log, transition, and persistence ports.
3. `client/proxy.py` imports `sdbus` at module import time;
   `client/sdbus_client.py` now defers transport imports until connection.
   Add `client/factory.py` to choose the transport without loading Linux code.
4. `frontend/gui/controller.py` constructs `SdbusClient` directly. It must
   depend only on `ControlClient`, with fake-client injection for tests.
5. `core/gestures.py` combines semantic report decoding with Linux key codes;
   move key/action tables to the Linux platform and keep semantic events
   shared.
6. `backend/dbus/codec.py` combines neutral DTO conversion with D-Bus variant
   wrapping. Extract a plain-Python codec and make D-Bus and pipe codecs use
   the same golden fixtures.
7. ConfigStore hard-codes Linux ownership, modes, and directory fsync rules.
   Add a persistence platform port with explicit schema migrations and safe
   replacement semantics.
8. GUI/tray startup and diagnostics use freedesktop paths, `systemctl`, and
   POSIX process behavior. Windows startup, mutex, logging, and service help
   must be separate implementations.

Do not move files merely for symmetry. Introduce neutral ports, prove Linux
behavior unchanged, then add Windows siblings. A Windows import smoke test
must fail if it imports `sdbus`, `fcntl`, Linux `signal`/uinput/hidraw code,
`journalctl`, or systemd composition.

## Windows architecture

### Processes and ownership

The planned process model is:

| Process | Identity/scope | Responsibility |
|---|---|---|
| `HonorControlService.exe` | SCM own-process service, initially `LocalService` plus service SID | Machine state, serialized hardware, IPC, diagnostics, lifecycle |
| `HonorControl.exe` | Unelevated interactive user | GUI client |
| `HonorControlTray.exe` | Unelevated interactive user | Tray client and per-user startup |
| `honorctl.exe` | Unelevated interactive user | CLI client and stable JSON/human output |
| `HonorControlElevate.exe` | One-shot UAC elevated helper, only if needed | Redeem one exact service-issued operation |
| `HonorControlSession.exe` | Unelevated active-session process, deferred | Custom gesture action delivery only after session/reader tests |

The service must not create windows, inject input into session 0, use an
interactive user profile, or launch arbitrary commands. All machine hardware
access remains behind the service and command queue. Per-user UI preferences
live in the user profile; machine desired state lives in ProgramData.

### Service identity and lifecycle

Start with `LocalService` and an enabled service SID. Measure the exact
privileges and device ACLs under that identity; do not infer them from Honor
software running as `SYSTEM`. Keep `SeImpersonatePrivilege` only if the final
named-pipe token design requires it and a probe proves the need. If one
feature genuinely needs more privilege, omit that feature or design a tiny,
separately reviewed broker with a service-SID-only pipe and fixed typed calls.
Do not make the whole service `LocalSystem` for convenience.

The SCM host must:

- register its control handler immediately;
- report `RUNNING` only after state validation, IPC readiness, read-only
  discovery, and initial snapshot publication;
- reject new mutations during stop/preshutdown, close or cancel native
  handles, flush bounded logs, and exit within a fixed budget;
- configure bounded crash recovery with a reset period and escalating delays;
- register and unregister power/device notifications explicitly;
- invalidate a hardware generation on removal, resume, driver restart, or
  cancellation ambiguity;
- rediscover on arrival/resume, refresh observed state, and reconcile only
  explicitly enabled target-validated settings;
- handle cold boot, service restart, sleep, hibernate, Modern Standby where
  present, Fast Startup, and Explorer/session changes as separate cases.

Device notifications are hints. Safety must not depend on receiving a
pre-sleep event. A timeout or cancellation does not prove a write did not
reach hardware; poison the queue/handle generation and require rediscovery or
an explicit readback before another mutation.

### Named-pipe IPC

Use a local duplex byte-mode pipe such as
`\\.\pipe\HonorControl-v1`. Define a closed request/response/event schema:

```json
{"type":"hello","id":1,"transport":1,"api":{"major":1,"minor":0},"schema":{"min":5,"max":5},"client":"gui"}
{"type":"request","id":2,"method":"snapshot.get","params":{},"deadline_ms":5000}
{"type":"response","id":2,"result":{"sequence":42,"snapshot":{}}}
{"type":"event","event":"state_changed","sequence":43,"domains":["power"]}
```

Use a four-byte little-endian length prefix followed by strict UTF-8 JSON,
overlapped I/O, `PIPE_WAIT`, `PIPE_REJECT_REMOTE_CLIENTS`, and
`FILE_FLAG_FIRST_PIPE_INSTANCE` on the first server instance. Bound frames
(initial ceiling 1 MiB), JSON depth/count/string sizes, connections, requests,
event queues, and deadlines. Reject duplicate keys and non-finite numbers.
Cancel pending I/O with `CancelIoEx`, but preserve unknown-outcome semantics
for a native operation that may have completed. Retain every buffer and
`OVERLAPPED` object until completion is observed; cancellation requests do
not complete the I/O synchronously. Drain completions before releasing owned
resources. See [CancelIoEx](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelioex).

Set an explicit DACL. Clients need only the individual connect/read/write
rights required by the protocol. Do not grant `FILE_GENERIC_WRITE`: Microsoft
documents that its `FILE_APPEND_DATA` bit aliases
`FILE_CREATE_PIPE_INSTANCE`, which can allow a client to create a pipe
instance. Test that a standard user can use an allowed client endpoint but
cannot squat or create a server instance. Deny anonymous and remote access;
use a logon SID if access must be limited to one interactive session.

The client verifies the server PID with `GetNamedPipeServerProcessId`, compares
it with the SCM service process, resolves the expected immutable Program Files
image, and checks its signed identity before sending requests. PID checking is
additional evidence, not a replacement for the DACL and local-only flags.

Authenticate every request on the worker that read it:

1. call `ImpersonateNamedPipeClient` and check the return value;
2. read bounded facts from the thread token (user SID, groups, session,
   elevation state, integrity, and logon type where available);
3. apply a fixed method policy; ignore identity fields supplied in JSON;
4. always `RevertToSelf` in cleanup, and close the connection on any failure.

Initial policy is intentionally conservative: local authenticated clients may
read snapshots and redacted diagnostics; every machine policy or hardware
mutation requires an elevated administrator token. Revisit this per method
only after a threat review.

If a standard user needs approval, the service issues a cryptographically
random, one-use, short-lived nonce bound to the canonical operation, initiating
SID/session, request, hardware generation, and expiry. The absolute signed
elevation helper receives only that nonce and transport version. On redemption
the service verifies elevation, administrator membership, nonce freshness,
operation hash, session, generation, and one-time use. Log initiating and
approving SIDs for over-the-shoulder approval. The helper must never accept an
arbitrary executable, device path, native command, or JSON parameter blob.

## Native hardware adapters

`WindowsHardwareAdapter` implements the shared high-level port and is called
only by `HardwareCommandQueue`. Keep fixed helpers for discovery, HID, WMI,
power/telemetry, touchpad, logging, and a deterministic fake backend. No IPC
or config field may choose a native path or pass raw bytes.

### Discovery and allowlisting

Use SMBIOS/WMI for identity and SetupAPI/Configuration Manager for the device
tree. Enumerate all matching HID collections and inspect usage page, usage,
report IDs, and input/output lengths. A writable feature requires every
predicate relevant to that feature:

- exact vendor/product and validated SKU row;
- exact PnP ancestry and HID collection usage;
- VID/PID and descriptor/capabilities;
- report ID and lengths;
- WMI class/method/instance and strict input/output shape where applicable;
- access under the final service token;
- no conflicting Honor reader/writer;
- current hardware generation and explicit experimental opt-in.

Record BIOS, EC, Windows, HID, and Honor software versions for evidence, but
do not make a release/BIOS string a selector unless a matrix proves it is
necessary and sufficient. Zero or ambiguous matches are unavailable. Do not
leak arbitrary device paths to unprivileged clients.

### Touchpad HID settings

The repository-confirmed target is I2C HID `35cc:0104`, vendor application
usage page `0xff00`, usage `0x0001`, report ID `0x0e`, and nine-byte input and
output reports including the report ID. The Windows-observed release `0x0101`
is evidence, not a selector. The setting report is:

```text
0e CC VV 00 00 00 00 00 00
```

The canonical commands are sensitivity `01`, vibration `02`, press text `03`,
press picture `04`, three-finger drag `05` followed by `11`, mouse-like mode
`06`, edge brightness `07`, edge volume `08`, edge control center `09`, edge
close/minimize `0a`, knuckle screenshot `0b`, and knuckle recording `0c`.
Values are typed and bounded by `docs/protocol/touchpad.json`.

The adapter must enumerate and verify the collection, validate the full
profile before opening, open with the minimum tested access/share flags, use
overlapped `WriteFile`, require exactly nine bytes accepted, serialize writes,
and report the exact accepted count. A write completion is transport evidence;
the recovered protocol has no setting-value readback. Therefore a timeout or
disconnect is `outcome_unknown`, partial application never claims rollback,
and no automatic retry is allowed. Reapply after resume only when the current
generation and opt-in persistence gates pass.

Use a reviewed SetupAPI/HID wrapper or a proven binary HID package only after
the latter exposes every needed identity/capability and packages safely. Do
not use `DeviceIoControl`, `HidD_SetFeature`, guessed feature reports, or old
EC IOCTLs for these settings.

Preserve the startup/resume clock vector `0e 00 TT TT TT TT TT 00 00`, with
Unix seconds in five little-endian bytes, and the support request
`0e f0 00 00 00 00 00 00 00`. Route asynchronous support responses separately
from gesture events through the single reader owner. Test stale responses,
unrelated events, timeout, and removal; do not mistake a successful request
write for a received support bitmap.

### OEM WMI master switch

The master switch is a separate undocumented OEM path:

| Field | Contract |
|---|---|
| Namespace | `ROOT\\WMI` |
| Class/method | `OemWMIMethod` / `OemWMIfun` |
| Instance | `ACPI\\PNP0C14\\HWMI_0` |
| Method ID | `1` |
| Input | 64 zero-filled bytes; little-endian `u64` command in first 8 bytes |
| Output | 256-byte MOF payload; status byte 0, enabled byte 1 |
| Query/disable/enable | `0x00000f02` / `0x00001002` / `0x00011002` |

This contract is static/reverse-engineering evidence. First perform query-only
under the final service identity and with/without PC Manager/provider. A write
needs external pointing-device recovery, query/change/query/restore, service
restart, device removal, resume, and firmware matrix evidence. Strictly reject
unexpected object type or output length. Never brute-force WMI methods, use a
generic WMI executor, or expose raw WMI commands.

Initialize COM on the actual worker thread that uses WMI, select and test
its apartment model, balance successful initialization with teardown, and
marshal interfaces rather than copying apartment-bound objects between Qt,
asyncio, and worker threads. Fix the process/proxy COM security policy before
queries and test it under the service token. A timeout on a blocking WMI call
does not cancel that thread: keep the serialized queue poisoned until it
finishes or isolate the adapter in a bounded-lifecycle helper. See
[COM initialization](https://learn.microsoft.com/en-us/windows/win32/learnwin32/initializing-the-com-library).

### Power and telemetry

`PowerGetActiveScheme`/`PowerSetActiveScheme` operate on the current user;
Windows 11 user-configured power-mode APIs are also user-scoped. A
`LocalService` call must not be represented as machine-global or as a user's
choice without a measured architecture. Compare calls under LocalService, a
normal interactive user, and an explicit session component, including
readback, restore, session switch, and competing system signals. Linux
PPD/governor/EPP/RAPL/PL1/PL2 fields have no automatic Windows mapping.

Battery/AC/thermal facts may use documented Windows APIs only after comparing
them with independent tools and OEM UI across AC, battery, charging, and
thermal states. Battery protection, Honor performance modes, fan control, GPU
control, and transition hooks remain unavailable until their own native
boundary, safety, readback, and recovery evidence exists.

## State, paths, logs, and UI

Resolve known folders through Windows APIs and reject reparse-point/junction
attacks before privileged replacement. The planned locations are:

| Data | Location | Policy |
|---|---|---|
| Immutable binaries/resources | `%ProgramFiles%\Honor Control\` | Installer/Administrators write; runtime read/execute |
| Machine desired state/backup | `%ProgramData%\Honor Control\` | Service SID/SYSTEM write; users access only through IPC |
| Service logs | `%ProgramData%\Honor Control\Logs\` | Bounded structured files plus Event Log |
| Crash dumps | Restricted ProgramData subdirectory | Opt-in, bounded retention |
| GUI/tray preferences | HKCU or `%LocalAppData%` | Owning user only |
| Temporary state writes | Create-new file below state root | Same volume, restrictive ACL, atomic replace |

Test paths with spaces, non-ASCII names, case collisions, long paths, reserved
Windows names, trailing dots/spaces, and CRLF files. Use Unicode native APIs
and known-folder resolution, not concatenated user-controlled environment
variables. Privileged code must not inherit caller `PATH`, `PYTHONPATH`, Qt
plugin paths, working directory, or temporary directories. Launch fixed
executables with argument arrays and explicit environment; do not translate
Bash command strings into `cmd.exe` or PowerShell. Refuse UNC/network state
roots for the initial local-only service. See
[Windows path rules](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file).

Keep the logical TOML schema shared, but add explicit version-to-version
migrations and fixtures. A corrupt, future, or invalid schema enters safe
read-only/recovery mode; it never silently resets into writes. Persist desired
policy and evidence-safe metadata, not observed truth. Event Log records
lifecycle, discovery summaries, authorization denials, safety transitions, and
crashes; rolling files carry bounded detail. Redact tokens, nonces, raw pipe
payloads, environment variables, user documents, secrets, and arbitrary
device paths.

The GUI, tray, and CLI consume only `ControlClient`. Their controls are
capability-driven and distinguish unavailable, experimental, conflict, stale,
desired, accepted, observed, and unknown states. Bundle Qt resources and use
absolute installed paths. Use a per-user/session mutex for the tray; do not
use a global service mutex. Windows startup, Explorer restart, DPI, dark mode,
multi-monitor, and lock/logoff behavior require explicit tests. JSON CLI mode
never opens an unexpected UAC prompt.

## Dependencies, build, install, and signing

The current Bash scripts and `make-release-bundle.sh` are Linux-only. They use
systemd, GNU utilities, `/opt`, `/usr`, `/etc`, and a Linux wheelhouse. They
must not be invoked on Windows or described as a Windows installer. Add a
separate Windows build directory/workflow rather than teaching the Linux
installer to branch across two operating systems.

After import separation proves the dependency set, pin and test a Windows
runtime matrix. Candidate dependencies are PySide6 and pywin32; use `ctypes`
only for reviewed missing declarations. Do not add a Windows dependency merely
because it is convenient before proving that it exposes the required security,
HID, SCM, and Event Log surfaces. Build and freeze with a pinned Windows
toolchain after a clean-VM proof. Match Python, Qt, native libraries, and the
frozen executable architecture (initially x64); ARM64 needs a separate matrix.
For ctypes bindings specify argument/return types, pointer-sized handles,
calling convention, structure layout, and ownership; test size/offset fixtures
against the Windows SDK. Windows builds run on Windows, without Unix compiler
flags, and must prove the VC runtime and Qt platform plugins are included. A PyInstaller onedir layout plus WiX/MSI is a
candidate because it can bundle Python/Qt and provide service installation,
repair, major upgrade, rollback, and uninstall metadata; this is a build
decision gate, not an implementation already present.

The Windows installer must:

1. perform no hardware writes;
2. install immutable signed files under Program Files;
3. create and ACL ProgramData without following reparse points;
4. register the Event Log source and quoted SCM service path;
5. configure service SID, delayed start, required privileges, recovery, and
   preshutdown only after probe results justify them;
6. start the service and verify read-only health;
7. roll back files/service metadata if health fails;
8. preserve state by default on uninstall and make purge explicit and narrow;
9. publish hashes, SBOM, dependency/license inventory, support rows,
   limitations, and recovery instructions.

Sign every executable, DLL, and MSI with Authenticode and a timestamp in an
isolated release stage. Keep signing keys out of ordinary CI. Audit DLL search
paths and loaded modules in a clean VM; never bundle Honor binaries, drivers,
or reverse-engineering tools into the product.

## Feature scope and blockers

| Feature | Read-only beta | First stable Windows | Blocker |
|---|---|---|---|
| Service, state, snapshots, IPC | Required | Required | SCM/pipe/security/lifecycle proof |
| GUI, tray, CLI | Required | Required | Client factory and capability UI |
| Exact platform/device detection | Required | Required | SMBIOS/SetupAPI fixtures and near-match tests |
| AC/battery telemetry | Validated fields only | Same | Independent accuracy comparison |
| Windows power mode | Read/research | Candidate first control | User scope, readback, restore, competing signals |
| Battery threshold/protection | Unavailable | Deferred | Native boundary and independent readback |
| Linux advanced power fields | Unavailable | Unsupported | No semantic Windows equivalent |
| Fan/manual/curve | Unavailable | Unsupported | Access, sensor, auto restore, model matrix |
| Touchpad HID settings | Probe only | Default-off Preview if all gates pass | Target replay, no-readback UX, coexistence/lifecycle |
| Touchpad support bitmap | Probe after reader coordination | Optional with Preview | One-reader and response ownership |
| Touchpad master WMI | Query research | Deferred separate gate | Provider, permissions, output shape, restore |
| Gesture decoding/actions | Research | Deferred | Reader ownership, active session, fixed action map |
| GPU control | Unavailable | Unsupported | No validated path and Linux is intentionally gated |
| Transition hooks | Unavailable | Deferred | Windows privilege and recovery design |
| Custom kernel driver | Out of scope | Out of scope | New ADR, signing/HVCI/HLK/fuzz/recovery plan |

## Tests and validation gates

### Shared and import-safety tests

Run on Linux and Windows where applicable:

- core validation, models, touchpad byte vectors, config migration fixtures,
  application use cases, snapshots, supervisor, and queue behavior;
- plain-Python codec golden fixtures consumed by D-Bus and pipe transports;
- ControlClient conformance for fake, D-Bus, and Windows pipe clients;
- fake unavailable/unsupported/conflict/partial/stale/timeout/removal/resume
  and generation-mismatch scenarios;
- stable CLI JSON, errors, and exit codes;
- GUI capability rendering and no-fabricated-Windows-fields checks;
- forbidden-import checks proving Windows shared imports do not load Linux
  transport modules.

### Windows service/IPC/package tests

On a clean Windows VM test fragmented/coalesced/oversized/malformed frames,
duplicate keys, depth/count limits, request collisions, slow readers,
backpressure, cancellation, reconnect, sequence gaps, and bounded shutdown.
Inspect the real DACL and prove standard/filtered/elevated/over-the-shoulder
tokens, anonymous/remote/service/batch logons, impersonation failure, and
`RevertToSelf` failure all fail closed. Test nonce replay, expiry, operation
substitution, cross-session redemption, service restart, and generation
changes. Test SCM start/stop/preshutdown/recovery, Event Log registration,
power/device notification registration, ProgramData reparse/ACL/atomic replace
and disk-full behavior, and fake device arrival/removal/resume.

Build and test frozen applications and MSI clean install, repair, major
upgrade, failed-upgrade rollback, downgrade block, uninstall-preserve, and
explicit-purge behavior. Run dependency/license/SBOM/signature/load-path
checks. Ordinary CI jobs never perform writes; the separate manual hardware
gate below requires explicit operator authorization.

### Target hardware matrix

For every feature row record SKU, BIOS/EC, Windows build, HID/driver/Honor
versions, service identity, PC Manager present/absent, AC/battery, cold boot,
service restart/crash, sleep/hibernate/Modern Standby as applicable, Fast
Startup, device/driver restart, two users, concurrent clients,
disconnect/timeout, and restore outcome. Hardware writes require a dedicated
tagged runner, external mouse/power recovery, explicit feature allowlist, and
human approval. The current Linux/Windows analysis host cannot substitute for
this matrix.

## Research gates

Each experiment records exact versions, identity/token, input, output, timing,
errors, pre/post state, restore result, and evidence hash. A failed gate makes
the feature unavailable; it never authorizes a broader API.

| ID | Safe experiment | Decision |
|---|---|---|
| R0-01 | Read SMBIOS, build, driver, service, HID, BIOS/EC, Honor inventory | Initial support rows and near-match fixtures |
| R0-02 | Install a probe SCM service with service SID; read-only discovery under final token | Identity, ACL, and required privilege list |
| R0-03 | Enumerate all HID collections and report caps; test minimal open/share flags | Windows HID implementation |
| R0-04 | Exercise support query with PC Manager/custom reader present and absent | Reader ownership/coexistence |
| R0-05 | One benign reversible HID setting, immediate restore, restart/resume repeats | Touchpad Preview eligibility |
| R0-06 | Query WMI master under LocalService with/without provider | WMI availability and output shape |
| R0-07 | Compare documented power calls under service, user, and session contexts | Scope and first-control decision |
| R0-08 | Compare AC/battery/thermal facts with independent tools and OEM UI | Telemetry fields |
| R0-09 | Freeze service/GUI/client/helper and test clean VM startup/ACL/update | Dependency and packaging freeze |
| R0-10 | PC Manager open/read/write coexistence and oscillation matrix | Conflict policy |

Unsafe work requires a separate proposal: brute-force WMI/IOCTL/HID/EC/ACPI
commands, fan or battery writes without readback/recovery, CPU/GPU register
writes, caller spoofing, Honor binary patching, disabling PC Manager, driver
replacement, or Secure Boot/HVCI changes.

## Roadmap and release definitions

### M0 — evidence and frozen-runtime proof

Run the read-only portions of R0-01 through R0-04 and R0-06 through R0-09,
produce an evidence/support matrix, prove the final service identity and
dependency set, and build a read-only probe. Record R0-05 and the write side
of R0-10 as later target experiments; neither is required for this gate. No
production hardware writes are enabled.

### M1 — platform separation with Linux unchanged

M1 may proceed in parallel with the read-only M0 evidence work. Add neutral
ports and codec fixtures, extract Linux composition, inject the client factory
and platform services, move Linux key/action tables, add explicit config
migrations, and add Windows forbidden-import tests.

Gate: all current Linux tests, lint, compile, D-Bus fake smoke, and package
build remain green; shared modules import on Windows without Linux runtime
dependencies.

### M2 — secure Windows service and fake hardware

Implement strict pipe framing/limits, ACL/token authorization, reconnect and
resync, nonce-bound approval if needed, SCM lifecycle, paths/ACLs, Event Log,
notifications, and deterministic fake hardware.

Gate: M1 shared ports are import-safe and a clean Windows VM passes service,
security, IPC, config, lifecycle, timeout, removal, and fake-client tests; no
physical hardware evidence or hardware adapter is required.

### M3 — read-only Windows beta

Implement SMBIOS/SetupAPI/CM/HID inventory, capability reasons, validated
telemetry, redacted diagnostics, capability-driven GUI/tray/CLI, signed frozen
build, and MSI lifecycle.

Gate: unsupported and near-match hardware performs no writes; independent
facts match; clean VM install/repair/upgrade/uninstall and Linux regression
gates pass. This gate depends on read-only evidence, not on R0-05 or the write
side of R0-10.

### M4 — one reversible control and touchpad Preview

Implement whichever documented control passed the R0-07 scope comparison in
the correct user or machine component only after its own reversible write and
restore evidence passes. If R0-05 and the write side of R0-10 pass, add
exact-gated HID settings with accepted/unknown results, generation handling,
reader coordination, and default-off Preview UX. Keep master WMI query/write
as a separate gate.

Gate: target hardware completes cold boot, restart/crash, sleep/resume,
Fast-Startup, driver restart, two-user, concurrent-client, PC Manager,
disconnect/timeout, and restore tests. A WMI query that cannot establish a
validated before-state cannot claim restore: record accepted/unknown or
restore-unavailable and keep that feature deferred. Otherwise remain
read-only beta.

### M5/M6 — release hardening and separately gated features

Complete DPI/theme/session/Explorer behavior, MSI repair/upgrade/downgrade,
signing/AV/load-path/privacy work, then consider OEM gesture integration,
custom session actions, master switch, battery protection, Honor performance
modes, thermal reads, and fan research one feature at a time. GPU control,
arbitrary native calls, old EC IOCTLs, unallowlisted hardware, Honor binary
bundling, and a custom kernel driver remain unsupported without new proposals.

## Highest-priority next steps

1. Preserve sanitized reverse-engineering provenance and protocol fixtures in
   tracked documentation; keep ignored captures and OEM binaries out of build
   inputs.
2. Complete M1 import separation and the Windows forbidden-import test before
   creating any Windows hardware code.
3. Build the LocalService/SCM/pipe probe and measure real ACL, token, HID, WMI,
   and notification behavior on the target Windows laptop.
4. Establish a Windows CI lane for shared tests, import checks, frozen build,
   and clean-VM MSI lifecycle; reserve hardware writes for a manual tagged
   runner.
5. Run R0-03/R0-05 only with external recovery input and preserve exact
   accepted/unknown outcomes; decide whether touchpad Preview or a documented
   per-user power control is the first write feature.

## Authoritative platform references

The implementation must verify API details against current Microsoft
documentation during M0. The following primary references support the current
plan:

- [PowerSetActiveScheme](https://learn.microsoft.com/en-us/windows/win32/api/powersetting/nf-powersetting-powersetactivescheme)
  explicitly sets the active scheme for the current user.
- [Named pipe security and access rights](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)
  documents explicit security descriptors and the
  `FILE_GENERIC_WRITE`/`FILE_CREATE_PIPE_INSTANCE` alias.
- [CreateNamedPipe](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createnamedpipew)
  documents `FILE_FLAG_FIRST_PIPE_INSTANCE` and
  `PIPE_REJECT_REMOTE_CLIENTS`.
- [LocalService account](https://learn.microsoft.com/en-us/windows/win32/services/localservice-account)
  and [service isolation](https://learn.microsoft.com/en-us/windows/win32/services/service-changes-for-windows-vista)
  define the least-privilege service baseline and service SID considerations.
- [Sending HID reports from user mode](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/sending-hid-reports)
  and [obtaining HID reports](https://learn.microsoft.com/en-us/windows-hardware/drivers/hid/obtaining-hid-reports)
  are the platform references for the future fixed HID adapter.
- [Windows 10 lifecycle](https://learn.microsoft.com/en-us/lifecycle/announcements/windows-10-end-of-support)
  records the 2025-10-14 end of support for general Windows 10 editions. The
  initial support target should therefore be explicitly tested Windows 11
  x64 builds, with any LTSC/ESU scope treated as a separate support row.
