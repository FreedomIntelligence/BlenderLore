from __future__ import annotations

import argparse
import contextlib
import fcntl
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_holder_guard as guard
from total_asset_remote_preflight import GpuObservation, NodeObservation, RemoteProbeError
from total_asset_scheduler import WorkerClaim
from total_asset_topology import REQUIRED_REMOTE_GPU_LAYOUT


def gpu_observation(
    gpu: int,
    *,
    holder: bool = False,
    compute_kinds: tuple[str, ...] = (),
    lock_state: str = "available",
    wrapper: bool = False,
    compute_query_ok: bool = True,
    physical_binding_ok: bool = True,
) -> GpuObservation:
    gpu_uuid = f"GPU-00000000-0000-0000-0000-{gpu:012d}"
    return GpuObservation(
        gpu=gpu,
        gpu_query_ok=True,
        compute_query_ok=compute_query_ok,
        compute_process_count=len(compute_kinds),
        compute_process_kinds=compute_kinds,
        holder_session=holder,
        lock_state=lock_state,
        wrapper_process_present=wrapper,
        gpu_uuid=gpu_uuid,
        physical_binding_ok=physical_binding_ok,
        compute_process_gpu_uuids=tuple(gpu_uuid for _kind in compute_kinds),
        holder_descendant_ok=holder,
        holder_lock_owner_ok=holder,
        holder_command_ok=holder,
    )


def node_observation(
    port: int,
    gpus: tuple[int, ...],
    *,
    holders: frozenset[int] = frozenset(),
    busy: frozenset[int] = frozenset(),
    workers: frozenset[int] = frozenset(),
) -> NodeObservation:
    observations = []
    for gpu in gpus:
        if gpu in holders:
            observations.append(gpu_observation(
                gpu, holder=True, compute_kinds=("holder",), lock_state="busy"
            ))
        elif gpu in busy:
            observations.append(gpu_observation(
                gpu, compute_kinds=("blender",), lock_state="busy"
            ))
        elif gpu in workers:
            observations.append(gpu_observation(
                gpu,
                compute_kinds=("blender",),
                lock_state="busy",
                wrapper=True,
            ))
        else:
            observations.append(gpu_observation(gpu))
    return NodeObservation(
        port=port,
        probe_ok=True,
        holder_audit_ok=True,
        unexpected_holder_sessions=0,
        legacy_holder_sessions=0,
        gpus=tuple(observations),
    )


def boot_id(port: int, generation: int = 1) -> str:
    return f"{generation:08x}-0000-0000-0000-{port:012x}"


