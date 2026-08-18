import json
import stat
import tempfile
import unittest
from pathlib import Path

from blender.scripts.good_case_trajectory_archive import (
    ArchiveError,
    archive_case,
    verify_case,
)


class GoodCaseTrajectoryArchiveTests(unittest.TestCase):
    def test_archive_is_private_deduplicated_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "rollout.jsonl"
            source.write_text('{"type":"function_call"}\n', encoding="utf-8")
            artifact_tree = base / "result"
            artifact_tree.mkdir()
            (artifact_tree / "asset.blend").write_bytes(b"blend")
            root = base / "private"
            first = archive_case(
                root=root,
                case_id="chromatic_duck",
                title="Chromatic duck",
                trajectories=[source],
                api_records=[],
                artifacts=[source, artifact_tree],
                search_terms=["鸭吉吉", "chromatic_duck"],
                completeness="agent_trace_complete",
                known_gaps=["no_external_model_api_calls"],
            )
            manifest = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["captures"]), 3)
            self.assertEqual(
                manifest["captures"][0]["sha256"],
                manifest["captures"][1]["sha256"],
            )
            objects = [path for path in (root / "objects").rglob("*") if path.is_file()]
            self.assertEqual(len(objects), 2)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(objects[0].stat().st_mode), 0o600)
            self.assertEqual(manifest["completeness"], "agent_trace_complete")
            self.assertEqual(manifest["known_gaps"], ["no_external_model_api_calls"])
            tree_capture = manifest["captures"][2]
            self.assertEqual(tree_capture["tree_relative_path"], "asset.blend")
            self.assertEqual(tree_capture["tree_root"], str(artifact_tree.resolve()))
            result = verify_case(root, "chromatic_duck")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["captures_checked"], 3)

    def test_generations_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "trace.jsonl"
            source.write_text("one", encoding="utf-8")
            root = base / "archive"
            first = archive_case(
                root=root,
                case_id="crocodile_walk",
                title="Crocodile",
                trajectories=[source],
                api_records=[],
                artifacts=[],
                search_terms=[],
            )
            source.write_text("two", encoding="utf-8")
            second = archive_case(
                root=root,
                case_id="crocodile_walk",
                title="Crocodile",
                trajectories=[source],
                api_records=[],
                artifacts=[],
                search_terms=[],
            )
            self.assertNotEqual(first.parent, second.parent)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            current = json.loads(
                (root / "cases/crocodile_walk/current.json").read_text(encoding="utf-8")
            )
            self.assertIn(second.parent.name, current["manifest_path"])

    def test_invalid_case_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "trace"
            source.write_text("x", encoding="utf-8")
            with self.assertRaises(ArchiveError):
                archive_case(
                    root=Path(temporary) / "archive",
                    case_id="../escape",
                    title="bad",
                    trajectories=[source],
                    api_records=[],
                    artifacts=[],
                    search_terms=[],
                )

    def test_verify_detects_object_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "trace"
            source.write_text("original", encoding="utf-8")
            root = base / "archive"
            manifest_path = archive_case(
                root=root,
                case_id="glass_window",
                title="Glass window",
                trajectories=[source],
                api_records=[],
                artifacts=[],
                search_terms=[],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            archived = root / manifest["captures"][0]["object_path"]
            archived.write_text("corrupt", encoding="utf-8")
            with self.assertRaises(ArchiveError):
                verify_case(root, "glass_window")


if __name__ == "__main__":
    unittest.main()
