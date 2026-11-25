"""Parity between the CPU reference and LUT-accelerated backends."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.backends.accelerated import AcceleratedBackend  # noqa: E402
from layerpaint.backends.reference import ReferenceBackend  # noqa: E402
from layerpaint.blend import BLEND_MODES  # noqa: E402
from layerpaint.raster import Raster  # noqa: E402

TOLERANCE = 1  # byte-level quantization difference allowed


def max_diff(a, b):
    return max(abs(x - y) for x, y in zip(a.data, b.data))


class TestBackendParity(unittest.TestCase):
    def setUp(self):
        self.ref = ReferenceBackend()
        self.acc = AcceleratedBackend()
        rng = random.Random(42)
        self.backdrop = Raster(8, 8)
        self.source = Raster(8, 8)
        for i in range(0, len(self.backdrop.data), 4):
            self.backdrop.data[i:i + 4] = bytes(rng.randrange(256) for _ in range(4))
            self.source.data[i:i + 4] = bytes(rng.randrange(256) for _ in range(4))
        # Guarantee some zero-alpha pixels on both sides.
        for i in range(0, len(self.backdrop.data), 16):
            self.backdrop.data[i + 3] = 0
            self.source.data[i + 3] = 0

    def test_all_blend_modes_full_opacity(self):
        for mode in sorted(BLEND_MODES):
            with self.subTest(mode=mode):
                r = self.ref.composite(self.backdrop, self.source, mode, 1.0)
                a = self.acc.composite(self.backdrop, self.source, mode, 1.0)
                self.assertLessEqual(max_diff(r, a), TOLERANCE)

    def test_all_blend_modes_half_opacity(self):
        for mode in sorted(BLEND_MODES):
            with self.subTest(mode=mode):
                r = self.ref.composite(self.backdrop, self.source, mode, 0.37)
                a = self.acc.composite(self.backdrop, self.source, mode, 0.37)
                self.assertLessEqual(max_diff(r, a), TOLERANCE)

    def test_zero_opacity_is_noop(self):
        for backend in (self.ref, self.acc):
            out = backend.composite(self.backdrop, self.source, "multiply", 0.0)
            self.assertEqual(bytes(out.data), bytes(self.backdrop.data))

    def test_mask_parity(self):
        mask = Raster(8, 8)
        rng = random.Random(7)
        for i in range(0, len(mask.data), 4):
            v = rng.randrange(256)
            mask.data[i:i + 4] = bytes([v, v, v, 255])
        r = self.ref.apply_mask(self.source, mask)
        a = self.acc.apply_mask(self.source, mask)
        # Integer vs float rounding of alpha*mask can differ by one byte.
        self.assertLessEqual(max_diff(r, a), TOLERANCE)

    def test_clipping_parity(self):
        base_alpha = Raster(8, 8)
        rng = random.Random(99)
        for i in range(0, len(base_alpha.data), 4):
            v = rng.choice([0, 64, 128, 200, 255])
            base_alpha.data[i:i + 4] = bytes([0, 0, 0, v])
        r = self.ref.clip_lerp(self.backdrop, self.source, base_alpha)
        a = self.acc.clip_lerp(self.backdrop, self.source, base_alpha)
        self.assertLessEqual(max_diff(r, a), TOLERANCE)

    def test_rect_ops_match_full_ops_inside_rect(self):
        rect = (2, 2, 6, 6)
        for mode in ("normal", "multiply", "screen", "soft-light"):
            with self.subTest(mode=mode):
                full = self.acc.composite(self.backdrop, self.source, mode, 0.6)
                partial = self.acc.composite(
                    self.backdrop, self.source, mode, 0.6, rect=rect)
                x0, y0, x1, y1 = rect
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        i = (y * 8 + x) * 4
                        self.assertEqual(
                            tuple(full.data[i:i + 4]),
                            tuple(partial.data[i:i + 4]))


if __name__ == "__main__":
    unittest.main()
