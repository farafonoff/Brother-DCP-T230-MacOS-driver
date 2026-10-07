#!/usr/bin/env python3
"""AirPrint front-end for the Brother DCP-T230 CUPS queue.

CUPS derives `document-format-supported` from its filter graph, so a queue
always advertises PDF/PostScript/... and clients (macOS, Android) pick them,
forcing the slow ARM box to rasterize. This tiny IPP reverse proxy sits in
front of the local CUPS queue, forwards every request unchanged, and only
rewrites the Get-Printer-Attributes response so the printer offers just
image/urf (+ image/pwg-raster). Clients then rasterize on their own CPU and
the box merely converts headers (see brother_dcpt230_pjl).

stdlib only. Run:  t230ipp.py --queue DCP_T230 --listen 0.0.0.0:8631
"""

import argparse
import http.client
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OP_GET_PRINTER_ATTRS = 0x000B

TAG_END = 0x03
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_MIME = 0x49

FORMATS = [b"image/urf", b"image/pwg-raster"]
# Attributes pointing at cupsd's own (https, port 631) URLs: drop them.
DROP = {b"printer-more-info", b"printer-icons", b"printer-supply-info-uri",
        b"printer-privacy-policy-uri", b"printer-strings-uri"}


# ---------------------------------------------------------------- IPP codec
# A message is kept as a flat list of (tag, name, value) records. Group
# delimiters (tag <= 5) carry empty name/value. Additional values of an
# attribute (and collection members) have an empty name.

def parse(buf: bytes, pos: int = 8):
    """Return (records, end_offset), or None if `buf` is incomplete."""
    recs = []
    n = len(buf)
    while True:
        if pos >= n:
            return None
        tag = buf[pos]
        pos += 1
        if tag <= 0x05:
            recs.append((tag, b"", b""))
            if tag == TAG_END:
                return recs, pos
            continue
        if pos + 2 > n:
            return None
        nl = int.from_bytes(buf[pos:pos + 2], "big")
        pos += 2
        if pos + nl + 2 > n:
            return None
        name = bytes(buf[pos:pos + nl])
        pos += nl
        vl = int.from_bytes(buf[pos:pos + 2], "big")
        pos += 2
        if pos + vl > n:
            return None
        recs.append((tag, name, bytes(buf[pos:pos + vl])))
        pos += vl


def encode(recs) -> bytes:
    out = bytearray()
    for tag, name, value in recs:
        out.append(tag)
        if tag > 0x05:
            out += len(name).to_bytes(2, "big") + name
            out += len(value).to_bytes(2, "big") + value
    return bytes(out)


def _span(recs, name):
    """(start, stop) of attribute `name` incl. its additional values."""
    for i, (tag, nm, _v) in enumerate(recs):
        if tag > 0x05 and nm == name:
            j = i + 1
            while j < len(recs) and recs[j][0] > 0x05 and recs[j][1] == b"":
                j += 1
            return i, j
    return None


def replace_attr(recs, name, tag, values):
    """Replace attribute `name` (and its extra values); no-op if absent."""
    sp = _span(recs, name)
    if sp is not None:
        recs[sp[0]:sp[1]] = [(tag, name if k == 0 else b"", v)
                             for k, v in enumerate(values)]


def drop_attr(recs, name):
    while True:
        sp = _span(recs, name)
        if sp is None:
            return
        del recs[sp[0]:sp[1]]


def rewrite_printer_attrs(recs, own_uri: bytes):
    """Patch a Get-Printer-Attributes response in place."""
    for n in DROP:
        drop_attr(recs, n)
    if _span(recs, b"document-format-supported"):
        replace_attr(recs, b"document-format-supported", TAG_MIME, FORMATS)
    if _span(recs, b"document-format-preferred"):
        replace_attr(recs, b"document-format-preferred", TAG_MIME, [FORMATS[0]])
    if _span(recs, b"printer-uri-supported"):
        replace_attr(recs, b"printer-uri-supported", TAG_URI, [own_uri])
    if _span(recs, b"uri-security-supported"):
        replace_attr(recs, b"uri-security-supported", TAG_KEYWORD, [b"none"])
    if _span(recs, b"uri-authentication-supported"):
        replace_attr(recs, b"uri-authentication-supported", TAG_KEYWORD, [b"none"])
    return recs


