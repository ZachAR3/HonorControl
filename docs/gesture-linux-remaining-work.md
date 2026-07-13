# Honor Touchpad Protocol and Linux Validation

## Result

The missing Windows-to-hardware boundary is recovered. Ordinary touchpad
settings do not use the Honor EC driver, `DeviceIoControl`, a HID Feature
report, or the registry. The updated `MagicTouchPadPlugin.dll` opens the
touchpad's vendor HID collection and sends nine-byte **Output** reports with
overlapped `WriteFile`:

```text
0e CC VV 00 00 00 00 00 00
```

`CC` is the setting command and `VV` is the validated value. The global
touchpad enable switch is the one exception: it uses `OemWMIMethod.OemWMIfun`
in `ROOT\WMI` with a fixed 64-byte input.

The Linux implementation consists of:

- `honor_control/core/touchpad.py`: canonical commands and encoders;
- `honor_control/backend/touchpad_firmware.py`: strict descriptor discovery,
  typed hidraw transport, timeout handling, batching, and support query;
- `honor_control/cli/touchpadctl.py`: `probe`, `encode`, `set`, `apply`,
  `support`, and `master` operations;
- `Reverse engineering/finish-trackpad-re/linux/wmi/`: optional narrow kernel
  driver for the master switch;
- `packaging/systemd/honor-touchpad-restore.service`: boot restore plus a
  system-sleep hook for resume;
- `Reverse engineering/finish-trackpad-re/protocol.json`: machine-readable
  protocol contract.

## Proven route

```text
MagicTouchPadSettingUI.exe
  -> MagicTouchPadHelper.dll (one int argument)
  -> 61-byte IPC message (message ID + value)
  -> PCManagerMainService.exe / MagicTouchPadPlugin.dll
  -> typed DLOpermpl setter
  -> HIDClient::WriteToDev
  -> WriteFile(COL05, 9-byte output report)
  -> TOPS0102 touchpad firmware
```

The master switch branches at `DLOpermpl::SetTouchPadEnable` to the OEM WMI
method instead. The plugin constructs a HID-shaped temporary buffer in that
function but does not send it.

## Exact endpoint

The Linux writer accepts the endpoint only when all of these checks pass:

| Property | Required value |
|---|---:|
| DMI vendor / product | `HONOR` / `MRA-XXX` |
| ACPI device | `TOPS0102` |
| HID VID / PID | `35cc` / `0104` |
| Windows-observed release | `0101` (recorded, not a Linux selector) |
| Usage page / usage | `ff00` / `0001` |
| Collection type | Application |
| Report ID | `0e` |
| Input / output length | 9 / 9 bytes |

On Windows this is collection `COL05`. On Linux the collection is discovered
from the hidraw report descriptor; no `/dev/hidrawN` number is hardcoded.

## Public setting calls

All reserved bytes are zero. Boolean values are `0` and `1`.

| Linux name | OEM registry name | Helper ID | Output report(s) |
|---|---|---:|---|
| `sensitivity` | `sensitivity` | `02` | `0e 01 VV 00 00 00 00 00 00` |
| `vibration_intensity` | `shock` | `03` | `0e 02 VV 00 00 00 00 00 00` |
| `press_text` | `SensitivityPressText` | `04` | `0e 03 VV 00 00 00 00 00 00` |
| `press_picture` | `SensitivityPressPicture` | `05` | `0e 04 VV 00 00 00 00 00 00` |
| `three_finger_drag` | `ThreeFingerDrag` | `06` | command `05`, then companion `11` |
| `mouse_like_mode` | `MouseLikeMode` | `07` | `0e 06 VV 00 00 00 00 00 00` |
| `edge_brightness` | `EdgeGestureAdjusBrightness` | `08` | `0e 07 VV 00 00 00 00 00 00` |
| `edge_volume` | `EdgeGestureAdjusVolume` | `09` | `0e 08 VV 00 00 00 00 00 00` |
| `edge_control_center` | `EdgeGestureOpenHiCenter` | `0a` | `0e 09 VV 00 00 00 00 00 00` |
| `edge_close_or_minimize` | `EdgeGestureCloesOrMinWnd` | `0b` | `0e 0a VV 00 00 00 00 00 00` |
| `knuckle_screenshot` | `KnuckleScreenShot` | `0c` | `0e 0b VV 00 00 00 00 00 00` |
| `knuckle_screen_record` | `KnuckleRecordScreen` | `0d` | `0e 0c VV 00 00 00 00 00 00` |

