# Honor Control

Honor Control is a Linux system service and Qt 6 desktop app for managing
supported Honor MagicBook laptops.

The project is currently alpha software. Test hardware changes carefully and
keep a recovery path available.

Windows support is planning only. A few dependency-light core and protocol
modules are kept importable on Windows for porting work, but the service, GUI,
Linux hardware paths, and release installers remain Linux-only. See the
[Windows support plan](WINDOWS_SUPPORT_PLAN.md) for the gates and prerequisites.

## Supported hardware

Hardware writes require an exact platform match:

- DMI vendor/product: `HONOR` / `MRA-XXX` (MagicBook Art 14)
- Fan control CPU: Intel Core Ultra 5 125H
- Kernel modules: `acpi_call` for fan control and `uinput` for gestures

Other models remain read-only. GPU mitigation and automatic touchpad firmware
restore are disabled by default.

## Features

- Battery charge thresholds and presets
- Power profiles and AC/battery switching
- Stock, manual, and curve-based fan control
- Touchpad gesture mapping and firmware settings
- GUI, integrated tray, tray-only process, and CLI
- D-Bus API with polkit authorization
- Atomic configuration and structured diagnostics

## Install

`honor-tools` 0.1.0 must be available at `../honor-tools`, or supplied as a
wheel in an offline release bundle.

```bash
git clone git@github.com:ZachAR3/HonorControl.git
cd HonorControl
sudo bash scripts/install-local.sh
sudo systemctl enable --now honor-control.service
```

Run the touchpad hardware validation before any direct root write. Automatic
profile restore remains disabled unless the validated installation is marked
explicitly:

```bash
sudo bash scripts/install-local.sh --enable-touchpad-firmware
```

Build a complete offline bundle with:

```bash
bash scripts/make-release-bundle.sh
```

The bundle must be signed before distribution. Its installer verifies the
bundle and exact wheelhouse contents before changing the system.

Rollback and removal:

```bash
sudo bash scripts/rollback.sh
sudo bash scripts/uninstall-local.sh
sudo bash scripts/uninstall-local.sh --purge
```

## Use

```bash
honor-control-gui
honor-control-tray
honorctl status
honorctl battery thresholds 80 75
honorctl power profile balanced
honorctl fan stock
honorctl diagnostics checks
```

The standalone `honor-touchpadctl` tool accesses hidraw directly. Prefer the
GUI or `honorctl` for normal operation.

## Development

```bash
python -m pip install -e ".[gui,dev]"
pytest
ruff check honor_control tests
scripts/dev-run-service.sh
scripts/dev-run-gui.sh --bus session
```

The development service uses fake hardware on the session bus.

## Documentation

- [Architecture](docs/architecture.md)
- [Safety model](docs/safety.md)
- [Hardware support](docs/hardware-support.md)
- [D-Bus API](docs/dbus-api.md)
- [Development](docs/development.md)
- [Touchpad validation](docs/hardware-validation/touchpad-linux.md)
- [Windows support plan](WINDOWS_SUPPORT_PLAN.md)

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Acknowledgements

Touchpad gesture work builds on
[honor-magicbook-art-touchpad-gestures](https://github.com/MadhiasM/honor-magicbook-art-touchpad-gestures).
Fan-control research builds on
[art14-fan-daemon](https://github.com/mark-herbert42/art14-fan-daemon).
