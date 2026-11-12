# Repository evaluation — 2026-09-07

The audit covered the tracked Python service, hardware adapters, firmware
transport, clients, GUI/tray/CLI, tests, installers, release configuration,
CI, documentation, and experimental WMI module. Local ignored reverse-
engineering material was consulted for provenance and platform constraints;
proprietary binaries and historical exploratory scripts were not executed.
Existing uncommitted removal of the touchpad qualification flag was preserved.
No installed service, physical hardware, or system configuration was changed.

Three Luna tracks investigated backend safety, frontend/client behavior, and
Windows/packaging. Their edits were reviewed and tested by the primary agent.
The agents hit the account usage limit before returning final reports, so a
completed independent Luna sign-off is **not** claimed.

## Architecture and findings

The application uses unprivileged Qt/CLI clients, a versioned sdbus contract,
polkit authorization, an async application service, atomic desired-state
storage, immutable snapshots, supervised controllers, and a single blocking
hardware worker. Real hardware paths remain Linux-specific. The privileged
standalone touchpad tool shares a process lock with the service.

| Finding | Fix and evidence |
|---|---|
| Eleven baseline tests still expected the removed qualification flag | Reconciled tests with capability-based writes; retained unavailable-device, validation, locking, and partial-write assertions |
| Firmware write retries could exceed the deadline on repeated interruption; NaN/infinity bypassed timeout bounds | Finite timeout validation and deadline checks on every retry; deterministic regression tests |
| Descriptor verification accepted unbalanced nesting and undercounted report IDs shared across collections | Reject malformed nesting and count complete reports across collections; rejection tests |
| Re-probing a path did not prove it still matched the opened handle | Compare opened node identity with the verified path and close on verification failure; simulated replacement test |
| Invalid touchpad value types escaped as AttributeError | Return a validation ValueError before I/O |
| Snapshot callbacks could deadlock by updating the store, or skip a subscriber while unsubscribing | Notify outside the state lock and iterate a stable subscriber list; bounded re-entry tests |
| Battery transitions could violate the intermediate hysteresis gap | Rediscover the supply, read both thresholds, select a safe write order, and fail closed without readable state; simulated driver checks |
| Power rollback restored governors before EPP | Restore EPP first while the writable governor is active; rejection-model regression test |
| Explicit stock-fan restoration lacked the common timeout recovery reservation | Route through the existing recovery mechanism; delayed-call/recovery test |
| Gesture watcher registration failure leaked descriptors | Close the input and uinput session on registration failure |
| Cancelled diagnostics could leave a helper running | Share subprocess-group termination/reaping with transition hooks; cancellation test |
| GUI reconnect and pending completion could overwrite page-specific disabled states | Gate the content container separately, preserve pending state, clean removed controls, and test disconnect/reconnect ordering |
| Wire decoding inconsistently coerced malformed values | Validate integer dictionaries, string arrays, and result details; await general awaitables from subscribers |
| Neutral CLI/profile code pulled in Linux transports | Lazy D-Bus imports, deferred flock import, shared profile loader, and Linux-only sdbus dependency marker |
| Extracted profile loader needed standalone installation coverage | Include it in both installers and rollback allowlist; installer assertions |
| Installer text described a removed runtime gate | Clarify explicit writes versus opt-in automatic restore; retain legacy option/marker names for compatibility |

## Validation

- Baseline: **400 passed, 11 failed**.
- Final full suite: **449 passed**, including private D-Bus roundtrips and
  offscreen Qt tests, with warnings treated as errors.
- Branch-aware coverage: **74.44%**, above the configured **72%** floor.
- Ruff, byte compilation, `git diff --check`, Bash syntax, ShellCheck,
  polkit/D-Bus XML, desktop entries, and systemd unit verification passed.
- Wheel and normalized sdist built twice in clean temporary source trees;
  each corresponding artifact was byte-identical. The isolated build uses
  pinned build 1.5.1, setuptools 83.0.0, and wheel 0.47.0. No existing build directory was deleted.
- Clean core wheel install and `pip check` passed; installed CLI help and
  touchpad report encoding passed outside the source tree.
- Neutral core/profile/fake-client/CLI imports passed with `sdbus` and `fcntl`
  deliberately unavailable. This is import isolation, not Windows execution.
- `pip-audit --strict -r requirements-audit.txt`: no known vulnerabilities.
- Bandit: no medium/high findings; 18 low-severity findings remain.
- Experimental WMI module compiled out of tree against kernel
  `7.2.2-1-cachyos` with `LLVM=1`. Default GCC failed because those kernel
  headers require LLVM flags; using the matching toolchain succeeded.
- CI-pinned Python tool versions were checked against their published PyPI
  releases. The full CI Python-version matrix was not run locally.

## Limits and remaining risks

- No hardware replay, fan/thermal safety qualification, battery readback on
  the laptop, suspend/resume trial, WMI module load, or privileged installer
  execution was performed. Passing offline tests is not hardware evidence.
- The full suite emits an existing Qt/Python garbage-collection warning at
  interpreter shutdown despite successful test exit. It also occurred before
  this work; no failing assertion or new warning was attributed to the changes.
- The development venv inherits system packages: its `pip check` reports an
  unrelated `glances`/`pyinstrument` mismatch. The isolated core installation
  has no broken requirements.
- Gitleaks full-history scanning was not run locally (binary unavailable);
  the existing CI gate remains required. There is no configured type checker.
- The unpublished local `honor-tools` wheel and hardware-specific behavior
  remain separate release/qualification inputs. Clean GUI packaging on each
  supported target still needs the CI/runtime matrix.
- Snapshot state sequences are monotonic, but asynchronous/reentrant callback
  completion order is not guaranteed; consumers must use sequence checks.
- Windows has no implemented service/IPC/hardware adapter or installer. Do not
  advertise Windows product support based on the dependency/import changes.

## Windows phase

[WINDOWS_SUPPORT_PLAN.md](../WINDOWS_SUPPORT_PLAN.md) consolidates the Windows
roadmap alongside the existing Linux touchpad porting specification. It
separates committed protocol evidence from ignored local captures, read-only
platform work from hardware-write gates, HID settings from OEM WMI, and
machine ownership from user-scoped power/session actions. It covers service
identity, pipe ACL/token authorization, native cancellation/COM/ABI rules,
paths/environment, packaging/signing, recovery, UI, CI, and target matrices.
Primary API references are linked in the plan.

The first priorities are sanitized tracked evidence, neutral ports/client
selection, Windows CI and a clean VM, then a least-privilege SCM/IPC read-only
probe. Select a first write feature only after exact-device permission,
coexistence, lifecycle, and restore experiments pass. Fan, battery protection,
GPU, custom gesture actions, and master WMI writes retain separate blockers.
