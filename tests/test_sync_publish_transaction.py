from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sync_coordinator import CoordinatorConfig, GateResult, SyncCoordinator  # noqa: E402


MV_SHIM = r'''import json
import os
import signal
import sys
import time


def consume_option(arguments, *values):
    if arguments and arguments[0] in values:
        return arguments[1:], True
    return arguments, False


arguments = sys.argv[1:]
arguments, atomic = consume_option(arguments, "-Tf", "-fT")
arguments, _ = consume_option(arguments, "--")
if len(arguments) != 2:
    raise SystemExit(64)

source, destination = arguments
log_path = os.environ.get("V2B_TEST_MV_LOG", "")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"source": source, "destination": destination, "atomic": atomic},
                sort_keys=True,
            )
            + "\n"
        )

source_exact = os.environ.get("V2B_TEST_MV_SOURCE", "")
source_contains = os.environ.get("V2B_TEST_MV_SOURCE_CONTAINS", "")
destination_exact = os.environ.get("V2B_TEST_MV_DESTINATION", "")
mode = os.environ.get("V2B_TEST_MV_MODE", "")
once_path = os.environ.get("V2B_TEST_MV_ONCE", "")
matches = bool(mode)
if source_exact:
    matches = matches and source == source_exact
if source_contains:
    matches = matches and source_contains in source
if destination_exact:
    matches = matches and destination == destination_exact
if once_path and os.path.exists(once_path):
    matches = False

if matches and once_path:
    with open(once_path, "x", encoding="utf-8") as handle:
        handle.write(mode)
if matches and mode == "before_error":
    raise SystemExit(97)

if atomic:
    os.replace(source, destination)
else:
    os.rename(source, destination)

if matches and mode == "after_error":
    raise SystemExit(98)
if matches and mode.startswith("after_signal_"):
    signal_name = mode.removeprefix("after_signal_")
    os.kill(os.getppid(), getattr(signal, signal_name))
    time.sleep(0.05)
'''


