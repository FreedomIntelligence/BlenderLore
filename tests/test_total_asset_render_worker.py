from __future__ import annotations

import gzip
import io
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_total_asset_render_worker as worker
from gpu_runtime_guard import GpuLockBusy
from storage_capacity import DiskCapacity, DiskCapacityError
from total_asset_remote_preflight import GpuObservation, NodeObservation


class FakeRemote:
    gpu = "2"
    gpu_uuid = "gpu-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d"
    vulkan_available = True
    vulkan_probe_state = "available"
    vulkan_error_code = ""

    def __init__(self) -> None:
        self.commands: list[tuple[str, int]] = []

    def run(self, command: str, timeout: int = 900) -> str:
        self.commands.append((command, timeout))
        return "ok"

    def put(self, _local: Path, _remote: str, timeout: int = 1800) -> None:
        del timeout

    def blender_env(self) -> str:
        return f"export CUDA_VISIBLE_DEVICES={self.gpu_uuid};"

    def vulkan_env(self, *, family: str = "") -> str:
        return (
            self.blender_env()
            + f" export BLENDER_USER_CONFIG=/tmp/profile-{family}/config;"
        )

    def ensure_vulkan_runtime(self, family: str, blender_binary: str) -> None:
        del family, blender_binary
        self.vulkan_available = True
        self.vulkan_probe_state = "blender_pid_uuid_attested"


def valid_vulkan_manifest(
    remote: worker.Remote, *, family: str = "5.1", preferred_index: int = 0
) -> str:
    remote.vulkan_preferred_prefix = "10de/2b85"
    return json.dumps({
        "schema": worker.VULKAN_PROFILE_SCHEMA,
        "policy": worker.VULKAN_PROFILE_POLICY,
        "family": family,
        "gpu_index": int(remote.gpu),
        "gpu_uuid": remote.gpu_uuid,
        "gpu_pci_selector": remote.pci_selector,
        "blender_binary": worker.REMOTE_BLENDERS[family],
        "preference_writer": worker.VULKAN_PROFILE_PREFERENCE_WRITER,
        "graphics_driver_version": worker.RUNTIME_DRIVER_VERSION,
        "graphics_runtime_source_sha256": worker.RUNTIME_SOURCE_SHA256,
        "graphics_runtime_manifest_sha256": worker.RUNTIME_MANIFEST_DIGEST,
        "graphics_environment_policy": worker.RUNTIME_ENVIRONMENT_POLICY,
        "graphics_environment_fingerprint": worker.runtime_environment_fingerprint(
            worker.REMOTE_GL_ROOT,
        ),
        "device_select_environment_policy": (
            worker.DEVICE_SELECT_ENVIRONMENT_POLICY
        ),
        "device_select_environment_fingerprint": (
            worker.device_select_environment_fingerprint()
        ),
        "gpu_pid_attestation_policy": worker.GPU_PID_ATTESTATION_POLICY,
        "gpu0_aux_graphics_max_fb_mib": (
            worker.GPU0_AUX_GRAPHICS_MAX_FB_MIB
        ),
        "gpu_pid_attestation_stable_samples": (
            worker.GPU_PID_ATTESTATION_STABLE_SAMPLES
        ),
        "profile_userpref_sha256": "a" * 64,
        "blender_binary_sha256": "b" * 64,
        "preferred_device": f"10de/2b85/{preferred_index}",
    })


def valid_vulkan_attestation(remote: worker.Remote) -> str:
    return (
        f"{worker.GPU_PID_ATTESTATION_MARKER} pid=1234 "
        f"uuid={remote.gpu_uuid} "
        f"policy={worker.GPU_PID_ATTESTATION_POLICY} "
        f"observed={remote.gpu_uuid} pmon_indices={remote.gpu} "
        f"samples={worker.GPU_PID_ATTESTATION_STABLE_SAMPLES} "
        "aux_gpu0_fb_mb=0\n"
    )


def strict_render_recovery_payload(
    paths: worker.RemoteRenderDiagnosticPaths,
    remote: FakeRemote,
    *,
    sentinel_schema: str = "total_asset_remote_render_exit.v1",
    sentinel_owner: str | None = None,
    sentinel_attempt: str | None = None,
    sentinel_exitstatus: str = "0",
    counts: dict[str, int] | None = None,
    marker_lines: list[str] | None = None,
    extra_sentinel_lines: tuple[str, ...] = (),
    uses_vulkan: bool = False,
    require_monitor: bool = True,
) -> worker.RemoteCommandOutput:
    effective_counts = {
        "remote_started": 1,
        "remote_command_exit": 1,
        "runtime_ready": 1,
        "device_select_ready": 1 if uses_vulkan else 0,
        "monitor_started": 1 if require_monitor else 0,
        "monitor_ready": 1 if require_monitor else 0,
        **(counts or {}),
    }
    runtime_marker = (
        (
            f"{worker.PINNED_NATIVE_RUNTIME_READY_MARKER} "
            f"driver={worker.NATIVE_SYSTEM_DRIVER_VERSION} "
            f"manifest={worker.pinned_native_runtime_manifest_digest()}"
        )
        if getattr(remote, "native_system_graphics_runtime", False)
        else (
            f"{worker.RUNTIME_READY_MARKER} driver={worker.RUNTIME_DRIVER_VERSION}"
        )
    )
    default_markers = [
        (
            f"{worker.REMOTE_RENDER_DIAGNOSTIC_MARKER} phase=started "
            f"owner_sha256={paths.owner_sha256} attempt_id={paths.attempt_id}"
        ),
        runtime_marker,
        *(
            [
                f"{worker.DEVICE_SELECT_READY_MARKER} "
                f"fingerprint={worker.device_select_environment_fingerprint()}"
            ]
            if uses_vulkan
            else []
        ),
        *(
            [
                (
                    f"{worker.VULKAN_RENDER_MONITOR_STARTED_MARKER} pid=1234 "
                    f"policy={worker.GPU_PID_ATTESTATION_POLICY}"
                ),
                (
                    f"{worker.VULKAN_RENDER_MONITOR_READY_MARKER} pid=1234 "
                    f"uuid={remote.gpu_uuid} "
                    f"policy={worker.GPU_PID_ATTESTATION_POLICY} "
                    f"observed={remote.gpu_uuid} pmon_indices={remote.gpu} "
                    f"samples={worker.GPU_PID_ATTESTATION_STABLE_SAMPLES}"
                ),
            ]
            if require_monitor
            else []
        ),
        (
            f"{worker.REMOTE_RENDER_DIAGNOSTIC_MARKER} phase=command_exit "
            f"owner_sha256={paths.owner_sha256} attempt_id={paths.attempt_id} "
            "exitstatus=0"
        ),
    ]
    effective_markers = default_markers if marker_lines is None else marker_lines
    lines = [
        (
            f"{worker.REMOTE_RENDER_DIAGNOSTIC_MARKER} phase=transport_recovery "
            f"owner_sha256={paths.owner_sha256} attempt_id={paths.attempt_id}"
        ),
        "log_state=present",
        "log_marker_counts_begin=1",
        *(f"{key}={value}" for key, value in effective_counts.items()),
        "log_marker_counts_end=1",
        "log_marker_lines_begin=1",
        *effective_markers,
        "log_marker_lines_end=1",
        "log_tail_begin=1",
        "Blender render complete",
        "log_tail_end=1",
        "exit_sentinel_state=present",
        "exit_sentinel_begin=1",
        f"schema={sentinel_schema}",
        f"owner_sha256={sentinel_owner or paths.owner_sha256}",
        f"attempt_id={sentinel_attempt or paths.attempt_id}",
        f"exitstatus={sentinel_exitstatus}",
        *extra_sentinel_lines,
        "exit_sentinel_end=1",
    ]
    return worker.RemoteCommandOutput("\n".join(lines) + "\n", "")


