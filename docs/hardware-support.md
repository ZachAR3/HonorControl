# Hardware Support

## Supported platforms

Honor Control requires a **positive platform match** before any
EC/fan/GPU write. A fallback `Platform` object is insufficient.

Detection checks:
1. Exact verified DMI identity (`HONOR`, product `MRA-XXX`).
2. Verified 2024 Intel Core Ultra/Meteor Lake CPU identity.
3. Required resource discovery (sysfs paths, `acpi_call`, hwmon).
4. Access mode (read/write permissions).
5. Non-mutating read probe.

## Feature requirements

### Battery charge control

- sysfs power supply device with `charge_control_end_threshold` file.
- `charge_control_start_threshold` file (required).
- Does not assume `BAT0` — discovers the battery path.

### Power profiles

- Compatible `honor-tools` 0.1.0 platform backend.
- Running `power-profiles-daemon` and a working `powerprofilesctl` client.
- At least one Intel RAPL sysfs tree with writable PL1/PL2 controls.
- Per-CPU governor and EPP sysfs controls, plus Intel `no_turbo` and
  `max_perf_pct` controls.
- Built-in and custom definitions control PL1/PL2, governor, EPP, PPD mode,
  turbo enablement, and `max_perf_pct`. An apply succeeds only after every
  requested field is read back from hardware.
- Honor Control coordinates through PPD; it never masks PPD or `intel_lpmd`
  and never writes `/dev/cpu/*/msr`.

### Fan control

- Verified MRA-XXX platform.
- `/proc/acpi/call` (acpi_call kernel module).
- Readable, positive `coretemp` or `k10temp` hwmon value.
- RPM is displayed when any hwmon device exposes `fan*_input`. The current
  MRA-XXX kernel interface exposes temperature and EC control but no RPM or
  stock-firmware percentage, so stock mode is reported as firmware-controlled
  instead of fabricating a numeric reading.

### Touchpad gestures

- Touchpad HID identity VID `0x35cc`, PID `0x0104` on hidraw.
- `/dev/uinput` (for virtual keyboard output).
- hidraw access to the touchpad device.

### GPU mitigation

- i915 or xe GPU IRQs present in `/proc/interrupts`.
- Online valid target CPU.
- Writable IRQ affinity files.
- Writable C-state control files.

## Known limitations

- **Firmware setting writes are unsupported.** The observed Windows UI IPC,
  registry values, WMI buffers, and HID input reports do not establish the
  service-to-driver/EC write protocol. Unknown WMI/HID output is never sent.
- **GPU mitigation is intentionally unavailable.** The dependency cannot yet
  restore the exact pre-change IRQ affinity and C-state values.
- **No fallback to a default platform.** Unknown hardware cannot perform
  model-specific power, fan, or GPU writes.
- **AMD power profiles are unsupported.** Non-allowlisted AMD and Intel
  systems report the power domain as unsupported without attempting writes.
- **Fan curve validation is strict.** 2-12 points, strictly increasing
  temperatures, non-decreasing speeds, at/above 95°C must target 100%.
