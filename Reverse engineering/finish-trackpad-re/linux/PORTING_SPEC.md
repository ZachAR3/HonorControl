# Language-neutral Linux porting specification

This is the complete contract needed to reimplement every recovered Honor
touchpad configuration operation without the Windows Honor application.
`../protocol.json` is the machine-readable source of truth; this document adds
the ordering, discovery, failure behavior, and implementation boundaries that
are not obvious from a command table.

## Scope and safety gate

The analyzed machine identifies as DMI system vendor `HONOR`, product name
`MRA-XXX`. Refuse firmware writes on any other identity unless a separately
validated model table is added. The observed touchpad is I2C-HID `35cc:0104`
(release `0101`, ACPI `TOPS0102`). Release is evidence, not a selector: use the
report descriptor to select the collection.

Never select the first matching VID/PID. This device exposes multiple HID
collections. Enumerate `/sys/class/hidraw/hidraw*/device/uevent`, match
`HID_ID` vendor/product, and parse each `report_descriptor`. Select only the
Application collection with:

- Usage Page `0xff00`, Usage `0x0001`;
- Report ID `0x0e`;
- 9-byte Input report including the ID;
- 9-byte Output report including the ID.

Open its `/dev/hidrawN` node `O_RDWR`. Linux hidraw writes include the report ID
as byte zero. Require a return value of exactly 9; a short write is failure.
The node number is not stable across boots or resumes, so rediscover it for
every transaction/reconnection.

## Transaction sequence

Validate the complete requested profile before opening the device. Open once,
then optionally send the OEM startup/resume clock report:

```text
0e 00 TT TT TT TT TT 00 00
```

`TT...` is the low 40 bits of Unix `time(NULL)`, least-significant byte first.
Then write each setting report in the canonical order below. A report is:

```text
0e CC VV 00 00 00 00 00 00
```

| Order | Setting | `CC` | `VV` |
|---:|---|---:|---|
| 1 | sensitivity | `01` | low `00`, high `01` |
| 2 | vibration intensity | `02` | low `00`, medium `01`, high `02` |
| 3 | pressure action for text | `03` | off `00`, on `01` |
| 4 | pressure action for pictures | `04` | off `00`, on `01` |
| 5 | three-finger drag | `05`, then `11` | same boolean in both reports |
| 6 | mouse-like mode | `06` | boolean |
| 7 | edge brightness | `07` | boolean |
| 8 | edge volume | `08` | boolean |
| 9 | edge control center | `09` | boolean |
| 10 | edge close/minimize | `0a` | boolean |
| 11 | knuckle screenshot | `0b` | boolean |
| 12 | knuckle screen recording | `0c` | boolean |

Do not expose arbitrary command bytes. Commands `03`, `04`, and `05` collide
with labels from an older implementation; they are the current public controls
shown above. Command `10` is a legacy double-pressure alias and is not a
separate current UI setting. `0e 0e 00 00 00 00 00 00 00` is an internal
shutdown/reset operation and is not part of profile application.

There is no recovered firmware value-readback command. A successful 9-byte
write proves kernel/transport acceptance, not the current stored value. Keep a
complete desired-state profile and reapply it after boot and resume. Report
partial transaction counts on failure; do not claim rollback, because settings
already accepted by firmware cannot be read back transactionally.

## Capability query and input reports

Write `0e f0 00 00 00 00 00 00 00`, then read the same vendor node until the
9-byte response `0e f0 <seven bitmap bytes>` arrives or a timeout expires.
Bitmap numbering starts at byte 2, least-significant bit first. Known bits are
fully listed in `../protocol.json`.

Only one process should read the vendor node during this query. A gesture daemon
and diagnostic query can otherwise race for the response. Normal input reports
use byte 1 as event type and byte 2 as its parameter:

| Type | Meaning / Linux action boundary |
|---:|---|
| `03` | brightness; byte 2 selects direction |
| `04` | volume; byte 2 selects direction |
| `05` | window switch; byte 2 value `04` selects alternate direction |
| `06` | screenshot request |
| `07` | screen-recording request |
| `08` | minimize request |
| `09` | close request |
| `0a` | control-center request |
| `0b` | single-finger heavy-pressure event |
| `0c` | three-finger-drag event |
| `0e` | diagnostic event |
| `0f` | double-finger-pressure event |
| `10` | three-finger-light-touch / AI-search event |
| `f0` | capability response, not a user action |

These are firmware events, not desktop-protocol calls. Reproducing the Windows
result requires mapping them to the active Linux desktop through uinput, a
compositor API, portals, or desktop-specific commands. The wire protocol is
complete even where Windows-only concepts such as Honor AI search have no
universal Linux equivalent.

## Global master enable switch

The master switch is not HID. Bind a Linux WMI driver to GUID
`ABBC0F5B-8EA1-11D1-A000-C90629100000`, instance 0, method ID 1. Input is a
64-byte zero-filled buffer with the command in its first 8 bytes as
little-endian `u64`:

| Operation | Command |
|---|---:|
| Query | `0x00000f02` |
| Disable | `0x00001002` |
| Enable | `0x00011002` |

The Windows MOF output is `u32Resrved` plus `u8Output[256]`; the useful payload
has status in byte 0 (zero succeeds) and enabled state in byte 1. Retry at most
three times. After enable/disable, query and require the state to match. Enable
master before HID settings; disable it only after them, because the hidraw node
may disappear when disabled.

The supplied driver exposes only a boolean sysfs attribute. Its first physical
Linux query remains the sole unexecuted ABI check: Linux ACPI-WMI may return the
256-byte output directly or with the four-byte reserved prefix. An unexpected
object/length must fail closed and be captured from the kernel log, not guessed
around.

Internal screen lifecycle commands are WMI `0x00001502` (screen off) and
`0x00011502` (screen on). They are documented for completeness but are not
configuration controls and are deliberately absent from the public interface.

## Boot, resume, permissions, and coexistence

- Run profile restore as root, after udev creates hidraw, with a bounded wait
  and before starting the gesture reader.
- On resume, rediscover the node and send a fresh clock report before settings.
- Serialize writers. Stop or coordinate the gesture reader for capability
  query; writes alone do not consume its input stream.
- Load the optional WMI module before a profile containing `[master]`.
- Use an external mouse for the first master-switch test.
- Preserve logs, descriptor, DMI strings, kernel version, and partial write
  count for any physical validation failure.

The supplied Python code is the executable reference. The standalone
`../../honor_touchpad_linux.c` demonstrates the HID transaction with hidapi but
deliberately lacks the Python implementation's descriptor and DMI validation.

## Definition of a complete Linux remake

A port is complete when:

1. exact DMI and vendor collection are discovered without a hardcoded node;
2. every report matches `../protocol.json` byte-for-byte;
3. three-finger drag always writes both reports in order;
4. complete validation occurs before the first write;
5. short writes, timeouts, disconnects, and partial profiles fail visibly;
6. capability response reading has exclusive-reader coordination;
7. master query/change/query succeeds through the narrow WMI method;
8. the profile restores after cold boot and suspend/resume;
9. every input event is mapped explicitly or surfaced as unsupported;
10. no raw HID, WMI, EC, or arbitrary-command escape hatch is exposed.
