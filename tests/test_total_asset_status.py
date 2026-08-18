from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from total_asset_status import StatusDataError, load_effective_statuses  # noqa: E402


CATALOG_FIELDS = [
    "asset_id",
    "identity_key",
    "model_file",
    "render_order",
    "render_batch",
]


class TotalAssetStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inventory = self.root / "inventory"
        self.inventory.mkdir()
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.legacy = self.inventory / "total_asset_pilot_1000.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def write_status(self, name: str, rows: list[object], *, bad_tail: bool = False) -> None:
        path = self.inventory / f"total_asset_render_status_{name}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if bad_tail:
                handle.write('{"asset_id":')

    def seed_catalog(self) -> None:
        self.write_csv(
            self.catalog,
            CATALOG_FIELDS,
            [
                {
                    "asset_id": "A00000",
                    "identity_key": "identity-a",
                    "model_file": "/models/a.blend",
                    "render_order": "1",
                    "render_batch": "0000",
                },
                {
                    "asset_id": "B00001",
                    "identity_key": "identity-b",
                    "model_file": "/models/b.blend",
                    "render_order": "1001",
                    "render_batch": "0001",
                },
            ],
        )
        self.write_csv(
            self.legacy,
            ["asset_id", "title", "model_file"],
            [
                {"asset_id": "000001", "title": "Legacy A", "model_file": "/models/a.blend"},
                {"asset_id": "000002", "title": "Legacy B", "model_file": "/models/b.blend"},
            ],
        )

    def test_legacy_maps_only_to_batch0000_and_batch0001_is_formal_only(self) -> None:
        self.seed_catalog()
        self.write_status(
            "pilot1000",
            [
                {
                    "asset_id": "000001",
                    "title": "Legacy A",
                    "batch": "pilot1000",
                    "status": "accepted",
                    "updated_at": "2026-07-15 10:00:00",
                },
                {
                    "asset_id": "B00001",
                    "batch": "pilot1000",
                    "status": "accepted",
                    "updated_at": "2026-07-16 10:00:00",
                },
            ],
        )
        self.write_status(
            "batch0001_worker",
            [
                {
                    "asset_id": "B00001",
                    "batch": "batch0001_worker",
                    "status": "needs_review",
                    "updated_at": "2026-07-15 09:00:00",
                }
            ],
        )
        statuses = load_effective_statuses(self.catalog, self.inventory, self.legacy)
        self.assertEqual(statuses["A00000"]["status"], "accepted")
        self.assertEqual(statuses["A00000"]["render_batch"], "0000")
        self.assertEqual(statuses["B00001"]["status"], "needs_review")
        self.assertEqual(
            set(load_effective_statuses(
                self.catalog,
                self.inventory,
                self.legacy,
                batch_key="batch0001",
            )),
            {"B00001"},
        )

    def test_newer_formal_repair_overrides_legacy_status(self) -> None:
        self.seed_catalog()
        self.write_status(
            "pilot1000",
            [{
                "asset_id": "000001",
                "title": "Legacy A",
                "batch": "pilot1000",
                "status": "accepted",
                "updated_at": "2026-07-16 10:00:00",
            }],
        )
        self.write_status(
            "batch0000_repair",
            [{
                "asset_id": "A00000",
                "batch": "batch0000_repair",
                "status": "failed",
                "updated_at": "2026-07-16 11:00:00",
            }],
        )
        statuses = load_effective_statuses(self.catalog, self.inventory, self.legacy)
        self.assertEqual(statuses["A00000"]["status"], "failed")

    def test_newer_legacy_success_overrides_stale_formal_failure(self) -> None:
        self.seed_catalog()
        self.write_status(
            "batch0000_worker",
            [{
                "asset_id": "A00000",
                "batch": "batch0000_worker",
                "status": "failed",
                "updated_at": "2026-07-16 09:00:00",
            }],
        )
        self.write_status(
            "pilot1000_repair",
            [{
                "asset_id": "000001",
                "title": "Legacy A",
                "batch": "pilot1000_repair",
                "status": "accepted",
                "updated_at": "2026-07-16 10:00:00",
            }],
        )
        statuses = load_effective_statuses(self.catalog, self.inventory, self.legacy)
        self.assertEqual(statuses["A00000"]["status"], "accepted")

    def test_wrong_batch_and_identity_mismatch_are_rejected(self) -> None:
        self.seed_catalog()
        self.write_status(
            "wrong",
            [
                {
                    "asset_id": "A00000",
                    "batch": "batch0001_wrong",
                    "status": "accepted",
                },
                {
                    "asset_id": "B00001",
                    "batch": "batch0001_wrong",
                    "identity_key": "not-identity-b",
                    "status": "accepted",
                },
                {
                    "asset_id": "B00001",
                    "batch": "batch0001oops",
                    "status": "accepted",
                },
            ],
        )
        statuses = load_effective_statuses(self.catalog, self.inventory, self.legacy)
        self.assertEqual(statuses, {})

    def test_latest_duplicate_status_wins_and_bad_json_is_ignored(self) -> None:
        self.seed_catalog()
        self.write_status(
            "batch0001_worker",
            [
                {
                    "asset_id": "B00001",
                    "batch": "batch0001_worker",
                    "status": "failed",
                    "updated_at": "2026-07-15 10:00:00",
                },
                {
                    "asset_id": "B00001",
                    "batch": "batch0001_worker",
                    "status": "accepted",
                    "updated_at": "2026-07-15 11:00:00",
                },
            ],
            bad_tail=True,
        )
        statuses = load_effective_statuses(self.catalog, self.inventory, self.legacy)
        self.assertEqual(statuses["B00001"]["status"], "accepted")

    def test_transient_gpu_busy_never_regresses_terminal_effective_status(self) -> None:
        self.seed_catalog()
        self.write_status(
            "batch0001_worker",
            [
                {
                    "asset_id": "B00001",
                    "batch": "batch0001_worker",
                    "status": "needs_review",
                    "updated_at": "2026-07-16 10:00:00",
                },
                {
                    "asset_id": "B00001",
                    "batch": "batch0001_worker",
                    "status": "blocked_gpu_busy",
                    "failure_category": "gpu_busy",
                    "updated_at": "2026-07-17 10:00:00",
                },
            ],
        )
        self.write_status(
            "batch0000_busy_only",
            [{
                "asset_id": "A00000",
                "batch": "batch0000_busy_only",
                "status": "blocked_gpu_busy",
                "updated_at": "2026-07-17 11:00:00",
            }],
        )
        statuses = load_effective_statuses(
            self.catalog, self.inventory, self.legacy
        )
        self.assertEqual(statuses["B00001"]["status"], "needs_review")
        self.assertNotIn("A00000", statuses)

    def test_duplicate_catalog_asset_id_fails_closed(self) -> None:
        duplicate = {
            "asset_id": "A00000",
            "identity_key": "identity-a",
            "model_file": "/models/a.blend",
            "render_order": "1",
            "render_batch": "0000",
        }
        self.write_csv(self.catalog, CATALOG_FIELDS, [duplicate, duplicate])
        with self.assertRaises(StatusDataError):
            load_effective_statuses(self.catalog, self.inventory, self.legacy)

    def test_ambiguous_legacy_id_and_title_are_not_mapped(self) -> None:
        self.write_csv(
            self.catalog,
            CATALOG_FIELDS,
            [
                {
                    "asset_id": "A00000",
                    "identity_key": "identity-a",
                    "model_file": "/models/a.blend",
                    "render_order": "1",
                    "render_batch": "0000",
                },
                {
                    "asset_id": "C00000",
                    "identity_key": "identity-c",
                    "model_file": "/models/c.blend",
                    "render_order": "2",
                    "render_batch": "0000",
                },
            ],
        )
        self.write_csv(
            self.legacy,
            ["asset_id", "title", "model_file"],
            [
                {"asset_id": "000001", "title": "Same", "model_file": "/models/a.blend"},
                {"asset_id": "000001", "title": "Same", "model_file": "/models/c.blend"},
            ],
        )
        self.write_status(
            "pilot1000_repair",
            [{
                "asset_id": "000001",
                "title": "Same",
                "batch": "pilot1000_repair",
                "status": "accepted",
            }],
        )
        self.assertEqual(
            load_effective_statuses(self.catalog, self.inventory, self.legacy),
            {},
        )

    def test_missing_empty_or_malformed_catalog_fails_closed(self) -> None:
        with self.subTest("missing"):
            with self.assertRaises(StatusDataError):
                load_effective_statuses(self.catalog, self.inventory, self.legacy)

    def test_requested_model_is_stronger_than_colliding_legacy_id(self) -> None:
        self.write_csv(
            self.catalog,
            CATALOG_FIELDS,
            [
                {
                    "asset_id": "000001",
                    "identity_key": "identity-old-collision",
                    "model_file": "/models/collision.blend",
                    "render_order": "1",
                    "render_batch": "0000",
                },
                {
                    "asset_id": "A00000",
                    "identity_key": "identity-requested",
                    "model_file": "/models/requested.blend",
                    "render_order": "2",
                    "render_batch": "0000",
                },
            ],
        )
        self.write_csv(
            self.legacy,
            ["asset_id", "title", "model_file"],
            [{
                "asset_id": "000001",
                "title": "Legacy",
                "model_file": "/models/collision.blend",
            }],
        )
        self.write_status(
            "pilot1000_repair",
            [{
                "asset_id": "000001",
                "title": "Legacy",
                "source_model_requested": "/models/requested.blend",
                "batch": "pilot1000_repair",
                "status": "accepted",
            }],
        )
        statuses = load_effective_statuses(self.catalog, self.inventory, self.legacy)
        self.assertEqual(set(statuses), {"A00000"})
        with self.subTest("missing required headers"):
            self.write_csv(self.catalog, ["asset_id"], [{"asset_id": "A"}])
            with self.assertRaises(StatusDataError):
                load_effective_statuses(self.catalog, self.inventory, self.legacy)
        with self.subTest("empty"):
            self.write_csv(self.catalog, CATALOG_FIELDS, [])
            with self.assertRaises(StatusDataError):
                load_effective_statuses(self.catalog, self.inventory, self.legacy)
        with self.subTest("row missing asset id"):
            self.write_csv(
                self.catalog,
                CATALOG_FIELDS,
                [{
                    "asset_id": "",
                    "identity_key": "identity-a",
                    "model_file": "/models/a.blend",
                    "render_order": "1",
                    "render_batch": "0000",
                }],
            )
            with self.assertRaises(StatusDataError):
                load_effective_statuses(self.catalog, self.inventory, self.legacy)


if __name__ == "__main__":
    unittest.main()
