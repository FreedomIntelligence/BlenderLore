from __future__ import annotations

import io
import csv
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_pipeline as pipeline
from storage_capacity import DiskCapacity


class PipelineStatusTests(unittest.TestCase):
    def test_status_uses_expected_denominator_and_reports_all_terminal_states(self) -> None:
        rows = [
            {"asset_id": f"A{index:04d}", "render_batch": "0000"}
            for index in range(1000)
        ]
        statuses = {}
        boundaries = (
            (415, "accepted"),
            (765, "needs_review"),
            (901, "deferred"),
            (1000, "failed"),
        )
        start = 0
        for end, state in boundaries:
            for index in range(start, end):
                statuses[f"A{index:04d}"] = {"status": state}
            start = end
        capacity = DiskCapacity(16 * 1024**4, 8 * 1024**4, 8 * 1024**4, "test")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = list(root.iterdir())
            output = io.StringIO()
            with (
                mock.patch.object(pipeline, "read_csv", return_value=rows),
                mock.patch.object(pipeline, "load_render_status", return_value=statuses),
                mock.patch.object(pipeline, "get_disk_capacity", return_value=capacity),
                mock.patch.object(pipeline, "ROOT", root),
                mock.patch.object(pipeline, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(pipeline, "task1_state", return_value={}),
                mock.patch.dict(pipeline.os.environ, {"TOTAL_ASSET_BATCH_INDEX": "0"}),
                redirect_stdout(output),
            ):
                pipeline.status_report()
            payload = json.loads(output.getvalue())
            acceptance = payload["batch_acceptance"]
            self.assertEqual(
                {key: acceptance[key] for key in ("accepted", "needs_review", "deferred", "failed")},
                {"accepted": 415, "needs_review": 350, "deferred": 136, "failed": 99},
            )
            self.assertEqual(acceptance["terminal"], 1000)
            self.assertEqual(acceptance["pending"], 0)
            self.assertAlmostEqual(acceptance["technical_success_rate"], 0.765)
            self.assertAlmostEqual(acceptance["hard_failure_rate"], 0.099)
            self.assertFalse(acceptance["gate_passed"])
            self.assertEqual(list(root.iterdir()), before)

    def _release_fixture(self, root: Path) -> dict[str, Path]:
        inventory = root / "inventory"
        html_root = root / "html"
        inventory.mkdir()
        html_root.mkdir()
        catalog = inventory / "total_asset_catalog.csv"
        fields = (
            "asset_id",
            "identity_key",
            "model_file",
            "render_order",
            "render_batch",
            "inventory_status",
            "duplicate_of",
        )
        with catalog.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for index in range(1000):
                writer.writerow(
                    {
                        "asset_id": f"asset-{index:04d}",
                        "identity_key": f"identity-{index:04d}",
                        "model_file": f"/models/{index:04d}.blend",
                        "render_order": index + 1,
                        "render_batch": "0000",
                        "inventory_status": "ready",
                        "duplicate_of": "",
                    }
                )
        status = inventory / "total_asset_render_status_batch0000_quality_audit.jsonl"
        with status.open("w", encoding="utf-8") as handle:
            for index in range(1000):
                handle.write(
                    json.dumps(
                        {
                            "batch": "batch0000_quality_audit",
                            "asset_id": f"asset-{index:04d}",
                            "identity_key": f"identity-{index:04d}",
                            "render_order": str(index + 1),
                            "render_batch": "0000",
                            "status": "accepted" if index < 950 else "failed",
                            "updated_at": "2026-07-16T00:00:00+00:00",
                        }
                    )
                    + "\n"
                )
        from total_asset_scheduler import derive_plan

        plan = derive_plan(
            catalog,
            inventory,
            batch_size=1000,
            active_batches=set(),
            worker_claims=(),
        )
        batch = next(item for item in plan.batches if item.key == "batch0000")
        marker = inventory / "scheduler_events" / f"batch0000.audit.{batch.generation}.json"
        marker.parent.mkdir()
        marker.write_text(
            json.dumps(
                {
                    "event": "audit",
                    "batch": "batch0000",
                    "generation": batch.generation,
                    "completed_at": "2026-07-16 00:00:00",
                }
            ),
            encoding="utf-8",
        )
        media = html_root / "model_gallery_media" / "asset-0000" / "preview.jpg"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"jpeg-data")
        gallery = html_root / "asset_gallery_model_full.html"
        gallery.write_text(
            "<html>model_gallery_media/asset-0000/preview.jpg</html>",
            encoding="utf-8",
        )
        return {
            "inventory": inventory,
            "catalog": catalog,
            "html_root": html_root,
            "gallery": gallery,
            "media": media,
            "manifest": inventory / "model_gallery_publish_manifest.json",
        }

    def test_release_manifest_is_generation_bound_and_hash_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._release_fixture(Path(temporary))
            with (
                mock.patch.object(pipeline, "INVENTORY", paths["inventory"]),
                mock.patch.object(pipeline, "CATALOG", paths["catalog"]),
                mock.patch.object(pipeline, "HTML_ROOT", paths["html_root"]),
                mock.patch.object(pipeline, "GALLERY_HTML", paths["gallery"]),
                mock.patch.object(pipeline, "MODEL_RELEASE_MANIFEST", paths["manifest"]),
                mock.patch("total_asset_scheduler.detect_qa_activity", return_value=()),
                redirect_stdout(io.StringIO()),
            ):
                result = pipeline.build_model_release_manifest(0)
            persisted = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            self.assertEqual(result, persisted)
            self.assertEqual(persisted["terminal_count"], 1000)
            self.assertEqual(persisted["failed_count"], 50)
            files = {row["path"]: row for row in persisted["files"]}
            self.assertEqual(
                files["model_gallery_media/asset-0000/preview.jpg"]["sha256"],
                hashlib.sha256(paths["media"].read_bytes()).hexdigest(),
            )
            self.assertTrue(persisted["audit_generation"])

    def test_release_manifest_refuses_active_qa_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._release_fixture(Path(temporary))
            with (
                mock.patch.object(pipeline, "INVENTORY", paths["inventory"]),
                mock.patch.object(pipeline, "CATALOG", paths["catalog"]),
                mock.patch.object(pipeline, "HTML_ROOT", paths["html_root"]),
                mock.patch.object(pipeline, "GALLERY_HTML", paths["gallery"]),
                mock.patch.object(pipeline, "MODEL_RELEASE_MANIFEST", paths["manifest"]),
                mock.patch(
                    "total_asset_scheduler.detect_qa_activity",
                    return_value=("screen:batch0000:repair",),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    pipeline.build_model_release_manifest(0)
            self.assertFalse(paths["manifest"].exists())


if __name__ == "__main__":
    unittest.main()
