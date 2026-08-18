from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_blenderkit_supply_adapter as adapter
import total_asset_supply_refill as refill
from storage_capacity import DiskCapacity, DiskCapacityError


FIELDS = [
    "asset_id", "identity_key", "record_type", "source", "source_asset_id",
    "title", "source_url", "license_name", "source_category_raw", "source_root",
    "model_file", "model_format", "file_size", "content_category",
    "technical_tags", "asset_kind", "render_route", "render_engine_hint",
    "content_hash", "duplicate_of", "download_status", "inventory_status",
    "block_reason", "render_order", "render_batch", "updated_at",
]

PROGRESS_FIELDS = [
    "time", "asset_id", "asset_base_id", "title", "author", "license",
    "asset_url", "file_type", "file_size", "download_api_url", "local_file",
    "status", "error",
]


def catalog_row(
    asset_id: str,
    source_asset_id: str,
    *,
    source: str = "BlenderKit",
    inventory_status: str = "pending_download",
    order: int | None = None,
) -> dict[str, str]:
    row = {field: "" for field in FIELDS}
    row.update({
        "asset_id": asset_id,
        "identity_key": f"manifest|{source}|{source_asset_id}",
        "record_type": "manifest_record",
        "source": source,
        "source_asset_id": source_asset_id,
        "title": asset_id,
        "download_status": "pending_download",
        "inventory_status": inventory_status,
        "render_order": str(order or ""),
        "render_batch": f"{(order - 1) // 1000:04d}" if order else "",
        "updated_at": "2026-07-20 00:00:00",
    })
    return row


def write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_catalog(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_progress(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROGRESS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in PROGRESS_FIELDS})


def make_blend(path: Path) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"BLENDER-v450" + path.name.encode() + b"\0" * 512)
    payload = path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def good_capacity(_path: object) -> DiskCapacity:
    return DiskCapacity(4 * 1024**4, 1024**4, 3 * 1024**4, "test")


class FakeBlenderRunner:
    def __init__(self, object_count: int = 2) -> None:
        self.object_count = object_count
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        self.environments.append(dict(kwargs.get("env") or {}))
        payload = {
            "schema": adapter.PROBE_SCHEMA,
            "status": "verified_renderable",
            "renderable_object_count": self.object_count,
        }
        return subprocess.CompletedProcess(
            command,
            0 if self.object_count else 41,
            stdout=adapter.PROBE_MARKER + json.dumps(payload) + "\n",
            stderr="",
        )


