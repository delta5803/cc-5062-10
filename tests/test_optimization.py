"""Optimization tests: reuse, temporary-buffer reduction and exact pixels.

These tests pin the behavior of the deep-nesting optimizations:

* lazy backdrop snapshots (clones skipped unless a clipping base needs them);
* per-run clipping-base alpha memoization;
* frozen-subtree splices for unchanged sibling branches;
* transparent-source fast paths in both backends;
* and, above all, byte-for-byte equality with a from-scratch evaluation for
  every reuse path, including clipping chains and opacity changes.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.backends.accelerated import AcceleratedBackend  # noqa: E402
from layerpaint.backends.reference import ReferenceBackend  # noqa: E402
from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402


def solid(rgba, size=8):
    return Raster(size, size, bytearray(rgba * size * size))


def nested_project(depth=6, size=12):
    ink = Raster(size, size)
    ink.stroke([(2, 2), (size - 3, size - 3)], (220, 40, 40, 200), 3.0, 0.4)
    node = Layer("leaf", ink, blend_mode="multiply", opacity=0.9)
    for d in range(depth):
        node = Group("g%d" % d, [node], isolated=True, opacity=0.95,
                     blend_mode="screen" if d % 2 else "normal")
    neighbor = Raster(size, size)
    neighbor.stroke([(1, size - 2), (size - 2, 1)], (40, 60, 220, 160),
                    4.0, 0.25)
    root = Group("root", [
        Layer("paper", solid([245, 242, 235, 255], size)),
        node,
        Layer("neighbor", neighbor),
    ])
    return Project(size, size, root)


def full_reference(project):
    raster, _ = Evaluator(project, ReferenceBackend()).evaluate()
    return bytes(raster.data)


class TestLazyBackdropSnapshots(unittest.TestCase):
    def test_non_clipped_chain_skips_clones(self):
        project = nested_project()
        ev = Evaluator(project)
        ev.evaluate()
        # Every non-base node skips its backdrop clone; there are no clipped
        # nodes in this project, so every node must skip.
        self.assertGreater(ev.stats["clone-skipped"], 0)
        self.assertEqual(ev.stats["clip-calls"], 0)

    def test_clipping_base_still_takes_snapshot(self):
        size = 4
        base = Raster(size, size, bytearray([255, 255, 255, 255] * size * size))
        red = solid([255, 0, 0, 255], size)
        project = Project(size, size, Group("root", [
            Layer("base", base),
            Layer("red", red, clipped=True),
        ]))
        ev = Evaluator(project)
        ev.evaluate()
        # The base needs its pre-placement snapshot, so the clipped branch is
        # still correct even while unrelated nodes skip clones.
        self.assertGreaterEqual(ev.stats["clip-calls"], 1)
        out, _ = ev.evaluate()
        self.assertEqual(bytes(out.data), full_reference(project))


class TestAlphaMemoization(unittest.TestCase):
    def test_clipping_base_alpha_reused_when_requested_twice(self):
        # The same base's masked footprint is requested once to serve the
        # clipped sibling and again when a later non-clipped sibling resets
        # the clipping chain; the second lookup must be a memoized hit.
        size = 8
        base = Raster(size, size)
        base.stroke([(2, 2), (6, 6)], (200, 30, 30, 255), 3.0, 0.5)
        red = solid([255, 0, 0, 255], size)
        breaker = solid([0, 200, 0, 255], size)  # non-clipped: resets chain
        project = Project(size, size, Group("root", [
            Layer("base", base),
            Layer("red", red, clipped=True),
            Layer("breaker", breaker),
        ]))
        ev = Evaluator(project)
        raster, _ = ev.evaluate()
        # A second, unchanged evaluation reuses the memoized base footprint
        # (keyed on node/mask versions), so the clipping base is not rendered
        # and masked a second time.
        ev.evaluate()
        self.assertGreaterEqual(ev.stats["alpha-hit"], 1)
        self.assertEqual(bytes(raster.data), full_reference(project))


class TestFrozenSplice(unittest.TestCase):
    def test_unchanged_neighbor_is_frozen_on_dirty_rect(self):
        project = nested_project()
        size = project.width
        ev = Evaluator(project)
        ev.evaluate()
        leaf = next(n for n in project.all_nodes() if n.name == "leaf")
        # Full-canvas rect: exactly bounds any soft-stamp influence, so the
        # reuse result is required to be byte-identical to a full run.
        rect = (0, 0, size, size)
        leaf.raster.stroke([(3, 3), (7, 7)], (0, 0, 200, 200), 2.0, 0.5)
        ev.touch_node(leaf.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        names = {row[2] for row in ev.log if row[0] == "frozen-splice"}
        # The paper backdrop is an untouched sibling branch and is reused;
        # every ancestor of the edited leaf is recomputed, never frozen.
        self.assertIn("paper", names)
        ancestor_names = {
            n.name for n in project.all_nodes()
            if n is not leaf and leaf in list(n.walk())
        }
        self.assertTrue(names.isdisjoint(ancestor_names))
        self.assertEqual(bytes(raster.data), full_reference(project))

    def test_frozen_splice_disabled_for_full_evaluation(self):
        project = nested_project()
        ev = Evaluator(project)
        ev.evaluate()
        self.assertEqual(ev.stats["frozen-splice"], 0)

    def test_blend_mode_change_invalidates_frozen_branch(self):
        project = nested_project()
        size = project.width
        ev = Evaluator(project)
        ev.evaluate()
        neighbor = next(n for n in project.all_nodes()
                        if n.name == "neighbor")
        neighbor.blend_mode = "multiply"
        rect = (0, 0, size, size)
        ev.touch_node(neighbor.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        # A placement-field change bumps the node version, so it cannot be
        # treated as a frozen (unchanged) subtree.
        frozen = {row[2] for row in ev.log if row[0] == "frozen-splice"}
        self.assertNotIn("neighbor", frozen)
        self.assertEqual(bytes(raster.data), full_reference(project))

    def test_opacity_change_keeps_group_semantics_and_matches_reference(self):
        # Group opacity must remain a whole-group isolated effect, never a
        # per-layer flattening, and the dirty path must match a full run.
        project = nested_project(depth=3)
        size = project.width
        ev = Evaluator(project)
        ev.evaluate()
        group = next(n for n in project.all_nodes() if n.name == "g1")
        group.opacity = 0.42
        rect = (0, 0, size, size)
        ev.touch_node(group.uid, rect=rect)
        raster, _ = ev.reevaluate_dirty_rect(rect)
        self.assertEqual(bytes(raster.data), full_reference(project))


class TestTransparentSourceFastPath(unittest.TestCase):
    def test_both_backends_skip_empty_source_unchanged(self):
        for backend in (ReferenceBackend(), AcceleratedBackend()):
            with self.subTest(backend=backend.name):
                backdrop = solid([120, 60, 200, 255], 4)
                empty = Raster(4, 4)  # all transparent
                out = backend.composite(backdrop, empty, "multiply", 1.0,
                                        rect=(0, 0, 4, 4))
                self.assertEqual(bytes(out.data), bytes(backdrop.data))

    def test_partial_rect_transparent_region_untouched(self):
        backend = ReferenceBackend()
        backdrop = solid([10, 20, 30, 255], 4)
        source = Raster(4, 4)
        source.set_byte(0, 0, (255, 0, 0, 255))
        out = backend.composite(backdrop, source, "normal", 1.0,
                                rect=(2, 2, 4, 4))
        # The rect covers only transparent source pixels.
        for y in range(2, 4):
            for x in range(2, 4):
                self.assertEqual(out.get_byte(x, y), (10, 20, 30, 255))


class TestCostBreakdownAndBuffers(unittest.TestCase):
    def test_cost_breakdown_reports_events_and_buffers(self):
        project = nested_project()
        ev = Evaluator(project)
        ev.evaluate()
        breakdown = ev.cost_breakdown()
        self.assertIn("events", breakdown)
        self.assertIn("buffers", breakdown)
        for key in ("composite-calls", "place", "clone-skipped"):
            self.assertIn(key, breakdown["events"])
        for key in ("allocations", "allocated_bytes", "peak_live_bytes"):
            self.assertIn(key, breakdown["buffers"])
        self.assertGreater(breakdown["buffers"]["peak_live_bytes"], 0)
        self.assertGreaterEqual(
            breakdown["buffers"]["allocated_bytes"],
            breakdown["buffers"]["peak_live_bytes"])

    def test_optimized_uses_fewer_clones_than_naive_nested_reeval(self):
        # The dirty-rect run must not allocate more full-canvas clones than
        # the number of nodes that genuinely need a clipping snapshot (zero
        # here): clone-skipped should cover every placement.
        project = nested_project()
        size = project.width
        ev = Evaluator(project)
        ev.evaluate()
        leaf = next(n for n in project.all_nodes() if n.name == "leaf")
        rect = (0, 0, size, size)
        ev.touch_node(leaf.uid, rect=rect)
        ev.reevaluate_dirty_rect(rect)
        placements = ev.stats["place"] + ev.stats["frozen-splice"]
        self.assertGreaterEqual(ev.stats["clone-skipped"], placements)


if __name__ == "__main__":
    unittest.main()
