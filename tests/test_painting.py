"""Real painting conditions: soft semi-transparent strokes and edge safety."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.backends.accelerated import AcceleratedBackend  # noqa: E402
from layerpaint.backends.reference import ReferenceBackend  # noqa: E402
from layerpaint.engine import Evaluator  # noqa: E402
from layerpaint.model import Group, Layer, Project  # noqa: E402
from layerpaint.raster import Raster, write_png, read_png  # noqa: E402
import tempfile  # noqa: E402


class TestSoftStrokes(unittest.TestCase):
    def test_soft_stroke_has_feathered_alpha_gradient(self):
        canvas = Raster(16, 16)
        canvas.stroke([(8, 8)], color=(255, 0, 0, 255), radius=5, hardness=0.4)
        center = canvas.get_byte(8, 8)
        mid = canvas.get_byte(8, 11)      # inside feather region
        edge = canvas.get_byte(8, 13)     # near brush boundary
        self.assertEqual(center[3], 255)
        self.assertGreater(mid[3], 0)
        self.assertLess(mid[3], 255)
        self.assertEqual(edge[3], 0)
        # Feather pixels that are transparent must not carry hidden red.
        self.assertEqual(edge[:3], (0, 0, 0))

    def test_low_opacity_stroke_accumulates_gradually(self):
        canvas = Raster(8, 8)
        canvas.stroke([(4, 4)], color=(0, 0, 0, 64), radius=2, hardness=1.0)
        first = canvas.get_byte(4, 4)[3]
        canvas.stroke([(4, 4)], color=(0, 0, 0, 64), radius=2, hardness=1.0)
        second = canvas.get_byte(4, 4)[3]
        self.assertGreater(second, first)
        self.assertLess(second, 255)

    def test_backend_parity_on_painted_stroke(self):
        painted = Raster(24, 24)
        painted.stroke([(6, 6), (18, 16)], color=(40, 90, 200, 110),
                       radius=4, hardness=0.3)
        backdrop = Raster(24, 24)
        backdrop.stroke([(12, 4), (10, 20)], color=(220, 200, 30, 160),
                        radius=5, hardness=0.6)
        ref = ReferenceBackend()
        acc = AcceleratedBackend()
        for mode in ("normal", "multiply", "screen", "overlay", "soft-light"):
            with self.subTest(mode=mode):
                r = ref.composite(backdrop, painted, mode, 0.7)
                a = acc.composite(backdrop, painted, mode, 0.7)
                self.assertLessEqual(
                    max(abs(x - y) for x, y in zip(r.data, a.data)), 1)

    def test_transparent_edge_color_cannot_pollute_after_compositing(self):
        # Poison a fully transparent pixel with hidden chroma, then force the
        # storage invariant; a multiply over white must remain pure white.
        poisoned = Raster(2, 1, bytearray([
            255, 0, 0, 0,    # alpha 0 but poisoned RGB
            128, 128, 128, 255,
        ]))
        poisoned.strip_hidden_color()
        self.assertEqual(poisoned.get_byte(0, 0), (0, 0, 0, 0))
        white = Raster(2, 1, bytearray([255, 255, 255, 255] * 2))
        out = ReferenceBackend().composite(white, poisoned, "multiply", 1.0)
        self.assertEqual(out.get_byte(0, 0), (255, 255, 255, 255))

    def test_stroke_inside_layer_tree_matches_direct_composite(self):
        painted = Raster(16, 16)
        painted.stroke([(4, 4), (12, 12)], color=(200, 30, 30, 140),
                       radius=3, hardness=0.5)
        white = Raster(16, 16, bytearray([255, 255, 255, 255] * 256))
        project = Project(16, 16, Group("root", [
            Layer("paper", white),
            Layer("ink", painted, blend_mode="multiply", opacity=0.8),
        ]))
        ev = Evaluator(project)
        tree_out, _ = ev.evaluate()
        direct = ReferenceBackend().composite(
            white, painted, "multiply", 0.8)
        self.assertEqual(bytes(tree_out.data), bytes(direct.data))

    def test_png_roundtrip_preserves_straight_rgba(self):
        painted = Raster(12, 9)
        painted.stroke([(3, 3), (9, 6)], color=(10, 200, 90, 170),
                       radius=3, hardness=0.4)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stroke.png")
            write_png(path, painted)
            loaded = read_png(path)
            self.assertEqual((loaded.width, loaded.height), (12, 9))
            self.assertEqual(bytes(loaded.data), bytes(painted.data))


if __name__ == "__main__":
    unittest.main()
