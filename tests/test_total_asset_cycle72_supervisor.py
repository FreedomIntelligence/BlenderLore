from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_cycle72 as cycle72  # noqa: E402
import total_asset_cycle72_supervisor as supervisor  # noqa: E402
from total_asset_topology import CANONICAL_WORKER_INDEX_BY_LOCATION  # noqa: E402


BOOT = "11111111-2222-3333-4444-555555555555"
NEW_BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class TotalAssetCycle72SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.inventory.mkdir()
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.catalog.write_text("asset_id\n", encoding="utf-8")
        self.state_root = self.root / "state"
        self.guard_state = self.root / "holder_guard.json"
        self.identity = self.root / "id_ed25519"
        self.identity.write_text("fake", encoding="utf-8")
        self.identity.chmod(0o600)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text("host key\n", encoding="utf-8")
        self.config = supervisor.SupervisorConfig(
            project=PROJECT,
            inventory=self.inventory,
            catalog=self.catalog,
            db_path=self.state_root / "leases.sqlite3",
            state_path=self.state_root / "supervisor.json",
            lock_path=self.state_root / "supervisor.lock",
            checkpoint_dir=self.state_root / "checkpoints",
            runtime_root=self.inventory / "worker_control/cycle72_runtime",
            log_root=self.inventory / "logs/cycle72",
            transaction_root=self.state_root / "transactions",
            holder_guard_state=self.guard_state,
            slot_lock_root=self.root / "slot-locks",
            ssh_host="root@example.invalid",
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            poll_seconds=45,
            guard_max_age_seconds=90,
            lease_seconds=600,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_v1_database_with_active_lease(self) -> dict[str, object]:
        self.state_root.mkdir(parents=True, exist_ok=True)
        cycle72.initialize_database(self.config.db_path, self.inventory)
        lease = self.lease(asset="migration-asset")
        with sqlite3.connect(self.config.db_path) as connection:
            connection.execute(
                """INSERT INTO cycles(
                       cycle_id, schema_name, state, started_at_epoch,
                       checkpoint_at_epoch, config_json
                   ) VALUES(?, 'production_cycle.v1', 'running', 1, 2, '{}')""",
                (lease["cycle_id"],),
            )
            fields = tuple(sorted(cycle72._V1_REQUIRED_LEASE_COLUMNS))
            values = {
                **lease,
                "identity_key": lease.get("identity_key") or "migration-identity",
                "render_order": int(lease.get("render_order") or 1),
                "render_batch": str(lease.get("render_batch") or "0003"),
                "attempt": int(lease.get("attempt") or 1),
                "recovery_count": int(lease.get("recovery_count") or 0),
                "claimed_at_epoch": 10.0,
                "heartbeat_at_epoch": 11.0,
                "expires_at_epoch": 999.0,
                "completed_at_epoch": None,
                "released_at_epoch": None,
                "release_reason": None,
                "authority_generation": "authority-test",
                "knowledge_generation": "knowledge-test",
            }
            connection.execute(
                f"INSERT INTO leases({', '.join(fields)}) "
                f"VALUES({', '.join('?' for _ in fields)})",
                tuple(values[field] for field in fields),
            )
            connection.execute("DROP TABLE priority_work_items")
            connection.execute("DROP TABLE priority_workloads")
            connection.execute("ALTER TABLE leases RENAME TO leases_v2_backup")
            connection.execute(
                """CREATE TABLE leases (
                       lease_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
                       identity_key TEXT NOT NULL, render_order INTEGER NOT NULL,
                       render_batch TEXT NOT NULL, cycle_id TEXT NOT NULL,
                       worker_index INTEGER NOT NULL, remote_port INTEGER NOT NULL,
                       gpu INTEGER NOT NULL, node_boot_id TEXT NOT NULL,
                       state TEXT NOT NULL, attempt INTEGER NOT NULL,
                       recovery_count INTEGER NOT NULL DEFAULT 0,
                       claimed_at_epoch REAL NOT NULL, heartbeat_at_epoch REAL NOT NULL,
                       expires_at_epoch REAL NOT NULL, completed_at_epoch REAL,
                       released_at_epoch REAL, release_reason TEXT,
                       authority_generation TEXT NOT NULL,
                       knowledge_generation TEXT NOT NULL
                   )"""
            )
            connection.execute(
                f"INSERT INTO leases({', '.join(fields)}) "
                f"SELECT {', '.join(fields)} FROM leases_v2_backup"
            )
            connection.execute("DROP TABLE leases_v2_backup")
            connection.execute(
                "UPDATE metadata SET value='1' WHERE key='db_schema_version'"
            )
            connection.commit()
        return lease

    def write_guard(
        self,
        *,
        now: float = 1000.0,
        default_action: str = "holder_ready",
        actions: dict[int, str] | None = None,
        boots: dict[int, str | None] | None = None,
        details: dict[int, str] | None = None,
    ) -> None:
        actions = actions or {}
        boots = boots or {}
        details = details or {}
        events = []
        for (port, gpu), worker in sorted(
            CANONICAL_WORKER_INDEX_BY_LOCATION.items(), key=lambda item: item[1]
        ):
            action = actions.get(worker, default_action)
            boot = boots.get(worker, BOOT)
            events.append(
                {
                    "remote_port": port,
                    "gpu": gpu,
                    "worker_index": worker,
                    "action": action,
                    "detail": details.get(worker, action),
                    "boot_id": boot,
                }
            )
        self.guard_state.write_text(
            json.dumps(
                {
                    "schema": supervisor.HOLDER_GUARD_SCHEMA,
                    "observed_at_epoch": now,
                    "events": events,
                }
            ),
            encoding="utf-8",
        )

    def lease(
        self,
        worker: int = 0,
        *,
        boot: str = BOOT,
        asset: str = "asset-3001",
    ) -> dict[str, object]:
        location = next(
            location
            for location, index in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if index == worker
        )
        return {
            "lease_id": f"{worker + 1:032x}",
            "asset_id": asset,
            "identity_key": f"identity-{asset}",
            "render_order": 3001 + worker,
            "render_batch": "0003",
            "cycle_id": "cycle-test",
            "worker_index": worker,
            "remote_port": location[0],
            "gpu": location[1],
            "node_boot_id": boot,
            "knowledge_generation": "knowledge-v10",
            "state": "active",
        }

    def catalog_row(
        self,
        lease: dict[str, object],
        *,
        render_engine_hint: str = "CYCLES",
    ) -> dict[str, str]:
        return {
            "asset_id": str(lease["asset_id"]),
            "identity_key": str(lease["identity_key"]),
            "model_file": str(self.root / f"{lease['asset_id']}.blend"),
            "model_format": ".blend",
            "inventory_status": "ready",
            "duplicate_of": "",
            "render_order": str(lease["render_order"]),
            "render_batch": str(lease["render_batch"]),
            "render_engine_hint": render_engine_hint,
        }

    def authority_snapshot(
        self, *rows: dict[str, str]
    ) -> cycle72.AuthoritySnapshot:
        return cycle72.AuthoritySnapshot(
            tuple(rows),
            {},
            "authority-test",
            self.catalog.resolve(),
            self.inventory.resolve(),
            None,
            1000,
            1000.0,
        )

    def write_runtime(
        self,
        lease: dict[str, object],
        *,
        status: str = "failed",
        category: str = "remote_transport",
        code: str = "worker_ssh_timeout",
        attempted: int = 0,
    ) -> None:
        path = supervisor.runtime_state_path(self.config, lease)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": status,
                    "assignment_mode": supervisor.ASSIGNMENT_MODE,
                    "cycle_id": lease["cycle_id"],
                    "lease_id": lease["lease_id"],
                    "worker_index": lease["worker_index"],
                    "remote_port": lease["remote_port"],
                    "gpu": lease["gpu"],
                    "node_boot_id": lease["node_boot_id"],
                    "attempted": attempted,
                    "failure_category": category,
                    "failure_code": code,
                    "failure_stage": "worker_initialization",
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def status_payload(
        leases: list[dict[str, object]], *, checkpoint_due: bool = False
    ) -> dict[str, object]:
        return {
            "cycle": {
                "cycle_id": "cycle-test",
                "state": "running",
                "checkpoint_at_epoch": 999.0,
            },
            "active_leases": leases,
            "checkpoint_due": checkpoint_due,
        }

    @staticmethod
    def no_worker_runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["screen", "-ls"]:
            return subprocess.CompletedProcess(command, 1, "No Sockets found.\n", "")
        if command[:4] == ["ps", "axww", "-o", "pid=,command="]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    def test_guard_snapshot_requires_fresh_complete_v2_topology(self) -> None:
        self.write_guard(now=1000.0)
        slots = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1040.0, max_age_seconds=90
        )
        self.assertEqual(set(slots), set(range(11)))
        self.assertTrue(slots[0].holder_verified)
        self.assertFalse(slots[0].handoff_verified)
        self.write_guard(now=1000.0, actions={0: "handoff_ready"})
        handoff_slots = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1040.0, max_age_seconds=90
        )
        self.assertTrue(handoff_slots[0].holder_verified)
        self.assertTrue(handoff_slots[0].handoff_verified)
        with self.assertRaisesRegex(supervisor.SupervisorError, "stale"):
            supervisor.load_guard_slots(
                self.guard_state, now_epoch=1100.0, max_age_seconds=90
            )

    def test_supervisor_health_requires_every_gpu_to_be_productive(self) -> None:
        self.write_guard(now=1000.0, default_action="worker_active")
        guard = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1000.0, max_age_seconds=90
        )
        payload = supervisor.build_supervisor_state(
            self.config,
            now_epoch=1000.0,
            cycle_payload=self.status_payload([]),
            guard_slots=guard,
            slot_states={
                worker: ("productive", "exact_worker_heartbeat", None)
                for worker in guard
            },
            completed={},
            infrastructure_retries={},
            legacy_workers=[],
            checkpoint_path=None,
        )
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["productive_gpu_count"], 11)
        self.assertEqual(payload["holder_gpu_count"], 0)
        self.assertEqual(payload["offline_gpu_count"], 0)
        self.assertEqual(payload["blocked_gpu_count"], 0)

    def test_verified_holders_report_protected_ramping_not_healthy(self) -> None:
        self.write_guard(now=1000.0)
        guard = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1000.0, max_age_seconds=90
        )
        payload = supervisor.build_supervisor_state(
            self.config,
            now_epoch=1000.0,
            cycle_payload=self.status_payload([]),
            guard_slots=guard,
            slot_states={
                worker: ("holder", "no_compatible_ready_asset", None)
                for worker in guard
            },
            completed={},
            infrastructure_retries={},
            legacy_workers=[],
            checkpoint_path=None,
        )
        self.assertEqual(payload["status"], "protected_ramping")
        self.assertEqual(payload["productive_gpu_count"], 0)
        self.assertEqual(payload["holder_gpu_count"], 11)
        self.assertEqual(payload["offline_gpu_count"], 0)
        self.assertEqual(payload["blocked_gpu_count"], 0)

    def test_adaptive_poll_keeps_productive_steady_state_at_configured_cadence(self) -> None:
        payload = {
            "slots": [
                {
                    "state": "productive",
                    "detail": "exact_worker_heartbeat",
                    "guard_action": "worker_active",
                    "lease_id": "lease-1",
                }
            ]
        }
        self.assertEqual(supervisor.next_supervisor_poll_seconds(payload, 45), 45)

    def test_adaptive_poll_rechecks_attested_handoff_and_preparation_within_five_seconds(self) -> None:
        for action in ("handoff_ready", "worker_preparing", "handoff_released"):
            with self.subTest(action=action):
                payload = {
                    "slots": [
                        {
                            "state": "blocked",
                            "detail": "controller_intent_is_not_productive",
                            "guard_action": action,
                            "lease_id": "lease-1",
                        }
                    ]
                }
                self.assertEqual(
                    supervisor.next_supervisor_poll_seconds(payload, 45), 5
                )

    def test_adaptive_poll_rechecks_lease_free_ramp_wait_and_recent_exit(self) -> None:
        details = (
            "global_preparation_concurrency_wait:active=1:limit=1",
            "exact_worker_start_or_exit_ambiguous",
            "worker_started_awaiting_remote_evidence",
        )
        for detail in details:
            with self.subTest(detail=detail):
                payload = {
                    "slots": [
                        {
                            "state": "holder",
                            "detail": detail,
                            "guard_action": "holder_ready",
                            "lease_id": None,
                        }
                    ]
                }
                self.assertEqual(
                    supervisor.next_supervisor_poll_seconds(payload, 30), 5
                )

    def test_adaptive_poll_does_not_busy_loop_persistent_supply_or_infrastructure_blocks(self) -> None:
        for detail in (
            "no_compatible_ready_asset",
            "infrastructure_retry_scheduled:ssh_transport_error",
            "waiting_bootstrap_attestation",
        ):
            with self.subTest(detail=detail):
                payload = {
                    "slots": [
                        {
                            "state": "holder",
                            "detail": detail,
                            "guard_action": "holder_ready",
                            "lease_id": None,
                        }
                    ]
                }
                self.assertEqual(
                    supervisor.next_supervisor_poll_seconds(payload, 45), 45
                )

    def test_adaptive_poll_rechecks_ambiguous_exit_recovery_within_fifteen_seconds(self) -> None:
        for detail, action, lease_id in (
            ("remote_busy_or_ambiguous", "remote_busy_or_ambiguous", None),
            ("local_owner", "local_owner", "lease-1"),
            (
                "worker_remote_evidence_missing:holder_ready",
                "holder_ready",
                "lease-1",
            ),
        ):
            with self.subTest(detail=detail):
                payload = {
                    "slots": [{
                        "state": "blocked",
                        "detail": detail,
                        "guard_action": action,
                        "lease_id": lease_id,
                    }]
                }
                self.assertEqual(
                    supervisor.next_supervisor_poll_seconds(payload, 45), 15
                )

    def test_unattested_holder_intent_is_reported_offline_or_blocked(self) -> None:
        actions = {
            **{worker: "holder_ready" for worker in range(4)},
            **{worker: "probe_blocked" for worker in range(4, 8)},
            **{worker: "local_owner" for worker in range(8, 11)},
        }
        boots = {worker: None for worker in range(4, 8)}
        self.write_guard(now=1000.0, actions=actions, boots=boots)
        guard = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1000.0, max_age_seconds=90
        )
        payload = supervisor.build_supervisor_state(
            self.config,
            now_epoch=1000.0,
            cycle_payload=self.status_payload([]),
            guard_slots=guard,
            slot_states={
                worker: ("holder", "waiting_bootstrap_attestation", None)
                for worker in guard
            },
            completed={},
            infrastructure_retries={},
            legacy_workers=[],
            checkpoint_path=None,
        )
        self.assertEqual(payload["status"], "attention")
        self.assertEqual(payload["productive_gpu_count"], 0)
        self.assertEqual(payload["holder_gpu_count"], 4)
        self.assertEqual(payload["offline_gpu_count"], 4)
        self.assertEqual(payload["blocked_gpu_count"], 3)

    def test_exhausted_infrastructure_retry_forces_attention(self) -> None:
        self.write_guard(now=1000.0, default_action="worker_active")
        guard = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1000.0, max_age_seconds=90
        )
        payload = supervisor.build_supervisor_state(
            self.config,
            now_epoch=1000.0,
            cycle_payload=self.status_payload([]),
            guard_slots=guard,
            slot_states={
                worker: ("productive", "exact_worker_heartbeat", None)
                for worker in guard
            },
            completed={},
            infrastructure_retries={"asset": {"exhausted": True}},
            legacy_workers=[],
            checkpoint_path=None,
        )
        self.assertEqual(payload["status"], "attention")
        self.assertEqual(payload["productive_gpu_count"], 11)

    def test_worker_command_is_exact_dynamic_one_asset_contract(self) -> None:
        lease = self.lease()
        command = supervisor.build_worker_command(self.config, lease)
        rendered = " ".join(command)
        self.assertIn("--assignment-mode dynamic_compatible_v1", rendered)
        self.assertIn("--worker-index 0 --worker-count 11", rendered)
        self.assertIn("--asset-id asset-3001", rendered)
        self.assertIn("--resume-unprocessed-only", command)
        self.assertIn("--defer-holder-to-shared-lock", command)
        self.assertNotIn("--runtime-aware-partition", command)
        self.assertTrue(supervisor.command_matches_lease(rendered, lease))
        self.assertFalse(
            supervisor.command_matches_lease(
                rendered.replace(" --defer-holder-to-shared-lock", ""), lease
            )
        )
        self.assertFalse(
            supervisor.command_matches_lease(
                rendered.replace("--asset-id asset-3001", "--asset-id other"),
                lease,
            )
        )

    def test_worker_command_and_cwd_can_remain_inside_runtime_generation(self) -> None:
        generation = self.root / "runtime" / ("a" * 64)
        config = replace(self.config, project=generation)
        command = supervisor.build_worker_command(config, self.lease())
        self.assertEqual(
            command[1],
            str(generation / "blender/scripts/run_total_asset_render_worker.py"),
        )

    def test_dynamic_claim_capability_routes_legacy_eevee_only_to_gpu0(self) -> None:
        model = self.root / "legacy.blend"
        model.write_bytes(b"BLENDER-v306" + b"\0" * 64)
        row = {
            "model_file": str(model),
            "model_format": ".blend",
            "render_engine_hint": "BLENDER_EEVEE",
        }
        secondary_port = next(
            port
            for (port, gpu), worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 0 and gpu == 0
        )
        self.assertTrue(
            supervisor.DynamicSlotCapabilities(secondary_port, 0).accepts(row)
        )
        self.assertFalse(
            supervisor.DynamicSlotCapabilities(secondary_port, 1).accepts(row)
        )
        self.assertTrue(
            supervisor.DynamicSlotCapabilities(secondary_port, 1).accepts(
                {**row, "render_engine_hint": "CYCLES"}
            )
        )
        attested = supervisor.DynamicSlotCapabilities(
            secondary_port,
            1,
            cycle72.SlotCapabilities.normalized(
                attested_vulkan_families=["4.5"]
            ),
        )
        modern = self.root / "modern.blend"
        modern.write_bytes(b"BLENDER-v405" + b"\0" * 64)
        self.assertTrue(attested.accepts({
            **row,
            "model_file": str(modern),
            "render_engine_hint": "BLENDER_EEVEE_NEXT",
        }))
        self.assertEqual(attested.attested_vulkan_families, ("4.5",))

    def test_dynamic_claim_capability_makes_30773_cycles_only_on_every_gpu(self) -> None:
        primary_port = next(
            port
            for (port, gpu), worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 4 and gpu == 0
        )
        modern = self.root / "modern.blend"
        modern.write_bytes(b"BLENDER-v405" + b"\0" * 64)
        source_row = {
            "model_file": str(modern),
            "model_format": ".blend",
            "render_engine_hint": "BLENDER_EEVEE_NEXT",
        }
        cycles_row = {**source_row, "render_engine_hint": "CYCLES"}

        for gpu in range(4):
            capability = supervisor.DynamicSlotCapabilities(primary_port, gpu)
            self.assertFalse(capability.accepts(source_row), gpu)
            self.assertTrue(capability.accepts(cycles_row), gpu)

    def test_profile_capability_probe_requires_exact_standalone_holder(self) -> None:
        location = next(
            location
            for location, worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 1
        )
        config = replace(
            self.config,
            profile_capability_state=self.state_root / "profile-capabilities.json",
        )
        evidence = supervisor.GuardSlotEvidence(
            location[0], location[1], 1, "holder_ready", "holder_ready", BOOT, 1000.0
        )
        expected = cycle72.SlotCapabilities.normalized(
            attested_vulkan_families=("4.5",)
        )
        with mock.patch.object(
            supervisor, "strict_slot_capabilities", return_value=expected
        ) as probe:
            actual = supervisor.live_slot_capabilities(
                config=config, evidence=evidence, now_epoch=1000.0
            )
        self.assertEqual(actual, expected)
        self.assertTrue(probe.call_args.kwargs["exact_holder_attested"])
        self.assertEqual(probe.call_args.kwargs["expected_boot_id"], BOOT)

        for action in ("handoff_ready", "local_owner", "worker_active"):
            with self.subTest(action=action), mock.patch.object(
                supervisor,
                "strict_slot_capabilities",
                return_value=cycle72.SlotCapabilities.normalized(),
            ) as blocked_probe:
                supervisor.live_slot_capabilities(
                    config=config,
                    evidence=replace(evidence, action=action),
                    now_epoch=1000.0,
                )
                self.assertFalse(
                    blocked_probe.call_args.kwargs["exact_holder_attested"]
                )

    def test_attested_profile_families_are_passed_to_dynamic_claim(self) -> None:
        location = next(
            location
            for location, worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 1
        )
        evidence = supervisor.GuardSlotEvidence(
            location[0], location[1], 1, "holder_ready", "holder_ready", BOOT, 1000.0
        )
        claimed: list[object] = []
        capabilities_seen: list[object] = []

        def claim(**kwargs: object):
            claimed.append(kwargs)
            capabilities_seen.append(kwargs["capabilities"])
            return None

        dep = supervisor.Dependencies(
            claim=claim,
            slot_capabilities=lambda **_kwargs: cycle72.SlotCapabilities.normalized(
                attested_vulkan_families=("4.5", "5.1")
            ),
            run_process=self.no_worker_runner,
        )
        result = supervisor._supervise_one_slot(
            self.config,
            evidence=evidence,
            lease=None,
            cycle_state="running",
            authority=supervisor.AuthorityCursor(object(), lambda: object()),
            retries={},
            excluded_assets=set(),
            legacy_claim_barrier=False,
            bootstrap_ready=True,
            dependency=dep,
            now_epoch=1000.0,
        )
        self.assertEqual(result, ("holder", "no_compatible_ready_asset", None))
        self.assertEqual(len(claimed), 1)
        self.assertEqual(
            capabilities_seen[0].attested_vulkan_families,
            ("4.5", "5.1"),
        )

    def test_untrusted_bare_holder_does_not_invoke_profile_probe(self) -> None:
        location = next(
            location
            for location, worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 1
        )
        evidence = supervisor.GuardSlotEvidence(
            location[0], location[1], 1, "local_owner", "legacy bare holder", BOOT, 1000.0
        )
        dep = supervisor.Dependencies(
            slot_capabilities=lambda **_kwargs: self.fail(
                "untrusted holder must not probe profiles"
            ),
            claim=lambda **_kwargs: self.fail("untrusted holder cannot claim"),
            run_process=self.no_worker_runner,
        )
        result = supervisor._supervise_one_slot(
            self.config,
            evidence=evidence,
            lease=None,
            cycle_state="running",
            authority=supervisor.AuthorityCursor(object(), lambda: object()),
            retries={},
            excluded_assets=set(),
            legacy_claim_barrier=False,
            bootstrap_ready=True,
            dependency=dep,
            now_epoch=1000.0,
        )
        self.assertEqual(result, ("blocked", "local_owner", None))

    def test_active_exact_worker_is_heartbeated_without_new_claim(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_active"})
        lease = self.lease()
        name = supervisor.exact_worker_screen_name(lease)
        worker_command = " ".join(supervisor.build_worker_command(self.config, lease))

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["screen", "-ls"]:
                return subprocess.CompletedProcess(command, 0, f"123.{name}\t(Detached)\n", "")
            return subprocess.CompletedProcess(command, 0, f"999 {worker_command}\n", "")

        heartbeats: list[dict[str, object]] = []
        claims: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(kwargs),
            heartbeat=lambda **kwargs: heartbeats.append(kwargs) or lease,
            complete=lambda **_kwargs: self.fail("active worker must not complete"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        slot = payload["slots"][0]
        self.assertEqual(slot["state"], "productive")
        self.assertEqual(len(heartbeats), 1)
        # Ten holder-only slots can ask for work; the leased slot cannot.
        self.assertEqual({item["worker_index"] for item in claims}, set(range(1, 11)))

    def test_safe_ramp_attempts_only_one_handoff_globally_by_default(self) -> None:
        self.write_guard(now=1000.0)
        claimed_workers: list[int] = []

        def claim(**kwargs: object) -> dict[str, object]:
            worker = int(kwargs["worker_index"])
            claimed_workers.append(worker)
            return self.lease(worker, asset=f"asset-ramp-{worker}")

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("no existing worker"),
            complete=lambda **_kwargs: self.fail("no existing worker"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor, "launch_exact_worker", return_value=True
        ) as launch:
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(claimed_workers, [0])
        self.assertEqual(
            [int(call.args[2]["worker_index"]) for call in launch.call_args_list],
            [0],
        )
        for worker in (1, 2, 3):
            self.assertEqual(
                payload["slots"][worker]["detail"],
                "node_preparation_concurrency_wait:active=1:limit=1",
            )
        for worker in (4, 5, 6, 7, 8, 9, 10):
            self.assertEqual(
                payload["slots"][worker]["detail"],
                "global_preparation_concurrency_wait:active=1:limit=1",
            )
        self.assertEqual(
            payload["global_preparation_concurrency"],
            {"active": 1, "limit": 1},
        )

    def test_global_preparation_override_remains_bounded_by_node_gate(self) -> None:
        self.write_guard(now=1000.0)
        config = replace(self.config, global_preparation_limit=2)
        claimed_workers: list[int] = []

        def claim(**kwargs: object) -> dict[str, object]:
            worker = int(kwargs["worker_index"])
            claimed_workers.append(worker)
            return self.lease(worker, asset=f"asset-global-two-{worker}")

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor, "launch_exact_worker", return_value=True
        ) as launch:
            payload = supervisor.supervise_once(config, dependency=dep)

        self.assertEqual(claimed_workers, [0, 4])
        self.assertEqual(
            [int(call.args[2]["worker_index"]) for call in launch.call_args_list],
            [0, 4],
        )
        self.assertEqual(
            payload["global_preparation_concurrency"],
            {"active": 2, "limit": 2},
        )
        self.assertEqual(
            payload["slots"][8]["detail"],
            "global_preparation_concurrency_wait:active=2:limit=2",
        )

    def test_full_preparation_limits_launch_every_slot_without_node_poll_barrier(self) -> None:
        self.write_guard(now=1000.0)
        node_limits = tuple(
            (port, count)
            for port, count in supervisor._canonical_node_gpu_counts().items()
        )
        config = replace(
            self.config,
            node_preparation_limits=node_limits,
            global_preparation_limit=11,
        )
        claimed_workers: list[int] = []

        def claim(**kwargs: object) -> dict[str, object]:
            worker = int(kwargs["worker_index"])
            claimed_workers.append(worker)
            return self.lease(worker, asset=f"asset-full-ramp-{worker}")

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor, "launch_exact_worker", return_value=True
        ) as launch:
            payload = supervisor.supervise_once(config, dependency=dep)

        self.assertEqual(claimed_workers, list(range(11)))
        self.assertEqual(
            [int(call.args[2]["worker_index"]) for call in launch.call_args_list],
            list(range(11)),
        )
        self.assertEqual(
            payload["global_preparation_concurrency"],
            {"active": 11, "limit": 11},
        )
        self.assertNotIn(
            "node_handoff_ramp_wait",
            {item["detail"] for item in payload["slots"]},
        )

    def test_existing_source_preparation_blocks_claims_on_same_node(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_preparing"})
        preparing = self.lease(0, asset="asset-source-transfer")
        claims: list[int] = []

        def audit(lease: dict[str, object], **_kwargs: object):
            if int(lease["worker_index"]) == 0:
                return supervisor.LocalWorkerEvidence("exact", 1, 1)
            return supervisor.LocalWorkerEvidence("exact", 0, 0)

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([preparing]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(int(kwargs["worker_index"])),
            heartbeat=lambda **_kwargs: preparing,
            complete=lambda **_kwargs: self.fail("preparing worker has no outcome"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(supervisor, "audit_local_worker", side_effect=audit):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(claims, [])
        for worker in range(1, 4):
            self.assertEqual(
                payload["slots"][worker]["detail"],
                "node_preparation_concurrency_wait:active=1:limit=1",
            )
        port = int(preparing["remote_port"])
        self.assertEqual(
            payload["node_preparation_concurrency"][str(port)],
            {"active": 1, "limit": 1},
        )
        other_port = int(self.lease(4)["remote_port"])
        self.assertEqual(
            payload["slots"][4]["detail"],
            "global_preparation_concurrency_wait:active=1:limit=1",
        )
        self.assertNotEqual(other_port, port)
        self.assertEqual(
            payload["global_preparation_concurrency"],
            {"active": 1, "limit": 1},
        )

    def test_productive_worker_does_not_consume_preparation_capacity(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_active"})
        active = self.lease(0, asset="asset-rendering")
        next_lease = self.lease(1, asset="asset-next")
        claims: list[int] = []

        def claim(**kwargs: object) -> dict[str, object] | None:
            worker = int(kwargs["worker_index"])
            claims.append(worker)
            return next_lease if worker == 1 else None

        def audit(lease: dict[str, object], **_kwargs: object):
            if int(lease["worker_index"]) == 0:
                return supervisor.LocalWorkerEvidence("exact", 1, 1)
            return supervisor.LocalWorkerEvidence("exact", 0, 0)

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([active]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            heartbeat=lambda **_kwargs: active,
            complete=lambda **_kwargs: self.fail("active worker cannot complete"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: next_lease,
            run_process=self.no_worker_runner,
        )
        with (
            mock.patch.object(supervisor, "audit_local_worker", side_effect=audit),
            mock.patch.object(
                supervisor, "launch_exact_worker", return_value=True
            ) as launch,
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertIn(1, claims)
        self.assertEqual(int(launch.call_args.args[2]["worker_index"]), 1)
        self.assertEqual(payload["slots"][0]["state"], "productive")

    def test_node_without_productive_worker_gets_global_ramp_first(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_active"})
        active = self.lease(0, asset="asset-rendering")
        empty_node_lease = self.lease(4, asset="asset-empty-node-first")
        claims: list[int] = []
        heartbeats: list[int] = []

        def claim(**kwargs: object) -> dict[str, object] | None:
            worker = int(kwargs["worker_index"])
            claims.append(worker)
            return empty_node_lease if worker == 4 else None

        def audit(lease: dict[str, object], **_kwargs: object):
            if int(lease["worker_index"]) == 0:
                return supervisor.LocalWorkerEvidence("exact", 1, 1)
            return supervisor.LocalWorkerEvidence("exact", 0, 0)

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([active]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            heartbeat=lambda **kwargs: heartbeats.append(
                int(kwargs["worker_index"])
            ) or active,
            complete=lambda **_kwargs: self.fail("active worker cannot complete"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: empty_node_lease,
            run_process=self.no_worker_runner,
        )
        with (
            mock.patch.object(supervisor, "audit_local_worker", side_effect=audit),
            mock.patch.object(
                supervisor, "launch_exact_worker", return_value=True
            ) as launch,
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(claims, [4])
        self.assertEqual(int(launch.call_args.args[2]["worker_index"]), 4)
        self.assertEqual(heartbeats, [0])
        self.assertEqual(payload["slots"][0]["state"], "productive")
        self.assertEqual(
            payload["slots"][8]["detail"],
            "global_preparation_concurrency_wait:active=1:limit=1",
        )

    def test_exact_cycle72_local_owner_consumes_global_preparation_capacity(self) -> None:
        self.write_guard(
            now=1000.0,
            actions={0: "local_owner"},
            details={0: "cycle72_worker_controller:source_transfer"},
        )
        claims: list[int] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(int(kwargs["worker_index"])),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            run_process=self.no_worker_runner,
        )

        payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(claims, [])
        self.assertEqual(
            payload["global_preparation_concurrency"],
            {"active": 1, "limit": 1},
        )
        self.assertEqual(
            payload["slots"][4]["detail"],
            "global_preparation_concurrency_wait:active=1:limit=1",
        )

    def test_preparation_limit_override_is_strict_and_bounded(self) -> None:
        defaults = dict(supervisor.parse_node_preparation_limits(None))
        self.assertEqual(set(defaults), {port for port, _gpu in CANONICAL_WORKER_INDEX_BY_LOCATION})
        self.assertTrue(all(limit == 1 for limit in defaults.values()))
        primary = supervisor.PRIMARY_REMOTE_PORT
        overridden = dict(supervisor.parse_node_preparation_limits(f"{primary}=2"))
        self.assertEqual(overridden[primary], 2)
        for raw in ("unknown", f"{primary}=0", f"{primary}=99", f"{primary}=1,{primary}=2"):
            with self.subTest(raw=raw), self.assertRaises(supervisor.SupervisorError):
                supervisor.parse_node_preparation_limits(raw)

        self.assertEqual(supervisor.parse_global_preparation_limit(None), 1)
        self.assertEqual(supervisor.parse_global_preparation_limit("2"), 2)
        for raw in ("0", "12", "+1", " 1", "1,2"):
            with self.subTest(global_raw=raw), self.assertRaises(
                supervisor.SupervisorError
            ):
                supervisor.parse_global_preparation_limit(raw)

    def test_healthy_worker_heartbeats_without_consuming_node_ramp(self) -> None:
        # Worker 0 consumes this node's launch token before worker 1 is
        # visited; the already healthy worker must still heartbeat.
        self.write_guard(now=1000.0, actions={1: "worker_active"})
        active = self.lease(1, asset="asset-active")
        next_lease = self.lease(0, asset="asset-next")
        active_name = supervisor.exact_worker_screen_name(active)
        active_command = " ".join(supervisor.build_worker_command(self.config, active))
        heartbeats: list[int] = []
        claims: list[int] = []

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["screen", "-ls"]:
                return subprocess.CompletedProcess(command, 0, f"1.{active_name}\n", "")
            if command[:4] == ["ps", "axww", "-o", "pid=,command="]:
                return subprocess.CompletedProcess(command, 0, f"1 {active_command}\n", "")
            raise AssertionError(command)

        def claim(**kwargs: object) -> dict[str, object] | None:
            worker = int(kwargs["worker_index"])
            claims.append(worker)
            return next_lease if worker == 0 else None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([active]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            heartbeat=lambda **kwargs: heartbeats.append(int(kwargs["worker_index"])) or active,
            complete=lambda **_kwargs: self.fail("active worker cannot complete"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: next_lease,
            run_process=runner,
        )
        with mock.patch.object(
            supervisor, "launch_exact_worker", return_value=True
        ) as launch:
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(heartbeats, [1])
        self.assertEqual(payload["slots"][1]["detail"], "exact_worker_heartbeat")
        self.assertEqual(int(launch.call_args.args[2]["worker_index"]), 0)
        self.assertNotIn(2, claims)

    def test_exact_local_worker_releases_only_racing_holder_without_lock_barrier(self) -> None:
        self.write_guard(now=1000.0, actions={0: "handoff_ready"})
        lease = self.lease(0, asset="asset-waiting-for-holder")
        lease["expired"] = True
        verified: list[dict[str, object]] = []
        ordered: list[str] = []
        renewed = False

        def renew(**_kwargs: object) -> dict[str, object]:
            nonlocal renewed
            ordered.append("renew")
            renewed = True
            return lease

        def verify(**kwargs: object) -> dict[str, object]:
            self.assertTrue(renewed, "expired lease must be renewed before verify")
            ordered.append("verify")
            verified.append(kwargs)
            return lease

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=renew,
            complete=lambda **_kwargs: self.fail("holder race is not an asset outcome"),
            release=lambda **_kwargs: self.fail("holder race must retain its lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=verify,
            remote_boot_id=lambda *_args, **_kwargs: BOOT,
            run_process=self.no_worker_runner,
        )
        with (
            mock.patch.object(
                supervisor,
                "audit_local_worker",
                return_value=supervisor.LocalWorkerEvidence("exact", 1, 1),
            ),
            mock.patch.object(
                supervisor,
                "acquire_slot_control_lock",
                return_value=contextlib.nullcontext(object()),
            ),
            mock.patch.object(supervisor, "exact_session_present", return_value=True),
            mock.patch.object(
                supervisor,
                "stop_exact_session",
                side_effect=lambda *_args, **_kwargs: ordered.append("stop"),
            ) as stop,
            mock.patch.object(supervisor, "launch_exact_worker") as launch,
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(ordered, ["renew", "verify", "stop"])
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["lease_id"], lease["lease_id"])
        self.assertEqual(
            payload["slots"][0]["detail"],
            "racing_holder_released_awaiting_worker_evidence",
        )
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(stop.call_args.args[0], "=total_asset_gpu_holder_g0")
        self.assertIn("kill-session -t =total_asset_gpu_holder_g0", stop.call_args.args[1])
        self.assertNotIn("pkill", stop.call_args.args[1])
        self.assertNotIn("gpu", stop.call_args.kwargs)
        launch.assert_not_called()
        for worker in (1, 2, 3):
            self.assertEqual(
                payload["slots"][worker]["detail"],
                "node_preparation_concurrency_wait:active=1:limit=1",
            )

    def test_exact_highqal_worker_accepts_only_legacy_guard_handoff_signal(self) -> None:
        self.write_guard(
            now=1000.0,
            actions={0: "remote_busy_or_ambiguous"},
            details={
                0: supervisor.LEGACY_GUARD_HIGHQAL_HANDOFF_DETAIL,
            },
        )
        lease = self.lease(0, asset="highqal-base-asset")
        lease["workload_kind"] = supervisor.HIGHQAL_WORKLOAD_KIND
        lease["work_item_id"] = "highqal-base-asset--0123456789abcdef"

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: lease,
            complete=lambda **_kwargs: self.fail(
                "legacy Highqal handoff is not an asset outcome"
            ),
            release=lambda **_kwargs: self.fail(
                "legacy Highqal handoff must retain its lease"
            ),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=self.no_worker_runner,
        )
        with (
            mock.patch.object(
                supervisor,
                "audit_local_worker",
                return_value=supervisor.LocalWorkerEvidence("exact", 1, 1),
            ),
            mock.patch.object(
                supervisor,
                "release_racing_holder_for_exact_worker",
            ) as release,
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        release.assert_called_once()
        self.assertEqual(
            payload["slots"][0]["detail"],
            "racing_holder_released_awaiting_worker_evidence",
        )

        ordinary = dict(lease, workload_kind="total_asset")
        evidence = supervisor.GuardSlotEvidence(
            remote_port=int(ordinary["remote_port"]),
            gpu=int(ordinary["gpu"]),
            worker_index=int(ordinary["worker_index"]),
            action="remote_busy_or_ambiguous",
            detail=supervisor.LEGACY_GUARD_HIGHQAL_HANDOFF_DETAIL,
            boot_id=str(ordinary["node_boot_id"]),
            observed_at_epoch=1000.0,
        )
        self.assertFalse(
            supervisor.legacy_highqal_handoff_candidate(evidence, ordinary)
        )
        self.assertFalse(
            supervisor.legacy_highqal_handoff_candidate(
                replace(evidence, detail="unrelated_holder"), lease
            )
        )
        self.assertTrue(
            supervisor.legacy_highqal_handoff_candidate(
                replace(evidence, action="local_owner", detail="worker"), lease
            )
        )
        self.assertFalse(
            supervisor.legacy_highqal_handoff_candidate(
                replace(evidence, action="local_owner", detail="worker"), ordinary
            )
        )

    def test_verified_handoff_is_released_before_authority_snapshot(self) -> None:
        self.write_guard(now=1000.0, actions={0: "handoff_ready"})
        lease = self.lease(0, asset="asset-pre-authority-handoff")
        ordered: list[str] = []

        def handoff_leases(**_kwargs: object) -> list[dict[str, object]]:
            ordered.append("lease_read")
            return [lease]

        def authority(*_args: object, **_kwargs: object) -> object:
            ordered.append("authority")
            return object()

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=authority,
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail(
                "post-release stale guard evidence cannot heartbeat"
            ),
            complete=lambda **_kwargs: self.fail(
                "live exact worker is not a completion candidate"
            ),
            release=lambda **_kwargs: self.fail("handoff retains its lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=self.no_worker_runner,
            handoff_leases=handoff_leases,
        )
        with (
            mock.patch.object(
                supervisor,
                "audit_local_worker",
                return_value=supervisor.LocalWorkerEvidence("exact", 1, 1),
            ),
            mock.patch.object(
                supervisor,
                "release_racing_holder_for_exact_worker",
                side_effect=lambda *_args, **_kwargs: ordered.append("release"),
            ) as release,
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(ordered[:3], ["lease_read", "release", "authority"])
        self.assertEqual(release.call_count, 1)
        self.assertEqual(payload["pre_authority_handoffs"], {"0": "released"})
        slot = payload["slots"][0]
        self.assertEqual(slot["guard_action"], "handoff_released")
        self.assertNotEqual(slot["state"], "productive")
        self.assertEqual(
            slot["detail"], "worker_remote_evidence_missing:handoff_released"
        )

    def test_same_guard_handoff_snapshot_is_consumed_exactly_once(self) -> None:
        self.write_guard(now=1000.0, actions={0: "handoff_ready"})
        slots = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1000.0, max_age_seconds=90
        )
        lease = self.lease(0, asset="asset-one-release")
        receipts: dict[int, dict[str, object]] = {}
        releases: list[str] = []
        dep = supervisor.Dependencies(
            run_process=self.no_worker_runner,
            handoff_leases=lambda **_kwargs: [lease],
        )
        local = supervisor.LocalWorkerEvidence("exact", 1, 1)
        with (
            mock.patch.object(supervisor, "audit_local_worker", return_value=local),
            mock.patch.object(
                supervisor,
                "release_racing_holder_for_exact_worker",
                side_effect=lambda *_args, **_kwargs: releases.append("release"),
            ),
        ):
            first, first_outcomes = supervisor.release_handoffs_before_authority(
                self.config,
                slots,
                dependency=dep,
                handoff_receipts=receipts,
                now_epoch=1001.0,
            )
            second, second_outcomes = supervisor.release_handoffs_before_authority(
                self.config,
                slots,
                dependency=dep,
                handoff_receipts=receipts,
                now_epoch=1006.0,
            )

        self.assertEqual(releases, ["release"])
        self.assertEqual(first_outcomes, {0: "released"})
        self.assertEqual(second_outcomes, {0: "already_released"})
        self.assertEqual(first[0].action, "handoff_released")
        self.assertEqual(second[0].action, "handoff_released")
        self.assertEqual(receipts[0]["lease_id"], lease["lease_id"])
        self.assertEqual(receipts[0]["guard_observed_at_epoch"], 1000.0)

    def test_pre_authority_handoff_requires_exact_local_and_physical_identity(self) -> None:
        self.write_guard(
            now=1000.0,
            actions={
                0: "handoff_ready",
                1: "handoff_ready",
                2: "handoff_ready",
                3: "handoff_ready",
            },
        )
        slots = supervisor.load_guard_slots(
            self.guard_state, now_epoch=1000.0, max_age_seconds=90
        )
        exact0 = self.lease(0, asset="asset-exact-0")
        exact1 = self.lease(1, asset="asset-exact-1")
        wrong2 = self.lease(2, asset="asset-wrong-2")
        wrong2["gpu"] = int(wrong2["gpu"]) + 1
        no_local3 = self.lease(3, asset="asset-no-local-3")
        released: list[int] = []

        dep = supervisor.Dependencies(
            run_process=self.no_worker_runner,
            handoff_leases=lambda **_kwargs: [
                exact0,
                exact1,
                wrong2,
                no_local3,
            ],
        )

        def local(lease: dict[str, object], **_kwargs: object) -> object:
            active = int(lease["worker_index"]) in {0, 1}
            return supervisor.LocalWorkerEvidence(
                "exact", 1 if active else 0, 1 if active else 0
            )

        with (
            mock.patch.object(supervisor, "audit_local_worker", side_effect=local),
            mock.patch.object(
                supervisor,
                "release_racing_holder_for_exact_worker",
                side_effect=lambda _config, _evidence, lease, **_kwargs: (
                    released.append(int(lease["worker_index"]))
                ),
            ),
        ):
            updated, outcomes = supervisor.release_handoffs_before_authority(
                self.config,
                slots,
                dependency=dep,
            )

        self.assertEqual(released, [0, 1])
        self.assertEqual(outcomes[0], "released")
        self.assertEqual(outcomes[1], "released")
        self.assertEqual(outcomes[2], "lease_physical_identity_mismatch")
        self.assertEqual(outcomes[3], "exact_local_worker_missing")
        self.assertEqual(updated[0].action, "handoff_released")
        self.assertEqual(updated[1].action, "handoff_released")
        self.assertEqual(updated[2].action, "handoff_blocked")
        self.assertEqual(updated[3].action, "handoff_blocked")

    def test_handoff_without_matching_active_lease_cannot_enter_claim_path(self) -> None:
        self.write_guard(now=1000.0, actions={0: "handoff_ready"})
        claims: list[int] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(int(kwargs["worker_index"])),
            heartbeat=lambda **_kwargs: self.fail("missing lease cannot heartbeat"),
            complete=lambda **_kwargs: self.fail("missing lease cannot complete"),
            release=lambda **_kwargs: self.fail("missing lease cannot release"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: self.fail("missing lease cannot verify"),
            run_process=self.no_worker_runner,
            handoff_leases=lambda **_kwargs: [],
        )

        payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertNotIn(0, claims)
        self.assertEqual(
            payload["pre_authority_handoffs"], {"0": "active_lease_missing"}
        )
        self.assertEqual(payload["slots"][0]["guard_action"], "handoff_blocked")
        self.assertEqual(payload["slots"][0]["state"], "blocked")

    def test_racing_holder_remote_boot_change_fails_closed_without_stop(self) -> None:
        self.write_guard(now=1000.0, actions={0: "handoff_ready"})
        lease = self.lease(0, asset="asset-waiting-for-holder")
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail("boot mismatch cannot heartbeat"),
            complete=lambda **_kwargs: self.fail("boot mismatch is not an asset outcome"),
            release=lambda **_kwargs: self.fail("boot mismatch must retain its lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            remote_boot_id=lambda *_args, **_kwargs: NEW_BOOT,
            run_process=self.no_worker_runner,
        )
        with (
            mock.patch.object(
                supervisor,
                "audit_local_worker",
                return_value=supervisor.LocalWorkerEvidence("exact", 1, 1),
            ),
            mock.patch.object(
                supervisor,
                "acquire_slot_control_lock",
                return_value=contextlib.nullcontext(object()),
            ),
            mock.patch.object(supervisor, "exact_session_present") as present,
            mock.patch.object(supervisor, "stop_exact_session") as stop,
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(
            payload["slots"][0]["detail"], "racing_holder_boot_identity_changed"
        )
        present.assert_not_called()
        stop.assert_not_called()

    def test_local_exact_worker_with_preparing_holder_renews_without_releasing_it(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_preparing"})
        lease = self.lease(0, asset="asset-preparing-source")
        heartbeats: list[dict[str, object]] = []
        clock = {"now": 1000.0}

        def status(**_kwargs: object) -> dict[str, object]:
            # Simulate a slow authority/status scan after the poll timestamp
            # was captured. Lease mutations must use the transaction clock.
            clock["now"] = 1700.0
            return self.status_payload([lease])

        dep = supervisor.Dependencies(
            now=lambda: clock["now"],
            status=status,
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **kwargs: heartbeats.append(kwargs) or lease,
            complete=lambda **_kwargs: self.fail("preparing worker has no outcome"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=self.no_worker_runner,
        )
        with (
            mock.patch.object(
                supervisor,
                "audit_local_worker",
                return_value=supervisor.LocalWorkerEvidence(
                    "exact",
                    1,
                    1,
                    process_command=" ".join(
                        supervisor.build_worker_command(self.config, lease)
                    ),
                ),
            ),
            mock.patch.object(
                supervisor, "release_racing_holder_for_exact_worker"
            ) as release_holder,
            mock.patch.object(supervisor, "stop_exact_session") as stop,
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(
            payload["slots"][0]["detail"],
            "worker_preparing_lease_heartbeat",
        )
        self.assertEqual(len(heartbeats), 1)
        self.assertEqual(heartbeats[0]["lease_id"], lease["lease_id"])
        self.assertEqual(heartbeats[0]["now_epoch"], 1700.0)
        release_holder.assert_not_called()
        stop.assert_not_called()

    def test_superseded_preparing_worker_is_retired_without_remote_signal(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_preparing"})
        lease = {
            **self.lease(0, asset="asset-superseded-source"),
            "claimed_at_epoch": 700.0,
        }
        old_project = self.root / "old-runtime"
        old_command = " ".join(supervisor.build_worker_command(self.config, lease)).replace(
            str(self.config.project), str(old_project), 1
        )
        retirements: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail("superseded preparation must not renew"),
            complete=lambda **_kwargs: self.fail("retirement waits for holder"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            retire_local_worker=lambda *_args, **kwargs: retirements.append(kwargs),
            run_process=self.no_worker_runner,
        )
        local = supervisor.LocalWorkerEvidence(
            "exact", 1, 1, 123, 456, old_command
        )
        with mock.patch.object(supervisor, "audit_local_worker", return_value=local):
            payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(len(retirements), 1)
        self.assertEqual(
            payload["slots"][0]["detail"],
            "superseded_preparation_retired_awaiting_holder",
        )

    def test_current_project_detection_preserves_unquoted_path_with_spaces(self) -> None:
        project = self.root / "Application Support/runtime-generation"
        config = replace(self.config, project=project)
        lease = self.lease()
        command = " ".join(supervisor.build_worker_command(config, lease))
        local = supervisor.LocalWorkerEvidence(
            "exact", 1, 1, 123, 456, command
        )
        self.assertTrue(supervisor.local_worker_uses_current_project(config, local))
        self.assertFalse(
            supervisor.local_worker_uses_current_project(self.config, local)
        )

    def test_dead_screen_socket_is_not_exact_worker_evidence(self) -> None:
        lease = self.lease()
        name = supervisor.exact_worker_screen_name(lease)
        output = (
            f"123.{name}\t(Dead ???)\n"
            f"124.{name}-live\t(Detached)\n"
        )
        self.assertEqual(supervisor.parse_screen_sessions(output), (f"{name}-live",))

    def test_stale_retirement_targets_only_exact_screen_process_tree(self) -> None:
        lease = self.lease()
        evidence = supervisor.GuardSlotEvidence(
            int(lease["remote_port"]),
            int(lease["gpu"]),
            int(lease["worker_index"]),
            "local_owner",
            "remote_idle_worker_starting:cycle72_worker_controller",
            str(lease["node_boot_id"]),
            1000.0,
        )
        local = supervisor.LocalWorkerEvidence(
            supervisor.exact_worker_screen_name(lease), 1, 1, 123, 456
        )
        absent = supervisor.LocalWorkerEvidence(local.screen_name, 0, 0)
        root = mock.Mock(pid=123)
        worker = mock.Mock(
            pid=456,
            command=" ".join(supervisor.build_worker_command(self.config, lease)),
        )
        tracked = {123: root, 456: worker}

        with (
            mock.patch.object(
                supervisor, "acquire_slot_control_lock",
                return_value=contextlib.nullcontext(object()),
            ),
            mock.patch.object(
                supervisor, "audit_local_worker", side_effect=[local, absent]
            ),
            mock.patch.object(supervisor, "process_identity", return_value=root),
            mock.patch.object(
                supervisor, "descendant_identities", return_value=tracked
            ),
            mock.patch.object(supervisor, "terminate_exact_process_tree") as terminate,
        ):
            supervisor.retire_stale_exact_local_worker(
                self.config,
                evidence=evidence,
                lease=lease,
                local=local,
                runner=self.no_worker_runner,
            )

        terminate.assert_called_once_with(
            root,
            tracked=tracked,
            term_grace_seconds=supervisor.STALE_CONTROLLER_TERM_GRACE_SECONDS,
            kill_grace_seconds=supervisor.STALE_CONTROLLER_KILL_GRACE_SECONDS,
        )

    def test_local_exact_worker_without_remote_worker_proof_never_heartbeats(self) -> None:
        self.write_guard(now=1000.0, actions={0: "local_owner"})
        lease = self.lease()
        name = supervisor.exact_worker_screen_name(lease)
        worker_command = " ".join(supervisor.build_worker_command(self.config, lease))

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["screen", "-ls"]:
                return subprocess.CompletedProcess(
                    command, 0, f"123.{name}\t(Detached)\n", ""
                )
            return subprocess.CompletedProcess(
                command, 0, f"999 {worker_command}\n", ""
            )

        claims: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(kwargs),
            heartbeat=lambda **_kwargs: self.fail(
                "local process evidence cannot extend a lease"
            ),
            complete=lambda **_kwargs: self.fail("active local worker cannot complete"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(payload["slots"][0]["state"], "blocked")
        self.assertEqual(
            payload["slots"][0]["detail"],
            "worker_remote_evidence_missing:local_owner",
        )
        self.assertEqual({item["worker_index"] for item in claims}, set(range(1, 11)))

    def test_remote_verified_rendering_never_retires_local_controller(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_active"})
        lease = {**self.lease(), "expires_at_epoch": 999.0}
        heartbeats: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **kwargs: heartbeats.append(kwargs) or lease,
            complete=lambda **_kwargs: self.fail("active render cannot complete"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            slot_capabilities=lambda **_kwargs: supervisor.SlotCapabilities(),
            retire_local_worker=lambda *_args, **_kwargs: self.fail(
                "remote Blender proof must prevent local retirement"
            ),
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor,
            "audit_local_worker",
            return_value=supervisor.LocalWorkerEvidence("exact", 1, 1, 123, 456),
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(payload["slots"][0]["state"], "productive")
        self.assertEqual(len(heartbeats), 1)

    def test_remote_idle_slow_preparation_does_not_retire_before_lease_deadline(self) -> None:
        self.write_guard(
            now=1000.0,
            actions={0: "local_owner"},
            details={
                0: "remote_idle_worker_starting:cycle72_worker_controller,worker"
            },
        )
        lease = {**self.lease(), "expires_at_epoch": 1600.0}
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail("local_owner cannot heartbeat"),
            complete=lambda **_kwargs: self.fail("local owner cannot complete"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            slot_capabilities=lambda **_kwargs: supervisor.SlotCapabilities(),
            retire_local_worker=lambda *_args, **_kwargs: self.fail(
                "unexpired preparation grace must not be retired"
            ),
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor,
            "audit_local_worker",
            return_value=supervisor.LocalWorkerEvidence("exact", 1, 1, 123, 456),
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(
            payload["slots"][0]["detail"],
            "worker_remote_evidence_missing:local_owner",
        )

    def test_remote_idle_stale_controller_retires_only_after_lease_deadline(self) -> None:
        self.write_guard(
            now=1000.0,
            actions={0: "local_owner"},
            details={0: "remote_idle_worker_starting:cycle72_worker_controller"},
        )
        lease = {**self.lease(), "expires_at_epoch": 999.0}
        retirements: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail("stale local owner cannot heartbeat"),
            complete=lambda **_kwargs: self.fail("retirement waits for holder"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            slot_capabilities=lambda **_kwargs: supervisor.SlotCapabilities(),
            retire_local_worker=lambda *_args, **kwargs: retirements.append(kwargs),
            run_process=self.no_worker_runner,
        )
        local = supervisor.LocalWorkerEvidence("exact", 1, 1, 123, 456)
        with mock.patch.object(supervisor, "audit_local_worker", return_value=local):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(len(retirements), 1)
        self.assertIs(retirements[0]["local"], local)
        self.assertEqual(retirements[0]["lease"]["lease_id"], lease["lease_id"])
        self.assertEqual(
            payload["slots"][0]["detail"],
            "stale_local_controller_retired_awaiting_holder:"
            "remote_idle_preparation_timeout",
        )

    def test_terminal_runtime_allows_exact_remote_idle_controller_retirement(self) -> None:
        self.write_guard(
            now=1000.0,
            actions={0: "local_owner"},
            details={0: "remote_idle_worker_starting:cycle72_worker_controller"},
        )
        lease = {**self.lease(), "expires_at_epoch": 1600.0}
        self.write_runtime(lease, status="complete", category="", code="")
        retirements: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail("terminal owner cannot heartbeat"),
            complete=lambda **_kwargs: self.fail("retirement waits for holder"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            slot_capabilities=lambda **_kwargs: supervisor.SlotCapabilities(),
            retire_local_worker=lambda *_args, **kwargs: retirements.append(kwargs),
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor,
            "audit_local_worker",
            return_value=supervisor.LocalWorkerEvidence("exact", 1, 1, 123, 456),
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(len(retirements), 1)
        self.assertEqual(
            payload["slots"][0]["detail"],
            "stale_local_controller_retired_awaiting_holder:"
            "terminal_runtime_complete",
        )

    def test_terminal_runtime_behind_holder_retires_before_preparation_heartbeat(self) -> None:
        self.write_guard(
            now=1000.0,
            actions={0: "worker_preparing"},
            details={
                0: "exact cycle72 worker has not reached remote wrapper"
            },
        )
        lease = {**self.lease(), "expires_at_epoch": 1600.0}
        self.write_runtime(lease, status="failed")
        retirements: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail(
                "terminal preparation must not renew"
            ),
            complete=lambda **_kwargs: self.fail("retirement waits for holder"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            retire_local_worker=lambda *_args, **kwargs: retirements.append(kwargs),
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor,
            "audit_local_worker",
            return_value=supervisor.LocalWorkerEvidence("exact", 1, 1, 123, 456),
        ):
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(len(retirements), 1)
        self.assertEqual(
            payload["slots"][0]["detail"],
            "stale_local_controller_retired_awaiting_holder:"
            "terminal_runtime_failed",
        )

    def test_boot_change_recovers_same_lease_only_after_holder_evidence(self) -> None:
        self.write_guard(now=1000.0, boots={0: NEW_BOOT})
        old = self.lease(boot=BOOT)
        recovered = {**old, "node_boot_id": NEW_BOOT}
        snapshot = self.authority_snapshot(self.catalog_row(old))
        claims: list[dict[str, object]] = []

        def claim(**kwargs: object) -> dict[str, object] | None:
            claims.append(kwargs)
            return recovered if kwargs["worker_index"] == 0 else None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([old]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("old boot must not heartbeat"),
            complete=lambda **_kwargs: (_ for _ in ()).throw(
                cycle72.Cycle72Conflict("not terminal")
            ),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: recovered,
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(supervisor, "launch_exact_worker", return_value=True) as launch:
            payload = supervisor.supervise_once(self.config, dependency=dep)
        recovery = next(item for item in claims if item["worker_index"] == 0)
        self.assertTrue(recovery["confirm_remote_idle"])
        self.assertEqual(recovery["node_boot_id"], NEW_BOOT)
        self.assertEqual(launch.call_args.args[2]["lease_id"], old["lease_id"])
        self.assertEqual(payload["slots"][0]["state"], "blocked")
        self.assertEqual(
            payload["slots"][0]["detail"],
            "worker_started_awaiting_remote_evidence",
        )

    def test_incompatible_active_lease_is_released_and_replaced_in_same_poll(self) -> None:
        self.write_guard(now=1000.0)
        old = self.lease(4, asset="asset-source-on-cycles-only-node")
        new = {
            **self.lease(4, asset="asset-cycles-compatible"),
            "lease_id": "f" * 32,
            "render_order": 3006,
        }
        old_row = self.catalog_row(
            old, render_engine_hint="BLENDER_EEVEE_NEXT"
        )
        new_row = self.catalog_row(new, render_engine_hint="CYCLES")
        snapshot = self.authority_snapshot(old_row, new_row)
        ordered: list[str] = []
        releases: list[dict[str, object]] = []
        claims: list[dict[str, object]] = []

        def complete(**_kwargs: object) -> None:
            ordered.append("complete")
            raise cycle72.Cycle72Conflict("not terminal")

        def release(**kwargs: object) -> dict[str, object]:
            ordered.append("release")
            releases.append(kwargs)
            return old

        def claim(**kwargs: object) -> dict[str, object] | None:
            claims.append(kwargs)
            if kwargs["worker_index"] == 4:
                ordered.append("claim")
                return new
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([old]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail(
                "an incompatible active lease must not heartbeat"
            ),
            complete=complete,
            release=release,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: new,
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor, "launch_exact_worker", return_value=True
        ) as launch:
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(ordered, ["complete", "release", "claim"])
        self.assertEqual(len(releases), 1)
        self.assertEqual(
            releases[0]["reason"],
            "active_lease_incompatible_with_current_physical_slot",
        )
        self.assertTrue(releases[0]["confirm_remote_idle"])
        worker_claim = next(item for item in claims if item["worker_index"] == 4)
        capability = worker_claim["capabilities"]
        self.assertFalse(capability.accepts(old_row))
        self.assertTrue(capability.accepts(new_row))
        self.assertEqual(launch.call_args.args[2]["asset_id"], new["asset_id"])
        self.assertEqual(payload["infrastructure_retries"], {})
        self.assertEqual(
            payload["slots"][4]["detail"],
            "worker_started_awaiting_remote_evidence",
        )

    def test_missing_active_lease_catalog_row_fails_closed_without_release(self) -> None:
        self.write_guard(now=1000.0)
        old = self.lease(4, asset="asset-missing-from-current-catalog")
        unrelated = self.lease(0, asset="asset-unrelated")
        snapshot = self.authority_snapshot(self.catalog_row(unrelated))
        claimed_workers: list[int] = []

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([old]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=lambda **kwargs: claimed_workers.append(
                int(kwargs["worker_index"])
            ),
            heartbeat=lambda **_kwargs: self.fail(
                "unknown catalog identity must not heartbeat"
            ),
            complete=lambda **_kwargs: (_ for _ in ()).throw(
                cycle72.Cycle72Conflict("not terminal")
            ),
            release=lambda **_kwargs: self.fail(
                "unknown catalog identity must retain its lease"
            ),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: old,
            run_process=self.no_worker_runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertNotIn(4, claimed_workers)
        self.assertEqual(payload["slots"][4]["state"], "blocked")
        self.assertEqual(
            payload["slots"][4]["detail"],
            "active_lease_catalog_row_missing",
        )
        self.assertEqual(payload["infrastructure_retries"], {})

    def test_landed_incompatible_lease_completes_before_compatibility_release(self) -> None:
        self.write_guard(now=1000.0)
        old = self.lease(4, asset="asset-landed-source")
        old_row = self.catalog_row(
            old, render_engine_hint="BLENDER_EEVEE_NEXT"
        )
        snapshot = self.authority_snapshot(old_row)
        completed: list[str] = []

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([old]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=lambda **_kwargs: None,
            heartbeat=lambda **_kwargs: self.fail("landed lease must not heartbeat"),
            complete=lambda **kwargs: completed.append(
                str(kwargs["lease_id"])
            ) or old,
            release=lambda **_kwargs: self.fail(
                "landed authority must win over compatibility release"
            ),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: old,
            run_process=self.no_worker_runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(completed, [old["lease_id"]])
        self.assertEqual(payload["slots"][4]["state"], "holder")
        self.assertEqual(
            payload["slots"][4]["detail"], "no_compatible_ready_asset"
        )
        self.assertEqual(payload["infrastructure_retries"], {})

    def test_auth_blocked_slot_does_not_complete_release_or_claim(self) -> None:
        self.write_guard(
            now=1000.0,
            default_action="probe_blocked",
            boots={index: None for index in range(11)},
        )
        lease = self.lease()
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: self.fail(
                "offline-only poll must not scan production authority"
            ),
            claim=lambda **_kwargs: self.fail("auth failure cannot claim"),
            heartbeat=lambda **_kwargs: self.fail("no exact worker"),
            complete=lambda **_kwargs: self.fail("transport cannot become asset outcome"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=self.no_worker_runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(payload["slots"][0]["state"], "offline")
        self.assertEqual(payload["slots"][0]["detail"], "probe_blocked")

    def test_bootstrap_provisioning_runs_before_guard_and_is_reported(self) -> None:
        self.write_guard(
            now=1000.0,
            default_action="probe_blocked",
            boots={index: None for index in range(11)},
        )
        config = replace(
            self.config,
            bootstrap_provision_state=self.state_root / "node-provision.json",
        )
        calls: list[object] = []

        def provision(provision_config: object, **kwargs: object) -> dict[str, object]:
            calls.append((provision_config, kwargs))
            return {
                "schema": "video2blender.total-asset-node-provision-state.v1",
                "status": "pending",
                "nodes": {
                    "31722": {"status": "attested", "action": "attested_skip"},
                    "30773": {"status": "pending_auth"},
                    "30808": {"status": "pending_auth"},
                },
            }

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: self.fail(
                "offline-only poll must not scan production authority"
            ),
            claim=lambda **_kwargs: self.fail("offline slot cannot claim"),
            heartbeat=lambda **_kwargs: self.fail("no exact worker"),
            complete=lambda **_kwargs: self.fail("no exact worker"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: self.fail("no launch"),
            provision=provision,
            run_process=self.no_worker_runner,
        )
        payload = supervisor.supervise_once(config, dependency=dep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["bootstrap_provisioning"]["status"], "pending")
        self.assertEqual(
            payload["bootstrap_provisioning"]["nodes"]["31722"]["action"],
            "attested_skip",
        )

    def test_new_claims_wait_for_per_node_bootstrap_attestation(self) -> None:
        self.write_guard(now=1000.0)
        secondary_port = next(
            port
            for (port, _gpu), worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 0
        )
        primary_port = next(
            port
            for (port, _gpu), worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 4
        )
        tertiary_port = next(
            port
            for (port, _gpu), worker in CANONICAL_WORKER_INDEX_BY_LOCATION.items()
            if worker == 8
        )
        config = replace(
            self.config,
            bootstrap_provision_state=self.state_root / "node-provision.json",
        )
        claims: list[int] = []

        def provision(_config: object, **_kwargs: object) -> dict[str, object]:
            return {
                "schema": "video2blender.total-asset-node-provision-state.v1",
                "status": "pending",
                "nodes": {
                    str(secondary_port): {"status": "attested"},
                    str(primary_port): {"status": "pending_auth"},
                    str(tertiary_port): {"status": "blocked"},
                },
            }

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(int(kwargs["worker_index"])),
            heartbeat=lambda **_kwargs: self.fail("no existing lease"),
            complete=lambda **_kwargs: self.fail("no existing lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            provision=provision,
            run_process=self.no_worker_runner,
        )
        payload = supervisor.supervise_once(config, dependency=dep)
        self.assertEqual(claims, [0, 1, 2, 3])
        for worker in range(4, 11):
            self.assertEqual(
                payload["slots"][worker]["detail"],
                "waiting_bootstrap_attestation",
            )

    def test_one_slot_lease_conflict_does_not_stop_other_slots(self) -> None:
        self.write_guard(now=1000.0)

        def claim(**kwargs: object) -> None:
            if kwargs["worker_index"] == 0:
                raise cycle72.Cycle72Conflict("synthetic conflict")
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("no lease"),
            complete=lambda **_kwargs: self.fail("no lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(payload["slots"][0]["detail"], "cycle72_lease_conflict")
        self.assertEqual(payload["slots"][1]["state"], "holder")

    def test_live_legacy_formal_worker_blocks_every_new_dynamic_claim(self) -> None:
        self.write_guard(now=1000.0)
        legacy_command = (
            "python3 blender/scripts/run_total_asset_render_worker.py "
            "--queue /tmp/catalog.csv --batch batch0003 --remote-port 31722 "
            "--gpu 0 --worker-index 0 --worker-count 11"
        )

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:4] == ["ps", "axww", "-o", "pid=,command="]:
                return subprocess.CompletedProcess(command, 0, f"321 {legacy_command}\n", "")
            raise AssertionError(command)

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: self.fail("legacy cutover barrier must block claims"),
            heartbeat=lambda **_kwargs: self.fail("no dynamic lease"),
            complete=lambda **_kwargs: self.fail("no dynamic lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertTrue(payload["legacy_cutover_barrier"]["active"])
        self.assertEqual(payload["legacy_cutover_barrier"]["worker_count"], 1)
        self.assertTrue(
            all(
                item["detail"] == "waiting_legacy_drain_barrier"
                for item in payload["slots"]
            )
        )

    def test_one_authority_snapshot_is_shared_by_status_and_all_claims(self) -> None:
        self.write_guard(now=1000.0)
        snapshot = object()
        seen: list[object] = []

        def status(**kwargs: object) -> dict[str, object]:
            seen.append(kwargs["snapshot"])
            return self.status_payload([])

        def claim(**kwargs: object) -> None:
            seen.append(kwargs["snapshot"])
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=status,
            authority=lambda *_args, **_kwargs: snapshot,
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("no lease"),
            complete=lambda **_kwargs: self.fail("no lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(len(seen), 12)
        self.assertTrue(all(item is snapshot for item in seen))

    def test_all_claims_share_one_bounded_runtime_probe_budget_per_poll(self) -> None:
        self.write_guard(now=1000.0)
        budgets: list[cycle72.RuntimeProbeBudget] = []

        def claim(**kwargs: object) -> None:
            budget = kwargs["runtime_probe_budget"]
            self.assertIsInstance(budget, cycle72.RuntimeProbeBudget)
            budgets.append(budget)
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("no lease"),
            complete=lambda **_kwargs: self.fail("no lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(len(budgets), 11)
        self.assertTrue(all(item is budgets[0] for item in budgets))
        self.assertEqual(
            budgets[0].limit, cycle72.DEFAULT_RUNTIME_PROBES_PER_POLL
        )

    def test_stale_authority_after_first_launch_refreshes_for_remaining_nodes(self) -> None:
        self.write_guard(now=1000.0)
        config = replace(self.config, global_preparation_limit=3)
        first_snapshot = object()
        refreshed_snapshot = object()
        authority_calls: list[object] = []
        claim_snapshots: list[tuple[int, object]] = []

        def authority(*_args: object, **_kwargs: object) -> object:
            value = first_snapshot if not authority_calls else refreshed_snapshot
            authority_calls.append(value)
            return value

        def claim(**kwargs: object) -> dict[str, object] | None:
            worker = int(kwargs["worker_index"])
            snapshot = kwargs["snapshot"]
            claim_snapshots.append((worker, snapshot))
            if worker == 4 and snapshot is first_snapshot:
                raise cycle72.Cycle72Conflict(
                    "provided authority snapshot is no longer current"
                )
            if worker in {0, 4, 8}:
                return self.lease(worker, asset=f"asset-refresh-{worker}")
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=authority,
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("no existing worker"),
            complete=lambda **_kwargs: self.fail("no existing worker"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor, "launch_exact_worker", return_value=True
        ) as launch:
            payload = supervisor.supervise_once(config, dependency=dep)

        self.assertEqual(authority_calls, [first_snapshot, refreshed_snapshot])
        self.assertIn((4, first_snapshot), claim_snapshots)
        self.assertIn((4, refreshed_snapshot), claim_snapshots)
        self.assertIn((8, refreshed_snapshot), claim_snapshots)
        self.assertEqual(
            [int(call.args[2]["worker_index"]) for call in launch.call_args_list],
            [0, 4, 8],
        )
        self.assertNotIn(
            "cycle72_lease_conflict",
            {item["detail"] for item in payload["slots"]},
        )

    def test_expired_same_boot_lease_with_exact_holder_is_renewed_not_released(self) -> None:
        self.write_guard(now=1000.0)
        lease = {**self.lease(), "expired": True, "expires_at_epoch": 999.0}
        snapshot = self.authority_snapshot(self.catalog_row(lease))
        heartbeats: list[dict[str, object]] = []
        releases: list[dict[str, object]] = []
        claims: list[int] = []

        def claim(**kwargs: object) -> None:
            claims.append(int(kwargs["worker_index"]))
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=claim,
            heartbeat=lambda **kwargs: heartbeats.append(kwargs) or lease,
            complete=lambda **_kwargs: (_ for _ in ()).throw(
                cycle72.Cycle72Conflict(
                    "formal terminal JSONL outcome must land before completing a lease"
                )
            ),
            release=lambda **kwargs: releases.append(kwargs) or lease,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(
            supervisor, "launch_exact_worker", return_value=True
        ) as launch:
            payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(len(heartbeats), 1)
        self.assertEqual(heartbeats[0]["lease_id"], lease["lease_id"])
        self.assertEqual(releases, [])
        self.assertNotIn(0, claims)
        self.assertEqual(launch.call_args.args[2]["lease_id"], lease["lease_id"])
        self.assertEqual(
            payload["slots"][0]["detail"],
            "worker_started_awaiting_remote_evidence",
        )

    def test_existing_unlanded_lease_cannot_launch_before_bootstrap_attestation(self) -> None:
        self.write_guard(now=1000.0)
        lease = self.lease()
        snapshot = self.authority_snapshot(self.catalog_row(lease))
        config = replace(
            self.config,
            bootstrap_provision_state=self.state_root / "node-provision.json",
        )
        heartbeats: list[dict[str, object]] = []
        claims: list[int] = []
        blocked_port = str(lease["remote_port"])

        def provision(_config: object, **_kwargs: object) -> dict[str, object]:
            return {
                "schema": "video2blender.total-asset-node-provision-state.v1",
                "status": "pending",
                "nodes": {
                    str(port): {
                        "status": "blocked" if str(port) == blocked_port else "attested"
                    }
                    for port in {location[0] for location in CANONICAL_WORKER_INDEX_BY_LOCATION}
                },
            }

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=lambda **kwargs: claims.append(int(kwargs["worker_index"])),
            heartbeat=lambda **kwargs: heartbeats.append(kwargs) or lease,
            complete=lambda **_kwargs: (_ for _ in ()).throw(
                cycle72.Cycle72Conflict(
                    "formal terminal JSONL outcome must land before completing a lease"
                )
            ),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: self.fail("blocked lease cannot launch"),
            provision=provision,
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(supervisor, "launch_exact_worker") as launch:
            payload = supervisor.supervise_once(config, dependency=dep)

        self.assertEqual(len(heartbeats), 1)
        self.assertNotIn(0, claims)
        launch.assert_not_called()
        self.assertEqual(payload["slots"][0]["state"], "holder")
        self.assertEqual(
            payload["slots"][0]["detail"], "waiting_bootstrap_attestation"
        )
        self.assertEqual(payload["slots"][0]["lease_id"], lease["lease_id"])

    def test_terminal_status_completes_then_claims_next_in_same_poll(self) -> None:
        self.write_guard(now=1000.0)
        old = self.lease()
        new = self.lease(asset="asset-3002")
        completed: list[str] = []

        def claim(**kwargs: object) -> dict[str, object] | None:
            return new if kwargs["worker_index"] == 0 else None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([old]),
            authority=lambda *_args, **_kwargs: object(),
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("terminal lease must not heartbeat"),
            complete=lambda **kwargs: completed.append(str(kwargs["lease_id"])) or old,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: new,
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(supervisor, "launch_exact_worker", return_value=True) as launch:
            supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(completed, [old["lease_id"]])
        self.assertEqual(launch.call_args.args[2]["asset_id"], "asset-3002")

    def test_infrastructure_runtime_accepts_only_exact_nested_boot_identity(self) -> None:
        lease = self.lease()
        self.write_runtime(lease, status="blocked_runtime_unavailable")
        path = supervisor.runtime_state_path(self.config, lease)
        base = json.loads(path.read_text(encoding="utf-8"))
        base.pop("node_boot_id")
        exact_physical_worker = {
            "worker_index": lease["worker_index"],
            "remote_port": lease["remote_port"],
            "gpu": lease["gpu"],
            "node_boot_id": lease["node_boot_id"],
        }
        base["physical_worker"] = exact_physical_worker
        path.write_text(json.dumps(base), encoding="utf-8")
        self.assertEqual(
            supervisor.load_infrastructure_runtime_episode(self.config, lease),
            "worker_ssh_timeout",
        )

        mismatches = (
            ("worker_index_value", "worker_index", int(lease["worker_index"]) + 1),
            ("worker_index_type", "worker_index", False),
            ("remote_port", "remote_port", int(lease["remote_port"]) + 1),
            ("gpu", "gpu", int(lease["gpu"]) + 1),
            ("node_boot_id", "node_boot_id", NEW_BOOT),
        )
        for label, field, wrong_value in mismatches:
            with self.subTest(field=label):
                payload = dict(base)
                payload["physical_worker"] = {
                    **exact_physical_worker,
                    field: wrong_value,
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(
                    supervisor.load_infrastructure_runtime_episode(
                        self.config, lease
                    )
                )

        base.pop("physical_worker")
        path.write_text(json.dumps(base), encoding="utf-8")
        self.assertIsNone(
            supervisor.load_infrastructure_runtime_episode(self.config, lease)
        )

    def test_blender_loader_runtime_block_is_reclaimable_infrastructure(self) -> None:
        lease = self.lease()
        self.write_runtime(
            lease,
            status="blocked_runtime_unavailable",
            category="blender_runtime_shared_library_missing",
            code="blender_runtime_shared_library_missing",
        )
        self.assertEqual(
            supervisor.load_infrastructure_runtime_episode(self.config, lease),
            "blender_runtime_shared_library_missing",
        )

    def test_infrastructure_exit_releases_exact_lease_with_backoff(self) -> None:
        self.write_guard(now=1000.0)
        old = self.lease()
        new = {**self.lease(asset="asset-3002"), "lease_id": "f" * 32}
        snapshot = self.authority_snapshot(
            self.catalog_row(old), self.catalog_row(new)
        )
        self.write_runtime(old)
        releases: list[dict[str, object]] = []
        claims: list[dict[str, object]] = []

        def claim(**kwargs: object) -> dict[str, object] | None:
            claims.append(kwargs)
            if kwargs["worker_index"] == 0:
                self.assertIn(old["asset_id"], kwargs["excluded_asset_ids"])
                return new
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([old]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=claim,
            heartbeat=lambda **_kwargs: self.fail("infra exit must release, not heartbeat"),
            complete=lambda **_kwargs: (_ for _ in ()).throw(
                cycle72.Cycle72Conflict("not terminal")
            ),
            release=lambda **kwargs: releases.append(kwargs) or old,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: new,
            run_process=self.no_worker_runner,
        )
        with mock.patch.object(supervisor, "launch_exact_worker", return_value=True) as launch:
            payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(len(releases), 1)
        self.assertTrue(releases[0]["confirm_remote_idle"])
        self.assertEqual(releases[0]["observed_node_boot_id"], BOOT)
        self.assertIn("worker_ssh_timeout", releases[0]["reason"])
        retry = payload["infrastructure_retries"][str(old["asset_id"])]
        self.assertEqual(retry["attempts"], 1)
        self.assertEqual(retry["next_retry_at_epoch"], 1060.0)
        self.assertFalse(retry["exhausted"])
        self.assertEqual(
            retry["runtime_generation"], payload["runtime_generation"]
        )
        self.assertNotIn(0, {int(item["worker_index"]) for item in claims})
        self.assertEqual(
            payload["slots"][0]["detail"],
            "infrastructure_retry_scheduled:worker_ssh_timeout",
        )
        launch.assert_not_called()

    def test_blocked_gpu_busy_releases_exact_lease_in_same_poll(self) -> None:
        self.write_guard(now=1000.0)
        lease = self.lease()
        snapshot = self.authority_snapshot(self.catalog_row(lease))
        self.write_runtime(
            lease,
            status="blocked_gpu_busy",
            category="gpu_contention",
            code="blocked_gpu_busy",
        )
        releases: list[dict[str, object]] = []
        claim_workers: list[int] = []

        def no_claim(**kwargs: object) -> None:
            claim_workers.append(int(kwargs["worker_index"]))
            return None

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=no_claim,
            heartbeat=lambda **_kwargs: self.fail(
                "ended GPU-busy worker must release, not heartbeat"
            ),
            complete=lambda **_kwargs: (_ for _ in ()).throw(
                cycle72.Cycle72Conflict("not terminal")
            ),
            release=lambda **kwargs: releases.append(kwargs) or lease,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: self.fail("same poll must not relaunch"),
            run_process=self.no_worker_runner,
        )

        payload = supervisor.supervise_once(self.config, dependency=dep)

        self.assertEqual(len(releases), 1)
        self.assertTrue(releases[0]["confirm_remote_idle"])
        self.assertEqual(releases[0]["observed_node_boot_id"], BOOT)
        self.assertIn("blocked_gpu_busy", releases[0]["reason"])
        self.assertNotIn(int(lease["worker_index"]), claim_workers)
        retry = payload["infrastructure_retries"][str(lease["asset_id"])]
        self.assertEqual(retry["attempts"], 1)
        self.assertFalse(retry["exhausted"])
        self.assertEqual(
            payload["slots"][0]["detail"],
            "infrastructure_retry_scheduled:blocked_gpu_busy",
        )

    def test_local_source_preparation_exit_enters_bounded_retry_policy(self) -> None:
        lease = self.lease()
        self.write_runtime(
            lease,
            category="local_source_preparation",
            code="local_source_spool_model_verification_failed",
            attempted=0,
        )
        path = supervisor.runtime_state_path(self.config, lease)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["failure_stage"] = "local_source_preparation"
        payload["error"] = "local_source_spool_model_verification_failed"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(
            supervisor.load_infrastructure_runtime_episode(self.config, lease),
            "local_source_spool_model_verification_failed",
        )
        retries: dict[str, dict[str, object]] = {}
        episode: dict[str, object] = {}
        for attempt in range(1, supervisor.INFRA_RETRY_MAX_ATTEMPTS + 1):
            episode = supervisor.record_infrastructure_retry(
                retries,
                lease=lease,
                code="local_source_spool_model_verification_failed",
                now_epoch=1000.0 + attempt,
                runtime_generation="a" * 64,
            )
        self.assertEqual(
            episode["attempts"], supervisor.INFRA_RETRY_MAX_ATTEMPTS
        )
        self.assertTrue(episode["exhausted"])
        self.assertIn(
            str(lease["asset_id"]),
            supervisor.excluded_retry_assets(retries, now_epoch=99999.0),
        )

        payload.update({
            "failure_category": "worker_processing",
            "failure_stage": "worker_processing",
            "failure_code": "LocalSourceSpoolError",
        })
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(
            supervisor.load_infrastructure_runtime_episode(self.config, lease),
            "local_source_preparation_failed",
        )

        payload["attempted"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(
            supervisor.load_infrastructure_runtime_episode(self.config, lease)
        )

    def test_gpu_busy_contention_never_exhausts_asset_retry_budget(self) -> None:
        lease = self.lease()
        retries: dict[str, dict[str, object]] = {}
        episode: dict[str, object] = {}
        for attempt in range(1, 7):
            episode = supervisor.record_infrastructure_retry(
                retries,
                lease=lease,
                code="blocked_gpu_busy",
                now_epoch=1000.0 + attempt,
                runtime_generation="a" * 64,
            )
        self.assertEqual(episode["attempts"], 6)
        self.assertFalse(episode["exhausted"])
        self.assertEqual(episode["next_retry_at_epoch"], 1246.0)
        self.assertIn(
            str(lease["asset_id"]),
            supervisor.excluded_retry_assets(retries, now_epoch=1245.0),
        )
        self.assertNotIn(
            str(lease["asset_id"]),
            supervisor.excluded_retry_assets(retries, now_epoch=1247.0),
        )

        # A later, materially different runtime failure starts its own bounded
        # episode instead of inheriting six holder-handoff observations.
        runtime_episode = supervisor.record_infrastructure_retry(
            retries,
            lease=lease,
            code="gpu_process_uuid_attestation_failed",
            now_epoch=1300.0,
            runtime_generation="a" * 64,
        )
        self.assertEqual(runtime_episode["attempts"], 1)
        self.assertFalse(runtime_episode["exhausted"])

    def test_third_same_generation_infrastructure_exit_exhausts_asset(self) -> None:
        self.write_guard(now=1000.0)
        lease = self.lease()
        snapshot = self.authority_snapshot(self.catalog_row(lease))
        self.write_runtime(lease)
        generation = supervisor.trusted_runtime_generation(self.config.project)
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.state_path.write_text(
            json.dumps(
                {
                    "schema": supervisor.STATE_SCHEMA,
                    "observed_at_epoch": 955.0,
                    "slots": [],
                    "infrastructure_retries": {
                        lease["asset_id"]: {
                            "attempts": 2,
                            "next_retry_at_epoch": 960.0,
                            "exhausted": False,
                            "last_code": "worker_ssh_timeout",
                            "runtime_generation": generation,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        claims: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([lease]),
            authority=lambda *_args, **_kwargs: snapshot,
            claim=lambda **kwargs: claims.append(kwargs),
            heartbeat=lambda **_kwargs: self.fail("infra exit must not heartbeat"),
            complete=lambda **_kwargs: (_ for _ in ()).throw(
                cycle72.Cycle72Conflict("not terminal")
            ),
            release=lambda **_kwargs: lease,
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: lease,
            run_process=self.no_worker_runner,
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        retry = payload["infrastructure_retries"][str(lease["asset_id"])]
        self.assertEqual(retry["attempts"], 3)
        self.assertTrue(retry["exhausted"])
        self.assertEqual(payload["supply_status"], "supply_blocked")
        self.assertEqual(payload["infrastructure_retry_exhausted_count"], 1)
        self.assertTrue(
            all(lease["asset_id"] in item["excluded_asset_ids"] for item in claims)
        )

    def test_retry_generation_filters_legacy_and_old_but_keeps_current(self) -> None:
        current = "a" * 64
        old = "b" * 64
        base = {
            "attempts": 3,
            "next_retry_at_epoch": 9999.0,
            "exhausted": True,
        }
        selected = supervisor.retries_for_runtime_generation(
            {
                "legacy": dict(base),
                "old": {**base, "runtime_generation": old},
                "current": {**base, "runtime_generation": current},
            },
            current,
        )
        self.assertEqual(set(selected), {"current"})

    def test_new_runtime_generation_restarts_attempt_budget_at_one(self) -> None:
        lease = self.lease()
        retries = {
            str(lease["asset_id"]): {
                "attempts": 3,
                "next_retry_at_epoch": 9999.0,
                "exhausted": True,
                "runtime_generation": "a" * 64,
            }
        }
        episode = supervisor.record_infrastructure_retry(
            retries,
            lease=lease,
            code="worker_ssh_timeout",
            now_epoch=1000.0,
            runtime_generation="b" * 64,
        )
        self.assertEqual(episode["attempts"], 1)
        self.assertFalse(episode["exhausted"])
        self.assertEqual(episode["runtime_generation"], "b" * 64)

    def test_checkpoint_is_created_once_and_never_stops_cycle(self) -> None:
        self.write_guard(now=1000.0, default_action="probe_blocked")
        calls: list[Path] = []

        def checkpoint(**kwargs: object) -> dict[str, object]:
            directory = Path(kwargs["checkpoint_dir"])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "cycle-test.checkpoint.json"
            path.write_text("{}\n", encoding="utf-8")
            calls.append(path)
            return {"checkpoint_path": str(path), "continue_running": True}

        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([], checkpoint_due=True),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **_kwargs: self.fail("offline slots cannot claim"),
            heartbeat=lambda **_kwargs: self.fail("no lease"),
            complete=lambda **_kwargs: self.fail("no lease"),
            checkpoint=checkpoint,
            verify=lambda **_kwargs: {},
            run_process=self.no_worker_runner,
        )
        first = supervisor.supervise_once(self.config, dependency=dep)
        second = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["checkpoint_path"], str(calls[0]))
        self.assertIsNone(second["checkpoint_path"])

    def test_launch_attests_lease_and_disables_password_fallback(self) -> None:
        lease = self.lease()
        evidence = supervisor.GuardSlotEvidence(
            int(lease["remote_port"]),
            int(lease["gpu"]),
            int(lease["worker_index"]),
            "holder_ready",
            "verified",
            BOOT,
            1000.0,
        )
        verified: list[dict[str, object]] = []
        launches: list[tuple[list[str], dict[str, object]]] = []

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            launches.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(
                supervisor,
                "acquire_slot_control_lock",
                return_value=contextlib.nullcontext(object()),
            ),
            mock.patch.object(
                supervisor,
                "audit_local_worker",
                return_value=supervisor.LocalWorkerEvidence("exact", 1, 1),
            ),
            mock.patch.object(supervisor, "_release_verified_holder") as release_holder,
            mock.patch.object(supervisor, "_restore_transaction") as restore_holder,
        ):
            launched = supervisor.launch_exact_worker(
                self.config,
                evidence,
                lease,
                runner=runner,
                verifier=lambda **kwargs: verified.append(kwargs) or lease,
                settle=lambda _seconds: None,
            )
        self.assertTrue(launched)
        self.assertEqual(verified[0]["lease_id"], lease["lease_id"])
        self.assertEqual(
            launches[0][1]["env"]["TOTAL_ASSET_ALLOW_LEGACY_PASSWORD_SSH"], "0"
        )
        self.assertEqual(
            launches[0][1]["env"]["TOTAL_ASSET_PREFLIGHT_SSH_KEY"],
            str(self.identity),
        )
        self.assertEqual(
            launches[0][1]["env"]["TOTAL_ASSET_SSH_KNOWN_HOSTS"],
            str(self.known_hosts),
        )
        self.assertNotIn(str(self.identity), " ".join(launches[0][0]))
        self.assertNotIn(str(self.known_hosts), " ".join(launches[0][0]))
        self.assertEqual(launches[0][0][1], "-dmS")
        self.assertIn("--resume-unprocessed-only", " ".join(launches[0][0]))
        release_holder.assert_not_called()
        restore_holder.assert_not_called()

    def test_highqal_lease_uses_same_exact_worker_with_parameterized_roots(self) -> None:
        lease = self.lease(asset="highqal-asset-1")
        work_item_id = "highqal-asset-1--0123456789abcdef"
        manifest = self.root / "canary/manifest.json"
        status = self.root / "canary/status.jsonl"
        final_root = self.root / "canary/final"
        evidence_root = self.root / "canary/evidence"
        lease.update({
            "render_batch": "highqal",
            "workload_kind": supervisor.HIGHQAL_WORKLOAD_KIND,
            "work_item_id": work_item_id,
            "workload_generation": "a" * 64,
            "workload_config": {
                "manifest_path": str(manifest),
                "manifest_schema": supervisor.HIGHQAL_MANIFEST_SCHEMA,
                "status_path": str(status),
                "final_root": str(final_root),
                "evidence_root": str(evidence_root),
            },
        })
        command = supervisor.build_worker_command(self.config, lease)
        rendered = " ".join(command)
        self.assertIn(f"--queue {manifest}", rendered)
        self.assertIn("--batch highqal", rendered)
        self.assertNotIn("--batch-index", command)
        self.assertIn(f"--highqal-status-path {status}", rendered)
        self.assertIn(f"--highqal-final-root {final_root}", rendered)
        self.assertIn(f"--highqal-evidence-root {evidence_root}", rendered)
        self.assertIn(f"--work-item-id {work_item_id}", rendered)
        self.assertNotEqual(lease["asset_id"], lease["work_item_id"])
        self.assertIn("--defer-holder-to-shared-lock", command)
        self.assertEqual(command.count("--asset-id"), 1)

    def test_highqal_worker_rejects_old_manifest_schema_before_holder_handoff(self) -> None:
        lease = self.lease(asset="highqal-asset-1")
        lease.update({
            "render_batch": "highqal",
            "workload_kind": supervisor.HIGHQAL_WORKLOAD_KIND,
            "work_item_id": "highqal-asset-1",
            "workload_generation": "b" * 64,
            "workload_config": {
                "manifest_path": str(self.root / "manifest.json"),
                "manifest_schema": "highqal-source-work-manifest.v1",
                "status_path": str(self.root / "status.jsonl"),
                "final_root": str(self.root / "final"),
                "evidence_root": str(self.root / "evidence"),
            },
        })
        with self.assertRaisesRegex(
            supervisor.SupervisorError, "priority_workload_manifest_schema_invalid"
        ):
            supervisor.build_worker_command(self.config, lease)

    def test_highqal_runtime_attestation_includes_exact_workload_identity(self) -> None:
        lease = self.lease(asset="highqal-asset-1")
        lease.update({
            "render_batch": "highqal",
            "workload_kind": supervisor.HIGHQAL_WORKLOAD_KIND,
            "work_item_id": "highqal-asset-1--fedcba9876543210",
            "workload_generation": "c" * 64,
        })
        path = supervisor.runtime_state_path(self.config, lease)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "status": "running",
            "assignment_mode": supervisor.ASSIGNMENT_MODE,
            "cycle_id": lease["cycle_id"],
            "lease_id": lease["lease_id"],
            "worker_index": lease["worker_index"],
            "remote_port": lease["remote_port"],
            "gpu": lease["gpu"],
            "node_boot_id": lease["node_boot_id"],
            "workload_kind": lease["workload_kind"],
            "work_item_id": lease["work_item_id"],
            "workload_generation": lease["workload_generation"],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(
            supervisor.load_exact_worker_runtime_payload(self.config, lease), payload
        )
        payload["work_item_id"] = "other-item"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(
            supervisor.load_exact_worker_runtime_payload(self.config, lease)
        )

    def test_priority_schema_migration_preserves_active_lease_and_writes_backup(self) -> None:
        lease = self.make_v1_database_with_active_lease()
        receipt = self.state_root / "cutover/migration.json"
        payload = supervisor.migrate_priority_database(
            db_path=self.config.db_path,
            inventory=self.inventory,
            backup_dir=self.state_root / "cutover/backups",
            receipt_path=receipt,
            now_epoch=1234,
        )
        self.assertEqual(payload["status"], "migrated")
        self.assertEqual(payload["before"]["schema_version"], 1)
        self.assertEqual(payload["after"]["schema_version"], 2)
        self.assertEqual(payload["before"]["active_lease_ids"], [lease["lease_id"]])
        self.assertEqual(
            payload["after"]["lease_identity_sha256"],
            payload["before"]["lease_identity_sha256"],
        )
        self.assertTrue(Path(payload["backup_path"]).is_file())
        self.assertEqual(json.loads(receipt.read_text())["status"], "migrated")
        repeated = supervisor.migrate_priority_database(
            db_path=self.config.db_path,
            inventory=self.inventory,
            backup_dir=self.state_root / "cutover/backups",
            receipt_path=receipt,
            now_epoch=9999,
        )
        self.assertEqual(repeated, payload)
        self.assertEqual(
            [path.resolve() for path in (self.state_root / "cutover/backups").glob("*.sqlite3")],
            [Path(payload["backup_path"]).resolve()],
        )

    def test_priority_schema_migration_failure_uses_transaction_rollback_only(self) -> None:
        self.make_v1_database_with_active_lease()
        before = supervisor._priority_migration_database_snapshot(self.config.db_path)
        receipt = self.state_root / "cutover/migration.json"

        def partial_mutation(connection: sqlite3.Connection) -> None:
            connection.execute("ALTER TABLE leases ADD COLUMN injected TEXT")
            connection.execute(
                "UPDATE metadata SET value='999' WHERE key='db_schema_version'"
            )
            raise cycle72.Cycle72DataError("injected migration failure")

        with mock.patch.object(
            supervisor, "_migrate_v1_to_v2", side_effect=partial_mutation
        ):
            with self.assertRaisesRegex(
                supervisor.SupervisorError, "priority_migration_failed_and_rolled_back"
            ):
                supervisor.migrate_priority_database(
                    db_path=self.config.db_path,
                    inventory=self.inventory,
                    backup_dir=self.state_root / "cutover/backups",
                    receipt_path=receipt,
                    now_epoch=1234,
                )
        self.assertEqual(
            supervisor._priority_migration_database_snapshot(self.config.db_path), before
        )
        recorded = json.loads(receipt.read_text())
        self.assertEqual(recorded["status"], "rolled_back")
        self.assertFalse(recorded["database_replaced"])
        self.assertTrue(Path(recorded["backup_path"]).is_file())

    def test_priority_schema_migration_serializes_concurrent_heartbeat_without_loss(
        self,
    ) -> None:
        lease = self.make_v1_database_with_active_lease()
        receipt = self.state_root / "cutover/migration.json"
        migration_entered = threading.Event()
        writer_attempted = threading.Event()
        writer_done = threading.Event()
        writer_errors: list[BaseException] = []
        original = supervisor._migrate_v1_to_v2

        def heartbeat_writer() -> None:
            try:
                self.assertTrue(migration_entered.wait(timeout=5))
                connection = sqlite3.connect(
                    self.config.db_path, timeout=10, isolation_level=None
                )
                try:
                    connection.execute("PRAGMA busy_timeout=10000")
                    writer_attempted.set()
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE leases SET heartbeat_at_epoch=222, "
                        "expires_at_epoch=333 WHERE lease_id=?",
                        (lease["lease_id"],),
                    )
                    connection.execute("COMMIT")
                finally:
                    connection.close()
            except BaseException as exc:  # pragma: no cover - asserted below
                writer_errors.append(exc)
            finally:
                writer_done.set()

        writer = threading.Thread(target=heartbeat_writer, daemon=True)
        writer.start()

        def guarded_migration(connection: sqlite3.Connection) -> None:
            migration_entered.set()
            self.assertTrue(writer_attempted.wait(timeout=5))
            time.sleep(0.05)
            self.assertFalse(writer_done.is_set())
            original(connection)

        with mock.patch.object(
            supervisor, "_migrate_v1_to_v2", side_effect=guarded_migration
        ):
            payload = supervisor.migrate_priority_database(
                db_path=self.config.db_path,
                inventory=self.inventory,
                backup_dir=self.state_root / "cutover/backups",
                receipt_path=receipt,
                now_epoch=1234,
            )
        writer.join(timeout=10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(writer_errors, [])
        with sqlite3.connect(self.config.db_path) as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='db_schema_version'"
            ).fetchone()[0]
            heartbeat = connection.execute(
                "SELECT heartbeat_at_epoch, expires_at_epoch FROM leases "
                "WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone()
        self.assertEqual(version, "2")
        self.assertEqual(heartbeat, (222.0, 333.0))
        # Idempotent resume uses the in-database migration marker, not a stale
        # heartbeat-bearing snapshot from the receipt.
        self.assertEqual(
            supervisor.migrate_priority_database(
                db_path=self.config.db_path,
                inventory=self.inventory,
                backup_dir=self.state_root / "cutover/backups",
                receipt_path=receipt,
                now_epoch=9999,
            ),
            payload,
        )

    def test_failed_migration_does_not_overwrite_concurrent_heartbeat(self) -> None:
        lease = self.make_v1_database_with_active_lease()
        receipt = self.state_root / "cutover/migration.json"
        migration_entered = threading.Event()
        writer_attempted = threading.Event()
        writer_errors: list[BaseException] = []

        def heartbeat_writer() -> None:
            try:
                self.assertTrue(migration_entered.wait(timeout=5))
                connection = sqlite3.connect(
                    self.config.db_path, timeout=10, isolation_level=None
                )
                try:
                    connection.execute("PRAGMA busy_timeout=10000")
                    writer_attempted.set()
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE leases SET heartbeat_at_epoch=444, "
                        "expires_at_epoch=555 WHERE lease_id=?",
                        (lease["lease_id"],),
                    )
                    connection.execute("COMMIT")
                finally:
                    connection.close()
            except BaseException as exc:  # pragma: no cover - asserted below
                writer_errors.append(exc)

        writer = threading.Thread(target=heartbeat_writer, daemon=True)
        writer.start()

        def failing_migration(connection: sqlite3.Connection) -> None:
            migration_entered.set()
            self.assertTrue(writer_attempted.wait(timeout=5))
            time.sleep(0.05)
            connection.execute("ALTER TABLE leases ADD COLUMN injected TEXT")
            raise cycle72.Cycle72DataError("injected migration failure")

        with mock.patch.object(
            supervisor, "_migrate_v1_to_v2", side_effect=failing_migration
        ):
            with self.assertRaisesRegex(
                supervisor.SupervisorError, "priority_migration_failed_and_rolled_back"
            ):
                supervisor.migrate_priority_database(
                    db_path=self.config.db_path,
                    inventory=self.inventory,
                    backup_dir=self.state_root / "cutover/backups",
                    receipt_path=receipt,
                    now_epoch=1234,
                )
        writer.join(timeout=10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(writer_errors, [])
        with sqlite3.connect(self.config.db_path) as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='db_schema_version'"
            ).fetchone()[0]
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(leases)")
            }
            heartbeat = connection.execute(
                "SELECT heartbeat_at_epoch, expires_at_epoch FROM leases "
                "WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone()
        self.assertEqual(version, "1")
        self.assertNotIn("injected", columns)
        self.assertTrue(set(cycle72._V2_LEASE_ADDITIONS).isdisjoint(columns))
        self.assertEqual(heartbeat, (444.0, 555.0))
        self.assertFalse(json.loads(receipt.read_text())["database_replaced"])

    def test_cutover_restore_uses_migration_receipt_and_recovers_v1(self) -> None:
        self.make_v1_database_with_active_lease()
        receipt = self.state_root / "cutover/migration.json"
        migrated = supervisor.migrate_priority_database(
            db_path=self.config.db_path,
            inventory=self.inventory,
            backup_dir=self.state_root / "cutover/backups",
            receipt_path=receipt,
            now_epoch=1234,
        )
        restored = supervisor.restore_priority_database_migration(
            db_path=self.config.db_path, receipt_path=receipt
        )
        self.assertEqual(restored["status"], "restored_after_cutover_failure")
        self.assertEqual(restored["after"], migrated["before"])
        self.assertEqual(
            supervisor._priority_migration_database_snapshot(self.config.db_path)[
                "schema_version"
            ],
            1,
        )
        repeated = supervisor.restore_priority_database_migration(
            db_path=self.config.db_path, receipt_path=receipt
        )
        self.assertEqual(repeated, restored)

    def test_cutover_restore_refuses_any_post_migration_heartbeat(self) -> None:
        lease = self.make_v1_database_with_active_lease()
        receipt = self.state_root / "cutover/migration.json"
        supervisor.migrate_priority_database(
            db_path=self.config.db_path,
            inventory=self.inventory,
            backup_dir=self.state_root / "cutover/backups",
            receipt_path=receipt,
            now_epoch=1234,
        )
        with sqlite3.connect(self.config.db_path) as connection:
            connection.execute(
                "UPDATE leases SET heartbeat_at_epoch=777, expires_at_epoch=888 "
                "WHERE lease_id=?",
                (lease["lease_id"],),
            )
            connection.commit()
        with self.assertRaisesRegex(
            supervisor.SupervisorError,
            "priority_migration_restore_database_mutated",
        ):
            supervisor.restore_priority_database_migration(
                db_path=self.config.db_path, receipt_path=receipt
            )
        with sqlite3.connect(self.config.db_path) as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='db_schema_version'"
            ).fetchone()[0]
            heartbeat = connection.execute(
                "SELECT heartbeat_at_epoch, expires_at_epoch FROM leases "
                "WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone()
        self.assertEqual(version, "2")
        self.assertEqual(heartbeat, (777.0, 888.0))

    def test_initial_screen_launch_failure_keeps_verified_holder_untouched(self) -> None:
        lease = self.lease()
        evidence = supervisor.GuardSlotEvidence(
            int(lease["remote_port"]),
            int(lease["gpu"]),
            int(lease["worker_index"]),
            "holder_ready",
            "verified",
            BOOT,
            1000.0,
        )

        def failed_runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[:2], ["screen", "-dmS"])
            return subprocess.CompletedProcess(command, 1, "", "screen failed")

        with (
            mock.patch.object(
                supervisor,
                "acquire_slot_control_lock",
                return_value=contextlib.nullcontext(object()),
            ),
            mock.patch.object(supervisor, "_release_verified_holder") as release_holder,
            mock.patch.object(supervisor, "_restore_transaction") as restore_holder,
        ):
            with self.assertRaisesRegex(
                supervisor.SupervisorError, "worker_screen_launch_failed"
            ):
                supervisor.launch_exact_worker(
                    self.config,
                    evidence,
                    lease,
                    runner=failed_runner,
                    verifier=lambda **_kwargs: lease,
                    settle=lambda _seconds: None,
                )

        release_holder.assert_not_called()
        restore_holder.assert_not_called()

    def test_duplicate_exact_process_fails_closed(self) -> None:
        lease = self.lease()
        name = supervisor.exact_worker_screen_name(lease)
        worker = " ".join(supervisor.build_worker_command(self.config, lease))

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["screen", "-ls"]:
                return subprocess.CompletedProcess(command, 0, f"1.{name}\n", "")
            return subprocess.CompletedProcess(command, 0, f"1 {worker}\n2 {worker}\n", "")

        with self.assertRaisesRegex(supervisor.SupervisorError, "duplicate"):
            supervisor.audit_local_worker(lease, runner=runner)

    def test_local_worker_audit_retains_exact_screen_and_process_pids(self) -> None:
        lease = self.lease()
        name = supervisor.exact_worker_screen_name(lease)
        worker = " ".join(supervisor.build_worker_command(self.config, lease))

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["screen", "-ls"]:
                return subprocess.CompletedProcess(
                    command, 0, f"123.{name}\t(Detached)\n", ""
                )
            return subprocess.CompletedProcess(command, 0, f"456 {worker}\n", "")

        evidence = supervisor.audit_local_worker(lease, runner=runner)
        self.assertTrue(evidence.exact_active)
        self.assertEqual(evidence.screen_pid, 123)
        self.assertEqual(evidence.process_pid, 456)
        self.assertEqual(evidence.process_command, worker)

    def test_nonactive_exact_local_controller_is_audited_from_live_argv(self) -> None:
        lease = self.make_v1_database_with_active_lease()
        with sqlite3.connect(self.config.db_path) as connection:
            connection.execute(
                """UPDATE leases SET state='completed', completed_at_epoch=20
                   WHERE lease_id=?""",
                (lease["lease_id"],),
            )
            connection.commit()
        lease["state"] = "completed"
        name = supervisor.exact_worker_screen_name(lease)
        worker = " ".join(supervisor.build_worker_command(self.config, lease))

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["screen", "-ls"]:
                return subprocess.CompletedProcess(
                    command, 0, f"123.{name}\t(Detached)\n", ""
                )
            if command[:4] == ["ps", "axww", "-o", "pid=,command="]:
                return subprocess.CompletedProcess(command, 0, f"456 {worker}\n", "")
            raise AssertionError(command)

        orphaned = supervisor.audit_nonactive_local_worker_leases(
            db_path=self.config.db_path,
            runner=runner,
        )
        self.assertEqual(len(orphaned), 1)
        self.assertEqual(orphaned[0]["lease_id"], lease["lease_id"])
        self.assertEqual(orphaned[0]["state"], "completed")
        self.assertEqual(orphaned[0]["local_process_pid"], 456)
        self.assertEqual(orphaned[0]["local_screen_pid"], 123)

    def test_active_exact_local_controller_is_not_an_orphan(self) -> None:
        lease = self.make_v1_database_with_active_lease()
        name = supervisor.exact_worker_screen_name(lease)
        worker = " ".join(supervisor.build_worker_command(self.config, lease))

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["screen", "-ls"]:
                return subprocess.CompletedProcess(command, 0, f"123.{name}\n", "")
            if command[:4] == ["ps", "axww", "-o", "pid=,command="]:
                return subprocess.CompletedProcess(command, 0, f"456 {worker}\n", "")
            raise AssertionError(command)

        self.assertEqual(
            supervisor.audit_nonactive_local_worker_leases(
                db_path=self.config.db_path,
                runner=runner,
            ),
            (),
        )

    def test_nonactive_local_controller_blocks_all_new_claims(self) -> None:
        self.write_guard(now=1000.0)
        orphan = self.lease(0, asset="asset-stale-controller")
        orphan["state"] = "completed"
        orphan["local_process_pid"] = 456
        orphan["local_screen_pid"] = 123
        claims: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(kwargs),
            heartbeat=lambda **_kwargs: self.fail("no active lease"),
            complete=lambda **_kwargs: self.fail("no active lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: self.fail("no active lease"),
            run_process=self.no_worker_runner,
            nonactive_local_leases=lambda **_kwargs: [orphan],
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(claims, [])
        self.assertTrue(payload["nonactive_worker_claim_barrier"]["active"])
        self.assertEqual(
            payload["nonactive_worker_claim_barrier"]["local_controllers"][0][
                "asset_id"
            ],
            "asset-stale-controller",
        )
        self.assertTrue(
            all(
                item["detail"] == "waiting_nonactive_worker_reconciliation"
                for item in payload["slots"]
            )
        )

    def test_remote_worker_without_active_lease_blocks_cross_slot_claims(self) -> None:
        self.write_guard(now=1000.0, actions={0: "worker_active"})
        claims: list[dict[str, object]] = []
        dep = supervisor.Dependencies(
            now=lambda: 1000.0,
            status=lambda **_kwargs: self.status_payload([]),
            authority=lambda *_args, **_kwargs: object(),
            claim=lambda **kwargs: claims.append(kwargs),
            heartbeat=lambda **_kwargs: self.fail("no active lease"),
            complete=lambda **_kwargs: self.fail("no active lease"),
            checkpoint=lambda **_kwargs: self.fail("checkpoint not due"),
            verify=lambda **_kwargs: self.fail("no active lease"),
            run_process=self.no_worker_runner,
            nonactive_local_leases=lambda **_kwargs: [],
        )
        payload = supervisor.supervise_once(self.config, dependency=dep)
        self.assertEqual(claims, [])
        barrier = payload["nonactive_worker_claim_barrier"]
        self.assertTrue(barrier["active"])
        self.assertEqual(barrier["remote_workers_without_active_lease"], [0])
        self.assertTrue(
            all(
                item["detail"]
                in {"waiting_nonactive_worker_reconciliation", "worker_active"}
                for item in payload["slots"]
            )
        )

    def test_status_cli_is_strictly_read_only_and_needs_no_ssh_key(self) -> None:
        expected = {"schema": supervisor.STATE_SCHEMA, "status": "healthy"}
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.state_path.write_text(json.dumps(expected), encoding="utf-8")
        parse_cache = self.state_root / "authority_parse_cache.sqlite3"
        with mock.patch.dict(
            "os.environ", {"TOTAL_ASSET_PREFLIGHT_SSH_KEY": ""}, clear=False
        ):
            with (
                mock.patch("builtins.print") as output,
                mock.patch.object(
                    supervisor,
                    "PersistentAuthorityParseCache",
                    side_effect=AssertionError("status must not construct a cache"),
                ),
            ):
                result = supervisor.main([
                    "status",
                    "--state-root",
                    str(self.state_root),
                    "--state-path",
                    str(self.config.state_path),
                ])
        self.assertEqual(result, 0)
        rendered = output.call_args.args[0]
        self.assertEqual(json.loads(rendered), expected)
        self.assertFalse(parse_cache.exists())

    def test_run_mode_binds_persistent_cache_after_single_instance_lock(self) -> None:
        cache_path = self.state_root / "authority_parse_cache.sqlite3"
        config = replace(
            self.config,
            authority_parse_cache_path=cache_path,
        )
        persistent_cache = mock.MagicMock()
        persistent_cache.__enter__.return_value = persistent_cache
        persistent_cache.__exit__.return_value = None
        seen_dependencies: list[supervisor.Dependencies] = []

        def supervise(
            _config: supervisor.SupervisorConfig,
            *,
            dependency: supervisor.Dependencies,
        ) -> dict[str, object]:
            seen_dependencies.append(dependency)
            return {"schema": supervisor.STATE_SCHEMA, "status": "healthy"}

        with (
            mock.patch.object(supervisor, "_validate_config"),
            mock.patch.object(supervisor, "validate_local_database_path") as validate,
            mock.patch.object(
                supervisor,
                "PersistentAuthorityParseCache",
                return_value=persistent_cache,
            ) as construct,
            mock.patch.object(supervisor, "supervise_once", side_effect=supervise),
            mock.patch("builtins.print"),
        ):
            result = supervisor.run_locked(config, once=True)

        self.assertEqual(result, 0)
        resolved_cache_path = supervisor._authority_parse_cache_path(config)
        validate.assert_called_once_with(resolved_cache_path, self.inventory)
        construct.assert_called_once_with(resolved_cache_path)
        self.assertEqual(len(seen_dependencies), 1)
        self.assertIsNot(
            seen_dependencies[0].authority,
            supervisor.load_authority_snapshot,
        )


if __name__ == "__main__":
    unittest.main()
