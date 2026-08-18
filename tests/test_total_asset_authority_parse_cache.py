from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import total_asset_authority_parse_cache as cache_module  # noqa: E402


class PersistentAuthorityParseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.inventory.mkdir()
        self.status = self.inventory / "total_asset_render_status_batch0003.jsonl"
        self.database = self.root / "local-apfs" / "authority.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def row(asset_id: str, status: str = "accepted") -> dict[str, str]:
        return {
            "asset_id": asset_id,
            "batch": "batch0003_worker",
            "status": status,
        }

    def write_rows(self, *rows: dict[str, str]) -> bytes:
        data = b"".join(
            json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows
        )
        self.status.write_bytes(data)
        return data

    def append_row(self, row: dict[str, str]) -> bytes:
        data = json.dumps(row, sort_keys=True).encode("utf-8") + b"\n"
        with self.status.open("ab") as handle:
            handle.write(data)
        return data

    def identity(self) -> cache_module.AuthorityFileIdentity:
        metadata = self.status.stat()
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def dirent_inode(self) -> int:
        with os.scandir(self.inventory) as entries:
            return next(
                int(entry.inode()) for entry in entries if entry.name == self.status.name
            )

    def read(
        self, cache: cache_module.PersistentAuthorityParseCache
    ) -> cache_module.CachedJsonlRead:
        return cache.read_jsonl(
            self.status,
            dirent_inode=self.dirent_inode(),
            identity=self.identity(),
        )

    def cache_count(self) -> int:
        with sqlite3.connect(self.database) as connection:
            return int(connection.execute("SELECT count(*) FROM jsonl_cache").fetchone()[0])

    def test_restart_uses_persistent_rows_without_reading_source_content(self) -> None:
        original = self.write_rows(self.row("asset-a"), self.row("asset-b"))
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            cold = self.read(cache)
        self.assertEqual(cold.source, "full")
        self.assertEqual(cold.prefix_sha256, cache_module.hashlib.sha256(original).hexdigest())

        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with mock.patch.object(
                cache_module.os,
                "pread",
                side_effect=AssertionError("persistent hit must not read ExFAT content"),
            ):
                warm = self.read(cache)
        self.assertEqual(warm.source, "persistent_hit")
        self.assertEqual([line for line, _row in warm.rows], [1, 2])

    def test_restart_append_verifies_prefix_and_parses_only_tail(self) -> None:
        original = self.write_rows(self.row("asset-a", "failed"))
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
        tail = self.append_row(self.row("asset-a", "accepted"))

        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with mock.patch.object(
                cache_module,
                "_parse_jsonl_bytes",
                wraps=cache_module._parse_jsonl_bytes,
            ) as parse:
                result = self.read(cache)
        self.assertEqual(result.source, "incremental")
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(parse.call_args.args[1], tail)
        self.assertEqual(parse.call_args.kwargs["first_line_number"], 2)
        self.assertEqual(
            result.prefix_sha256,
            cache_module.hashlib.sha256(original + tail).hexdigest(),
        )
        self.assertEqual([line for line, _row in result.rows], [1, 2])

    def test_prefix_rewrite_then_growth_is_not_mistaken_for_append(self) -> None:
        original = self.write_rows(
            self.row("asset-a", "failed"), self.row("asset-b", "accepted")
        )
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)

        changed = original.replace(b'"failed"', b'"broken"')
        self.assertEqual(len(changed), len(original))
        self.status.write_bytes(changed + json.dumps(self.row("asset-c")).encode() + b"\n")
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with self.assertRaisesRegex(
                cache_module.AuthorityPrefixMismatch, "cached prefix"
            ):
                self.read(cache)
        self.assertEqual(self.cache_count(), 0)

    def test_truncation_forces_full_parse_and_drops_cached_rows(self) -> None:
        first = json.dumps(self.row("asset-a"), sort_keys=True).encode() + b"\n"
        self.write_rows(self.row("asset-a"), self.row("asset-b"))
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
        self.status.write_bytes(first)

        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            result = self.read(cache)
        self.assertEqual(result.source, "full")
        self.assertEqual([row[1]["asset_id"] for row in result.rows], ["asset-a"])

    def test_replacement_forces_full_parse(self) -> None:
        self.write_rows(self.row("asset-a"))
        old_dirent = self.dirent_inode()
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
        replacement = self.inventory / "replacement.jsonl"
        replacement.write_text(
            json.dumps(self.row("asset-new"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        replacement.replace(self.status)
        self.assertNotEqual(self.dirent_inode(), old_dirent)

        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            result = self.read(cache)
        self.assertEqual(result.source, "full")
        self.assertEqual(result.rows[0][1]["asset_id"], "asset-new")

    def test_bad_append_is_rejected_and_invalidates_old_prefix(self) -> None:
        self.write_rows(self.row("asset-a"))
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
        with self.status.open("ab") as handle:
            handle.write(b'{"asset_id":')

        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with self.assertRaisesRegex(
                cache_module.AuthorityJsonlError, "incomplete"
            ):
                self.read(cache)
        self.assertEqual(self.cache_count(), 0)

        with self.status.open("ab") as handle:
            handle.write(b'"asset-b"}\n')
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            repaired = self.read(cache)
        self.assertEqual(repaired.source, "full")
        self.assertEqual([line for line, _row in repaired.rows], [1, 2])

    def test_malformed_complete_line_is_never_cached(self) -> None:
        self.write_rows(self.row("asset-a"))
        with self.status.open("ab") as handle:
            handle.write(b'{"asset_id":}\n')
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with self.assertRaisesRegex(
                cache_module.AuthorityJsonlError, "malformed"
            ):
                self.read(cache)
        self.assertEqual(self.cache_count(), 0)

    def test_concurrent_append_during_full_read_is_rejected(self) -> None:
        self.write_rows(self.row("asset-a"))
        identity = self.identity()
        dirent_inode = self.dirent_inode()
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            original_read_range = cache._read_range
            mutated = False

            def mutate_after_read(*args: object, **kwargs: object) -> bytes:
                nonlocal mutated
                data = original_read_range(*args, **kwargs)
                if not mutated:
                    mutated = True
                    self.append_row(self.row("asset-b"))
                return data

            with mock.patch.object(cache, "_read_range", side_effect=mutate_after_read):
                with self.assertRaisesRegex(
                    cache_module.AuthoritySourceChanged, "changed"
                ):
                    cache.read_jsonl(
                        self.status,
                        dirent_inode=dirent_inode,
                        identity=identity,
                    )
        self.assertEqual(self.cache_count(), 0)

    def test_append_before_persistent_hit_is_not_hidden_by_cache(self) -> None:
        self.write_rows(self.row("asset-a"))
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
        stale_identity = self.identity()
        stale_dirent = self.dirent_inode()
        self.append_row(self.row("asset-b"))

        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with self.assertRaisesRegex(
                cache_module.AuthoritySourceChanged, "changed"
            ):
                cache.read_jsonl(
                    self.status,
                    dirent_inode=stale_dirent,
                    identity=stale_identity,
                )

    def test_deleted_source_is_not_served_from_cache(self) -> None:
        self.write_rows(self.row("asset-a"))
        identity = self.identity()
        dirent_inode = self.dirent_inode()
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
        self.status.unlink()
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with self.assertRaisesRegex(
                cache_module.AuthoritySourceChanged, "cannot be opened"
            ):
                cache.read_jsonl(
                    self.status,
                    dirent_inode=dirent_inode,
                    identity=identity,
                )

    def test_corrupt_local_payload_falls_back_to_full_source_validation(self) -> None:
        self.write_rows(self.row("asset-a"))
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE jsonl_cache SET rows_payload = ?", (sqlite3.Binary(b"bad"),)
            )

        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            result = self.read(cache)
        self.assertEqual(result.source, "full")
        self.assertEqual(result.rows[0][1]["asset_id"], "asset-a")
        self.assertEqual(self.cache_count(), 1)

    def test_non_object_and_unknown_schema_fail_closed(self) -> None:
        self.status.write_text("[]\n", encoding="utf-8")
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            with self.assertRaisesRegex(
                cache_module.AuthorityJsonlError, "non-object"
            ):
                self.read(cache)
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(cache_module.AuthorityCacheCorrupt, "unknown"):
            cache_module.PersistentAuthorityParseCache(self.database)

    def test_prune_removes_only_unretained_cache_records(self) -> None:
        self.write_rows(self.row("asset-a"))
        second = self.inventory / "total_asset_render_status_batch0004.jsonl"
        second.write_text(json.dumps(self.row("asset-b")) + "\n", encoding="utf-8")
        with cache_module.PersistentAuthorityParseCache(self.database) as cache:
            self.read(cache)
            metadata = second.stat()
            with os.scandir(self.inventory) as entries:
                second_inode = next(
                    entry.inode() for entry in entries if entry.name == second.name
                )
            cache.read_jsonl(
                second,
                dirent_inode=second_inode,
                identity=(
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ),
            )
            self.assertEqual(cache.prune((self.status,)), 1)
        self.assertEqual(self.cache_count(), 1)


if __name__ == "__main__":
    unittest.main()
