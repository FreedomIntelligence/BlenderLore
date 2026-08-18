from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_total_asset_render_worker as worker
import total_asset_render_knowledge as knowledge
from storage_capacity import DiskCapacity


class FakeRemote:
    gpu = "2"
    gpu_uuid = "gpu-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d"
    vulkan_available = True
    vulkan_probe_state = "available"
    vulkan_error_code = ""

    def __init__(self) -> None:
        self.commands: list[tuple[str, int]] = []
        self._vulkan_profile_paths = {"5.1": "/tmp/profile"}

    def run(self, command: str, timeout: int = 900) -> str:
        self.commands.append((command, timeout))
        return "ok"

    def put(self, _local: Path, _remote: str, timeout: int = 1800) -> None:
        del timeout

    def blender_env(self) -> str:
        return f"export CUDA_VISIBLE_DEVICES={self.gpu_uuid};"

    def vulkan_env(self, *, family: str = "") -> str:
        return self.blender_env() + f" export BLENDER_USER_CONFIG=/tmp/{family};"

    def ensure_vulkan_runtime(self, family: str, blender_binary: str) -> None:
        del family, blender_binary


class FailureKnowledgeTests(unittest.TestCase):
    def row(self, root: Path, *, asset_id: str = "000001") -> dict[str, str]:
        return {
            "asset_id": asset_id,
            "identity_key": f"identity-{asset_id}",
            "render_order": "1",
            "render_batch": "0000",
            "render_route": "static",
            "model_file": str(root / "asset.blend"),
            "source_root": str(root),
            "title": "test chair",
        }

    def capacity(self) -> DiskCapacity:
        return DiskCapacity(
            total_bytes=2 * 1024**4,
            used_bytes=1024**3,
            free_bytes=2 * 1024**4 - 1024**3,
            source="test",
        )

    def test_infrastructure_identity_is_never_a_formal_episode(self) -> None:
        self.assertTrue(knowledge.is_infrastructure_failure("remote_transport"))
        self.assertTrue(knowledge.is_infrastructure_failure("disk_or_transfer"))
        self.assertTrue(knowledge.is_infrastructure_failure(
            "remote_render_failure",
            failure_code="gpu_process_uuid_attestation_failed",
        ))
        with self.assertRaisesRegex(ValueError, "infrastructure"):
            knowledge.build_failure_episode(
                {"asset_id": "a"},
                status="failed",
                stage="primary_render",
                category="gpu_runtime_identity",
                failure_code="gpu_process_uuid_attestation_failed",
                error="uuid mismatch",
                occurred_at="2026-07-20 12:00:00",
            )

    def test_missing_local_model_has_complete_failure_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status_path = root / "status.jsonl"
            row = self.row(root)
            worker.render_one(FakeRemote(), row, status_path, "batch0000")
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(
            payload["render_knowledge_context"]["source_kind"], "total_asset"
        )
        self.assertEqual(
            payload["recipe_match"]["schema_version"],
            worker.structured_render_knowledge.MATCH_SCHEMA_VERSION,
        )
        self.assertEqual(
            payload["failure_episode"]["schema"],
            knowledge.FAILURE_EPISODE_SCHEMA_VERSION,
        )
        self.assertTrue(payload["failure_episode"]["asset_attributed"])
        self.assertIn(
            "formal_failure_episode_contract", payload["knowledge_rules_applied"]
        )

    def test_authenticated_remote_asset_failure_has_complete_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = self.row(root)
            model = Path(row["model_file"])
            model.write_bytes(b"BLENDER-v500" + b"x" * 2048)
            status_path = root / "status.jsonl"
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=self.capacity()),
                mock.patch.object(
                    worker,
                    "run_remote_render",
                    side_effect=worker.RemoteCommandError(
                        1, stderr="asset-specific renderer crash"
                    ),
                ),
            ):
                worker.render_one(FakeRemote(), row, status_path, "batch0000")
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failure_category"], "remote_render_failure")
        self.assertEqual(payload["failure_episode"]["failure_category"], "remote_render_failure")
        self.assertIn("render_knowledge_context", payload)
        self.assertIn("recipe_match", payload)

    def test_ssh_signature_remains_worker_attempt_without_formal_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = self.row(root)
            model = Path(row["model_file"])
            model.write_bytes(b"BLENDER-v500" + b"x" * 2048)
            status_path = root / "status.jsonl"
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=self.capacity()),
                mock.patch.object(
                    worker,
                    "run_remote_render",
                    side_effect=worker.RemoteCommandError(
                        1, stderr="ssh: connect to host: Connection refused"
                    ),
                ),
            ):
                with self.assertRaises(worker.RemoteTransportError):
                    worker.render_one(FakeRemote(), row, status_path, "batch0000")
            self.assertFalse(status_path.exists())


if __name__ == "__main__":
    unittest.main()