def rewrite_request(recs, queue: str, cups_port: int):
    """Point printer-uri / job-uri at the local CUPS queue."""
    for i, (tag, name, value) in enumerate(recs):
        if tag == TAG_URI and name == b"printer-uri":
            recs[i] = (tag, name, f"ipp://localhost:{cups_port}/printers/{queue}".encode())
        elif tag == TAG_URI and name == b"job-uri" and b"/jobs/" in value:
            job = value.rsplit(b"/jobs/", 1)[1]
            recs[i] = (tag, name, f"ipp://localhost:{cups_port}/jobs/".encode() + job)
    return recs


# ---------------------------------------------------------------- HTTP side

def _chunked(rfile):
    while True:
        size = int(rfile.readline().split(b";")[0].strip() or b"0", 16)
        if size == 0:
            while rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            return
        data = rfile.read(size)
        rfile.readline()
        yield data


def _body(handler):
    if "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
        yield from _chunked(handler.rfile)
        return
    left = int(handler.headers.get("Content-Length", 0))
    while left > 0:
        data = handler.rfile.read(min(left, 256 * 1024))
        if not data:
            return
        left -= len(data)
        yield data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    queue = "DCP_T230"
    cups_host, cups_port = "localhost", 631

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, fmt, *args):
        sys.stderr.write("t230ipp: " + fmt % args + "\n")

    def _reply(self, code, body=b"", ctype="application/ipp"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        gen = _body(self)
        buf = bytearray()
        parsed = None
        for chunk in gen:
            buf += chunk
            parsed = parse(buf)
            if parsed:
                break
        if not parsed:
            return self._reply(400)
        recs, end = parsed
        op = int.from_bytes(buf[2:4], "big")
        head = bytes(buf[:8]) + encode(rewrite_request(recs, self.queue, self.cups_port))
        rest = bytes(buf[end:])

        t0, sent = time.monotonic(), len(head) + len(rest)
        try:
            conn = http.client.HTTPConnection(self.cups_host, self.cups_port, timeout=600)
            conn.connect()
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.putrequest("POST", f"/printers/{self.queue}", skip_host=False)
            conn.putheader("Content-Type", "application/ipp")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()

            # Clients (CUPS) upload in ~2 KB chunks; forward in big writes so
            # neither Nagle nor per-chunk Python overhead throttles the job.
            pend = bytearray()

            def flush():
                if pend:
                    conn.send(f"{len(pend):x}\r\n".encode() + pend + b"\r\n")
                    pend.clear()
            pend += head + rest
            for chunk in gen:
                sent += len(chunk)
                pend += chunk
                if len(pend) >= 256 * 1024:
                    flush()
            flush()
            conn.send(b"0\r\n\r\n")
            resp = conn.getresponse()
            body = resp.read()
            status = resp.status
            conn.close()
            if op == 0x0002:
                self.log_message("Print-Job: %d bytes in %.1fs, cups status %d", sent, time.monotonic() - t0, status)
        except (OSError, http.client.HTTPException) as e:
            self.log_message("cups forward failed: %s", e)
            return self._reply(503)

        if op == OP_GET_PRINTER_ATTRS and status == 200:
            out = parse(body)
            if out:
                host = self.headers.get("Host") or f"{socket.gethostname()}:{self.server.server_port}"
                own = f"ipp://{host}/ipp/print".encode()
                recs2 = rewrite_printer_attrs(out[0], own)
                body = body[:8] + encode(recs2) + body[out[1]:]
        self._reply(status, body)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queue", default="DCP_T230")
    ap.add_argument("--listen", default="0.0.0.0:8631")
    ap.add_argument("--cups", default="localhost:631")
    a = ap.parse_args()
    Handler.queue = a.queue
    Handler.cups_host, port = a.cups.rsplit(":", 1)
    Handler.cups_port = int(port)
    host, lport = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(lport)), Handler)
    sys.stderr.write(f"t230ipp: {a.listen} -> cups {a.cups} queue {a.queue}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
