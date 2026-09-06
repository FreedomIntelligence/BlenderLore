from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build_pipeline_specs as specs
import run_video_strict_replay as strict_replay


class PipelineSpecSignalTests(unittest.TestCase):
    def motion(self, tutorial: str, steps=None, title="Blue cube"):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(
                specs, "scan_full_transcript_motion", return_value={"dynamic": False}
            ),
        ):
            return specs.build_motion_plan(Path(root), title, tutorial, steps or {})

    def material(self, tutorial: str, title="Blue cube"):
        with tempfile.TemporaryDirectory() as root:
            return specs.build_material_spec(Path(root), title, tutorial, {})

    def test_static_cube_negations_and_origin_do_not_request_motion(self):
        text = "Create a cube at the origin. No animation. No keyframe or simulated motion. Set uniform Roughness to 0.35."
        result = self.motion(text, {"steps": [{"instruction": text}]})
        self.assertEqual(result["motion_type"], "static")
        self.assertEqual(result["evidence_terms"], [])
        self.assertEqual(result["presentation_profile"], "studio_turntable")
        self.assertFalse(self.material(text)["texture_detail_required"])

    def test_chinese_negations_do_not_request_motion(self):
        text = "在原点添加立方体。没有动画。无需关键帧。不要添加流体模拟。"
        result = self.motion(text, {"steps": [{"instruction": text}]}, "蓝色立方体")
        self.assertEqual(result["motion_type"], "static")
        self.assertEqual(result["evidence_terms"], [])

    def test_postposed_negation_is_not_evidence(self):
        text = (
            "Animation is not required. Keyframes are absent. Simulation is disabled."
        )
        self.assertEqual(
            self.motion(text, {"instruction": text})["motion_type"], "static"
        )

    def test_actual_keyframes_are_preserved_in_both_languages(self):
        for text in (
            "Insert keyframes for cube location at frames 1 and 30.",
            "为立方体位置在第1帧和第30帧插入关键帧。",
        ):
            with self.subTest(text=text):
                result = self.motion(text, {"instruction": text})
                self.assertEqual(result["motion_type"], "dynamic")

    def test_negation_does_not_leak_past_contrast_or_new_sentence(self):
        for text in (
            "No simulation, but insert keyframes for cube location.",
            "No simulation. Insert keyframes for cube location.",
            "不需要流体模拟，但是要为立方体位置插入关键帧。",
        ):
            with self.subTest(text=text):
                result = self.motion(text, {"instruction": text})
                self.assertEqual(result["motion_type"], "dynamic")

    def test_real_simulation_is_preserved(self):
        text = "Simulate the cloth using collision physics."
        self.assertEqual(
            self.motion(text, {"instruction": text})["motion_type"], "dynamic"
        )

    def test_constant_bsdf_parameters_do_not_imply_texture(self):
        for text, title in (
            ("Set Roughness to 0.35 and Base Color to blue.", "Blue cube material"),
            ("设置粗糙度为0.35，基础色为蓝色。", "蓝色材质"),
            (
                "Use uniform roughness, no texture, noise, bump, or wave pattern.",
                "Blue cube",
            ),
        ):
            with self.subTest(text=text):
                self.assertFalse(self.material(text, title)["texture_detail_required"])

    def test_actual_surface_texture_and_roughness_variation_are_preserved(self):
        for text in (
            "Connect a Noise Texture to Roughness.",
            "Use varying roughness across the surface.",
            "使用噪声纹理驱动粗糙度。",
            "为表面设置不均匀粗糙度。",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.material(text)["texture_detail_required"])

    def test_uniform_image_texture_is_not_an_invented_procedural_pattern(self):
        for text in (
            "Use the supplied uniform blue Image Texture for Base Color. Set Metallic to 0 and Roughness to 0.35.",
            "使用提供的纯蓝色图像纹理连接基础色。不透明，金属度为0，粗糙度0.35。",
        ):
            with self.subTest(text=text):
                spec = self.material(text)
                self.assertTrue(spec["image_texture_required"])
                self.assertFalse(spec["texture_detail_required"])
                self.assertFalse(spec["procedural_texture_required"])
                self.assertNotIn("金", spec["colors_detected"])
                self.assertNotIn("透明", spec["colors_detected"])
                self.assertFalse(
                    {"metallic", "metal", "金属", "金属度"}
                    & set(spec["material_terms_detected"])
                )

    def test_positive_gold_transparency_and_nonzero_metal_are_preserved(self):
        spec = self.material("金色材质，金属度为0.8。另一片玻璃需要透明效果。")
        self.assertIn("金", spec["colors_detected"])
        self.assertIn("透明", spec["colors_detected"])
        self.assertIn("金属度", spec["material_terms_detected"])

    def test_mapped_image_detail_passes_but_does_not_replace_required_procedural_nodes(
        self,
    ):
        generated = """
def build_scene():
    mat = bpy.data.materials.new("ImageMaterial")
    mat.use_nodes = True
    image = mat.node_tree.nodes.new("ShaderNodeTexImage")
    image.image = bpy.data.images["INPUT_fixture"]
    mat.node_tree.links.new(image.outputs["Color"], mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs.build_material_spec(
                root,
                "Image detail",
                "Use an Image Texture with grain and speckles.",
                {},
            )
            self.assertTrue(
                strict_replay.review_generated_material_code(root, generated)[0]
            )
            specs.build_material_spec(
                root,
                "Procedural detail",
                "Build procedural grain using Noise Texture and a Color Ramp.",
                {},
            )
            self.assertFalse(
                strict_replay.review_generated_material_code(root, generated)[0]
            )


if __name__ == "__main__":
    unittest.main()
