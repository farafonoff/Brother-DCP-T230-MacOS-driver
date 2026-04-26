/*
 * t230scan — minimal eSCL-over-IPP-USB scanner client for Brother DCP-T230.
 *
 * Talks raw HTTP over the device's IPP-USB bulk endpoints (USB class 7,
 * subclass 1, protocol 4). No Brother-proprietary code path is used.
 *
 * Modes:
 *   ./t230scan probe          -> dump USB descriptors (interfaces + endpoints)
 *   ./t230scan caps           -> GET /eSCL/ScannerCapabilities (XML to stdout)
 *   ./t230scan status         -> GET /eSCL/ScannerStatus
 *   ./t230scan scan FILE      -> POST a job, fetch first page, write to FILE
 *   ./t230scan proxy [PORT]   -> Listen on 127.0.0.1:PORT (default 8080) and
 *                                tunnel HTTP/1.0 + HTTP/1.1 requests onto the
 *                                IPP-USB bulk pipe. Use any HTTP client:
 *                                  curl http://127.0.0.1:8080/eSCL/ScannerCapabilities
 *
 * Build: clang -O2 -Wall -Wextra t230scan.c \
 *              -I/opt/homebrew/include/libusb-1.0 \
 *              -L/opt/homebrew/lib -lusb-1.0 -lpthread -o t230scan
 */

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>

#include <libusb.h>

#define BR_VID 0x04F9
#define BR_PID 0x0716

#define USB_CLASS_PRINTER     0x07
#define USB_SUBCLASS_PRINTER  0x01
#define USB_PROTO_IPP_USB     0x04

#define BULK_TIMEOUT_MS       30000

static int verbose = 0;

