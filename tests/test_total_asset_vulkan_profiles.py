from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_vulkan_profiles as profiles


class FakeRemote:
    port = "31722"

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.commands: list[str] = []

    def run(self, command: str, timeout: int = 30) -> str:
        del timeout
        self.commands.append(command)
        return self.outputs.pop(0)


class VulkanProfileTests(unittest.TestCase):
    def _execute_attestation(
        self,
        *,
        target_index: int,
        target_uuid: str,
        compute_uuids: tuple[str, ...],
        pmon_rows: tuple[tuple[int, str, int, int], ...],
    ) -> str:
        pid = str(os.getpid())
        inventory = (
            "0, GPU-0000-aaaa\n"
            f"{target_index}, {target_uuid}\n"
            if target_index != 0
            else f"0, {target_uuid}\n"
        )
        compute = "".join(f"{value}, {pid}\n" for value in compute_uuids)
        pmon = "# gpu pid type fb ccpm command\n" + "".join(
            f"{index} {pid} {row_type} {fb} {ccpm} blender\n"
            for index, row_type, fb, ccpm in pmon_rows
        )
        responses = []
        for _sample in range(profiles.GPU_PID_ATTESTATION_STABLE_SAMPLES):
            responses.extend((inventory, compute, pmon))

        def fake_run(*_args, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=responses.pop(0),
                stderr="",
            )

        scene = SimpleNamespace(
            camera=object(),
            render=SimpleNamespace(
                engine="", resolution_x=0, resolution_y=0,
                resolution_percentage=0,
            ),
        )
        bpy = SimpleNamespace(
            app=SimpleNamespace(version=(5, 1, 0)),
            context=SimpleNamespace(
                scene=scene,
                preferences=SimpleNamespace(
                    system=SimpleNamespace(
                        gpu_preferred_device="10de/2b85/0"
                    )
                ),
            ),
            ops=SimpleNamespace(
                render=SimpleNamespace(render=lambda **_kwargs: {"FINISHED"})
            ),
        )
        gpu = SimpleNamespace(
            platform=SimpleNamespace(backend_type_get=lambda: "VULKAN")
        )
        program = profiles.blender_pid_uuid_attestation_program(
            target_uuid,
            target_index,
            expected_family="5.1",
            expected_preferred_device="10de/2b85/0",
        )
        stdout = io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"bpy": bpy, "gpu": gpu}),
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch("time.sleep", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            exec(program, {})
        return stdout.getvalue()

    def _artifact_fixture(self, base: Path) -> tuple[Path, Path, Path, Path]:
        trusted = base / "trusted"
        root = trusted / "vulkan-profiles" / "v1"
        profile_base = root / "blender-5.1" / "gpu-aaaa-bbbb"
        published = profile_base / "profile-1.1-0"
        config = published / "config" / "5.1" / "config"
        config.mkdir(parents=True)
        binary = trusted / "blender"
        binary.write_bytes(b"trusted blender binary")
        userpref = config / "userpref.blend"
        userpref.write_bytes(b"trusted user preference")
        manifest = {
            "profile_userpref_sha256": hashlib.sha256(
                userpref.read_bytes()
            ).hexdigest(),
            "blender_binary_sha256": hashlib.sha256(
                binary.read_bytes()
            ).hexdigest(),
        }
        (published / "profile.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        current = profile_base / "current"
        current.symlink_to(published)
        return trusted, root, current, binary

    def _run_artifact_probe(
        self, trusted: Path, root: Path, current: Path, binary: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                profiles.profile_artifact_probe_expression(
                    expected_owner_uid=os.getuid(),
                    trusted_ancestor=str(trusted),
                ),
                str(root),
                str(current),
                str(binary),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_profile_artifact_probe_rejects_tamper_and_untrusted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted, root, current, binary = self._artifact_fixture(
                Path(temporary)
            )
            result = self._run_artifact_probe(trusted, root, current, binary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(profiles.PROFILE_ARTIFACT_INVALID_MARKER, result.stdout)

            userpref = next(current.resolve().rglob("userpref.blend"))
            userpref.write_bytes(b"tampered")
            result = self._run_artifact_probe(trusted, root, current, binary)
            self.assertIn("code=profile_userpref_hash_mismatch", result.stdout)

        with tempfile.TemporaryDirectory() as temporary:
            trusted, root, current, binary = self._artifact_fixture(
                Path(temporary)
            )
            (current.resolve() / "config" / "unexpected.conf").write_text("x")
            result = self._run_artifact_probe(trusted, root, current, binary)
            self.assertIn("code=profile_config_file_unexpected", result.stdout)

        with tempfile.TemporaryDirectory() as temporary:
            trusted, root, current, binary = self._artifact_fixture(
                Path(temporary)
            )
            trusted.chmod(0o777)
            result = self._run_artifact_probe(trusted, root, current, binary)
            self.assertIn("code=profile_ancestor_untrusted", result.stdout)
    def test_inventory_parses_device_then_vendor_order(self) -> None:
        identity = profiles.parse_gpu_inventory(
            "0, 00000000:16:00.0, GPU-54fad6b2-bbad-95b8-ea94-cc94237c8860, 0x2B8510DE\n"
            "1, 00000000:27:00.0, GPU-f36efb06-3505-155e-8c9f-7beac51317ca, 0x2B8510DE\n",
            1,
        )
        self.assertEqual(identity.index, 1)
        self.assertEqual(identity.pci_selector, "pci-0000_27_00_0")
        self.assertEqual(identity.vendor, "10de")
        self.assertEqual(identity.device, "2b85")
        self.assertEqual(identity.gpu_count, 2)
        self.assertEqual(identity.preferred_prefix, "10de/2b85")

    def test_inventory_fails_closed_on_bad_or_duplicate_identity(self) -> None:
        invalid = (
            "0, bad-bus, GPU-aaaa-bbbb, 0x2B8510DE\n",
            "0, 00000000:16:00.0, not-a-uuid, 0x2B8510DE\n",
            "0, 00000000:16:00.0, GPU-aaaa-bbbb, 0x2B8510DE\n"
            "0, 00000000:27:00.0, GPU-cccc-dddd, 0x2B8510DE\n",
            "0, 00000000:16:00.0, GPU-aaaa-bbbb, unknown\n",
        )
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(profiles.ProfileError):
                profiles.parse_gpu_inventory(output, 0)

    def test_process_attestation_accepts_only_bounded_gpu0_auxiliary_row(self) -> None:
        target = "GPU-1111-bbbb"
        output = self._execute_attestation(
            target_index=1,
            target_uuid=target,
            compute_uuids=(target,),
            pmon_rows=((0, "G", 6, 0), (1, "C+G", 48, 0)),
        )
        self.assertIn(profiles.GPU_PID_ATTESTATION_MARKER, output)
        self.assertIn("policy=target_plus_gpu0_aux_graphics", output)
        self.assertIn("pmon_indices=0,1", output)
        self.assertIn("samples=3", output)
        self.assertIn("aux_gpu0_fb_mb=6", output)

    def test_process_attestation_accepts_graphics_only_target_pmon_evidence(self) -> None:
        target = "GPU-1111-bbbb"
        output = self._execute_attestation(
            target_index=1,
            target_uuid=target,
            compute_uuids=(),
            pmon_rows=((1, "G", 48, 0),),
        )
        self.assertIn(profiles.GPU_PID_ATTESTATION_MARKER, output)
        self.assertIn("observed=gpu-1111-bbbb", output.lower())
        self.assertIn("pmon_indices=1", output)
        self.assertIn("aux_gpu0_fb_mb=0", output)

    def test_process_attestation_rejects_every_out_of_policy_auxiliary(self) -> None:
        target = "GPU-1111-bbbb"
        cases = (
            ((target, "GPU-0000-aaaa"), ((0, "G", 6, 0), (1, "C+G", 48, 0))),
            ((target,), ((0, "C+G", 6, 0), (1, "C+G", 48, 0))),
            ((target,), ((0, "G", 17, 0), (1, "C+G", 48, 0))),
            ((target,), ((0, "G", 6, 1), (1, "C+G", 48, 0))),
            ((target,), ((1, "C+G", 48, 0), (2, "G", 1, 0))),
        )
        for compute, rows in cases:
            with self.subTest(compute=compute, rows=rows), self.assertRaises(
                RuntimeError
            ):
                self._execute_attestation(
                    target_index=1,
                    target_uuid=target,
                    compute_uuids=compute,
                    pmon_rows=rows,
                )

    def test_gpu0_target_rejects_any_second_pmon_card(self) -> None:
        target = "GPU-0000-aaaa"
        with self.assertRaises(RuntimeError):
            self._execute_attestation(
                target_index=0,
                target_uuid=target,
                compute_uuids=(target,),
                pmon_rows=((0, "C+G", 48, 0), (1, "G", 6, 0)),
            )

    def test_builder_attests_target_plus_bounded_gpu0_aux_graphics(self) -> None:
        identity = profiles.GpuIdentity(
            index=2,
            uuid="GPU-6d5aa9b6-c4f7-9b3a-6932-b25e6c217028",
            pci_selector="pci-0000_c8_00_0",
            vendor="10de",
            device="2b85",
            gpu_count=4,
        )
        command = profiles.build_profile_command(
            identity=identity,
            family="5.1",
            blender_binary="/opt/blender-5.1/blender",
        )
        self.assertIn("total_asset_vulkan_profile_builder.lock", command)
        self.assertIn("total_asset_gpu_0.lock", command)
        self.assertIn("total_asset_gpu_2.lock", command)
        self.assertIn("CUDA_DEVICE_ORDER=PCI_BUS_ID", command)
        self.assertIn(f"CUDA_VISIBLE_DEVICES={identity.uuid}", command)
        self.assertIn("BLENDER_USER_CONFIG=", command)
        self.assertEqual(command.count("--gpu-backend vulkan"), 2)
        self.assertIn("--gpu-backend vulkan --factory-startup", command)
        self.assertIn("--gpu-backend vulkan -b --python-expr", command)
        self.assertIn("unset DRI_PRIME NODEVICE_SELECT", command)
        self.assertIn("DRI_PRIME=pci-0000_c8_00_0", command)
        self.assertIn("MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE=1", command)
        self.assertIn(
            "XDG_DATA_HOME=/root/.local/share/video2blender/"
            "vulkan-device-select-mesa-23.2.1/xdg",
            command,
        )
        self.assertIn(
            "XDG_DATA_DIRS=/root/.local/share/video2blender/"
            "vulkan-device-select-mesa-23.2.1/xdg",
            command,
        )
        self.assertNotIn(
            "export VK_INSTANCE_LAYERS=VK_LAYER_MESA_device_select", command
        )
        self.assertIn("BLENDER_WORKBENCH", command)
        self.assertIn("gpu.platform.backend_type_get", command)
        self.assertIn(profiles.PROFILE_PREFERENCE_WRITER, command)
        self.assertIn(profiles.PROFILE_SMOKE_READY_MARKER, command)
        self.assertIn("gpu_preferred_device = candidate", command)
        self.assertIn("bpy.ops.wm.save_userpref", command)
        self.assertLess(
            command.index("bpy.ops.render.render"),
            command.index("gpu_preferred_device = candidate"),
        )
        self.assertLess(
            command.index("gpu_preferred_device = candidate"),
            command.index("bpy.ops.wm.save_userpref"),
        )
        self.assertNotIn("ctypes", command)
        self.assertNotIn("gpu_preferred_index", command)
        self.assertIn("--query-compute-apps=gpu_uuid,pid", command)
        smoke_program = profiles._profile_smoke_expression(
            family="5.1",
            candidate="10de/2b85/0",
            gpu_uuid=identity.uuid,
            gpu_index=identity.index,
        )
        self.assertIn("'pmon', '-s', 'm', '-c', '1'", smoke_program)
        self.assertIn("compute_uuids != {expected}", smoke_program)
        self.assertIn("row_type not in {'C', 'G', 'C+G'}", smoke_program)
        self.assertIn("aux_type != 'G'", smoke_program)
        self.assertIn("aux_fb > aux_fb_limit", smoke_program)
        self.assertIn("aux_ccpm != 0", smoke_program)
        self.assertIn("extras - {0}", smoke_program)
        self.assertIn(profiles.GPU_PID_ATTESTATION_POLICY, command)
        self.assertIn(profiles.GPU_PID_ATTESTATION_MARKER, command)
        self.assertIn("stable_samples", command)
        self.assertIn("expected_target_indices=2", command)
        self.assertIn("expected_aux_indices=0,2", command)
        self.assertIn('[ "$observed_count" -eq 1 ]', command)
        self.assertIn('[ "$observed_count" -eq 2 ]', command)
        self.assertIn("else marker_contract=0", command)
        self.assertIn(
            f'\"$marker_samples\" -ge {profiles.GPU_PID_ATTESTATION_STABLE_SAMPLES}',
            command,
        )
        self.assertLess(
            command.index(f"grep -Fq {profiles.PROFILE_SMOKE_READY_MARKER}"),
            command.index("attestation_line=$(grep"),
        )
        self.assertIn(identity.uuid, command)
        self.assertIn("10de/2b85/0", command)
        self.assertNotIn("10de/2b85/1", command)
        self.assertIn("mv -Tf", command)
        self.assertIn("exit 78", command)
        self.assertIn("command -v timeout", command)
        self.assertIn("command -v setsid", command)
        self.assertGreaterEqual(command.count("timeout --signal=TERM"), 2)
        self.assertIn(
            f"run_profile_bounded {profiles.PROFILE_SETUP_TIMEOUT_SECONDS}",
            command,
        )
        self.assertIn(
            f"--kill-after={profiles.PROFILE_TERM_GRACE_SECONDS}s "
            f"{profiles.PROFILE_SMOKE_TIMEOUT_SECONDS}s",
            command,
        )
        self.assertIn("TOTAL_ASSET_VULKAN_PROFILE_TXN", command)
        self.assertIn("profile_txn_signal \"$txn\" TERM", command)
        self.assertIn("profile_txn_signal \"$txn\" KILL", command)
        self.assertIn('kill -"$signal" -- "-$pgid"', command)
        self.assertIn("TOTAL_ASSET_PROFILE_TRANSACTION_RESIDUAL", command)
        self.assertIn('profile_guard_cleanup "$smoke_guard_pid"', command)
        self.assertIn('wait "$guard_pid"', command)
        self.assertIn("trap profile_builder_exit_cleanup EXIT", command)
        self.assertIn("trap 'exit 78' HUP INT TERM", command)
        self.assertIn("active_txn=$smoke_txn", command)
        self.assertIn("active_guard=$smoke_guard_pid", command)
        self.assertIn("profile_guard_cleanup", command)
        self.assertIn('kill -TERM "$guard_pid"', command)
        self.assertIn('kill -KILL "$guard_pid"', command)
        self.assertNotIn("pkill", command)
        self.assertNotIn("killall", command)
        syntax = subprocess.run(
            ["bash", "-n"],
            input=command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_profile_status_requires_exact_schema_policy_identity_and_binary(self) -> None:
        identity = profiles.GpuIdentity(
            index=1,
            uuid="GPU-f36efb06-3505-155e-8c9f-7beac51317ca",
            pci_selector="pci-0000_27_00_0",
            vendor="10de",
            device="2b85",
            gpu_count=4,
        )
        valid = {
            "schema": profiles.PROFILE_SCHEMA,
            "policy": profiles.PROFILE_POLICY,
            "family": "4.5",
            "gpu_index": 1,
            "gpu_uuid": identity.uuid,
            "gpu_pci_selector": identity.pci_selector,
            "blender_binary": "/opt/blender-4.5/blender",
            "preference_writer": profiles.PROFILE_PREFERENCE_WRITER,
            "graphics_driver_version": profiles.RUNTIME_DRIVER_VERSION,
            "graphics_runtime_source_sha256": profiles.RUNTIME_SOURCE_SHA256,
            "graphics_runtime_manifest_sha256": profiles.RUNTIME_MANIFEST_DIGEST,
            "graphics_environment_policy": profiles.RUNTIME_ENVIRONMENT_POLICY,
            "graphics_environment_fingerprint": (
                profiles.runtime_environment_fingerprint()
            ),
            "device_select_environment_policy": (
                profiles.DEVICE_SELECT_ENVIRONMENT_POLICY
            ),
            "device_select_environment_fingerprint": (
                profiles.device_select_environment_fingerprint()
            ),
            "gpu_pid_attestation_policy": profiles.GPU_PID_ATTESTATION_POLICY,
            "gpu0_aux_graphics_max_fb_mib": (
                profiles.GPU0_AUX_GRAPHICS_MAX_FB_MIB
            ),
            "gpu_pid_attestation_stable_samples": (
                profiles.GPU_PID_ATTESTATION_STABLE_SAMPLES
            ),
            "profile_userpref_sha256": "a" * 64,
            "blender_binary_sha256": "b" * 64,
            "preferred_device": "10de/2b85/0",
        }
        remote = FakeRemote([json.dumps(valid)])
        status = profiles.read_profile_status(
            remote,
            identity,
            "4.5",
            blender_binary="/opt/blender-4.5/blender",
        )
        self.assertTrue(status["ready"])
        profile_check = remote.commands[0]
        self.assertIn("vulkan-profiles/v1", profile_check)
        self.assertIn("userpref.blend", profile_check)

        for field, value in (
            ("gpu_uuid", "GPU-aaaa-bbbb"),
            ("gpu_pci_selector", "pci-0000_d8_00_0"),
            ("policy", "obsolete"),
            ("blender_binary", "/wrong/blender"),
            ("preference_writer", "unsafe"),
            ("graphics_driver_version", "580.0"),
            ("graphics_runtime_source_sha256", "0" * 64),
            ("graphics_runtime_manifest_sha256", "0" * 64),
            ("graphics_environment_policy", "obsolete"),
            ("graphics_environment_fingerprint", "0" * 64),
            ("device_select_environment_policy", "obsolete"),
            ("device_select_environment_fingerprint", "0" * 64),
            ("gpu_pid_attestation_policy", "exact_only"),
            ("gpu0_aux_graphics_max_fb_mib", 17),
            ("gpu_pid_attestation_stable_samples", 1),
            ("preferred_device", "ffff/2b85/1"),
        ):
            payload = {**valid, field: value}
            status = profiles.read_profile_status(
                FakeRemote([json.dumps(payload)]),
                identity,
                "4.5",
                blender_binary="/opt/blender-4.5/blender",
            )
            self.assertFalse(status["ready"])

        drifted = profiles.read_profile_status(
            FakeRemote([
                profiles.PROFILE_ARTIFACT_INVALID_MARKER
                + " code=graphics_runtime_invalid"
            ]),
            identity,
            "4.5",
            blender_binary="/opt/blender-4.5/blender",
        )
        self.assertFalse(drifted["ready"])
        self.assertFalse(drifted["runtime_ready"])

    def test_setup_warms_gpu_before_using_dynamic_rna_enum(self) -> None:
        expression = profiles._profile_setup_expression(
            family="5.1",
            candidate="10de/2b85/2",
        )
        compile(expression, "<profile-setup-test>", "exec")
        self.assertIn("tuple(bpy.app.version[:2])", expression)
        self.assertIn('scene.render.engine = "BLENDER_WORKBENCH"', expression)
        self.assertIn('gpu.platform.backend_type_get() != "VULKAN"', expression)
        self.assertIn('prefs.gpu_preferred_device = candidate', expression)
        self.assertIn('prefs.gpu_preferred_device != candidate', expression)
        self.assertLess(
            expression.index("bpy.ops.render.render"),
            expression.index("prefs.gpu_preferred_device = candidate"),
        )
        self.assertLess(
            expression.index("prefs.gpu_preferred_device = candidate"),
            expression.index("bpy.ops.wm.save_userpref"),
        )
        self.assertNotIn("bl_rna.properties", expression)

    def test_legacy_family_is_not_profiled(self) -> None:
        with self.assertRaises(profiles.ProfileError):
            profiles.normalize_family("3.6")

    def test_transport_timeout_exceeds_all_internal_candidate_bounds(self) -> None:
        timeout = profiles.profile_build_transport_timeout_seconds(4)
        internal_bound = 4 * (
            profiles.PROFILE_SETUP_TIMEOUT_SECONDS
            + profiles.PROFILE_SMOKE_TIMEOUT_SECONDS
            + 2 * profiles.PROFILE_TERM_GRACE_SECONDS
        )
        self.assertGreater(timeout, internal_bound)
        with self.assertRaises(profiles.ProfileError):
            profiles.profile_build_transport_timeout_seconds(0)

    def test_transaction_residual_is_reported_as_capability_block(self) -> None:
        class ResidualRemote:
            port = "31722"

            def __init__(self, _port: int, _gpu: int):
                self.calls = 0

            def run(self, _command: str, timeout: int = 30) -> str:
                del timeout
                self.calls += 1
                if self.calls == 1:
                    return (
                        "0, 00000000:16:00.0, "
                        "GPU-54fad6b2-bbad-95b8-ea94-cc94237c8860, "
                        "0x2B8510DE\n"
                    )
                if self.calls == 2:
                    return "{}"
                raise RuntimeError("TOTAL_ASSET_PROFILE_TRANSACTION_RESIDUAL")

        worker_stub = SimpleNamespace(
            REMOTE_BLENDERS={"5.1": "/opt/blender-5.1/blender"},
            Remote=ResidualRemote,
        )
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                sys.modules,
                {"run_total_asset_render_worker": worker_stub},
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = profiles.main([
                "build", "--port", "31722", "--gpu", "0", "--family", "5.1"
            ])
        self.assertEqual(result, profiles.CAPABILITY_EXIT)
        self.assertIn("stage=legacy_attestation_or_cleanup", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
