#!/usr/bin/env python3
"""Tests for t230ipp.py. Run: python3 -m unittest test_t230ipp -v"""
import http.client
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import t230ipp as ipp


def rec(tag, name, value):
    return (tag, name.encode(), value if isinstance(value, bytes) else value.encode())


def message(prefix, recs):
    return prefix + ipp.encode(recs)


GPA_RESPONSE = [
    (0x01, b"", b""),
    rec(0x47, "attributes-charset", "utf-8"),
    (0x04, b"", b""),
    rec(0x49, "document-format-supported", "application/pdf"),
    rec(0x49, "", "application/postscript"),
    rec(0x49, "", "image/urf"),
    rec(0x49, "document-format-preferred", "application/pdf"),
    rec(0x45, "printer-uri-supported", "ipps://box:631/printers/DCP_T230"),
    rec(0x44, "uri-security-supported", "tls"),
    rec(0x45, "printer-icons", "https://box:631/icon.png"),
    rec(0x42, "printer-name", "DCP_T230"),
    (0x03, b"", b""),
]
RESP_PREFIX = b"\x02\x00\x00\x00\x00\x00\x00\x01"


class CodecTests(unittest.TestCase):
    def test_roundtrip(self):
        raw = message(RESP_PREFIX, GPA_RESPONSE) + b"DATA"
        recs, end = ipp.parse(raw)
        self.assertEqual(recs, GPA_RESPONSE)
        self.assertEqual(raw[end:], b"DATA")

    def test_incomplete_returns_none(self):
        raw = message(RESP_PREFIX, GPA_RESPONSE)
        for cut in (9, 20, len(raw) - 1):
            self.assertIsNone(ipp.parse(raw[:cut]))

    def test_rewrite_printer_attrs(self):
        recs = ipp.rewrite_printer_attrs(list(GPA_RESPONSE), b"ipp://me:8631/ipp/print")
        d = {}
        cur = None
        for tag, name, val in recs:
            if tag > 5:
                cur = name or cur
                d.setdefault(cur, []).append(val)
        self.assertEqual(d[b"document-format-supported"], [b"image/urf", b"image/pwg-raster"])
        self.assertEqual(d[b"document-format-preferred"], [b"image/urf"])
        self.assertEqual(d[b"printer-uri-supported"], [b"ipp://me:8631/ipp/print"])
        self.assertEqual(d[b"uri-security-supported"], [b"none"])
        self.assertNotIn(b"printer-icons", d)
        self.assertEqual(d[b"printer-name"], [b"DCP_T230"])

    def test_rewrite_request_uris(self):
        recs = [(0x01, b"", b""), rec(0x45, "printer-uri", "ipp://x/ipp/print"),
                rec(0x45, "job-uri", "ipp://x:8631/jobs/42"), (0x03, b"", b"")]
        out = ipp.rewrite_request(recs, "DCP_T230", 631)
        self.assertEqual(out[1][2], b"ipp://localhost:631/printers/DCP_T230")
        self.assertEqual(out[2][2], b"ipp://localhost:631/jobs/42")


class FakeCups(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = b""
        if "chunked" in self.headers.get("Transfer-Encoding", ""):
            for c in ipp._chunked(self.rfile):
                body += c
        else:
            body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeCups.seen.append((self.path, body))
        op = int.from_bytes(body[2:4], "big")
        out = message(RESP_PREFIX, GPA_RESPONSE) if op == ipp.OP_GET_PRINTER_ATTRS \
            else RESP_PREFIX + b"\x03"
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cups = HTTPServer(("127.0.0.1", 0), FakeCups)
        threading.Thread(target=cls.cups.serve_forever, daemon=True).start()
        ipp.Handler.cups_host, ipp.Handler.cups_port = "127.0.0.1", cls.cups.server_port
        ipp.Handler.queue = "DCP_T230"
        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), ipp.Handler)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.cups.shutdown()
        cls.proxy.shutdown()

    def post(self, body, chunked=False):
        c = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port)
        if chunked:
            c.request("POST", "/ipp/print", body=iter([body[:7], body[7:50], body[50:]]),
                      headers={"Content-Type": "application/ipp"}, encode_chunked=True)
        else:
            c.request("POST", "/ipp/print", body=body, headers={"Content-Type": "application/ipp"})
        r = c.getresponse()
        return r.status, r.read()

    def request(self, op, data=b""):
        recs = [(0x01, b"", b""), rec(0x47, "attributes-charset", "utf-8"),
                rec(0x45, "printer-uri", "ipp://me:8631/ipp/print"), (0x03, b"", b"")]
        return b"\x02\x00" + op.to_bytes(2, "big") + b"\x00\x00\x00\x07" + ipp.encode(recs) + data

    def test_get_printer_attributes_is_rewritten(self):
        for chunked in (False, True):
            st, body = self.post(self.request(0x000B), chunked)
            self.assertEqual(st, 200)
            recs, _ = ipp.parse(body)
            fmts = [v for t, n, v in recs if t == 0x49 and (n == b"document-format-supported" or n == b"")]
            self.assertEqual(fmts, [b"image/urf", b"image/pwg-raster"])

    def test_print_job_forwarded_verbatim_with_uri_rewritten(self):
        FakeCups.seen.clear()
        payload = bytes(range(256)) * 2000
        for chunked in (False, True):
            st, _ = self.post(self.request(0x0002, payload), chunked)
            self.assertEqual(st, 200)
        for path, body in FakeCups.seen:
            self.assertEqual(path, "/printers/DCP_T230")
            self.assertTrue(body.endswith(payload))
            self.assertIn(b"ipp://localhost:%d/printers/DCP_T230" % self.cups.server_port, body)


class DualStack(unittest.TestCase):
    def test_accepts_ipv4_and_ipv6(self):
        import socket
        srv = ipp.make_server("[::]:0", ipp.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                with self.subTest(addr=addr):
                    try:
                        s = socket.socket(fam)
                        s.settimeout(3)
                        s.connect((addr, srv.server_port))
                    except OSError as e:
                        self.skipTest(f"{addr} unavailable here: {e}")
                    s.close()
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
