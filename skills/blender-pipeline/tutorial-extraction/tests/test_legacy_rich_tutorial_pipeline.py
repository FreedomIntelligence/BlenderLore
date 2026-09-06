from __future__ import annotations

import contextlib
import io
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
import legacy_rich_tutorial_pipeline as rich
import validate_tutorial_package
import visual_tutorial_pipeline as visual


def evidence_fixture(cache: Path, video: Path, source: dict, speech=None):
    windows, frames = [], []
    for index in range(2):
        relative = (
            f"rich_evidence/windows/w_{index * 60:05d}_{(index + 1) * 60:05d}.jpg"
        )
        path = cache / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 64), (index * 120, 40, 80)).save(path)
        windows.append(
            {
                "start_sec": index * 60.0,
                "end_sec": (index + 1) * 60.0,
                "sheet": relative,
                "asr_text": "选择对象并修改厚度",
                "ocr_samples": [],
            }
        )
        frames.append({"timestamp_sec": index * 60, "path": relative})
    rich.transport.atomic_json(cache / "rich_evidence" / "windows.json", windows)
    rich.transport.atomic_json(cache / "rich_evidence" / "frame_index.json", frames)
    rich.transport.atomic_json(
        cache / "source.info.json",
        {
            "title": source["title"],
            "webpage_url": source["url"],
            "source_video_path": str(video),
        },
    )
    return windows


def response_fixture(index: int):
    return {
        "steps": [
            {
                "time_range": f"{index:02d}:00-{index:02d}:50",
                "action": f"操作{index + 1}",
                "object": "Cube",
                "parameters": {"thickness": 0.25},
                "evidence": f"IMAGE {index * 60}s",
                "visual_result": "完整方体",
                "material_color": "红色粗糙表面",
                "spatial_relation": "一个主体",
                "surface_detail": "平面",
                "implementation_notes": "保留厚度",
            }
        ],
        "visual_contracts": {
            "visible_objects": ["Cube"],
            "materials": ["red"],
            "spatial_layout": ["one subject"],
            "do_not_omit": ["body"],
        },
        "uncertain_items": [],
    }


