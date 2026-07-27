# Honor touchpad WMI bridge

This optional, deliberately narrow module implements only the global
touchpad-enable method used by the updated Windows plugin. All sensitivity,
haptic, pressure, edge, drag, mouse-like, screenshot, and recording settings
use hidraw and do not need this module.

The driver binds only when both conditions match:

- WMI GUID `ABBC0F5B-8EA1-11D1-A000-C90629100000` exists;
- DMI is exactly vendor `HONOR`, product `MRA-XXX`.

It exposes one boolean `touchpad_enabled` attribute. There is no generic WMI,
ACPI, EC, or raw-command interface.

## One-kernel build

Install the distribution's compiler and matching kernel headers, then:

```bash
make
modinfo ./honor_touchpad_wmi.ko
sudo make install
sudo modprobe honor_touchpad_wmi
dmesg | tail -n 30
find /sys/bus/wmi/devices -path '*ABBC0F5B*/touchpad_enabled' -print
```

## DKMS installation

If DKMS is installed, the included `dkms.conf` can rebuild the module after a
kernel update:

```bash
version=0.1.0
source=/usr/src/honor-touchpad-wmi-$version
sudo install -d -m 0755 "$source"
sudo install -m 0644 honor_touchpad_wmi.c Makefile dkms.conf "$source/"
sudo dkms add -m honor-touchpad-wmi -v "$version"
sudo dkms build -m honor-touchpad-wmi -v "$version"
sudo dkms install -m honor-touchpad-wmi -v "$version"
printf '%s\n' honor_touchpad_wmi | \
  sudo tee /etc/modules-load.d/honor-touchpad-wmi.conf
sudo modprobe honor_touchpad_wmi
```

## First reversible test

Have an external mouse connected. Read before writing:

```bash
sudo honor-touchpadctl master
sudo honor-touchpadctl master off
sudo honor-touchpadctl master on
```

The CLI reads the query attribute after a change and fails if it does not match.
The underlying commands are fixed-width `u64` values in a zero-filled 64-byte
input:

| Operation | Command |
|---|---:|
| Query | `0x00000f02` |
| Disable | `0x00001002` |
| Enable | `0x00011002` |

The query accepts only a zero firmware status in output byte 0 and reads state
from output byte 1.

If the module returns `EPROTO` or `EMSGSIZE`, unload it and capture the raw ACPI
return object. Do not weaken the accepted output layouts or add a generic
command attribute. The static Windows contract is complete, but the raw return
object layout still requires the first on-hardware Linux call.

```bash
sudo modprobe -r honor_touchpad_wmi
```
