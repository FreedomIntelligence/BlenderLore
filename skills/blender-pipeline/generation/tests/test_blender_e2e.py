from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GENERATION = Path(__file__).resolve().parents[1]
SCRIPTS = GENERATION / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_video_strict_replay as strict_replay
from video_replay_delivery_contract import missing_canonical_six_views

BLENDER_BIN = os.environ.get("BLENDER_BIN")
ATTESTED_TEST_UUID = "GPU-00000000-0000-0000-0000-000000000001"


@unittest.skipUnless(BLENDER_BIN, "set BLENDER_BIN to run the real Blender E2E")
class BlenderGenerationEndToEndTests(unittest.TestCase):
    def test_strict_wrapper_ignores_polluted_user_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_config = root / "blender-user-config"
            user_config.mkdir()
            startup = user_config / "startup.blend"
            pollution = "\n".join(
                (
                    "import bpy",
                    "world = bpy.data.worlds.new('StartupPollutionWorld')",
                    "bpy.context.scene.world = world",
                    "bpy.context.scene.use_nodes = True",
                    "bpy.context.scene.node_tree.nodes.new('CompositorNodeRGB')",
                    "mesh = bpy.data.meshes.new('StartupPollutionMesh')",
                    "obj = bpy.data.objects.new('StartupPollutionObject', mesh)",
                    "bpy.context.collection.objects.link(obj)",
                    "bpy.data.materials.new('StartupPollutionMaterial')",
                    f"bpy.ops.wm.save_as_mainfile(filepath={str(startup)!r})",
                )
            )
            created = subprocess.run(
                [
                    str(BLENDER_BIN),
                    "--factory-startup",
                    "--background",
                    "--python-expr",
                    pollution,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)

            delivery = root / "delivery"
            delivery.mkdir()
            generated = """
def build_scene():
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.name = "GeneratedSubject"
    material = bpy.data.materials.new("GeneratedBrown")
    material.diffuse_color = (0.30, 0.12, 0.04, 1.0)
    cube.data.materials.append(material)
""".strip()
            script = strict_replay.write_script(delivery, generated)
            environment = {
                "BLENDER_USER_CONFIG": str(user_config),
                "BLENDER_PIPELINE_RENDER_W": "32",
                "BLENDER_PIPELINE_RENDER_H": "32",
                "BLENDER_PIPELINE_RENDER_SAMPLES": "1",
                "VIDEO2BLENDER_RENDER_ENGINE": "BLENDER_EEVEE_NEXT",
                "TOTAL_ASSET_EXPECTED_GPU_UUID": ATTESTED_TEST_UUID,
                "VIDEO2BLENDER_GPU_PROCESS_ATTESTED": "1",
                "VIDEO2BLENDER_GPU_PROCESS_ATTESTED_UUID": ATTESTED_TEST_UUID,
            }
            with (
                patch.object(strict_replay, "BLENDER", Path(BLENDER_BIN)),
                patch.dict(os.environ, environment, clear=False),
            ):
                returncode = strict_replay.run_blender(
                    script, delivery / "strict-wrapper.log"
                )
            self.assertEqual(
                returncode,
                0,
                (delivery / "strict-wrapper.log").read_text(
                    encoding="utf-8", errors="replace"
                ),
            )
            asset = delivery / "asset.blend"
            audit = "\n".join(
                (
                    "import bpy",
                    "assert 'GeneratedSubject' in bpy.data.objects",
                    "assert 'StartupPollutionObject' not in bpy.data.objects",
                    "assert 'StartupPollutionWorld' not in bpy.data.worlds",
                    "assert 'StartupPollutionMaterial' not in bpy.data.materials",
                    "assert bpy.context.scene.get('video2blender_canonical_blank') is True",
                )
            )
            reopened = subprocess.run(
                [
                    str(BLENDER_BIN),
                    "--factory-startup",
                    "--background",
                    str(asset),
                    "--python-expr",
                    audit,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(reopened.returncode, 0, reopened.stdout + reopened.stderr)

    def test_blank_reopen_and_complete_six_view_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blank = root / "canonical_blank.blend"
            harness = SCRIPTS / "model_direct_generation_v3_harness.py"
            subprocess.run(
                [
                    sys.executable,
                    str(harness),
                    "create-blank",
                    "--blender",
                    str(BLENDER_BIN),
                    "--output",
                    str(blank),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            reopened = subprocess.run(
                [
                    str(BLENDER_BIN),
                    "--background",
                    str(blank),
                    "--python-expr",
                    "import bpy; assert len(bpy.data.objects) == 0",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(reopened.returncode, 0, reopened.stdout + reopened.stderr)

            source = root / "source.blend"
            create = (
                "import bpy; bpy.ops.mesh.primitive_cube_add(); "
                "bpy.context.object.name='Generation_E2E_Cube'; "
                f"bpy.ops.wm.save_as_mainfile(filepath={str(source)!r})"
            )
            subprocess.run(
                [
                    str(BLENDER_BIN),
                    "--background",
                    "--factory-startup",
                    "--python-expr",
                    create,
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            delivery = root / "delivery"
            renderer = SCRIPTS / "render_asset_six_views_turntable.py"
            environment = {
                **os.environ,
                "TOTAL_ASSET_EXPECTED_GPU_UUID": ATTESTED_TEST_UUID,
                "VIDEO2BLENDER_GPU_PROCESS_ATTESTED": "1",
                "VIDEO2BLENDER_GPU_PROCESS_ATTESTED_UUID": ATTESTED_TEST_UUID,
                "VIDEO2BLENDER_RENDER_ENGINE": "BLENDER_EEVEE_NEXT",
            }
            rendered = subprocess.run(
                [
                    str(BLENDER_BIN),
                    str(source),
                    "--background",
                    "--python",
                    str(renderer),
                    "--",
                    "--out-dir",
                    str(delivery),
                    "--view-resolution",
                    "64",
                    "--samples",
                    "1",
                    "--skip-animation",
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
            self.assertEqual(missing_canonical_six_views(delivery), ())
            receipt = json.loads(
                (delivery / "postprocess_render_receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn(
                receipt["render_engine"], {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}
            )
            self.assertTrue(receipt["output_visual_attested"])
            self.assertTrue(receipt["gpu_process_attested"])
            self.assertEqual(receipt["observed_gpu_uuid"], ATTESTED_TEST_UUID.lower())


if __name__ == "__main__":
    unittest.main()
