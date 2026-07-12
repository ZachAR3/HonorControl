# Honor Control

D-Bus service and Qt6 GUI for managing Honor MagicBook laptops on Linux.

## Status

**Alpha.** This software is under active development. Features are
verified against fake hardware in CI; real hardware testing is an
explicit, manual pre-release gate.

**Compatibility:** hardware writes are enabled only for the verified Honor
MagicBook Art 14 (`MRA-XXX`) with a supported Intel Core Ultra/Meteor Lake
CPU. Other Intel and AMD models are not allowlisted and receive no power,
fan, or GPU writes. Power profiles use PPD plus standard Intel sysfs controls;
the service does not write raw CPU MSRs or disable host power-management
services.

- **Battery charge control:** verified (sysfs charge thresholds)
- **Power profiles:** enabled only on the verified MRA-XXX platform (requires
  working PPD, Intel RAPL/intel_pstate sysfs controls, and exact readback)
- **Fan control:** enabled only on verified MRA-XXX hardware with a valid CPU
  temperature sensor (EC writes via `acpi_call`)
- **Touchpad gesture actions:** input decoder and Linux uinput dispatcher
  implemented; firmware setting writes remain unsupported
- **GPU mitigation:** disabled until its original IRQ/C-state values can be
  captured and restored reliably
- **Firmware setting writes:** unsupported (the Windows
  service/driver-to-hardware protocol and WMI buffer effects are unknown)

See [gesture Linux remaining work](docs/gesture-linux-remaining-work.md) for
the confirmed protocol, Windows capture procedure, and implementation gate.

## Architecture

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
  Per-user GUI settings: QSettings
```

### Safety invariants

1. **One hardware owner:** only the root system service may instantiate
   `HonorToolsAdapter`. Frontends depend only on the D-Bus client
   protocol.
2. **Unknown hardware cannot write:** a positive platform match
   (recognized DMI vendor/product) is required before any EC/fan/GPU
   write. No fallback to a default platform.
3. **Polkit fails closed:** missing sender, credentials, or polkit
   unavailability denies the call. No "active local user" fallback.
4. **Serialized mutations:** all hardware mutations go through one daemon
   worker and a global async lock. A timed-out, non-cancellable hardware call
   poisons the queue until it actually returns.
5. **Desired/applied/observed are separate:** persisted user intent,
   last successfully applied state, and live hardware observation never
   overwrite one another.
6. **Structured results:** every mutation returns an `OperationResult`
   with `changed`, `persisted`, and `applied` booleans. Config
   persistence is never reported as hardware success.
7. **Qt main thread is I/O-free:** all D-Bus, filesystem, subprocess,
   and hardware I/O happens on a dedicated worker thread.

## Installation

```bash
# Clone and install (creates an isolated versioned venv under /opt/honor-control,
# symlinks entry points to /usr/bin, installs systemd/D-Bus/polkit files)
git clone git@github.com:ZachAR3/HonorControl.git
cd HonorControl
sudo bash scripts/install-local.sh

# Enable and start the service
sudo systemctl enable --now honor-control.service
```

The installer removes the stock `honor-tools` power-supply udev hook because
it directly rewrites CPU EPP after calling `powerprofilesctl`, which conflicts
with KDE's power-profiles-daemon slider. Honor Control owns AC/battery
switching instead; PPD remains enabled for KDE.

To uninstall:

```bash
sudo bash scripts/uninstall-local.sh            # keep state
sudo bash scripts/uninstall-local.sh --purge     # remove state too
```

For development, use the fake session-bus service without installing it as root:

```bash
scripts/dev-run-service.sh
scripts/dev-run-gui.sh --bus session
```

## Usage

### CLI

```bash
honorctl status                    # overall status
honorctl battery thresholds 80 75  # set charge thresholds
honorctl battery mode home         # apply charge mode preset
honorctl power profile balanced     # apply power profile
honorctl power save compile --pl1 30 --pl2 45 \
  --epp balance_performance --max-perf 90
honorctl power auto-switch on --on-ac compile --on-battery silent
honorctl fan stock                 # restore stock auto mode
honorctl fan curve "40000:0,95000:100"  # set fan curve
honorctl fan manual 50 --ttl 300   # manual fan speed with TTL
honorctl gestures batch on         # enable all gestures
honorctl gestures daemon on        # start HID-to-uinput dispatch
honorctl gpu enable                # reports unavailable until restore is safe
honorctl diagnostics checks        # run diagnostic checks
honorctl diagnostics export -o bundle.json  # export debug bundle
honorctl reload                    # reload config
```

The GUI power page includes the three built-in profiles (Silent, Balanced,
Performance), editable PL1/PL2 wattage, governor, EPP, PPD mode, turbo and
maximum-performance settings, additional custom profiles, and explicit
AC/battery profile and script-hook selection. The fan page provides a
click/drag graphical curve editor.

Install transition hooks in a root-owned location before selecting them, for
example:

```bash
sudo install -D -o root -g root -m 0755 my-ac-hook \
  /usr/local/libexec/honor-control-ac-hook
```

### GUI

```bash
honor-control-gui        # launch the GUI
honor-control-tray       # launch the system tray
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run lint
ruff check honor_control tests

# Run the service on the session bus (FakeHardware, no root)
python -m honor_control.backend.service --session-bus

# Run the GUI against the session bus
honor-control-gui --bus session
```

## Configuration

- `/var/lib/honor-control/state.toml` — service-owned desired state
  (battery thresholds, power profile, fan curves, gesture mappings,
  GPU mitigation). Atomic, versioned, validated.
- `QSettings` — window geometry, refresh preference only.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Acknowledgements

This project builds on research and prior work from the Honor MagicBook
Linux community. In particular:

- **[honor-magicbook-art-touchpad-gestures](https://github.com/MadhiasM/honor-magicbook-art-touchpad-gestures)**
  by [MadhiasM](https://github.com/MadhiasM) (GPL-3.0) — the touchpad
  HID report `0x0e` gesture decoder and uinput dispatch approach that
  informed this project's gesture runtime.
- **[art14-fan-daemon](https://github.com/mark-herbert42/art14-fan-daemon)**
  by [mark-herbert42](https://github.com/mark-herbert42) — the documented
  EC ACPI call sequences (`_SB.PC00.LPCB.H_EC.WTER ...`) used for fan
  control on the Honor MagicBook Art 14.

## Disclaimer

This software is **AI-generated**. While I have personally tested all of
it on my own hardware, it is provided "AS IS", without warranty of any
kind, express or implied. I am **not responsible** for any issues, data
loss, or hardware damage that may occur from using this product. You use
it entirely at your own risk.
