from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import total_asset_slot_watchdog as watchdog
import total_asset_scheduler as scheduler


class TotalAssetSlotWatchdogTests(unittest.TestCase):
    def test_local_patrol_defaults_to_hourly(self) -> None:
        self.assertEqual(watchdog.DEFAULT_POLL_SECONDS, 3600)
        self.assertEqual(
            watchdog.next_poll_seconds({"status": "healthy", "events": []}, 3600),
            3600,
        )

    def test_missing_chain_confirmation_gets_short_rechecks(self) -> None:
        payload = {
            "events": [{
                "action": "confirming",
                "detail": "missing chain confirmation 1/3 target=batch0002",
            }]
        }
        self.assertEqual(watchdog.next_poll_seconds(payload, 3600), 15)

    def test_stale_chain_confirmation_gets_short_rechecks(self) -> None:
        payload = {
            "events": [{
                "action": "confirming",
                "detail": "chain heartbeat stale confirmation 1/3; missing",
            }]
        }
        self.assertEqual(watchdog.next_poll_seconds(payload, 3600), 15)

    def test_retry_deadline_wakes_before_hourly_patrol(self) -> None:
        self.assertEqual(
            watchdog.next_poll_seconds(
                {"events": [], "next_retry_seconds": 299}, 3600
            ),
            299,
        )

    def test_only_confirmation_events_request_short_state_retry(self) -> None:
        slot = dict(remote_port=30773, gpu=0, worker_index=4)
        self.assertEqual(
            watchdog.confirmation_retry_seconds((watchdog.SlotEvent(
                **slot,
                action="confirming",
                detail="missing chain confirmation 2/3 target=batch0002",
            ),)),
            15,
        )
        self.assertEqual(
            watchdog.confirmation_retry_seconds((watchdog.SlotEvent(
                **slot,
                action="waiting",
                detail="chain heartbeat stale confirmation 2/3; pid=10",
            ),)),
            15,
        )
        for action in ("blocked", "controller_healthy_remote_unverified"):
            with self.subTest(action=action):
                self.assertIsNone(watchdog.confirmation_retry_seconds((
                    watchdog.SlotEvent(**slot, action=action, detail="ordinary"),
                )))

    def test_audit_error_uses_read_only_recheck_before_normal_pass(self) -> None:
        class StopLoop(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            (asset_root / "_asset_inventory/worker_control").mkdir(
                parents=True
            )
            args = SimpleNamespace(
                project=ROOT,
                asset_root=asset_root,
                read_only=False,
                once=False,
                poll_seconds=3600,
            )
            baseline = watchdog.ControlBaseline(1, (), (), ())
            healthy_payload = {
                "schema": "video2blender.formal-slot-watchdog-state.v1",
                "status": "attention",
                "events": [],
                "next_retry_seconds": None,
            }
            recorded_payloads: list[dict[str, object]] = []

            def record_state(_path: Path, payload: dict[str, object]) -> None:
                recorded_payloads.append(dict(payload))

            with (
                mock.patch.object(watchdog.signal, "signal"),
                mock.patch.object(
                    watchdog, "capture_control_baseline", return_value=baseline
                ),
                mock.patch.object(
                    watchdog, "verify_control_baseline", return_value=baseline
                ),
                mock.patch.object(
                    watchdog,
                    "audit_and_repair",
                    side_effect=(
                        watchdog.WatchdogError(
                            "duplicate formal chain controller slots=[safe]"
                        ),
                        watchdog.WatchdogError(
                            "duplicate formal chain controller slots=[safe]"
                        ),
                        dict(healthy_payload),
                        dict(healthy_payload),
                    ),
                ) as audit,
                mock.patch.object(
                    watchdog, "atomic_write_json", side_effect=record_state
                ),
                mock.patch.object(
                    watchdog,
                    "next_poll_seconds",
                    side_effect=(0, 0, 0, StopLoop()),
                ),
                mock.patch("builtins.print"),
            ):
                with self.assertRaises(StopLoop):
                    watchdog.run_loop(args)

            self.assertEqual(
                recorded_payloads[0]["next_retry_seconds"],
                watchdog.AUDIT_ERROR_RECHECK_SECONDS,
            )
            self.assertEqual(
                recorded_payloads[0]["audit_mode"],
                "blocked_read_only_recheck_pending",
            )
            self.assertEqual(
                recorded_payloads[1]["audit_mode"],
                "blocked_read_only_recheck_pending",
            )
            self.assertEqual(
                recorded_payloads[1]["next_retry_seconds"],
                watchdog.AUDIT_ERROR_RECHECK_SECONDS,
            )
            self.assertEqual(
                recorded_payloads[2]["audit_mode"], "read_only_recheck"
            )
            self.assertEqual(
                recorded_payloads[2]["next_retry_seconds"],
                watchdog.AUDIT_ERROR_RECHECK_SECONDS,
            )
            self.assertEqual(
                [call.kwargs["launch"] for call in audit.call_args_list],
                [True, False, False, True],
            )

    def test_remote_tcp_host_keeps_only_valid_hostname(self) -> None:
        self.assertEqual(
            watchdog.configured_remote_tcp_host({
                "REMOTE_GPU_SSH_HOST": "render-user@render.example"
            }),
            "render.example",
        )
        self.assertEqual(
            watchdog.configured_remote_tcp_host({
                "REMOTE_GPU_SSH_HOST": "render-user@[2001:db8::1]"
            }),
            "2001:db8::1",
        )
        with self.assertRaisesRegex(
            watchdog.WatchdogError,
            "remote TCP endpoint configuration is invalid",
        ):
            watchdog.configured_remote_tcp_host({
                "REMOTE_GPU_SSH_HOST": "ssh -o unsafe render.example"
            })

    def test_tcp_probe_returns_only_safe_reachability_categories(self) -> None:
        cases = (
            (ConnectionRefusedError("private detail"), "connection_refused"),
            (watchdog.socket.timeout("private detail"), "timeout"),
            (
                OSError(watchdog.errno.EHOSTUNREACH, "private detail"),
                "route_unreachable",
            ),
            (OSError(watchdog.errno.EACCES, "private detail"), "probe_error"),
        )
        for failure, expected in cases:
            with (
                self.subTest(expected=expected),
                mock.patch.object(
                    watchdog.socket,
                    "create_connection",
                    side_effect=failure,
                ) as connect,
            ):
                result = watchdog.probe_tcp_endpoint(
                    "render.example", 30808, 0.25
                )
            self.assertFalse(result.reachable)
            self.assertEqual(result.reason, expected)
            self.assertNotIn("private", result.reason)
            connect.assert_called_once_with(
                ("render.example", 30808), timeout=0.25
            )

    def test_offline_node_cooldown_is_shared_across_its_gpu_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            port = 30808
            topology = {(port, 0): 8, (port, 1): 9}
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            cooldowns: dict[tuple[str, int], watchdog.NodeCooldown] = {}
            streak = {8: 2, 9: 2}
            probe = mock.Mock(
                return_value=watchdog.NodeProbeResult(
                    False, "connection_refused"
                )
            )
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    topology,
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(watchdog, "launch_chain") as launch,
            ):
                first = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=streak,
                    node_cooldowns=cooldowns,
                    node_probe=probe,
                    remote_tcp_host="render.example",
                    now_epoch=1000.0,
                )
                second = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=streak,
                    node_cooldowns=cooldowns,
                    node_probe=probe,
                    remote_tcp_host="render.example",
                    now_epoch=1001.0,
                )
            self.assertEqual(
                [event["action"] for event in first["events"]],
                ["node_unreachable", "node_unreachable"],
            )
            self.assertEqual(first["node_unreachable_count"], 1)
            self.assertEqual(first["next_retry_seconds"], 900)
            self.assertIn("source=probe", first["events"][0]["detail"])
            self.assertIn("source=cached", first["events"][1]["detail"])
            self.assertTrue(all(
                "source=cached" in event["detail"]
                for event in second["events"]
            ))
            probe.assert_called_once_with(
                "render.example", port, watchdog.NODE_PROBE_TIMEOUT_SECONDS
            )
            self.assertEqual(len(cooldowns), 1)
            launch.assert_not_called()

    def test_launcher_uses_hourly_watchdog_poll(self) -> None:
        script = (SCRIPTS / "run_total_asset_pipeline.sh").read_text(encoding="utf-8")
        self.assertIn("--poll-seconds 3600", script)
        self.assertNotIn("total_asset_slot_watchdog.py --project '$PROJECT' --asset-root '$ROOT' --poll-seconds 7200", script)
        self.assertNotIn("total_asset_slot_watchdog.py --project '$PROJECT' --asset-root '$ROOT' --poll-seconds 60", script)

    def test_aggregate_status_requires_real_verified_work(self) -> None:
        slot = dict(remote_port=30773, gpu=0, worker_index=4)
        self.assertEqual(
            watchdog.aggregate_watchdog_status(
                [watchdog.SlotEvent(**slot, action="healthy", detail="remote uuid verified")],
                worker_claim_count=1,
                active_drain_requests=(),
            ),
            "healthy",
        )
        self.assertEqual(
            watchdog.aggregate_watchdog_status(
                [watchdog.SlotEvent(
                    **slot,
                    action="controller_healthy_remote_unverified",
                    detail="local only",
                )],
                worker_claim_count=1,
                active_drain_requests=(),
            ),
            "attention",
        )

    def test_aggregate_status_distinguishes_drain_quarantine_and_finalizer_waits(
        self,
    ) -> None:
        drain_event = watchdog.SlotEvent(
            30773,
            0,
            4,
            "waiting",
            "validated drain generation keeps empty slot stopped: batch0002.wc11.drain.json",
        )
        self.assertEqual(
            watchdog.aggregate_watchdog_status(
                [drain_event],
                worker_claim_count=0,
                active_drain_requests=("batch0002.wc11.drain.json",),
            ),
            "drained",
        )
        for detail in (
            "slot quarantined with holder: offline",
            "verified finalizer is completing holder handoff",
        ):
            with self.subTest(detail=detail):
                self.assertEqual(
                    watchdog.aggregate_watchdog_status(
                        [watchdog.SlotEvent(30808, 0, 8, "waiting", detail)],
                        worker_claim_count=0,
                        active_drain_requests=("batch0002.wc11.drain.json",),
                    ),
                    "waiting",
                )

    def test_chain_writes_atomic_heartbeat_and_retries_outer_gate_lock(self) -> None:
        script = (SCRIPTS / "run_total_asset_pipeline.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("video2blender.formal-slot-chain-state.v1", script)
        self.assertIn('"waiting_partial_batch"', script)
        self.assertIn('"waiting_scheduler_lock"', script)
        self.assertIn(
            "waiting: scheduler slot-gate lock is busy; exact chain will retry",
            script,
        )
        self.assertIn("os.replace(temporary, path)", script)

    def test_parse_chain_claims_accepts_only_real_bash_process(self) -> None:
        rows = watchdog.parse_process_table(
            "\n".join((
                " 10 1 10 SCREEN SCREEN -dmS x /bin/bash -lc 'bash blender/scripts/run_total_asset_pipeline.sh formal-slot-chain-loop batch0004 31722 2 2'",
                " 11 10 11 bash bash blender/scripts/run_total_asset_pipeline.sh formal-slot-chain-loop batch0004 31722 2 2",
                " 12 1 12 Python python3 blender/scripts/run_total_asset_render_worker.py --batch batch0004 --remote-port 31722 --gpu 2 --worker-index 2 --worker-count 11",
            ))
        )
        claims = watchdog.parse_chain_claims(rows)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].pid, 11)
        self.assertEqual(
            (claims[0].batch, claims[0].remote_port, claims[0].gpu, claims[0].worker_index),
            ("batch0004", 31722, 2, 2),
        )

    def test_finalizer_requires_one_token_in_exact_screen_lineage(self) -> None:
        token = "total_asset_gpu_finalizer_v2_" + "a" * 64
        name = "total_asset_batch0004_31722_g2_finalizer"
        rows = (
            watchdog.ProcessRow(100, 1, 100, "SCREEN", f"SCREEN {token}"),
            watchdog.ProcessRow(101, 100, 101, "login", f"login {token}"),
            watchdog.ProcessRow(102, 101, 101, "bash", f"bash -lc loop {token}"),
        )
        self.assertEqual(watchdog.verify_finalizer(name, {name: (100,)}, rows), token)
        self.assertEqual(watchdog.screenless_finalizer_tokens({}, rows), (100, 101, 102))

    def test_finalizer_rejects_token_outside_exact_screen(self) -> None:
        token = "total_asset_gpu_finalizer_v2_" + "b" * 64
        name = "total_asset_batch0004_31722_g2_finalizer"
        rows = (
            watchdog.ProcessRow(100, 1, 100, "SCREEN", f"SCREEN {token}"),
            watchdog.ProcessRow(101, 100, 101, "bash", f"bash {token}"),
            watchdog.ProcessRow(900, 1, 900, "bash", f"bash {token}"),
        )
        with self.assertRaisesRegex(watchdog.WatchdogError, "outside exact screen"):
            watchdog.verify_finalizer(name, {name: (100,)}, rows)

    def test_control_baseline_advances_only_valid_drain_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            asset_root = root / "assets"
            scripts = project / "blender/scripts"
            controls = asset_root / "_asset_inventory/worker_control"
            scripts.mkdir(parents=True)
            controls.mkdir(parents=True)
            for name in watchdog.CONTROL_SCRIPT_NAMES:
                (scripts / name).write_text(f"{name}\n")
            baseline = watchdog.capture_control_baseline(project, asset_root)
            self.assertEqual(baseline.drain_generations, ())

            first = scheduler.create_drain_request(
                asset_root / "_asset_inventory", "batch0001", 11
            )
            drained = watchdog.verify_control_baseline(
                baseline, project, asset_root
            )
            self.assertEqual(
                drained.drain_generations[0][1], first["request_id"]
            )

            drain = scheduler.worker_drain_path(
                asset_root / "_asset_inventory", "batch0001", 11
            )
            scheduler.clear_drain_request(
                asset_root / "_asset_inventory",
                "batch0001",
                11,
                expected_request_id=str(first["request_id"]),
            )
            resumed = watchdog.verify_control_baseline(
                drained, project, asset_root
            )
            self.assertEqual(resumed.drain_generations, ())

            second = scheduler.create_drain_request(
                asset_root / "_asset_inventory", "batch0001", 11
            )
            redrained = watchdog.verify_control_baseline(
                resumed, project, asset_root
            )
            self.assertNotEqual(first["request_id"], second["request_id"])
            self.assertEqual(
                redrained.drain_generations[0][1], second["request_id"]
            )

            payload = scheduler.load_drain_request_path(drain)
            payload["requested_at"] = "tampered without generation change"
            watchdog.atomic_write_json(drain, payload)
            with self.assertRaisesRegex(watchdog.WatchdogError, "drain control"):
                watchdog.verify_control_baseline(
                    redrained, project, asset_root
                )

    def test_active_drain_generation_keeps_empty_slot_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            scheduler.create_drain_request(
                inventory, "batch0002", watchdog.DEFAULT_WORKER_COUNT
            )
            drain = scheduler.worker_drain_path(
                inventory, "batch0002", watchdog.DEFAULT_WORKER_COUNT
            )
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
                drain_requests=(str(drain),),
            )
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(watchdog, "launch_chain") as launch,
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=True
                )
            self.assertEqual(payload["events"][0]["action"], "waiting")
            self.assertEqual(payload["status"], "drained")
            self.assertIn(
                "drain generation keeps empty slot stopped",
                payload["events"][0]["detail"],
            )
            launch.assert_not_called()

    def test_generic_scheduler_parser_ignores_wrappers(self) -> None:
        rows = (
            watchdog.ProcessRow(
                1, 0, 1, "SCREEN",
                "SCREEN -dmS scheduler python3 blender/scripts/total_asset_scheduler.py run",
            ),
            watchdog.ProcessRow(
                2, 1, 2, "Python",
                "python3 blender/scripts/total_asset_scheduler.py run --batch-size 1000",
            ),
        )
        self.assertEqual(watchdog.generic_scheduler_processes(rows), (2,))

    def test_worker_must_descend_from_exact_screen(self) -> None:
        port, gpu = next(
            location for location, index in
            watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items() if index == 0
        )
        claim = watchdog.WorkerClaim("batch0001", port, gpu, 0, 11)
        screen = watchdog.exact_worker_screen(claim)
        rows = (
            watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN"),
            watchdog.ProcessRow(101, 100, 101, "Python", "python worker"),
        )
        process = watchdog.WorkerProcess(101, claim)
        self.assertEqual(
            watchdog.verify_worker_lineage(claim, (process,), {screen: (100,)}, rows),
            101,
        )
        rogue = watchdog.WorkerProcess(900, claim)
        rogue_rows = rows + (
            watchdog.ProcessRow(900, 1, 900, "Python", "python worker"),
        )
        with self.assertRaisesRegex(watchdog.WatchdogError, "does not descend"):
            watchdog.verify_worker_lineage(
                claim, (rogue,), {screen: (100,)}, rogue_rows
            )

    def test_local_worker_lineage_is_not_reported_as_remote_gpu_healthy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            claim = watchdog.WorkerClaim(
                "batch0002", port, gpu, worker_index, 11
            )
            chain_screen = watchdog.exact_chain_screen(
                port, gpu, worker_index
            )
            worker_screen = watchdog.exact_worker_screen(claim)
            finalizer_screen = f"{worker_screen}_finalizer"
            token = "total_asset_gpu_finalizer_v2_" + "d" * 64
            rows = (
                watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN chain"),
                watchdog.ProcessRow(
                    101,
                    100,
                    101,
                    "bash",
                    "bash blender/scripts/run_total_asset_pipeline.sh "
                    f"formal-slot-chain-loop batch0002 {port} {gpu} {worker_index}",
                ),
                watchdog.ProcessRow(200, 1, 200, "SCREEN", "SCREEN worker"),
                watchdog.ProcessRow(
                    201,
                    200,
                    201,
                    "Python",
                    "python3 blender/scripts/run_total_asset_render_worker.py "
                    f"--batch batch0002 --remote-port {port} --gpu {gpu} "
                    f"--worker-index {worker_index} --worker-count 11",
                ),
                watchdog.ProcessRow(300, 1, 300, "SCREEN", f"SCREEN {token}"),
                watchdog.ProcessRow(301, 300, 301, "bash", f"bash {token}"),
            )
            sessions = {
                chain_screen: (100,),
                worker_screen: (200,),
                finalizer_screen: (300,),
            }
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
                drain_requests=(),
            )
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=rows),
                mock.patch.object(
                    watchdog, "screen_snapshot", return_value=sessions
                ),
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=False
                )
            event = payload["events"][0]
            self.assertEqual(
                event["action"], "controller_healthy_remote_unverified"
            )
            self.assertIn("PID-to-GPU UUID is unverified", event["detail"])
            self.assertEqual(payload["status"], "attention")

    def test_empty_slot_recovers_exact_chain_after_short_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            missing_streak: dict[int, int] = {}
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(
                    watchdog,
                    "probe_tcp_endpoint",
                    return_value=watchdog.NodeProbeResult(True, "reachable"),
                ),
                mock.patch.object(
                    watchdog, "launch_chain", return_value=(75, "test launch failure")
                ) as launch,
            ):
                first = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=missing_streak,
                )
                second = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=missing_streak,
                )
                third = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=missing_streak,
                )
            self.assertEqual(first["events"][0]["action"], "confirming")
            self.assertEqual(second["events"][0]["action"], "confirming")
            self.assertEqual(first["next_retry_seconds"], 15)
            self.assertEqual(second["next_retry_seconds"], 15)
            self.assertEqual(third["events"][0]["action"], "blocked")
            launch.assert_called_once_with(
                ROOT, "batch0002", port, gpu, worker_index
            )

    def test_missing_chain_hard_runtime_failure_is_not_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            (inventory / "worker_runtime").mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            runtime = watchdog.formal_slot_runtime_path(
                inventory, "batch0002", port, gpu, worker_index
            )
            watchdog.atomic_write_json(runtime, {
                "schema_version": 1,
                "status": "failed",
                "state": "failed",
                "batch": "batch0002",
                "remote_port": port,
                "gpu": gpu,
                "worker_index": worker_index,
                "worker_count": 11,
                "failure_category": "worker_processing",
                "error": "deterministic render contract failure",
            })
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(watchdog, "launch_chain") as launch,
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=True
                )
            self.assertEqual(payload["events"][0]["action"], "blocked")
            self.assertIn(
                "runtime status is not safely retryable: failed",
                payload["events"][0]["detail"],
            )
            launch.assert_not_called()

    def test_orphan_running_runtime_accepts_only_exact_actionable_gates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            (inventory / "worker_runtime").mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            runtime = watchdog.formal_slot_runtime_path(
                inventory, "batch0002", port, gpu, worker_index
            )
            watchdog.atomic_write_json(runtime, {
                "schema_version": 1,
                "status": "running",
                "state": "running",
                "batch": "batch0002",
                "remote_port": port,
                "gpu": gpu,
                "worker_index": worker_index,
                "worker_count": 11,
            })
            cases = (
                (
                    (True, "target_partition_complete"),
                    True,
                    "batch0003",
                    "running_partition_complete",
                ),
                (
                    (False, "ready"),
                    True,
                    "batch0002",
                    "running_partition_ready",
                ),
                (
                    (None, "slot gate lock is busy"),
                    False,
                    "batch0002",
                    "lacks an exact actionable partition gate",
                ),
                (
                    (False, "previous_partition_statusless"),
                    False,
                    "batch0002",
                    "lacks an exact actionable partition gate",
                ),
                (
                    (True, "ready"),
                    False,
                    "batch0002",
                    "lacks an exact actionable partition gate",
                ),
            )
            for gate_result, allowed, target_batch, evidence in cases:
                with (
                    self.subTest(gate_result=gate_result),
                    mock.patch.object(
                        watchdog,
                        "exact_partition_complete",
                        return_value=gate_result,
                    ) as gate,
                ):
                    recovery = watchdog.missing_chain_recovery(
                        inventory,
                        plan_batch="batch0001",
                        remote_port=port,
                        gpu=gpu,
                        worker_index=worker_index,
                        heartbeat=None,
                    )
                    self.assertEqual(recovery.allowed, allowed)
                    self.assertEqual(recovery.target_batch, target_batch)
                    self.assertIn(evidence, recovery.evidence)
                    gate.assert_called_once_with(
                        inventory,
                        batch="batch0002",
                        remote_port=port,
                        gpu=gpu,
                        worker_index=worker_index,
                    )

    def test_stale_partial_batch_heartbeat_without_runtime_recovers_latest_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            now = time.time()
            heartbeat = watchdog.ChainState(
                status="waiting_partial_batch",
                batch="batch0004",
                remote_port=port,
                gpu=gpu,
                worker_index=worker_index,
                worker_count=11,
                pid=1234,
                detail="target batch is not full",
                observed_at_epoch=now - 600,
                lease_until_epoch=now - 300,
            )
            recovery = watchdog.missing_chain_recovery(
                inventory,
                plan_batch="batch0005",
                remote_port=port,
                gpu=gpu,
                worker_index=worker_index,
                heartbeat=heartbeat,
            )
            self.assertTrue(recovery.allowed)
            self.assertEqual(recovery.target_batch, "batch0005")
            self.assertEqual(
                recovery.evidence, "stale_heartbeat_waiting_partial_batch"
            )

            fresh = watchdog.ChainState(
                **{
                    **heartbeat.__dict__,
                    "observed_at_epoch": now,
                    "lease_until_epoch": now + 300,
                }
            )
            blocked = watchdog.missing_chain_recovery(
                inventory,
                plan_batch="batch0005",
                remote_port=port,
                gpu=gpu,
                worker_index=worker_index,
                heartbeat=fresh,
            )
            self.assertFalse(blocked.allowed)
            self.assertIn("lease remains active", blocked.evidence)

    def test_scheduler_lock_busy_launch_uses_short_retry_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            retry_after: dict[int, float] = {}
            missing_streak: dict[int, int] = {}
            before = time.time()
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(
                    watchdog,
                    "probe_tcp_endpoint",
                    return_value=watchdog.NodeProbeResult(True, "reachable"),
                ),
                mock.patch.object(
                    watchdog,
                    "launch_chain",
                    return_value=(75, "scheduler lock busy: formal_slot_gate.lock"),
                ) as launch,
            ):
                for _ in range(3):
                    payload = watchdog.audit_and_repair(
                        project=ROOT,
                        asset_root=asset_root,
                        launch=True,
                        retry_after=retry_after,
                        missing_streak=missing_streak,
                    )
            self.assertEqual(payload["events"][0]["action"], "blocked")
            launch.assert_called_once()
            self.assertGreaterEqual(
                retry_after[worker_index],
                before + watchdog.MISSING_CHAIN_RECHECK_SECONDS - 1,
            )
            self.assertLessEqual(
                retry_after[worker_index],
                time.time() + watchdog.MISSING_CHAIN_RECHECK_SECONDS + 1,
            )
            self.assertEqual(
                watchdog.chain_launch_retry_seconds("remote preflight failed"),
                watchdog.FAILED_LAUNCH_RETRY_SECONDS,
            )

    def test_missing_chain_blocked_gpu_busy_runtime_is_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            (inventory / "worker_runtime").mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            runtime = watchdog.formal_slot_runtime_path(
                inventory, "batch0002", port, gpu, worker_index
            )
            watchdog.atomic_write_json(runtime, {
                "schema_version": 1,
                "status": "blocked_gpu_busy",
                "state": "blocked_gpu_busy",
                "batch": "batch0002",
                "remote_port": port,
                "gpu": gpu,
                "worker_index": worker_index,
                "worker_count": 11,
                "reason": "selected GPU remains occupied",
            })
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            streak: dict[int, int] = {}
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(
                    watchdog,
                    "probe_tcp_endpoint",
                    return_value=watchdog.NodeProbeResult(True, "reachable"),
                ),
                mock.patch.object(
                    watchdog, "launch_chain", return_value=(75, "test launch failure")
                ) as launch,
            ):
                first = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=streak,
                )
                second = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=streak,
                )
                third = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    missing_streak=streak,
                )
            self.assertEqual(first["events"][0]["action"], "confirming")
            self.assertIn("blocked_gpu_busy", first["events"][0]["detail"])
            self.assertEqual(second["events"][0]["action"], "confirming")
            self.assertEqual(third["events"][0]["action"], "blocked")
            launch.assert_called_once_with(
                ROOT, "batch0002", port, gpu, worker_index
            )

    def test_drained_partial_runtime_waits_for_resume_then_restarts_same_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            (inventory / "worker_runtime").mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            drain = scheduler.worker_drain_path(
                inventory, "batch0002", watchdog.DEFAULT_WORKER_COUNT
            )
            request = scheduler.create_drain_request(
                inventory, "batch0002", watchdog.DEFAULT_WORKER_COUNT
            )
            runtime = watchdog.formal_slot_runtime_path(
                inventory, "batch0002", port, gpu, worker_index
            )
            watchdog.atomic_write_json(runtime, {
                "schema_version": 1,
                "status": "drained",
                "state": "drained",
                "batch": "batch0002",
                "remote_port": port,
                "gpu": gpu,
                "worker_index": worker_index,
                "worker_count": 11,
                "drain_file": str(drain),
                "reason": "drain request observed after durable asset boundary",
            })
            with mock.patch.object(
                watchdog, "exact_partition_complete"
            ) as gate:
                active = watchdog.missing_chain_recovery(
                    inventory,
                    plan_batch="batch0001",
                    remote_port=port,
                    gpu=gpu,
                    worker_index=worker_index,
                    heartbeat=None,
                )
            self.assertFalse(active.allowed)
            self.assertIn("drain request remains active", active.evidence)
            gate.assert_not_called()

            scheduler.clear_drain_request(
                inventory,
                "batch0002",
                watchdog.DEFAULT_WORKER_COUNT,
                expected_request_id=str(request["request_id"]),
            )
            with mock.patch.object(
                watchdog,
                "exact_partition_complete",
                return_value=(False, "target_partition_incomplete"),
            ) as gate:
                resumed = watchdog.missing_chain_recovery(
                    inventory,
                    plan_batch="batch0001",
                    remote_port=port,
                    gpu=gpu,
                    worker_index=worker_index,
                    heartbeat=None,
                )
            self.assertTrue(resumed.allowed)
            self.assertEqual(resumed.target_batch, "batch0002")
            self.assertEqual(
                resumed.evidence,
                "drained_runtime_drain_cleared_partial_partition",
            )
            gate.assert_called_once()

    def test_drained_heartbeat_without_runtime_recovers_after_exact_clear(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            request = scheduler.create_drain_request(
                inventory, "batch0002", watchdog.DEFAULT_WORKER_COUNT
            )
            drain = scheduler.worker_drain_path(
                inventory, "batch0002", watchdog.DEFAULT_WORKER_COUNT
            )
            heartbeat = watchdog.ChainState(
                status="drained",
                batch="batch0002",
                remote_port=port,
                gpu=gpu,
                worker_index=worker_index,
                worker_count=11,
                pid=1234,
                detail=(
                    f"request_id={request['request_id']} "
                    f"drain_file={drain}"
                ),
                observed_at_epoch=time.time(),
                lease_until_epoch=time.time() + 900,
            )
            scheduler.clear_drain_request(
                inventory,
                "batch0002",
                watchdog.DEFAULT_WORKER_COUNT,
                expected_request_id=str(request["request_id"]),
            )
            with mock.patch.object(
                watchdog,
                "exact_partition_complete",
                return_value=(False, "target_partition_incomplete"),
            ):
                recovery = watchdog.missing_chain_recovery(
                    inventory,
                    plan_batch="batch0001",
                    remote_port=port,
                    gpu=gpu,
                    worker_index=worker_index,
                    heartbeat=heartbeat,
                )
            self.assertTrue(recovery.allowed)
            self.assertEqual(recovery.target_batch, "batch0002")
            self.assertIn(
                str(request["request_id"]), recovery.evidence
            )

    def test_missing_chain_complete_partition_advances_without_repeating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            (inventory / "worker_runtime").mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            runtime = watchdog.formal_slot_runtime_path(
                inventory, "batch0002", port, gpu, worker_index
            )
            watchdog.atomic_write_json(runtime, {
                "schema_version": 1,
                "status": "complete",
                "state": "complete",
                "batch": "batch0002",
                "remote_port": port,
                "gpu": gpu,
                "worker_index": worker_index,
                "worker_count": 11,
                "reason": "assigned partition exhausted",
            })
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                # Another slot may still make the global lowest batch older.
                lowest_incomplete_batch="batch0001",
            )
            streak: dict[int, int] = {}
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(
                    watchdog,
                    "exact_partition_complete",
                    return_value=(True, "target_partition_complete"),
                ) as gate,
                mock.patch.object(
                    watchdog,
                    "probe_tcp_endpoint",
                    return_value=watchdog.NodeProbeResult(True, "reachable"),
                ),
                mock.patch.object(
                    watchdog, "launch_chain", return_value=(75, "test launch failure")
                ) as launch,
            ):
                for _ in range(3):
                    payload = watchdog.audit_and_repair(
                        project=ROOT,
                        asset_root=asset_root,
                        launch=True,
                        missing_streak=streak,
                    )
            gate.assert_called_with(
                inventory,
                batch="batch0002",
                remote_port=port,
                gpu=gpu,
                worker_index=worker_index,
            )
            self.assertIn(
                "complete_partition_complete",
                payload["events"][0]["detail"],
            )
            launch.assert_called_once_with(
                ROOT, "batch0003", port, gpu, worker_index
            )

    def test_only_explicit_remote_transport_failures_are_retryable(self) -> None:
        timeout = {
            "status": "failed",
            "failure_category": "remote_transport",
            "error": "worker_initialization: worker_ssh_timeout",
        }
        refused = {
            "status": "failed",
            "failure_category": "remote_transport",
            "error": (
                "worker_initialization: exitstatus=255 worker_ssh_failed\n"
                "ssh: connect to host 10.26.6.88 port 30808: Connection refused"
            ),
        }
        remotely_closed = {
            "status": "failed",
            "failure_category": "remote_transport",
            "error": (
                "worker_initialization: exitstatus=255 worker_ssh_failed\n"
                "Connection to 10.26.6.88 closed by remote host.\n"
                "client_loop: send disconnect: Broken pipe"
            ),
        }
        auth_failure = {
            "status": "failed",
            "failure_category": "remote_transport",
            "error": (
                "worker_initialization: exitstatus=255 worker_ssh_failed\n"
                "root@10.26.6.88: Permission denied (publickey)."
            ),
        }
        remote_runtime_crash = {
            "status": "failed",
            "failure_category": "remote_transport",
            "error": (
                "primary_render: exitstatus=255 worker_ssh_failed\n"
                "ERROR: vkGetDeviceQueue: Invalid device "
                "[VUID-vkGetDeviceQueue-device-parameter]\n"
                "timeout: the monitored command dumped core"
            ),
        }
        self.assertEqual(
            watchdog.transient_runtime_error(timeout),
            "remote_transport_timeout",
        )
        self.assertEqual(
            watchdog.transient_runtime_error(refused),
            "remote_transport_connection",
        )
        self.assertEqual(
            watchdog.transient_runtime_error(remotely_closed),
            "remote_transport_connection",
        )
        self.assertIsNone(watchdog.transient_runtime_error(auth_failure))
        self.assertIsNone(
            watchdog.transient_runtime_error(remote_runtime_crash)
        )

    def test_audit_derives_worker_claims_from_one_process_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(
                    watchdog, "derive_plan", return_value=plan
                ) as derive,
                mock.patch.object(
                    watchdog, "process_snapshot", return_value=()
                ) as snapshot,
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
            ):
                watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=False
                )
            snapshot.assert_called_once_with()
            self.assertEqual(derive.call_args.kwargs["worker_claims"], ())
            self.assertEqual(derive.call_args.kwargs["active_batches"], set())

    def test_unversioned_chain_without_worker_is_blocked_not_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                item for item in watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
                if item[1] == 0
            )
            screen = watchdog.exact_chain_screen(port, gpu, worker_index)
            rows = (
                watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN"),
                watchdog.ProcessRow(
                    101, 100, 101, "bash",
                    "bash blender/scripts/run_total_asset_pipeline.sh "
                    f"formal-slot-chain-loop batch0001 {port} {gpu} {worker_index}",
                ),
            )
            plan = SimpleNamespace(malformed_status_lines=0, topology_errors=())
            with (
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=rows),
                mock.patch.object(
                    watchdog, "screen_snapshot", return_value={screen: (100,)}
                ),
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=False
                )
            event = next(
                item for item in payload["events"] if item["worker_index"] == 0
            )
            self.assertEqual(event["action"], "blocked")
            self.assertIn("operator audit", event["detail"])
            self.assertNotEqual(event["action"], "healthy")

    def test_fresh_partial_batch_heartbeat_preserves_idle_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            controls = asset_root / "_asset_inventory/worker_control"
            controls.mkdir(parents=True)
            (asset_root / "_asset_inventory/total_asset_catalog.csv").write_text(
                "asset_id\n"
            )
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            screen = watchdog.exact_chain_screen(port, gpu, worker_index)
            rows = (
                watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN"),
                watchdog.ProcessRow(
                    101,
                    100,
                    101,
                    "bash",
                    "bash blender/scripts/run_total_asset_pipeline.sh "
                    f"formal-slot-chain-loop batch0001 {port} {gpu} {worker_index}",
                ),
            )
            watchdog.atomic_write_json(
                controls
                / f"formal_slot_chain_p{port}_g{gpu}_w{worker_index}of11.json",
                {
                    "schema": watchdog.CHAIN_STATE_SCHEMA,
                    "status": "waiting_partial_batch",
                    "batch": "batch0016",
                    "remote_port": port,
                    "gpu": gpu,
                    "worker_index": worker_index,
                    "worker_count": 11,
                    "pid": 101,
                    "detail": "target batch is not full",
                    "observed_at_epoch": time.time(),
                    "lease_until_epoch": time.time() + 120,
                },
            )
            plan = SimpleNamespace(malformed_status_lines=0, topology_errors=())
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=rows),
                mock.patch.object(
                    watchdog, "screen_snapshot", return_value={screen: (100,)}
                ),
                mock.patch.object(watchdog, "retire_stale_chain") as retire,
                mock.patch.object(watchdog, "launch_chain") as launch,
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=True
                )
            event = payload["events"][0]
            self.assertEqual(event["action"], "waiting")
            self.assertIn("waiting_partial_batch", event["detail"])
            retire.assert_not_called()
            launch.assert_not_called()

    def test_stale_idle_chain_is_exactly_retired_then_relaunched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            screen = watchdog.exact_chain_screen(port, gpu, worker_index)
            rows = (
                watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN"),
                watchdog.ProcessRow(
                    101,
                    100,
                    101,
                    "bash",
                    "bash blender/scripts/run_total_asset_pipeline.sh "
                    f"formal-slot-chain-loop batch0003 {port} {gpu} {worker_index}",
                ),
            )
            plan = SimpleNamespace(malformed_status_lines=0, topology_errors=())
            controls = inventory / "worker_control"
            controls.mkdir()
            observed = time.time() - 600
            watchdog.atomic_write_json(
                controls
                / f"formal_slot_chain_p{port}_g{gpu}_w{worker_index}of11.json",
                {
                    "schema": watchdog.CHAIN_STATE_SCHEMA,
                    "status": "waiting_partial_batch",
                    "batch": "batch0003",
                    "remote_port": port,
                    "gpu": gpu,
                    "worker_index": worker_index,
                    "worker_count": 11,
                    "pid": 101,
                    "detail": "target batch is not full",
                    "observed_at_epoch": observed,
                    "lease_until_epoch": observed + 180,
                },
            )
            streak: dict[int, int] = {}
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=rows),
                mock.patch.object(
                    watchdog, "screen_snapshot", return_value={screen: (100,)}
                ),
                mock.patch.object(
                    watchdog,
                    "retire_stale_chain",
                    return_value=(True, "retired exact stale chain"),
                ) as retire,
                mock.patch.object(
                    watchdog,
                    "probe_tcp_endpoint",
                    return_value=watchdog.NodeProbeResult(True, "reachable"),
                ),
                mock.patch.object(
                    watchdog, "launch_chain", return_value=(0, "started exact chain")
                ) as launch,
            ):
                first = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    idle_chain_streak=streak,
                )
                second = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    idle_chain_streak=streak,
                )
                third = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    idle_chain_streak=streak,
                )
            self.assertEqual(first["events"][0]["action"], "confirming")
            self.assertEqual(second["events"][0]["action"], "confirming")
            self.assertEqual(first["next_retry_seconds"], 15)
            self.assertEqual(second["next_retry_seconds"], 15)
            self.assertEqual(third["events"][0]["action"], "started")
            self.assertEqual(third["status"], "attention")
            retire.assert_called_once()
            launch.assert_called_once_with(
                ROOT, "batch0003", port, gpu, worker_index
            )

    def test_offline_stale_chain_is_left_intact_before_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            inventory = asset_root / "_asset_inventory"
            controls = inventory / "worker_control"
            controls.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            screen = watchdog.exact_chain_screen(port, gpu, worker_index)
            rows = (
                watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN"),
                watchdog.ProcessRow(
                    101,
                    100,
                    101,
                    "bash",
                    "bash blender/scripts/run_total_asset_pipeline.sh "
                    f"formal-slot-chain-loop batch0003 {port} {gpu} {worker_index}",
                ),
            )
            watchdog.atomic_write_json(
                controls
                / f"formal_slot_chain_p{port}_g{gpu}_w{worker_index}of11.json",
                {
                    "schema": watchdog.CHAIN_STATE_SCHEMA,
                    "status": "waiting_partial_batch",
                    "batch": "batch0003",
                    "remote_port": port,
                    "gpu": gpu,
                    "worker_index": worker_index,
                    "worker_count": 11,
                    "pid": 101,
                    "detail": "target batch is not full",
                    "observed_at_epoch": 1000.0,
                    "lease_until_epoch": 1100.0,
                },
            )
            plan = SimpleNamespace(malformed_status_lines=0, topology_errors=())
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=rows),
                mock.patch.object(
                    watchdog, "screen_snapshot", return_value={screen: (100,)}
                ),
                mock.patch.object(watchdog, "retire_stale_chain") as retire,
                mock.patch.object(watchdog, "launch_chain") as launch,
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT,
                    asset_root=asset_root,
                    launch=True,
                    idle_chain_streak={worker_index: 2},
                    node_probe=lambda _host, _port, _timeout: (
                        watchdog.NodeProbeResult(False, "route_unreachable")
                    ),
                    remote_tcp_host="render.example",
                    now_epoch=2000.0,
                )
            self.assertEqual(payload["events"][0]["action"], "node_unreachable")
            self.assertIn("route_unreachable", payload["events"][0]["detail"])
            retire.assert_not_called()
            launch.assert_not_called()

    def test_chain_yields_to_verified_managed_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            repair_batch = "batch0001_failed_repair_transport"
            repair = watchdog.WorkerClaim(
                repair_batch, port, gpu, worker_index, 11
            )
            chain_screen = watchdog.exact_chain_screen(
                port, gpu, worker_index
            )
            worker_screen = watchdog.exact_worker_screen(repair)
            finalizer_screen = f"{worker_screen}_finalizer"
            token = "total_asset_gpu_finalizer_v2_" + "c" * 64
            rows = (
                watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN chain"),
                watchdog.ProcessRow(
                    101, 100, 101, "bash",
                    "bash blender/scripts/run_total_asset_pipeline.sh "
                    f"formal-slot-chain-loop batch0016 {port} {gpu} {worker_index}",
                ),
                watchdog.ProcessRow(200, 1, 200, "SCREEN", "SCREEN worker"),
                watchdog.ProcessRow(
                    201, 200, 201, "Python",
                    "python3 blender/scripts/run_total_asset_render_worker.py "
                    f"--batch {repair_batch} --remote-port {port} --gpu {gpu} "
                    f"--worker-index {worker_index} --worker-count 11",
                ),
                watchdog.ProcessRow(300, 1, 300, "SCREEN", f"SCREEN {token}"),
                watchdog.ProcessRow(301, 300, 301, "bash", f"bash {token}"),
            )
            sessions = {
                chain_screen: (100,),
                worker_screen: (200,),
                finalizer_screen: (300,),
            }
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(
                    watchdog, "process_snapshot", return_value=rows
                ),
                mock.patch.object(
                    watchdog, "screen_snapshot", return_value=sessions
                ),
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=False
                )
            event = payload["events"][0]
            self.assertEqual(
                event["action"], "controller_healthy_remote_unverified"
            )
            self.assertIn("verified managed repair", event["detail"])
            self.assertEqual(payload["status"], "attention")

    def test_quarantined_slot_stays_parked_without_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text("asset_id\n")
            (port, gpu), worker_index = next(
                iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
            )
            watchdog.write_slot_quarantine(
                asset_root,
                remote_port=port,
                gpu=gpu,
                worker_index=worker_index,
                reason="vulkan_backend_unavailable",
            )
            plan = SimpleNamespace(
                malformed_status_lines=0,
                topology_errors=(),
                lowest_incomplete_batch="batch0002",
            )
            with (
                mock.patch.object(
                    watchdog,
                    "CANONICAL_WORKER_INDEX_BY_LOCATION",
                    {(port, gpu): worker_index},
                ),
                mock.patch.object(watchdog, "derive_plan", return_value=plan),
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(watchdog, "launch_chain") as launch,
            ):
                payload = watchdog.audit_and_repair(
                    project=ROOT, asset_root=asset_root, launch=True
                )
            self.assertEqual(payload["status"], "waiting")
            self.assertEqual(payload["events"][0]["action"], "waiting")
            self.assertIn("quarantined", payload["events"][0]["detail"])
            launch.assert_not_called()

    def test_w0_audit_python_heredoc_compiles(self) -> None:
        script = (SCRIPTS / "run_total_asset_pipeline.sh").read_text(encoding="utf-8")
        function_start = script.index("write_w0_continuation_audit()")
        next_function = script.index(
            "start_secondary_w0_formal_chain_after_audit()", function_start
        )
        block = script[function_start:next_function]
        marker = "python3 - <<'PY'\n"
        python_start = block.index(marker) + len(marker)
        python_end = block.index("\nPY\n", python_start)
        payload = block[python_start:python_end]
        compile(payload, "write_w0_continuation_audit", "exec")
        self.assertNotIn("start_secondary_w0_formal_chain_after_audit", payload)


if __name__ == "__main__":
    unittest.main()
