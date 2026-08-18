from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_batch_failed_repair_manifests as manifests  # noqa: E402
import total_asset_frozen_repair_adapter as adapter  # noqa: E402


CATALOG_FIELDS = (
    "asset_id",
    "identity_key",
    "model_file",
    "source_root",
    "render_order",
    "render_batch",
    "source",
    "title",
    "render_route",
    "render_engine_hint",
)


class FrozenRepairAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.models = self.root / "models"
        self.output = self.inventory / "repair_manifests"
        self.inventory.mkdir()
        self.models.mkdir()
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.status = self.inventory / "total_asset_render_status_fixture.jsonl"
        self.catalog_rows: list[dict[str, str]] = []
        self.status_rows: list[dict[str, object]] = []
        self.expected: dict[str, list[str]] = {
            "batch0000": [],
            "batch0001": [],
        }

        self.add_asset(
            "batch0000",
            "0000-static-legacy",
            ".blend",
            b"BLENDER-v360",
            {
                "issues": [
                    "missing six_views/front.png",
                    "dynamic downgraded to static after visible-motion gate",
                ]
            },
        )
        self.add_asset(
            "batch0000",
            "0000-static-modern",
            ".blend",
            b"BLENDER-v450",
            {
                "issues": [
                    "missing six_views/front.png",
                    "dynamic downgraded to static after visible-motion gate",
                ]
            },
        )
        self.add_asset(
            "batch0000",
            "0000-runtime-modern",
            ".fbx",
            b"Kaydara FBX Binary",
            {
                "failure_category": "blender_version_or_api",
                "error": "CyclesLightSettings.cast_shadow AttributeError",
            },
        )
        self.add_asset(
            "batch0001",
            "0001-static-modern",
            ".blend",
            b"BLENDER-v450",
            {
                "issues": [
                    "missing six_views/front.png",
                    "dynamic downgraded to static after visible-motion gate",
                ]
            },
        )
        self.add_asset(
            "batch0001",
            "0001-transport-legacy",
            ".blend",
            b"BLENDER-v420",
            {
                "failure_category": "render_timeout",
                "error": "worker_ssh_timeout",
            },
        )
        self.write_catalog()
        self.write_status()
        self.bundle = manifests.derive_bundle(
            catalog_path=self.catalog,
            inventory_dir=self.inventory,
            expected_asset_ids=self.expected,
        )
        manifests.write_bundle(self.bundle, self.output)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_asset(
        self,
        batch: str,
        asset_id: str,
        suffix: str,
        header: bytes,
        diagnostic: dict[str, object],
    ) -> None:
        render_batch = manifests.BATCH_RENDER_KEYS[batch]
        ordinal = len(self.catalog_rows) + 1
        model = self.models / f"{asset_id}{suffix}"
        model.write_bytes(header + b"x" * 2048)
        row = {
            "asset_id": asset_id,
            "identity_key": f"identity-{asset_id}",
            "model_file": str(model),
            "source_root": str(self.models),
            "render_order": str(ordinal),
            "render_batch": render_batch,
            "source": "fixture",
            "title": asset_id,
            "render_route": "dynamic_candidate",
            "render_engine_hint": "source",
        }
        self.catalog_rows.append(row)
        self.status_rows.append({
            "asset_id": asset_id,
            "identity_key": row["identity_key"],
            "render_order": row["render_order"],
            "render_batch": render_batch,
            "batch": batch,
            "status": "failed",
            "worker": "fixture",
            "updated_at": f"2026-07-19 12:{ordinal:02d}:00",
            **diagnostic,
        })
        self.expected[batch].append(asset_id)

    def write_catalog(self) -> None:
        with self.catalog.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
            writer.writeheader()
            writer.writerows(self.catalog_rows)

    def write_status(self) -> None:
        with self.status.open("w", encoding="utf-8") as handle:
            for row in self.status_rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    def append_status(self, row: dict[str, object]) -> None:
        with self.status.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def inspect(
        self,
        batch: str = "batch0000",
        group: str = manifests.STATIC_FALLBACK,
    ) -> adapter.FrozenRepairPlan:
        return adapter.inspect_frozen_repair_group(
            batch=batch,
            group=group,
            catalog_path=self.catalog,
            inventory_dir=self.inventory,
            output_dir=self.output,
        )

    def test_supported_group_returns_pending_rows_partitioned_by_runtime_lane(
        self,
    ) -> None:
        plan = self.inspect()
        self.assertEqual(plan.pending_count, 2)
        self.assertEqual(
            [row["asset_id"] for row in plan.pending_rows_by_lane["legacy_gpu0"]],
            ["0000-static-legacy"],
        )
        self.assertEqual(
            [row["asset_id"] for row in plan.pending_rows_by_lane["modern_vulkan"]],
            ["0000-static-modern"],
        )
        summary = plan.summary(runtime_lane="modern_vulkan")
        self.assertEqual(summary["lane"], "modern_vulkan")
        self.assertEqual(summary["lane_pending"], 1)
        self.assertEqual(summary["other_lane_pending"], 1)
        for field in (
            "sidecar_hash",
            "queue_sha256",
            "identity_snapshot_hash",
            "source_snapshot_hash",
            "expected_status_snapshot_hash",
        ):
            self.assertRegex(summary[field], r"^[0-9a-f]{64}$")

    def test_exact_supported_matrix_and_automatic_policy_are_fail_closed(self) -> None:
        for batch, group in adapter.SUPPORTED_GROUPS:
            self.inspect(batch, group)
        with self.assertRaisesRegex(
            adapter.FrozenRepairAdapterError, "not enabled"
        ):
            self.inspect("batch0001", manifests.RUNTIME_API)

        sidecar_path = self.output / manifests.SIDECAR_FILE
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar["batches"]["batch0000"]["groups"][
            manifests.STATIC_FALLBACK
        ]["automatic_execution_allowed"] = False
        package = {key: value for key, value in sidecar.items() if key != "hash"}
        sidecar["hash"] = adapter._sha256(adapter._canonical_json_bytes(package))
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        with self.assertRaisesRegex(
            adapter.FrozenRepairAdapterError, "not reviewed"
        ):
            self.inspect()

    def test_sidecar_generation_and_csv_bytes_are_verified(self) -> None:
        sidecar = dict(self.bundle.sidecar)
        sidecar["generation"] = "batch0000-batch0001-failed-00000000000000000000"
        package = {key: value for key, value in sidecar.items() if key != "hash"}
        sidecar["hash"] = adapter._sha256(adapter._canonical_json_bytes(package))
        with self.assertRaisesRegex(
            adapter.FrozenRepairAdapterError, "generation mismatch"
        ):
            adapter._validate_sidecar_generation(sidecar)

        queue = self.output / manifests.GROUP_FILES[
            ("batch0000", manifests.STATIC_FALLBACK)
        ]
        with queue.open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaisesRegex(
            adapter.FrozenRepairAdapterError, "CSV hash mismatch"
        ):
            self.inspect()

    def test_source_status_and_identity_drift_are_blocked(self) -> None:
        model = self.models / "0000-static-modern.blend"
        with model.open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaisesRegex(
            adapter.FrozenRepairAdapterError, "repair source differs"
        ):
            self.inspect()

        model.write_bytes(b"BLENDER-v450" + b"x" * 2048)
        # Restore the committed source timestamp as well as its bytes.
        row = self.bundle.rows_by_batch_group[
            ("batch0000", manifests.STATIC_FALLBACK)
        ][1]
        model.touch()
        import os
        os.utime(model, ns=(int(row["source_mtime_ns"]), int(row["source_mtime_ns"])))
        drifted = dict(self.status_rows[0])
        drifted["error"] = "new unreviewed failure"
        drifted["updated_at"] = "2026-07-19 23:59:59"
        self.append_status(drifted)
        with self.assertRaisesRegex(
            adapter.FrozenRepairAdapterError, "current state is unsafe"
        ):
            self.inspect()

        self.write_status()
        self.catalog_rows[0]["identity_key"] = "changed-identity"
        self.write_catalog()
        with self.assertRaisesRegex(
            adapter.FrozenRepairAdapterError,
            "no current catalog/status identity|current identity_key differs",
        ):
            self.inspect()

    def test_landed_and_attempted_failed_rows_are_not_retried(self) -> None:
        landed = dict(self.status_rows[0])
        landed.update({
            "batch": "batch0000_quality_audit",
            "status": "accepted",
            "updated_at": "2026-07-19 23:59:58",
        })
        attempted = dict(self.status_rows[1])
        attempted.update({
            "batch": "batch0000_failed_repair_static_fallback_contract",
            "status": "failed",
            "updated_at": "2026-07-19 23:59:59",
        })
        self.append_status(landed)
        self.append_status(attempted)
        plan = self.inspect()
        self.assertEqual(plan.pending_count, 0)
        self.assertEqual(plan.landed_asset_ids, ("0000-static-legacy",))
        self.assertEqual(
            plan.attempted_failed_asset_ids,
            ("0000-static-modern",),
        )

    def test_cli_is_shell_safe_and_require_pending_returns_75(self) -> None:
        common = [
            "inspect",
            "--batch",
            "batch0000",
            "--group",
            manifests.STATIC_FALLBACK,
            "--inventory-dir",
            str(self.inventory),
            "--catalog",
            str(self.catalog),
            "--output-dir",
            str(self.output),
            "--runtime-lane",
            "modern_vulkan",
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(adapter.main(common), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["lane"], "modern_vulkan")
        self.assertEqual(payload["lane_pending"], 1)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = adapter.main(common[:-1] + ["legacy_gpu0", "--require-pending"])
        self.assertEqual(code, 0)

        empty_group = [
            "inspect",
            "--batch",
            "batch0001",
            "--group",
            manifests.STATIC_FALLBACK,
            "--inventory-dir",
            str(self.inventory),
            "--catalog",
            str(self.catalog),
            "--output-dir",
            str(self.output),
            "--runtime-lane",
            "legacy_gpu0",
            "--require-pending",
        ]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(adapter.main(empty_group), 75)
        self.assertIn("no pending rows", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
