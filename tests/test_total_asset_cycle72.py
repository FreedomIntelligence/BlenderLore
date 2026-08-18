from __future__ import annotations

import csv
import hashlib
import json
import os
import select
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import total_asset_cycle72 as cycle72  # noqa: E402
import total_asset_authority_parse_cache as authority_cache  # noqa: E402


FIELDS = [
    "asset_id",
    "identity_key",
    "model_file",
    "model_format",
    "render_engine_hint",
    "inventory_status",
    "duplicate_of",
    "render_order",
    "render_batch",
    "file_size",
]


class TotalAssetCycle72Tests(unittest.TestCase):
    def setUp(self) -> None:
        cycle72._close_authority_fd_cache()
        cycle72._clear_runtime_incompatibility_cache()
        cycle72._cached_source_runtime_family.cache_clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.inventory.mkdir()
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.state = self.root / "local-state"
        self.db = self.state / "leases.sqlite3"
        self.manifest = self.state / "production_cycle.json"
        self.slots = cycle72.canonical_slots()

    def tearDown(self) -> None:
        cycle72._close_authority_fd_cache()
        cycle72._clear_runtime_incompatibility_cache()
        cycle72._cached_source_runtime_family.cache_clear()
        self.temporary.cleanup()

    def test_authority_generation_and_cache_keys_are_lexical_not_resolved(self) -> None:
        path = self.inventory / "total_asset_render_status_batch0003.jsonl"
        identity = (1, 2, 3, 4, 5)
        with mock.patch.object(
            Path,
            "resolve",
            side_effect=AssertionError("authority hot path must not resolve files"),
        ):
            generation = cycle72._authority_generation_from_identities(
                ((path, identity),)
            )
            key = cycle72._authority_cache_path_key(path)
        self.assertEqual(len(generation), 64)
        self.assertTrue(key.is_absolute())
        self.assertEqual(key, path)

    def test_authority_snapshot_scans_once_then_reuses_directory_identity(self) -> None:
        self.write_catalog(self.rows(2))
        self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        real_scandir = cycle72.os.scandir

        class InodeOnlyEntry:
            def __init__(self, entry: os.DirEntry[str]) -> None:
                self.name = entry.name
                # Deliberately make this unlike fstat().st_ino.  ExFAT exposes
                # distinct inode domains and the implementation must not
                # compare them numerically.
                self._inode = int(entry.inode()) + 10_000_000

            def inode(self) -> int:
                return self._inode

            def stat(self, **_kwargs: object) -> object:
                raise AssertionError("authority scan must not call DirEntry.stat")

        class InodeOnlyScandir:
            def __init__(self, path: Path) -> None:
                with real_scandir(path) as iterator:
                    self._entries = tuple(InodeOnlyEntry(entry) for entry in iterator)

            def __enter__(self) -> "InodeOnlyScandir":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def __iter__(self) -> object:
                return iter(self._entries)

        with (
            mock.patch.object(
                cycle72.os,
                "scandir",
                side_effect=InodeOnlyScandir,
            ) as scandir,
            mock.patch.object(
                Path,
                "stat",
                side_effect=AssertionError(
                    "authority snapshot must reuse DirEntry identities"
                ),
            ),
            mock.patch.object(
                Path,
                "resolve",
                side_effect=AssertionError(
                    "authority snapshot must use lexical absolute paths"
                ),
            ),
        ):
            snapshot = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
            repeated = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )

        self.assertEqual(scandir.call_count, 1)
        self.assertEqual(
            snapshot.effective_statuses["asset-0001"]["status"], "accepted"
        )
        self.assertEqual(repeated.generation, snapshot.generation)

    def test_authority_snapshot_rejects_status_symlink(self) -> None:
        self.write_catalog(self.rows(2))
        target = self.root / "status-target.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        status = self.inventory / "total_asset_render_status_batch0000_link.jsonl"
        status.symlink_to(target)

        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "not a regular file"
        ):
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )

    def test_authority_snapshot_reuses_read_only_descriptors_and_closes_them(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        catalog_key = cycle72._authority_cache_path_key(self.catalog)
        status_key = cycle72._authority_cache_path_key(status)
        descriptors = {
            cycle72._AUTHORITY_FD_CACHE[catalog_key].descriptor,
            cycle72._AUTHORITY_FD_CACHE[status_key].descriptor,
        }
        directory_descriptors = {
            cached.descriptor
            for cached in cycle72._AUTHORITY_DIRECTORY_CACHE.values()
        }
        watchers = {
            cached.watcher
            for cached in cycle72._AUTHORITY_FD_CACHE.values()
            if cached.watcher is not None
        }

        with (
            mock.patch.object(
                cycle72.os, "open", wraps=cycle72.os.open
            ) as open_file,
            mock.patch.object(
                cycle72.os, "fstat", wraps=cycle72.os.fstat
            ) as fstat,
        ):
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )

        # Only two fresh directory descriptors are opened for the before/after
        # identity checks; catalog/status pathnames remain unopened.
        self.assertEqual(open_file.call_count, 2)
        self.assertEqual(
            {
                cycle72._authority_cache_path_key(Path(call.args[0]))
                for call in open_file.call_args_list
            },
            {cycle72._authority_cache_path_key(self.inventory)},
        )
        self.assertEqual(fstat.call_count, 2 if watchers else 6)
        self.assertEqual(
            descriptors,
            {
                cycle72._AUTHORITY_FD_CACHE[catalog_key].descriptor,
                cycle72._AUTHORITY_FD_CACHE[status_key].descriptor,
            },
        )
        cycle72._close_authority_fd_cache()
        self.assertEqual(cycle72._AUTHORITY_FD_CACHE, {})
        self.assertEqual(cycle72._AUTHORITY_DIRECTORY_CACHE, {})
        for descriptor in descriptors | directory_descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertTrue(all(watcher.closed for watcher in watchers))

    def test_ambiguous_file_watcher_change_evicts_only_that_file_and_recovers(self) -> None:
        self.write_catalog(self.rows(2))
        target = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_target",
            "status": "accepted",
        }], name="batch0000_target")
        untouched = self.write_status([{
            "asset_id": "asset-0002",
            "batch": "batch0000_untouched",
            "status": "accepted",
        }], name="batch0000_untouched")
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        target_key = cycle72._authority_cache_path_key(target)
        untouched_key = cycle72._authority_cache_path_key(untouched)
        target_entry = cycle72._AUTHORITY_FD_CACHE[target_key]
        untouched_entry = cycle72._AUTHORITY_FD_CACHE[untouched_key]
        old_target_descriptor = target_entry.descriptor
        if target_entry.watcher is not None:
            target_entry.watcher.close()
        watcher = mock.Mock()
        target_entry.watcher = watcher

        with (
            mock.patch.object(
                cycle72, "_authority_file_watcher_flags", return_value=1
            ),
            mock.patch.object(
                cycle72.select, "KQ_NOTE_WRITE", 1, create=True
            ),
        ):
            with self.assertRaisesRegex(
                cycle72.Cycle72DataError,
                "changed without a distinct identity",
            ):
                cycle72._cached_authority_identity(
                    target, target_entry.dirent_inode
                )

        self.assertNotIn(target_key, cycle72._AUTHORITY_FD_CACHE)
        self.assertIs(
            cycle72._AUTHORITY_FD_CACHE[untouched_key], untouched_entry
        )
        watcher.close.assert_called_once_with()
        with self.assertRaises(OSError):
            os.fstat(old_target_descriptor)
        os.fstat(untouched_entry.descriptor)

        recovered = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        self.assertIn(target_key, cycle72._AUTHORITY_FD_CACHE)
        self.assertIsNot(cycle72._AUTHORITY_FD_CACHE[target_key], target_entry)
        self.assertEqual(
            recovered.effective_statuses["asset-0001"]["status"],
            "accepted",
        )

    def test_existing_jsonl_append_is_seen_without_directory_rescan(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        with status.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "asset_id": "asset-0002",
                "batch": "batch0000_worker",
                "status": "needs_review",
            }) + "\n")

        with mock.patch.object(
            cycle72.os,
            "scandir",
            side_effect=AssertionError(
                "an append to a known descriptor must not rescan ExFAT"
            ),
        ):
            refreshed = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        self.assertEqual(
            refreshed.effective_statuses["asset-0002"]["status"],
            "needs_review",
        )

    @unittest.skipUnless(hasattr(select, "kqueue"), "requires macOS kqueue")
    def test_hot_snapshot_of_174_known_files_never_fstats_file_descriptors(self) -> None:
        self.write_catalog(self.rows(2))
        for index in range(174):
            path = self.inventory / (
                f"total_asset_render_status_batch0000_probe_{index:03d}.jsonl"
            )
            path.write_text("", encoding="utf-8")
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        authority_descriptors = {
            cached.descriptor for cached in cycle72._AUTHORITY_FD_CACHE.values()
        }
        self.assertEqual(len(authority_descriptors), 175)
        self.assertTrue(all(
            cached.watcher is not None
            for cached in cycle72._AUTHORITY_FD_CACHE.values()
        ))

        with (
            mock.patch.object(
                cycle72.os, "fstat", wraps=cycle72.os.fstat
            ) as fstat,
            mock.patch.object(
                cycle72.os,
                "scandir",
                side_effect=AssertionError("hot authority must not rescan"),
            ),
        ):
            cycle72._authority_file_snapshot(self.catalog, self.inventory)

        touched_authority_descriptors = {
            int(call.args[0])
            for call in fstat.call_args_list
            if int(call.args[0]) in authority_descriptors
        }
        self.assertEqual(touched_authority_descriptors, set())

    @unittest.skipUnless(hasattr(select, "kqueue"), "requires macOS kqueue")
    def test_single_jsonl_append_fstats_and_reads_only_changed_file(self) -> None:
        self.write_catalog(self.rows(2))
        target = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        for index in range(12):
            (self.inventory / (
                f"total_asset_render_status_batch0000_idle_{index:02d}.jsonl"
            )).write_text("", encoding="utf-8")
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        descriptors_by_path = {
            path: cached.descriptor
            for path, cached in cycle72._AUTHORITY_FD_CACHE.items()
        }
        target_key = cycle72._authority_cache_path_key(target)
        target_descriptor = descriptors_by_path[target_key]
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "asset_id": "asset-0002",
                "batch": "batch0000_worker",
                "status": "accepted",
            }) + "\n")

        with (
            mock.patch.object(
                cycle72.os, "fstat", wraps=cycle72.os.fstat
            ) as fstat,
            mock.patch.object(
                cycle72,
                "_read_jsonl_stable_bytes",
                wraps=cycle72._read_jsonl_stable_bytes,
            ) as stable_read,
        ):
            refreshed = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )

        authority_descriptors = set(descriptors_by_path.values())
        touched = {
            int(call.args[0])
            for call in fstat.call_args_list
            if int(call.args[0]) in authority_descriptors
        }
        self.assertEqual(touched, {target_descriptor})
        self.assertEqual(
            {cycle72._authority_cache_path_key(call.args[0]) for call in stable_read.call_args_list},
            {target_key},
        )
        self.assertEqual(
            refreshed.effective_statuses["asset-0002"]["status"], "accepted"
        )

    def test_new_jsonl_dirties_directory_cache_and_is_discovered(self) -> None:
        self.write_catalog(self.rows(2))
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        self.write_status([{
            "asset_id": "asset-0002",
            "batch": "batch0000_new_worker",
            "status": "accepted",
        }], name="batch0000_new_worker")

        with mock.patch.object(
            cycle72.os, "scandir", wraps=cycle72.os.scandir
        ) as scandir:
            refreshed = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        self.assertEqual(scandir.call_count, 1)
        self.assertEqual(
            refreshed.effective_statuses["asset-0002"]["status"], "accepted"
        )

    def test_authority_status_deletion_closes_cached_descriptor(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        key = cycle72._authority_cache_path_key(status)
        cached = cycle72._AUTHORITY_FD_CACHE[key]
        descriptor = cached.descriptor
        watcher = cached.watcher
        status.unlink()

        with self.assertRaisesRegex(
            cycle72.Cycle72Conflict, "no longer current"
        ):
            cycle72._resolve_authority_snapshot(
                snapshot,
                catalog_path=self.catalog,
                inventory=self.inventory,
                batch_size=2,
            )

        self.assertNotIn(key, cycle72._AUTHORITY_FD_CACHE)
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        if watcher is not None:
            self.assertTrue(watcher.closed)

    def test_authority_status_replacement_closes_old_descriptor_and_reopens(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        key = cycle72._authority_cache_path_key(status)
        cached = cycle72._AUTHORITY_FD_CACHE[key]
        old_descriptor = cached.descriptor
        old_dirent_inode = cached.dirent_inode
        old_watcher = cached.watcher
        replacement = status.with_suffix(".replacement")
        replacement.write_text(json.dumps({
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "needs_review",
        }) + "\n", encoding="utf-8")
        replacement.replace(status)

        with mock.patch.object(
            cycle72.os, "close", wraps=cycle72.os.close
        ) as close_file:
            with self.assertRaisesRegex(
                cycle72.Cycle72Conflict, "no longer current"
            ):
                cycle72._resolve_authority_snapshot(
                    snapshot,
                    catalog_path=self.catalog,
                    inventory=self.inventory,
                    batch_size=2,
                )

        close_file.assert_any_call(old_descriptor)
        if old_watcher is not None:
            self.assertTrue(old_watcher.closed)
        refreshed = cycle72._AUTHORITY_FD_CACHE[key]
        self.assertNotEqual(refreshed.dirent_inode, old_dirent_inode)

    def test_authority_symlink_replacement_closes_old_descriptor(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        key = cycle72._authority_cache_path_key(status)
        cached = cycle72._AUTHORITY_FD_CACHE[key]
        descriptor = cached.descriptor
        watcher = cached.watcher
        target = self.root / "replacement-target.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        status.unlink()
        status.symlink_to(target)

        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "not a regular file"
        ):
            cycle72._authority_file_snapshot(self.catalog, self.inventory)

        self.assertNotIn(key, cycle72._AUTHORITY_FD_CACHE)
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        if watcher is not None:
            self.assertTrue(watcher.closed)

    def rows(
        self,
        count: int,
        *,
        first_order: int = 1,
        batch_size: int = 2,
    ) -> list[dict[str, str]]:
        result = []
        models = self.root / "models"
        models.mkdir(parents=True, exist_ok=True)
        for offset in range(count):
            order = first_order + offset
            model = models / f"asset-{order:04d}.blend"
            model.write_bytes(b"BLENDER-v405" + b"\0" * 64)
            result.append(
                {
                    "asset_id": f"asset-{order:04d}",
                    "identity_key": f"identity-{order:04d}",
                    "model_file": str(model),
                    "model_format": ".blend",
                    "render_engine_hint": "CYCLES" if order % 2 else "BLENDER_EEVEE_NEXT",
                    "inventory_status": "ready",
                    "duplicate_of": "",
                    "render_order": str(order),
                    "render_batch": f"{(order - 1) // batch_size:04d}",
                    "file_size": str(order * 100),
                }
            )
        return result

    def write_catalog(self, rows: list[dict[str, str]]) -> None:
        with self.catalog.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def write_status(self, rows: list[object], name: str = "batch0000_worker") -> Path:
        path = self.inventory / f"total_asset_render_status_{name}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                if isinstance(row, str):
                    handle.write(row + "\n")
                else:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
        return path

    @staticmethod
    def exact_lease_status(
        lease: dict[str, object],
        status: str,
        **extra: object,
    ) -> dict[str, object]:
        fields = lease.get("status_fields")
        if not isinstance(fields, dict):
            raise AssertionError("lease lacks status fields")
        return {
            "asset_id": str(lease["asset_id"]),
            "batch": f"batch{lease['render_batch']}_worker",
            "status": status,
            **fields,
            **extra,
        }

    def start(
        self,
        *,
        rows: list[dict[str, str]] | None = None,
        now: float = 100.0,
        batch_size: int = 2,
    ) -> dict[str, object]:
        self.write_catalog(
            self.rows(batch_size, batch_size=batch_size)
            if rows is None
            else rows
        )
        return cycle72.start_cycle(
            db_path=self.db,
            manifest_path=self.manifest,
            catalog_path=self.catalog,
            inventory=self.inventory,
            cycle_id="test-cycle",
            duration_hours=72,
            buffer_low_hours=72,
            buffer_target_hours=96,
            batch_min=0,
            batch_size=batch_size,
            knowledge_generation="knowledge-v10",
            code_generation="code-test",
            now_epoch=now,
        )

    def claim(
        self,
        slot_index: int,
        *,
        boot: str = "boot-a",
        now: float = 101.0,
        ttl: float = 60.0,
        confirm_remote_idle: bool = False,
        capabilities: cycle72.SlotCapabilities | None = None,
        snapshot: cycle72.AuthoritySnapshot | None = None,
        excluded_asset_ids: tuple[str, ...] = (),
        runtime_probe_budget: cycle72.RuntimeProbeBudget | None = None,
        source_size_candidate_window: int | None = None,
        preferred_asset_ids: tuple[str, ...] | None = None,
        knowledge_generation: str | None = None,
    ) -> dict[str, object] | None:
        slot = self.slots[slot_index]
        return cycle72.claim_asset(
            db_path=self.db,
            catalog_path=self.catalog,
            inventory=self.inventory,
            worker_index=slot.worker_index,
            remote_port=slot.remote_port,
            gpu=slot.gpu,
            node_boot_id=boot,
            lease_seconds=ttl,
            confirm_remote_idle=confirm_remote_idle,
            capabilities=capabilities,
            snapshot=snapshot,
            excluded_asset_ids=excluded_asset_ids,
            runtime_probe_budget=runtime_probe_budget,
            source_size_candidate_window=source_size_candidate_window,
            preferred_asset_ids=preferred_asset_ids,
            knowledge_generation=knowledge_generation,
            now_epoch=now,
        )

    def priority_items(
        self,
        count: int = 2,
        *,
        suffix: str = ".blend",
        engine: str = "CYCLES",
    ) -> list[dict[str, object]]:
        models = self.root / "priority-models"
        models.mkdir(parents=True, exist_ok=True)
        items: list[dict[str, object]] = []
        for index in range(1, count + 1):
            model = models / f"priority-{index:04d}{suffix}"
            model.write_bytes(
                b"BLENDER-v405" + b"\0" * 64
                if suffix == ".blend"
                else b"priority-source"
            )
            items.append({
                "work_item_id": f"priority-item-{index:04d}",
                "asset_id": f"priority-asset-{index:04d}",
                "identity_key": f"priority-identity-{index:04d}",
                "render_order": index,
                "render_batch": "highqal",
                "inventory_status": "ready",
                "duplicate_of": "",
                "model_file": str(model),
                "model_format": suffix,
                "render_engine_hint": engine,
                "source_kind": "bili_linked_asset",
            })
        return items

    def register_priority(
        self,
        *,
        items: list[dict[str, object]] | None = None,
        generation: str = "a" * 64,
        state: str = "active",
        priority: int = 100,
    ) -> dict[str, object]:
        manifest = self.state / f"priority-{generation}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.exists():
            manifest.write_text("{}\n", encoding="utf-8")
        return cycle72.register_priority_workload(
            db_path=self.db,
            workload_kind="highqal_source",
            generation=generation,
            manifest_path=manifest,
            items=self.priority_items() if items is None else items,
            priority=priority,
            state=state,
            config={"contract": "highqal-test-v1"},
            inventory=self.inventory,
            now_epoch=105,
        )

    def downgrade_database_to_v1(self) -> None:
        """Rebuild only the lease table as the deployed pre-priority schema."""

        legacy_columns = tuple(sorted(cycle72._V1_REQUIRED_LEASE_COLUMNS))
        column_list = ", ".join(legacy_columns)
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE priority_work_items")
            connection.execute("DROP TABLE priority_workloads")
            connection.execute("ALTER TABLE leases RENAME TO leases_v2_backup")
            connection.execute(
                """CREATE TABLE leases (
                       lease_id TEXT PRIMARY KEY,
                       asset_id TEXT NOT NULL,
                       identity_key TEXT NOT NULL,
                       render_order INTEGER NOT NULL,
                       render_batch TEXT NOT NULL,
                       cycle_id TEXT NOT NULL REFERENCES cycles(cycle_id),
                       worker_index INTEGER NOT NULL,
                       remote_port INTEGER NOT NULL,
                       gpu INTEGER NOT NULL,
                       node_boot_id TEXT NOT NULL,
                       state TEXT NOT NULL CHECK(state IN ('active', 'completed', 'released')),
                       attempt INTEGER NOT NULL,
                       recovery_count INTEGER NOT NULL DEFAULT 0,
                       claimed_at_epoch REAL NOT NULL,
                       heartbeat_at_epoch REAL NOT NULL,
                       expires_at_epoch REAL NOT NULL,
                       completed_at_epoch REAL,
                       released_at_epoch REAL,
                       release_reason TEXT,
                       authority_generation TEXT NOT NULL,
                       knowledge_generation TEXT NOT NULL
                   )"""
            )
            connection.execute(
                f"INSERT INTO leases({column_list}) "
                f"SELECT {column_list} FROM leases_v2_backup"
            )
            connection.execute("DROP TABLE leases_v2_backup")
            connection.execute(
                """CREATE UNIQUE INDEX one_active_lease_per_asset
                       ON leases(asset_id) WHERE state='active'"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX one_active_lease_per_worker
                       ON leases(worker_index) WHERE state='active'"""
            )
            connection.execute(
                "CREATE INDEX lease_asset_history ON leases(asset_id, attempt)"
            )
            connection.execute(
                "UPDATE metadata SET value='1' WHERE key='db_schema_version'"
            )
            connection.commit()

    @staticmethod
    def file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def seal_adoption_receipt(receipt: dict[str, object]) -> dict[str, object]:
        payload = dict(receipt)
        payload.pop("generation", None)
        payload["generation"] = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return payload

    def adoption_fixture(
        self, count: int = 1
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        generation = "d" * 64
        canary_generation = "e" * 64
        source_root = self.root / "adoption-sources"
        final_root = self.root / "adoption-final"
        source_root.mkdir()
        final_root.mkdir()
        items: list[dict[str, object]] = []
        receipt_items: list[dict[str, object]] = []
        statuses: list[dict[str, object]] = []
        for index in range(1, count + 1):
            work_item_id = f"adoption-item-{index:04d}"
            asset_id = f"adoption-asset-{index:04d}"
            identity_key = f"adoption-identity-{index:04d}"
            model = source_root / f"model-{index:04d}.blend"
            reference = source_root / f"reference-{index:04d}.mp4"
            model.write_bytes(b"BLENDER-v405" + bytes([index]) * 256)
            reference.write_bytes(b"reference-video" + bytes([index]) * 256)
            model_hash = self.file_sha256(model)
            reference_hash = self.file_sha256(reference)
            item = {
                "work_item_id": work_item_id,
                "asset_id": asset_id,
                "identity_key": identity_key,
                "render_order": index,
                "render_batch": "highqal",
                "inventory_status": "ready",
                "duplicate_of": "",
                "model_file": str(model),
                "model_format": ".blend",
                "model_sha256": model_hash,
                "reference_video": str(reference),
                "reference_sha256": reference_hash,
                "render_engine_hint": "CYCLES",
                "source_kind": "bili_linked_asset",
                "knowledge_generation": "knowledge-adoption-test",
            }
            items.append(item)
            route = "dynamic" if index % 2 == 0 else "static"
            final_dir = final_root / route / work_item_id
            final_dir.mkdir(parents=True)
            asset_blend = final_dir / "asset.blend"
            if route == "static":
                (final_dir / "six_views").mkdir()
                media = final_dir / "six_views/iso.png"
            else:
                media = final_dir / "final_effect.mp4"
            asset_blend.write_bytes(b"adopted-blend" + bytes([index]) * 128)
            media.write_bytes(b"adopted-media" + bytes([index]) * 128)
            status = {
                "status": "accepted",
                "published": True,
                "workload_generation": canary_generation,
                "work_item_id": work_item_id,
                "asset_id": asset_id,
                "model_sha256": model_hash,
                "reference_sha256": reference_hash,
                "render_route": route,
                "final_dir": str(final_dir.resolve()),
            }
            statuses.append(status)
            receipt_items.append({
                "work_item_id": work_item_id,
                "asset_id": asset_id,
                "identity_key": identity_key,
                "model_sha256": model_hash,
                "reference_sha256": reference_hash,
                "canary_status_sha256": hashlib.sha256(
                    json.dumps(
                        status,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "render_route": route,
                "final_dir": str(final_dir.resolve()),
                "asset_blend_sha256": self.file_sha256(asset_blend),
                "media_path": str(media.resolve()),
                "media_sha256": self.file_sha256(media),
            })
        manifest_path = self.state / "adoption-full-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({
                "schema": cycle72.HIGHQAL_FULL_MANIFEST_SCHEMA,
                "generation": generation,
                "items": items,
            }),
            encoding="utf-8",
        )
        cycle72.register_priority_workload(
            db_path=self.db,
            workload_kind="highqal_source",
            generation=generation,
            manifest_path=manifest_path,
            items=items,
            priority=100,
            state="paused",
            config={
                "contract": "highqal-adoption-test-v1",
                "knowledge_generation": "knowledge-adoption-test",
            },
            inventory=self.inventory,
            now_epoch=105,
        )
        status_path = self.root / "adoption-canary-status.jsonl"
        status_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in statuses),
            encoding="utf-8",
        )
        receipt = self.seal_adoption_receipt({
            "schema": cycle72.CANARY_ADOPTION_RECEIPT_SCHEMA,
            "full_generation": generation,
            "canary_generation": canary_generation,
            "status_path": str(status_path.resolve()),
            "final_root": str(final_root.resolve()),
            "items": receipt_items,
        })
        return receipt, items

    def test_start_writes_production_cycle_v1_and_local_wal_database(self) -> None:
        payload = self.start()
        self.assertEqual(payload["schema"], "production_cycle.v1")
        self.assertEqual(payload["assignment_mode"], "dynamic_compatible_v1")
        self.assertEqual(payload["checkpoint_at_epoch"], 100.0 + 72 * 3600)
        self.assertEqual(len(payload["topology"]), 11)
        self.assertEqual(json.loads(self.manifest.read_text())["cycle_id"], "test-cycle")
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            indices = {row[1] for row in connection.execute("PRAGMA index_list('leases')")}
        self.assertIn("one_active_lease_per_asset", indices)
        self.assertIn("one_active_lease_per_worker", indices)

    def test_schema_v1_migration_is_transactional_idempotent_and_preserves_lease(self) -> None:
        self.start()
        original = self.claim(0, now=101)
        self.downgrade_database_to_v1()

        cycle72.initialize_database(self.db, self.inventory)
        cycle72.initialize_database(self.db, self.inventory)

        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='db_schema_version'"
            ).fetchone()["value"]
            migrated = connection.execute(
                "SELECT * FROM leases WHERE lease_id=?", (original["lease_id"],)
            ).fetchone()
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, "2")
        self.assertEqual(migrated["state"], "active")
        self.assertEqual(migrated["asset_id"], original["asset_id"])
        self.assertEqual(migrated["workload_kind"], "total_asset")
        self.assertEqual(migrated["work_item_id"], original["asset_id"])
        self.assertEqual(
            migrated["workload_generation"], original["authority_generation"]
        )
        self.assertTrue({"priority_workloads", "priority_work_items"} <= tables)

        resumed = self.claim(0, now=102)
        self.assertEqual(resumed["lease_id"], original["lease_id"])

    def test_schema_v1_migration_failure_rolls_back_every_ddl_change(self) -> None:
        self.start()
        original = self.claim(0, now=101)
        self.downgrade_database_to_v1()

        with mock.patch.object(
            cycle72,
            "_create_v2_tables",
            side_effect=RuntimeError("injected migration interruption"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                cycle72.initialize_database(self.db, self.inventory)

        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='db_schema_version'"
            ).fetchone()["value"]
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(leases)")
            }
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            lease = connection.execute(
                "SELECT lease_id, state FROM leases"
            ).fetchone()
        self.assertEqual(version, "1")
        self.assertTrue(set(cycle72._V2_LEASE_ADDITIONS).isdisjoint(columns))
        self.assertNotIn("priority_workloads", tables)
        self.assertNotIn("priority_work_items", tables)
        self.assertEqual((lease["lease_id"], lease["state"]), (original["lease_id"], "active"))

        cycle72.initialize_database(self.db, self.inventory)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM metadata WHERE key='db_schema_version'"
                ).fetchone()[0],
                "2",
            )

    def test_unknown_database_schema_is_rejected_without_structural_mutation(self) -> None:
        self.start()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE metadata SET value='99' WHERE key='db_schema_version'"
            )
            before = tuple(connection.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ))

        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "unsupported lease database schema"
        ):
            cycle72.initialize_database(self.db, self.inventory)

        with sqlite3.connect(self.db) as connection:
            after = tuple(connection.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            ))
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='db_schema_version'"
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(version, "99")

    def test_same_cycle_id_start_is_idempotent_after_controller_restart(self) -> None:
        original = self.start(now=100)
        resumed = cycle72.start_cycle(
            db_path=self.db,
            manifest_path=self.manifest,
            catalog_path=self.catalog,
            inventory=self.inventory,
            cycle_id="test-cycle",
            duration_hours=72,
            buffer_low_hours=72,
            buffer_target_hours=96,
            batch_min=0,
            batch_size=2,
            knowledge_generation="knowledge-v10",
            code_generation="code-test",
            now_epoch=200,
        )
        self.assertEqual(resumed, original)
        self.assertEqual(resumed["started_at_epoch"], 100)

    def test_claim_is_global_ordered_not_modulo_partitioned(self) -> None:
        self.start()
        # The last physical worker is still allowed to claim the first global
        # render order; worker indices are identities, not modulo partitions.
        first = self.claim(10)
        second = self.claim(0)
        self.assertEqual(first["render_order"], 1)
        self.assertEqual(second["render_order"], 2)
        self.assertEqual(first["worker_index"], self.slots[10].worker_index)
        self.assertEqual(
            first["status_fields"]["knowledge_generation"],
            cycle72.render_knowledge.KNOWLEDGE_VERSION,
        )

    def test_claim_prefers_small_source_package_inside_bounded_window(self) -> None:
        rows = self.rows(4, batch_size=4)
        for row, size in zip(rows, ("400", "100", "300", "1")):
            row["file_size"] = size
        self.start(rows=rows, batch_size=4)

        claimed = self.claim(
            8,
            source_size_candidate_window=3,
        )

        # Order four is globally smaller but deliberately outside the first
        # three compatible candidates in this batch.
        self.assertEqual(claimed["asset_id"], "asset-0002")
        self.assertEqual(claimed["render_batch"], "0000")
        with sqlite3.connect(self.db) as connection:
            event = json.loads(connection.execute(
                "SELECT payload_json FROM lease_events "
                "WHERE event_type='lease_claimed' ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0])
        self.assertEqual(event["claim_selection"], {
            "policy": "earliest_compatible_batch_prefetch_source_size_window_v2",
            "source_size_candidate_window": 3,
            "local_source_prefetch_ready": False,
            "catalog_file_size_bytes": 100,
        })

    def test_claim_prefers_apfs_ready_source_inside_compatible_window(self) -> None:
        rows = self.rows(4, batch_size=4)
        for row, size in zip(rows, ("10", "20", "300", "1")):
            row["file_size"] = size
        self.start(rows=rows, batch_size=4)

        claimed = self.claim(
            8,
            source_size_candidate_window=3,
            preferred_asset_ids=("asset-0003",),
        )

        self.assertEqual(claimed["asset_id"], "asset-0003")
        with sqlite3.connect(self.db) as connection:
            event = json.loads(connection.execute(
                "SELECT payload_json FROM lease_events "
                "WHERE event_type='lease_claimed' ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0])
        self.assertTrue(event["claim_selection"]["local_source_prefetch_ready"])

    def test_claim_prefers_apfs_ready_source_beyond_ordinary_window(self) -> None:
        rows = self.rows(6, batch_size=6)
        for row, size in zip(rows, ("10", "20", "30", "40", "50", "60")):
            row["file_size"] = size
        self.start(rows=rows, batch_size=6)

        claimed = self.claim(
            8,
            source_size_candidate_window=3,
            preferred_asset_ids=("asset-0006",),
        )

        self.assertEqual(claimed["asset_id"], "asset-0006")
        with sqlite3.connect(self.db) as connection:
            event = json.loads(connection.execute(
                "SELECT payload_json FROM lease_events "
                "WHERE event_type='lease_claimed' ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0])
        self.assertEqual(
            event["claim_selection"]["source_size_candidate_window"], 3
        )
        self.assertTrue(event["claim_selection"]["local_source_prefetch_ready"])

    def test_later_batch_ready_hint_never_leapfrogs_earliest_batch(self) -> None:
        rows = self.rows(8, batch_size=4)
        self.start(rows=rows, batch_size=4)

        claimed = self.claim(
            8,
            source_size_candidate_window=3,
            preferred_asset_ids=("asset-0005",),
        )

        self.assertEqual(claimed["render_batch"], "0000")
        self.assertNotEqual(claimed["asset_id"], "asset-0005")

    def test_claim_size_priority_can_use_the_configured_full_window(self) -> None:
        rows = self.rows(4, batch_size=4)
        for row, size in zip(rows, ("400", "100", "300", "1")):
            row["file_size"] = size
        self.start(rows=rows, batch_size=4)

        claimed = self.claim(
            8,
            source_size_candidate_window=4,
        )

        self.assertEqual(claimed["asset_id"], "asset-0004")

    def test_small_package_never_leapfrogs_earliest_incomplete_full_batch(self) -> None:
        rows = self.rows(4)
        for row, size in zip(rows, ("900", "800", "1", "2")):
            row["file_size"] = size
        self.start(rows=rows)
        self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])

        claimed = self.claim(8)

        self.assertEqual(claimed["asset_id"], "asset-0002")
        self.assertEqual(claimed["render_batch"], "0000")

    def test_incompatible_earliest_batch_allows_compatible_later_batch(self) -> None:
        rows = self.rows(4)
        rows[0]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        rows[1]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        rows[2]["render_engine_hint"] = "CYCLES"
        rows[3]["render_engine_hint"] = "CYCLES"
        self.start(rows=rows)
        primary_slot = next(
            index
            for index, slot in enumerate(self.slots)
            if slot.remote_port == cycle72.PRIMARY_REMOTE_PORT
        )

        # The primary node is CYCLES-only.  It cannot consume batch 0000, so
        # leaving it idle would violate the dynamic-compatible production
        # contract while batch 0001 has safe work for this physical slot.
        claimed = self.claim(primary_slot)
        self.assertEqual(claimed["asset_id"], "asset-0003")
        self.assertEqual(claimed["render_batch"], "0001")

    def test_temporarily_excluded_earliest_tail_does_not_idle_later_work(self) -> None:
        rows = self.rows(4)
        self.start(rows=rows)

        claimed = self.claim(
            8,
            excluded_asset_ids=("asset-0001", "asset-0002"),
        )

        self.assertEqual(claimed["asset_id"], "asset-0003")
        self.assertEqual(claimed["render_batch"], "0001")

    def test_completed_earliest_batch_releases_next_full_batch(self) -> None:
        rows = self.rows(4)
        rows[2]["file_size"] = "2"
        rows[3]["file_size"] = "1"
        self.start(rows=rows)
        self.write_status([
            {
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "accepted",
            },
            {
                "asset_id": "asset-0002",
                "batch": "batch0000_worker",
                "status": "needs_review",
            },
        ])

        claimed = self.claim(8)

        self.assertEqual(claimed["asset_id"], "asset-0004")
        self.assertEqual(claimed["render_batch"], "0001")

    def test_source_size_window_and_unknown_sizes_fail_closed_or_sort_last(self) -> None:
        self.start()
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError,
            "source-size claim window",
        ):
            self.claim(8, source_size_candidate_window=1)
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError,
            "source-size claim window",
        ):
            self.claim(
                8,
                source_size_candidate_window=(
                    cycle72.MAX_CLAIM_SOURCE_SIZE_WINDOW + 1
                ),
            )
        with mock.patch.dict(
            os.environ,
            {cycle72.CLAIM_SOURCE_SIZE_WINDOW_ENV: "not-an-integer"},
        ), self.assertRaisesRegex(
            cycle72.Cycle72DataError,
            "source-size claim window",
        ):
            self.claim(8)

        known = {"render_order": "2", "file_size": "10"}
        unknown = {"render_order": "1", "file_size": "unknown"}
        self.assertLess(
            cycle72._source_package_size_sort_key(known),
            cycle72._source_package_size_sort_key(unknown),
        )

    def test_runtime_compatibility_routes_legacy_source_to_gpu0(self) -> None:
        rows = self.rows(2)
        legacy = Path(rows[0]["model_file"])
        modern = Path(rows[1]["model_file"])
        legacy.write_bytes(b"BLENDER-v306" + b"\0" * 64)
        modern.write_bytes(b"BLENDER-v405" + b"\0" * 64)
        rows[0]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        rows[1]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        self.start(rows=rows)

        self.assertEqual(cycle72.source_runtime_family_for_claim(rows[0]), "3.6")
        self.assertFalse(cycle72.row_compatible_with_physical_gpu(rows[0], 1))
        self.assertTrue(cycle72.row_compatible_with_physical_gpu(rows[0], 0))
        self.assertEqual(cycle72.source_runtime_family_for_claim(rows[1]), "4.5")
        self.assertTrue(cycle72.row_compatible_with_physical_gpu(rows[1], 1))

        nonzero_claim = self.claim(
            1,
            capabilities=cycle72.SlotCapabilities.normalized(
                attested_vulkan_families=["4.5"]
            ),
        )
        gpu0_claim = self.claim(0)
        self.assertEqual(nonzero_claim["asset_id"], "asset-0002")
        self.assertEqual(gpu0_claim["asset_id"], "asset-0001")

    def test_nonzero_pinned_slot_requires_exact_vulkan_family_attestation(self) -> None:
        modern = self.root / "modern.blend"
        modern.write_bytes(b"BLENDER-v405" + b"\0" * 64)
        source_row = {
            "model_file": str(modern),
            "model_format": ".blend",
            "render_engine_hint": "BLENDER_EEVEE_NEXT",
        }
        cycles_row = {**source_row, "render_engine_hint": "CYCLES"}
        pinned_port = next(
            slot.remote_port
            for slot in self.slots
            if slot.remote_port != cycle72.PRIMARY_REMOTE_PORT and slot.gpu == 1
        )

        self.assertTrue(
            cycle72.row_compatible_with_physical_slot(source_row, pinned_port, 0)
        )
        self.assertFalse(
            cycle72.row_compatible_with_physical_slot(source_row, pinned_port, 1)
        )
        self.assertFalse(
            cycle72.row_compatible_with_physical_slot(
                source_row,
                pinned_port,
                1,
                attested_vulkan_families=("5.1",),
            )
        )
        self.assertTrue(
            cycle72.row_compatible_with_physical_slot(
                source_row,
                pinned_port,
                1,
                attested_vulkan_families=("4.5",),
            )
        )
        self.assertTrue(
            cycle72.row_compatible_with_physical_slot(cycles_row, pinned_port, 1)
        )

    def test_claim_falls_back_to_cycles_when_profile_attestation_is_absent(self) -> None:
        rows = self.rows(2)
        rows[0]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        rows[1]["render_engine_hint"] = "CYCLES"
        self.start(rows=rows)

        cycles_claim = self.claim(1)
        self.assertEqual(cycles_claim["asset_id"], rows[1]["asset_id"])
        claimed = self.claim(
            2,
            capabilities=cycle72.SlotCapabilities.normalized(
                attested_vulkan_families=["4.5"]
            ),
        )
        self.assertEqual(claimed["asset_id"], rows[0]["asset_id"])

    def test_known_policy_rejection_does_not_open_source_asset(self) -> None:
        source_row = {
            "model_file": str(self.root / "must-not-be-opened.blend"),
            "model_format": ".blend",
            "render_engine_hint": "BLENDER_EEVEE_NEXT",
        }
        pinned_port = next(
            slot.remote_port
            for slot in self.slots
            if slot.remote_port != cycle72.PRIMARY_REMOTE_PORT and slot.gpu == 1
        )
        with mock.patch.object(
            cycle72,
            "source_runtime_family_for_claim",
            side_effect=AssertionError("source asset should not be opened"),
        ):
            self.assertFalse(cycle72.row_compatible_with_physical_slot(
                source_row, pinned_port, 1
            ))
            self.assertFalse(cycle72.row_compatible_with_physical_slot(
                source_row, cycle72.PRIMARY_REMOTE_PORT, 0
            ))
    def test_imported_source_requires_worker_selected_profile_family(self) -> None:
        pinned_port = next(
            slot.remote_port
            for slot in self.slots
            if slot.remote_port != cycle72.PRIMARY_REMOTE_PORT and slot.gpu == 1
        )
        fbx = {
            "model_file": str(self.root / "model.fbx"),
            "model_format": ".fbx",
            "render_engine_hint": "source",
        }
        obj = {
            "model_file": str(self.root / "model.obj"),
            "model_format": ".obj",
            "render_engine_hint": "source",
        }
        self.assertTrue(
            cycle72.row_compatible_with_physical_slot(
                fbx,
                pinned_port,
                1,
                attested_vulkan_families=("4.5",),
            )
        )
        self.assertFalse(
            cycle72.row_compatible_with_physical_slot(
                fbx,
                pinned_port,
                1,
                attested_vulkan_families=("5.1",),
            )
        )
        self.assertTrue(
            cycle72.row_compatible_with_physical_slot(
                obj,
                pinned_port,
                1,
                attested_vulkan_families=("5.1",),
            )
        )

    def test_runtime_compatibility_allows_cycles_and_imports_fail_closed_unknown(self) -> None:
        legacy = self.root / "legacy.blend"
        legacy.write_bytes(b"BLENDER-v402" + b"\0" * 64)
        cycles_row = {
            "model_file": str(legacy),
            "model_format": ".blend",
            "render_engine_hint": "CYCLES",
        }
        imported_row = {
            "model_file": str(self.root / "model.fbx"),
            "model_format": ".fbx",
            "render_engine_hint": "source",
        }
        unknown_row = {
            "model_file": str(self.root / "missing.blend"),
            "model_format": ".blend",
            "render_engine_hint": "source",
        }

        self.assertEqual(cycle72.source_runtime_family_for_claim(cycles_row), "4.2")
        self.assertTrue(cycle72.row_compatible_with_physical_gpu(cycles_row, 1))
        self.assertEqual(cycle72.source_runtime_family_for_claim(imported_row), "imported")
        self.assertTrue(cycle72.row_compatible_with_physical_gpu(imported_row, 1))
        self.assertEqual(cycle72.source_runtime_family_for_claim(unknown_row), "unknown")
        self.assertFalse(cycle72.row_compatible_with_physical_gpu(unknown_row, 1))
        self.assertTrue(cycle72.row_compatible_with_physical_gpu(unknown_row, 0))
        self.assertFalse(cycle72.row_compatible_with_physical_gpu(imported_row, -1))

    def test_imported_runtime_family_never_stats_a_non_blend_source(self) -> None:
        row = {
            "model_file": str(self.root / "missing-but-importable.obj"),
            "model_format": ".obj",
            "render_engine_hint": "source",
        }
        with mock.patch.object(
            Path,
            "stat",
            side_effect=AssertionError("imported dispatch must not stat a header"),
        ):
            self.assertEqual(
                cycle72.source_runtime_family_for_claim(row), "imported"
            )

    def test_probe_budget_resumes_after_cached_fail_closed_negative(self) -> None:
        rows = self.rows(2)
        Path(rows[0]["model_file"]).write_bytes(
            b"BLENDER-v306" + b"\0" * 64
        )
        rows[0]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        rows[1]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        self.start(rows=rows)
        capability = cycle72.SlotCapabilities.normalized(
            attested_vulkan_families=["4.5"]
        )

        first_budget = cycle72.RuntimeProbeBudget(limit=1)
        with mock.patch.object(
            cycle72,
            "source_runtime_family_for_claim",
            wraps=cycle72.source_runtime_family_for_claim,
        ) as runtime_family:
            self.assertIsNone(
                self.claim(
                    1,
                    capabilities=capability,
                    runtime_probe_budget=first_budget,
                )
            )
        self.assertEqual(runtime_family.call_count, 1)
        self.assertEqual(first_budget.consumed, 1)

        second_budget = cycle72.RuntimeProbeBudget(limit=1)
        with mock.patch.object(
            cycle72,
            "source_runtime_family_for_claim",
            wraps=cycle72.source_runtime_family_for_claim,
        ) as runtime_family:
            claimed = self.claim(
                1,
                capabilities=capability,
                runtime_probe_budget=second_budget,
            )
        self.assertEqual(claimed["asset_id"], rows[1]["asset_id"])
        self.assertEqual(runtime_family.call_count, 1)
        self.assertEqual(second_budget.consumed, 1)

        slot = self.slots[1]
        Path(rows[0]["model_file"]).write_bytes(
            b"BLENDER-v405" + b"\0" * 64
        )
        cycle72._cached_source_runtime_family.cache_clear()
        with mock.patch.object(
            cycle72,
            "source_runtime_family_for_claim",
            side_effect=AssertionError(
                "a cached negative must not repeat the ExFAT probe"
            ),
        ):
            self.assertFalse(
                cycle72._claim_row_runtime_compatible(
                    rows[0],
                    slot.remote_port,
                    slot.gpu,
                    attested_vulkan_families=("4.5",),
                    probe_budget=cycle72.RuntimeProbeBudget(limit=0),
                )
            )

    def test_runtime_probe_never_holds_the_sqlite_write_lock(self) -> None:
        rows = self.rows(2)
        for row in rows:
            row["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        self.start(rows=rows)
        real_runtime_family = cycle72.source_runtime_family_for_claim
        writer_acquisitions = 0

        def probe_without_writer_block(row: dict[str, str]) -> str:
            nonlocal writer_acquisitions
            with sqlite3.connect(self.db, timeout=0, isolation_level=None) as writer:
                writer.execute("PRAGMA busy_timeout=0")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("ROLLBACK")
            writer_acquisitions += 1
            return real_runtime_family(row)

        with mock.patch.object(
            cycle72,
            "source_runtime_family_for_claim",
            side_effect=probe_without_writer_block,
        ):
            claimed = self.claim(
                1,
                capabilities=cycle72.SlotCapabilities.normalized(
                    attested_vulkan_families=["4.5"]
                ),
                runtime_probe_budget=cycle72.RuntimeProbeBudget(limit=1),
            )
        self.assertEqual(claimed["asset_id"], rows[0]["asset_id"])
        self.assertEqual(writer_acquisitions, 1)

    def test_primary_native_565_all_slots_claim_cycles_only(self) -> None:
        rows = self.rows(2)
        rows[0]["render_engine_hint"] = "BLENDER_EEVEE_NEXT"
        rows[1]["render_engine_hint"] = "CYCLES"
        self.start(rows=rows)
        primary_gpu0 = next(
            index
            for index, slot in enumerate(self.slots)
            if slot.remote_port == cycle72.PRIMARY_REMOTE_PORT and slot.gpu == 0
        )
        primary_gpu1 = next(
            index
            for index, slot in enumerate(self.slots)
            if slot.remote_port == cycle72.PRIMARY_REMOTE_PORT and slot.gpu == 1
        )

        self.assertFalse(cycle72.row_compatible_with_physical_slot(
            rows[0], cycle72.PRIMARY_REMOTE_PORT, 1
        ))
        self.assertFalse(cycle72.row_compatible_with_physical_slot(
            rows[0], cycle72.PRIMARY_REMOTE_PORT, 0
        ))
        self.assertTrue(cycle72.row_compatible_with_physical_slot(
            rows[1], cycle72.PRIMARY_REMOTE_PORT, 1
        ))
        nonzero = self.claim(primary_gpu1)
        gpu0 = self.claim(primary_gpu0)
        self.assertEqual(nonzero["asset_id"], rows[1]["asset_id"])
        self.assertIsNone(gpu0)

    def test_reviewed_static_cycles_fallback_avoids_serial_header_probe(self) -> None:
        row = {
            "model_file": str(self.root / "cold-exfat-source.blend"),
            "model_format": ".blend",
            "render_engine_hint": "source",
            "render_route": "static",
        }
        pinned_port = next(
            slot.remote_port
            for slot in self.slots
            if slot.remote_port != cycle72.PRIMARY_REMOTE_PORT and slot.gpu == 1
        )
        with mock.patch.object(
            cycle72,
            "source_runtime_family_for_claim",
            side_effect=AssertionError(
                "reviewed fallback must not serialize an ExFAT header probe"
            ),
        ):
            self.assertTrue(cycle72.row_compatible_with_physical_slot(
                row, cycle72.PRIMARY_REMOTE_PORT, 3
            ))
            self.assertTrue(cycle72.row_compatible_with_physical_slot(
                row, pinned_port, 1
            ))
            self.assertFalse(cycle72._claim_runtime_probe_required(
                row, pinned_port, 1
            ))

    def test_reviewed_static_fallback_never_changes_dynamic_candidate(self) -> None:
        row = {
            "model_file": str(self.root / "dynamic.blend"),
            "model_format": ".blend",
            "render_engine_hint": "source",
            "render_route": "dynamic_candidate",
        }
        pinned_port = next(
            slot.remote_port
            for slot in self.slots
            if slot.remote_port != cycle72.PRIMARY_REMOTE_PORT and slot.gpu == 1
        )
        with mock.patch.object(
            cycle72,
            "source_runtime_family_for_claim",
            side_effect=AssertionError(
                "missing profile must reject dynamic before source probing"
            ),
        ):
            self.assertFalse(cycle72.row_compatible_with_physical_slot(
                row, cycle72.PRIMARY_REMOTE_PORT, 0
            ))
            self.assertFalse(cycle72.row_compatible_with_physical_slot(
                row, pinned_port, 1
            ))

    def test_runtime_compatibility_uses_final_cumulative_tutorial_stage(self) -> None:
        tutorial = self.root / "tutorial"
        tutorial.mkdir()
        first = tutorial / "1 - Base.blend"
        second = tutorial / "2 - Materials.blend"
        final = tutorial / "3 - Final.blend"
        first.write_bytes(b"BLENDER-v405" + b"\0" * 64)
        second.write_bytes(b"BLENDER-v405" + b"\0" * 64)
        final.write_bytes(b"BLENDER-v306" + b"\0" * 64)
        row = {
            "model_file": str(first),
            "model_format": ".blend",
            "render_engine_hint": "BLENDER_EEVEE_NEXT",
        }

        selected, reason = cycle72.select_canonical_model(first)
        self.assertEqual(selected, final)
        self.assertIn("cumulative tutorial stage", reason)
        self.assertEqual(cycle72.source_runtime_family_for_claim(row), "3.6")
        self.assertFalse(cycle72.row_compatible_with_physical_gpu(row, 1))
        self.assertTrue(cycle72.row_compatible_with_physical_gpu(row, 0))

    def test_new_claim_can_pin_a_new_knowledge_generation_at_asset_boundary(self) -> None:
        self.start()
        claim = self.claim(0, knowledge_generation="knowledge-v11")
        self.assertEqual(claim["knowledge_generation"], "knowledge-v11")
        self.assertEqual(
            claim["status_fields"]["knowledge_generation"], "knowledge-v11"
        )

    def test_new_claim_uses_current_knowledge_without_hot_changing_active_lease(self) -> None:
        self.start()
        active = self.claim(0, knowledge_generation="knowledge-v10")
        same = self.claim(0)
        new_claim = self.claim(8)

        self.assertEqual(same["lease_id"], active["lease_id"])
        self.assertEqual(same["knowledge_generation"], "knowledge-v10")
        self.assertEqual(
            new_claim["knowledge_generation"],
            cycle72.render_knowledge.KNOWLEDGE_VERSION,
        )

    def test_wrong_physical_location_is_rejected(self) -> None:
        self.start()
        slot = self.slots[0]
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "physical slot"):
            cycle72.claim_asset(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                worker_index=slot.worker_index,
                remote_port=slot.remote_port,
                gpu=slot.gpu + 1,
                node_boot_id="boot-a",
            )

    def test_partial_batch_is_never_claimed(self) -> None:
        rows = self.rows(3)
        self.start(rows=rows)
        self.write_status(
            [
                {"asset_id": "asset-0001", "batch": "batch0000_worker", "status": "accepted"},
                {"asset_id": "asset-0002", "batch": "batch0000_worker", "status": "failed"},
            ]
        )
        self.assertIsNone(self.claim(0))
        status = cycle72.cycle_status(
            db_path=self.db,
            catalog_path=self.catalog,
            inventory=self.inventory,
            now_epoch=105,
        )
        self.assertEqual(status["partial_batches_excluded"], {"0001": 1})

    def test_formal_status_is_authoritative_and_compatibility_can_skip(self) -> None:
        self.start()
        self.write_status(
            [{"asset_id": "asset-0001", "batch": "batch0000_worker", "status": "accepted"}]
        )
        claim = self.claim(
            0,
            capabilities=cycle72.SlotCapabilities.normalized(
                allowed_engine_hints=["BLENDER_EEVEE_NEXT"],
                allowed_model_formats=["blend"],
            ),
        )
        self.assertEqual(claim["asset_id"], "asset-0002")

    def test_only_reviewed_post_render_gpu_race_is_reclaimed(self) -> None:
        self.start()
        obsolete = {
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "failed",
            "render_batch": "0000",
            "failure_stage": "contract_validation",
            "failure_category": "remote_render_failure",
            "knowledge_version": (
                cycle72.render_knowledge
                .OBSOLETE_POST_RENDER_GPU_ATTESTATION_KNOWLEDGE_VERSION
            ),
            "error": (
                "Saved: '/tmp/total_asset_render/batch0000/asset-0001/"
                "output/six_views/iso.png'\n"
                "RuntimeError: TOTAL_ASSET_GPU_UUID_ATTESTATION_FAILED "
                "observed=none probe=ok\nBlender quit"
            ),
        }
        genuine = {
            "asset_id": "asset-0002",
            "batch": "batch0000_worker",
            "status": "failed",
            "render_batch": "0000",
            "failure_stage": "primary_render",
            "failure_category": "missing_dependency",
            "knowledge_version": (
                cycle72.render_knowledge
                .OBSOLETE_POST_RENDER_GPU_ATTESTATION_KNOWLEDGE_VERSION
            ),
            "error": "source texture is genuinely missing",
        }
        self.write_status([obsolete, genuine])

        before = cycle72.cycle_status(
            db_path=self.db,
            catalog_path=self.catalog,
            inventory=self.inventory,
            now_epoch=100.5,
        )
        self.assertEqual(
            before["batch_counts"]["0000"],
            {"expected": 2, "terminal": 1, "leased": 0, "claimable": 1},
        )
        self.assertEqual(
            before["obsolete_infrastructure_retry_candidate_count"], 1
        )
        self.assertEqual(
            before["obsolete_infrastructure_retry_candidates"],
            [{
                "asset_id": "asset-0001",
                "render_order": 1,
                "render_batch": "0000",
                "rules": [
                    cycle72.render_knowledge
                    .POST_RENDER_GPU_ATTESTATION_RACE_RULE
                ],
            }],
        )

        claim = self.claim(0, now=101)
        self.assertEqual(claim["asset_id"], "asset-0001")
        self.assertEqual(
            claim["knowledge_generation"],
            cycle72.render_knowledge.KNOWLEDGE_VERSION,
        )
        self.assertEqual(
            claim["obsolete_infrastructure_retry_rules"],
            [cycle72.render_knowledge.POST_RENDER_GPU_ATTESTATION_RACE_RULE],
        )

        same = self.claim(0, now=102)
        self.assertEqual(same["lease_id"], claim["lease_id"])
        with self.assertRaisesRegex(
            cycle72.Cycle72Conflict, "exact active lease"
        ):
            cycle72.complete_lease(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                lease_id=str(claim["lease_id"]),
                worker_index=self.slots[0].worker_index,
                node_boot_id="boot-a",
                now_epoch=103,
            )

        self.write_status([
            obsolete,
            genuine,
            self.exact_lease_status(
                claim,
                "accepted",
                knowledge_version=cycle72.render_knowledge.KNOWLEDGE_VERSION,
            ),
        ])
        completed = cycle72.complete_lease(
            db_path=self.db,
            catalog_path=self.catalog,
            inventory=self.inventory,
            lease_id=str(claim["lease_id"]),
            worker_index=self.slots[0].worker_index,
            node_boot_id="boot-a",
            now_epoch=104,
        )
        self.assertEqual(completed["state"], "completed")
        self.assertIsNone(self.claim(1, now=105))

    def test_reviewed_31722_loader_failure_is_append_only_reclaimed(self) -> None:
        topology = {
            (
                31722 if worker_index in {0, 1, 2, 3} else port,
                gpu,
            ): worker_index
            for (port, gpu), worker_index in (
                cycle72.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            )
        }
        obsolete_version = (
            cycle72.render_knowledge
            .OBSOLETE_BLENDER_SHARED_LIBRARY_LOADER_KNOWLEDGE_VERSION
        )
        loader_failure = {
            "asset_id": "asset-0001",
            "identity_key": "identity-0001",
            "cycle_id": "cycle72-test",
            "lease_id": "lease-before-runtime-package-repair",
            "workload_kind": "total_asset",
            "batch": "batch0000_worker",
            "status": "failed",
            "render_batch": "0000",
            "failure_stage": "primary_render",
            "failure_category": "remote_render_failure",
            "failure_code": "worker_remote_command_failed",
            "remote_exit_status": 127,
            "knowledge_version": obsolete_version,
            "knowledge_generation": obsolete_version,
            "assignment_mode": "dynamic_compatible_v1",
            "physical_worker": {
                "worker_index": 0,
                "remote_port": 31722,
                "gpu": 0,
                "node_boot_id": "boot-before-runtime-package-repair",
            },
            "error": (
                "exitstatus=127 worker_remote_command_failed\n"
                "[remote stderr]\n"
                "/root/blender-4.5.10-linux-x64/blender: error while "
                "loading shared libraries: libSM.so.6: cannot open shared "
                "object file: No such file or directory"
            ),
            "updated_at": "2026-07-22T15:00:00+08:00",
        }
        genuine_asset_failure = {
            "asset_id": "asset-0002",
            "identity_key": "identity-0002",
            "cycle_id": "cycle72-test",
            "lease_id": "lease-before-runtime-package-repair-2",
            "workload_kind": "total_asset",
            "batch": "batch0000_worker",
            "status": "failed",
            "render_batch": "0000",
            "failure_stage": "primary_render",
            "failure_category": "missing_dependency",
            "failure_code": "worker_remote_command_failed",
            "remote_exit_status": 127,
            "knowledge_version": obsolete_version,
            "knowledge_generation": obsolete_version,
            "assignment_mode": "dynamic_compatible_v1",
            "physical_worker": {
                "worker_index": 0,
                "remote_port": 31722,
                "gpu": 0,
                "node_boot_id": "boot-before-runtime-package-repair",
            },
            "error": (
                "exitstatus=127 worker_remote_command_failed\n"
                "source texture genuinely missing: /textures/albedo.png"
            ),
            "updated_at": "2026-07-22T15:00:01+08:00",
        }

        with mock.patch.dict(
            cycle72.CANONICAL_WORKER_INDEX_BY_LOCATION,
            topology,
            clear=True,
        ):
            self.start()
            status_path = self.write_status([
                loader_failure,
                genuine_asset_failure,
            ])
            historical_bytes = status_path.read_bytes()

            status = cycle72.cycle_status(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                now_epoch=100.5,
            )
            self.assertEqual(
                status["obsolete_infrastructure_retry_candidates"],
                [{
                    "asset_id": "asset-0001",
                    "render_order": 1,
                    "render_batch": "0000",
                    "rules": [
                        cycle72.render_knowledge
                        .BLENDER_SHARED_LIBRARY_LOADER_RETRY_RULE
                    ],
                }],
            )

            claim = cycle72.claim_asset(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                worker_index=0,
                remote_port=31722,
                gpu=0,
                node_boot_id="boot-after-runtime-package-repair",
                now_epoch=101,
            )
            self.assertEqual(claim["asset_id"], "asset-0001")
            self.assertEqual(
                claim["knowledge_generation"],
                cycle72.render_knowledge.KNOWLEDGE_VERSION,
            )
            self.assertEqual(
                claim["obsolete_infrastructure_retry_rules"],
                [
                    cycle72.render_knowledge
                    .BLENDER_SHARED_LIBRARY_LOADER_RETRY_RULE
                ],
            )
            with self.assertRaisesRegex(
                cycle72.Cycle72Conflict,
                "exact active lease",
            ):
                cycle72.complete_lease(
                    db_path=self.db,
                    catalog_path=self.catalog,
                    inventory=self.inventory,
                    lease_id=str(claim["lease_id"]),
                    worker_index=0,
                    node_boot_id="boot-after-runtime-package-repair",
                    now_epoch=102,
                )
            self.assertEqual(status_path.read_bytes(), historical_bytes)

            replacement = self.exact_lease_status(
                claim,
                "accepted",
                knowledge_version=(
                    cycle72.render_knowledge.KNOWLEDGE_VERSION
                ),
                knowledge_generation=(
                    cycle72.render_knowledge.KNOWLEDGE_VERSION
                ),
                updated_at="2026-07-22T16:00:00+08:00",
            )
            with status_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(replacement, sort_keys=True) + "\n")
            self.assertTrue(status_path.read_bytes().startswith(historical_bytes))

            completed = cycle72.complete_lease(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                lease_id=str(claim["lease_id"]),
                worker_index=0,
                node_boot_id="boot-after-runtime-package-repair",
                now_epoch=103,
            )
            self.assertEqual(completed["state"], "completed")
            self.assertIsNone(cycle72.claim_asset(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                worker_index=1,
                remote_port=31722,
                gpu=1,
                node_boot_id="boot-after-runtime-package-repair",
                now_epoch=104,
            ))

    def test_concurrent_claims_cannot_duplicate_asset_or_worker(self) -> None:
        self.start()
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        barrier = threading.Barrier(3)
        results: list[dict[str, object] | None] = []
        errors: list[Exception] = []

        def run(slot: int) -> None:
            try:
                barrier.wait()
                results.append(
                    self.claim(
                        slot,
                        snapshot=snapshot,
                        capabilities=cycle72.SlotCapabilities.normalized(
                            attested_vulkan_families=["4.5"]
                        ),
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({item["asset_id"] for item in results}, {"asset-0001", "asset-0002"})
        with sqlite3.connect(self.db) as connection:
            active = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT asset_id), COUNT(DISTINCT worker_index) "
                "FROM leases WHERE state='active'"
            ).fetchone()
        self.assertEqual(active, (2, 2, 2))

    def test_simultaneous_read_only_preselection_is_rechecked_under_write_lock(self) -> None:
        self.start()
        barrier = threading.Barrier(2)
        real_preselect = cycle72._preselect_claim_candidates
        results: dict[int, dict[str, object] | None] = {}
        errors: list[Exception] = []

        def synchronized_preselect(*args: object, **kwargs: object):
            selected = real_preselect(*args, **kwargs)
            barrier.wait(timeout=5)
            return selected

        def run(slot: int) -> None:
            try:
                results[slot] = self.claim(slot)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with mock.patch.object(
            cycle72,
            "_preselect_claim_candidates",
            side_effect=synchronized_preselect,
        ):
            threads = [threading.Thread(target=run, args=(slot,)) for slot in (0, 8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(errors, [])
        claimed = [item for item in results.values() if item is not None]
        self.assertEqual(len(claimed), 2)
        self.assertEqual(
            {item["asset_id"] for item in claimed},
            {"asset-0001", "asset-0002"},
        )

        with sqlite3.connect(self.db) as connection:
            active = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT asset_id), "
                "COUNT(DISTINCT worker_index) FROM leases WHERE state='active'"
            ).fetchone()
        self.assertEqual(active, (2, 2, 2))

    def test_final_transaction_fence_rejects_newly_terminal_fallback_asset(self) -> None:
        self.start()
        # Keep a real priority generation active but incompatible with this
        # slot, so the assertion also covers the priority-to-total fallback.
        self.register_priority(items=self.priority_items(1, suffix=".fbx"))
        real_resolve = cycle72._resolve_authority_snapshot
        calls = 0

        def land_terminal_before_final_fence(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.write_status([{
                    "asset_id": "asset-0001",
                    "batch": "batch0000_worker",
                    "status": "accepted",
                }])
            return real_resolve(*args, **kwargs)

        with mock.patch.object(
            cycle72,
            "_resolve_authority_snapshot",
            side_effect=land_terminal_before_final_fence,
        ):
            with self.assertRaisesRegex(
                cycle72.Cycle72Conflict, "no longer current"
            ):
                self.claim(
                    0,
                    now=106,
                    capabilities=cycle72.SlotCapabilities.normalized(
                        allowed_model_formats=[".blend"]
                    ),
                )

        self.assertEqual(calls, 3)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
                0,
            )
        status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        self.assertEqual(status["counts"], {"ready": 1})

    def test_shared_snapshot_is_generation_checked_and_supports_exclusions(self) -> None:
        self.start()
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        claim = self.claim(
            0,
            snapshot=snapshot,
            excluded_asset_ids=("asset-0001",),
        )
        self.assertEqual(claim["asset_id"], "asset-0002")
        self.write_status(
            [{"asset_id": "asset-0001", "batch": "batch0000_worker", "status": "accepted"}]
        )
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "no longer current"):
            self.claim(1, snapshot=snapshot)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "no longer current"):
            cycle72.cycle_status(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                snapshot=snapshot,
            )

    def test_authority_snapshot_parses_each_unchanged_file_only_once(self) -> None:
        self.write_catalog(self.rows(2))
        self.write_status(
            [{
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "accepted",
            }]
        )
        with (
            mock.patch.object(
                cycle72, "_read_catalog", wraps=cycle72._read_catalog
            ) as read_catalog,
            mock.patch.object(
                cycle72, "_read_jsonl_strict", wraps=cycle72._read_jsonl_strict
            ) as read_jsonl,
        ):
            first = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
            second = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )

        self.assertEqual(read_catalog.call_count, 1)
        self.assertEqual(read_jsonl.call_count, 1)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(second.effective_statuses["asset-0001"]["status"], "accepted")

    def test_persistent_authority_cache_survives_restart_without_jsonl_read(self) -> None:
        self.write_catalog(self.rows(2))
        self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        database = self.state / "authority_parse_cache.sqlite3"
        with authority_cache.PersistentAuthorityParseCache(database) as cache:
            with mock.patch.object(
                cycle72,
                "_authority_file_snapshot",
                wraps=cycle72._authority_file_snapshot,
            ) as scans:
                first = cycle72.load_authority_snapshot(
                    self.catalog,
                    self.inventory,
                    batch_size=2,
                    persistent_jsonl_cache=cache,
                )
        self.assertEqual(scans.call_count, 2)
        self.assertTrue(database.is_file())

        # Simulate a supervisor restart: the open-descriptor and in-memory row
        # caches disappear, while the APFS SQLite cache remains.
        cycle72._close_authority_fd_cache()
        with cycle72._AUTHORITY_CACHE_LOCK:
            cycle72._JSONL_PARSE_CACHE.clear()
        with authority_cache.PersistentAuthorityParseCache(database) as cache:
            with mock.patch.object(
                authority_cache.os,
                "pread",
                side_effect=AssertionError(
                    "an unchanged persistent hit must not reread JSONL bytes"
                ),
            ):
                restarted = cycle72.load_authority_snapshot(
                    self.catalog,
                    self.inventory,
                    batch_size=2,
                    persistent_jsonl_cache=cache,
                )
        self.assertEqual(first.generation, restarted.generation)
        self.assertEqual(
            restarted.effective_statuses["asset-0001"]["status"], "accepted"
        )

    def test_persistent_source_change_evicts_only_failed_fd_then_reopens(self) -> None:
        self.write_catalog(self.rows(2))
        target = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_target",
            "status": "accepted",
        }], name="batch0000_target")
        untouched = self.write_status([{
            "asset_id": "asset-0002",
            "batch": "batch0000_untouched",
            "status": "accepted",
        }], name="batch0000_untouched")
        database = self.state / "authority_parse_cache.sqlite3"
        with authority_cache.PersistentAuthorityParseCache(database) as cache:
            cycle72.load_authority_snapshot(
                self.catalog,
                self.inventory,
                batch_size=2,
                persistent_jsonl_cache=cache,
            )
            target_key = cycle72._authority_cache_path_key(target)
            untouched_key = cycle72._authority_cache_path_key(untouched)
            target_entry = cycle72._AUTHORITY_FD_CACHE[target_key]
            untouched_entry = cycle72._AUTHORITY_FD_CACHE[untouched_key]
            catalog_entry = cycle72._AUTHORITY_FD_CACHE[
                cycle72._authority_cache_path_key(self.catalog)
            ]
            target_descriptor = target_entry.descriptor
            target_watcher = target_entry.watcher
            with cycle72._AUTHORITY_CACHE_LOCK:
                cycle72._JSONL_PARSE_CACHE.pop(target_key, None)

            with mock.patch.object(
                cache,
                "read_jsonl",
                side_effect=authority_cache.AuthoritySourceChanged(
                    "authority JSONL changed during read"
                ),
            ):
                with self.assertRaisesRegex(
                    cycle72.Cycle72DataError, "changed during read"
                ):
                    cycle72.load_authority_snapshot(
                        self.catalog,
                        self.inventory,
                        batch_size=2,
                        persistent_jsonl_cache=cache,
                    )

            self.assertNotIn(target_key, cycle72._AUTHORITY_FD_CACHE)
            self.assertIs(
                cycle72._AUTHORITY_FD_CACHE[untouched_key], untouched_entry
            )
            self.assertIs(
                cycle72._AUTHORITY_FD_CACHE[
                    cycle72._authority_cache_path_key(self.catalog)
                ],
                catalog_entry,
            )
            with self.assertRaises(OSError):
                os.fstat(target_descriptor)
            if target_watcher is not None:
                self.assertTrue(target_watcher.closed)
            os.fstat(untouched_entry.descriptor)
            os.fstat(catalog_entry.descriptor)

            recovered = cycle72.load_authority_snapshot(
                self.catalog,
                self.inventory,
                batch_size=2,
                persistent_jsonl_cache=cache,
            )

        self.assertIn(target_key, cycle72._AUTHORITY_FD_CACHE)
        self.assertIsNot(cycle72._AUTHORITY_FD_CACHE[target_key], target_entry)
        self.assertEqual(
            recovered.effective_statuses["asset-0001"]["status"],
            "accepted",
        )

    def test_persistent_authority_integration_rejects_bad_append(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        database = self.state / "authority_parse_cache.sqlite3"
        with authority_cache.PersistentAuthorityParseCache(database) as cache:
            cycle72.load_authority_snapshot(
                self.catalog,
                self.inventory,
                batch_size=2,
                persistent_jsonl_cache=cache,
            )
        cycle72._close_authority_fd_cache()
        with cycle72._AUTHORITY_CACHE_LOCK:
            cycle72._JSONL_PARSE_CACHE.clear()
        with status.open("ab") as handle:
            handle.write(b'{"asset_id":')
        with authority_cache.PersistentAuthorityParseCache(database) as cache:
            with self.assertRaisesRegex(
                cycle72.Cycle72DataError, "incomplete status JSONL line"
            ):
                cycle72.load_authority_snapshot(
                    self.catalog,
                    self.inventory,
                    batch_size=2,
                    persistent_jsonl_cache=cache,
                )
        with sqlite3.connect(database) as connection:
            cached = int(
                connection.execute("SELECT count(*) FROM jsonl_cache").fetchone()[0]
            )
        self.assertEqual(cached, 0)

    def test_authority_snapshot_reparses_only_the_changed_jsonl(self) -> None:
        self.write_catalog(self.rows(2))
        changed = self.write_status(
            [{
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "failed",
            }],
            name="batch0000_changed",
        )
        self.write_status(
            [{
                "asset_id": "asset-0002",
                "batch": "batch0000_worker",
                "status": "accepted",
            }],
            name="batch0000_unchanged",
        )
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        confirmed_size = changed.stat().st_size
        with changed.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "accepted",
            }) + "\n")

        with (
            mock.patch.object(
                cycle72, "_read_catalog", wraps=cycle72._read_catalog
            ) as read_catalog,
            mock.patch.object(
                cycle72, "_read_jsonl_strict", wraps=cycle72._read_jsonl_strict
            ) as read_jsonl,
            mock.patch.object(
                cycle72,
                "_read_jsonl_incremental_strict",
                wraps=cycle72._read_jsonl_incremental_strict,
            ) as read_incremental,
            mock.patch.object(
                cycle72,
                "_read_jsonl_stable_bytes",
                wraps=cycle72._read_jsonl_stable_bytes,
            ) as read_bytes,
        ):
            refreshed = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )

        self.assertEqual(read_catalog.call_count, 0)
        self.assertEqual(read_jsonl.call_count, 0)
        self.assertEqual(read_incremental.call_count, 1)
        self.assertEqual(read_incremental.call_args.args[0], changed)
        self.assertEqual(read_bytes.call_count, 1)
        self.assertEqual(read_bytes.call_args.kwargs["start"], confirmed_size)
        self.assertEqual(
            refreshed.effective_statuses["asset-0001"]["status"], "accepted"
        )
        self.assertEqual(
            refreshed.effective_statuses["asset-0002"]["status"], "accepted"
        )

    def test_changed_cached_jsonl_that_becomes_malformed_still_fails_closed(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status(
            [{
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "accepted",
            }]
        )
        cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        with status.open("a", encoding="utf-8") as handle:
            handle.write('{"asset_id":')

        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "incomplete status JSONL line"
        ):
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )

    def test_completed_partial_append_recovers_via_full_revalidation(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "failed",
        }])
        cycle72.load_authority_snapshot(self.catalog, self.inventory, batch_size=2)
        appended = json.dumps({
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        })
        with status.open("a", encoding="utf-8") as handle:
            handle.write(appended)
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "incomplete status JSONL line"
        ):
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        with status.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        self.assertEqual(
            snapshot.effective_statuses["asset-0001"]["status"], "accepted"
        )

    def test_bad_complete_append_does_not_poison_prior_cache(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "failed",
        }])
        cycle72.load_authority_snapshot(self.catalog, self.inventory, batch_size=2)
        with status.open("a", encoding="utf-8") as handle:
            handle.write('{"asset_id":}\n')
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "malformed status JSONL"
        ):
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        status.write_text(
            json.dumps({
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "accepted",
            }) + "\n",
            encoding="utf-8",
        )
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        self.assertEqual(
            snapshot.effective_statuses["asset-0001"]["status"], "accepted"
        )

    def test_historical_bad_line_is_never_skipped_by_incremental_cache(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status(['{"asset_id":}'])
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "malformed status JSONL"
        ):
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        with status.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "accepted",
            }) + "\n")
        with mock.patch.object(
            cycle72,
            "_read_jsonl_incremental_strict",
            wraps=cycle72._read_jsonl_incremental_strict,
        ) as incremental:
            with self.assertRaisesRegex(
                cycle72.Cycle72DataError, "malformed status JSONL"
            ):
                cycle72.load_authority_snapshot(
                    self.catalog, self.inventory, batch_size=2
                )
        self.assertEqual(incremental.call_count, 0)

    def test_same_size_rewrite_truncate_and_replace_use_full_parse(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
            "reason": "alpha",
        }])
        cycle72.load_authority_snapshot(self.catalog, self.inventory, batch_size=2)

        original = status.read_text(encoding="utf-8")
        rewritten = original.replace('"alpha"', '"bravo"')
        self.assertEqual(len(original.encode()), len(rewritten.encode()))
        status.write_text(rewritten, encoding="utf-8")
        with (
            mock.patch.object(
                cycle72, "_read_jsonl_strict", wraps=cycle72._read_jsonl_strict
            ) as full,
            mock.patch.object(
                cycle72,
                "_read_jsonl_incremental_strict",
                wraps=cycle72._read_jsonl_incremental_strict,
            ) as incremental,
        ):
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        self.assertEqual(full.call_count, 1)
        self.assertEqual(incremental.call_count, 0)

        with status.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "failed",
            }) + "\n")
        cycle72.load_authority_snapshot(self.catalog, self.inventory, batch_size=2)
        status.write_text(rewritten, encoding="utf-8")
        with mock.patch.object(
            cycle72, "_read_jsonl_strict", wraps=cycle72._read_jsonl_strict
        ) as full:
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        self.assertEqual(full.call_count, 1)

        replacement = status.with_suffix(".replacement")
        replacement.write_text(original, encoding="utf-8")
        replacement.replace(status)
        with mock.patch.object(
            cycle72, "_read_jsonl_strict", wraps=cycle72._read_jsonl_strict
        ) as full:
            cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2
            )
        self.assertEqual(full.call_count, 1)

    def test_stale_identity_and_full_file_without_newline_fail_closed(self) -> None:
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        identity = cycle72._authority_file_identity(status)
        with status.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "changed before read"
        ):
            cycle72._read_jsonl_cached(status, identity)

        no_newline = self.inventory / "total_asset_render_status_batch0000_partial.jsonl"
        no_newline.write_text(json.dumps({"asset_id": "asset-0001"}), encoding="utf-8")
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "incomplete status JSONL line"
        ):
            cycle72._read_jsonl_cached(
                no_newline, cycle72._authority_file_identity(no_newline)
            )

    def test_jsonl_change_during_fd_read_is_rejected_and_not_cached(self) -> None:
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        identity = cycle72._authority_file_identity(status)
        real_fstat = cycle72.os.fstat
        calls = 0

        def changing_fstat(descriptor: int) -> object:
            nonlocal calls
            calls += 1
            observed = real_fstat(descriptor)
            if calls == 1:
                return observed
            return mock.Mock(
                st_dev=observed.st_dev,
                st_ino=observed.st_ino,
                st_size=observed.st_size + 1,
                st_mtime_ns=observed.st_mtime_ns + 1,
                st_ctime_ns=observed.st_ctime_ns + 1,
            )

        with mock.patch.object(cycle72.os, "fstat", side_effect=changing_fstat):
            with self.assertRaisesRegex(
                cycle72.Cycle72DataError, "changed during read"
            ):
                cycle72._read_jsonl_cached(status, identity)
        self.assertNotIn(status.resolve(), cycle72._JSONL_PARSE_CACHE)

    def test_failed_full_revalidation_invalidates_old_prefix(self) -> None:
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        identity = cycle72._authority_file_identity(status)
        cycle72._read_jsonl_cached(status, identity)
        original = status.read_text(encoding="utf-8")
        malformed_same_size = "[" + original[1:-2] + "]\n"
        self.assertEqual(len(original.encode()), len(malformed_same_size.encode()))
        status.write_text(malformed_same_size, encoding="utf-8")
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "malformed status JSONL"
        ):
            cycle72._read_jsonl_cached(
                status, cycle72._authority_file_identity(status)
            )
        self.assertNotIn(status.resolve(), cycle72._JSONL_PARSE_CACHE)

        status.write_text(
            original + json.dumps({
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "needs_review",
            }) + "\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                cycle72, "_read_jsonl_strict", wraps=cycle72._read_jsonl_strict
            ) as full,
            mock.patch.object(
                cycle72,
                "_read_jsonl_incremental_strict",
                wraps=cycle72._read_jsonl_incremental_strict,
            ) as incremental,
        ):
            rows = cycle72._read_jsonl_cached(
                status, cycle72._authority_file_identity(status)
            )
        self.assertEqual(full.call_count, 1)
        self.assertEqual(incremental.call_count, 0)
        self.assertEqual([line for line, _row in rows], [1, 2])

    def test_authority_snapshot_retries_when_file_changes_during_parse(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status(
            [{
                "asset_id": "asset-0001",
                "batch": "batch0000_worker",
                "status": "failed",
            }]
        )
        original = cycle72._read_jsonl_cached
        mutated = False
        cached_descriptors: list[int] = []

        def read_then_append(
            path: Path, identity: cycle72.AuthorityFileIdentity
        ) -> tuple[tuple[int, dict[str, object]], ...]:
            nonlocal mutated
            cached_descriptors.append(
                cycle72._AUTHORITY_FD_CACHE[
                    cycle72._authority_cache_path_key(path)
                ].descriptor
            )
            rows = original(path, identity)
            if not mutated:
                mutated = True
                with status.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "asset_id": "asset-0001",
                        "batch": "batch0000_worker",
                        "status": "needs_review",
                    }) + "\n")
            return rows

        with mock.patch.object(
            cycle72, "_read_jsonl_cached", side_effect=read_then_append
        ) as read_jsonl:
            snapshot = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2, attempts=2
            )

        self.assertEqual(read_jsonl.call_count, 2)
        self.assertEqual(len(set(cached_descriptors)), 1)
        self.assertEqual(
            snapshot.effective_statuses["asset-0001"]["status"], "needs_review"
        )

    def test_authority_snapshot_retries_when_status_file_set_changes(self) -> None:
        self.write_catalog(self.rows(2))
        first = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "accepted",
        }])
        original = cycle72._read_jsonl_cached
        added = False

        def read_then_add(
            path: Path, identity: cycle72.AuthorityFileIdentity
        ) -> tuple[tuple[int, dict[str, object]], ...]:
            nonlocal added
            rows = original(path, identity)
            if path == first and not added:
                added = True
                self.write_status([{
                    "asset_id": "asset-0002",
                    "batch": "batch0000_worker",
                    "status": "needs_review",
                }], name="batch0000_added")
            return rows

        with mock.patch.object(
            cycle72, "_read_jsonl_cached", side_effect=read_then_add
        ):
            snapshot = cycle72.load_authority_snapshot(
                self.catalog, self.inventory, batch_size=2, attempts=2
            )

        self.assertEqual(
            snapshot.effective_statuses["asset-0001"]["status"], "accepted"
        )
        self.assertEqual(
            snapshot.effective_statuses["asset-0002"]["status"], "needs_review"
        )

    def test_authority_snapshot_concurrent_append_reuses_fd_and_retries(self) -> None:
        self.write_catalog(self.rows(2))
        status = self.write_status([{
            "asset_id": "asset-0001",
            "batch": "batch0000_worker",
            "status": "failed",
        }])
        original = cycle72._read_jsonl_cached
        parsed = threading.Event()
        appended = threading.Event()
        first_read = True
        descriptors: list[int] = []

        def append_status() -> None:
            self.assertTrue(parsed.wait(2.0))
            with status.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "asset_id": "asset-0001",
                    "batch": "batch0000_worker",
                    "status": "accepted",
                }) + "\n")
            appended.set()

        def read_then_wait(
            path: Path, identity: cycle72.AuthorityFileIdentity
        ) -> tuple[tuple[int, dict[str, object]], ...]:
            nonlocal first_read
            descriptors.append(
                cycle72._AUTHORITY_FD_CACHE[
                    cycle72._authority_cache_path_key(path)
                ].descriptor
            )
            rows = original(path, identity)
            if first_read:
                first_read = False
                parsed.set()
                self.assertTrue(appended.wait(2.0))
            return rows

        writer = threading.Thread(target=append_status)
        writer.start()
        try:
            with mock.patch.object(
                cycle72, "_read_jsonl_cached", side_effect=read_then_wait
            ):
                snapshot = cycle72.load_authority_snapshot(
                    self.catalog, self.inventory, batch_size=2, attempts=2
                )
        finally:
            writer.join(2.0)

        self.assertFalse(writer.is_alive())
        self.assertEqual(len(set(descriptors)), 1)
        self.assertEqual(
            snapshot.effective_statuses["asset-0001"]["status"], "accepted"
        )

    def test_priority_registration_is_immutable_and_rejects_duplicate_assets(self) -> None:
        self.start()
        items = self.priority_items()
        registered = self.register_priority(items=items)
        repeated = self.register_priority(items=items)
        self.assertEqual(registered["generation"], repeated["generation"])
        self.assertEqual(repeated["item_count"], 2)

        changed = [dict(item) for item in items]
        changed[0]["model_file"] = changed[1]["model_file"]
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "immutable"):
            self.register_priority(items=changed)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "state API"):
            self.register_priority(items=items, state="paused")

        duplicate_asset = [dict(item) for item in items]
        duplicate_asset[1]["asset_id"] = duplicate_asset[0]["asset_id"]
        duplicate_asset[1]["identity_key"] = "another-identity"
        with self.assertRaisesRegex(
            cycle72.Cycle72DataError, "duplicate asset_id"
        ):
            self.register_priority(
                items=duplicate_asset,
                generation="b" * 64,
            )

        with sqlite3.connect(self.db) as connection:
            current = connection.execute(
                "SELECT value FROM metadata "
                "WHERE key='current_priority_workload:highqal_source'"
            ).fetchone()[0]
            item_count = connection.execute(
                "SELECT COUNT(*) FROM priority_work_items"
            ).fetchone()[0]
        self.assertEqual(current, "a" * 64)
        self.assertEqual(item_count, 2)

    def test_priority_generation_switch_is_one_way_and_state_transitions_are_explicit(self) -> None:
        self.start()
        first_items = self.priority_items(1)
        self.register_priority(items=first_items, state="paused")
        cycle72.set_priority_workload_state(
            db_path=self.db,
            workload_kind="highqal_source",
            state="active",
            now_epoch=106,
        )
        cycle72.set_priority_workload_state(
            db_path=self.db,
            workload_kind="highqal_source",
            state="drained",
            now_epoch=107,
        )
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "cannot be resumed"):
            cycle72.set_priority_workload_state(
                db_path=self.db,
                workload_kind="highqal_source",
                state="active",
                now_epoch=108,
            )

        second_items = self.priority_items(1)
        second_items[0] = {
            **second_items[0],
            "work_item_id": "priority-item-wave2",
            "asset_id": "priority-asset-wave2",
            "identity_key": "priority-identity-wave2",
        }
        self.register_priority(
            items=second_items,
            generation="b" * 64,
            state="active",
        )
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "historical"):
            self.register_priority(
                items=first_items,
                generation="a" * 64,
                state="drained",
            )

    def test_terminal_rejected_priority_generation_is_atomically_replaced_paused(self) -> None:
        self.start()
        items = self.priority_items(2)
        self.register_priority(items=items, generation="a" * 64)
        leases = [self.claim(index, now=106 + index) for index in range(2)]
        for lease, status in zip(leases, ("needs_review", "failed")):
            cycle72.record_priority_workload_outcome(
                db_path=self.db,
                lease_id=lease["lease_id"],
                worker_index=lease["worker_index"],
                node_boot_id="boot-a",
                workload_kind="highqal_source",
                workload_generation="a" * 64,
                work_item_id=lease["work_item_id"],
                outcome={"status": status, "asset_id": lease["asset_id"]},
                now_epoch=110,
            )

        replacement_manifest = self.state / f"priority-{'b' * 64}.json"
        replacement_manifest.write_text("{}\n", encoding="utf-8")
        replacement = cycle72.register_priority_workload(
            db_path=self.db,
            workload_kind="highqal_source",
            generation="b" * 64,
            manifest_path=replacement_manifest,
            items=items,
            priority=100,
            state="paused",
            config={"contract": "highqal-test-v1"},
            inventory=self.inventory,
            expected_current_generation="a" * 64,
            require_current_terminal_rejected=True,
            now_epoch=111,
        )

        self.assertEqual(replacement["generation"], "b" * 64)
        self.assertEqual(replacement["state"], "paused")
        self.assertEqual(replacement["counts"], {"ready": 2})
        with sqlite3.connect(self.db) as connection:
            old_state = connection.execute(
                "SELECT state FROM priority_workloads WHERE workload_kind=? AND generation=?",
                ("highqal_source", "a" * 64),
            ).fetchone()[0]
            current = connection.execute(
                "SELECT value FROM metadata WHERE key='current_priority_workload:highqal_source'"
            ).fetchone()[0]
        self.assertEqual(old_state, "drained")
        self.assertEqual(current, "b" * 64)

    def test_terminal_retry_rejects_nonterminal_boundary(self) -> None:
        self.start()
        items = self.priority_items(1)
        self.register_priority(items=items, generation="a" * 64)
        replacement_manifest = self.state / f"priority-{'b' * 64}.json"
        replacement_manifest.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(
            cycle72.Cycle72Conflict, "not terminal rejected"
        ):
            cycle72.register_priority_workload(
                db_path=self.db,
                workload_kind="highqal_source",
                generation="b" * 64,
                manifest_path=replacement_manifest,
                items=items,
                priority=100,
                state="paused",
                config={"contract": "highqal-test-v1"},
                inventory=self.inventory,
                expected_current_generation="a" * 64,
                require_current_terminal_rejected=True,
                now_epoch=108,
            )
        status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        self.assertEqual(status["generation"], "a" * 64)

    def test_terminal_retry_rejects_accepted_only_boundary(self) -> None:
        self.start()
        items = self.priority_items(1)
        self.register_priority(items=items, generation="a" * 64)
        lease = self.claim(0, now=106)
        cycle72.record_priority_workload_outcome(
            db_path=self.db,
            lease_id=lease["lease_id"],
            worker_index=lease["worker_index"],
            node_boot_id="boot-a",
            workload_kind="highqal_source",
            workload_generation="a" * 64,
            work_item_id=lease["work_item_id"],
            outcome={"status": "accepted", "asset_id": lease["asset_id"]},
            now_epoch=107,
        )
        replacement_manifest = self.state / f"priority-{'b' * 64}.json"
        replacement_manifest.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(
            cycle72.Cycle72Conflict, "not terminal rejected"
        ):
            cycle72.register_priority_workload(
                db_path=self.db,
                workload_kind="highqal_source",
                generation="b" * 64,
                manifest_path=replacement_manifest,
                items=items,
                priority=100,
                state="paused",
                config={"contract": "highqal-test-v1"},
                inventory=self.inventory,
                expected_current_generation="a" * 64,
                require_current_terminal_rejected=True,
                now_epoch=108,
            )

    def test_active_total_asset_is_not_preempted_but_next_slot_takes_priority(self) -> None:
        self.start()
        total = self.claim(0, now=101)
        self.register_priority(items=self.priority_items(1))

        same = self.claim(0, now=106)
        priority = self.claim(1, now=106)

        self.assertEqual(same["lease_id"], total["lease_id"])
        self.assertEqual(same["workload_kind"], "total_asset")
        self.assertEqual(priority["workload_kind"], "highqal_source")
        self.assertEqual(priority["work_item_id"], "priority-item-0001")
        self.assertEqual(priority["render_batch"], "highqal")

    def test_priority_registration_during_total_scan_fences_stale_claim(self) -> None:
        self.start()
        real_preselect = cycle72._preselect_claim_candidates
        registered = False

        def preselect_then_register(*args: object, **kwargs: object) -> object:
            nonlocal registered
            candidates = real_preselect(*args, **kwargs)
            if not registered:
                registered = True
                self.register_priority(items=self.priority_items(1))
            return candidates

        with mock.patch.object(
            cycle72,
            "_preselect_claim_candidates",
            side_effect=preselect_then_register,
        ):
            with self.assertRaisesRegex(
                cycle72.Cycle72Conflict,
                "current active priority workload changed during claim",
            ):
                self.claim(0, now=106)

        with sqlite3.connect(self.db) as connection:
            active_count = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE state='active'"
            ).fetchone()[0]
        self.assertEqual(active_count, 0)
        priority = self.claim(0, now=107)
        self.assertEqual(priority["workload_kind"], "highqal_source")
        self.assertEqual(priority["work_item_id"], "priority-item-0001")

    def test_priority_activation_during_total_scan_fences_stale_claim(self) -> None:
        self.start()
        self.register_priority(items=self.priority_items(1), state="paused")
        real_preselect = cycle72._preselect_claim_candidates
        activated = False

        def preselect_then_activate(*args: object, **kwargs: object) -> object:
            nonlocal activated
            candidates = real_preselect(*args, **kwargs)
            if not activated:
                activated = True
                cycle72.set_priority_workload_state(
                    db_path=self.db,
                    workload_kind="highqal_source",
                    state="active",
                    now_epoch=106,
                )
            return candidates

        with mock.patch.object(
            cycle72,
            "_preselect_claim_candidates",
            side_effect=preselect_then_activate,
        ):
            with self.assertRaisesRegex(
                cycle72.Cycle72Conflict,
                "current active priority workload changed during claim",
            ):
                self.claim(0, now=106)

        with sqlite3.connect(self.db) as connection:
            active_count = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE state='active'"
            ).fetchone()[0]
        self.assertEqual(active_count, 0)
        priority = self.claim(0, now=107)
        self.assertEqual(priority["workload_kind"], "highqal_source")
        self.assertEqual(priority["work_item_id"], "priority-item-0001")

    def test_priority_pause_resume_aba_during_total_scan_fences_stale_claim(
        self,
    ) -> None:
        self.start()
        self.register_priority(
            items=self.priority_items(1, suffix=".fbx"),
            state="active",
        )
        real_preselect = cycle72._preselect_claim_candidates
        toggled = False

        def preselect_during_pause_resume(
            *args: object, **kwargs: object
        ) -> object:
            nonlocal toggled
            candidates = real_preselect(*args, **kwargs)
            if not toggled:
                toggled = True
                cycle72.set_priority_workload_state(
                    db_path=self.db,
                    workload_kind="highqal_source",
                    state="paused",
                    now_epoch=106,
                )
                cycle72.set_priority_workload_state(
                    db_path=self.db,
                    workload_kind="highqal_source",
                    state="active",
                    now_epoch=106,
                )
            return candidates

        with mock.patch.object(
            cycle72,
            "_preselect_claim_candidates",
            side_effect=preselect_during_pause_resume,
        ):
            with self.assertRaisesRegex(
                cycle72.Cycle72Conflict,
                "current active priority workload changed during claim",
            ):
                self.claim(
                    0,
                    now=106,
                    capabilities=cycle72.SlotCapabilities.normalized(
                        allowed_model_formats=[".blend"]
                    ),
                )

        with sqlite3.connect(self.db) as connection:
            active_count = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE state='active'"
            ).fetchone()[0]
        self.assertEqual(active_count, 0)
        total = self.claim(
            0,
            now=107,
            capabilities=cycle72.SlotCapabilities.normalized(
                allowed_model_formats=[".blend"]
            ),
        )
        self.assertEqual(total["workload_kind"], "total_asset")

    def test_incompatible_priority_item_falls_back_to_total_asset(self) -> None:
        self.start()
        self.register_priority(items=self.priority_items(1, suffix=".fbx"))
        claimed = self.claim(
            0,
            now=106,
            capabilities=cycle72.SlotCapabilities.normalized(
                allowed_model_formats=[".blend"]
            ),
        )
        self.assertEqual(claimed["workload_kind"], "total_asset")
        self.assertEqual(claimed["asset_id"], "asset-0001")
        status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        self.assertEqual(status["counts"], {"ready": 1})

    def test_concurrent_priority_claims_do_not_duplicate_asset_or_work_item(self) -> None:
        self.start()
        self.register_priority(items=self.priority_items(2))
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        barrier = threading.Barrier(3)
        results: list[dict[str, object] | None] = []
        errors: list[Exception] = []

        def run(slot: int) -> None:
            try:
                barrier.wait()
                results.append(self.claim(slot, now=106, snapshot=snapshot))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(slot,)) for slot in (0, 1)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {item["work_item_id"] for item in results},
            {"priority-item-0001", "priority-item-0002"},
        )
        self.assertEqual(
            {item["asset_id"] for item in results},
            {"priority-asset-0001", "priority-asset-0002"},
        )
        with sqlite3.connect(self.db) as connection:
            active = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT asset_id), "
                "COUNT(DISTINCT workload_kind || ':' || work_item_id), "
                "COUNT(DISTINCT worker_index) FROM leases WHERE state='active'"
            ).fetchone()
        self.assertEqual(active, (2, 2, 2, 2))

    def test_priority_boot_recovery_and_release_preserve_exact_item(self) -> None:
        self.start()
        self.register_priority(items=self.priority_items(1))
        original = self.claim(0, now=106, ttl=5)

        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "boot changed"):
            self.claim(0, now=112, boot="boot-b")
        recovered = self.claim(
            0,
            now=113,
            boot="boot-b",
            confirm_remote_idle=True,
        )
        self.assertEqual(recovered["lease_id"], original["lease_id"])
        self.assertEqual(recovered["work_item_id"], original["work_item_id"])
        self.assertEqual(recovered["recovery_count"], 1)

        released = cycle72.release_lease(
            db_path=self.db,
            lease_id=str(original["lease_id"]),
            worker_index=self.slots[0].worker_index,
            observed_node_boot_id="boot-b",
            reason="remote_boot_verified_idle",
            confirm_remote_idle=True,
            now_epoch=114,
        )
        self.assertEqual(released["state"], "released")
        status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        self.assertEqual(status["counts"], {"ready": 1})
        retried = self.claim(0, now=115, boot="boot-b")
        self.assertEqual(retried["work_item_id"], original["work_item_id"])
        self.assertEqual(retried["attempt"], 2)

    def test_priority_outcome_is_atomic_idempotent_and_immutable(self) -> None:
        self.start()
        self.register_priority(items=self.priority_items(1))
        lease = self.claim(0, now=106)
        outcome = {
            "status": "accepted",
            "asset_id": lease["asset_id"],
            "artifact_sha256": "c" * 64,
        }
        kwargs = {
            "db_path": self.db,
            "lease_id": lease["lease_id"],
            "worker_index": self.slots[0].worker_index,
            "node_boot_id": "boot-a",
            "workload_kind": "highqal_source",
            "workload_generation": "a" * 64,
            "work_item_id": lease["work_item_id"],
            "now_epoch": 107,
        }
        completed = cycle72.record_priority_workload_outcome(
            **kwargs, outcome=outcome
        )
        repeated = cycle72.record_priority_workload_outcome(
            **kwargs, outcome=outcome
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(repeated["lease_id"], completed["lease_id"])
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "immutable"):
            cycle72.record_priority_workload_outcome(
                **kwargs,
                outcome={**outcome, "status": "failed"},
            )
        status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        self.assertEqual(status["counts"], {"accepted": 1})

    def test_strict_canary_adoption_is_auditable_idempotent_and_skips_rerender(self) -> None:
        self.start()
        receipt, _items = self.adoption_fixture(count=2)
        first = cycle72.adopt_priority_canary_receipt(
            db_path=self.db,
            workload_kind="highqal_source",
            workload_generation="d" * 64,
            receipt=receipt,
            now_epoch=110,
        )
        repeated = cycle72.adopt_priority_canary_receipt(
            db_path=self.db,
            workload_kind="highqal_source",
            workload_generation="d" * 64,
            receipt=receipt,
            now_epoch=120,
        )

        self.assertEqual(first["adopted_count"], 2)
        self.assertEqual(first["already_adopted_count"], 0)
        self.assertEqual(repeated["adopted_count"], 0)
        self.assertEqual(repeated["already_adopted_count"], 2)
        status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        self.assertEqual(status["counts"], {"accepted": 2})
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            leases = connection.execute(
                """SELECT * FROM leases
                   WHERE workload_kind='highqal_source' ORDER BY work_item_id"""
            ).fetchall()
            events = connection.execute(
                """SELECT COUNT(*) FROM lease_events
                   WHERE event_type='priority_workload_item_adopted'"""
            ).fetchone()[0]
            outcomes = [
                json.loads(row["outcome_json"])
                for row in connection.execute(
                    "SELECT outcome_json FROM priority_work_items ORDER BY work_item_id"
                )
            ]
        self.assertEqual(len(leases), 2)
        self.assertTrue(all(row["state"] == "completed" for row in leases))
        self.assertTrue(all(row["worker_index"] == -1 for row in leases))
        self.assertTrue(all(row["lease_id"].startswith("adopted-") for row in leases))
        self.assertEqual(events, 2)
        self.assertTrue(all(outcome["adopted"] for outcome in outcomes))
        self.assertTrue(all(outcome["published"] for outcome in outcomes))

        # The full workload is paused, and after resume both adopted items are
        # terminal, so the next boundary falls back to ordinary total_asset.
        cycle72.set_priority_workload_state(
            db_path=self.db,
            workload_kind="highqal_source",
            state="active",
            now_epoch=121,
        )
        claimed = self.claim(0, now=122)
        self.assertEqual(claimed["workload_kind"], "total_asset")

    def test_strict_canary_adoption_rejects_receipt_and_artifact_mismatches(self) -> None:
        self.start()
        receipt, _items = self.adoption_fixture(count=1)

        bad_schema = json.loads(json.dumps(receipt))
        bad_schema["schema"] = "highqal-canary-adoption-receipt.v0"
        with self.assertRaisesRegex(cycle72.Cycle72DataError, "schema"):
            cycle72.adopt_priority_canary_receipt(
                db_path=self.db,
                workload_kind="highqal_source",
                workload_generation="d" * 64,
                receipt=bad_schema,
            )

        bad_generation = json.loads(json.dumps(receipt))
        bad_generation["generation"] = "0" * 64
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "generation"):
            cycle72.adopt_priority_canary_receipt(
                db_path=self.db,
                workload_kind="highqal_source",
                workload_generation="d" * 64,
                receipt=bad_generation,
            )

        bad_full = json.loads(json.dumps(receipt))
        bad_full["full_generation"] = "f" * 64
        bad_full = self.seal_adoption_receipt(bad_full)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "full_generation"):
            cycle72.adopt_priority_canary_receipt(
                db_path=self.db,
                workload_kind="highqal_source",
                workload_generation="d" * 64,
                receipt=bad_full,
            )

        bad_identity = json.loads(json.dumps(receipt))
        bad_identity["items"][0]["identity_key"] = "different-identity"
        bad_identity = self.seal_adoption_receipt(bad_identity)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "identity_key"):
            cycle72.adopt_priority_canary_receipt(
                db_path=self.db,
                workload_kind="highqal_source",
                workload_generation="d" * 64,
                receipt=bad_identity,
            )

        bad_status = json.loads(json.dumps(receipt))
        bad_status["items"][0]["canary_status_sha256"] = "1" * 64
        bad_status = self.seal_adoption_receipt(bad_status)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "status SHA-256"):
            cycle72.adopt_priority_canary_receipt(
                db_path=self.db,
                workload_kind="highqal_source",
                workload_generation="d" * 64,
                receipt=bad_status,
            )

        media = Path(receipt["items"][0]["media_path"])
        media.write_bytes(b"mutated-published-media")
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "media.*SHA-256"):
            cycle72.adopt_priority_canary_receipt(
                db_path=self.db,
                workload_kind="highqal_source",
                workload_generation="d" * 64,
                receipt=receipt,
            )

        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM priority_work_items"
                ).fetchone()[0],
                "ready",
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
                0,
            )

    def test_strict_canary_adoption_rolls_back_all_items_on_mid_transaction_failure(self) -> None:
        self.start()
        receipt, _items = self.adoption_fixture(count=2)
        real_event = cycle72._event
        event_count = 0

        def fail_second_adoption_event(*args: object, **kwargs: object) -> None:
            nonlocal event_count
            event_count += 1
            if event_count == 2:
                raise RuntimeError("injected adoption transaction failure")
            real_event(*args, **kwargs)

        with mock.patch.object(
            cycle72, "_event", side_effect=fail_second_adoption_event
        ):
            with self.assertRaisesRegex(RuntimeError, "transaction failure"):
                cycle72.adopt_priority_canary_receipt(
                    db_path=self.db,
                    workload_kind="highqal_source",
                    workload_generation="d" * 64,
                    receipt=receipt,
                    now_epoch=110,
                )

        with sqlite3.connect(self.db) as connection:
            states = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT state FROM priority_work_items ORDER BY work_item_id"
                )
            )
            leases = connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
            events = connection.execute(
                """SELECT COUNT(*) FROM lease_events
                   WHERE event_type='priority_workload_item_adopted'"""
            ).fetchone()[0]
        self.assertEqual(states, ("ready", "ready"))
        self.assertEqual(leases, 0)
        self.assertEqual(events, 0)

        completed = cycle72.adopt_priority_canary_receipt(
            db_path=self.db,
            workload_kind="highqal_source",
            workload_generation="d" * 64,
            receipt=receipt,
            now_epoch=111,
        )
        self.assertEqual(completed["adopted_count"], 2)

    def test_strict_canary_adoption_refuses_non_ready_full_item(self) -> None:
        self.start()
        receipt, _items = self.adoption_fixture(count=2)
        cycle72.set_priority_workload_state(
            db_path=self.db,
            workload_kind="highqal_source",
            state="active",
            now_epoch=106,
        )
        leased = self.claim(0, now=107)
        self.assertEqual(leased["workload_kind"], "highqal_source")

        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "only matching ready"):
            cycle72.adopt_priority_canary_receipt(
                db_path=self.db,
                workload_kind="highqal_source",
                workload_generation="d" * 64,
                receipt=receipt,
                now_epoch=108,
            )
        status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        self.assertEqual(status["counts"], {"leased": 1, "ready": 1})
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM leases
                       WHERE lease_id LIKE 'adopted-%'"""
                ).fetchone()[0],
                0,
            )

    def test_status_and_checkpoint_do_not_change_cycle_lease_or_priority_state(self) -> None:
        self.start(now=100)
        self.register_priority(items=self.priority_items(1))
        lease = self.claim(0, now=106)
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )

        def operational_rows() -> dict[str, tuple[tuple[object, ...], ...]]:
            with sqlite3.connect(self.db) as connection:
                return {
                    table: tuple(connection.execute(f"SELECT * FROM {table}"))
                    for table in (
                        "cycles",
                        "leases",
                        "priority_workloads",
                        "priority_work_items",
                    )
                }

        before = operational_rows()
        priority_status = cycle72.priority_workload_status(
            db_path=self.db, workload_kind="highqal_source"
        )
        status = cycle72.cycle_status(
            db_path=self.db,
            catalog_path=self.catalog,
            inventory=self.inventory,
            snapshot=snapshot,
            now_epoch=107,
        )
        checkpoint = cycle72.create_checkpoint(
            db_path=self.db,
            catalog_path=self.catalog,
            inventory=self.inventory,
            checkpoint_dir=self.state / "checkpoints",
            snapshot=snapshot,
            now_epoch=108,
        )
        after = operational_rows()

        self.assertTrue(priority_status["read_only"])
        self.assertEqual(status["active_leases"][0]["lease_id"], lease["lease_id"])
        self.assertTrue(checkpoint["continue_running"])
        self.assertEqual(after, before)

    def test_expiry_never_reassigns_and_boot_recovery_keeps_exact_lease(self) -> None:
        self.start()
        original = self.claim(0, now=101, ttl=5)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "expired on the same boot"):
            self.claim(0, now=107, boot="boot-a")
        # Another worker gets the second asset, never the expired first asset.
        other = self.claim(
            1,
            now=107,
            boot="boot-b",
            capabilities=cycle72.SlotCapabilities.normalized(
                attested_vulkan_families=["4.5"]
            ),
        )
        self.assertEqual(other["asset_id"], "asset-0002")
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "boot changed"):
            self.claim(0, now=107, boot="boot-new")
        recovered = self.claim(
            0,
            now=108,
            boot="boot-new",
            confirm_remote_idle=True,
        )
        self.assertEqual(recovered["lease_id"], original["lease_id"])
        self.assertEqual(recovered["asset_id"], "asset-0001")
        self.assertEqual(recovered["recovery_count"], 1)

    def test_heartbeat_can_renew_exact_expired_lease_after_local_restart(self) -> None:
        self.start()
        lease = self.claim(0, now=101, ttl=5)
        renewed = cycle72.heartbeat_lease(
            db_path=self.db,
            lease_id=lease["lease_id"],
            worker_index=self.slots[0].worker_index,
            node_boot_id="boot-a",
            lease_seconds=60,
            now_epoch=110,
        )
        self.assertEqual(renewed["expires_at_epoch"], 170)
        self.assertFalse(renewed["expired"])

    def test_worker_launch_attestation_requires_exact_current_active_lease(self) -> None:
        self.start()
        lease = self.claim(0, now=101, ttl=60)
        slot = self.slots[0]
        verified = cycle72.verify_active_lease(
            db_path=self.db,
            lease_id=lease["lease_id"],
            asset_id=lease["asset_id"],
            cycle_id="test-cycle",
            worker_index=slot.worker_index,
            remote_port=slot.remote_port,
            gpu=slot.gpu,
            node_boot_id="boot-a",
            now_epoch=102,
        )
        self.assertEqual(verified["lease_id"], lease["lease_id"])
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "mismatch"):
            cycle72.verify_active_lease(
                db_path=self.db,
                lease_id=lease["lease_id"],
                asset_id="wrong-asset",
                cycle_id="test-cycle",
                worker_index=slot.worker_index,
                remote_port=slot.remote_port,
                gpu=slot.gpu,
                node_boot_id="boot-a",
                now_epoch=102,
            )
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "expired"):
            cycle72.verify_active_lease(
                db_path=self.db,
                lease_id=lease["lease_id"],
                asset_id=lease["asset_id"],
                cycle_id="test-cycle",
                worker_index=slot.worker_index,
                remote_port=slot.remote_port,
                gpu=slot.gpu,
                node_boot_id="boot-a",
                now_epoch=162,
            )

    def test_complete_requires_landed_formal_outcome(self) -> None:
        self.start()
        lease = self.claim(0)
        kwargs = {
            "db_path": self.db,
            "catalog_path": self.catalog,
            "inventory": self.inventory,
            "lease_id": lease["lease_id"],
            "worker_index": self.slots[0].worker_index,
            "node_boot_id": "boot-a",
            "now_epoch": 110,
        }
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "must land"):
            cycle72.complete_lease(**kwargs)
        self.write_status([self.exact_lease_status(lease, "needs_review")])
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        with mock.patch.object(
            cycle72,
            "load_authority_snapshot",
            side_effect=AssertionError("complete reparsed shared authority"),
        ):
            completed = cycle72.complete_lease(**kwargs, snapshot=snapshot)
        self.assertEqual(completed["state"], "completed")
        next_lease = self.claim(0, now=111)
        self.assertEqual(next_lease["asset_id"], "asset-0002")

    def test_historical_terminal_cannot_complete_or_reconcile_current_lease(self) -> None:
        rows = self.rows(2)
        for row in rows:
            row["render_engine_hint"] = "CYCLES"
        self.start(rows=rows)
        lease = self.claim(0, now=101)
        stale = self.exact_lease_status(
            lease,
            "failed",
            lease_id="f" * 32,
            updated_at="2026-07-22T09:00:00Z",
        )
        self.write_status([stale])

        # A claim on another slot runs _reconcile_landed first.  The stale
        # same-asset terminal row must not complete this still-running lease.
        second = self.claim(1, now=102)
        self.assertEqual(second["asset_id"], "asset-0002")
        with sqlite3.connect(self.db) as connection:
            state = connection.execute(
                "SELECT state FROM leases WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone()[0]
            event_count = connection.execute(
                """SELECT COUNT(*) FROM lease_events
                   WHERE lease_id=? AND event_type='lease_completed_from_authority'""",
                (lease["lease_id"],),
            ).fetchone()[0]
        self.assertEqual(state, "active")
        self.assertEqual(event_count, 0)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "exact active lease"):
            cycle72.complete_lease(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                lease_id=str(lease["lease_id"]),
                worker_index=self.slots[0].worker_index,
                node_boot_id="boot-a",
                now_epoch=103,
            )

        exact = self.exact_lease_status(
            lease,
            "needs_review",
            updated_at="2026-07-22T10:00:00Z",
        )
        self.write_status([exact], name="batch0000_exact_lease")
        completed = cycle72.complete_lease(
            db_path=self.db,
            catalog_path=self.catalog,
            inventory=self.inventory,
            lease_id=str(lease["lease_id"]),
            worker_index=self.slots[0].worker_index,
            node_boot_id="boot-a",
            now_epoch=104,
        )
        self.assertEqual(completed["state"], "completed")

    def test_generation_and_attempt_mismatch_cannot_complete_exact_lease_id(self) -> None:
        self.start()
        lease = self.claim(0, now=101)
        wrong_generation = self.exact_lease_status(
            lease,
            "accepted",
            knowledge_generation="older-reviewed-generation",
            updated_at="2026-07-22T09:00:00Z",
        )
        self.write_status([wrong_generation])
        kwargs = {
            "db_path": self.db,
            "catalog_path": self.catalog,
            "inventory": self.inventory,
            "lease_id": str(lease["lease_id"]),
            "worker_index": self.slots[0].worker_index,
            "node_boot_id": "boot-a",
        }
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "exact active lease"):
            cycle72.complete_lease(**kwargs, now_epoch=102)

        wrong_attempt = self.exact_lease_status(
            lease,
            "accepted",
            lease_attempt=int(lease["attempt"]) + 1,
            updated_at="2026-07-22T10:00:00Z",
        )
        self.write_status(
            [wrong_attempt], name="batch0000_wrong_lease_attempt"
        )
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "exact active lease"):
            cycle72.complete_lease(**kwargs, now_epoch=103)

        self.write_status(
            [
                self.exact_lease_status(
                    lease,
                    "accepted",
                    updated_at="2026-07-22T11:00:00Z",
                )
            ],
            name="batch0000_exact_lease_attempt",
        )
        self.assertEqual(
            cycle72.complete_lease(**kwargs, now_epoch=104)["state"],
            "completed",
        )

    def test_boot_recovery_requires_terminal_from_recovered_lease_identity(self) -> None:
        self.start()
        original = self.claim(0, boot="boot-a", now=101)
        stale_before_recovery = self.exact_lease_status(original, "accepted")
        recovered = self.claim(
            0,
            boot="boot-b",
            now=102,
            confirm_remote_idle=True,
        )
        self.assertEqual(recovered["lease_id"], original["lease_id"])
        self.assertEqual(recovered["node_boot_id"], "boot-b")
        self.write_status([stale_before_recovery])
        kwargs = {
            "db_path": self.db,
            "catalog_path": self.catalog,
            "inventory": self.inventory,
            "lease_id": str(recovered["lease_id"]),
            "worker_index": self.slots[0].worker_index,
            "node_boot_id": "boot-b",
        }
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "exact active lease"):
            cycle72.complete_lease(**kwargs, now_epoch=103)
        self.write_status(
            [self.exact_lease_status(recovered, "accepted")],
            name="batch0000_recovered_lease",
        )
        self.assertEqual(
            cycle72.complete_lease(**kwargs, now_epoch=104)["state"],
            "completed",
        )

    def test_release_requires_remote_idle_and_allows_bounded_retry(self) -> None:
        self.start()
        lease = self.claim(0)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "remote-idle"):
            cycle72.release_lease(
                db_path=self.db,
                lease_id=lease["lease_id"],
                worker_index=self.slots[0].worker_index,
                observed_node_boot_id="boot-a",
                reason="transport_reset",
                confirm_remote_idle=False,
            )
        released = cycle72.release_lease(
            db_path=self.db,
            lease_id=lease["lease_id"],
            worker_index=self.slots[0].worker_index,
            observed_node_boot_id="boot-a",
            reason="remote_boot_verified_idle",
            confirm_remote_idle=True,
            now_epoch=110,
        )
        self.assertEqual(released["state"], "released")
        retried = self.claim(0, now=111)
        self.assertEqual(retried["asset_id"], lease["asset_id"])
        self.assertEqual(retried["attempt"], 2)

    def test_malformed_jsonl_fails_closed_without_a_claim(self) -> None:
        self.start()
        self.write_status(['{"asset_id":'], name="batch0000_bad")
        with self.assertRaisesRegex(cycle72.Cycle72DataError, "malformed status JSONL"):
            self.claim(0)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0
            )

    def test_checkpoint_is_observational_and_does_not_stop_claims(self) -> None:
        self.start(now=100)
        snapshot = cycle72.load_authority_snapshot(
            self.catalog, self.inventory, batch_size=2
        )
        with mock.patch.object(
            cycle72,
            "load_authority_snapshot",
            side_effect=AssertionError("status/checkpoint reparsed shared authority"),
        ):
            status = cycle72.cycle_status(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                snapshot=snapshot,
                now_epoch=100 + 72 * 3600 + 1,
            )
            checkpoint = cycle72.create_checkpoint(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                checkpoint_dir=self.state / "checkpoints",
                snapshot=snapshot,
                now_epoch=100 + 72 * 3600 + 1,
            )
        self.assertEqual(status["authority_generation"], snapshot.generation)
        self.assertTrue(checkpoint["checkpoint_due"])
        self.assertTrue(checkpoint["continue_running"])
        self.assertTrue(Path(checkpoint["checkpoint_path"]).is_file())
        self.assertIsNotNone(self.claim(0, now=100 + 72 * 3600 + 2))

    def test_cycle_rotation_refuses_to_orphan_active_launch_attestation(self) -> None:
        self.start()
        self.claim(0)
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "active leases exist"):
            cycle72.start_cycle(
                db_path=self.db,
                manifest_path=self.manifest,
                catalog_path=self.catalog,
                inventory=self.inventory,
                cycle_id="next-cycle",
                batch_min=0,
                batch_size=2,
                now_epoch=200,
            )
        verified = cycle72.verify_active_lease(
            db_path=self.db,
            lease_id=cycle72.cycle_status(
                db_path=self.db,
                catalog_path=self.catalog,
                inventory=self.inventory,
                now_epoch=102,
            )["active_leases"][0]["lease_id"],
            asset_id="asset-0001",
            cycle_id="test-cycle",
            worker_index=self.slots[0].worker_index,
            remote_port=self.slots[0].remote_port,
            gpu=self.slots[0].gpu,
            node_boot_id="boot-a",
            now_epoch=102,
        )
        self.assertEqual(verified["cycle_id"], "test-cycle")

    def test_drain_disables_new_claims_but_preserves_active_lease(self) -> None:
        self.start()
        lease = self.claim(0)
        drained = cycle72.drain_cycle(
            db_path=self.db, manifest_path=self.manifest, now_epoch=110
        )
        self.assertEqual(drained["state"], "draining")
        with self.assertRaisesRegex(cycle72.Cycle72Conflict, "new claims are disabled"):
            self.claim(1, now=111)
        renewed = cycle72.heartbeat_lease(
            db_path=self.db,
            lease_id=lease["lease_id"],
            worker_index=self.slots[0].worker_index,
            node_boot_id="boot-a",
            now_epoch=111,
        )
        self.assertEqual(renewed["state"], "active")


if __name__ == "__main__":
    unittest.main()
