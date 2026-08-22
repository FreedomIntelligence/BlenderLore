from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from blender_edit_pipeline.contracts import (
    ContractError,
    EditKind,
    EditRequest,
    atomic_write_json,
    load_request,
)


DIGEST = "a" * 64


def request(
    kind: str = "material", targets: dict | None = None, parameters: dict | None = None
) -> dict:
    return {
        "schema": "blender-edit-request/v1",
        "edit_id": "public-edit-1",
        "kind": kind,
        "source_blend": "source.blend",
        "source_sha256": DIGEST,
        "targets": targets if targets is not None else {"materials": ["Material"]},
        "parameters": parameters
        if parameters is not None
        else {"materials": {"Material": {"Roughness": 0.2}}},
    }


class ContractTests(unittest.TestCase):
    def test_all_edit_kinds_have_a_valid_scope(self) -> None:
        cases = [
            (
                "material",
                {"materials": ["M"]},
                {"materials": {"M": {"Roughness": 0.3}}},
            ),
            ("color", {"materials": ["M"]}, {"colors": {"M": [0.2, 0.3, 0.4, 1.0]}}),
            (
                "geometry",
                {"objects": ["O"]},
                {"replacements": {"O": {"vertices": [], "faces": []}}},
            ),
            (
                "modeling",
                {"generated_objects": ["G"]},
                {"operations": [{"op": "add_primitive"}]},
            ),
            ("scene", {}, {"allow_world": True, "world_color": [0.1, 0.2, 0.3]}),
        ]
        for kind, targets, parameters in cases:
            with self.subTest(kind=kind):
                self.assertEqual(
                    EditRequest.from_mapping(request(kind, targets, parameters)).kind,
                    EditKind(kind),
                )

    def test_rejects_scope_expansion_and_non_finite_values(self) -> None:
        with self.assertRaises(ContractError):
            EditRequest.from_mapping(
                request(targets={"materials": ["M"], "objects": ["O"]})
            )
        raw = request()
        raw["parameters"] = {"value": float("nan")}
        with self.assertRaises(ContractError):
            EditRequest.from_mapping(raw)
        raw = request()
        raw["evidence"] = {"render": False}
        with self.assertRaises(ContractError):
            EditRequest.from_mapping(raw)

    def test_visual_review_is_mandatory_and_joint_threshold_defaults_to_80(
        self,
    ) -> None:
        parsed = EditRequest.from_mapping(request())
        self.assertTrue(parsed.evidence.render)
        self.assertEqual(parsed.evidence.minimum_after_score, 80.0)

    def test_atomic_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "request.json")
            atomic_write_json(path, request())
            loaded = load_request(path)
            self.assertEqual(loaded.edit_id, "public-edit-1")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["schema"],
                "blender-edit-request/v1",
            )


if __name__ == "__main__":
    unittest.main()
