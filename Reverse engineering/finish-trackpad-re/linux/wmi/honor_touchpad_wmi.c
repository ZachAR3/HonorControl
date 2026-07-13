// SPDX-License-Identifier: GPL-2.0-only
/*
 * Narrow ACPI-WMI bridge for the Honor MRA-XXX touchpad master switch.
 *
 * Recovered Windows contract:
 *   namespace: ROOT\\WMI
 *   class:     OemWMIMethod
 *   instance:  ACPI\\PNP0C14\\HWMI_0
 *   GUID:      ABBC0F5B-8EA1-11D1-A000-C90629100000
 *   method:    OemWMIfun / WmiMethodId 1
 *   input:     64 bytes, command as little-endian u64, remaining bytes zero
 *   query:     0x00000f02 -> output[0] status, output[1] enabled
 *   disable:   0x00001002
 *   enable:    0x00011002
 *
 * No generic command passthrough is provided.
 */

#include <linux/acpi.h>
#include <linux/device.h>
#include <linux/dmi.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/unaligned.h>
#include <linux/wmi.h>

#define HONOR_TOUCHPAD_WMI_GUID "ABBC0F5B-8EA1-11D1-A000-C90629100000"
#define HONOR_WMI_METHOD_ID 1
#define HONOR_WMI_INPUT_SIZE 64
#define HONOR_WMI_OUTPUT_SIZE 256

#define HONOR_TOUCHPAD_QUERY 0x00000f02ULL
#define HONOR_TOUCHPAD_DISABLE 0x00001002ULL
#define HONOR_TOUCHPAD_ENABLE 0x00011002ULL

struct honor_touchpad_wmi {
	struct wmi_device *wdev;
	struct mutex lock;
};

static const struct dmi_system_id honor_touchpad_dmi_table[] = {
	{
		.ident = "Honor MagicBook Art 14 (MRA-XXX)",
		.matches = {
			DMI_EXACT_MATCH(DMI_SYS_VENDOR, "HONOR"),
			DMI_EXACT_MATCH(DMI_PRODUCT_NAME, "MRA-XXX"),
		},
	},
	{}
};

MODULE_DEVICE_TABLE(dmi, honor_touchpad_dmi_table);

/*
 * ACPI-WMI implementations return either the MOF byte-array directly or a
 * method-output buffer with the four-byte u32Resrved field preceding it.
 * Accept only those two layouts; do not scan arbitrary output for a match.
 */
static int honor_extract_output(union acpi_object *obj,
				const u8 **payload, size_t *length)
{
	union acpi_object *element;

	if (!obj)
		return -ENODATA;

	if (obj->type == ACPI_TYPE_PACKAGE) {
		if (obj->package.count != 1)
			return -EPROTO;
		element = &obj->package.elements[0];
		if (element->type != ACPI_TYPE_BUFFER)
			return -EPROTO;
		obj = element;
	}

	if (obj->type != ACPI_TYPE_BUFFER || !obj->buffer.pointer)
		return -EPROTO;

	if (obj->buffer.length == HONOR_WMI_OUTPUT_SIZE) {
		*payload = obj->buffer.pointer;
		*length = obj->buffer.length;
		return 0;
	}

	if (obj->buffer.length == sizeof(u32) + HONOR_WMI_OUTPUT_SIZE) {
		*payload = obj->buffer.pointer + sizeof(u32);
		*length = HONOR_WMI_OUTPUT_SIZE;
		return 0;
	}

	return -EMSGSIZE;
}

