# Honor Touchpad Gestures: Remaining Windows-to-Linux Work

## Scope and current result

The Linux action path is implemented for the confirmed vendor input report:

`TOPS0102 touchpad → /dev/hidraw* → GestureRuntime → /dev/uinput → Linux keys`

The service discovers HID VID `0x35cc`, PID `0x0104`, accepts only report ID
`0x0e`, reads the gesture type/subtype from bytes 1/2, and dispatches validated
key chords. It reconnects after device removal and exposes device, report, and
emission status through D-Bus/UI. It performs no HID output, WMI, or EC write.

Fully enabling/configuring every firmware gesture from Linux is **not yet
possible**. The missing artifact is the Windows service/driver-to-hardware
write protocol. The existing captures establish UI IPC and input events, but
not that final write.

## Evidence that is safe to use

| Evidence | Established meaning | Linux use |
|---|---|---|
| ACPI `TOPS0102`; HID `0018:35CC:0104` | Target device identity | Exact hidraw discovery |
| HID page `0xff00`, report ID `0x0e`, 8-byte payload | Vendor **input** report | Read only |
| `3:1`, `3:2` | Brightness up/down | Implemented defaults |
| `4:1`, `4:2` | Volume up/down | Implemented defaults |
| `10:3` | Two-finger swipe left | Implemented default |
| types `6`, `7`, `8`, `9` | Knuckle/corner actions | Implemented defaults |
| 61-byte PC Manager IPC; setting ID at byte 27, value at byte 52 | UI-to-`PCManagerMainService` message | RE lead only |
| setting IDs `02`, `03`, `06`, `08`–`0d` | Windows UI setting identifiers | Do not send to hardware |
| Registry values | Windows persistence/cache mirror | Do not treat as hardware protocol |
| WMI GUID `ABBC0F5B-8EA1-11D1-A000-C90629100000`, MethodId 1 | A transport exists | Read-only presence probe |
| WMI buffers beginning `06 02` / `06 0e` | Unknown operations/effects | Never execute |

Conflicting claims in the existing markdown reports are hypotheses, not proof.
The duplicated ` (1)` files contain no independent corroboration. The C PoC
`Reverse engineering/honor_touchpad_linux.c` is a placeholder: its mappings
are empty and it names the hidapi type incorrectly, so it is not an
implementation reference.

## What must be captured on Windows

Use the same MRA-XXX machine and record the exact PC Manager, service, driver,
and BIOS versions. Keep Windows offline during capture where practical and
retain original binaries. Hash every binary with SHA-256.

Required artifacts:

1. Export the relevant driver packages with `pnputil /export-driver`, including
   INF, catalog, service executable/DLLs, and `.sys` files. Save `driverquery
   /v`, device instance IDs, service names, and file versions.
2. Record one clean boot with all gesture settings at a known baseline. Export
   the relevant registry branch before and after; registry changes alone are
   not protocol evidence.
3. Capture one setting transition at a time: `off→on`, then `on→off`. Do not
   click other PC Manager controls in the same trace. Include a no-change click
   as a negative control.
4. Repeat each transition at least three times. Then reboot Windows and verify
   whether the hardware behavior persisted before opening PC Manager.
5. Separately perform each physical gesture while no setting changes occur.
   This separates input reports from configuration traffic.

For every experiment save synchronized timestamps and this row:

| Field | Required value |
|---|---|
| setting name / UI ID | exact name and observed 61-byte ID |
| old/new value | `0→1` or `1→0` |
| UI IPC | complete request/response bytes |
| service call stack | module + RVA/symbol at hardware boundary |
| device handle | path/interface GUID and owning PID |
| operation | API plus IOCTL/report/method identifier |
| input/output buffers | exact bytes and lengths, before and after call |
| return status | Win32/NTSTATUS and bytes returned |
| observed effect | behavior before/after and after reboot |

## Capture order

### 1. Identify the real hardware boundary

Use Process Monitor first to correlate the UI action with
`PCManagerMainService` and child/helper processes. Capture process/thread
activity, registry writes, device opens, and image loads. Procmon will not
usually expose kernel IOCTL payloads; its purpose here is to identify the
process, device path, and timing.

In the service and loaded DLLs, locate imports/call sites for:

- `CreateFileW`, `WriteFile`, `DeviceIoControl`;
- `HidD_SetFeature`, `HidD_SetOutputReport`, `HidD_GetFeature`;
- `SetupDi*`, `CM_Get_Device_Interface_List*`;
- `IWbemServices::ExecMethod` / WMI COM calls.

