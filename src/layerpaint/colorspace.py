"""Color space and alpha representation conventions.

All raster storage uses sRGB-encoded, non-premultiplied (straight) RGBA with
8-bit channels.  Arithmetic happens on normalized floats in [0, 1] and is
quantized back to bytes only at output boundaries.  Blend math follows the W3C
Compositing and Blending Level 1 convention, which is defined on gamma-encoded
sRGB values; ``srgb_to_linear`` / ``linear_to_srgb`` are provided explicitly so
no conversion happens implicitly.

The hard rule implemented here: a pixel with alpha 0 carries no color.
Un-premultiplying a zero-alpha pixel yields black, never a hidden chroma value,
so fully transparent pixels cannot tint neighboring edges through arithmetic.
"""

ALPHA_EPSILON = 1e-10


def clamp01(x):
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def byte_to_unit(v):
    return v / 255.0


def unit_to_byte(x):
    return int(clamp01(x) * 255.0 + 0.5)


def srgb_to_linear(c):
    """Explicit sRGB EOTF inverse; blending itself does not call this."""
    c = clamp01(c)
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c):
    """Explicit sRGB OETF; provided so conversions stay auditable."""
    c = clamp01(c)
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * c ** (1.0 / 2.4) - 0.055


def premultiply(r, g, b, a):
    """Straight RGBA -> premultiplied RGB (alpha returned unchanged)."""
    return r * a, g * a, b * a, a


def unpremultiply(pr, pg, pb, a):
    """Premultiplied RGB -> straight RGBA.

    Zero (or near-zero) alpha resolves to black straight color by definition.
    This is the guard against hidden-color pollution at transparent pixels.
    """
    if a <= ALPHA_EPSILON:
        return 0.0, 0.0, 0.0, 0.0
    return (
        clamp01(pr / a),
        clamp01(pg / a),
        clamp01(pb / a),
        a,
    )


def straight_pixel_to_bytes(r, g, b, a):
    return (
        unit_to_byte(r),
        unit_to_byte(g),
        unit_to_byte(b),
        unit_to_byte(a),
    )
