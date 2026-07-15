# Safety

## Fan fail-safe

- On startup, the service always requests stock auto mode first.
- Curve mode is entered only after sensor and EC probes pass.
- After any temperature read failure, EC write failure, controller
  exception, manual TTL expiry, or service shutdown, the controller
  attempts stock auto through a bounded safety cleanup.
- After N consecutive sensor/write failures, the controller enters
  `failed_safe` mode and stops custom writes.
- Manual override has a required TTL (default 5 minutes, max 15) and is
  never persisted across restart.
- At/above 95°C, the curve must target 100% speed.

## GPU mitigation

- Production capability is intentionally non-writable because honor-tools
  does not implement an exact, verified restore of prior IRQ affinity and
  C-state values.
- The D-Bus surface remains for compatibility but returns unavailable; the
  service does not apply an irreversible partial mitigation.

## Authorization tiers

- **Active local user** (no password): battery thresholds/mode, power
  profile, gesture mappings, debug bundle export.
- **Admin authentication** (password required): fan mode/curve/manual,
  power-profile definitions/automatic hooks, touchpad firmware writes/support
  queries, GPU mitigation, config reload, restricted logs.
- Polkit failure/missing caller **fails closed**. No "active local user"
  fallback for admin-tier actions.
- Internal controller calls use explicit internal application methods
  and never forge/omit a sender.

## Automatic power scripts

- Hooks run only after the selected power profile applies successfully and at
  most once per AC/battery transition or policy change.
- Commands are parsed into argv and executed directly; shell operators,
  expansion, pipelines, and redirection are never interpreted.
- The executable path must be absolute. The executable and every parent
  directory must be root-owned and not group/world-writable.
- Execution uses a minimal environment, `/` as its working directory, a
  15-second timeout, and a separate process group. Hook stdout/stderr is
  discarded; only completion/exit status enters the public snapshot.
- Hooks inherit the service's systemd filesystem, capability, home, network,
  and privilege restrictions; they are not an escape from the service sandbox.
- Configuring or enabling hooks requires administrator authorization.

## Config locations

- `/var/lib/honor-control/state.toml` — service-owned desired state.
  Atomic, versioned, validated. Root never writes a user's home.
- `QSettings` — window geometry and close-to-tray preference only.

## Debug redaction

- Debug bundles are bounded, schema-versioned JSON.
- Usernames, home paths, environment variables, and secrets are
  redacted.
- The bundle is returned as a structured `a{sv}` dictionary to the unprivileged
  caller, which serializes and writes the destination file. Root never creates
  a file in `/tmp` (no `PrivateTmp` namespace issue).

## Recovery

- On initial process load, a corrupt primary may recover the one validated
  backup while service health remains degraded.
- During a running reload, a corrupt primary leaves the newer in-memory
  last-known-good state active; it never replaces it with the older backup.
- The service retains last-known-good data on refresh failure and marks
  the domain stale.
- A historical recovered error clears from active health.