static void die(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("error: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    exit(1);
}

static void vlog(const char *fmt, ...) {
    if (!verbose) return;
    va_list ap;
    va_start(ap, fmt);
    fputs("[+] ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

/* Find an interface alt-setting matching the requested class triple and
 * collect its bulk IN/OUT endpoint addresses. */
struct ipp_iface {
    uint8_t bInterfaceNumber;
    uint8_t bAlternateSetting;
    uint8_t ep_in;
    uint8_t ep_out;
    uint16_t wMaxPacketSize_in;
    uint16_t wMaxPacketSize_out;
};

enum iface_kind { IFACE_IPP_USB, IFACE_VENDOR };

/* For VENDOR mode the caller may pass `prefer_iface_no` >= 0 to select a
 * specific interface number (the T230 has TWO vendor interfaces — iface 1
 * alt 0 and iface 2 alt 0). -1 = take the first that matches. */
static int find_iface(libusb_device *dev, enum iface_kind kind,
                      int prefer_iface_no, struct ipp_iface *out) {
    struct libusb_config_descriptor *cfg = NULL;
    if (libusb_get_active_config_descriptor(dev, &cfg) != 0) return -1;

    int found = 0;
    for (uint8_t i = 0; i < cfg->bNumInterfaces && !found; i++) {
        const struct libusb_interface *iface = &cfg->interface[i];
        for (int a = 0; a < iface->num_altsetting; a++) {
            const struct libusb_interface_descriptor *alt = &iface->altsetting[a];
            if (verbose) {
                fprintf(stderr,
                    "    iface=%u alt=%u  class=%02x sub=%02x proto=%02x  num_eps=%u\n",
                    alt->bInterfaceNumber, alt->bAlternateSetting,
                    alt->bInterfaceClass, alt->bInterfaceSubClass,
                    alt->bInterfaceProtocol, alt->bNumEndpoints);
            }
            int match = 0;
            if (kind == IFACE_IPP_USB) {
                match = (alt->bInterfaceClass    == USB_CLASS_PRINTER &&
                         alt->bInterfaceSubClass == USB_SUBCLASS_PRINTER &&
                         alt->bInterfaceProtocol == USB_PROTO_IPP_USB);
            } else { /* IFACE_VENDOR */
                match = (alt->bInterfaceClass    == 0xff &&
                         alt->bInterfaceSubClass == 0xff &&
                         alt->bInterfaceProtocol == 0xff);
                if (prefer_iface_no >= 0 &&
                    alt->bInterfaceNumber != (uint8_t)prefer_iface_no) match = 0;
            }
            if (!match) continue;

            uint8_t ep_in = 0, ep_out = 0;
            uint16_t mps_in = 0, mps_out = 0;
            for (uint8_t e = 0; e < alt->bNumEndpoints; e++) {
                const struct libusb_endpoint_descriptor *ep = &alt->endpoint[e];
                if ((ep->bmAttributes & 0x03) != LIBUSB_TRANSFER_TYPE_BULK) continue;
                if (ep->bEndpointAddress & LIBUSB_ENDPOINT_IN) {
                    ep_in = ep->bEndpointAddress;
                    mps_in = ep->wMaxPacketSize;
                } else {
                    ep_out = ep->bEndpointAddress;
                    mps_out = ep->wMaxPacketSize;
                }
            }
            if (ep_in && ep_out) {
                out->bInterfaceNumber = alt->bInterfaceNumber;
                out->bAlternateSetting = alt->bAlternateSetting;
                out->ep_in = ep_in;
                out->ep_out = ep_out;
                out->wMaxPacketSize_in = mps_in;
                out->wMaxPacketSize_out = mps_out;
                found = 1;
                break;
            }
        }
    }
    libusb_free_config_descriptor(cfg);
    return found ? 0 : -1;
}

static libusb_device_handle *open_t230(libusb_context *ctx,
                                       enum iface_kind kind,
                                       int prefer_iface_no,
                                       struct ipp_iface *iface) {
    libusb_device **list = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    if (n < 0) die("libusb_get_device_list: %s", libusb_error_name((int)n));

    libusb_device *target = NULL;
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        if (libusb_get_device_descriptor(list[i], &d) != 0) continue;
        if (d.idVendor == BR_VID && d.idProduct == BR_PID) {
            target = list[i];
            break;
        }
    }
    if (!target) {
        libusb_free_device_list(list, 1);
        die("Brother DCP-T230 (%04x:%04x) not found on USB", BR_VID, BR_PID);
    }
    if (find_iface(target, kind, prefer_iface_no, iface) != 0) {
        libusb_free_device_list(list, 1);
        die("no matching interface (%s)",
            kind == IFACE_IPP_USB ? "IPP-USB" : "vendor ff/ff/ff");
    }
    libusb_device_handle *h = NULL;
    int rc = libusb_open(target, &h);
    libusb_free_device_list(list, 1);
    if (rc != 0) die("libusb_open: %s", libusb_error_name(rc));

    libusb_set_auto_detach_kernel_driver(h, 1);

    rc = libusb_claim_interface(h, iface->bInterfaceNumber);
    if (rc != 0) die("claim_interface(%u): %s",
                     iface->bInterfaceNumber, libusb_error_name(rc));

    /* Force a real SET_INTERFACE control transfer so the firmware actually
     * sees the alt change. Some libusb backends skip the transfer when the
     * requested alt matches a cached value. We toggle to a different alt
     * (if available) and back. */
    if (kind == IFACE_VENDOR) {
        struct libusb_config_descriptor *cfg = NULL;
        if (libusb_get_active_config_descriptor(libusb_get_device(h), &cfg) == 0) {
            const struct libusb_interface *libif = &cfg->interface[iface->bInterfaceNumber];
            for (int a = 0; a < libif->num_altsetting; a++) {
                uint8_t other = libif->altsetting[a].bAlternateSetting;
                if (other != iface->bAlternateSetting) {
                    libusb_set_interface_alt_setting(h, iface->bInterfaceNumber, other);
                    break;
                }
            }
            libusb_free_config_descriptor(cfg);
        }
    }

    rc = libusb_set_interface_alt_setting(h, iface->bInterfaceNumber,
                                          iface->bAlternateSetting);
    if (rc != 0) die("set_alt(iface=%u alt=%u): %s",
                     iface->bInterfaceNumber, iface->bAlternateSetting,
                     libusb_error_name(rc));
    return h;
}

/* --- Brother vendor "scan-channel" lock/unlock ---
 * Reverse-engineered from libLxBsUsbDevAccs.so:
 *   OpenForDataTransfer  -> claim iface, then ControlScannerDevice(1)
 *   CloseForDataTransfer -> ControlScannerDevice(2), then release
 *   ControlScannerDevice -> RequestToScannerDevice(bReq=mode, buf, wLen=0xff)
 *   RequestToScannerDevice issues the libusb control transfer with
 *     bmRequestType = 0xC0 (vendor, IN, device)
 *     wValue        = 0x0002
 *     wIndex        = 0
 *     wLength       = 0xff
 *   On success, the response buffer must satisfy:
 *     buf[1] == 0x10   (echoed marker)
 *     buf[2] == bRequest (echoed mode)
 *     buf[3] == 0x02   (echoed wValue lo)
 *     buf[4] sign bit clear (no error flag)
 */
#define BR_VENDOR_LOCK    1
#define BR_VENDOR_UNLOCK  2

static int br_scan_channel_ctl(libusb_device_handle *h, uint8_t mode) {
    uint8_t buf[256];
    memset(buf, 0, sizeof(buf));
    int rc = libusb_control_transfer(
        h,
        /*bmRequestType*/ 0xC0,
        /*bRequest*/      mode,
        /*wValue*/        0x0002,
        /*wIndex*/        0x0000,
        buf, 0xFF,
        BULK_TIMEOUT_MS);
    if (rc < 0) {
        fprintf(stderr, "ControlScannerDevice(%u): %s\n", mode, libusb_error_name(rc));
        return rc;
    }
    if (verbose) {
        fprintf(stderr, "[+] ControlScannerDevice(%u) returned %d bytes:\n", mode, rc);
        for (int i = 0; i < (rc < 32 ? rc : 32); i++)
            fprintf(stderr, "%02x ", buf[i]);
        fprintf(stderr, "\n");
    }
    if (rc < 5) {
        fprintf(stderr, "warning: short response (%d bytes)\n", rc);
        return -1;
    }
    if (buf[1] != 0x10 || buf[2] != mode || buf[3] != 0x02 || (buf[4] & 0x80)) {
        fprintf(stderr, "warning: handshake mismatch buf[1..4] = %02x %02x %02x %02x (expected 10 %02x 02 [no MSB])\n",
            buf[1], buf[2], buf[3], buf[4], mode);
    }
    return 0;
}

/* --- HTTP-over-IPP-USB --- */

static int bulk_write_all(libusb_device_handle *h, uint8_t ep, const void *buf, int len) {
    int sent = 0;
    int rc = libusb_bulk_transfer(h, ep, (unsigned char *)buf, len, &sent, BULK_TIMEOUT_MS);
    if (rc != 0) die("bulk OUT: %s", libusb_error_name(rc));
    if (sent != len) die("bulk OUT short: %d/%d", sent, len);
    return sent;
}

/* Returns: >0 bytes received, 0 on a ZLP (success but empty frame),
 *          -1 on timeout, -2 on permanent error. ZLP and timeout are
 * distinct because Brother devices sometimes send a ZLP to ack/end a
 * transfer; a real "no reply" is the timeout case. */
static int bulk_read_some(libusb_device_handle *h, uint8_t ep,
                          void *buf, int max_len, int timeout_ms) {
    int got = 0;
    int rc = libusb_bulk_transfer(h, ep, (unsigned char *)buf, max_len, &got, timeout_ms);
    if (rc == 0) return got;            /* may be 0 (ZLP) or >0 */
    if (rc == LIBUSB_ERROR_TIMEOUT) return -1;
    fprintf(stderr, "bulk IN: %s (got=%d)\n", libusb_error_name(rc), got);
    return -2;
}

/* Read until headers complete; returns total bytes in *buf and the offset of
 * the body start. *buf is realloc()'d as needed; caller frees. */
struct http_resp {
    int status;
    char *headers;            /* malloc'd, NUL-terminated */
    uint8_t *body;            /* malloc'd, exactly body_len */
    long body_len;            /* may be -1 if chunked / unknown */
    long content_length;      /* parsed Content-Length, or -1 */
    int chunked;
};

static const char *find_header(const char *headers, const char *name) {
    size_t nlen = strlen(name);
    const char *p = headers;
    while (p && *p) {
        if (strncasecmp(p, name, nlen) == 0 && p[nlen] == ':') {
            p += nlen + 1;
            while (*p == ' ' || *p == '\t') p++;
            return p;
        }
        p = strchr(p, '\n');
        if (p) p++;
    }
    return NULL;
}

static long parse_long(const char *s) {
    if (!s) return -1;
    return strtol(s, NULL, 10);
}

/* Read exactly `need` bytes into buf, growing as needed. */
static long read_n(libusb_device_handle *h, uint8_t ep,
                   uint8_t **buf, long *cap, long have, long need) {
    while (have < need) {
        if (*cap - have < 4096) {
            *cap = (*cap + 4096) * 2;
            *buf = realloc(*buf, *cap);
            if (!*buf) die("oom");
        }
        int got = bulk_read_some(h, ep, *buf + have, (int)(*cap - have), BULK_TIMEOUT_MS);
        if (got == 0) break;
        have += got;
    }
    return have;
}

/* Read a full HTTP response from the device's bulk-IN pipe (assumes a request
 * has just been written and the device is about to reply). */
static struct http_resp http_recv_response(libusb_device_handle *h,
                                           const struct ipp_iface *iface) {
    long cap = 8192, have = 0;
    uint8_t *buf = malloc(cap);
    if (!buf) die("oom");
    long hdr_end = -1;
    while (1) {
        int got = bulk_read_some(h, iface->ep_in, buf + have, (int)(cap - have), BULK_TIMEOUT_MS);
        if (got <= 0) break;
        have += got;
        for (long i = 3; i < have; i++) {
            if (buf[i-3] == '\r' && buf[i-2] == '\n' &&
                buf[i-1] == '\r' && buf[i] == '\n') {
                hdr_end = i + 1;
                break;
            }
        }
        if (hdr_end >= 0) break;
        if (cap - have < 4096) {
            cap *= 2;
            buf = realloc(buf, cap);
            if (!buf) die("oom");
        }
    }
    if (hdr_end < 0) die("no end of HTTP headers in %ld bytes", have);

    struct http_resp r = {0};
    r.headers = malloc(hdr_end + 1);
    memcpy(r.headers, buf, hdr_end);
    r.headers[hdr_end] = 0;

    int hv1, hv2;
    if (sscanf(r.headers, "HTTP/%d.%d %d", &hv1, &hv2, &r.status) != 3)
        die("bad HTTP status line: %.40s", r.headers);

    const char *cl = find_header(r.headers, "Content-Length");
    r.content_length = parse_long(cl);
    const char *te = find_header(r.headers, "Transfer-Encoding");
    r.chunked = te && strncasecmp(te, "chunked", 7) == 0;

    long body_have = have - hdr_end;
    if (r.content_length >= 0) {
        have = read_n(h, iface->ep_in, &buf, &cap, have, hdr_end + r.content_length);
        body_have = have - hdr_end;
        r.body = malloc(body_have ? body_have : 1);
        memcpy(r.body, buf + hdr_end, body_have);
        r.body_len = body_have;
    } else if (r.chunked) {
        /* minimal chunked decoder */
        uint8_t *out = NULL;
        long out_len = 0, out_cap = 0;
        long pos = hdr_end;
        while (1) {
            long line_end = -1;
            while (line_end < 0) {
                for (long i = pos; i + 1 < have; i++) {
                    if (buf[i] == '\r' && buf[i+1] == '\n') { line_end = i; break; }
                }
                if (line_end >= 0) break;
                int got = bulk_read_some(h, iface->ep_in, buf + have,
                                         (int)(cap - have), BULK_TIMEOUT_MS);
                if (got <= 0) die("EOF in chunked stream");
                have += got;
                if (cap - have < 4096) { cap *= 2; buf = realloc(buf, cap); }
            }
            char numbuf[32] = {0};
            long nlen = line_end - pos;
            if (nlen >= (long)sizeof(numbuf)) nlen = sizeof(numbuf) - 1;
            memcpy(numbuf, buf + pos, nlen);
            long chunk = strtol(numbuf, NULL, 16);
            pos = line_end + 2;
            if (chunk == 0) break;
            have = read_n(h, iface->ep_in, &buf, &cap, have, pos + chunk + 2);
            if (out_len + chunk > out_cap) {
                out_cap = (out_len + chunk) * 2;
                out = realloc(out, out_cap);
            }
            memcpy(out + out_len, buf + pos, chunk);
            out_len += chunk;
            pos += chunk + 2;
        }
        r.body = out;
        r.body_len = out_len;
    } else {
        /* read until short read / timeout */
        while (1) {
            if (cap - have < 4096) { cap *= 2; buf = realloc(buf, cap); }
            int got = bulk_read_some(h, iface->ep_in, buf + have,
                                     (int)(cap - have), 2000);
            if (got <= 0) break;
            have += got;
        }
        body_have = have - hdr_end;
        r.body = malloc(body_have ? body_have : 1);
        memcpy(r.body, buf + hdr_end, body_have);
        r.body_len = body_have;
    }
    free(buf);
    return r;
}

/* Send a request whose head + body is already laid out, then read the response.
 * The proxy uses this to forward client traffic verbatim (modulo a few rewritten
 * headers). Caller must hold whatever USB mutex is in force. */
static struct http_resp http_round_trip_raw(libusb_device_handle *h,
                                            const struct ipp_iface *iface,
                                            const void *req_head, long head_len,
                                            const void *body, long body_len) {
    bulk_write_all(h, iface->ep_out, req_head, (int)head_len);
    if (body_len > 0) bulk_write_all(h, iface->ep_out, body, (int)body_len);
    return http_recv_response(h, iface);
}

/* Send an HTTP request and read the full response. */
static struct http_resp http_round_trip(libusb_device_handle *h,
                                        const struct ipp_iface *iface,
                                        const char *method,
                                        const char *path,
                                        const char *content_type,
                                        const void *body, long body_len) {
    char req[1024];
    int rl;
    if (body_len > 0) {
        rl = snprintf(req, sizeof(req),
            "%s %s HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "User-Agent: t230scan/0.1\r\n"
            "Accept: */*\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %ld\r\n"
            "Connection: close\r\n"
            "\r\n",
            method, path, content_type ? content_type : "application/octet-stream", body_len);
    } else {
        rl = snprintf(req, sizeof(req),
            "%s %s HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "User-Agent: t230scan/0.1\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n"
            "\r\n",
            method, path);
    }
    if (rl < 0 || rl >= (int)sizeof(req)) die("request too large");

    vlog("OUT  %s %s  (%d hdr bytes, %ld body bytes)", method, path, rl, body_len);
    return http_round_trip_raw(h, iface, req, rl, body, body_len);
}

static void free_resp(struct http_resp *r) {
    free(r->headers);
    free(r->body);
}

/* --- subcommands --- */

static int cmd_probe(libusb_device_handle *h, const struct ipp_iface *iface) {
    (void)h;
    fprintf(stderr,
        "Found Brother DCP-T230 IPP-USB interface\n"
        "  bInterfaceNumber  = %u\n"
        "  bAlternateSetting = %u\n"
        "  bulk IN  endpoint = 0x%02x  (mps %u)\n"
        "  bulk OUT endpoint = 0x%02x  (mps %u)\n",
        iface->bInterfaceNumber, iface->bAlternateSetting,
        iface->ep_in, iface->wMaxPacketSize_in,
        iface->ep_out, iface->wMaxPacketSize_out);
    return 0;
}

static int cmd_get(libusb_device_handle *h, const struct ipp_iface *iface,
                   const char *path) {
    struct http_resp r = http_round_trip(h, iface, "GET", path, NULL, NULL, 0);
    fprintf(stderr, "<-- HTTP %d  (%ld bytes)\n", r.status, r.body_len);
    if (verbose || r.status / 100 == 3) fputs(r.headers, stderr);
    fwrite(r.body, 1, r.body_len, stdout);
    int rc = (r.status / 100 == 2) ? 0 : 2;
    free_resp(&r);
    return rc;
}

static int cmd_scan(libusb_device_handle *h, const struct ipp_iface *iface,
                    const char *out_path) {
    static const char job_xml[] =
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<scan:ScanSettings xmlns:scan=\"http://schemas.hp.com/imaging/escl/2011/05/03\""
        " xmlns:pwg=\"http://www.pwg.org/schemas/2010/12/sm\">"
          "<pwg:Version>2.6</pwg:Version>"
          "<pwg:ScanRegions>"
            "<pwg:ScanRegion>"
              "<pwg:Height>3508</pwg:Height>"     /* A4 @ 300dpi */
              "<pwg:Width>2480</pwg:Width>"
              "<pwg:XOffset>0</pwg:XOffset>"
              "<pwg:YOffset>0</pwg:YOffset>"
              "<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>"
            "</pwg:ScanRegion>"
          "</pwg:ScanRegions>"
          "<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>"
          "<scan:ColorMode>RGB24</scan:ColorMode>"
          "<scan:XResolution>300</scan:XResolution>"
          "<scan:YResolution>300</scan:YResolution>"
          "<pwg:InputSource>Platen</pwg:InputSource>"
        "</scan:ScanSettings>";

    struct http_resp create = http_round_trip(h, iface,
        "POST", "/eSCL/ScanJobs", "application/xml; charset=UTF-8",
        job_xml, (long)(sizeof(job_xml) - 1));
    fprintf(stderr, "<-- POST /eSCL/ScanJobs HTTP %d\n", create.status);
    if (verbose) fputs(create.headers, stderr);
    if (create.status != 201) {
        fwrite(create.body, 1, create.body_len, stderr);
        free_resp(&create);
        return 2;
    }
    const char *loc = find_header(create.headers, "Location");
    if (!loc) { free_resp(&create); die("no Location header on 201"); }
    char job_path[512];
    int li = 0;
    while (loc[li] && loc[li] != '\r' && loc[li] != '\n' && li < 480) {
        job_path[li] = loc[li]; li++;
    }
    job_path[li] = 0;
    /* Location may be absolute http://host/eSCL/ScanJobs/uuid -- strip scheme/host */
    char *p = strstr(job_path, "/eSCL/");
    const char *base = p ? p : job_path;
    char next_doc[600];
    snprintf(next_doc, sizeof(next_doc), "%s/NextDocument", base);
    free_resp(&create);
    fprintf(stderr, "    job at %s\n", base);

    struct http_resp page = http_round_trip(h, iface, "GET", next_doc, NULL, NULL, 0);
    fprintf(stderr, "<-- GET %s HTTP %d  (%ld bytes)\n",
            next_doc, page.status, page.body_len);
    if (verbose) fputs(page.headers, stderr);
    if (page.status / 100 != 2) {
        fwrite(page.body, 1, page.body_len, stderr);
        free_resp(&page);
        return 2;
    }
    FILE *f = fopen(out_path, "wb");
    if (!f) { free_resp(&page); die("open %s: %s", out_path, strerror(errno)); }
    fwrite(page.body, 1, page.body_len, f);
    fclose(f);
    fprintf(stderr, "wrote %ld bytes -> %s\n", page.body_len, out_path);
    free_resp(&page);
    return 0;
}

static void hexdump(const uint8_t *p, long n, FILE *out) {
    for (long i = 0; i < n; i += 16) {
        fprintf(out, "  %06lx  ", i);
        for (long j = 0; j < 16; j++) {
            if (i+j < n) fprintf(out, "%02x ", p[i+j]);
            else         fputs("   ", out);
        }
        fputc(' ', out);
        for (long j = 0; j < 16 && i+j < n; j++) {
            uint8_t c = p[i+j];
            fputc((c >= 0x20 && c < 0x7f) ? c : '.', out);
        }
        fputc('\n', out);
    }
}

/* Send `ESC + cmd_name + LF + body + 0x80` on the vendor bulk pipe and
 * read the reply (handling Brother's ZLP keep-alives). out_buf is filled
 * with up to *out_len bytes; on return *out_len is the actual count.
 * Returns 0 on a non-empty reply, 1 on no data within deadline. */
static int br_round_trip(libusb_device_handle *h,
                         const struct ipp_iface *iface,
                         const char *cmd_name,
                         const char *body,
                         uint8_t *out_buf, size_t *out_len,
                         long deadline_ms) {
    uint8_t pkt[2048];
    int n = 0;
    pkt[n++] = 0x1B;
    for (const char *p = cmd_name; *p; p++) pkt[n++] = (uint8_t)*p;
    pkt[n++] = 0x0A;
    if (body) {
        size_t blen = strlen(body);
        if (n + (int)blen + 1 > (int)sizeof(pkt)) die("body too large");
        memcpy(pkt + n, body, blen);
        n += (int)blen;
    }
    pkt[n++] = 0x80;
    if (verbose) {
        fprintf(stderr, "OUT  %s (%d bytes):\n", cmd_name, n);
        hexdump(pkt, n, stderr);
    }
    bulk_write_all(h, iface->ep_out, pkt, n);

    size_t cap = *out_len;
    *out_len = 0;
    int zlps = 0;
    long elapsed = 0;
    while (elapsed < deadline_ms) {
        int got = bulk_read_some(h, iface->ep_in, out_buf + *out_len,
                                 (int)(cap - *out_len), 1000);
        if (got < -1) return 2;
        if (got == -1) { elapsed += 1000; continue; }
        if (got == 0) {
            zlps++;
            usleep(10 * 1000);
            elapsed += 10;
            continue;
        }
        *out_len += (size_t)got;
        if ((size_t)got < cap - *out_len + got) break;  /* short -> done */
        if (*out_len >= cap) break;
    }
    if (verbose) {
        fprintf(stderr, "IN   %s -> %zu bytes (after %d ZLPs):\n",
            cmd_name, *out_len, zlps);
        hexdump(out_buf, *out_len, stderr);
    }
    return *out_len > 0 ? 0 : 1;
}

/* End-to-end scan: lock + CKD + SSP + XSC + read image stream. Writes raw
 * payload bytes (whatever the device emits) to `out_path`. Color/grayscale
 * and resolution are caller-controlled; AREA is auto-derived from the
 * resolution to cover an A4 page (8.27" x 11.69"). */
static int cmd_scan_vendor(libusb_device_handle *h,
                           const struct ipp_iface *iface,
                           const char *out_path,
                           int dpi,
                           const char *color_mode /* GRAY256 / C24BIT */) {
    /* CKD — confirm flatbed */
    uint8_t buf[16384];
    size_t blen = sizeof(buf);
    int rc = br_round_trip(h, iface, "CKD", "PSRC=FB\n", buf, &blen, 35000);
    if (rc != 0 || blen < 1) {
        fprintf(stderr, "CKD: no reply\n");
        return 2;
    }
    fprintf(stderr, "CKD reply: %02x %02x  (status=%02x doc=%02x)\n",
        buf[0], blen > 1 ? buf[1] : 0xff, buf[0], blen > 1 ? buf[1] : 0xff);
    if (buf[0] != 0x00) {
        fprintf(stderr, "CKD reported error 0x%02x — is the device ready?\n", buf[0]);
        return 2;
    }

    /* SSP — set scan parameters. AREA is in pixel-units at the requested
     * DPI (so the actual physical region matches A4 regardless of DPI). */
    char ssp_body[512];
    int max_x = (int)(8.27 * dpi);
    int max_y = (int)(11.69 * dpi);
    snprintf(ssp_body, sizeof(ssp_body),
        "OS=LNX\n"
        "PSRC=FB\n"
        "RESO=%d,%d\n"
        "CLR=%s\n"
        "AREA=0,0,%d,%d\n",
        dpi, dpi, color_mode, max_x, max_y);
    blen = sizeof(buf);
    rc = br_round_trip(h, iface, "SSP", ssp_body, buf, &blen, 35000);
    if (rc != 0 || blen < 4) {
        fprintf(stderr, "SSP: short or no reply\n"); return 2;
    }
    if (buf[0] != 0x00 || memcmp(buf + 1, "SSP", 3) != 0) {
        fprintf(stderr, "SSP unexpected: %02x %.3s ...\n", buf[0], (char*)buf+1);
        return 2;
    }
    fprintf(stderr, "SSP accepted (%zu byte capability descriptor)\n", blen);

    /* XSC — execute scan. Body reuses RESO/AREA. The device responds with
     * a stream of <status_byte> + payload chunks, each segment terminated
     * by short reads. We capture everything into out_path until the device
     * sends a "page-end" status (the disasm enumerates several values). */
    char xsc_body[256];
    snprintf(xsc_body, sizeof(xsc_body),
        "RESO=%d,%d\nAREA=0,0,%d,%d\n",
        dpi, dpi, max_x, max_y);
    /* Build the XSC packet manually so we can stream the response. */
    uint8_t pkt[256]; int n = 0;
    pkt[n++] = 0x1B; pkt[n++]='X'; pkt[n++]='S'; pkt[n++]='C'; pkt[n++]='\n';
    size_t bl = strlen(xsc_body);
    memcpy(pkt + n, xsc_body, bl); n += (int)bl;
    pkt[n++] = 0x80;
    fprintf(stderr, "OUT  XSC (%d bytes)\n", n);
    if (verbose) hexdump(pkt, n, stderr);
    bulk_write_all(h, iface->ep_out, pkt, n);

    /* Stream image data into an in-memory buffer. Brother sends ONE status
     * byte at the very start of the XSC reply, then a continuous data
     * stream split across USB bulk packets — NOT a status byte per packet.
     * Stripping per-packet was corrupting the JPEG. Save everything raw
     * (we extract the JPEG by FF D8 / FF D9 markers below). The
     * end-of-page is detected by an idle period + short final read,
     * since the page-end status byte is part of the byte stream and
     * only meaningful at message boundaries (not at every 16 KB chunk). */
    size_t cap_buf = 512 * 1024 * 1024;   /* up to ~half a GB raw */
    uint8_t *raw = malloc(cap_buf);
    if (!raw) die("oom");
    size_t raw_len = 0;
    /* End-of-page detection:
     *   - sustained idle (~3s with no bytes) once we've already received
     *     some data,
     *   - or absolute deadline of 60s for the whole page.
     *
     * Brother streams the page as many short bulk reads (often <16 KB
     * each) interleaved with ZLPs while the firmware processes the next
     * band. Stopping on a single short read corrupted the JPEG by
     * truncating the page mid-stream. */
    long since_last_ms = 0;
    const long deadline_total_ms = 900000;     /* 15 min hard cap */
    long idle_after_data_ms_local = 8000;      /* idle wait before trailer seen */
    size_t saw_trailer_at = (size_t)-1;
    long elapsed_ms = 0;
    while (elapsed_ms < deadline_total_ms) {
        if (raw_len >= cap_buf) { fprintf(stderr, "buffer full\n"); break; }
        int want = (int)(cap_buf - raw_len);
        if (want > (int)sizeof(buf)) want = (int)sizeof(buf);
        int got = bulk_read_some(h, iface->ep_in, buf, want, 1000);
        if (got < -1) break;
        if (got == -1) {
            elapsed_ms += 1000; since_last_ms += 1000;
            if (raw_len > 0 && since_last_ms >= idle_after_data_ms_local) break;
            continue;
        }
        if (got == 0) {
            elapsed_ms += 10; since_last_ms += 10;
            usleep(10 * 1000);
            if (raw_len > 0 && since_last_ms >= idle_after_data_ms_local) break;
            continue;
        }
        if (verbose) {
            fprintf(stderr, "  chunk %d bytes  (raw_len now %zu)  first8: %02x %02x %02x %02x %02x %02x %02x %02x\n",
                got, raw_len + got,
                buf[0], buf[1], buf[2], buf[3],
                buf[4], buf[5], buf[6], buf[7]);
        }
        memcpy(raw + raw_len, buf, (size_t)got);
        raw_len += (size_t)got;
        since_last_ms = 0;
        if (!verbose && raw_len % (256 * 1024) < (size_t)got)
            fprintf(stderr, "  ... %zu KB\n", raw_len / 1024);

        /* Note the trailer position when we see it (so we can trim it
         * from the JPEG buffer) but DON'T stop reading — we let the device
         * naturally drain its bulk-IN queue. After the trailer the
         * firmware sends a small tail packet (~2 bytes) and then goes
         * idle; the idle-after-data timeout is what ends the loop.
         * Stopping early at the trailer left residual bytes that put the
         * firmware into a bad state for the next scan (SSP returned 0xb0). */
        if (saw_trailer_at == (size_t)-1 && raw_len >= 4) {
            size_t scan_from = raw_len > 4096 ? raw_len - 4096 : 0;
            for (size_t i = scan_from; i + 4 <= raw_len; i++) {
                if (raw[i]   == 0x00 && raw[i+1] == 0x21 &&
                    raw[i+2] == 0x01 && raw[i+3] == 0x00) {
                    saw_trailer_at = i;
                    /* shrink the idle timeout so we exit as soon as the
                     * device stops sending */
                    idle_after_data_ms_local = 1500;
                    break;
                }
            }
        }
    }
    /* Trim trailer from the JPEG buffer. */
    if (saw_trailer_at != (size_t)-1 && saw_trailer_at < raw_len) {
        if (verbose) fprintf(stderr, "trimming %zu trailer bytes\n",
            raw_len - saw_trailer_at);
        raw_len = saw_trailer_at;
    }
    fprintf(stderr, "captured %zu raw bytes from XSC stream\n", raw_len);

    /* Optional: dump the raw stream untouched for debugging. */
    if (getenv("T230_DUMP_RAW")) {
        const char *path = getenv("T230_DUMP_RAW");
        FILE *r = fopen(path, "wb");
        if (r) { fwrite(raw, 1, raw_len, r); fclose(r);
            fprintf(stderr, "dumped raw stream -> %s\n", path); }
    }

    /* Brother sends the page as a sequence of fixed-size 65536-byte "bands",
     * each prefixed by a 14-byte band header (status byte + 13 metadata
     * bytes). The last band is shorter. We need to strip every band header
     * before reassembling the JPEG payload — otherwise those header bytes
     * are interpreted as JPEG entropy data and the page goes gray after
     * the first band. */
    const size_t BAND_TOTAL = 65536;     /* fixed for v2.x mini21$PHNX$ family */
    const size_t BAND_HDR   = 14;
    uint8_t *jpeg_buf = malloc(raw_len);
    if (!jpeg_buf) { free(raw); die("oom"); }
    size_t jpeg_len = 0;
    size_t pos = 0;
    int bands_seen = 0;
    while (pos < raw_len) {
        size_t band_size = (raw_len - pos > BAND_TOTAL) ? BAND_TOTAL : (raw_len - pos);
        if (band_size <= BAND_HDR) {
            /* trailing scrap < 14 bytes — skip it */
            break;
        }
        /* Band header always starts with 00 02 01 00; bytes 4-5 vary by
         * color mode (grayscale: 11 00, 24-bit color: 15 00, etc.). */
        if (raw[pos] == 0x00 && raw[pos+1] == 0x02 &&
            raw[pos+2] == 0x01 && raw[pos+3] == 0x00) {
            memcpy(jpeg_buf + jpeg_len, raw + pos + BAND_HDR, band_size - BAND_HDR);
            jpeg_len += band_size - BAND_HDR;
            bands_seen++;
        } else {
            fprintf(stderr, "warning: band at offset %zu lacks expected header "
                "(got %02x %02x %02x %02x %02x %02x), keeping raw\n",
                pos, raw[pos], raw[pos+1], raw[pos+2],
                raw[pos+3], raw[pos+4], raw[pos+5]);
            memcpy(jpeg_buf + jpeg_len, raw + pos, band_size);
            jpeg_len += band_size;
        }
        pos += band_size;
    }
    fprintf(stderr, "stripped %d band headers; reassembled %zu JPEG bytes\n",
        bands_seen, jpeg_len);

    /* The reassembled buffer should already be a valid baseline JPEG from
     * the first SOI to the last EOI; trim leading/trailing junk just in
     * case (some bands may end with a few stuffing bytes after EOI). */
    long soi = -1, eoi = -1;
    for (size_t i = 0; i + 1 < jpeg_len; i++)
        if (jpeg_buf[i] == 0xFF && jpeg_buf[i+1] == 0xD8) { soi = (long)i; break; }
    for (size_t i = jpeg_len; i >= 2; i--)
        if (jpeg_buf[i-2] == 0xFF && jpeg_buf[i-1] == 0xD9) { eoi = (long)i; break; }

    FILE *out = fopen(out_path, "wb");
    if (!out) { free(raw); free(jpeg_buf); die("open %s: %s", out_path, strerror(errno)); }
    long written;
    if (soi >= 0 && eoi > soi) {
        written = eoi - soi;
        fwrite(jpeg_buf + soi, 1, (size_t)written, out);
        fprintf(stderr, "JPEG (%ld bytes) -> %s\n", written, out_path);
    } else {
        written = (long)jpeg_len;
        fwrite(jpeg_buf, 1, jpeg_len, out);
        fprintf(stderr, "no JPEG markers; wrote raw reassembled payload (%ld bytes) -> %s\n",
            written, out_path);
    }
    fclose(out);
    free(jpeg_buf);
    free(raw);
    return written > 0 ? 0 : 2;
}

/* Legacy single-shot probe wrapper for backwards compat with older
 * subcommands (lock/ckd/ssp/xsc/qdi/q/esc/vprobe). */
static int cmd_vendor_probe_body(libusb_device_handle *h,
                                 const struct ipp_iface *iface,
                                 const char *cmd_name,
                                 const char *body) {
    /* brscan5 never calls clear_halt; it just claims, set-alts (sometimes),
     * locks, and writes. We deliberately do NOT drain bulk IN here because
     * some devices queue an unsolicited "ready" frame after lock; we want
     * to see whatever that is. */
    uint8_t pkt[1024];
    int n = 0;
    pkt[n++] = 0x1B;
    for (const char *p = cmd_name; *p; p++) pkt[n++] = (uint8_t)*p;
    pkt[n++] = 0x0A;
    if (body) {
        size_t blen = strlen(body);
        if (n + (int)blen + 1 > (int)sizeof(pkt)) die("body too large");
        memcpy(pkt + n, body, blen);
        n += (int)blen;
    }
    pkt[n++] = 0x80;

    fprintf(stderr, "OUT  ESC+%s+LF%s+0x80  (%d bytes) on ep 0x%02x\n",
        cmd_name, body && *body ? "+body" : "", n, iface->ep_out);
    if (verbose) hexdump(pkt, n, stderr);
    bulk_write_all(h, iface->ep_out, pkt, n);

    /* brscan5's ReceiveData retries on ZLPs with 10ms sleep, up to total
     * timeout (~35s for CKD). Mirror that exactly: keep polling until we
     * either get real data, time out total, or see a permanent error. */
    uint8_t buf[8192];
    long total = 0;
    int zlps = 0;
    const long deadline_ms = 35000;
    long elapsed_ms = 0;
    while (elapsed_ms < deadline_ms) {
        int got = bulk_read_some(h, iface->ep_in, buf, sizeof(buf), 1000);
        if (got < -1) break;          /* permanent error */
        if (got == -1) {              /* libusb timeout */
            elapsed_ms += 1000;
            continue;
        }
        if (got == 0) {               /* ZLP — device says "nothing yet" */
            zlps++;
            if (zlps == 1 || zlps % 50 == 0)
                fprintf(stderr, "IN   ZLP on ep 0x%02x (count=%d)\n",
                    iface->ep_in, zlps);
            usleep(10 * 1000);        /* match brscan5's 10ms backoff */
            elapsed_ms += 10;
            continue;
        }
        fprintf(stderr, "IN   %d bytes on ep 0x%02x (after %d ZLPs)\n",
            got, iface->ep_in, zlps);
        hexdump(buf, got, stderr);
        fwrite(buf, 1, got, stdout);
        total += got;
        if ((size_t)got < sizeof(buf)) break;   /* short non-empty -> done */
        zlps = 0;
    }
    if (total == 0)
        fprintf(stderr, "(no data after %ld ms / %d ZLPs)\n", elapsed_ms, zlps);
    else
        fprintf(stderr, "(read %ld bytes total)\n", total);
    return total > 0 ? 0 : 2;
}

/* --- HTTP/1.x proxy ---------------------------------------------------------
 *
 * Listen on a local TCP port; for every accepted connection, read HTTP/1.0 or
 * HTTP/1.1 requests and forward each one onto the IPP-USB pipe via
 * http_round_trip_raw(). The bulk endpoints are a single shared resource, so
 * all USB I/O is serialized on g_usb_lock — multiple TCP clients are fine,
 * they just take turns on the device.
 *
 * Per-request behaviour:
 *   - request line + most headers are forwarded verbatim, minus the
 *     hop-by-hop set (Connection, Keep-Alive, TE, Trailers, Transfer-Encoding,
 *     Upgrade, Proxy-{Authenticate,Authorization,Connection}, Host, and
 *     Content-Length, which we recompute);
 *   - chunked request bodies are de-chunked before being shipped across USB
 *     (the device only speaks fixed-length);
 *   - response headers are forwarded back, minus the same hop-by-hop set, and
 *     we always emit our own Content-Length (we already buffered the body) and
 *     Connection;
 *   - HTTP/1.1 keep-alive is honoured: a connection stays open until the
 *     client sends Connection: close, the request was HTTP/1.0 without
 *     Connection: keep-alive, or the socket goes away.
 *
 * Failures (USB hiccup, malformed requests, etc.) close just the affected TCP
 * connection — but a hard USB error still calls die() in the helpers below
 * and tears down the whole proxy. That's a known sharp edge; restart the
 * proxy if the device gets unplugged.
 */

static pthread_mutex_t g_usb_lock = PTHREAD_MUTEX_INITIALIZER;

struct proxy_ctx {
    int fd;
    libusb_device_handle *h;
    struct ipp_iface iface;
    char peer[64];
};

static const char *const proxy_req_drop[] = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
    "host", "content-length", NULL,
};
static const char *const proxy_resp_drop[] = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
    "content-length", NULL,
};

