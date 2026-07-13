# First Linux execution runbook

The protocol is recovered; this runbook performs the physical validation that
cannot run while the laptop is booted into Windows.

For a clean-room implementation in another language, read `PORTING_SPEC.md`
together with `../protocol.json`. It defines discovery, ordering, failure
semantics, the input-event boundary, the master WMI ABI, and completion tests.

## 1. Read-only preflight

From the `honor-control` repository root, the touchpad CLI can run directly
with Python 3.11+ before installing the full application:

```bash
cat /sys/class/dmi/id/sys_vendor
cat /sys/class/dmi/id/product_name
cat /sys/class/dmi/id/bios_version
uname -a
sudo python3 -m honor_control.cli.touchpadctl --json probe
python3 -m honor_control.cli.touchpadctl list
python3 -m honor_control.cli.touchpadctl encode edge_brightness on
```

Stop if DMI is not exactly `HONOR` / `MRA-XXX`, descriptor verification is
false, or input/output report sizes are not both 9.

## 2. Inspect the full profile without writing

```bash
python3 -m honor_control.cli.touchpadctl apply --dry-run \
  packaging/touchpad/honor-touchpad.example.toml
```

The example's HID values mirror those recorded at the end of the Windows
analysis. Review every value. The profile is strict TOML; unknown tables, keys,
settings, types, and values are rejected.

## 3. Reversible HID test

The captured original `edge_brightness` value is on:

```bash
sudo python3 -m honor_control.cli.touchpadctl set edge_brightness off
# Physically test the edge gesture.
sudo python3 -m honor_control.cli.touchpadctl set edge_brightness on
```

Record both JSON outputs and restore immediately. A successful write means the
kernel accepted all nine bytes; the OEM protocol provides no value readback, so
physical behavior is the observation.

## 4. Capability query

Only one process should read the asynchronous response:

```bash
sudo systemctl stop honor-control.service 2>/dev/null || true
sudo python3 -m honor_control.cli.touchpadctl --timeout 5 support
```

Restart the service afterward if it had been enabled.

## 5. Optional master switch

Connect an external mouse. Follow `wmi/README.md` to build and load the module,
then query before writing:

```bash
sudo python3 -m honor_control.cli.touchpadctl master
sudo python3 -m honor_control.cli.touchpadctl master off
sudo python3 -m honor_control.cli.touchpadctl master on
```

If module load/query returns `EPROTO` or `EMSGSIZE`, unload it and preserve the
kernel log. Do not attempt alternate WMI IDs or raw buffers.

## 6. Persistent configuration

After the reversible tests pass, install just the dependency-free touchpad
controller (Python 3.11+ standard library only) and create the profile:

```bash
sudo bash scripts/install-touchpad-only.sh
sudo cp /usr/share/doc/honor-control/honor-touchpad.example.toml \
  /etc/honor-touchpad.toml
sudoedit /etc/honor-touchpad.toml
honor-touchpadctl apply --dry-run /etc/honor-touchpad.toml
sudo honor-touchpadctl apply /etc/honor-touchpad.toml
sudo systemctl enable --now honor-touchpad-restore.service
```

Use `scripts/install-local.sh` instead only when installing the complete Honor
Control D-Bus service and GUI as well. Both installers deploy the same touchpad
controller and restore unit.

Uncomment the `[master]` table only if the optional WMI module is installed and
its read-only query passed.

## 7. Resume and cold-boot validation

Suspend once, resume, and test the configured gestures. Then reboot once and
repeat. Preserve:

```bash
systemctl status honor-touchpad-restore.service --no-pager
journalctl -b -u honor-touchpad-restore.service --no-pager
dmesg | grep -i -E 'honor|wmi|acpi|hidraw'
sudo honor-touchpadctl --json probe
```

Pass criteria:

- exact endpoint remains verified;
- restore service exits successfully;
- the original/reapplied settings behave as expected;
- master query matches the requested state when used;
- no short write, timeout, disconnect, ACPI error, or partial batch is logged.

## Immediate rollback

Apply the preserved original example values and disable automatic restoration:

```bash
sudo honor-touchpadctl apply \
  /usr/share/doc/honor-control/honor-touchpad.example.toml
sudo systemctl disable --now honor-touchpad-restore.service
```

If the WMI module itself misbehaves:

```bash
sudo honor-touchpadctl master on
sudo modprobe -r honor_touchpad_wmi
```