Sensitivity is `low=0`, `high=1`. Vibration intensity is `low=0`,
`medium=1`, `high=2`. Updated managed UI IL proves those label mappings.
Three-finger drag always sends both `05` and `11` with the same value, matching
the OEM's `ThreeFingerDrag` and `TripleFingerLightTouch` persistence keys.

## Lifecycle and capability calls

At startup and resume the OEM service sends:

```text
0e 00 TT TT TT TT TT 00 00
```

The five `TT` bytes are the low five bytes of `time(NULL)`, little-endian.
Linux sends this once per open/profile transaction by default.

The capability query is `0e f0 00 00 00 00 00 00 00`. A matching input report
starts `0e f0`; supported gesture bits begin at byte 2. Stop the gesture daemon
before running `honor-touchpadctl support`, because two hidraw readers can race
for the asynchronous response.

The shutdown/reset report is command `0e`, value `00`. It is internal and is
not exposed by the Linux CLI because its externally visible effect and recovery
contract are not needed for setting control.

## Master switch WMI contract

| Field | Value |
|---|---|
| Namespace | `ROOT\WMI` |
| Class / method | `OemWMIMethod` / `OemWMIfun` |
| Instance | `ACPI\PNP0C14\HWMI_0` |
| GUID | `ABBC0F5B-8EA1-11D1-A000-C90629100000` |
| WMI method ID | `1` |
| Input | 64 bytes: little-endian command `u64`, then 56 zero bytes |
| Output | 256-byte `u8Output` (`u32Resrved` is the other MOF output field) |
| Query | `0x00000f02` |
| Disable / enable | `0x00001002` / `0x00011002` |
| Query result | output byte 0 status; byte 1 enabled |

The optional kernel module exposes only `touchpad_enabled`; there is no raw WMI
command passthrough. The OEM screen lifecycle calls (`0x1502` off and
`0x11502` on) are deliberately not exposed as user settings.

## Linux use

Install the project, then perform read-only checks first:

```bash
sudo honor-touchpadctl probe
honor-touchpadctl list
honor-touchpadctl encode vibration_intensity high
honor-touchpadctl apply --dry-run \
  /usr/share/doc/honor-control/honor-touchpad.example.toml
```

Apply one reversible HID setting:

```bash
sudo honor-touchpadctl set edge_brightness off
sudo honor-touchpadctl set edge_brightness on
```

For a complete persistent profile:

```bash
sudo cp /usr/share/doc/honor-control/honor-touchpad.example.toml \
  /etc/honor-touchpad.toml
sudoedit /etc/honor-touchpad.toml
honor-touchpadctl apply --dry-run /etc/honor-touchpad.toml
sudo honor-touchpadctl apply /etc/honor-touchpad.toml
sudo systemctl enable --now honor-touchpad-restore.service
```

The service waits up to ten seconds for the validated endpoint. Its sleep hook
restarts the oneshot after resume. If `[master]` is present, enabling occurs
before HID writes and disabling occurs last so the endpoint cannot disappear
mid-profile.

## State and failure semantics

The HID protocol has no setting-value readback. `WriteFile` completion proves
only that the kernel accepted the report. The OEM code itself ignores the
`WriteToDev` boolean before updating its registry mirror, so the registry is
not independent firmware confirmation.

Linux therefore reports:

- exact reports requested;
- count of reports successfully written;
- whether the clock handshake completed;
- partial progress if the two-report three-finger operation fails;
- WMI query readback separately for the master switch.

No arbitrary report, command, length, reserved byte, WMI method, EC offset, or
IOCTL is accepted. A profile is completely validated before the HID node is
opened.

## Remaining hardware-only gate

Offline reconstruction and tests are complete. This Windows environment has no
running Linux kernel or WSL installation, so the following cannot honestly be
claimed until the laptop boots Linux:

1. compile/load `honor_touchpad_wmi.ko` against the target kernel;
2. confirm `honor-touchpadctl probe` selects the Linux vendor collection;
3. set one HID boolean off/on and restore its original value;
4. query the support bitmap with the gesture daemon stopped;
5. query the master state, toggle with an external mouse, and restore it;
6. repeat profile restore after suspend/resume and a cold boot;
7. inspect kernel/journal logs for timeout, disconnect, or ACPI shape errors.

These are execution checks, not unresolved protocol fields. If the WMI module
returns `EPROTO` or `EMSGSIZE`, do not broaden its decoder blindly; capture the
raw ACPI return object and adjust only to the observed fixed layout.