static int hdr_in_set(const char *name, size_t nlen, const char *const *set) {
    for (; *set; set++) {
        size_t l = strlen(*set);
        if (l == nlen && strncasecmp(name, *set, l) == 0) return 1;
    }
    return 0;
}

static int sock_read_full(int fd, void *buf, size_t n) {
    uint8_t *p = buf;
    while (n) {
        ssize_t r = read(fd, p, n);
        if (r < 0) { if (errno == EINTR) continue; return -1; }
        if (r == 0) return -1;
        p += r; n -= r;
    }
    return 0;
}

static int sock_write_full(int fd, const void *buf, size_t n) {
    const uint8_t *p = buf;
    while (n) {
        ssize_t w = write(fd, p, n);
        if (w < 0) { if (errno == EINTR) continue; return -1; }
        p += w; n -= w;
    }
    return 0;
}

/* Read from the client socket until the request head terminator (CRLF CRLF)
 * is found. *buf is realloc'd as needed; on success *head_end is the offset
 * just past the empty line (so any pre-fetched body bytes live in
 * [*head_end .. *have)). Returns 0 on success, -1 on I/O / oversize error,
 * -2 on a clean EOF before any byte was read (idle keep-alive close). */
static int proxy_read_head(int fd, uint8_t **buf, size_t *cap, size_t *have,
                           size_t *head_end) {
    *cap = 4096;
    *have = 0;
    *head_end = 0;
    *buf = malloc(*cap);
    if (!*buf) return -1;
    while (1) {
        if (*cap - *have < 512) {
            if (*cap >= (size_t)1 << 20) { free(*buf); *buf = NULL; return -1; }
            size_t ncap = *cap * 2;
            uint8_t *nb = realloc(*buf, ncap);
            if (!nb) { free(*buf); *buf = NULL; return -1; }
            *buf = nb; *cap = ncap;
        }
        ssize_t r = read(fd, *buf + *have, *cap - *have);
        if (r < 0) {
            if (errno == EINTR) continue;
            free(*buf); *buf = NULL; return -1;
        }
        if (r == 0) {
            if (*have == 0) { free(*buf); *buf = NULL; return -2; }
            free(*buf); *buf = NULL; return -1;
        }
        size_t scan_from = (*have >= 3) ? (*have - 3) : 0;
        *have += (size_t)r;
        for (size_t i = scan_from; i + 4 <= *have; i++) {
            if ((*buf)[i] == '\r' && (*buf)[i+1] == '\n' &&
                (*buf)[i+2] == '\r' && (*buf)[i+3] == '\n') {
                *head_end = i + 4;
                return 0;
            }
        }
    }
}