Use Ghidra/IDA to trace the 61-byte IPC parser from the setting ID to one of
those boundary calls. Record module-relative offsets and decompile the buffer
builder. Static matches without a live call are insufficient.

### 2. Capture user-mode calls and buffers

Attach to the **service process**, not only the PC Manager UI. Use WinDbg or
Frida hooks on the boundary functions above. Log entry and return, thread ID,
handle, control code/report ID, input/output lengths, full buffers, and return
status. Resolve each handle to its NT/DOS device path.

If the process is protected or injection is blocked, use a test-signed VM or
kernel debugging on a cloned installation. Do not disable protections on the
only working Windows install without a recoverable image.

Success criterion: toggling one setting produces a reproducible call whose
payload changes only in understood fields and whose success/failure matches the
observed hardware behavior.

### 3. Trace the kernel driver when user mode ends at an IOCTL

If the service calls a vendor driver, use WinDbg kernel debugging to break on
that driver's `IRP_MJ_DEVICE_CONTROL` / `IRP_MJ_INTERNAL_DEVICE_CONTROL`
dispatch. Record:

- IOCTL value and decoded transfer method/access bits;
- input/output buffer lengths and bytes;
- dispatch handler and downstream calls;
- any ACPI `_DSM`/WMI method, I2C HID transfer, EC command, or HID class request;
- completion status and output bytes.

Static analysis must then explain the observed live path. The current
`HNOs2EC10x64.sys`/`OS2SOC.sys` reports do not yet prove that a particular
IOCTL carries touchpad settings.

### 4. Capture the final transport

- For HID class calls, record report type, report ID, total length, and every
  byte. Confirm with the HID report descriptor whether it is Feature or Output.
- For ACPI/WMI, record GUID, instance, method ID, input/output buffers, ACPI
  status, and AML method reached. Never infer semantics from the first two
  bytes alone.
- For I2C/EC traffic, prefer kernel/driver instrumentation. USBPcap does not
  capture an internal I2C-HID touchpad.

Change one setting per trace and compute bytewise diffs across repetitions.
Fields that vary with timestamps, sequence numbers, checksums, or device IDs
must be identified before Linux reproduction.

## Protocol acceptance gate

Do not add a Linux write implementation until all items pass:

1. The same Windows call occurs for at least three repetitions of one toggle.
2. Opposite values produce a stable, explained payload difference.
3. A negative-control UI action does not produce the call.
4. The complete route is known: UI IPC → service parser → driver/API → final
   HID/WMI/ACPI/I2C/EC operation.
5. Return status and any readback are understood.
6. The exact target identity and supported firmware/driver versions are known.
7. Recovery is proven: stock value can be restored after interruption,
   malformed input is rejected, and a failed write cannot leave an unsafe
   partial state.
8. The operation has been replayed first on Windows through a minimal harness,
   then on Linux in a readback-only or reversible laboratory test.

## Linux implementation after the gate

Keep firmware configuration separate from `GestureRuntime`:

1. Add a narrow `GestureFirmwareTransport` implementation for the proven
   transport. No generic raw-command D-Bus method.
2. Add an exact platform/firmware allowlist and a non-mutating capability
   probe. Unknown versions are read-only/unsupported.
3. Model each setting as a typed enum/boolean with explicit encoding. Reject
   unknown IDs, lengths, values, and reserved bits.
4. Serialize writes through `HardwareCommandQueue`; apply one setting at a
   time with timeout, readback, and structured partial-failure reporting.
5. Save desired state only after verified success. Preserve observed,
   desired, and applied values separately.
6. Add golden-vector tests from sanitized Windows captures, invalid-buffer
   tests, timeout/queue-poison tests, disconnect tests, and restore tests.
7. Expose only typed D-Bus methods protected by the gesture polkit action.
8. Add an explicit opt-in experimental flag until multiple cold boots and
   suspend/resume cycles pass on real hardware.

## What is usable now

After installing the current source, verify:

```bash
systemctl status honor-control.service
honorctl status
honorctl gestures daemon on
honorctl gestures list
journalctl -u honor-control.service -n 100
```

The service account must be able to read the discovered `/dev/hidraw*` node and
write `/dev/uinput`; the production root service normally can. The UI reports
the selected device, daemon state, reports seen, emitted actions, and last
error. If firmware already emits the confirmed reports, Linux key actions work
without any Windows-side protocol work. Windows RE is required only to change
the touchpad firmware's own gesture enable/configuration settings from Linux.
