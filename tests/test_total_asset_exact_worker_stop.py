from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import sys


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import legacy_worker_boundary_drain as boundary
import total_asset_exact_worker_stop as exact_stop
import total_asset_scheduler as scheduler
import total_asset_slot_watchdog as watchdog
from total_asset_topology import CANONICAL_WORKER_INDEX_BY_LOCATION


class ExactFormalWorkerStopTests(unittest.TestCase):
    def setUp(self) -> None:
        (self.port, self.gpu), self.worker_index = sorted(
            CANONICAL_WORKER_INDEX_BY_LOCATION.items(), key=lambda item: item[1]
        )[0]
        self.batch = "batch0002"
        self.claim = scheduler.WorkerClaim(
            self.batch,
            self.port,
            self.gpu,
            self.worker_index,
            11,
        )
        self.chain_name = watchdog.exact_chain_screen(
            self.port, self.gpu, self.worker_index
        )
        self.worker_name = watchdog.exact_worker_screen(self.claim)
        self.worker_command = (
            "python3 blender/scripts/run_total_asset_render_worker.py "
            f"--batch {self.batch} --remote-port {self.port} --gpu {self.gpu} "
            f"--worker-index {self.worker_index} --worker-count 11"
        )
        self.chain_command = (
            "bash blender/scripts/run_total_asset_pipeline.sh "
            f"formal-slot-chain-loop batch0001 {self.port} {self.gpu} "
            f"{self.worker_index}"
        )
        self.rows = (
            watchdog.ProcessRow(100, 1, 100, "SCREEN", "SCREEN chain"),
            watchdog.ProcessRow(101, 100, 101, "bash", self.chain_command),
            watchdog.ProcessRow(200, 1, 200, "SCREEN", "SCREEN worker"),
            watchdog.ProcessRow(201, 200, 201, "python3", self.worker_command),
        )
        self.tokens = (
            f"100.{self.chain_name}",
            f"200.{self.worker_name}",
        )
        self.identities = {
            101: boundary.ProcessIdentity(
                101, 100, "S", "Mon Jul 20 01:00:00 2026", self.chain_command
            ),
            201: boundary.ProcessIdentity(
                201, 200, "S", "Mon Jul 20 01:01:00 2026", self.worker_command
            ),
        }

    def identity_reader(self, pid: int) -> boundary.ProcessIdentity | None:
        return self.identities.get(pid)

    def preflight(self, claims: tuple[scheduler.WorkerClaim, ...]) -> dict[str, object]:
        return {
            "schema_version": exact_stop.REMOTE_PREFLIGHT_SCHEMA_VERSION,
            "scope": "launch_slots",
            "target_batch": self.batch,
            "worker_count": 11,
            "observed_at_epoch": time.time(),
            "local_claim_count": len(claims),
            "local_claims_digest": scheduler.worker_claims_digest(claims),
            "launch_layout": [{
                "port": self.port,
                "gpu": self.gpu,
                "worker_index": self.worker_index,
            }],
            "nodes": [{
                "port": self.port,
                "reachable": True,
                "gpus": [{
                    "gpu": self.gpu,
                    "physical_binding_ok": False,
                    "reason_codes": ["physical_gpu_binding_failed"],
                    "claim_count": 1,
                    "claim_state": "compatible_target",
                    "compute_process_count": 1,
                    "compute_process_kinds": ["blender"],
                    "wrapper_process_present": True,
                }],
            }],
        }

    def test_validates_unique_legacy_chain_worker_and_rejects_drain_aware_worker(
        self,
    ) -> None:
        target = exact_stop.validate_exact_target(
            self.rows,
            self.tokens,
            batch=self.batch,
            remote_port=self.port,
            gpu=self.gpu,
            worker_index=self.worker_index,
            identity_reader=self.identity_reader,
            worker_assertion=lambda *_args, **_kwargs: self.worker_command,
        )
        self.assertEqual(target.worker_pid, 201)
        self.assertEqual(target.chain_pid, 101)

        drain_aware = self.worker_command + " --drain-file /tmp/exact.drain.json"
        with self.assertRaisesRegex(
            exact_stop.ExactWorkerStopError, "cooperative --drain-file"
        ):
            exact_stop.validate_exact_target(
                self.rows,
                self.tokens,
                batch=self.batch,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                identity_reader=self.identity_reader,
                worker_assertion=lambda *_args, **_kwargs: drain_aware,
            )

    def test_misbound_preflight_must_be_fresh_scoped_and_exact(self) -> None:
        claims = (self.claim,)
        report = self.preflight(claims)
        exact_stop.validate_misbound_preflight(
            report,
            target=self.claim,
            current_claims=claims,
        )
        report["nodes"][0]["gpus"][0]["physical_binding_ok"] = True  # type: ignore[index]
        with self.assertRaisesRegex(
            exact_stop.ExactWorkerStopError, "physically misbound"
        ):
            exact_stop.validate_misbound_preflight(
                report,
                target=self.claim,
                current_claims=claims,
            )

    def test_exact_chain_is_stopped_before_only_the_captured_worker_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            inventory.mkdir()
            drain = scheduler.create_drain_request(inventory, self.batch, 11)
            preflight = root / "preflight.json"
            preflight.write_text(
                json.dumps(self.preflight((self.claim,))), encoding="utf-8"
            )
            state = root / "stop-state.json"
            rows_without_chain = tuple(row for row in self.rows if row.pid != 101)
            rows_without_worker = tuple(
                row for row in rows_without_chain if row.pid != 201
            )
            process_rows = iter((
                self.rows,
                rows_without_chain,
                rows_without_chain,
                rows_without_worker,
            ))
            screen_tokens = iter((
                self.tokens,
                (f"200.{self.worker_name}",),
                (f"200.{self.worker_name}",),
            ))
            actions: list[str] = []
            child = boundary.ProcessIdentity(
                202, 201, "S", "Mon Jul 20 01:01:01 2026", "ssh exact-child"
            )

            def quit_controller(*_args, **_kwargs) -> None:
                actions.append("quit_chain")

            def terminate_tree(
                root_identity: boundary.ProcessIdentity,
                *,
                tracked: dict[int, boundary.ProcessIdentity],
                term_grace_seconds: float,
                kill_grace_seconds: float,
            ) -> None:
                self.assertEqual(actions, ["quit_chain"])
                self.assertEqual(root_identity, self.identities[201])
                self.assertEqual(set(tracked), {201, 202})
                self.assertEqual(term_grace_seconds, 30)
                self.assertEqual(kill_grace_seconds, 10)
                actions.append("term_worker_tree")

            result = exact_stop.stop_exact_uncooperative_worker(
                inventory=inventory,
                batch=self.batch,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                drain_request_id=str(drain["request_id"]),
                remote_preflight_json=preflight,
                state_path=state,
                process_reader=lambda: next(process_rows),
                token_reader=lambda _screen: next(screen_tokens),
                identity_reader=self.identity_reader,
                worker_assertion=lambda *_args, **_kwargs: self.worker_command,
                tree_discoverer=lambda _root: {
                    201: self.identities[201],
                    202: child,
                },
                tree_terminator=terminate_tree,
                controller_quitter=quit_controller,
            )
            self.assertEqual(actions, ["quit_chain", "term_worker_tree"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["terminated_tree_pids"], [201, 202])
            persisted = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "complete")
            self.assertEqual(persisted["drain_request_id"], drain["request_id"])

    def test_wrong_drain_generation_performs_no_process_or_screen_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            inventory.mkdir()
            scheduler.create_drain_request(inventory, self.batch, 11)
            preflight = root / "preflight.json"
            preflight.write_text("{}", encoding="utf-8")
            process_reader = mock.Mock()
            quitter = mock.Mock()
            terminator = mock.Mock()
            with self.assertRaisesRegex(
                exact_stop.ExactWorkerStopError, "request_id differs"
            ):
                exact_stop.stop_exact_uncooperative_worker(
                    inventory=inventory,
                    batch=self.batch,
                    remote_port=self.port,
                    gpu=self.gpu,
                    worker_index=self.worker_index,
                    drain_request_id="f" * 32,
                    remote_preflight_json=preflight,
                    state_path=root / "state.json",
                    process_reader=process_reader,
                    controller_quitter=quitter,
                    tree_terminator=terminator,
                )
            process_reader.assert_not_called()
            quitter.assert_not_called()
            terminator.assert_not_called()

    def test_pipeline_exposes_only_guarded_exact_stop_command(self) -> None:
        shell = (PROJECT / "blender/scripts/run_total_asset_pipeline.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("formal-slot-exact-stop", shell)
        function_start = shell.index("stop_exact_uncooperative_formal_worker()")
        function_end = shell.index("\n}\n", function_start)
        source = shell[function_start:function_end]
        self.assertIn("--drain-request-id", source)
        self.assertIn("--remote-preflight-json", source)
        self.assertNotIn("pkill", source)
        self.assertNotIn("killall", source)


if __name__ == "__main__":
    unittest.main()
