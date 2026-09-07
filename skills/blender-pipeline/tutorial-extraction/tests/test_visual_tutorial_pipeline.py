from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import extract_video_tutorial as cli
import prepare_video
import visual_tutorial_pipeline as visual
import validate_visual_package as check
import validate_tutorial_package as legacy_check


def fixture(root: Path):
    frames = {}
    for number, color in [(0, "white"), (4, "red"), (9, "blue"), (14, "green")]:
        name = f"frames/frame_{number * 1000:09d}.jpg"
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), color).save(path)
        frames[name] = {
            "path": name,
            "timestamp": number,
            "sha256": visual.transport.sha256_path(path),
        }
    doc = {
        "content_name": "几何练习",
        "purpose": "制作三个有序网格步骤。",
        "starting_scene": "默认场景",
        "closing": "保留模型结构并另存工程。",
        "final_result_description": "三个依次完成的网格操作。",
        "learner_inputs": [],
        "missing_inputs": [],
        "cover_frame": "frames/frame_000000000.jpg",
        "steps": [],
    }
    for i, t in enumerate([4, 9, 14], 1):
        frame = f"frames/frame_{t * 1000:09d}.jpg"
        doc["steps"].append(
            {
                "number": i,
                "title": f"完成第{i}个网格操作",
                "time_start": t - 3,
                "time_end": t,
                "actions": [f"选择网格并执行操作{i}"],
                "expected": f"网格状态{i}",
                "caption": f"操作{i}后的网格",
                "evidence_frame": frame,
                "claims": [
                    {"text": f"执行操作{i}", "status": "shown", "at": t, "frame": frame}
                ],
            }
        )
    source = {
        "id": "fixture",
        "title": "几何练习",
        "duration_seconds": 15,
        "url": "https://example.com/tutorial",
    }
    rubric = {
        "schema_version": "1.0",
        "total_points": 100,
        "status_weights": {"PASS": 1.0, "PARTIAL": 0.5, "FAIL": 0.0},
        "capability_totals": {"GEO": 100},
        "rubric": [
            {
                "id": "GEO-01",
                "capability": "GEO",
                "points": 100,
                "criterion": "网格存在",
                "scoring_rule": "存在得100分，否则0分",
                "artifact_verifier_check": "GEO-01",
            }
        ],
        "artifact_verifier": {
            "engine": "blender_python",
            "python_source": "import bpy\nchecks = {'GEO-01': any(o.type == 'MESH' for o in bpy.data.objects)}\n",
        },
    }
    return doc, source, rubric, frames


