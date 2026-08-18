from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
import sys
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_local_source_spool as spool
from storage_capacity import DiskCapacity, DiskCapacityError


class LocalSourceSpoolTests(unittest.TestCase):
    @staticmethod
    def healthy_capacity(_path: Path) -> DiskCapacity:
        return DiskCapacity(2 * 1024**4, 0, 2 * 1024**4, "test")

    def test_small_source_is_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "source"
            root.mkdir()
            model = root / "asset.blend"
            model.write_bytes(b"blend")
            with mock.patch.object(spool, "MIN_BYTES", 100):
                result = spool.stage_local_source(
                    root, model, size_bytes=5, model_only=False,
                    spool_root=Path(td) / "spool",
                )
            self.assertFalse(result.used)
            self.assertEqual(result.transfer_model, model.resolve())

    def test_large_directory_is_copied_and_excludes_archives(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "source"
            root.mkdir()
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER" * 100)
            (root / "texture.png").write_bytes(b"texture")
            (root / "unused.zip").write_bytes(b"archive")
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
            ):
                result = spool.stage_local_source(
                    root, model, size_bytes=1000, model_only=False,
                    spool_root=Path(td) / "spool",
                )
            try:
                self.assertTrue(result.used)
                self.assertEqual(result.transfer_model.read_bytes(), model.read_bytes())
                self.assertTrue((result.transfer_root / "texture.png").is_file())
                self.assertFalse((result.transfer_root / "unused.zip").exists())
            finally:
                spool.cleanup_local_source_spool(result)
            self.assertFalse(result.cleanup_root.exists())

    def test_archive_suffixed_directories_are_copied_but_archives_are_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "source"
            root.mkdir()
            models: list[Path] = []
            for suffix in ("zip", "rar", "7z"):
                extracted = root / f"downloaded.{suffix}"
                extracted.mkdir()
                model = extracted / f"scene-{suffix}.FBX"
                model.write_bytes(f"model-{suffix}".encode("ascii"))
                models.append(model)
                (root / f"archive.{suffix}").write_bytes(b"archive")
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
            ):
                result = spool.stage_local_source(
                    root,
                    models[0],
                    size_bytes=1000,
                    model_only=False,
                    spool_root=Path(td) / "spool",
                )
            try:
                for model in models:
                    relative = model.relative_to(root)
                    self.assertEqual(
                        (result.transfer_root / relative).read_bytes(),
                        model.read_bytes(),
                    )
                for suffix in ("zip", "rar", "7z"):
                    self.assertFalse(
                        (result.transfer_root / f"archive.{suffix}").exists()
                    )
            finally:
                spool.cleanup_local_source_spool(result)

    def test_model_only_preserves_relative_model_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "source"
            model = root / "nested" / "asset.blend"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"model")
            (root / "ignored.bin").write_bytes(b"ignored")
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
            ):
                result = spool.stage_local_source(
                    root, model, size_bytes=100, model_only=True,
                    spool_root=Path(td) / "spool",
                )
            try:
                self.assertTrue(result.transfer_model.is_file())
                self.assertFalse((result.transfer_root / "ignored.bin").exists())
            finally:
                spool.cleanup_local_source_spool(result)

    def test_global_lock_serializes_source_copies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            roots = []
            for index in range(2):
                root = base / f"source{index}"
                root.mkdir()
                model = root / "asset.blend"
                model.write_bytes(b"x" * 100)
                roots.append((root, model))
            active = 0
            peak = 0
            guard = threading.Lock()
            original = spool.shutil.copytree

            def delayed_copy(*args, **kwargs):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.08)
                try:
                    return original(*args, **kwargs)
                finally:
                    with guard:
                        active -= 1

            results = []
            def run(item):
                results.append(spool.stage_local_source(
                    item[0], item[1], size_bytes=100,
                    model_only=False, spool_root=base / "spool",
                ))
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
                mock.patch.object(spool.shutil, "copytree", side_effect=delayed_copy),
            ):
                threads = [threading.Thread(target=run, args=(item,)) for item in roots]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            try:
                self.assertEqual(peak, 1)
                self.assertEqual(len(results), 2)
                self.assertTrue(any(item.wait_seconds >= 0.05 for item in results))
            finally:
                for item in results:
                    spool.cleanup_local_source_spool(item)

    def test_prefetched_ready_is_atomically_consumed_by_worker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            model = source / "asset.blend"
            model.write_bytes(b"BLEND" * 100)
            spool_root = base / "spool"
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
            ):
                prefetched = spool.prefetch_local_source(
                    source,
                    model,
                    size_bytes=model.stat().st_size,
                    model_only=False,
                    spool_root=spool_root,
                    capacity_probe=self.healthy_capacity,
                    identity_token="asset-test",
                )
                self.assertEqual(prefetched.status, "prefetched")
                self.assertTrue(prefetched.ready_path.is_dir())
                self.assertEqual(
                    spool.ready_identity_tokens(spool_root=spool_root),
                    frozenset({"asset-test"}),
                )
                self.assertEqual(
                    spool.ready_identity_tokens_snapshot(spool_root=spool_root),
                    frozenset({"asset-test"}),
                )
                worker = spool.stage_local_source(
                    source,
                    model,
                    size_bytes=model.stat().st_size,
                    model_only=False,
                    spool_root=spool_root,
                )
            try:
                self.assertTrue(worker.used)
                self.assertTrue(worker.cache_hit)
                self.assertEqual(worker.cache_key, prefetched.cache_key)
                self.assertFalse(prefetched.ready_path.exists())
                self.assertEqual(
                    spool.ready_identity_tokens(spool_root=spool_root),
                    frozenset(),
                )
                self.assertEqual(
                    spool.ready_identity_tokens_snapshot(spool_root=spool_root),
                    frozenset(),
                )
                self.assertEqual(worker.transfer_model.read_bytes(), model.read_bytes())
            finally:
                spool.cleanup_local_source_spool(worker)

    def test_prefetch_and_worker_share_one_exfat_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            model = source / "asset.blend"
            model.write_bytes(b"x" * 1000)
            spool_root = base / "spool"
            entered = threading.Event()
            release = threading.Event()
            original = spool.shutil.copytree
            copies = 0

            def delayed_copy(*args, **kwargs):
                nonlocal copies
                copies += 1
                entered.set()
                release.wait(timeout=5)
                return original(*args, **kwargs)

            prefetch_result = []

            def prefetch() -> None:
                prefetch_result.append(spool.prefetch_local_source(
                    source,
                    model,
                    size_bytes=1000,
                    model_only=False,
                    spool_root=spool_root,
                    capacity_probe=self.healthy_capacity,
                ))

            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
                mock.patch.object(spool.shutil, "copytree", side_effect=delayed_copy),
            ):
                thread = threading.Thread(target=prefetch)
                thread.start()
                self.assertTrue(entered.wait(timeout=2))
                worker_thread_result = []
                worker_thread = threading.Thread(target=lambda: worker_thread_result.append(
                    spool.stage_local_source(
                        source,
                        model,
                        size_bytes=1000,
                        model_only=False,
                        spool_root=spool_root,
                    )
                ))
                worker_thread.start()
                time.sleep(0.05)
                release.set()
                thread.join(timeout=5)
                worker_thread.join(timeout=5)
            self.assertEqual(copies, 1)
            self.assertEqual(prefetch_result[0].status, "prefetched")
            self.assertTrue(worker_thread_result[0].cache_hit)
            spool.cleanup_local_source_spool(worker_thread_result[0])

    def test_prefetch_does_not_reread_existing_worker_spool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            model = source / "asset.blend"
            model.write_bytes(b"x" * 1000)
            spool_root = base / "spool"
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
            ):
                worker = spool.stage_local_source(
                    source,
                    model,
                    size_bytes=1000,
                    model_only=False,
                    spool_root=spool_root,
                )
                with mock.patch.object(
                    spool.shutil, "copytree", wraps=spool.shutil.copytree
                ) as copytree:
                    result = spool.prefetch_local_source(
                        source,
                        model,
                        size_bytes=1000,
                        model_only=False,
                        spool_root=spool_root,
                        capacity_probe=self.healthy_capacity,
                    )
                self.assertEqual(result.status, "worker_spool_active")
                copytree.assert_not_called()
            spool.cleanup_local_source_spool(worker)

    def test_prefetch_capacity_unknown_and_500_gib_line_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            model = source / "asset.blend"
            model.write_bytes(b"x" * 100)
            with mock.patch.object(spool, "MIN_BYTES", 1):
                with self.assertRaisesRegex(
                    spool.LocalSourceSpoolError, "source_capacity_unknown"
                ):
                    spool.prefetch_local_source(
                        source,
                        model,
                        size_bytes=100,
                        model_only=False,
                        spool_root=base / "spool1",
                        capacity_probe=lambda _path: (_ for _ in ()).throw(
                            DiskCapacityError("unknown")
                        ),
                    )
                below = DiskCapacity(
                    2 * 1024**4,
                    0,
                    500 * 1024**3 - 1,
                    "test",
                )
                with self.assertRaisesRegex(
                    spool.LocalSourceSpoolError, "below_500_gib"
                ):
                    spool.prefetch_local_source(
                        source,
                        model,
                        size_bytes=100,
                        model_only=False,
                        spool_root=base / "spool2",
                        capacity_probe=lambda _path: below,
                    )

    def test_remote_snapshot_survives_atomic_worker_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            model = source / "asset.blend"
            model.write_bytes(b"BLEND" * 100)
            (source / "texture.png").write_bytes(b"texture")
            spool_root = base / "spool"
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
            ):
                ready = spool.prefetch_local_source(
                    source,
                    model,
                    size_bytes=1000,
                    model_only=False,
                    spool_root=spool_root,
                    capacity_probe=self.healthy_capacity,
                    identity_token="asset-test",
                )
                snapshot = spool.snapshot_ready_source(
                    spool_root=spool_root,
                    min_size_bytes=1,
                )
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(
                    (snapshot.transfer_root / "asset.blend").stat().st_ino,
                    (ready.ready_path / "payload/asset.blend").stat().st_ino,
                )
                worker = spool.stage_local_source(
                    source,
                    model,
                    size_bytes=1000,
                    model_only=False,
                    spool_root=spool_root,
                )
            try:
                self.assertFalse(ready.ready_path.exists())
                spool.cleanup_local_source_spool(worker)
                self.assertEqual(
                    (snapshot.transfer_root / "asset.blend").read_bytes(),
                    model.read_bytes(),
                )
                self.assertFalse(spool.mark_ready_source_remote_cached(
                    ready.cache_key,
                    "a" * 64,
                    spool_root=spool_root,
                ))
            finally:
                spool.cleanup_ready_source_snapshot(snapshot)

    def test_remote_snapshot_failure_leaves_ready_entry_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            model = source / "asset.blend"
            model.write_bytes(b"BLEND" * 100)
            spool_root = base / "spool"
            with (
                mock.patch.object(spool, "MIN_BYTES", 1),
                mock.patch.object(spool, "FREE_RESERVE_BYTES", 0),
            ):
                ready = spool.prefetch_local_source(
                    source,
                    model,
                    size_bytes=1000,
                    model_only=False,
                    spool_root=spool_root,
                    capacity_probe=self.healthy_capacity,
                    identity_token="asset-test",
                )
            with mock.patch.object(spool.os, "link", side_effect=OSError("no link")):
                with self.assertRaisesRegex(
                    spool.LocalSourceSpoolError, "snapshot_link_failed"
                ):
                    spool.snapshot_ready_source(
                        spool_root=spool_root,
                        min_size_bytes=1,
                    )
            self.assertTrue(ready.ready_path.is_dir())
            self.assertEqual(
                [path.name for path in spool_root.glob(".spool.remote.*")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
