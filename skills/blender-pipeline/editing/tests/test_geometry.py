from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from blender_edit_pipeline.contracts import ContractError
from blender_edit_pipeline.operators.geometry import (
    bbox_extent_ratios,
    bbox_volume,
    bounds,
    center,
    fit_vertices_to_bbox,
    validate_faces,
    validate_generation_handoff,
)
from blender_edit_pipeline.operators.scene import _vector3


class GeometryTests(unittest.TestCase):
    def test_fit_preserves_target_center_and_extent(self) -> None:
        source = [(-1, -1, -1), (1, 1, 1)]
        fitted = fit_vertices_to_bbox(source, ((10, 20, 30), (14, 26, 38)))
        self.assertEqual(bounds(fitted), ((10.0, 20.0, 30.0), (14.0, 26.0, 38.0)))
        self.assertEqual(bbox_extent_ratios(fitted, fitted), (1.0, 1.0, 1.0))
        self.assertEqual(center(bounds(fitted)), (12.0, 23.0, 34.0))
        self.assertEqual(bbox_volume(bounds(fitted)), 192.0)

    def test_faces_and_camera_vectors_are_strict(self) -> None:
        self.assertEqual(validate_faces([[0, 1, 2]], 3), [(0, 1, 2)])
        with self.assertRaises(ContractError):
            validate_faces([[0, 1, 3]], 3)
        with self.assertRaises(ContractError):
            _vector3([1, 2], "camera location", -10, 10)

    def test_generation_handoff_requires_accepted_self_hashed_receipt_and_asset_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "asset.blend"
            asset.write_bytes(b"BLENDER" * 4)
            asset_sha = hashlib.sha256(asset.read_bytes()).hexdigest()
            receipt = {
                "schema": "video2blender.model-direct-generation-attempt.v3",
                "acceptance_scope": "web_delivery_joint_at_80_candidate",
                "execution_is_formal": False,
                "sandbox_attested": False,
                "task_id": "GEN-V3-998",
                "attempt": 1,
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
                        "size_bytes": asset.stat().st_size,
                    }
                },
                "acceptance": {
                    "acceptance_scope": "web_delivery_joint_at_80_candidate",
                    "rubric_score": 90,
                    "vlm_score": 88,
                    "hard_gates_passed": True,
                    "critical_criteria_passed": True,
                    "judge_protocol_valid": True,
                    "joint_at_threshold": True,
                },
            }
            body = json.dumps(
                receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            receipt["receipt_sha256"] = hashlib.sha256(body).hexdigest()
            receipt_path = root / "attempt_receipt.json"
            receipt_path.write_text(
                json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n",
                encoding="utf-8",
            )
            spec = {
                "attempt_receipt": str(receipt_path),
                "attempt_receipt_sha256": hashlib.sha256(
                    receipt_path.read_bytes()
                ).hexdigest(),
                "asset_blend": str(asset),
                "asset_blend_sha256": asset_sha,
                "object": "GeneratedObject",
            }
            verified = validate_generation_handoff(spec, "Target")
            self.assertEqual(verified["asset_sha256"], asset_sha)
            rejected = json.loads(receipt_path.read_text(encoding="utf-8"))
            rejected["status"] = "below_threshold"
            receipt_path.write_text(json.dumps(rejected), encoding="utf-8")
            spec["attempt_receipt_sha256"] = hashlib.sha256(
                receipt_path.read_bytes()
            ).hexdigest()
            with self.assertRaises(ContractError):
                validate_generation_handoff(spec, "Target")


if __name__ == "__main__":
    unittest.main()
