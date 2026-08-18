from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
GUARD = PROJECT / "blender/scripts/total_asset_launch_guard.py"
PIPELINE = PROJECT / "blender/scripts/run_total_asset_pipeline.sh"


class TotalAssetLaunchGuardTests(unittest.TestCase):
    def test_guard_holds_scheduler_lock_and_marks_child_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "scheduler.lock"
            output = root / "child.txt"
            result = subprocess.run(
                [
                    sys.executable,
                    str(GUARD),
                    "--lock",
                    str(lock),
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import os,pathlib; "
                        f"pathlib.Path({str(output)!r}).write_text("
                        "os.environ.get('TOTAL_ASSET_SCHEDULER_LOCK_HELD',''))"
                    ),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "1")

    def test_busy_scheduler_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "scheduler.lock"
            with lock.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(GUARD),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "raise SystemExit(0)",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
            self.assertEqual(result.returncode, 75)
            self.assertIn("scheduler lock busy", result.stderr)

    def test_forged_lock_held_token_cannot_bypass_shell_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "assets"
            inventory = root / "_asset_inventory"
            inventory.mkdir(parents=True)
            lock = inventory / "total_asset_scheduler.lock"
            with lock.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    ["bash", str(PIPELINE), "batch"],
                    env={
                        **os.environ,
                        "TOTAL_ASSET_ROOT": str(root),
                        "TOTAL_ASSET_SCHEDULER_LOCK_HELD": "1",
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )
            self.assertEqual(result.returncode, 75)
            self.assertIn("scheduler lock busy", result.stderr)


if __name__ == "__main__":
    unittest.main()
