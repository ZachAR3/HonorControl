obj-m += honor_touchpad_wmi.o

KDIR ?= /lib/modules/$(shell uname -r)/build

.PHONY: all clean install

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

install: all
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules_install
	depmod -a
