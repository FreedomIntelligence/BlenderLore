import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from camera_framing import orthographic_scale_factor


class CameraFramingTests(unittest.TestCase):
    def test_complete_camera_does_not_zoom_in(self):
        self.assertEqual(orthographic_scale_factor((0.1, 0.8, 0.2, 0.9)), 1.0)

    def test_clipped_height_requires_more_than_one(self):
        self.assertGreater(orthographic_scale_factor((0.04, 0.96, -0.014, 1.014)), 1.19)

    def test_narrow_output_uses_horizontal_projection(self):
        self.assertAlmostEqual(
            orthographic_scale_factor((-0.4, 1.4, 0.2, 0.8)), 0.9 / 0.43
        )

    def test_off_center_extreme_is_not_hidden_by_bbox_span(self):
        self.assertAlmostEqual(
            orthographic_scale_factor((0.6, 1.1, 0.4, 0.6)), 0.6 / 0.43
        )

    def test_invalid_inputs_are_not_accepted(self):
        for bounds in [(float("nan"), 1, 0, 1), (1, 0, 0, 1), (0, 1, 0)]:
            with self.assertRaises(ValueError):
                orthographic_scale_factor(bounds)
        for margin in [-0.1, 0.5, 1]:
            with self.assertRaises(ValueError):
                orthographic_scale_factor((0, 1, 0, 1), margin=margin)


if __name__ == "__main__":
    unittest.main()