class VisualTutorialTests(unittest.TestCase):
    def test_api_model_ids_are_preserved_in_both_extraction_plans(self):
        for method in ("visual", "legacy-rich"):
            for model in ("vendor/gemini-example-vision", "gpt-5.5"):
                with self.subTest(method=method, model=model):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        cli.main(
                            [
                                "--video-url",
                                "https://example.com/tutorial",
                                "--title",
                                "Example",
                                "--output-dir",
                                "/unused/dry-run",
                                "--provider",
                                "api",
                                "--model",
                                model,
                                "--tutorial-method",
                                method,
                                "--dry-run",
                            ]
                        )
                    plan = json.loads(output.getvalue())
                    self.assertEqual(model, plan["model"])
                    self.assertEqual("", plan["fallback_reason"])
        for model in ("vendor/custom", "gpt-5.5"):
            with self.assertRaises(visual.transport.ExtractionError):
                visual.transport.validate_model_fallback(model)
        for model in (
            "",
            " ",
            " vendor/custom",
            "vendor/custom\n",
            "vendor\x00/custom",
            "x" * 257,
        ):
            with (
                self.subTest(invalid_model=model),
                self.assertRaises(visual.transport.ExtractionError),
            ):
                visual.transport.validate_model_fallback(model, provider="api")

    def test_api_custom_model_reaches_request_and_response_identity_gate(self):
        model = "vendor/gemini-example-vision"
        with tempfile.TemporaryDirectory() as raw:
            picture = Path(raw) / "evidence.jpg"
            Image.new("RGB", (16, 16), "white").save(picture)
            for observed in (model, "vendor/different-model"):
                event = {
                    "id": "fixture-response",
                    "model": observed,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": '{"result":"ok"}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
                response = SimpleNamespace(
                    status_code=200,
                    content=(
                        "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
                    ).encode(),
                )
                client = visual.transport.ModelClient(
                    endpoint="https://provider.invalid/v1/chat/completions",
                    key="fixture-key",
                    model=model,
                    profile=visual.transport.PROFILES["balanced"],
                    call_budget=1,
                )
                with patch.object(visual.transport.requests, "Session") as session:
                    session.return_value.post.return_value = response
                    if observed == model:
                        self.assertEqual(
                            {"result": "ok"}, client.call("Inspect evidence", picture)
                        )
                    else:
                        with self.assertRaises(visual.transport.ExtractionError):
                            client.call("Inspect evidence", picture)
                    payload = json.loads(
                        session.return_value.post.call_args.kwargs["data"]
                    )
                    self.assertEqual(model, payload["model"])
                    self.assertNotIn("reasoning_effort", payload)
        self.assertTrue(legacy_check._model_identity_matches(model, model))
        self.assertFalse(
            legacy_check._model_identity_matches(model, "vendor/different-model")
        )

    def test_manifest_schema_accepts_api_model_and_keeps_codex_fallback_gate(self):
        schema = json.loads(
            (SCRIPTS.parent / "schemas/manifest.schema.json").read_text()
        )
        validator = legacy_check.jsonschema.Draft202012Validator(schema)
        manifest = {
            "schema": "video2blender-tutorial-manifest.v2",
            "status": "complete",
            "created_at": "2026-09-07T00:00:00Z",
            "title": "Example",
            "profile": "balanced",
            "model": "vendor/custom-vision",
            "provider": "api",
            "fallback_reason": "",
            "source": {
                "kind": "local_file",
                "sha256": "a" * 64,
                "duration_seconds": 10,
            },
            "transcript": {},
            "counts": {},
            "outputs": {},
            "warnings": [],
            "model_usage": {
                "calls": 1,
                "reported_calls": 1,
                "response_model": "vendor/custom-vision",
                "finish_reason": "stop",
                "call_budget": 1,
            },
            "raw_model_responses_persisted": False,
            "source_video_persisted": False,
        }
        self.assertEqual([], list(validator.iter_errors(manifest)))
        manifest["model"] = "gpt-5.5"
        self.assertEqual([], list(validator.iter_errors(manifest)))
        manifest["provider"] = "codex-cli"
        self.assertTrue(list(validator.iter_errors(manifest)))
        manifest["fallback_reason"] = "Requested Codex deployment unavailable"
        self.assertEqual([], list(validator.iter_errors(manifest)))

    def test_quiet_command_keeps_failure_reason_without_signed_url(self):
        failure = subprocess.CalledProcessError(
            1,
            ["yt-dlp"],
            stderr="HTTP Error 412: Precondition Failed https://example.org/video?token=secret",
        )
        with patch.object(
            visual.transport.subprocess, "run", side_effect=failure
        ) as run:
            with self.assertRaises(visual.transport.ExtractionError) as caught:
                visual.transport.run_command(["yt-dlp"], capture=False)
        self.assertIn("412: Precondition Failed", str(caught.exception))
        self.assertNotIn("token", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(subprocess.PIPE, run.call_args.kwargs["stderr"])

    def test_url_download_bounds_network_retries(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.mp4").touch()
            with (
                patch.object(
                    visual.transport, "_yt_dlp_command", return_value=["yt-dlp"]
                ),
                patch.object(visual.transport, "run_command") as run,
                patch.object(
                    visual.transport, "fetch_platform_subtitles", return_value=[]
                ),
            ):
                video, _ = visual.transport.materialize_url(
                    "https://example.org/video", root
                )
            self.assertEqual(root / "source.mp4", video)
            command = run.call_args.args[0]
            self.assertEqual("15", command[command.index("--socket-timeout") + 1])
            self.assertEqual("1", command[command.index("--retries") + 1])
            self.assertEqual("1", command[command.index("--fragment-retries") + 1])
            self.assertIn("--no-progress", command)

    def test_owned_cleanup_tolerates_only_vanished_entries(self):
        def vanished(_path, *, onerror):
            onerror(
                None,
                "._input",
                (FileNotFoundError, FileNotFoundError("vanished sidecar"), None),
            )

        def denied(_path, *, onerror):
            onerror(None, "private", (PermissionError, PermissionError("denied"), None))

        with patch.object(visual.transport.shutil, "rmtree", side_effect=vanished):
            visual.transport.remove_owned_tree(Path("run-owned-staging"))
        with patch.object(visual.transport.shutil, "rmtree", side_effect=denied):
            with self.assertRaises(PermissionError):
                visual.transport.remove_owned_tree(Path("run-owned-staging"))

    def test_rubric_accepts_complete_status_conditions_but_rejects_partial_mapping(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, rubric, _ = fixture(root)
            path = root / "rubric.json"
            rubric["rubric"][0]["scoring_rule"] = {
                "PASS": "Mesh and topology match.",
                "PARTIAL": "Mesh exists but topology differs.",
                "FAIL": "No mesh.",
            }
            path.write_text(json.dumps(rubric), encoding="utf-8")
            check.validate_rubric(path)
            del rubric["rubric"][0]["scoring_rule"]["PARTIAL"]
            path.write_text(json.dumps(rubric), encoding="utf-8")
            with self.assertRaises(SystemExit):
                check.validate_rubric(path)

    def test_package_and_adapter_preserve_whole_procedure_without_rubric_leak(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            analysis = root / "analysis"
            doc, source, rubric, frames = fixture(analysis)
            workspace = root / "workspace"
            package = workspace / "tutorial_package" / doc["content_name"]
            files = visual.write_package(
                package, doc, rubric, source, analysis, frames, {}
            )
            self.assertEqual([], list((package / "input").iterdir()))
            self.assertEqual(3, len(check.content_entries(package / "output")))
            visual.publish_workspace(
                workspace, package, doc, source, files, {}, frames, True
            )
            self.assertEqual([], legacy_check.validate_package(workspace))
            text = (workspace / "tutorial.md").read_text()
            self.assertIn("起始场景", text)
            self.assertIn("收尾与保存", text)
            self.assertNotIn("GEO-01", text)
            self.assertNotIn("rubric", text.lower())
            self.assertNotIn("data:image", text)
            self.assertEqual(4, len(visual.IMAGE_RE.findall(text)))
            self.assertTrue((workspace / "illustrated_tutorial.html").is_file())
            self.assertEqual(
                3,
                len(
                    json.loads((workspace / "steps_verified.json").read_text())["steps"]
                ),
            )
            generation_scripts = SCRIPTS.parents[1] / "generation" / "scripts"
            sys.path.insert(0, str(generation_scripts))
            try:
                import run_video_strict_replay
                import run_video_replay_main

                self.assertEqual(
                    3,
                    len(
                        run_video_strict_replay.ordered_tutorial_visual_evidence(
                            workspace
                        )
                    ),
                )
                self.assertEqual(
                    "pass",
                    run_video_replay_main.tutorial_visual_text_status(workspace)[
                        "status"
                    ],
                )
            finally:
                sys.path.pop(0)

    def test_unavailable_input_blocks_package(self):
        with tempfile.TemporaryDirectory() as raw:
            doc, source, rubric, frames = fixture(Path(raw))
            doc["learner_inputs"] = [{"name": "duck.blend", "use": "打开原版鸭模型"}]
            with self.assertRaisesRegex(visual.Error, "unavailable"):
                visual.validate_document(doc, frames, source["duration_seconds"], set())

    def test_images_and_operation_order_are_not_silently_repaired(self):
        with tempfile.TemporaryDirectory() as raw:
            doc, source, rubric, frames = fixture(Path(raw))
            doc["steps"][0]["claims"][0]["frame"] = "frames/missing.jpg"
            with self.assertRaisesRegex(visual.Error, "shown claim"):
                visual.validate_document(doc, frames, 15, set())

    def test_actual_inputs_are_copied_and_paths_rebased(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doc, source, rubric, frames = fixture(root / "analysis")
            asset = root / "original.blend"
            asset.write_bytes(b"source-fixture")
            doc["learner_inputs"] = [{"name": asset.name, "use": "打开并保存工作副本"}]
            workspace = root / "run"
            package = workspace / "tutorial_package" / doc["content_name"]
            files = visual.write_package(
                package,
                doc,
                rubric,
                source,
                root / "analysis",
                frames,
                {asset.name: asset},
            )
            visual.publish_workspace(workspace, package, doc, source, files, {}, frames)
            self.assertEqual(
                asset.read_bytes(), (package / "input" / asset.name).read_bytes()
            )
            self.assertIn(
                "tutorial_package/几何练习/input/original.blend",
                (workspace / "tutorial.md").read_text(),
            )

    def test_interval_and_large_video_sampling(self):
        with self.assertRaises(ValueError):
            prepare_video.prepare(Path("video.mp4"), Path("cache"), 0)
        self.assertEqual(2, visual.sampling(57, "balanced"))
        self.assertEqual(5, visual.sampling(400, "balanced"))
        self.assertEqual(15, visual.sampling(900, "balanced"))

    def test_subject_paths_are_safe_for_unchanged_markdown_consumer_and_windows(self):
        for name in ["Geometry Nodes (Ice)", "冰块 #1", "CON", "A/B"]:
            safe = visual.safe_name(name)
            self.assertNotRegex(safe, r"[\s()/\\#%]")
            self.assertNotEqual("CON", safe)

    def test_cli_calls_new_skill_with_existing_arguments(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "source.mp4"
            video.write_bytes(b"fixture")
            with patch.object(
                cli, "extract_visual_tutorial", return_value={"status": "complete"}
            ) as extract:
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "--video-file",
                            str(video),
                            "--title",
                            "Test",
                            "--output-dir",
                            str(root / "out"),
                            "--provider",
                            "codex-cli",
                            "--workspace-mode",
                            "--window-budget",
                            "0",
                        ]
                    ),
                )
            self.assertTrue(extract.call_args.kwargs["workspace_mode"])
            self.assertEqual("gpt-5.6-sol", extract.call_args.kwargs["model"])

    def test_new_extraction_orchestration_uses_skill_not_legacy_fragments(self):
        self._assert_extraction_orchestration("codex-cli", "gpt-5.6-sol")

    def test_api_custom_model_uses_full_visual_orchestration_and_package_validation(
        self,
    ):
        self._assert_extraction_orchestration("api", "vendor/custom-vision")

    def _assert_extraction_orchestration(self, provider, model):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "source.mp4"
            video.write_bytes(b"video-fixture")
            doc, source, rubric, _ = fixture(root / "fixture")
            ledger = {
                "summary": "完整几何流程",
                "steps": doc["steps"],
                "evidence_requests": [],
                "final_result_candidates": [doc["cover_frame"]],
            }
            responses = iter([ledger, ledger, doc, rubric])

            class Client:
                def __init__(self, **kwargs):
                    self.model = kwargs["model"]
                    self.usage = visual.transport.Usage()

                def call(self, prompt, images):
                    self.usage.calls += 1
                    self.last_prompt = prompt
                    return next(responses)

            def prepare(_video, cache, _interval):
                _, _, _, frames = fixture(cache)
                return {"frames": list(frames.values())}

            with (
                patch.object(visual.prepare_video, "prepare", side_effect=prepare),
                patch.object(visual.transport, "ffprobe_duration", return_value=15),
                patch.object(
                    visual.transport,
                    "resolve_transcript",
                    return_value=visual.transport.TranscriptResult(
                        "unavailable", "none", warning="No transcript supplied"
                    ),
                ),
                patch.object(visual.transport, "CodexCliModelClient", Client),
                patch.object(visual.transport, "ModelClient", Client),
                patch.object(
                    visual.transport, "read_secret", return_value="fixture-key"
                ),
                patch.object(visual.transport, "extract_tutorial") as old_extractor,
            ):
                manifest = visual.extract_visual_tutorial(
                    video_file=video,
                    video_url=None,
                    title="完整几何流程",
                    output_dir=root / "out",
                    profile_name="balanced",
                    model=model,
                    transcript=None,
                    render_html_enabled=False,
                    provider=provider,
                    endpoint="https://provider.invalid/v1/chat/completions",
                    secret_file=root / "fixture.key",
                    workspace_mode=True,
                    max_calls=5,
                    cache_dir=root / "cache",
                )
                rerun = visual.extract_visual_tutorial(
                    video_file=video,
                    video_url=None,
                    title="完整几何流程",
                    output_dir=root / "out",
                    profile_name="balanced",
                    model=model,
                    transcript=None,
                    render_html_enabled=False,
                    provider=provider,
                    endpoint="https://provider.invalid/v1/chat/completions",
                    secret_file=root / "fixture.key",
                    workspace_mode=True,
                    max_calls=5,
                    cache_dir=root / "cache",
                    replace_existing=True,
                )
            old_extractor.assert_not_called()
            self.assertEqual(visual.SCHEMA, manifest["schema"])
            self.assertEqual(model, manifest["model"])
            self.assertEqual(4, manifest["model_usage"]["calls"])
            self.assertEqual(0, rerun["model_usage"]["calls"])
            self.assertEqual(4, rerun["model_usage"]["cache_hits"])
            self.assertEqual([], visual.validate_workspace(root / "out"))
            self.assertEqual([], legacy_check.validate_package(root / "out"))
            self.assertFalse(list((root / "out").rglob("*.mp4")))
            self.assertFalse(list((root / "out").rglob("*.blend")))


if __name__ == "__main__":
    unittest.main()
