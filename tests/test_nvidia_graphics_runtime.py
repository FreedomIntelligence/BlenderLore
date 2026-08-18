from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import nvidia_graphics_runtime as runtime


class NvidiaGraphicsRuntimeTests(unittest.TestCase):
    def test_tls_library_is_an_explicit_required_artifact(self) -> None:
        self.assertIn(
            "libnvidia-tls.so.570.211.01",
            runtime.RUNTIME_REQUIRED_FILES,
        )
    def _build_runtime(self, root: Path) -> None:
        root.mkdir(parents=True)
        for relative in runtime.RUNTIME_REQUIRED_FILES:
            path = root / relative
            if relative == "libEGL_nvidia.so.0":
                continue
            if relative == runtime.RUNTIME_ICD:
                path.write_text(
                    json.dumps({
                        "file_format_version": "1.0.0",
                        "ICD": {"library_path": "libEGL_nvidia.so.0"},
                    }),
                    encoding="utf-8",
                )
            elif relative == runtime.RUNTIME_EGL_VENDOR:
                path.write_text(
                    json.dumps({
                        "file_format_version": "1.0.0",
                        "ICD": {"library_path": "libEGL_nvidia.so.0"},
                    }),
                    encoding="utf-8",
                )
            else:
                path.write_bytes((relative + "\n").encode("ascii"))
        (root / "libEGL_nvidia.so.0").symlink_to(
            "libEGL_nvidia.so.570.211.01"
        )
        self._write_manifest(root)

    def _write_manifest(self, root: Path, **updates: object) -> None:
        files = {}
        for path in sorted(root.iterdir()):
            if path.name.startswith("runtime_manifest.json"):
                continue
            if path.is_file():
                files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = {
            "schema": runtime.RUNTIME_SCHEMA,
            "driver_version": runtime.RUNTIME_DRIVER_VERSION,
            "source": {
                "filename": runtime.RUNTIME_SOURCE_FILENAME,
                "sha256": runtime.RUNTIME_SOURCE_SHA256,
            },
            "vulkan_icd": runtime.RUNTIME_ICD,
            "egl_vendor": runtime.RUNTIME_EGL_VENDOR,
            "library_dirs": ["."],
            "files": files,
            **updates,
        }
        manifest = root / runtime.RUNTIME_MANIFEST
        manifest.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        (root / runtime.RUNTIME_MANIFEST_SHA256).write_text(
            f"{digest} {runtime.RUNTIME_MANIFEST}\n",
            encoding="ascii",
        )

    def _validate(self, root: Path) -> subprocess.CompletedProcess[str]:
        digest = hashlib.sha256(
            (root / runtime.RUNTIME_MANIFEST).read_bytes()
        ).hexdigest()
        return subprocess.run(
            [
                sys.executable,
                "-c",
                runtime.runtime_validation_expression(
                    expected_manifest_sha256=digest,
                    expected_owner_uid=root.stat().st_uid,
                    trusted_ancestor=str(root),
                    validate_device_inventory=False,
                ),
                str(root),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_exact_manifest_and_all_file_hashes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self._build_runtime(root)
            result = self._validate(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(runtime.RUNTIME_READY_MARKER, result.stdout)

    def test_corruption_path_escape_and_glx_icd_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            self._build_runtime(root)
            (root / "libnvidia-glcore.so.570.211.01").write_bytes(b"corrupt")
            result = self._validate(root)
            self.assertEqual(result.returncode, 78)
            self.assertIn("code=runtime_file_hash_mismatch", result.stderr)

            self._build_runtime(Path(temporary) / "runtime-extra")
            extra_root = Path(temporary) / "runtime-extra"
            (extra_root / "libuntrusted.so").write_bytes(b"untrusted")
            result = self._validate(extra_root)
            self.assertEqual(result.returncode, 78)
            self.assertIn("code=runtime_directory_entries_mismatch", result.stderr)

            self._build_runtime(Path(temporary) / "runtime-escape")
            escape_root = Path(temporary) / "runtime-escape"
            outside = Path(temporary) / "outside.so"
            outside.write_bytes(b"outside")
            payload = json.loads((escape_root / runtime.RUNTIME_MANIFEST).read_text())
            payload["files"]["../outside.so"] = hashlib.sha256(b"outside").hexdigest()
            self._write_manifest(escape_root, files=payload["files"])
            result = self._validate(escape_root)
            self.assertEqual(result.returncode, 78)
            self.assertIn("code=manifest_file_path_invalid", result.stderr)

            glx_root = Path(temporary) / "runtime-glx"
            self._build_runtime(glx_root)
            (glx_root / runtime.RUNTIME_ICD).write_text(
                json.dumps({"ICD": {"library_path": "libGLX_nvidia.so.0"}}),
                encoding="utf-8",
            )
            self._write_manifest(glx_root)
            result = self._validate(glx_root)
            self.assertEqual(result.returncode, 78)
            self.assertIn("code=vulkan_icd_library_invalid", result.stderr)

    def test_untrusted_writable_ancestor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ancestor = Path(temporary) / "untrusted"
            root = ancestor / "runtime"
            self._build_runtime(root)
            ancestor.chmod(0o777)
            digest = hashlib.sha256(
                (root / runtime.RUNTIME_MANIFEST).read_bytes()
            ).hexdigest()
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    runtime.runtime_validation_expression(
                        expected_manifest_sha256=digest,
                        expected_owner_uid=root.stat().st_uid,
                        trusted_ancestor=str(ancestor),
                        validate_device_inventory=False,
                    ),
                    str(root),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 78)
        self.assertIn("code=runtime_ancestor_permissions_invalid", result.stderr)

    def test_root_configuration_conflict_fails_closed(self) -> None:
        self.assertEqual(
            runtime.configured_runtime_root({}),
            runtime.DEFAULT_RUNTIME_ROOT,
        )
        self.assertEqual(
            runtime.configured_runtime_root({runtime.RUNTIME_ROOT_ENV: "/safe/root"}),
            "/safe/root",
        )
        with self.assertRaises(runtime.NvidiaGraphicsRuntimeError):
            runtime.configured_runtime_root({
                runtime.RUNTIME_ROOT_ENV: "/safe/a",
                runtime.LEGACY_RUNTIME_ROOT_ENV: "/safe/b",
            })
        with self.assertRaises(runtime.NvidiaGraphicsRuntimeError):
            runtime.normalize_runtime_root("relative/runtime")

    def test_remote_environment_is_pinned_and_read_only(self) -> None:
        command = runtime.runtime_environment_command(
            "/safe/runtime",
            extra_library_dirs=("/safe/ffmpeg",),
        )
        syntax = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn("--query-gpu=driver_version", command)
        self.assertIn("--query-gpu=index,pci.bus_id,uuid", command)
        self.assertIn("/proc/driver/nvidia/gpus", command)
        self.assertNotIn("minor_number", command)
        self.assertIn(runtime.RUNTIME_DRIVER_VERSION, command)
        self.assertIn(runtime.RUNTIME_SOURCE_SHA256, command)
        self.assertIn(runtime.RUNTIME_MANIFEST_DIGEST, command)
        self.assertIn("VK_DRIVER_FILES=/safe/runtime/nvidia_icd.json", command)
        self.assertIn("VK_ICD_FILENAMES=/safe/runtime/nvidia_icd.json", command)
        self.assertIn("__EGL_VENDOR_LIBRARY_FILENAMES=/safe/runtime/10_nvidia.json", command)
        self.assertIn("LD_LIBRARY_PATH=/safe/runtime:/safe/ffmpeg", command)
        self.assertIn("export XDG_RUNTIME_DIR=/tmp", command)
        for mutation in ("apt-get", "dnf ", "yum ", "ln -", "cp ", "mv "):
            self.assertNotIn(mutation, command)

    def test_failed_validation_never_exports_or_runs_following_command(self) -> None:
        sentinel = "TOTAL_ASSET_UNSAFE_SENTINEL"
        result = subprocess.run(
            [
                "bash",
                "-c",
                runtime.runtime_environment_command("/definitely/missing/runtime")
                + f" printf '{sentinel}\\n'",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 78)
        self.assertNotIn(sentinel, result.stdout + result.stderr)

    def _build_native_system_fixture(
        self, root: Path, *, driver: str = runtime.NATIVE_SYSTEM_DRIVER_VERSION
    ) -> dict[str, object]:
        bin_dir = root / "bin"
        library_root = root / "lib"
        vulkan_dir = root / "share" / "vulkan"
        egl_dir = root / "share" / "egl"
        for directory in (bin_dir, library_root, vulkan_dir, egl_dir):
            directory.mkdir(parents=True, exist_ok=True)
        nvidia_smi = bin_dir / "nvidia-smi"
        nvidia_smi.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '0, 0000:01:00.0, GPU-aaaa-bbbb, {driver}'\n",
            encoding="ascii",
        )
        nvidia_smi.chmod(0o755)
        cache_rows = []
        for soname, basename in runtime.NATIVE_SYSTEM_REQUIRED_SONAMES.items():
            target = library_root / basename
            target.write_bytes(b"\x7fELF" + basename.encode("ascii"))
            alias = library_root / soname
            alias.symlink_to(basename)
            cache_rows.append(f"{soname} (libc6,x86-64) => {alias}")
        ldconfig = bin_dir / "ldconfig"
        ldconfig.write_text(
            "#!/bin/sh\n"
            + "printf '%s\\n' "
            + " ".join(json.dumps(row) for row in cache_rows)
            + "\n",
            encoding="ascii",
        )
        ldconfig.chmod(0o755)
        vulkan = vulkan_dir / "nvidia_icd.json"
        vulkan.write_text(
            json.dumps({"ICD": {"library_path": "libGLX_nvidia.so.0"}}),
            encoding="utf-8",
        )
        egl = egl_dir / "10_nvidia.json"
        egl.write_text(
            json.dumps({"ICD": {"library_path": "libEGL_nvidia.so.0"}}),
            encoding="utf-8",
        )
        return {
            "nvidia_smi_binary": str(nvidia_smi),
            "ldconfig_binary": str(ldconfig),
            "vulkan_icd_paths": (str(vulkan),),
            "egl_vendor_paths": (str(egl),),
            "library_roots": (str(library_root),),
        }

    def _validate_native_fixture(
        self, root: Path, fixture: dict[str, object]
    ) -> subprocess.CompletedProcess[str]:
        expression = runtime.native_system_runtime_validation_expression(
            expected_owner_uid=os.geteuid(),
            trusted_ancestor=str(root),
            validate_device_inventory=False,
            **fixture,
        )
        return subprocess.run(
            [sys.executable, "-c", expression],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_native_system_runtime_requires_exact_driver_and_trusted_elfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            valid_fixture = self._build_native_system_fixture(root / "valid")
            valid = self._validate_native_fixture(root / "valid", valid_fixture)

            mismatch_fixture = self._build_native_system_fixture(
                root / "mismatch", driver=runtime.RUNTIME_DRIVER_VERSION
            )
            mismatch = self._validate_native_fixture(
                root / "mismatch", mismatch_fixture
            )

            untrusted_root = root / "untrusted"
            untrusted_fixture = self._build_native_system_fixture(untrusted_root)
            target = (
                untrusted_root
                / "lib"
                / runtime.NATIVE_SYSTEM_REQUIRED_SONAMES["libcuda.so.1"]
            )
            target.chmod(0o666)
            untrusted = self._validate_native_fixture(
                untrusted_root, untrusted_fixture
            )

        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn(runtime.NATIVE_SYSTEM_READY_MARKER, valid.stdout)
        self.assertEqual(mismatch.returncode, 78)
        self.assertIn("code=driver_version_mismatch", mismatch.stderr)
        self.assertEqual(untrusted.returncode, 78)
        self.assertIn("code=system_library_untrusted", untrusted.stderr)

    def test_native_blender_environment_clears_pinned_loader_overrides(self) -> None:
        command = runtime.native_system_runtime_environment_command()
        syntax = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn(runtime.NATIVE_SYSTEM_DRIVER_VERSION, command)
        self.assertIn(runtime.NATIVE_SYSTEM_ENVIRONMENT_POLICY, command)
        self.assertIn("unset LD_LIBRARY_PATH", command)
        self.assertNotIn(runtime.DEFAULT_RUNTIME_ROOT, command)
        self.assertNotIn(runtime.RUNTIME_SOURCE_SHA256, command)
        for mutation in ("apt-get", "dnf ", "yum ", "cp ", "mv "):
            self.assertNotIn(mutation, command)

    def _build_pinned_native_sources(
        self, root: Path, *, driver: str = runtime.NATIVE_SYSTEM_DRIVER_VERSION
    ) -> dict[str, tuple[str, str]]:
        root.mkdir(parents=True)
        # Reproduce the live image's unsafe ancestor condition.  Trust comes
        # from a stable opened FD plus the pinned content hash, not this path.
        root.chmod(0o777)
        result: dict[str, tuple[str, str]] = {}
        for name in runtime.PINNED_NATIVE_RUNTIME_SOURCES:
            path = root / name
            if name == "nvidia-smi":
                content = (
                    "#!/bin/sh\n"
                    f"printf '%s\\n' '0, 0000:01:00.0, GPU-aaaa-bbbb, {driver}'\n"
                ).encode("ascii")
                mode = 0o755
            else:
                content = b"\x7fELF" + name.encode("ascii")
                mode = 0o644
            path.write_bytes(content)
            path.chmod(mode)
            result[name] = (str(path), hashlib.sha256(content).hexdigest())
        return result

    def test_pinned_native_runtime_installs_atomically_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            trusted = base / "trusted"
            trusted.mkdir()
            sources = self._build_pinned_native_sources(base / "image-owned-by-uid1000")
            runtime_root = trusted / "generation" / "runtime"
            files = {name: digest for name, (_path, digest) in sources.items()}
            install_expression = runtime.pinned_native_runtime_install_expression(
                runtime_root=str(runtime_root),
                source_files=sources,
                expected_owner_uid=os.geteuid(),
                trusted_destination_ancestor=str(trusted),
            )
            installed = subprocess.run(
                [sys.executable, "-c", install_expression],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            installed_again = subprocess.run(
                [sys.executable, "-c", install_expression],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            validation_expression = runtime.pinned_native_runtime_validation_expression(
                runtime_root=str(runtime_root),
                expected_owner_uid=os.geteuid(),
                trusted_ancestor=str(trusted),
                files=files,
                validate_device_inventory=False,
            )
            validated = subprocess.run(
                [sys.executable, "-c", validation_expression],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("installed=new", installed.stdout)
        self.assertEqual(installed_again.returncode, 0, installed_again.stderr)
        self.assertIn("installed=existing", installed_again.stdout)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertIn(runtime.PINNED_NATIVE_RUNTIME_READY_MARKER, validated.stdout)

    def test_pinned_native_runtime_rejects_source_or_installed_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            trusted = base / "trusted"
            trusted.mkdir()
            sources = self._build_pinned_native_sources(base / "sources")
            runtime_root = trusted / "runtime"
            original_files = {
                name: digest for name, (_path, digest) in sources.items()
            }
            corrupted_sources = dict(sources)
            source_path, digest = corrupted_sources["libcuda.so.565.57.01"]
            Path(source_path).write_bytes(b"\x7fELFcorrupt")
            rejected_install = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    runtime.pinned_native_runtime_install_expression(
                        runtime_root=str(runtime_root),
                        source_files=corrupted_sources,
                        expected_owner_uid=os.geteuid(),
                        trusted_destination_ancestor=str(trusted),
                    ),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            root_exists_after_reject = runtime_root.exists()

            # Restore the attested source, install, then prove runtime drift is
            # independently rejected on every validation.
            restored = b"\x7fELF" + b"libcuda.so.565.57.01"
            Path(source_path).write_bytes(restored)
            Path(source_path).chmod(0o644)
            self.assertEqual(hashlib.sha256(restored).hexdigest(), digest)
            installed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    runtime.pinned_native_runtime_install_expression(
                        runtime_root=str(runtime_root),
                        source_files=sources,
                        expected_owner_uid=os.geteuid(),
                        trusted_destination_ancestor=str(trusted),
                    ),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            installed_cuda = runtime_root / "libcuda.so.565.57.01"
            installed_cuda.chmod(0o600)
            installed_cuda.write_bytes(b"\x7fELFdrift")
            validation = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    runtime.pinned_native_runtime_validation_expression(
                        runtime_root=str(runtime_root),
                        expected_owner_uid=os.geteuid(),
                        trusted_ancestor=str(trusted),
                        files=original_files,
                        validate_device_inventory=False,
                    ),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(rejected_install.returncode, 78)
        self.assertIn("code=runtime_source_hash_mismatch", rejected_install.stderr)
        self.assertFalse(root_exists_after_reject)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(validation.returncode, 78)
        self.assertIn("code=runtime_file_invalid", validation.stderr)

    def test_production_native_environment_uses_only_pinned_565_runtime(self) -> None:
        validation = runtime.pinned_native_runtime_validation_command()
        environment = runtime.pinned_native_runtime_environment_command()
        install = runtime.pinned_native_runtime_install_command()
        self.assertIn(runtime.PINNED_NATIVE_RUNTIME_DEFAULT_ROOT, validation)
        self.assertNotIn("/usr/lib/x86_64-linux-gnu", validation)
        self.assertNotIn("/usr/bin/nvidia-smi", validation)
        self.assertIn(
            "export LD_LIBRARY_PATH=" + runtime.PINNED_NATIVE_RUNTIME_DEFAULT_ROOT,
            environment,
        )
        self.assertNotIn(runtime.DEFAULT_RUNTIME_ROOT, environment)
        self.assertIn("/usr/bin/nvidia-smi", install)
        self.assertIn("runtime_source_hash_mismatch", install)


if __name__ == "__main__":
    unittest.main()
