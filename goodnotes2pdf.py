#!/usr/bin/env python3
"""
goodnotes2pdf - batch-convert .goodnotes files to PDF without the GoodNotes app.

A .goodnotes file is a ZIP archive. Inside it are:
  attachments/   embedded PDFs / images (imported documents, paper templates)
  notes/         per-page ink data (Protocol Buffers, undocumented schema)
  thumbnails/    small JPEG/PNG previews of each page
  index.*.pb     indexes (page order, etc.)

The ink format is not publicly documented, so stroke decoding here is
heuristic: it walks the protobuf data looking for packed coordinate arrays.
Run the `inspect` command on one file first if the output looks wrong.

Usage:
  python goodnotes2pdf.py                       (GUI folder pickers)
  python goodnotes2pdf.py convert IN_DIR OUT_DIR [--workers 4] [--overwrite]
  python goodnotes2pdf.py convert file.goodnotes OUT_DIR
  python goodnotes2pdf.py inspect file.goodnotes [--report report.txt]
  python goodnotes2pdf.py dump file.goodnotes [--report dump.txt] [--page N]

Requires:  pip install pymupdf
"""

import argparse
import io
import math
import os
import re
import struct
import sys
import time
import traceback
import zipfile
import zlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    import pymupdf
except ImportError:  # older PyMuPDF
    try:
        import fitz as pymupdf
    except ImportError:
        sys.exit("PyMuPDF is required:  pip install pymupdf")

__version__ = "0.11 (filled shapes, curves, long strokes, slide outlines)"

UUID_RE = re.compile(rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
DEFAULT_PAGE = (595.0, 842.0)  # A4 in points
MAX_DEPTH = 14
GN_SCALE = 72 / 132  # GoodNotes units are 1/132 inch; PDF points are 1/72 inch
PEN_WIDTH = None     # None = use the pen width stored with each stroke
PDF_OUTLINES = True  # also include the outline that came inside imported PDFs
WT_NAMES = {0: "varint", 1: "f64", 2: "bytes", 5: "f32"}


# --------------------------------------------------------------------------
# Container access (zip file, or an already-unpacked folder)
# --------------------------------------------------------------------------
class Container:
    def __init__(self, path):
        self.path = Path(path)
        self.zip = None
        if self.path.is_dir():
            self.names = sorted(
                str(p.relative_to(self.path)).replace("\\", "/")
                for p in self.path.rglob("*") if p.is_file()
            )
        else:
            self.zip = zipfile.ZipFile(self.path)
            self.names = sorted(n for n in self.zip.namelist() if not n.endswith("/"))

    def read(self, name):
        if self.zip:
            return self.zip.read(name)
        return (self.path / name).read_bytes()

    def size(self, name):
        if self.zip:
            return self.zip.getinfo(name).file_size
        return (self.path / name).stat().st_size

    def under(self, folder):
        # match "notes/..." and also "<prefix>/notes/..." in case of a wrapper folder
        out = []
        for n in self.names:
            parts = n.split("/")
            if folder in parts[:-1] and not parts[-1].startswith("."):
                out.append(n)
        return out

    def find(self, basename):
        for n in self.names:
            if n.split("/")[-1] == basename:
                return n
        return None

    def close(self):
        if self.zip:
            self.zip.close()


def sniff(data):
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:12] == b"\x00\x00\x00\x0cjP  \r\n\x87\n" or data[:4] == b"\xffO\xffQ":
        return "jp2"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if data[:2] in (b"\x78\x01", b"\x78\x9c", b"\x78\xda"):
        return "zlib"
    if data[:4] in (b"bvx2", b"bvx1", b"bvxn", b"bvx-"):
        return "lzfse"
    return "data"


def maybe_decompress(data):
    if sniff(data) == "zlib":
        try:
            return zlib.decompress(data)
        except zlib.error:
            pass
    return data


# --------------------------------------------------------------------------
# Schema-less protobuf decoding
# --------------------------------------------------------------------------
def read_varint(buf, pos):
    result = shift = 0
    n = len(buf)
    while True:
        if pos >= n:
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def parse_message(buf):
    """Return list of (field_no, wire_type, value) or raise ValueError."""
    fields = []
    pos, n = 0, len(buf)
    while pos < n:
        key, pos = read_varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if fn == 0 or fn > 100000:
            raise ValueError("bad field number")
        if wt == 0:
            v, pos = read_varint(buf, pos)
        elif wt == 1:
            v = buf[pos:pos + 8]
            pos += 8
        elif wt == 2:
            ln, pos = read_varint(buf, pos)
            v = buf[pos:pos + ln]
            pos += ln
        elif wt == 5:
            v = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError("unsupported wire type")
        if pos > n:
            raise ValueError("overrun")
        fields.append((fn, wt, v))
    return fields


def split_delimited(buf):
    """Try to read a stream of varint-length-prefixed messages."""
    msgs, pos, n = [], 0, len(buf)
    try:
        while pos < n:
            ln, pos = read_varint(buf, pos)
            if ln == 0 or pos + ln > n:
                return None
            msgs.append(buf[pos:pos + ln])
            pos += ln
    except ValueError:
        return None
    return msgs


def top_level_messages(buf):
    """A notes file is either a delimited stream or one big message."""
    buf = maybe_decompress(buf)
    msgs = split_delimited(buf)
    if msgs and len(msgs) > 1:
        try:
            for m in msgs[:5]:
                parse_message(m)
            return msgs
        except ValueError:
            pass
    return [buf]


