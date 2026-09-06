"""Offline coverage of the skill adapter; no downloads or model execution."""

from __future__ import annotations

import base64
import errno
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PIPELINE = Path(__file__).resolve().parents[1]
REPO = PIPELINE.parents[1]
SCRIPT = PIPELINE / "scripts" / "launch_from_codex.py"
sys.path.insert(0, str(SCRIPT.parent))
import launch_from_codex as adapter


class CodexSkillLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="codex-skill-launcher-", dir=os.environ.get("PIPELINE_TEST_TMPDIR")
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "unrelated working directory"
        self.cwd.mkdir()
        self.video = self.root / "source video.mp4"
        # Dry-run checks routing and paths, never media decoding.
        self.video.write_bytes(b"offline video routing fixture")
        self.bundle = self.root / "authorized asset bundle"
        self.bundle.mkdir()
        self.asset = self.bundle / "starting project.blend"
        self.asset.write_bytes(b"offline asset routing fixture")
        self.texture = self.bundle / "texture image.png"
        self.preview = self.root / "starting preview.png"
        self.target = self.root / "finished result.png"
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+aNogAAAAASUVORK5CYII="
        )
        for path in (self.texture, self.preview, self.target):
            path.write_bytes(png)
        self.tutorial = self.root / "provided tutorial.md"
        self.tutorial.write_text(
            "# Provided tutorial\n\n## 1. Create the scene\nAdd a cube.\n\n"
            "## 2. Save the scene\nSave a working copy.\n",
            encoding="utf-8",
        )

    def inputs(self):
        return {
            "video_url": ["--video-url", "https://example.org/tutorial?v=demo"],
            "local_video": ["--video-file", str(self.video)],
            "video_assets": [
                "--video-file",
                str(self.video),
                "--asset",
                str(self.asset),
                "--asset-root",
                str(self.bundle),
                "--input-asset",
                str(self.texture),
                "--input-asset",
                str(self.bundle),
                "--preview",
                str(self.preview),
                "--target-image",
                str(self.target),
            ],
            "markdown": [
                "--tutorial",
                str(self.tutorial),
                "--target-image",
                str(self.target),
            ],
        }

    def test_forwards_all_inputs_unchanged_with_full_pipeline_defaults(self):
        for name, inputs in self.inputs().items():
            with self.subTest(input=name):
                argv = inputs + ["--output-dir", str(self.root / (name + " output"))]
                with patch.object(
                    adapter.pipeline_launcher, "main", return_value=0
                ) as run:
                    self.assertEqual(0, adapter.main(argv))
                run.assert_called_once_with("codex-cli", argv)
                self.assertIs(argv, run.call_args.args[1])
                args = adapter.pipeline_launcher.parser("codex-cli").parse_args(argv)
                self.assertFalse(args.extract_only)
                self.assertFalse(args.check)
                self.assertEqual("visual", args.tutorial_method)
                self.assertEqual("balanced", args.profile)
                self.assertEqual(8, args.max_replay_calls)
                self.assertEqual(500000, args.max_replay_tokens)
                self.assertEqual(2, args.repair_attempts)

    def test_preserves_exit_codes_and_default_argv(self):
        for status in (0, 1, 2, 17):
            with self.subTest(status=status):
                with patch.object(
                    adapter.pipeline_launcher, "main", return_value=status
                ) as run:
                    self.assertEqual(status, adapter.main())
                run.assert_called_once_with("codex-cli", None)

    def test_tutorial_only_choice_reaches_shared_pipeline_for_each_input(self):
        for name, inputs in self.inputs().items():
            with self.subTest(input=name):
                output = self.root / (name + " tutorial only")
                result = self.invoke(
                    SCRIPT,
                    inputs
                    + [
                        "--output-dir",
                        str(output),
                        "--extract-only",
                        "--render-html",
                        "--dry-run",
                    ],
                )
                self.assertEqual(0, result.returncode, result.stderr)
                plan = json.loads(result.stdout)
                self.assertTrue(plan["extract_only"])
                self.assertEqual(
                    ["input staging", "tutorial preparation"], plan["stages"]
                )
                self.assertEqual("codex-cli", plan["provider"])
                self.assertFalse(output.exists())
                if name == "video_assets":
                    self.assertEqual(str(self.asset), plan["input_project"])
                    self.assertEqual([str(self.preview)], plan["input_previews"])

    def test_markdown_tutorial_only_finishes_without_reconstruction_or_model(self):
        launcher = adapter.pipeline_launcher
        for html in (False, True):
            with self.subTest(html=html):
                output = self.root / ("prepared html" if html else "prepared markdown")
                argv = [
                    "--tutorial",
                    str(self.tutorial),
                    "--output-dir",
                    str(output),
                    "--extract-only",
                ]
                if html:
                    argv.append("--render-html")
                with patch.object(launcher, "command") as stage_command:
                    self.assertEqual(0, adapter.main(argv))
                    stage_command.assert_not_called()
                self.assertEqual(
                    self.tutorial.read_text(encoding="utf-8"),
                    (output / "tutorial.md").read_text(encoding="utf-8"),
                )
                self.assertEqual(html, (output / "illustrated_tutorial.html").exists())
                self.assertFalse((output / "asset.blend").exists())
                self.assertFalse((output / "pipeline_review.json").exists())

    def test_root_launcher_is_a_compatibility_shim(self):
        spec = importlib.util.spec_from_file_location(
            "codex_root_shim", REPO / "run_codex.py"
        )
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)
        self.assertIs(adapter.main, shim.main)
        self.assertTrue(sys.dont_write_bytecode)

    def invoke(self, script, argv):
        return subprocess.run(
            [sys.executable, str(script), *argv],
            cwd=self.cwd,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def assert_full_plan(self, result, output, name):
        self.assertEqual(0, result.returncode, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual("codex-cli", plan["provider"])
        self.assertEqual("gpt-5.6-sol", plan["model"])
        self.assertFalse(plan["extract_only"])
        self.assertFalse(plan["rw1_required"])
        self.assertEqual(
            "provided" if name == "markdown" else "visual", plan["tutorial_method"]
        )
        self.assertEqual(
            [
                "input staging",
                "tutorial preparation",
                "knowledge retrieval",
                "existing strict Blender replay",
                "route-specific review",
            ],
            plan["stages"],
        )
        self.assertEqual(str(output), plan["output_dir"])
        self.assertFalse(output.exists(), "Dry-run must not create the run directory")
        return plan

    def test_direct_and_compatibility_invocation_from_independent_cwd(self):
        for script in (SCRIPT, REPO / "run_codex.py"):
            for name, inputs in self.inputs().items():
                with self.subTest(script=script.name, input=name):
                    output = self.root / (script.stem + " " + name + " output")
                    result = self.invoke(
                        script, inputs + ["--output-dir", str(output), "--dry-run"]
                    )
                    plan = self.assert_full_plan(result, output, name)
                    if name == "video_assets":
                        self.assertEqual(str(self.asset), plan["input_project"])
                        self.assertEqual(
                            [str(self.texture), str(self.bundle)],
                            plan["supporting_inputs"],
                        )
                        self.assertEqual([str(self.preview)], plan["input_previews"])
                        self.assertEqual(str(self.target), plan["target_image"])

    def test_symlinked_skill_invocation_from_independent_cwd(self):
        installed = self.root / "installed skill with spaces"
        try:
            installed.symlink_to(PIPELINE, target_is_directory=True)
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS}:
                self.skipTest("Test filesystem does not support symlinks")
            raise
        output = self.root / "symlinked skill output"
        result = self.invoke(
            installed / "scripts" / SCRIPT.name,
            self.inputs()["video_assets"] + ["--output-dir", str(output), "--dry-run"],
        )
        self.assert_full_plan(result, output, "video_assets")

    def test_subprocess_preserves_validation_failure_exit_code(self):
        for script in (SCRIPT, REPO / "run_codex.py"):
            with self.subTest(script=script.name):
                result = self.invoke(
                    script, ["--video-url", "https://example.org/video", "--dry-run"]
                )
                self.assertEqual(1, result.returncode)
                self.assertIn("--output-dir is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
