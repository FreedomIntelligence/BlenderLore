from __future__ import annotations

import csv
from dataclasses import asdict
import fcntl
import io
import json
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

import batch_bilibili_resource_model_render as task1
import gpu_runtime_guard as guard
import task1_gpu_handoff as handoff
import total_asset_scheduler as scheduler
import total_asset_remote_preflight as remote_preflight


def write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "asset_id", "identity_key", "render_order", "render_batch",
        "inventory_status", "duplicate_of", "model_file",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def catalog_row(asset_id: str, order: int, batch: int) -> dict[str, str]:
    return {
        "asset_id": asset_id,
        "identity_key": f"identity-{asset_id}",
        "render_order": str(order),
        "render_batch": f"{batch:04d}",
        "inventory_status": "ready",
        "duplicate_of": "",
        "model_file": f"/assets/{asset_id}.blend",
    }


def append_status(path: Path, **values: object) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(values) + "\n")


class GpuGuardTests(unittest.TestCase):
    def test_secondary_role_can_move_to_31722_without_changing_worker_indices(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SCRIPTS)
        env["TOTAL_ASSET_SECONDARY_PORT"] = "31722"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; import total_asset_topology as t; "
                    "print(json.dumps({str(k): v for k, v in "
                    "t.CANONICAL_WORKER_INDEX_BY_LOCATION.items()}))"
                ),
            ],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        mapping = json.loads(result.stdout)
        self.assertEqual(mapping["(31722, 0)"], 0)
        self.assertEqual(mapping["(31722, 3)"], 3)
        self.assertNotIn("(30422, 0)", mapping)

    def test_lock_wrapper_has_stable_busy_contract(self) -> None:
        command = guard.wrap_remote_gpu_command(
            "blender -b scene.blend", 2, owner="unit test", wait_seconds=0
        )
        self.assertIn("/tmp/total_asset_gpu_2.lock", command)
        self.assertIn(guard.GPU_LOCK_BUSY_MARKER, command)
        self.assertIn("exit 75", command)
        self.assertEqual(guard.GPU_LOCK_BUSY_EXIT, 75)
        self.assertIs(guard.GpuLockBusy, guard.GpuBusyError)

    def test_holders_are_per_gpu_and_hold_the_shared_lock(self) -> None:
        gpu0 = guard.holder_start_command(0, hold_mem_mb=512, hold_touch_mb=128, hold_sleep_sec=1)
        gpu1 = guard.holder_start_command(1, hold_mem_mb=512, hold_touch_mb=128, hold_sleep_sec=1)
        self.assertIn("total_asset_gpu_holder_g0", gpu0)
        self.assertIn("total_asset_gpu_0.lock", gpu0)
        self.assertNotIn("total_asset_gpu_holder_g1", gpu0)
        self.assertIn("total_asset_gpu_holder_g1", gpu1)
        self.assertIn("total_asset_gpu_1.lock", gpu1)
        self.assertIn("--query-gpu=uuid", gpu0)
        self.assertIn("CUDA_DEVICE_ORDER=PCI_BUS_ID", gpu0)
        self.assertIn('CUDA_VISIBLE_DEVICES="$gpu_uuid"', gpu0)
        self.assertNotIn("CUDA_VISIBLE_DEVICES=0", gpu0)
        self.assertIn(guard.HOLDER_GPU_IDENTITY_ERROR_MARKER, gpu0)
        stop = guard.holder_stop_command(0)
        self.assertIn("total_asset_gpu_holder_g0", stop)
        self.assertNotIn("pkill", stop)

    def test_physical_gpu_uuid_resolution_is_strict_and_fails_closed(self) -> None:
        assignment = guard._physical_gpu_uuid_assignment(2, exit_code=45)
        script = (
            "nvidia-smi() { printf '%s' \"$FAKE_NVIDIA_OUTPUT\"; }; "
            + assignment
            + "printf '%s\\n' \"$gpu_uuid\""
        )
        valid_uuid = "GPU-12345678-abcd-4321-abcd-0123456789ab"
        cases = (
            (valid_uuid + "\n", 0),
            ("", 45),
            ("not-a-gpu-uuid\n", 45),
            ("GPU-a-b\n", 45),
            (valid_uuid + "\n" + valid_uuid + "\n", 45),
        )
        for fake_output, expected_code in cases:
            with self.subTest(fake_output=fake_output, expected_code=expected_code):
                env = os.environ.copy()
                env["FAKE_NVIDIA_OUTPUT"] = fake_output
                result = subprocess.run(
                    ["bash", "-c", script],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_code)
                if expected_code == 0:
                    self.assertEqual(result.stdout.strip(), valid_uuid)
                else:
                    self.assertIn(
                        guard.HOLDER_GPU_IDENTITY_ERROR_MARKER,
                        result.stderr,
                    )

    def test_holder_readiness_requires_session_memory_compute_and_busy_lock(self) -> None:
        command = guard.holder_readiness_command(2, minimum_memory_mb=3840)
        self.assertIn("total_asset_gpu_holder_g2", command)
        self.assertIn("--query-gpu=uuid,memory.used", command)
        self.assertIn('-i "$gpu_uuid"', command)
        self.assertIn('"${used:-0}" -ge 3840', command)
        self.assertIn("--query-compute-apps=gpu_uuid,pid", command)
        self.assertIn('uuid == target', command)
        self.assertIn("tmux list-panes", command)
        self.assertIn("list-panes -s -t =total_asset_gpu_holder_g2", command)
        self.assertIn("#{pane_pid}:#{pane_dead}", command)
        self.assertIn("/proc/$cursor/status", command)
        self.assertIn("holder_descendant", command)
        self.assertIn("/proc/$holder_pid/cmdline", command)
        self.assertIn("/proc/$holder_pid/environ", command)
        self.assertIn("CUDA_DEVICE_ORDER", command)
        self.assertIn("HOLD_GPU_IDS", command)
        self.assertIn("/proc/$holder_pid/fd/9", command)
        self.assertIn("/proc/$holder_pid/fdinfo/9", command)
        self.assertIn("FLOCK[[:space:]]+ADVISORY", command)
        self.assertIn("stat -Lc '%d:%i'", command)
        self.assertIn("holder_command_ok=1", command)
        self.assertIn("holder_lock_owner_ok=1", command)
        self.assertIn("physical_uuid=$gpu_uuid", command)
        self.assertIn(guard.HOLDER_GPU_IDENTITY_ERROR_MARKER, command)
        self.assertIn("/tmp/total_asset_gpu_2.lock", command)
        self.assertIn("if flock -n 8; then exit 44", command)
        self.assertIn(guard.HOLDER_READY_MARKER, command)

    def test_holder_start_is_single_attempt_and_polls_until_cuda_is_ready(self) -> None:
        config = guard.HolderConfig(2, 4096, 1024, 0.25)
        start_command = config.start_command()
        calls: list[tuple[str, int]] = []
        readiness_attempts = 0

        def runner(command: str, timeout: int) -> str:
            nonlocal readiness_attempts
            calls.append((command, timeout))
            if command == start_command:
                return ""
            if guard.HOLDER_READY_MARKER in command:
                readiness_attempts += 1
                if readiness_attempts < 3:
                    raise RuntimeError("CUDA context still initializing")
                return f"{guard.HOLDER_READY_MARKER} gpu=2 held_mb=4096"
            return ""

        with mock.patch.object(guard.time, "sleep") as sleep:
            output = guard.start_and_verify_holder(
                runner,
                config,
                timeout_seconds=180,
                poll_seconds=10,
            )
        self.assertIn(guard.HOLDER_READY_MARKER, output)
        self.assertEqual(sum(command == start_command for command, _ in calls), 1)
        self.assertEqual(readiness_attempts, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(any("tmux kill-session" in command for command, _ in calls))

    def test_holder_timeout_cleans_only_its_gpu_session(self) -> None:
        config = guard.HolderConfig(1, 4096, 1024, 0.25)
        start_command = config.start_command()
        calls: list[tuple[str, int]] = []

        def runner(command: str, timeout: int) -> str:
            calls.append((command, timeout))
            if command == start_command:
                return ""
            if guard.HOLDER_READY_MARKER in command:
                raise RuntimeError("holder not ready")
            return ""

        with self.assertRaisesRegex(RuntimeError, "timed out"):
            guard.start_and_verify_holder(
                runner,
                config,
                timeout_seconds=0,
                poll_seconds=10,
            )
        self.assertEqual(sum(command == start_command for command, _ in calls), 1)
        cleanup_commands = [command for command, _ in calls if "tmux kill-session" in command]
        self.assertEqual(len(cleanup_commands), 1)
        self.assertIn("total_asset_gpu_holder_g1", cleanup_commands[0])
        self.assertNotIn("total_asset_gpu_holder_g0", cleanup_commands[0])

    def test_busy_lock_returns_exit_75_without_running_command(self) -> None:
        gpu = 99123
        lock_path = Path(guard.gpu_lock_path(gpu))
        executed = Path(tempfile.gettempdir()) / "gpu_guard_should_not_run"
        executed.unlink(missing_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                guard.wrap_remote_gpu_command(f"touch {executed}", gpu, owner="test"),
                shell=True,
                text=True,
                capture_output=True,
                check=False,
            )
        lock_path.unlink(missing_ok=True)
        self.assertEqual(result.returncode, 75)
        self.assertIn(guard.GPU_LOCK_BUSY_MARKER, result.stderr)
        self.assertFalse(executed.exists())

    def test_task1_runtime_state_is_atomic_and_uses_required_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime.json"
            with mock.patch.object(task1, "TASK1_RUNTIME_STATE", path):
                task1.write_runtime_state("running", started_at="2026-01-01 00:00:00")
                task1.write_runtime_state("blocked_gpu_busy", error="busy")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "blocked_gpu_busy")
            self.assertEqual(payload["state"], "blocked_gpu_busy")
            self.assertEqual(payload["schema_version"], 1)
            self.assertTrue(payload["run_id"])
            self.assertEqual(payload["started_at"], "2026-01-01 00:00:00")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_task1_local_instance_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "task1.lock"
            first = task1.acquire_task1_instance_lock(path)
            try:
                with self.assertRaises(task1.Task1InstanceBusy):
                    task1.acquire_task1_instance_lock(path)
            finally:
                fcntl.flock(first.fileno(), fcntl.LOCK_UN)
                first.close()