class RemoteModelPublishHarness:
    def __init__(self, root: Path):
        self.root = root
        self.remote_root = root / "remote html"
        self.remote_root.mkdir(parents=True)
        self.shims = root / "command-shims"
        self.shims.mkdir()
        self.mv_log = root / "mv.jsonl"
        self._write_python_executable(self.shims / "mv", MV_SHIM)
        self._write_shell_executable(self.shims / "flock", "exit 0")
        self._write_shell_executable(
            self.shims / "chmod",
            'if [ "$2" = -- ]; then mode=$1; shift 2; exec /bin/chmod "$mode" "$@"; fi\n'
            'exec /bin/chmod "$@"',
        )

        project = root / "project"
        state = root / "state"
        assets = root / "assets"
        html = root / "html"
        project.mkdir()
        assets.mkdir()
        html.mkdir()
        key = root / "id_ed25519"
        key.write_text("test-key", encoding="utf-8")
        key.chmod(0o600)
        known_hosts = root / "known_hosts"
        known_hosts.write_text("example.invalid ssh-ed25519 AAAATEST\n", encoding="utf-8")
        known_hosts.chmod(0o600)
        no_op = self._write_shell_executable(root / "no-op", "exit 0")
        config = CoordinatorConfig(
            project_root=project,
            state_root=state,
            asset_root=assets,
            html_root=html,
            release_manifest=assets / "release.json",
            remote="syncbot@example.invalid",
            remote_port=22,
            remote_asset_root=str(root / "remote-assets"),
            remote_html_root=str(self.remote_root),
            ssh_key=key,
            known_hosts=known_hosts,
            ssh_bin=str(no_op),
            rsync_bin=str(no_op),
            connect_timeout=1.0,
            wall_timeout=5.0,
            scope_wall_timeouts={scope: 5.0 for scope in (
                "assets",
                "total-render",
                "video-html",
                "model-html",
            )},
            heartbeat_interval=0.01,
            assets_authorized=False,
        )
        self.coordinator = SyncCoordinator(config, qa_activity_detector=lambda: ())

    @staticmethod
    def _write_shell_executable(path: Path, body: str) -> Path:
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    @staticmethod
    def _write_python_executable(path: Path, body: str) -> Path:
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    @property
    def page(self) -> Path:
        return self.remote_root / "asset_gallery_model_full.html"

    @property
    def media(self) -> Path:
        return self.remote_root / "model_gallery_media"

    @property
    def current(self) -> Path:
        return self.remote_root / ".video2blender-model-current"

    def install_legacy_live_tree(self) -> None:
        self.page.write_text("legacy page", encoding="utf-8")
        (self.media / "legacy").mkdir(parents=True)
        (self.media / "legacy" / "preview.jpg").write_bytes(b"legacy-media")
        legacy_target = self.remote_root / ".legacy-current-target"
        legacy_target.mkdir()
        (legacy_target / "sentinel").write_text("legacy-current", encoding="utf-8")
        self.current.symlink_to(legacy_target.name)

    def new_paths(self, request_generation: int) -> tuple[Path, Path]:
        gate = GateResult(True, "ready", secrets.token_hex(32), ())
        stage, release = self.coordinator._model_remote_paths(gate, request_generation)
        return Path(stage), Path(release)

    @staticmethod
    def prepare_stage(stage: Path, label: str) -> None:
        (stage / "model_gallery_media" / label).mkdir(parents=True)
        (stage / "asset_gallery_model_full.html").write_text(
            f"page-{label}", encoding="utf-8"
        )
        (stage / "model_gallery_media" / label / "preview.jpg").write_bytes(
            f"media-{label}".encode("ascii")
        )

    def run_publish(
        self,
        stage: Path,
        release: Path,
        *,
        mode: str = "",
        source: Path | None = None,
        source_contains: str = "",
        destination: Path | None = None,
        once_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = self.coordinator._remote_model_publish_command(str(stage), str(release))
        script = command[-1]
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            (str(self.shims), os.environ.get("PATH", "/usr/bin:/bin"))
        )
        environment["V2B_TEST_MV_LOG"] = str(self.mv_log)
        if mode:
            environment["V2B_TEST_MV_MODE"] = mode
        if source is not None:
            environment["V2B_TEST_MV_SOURCE"] = str(source)
        if source_contains:
            environment["V2B_TEST_MV_SOURCE_CONTAINS"] = source_contains
        if destination is not None:
            environment["V2B_TEST_MV_DESTINATION"] = str(destination)
        if once_path is not None:
            environment["V2B_TEST_MV_ONCE"] = str(once_path)
        return subprocess.run(
            ["/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )

    def mv_operations(self) -> list[dict[str, object]]:
        if not self.mv_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.mv_log.read_text(encoding="utf-8").splitlines()
        ]

    def assert_no_transaction_temporaries(self, case: unittest.TestCase) -> None:
        names = [path.name for path in self.remote_root.iterdir()]
        case.assertFalse(any(".next." in name for name in names), names)
        case.assertFalse(any(".rollback." in name for name in names), names)


class RemoteModelPublishTransactionTest(unittest.TestCase):
    def make_harness(self, parent: Path, name: str) -> RemoteModelPublishHarness:
        root = parent / name
        root.mkdir()
        return RemoteModelPublishHarness(root)

    def assert_legacy_live_tree(self, harness: RemoteModelPublishHarness) -> None:
        self.assertFalse(harness.page.is_symlink())
        self.assertEqual(harness.page.read_text(encoding="utf-8"), "legacy page")
        self.assertFalse(harness.media.is_symlink())
        self.assertEqual(
            (harness.media / "legacy" / "preview.jpg").read_bytes(), b"legacy-media"
        )
        self.assertTrue(harness.current.is_symlink())
        self.assertEqual(os.readlink(harness.current), ".legacy-current-target")
        self.assertTrue(harness.current.exists(), "restored current link must not dangle")

    def assert_managed_release(
        self,
        harness: RemoteModelPublishHarness,
        release: Path,
        label: str,
    ) -> None:
        expected_current = f".video2blender-model-releases/{release.name}"
        self.assertEqual(os.readlink(harness.current), expected_current)
        self.assertEqual(
            os.readlink(harness.page),
            ".video2blender-model-current/asset_gallery_model_full.html",
        )
        self.assertEqual(
            os.readlink(harness.media),
            ".video2blender-model-current/model_gallery_media",
        )
        self.assertTrue(harness.current.exists(), "current release link must not dangle")
        self.assertTrue(harness.page.exists(), "public page link must not dangle")
        self.assertTrue(harness.media.exists(), "public media link must not dangle")
        self.assertEqual(harness.page.read_text(encoding="utf-8"), f"page-{label}")
        self.assertEqual(
            (harness.media / label / "preview.jpg").read_bytes(),
            f"media-{label}".encode("ascii"),
        )

    def test_first_migration_then_managed_release_uses_one_atomic_current_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = self.make_harness(Path(temporary), "success")
            harness.install_legacy_live_tree()
            first_stage, first_release = harness.new_paths(1)
            harness.prepare_stage(first_stage, "first")

            first = harness.run_publish(first_stage, first_release)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assert_managed_release(harness, first_release, "first")
            first_backup = (
                harness.remote_root
                / ".video2blender-model-legacy-backup"
                / first_release.name
            )
            self.assertEqual((first_backup / "page").read_text(), "legacy page")
            self.assertEqual(
                (first_backup / "media" / "legacy" / "preview.jpg").read_bytes(),
                b"legacy-media",
            )
            self.assertTrue((first_backup / "current").is_symlink())

            page_inode = os.lstat(harness.page).st_ino
            media_inode = os.lstat(harness.media).st_ino
            operation_count = len(harness.mv_operations())
            second_stage, second_release = harness.new_paths(2)
            self.assertNotEqual(first_release.name, second_release.name)
            harness.prepare_stage(second_stage, "second")

            second = harness.run_publish(second_stage, second_release)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assert_managed_release(harness, second_release, "second")
            self.assertEqual(os.lstat(harness.page).st_ino, page_inode)
            self.assertEqual(os.lstat(harness.media).st_ino, media_inode)
            self.assertTrue(first_release.is_dir())
            self.assertTrue(second_release.is_dir())
            second_operations = harness.mv_operations()[operation_count:]
            public_moves = [
                operation
                for operation in second_operations
                if operation["destination"]
                in {str(harness.current), str(harness.page), str(harness.media)}
            ]
            self.assertEqual(len(public_moves), 1, public_moves)
            self.assertEqual(public_moves[0]["destination"], str(harness.current))
            self.assertTrue(public_moves[0]["atomic"])
            self.assertIn(".video2blender-model-current.next.", public_moves[0]["source"])
            harness.assert_no_transaction_temporaries(self)

    def test_first_migration_failpoints_and_signals_restore_every_legacy_entry(self) -> None:
        cases = []
        for entry in ("page", "media", "current"):
            cases.extend(
                (
                    (entry, "before_error"),
                    (entry, "after_signal_SIGTERM"),
                )
            )
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            for index, (entry, mode) in enumerate(cases):
                with self.subTest(entry=entry, mode=mode):
                    harness = self.make_harness(parent, f"legacy-{index}")
                    harness.install_legacy_live_tree()
                    stage, release = harness.new_paths(index + 1)
                    harness.prepare_stage(stage, "failed")
                    sources = {
                        "page": harness.page,
                        "media": harness.media,
                        "current": harness.current,
                    }
                    trigger = harness.root / "failpoint-consumed"

                    failed = harness.run_publish(
                        stage,
                        release,
                        mode=mode,
                        source=sources[entry],
                        once_path=trigger,
                    )
                    self.assertNotEqual(failed.returncode, 0, failed.stderr)
                    self.assertTrue(trigger.exists(), "the requested failpoint did not run")
                    self.assert_legacy_live_tree(harness)
                    self.assertFalse(release.exists())
                    self.assertFalse(
                        (
                            harness.remote_root
                            / ".video2blender-model-legacy-backup"
                            / release.name
                        ).exists()
                    )
                    harness.assert_no_transaction_temporaries(self)

                    retry_stage, retry_release = harness.new_paths(100 + index)
                    self.assertNotEqual(release.name, retry_release.name)
                    harness.prepare_stage(retry_stage, "retry")
                    retried = harness.run_publish(retry_stage, retry_release)
                    self.assertEqual(retried.returncode, 0, retried.stderr)
                    self.assert_managed_release(harness, retry_release, "retry")
                    harness.assert_no_transaction_temporaries(self)

    def test_failure_and_signal_after_managed_current_rename_restore_old_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            for index, mode in enumerate(("after_error", "after_signal_SIGTERM")):
                with self.subTest(mode=mode):
                    harness = self.make_harness(parent, f"managed-{index}")
                    harness.install_legacy_live_tree()
                    old_stage, old_release = harness.new_paths(1)
                    harness.prepare_stage(old_stage, "old")
                    initial = harness.run_publish(old_stage, old_release)
                    self.assertEqual(initial.returncode, 0, initial.stderr)
                    self.assert_managed_release(harness, old_release, "old")
                    page_inode = os.lstat(harness.page).st_ino
                    media_inode = os.lstat(harness.media).st_ino

                    failed_stage, failed_release = harness.new_paths(2)
                    harness.prepare_stage(failed_stage, "failed")
                    trigger = harness.root / "current-switch-failpoint-consumed"
                    failed = harness.run_publish(
                        failed_stage,
                        failed_release,
                        mode=mode,
                        source_contains=".video2blender-model-current.next.",
                        destination=harness.current,
                        once_path=trigger,
                    )
                    self.assertNotEqual(failed.returncode, 0, failed.stderr)
                    self.assertTrue(trigger.exists(), "current rename failpoint did not run")
                    self.assert_managed_release(harness, old_release, "old")
                    self.assertEqual(os.lstat(harness.page).st_ino, page_inode)
                    self.assertEqual(os.lstat(harness.media).st_ino, media_inode)
                    self.assertFalse(failed_release.exists())
                    harness.assert_no_transaction_temporaries(self)

                    retry_stage, retry_release = harness.new_paths(3)
                    self.assertNotEqual(failed_release.name, retry_release.name)
                    harness.prepare_stage(retry_stage, "retry")
                    retry = harness.run_publish(retry_stage, retry_release)
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    self.assert_managed_release(harness, retry_release, "retry")
                    harness.assert_no_transaction_temporaries(self)


if __name__ == "__main__":
    unittest.main()
