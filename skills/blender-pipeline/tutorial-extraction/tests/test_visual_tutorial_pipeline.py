from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
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
                patch.object(visual.transport, "extract_tutorial") as old_extractor,
            ):
                manifest = visual.extract_visual_tutorial(
                    video_file=video,
                    video_url=None,
                    title="完整几何流程",
                    output_dir=root / "out",
                    profile_name="balanced",
                    model="gpt-5.6-sol",
                    transcript=None,
                    render_html_enabled=False,
                    provider="codex-cli",
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
                    model="gpt-5.6-sol",
                    transcript=None,
                    render_html_enabled=False,
                    provider="codex-cli",
                    workspace_mode=True,
                    max_calls=5,
                    cache_dir=root / "cache",
                    replace_existing=True,
                )
            old_extractor.assert_not_called()
            self.assertEqual(visual.SCHEMA, manifest["schema"])
            self.assertEqual(4, manifest["model_usage"]["calls"])
            self.assertEqual(0, rerun["model_usage"]["calls"])
            self.assertEqual(4, rerun["model_usage"]["cache_hits"])
            self.assertEqual([], visual.validate_workspace(root / "out"))
            self.assertFalse(list((root / "out").rglob("*.mp4")))
            self.assertFalse(list((root / "out").rglob("*.blend")))


if __name__ == "__main__":
    unittest.main()