# --------------------------------------------------------------------------
# Stroke heuristics
# --------------------------------------------------------------------------
def _points_from(values, stride):
    pts = [(values[i], values[i + 1]) for i in range(0, len(values) - stride + 1, stride)]
    if len(pts) < 4:
        return None
    for x, y in pts:
        if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > 50000 or abs(y) > 50000:
            return None
    steps = sorted(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                   for i in range(len(pts) - 1))
    if steps[-1] == 0:
        return None
    median = steps[len(steps) // 2]
    if median > 40 or steps[-1] > 600:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    # reject arrays that are mostly zeros / tiny numbers (pressure, times, colors)
    if max(xs) - min(xs) < 0.5 and max(ys) - min(ys) < 0.5:
        return None
    if max(abs(v) for v in xs + ys) < 2:
        return None
    return pts


def looks_like_text(b):
    if not b:
        return False
    printable = sum(1 for c in b if 32 <= c < 127 or c in (9, 10, 13))
    return printable / len(b) > 0.85


def decode_point_blob(b):
    """If bytes look like a packed array of coordinates, return list of (x, y)."""
    if len(b) < 32 or looks_like_text(b):
        return None
    if len(b) % 4 == 0:
        vals = struct.unpack("<%df" % (len(b) // 4), b)
        for stride in (2, 3, 4, 5, 6, 7, 8):
            if len(vals) % stride == 0 and len(vals) // stride >= 2:
                pts = _points_from(vals, stride)
                if pts:
                    return pts
    if len(b) % 8 == 0:
        vals = struct.unpack("<%dd" % (len(b) // 8), b)
        for stride in (2, 3, 4):
            if len(vals) % stride == 0 and len(vals) // stride >= 2:
                pts = _points_from(vals, stride)
                if pts:
                    return pts
    return None


def _as_float(wt, v):
    if wt == 5 and len(v) == 4:
        return struct.unpack("<f", v)[0]
    if wt == 1 and len(v) == 8:
        return struct.unpack("<d", v)[0]
    return None


def _guess_color(fields):
    for fn, wt, v in fields:
        if wt != 2:
            continue
        if len(v) == 16:
            c = struct.unpack("<4f", v)
            if all(0 <= x <= 1 for x in c):
                return c
        try:
            sub = parse_message(v)
        except ValueError:
            continue
        fl = [f for f in (_as_float(w, x) for _, w, x in sub) if f is not None]
        if 3 <= len(fl) <= 4 and len(fl) == len(sub) and all(0 <= x <= 1 for x in fl):
            return tuple(fl) + ((1.0,) if len(fl) == 3 else ())
    return None


def _guess_width(fields):
    for fn, wt, v in fields:
        f = _as_float(wt, v)
        if f is not None and 0.2 <= f <= 40:
            return f
    return None


def find_strokes(buf, out, depth=0, inherited=None):
    try:
        fields = parse_message(buf)
    except ValueError:
        return
    color = _guess_color(fields) or (inherited or {}).get("color")
    width = _guess_width(fields) or (inherited or {}).get("width")
    ctx = {"color": color, "width": width}
    for fn, wt, v in fields:
        if wt != 2 or len(v) < 2:
            continue
        pts = decode_point_blob(v)
        if pts:
            out.append({"points": pts, "color": color, "width": width})
            continue
        if depth < MAX_DEPTH:
            find_strokes(v, out, depth + 1, ctx)



# --------------------------------------------------------------------------
# GoodNotes ink records (decoded from real files)
#
# A page file is a stream of length-prefixed protobuf records. Ink elements
# live in field 7:  7.1 = element uuid, 7.2 = geometry blob, 7.14 = deleted,
# 7.15.1 = version.  The geometry blob is Apple "bv4" LZ4 framing around a
# "tpl" structure whose type signature is e.g. "vuA(v)A(S(uu))A(S(uuuu))",
# followed by: u32 N (point count) ... first point (f32 x, y), u32 N-1,
# then N-1 points (f32 x, y) in page points, origin top-left.
# --------------------------------------------------------------------------
def lz4_block_decompress(src, size_hint=None, prefix=b""):
    """Raw LZ4 block. `prefix` is previously decoded data that matches may refer back to."""
    base = len(prefix)
    dst = bytearray(prefix)
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                b = src[i]
                i += 1
                ll += b
                if b != 255:
                    break
        dst += src[i:i + ll]
        i += ll
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off == 0 or start < 0:
            raise ValueError("bad lz4 offset")
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    return bytes(dst[base:])


def apple_lz4_decode(b):
    """Decode Apple compression_lz4 framing (bv41 / bv4- / bv4$ blocks)."""
    out, pos = bytearray(), 0
    while pos + 4 <= len(b):
        magic = b[pos:pos + 4]
        if magic == b"bv4$":
            break
        if magic == b"bv41":
            dsize, csize = struct.unpack_from("<II", b, pos + 4)
            out += lz4_block_decompress(b[pos + 12:pos + 12 + csize], prefix=bytes(out[-65536:]))[:dsize]
            pos += 12 + csize
        elif magic == b"bv4-":
            (size,) = struct.unpack_from("<I", b, pos + 4)
            out += b[pos + 8:pos + 8 + size]
            pos += 8 + size
        else:
            raise ValueError("unknown bv4 block")
    return bytes(out)


def _plausible(x, y):
    return math.isfinite(x) and math.isfinite(y) and -2000 < x < 20000 and -2000 < y < 20000


def points_from_tpl(d):
    """Parse a decoded 'tpl' path.

    Layout (verified on real files): NUL-terminated type signature
    "vuA(v)A(S(uu))A(S(uuuu))", u16, f32 pen width, u32 N, per-point types,
    then the start point (f32 x, y), u32 N-1, and N-1 quadratic segments of
    four f32 each (control x, control y, end x, end y).
    Returns (pen_width, [ (start, [(ctrl, end), ...]) ]).
    """
    if not d.startswith(b"tpl\x00"):
        return None, []
    sig_end = d.find(b"\x00", 8)
    if sig_end < 0:
        return None, []
    total, width = None, None
    if sig_end + 11 <= len(d):
        (w,) = struct.unpack_from("<f", d, sig_end + 3)
        if math.isfinite(w) and 0.05 < w < 500:
            width = w
        (total,) = struct.unpack_from("<I", d, sig_end + 7)
        if not 1 <= total <= 200000:
            total = None
    paths = []
    p = sig_end + 1
    end = len(d)
    while p + 12 <= end:
        x0, y0, m = struct.unpack_from("<ffI", d, p)
        ok = (_plausible(x0, y0) and abs(x0) > 0.01 and
              (m == total - 1 if total else 1 <= m <= 200000) and p + 12 + 16 * m <= end)
        if ok:
            vals = struct.unpack_from("<%df" % (4 * m), d, p + 12) if m else ()
            segs = [((vals[i], vals[i + 1]), (vals[i + 2], vals[i + 3])) for i in range(0, len(vals), 4)]
            flat = [q for c, e in segs for q in (c, e)]
            if all(_plausible(x, y) for x, y in flat) and (
                    m == 0 or math.hypot(flat[0][0] - x0, flat[0][1] - y0) < 80):
                paths.append(((x0, y0), segs))
                p += 12 + 16 * m
                total = None
                continue
        p += 1
    return width, paths


def _tpl_arrays(d):
    """Read a 'tpl' payload generically from its signature (v = u16, u = f32, A(x) = u32 count + items)."""
    se = d.find(b"\x00", 8)
    sig = d[8:se].decode("ascii", "replace")
    pos, out = se + 1, []
    for arr, scal in re.findall(r"A\((.)\)|(.)", sig):
        t = arr or scal
        size, code = {"v": (2, "H"), "u": (4, "f")}.get(t, (None, None))
        if size is None:
            raise ValueError(f"unknown tpl type {t!r}")
        if arr:
            (n,) = struct.unpack_from("<I", d, pos)
            pos += 4
            out.append(struct.unpack_from("<%d%s" % (n, code), d, pos))
            pos += n * size
        else:
            out.append(struct.unpack_from("<" + code, d, pos)[0])
            pos += size
    return sig, out


def decode_outline_fill(blob):
    """Strokes stored as a filled outline (signature vuA(v)A(u)A(u)A(v)A(v)A(u)...; pen = -1).

    Array 6 = path commands (2 = move to next point of array 7, 4 = cubic using the next
    6 floats of array 9). Returns [(start, [(c1, c2, end), ...]), ...] or []."""
    try:
        d = apple_lz4_decode(blob) if blob[:3] == b"bv4" else blob
        sig, arrs = _tpl_arrays(d)
    except Exception:
        return []
    if not sig.startswith("vuA(v)A(u)A(u)A(v)A(v)A(u)") or len(arrs) < 10:
        return []
    cmds, moves, cubics = arrs[6], arrs[7], arrs[9]
    paths, mi, ci, cur = [], 0, 0, None
    for cmd in cmds:
        if cmd == 2 and mi + 1 < len(moves):
            cur = ((moves[mi], moves[mi + 1]), [])
            paths.append(cur)
            mi += 2
        elif cmd == 4 and cur is not None and ci + 5 < len(cubics):
            v = cubics[ci:ci + 6]
            cur[1].append(((v[0], v[1]), (v[2], v[3]), (v[4], v[5])))
            ci += 6
    return [p for p in paths if p[1]]


def decode_geometry(blob):
    try:
        d = apple_lz4_decode(blob) if blob[:3] == b"bv4" else blob
    except (ValueError, IndexError, struct.error):
        return None, []
    return points_from_tpl(d)


def _floats_by_field(buf):
    out = {}
    try:
        for fn, wt, v in parse_message(buf):
            f = _as_float(wt, v)
            if f is not None:
                out[fn] = f
    except ValueError:
        pass
    return out


def _xy(buf):
    f = _floats_by_field(buf)
    return f.get(1, 0.0), f.get(2, 0.0)


def _rotated(cx, cy, pts, rot):
    if not rot:
        return pts
    cr, sr = math.cos(rot), math.sin(rot)
    return [(cx + (x - cx) * cr - (y - cy) * sr, cy + (x - cx) * sr + (y - cy) * cr) for x, y in pts]


def _shape_paths(buf):
    """7.9 shape element -> (list of point lists, width).

    9.1 = polyline {repeated 1:{x,y}}, 9.2 = quadratic curve {1: start, 2: control, 3: end},
    9.3 = box {1: centre, 2: size, 3: rotation},
    9.4 = ellipse {1: centre, 2: radii, 3: rotation}, 9.15 = line width.
    """
    paths, width = [], None
    try:
        fields = parse_message(buf)
    except ValueError:
        return paths, width
    for fn, wt, v in fields:
        if fn == 15:
            width = _as_float(wt, v)
            continue
        if wt != 2:
            continue
        try:
            sub = parse_message(v)
        except ValueError:
            continue
        if fn == 1:
            pts = [_xy(pv) for pfn, pwt, pv in sub if pfn == 1 and pwt == 2]
            if pts:
                paths.append(pts)
        elif fn == 2:  # quadratic curve: 1 = start, 2 = control point, 3 = end
            q = {pfn: _xy(pv) for pfn, pwt, pv in sub if pwt == 2}
            if 1 in q and 3 in q:
                (x0, y0), (x2, y2) = q[1], q[3]
                x1, y1 = q.get(2, ((x0 + x2) / 2, (y0 + y2) / 2))
                paths.append([((1 - t) ** 2 * x0 + 2 * (1 - t) * t * x1 + t * t * x2,
                               (1 - t) ** 2 * y0 + 2 * (1 - t) * t * y1 + t * t * y2)
                              for t in (k / 32 for k in range(33))])
        elif fn in (3, 4):
            centre, size, rot = (0.0, 0.0), (0.0, 0.0), 0.0
            for sfn, swt, sv in sub:
                if sfn == 1 and swt == 2:
                    centre = _xy(sv)
                elif sfn == 2 and swt == 2:
                    size = _xy(sv)
                elif sfn == 3:
                    rot = _as_float(swt, sv) or 0.0
            cx, cy = centre
            if fn == 3:
                hw, hh = size[0] / 2, size[1] / 2
                pts = [(cx - hw, cy - hh), (cx + hw, cy - hh), (cx + hw, cy + hh),
                       (cx - hw, cy + hh), (cx - hw, cy - hh)]
            else:
                rx, ry = size
                pts = [(cx + rx * math.cos(t * math.pi / 36), cy + ry * math.sin(t * math.pi / 36))
                       for t in range(73)]
            paths.append(_rotated(cx, cy, pts, rot))
    return paths, width


def goodnotes_elements(data):
    """Return {uuid: element dict} (latest version of each element, first-seen order)."""
    elems = {}
    for msg in top_level_messages(data):
        try:
            fields = parse_message(msg)
        except ValueError:
            continue
        for fn, wt, v in fields:
            if fn != 7 or wt != 2:
                continue
            try:
                sub = parse_message(v)
            except ValueError:
                continue
            el = {"uid": None, "blob": None, "deleted": False, "version": 0,
                  "color": (0.0, 0.0, 0.0, 1.0), "highlighter": False, "shape": None,
                  "offset": (0.0, 0.0)}
            for sfn, swt, sv in sub:
                if sfn == 1 and swt == 2:
                    el["uid"] = bytes(sv)
                elif sfn == 2 and swt == 2:
                    el["blob"] = sv
                elif sfn == 4 and swt == 2:
                    c = _floats_by_field(sv)
                    el["color"] = (c.get(1, 0.0), c.get(2, 0.0), c.get(3, 0.0), c.get(4, 0.0))
                elif sfn == 5 and swt == 0:
                    el["highlighter"] = bool(sv)
                elif sfn == 6 and swt == 2 and sv:
                    el["offset"] = _xy(sv)
                elif sfn == 9 and swt == 2:
                    el["shape"] = sv
                elif sfn == 14 and swt == 0:
                    el["deleted"] = bool(sv)
                elif sfn == 15 and swt == 2:
                    try:
                        for a_, b_, c_ in parse_message(sv):
                            if a_ == 1 and b_ == 0:
                                el["version"] = c_
                    except ValueError:
                        pass
            uid = el["uid"] if el["uid"] is not None else id(v)
            prev = elems.get(uid)
            if prev is None or el["version"] >= prev["version"]:
                elems[uid] = el
    return elems


def page_images(data):
    """Image elements on a page: [{att, centre, size, rot}] (latest version, not deleted)."""
    imgs = {}
    for msg in top_level_messages(data):
        for fn, wt, v in _sub(msg):
            if fn != 1 or wt != 2 or UUID_RE.fullmatch(bytes(v)):
                continue
            sub = _sub(v)
            if not sub:
                continue
            uid = att = None
            centre = size = None
            rot, version, deleted = 0.0, 0, False
            bounds, style, zkey = None, None, (0, 0, 0)
            for sfn, swt, sv in sub:
                if sfn == 1 and swt == 2:
                    uid = _uuid_str(sv)
                elif sfn == 4 and swt == 2:
                    att = _uuid_str(sv)
                elif sfn == 3 and swt == 2:
                    for a_, b_, c_ in _sub(sv):
                        if a_ == 1 and b_ == 2:
                            centre = _xy(c_)
                        elif a_ == 2 and b_ == 2:
                            size = _xy(c_)
                        elif a_ == 3:
                            rot = _as_float(b_, c_) or 0.0
                elif sfn == 2 and swt == 2:
                    parts = {a_: _xy(c_) for a_, b_, c_ in _sub(sv) if b_ == 2}
                    if 1 in parts and 2 in parts:
                        bounds = parts
                elif sfn == 6 and swt == 0:
                    style = sv
                elif sfn == 5 and swt == 2:  # stacking key {1: {1: a, 2: b, 3: c}}
                    for a_, b_, c_ in _sub(sv):
                        if a_ == 1 and b_ == 2:
                            k = {x: y for x, t, y in _sub(c_) if t == 0}
                            zkey = (k.get(1, 0), k.get(3, 0), k.get(2, 0))
                elif sfn == 14 and swt == 0:
                    deleted = bool(sv)
                elif sfn == 15 and swt == 2:
                    for a_, b_, c_ in _sub(sv):
                        if a_ == 1 and b_ == 0:
                            version = c_
            if not uid or not att:
                continue
            if (centre is None or size is None) and bounds:
                (ox, oy), (w, h) = bounds[1], bounds[2]
                centre, size = (ox + w / 2, oy + h / 2), (w, h)
            if centre is None or size is None:
                continue
            prev = imgs.get(uid)
            if prev is None or version >= prev["version"]:
                imgs[uid] = {"att": att, "centre": centre, "size": size, "rot": rot,
                             "version": version, "deleted": deleted, "style": style,
                             "z": zkey}
    live = [i for i in imgs.values() if not i["deleted"]]
    live.sort(key=lambda i: i["z"])  # later in stacking order = drawn on top
    return live


def page_fills(data):
    """Filled shapes: top-level field 9 records.

    9.1 = id, 9.3 = stacking key, 9.4 = geometry (same encoding as a shape outline),
    9.6 = lasso offset, 9.7 = fill colour RGBA, 9.14 = deleted, 9.15 = version."""
    fills = {}
    for msg in top_level_messages(data):
        for fn, wt, v in _sub(msg):
            if fn != 9 or wt != 2:
                continue
            uid, geo, color, offset = None, None, (0.0, 0.0, 0.0, 0.0), (0.0, 0.0)
            version, deleted, zkey = 0, False, (0, 0, 0)
            for sfn, swt, sv in _sub(v):
                if sfn == 1 and swt == 2:
                    uid = _uuid_str(sv)
                elif sfn == 3 and swt == 2:
                    for a_, b_, c_ in _sub(sv):
                        if a_ == 1 and b_ == 2:
                            k = {x: y for x, t, y in _sub(c_) if t == 0}
                            zkey = (k.get(1, 0), k.get(3, 0), k.get(2, 0))
                elif sfn == 4 and swt == 2:
                    geo = sv
                elif sfn == 6 and swt == 2 and sv:
                    offset = _xy(sv)
                elif sfn == 7 and swt == 2:
                    f = _floats_by_field(sv)
                    color = (f.get(1, 0.0), f.get(2, 0.0), f.get(3, 0.0), f.get(4, 0.0))
                elif sfn == 14 and swt == 0:
                    deleted = bool(sv)
                elif sfn == 15 and swt == 2:
                    for a_, b_, c_ in _sub(sv):
                        if a_ == 1 and b_ == 0:
                            version = c_
            if not uid or geo is None:
                continue
            prev = fills.get(uid)
            if prev is None or version >= prev["version"]:
                fills[uid] = {"geo": geo, "color": color, "offset": offset, "version": version,
                              "deleted": deleted, "z": zkey}
    live = [f for f in fills.values() if not f["deleted"]]
    live.sort(key=lambda f: f["z"])
    return live


def draw_fills(page, fills, scale):
    if not fills:
        return
    shape = page.new_shape()
    n = 0
    for f in fills:
        paths, _ = _shape_paths(f["geo"])
        dx, dy = f["offset"]
        r, g_, b, a = f["color"]
        if a <= 0:
            continue
        for pts in paths:
            if len(pts) < 3:
                continue
            shape.draw_polyline([pymupdf.Point((x + dx) * scale, (y + dy) * scale) for x, y in pts])
            shape.finish(color=None, fill=(r, g_, b), fill_opacity=min(1.0, a), closePath=True)
            n += 1
    if n:
        shape.commit()


def draw_images(page, images, files, scale):
    for im in images:
        data = files.get(im["att"])
        if not data:
            continue
        cx, cy = im["centre"]
        w, h = im["size"][0] * scale, im["size"][1] * scale
        cx, cy = cx * scale, cy * scale
        if w <= 0 or h <= 0:
            continue
        deg = math.degrees(im["rot"])
        try:
            if abs(deg) < 0.05:
                page.insert_image(pymupdf.Rect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
                                  stream=data, keep_proportion=False)
            else:
                tmp = pymupdf.open()
                tp = tmp.new_page(width=w, height=h)
                tp.insert_image(tp.rect, stream=data, keep_proportion=False)
                cr, sr = abs(math.cos(im["rot"])), abs(math.sin(im["rot"]))
                bw, bh = w * cr + h * sr, w * sr + h * cr
                page.show_pdf_page(pymupdf.Rect(cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2),
                                   tmp, 0, rotate=-deg)
                tmp.close()
            if im.get("style") == 2:  # GoodNotes draws a thin grey frame (matches its export)
                corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2), (-w / 2, -h / 2)]
                cr, sr = math.cos(im["rot"]), math.sin(im["rot"])
                pts = [pymupdf.Point(cx + x * cr - y * sr, cy + x * sr + y * cr) for x, y in corners]
                sh = page.new_shape()
                sh.draw_polyline(pts)
                sh.finish(color=(0.72, 0.72, 0.72), width=0.3, closePath=True)
                sh.commit()
        except Exception as e:
            print(f"  warning: could not place image {im['att'][:8]}: {e}")


def load_image_files(c):
    files, cache = {}, {}
    for aid, path in attachment_files(c).items():
        if path not in cache:
            data = c.read(path)
            cache[path] = data if sniff(data[:12]) in ("jpeg", "png", "jp2", "tiff", "gif") else None
        if cache[path]:
            files[aid] = cache[path]
    return files


def page_strokes(data, width=None):
    elems = goodnotes_elements(data)
    if elems:
        out = []
        for el in elems.values():
            if el["deleted"]:
                continue
            col = el["color"]
            if col[3] == 0:
                col = col[:3] + (1.0,)
            dx, dy = el["offset"]
            mv = (lambda q: (q[0] + dx, q[1] + dy)) if (dx or dy) else (lambda q: q)
            if el["blob"]:
                pen, paths = decode_geometry(el["blob"])
                if not paths:
                    fp = decode_outline_fill(el["blob"])
                    if fp:
                        fp = [(mv(st), [(mv(a), mv(b), mv(e)) for a, b, e in sg]) for st, sg in fp]
                        pts = [st for st, _ in fp] + [e for _, sg in fp for _, _, e in sg]
                        out.append({"points": pts, "fillpath": fp, "color": col, "width": None,
                                    "highlighter": el["highlighter"]})
                for start, segs in paths:
                    start = mv(start)
                    segs = [(mv(c), mv(e)) for c, e in segs]
                    pts = [start] + [q for c, e in segs for q in (c, e)]
                    out.append({"points": pts, "start": start, "segs": segs, "color": col,
                                "width": width or pen, "highlighter": el["highlighter"]})
            if el["shape"]:
                spaths, sw = _shape_paths(el["shape"])
                for pts in spaths:
                    pts = [mv(q) for q in pts]
                    out.append({"points": pts, "color": col, "width": width or sw,
                                "highlighter": el["highlighter"]})
        return out
    # unknown layout: fall back to the generic heuristic
    strokes = []
    for msg in top_level_messages(data):
        find_strokes(msg, strokes)
    return strokes


# --------------------------------------------------------------------------
# Page ordering
# --------------------------------------------------------------------------
def _sub(buf):
    try:
        return parse_message(buf)
    except ValueError:
        return []


def _ver(buf):
    """Version number stored as {.., 2: {1: version, 2: hash}} alongside a value."""
    for fn, wt, v in _sub(buf):
        if fn == 2 and wt == 2:
            for a_, b_, c_ in _sub(v):
                if a_ == 1 and b_ == 0:
                    return c_
    return 0


def _uuid_str(v):
    m = UUID_RE.search(bytes(v))
    return m.group(0).decode().lower() if m else None


def document_layout(c):
    """Read index.events.pb.

    Returns (page_keys, page_paper, papers) where
      page_keys  = {page uuid: sort key bytes}      (record 54, field 4.1)
      page_paper = {page uuid: paper uuid}           (record 54, field 3.1)
      papers     = {paper uuid: (attachment uuid, (w, h) in GoodNotes units)}  (record 2)
    """
    name = c.find("index.events.pb")
    page_keys, page_paper, papers = {}, {}, {}
    kver, pver = {}, {}
    deleted, dver = {}, {}
    if not name:
        return page_keys, page_paper, papers
    for rec in top_level_messages(c.read(name)):
        for fn, wt, v in _sub(rec):
            if wt != 2:
                continue
            if fn == 54:
                page = key = paper = None
                kv = pv = 0
                for sfn, swt, sv in _sub(v):
                    if sfn == 2 and swt == 2:
                        page = _uuid_str(sv)
                    elif sfn == 3 and swt == 2:
                        for a_, b_, c_ in _sub(sv):
                            if a_ == 1 and b_ == 2:
                                paper = _uuid_str(c_)
                        pv = _ver(sv)
                    elif sfn == 4 and swt == 2:
                        for a_, b_, c_ in _sub(sv):
                            if a_ == 1 and b_ == 2:
                                key = bytes(c_)
                        kv = _ver(sv)
                if not page:
                    continue
                if key is not None and kv >= kver.get(page, -1):
                    page_keys[page], kver[page] = key, kv
                if paper and pv >= pver.get(page, -1):
                    page_paper[page], pver[page] = paper, pv
            elif fn == 55:  # page moved: 55.2 = page, 55.3 = {1: new sort key, 2: version}
                page = key = None
                kv = 0
                for sfn, swt, sv in _sub(v):
                    if sfn == 2 and swt == 2:
                        page = _uuid_str(sv)
                    elif sfn == 3 and swt == 2:
                        for a_, b_, c_ in _sub(sv):
                            if a_ == 1 and b_ == 2:
                                key = bytes(c_)
                        kv = _ver(sv)
                if page and key is not None and kv >= kver.get(page, -1):
                    page_keys[page], kver[page] = key, kv
            elif fn == 56:  # page deleted flag: 56.2 = page, 56.3 = {1: flag, 2: version}
                page, flag, ver = None, None, 0
                for sfn, swt, sv in _sub(v):
                    if sfn == 2 and swt == 2:
                        page = _uuid_str(sv)
                    elif sfn == 3 and swt == 2:
                        for a_, b_, c_ in _sub(sv):
                            if a_ == 1 and b_ == 0:
                                flag = bool(c_)
                        ver = _ver(sv)
                if page and flag is not None and ver >= dver.get(page, -1):
                    deleted[page], dver[page] = flag, ver
            elif fn == 2:
                pid = att = None
                size = pnum = None
                for sfn, swt, sv in _sub(v):
                    if sfn == 2 and swt == 2:
                        pid = _uuid_str(sv)
                    elif sfn == 4 and swt == 2:
                        att = _uuid_str(sv)
                    elif sfn == 5 and swt == 0:
                        pnum = sv
                    elif sfn == 8 and swt == 2:
                        size = _xy(sv)
                if pid:
                    old = papers.get(pid, (None, None, None))
                    papers[pid] = (att or old[0], size or old[1], pnum or old[2] or 1)
    for page, flag in deleted.items():
        if flag:
            page_keys.pop(page, None)
            page_paper[page] = DELETED
    return page_keys, page_paper, papers


def document_outlines(c):
    """Outline entries from index.events.pb record 65.

    65.1 = page uuid, 65.2 = outline id, 65.5 = {1: title, 2: version}.
    Returns [(page uuid, title)] in the order they were created (latest title per id).
    """
    name = c.find("index.events.pb")
    if not name:
        return []
    entries, order = {}, []
    for rec in top_level_messages(c.read(name)):
        for fn, wt, v in _sub(rec):
            if fn != 65 or wt != 2:
                continue
            page = oid = title = None
            tver = 0
            for sfn, swt, sv in _sub(v):
                if sfn == 1 and swt == 2:
                    page = _uuid_str(sv)
                elif sfn == 2 and swt == 2:
                    oid = _uuid_str(sv)
                elif sfn == 5 and swt == 2:
                    for a_, b_, c_ in _sub(sv):
                        if a_ == 1 and b_ == 2:
                            title = bytes(c_).decode("utf-8", "replace")
                    tver = _ver(sv)
            if not page or not oid:
                continue
            prev = entries.get(oid)
            if prev is None:
                order.append(oid)
                entries[oid] = [page, title, tver]
            else:
                prev[0] = page
                if title is not None and tver >= prev[2]:
                    prev[1], prev[2] = title, tver
    return [(entries[o][0], entries[o][1]) for o in order if entries[o][1]]


def imported_pdf_outlines(c):
    """[(page uuid, title)] from index.events.pb record 62 (outline of imported PDFs)."""
    name = c.find("index.events.pb")
    if not name:
        return []
    entries = {}
    for rec in top_level_messages(c.read(name)):
        for fn, wt, v in _sub(rec):
            if fn != 62 or wt != 2:
                continue
            page = oid = title = None
            for sfn, swt, sv in _sub(v):
                if sfn == 1 and swt == 2:
                    page = _uuid_str(sv)
                elif sfn == 2 and swt == 2:
                    oid = _uuid_str(sv)
                elif sfn == 5 and swt == 2:
                    title = bytes(sv).decode("utf-8", "replace")
            if page and title:
                entries[oid or (page, title)] = (page, title)
    return list(entries.values())


DELETED = "__deleted__"


def _note_uuid(n):
    m = UUID_RE.search(n.split("/")[-1].encode())
    return m.group(0).decode().lower() if m else None


def _lookup(d, uid):
    """Find uid in a dict keyed by page uuid: exact match, else by 8-char prefix."""
    if not uid:
        return None
    if uid in d:
        return d[uid]
    pre = uid.replace("-", "")[:8]
    hits = [v for k, v in d.items() if k.replace("-", "")[:8] == pre]
    return hits[0] if len(hits) == 1 else None


def ordered_notes(c):
    notes = index_ordered_notes(c)
    try:
        keys, _, _ = document_layout(c)
    except Exception as e:
        print(f"  warning: could not read page order: {type(e).__name__}: {e}")
        return notes
    if not keys:
        return notes
    try:
        _, page_paper, _ = document_layout(c)
        notes = [n for n in notes if _lookup(page_paper, _note_uuid(n)) != DELETED]
    except Exception:
        pass
    pos = {n: i for i, n in enumerate(notes)}
    k = {n: _lookup(keys, _note_uuid(n)) for n in notes}
    keyed = [n for n in notes if k[n] is not None]
    rest = [n for n in notes if k[n] is None]
    keyed.sort(key=lambda n: (k[n], pos[n]))
    return keyed + rest


def index_ordered_notes(c):
    notes = c.under("notes")
    by_id = {}
    for n in notes:
        m = UUID_RE.search(n.split("/")[-1].encode())
        if m:
            by_id[m.group(0).decode().lower()] = n
    order = []
    idx = c.find("index.notes.pb")
    if idx:
        seen = set()
        for m in UUID_RE.finditer(maybe_decompress(c.read(idx))):
            u = m.group(0).decode().lower()
            if u in by_id and u not in seen:
                seen.add(u)
                order.append(by_id[u])
    rest = [n for n in notes if n not in order]
    return order + sorted(rest)


def ordered_thumbnails(c, note_names):
    thumbs = [t for t in c.under("thumbnails") if sniff(c.read(t)[:8]) in ("jpeg", "png")]
    by_id = {}
    for t in thumbs:
        m = UUID_RE.search(t.split("/")[-1].encode())
        if m:
            by_id[m.group(0).decode().lower()] = t
    order = []
    for n in note_names:
        m = UUID_RE.search(n.split("/")[-1].encode())
        if m and m.group(0).decode().lower() in by_id:
            order.append(by_id[m.group(0).decode().lower()])
    rest = [t for t in thumbs if t not in order]
    return order + sorted(rest)


def attachment_files(c):
    """{attachment id: archive path}. Uses index.attachments.pb (id -> path), since an
    attachment's id is not always its file name (e.g. pages imported from a PDF)."""
    files = {}
    for a in c.under("attachments"):
        u = _uuid_str(a.split("/")[-1].encode())
        if u:
            files[u] = a
    name = c.find("index.attachments.pb")
    if name:
        by_base = {a.split("/")[-1]: a for a in c.under("attachments")}
        for rec in top_level_messages(c.read(name)):
            aid = path = None
            for fn, wt, v in _sub(rec):
                if fn == 1 and wt == 2:
                    aid = _uuid_str(v)
                elif fn == 2 and wt == 2:
                    path = bytes(v).decode("utf-8", "replace")
            if aid and path:
                real = by_base.get(path.split("/")[-1])
                if real:
                    files[aid] = real
    return files


def load_attachments(c):
    """{attachment id: pymupdf doc} for every embedded PDF."""
    docs, opened = {}, {}
    for aid, path in attachment_files(c).items():
        if path not in opened:
            data = c.read(path)
            doc = None
            if sniff(data) == "pdf":
                try:
                    doc = pymupdf.open(stream=data, filetype="pdf")
                except Exception:
                    doc = None
            opened[path] = doc
        if opened[path] is not None:
            docs[aid] = opened[path]
    return docs


def page_background(page_data, docs, note=None, layout=None):
    """(pdf doc, page index, paper size in GoodNotes units) for a page.

    From index.events.pb: page -> paper -> (attachment, page number); falls back to a
    single-page attachment whose id appears in the page data."""
    if layout and note:
        _, page_paper, papers = layout
        paper = _lookup(page_paper, _note_uuid(note))
        if paper in papers:
            att, size, pnum = papers[paper]
            doc = docs.get(att)
            if doc is not None and 1 <= (pnum or 1) <= doc.page_count:
                return doc, (pnum or 1) - 1, size
            return None, 0, size
    if not docs:
        return None, 0, None
    found = {m.group(0).decode().lower() for m in UUID_RE.finditer(page_data)}
    hits = [k for k in docs if k in found and docs[k].page_count == 1]
    return (docs[hits[0]] if hits else None), 0, None


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------
def draw_strokes(page, strokes, scale, offset=(0, 0)):
    if not strokes:
        return
    ox, oy = offset
    P = lambda q: pymupdf.Point(q[0] * scale + ox, q[1] * scale + oy)
    # highlighter first so ink stays on top of it
    for layer in (True, False):
        shape = page.new_shape()
        n = 0
        for s in strokes:
            if bool(s.get("highlighter")) != layer:
                continue
            if "fillpath" in s:
                for st, sg in s["fillpath"]:
                    cur = P(st)
                    for c1, c2, e in sg:
                        c1, c2, e = P(c1), P(c2), P(e)
                        shape.draw_bezier(cur, c1, c2, e)
                        cur = e
                col = s["color"] or (0, 0, 0, 1)
                opacity = col[3] if len(col) > 3 else 1
                shape.finish(color=None, fill=col[:3], closePath=True,
                             fill_opacity=max(0.05, min(1, opacity)))
                n += 1
                continue
            if "segs" in s:
                cur = P(s["start"])
                if not s["segs"]:
                    shape.draw_line(cur, cur + (0.01, 0.01))
                for c, e in s["segs"]:
                    c, e = P(c), P(e)
                    # quadratic -> cubic
                    shape.draw_bezier(cur, cur + (c - cur) * (2 / 3), e + (c - e) * (2 / 3), e)
                    cur = e
            else:
                pts = [P(q) for q in s["points"]]
                if len(pts) == 1:
                    pts.append(pts[0] + (0.01, 0.01))
                shape.draw_polyline(pts)
            col = s["color"] or (0, 0, 0, 1)
            w = PEN_WIDTH if PEN_WIDTH else (s["width"] or 2.2) * scale
            opacity = col[3] if len(col) > 3 else 1
            shape.finish(color=col[:3], width=w, closePath=False,
                         lineCap=0 if layer else 1, lineJoin=1,
                         stroke_opacity=max(0.05, min(1, opacity)))
            n += 1
        if n:
            shape.commit()


def bbox(strokes):
    xs = [x for s in strokes for x, _ in s["points"]]
    ys = [y for s in strokes for _, y in s["points"]]
    return min(xs), min(ys), max(xs), max(ys)


def convert_file(src, dst, mode="auto", scale=GN_SCALE, page_size=DEFAULT_PAGE):
    t0 = time.time()
    c = Container(src)
    out = pymupdf.open()
    extra_docs = []
    try:
        notes = ordered_notes(c)
        used = mode
        total_strokes = 0

        if mode in ("auto", "ink"):
            raw = [c.read(n) for n in notes]
            per_page = [page_strokes(r) for r in raw]
            total_strokes = sum(len(s) for s in per_page)
            if total_strokes == 0 and mode == "auto" and c.under("thumbnails"):
                used = "thumbnails"
            else:
                used = "ink"
                docs = load_attachments(c)
                extra_docs = list({id(d): d for d in docs.values()}.values())
                sizes = {(round(d[0].rect.width), round(d[0].rect.height)) for d in extra_docs}
                if len(sizes) == 1 and page_size == DEFAULT_PAGE:
                    page_size = sizes.pop()
                try:
                    layout = document_layout(c)
                    if c.find("index.events.pb") and not layout[0]:
                        print(f"  warning: {Path(src).name}: page order/templates not found "
                              f"(run the 'layout' command on this file)")
                except Exception as e:
                    layout = None
                    print(f"  warning: {Path(src).name}: could not read page layout: {e}")
                image_files = load_image_files(c)
                for strokes, data, note in zip(per_page, raw, notes):
                    w, h = page_size
                    bg, bg_page, units = page_background(data, docs, note, layout)
                    if units and units[0] > 0 and units[1] > 0:
                        w, h = units[0] * scale, units[1] * scale
                    elif bg is not None:
                        w, h = bg[bg_page].rect.width, bg[bg_page].rect.height
                    elif strokes:
                        _, _, mx, my = bbox(strokes)
                        w = max(w, mx * scale + 20)
                        h = max(h, my * scale + 20)
                    page = out.new_page(width=w, height=h)
                    if bg is not None:
                        page.show_pdf_page(page.rect, bg, bg_page)
                    if image_files:
                        draw_images(page, page_images(data), image_files, scale)
                    draw_fills(page, page_fills(data), scale)
                    draw_strokes(page, strokes, scale)
                if not notes:
                    for d in extra_docs:
                        out.insert_pdf(d)

        if used == "thumbnails":
            for t in ordered_thumbnails(c, notes):
                img = c.read(t)
                pix = pymupdf.Pixmap(img)
                page = out.new_page(width=pix.width, height=pix.height)
                page.insert_image(page.rect, stream=img)

        if out.page_count == 0:
            raise RuntimeError("nothing convertible found (run `inspect` on this file)")

        if used == "ink":
            try:
                page_index = {_note_uuid(n): i for i, n in enumerate(notes)}
                toc = []
                for k, (page, title) in enumerate(document_outlines(c)):
                    i = _lookup(page_index, page)
                    if i is not None:  # outlines on deleted pages are dropped
                        toc.append((i, k, title))
                toc.sort()
                entries = [[1, title, i + 1] for i, _, title in toc]
                if PDF_OUTLINES:
                    slides = sorted((i, t) for i, t in ((_lookup(page_index, p), t)
                                    for p, t in imported_pdf_outlines(c)) if i is not None)
                    if slides:
                        entries.append([1, "Imported PDF outline", slides[0][0] + 1])
                        entries += [[2, t, i + 1] for i, t in slides]
                if entries:
                    out.set_toc(entries)
            except Exception as e:
                print(f"  warning: {Path(src).name}: could not add outline: {e}")

        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        tmp = str(dst) + ".part"
        out.save(tmp, garbage=3, deflate=True)
        os.replace(tmp, dst)
        return {"ok": True, "src": str(src), "pages": out.page_count, "strokes": total_strokes,
                "mode": used, "secs": round(time.time() - t0, 1)}
    finally:
        out.close()
        for d in extra_docs:
            d.close()
        c.close()


def _worker(args):
    global PEN_WIDTH
    if os.environ.get("GN2PDF_PEN_WIDTH"):
        PEN_WIDTH = float(os.environ["GN2PDF_PEN_WIDTH"])
    global PDF_OUTLINES
    PDF_OUTLINES = not os.environ.get("GN2PDF_NO_PDF_OUTLINES")
    src, dst, mode, scale = args
    try:
        return convert_file(src, dst, mode, scale)
    except Exception as e:
        return {"ok": False, "src": str(src), "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()}


def run_batch(inp, outdir, workers, overwrite, mode, scale):
    inp, outdir = Path(inp), Path(outdir)
    if inp.is_file():
        jobs = [(inp, outdir / (inp.stem + ".pdf"))]
    else:
        jobs = [(p, outdir / p.relative_to(inp).with_suffix(".pdf"))
                for p in sorted(inp.rglob("*.goodnotes"))]
    todo = [(s, d) for s, d in jobs if overwrite or not d.exists()]
    print(f"goodnotes2pdf {__version__}")
    print(f"Found {len(jobs)} file(s); {len(jobs) - len(todo)} already converted; "
          f"converting {len(todo)} with {workers} worker(s).\n")
    if not todo:
        return
    failures = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_worker, (str(s), str(d), mode, scale)) for s, d in todo]
        for f in as_completed(futs):
            r = f.result()
            done += 1
            name = Path(r["src"]).name
            if r["ok"]:
                print(f"[{done}/{len(todo)}] OK   {name}: {r['pages']} pages, "
                      f"{r['strokes']} strokes, mode={r['mode']}, {r['secs']}s")
            else:
                print(f"[{done}/{len(todo)}] FAIL {name}: {r['error']}")
                failures.append(r)
    if failures:
        log = outdir / "conversion_errors.log"
        outdir.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            for r in failures:
                fh.write(f"=== {r['src']}\n{r['trace']}\n")
        print(f"\n{len(failures)} failure(s). Details in {log}")
    print("\nDone.")


# --------------------------------------------------------------------------
# Inspect (diagnostic report - structure only, no note text)
# --------------------------------------------------------------------------
def field_shape(buf, depth=0, counter=None, prefix=""):
    counter = counter if counter is not None else Counter()
    try:
        fields = parse_message(buf)
    except ValueError:
        return counter
    for fn, wt, v in fields:
        key = f"{prefix}{fn}:{WT_NAMES.get(wt, wt)}"
        counter[key] += 1
        if wt == 2 and depth < 5 and len(v) > 1 and not decode_point_blob(v):
            field_shape(v, depth + 1, counter, key + ".")
        elif wt == 2 and decode_point_blob(v):
            counter[key + " <POINTS>"] += 1
    return counter


def inspect(path, report=None):
    c = Container(path)
    lines = [f"File: {path}", f"Entries: {len(c.names)}", ""]
    kinds = Counter()
    lines.append("== Entries (first 60) ==")
    for n in c.names:
        k = sniff(c.read(n)[:8])
        kinds[(n.split('/')[0] if '/' in n else '(root)', k)] += 1
    for n in c.names[:60]:
        lines.append(f"  {c.size(n):>10}  {sniff(c.read(n)[:8]):6}  {n}")
    lines.append("\n== Summary by folder/type ==")
    for (folder, k), cnt in sorted(kinds.items()):
        lines.append(f"  {folder:20} {k:8} {cnt}")

    notes = ordered_notes(c)
    lines.append(f"\n== Notes: {len(notes)} ==")
    total = 0
    for n in notes[:3]:
        data = c.read(n)
        msgs = top_level_messages(data)
        strokes = page_strokes(data)
        total += len(strokes)
        lines.append(f"\n-- {n}  ({len(data)} bytes, type={sniff(data[:8])}, "
                     f"{len(msgs)} top-level msg(s), {len(strokes)} stroke(s) detected)")
        lines.append("   first bytes: " + data[:48].hex(" "))
        shape = Counter()
        for m in msgs[:200]:
            field_shape(m, counter=shape)
        for k, v in shape.most_common(40):
            lines.append(f"   {v:>6}  {k}")
        if strokes:
            s = strokes[0]
            lines.append(f"   sample stroke: {len(s['points'])} pts, color={s['color']}, "
                         f"width={s['width']}, first={s['points'][:3]}")
    for n in notes[3:]:
        total += len(page_strokes(c.read(n)))
    lines.append(f"\nTotal strokes detected across all pages: {total}")
    c.close()
    text = "\n".join(lines)
    print(text)
    if report:
        Path(report).write_text(text, encoding="utf-8")
        print(f"\nReport written to {report}")



# --------------------------------------------------------------------------
# Dump (deep diagnostic of one page's raw records)
# --------------------------------------------------------------------------
UUID_TEXT = re.compile(r"^[0-9A-Fa-f-]{32,36}$")


def _hex(b, n=96):
    h = b[:n].hex(" ")
    return h + (" ..." if len(b) > n else "")


def _fmt_floats(vals):
    return "[" + ", ".join(f"{v:.4g}" for v in vals) + "]"


def _packed_varints(b, limit=16):
    out, pos = [], 0
    try:
        while pos < len(b) and len(out) < limit:
            v, pos = read_varint(b, pos)
            out.append(v)
    except ValueError:
        pass
    return out


def blob_trials(b, ind):
    lines = []
    for label, fn in (("zlib", lambda x: zlib.decompress(x)),
                      ("deflate", lambda x: zlib.decompress(x, -15)),
                      ("gzip", lambda x: zlib.decompress(x, 31))):
        try:
            d = fn(b)
            lines.append(f"{ind}  ** {label} OK -> {len(d)} bytes: {_hex(d, 64)}")
        except Exception:
            pass
    try:
        import lz4.block, lz4.frame
        for label, fn in (("lz4frame", lz4.frame.decompress),
                          ("lz4block", lambda x: lz4.block.decompress(x, uncompressed_size=len(x) * 20))):
            try:
                d = fn(b)
                lines.append(f"{ind}  ** {label} OK -> {len(d)} bytes: {_hex(d, 64)}")
            except Exception:
                pass
    except ImportError:
        pass
    n = len(b)
    if n >= 8:
        k = min(n // 4, 12)
        lines.append(f"{ind}  f32@0 {_fmt_floats(struct.unpack('<%df' % k, b[:k * 4]))}")
        for off in (1, 2, 3, 4, 8):
            if n - off >= 16:
                k = min((n - off) // 4, 8)
                lines.append(f"{ind}  f32@{off} {_fmt_floats(struct.unpack('<%df' % k, b[off:off + k * 4]))}")
        k = min(n // 2, 12)
        lines.append(f"{ind}  f16   {_fmt_floats(struct.unpack('<%de' % k, b[:k * 2]))}")
        lines.append(f"{ind}  i16   {list(struct.unpack('<%dh' % k, b[:k * 2]))}")
        pv = _packed_varints(b)
        lines.append(f"{ind}  varints {pv}")
        lines.append(f"{ind}  zigzag  {[(v >> 1) ^ -(v & 1) for v in pv]}")
    return lines


def dump_fields(buf, lines, depth, max_depth=8):
    ind = "    " * depth
    try:
        fields = parse_message(buf)
    except ValueError:
        return False
    for fn, wt, v in fields:
        tag = f"{ind}{fn}:{WT_NAMES.get(wt, wt)}"
        if wt == 0:
            lines.append(f"{tag} = {v}")
        elif wt == 5:
            lines.append(f"{tag} = {struct.unpack('<f', v)[0]:.6g}  (u32 {struct.unpack('<I', v)[0]})")
        elif wt == 1:
            lines.append(f"{tag} = {struct.unpack('<d', v)[0]:.6g}")
        else:
            txt = v.decode("ascii", "replace") if looks_like_text(v) else None
            if txt is not None and UUID_TEXT.match(txt):
                lines.append(f"{tag} [{len(v)}] uuid {txt[:8]}")
                continue
            if txt is not None and UUID_RE.search(v):
                ref = UUID_RE.sub(lambda m: m.group(0)[:8], bytes(v)).decode("ascii", "replace")
                ref = "".join(ch if 32 <= ord(ch) < 127 else "." for ch in ref)
                lines.append(f"{tag} [{len(v)}] ref {ref}")
                continue
            if txt is not None:
                lines.append(f"{tag} [{len(v)}] text (hidden)")
                continue
            if FULL_DUMP and v[:3] == b"bv4":
                try:
                    dec = apple_lz4_decode(v)
                    pen, runs = points_from_tpl(dec)
                    npts = sum(1 + 2 * len(sg) for _, sg in runs)
                    lines.append(f"{tag} [{len(v)}] INK decoded={len(dec)}B pen={pen} runs={len(runs)} pts={npts}")
                    sig_end = dec.find(b"\x00", 8)
                    lines.append(f"{ind}  sig {dec[8:sig_end].decode('ascii', 'replace')}")
                    lines.append(f"{ind}  head {dec[sig_end:sig_end + 48].hex(' ')}")
                    # everything after the last point run = still-unknown fields (colour etc.)
                    tail_at = len(dec)
                    if runs:
                        start, segs = runs[-1]
                        lastpt = segs[-1][1] if segs else start
                        tail_at = dec.rfind(struct.pack('<ff', *lastpt)) + 8
                    tail = dec[tail_at:]
                    lines.append(f"{ind}  tail[{len(tail)}] {tail[:400].hex(' ')}")
                    continue
                except Exception as e:
                    lines.append(f"{tag} [{len(v)}] bv4 decode failed: {e}")
            lines.append(f"{tag} [{len(v)}] {{")
            if depth >= max_depth or not v or not dump_fields(v, lines, depth + 1, max_depth):
                lines[-1] = f"{tag} [{len(v)}] OPAQUE sniff={sniff(v[:8])}"
                lines.append(f"{ind}  hex {_hex(v)}")
                lines.extend(blob_trials(v, ind))
            else:
                lines.append(f"{ind}}}")
    return True


FULL_DUMP = False


def attachment_info(c):
    """{uuid-prefix: description} for every attachment (PDFs and images)."""
    info = {}
    for a in c.under("attachments"):
        data = c.read(a)
        kind = sniff(data[:8])
        key = a.split("/")[-1][:8].upper()
        desc = f"{kind}, {len(data)} bytes"
        try:
            if kind == "pdf":
                d = pymupdf.open(stream=data, filetype="pdf")
                r = d[0].rect
                desc = f"pdf, {d.page_count} page(s), {r.width:.0f}x{r.height:.0f} pt"
                d.close()
            elif kind in ("jpeg", "png", "tiff"):
                pix = pymupdf.Pixmap(data)
                desc = f"{kind} image, {pix.width}x{pix.height} px"
        except Exception as e:
            desc += f" (could not open: {e})"
        info[key] = desc
    return info


def references(data, keys):
    found = {m.group(0).decode()[:8].upper() for m in UUID_RE.finditer(data)}
    return sorted(k for k in keys if k in found)


def dump(path, report=None, page=None, max_records=40):
    c = Container(path)
    notes = ordered_notes(c)
    att = attachment_info(c)
    refs = {n: references(c.read(n), att) for n in notes}

    if page is None:  # smallest non-trivial page is easiest to study
        cands = [n for n in notes if c.size(n) > 2000] or notes
        targets = [min(cands, key=c.size)]
    elif page == "media":  # one page using a PDF attachment, one using an image
        targets = []
        for want in ("pdf,", "image"):
            cands = [n for n in notes if any(want in att[k] for k in refs[n])
                     and n not in targets]
            if cands:
                targets.append(min(cands, key=c.size))
        if not targets:
            targets = [min(notes, key=c.size)]
    else:
        targets = [notes[int(x)] for x in str(page).split(",")]

    lines = [f"goodnotes2pdf {__version__}", f"File: {path}",
             f"Page order (index.notes.pb): {[n.split('/')[-1][:8] for n in notes]}", ""]
    lines.append("== attachments ==")
    for k, v in att.items():
        lines.append(f"  {k}: {v}")
    lines.append("\n== attachment references per page (index order) ==")
    for i, n in enumerate(notes):
        lines.append(f"  [{i:>3}] {n.split('/')[-1][:8]}  {c.size(n):>9} B  refs: {', '.join(refs[n]) or '-'}")
    lines.append("")
    for extra in ("index.notes.pb", "index.attachments.pb", "document.info.pb", "schema.pb",
                  "index.events.pb"):
        name = c.find(extra)
        if name:
            lines.append(f"== {extra} ==")
            b = c.read(name)
            recs = top_level_messages(b)
            for i, r in enumerate(recs[:400]):
                if len(recs) > 1:
                    lines.append(f"  -- {extra} record {i}")
                if not dump_fields(r, lines, 1, 6):
                    lines.append("    hex " + _hex(r, 200))
            lines.append("")

    for target in targets:
        data = c.read(target)
        msgs = top_level_messages(data)
        lines.append(f"\n######## Page file: {target} ({len(data)} bytes) refs: {', '.join(refs[target]) or '-'}")
        lines.append(f"== {len(msgs)} top-level records; showing first {min(max_records, len(msgs))} ==")
        for i, m in enumerate(msgs[:max_records]):
            lines.append(f"\n--- record {i} ({len(m)} bytes) ---")
            if not dump_fields(m, lines, 1):
                lines.append(f"    NOT PROTOBUF sniff={sniff(m[:8])}")
                lines.append("    hex " + _hex(m, 160))
                lines.extend(blob_trials(m, "    "))
    c.close()
    text = "\n".join(lines)
    print(text)
    if report:
        Path(report).write_text(text, encoding="utf-8")
        print(f"\nReport written to {report}")


def layout_report(path):
    """Diagnose page order / paper template detection. Prints no note content."""
    c = Container(path)
    print(f"goodnotes2pdf {__version__}")
    print(f"File: {path}")
    name = c.find("index.events.pb")
    print(f"events file: {name!r}")
    if not name:
        return
    data = c.read(name)
    print(f"size {len(data)} B, first bytes {data[:16].hex(' ')}, sniff={sniff(data[:8])}")
    try:
        recs = top_level_messages(data)
        print(f"records: {len(recs)}")
        hist = Counter()
        n54 = 0
        for r in recs:
            for fn, wt, v in _sub(r):
                hist[(fn, wt)] += 1
                if fn == 54 and wt == 2 and n54 < 3:
                    n54 += 1
                    print(f"  sample 54 record, subfields: {[(a, b, len(x) if isinstance(x, (bytes, bytearray, memoryview)) else x) for a, b, x in _sub(v)]}")
                    for sfn, swt, sv in _sub(v):
                        if sfn in (3, 4) and swt == 2:
                            print(f"     54.{sfn} -> {[(a, b, bytes(x)[:40].hex(' ') if isinstance(x, (bytes, bytearray, memoryview)) else x) for a, b, x in _sub(sv)]}")
        print(f"top-level field histogram: {sorted(hist.items())}")
    except Exception:
        traceback.print_exc()
    try:
        keys, page_paper, papers = document_layout(c)
        print(f"\npages with sort keys: {len(keys)}, with paper: {len(page_paper)}, papers: {len(papers)}")
        for u, k in sorted(keys.items(), key=lambda kv: kv[1]):
            print(f"  {u[:8]}  key={k!r}  paper={page_paper.get(u, '-')[:8]}")
        for pid, (att, size, *_) in papers.items():
            print(f"  paper {pid[:8]} -> attachment {str(att)[:8]}, size {size}")
        print("\nmatching page files to page records:")
        for n in index_ordered_notes(c):
            u = _note_uuid(n)
            exact = u in keys
            close = [k for k in keys if k[:8] == (u or "")[:8]]
            print(f"  file {n!r} -> uuid {u!r} exact={exact} record={close[0] if close else None!r}")
        notes = ordered_notes(c)
        print(f"\nfinal page order: {[n.split('/')[-1][:8] for n in notes]}")
        docs = load_attachments(c)
        print(f"attachments loaded: {[k[:8] for k in docs]}")
        page_index = {_note_uuid(n): i for i, n in enumerate(notes)}
        print("outline:")
        for page, title in document_outlines(c):
            i = _lookup(page_index, page)
            print(f"  page {i + 1 if i is not None else '(deleted page)'}: {title}")
    except Exception:
        traceback.print_exc()
    c.close()


# --------------------------------------------------------------------------
def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    root = tk.Tk()
    root.withdraw()
    inp = filedialog.askdirectory(title="Folder containing .goodnotes files")
    if not inp:
        return
    out = filedialog.askdirectory(title="Folder to save PDFs into")
    if not out:
        return
    run_batch(inp, out, max(1, (os.cpu_count() or 2) - 1), False, "auto", GN_SCALE)
    messagebox.showinfo("goodnotes2pdf", "Finished. See the console window for details.")


def main():
    if len(sys.argv) == 1:
        gui()
        return
    ap = argparse.ArgumentParser(description="Convert .goodnotes files to PDF")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cv = sub.add_parser("convert", help="convert a file or a folder tree")
    cv.add_argument("input")
    cv.add_argument("output_dir")
    cv.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    cv.add_argument("--overwrite", action="store_true")
    cv.add_argument("--mode", choices=["auto", "ink", "thumbnails"], default="auto",
                    help="auto: vector ink, falling back to thumbnails if no ink is decoded")
    cv.add_argument("--scale", type=float, default=GN_SCALE,
                    help="GoodNotes units -> PDF points (default 72/132)")
    cv.add_argument("--no-pdf-outlines", action="store_true",
                    help="leave out the outline entries that came inside imported PDFs")
    cv.add_argument("--pen-width", type=float, default=None,
                    help="force one line width in points (default: width stored per stroke)")
    ly = sub.add_parser("layout", help="diagnose page order and paper templates")
    ly.add_argument("file")
    ins = sub.add_parser("inspect", help="print a structural report for one file")
    ins.add_argument("file")
    ins.add_argument("--report")
    dp = sub.add_parser("dump", help="deep dump of one page's raw records")
    dp.add_argument("file")
    dp.add_argument("--report")
    dp.add_argument("--page", help="page index, comma list, or 'media' "
                                   "(auto-pick pages with embedded PDFs/images); default: smallest page")
    dp.add_argument("--records", type=int, default=40)
    dp.add_argument("--full", action="store_true", help="decode ink blobs and show unknown fields")
    a = ap.parse_args()
    if a.cmd == "convert":
        if a.pen_width:
            os.environ["GN2PDF_PEN_WIDTH"] = str(a.pen_width)
        if a.no_pdf_outlines:
            os.environ["GN2PDF_NO_PDF_OUTLINES"] = "1"
        run_batch(a.input, a.output_dir, a.workers, a.overwrite, a.mode, a.scale)
    elif a.cmd == "layout":
        layout_report(a.file)
    elif a.cmd == "dump":
        global FULL_DUMP
        FULL_DUMP = a.full
        dump(a.file, a.report, a.page, a.records)
    else:
        inspect(a.file, a.report)


if __name__ == "__main__":
    main()