class Task1HandoffTests(unittest.TestCase):
    def make_files(self, root: Path, rows: int, natural: bool = True) -> tuple[Path, Path, Path]:
        runtime = root / "runtime.json"
        runtime.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
        log = root / "task1.log"
        log.write_text("output /render\n" if natural else "processing last\n", encoding="utf-8")
        state = root / "state.csv"
        with state.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["asset_id", "status"])
            writer.writeheader()
            for index in range(rows):
                writer.writerow({"asset_id": str(index), "status": "rendered"})
        return runtime, log, state

    def test_handoff_requires_natural_marker_and_status_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime, log, state = self.make_files(Path(temp), 2, natural=True)
            with mock.patch.object(handoff, "task1_process_active", return_value=False):
                complete = handoff.observe_task1(runtime, log, state, required_status_rows=2)
                blocked = handoff.observe_task1(runtime, log, state, required_status_rows=3)
            self.assertEqual(complete.state, "complete")
            self.assertEqual(blocked.state, "blocked")

    def test_process_disappearance_without_marker_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime, log, state = self.make_files(Path(temp), 2, natural=False)
            with mock.patch.object(handoff, "task1_process_active", return_value=False):
                observation = handoff.observe_task1(runtime, log, state, required_status_rows=2)
            self.assertEqual(observation.state, "blocked")
            self.assertIn("natural", observation.reason)

    def test_old_marker_does_not_complete_a_new_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, log, state = self.make_files(root, 2, natural=True)
            runtime.write_text("{}", encoding="utf-8")
            baseline = handoff.capture_handoff_baseline(
                runtime, log, process_active=True
            )
            with mock.patch.object(handoff, "task1_process_active", return_value=False):
                blocked = handoff.observe_task1(
                    runtime,
                    log,
                    state,
                    required_status_rows=2,
                    baseline=baseline,
                )
            self.assertEqual(blocked.state, "blocked")
            self.assertIn("natural", blocked.reason)

            with log.open("a", encoding="utf-8") as handle:
                handle.write("output /new-run\n")
            with mock.patch.object(handoff, "task1_process_active", return_value=False):
                complete = handoff.observe_task1(
                    runtime,
                    log,
                    state,
                    required_status_rows=2,
                    baseline=baseline,
                )
            self.assertEqual(complete.state, "complete")

    def test_durable_precompleted_task_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime, log, state = self.make_files(root, 2, natural=True)
            ended = time.time()
            runtime.write_text(
                json.dumps({
                    "status": "complete",
                    "pid": 12345,
                    "started_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(ended - 60)
                    ),
                    "updated_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(ended)
                    ),
                    "ended_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(ended)
                    ),
                }),
                encoding="utf-8",
            )
            baseline = handoff.capture_handoff_baseline(
                runtime, log, process_active=False
            )
            with mock.patch.object(handoff, "task1_process_active", return_value=False):
                observation = handoff.observe_task1(
                    runtime,
                    log,
                    state,
                    required_status_rows=2,
                    baseline=baseline,
                )
            self.assertEqual(observation.state, "complete")

    def test_handoff_command_is_not_mistaken_for_running_worker(self) -> None:
        self_pid = os.getpid()
        handoff_command = (
            f"{self_pid} python task1_gpu_handoff.py --batch batch0001 -- "
            "python run_total_asset_render_worker.py --batch batch0001 "
            "--remote-port 30773 --gpu 0"
        )
        fake_result = mock.Mock(stdout=handoff_command + "\n")
        with mock.patch.object(handoff.subprocess, "run", return_value=fake_result):
            self.assertFalse(handoff.worker_already_running("batch0001", 0, 30773))

        external = (
            "999999 python run_total_asset_render_worker.py --batch batch0001 "
            "--remote-port 30773 --gpu 0\n"
        )
        with mock.patch.object(
            handoff.subprocess, "run", return_value=mock.Mock(stdout=external)
        ):
            self.assertTrue(handoff.worker_already_running("batch0001", 0, 30773))


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inventory = self.root / "inventory"
        self.inventory.mkdir()
        self.catalog = self.inventory / "total_asset_catalog.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def remote_preflight(
        self,
        target_batch: str,
        *,
        worker_count: int = 11,
        observed_at_epoch: float | None = None,
        claims: tuple[scheduler.WorkerClaim, ...] = (),
    ) -> dict[str, object]:
        return {
            "schema_version": scheduler.REMOTE_PREFLIGHT_SCHEMA_VERSION,
            "target_batch": target_batch,
            "worker_count": worker_count,
            "ready": True,
            "error_codes": [],
            "local_claims_digest": scheduler.worker_claims_digest(claims),
            "local_controllers_digest": scheduler.screen_controllers_digest(()),
            "launch_layout": [
                {
                    "port": port,
                    "gpu": gpu,
                    "worker_index": worker_index,
                }
                for (port, gpu), worker_index in sorted(
                    scheduler.CANONICAL_WORKER_INDEX_BY_LOCATION.items(),
                    key=lambda item: item[1],
                )
            ],
            "observed_at_epoch": (
                time.time() if observed_at_epoch is None else observed_at_epoch
            ),
            "nodes": [
                {
                    "port": port,
                    "reachable": True,
                    "gpus": [
                        {
                            "gpu": gpu,
                            "gpu_ok": True,
                            "holder_ok": True,
                            "lock_ok": True,
                        }
                        for gpu in gpus
                    ],
                }
                for port, gpus in scheduler.REQUIRED_REMOTE_GPU_LAYOUT.items()
            ],
        }

    def test_lowest_incomplete_is_derived_and_wrong_batch_is_rejected(self) -> None:
        rows = [
            catalog_row("a", 1, 0), catalog_row("b", 2, 0),
            catalog_row("c", 3, 1), catalog_row("d", 4, 1),
        ]
        write_catalog(self.catalog, rows)
        path = self.inventory / "total_asset_render_status_formal.jsonl"
        append_status(path, asset_id="a", batch="batch0000", status="accepted", updated_at="1")
        append_status(path, asset_id="b", batch="batch0000_quality_audit", status="needs_review", updated_at="2")
        append_status(path, asset_id="c", batch="batch0000", status="accepted", updated_at="3")
        append_status(path, asset_id="c", batch="batch0001", status="accepted", updated_at="4")
        path.write_text(path.read_text() + "{bad json\n", encoding="utf-8")
        plan = scheduler.derive_plan(
            self.catalog, self.inventory, batch_size=2, active_batches=set()
        )
        self.assertEqual(plan.lowest_incomplete_batch, "batch0001")
        self.assertEqual(plan.action, "launch_lowest_incomplete")
        self.assertEqual(plan.malformed_status_lines, 1)
        self.assertEqual(plan.batches[1].counts, {"accepted": 1, "pending": 1})

    def test_transient_gpu_busy_does_not_reduce_scheduler_terminal_count(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        path = self.inventory / "total_asset_render_status_formal.jsonl"
        append_status(
            path,
            asset_id="a",
            batch="batch0000",
            status="failed",
            updated_at="2026-07-16 10:00:00",
        )
        append_status(
            path,
            asset_id="a",
            batch="batch0000_retry",
            status="blocked_gpu_busy",
            updated_at="2026-07-17 10:00:00",
        )
        plan = scheduler.derive_plan(
            self.catalog,
            self.inventory,
            batch_size=1,
            active_batches=set(),
        )
        self.assertEqual(plan.batches[0].terminal, 1)
        self.assertEqual(plan.batches[0].counts, {"failed": 1})

    def test_deferred_is_terminal_for_formal_progress_and_remains_repair_work(self) -> None:
        rows = [
            catalog_row("a", 1, 0), catalog_row("b", 2, 0),
            catalog_row("c", 3, 1), catalog_row("d", 4, 1),
        ]
        write_catalog(self.catalog, rows)
        path = self.inventory / "total_asset_render_status_formal.jsonl"
        append_status(path, asset_id="a", batch="batch0000", status="accepted")
        append_status(path, asset_id="b", batch="batch0000", status="deferred")
        append_status(path, asset_id="c", batch="batch0001", status="accepted")
        append_status(path, asset_id="d", batch="batch0001", status="accepted")

        plan = scheduler.derive_plan(
            self.catalog,
            self.inventory,
            batch_size=2,
            active_batches=set(),
        )

        first, second = plan.batches
        self.assertEqual(first.counts, {"accepted": 1, "deferred": 1})
        self.assertEqual(sum(first.counts.values()), first.expected)
        self.assertNotIn("pending", first.counts)
        self.assertEqual(first.terminal, 2)
        self.assertEqual(first.unresolved, 0)
        self.assertEqual(first.repair_unresolved, 1)
        self.assertTrue(first.complete)
        self.assertTrue(second.complete)
        self.assertIsNone(plan.lowest_incomplete_batch)
        self.assertEqual(plan.action, "idle_all_complete")
        self.assertEqual(plan.to_dict()["batches"][0]["unresolved"], 0)
        self.assertEqual(plan.to_dict()["batches"][0]["repair_unresolved"], 1)

    def test_pending_and_deferred_are_distinct_without_double_counting(self) -> None:
        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 2, 0)],
        )
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a",
            batch="batch0000",
            status="deferred",
        )

        plan = scheduler.derive_plan(
            self.catalog,
            self.inventory,
            batch_size=2,
            active_batches=set(),
        )
        batch = plan.batches[0]
        self.assertEqual(batch.counts, {"deferred": 1, "pending": 1})
        self.assertEqual(sum(batch.counts.values()), batch.expected)
        self.assertEqual(batch.terminal, 1)
        self.assertEqual(batch.unresolved, 1)
        self.assertEqual(batch.repair_unresolved, 1)
        self.assertFalse(batch.complete)

    def test_slot_gate_blocks_only_statusless_rows_in_same_previous_partition(self) -> None:
        write_catalog(
            self.catalog,
            [
                catalog_row("a", 1, 0),
                catalog_row("b", 2, 0),
                catalog_row("c", 3, 0),
                catalog_row("d", 4, 1),
                catalog_row("e", 5, 1),
                catalog_row("f", 6, 1),
            ],
        )
        location = next(
            location
            for location, index in scheduler.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if index == 0
        )
        blocked = scheduler.slot_launch_gate(
            self.catalog,
            self.inventory,
            batch_size=3,
            target_batch="batch0001",
            remote_port=location[0],
            gpu=location[1],
            worker_index=0,
        )
        self.assertFalse(blocked.ready)
        self.assertEqual(blocked.reason, "previous_partition_statusless")
        self.assertEqual(blocked.previous_statusless, 1)

        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a",
            batch="batch0000",
            status="deferred",
        )
        ready = scheduler.slot_launch_gate(
            self.catalog,
            self.inventory,
            batch_size=3,
            target_batch="batch0001",
            remote_port=location[0],
            gpu=location[1],
            worker_index=0,
        )
        self.assertTrue(ready.ready)
        self.assertEqual(ready.reason, "ready")
        self.assertEqual(ready.previous_statusless, 0)
        self.assertEqual(ready.target_statusless, 1)

    def test_slot_gate_rejects_location_worker_index_mismatch(self) -> None:
        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 2, 1)],
        )
        with self.assertRaisesRegex(
            scheduler.CatalogError, "does not own"
        ):
            scheduler.slot_launch_gate(
                self.catalog,
                self.inventory,
                batch_size=1,
                target_batch="batch0001",
                remote_port=30773,
                gpu=0,
                worker_index=5,
            )

    def test_mixed_eight_and_eleven_shards_are_blocked_on_overlap(self) -> None:
        claims = scheduler.parse_worker_claims(
            "\n".join((
                "101 1 Python python3 run_total_asset_render_worker.py --batch batch0001 "
                "--remote-port 30773 --gpu 1 --worker-index 5 --worker-count 8",
                "102 1 python3 python3 run_total_asset_render_worker.py --batch batch0001 "
                "--remote-port 30808 --gpu 0 --worker-index 8 --worker-count 11",
            ))
        )
        errors = scheduler.validate_worker_topology(
            claims,
            expected_by_batch={"batch0001": 1000},
            desired_worker_count=11,
        )
        self.assertTrue(any("worker-count mismatch" in error for error in errors))
        self.assertTrue(any("overlapping partitions" in error for error in errors))

    def test_screen_monitor_embedded_worker_text_is_not_a_process_claim(self) -> None:
        output = (
            "201 1 SCREEN SCREEN -dmS monitor bash -lc while ps | rg "
            "'[r]un_total_asset_render_worker.py.*"
            "--batch batch0001'; do sleep 60; done; "
            "python run_total_asset_render_worker.py --batch batch0001_deferred "
            "--remote-port 30773 --gpu 0 --worker-index 0 --worker-count 1; "
            "python run_total_asset_render_worker.py --batch batch0001_failed_retry "
            "--remote-port 30773 --gpu 0 --worker-index 0 --worker-count 1"
        )
        claims = scheduler.parse_worker_claims(output)
        self.assertEqual(claims, ())

    def test_login_and_bash_wrappers_do_not_duplicate_real_python_worker(self) -> None:
        worker = (
            "run_total_asset_render_worker.py --batch batch0001 "
            "--remote-port 30773 --gpu 1 --worker-index 5 --worker-count 11"
        )
        output = "\n".join((
            f"301 1 login login -fp user /bin/bash -lc 'python3 {worker}'",
            f"302 301 bash /bin/bash -lc 'exec python3 {worker}'",
            f"303 302 Python python3 {worker}",
            f"304 302 Python python3 -c 'print(1)' {worker}",
        ))
        claims = scheduler.parse_worker_claims(output)
        self.assertEqual(
            claims,
            (scheduler.WorkerClaim("batch0001", 30773, 1, 5, 11),),
        )

    def test_macos_truncated_comm_still_detects_real_python_worker(self) -> None:
        output = (
            "77929 1 /Library/Framewo "
            "/Library/Frameworks/Python.framework/Versions/3.11/Resources/"
            "Python.app/Contents/MacOS/Python "
            "blender/scripts/run_total_asset_render_worker.py "
            "--batch batch0001 --remote-port 30773 --gpu 3 "
            "--worker-index 7 --worker-count 8"
        )
        self.assertEqual(
            scheduler.parse_worker_claims(output),
            (scheduler.WorkerClaim("batch0001", 30773, 3, 7, 8),),
        )

    def test_macos_unquoted_application_support_path_is_worker_claim(self) -> None:
        output = (
            "11750 11749 /Library/Framewo "
            "/Library/Frameworks/Python.framework/Versions/3.10/Resources/"
            "Python.app/Contents/MacOS/Python "
            "/Users/user/Library/Application Support/Video2Blender/cycle72/"
            "runtime/" + "d" * 64 + "/blender/scripts/"
            "run_total_asset_render_worker.py "
            "--batch batch0003 --remote-port 30808 --gpu 0 "
            "--worker-index 8 --worker-count 11"
        )
        self.assertEqual(
            scheduler.parse_worker_claims(output),
            (scheduler.WorkerClaim("batch0003", 30808, 0, 8, 11),),
        )

    def test_python_c_with_spaced_worker_text_is_not_worker_claim(self) -> None:
        output = (
            "11750 11749 Python python3 -c 'print(1)' "
            "/Users/user/Library/Application Support/Video2Blender/"
            "run_total_asset_render_worker.py "
            "--batch batch0003 --remote-port 30808 --gpu 0 "
            "--worker-index 8 --worker-count 11"
        )
        self.assertEqual(scheduler.parse_worker_claims(output), ())

    def test_plan_exposes_live_mac_worker_and_eight_way_topology_mismatch(self) -> None:
        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 2, 1)],
        )
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a",
            batch="batch0000",
            status="accepted",
        )
        claim = scheduler.WorkerClaim("batch0001", 30773, 3, 7, 8)
        controller = scheduler.ScreenController(
            "total_asset_batch0001_g3_retry", "batch0001", "retry", 30773, 3
        )
        with mock.patch.object(
            scheduler, "detect_worker_claims", return_value=(claim,)
        ), mock.patch.object(
            scheduler, "detect_screen_controllers", return_value=(controller,)
        ):
            plan = scheduler.derive_plan(
                self.catalog, self.inventory, batch_size=1
            )
        self.assertEqual(plan.worker_claims, (claim,))
        self.assertEqual(plan.action, "blocked_worker_topology")
        self.assertTrue(any(
            "worker-count mismatch" in item for item in plan.topology_errors
        ))

    def test_retry_and_handoff_screen_controllers_are_not_lost_in_sleep_window(self) -> None:
        output = "\n".join((
            "77910.total_asset_batch0001_g3_retry (Detached)",
            "77911.total_asset_batch0001_g0_handoff (Detached)",
            "77912.total_asset_batch0001_30808_monitor (Detached)",
            "77913.total_asset_batch0001_deferred_vulkan_repair_30422_g1 (Detached)",
        ))
        controllers = scheduler.parse_screen_controllers(output)
        self.assertEqual(
            {(item.kind, item.remote_port, item.gpu) for item in controllers},
            {
                ("retry", 30773, 3),
                ("handoff", 30773, 0),
                ("monitor", 30808, None),
                ("worker", 30422, 1),
            },
        )
        repair = next(item for item in controllers if item.session.endswith("30422_g1"))
        self.assertEqual(repair.batch, "batch0001_deferred_vulkan_repair")
        errors = scheduler.validate_screen_controllers(controllers, ())
        self.assertEqual(len(errors), 4)

    def test_screen_controller_requires_compatible_exact_claim(self) -> None:
        controller = scheduler.ScreenController(
            "total_asset_batch0001_g3_retry", "batch0001", "retry", 30773, 3
        )
        old_claim = scheduler.WorkerClaim("batch0001", 30773, 3, 7, 8)
        new_claim = scheduler.WorkerClaim("batch0001", 30773, 3, 7, 11)
        self.assertTrue(
            scheduler.validate_screen_controllers((controller,), (old_claim,))
        )
        self.assertEqual(
            scheduler.validate_screen_controllers((controller,), (new_claim,)),
            (),
        )

    def test_guarded_failed_repair_group_is_not_misclassified_as_orphan(self) -> None:
        port = scheduler.SECONDARY_REMOTE_PORT
        output = (
            "77920.total_asset_batch0001_failed_repair_"
            f"course_timeout_exact_{port}_g0 (Detached)\n"
        )
        controllers = scheduler.parse_screen_controllers(output)
        self.assertEqual(len(controllers), 1)
        self.assertEqual(
            controllers[0].batch,
            "batch0001_failed_repair_course_timeout_exact",
        )
        claim = scheduler.WorkerClaim(
            "batch0001_failed_repair_course_timeout_exact",
            port,
            0,
            0,
            11,
        )
        self.assertEqual(
            scheduler.validate_screen_controllers(
                controllers,
                (claim,),
                require_claim_controller=True,
            ),
            (),
        )
        self.assertEqual(
            scheduler.validate_worker_topology(
                (claim,),
                expected_by_batch={"batch0001": 1000},
                desired_worker_count=11,
            ),
            (),
        )
        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 2, 1)],
        )
        with mock.patch.object(
            scheduler, "detect_worker_claims", return_value=(claim,)
        ), mock.patch.object(
            scheduler, "detect_screen_controllers", return_value=controllers
        ):
            plan = scheduler.derive_plan(
                self.catalog,
                self.inventory,
                batch_size=1,
                desired_worker_count=11,
            )
        self.assertEqual(plan.topology_errors, ())
        self.assertNotEqual(plan.action, "blocked_worker_topology")

    def test_unknown_wrong_slot_and_duplicate_managed_repairs_still_block(self) -> None:
        port = scheduler.SECONDARY_REMOTE_PORT
        unknown = scheduler.WorkerClaim(
            "batch0001_failed_repair_unreviewed_group", port, 0, 0, 11
        )
        wrong_slot = scheduler.WorkerClaim(
            "batch0001_failed_repair_transport", 30773, 0, 3, 11
        )
        duplicate = scheduler.WorkerClaim(
            "batch0001_failed_repair_transport", port, 0, 0, 11
        )
        errors = scheduler.validate_worker_topology(
            (unknown, wrong_slot, duplicate, duplicate),
            expected_by_batch={"batch0001": 1000},
            desired_worker_count=11,
        )
        self.assertTrue(any("unreviewed_group" in error for error in errors))
        self.assertTrue(any("port=30773" in error for error in errors))
        self.assertTrue(any("duplicate managed repair claim" in error for error in errors))
        self.assertTrue(any("duplicate managed repair group claim" in error for error in errors))

        unknown_controller = scheduler.ScreenController(
            f"total_asset_{unknown.batch}_{port}_g0",
            unknown.batch,
            "worker",
            port,
            0,
        )
        self.assertTrue(any(
            "orphan or incompatible" in error
            for error in scheduler.validate_screen_controllers(
                (unknown_controller,), (unknown,)
            )
        ))

        duplicate_controllers = (
            scheduler.ScreenController(
                f"total_asset_{duplicate.batch}_{port}_g0",
                duplicate.batch,
                "worker",
                port,
                0,
            ),
            scheduler.ScreenController(
                f"total_asset_{duplicate.batch}_{port}_g0_retry",
                duplicate.batch,
                "retry",
                port,
                0,
            ),
        )
        self.assertTrue(any(
            "duplicate managed repair screen controllers" in error
            for error in scheduler.validate_screen_controllers(
                duplicate_controllers, (duplicate,)
            )
        ))

    def test_reviewed_failed_repair_can_use_any_canonical_slot_but_group_is_singleton(
        self,
    ) -> None:
        locations = sorted(
            scheduler.CANONICAL_WORKER_INDEX_BY_LOCATION.items(),
            key=lambda item: item[1],
        )
        (port_a, gpu_a), index_a = locations[7]
        (port_b, gpu_b), index_b = locations[9]
        first = scheduler.WorkerClaim(
            "batch0001_failed_repair_transport",
            port_a,
            gpu_a,
            index_a,
            11,
        )
        second = scheduler.WorkerClaim(
            "batch0001_failed_repair_transport",
            port_b,
            gpu_b,
            index_b,
            11,
        )
        self.assertEqual(
            scheduler.validate_worker_topology(
                (first,),
                expected_by_batch={"batch0001": 1000},
                desired_worker_count=11,
            ),
            (),
        )
        errors = scheduler.validate_worker_topology(
            (first, second),
            expected_by_batch={"batch0001": 1000},
            desired_worker_count=11,
        )
        self.assertTrue(any(
            "duplicate managed repair group claim" in error for error in errors
        ))

    def test_generic_batch0_failed_repair_is_canonical_singleton_and_manual_blocks(
        self,
    ) -> None:
        locations = sorted(
            scheduler.CANONICAL_WORKER_INDEX_BY_LOCATION.items(),
            key=lambda item: item[1],
        )
        (port_a, gpu_a), index_a = locations[3]
        (port_b, gpu_b), index_b = locations[10]
        first = scheduler.WorkerClaim(
            "batch0000_failed_repair_runtime_api_compat",
            port_a,
            gpu_a,
            index_a,
            11,
        )
        second = scheduler.WorkerClaim(
            "batch0000_failed_repair_runtime_api_compat",
            port_b,
            gpu_b,
            index_b,
            11,
        )
        self.assertEqual(
            scheduler.validate_worker_topology(
                (first,),
                expected_by_batch={"batch0000": 1000},
                desired_worker_count=11,
            ),
            (),
        )
        self.assertTrue(any(
            "duplicate managed repair group claim" in error
            for error in scheduler.validate_worker_topology(
                (first, second),
                expected_by_batch={"batch0000": 1000},
                desired_worker_count=11,
            )
        ))
        manual = scheduler.WorkerClaim(
            "batch0000_failed_repair_timeout_scene_audit",
            port_a,
            gpu_a,
            index_a,
            11,
        )
        self.assertTrue(any(
            "unknown or incompatible managed repair worker" in error
            for error in scheduler.validate_worker_topology(
                (manual,),
                expected_by_batch={"batch0000": 1000},
                desired_worker_count=11,
            )
        ))

    def test_live_v5_repair_names_and_secondary_busy_lock_are_qa_activity(self) -> None:
        claims = (
            scheduler.WorkerClaim(
                "batch0000_repair_v5_failed", 30422, 0, 0, 1
            ),
            scheduler.WorkerClaim(
                "batch0001_quality_audit", 30773, 2, 0, 1
            ),
            scheduler.WorkerClaim(
                "pilot1000_gpu0_failed_retry", 30422, 1, 0, 1
            ),
            scheduler.WorkerClaim(
                "quality_repair", 30422, 2, 0, 1
            ),
        )
        sessions = (
            "total_asset_batch0000_repair_v5_g0",
            "total_asset_batch0001_quality_audit_monitor",
            "total_asset_pilot1000_gpu0_failed_retry",
            "total_asset_quality_repair_g2",
        )
        report = {
            "nodes": [{
                "port": 30422,
                "probe_ok": True,
                "holder_audit_ok": True,
                "unexpected_holder_sessions": 0,
                "legacy_holder_sessions": 0,
                "gpus": [
                    {
                        "gpu": 0, "lock_state": "busy",
                        "gpu_query_ok": True, "compute_query_ok": True,
                        "compute_process_count": 1,
                        "compute_process_kinds": ["holder"],
                        "holder_session_present": True,
                        "holder_descendant_ok": True,
                        "holder_lock_owner_ok": True,
                        "holder_command_ok": True,
                        "wrapper_process_present": False,
                    },
                    {
                        "gpu": 1, "lock_state": "available",
                        "gpu_query_ok": True, "compute_query_ok": True,
                        "compute_process_count": 0,
                        "compute_process_kinds": [],
                        "holder_session_present": False,
                        "holder_descendant_ok": False,
                        "holder_lock_owner_ok": False,
                        "holder_command_ok": False,
                        "wrapper_process_present": False,
                    },
                    {
                        "gpu": 2, "lock_state": "busy",
                        "gpu_query_ok": True, "compute_query_ok": True,
                        "compute_process_count": 1,
                        "compute_process_kinds": ["blender"],
                        "holder_session_present": False,
                        "holder_descendant_ok": False,
                        "holder_lock_owner_ok": False,
                        "holder_command_ok": False,
                        "wrapper_process_present": True,
                    },
                    {
                        "gpu": 3, "lock_state": "available",
                        "gpu_query_ok": True, "compute_query_ok": True,
                        "compute_process_count": 0,
                        "compute_process_kinds": [],
                        "holder_session_present": False,
                        "holder_descendant_ok": False,
                        "holder_lock_owner_ok": False,
                        "holder_command_ok": False,
                        "wrapper_process_present": False,
                    },
                ],
            }],
        }
        activity = scheduler.classify_qa_activity(
            claims, sessions, remote_report=report
        )
        self.assertTrue(any("batch0000_repair_v5_failed" in item for item in activity))
        self.assertTrue(any("quality_audit" in item for item in activity))
        self.assertTrue(any("pilot1000" in item for item in activity))
        self.assertTrue(any("quality_repair" in item for item in activity))
        self.assertTrue(any("qa_or_unknown_activity" in item for item in activity))

    def test_secondary_remote_qa_audit_allows_only_idle_or_exact_holder(self) -> None:
        rows = []
        for gpu in range(4):
            rows.append({
                "gpu": gpu,
                "gpu_query_ok": True,
                "compute_query_ok": True,
                "lock_state": "busy" if gpu == 0 else "available",
                "compute_process_count": 1 if gpu == 0 else 0,
                "compute_process_kinds": ["holder"] if gpu == 0 else [],
                "holder_session_present": gpu == 0,
                "holder_descendant_ok": gpu == 0,
                "holder_lock_owner_ok": gpu == 0,
                "holder_command_ok": gpu == 0,
                "wrapper_process_present": False,
            })
        safe = scheduler.classify_qa_activity(
            (), (), remote_report={"nodes": [{
                "port": 30422,
                "probe_ok": True,
                "holder_audit_ok": True,
                "unexpected_holder_sessions": 0,
                "legacy_holder_sessions": 0,
                "gpus": rows,
            }]}
        )
        self.assertEqual(safe, ())

        for field in (
            "holder_descendant_ok",
            "holder_lock_owner_ok",
            "holder_command_ok",
        ):
            with self.subTest(holder_evidence_field=field):
                missing_rows = [dict(row) for row in rows]
                missing_rows[0].pop(field)
                self.assertTrue(scheduler.classify_qa_activity(
                    (), (), remote_report={"nodes": [{
                        "port": 30422,
                        "probe_ok": True,
                        "holder_audit_ok": True,
                        "unexpected_holder_sessions": 0,
                        "legacy_holder_sessions": 0,
                        "gpus": missing_rows,
                    }]},
                ))
                false_rows = [dict(row) for row in rows]
                false_rows[0][field] = False
                self.assertTrue(scheduler.classify_qa_activity(
                    (), (), remote_report={"nodes": [{
                        "port": 30422,
                        "probe_ok": True,
                        "holder_audit_ok": True,
                        "unexpected_holder_sessions": 0,
                        "legacy_holder_sessions": 0,
                        "gpus": false_rows,
                    }]},
                ))

        rows[1] = {
            **rows[1],
            "lock_state": "busy",
            "compute_process_count": 1,
            "compute_process_kinds": ["blender"],
        }
        blender = scheduler.classify_qa_activity(
            (), (), remote_report={"nodes": [{
                "port": 30422,
                "probe_ok": True,
                "holder_audit_ok": True,
                "unexpected_holder_sessions": 0,
                "legacy_holder_sessions": 0,
                "gpus": rows,
            }]}
        )
        self.assertTrue(any("qa_or_unknown_activity" in item for item in blender))

        malformed = scheduler.classify_qa_activity(
            (), (), remote_report={"nodes": [{
                "port": 30422,
                "gpus": [{"gpu": 0, "lock_state": "available"}],
            }]},
        )
        self.assertTrue(malformed)

    def test_secondary_remote_qa_audit_accepts_sync_node_observation_schema(self) -> None:
        node = remote_preflight.NodeObservation(
            port=30422,
            probe_ok=True,
            holder_audit_ok=True,
            unexpected_holder_sessions=0,
            legacy_holder_sessions=0,
            gpus=tuple(
                remote_preflight.GpuObservation(
                    gpu=gpu,
                    gpu_query_ok=True,
                    compute_query_ok=True,
                    compute_process_count=1 if gpu == 0 else 0,
                    compute_process_kinds=("holder",) if gpu == 0 else (),
                    holder_session=gpu == 0,
                    lock_state="busy" if gpu == 0 else "available",
                    wrapper_process_present=False,
                    holder_descendant_ok=gpu == 0,
                    holder_lock_owner_ok=gpu == 0,
                    holder_command_ok=gpu == 0,
                )
                for gpu in range(4)
            ),
        )
        self.assertEqual(
            scheduler.classify_qa_activity(
                (), (), remote_report={"nodes": [asdict(node)]}
            ),
            (),
        )
        broken = asdict(node)
        broken["gpus"][2]["compute_query_ok"] = False
        self.assertTrue(
            scheduler.classify_qa_activity(
                (), (), remote_report={"nodes": [broken]}
            )
        )
        broken_node = asdict(node)
        broken_node["holder_audit_ok"] = False
        self.assertTrue(
            scheduler.classify_qa_activity(
                (), (), remote_report={"nodes": [broken_node]}
            )
        )

    def test_repair_like_screen_session_parser_keeps_full_live_name(self) -> None:
        output = (
            "77929.total_asset_batch0000_repair_v5_failed_g3 (Detached)\n"
            "77930.total_asset_batch0000_quality_audit_monitor (Detached)\n"
        )
        self.assertEqual(
            scheduler.parse_screen_session_names(output),
            (
                "total_asset_batch0000_quality_audit_monitor",
                "total_asset_batch0000_repair_v5_failed_g3",
            ),
        )

    def test_dead_screen_sockets_are_not_live_controllers_or_sessions(self) -> None:
        output = (
            "77929.total_asset_batch0000_g3 (Dead ???)\n"
            "77930.total_asset_batch0000_g2 (Detached)\n"
        )
        self.assertEqual(
            scheduler.parse_screen_session_names(output),
            ("total_asset_batch0000_g2",),
        )
        controllers = scheduler.parse_screen_controllers(output)
        self.assertEqual(len(controllers), 1)
        self.assertEqual(
            controllers[0].session,
            "total_asset_batch0000_g2",
        )

    def test_production_claim_detection_uses_structured_ps_and_fails_closed(self) -> None:
        worker = (
            "501 1 Python python3 run_total_asset_render_worker.py "
            "--batch batch0001 --remote-port 30773 --gpu 0 "
            "--worker-index 4 --worker-count 11\n"
        )
        success = subprocess.CompletedProcess([], 0, worker, "")
        with mock.patch.object(
            scheduler.subprocess, "run", return_value=success
        ) as run:
            claims = scheduler.detect_worker_claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(
            run.call_args.args[0],
            ["ps", "-axo", "pid=,ppid=,comm=,args="],
        )
        failed = subprocess.CompletedProcess([], 2, "", "permission denied")
        with mock.patch.object(
            scheduler.subprocess, "run", return_value=failed
        ), self.assertRaisesRegex(scheduler.CatalogError, "process audit failed"):
            scheduler.detect_worker_claims()

    def test_screen_audit_accepts_only_explicit_no_socket_result(self) -> None:
        no_sockets = subprocess.CompletedProcess(
            [], 1, "No Sockets found in /tmp/screens.\n", ""
        )
        with mock.patch.object(
            scheduler.subprocess, "run", return_value=no_sockets
        ):
            self.assertEqual(scheduler.detect_active_batches(()), set())
        ambiguous_failure = subprocess.CompletedProcess(
            [], 1, "", "permission denied"
        )
        with mock.patch.object(
            scheduler.subprocess, "run", return_value=ambiguous_failure
        ), self.assertRaisesRegex(scheduler.CatalogError, "screen controller audit failed"):
            scheduler.detect_active_batches(())

    def test_duplicate_partition_claim_on_two_gpus_is_blocked(self) -> None:
        claims = (
            scheduler.WorkerClaim("batch0001", 30773, 0, 4, 11),
            scheduler.WorkerClaim("batch0001", 30808, 2, 4, 11),
        )
        errors = scheduler.validate_worker_topology(
            claims,
            expected_by_batch={"batch0001": 1000},
            desired_worker_count=11,
        )
        self.assertTrue(any("duplicate partition claim" in error for error in errors))

    def test_formal_11_worker_must_use_canonical_node_gpu_partition(self) -> None:
        claim = scheduler.WorkerClaim("batch0001", 39999, 9, 4, 11)
        errors = scheduler.validate_worker_topology(
            (claim,),
            expected_by_batch={"batch0001": 1000},
            desired_worker_count=11,
        )
        self.assertTrue(any("non-canonical worker location" in item for item in errors))

    def test_exact_duplicate_controller_is_not_silently_deduplicated(self) -> None:
        command = (
            "Python python3 run_total_asset_render_worker.py --batch batch0001 "
            "--remote-port 30773 --gpu 1 --worker-index 5 --worker-count 11"
        )
        claims = scheduler.parse_worker_claims(
            "401 1 " + command + "\n402 1 " + command
        )
        self.assertEqual(len(claims), 2)
        errors = scheduler.validate_worker_topology(
            claims,
            expected_by_batch={"batch0001": 1000},
            desired_worker_count=11,
        )
        self.assertTrue(any("duplicate controller claim" in error for error in errors))

    def test_out_of_order_primary_batch_never_launches_lower_batch(self) -> None:
        rows = [
            catalog_row("a", 1, 0), catalog_row("b", 2, 0),
            catalog_row("c", 3, 1), catalog_row("d", 4, 1),
            catalog_row("e", 5, 2), catalog_row("f", 6, 2),
        ]
        write_catalog(self.catalog, rows)
        status = self.inventory / "total_asset_render_status_formal.jsonl"
        append_status(status, asset_id="a", batch="batch0000", status="accepted")
        append_status(status, asset_id="b", batch="batch0000", status="accepted")
        claim = scheduler.WorkerClaim("batch0002", 30808, 0, 8, 11)
        plan = scheduler.derive_plan(
            self.catalog,
            self.inventory,
            batch_size=2,
            active_batches={"batch0002"},
            worker_claims=(claim,),
        )
        self.assertEqual(plan.lowest_incomplete_batch, "batch0001")
        self.assertEqual(plan.action, "blocked_out_of_order_activity")
        with mock.patch.object(scheduler, "run_command") as run:
            events = scheduler.run_iteration(
                plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=self.root / "events",
                batch_size=2,
                qa_activity=(),
            )
        launch_calls = [
            call for call in run.call_args_list
            if call.args and call.args[0] == ["bash", str(self.root / "pipeline.sh"), "batch"]
        ]
        self.assertEqual(launch_calls, [])
        self.assertNotIn("launch", {event["event"] for event in events})

    def test_barrier_is_read_only_and_requires_all_primary_workers_drained(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        before = {path.relative_to(self.inventory) for path in self.inventory.rglob("*")}
        claim = scheduler.WorkerClaim("batch0000", 30773, 0, 4, 11)
        blocked = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(claim,),
            active_batches={"batch0000"},
            remote_preflight_report=self.remote_preflight(
                "batch0000", claims=(claim,)
            ),
        )
        ready = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
        )
        after = {path.relative_to(self.inventory) for path in self.inventory.rglob("*")}
        self.assertFalse(blocked["ready"])
        self.assertTrue(ready["ready"])
        self.assertEqual(after, before)

    def test_barrier_blocks_orphaned_target_retry_but_allows_earlier_qa(self) -> None:
        rows = [
            catalog_row("a", 1, 0),
            catalog_row("b", 2, 1),
        ]
        write_catalog(self.catalog, rows)
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a",
            batch="batch0000",
            status="accepted",
        )
        target_retry = scheduler.WorkerClaim(
            "batch0001_failed_retry", 30773, 0, 0, 1
        )
        earlier_qa = scheduler.WorkerClaim(
            "batch0000_quality_repair", 30422, 0, 0, 4
        )
        blocked = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0001",
            desired_worker_count=11,
            worker_claims=(target_retry, earlier_qa),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight(
                "batch0001", claims=(target_retry, earlier_qa)
            ),
        )
        ready = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0001",
            desired_worker_count=11,
            worker_claims=(earlier_qa,),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight(
                "batch0001", claims=(earlier_qa,)
            ),
        )
        self.assertFalse(blocked["ready"])
        self.assertTrue(ready["ready"])

    def test_barrier_requires_existing_full_lowest_incomplete_target(self) -> None:
        rows = [
            catalog_row("a", 1, 0), catalog_row("b", 2, 0),
            catalog_row("c", 3, 1), catalog_row("d", 4, 1),
        ]
        write_catalog(self.catalog, rows)
        wrong = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=2,
            target_batch="batch0001",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0001"),
        )
        missing = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=2,
            target_batch="batch9999",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch9999"),
        )
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a", batch="batch0000", status="accepted",
        )
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="b", batch="batch0000", status="accepted",
        )
        correct = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=2,
            target_batch="batch0001",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0001"),
        )
        self.assertFalse(wrong["ready"])
        self.assertTrue(any("not the lowest incomplete" in item for item in wrong["blockers"]))
        self.assertFalse(missing["ready"])
        self.assertTrue(any("does not exist" in item for item in missing["blockers"]))
        self.assertTrue(correct["ready"])

    def test_barrier_rejects_partial_and_complete_target(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        partial = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=2,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
        )
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a", batch="batch0000", status="accepted",
        )
        complete = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
        )
        self.assertFalse(partial["ready"])
        self.assertTrue(any("not full" in item for item in partial["blockers"]))
        self.assertFalse(complete["ready"])
        self.assertTrue(any("already complete" in item for item in complete["blockers"]))

    def test_barrier_remote_preflight_is_required_fresh_and_complete(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        missing = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
        )
        stale_report = self.remote_preflight(
            "batch0000",
            observed_at_epoch=time.time() - scheduler.REMOTE_PREFLIGHT_MAX_AGE_SECONDS - 1,
        )
        stale = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=stale_report,
        )
        bad_report = self.remote_preflight("batch0000")
        bad_report["nodes"][0]["gpus"][0]["lock_ok"] = False  # type: ignore[index]
        bad = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=bad_report,
        )
        good = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
        )
        not_ready_report = self.remote_preflight("batch0000")
        not_ready_report["ready"] = False
        not_ready_report["error_codes"] = ["ssh_exit_nonzero"]
        not_ready = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=not_ready_report,
        )
        changed_claim = scheduler.WorkerClaim("batch0000", 30422, 0, 0, 11)
        changed_snapshot = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(changed_claim,),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
        )
        self.assertFalse(missing["ready"])
        self.assertFalse(stale["ready"])
        self.assertFalse(bad["ready"])
        self.assertFalse(not_ready["ready"])
        self.assertTrue(any(
            "top-level ready" in item for item in not_ready["blockers"]
        ))
        self.assertFalse(changed_snapshot["ready"])
        self.assertTrue(any(
            "claim snapshot has changed" in item
            for item in changed_snapshot["blockers"]
        ))
        self.assertTrue(good["ready"])

    def test_drain_request_is_atomic_idempotent_and_topology_scoped(self) -> None:
        first = scheduler.create_drain_request(self.inventory, "batch0001", 8)
        second = scheduler.create_drain_request(self.inventory, "batch0001", 8)
        old_path = scheduler.worker_drain_path(self.inventory, "batch0001", 8)
        new_path = scheduler.worker_drain_path(self.inventory, "batch0001", 11)
        self.assertEqual(first, second)
        self.assertTrue(old_path.is_file())
        self.assertFalse(new_path.exists())
        self.assertEqual(list(old_path.parent.glob("*.tmp")), [])
        self.assertTrue(scheduler.clear_drain_request(self.inventory, "batch0001", 8))
        self.assertFalse(old_path.exists())

    def test_legacy_wc8_drain_marker_is_readonly_compatible_and_blocks_plan(self) -> None:
        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 2, 1)],
        )
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a",
            batch="batch0000",
            status="accepted",
        )
        control = self.inventory / "worker_control"
        control.mkdir()
        path = control / "batch0001.wc8.drain.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "status": "drain_requested",
            "batch": "batch0001",
            "worker_count": 8,
            "requested_at": "2026-07-16 20:00:00",
            "requested_by_pid": 77929,
        }), encoding="utf-8")
        before = path.read_bytes()
        request = scheduler.load_drain_request_path(path)
        self.assertEqual(request["compatibility"], "legacy_v1_readonly")
        self.assertRegex(str(request["request_id"]), r"^[0-9a-f]{32}$")
        plan = scheduler.derive_plan(
            self.catalog,
            self.inventory,
            batch_size=1,
            active_batches=set(),
        )
        self.assertEqual(plan.action, "wait_drain_requested")
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(scheduler.clear_drain_request(
            self.inventory,
            "batch0001",
            8,
            expected_request_id=str(request["request_id"]),
        ))
        self.assertFalse(path.exists())

    def test_drain_compare_and_clear_preserves_replaced_generation(self) -> None:
        original = scheduler.create_drain_request(
            self.inventory, "batch0001", 11
        )
        path = scheduler.worker_drain_path(self.inventory, "batch0001", 11)
        replacement = dict(original)
        replacement["request_id"] = "f" * 32
        scheduler.atomic_write_json(path, replacement)
        with self.assertRaisesRegex(
            scheduler.CatalogError, "changed before compare-and-clear"
        ):
            scheduler.clear_drain_request(
                self.inventory,
                "batch0001",
                11,
                expected_request_id=str(original["request_id"]),
            )
        self.assertTrue(path.is_file())
        self.assertEqual(
            scheduler.load_drain_request_path(path)["request_id"], "f" * 32
        )

    def test_malformed_or_unreadable_drain_control_fails_closed(self) -> None:
        control = self.inventory / "worker_control"
        control.mkdir()
        malformed = control / "batch0001.wc11.drain.json"
        malformed.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(scheduler.CatalogError, "schema is incomplete"):
            scheduler.list_drain_requests(self.inventory)
        malformed.unlink()
        with mock.patch.object(
            Path, "glob", side_effect=PermissionError("private path")
        ), self.assertRaisesRegex(scheduler.CatalogError, "cannot audit drain"):
            scheduler.list_drain_requests(self.inventory)

    def test_resume_barrier_ignores_only_its_exact_drain_marker(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        exact = scheduler.worker_drain_path(self.inventory, "batch0000", 8)
        scheduler.create_drain_request(self.inventory, "batch0000", 8)
        ordinary = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
        )
        resume_exact = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
            ignored_drain_request=exact,
        )
        scheduler.create_drain_request(self.inventory, "batch0002", 11)
        resume_with_other_marker = scheduler.barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            target_batch="batch0000",
            desired_worker_count=11,
            worker_claims=(),
            active_batches=set(),
            remote_preflight_report=self.remote_preflight("batch0000"),
            ignored_drain_request=exact,
        )
        self.assertFalse(ordinary["ready"])
        self.assertTrue(any("drain request" in item for item in ordinary["blockers"]))
        self.assertTrue(resume_exact["ready"])
        self.assertFalse(resume_with_other_marker["ready"])
        self.assertTrue(any(
            "drain request" in item
            for item in resume_with_other_marker["blockers"]
        ))

    def test_resume_uses_11_barrier_to_clear_exact_legacy_wc8_marker(self) -> None:
        request = {"request_id": "a" * 32}
        argv = [
            "total_asset_scheduler.py",
            "resume",
            "--catalog", str(self.catalog),
            "--inventory", str(self.inventory),
            "--batch", "batch0001",
            "--worker-count", "11",
            "--drain-worker-count", "8",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            scheduler, "load_drain_request", return_value=request
        ) as load, mock.patch.object(
            scheduler, "barrier_report", return_value={"ready": True}
        ) as barrier, mock.patch.object(
            scheduler, "clear_drain_request", return_value=True
        ) as clear, mock.patch.object(
            scheduler, "list_drain_requests", return_value=()
        ), mock.patch.object(sys, "stdout", new=io.StringIO()):
            rc = scheduler.main()
        self.assertEqual(rc, 0)
        load.assert_called_once_with(self.inventory, "batch0001", 8)
        self.assertEqual(barrier.call_args.kwargs["desired_worker_count"], 11)
        self.assertFalse(barrier.call_args.kwargs["allow_compatible_active"])
        self.assertEqual(
            barrier.call_args.kwargs["ignored_drain_request"],
            scheduler.worker_drain_path(self.inventory, "batch0001", 8),
        )
        clear.assert_called_once_with(
            self.inventory,
            "batch0001",
            8,
            expected_request_id="a" * 32,
        )

    def test_resume_can_explicitly_allow_compatible_active_wc11_workers(self) -> None:
        request = {"request_id": "b" * 32}
        argv = [
            "total_asset_scheduler.py",
            "resume",
            "--catalog", str(self.catalog),
            "--inventory", str(self.inventory),
            "--batch", "batch0001",
            "--worker-count", "11",
            "--drain-worker-count", "8",
            "--allow-compatible-active",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            scheduler, "load_drain_request", return_value=request
        ), mock.patch.object(
            scheduler, "barrier_report", return_value={"ready": True}
        ) as barrier, mock.patch.object(
            scheduler, "clear_drain_request", return_value=True
        ), mock.patch.object(
            scheduler, "list_drain_requests", return_value=()
        ), mock.patch.object(sys, "stdout", new=io.StringIO()):
            rc = scheduler.main()
        self.assertEqual(rc, 0)
        self.assertTrue(barrier.call_args.kwargs["allow_compatible_active"])

    def test_resume_all_drains_derives_lowest_batch_and_clears_every_generation(
        self,
    ) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        first = scheduler.create_drain_request(
            self.inventory, "batch0000", 11
        )
        second = scheduler.create_drain_request(
            self.inventory, "batch0001", 8
        )
        report = self.remote_preflight("batch0000")
        report["scope"] = "launch_slots"
        preflight = self.root / "resume-all-preflight.json"
        preflight.write_text(json.dumps(report), encoding="utf-8")
        argv = [
            "total_asset_scheduler.py",
            "resume-all-drains",
            "--catalog", str(self.catalog),
            "--inventory", str(self.inventory),
            "--batch-size", "1",
            "--worker-count", "11",
            "--remote-preflight-json", str(preflight),
        ]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            scheduler, "detect_worker_claims", return_value=()
        ), mock.patch.object(
            scheduler, "_screen_listing", return_value="No Sockets found"
        ), mock.patch.object(sys, "stdout", new=stdout):
            rc = scheduler.main()
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "all_drain_generations_cleared")
        self.assertEqual(payload["target_batch"], "batch0000")
        self.assertEqual(len(payload["cleared"]), 2)
        self.assertFalse(scheduler.worker_drain_path(
            self.inventory, "batch0000", 11
        ).exists())
        self.assertFalse(scheduler.worker_drain_path(
            self.inventory, "batch0001", 8
        ).exists())
        self.assertRegex(str(first["request_id"]), r"^[0-9a-f]{32}$")
        self.assertRegex(str(second["request_id"]), r"^[0-9a-f]{32}$")

    def test_resume_all_blocks_target_claim_chain_and_malformed_status(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        scheduler.create_drain_request(self.inventory, "batch0000", 11)
        generations = scheduler.capture_drain_request_generations(self.inventory)
        claim = scheduler.WorkerClaim(
            "batch0000", scheduler.SECONDARY_REMOTE_PORT, 0, 0, 11
        )
        controller = scheduler.ScreenController(
            f"total_asset_batch0000_{scheduler.SECONDARY_REMOTE_PORT}_g0",
            "batch0000",
            "worker",
            scheduler.SECONDARY_REMOTE_PORT,
            0,
        )
        report = self.remote_preflight("batch0000", claims=(claim,))
        report["scope"] = "launch_slots"
        report["local_controllers_digest"] = scheduler.screen_controllers_digest(
            (controller,)
        )
        blocked = scheduler.resume_all_barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            desired_worker_count=11,
            drain_generations=generations,
            remote_preflight_report=report,
            worker_claims=(claim,),
            screen_controllers=(controller,),
            screen_sessions=(
                controller.session,
                "total_asset_slot_chain_p30773_g0_w4",
            ),
        )
        self.assertFalse(blocked["ready"])
        self.assertTrue(any(
            "target/later worker" in value for value in blocked["blockers"]
        ))
        self.assertTrue(any(
            "formal-chain" in value for value in blocked["blockers"]
        ))

        (self.inventory / "total_asset_render_status_bad.jsonl").write_text(
            "{bad json\n", encoding="utf-8"
        )
        clean_report = self.remote_preflight("batch0000")
        clean_report["scope"] = "launch_slots"
        malformed = scheduler.resume_all_barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            desired_worker_count=11,
            drain_generations=generations,
            remote_preflight_report=clean_report,
            worker_claims=(),
            screen_controllers=(),
            screen_sessions=(),
        )
        self.assertFalse(malformed["ready"])
        self.assertIn("malformed status lines=1", malformed["blockers"])
        self.assertTrue(generations[0].path.exists())

    def test_resume_all_cas_preserves_all_markers_when_snapshot_changed(self) -> None:
        scheduler.create_drain_request(self.inventory, "batch0000", 11)
        scheduler.create_drain_request(self.inventory, "batch0001", 8)
        snapshot = scheduler.capture_drain_request_generations(self.inventory)
        changed = snapshot[1]
        payload = scheduler.load_drain_request_path(changed.path)
        payload["request_id"] = "f" * 32
        scheduler.atomic_write_json(changed.path, payload)
        with self.assertRaisesRegex(
            scheduler.CatalogError, "changed before global clear"
        ):
            scheduler.clear_all_drain_requests_cas(self.inventory, snapshot)
        self.assertTrue(snapshot[0].path.exists())
        self.assertTrue(snapshot[1].path.exists())

    def test_resume_all_requires_fresh_launch_slots_preflight(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        generations = scheduler.capture_drain_request_generations(self.inventory)
        report = self.remote_preflight(
            "batch0000",
            observed_at_epoch=(
                time.time() - scheduler.REMOTE_PREFLIGHT_MAX_AGE_SECONDS - 1
            ),
        )
        report["scope"] = "full"
        blocked = scheduler.resume_all_barrier_report(
            self.catalog,
            self.inventory,
            batch_size=1,
            desired_worker_count=11,
            drain_generations=generations,
            remote_preflight_report=report,
            worker_claims=(),
            screen_controllers=(),
            screen_sessions=(),
        )
        self.assertFalse(blocked["ready"])
        self.assertIn(
            "remote preflight scope must be launch_slots",
            blocked["remote_preflight_errors"],
        )
        self.assertIn(
            "remote preflight is stale", blocked["remote_preflight_errors"]
        )

    def test_scheduler_does_not_restart_a_drained_partition(self) -> None:
        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 2, 1)],
        )
        append_status(
            self.inventory / "total_asset_render_status_formal.jsonl",
            asset_id="a", batch="batch0000", status="accepted",
        )
        scheduler.create_drain_request(self.inventory, "batch0001", 11)
        plan = scheduler.derive_plan(
            self.catalog,
            self.inventory,
            batch_size=1,
            active_batches=set(),
        )
        self.assertEqual(plan.action, "wait_drain_requested")
        with mock.patch.object(scheduler, "run_command") as run:
            events = scheduler.run_iteration(
                plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=self.root / "events",
                batch_size=1,
                qa_activity=(),
            )
        run.assert_not_called()
        self.assertEqual(events, [])

    def test_scheduler_marks_batch_launch_as_lock_owned(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        plan = scheduler.derive_plan(
            self.catalog,
            self.inventory,
            batch_size=1,
            active_batches=set(),
        )
        lock_path = self.inventory / "total_asset_scheduler.lock"
        with scheduler.SchedulerLock(lock_path) as held, mock.patch.object(
            scheduler, "run_command", return_value=0
        ) as run:
            assert held.handle is not None
            lock_fd = held.handle.fileno()
            events = scheduler.run_iteration(
                plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=self.root / "events",
                batch_size=1,
                qa_activity=(),
                scheduler_lock_fd=lock_fd,
            )
        self.assertEqual([event["event"] for event in events], ["launch"])
        launch = run.call_args_list[0]
        self.assertEqual(
            launch.kwargs["env"]["TOTAL_ASSET_SCHEDULER_LOCK_HELD"], "1"
        )
        self.assertEqual(
            launch.kwargs["env"]["TOTAL_ASSET_SCHEDULER_LOCK_FD"], str(lock_fd)
        )
        self.assertEqual(launch.kwargs["pass_fds"], (lock_fd,))

    def test_scheduler_child_reenters_same_inherited_lock(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        plan = scheduler.derive_plan(
            self.catalog, self.inventory, batch_size=1, active_batches=set()
        )
        pipeline = self.root / "lock-child.sh"
        scheduler_script = SCRIPTS / "total_asset_scheduler.py"
        pipeline.write_text(
            "#!/bin/bash\n"
            f"exec {sys.executable!s} {scheduler_script!s} drain "
            f"--catalog {self.catalog!s} --inventory {self.inventory!s} "
            f"--project {PROJECT!s} --batch-size 1 --batch batch0000 "
            "--worker-count 11\n",
            encoding="utf-8",
        )
        pipeline.chmod(0o755)
        lock_path = self.inventory / "total_asset_scheduler.lock"
        with scheduler.SchedulerLock(lock_path) as held:
            assert held.handle is not None
            events = scheduler.run_iteration(
                plan,
                project=self.root,
                pipeline_script=pipeline,
                pipeline_python=self.root / "pipeline.py",
                event_dir=self.root / "events",
                batch_size=1,
                qa_activity=(),
                scheduler_lock_fd=held.handle.fileno(),
            )
        self.assertEqual(events[0]["returncode"], 0)
        self.assertTrue(
            scheduler.worker_drain_path(self.inventory, "batch0000", 11).is_file()
        )

    def test_partial_batch_is_never_launched(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        plan = scheduler.derive_plan(
            self.catalog, self.inventory, batch_size=2, active_batches=set()
        )
        self.assertEqual(plan.action, "wait_partial_batch")
        with mock.patch.object(scheduler, "run_command") as run:
            events = scheduler.run_iteration(
                plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=self.root / "events",
                batch_size=2,
                qa_activity=(),
            )
        run.assert_not_called()
        self.assertEqual(events, [])

    def test_checkpoint_cannot_override_actual_status(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0), catalog_row("b", 2, 0)])
        (self.inventory / "total_asset_scheduler_checkpoint.json").write_text(
            json.dumps({"lowest_incomplete_batch": "batch9999"}), encoding="utf-8"
        )
        plan = scheduler.derive_plan(
            self.catalog, self.inventory, batch_size=2, active_batches=set()
        )
        self.assertEqual(plan.lowest_incomplete_batch, "batch0000")

    def test_plan_and_status_commands_do_not_write_runtime_files(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        before = {path.relative_to(self.inventory) for path in self.inventory.rglob("*")}
        for command in ("plan", "status"):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "total_asset_scheduler.py"),
                    command,
                    "--catalog",
                    str(self.catalog),
                    "--inventory",
                    str(self.inventory),
                    "--project",
                    str(PROJECT),
                    "--batch-size",
                    "1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        after = {path.relative_to(self.inventory) for path in self.inventory.rglob("*")}
        self.assertEqual(after, before)

    def test_scheduler_lock_is_single_instance(self) -> None:
        lock_path = self.inventory / "scheduler.lock"
        with scheduler.SchedulerLock(lock_path):
            with self.assertRaises(scheduler.SchedulerAlreadyRunning):
                with scheduler.SchedulerLock(lock_path):
                    pass

    def test_control_cli_commands_share_lock_and_guard_passes_verified_fd(self) -> None:
        script = SCRIPTS / "total_asset_scheduler.py"
        guard = SCRIPTS / "total_asset_launch_guard.py"
        lock_path = self.inventory / "total_asset_scheduler.lock"
        common = [
            "--catalog", str(self.catalog),
            "--inventory", str(self.inventory),
            "--project", str(PROJECT),
            "--batch-size", "1",
            "--batch", "batch0001",
        ]
        commands = (
            ["drain", *common, "--worker-count", "11"],
            ["barrier", *common, "--worker-count", "11", "--local-only"],
            ["resume", *common, "--worker-count", "11"],
        )
        with scheduler.SchedulerLock(lock_path):
            for command in commands:
                blocked = subprocess.run(
                    [sys.executable, str(script), *command],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(blocked.returncode, 75, command)
                self.assertIn("scheduler lock busy", blocked.stderr)
        self.assertFalse(
            scheduler.worker_drain_path(
                self.inventory, "batch0001", 11
            ).exists()
        )

        inherited = subprocess.run(
            [
                sys.executable,
                str(guard),
                "--lock", str(lock_path),
                "--",
                sys.executable,
                str(script),
                "drain",
                *common,
                "--worker-count", "11",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(inherited.returncode, 0, inherited.stderr)
        self.assertTrue(
            scheduler.worker_drain_path(
                self.inventory, "batch0001", 11
            ).is_file()
        )

    def test_plan_remains_read_only_and_unlocked_while_scheduler_lock_is_held(self) -> None:
        write_catalog(self.catalog, [catalog_row("a", 1, 0)])
        lock_path = self.inventory / "total_asset_scheduler.lock"
        with scheduler.SchedulerLock(lock_path):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "total_asset_scheduler.py"),
                    "plan",
                    "--catalog", str(self.catalog),
                    "--inventory", str(self.inventory),
                    "--project", str(PROJECT),
                    "--batch-size", "1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_duplicate_catalog_identity_fails_closed(self) -> None:
        rows = [catalog_row("a", 1, 0), catalog_row("a", 2, 0)]
        write_catalog(self.catalog, rows)
        with self.assertRaises(scheduler.CatalogError):
            scheduler.derive_plan(self.catalog, self.inventory, batch_size=2, active_batches=set())

    def test_render_order_gap_and_batch_mismatch_fail_closed(self) -> None:
        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 3, 1)],
        )
        with self.assertRaisesRegex(scheduler.CatalogError, "continuous"):
            scheduler.derive_plan(
                self.catalog, self.inventory, batch_size=2, active_batches=set()
            )

        write_catalog(
            self.catalog,
            [catalog_row("a", 1, 0), catalog_row("b", 2, 1)],
        )
        with self.assertRaisesRegex(scheduler.CatalogError, "does not match"):
            scheduler.derive_plan(
                self.catalog, self.inventory, batch_size=2, active_batches=set()
            )

        missing_order = catalog_row("a", 1, 0)
        missing_order["render_order"] = ""
        write_catalog(self.catalog, [missing_order])
        with self.assertRaisesRegex(scheduler.CatalogError, "positive render_order"):
            scheduler.derive_plan(
                self.catalog, self.inventory, batch_size=2, active_batches=set()
            )

    def test_audit_is_idempotent_and_scheduler_never_launches_generic_repair(self) -> None:
        batch = scheduler.BatchState(
            index=0,
            key="batch0000",
            expected=2,
            requested_batch_size=2,
            terminal=2,
            full=True,
            complete=True,
            counts={"accepted": 1, "failed": 1},
            generation="generation",
        )
        plan = scheduler.SchedulerPlan(
            generated_at="now",
            catalog_rows=2,
            malformed_status_lines=0,
            batches=(batch,),
            lowest_incomplete_batch=None,
            action="idle_all_complete",
            active_batches=(),
        )
        refreshed_batch = scheduler.BatchState(
            index=0,
            key="batch0000",
            expected=2,
            requested_batch_size=2,
            terminal=2,
            full=True,
            complete=True,
            counts={"accepted": 1, "needs_review": 1},
            generation="post-audit-generation",
        )
        refreshed_plan = scheduler.SchedulerPlan(
            generated_at="later",
            catalog_rows=2,
            malformed_status_lines=0,
            batches=(refreshed_batch,),
            lowest_incomplete_batch=None,
            action="idle_all_complete",
            active_batches=(),
        )
        events_dir = self.root / "events"
        with mock.patch.object(scheduler, "run_command", return_value=0) as run:
            first = scheduler.run_iteration(
                plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=events_dir,
                batch_size=2,
                repair_retry_seconds=60,
                qa_activity=(),
                refresh_plan=lambda: refreshed_plan,
            )
            second = scheduler.run_iteration(
                refreshed_plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=events_dir,
                batch_size=2,
                repair_retry_seconds=60,
                qa_activity=(),
            )
            third = scheduler.run_iteration(
                refreshed_plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=events_dir,
                batch_size=2,
                repair_retry_seconds=60,
                qa_activity=(),
            )
        self.assertEqual([event["event"] for event in first], ["audit"])
        self.assertEqual(second, [])
        self.assertEqual(third, [])
        self.assertEqual(run.call_count, 1)
        self.assertTrue(
            scheduler.event_marker(
                events_dir, refreshed_batch, "audit"
            ).is_file()
        )
        self.assertFalse(
            scheduler.event_marker(events_dir, batch, "audit").exists()
        )
        repair_calls = [
            call
            for call in run.call_args_list
            if call.args and call.args[0][-1] == "repair-batch"
        ]
        self.assertEqual(repair_calls, [])

    def test_live_repair_or_audit_activity_blocks_new_qa_commands(self) -> None:
        batch = scheduler.BatchState(
            index=0,
            key="batch0000",
            expected=1,
            requested_batch_size=1,
            terminal=1,
            full=True,
            complete=True,
            counts={"failed": 1},
            generation="generation",
        )
        plan = scheduler.SchedulerPlan(
            generated_at="now",
            catalog_rows=1,
            malformed_status_lines=0,
            batches=(batch,),
            lowest_incomplete_batch=None,
            action="idle_all_complete",
            active_batches=(),
        )
        with mock.patch.object(scheduler, "run_command") as run:
            events = scheduler.run_iteration(
                plan,
                project=self.root,
                pipeline_script=self.root / "pipeline.sh",
                pipeline_python=self.root / "pipeline.py",
                event_dir=self.root / "events",
                batch_size=1,
                qa_activity=(
                    "screen:batch0000:total_asset_batch0000_repair_v5_g0",
                ),
            )
        self.assertEqual(events, [])
        run.assert_not_called()

    def test_explicit_generic_repair_command_fails_before_holder_or_worker_path(self) -> None:
        shell = PROJECT / "blender/scripts/run_total_asset_pipeline.sh"
        asset_root = self.root / "asset-root"
        (asset_root / "_asset_inventory").mkdir(parents=True)
        environment = os.environ.copy()
        environment.update({
            "TOTAL_ASSET_ROOT": str(asset_root),
            "TOTAL_ASSET_BATCH_INDEX": "0",
        })
        result = subprocess.run(
            ["bash", str(shell), "repair-batch"],
            cwd=PROJECT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 75)
        self.assertIn(
            "deferred repair launcher is restricted to batch0001",
            result.stderr,
        )
        self.assertFalse((asset_root / "total_render").exists())


if __name__ == "__main__":
    unittest.main()
