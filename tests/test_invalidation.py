"""Invalidation correctness for clipping chains and group opacity changes.

The reuse machinery must never keep a stale boundary when clipping or
opacity changes.  Every case here compares the optimized dirty-rect path
against an independent from-scratch reference, byte for byte.  A separate
test re-locks the rule that group opacity is an isolated whole-group effect,
not a per-layer flattening.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster, solid_raster  # noqa: E402


def reference_bytes(project):
    raster, _ = Evaluator(project).evaluate()
    return bytes(raster.data)


class TestClippingInvalidation(unittest.TestCase):
    def _project(self, size=8):
        base = Raster(size, size)
        for i in range(size * size):
            if i % size < 4:
                base.data[i * 4:i * 4 + 4] = bytes([255, 255, 255, 255])
        ink = Raster(size, size)
        ink.stroke([(1, 1), (6, 6)], (255, 0, 0, 200), 2.0, 0.4)
        clip = Layer("clip", ink, clipped=True)
        return Project(size, size, Group("root", [
            Group("iso", [Layer("base", base), clip], isolated=True),
        ])), clip

    def test_clip_edit_dirty_rect_matches_full(self):
        project, clip = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        clip.raster.stroke([(5, 5), (7, 7)], (0, 0, 255, 220), 1.5, 0.6)
        rect = (3, 3, 8, 8)
        ev.touch_node(clip.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        self.assertEqual(bytes(raster.data), reference_bytes(project))

    def test_toggling_clipping_flag_matches_full(self):
        project, clip = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        clip.clipped = False  # break the clipping relationship
        rect = (0, 0, project.width, project.height)
        ev.touch_node(clip.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        self.assertEqual(bytes(raster.data), reference_bytes(project))
        # And re-enabling it must restore the clipped answer.
        clip.clipped = True
        ev.touch_node(clip.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        self.assertEqual(bytes(raster.data), reference_bytes(project))


class TestOpacityInvalidation(unittest.TestCase):
    def test_group_opacity_change_dirty_rect_matches_full(self):
        size = 8
        black = solid_raster(size, size, (0, 0, 0, 255))
        project = Project(size, size, Group("root", [
            Layer("white", solid_raster(size, size, (255, 255, 255, 255))),
            Group("g", [
                Layer("a", black.clone()),
                Layer("b", black.clone()),
            ], isolated=True),
        ]))
        ev = Evaluator(project)
        ev.evaluate()
        group = next(n for n in project.all_nodes() if n.name == "g")
        group.opacity = 0.5
        rect = (0, 0, size, size)
        ev.touch_node(group.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        self.assertEqual(bytes(raster.data), reference_bytes(project))

    def test_group_opacity_not_flattened_to_single_layer(self):
        # Whole-group 0.5 opacity over white yields gray 128; a per-layer 0.5
        # flattening yields 64.  The optimized path must keep the group
        # semantics exactly (this is the same invariant as test_groups, run
        # through a dirty-rect cycle to prove caching does not change it).
        size = 4
        white = solid_raster(size, size, (255, 255, 255, 255))
        black = solid_raster(size, size, (0, 0, 0, 255))
        grouped = Project(size, size, Group("root", [
            Layer("w", white),
            Group("g", [Layer("a", black.clone()), Layer("b", black.clone())],
                  isolated=True, opacity=0.5),
        ]))
        ev = Evaluator(grouped)
        ev.evaluate()
        group = next(n for n in grouped.all_nodes() if n.name == "g")
        ev.touch_node(group.uid, rect=(0, 0, size, size))
        raster, _ = ev.reevaluate_dirty_rect((0, 0, size, size))
        self.assertEqual(raster.get_byte(0, 0)[:3], (128, 128, 128))

    def test_layer_opacity_change_matches_full(self):
        size = 6
        project = Project(size, size, Group("root", [
            Layer("bg", solid_raster(size, size, (255, 255, 255, 255))),
            Layer("ink", solid_raster(size, size, (0, 0, 0, 255)),
                  opacity=1.0),
        ]))
        ev = Evaluator(project)
        ev.evaluate()
        ink = next(n for n in project.all_nodes() if n.name == "ink")
        ink.opacity = 0.25
        rect = (0, 0, size, size)
        ev.touch_node(ink.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        self.assertEqual(bytes(raster.data), reference_bytes(project))


if __name__ == "__main__":
    unittest.main()
