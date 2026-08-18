from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import blender_knowledge_common as kc
import build_blender_knowledge_index as build_index
import update_replay_knowledge_base as update_replay


@contextmanager
def isolated_store(root: Path):
    paths = {
        "KNOWLEDGE_ROOT": root,
        "MANIFEST_PATH": root / "blender_knowledge_manifest.jsonl",
        "QDRANT_PATH": root / "qdrant",
        "BUILDS_PATH": root / "builds",
        "CURRENT_BUILD_PATH": root / "current.json",
        "STORE_LOCK_PATH": root / ".knowledge_store.lock",
    }

    def fake_rebuild(rows: list[dict], target: Path) -> dict:
        target.mkdir(parents=True, exist_ok=False)
        (target / "index.marker").write_text(str(len(rows)), encoding="utf-8")
        return {"collection": kc.COLLECTION_NAME, "points": len(rows), "vector_size": 1}

    with ExitStack() as stack:
        for name, value in paths.items():
            stack.enter_context(mock.patch.object(kc, name, value))
        stack.enter_context(
            mock.patch.object(update_replay, "_rebuild_qdrant", side_effect=fake_rebuild)
        )
        yield paths


def make_chunk(root: Path, logical_key: str = "video_replay:test:failure") -> kc.KnowledgeChunk:
    return kc.make_chunk(
        root / "source.json",
        "failure episode",
        "A deterministic replay failure with enough evidence for a candidate knowledge entry.",
        source_type="run_artifact",
        logical_key=logical_key,
    )


def concurrent_append_worker(root_text: str, logical_key: str, start_event, result_queue) -> None:
    root = Path(root_text)
    kc.KNOWLEDGE_ROOT = root
    kc.MANIFEST_PATH = root / "blender_knowledge_manifest.jsonl"
    kc.QDRANT_PATH = root / "qdrant"
    kc.BUILDS_PATH = root / "builds"
    kc.CURRENT_BUILD_PATH = root / "current.json"
    kc.STORE_LOCK_PATH = root / ".knowledge_store.lock"

    def fake_rebuild(rows: list[dict], target: Path) -> dict:
        target.mkdir(parents=True, exist_ok=False)
        (target / "index.marker").write_text(str(len(rows)), encoding="utf-8")
        return {"collection": kc.COLLECTION_NAME, "points": len(rows), "vector_size": 1}

    update_replay._rebuild_qdrant = fake_rebuild
    if not start_event.wait(10):
        result_queue.put((logical_key, "start_timeout"))
        return
    try:
        result_queue.put(
            (logical_key, update_replay.append_unique([make_chunk(root, logical_key)]))
        )
    except Exception as exc:  # pragma: no cover - surfaced in parent assertion.
        result_queue.put((logical_key, f"{type(exc).__name__}:{exc}"))