struct req_meta {
    char method[16];
    char path[1024];
    int http_minor;        /* 0 or 1 */
    long content_length;   /* -1 if absent */
    int chunked;
    int conn_close;
    int expect_100;        /* client sent "Expect: 100-continue" */
};

/* Parse the request head (bytes [0..head_end), terminated with CRLF CRLF) and
 * build a CRLF-separated list of forwardable headers (hop-by-hop dropped). */
static int proxy_parse_req(const uint8_t *raw, size_t head_end,
                           struct req_meta *m,
                           char **fwd_out, size_t *fwd_len) {
    const char *p = (const char *)raw;
    const char *end = (const char *)raw + head_end - 2; /* drop trailing CRLF */

    const char *sp1 = memchr(p, ' ', end - p);
    if (!sp1 || (size_t)(sp1 - p) >= sizeof(m->method)) return -1;
    memcpy(m->method, p, sp1 - p); m->method[sp1 - p] = 0;
    p = sp1 + 1;
    const char *sp2 = memchr(p, ' ', end - p);
    if (!sp2 || (size_t)(sp2 - p) >= sizeof(m->path)) return -1;
    memcpy(m->path, p, sp2 - p); m->path[sp2 - p] = 0;
    p = sp2 + 1;
    if (end - p < 8 || memcmp(p, "HTTP/1.", 7) != 0) return -1;
    m->http_minor = (p[7] == '1') ? 1 : 0;
    const char *eol = memmem(p, end - p, "\r\n", 2);
    if (!eol) return -1;
    p = eol + 2;

    m->content_length = -1;
    m->chunked = 0;
    m->conn_close = (m->http_minor == 0); /* HTTP/1.0 default = close */
    m->expect_100 = 0;

    size_t cap = 1024;
    char *fwd = malloc(cap);
    if (!fwd) return -1;
    size_t flen = 0;

    while (p < end) {
        const char *le = memmem(p, end - p, "\r\n", 2);
        if (!le) le = end;
        if (le == p) break;
        const char *colon = memchr(p, ':', le - p);
        if (colon) {
            size_t nlen = (size_t)(colon - p);
            const char *vp = colon + 1;
            while (vp < le && (*vp == ' ' || *vp == '\t')) vp++;
            size_t vlen = (size_t)(le - vp);

            if (nlen == 14 && strncasecmp(p, "Content-Length", 14) == 0) {
                char nb[32] = {0};
                size_t n = vlen < 31 ? vlen : 31;
                memcpy(nb, vp, n);
                m->content_length = strtol(nb, NULL, 10);
            } else if (nlen == 17 && strncasecmp(p, "Transfer-Encoding", 17) == 0) {
                /* Per RFC 7230 only "chunked" matters here; if the client used
                 * something exotic we'll just pass it on and let the device
                 * (and likely failure) sort it out. */
                if (vlen >= 7 && memmem(vp, vlen, "chunked", 7)) m->chunked = 1;
            } else if (nlen == 10 && strncasecmp(p, "Connection", 10) == 0) {
                if (memmem(vp, vlen, "close", 5)) m->conn_close = 1;
                else if (memmem(vp, vlen, "keep-alive", 10)) m->conn_close = 0;
            } else if (nlen == 6 && strncasecmp(p, "Expect", 6) == 0) {
                if (memmem(vp, vlen, "100-continue", 12)) m->expect_100 = 1;
            }

            if (!hdr_in_set(p, nlen, proxy_req_drop)) {
                size_t need = (size_t)(le - p) + 2;
                while (flen + need > cap) cap *= 2;
                char *nb = realloc(fwd, cap);
                if (!nb) { free(fwd); return -1; }
                fwd = nb;
                memcpy(fwd + flen, p, le - p);
                flen += (size_t)(le - p);
                memcpy(fwd + flen, "\r\n", 2);
                flen += 2;
            }
        }
        p = le + 2;
        if (p > end) break;
    }
    *fwd_out = fwd;
    *fwd_len = flen;
    return 0;
}

