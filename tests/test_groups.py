"""Group semantics: isolation, pass-through, nested and empty groups."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster, solid_raster  # noqa: E402


def layer(name, rgba, **kw):
    return Layer(name, solid_raster(1, 1, rgba), **kw)


def project(root):
    return Project(1, 1, root)


def final_pixel(evaluator):
    raster, _ = evaluator.evaluate()
    return raster.get_byte(0, 0)


class TestGroupSemantics(unittest.TestCase):
    def test_empty_group_changes_nothing(self):
        bottom = layer("bottom", [100, 100, 100, 255])
        empty = Group("empty", [])
        root = Group("root", [bottom, empty])
        ev = Evaluator(project(root))
        self.assertEqual(final_pixel(ev), (100, 100, 100, 255))

    def test_nested_empty_groups(self):
        root = Group("root", [
            layer("b", [40, 80, 120, 255]),
            Group("g1", [Group("g2", [Group("g3", [])])]),
        ])
        ev = Evaluator(project(root))
        self.assertEqual(final_pixel(ev), (40, 80, 120, 255))

    def test_isolated_group_composites_on_transparent_first(self):
        # Two 50% black layers inside an isolated group, over a white base.
        # Inside the group: 0.5 black over transparent then 0.5 black again.
        black = [0, 0, 0, 128]
        root = Group("root", [
            layer("white", [255, 255, 255, 255]),
            Group("iso", [layer("a", black), layer("b", black)], isolated=True),
        ])
        ev = Evaluator(project(root))
        r, g, b, a = final_pixel(ev)
        self.assertEqual(a, 255)
        # Group result alpha: 0.75 with premultiplied black; over white the
        # gray = 255 * (1 - 0.75) = 64 (quantization may land on 63/64).
        self.assertAlmostEqual(r, 64, delta=1)

    def test_group_opacity_not_equal_to_per_layer_opacity(self):
        black = [0, 0, 0, 255]
        # Group opacity 0.5 applied to the flattened group (W3C forces
        # isolation): the group's two opaque blacks flatten to opaque black,
        # then enter the white backdrop at 50% -> gray 128.
        grouped = Group("g", [layer("a", black), layer("b", black)],
                        isolated=True, opacity=0.5)
        root_grouped = Group("root", [layer("w", [255, 255, 255, 255]), grouped])
        ev1 = Evaluator(project(root_grouped))
        px_group = final_pixel(ev1)[:3]

        # Per-layer opacity 0.5 without a group: two 50% blacks over white.
        root_flat = Group("root", [
            layer("w", [255, 255, 255, 255]),
            layer("a", black, opacity=0.5),
            layer("b", black, opacity=0.5),
        ])
        ev2 = Evaluator(project(root_flat))
        px_flat = final_pixel(ev2)[:3]
        self.assertEqual(px_group, (128, 128, 128))
        # First 50% black: 128. Second 50% black over that: 64.
        self.assertEqual(px_flat, (64, 64, 64))
        self.assertNotEqual(px_group, px_flat)

    def test_passthrough_group_children_see_outer_backdrop(self):
        # Multiply inside a pass-through group must see the white base outside.
        root = Group("root", [
            layer("white", [255, 255, 255, 255]),
            Group("pt", [
                layer("m", [128, 128, 128, 255], blend_mode="multiply"),
            ], isolated=False),
        ])
        ev = Evaluator(project(root))
        # multiply(white, ~0.502) = ~0.502 -> 128
        r, g, b, a = final_pixel(ev)
        self.assertEqual(a, 255)
        self.assertAlmostEqual(r, 128, delta=1)

    def test_passthrough_vs_isolated_differ_with_blend(self):
        # Two 50%-alpha screen layers. In an isolated group the second screen
        # sees the group's own backdrop (black where nothing composited yet);
        # pass-through hands both screens the outer white backdrop directly.
        def build(isolated):
            return Group("root", [
                layer("white", [255, 255, 255, 255]),
                Group("g", [
                    layer("s1", [0, 0, 0, 128], blend_mode="screen"),
                    layer("s2", [0, 0, 0, 128], blend_mode="screen"),
                ], isolated=isolated),
            ])
        iso = final_pixel(Evaluator(project(build(True))))
        pt = final_pixel(Evaluator(project(build(False))))
        self.assertNotEqual(iso[:3], pt[:3])
        # Isolated: screens on black leave black; group alpha 0.75; over white
        # the result is white*(1-0.75) ~= 64.
        self.assertAlmostEqual(iso[0], 64, delta=1)
        # Pass-through: every screen sees white, so white stays white.
        self.assertEqual(pt[0], 255)

    def test_passthrough_group_rejects_opacity(self):
        root = Group("root", [
            Group("pt", [], isolated=False, opacity=0.5),
        ])
        ev = Evaluator(project(root))
        with self.assertRaises(Exception):
            ev.evaluate()

    def test_nested_groups_mixed_isolation(self):
        root = Group("root", [
            layer("w", [255, 255, 255, 255]),
            Group("outer", [
                Group("inner-pt", [
                    layer("m", [128, 128, 128, 255], blend_mode="multiply"),
                ], isolated=False),
            ], isolated=True),
        ])
        ev = Evaluator(project(root))
        r, _, _, a = final_pixel(ev)
        self.assertEqual(a, 255)
        self.assertAlmostEqual(r, 128, delta=2)


if __name__ == "__main__":
    unittest.main()
