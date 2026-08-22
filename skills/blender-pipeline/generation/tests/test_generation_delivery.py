from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_video_replay_main as replay_main
import run_video_strict_replay as strict_replay
from video_replay_delivery_contract import (
    CANONICAL_SIX_VIEW_NAMES,
    complete_six_view_delivery,
)


def write_test_png(path: Path, color: tuple[int, int, int] = (90, 130, 180)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, "PNG")


class _TrajectoryStub:
    def activate_environment(self) -> None:
        return None

    def store_path(self, *args, **kwargs) -> list[dict]:
        return []

    def reference_path(self, *args, **kwargs) -> list[dict]:
        return []

    def store_bytes(self, *args, **kwargs) -> dict:
        return {}

    def append_event(self, *args, **kwargs) -> None:
        return None

    def record_tool_call(self, *args, **kwargs) -> str:
        return "call"

    def record_tool_result(self, *args, **kwargs) -> None:
        return None


class GenerationDeliveryTests(unittest.TestCase):
    def test_visual_review_cannot_be_disabled_for_a_completed_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory)
            with (
                patch.object(strict_replay, "OUT_NAME", "delivery"),
                patch.object(strict_replay, "apply_subject_render_defaults"),
                patch.dict(
                    os.environ,
                    {"BLENDER_PIPELINE_VISUAL_REVIEW": "0"},
                    clear=False,
                ),
            ):
                result = strict_replay.process(video_dir)
            self.assertEqual(result["status"], "visual_review_required")
            self.assertFalse(
                (video_dir / "delivery/delivery_validation_receipt.json").exists()
            )

    def test_strict_replay_command_forces_factory_startup(self) -> None:
        command = strict_replay.blender_replay_command(Path("reproduce.py"))
        self.assertIn("--factory-startup", command)
        self.assertLess(command.index("--factory-startup"), command.index("--python"))

    def test_static_acceptance_requires_all_named_png_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            delivery = Path(directory)
            write_test_png(delivery / "six_views/iso.png")
            self.assertFalse(complete_six_view_delivery(delivery))
            self.assertFalse(replay_main.static_preview_available(delivery))

            for name in CANONICAL_SIX_VIEW_NAMES:
                write_test_png(delivery / "six_views" / f"{name}.png")
            self.assertTrue(complete_six_view_delivery(delivery))
            self.assertTrue(replay_main.static_preview_available(delivery))

            (delivery / "six_views/top.png").write_bytes(b"not-a-png")
            self.assertFalse(complete_six_view_delivery(delivery))

    def test_static_visual_review_fails_closed_without_any_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory)
            out_dir = video_dir / "delivery"
            out_dir.mkdir()
            (video_dir / "motion_plan.json").write_text(
                json.dumps({"motion_type": "static"}), encoding="utf-8"
            )
            write_test_png(out_dir / "render.png")
            with patch.object(strict_replay, "call_chat_completions") as api:
                passed, review = strict_replay.review_static_render(
                    video_dir, out_dir, 0
                )
            self.assertFalse(passed)
            self.assertIn("abstained_no_visual_baseline", review)
            api.assert_not_called()

    def test_static_visual_review_uses_manifest_ordered_tutorial_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory)
            out_dir = video_dir / "delivery"
            out_dir.mkdir()
            evidence_dir = video_dir / "rich_evidence/windows"
            first = evidence_dir / "first.png"
            second = evidence_dir / "second.png"
            write_test_png(first, color=(30, 80, 120))
            write_test_png(second, color=(140, 100, 50))
            (video_dir / "rich_evidence/windows.json").write_text(
                json.dumps(
                    [
                        {
                            "start_sec": 0,
                            "end_sec": 10,
                            "sheet": "rich_evidence/windows/first.png",
                        },
                        {
                            "start_sec": 10,
                            "end_sec": 20,
                            "sheet": "rich_evidence/windows/second.png",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            (video_dir / "tutorial_path_refs.md").write_text(
                "![first](rich_evidence/windows/first.png)\n"
                "![second](rich_evidence/windows/second.png)\n",
                encoding="utf-8",
            )
            (video_dir / "motion_plan.json").write_text(
                json.dumps({"motion_type": "static"}), encoding="utf-8"
            )
            (video_dir / "material_spec.json").write_text("{}", encoding="utf-8")
            (video_dir / "target_reference.png").write_bytes(b"corrupt-image")
            (video_dir / "final_reference_status.json").write_text(
                json.dumps(
                    {
                        "reference_path": str(video_dir / "target_reference.png"),
                        "valid": True,
                        "use_in_prompt": True,
                        "priority": "auxiliary_only",
                        "conflict_policy": "tutorial_and_steps_win",
                    }
                ),
                encoding="utf-8",
            )
            write_test_png(out_dir / "render.png")
            captured: dict = {}

            def checkpoint(**kwargs):
                captured.update(kwargs["semantic_input"])
                return (
                    b'{"pass": true, "critical_issues": [], "repair_instruction": ""}'
                )

            with patch.object(
                strict_replay, "load_stage_checkpoint", side_effect=checkpoint
            ):
                passed, _review = strict_replay.review_static_render(
                    video_dir, out_dir, 0
                )
            self.assertTrue(passed)
            self.assertEqual(
                [item["path"] for item in captured["ordered_visual_baseline"]],
                [
                    "rich_evidence/windows/first.png",
                    "rich_evidence/windows/second.png",
                ],
            )

    def test_rw1_static_release_renders_complete_six_views(self) -> None:
        blender = (
            os.environ.get("BLENDER_BIN")
            or shutil.which("blender")
            or "/Applications/Blender.app/Contents/MacOS/Blender"
        )
        if not Path(blender).is_file():
            self.skipTest("Blender executable is unavailable")

        renderer = SCRIPTS / "render_asset_six_views_turntable.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "source.blend"
            out_dir = root / "delivery"
            create_scene = "\n".join(
                (
                    "import bpy",
                    "from mathutils import Vector",
                    "bpy.ops.object.select_all(action='SELECT')",
                    "bpy.ops.object.delete(use_global=False)",
                    "bpy.ops.mesh.primitive_cube_add()",
                    "bpy.context.active_object.name = 'Subject'",
                    "bpy.ops.object.camera_add(location=(4.0, -4.0, 3.0))",
                    "camera = bpy.context.active_object",
                    "camera.data.type = 'ORTHO'",
                    "camera.data.ortho_scale = 3.2",
                    "camera.rotation_euler = ((Vector((0, 0, 0)) - camera.location).to_track_quat('-Z', 'Y').to_euler())",
                    "bpy.context.scene.camera = camera",
                    "bpy.ops.object.light_add(type='AREA', location=(2.0, -2.0, 4.0))",
                    "bpy.context.active_object.data.energy = 900.0",
                    "bpy.context.active_object.data.shape = 'DISK'",
                    "bpy.context.active_object.data.size = 4.0",
                    "bpy.context.scene.render.engine = 'BLENDER_EEVEE'",
                    f"bpy.ops.wm.save_as_mainfile(filepath={str(blend)!r})",
                )
            )
            subprocess.run(
                [blender, "--background", "--python-expr", create_scene],
                check=True,
                capture_output=True,
                text=True,
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "VIDEO2BLENDER_PRESERVE_SOURCE_RENDER_SETTINGS": "1",
                    "VIDEO2BLENDER_POSTPROCESS_ENGINE": "BLENDER_EEVEE",
                    "TOTAL_ASSET_EXPECTED_GPU_UUID": (
                        "GPU-00000000-0000-0000-0000-000000000001"
                    ),
                    "VIDEO2BLENDER_GPU_PROCESS_ATTESTED": "1",
                    "VIDEO2BLENDER_GPU_PROCESS_ATTESTED_UUID": (
                        "gpu-00000000-0000-0000-0000-000000000001"
                    ),
                }
            )
            completed = subprocess.run(
                [
                    blender,
                    str(blend),
                    "--background",
                    "--python",
                    str(renderer),
                    "--",
                    "--out-dir",
                    str(out_dir),
                    "--rw1-source-contract",
                    "--use-authored-static-camera",
                    "--view-resolution",
                    "32",
                    "--samples",
                    "1",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            self.assertTrue(
                complete_six_view_delivery(out_dir),
                completed.stdout
                + completed.stderr
                + "\nfiles="
                + repr(
                    sorted(
                        str(path.relative_to(out_dir)) for path in out_dir.rglob("*")
                    )
                ),
            )
            presentation = json.loads(
                (out_dir / "presentation_camera.json").read_text(encoding="utf-8")
            )
            self.assertEqual(presentation["mode"], "authored_source_camera")
            shutil.copy2(blend, out_dir / "asset.blend")
            (root / "motion_plan.json").write_text(
                json.dumps({"motion_type": "static"}), encoding="utf-8"
            )
            (out_dir / "effective_route.txt").write_text("static\n", encoding="utf-8")
            with patch.object(strict_replay, "BLENDER", Path(blender)):
                strict_replay.write_delivery_receipt(root, out_dir)
            self.assertTrue(strict_replay.existing_delivery_complete(root, out_dir))
            with (
                patch.object(strict_replay, "OUT_NAME", "delivery"),
                patch.object(strict_replay, "QUALITY_PROFILE", "draft"),
                patch.object(strict_replay, "apply_subject_render_defaults"),
                patch.dict(os.environ, {"BLENDER_PIPELINE_FORCE_REPLAY": "0"}),
            ):
                self.assertEqual(
                    strict_replay.process(root)["status"], "skipped_existing"
                )

            receipt = out_dir / "delivery_validation_receipt.json"
            original_receipt = receipt.read_bytes()
            receipt_data = json.loads(original_receipt)
            receipt_data["effective_route"] = "dynamic"
            receipt.write_text(json.dumps(receipt_data), encoding="utf-8")
            self.assertFalse(strict_replay.existing_delivery_complete(root, out_dir))
            receipt.write_bytes(original_receipt)

            (out_dir / "effective_route.txt").write_text("dynamic\n", encoding="utf-8")
            self.assertFalse(strict_replay.existing_delivery_complete(root, out_dir))
            (out_dir / "effective_route.txt").write_text("static\n", encoding="utf-8")

            original_asset = (out_dir / "asset.blend").read_bytes()
            (out_dir / "asset.blend").write_bytes(original_asset + b"changed")
            self.assertFalse(strict_replay.existing_delivery_complete(root, out_dir))
            (out_dir / "asset.blend").write_bytes(original_asset)
            self.assertTrue(strict_replay.existing_delivery_complete(root, out_dir))

            write_test_png(out_dir / "six_views/top.png", color=(180, 60, 40))
            self.assertFalse(strict_replay.existing_delivery_complete(root, out_dir))

    def test_draft_render_receipt_rejects_incomplete_six_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_test_png(out_dir / "six_views/iso.png")
            with patch.object(strict_replay, "QUALITY_PROFILE", "draft"):
                strict_replay.write_render_time_review(
                    out_dir,
                    {
                        "TURNTABLE_SAMPLES": "16",
                        "TURNTABLE_SECONDS": "5",
                        "TURNTABLE_FPS": "24",
                    },
                    returncode=0,
                    measured_seconds=1.0,
                )
            receipt = json.loads(
                (out_dir / "render_time_review.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["status"], "needs_fix")
            self.assertEqual(
                receipt["missing_canonical_six_views"],
                ["front", "back", "left", "right", "top"],
            )

    def test_dynamic_visual_review_runs_after_fresh_postprocess_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory)
            (video_dir / "source.info.json").write_text(
                json.dumps({"source_kind": "video_replay"}), encoding="utf-8"
            )
            (video_dir / "motion_plan.json").write_text(
                json.dumps({"motion_type": "dynamic"}), encoding="utf-8"
            )
            out_dir = video_dir / "delivery"
            out_dir.mkdir()
            (out_dir / "final_effect.mp4").write_bytes(b"stale")
            events: list[str] = []

            def write_script(directory: Path, generated: str) -> Path:
                script = directory / "reproduce.py"
                script.write_text(generated, encoding="utf-8")
                return script

            def run_blender(script: Path, log_path: Path) -> int:
                log_path.write_text("fresh Blender run", encoding="utf-8")
                (script.parent / "asset.blend").write_bytes(
                    b"model-stage output is mocked in this ordering-only test"
                )
                write_test_png(script.parent / "render.png")
                return 0

            def run_postprocess(directory: Path) -> int:
                self.assertFalse((directory / "final_effect.mp4").exists())
                events.append("postprocess")
                (directory / "postprocess_views_turntable.log").write_text(
                    "fresh postprocess", encoding="utf-8"
                )
                (directory / "effective_route.txt").write_text(
                    "dynamic\n", encoding="utf-8"
                )
                (directory / "final_effect.mp4").write_bytes(b"fresh")
                for name in CANONICAL_SIX_VIEW_NAMES:
                    write_test_png(directory / "six_views" / f"{name}.png")
                return 0

            def review_delivery(
                supplied_video_dir: Path,
                directory: Path,
                attempt: int,
                *,
                dynamic_override: bool | None = None,
            ) -> tuple[bool, str]:
                self.assertEqual(supplied_video_dir, video_dir)
                self.assertTrue(dynamic_override)
                self.assertEqual(
                    (directory / "final_effect.mp4").read_bytes(), b"fresh"
                )
                events.append("review")
                (directory / f"visual_review_attempt_{attempt}.txt").write_text(
                    "pass", encoding="utf-8"
                )
                return True, "pass"

            trajectory = _TrajectoryStub()
            environment = {
                "BLENDER_PIPELINE_REPAIR_ATTEMPTS": "0",
                "BLENDER_PIPELINE_VISUAL_REVIEW": "1",
            }
            with (
                patch.object(strict_replay, "OUT_NAME", "delivery"),
                patch.object(strict_replay, "apply_subject_render_defaults"),
                patch.object(strict_replay, "request_code", return_value="pass\n"),
                patch.object(
                    strict_replay,
                    "review_generated_material_code",
                    return_value=(True, ""),
                ),
                patch.object(strict_replay, "write_script", side_effect=write_script),
                patch.object(strict_replay, "run_blender", side_effect=run_blender),
                patch.object(
                    strict_replay,
                    "run_postprocess",
                    side_effect=run_postprocess,
                ),
                patch.object(
                    strict_replay,
                    "review_static_render",
                    side_effect=review_delivery,
                ),
                patch.object(
                    strict_replay,
                    "write_delivery_receipt",
                    side_effect=lambda *_args: events.append("receipt"),
                ),
                patch.object(
                    strict_replay.AgentTrajectory,
                    "from_environment",
                    return_value=trajectory,
                ),
                patch.dict(os.environ, environment, clear=False),
            ):
                result = strict_replay.process(video_dir)

            self.assertEqual(result["status"], "done")
            self.assertEqual(events, ["postprocess", "review", "receipt"])


if __name__ == "__main__":
    unittest.main()
