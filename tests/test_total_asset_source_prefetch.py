from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_cycle72 as cycle72
import total_asset_source_prefetch as prefetch
from total_asset_local_source_spool import LocalReadySource, LocalSourcePrefetch


class TotalAssetSourcePrefetchTests(unittest.TestCase):
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
                        "allowed_batches": {"min": 3, "max": None},
                    }),
                ),
            )
        self.supervisor_state = self.root / "supervisor_state.json"
        self.supervisor_state.write_text(json.dumps({
            "schema": prefetch.SUPERVISOR_STATE_SCHEMA,
            "infrastructure_retries": {},
        }), encoding="utf-8")
        self.config = prefetch.PrefetchConfig(
            asset_root=self.asset_root,
            inventory=self.inventory,
            catalog=self.catalog,
            db_path=self.db,
            spool_root=self.root / "spool",
            state_path=self.root / "state.json",
            lock_path=self.root / "prefetch.lock",
            supervisor_state_path=self.supervisor_state,
            candidate_window=64,
            candidate_limit=16,
            max_source_bytes=1024**3,
            max_ready_entries=16,
            max_ready_bytes=2 * 1024**3,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def row(
        self,
        offset: int,
        *,
        batch: int = 3,
        suffix: str = ".blend",
        size: int = 20 * 1024**2,
    ) -> dict[str, str]:
        order = batch * 1000 + offset
        model = self.asset_root / f"source-{order}{suffix}"
        return {
            "asset_id": f"asset-{order}",
            "identity_key": f"identity-{order}",
            "inventory_status": "ready",
            "duplicate_of": "",
            "render_order": str(order),
            "render_batch": f"{batch:04d}",
            "model_file": str(model),
            "source_root": str(model.parent),
            "file_size": str(size),
        }

    def snapshot(
        self,
        rows: list[dict[str, str]],
        statuses: dict[str, dict[str, str]] | None = None,
    ) -> cycle72.AuthoritySnapshot:
        return cycle72.AuthoritySnapshot(
            tuple(rows),
            statuses or {},
            "generation",
            self.catalog,
            self.inventory,
            None,
            1000,
            time.time(),
        )

    def full_batch(self, batch: int = 3) -> list[dict[str, str]]:
        return [self.row(index, batch=batch) for index in range(1, 1001)]

    def test_selection_excludes_partial_terminal_active_unsupported_and_retry(self) -> None:
        partial = self.full_batch()[:-1]
        self.assertEqual(prefetch.select_prefetch_candidates(
            self.snapshot(partial),
            batch_min=3,
            batch_max=None,
            batch_size=1000,
            active_assets=frozenset(),
            excluded_assets=frozenset(),
            candidate_window=64,
            candidate_limit=16,
            max_source_bytes=1024**3,
        ), ())

        rows = self.full_batch()
        rows[2] = self.row(3, suffix=".max")
        statuses = {
            rows[0]["asset_id"]: {"status": "accepted"},
        }
        selected = prefetch.select_prefetch_candidates(
            self.snapshot(rows, statuses),
            batch_min=3,
            batch_max=None,
            batch_size=1000,
            active_assets=frozenset({rows[1]["asset_id"]}),
            excluded_assets=frozenset({rows[3]["asset_id"]}),
            candidate_window=64,
            candidate_limit=16,
            max_source_bytes=1024**3,
        )
        selected_ids = {row["asset_id"] for row in selected}
        self.assertNotIn(rows[0]["asset_id"], selected_ids)
        self.assertNotIn(rows[1]["asset_id"], selected_ids)
        self.assertNotIn(rows[2]["asset_id"], selected_ids)
        self.assertNotIn(rows[3]["asset_id"], selected_ids)
        self.assertTrue(selected)

    def test_selection_stays_in_earliest_incomplete_full_batch_and_prefers_small(self) -> None:
        batch3 = self.full_batch(3)
        batch4 = self.full_batch(4)
        batch3[0]["file_size"] = str(80 * 1024**2)
        batch3[1]["file_size"] = str(20 * 1024**2)
        selected = prefetch.select_prefetch_candidates(
            self.snapshot(batch3 + batch4),
            batch_min=3,
            batch_max=None,
            batch_size=1000,
            active_assets=frozenset(),
            excluded_assets=frozenset(),
            candidate_window=2,
            candidate_limit=2,
            max_source_bytes=1024**3,
        )
        self.assertEqual([row["render_batch"] for row in selected], ["0003", "0003"])
        self.assertEqual(selected[0]["asset_id"], batch3[1]["asset_id"])

    def test_selection_advances_when_earliest_tail_has_no_prefetchable_work(self) -> None:
        batch3 = self.full_batch(3)
        batch4 = self.full_batch(4)
        selected = prefetch.select_prefetch_candidates(
            self.snapshot(batch3 + batch4),
            batch_min=3,
            batch_max=None,
            batch_size=1000,
            active_assets=frozenset(),
            excluded_assets=frozenset(row["asset_id"] for row in batch3),
            candidate_window=2,
            candidate_limit=2,
            max_source_bytes=1024**3,
        )

        self.assertEqual([row["render_batch"] for row in selected], ["0004", "0004"])

    def test_remote_cache_reserve_filters_for_cache_eligible_sources(self) -> None:
        rows = self.full_batch()
        rows[0]["file_size"] = str(20 * 1024**2)
        rows[1]["file_size"] = str(80 * 1024**2)
        selected = prefetch.select_prefetch_candidates(
            self.snapshot(rows),
            batch_min=3,
            batch_max=None,
            batch_size=1000,
            active_assets=frozenset(),
            excluded_assets=frozenset(),
            candidate_window=64,
            candidate_limit=16,
            max_source_bytes=1024**3,
            min_source_bytes=64 * 1024**2,
        )
        self.assertTrue(selected)
        self.assertEqual(selected[0]["asset_id"], rows[1]["asset_id"])
        self.assertTrue(all(
            int(row["file_size"]) >= 64 * 1024**2 for row in selected
        ))

    def test_run_once_rechecks_active_lease_and_never_prefetches_it(self) -> None:
        rows = self.full_batch()
        candidate = rows[0]
        Path(candidate["model_file"]).write_bytes(b"blend" * 100)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO leases(asset_id, state) VALUES(?, 'active')",
                (candidate["asset_id"],),
            )
        status = {
            "cycle": {
                "cycle_id": "cycle-test",
                "state": "running",
                "allowed_batches": {"min": 3, "max": None},
            },
            # Deliberately stale: the exact read-only SQLite recheck must win.
            "active_leases": [],
        }
        with (
            mock.patch.object(prefetch, "MIN_BYTES", 1),
            mock.patch.object(prefetch, "prefetch_local_source") as stage,
        ):
            cycle, _active = cycle72.current_cycle_activity(db_path=self.db)
            payload = prefetch.run_once(
                self.config,
                authority_loader=lambda *_args, **_kwargs: self.snapshot(rows),
                cycle_activity_loader=lambda **_kwargs: (cycle, frozenset()),
            )
        stage.assert_not_called()
        self.assertEqual(payload["attempts"][0]["status"], "became_active")

    def test_run_once_prefetches_without_mutating_catalog_or_lease(self) -> None:
        rows = self.full_batch()
        candidate = rows[0]
        model = Path(candidate["model_file"])
        model.write_bytes(b"blend" * 100)
        status = {
            "cycle": {
                "cycle_id": "cycle-test",
                "state": "running",
                "allowed_batches": {"min": 3, "max": None},
            },
            "active_leases": [],
        }
        catalog_before = self.catalog.read_bytes()
        with sqlite3.connect(self.db) as connection:
            lease_before = connection.execute("SELECT * FROM leases").fetchall()
        result = LocalSourcePrefetch(
            "prefetched", "a" * 64, 500, 0.1, 0.2, self.root / "ready"
        )
        with (
            mock.patch.object(prefetch, "MIN_BYTES", 1),
            mock.patch.object(prefetch, "_candidate_plan", return_value=(
                model.parent, model, 500, True
            )),
            mock.patch.object(
                prefetch, "prefetch_local_source", return_value=result
            ) as stage,
        ):
            payload = prefetch.run_once(
                self.config,
                authority_loader=lambda *_args, **_kwargs: self.snapshot(rows),
            )
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["selected_asset_id"], candidate["asset_id"])
        stage.assert_called_once()
        self.assertEqual(self.catalog.read_bytes(), catalog_before)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT * FROM leases").fetchall(), lease_before)

    def test_malformed_retry_state_fails_closed(self) -> None:
        self.supervisor_state.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(
            prefetch.SourcePrefetchError, "supervisor_state_unreadable"
        ):
            prefetch.excluded_assets_from_supervisor(
                self.supervisor_state, now_epoch=time.time()
            )

    def test_sufficient_ready_cache_skips_repeated_full_authority_scan(self) -> None:
        current = 1000.0
        self.config.state_path.write_text(json.dumps({
            "schema": prefetch.STATE_SCHEMA,
            "cycle_id": "cycle-test",
            "authority_generation": "authority-test",
            "authority_scan_at_epoch": current - 10,
        }), encoding="utf-8")
        entries = tuple(
            LocalReadySource(
                cache_key=f"{index:064x}",
                identity_token=f"asset-ready-{index}",
                size_bytes=128 * 1024**2,
                model_only=False,
                remote_cache_key="f" * 64,
                ready_path=self.config.spool_root / f"ready-{index}",
            )
            for index in range(8)
        )
        with (
            mock.patch.object(prefetch, "ready_sources", return_value=entries),
            mock.patch.object(
                prefetch.cycle72,
                "load_authority_snapshot",
                side_effect=AssertionError("full authority scan was not skipped"),
            ) as authority_scan,
        ):
            payload = prefetch.run_once(
                self.config,
                authority_loader=prefetch.cycle72.load_authority_snapshot,
                now=lambda: current,
            )
        authority_scan.assert_not_called()
        self.assertEqual(payload["detail"], "ready_cache_sufficient")
        self.assertEqual(payload["ready_cache_count"], 8)
        self.assertEqual(payload["authority_generation"], "authority-test")

    def test_topology_watermark_covers_all_slots_plus_remote_reserve(self) -> None:
        cycle = {
            "topology": [
                {"worker_index": index, "remote_port": 30000, "gpu": index}
                for index in range(11)
            ]
        }
        self.assertEqual(
            prefetch.ready_cache_watermark(
                cycle,
                max_ready_entries=16,
                remote_cache_ready_reserve=2,
            ),
            13,
        )
        with self.assertRaisesRegex(
            prefetch.SourcePrefetchError, "cycle_topology_invalid"
        ):
            prefetch.ready_cache_watermark(
                {"topology": [
                    {"worker_index": 0},
                    {"worker_index": 0},
                ]},
                max_ready_entries=16,
                remote_cache_ready_reserve=2,
            )

    def test_eight_ready_entries_do_not_stop_eleven_slot_prefetch(self) -> None:
        current = 1000.0
        self.config.state_path.write_text(json.dumps({
            "schema": prefetch.STATE_SCHEMA,
            "cycle_id": "cycle-test",
            "authority_generation": "authority-test",
            "authority_scan_at_epoch": current - 10,
        }), encoding="utf-8")
        entries = tuple(
            LocalReadySource(
                cache_key=f"{index:064x}",
                identity_token=f"asset-ready-{index}",
                size_bytes=128 * 1024**2,
                model_only=False,
                remote_cache_key="f" * 64,
                ready_path=self.config.spool_root / f"ready-{index}",
            )
            for index in range(8)
        )
        cycle, active = cycle72.current_cycle_activity(db_path=self.db)
        cycle = {
            **cycle,
            "topology": [
                {"worker_index": index, "remote_port": 30000, "gpu": index}
                for index in range(11)
            ],
        }
        authority_loader = mock.Mock(return_value=self.snapshot(self.full_batch()))
        with (
            mock.patch.object(prefetch, "ready_sources", return_value=entries),
            mock.patch.object(prefetch, "_candidate_plan", return_value=None),
        ):
            payload = prefetch.run_once(
                self.config,
                authority_loader=authority_loader,
                cycle_activity_loader=lambda **_kwargs: (cycle, active),
                now=lambda: current,
            )

        authority_loader.assert_called_once()
        self.assertEqual(payload["ready_cache_count"], 8)
        self.assertEqual(payload["ready_cache_watermark"], 13)


if __name__ == "__main__":
    unittest.main()
