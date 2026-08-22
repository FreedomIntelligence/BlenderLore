from __future__ import annotations

import sys
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import model_direct_generation_contracts as contracts
import blender_version_registry as version_registry
from video_replay_animation_contract import (
    effective_route,
    motion_plan_has_verified_time_animation,
)
from video_replay_paid_api import normalize_chat_completions_endpoint


class GenerationContractTests(unittest.TestCase):
    def test_generated_source_accepts_blender_and_rejects_io(self) -> None:
        contracts.audit_generated_python_source(
            "import bpy\nbpy.ops.mesh.primitive_cube_add()\n"
        )
        rejected = (
            "import os\nos.listdir('.')\n",
            "from pathlib import Path\nPath('asset.blend').read_bytes()\n",
            "open('asset.blend', 'rb')\n",
            "import bpy\ngetattr(bpy, 'data')\n",
            "prefix = 'ht'\nendpoint = prefix + 'tps://example.com'\n",
            "import bpy\nprint(bpy.__dict__)\n",
            "template = '{}'\nprint(template.format(dynamic_value))\n",
        )
        for source in rejected:
            with self.subTest(source=source):
                with self.assertRaises(contracts.GenerationContractError):
                    contracts.audit_generated_python_source(source)

    def test_model_payload_is_exact_and_has_no_private_provenance(self) -> None:
        payload = {
            "task_id": "GEN-V3-001",
            "title": "Ceramic cup",
            "instruction": "Build a centered ceramic cup",
            "category": "single_object",
        }
        self.assertEqual(contracts.validate_sanitized_task_payload(payload), payload)
        with self.assertRaises(contracts.GenerationContractError):
            contracts.validate_sanitized_task_payload({**payload, "source": "hidden"})
        with self.assertRaises(contracts.GenerationContractError):
            contracts.validate_sanitized_task_payload(
                {**payload, "instruction": "Use https://example.com/source.mp4"}
            )
        with self.assertRaises(contracts.GenerationContractError):
            contracts.validate_sanitized_task_payload(
                payload, expected={**payload, "title": "Different task"}
            )

    def test_dynamic_route_requires_verified_subject_motion(self) -> None:
        self.assertFalse(
            motion_plan_has_verified_time_animation(
                {"motion_mechanisms": ["camera_orbit", "hair_presence"]}
            )
        )
        self.assertEqual(
            effective_route("dynamic", verified_scene_animation=False),
            ("static", "dynamic_without_verified_scene_animation"),
        )
        self.assertTrue(
            motion_plan_has_verified_time_animation(
                {"motion_mechanisms": ["shape_key_animation"]}
            )
        )

    def test_paid_endpoint_normalization_requires_https(self) -> None:
        self.assertEqual(
            normalize_chat_completions_endpoint("https://example.com/v1"),
            "https://example.com/v1/chat/completions",
        )
        with self.assertRaises(ValueError):
            normalize_chat_completions_endpoint("http://example.com/v1")

    def test_blender_archive_extraction_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.tar"
            payload = b"must-not-escape"
            with tarfile.open(archive, "w") as handle:
                member = tarfile.TarInfo("../escape.txt")
                member.size = len(payload)
                handle.addfile(member, BytesIO(payload))

            destination = root / "extract"
            destination.mkdir()
            with self.assertRaises(ValueError):
                version_registry._extract_tar_safely(archive, destination)
            self.assertFalse((root / "escape.txt").exists())

    def test_blender_release_checksum_requires_one_exact_filename(self) -> None:
        digest = "a" * 64
        filename = "blender-5.1.1-linux-x64.tar.xz"
        self.assertEqual(
            version_registry._parse_release_sha256(
                f"{'b' * 64}  other.tar.xz\n{digest}  {filename}\n",
                filename,
            ),
            digest,
        )
        with self.assertRaises(ValueError):
            version_registry._parse_release_sha256(
                f"{digest}  other.tar.xz\n", filename
            )


if __name__ == "__main__":
    unittest.main()
