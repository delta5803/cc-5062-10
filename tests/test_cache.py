"""Shared masks, cache boundaries and dependency-driven recompute ranges."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402


def white_layer(name, masked=False, mask_id=None, **kw):
    return Layer(name, Raster(2, 2, bytearray([255, 255, 255, 255] * 4)),
                 mask_id=mask_id, **kw)


def mask_raster(value=255):
    return Raster(2, 2, bytearray([value, value, value, 255] * 4))


class TestMasksAndCaching(unittest.TestCase):
    def _build(self):
        # One shared mask consumed by an isolated and a pass-through group.
        mask_id = "shared"
        project = Project(2, 2, Group("root", [
            Group("iso", [white_layer("iso-child")], isolated=True,
                  mask_id=mask_id),
            Group("pass", [white_layer("pass-child")], isolated=False,
                  mask_id=mask_id),
        ]))
        project.add_mask(mask_id, mask_raster(255))
        return project, mask_id

    def test_mask_limits_alpha(self):
        project, mask_id = self._build()
        project.masks[mask_id] = mask_raster(128)
        ev = Evaluator(project)
        out, _ = ev.evaluate()
        # Mask at 128 scales the rendered alpha.
        self.assertEqual(out.get_byte(0, 0)[3], 128)

    def test_second_evaluation_uses_caches(self):
        project, _ = self._build()
        ev = Evaluator(project)
        ev.evaluate()
        ev.evaluate()
        rebuilds = [row for row in ev.log if row[0] == "rebuild"]
        self.assertEqual(rebuilds, [])
        self.assertTrue(any(row[0] == "cache-hit" for row in ev.log))

    def test_node_edit_invalidates_ancestor_cache_only(self):
        project, _ = self._build()
        ev = Evaluator(project)
        ev.evaluate()
        child = next(n for n in project.all_nodes() if n.name == "iso-child")
        ev.touch_node(child.uid)
        ev.evaluate()
        names_rebuilt = [row[2] for row in ev.log if row[0] == "rebuild"]
        self.assertIn("iso-child", names_rebuilt)
        self.assertIn("iso", names_rebuilt)
        # The pass-through branch never read that child, so it does not rebuild.
        self.assertNotIn("pass-child", names_rebuilt)

    def test_shared_mask_rect_distinguishes_isolated_and_passthrough(self):
        project, mask_id = self._build()
        ev = Evaluator(project)
        ev.evaluate()
        rect = (0, 0, 1, 1)
        ev.touch_mask(mask_id, rect)
        ev.reevaluate_dirty_rect(rect)
        # The isolated group owns a self-contained content cache that stays
        # valid; its mask is re-applied over exactly the edited rect.
        iso_mask_events = [
            row for row in ev.log
            if row[0] == "mask-apply" and row[2] == "iso"
            and row[3] == rect and row[4] == mask_id
        ]
        self.assertTrue(iso_mask_events,
                        "isolated group should re-apply the mask on the rect")
        iso_rebuilds = [row for row in ev.log
                        if row[0] in ("rebuild", "recompute-rect")
                        and row[2] == "iso"]
        self.assertEqual(iso_rebuilds, [],
                         "isolated content cache must survive a mask-only edit")
        # The pass-through group holds no own backdrop: the rect is carried
        # through the passthrough boundary into the parent-owned backdrop.
        pt_events = [row for row in ev.log
                     if row[0] == "passthrough" and row[2] == "pass"
                     and row[3] == rect]
        self.assertTrue(pt_events)
        # And no full-canvas content rebuild was triggered by a rect edit.
        full_rebuilds = [row for row in ev.log
                         if row[0] == "rebuild" and row[3] is None]
        self.assertEqual(full_rebuilds, [])

    def test_full_mask_edit_keeps_isolated_cache_semantics(self):
        project, mask_id = self._build()
        ev = Evaluator(project)
        ev.evaluate()
        ev.touch_mask(mask_id)  # FULL
        ev.evaluate()
        # Content caches survive a mask-only edit: no content rebuild.
        rebuilds = [row for row in ev.log if row[0] == "rebuild"]
        self.assertEqual(rebuilds, [])

    def test_rect_recompute_matches_full_recompute(self):
        project, mask_id = self._build()
        ev = Evaluator(project)
        ev.evaluate()
        ev.touch_mask(mask_id, (0, 0, 1, 1))
        partial, _ = ev.reevaluate_dirty_rect((0, 0, 1, 1))
        full, _ = Evaluator(project).evaluate()
        self.assertEqual(bytes(partial.data), bytes(full.data))


if __name__ == "__main__":
    unittest.main()
