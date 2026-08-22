from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema.validators import Draft202012Validator


class SchemaTests(unittest.TestCase):
    def test_all_public_schemas_are_valid_json_schema(self) -> None:
        schema_dir = Path(__file__).resolve().parents[1] / "schemas"
        schemas = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(schema_dir.glob("*.json"))
        ]
        self.assertEqual(len(schemas), 5)
        self.assertEqual(len({schema["$id"] for schema in schemas}), 5)
        for schema in schemas:
            Draft202012Validator.check_schema(schema)

    def test_request_schema_rejects_unknown_operator_fields(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1] / "schemas" / "edit_request.schema.json"
        )
        validator = Draft202012Validator(
            json.loads(schema_path.read_text(encoding="utf-8"))
        )
        invalid = {
            "schema": "blender-edit-request/v1",
            "edit_id": "invalid-material",
            "kind": "material",
            "source_blend": "source.blend",
            "source_sha256": "a" * 64,
            "targets": {"materials": ["Material"]},
            "parameters": {"materials": {"Material": {"Arbitrary Socket": 1}}},
        }
        self.assertTrue(list(validator.iter_errors(invalid)))


if __name__ == "__main__":
    unittest.main()
