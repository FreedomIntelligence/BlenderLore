from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_batch_failed_repair_manifests as manifests  # noqa: E402


CATALOG_FIELDS = [
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
]


class BatchFailedRepairManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.models = self.root / "models"
        self.output = self.root / "repair_manifests"
        self.inventory.mkdir()
        self.models.mkdir()
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.status = self.inventory / "total_asset_render_status_fixture.jsonl"
        self.expected: dict[str, tuple[str, ...]] = {}
        self.catalog_rows: list[dict[str, str]] = []
        self.status_rows: list[dict[str, object]] = []

        diagnostics = {
            manifests.STATIC_FALLBACK: {
                "issues": [
                    "missing six_views/front.png",
                    "dynamic downgraded to static after visible-motion gate",
                ],
            },
            manifests.RUNTIME_API: {
                "failure_category": "blender_version_or_api",
                "error": (
                    "AttributeError: CyclesLightSettings has no attribute "
                    "cast_shadow"
                ),
            },
            manifests.TIMEOUT_SCENE_AUDIT: {
                "failure_category": "render_timeout",
                "error": "render timeout after 7200s",
            },
            manifests.TRANSPORT_DIAGNOSTIC: {
                "failure_category": "render_timeout",
                "error": "worker_ssh_timeout",
            },
            manifests.DEPENDENCY_RELINK_REVIEW: {
                "failure_category": "missing_dependency",
                "error": "Unable to pack file, source path texture.dds not found",
            },
            manifests.SOURCE_CORRUPT_MANUAL: {
                "failure_category": "missing_dependency",
                "error": "No such file or directory; Missing DNA block",
            },
            manifests.NON_RENDERABLE_HELPER_REVIEW: {
                "failure_category": "quality_revalidation_failed",
                "error": "no renderable objects",
            },
            manifests.MANUAL_REVIEW: {
                "failure_category": "remote_render_failure",
                "error": " ",
            },
        }
        ordinal = 0
        for batch, render_batch in manifests.BATCH_RENDER_KEYS.items():
            asset_ids = []
            for group, diagnostic in diagnostics.items():
                ordinal += 1
                asset_id = f"{render_batch}-{ordinal:03d}"
                asset_ids.append(asset_id)
                suffix = ".fbx" if group == manifests.RUNTIME_API else ".blend"
                model = self.models / f"{asset_id}{suffix}"
                payload = (
                    b"Kaydara FBX Binary"
                    if suffix == ".fbx"
                    else b"BLENDER-v450"
                )
                model.write_bytes(payload + b"x" * 2048)
                order = str(1000 + ordinal)
                identity = f"identity-{asset_id}"
                self.catalog_rows.append({
                    "asset_id": asset_id,
                    "identity_key": identity,
                    "model_file": str(model),
                    "source_root": str(self.models),
                    "render_order": order,
                    "render_batch": render_batch,
                    "source": "fixture",
                    "title": asset_id,
                    "render_route": "dynamic_candidate",
                    "render_engine_hint": "source",
                })
                self.status_rows.append({
                    "asset_id": asset_id,
                    "identity_key": identity,
                    "render_order": order,
                    "render_batch": render_batch,
                    "batch": batch,
                    "status": "failed",
                    "worker": "fixture-worker",
                    "updated_at": f"2026-07-19 12:{ordinal:02d}:00",
                    **diagnostic,
                })
            self.expected[batch] = tuple(asset_ids)
        self.write_catalog()
        self.write_status()

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def derive(self) -> manifests.ManifestBundle:
        return manifests.derive_bundle(
            catalog_path=self.catalog,
            inventory_dir=self.inventory,
            expected_asset_ids=self.expected,
        )

    def test_live_frozen_contract_has_exact_requested_counts(self) -> None:
        self.assertEqual(manifests.EXPECTED_COUNTS, {
            "batch0000": 64,
            "batch0001": 26,
        })
        self.assertEqual(
            set(manifests.FROZEN_FAILED_ASSET_IDS),
            set(manifests.BATCH_RENDER_KEYS),
        )

    def test_builds_disjoint_root_cause_groups_with_safe_execution_policy(self) -> None:
        before_catalog = hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        before_status = hashlib.sha256(self.status.read_bytes()).hexdigest()
        bundle = self.derive()
        self.assertEqual(bundle.sidecar["count"], 16)
        self.assertRegex(
            bundle.sidecar["generation"],
            r"^batch0000-batch0001-failed-[0-9a-f]{20}$",
        )
        self.assertRegex(bundle.sidecar["snapshot_hash"], r"^[0-9a-f]{64}$")

        for batch in manifests.BATCH_RENDER_KEYS:
            observed: set[str] = set()
            for spec in manifests.GROUP_SPECS:
                rows = bundle.rows_by_batch_group[(batch, spec.name)]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertNotIn(row["asset_id"], observed)
                observed.add(row["asset_id"])
                self.assertEqual(row["repair_group"], spec.name)
                self.assertEqual(row["render_batch"], manifests.BATCH_RENDER_KEYS[batch])
                self.assertEqual(row["expected_status"], "failed")
                self.assertEqual(row["manifest_generation"], bundle.sidecar["generation"])
                for field in (
                    "expected_status_signature",
                    "diagnostic_signature",
                    "source_stat_signature",
                ):
                    self.assertRegex(row[field], r"^[0-9a-f]{64}$")
                self.assertEqual(
                    row["automatic_execution_allowed"],
                    "true" if spec.automatic_execution_allowed else "false",
                )
                self.assertEqual(
                    row["requires_human_review"],
                    "true" if spec.requires_human_review else "false",
                )
                if spec.name in {
                    manifests.SOURCE_CORRUPT_MANUAL,
                    manifests.NON_RENDERABLE_HELPER_REVIEW,
                    manifests.MANUAL_REVIEW,
                }:
                    self.assertEqual(row["automatic_execution_allowed"], "false")
                    self.assertEqual(row["max_attempts"], "0")
            self.assertEqual(observed, set(self.expected[batch]))

        manifests.write_bundle(bundle, self.output)
        self.assertEqual(manifests.load_committed_bundle(self.output), bundle.sidecar)
        self.assertEqual(
            manifests.validate_current_bundle(self.derive(), self.output),
            bundle.sidecar,
        )
        self.assertEqual(hashlib.sha256(self.catalog.read_bytes()).hexdigest(), before_catalog)
        self.assertEqual(hashlib.sha256(self.status.read_bytes()).hexdigest(), before_status)
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_classifier_precedence_never_makes_corruption_or_unknown_automatic(self) -> None:
        corrupt, _evidence = manifests.classify_repair_group({
            "failure_category": "missing_dependency",
            "error": "render timeout and Missing DNA block",
        })
        self.assertEqual(corrupt, manifests.SOURCE_CORRUPT_MANUAL)
        transport, _evidence = manifests.classify_repair_group({
            "failure_category": "render_timeout",
            "error": "worker_ssh_timeout",
        })
        self.assertEqual(transport, manifests.TRANSPORT_DIAGNOSTIC)
        missing_contract, _evidence = manifests.classify_repair_group({
            "failure_category": "missing_dependency",
            "error": "render_contract.json missing; Blender quit",
        })
        self.assertEqual(missing_contract, manifests.MANUAL_REVIEW)
        for group in (corrupt, missing_contract):
            self.assertFalse(
                manifests.GROUP_SPEC_BY_NAME[group].automatic_execution_allowed
            )

    def test_status_drift_blocks_current_generation_without_shrinking_union(self) -> None:
        bundle = self.derive()
        manifests.write_bundle(bundle, self.output)
        row = dict(self.status_rows[0])
        row["status"] = "accepted"
        row["updated_at"] = "2026-07-19 23:59:59"
        self.append_status(row)
        with self.assertRaisesRegex(
            manifests.BatchFailedRepairManifestError,
            "differs from frozen IDs",
        ):
            self.derive()
        self.assertEqual(manifests.load_committed_bundle(self.output), bundle.sidecar)

    def test_missing_source_and_csv_tampering_fail_closed(self) -> None:
        bundle = self.derive()
        manifests.write_bundle(bundle, self.output)
        first_model = Path(self.catalog_rows[0]["model_file"])
        first_model.unlink()
        with self.assertRaisesRegex(
            manifests.BatchFailedRepairManifestError,
            "source model is missing",
        ):
            self.derive()

        path = self.output / manifests.GROUP_FILES[
            ("batch0001", manifests.MANUAL_REVIEW)
        ]
        with path.open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaisesRegex(
            manifests.BatchFailedRepairManifestError,
            "CSV hash mismatch",
        ):
            manifests.load_committed_bundle(self.output)


if __name__ == "__main__":
    unittest.main()