class BlenderKitSupplyAdapterTests(unittest.TestCase):
    def test_explicit_enumeration_then_probe_and_promote_preserves_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            model = assets / "BlenderKit" / "exact-uuid" / "asset.blend"
            make_blend(model)
            catalog = root / "catalog.csv"
            progress = root / "progress.csv"
            placeholder_source_id = "official_access_required#000001"
            placeholder = catalog_row(
                "stable-catalog-id",
                placeholder_source_id,
                inventory_status="indexed_only",
            )
            placeholder.update({
                "record_type": "index_placeholder",
                "identity_key": f"manifest|BlenderKit|{placeholder_source_id}",
                "download_status": "blocked_adapter_not_configured",
                "block_reason": "aggregate source requires exact enumeration",
            })
            write_catalog(catalog, [placeholder])
            snapshot = refill.read_catalog_snapshot(catalog)
            receipt_payload: dict[str, object] = {
                "schema": refill.ENUMERATION_RECEIPT_SCHEMA,
                "expected_catalog_generation": snapshot.generation,
                "source": "BlenderKit",
                "records": [{
                    "placeholder_asset_id": "stable-catalog-id",
                    "placeholder_source_asset_id": placeholder_source_id,
                    "source_asset_id": "exact-uuid",
                    "title": "Exact enumerated model",
                    "source_url": "https://www.blenderkit.com/asset-gallery-detail/exact-uuid/",
                    "license_name": "Royalty Free",
                    "source_category_raw": "model",
                    "source_root": str(model.parent),
                }],
            }
            receipt_payload["receipt_sha256"] = refill.enumeration_receipt_sha256(
                receipt_payload
            )
            receipt_path = root / "enumeration.json"
            receipt_path.write_text(json.dumps(receipt_payload), encoding="utf-8")
            refill.enumerate_aggregate_placeholders(
                catalog_path=catalog,
                state_root=root / "enumeration-state",
                expected_generation=snapshot.generation,
                receipt=refill.read_enumeration_receipt(receipt_path),
            )
            after_enumeration = read_catalog(catalog)[0]
            self.assertEqual(after_enumeration["asset_id"], "stable-catalog-id")
            self.assertEqual(
                after_enumeration["identity_key"],
                f"manifest|BlenderKit|{placeholder_source_id}",
            )
            self.assertEqual(after_enumeration["source_asset_id"], "exact-uuid")

            write_progress(progress, [{
                "asset_id": "exact-uuid",
                "local_file": str(model),
                "status": "downloaded",
            }])
            plan = adapter.build_plan(
                catalog_path=catalog,
                progress_csv=progress,
                asset_root=assets,
                capacity_path=assets,
                capacity_probe=good_capacity,
            )
            self.assertEqual(plan.runnable_count, 1)
            result = adapter.run_adapter(
                catalog_path=catalog,
                progress_csv=progress,
                asset_root=assets,
                capacity_path=assets,
                state_root=root / "adapter-state",
                refill_state_root=root / "refill-state",
                blender_binary="fake-blender",
                max_assets=1,
                promotion_batch_size=1,
                capacity_probe=good_capacity,
                blender_runner=FakeBlenderRunner(),
            )
            self.assertEqual(result["promoted_count"], 1)
            promoted = read_catalog(catalog)[0]
            self.assertEqual(promoted["asset_id"], "stable-catalog-id")
            self.assertEqual(
                promoted["identity_key"],
                f"manifest|BlenderKit|{placeholder_source_id}",
            )
            self.assertEqual(promoted["source_asset_id"], "exact-uuid")
            self.assertEqual(promoted["render_order"], "1")

    def test_plan_maps_exact_uuid_and_base_id_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            exact_model = assets / "exact.blend"
            base_model = assets / "base.blend"
            make_blend(exact_model)
            make_blend(base_model)
            catalog = root / "catalog.csv"
            progress = root / "progress.csv"
            state_root = root / "state-must-not-exist"
            rows = [
                catalog_row("ready", "already", inventory_status="ready", order=1),
                catalog_row("catalog-exact", "download-uuid"),
                catalog_row("catalog-base", "base-uuid", inventory_status="indexed_only"),
                catalog_row("wrong-source", "download-uuid", source="Budeco"),
            ]
            write_catalog(catalog, rows)
            write_progress(progress, [
                {"asset_id": "download-uuid", "asset_base_id": "", "local_file": str(exact_model), "status": "downloaded"},
                {"asset_id": "version-uuid", "asset_base_id": "base-uuid", "local_file": str(base_model), "status": "downloaded"},
            ])
            before_catalog = catalog.read_bytes()
            before_progress = progress.read_bytes()
            plan = adapter.build_plan(
                catalog_path=catalog,
                progress_csv=progress,
                asset_root=assets,
                capacity_path=assets,
                capacity_probe=good_capacity,
            )
            self.assertEqual(plan.eligible_catalog_count, 2)
            self.assertEqual(plan.runnable_count, 2)
            self.assertEqual(
                [(item.asset_id, item.progress_asset_id) for item in plan.candidates],
                [("catalog-exact", "download-uuid"), ("catalog-base", "version-uuid")],
            )
            status = adapter.status_from_paths(
                catalog_path=catalog,
                progress_csv=progress,
                asset_root=assets,
                capacity_path=assets,
                state_root=state_root,
                capacity_probe=good_capacity,
            )
            self.assertTrue(status["read_only"])
            self.assertIsNone(status["recorded"])
            self.assertFalse(state_root.exists())
            self.assertEqual(before_catalog, catalog.read_bytes())
            self.assertEqual(before_progress, progress.read_bytes())

    def test_plan_blocks_ambiguous_missing_and_outside_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            one = assets / "one.blend"
            two = assets / "two.blend"
            outside = root / "outside.blend"
            for path in (one, two, outside):
                make_blend(path)
            catalog = root / "catalog.csv"
            progress = root / "progress.csv"
            write_catalog(catalog, [
                catalog_row("ambiguous", "same"),
                catalog_row("missing", "none"),
                catalog_row("outside", "external"),
                catalog_row("aggregate", "official_access_required#000001", inventory_status="indexed_only"),
            ])
            write_progress(progress, [
                {"asset_id": "same", "local_file": str(one), "status": "downloaded"},
                {"asset_id": "same", "local_file": str(two), "status": "downloaded"},
                {"asset_id": "external", "local_file": str(outside), "status": "downloaded"},
            ])
            plan = adapter.build_plan(
                catalog_path=catalog,
                progress_csv=progress,
                asset_root=assets,
                capacity_path=assets,
                capacity_probe=good_capacity,
            )
            self.assertEqual(plan.runnable_count, 0)
            self.assertEqual(plan.blocked_counts["ambiguous_downloaded_progress_match"], 1)
            self.assertEqual(plan.blocked_counts["no_downloaded_progress_match"], 1)
            self.assertEqual(plan.blocked_counts["download_path_outside_asset_root"], 1)
            self.assertEqual(plan.blocked_counts["aggregate_placeholder_enumeration_required"], 1)

    def test_probe_is_bounded_disables_autoexec_and_sanitizes_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            adapter.os.environ,
            {"BLENDERKIT_API_KEY": "secret-key", "SIGNED_TOKEN": "signed-value"},
            clear=False,
        ):
            root = Path(temporary)
            model = root / "model.blend"
            make_blend(model)
            runner = FakeBlenderRunner()
            result = adapter.probe_model_with_blender(
                model,
                asset_root=root,
                blender_binary="fake-blender",
                timeout_seconds=17,
                runner=runner,
            )
            self.assertEqual(result.renderable_object_count, 2)
            command = runner.commands[0]
            self.assertIn("--background", command)
            self.assertIn("--disable-autoexec", command)
            self.assertEqual(command[0], "fake-blender")
            self.assertNotIn("secret-key", " ".join(command))
            self.assertNotIn("signed-value", " ".join(command))
            self.assertNotIn("BLENDERKIT_API_KEY", runner.environments[0])
            self.assertNotIn("SIGNED_TOKEN", runner.environments[0])

    def test_probe_timeout_is_a_closed_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model.blend"
            make_blend(model)

            def timeout(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired(command, 1)

            with self.assertRaisesRegex(adapter.BlenderKitAdapterDataError, "timeout"):
                adapter.probe_model_with_blender(
                    model,
                    asset_root=root,
                    blender_binary="fake-blender",
                    timeout_seconds=1,
                    runner=timeout,
                )

    def test_run_promotes_same_catalog_asset_with_hash_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            model = assets / "BlenderKit" / "uuid" / "asset.blend"
            size, digest = make_blend(model)
            catalog = root / "catalog.csv"
            progress = root / "progress.csv"
            state_root = root / "adapter-state"
            refill_state = root / "refill-state"
            preserved = catalog_row("existing", "old", inventory_status="ready", order=1)
            write_catalog(catalog, [preserved, catalog_row("catalog-stable-id", "blenderkit-uuid")])
            signed = "https://download.invalid/file?token=do-not-log"
            write_progress(progress, [{
                "asset_id": "blenderkit-uuid",
                "local_file": str(model),
                "download_api_url": signed,
                "status": "downloaded",
            }])
            runner = FakeBlenderRunner(object_count=3)
            result = adapter.run_adapter(
                catalog_path=catalog,
                progress_csv=progress,
                asset_root=assets,
                capacity_path=assets,
                state_root=state_root,
                refill_state_root=refill_state,
                blender_binary="fake-blender",
                max_assets=1,
                promotion_batch_size=1,
                capacity_probe=good_capacity,
                blender_runner=runner,
            )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["promoted_count"], 1)
            rows = read_catalog(catalog)
            self.assertEqual(rows[0], preserved)
            promoted = rows[1]
            self.assertEqual(promoted["asset_id"], "catalog-stable-id")
            self.assertEqual(promoted["source_asset_id"], "blenderkit-uuid")
            self.assertEqual(promoted["render_order"], "2")
            self.assertEqual(promoted["inventory_status"], "ready")
            self.assertEqual(promoted["file_size"], str(size))
            self.assertEqual(promoted["content_hash"], digest)
            receipt = Path(result["receipts"][0])
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt_payload["schema"], refill.PROMOTION_SCHEMA)
            self.assertEqual(receipt_payload["assets"][0]["asset_id"], "catalog-stable-id")
            self.assertEqual(receipt_payload["assets"][0]["size_bytes"], size)
            self.assertEqual(receipt_payload["assets"][0]["sha256"], digest)
            serialized = json.dumps(result) + receipt.read_text(encoding="utf-8")
            self.assertNotIn("do-not-log", serialized)
            self.assertFalse(result["sync_triggered"])

    def test_capacity_unknown_writes_blocked_state_but_never_probes_or_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            model = assets / "model.blend"
            make_blend(model)
            catalog = root / "catalog.csv"
            progress = root / "progress.csv"
            write_catalog(catalog, [catalog_row("catalog-id", "uuid")])
            write_progress(progress, [{"asset_id": "uuid", "local_file": str(model), "status": "downloaded"}])
            before = catalog.read_bytes()
            runner = FakeBlenderRunner()

            def unknown(_path: object) -> DiskCapacity:
                raise DiskCapacityError("capacity unavailable")

            result = adapter.run_adapter(
                catalog_path=catalog,
                progress_csv=progress,
                asset_root=assets,
                capacity_path=assets,
                state_root=root / "state",
                refill_state_root=root / "refill",
                blender_binary="fake-blender",
                max_assets=1,
                capacity_probe=unknown,
                blender_runner=runner,
            )
            self.assertEqual(result["status"], "supply_blocked")
            self.assertEqual(result["promoted_count"], 0)
            self.assertEqual(runner.commands, [])
            self.assertEqual(catalog.read_bytes(), before)

    def test_catalog_generation_race_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            model = assets / "model.blend"
            make_blend(model)
            catalog = root / "catalog.csv"
            progress = root / "progress.csv"
            write_catalog(catalog, [catalog_row("catalog-id", "uuid")])
            write_progress(progress, [{"asset_id": "uuid", "local_file": str(model), "status": "downloaded"}])
            real_promote = refill.promote_verified_models
            calls = 0

            def raced(**kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise refill.SupplyConflict("catalog generation precondition failed")
                return real_promote(**kwargs)

            with mock.patch.object(adapter.refill, "promote_verified_models", side_effect=raced):
                result = adapter.run_adapter(
                    catalog_path=catalog,
                    progress_csv=progress,
                    asset_root=assets,
                    capacity_path=assets,
                    state_root=root / "state",
                    refill_state_root=root / "refill",
                    blender_binary="fake-blender",
                    max_assets=1,
                    promotion_batch_size=1,
                    generation_retries=2,
                    capacity_probe=good_capacity,
                    blender_runner=FakeBlenderRunner(),
                )
            self.assertEqual(calls, 2)
            self.assertEqual(result["promoted_count"], 1)

    def test_generation_retry_revalidates_catalog_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            model = assets / "model.blend"
            make_blend(model)
            catalog = root / "catalog.csv"
            progress = root / "progress.csv"
            write_catalog(catalog, [catalog_row("catalog-id", "uuid")])
            write_progress(progress, [{"asset_id": "uuid", "local_file": str(model), "status": "downloaded"}])
            calls = 0

            def raced(**_kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                changed = read_catalog(catalog)
                changed[0]["source_asset_id"] = "different-uuid"
                write_catalog(catalog, changed)
                raise refill.SupplyConflict("catalog generation precondition failed")

            with mock.patch.object(adapter.refill, "promote_verified_models", side_effect=raced):
                result = adapter.run_adapter(
                    catalog_path=catalog,
                    progress_csv=progress,
                    asset_root=assets,
                    capacity_path=assets,
                    state_root=root / "state",
                    refill_state_root=root / "refill",
                    blender_binary="fake-blender",
                    max_assets=1,
                    promotion_batch_size=1,
                    generation_retries=2,
                    capacity_probe=good_capacity,
                    blender_runner=FakeBlenderRunner(),
                )
            self.assertEqual(calls, 1)
            self.assertEqual(result["promoted_count"], 0)
            self.assertEqual(result["status"], "supply_blocked")
            row = read_catalog(catalog)[0]
            self.assertEqual(row["source_asset_id"], "different-uuid")
            self.assertEqual(row["render_order"], "")

    def test_singleton_lock_rejects_a_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary)
            with adapter.singleton_lock(state_root):
                with self.assertRaises(adapter.BlenderKitAdapterConflict):
                    with adapter.singleton_lock(state_root):
                        self.fail("second lock unexpectedly acquired")


if __name__ == "__main__":
    unittest.main()
