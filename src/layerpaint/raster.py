"""Raster storage, painting primitives and zero-dependency PNG I/O.

A :class:`Raster` is an 8-bit straight (non-premultiplied) sRGB RGBA buffer.
Bytes are the storage format only; compositing reads and writes normalized
floats so every arithmetic step is defined in [0, 1] space.
"""

import struct
import zlib

from .colorspace import byte_to_unit, clamp01, unit_to_byte


class Raster:
    def __init__(self, width, height, data=None):
        if width <= 0 or height <= 0:
            raise ValueError("raster dimensions must be positive")
        self.width = int(width)
        self.height = int(height)
        n = self.width * self.height * 4
        self.data = bytearray(data) if data is not None else bytearray(n)
        if len(self.data) != n:
            raise ValueError("data length does not match raster dimensions")

    # ------------------------------------------------------------- basic API

    def clone(self):
        return Raster(self.width, self.height, self.data)

    def index(self, x, y):
        return (y * self.width + x) * 4

    def get_byte(self, x, y):
        i = self.index(x, y)
        return tuple(self.data[i:i + 4])

    def set_byte(self, x, y, rgba):
        i = self.index(x, y)
        self.data[i:i + 4] = bytes(rgba)

    def get_unit(self, x, y):
        i = self.index(x, y)
        d = self.data
        return (
            d[i] / 255.0,
            d[i + 1] / 255.0,
            d[i + 2] / 255.0,
            d[i + 3] / 255.0,
        )

    def set_unit(self, x, y, rgba):
        i = self.index(x, y)
        d = self.data
        d[i] = unit_to_byte(rgba[0])
        d[i + 1] = unit_to_byte(rgba[1])
        d[i + 2] = unit_to_byte(rgba[2])
        d[i + 3] = unit_to_byte(rgba[3])

    def fill(self, rgba_bytes):
        row = bytes(rgba_bytes) * self.width
        for y in range(self.height):
            self.data[y * self.width * 4:(y + 1) * self.width * 4] = row

    def blit(self, other, ox=0, oy=0):
        """Copy ``other`` on top at integer offset (straight replace)."""
        for sy in range(other.height):
            ty = sy + oy
            if not 0 <= ty < self.height:
                continue
            for sx in range(other.width):
                tx = sx + ox
                if 0 <= tx < self.width:
                    self.set_byte(tx, ty, other.get_byte(sx, sy))

    # -------------------------------------------------------- alpha semantics

    def strip_hidden_color(self):
        """Force RGB to black wherever alpha is zero.

        Storage invariant: transparent pixels hold (0,0,0,0).  This makes the
        "hidden color pollutes edges" class of bug impossible by construction.
        """
        d = self.data
        for i in range(0, len(d), 4):
            if d[i + 3] == 0:
                d[i] = d[i + 1] = d[i + 2] = 0

    def multiply_alpha(self, mask):
        """Alpha-mask this raster in place; mask is a grayscale Raster."""
        if (mask.width, mask.height) != (self.width, self.height):
            raise ValueError("mask dimensions must match layer dimensions")
        d, m = self.data, mask.data
        for i in range(0, len(d), 4):
            d[i + 3] = (d[i + 3] * m[i]) // 255
        self.strip_hidden_color()

    # ------------------------------------------------------- paint primitives

    def stroke(self, points, color, radius, hardness=0.5):
        """Anti-aliased soft round brush stroke.

        ``color`` is straight RGBA bytes; ``hardness`` in [0,1] maps the
        brush's linear-falloff core radius.  Source-over compositing per
        stamp, evaluated in straight-alpha float space.  Low opacity and soft
        edges are first-class here, not an afterthought.
        """
        if len(points) == 0:
            return
        r = max(float(radius), 0.5)
        hardness = clamp01(hardness)
        cr, cg, cb, ca = (byte_to_unit(c) for c in color)
        stamps = _stamp_centers(points, max(r * 0.25, 0.5))
        for cx, cy in stamps:
            self._stamp(cx, cy, r, hardness, cr, cg, cb, ca)
        self.strip_hidden_color()

    def _stamp(self, cx, cy, r, hardness, cr, cg, cb, ca):
        x0 = max(0, int(cx - r - 1))
        x1 = min(self.width - 1, int(cx + r + 1))
        y0 = max(0, int(cy - r - 1))
        y1 = min(self.height - 1, int(cy + r + 1))
        core = r * hardness
        r2 = r * r
        d = self.data
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                dx = x + 0.5 - cx
                dy = y + 0.5 - cy
                dist2 = dx * dx + dy * dy
                if dist2 > r2:
                    continue
                dist = dist2 ** 0.5
                if dist <= core:
                    coverage = 1.0
                else:
                    coverage = max(0.0, (r - dist) / max(r - core, 1e-6))
                sa = ca * coverage
                if sa <= 0.0:
                    continue
                i = self.index(x, y)
                da = d[i + 3] / 255.0
                out_a = sa + da * (1.0 - sa)
                if out_a <= 1e-10:
                    continue
                # source-over in straight space
                sr, sg, sb = cr, cg, cb
                br = d[i] / 255.0
                bg = d[i + 1] / 255.0
                bb = d[i + 2] / 255.0
                d[i] = unit_to_byte((sr * sa + br * da * (1 - sa)) / out_a)
                d[i + 1] = unit_to_byte((sg * sa + bg * da * (1 - sa)) / out_a)
                d[i + 2] = unit_to_byte((sb * sa + bb * da * (1 - sa)) / out_a)
                d[i + 3] = unit_to_byte(out_a)


