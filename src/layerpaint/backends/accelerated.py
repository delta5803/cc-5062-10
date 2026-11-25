"""Accelerated backend: tight float loops with pre-resolved blend functions.

The reference backend resolves blend modes, dictionary lookups and tuple
allocations for every pixel.  This backend resolves each mode's channel
function once per call, keeps arithmetic on normalized floats in tight loops,
and only quantizes at the storage boundary - matching the reference within one
byte while removing per-pixel dispatch overhead.  Non-separable modes delegate
to the reference math (they are inherently per-pixel and cannot be reduced to
a channel function); clipping uses integer weights with an exact final
divide.
"""

from ..blend import NON_SEPARABLE, SEPARABLE
from ..colorspace import unit_to_byte
from ..raster import Raster
from . import intersect_rect
from .reference import ReferenceBackend


class AcceleratedBackend:
    name = "accelerated-lut"

    def __init__(self):
        self._ref = ReferenceBackend()

    # ------------------------------------------------------------ composite

    def composite(self, backdrop, source, mode, opacity, rect=None):
        box = intersect_rect(rect, backdrop.width, backdrop.height)
        if box is None or opacity <= 0.0:
            return backdrop.clone()
        if mode in NON_SEPARABLE:
            return self._ref.composite(backdrop, source, mode, opacity, rect)
        out = backdrop.clone()
        fn = None if mode == "normal" else SEPARABLE[mode]
        x0, y0, x1, y1 = box
        bd, sd, od = backdrop.data, source.data, out.data
        w = backdrop.width
        for y in range(y0, y1):
            row = y * w
            for x in range(x0, x1):
                i = (row + x) * 4
                sa = (sd[i + 3] / 255.0) * opacity
                if sa <= 0.0:
                    continue
                da255 = bd[i + 3]
                if da255 == 0:
                    od[i] = sd[i]
                    od[i + 1] = sd[i + 1]
                    od[i + 2] = sd[i + 2]
                    od[i + 3] = unit_to_byte(sa)
                    continue
                da = da255 / 255.0
                ao = sa + da * (1.0 - sa)
                inv = 1.0 - sa
                if fn is None:
                    for k in range(3):
                        cs = sd[i + k] / 255.0
                        cb = bd[i + k] / 255.0
                        pre = cs * sa + cb * da * inv
                        od[i + k] = unit_to_byte(pre / ao)
                else:
                    for k in range(3):
                        cs = sd[i + k] / 255.0
                        cb = bd[i + k] / 255.0
                        blended = fn(cb, cs)
                        pre = blended * sa + cb * da * inv
                        od[i + k] = unit_to_byte(pre / ao)
                od[i + 3] = unit_to_byte(ao)
        return out

    # --------------------------------------------------------- mask / clipping

    def apply_mask(self, raster, mask, rect=None):
        out = raster.clone()
        box = intersect_rect(rect, raster.width, raster.height)
        if box is None:
            return out
        x0, y0, x1, y1 = box
        od, md = out.data, mask.data
        w = raster.width
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * w + x) * 4
                new_a = unit_to_byte((od[i + 3] / 255.0) * (md[i] / 255.0))
                od[i + 3] = new_a
                if new_a == 0:
                    od[i] = od[i + 1] = od[i + 2] = 0
        return out

    def clip_lerp(self, backdrop, over_render, base_alpha, rect=None):
        # Premultiplied lerp m*over + (1-m)*backdrop; integer mask weights,
        # exact float unpremultiply, so low-alpha pixels stay precise.
        out = backdrop.clone()
        box = intersect_rect(rect, backdrop.width, backdrop.height)
        if box is None:
            return out
        x0, y0, x1, y1 = box
        bd, vd, od = backdrop.data, over_render.data, out.data
        w = backdrop.width
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * w + x) * 4
                m255 = base_alpha.data[i + 3]
                if m255 == 255:
                    od[i:i + 4] = vd[i:i + 4]
                    continue
                if m255 == 0:
                    # Keep the backdrop, but a transparent pixel must hold no
                    # hidden color (straight-alpha storage invariant).
                    if od[i + 3] == 0:
                        od[i] = od[i + 1] = od[i + 2] = 0
                    continue
                m = m255 / 255.0
                inv = 1.0 - m
                a_b = bd[i + 3] / 255.0
                a_v = vd[i + 3] / 255.0
                ao = m * a_v + inv * a_b
                od[i + 3] = unit_to_byte(ao)
                if od[i + 3] == 0:
                    od[i] = od[i + 1] = od[i + 2] = 0
                else:
                    for k in range(3):
                        bp = (bd[i + k] / 255.0) * a_b
                        vp = (vd[i + k] / 255.0) * a_v
                        od[i + k] = unit_to_byte((m * vp + inv * bp) / ao)
        return out

    def empty_raster(self, width, height):
        return Raster(width, height)
