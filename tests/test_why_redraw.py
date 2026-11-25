"""Tests for the "why redraw" reverse lookup and mask propagation.

The layer inspector can click a cached region and ask which tracked edits
propagate to it.  These tests pin the dependency vocabulary:

* direct content edits and ancestor propagation for node edits;
* mask placement invalidation for isolated readers;
* pass-through masks baking into the nearest enclosing isolated ancestor;
* and an explicit propagation map per reader cache boundary.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402


def white(name, mask_id=None, size=2, **kw):
    return Layer(name, Raster(size, size,
                 bytearray([255, 255, 255, 255] * size * size)),
                 mask_id=mask_id, **kw)


def mask(value=255, size=2):
    return Raster(size, size,
                  bytearray([value, value, value, 255] * size * size))


class TestWhyRedraw(unittest.TestCase):
    def _project(self):
        mid = "shared"
        project = Project(2, 2, Group("root", [
            Group("iso", [white("iso-child")], isolated=True, mask_id=mid),
            Group("pass", [white("pass-child")], isolated=False, mask_id=mid),
        ]))
        project.add_mask(mid, mask(255))
        return project, mid

    def test_direct_node_edit_is_reported(self):
        project, _ = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        child = next(n for n in project.all_nodes() if n.name == "iso-child")
        ev.touch_node(child.uid, rect=(0, 0, 1, 1))
        why = ev.why_redraw(child.uid)
        causes = {r["cause"] for r in why["reasons"]}
        self.assertIn("direct-content-edit", causes)

    def test_ancestor_edit_propagates_to_descendant(self):
        project, _ = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        iso = next(n for n in project.all_nodes() if n.name == "iso")
        child = next(n for n in project.all_nodes() if n.name == "iso-child")
        ev.touch_node(iso.uid)
        why = ev.why_redraw(child.uid)
        causes = {r["cause"] for r in why["reasons"]}
        self.assertIn("ancestor-content-edit", causes)
        path = next(r["path"] for r in why["reasons"]
                    if r["cause"] == "ancestor-content-edit")
        self.assertEqual(path[0], "iso")
        self.assertEqual(path[-1], "iso-child")

    def test_isolated_mask_edit_reports_placement_invalidation(self):
        project, mid = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        iso = next(n for n in project.all_nodes() if n.name == "iso")
        rect = (0, 0, 1, 1)
        ev.touch_mask(mid, rect)
        why = ev.why_redraw(iso.uid, rect=rect)
        causes = {r["cause"] for r in why["reasons"]}
        self.assertIn("mask-placement-edit", causes)
        hit = next(r for r in why["reasons"]
                   if r["cause"] == "mask-placement-edit")
        self.assertEqual(hit["boundary"], "isolated-placement")

    def test_passthrough_mask_bakes_into_isolated_ancestor(self):
        mid = "m"
        project = Project(4, 4, Group("root", [
            Group("outer", [
                Group("pt1", [white("a", size=4)], isolated=False, mask_id=mid),
            ], isolated=True),
        ]))
        project.add_mask(mid, mask(255, size=4))
        ev = Evaluator(project)
        ev.evaluate()
        outer = next(n for n in project.all_nodes() if n.name == "outer")
        ev.touch_mask(mid, (1, 1, 3, 3))
        why = ev.why_redraw(outer.uid)
        causes = {r["cause"] for r in why["reasons"]}
        self.assertIn("mask-baked-into-isolated-ancestor", causes)

    def test_unrelated_node_has_no_reasons_after_other_edit(self):
        project, _ = self._project()
        ev = Evaluator(project)
        ev.evaluate()
        child = next(n for n in project.all_nodes() if n.name == "iso-child")
        other = next(n for n in project.all_nodes() if n.name == "pass-child")
        ev.touch_node(child.uid)
        why = ev.why_redraw(other.uid)
        self.assertEqual(why["reasons"], [])


class TestMaskPropagation(unittest.TestCase):
    def test_propagation_distinguishes_reader_boundaries(self):
        mid = "shared"
        project = Project(2, 2, Group("root", [
            Group("iso", [white("ic")], isolated=True, mask_id=mid),
            Group("pass", [white("pc")], isolated=False, mask_id=mid),
        ]))
        project.add_mask(mid, mask(255))
        ev = Evaluator(project)
        ev.evaluate()
        rect = (0, 0, 1, 1)
        result = ev.mask_propagation(mid, rect=rect)
        boundaries = {r["name"]: r["boundary"] for r in result["readers"]}
        self.assertEqual(boundaries["iso"], "isolated-placement")
        self.assertEqual(boundaries["pass"], "passthrough-baked")
        iso_reader = next(r for r in result["readers"] if r["name"] == "iso")
        self.assertEqual(iso_reader["content_cache"], "reused")

    def test_unknown_mask_raises(self):
        project, _ = (lambda: (
            Project(2, 2, Group("root", [white("x")])), None))()
        ev = Evaluator(project)
        with self.assertRaises(KeyError):
            ev.mask_propagation("nope")


if __name__ == "__main__":
    unittest.main()