class TotalAssetHolderGuardTests(unittest.TestCase):
    def run_audit(
        self,
        *,
        ownership: guard.LocalOwnership | None = None,
        nodes: dict[int, NodeObservation] | None = None,
        reachable: frozenset[int] | None = None,
        starts: list[tuple[int, int]] | None = None,
        previous_boot_ids: dict[int, str] | None = None,
        boot_ids: dict[int, str] | None = None,
        boot_errors: frozenset[int] = frozenset(),
        slot_lock_root: Path | None = None,
        extra_probe_gpus: dict[int, tuple[int, ...]] | None = None,
    ) -> dict[str, object]:
        nodes = nodes or {
            port: node_observation(port, tuple(gpus))
            for port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        reachable = frozenset(nodes) if reachable is None else reachable
        starts = [] if starts is None else starts
        boot_ids = boot_ids or {
            port: boot_id(port) for port in REQUIRED_REMOTE_GPU_LAYOUT
        }

        def probe(port: int, _gpus: tuple[int, ...]) -> NodeObservation:
            value = nodes.get(port)
            if value is None:
                raise RemoteProbeError("ssh_exit_nonzero")
            return value

        def probe_boot_id(port: int) -> str:
            if port in boot_errors:
                raise RemoteProbeError("boot_id_auth_or_remote_failed")
            return boot_ids[port]

        context = (
            contextlib.nullcontext(slot_lock_root)
            if slot_lock_root is not None
            else tempfile.TemporaryDirectory()
        )
        with context as value:
            effective_slot_root = (
                Path(value) if slot_lock_root is None else slot_lock_root
            )
            return guard.audit_and_protect(
                ssh_host="root@render.example",
                identity_file=Path("/key"),
                known_hosts=Path("/known-hosts"),
                local_ownership=ownership or guard.LocalOwnership(frozenset(), {}),
                previous_boot_ids=previous_boot_ids,
                tcp_probe=lambda _host, port, _timeout: port in reachable,
                node_probe=probe,
                boot_id_probe=probe_boot_id,
                holder_starter=lambda port, gpu: starts.append((port, gpu)),
                local_ownership_probe=lambda: (
                    ownership
                    or guard.LocalOwnership(frozenset(), {})
                ),
                extra_probe_gpus=extra_probe_gpus,
                slot_lock_root=effective_slot_root,
            )

    def test_idle_slots_are_held_without_scheduler_or_batch_gate(self) -> None:
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(starts=starts)
        expected = sorted(
            (port, gpu)
            for port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
            for gpu in gpus
        )
        self.assertEqual(sorted(starts), expected)
        self.assertEqual(payload["holder_started_count"], len(expected))

    def test_scoped_sibling_probe_gap_does_not_block_idle_holder_start(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpus = tuple(REQUIRED_REMOTE_GPU_LAYOUT[port])
        affected_gpu, idle_gpu = gpus[:2]
        observations = tuple(
            gpu_observation(
                gpu,
                compute_query_ok=False,
                physical_binding_ok=False,
                lock_state="busy",
                wrapper=True,
            )
            if gpu == affected_gpu
            else gpu_observation(gpu)
            for gpu in gpus
        )
        nodes = {
            candidate_port: node_observation(
                candidate_port, tuple(candidate_gpus)
            )
            for candidate_port, candidate_gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        nodes[port] = NodeObservation(
            port=port,
            probe_ok=True,
            holder_audit_ok=True,
            unexpected_holder_sessions=0,
            legacy_holder_sessions=0,
            gpus=observations,
        )
        starts: list[tuple[int, int]] = []

        payload = self.run_audit(nodes=nodes, starts=starts)

        self.assertNotIn((port, affected_gpu), starts)
        self.assertIn((port, idle_gpu), starts)
        events = {
            (int(event["remote_port"]), int(event["gpu"])): event
            for event in payload["events"]
        }
        self.assertEqual(
            events[(port, affected_gpu)]["action"],
            "remote_busy_or_ambiguous",
        )
        self.assertEqual(events[(port, idle_gpu)]["action"], "holder_started")

    def test_waiting_chain_is_not_local_ownership(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        starts: list[tuple[int, int]] = []
        with mock.patch.object(guard, "detect_worker_claims", return_value=()), \
             mock.patch.object(guard, "detect_screen_controllers", return_value=()), \
             mock.patch.object(
                 guard,
                 "detect_screen_session_names",
                 return_value=(f"total_asset_slot_chain_p{port}_g{gpu}_w0",),
             ):
            ownership = guard.snapshot_local_ownership()
        self.assertNotIn((port, gpu), ownership.owned_slots)
        self.run_audit(ownership=ownership, starts=starts)
        self.assertIn((port, gpu), starts)

    def test_cycle72_worker_screen_is_exact_local_ownership(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        worker = guard.CANONICAL_WORKER_INDEX_BY_LOCATION[(port, gpu)]
        session = (
            f"total_asset_cycle72_p{port}_g{gpu}_w{worker}_"
            "l0123456789abcdef"
        )
        claim = WorkerClaim("batch0004", port, gpu, worker, 11)
        with mock.patch.object(
                 guard, "detect_worker_claims", return_value=(claim,)
             ), \
             mock.patch.object(guard, "detect_screen_controllers", return_value=()), \
             mock.patch.object(
                 guard, "detect_screen_session_names", return_value=(session,)
             ):
            ownership = guard.snapshot_local_ownership()
        self.assertIn((port, gpu), ownership.owned_slots)
        self.assertEqual(
            ownership.reasons[(port, gpu)],
            ("cycle72_worker_controller", "worker"),
        )

    def test_highqal_cycle72_worker_screen_is_exact_local_ownership(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[-1]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        worker = guard.CANONICAL_WORKER_INDEX_BY_LOCATION[(port, gpu)]
        session = (
            f"total_asset_cycle72_p{port}_g{gpu}_w{worker}_"
            "l0123456789abcdef"
        )
        claim = WorkerClaim("highqal", port, gpu, worker, 11)
        with mock.patch.object(
                 guard, "detect_worker_claims", return_value=(claim,)
             ), \
             mock.patch.object(guard, "detect_screen_controllers", return_value=()), \
             mock.patch.object(
                 guard, "detect_screen_session_names", return_value=(session,)
             ):
            ownership = guard.snapshot_local_ownership()
        self.assertIn((port, gpu), ownership.owned_slots)
        self.assertEqual(
            ownership.reasons[(port, gpu)],
            ("cycle72_worker_controller", "worker"),
        )

    def test_ended_cycle72_screen_without_python_worker_is_not_owner(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        worker = guard.CANONICAL_WORKER_INDEX_BY_LOCATION[(port, gpu)]
        session = (
            f"total_asset_cycle72_p{port}_g{gpu}_w{worker}_"
            "l0123456789abcdef"
        )
        with mock.patch.object(guard, "detect_worker_claims", return_value=()), \
             mock.patch.object(guard, "detect_screen_controllers", return_value=()), \
             mock.patch.object(
                 guard, "detect_screen_session_names", return_value=(session,)
             ):
            ownership = guard.snapshot_local_ownership()
        self.assertNotIn((port, gpu), ownership.owned_slots)

    def test_cycle72_worker_screen_with_wrong_worker_is_not_owner(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        worker = guard.CANONICAL_WORKER_INDEX_BY_LOCATION[(port, gpu)] + 1
        session = (
            f"total_asset_cycle72_p{port}_g{gpu}_w{worker}_"
            "l0123456789abcdef"
        )
        with mock.patch.object(guard, "detect_worker_claims", return_value=()), \
             mock.patch.object(guard, "detect_screen_controllers", return_value=()), \
             mock.patch.object(
                 guard, "detect_screen_session_names", return_value=(session,)
             ):
            ownership = guard.snapshot_local_ownership()
        self.assertNotIn((port, gpu), ownership.owned_slots)

    def test_controller_appearing_during_remote_probe_is_not_holder_ready(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        worker = guard.CANONICAL_WORKER_INDEX_BY_LOCATION[slot]
        for wrapper, expected_action in (
            (False, "worker_preparing"),
            (True, "handoff_ready"),
        ):
            with self.subTest(wrapper=wrapper):
                selected = gpu_observation(
                    gpu,
                    holder=True,
                    compute_kinds=("holder",),
                    lock_state="busy",
                    wrapper=wrapper,
                )
                observations = {
                    item_port: (
                        NodeObservation(
                            port=item_port,
                            probe_ok=True,
                            holder_audit_ok=True,
                            unexpected_holder_sessions=0,
                            legacy_holder_sessions=0,
                            gpus=tuple(
                                selected
                                if item_gpu == gpu
                                else gpu_observation(item_gpu)
                                for item_gpu in gpus
                            ),
                        )
                        if item_port == port
                        else node_observation(item_port, tuple(gpus))
                    )
                    for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
                }
                snapshots = iter(
                    (
                        guard.LocalOwnership(frozenset(), {}),
                        guard.LocalOwnership(
                            frozenset({slot}),
                            {slot: ("cycle72_worker_controller",)},
                        ),
                    )
                )
                starts: list[tuple[int, int]] = []

                with tempfile.TemporaryDirectory() as temporary:
                    payload = guard.audit_and_protect(
                        ssh_host="root@render.example",
                        identity_file=Path("/key"),
                        known_hosts=Path("/known-hosts"),
                        tcp_probe=lambda _host, _port, _timeout: True,
                        node_probe=lambda item_port, _gpus: observations[item_port],
                        boot_id_probe=lambda item_port: boot_id(item_port),
                        holder_starter=lambda item_port, item_gpu: starts.append(
                            (item_port, item_gpu)
                        ),
                        local_ownership_probe=lambda: next(snapshots),
                        slot_lock_root=Path(temporary),
                    )

                event = next(
                    item for item in payload["events"]
                    if item["remote_port"] == port and item["gpu"] == gpu
                )
                self.assertEqual(event["worker_index"], worker)
                self.assertEqual(event["action"], expected_action)
                self.assertNotEqual(event["action"], "holder_ready")
                self.assertNotIn(slot, starts)

    def test_post_probe_local_audit_failure_blocks_before_holder_mutation(self) -> None:
        calls = 0
        starts: list[tuple[int, int]] = []
        observations = {
            port: node_observation(port, tuple(gpus))
            for port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }

        def ownership_probe() -> guard.LocalOwnership:
            nonlocal calls
            calls += 1
            if calls == 1:
                return guard.LocalOwnership(frozenset(), {})
            raise guard.HolderGuardError("local_ownership_audit_failed")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                guard.HolderGuardError, "local_ownership_audit_failed"
            ):
                guard.audit_and_protect(
                    ssh_host="root@render.example",
                    identity_file=Path("/key"),
                    known_hosts=Path("/known-hosts"),
                    tcp_probe=lambda _host, _port, _timeout: True,
                    node_probe=lambda port, _gpus: observations[port],
                    boot_id_probe=lambda port: boot_id(port),
                    holder_starter=lambda port, gpu: starts.append((port, gpu)),
                    local_ownership_probe=ownership_probe,
                    slot_lock_root=Path(temporary),
                )

        self.assertEqual(calls, 2)
        self.assertEqual(starts, [])

    def test_controller_ending_during_probe_does_not_block_holder_recovery(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        observations = {
            item_port: node_observation(item_port, tuple(gpus))
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        empty = guard.LocalOwnership(frozenset(), {})
        snapshots = itertools.chain(
            (
                guard.LocalOwnership(
                    frozenset({slot}),
                    {slot: ("cycle72_worker_controller", "worker")},
                ),
            ),
            itertools.repeat(empty),
        )
        starts: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as temporary:
            payload = guard.audit_and_protect(
                ssh_host="root@render.example",
                identity_file=Path("/key"),
                known_hosts=Path("/known-hosts"),
                tcp_probe=lambda _host, _port, _timeout: True,
                node_probe=lambda item_port, _gpus: observations[item_port],
                boot_id_probe=lambda item_port: boot_id(item_port),
                holder_starter=lambda item_port, item_gpu: starts.append(
                    (item_port, item_gpu)
                ),
                local_ownership_probe=lambda: next(snapshots),
                slot_lock_root=Path(temporary),
            )

        self.assertIn(slot, starts)
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "holder_started")

    def test_holder_with_local_cycle72_owner_waits_for_remote_wrapper(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        nodes = {
            item_port: node_observation(
                item_port,
                tuple(gpus),
                holders=frozenset({gpu}) if item_port == port else frozenset(),
            )
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }

        payload = self.run_audit(
            ownership=guard.LocalOwnership(
                frozenset({slot}), {slot: ("cycle72_worker_controller",)}
            ),
            nodes=nodes,
        )
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )

        self.assertEqual(event["action"], "worker_preparing")
        self.assertNotIn(event["action"], {"holder_ready", "handoff_ready"})

    def test_holder_wrapper_and_exact_cycle72_owner_is_handoff_ready(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        wrapped_holder = gpu_observation(
            gpu,
            holder=True,
            compute_kinds=("holder",),
            lock_state="busy",
            wrapper=True,
        )
        nodes = {
            item_port: (
                NodeObservation(
                    port=item_port,
                    probe_ok=True,
                    holder_audit_ok=True,
                    unexpected_holder_sessions=0,
                    legacy_holder_sessions=0,
                    gpus=tuple(
                        wrapped_holder if item_gpu == gpu else gpu_observation(item_gpu)
                        for item_gpu in gpus
                    ),
                )
                if item_port == port
                else node_observation(item_port, tuple(gpus))
            )
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }

        payload = self.run_audit(
            ownership=guard.LocalOwnership(
                frozenset({slot}), {slot: ("cycle72_worker_controller",)}
            ),
            nodes=nodes,
        )
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )

        self.assertEqual(event["action"], "handoff_ready")

    def test_holder_wrapper_without_exact_cycle72_owner_never_authorizes_handoff(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        wrapped_holder = gpu_observation(
            gpu,
            holder=True,
            compute_kinds=("holder",),
            lock_state="busy",
            wrapper=True,
        )
        nodes = {
            item_port: (
                NodeObservation(
                    port=item_port,
                    probe_ok=True,
                    holder_audit_ok=True,
                    unexpected_holder_sessions=0,
                    legacy_holder_sessions=0,
                    gpus=tuple(
                        wrapped_holder if item_gpu == gpu else gpu_observation(item_gpu)
                        for item_gpu in gpus
                    ),
                )
                if item_port == port
                else node_observation(item_port, tuple(gpus))
            )
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        ownership_cases = (
            guard.LocalOwnership(frozenset(), {}),
            guard.LocalOwnership(frozenset({slot}), {slot: ("worker",)}),
        )

        for ownership in ownership_cases:
            with self.subTest(reasons=ownership.reasons.get(slot, ())):
                payload = self.run_audit(ownership=ownership, nodes=nodes)
                event = next(
                    item for item in payload["events"]
                    if item["remote_port"] == port and item["gpu"] == gpu
                )
                self.assertEqual(event["action"], "remote_busy_or_ambiguous")
                self.assertEqual(
                    event["detail"],
                    "holder_wrapper_without_exact_cycle72_owner",
                )

    def test_remote_busy_local_worker_or_finalizer_never_gets_holder(self) -> None:
        ports = sorted(REQUIRED_REMOTE_GPU_LAYOUT)
        worker_slot = (ports[0], REQUIRED_REMOTE_GPU_LAYOUT[ports[0]][0])
        finalizer_slot = (ports[-1], REQUIRED_REMOTE_GPU_LAYOUT[ports[-1]][-1])
        ownership = guard.LocalOwnership(
            frozenset({worker_slot, finalizer_slot}),
            {worker_slot: ("worker",), finalizer_slot: ("holder_finalizer",)},
        )
        nodes = {
            port: node_observation(
                port,
                tuple(gpus),
                busy=frozenset(
                    gpu for item_port, gpu in (worker_slot, finalizer_slot)
                    if item_port == port
                ),
            )
            for port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(
            ownership=ownership, nodes=nodes, starts=starts
        )
        self.assertNotIn(worker_slot, starts)
        self.assertNotIn(finalizer_slot, starts)
        self.assertEqual(payload["local_owner_count"], 2)

    def test_worker_active_requires_local_owner_and_exact_remote_proof(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        nodes = {
            item_port: node_observation(
                item_port,
                tuple(gpus),
                workers=frozenset({gpu}) if item_port == port else frozenset(),
            )
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        payload = self.run_audit(
            ownership=guard.LocalOwnership(
                frozenset({slot}), {slot: ("worker",)}
            ),
            nodes=nodes,
        )
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "worker_active")
        self.assertIn("remote_worker_verified:worker", event["detail"])
        self.assertEqual(payload["worker_active_count"], 1)
        self.assertEqual(payload["status"], "healthy")

    def test_exact_remote_worker_without_local_owner_is_orphan_blocked(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        nodes = {
            item_port: node_observation(
                item_port,
                tuple(gpus),
                workers=frozenset({gpu}) if item_port == port else frozenset(),
            )
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(nodes=nodes, starts=starts)
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "remote_busy_or_ambiguous")
        self.assertEqual(event["detail"], "orphan_remote_worker")
        self.assertNotIn((port, gpu), starts)
        self.assertEqual(payload["status"], "attention")

    def test_local_owner_with_incomplete_remote_worker_proof_stays_blocked(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        base_gpus = tuple(REQUIRED_REMOTE_GPU_LAYOUT[port])
        incomplete_cases = (
            gpu_observation(
                gpu, compute_kinds=("blender",), lock_state="busy", wrapper=False
            ),
            gpu_observation(
                gpu, compute_kinds=("blender",), lock_state="available", wrapper=True
            ),
            gpu_observation(
                gpu,
                compute_kinds=("blender", "blender"),
                lock_state="busy",
                wrapper=True,
            ),
            gpu_observation(
                gpu,
                compute_kinds=("blender",),
                lock_state="busy",
                wrapper=True,
                physical_binding_ok=False,
            ),
        )
        for incomplete in incomplete_cases:
            with self.subTest(observation=incomplete):
                node_gpus = tuple(
                    incomplete if item_gpu == gpu else gpu_observation(item_gpu)
                    for item_gpu in base_gpus
                )
                nodes = {
                    item_port: (
                        NodeObservation(
                            port=item_port,
                            probe_ok=True,
                            holder_audit_ok=True,
                            unexpected_holder_sessions=0,
                            legacy_holder_sessions=0,
                            gpus=node_gpus,
                        )
                        if item_port == port
                        else node_observation(item_port, tuple(gpus))
                    )
                    for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
                }
                payload = self.run_audit(
                    ownership=guard.LocalOwnership(
                        frozenset({slot}), {slot: ("worker",)}
                    ),
                    nodes=nodes,
                )
                event = next(
                    item for item in payload["events"]
                    if item["remote_port"] == port and item["gpu"] == gpu
                )
                self.assertEqual(event["action"], "local_owner")
                self.assertEqual(payload["worker_active_count"], 0)
                self.assertEqual(payload["status"], "attention")

    def test_changed_remote_boot_overrides_stale_local_owner(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(
            ownership=guard.LocalOwnership(
                frozenset({slot}), {slot: ("worker_controller",)}
            ),
            previous_boot_ids={port: boot_id(port, 1)},
            boot_ids={
                item_port: boot_id(item_port, 2 if item_port == port else 1)
                for item_port in REQUIRED_REMOTE_GPU_LAYOUT
            },
            starts=starts,
        )
        self.assertIn(slot, starts)
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "holder_started")
        self.assertIn("stale_local_owner_after_boot", event["detail"])
        node = next(
            item for item in payload["nodes"] if item["remote_port"] == port
        )
        self.assertTrue(node["boot_changed"])

    def test_remote_physical_idle_preserves_live_owner_during_startup(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        slot = (port, gpu)
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(
            ownership=guard.LocalOwnership(frozenset({slot}), {slot: ("worker",)}),
            previous_boot_ids={},
            starts=starts,
        )
        self.assertNotIn(slot, starts)
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "local_owner")
        self.assertEqual(event["detail"], "remote_idle_worker_starting:worker")

    def test_valid_existing_holder_is_left_untouched(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        nodes = {
            item_port: node_observation(
                item_port,
                tuple(gpus),
                holders=frozenset({gpu}) if item_port == port else frozenset(),
            )
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(nodes=nodes, starts=starts)
        self.assertNotIn((port, gpu), starts)
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "holder_ready")

    def test_remote_blender_or_busy_lock_never_gets_holder(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        nodes = {
            item_port: node_observation(
                item_port,
                tuple(gpus),
                busy=frozenset({gpu}) if item_port == port else frozenset(),
            )
            for item_port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(nodes=nodes, starts=starts)
        self.assertNotIn((port, gpu), starts)
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "remote_busy_or_ambiguous")

    def test_offline_node_stays_pending_while_other_nodes_are_protected(self) -> None:
        offline_port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[-1]
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(
            reachable=frozenset(set(REQUIRED_REMOTE_GPU_LAYOUT) - {offline_port}),
            starts=starts,
        )
        self.assertFalse(any(port == offline_port for port, _gpu in starts))
        offline_events = [
            item for item in payload["events"]
            if item["remote_port"] == offline_port
        ]
        self.assertTrue(offline_events)
        self.assertTrue(all(item["action"] == "node_unreachable" for item in offline_events))
        self.assertTrue(any(port != offline_port for port, _gpu in starts))

    def test_probe_failure_does_not_fall_open(self) -> None:
        failed_port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        nodes = {
            port: node_observation(port, tuple(gpus))
            for port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
            if port != failed_port
        }
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(
            nodes=nodes,
            reachable=frozenset(REQUIRED_REMOTE_GPU_LAYOUT),
            starts=starts,
        )
        self.assertFalse(any(port == failed_port for port, _gpu in starts))
        failed_events = [
            item for item in payload["events"]
            if item["remote_port"] == failed_port
        ]
        self.assertTrue(all(item["action"] == "probe_blocked" for item in failed_events))

    def test_boot_id_auth_failure_never_launches_holder(self) -> None:
        failed_port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        starts: list[tuple[int, int]] = []
        payload = self.run_audit(
            boot_errors=frozenset({failed_port}),
            starts=starts,
        )
        self.assertFalse(any(port == failed_port for port, _gpu in starts))
        failed_events = [
            item for item in payload["events"]
            if item["remote_port"] == failed_port
        ]
        self.assertTrue(all(item["action"] == "probe_blocked" for item in failed_events))
        self.assertTrue(all(
            item["detail"] == "boot_id_auth_or_remote_failed"
            for item in failed_events
        ))
        node = next(
            item for item in payload["nodes"]
            if item["remote_port"] == failed_port
        )
        self.assertIsNone(node["boot_id"])

    def test_per_slot_control_lock_prevents_holder_launch(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        gpu = REQUIRED_REMOTE_GPU_LAYOUT[port][0]
        starts: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with guard.acquire_slot_control_lock(port, gpu, root=root) as held:
                self.assertIsNotNone(held)
                payload = self.run_audit(
                    starts=starts,
                    slot_lock_root=root,
                )
        self.assertNotIn((port, gpu), starts)
        event = next(
            item for item in payload["events"]
            if item["remote_port"] == port and item["gpu"] == gpu
        )
        self.assertEqual(event["action"], "slot_control_busy")

    def test_state_boot_id_loader_ignores_invalid_records(self) -> None:
        port = sorted(REQUIRED_REMOTE_GPU_LAYOUT)[0]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({
                "nodes": [
                    {"remote_port": port, "boot_id": boot_id(port)},
                    {"remote_port": 99999, "boot_id": boot_id(port)},
                    {"remote_port": port + 1, "boot_id": "invalid"},
                ]
            }), encoding="utf-8")
            self.assertEqual(
                guard.load_previous_boot_ids(state), {port: boot_id(port)}
            )

    def test_extra_probe_slot_is_validated_without_changing_worker_topology(self) -> None:
        tertiary = max(REQUIRED_REMOTE_GPU_LAYOUT)
        extra_gpu = max(REQUIRED_REMOTE_GPU_LAYOUT[tertiary]) + 1
        self.assertEqual(
            guard.parse_extra_probe_slots([f"{tertiary}:{extra_gpu}"]),
            {tertiary: (extra_gpu,)},
        )
        with self.assertRaises(guard.HolderGuardError):
            guard.parse_extra_probe_slots([f"{tertiary}:0"])
        with self.assertRaises(guard.HolderGuardError):
            guard.parse_extra_probe_slots(["99999:0"])

    def test_exact_reserve_holder_is_audited_outside_canonical_events(self) -> None:
        tertiary = max(REQUIRED_REMOTE_GPU_LAYOUT)
        extra_gpu = max(REQUIRED_REMOTE_GPU_LAYOUT[tertiary]) + 1
        nodes = {
            port: node_observation(
                port,
                (
                    tuple(gpus) + (extra_gpu,)
                    if port == tertiary
                    else tuple(gpus)
                ),
                holders=(
                    frozenset({extra_gpu})
                    if port == tertiary
                    else frozenset()
                ),
            )
            for port, gpus in REQUIRED_REMOTE_GPU_LAYOUT.items()
        }
        payload = self.run_audit(
            nodes=nodes,
            extra_probe_gpus={tertiary: (extra_gpu,)},
        )
        reserve = payload["reserve_events"]
        self.assertEqual(len(reserve), 1)
        self.assertEqual(reserve[0]["gpu"], extra_gpu)
        self.assertEqual(reserve[0]["action"], "holder_ready")
        self.assertNotIn(
            extra_gpu,
            [
                event["gpu"]
                for event in payload["events"]
                if event["remote_port"] == tertiary
            ],
        )

    def test_boot_id_probe_is_key_only_and_rejects_auth_failure(self) -> None:
        commands: list[list[str]] = []

        def successful_runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(
                command, 0, boot_id(30773).upper() + "\n", ""
            )

        value = guard.probe_remote_boot_id(
            30773,
            host="root@render.example",
            identity_file=Path("/private/key"),
            known_hosts=Path("/fixed/known_hosts"),
            runner=successful_runner,
        )
        self.assertEqual(value, boot_id(30773))
        joined = " ".join(commands[0])
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("PasswordAuthentication=no", joined)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("cat /proc/sys/kernel/random/boot_id", commands[0])

        def failed_runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 255, "", "permission denied")

        with self.assertRaisesRegex(
            RemoteProbeError, "boot_id_auth_or_remote_failed"
        ):
            guard.probe_remote_boot_id(
                30773,
                host="root@render.example",
                identity_file=Path("/private/key"),
                known_hosts=Path("/fixed/known_hosts"),
                runner=failed_runner,
            )

    def test_atomic_state_and_single_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            guard.atomic_write_json(state, {"schema": guard.STATE_SCHEMA, "ok": True})
            self.assertTrue(json.loads(state.read_text(encoding="utf-8"))["ok"])

            lock = root / "guard.lock"
            held = lock.open("a+")
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            args = argparse.Namespace(
                identity_file=root / "key",
                known_hosts=root / "known_hosts",
                lock_path=lock,
                state_path=state,
                ssh_host="root@render.example",
                poll_seconds=15,
                tcp_timeout=1.0,
                ssh_timeout=20,
                log_heartbeat_seconds=600,
                once=True,
            )
            with mock.patch.object(guard, "validate_ssh_material"):
                self.assertEqual(guard.run_loop(args), 0)
            held.close()

    def test_shell_launcher_is_independent_of_mount_and_scheduler_lock(self) -> None:
        shell = (SCRIPTS / "run_total_asset_pipeline.sh").read_text(encoding="utf-8")
        start = shell.index("start_holder_guard() {")
        end = shell.index("\n}\n", start) + len("\n}\n")
        function = shell[start:end]
        self.assertIn("total_asset_holder_guard.py", function)
        self.assertIn("--poll-seconds", function)
        self.assertNotIn("require_mount\n", function)
        self.assertNotIn("total_asset_scheduler.lock", function)
        self.assertIn("holder-guard)\n    start_holder_guard", shell)


if __name__ == "__main__":
    unittest.main()
