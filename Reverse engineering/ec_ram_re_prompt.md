# Honor MagicBook Art 14 2024 — EC RAM Touchpad Gesture Reverse Engineering

## Goal

Find which EC (Embedded Controller) RAM offsets store the touchpad gesture configuration settings on an Honor MagicBook Art 14 2024 (Meteor Lake, Core Ultra 5 125H). Once found, these can be written from Linux to control touchpad gestures without booting into Windows.

## Background

The touchpad on this laptop is an I2C-HID device (VID 0x35CC, PID 0x0104, "TOPS0102"). On Windows, the Honor PC Manager software configures touchpad gestures (sensitivity, knuckle screenshot, edge swipes, etc.) via the Honor EC driver (`HNOs2EC10x64.sys`), which writes directly to EC RAM using `HalGetBusDataByOffset`.

Frida captures confirmed that these config writes do **NOT** go through HID output reports or ACPI methods — they go directly to EC RAM. The ACPI DSDT was analyzed and most WMI "Set" methods for touchpad gestures (`SFNM`, `STUB`, `SWKM`, `SPFM`, `SFNS`, `WWST`) return `STAT=1` (not implemented), confirming the Windows driver bypasses ACPI.

## EC Architecture (from ACPI DSDT analysis)

The EC on this laptop exposes several access mechanisms:

### EC Shared Memory (Command Interface)
- **Address**: `0xFE0B0400` (SystemMemory, 0x80 bytes)
- **Layout**:
  | Offset | Name | Size | Purpose |
  |--------|------|------|---------|
  | 0x00 | CMDB | 8-bit | Command byte (write command, EC clears when done) |
  | 0x01 | STAT | 8-bit | Status |
  | 0x02 | NUMB | 8-bit | Number of data bytes |
  | 0x03 | DAT0 | 8-bit | Data byte 0 (highest byte of 32-bit address) |
  | 0x04 | DAT1 | 8-bit | Data byte 1 |
  | 0x05 | DAT2 | 8-bit | Data byte 2 |
  | 0x06 | DAT3 | 8-bit | Data byte 3 (lowest byte of 32-bit address) |
  | 0x07 | DAT4 | 8-bit | Data byte 4 |
  | 0x08 | DATR | 8-bit | Data return / data to write |
  | 0x09 | DAR1 | 8-bit | Data return 1 |
  | 0x0A | DAR2 | 8-bit | Data return 2 |

### EC RAM (EmbeddedControl region)
- **Region**: `ERM2`, EmbeddedControl, offset 0x00-0xFF (256 bytes)
- **Known fields**:
  | Offset | Name | Purpose |
  |--------|------|---------|
  | 0x10 | ER10 | Touchpad/keyboard control byte (bit 0x04 = touchpad settings, bit 0x08 = touchpad enable) |
  | 0x6C | KBL2 | Keyboard backlight 2 |
  | 0x6D | KBH2 | Keyboard HID 2 |
  | 0x78 | KZM2 | Keyboard zone mode 2 |
  | 0xDC | TUBF | Turbo flag (1 bit) |

### EC I/O Ports (direct port access)
- **Command port**: 0x66 (EC66 in ACPI)
- **Data port**: 0x62 (EC62 in ACPI)
- **Secondary**: 0x68 (EC68), 0x6C (EC6C)
- The Windows driver uses `HalGetBusDataByOffset` to read/write EC RAM directly

### EC Memory Region (EMEM)
- **Address**: `0xFE0B0300` (SystemMemory, 0x20 bytes)
- **Known fields**: offset 0x10 = EM10 (touchpad status mirror)

## Known EC Commands (from ACPI DSDT)

These commands are written to CMDB with data in DATR:

| Command | Data | Purpose | Source Method |
|---------|------|---------|---------------|
| 0x46 | 0xA0 | Fn key lock OFF | SFRS(1) |
| 0x46 | 0xA1 | Fn key lock ON | SFRS(2) |
| 0x48 | 0xA0 | SMLS ON | SMLS(0) |
| 0x48 | 0xA1 | SMLS OFF | SMLS(1) |
| 0x73 | data | EDDS command | EDDS() |
| 0x80 | addr | Read EC register (32-bit addr in DAT0-DAT3) | RDER() |
| 0x81 | data | Write EC register (addr in DAT0-DAT3) | ECWT() |
| 0x29 | — | WTER command (fan control) | WTER() |

## Touchpad Settings to Find

These are the settings exposed in Honor PC Manager that we need EC RAM offsets for:

| Setting | Values | RE Doc Setting ID |
|---------|--------|-------------------|
| Sensitivity | 0=low, 1=high | 0x02 |
| Palm rejection (shock) | 0=off, 1=low, 2=high | 0x03 |
| Three-finger drag | 0=off, 1=on | 0x06 |
| Edge brightness gesture | 0=off, 1=on | 0x08 |
| Edge volume gesture | 0=off, 1=on | 0x09 |
| Edge HiCenter gesture | 0=off, 1=on | 0x0A |
| Edge close/minimize gesture | 0=off, 1=on | 0x0B |
| Knuckle screenshot | 0=off, 1=on | 0x0C |
| Knuckle screen record | 0=off, 1=on | 0x0D |

## Methodology

We need to dump EC RAM, toggle ONE setting at a time in Honor PC Manager, and compare the dumps to find which bytes changed.

### Step 1: Install RWEverything

1. Download RWEverything from http://rweverything.com/
2. Install and launch as Administrator
3. Go to: Access → EC (Embedded Controller)
4. You'll see a grid of 256 bytes (offsets 0x00 to 0xFF)