class WorkerGuardTests(unittest.TestCase):
    def _run_formal_gpu_monitor(
        self,
        *,
        inventory_rows: tuple[tuple[object, object, object], ...],
        compute_uuids: tuple[str, ...],
        pmon_rows: tuple[tuple[int, str, int, int], ...],
        expected_index: int = 2,
        expected_uuid: str = "GPU-6d5aa9b6-c4f7-9b3a-6932-b25e6c217028",
    ) -> subprocess.CompletedProcess[str]:
        """Execute the generated daemon against one deterministic fake SMI."""

        with tempfile.TemporaryDirectory() as temporary:
            fake_smi = Path(temporary) / "nvidia-smi"
            fake_smi.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                f"inventory_rows = {inventory_rows!r}\n"
                f"compute_uuids = {compute_uuids!r}\n"
                f"pmon_rows = {pmon_rows!r}\n"
                "args = ' '.join(sys.argv[1:])\n"
                "pid = os.getppid()\n"
                "if '--query-gpu=index,uuid,memory.total' in args:\n"
                "    for row in inventory_rows:\n"
                "        print(', '.join(str(value) for value in row))\n"
                "elif '--query-compute-apps=gpu_uuid,pid' in args:\n"
                "    for value in compute_uuids:\n"
                "        print(f'{value}, {pid}')\n"
                "elif 'pmon -s m -c 1' in args:\n"
                "    print('# gpu pid type fb ccpm command')\n"
                "    for index, row_type, fb, ccpm in pmon_rows:\n"
                "        print(f'{index} {pid} {row_type} {fb} {ccpm} blender')\n"
                "else:\n"
                "    sys.exit(93)\n",
                encoding="utf-8",
            )
            fake_smi.chmod(0o700)
            with mock.patch.object(worker, "NVIDIA_SMI_BINARY", str(fake_smi)):
                expression = worker.blender_render_gpu_monitor_expression(
                    expected_uuid,
                    expected_index,
                )
            return subprocess.run(
                [sys.executable, "-c", expression],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

    def test_graphics_only_pmon_process_is_busy_on_exact_gpu(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        output = "\n".join((
            "TOTAL_ASSET_GPU_PROCESSES_COMPUTE_BEGIN",
            "",
            "TOTAL_ASSET_GPU_PROCESSES_COMPUTE_END",
            "TOTAL_ASSET_GPU_PROCESSES_INVENTORY_BEGIN",
            "3, GPU-287F358A-B090-C1FC-89E3-61D3BE51D37C",
            "TOTAL_ASSET_GPU_PROCESSES_INVENTORY_END",
            "TOTAL_ASSET_GPU_PROCESSES_PMON_BEGIN",
            "# gpu pid type sm mem enc dec command",
            "3 4242 G - - - - blender",
            "TOTAL_ASSET_GPU_PROCESSES_PMON_END",
        ))
        with mock.patch.object(remote, "run", return_value=output):
            processes = remote.gpu_compute_processes()
        self.assertEqual(
            processes,
            [{
                "gpu_uuid": remote.gpu_uuid,
                "pid": "4242",
                "name": "blender",
            }],
        )

    def test_runtime_invalid_marker_requires_authenticated_rc78(self) -> None:
        diagnostic = (
            worker.RUNTIME_INVALID_MARKER + " code=manifest_digest_untrusted"
        )
        self.assertEqual(
            worker.remote_runtime_capability_error(diagnostic, returncode=78),
            ("nvidia_graphics_runtime_invalid", False),
        )
        self.assertIsNone(
            worker.remote_runtime_capability_error(diagnostic, returncode=1)
        )
        native_diagnostic = (
            worker.NATIVE_SYSTEM_INVALID_MARKER
            + " code=system_library_version_mismatch"
        )
        self.assertEqual(
            worker.remote_runtime_capability_error(
                native_diagnostic, returncode=78
            ),
            ("nvidia_graphics_runtime_invalid", False),
        )
        self.assertIsNone(
            worker.remote_runtime_capability_error(
                native_diagnostic, returncode=1
            )
        )
        pinned_native_diagnostic = (
            worker.PINNED_NATIVE_RUNTIME_INVALID_MARKER
            + " code=runtime_file_invalid"
        )
        self.assertEqual(
            worker.remote_runtime_capability_error(
                pinned_native_diagnostic, returncode=78
            ),
            ("nvidia_graphics_runtime_invalid", False),
        )

        remote = mock.Mock(gpu="3")
        remote.run.side_effect = worker.RemoteCommandError(
            78, stderr=diagnostic
        )
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
            worker.run_remote_render(
                remote,
                "BLENDER_MUST_NOT_START",
                owner="test-runtime-drift",
                wait_seconds=0,
            )
        self.assertEqual(caught.exception.code, "nvidia_graphics_runtime_invalid")

        layer_diagnostic = (
            worker.DEVICE_SELECT_INVALID_MARKER + " code=layer_binary_hash_mismatch"
        )
        self.assertEqual(
            worker.remote_runtime_capability_error(
                layer_diagnostic, returncode=78
            ),
            ("vulkan_device_select_runtime_invalid", False),
        )
        remote.run.side_effect = worker.RemoteCommandError(
            78, stderr=layer_diagnostic
        )
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
            worker.run_remote_render(
                remote,
                "BLENDER_MUST_NOT_START",
                owner="test-device-select-runtime-drift",
                wait_seconds=0,
            )
        self.assertEqual(
            caught.exception.code, "vulkan_device_select_runtime_invalid"
        )

    def test_blender_shared_library_loader_failure_requires_exit_127(self) -> None:
        diagnostic = (
            "/root/blender-4.5.10-linux-x64/blender: error while loading "
            "shared libraries: libSM.so.6: cannot open shared object file: "
            "No such file or directory"
        )
        self.assertEqual(
            worker.remote_runtime_capability_error(
                diagnostic, returncode=127
            ),
            ("blender_runtime_shared_library_missing", False),
        )
        self.assertIsNone(
            worker.remote_runtime_capability_error(
                diagnostic, returncode=1
            )
        )

        remote = mock.Mock(gpu="0")
        remote.run.side_effect = worker.RemoteCommandError(
            127, stderr=diagnostic
        )
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
            worker.run_remote_render(
                remote,
                "BLENDER_MUST_NOT_START",
                owner="test-missing-shared-library",
                wait_seconds=0,
            )
        self.assertEqual(
            caught.exception.code,
            "blender_runtime_shared_library_missing",
        )

    def test_formal_vulkan_monitor_is_full_lifetime_and_fail_closed(self) -> None:
        expression = worker.blender_render_gpu_monitor_expression(
            "GPU-287f358a-b090-c1fc-89e3-61d3be51d37c", 3
        )
        compile(expression, "<formal-vulkan-monitor>", "exec")
        self.assertIn("threading.Thread", expression)
        self.assertIn("daemon=True", expression)
        self.assertIn("--query-compute-apps=gpu_uuid,pid", expression)
        self.assertIn("'pmon', '-s', 'm', '-c', '1'", expression)
        self.assertIn("compute_uuids != {expected}", expression)
        self.assertIn("if compute_uuids and compute_uuids != {expected}", expression)
        self.assertNotIn(
            "if compute_uuids != {expected} or expected_index not in pmon_rows",
            expression,
        )
        self.assertIn("if pmon_rows and expected_index not in pmon_rows", expression)
        self.assertNotIn("target_evidence_incomplete", expression)
        self.assertIn(
            "incomplete_grace_seconds = "
            f"{worker.GPU_PID_ATTESTATION_INCOMPLETE_GRACE_SECONDS}",
            expression,
        )
        self.assertIn(
            "sample_interval_seconds = "
            f"{worker.GPU_PID_ATTESTATION_SAMPLE_INTERVAL_SECONDS}",
            expression,
        )
        self.assertIn(
            "attested_interval_seconds = "
            f"{worker.GPU_PID_ATTESTATION_ATTESTED_INTERVAL_SECONDS}",
            expression,
        )
        self.assertIn(
            "monitor_stop.wait(attested_interval_seconds", expression
        )
        self.assertIn("if marker_emitted else sample_interval_seconds", expression)
        self.assertIn(
            f"probe_timeout_seconds = {worker.GPU_PID_ATTESTATION_PROBE_TIMEOUT_SECONDS}",
            expression,
        )
        self.assertIn(
            f"probe_attempts = {worker.GPU_PID_ATTESTATION_PROBE_ATTEMPTS}",
            expression,
        )
        self.assertIn("except subprocess.TimeoutExpired", expression)
        self.assertIn("'_timeout_retries_exhausted'", expression)
        self.assertIn("atexit.register(finalize_monitor)", expression)
        self.assertIn(
            "attested_event.wait(incomplete_grace_seconds)", expression
        )
        self.assertNotIn("final_attestation_incomplete", expression)
        self.assertIn("extras - {0}", expression)
        self.assertIn("aux_type != 'G'", expression)
        self.assertIn("aux_fb > aux_fb_limit", expression)
        self.assertIn("aux_ccpm != 0", expression)
        self.assertIn("os._exit(78)", expression)
        self.assertIn(
            "os.environ.pop('TOTAL_ASSET_EXPECTED_GPU_UUID', None)",
            expression,
        )
        self.assertIn(worker.VULKAN_RENDER_MONITOR_READY_MARKER, expression)

    def test_pre_gpu_asset_exit_is_not_overwritten_by_monitor_exit_78(self) -> None:
        expression = worker.blender_render_gpu_monitor_expression(
            "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c", 3
        )

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                return None

        registered: list[object] = []
        namespace: dict[str, object] = {}
        with (
            mock.patch("atexit.register", side_effect=registered.append),
            mock.patch.object(threading, "Thread", DeferredThread),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            exec(expression, namespace)
        self.assertEqual(len(registered), 1)
        namespace["incomplete_grace_seconds"] = 0
        with mock.patch.object(os, "_exit") as exit_process:
            registered[0]()  # type: ignore[operator]
        exit_process.assert_not_called()
        self.assertTrue(namespace["monitor_stop"].is_set())  # type: ignore[union-attr]

    def test_pre_gpu_loading_wait_does_not_fail_target_attestation(self) -> None:
        expected = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        other = "gpu-11111111-1111-1111-1111-111111111111"
        expression = worker.blender_render_gpu_monitor_expression(expected, 3)

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                return None

        class BoundedStop:
            def __init__(self) -> None:
                self.waits = 0

            def is_set(self) -> bool:
                return self.waits >= 2

            def wait(self, _seconds: float) -> bool:
                self.waits += 1
                return self.is_set()

            def set(self) -> None:
                self.waits = 2

        def nvidia_probe(args, **_kwargs):
            if "--query-gpu=index,uuid,memory.total" in args:
                stdout = f"0, {other}, 24576\n3, {expected}, 24576\n"
            elif "--query-compute-apps=gpu_uuid,pid" in args:
                stdout = ""
            elif "pmon" in args:
                stdout = "# gpu pid type sm mem enc dec command\n"
            else:  # pragma: no cover - generated monitor owns the command set
                self.fail(f"unexpected generated nvidia-smi probe: {args}")
            return subprocess.CompletedProcess(args, 0, stdout, "")

        namespace: dict[str, object] = {}
        with (
            mock.patch("atexit.register"),
            mock.patch.object(threading, "Thread", DeferredThread),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            exec(expression, namespace)
        namespace["monitor_stop"] = BoundedStop()
        namespace["incomplete_grace_seconds"] = 0
        with (
            mock.patch.object(subprocess, "run", side_effect=nvidia_probe),
            mock.patch.object(os, "_exit") as exit_process,
        ):
            namespace["monitor_loop"]()  # type: ignore[operator]
        exit_process.assert_not_called()
        self.assertFalse(namespace["attested_event"].is_set())  # type: ignore[union-attr]

    def test_formal_monitor_allows_bounded_gpu0_graphics_scene_mirror(self) -> None:
        gpu0 = "GPU-54fad6b2-bbad-95b8-ea94-cc94237c8860"
        target = "GPU-6d5aa9b6-c4f7-9b3a-6932-b25e6c217028"
        completed = self._run_formal_gpu_monitor(
            inventory_rows=((0, gpu0, 24576), (2, target, 24576)),
            compute_uuids=(target,),
            pmon_rows=((0, "G", 4608, 0), (2, "C+G", 256, 0)),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(worker.VULKAN_RENDER_MONITOR_READY_MARKER, completed.stdout)

    def test_formal_monitor_rejects_gpu0_aux_above_memory_fraction(self) -> None:
        gpu0 = "GPU-54fad6b2-bbad-95b8-ea94-cc94237c8860"
        target = "GPU-6d5aa9b6-c4f7-9b3a-6932-b25e6c217028"
        completed = self._run_formal_gpu_monitor(
            inventory_rows=((0, gpu0, 24576), (2, target, 24576)),
            compute_uuids=(target,),
            # 25% of 24576 MiB is exactly 6144 MiB.
            pmon_rows=((0, "G", 6145, 0), (2, "C+G", 256, 0)),
        )
        self.assertEqual(completed.returncode, 78)
        self.assertIn(
            "gpu0_aux_graphics_out_of_policy_observed_6145_limit_6144",
            completed.stderr,
        )

    def test_formal_monitor_keeps_non_memory_gpu_identity_checks_strict(self) -> None:
        gpu0 = "GPU-54fad6b2-bbad-95b8-ea94-cc94237c8860"
        gpu1 = "GPU-f36efb06-3505-155e-8c9f-7beac51317ca"
        target = "GPU-6d5aa9b6-c4f7-9b3a-6932-b25e6c217028"
        inventory = ((0, gpu0, 24576), (1, gpu1, 24576), (2, target, 24576))
        cases = {
            "gpu0_compute_uuid": (
                (target, gpu0),
                ((0, "G", 4608, 0), (2, "C+G", 256, 0)),
                "compute_uuid_mismatch",
            ),
            "gpu0_compute_type": (
                (target,),
                ((0, "C+G", 4608, 0), (2, "C+G", 256, 0)),
                "gpu0_aux_graphics_out_of_policy",
            ),
            "gpu0_ccpm": (
                (target,),
                ((0, "G", 4608, 1), (2, "C+G", 256, 0)),
                "gpu0_aux_graphics_out_of_policy",
            ),
            "third_card": (
                (target,),
                ((1, "G", 1, 0), (2, "C+G", 256, 0)),
                "pmon_non_target_process",
            ),
        }
        for label, (compute, pmon, failure) in cases.items():
            with self.subTest(label=label):
                completed = self._run_formal_gpu_monitor(
                    inventory_rows=inventory,
                    compute_uuids=compute,
                    pmon_rows=pmon,
                )
                self.assertEqual(completed.returncode, 78)
                self.assertIn(failure, completed.stderr)

    def test_formal_monitor_fails_closed_on_unknown_or_duplicate_memory_inventory(
        self,
    ) -> None:
        gpu0 = "GPU-54fad6b2-bbad-95b8-ea94-cc94237c8860"
        target = "GPU-6d5aa9b6-c4f7-9b3a-6932-b25e6c217028"
        cases = {
            "unknown": ((0, gpu0, "N/A"), (2, target, 24576)),
            "zero": ((0, gpu0, 0), (2, target, 24576)),
            "duplicate_index": (
                (0, gpu0, 24576),
                (0, target, 24576),
                (2, target, 24576),
            ),
        }
        for label, inventory in cases.items():
            with self.subTest(label=label):
                completed = self._run_formal_gpu_monitor(
                    inventory_rows=inventory,
                    compute_uuids=(target,),
                    pmon_rows=((2, "C+G", 256, 0),),
                )
                self.assertEqual(completed.returncode, 78)
                self.assertIn("inventory_", completed.stderr)

    def test_formal_monitor_proof_required_by_remote_render(self) -> None:
        remote = mock.Mock(
            gpu="3",
            gpu_uuid="gpu-287f358a-b090-c1fc-89e3-61d3be51d37c",
        )
        remote.run.return_value = (
            f"{worker.VULKAN_RENDER_MONITOR_READY_MARKER} pid=1234 "
            f"uuid={remote.gpu_uuid} policy={worker.GPU_PID_ATTESTATION_POLICY} "
            f"observed={remote.gpu_uuid} pmon_indices=3 "
            f"samples={worker.GPU_PID_ATTESTATION_STABLE_SAMPLES}\n"
        )
        worker.run_remote_render(
            remote,
            "blender -b asset.blend",
            owner="formal-monitor-test",
            wait_seconds=0,
            require_vulkan_monitor=True,
        )
        remote.run.return_value = "render finished without monitor proof\n"
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="formal-monitor-missing",
                wait_seconds=0,
                require_vulkan_monitor=True,
            )
        self.assertEqual(
            caught.exception.code, "gpu_process_uuid_attestation_failed"
        )

    def test_short_render_exit_synchronously_waits_for_one_attested_marker(self) -> None:
        expected = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        expression = worker.blender_render_gpu_monitor_expression(expected, 3)
        registered: list[object] = []
        output = io.StringIO()
        errors = io.StringIO()
        pid = str(os.getpid())
        other = "gpu-11111111-1111-1111-1111-111111111111"

        def nvidia_probe(args, **_kwargs):
            if "--query-gpu=index,uuid,memory.total" in args:
                stdout = f"0, {other}, 24576\n3, {expected}, 24576\n"
            elif "--query-compute-apps=gpu_uuid,pid" in args:
                stdout = f"{expected}, {pid}\n"
            elif "pmon" in args:
                stdout = (
                    "# gpu pid type sm mem enc dec command\n"
                    f"3 {pid} G 64 0 - - blender\n"
                )
            else:  # pragma: no cover - generated monitor owns the command set
                self.fail(f"unexpected generated nvidia-smi probe: {args}")
            return subprocess.CompletedProcess(args, 0, stdout, "")

        namespace: dict[str, object] = {}
        with (
            mock.patch("atexit.register", side_effect=registered.append),
            mock.patch.object(subprocess, "run", side_effect=nvidia_probe) as run,
            mock.patch.object(sys, "stdout", output),
            mock.patch.object(sys, "stderr", errors),
        ):
            exec(expression, namespace)
            self.assertEqual(len(registered), 1)
            registered[0]()  # type: ignore[operator]
            namespace["monitor_thread"].join(timeout=1)  # type: ignore[union-attr]

        ready_lines = [
            line
            for line in output.getvalue().splitlines()
            if line.startswith(worker.VULKAN_RENDER_MONITOR_READY_MARKER)
        ]
        self.assertEqual(len(ready_lines), 1)
        self.assertIn(f"uuid={expected}", ready_lines[0])
        self.assertIn(
            f"samples={worker.GPU_PID_ATTESTATION_STABLE_SAMPLES}",
            ready_lines[0],
        )
        self.assertEqual(run.call_count, worker.GPU_PID_ATTESTATION_STABLE_SAMPLES * 3)
        self.assertEqual(errors.getvalue(), "")

    def test_render_monitor_retries_one_nvidia_smi_timeout_but_is_bounded(self) -> None:
        expression = worker.blender_render_gpu_monitor_expression(
            "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c", 3
        )

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                return None

        namespace: dict[str, object] = {}
        completed = subprocess.CompletedProcess([], 0, "inventory-ok\n", "")
        timeout = subprocess.TimeoutExpired([worker.NVIDIA_SMI_BINARY], 5)
        with (
            mock.patch("atexit.register"),
            mock.patch.object(threading, "Thread", DeferredThread),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            exec(expression, namespace)
        namespace["probe_retry_seconds"] = 0
        with mock.patch.object(
            subprocess, "run", side_effect=[timeout, completed]
        ) as run:
            stdout = namespace["probe"]([worker.NVIDIA_SMI_BINARY], "inventory")  # type: ignore[operator]
        self.assertEqual(stdout, "inventory-ok\n")
        self.assertEqual(run.call_count, 2)

        class MonitorExit(RuntimeError):
            pass

        with (
            mock.patch.object(subprocess, "run", side_effect=timeout) as run,
            mock.patch.object(os, "_exit", side_effect=MonitorExit) as exit_process,
            mock.patch.object(sys, "stderr", io.StringIO()),
            self.assertRaises(MonitorExit),
        ):
            namespace["probe"]([worker.NVIDIA_SMI_BINARY], "inventory")  # type: ignore[operator]
        self.assertEqual(run.call_count, worker.GPU_PID_ATTESTATION_PROBE_ATTEMPTS)
        exit_process.assert_called_once_with(78)

    def test_render_monitor_uuid_mismatch_still_exits_fail_closed(self) -> None:
        expression = worker.blender_render_gpu_monitor_expression(
            "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c", 3
        )

        class DeferredThread:
            def __init__(self, *, target, name, daemon):
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                return None

        namespace: dict[str, object] = {}
        with (
            mock.patch("atexit.register"),
            mock.patch.object(threading, "Thread", DeferredThread),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            exec(expression, namespace)

        wrong_inventory = subprocess.CompletedProcess(
            [],
            0,
            "0, gpu-11111111-1111-1111-1111-111111111111\n"
            "3, gpu-22222222-2222-2222-2222-222222222222\n",
            "",
        )

        class MonitorExit(RuntimeError):
            pass

        with (
            mock.patch.object(subprocess, "run", return_value=wrong_inventory),
            mock.patch.object(os, "_exit", side_effect=MonitorExit) as exit_process,
            mock.patch.object(sys, "stderr", io.StringIO()),
            self.assertRaises(MonitorExit),
        ):
            namespace["monitor_loop"]()  # type: ignore[operator]
        exit_process.assert_called_once_with(78)

    def test_success_exit_stderr_gpu_failure_is_not_hidden(self) -> None:
        remote = mock.Mock(gpu="3")
        remote.run.return_value = worker.RemoteCommandOutput(
            "Blender quit\n",
            "RuntimeError: TOTAL_ASSET_GPU_UUID_ATTESTATION_FAILED "
            "observed=none probe=ok\n",
        )
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
            worker.run_remote_render(
                remote,
                "blender --python render.py",
                owner="formal-success-stderr",
                wait_seconds=0,
            )
        self.assertEqual(
            caught.exception.code, "gpu_process_uuid_attestation_failed"
        )

    def test_source_bundle_fingerprint_is_content_addressed_and_process_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            (root / "textures").mkdir(parents=True)
            (root / "asset.blend").write_bytes(b"BLENDER-v500" + b"x" * 64)
            (root / "textures" / "albedo.png").write_bytes(b"texture")
            (root / "ignored.zip").write_bytes(b"excluded archive")
            worker._SOURCE_FILE_DIGEST_CACHE.clear()

            first = worker.source_bundle_fingerprint(root)
            second = worker.source_bundle_fingerprint(root)
            self.assertEqual(first, second)
            self.assertEqual(first.file_count, 2)
            self.assertEqual(len(worker._SOURCE_FILE_DIGEST_CACHE), 2)

            (root / "textures" / "albedo.png").write_bytes(b"changed texture")
            changed = worker.source_bundle_fingerprint(root)
            self.assertNotEqual(first.digest, changed.digest)

            other = Path(temporary) / "same-content"
            shutil.copytree(root, other)
            self.assertEqual(
                changed.digest,
                worker.source_bundle_fingerprint(other).digest,
            )

    def test_source_bundle_keeps_extracted_archive_directories_but_not_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            extracted = root / "__extracted" / "source.zip" / "nested"
            extracted.mkdir(parents=True)
            (extracted / "asset.blend").write_bytes(b"BLENDER-v500")
            (root / "source.zip").write_bytes(b"archive bytes are not transferred")

            fingerprint = worker.source_bundle_fingerprint(root)

            self.assertEqual(fingerprint.file_count, 1)
            self.assertEqual(
                fingerprint.size_bytes,
                (extracted / "asset.blend").stat().st_size,
            )

    def test_remote_put_excludes_archive_files_without_excluding_archive_directories(self) -> None:
        remote = object.__new__(worker.Remote)
        remote.host = "root@example.invalid"
        remote.rsync_transfer = mock.Mock()

        remote.put(Path("/tmp/source"), "/tmp/target", timeout=321)

        options = remote.rsync_transfer.call_args.kwargs["options"]
        include_directory = options.index("--include")
        exclude_archive = next(
            index
            for index, value in enumerate(options)
            if value == "*.zip"
        )
        self.assertEqual(options[include_directory + 1], "*/")
        self.assertLess(include_directory, exclude_archive)
        self.assertEqual(remote.rsync_transfer.call_args.kwargs["timeout"], 321)

    def test_source_bundle_cache_threshold_leaves_small_and_model_only_unchanged(self) -> None:
        self.assertEqual(worker.SOURCE_BUNDLE_CACHE_MIN_BYTES, 64 * 1024**2)
        self.assertEqual(
            worker.SOURCE_BUNDLE_CACHE_ROOT,
            "/F00120250029/lixiang_share/wuminghao_share/"
            "video2blender_source_cache/sha256-tree-v1",
        )
        self.assertNotIn("/tmp/", worker.SOURCE_BUNDLE_CACHE_ROOT)
        with mock.patch.object(worker, "SOURCE_BUNDLE_CACHE_MIN_BYTES", 1024):
            self.assertFalse(worker.should_use_source_bundle_cache(
                size_bytes=1023,
                model_only=False,
            ))
            self.assertTrue(worker.should_use_source_bundle_cache(
                size_bytes=1024,
                model_only=False,
            ))
            self.assertFalse(worker.should_use_source_bundle_cache(
                size_bytes=4096,
                model_only=True,
            ))

    def test_shared_cache_claim_publish_and_hash_are_atomic_across_clients(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "asset.blend").write_bytes(b"BLENDER-v500\nshared")
            fingerprint = worker.source_bundle_fingerprint(source)
            cache_root = str(root / "shared-gpfs-cache")

            class LocalShellRemote:
                source_cache_claim = worker.Remote.source_cache_claim
                source_cache_begin_upload = worker.Remote.source_cache_begin_upload
                source_cache_verify_upload = worker.Remote.source_cache_verify_upload
                source_cache_verify_path = worker.Remote.source_cache_verify_path
                source_cache_publish = worker.Remote.source_cache_publish
                source_cache_abort = worker.Remote.source_cache_abort
                source_cache_stage = worker.Remote.source_cache_stage

                def run(self, command: str, timeout: int) -> str:
                    completed = subprocess.run(
                        ["bash", "-c", command],
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=timeout,
                    )
                    if completed.returncode != 0:
                        raise worker.RemoteTransportError(
                            "test_shared_cache_command_failed",
                            detail=completed.stderr,
                            returncode=completed.returncode,
                        )
                    return completed.stdout

            clients = [LocalShellRemote(), LocalShellRemote()]
            tokens = ["1" * 32, "2" * 32]
            claims: dict[str, str] = {}
            barrier = threading.Barrier(2)

            def claim(index: int) -> None:
                barrier.wait()
                claims[tokens[index]] = clients[index].source_cache_claim(
                    fingerprint.cache_key, tokens[index]
                )

            with mock.patch.object(worker, "SOURCE_BUNDLE_CACHE_ROOT", cache_root):
                threads = [threading.Thread(target=claim, args=(index,)) for index in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(sorted(claims.values()), ["busy", "owner"])

                owner_token = next(
                    token for token, state in claims.items() if state == "owner"
                )
                owner = clients[tokens.index(owner_token)]
                remote_payload = Path(owner.source_cache_begin_upload(
                    fingerprint.cache_key, owner_token
                ))
                shutil.copytree(source, remote_payload, dirs_exist_ok=True)
                owner.source_cache_verify_upload(
                    fingerprint, owner_token, timeout=60
                )
                self.assertEqual(
                    owner.source_cache_publish(fingerprint.cache_key, owner_token),
                    "published",
                )

                waiter = clients[1 - tokens.index(owner_token)]
                self.assertEqual(
                    waiter.source_cache_claim(
                        fingerprint.cache_key,
                        tokens[1 - tokens.index(owner_token)],
                    ),
                    "ready",
                )
                staged = root / "private-stage"
                waiter.source_cache_stage(fingerprint.cache_key, str(staged))
                waiter.source_cache_verify_path(
                    fingerprint, str(staged), timeout=60
                )
                self.assertEqual(
                    (staged / "asset.blend").read_bytes(),
                    (source / "asset.blend").read_bytes(),
                )

    def test_shared_cache_failure_falls_back_to_verified_direct_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "asset.blend").write_bytes(b"BLENDER-v500")
            fingerprint = worker.source_bundle_fingerprint(source)
            calls: list[tuple[str, object]] = []

            class CacheUnavailableRemote:
                def source_cache_claim(
                    self, _key: str, _token: str, *, required_bytes: int = 0
                ) -> str:
                    del required_bytes
                    raise worker.RemoteTransportError(
                        "worker_source_cache_shared_root_unavailable"
                    )

                def run(self, command: str, timeout: int) -> str:
                    calls.append(("reset", (command, timeout)))
                    return ""

                def put(self, local: Path, remote: str, timeout: int) -> None:
                    calls.append(("put", (local, remote, timeout)))

                def source_cache_verify_path(
                    self,
                    observed: worker.SourceBundleFingerprint,
                    remote: str,
                    *,
                    timeout: int,
                ) -> None:
                    calls.append(("verify", (observed, remote, timeout)))

            outcome = worker.stage_source_bundle_with_cache_fallback(
                CacheUnavailableRemote(),
                source,
                "/tmp/private-asset",
                fingerprint,
                timeout=1800,
            )
            self.assertIsNone(outcome.cache_result)
            self.assertEqual(
                outcome.fallback_reason,
                "worker_source_cache_shared_root_unavailable",
            )
            self.assertEqual([item[0] for item in calls], ["reset", "put", "verify"])

    def test_shared_cache_capacity_gate_is_inside_atomic_owner_claim(self) -> None:
        key = "c" * 64
        token = "d" * 32
        commands: list[str] = []

        class CapacityRemote:
            source_cache_claim = worker.Remote.source_cache_claim

            def run(self, command: str, timeout: int) -> str:
                self.timeout = timeout
                commands.append(command)
                return "SOURCE_CACHE_CAPACITY_FULL\n"

        remote = CapacityRemote()
        state = remote.source_cache_claim(
            key,
            token,
            required_bytes=80 * 1024**2,
        )
        self.assertEqual(state, "capacity_full")
        self.assertEqual(remote.timeout, 30)
        self.assertEqual(len(commands), 1)
        self.assertIn("if mkdir --", commands[0])
        self.assertIn("df -Pk", commands[0])
        self.assertIn("SOURCE_CACHE_CAPACITY_FULL", commands[0])
        self.assertLess(
            commands[0].index("if mkdir --"),
            commands[0].index("df -Pk"),
        )

    def test_staged_shared_cache_hash_mismatch_is_rejected(self) -> None:
        fingerprint = worker.SourceBundleFingerprint(
            schema=worker.SOURCE_BUNDLE_FINGERPRINT_SCHEMA,
            digest="e" * 64,
            size_bytes=123,
            file_count=1,
            entry_count=1,
        )
        observed = json.dumps(
            {
                "digest": "f" * 64,
                "size_bytes": 123,
                "file_count": 1,
                "entry_count": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        remote = object.__new__(worker.Remote)
        with mock.patch.object(
            worker.Remote,
            "run",
            return_value=f"SOURCE_CACHE_VERIFIED {observed}\n",
        ):
            with self.assertRaises(worker.RemoteTransportError) as caught:
                remote.source_cache_verify_path(
                    fingerprint,
                    "/tmp/private-stage",
                    timeout=60,
                )
        self.assertEqual(
            caught.exception.code,
            "worker_source_cache_integrity_mismatch",
        )

    def test_source_cache_does_not_stage_half_finished_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = "a" * 64
            paths = worker.source_cache_paths(
                key,
                cache_root=str(root / "cache"),
            )
            payload = Path(paths["ready"]) / "payload"
            payload.mkdir(parents=True)
            (payload / "partial.blend").write_bytes(b"partial")
            remote_asset = root / "asset"
            completed = subprocess.run(
                [
                    "sh",
                    "-c",
                    worker.build_source_cache_stage_command(
                        key,
                        str(remote_asset),
                        cache_root=str(root / "cache"),
                    ),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(remote_asset.exists())

    def test_source_cache_stage_command_quotes_asset_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = "b" * 64
            paths = worker.source_cache_paths(
                key,
                cache_root=str(root / "cache root"),
            )
            ready = Path(paths["ready"])
            (ready / "payload").mkdir(parents=True)
            (ready / "payload" / "asset.blend").write_bytes(b"safe")
            (ready / ".ready").write_text(key + "\n", encoding="utf-8")
            malicious_asset = root / "asset$(touch PWN)"
            completed = subprocess.run(
                [
                    "sh",
                    "-c",
                    worker.build_source_cache_stage_command(
                        key,
                        str(malicious_asset),
                        cache_root=str(root / "cache root"),
                    ),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((root / "PWN").exists())
            self.assertEqual((malicious_asset / "asset.blend").read_bytes(), b"safe")
            self.assertFalse((malicious_asset / "asset.blend").stat().st_mode & 0o222)

    def test_source_cache_concurrent_claim_uploads_once_and_waiter_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bundle"
            source.mkdir()
            (source / "asset.blend").write_bytes(b"BLENDER-v500")
            fingerprint = worker.source_bundle_fingerprint(source)
            state_lock = threading.Lock()
            upload_started = threading.Event()
            release_upload = threading.Event()
            shared: dict[str, object] = {
                "owner": "",
                "ready": False,
                "uploads": 0,
                "stages": 0,
            }

            class CacheRemote:
                def source_cache_claim(
                    self, _key: str, token: str, *, required_bytes: int = 0
                ) -> str:
                    del required_bytes
                    with state_lock:
                        if shared["ready"]:
                            return "ready"
                        if not shared["owner"]:
                            shared["owner"] = token
                            return "owner"
                        return "busy"

                def source_cache_begin_upload(self, _key: str, _token: str) -> str:
                    return "/tmp/private-cache-temp/payload"

                def put(self, _source: Path, _remote: str, timeout: int) -> None:
                    del timeout
                    with state_lock:
                        shared["uploads"] = int(shared["uploads"]) + 1
                    upload_started.set()
                    release_upload.wait(5)

                def source_cache_verify_upload(self, *_args: object, **_kwargs: object) -> None:
                    return None

                def source_cache_publish(self, _key: str, token: str) -> str:
                    with state_lock:
                        self_owner = shared["owner"] == token
                        self.assert_owner = self_owner
                        if not self_owner:
                            raise AssertionError("cache published by non-owner")
                        shared["ready"] = True
                        shared["owner"] = ""
                    return "published"

                def source_cache_abort(self, _key: str, token: str) -> None:
                    with state_lock:
                        if shared["owner"] == token:
                            shared["owner"] = ""

                def source_cache_stage(self, _key: str, _asset: str) -> None:
                    with state_lock:
                        if not shared["ready"]:
                            raise AssertionError("half-finished cache was staged")
                        shared["stages"] = int(shared["stages"]) + 1

                def source_cache_verify_path(
                    self,
                    _fingerprint: worker.SourceBundleFingerprint,
                    _remote_path: str,
                    *,
                    timeout: int,
                ) -> None:
                    del timeout

            results: list[worker.SourceBundleCacheResult] = []
            errors: list[BaseException] = []

            def run_one(index: int) -> None:
                try:
                    results.append(worker.stage_remote_source_bundle(
                        CacheRemote(),
                        source,
                        f"/tmp/asset-{index}",
                        fingerprint,
                        timeout=60,
                    ))
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(worker, "SOURCE_BUNDLE_CACHE_POLL_SECONDS", 0.01):
                owner_thread = threading.Thread(target=run_one, args=(1,))
                owner_thread.start()
                self.assertTrue(upload_started.wait(2))
                waiter_thread = threading.Thread(target=run_one, args=(2,))
                waiter_thread.start()
                time.sleep(0.03)
                release_upload.set()
                owner_thread.join(5)
                waiter_thread.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(shared["uploads"], 1)
            self.assertEqual(shared["stages"], 2)
            self.assertEqual(sorted(result.cache_hit for result in results), [False, True])

    def test_zero_model_cache_population_never_waits_or_stages_when_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bundle"
            source.mkdir()
            (source / "asset.blend").write_bytes(b"BLENDER-v500")
            fingerprint = worker.source_bundle_fingerprint(source)

            class BusyRemote:
                def source_cache_claim(
                    self, _key: str, _token: str, *, required_bytes: int = 0
                ) -> str:
                    self.required_bytes = required_bytes
                    return "busy"

                def source_cache_stage(self, *_args: object) -> None:
                    raise AssertionError("zero-model population staged a GPU workspace")

            remote = BusyRemote()
            with self.assertRaises(worker.RemoteTransportError) as caught:
                worker.ensure_remote_source_bundle_cached(
                    remote,
                    source,
                    fingerprint,
                    timeout=60,
                    wait_on_busy=False,
                )
            self.assertEqual(
                caught.exception.code, "worker_source_cache_population_busy"
            )
            self.assertEqual(remote.required_bytes, fingerprint.size_bytes)

    def test_source_blender_version_reads_only_bounded_raw_header(self) -> None:
        read_sizes: list[int] = []

        class ReadSpy(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                read_sizes.append(size)
                return super().read(size)

        stream = ReadSpy(b"BLENDER-v420" + b"x" * 4096)
        with mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(
                worker.source_blender_version(Path("bounded.blend")),
                "4.20",
            )
        self.assertEqual(read_sizes, [worker.BLEND_HEADER_PROBE_BYTES])

    def test_source_blender_version_supports_gzip_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "compressed.blend"
            model.write_bytes(gzip.compress(b"BLENDER-v360" + b"x" * 4096))
            self.assertEqual(worker.source_blender_version(model), "3.60")

    def test_source_blender_version_supports_zstd_header(self) -> None:
        executable = shutil.which("zstd")
        if not executable:
            self.skipTest("zstd CLI is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "header.raw"
            model = root / "compressed.blend"
            source.write_bytes(b"BLENDER17-01v0500" + b"x" * 4096)
            subprocess.run(
                [executable, "-q", "-f", str(source), "-o", str(model)],
                check=True,
            )
            self.assertEqual(worker.source_blender_version(model), "5.0")

    def test_source_blender_version_supports_extended_header_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "extended.blend"
            model.write_bytes(b"BLENDER19-02v0510" + b"x" * 64)
            self.assertEqual(worker.source_blender_version(model), "5.10")

    def test_compressed_header_probe_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad_gzip = root / "bad-gzip.blend"
            bad_gzip.write_bytes(worker.GZIP_MAGIC + b"not-a-gzip-stream")
            self.assertEqual(worker.source_blender_version(bad_gzip), "unknown")

            zstd_without_cli = root / "zstd-without-cli.blend"
            zstd_without_cli.write_bytes(worker.ZSTD_MAGIC + b"not-decoded")
            with mock.patch.object(worker.shutil, "which", return_value=None):
                self.assertEqual(
                    worker.source_blender_version(zstd_without_cli),
                    "unknown",
                )

    def test_fbx_uses_blender45_compatibility_runtime(self) -> None:
        binary, family, reason, rules = worker.select_model_runtime(
            Path("model-with-lights.fbx")
        )
        self.assertEqual(binary, worker.REMOTE_BLENDERS["4.5"])
        self.assertEqual(family, "4.5")
        self.assertIn("cast_shadow", reason)
        self.assertEqual(rules, ("fbx_blender45_importer_compatibility",))

        other_binary, other_family, other_reason, other_rules = (
            worker.select_model_runtime(Path("model.glb"))
        )
        self.assertEqual(other_binary, worker.REMOTE_BLENDER)
        self.assertEqual(other_family, "5.1")
        self.assertEqual(other_reason, "")
        self.assertEqual(other_rules, ())

    def test_missing_fbx_compatibility_runtime_is_nonterminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.fbx"
            model.write_bytes(b"Kaydara FBX Binary" + b"x" * 2048)
            status_path = root / "status.jsonl"
            remote = FakeRemote()

            def unavailable(command: str, timeout: int = 900) -> str:
                remote.commands.append((command, timeout))
                raise RuntimeError("exitstatus=1")

            remote.run = unavailable  # type: ignore[method-assign]
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(
                    worker,
                    "get_disk_capacity",
                    return_value=capacity,
                ),
            ):
                with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                    worker.render_one(
                        remote,
                        {
                            "asset_id": "fbx-1",
                            "identity_key": "fbx-identity",
                            "render_order": "1",
                            "render_batch": "0001",
                            "model_file": str(model),
                            "source_root": str(root),
                            "title": "FBX asset",
                        },
                        status_path,
                        "batch0001",
                    )

            self.assertEqual(
                caught.exception.code,
                "compatible_blender_runtime_unavailable",
            )
            self.assertFalse(status_path.exists())
            self.assertEqual(len(remote.commands), 1)
            self.assertIn(worker.REMOTE_BLENDERS["4.5"], remote.commands[0][0])

    def test_unknown_blend_is_conservatively_routed_to_gpu0(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "unknown.blend"
            model.write_bytes(b"not-a-recognized-blend-header")
            executable, family = worker.select_remote_blender(
                worker.source_blender_version(model)
            )
            self.assertEqual(executable, worker.REMOTE_BLENDER)
            self.assertEqual(family, "unknown")
            for engine_hint in ("source", "CYCLES"):
                with self.subTest(engine_hint=engine_hint):
                    self.assertTrue(worker.row_requires_source_family_gpu0({
                        "model_file": str(model),
                        "render_engine_hint": engine_hint,
                    }))

    def test_unknown_blend_blocks_worker_without_formal_asset_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "unknown.blend"
            model.write_bytes(b"not-a-recognized-blend-header")
            status_path = root / "inventory" / "status.jsonl"
            row = {
                "asset_id": "000001",
                "identity_key": "identity-1",
                "render_order": "1",
                "render_batch": "0001",
                "model_file": str(model),
                "source_root": str(root),
                "render_engine_hint": "CYCLES",
                "title": "unknown",
            }
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
            ):
                with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                    worker.render_one(
                        FakeRemote(), row, status_path, "batch0001"
                    )
            self.assertEqual(caught.exception.code, "legacy_blender_gpu0_required")
            self.assertFalse(status_path.exists())

    def test_deferred_repair_execution_is_exact_and_slots_are_disjoint(self) -> None:
        valid = {
            "strategy": "legacy_gpu0",
            "repair_slot_index": 1,
            "repair_slot_count": 2,
            "batch": "batch0001_deferred_legacy_repair",
            "batch_index": 1,
            "batch_size": 1000,
            "remote_port": 30773,
            "gpu": 0,
            "worker_index": 4,
            "worker_count": 11,
            "only_status": ("deferred",),
            "force": True,
            "resume_unprocessed_only": False,
            "runtime_aware_partition": False,
            "excluded_partitions": (),
        }
        worker.validate_deferred_repair_execution(**valid)
        worker.validate_deferred_repair_execution(**{
            **valid,
            "batch": "batch0002_deferred_legacy_repair",
            "batch_index": 2,
        })
        for changes in (
            {"batch": "batch0001"},
            {"gpu": 1},
            {"worker_index": 5},
            {"only_status": ("failed",)},
            {"force": False},
            {"repair_slot_index": 2},
            {"runtime_aware_partition": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_deferred_repair_execution(**{**valid, **changes})

        rows = [{"asset_id": str(index)} for index in range(21)]
        selected = []
        for slot in range(4):
            part = worker.select_repair_slot_rows(
                rows, slot_index=slot, slot_count=4
            )
            self.assertFalse({row["asset_id"] for row in selected} & {
                row["asset_id"] for row in part
            })
            selected.extend(part)
        self.assertEqual(
            {row["asset_id"] for row in selected},
            {row["asset_id"] for row in rows},
        )

    def test_formal_primary_worker_enforces_canonical_physical_slot(self) -> None:
        worker.validate_formal_primary_location(
            batch="batch0002",
            remote_port=worker.SECONDARY_REMOTE_PORT,
            gpu=2,
            worker_index=2,
            worker_count=11,
        )
        for changes in (
            {"worker_index": 3},
            {"remote_port": 30773, "worker_index": 2},
            {"worker_count": 8},
        ):
            arguments = {
                "batch": "batch0002",
                "remote_port": worker.SECONDARY_REMOTE_PORT,
                "gpu": 2,
                "worker_index": 2,
                "worker_count": 11,
            }
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_formal_primary_location(**arguments)

        # Repair labels have their own exact validators and are not mistaken
        # for formal primary work.
        worker.validate_formal_primary_location(
            batch="batch0001_deferred_vulkan_repair",
            remote_port=30773,
            gpu=1,
            worker_index=5,
            worker_count=11,
        )

    def test_failed_repair_execution_requires_exact_group_and_canonical_slot(self) -> None:
        valid = {
            "group": "course_timeout_exact",
            "batch": "batch0001_failed_repair_course_timeout_exact",
            "batch_index": 1,
            "batch_size": 1000,
            "remote_port": 30422,
            "gpu": 0,
            "worker_index": 0,
            "worker_count": 11,
            "only_status": ("failed",),
            "force": True,
            "resume_unprocessed_only": False,
            "runtime_aware_partition": False,
            "excluded_partitions": (),
            "deferred_strategy": "",
            "repair_slot_index": None,
            "repair_slot_count": None,
            "runtime_lane": "legacy_gpu0",
        }
        worker.validate_failed_repair_execution(**valid)
        worker.validate_failed_repair_execution(**{
            **valid,
            "remote_port": worker.TERTIARY_REMOTE_PORT,
            "gpu": 1,
            "worker_index": 9,
            "runtime_lane": "modern_vulkan",
        })
        worker.validate_failed_repair_execution(**{
            **valid,
            "remote_port": next(
                port
                for (port, gpu), index in
                worker.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
                if gpu == 3 and index == 7
            ),
            "gpu": 3,
            "worker_index": 7,
            "runtime_lane": "modern_vulkan",
        })
        for changes in (
            {"batch": "batch0001"},
            {"gpu": 1, "worker_index": 0},
            {"remote_port": 30773, "worker_index": 0},
            {"only_status": ("deferred",)},
            {"force": False},
            {"deferred_strategy": "legacy_gpu0"},
            {"runtime_lane": "modern_vulkan"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_failed_repair_execution(**{**valid, **changes})

    def test_failed_repair_runtime_lane_separates_mixed_group_before_attempt(self) -> None:
        rows = [
            {"asset_id": "legacy", "model_file": "legacy.blend"},
            {"asset_id": "modern", "model_file": "modern.blend"},
            {"asset_id": "mesh", "model_file": "mesh.fbx"},
        ]
        with mock.patch.object(
            worker,
            "row_requires_source_family_gpu0",
            side_effect=lambda row: row["asset_id"] == "legacy",
        ):
            legacy, legacy_excluded = (
                worker.select_failed_repair_runtime_lane_rows(
                    rows, "legacy_gpu0"
                )
            )
            modern, modern_excluded = (
                worker.select_failed_repair_runtime_lane_rows(
                    rows, "modern_vulkan"
                )
            )
        self.assertEqual([row["asset_id"] for row in legacy], ["legacy"])
        self.assertEqual(
            [row["asset_id"] for row in modern], ["modern", "mesh"]
        )
        self.assertEqual(legacy_excluded, 2)
        self.assertEqual(modern_excluded, 1)
        self.assertEqual(
            {row["asset_id"] for row in legacy + modern},
            {row["asset_id"] for row in rows},
        )

    def test_failed_repair_runtime_lane_is_bound_to_physical_gpu(self) -> None:
        self.assertEqual(
            worker.resolve_failed_repair_runtime_lane(
                group="transport", requested_lane="auto", gpu=0
            ),
            "legacy_gpu0",
        )
        self.assertEqual(
            worker.resolve_failed_repair_runtime_lane(
                group="transport", requested_lane="auto", gpu=2
            ),
            "modern_vulkan",
        )
        with self.assertRaisesRegex(ValueError, "incompatible"):
            worker.resolve_failed_repair_runtime_lane(
                group="transport", requested_lane="legacy_gpu0", gpu=2
            )
        with self.assertRaisesRegex(ValueError, "requires --failed-repair-group"):
            worker.resolve_failed_repair_runtime_lane(
                group="", requested_lane="modern_vulkan", gpu=2
            )

    def test_generic_batch0_failed_repair_requires_automatic_frozen_policy(
        self,
    ) -> None:
        worker.validate_failed_repair_execution(
            group="runtime_api_compat",
            batch="batch0000_failed_repair_runtime_api_compat",
            batch_index=0,
            batch_size=1000,
            remote_port=worker.SECONDARY_REMOTE_PORT,
            gpu=3,
            worker_index=3,
            worker_count=11,
            only_status=("failed",),
            force=True,
            resume_unprocessed_only=False,
            runtime_aware_partition=False,
            excluded_partitions=(),
            deferred_strategy="",
            repair_slot_index=None,
            repair_slot_count=None,
            runtime_lane="modern_vulkan",
        )
        row = {
            "asset_id": "000001",
            "identity_key": "identity-000001",
            "render_batch": "0000",
            "repair_group": "runtime_api_compat",
            "expected_status": "failed",
            "expected_status_signature": "a" * 64,
            "automatic_execution_allowed": "true",
            "requires_human_review": "false",
            "max_attempts": "1",
            "manifest_generation": "frozen-generation",
        }
        worker.validate_failed_repair_manifest_rows(
            [row],
            "runtime_api_compat",
            "batch0000_failed_repair_runtime_api_compat",
        )
        self.assertEqual(
            worker.failed_repair_batch_identity(
                "batch0002_failed_repair_runtime_api_compat",
                "runtime_api_compat",
            ),
            ("batch0002", 2),
        )
        for changes in (
            {"automatic_execution_allowed": "false"},
            {"requires_human_review": "true"},
            {"max_attempts": "0"},
            {"manifest_generation": ""},
            {"render_batch": "0001"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_failed_repair_manifest_rows(
                    [{**row, **changes}],
                    "runtime_api_compat",
                    "batch0000_failed_repair_runtime_api_compat",
                )
        self.assertIsNone(
            worker.failed_repair_batch_identity(
                "batch0000_failed_repair_timeout_scene_audit",
                "timeout_scene_audit",
            )
        )

    def test_failed_repair_row_state_rechecks_snapshot_and_never_blind_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "asset.blend"
            model.write_bytes(b"BLENDER-v360" + b"x" * 32)
            status: dict[str, object] = {
                "asset_id": "000581",
                "identity_key": "identity-000581",
                "render_order": "1581",
                "render_batch": "0001",
                "batch": "batch0001",
                "status": "failed",
                "updated_at": "2026-07-18 10:00:00",
            }
            signature, size, mtime_ns = worker.failed_repair_source_signature(model)
            row = {
                "asset_id": "000581",
                "identity_key": "identity-000581",
                "render_order": "1581",
                "render_batch": "0001",
                "model_file": str(model),
                "repair_group": "course_timeout_exact",
                "expected_status": "failed",
                "expected_status_signature": worker.effective_status_signature(status),
                "expected_updated_at": "2026-07-18 10:00:00",
                "source_stat_signature": signature,
                "source_size": str(size),
                "source_mtime_ns": str(mtime_ns),
            }
            self.assertEqual(
                worker.failed_repair_row_state(
                    row, status, "course_timeout_exact"
                ),
                "pending",
            )
            landed = {**status, "status": "needs_review", "updated_at": "later"}
            self.assertEqual(
                worker.failed_repair_row_state(
                    row, landed, "course_timeout_exact"
                ),
                "landed",
            )
            attempted = {
                **status,
                "batch": "batch0001_failed_repair_course_timeout_exact",
                "error": "new failure evidence",
                "updated_at": "later",
            }
            self.assertEqual(
                worker.failed_repair_row_state(
                    row, attempted, "course_timeout_exact"
                ),
                "attempted_failed",
            )
            with self.assertRaisesRegex(ValueError, "unsafe"):
                worker.failed_repair_row_state(
                    row,
                    {**attempted, "batch": "batch0001_unreviewed_repair"},
                    "course_timeout_exact",
                )
            model.write_bytes(model.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "source stat differs"):
                worker.failed_repair_row_state(
                    row, status, "course_timeout_exact"
                )

    def test_vulkan_probe_missing_is_distinct_from_backend_failure(self) -> None:
        remote = worker.Remote(30773, 1)
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                "0000:27:00.0, GPU-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d, 0x2B8510DE\n",
                "",
            ],
        ):
            remote.configure_gpu()
        self.assertFalse(remote.vulkan_available)
        self.assertEqual(remote.vulkan_probe_state, "probe_missing")
        self.assertEqual(remote.vulkan_error_code, "vulkaninfo_missing")

    def test_worker_can_skip_non_authoritative_vulkaninfo_diagnostic(self) -> None:
        remote = worker.Remote(30773, 1)
        with mock.patch.object(
            remote,
            "run",
            return_value=(
                "0000:27:00.0, GPU-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d, 0x2B8510DE\n"
            ),
        ) as run:
            remote.configure_gpu(run_vulkaninfo_probe=False)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(remote.vulkan_available)
        self.assertEqual(
            remote.vulkan_probe_state, "diagnostic_skipped_unattested"
        )
        self.assertEqual(
            remote.vulkan_error_code,
            "blender_pid_uuid_attestation_required",
        )

    def test_vulkan_uuid_probe_retries_transient_device_failure(self) -> None:
        remote = worker.Remote(31722, 3)
        expected = "287f358a-b090-c1fc-89e3-61d3be51d37c"
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                f"0000:A8:00.0, GPU-{expected}, 0x2B8510DE\n",
                "/usr/bin/vulkaninfo\n",
                "GPU0:\n  deviceUUID = 11111111-1111-1111-1111-111111111111\n",
                f"GPU0:\n  deviceUUID = {expected}\n",
            ],
        ), mock.patch.object(worker.time, "sleep") as sleep:
            remote.configure_gpu(retry_attempts=2, retry_delay_seconds=2)
        self.assertFalse(remote.vulkan_available)
        self.assertEqual(
            remote.vulkan_probe_state, "vulkaninfo_uuid_unattested"
        )
        self.assertEqual(
            remote.vulkan_error_code,
            "blender_pid_uuid_attestation_required",
        )
        self.assertEqual(remote.vulkan_probe_attempts, 2)
        sleep.assert_called_once_with(2)

    def test_vulkan_probe_does_not_swallow_transport_interruption(self) -> None:
        remote = worker.Remote(31722, 2)
        interruption = worker.RemoteTransportError("worker_ssh_timeout")
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                "0000:27:00.0, GPU-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d, 0x2B8510DE\n",
                "/usr/bin/vulkaninfo\n",
                interruption,
            ],
        ) as run, self.assertRaises(worker.RemoteTransportError) as caught:
            remote.configure_gpu()
        self.assertIs(caught.exception, interruption)
        self.assertEqual(run.call_count, 3)

    def test_vulkan_env_exposes_only_the_selected_pci_device(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        remote.vulkan_preferred_prefix = "10de/2b85"
        remote._nvidia_graphics_runtime_validated = True
        remote._vulkan_profile_paths["5.1"] = "/tmp/profile"
        command = remote.vulkan_env(family="5.1")
        self.assertIn("DRI_PRIME=pci-0000_a8_00_0", command)
        self.assertNotIn("pci-0000_a8_00_0!", command)
        self.assertIn("VK_DRIVER_FILES=", command)
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
        self.assertIn("export CUDA_DEVICE_ORDER=PCI_BUS_ID;", command)
        self.assertIn(
            "export CUDA_VISIBLE_DEVICES=GPU-287f358a-b090-c1fc-89e3-61d3be51d37c;",
            command,
        )
        self.assertNotIn("CUDA_VISIBLE_DEVICES=3", command)

    def test_missing_or_manifest_only_vulkan_profile_fails_without_dri_fallback(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        remote.vulkan_preferred_prefix = "10de/2b85"
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                worker.RUNTIME_READY_MARKER + " driver=570.211.01\n",
                worker.PROFILE_ARTIFACT_INVALID_MARKER + " code=current_missing\n",
            ],
        ) as run:
            with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                remote.ensure_vulkan_runtime(
                    "5.1", worker.REMOTE_BLENDERS["5.1"]
                )
        self.assertEqual(caught.exception.code, "vulkan_profile_invalid")
        self.assertEqual(run.call_count, 2)
        profile_check = run.call_args_list[-1].args[0]
        self.assertIn("vulkan-profiles/v1", profile_check)
        self.assertIn("userpref.blend", profile_check)
        self.assertNotIn("DRI_PRIME", profile_check)

    def test_blender_smoke_requires_pid_uuid_attestation_not_a_marker_only(self) -> None:
        remote = worker.Remote(31722, 3)
        expected = "287f358a-b090-c1fc-89e3-61d3be51d37c"
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = f"gpu-{expected}"
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                worker.RUNTIME_READY_MARKER + " driver=570.211.01\n",
                valid_vulkan_manifest(remote),
                "TOTAL_ASSET_VULKAN_BLENDER_FALLBACK 5.1.1\n",
            ],
        ) as run:
            with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                remote.ensure_vulkan_runtime("5.1", worker.REMOTE_BLENDERS["5.1"])
        self.assertEqual(
            caught.exception.code, "vulkan_blender_pid_uuid_attestation_failed"
        )
        self.assertFalse(remote.vulkan_available)
        smoke_command = run.call_args_list[-1].args[0]
        self.assertIn("total_asset_gpu_3.lock", smoke_command)
        self.assertIn("--gpu-backend vulkan -b", smoke_command)
        self.assertIn("BLENDER_USER_CONFIG=", smoke_command)
        self.assertIn("--query-compute-apps=gpu_uuid,pid", smoke_command)

    def test_blender_smoke_accepts_only_matching_pid_uuid(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        remote.vulkan_preferred_prefix = "10de/2b85"
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                worker.RUNTIME_READY_MARKER + " driver=570.211.01\n",
                valid_vulkan_manifest(remote),
                valid_vulkan_attestation(remote),
            ],
        ):
            remote.ensure_vulkan_runtime("5.1", worker.REMOTE_BLENDERS["5.1"])
        self.assertTrue(remote.vulkan_available)
        self.assertEqual(
            remote.vulkan_probe_state, "blender_pid_uuid_attested"
        )

    def test_vulkan_attestation_lock_busy_remains_dedicated_runtime_state(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        busy = worker.RemoteCommandError(
            75, stderr="TOTAL_ASSET_GPU_BUSY gpu=3"
        )
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                worker.RUNTIME_READY_MARKER + " driver=570.211.01\n",
                valid_vulkan_manifest(remote),
                busy,
            ],
        ):
            with self.assertRaises(GpuLockBusy):
                remote.ensure_vulkan_runtime(
                    "5.1", worker.REMOTE_BLENDERS["5.1"]
                )
        self.assertNotIn("5.1", remote._vulkan_failures)

    def test_vulkan_profile_is_identity_checked_and_uses_pinned_pci_filter(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        remote.vulkan_preferred_prefix = "10de/2b85"
        manifest = {
            "schema": worker.VULKAN_PROFILE_SCHEMA,
            "policy": worker.VULKAN_PROFILE_POLICY,
            "family": "5.1",
            "gpu_index": 3,
            "gpu_uuid": remote.gpu_uuid,
            "gpu_pci_selector": remote.pci_selector,
            "blender_binary": worker.REMOTE_BLENDERS["5.1"],
            "preference_writer": worker.VULKAN_PROFILE_PREFERENCE_WRITER,
            "graphics_driver_version": worker.RUNTIME_DRIVER_VERSION,
            "graphics_runtime_source_sha256": worker.RUNTIME_SOURCE_SHA256,
            "graphics_runtime_manifest_sha256": worker.RUNTIME_MANIFEST_DIGEST,
            "graphics_environment_policy": worker.RUNTIME_ENVIRONMENT_POLICY,
            "graphics_environment_fingerprint": worker.runtime_environment_fingerprint(
                worker.REMOTE_GL_ROOT,
            ),
            "device_select_environment_policy": (
                worker.DEVICE_SELECT_ENVIRONMENT_POLICY
            ),
            "device_select_environment_fingerprint": (
                worker.device_select_environment_fingerprint()
            ),
            "gpu_pid_attestation_policy": worker.GPU_PID_ATTESTATION_POLICY,
            "gpu0_aux_graphics_max_fb_mib": (
                worker.GPU0_AUX_GRAPHICS_MAX_FB_MIB
            ),
            "gpu_pid_attestation_stable_samples": (
                worker.GPU_PID_ATTESTATION_STABLE_SAMPLES
            ),
            "profile_userpref_sha256": "a" * 64,
            "blender_binary_sha256": "b" * 64,
            "preferred_device": "10de/2b85/0",
        }
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                worker.RUNTIME_READY_MARKER + " driver=570.211.01\n",
                json.dumps(manifest),
                valid_vulkan_attestation(remote),
            ],
        ) as run:
            remote.ensure_vulkan_runtime("5.1", worker.REMOTE_BLENDERS["5.1"])
        profile_check = run.call_args_list[1].args[0]
        self.assertIn("vulkan-profiles/v1", profile_check)
        self.assertIn("userpref.blend", profile_check)
        smoke_command = run.call_args_list[-1].args[0]
        expected_current = worker.vulkan_profile_current_path(
            "5.1", remote.gpu_uuid
        )
        self.assertIn(
            f"export BLENDER_USER_CONFIG={expected_current}/config;",
            smoke_command,
        )
        self.assertIn("unset DRI_PRIME NODEVICE_SELECT", smoke_command)
        self.assertIn("DRI_PRIME=pci-0000_a8_00_0", smoke_command)
        self.assertIn(
            "XDG_DATA_HOME=/root/.local/share/video2blender/"
            "vulkan-device-select-mesa-23.2.1/xdg",
            smoke_command,
        )
        self.assertIn(
            "XDG_DATA_DIRS=/root/.local/share/video2blender/"
            "vulkan-device-select-mesa-23.2.1/xdg",
            smoke_command,
        )
        self.assertNotIn(
            "export VK_INSTANCE_LAYERS=VK_LAYER_MESA_device_select",
            smoke_command,
        )
        self.assertNotIn("pci-0000_a8_00_0!", smoke_command)

    def test_vulkan_profile_candidate_index_is_not_physical_gpu_index(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        remote.vulkan_preferred_prefix = "10de/2b85"
        manifest = {
            "schema": worker.VULKAN_PROFILE_SCHEMA,
            "policy": worker.VULKAN_PROFILE_POLICY,
            "family": "5.1",
            "gpu_index": 3,
            "gpu_uuid": remote.gpu_uuid,
            "gpu_pci_selector": remote.pci_selector,
            "blender_binary": worker.REMOTE_BLENDERS["5.1"],
            "preference_writer": worker.VULKAN_PROFILE_PREFERENCE_WRITER,
            "graphics_driver_version": worker.RUNTIME_DRIVER_VERSION,
            "graphics_runtime_source_sha256": worker.RUNTIME_SOURCE_SHA256,
            "graphics_runtime_manifest_sha256": worker.RUNTIME_MANIFEST_DIGEST,
            "graphics_environment_policy": worker.RUNTIME_ENVIRONMENT_POLICY,
            "graphics_environment_fingerprint": worker.runtime_environment_fingerprint(
                worker.REMOTE_GL_ROOT,
            ),
            "device_select_environment_policy": (
                worker.DEVICE_SELECT_ENVIRONMENT_POLICY
            ),
            "device_select_environment_fingerprint": (
                worker.device_select_environment_fingerprint()
            ),
            "gpu_pid_attestation_policy": worker.GPU_PID_ATTESTATION_POLICY,
            "gpu0_aux_graphics_max_fb_mib": (
                worker.GPU0_AUX_GRAPHICS_MAX_FB_MIB
            ),
            "gpu_pid_attestation_stable_samples": (
                worker.GPU_PID_ATTESTATION_STABLE_SAMPLES
            ),
            "profile_userpref_sha256": "a" * 64,
            "blender_binary_sha256": "b" * 64,
            # Blender's Vulkan enumeration is independent of nvidia-smi's
            # physical index; the subsequent PID UUID smoke is authoritative.
            "preferred_device": "10de/2b85/0",
        }
        with mock.patch.object(
            remote,
            "run",
            side_effect=[
                worker.RUNTIME_READY_MARKER + " driver=570.211.01\n",
                json.dumps(manifest),
                valid_vulkan_attestation(remote),
            ],
        ):
            remote.ensure_vulkan_runtime("5.1", worker.REMOTE_BLENDERS["5.1"])
        self.assertTrue(remote.vulkan_available)

    def test_vulkan_profile_requires_a_saved_user_preference(self) -> None:
        remote = worker.Remote(31722, 3)
        remote.pci_selector = "pci-0000_a8_00_0"
        remote.gpu_uuid = "gpu-287f358a-b090-c1fc-89e3-61d3be51d37c"
        with mock.patch.object(
            remote,
            "run",
            return_value=(
                worker.PROFILE_ARTIFACT_INVALID_MARKER + " code=current_missing\n"
            ),
        ) as run:
            with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                remote._load_vulkan_profile(
                    "5.1", worker.REMOTE_BLENDERS["5.1"]
                )
        self.assertEqual(caught.exception.code, "vulkan_profile_invalid")
        profile_probe = run.call_args.args[0]
        self.assertIn('name == "userpref.blend"', profile_probe)
        self.assertIn("profile.json", profile_probe)

    def test_repair_claim_occurs_only_after_runtime_capability_preflight(self) -> None:
        source = inspect.getsource(worker.main)
        repair_branch = source[source.index("if repair_closure_enabled:", source.index("def process_row")):]
        self.assertLess(
            repair_branch.index("ensure_row_runtime_capability("),
            repair_branch.index("claim_repair_closure_asset("),
        )

    def test_dynamic_assignment_requires_one_exact_resume_only_lease(self) -> None:
        worker.validate_dynamic_assignment_invocation(
            assignment_mode=worker.DYNAMIC_ASSIGNMENT_MODE,
            batch="batch0003",
            batch_index=3,
            worker_count=11,
            asset_ids=("003001",),
            cycle_id="cycle72-test",
            lease_id="a" * 32,
            lease_db=Path("/tmp/cycle72.sqlite3"),
            node_boot_id="boot-id",
            knowledge_generation="reviewed-v10",
            resume_unprocessed_only=True,
            force=False,
            runtime_aware_partition=False,
            excluded_partitions=(),
            repair_strategy="",
            failed_repair_group="",
            only_status=(),
            only_engine_hint=(),
            defer_holder_to_shared_lock=True,
        )

    def test_highqal_invocation_binds_asset_to_composite_work_item_identity(self) -> None:
        common = {
            "workload_kind": worker.HIGHQAL_SOURCE_WORKLOAD_KIND,
            "workload_generation": "a" * 64,
            "status_path": Path("/tmp/highqal-status.jsonl"),
            "final_root": Path("/tmp/highqal-final"),
            "evidence_root": Path("/tmp/highqal-evidence"),
        }
        worker.validate_workload_invocation(
            **common,
            asset_ids=("bili_BV12F5m6UE7C_ee05948820",),
            work_item_id=(
                "bili_BV12F5m6UE7C_ee05948820--2d67b5ccefbf8ac2"
            ),
        )
        invalid = (
            (("bili_BV12F5m6UE7C_ee05948820",), "bili_BV12F5m6UE7C_ee05948820"),
            (("different_asset",), "bili_BV12F5m6UE7C_ee05948820--2d67b5ccefbf8ac2"),
            (("bili_BV12F5m6UE7C_ee05948820",), "bili_BV12F5m6UE7C_ee05948820--not-a-hash"),
            (
                ("bili_BV12F5m6UE7C_ee05948820", "second_asset"),
                "bili_BV12F5m6UE7C_ee05948820--2d67b5ccefbf8ac2",
            ),
        )
        for asset_ids, work_item_id in invalid:
            with self.subTest(asset_ids=asset_ids, work_item_id=work_item_id):
                with self.assertRaisesRegex(
                    ValueError, "highqal workload identity is incomplete"
                ):
                    worker.validate_workload_invocation(
                        **common,
                        asset_ids=asset_ids,
                        work_item_id=work_item_id,
                    )

    def test_holder_defer_flag_is_dynamic_only(self) -> None:
        with self.assertRaisesRegex(ValueError, "lease metadata"):
            worker.validate_dynamic_assignment_invocation(
                assignment_mode=worker.FIXED_ASSIGNMENT_MODE,
                batch="batch0003",
                batch_index=3,
                worker_count=11,
                asset_ids=("003001",),
                cycle_id="",
                lease_id="",
                lease_db=None,
                node_boot_id="",
                knowledge_generation="",
                resume_unprocessed_only=True,
                force=False,
                runtime_aware_partition=False,
                excluded_partitions=(),
                repair_strategy="",
                failed_repair_group="",
                only_status=(),
                only_engine_hint=(),
                defer_holder_to_shared_lock=True,
            )

    def test_exact_holder_defer_requires_all_remote_holder_evidence(self) -> None:
        port = worker.SECONDARY_REMOTE_PORT
        gpu_uuid = "GPU-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d"
        observation = GpuObservation(
            gpu=0,
            gpu_query_ok=True,
            compute_query_ok=True,
            compute_process_count=1,
            compute_process_kinds=("holder",),
            holder_session=True,
            lock_state="busy",
            wrapper_process_present=False,
            gpu_uuid=gpu_uuid,
            physical_binding_ok=True,
            compute_process_gpu_uuids=(gpu_uuid,),
            holder_descendant_ok=True,
            holder_lock_owner_ok=True,
            holder_command_ok=True,
        )
        node = NodeObservation(
            port=port,
            probe_ok=True,
            holder_audit_ok=True,
            unexpected_holder_sessions=0,
            legacy_holder_sessions=0,
            gpus=(observation,),
        )
        probe = mock.Mock(return_value=node)
        self.assertTrue(worker.exact_dynamic_holder_can_defer_to_shared_lock(
            remote_port=port,
            gpu=0,
            host="root@example.invalid",
            identity_file=Path("/tmp/id"),
            known_hosts=Path("/tmp/known_hosts"),
            probe=probe,
        ))
        self.assertEqual(probe.call_args.kwargs["audited_node_gpus"], (0, 1, 2, 3))
        self.assertEqual(
            probe.call_args.args[1], worker.REQUIRED_REMOTE_GPU_LAYOUT[port]
        )

        for unsafe in (
            GpuObservation(**{**observation.__dict__, "wrapper_process_present": True}),
            GpuObservation(**{**observation.__dict__, "holder_lock_owner_ok": False}),
            GpuObservation(**{**observation.__dict__, "compute_process_count": 2}),
            GpuObservation(**{**observation.__dict__, "lock_state": "available"}),
        ):
            self.assertFalse(worker.exact_dynamic_holder_can_defer_to_shared_lock(
                remote_port=port,
                gpu=0,
                host="root@example.invalid",
                identity_file=Path("/tmp/id"),
                known_hosts=Path("/tmp/known_hosts"),
                probe=mock.Mock(return_value=NodeObservation(
                    port=port,
                    probe_ok=True,
                    holder_audit_ok=True,
                    unexpected_holder_sessions=0,
                    legacy_holder_sessions=0,
                    gpus=(unsafe,),
                )),
            ))

    def test_tertiary_exact_holder_defer_audits_reserve_without_dispatching_it(self) -> None:
        port = worker.TERTIARY_REMOTE_PORT
        dispatch = worker.REQUIRED_REMOTE_GPU_LAYOUT[port]
        audit = worker.HOLDER_AUDIT_GPU_LAYOUT[port]
        self.assertEqual(dispatch, (0, 1, 2))
        self.assertEqual(audit, (0, 1, 2, 3))
        self.assertNotIn((port, 3), worker.CANONICAL_WORKER_INDEX_BY_LOCATION)

        gpu_uuid = "GPU-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d"
        observation = GpuObservation(
            gpu=0,
            gpu_query_ok=True,
            compute_query_ok=True,
            compute_process_count=1,
            compute_process_kinds=("holder",),
            holder_session=True,
            lock_state="busy",
            wrapper_process_present=False,
            gpu_uuid=gpu_uuid,
            physical_binding_ok=True,
            compute_process_gpu_uuids=(gpu_uuid,),
            holder_descendant_ok=True,
            holder_lock_owner_ok=True,
            holder_command_ok=True,
        )
        probe = mock.Mock(return_value=NodeObservation(
            port=port,
            probe_ok=True,
            holder_audit_ok=True,
            unexpected_holder_sessions=0,
            legacy_holder_sessions=0,
            gpus=(observation,),
        ))
        self.assertTrue(worker.exact_dynamic_holder_can_defer_to_shared_lock(
            remote_port=port,
            gpu=0,
            host="root@example.invalid",
            identity_file=Path("/tmp/id"),
            known_hosts=Path("/tmp/known_hosts"),
            probe=probe,
        ))
        self.assertEqual(probe.call_args.kwargs["audited_node_gpus"], audit)
        self.assertEqual(probe.call_args.args[1], dispatch)

        for node_field in ("unexpected_holder_sessions", "legacy_holder_sessions"):
            unsafe_node = NodeObservation(**{
                **probe.return_value.__dict__,
                node_field: 1,
            })
            self.assertFalse(worker.exact_dynamic_holder_can_defer_to_shared_lock(
                remote_port=port,
                gpu=0,
                host="root@example.invalid",
                identity_file=Path("/tmp/id"),
                known_hosts=Path("/tmp/known_hosts"),
                probe=mock.Mock(return_value=unsafe_node),
            ))

    def test_dynamic_assignment_uses_lease_authority_without_jsonl_rescan(self) -> None:
        with mock.patch.object(worker, "all_statuses") as load:
            self.assertEqual(
                worker.initial_completed_statuses(
                    assignment_mode=worker.DYNAMIC_ASSIGNMENT_MODE
                ),
                {},
            )
        load.assert_not_called()

        expected = {"000001": {"status": "accepted"}}
        with mock.patch.object(worker, "all_statuses", return_value=expected) as load:
            self.assertIs(
                worker.initial_completed_statuses(
                    assignment_mode=worker.FIXED_ASSIGNMENT_MODE
                ),
                expected,
            )
        load.assert_called_once_with()

    def test_holder_reprobe_precedes_dynamic_busy_wait(self) -> None:
        source = inspect.getsource(worker.main)
        busy = source.index("busy_processes = remote.gpu_compute_processes()")
        reprobe = source.index("retry_exact_dynamic_holder_defer(", busy)
        wait_loop = source.index("while (", reprobe)
        self.assertLess(busy, reprobe)
        self.assertLess(reprobe, wait_loop)

    @staticmethod
    def _holder_busy_processes() -> list[dict[str, str]]:
        return [{
            "gpu_uuid": "gpu-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d",
            "pid": "4242",
            "name": "/usr/bin/python3",
        }]

    def test_dynamic_holder_reattest_recovers_after_transient_probe_failure(
        self,
    ) -> None:
        busy = self._holder_busy_processes()
        remote = mock.Mock()
        remote.gpu_compute_processes.return_value = busy
        holder_probe = mock.Mock(side_effect=(False, True))
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds

        result = worker.retry_exact_dynamic_holder_defer(
            remote=remote,
            initial_busy_processes=busy,
            remote_port=worker.SECONDARY_REMOTE_PORT,
            gpu=0,
            host="root@example.invalid",
            identity_file=Path("/tmp/id"),
            known_hosts=Path("/tmp/known_hosts"),
            drain_file=None,
            timeout_seconds=10,
            interval_seconds=5,
            holder_probe=holder_probe,
            monotonic=lambda: current[0],
            sleeper=sleep,
        )

        self.assertTrue(result.exact_holder_deferred)
        self.assertFalse(result.drained)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(remote.gpu_compute_processes.call_count, 1)
        self.assertEqual(current[0], 5)

    def test_dynamic_holder_reattest_continues_when_gpu_becomes_empty(self) -> None:
        busy = self._holder_busy_processes()
        remote = mock.Mock()
        remote.gpu_compute_processes.return_value = []
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds

        result = worker.retry_exact_dynamic_holder_defer(
            remote=remote,
            initial_busy_processes=busy,
            remote_port=worker.SECONDARY_REMOTE_PORT,
            gpu=0,
            host="root@example.invalid",
            identity_file=Path("/tmp/id"),
            known_hosts=Path("/tmp/known_hosts"),
            drain_file=None,
            timeout_seconds=10,
            interval_seconds=5,
            holder_probe=mock.Mock(return_value=False),
            monotonic=lambda: current[0],
            sleeper=sleep,
        )

        self.assertEqual(result.busy_processes, ())
        self.assertFalse(result.exact_holder_deferred)
        self.assertFalse(result.drained)
        self.assertEqual(result.attempts, 1)
        remote.gpu_compute_processes.assert_called_once_with()

    def test_dynamic_holder_reattest_timeout_remains_fail_closed(self) -> None:
        busy = self._holder_busy_processes()
        remote = mock.Mock()
        remote.gpu_compute_processes.return_value = busy
        holder_probe = mock.Mock(return_value=False)
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds

        result = worker.retry_exact_dynamic_holder_defer(
            remote=remote,
            initial_busy_processes=busy,
            remote_port=worker.SECONDARY_REMOTE_PORT,
            gpu=0,
            host="root@example.invalid",
            identity_file=Path("/tmp/id"),
            known_hosts=Path("/tmp/known_hosts"),
            drain_file=None,
            timeout_seconds=10,
            interval_seconds=5,
            holder_probe=holder_probe,
            monotonic=lambda: current[0],
            sleeper=sleep,
        )

        self.assertEqual(result.busy_processes, tuple(busy))
        self.assertFalse(result.exact_holder_deferred)
        self.assertFalse(result.drained)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(holder_probe.call_count, 3)
        self.assertEqual(remote.gpu_compute_processes.call_count, 2)
        self.assertEqual(current[0], 10)

    def test_dynamic_holder_reattest_honors_drain_during_retry(self) -> None:
        busy = self._holder_busy_processes()
        remote = mock.Mock()
        drain_state = [False]
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            drain_state[0] = True

        result = worker.retry_exact_dynamic_holder_defer(
            remote=remote,
            initial_busy_processes=busy,
            remote_port=worker.SECONDARY_REMOTE_PORT,
            gpu=0,
            host="root@example.invalid",
            identity_file=Path("/tmp/id"),
            known_hosts=Path("/tmp/known_hosts"),
            drain_file=Path("/tmp/drain"),
            timeout_seconds=10,
            interval_seconds=5,
            holder_probe=mock.Mock(return_value=False),
            monotonic=lambda: current[0],
            sleeper=sleep,
            drain_probe=lambda _path: drain_state[0],
        )

        self.assertTrue(result.drained)
        self.assertFalse(result.exact_holder_deferred)
        self.assertEqual(result.attempts, 1)
        remote.gpu_compute_processes.assert_not_called()

    def test_dynamic_holder_reattest_does_not_wait_for_blender(self) -> None:
        busy = [{
            "gpu_uuid": "gpu-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d",
            "pid": "4242",
            "name": "blender",
        }]
        remote = mock.Mock()
        holder_probe = mock.Mock(return_value=False)
        sleeper = mock.Mock()
        result = worker.retry_exact_dynamic_holder_defer(
            remote=remote,
            initial_busy_processes=busy,
            remote_port=worker.SECONDARY_REMOTE_PORT,
            gpu=0,
            host="root@example.invalid",
            identity_file=Path("/tmp/id"),
            known_hosts=Path("/tmp/known_hosts"),
            drain_file=None,
            holder_probe=holder_probe,
            sleeper=sleeper,
        )
        self.assertFalse(result.exact_holder_deferred)
        self.assertEqual(result.attempts, 1)
        sleeper.assert_not_called()
        remote.gpu_compute_processes.assert_not_called()

    def test_dynamic_assignment_rejects_old_partition_and_repair_overrides(self) -> None:
        with self.assertRaisesRegex(ValueError, "forbids repair/filter/partition"):
            worker.validate_dynamic_assignment_invocation(
                assignment_mode=worker.DYNAMIC_ASSIGNMENT_MODE,
                batch="batch0003",
                batch_index=3,
                worker_count=11,
                asset_ids=("003001",),
                cycle_id="cycle72-test",
                lease_id="a" * 32,
                lease_db=Path("/tmp/cycle72.sqlite3"),
                node_boot_id="boot-id",
                knowledge_generation="reviewed-v10",
                resume_unprocessed_only=True,
                force=False,
                runtime_aware_partition=True,
                excluded_partitions=(),
                repair_strategy="",
                failed_repair_group="",
                only_status=(),
                only_engine_hint=(),
            )

    def test_output_identity_collision_uses_stable_suffixed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_dir = root / "static" / "000001_Tree"
            old_dir.mkdir(parents=True)
            (old_dir / "render_review.json").write_text(json.dumps({
                "asset_id": "000001",
                "identity_key": "old-identity",
                "render_batch": "0000",
            }), encoding="utf-8")
            row = {
                "asset_id": "000001",
                "identity_key": "new-identity",
                "render_batch": "0001",
            }
            with mock.patch.object(worker, "RENDER_ROOT", root):
                name, collisions = worker.collision_safe_output_name(
                    "000001_Tree", row, Path("/current/tree.blend")
                )
            self.assertRegex(name, r"^000001_Tree_[0-9a-f]{10}$")
            self.assertEqual(collisions, (str(old_dir),))
            self.assertTrue(old_dir.exists())

    def test_runtime_aware_partition_is_disjoint_and_routes_legacy_to_gpu0(self) -> None:
        rows = [
            {
                "asset_id": f"{index:06d}",
                "model_file": f"{'legacy' if index % 4 == 0 else 'modern'}-{index}.blend",
            }
            for index in range(77)
        ]
        assignments: dict[str, int] = {}
        with mock.patch.object(
            worker,
            "row_requires_source_family_gpu0",
            side_effect=lambda row: str(row["model_file"]).startswith("legacy"),
        ):
            for worker_index in range(11):
                selected, excluded = worker.select_runtime_aware_worker_rows(
                    rows,
                    worker_index=worker_index,
                    worker_count=11,
                )
                self.assertEqual(excluded, 0)
                for row in selected:
                    self.assertNotIn(row["asset_id"], assignments)
                    assignments[row["asset_id"]] = worker_index
        self.assertEqual(set(assignments), {row["asset_id"] for row in rows})
        for row in rows:
            if str(row["model_file"]).startswith("legacy"):
                self.assertIn(
                    assignments[row["asset_id"]],
                    worker.CANONICAL_GPU0_WORKER_INDICES,
                )

    def test_formal_worker_reuses_strict_scheduler_partition_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            catalog = inventory / "total_asset_catalog.csv"
            inventory.mkdir()
            catalog.write_text("catalog-generation-one\n", encoding="utf-8")
            rows = [
                {
                    "asset_id": f"asset-{index:02d}",
                    "render_order": str(index + 1),
                    "render_batch": "0002",
                    "model_file": str(root / f"asset-{index:02d}.blend"),
                    "render_engine_hint": "source",
                }
                for index in range(22)
            ]

            with mock.patch.object(
                worker,
                "row_requires_source_family_gpu0",
                return_value=False,
            ) as header_route, mock.patch.object(
                worker,
                "partition_runtime_aware_worker_rows",
                wraps=worker.partition_runtime_aware_worker_rows,
            ) as build:
                first, _ = worker.select_cached_runtime_aware_formal_worker_rows(
                    rows,
                    catalog_path=catalog,
                    inventory=inventory,
                    batch="batch0002",
                    worker_index=5,
                    worker_count=11,
                )
                first_header_calls = header_route.call_count
                second, _ = worker.select_cached_runtime_aware_formal_worker_rows(
                    rows,
                    catalog_path=catalog,
                    inventory=inventory,
                    batch="batch0002",
                    worker_index=5,
                    worker_count=11,
                )
            self.assertEqual(build.call_count, 1)
            self.assertEqual(header_route.call_count, first_header_calls)
            self.assertEqual(
                [row["asset_id"] for row in first],
                [row["asset_id"] for row in second],
            )

            cache = (
                inventory / "worker_control" /
                "batch0002.wc11.runtime_partition_cache.json"
            )
            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["schema"],
                "video2blender.runtime-partition-cache.v2",
            )
            self.assertEqual(
                payload["routing_policy"],
                "blender-native-vulkan-profile-v2",
            )
            payload["partitions"]["5"].pop()
            cache.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(
                worker,
                "row_requires_source_family_gpu0",
                return_value=False,
            ), mock.patch.object(
                worker,
                "partition_runtime_aware_worker_rows",
                wraps=worker.partition_runtime_aware_worker_rows,
            ) as rebuild:
                worker.select_cached_runtime_aware_formal_worker_rows(
                    rows,
                    catalog_path=catalog,
                    inventory=inventory,
                    batch="batch0002",
                    worker_index=5,
                    worker_count=11,
                )
            self.assertEqual(rebuild.call_count, 1)
            repaired = json.loads(cache.read_text(encoding="utf-8"))
            repaired_ids = [
                asset_id
                for index in range(11)
                for asset_id in repaired["partitions"][str(index)]
            ]
            self.assertEqual(set(repaired_ids), {row["asset_id"] for row in rows})
            self.assertEqual(len(repaired_ids), len(set(repaired_ids)))

    def test_v1_runtime_partition_cache_is_rebuilt_under_current_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            control = inventory / "worker_control"
            control.mkdir(parents=True)
            catalog = inventory / "total_asset_catalog.csv"
            catalog.write_text("catalog\n", encoding="utf-8")
            rows = [{
                "asset_id": "asset-a",
                "render_order": "2001",
                "render_batch": "0002",
                "model_file": str(root / "asset-a.blend"),
                "render_engine_hint": "source",
            }]
            stale = control / "batch0002.wc11.runtime_partition_cache.json"
            stale.write_text(json.dumps({
                "schema": "video2blender.runtime-partition-cache.v1",
                "batch": "batch0002",
                "worker_count": 11,
                "catalog_generation": "stale",
                "partitions": {
                    str(index): (["asset-a"] if index == 10 else [])
                    for index in range(11)
                },
            }), encoding="utf-8")
            with mock.patch.object(
                worker, "row_requires_source_family_gpu0", return_value=False
            ):
                worker.select_cached_runtime_aware_formal_worker_rows(
                    rows,
                    catalog_path=catalog,
                    inventory=inventory,
                    batch="batch0002",
                    worker_index=0,
                    worker_count=11,
                )
            rebuilt = json.loads(stale.read_text(encoding="utf-8"))
            self.assertEqual(
                rebuilt["schema"],
                "video2blender.runtime-partition-cache.v2",
            )
            self.assertEqual(
                rebuilt["routing_policy"],
                "blender-native-vulkan-profile-v2",
            )

    def test_formal_partition_cache_generation_mismatch_rebuilds_and_failure_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            catalog = inventory / "total_asset_catalog.csv"
            inventory.mkdir()
            catalog.write_text("generation-one\n", encoding="utf-8")
            rows = [{
                "asset_id": "asset-a",
                "render_order": "2001",
                "render_batch": "0002",
                "model_file": str(root / "asset-a.blend"),
                "render_engine_hint": "source",
            }]
            with mock.patch.object(
                worker, "row_requires_source_family_gpu0", return_value=False
            ):
                worker.select_cached_runtime_aware_formal_worker_rows(
                    rows,
                    catalog_path=catalog,
                    inventory=inventory,
                    batch="batch0002",
                    worker_index=0,
                    worker_count=11,
                )
            catalog.write_text("generation-two-is-different\n", encoding="utf-8")
            with mock.patch.object(
                worker,
                "partition_runtime_aware_worker_rows",
                side_effect=RuntimeError("header routing unavailable"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "formal runtime partition cache selection failed"
                ):
                    worker.select_cached_runtime_aware_formal_worker_rows(
                        rows,
                        catalog_path=catalog,
                        inventory=inventory,
                        batch="batch0002",
                        worker_index=0,
                        worker_count=11,
                    )

    def test_formal_partition_cache_rejects_duplicate_input_asset_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            catalog = inventory / "total_asset_catalog.csv"
            inventory.mkdir()
            catalog.write_text("catalog\n", encoding="utf-8")
            duplicate = {
                "asset_id": "same",
                "render_order": "2001",
                "render_batch": "0002",
                "model_file": str(root / "same.blend"),
                "render_engine_hint": "source",
            }
            with self.assertRaisesRegex(ValueError, "invalid asset IDs"):
                worker.select_cached_runtime_aware_formal_worker_rows(
                    [duplicate, {**duplicate, "render_order": "2002"}],
                    catalog_path=catalog,
                    inventory=inventory,
                    batch="batch0002",
                    worker_index=0,
                    worker_count=11,
                )

    def test_source_family_gpu0_requirement_uses_header_and_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.blend"
            modern = root / "modern.blend"
            legacy.write_bytes(b"BLENDER-v300")
            modern.write_bytes(b"BLENDER-v450")
            self.assertTrue(worker.row_requires_source_family_gpu0({
                "model_file": str(legacy), "render_engine_hint": "source",
            }))
            self.assertFalse(worker.row_requires_source_family_gpu0({
                "model_file": str(legacy), "render_engine_hint": "CYCLES",
            }))
            self.assertFalse(worker.row_requires_source_family_gpu0({
                "model_file": str(modern), "render_engine_hint": "source",
            }))

    def test_runtime_aware_gpu0_workers_require_physical_gpu0(self) -> None:
        worker.validate_runtime_aware_partition(
            enabled=True,
            worker_index=4,
            worker_count=11,
            gpu=0,
            excluded_partitions=(),
        )
        for changes in (
            {"worker_count": 8},
            {"worker_index": 4, "gpu": 1},
            {"excluded_partitions": ((7, 8),)},
        ):
            values = {
                "enabled": True,
                "worker_index": 4,
                "worker_count": 11,
                "gpu": 0,
                "excluded_partitions": (),
                **changes,
            }
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_runtime_aware_partition(**values)

    def test_obsolete_batch0002_failures_are_narrowly_retryable(self) -> None:
        gpu_race = {
            "asset_id": "004023",
            "status": "failed",
            "render_batch": "0004",
            "failure_stage": "contract_validation",
            "failure_category": "remote_render_failure",
            "failure_code": "RuntimeError",
            "knowledge_version": (
                worker.render_knowledge
                .OBSOLETE_POST_RENDER_GPU_ATTESTATION_KNOWLEDGE_VERSION
            ),
            "error": (
                "Saved: '/tmp/total_asset_render/batch0004/004023/"
                "output/six_views/iso.png'\n"
                "TOTAL_ASSET_GPU_UUID_ATTESTATION_FAILED "
                "observed=none probe=ok\nBlender quit"
            ),
        }
        self.assertEqual(
            worker.obsolete_failure_repair_rules(gpu_race),
            (worker.render_knowledge.POST_RENDER_GPU_ATTESTATION_RACE_RULE,),
        )
        material = {
            "status": "failed",
            "render_batch": "0002",
            "error": (
                "material_audit objects; materials[index] is None; "
                "IndexError: list index out of range"
            ),
        }
        self.assertEqual(
            worker.obsolete_failure_repair_rules(material),
            ("material_slot_index_bounds_guard",),
        )
        motion = {
            "status": "failed",
            "render_batch": "0002",
            "knowledge_version": "total-asset-render-2026-07-16-v5",
            "issues": [
                "missing six_views/front.png",
                "dynamic downgraded to static after visible-motion gate",
            ],
        }
        self.assertEqual(
            worker.obsolete_failure_repair_rules(motion),
            ("dynamic_to_static_visible_motion_fallback",),
        )

        rejected = (
            {**material, "render_batch": "0001"},
            {**material, "status": "accepted"},
            {**material, "knowledge_version": worker.render_knowledge.KNOWLEDGE_VERSION},
            {**material, "knowledge_version": "total-asset-render-2026-07-19-v7"},
            {**material, "knowledge_version": ["unknown-schema"]},
            {**motion, "issues": ["missing six_views/front.png"]},
            {**motion, "knowledge_version": worker.render_knowledge.KNOWLEDGE_VERSION},
        )
        for previous in rejected:
            with self.subTest(previous=previous):
                self.assertEqual(worker.obsolete_failure_repair_rules(previous), ())

    def test_obsolete_failure_retry_cli_contract_is_fail_closed(self) -> None:
        valid = {
            "enabled": True,
            "batch": "batch0002",
            "batch_index": 2,
            "resume_unprocessed_only": True,
            "force": False,
        }
        worker.validate_obsolete_failure_retry(**valid)
        for changes in (
            {"batch": "batch0001"},
            {"batch": "batch0002_quality_repair"},
            {"batch_index": 1},
            {"resume_unprocessed_only": False},
            {"force": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_obsolete_failure_retry(**{**valid, **changes})
        worker.validate_obsolete_failure_retry(**{
            **valid,
            "enabled": False,
            "batch": "batch9999",
            "force": True,
        })

    def test_source_root_never_expands_to_site_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            site = root / "BlenderKit"
            asset = site / "asset-123"
            asset.mkdir(parents=True)
            model = asset / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            with mock.patch.object(worker, "ROOT", root):
                selected = worker.effective_source_root(model, site)
            self.assertEqual(selected, asset)
            self.assertNotEqual(selected, site)

    def test_model_directly_under_site_root_uses_model_only_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            site = root / "BlenderKit"
            site.mkdir(parents=True)
            model = site / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            with mock.patch.object(worker, "ROOT", root):
                selected = worker.effective_source_root(model, site)
            self.assertEqual(selected, model)

    def test_course_resource_expansion_stops_below_site_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            site = root / "bili"
            course = site / "course"
            chapter = course / "01 - intro"
            (course / "Resources").mkdir(parents=True)
            chapter.mkdir()
            model = chapter / "lesson.blend"
            model.write_bytes(b"BLENDER-v500")
            with mock.patch.object(worker, "ROOT", root):
                selected = worker.effective_source_root(model, chapter)
            self.assertEqual(selected, course)
            self.assertNotEqual(selected, site)

    def test_cooperative_drain_stops_before_claiming_next_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drain_file = Path(temporary) / "batch0001.wc11.drain.json"
            processed: list[str] = []

            def process(row: dict[str, str]) -> bool:
                processed.append(row["asset_id"])
                drain_file.write_text("{}", encoding="utf-8")
                return True

            result = worker.process_worker_rows(
                [{"asset_id": "a"}, {"asset_id": "b"}],
                drain_file=drain_file,
                process_row=process,
            )
            self.assertEqual(result.status, "drained")
            self.assertEqual(result.attempted, 1)
            self.assertEqual(result.last_asset_id, "a")
            self.assertEqual(processed, ["a"])

    def test_preexisting_drain_claims_no_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drain_file = Path(temporary) / "batch0001.wc11.drain.json"
            drain_file.write_text("{}", encoding="utf-8")
            process = mock.Mock(return_value=True)
            result = worker.process_worker_rows(
                [{"asset_id": "a"}],
                drain_file=drain_file,
                process_row=process,
            )
            self.assertEqual(result.status, "drained")
            self.assertEqual(result.attempted, 0)
            process.assert_not_called()

    def test_repair_closure_drain_has_dedicated_non_failure_exit(self) -> None:
        self.assertEqual(
            worker.worker_completion_exit_code(
                "drained", repair_closure_enabled=True
            ),
            worker.COOPERATIVE_DRAIN_EXIT,
        )
        self.assertEqual(
            worker.worker_completion_exit_code(
                "drained", repair_closure_enabled=False
            ),
            0,
        )
        self.assertEqual(
            worker.worker_completion_exit_code(
                "complete", repair_closure_enabled=True
            ),
            0,
        )

    def test_drained_runtime_state_is_atomic_and_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            worker.write_worker_runtime_state(
                path,
                {"batch": "batch0001", "started_at": "start"},
                "drained",
                attempted=3,
                last_asset_id="asset-3",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "drained")
            self.assertEqual(payload["state"], "drained")
            self.assertEqual(payload["attempted"], 3)
            self.assertIn("ended_at", payload)
            self.assertEqual(list(path.parent.glob(".runtime.json.*.tmp")), [])

    def test_worker_runtime_identifies_active_knowledge_policy(self) -> None:
        metadata = worker.knowledge_runtime_metadata()
        self.assertEqual(
            metadata["knowledge_version"],
            worker.render_knowledge.KNOWLEDGE_VERSION,
        )
        self.assertEqual(
            metadata["render_knowledge_context_schema"],
            worker.structured_render_knowledge.CONTEXT_SCHEMA_VERSION,
        )
        self.assertEqual(metadata["structured_recipe_mode"], "shadow_only")

    def test_worker_queue_identity_validation_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "without asset_id"):
            worker.validate_worker_rows([{"asset_id": "", "render_batch": "0000"}], "batch0000")
        with self.assertRaisesRegex(ValueError, "duplicate asset_id"):
            worker.validate_worker_rows(
                [
                    {"asset_id": "A", "render_batch": "0000"},
                    {"asset_id": "A", "render_batch": "0000"},
                ],
                "batch0000",
            )
        with self.assertRaisesRegex(ValueError, "outside render_batch"):
            worker.validate_worker_rows(
                [{"asset_id": "A", "render_batch": "0001"}],
                "batch0000_repair",
            )
        worker.validate_worker_rows(
            [{"asset_id": "A", "render_batch": "0000"}],
            "batch0000_repair",
        )

    def test_quarantine_partition_uses_original_zero_based_batch_position(self) -> None:
        rows = [
            {"asset_id": f"asset-{position + 1}", "render_order": str(position + 1001)}
            for position in range(88)
        ]
        selected, excluded = worker.select_worker_rows(
            rows,
            worker_index=8,
            worker_count=11,
            excluded_partitions=((7, 8),),
        )
        selected_positions = [int(row["render_order"]) - 1001 for row in selected]
        self.assertTrue(selected_positions)
        self.assertTrue(all(position % 11 == 8 for position in selected_positions))
        self.assertTrue(all(position % 8 != 7 for position in selected_positions))
        self.assertEqual(excluded, 1)
        # render_order is one-based; the old 7/8 shard therefore has order % 8 == 0.
        self.assertNotIn(1008, [int(row["render_order"]) for row in selected])

    def test_quarantine_partition_is_applied_after_canonical_partitioning(self) -> None:
        rows = [{"asset_id": str(position)} for position in range(1000)]
        all_selected: set[str] = set()
        total_excluded = 0
        for worker_index in range(11):
            selected, excluded = worker.select_worker_rows(
                rows,
                worker_index=worker_index,
                worker_count=11,
                excluded_partitions=((7, 8),),
            )
            ids = {row["asset_id"] for row in selected}
            self.assertFalse(all_selected & ids)
            all_selected.update(ids)
            total_excluded += excluded
        expected = {str(position) for position in range(1000) if position % 8 != 7}
        self.assertEqual(all_selected, expected)
        self.assertEqual(total_excluded, 125)

    def test_excluded_partition_parser_fails_closed(self) -> None:
        self.assertEqual(
            worker.parse_excluded_worker_partitions(["7/8"]),
            ((7, 8),),
        )
        for invalid in ("", "8/8", "-1/8", "7:8", "7/0"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                worker.parse_excluded_worker_partitions([invalid])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            worker.parse_excluded_worker_partitions(["7/8", "7/8"])

    def test_quarantine_exclusion_is_narrow_to_exact_transition_slots(self) -> None:
        valid = {
            "excluded_partitions": ((7, 8),),
            "batch": "batch0001",
            "batch_index": 1,
            "batch_size": 1000,
            "remote_port": 30808,
            "gpu": 0,
            "worker_index": 8,
            "worker_count": 11,
            "resume_unprocessed_only": True,
            "force": False,
        }
        worker.validate_quarantine_exclusion(**valid)
        invalid_changes = (
            {"batch": "batch0002"},
            {"remote_port": 30773, "gpu": 0, "worker_index": 4},
            {"worker_index": 9},
            {"excluded_partitions": ((6, 8),)},
            {"resume_unprocessed_only": False},
            {"force": True},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.validate_quarantine_exclusion(**{**valid, **changes})
        worker.validate_quarantine_exclusion(**{
            **valid,
            "excluded_partitions": (),
            "batch": "batch9999",
        })

    def test_remote_render_uses_shared_gpu_lock(self) -> None:
        remote = FakeRemote()
        result = worker.run_remote_render(
            remote,
            "blender -b asset.blend",
            owner="test-owner",
            wait_seconds=17,
        )
        self.assertEqual(result, "ok")
        command, timeout = remote.commands[0]
        self.assertIn("/tmp/total_asset_gpu_2.lock", command)
        self.assertIn("flock -w 17", command)
        self.assertIn("test-owner", command)
        self.assertIn(
            f"--kill-after={worker.REMOTE_RENDER_KILL_AFTER_SECONDS}s",
            command,
        )
        self.assertEqual(
            timeout,
            worker.REMOTE_RENDER_TIMEOUT_SECONDS
            + 17
            + worker.REMOTE_RENDER_KILL_AFTER_SECONDS
            + worker.REMOTE_RENDER_WATCHDOG_GRACE_SECONDS,
        )

    def test_remote_render_diagnostic_wrapper_persists_atomic_exit_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            worker.secrets,
            "token_hex",
            return_value="ab" * 16,
        ):
            owner = "unsafe owner; password=hunter2"
            paths = worker.remote_render_diagnostic_paths(
                owner,
                diagnostic_root=str(Path(temporary) / "diagnostics"),
            )
            self.assertNotIn(owner, paths.attempt_dir)
            self.assertRegex(paths.owner_sha256, r"^[0-9a-f]{64}$")
            wrapped = worker.wrap_remote_render_diagnostic_command(
                "printf 'blender stdout\\n'; "
                "printf 'monitor stderr\\n' >&2; exit 37",
                paths,
            )
            self.assertEqual(wrapped.count("blender stdout"), 1)
            self.assertNotIn("hunter2", wrapped)
            completed = subprocess.run(
                ["/bin/bash", "-lc", wrapped],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 37)
            self.assertIn("blender stdout", completed.stdout)
            self.assertIn("monitor stderr", completed.stdout)
            self.assertIn("phase=command_exit", completed.stdout)
            sentinel = Path(paths.exit_path).read_text(encoding="utf-8")
            self.assertIn("schema=total_asset_remote_render_exit.v1", sentinel)
            self.assertIn("exitstatus=37", sentinel)
            fetched = subprocess.run(
                [
                    "/bin/bash",
                    "-lc",
                    worker.remote_render_diagnostic_fetch_command(paths),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(fetched.returncode, 0)
            self.assertIn("log_marker_counts_begin=1", fetched.stdout)
            self.assertIn("remote_started=1", fetched.stdout)
            self.assertIn("remote_command_exit=1", fetched.stdout)
            self.assertIn("log_marker_lines_begin=1", fetched.stdout)
            self.assertIn("exit_sentinel_begin=1", fetched.stdout)

    def test_real_diagnostic_wrapper_and_bounded_fetch_form_strict_recovery(self) -> None:
        remote = FakeRemote()
        remote.native_system_graphics_runtime = False
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            worker.secrets,
            "token_hex",
            return_value="ac" * 16,
        ):
            paths = worker.remote_render_diagnostic_paths(
                "strict-real-fetch",
                diagnostic_root=str(Path(temporary) / "diagnostics"),
            )
            evidence_lines = [
                (
                    f"{worker.RUNTIME_READY_MARKER} "
                    f"driver={worker.RUNTIME_DRIVER_VERSION}"
                ),
                (
                    f"{worker.VULKAN_RENDER_MONITOR_STARTED_MARKER} pid=1234 "
                    f"policy={worker.GPU_PID_ATTESTATION_POLICY}"
                ),
                (
                    f"{worker.VULKAN_RENDER_MONITOR_READY_MARKER} pid=1234 "
                    f"uuid={remote.gpu_uuid} "
                    f"policy={worker.GPU_PID_ATTESTATION_POLICY} "
                    f"observed={remote.gpu_uuid} pmon_indices={remote.gpu} "
                    f"samples={worker.GPU_PID_ATTESTATION_STABLE_SAMPLES}"
                ),
                "Blender render complete",
            ]
            render_command = "printf '%s\\n' " + " ".join(
                shlex.quote(line) for line in evidence_lines
            )
            wrapped = worker.wrap_remote_render_diagnostic_command(
                render_command,
                paths,
            )
            rendered = subprocess.run(
                ["/bin/bash", "-lc", wrapped],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(rendered.returncode, 0)
            fetched = subprocess.run(
                [
                    "/bin/bash",
                    "-lc",
                    worker.remote_render_diagnostic_fetch_command(paths),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(fetched.returncode, 0)
            recovered = worker.recover_completed_remote_render(
                fetched.stdout,
                paths,
                remote,
                render_command,
                require_vulkan_monitor=True,
            )
            self.assertIsNotNone(recovered)
            self.assertIn("Blender render complete", recovered or "")

    def test_protocol_incomplete_exit_zero_recovers_only_from_strict_render_proof(self) -> None:
        remote = FakeRemote()
        remote.native_system_graphics_runtime = False
        owner = "strict-protocol-recovery"
        interruption = worker.RemoteTransportError(
            "worker_ssh_protocol_incomplete",
            returncode=0,
            detail="authenticated SSH envelope ended before its trailer",
        )
        with mock.patch.object(
            worker.secrets, "token_hex", return_value="ab" * 16
        ):
            paths = worker.remote_render_diagnostic_paths(owner)
            recovered = strict_render_recovery_payload(paths, remote)
            remote.run = mock.Mock(
                side_effect=[interruption, recovered]
            )  # type: ignore[method-assign]
            output = worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner=owner,
                wait_seconds=0,
                require_vulkan_monitor=True,
            )
        self.assertIn("Blender render complete", output)
        self.assertIn(worker.VULKAN_RENDER_MONITOR_READY_MARKER, output)
        self.assertEqual(remote.run.call_count, 2)

    def test_protocol_recovery_accepts_strict_fetch_stdout_when_only_fetch_trailer_is_missing(
        self,
    ) -> None:
        remote = FakeRemote()
        remote.native_system_graphics_runtime = False
        owner = "strict-protocol-double-trailer-loss"
        initial_interruption = worker.RemoteTransportError(
            "worker_ssh_protocol_incomplete",
            returncode=0,
            detail="render SSH envelope ended before its trailer",
        )
        with mock.patch.object(
            worker.secrets, "token_hex", return_value="ad" * 16
        ):
            paths = worker.remote_render_diagnostic_paths(owner)
            recovered = strict_render_recovery_payload(paths, remote)
            fetch_interruption = worker.RemoteTransportError(
                "worker_ssh_protocol_incomplete",
                returncode=0,
                detail=recovered.diagnostic_text,
            )
            remote.run = mock.Mock(
                side_effect=[initial_interruption, fetch_interruption]
            )  # type: ignore[method-assign]
            output = worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner=owner,
                wait_seconds=0,
                require_vulkan_monitor=True,
            )
        self.assertIn("Blender render complete", output)
        self.assertEqual(remote.run.call_count, 2)

    def test_protocol_recovery_rejects_mismatched_fetch_stdout_when_fetch_trailer_is_missing(
        self,
    ) -> None:
        remote = FakeRemote()
        remote.native_system_graphics_runtime = False
        owner = "strict-protocol-double-trailer-loss-reject"
        initial_interruption = worker.RemoteTransportError(
            "worker_ssh_protocol_incomplete",
            returncode=0,
        )
        with mock.patch.object(
            worker.secrets, "token_hex", return_value="ae" * 16
        ):
            paths = worker.remote_render_diagnostic_paths(owner)
            mismatched = strict_render_recovery_payload(
                paths,
                remote,
                sentinel_owner="0" * 64,
            )
            fetch_interruption = worker.RemoteTransportError(
                "worker_ssh_protocol_incomplete",
                returncode=0,
                detail=mismatched.diagnostic_text,
            )
            remote.run = mock.Mock(
                side_effect=[initial_interruption, fetch_interruption]
            )  # type: ignore[method-assign]
            with self.assertRaises(worker.RemoteTransportError) as caught:
                worker.run_remote_render(
                    remote,
                    "blender -b asset.blend",
                    owner=owner,
                    wait_seconds=0,
                    require_vulkan_monitor=True,
                )
        self.assertIs(caught.exception, initial_interruption)
        self.assertEqual(remote.run.call_count, 2)

    def test_protocol_recovery_rejects_bad_sentinel_and_marker_evidence(self) -> None:
        remote = FakeRemote()
        remote.native_system_graphics_runtime = False
        owner = "strict-protocol-recovery-reject"
        with mock.patch.object(
            worker.secrets, "token_hex", return_value="cd" * 16
        ):
            paths = worker.remote_render_diagnostic_paths(owner)
            valid_payload_lines = (
                strict_render_recovery_payload(paths, remote).stdout.splitlines()
            )
            marker_begin = valid_payload_lines.index("log_marker_lines_begin=1")
            marker_end = valid_payload_lines.index("log_marker_lines_end=1")
            valid_markers = valid_payload_lines[marker_begin + 1:marker_end]
            sentinel_begin = valid_payload_lines.index("exit_sentinel_begin=1")
            sentinel_end = valid_payload_lines.index("exit_sentinel_end=1")
            missing_sentinel_lines = (
                valid_payload_lines[:sentinel_begin]
                + valid_payload_lines[sentinel_end + 1:]
            )
            unexpected_wrapper_markers = [
                *valid_markers,
                (
                    f"{worker.REMOTE_RENDER_DIAGNOSTIC_MARKER} phase=started "
                    f"owner_sha256={'0' * 64} attempt_id={'1' * 32}"
                ),
            ]
            variants = {
                "missing_sentinel": worker.RemoteCommandOutput(
                    "\n".join(missing_sentinel_lines) + "\n", ""
                ),
                "wrong_schema": strict_render_recovery_payload(
                    paths, remote, sentinel_schema="wrong.schema"
                ),
                "wrong_owner": strict_render_recovery_payload(
                    paths, remote, sentinel_owner="0" * 64
                ),
                "wrong_attempt": strict_render_recovery_payload(
                    paths, remote, sentinel_attempt="ef" * 16
                ),
                "nonzero_exit": strict_render_recovery_payload(
                    paths, remote, sentinel_exitstatus="1"
                ),
                "exit_255": strict_render_recovery_payload(
                    paths, remote, sentinel_exitstatus="255"
                ),
                "duplicate_sentinel": strict_render_recovery_payload(
                    paths,
                    remote,
                    extra_sentinel_lines=("exitstatus=0",),
                ),
                "duplicate_capability": strict_render_recovery_payload(
                    paths, remote, counts={"runtime_ready": 2}
                ),
                "unexpected_wrapper_marker": strict_render_recovery_payload(
                    paths,
                    remote,
                    marker_lines=unexpected_wrapper_markers,
                ),
                "missing_capability": strict_render_recovery_payload(
                    paths,
                    remote,
                    counts={"runtime_ready": 0},
                    marker_lines=[
                        line
                        for line in valid_markers
                        if not line.startswith(worker.RUNTIME_READY_MARKER)
                    ],
                ),
                "missing_monitor": strict_render_recovery_payload(
                    paths,
                    remote,
                    counts={"monitor_ready": 0},
                    marker_lines=[
                        line
                        for line in valid_markers
                        if not line.startswith(
                            worker.VULKAN_RENDER_MONITOR_READY_MARKER
                        )
                    ],
                ),
            }
            for label, diagnostic in variants.items():
                with self.subTest(label=label):
                    interruption = worker.RemoteTransportError(
                        "worker_ssh_protocol_incomplete", returncode=0
                    )
                    remote.run = mock.Mock(
                        side_effect=[interruption, diagnostic]
                    )  # type: ignore[method-assign]
                    with self.assertRaises(worker.RemoteTransportError) as caught:
                        worker.run_remote_render(
                            remote,
                            "blender -b asset.blend",
                            owner=owner,
                            wait_seconds=0,
                            require_vulkan_monitor=True,
                        )
                    self.assertIs(caught.exception, interruption)

    def test_protocol_recovery_requires_matching_native_and_vulkan_capabilities(self) -> None:
        owner = "strict-protocol-capabilities"
        cases = (
            (True, False, True, "blender -b asset.blend"),
            (False, True, True, "blender --gpu-backend vulkan -b asset.blend"),
            (False, False, False, "blender -b asset.blend"),
        )
        for native, uses_vulkan, require_monitor, command in cases:
            with self.subTest(
                native=native,
                uses_vulkan=uses_vulkan,
                require_monitor=require_monitor,
            ):
                remote = FakeRemote()
                remote.native_system_graphics_runtime = native
                with mock.patch.object(
                    worker.secrets, "token_hex", return_value="de" * 16
                ):
                    paths = worker.remote_render_diagnostic_paths(owner)
                    diagnostic = strict_render_recovery_payload(
                        paths,
                        remote,
                        uses_vulkan=uses_vulkan,
                        require_monitor=require_monitor,
                    )
                    interruption = worker.RemoteTransportError(
                        "worker_ssh_protocol_incomplete", returncode=0
                    )
                    remote.run = mock.Mock(
                        side_effect=[interruption, diagnostic]
                    )  # type: ignore[method-assign]
                    output = worker.run_remote_render(
                        remote,
                        command,
                        owner=owner,
                        wait_seconds=0,
                        require_vulkan_monitor=require_monitor,
                    )
                self.assertIn("Blender render complete", output)

    def test_protocol_recovery_never_applies_to_other_transport_identity(self) -> None:
        remote = FakeRemote()
        remote.native_system_graphics_runtime = False
        owner = "strict-protocol-recovery-gate"
        with mock.patch.object(
            worker.secrets, "token_hex", return_value="ef" * 16
        ):
            paths = worker.remote_render_diagnostic_paths(owner)
            diagnostic = strict_render_recovery_payload(paths, remote)
            failures = (
                worker.RemoteTransportError(
                    "worker_ssh_protocol_incomplete", returncode=None
                ),
                worker.RemoteTransportError(
                    "worker_ssh_protocol_incomplete", returncode=255
                ),
                worker.RemoteTransportError("worker_ssh_failed", returncode=0),
            )
            for interruption in failures:
                with self.subTest(
                    code=interruption.code, returncode=interruption.returncode
                ):
                    remote.run = mock.Mock(
                        side_effect=[interruption, diagnostic]
                    )  # type: ignore[method-assign]
                    with self.assertRaises(worker.RemoteTransportError) as caught:
                        worker.run_remote_render(
                            remote,
                            "blender -b asset.blend",
                            owner=owner,
                            wait_seconds=0,
                            require_vulkan_monitor=True,
                        )
                    self.assertIs(caught.exception, interruption)

            interruption = worker.RemoteTransportError(
                "worker_ssh_protocol_incomplete", returncode=0
            )
            diagnostic_timeout = worker.RemoteTransportError("worker_ssh_timeout")
            remote.run = mock.Mock(
                side_effect=[interruption, diagnostic_timeout]
            )  # type: ignore[method-assign]
            with self.assertRaises(worker.RemoteTransportError) as caught:
                worker.run_remote_render(
                    remote,
                    "blender -b asset.blend",
                    owner=owner,
                    wait_seconds=0,
                    require_vulkan_monitor=True,
                )
            self.assertIs(caught.exception, interruption)

    def test_transport_reset_attaches_bounded_persisted_render_tail(self) -> None:
        remote = FakeRemote()
        interruption = worker.RemoteTransportError(
            "worker_ssh_failed",
            returncode=255,
            detail="Connection reset by peer",
        )
        recovered = worker.RemoteCommandOutput(
            (
                f"{worker.REMOTE_RENDER_DIAGNOSTIC_MARKER} "
                "phase=transport_recovery\n"
                "log_state=present\n"
                "Blender monitor exited password=hunter2\n"
                "exit_sentinel_state=present\n"
                "schema=total_asset_remote_render_exit.v1\n"
                "exitstatus=78\n"
            ),
            "",
        )
        remote.run = mock.Mock(
            side_effect=[interruption, recovered]
        )  # type: ignore[method-assign]
        with mock.patch.object(
            worker.secrets,
            "token_hex",
            return_value="cd" * 16,
        ), self.assertRaises(worker.RemoteTransportError) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
                diagnostic_root="/tmp/asset-safe-root",
            )
        self.assertIs(caught.exception, interruption)
        self.assertEqual(remote.run.call_count, 2)
        recovery_command = remote.run.call_args_list[1].args[0]
        self.assertIn(
            f"tail -c {worker.REMOTE_RENDER_DIAGNOSTIC_LOG_TAIL_BYTES}",
            recovery_command,
        )
        self.assertIn(
            f"head -c {worker.REMOTE_RENDER_DIAGNOSTIC_SENTINEL_BYTES}",
            recovery_command,
        )
        self.assertEqual(
            remote.run.call_args_list[1].kwargs["timeout"],
            worker.REMOTE_RENDER_DIAGNOSTIC_FETCH_TIMEOUT_SECONDS,
        )
        self.assertIn("Connection reset by peer", caught.exception.detail)
        self.assertIn("[persisted remote render diagnostic]", caught.exception.detail)
        self.assertIn("exitstatus=78", caught.exception.detail)
        self.assertNotIn("hunter2", caught.exception.detail)
        self.assertIn("password=[REDACTED]", caught.exception.detail)
        self.assertLessEqual(
            len(caught.exception.detail),
            worker.REMOTE_RENDER_DIAGNOSTIC_DETAIL_LIMIT,
        )

    def test_failed_diagnostic_reconnect_never_masks_transport_error(self) -> None:
        remote = FakeRemote()
        interruption = worker.RemoteTransportError(
            "worker_ssh_failed",
            returncode=255,
            detail="original reset",
        )
        recovery_failure = worker.RemoteTransportError(
            "worker_ssh_timeout",
        )
        remote.run = mock.Mock(
            side_effect=[interruption, recovery_failure]
        )  # type: ignore[method-assign]
        with self.assertRaises(worker.RemoteTransportError) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertIs(caught.exception, interruption)
        self.assertEqual(caught.exception.detail, "original reset")
        self.assertEqual(remote.run.call_count, 2)

    def test_source_transfer_timeout_scales_and_is_bounded(self) -> None:
        with (
            mock.patch.object(worker, "SOURCE_TRANSFER_MIN_TIMEOUT_SECONDS", 1800),
            mock.patch.object(worker, "SOURCE_TRANSFER_MAX_TIMEOUT_SECONDS", 21600),
            mock.patch.object(
                worker, "SOURCE_TRANSFER_MIN_BYTES_PER_SECOND", 2 * 1024**2
            ),
            mock.patch.object(worker, "SOURCE_TRANSFER_GRACE_SECONDS", 900),
        ):
            self.assertEqual(worker.source_transfer_timeout_seconds(1024**3), 1800)
            self.assertEqual(
                worker.source_transfer_timeout_seconds(18 * 1024**3),
                10116,
            )
            self.assertEqual(
                worker.source_transfer_timeout_seconds(100 * 1024**3),
                21600,
            )
            with self.assertRaises(ValueError):
                worker.source_transfer_timeout_seconds(-1)

    def test_remote_lock_busy_has_dedicated_exception(self) -> None:
        remote = FakeRemote()

        def busy(_command: str, timeout: int = 900) -> str:
            del timeout
            raise RuntimeError("exitstatus=75 TOTAL_ASSET_GPU_BUSY")

        remote.run = busy  # type: ignore[method-assign]
        with self.assertRaises(GpuLockBusy):
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )

    def test_unmarked_exit_75_is_not_misclassified_as_gpu_busy(self) -> None:
        remote = FakeRemote()

        def application_failure(_command: str, timeout: int = 900) -> str:
            del timeout
            raise RuntimeError("exitstatus=75 application returned a temporary failure")

        remote.run = application_failure  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertNotIsInstance(caught.exception, GpuLockBusy)

    def test_remote_command_255_is_an_asset_error_not_transport(self) -> None:
        remote = FakeRemote()
        failure = worker.RemoteCommandError(
            255,
            stdout="Blender failed while opening this asset",
            stderr="DeprecationWarning: harmless warning",
        )
        remote.run = mock.Mock(side_effect=failure)  # type: ignore[method-assign]
        with self.assertRaises(worker.RemoteCommandError) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertEqual(caught.exception.returncode, 255)
        self.assertEqual(remote.run.call_count, 1)

    def test_transport_identity_wins_over_partial_vulkan_output(self) -> None:
        remote = FakeRemote()
        interruption = worker.RemoteTransportError(
            "worker_ssh_failed",
            returncode=255,
            detail=(
                "vkGetDeviceQueue: Invalid device "
                "[VUID-vkGetDeviceQueue-device-parameter]"
            ),
        )
        remote.run = mock.Mock(side_effect=interruption)  # type: ignore[method-assign]
        with mock.patch.object(worker.time, "sleep") as sleep, self.assertRaises(
            worker.RemoteTransportError
        ) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertIs(caught.exception, interruption)
        sleep.assert_not_called()

    def test_vulkan_device_queue_error_blocks_worker_without_retry(self) -> None:
        remote = FakeRemote()
        failure = worker.RemoteCommandError(
            255,
            stdout=(
                "ERROR: vkGetDeviceQueue: Invalid device "
                "[VUID-vkGetDeviceQueue-device-parameter]"
            ),
            stderr="DeprecationWarning: Material.use_nodes",
        )
        remote.run = mock.Mock(side_effect=failure)  # type: ignore[method-assign]
        with mock.patch.object(worker.time, "sleep") as sleep, self.assertRaises(
            worker.RuntimeCapabilityBlocked
        ) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertEqual(caught.exception.code, "vulkan_device_queue_invalid")
        self.assertEqual(remote.run.call_count, 1)
        sleep.assert_not_called()

    def test_vulkan_initialization_retries_once_then_blocks(self) -> None:
        remote = FakeRemote()
        failure = worker.RemoteCommandError(
            255,
            stdout=(
                "ERROR Vulkan: vkCreateDevice(device) resulted in code "
                "VK_ERROR_INITIALIZATION_FAILED"
            ),
            stderr="DeprecationWarning: Material.use_nodes",
        )
        remote.run = mock.Mock(side_effect=[failure, failure])  # type: ignore[method-assign]
        with (
            mock.patch.object(worker.time, "sleep") as sleep,
            mock.patch.object(
                worker.secrets,
                "token_hex",
                side_effect=["11" * 16, "22" * 16],
            ),
            self.assertRaises(worker.RuntimeCapabilityBlocked) as caught,
        ):
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertEqual(
            caught.exception.code, "vulkan_device_initialization_failed"
        )
        self.assertEqual(remote.run.call_count, 2)
        self.assertIn("11" * 16, remote.run.call_args_list[0].args[0])
        self.assertIn("22" * 16, remote.run.call_args_list[1].args[0])
        self.assertNotEqual(
            remote.run.call_args_list[0].args[0],
            remote.run.call_args_list[1].args[0],
        )
        sleep.assert_called_once_with(20)

    def test_vulkan_initialization_retry_can_recover(self) -> None:
        remote = FakeRemote()
        failure = worker.RemoteCommandError(
            255,
            stdout=(
                "vkCreateDevice(device) resulted in code "
                "VK_ERROR_INITIALIZATION_FAILED"
            ),
        )
        remote.run = mock.Mock(side_effect=[failure, "render complete"])  # type: ignore[method-assign]
        with mock.patch.object(worker.time, "sleep") as sleep:
            output = worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertEqual(output, "render complete")
        sleep.assert_called_once_with(20)

    def test_gpu_uuid_attestation_failure_blocks_worker_not_asset(self) -> None:
        remote = FakeRemote()
        failure = worker.RemoteCommandError(
            1,
            stderr=(
                "RuntimeError: TOTAL_ASSET_GPU_UUID_ATTESTATION_FAILED "
                f"expected={remote.gpu_uuid} "
                "observed=gpu-11111111-1111-1111-1111-111111111111"
            ),
        )
        remote.run = mock.Mock(side_effect=failure)  # type: ignore[method-assign]
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertEqual(
            caught.exception.code, "gpu_process_uuid_attestation_failed"
        )

    def test_cycles_device_attestation_failure_blocks_worker_not_asset(self) -> None:
        remote = FakeRemote()
        failure = worker.RemoteCommandError(
            1,
            stderr=(
                "RuntimeError: TOTAL_ASSET_CYCLES_DEVICE_ATTESTATION_FAILED "
                "cuda_gpu_count=2 expected=1"
            ),
        )
        remote.run = mock.Mock(side_effect=failure)  # type: ignore[method-assign]
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
            worker.run_remote_render(
                remote,
                "blender -b asset.blend",
                owner="test-owner",
                wait_seconds=0,
            )
        self.assertEqual(
            caught.exception.code, "cycles_cuda_device_attestation_failed"
        )

    def test_render_contract_requires_exact_pid_uuid_and_one_cuda_device(self) -> None:
        contract = {
            "render_engine": "CYCLES",
            "gpu_process_attestation": {
                "status": "attested",
                "expected_gpu_uuid": FakeRemote.gpu_uuid,
                "gpu_uuids": [FakeRemote.gpu_uuid],
            },
            "cycles_runtime": {
                "backend": "CUDA",
                "devices": [
                    {"name": "RTX 5090", "type": "CUDA", "use": True},
                    {"name": "CPU", "type": "CPU", "use": False},
                ],
            },
        }
        worker.validate_render_contract_gpu_binding(contract, FakeRemote.gpu_uuid)
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as wrong_uuid:
            worker.validate_render_contract_gpu_binding(
                {
                    **contract,
                    "gpu_process_attestation": {
                        **contract["gpu_process_attestation"],
                        "gpu_uuids": [
                            "gpu-11111111-1111-1111-1111-111111111111"
                        ],
                    },
                },
                FakeRemote.gpu_uuid,
            )
        self.assertEqual(
            wrong_uuid.exception.code, "gpu_process_uuid_attestation_failed"
        )
        with self.assertRaises(worker.RuntimeCapabilityBlocked) as cpu_enabled:
            worker.validate_render_contract_gpu_binding(
                {
                    **contract,
                    "cycles_runtime": {
                        **contract["cycles_runtime"],
                        "devices": [
                            {"name": "RTX 5090", "type": "CUDA", "use": True},
                            {"name": "CPU", "type": "CPU", "use": True},
                        ],
                    },
                },
                FakeRemote.gpu_uuid,
            )
        self.assertEqual(
            cpu_enabled.exception.code, "cycles_cuda_device_attestation_failed"
        )

    def test_static_fallback_rewrites_auto_route_argument(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--out-dir output --route auto --engine source"
        )
        rewritten = worker.rewrite_route_argument(command)
        self.assertIn("--route static --engine source", rewritten)
        self.assertNotIn("--route auto", rewritten)

    def test_static_fallback_rewrites_dynamic_route_argument(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--out-dir output --route dynamic --engine source"
        )
        rewritten = worker.rewrite_route_argument(command)
        self.assertIn("--route static --engine source", rewritten)
        self.assertNotIn("--route dynamic", rewritten)

    def test_short_dynamic_video_reaches_visible_motion_fallback_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "asset.blend").write_bytes(b"x" * 2048)
            (output / "final_effect.mp4").write_bytes(b"short-video")
            with (
                mock.patch.object(worker, "probe_video", return_value=(5.0, 70.0)) as probe,
                mock.patch.object(
                    worker,
                    "video_motion_stats",
                    return_value={"passed": True},
                ),
                mock.patch.object(worker, "select_preview", return_value=(True, "ok")),
                mock.patch.object(worker, "image_stats", return_value=(True, "ok")),
            ):
                status, issues, motion = worker.validate_dynamic(output)

        self.assertEqual(status, "needs_review")
        self.assertTrue(any(
            "insufficient visible motion: short final_effect.mp4" in issue
            for issue in issues
        ))
        self.assertTrue(motion["short_video"])
        self.assertEqual(motion["video_bytes"], len(b"short-video"))
        probe.assert_called_once()

    def test_full_frame_gradient_never_passes_as_high_coverage_subject(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "gradient.png"
            image = Image.new("RGB", (256, 256))
            image.putdata([
                (row, row, row)
                for row in range(256)
                for _column in range(256)
            ])
            image.save(path)
            ok, message = worker.image_stats(path)
        self.assertFalse(ok)
        self.assertIn("subject_edge_sides=4", message)
        coverage = float(
            re.search(r"subject_coverage=([0-9.]+)", message).group(1)
        )
        self.assertGreaterEqual(coverage, 0.80)

    def test_high_coverage_subject_may_touch_at_most_two_edges(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "two-edges.png"
            image = Image.new("RGB", (256, 256), (245, 245, 245))
            ImageDraw.Draw(image).rectangle(
                (18, 0, 237, 255), fill=(45, 110, 180)
            )
            image.save(path)
            ok, message = worker.image_stats(path)
        self.assertTrue(ok, message)
        self.assertIn("subject_edge_sides=2", message)

    def test_subject_highlight_clip_is_not_diluted_by_black_background(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "washed-white-subject.png"
            image = Image.new("RGB", (256, 256), (8, 8, 8))
            draw = ImageDraw.Draw(image)
            draw.rectangle((64, 64, 191, 191), fill=(252, 252, 252))
            draw.line((64, 128, 191, 128), fill=(220, 220, 220), width=4)
            image.save(path)
            ok, message = worker.image_stats(path)
        self.assertFalse(ok)
        self.assertIn("subject_luma_clip=", message)
        whole_frame_clip = float(
            re.search(r"\bwhite_clip=([0-9.]+)", message).group(1)
        )
        subject_clip = float(
            re.search(r"\bsubject_luma_clip=([0-9.]+)", message).group(1)
        )
        self.assertLess(whole_frame_clip, worker.render_knowledge.IMAGE_QUALITY["max_white_clip"])
        self.assertGreater(
            subject_clip,
            worker.render_knowledge.IMAGE_QUALITY["max_subject_luma_clip"],
        )

    def test_quality_rescue_isolates_overexposure_from_lighting(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--lighting auto --camera-policy auto --exposure 0.0"
        )
        rescued, actions = worker.build_quality_rescue_command(
            command,
            dark=False,
            overexposed=True,
            bad_framing=False,
        )
        self.assertIn("--lighting auto", rescued)
        self.assertIn("--exposure -1.0", rescued)
        self.assertNotIn("--lighting studio", rescued)
        self.assertEqual(actions, ("reduced exposure -1.0 EV",))

    def test_quality_rescue_scales_negative_exposure_for_severe_clipping(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--lighting auto --camera-policy auto --exposure 0.0"
        )
        rescued, actions = worker.build_quality_rescue_command(
            command,
            dark=False,
            overexposed=True,
            bad_framing=False,
            issues=[
                "iso quality: subject_luma_clip=0.831, white_clip=0.175"
            ],
        )
        self.assertIn("--exposure -2.0", rescued)
        self.assertEqual(actions, ("reduced exposure -2.0 EV",))

    def test_quality_rescue_framing_only_never_changes_brightness(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--lighting auto --camera-policy auto --exposure 0.0"
        )
        rescued, actions = worker.build_quality_rescue_command(
            command,
            dark=False,
            overexposed=False,
            bad_framing=True,
        )
        self.assertIn("--lighting auto", rescued)
        self.assertIn("--exposure 0.0", rescued)
        self.assertIn("--camera-policy fallback", rescued)
        self.assertEqual(actions, ("fallback camera",))

    def test_quality_rescue_cropped_dynamic_expands_framing_margin(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--lighting auto --camera-policy auto --exposure 0.0"
        )
        rescued, actions = worker.build_quality_rescue_command(
            command,
            dark=False,
            overexposed=False,
            bad_framing=True,
            issues=[
                "preview quality: subject_bbox_coverage=0.600, "
                "subject_edge_sides=2"
            ],
        )
        self.assertIn("--camera-policy fallback", rescued)
        self.assertIn("--framing-margin 1.35", rescued)
        self.assertEqual(actions, ("fallback camera",))

    def test_quality_rescue_small_subject_preserves_existing_margin(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--lighting auto --camera-policy auto --exposure 0.0"
        )
        rescued, _actions = worker.build_quality_rescue_command(
            command,
            dark=False,
            overexposed=False,
            bad_framing=True,
            issues=[
                "iso quality: subject_coverage=0.100, "
                "subject_bbox_coverage=0.050, subject_edge_sides=0"
            ],
        )
        self.assertIn("--framing-margin 1.10", rescued)

    def test_quality_rescue_darkness_may_add_one_studio_rig(self) -> None:
        command = (
            "blender -b asset.blend --python render.py -- "
            "--lighting auto --camera-policy auto --exposure 0.0"
        )
        rescued, actions = worker.build_quality_rescue_command(
            command,
            dark=True,
            overexposed=False,
            bad_framing=False,
        )
        self.assertIn("--lighting studio", rescued)
        self.assertIn("--exposure 1.0", rescued)
        self.assertEqual(actions, ("studio fill lighting", "raised exposure"))

    def test_rescue_selection_restores_primary_unless_status_improves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            (staging / "marker.txt").write_text("primary", encoding="utf-8")
            backup = worker.stage_primary_for_rescue(staging)
            (staging / "marker.txt").write_text("rescue", encoding="utf-8")
            kept = worker.finish_rescue_selection(
                staging,
                backup,
                primary_status="needs_review",
                rescue_status="needs_review",
            )
            self.assertFalse(kept)
            self.assertEqual(
                (staging / "marker.txt").read_text(encoding="utf-8"),
                "primary",
            )

    def test_rescue_selection_keeps_same_status_when_highlight_metric_improves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            (staging / "marker.txt").write_text("primary", encoding="utf-8")
            backup = worker.stage_primary_for_rescue(staging)
            (staging / "marker.txt").write_text("rescue", encoding="utf-8")
            kept = worker.finish_rescue_selection(
                staging,
                backup,
                primary_status="needs_review",
                rescue_status="needs_review",
                primary_issues=[
                    "preview quality: subject_luma_clip=0.700, white_clip=0.300",
                    "insufficient visible motion: active_transitions=0",
                ],
                rescue_issues=[
                    "preview quality: subject_luma_clip=0.200, white_clip=0.050",
                    "insufficient visible motion: active_transitions=0",
                ],
                overexposed=True,
            )
            self.assertTrue(kept)
            self.assertEqual(
                (staging / "marker.txt").read_text(encoding="utf-8"),
                "rescue",
            )

    def test_framing_improvement_cannot_introduce_highlight_clipping(self) -> None:
        improved = worker.rescue_quality_improved(
            [
                "preview quality: subject_coverage=0.010, "
                "subject_bbox_coverage=0.020, subject_edge_sides=0, "
                "subject_luma_clip=0.100"
            ],
            [
                "preview quality: subject_coverage=0.100, "
                "subject_bbox_coverage=0.200, subject_edge_sides=0, "
                "subject_luma_clip=0.400"
            ],
            dark=False,
            overexposed=False,
            bad_framing=True,
        )
        self.assertFalse(improved)

    def test_minor_overexposed_view_is_not_tolerated_as_edge_on(self) -> None:
        passed = (
            True,
            "mean=0.200, spread=0.200, subject_coverage=0.2000, "
            "subject_mean=0.500, subject_bbox_coverage=0.3000, "
            "subject_edge_sides=0, flat_neutral_ratio=0.100, "
            "colorfulness=0.100, white_clip=0.000, "
            "subject_luma_clip=0.000, subject_luma_p95=0.700",
        )
        clipped = (
            False,
            "mean=0.200, spread=0.200, subject_coverage=0.2000, "
            "subject_mean=0.900, subject_bbox_coverage=0.3000, "
            "subject_edge_sides=0, flat_neutral_ratio=0.900, "
            "colorfulness=0.010, white_clip=0.100, "
            "subject_luma_clip=0.200, subject_luma_p95=0.999",
        )
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            (out / "asset.blend").write_bytes(b"x" * 2048)
            views = out / "six_views"
            views.mkdir()
            for name in ("front", "back", "left", "right", "top", "iso"):
                (views / f"{name}.png").write_bytes(b"x" * 10_001)
            with mock.patch.object(
                worker,
                "image_stats",
                side_effect=[clipped, passed, passed, passed, passed, passed],
            ):
                status, issues = worker.validate_static(out)
        self.assertEqual(status, "needs_review")
        self.assertTrue(any(issue.startswith("front quality:") for issue in issues))
        self.assertFalse(any(issue.startswith("edge-on view tolerated:") for issue in issues))

    def test_dynamic_peak_highlight_is_checked_even_when_preview_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            (out / "asset.blend").write_bytes(b"x" * 2048)
            (out / "final_effect.mp4").write_bytes(b"x" * 60_000)
            with (
                mock.patch.object(worker, "probe_video", return_value=(5.0, 72.0)),
                mock.patch.object(
                    worker,
                    "video_motion_stats",
                    return_value={"passed": True, "active_transitions": 4},
                ),
                mock.patch.object(
                    worker,
                    "video_highlight_stats",
                    return_value={
                        "sample_frames": 10,
                        "subject_luma_clip_median": 0.05,
                        "subject_luma_clip_max": 0.50,
                        "subject_luma_p95_max": 1.0,
                        "white_clip_median": 0.01,
                        "white_clip_max": 0.20,
                    },
                ),
                mock.patch.object(worker, "select_preview", return_value=(True, "ok")),
                mock.patch.object(worker, "image_stats", return_value=(True, "ok")),
            ):
                status, issues, motion = worker.validate_dynamic(out)
        self.assertEqual(status, "needs_review")
        self.assertTrue(any(issue.startswith("video highlight quality:") for issue in issues))
        self.assertEqual(
            motion["highlight_quality"]["subject_luma_clip_max"], 0.50
        )

    def test_corrupt_blend_signature_precedes_missing_dependency(self) -> None:
        message = (
            "Failed to open dir: No such file or directory; "
            "Failed to read blend file: Missing DNA block"
        )
        self.assertEqual(worker.failure_category(message), "source_corrupt")

    def test_auto_rig_pro_presets_skip_before_capacity_or_remote_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = (
                root
                / "Auto-Rig Pro 3.75.14"
                / "auto_rig_pro-master"
                / "armature_presets"
                / "Gorilla.blend"
            )
            model.parent.mkdir(parents=True)
            model.write_bytes(b"BLENDER-v340" + b"x" * 2048)
            status_path = root / "status.jsonl"
            remote = FakeRemote()
            capacity = mock.Mock(side_effect=AssertionError("capacity must not run"))
            with mock.patch.object(worker, "get_disk_capacity", capacity):
                worker.render_one(
                    remote,
                    {
                        "asset_id": "000104",
                        "identity_key": "helper-identity",
                        "render_order": "1024",
                        "render_batch": "0001",
                        "model_file": str(model),
                        "source_root": str(model.parent),
                        "title": "Gorilla",
                    },
                    status_path,
                    "batch0001",
                )

            result = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(
                result["failure_category"], "source_no_renderable_objects"
            )
            self.assertIn(
                "tutorial_plugin_preset_exclusion",
                result["knowledge_rules_applied"],
            )
            self.assertEqual(remote.commands, [])
            capacity.assert_not_called()

    def test_route_rewrite_fails_closed_on_ambiguous_shell_text(self) -> None:
        command = (
            "blender -b '/tmp/source --route auto file.blend' --python render.py -- "
            "--route dynamic --engine source"
        )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            worker.rewrite_route_argument(command)

    def test_busy_render_is_blocked_not_terminal_or_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            status_path = root / "inventory" / "status.jsonl"
            remote = FakeRemote()
            row = {
                "asset_id": "000001",
                "identity_key": "identity-1",
                "render_order": "1",
                "render_batch": "0000",
                "model_file": str(model),
                "source_root": str(root),
                "title": "asset",
            }
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
                mock.patch.object(
                    worker, "run_remote_render", side_effect=GpuLockBusy("busy")
                ) as render,
            ):
                with self.assertRaises(GpuLockBusy):
                    worker.render_one(
                        remote,
                        row,
                        status_path,
                        "batch0000",
                        gpu_wait_seconds=5,
                    )
            self.assertFalse(status_path.exists())
            primary_command = render.call_args.args[1]
            self.assertIn("BLENDER_USER_CONFIG=/tmp/profile-5.1/config", primary_command)
            self.assertIn("--gpu-backend vulkan -b", primary_command)
            self.assertIn(
                f"--python-exit-code {worker.FORMAL_BLENDER_PYTHON_EXIT_CODE}",
                primary_command,
            )
            self.assertIn("--python-expr", primary_command)
            self.assertIn(
                worker.VULKAN_RENDER_MONITOR_STARTED_MARKER,
                primary_command,
            )
            self.assertTrue(
                render.call_args.kwargs["require_vulkan_monitor"]
            )
            self.assertEqual(
                render.call_args.kwargs["diagnostic_root"],
                "/tmp/total_asset_render/batch0000/000001/render_diagnostics",
            )

    def test_formal_cycles_render_uses_python_exit_code_and_full_lifetime_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            status_path = root / "inventory" / "status.jsonl"
            remote = FakeRemote()
            remote.vulkan_available = False
            row = {
                "asset_id": "000001",
                "identity_key": "identity-1",
                "render_order": "1",
                "render_batch": "0000",
                "render_engine_hint": "CYCLES",
                "model_file": str(model),
                "source_root": str(root),
                "title": "asset",
            }
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
                mock.patch.object(
                    worker, "run_remote_render", side_effect=GpuLockBusy("busy")
                ) as render,
            ):
                with self.assertRaises(GpuLockBusy):
                    worker.render_one(
                        remote,
                        row,
                        status_path,
                        "batch0000",
                        gpu_wait_seconds=5,
                    )

            command = render.call_args.args[1]
            self.assertIn("--engine CYCLES", command)
            self.assertNotIn("--gpu-backend vulkan", command)
            self.assertIn("--python-expr", command)
            self.assertIn(worker.VULKAN_RENDER_MONITOR_STARTED_MARKER, command)
            self.assertIn(
                f"--python-exit-code {worker.FORMAL_BLENDER_PYTHON_EXIT_CODE}",
                command,
            )
            self.assertTrue(render.call_args.kwargs["require_vulkan_monitor"])
            self.assertFalse(status_path.exists())

    def test_transport_interruption_is_worker_attempt_not_asset_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            status_path = root / "inventory" / "status.jsonl"
            remote = FakeRemote()
            row = {
                "asset_id": "000001",
                "identity_key": "identity-1",
                "render_order": "1",
                "render_batch": "0001",
                "model_file": str(model),
                "source_root": str(root),
                "title": "asset",
            }
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            interruption = worker.RemoteTransportError("worker_ssh_timeout")
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
                mock.patch.object(
                    worker, "run_remote_render", side_effect=interruption
                ),
            ):
                with self.assertRaises(worker.RemoteTransportError) as caught:
                    worker.render_one(remote, row, status_path, "batch0001")
            self.assertEqual(caught.exception.stage, "primary_render")
            self.assertFalse(status_path.exists())
            self.assertFalse(
                any(command.startswith("rm -rf /tmp/total_asset_render/batch0001")
                    for command, _timeout in remote.commands[2:])
            )

            runtime_drift = worker.RuntimeCapabilityBlocked(
                "nvidia_graphics_runtime_invalid",
                worker.RUNTIME_INVALID_MARKER + " code=runtime_file_hash_mismatch",
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
                mock.patch.object(
                    worker, "run_remote_render", side_effect=runtime_drift
                ),
            ):
                with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                    worker.render_one(remote, row, status_path, "batch0001")
            self.assertEqual(
                caught.exception.code, "nvidia_graphics_runtime_invalid"
            )
            self.assertEqual(caught.exception.stage, "primary_render")
            self.assertFalse(status_path.exists())

    def test_missing_contract_with_gpu_identity_evidence_is_not_formal_failure(self) -> None:
        class MissingContractRemote(FakeRemote):
            def get(self, _remote: str, local: Path, timeout: int = 1800) -> None:
                del timeout
                local.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            status_path = root / "inventory" / "status.jsonl"
            remote = MissingContractRemote()
            row = {
                "asset_id": "004023",
                "identity_key": "identity-4023",
                "render_order": "4001",
                "render_batch": "0004",
                "model_file": str(model),
                "source_root": str(root),
                "title": "Modern Abstract Decorative Shelf",
            }
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            diagnostic = (
                "six views saved\nRuntimeError: "
                "TOTAL_ASSET_GPU_UUID_ATTESTATION_FAILED "
                f"expected={remote.gpu_uuid} observed=none probe=ok"
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
                mock.patch.object(worker, "run_remote_render", return_value=diagnostic),
            ):
                with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                    worker.render_one(remote, row, status_path, "batch0004")

            self.assertEqual(
                caught.exception.code, "gpu_process_uuid_attestation_failed"
            )
            self.assertEqual(caught.exception.stage, "contract_validation")
            self.assertFalse(status_path.exists())

    def test_remote_255_writes_only_the_current_asset_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            status_path = root / "inventory" / "status.jsonl"
            remote = FakeRemote()
            row = {
                "asset_id": "000001",
                "identity_key": "identity-1",
                "render_order": "1",
                "render_batch": "0001",
                "model_file": str(model),
                "source_root": str(root),
                "title": "asset",
            }
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            failure = worker.RemoteCommandError(
                255,
                stdout="asset-specific Blender failure",
                stderr="DeprecationWarning: harmless",
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
                mock.patch.object(worker, "run_remote_render", side_effect=failure),
            ):
                worker.render_one(remote, row, status_path, "batch0001")
            records = [
                json.loads(line)
                for line in status_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["asset_id"], "000001")
            self.assertEqual(records[0]["status"], "failed")
            self.assertEqual(records[0]["remote_exit_status"], 255)
            self.assertEqual(
                records[0]["failure_code"], "worker_remote_command_failed"
            )
            self.assertTrue(
                records[0]["error"].startswith(
                    "exitstatus=255 worker_remote_command_failed"
                )
            )

    def test_blender_loader_failure_is_nonterminal_for_total_and_highqal(
        self,
    ) -> None:
        for profile, static_views, verify_packed_reopen in (
            ("total", "six", False),
            ("highqal", "iso-only", True),
        ):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                model = root / "asset.blend"
                model.write_bytes(b"BLENDER-v500")
                status_path = root / "inventory" / f"{profile}.jsonl"
                remote = FakeRemote()
                row = {
                    "asset_id": "000001",
                    "identity_key": "identity-1",
                    "render_order": "1",
                    "render_batch": "0001",
                    "model_file": str(model),
                    "source_root": str(root),
                    "title": "asset",
                }
                capacity = DiskCapacity(
                    total_bytes=2 * 1024**4,
                    used_bytes=1024**3,
                    free_bytes=2 * 1024**4 - 1024**3,
                    source="test",
                )
                loader_failure = worker.RemoteCommandError(
                    127,
                    stderr=(
                        "/root/blender-4.5.10-linux-x64/blender: error while "
                        "loading shared libraries: libICE.so.6: cannot open "
                        "shared object file: No such file or directory"
                    ),
                )
                with (
                    mock.patch.object(worker, "ROOT", root),
                    mock.patch.object(
                        worker, "RENDER_ROOT", root / "total_render"
                    ),
                    mock.patch.object(
                        worker, "get_disk_capacity", return_value=capacity
                    ),
                    mock.patch.object(
                        worker,
                        "run_remote_render",
                        side_effect=loader_failure,
                    ),
                ):
                    with self.assertRaises(
                        worker.RuntimeCapabilityBlocked
                    ) as caught:
                        worker.render_one(
                            remote,
                            row,
                            status_path,
                            "batch0001",
                            render_root=root / profile,
                            static_views=static_views,
                            verify_packed_reopen=verify_packed_reopen,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "blender_runtime_shared_library_missing",
                )
                self.assertEqual(caught.exception.stage, "primary_render")
                self.assertFalse(status_path.exists())

    def test_vulkan_card_error_is_nonterminal_for_asset_and_stops_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            status_path = root / "inventory" / "status.jsonl"
            remote = FakeRemote()
            row = {
                "asset_id": "000001",
                "identity_key": "identity-1",
                "render_order": "1",
                "render_batch": "0001",
                "model_file": str(model),
                "source_root": str(root),
                "title": "asset",
            }
            capacity = DiskCapacity(
                total_bytes=2 * 1024**4,
                used_bytes=1024**3,
                free_bytes=2 * 1024**4 - 1024**3,
                source="test",
            )
            blocked = worker.RuntimeCapabilityBlocked(
                "vulkan_device_queue_invalid", "exact Vulkan evidence"
            )
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(worker, "get_disk_capacity", return_value=capacity),
                mock.patch.object(worker, "run_remote_render", side_effect=blocked),
            ):
                with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                    worker.render_one(remote, row, status_path, "batch0001")
            self.assertEqual(caught.exception.stage, "primary_render")
            self.assertFalse(status_path.exists())

    def test_runtime_transport_diagnostic_keeps_structured_prefix(self) -> None:
        detail = "x" * 5000 + "\nConnection reset by peer"
        exc = worker.RemoteTransportError(
            "worker_ssh_failed",
            stage="primary_render",
            detail=detail,
            returncode=255,
        )
        fields = worker.worker_runtime_exception_fields(
            exc,
            default_stage="worker_processing",
            default_category="worker_processing",
        )
        self.assertEqual(fields["failure_code"], "worker_ssh_failed")
        self.assertEqual(fields["transport_returncode"], 255)
        self.assertTrue(
            str(fields["error"]).startswith(
                "primary_render: exitstatus=255 worker_ssh_failed"
            )
        )
        self.assertLessEqual(len(str(fields["error"])), 2000)

    def test_local_source_spool_failure_has_stable_infrastructure_identity(self) -> None:
        fields = worker.worker_runtime_exception_fields(
            worker.LocalSourceSpoolError(
                "local_source_spool_model_verification_failed"
            ),
            default_stage="worker_processing",
            default_category="worker_processing",
        )
        self.assertEqual(fields["failure_stage"], "local_source_preparation")
        self.assertEqual(fields["failure_category"], "local_source_preparation")
        self.assertEqual(
            fields["failure_code"],
            "local_source_spool_model_verification_failed",
        )

    def test_unknown_capacity_stops_before_remote_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            remote = FakeRemote()
            row = {
                "asset_id": "000001",
                "model_file": str(model),
                "source_root": str(root),
            }
            with (
                mock.patch.object(worker, "ROOT", root),
                mock.patch.object(worker, "RENDER_ROOT", root / "total_render"),
                mock.patch.object(
                    worker,
                    "get_disk_capacity",
                    side_effect=DiskCapacityError("no trustworthy source"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "queue paused"):
                    worker.render_one(remote, row, root / "status.jsonl", "batch0000")
            self.assertEqual(remote.commands, [])
            self.assertFalse((root / "status.jsonl").exists())

    def test_final_audit_is_recorded_as_shadow_recipe_decision(self) -> None:
        payload = worker.build_structured_knowledge_decision(
            {"title": "cloth banner", "render_route": "dynamic_candidate"},
            {
                "render_route": "dynamic",
                "animation_evidence": {"cloth_simulations": 1},
            },
            "static",
            ["dynamic downgraded to static after visible-motion gate"],
        )
        context = payload["render_knowledge_context"]
        match = payload["recipe_match"]
        self.assertEqual(context["source_kind"], "total_asset")
        self.assertEqual(context["route"], "static")
        self.assertEqual(context["render_profile"], "static_fallback")
        self.assertEqual(match["decision"], "abstain")
        self.assertIn("dynamic_to_static_fallback", match["candidate_recipe_ids"])

    def test_repair_claim_retries_only_transient_short_lock_contention(self) -> None:
        busy = subprocess.CompletedProcess(
            [], 75, "", "blocked: repair closure lock busy\n"
        )
        claimed = subprocess.CompletedProcess(
            [], 0, json.dumps({"claim": "claimed"}), ""
        )
        with (
            mock.patch.object(
                worker, "_repair_closure_command", side_effect=(busy, claimed)
            ) as command,
            mock.patch.object(worker.time, "sleep") as sleep,
        ):
            self.assertTrue(worker.claim_repair_closure_asset(
                generation="repair-" + "1" * 24,
                group_key=(
                    "batch0002/failed/runtime_api_compat/modern_vulkan"
                ),
                attempt_id="attempt-" + "a" * 32,
                asset_id="asset-1",
            ))
        self.assertEqual(command.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_repair_closure_command_uses_current_python_interpreter(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "{}", "")
        with mock.patch.object(
            worker.subprocess, "run", return_value=completed
        ) as run:
            result = worker._repair_closure_command(
                "verify-launch",
                generation="repair-" + "1" * 24,
                group_key="batch0002/failed/runtime_api_compat/modern_vulkan",
                attempt_id="attempt-" + "a" * 32,
            )
        self.assertIs(result, completed)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("total_asset_repair_closure.py", argv[1])
        self.assertIn("verify-launch", argv)

    def test_primary_565_node_uses_native_runtime_without_pinned_570_exports(self) -> None:
        remote = worker.Remote(worker.PRIMARY_REMOTE_PORT, 2)
        remote.pci_selector = "pci-0000_01_00_0"
        remote.gpu_uuid = "gpu-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.assertTrue(remote.native_system_graphics_runtime)
        with mock.patch.object(
            remote,
            "run",
            return_value=(
                worker.PINNED_NATIVE_RUNTIME_READY_MARKER
                + " driver="
                + worker.NATIVE_SYSTEM_DRIVER_VERSION
            ),
        ) as run:
            remote.ensure_nvidia_graphics_runtime()
        validation = run.call_args.args[0]
        self.assertIn(worker.NATIVE_SYSTEM_DRIVER_VERSION, validation)
        self.assertIn(worker.PINNED_NATIVE_RUNTIME_INVALID_MARKER, validation)
        self.assertNotIn(worker.RUNTIME_SOURCE_SHA256, validation)
        environment = remote.blender_env()
        self.assertIn("unset LD_LIBRARY_PATH", environment)
        self.assertNotIn(worker.REMOTE_GL_ROOT, environment)
        self.assertNotIn(worker.RUNTIME_SOURCE_SHA256, environment)
        metadata = remote.graphics_runtime_metadata()
        self.assertEqual(
            metadata["graphics_environment_policy"],
            worker.PINNED_NATIVE_RUNTIME_POLICY,
        )

    def test_5090_node_retains_exact_pinned_570_runtime(self) -> None:
        remote = worker.Remote(worker.TERTIARY_REMOTE_PORT, 1)
        remote.pci_selector = "pci-0000_01_00_0"
        remote.gpu_uuid = "gpu-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.assertFalse(remote.native_system_graphics_runtime)
        environment = remote.blender_env()
        self.assertIn(worker.RUNTIME_DRIVER_VERSION, environment)
        self.assertIn(worker.RUNTIME_MANIFEST_DIGEST, environment)
        self.assertIn(f"export LD_LIBRARY_PATH={worker.REMOTE_GL_ROOT};", environment)
        self.assertNotIn(worker.PINNED_NATIVE_RUNTIME_READY_MARKER, environment)

    def test_primary_565_nonzero_gpu_is_cycles_only_until_vulkan_proven(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "modern.blend"
            model.write_bytes(b"BLENDER-v450" + b"\0" * 64)
            remote = worker.Remote(worker.PRIMARY_REMOTE_PORT, 1)
            with mock.patch.object(remote, "run") as run:
                with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                    worker.ensure_row_runtime_capability(
                        remote,
                        {
                            "model_file": str(model),
                            "render_engine_hint": "source",
                        },
                        allow_forward_compatible_blender=False,
                    )
            self.assertEqual(caught.exception.code, "native_system_cycles_only")
            run.assert_not_called()

            with mock.patch.object(
                remote,
                "run",
                return_value=worker.PINNED_NATIVE_RUNTIME_READY_MARKER,
            ) as run:
                worker.ensure_row_runtime_capability(
                    remote,
                    {
                        "model_file": str(model),
                        "render_engine_hint": "CYCLES",
                    },
                    allow_forward_compatible_blender=False,
                )
            self.assertEqual(run.call_count, 1)

    def test_primary_565_gpu0_is_also_cycles_only_without_graphics_stack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "legacy.blend"
            model.write_bytes(b"BLENDER-v360" + b"\0" * 64)
            remote = worker.Remote(worker.PRIMARY_REMOTE_PORT, 0)
            with mock.patch.object(remote, "run") as run:
                with self.assertRaises(worker.RuntimeCapabilityBlocked) as caught:
                    worker.ensure_row_runtime_capability(
                        remote,
                        {
                            "model_file": str(model),
                            "render_engine_hint": "source",
                        },
                        allow_forward_compatible_blender=False,
                    )
            self.assertEqual(caught.exception.code, "native_system_cycles_only")
            run.assert_not_called()
            with mock.patch.object(
                remote,
                "run",
                return_value=worker.PINNED_NATIVE_RUNTIME_READY_MARKER,
            ) as run:
                worker.ensure_row_runtime_capability(
                    remote,
                    {
                        "model_file": str(model),
                        "render_engine_hint": "CYCLES",
                    },
                    allow_forward_compatible_blender=False,
                )
            self.assertEqual(run.call_count, 1)
            self.assertTrue(remote._nvidia_graphics_runtime_validated)


class HighqalWorkerIntegrationTests(unittest.TestCase):
    def test_packed_reopen_ignores_only_contract_attested_benign_paths(self) -> None:
        smooth_by_angle = (
            "/f/software/model/blender-4.5.3-windows-x64/4.5/"
            "datafiles/assets/geometry_nodes/smooth_by_angle.blend"
        )
        output = (
            "HIGHQAL_PACKED_REOPEN_MISSING:"
            + json.dumps(
                [smooth_by_angle, "/cache/export_blenderkit.blend"],
                separators=(",", ":"),
            )
        )
        with mock.patch.object(
            worker, "run_remote_render", return_value=output
        ) as remote_render:
            result = worker.verify_remote_packed_blend_reopen(
                FakeRemote(),
                blender_binary="/opt/blender/blender",
                runtime_environment="env TEST=1",
                blend_path="/tmp/asset.blend",
                owner="highqal:test",
                diagnostic_root="/tmp/diagnostics",
                benign_missing=[
                    "F:/software/model/blender-4.5.3-windows-x64/4.5/"
                    "datafiles/assets/geometry_nodes/smooth_by_angle.blend"
                ],
            )

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["missing_external_resources"], 0)
        self.assertEqual(result["total_missing_external_resources"], 2)
        self.assertEqual(result["benign_missing_external_resources"], 2)
        remote_render.assert_called_once()
        self.assertFalse(
            remote_render.call_args.kwargs["require_vulkan_monitor"]
        )

    def test_packed_reopen_keeps_unattested_missing_path_critical(self) -> None:
        smooth_by_angle = "/assets/smooth_by_angle.blend"
        missing_texture = "/source/missing-texture.png"
        output = (
            "HIGHQAL_PACKED_REOPEN_MISSING:"
            + json.dumps(
                [smooth_by_angle, missing_texture], separators=(",", ":")
            )
        )
        with mock.patch.object(
            worker, "run_remote_render", return_value=output
        ):
            result = worker.verify_remote_packed_blend_reopen(
                FakeRemote(),
                blender_binary="/opt/blender/blender",
                runtime_environment="env TEST=1",
                blend_path="/tmp/asset.blend",
                owner="highqal:test",
                diagnostic_root="/tmp/diagnostics",
                benign_missing=[smooth_by_angle],
            )

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["missing_external_resources"], 1)
        self.assertEqual(result["total_missing_external_resources"], 2)
        self.assertEqual(result["benign_missing_external_resources"], 1)

    def test_manifest_selection_preserves_nested_reference_profile(self) -> None:
        generation = "a" * 64
        item = {
            "work_item_id": "asset-1",
            "asset_id": "asset-1",
            "reference_profile": {"schema": "reference_profile.v1", "fps": 24.0},
        }
        with mock.patch(
            "highqal_source_priority.load_manifest",
            return_value={"generation": generation, "items": [item]},
        ), mock.patch(
            "highqal_source_priority.verify_manifest_item_files",
            return_value={"schema": "highqal-frozen-input-attestation.v1"},
        ) as verify:
            selected = worker.read_highqal_manifest_rows(
                Path("/tmp/manifest.json"),
                generation=generation,
                work_item_id="asset-1",
            )
        self.assertEqual(selected[0]["asset_id"], "asset-1")
        self.assertEqual(selected[0]["manifest_generation"], generation)
        self.assertIsInstance(selected[0]["reference_profile"], dict)
        self.assertEqual(
            selected[0]["frozen_input_attestation"]["schema"],
            "highqal-frozen-input-attestation.v1",
        )
        verify.assert_called_once_with(item)

    def test_reference_gate_cannot_upgrade_nonaccepted_result(self) -> None:
        with mock.patch(
            "highqal_source_priority.evaluate_reference_quality_gate",
            return_value={"status": "accepted"},
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "invalid status transition"):
                worker.evaluate_highqal_reference_gate(
                    {"asset_id": "asset-1"}, {"status": "needs_review"}
                )

    def test_reference_manifest_fields_are_carried_without_stringifying_profile(self) -> None:
        profile = {"schema": "reference_profile.v1", "motion": {"visible": True}}
        with mock.patch(
            "highqal_source_priority.validate_reference_profile",
            return_value=profile,
            create=True,
        ):
            fields = worker.highqal_manifest_status_fields({
                "bvid": "BV1234567890",
                "reference_video": "/tmp/reference.mp4",
                "reference_sha256": "b" * 64,
                "model_sha256": "c" * 64,
                "reference_profile": profile,
                "wave": 1,
            })
        self.assertEqual(fields["reference_profile"], profile)
        self.assertIsInstance(fields["reference_profile"], dict)
        self.assertEqual(fields["wave"], 1)

    def test_shared_highqal_status_append_remains_valid_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "highqal.jsonl"

            def writer(index: int) -> None:
                for sequence in range(8):
                    worker.append_status(status, {
                        "asset_id": f"asset-{index}",
                        "sequence": sequence,
                        "payload": "x" * 4096,
                    })

            threads = [threading.Thread(target=writer, args=(index,)) for index in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rows = [json.loads(line) for line in status.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 48)
            self.assertEqual({row["asset_id"] for row in rows}, {
                f"asset-{index}" for index in range(6)
            })

    def test_accepted_static_publication_contains_only_minimal_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            (evidence / "six_views").mkdir(parents=True)
            (evidence / "asset.blend").write_bytes(b"b" * 2048)
            (evidence / "six_views/iso.png").write_bytes(b"p" * 10001)
            (evidence / "render_review.json").write_text("{}", encoding="utf-8")
            delivery = worker.publish_highqal_minimal(
                row={"output_key": "asset-1"},
                review={
                    "status": "accepted",
                    "render_route": "static",
                    "output_dir": str(evidence),
                },
                final_root=root / "final",
            )
            destination = Path(str(delivery["final_dir"]))
            self.assertEqual(
                sorted(str(path.relative_to(destination)) for path in destination.rglob("*")),
                ["asset.blend", "six_views", "six_views/iso.png"],
            )
            self.assertEqual(delivery["asset_blend_sha256"], worker.file_sha256(destination / "asset.blend"))
            self.assertEqual(delivery["media_sha256"], worker.file_sha256(destination / "six_views/iso.png"))
            repeated = worker.publish_highqal_minimal(
                row={"output_key": "asset-1"},
                review={
                    "status": "accepted",
                    "render_route": "static",
                    "output_dir": str(evidence),
                },
                final_root=root / "final",
            )
            self.assertEqual(repeated, delivery)

    def _write_published_highqal_receipt(
        self,
        *,
        evidence_root: Path,
        row: dict[str, str],
        generation: str,
        route: str,
        destination: Path,
        updated_at: str,
    ) -> Path:
        media_relative = (
            "six_views/iso.png" if route == "static" else "final_effect.mp4"
        )
        asset = destination / "asset.blend"
        media = destination / media_relative
        outcome = {
            **row,
            "status": "accepted",
            "published": True,
            "render_route": route,
            "workload_generation": generation,
            "updated_at": updated_at,
            "final_dir": str(destination),
            "asset_blend_sha256": worker.file_sha256(asset),
            "asset_blend_size_bytes": asset.stat().st_size,
            "media_sha256": worker.file_sha256(media),
            "media_size_bytes": media.stat().st_size,
            "media_relative_path": media_relative,
        }
        path = worker.highqal_finalization_receipt_path(
            evidence_root,
            workload_generation=generation,
            work_item_id=row["work_item_id"],
            lease_id="prior-lease",
        )
        worker.atomic_write_runtime_state(path, {
            "schema": worker.HIGHQAL_FINALIZATION_SCHEMA,
            "phase": "completed",
            "origin_lease_id": "prior-lease",
            "reconcile_lease_ids": ["prior-lease"],
            "workload_kind": worker.HIGHQAL_SOURCE_WORKLOAD_KIND,
            "workload_generation": generation,
            "work_item_id": row["work_item_id"],
            "asset_id": row["asset_id"],
            "identity_key": row["identity_key"],
            "model_sha256": row["model_sha256"],
            "reference_sha256": row["reference_sha256"],
            "outcome": outcome,
            "outcome_sha256": worker._highqal_outcome_sha256(outcome),
            "updated_at": updated_at,
        })
        return path

    def test_newer_generation_archives_and_replaces_exact_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final_root = root / "highqal-data/rest_7_21"
            output_key = "asset-1"
            destination = final_root / "static" / output_key
            (destination / "six_views").mkdir(parents=True)
            (destination / "asset.blend").write_bytes(b"o" * 2048)
            (destination / "six_views/iso.png").write_bytes(b"i" * 10001)
            (destination / "legacy-proof.bin").write_bytes(b"proof")
            evidence_parent = root / "experiments/highqal-canary"
            prior_evidence = evidence_parent / "render_outputs.v19"
            current_evidence = evidence_parent / "render_outputs.v20"
            row = {
                "asset_id": "asset-1",
                "identity_key": "identity-1",
                "work_item_id": "asset-1--0123456789abcdef",
                "model_sha256": "a" * 64,
                "reference_sha256": "b" * 64,
                "output_key": output_key,
            }
            self._write_published_highqal_receipt(
                evidence_root=prior_evidence,
                row=row,
                generation="1" * 64,
                route="static",
                destination=destination,
                updated_at="2026-07-22 19:00:00",
            )
            (current_evidence / "six_views").mkdir(parents=True)
            (current_evidence / "asset.blend").write_bytes(b"n" * 2048)
            (current_evidence / "six_views/iso.png").write_bytes(b"q" * 10001)
            delivery = worker.publish_highqal_minimal(
                row=row,
                review={
                    **row,
                    "status": "accepted",
                    "render_route": "static",
                    "output_dir": str(current_evidence),
                    "workload_generation": "2" * 64,
                    "updated_at": "2026-07-22 20:00:00",
                },
                final_root=final_root,
                evidence_root=current_evidence,
            )
            self.assertEqual((destination / "asset.blend").read_bytes(), b"n" * 2048)
            self.assertEqual(
                (destination / "six_views/iso.png").read_bytes(), b"q" * 10001
            )
            archives = list(
                worker.highqal_publication_archive_root(final_root).glob(
                    "static/*/*/archive_receipt.json"
                )
            )
            self.assertEqual(len(archives), 1)
            archived_delivery = archives[0].parent / "delivery"
            self.assertEqual(
                (archived_delivery / "asset.blend").read_bytes(), b"o" * 2048
            )
            self.assertEqual(
                (archived_delivery / "legacy-proof.bin").read_bytes(), b"proof"
            )
            archive_receipt = json.loads(archives[0].read_text(encoding="utf-8"))
            self.assertEqual(archive_receipt["prior_identity"]["identity_key"], "identity-1")
            index = json.loads(
                worker._highqal_publication_index_path(
                    final_root=final_root, destination=destination
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(index["state"], "published")
            self.assertEqual(index["workload_generation"], "2" * 64)
            self.assertEqual(delivery["asset_blend_sha256"], worker.file_sha256(destination / "asset.blend"))

    def test_newer_generation_cannot_replace_different_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final_root = root / "highqal-data/rest_7_21"
            destination = final_root / "static/asset-1"
            (destination / "six_views").mkdir(parents=True)
            (destination / "asset.blend").write_bytes(b"o" * 2048)
            (destination / "six_views/iso.png").write_bytes(b"i" * 10001)
            evidence_parent = root / "experiments/highqal-canary"
            prior_evidence = evidence_parent / "render_outputs.v19"
            current_evidence = evidence_parent / "render_outputs.v20"
            prior_row = {
                "asset_id": "asset-1",
                "identity_key": "old-identity",
                "work_item_id": "asset-1--old",
                "model_sha256": "a" * 64,
                "reference_sha256": "b" * 64,
                "output_key": "asset-1",
            }
            self._write_published_highqal_receipt(
                evidence_root=prior_evidence,
                row=prior_row,
                generation="1" * 64,
                route="static",
                destination=destination,
                updated_at="2026-07-22 19:00:00",
            )
            (current_evidence / "six_views").mkdir(parents=True)
            (current_evidence / "asset.blend").write_bytes(b"n" * 2048)
            (current_evidence / "six_views/iso.png").write_bytes(b"q" * 10001)
            current_row = {
                **prior_row,
                "identity_key": "new-identity",
                "work_item_id": "asset-1--new",
            }
            with self.assertRaisesRegex(RuntimeError, "different task identity"):
                worker.publish_highqal_minimal(
                    row=current_row,
                    review={
                        **current_row,
                        "status": "accepted",
                        "render_route": "static",
                        "output_dir": str(current_evidence),
                        "workload_generation": "2" * 64,
                        "updated_at": "2026-07-22 20:00:00",
                    },
                    final_root=final_root,
                    evidence_root=current_evidence,
                )
            self.assertEqual((destination / "asset.blend").read_bytes(), b"o" * 2048)
            self.assertFalse(worker.highqal_publication_archive_root(final_root).exists())

    def test_newer_generation_cross_route_supersession_leaves_one_delivery(self) -> None:
        for old_route, new_route in (("static", "dynamic"), ("dynamic", "static")):
            with self.subTest(old_route=old_route, new_route=new_route):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    final_root = root / "highqal-data/rest_7_21"
                    output_key = "asset-1"
                    old_destination = final_root / old_route / output_key
                    old_destination.mkdir(parents=True)
                    (old_destination / "asset.blend").write_bytes(b"o" * 2048)
                    if old_route == "static":
                        (old_destination / "six_views").mkdir()
                        (old_destination / "six_views/iso.png").write_bytes(
                            b"i" * 10001
                        )
                    else:
                        (old_destination / "final_effect.mp4").write_bytes(
                            b"v" * 50001
                        )
                    evidence_parent = root / "experiments/highqal-canary"
                    prior_evidence = evidence_parent / "render_outputs.v19"
                    current_evidence = evidence_parent / "render_outputs.v20"
                    row = {
                        "asset_id": "asset-1",
                        "identity_key": "identity-1",
                        "work_item_id": "asset-1--0123456789abcdef",
                        "model_sha256": "a" * 64,
                        "reference_sha256": "b" * 64,
                        "output_key": output_key,
                    }
                    self._write_published_highqal_receipt(
                        evidence_root=prior_evidence,
                        row=row,
                        generation="1" * 64,
                        route=old_route,
                        destination=old_destination,
                        updated_at="2026-07-22 19:00:00",
                    )
                    current_evidence.mkdir(parents=True)
                    (current_evidence / "asset.blend").write_bytes(b"n" * 2048)
                    if new_route == "static":
                        (current_evidence / "six_views").mkdir()
                        (current_evidence / "six_views/iso.png").write_bytes(
                            b"q" * 10001
                        )
                    else:
                        (current_evidence / "final_effect.mp4").write_bytes(
                            b"w" * 50001
                        )
                    worker.publish_highqal_minimal(
                        row=row,
                        review={
                            **row,
                            "status": "accepted",
                            "render_route": new_route,
                            "output_dir": str(current_evidence),
                            "workload_generation": "2" * 64,
                            "updated_at": "2026-07-22 20:00:00",
                        },
                        final_root=final_root,
                        evidence_root=current_evidence,
                    )
                    self.assertFalse(old_destination.exists())
                    self.assertTrue((final_root / new_route / output_key).is_dir())
                    self.assertEqual(
                        len(
                            list(
                                worker.highqal_publication_archive_root(
                                    final_root
                                ).glob(f"{old_route}/*/*/archive_receipt.json")
                            )
                        ),
                        1,
                    )

    def test_finalization_recovers_across_lease_rotation_without_rerender(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            (evidence / "six_views").mkdir(parents=True)
            (evidence / "asset.blend").write_bytes(b"b" * 2048)
            (evidence / "six_views/iso.png").write_bytes(b"p" * 10001)
            (evidence / "render_review.json").write_text(
                json.dumps({"asset_id": "asset-1"}), encoding="utf-8"
            )
            row = {
                "asset_id": "asset-1",
                "identity_key": "identity-1",
                "output_key": "asset-1",
                "model_sha256": "a" * 64,
                "reference_sha256": "b" * 64,
                "reference_video": "/tmp/reference.mp4",
            }
            outcome = {
                "asset_id": "asset-1",
                "status": "accepted",
                "render_route": "static",
                "output_dir": str(evidence),
            }
            status = root / "status.jsonl"
            common = {
                "row": row,
                "outcome": outcome,
                "final_root": root / "final",
                "evidence_root": root / "logs",
                "status_path": status,
                "lease_db": root / "leases.sqlite3",
                "worker_index": 0,
                "node_boot_id": "boot-a",
                "workload_generation": "c" * 64,
                "work_item_id": "asset-1--0123456789abcdef",
            }
            with mock.patch.object(
                worker,
                "complete_highqal_work_item",
                side_effect=RuntimeError("transient db failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "transient db"):
                    worker.finalize_highqal_outcome(**common, lease_id="lease-1")
            self.assertEqual(len(status.read_text(encoding="utf-8").splitlines()), 1)
            with mock.patch.object(
                worker, "complete_highqal_work_item", return_value={"state": "completed"}
            ) as complete:
                recovery_args = {
                    **common,
                    "lease_id": "lease-2",
                    "node_boot_id": "boot-b",
                    "worker_index": 4,
                }
                recovered = worker.finalize_highqal_outcome(**recovery_args)
            self.assertEqual(len(status.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(recovered["origin_lease_id"], "lease-1")
            self.assertRegex(str(recovered["asset_blend_sha256"]), r"^[0-9a-f]{64}$")
            self.assertRegex(str(recovered["media_sha256"]), r"^[0-9a-f]{64}$")
            self.assertEqual(complete.call_args.kwargs["lease_id"], "lease-2")
            receipt = json.loads(
                worker.highqal_finalization_receipt_path(
                    root / "logs",
                    workload_generation="c" * 64,
                    work_item_id="asset-1--0123456789abcdef",
                    lease_id="lease-2",
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["phase"], "completed")
            self.assertEqual(receipt["reconcile_lease_ids"], ["lease-1", "lease-2"])

    def test_finalization_receipt_precedes_publish_and_replays_after_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            (evidence / "six_views").mkdir(parents=True)
            (evidence / "asset.blend").write_bytes(b"b" * 2048)
            (evidence / "six_views/iso.png").write_bytes(b"p" * 10001)
            (evidence / "render_review.json").write_text(
                json.dumps({"asset_id": "asset-1"}), encoding="utf-8"
            )
            kwargs = {
                "row": {
                    "asset_id": "asset-1",
                    "identity_key": "identity-1",
                    "output_key": "asset-1",
                    "model_sha256": "a" * 64,
                    "reference_sha256": "b" * 64,
                },
                "outcome": {
                    "asset_id": "asset-1",
                    "status": "accepted",
                    "render_route": "static",
                    "output_dir": str(evidence),
                },
                "final_root": root / "final",
                "evidence_root": root / "logs",
                "status_path": root / "status.jsonl",
                "lease_db": root / "leases.sqlite3",
                "lease_id": "lease-1",
                "worker_index": 0,
                "node_boot_id": "boot-a",
                "workload_generation": "d" * 64,
                "work_item_id": "asset-1--fedcba9876543210",
            }
            with mock.patch.object(worker.shutil, "copy2", side_effect=OSError("disk")):
                with self.assertRaisesRegex(OSError, "disk"):
                    worker.finalize_highqal_outcome(**kwargs)
            receipt_path = worker.highqal_finalization_receipt_path(
                root / "logs",
                workload_generation="d" * 64,
                work_item_id="asset-1--fedcba9876543210",
                lease_id="lease-1",
            )
            prepared_receipt = json.loads(
                receipt_path.read_text(encoding="utf-8")
            )
            self.assertEqual(prepared_receipt["phase"], "prepared")
            self.assertFalse(prepared_receipt["outcome"]["published"])
            self.assertFalse((root / "status.jsonl").exists())
            with mock.patch.object(
                worker, "complete_highqal_work_item", return_value={"state": "completed"}
            ):
                recovered = worker.finalize_highqal_outcome(**kwargs)
            self.assertTrue(recovered["published"])
            self.assertTrue(Path(str(recovered["final_dir"])).is_dir())

    def test_legacy_prepared_receipt_is_normalized_before_publish_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            (evidence / "six_views").mkdir(parents=True)
            (evidence / "asset.blend").write_bytes(b"b" * 2048)
            (evidence / "six_views/iso.png").write_bytes(b"p" * 10001)
            (evidence / "render_review.json").write_text(
                json.dumps({"asset_id": "asset-1"}), encoding="utf-8"
            )
            row = {
                "asset_id": "asset-1",
                "identity_key": "identity-1",
                "work_item_id": "asset-1--0123456789abcdef",
                "output_key": "asset-1",
                "model_sha256": "a" * 64,
                "reference_sha256": "b" * 64,
            }
            generation = "d" * 64
            outcome = {
                **row,
                "status": "accepted",
                "render_route": "static",
                "output_dir": str(evidence),
                "workload_kind": worker.HIGHQAL_SOURCE_WORKLOAD_KIND,
                "workload_generation": generation,
                "lease_id": "lease-1",
                "origin_lease_id": "lease-1",
                "reference_video": "/tmp/reference.mp4",
                "updated_at": "2026-07-22 20:00:00",
            }
            outcome.update(
                worker.highqal_delivery_attestation(
                    row=row, review=outcome, final_root=root / "final"
                )
            )
            outcome["published"] = True
            receipt_path = worker.highqal_finalization_receipt_path(
                evidence,
                workload_generation=generation,
                work_item_id=row["work_item_id"],
                lease_id="lease-1",
            )
            worker.atomic_write_runtime_state(receipt_path, {
                "schema": worker.HIGHQAL_FINALIZATION_SCHEMA,
                "phase": "prepared",
                "origin_lease_id": "lease-1",
                "reconcile_lease_ids": ["lease-1"],
                "workload_kind": worker.HIGHQAL_SOURCE_WORKLOAD_KIND,
                "workload_generation": generation,
                "work_item_id": row["work_item_id"],
                "asset_id": row["asset_id"],
                "identity_key": row["identity_key"],
                "model_sha256": row["model_sha256"],
                "reference_sha256": row["reference_sha256"],
                "outcome": outcome,
                "outcome_sha256": worker._highqal_outcome_sha256(outcome),
                "updated_at": "2026-07-22 20:00:00",
            })
            with mock.patch.object(
                worker,
                "publish_highqal_minimal",
                side_effect=RuntimeError("publish remains blocked"),
            ):
                with self.assertRaisesRegex(RuntimeError, "publish remains blocked"):
                    worker.finalize_highqal_outcome(
                        row=row,
                        outcome=outcome,
                        final_root=root / "final",
                        evidence_root=evidence,
                        status_path=root / "status.jsonl",
                        lease_db=root / "leases.sqlite3",
                        lease_id="lease-1",
                        worker_index=0,
                        node_boot_id="boot-a",
                        workload_generation=generation,
                        work_item_id=row["work_item_id"],
                    )
            normalized = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(normalized["phase"], "prepared")
            self.assertFalse(normalized["outcome"]["published"])
            self.assertFalse((root / "status.jsonl").exists())

    def test_published_hash_conflict_is_not_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            (evidence / "six_views").mkdir(parents=True)
            (evidence / "asset.blend").write_bytes(b"b" * 2048)
            (evidence / "six_views/iso.png").write_bytes(b"p" * 10001)
            destination = root / "final/static/asset-1"
            (destination / "six_views").mkdir(parents=True)
            (destination / "asset.blend").write_bytes(b"x" * 2048)
            (destination / "six_views/iso.png").write_bytes(b"y" * 10001)
            with self.assertRaisesRegex(RuntimeError, "different bytes"):
                worker.publish_highqal_minimal(
                    row={"output_key": "asset-1"},
                    review={
                        "status": "accepted",
                        "render_route": "static",
                        "output_dir": str(evidence),
                    },
                    final_root=root / "final",
                )

    def test_finalization_replays_after_status_append_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "asset.blend").write_bytes(b"b" * 2048)
            (evidence / "render_review.json").write_text(
                json.dumps({"asset_id": "asset-2"}), encoding="utf-8"
            )
            kwargs = {
                "row": {
                    "asset_id": "asset-2",
                    "identity_key": "identity-2",
                    "output_key": "asset-2",
                    "model_sha256": "a" * 64,
                    "reference_sha256": "b" * 64,
                },
                "outcome": {
                    "asset_id": "asset-2",
                    "status": "needs_review",
                    "render_route": "static",
                    "output_dir": str(evidence),
                },
                "final_root": root / "final",
                "evidence_root": root / "logs",
                "status_path": root / "status.jsonl",
                "lease_db": root / "leases.sqlite3",
                "lease_id": "lease-a",
                "worker_index": 0,
                "node_boot_id": "boot-a",
                "workload_generation": "e" * 64,
                "work_item_id": "asset-2--0123456789abcdef",
            }
            real_append = worker.append_highqal_status_idempotent
            with mock.patch.object(
                worker,
                "append_highqal_status_idempotent",
                side_effect=OSError("append unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "append unavailable"):
                    worker.finalize_highqal_outcome(**kwargs)
            self.assertFalse((root / "status.jsonl").exists())
            with (
                mock.patch.object(
                    worker,
                    "append_highqal_status_idempotent",
                    side_effect=real_append,
                ),
                mock.patch.object(
                    worker,
                    "complete_highqal_work_item",
                    return_value={"state": "completed"},
                ),
            ):
                worker.finalize_highqal_outcome(**kwargs)
            self.assertEqual(len((root / "status.jsonl").read_text().splitlines()), 1)

    def test_needs_review_is_never_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "only accepted"):
                worker.publish_highqal_minimal(
                    row={"output_key": "asset-1"},
                    review={
                        "status": "needs_review",
                        "render_route": "static",
                        "output_dir": temporary,
                    },
                    final_root=Path(temporary) / "final",
                )
            self.assertFalse((Path(temporary) / "final/static/asset-1").exists())


if __name__ == "__main__":
    unittest.main()
