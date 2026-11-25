"""Project save/load round-trip including nested groups and shared masks."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402
from layerpaint.serialization import load, save  # noqa: E402


def build_project():
    ink = Raster(8, 8)
    ink.stroke([(2, 2), (6, 6)], (10, 200, 60, 150), radius=2, hardness=0.5)
    mask = Raster(8, 8)
    for i in range(0, len(mask.data), 4):
        mask.data[i:i + 4] = bytes([180, 180, 180, 255])
    project = Project(8, 8, Group("root", [
        Layer("paper", Raster(8, 8, bytearray([255, 255, 255, 255] * 64))),
        Group("pass-group", [
            Layer("base", Raster(8, 8, bytearray([120, 120, 120, 200] * 64))),
            Layer("ink", ink, blend_mode="multiply", clipped=True),
        ], isolated=False, mask_id="m1"),
        Group("iso-group", [
            Layer("wash", Raster(8, 8, bytearray([0, 50, 200, 90] * 64)),
                  opacity=0.4),
        ], isolated=True, opacity=0.9, mask_id="m1"),
    ]))
    project.add_mask("m1", mask)
    return project


class TestSerialization(unittest.TestCase):
    def test_roundtrip_preserves_composite(self):
        project = build_project()
        before, _ = Evaluator(project).evaluate()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "project.lp.json")
            save(project, path)
            reloaded = load(path)
        after, _ = Evaluator(reloaded).evaluate()
        self.assertEqual(bytes(before.data), bytes(after.data))

    def test_roundtrip_preserves_tree_flags(self):
        project = build_project()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            save(project, path)
            reloaded = load(path)
        nodes = {n.name: n for n in reloaded.all_nodes()}
        self.assertFalse(nodes["pass-group"].isolated)
        self.assertTrue(nodes["iso-group"].isolated)
        self.assertEqual(nodes["ink"].blend_mode, "multiply")
        self.assertTrue(nodes["ink"].clipped)
        self.assertEqual(nodes["iso-group"].mask_id, "m1")
        self.assertIn("m1", reloaded.masks)


if __name__ == "__main__":
    unittest.main()
