from __future__ import annotations

import json
import re
import subprocess
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_asset_showcase as showcase  # noqa: E402


def _glb_bytes(document: object, *, raw_json: bytes | None = None) -> bytes:
    encoded = raw_json
    if encoded is None:
        encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    chunk = struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunk)) + chunk


def _valid_document() -> dict[str, object]:
    return {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": []}],
    }


class GlbValidationTests(unittest.TestCase):
    def _write(self, root: Path, data: bytes, name: str = "model.glb") -> Path:
        path = root / name
        path.write_bytes(data)
        return path

    def test_synthetic_glb_v2_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), _glb_bytes(_valid_document()))
            facts = showcase.validate_glb(path)
        self.assertEqual(facts["scene_count"], 1)
        self.assertEqual(facts["node_count"], 1)
        self.assertEqual(facts["mesh_count"], 1)

    def test_bad_magic_is_rejected(self) -> None:
        data = bytearray(_glb_bytes(_valid_document()))
        data[:4] = b"nope"
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), bytes(data))
            with self.assertRaises(showcase.ShowcaseError):
                showcase.validate_glb(path)

    def test_bad_declared_length_is_rejected(self) -> None:
        data = bytearray(_glb_bytes(_valid_document()))
        struct.pack_into("<I", data, 8, len(data) + 4)
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), bytes(data))
            with self.assertRaises(showcase.ShowcaseError):
                showcase.validate_glb(path)

    def test_bad_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(
                Path(temporary),
                _glb_bytes({}, raw_json=b'{"asset":{"version":"2.0"},wat'),
            )
            with self.assertRaises(showcase.ShowcaseError):
                showcase.validate_glb(path)

    def test_document_without_mesh_is_rejected(self) -> None:
        document = _valid_document()
        document["meshes"] = []
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), _glb_bytes(document))
            with self.assertRaises(showcase.ShowcaseError):
                showcase.validate_glb(path)

    def test_external_resource_uri_is_rejected(self) -> None:
        document = _valid_document()
        document["images"] = [{"uri": "textures/albedo.png"}]
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), _glb_bytes(document))
            with self.assertRaises(showcase.ShowcaseError):
                showcase.validate_glb(path)


class CandidateRankingTests(unittest.TestCase):
    def _candidate(
        self,
        root: Path,
        asset_id: str,
        category: str,
        size: int,
        *,
        source_kind: str = "total_asset",
    ) -> showcase.Candidate:
        blend_path = root / f"{source_kind}-{asset_id}.blend"
        blend_path.write_bytes(b"x" * size)
        return showcase.Candidate(
            source_kind=source_kind,
            asset_id=asset_id,
            title=f"Asset {asset_id}",
            category=category,
            license_name="test-only",
            blend_path=blend_path,
            poster_path=root / "poster.jpg",
            output_dir=root,
            review_path=root / "review.json",
            source_label="测试来源",
        )

    def test_rank_candidates_deduplicates_and_round_robins_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = [
                self._candidate(root, "a1", "A", 1024),
                self._candidate(root, "a2", "A", 2048),
                self._candidate(root, "b1", "B", 1024),
                self._candidate(root, "b2", "B", 2048),
                self._candidate(root, "c1", "C", 1024),
            ]
            candidates.append(candidates[0])
            ranked = showcase._rank_candidates(candidates)

        self.assertEqual(len(ranked), 5)
        self.assertEqual(len({item.stable_id for item in ranked}), 5)
        self.assertEqual([item.category for item in ranked], ["A", "B", "C", "A", "B"])


