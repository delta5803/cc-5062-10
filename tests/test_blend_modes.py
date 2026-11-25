"""The blend-mode set is the complete, finite W3C list."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from layerpaint.blend import (  # noqa: E402
    BLEND_MODES, NON_SEPARABLE, SEPARABLE, blend_pixel,
)

EXPECTED = {
    "normal",
    # separable
    "multiply", "screen", "overlay", "darken", "lighten",
    "color-dodge", "color-burn", "hard-light", "soft-light",
    "difference", "exclusion",
    # non-separable
    "hue", "saturation", "color", "luminosity",
}


class TestBlendCatalog(unittest.TestCase):
    def test_exact_sixteen_modes(self):
        self.assertEqual(BLEND_MODES, EXPECTED)
        self.assertEqual(len(BLEND_MODES), 16)

    def test_separable_and_non_separable_partition(self):
        self.assertEqual(len(SEPARABLE), 11)
        self.assertEqual(len(NON_SEPARABLE), 4)
        self.assertEqual(
            set(SEPARABLE) | set(NON_SEPARABLE) | {"normal"}, EXPECTED)

    def test_non_separable_hand_computed(self):
        # color mode keeps backdrop luminosity and adopts source hue/chroma.
        # A gray source has zero saturation, so the result must desaturate.
        backdrop = (0.8, 0.2, 0.1)
        source = (0.5, 0.5, 0.5)  # neutral gray
        out = blend_pixel("color", backdrop, source)
        sat_in = max(backdrop) - min(backdrop)
        sat_out = max(out) - min(out)
        self.assertLess(sat_out, sat_in)
        # Luminosity is preserved by definition.
        def lum(c):
            return 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]
        self.assertAlmostEqual(lum(out), lum(backdrop), places=6)

    def test_multiply_and_identity_values(self):
        self.assertEqual(blend_pixel("multiply", (1.0,), (0.5,)), (0.5,))
        self.assertEqual(blend_pixel("screen", (0.0,), (0.7,)), (0.7,))
        self.assertEqual(blend_pixel("normal", (0.1, 0.2, 0.3),
                                     (0.9, 0.8, 0.7)), (0.9, 0.8, 0.7))


if __name__ == "__main__":
    unittest.main()
