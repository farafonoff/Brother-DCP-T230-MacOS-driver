/* Issue the IEEE-1284 GET_DEVICE_ID control request against each interface
 * to learn what command sets the device claims to support. */
#include <stdio.h>
#include <string.h>
#include <libusb.h>

int main(void) {
    libusb_context *ctx; libusb_init(&ctx);
    libusb_device **list; ssize_t n = libusb_get_device_list(ctx, &list);
    libusb_device_handle *h = NULL;
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        libusb_get_device_descriptor(list[i], &d);
        if (d.idVendor == 0x04F9 && d.idProduct == 0x0716) {
            libusb_open(list[i], &h); break;
        }
    }
    libusb_free_device_list(list, 1);
    if (!h) { fprintf(stderr,"not found\n"); return 1; }
    libusb_set_auto_detach_kernel_driver(h, 1);

    /* IEEE-1284 GET_DEVICE_ID:
     *   bmRequestType = 0xA1 (class, device-to-host, interface)
     *   bRequest      = 0
     *   wValue        = config (1)
     *   wIndex        = (interface << 8) | alt
     *   wLength       = up to 1024
     */
    for (int iface = 0; iface <= 2; iface++) {
        for (int alt = 0; alt <= 1; alt++) {
            unsigned char buf[1024] = {0};
            int rc = libusb_control_transfer(
                h, 0xA1, 0x00,
                /*wValue=*/0x0001,
                /*wIndex=*/(iface << 8) | alt,
                buf, sizeof(buf), 5000);
            if (rc < 2) {
                fprintf(stderr, "iface=%d alt=%d  rc=%d (%s)\n",
                    iface, alt, rc, rc < 0 ? libusb_error_name(rc) : "short");
                continue;
            }
            int len = (buf[0] << 8) | buf[1];
            if (len > rc) len = rc;
            fprintf(stderr, "iface=%d alt=%d  len=%d:\n", iface, alt, len - 2);
            fwrite(buf + 2, 1, len > 2 ? len - 2 : 0, stderr);
            fputc('\n', stderr);
            fputc('\n', stderr);
        }
    }
    libusb_close(h);
    libusb_exit(ctx);
    return 0;
}
