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

    def material(self, tutorial: str, title="Blue cube", steps=None):
        with tempfile.TemporaryDirectory() as root:
            return specs.build_material_spec(Path(root), title, tutorial, steps or {})

    def test_attribution_and_step_verification_metadata_do_not_change_subject(self):
        tutorial = (
            "# Blender 冰块建模教程\n"
            "*视频原帧｜BV0000000000｜奶油小狗设计｜00:56*\n"
            "## 1. 建立基础立方体\n添加立方体并用玻璃材质制作冰块。\n"
            "来源：[原视频](https://example.com/cat-dog-animation)\n"
        )
        steps = {
            "verification_note": "Compatibility filename; video verification.",
            "source": {"uploader": "Blue Cat Studio", "title": "dog"},
            "steps": [
                {
                    "action": "添加立方体，设置玻璃材质。",
                    "verification_status": "user_supplied_not_independently_video_verified",
                    "source": "cat channel",
                    "url": "https://example.com/dog",
                }
            ],
        }
        spec = self.material(tutorial, "Blender 冰块建模教程", steps)
        self.assertEqual(spec["subject_family"], "general_asset")
        self.assertEqual(spec["animal_terms_detected"], [])
        self.assertNotIn("蓝", spec["colors_detected"])
        self.assertTrue("玻璃" in spec["material_terms_detected"])

    def test_english_and_table_attribution_are_not_subject_evidence(self):
        for metadata in (
            "**Uploader:** Golden Dog Studio",
            "| Author | Blue Cat Studio |",
            "Source URL: https://example.com/animal/glass",
        ):
            with self.subTest(metadata=metadata):
                spec = self.material(
                    metadata + "\n## 1. Add a cube\nUse a uniform grey material.",
                    "Object modelling",
                    {"steps": [{"action": "Add a cube."}]},
                )
                self.assertEqual(spec["animal_terms_detected"], [])
                self.assertEqual(spec["subject_family"], "general_asset")
                self.assertNotIn("glass", spec["material_terms_detected"])

    def test_real_animal_operations_remain_authoritative(self):
        for operation in ("Model a cat body with two ears.", "创建小猫的身体和耳朵。"):
            with self.subTest(operation=operation):
                spec = self.material(
                    "Author: Neutral Studio\n## 1. Model the subject\n" + operation,
                    "Character exercise",
                    {"steps": [{"action": operation}]},
                )
                self.assertEqual(spec["subject_family"], "animal_cartoon_character")

    def test_verification_words_and_negated_animals_are_not_confirmation(self):
        spec = self.material(
            "A dog is shown only as a reference example. Model the block, not an animal.",
            "Object modelling",
            {
                "verification_note": "verification",
                "steps": [
                    {
                        "action": "Create the block. Do not model a dog or cat.",
                        "verification_status": "verified",
                    }
                ],
            },
        )
        self.assertEqual(spec["subject_family"], "general_asset")

    def test_empty_startup_scene_is_not_the_final_subject(self):
        for title in (
            "Blue beveled cube from an empty scene",
            "Build a cube in a blank Blender scene",
            "从空白场景制作冰块",
            "默认场景中的蓝色立方体",
        ):
            with self.subTest(title=title):
                result = self.material("Add one cube.", title)
                self.assertEqual(result["subject_family"], "general_asset")
                self.assertEqual(result["title_scene_environment_terms_detected"], [])

    def test_actual_room_and_scene_subjects_remain_positive(self):
        for title in (
            "Build a kitchen scene from a blank scene",
            "Build an empty room",
            "从空场景制作卧室",
            "咖啡店场景建模",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    self.material("Build the room.", title)["subject_family"],
                    "scene_environment",
                )

    def test_static_cube_negations_and_origin_do_not_request_motion(self):
        text = "Create a cube at the origin. No animation. No keyframe or simulated motion. Set uniform Roughness to 0.35."
        result = self.motion(text, {"steps": [{"instruction": text}]})
        self.assertEqual(result["motion_type"], "static")
        self.assertEqual(result["evidence_terms"], [])
        self.assertEqual(result["presentation_profile"], "studio_turntable")
        self.assertFalse(self.material(text)["texture_detail_required"])

    def test_motion_does_not_come_from_attribution_or_schema_metadata(self):
        tutorial = (
            "Author: Animation Simulation Rig Studio\n## 1. Add a cube\nAdd a cube."
        )
        steps = {
            "source": {"uploader": "Animation Studio", "title": "Cloth simulation"},
            "verification_note": "animation schema",
            "steps": [{"action": "Add a cube.", "source": "Rig Simulation Studio"}],
        }
        result = self.motion(tutorial, steps)
        self.assertEqual(result["motion_type"], "static")
        self.assertEqual(result["evidence_terms"], [])
        steps["steps"][0]["action"] = (
            "Animate cube location with keyframes at 1 and 30."
        )
        self.assertEqual(self.motion(tutorial, steps)["motion_type"], "dynamic")

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

    def test_chinese_negative_texture_enumerations_remain_negative(self):
        for text in (
            "面部本身保持平整，无花纹、凹凸或划痕。",
            "可见表面无明确花纹、斑点、凹凸、划痕或接缝。",
            "截图中无清晰可辨的贴图花纹、凹凸、划痕或接缝。",
            "最终为非金属、不透明、非发光的纯色材质。",
        ):
            with self.subTest(text=text):
                result = self.material(text)
                self.assertFalse(result["texture_detail_required"])
                self.assertFalse(result["procedural_texture_required"])
                self.assertNotIn("金属", result["material_terms_detected"])

    def test_procedural_geometry_does_not_require_a_textured_material(self):
        for text in (
            "应用修改器后，程序化生成的内壁与顶部环带会转换为基础网格，才能直接挤出和倒角瓶唇。",
            "Use procedural geometry and then apply the Solidify modifier. Keep a uniform grey material.",
            "程序化建模完成瓶体，保持纯灰材质。",
        ):
            with self.subTest(text=text):
                spec = self.material(
                    text,
                    "Bottle modelling",
                    {"steps": [{"implementation_notes": text}]},
                )
                self.assertFalse(spec["texture_detail_required"])
                self.assertFalse(spec["procedural_texture_required"])

    def test_geometry_only_tutorial_allows_neutral_untextured_presentation(self):
        spec = self.material(
            "Extrude a cylinder into a milk bottle. No material or color operations are taught.",
            "Milk bottle modelling",
        )
        self.assertFalse(spec["texture_detail_required"])
        self.assertFalse(spec["procedural_texture_required"])
        self.assertTrue(
            any(
                "allow neutral, untextured gray/white" in rule
                for rule in spec["hard_constraints"]
            )
        )

    def test_explicit_character_colors_and_material_rules_remain(self):
        instruction = "Model a blue cat with black eyes."
        spec = self.material(instruction, "Cat", {"steps": [{"action": instruction}]})
        self.assertEqual(spec["subject_family"], "animal_cartoon_character")
        self.assertIn("blue", spec["colors_detected"])
        self.assertIn("black", spec["colors_detected"])
        self.assertIn("character_material_policy", spec)
        self.assertTrue(
            any(
                "Animal/cartoon" in value or "animal/cartoon" in value
                for value in spec["hard_constraints"]
            )
        )

    def test_explicit_procedural_materials_still_require_their_mechanism(self):
        for text in (
            "Create a procedural texture for the material.",
            "Build a procedural shader.",
            "创建程序化材质。",
            "使用程序化方式生成纹理。",
            "使用程序化生成贴图。",
        ):
            with self.subTest(text=text):
                spec = self.material(text)
                self.assertTrue(spec["texture_detail_required"])
                self.assertTrue(spec["procedural_texture_required"])
        for text in ("Do not add a procedural texture.", "不要使用程序化材质。"):
            with self.subTest(text=text):
                self.assertFalse(self.material(text)["procedural_texture_required"])

    def test_texture_negation_stops_before_an_affirmative_operation(self):
        for text in (
            "无斑点、凹凸或划痕，但是添加噪声纹理控制粗糙度。",
            "无图片贴图，使用 Noise Texture 制作表面纹理。",
            "制作无缝噪声纹理。",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.material(text)["texture_detail_required"])
                self.assertTrue(self.material(text)["procedural_texture_required"])

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
