from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_work_buffer as work_buffer
from storage_capacity import DiskCapacity, DiskCapacityError


def catalog_row(
    asset_id: str,
    order: int | None,
    *,
    inventory_status: str = "ready",
    duplicate_of: str = "",
) -> dict[str, str]:
    return {
        "asset_id": asset_id,
        "identity_key": f"identity-{asset_id}",
        "model_file": f"/models/{asset_id}.blend" if inventory_status == "ready" else "",
        "render_order": str(order or ""),
        "render_batch": f"{(order - 1) // 2:04d}" if order else "",
        "inventory_status": inventory_status,
        "duplicate_of": duplicate_of,
    }


class WorkBufferTests(unittest.TestCase):
    def capacity(self, free_bytes: int = 2 * 1024**4) -> work_buffer.CapacityPlan:
        return work_buffer.build_capacity_plan(
            "/assets",
            probe=lambda _path: DiskCapacity(
                total_bytes=3 * 1024**4,
                used_bytes=3 * 1024**4 - free_bytes,
                free_bytes=free_bytes,
                source="test",
            ),
        )

    def test_catalog_supply_classes_are_disjoint(self) -> None:
        rows = [
            catalog_row("a", 1),
            catalog_row("b", None, inventory_status="pending_download"),
            catalog_row("c", None, inventory_status="indexed_only"),
            catalog_row("d", 2, duplicate_of="a"),
        ]
        counts, groups = work_buffer.classify_catalog_rows(rows)
        self.assertEqual(counts["total"], 4)
        self.assertEqual(counts["ready"], 1)
        self.assertEqual(counts["pending_download"], 1)
        self.assertEqual(counts["indexed_only"], 1)
        self.assertEqual(counts["other"], 1)
        self.assertEqual({key: len(value) for key, value in groups.items()}, {
            "ready": 1,
            "pending_download": 1,
            "indexed_only": 1,
            "other": 1,
        })

    def test_72_96_hour_estimate_uses_per_gpu_capacity(self) -> None:
        rows = [catalog_row(f"{index:03d}", index) for index in range(1, 9)]
        # Eight one-asset batches are full under this test batch size.  At
        # 60 GPU-minutes each over two GPUs, the ready buffer is four hours.
        plan = work_buffer.build_work_buffer_plan(
            rows,
            {},
            capacity=self.capacity(),
            worker_count=2,
            batch_size=2,
            low_watermark_hours=6,
            target_hours=8,
            fallback_gpu_minutes_per_asset=60,
        )
        self.assertEqual(plan.dispatchable_asset_count, 8)
        self.assertEqual(plan.estimated_buffer_hours_per_gpu, 4.0)
        self.assertEqual(plan.assets_needed_for_low_watermark, 4)
        self.assertEqual(plan.assets_needed_for_target, 8)
        self.assertEqual(plan.supply_state, "supply_blocked")
        self.assertEqual(plan.refill_action, "no_source_supply")

    def test_terminal_rows_are_not_dispatchable_and_history_drives_estimate(self) -> None:
        rows = [catalog_row("a", 1), catalog_row("b", 2)]
        statuses = {
            "a": {"status": "accepted", "render_elapsed_minutes": 10},
            "b": {"status": "running", "render_elapsed_minutes": 30},
        }
        plan = work_buffer.build_work_buffer_plan(
            rows,
            statuses,
            capacity=self.capacity(),
            worker_count=1,
            batch_size=2,
            low_watermark_hours=1,
            target_hours=2,
        )
        self.assertEqual(plan.dispatchable_asset_count, 1)
        self.assertEqual(plan.estimated_gpu_minutes_per_asset, 20.0)
        self.assertEqual(plan.estimate_source, "formal_status_median")
        self.assertEqual(plan.estimate_samples, 2)

    def test_partial_batch_is_visible_but_never_dispatchable(self) -> None:
        rows = [catalog_row("a", 1), catalog_row("b", 2), catalog_row("c", 3)]
        plan = work_buffer.build_work_buffer_plan(
            rows,
            {},
            capacity=self.capacity(),
            worker_count=1,
            batch_size=2,
            low_watermark_hours=1,
            target_hours=2,
            fallback_gpu_minutes_per_asset=60,
        )
        self.assertEqual(plan.full_batch_keys, ("0000",))
        self.assertEqual(plan.partial_batch_keys, ("0001",))
        self.assertEqual(plan.dispatchable_asset_count, 2)

    def test_capacity_unknown_and_pause_are_fail_closed_for_refill(self) -> None:
        unknown = work_buffer.build_capacity_plan(
            "/assets",
            probe=lambda _path: (_ for _ in ()).throw(DiskCapacityError("bad df")),
        )
        self.assertFalse(unknown.known)
        self.assertFalse(unknown.download_allowed)
        rows = [
            catalog_row("ready", 1),
            catalog_row("remote", None, inventory_status="pending_download"),
        ]
        plan = work_buffer.build_work_buffer_plan(
            rows,
            {},
            capacity=unknown,
            worker_count=1,
            batch_size=1,
            low_watermark_hours=2,
            target_hours=3,
            fallback_gpu_minutes_per_asset=60,
        )
        self.assertEqual(plan.dispatchable_asset_count, 1)
        self.assertEqual(plan.supply_state, "supply_blocked")
        self.assertEqual(plan.refill_action, "capacity_unknown")

        pause = self.capacity(500 * 1024**3 - 1)
        self.assertEqual(pause.tier, "pause")
        self.assertFalse(pause.download_allowed)
        warning = self.capacity(500 * 1024**3)
        self.assertEqual(warning.tier, "warning")
        self.assertTrue(warning.download_allowed)
        ok = self.capacity(1024 * 1024**3)
        self.assertEqual(ok.tier, "ok")
        self.assertTrue(ok.download_allowed)

    def test_pending_download_plan_never_mutates_catalog(self) -> None:
        rows = [
            catalog_row("ready", 1),
            catalog_row("remote-a", None, inventory_status="pending_download"),
            catalog_row("remote-b", None, inventory_status="pending_download"),
        ]
        before = json.dumps(rows, sort_keys=True)
        plan = work_buffer.build_work_buffer_plan(
            rows,
            {},
            capacity=self.capacity(),
            worker_count=1,
            batch_size=1,
            low_watermark_hours=2,
            target_hours=3,
            fallback_gpu_minutes_per_asset=60,
        )
        self.assertEqual(plan.supply_state, "refill_required")
        self.assertEqual(plan.refill_action, "plan_pending_download")
        self.assertEqual(plan.refill_asset_target, 2)
        self.assertEqual(json.dumps(rows, sort_keys=True), before)

    def test_path_interface_reads_formal_status_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            inventory = root / "inventory"
            inventory.mkdir()
            fields = [
                "asset_id", "identity_key", "model_file", "render_order",
                "render_batch", "inventory_status", "duplicate_of",
            ]
            rows = [catalog_row("a", 1)]
            with catalog.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            status = inventory / "total_asset_render_status_formal.jsonl"
            status.write_text(json.dumps({
                "asset_id": "a",
                "identity_key": "identity-a",
                "render_order": "1",
                "render_batch": "0000",
                "batch": "batch0000",
                "status": "accepted",
                "render_elapsed_minutes": 12,
                "updated_at": "2026-07-20 00:00:00",
            }) + "\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in (catalog, status)}
            plan = work_buffer.plan_from_paths(
                catalog_path=catalog,
                inventory_dir=inventory,
                capacity_path=root,
                worker_count=1,
                batch_size=1,
                capacity_probe=lambda _path: DiskCapacity(
                    2 * 1024**4, 1024**4, 1024**4, "test"
                ),
            )
            self.assertEqual(plan.dispatchable_asset_count, 0)
            self.assertEqual(plan.estimated_gpu_minutes_per_asset, 12)
            self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == "__main__":
    unittest.main()
