from __future__ import annotations

import struct
from pathlib import Path
import tempfile
import unittest
import zlib

from blender_edit_pipeline.contracts import EditRequest
from blender_edit_pipeline.pipeline import (
    PipelineError,
    finalize_pipeline,
    validate_assessment,
)
from blender_edit_pipeline.qa import (
    QualityError,
    image_difference,
    validate_non_target_drift,
)


def _request(kind: str, targets: dict, parameters: dict) -> EditRequest:
    return EditRequest.from_mapping(
        {
            "schema": "blender-edit-request/v1",
            "edit_id": "qa",
            "kind": kind,
            "source_blend": "source.blend",
            "source_sha256": "b" * 64,
            "targets": targets,
            "parameters": parameters,
        }
    )


def _snapshot() -> dict:
    return {
        "objects": {
            "A": {
                "type": "MESH",
                "identity": {},
                "material_slots": ["M"],
                "modifiers": [],
                "constraints": [],
                "animation_data": {"drivers": [], "nla_tracks": []},
                "data": 1,
            }
        },
        "materials": {"M": {"roughness": 0.5}},
        "collections": {"C": ["A"]},
        "world": {"color": [0, 0, 0]},
        "compositor": {"use_nodes": False, "node_tree": None},
        "scene_settings": {"exposure": 0},
        "animation": [],
    }


def _png(path: Path, rgb: tuple[int, int, int], invalid_filter: bool = False) -> None:
    raw = bytes([5 if invalid_filter else 0, *rgb])

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class QualityTests(unittest.TestCase):
    def test_material_scope_detects_object_drift(self) -> None:
        before, after = _snapshot(), _snapshot()
        after["materials"]["M"] = {"roughness": 0.2}
        request = _request(
            "material", {"materials": ["M"]}, {"materials": {"M": {"Roughness": 0.2}}}
        )
        self.assertEqual(validate_non_target_drift(before, after, request), [])
        after["objects"]["A"]["identity"] = {"location": [1, 0, 0]}
        self.assertEqual(
            validate_non_target_drift(before, after, request), ["objects.A"]
        )

    def test_constraints_drivers_nla_and_compositor_are_visible_as_drift(self) -> None:
        request = _request(
            "material", {"materials": ["M"]}, {"materials": {"M": {"Roughness": 0.2}}}
        )
        before, after = _snapshot(), _snapshot()
        after["objects"]["A"]["constraints"] = [{"name": "Unexpected"}]
        self.assertIn("objects.A", validate_non_target_drift(before, after, request))
        before, after = _snapshot(), _snapshot()
        after["objects"]["A"]["animation_data"]["drivers"] = [{"path": "scale"}]
        self.assertIn("objects.A", validate_non_target_drift(before, after, request))
        before, after = _snapshot(), _snapshot()
        after["objects"]["A"]["animation_data"]["nla_tracks"] = [{"name": "Unexpected"}]
        self.assertIn("objects.A", validate_non_target_drift(before, after, request))
        before, after = _snapshot(), _snapshot()
        after["compositor"] = {"use_nodes": True, "node_tree": {}}
        self.assertIn("compositor", validate_non_target_drift(before, after, request))

    def test_modeling_allows_only_declared_collection_membership(self) -> None:
        before, after = _snapshot(), _snapshot()
        after["objects"]["Generated"] = {"type": "MESH"}
        after["collections"]["C"] = ["A", "Generated"]
        request = _request(
            "modeling",
            {"generated_objects": ["Generated"]},
            {
                "operations": [
                    {
                        "op": "add_primitive",
                        "name": "Generated",
                        "primitive": "cube",
                        "collection": "C",
                    }
                ]
            },
        )
        self.assertEqual(validate_non_target_drift(before, after, request), [])
        after["collections"]["Other"] = ["Generated"]
        self.assertIn(
            "objects.Generated:collection-membership",
            validate_non_target_drift(before, after, request),
        )

    def test_png_difference_and_invalid_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            before, after, invalid = (
                Path(directory, name)
                for name in ("before.png", "after.png", "invalid.png")
            )
            _png(before, (0, 0, 0))
            _png(after, (3, 6, 9))
            _png(invalid, (0, 0, 0), True)
            result = image_difference(before, after)
            self.assertEqual(result.changed_fraction, 1.0)
            self.assertEqual(result.mean_absolute_delta, 6.0)
            with self.assertRaises(QualityError):
                image_difference(before, invalid)

    def test_external_assessment_is_hash_bound_and_forbids_regression(self) -> None:
        artifacts = {
            "candidate.blend": {"sha256": "a" * 64},
            "before.png": {"sha256": "b" * 64},
            "after.png": {"sha256": "c" * 64},
        }
        assessment = {
            "schema": "blender-edit-assessment/v1",
            "decision": "accepted",
            "review_package_sha256": "d" * 64,
            "bindings": {
                "candidate_sha256": "a" * 64,
                "before_sha256": "b" * 64,
                "after_sha256": "c" * 64,
            },
            "assessor": {"kind": "vlm", "name": "independent-reviewer"},
            "scores": {"before": 82, "after": 86},
            "summary": "Candidate preserves the original composition and improves the target.",
        }
        validated = validate_assessment(
            assessment,
            review_sha256="d" * 64,
            artifacts=artifacts,
            minimum_after_score=80,
        )
        self.assertEqual(validated["scores"]["after"], 86.0)
        assessment["scores"] = {"before": 90, "after": 89}
        with self.assertRaises(QualityError):
            validate_assessment(
                assessment,
                review_sha256="d" * 64,
                artifacts=artifacts,
                minimum_after_score=80,
            )
        assessment["scores"] = {"before": 70, "after": 79}
        with self.assertRaises(QualityError):
            validate_assessment(
                assessment,
                review_sha256="d" * 64,
                artifacts=artifacts,
                minimum_after_score=80,
            )

    def test_finalize_rejects_review_directory_symlink_before_blender_load(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review = root / "review"
            review.mkdir()
            alias = root / "review-alias"
            alias.symlink_to(review, target_is_directory=True)
            with self.assertRaises(PipelineError):
                finalize_pipeline(alias, root / "assessment.json", root / "output")


if __name__ == "__main__":
    unittest.main()
