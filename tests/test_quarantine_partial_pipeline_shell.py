from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
PIPELINE = PROJECT / "blender/scripts/run_total_asset_pipeline.sh"


def shell_function_source(name: str) -> str:
    shell = PIPELINE.read_text(encoding="utf-8")
    start = shell.index(f"{name}() {{")
    end = shell.index("\n}\n", start) + len("\n}\n")
    return shell[start:end]


class QuarantinePartialPipelineShellTests(unittest.TestCase):
    def test_layout_defaults_to_30808_and_accepts_only_explicit_30422_slots(self) -> None:
        functions = "\n".join(
            (
                shell_function_source("normalized_quarantine_secondary_gpu_ids"),
                shell_function_source("quarantine_partial_layout_json"),
            )
        )
        base = (
            "BATCH_NAME=batch0001\n"
            "GLOBAL_WORKER_COUNT=11\n"
            "SECONDARY_PORT=30422\n"
            "TERTIARY_PORT=30808\n"
        )
        command = (
            functions
            + "\n"
            + base
            + 'gpus="$(normalized_quarantine_secondary_gpu_ids)" || exit $?\n'
            + 'quarantine_partial_layout_json "$gpus" "${INCLUDE_TERTIARY:-1}"\n'
        )

        default = subprocess.run(
            ["bash", "-c", command],
            env={
                key: value
                for key, value in os.environ.items()
                if key != "TOTAL_ASSET_QUARANTINE_SECONDARY_GPU_IDS"
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(
            json.loads(default.stdout)["workers"],
            [
                {"port": 30808, "gpu": 0, "worker_index": 8},
                {"port": 30808, "gpu": 1, "worker_index": 9},
                {"port": 30808, "gpu": 2, "worker_index": 10},
            ],
        )

        secondary = subprocess.run(
            ["bash", "-c", command],
            env={
                **os.environ,
                "TOTAL_ASSET_QUARANTINE_SECONDARY_GPU_IDS": "3,1",
                "INCLUDE_TERTIARY": "0",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(secondary.returncode, 0, secondary.stderr)
        self.assertEqual(
            json.loads(secondary.stdout)["workers"],
            [
                {"port": 30422, "gpu": 1, "worker_index": 1},
                {"port": 30422, "gpu": 3, "worker_index": 3},
            ],
        )

        for invalid in ("0,0", "0,4", "0, 1", "all"):
            blocked = subprocess.run(
                ["bash", "-c", command],
                env={
                    **os.environ,
                    "TOTAL_ASSET_QUARANTINE_SECONDARY_GPU_IDS": invalid,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 75, invalid)

        empty = subprocess.run(
            ["bash", "-c", command],
            env={
                **os.environ,
                "TOTAL_ASSET_QUARANTINE_SECONDARY_GPU_IDS": "",
                "INCLUDE_TERTIARY": "0",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(empty.returncode, 75)

    def test_active_slot_requires_exact_quarantine_and_resume_flags(self) -> None:
        function = shell_function_source("quarantine_worker_process_attested")
        base_command = (
            function
            + "\nBATCH_NAME=batch0001\n"
            + "quarantine_worker_process_attested 30808 0 8 11\n"
        )

        def run_with_worker(extra_args: str) -> subprocess.CompletedProcess[str]:
            with tempfile.TemporaryDirectory() as temp:
                fake_ps = Path(temp) / "ps"
                worker = (
                    "123 1 python3 /usr/bin/python3 "
                    "blender/scripts/run_total_asset_render_worker.py "
                    "--queue /tmp/catalog.csv --batch batch0001 --batch-index 1 "
                    "--batch-size 1000 --remote-port 30808 --gpu 0 "
                    "--worker-index 8 --worker-count 11 "
                    f"{extra_args}\n"
                )
                fake_ps.write_text(
                    "#!/usr/bin/env bash\n"
                    + "printf '%s\\n' \"$FAKE_PS_OUTPUT\"\n",
                    encoding="utf-8",
                )
                fake_ps.chmod(fake_ps.stat().st_mode | stat.S_IXUSR)
                return subprocess.run(
                    ["bash", "-c", base_command],
                    cwd=PROJECT,
                    env={
                        **os.environ,
                        "TOTAL_ASSET_PS_BIN": str(fake_ps),
                        "FAKE_PS_OUTPUT": worker.rstrip("\n"),
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )

        valid = run_with_worker(
            "--exclude-worker-partition 7/8 --drain-file /tmp/wc11.drain.json "
            "--resume-unprocessed-only"
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        for invalid in (
            "--drain-file /tmp/wc11.drain.json --resume-unprocessed-only",
            "--exclude-worker-partition 6/8 --resume-unprocessed-only",
            "--exclude-worker-partition 7/8",
            "--exclude-worker-partition 7/8 --exclude-worker-partition 7/8 "
            "--resume-unprocessed-only",
        ):
            blocked = run_with_worker(invalid)
            self.assertEqual(blocked.returncode, 75, invalid)

    def test_orchestration_is_scoped_transactional_and_renews_attestation(self) -> None:
        launcher = shell_function_source("start_quarantine_partial_batch")
        scoped_preflight = shell_function_source(
            "generate_quarantine_partial_remote_preflight"
        )
        scoped_release = shell_function_source("release_quarantine_partial_holders")
        layout = shell_function_source("quarantine_partial_layout_json")

        self.assertIn('"$BATCH_NAME" != "batch0001"', launcher)
        self.assertIn('"$GLOBAL_WORKER_COUNT" != "11"', launcher)
        self.assertIn('"$BATCH_SIZE" != "1000"', launcher)
        self.assertIn("--exclude-worker-partition 7/8", launcher)
        self.assertIn("--resume-unprocessed-only", launcher)
        self.assertIn("quarantine_worker_slot_state", launcher)
        self.assertIn("quarantine_worker_process_attested", launcher)
        self.assertIn("ensure_gpu_finalizer", launcher)
        self.assertIn("mark_holder_slot_started", launcher)
        self.assertIn("cleanup_failed_worker_launch", launcher)
        self.assertIn("begin_holder_transaction", launcher)
        self.assertIn("commit_holder_transaction", launcher)
        self.assertGreaterEqual(launcher.count("attest_quarantined_legacy_worker"), 2)
        self.assertEqual(
            launcher.count("generate_quarantine_partial_remote_preflight"), 1
        )
        runtime_position = min(
            launcher.index("secondary_remote_preflight"),
            launcher.index("tertiary_remote_preflight"),
        )
        self.assertLess(
            runtime_position,
            launcher.index("generate_quarantine_partial_remote_preflight"),
        )
        self.assertNotIn("barrier_preflight", launcher)
        self.assertNotIn("local_barrier_precheck", launcher)
        self.assertNotIn("verify_and_release_task1_legacy_holder", launcher)
        self.assertLess(
            launcher.index("ensure_secondary_holder_audit_runtime"),
            launcher.index("begin_holder_transaction"),
        )

        self.assertIn("--scope-launch-ports", scoped_preflight)
        self.assertIn('QUARANTINE_PARTIAL_PREFLIGHT_REPORT', scoped_preflight)
        self.assertIn('release', scoped_release)
        self.assertIn('--launch-layout "$layout"', scoped_release)
        for source in (launcher, scoped_preflight, scoped_release, layout):
            self.assertNotIn('REMOTE_GPU_SSH_PORT="$PORT"', source)
            self.assertNotIn('--port "$PORT"', source)
        holder_runtime = shell_function_source(
            "ensure_secondary_holder_audit_runtime"
        )
        self.assertIn("command -v tmux", holder_runtime)
        self.assertNotIn("apt-get", holder_runtime)
        self.assertIn('REMOTE_GPU_SSH_PORT="$SECONDARY_PORT"', holder_runtime)
        self.assertNotIn("30773", holder_runtime)

        pipeline_source = PIPELINE.read_text(encoding="utf-8")
        for mutation in (
            "apt-get",
            "dnf ",
            "yum ",
            "ldconfig ",
            "ln -sfn /usr",
        ):
            self.assertNotIn(mutation, pipeline_source)

    def test_command_is_launch_guarded_dispatched_and_visible_in_usage(self) -> None:
        shell = PIPELINE.read_text(encoding="utf-8")
        guard = shell[shell.index('case "$COMMAND" in') : shell.index("require_mount()")]
        dispatch = shell[shell.rindex('case "$COMMAND" in') :]
        self.assertIn("quarantine-partial-batch", guard)
        self.assertIn(
            "quarantine-partial-batch)\n    start_quarantine_partial_batch",
            dispatch,
        )
        self.assertIn("quarantine-partial-batch|continuous", dispatch)

    def test_tertiary_runtime_smoke_can_target_only_inactive_selected_gpus(self) -> None:
        source = shell_function_source("tertiary_remote_preflight")
        self.assertIn("TOTAL_ASSET_TERTIARY_GPU_IDS", source)
        self.assertIn("bootstrap = module.Remote(port, gpus[0])", source)
        self.assertIn("for gpu in gpus:", source)
        self.assertNotIn("for gpu in range(3):", source)

    def test_embedded_worker_imports_register_module_before_dataclasses(self) -> None:
        shell = PIPELINE.read_text(encoding="utf-8")
        self.assertEqual(shell.count("module_from_spec(spec)"), 4)
        self.assertEqual(shell.count("sys.modules[spec.name] = module"), 4)
        for source in (
            shell_function_source("secondary_remote_preflight"),
            shell_function_source("ensure_secondary_holder_audit_runtime"),
            shell_function_source("tertiary_remote_preflight"),
        ):
            self.assertLess(
                source.index("sys.modules[spec.name] = module"),
                source.index("spec.loader.exec_module(module)"),
            )


if __name__ == "__main__":
    unittest.main()
