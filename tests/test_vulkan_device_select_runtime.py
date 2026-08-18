from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vulkan_device_select_runtime as runtime


class VulkanDeviceSelectRuntimeTests(unittest.TestCase):
    def _build_runtime(self, root: Path, binary: bytes = b"mesa layer\n") -> str:
        root.mkdir(parents=True)
        (root / runtime.DEVICE_SELECT_LAYER_BINARY).write_bytes(binary)
        (root / runtime.DEVICE_SELECT_LAYER_MANIFEST).write_bytes(
            runtime.device_select_manifest_bytes(str(root))
        )
        root.chmod(0o755)
        for child in root.iterdir():
            child.chmod(0o444)
        xdg_manifest = Path(
            runtime.device_select_activation_manifest_path(str(root))
        )
        xdg_manifest.parent.mkdir(parents=True)
        xdg_manifest.write_bytes(runtime.device_select_manifest_bytes(str(root)))
        xdg_manifest.chmod(0o444)
        for directory in (
            xdg_manifest.parent,
            xdg_manifest.parent.parent,
            xdg_manifest.parent.parent.parent,
        ):
            directory.chmod(0o755)
        return hashlib.sha256(binary).hexdigest()

    def _validate(
        self, root: Path, binary_sha256: str
    ) -> runtime.DeviceSelectRuntimeStatus:
        return runtime.validate_device_select_runtime(
            str(root),
            expected_owner_uid=os.getuid(),
            trusted_ancestor=str(root.parent),
            expected_binary_sha256=binary_sha256,
        )

    def _run_embedded_validator(
        self, root: Path, binary_sha256: str
    ) -> subprocess.CompletedProcess[str]:
        expression = runtime.device_select_runtime_validation_expression(
            str(root),
            expected_owner_uid=os.getuid(),
            trusted_ancestor=str(root.parent),
            expected_binary_sha256=binary_sha256,
        )
        return subprocess.run(
            [sys.executable, "-c", expression],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_pinned_contract_and_root_derived_manifest(self) -> None:
        self.assertEqual(runtime.DEVICE_SELECT_MESA_VERSION, "23.2.1")
        self.assertEqual(
            runtime.DEVICE_SELECT_BINARY_SHA256,
            "c2d8fc40963e0387b8fb82985235ab267be23e892f2ff5949cdbb98aeebb180f",
        )
        self.assertEqual(
            runtime.DEFAULT_DEVICE_SELECT_ROOT,
            "/root/.local/share/video2blender/"
            "vulkan-device-select-mesa-23.2.1/runtime",
        )
        self.assertTrue(runtime.DEVICE_SELECT_RUNTIME_POLICY.endswith("-v2"))
        self.assertTrue(runtime.DEVICE_SELECT_ENVIRONMENT_POLICY.endswith("-v2"))
        first = runtime.device_select_manifest_payload("/safe/one")
        second = runtime.device_select_manifest_payload("/safe/two")
        layer = first["layer"]
        self.assertEqual(layer["name"], runtime.DEVICE_SELECT_LAYER_NAME)
        self.assertEqual(
            layer["library_path"],
            "/safe/one/" + runtime.DEVICE_SELECT_LAYER_BINARY,
        )
        self.assertEqual(layer["disable_environment"], {"NODEVICE_SELECT": "1"})
        self.assertEqual(
            runtime.device_select_xdg_root("/safe/package/runtime"),
            "/safe/package/xdg",
        )
        self.assertEqual(
            runtime.device_select_activation_manifest_path(
                "/safe/package/runtime"
            ),
            "/safe/package/xdg/vulkan/implicit_layer.d/"
            + runtime.DEVICE_SELECT_LAYER_MANIFEST,
        )
        self.assertNotEqual(
            runtime.device_select_manifest_sha256("/safe/one"),
            runtime.device_select_manifest_sha256("/safe/two"),
        )
        self.assertNotEqual(
            first["layer"]["library_path"],
            second["layer"]["library_path"],
        )

    def test_exact_runtime_validates_with_local_test_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            digest = self._build_runtime(root)
            status = self._validate(root, digest)
            embedded = self._run_embedded_validator(root, digest)
        self.assertEqual(status.binary_sha256, digest)
        self.assertEqual(embedded.returncode, 0, embedded.stderr)
        self.assertIn(runtime.DEVICE_SELECT_READY_MARKER, embedded.stdout)

    def test_bad_hash_extra_entry_symlink_and_writable_root_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)

            bad_hash = base / "bad-hash" / "runtime"
            digest = self._build_runtime(bad_hash)
            bad_binary = bad_hash / runtime.DEVICE_SELECT_LAYER_BINARY
            bad_binary.chmod(0o644)
            bad_binary.write_bytes(b"changed")
            bad_binary.chmod(0o444)
            with self.assertRaisesRegex(
                runtime.DeviceSelectRuntimeError,
                "device_select_binary_hash_mismatch",
            ):
                self._validate(bad_hash, digest)

            extra = base / "extra" / "runtime"
            digest = self._build_runtime(extra)
            (extra / "untrusted").write_text("x", encoding="ascii")
            with self.assertRaisesRegex(
                runtime.DeviceSelectRuntimeError,
                "device_select_directory_entries_mismatch",
            ):
                self._validate(extra, digest)

            linked = base / "linked" / "runtime"
            digest = self._build_runtime(linked)
            binary = linked / runtime.DEVICE_SELECT_LAYER_BINARY
            binary.unlink()
            binary.symlink_to(linked / runtime.DEVICE_SELECT_LAYER_MANIFEST)
            with self.assertRaisesRegex(
                runtime.DeviceSelectRuntimeError,
                "device_select_binary_invalid",
            ):
                self._validate(linked, digest)

            writable = base / "writable" / "runtime"
            digest = self._build_runtime(writable)
            writable.chmod(0o777)
            with self.assertRaisesRegex(
                runtime.DeviceSelectRuntimeError,
                "device_select_ancestor_permissions_invalid",
            ):
                self._validate(writable, digest)

    def test_xdg_activation_tree_is_exact_trusted_and_manifest_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package" / "runtime"
            digest = self._build_runtime(root)
            xdg = Path(runtime.device_select_xdg_root(str(root)))
            (xdg / "unexpected").write_text("x", encoding="ascii")
            with self.assertRaisesRegex(
                runtime.DeviceSelectRuntimeError,
                "device_select_xdg_tree_mismatch",
            ):
                self._validate(root, digest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package" / "runtime"
            digest = self._build_runtime(root)
            manifest = Path(
                runtime.device_select_activation_manifest_path(str(root))
            )
            manifest.chmod(0o644)
            manifest.write_bytes(b"{}\n")
            manifest.chmod(0o444)
            with self.assertRaisesRegex(
                runtime.DeviceSelectRuntimeError,
                "device_select_xdg_manifest_hash_mismatch",
            ):
                self._validate(root, digest)

    def test_manifest_must_be_canonical_and_use_absolute_final_library(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            digest = self._build_runtime(root)
            payload = runtime.device_select_manifest_payload(str(root))
            payload["layer"]["library_path"] = runtime.DEVICE_SELECT_LAYER_BINARY
            manifest = root / runtime.DEVICE_SELECT_LAYER_MANIFEST
            manifest.chmod(0o644)
            manifest.write_text(
                json.dumps(payload), encoding="ascii"
            )
            manifest.chmod(0o444)
            with self.assertRaisesRegex(
                runtime.DeviceSelectRuntimeError,
                "device_select_manifest_hash_mismatch",
            ):
                self._validate(root, digest)

    def test_root_configuration_and_pci_selector_are_strict(self) -> None:
        self.assertEqual(
            runtime.configured_device_select_root({}),
            runtime.DEFAULT_DEVICE_SELECT_ROOT,
        )
        self.assertEqual(
            runtime.configured_device_select_root(
                {runtime.DEVICE_SELECT_ROOT_ENV: "/safe/runtime/"}
            ),
            "/safe/runtime",
        )
        expected = "pci-0000_a8_00_0"
        self.assertEqual(runtime.canonical_pci_selector("00000000:A8:00.0"), expected)
        self.assertEqual(runtime.canonical_pci_selector(expected), expected)
        for invalid in (
            "",
            "0:A8:00.0",
            "0000:A8:00.8",
            "pci-0000_a8_00_0!",
            "pci-0000_a8_0_0",
            "pci-0000_a8_00_0;id",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                runtime.DeviceSelectRuntimeError
            ):
                runtime.canonical_pci_selector(invalid)

    def test_environment_is_validated_then_exactly_isolated(self) -> None:
        root = "/root/.local/share/video2blender/device-select/runtime"
        command = runtime.device_select_environment_command(
            "00000000:A8:00.0", root
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
        self.assertLess(
            command.index("python3 -c"), command.index("export XDG_DATA_HOME=")
        )
        xdg = "/root/.local/share/video2blender/device-select/xdg"
        self.assertIn("unset DRI_PRIME NODEVICE_SELECT", command)
        self.assertNotIn("export NODEVICE_SELECT", command)
        self.assertIn("export XDG_DATA_HOME=" + xdg, command)
        self.assertIn("export XDG_DATA_DIRS=" + xdg, command)
        self.assertIn("export DRI_PRIME=pci-0000_a8_00_0", command)
        self.assertNotIn("pci-0000_a8_00_0!", command)
        self.assertIn(
            "export MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE=1", command
        )
        self.assertNotIn("export VK_LAYER_PATH", command)
        self.assertNotIn("export VK_INSTANCE_LAYERS", command)
        for conflict in (
            "MESA_VK_DEVICE_SELECT",
            "VK_ADD_LAYER_PATH",
            "VK_IMPLICIT_LAYER_PATH",
            "VK_LOADER_DEVICE_SELECT",
            "VK_LOADER_LAYERS_ENABLE",
            "VK_LOADER_LAYERS_DISABLE",
        ):
            self.assertIn(conflict, command.split("; export XDG_DATA_HOME", 1)[0])

    def test_failed_validation_never_exports_or_runs_following_command(self) -> None:
        sentinel = "DEVICE_SELECT_UNSAFE_SENTINEL"
        command = (
            runtime.device_select_environment_command(
                "0000:A8:00.0", "/definitely/missing/device-select-runtime"
            )
            + f" printf '{sentinel}\\n'"
        )
        result = subprocess.run(
            ["bash", "-c", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, runtime.EX_CONFIG)
        self.assertIn(runtime.DEVICE_SELECT_INVALID_MARKER, result.stderr)
        self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_fingerprint_is_stable_and_root_sensitive(self) -> None:
        first = runtime.device_select_environment_fingerprint("/safe/one")
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(
            first, runtime.device_select_environment_fingerprint("/safe/one")
        )
        self.assertNotEqual(
            first, runtime.device_select_environment_fingerprint("/safe/two")
        )
        self.assertRegex(runtime.DEVICE_SELECT_POLICY_FINGERPRINT, r"^[0-9a-f]{64}$")

    def test_existing_invalid_root_is_never_overwritten_by_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package" / "runtime"
            root.mkdir(parents=True)
            sentinel = root / "keep-me"
            sentinel.write_text("operator evidence", encoding="utf-8")
            with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
                runtime, "_ensure_trusted_parent_chain"
            ), mock.patch.object(runtime, "_read_deployment_source") as source:
                with self.assertRaises(runtime.DeviceSelectRuntimeError):
                    runtime.deploy_device_select_runtime(str(root))
            source.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "operator evidence")

    def test_existing_invalid_xdg_tree_is_never_overwritten_by_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package" / "runtime"
            xdg = Path(runtime.device_select_xdg_root(str(root)))
            xdg.mkdir(parents=True)
            sentinel = xdg / "keep-me"
            sentinel.write_text("operator evidence", encoding="utf-8")
            with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
                runtime, "_ensure_trusted_parent_chain"
            ), mock.patch.object(runtime, "_read_deployment_source") as source:
                with self.assertRaises(runtime.DeviceSelectRuntimeError):
                    runtime.deploy_device_select_runtime(str(root))
            source.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "operator evidence")

    def test_deploy_uses_unique_staging_and_atomic_noreplace_publish(self) -> None:
        root = "/safe/device-select/runtime"
        status = runtime.DeviceSelectRuntimeStatus(
            root=root,
            binary_sha256=runtime.DEVICE_SELECT_BINARY_SHA256,
            manifest_sha256=runtime.device_select_manifest_sha256(root),
            environment_fingerprint=runtime.device_select_environment_fingerprint(root),
        )
        with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
            runtime, "_ensure_trusted_parent_chain"
        ), mock.patch.object(os.path, "lexists", return_value=False), mock.patch.object(
            runtime, "_read_deployment_source", return_value=b"pinned"
        ), mock.patch.object(
            runtime, "_build_runtime_staging"
        ) as build_runtime, mock.patch.object(
            runtime, "_build_xdg_staging"
        ) as build_xdg, mock.patch.object(runtime, "_fsync_directory"), mock.patch.object(
            runtime, "validate_device_select_runtime", return_value=status
        ) as validate, mock.patch.object(
            runtime, "_publish_noreplace"
        ) as publish, mock.patch.object(
            runtime, "_cleanup_owned_staging"
        ) as cleanup:
            observed = runtime.deploy_device_select_runtime(root)
        self.assertEqual(observed, status)
        runtime_staging = build_runtime.call_args.args[0]
        xdg_staging = build_xdg.call_args.args[0]
        self.assertTrue(
            runtime_staging.startswith("/safe/device-select/.runtime.staging.")
        )
        self.assertTrue(xdg_staging.startswith("/safe/device-select/.xdg.staging."))
        self.assertEqual(
            publish.call_args_list,
            [
                mock.call(runtime_staging, root),
                mock.call(xdg_staging, "/safe/device-select/xdg"),
            ],
        )
        self.assertEqual(cleanup.call_count, 2)
        validate.assert_called_once_with(root)
        source = inspect.getsource(runtime._publish_noreplace)
        self.assertIn("RENAME_NOREPLACE", source)
        self.assertNotIn("os.replace", source)
        self.assertNotIn("os.rename", source)

    def test_existing_exact_runtime_and_xdg_are_idempotently_accepted(self) -> None:
        root = "/safe/device-select/runtime"
        status = runtime.DeviceSelectRuntimeStatus(
            root=root,
            binary_sha256=runtime.DEVICE_SELECT_BINARY_SHA256,
            manifest_sha256=runtime.device_select_manifest_sha256(root),
            environment_fingerprint=runtime.device_select_environment_fingerprint(root),
            xdg_root="/safe/device-select/xdg",
            activation_manifest_sha256=runtime.device_select_manifest_sha256(root),
        )
        with mock.patch.object(os, "geteuid", return_value=0), mock.patch.object(
            runtime, "_ensure_trusted_parent_chain"
        ), mock.patch.object(os.path, "lexists", return_value=True), mock.patch.object(
            runtime, "_validate_runtime_component"
        ) as validate_runtime, mock.patch.object(
            runtime, "_validate_xdg_component"
        ) as validate_xdg, mock.patch.object(
            runtime, "validate_device_select_runtime", return_value=status
        ), mock.patch.object(runtime, "_read_deployment_source") as source, mock.patch.object(
            runtime, "_publish_noreplace"
        ) as publish:
            observed = runtime.deploy_device_select_runtime(root)
        self.assertEqual(observed, status)
        validate_runtime.assert_called_once()
        validate_xdg.assert_called_once()
        source.assert_not_called()
        publish.assert_not_called()

    def test_deployment_cli_command_is_quoted_and_fixed_to_deploy(self) -> None:
        command = runtime.device_select_runtime_deployment_command(
            "/safe/root with space", script_path="/safe/tool with space.py"
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
        self.assertIn(" deploy --root ", command)
        self.assertIn(runtime.DEVICE_SELECT_SOURCE_BINARY, inspect.getsource(runtime))


if __name__ == "__main__":
    unittest.main()
