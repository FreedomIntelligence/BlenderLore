"""Real Blender projection regression; skips if Blender is not installed."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

BLENDER = os.environ.get("BLENDER_PIPELINE_TEST_BLENDER") or shutil.which("blender")
if not BLENDER and Path("/Applications/Blender.app/Contents/MacOS/Blender").is_file():
    BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"


@unittest.skipUnless(BLENDER, "Blender executable is required")
class BlenderCameraFramingTests(unittest.TestCase):
    def test_square_and_portrait_render_projection_contains_complete_cube(self):
        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        code = f"""
import bpy,sys
from mathutils import Vector
sys.path.insert(0,{scripts!r})
from camera_framing import fit_orthographic_camera
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2)
cube=bpy.context.object
vertices_before=[tuple(v.co) for v in cube.data.vertices]
bpy.ops.object.camera_add(location=(0,-6,.7))
camera=bpy.context.object
camera.data.type="ORTHO"
scene=bpy.context.scene
scene.camera=camera
for width,height,position in [(384,384,(0,-6,.7)),(128,512,(4,-6,4))]:
    scene.render.resolution_x=width
    scene.render.resolution_y=height
    scene.render.resolution_percentage=100
    camera.location=position
    camera.rotation_euler=(-camera.location).to_track_quat("-Z","Y").to_euler()
    camera.data.ortho_scale=2.16
    before_rotation=tuple(camera.rotation_euler)
    receipt=fit_orthographic_camera(scene,camera,[cube])
    assert receipt["adjusted"],receipt
    assert receipt["passed"],receipt
    assert min(receipt["projection_after"][0],receipt["projection_after"][2])>=.06999,receipt
    assert max(receipt["projection_after"][1],receipt["projection_after"][3])<=.93001,receipt
    assert before_rotation==tuple(camera.rotation_euler)
    assert vertices_before==[tuple(v.co) for v in cube.data.vertices]
    print("FRAME_FIT_OK",width,height,receipt)
"""
        result = subprocess.run(
            [
                BLENDER,
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--python-expr",
                code,
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.count("FRAME_FIT_OK"), 2, result.stdout + result.stderr
        )


if __name__ == "__main__":
    unittest.main()