class PageRenderingTests(unittest.TestCase):
    @staticmethod
    def _manifest(item_count: int = 50) -> dict[str, object]:
        items = []
        for index in range(item_count):
            source_kind = "total_asset" if index < item_count // 2 else "bili_linked_asset"
            items.append(
                {
                    "asset_id": f"asset-{index:03d}",
                    "identity": f"{source_kind}:asset-{index:03d}",
                    "source_kind": source_kind,
                    "source_label": "70k 资产库" if source_kind == "total_asset" else "Task 1 工程资产",
                    "title": f"合成资产 {index:03d}",
                    "category": f"类别 {index % 5}",
                    "license_status": "test-only",
                    "model": f"item-{index:03d}/model.glb",
                    "poster": f"item-{index:03d}/poster.jpg",
                    "artifact": f"item-{index:03d}/web_artifact.json",
                    "glb_size": 1024 + index,
                    "mesh_count": 1,
                    "material_count": 1,
                    "image_count": 1,
                    "model_sha256": f"{index:064x}",
                    "poster_sha256": f"{index + 1:064x}",
                }
            )
        return {
            "schema_version": 1,
            "generation": "showcase-v1-test",
            "created_at": "2026-07-17T00:00:00+0800",
            "title": "资产展示",
            "item_count": item_count,
            "source_counts": {
                "total_asset": item_count // 2,
                "bili_linked_asset": item_count - item_count // 2,
            },
            "total_glb_bytes": sum(item["glb_size"] for item in items),
            "viewer": "model-viewer-local",
            "export_profile": "static_pbr_glb_v1",
            "items": items,
        }

    def test_render_page_embeds_50_items_and_one_lazy_viewer(self) -> None:
        page = showcase.render_page(self._manifest())
        self.assertEqual(page.count("<model-viewer"), 1)
        self.assertNotIn(".mp4", page.lower())
        self.assertNotIn("model_gallery_media/", page)
        self.assertNotIn('href="asset_gallery_model_full.html"', page)
        self.assertNotIn("/Volumes/", page)
        self.assertNotIn("/Users/", page)

        match = re.search(
            r'<script id="showcase-data" type="application/json">(.*?)</script>',
            page,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["item_count"], 50)
        self.assertEqual(len(embedded["items"]), 50)
        self.assertEqual(len({item["identity"] for item in embedded["items"]}), 50)

    def test_json_has_private_path_recurses(self) -> None:
        unsafe_values = (
            {"path": "/Volumes/Private/model.blend"},
            {"nested": [{"path": "/Users/example/asset.glb"}]},
            ["file:///tmp/model.glb"],
            {"model": "safe/../private/model.glb"},
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                self.assertTrue(showcase._json_has_private_path(value))
        self.assertFalse(
            showcase._json_has_private_path(
                {
                    "model": "total_asset-a/model.glb",
                    "poster": "total_asset-a/poster.jpg",
                    "site": "https://example.test/assets/asset.glb",
                }
            )
        )

    def test_site_navigation_is_idempotent_in_an_isolated_html_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            html_root = Path(temporary)
            model_page = html_root / "asset_gallery_model_full.html"
            video_page = html_root / "asset_gallery_video_full.html"
            model_page.write_text(
                '<!doctype html><div class="filters"><span>filter</span></div>',
                encoding="utf-8",
            )
            video_page.write_text(
                '<!doctype html><nav class="topnav"><a href="home.html">home</a></nav>',
                encoding="utf-8",
            )

            with mock.patch.object(showcase, "HTML_ROOT", html_root):
                showcase.ensure_site_navigation()
                first_model = model_page.read_text(encoding="utf-8")
                first_video = video_page.read_text(encoding="utf-8")
                showcase.ensure_site_navigation()

            self.assertEqual(model_page.read_text(encoding="utf-8"), first_model)
            self.assertEqual(video_page.read_text(encoding="utf-8"), first_video)
            self.assertEqual(first_model.count('href="asset_showcase.html"'), 1)
            self.assertEqual(first_video.count('href="asset_showcase.html"'), 1)
            self.assertIn(">资产展示</a>", first_model)
            self.assertIn(">资产展示</a>", first_video)

    def test_preview_launcher_is_installed_and_server_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "start_preview.command"
            with mock.patch.object(showcase, "PREVIEW_LAUNCHER", launcher):
                showcase.install_preview_launcher()
                first = launcher.read_text(encoding="utf-8")
                showcase.install_preview_launcher()

            self.assertEqual(launcher.read_text(encoding="utf-8"), first)
            self.assertNotEqual(launcher.stat().st_mode & 0o111, 0)
            self.assertIn('--bind "$BIND_HOST" --directory "$ROOT"', first)
            self.assertIn("ssh -N -L", first)
            self.assertIn("SO_REUSEADDR", first)
            self.assertIn('PAGE="${PAGE:-asset_gallery_video_full.html}"', first)
            self.assertNotIn("while lsof", first)
            subprocess.run(["bash", "-n", str(launcher)], check=True)


if __name__ == "__main__":
    unittest.main()
