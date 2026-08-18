from __future__ import annotations

import csv
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_budeco_materializer as materializer
import total_asset_supply_refill as refill


FIELDS = [
    "asset_id", "identity_key", "record_type", "source", "source_asset_id",
    "title", "source_url", "license_name", "source_category_raw", "source_root",
    "model_file", "model_format", "file_size", "content_category",
    "technical_tags", "asset_kind", "render_route", "render_engine_hint",
    "content_hash", "duplicate_of", "download_status", "inventory_status",
    "block_reason", "render_order", "render_batch", "updated_at",
]


def catalog_row(asset_id: str, source_root: Path) -> dict[str, str]:
    row = {field: "" for field in FIELDS}
    row.update({
        "asset_id": asset_id,
        "identity_key": f"manifest|Budeco|source-{asset_id}",
        "record_type": "manifest_record",
        "source": "Budeco",
        "source_asset_id": f"source-{asset_id}",
        "title": f"asset {asset_id}",
        "source_root": str(source_root),
        "download_status": "downloaded",
        "inventory_status": "pending_download",
        "updated_at": "2026-07-21 00:00:00",
    })
    return row


def write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def blend_bytes(label: str = "scene") -> bytes:
    return b"BLENDER-v450" + label.encode("utf-8") + bytes(range(256)) * 2


def write_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, payload in members:
            handle.writestr(name, payload)


def write_legacy_zip(path: Path, raw_name: bytes, payload: bytes) -> None:
    """Write one legacy ZIP member whose filename lacks the UTF-8 flag."""

    placeholder = b"A" * len(raw_name)
    write_zip(path, [(placeholder.decode("ascii"), payload)])
    archive = path.read_bytes()
    if archive.count(placeholder) != 2:
        raise AssertionError("test ZIP does not have exact local/central names")
    path.write_bytes(archive.replace(placeholder, raw_name))


def appledouble_bytes() -> bytes:
    finder_info = b"\0" * 32
    resource_fork = b"\0" * 16
    table_end = 26 + 2 * 12
    return b"".join([
        materializer.APPLEDOUBLE_MAGIC,
        materializer.APPLEDOUBLE_VERSION,
        b"Mac OS X        ",
        (2).to_bytes(2, "big"),
        (9).to_bytes(4, "big"),
        table_end.to_bytes(4, "big"),
        len(finder_info).to_bytes(4, "big"),
        (2).to_bytes(4, "big"),
        (table_end + len(finder_info)).to_bytes(4, "big"),
        len(resource_fork).to_bytes(4, "big"),
        finder_info,
        resource_fork,
    ])


def make_binary(path: Path) -> str:
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeBlenderRunner:
    def __init__(self, object_count: int = 3) -> None:
        self.object_count = object_count
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        payload = {
            "schema": materializer.blender_preflight.PROBE_SCHEMA,
            "status": "verified_renderable",
            "renderable_object_count": self.object_count,
        }
        return subprocess.CompletedProcess(
            command,
            0 if self.object_count else 41,
            stdout=(
                materializer.blender_preflight.PROBE_MARKER
                + json.dumps(payload)
                + "\n"
            ),
            stderr="",
        )


