# Changelog

## [Unreleased] - 0.2.0

### Fixed

- Power-profile application now coordinates with PPD without masking host
  services or using raw MSR access, verifies every sysfs field before reporting
  success, and reconciles complete observed definitions instead of PPD names.
- Profile persistence and active-profile edits now report hardware and durable
  state independently when only one stage succeeds.
- The hardware queue remains responsive on runtimes where cross-thread event
  loop wakeups are delayed, and automatic profile retries use bounded backoff.
- `honor-tools` is pinned and preflighted against the supported 0.1.0 API.
- ACPI fan responses are parsed as an integer result with NUL padding stripped
  (e.g. `0x0\x00`), so EC fan commands no longer misreport failure.
- Safety-review findings hardened across the serialized command queue
  (timeout poison/recovery ordering), fan fail-safe stock-auto restoration,
  config-store atomicity, and fail-closed polkit caller capture, with expanded
  regression coverage.
- AC/battery transition hooks now run after the mutation lock is released, so a
  slow hook no longer blocks other mutations or the fan thermal watchdog.
- The client and CLI now distinguish a busy hardware queue (transient,
  retryable) from an internal fault instead of reporting both as internal.
- The GUI debug-bundle save reports a failed write instead of raising an
  unhandled exception in the slot.
- Gesture input type 7 is labeled "screen recording" to match the
  reverse-engineered protocol; its default chord remains a selective screenshot
  (Linux has no universal screen-recording key).

### Changed — overhaul

- **Architecture:** rebuilt as a safe, testable system service with thin
  GUI, tray, and CLI clients. All hardware mutations go through
  authorized system D-Bus and one serialized command queue.
- **Domain layer:** added `core/errors.py`, `core/models.py`, and
  `core/validation.py` with frozen dataclasses, string enums, and strict
  parsers. No silent coercion to `0`, `False`, or `""`.
- **Config store:** replaced active-user config with a versioned system
  state store at `/var/lib/honor-control/state.toml`. Atomic, validated,
  and never writes to a user's home as root.
- **Hardware adapter:** isolated all `honor-tools` imports behind one
  `HonorToolsAdapter` and `HardwarePort` protocol. No private imports, no
  `_patch_acpi_call` monkeypatch. Battery/AC paths are discovered, not
  hard-coded to `BAT0`/`ADP1`.
- **Application service:** async use cases with a serialized daemon-thread
  hardware queue, snapshot store with monotonic sequence,
  and runtime supervisor with idempotent controller lifecycle.
- **D-Bus contract:** versioned API (`GetApiVersion`, `GetSchemaVersion`),
  `GetSnapshot`, `StateChanged` signal. All mutations return structured
  `OperationResult`. No raw WMI commands or setters that only return `b`.
- **Client:** typed async sdbus client with version handshake, error
  classification, and signal subscription. `dbus-python` removed.
- **Polkit:** fail-closed authorizer. No "active local user" fallback for
  admin-tier actions. Missing sender denies the call.
- **CLI:** thin D-Bus client with deterministic exit codes. No direct
  backend fallback. `--bus`, `--timeout`, `--json` flags.
- **GUI:** controller worker thread owns all I/O. Pages receive typed
  snapshots and emit user intents — no D-Bus calls from the Qt main
  thread.
- **Tray:** snapshot-driven menu with batch gesture operations.
- **Packaging:** stable `/usr/bin` paths, no venv path patching.
  Hardened systemd unit (`ProtectSystem=strict`, `ProtectHome=true`,
  `PrivateTmp=true`, `StateDirectory`, `RuntimeDirectory`).
- **Gesture runtime:** added service-owned, reconnecting hidraw input and
  uinput dispatch for confirmed report `0x0e`; firmware writes remain disabled.
- **Power profiles:** added editable built-in and custom definitions for
  PL1/PL2, governor, EPP, PPD mode, turbo and maximum performance, plus
  selectable AC/battery policies and bounded root-owned transition hooks.
- **Fan UI:** restored a graphical point editor, preserves unsaved edits across
  refreshes, publishes live curve targets, and reports unavailable RPM
  explicitly instead of rendering blank values.
- **Tests:** comprehensive tests covering domain validation, config store, hardware
  adapter, snapshot/queue/supervisor, application service, client/codec,
  and contract consistency.

### Changed

- Fan support is narrowed to the single capture-verified CPU (Core Ultra 5 125H
  on MRA-XXX); unevidenced CPU markers were removed. Re-adding a CPU requires a
  committed capture proving the same EC fan interface on that SKU. The broader
  platform identities remain available to the independent sysfs/PPD power
  backend.
- `powerprofilesctl get` results are cached briefly (invalidated on every set)
  to reduce subprocess churn from the poll loops.
- Added end-to-end coverage for the D-Bus wire path (a real private-bus
  round trip), the `/proc` caller-starttime parse, the real adapter's
  non-writable GPU gate, and the transition-hook-out-of-lock guarantee.

### Removed

- WMI firmware execution buttons and CLI commands from normal surfaces.
- Dead code: unused per-feature D-Bus object-path/interface constants, the
  vestigial `InternalAuthorizer`, and `FakeHardware` gesture setters that were
  never part of the hardware protocol.
- `firmware_run_wmi_command`, `firmware_get_setting`, `firmware_set_setting`
  from the D-Bus API.
- `RealBackend` direct fallback from `honorctl`.
- `_is_local_active_user()` polkit fallback.
- `_patch_acpi_call` monkeypatch.
- `dbus-python` dependency.
- Hard-coded `BAT0`/`ADP1` paths.
- `CHARGE_MODES` duplication across backend/client.

### Migration

- Old `~/.config/honor-tools/config.toml` files are not read by the root
  service. Re-enter desired values through the CLI/GUI; do not copy legacy
  TOML directly because its schema differs.
- `battery mode custom` is no longer accepted by `SetMode`; use
  `battery thresholds END START` instead.
- CLI exit codes are now deterministic (0/2/3/4/13/69/70).
