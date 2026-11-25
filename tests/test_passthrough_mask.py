"""Mask edits on pass-through groups invalidate the enclosing isolated cache."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402


def solid(rgba):
    return Raster(4, 4, bytearray(rgba * 16))


class TestPassThroughMaskDependency(unittest.TestCase):
    def _project(self):
        mask_id = "m"
        project = Project(4, 4, Group("root", [
            Group("outer", [
                Group("pt1", [
                    Layer("a", solid([255, 0, 0, 255]))],
                    isolated=False, mask_id=mask_id),
                Group("pt2", [
                    Group("pt3", [
                        Layer("b", solid([0, 0, 255, 128]))],
                        isolated=False, mask_id=mask_id),
                ], isolated=True),
            ], isolated=True),
        ]))
        project.add_mask(mask_id, solid([255, 255, 255, 255]))
        return project, mask_id

    def test_passthrough_mask_dirties_isolated_ancestor_rect(self):
        project, mask_id = self._project()
        ev = Evaluator(project)
        ev.evaluate()  # warm caches
        rect = (1, 1, 3, 3)
        ev.touch_mask(mask_id, rect)
        ev.reevaluate_dirty_rect(rect)
        # The pass-through readers re-apply the mask exactly on the rect.
        mask_events = [r for r in ev.log if r[0] == "mask-apply"]
        self.assertTrue(mask_events)
        self.assertTrue(all(r[3] == rect for r in mask_events))
        names = {r[2] for r in mask_events}
        self.assertIn("pt1", names)
        self.assertIn("pt3", names)
        # The enclosing isolated group 'outer' must recompute on the rect
        # because the pass-through mask is baked into its local render.
        outer_events = [r for r in ev.log
                        if r[0] == "recompute-rect" and r[2] == "outer"
                        and r[3] == rect]
        self.assertTrue(outer_events)
        # No full-canvas rebuild for a rect edit.
        self.assertFalse(any(r[0] == "rebuild" for r in ev.log))

    def test_masked_passthrough_output_matches_full_evaluation(self):
        project, mask_id = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        # Repaint part of the mask to partial coverage.
        mask = project.masks[mask_id]
        mask.stroke([(1, 1), (3, 3)], (0, 0, 0, 120), 1.5, 0.5)
        ev.touch_mask(mask_id, (0, 0, 4, 4))
        partial, _ = ev.reevaluate_dirty_rect((0, 0, 4, 4))
        full, _ = Evaluator(project).evaluate()
        self.assertEqual(bytes(partial.data), bytes(full.data))

    def test_sub_rect_passthrough_mask_matches_full_evaluation(self):
        project, mask_id = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        project.masks[mask_id].stroke(
            [(2, 2)], (0, 0, 0, 100), 1.0, 0.5)
        rect = (1, 1, 3, 3)
        ev.touch_mask(mask_id, rect)
        partial, _ = ev.reevaluate_dirty_rect(rect)
        full, _ = Evaluator(project).evaluate()
        self.assertEqual(bytes(partial.data), bytes(full.data))


if __name__ == "__main__":
    unittest.main()
