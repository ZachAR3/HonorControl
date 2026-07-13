# Reverse-engineering status

The authoritative current touchpad result is documented in
`../docs/gesture-linux-remaining-work.md`, with the machine-readable wire
contract in `finish-trackpad-re/protocol.json` and implementation rules in
`finish-trackpad-re/linux/PORTING_SPEC.md`.

`honor_drivers/` and `honor_touchpad_reverse_engineering.md` are preserved as
historical investigation records. Their final WMI/EC conclusion is superseded:
the updated Honor plugin proves that ordinary settings are nine-byte HID Output
reports sent with `WriteFile`. Only the global master switch uses OEM WMI.

Do not run the old `wmi_ioctl_test.js` or infer a firmware command from the
61-byte UI IPC packet. Use the typed Linux tooling and first-boot checklist in
`finish-trackpad-re/linux/README.md`.
