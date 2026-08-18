from __future__ import annotations

import contextlib
import csv
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

import build_batch0001_failed_repair_manifests as manifests  # noqa: E402


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


class Batch0001FailedRepairManifestTests(unittest.TestCase):
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
        self.catalog_rows: list[dict[str, str]] = []
        self.status_rows: list[dict[str, object]] = []
        self.order_by_id: dict[str, str] = {}

        ordinal = 0
        for spec in manifests.GROUP_SPECS:
            for asset_id in spec.asset_ids:
                ordinal += 1
                model = self.models / f"{asset_id}.blend"
                header = (
                    b"BLENDER-v360"
                    if ordinal % 3 == 1
                    else b"BLENDER-v410"
                    if ordinal % 3 == 2
                    else b"BLENDER-v450"
                )
                model.write_bytes(header + asset_id.encode("ascii"))
                order = str(1000 + ordinal)
                self.order_by_id[asset_id] = order
                self.catalog_rows.append(self.catalog_row(asset_id, model, order))
                self.status_rows.append(
                    self.status_row(
                        asset_id,
                        group=spec.name,
                        updated_at=f"2026-07-18 10:{ordinal:02d}:00",
                    )
                )
        self.write_catalog()
        self.write_status()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def catalog_row(
        self,
        asset_id: str,
        model: Path,
        order: str,
        *,
        batch: str = "0001",
        source_root: Path | None = None,
    ) -> dict[str, str]:
        return {
            "asset_id": asset_id,
            "identity_key": f"identity-{asset_id}",
            "model_file": str(model),
            "source_root": str(source_root or self.models),
            "render_order": order,
            "render_batch": batch,
            "source": "test",
            "title": f"asset {asset_id}",
            "render_route": "static",
            "render_engine_hint": "source",
        }

    def status_row(
        self,
        asset_id: str,
        *,
        group: str,
        updated_at: str,
        status: str = "failed",
        error_override: str | None = None,
    ) -> dict[str, object]:
        failure_category = ""
        error = ""
        if group == manifests.COURSE_TIMEOUT_EXACT:
            failure_category = "render_timeout"
            error = "worker_ssh_timeout"
        elif group == manifests.INFERRED_BLANK_TIMEOUT:
            failure_category = "remote_render_failure"
            error = " \r\n"
        elif group == manifests.TRANSPORT:
            failure_category = (
                "remote_render_failure"
                if asset_id == "000661"
                else "render_timeout"
            )
            error = (
                "Connection closed by remote host"
                if asset_id == "000661"
                else "worker_ssh_timeout"
            )
        elif group == manifests.SCENE_AUDIT:
            failure_category = "render_timeout"
            error = "render timeout after 7200s; remote Blender terminated"
        if error_override is not None:
            error = error_override
        return {
            "asset_id": asset_id,
            "identity_key": f"identity-{asset_id}",
            "render_order": self.order_by_id[asset_id],
            "render_batch": "0001",
            "batch": "batch0001",
            "status": status,
            "failure_category": failure_category,
            "error": error,
            "worker": "test-worker",
            "updated_at": updated_at,
        }

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
        )

    def test_build_and_validate_exact_disjoint_failed_groups(self) -> None:
        bundle = self.derive()
        self.assertEqual(bundle.sidecar["count"], 42)
        self.assertEqual(
            {
                group: len(bundle.rows_by_group[group])
                for group in manifests.GROUPS
            },
            {
                manifests.DYNAMIC_STATIC_CONTRACT: 17,
                manifests.COURSE_TIMEOUT_EXACT: 20,
                manifests.INFERRED_BLANK_TIMEOUT: 2,
                manifests.TRANSPORT: 2,
                manifests.SCENE_AUDIT: 1,
            },
        )
        observed_ids = {
            row["asset_id"]
            for group in manifests.GROUPS
            for row in bundle.rows_by_group[group]
        }
        self.assertEqual(observed_ids, set(manifests.EXPECTED_FAILED_ASSET_IDS))
        for spec in manifests.GROUP_SPECS:
            for row in bundle.rows_by_group[spec.name]:
                self.assertEqual(row["repair_group"], spec.name)
                self.assertEqual(row["expected_status"], "failed")
                self.assertRegex(
                    row["expected_status_signature"], r"^[0-9a-f]{64}$"
                )
                self.assertRegex(row["source_stat_signature"], r"^[0-9a-f]{64}$")
                self.assertIn(row["source_family"], {"3.6", "4.2", "4.5"})
                self.assertEqual(
                    json.loads(row["required_rule_ids"]),
                    list(spec.required_rule_ids),
                )
                self.assertEqual(row["max_attempts"], str(spec.max_attempts))

        sidecar = manifests.write_bundle(bundle, self.output)
        self.assertEqual(sidecar["schema"], manifests.SCHEMA)
        self.assertRegex(
            sidecar["generation"], r"^batch0001-failed-[0-9a-f]{20}$"
        )
        self.assertRegex(sidecar["hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(sidecar["snapshot_hash"], r"^[0-9a-f]{64}$")
        for group in manifests.GROUPS:
            path = self.output / manifests.GROUP_FILES[group]
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                sidecar["groups"][group]["sha256"],
            )
        self.assertEqual(manifests.validate_bundle(self.derive(), self.output), sidecar)
        self.assertEqual(list(self.output.glob(".*.tmp")), [])

    def test_generation_and_status_signatures_are_deterministic(self) -> None:
        first = self.derive()
        second = self.derive()
        self.assertEqual(first.sidecar, second.sidecar)
        self.assertEqual(first.csv_bytes_by_group, second.csv_bytes_by_group)

        manifests.write_bundle(first, self.output)
        asset_id = manifests.DYNAMIC_STATIC_CONTRACT_IDS[0]
        self.append_status(
            self.status_row(
                asset_id,
                group=manifests.DYNAMIC_STATIC_CONTRACT,
                updated_at="2026-07-18 23:59:59",
                error_override="new diagnostic evidence",
            )
        )
        changed = self.derive()
        self.assertNotEqual(first.sidecar["snapshot_hash"], changed.sidecar["snapshot_hash"])
        self.assertNotEqual(first.sidecar["generation"], changed.sidecar["generation"])
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "sidecar does not match"
        ):
            manifests.validate_bundle(changed, self.output)

    def test_execution_validation_is_idempotent_and_allows_new_failed_ids(self) -> None:
        bundle = self.derive()
        manifests.write_bundle(bundle, self.output)
        initial = manifests.validate_execution_bundle(
            catalog_path=self.catalog,
            inventory_dir=self.inventory,
            output_dir=self.output,
        )
        self.assertEqual(initial["pending"], 42)
        self.assertEqual(initial["landed"], 0)
        self.assertEqual(initial["attempted_failed"], 0)

        landed_id = manifests.DYNAMIC_STATIC_CONTRACT_IDS[0]
        self.append_status(
            self.status_row(
                landed_id,
                group=manifests.DYNAMIC_STATIC_CONTRACT,
                updated_at="2026-07-18 23:58:00",
                status="needs_review",
            )
        )
        attempted_id = manifests.TRANSPORT_IDS[0]
        attempted = self.status_row(
            attempted_id,
            group=manifests.TRANSPORT,
            updated_at="2026-07-18 23:58:01",
        )
        attempted["batch"] = "batch0001_failed_repair_transport"
        self.append_status(attempted)

        extra_id = "009999"
        model = self.models / f"{extra_id}.blend"
        model.write_bytes(b"BLENDER-v360")
        self.order_by_id[extra_id] = "1999"
        self.catalog_rows.append(self.catalog_row(extra_id, model, "1999"))
        self.write_catalog()
        self.append_status(
            self.status_row(
                extra_id,
                group=manifests.COURSE_TIMEOUT_EXACT,
                updated_at="2026-07-18 23:59:59",
            )
        )

        current = manifests.validate_execution_bundle(
            catalog_path=self.catalog,
            inventory_dir=self.inventory,
            output_dir=self.output,
        )
        self.assertEqual(current["pending"], 40)
        self.assertEqual(current["landed"], 1)
        self.assertEqual(current["attempted_failed"], 1)
        self.assertEqual(current["additional_effective_failed_asset_ids"], [extra_id])

    def test_execution_validation_blocks_unowned_failed_status_drift(self) -> None:
        bundle = self.derive()
        manifests.write_bundle(bundle, self.output)
        asset_id = manifests.SCENE_AUDIT_IDS[0]
        drifted = self.status_row(
            asset_id,
            group=manifests.SCENE_AUDIT,
            updated_at="2026-07-18 23:59:59",
            error_override="new unreviewed diagnosis",
        )
        drifted["batch"] = "batch0001_other_repair"
        self.append_status(drifted)
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "unsafe for execution"
        ):
            manifests.validate_execution_bundle(
                catalog_path=self.catalog,
                inventory_dir=self.inventory,
                output_dir=self.output,
            )

    def test_current_failed_status_drift_blocks_instead_of_shrinking_union(self) -> None:
        asset_id = manifests.DYNAMIC_STATIC_CONTRACT_IDS[0]
        self.append_status(
            self.status_row(
                asset_id,
                group=manifests.DYNAMIC_STATIC_CONTRACT,
                updated_at="2026-07-18 23:59:59",
                status="accepted",
            )
        )
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "differs from the frozen 42 IDs"
        ):
            self.derive()

    def test_new_unreviewed_failed_asset_blocks_generation(self) -> None:
        asset_id = "009999"
        model = self.models / f"{asset_id}.blend"
        model.write_bytes(b"BLENDER-v360")
        self.order_by_id[asset_id] = "1999"
        self.catalog_rows.append(self.catalog_row(asset_id, model, "1999"))
        self.write_catalog()
        self.append_status(
            self.status_row(
                asset_id,
                group=manifests.COURSE_TIMEOUT_EXACT,
                updated_at="2026-07-18 23:59:59",
            )
        )
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "unexpected=\['009999'\]"
        ):
            self.derive()

    def test_missing_or_misrooted_source_blocks_generation(self) -> None:
        asset_id = manifests.SCENE_AUDIT_IDS[0]
        row = next(row for row in self.catalog_rows if row["asset_id"] == asset_id)
        model = Path(row["model_file"])
        model.unlink()
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "source model is missing"
        ):
            self.derive()

        model.write_bytes(b"BLENDER-v360")
        outside = self.root / "outside"
        outside.mkdir()
        row["source_root"] = str(outside)
        self.write_catalog()
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "outside source_root"
        ):
            self.derive()

    def test_duplicate_failed_identity_blocks_generation(self) -> None:
        first, second = manifests.DYNAMIC_STATIC_CONTRACT_IDS[:2]
        first_identity = next(
            row["identity_key"] for row in self.catalog_rows if row["asset_id"] == first
        )
        for row in self.catalog_rows:
            if row["asset_id"] == second:
                row["identity_key"] = first_identity
        self.write_catalog()
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError,
            "identity_key is not unique|inconsistent identity_key|frozen 42 IDs",
        ):
            self.derive()

    def test_validate_detects_csv_tampering(self) -> None:
        bundle = self.derive()
        manifests.write_bundle(bundle, self.output)
        path = self.output / manifests.GROUP_FILES[manifests.TRANSPORT]
        with path.open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "CSV hash mismatch"
        ):
            manifests.validate_bundle(self.derive(), self.output)

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
            self.assertEqual(manifests.main(["validate-execution", *common]), 0)
        payloads = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [payload["command"] for payload in payloads],
            ["build", "validate", "validate-execution"],
        )
        self.assertTrue(all(payload["valid"] for payload in payloads))
        self.assertTrue(all(payload["count"] == 42 for payload in payloads))

    def test_group_definition_validator_rejects_overlap_and_unknown_rule(self) -> None:
        overlap = (
            manifests.RepairGroupSpec("a", ("1",), ("status_latest_by_updated_at",), 1),
            manifests.RepairGroupSpec("b", ("1",), ("status_latest_by_updated_at",), 1),
        )
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "overlap"
        ):
            manifests._validate_group_specs(overlap, {"1"})

        unknown = (
            manifests.RepairGroupSpec("a", ("1",), ("not_a_rule",), 1),
        )
        with self.assertRaisesRegex(
            manifests.FailedRepairManifestError, "unavailable reviewed rule"
        ):
            manifests._validate_group_specs(unknown, {"1"})


if __name__ == "__main__":
    unittest.main()
