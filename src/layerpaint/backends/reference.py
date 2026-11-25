"""CPU reference backend: one float operation per W3C formula per pixel.

This is the definitional implementation.  Every output is computed from the
W3C Compositing and Blending Level 1 equations in normalized straight-alpha
float space; bytes only appear at the storage boundary.  The accelerated
backend is validated against this one.
"""

from ..blend import blend_pixel
from ..colorspace import clamp01, premultiply, unpremultiply, unit_to_byte
from ..raster import Raster
from . import intersect_rect


class ReferenceBackend:
    name = "cpu-reference"

    # ------------------------------------------------------------ composite

    def composite(self, backdrop, source, mode, opacity, rect=None):
        """W3C blend + alpha compositing of ``source`` over ``backdrop``."""
        out = backdrop.clone()
        box = intersect_rect(rect, backdrop.width, backdrop.height)
        if box is None or opacity <= 0.0:
            return out
        x0, y0, x1, y1 = box
        bd, sd, od = backdrop.data, source.data, out.data
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * backdrop.width + x) * 4
                da = bd[i + 3] / 255.0
                sa = (sd[i + 3] / 255.0) * opacity
                if sa <= 0.0:
                    continue
                if da <= 0.0:
                    # Empty backdrop: source color passes through unchanged;
                    # its straight color is already defined (zero alpha stores
                    # black, so no hidden color can leak).
                    for k in range(3):
                        od[i + k] = sd[i + k]
                    od[i + 3] = unit_to_byte(sa)
                    continue
                cs = (sd[i] / 255.0, sd[i + 1] / 255.0, sd[i + 2] / 255.0)
                cb = (bd[i] / 255.0, bd[i + 1] / 255.0, bd[i + 2] / 255.0)
                co = blend_pixel(mode, cb, cs)
                # W3C section 13: composite in premultiplied space.
                co_p = tuple(c * sa for c in co)
                cb_p = tuple(c * da for c in cb)
                ao = sa + da * (1.0 - sa)
                out_p = tuple(
                    co_p[k] + cb_p[k] * (1.0 - sa) for k in range(3)
                )
                straight = unpremultiply(out_p[0], out_p[1], out_p[2], ao)
                for k in range(4):
                    od[i + k] = unit_to_byte(straight[k])
        return out

    # --------------------------------------------------------- mask / clipping

    def apply_mask(self, raster, mask, rect=None):
        out = raster.clone()
        box = intersect_rect(rect, raster.width, raster.height)
        if box is None:
            return out
        x0, y0, x1, y1 = box
        od, md = out.data, mask.data
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * raster.width + x) * 4
                new_a = (od[i + 3] / 255.0) * (md[i] / 255.0)
                od[i + 3] = unit_to_byte(new_a)
                if od[i + 3] == 0:
                    od[i] = od[i + 1] = od[i + 2] = 0
        return out

    def clip_lerp(self, backdrop, over_render, base_alpha, rect=None):
        """Uniform clipping rule.

        ``over_render`` is the clipped node's own render against transparency
        (not yet combined with the backdrop), ``backdrop`` is the saved
        backdrop below the clipping base, and ``base_alpha`` is the base
        node's own alpha raster:

            result = base_alpha * over_render
                     + (1 - base_alpha) * backdrop

        in premultiplied space.  Mask 1 keeps the node's result, mask 0
        restores the untouched backdrop, so the node can never write color
        outside the base layer's own alpha footprint.
        """
        out = backdrop.clone()
        box = intersect_rect(rect, backdrop.width, backdrop.height)
        if box is None:
            return out
        x0, y0, x1, y1 = box
        bd, vd, od = backdrop.data, over_render.data, out.data
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * backdrop.width + x) * 4
                m = base_alpha.data[i + 3] / 255.0
                if m <= 1e-12:
                    if od[i + 3] == 0:
                        od[i] = od[i + 1] = od[i + 2] = 0
                    continue
                if m >= 1.0 - 1e-12:
                    od[i:i + 4] = vd[i:i + 4]
                    continue
                a_b = bd[i + 3] / 255.0
                a_v = vd[i + 3] / 255.0
                bp = premultiply(
                    bd[i] / 255.0, bd[i + 1] / 255.0,
                    bd[i + 2] / 255.0, a_b,
                )
                vp = premultiply(
                    vd[i] / 255.0, vd[i + 1] / 255.0,
                    vd[i + 2] / 255.0, a_v,
                )
                rp = tuple(m * vp[k] + (1.0 - m) * bp[k] for k in range(4))
                straight = unpremultiply(*rp)
                for k in range(4):
                    od[i + k] = unit_to_byte(straight[k])
        return out

    def empty_raster(self, width, height):
        return Raster(width, height)
