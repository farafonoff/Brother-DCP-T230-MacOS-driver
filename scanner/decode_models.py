#!/usr/bin/env python3
"""
Decoder for Brother brscan5 obfuscated model files (`?#`-prefixed lines in
models/brscan5ext_*.ini). Reverse-engineered from libsane-brother5.so:
`decode_sub` at file offset 0xf11b plus eight `scramble_*` helpers.

Each encoded line is `?#<A><B><payload>`:
  - A and B are signed-byte keys (NOT decoded; they ARE part of the cipher key)
  - Payload bytes outside [0x20..0x7b] pass through unchanged
  - Otherwise: swap-fn -> rev -> add (per-line transform chosen by (A+B)&7)
"""
import sys
import os
import glob

# ---- the 8 byte-shuffles ----

def swap1(c):
    if c > 0x7b: return c
    return c - 1 if (c & 1) else c + 1

def swap2(c):
    if c <= 0x1f or c > 0x7b: return c
    r = c & 3
    if r == 1: return c + 1
    if r == 2: return c - 1
    return c          # 0 or 3 unchanged

def swap3(c):
    if c <= 0x1f or c > 0x7b: return c
    r = c & 3
    if r == 0: return c + 3
    if r == 3: return c - 3
    return c          # 1 or 2 unchanged

def swap4(c):
    if c > 0x7b: return c
    r = c & 3
    if r <= 1: return c + 2
    return c - 2      # r in {2,3}

def swap5(c):
    if c > 0x7b: return c
    r = c & 3
    if r == 0: return c + 2
    if r == 2: return c - 2
    return c

def swap6(c):
    if c > 0x7b: return c
    r = c & 3
    if r == 1: return c + 2
    if r == 3: return c - 2
    return c

def rev(c):
    # 0x7c - (c - 0x20)
    return 0x9c - c

def add_fold(c, key):
    v = c + key
    while v > 0x7c:  v -= 0x5c
    while v <= 0x1f: v += 0x5c
    return v

# (A+B) & 7 picks one of these per line
TRANSFORMS = [
    # (swap_fn, key_offset, key_var)   key = key_offset - (A or B)
    (swap2,  -3,  'A'),
    (swap1, -20,  'B'),
    (swap6, -69,  'A'),
    (swap4,  -7,  'B'),
    (swap2, -53,  'A'),
    (swap4, -55,  'B'),
    (swap3, -32,  'A'),
    (swap5, -19,  'B'),
]

def signed8(b):
    return b - 256 if b >= 128 else b

def decode_line(raw_bytes):
    """raw_bytes: bytes of one line (without the trailing newline)."""
    if not raw_bytes.startswith(b'?#'):
        return raw_bytes
    body = raw_bytes[2:]
    if len(body) < 2:
        return raw_bytes
    A = signed8(body[0])
    B = signed8(body[1])
    idx = (A + B) & 7
    swap_fn, key_off, key_var = TRANSFORMS[idx]
    key = key_off - (A if key_var == 'A' else B)
    out = bytearray()
    for c in body[2:]:
        if c <= 0x1f or c > 0x7b:
            out.append(c)
        else:
            x = swap_fn(c)
            x = rev(x)
            x = add_fold(x, key)
            out.append(x & 0xff)
    return bytes(out)

def decode_file(path):
    with open(path, 'rb') as f:
        data = f.read()
    out_lines = []
    for line in data.splitlines():
        decoded = decode_line(line)
        out_lines.append(decoded)
    return out_lines

if __name__ == '__main__':
    base = sys.argv[1] if len(sys.argv) > 1 else \
        '/Users/artem_farafonov/Downloads/brscan/brscan5-1.5.1-0.amd64/data/opt/brother/scanner/brscan5/models'
    target = sys.argv[2].lower() if len(sys.argv) > 2 else None  # e.g. "t230"
    for path in sorted(glob.glob(os.path.join(base, 'brscan5ext_*.ini'))):
        lines = decode_file(path)
        printed_header = False
        for ln in lines:
            try:
                txt = ln.decode('latin1')
            except Exception:
                txt = repr(ln)
            if target and target not in txt.lower():
                continue
            if not printed_header:
                print(f'\n=== {os.path.basename(path)} ===')
                printed_header = True
            print(txt)
