from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPRODUCTION_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_ROOT = REPRODUCTION_ROOT.parent
REPRODUCTION_SCRIPTS = REPRODUCTION_ROOT / "scripts"
GENERATION_SCRIPTS = PIPELINE_ROOT / "generation/scripts"
for path in (REPRODUCTION_SCRIPTS, GENERATION_SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import export_public_showcase_knowledge as exporter
import launch_video2blender_reproduction as launcher
import render_illustrated_tutorial as html_renderer
import validate_public_knowledge as public_validator


SOURCE_PACK = (
    Path(__file__).resolve().parents[5] / "video2blender/showcase_knowledge_pack"
)


class PublicKnowledgeTests(unittest.TestCase):
    def test_packaged_public_catalog_is_portable_and_fail_closed(self) -> None:
        self.assertEqual([], public_validator.validate(REPRODUCTION_ROOT / "knowledge"))

    @unittest.skipUnless(SOURCE_PACK.is_dir(), "private source catalog is not present")
    def test_exporter_reproduces_the_committed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog, inventory, attestations = exporter.export(
                SOURCE_PACK / "manifest.json",
                SOURCE_PACK / "public_showcase_inventory.json",
                SOURCE_PACK / "public_media_attestations.json",
            )
            values = {
                "manifest.json": catalog,
                "public_showcase_inventory.json": inventory,
                "public_media_attestations.json": attestations,
            }
            root = Path(temporary)
            for name, value in values.items():
                (root / name).write_text(
                    json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    (REPRODUCTION_ROOT / "knowledge" / name).read_bytes(),
                    (root / name).read_bytes(),
                )

    def test_current_public_recipe_stops_before_any_runtime_action(self) -> None:
        args = launcher.parse_args(["--target", "hero-01"])
        with self.assertRaisesRegex(launcher.LaunchError, "disabled"):
            launcher.build_plan(args)


class LauncherTutorialTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, str]:
        evidence = root / "evidence.txt"
        evidence.write_text("verified evidence", encoding="utf-8")
        tutorial = root / "tutorial.md"
        tutorial.write_text("# tutorial\n", encoding="utf-8")
        image = root / "reference.png"
        image.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2n7sAAAAASUVORK5CYII="
            )
        )
        asset = root / "source.blend"
        asset.write_bytes(b"authorized-test-asset")
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        catalog = {
            "schema": "test-catalog.v1",
            "coverage_status_values": ["verified-complete"],
            "recipes": {
                "test-replay": {
                    "id": "test-replay",
                    "title": "Test replay",
                    "category": "test",
                    "coverage_status": "verified-complete",
                    "enabled": True,
                    "mode": "deterministic-script",
                    "entrypoint": None,
                    "video_replay": {"enabled": True},
                    "inputs": {"required": ["asset", "tutorial"]},
                    "dynamic_contract": {},
                    "evidence_paths": ["evidence.txt"],
                    "gaps": [],
                    "adapter_contract": {"visual_equivalence_verified": True},
                    "distribution_contract": {
                        "schema_version": "1.0",
                        "policy_mode": "hybrid",
                        "code_distribution": "open-source",
                        "asset_delivery": "user-supplied",
                        "requires_user_asset": True,
                        "auto_download_allowed": False,
                        "source_license_status": "unknown",
                        "open_source_contents": [
                            "recipe",
                            "adapter",
                            "parameters",
                            "verification-contract",
                        ],
                        "asset_provenance": {
                            "evidence_status": "user-attested",
                            "license_id": None,
                            "evidence_refs": [],
                        },
                        "user_asset_verification": {
                            "accepted_sha256": [digest],
                            "semantic_validation_required": True,
                        },
                        "bundled_asset_relative_path": None,
                        "rationale": "The caller supplies the authorized local asset.",
                    },
                }
            },
        }
        (root / "manifest.json").write_text(json.dumps(catalog), encoding="utf-8")
        return tutorial, image, asset, digest

    def test_extract_mode_is_explicit_bounded_and_keeps_html_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _tutorial, image, asset, _digest = self._fixture(root)
            args = launcher.parse_args(
                [
                    "--target",
                    "test-replay",
                    "--knowledge-root",
                    str(root),
                    "--asset",
                    str(asset),
                    "--video-url",
                    "https://www.bilibili.com/video/BV1test/",
                    "--image",
                    str(image),
                    "--tutorial-mode",
                    "extract",
                    "--tutorial-window-budget",
                    "4",
                    "--render-tutorial-html",
                ]
            )
            plan = launcher.build_plan(args)
            command = launcher.command_for(
                plan, Path(plan["run_dir"]) / "run_manifest.json"
            )
            self.assertIn("--force-tutorial", command)
            self.assertIn("--max-windows", command)
            self.assertIn("--render-tutorial-html", command)
            self.assertEqual("gpt-5.6-sol", plan["tutorial_extraction"]["model"])
            self.assertFalse((Path(plan["run_dir"])).exists())

    def test_provided_tutorial_does_not_enable_html_or_paid_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tutorial, _image, asset, _digest = self._fixture(root)
            args = launcher.parse_args(
                [
                    "--target",
                    "test-replay",
                    "--knowledge-root",
                    str(root),
                    "--asset",
                    str(asset),
                    "--tutorial",
                    str(tutorial),
                ]
            )
            plan = launcher.build_plan(args)
            self.assertEqual("provided", plan["tutorial_extraction"]["mode"])
            self.assertNotIn(
                "--force-tutorial",
                launcher.command_for(plan, Path(plan["run_dir"]) / "run_manifest.json"),
            )


class OptionalHtmlTests(unittest.TestCase):
    def test_html_is_derived_from_verified_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "rich_evidence/windows").mkdir(parents=True)
            image = root / "rich_evidence/windows/w_00000_00060.png"
            image.write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2n7sAAAAASUVORK5CYII="
                )
            )
            (root / "source.info.json").write_text(
                json.dumps(
                    {"title": "示例", "webpage_url": "https://example.com/video"}
                ),
                encoding="utf-8",
            )
            (root / "rich_evidence/windows.json").write_text(
                json.dumps([{"start_sec": 0, "end_sec": 60, "sheet": str(image)}]),
                encoding="utf-8",
            )
            (root / "steps_verified.json").write_text(
                json.dumps(
                    {
                        "steps": [
                            {
                                "window_index": 0,
                                "time_range": "00:10-00:20",
                                "action": "添加材质",
                                "object": "主体",
                                "visual_result": "表面变为玻璃质感",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "tutorial.md").write_text("# operational truth\n", encoding="utf-8")
            receipt = html_renderer.render(root)
            body = (root / "illustrated_tutorial.html").read_text(encoding="utf-8")
            self.assertIn("tutorial.md", body)
            self.assertIn("添加材质", body)
            self.assertEqual("tutorial.md", receipt["operational_source"])


class DirectGenerationPreflightTests(unittest.TestCase):
    def test_force_tutorial_rejects_zero_budget_before_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "not-created"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATION_SCRIPTS / "run_video_replay_main.py"),
                    "--video-dir",
                    str(missing),
                    "--force-tutorial",
                    "--max-windows",
                    "0",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("positive --max-windows", completed.stderr)
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
