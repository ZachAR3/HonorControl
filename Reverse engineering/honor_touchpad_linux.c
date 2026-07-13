/* SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Standalone Linux hidapi replay tool for the confirmed Honor MagicTouchPad
 * vendor protocol. The packaged Python command honor-touchpadctl performs
 * stronger report-descriptor verification and is preferred for normal use.
 *
 * Build:
 *   cc -O2 -Wall -Wextra honor_touchpad_linux.c -o honor_touchpad_linux \
 *      $(pkg-config --cflags --libs hidapi-hidraw)
 */

#include <errno.h>
#include <hidapi/hidapi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>

#define HONOR_VID 0x35cc
#define HONOR_PID 0x0104
#define HONOR_USAGE_PAGE 0xff00
#define HONOR_USAGE 0x0001
#define HONOR_REPORT_ID 0x0e
#define HONOR_REPORT_LEN 9

struct setting {
	const char *name;
	uint8_t command;
	uint8_t maximum;
	uint8_t companion;
};

static const struct setting settings[] = {
	{ "sensitivity", 0x01, 1, 0 },
	{ "vibration_intensity", 0x02, 2, 0 },
	{ "shock", 0x02, 2, 0 },
	{ "press_text", 0x03, 1, 0 },
	{ "press_picture", 0x04, 1, 0 },
	{ "three_finger_drag", 0x05, 1, 0x11 },
	{ "mouse_like_mode", 0x06, 1, 0 },
	{ "edge_brightness", 0x07, 1, 0 },
	{ "edge_volume", 0x08, 1, 0 },
	{ "edge_control_center", 0x09, 1, 0 },
	{ "edge_close_or_minimize", 0x0a, 1, 0 },
	{ "knuckle_screenshot", 0x0b, 1, 0 },
	{ "knuckle_screen_record", 0x0c, 1, 0 },
	{ NULL, 0, 0, 0 },
};

static void make_report(uint8_t report[HONOR_REPORT_LEN], uint8_t command,
			uint8_t value)
{
	memset(report, 0, HONOR_REPORT_LEN);
	report[0] = HONOR_REPORT_ID;
	report[1] = command;
	report[2] = value;
}

static void make_timestamp_report(uint8_t report[HONOR_REPORT_LEN])
{
	uint64_t seconds = (uint64_t)time(NULL);
	int i;

	make_report(report, 0, 0);
	for (i = 0; i < 5; i++)
		report[2 + i] = (uint8_t)(seconds >> (8 * i));
}

static const struct setting *find_setting(const char *name)
{
	const struct setting *item;

	for (item = settings; item->name; item++)
		if (!strcasecmp(item->name, name))
			return item;
	return NULL;
}

static char *find_exact_path(void)
{
	struct hid_device_info *devices;
	struct hid_device_info *item;
	char *result = NULL;

	devices = hid_enumerate(HONOR_VID, HONOR_PID);
	for (item = devices; item; item = item->next) {
		if (item->usage_page != HONOR_USAGE_PAGE ||
		    item->usage != HONOR_USAGE)
			continue;
		result = strdup(item->path);
		break;
	}
	hid_free_enumeration(devices);
	return result;
}

static void list_devices(void)
{
	struct hid_device_info *devices;
	struct hid_device_info *item;

	devices = hid_enumerate(HONOR_VID, HONOR_PID);
	for (item = devices; item; item = item->next)
		printf("path=%s usage_page=0x%04x usage=0x%04x release=0x%04x%s\n",
		       item->path, item->usage_page, item->usage,
		       item->release_number,
		       item->usage_page == HONOR_USAGE_PAGE &&
		       item->usage == HONOR_USAGE ? " [control]" : "");
	hid_free_enumeration(devices);
}

static int write_report(hid_device *device,
			const uint8_t report[HONOR_REPORT_LEN])
{
	int result = hid_write(device, report, HONOR_REPORT_LEN);

	if (result == HONOR_REPORT_LEN)
		return 0;
	if (result < 0)
		fwprintf(stderr, L"hid_write failed: %ls\n", hid_error(device));
	else
		fprintf(stderr, "short hid_write: %d of %d bytes\n", result,
			HONOR_REPORT_LEN);
	return -1;
}

static int set_value(const char *name, const char *value_text)
{
	const struct setting *setting = find_setting(name);
	char *end = NULL;
	long value;
	char *path;
	hid_device *device;
	uint8_t report[HONOR_REPORT_LEN];
	int result = 1;

	if (!setting) {
		fprintf(stderr, "unknown setting: %s\n", name);
		return 2;
	}
	errno = 0;
	value = strtol(value_text, &end, 0);
	if (errno || !end || *end || value < 0 || value > setting->maximum) {
		fprintf(stderr, "%s must be 0..%u\n", name, setting->maximum);
		return 2;
	}

	path = find_exact_path();
	if (!path) {
		fprintf(stderr, "35cc:0104 vendor collection ff00:0001 not found\n");
		return 3;
	}
	device = hid_open_path(path);
	if (!device) {
		fprintf(stderr, "cannot open %s read/write\n", path);
		free(path);
		return 3;
	}

	make_timestamp_report(report);
	if (write_report(device, report))
		goto out;
	make_report(report, setting->command, (uint8_t)value);
	if (write_report(device, report))
		goto out;
	if (setting->companion) {
		make_report(report, setting->companion, (uint8_t)value);
		if (write_report(device, report))
			goto out;
	}
	printf("applied %s=%ld on %s (no firmware value readback)\n",
	       setting->name, value, path);
	result = 0;

out:
	hid_close(device);
	free(path);
	return result;
}

static void print_settings(void)
{
	const struct setting *item;

	for (item = settings; item->name; item++)
		printf("%-28s 0..%u report=0e %02x VV%s\n", item->name,
		       item->maximum, item->command,
		       item->companion ? " + companion" : "");
}

int main(int argc, char **argv)
{
	int result;

	result = hid_init();
	if (result) {
		fprintf(stderr, "hid_init failed: %d\n", result);
		return 1;
	}

	if (argc == 1 || !strcmp(argv[1], "list-devices")) {
		list_devices();
		result = 0;
	} else if (!strcmp(argv[1], "list-settings") && argc == 2) {
		print_settings();
		result = 0;
	} else if (!strcmp(argv[1], "set") && argc == 4) {
		result = set_value(argv[2], argv[3]);
	} else {
		fprintf(stderr,
			"usage: %s [list-devices|list-settings|set SETTING VALUE]\n",
			argv[0]);
		result = 2;
	}

	hid_exit();
	return result;
}
