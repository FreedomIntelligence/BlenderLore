"""Focused offline coverage for public launchers and explicit input adapters."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

PIPELINE = Path(__file__).resolve().parents[1]
REPO = PIPELINE.parents[1]
sys.path.insert(0, str(PIPELINE / "scripts"))
import pipeline_launcher as launcher
import provided_tutorial as provided


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="root-launcher-", dir=os.environ.get("PIPELINE_TEST_TMPDIR")
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def tutorial(self, text=None):
        source = self.root / "source.md"
        source.write_text(
            text
            or "# Fixture tutorial\n\n## 1. Add the mesh\nCreate the mesh.\n\n## 2. Save\nSave a working copy.\n",
            encoding="utf-8",
        )
        return source

    def png(self, name="frame.png"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 8), "blue").save(path)
        return path

    def test_both_public_entrypoints_help_and_secret_free_no_write_dry_run(self):
        source = self.tutorial()
        config = self.root / "private-config.json"
        config.write_text(
            json.dumps(
                {
                    "endpoint": "https://private-provider.invalid/v1/chat/completions",
                    "api_key_file": str(self.root / "MUST_NOT_READ.key"),
                    "model": "gpt-5.6-sol",
                }
            )
        )
        for name, provider in (("run_api.py", "api"), ("run_codex.py", "codex-cli")):
            with self.subTest(provider=provider):
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
                help_run = subprocess.run(
                    [sys.executable, str(REPO / name), "--help"],
                    env=env,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, help_run.returncode, help_run.stderr)
                self.assertIn("--asset-root", help_run.stdout)
                self.assertIn("legacy-rich", help_run.stdout)
                output = self.root / (provider + "-run")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(REPO / name),
                        "--tutorial",
                        str(source),
                        "--output-dir",
                        str(output),
                        "--config",
                        str(config),
                        "--dry-run",
                    ],
                    env=env,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                plan = json.loads(result.stdout)
                self.assertEqual(provider, plan["provider"])
                self.assertEqual("provided", plan["tutorial_method"])
                self.assertFalse(plan["rw1_required"])
                self.assertNotIn("private-provider", result.stdout)
                self.assertNotIn("MUST_NOT_READ", result.stdout)
                self.assertFalse(output.exists())

    def test_dry_run_never_enters_interactive_configuration(self):
        with (
            patch.object(launcher, "configure") as configure,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = launcher.main(
                "api",
                ["--configure", "--config", str(self.root / "new.json"), "--dry-run"],
            )
        configure.assert_not_called()
        self.assertEqual(1, status)
        self.assertFalse((self.root / "new.json").exists())

    def test_configure_keeps_secret_out_of_json_stdout_and_repository(self):
        dest = self.root / "private" / "pipeline.json"
        key = "DUMMY_CONFIG_TEST_SECRET"
        real_fstat = os.fstat

        def owner_only(fd):
            values = list(real_fstat(fd))
            values[0] = (values[0] & ~0o777) | 0o600
            return os.stat_result(values)

        # Test successful configuration logic independently from the external
        # test volume's permission capabilities (exFAT is tested separately).
        stdout = io.StringIO()
        with (
            patch(
                "builtins.input",
                side_effect=[
                    "https://example.org/v1/chat/completions",
                    "Provider/vision-model:stable",
                ],
            ),
            patch.object(launcher.getpass, "getpass", return_value=key),
            patch.object(launcher.os, "fstat", side_effect=owner_only),
            contextlib.redirect_stdout(stdout),
        ):
            status = launcher.main("api", ["--configure", "--config", str(dest)])
        self.assertEqual(status, 0)
        config = json.loads(dest.read_text())
        self.assertEqual(set(config), {"endpoint", "api_key_file", "model"})
        self.assertEqual("Provider/vision-model:stable", config["model"])
        key_file = Path(config["api_key_file"])
        self.assertEqual(key_file.read_text(), key)
        self.assertFalse(key_file.is_relative_to(REPO))
        self.assertNotIn(key, dest.read_text())
        self.assertNotIn(key, stdout.getvalue())

    def test_configure_rejects_unsupported_permissions_and_removes_own_files(self):
        dest = self.root / "unsupported" / "pipeline.json"
        real_fstat = os.fstat

        def ignored_permissions(fd):
            values = list(real_fstat(fd))
            values[0] = (values[0] & ~0o777) | 0o700
            return os.stat_result(values)

        stderr = io.StringIO()
        with (
            patch(
                "builtins.input",
                side_effect=["https://example.org/v1/chat/completions", ""],
            ),
            patch.object(
                launcher.getpass, "getpass", return_value="DUMMY_NEVER_STORED"
            ),
            patch.object(launcher.os, "fstat", side_effect=ignored_permissions),
            contextlib.redirect_stderr(stderr),
        ):
            status = launcher.main("api", ["--configure", "--config", str(dest)])
        self.assertEqual(status, 1)
        self.assertIn("0600", stderr.getvalue())
        self.assertNotIn("DUMMY_NEVER_STORED", stderr.getvalue())
        self.assertFalse(dest.exists())
        self.assertFalse((dest.parent / "model_api_key").exists())

    def test_configure_rejects_repository_path_and_preserves_existing_secret(self):
        existing = self.root / "model_api_key"
        existing.write_text("PRESERVE_EXISTING")
        for dest in (REPO / "private-test.json", self.root / "private.json"):
            with (
                patch("builtins.input") as prompt,
                patch.object(launcher.getpass, "getpass") as secret,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = launcher.main("api", ["--configure", "--config", str(dest)])
            self.assertEqual(status, 1)
            prompt.assert_not_called()
            secret.assert_not_called()
        self.assertEqual(existing.read_text(), "PRESERVE_EXISTING")

    def test_configure_does_not_overwrite_a_config_created_during_setup(self):
        dest = self.root / "raced" / "pipeline.json"
        real_open, real_fstat = os.open, os.fstat

        def owner_only(fd):
            values = list(real_fstat(fd))
            values[0] = (values[0] & ~0o777) | 0o600
            return os.stat_result(values)

        def concurrent_config(path, *args, **kwargs):
            if Path(path) == dest:
                dest.write_text("USER_CREATED_CONFIG")
            return real_open(path, *args, **kwargs)

        with (
            patch(
                "builtins.input",
                side_effect=["https://example.org/v1/chat/completions", ""],
            ),
            patch.object(
                launcher.getpass, "getpass", return_value="DUMMY_RACED_SECRET"
            ),
            patch.object(launcher.os, "open", side_effect=concurrent_config),
            patch.object(launcher.os, "fstat", side_effect=owner_only),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = launcher.main("api", ["--configure", "--config", str(dest)])
        self.assertEqual(status, 1)
        self.assertEqual(dest.read_text(), "USER_CREATED_CONFIG")
        self.assertFalse((dest.parent / "model_api_key").exists())

    def test_json_configuration_and_direct_parameters_resolve_identically(self):
        config = self.root / "pipeline.json"
        key_file = self.root / "do-not-read.key"
        values = {
            "endpoint": "https://example.org/v1/chat/completions",
            "api_key_file": str(key_file),
            "model": "Provider/vision-model:stable",
            "blender": "configured-blender",
        }
        config.write_text(json.dumps(values))
        with patch.dict(
            os.environ, {"BLENDER_PIPELINE_API_ENDPOINT": "ignored-env-value"}
        ):
            configured = launcher.settings(
                launcher.parser("api").parse_args(
                    ["--config", str(config), "--dry-run"]
                ),
                "api",
            )
            explicit = launcher.settings(
                launcher.parser("api").parse_args(
                    [
                        "--endpoint",
                        values["endpoint"],
                        "--api-key-file",
                        str(key_file),
                        "--model",
                        values["model"],
                        "--blender",
                        values["blender"],
                        "--dry-run",
                    ]
                ),
                "api",
            )
        self.assertEqual(configured, explicit)
        self.assertFalse(key_file.exists())

    def test_configure_default_and_explicit_model_selection(self):
        real_fstat = os.fstat

        def owner_only(fd):
            values = list(real_fstat(fd))
            values[0] = (values[0] & ~0o777) | 0o600
            return os.stat_result(values)

        for name, extra, answers, expected in (
            ("default", [], [""], "gpt-5.6-sol"),
            ("explicit", ["--model", "vendor/custom-model"], [], "vendor/custom-model"),
            ("selected-55", [], ["gpt-5.5"], "gpt-5.5"),
        ):
            with self.subTest(name=name):
                dest = self.root / name / "pipeline.json"
                with (
                    patch(
                        "builtins.input",
                        side_effect=[
                            "https://example.org/v1/chat/completions",
                            *answers,
                        ],
                    ),
                    patch.object(
                        launcher.getpass, "getpass", return_value="DUMMY_CONFIG_SECRET"
                    ),
                    patch.object(launcher.os, "fstat", side_effect=owner_only),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    status = launcher.main(
                        "api", ["--configure", "--config", str(dest), *extra]
                    )
                self.assertEqual(0, status)
                self.assertEqual(expected, json.loads(dest.read_text())["model"])

    def test_api_model_override_is_preserved_in_runtime_environment(self):
        config = self.root / "pipeline.json"
        config.write_text(json.dumps({"model": "vendor/config-model"}))
        for override, expected in (
            ([], "vendor/config-model"),
            (["--model", "Vendor/other:stable"], "Vendor/other:stable"),
            (["--model", "gpt-5.5"], "gpt-5.5"),
        ):
            with self.subTest(model=expected):
                args = launcher.parser("api").parse_args(
                    ["--config", str(config), "--dry-run", *override]
                )
                options = launcher.settings(args, "api")
                self.assertEqual(expected, options["model"])
                env = launcher.runtime_env(self.root / "runtime", options, "api", args)
                self.assertEqual(expected, env["BLENDER_PIPELINE_MODEL"])
        self.assertEqual("vendor/config-model", json.loads(config.read_text())["model"])

    def test_invalid_api_model_is_rejected_without_writing_config_or_credentials(self):
        for index, value in enumerate(
            (
                "",
                "  ",
                " model",
                "model ",
                "bad\nmodel",
                "bad\x00model",
                "bad\u2028model",
                "x" * 257,
            )
        ):
            with self.subTest(model=repr(value)):
                dest = self.root / f"invalid-{index}" / "pipeline.json"
                with (
                    patch(
                        "builtins.input",
                        return_value="https://example.org/v1/chat/completions",
                    ),
                    patch.object(
                        launcher.getpass,
                        "getpass",
                        return_value="DUMMY_INVALID_CONFIG_SECRET",
                    ),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    status = launcher.main(
                        "api", ["--configure", "--config", str(dest), "--model", value]
                    )
                self.assertEqual(1, status)
                self.assertFalse(dest.parent.exists())

    def test_codex_model_policy_remains_independent_of_api_model_selection(self):
        config = self.root / "pipeline.json"
        config.write_text(json.dumps({"model": "vendor/custom-model"}))
        args = launcher.parser("codex-cli").parse_args(
            ["--config", str(config), "--dry-run"]
        )
        with self.assertRaisesRegex(ValueError, "Codex model"):
            launcher.settings(args, "codex-cli")
        args = launcher.parser("codex-cli").parse_args(
            ["--model", "gpt-5.5", "--dry-run"]
        )
        with self.assertRaisesRegex(ValueError, "fallback-reason"):
            launcher.settings(args, "codex-cli")

    def test_video_url_validation_matches_the_extractor(self):
        fake_userinfo = ":".join(("test-user", "test-password"))
        for url in (
            "http://example.org/video.mp4",
            f"https://{fake_userinfo}@example.org/video.mp4",
            "https:///video.mp4",
        ):
            with self.subTest(url=url), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    launcher.main(
                        "codex-cli",
                        [
                            "--video-url",
                            url,
                            "--output-dir",
                            str(self.root / "url-run"),
                            "--dry-run",
                        ],
                    ),
                )
        self.assertFalse((self.root / "url-run").exists())

    def test_invalid_config_values_stop_without_exposing_contents(self):
        source = self.tutorial()
        config = self.root / "invalid.json"
        for value in (None, [], {"secret": "SENSITIVE_SENTINEL"}, 123, ""):
            config.write_text(json.dumps({"endpoint": value}))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = launcher.main(
                    "api",
                    [
                        "--tutorial",
                        str(source),
                        "--config",
                        str(config),
                        "--output-dir",
                        str(self.root / "out"),
                        "--dry-run",
                    ],
                )
            self.assertEqual(1, status)
            self.assertNotIn("SENSITIVE_SENTINEL", stderr.getvalue())
            self.assertFalse((self.root / "out").exists())

    def test_invalid_inputs_are_rejected_before_staging_or_model_work(self):
        source = self.tutorial()
        asset = self.root / "starter.blend"
        asset.write_bytes(b"fixture")
        empty_bundle = self.root / "other-bundle"
        empty_bundle.mkdir()
        cases = [
            ["--input-asset", str(self.root / "missing")],
            ["--asset", str(asset), "--asset-root", str(empty_bundle)],
            ["--preview", str(source)],
            ["--max-extraction-calls", "0"],
            ["--output-dir", str(source)],
        ]
        for extra in cases:
            with (
                self.subTest(extra=extra),
                patch.object(launcher, "stage_inputs") as stage,
                patch.object(launcher, "command") as command,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = launcher.main(
                    "codex-cli",
                    [
                        "--tutorial",
                        str(source),
                        "--output-dir",
                        str(self.root / "out"),
                        "--dry-run",
                        *extra,
                    ],
                )
                self.assertEqual(1, status)
                stage.assert_not_called()
                command.assert_not_called()
                self.assertFalse((self.root / "out").exists())

    def test_nonempty_output_and_inline_secret_config_are_rejected(self):
        source = self.tutorial()
        out = self.root / "existing"
        out.mkdir()
        preserved = out / "user-owned.txt"
        preserved.write_text("Keep me")
        with (
            patch.object(launcher, "settings", return_value={"model": "gpt-5.6-sol"}),
            patch.object(launcher, "stage_inputs") as stage,
        ):
            with self.assertRaisesRegex(ValueError, "new/empty"):
                launcher.execute(
                    "api", ["--tutorial", str(source), "--output-dir", str(out)]
                )
        stage.assert_not_called()
        self.assertEqual("Keep me", preserved.read_text())
        bad = self.root / "bad-config.json"
        bad.write_text(json.dumps({"api_key": "INLINE_SECRET_SENTINEL"}))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = launcher.main(
                "api",
                [
                    "--tutorial",
                    str(source),
                    "--output-dir",
                    str(self.root / "run"),
                    "--config",
                    str(bad),
                    "--dry-run",
                ],
            )
        self.assertEqual(1, result)
        self.assertNotIn("INLINE_SECRET_SENTINEL", stderr.getvalue())

    def test_runtime_environment_drops_dataset_policy_and_api_credentials_for_codex(
        self,
    ):
        out = self.root / "run"
        args = launcher.parser("codex-cli").parse_args([])
        options = {
            "model": "gpt-5.6-sol",
            "endpoint": "secret-endpoint",
            "key": "secret-path",
            "blender": "blender",
        }
        with patch.dict(
            os.environ,
            {
                "RW1_DATASET_ROOT": "/private/dataset",
                "RW2_WORKER": "1",
                "TOTAL_ASSET_ROOT": "/private/assets",
                "VIDEO_REPLAY_WORKER": "private",
                "BLENDER_PIPELINE_API_KEY_FILE": "/private/key",
                "BLENDER_PIPELINE_API_ENDPOINT": "private",
            },
        ):
            env = launcher.runtime_env(out, options, "codex-cli", args)
        for key in (
            "RW1_DATASET_ROOT",
            "RW2_WORKER",
            "TOTAL_ASSET_ROOT",
            "VIDEO_REPLAY_WORKER",
            "BLENDER_PIPELINE_API_KEY_FILE",
            "BLENDER_PIPELINE_API_ENDPOINT",
        ):
            self.assertNotIn(key, env)
        self.assertTrue(Path(env["TMPDIR"]).is_relative_to(out))
        self.assertEqual("codex-cli", env["BLENDER_PIPELINE_PROVIDER"])
        self.assertEqual("CYCLES", env["VIDEO2BLENDER_RENDER_ENGINE"])
        self.assertEqual("CPU", env["VIDEO2BLENDER_CYCLES_BACKEND"])

    def test_self_nested_supporting_directory_copy_is_blocked_before_copytree(self):
        source = self.root / "assets"
        source.mkdir()
        with patch.object(launcher.shutil, "copytree") as copytree:
            with self.assertRaises(ValueError):
                launcher.safe_copy(source, source / "run" / "linked_source" / "assets")
        copytree.assert_not_called()

    def test_text_only_replay_requires_finished_reference_before_model_calls(self):
        source = self.tutorial()
        out = self.root / "text-only-run"
        options = {
            "model": "gpt-5.6-sol",
            "blender": "not-run",
            "endpoint": "",
            "key": "",
        }
        with (
            patch.object(launcher, "settings", return_value=options),
            patch.object(launcher, "check_dependencies"),
            patch.object(launcher, "stage_inputs", return_value=[]),
            patch.object(launcher, "command") as command,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = launcher.main(
                "codex-cli", ["--tutorial", str(source), "--output-dir", str(out)]
            )
        self.assertEqual(1, status)
        command.assert_not_called()
        self.assertTrue((out / "tutorial.md").is_file())
        prepared = self.root / "prepared-only"
        with contextlib.redirect_stdout(io.StringIO()):
            status = launcher.main(
                "api",
                [
                    "--tutorial",
                    str(source),
                    "--output-dir",
                    str(prepared),
                    "--extract-only",
                ],
            )
        self.assertEqual(0, status)

    def test_asset_root_preserves_dependency_paths_and_exports_the_complete_bundle(
        self,
    ):
        bundle = self.root / "bundle"
        (bundle / "models").mkdir(parents=True)
        (bundle / "textures").mkdir()
        asset = bundle / "models/start.obj"
        asset.write_text("mtllib ../textures/material.mtl\nv 0 0 0\n")
        (bundle / "textures/material.mtl").write_text(
            "newmtl material\nmap_Kd color.png\n"
        )
        Image.new("RGB", (8, 8), "red").save(bundle / "textures/color.png")
        original_digest = launcher.digest(asset)
        out = self.root / "run"
        out.mkdir()
        (out / ".control").mkdir()
        args = launcher.parser("codex-cli").parse_args(
            [
                "--asset",
                str(asset),
                "--asset-root",
                str(bundle),
                "--input-asset",
                str(bundle / "textures/color.png"),
            ]
        )

        def inspect(command, env, log=None):
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps({"status": "pass", "issues": []}))

        with patch.object(launcher, "command", side_effect=inspect):
            learner = launcher.stage_inputs(
                args, out, "Fixture", {}, {"blender": "not-run-blender"}
            )
        self.assertEqual(original_digest, launcher.digest(asset))
        linked = out / "linked_source"
        self.assertTrue((linked / "models/start.obj").is_file())
        self.assertTrue((linked / "textures/material.mtl").is_file())
        self.assertTrue((linked / "textures/color.png").is_file())
        self.assertFalse(
            (linked / "color.png").exists(),
            "bundled dependencies must not be flattened",
        )
        self.assertIn(
            linked,
            learner,
            "learner export must include the complete dependency bundle",
        )
        self.assertNotIn(
            linked / "models/start.obj",
            learner,
            "a second standalone model breaks bundle-relative references",
        )

    def test_video_launcher_analysis_cache_is_outside_extraction_workspace(self):
        video = self.root / "source.mp4"
        video.write_bytes(b"fixture-only")
        out = self.root / "run"
        options = {
            "model": "gpt-5.6-sol",
            "blender": "not-run",
            "endpoint": "",
            "key": "",
        }
        with (
            patch.object(launcher, "settings", return_value=options),
            patch.object(launcher, "stage_inputs", return_value=[]),
            patch.object(launcher, "command") as command,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            launcher.execute(
                "codex-cli",
                [
                    "--video-file",
                    str(video),
                    "--output-dir",
                    str(out),
                    "--extract-only",
                ],
            )
        cmd = command.call_args.args[0]
        cache = Path(cmd[cmd.index("--cache-dir") + 1]).resolve()
        self.assertFalse(
            cache.is_relative_to(out),
            "both extraction modes reject a cache inside the workspace",
        )
        self.assertEqual(
            out.parent,
            cache.parent.parent
            if cache.parent.name == ".video-tutorial-cache"
            else cache.parent,
        )

    def test_provided_tutorial_stages_inline_reference_base64_images_and_preserves_source(
        self,
    ):
        frame = self.png("images/frame with space.png")
        encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
        source = self.tutorial(
            "# Fixture\n\n## 1. Add mesh\n"
            '![Inline](<images/frame with space.png> "Optional title")\n'
            "![Reference][frame]\n![frame][]\n![frame]\n"
            f"![Embedded](data:image/png;base64,{encoded})\n\n"
            "[frame]: <images/frame with space.png> 'Reference title'\n\n"
            "## 2. Save\nKeep the source asset unchanged.\n"
        )
        original = source.read_bytes()
        out = self.root / "run"
        out.mkdir()
        manifest = provided.stage(source, out, render_html=True)
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(2, manifest["counts"]["steps"])
        self.assertEqual(5, len(manifest["images"]))
        self.assertEqual([], provided.validate_workspace(out))
        self.assertIn(
            'title="Optional title"', (out / "illustrated_tutorial.html").read_text()
        )
        self.assertIn('"Optional title"', (out / "tutorial.md").read_text())
        self.assertNotIn("data:image", (out / "tutorial.md").read_text())
        for image in manifest["images"]:
            self.assertEqual(frame.read_bytes(), (out / image["path"]).read_bytes())
        with self.assertRaisesRegex(ValueError, "already exist"):
            provided.stage(source, out)

    def test_provided_adapter_rejects_unresolved_remote_or_outside_images(self):
        outside = self.png()
        for expression in (
            "![missing][no-definition]",
            "![remote](https://example.org/image.png)",
            "<img src='frame.png'>",
        ):
            with self.subTest(expression=expression):
                source = self.tutorial("## 1. Step\n" + expression)
                out = self.root / ("run-" + str(len(expression)))
                out.mkdir(exist_ok=True)
                with self.assertRaises(ValueError):
                    provided.stage(source, out)
                self.assertFalse((out / "tutorial_manifest.json").exists())
        nested = self.root / "document"
        nested.mkdir()
        source = nested / "source.md"
        source.write_text("## 1. Step\n![outside](../frame.png)")
        out = self.root / "outside-run"
        out.mkdir()
        with self.assertRaisesRegex(ValueError, "inside"):
            provided.stage(source, out)
        self.assertTrue(outside.is_file())

    def test_code_examples_do_not_create_fake_steps_or_image_dependencies(self):
        source = self.tutorial(
            "# Tutorial\n\n## 1. Actual operation\n"
            "```markdown\n## 88. Not an operation\n![example](missing.png)\n```\n"
            "Write `![example](also-missing.png)` as text.\n\n## 2. Save\nDone.\n"
        )
        out = self.root / "run"
        out.mkdir()
        manifest = provided.stage(source, out)
        self.assertEqual(2, manifest["counts"]["steps"])
        self.assertEqual([], manifest["images"])

    def test_top_level_ordered_operation_fallback_excludes_nested_and_fenced_lists(
        self,
    ):
        source = self.tutorial(
            "# Tutorial\n\n1. Select the mesh.\n"
            "   1. Nested detail, not another top-level operation.\n"
            "```markdown\n9. Code example, not another operation.\n```\n"
            "2) Save a working copy.\n3. `save_as_mainfile()`\n"
        )
        out = self.root / "run"
        out.mkdir()
        manifest = provided.stage(source, out)
        self.assertEqual("top_level_ordered_list", manifest["step_format"])
        self.assertEqual(3, manifest["counts"]["steps"])
        steps = json.loads((out / "steps_verified.json").read_text())["steps"]
        self.assertFalse(
            any(
                "time_range" in step or "start_sec" in step or "end_sec" in step
                for step in steps
            )
        )
        self.assertEqual(source.read_text(), (out / "tutorial.md").read_text())

    def test_real_rich_merger_path_and_base64_outputs_round_trip_as_provided_tutorials(
        self,
    ):
        production = PIPELINE / "generation/scripts"
        cache = self.root / "rich-production"
        (cache / "rich_evidence/windows").mkdir(parents=True)
        (cache / "rich_tutorial_chunks").mkdir()
        windows = []
        expected_intervals = ["00:05-00:15", "00:20-00:40", "01:05-01:50"]
        for window_index in range(2):
            image = cache / f"rich_evidence/windows/w_{window_index:03d}.jpg"
            Image.new("RGB", (12, 8), (window_index * 100, 50, 100)).save(image)
            window = {
                "start_sec": window_index * 60,
                "end_sec": (window_index + 1) * 60,
                "sheet": image.relative_to(cache).as_posix(),
            }
            windows.append(window)
            intervals = (
                expected_intervals[:2] if window_index == 0 else expected_intervals[2:]
            )
            steps = [
                {
                    "time_range": interval,
                    "action": "保留原始操作" + interval,
                    "object": "Cube",
                    "parameters": {"thickness": 0.25},
                    "evidence": "source screenshot",
                    "visual_result": "厚度可见",
                    "material_color": "red",
                    "spatial_relation": "one subject",
                    "surface_detail": "flat",
                    "implementation_notes": "preserve source",
                }
                for interval in intervals
            ]
            (cache / f"rich_tutorial_chunks/window_{window_index:03d}.json").write_text(
                json.dumps(
                    {
                        **window,
                        "window_index": window_index,
                        "steps": steps,
                        "visual_contracts": {"visible_objects": ["Cube"]},
                        "uncertain_items": [],
                    }
                )
            )
        (cache / "source.info.json").write_text(
            json.dumps({"title": "Original rich tutorial", "webpage_url": ""})
        )
        (cache / "rich_evidence/windows.json").write_text(json.dumps(windows))
        result = subprocess.run(
            [
                sys.executable,
                str(production / "merge_rich_tutorial_chunks.py"),
                "--video-dir",
                str(cache),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        path_refs = cache / "tutorial_path_refs.md"
        raw = path_refs.read_text()
        # Setup and summary lists are valid prose but not rich operation steps.
        raw = raw.replace("## 证据覆盖", "1. SETUP_NOT_AN_OPERATION\n\n## 证据覆盖")
        raw = raw.replace(
            "## 成品视觉参考", "## 成品视觉参考\n\n1. SUMMARY_NOT_AN_OPERATION"
        )
        path_refs.write_text(raw)
        result = subprocess.run(
            [
                sys.executable,
                str(production / "embed_markdown_images.py"),
                "--video-dir",
                str(cache),
                "--source-name",
                "tutorial_path_refs.md",
                "--dest-name",
                "tutorial.md",
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for filename in ("tutorial_path_refs.md", "tutorial.md"):
            with self.subTest(filename=filename):
                source = cache / filename
                original_hash = provided.sha(source)
                out = self.root / ("imported-" + source.stem)
                out.mkdir()
                manifest = provided.stage(source, out)
                self.assertEqual("rich_ordered_list", manifest["step_format"])
                self.assertEqual(3, manifest["counts"]["steps"])
                self.assertEqual(2, len(manifest["images"]))
                self.assertEqual(original_hash, provided.sha(source))
                projected = json.loads((out / "steps_verified.json").read_text())[
                    "steps"
                ]
                self.assertEqual(
                    expected_intervals, [step["time_range"] for step in projected]
                )
                self.assertFalse(
                    any(
                        "SETUP_NOT_AN_OPERATION" in step["action"]
                        or "SUMMARY_NOT_AN_OPERATION" in step["action"]
                        or "### W" in step["action"]
                        for step in projected
                    )
                )
                delivered = (out / "tutorial.md").read_text()
                self.assertIn("SETUP_NOT_AN_OPERATION", delivered)
                self.assertIn("SUMMARY_NOT_AN_OPERATION", delivered)
                self.assertEqual([], provided.validate_workspace(out))

    def test_tiled_input_audit_accepts_complete_udim_bundle_and_blocks_missing_tiles(
        self,
    ):
        asset = self.root / "source.blend"
        asset.write_bytes(b"not-opened-by-real-blender")
        image = types.SimpleNamespace(
            name="UDIM material",
            source="TILED",
            packed_file=None,
            packed_files=[],
            filepath=str(self.root / "diffuse.<UDIM>.png"),
            library=None,
            tiles=[
                types.SimpleNamespace(number=1001),
                types.SimpleNamespace(number=1002),
            ],
        )
        bpy = types.SimpleNamespace(
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(open_mainfile=Mock())),
            data=types.SimpleNamespace(images=[image], libraries=[], materials=[]),
            path=types.SimpleNamespace(abspath=lambda value, **kwargs: value),
            app=types.SimpleNamespace(version_string="fixture"),
            context=types.SimpleNamespace(
                scene=types.SimpleNamespace(objects=[], frame_start=1, frame_end=250)
            ),
        )
        spec = importlib.util.spec_from_file_location(
            "fixture_asset_inspector", PIPELINE / "scripts/inspect_input_asset.py"
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"bpy": bpy}):
            spec.loader.exec_module(module)
        self.png("diffuse.1001.png")
        self.png("diffuse.1002.png")
        result = module.inspect(asset, self.root)
        self.assertEqual("pass", result["status"])
        self.assertEqual(2, len(result["images"][0]["tile_paths"]))
        bpy.ops.wm.open_mainfile.assert_called_once_with(
            filepath=str(asset), load_ui=False, use_scripts=False
        )
        (self.root / "diffuse.1002.png").unlink()
        self.assertEqual("blocked", module.inspect(asset, self.root)["status"])

    def test_interchange_inspection_removes_default_scene_before_import(self):
        asset = self.root / "starter.glb"
        asset.write_bytes(b"fixture")
        calls = []
        bpy = types.SimpleNamespace(
            ops=types.SimpleNamespace(
                wm=types.SimpleNamespace(
                    read_factory_settings=lambda **kwargs: calls.append(
                        ("clear", kwargs)
                    )
                ),
                import_scene=types.SimpleNamespace(
                    gltf=lambda **kwargs: calls.append(("import", kwargs))
                ),
            ),
            data=types.SimpleNamespace(images=[], libraries=[], materials=[]),
            app=types.SimpleNamespace(version_string="fixture"),
            context=types.SimpleNamespace(
                scene=types.SimpleNamespace(objects=[], frame_start=1, frame_end=250)
            ),
        )
        spec = importlib.util.spec_from_file_location(
            "fixture_interchange_inspector", PIPELINE / "scripts/inspect_input_asset.py"
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"bpy": bpy}):
            spec.loader.exec_module(module)
        self.assertEqual("pass", module.inspect(asset, self.root)["status"])
        self.assertEqual(
            [("clear", {"use_empty": True}), ("import", {"filepath": str(asset)})],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
