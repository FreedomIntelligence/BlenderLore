"""Blender-backed source-topology audit, without modifying input assets."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BLENDER = os.environ.get("BLENDER_PIPELINE_TEST_BLENDER") or shutil.which("blender")
if not BLENDER and Path("/Applications/Blender.app/Contents/MacOS/Blender").is_file():
    BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"


@unittest.skipUnless(BLENDER, "Blender executable is required")
class InputAssetTopologyTests(unittest.TestCase):
    def test_native_and_split_glb_topology_is_reported_without_mutation(self):
        inspector = Path(__file__).resolve().parents[1] / "scripts"
        with tempfile.TemporaryDirectory(
            prefix="input-topology-", dir=os.environ.get("PIPELINE_TEST_TMPDIR")
        ) as temporary:
            code = f"""
import bpy,hashlib,json,sys
from pathlib import Path
from mathutils import Vector
sys.path.insert(0,{str(inspector)!r})
import inspect_input_asset as inspector
root=Path({temporary!r})
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2)
bpy.context.object.name="StudyCube"
native=root/"cube.blend"
glb=root/"cube.glb"
bpy.ops.wm.save_as_mainfile(filepath=str(native))
bpy.ops.export_scene.gltf(filepath=str(glb),export_format="GLB")
hashes={{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (native,glb)}}
a=inspector.inspect(native,root)
b=inspector.inspect(glb,root)
na=next(o for o in a["objects"] if o["type"]=="MESH")
gb=next(o for o in b["objects"] if o["type"]=="MESH")
assert na["vertices"]==8 and na["boundary_edges"]==0 and na["coincident_vertex_count"]==0,na
assert gb["vertices"]==24 and gb["boundary_edges"]==24 and gb["nonmanifold_edges"]==24 and gb["coincident_vertex_count"]==16,gb
obj=next(o for o in bpy.context.scene.objects if o.type=="MESH")
raw_vertices=[tuple(v.co) for v in obj.data.vertices]
raw_uvs=[[tuple(v.uv) for v in layer.data] for layer in obj.data.uv_layers]
bevel=obj.modifiers.new("Diagnostic Bevel","BEVEL")
bevel.width=.12
bevel.segments=3
bevel.limit_method="ANGLE"
bpy.context.view_layer.update()
before=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
before_distance=min((v.co-Vector((1,1,1))).length for v in before.data.vertices)
obj.modifiers.clear()
weld=obj.modifiers.new("Diagnostic Weld","WELD")
weld.merge_threshold=.00001
bevel=obj.modifiers.new("Diagnostic Bevel","BEVEL")
bevel.width=.12
bevel.segments=3
bevel.limit_method="ANGLE"
bpy.context.view_layer.update()
after=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
after_distance=min((v.co-Vector((1,1,1))).length for v in after.data.vertices)
assert before_distance<1e-6 and after_distance>.01,(before_distance,after_distance)
assert raw_vertices==[tuple(v.co) for v in obj.data.vertices]
assert raw_uvs==[[tuple(v.uv) for v in layer.data] for layer in obj.data.uv_layers]
assert all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in hashes.items())
print("TOPOLOGY_AUDIT_PASS="+json.dumps({{"native":na,"glb":gb,"bevel_only_corner_distance":before_distance,"weld_bevel_corner_distance":after_distance,"original_files_and_mesh_unchanged":True}}))
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
            self.assertIn(
                "TOPOLOGY_AUDIT_PASS=", result.stdout, result.stdout + result.stderr
            )


if __name__ == "__main__":
    unittest.main()
