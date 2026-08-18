from __future__ import annotations

import csv
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_batch0001_deferred_repair_manifests as manifests  # noqa: E402


CATALOG_FIELDS = [
    "asset_id",
    "identity_key",
    "model_file",
    "render_order",
    "render_batch",
    "source",
    "title",
    "source_root",
    "render_route",
    "render_engine_hint",
]


class Batch0001DeferredRepairManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.models = self.root / "models"
        self.output = self.root / "manifests"
        self.inventory.mkdir()
        self.models.mkdir()
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.status = (
            self.inventory / "total_asset_render_status_batch0001_test.jsonl"
        )

        self.model_paths = {
            "legacy": self.models / "legacy.blend",
            "modern": self.models / "modern.blend",
            "unknown": self.models / "unknown.blend",
            "imported": self.models / "imported.stl",
            "effective_deferred": self.models / "effective-deferred.blend",
            "effective_accepted": self.models / "effective-accepted.blend",
            "accepted": self.models / "accepted.blend",
            "other_batch": self.models / "other-batch.blend",
        }
        self.model_paths["legacy"].write_bytes(b"BLENDER-v306")
        self.model_paths["modern"].write_bytes(b"BLENDER-v405")
        self.model_paths["unknown"].write_bytes(b"not-a-blend-header")
        self.model_paths["imported"].write_text("solid imported\nendsolid\n")
        self.model_paths["effective_deferred"].write_bytes(b"BLENDER17-01v0500")
        self.model_paths["effective_accepted"].write_bytes(b"BLENDER-v306")
        self.model_paths["accepted"].write_bytes(b"BLENDER-v405")
        self.model_paths["other_batch"].write_bytes(b"BLENDER-v405")

        self.catalog_rows = [
            self.catalog_row("L", "identity-l", "legacy", "1001"),
            self.catalog_row("M", "identity-m", "modern", "1002"),
            self.catalog_row("U", "identity-u", "unknown", "1003"),
            self.catalog_row("I", "identity-i", "imported", "1004"),
            self.catalog_row(
                "D", "identity-d", "effective_deferred", "1005"
            ),
            self.catalog_row(
                "A", "identity-a", "effective_accepted", "1006"
            ),
            self.catalog_row("X", "identity-x", "accepted", "1007"),
            self.catalog_row(
                "B2", "identity-b2", "other_batch", "2001", batch="0002"
            ),
        ]
        self.write_catalog()
        self.write_status([
            self.status_row("L", "deferred", "2026-07-18 01:00:00"),
            self.status_row("M", "deferred", "2026-07-18 01:00:01"),
            self.status_row("U", "deferred", "2026-07-18 01:00:02"),
            self.status_row("I", "deferred", "2026-07-18 01:00:03"),
            self.status_row("D", "accepted", "2026-07-18 00:00:00"),
            self.status_row("D", "deferred", "2026-07-18 02:00:00"),
            self.status_row("A", "deferred", "2026-07-18 00:00:00"),
            self.status_row("A", "accepted", "2026-07-18 02:00:00"),
            self.status_row("X", "accepted", "2026-07-18 01:00:04"),
            self.status_row(
                "B2", "deferred", "2026-07-18 01:00:05", batch="batch0002"
            ),
        ])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def catalog_row(
        self,
        asset_id: str,
        identity: str,
        model_key: str,
        order: str,
        *,
        batch: str = "0001",
    ) -> dict[str, str]:
        model = self.model_paths[model_key]
        return {
            "asset_id": asset_id,
            "identity_key": identity,
            "model_file": str(model),
            "render_order": order,
            "render_batch": batch,
            "source": "test",
            "title": model.stem,
            "source_root": str(model.parent),
            "render_route": "static",
            "render_engine_hint": "source",
        }

    def status_row(
        self,
        asset_id: str,
        status: str,
        updated_at: str,
        *,
        batch: str = "batch0001",
    ) -> dict[str, str]:
        return {
            "asset_id": asset_id,
            "batch": batch,
            "status": status,
            "failure_category": "test_deferred" if status == "deferred" else "",
            "worker": "test-worker",
            "updated_at": updated_at,
        }

    def write_catalog(self) -> None:
        with self.catalog.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
            writer.writeheader()
            writer.writerows(self.catalog_rows)

    def write_status(self, rows: list[dict[str, str]]) -> None:
        with self.status.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    def append_status(self, row: dict[str, str]) -> None:
        with self.status.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def derive(self) -> manifests.ManifestBundle:
        return manifests.derive_bundle(
            catalog_path=self.catalog,
            inventory_dir=self.inventory,
        )

    def test_build_and_validate_disjoint_effective_deferred_manifests(self) -> None:
        bundle = self.derive()
        self.assertEqual(
            [row["asset_id"] for row in bundle.rows_by_group[manifests.LEGACY_GPU0]],
            ["L", "U"],
        )
        self.assertEqual(
            [row["asset_id"] for row in bundle.rows_by_group[manifests.MODERN_VULKAN]],
            ["M", "I", "D"],
        )
        unknown = next(
            row
            for row in bundle.rows_by_group[manifests.LEGACY_GPU0]
            if row["asset_id"] == "U"
        )
        self.assertEqual(unknown["detected_source_blender_version"], "unknown")
        self.assertEqual(unknown["selected_blender_family"], "unknown")

        sidecar = manifests.write_bundle(bundle, self.output)
        self.assertEqual(sidecar["schema"], manifests.SCHEMA)
        self.assertEqual(sidecar["count"], 5)
        self.assertEqual(
            sidecar["groups"][manifests.LEGACY_GPU0]["count"], 2
        )
        self.assertEqual(
            sidecar["groups"][manifests.MODERN_VULKAN]["count"], 3
        )
        self.assertRegex(sidecar["generation"], r"^batch0001-[0-9a-f]{20}$")
        self.assertRegex(sidecar["hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(sidecar["invariants"], {
            "effective_status": "deferred",
            "render_batch": "0001",
            "source_files_exist": True,
            "identity_unique": True,
            "groups_disjoint": True,
            "group_union_matches_effective_deferred": True,
        })

        for group in manifests.GROUPS:
            path = self.output / manifests.GROUP_FILES[group]
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                sidecar["groups"][group]["sha256"],
            )
        self.assertEqual(manifests.validate_bundle(self.derive(), self.output), sidecar)
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_generation_is_deterministic(self) -> None:
        first = self.derive()
        second = self.derive()
        self.assertEqual(first.sidecar, second.sidecar)
        self.assertEqual(first.csv_bytes_by_group, second.csv_bytes_by_group)

        for row in self.catalog_rows:
            if row["asset_id"] == "M":
                row["title"] = "changed catalog metadata"
        self.write_catalog()
        changed = self.derive()
        self.assertNotEqual(
            first.sidecar["generation"], changed.sidecar["generation"]
        )
        self.assertNotEqual(first.sidecar["hash"], changed.sidecar["hash"])

    def test_cli_build_then_validate(self) -> None:
        common = [
            "--inventory-dir",
            str(self.inventory),
            "--catalog",
            str(self.catalog),
            "--output-dir",
            str(self.output),
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(manifests.main(["build", *common]), 0)
            self.assertEqual(manifests.main(["validate", *common]), 0)
        payloads = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([payload["command"] for payload in payloads], [
            "build", "validate"
        ])
        self.assertTrue(all(payload["valid"] for payload in payloads))

    def test_missing_deferred_source_blocks_generation(self) -> None:
        self.model_paths["legacy"].unlink()
        with self.assertRaisesRegex(
            manifests.RepairManifestError, "source model is missing"
        ):
            self.derive()

    def test_duplicate_deferred_identity_blocks_generation(self) -> None:
        for row in self.catalog_rows:
            if row["asset_id"] == "U":
                row["identity_key"] = "identity-l"
        self.write_catalog()
        with self.assertRaisesRegex(
            manifests.RepairManifestError, "identity_key is not unique"
        ):
            self.derive()

    def test_validate_detects_csv_tampering(self) -> None:
        bundle = self.derive()
        manifests.write_bundle(bundle, self.output)
        path = self.output / manifests.GROUP_FILES[manifests.LEGACY_GPU0]
        with path.open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaisesRegex(
            manifests.RepairManifestError, "CSV hash mismatch"
        ):
            manifests.validate_bundle(self.derive(), self.output)

    def test_validate_detects_effective_status_drift(self) -> None:
        bundle = self.derive()
        manifests.write_bundle(bundle, self.output)
        self.append_status(
            self.status_row("L", "accepted", "2026-07-18 03:00:00")
        )
        with self.assertRaisesRegex(
            manifests.RepairManifestError, "sidecar does not match"
        ):
            manifests.validate_bundle(self.derive(), self.output)

    def test_partition_validator_rejects_overlap_and_incomplete_union(self) -> None:
        legacy = ({"asset_id": "L", "repair_group": manifests.LEGACY_GPU0},)
        overlapping = ({
            "asset_id": "L",
            "repair_group": manifests.MODERN_VULKAN,
        },)
        with self.assertRaisesRegex(manifests.RepairManifestError, "overlap"):
            manifests._validate_partition(
                {
                    manifests.LEGACY_GPU0: legacy,
                    manifests.MODERN_VULKAN: overlapping,
                },
                {"L"},
            )

        with self.assertRaisesRegex(manifests.RepairManifestError, "union differs"):
            manifests._validate_partition(
                {
                    manifests.LEGACY_GPU0: legacy,
                    manifests.MODERN_VULKAN: (),
                },
                {"L", "M"},
            )


if __name__ == "__main__":
    unittest.main()