def _stamp_centers(points, step):
    """Interpolate brush centers along a polyline so fast drags stay solid."""
    centers = [(float(points[0][0]), float(points[0][1]))]
    for p, q in zip(points, points[1:]):
        x0, y0 = centers[-1]
        x1, y1 = float(q[0]), float(q[1])
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        n = max(1, int(dist // step))
        for k in range(1, n + 1):
            t = k / n
            centers.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return centers


# ----------------------------------------------------------------- PNG output

def write_png(target, raster):
    """Serialize an RGBA8 raster to a PNG path or writable binary stream."""
    raw = bytearray()
    stride = raster.width * 4
    for y in range(raster.height):
        raw.append(0)  # filter type 0 (none)
        raw.extend(raster.data[y * stride:(y + 1) * stride])
    compressed = zlib.compress(bytes(raw), 9)

    def chunk(tag, payload):
        out = struct.pack(">I", len(payload)) + tag + payload
        return out + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", raster.width, raster.height, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    if hasattr(target, "write"):
        target.write(png)
    else:
        with open(target, "wb") as f:
            f.write(png)


def read_png(source):
    """Minimal RGBA8/RGB8 PNG reader for paths, streams or bytes."""
    if isinstance(source, (bytes, bytearray)):
        blob = bytes(source)
    elif hasattr(source, "read"):
        blob = source.read()
    else:
        with open(source, "rb") as f:
            blob = f.read()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        payload = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            break
    if bit_depth != 8 or color_type not in (2, 6):
        raise ValueError("only 8-bit RGB/RGBA PNGs are supported")
    channels = 3 if color_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    raster = Raster(width, height)
    prev = bytearray(stride)
    rp = 0
    for y in range(height):
        filt = raw[rp]
        rp += 1
        line = bytearray(raw[rp:rp + stride])
        rp += stride
        _unfilter(filt, line, prev, channels)
        for x in range(width):
            si = x * channels
            di = (y * width + x) * 4
            raster.data[di] = line[si]
            raster.data[di + 1] = line[si + 1]
            raster.data[di + 2] = line[si + 2]
            raster.data[di + 3] = line[si + 3] if channels == 4 else 255
        prev = line
    return raster


def _unfilter(filt, line, prev, channels):
    if filt == 0:
        return
    if filt == 1:  # sub
        for i in range(channels, len(line)):
            line[i] = (line[i] + line[i - channels]) & 0xFF
    elif filt == 2:  # up
        for i in range(len(line)):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif filt == 3:  # average
        for i in range(len(line)):
            a = line[i - channels] if i >= channels else 0
            line[i] = (line[i] + (a + prev[i]) // 2) & 0xFF
    elif filt == 4:  # paeth
        for i in range(len(line)):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            if pa <= pb and pa <= pc:
                pr = a
            elif pb <= pc:
                pr = b
            else:
                pr = c
            line[i] = (line[i] + pr) & 0xFF
    else:
        raise ValueError("unknown PNG filter type %d" % filt)


def solid_raster(width, height, rgba_bytes):
    r = Raster(width, height)
    r.fill(rgba_bytes)
    return r
