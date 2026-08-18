from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import batch_bilibili_resource_model_render as task1
import nvidia_graphics_runtime as graphics_runtime
from run_total_asset_render_worker import RemoteCommandError


class Task1PinnedGraphicsRuntimeTests(unittest.TestCase):
    def test_launcher_uses_only_the_pinned_runtime_root(self) -> None:
        source = (
            SCRIPTS / "run_bili_project_files_task1.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("TOTAL_ASSET_NVIDIA_GRAPHICS_RUNTIME_ROOT", source)
        self.assertIn("env -u REMOTE_NVIDIA_GL_ROOT", source)
        self.assertNotIn("BILI_PROJECT_REMOTE_NVIDIA_GL_ROOT", source)
        self.assertNotIn("/wuminghao/share/nvidia-570.211.01", source)
        self.assertIn("BILIBILI_RESOURCE_MANAGE_HOLDER_ON_EXIT", source)
        self.assertIn(":-0", source)

    def test_blender_environment_uses_shared_validator_and_strict_exports(self) -> None:
        root = "/root/.local/share/video2blender/test-pinned-runtime/runtime"
        gpu_uuid = "GPU-12345678-abcd-4321-abcd-0123456789ab"
        with mock.patch.object(
            task1,
            "task1_nvidia_graphics_runtime_root",
            return_value=root,
        ), mock.patch.object(
            task1,
            "resolved_task1_gpu_uuid",
            return_value=gpu_uuid,
        ):
            command = task1.remote_blender_env("EEVEE")

        self.assertIn(graphics_runtime.RUNTIME_INVALID_MARKER, command)
        self.assertIn(graphics_runtime.RUNTIME_MANIFEST_DIGEST, command)
        self.assertIn(f"export LD_LIBRARY_PATH={root}", command)
        self.assertIn(f"export VK_DRIVER_FILES={root}/nvidia_icd.json", command)
        self.assertIn(
            f"export __EGL_VENDOR_LIBRARY_FILENAMES={root}/10_nvidia.json",
            command,
        )
        self.assertIn("export CUDA_DEVICE_ORDER=PCI_BUS_ID", command)
        self.assertIn(f"export CUDA_VISIBLE_DEVICES={gpu_uuid}", command)
        self.assertNotIn("export CUDA_VISIBLE_DEVICES=0", command)
        self.assertNotIn("${LD_LIBRARY_PATH", command)
        self.assertNotIn("|| true", command)
        self.assertNotIn("/wuminghao/share/nvidia-570.211.01", command)

    def test_remote_validator_exit_78_becomes_capability_block(self) -> None:
        transport = mock.Mock()
        transport.run.side_effect = RemoteCommandError(
            task1.TASK1_RUNTIME_UNAVAILABLE_EXIT_CODE,
            stderr=(
                f"{graphics_runtime.RUNTIME_INVALID_MARKER} "
                "code=manifest_digest_untrusted"
            ),
        )
        with mock.patch.object(task1, "secure_remote", return_value=transport):
            with self.assertRaises(task1.Task1RuntimeUnavailable) as caught:
                task1.remote_run("blender -b", timeout=31, gpu_guard=True)

        self.assertEqual(caught.exception.failure_code, "manifest_digest_untrusted")
        wrapped = transport.run.call_args.args[0]
        self.assertIn("/tmp/total_asset_gpu_0.lock", wrapped)

    def test_unmarked_remote_exit_78_is_not_reclassified(self) -> None:
        transport = mock.Mock()
        original = RemoteCommandError(
            task1.TASK1_RUNTIME_UNAVAILABLE_EXIT_CODE,
            stderr="unrelated application exit",
        )
        transport.run.side_effect = original
        with mock.patch.object(task1, "secure_remote", return_value=transport):
            with self.assertRaises(RemoteCommandError) as caught:
                task1.remote_run("other-command", timeout=31)

        self.assertIs(caught.exception, original)

    def test_ready_marker_is_required_before_task_processing(self) -> None:
        with mock.patch.object(
            task1,
            "task1_nvidia_graphics_runtime_root",
            return_value="/trusted/runtime",
        ), mock.patch.object(task1, "remote_run", return_value="unexpected output"):
            with self.assertRaises(task1.Task1RuntimeUnavailable) as caught:
                task1.ensure_remote_nvidia_graphics_runtime()

        self.assertEqual(caught.exception.failure_code, "ready_marker_missing")

    def test_gpu_configuration_accepts_only_canonical_gpu0(self) -> None:
        self.assertEqual(task1.task1_gpu_index("0"), 0)
        invalid_format = ("", "0,1", "GPU-deadbeef", "-1", "+1", "01", " 0")
        for invalid in invalid_format:
            with self.subTest(value=invalid), self.assertRaises(
                task1.Task1RuntimeUnavailable
            ) as caught:
                task1.task1_gpu_index(invalid)
            self.assertEqual(
                caught.exception.failure_code,
                "task1_gpu_configuration_invalid",
            )
        for unsupported in ("1", "2", "12"):
            with self.subTest(value=unsupported), self.assertRaises(
                task1.Task1RuntimeUnavailable
            ) as caught:
                task1.task1_gpu_index(unsupported)
            self.assertEqual(caught.exception.failure_code, "task1_gpu0_required")

    def test_runtime_preflight_resolves_exact_index_to_one_physical_uuid(self) -> None:
        gpu_uuid = "GPU-12345678-ABCD-4321-ABCD-0123456789AB"
        with mock.patch.object(
            task1,
            "REMOTE_CUDA_VISIBLE_DEVICES",
            "0",
        ), mock.patch.object(
            task1,
            "task1_nvidia_graphics_runtime_root",
            return_value="/trusted/runtime",
        ), mock.patch.object(
            task1,
            "remote_run",
            side_effect=[
                graphics_runtime.RUNTIME_READY_MARKER,
                f"0, {gpu_uuid}\n",
            ],
        ) as remote_run:
            task1.ensure_remote_nvidia_graphics_runtime()
            observed = task1.resolved_task1_gpu_uuid()

        self.assertEqual(
            observed,
            "GPU-12345678-abcd-4321-abcd-0123456789ab",
        )
        probe = remote_run.call_args_list[1].args[0]
        self.assertIn(graphics_runtime.NVIDIA_SMI_BINARY, probe)
        self.assertIn("--query-gpu=index,uuid", probe)
        self.assertIn("-i 0", probe)

    def test_gpu_uuid_probe_rejects_wrong_index_duplicate_or_bad_uuid(self) -> None:
        valid = "GPU-12345678-abcd-4321-abcd-0123456789ab"
        cases = (
            (f"1, {valid}\n", "task1_gpu_index_mismatch"),
            (f"0, {valid}\n0, {valid}\n", "task1_gpu_uuid_inventory_invalid"),
            ("0, not-a-uuid\n", "task1_gpu_uuid_invalid"),
        )
        for output, code in cases:
            with self.subTest(code=code), self.assertRaises(
                task1.Task1RuntimeUnavailable
            ) as caught:
                task1.parse_task1_gpu_uuid_row(output, 0)
            self.assertEqual(caught.exception.failure_code, code)

    def test_invalid_gpu_configuration_blocks_before_main_or_asset_status(self) -> None:
        for configured in ("0,1", "2"):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime_state = root / "runtime.json"
                instance_lock = root / "task1.lock"
                with mock.patch.object(
                    task1,
                    "REMOTE_CUDA_VISIBLE_DEVICES",
                    configured,
                ), mock.patch.object(
                    task1,
                    "TASK1_RUNTIME_STATE",
                    runtime_state,
                ), mock.patch.object(
                    task1,
                    "TASK1_INSTANCE_LOCK",
                    instance_lock,
                ), mock.patch.object(task1, "main") as main, mock.patch.object(
                    task1,
                    "append_state",
                ) as append_state:
                    returncode = task1.run_cli()

                self.assertEqual(
                    returncode,
                    task1.TASK1_RUNTIME_UNAVAILABLE_EXIT_CODE,
                )
                payload = json.loads(runtime_state.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "blocked_runtime_unavailable")
                self.assertEqual(payload["gpu"], None)
                self.assertEqual(payload["gpu_uuid"], "")
                main.assert_not_called()
                append_state.assert_not_called()

    def test_runtime_block_writes_only_task_state_and_returns_78(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_state = root / "runtime.json"
            instance_lock = root / "task1.lock"
            with mock.patch.object(
                task1,
                "TASK1_RUNTIME_STATE",
                runtime_state,
            ), mock.patch.object(
                task1,
                "TASK1_INSTANCE_LOCK",
                instance_lock,
            ), mock.patch.object(
                task1,
                "main",
                side_effect=task1.Task1RuntimeUnavailable(
                    "driver_version_mismatch"
                ),
            ), mock.patch.object(task1, "append_state") as append_state:
                returncode = task1.run_cli()

            self.assertEqual(
                returncode,
                task1.TASK1_RUNTIME_UNAVAILABLE_EXIT_CODE,
            )
            payload = json.loads(runtime_state.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "blocked_runtime_unavailable")
            self.assertEqual(payload["state"], "blocked_runtime_unavailable")
            self.assertIn("driver_version_mismatch", payload["error"])
            append_state.assert_not_called()

    def test_mid_asset_runtime_block_bypasses_asset_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_dir = root / "asset"
            asset_dir.mkdir()
            model = asset_dir / "asset.blend"
            model.write_bytes(b"BLENDER-v500")
            row = {
                "asset_id": "asset-1",
                "分类": "model",
                "标题": "fixture",
                "素材链接": "",
                "是否可自动下载": "yes",
                "priority_score": "1",
            }
            with mock.patch.multiple(
                task1,
                ROOT=root,
                OUT_ROOT=root / "renders",
                STATE_CSV=root / "state.csv",
                DOWNLOAD_ONLY=False,
                DEDUP_SOURCE_URL=False,
                MANAGE_REMOTE_HOLDER_ON_EXIT=False,
            ), mock.patch.object(
                task1,
                "read_rows",
                return_value=[row],
            ), mock.patch.object(
                task1,
                "ensure_remote_nvidia_graphics_runtime",
            ), mock.patch.object(
                task1,
                "load_state",
                return_value={},
            ), mock.patch.object(
                task1,
                "enrich_state_with_failure_policy",
            ), mock.patch.object(
                task1.dl,
                "refresh_login",
            ), mock.patch.object(
                task1.dl,
                "ensure_remote_temp",
            ), mock.patch.object(
                task1,
                "download_asset",
                return_value=asset_dir,
            ), mock.patch.object(
                task1,
                "extract_archives",
            ), mock.patch.object(
                task1,
                "directory_size_mb",
                return_value=1.0,
            ), mock.patch.object(
                task1,
                "existing_models",
                return_value=[model],
            ), mock.patch.object(
                task1,
                "prioritize_models",
                return_value=[model],
            ), mock.patch.object(
                task1,
                "render_model_with_self_repair",
                side_effect=task1.Task1RuntimeUnavailable(
                    "runtime_file_hash_mismatch"
                ),
            ), mock.patch.object(task1, "append_state") as append_state:
                with self.assertRaises(task1.Task1RuntimeUnavailable):
                    task1.main()

            append_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
