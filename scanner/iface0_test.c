/* Send the brscan5 lock + CKD via iface 0 (printer interface, eps 0x01/0x82).
 * Some Brother models multiplex scan commands over the printer interface. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libusb.h>

static void hexdump(const unsigned char *p, int n) {
    for (int i = 0; i < n; i += 16) {
        printf("  %04x  ", i);
        for (int j = 0; j < 16; j++)
            if (i+j < n) printf("%02x ", p[i+j]); else printf("   ");
        printf(" ");
        for (int j = 0; j < 16 && i+j < n; j++)
            putchar(p[i+j] >= 0x20 && p[i+j] < 0x7f ? p[i+j] : '.');
        putchar('\n');
    }
}

int main(int argc, char **argv) {
    int alt = (argc > 1) ? atoi(argv[1]) : 0;
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

    if (libusb_kernel_driver_active(h, 0) == 1) {
        fprintf(stderr, "iface 0 has kernel driver — detaching\n");
        libusb_detach_kernel_driver(h, 0);
    }

    int rc = libusb_claim_interface(h, 0);
    if (rc) { fprintf(stderr, "claim(0): %s\n", libusb_error_name(rc)); return 2; }
    libusb_set_interface_alt_setting(h, 0, alt);
    fprintf(stderr, "claimed iface 0 alt %d\n", alt);

    /* Lock the scan channel — the lock control transfer is recipient=device,
     * iface-independent. */
    unsigned char setup_resp[256] = {0};
    rc = libusb_control_transfer(h, 0xC0, 1, 0x0002, 0, setup_resp, 0xFF, 5000);
    fprintf(stderr, "lock ctrl xfer: rc=%d\n", rc);
    if (rc > 0) hexdump(setup_resp, rc < 16 ? rc : 16);

    /* Send CKD on iface 0 OUT (0x01), read on iface 0 IN (0x82). */
    unsigned char cmd[] = {
        0x1b, 'C','K','D', '\n',
        'P','S','R','C','=','F','B', '\n',
        0x80
    };
    int sent = 0;
    rc = libusb_bulk_transfer(h, 0x01, cmd, sizeof(cmd), &sent, 5000);
    fprintf(stderr, "bulk OUT 0x01: rc=%d sent=%d/%zu\n", rc, sent, sizeof(cmd));

    unsigned char buf[8192];
    int got = 0;
    rc = libusb_bulk_transfer(h, 0x82, buf, sizeof(buf), &got, 40000);
    fprintf(stderr, "bulk IN 0x82: rc=%d got=%d\n", rc, got);
    if (got > 0) hexdump(buf, got);

    /* Unlock */
    rc = libusb_control_transfer(h, 0xC0, 2, 0x0002, 0, setup_resp, 0xFF, 5000);
    fprintf(stderr, "unlock ctrl xfer: rc=%d\n", rc);

    libusb_release_interface(h, 0);
    libusb_close(h);
    libusb_exit(ctx);
    return got > 0 ? 0 : 3;
}
