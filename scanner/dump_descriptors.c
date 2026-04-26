/* dump raw config descriptor + all interface descriptors */
#include <stdio.h>
#include <stdlib.h>
#include <libusb.h>

int main(void) {
    libusb_context *ctx; libusb_init(&ctx);
    libusb_device **list;
    ssize_t n = libusb_get_device_list(ctx, &list);
    libusb_device *target = NULL;
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        libusb_get_device_descriptor(list[i], &d);
        if (d.idVendor == 0x04F9 && d.idProduct == 0x0716) target = list[i];
    }
    if (!target) { fprintf(stderr,"not found\n"); return 1; }
    struct libusb_device_descriptor dd;
    libusb_get_device_descriptor(target, &dd);
    printf("Device: VID=%04x PID=%04x bcdDevice=%04x bcdUSB=%04x\n",
        dd.idVendor, dd.idProduct, dd.bcdDevice, dd.bcdUSB);
    printf("  bDeviceClass=%02x bDeviceSubClass=%02x bDeviceProtocol=%02x\n",
        dd.bDeviceClass, dd.bDeviceSubClass, dd.bDeviceProtocol);
    printf("  bNumConfigurations=%u\n", dd.bNumConfigurations);

    for (uint8_t ci = 0; ci < dd.bNumConfigurations; ci++) {
        struct libusb_config_descriptor *cfg;
        libusb_get_config_descriptor(target, ci, &cfg);
        printf("\nConfig %u: bNumInterfaces=%u  wTotalLength=%u  bMaxPower=%u\n",
            cfg->bConfigurationValue, cfg->bNumInterfaces, cfg->wTotalLength, cfg->MaxPower);
        for (uint8_t i = 0; i < cfg->bNumInterfaces; i++) {
            const struct libusb_interface *iface = &cfg->interface[i];
            for (int a = 0; a < iface->num_altsetting; a++) {
                const struct libusb_interface_descriptor *alt = &iface->altsetting[a];
                printf("  iface=%u alt=%u  class=%02x sub=%02x proto=%02x  bNumEndpoints=%u  iInterface=%u\n",
                    alt->bInterfaceNumber, alt->bAlternateSetting,
                    alt->bInterfaceClass, alt->bInterfaceSubClass,
                    alt->bInterfaceProtocol, alt->bNumEndpoints, alt->iInterface);
                for (uint8_t e = 0; e < alt->bNumEndpoints; e++) {
                    const struct libusb_endpoint_descriptor *ep = &alt->endpoint[e];
                    const char *type = "?";
                    switch (ep->bmAttributes & 3) {
                        case 0: type="ctrl"; break; case 1: type="iso"; break;
                        case 2: type="bulk"; break; case 3: type="intr"; break;
                    }
                    printf("    ep=0x%02x %s %s mps=%u interval=%u\n",
                        ep->bEndpointAddress,
                        (ep->bEndpointAddress & 0x80)?"IN ":"OUT",
                        type, ep->wMaxPacketSize, ep->bInterval);
                }
                if (alt->extra_length > 0) {
                    printf("    [extra %d bytes:", alt->extra_length);
                    for (int k = 0; k < alt->extra_length; k++) printf(" %02x", alt->extra[k]);
                    printf("]\n");
                }
            }
        }
        if (cfg->extra_length > 0) {
            printf("  [config extra %d bytes:", cfg->extra_length);
            for (int k = 0; k < cfg->extra_length; k++) printf(" %02x", cfg->extra[k]);
            printf("]\n");
        }
        libusb_free_config_descriptor(cfg);
    }

    /* also try to read string descriptors */
    libusb_device_handle *h;
    if (libusb_open(target, &h) == 0) {
        unsigned char s[256];
        for (uint8_t i = 1; i <= 6; i++) {
            int rc = libusb_get_string_descriptor_ascii(h, i, s, sizeof(s));
            if (rc > 0) printf("\nstring[%u] = \"%s\"\n", i, s);
        }
        libusb_close(h);
    }
    libusb_free_device_list(list, 1);
    libusb_exit(ctx);
    return 0;
}
