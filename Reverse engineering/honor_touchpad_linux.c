/*
 * honor_touchpad_linux.c - Linux replay tool for Honor MagicTouchPad settings.
 *
 * Target: I2C-HID trackpad "TOPS0102", HID descriptor VID 0x35CC PID 0x0104.
 * Config channel: vendor-defined collection UsagePage 0xFF00, Usage 0x01,
 *                 9-byte input/output reports (report ID + 8 payload bytes).
 * Standard channel: mtconfig collection (Digitizer page 0x000D, Usage 0x000E),
 *                   2-byte feature report (HID_INPUT_CONFIGURATION).
 *
 * Build:   gcc honor_touchpad_linux.c -o honor_touchpad_linux -lhidapi-hidraw
 *   (or -lhidapi-libusb if you prefer the libusb backend; hidraw is correct for I2C-HID)
 *
 * Prereq on Linux:
 *   - The device binds to i2c-hid + hid-multitouch. Check:
 *       dmesg | grep -i tops   ;   ls /sys/bus/i2c/devices/*/hid
 *       cat /sys/class/hidraw/hidrawN/device/uevent  (find the one with HID_ID=0005:000035CC:00000104)
 *   - The vendor collection (0xFF00:0x01) must be reachable via a hidraw node.
 *     If hid-multitouch claims the whole device and hides 0xFF00, add a quirk
 *     (HID_QUIRK_HAVE_HIDRAW) or a tiny hid driver that exports the collection,
 *     then the matching /dev/hidraw* shows up.
 *
 * Usage:
 *   honor_touchpad_linux                      # list matching devices
 *   honor_touchpad_linux set <setting> <val>  # send one vendor output report
 *
 * Settings (names from MagicTouchPadHelper.dll; the report_id/payload byte
 * layout must be filled in from the Frida capture on Windows):
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <hidapi/hidapi.h>

#define HONOR_VID 0x35CC
#define HONOR_PID 0x0104
#define VENDOR_USAGE_PAGE 0xFF00
#define VENDOR_USAGE      0x01
#define VENDOR_REPORT_LEN 9   /* 1 report-id byte + 8 payload bytes */

/* TODO: fill these in from capture_honor_touchpad.js output.
 * Each setting's 9-byte output report = [report_id, b0..b7]. */
struct setting_map {
    const char *name;
    unsigned char report[VENDOR_REPORT_LEN];
};

static struct setting_map settings[] = {
    /* Example (placeholder) — replace with real bytes from Frida:
     *   { "touchpadenable",        { 0x06, 0x01, 0x00,0x00,0x00,0x00,0x00,0x00,0x00 } },
     */
    { "touchpadenable",            { 0 } },
    { "sensitivity",               { 0 } },
    { "shock",                     { 0 } },  /* palm rejection */
    { "mouselikemode",             { 0 } },
    { "threefingerdrag",           { 0 } },
    { "sensitivitypresstext",      { 0 } },
    { "sensitivitypresspicture",   { 0 } },
    { "knucklescreenshot",         { 0 } },
    { "knucklerecordscreen",       { 0 } },
    { "edgegesture_adjusbrightness",{ 0 } },
    { "edgegesture_adjusvolume",   { 0 } },
    { "edgegesture_openhicenter",  { 0 } },
    { "edgegesture_closeorminwnd", { 0 } },
    { NULL, { 0 } },
};

static void list_devices(void) {
    struct hid_device_info *devs = hid_enumerate(HONOR_VID, HONOR_PID);
    for (struct hid_info *d = devs; d; d = d->next) {
        printf("path=%ls  vid=0x%04x pid=0x%04x  release=0x%04x  usage=0x%x usage_page=0x%x  product=%ls\n",
               d->path, d->vendor_id, d->product_id, d->release_number,
               d->usage, d->usage_page, d->product_string ? d->product_string : L"(null)");
    }
    hid_free_enumeration(devs);
}

int main(int argc, char **argv) {
    if (argc < 2) { list_devices(); return 0; }

    if (!strcmp(argv[1], "set") && argc == 4) {
        const char *name = argv[2];
        struct setting_map *m = NULL;
        for (struct setting_map *s = settings; s->name; s++) {
            if (!strcasecmp(s->name, name)) { m = s; break; }
        }
        if (!m) { fprintf(stderr, "unknown setting: %s\n", name); return 2; }
        long v = strtol(argv[3], NULL, 0);
        /* Apply the value into the placeholder report. The exact encoding of
         * the value byte(s) depends on what Frida captured per setting. */
        m->report[1] = (unsigned char)v;

        hid_device *h = hid_open(HONOR_VID, HONOR_PID, NULL);
        if (!h) { fprintf(stderr, "hid_open failed\n"); return 3; }
        int r = hid_write(h, m->report, VENDOR_REPORT_LEN);
        printf("hid_write -> %d bytes  report: ", r);
        for (int i = 0; i < VENDOR_REPORT_LEN; i++) printf("%02x ", m->report[i]);
        printf("\n");
        hid_close(h);
        return r < 0 ? 4 : 0;
    }

    fprintf(stderr, "usage: %s | %s set <setting> <value>\n", argv[0], argv[0]);
    return 1;
}
