from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import jsonschema


BLENDER_BIN = os.environ.get("BLENDER_BIN")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _self_hash(value: dict) -> str:
    return hashlib.sha256(_canonical(value).rstrip(b"\n")).hexdigest()


@unittest.skipUnless(BLENDER_BIN, "set BLENDER_BIN to run the real Blender E2E")
class BlenderEndToEndTests(unittest.TestCase):
    def _run_blender(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BLENDER_BIN), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
        )

    def _assert_blender_success(
        self, completed: subprocess.CompletedProcess[str]
    ) -> None:
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertNotIn("Traceback (most recent call last)", output, output)

    def test_all_modes_require_review_and_generation_geometry_handoff(self) -> None:
        editing = Path(__file__).resolve().parents[1]
        pipeline = editing / "src" / "blender_edit_pipeline" / "pipeline.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.blend"
            create = """
import bpy
from mathutils import Vector
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
for item in list(bpy.data.materials):
    bpy.data.materials.remove(item)
mesh = bpy.data.meshes.new('BaseMesh')
mesh.from_pydata([(-1,-1,-1),(1,-1,-1),(0,1,-1),(0,0,1)], [], [(0,1,2),(0,3,1),(1,3,2),(2,3,0)])
uv = mesh.uv_layers.new(name='UVMap')
for item in uv.data:
    item.uv = (0.0, 0.0)
obj = bpy.data.objects.new('Base', mesh)
bpy.data.collections['Collection'].objects.link(obj)
obj.modifiers.new(name='StaticMirror', type='MIRROR')
mat = bpy.data.materials.new('Material')
mat.use_nodes = True
mesh.materials.append(mat)
animated_mesh = mesh.copy()
animated = bpy.data.objects.new('AnimatedBase', animated_mesh)
bpy.data.collections['Collection'].objects.link(animated)
animated.location = (6, 0, 0)
animated.keyframe_insert(data_path='location', frame=1)
animated.location.x = 7
animated.keyframe_insert(data_path='location', frame=12)
constraint = animated.constraints.new(type='LIMIT_ROTATION')
constraint.name = 'AnimatedConstraint'
sensitive_mesh = mesh.copy()
sensitive = bpy.data.objects.new('SensitiveBase', sensitive_mesh)
bpy.data.collections['Collection'].objects.link(sensitive)
sensitive.location = (8, 0, 0)
solidify = sensitive.modifiers.new(name='TopologySensitiveSolidify', type='SOLIDIFY')
solidify.thickness = 0.4
solidify.offset = 1.0
shape_mesh = mesh.copy()
shape = bpy.data.objects.new('ShapeKeyBase', shape_mesh)
bpy.data.collections['Collection'].objects.link(shape)
shape.location = (10, 0, 0)
shape.shape_key_add(name='Basis')
key = shape.shape_key_add(name='Deform')
key.data[3].co.z += 0.5
world = bpy.context.scene.world
world.use_nodes = True
world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.03, 0.03, 0.03, 1.0)
camera_data = bpy.data.cameras.new('CameraData')
camera = bpy.data.objects.new('Camera', camera_data)
bpy.data.collections['Collection'].objects.link(camera)
camera.location = (4.5, -4.5, 3.5)
camera.rotation_euler = ((Vector((0,0,0)) - camera.location).to_track_quat('-Z', 'Y').to_euler())
bpy.context.scene.camera = camera
light_data = bpy.data.lights.new('KeyData', type='AREA')
light_data.energy = 900
light_data.shape = 'DISK'
light_data.size = 4
light = bpy.data.objects.new('Key', light_data)
bpy.data.collections['Collection'].objects.link(light)
light.location = (3, -2, 5)
light.rotation_euler = ((Vector((0,0,0)) - light.location).to_track_quat('-Z', 'Y').to_euler())
scene = bpy.context.scene
scene.render.engine = 'BLENDER_EEVEE'
scene.render.resolution_x = 64
scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
scene.use_nodes = True
bpy.ops.wm.save_as_mainfile(filepath=SOURCE_PATH)
""".replace("SOURCE_PATH", repr(str(source)))
            completed = self._run_blender(
                [
                    "--background",
                    "--factory-startup",
                    "--python-expr",
                    create,
                ]
            )
            self._assert_blender_success(completed)

            attempt_dir = root / "generation" / "attempt-1"
            attempt_dir.mkdir(parents=True)
            generated_asset = attempt_dir / "asset.blend"
            generated = """
import bpy
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, location=(2.0, -1.0, 0.5))
obj = bpy.context.object
obj.name = 'GeneratedReplacement'
modifier = obj.modifiers.new(name='GeneratedBevel', type='BEVEL')
modifier.width = 0.08
modifier.segments = 2
bpy.ops.wm.save_as_mainfile(filepath=ASSET_PATH)
""".replace("ASSET_PATH", repr(str(generated_asset)))
            completed = self._run_blender(
                [
                    "--background",
                    "--factory-startup",
                    "--python-expr",
                    generated,
                ]
            )
            self._assert_blender_success(completed)
            asset_sha = _sha256(generated_asset)
            attempt = {
                "schema": "video2blender.model-direct-generation-attempt.v3",
                "acceptance_scope": "web_delivery_joint_at_80_candidate",
                "execution_is_formal": False,
                "sandbox_attested": False,
                "task_id": "GEN-V3-999",
                "attempt": 1,
                "source_task_sha256": "9" * 64,
                "status": "web_candidate_accepted",
                "hard_gates": {
                    "execution_success": True,
                    "asset_saved": True,
                    "fresh_reopen": True,
                    "evidence_complete": True,
                    "no_external_dependencies": True,
                },
                "artifacts": {
                    "asset": {
                        "path": "asset.blend",
                        "sha256": asset_sha,
                        "size_bytes": generated_asset.stat().st_size,
                    }
                },
                "acceptance": {
                    "acceptance_scope": "web_delivery_joint_at_80_candidate",
                    "threshold": 80.0,
                    "rubric_score": 91.0,
                    "vlm_score": 89.0,
                    "hard_gates_passed": True,
                    "critical_criteria_passed": True,
                    "judge_protocol_valid": True,
                    "joint_at_threshold": True,
                },
            }
            attempt["receipt_sha256"] = _self_hash(attempt)
            attempt_path = attempt_dir / "attempt_receipt.json"
            attempt_path.write_bytes(_canonical(attempt))

            digest = _sha256(source)
            handoff_spec = {
                "attempt_receipt": str(attempt_path),
                "attempt_receipt_sha256": _sha256(attempt_path),
                "asset_blend": str(generated_asset),
                "asset_blend_sha256": asset_sha,
                "object": "GeneratedReplacement",
                "fit_to_source_bbox": True,
                "envelope_tolerance": 0.01,
                "volume_ratio_tolerance": 0.01,
                "world_center_tolerance": 0.00001,
            }
            cases = {
                "material": (
                    {"materials": ["Material"]},
                    {"materials": {"Material": {"Metallic": 1.0, "Roughness": 0.05}}},
                ),
                "color": (
                    {"materials": ["Material"]},
                    {"colors": {"Material": [0.05, 0.8, 0.2, 1.0]}},
                ),
                "geometry": (
                    {"objects": ["Base"]},
                    {
                        "replacements": {
                            "Base": handoff_spec,
                        }
                    },
                ),
                "modeling": (
                    {"generated_objects": ["Added"]},
                    {
                        "operations": [
                            {
                                "op": "add_primitive",
                                "name": "Added",
                                "primitive": "uv_sphere",
                                "collection": "Collection",
                                "location": [1.5, 0, 0],
                                "scale": [0.4, 0.4, 0.4],
                            }
                        ]
                    },
                ),
                "scene": ({}, {"allow_world": True, "world_color": [0.2, 0.35, 0.7]}),
            }
            schemas = {
                path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in (editing / "schemas").glob("*.json")
            }
            for kind, (targets, parameters) in cases.items():
                with self.subTest(kind=kind):
                    request_path = root / f"{kind}.json"
                    review_dir = root / f"review-{kind}"
                    output = root / f"edited-{kind}"
                    request = {
                        "schema": "blender-edit-request/v1",
                        "edit_id": f"{kind}-e2e",
                        "kind": kind,
                        "source_blend": str(source),
                        "source_sha256": digest,
                        "targets": targets,
                        "parameters": parameters,
                        "evidence": {
                            "render": True,
                            "resolution": 64,
                            "min_changed_fraction": 0.0001,
                            "min_mean_absolute_delta": 0.01,
                            "minimum_after_score": 80,
                        },
                    }
                    request_path.write_bytes(_canonical(request))
                    prepared = self._run_blender(
                        [
                            str(source),
                            "--background",
                            "--python",
                            str(pipeline),
                            "--",
                            "prepare",
                            "--request",
                            str(request_path),
                            "--review-dir",
                            str(review_dir),
                        ]
                    )
                    self._assert_blender_success(prepared)
                    review = json.loads(
                        (review_dir / "review_package.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(review["status"], "awaiting_review")
                    self.assertFalse((review_dir / "receipt.json").exists())
                    self.assertTrue((review_dir / "candidate.blend").is_file())
                    bound = {row["path"]: row for row in review["artifacts"]}
                    assessment = {
                        "schema": "blender-edit-assessment/v1",
                        "decision": "accepted",
                        "review_package_sha256": _sha256(
                            review_dir / "review_package.json"
                        ),
                        "bindings": {
                            "candidate_sha256": bound["candidate.blend"]["sha256"],
                            "before_sha256": bound["before.png"]["sha256"],
                            "after_sha256": bound["after.png"]["sha256"],
                        },
                        "assessor": {"kind": "human", "name": "Blender E2E reviewer"},
                        "scores": {"before": 82, "after": 86},
                        "summary": "The declared edit remains visually coherent and does not reduce quality.",
                    }
                    assessment_path = root / f"assessment-{kind}.json"
                    assessment_path.write_bytes(_canonical(assessment))
                    finalized = self._run_blender(
                        [
                            "--background",
                            "--factory-startup",
                            "--python",
                            str(pipeline),
                            "--",
                            "finalize",
                            "--review-dir",
                            str(review_dir),
                            "--assessment",
                            str(assessment_path),
                            "--output-dir",
                            str(output),
                        ]
                    )
                    self._assert_blender_success(finalized)
                    receipt = json.loads(
                        (output / "receipt.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(receipt["status"], "complete")
                    self.assertTrue(receipt["reopen"]["verified"])
                    self.assertEqual(receipt["non_target_drift"], [])
                    self.assertIsInstance(receipt["visual_evidence"], dict)
                    self.assertGreaterEqual(
                        receipt["visual_evidence"]["assessment"]["after_score"],
                        receipt["visual_evidence"]["assessment"]["before_score"],
                    )
                    self.assertTrue((output / "asset.blend").is_file())
                    if kind == "geometry":
                        validation = receipt["changes"]["geometry_validation"]["Base"]
                        self.assertEqual(
                            validation["provenance"]["source"],
                            "accepted_generation_attempt",
                        )
                        self.assertTrue(validation["material_slots_preserved"])
                        self.assertTrue(validation["object_identity_preserved"])
                        self.assertAlmostEqual(
                            validation["bbox_volume_ratio"], 1.0, places=6
                        )
                        self.assertLessEqual(
                            validation["world_center_delta"],
                            validation["world_center_tolerance"],
                        )
                    documents = [
                        (request, schemas["edit_request.schema.json"]),
                        (
                            json.loads(
                                (review_dir / "source_map.json").read_text(
                                    encoding="utf-8"
                                )
                            ),
                            schemas["source_map.schema.json"],
                        ),
                        (review, schemas["review_package.schema.json"]),
                        (assessment, schemas["assessment.schema.json"]),
                        (receipt, schemas["edit_receipt.schema.json"]),
                    ]
                    for document, schema in documents:
                        jsonschema.Draft202012Validator(schema).validate(document)

            rejected_targets = {
                "modifier-envelope-drift": ("SensitiveBase", "evaluated visible"),
                "animated-target": ("AnimatedBase", "animation/drivers/NLA"),
                "shape-key-target": ("ShapeKeyBase", "shape keys"),
            }
            for case_name, (target_name, expected_error) in rejected_targets.items():
                with self.subTest(rejected=case_name):
                    request_path = root / f"reject-{case_name}.json"
                    review_dir = root / f"rejected-review-{case_name}"
                    request = {
                        "schema": "blender-edit-request/v1",
                        "edit_id": f"reject-{case_name}",
                        "kind": "geometry",
                        "source_blend": str(source),
                        "source_sha256": digest,
                        "targets": {"objects": [target_name]},
                        "parameters": {"replacements": {target_name: handoff_spec}},
                        "evidence": {"render": True, "resolution": 64},
                    }
                    request_path.write_bytes(_canonical(request))
                    rejected = self._run_blender(
                        [
                            str(source),
                            "--background",
                            "--python",
                            str(pipeline),
                            "--",
                            "prepare",
                            "--request",
                            str(request_path),
                            "--review-dir",
                            str(review_dir),
                        ]
                    )
                    output = rejected.stdout + rejected.stderr
                    self.assertIn('"status": "failed"', output, output)
                    self.assertIn(expected_error, output, output)
                    self.assertFalse(review_dir.exists())


if __name__ == "__main__":
    unittest.main()
