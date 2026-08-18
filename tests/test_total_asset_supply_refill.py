from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

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


def catalog_row(
    asset_id: str,
    *,
    order: int | None = None,
    inventory_status: str = "pending_download",
    source: str = "Budeco",
    download_status: str = "pending_download",
    source_root: str = "",
    model_file: str = "",
    content_hash: str = "",
) -> dict[str, str]:
    row = {field: "" for field in FIELDS}
    row.update({
        "asset_id": asset_id,
        "identity_key": f"manifest|{source}|{asset_id}",
        "record_type": "manifest_record",
        "source": source,
        "source_asset_id": f"source-{asset_id}",
        "title": f"asset {asset_id}",
        "source_root": source_root,
        "model_file": model_file,
        "model_format": Path(model_file).suffix.lower(),
        "content_hash": content_hash,
        "download_status": download_status,
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


def make_blend(path: Path) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"BLENDER-v450" + path.name.encode("utf-8") + b"\0" * 256)
    payload = path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def promotion(asset_id: str, path: Path) -> refill.Promotion:
    payload = path.read_bytes()
    return refill.Promotion(
        asset_id=asset_id,
        model_file=str(path),
        expected_size_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        verifier="blender_open_preflight.v1",
        verification_status="verified_renderable",
    )


def aggregate_placeholder(
    asset_id: str,
    ordinal: int,
    *,
    source: str = "BlenderKit",
    prefix: str = "official_access_required",
) -> dict[str, str]:
    source_asset_id = f"{prefix}#{ordinal:06d}"
    row = catalog_row(
        asset_id,
        inventory_status="indexed_only",
        source=source,
        download_status="blocked_adapter_not_configured",
    )
    row.update({
        "identity_key": f"manifest|{source}|{source_asset_id}",
        "record_type": "index_placeholder",
        "source_asset_id": source_asset_id,
        "title": f"aggregate {ordinal:06d}",
        "source_url": "https://index.invalid",
        "license_name": "aggregate-license",
        "source_category_raw": "aggregate-category",
        "source_root": "/aggregate/root",
        "file_size": "12345",
        "content_category": "待枚举",
        "technical_tags": "preserve-me",
        "asset_kind": "pending_unknown",
        "render_route": "not_renderable",
        "render_engine_hint": "source",
        "block_reason": "aggregate index has not been enumerated",
    })
    return row


def enumeration_record(
    placeholder: dict[str, str],
    source_asset_id: str,
    *,
    label: str,
) -> dict[str, str]:
    return {
        "placeholder_asset_id": placeholder["asset_id"],
        "placeholder_source_asset_id": placeholder["source_asset_id"],
        "source_asset_id": source_asset_id,
        "title": f"Exact {label}",
        "source_url": f"https://source.invalid/{source_asset_id}",
        "license_name": "CC0",
        "source_category_raw": "models",
        "source_root": f"/materialize/{source_asset_id}",
    }


def write_enumeration_receipt(
    path: Path,
    *,
    generation: str,
    source: str,
    records: list[dict[str, str]],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": refill.ENUMERATION_RECEIPT_SCHEMA,
        "expected_catalog_generation": generation,
        "source": source,
        "records": records,
    }
    payload["receipt_sha256"] = refill.enumeration_receipt_sha256(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


class SupplyRefillTests(unittest.TestCase):
    def good_capacity(self, _path: object) -> DiskCapacity:
        return DiskCapacity(3 * 1024**4, 1024**4, 2 * 1024**4, "test")

    def test_plan_selects_pending_download_in_physical_catalog_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            rows = [
                catalog_row("900", order=1, inventory_status="ready"),
                catalog_row("300", download_status="external_manual_required"),
                catalog_row("150", inventory_status="indexed_only", source="Blender GO", download_status="fetch_error"),
                catalog_row("100", source="Poly Haven", download_status="downloaded", source_root="/payload/studio.hdr"),
                catalog_row("200", source="Feishu", download_status="failed"),
            ]
            write_catalog(catalog, rows)
            plan = refill.plan_from_paths(
                catalog_path=catalog,
                capacity_path=root,
                target_assets=3,
                detail_limit=10,
                capacity_probe=self.good_capacity,
            )
            self.assertEqual([item.asset_id for item in plan.candidates], ["300", "150", "100"])
            self.assertEqual([item.catalog_position for item in plan.candidates], [2, 3, 4])
            self.assertEqual(plan.pending_download_total, 3)
            self.assertEqual(plan.indexed_only_total, 1)
            self.assertEqual(plan.selected_asset_count, 3)
            self.assertEqual(plan.supply_state, "supply_blocked")
            self.assertEqual(plan.blocked_reasons["source_authentication_or_manual_action_required"], 1)
            self.assertEqual(plan.blocked_reasons["indexed_source_refresh_required"], 1)
            self.assertEqual(plan.blocked_reasons["archive_or_hdri_requires_model_materialization"], 1)
            self.assertTrue(all(
                request.required_output_schema == refill.PROMOTION_SCHEMA
                for request in plan.adapter_requests
            ))

    def test_plan_and_status_are_strictly_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            state_root = root / "state-does-not-exist"
            write_catalog(catalog, [catalog_row("1")])
            before = catalog.read_bytes()
            plan = refill.plan_from_paths(
                catalog_path=catalog,
                capacity_path=root,
                capacity_probe=self.good_capacity,
            )
            status = refill.status_from_paths(
                catalog_path=catalog,
                capacity_path=root,
                state_root=state_root,
                capacity_probe=self.good_capacity,
            )
            self.assertTrue(plan.read_only)
            self.assertTrue(status["read_only"])
            self.assertIsNone(status["recorded"])
            self.assertFalse(state_root.exists())
            self.assertEqual(catalog.read_bytes(), before)

    def test_capacity_unknown_is_fail_closed_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            state_root = root / "state"
            write_catalog(catalog, [catalog_row("1")])
            plan = refill.plan_from_paths(
                catalog_path=catalog,
                capacity_path=root,
                capacity_probe=lambda _path: (_ for _ in ()).throw(DiskCapacityError("bad df")),
            )
            self.assertFalse(plan.capacity.known)
            self.assertFalse(plan.capacity.download_allowed)
            self.assertIn("capacity_unknown", plan.blocked_reasons)
            state_path = refill.checkpoint_plan(plan, state_root=state_root, catalog_path=catalog)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["supply_state"], "supply_blocked")
            self.assertIn("capacity_unknown", state["blocked_reasons"])
            status = refill.status_from_paths(
                catalog_path=catalog,
                capacity_path=root,
                state_root=state_root,
                capacity_probe=lambda _path: (_ for _ in ()).throw(DiskCapacityError("bad df")),
            )
            self.assertTrue(status["recorded_generation_is_current"])

    def test_generic_archives_hdris_and_disguised_archives_are_never_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in (
                ("payload.zip", b"PK\x03\x04archive"),
                ("lighting.hdr", b"#?RADIANCE\n"),
                ("disguised.blend", b"PK\x03\x04archive"),
            ):
                path = root / name
                path.write_bytes(payload)
                with self.assertRaises(refill.SupplyDataError, msg=name):
                    refill.verify_local_model(path, asset_root=root)

    def test_local_model_verification_checks_magic_and_asset_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "assets"
            root.mkdir()
            model = root / "good.blend"
            size, digest = make_blend(model)
            verified = refill.verify_local_model(model, asset_root=root)
            self.assertEqual(verified.size_bytes, size)
            self.assertEqual(verified.sha256, digest)
            outside = parent / "outside.blend"
            make_blend(outside)
            with self.assertRaises(refill.SupplyDataError):
                refill.verify_local_model(outside, asset_root=root)

    def test_promotion_updates_same_rows_and_appends_contiguous_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            state_root = root / "state"
            result_log = root / "formal_results.jsonl"
            result_log.write_text('{"asset_id":"ready","status":"accepted"}\n', encoding="utf-8")
            model_a = root / "models" / "a.blend"
            model_b = root / "models" / "b.blend"
            make_blend(model_a)
            make_blend(model_b)
            rows = [
                catalog_row("ready", order=1, inventory_status="ready", model_file=str(root / "existing.blend")),
                catalog_row("later-in-receipt", source="BlenderKit"),
                catalog_row("first-in-receipt", source="Poly Haven"),
            ]
            before_ready = dict(rows[0])
            write_catalog(catalog, rows)
            snapshot = refill.read_catalog_snapshot(catalog)
            result = refill.promote_verified_models(
                catalog_path=catalog,
                asset_root=root,
                state_root=state_root,
                expected_generation=snapshot.generation,
                promotions=[promotion("first-in-receipt", model_b), promotion("later-in-receipt", model_a)],
            )
            landed = read_catalog(catalog)
            by_id = {row["asset_id"]: row for row in landed}
            self.assertEqual(by_id["ready"], before_ready)
            self.assertEqual(by_id["later-in-receipt"]["render_order"], "2")
            self.assertEqual(by_id["first-in-receipt"]["render_order"], "3")
            self.assertEqual(by_id["later-in-receipt"]["identity_key"], "manifest|BlenderKit|later-in-receipt")
            self.assertEqual(by_id["later-in-receipt"]["inventory_status"], "ready")
            self.assertEqual(by_id["later-in-receipt"]["download_status"], "downloaded")
            self.assertEqual(result["promoted_count"], 2)
            self.assertNotEqual(result["catalog_generation_before"], result["catalog_generation_after"])
            self.assertEqual(result_log.read_text(encoding="utf-8"), '{"asset_id":"ready","status":"accepted"}\n')

    def test_promotion_completes_next_formal_batch_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            rows = [
                catalog_row(f"ready-{order:04d}", order=order, inventory_status="ready")
                for order in range(1, 1001)
            ]
            rows.append(catalog_row("pending"))
            write_catalog(catalog, rows)
            model = root / "model.blend"
            make_blend(model)
            snapshot = refill.read_catalog_snapshot(catalog)
            refill.promote_verified_models(
                catalog_path=catalog,
                asset_root=root,
                state_root=root / "state",
                expected_generation=snapshot.generation,
                promotions=[promotion("pending", model)],
            )
            promoted = read_catalog(catalog)[-1]
            self.assertEqual(promoted["render_order"], "1001")
            self.assertEqual(promoted["render_batch"], "0001")

    def test_stale_generation_and_hash_mismatch_leave_catalog_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            model = root / "model.blend"
            make_blend(model)
            write_catalog(catalog, [catalog_row("1")])
            before = catalog.read_bytes()
            with self.assertRaises(refill.SupplyConflict):
                refill.promote_verified_models(
                    catalog_path=catalog,
                    asset_root=root,
                    state_root=root / "state",
                    expected_generation="0" * 64,
                    promotions=[promotion("1", model)],
                )
            self.assertEqual(catalog.read_bytes(), before)
            bad = promotion("1", model)
            bad = refill.Promotion(
                bad.asset_id, bad.model_file, bad.expected_size_bytes,
                "f" * 64, bad.verifier, bad.verification_status,
            )
            generation = refill.read_catalog_snapshot(catalog).generation
            with self.assertRaises(refill.SupplyConflict):
                refill.promote_verified_models(
                    catalog_path=catalog,
                    asset_root=root,
                    state_root=root / "state",
                    expected_generation=generation,
                    promotions=[bad],
                )
            self.assertEqual(catalog.read_bytes(), before)

    def test_duplicate_content_cannot_be_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            existing = root / "existing.blend"
            make_blend(existing)
            duplicate = root / "copy.blend"
            duplicate.write_bytes(existing.read_bytes())
            digest = hashlib.sha256(existing.read_bytes()).hexdigest()
            write_catalog(catalog, [
                catalog_row("ready", order=1, inventory_status="ready", model_file=str(existing), content_hash=digest),
                catalog_row("pending"),
            ])
            before = catalog.read_bytes()
            with self.assertRaises(refill.SupplyConflict):
                refill.promote_verified_models(
                    catalog_path=catalog,
                    asset_root=root,
                    state_root=root / "state",
                    expected_generation=refill.read_catalog_snapshot(catalog).generation,
                    promotions=[promotion("pending", duplicate)],
                )
            self.assertEqual(catalog.read_bytes(), before)

    def test_receipts_require_known_schema_and_trusted_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"schema": "unknown", "assets": []}), encoding="utf-8")
            with self.assertRaises(refill.SupplyDataError):
                refill.read_promotion_receipts(receipt)
            receipt.write_text(json.dumps({
                "schema": refill.PROMOTION_SCHEMA,
                "assets": [{
                    "asset_id": "1", "model_file": "/tmp/a.blend", "size_bytes": 10,
                    "sha256": "a" * 64, "verifier": "title_keyword.v1",
                    "verification_status": "verified_renderable",
                }],
            }), encoding="utf-8")
            with self.assertRaises(refill.SupplyDataError):
                refill.read_promotion_receipts(receipt)

    def test_catalog_holes_duplicates_and_changed_generation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            rows = [catalog_row("a", order=1, inventory_status="ready"), catalog_row("b", order=3, inventory_status="ready")]
            write_catalog(catalog, rows)
            with self.assertRaises(refill.SupplyDataError):
                refill.read_catalog_snapshot(catalog)
            rows[1]["render_order"] = "1"
            rows[1]["render_batch"] = "0000"
            write_catalog(catalog, rows)
            with self.assertRaises(refill.SupplyDataError):
                refill.read_catalog_snapshot(catalog)

    def test_duplicate_placeholder_identity_is_visible_but_cannot_be_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            model = root / "model.blend"
            make_blend(model)
            rows = [catalog_row("a"), catalog_row("b")]
            rows[1]["identity_key"] = rows[0]["identity_key"]
            write_catalog(catalog, rows)
            snapshot = refill.read_catalog_snapshot(catalog)
            plan = refill.build_supply_plan(
                snapshot,
                capacity=refill.build_capacity_status(root, probe=self.good_capacity),
            )
            self.assertEqual(
                [candidate.blocked_reason for candidate in plan.candidates],
                ["identity_key_is_not_unique", "identity_key_is_not_unique"],
            )
            with self.assertRaises(refill.SupplyConflict):
                refill.promote_verified_models(
                    catalog_path=catalog,
                    asset_root=root,
                    state_root=root / "state",
                    expected_generation=snapshot.generation,
                    promotions=[promotion("a", model)],
                )

    def test_singleton_mutation_lock_rejects_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            with refill.singleton_lock(state_root):
                with self.assertRaises(refill.SupplyConflict):
                    with refill.singleton_lock(state_root):
                        self.fail("second owner acquired singleton lock")

    def test_explicit_enumeration_preserves_identity_results_and_non_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            result_log = root / "formal_results.jsonl"
            result_log.write_text(
                '{"asset_id":"ready","render_order":1,"status":"accepted"}\n',
                encoding="utf-8",
            )
            ready = catalog_row("ready", order=1, inventory_status="ready")
            first = aggregate_placeholder("placeholder-a", 1)
            second = aggregate_placeholder("placeholder-b", 2)
            unrelated = catalog_row(
                "unrelated",
                inventory_status="indexed_only",
                source="Budeco",
            )
            rows = [ready, first, second, unrelated]
            write_catalog(catalog, rows)
            before = read_catalog(catalog)
            snapshot = refill.read_catalog_snapshot(catalog)
            receipt_path = root / "enumeration.json"
            write_enumeration_receipt(
                receipt_path,
                generation=snapshot.generation,
                source="BlenderKit",
                records=[
                    enumeration_record(first, "exact-source-a", label="A"),
                    enumeration_record(second, "exact-source-b", label="B"),
                ],
            )
            receipt = refill.read_enumeration_receipt(receipt_path)
            result = refill.enumerate_aggregate_placeholders(
                catalog_path=catalog,
                state_root=root / "state",
                expected_generation=snapshot.generation,
                receipt=receipt,
            )
            landed = read_catalog(catalog)
            self.assertEqual(landed[0], before[0])
            self.assertEqual(landed[3], before[3])
            self.assertEqual(result["schema"], refill.ENUMERATION_RESULT_SCHEMA)
            self.assertEqual(result["enumerated_count"], 2)
            self.assertFalse(result["downloads_started"])
            for old, new, expected_source_id in (
                (before[1], landed[1], "exact-source-a"),
                (before[2], landed[2], "exact-source-b"),
            ):
                self.assertEqual(new["asset_id"], old["asset_id"])
                self.assertEqual(new["identity_key"], old["identity_key"])
                self.assertEqual(new["render_order"], old["render_order"])
                self.assertEqual(new["render_batch"], old["render_batch"])
                self.assertEqual(new["source_asset_id"], expected_source_id)
                self.assertEqual(new["record_type"], "manifest_record")
                self.assertEqual(new["inventory_status"], "pending_download")
                self.assertEqual(new["download_status"], "pending_download")
                self.assertEqual(new["block_reason"], refill.ENUMERATED_PENDING_REASON)
                # Conservative classification/model data are not inferred from
                # source titles during the identity-only transaction.
                for field in (
                    "model_file",
                    "model_format",
                    "file_size",
                    "content_category",
                    "technical_tags",
                    "asset_kind",
                    "render_route",
                    "render_engine_hint",
                    "content_hash",
                    "duplicate_of",
                    "updated_at",
                ):
                    self.assertEqual(new[field], old[field], field)
            self.assertEqual(
                result_log.read_text(encoding="utf-8"),
                '{"asset_id":"ready","render_order":1,"status":"accepted"}\n',
            )

    def test_enumeration_receipt_hash_schema_and_duplicates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            placeholder = aggregate_placeholder("placeholder", 1)
            write_catalog(catalog, [placeholder])
            before = catalog.read_bytes()
            generation = refill.read_catalog_snapshot(catalog).generation
            receipt_path = root / "enumeration.json"
            payload = write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[enumeration_record(placeholder, "source-uuid", label="one")],
            )
            payload["records"][0]["title"] = "tampered"  # type: ignore[index]
            receipt_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(refill.SupplyConflict, "hash mismatch"):
                refill.read_enumeration_receipt(receipt_path)
            self.assertEqual(catalog.read_bytes(), before)

            duplicate_key = (
                '{"schema":"%s","schema":"%s",'
                '"expected_catalog_generation":"%s","source":"BlenderKit",'
                '"records":[],"receipt_sha256":"%s"}'
                % (
                    refill.ENUMERATION_RECEIPT_SCHEMA,
                    refill.ENUMERATION_RECEIPT_SCHEMA,
                    generation,
                    "0" * 64,
                )
            )
            receipt_path.write_text(duplicate_key, encoding="utf-8")
            with self.assertRaisesRegex(refill.SupplyDataError, "duplicate JSON key"):
                refill.read_enumeration_receipt(receipt_path)

            duplicate_records = [
                enumeration_record(placeholder, "same-uuid", label="one"),
                enumeration_record(
                    {**placeholder, "asset_id": "placeholder-other", "source_asset_id": "official_access_required#000002"},
                    "same-uuid",
                    label="two",
                ),
            ]
            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=duplicate_records,
            )
            with self.assertRaisesRegex(refill.SupplyDataError, "duplicate logical source ID"):
                refill.read_enumeration_receipt(receipt_path)
            self.assertEqual(catalog.read_bytes(), before)

    def test_enumeration_stale_existing_id_and_wrong_order_never_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            existing = catalog_row("existing", inventory_status="pending_download", source="BlenderKit")
            existing["source_asset_id"] = "already-present"
            existing["identity_key"] = "manifest|BlenderKit|already-present"
            first = aggregate_placeholder("placeholder-a", 1)
            second = aggregate_placeholder("placeholder-b", 2)
            write_catalog(catalog, [existing, first, second])
            before = catalog.read_bytes()
            generation = refill.read_catalog_snapshot(catalog).generation
            receipt_path = root / "enumeration.json"

            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[enumeration_record(first, "already-present", label="one")],
            )
            with self.assertRaisesRegex(refill.SupplyConflict, "already exists"):
                refill.enumerate_aggregate_placeholders(
                    catalog_path=catalog,
                    state_root=root / "state",
                    expected_generation=generation,
                    receipt=refill.read_enumeration_receipt(receipt_path),
                )
            self.assertEqual(catalog.read_bytes(), before)

            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[
                    enumeration_record(second, "new-b", label="two"),
                    enumeration_record(first, "new-a", label="one"),
                ],
            )
            with self.assertRaisesRegex(refill.SupplyDataError, "catalog order"):
                refill.enumerate_aggregate_placeholders(
                    catalog_path=catalog,
                    state_root=root / "state",
                    expected_generation=generation,
                    receipt=refill.read_enumeration_receipt(receipt_path),
                )
            self.assertEqual(catalog.read_bytes(), before)

            valid_payload = write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[enumeration_record(first, "new-a", label="one")],
            )
            valid_receipt = refill.read_enumeration_receipt(receipt_path)
            with self.assertRaisesRegex(refill.SupplyConflict, "caller precondition"):
                refill.enumerate_aggregate_placeholders(
                    catalog_path=catalog,
                    state_root=root / "state",
                    expected_generation="f" * 64,
                    receipt=valid_receipt,
                )
            self.assertEqual(valid_payload["expected_catalog_generation"], generation)
            self.assertEqual(catalog.read_bytes(), before)

    def test_aggregate_supply_plan_requests_enumeration_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            write_catalog(catalog, [aggregate_placeholder("placeholder", 1)])
            plan = refill.plan_from_paths(
                catalog_path=catalog,
                capacity_path=root,
                capacity_probe=self.good_capacity,
            )
            self.assertEqual(
                plan.candidates[0].blocked_reason,
                "aggregate_placeholder_enumeration_required",
            )
            self.assertEqual(
                plan.adapter_requests[0].required_output_schema,
                refill.ENUMERATION_RECEIPT_SCHEMA,
            )

    def test_enumeration_requires_exact_untouched_source_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            receipt_path = root / "enumeration.json"
            placeholder = aggregate_placeholder("placeholder", 1)
            placeholder["record_type"] = "manifest_record"
            write_catalog(catalog, [placeholder])
            before = catalog.read_bytes()
            generation = refill.read_catalog_snapshot(catalog).generation
            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[enumeration_record(placeholder, "exact-uuid", label="one")],
            )
            with self.assertRaisesRegex(refill.SupplyConflict, "not an untouched"):
                refill.enumerate_aggregate_placeholders(
                    catalog_path=catalog,
                    state_root=root / "state",
                    expected_generation=generation,
                    receipt=refill.read_enumeration_receipt(receipt_path),
                )
            self.assertEqual(catalog.read_bytes(), before)

            placeholder["record_type"] = "index_placeholder"
            write_catalog(catalog, [placeholder])
            before = catalog.read_bytes()
            generation = refill.read_catalog_snapshot(catalog).generation
            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="Aplaybox",
                records=[enumeration_record(placeholder, "exact-uuid", label="one")],
            )
            with self.assertRaisesRegex(refill.SupplyConflict, "another source"):
                refill.enumerate_aggregate_placeholders(
                    catalog_path=catalog,
                    state_root=root / "state",
                    expected_generation=generation,
                    receipt=refill.read_enumeration_receipt(receipt_path),
                )
            self.assertEqual(catalog.read_bytes(), before)

    def test_enumeration_rejects_credential_url_and_concurrent_catalog_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            receipt_path = root / "enumeration.json"
            placeholder = aggregate_placeholder("placeholder", 1)
            write_catalog(catalog, [placeholder])
            before = catalog.read_bytes()
            generation = refill.read_catalog_snapshot(catalog).generation
            signed = enumeration_record(placeholder, "exact-uuid", label="one")
            signed["source_url"] = "https://source.invalid/model?X-Amz-Signature=secret"
            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[signed],
            )
            with self.assertRaisesRegex(refill.SupplyDataError, "credential material"):
                refill.read_enumeration_receipt(receipt_path)
            self.assertEqual(catalog.read_bytes(), before)

            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[enumeration_record(placeholder, "exact-uuid", label="one")],
            )
            receipt = refill.read_enumeration_receipt(receipt_path)
            real_read = refill._read_catalog_bytes
            reads = 0

            def catalog_race(path: Path) -> bytes:
                nonlocal reads
                reads += 1
                payload = real_read(path)
                # The snapshot requires two identical reads.  Present a new
                # generation only at the final compare-and-swap boundary.
                return payload if reads <= 2 else payload + b"concurrent-change"

            with mock.patch.object(refill, "_read_catalog_bytes", side_effect=catalog_race):
                with self.assertRaisesRegex(refill.SupplyConflict, "atomic replacement"):
                    refill.enumerate_aggregate_placeholders(
                        catalog_path=catalog,
                        state_root=root / "state",
                        expected_generation=generation,
                        receipt=receipt,
                    )
            self.assertEqual(catalog.read_bytes(), before)

    def test_enumeration_rejects_preexisting_duplicate_logical_source_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "catalog.csv"
            receipt_path = root / "enumeration.json"
            placeholder = aggregate_placeholder("placeholder", 1)
            first = catalog_row(
                "existing-a", inventory_status="pending_download", source="BlenderKit"
            )
            second = catalog_row(
                "existing-b", inventory_status="pending_download", source="BlenderKit"
            )
            first["source_asset_id"] = second["source_asset_id"] = "duplicate-source-id"
            write_catalog(catalog, [placeholder, first, second])
            before = catalog.read_bytes()
            generation = refill.read_catalog_snapshot(catalog).generation
            write_enumeration_receipt(
                receipt_path,
                generation=generation,
                source="BlenderKit",
                records=[enumeration_record(placeholder, "new-source-id", label="one")],
            )
            with self.assertRaisesRegex(refill.SupplyConflict, "duplicate logical"):
                refill.enumerate_aggregate_placeholders(
                    catalog_path=catalog,
                    state_root=root / "state",
                    expected_generation=generation,
                    receipt=refill.read_enumeration_receipt(receipt_path),
                )
            self.assertEqual(catalog.read_bytes(), before)

if __name__ == "__main__":
    unittest.main()
