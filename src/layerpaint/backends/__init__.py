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


def rect_fully_transparent(data, width, box):
    """True if every source alpha byte inside ``box`` is zero.

    A transparent source cannot change a backdrop, so callers can skip the
    per-pixel blend loop entirely.  The scan reads only the alpha channel of
    the requested rectangle, not the whole raster.
    """
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        row = (y * width + x0) * 4 + 3
        for i in range(row, row + (x1 - x0) * 4, 4):
            if data[i] != 0:
                return False
    return True