/* Read the request body off the socket given an already-fetched seed buffer
 * (the over-read tail of the header read). Either sucks down `content_length`
 * bytes verbatim, dechunks a chunked stream, or returns an empty body. */
static int proxy_read_body(int fd, const uint8_t *seed, size_t seed_len,
                           long content_length, int chunked,
                           uint8_t **body_out, long *body_len_out) {
    if (chunked) {
        size_t raw_cap = seed_len + 4096;
        if (raw_cap < 4096) raw_cap = 4096;
        uint8_t *raw = malloc(raw_cap);
        if (!raw) return -1;
        memcpy(raw, seed, seed_len);
        size_t raw_have = seed_len;

        uint8_t *out = NULL;
        long out_len = 0, out_cap = 0;
        size_t pos = 0;

        for (;;) {
            ssize_t le = -1;
            while (le < 0) {
                for (size_t i = pos; i + 1 < raw_have; i++) {
                    if (raw[i] == '\r' && raw[i+1] == '\n') { le = (ssize_t)i; break; }
                }
                if (le >= 0) break;
                if (raw_cap - raw_have < 4096) {
                    raw_cap *= 2;
                    uint8_t *n = realloc(raw, raw_cap);
                    if (!n) { free(raw); free(out); return -1; }
                    raw = n;
                }
                ssize_t r = read(fd, raw + raw_have, raw_cap - raw_have);
                if (r <= 0) { free(raw); free(out); return -1; }
                raw_have += (size_t)r;
            }
            char nb[32] = {0};
            size_t nlen = (size_t)le - pos;
            if (nlen >= sizeof(nb)) { free(raw); free(out); return -1; }
            memcpy(nb, raw + pos, nlen);
            long chunk = strtol(nb, NULL, 16);
            pos = (size_t)le + 2;
            if (chunk < 0) { free(raw); free(out); return -1; }
            if (chunk == 0) {
                /* Drain trailing CRLF (and any trailers — we discard them). */
                while (pos + 2 > raw_have) {
                    if (raw_cap - raw_have < 64) {
                        raw_cap *= 2;
                        uint8_t *n = realloc(raw, raw_cap);
                        if (!n) { free(raw); free(out); return -1; }
                        raw = n;
                    }
                    ssize_t r = read(fd, raw + raw_have, raw_cap - raw_have);
                    if (r <= 0) break;
                    raw_have += (size_t)r;
                }
                break;
            }
            while (pos + chunk + 2 > raw_have) {
                if (raw_cap - raw_have < (size_t)chunk + 4096) {
                    raw_cap = (pos + chunk + 4096) * 2;
                    uint8_t *n = realloc(raw, raw_cap);
                    if (!n) { free(raw); free(out); return -1; }
                    raw = n;
                }
                ssize_t r = read(fd, raw + raw_have, raw_cap - raw_have);
                if (r <= 0) { free(raw); free(out); return -1; }
                raw_have += (size_t)r;
            }
            if (out_len + chunk > out_cap) {
                out_cap = (out_len + chunk) * 2;
                uint8_t *n = realloc(out, out_cap);
                if (!n) { free(raw); free(out); return -1; }
                out = n;
            }
            memcpy(out + out_len, raw + pos, chunk);
            out_len += chunk;
            pos += chunk + 2;
        }
        free(raw);
        *body_out = out;
        *body_len_out = out_len;
        return 0;
    }
    if (content_length > 0) {
        uint8_t *body = malloc(content_length);
        if (!body) return -1;
        long have = (long)seed_len;
        if (have > content_length) have = content_length;
        memcpy(body, seed, (size_t)have);
        if (have < content_length) {
            if (sock_read_full(fd, body + have, content_length - have) < 0) {
                free(body); return -1;
            }
        }
        *body_out = body;
        *body_len_out = content_length;
        return 0;
    }
    *body_out = NULL;
    *body_len_out = 0;
    return 0;
}

