"""W3C Compositing and Blending Level 1 blend modes.

Every function operates on straight (non-premultiplied) sRGB color components
in [0, 1].  The set is the complete W3C list: ``normal`` plus eleven separable
modes and four non-separable modes.
"""

import math

from .colorspace import clamp01


# ---------------------------------------------------------------- separable

def _multiply(cb, cs):
    return cb * cs


def _screen(cb, cs):
    return cb + cs - cb * cs


def _overlay(cb, cs):
    if cb <= 0.5:
        return 2 * cb * cs
    return 1 - 2 * (1 - cb) * (1 - cs)


def _darken(cb, cs):
    return min(cb, cs)


def _lighten(cb, cs):
    return max(cb, cs)


def _color_dodge(cb, cs):
    if cs >= 1.0:
        return 1.0
    return min(1.0, cb / (1.0 - cs))


def _color_burn(cb, cs):
    if cs <= 0.0:
        return 0.0
    return 1.0 - min(1.0, (1.0 - cb) / cs)


def _hard_light(cb, cs):
    if cs <= 0.5:
        return _multiply(cb, 2 * cs)
    return _screen(cb, 2 * cs - 1)


def _soft_light(cb, cs):
    if cs <= 0.5:
        return cb - (1 - 2 * cs) * cb * (1 - cb)
    if cb <= 0.25:
        d = ((16 * cb - 12) * cb + 4) * cb
    else:
        d = math.sqrt(cb)
    return cb + (2 * cs - 1) * (d - cb)


def _difference(cb, cs):
    return abs(cb - cs)


def _exclusion(cb, cs):
    return cb + cs - 2 * cb * cs


SEPARABLE = {
    "multiply": _multiply,
    "screen": _screen,
    "overlay": _overlay,
    "darken": _darken,
    "lighten": _lighten,
    "color-dodge": _color_dodge,
    "color-burn": _color_burn,
    "hard-light": _hard_light,
    "soft-light": _soft_light,
    "difference": _difference,
    "exclusion": _exclusion,
}


# ------------------------------------------------------------- non-separable

def _luminosity(r, g, b):
    return 0.3 * r + 0.59 * g + 0.11 * b


def _saturation(r, g, b):
    return max(r, g, b) - min(r, g, b)


def _clip_color(r, g, b):
    l = _luminosity(r, g, b)
    n = min(r, g, b)
    x = max(r, g, b)
    if n < 0.0:
        r = l + (((r - l) * l) / (l - n))
        g = l + (((g - l) * l) / (l - n))
        b = l + (((b - l) * l) / (l - n))
    if x > 1.0:
        r = l + (((r - l) * (1 - l)) / (x - l))
        g = l + (((g - l) * (1 - l)) / (x - l))
        b = l + (((b - l) * (1 - l)) / (x - l))
    return r, g, b


def _set_luminosity(r, g, b, l):
    d = l - _luminosity(r, g, b)
    return _clip_color(r + d, g + d, b + d)


def _set_saturation(r, g, b, s):
    channels = [r, g, b]
    hi = max(range(3), key=channels.__getitem__)
    lo = min(range(3), key=channels.__getitem__)
    if hi == lo:
        return 0.0, 0.0, 0.0
    d = channels[hi] - channels[lo]
    out = [0.0, 0.0, 0.0]
    out[hi] = s
    out[lo] = 0.0
    mid = 3 - hi - lo
    out[mid] = ((channels[mid] - channels[lo]) * s) / d
    return out[0], out[1], out[2]


def _hue(cb, cs):
    return _set_luminosity(
        *_set_saturation(cs[0], cs[1], cs[2], _saturation(*cb)),
        _luminosity(*cb),
    )


def _saturation_mode(cb, cs):
    return _set_luminosity(
        *_set_saturation(cb[0], cb[1], cb[2], _saturation(*cs)),
        _luminosity(*cb),
    )


def _color_mode(cb, cs):
    return _set_luminosity(cs[0], cs[1], cs[2], _luminosity(*cb))


def _luminosity_mode(cb, cs):
    return _set_luminosity(cb[0], cb[1], cb[2], _luminosity(*cs))


NON_SEPARABLE = {
    "hue": _hue,
    "saturation": _saturation_mode,
    "color": _color_mode,
    "luminosity": _luminosity_mode,
}

BLEND_MODES = set(SEPARABLE) | set(NON_SEPARABLE) | {"normal"}


def blend_pixel(mode, cb_rgb, cs_rgb):
    """Blended straight RGB tuple for one backdrop/source pixel pair."""
    if mode == "normal":
        return tuple(cs_rgb)
    if mode in SEPARABLE:
        fn = SEPARABLE[mode]
        return tuple(fn(cb, cs) for cb, cs in zip(cb_rgb, cs_rgb))
    return NON_SEPARABLE[mode](tuple(cb_rgb), tuple(cs_rgb))
