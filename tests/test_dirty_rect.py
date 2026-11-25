"""Dirty-rect recompute correctness and pass-through clipping."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402


class TestDirtyRect(unittest.TestCase):
    def test_node_dirty_rect_matches_full_recompute(self):
        size = 8
        bottom = Layer("bottom", Raster(size, size, bytearray(
            [200, 200, 210, 255] * size * size)))
        ink = Raster(size, size)
        ink.stroke([(1, 1), (6, 6)], (255, 0, 0, 160), 2, 0.4)
        top = Layer("ink", ink, blend_mode="multiply", opacity=0.8)
        project = Project(size, size, Group("root", [bottom, top]))
        ev = Evaluator(project)
        ev.evaluate()
        # Edit a stroke inside a sub-rectangle only.
        top.raster.stroke([(2, 2), (4, 4)], (0, 0, 255, 200), 1.5, 0.6)
        ev.touch_node(top.uid, rect=(1, 1, 6, 6))
        partial, _ = ev.reevaluate_dirty_rect((1, 1, 6, 6))
        full, _ = Evaluator(project).evaluate()
        self.assertEqual(bytes(partial.data), bytes(full.data))

    def test_dirty_rect_outside_edit_is_untouched(self):
        project = Project(4, 4, Group("root", [
            Layer("a", Raster(4, 4, bytearray([10, 20, 30, 255] * 16))),
        ]))
        ev = Evaluator(project)
        ev.evaluate()
        log_events = []
        ev.reevaluate_dirty_rect((0, 0, 2, 2))
        # All backend work must be confined to the 2x2 rect.
        for row in ev.log:
            if row[0] in ("place", "mask-apply"):
                self.assertEqual(row[3], (0, 0, 2, 2))

    def test_sub_rect_recompute_with_clipping_chain_matches_full(self):
        size = 8
        base = Raster(size, size)
        for i in range(size * size):
            x = i % size
            j = i * 4
            if x < 4:
                base.data[j:j + 4] = bytes([255, 255, 255, 255])
        ink = Raster(size, size)
        ink.stroke([(1, 1), (6, 6)], (255, 0, 0, 200), 2, 0.4)
        clip = Layer("clip", ink, clipped=True)
        project = Project(size, size, Group("root", [
            Group("iso", [Layer("base", base), clip], isolated=True),
        ]))
        ev = Evaluator(project)
        ev.evaluate()
        clip.raster.stroke([(5, 5), (7, 7)], (0, 0, 255, 220), 1.5, 0.6)
        rect = (3, 3, 8, 8)
        ev.touch_node(clip.uid, rect=rect)
        partial, _ = ev.reevaluate_dirty_rect(rect)
        full, _ = Evaluator(project).evaluate()
        self.assertEqual(bytes(partial.data), bytes(full.data))


class TestPassThroughClipping(unittest.TestCase):
    def test_clipped_passthrough_group_respects_base_alpha(self):
        size = 2
        # Base opaque only in column 0.
        base_raster = Raster(size, 1, bytearray([
            255, 255, 255, 255, 0, 0, 0, 0]))
        red = Raster(size, 1, bytearray([255, 0, 0, 255, 255, 0, 0, 255]))
        root = Group("root", [
            Layer("base", base_raster),
            Group("pt", [Layer("red", red)], isolated=False, clipped=True),
        ])
        ev = Evaluator(Project(size, 1, root))
        out, _ = ev.evaluate()
        self.assertEqual(out.get_byte(0, 0), (255, 0, 0, 255))
        # Column 1 has no base alpha: the pass-through group cannot paint.
        self.assertEqual(out.get_byte(1, 0), (0, 0, 0, 0))

    def test_nested_passthrough_inside_isolated_group(self):
        # A pass-through group nested in an isolated group must let its blend
        # layer see the isolated group's internal backdrop, not the outer one.
        size = 1
        root = Group("root", [
            Layer("outer-white", Raster(1, 1, bytearray([255, 255, 255, 255]))),
            Group("iso", [
                Layer("inner-gray",
                      Raster(1, 1, bytearray([128, 128, 128, 255]))),
                Group("pt", [
                    Layer("mul",
                          Raster(1, 1, bytearray([128, 128, 128, 255])),
                          blend_mode="multiply"),
                ], isolated=False),
            ], isolated=True),
        ])
        ev = Evaluator(Project(size, size, root))
        r, _, _, a = ev.evaluate()[0].get_byte(0, 0)
        # multiply(128,128) ~= 64 inside the isolated group.
        self.assertEqual(a, 255)
        self.assertAlmostEqual(r, 64, delta=1)


if __name__ == "__main__":
    unittest.main()
