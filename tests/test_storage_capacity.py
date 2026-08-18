from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import storage_capacity  # noqa: E402


class StorageCapacityTests(unittest.TestCase):
    def test_valid_shutil_result_is_used(self) -> None:
        usage = SimpleNamespace(total=1_000, used=400, free=600)
        with (
            mock.patch.object(storage_capacity.shutil, "disk_usage", return_value=usage),
            mock.patch.object(storage_capacity, "_is_darwin", return_value=False),
        ):
            capacity = storage_capacity.get_disk_capacity("/tmp")
        self.assertEqual(capacity.source, "shutil.disk_usage")
        self.assertEqual(capacity.free_bytes, 600)

    def test_darwin_exfat_valid_looking_truncation_uses_df(self) -> None:
        anomaly = SimpleNamespace(
            total=607_673_909_248,
            used=71_221_379_072,
            free=536_452_530_176,
        )
        df_result = SimpleNamespace(
            returncode=0,
            stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/disk4s1 15625817088 8659486720 6966330368 56% /Volumes/My Book\n",
            stderr="",
        )
        with (
            mock.patch.object(storage_capacity.shutil, "disk_usage", return_value=anomaly),
            mock.patch.object(storage_capacity, "_is_darwin", return_value=True),
            mock.patch.object(storage_capacity.subprocess, "run", return_value=df_result) as run,
        ):
            capacity = storage_capacity.get_disk_capacity("/Volumes/My Book")
        self.assertEqual(capacity.source, "df -Pk")
        self.assertEqual(capacity.total_bytes, 15_625_817_088 * 1024)
        self.assertEqual(capacity.free_bytes, 6_966_330_368 * 1024)
        self.assertIn("materially disagrees", capacity.primary_error)
        self.assertEqual(run.call_args.args[0], ["df", "-Pk", "/Volumes/My Book"])

    def test_darwin_matching_cross_check_keeps_primary_source(self) -> None:
        usage = SimpleNamespace(total=1_000 * 1024, used=400 * 1024, free=600 * 1024)
        df_result = SimpleNamespace(
            returncode=0,
            stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/disk1 1000 400 600 40% /tmp\n",
            stderr="",
        )
        with (
            mock.patch.object(storage_capacity.shutil, "disk_usage", return_value=usage),
            mock.patch.object(storage_capacity, "_is_darwin", return_value=True),
            mock.patch.object(storage_capacity.subprocess, "run", return_value=df_result),
        ):
            capacity = storage_capacity.get_disk_capacity("/tmp")
        self.assertEqual(capacity.source, "shutil.disk_usage")

    def test_exfat_anomaly_falls_back_to_parameterized_df(self) -> None:
        anomaly = SimpleNamespace(total=600, used=-300, free=900)
        df_result = SimpleNamespace(
            returncode=0,
            stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/disk1 1000 250 750 25% /Volumes/My Book\n",
            stderr="",
        )
        with (
            mock.patch.object(storage_capacity.shutil, "disk_usage", return_value=anomaly),
            mock.patch.object(storage_capacity, "_is_windows", return_value=False),
            mock.patch.object(storage_capacity.subprocess, "run", return_value=df_result) as run,
        ):
            capacity = storage_capacity.get_disk_capacity("/Volumes/My Book")
        self.assertEqual(capacity.source, "df -Pk")
        self.assertEqual(capacity.total_bytes, 1000 * 1024)
        self.assertEqual(capacity.free_bytes, 750 * 1024)
        self.assertIn("used is negative", capacity.primary_error)
        command = run.call_args.args[0]
        self.assertEqual(command, ["df", "-Pk", "/Volumes/My Book"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_df_allows_transient_used_plus_available_overcount(self) -> None:
        anomaly = SimpleNamespace(total=600, used=-300, free=900)
        df_result = SimpleNamespace(
            returncode=0,
            stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/disk1 1000 501 500 50% /Volumes/My Book\n",
            stderr="",
        )
        with (
            mock.patch.object(storage_capacity.shutil, "disk_usage", return_value=anomaly),
            mock.patch.object(storage_capacity, "_is_windows", return_value=False),
            mock.patch.object(storage_capacity.subprocess, "run", return_value=df_result),
        ):
            capacity = storage_capacity.get_disk_capacity("/Volumes/My Book")
        self.assertEqual(capacity.source, "df -Pk")
        self.assertEqual(capacity.used_bytes, 501 * 1024)
        self.assertEqual(capacity.free_bytes, 500 * 1024)

    def test_windows_fallback_uses_get_disk_free_space_ex(self) -> None:
        class Kernel32:
            @staticmethod
            def GetDiskFreeSpaceExW(_path, free_available, total, total_free):
                free_available._obj.value = 300
                total._obj.value = 1000
                total_free._obj.value = 400
                return 1

        windll = SimpleNamespace(kernel32=Kernel32())
        with mock.patch.object(ctypes, "windll", windll, create=True):
            capacity = storage_capacity._get_windows_capacity(Path("C:/"), "invalid primary")
        self.assertEqual(capacity.source, "GetDiskFreeSpaceExW")
        self.assertEqual(capacity.total_bytes, 1000)
        self.assertEqual(capacity.used_bytes, 600)
        self.assertEqual(capacity.free_bytes, 300)

    def test_get_disk_capacity_selects_windows_fallback(self) -> None:
        expected = storage_capacity.DiskCapacity(1000, 600, 300, "GetDiskFreeSpaceExW", "primary")
        with (
            mock.patch.object(storage_capacity, "_get_shutil_capacity", side_effect=OSError("bad statvfs")),
            mock.patch.object(storage_capacity, "_is_windows", return_value=True),
            mock.patch.object(storage_capacity, "_get_windows_capacity", return_value=expected) as fallback,
        ):
            capacity = storage_capacity.get_disk_capacity("C:/")
        self.assertIs(capacity, expected)
        fallback.assert_called_once()

    def test_all_probes_failed_is_fail_closed(self) -> None:
        anomaly = SimpleNamespace(total=600, used=-300, free=900)
        df_result = SimpleNamespace(returncode=1, stdout="", stderr="df: unavailable")
        with (
            mock.patch.object(storage_capacity.shutil, "disk_usage", return_value=anomaly),
            mock.patch.object(storage_capacity, "_is_windows", return_value=False),
            mock.patch.object(storage_capacity.subprocess, "run", return_value=df_result),
        ):
            with self.assertRaises(storage_capacity.DiskCapacityError):
                storage_capacity.get_disk_capacity("/tmp")

    def test_df_timeout_is_fail_closed(self) -> None:
        anomaly = SimpleNamespace(total=600, used=-300, free=900)
        with (
            mock.patch.object(storage_capacity.shutil, "disk_usage", return_value=anomaly),
            mock.patch.object(storage_capacity, "_is_windows", return_value=False),
            mock.patch.object(
                storage_capacity.subprocess,
                "run",
                side_effect=storage_capacity.subprocess.TimeoutExpired(["df", "-Pk"], 15),
            ),
        ):
            with self.assertRaisesRegex(storage_capacity.DiskCapacityError, "timed out"):
                storage_capacity.get_disk_capacity("/tmp")

    def test_pause_and_warning_boundaries(self) -> None:
        make = lambda free: storage_capacity.DiskCapacity(free * 2, 0, free, "test")
        pause = storage_capacity.DOWNLOAD_PAUSE_BYTES
        warning = storage_capacity.DOWNLOAD_WARNING_BYTES
        self.assertEqual(storage_capacity.capacity_tier(make(pause - 1)), "pause")
        self.assertEqual(storage_capacity.capacity_tier(make(pause)), "warning")
        self.assertEqual(storage_capacity.capacity_tier(make(warning - 1)), "warning")
        self.assertEqual(storage_capacity.capacity_tier(make(warning)), "ok")


if __name__ == "__main__":
    unittest.main()
