"""Clipping chains and clipping-chain interruption."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402


def raster_from_rows(rows):
    """rows: list of RGBA byte tuples, row-major, square width inferred."""
    h = len(rows)
    w = len(rows[0])
    data = bytearray()
    for row in rows:
        for px in row:
            data.extend(px)
    return Raster(w, h, data)


def transparent():
    return (0, 0, 0, 0)


class TestClipping(unittest.TestCase):
    def setUp(self):
        # 2x1 canvas. Base layer opaque only in column 0.
        base = raster_from_rows([[
            (255, 255, 255, 255),
            transparent(),
        ]])
        overlay = raster_from_rows([[
            (255, 0, 0, 255),
            (255, 0, 0, 255),
        ]])
        self.base = Layer("base", base)
        self.overlay = Layer("red", overlay, clipped=True)

    def test_clipped_layer_confined_to_base_alpha(self):
        root = Group("root", [self.base, self.overlay])
        ev = Evaluator(Project(2, 1, root))
        out, _ = ev.evaluate()
        self.assertEqual(out.get_byte(0, 0), (255, 0, 0, 255))
        # Column 1 has no base alpha: clipped red cannot appear.
        self.assertEqual(out.get_byte(1, 0), (0, 0, 0, 0))

    def test_clipping_chain_multiple_clipped_siblings(self):
        green = Raster(2, 1, bytearray([0, 255, 0, 255] * 2))
        green_layer = Layer("green", green, clipped=True)
        root = Group("root", [self.base, self.overlay, green_layer])
        ev = Evaluator(Project(2, 1, root))
        out, _ = ev.evaluate()
        self.assertEqual(out.get_byte(0, 0), (0, 255, 0, 255))
        self.assertEqual(out.get_byte(1, 0), (0, 0, 0, 0))

    def test_chain_breaks_at_non_clipped_sibling(self):
        # A full-canvas non-clipped green layer becomes the new base; a later
        # clipped blue layer may then appear in column 1 as well.
        green = Raster(2, 1, bytearray([0, 255, 0, 255] * 2))
        green_layer = Layer("green", green)  # not clipped -> chain break
        blue = Raster(2, 1, bytearray([0, 0, 255, 255] * 2))
        blue_layer = Layer("blue", blue, clipped=True)
        root = Group("root", [self.base, self.overlay, green_layer, blue_layer])
        ev = Evaluator(Project(2, 1, root))
        out, _ = ev.evaluate()
        self.assertEqual(out.get_byte(0, 0), (0, 0, 255, 255))
        # New base is fully opaque, so blue shows in column 1 too.
        self.assertEqual(out.get_byte(1, 0), (0, 0, 255, 255))

    def test_clipped_group_uses_base_alpha(self):
        # The group itself is the clipped node; its child paints normally
        # inside the group, and the group is confined to the base alpha.
        inner_red = Raster(2, 1, bytearray([255, 0, 0, 255] * 2))
        inner = Group("inner", [Layer("red", inner_red)],
                      isolated=True, clipped=True)
        root = Group("root", [self.base, inner])
        ev = Evaluator(Project(2, 1, root))
        out, _ = ev.evaluate()
        self.assertEqual(out.get_byte(0, 0), (255, 0, 0, 255))
        self.assertEqual(out.get_byte(1, 0), (0, 0, 0, 0))

    def test_clipped_first_child_is_rejected(self):
        root = Group("root", [Layer("lone", Raster(1, 1), clipped=True)])
        ev = Evaluator(Project(1, 1, root))
        with self.assertRaises(Exception):
            ev.evaluate()

    def test_semi_transparent_base_partial_clip(self):
        # Base at 50% alpha: clipping lerps the red node render against the
        # saved (empty) backdrop using the base's own 50% alpha.
        #   out_alpha = 0.5*255 + 0.5*0 = 128 straight, but the backdrop below
        # is empty here, so result is red at exactly the base alpha 128.
        base = Raster(1, 1, bytearray([255, 255, 255, 128]))
        red = Raster(1, 1, bytearray([255, 0, 0, 255]))
        root = Group("root", [
            Layer("base", base),
            Layer("red", red, clipped=True),
        ])
        ev = Evaluator(Project(1, 1, root))
        out, _ = ev.evaluate()
        r, g, b, a = out.get_byte(0, 0)
        self.assertEqual(a, 128)
        self.assertEqual((r, g, b), (255, 0, 0))

    def test_semi_transparent_base_over_painted_backdrop(self):
        # With an opaque backdrop below the 50% base, clipping merges backdrop
        # and clipped color: alpha stays fully covered, color is half red.
        bottom = Raster(1, 1, bytearray([255, 255, 255, 255]))
        base = Raster(1, 1, bytearray([0, 0, 0, 128]))
        red = Raster(1, 1, bytearray([255, 0, 0, 255]))
        root = Group("root", [
            Layer("bottom", bottom),
            Layer("base", base),
            Layer("red", red, clipped=True),
        ])
        ev = Evaluator(Project(1, 1, root))
        out, _ = ev.evaluate()
        r, g, b, a = out.get_byte(0, 0)
        self.assertEqual(a, 255)
        self.assertAlmostEqual(r, 255, delta=1)
        self.assertAlmostEqual(g, 128, delta=1)
        self.assertAlmostEqual(b, 128, delta=1)


if __name__ == "__main__":
    unittest.main()
