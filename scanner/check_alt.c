/* GET_INTERFACE asks the device which alt setting it currently has active.
 * If our SET_INTERFACE isn't reaching the firmware, this will tell us. */
#include <stdio.h>
#include <stdlib.h>
#include <libusb.h>

int main(int argc, char **argv) {
    int target_iface = (argc > 1) ? atoi(argv[1]) : 1;
    int target_alt   = (argc > 2) ? atoi(argv[2]) : 0;
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
    if (!h) { fprintf(stderr, "not found\n"); return 1; }
    libusb_set_auto_detach_kernel_driver(h, 1);

    int rc = libusb_claim_interface(h, target_iface);
    if (rc) { fprintf(stderr, "claim(%d): %s\n", target_iface, libusb_error_name(rc)); return 2; }

    /* Read current alt from device BEFORE we set it */
    unsigned char alt = 0xFF;
    rc = libusb_control_transfer(
        h, 0x81, /*GET_INTERFACE*/ 0x0A,
        0, target_iface, &alt, 1, 5000);
    fprintf(stderr, "GET_INTERFACE(iface=%d) before any set_alt: rc=%d alt=%u\n",
        target_iface, rc, alt);

    /* Now set explicitly */
    rc = libusb_set_interface_alt_setting(h, target_iface, target_alt);
    fprintf(stderr, "set_alt(iface=%d, alt=%d) returned %d\n",
        target_iface, target_alt, rc);

    /* Read again */
    rc = libusb_control_transfer(
        h, 0x81, 0x0A, 0, target_iface, &alt, 1, 5000);
    fprintf(stderr, "GET_INTERFACE(iface=%d) after set_alt(%d): rc=%d alt=%u\n",
        target_iface, target_alt, rc, alt);

    /* Try a manual SET_INTERFACE control transfer too */
    rc = libusb_control_transfer(
        h, 0x01, /*SET_INTERFACE*/ 0x0B,
        target_alt, target_iface, NULL, 0, 5000);
    fprintf(stderr, "raw SET_INTERFACE(iface=%d, alt=%d) returned %d\n",
        target_iface, target_alt, rc);

    rc = libusb_control_transfer(
        h, 0x81, 0x0A, 0, target_iface, &alt, 1, 5000);
    fprintf(stderr, "GET_INTERFACE after raw SET_INTERFACE: rc=%d alt=%u\n", rc, alt);

    libusb_release_interface(h, target_iface);
    libusb_close(h);
    libusb_exit(ctx);
    return 0;
}