class LegacyRichTests(unittest.TestCase):
    def test_method_parser_default_and_explicit_rich_are_independent_from_provider(
        self,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "source.mp4"
            video.write_bytes(b"fixture")
            for method in ("visual", "legacy-rich"):
                for provider in ("api", "codex-cli"):
                    with self.subTest(method=method, provider=provider):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            cli.main(
                                [
                                    "--video-file",
                                    str(video),
                                    "--title",
                                    "Subject",
                                    "--output-dir",
                                    str(root / "out"),
                                    "--tutorial-method",
                                    method,
                                    "--provider",
                                    provider,
                                    "--dry-run",
                                ]
                            )
                        plan = json.loads(output.getvalue())
                        self.assertEqual(method, plan["tutorial_method"])
                        self.assertEqual(provider, plan["provider"])
                        self.assertEqual("gpt-5.6-sol", plan["model"])
            self.assertEqual(
                "visual",
                cli.build_parser()
                .parse_args(
                    [
                        "--video-file",
                        str(video),
                        "--title",
                        "Subject",
                        "--output-dir",
                        str(root),
                    ]
                )
                .tutorial_method,
            )

    def test_explicit_legacy_dispatch_preserves_existing_options(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "source.mp4"
            video.write_bytes(b"fixture")
            with (
                patch.object(
                    cli, "extract_legacy_rich_tutorial", return_value={}
                ) as legacy,
                patch.object(cli, "extract_visual_tutorial") as default,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cli.main(
                    [
                        "--video-file",
                        str(video),
                        "--title",
                        "Subject",
                        "--output-dir",
                        str(root / "out"),
                        "--tutorial-method",
                        "legacy-rich",
                        "--provider",
                        "codex-cli",
                        "--workspace-mode",
                        "--cache-dir",
                        str(root / "cache"),
                        "--max-calls",
                        "2",
                        "--input-asset",
                        str(root / "starter.blend"),
                    ]
                )
            default.assert_not_called()
            self.assertEqual("codex-cli", legacy.call_args.kwargs["provider"])
            self.assertEqual(2, legacy.call_args.kwargs["max_calls"])
            self.assertTrue(legacy.call_args.kwargs["workspace_mode"])
            self.assertEqual(
                [root / "starter.blend"], legacy.call_args.kwargs["input_assets"]
            )

    def test_complete_window_budget_never_silently_samples_gaps(self):
        self.assertEqual((11, 12), rich.complete_window_budget(600.1, None, None))
        self.assertEqual((1, 2), rich.complete_window_budget(60, 0, 2))
        with self.assertRaisesRegex(rich.Error, "all 11"):
            rich.complete_window_budget(601, 10, 99)
        with self.assertRaisesRegex(rich.Error, "max-calls >= 12"):
            rich.complete_window_budget(601, 0, 11)

    def test_input_dependency_tree_is_preserved_and_video_archives_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            asset = root / "textures"
            (asset / "maps").mkdir(parents=True)
            (asset / "maps" / "albedo.png").write_bytes(b"learner-texture")
            self.assertEqual(
                {"textures": asset.resolve()}, rich.collect_assets([asset], None)
            )
            (asset / "source.mp4").write_bytes(b"not-a-learner-input")
            with self.assertRaisesRegex(rich.Error, "source video"):
                rich.collect_assets([asset], None)

    def test_production_prompt_merge_embedding_and_both_transports_are_real_path(self):
        for provider in ("api", "codex-cli"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                video = root / "source.mp4"
                video.write_bytes(b"source-video-fixture")
                assets = root / "textures"
                (assets / "maps").mkdir(parents=True)
                (assets / "maps" / "color.png").write_bytes(b"learner-texture")
                prompts = []

                class Client:
                    def __init__(self, **kwargs):
                        self.model = kwargs["model"]
                        self.usage = rich.transport.Usage()

                    def call(self, prompt, images):
                        self.usage.calls += 1
                        prompts.append(prompt)
                        self.assert_images = all(path.is_file() for path in images)
                        return response_fixture(self.usage.calls - 1)

                args = dict(
                    video_file=video,
                    video_url=None,
                    title="完整教程",
                    output_dir=root / "out",
                    profile_name="balanced",
                    model="gpt-5.6-sol",
                    transcript=None,
                    render_html_enabled=False,
                    provider=provider,
                    workspace_mode=True,
                    max_calls=3,
                    cache_dir=root / "cache",
                    input_assets=[assets],
                    endpoint="https://provider.invalid/v1/chat/completions",
                    secret_file=root / "fixture.key",
                )
                with (
                    patch.object(
                        rich, "prepare_evidence", side_effect=evidence_fixture
                    ),
                    patch.object(rich.transport, "ffprobe_duration", return_value=120),
                    patch.object(
                        rich.transport,
                        "resolve_transcript",
                        return_value=rich.transport.TranscriptResult(
                            "unavailable", "none"
                        ),
                    ),
                    patch.object(
                        rich.transport, "read_secret", return_value="fixture-key"
                    ),
                    patch.object(rich.transport, "ModelClient", Client),
                    patch.object(rich.transport, "CodexCliModelClient", Client),
                    patch.object(rich.transport, "extract_tutorial") as fragment,
                    patch.object(visual, "extract_visual_tutorial") as visual_extract,
                ):
                    first = rich.extract_legacy_rich_tutorial(**args)
                    second = rich.extract_legacy_rich_tutorial(
                        **args, replace_existing=True
                    )
                fragment.assert_not_called()
                visual_extract.assert_not_called()
                self.assertEqual(2, len(prompts))
                production = rich.production_module("generate_rich_tutorial_chunks")
                self.assertIn(
                    production.build_prompt(
                        {"title": "完整教程", "webpage_url": ""},
                        {
                            "start_sec": 0.0,
                            "end_sec": 60.0,
                            "asr_text": "选择对象并修改厚度",
                            "ocr_samples": [],
                        },
                        0,
                    ),
                    prompts[0],
                )
                self.assertEqual(2, first["model_usage"]["calls"])
                self.assertEqual(0, second["model_usage"]["calls"])
                self.assertEqual(2, second["model_usage"]["cache_hits"])
                self.assertEqual(rich.SCHEMA, first["schema"])
                self.assertEqual(
                    rich.transport.sha256_path(video), first["source"]["sha256"]
                )
                self.assertEqual([], rich.validate_workspace(root / "out"))
                self.assertEqual(
                    [], validate_tutorial_package.validate_package(root / "out")
                )
                package = root / "out" / first["package"]
                self.assertEqual(
                    b"learner-texture",
                    (
                        package / "input" / "textures" / "maps" / "color.png"
                    ).read_bytes(),
                )
                embedded = (root / "out" / "tutorial.md").read_text(encoding="utf-8")
                refs = (root / "out" / "tutorial_path_refs.md").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(
                    rich.IMAGE_RE.sub("IMAGE", embedded),
                    rich.IMAGE_RE.sub("IMAGE", refs),
                )
                self.assertTrue(
                    all(
                        ref.startswith("data:image/")
                        for ref in rich.IMAGE_RE.findall(embedded)
                    )
                )
                self.assertTrue(
                    all(
                        (root / "out" / ref).is_file()
                        for ref in rich.IMAGE_RE.findall(refs)
                    )
                )
                self.assertIn("操作1", embedded)
                self.assertIn("操作2", embedded)
                self.assertIn("厚度", embedded)
                self.assertFalse(list(package.rglob("*.mp4")))
                self.assertFalse(list(package.rglob("*Rubric*")))
                self.assertNotIn(str(root / "cache"), refs)

    def test_low_budget_stops_before_evidence_or_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "source.mp4"
            video.write_bytes(b"fixture")
            with (
                patch.object(rich.transport, "ffprobe_duration", return_value=125),
                patch.object(rich, "prepare_evidence") as prepare,
                patch.object(rich.transport, "CodexCliModelClient") as client,
            ):
                with self.assertRaisesRegex(rich.Error, "max-calls >= 4"):
                    rich.extract_legacy_rich_tutorial(
                        video_file=video,
                        video_url=None,
                        title="Subject",
                        output_dir=root / "out",
                        profile_name="balanced",
                        model="gpt-5.6-sol",
                        transcript=None,
                        render_html_enabled=False,
                        provider="codex-cli",
                        max_calls=3,
                        cache_dir=root / "cache",
                    )
            prepare.assert_not_called()
            client.assert_not_called()

    def test_preparer_uses_production_recipe_and_reuses_its_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video, cache = root / "source.mp4", root / "cache"
            video.write_bytes(b"fixture")
            cache.mkdir()
            source = {
                "id": "source",
                "title": "Subject",
                "url": "",
                "duration_seconds": 120,
                "sha256": rich.transport.sha256_path(video),
            }
            speech = rich.transport.TranscriptResult("unavailable", "none")
            with patch.object(
                rich.transport,
                "run_command",
                side_effect=lambda *a, **k: evidence_fixture(cache, video, source),
            ) as run:
                self.assertEqual(
                    2, len(rich.prepare_evidence(cache, video, source, speech))
                )
                self.assertEqual(
                    2, len(rich.prepare_evidence(cache, video, source, speech))
                )
            self.assertEqual(1, run.call_count)
            command = run.call_args.args[0]
            self.assertEqual(
                str(rich.PRODUCTION / "prepare_rich_tutorial_evidence.py"), command[1]
            )
            self.assertEqual("0", command[command.index("--max-windows") + 1])
            self.assertEqual("5.0", command[command.index("--frame-step") + 1])
            self.assertEqual("60.0", command[command.index("--window-sec") + 1])
            self.assertEqual("", (cache / "segments.jsonl").read_text())

    def test_invalid_steps_receive_only_one_shared_format_repair(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            windows = evidence_fixture(
                cache, cache / "source.mp4", {"title": "Subject", "url": ""}
            )

            class Calls:
                responses = iter(
                    [
                        {"steps": "malformed"},
                        response_fixture(0),
                        {"steps": "malformed-again"},
                    ]
                )
                stages = []

                def call(self, stage, prompt, images):
                    self.stages.append(stage)
                    return next(self.responses)

            calls = Calls()
            with self.assertRaisesRegex(
                rich.Error, "single rich JSON repair allowance"
            ):
                rich.generate_chunks(cache, windows, calls)
            self.assertEqual(
                ["rich-window-000", "rich-format-repair-000", "rich-window-001"],
                calls.stages,
            )


if __name__ == "__main__":
    unittest.main()