/* Forward one request through the USB pipe. Holds g_usb_lock for the whole
 * round-trip so simultaneous TCP clients can't interleave on the bulk
 * endpoints. */
static struct http_resp proxy_dispatch(libusb_device_handle *h,
                                       const struct ipp_iface *iface,
                                       const char *method, const char *path,
                                       const char *fwd, size_t fwd_len,
                                       const uint8_t *body, long body_len) {
    size_t cap = fwd_len + 256 + strlen(method) + strlen(path);
    char *req = malloc(cap);
    if (!req) die("oom");
    int n = snprintf(req, cap, "%s %s HTTP/1.1\r\n", method, path);
    memcpy(req + n, fwd, fwd_len);
    n += (int)fwd_len;
    if (body_len > 0) {
        n += snprintf(req + n, cap - n,
                      "Host: localhost\r\n"
                      "Content-Length: %ld\r\n"
                      "Connection: close\r\n"
                      "\r\n", body_len);
    } else {
        n += snprintf(req + n, cap - n,
                      "Host: localhost\r\n"
                      "Connection: close\r\n"
                      "\r\n");
    }

    vlog("[proxy] -> USB %s %s (%d hdr bytes, %ld body bytes)",
         method, path, n, body_len);

    pthread_mutex_lock(&g_usb_lock);
    struct http_resp r = http_round_trip_raw(h, iface, req, n, body, body_len);
    pthread_mutex_unlock(&g_usb_lock);

    free(req);
    return r;
}

