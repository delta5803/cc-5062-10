"""Backend primitives shared by the CPU reference and the accelerated path.

A backend implements three raster-level operations over an optional pixel
rectangle:

* ``composite`` - source over backdrop with a W3C blend mode and opacity;
* ``apply_mask`` - multiply alpha by a grayscale mask;
* ``clip_lerp`` - restrict a composited result to a clipping base by
  premultiplied-space linear interpolation (the uniform clipping rule used for
  layers, groups and blend modes alike).

All inputs and outputs are straight-alpha RGBA8 rasters.
"""

from ..raster import Raster


def intersect_rect(rect, width, height):
    if rect is None:
        return (0, 0, width, height)
    x0, y0, x1, y1 = rect
    x0 = max(0, min(x0, width))
    y0 = max(0, min(y0, height))
    x1 = max(0, min(x1, width))
    y1 = max(0, min(y1, height))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)
