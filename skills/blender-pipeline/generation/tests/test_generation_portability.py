from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


GENERATION_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = GENERATION_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blender_knowledge_common as knowledge_common
import build_blender_knowledge_index as knowledge_index
import project_paths


class GenerationPortabilityTests(unittest.TestCase):
    def test_all_sibling_imports_are_present(self) -> None:
        local_modules = {path.stem for path in SCRIPTS.glob("*.py")}
        expected_sibling_imports = {
            "agent_api_trace",
            "agent_trajectory",
            "blender_knowledge_common",
            "blender_version_registry",
            "build_blender_knowledge_index",
            "codex_cli_chat_bridge",
            "model_direct_generation_contracts",
            "prepare_rich_tutorial_evidence",
            "project_paths",
            "render_knowledge",
            "run_video_strict_replay",
            "rw1_static_interchange_framing",
            "video_replay_animation_contract",
            "video_replay_delivery_contract",
            "video_replay_model_client",
            "video_replay_paid_api",
            "video_replay_provider_usage",
        }
        imported_roots: set[str] = set()
        for path in SCRIPTS.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported_roots.add(node.module.split(".", 1)[0])
                elif isinstance(node, ast.Import):
                    imported_roots.update(
                        alias.name.split(".", 1)[0] for alias in node.names
                    )
        actual_sibling_imports = imported_roots & local_modules
        self.assertEqual(actual_sibling_imports, expected_sibling_imports)
        self.assertLessEqual(expected_sibling_imports, local_modules)
        runtime_provided = {
            "bpy",
            "bpy_extras",
            "gpu",
            "fcntl",
            "mathutils",
            "PIL",
            "qdrant_client",
            "requests",
            "sentence_transformers",
        }
        unresolved = {
            module
            for module in imported_roots - local_modules - runtime_provided
            if importlib.util.find_spec(module) is None
            and not (
                GENERATION_ROOT.parent / "tutorial-extraction/scripts" / f"{module}.py"
            ).is_file()
        }
        self.assertEqual(unresolved, set())

    def test_default_paths_are_skill_relative(self) -> None:
        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith(
                (
                    "VIDEO2BLENDER_",
                    "VIDEO_REPLAY_",
                    "BLENDER_",
                    "PAPER12_",
                    "TOTAL_ASSET_ROOT",
                )
            ):
                env.pop(name, None)
        command = [
            sys.executable,
            "-c",
            (
                "import project_paths as p; import video_replay_model_client as m; "
                "print(p.GENERATION_ROOT); print(p.PROJECT_ROOT); print(p.OUTPUT_ROOT); "
                "print(m.ledger_path())"
            ),
        ]
        completed = subprocess.run(
            command,
            cwd=SCRIPTS,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        roots = [Path(value) for value in completed.stdout.splitlines()]
        self.assertEqual(roots[0], GENERATION_ROOT)
        self.assertEqual(roots[1], GENERATION_ROOT)
        self.assertEqual(roots[2], GENERATION_ROOT / "output")
        self.assertEqual(
            roots[3], Path.home() / ".config/blender-pipeline/paid_api/ledger.sqlite3"
        )

    def test_legacy_environment_options_are_lower_priority_aliases(self) -> None:
        legacy = {
            "PAPER12_MODEL": "legacy-model",
            "BLENDER_PIPELINE_MODEL": "public-model",
        }
        normalized = project_paths.normalize_legacy_environment(legacy)
        self.assertEqual(normalized["BLENDER_PIPELINE_MODEL"], "public-model")

        legacy_only = {"PAPER12_MODEL": "legacy-model"}
        normalized = project_paths.normalize_legacy_environment(legacy_only)
        self.assertEqual(normalized["BLENDER_PIPELINE_MODEL"], "legacy-model")

    def test_curated_knowledge_manifest_is_complete_and_ordered(self) -> None:
        manifest = GENERATION_ROOT.parent / "knowledge/manifest.json"
        declared = json.loads(manifest.read_text(encoding="utf-8"))["documents"]
        expected = [(manifest.parent / row["path"]).resolve() for row in declared]
        self.assertEqual(
            knowledge_index._bounded_manifest_markdown_paths(manifest), expected
        )
        indexed_sources = list(
            dict.fromkeys(
                Path(chunk.source_path).resolve()
                for chunk in knowledge_index.collect_skill_chunks()
            )
        )
        self.assertEqual(
            indexed_sources,
            [
                GENERATION_ROOT / "SKILL.md",
                GENERATION_ROOT.parent / "SKILL.md",
                *expected,
            ],
        )

    def test_default_knowledge_build_excludes_workspace_artifacts(self) -> None:
        chunks = knowledge_index.build_chunks()
        self.assertTrue(chunks)
        self.assertFalse(
            any(chunk.source_type in {"paper", "run_artifact"} for chunk in chunks)
        )
        self.assertFalse(
            any(
                "output/blender_tutorial_replay" in chunk.source_path
                for chunk in chunks
            )
        )

    def test_curated_knowledge_manifest_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(
                json.dumps({"documents": [{"path": "../../../outside.md"}]}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(knowledge_index, "SKILL_ROOT", Path(directory)),
                self.assertRaises(knowledge_common.KnowledgeManifestError),
            ):
                knowledge_index._bounded_manifest_markdown_paths(manifest)

    def test_public_cli_help(self) -> None:
        commands = (
            ("run_video_replay_main.py", "--help"),
            ("run_video_strict_replay.py", "--help"),
            ("model_direct_generation_v3_harness.py", "--help"),
            ("prepare_rich_tutorial_evidence.py", "--help"),
            ("build_blender_knowledge_index.py", "--help"),
            ("retrieve_blender_knowledge.py", "--help"),
            ("update_replay_knowledge_base.py", "--help"),
        )
        for command in commands:
            with self.subTest(command=command[0]):
                completed = subprocess.run(
                    [sys.executable, str(SCRIPTS / command[0]), command[1]],
                    cwd=GENERATION_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("usage:", completed.stdout.casefold())


if __name__ == "__main__":
    unittest.main()