/* Write the response back to the client. We re-emit the status line at the
 * client's HTTP minor version, copy through end-to-end response headers, and
 * always set our own Content-Length (we have the buffered body) and
 * Connection. */
static int proxy_write_response(int fd, const struct http_resp *r,
                                int http_minor, int keep_alive) {
    const char *eol = strstr(r->headers, "\r\n");
    if (!eol) return -1;
    char first[256];
    size_t fl = (size_t)(eol - r->headers);
    if (fl >= sizeof(first)) fl = sizeof(first) - 1;
    memcpy(first, r->headers, fl); first[fl] = 0;

    int status = r->status;
    const char *reason = "";
    const char *sp1 = strchr(first, ' ');
    if (sp1) {
        const char *sp2 = strchr(sp1 + 1, ' ');
        if (sp2) reason = sp2 + 1;
    }

    char outline[320];
    int oln = snprintf(outline, sizeof(outline), "HTTP/1.%d %d %s\r\n",
                       http_minor, status, *reason ? reason : "OK");
    if (sock_write_full(fd, outline, oln) < 0) return -1;

    const char *p = eol + 2;
    while (*p) {
        const char *le = strstr(p, "\r\n");
        if (!le || le == p) break;
        const char *colon = memchr(p, ':', le - p);
        if (colon) {
            size_t nlen = (size_t)(colon - p);
            if (!hdr_in_set(p, nlen, proxy_resp_drop)) {
                if (sock_write_full(fd, p, le - p) < 0) return -1;
                if (sock_write_full(fd, "\r\n", 2) < 0) return -1;
            }
        }
        p = le + 2;
    }

    char tail[160];
    int tl = snprintf(tail, sizeof(tail),
                      "Content-Length: %ld\r\n"
                      "Connection: %s\r\n"
                      "\r\n",
                      r->body_len, keep_alive ? "keep-alive" : "close");
    if (sock_write_full(fd, tail, tl) < 0) return -1;

    if (r->body_len > 0) {
        if (sock_write_full(fd, r->body, r->body_len) < 0) return -1;
    }
    return 0;
}

static int proxy_send_simple(int fd, int http_minor, int status, const char *reason) {
    char buf[256];
    int n = snprintf(buf, sizeof(buf),
                     "HTTP/1.%d %d %s\r\n"
                     "Content-Length: 0\r\n"
                     "Connection: close\r\n\r\n",
                     http_minor, status, reason);
    return sock_write_full(fd, buf, n);
}

static void *proxy_thread_main(void *arg) {
    struct proxy_ctx *c = arg;
    int fd = c->fd;
    libusb_device_handle *h = c->h;
    const struct ipp_iface *iface = &c->iface;

    for (;;) {
        uint8_t *raw = NULL;
        size_t cap = 0, have = 0, head_end = 0;
        int rc = proxy_read_head(fd, &raw, &cap, &have, &head_end);
        if (rc != 0) {
            free(raw);
            break;
        }

        struct req_meta m;
        char *fwd = NULL;
        size_t fwd_len = 0;
        if (proxy_parse_req(raw, head_end, &m, &fwd, &fwd_len) < 0) {
            proxy_send_simple(fd, 1, 400, "Bad Request");
            free(raw); free(fwd);
            break;
        }

        if (m.expect_100) {
            /* We always read+forward, so just satisfy the precondition. */
            const char *cont = "HTTP/1.1 100 Continue\r\n\r\n";
            if (sock_write_full(fd, cont, strlen(cont)) < 0) {
                free(raw); free(fwd);
                break;
            }
        }

        uint8_t *body = NULL;
        long body_len = 0;
        if (proxy_read_body(fd, raw + head_end, have - head_end,
                            m.content_length, m.chunked,
                            &body, &body_len) < 0) {
            proxy_send_simple(fd, m.http_minor, 400, "Bad Request");
            free(raw); free(fwd); free(body);
            break;
        }
        free(raw);

        if (verbose) {
            fprintf(stderr, "[proxy %s] %s %s HTTP/1.%d  body=%ld\n",
                    c->peer, m.method, m.path, m.http_minor, body_len);
        }

        struct http_resp r = proxy_dispatch(h, iface, m.method, m.path,
                                            fwd, fwd_len, body, body_len);
        free(fwd);
        free(body);

        int keep_alive = !m.conn_close && m.http_minor >= 1;
        int wrc = proxy_write_response(fd, &r, m.http_minor, keep_alive);

        if (verbose) {
            fprintf(stderr, "[proxy %s] <- %d  hdr=%zu body=%ld\n",
                    c->peer, r.status, strlen(r.headers), r.body_len);
        }

        free_resp(&r);

        if (wrc < 0 || !keep_alive) break;
    }

    close(fd);
    free(c);
    return NULL;
}

static int cmd_proxy(libusb_device_handle *h, const struct ipp_iface *iface, int port) {
    /* Don't kill the proxy when a client hangs up mid-write. */
    signal(SIGPIPE, SIG_IGN);

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) die("socket: %s", strerror(errno));
    int yes = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((uint16_t)port);
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0)
        die("bind 127.0.0.1:%d: %s", port, strerror(errno));
    if (listen(srv, 16) < 0) die("listen: %s", strerror(errno));

    fprintf(stderr, "t230scan proxy: http://127.0.0.1:%d/ -> Brother DCP-T230 IPP-USB\n",
            port);
    fprintf(stderr, "  try:  curl -sS http://127.0.0.1:%d/eSCL/ScannerCapabilities | xmllint --format -\n",
            port);

    for (;;) {
        struct sockaddr_in peer;
        socklen_t pl = sizeof(peer);
        int cfd = accept(srv, (struct sockaddr *)&peer, &pl);
        if (cfd < 0) {
            if (errno == EINTR) continue;
            die("accept: %s", strerror(errno));
        }
        int yes2 = 1;
        setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &yes2, sizeof(yes2));

        struct proxy_ctx *c = malloc(sizeof(*c));
        if (!c) { close(cfd); continue; }
        c->fd = cfd;
        c->h = h;
        c->iface = *iface;
        snprintf(c->peer, sizeof(c->peer), "%s:%u",
                 inet_ntoa(peer.sin_addr), ntohs(peer.sin_port));

        pthread_t th;
        if (pthread_create(&th, NULL, proxy_thread_main, c) != 0) {
            fprintf(stderr, "pthread_create: %s\n", strerror(errno));
            close(cfd);
            free(c);
            continue;
        }
        pthread_detach(th);
    }
    /* unreachable */
}