class BudecoMaterializerTests(unittest.TestCase):
    def limits(self, **overrides: object) -> materializer.ArchiveLimits:
        values: dict[str, object] = {
            "max_members": 100,
            "max_archive_bytes": 16 * 1024**2,
            "max_unpacked_bytes": 8 * 1024**2,
            "max_member_bytes": 4 * 1024**2,
            "max_compression_ratio": 1000.0,
            "timeout_seconds": 10,
        }
        values.update(overrides)
        return materializer.ArchiveLimits(**values)  # type: ignore[arg-type]

    def test_single_model_materializes_atomic_receipt_without_catalog_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            source = assets / "budeco" / "category" / "source-1"
            archive = source / "payload.zip"
            write_zip(archive, [
                ("package/scene.blend", blend_bytes()),
                ("package/textures/readme.txt", b"texture metadata"),
            ])
            catalog = root / "catalog.csv"
            write_catalog(catalog, [catalog_row("000001", source)])
            generation = refill.read_catalog_snapshot(catalog).generation
            before = catalog.read_bytes()
            binary = root / "pinned-blender"
            binary_sha = make_binary(binary)
            runner = FakeBlenderRunner()

            result = materializer.materialize_single_model(
                catalog_path=catalog,
                asset_root=assets,
                materialized_root=assets / "_materialized" / "budeco-v1",
                state_root=root / "state",
                asset_id="000001",
                expected_generation=generation,
                pin=materializer.PinnedPreflight(binary, binary_sha, 10),
                limits=self.limits(),
                blender_runner=runner,
            )

            self.assertEqual(catalog.read_bytes(), before)
            self.assertFalse(result.catalog_mutated)
            self.assertTrue(Path(result.model_file).is_file())
            self.assertEqual(Path(result.model_file).read_bytes(), blend_bytes())
            self.assertFalse(
                (Path(result.output_dir) / "_archive_input").exists()
            )
            self.assertEqual(runner.commands[0][0], str(binary))
            receipt_path = Path(result.receipt_path)
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            materializer.validate_materialization_receipt(
                payload, expected_generation=generation
            )
            self.assertEqual(
                materializer.read_materialization_receipt(
                    receipt_path, expected_generation=generation
                )["expected_catalog_generation"],
                generation,
            )
            promotions = refill.read_promotion_receipts(receipt_path)
            self.assertEqual(len(promotions), 1)
            self.assertEqual(promotions[0].asset_id, "000001")
            self.assertEqual(promotions[0].expected_sha256, result.model_sha256)
            self.assertEqual(
                payload["assets"][0]["archive"]["sha256"],
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                payload["assets"][0]["preflight"]["binary_sha256"],
                binary_sha,
            )

    def test_inspect_is_read_only_and_requires_exact_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            source = assets / "budeco" / "one"
            archive = source / "asset.zip"
            write_zip(archive, [("scene.blend", blend_bytes())])
            catalog = root / "catalog.csv"
            write_catalog(catalog, [catalog_row("a", source)])
            before = catalog.read_bytes()
            generation = refill.read_catalog_snapshot(catalog).generation
            candidate, manifest = materializer.inspect_single_model_candidate(
                catalog_path=catalog,
                asset_root=assets,
                asset_id="a",
                expected_generation=generation,
                limits=self.limits(),
            )
            self.assertEqual(candidate.archive_path, archive.resolve())
            self.assertEqual(len(manifest.model_members), 1)
            self.assertEqual(catalog.read_bytes(), before)
            with self.assertRaises(materializer.BudecoMaterializerConflict):
                materializer.inspect_single_model_candidate(
                    catalog_path=catalog,
                    asset_root=assets,
                    asset_id="a",
                    expected_generation="0" * 64,
                    limits=self.limits(),
                )
            self.assertEqual(catalog.read_bytes(), before)

    def test_path_traversal_is_rejected_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "traversal.zip"
            write_zip(archive, [("../escape.blend", blend_bytes())])
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "traversal"
            ):
                materializer.inspect_archive(archive, limits=self.limits())
            self.assertFalse((Path(temporary).parent / "escape.blend").exists())

    def test_legacy_chinese_zip_name_survives_strict_appledouble_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "legacy.zip"
            raw_name = "躺倒鸭.blend".encode("gbk")
            write_legacy_zip(archive, raw_name, blend_bytes("legacy"))
            manifest = materializer.inspect_archive(
                archive, limits=self.limits()
            )
            expected_name = raw_name.decode("cp437")
            self.assertEqual(manifest.model_members[0].relative_path, expected_name)
            destination = root / "payload"
            original_extract = materializer._extract_zip

            def extract_with_appledouble(
                archive_path: Path,
                output: Path,
                archive_manifest: materializer.ArchiveManifest,
                *,
                limits: materializer.ArchiveLimits,
            ) -> None:
                original_extract(
                    archive_path, output, archive_manifest, limits=limits
                )
                model = output / archive_manifest.model_members[0].relative_path
                model.with_name(f"._{model.name}").write_bytes(
                    appledouble_bytes()
                )

            with mock.patch.object(
                materializer,
                "_extract_zip",
                side_effect=extract_with_appledouble,
            ):
                materializer.extract_archive(
                    archive,
                    destination,
                    manifest,
                    limits=self.limits(),
                )

            model = destination / expected_name
            self.assertEqual(model.read_bytes(), blend_bytes("legacy"))
            self.assertFalse(model.with_name(f"._{model.name}").exists())

    def test_unicode_normalization_is_canonical_and_collisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            decomposed = "models/cafe\u0301.blend"
            archive = root / "normalized.zip"
            write_zip(archive, [(decomposed, blend_bytes("normalized"))])
            manifest = materializer.inspect_archive(
                archive, limits=self.limits()
            )
            composed = unicodedata.normalize("NFC", decomposed)
            self.assertEqual(manifest.model_members[0].relative_path, composed)
            destination = root / "payload"
            materializer.extract_archive(
                archive,
                destination,
                manifest,
                limits=self.limits(),
            )
            self.assertEqual(
                (destination / composed).read_bytes(),
                blend_bytes("normalized"),
            )

            collision = root / "collision.zip"
            write_zip(collision, [
                ("café.blend", blend_bytes("composed")),
                ("cafe\u0301.blend", blend_bytes("decomposed")),
            ])
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "colliding"
            ):
                materializer.inspect_archive(collision, limits=self.limits())

    def test_extracted_member_drift_and_forged_appledouble_fail_closed(self) -> None:
        for extra_name, extra_payload in (
            ("unexpected.txt", b"drift"),
            ("._scene.blend", b"not-valid-appledouble"),
        ):
            with self.subTest(extra_name=extra_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "asset.zip"
                write_zip(archive, [("scene.blend", blend_bytes())])
                manifest = materializer.inspect_archive(
                    archive, limits=self.limits()
                )
                destination = root / "payload"
                original_extract = materializer._extract_zip

                def extract_with_drift(
                    archive_path: Path,
                    output: Path,
                    archive_manifest: materializer.ArchiveManifest,
                    *,
                    limits: materializer.ArchiveLimits,
                ) -> None:
                    original_extract(
                        archive_path, output, archive_manifest, limits=limits
                    )
                    (output / extra_name).write_bytes(extra_payload)

                with mock.patch.object(
                    materializer,
                    "_extract_zip",
                    side_effect=extract_with_drift,
                ):
                    with self.assertRaisesRegex(
                        materializer.BudecoMaterializerConflict,
                        "members do not exactly match",
                    ):
                        materializer.extract_archive(
                            archive,
                            destination,
                            manifest,
                            limits=self.limits(),
                        )
                self.assertFalse(destination.exists())

    def test_zip_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "symlink.zip"
            info = zipfile.ZipInfo("scene.blend")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(info, "../outside")
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "symlink"
            ):
                materializer.inspect_archive(archive, limits=self.limits())

    def test_encrypted_zip_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "encrypted.zip"
            write_zip(archive, [("scene.blend", blend_bytes())])
            payload = bytearray(archive.read_bytes())
            local = payload.index(b"PK\x03\x04")
            central = payload.index(b"PK\x01\x02")
            local_flags = int.from_bytes(payload[local + 6:local + 8], "little") | 1
            central_flags = int.from_bytes(
                payload[central + 8:central + 10], "little"
            ) | 1
            payload[local + 6:local + 8] = local_flags.to_bytes(2, "little")
            payload[central + 8:central + 10] = central_flags.to_bytes(2, "little")
            archive.write_bytes(payload)
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "encrypted"
            ):
                materializer.inspect_archive(archive, limits=self.limits())

    def test_archive_bomb_limits_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bomb.zip"
            write_zip(archive, [("scene.blend", b"BLENDER" + b"\0" * 100_000)])
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError,
                "compression ratio|size limit",
            ):
                materializer.inspect_archive(
                    archive,
                    limits=self.limits(max_compression_ratio=2.0),
                )

    def test_multiple_models_abstain_from_single_model_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            source = assets / "budeco" / "multi"
            write_zip(source / "multi.zip", [
                ("a.blend", blend_bytes("a")),
                ("b.blend", blend_bytes("b")),
            ])
            catalog = root / "catalog.csv"
            write_catalog(catalog, [catalog_row("multi", source)])
            generation = refill.read_catalog_snapshot(catalog).generation
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "exactly one"
            ):
                materializer.inspect_single_model_candidate(
                    catalog_path=catalog,
                    asset_root=assets,
                    asset_id="multi",
                    expected_generation=generation,
                    limits=self.limits(),
                )

    def test_casefold_collisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "collision.zip"
            write_zip(archive, [
                ("Scene.blend", blend_bytes("a")),
                ("scene.blend", blend_bytes("b")),
            ])
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "colliding"
            ):
                materializer.inspect_archive(archive, limits=self.limits())

    def test_external_archive_link_listing_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "asset.rar"
            archive.write_bytes(b"not-used-by-fake-runner")
            binary = root / "bsdtar"
            make_binary(binary)

            def runner(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        "lrwxr-xr-x  0 0 0 7 Jul 21 00:00 "
                        "scene.blend -> ../outside\n"
                    ),
                    stderr="",
                )

            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "links"
            ):
                materializer.inspect_archive(
                    archive,
                    limits=self.limits(),
                    bsdtar_binary=binary,
                    runner=runner,
                )

    def test_archive_digest_change_removes_partial_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            archive = source_root / "asset.zip"
            destination = root / "capture" / "asset.zip"
            write_zip(archive, [("scene.blend", blend_bytes())])
            fingerprint = materializer._regular_fingerprint(archive)
            with mock.patch.object(
                materializer,
                "_hash_stable_file",
                return_value=("f" * 64, fingerprint),
            ):
                with self.assertRaisesRegex(
                    materializer.BudecoMaterializerConflict, "verification reads"
                ):
                    materializer.capture_stable_archive(
                        archive,
                        destination,
                        source_root=source_root,
                        limits=self.limits(),
                    )
            self.assertFalse(destination.exists())

    def test_pinned_binary_mismatch_fails_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            source = assets / "budeco" / "one"
            write_zip(source / "asset.zip", [("scene.blend", blend_bytes())])
            catalog = root / "catalog.csv"
            write_catalog(catalog, [catalog_row("one", source)])
            generation = refill.read_catalog_snapshot(catalog).generation
            binary = root / "blender"
            make_binary(binary)
            output = assets / "materialized"
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerConflict, "digest mismatch"
            ):
                materializer.materialize_single_model(
                    catalog_path=catalog,
                    asset_root=assets,
                    materialized_root=output,
                    state_root=root / "state",
                    asset_id="one",
                    expected_generation=generation,
                    pin=materializer.PinnedPreflight(binary, "f" * 64),
                    limits=self.limits(),
                    blender_runner=FakeBlenderRunner(),
                )
            self.assertFalse(output.exists())

    def test_catalog_generation_change_before_commit_leaves_no_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            source = assets / "budeco" / "one"
            write_zip(source / "asset.zip", [("scene.blend", blend_bytes())])
            catalog = root / "catalog.csv"
            write_catalog(catalog, [catalog_row("one", source)])
            snapshot = refill.read_catalog_snapshot(catalog)
            changed = refill.CatalogSnapshot(
                snapshot.raw_bytes,
                "f" * 64,
                snapshot.fieldnames,
                snapshot.rows,
            )
            binary = root / "blender"
            binary_sha = make_binary(binary)
            output = assets / "materialized"
            with mock.patch.object(
                materializer.refill,
                "read_catalog_snapshot",
                side_effect=[snapshot, snapshot, changed],
            ):
                with self.assertRaisesRegex(
                    materializer.BudecoMaterializerConflict,
                    "changed before materialization commit",
                ):
                    materializer.materialize_single_model(
                        catalog_path=catalog,
                        asset_root=assets,
                        materialized_root=output,
                        state_root=root / "state",
                        asset_id="one",
                        expected_generation=snapshot.generation,
                        pin=materializer.PinnedPreflight(binary, binary_sha),
                        limits=self.limits(),
                        blender_runner=FakeBlenderRunner(),
                    )
            self.assertFalse((output / "one").exists())

    def test_strict_receipt_rejects_unknown_fields(self) -> None:
        payload = {
            "schema": refill.PROMOTION_SCHEMA,
            "adapter_id": materializer.ADAPTER_ID,
            "expected_catalog_generation": "a" * 64,
            "created_at": "2026-07-21 00:00:00",
            "assets": [],
            "unexpected": True,
        }
        with self.assertRaisesRegex(
            materializer.BudecoMaterializerDataError, "top-level fields"
        ):
            materializer.validate_materialization_receipt(
                payload, expected_generation="a" * 64
            )

    def test_receipt_reader_rejects_duplicate_keys_and_stale_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            receipt.write_text(
                '{"schema":"%s","schema":"%s"}'
                % (refill.PROMOTION_SCHEMA, refill.PROMOTION_SCHEMA),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerDataError, "duplicate JSON key"
            ):
                materializer.read_materialization_receipt(
                    receipt, expected_generation="a" * 64
                )

            payload = {
                "schema": refill.PROMOTION_SCHEMA,
                "adapter_id": materializer.ADAPTER_ID,
                "expected_catalog_generation": "a" * 64,
                "created_at": "2026-07-21 00:00:00",
                "assets": [],
            }
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                materializer.BudecoMaterializerConflict, "generation is stale"
            ):
                materializer.read_materialization_receipt(
                    receipt, expected_generation="b" * 64
                )


if __name__ == "__main__":
    unittest.main()
