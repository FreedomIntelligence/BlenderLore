from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_launchd_runtime as runtime


class TotalAssetLaunchdRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "source"
        self.scripts = self.project / "blender" / "scripts"
        self.scripts.mkdir(parents=True)
        for name in sorted(runtime.REQUIRED_NAMES):
            self.write(name, f"# {name}\n".encode("utf-8"))
        self.write("dependency.py", b"VALUE = 1\n")
        self.write("secret.env", b"PASSWORD=must-not-copy\n")
        self.runtime_root = self.root / "Application Support" / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, payload: bytes) -> None:
        path = self.scripts / name
        path.write_bytes(payload)

    def test_content_hash_is_stable_and_changes_with_source(self) -> None:
        first = runtime.describe_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        again = runtime.describe_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        self.assertEqual(first.generation, again.generation)
        self.assertEqual(len(first.generation), 64)
        self.write("dependency.py", b"VALUE = 2\n")
        changed = runtime.describe_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        self.assertNotEqual(first.generation, changed.generation)

    def test_new_generation_never_removes_prior_generation(self) -> None:
        first = runtime.install_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        self.write("dependency.py", b"VALUE = 2\n")
        second = runtime.install_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        self.assertNotEqual(first.generation, second.generation)
        self.assertTrue(first.project_root.is_dir())
        self.assertTrue(second.project_root.is_dir())

    def test_install_is_private_immutable_and_copies_only_runtime_sources(self) -> None:
        bundle = runtime.install_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        self.assertEqual(bundle.project_root.parent, self.runtime_root)
        self.assertEqual(
            {path.name for path in bundle.scripts_root.iterdir()},
            set(runtime.REQUIRED_NAMES) | {"dependency.py"},
        )
        self.assertFalse((bundle.scripts_root / "secret.env").exists())
        self.assertEqual(self.runtime_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(bundle.project_root.stat().st_mode & 0o777, 0o555)
        self.assertEqual(
            (bundle.scripts_root / "dependency.py").stat().st_mode & 0o777,
            0o444,
        )
        self.assertEqual(
            (bundle.scripts_root / runtime.BOOTSTRAP_NAME).stat().st_mode & 0o777,
            0o555,
        )
        original_mtime = bundle.project_root.stat().st_mtime_ns
        same = runtime.install_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        self.assertEqual(same, bundle)
        self.assertEqual(bundle.project_root.stat().st_mtime_ns, original_mtime)

    def test_selected_symlink_is_rejected_and_never_copied(self) -> None:
        target = self.root / "outside.py"
        target.write_text("SECRET = True\n", encoding="utf-8")
        (self.scripts / "linked.py").symlink_to(target)
        with self.assertRaisesRegex(
            runtime.RuntimeBundleError, "symlink_forbidden"
        ):
            runtime.install_runtime_bundle(
                self.project, runtime_root=self.runtime_root
            )
        self.assertFalse(self.runtime_root.exists())

    def test_runtime_root_symlink_is_rejected(self) -> None:
        target = self.root / "runtime-target"
        target.mkdir()
        self.runtime_root.parent.mkdir(parents=True)
        self.runtime_root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(runtime.RuntimeBundleError, "runtime_root_invalid"):
            runtime.install_runtime_bundle(
                self.project, runtime_root=self.runtime_root
            )
        self.assertEqual(list(target.iterdir()), [])

    def test_failed_atomic_rename_leaves_no_visible_generation(self) -> None:
        with mock.patch.object(
            runtime.os,
            "rename",
            side_effect=OSError(errno.EIO, "injected"),
        ):
            with self.assertRaisesRegex(
                runtime.RuntimeBundleError, "runtime_bundle_install_failed"
            ):
                runtime.install_runtime_bundle(
                    self.project, runtime_root=self.runtime_root
                )
        visible = [
            path
            for path in self.runtime_root.iterdir()
            if not path.name.startswith(".")
        ]
        staging = [
            path
            for path in self.runtime_root.iterdir()
            if path.name.startswith(".staging-")
        ]
        self.assertEqual(visible, [])
        self.assertEqual(staging, [])

    def test_existing_generation_is_never_overwritten(self) -> None:
        bundle = runtime.install_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        target = bundle.scripts_root / "dependency.py"
        target.chmod(0o644)
        target.write_text("CORRUPTED = True\n", encoding="utf-8")
        target.chmod(0o444)
        with self.assertRaisesRegex(runtime.RuntimeBundleError, "hash_mismatch"):
            runtime.install_runtime_bundle(
                self.project, runtime_root=self.runtime_root
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "CORRUPTED = True\n")

    def test_installed_generation_can_be_re_attested_by_exact_path(self) -> None:
        bundle = runtime.install_runtime_bundle(
            self.project, runtime_root=self.runtime_root
        )
        attested = runtime.attest_installed_runtime_bundle(bundle.project_root)
        self.assertEqual(attested.generation, bundle.generation)
        self.assertEqual(attested.project_root, bundle.project_root)

        target = bundle.scripts_root / "dependency.py"
        target.chmod(0o644)
        target.write_text("CORRUPTED = True\n", encoding="utf-8")
        target.chmod(0o444)
        with self.assertRaisesRegex(
            runtime.RuntimeBundleError, "digest_mismatch"
        ):
            runtime.attest_installed_runtime_bundle(bundle.project_root)

    def test_real_holder_and_supervisor_entrypoints_import_from_bundle(self) -> None:
        runtime_root = self.root / "real-runtime"
        bundle = runtime.install_runtime_bundle(
            PROJECT, runtime_root=runtime_root
        )
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["VIDEO2BLENDER_PROJECT_ROOT"] = str(bundle.project_root)
        for name in (
            "total_asset_holder_guard.py",
            "total_asset_cycle72_supervisor.py",
        ):
            result = subprocess.run(
                [sys.executable, str(bundle.script(name)), "--help"],
                cwd=bundle.project_root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