static void usage(void) {
    fputs(
      "usage: t230scan [-v] <command> [args]\n"
      " IPP-USB (HTTP) commands:\n"
      "   probe                 dump USB interface/endpoint info\n"
      "   caps                  GET /eSCL/ScannerCapabilities\n"
      "   status                GET /eSCL/ScannerStatus\n"
      "   get PATH              GET arbitrary path\n"
      "   scan FILE             eSCL scan to FILE (won't work on T230)\n"
      "   proxy [PORT]          listen on 127.0.0.1:PORT (default 8080) and\n"
      "                         forward HTTP/1.x requests onto IPP-USB\n"
      " Brother proprietary commands (vendor interface):\n"
      "   lock   [iface_no]     just send the open-scan-channel ctrl xfer\n"
      "   vprobe [iface_no]     show vendor iface info (default iface_no=2)\n"
      "   ckd  [iface_no]       send '\\x1bCKD\\n\\x80'   (v2 check device)\n"
      "   ssp  [iface_no]       send '\\x1bSSP\\n\\x80'\n"
      "   xsc  [iface_no]       send '\\x1bXSC\\n\\x80'\n"
      "   q    [iface_no]       send '\\x1bQ\\n\\x80'    (v1 query)\n"
      "   qdi  [iface_no]       send '\\x1bQDI\\n\\x80'  (v2 query device info)\n"
      "   esc CMD [body] [if]   send arbitrary 'ESC+CMD+LF+body+0x80'\n"
      "   vscan FILE [dpi] [mode]  full lock+CKD+SSP+XSC scan; raw payload to FILE\n"
      "                            mode: GRAY256 (default), C24BIT (color), TEXT, ERRDIF\n",
      stderr);
}

int main(int argc, char **argv) {
    int argi = 1;
    if (argi < argc && strcmp(argv[argi], "-v") == 0) { verbose = 1; argi++; }
    if (argi >= argc) { usage(); return 1; }

    libusb_context *ctx = NULL;
    int rc = libusb_init(&ctx);
    if (rc != 0) die("libusb_init: %s", libusb_error_name(rc));

    int ret = 1;
    const char *cmd = argv[argi++];

    /* Vendor-pipe commands: open the vendor (ff/ff/ff) interface */
    int is_vendor = 0;
    int is_lock_only = 0;
    int is_vscan = 0;
    const char *probe_str = NULL;
    const char *probe_body = NULL;
    const char *vscan_path = NULL;
    /* The T230 scans physically at 300 DPI no matter what RESO we request:
     * the device returns a JPEG with 300-DPI-worth of pixel data and only
     * varies the JFIF density tag. So default to 300 — anything lower
     * gives an image that displays 3x oversized at the requested density. */
    int vscan_dpi = 300;
    const char *vscan_color = "GRAY256";
    if      (strcmp(cmd, "ckd") == 0)    { is_vendor = 1; probe_str = "CKD"; probe_body = "PSRC=FB\n"; }
    else if (strcmp(cmd, "ckd-bare") == 0){ is_vendor = 1; probe_str = "CKD"; }
    else if (strcmp(cmd, "ssp") == 0)    { is_vendor = 1; probe_str = "SSP"; }
    else if (strcmp(cmd, "xsc") == 0)    { is_vendor = 1; probe_str = "XSC"; }
    else if (strcmp(cmd, "q")   == 0)    { is_vendor = 1; probe_str = "Q";   }
    else if (strcmp(cmd, "qdi") == 0)    { is_vendor = 1; probe_str = "QDI"; }
    else if (strcmp(cmd, "vprobe") == 0) { is_vendor = 1; probe_str = NULL;  }
    else if (strcmp(cmd, "lock") == 0)   { is_vendor = 1; is_lock_only = 1; probe_str = NULL; }
    else if (strcmp(cmd, "esc") == 0)    {
        is_vendor = 1;
        if (argi >= argc) { usage(); return 1; }
        probe_str = argv[argi++];
        if (argi < argc && argv[argi][0] != '-' &&
            (argv[argi][0] < '0' || argv[argi][0] > '9'))
            probe_body = argv[argi++];
    }
    else if (strcmp(cmd, "vscan") == 0)  {
        is_vendor = 1; is_vscan = 1;
        if (argi >= argc) { usage(); return 1; }
        vscan_path = argv[argi++];
        if (argi < argc && argv[argi][0] >= '0' && argv[argi][0] <= '9') {
            vscan_dpi = atoi(argv[argi++]);
        }
        if (argi < argc && argv[argi][0] != '-' &&
            (argv[argi][0] < '0' || argv[argi][0] > '9')) {
            vscan_color = argv[argi++];
        }
    }

    if (is_vendor) {
        /* brscan5 uses iface 1 (the constructor sets [+0x39]=1, kept when
         * bNumInterfaces > 1). vscan/probes default to that; explicit
         * iface_no arg overrides. */
        int prefer = is_vscan ? 1 : 2;
        if (argi < argc) prefer = atoi(argv[argi++]);
        struct ipp_iface iface = {0};
        libusb_device_handle *h = open_t230(ctx, IFACE_VENDOR, prefer, &iface);
        vlog("opened vendor iface=%u alt=%u in=0x%02x out=0x%02x",
             iface.bInterfaceNumber, iface.bAlternateSetting, iface.ep_in, iface.ep_out);

        /* "Open scan-channel" handshake — required before any bulk traffic. */
        if (br_scan_channel_ctl(h, BR_VENDOR_LOCK) == 0) {
            vlog("scan channel locked");
            if (is_lock_only) {
                ret = 0;
            } else if (is_vscan) {
                ret = cmd_scan_vendor(h, &iface, vscan_path, vscan_dpi, vscan_color);
            } else if (probe_str) {
                ret = cmd_vendor_probe_body(h, &iface, probe_str, probe_body);
            } else {
                ret = cmd_probe(h, &iface);
            }
            br_scan_channel_ctl(h, BR_VENDOR_UNLOCK);
        } else {
            fprintf(stderr, "lock failed; skipping probe\n");
            ret = 2;
        }
        libusb_release_interface(h, iface.bInterfaceNumber);
        libusb_close(h);
        libusb_exit(ctx);
        return ret;
    }

    /* IPP-USB / HTTP commands */
    struct ipp_iface iface = {0};
    libusb_device_handle *h = open_t230(ctx, IFACE_IPP_USB, -1, &iface);
    vlog("opened IPP-USB iface=%u alt=%u in=0x%02x out=0x%02x",
         iface.bInterfaceNumber, iface.bAlternateSetting, iface.ep_in, iface.ep_out);

    if      (strcmp(cmd, "probe")  == 0) ret = cmd_probe(h, &iface);
    else if (strcmp(cmd, "caps")   == 0) ret = cmd_get(h, &iface, "/eSCL/ScannerCapabilities");
    else if (strcmp(cmd, "status") == 0) ret = cmd_get(h, &iface, "/eSCL/ScannerStatus");
    else if (strcmp(cmd, "get")    == 0) {
        if (argi >= argc) { usage(); ret = 1; }
        else ret = cmd_get(h, &iface, argv[argi]);
    }
    else if (strcmp(cmd, "scan")   == 0) {
        if (argi >= argc) { usage(); ret = 1; }
        else ret = cmd_scan(h, &iface, argv[argi]);
    }
    else if (strcmp(cmd, "proxy")  == 0) {
        int port = 8080;
        if (argi < argc) port = atoi(argv[argi]);
        if (port <= 0 || port > 65535) { usage(); ret = 1; }
        else ret = cmd_proxy(h, &iface, port);
    }
    else { usage(); ret = 1; }

    libusb_release_interface(h, iface.bInterfaceNumber);
    libusb_close(h);
    libusb_exit(ctx);
    return ret;
}
