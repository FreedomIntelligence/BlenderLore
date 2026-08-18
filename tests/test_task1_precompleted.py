from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import task1_gpu_handoff as handoff


PIPELINE_SHELL = PROJECT / "blender/scripts/run_total_asset_pipeline.sh"


def shell_function_source(name: str) -> str:
    shell = PIPELINE_SHELL.read_text(encoding="utf-8")
    start = shell.index(f"{name}() {{")
    end = shell.index("\n}\n", start) + len("\n}\n")
    return shell[start:end]


def select_task1_evidence_log(root: Path, *, explicit: str | None = None) -> str:
    environment = os.environ.copy()
    environment.pop("BILIBILI_RESOURCE_TASK1_LOG", None)
    if explicit is not None:
        environment["BILIBILI_RESOURCE_TASK1_LOG"] = explicit
    script = "\n".join(
        (
            "set -euo pipefail",
            shell_function_source("select_task1_evidence_log"),
            'select_task1_evidence_log "$1"',
        )
    )
    result = subprocess.run(
        ["bash", "-c", script, "task1-log-test", str(root)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.rstrip("\n")


class Task1PrecompletedTests(unittest.TestCase):
    def make_status(self, path: Path, rows: int = 2) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["asset_id", "status"])
            writer.writeheader()
            for index in range(rows):
                writer.writerow({"asset_id": str(index), "status": "rendered"})

    def test_legacy_complete_run_needs_latest_output_marker_and_full_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "missing-runtime.json"
            log = root / "task1.log"
            status = root / "status.csv"
            log.write_text(
                "[2026-07-16 19:00:00] mode=failed_retry\n"
                "output /render/task1\n",
                encoding="utf-8",
            )
            self.make_status(status)
            with mock.patch.object(handoff, "task1_process_active", return_value=False):
                observation = handoff.observe_precompleted_task1(
                    runtime, log, status, required_status_rows=2
                )
            self.assertEqual(observation.state, "complete")
            self.assertEqual(observation.runtime_status, "legacy_complete")

    def test_marker_from_an_older_run_does_not_release_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "missing-runtime.json"
            log = root / "task1.log"
            status = root / "status.csv"
            log.write_text(
                "[2026-07-16 18:00:00] mode=first\n"
                "output /render/old\n"
                "[2026-07-16 19:00:00] mode=failed_retry\n"
                "processing last asset\n",
                encoding="utf-8",
            )
            self.make_status(status)
            with mock.patch.object(handoff, "task1_process_active", return_value=False):
                observation = handoff.observe_precompleted_task1(
                    runtime, log, status, required_status_rows=2
                )
            self.assertEqual(observation.state, "blocked")
            self.assertIn("natural", observation.reason)

    def test_legacy_holder_cleanup_is_exact(self) -> None:
        remote = mock.Mock()
        handoff.clear_task1_legacy_holder(remote)
        command = remote.run.call_args.args[0]
        self.assertIn("GPU_holder", command)
        self.assertIn("GPU_holder_idle", command)
        self.assertNotIn("pkill", command)
        self.assertNotIn("killall", command)

    def test_default_evidence_log_selects_newer_existing_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            main = root / "bili_project_file_task1.log"
            retry = root / "bili_project_file_task1_retry426.log"
            main.write_text("incomplete\n", encoding="utf-8")
            retry.write_text("output /render/task1\n", encoding="utf-8")
            now = time.time()
            os.utime(main, (now - 10, now - 10))
            os.utime(retry, (now, now))

            self.assertEqual(select_task1_evidence_log(root), str(retry))

    def test_newer_main_evidence_supersedes_old_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            main = root / "bili_project_file_task1.log"
            retry = root / "bili_project_file_task1_retry426.log"
            main.write_text("output /render/new-main\n", encoding="utf-8")
            retry.write_text("output /render/old-retry\n", encoding="utf-8")
            now = time.time()
            os.utime(retry, (now - 10, now - 10))
            os.utime(main, (now, now))

            self.assertEqual(select_task1_evidence_log(root), str(main))

    def test_explicit_evidence_log_has_priority_over_newer_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            main = root / "bili_project_file_task1.log"
            retry = root / "bili_project_file_task1_retry426.log"
            main.write_text("output /render/main\n", encoding="utf-8")
            retry.write_text("output /render/retry\n", encoding="utf-8")
            explicit = root / "explicit-task1.log"

            self.assertEqual(
                select_task1_evidence_log(root, explicit=str(explicit)),
                str(explicit),
            )

    def test_missing_default_logs_keep_main_path_for_fail_closed_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            selected = select_task1_evidence_log(root)

            self.assertEqual(selected, str(root / "bili_project_file_task1.log"))
            self.assertFalse(Path(selected).exists())


if __name__ == "__main__":
    unittest.main()