class KnowledgeIndexGenerationTests(unittest.TestCase):
    def test_manifest_only_build_omits_qdrant_and_read_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_store(Path(tmp)) as paths, mock.patch.object(
            build_index, "collect_total_asset_render_rule_chunks", return_value=[]
        ), mock.patch.object(build_index, "collect_render_recipe_chunks", return_value=[]):
            summary = build_index.build_generation([make_chunk(Path(tmp))], manifest_only=True)

            current = json.loads(paths["CURRENT_BUILD_PATH"].read_text(encoding="utf-8"))
            self.assertNotIn("qdrant", current)
            self.assertNotIn("qdrant", summary)
            self.assertIsNone(kc.active_qdrant_path())
            with self.assertRaises(FileNotFoundError):
                kc.qdrant_client()
            self.assertFalse(paths["QDRANT_PATH"].exists())
            self.assertFalse((kc.active_manifest_path().parent / "qdrant").exists())

    def test_incremental_update_creates_generation_without_writing_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_store(Path(tmp)) as paths:
            legacy_row = {"source_id": "legacy", "logical_source_id": "legacy", "source_hash": "old"}
            kc.write_jsonl([legacy_row], paths["MANIFEST_PATH"])
            legacy_before = paths["MANIFEST_PATH"].read_bytes()

            self.assertEqual(update_replay.append_unique([make_chunk(Path(tmp))]), 1)

            self.assertEqual(paths["MANIFEST_PATH"].read_bytes(), legacy_before)
            current = json.loads(paths["CURRENT_BUILD_PATH"].read_text(encoding="utf-8"))
            self.assertIn("qdrant", current)
            active_manifest = kc.active_manifest_path()
            self.assertNotEqual(active_manifest, paths["MANIFEST_PATH"])
            self.assertTrue(kc.active_qdrant_path().is_dir())
            self.assertEqual(
                [row["logical_source_id"] for row in kc.read_jsonl(active_manifest)],
                ["legacy", make_chunk(Path(tmp)).logical_source_id],
            )

    def test_incremental_update_never_mutates_previous_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_store(Path(tmp)) as paths:
            old_dir = paths["BUILDS_PATH"] / "old-build"
            old_dir.mkdir(parents=True)
            old_manifest = old_dir / "blender_knowledge_manifest.jsonl"
            kc.write_jsonl(
                [{"source_id": "old", "logical_source_id": "old", "source_hash": "old"}],
                old_manifest,
            )
            old_qdrant = old_dir / "qdrant"
            old_qdrant.mkdir()
            kc.atomic_write_json(
                paths["CURRENT_BUILD_PATH"],
                {
                    "schema_version": "blender-knowledge-current-v1",
                    "build_id": "old-build",
                    "manifest": "builds/old-build/blender_knowledge_manifest.jsonl",
                    "qdrant": "builds/old-build/qdrant",
                },
            )
            old_manifest_before = old_manifest.read_bytes()

            self.assertEqual(update_replay.append_unique([make_chunk(Path(tmp))]), 1)

            self.assertEqual(old_manifest.read_bytes(), old_manifest_before)
            current = json.loads(paths["CURRENT_BUILD_PATH"].read_text(encoding="utf-8"))
            self.assertNotEqual(current["build_id"], "old-build")
            self.assertIn("qdrant", current)
            self.assertEqual(
                (kc.active_qdrant_path() / "index.marker").read_text(encoding="utf-8"),
                "2",
            )
            self.assertEqual(len(kc.read_jsonl()), 2)

    def test_failed_activation_preserves_current_and_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_store(Path(tmp)) as paths:
            old_dir = paths["BUILDS_PATH"] / "old-build"
            old_dir.mkdir(parents=True)
            old_manifest = old_dir / "blender_knowledge_manifest.jsonl"
            kc.write_jsonl(
                [{"source_id": "old", "logical_source_id": "old", "source_hash": "old"}],
                old_manifest,
            )
            kc.atomic_write_json(
                paths["CURRENT_BUILD_PATH"],
                {
                    "schema_version": "blender-knowledge-current-v1",
                    "build_id": "old-build",
                    "manifest": "builds/old-build/blender_knowledge_manifest.jsonl",
                },
            )
            current_before = paths["CURRENT_BUILD_PATH"].read_bytes()
            manifest_before = old_manifest.read_bytes()
            real_atomic_write_json = kc.atomic_write_json

            def fail_current(path: Path, payload: dict) -> None:
                if path == paths["CURRENT_BUILD_PATH"]:
                    raise OSError("simulated pointer activation failure")
                real_atomic_write_json(path, payload)

            with mock.patch.object(kc, "atomic_write_json", side_effect=fail_current):
                with self.assertRaisesRegex(OSError, "pointer activation failure"):
                    update_replay.append_unique([make_chunk(Path(tmp))])

            self.assertEqual(paths["CURRENT_BUILD_PATH"].read_bytes(), current_before)
            self.assertEqual(old_manifest.read_bytes(), manifest_before)
            self.assertEqual([path.name for path in paths["BUILDS_PATH"].iterdir()], ["old-build"])

    def test_failed_vector_rebuild_preserves_current_and_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_store(Path(tmp)) as paths:
            old_dir = paths["BUILDS_PATH"] / "old-build"
            old_dir.mkdir(parents=True)
            old_manifest = old_dir / "blender_knowledge_manifest.jsonl"
            kc.write_jsonl(
                [{"source_id": "old", "logical_source_id": "old", "source_hash": "old"}],
                old_manifest,
            )
            kc.atomic_write_json(
                paths["CURRENT_BUILD_PATH"],
                {
                    "schema_version": "blender-knowledge-current-v1",
                    "build_id": "old-build",
                    "manifest": "builds/old-build/blender_knowledge_manifest.jsonl",
                },
            )
            current_before = paths["CURRENT_BUILD_PATH"].read_bytes()
            manifest_before = old_manifest.read_bytes()

            with mock.patch.object(
                update_replay,
                "_rebuild_qdrant",
                side_effect=OSError("simulated vector rebuild failure"),
            ):
                with self.assertRaisesRegex(OSError, "vector rebuild failure"):
                    update_replay.append_unique([make_chunk(Path(tmp))])

            self.assertEqual(paths["CURRENT_BUILD_PATH"].read_bytes(), current_before)
            self.assertEqual(old_manifest.read_bytes(), manifest_before)
            self.assertEqual([path.name for path in paths["BUILDS_PATH"].iterdir()], ["old-build"])

    @unittest.skipIf(kc.fcntl is None, "POSIX fcntl is required")
    def test_concurrent_updates_merge_without_lost_rows(self) -> None:
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("deterministic file-lock test requires fork")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = multiprocessing.get_context("fork")
            start_event = context.Event()
            result_queue = context.Queue()
            keys = ["video_replay:one:failure", "video_replay:two:failure"]
            processes = [
                context.Process(
                    target=concurrent_append_worker,
                    args=(str(root), key, start_event, result_queue),
                )
                for key in keys
            ]
            for process in processes:
                process.start()
            start_event.set()
            results = [result_queue.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)

            self.assertEqual(sorted(value for _, value in results), [1, 1])
            with isolated_store(root):
                rows = kc.read_jsonl()
                self.assertEqual(
                    {row["logical_source_id"] for row in rows},
                    {make_chunk(root, key).logical_source_id for key in keys},
                )
                self.assertEqual(
                    (kc.active_qdrant_path() / "index.marker").read_text(encoding="utf-8"),
                    "2",
                )

    def test_incremental_update_holds_exclusive_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, isolated_store(Path(tmp)):
            calls: list[int] = []

            def record_flock(_descriptor: int, operation: int) -> None:
                calls.append(operation)

            with mock.patch.object(kc.fcntl, "flock", side_effect=record_flock):
                self.assertEqual(update_replay.append_unique([]), 0)

            self.assertEqual(calls, [kc.fcntl.LOCK_EX, kc.fcntl.LOCK_UN])


if __name__ == "__main__":
    unittest.main()
