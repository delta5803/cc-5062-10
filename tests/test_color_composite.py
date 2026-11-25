"""Hand-computable 1x1/2x2 cases for the reference backend."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.colorspace import premultiply, unpremultiply  # noqa: E402
from layerpaint.backends.reference import ReferenceBackend  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402


def px(raster, x=0, y=0):
    return raster.get_byte(x, y)


class TestColorspace(unittest.TestCase):
    def test_unpremultiply_zero_alpha_is_black(self):
        # A poisoned premultiplied triplet with zero alpha must never surface.
        self.assertEqual(unpremultiply(0.3, 0.2, 0.1, 0.0), (0.0, 0.0, 0.0, 0.0))

    def test_premultiply_roundtrip(self):
        r, g, b, a = premultiply(0.8, 0.4, 0.2, 0.5)
        self.assertAlmostEqual(r, 0.4)
        self.assertAlmostEqual(g, 0.2)
        self.assertAlmostEqual(b, 0.1)
        out = unpremultiply(r, g, b, a)
        self.assertAlmostEqual(out[0], 0.8)
        self.assertAlmostEqual(out[1], 0.4)
        self.assertAlmostEqual(out[2], 0.2)
        self.assertAlmostEqual(out[3], 0.5)


class TestReferenceComposite(unittest.TestCase):
    def setUp(self):
        self.b = ReferenceBackend()

    def test_source_over_transparent_keeps_source(self):
        backdrop = Raster(1, 1)
        source = Raster(1, 1, bytearray([200, 100, 50, 128]))
        out = self.b.composite(backdrop, source, "normal", 1.0)
        self.assertEqual(px(out), (200, 100, 50, 128))

    def test_source_over_opaque_hand_computed(self):
        # Backdrop 50% red, source 50% blue, both alpha 0.5.
        backdrop = Raster(1, 1, bytearray([255, 0, 0, 128]))
        source = Raster(1, 1, bytearray([0, 0, 255, 128]))
        out = self.b.composite(backdrop, source, "normal", 1.0)
        # ao = 0.5 + 0.5*0.5 = 0.75
        # preR = 0*0.5 + 1*0.5*0.5 = 0.25 -> straight R = 1/3
        # preB = 1*0.5 + 0 = 0.5 -> straight B = 2/3
        r, g, bl, a = px(out)
        self.assertEqual(a, 192)          # floor(0.75*255+0.5)
        self.assertAlmostEqual(r / 255.0, 1 / 3, delta=1 / 255)
        self.assertEqual(g, 0)
        self.assertAlmostEqual(bl / 255.0, 2 / 3, delta=1 / 255)

    def test_layer_opacity_scales_alpha_only(self):
        backdrop = Raster(1, 1, bytearray([255, 255, 255, 255]))
        source = Raster(1, 1, bytearray([0, 0, 0, 255]))
        half = self.b.composite(backdrop, source, "normal", 0.5)
        # 50% black over white = gray 128.
        self.assertEqual(px(half), (128, 128, 128, 255))

    def test_multiply_hand_computed(self):
        # multiply(0.8, 0.5) = 0.4, both opaque.
        backdrop = Raster(1, 1, bytearray([204, 204, 204, 255]))
        source = Raster(1, 1, bytearray([128, 128, 128, 255]))
        out = self.b.composite(backdrop, source, "multiply", 1.0)
        self.assertEqual(px(out)[0], 102)  # round(0.4*255)
        self.assertEqual(px(out)[3], 255)

    def test_screen_hand_computed(self):
        backdrop = Raster(1, 1, bytearray([128, 128, 128, 255]))
        source = Raster(1, 1, bytearray([128, 128, 128, 255]))
        out = self.b.composite(backdrop, source, "screen", 1.0)
        # 0.502+0.502-0.252 = 0.752 -> 192
        self.assertAlmostEqual(px(out)[0], 192, delta=1)

    def test_transparent_pixel_does_not_pollute_edge(self):
        # Hidden red behind alpha 0 must composite to nothing over white.
        backdrop = Raster(1, 1, bytearray([255, 255, 255, 255]))
        source = Raster(1, 1, bytearray([255, 0, 0, 0]))
        out = self.b.composite(backdrop, source, "multiply", 1.0)
        self.assertEqual(px(out), (255, 255, 255, 255))

    def test_rect_scoped_composite_leaves_outside_untouched(self):
        backdrop = Raster(2, 2, bytearray([10, 10, 10, 255] * 4))
        source = Raster(2, 2, bytearray([200, 200, 200, 255] * 4))
        out = self.b.composite(backdrop, source, "normal", 1.0, rect=(0, 0, 1, 1))
        self.assertEqual(px(out, 0, 0), (200, 200, 200, 255))
        self.assertEqual(px(out, 1, 0), (10, 10, 10, 255))
        self.assertEqual(px(out, 0, 1), (10, 10, 10, 255))
        self.assertEqual(px(out, 1, 1), (10, 10, 10, 255))


if __name__ == "__main__":
    unittest.main()