static int honor_touchpad_call(struct honor_touchpad_wmi *priv, u64 command,
			       u8 *state)
{
	u8 input_data[HONOR_WMI_INPUT_SIZE] = {};
	struct acpi_buffer input = {
		.length = sizeof(input_data),
		.pointer = input_data,
	};
	struct acpi_buffer output = {
		.length = ACPI_ALLOCATE_BUFFER,
		.pointer = NULL,
	};
	union acpi_object *obj;
	const u8 *payload;
	size_t payload_length;
	acpi_status status;
	int attempt;
	int ret = -EIO;

	put_unaligned_le64(command, input_data);

	/* The OEM implementation retries GetOutPutUInt up to three times. */
	for (attempt = 0; attempt < 3; attempt++) {
		output.pointer = NULL;
		status = wmidev_evaluate_method(priv->wdev, 0,
						HONOR_WMI_METHOD_ID,
						&input, &output);
		if (ACPI_FAILURE(status)) {
			ret = -EIO;
			kfree(output.pointer);
			continue;
		}

		obj = output.pointer;
		ret = honor_extract_output(obj, &payload, &payload_length);
		if (!ret && payload_length < 2)
			ret = -EMSGSIZE;
		if (!ret && payload[0] != 0)
			ret = -EREMOTEIO;
		if (!ret) {
			if (state)
				*state = payload[1];
			kfree(output.pointer);
			return 0;
		}
		kfree(output.pointer);
	}

	dev_err(&priv->wdev->dev,
		"WMI command 0x%llx failed after three attempts (%d)\n",
		command, ret);
	return ret;
}

static ssize_t touchpad_enabled_show(struct device *dev,
				     struct device_attribute *attr, char *buf)
{
	struct honor_touchpad_wmi *priv = dev_get_drvdata(dev);
	u8 enabled;
	int ret;

	mutex_lock(&priv->lock);
	ret = honor_touchpad_call(priv, HONOR_TOUCHPAD_QUERY, &enabled);
	mutex_unlock(&priv->lock);
	if (ret)
		return ret;

	return sysfs_emit(buf, "%u\n", !!enabled);
}

static ssize_t touchpad_enabled_store(struct device *dev,
				      struct device_attribute *attr,
				      const char *buf, size_t count)
{
	struct honor_touchpad_wmi *priv = dev_get_drvdata(dev);
	bool enabled;
	int ret;

	ret = kstrtobool(buf, &enabled);
	if (ret)
		return ret;

	mutex_lock(&priv->lock);
	ret = honor_touchpad_call(priv,
			enabled ? HONOR_TOUCHPAD_ENABLE : HONOR_TOUCHPAD_DISABLE,
			NULL);
	mutex_unlock(&priv->lock);
	if (ret)
		return ret;

	return count;
}

static DEVICE_ATTR_RW(touchpad_enabled);

static struct attribute *honor_touchpad_attrs[] = {
	&dev_attr_touchpad_enabled.attr,
	NULL,
};

ATTRIBUTE_GROUPS(honor_touchpad);

static int honor_touchpad_wmi_probe(struct wmi_device *wdev,
				    const void *context)
{
	struct honor_touchpad_wmi *priv;

	if (!dmi_check_system(honor_touchpad_dmi_table))
		return -ENODEV;

	priv = devm_kzalloc(&wdev->dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	priv->wdev = wdev;
	mutex_init(&priv->lock);
	dev_set_drvdata(&wdev->dev, priv);
	dev_info(&wdev->dev, "Honor touchpad master-switch bridge ready\n");
	return 0;
}

static const struct wmi_device_id honor_touchpad_wmi_id_table[] = {
	{ HONOR_TOUCHPAD_WMI_GUID, NULL },
	{}
};

MODULE_DEVICE_TABLE(wmi, honor_touchpad_wmi_id_table);

static struct wmi_driver honor_touchpad_wmi_driver = {
	.driver = {
		.name = "honor-touchpad-wmi",
		.dev_groups = honor_touchpad_groups,
	},
	.id_table = honor_touchpad_wmi_id_table,
	.probe = honor_touchpad_wmi_probe,
};

module_wmi_driver(honor_touchpad_wmi_driver);

MODULE_AUTHOR("Honor Control reverse-engineering project");
MODULE_DESCRIPTION("Honor MRA-XXX touchpad master switch via OEM ACPI-WMI");
MODULE_LICENSE("GPL");
