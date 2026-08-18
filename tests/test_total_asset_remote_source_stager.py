from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_total_asset_render_worker as worker
import total_asset_cycle72 as cycle72
import total_asset_local_source_spool as spool
import total_asset_remote_source_stager as stager
from storage_capacity import DiskCapacity


class TotalAssetRemoteSourceStagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.asset_root = self.root / "assets"
        self.inventory = self.asset_root / "_asset_inventory"
        self.inventory.mkdir(parents=True)
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.catalog.write_text("catalog unchanged\n", encoding="utf-8")
        self.db = self.root / "leases.sqlite3"
        with sqlite3.connect(self.db) as connection:
            connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE cycles(
                    cycle_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE leases(asset_id TEXT NOT NULL, state TEXT NOT NULL);
                INSERT INTO metadata(key, value)
                    VALUES('current_cycle_id', 'cycle-test');
                """
            )
            connection.execute(
                "INSERT INTO cycles(cycle_id, state, config_json) VALUES(?, ?, ?)",
                (
                    "cycle-test",
                    "running",
                    json.dumps({
                        "schema": cycle72.SCHEMA,
                        "cycle_id": "cycle-test",
                        "code_generation": "code-test",
                        "allowed_batches": {"min": 0, "max": None},
                    }),
                ),
            )
        self.spool_root = self.root / "spool"
        self.source = self.asset_root / "source"
        self.source.mkdir()
        self.model = self.source / "asset.blend"
        self.model.write_bytes(b"BLENDER-v500" + b"x" * 512)
        (self.source / "texture.png").write_bytes(b"texture")
        self.row = {
            "asset_id": "asset-1",
            "identity_key": "identity-1",
            "inventory_status": "ready",
            "duplicate_of": "",
            "render_order": "1",
            "render_batch": "0000",
            "model_file": str(self.model),
            "source_root": str(self.source),
            "file_size": "1000",
        }
        self.authority = cycle72.AuthoritySnapshot(
            (self.row,),
            {},
            "generation",
            self.catalog,
            self.inventory,
            None,
            1,
            1.0,
        )
        self.config = stager.RemoteSourceStagerConfig(
            inventory=self.inventory,
            catalog=self.catalog,
            db_path=self.db,
            spool_root=self.spool_root,
            state_path=self.root / "state.json",
            lock_path=self.root / "stager.lock",
            remote_port=30773,
            batch_size=1,
            min_source_bytes=1,
        )
        healthy = DiskCapacity(2 * 1024**4, 0, 2 * 1024**4, "test")
        with (
            mock.patch.object(spool, "MIN_BYTES", 1),
            mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
        ):
            self.ready = spool.prefetch_local_source(
                self.source,
                self.model,
                size_bytes=1000,
                model_only=False,
                spool_root=self.spool_root,
                capacity_probe=lambda _path: healthy,
                identity_token="asset-1",
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_once(self, remote_factory, *, cycle_activity_loader=None):
        loader = cycle_activity_loader or cycle72.current_cycle_activity
        with mock.patch.object(worker, "SOURCE_BUNDLE_CACHE_MIN_BYTES", 1):
            return stager.run_once(
                self.config,
                cycle_activity_loader=loader,
                remote_factory=remote_factory,
                now=lambda: 10.0,
            )

    def test_once_warms_cache_without_gpu_lease_or_formal_mutation(self) -> None:
        catalog_before = self.catalog.read_bytes()
        with sqlite3.connect(self.db) as connection:
            leases_before = connection.execute("SELECT * FROM leases").fetchall()

        class CacheHitRemote:
            def __init__(self, port: int, gpu: int):
                self.port = port
                self.gpu = gpu

            def validate_transport(self) -> None:
                return None

            def source_cache_claim(
                self, _key: str, _token: str, *, required_bytes: int = 0
            ) -> str:
                self.required_bytes = required_bytes
                return "ready"

            def configure_gpu(self) -> None:
                raise AssertionError("zero-model stager configured a GPU")

            def source_cache_stage(self, *_args: object) -> None:
                raise AssertionError("zero-model stager touched a private GPU workspace")

        with (
            mock.patch.object(
                cycle72,
                "load_authority_snapshot",
                side_effect=AssertionError("stager parsed formal authority"),
            ) as authority_scan,
            mock.patch.object(
                cycle72,
                "cycle_status",
                side_effect=AssertionError("stager built full cycle status"),
            ) as full_status,
        ):
            payload = self.run_once(CacheHitRemote)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["detail"], "remote_cache_hit")
        self.assertTrue(payload["zero_model"])
        entries = spool.ready_sources(spool_root=self.spool_root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].remote_cache_key, payload["remote_cache_key"])
        self.assertEqual(self.catalog.read_bytes(), catalog_before)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM leases").fetchall(), leases_before
            )
        self.assertEqual(list(self.spool_root.glob(".spool.remote.*")), [])
        authority_scan.assert_not_called()
        full_status.assert_not_called()

    def test_exact_active_lease_recheck_prevents_all_remote_mutation(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO leases(asset_id, state) VALUES('asset-1', 'active')"
            )

        def forbidden_remote(_port: int, _gpu: int):
            raise AssertionError("remote transport created for an active asset")

        cycle, _active = cycle72.current_cycle_activity(db_path=self.db)
        payload = self.run_once(
            forbidden_remote,
            cycle_activity_loader=lambda **_kwargs: (cycle, frozenset()),
        )
        self.assertEqual(payload["detail"], "ready_source_became_active")
        self.assertEqual(
            spool.ready_sources(spool_root=self.spool_root)[0].remote_cache_key,
            "",
        )

    def test_busy_population_is_nonblocking_and_keeps_ready_unmarked(self) -> None:
        class BusyRemote:
            def __init__(self, _port: int, _gpu: int):
                pass

            def validate_transport(self) -> None:
                return None

            def source_cache_claim(
                self, _key: str, _token: str, *, required_bytes: int = 0
            ) -> str:
                del required_bytes
                return "busy"

            def source_cache_stage(self, *_args: object) -> None:
                raise AssertionError("busy warmer staged a GPU workspace")

        payload = self.run_once(BusyRemote)
        self.assertEqual(payload["status"], "observing")
        self.assertEqual(payload["detail"], "worker_source_cache_population_busy")
        self.assertEqual(
            spool.ready_sources(spool_root=self.spool_root)[0].remote_cache_key,
            "",
        )
        self.assertEqual(list(self.spool_root.glob(".spool.remote.*")), [])

    def test_malformed_cycle_state_fails_closed_before_snapshot_or_remote(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM metadata WHERE key='current_cycle_id'")
        with self.assertRaisesRegex(cycle72.Cycle72DataError, "current production"):
            stager.run_once(
                self.config,
                remote_factory=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("remote called")
                ),
            )


if __name__ == "__main__":
    unittest.main()