### Step 2: Baseline EC RAM dump

1. Open Honor PC Manager
2. Go to the touchpad settings page
3. **Set ALL touchpad gestures to OFF/disabled** (sensitivity = low)
4. Wait 5 seconds for settings to settle
5. Open RWEverything → Access → EC
6. Record all 256 bytes (screenshot or manual transcription)
7. Save this as "baseline_off.txt"

### Step 3: Toggle each setting individually

For EACH setting below, do the following:
1. Start from the baseline (all off)
2. Change ONLY the one setting to ON/high
3. Wait 5 seconds
4. Dump EC RAM (all 256 bytes)
5. Compare with baseline
6. Record which bytes changed and their new values
7. Set the setting back to OFF
8. Wait 5 seconds
9. Dump EC RAM again to confirm the byte reverted

#### Settings to test (in this order):

1. **Sensitivity**: low → high
2. **Palm rejection**: off → high
3. **Three-finger drag**: off → on
4. **Edge brightness gesture**: off → on
5. **Edge volume gesture**: off → on
6. **Edge HiCenter gesture**: off → on
7. **Edge close/minimize gesture**: off → on
8. **Knuckle screenshot**: off → on
9. **Knuckle screen record**: off → on

### Step 4: Document findings

For each setting, record:

```
Setting: [name]
Value changed: [off → on] or [low → high]
EC RAM bytes that changed:
  - 0x[XX]: 0x[old] → 0x[new]
  - 0x[XX]: 0x[old] → 0x[new]
Reverted when set back to off: [yes/no]
```

## Alternative: Automated EC RAM monitoring

If RWEverything's manual approach is too slow, consider using a Python script with `portio` or `inpout32` driver to automatically dump EC RAM every second and log changes:

```python
# Requires: pip install pywin32
# Or use the inpout32.dll driver for direct port I/O

import ctypes
import time

# Load inpout32 driver (download from http://www.highrez.co.uk/downloads/inpout32/)
inpout = ctypes.windll.inpout32

def read_ec(offset):
    """Read a byte from EC RAM via ports 0x66/0x62"""
    inpout.Out32(0x66, offset)
    time.sleep(0.001)
    return inpout.Inp32(0x62)

def dump_ec():
    """Dump all 256 bytes of EC RAM"""
    return {i: read_ec(i) for i in range(256)}

# Monitor for changes
prev = dump_ec()
print("Baseline captured. Change a touchpad setting in PC Manager...")
while True:
    time.sleep(1)
    curr = dump_ec()
    changes = {k: (prev[k], curr[k]) for k in curr if curr[k] != prev[k]}
    if changes:
        print(f"[{time.strftime('%H:%M:%S')}] Changed:")
        for offset, (old, new) in changes.items():
            print(f"  0x{offset:02X}: 0x{old:02X} → 0x{new:02X}")
    prev = curr
```

**Note**: The exact port I/O protocol for EC access may vary. RWEverything's EC access dialog uses the correct sequence for this laptop. If using a script, verify it matches RWEverything's output first.

## Alternative: Frida hook on HalGetBusDataByOffset

If you have Frida set up on Windows (the user previously captured touchpad IPC traffic with Frida), you can hook the EC driver directly:

```javascript
// Hook HalGetBusDataByOffset in HNOs2EC10x64.sys
// This is the function that reads/writes EC RAM
// Look for imports of HalGetBusDataByOffset in the driver

var halGetBusData = Module.findExportByName("ntoskrnl.exe", "HalGetBusDataByOffset");
if (halGetBusData) {
    Interceptor.attach(halGetBusData, {
        onEnter: function(args) {
            this.busDataType = args[0].toInt32();
            this.slotNumber = args[1];
            this.offset = args[2].toInt32();
            this.buffer = args[3];
            this.length = args[4].toInt32();
        },
        onLeave: function(retval) {
            if (this.length <= 4) {
                var data = this.buffer.readU8();
                console.log(`[HalGetBusDataByOffset] bus=${this.busDataType} slot=${this.slotNumber} offset=0x${this.offset.toString(16)} data=0x${data.toString(16)} len=${this.length}`);
            }
        }
    });
}
```

This would directly capture the EC RAM offsets being written when touchpad settings change.

## What to provide back

After completing the EC RAM dump comparison, provide:

1. **For each touchpad setting**: the EC RAM offset(s) that changed and the values
2. **The full baseline EC RAM dump** (all 256 bytes) for reference
3. **Any observations** about settings that didn't change EC RAM (these might use a different mechanism)

## Expected output format

```
## EC RAM Touchpad Gesture Offsets

| Setting | EC Offset | Off value | On value | Notes |
|---------|-----------|-----------|----------|-------|
| Sensitivity | 0x?? | 0x00 | 0x01 | |
| Palm rejection | 0x?? | 0x00 | 0x02 | |
| Three-finger drag | 0x?? | 0x00 | 0x01 | |
| Edge brightness | 0x?? | 0x00 | 0x01 | |
| Edge volume | 0x?? | 0x00 | 0x01 | |
| Edge HiCenter | 0x?? | 0x00 | 0x01 | |
| Edge close/min | 0x?? | 0x00 | 0x01 | |
| Knuckle screenshot | 0x?? | 0x00 | 0x01 | |
| Knuckle record | 0x?? | 0x00 | 0x01 | |

## Baseline EC RAM dump
0x00: XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX
0x10: XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX
...
0xF0: XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX XX
```

Once we have this data, we can write to these EC RAM offsets from Linux using `acpi_call` with the `ECWT` method (command 0x81) or direct port I/O, enabling full touchpad gesture control from Linux.
