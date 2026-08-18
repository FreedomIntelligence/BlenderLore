from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_remote_preflight as preflight
import total_asset_scheduler as scheduler
import total_asset_holder_transaction as holder_tx


PIPELINE_SHELL = PROJECT / "blender/scripts/run_total_asset_pipeline.sh"


def shell_function_source(name: str) -> str:
    shell = PIPELINE_SHELL.read_text(encoding="utf-8")
    start = shell.index(f"{name}() {{")
    end = shell.index("\n}\n", start) + len("\n}\n")
    return shell[start:end]


def layout_payload(
    target: str = "batch0001",
    workers: list[dict[str, int]] | None = None,
) -> str:
    return json.dumps({
        "schema_version": 1,
        "target_batch": target,
        "worker_count": 11,
        "workers": workers or [
            {"port": 30773, "gpu": 0, "worker_index": 4},
            {"port": 30773, "gpu": 1, "worker_index": 5},
            {"port": 30773, "gpu": 2, "worker_index": 6},
            {"port": 30773, "gpu": 3, "worker_index": 7},
            {"port": 30808, "gpu": 0, "worker_index": 8},
            {"port": 30808, "gpu": 1, "worker_index": 9},
            {"port": 30808, "gpu": 2, "worker_index": 10},
        ],
    })


def clean_node(port: int) -> preflight.NodeObservation:
    return preflight.NodeObservation(
        port=port,
        probe_ok=True,
        holder_audit_ok=True,
        unexpected_holder_sessions=0,
        legacy_holder_sessions=0,
        gpus=tuple(
            preflight.GpuObservation(
                gpu=gpu,
                gpu_query_ok=True,
                compute_query_ok=True,
                compute_process_count=0,
                compute_process_kinds=(),
                holder_session=False,
                lock_state="available",
                gpu_uuid=(
                    f"GPU-00000000-0000-0000-{port:04x}-{gpu:012x}"
                ),
                physical_binding_ok=True,
            )
            for gpu in scheduler.REQUIRED_REMOTE_GPU_LAYOUT[port]
        ),
    )


def replace_gpu(
    node: preflight.NodeObservation,
    gpu: int,
    **changes: object,
) -> preflight.NodeObservation:
    values = []
    for item in node.gpus:
        if item.gpu != gpu:
            values.append(item)
            continue
        payload = {
            "gpu": item.gpu,
            "gpu_query_ok": item.gpu_query_ok,
            "compute_query_ok": item.compute_query_ok,
            "compute_process_count": item.compute_process_count,
            "compute_process_kinds": item.compute_process_kinds,
            "holder_session": item.holder_session,
            "lock_state": item.lock_state,
            "wrapper_process_present": item.wrapper_process_present,
            "gpu_uuid": item.gpu_uuid,
            "physical_binding_ok": item.physical_binding_ok,
            "compute_process_gpu_uuids": item.compute_process_gpu_uuids,
            "holder_descendant_ok": item.holder_descendant_ok,
            "holder_lock_owner_ok": item.holder_lock_owner_ok,
            "holder_command_ok": item.holder_command_ok,
        }
        payload.update(changes)
        values.append(preflight.GpuObservation(**payload))
    return preflight.NodeObservation(
        port=node.port,
        probe_ok=node.probe_ok,
        holder_audit_ok=node.holder_audit_ok,
        unexpected_holder_sessions=node.unexpected_holder_sessions,
        legacy_holder_sessions=node.legacy_holder_sessions,
        gpus=tuple(values),
    )


def physical_remote_payload(
    gpus: tuple[int, ...] = (0, 1),
    *,
    active_gpu: int | None = None,
    binding_ok: bool = True,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for gpu in gpus:
        gpu_uuid = f"GPU-00000000-0000-0000-0000-{gpu:012x}"
        active = gpu == active_gpu
        rows.append({
            "gpu": gpu,
            "gpu_query_ok": True,
            "compute_query_ok": binding_ok or not active,
            "nvidia_compute_query_ok": True,
            "nvidia_pmon_query_ok": True,
            "process_tree_query_ok": True,
            "compute_process_count": 1 if active else 0,
            "compute_process_kinds": ["blender"] if active else [],
            "wrapper_process_present": False,
            "holder_session": False,
            "holder_descendant_ok": False,
            "holder_lock_owner_ok": False,
            "holder_command_ok": False,
            "lock_state": "busy" if active else "available",
            "gpu_uuid": gpu_uuid,
            "physical_binding_ok": binding_ok or not active,
            "compute_process_gpu_uuids": [gpu_uuid] if active else [],
        })
    return {
        "schema_version": preflight.REMOTE_PROBE_SCHEMA_VERSION,
        "probe_ok": binding_ok,
        "process_attribution_ok": binding_ok,
        "holder_audit_ok": True,
        "unexpected_holder_sessions": 0,
        "legacy_holder_sessions": 0,
        "gpus": rows,
    }


class LaunchLayoutTests(unittest.TestCase):
    def test_valid_partial_layout_uses_canonical_global_partitions(self) -> None:
        slots = preflight.parse_launch_layout(
            layout_payload(), target_batch="batch0001", worker_count=11
        )
        self.assertEqual([slot.worker_index for slot in slots], list(range(4, 11)))

    def test_failed_repair_holder_layout_must_use_formal_base_batch(self) -> None:
        payload = layout_payload(
            target="batch0001",
            workers=[{"port": 30422, "gpu": 0, "worker_index": 0}],
        )
        slots = preflight.parse_launch_layout(
            payload, target_batch="batch0001", worker_count=11
        )
        self.assertEqual(
            [(slot.port, slot.gpu, slot.worker_index) for slot in slots],
            [(30422, 0, 0)],
        )
        with self.assertRaisesRegex(
            preflight.PreflightInputError, "target_batch_invalid"
        ):
            preflight.parse_launch_layout(
                layout_payload(
                    target="batch0001_failed_repair_transport",
                    workers=[{"port": 30422, "gpu": 0, "worker_index": 0}],
                ),
                target_batch="batch0001_failed_repair_transport",
                worker_count=11,
            )

    def test_layout_rejects_wrong_partition_duplicate_and_non_11_topology(self) -> None:
        wrong = layout_payload(workers=[
            {"port": 30773, "gpu": 0, "worker_index": 5},
        ])
        with self.assertRaisesRegex(preflight.PreflightInputError, "partition_mismatch"):
            preflight.parse_launch_layout(
                wrong, target_batch="batch0001", worker_count=11
            )
        duplicate = json.loads(layout_payload())
        duplicate["workers"].append(dict(duplicate["workers"][0]))
        with self.assertRaises(preflight.PreflightInputError):
            preflight.parse_launch_layout(
                json.dumps(duplicate), target_batch="batch0001", worker_count=11
            )
        with self.assertRaisesRegex(preflight.PreflightInputError, "worker_count_not_11"):
            preflight.parse_launch_layout(
                layout_payload(), target_batch="batch0001", worker_count=8
            )

    def test_layout_bad_json_and_unknown_fields_fail_closed(self) -> None:
        with self.assertRaisesRegex(preflight.PreflightInputError, "json_invalid"):
            preflight.parse_launch_layout(
                "{broken", target_batch="batch0001", worker_count=11
            )
        payload = json.loads(layout_payload())
        payload["typo"] = True
        with self.assertRaisesRegex(preflight.PreflightInputError, "fields_invalid"):
            preflight.parse_launch_layout(
                json.dumps(payload), target_batch="batch0001", worker_count=11
            )


class ReportEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layout = preflight.parse_launch_layout(
            layout_payload(), target_batch="batch0001", worker_count=11
        )
        self.nodes = {
            port: clean_node(port) for port in scheduler.REQUIRED_REMOTE_GPU_LAYOUT
        }

    def report(
        self,
        *,
        claims: tuple[scheduler.WorkerClaim, ...] = (),
        nodes: dict[int, preflight.NodeObservation] | None = None,
        failures: dict[int, str] | None = None,
    ) -> dict[str, object]:
        controllers = tuple(
            scheduler.ScreenController(
                session=(
                    f"total_asset_{claim.batch}_g{claim.gpu}"
                    if claim.remote_port == 30773
                    else f"total_asset_{claim.batch}_{claim.remote_port}_g{claim.gpu}"
                ),
                batch=claim.batch,
                kind="worker",
                remote_port=claim.remote_port,
                gpu=claim.gpu,
            )
            for claim in claims
            if claim.is_formal_primary
        )
        return preflight.build_report(
            target_batch="batch0001",
            worker_count=11,
            launch_layout=self.layout,
            claims=claims,
            controllers=controllers,
            observations=self.nodes if nodes is None else nodes,
            failures=failures,
            observed_at_epoch=12345.0,
        )

    @staticmethod
    def gpu(report: dict[str, object], port: int, gpu: int) -> dict[str, object]:
        for node in report["nodes"]:  # type: ignore[union-attr]
            if node["port"] == port:
                return next(item for item in node["gpus"] if item["gpu"] == gpu)
        raise AssertionError("missing GPU")

    def test_clean_three_node_report_matches_scheduler_schema(self) -> None:
        report = self.report()
        self.assertTrue(report["ready"])
        self.assertEqual(
            scheduler.validate_remote_preflight_report(
                report,
                target_batch="batch0001",
                desired_worker_count=11,
                now_epoch=12345.0,
            ),
            (),
        )

    def test_scoped_report_audits_full_selected_node_without_30773(self) -> None:
        layout = preflight.parse_launch_layout(
            layout_payload(workers=[
                {"port": 30808, "gpu": 0, "worker_index": 8},
                {"port": 30808, "gpu": 1, "worker_index": 9},
                {"port": 30808, "gpu": 2, "worker_index": 10},
            ]),
            target_batch="batch0001",
            worker_count=11,
        )
        calls: list[tuple[int, tuple[int, ...]]] = []

        def probe(port: int, gpus: tuple[int, ...]) -> preflight.NodeObservation:
            calls.append((port, tuple(gpus)))
            return clean_node(port)

        report = preflight.collect_remote_preflight(
            target_batch="batch0001",
            worker_count=11,
            launch_layout=layout,
            claims=(),
            controllers=(),
            host="root@10.26.6.88",
            known_hosts=Path("/unused"),
            identity_file=Path("/unused"),
            probe_runner=probe,
            required_remote_gpu_layout={30808: (0, 1, 2)},
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["scope"], "launch_ports")
        self.assertEqual(calls, [(30808, (0, 1, 2))])
        self.assertEqual([node["port"] for node in report["nodes"]], [30808])

    def test_scoped_report_still_rejects_other_gpu_activity_on_selected_node(self) -> None:
        layout = preflight.parse_launch_layout(
            layout_payload(workers=[
                {"port": 30422, "gpu": 3, "worker_index": 3},
            ]),
            target_batch="batch0001",
            worker_count=11,
        )
        node = replace_gpu(
            clean_node(30422),
            0,
            compute_process_count=1,
            compute_process_kinds=("blender",),
            lock_state="busy",
        )
        report = preflight.build_report(
            target_batch="batch0001",
            worker_count=11,
            launch_layout=layout,
            claims=(),
            observations={30422: node},
            required_remote_gpu_layout={30422: (0, 1, 2, 3)},
        )
        self.assertFalse(report["ready"])
        self.assertFalse(self.gpu(report, 30422, 0)["gpu_ok"])

    def test_scoped_api_rejects_partial_gpu_or_unknown_node_layout(self) -> None:
        for invalid in ({30808: (0, 1)}, {12345: (0,)}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                preflight.PreflightInputError,
                "audited_remote_layout_invalid",
            ):
                preflight.validate_audited_layout(invalid)

    def test_old_worker_outside_launch_layout_may_own_compute_and_lock(self) -> None:
        nodes = dict(self.nodes)
        nodes[30422] = replace_gpu(
            nodes[30422],
            2,
            compute_process_count=1,
            compute_process_kinds=("blender",),
            lock_state="busy",
        )
        claim = scheduler.WorkerClaim(
            "batch0000_quality_repair", 30422, 2, 2, 4
        )
        report = self.report(claims=(claim,), nodes=nodes)
        gpu = self.gpu(report, 30422, 2)
        self.assertEqual(gpu["claim_state"], "allowed_existing")
        self.assertTrue(gpu["gpu_ok"])
        self.assertTrue(gpu["lock_ok"])
        self.assertTrue(report["ready"])

    def test_four_legacy_batch0_repairs_without_partition_flags_are_scoped(self) -> None:
        process_rows = "\n".join(
            f"{600 + gpu} 1 Python python3 run_total_asset_render_worker.py "
            f"--batch batch0000_repair_v5_g{gpu} --remote-port 30422 --gpu {gpu}"
            for gpu in range(4)
        )
        claims = scheduler.parse_worker_claims(process_rows)
        self.assertEqual(len(claims), 4)
        self.assertEqual(
            {(claim.worker_index, claim.worker_count) for claim in claims},
            {(0, 1)},
        )
        nodes = dict(self.nodes)
        for gpu in range(4):
            nodes[30422] = replace_gpu(
                nodes[30422],
                gpu,
                compute_process_count=1,
                compute_process_kinds=("blender",),
                lock_state="busy",
            )
        outside_layout = self.report(claims=claims, nodes=nodes)
        self.assertTrue(outside_layout["ready"])
        self.assertTrue(all(
            self.gpu(outside_layout, 30422, gpu)["claim_state"]
            == "allowed_existing"
            for gpu in range(4)
        ))

        full_workers = [
            {
                "port": port,
                "gpu": gpu,
                "worker_index": worker_index,
            }
            for (port, gpu), worker_index in sorted(
                scheduler.CANONICAL_WORKER_INDEX_BY_LOCATION.items(),
                key=lambda item: item[1],
            )
        ]
        full_layout = preflight.parse_launch_layout(
            layout_payload(workers=full_workers),
            target_batch="batch0001",
            worker_count=11,
        )
        inside_layout = preflight.build_report(
            target_batch="batch0001",
            worker_count=11,
            launch_layout=full_layout,
            claims=claims,
            observations=nodes,
        )
        self.assertFalse(inside_layout["ready"])
        self.assertTrue(all(
            not self.gpu(inside_layout, 30422, gpu)["gpu_ok"]
            for gpu in range(4)
        ))

    def test_target_11_claim_inside_launch_layout_is_compatible(self) -> None:
        nodes = dict(self.nodes)
        nodes[30773] = replace_gpu(
            nodes[30773],
            1,
            compute_process_count=1,
            compute_process_kinds=("blender",),
            lock_state="busy",
        )
        claim = scheduler.WorkerClaim("batch0001", 30773, 1, 5, 11)
        report = self.report(claims=(claim,), nodes=nodes)
        gpu = self.gpu(report, 30773, 1)
        self.assertEqual(gpu["claim_state"], "compatible_target")
        self.assertTrue(gpu["gpu_ok"])
        self.assertTrue(gpu["lock_ok"])

    def test_canonical_target_claim_outside_launch_layout_is_allowed_existing(self) -> None:
        nodes = dict(self.nodes)
        nodes[30422] = replace_gpu(
            nodes[30422],
            3,
            compute_process_count=1,
            compute_process_kinds=("blender",),
            lock_state="busy",
            wrapper_process_present=True,
        )
        claim = scheduler.WorkerClaim("batch0001", 30422, 3, 3, 11)
        report = self.report(claims=(claim,), nodes=nodes)
        gpu = self.gpu(report, 30422, 3)
        self.assertEqual(gpu["claim_state"], "allowed_existing")
        self.assertTrue(gpu["gpu_ok"])
        self.assertTrue(gpu["lock_ok"])
        self.assertTrue(report["ready"])

    def test_noncanonical_target_claim_outside_launch_layout_is_rejected(self) -> None:
        nodes = dict(self.nodes)
        nodes[30422] = replace_gpu(
            nodes[30422],
            3,
            compute_process_count=1,
            compute_process_kinds=("blender",),
            lock_state="busy",
            wrapper_process_present=True,
        )
        claim = scheduler.WorkerClaim("batch0001", 30422, 3, 2, 11)
        report = self.report(claims=(claim,), nodes=nodes)
        gpu = self.gpu(report, 30422, 3)
        self.assertEqual(gpu["claim_state"], "incompatible")
        self.assertFalse(gpu["gpu_ok"])
        self.assertFalse(gpu["lock_ok"])
        self.assertFalse(report["ready"])

    def test_worker_compute_without_shared_lock_is_rejected(self) -> None:
        nodes = dict(self.nodes)
        nodes[30422] = replace_gpu(
            nodes[30422],
            1,
            compute_process_count=1,
            compute_process_kinds=("blender",),
            lock_state="available",
        )
        claim = scheduler.WorkerClaim(
            "batch0000_quality_repair", 30422, 1, 1, 4
        )
        report = self.report(claims=(claim,), nodes=nodes)
        gpu = self.gpu(report, 30422, 1)
        self.assertTrue(gpu["gpu_ok"])
        self.assertFalse(gpu["lock_ok"])
        self.assertIn("compute_without_shared_lock", gpu["reason_codes"])
        self.assertFalse(report["ready"])

    def test_incompatible_claim_on_launch_gpu_fails_even_when_gpu_is_idle(self) -> None:
        claim = scheduler.WorkerClaim("batch0001", 30773, 0, 0, 8)
        report = self.report(claims=(claim,))
        gpu = self.gpu(report, 30773, 0)
        self.assertFalse(gpu["gpu_ok"])
        self.assertIn("launch_claim_incompatible", gpu["reason_codes"])
        self.assertFalse(report["ready"])

    def test_unattributed_compute_holder_and_busy_lock_all_fail(self) -> None:
        nodes = dict(self.nodes)
        nodes[30808] = replace_gpu(
            nodes[30808],
            0,
            compute_process_count=1,
            compute_process_kinds=("other",),
            holder_session=True,
            holder_descendant_ok=True,
            holder_lock_owner_ok=True,
            holder_command_ok=True,
            lock_state="busy",
        )
        report = self.report(nodes=nodes)
        gpu = self.gpu(report, 30808, 0)
        self.assertFalse(gpu["gpu_ok"])
        self.assertFalse(gpu["holder_ok"])
        self.assertFalse(gpu["lock_ok"])
        self.assertIn("compute_unattributed", gpu["reason_codes"])
        self.assertIn("holder_present", gpu["reason_codes"])
        self.assertIn(
            "lock_busy_without_verified_blender_owner", gpu["reason_codes"]
        )

    def test_non_launch_secondary_holder_is_parked_but_launch_slot_requires_release(self) -> None:
        nodes = dict(self.nodes)
        nodes[30422] = replace_gpu(
            nodes[30422],
            1,
            compute_process_count=1,
            compute_process_kinds=("holder",),
            holder_session=True,
            holder_descendant_ok=True,
            holder_lock_owner_ok=True,
            holder_command_ok=True,
            lock_state="busy",
            wrapper_process_present=False,
        )
        parked = self.report(nodes=nodes)
        gpu = self.gpu(parked, 30422, 1)
        self.assertTrue(parked["ready"])
        self.assertTrue(gpu["parked_holder"])
        self.assertTrue(gpu["gpu_ok"])
        self.assertTrue(gpu["holder_ok"])
        self.assertTrue(gpu["lock_ok"])

        full_workers = [
            {"port": port, "gpu": gpu_id, "worker_index": worker_index}
            for (port, gpu_id), worker_index in sorted(
                scheduler.CANONICAL_WORKER_INDEX_BY_LOCATION.items(),
                key=lambda item: item[1],
            )
        ]
        full_layout = preflight.parse_launch_layout(
            layout_payload(workers=full_workers),
            target_batch="batch0001",
            worker_count=11,
        )
        included = preflight.build_report(
            target_batch="batch0001",
            worker_count=11,
            launch_layout=full_layout,
            claims=(),
            observations=nodes,
        )
        included_gpu = self.gpu(included, 30422, 1)
        self.assertFalse(included["ready"])
        self.assertFalse(included_gpu["parked_holder"])
        self.assertFalse(included_gpu["holder_ok"])
        self.assertFalse(included_gpu["lock_ok"])

    def test_exact_slot_pre_release_accepts_only_the_verified_holder(self) -> None:
        layout = preflight.parse_launch_layout(
            layout_payload(target="batch0002", workers=[
                {"port": 30773, "gpu": 1, "worker_index": 5},
            ]),
            target_batch="batch0002",
            worker_count=11,
        )
        holder_node = replace_gpu(
            clean_node(30773),
            1,
            compute_process_count=1,
            compute_process_kinds=("holder",),
            holder_session=True,
            holder_descendant_ok=True,
            holder_lock_owner_ok=True,
            holder_command_ok=True,
            lock_state="busy",
        )
        report = preflight.build_report(
            target_batch="batch0002",
            worker_count=11,
            launch_layout=layout,
            claims=(),
            observations={30773: holder_node},
            required_remote_gpu_layout={30773: (1,)},
            allow_gpu_subset=True,
            allow_launch_holders=True,
            scope="launch_slots",
        )
        gpu = self.gpu(report, 30773, 1)
        self.assertTrue(report["ready"])
        self.assertEqual(report["scope"], "launch_slots")
        self.assertTrue(gpu["parked_holder"])

        unrelated = replace_gpu(
            holder_node,
            1,
            holder_descendant_ok=False,
        )
        unrelated_report = preflight.build_report(
            target_batch="batch0002",
            worker_count=11,
            launch_layout=layout,
            claims=(),
            observations={30773: unrelated},
            required_remote_gpu_layout={30773: (1,)},
            allow_gpu_subset=True,
            allow_launch_holders=True,
            scope="launch_slots",
        )
        self.assertFalse(unrelated_report["ready"])
        unrelated_gpu = self.gpu(unrelated_report, 30773, 1)
        self.assertFalse(unrelated_gpu["parked_holder"])
        self.assertIn("holder_descendant_invalid", unrelated_gpu["reason_codes"])

        for field, reason in (
            ("holder_lock_owner_ok", "holder_lock_owner_invalid"),
            ("holder_command_ok", "holder_command_invalid"),
        ):
            with self.subTest(holder_evidence_field=field):
                invalid_node = replace_gpu(holder_node, 1, **{field: False})
                invalid_report = preflight.build_report(
                    target_batch="batch0002",
                    worker_count=11,
                    launch_layout=layout,
                    claims=(),
                    observations={30773: invalid_node},
                    required_remote_gpu_layout={30773: (1,)},
                    allow_gpu_subset=True,
                    allow_launch_holders=True,
                    scope="launch_slots",
                )
                invalid_gpu = self.gpu(invalid_report, 30773, 1)
                self.assertFalse(invalid_report["ready"])
                self.assertFalse(invalid_gpu["parked_holder"])
                self.assertIn(reason, invalid_gpu["reason_codes"])

        extra_compute = replace_gpu(
            holder_node,
            1,
            compute_process_count=2,
            compute_process_kinds=("holder", "blender"),
        )
        blocked = preflight.build_report(
            target_batch="batch0002",
            worker_count=11,
            launch_layout=layout,
            claims=(),
            observations={30773: extra_compute},
            required_remote_gpu_layout={30773: (1,)},
            allow_gpu_subset=True,
            allow_launch_holders=True,
            scope="launch_slots",
        )
        self.assertFalse(blocked["ready"])
        self.assertIn(
            "compute_process_count_unexpected",
            self.gpu(blocked, 30773, 1)["reason_codes"],
        )

    def test_exact_slot_report_probes_the_full_node_for_process_attribution(self) -> None:
        layout = preflight.parse_launch_layout(
            layout_payload(target="batch0002", workers=[
                {"port": 30808, "gpu": 2, "worker_index": 10},
            ]),
            target_batch="batch0002",
            worker_count=11,
        )
        calls: list[tuple[int, tuple[int, ...]]] = []

        def probe(port: int, gpus: tuple[int, ...]) -> preflight.NodeObservation:
            calls.append((port, tuple(gpus)))
            return clean_node(port)

        report = preflight.collect_remote_preflight(
            target_batch="batch0002",
            worker_count=11,
            launch_layout=layout,
            claims=(),
            controllers=(),
            host="root@10.26.6.88",
            known_hosts=Path("/unused"),
            identity_file=Path("/unused"),
            probe_runner=probe,
            required_remote_gpu_layout={30808: (2,)},
            probe_remote_gpu_layout={30808: (0, 1, 2)},
            allow_gpu_subset=True,
            allow_launch_holders=True,
            scope="launch_slots",
        )
        self.assertTrue(report["ready"])
        self.assertEqual(calls, [(30808, (0, 1, 2))])
        self.assertEqual(
            [gpu["gpu"] for gpu in report["nodes"][0]["gpus"]],
            [2],
        )

    def test_remote_blender_wrapper_without_local_claim_is_blocked(self) -> None:
        nodes = dict(self.nodes)
        nodes[30808] = replace_gpu(
            nodes[30808], 1, wrapper_process_present=True
        )
        report = self.report(nodes=nodes)
        gpu = self.gpu(report, 30808, 1)
        self.assertFalse(gpu["gpu_ok"])
        self.assertIn(
            "remote_blender_wrapper_unattributed", gpu["reason_codes"]
        )

    def test_busy_lock_without_verified_blender_is_never_explained_by_claim_alone(self) -> None:
        nodes = dict(self.nodes)
        nodes[30422] = replace_gpu(
            nodes[30422], 2, lock_state="busy"
        )
        claim = scheduler.WorkerClaim(
            "batch0000_quality_repair", 30422, 2, 2, 4
        )
        report = self.report(claims=(claim,), nodes=nodes)
        gpu = self.gpu(report, 30422, 2)
        self.assertFalse(gpu["lock_ok"])
        self.assertIn(
            "lock_busy_without_verified_blender_owner", gpu["reason_codes"]
        )
        self.assertFalse(report["ready"])

    def test_duplicate_local_controller_claim_is_not_accepted(self) -> None:
        claim = scheduler.WorkerClaim("batch0001", 30773, 2, 6, 11)
        report = self.report(claims=(claim, claim))
        gpu = self.gpu(report, 30773, 2)
        self.assertFalse(gpu["gpu_ok"])
        self.assertEqual(gpu["claim_state"], "duplicate")
        self.assertIn("local_claim_duplicate", gpu["reason_codes"])

    def test_invalid_existing_claim_cannot_explain_remote_occupancy(self) -> None:
        nodes = dict(self.nodes)
        nodes[30422] = replace_gpu(
            nodes[30422],
            3,
            compute_process_count=1,
            compute_process_kinds=("blender",),
            lock_state="busy",
        )
        claim = scheduler.WorkerClaim("not-a-batch", 30422, 3, 4, 4)
        report = self.report(claims=(claim,), nodes=nodes)
        gpu = self.gpu(report, 30422, 3)
        self.assertFalse(gpu["gpu_ok"])
        self.assertFalse(gpu["lock_ok"])
        self.assertIn("local_claim_invalid", gpu["reason_codes"])

    def test_unknown_or_legacy_holder_session_fails_node_wide(self) -> None:
        nodes = dict(self.nodes)
        original = nodes[30422]
        nodes[30422] = preflight.NodeObservation(
            port=30422,
            probe_ok=False,
            holder_audit_ok=False,
            unexpected_holder_sessions=1,
            legacy_holder_sessions=1,
            gpus=original.gpus,
        )
        report = self.report(nodes=nodes)
        for gpu_index in range(4):
            gpu = self.gpu(report, 30422, gpu_index)
            self.assertFalse(gpu["gpu_ok"])
            self.assertFalse(gpu["holder_ok"])
            self.assertIn("holder_audit_failed", gpu["reason_codes"])

    def test_remote_failure_is_redacted_and_fails_entire_node(self) -> None:
        nodes = dict(self.nodes)
        del nodes[30773]
        report = self.report(
            nodes=nodes,
            failures={30773: "ssh_exit_nonzero"},
        )
        node = next(item for item in report["nodes"] if item["port"] == 30773)  # type: ignore[union-attr]
        self.assertFalse(node["reachable"])
        self.assertEqual(node["error_code"], "ssh_exit_nonzero")
        serialized = json.dumps(report)
        self.assertNotIn("root@", serialized)
        self.assertNotIn("password", serialized.lower())
        self.assertNotIn("private", serialized.lower())


class RemotePhysicalIdentityTests(unittest.TestCase):
    @staticmethod
    def execute_probe_with_visible_device(
        value: str,
        *,
        graphics_only: bool = False,
        nvml_visible: bool = True,
        ps_visible: bool = True,
        exact_locked_wrapper: bool = False,
        wrapper_lock_held: bool = True,
        extra_process_rows: str = "",
        probe_pid: int = 999,
        probe_parent_by_pid: dict[int, int] | None = None,
    ) -> dict[str, object]:
        uuids = {
            0: "GPU-00000000-0000-0000-0000-000000000000",
            1: "GPU-00000000-0000-0000-0000-000000000001",
        }

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["tmux", "list-sessions"]:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="no server running"
                )
            if command[:3] == ["ps", "-eo", "pid=,comm=,args="]:
                wrapper_row = (
                    "222 bash CUDA_VISIBLE_DEVICES=0 /bin/bash -lc "
                    "'exec 9>/tmp/total_asset_gpu_0.lock; "
                    "/usr/bin/blender -b scene.blend'\n"
                    if exact_locked_wrapper
                    else ""
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        (
                            "123 blender /usr/bin/blender -b scene.blend\n"
                            if ps_visible
                            else ""
                        )
                        + wrapper_row
                        + extra_process_rows
                    ),
                    stderr="",
                )
            if command[0] == "nvidia-smi" and "--query-gpu=index,uuid" in command:
                gpu = int(command[2])
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"{gpu}, {uuids[gpu]}\n", stderr=""
                )
            if command[0] == "nvidia-smi" and any(
                item.startswith("--query-compute-apps=") for item in command
            ):
                gpu = int(command[2])
                output = (
                    f"123, blender, {uuids[0]}\n"
                    if gpu == 0 and not graphics_only and nvml_visible
                    else ""
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=output, stderr=""
                )
            if command[:2] == ["nvidia-smi", "pmon"]:
                gpu = int(command[3])
                process_type = "G" if graphics_only else "C"
                output = (
                    f"0 123 {process_type} 0 0 0 0 blender\n"
                    if gpu == 0 and nvml_visible else ""
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=output, stderr=""
                )
            raise AssertionError(command)

        def fake_open(
            path: str, _mode: str = "r", **_kwargs: object
        ) -> io.BytesIO | io.StringIO:
            if path == "/proc/123/environ":
                return io.BytesIO(
                    f"CUDA_VISIBLE_DEVICES={value}\0".encode("ascii")
                )
            if path == "/proc/222/environ" and exact_locked_wrapper:
                return io.BytesIO(b"CUDA_VISIBLE_DEVICES=0\0")
            if path == "/proc/222/fdinfo/9" and exact_locked_wrapper:
                return io.StringIO(
                    "pos:\t0\n"
                    "flags:\t0100001\n"
                    + (
                        "lock:\t1: FLOCK ADVISORY WRITE 222 00:01:700 0 EOF\n"
                        if wrapper_lock_held
                        else ""
                    )
                )
            status = re.fullmatch(r"/proc/(\d+)/status", path)
            if status is not None:
                pid = int(status.group(1))
                parent_map = dict(probe_parent_by_pid or {})
                if exact_locked_wrapper:
                    parent_map.setdefault(123, 222)
                    parent_map.setdefault(222, 1)
                parent = parent_map.get(pid)
                if parent is not None:
                    return io.StringIO(f"Name:\tpython\nPPid:\t{parent}\n")
            raise FileNotFoundError(path)

        def fake_stat(path: str, **_kwargs: object) -> mock.Mock:
            if path == "/tmp/total_asset_gpu_0.lock" and exact_locked_wrapper:
                return mock.Mock(st_mode=0o100600, st_dev=9, st_ino=700)
            if path == "/proc/222/fd/9" and exact_locked_wrapper:
                return mock.Mock(st_mode=0o100600, st_dev=9, st_ino=700)
            raise FileNotFoundError(path)

        def fake_flock(_fd: int, _operation: int) -> None:
            if exact_locked_wrapper and wrapper_lock_held:
                raise BlockingIOError(
                    errno.EAGAIN, "resource temporarily unavailable"
                )

        output = io.StringIO()
        namespace: dict[str, object] = {"EXPECTED_GPUS": [0, 1]}
        with (
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch("shutil.which", return_value="/usr/bin/tmux"),
            mock.patch("builtins.open", side_effect=fake_open),
            mock.patch("os.getpid", return_value=probe_pid),
            mock.patch(
                "os.path.exists",
                side_effect=lambda path: bool(
                    exact_locked_wrapper
                    and path == "/tmp/total_asset_gpu_0.lock"
                ),
            ),
            mock.patch("os.stat", side_effect=fake_stat),
            mock.patch("os.open", return_value=88),
            mock.patch("os.close"),
            mock.patch("fcntl.flock", side_effect=fake_flock),
            contextlib.redirect_stdout(output),
        ):
            exec(preflight.REMOTE_PROBE_PROGRAM, namespace)
        return json.loads(output.getvalue())

    @staticmethod
    def execute_holder_probe(
        *,
        pane_rows: str = "100:0\n",
        parent_pid: str = "100",
        holder_script: str = "/tmp/video2blender_gpu_hold.py",
        holder_lock_inode: int = 700,
        holder_lock_record: bool = True,
        session_rows: str = "total_asset_gpu_holder_g0\n",
        audited_node_gpus: list[int] | None = None,
    ) -> dict[str, object]:
        gpu_uuid = "GPU-00000000-0000-0000-0000-000000000000"

        def fake_run(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["tmux", "list-sessions"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=session_rows,
                    stderr="",
                )
            if command[:3] == ["tmux", "list-panes", "-s"]:
                if command != [
                        "tmux",
                        "list-panes",
                        "-s",
                        "-t",
                        "=total_asset_gpu_holder_g0",
                        "-F",
                        "#{pane_pid}:#{pane_dead}",
                    ]:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=pane_rows, stderr=""
                )
            if command[:3] == ["ps", "-eo", "pid=,comm=,args="]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=f"123 python /usr/bin/python3 {holder_script}\n",
                    stderr="",
                )
            if command[0] == "nvidia-smi" and "--query-gpu=index,uuid" in command:
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"0, {gpu_uuid}\n", stderr=""
                )
            if command[0] == "nvidia-smi" and any(
                item.startswith("--query-compute-apps=") for item in command
            ):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=f"123, /usr/bin/python3, {gpu_uuid}\n",
                    stderr="",
                )
            if command[:2] == ["nvidia-smi", "pmon"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="0 123 C 0 0 0 0 python3\n",
                    stderr="",
                )
            raise AssertionError(command)

        def fake_open(
            path: str, mode: str = "r", **_kwargs: object
        ) -> io.BytesIO | io.StringIO:
            if path == "/proc/123/status":
                return io.StringIO(f"Name:\tpython\nPPid:\t{parent_pid}\n")
            if path == "/proc/123/cmdline":
                return io.BytesIO(
                    f"/usr/bin/python3\0{holder_script}\0".encode("utf-8")
                )
            if path == "/proc/123/environ":
                return io.BytesIO(
                    (
                        "CUDA_DEVICE_ORDER=PCI_BUS_ID\0"
                        f"CUDA_VISIBLE_DEVICES={gpu_uuid}\0"
                        "HOLD_GPU_IDS=0\0"
                    ).encode("ascii")
                )
            if path == "/proc/123/fdinfo/9":
                return io.StringIO(
                    "pos:\t0\n"
                    "flags:\t0100001\n"
                    + (
                        "lock:\t1: FLOCK ADVISORY WRITE 123 00:01:700 0 EOF\n"
                        if holder_lock_record
                        else ""
                    )
                )
            raise FileNotFoundError(path)

        def fake_stat(path: str, **_kwargs: object) -> mock.Mock:
            if path == "/tmp/total_asset_gpu_0.lock":
                return mock.Mock(st_mode=0o100600, st_dev=9, st_ino=700)
            if path == "/proc/123/fd/9":
                return mock.Mock(
                    st_mode=0o100600,
                    st_dev=9,
                    st_ino=holder_lock_inode,
                )
            raise FileNotFoundError(path)

        output = io.StringIO()
        namespace: dict[str, object] = {"EXPECTED_GPUS": [0]}
        if audited_node_gpus is not None:
            namespace["AUDITED_NODE_GPUS"] = audited_node_gpus
        with (
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch("shutil.which", return_value="/usr/bin/tmux"),
            mock.patch("builtins.open", side_effect=fake_open),
            mock.patch("os.path.exists", return_value=True),
            mock.patch("os.stat", side_effect=fake_stat),
            mock.patch("os.open", return_value=88),
            mock.patch("os.close"),
            mock.patch(
                "fcntl.flock",
                side_effect=BlockingIOError(
                    errno.EAGAIN, "resource temporarily unavailable"
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            exec(preflight.REMOTE_PROBE_PROGRAM, namespace)
        return json.loads(output.getvalue())

    def test_normalizes_authoritative_index_uuid_and_process_uuid(self) -> None:
        payload = physical_remote_payload(active_gpu=1)
        node = preflight._normalize_remote_payload(30773, (0, 1), payload)
        self.assertTrue(node.probe_ok)
        observed = {item.gpu: item for item in node.gpus}[1]
        self.assertEqual(
            observed.gpu_uuid,
            "GPU-00000000-0000-0000-0000-000000000001",
        )
        self.assertEqual(
            observed.compute_process_gpu_uuids,
            ("GPU-00000000-0000-0000-0000-000000000001",),
        )
        self.assertTrue(observed.physical_binding_ok)

    def test_probe_binds_holder_to_live_pane_command_environment_and_lock(self) -> None:
        payload = self.execute_holder_probe()
        self.assertTrue(payload["probe_ok"])
        gpu = payload["gpus"][0]
        self.assertEqual(gpu["compute_process_kinds"], ["holder"])
        self.assertTrue(gpu["holder_descendant_ok"])
        self.assertTrue(gpu["holder_command_ok"])
        self.assertTrue(gpu["holder_lock_owner_ok"])
        self.assertEqual(gpu["lock_state"], "busy")

    def test_scoped_probe_allows_holder_on_another_valid_node_gpu(self) -> None:
        payload = self.execute_holder_probe(
            session_rows=(
                "total_asset_gpu_holder_g0\n"
                "total_asset_gpu_holder_g1\n"
            ),
            audited_node_gpus=[0, 1],
        )
        self.assertTrue(payload["probe_ok"])
        self.assertEqual(payload["unexpected_holder_sessions"], 0)

        fail_closed = self.execute_holder_probe(
            session_rows=(
                "total_asset_gpu_holder_g0\n"
                "total_asset_gpu_holder_g1\n"
            ),
            audited_node_gpus=[0],
        )
        self.assertFalse(fail_closed["probe_ok"])
        self.assertEqual(fail_closed["unexpected_holder_sessions"], 1)

    def test_probe_rejects_wrong_holder_command_or_lock_inode(self) -> None:
        wrong_command = self.execute_holder_probe(holder_script="/tmp/not-holder.py")
        self.assertFalse(wrong_command["probe_ok"])
        self.assertFalse(wrong_command["gpus"][0]["holder_command_ok"])

        wrong_lock = self.execute_holder_probe(holder_lock_inode=701)
        self.assertFalse(wrong_lock["probe_ok"])
        self.assertFalse(wrong_lock["gpus"][0]["holder_lock_owner_ok"])

        unlocked_fd = self.execute_holder_probe(holder_lock_record=False)
        self.assertFalse(unlocked_fd["probe_ok"])
        self.assertFalse(unlocked_fd["gpus"][0]["holder_lock_owner_ok"])

    def test_probe_rejects_unrelated_dead_or_multiple_tmux_panes(self) -> None:
        cases = (
            {"parent_pid": "99"},
            {"pane_rows": "100:1\n"},
            {"pane_rows": "100:0\n101:0\n"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                payload = self.execute_holder_probe(**changes)
                self.assertFalse(payload["probe_ok"])
                self.assertFalse(payload["gpus"][0]["holder_descendant_ok"])

    def test_legacy_schema_is_accepted_structurally_but_never_ready(self) -> None:
        payload = physical_remote_payload()
        for row in payload["gpus"]:  # type: ignore[index,union-attr]
            row.pop("gpu_uuid")
            row.pop("physical_binding_ok")
            row.pop("compute_process_gpu_uuids")
        node = preflight._normalize_remote_payload(30773, (0, 1), payload)
        self.assertFalse(node.probe_ok)
        self.assertTrue(all(not gpu.compute_query_ok for gpu in node.gpus))
        self.assertTrue(all(not gpu.physical_binding_ok for gpu in node.gpus))

    def test_legacy_holder_evidence_is_accepted_but_never_ready(self) -> None:
        payload = physical_remote_payload()
        for row in payload["gpus"]:  # type: ignore[index,union-attr]
            row.pop("holder_descendant_ok")
            row.pop("holder_lock_owner_ok")
            row.pop("holder_command_ok")
        node = preflight._normalize_remote_payload(30773, (0, 1), payload)
        self.assertFalse(node.probe_ok)
        self.assertTrue(all(not gpu.holder_descendant_ok for gpu in node.gpus))
        self.assertTrue(all(not gpu.holder_lock_owner_ok for gpu in node.gpus))
        self.assertTrue(all(not gpu.holder_command_ok for gpu in node.gpus))

    def test_complete_holder_evidence_is_required_for_probe_ready(self) -> None:
        payload = physical_remote_payload(active_gpu=0)
        holder = payload["gpus"][0]  # type: ignore[index]
        holder.update({
            "compute_process_kinds": ["holder"],
            "holder_session": True,
            "holder_descendant_ok": True,
            "holder_lock_owner_ok": True,
            "holder_command_ok": True,
        })
        node = preflight._normalize_remote_payload(30773, (0, 1), payload)
        self.assertTrue(node.probe_ok)

        old_unclassified = json.loads(json.dumps(payload))
        old_unclassified["gpus"][0]["compute_process_kinds"] = ["other"]
        with self.assertRaisesRegex(
            preflight.RemoteProbeError,
            "remote_holder_evidence_inconsistent",
        ):
            preflight._normalize_remote_payload(
                30773, (0, 1), old_unclassified
            )

        holder["holder_lock_owner_ok"] = False
        with self.assertRaisesRegex(
            preflight.RemoteProbeError, "remote_probe_inconsistent"
        ):
            preflight._normalize_remote_payload(30773, (0, 1), payload)

    def test_exact_holder_with_waiting_wrapper_is_structurally_valid(self) -> None:
        payload = physical_remote_payload(active_gpu=0)
        holder = payload["gpus"][0]  # type: ignore[index]
        holder.update({
            "compute_process_kinds": ["holder"],
            "holder_session": True,
            "holder_descendant_ok": True,
            "holder_lock_owner_ok": True,
            "holder_command_ok": True,
            "wrapper_process_present": True,
        })

        node = preflight._normalize_remote_payload(30773, (0, 1), payload)

        self.assertTrue(node.probe_ok)
        self.assertTrue(node.gpus[0].wrapper_process_present)
        self.assertEqual(node.gpus[0].compute_process_kinds, ("holder",))

    def test_holder_wrapper_without_complete_exact_evidence_fails_closed(self) -> None:
        payload = physical_remote_payload(active_gpu=0)
        holder = payload["gpus"][0]  # type: ignore[index]
        holder.update({
            "compute_process_kinds": ["holder"],
            "holder_session": True,
            "holder_descendant_ok": True,
            "holder_lock_owner_ok": False,
            "holder_command_ok": True,
            "wrapper_process_present": True,
        })

        with self.assertRaises(preflight.RemoteProbeError):
            preflight._normalize_remote_payload(30773, (0, 1), payload)

    def test_partial_or_contradictory_holder_evidence_is_rejected(self) -> None:
        partial = physical_remote_payload()
        partial["gpus"][0].pop("holder_command_ok")  # type: ignore[index]
        with self.assertRaisesRegex(
            preflight.RemoteProbeError,
            "remote_holder_evidence_fields_invalid",
        ):
            preflight._normalize_remote_payload(30773, (0, 1), partial)

        contradictory = physical_remote_payload()
        contradictory["gpus"][0].update({  # type: ignore[index]
            "holder_descendant_ok": True,
            "holder_lock_owner_ok": True,
            "holder_command_ok": True,
        })
        with self.assertRaisesRegex(
            preflight.RemoteProbeError,
            "remote_holder_evidence_inconsistent",
        ):
            preflight._normalize_remote_payload(30773, (0, 1), contradictory)

    def test_duplicate_physical_uuid_map_fails_closed(self) -> None:
        payload = physical_remote_payload()
        rows = payload["gpus"]  # type: ignore[index]
        rows[1]["gpu_uuid"] = rows[0]["gpu_uuid"]  # type: ignore[index]
        with self.assertRaisesRegex(
            preflight.RemoteProbeError, "remote_gpu_uuid_map_invalid"
        ):
            preflight._normalize_remote_payload(30773, (0, 1), payload)

    def test_process_uuid_must_equal_queried_physical_gpu(self) -> None:
        payload = physical_remote_payload(active_gpu=1)
        payload["gpus"][1]["compute_process_gpu_uuids"] = [  # type: ignore[index]
            "GPU-00000000-0000-0000-0000-000000000000"
        ]
        with self.assertRaisesRegex(
            preflight.RemoteProbeError,
            "remote_process_gpu_uuids_inconsistent",
        ):
            preflight._normalize_remote_payload(30773, (0, 1), payload)

    def test_binding_mismatch_is_explicit_report_reason(self) -> None:
        payload = physical_remote_payload(
            (0, 1, 2, 3), active_gpu=1, binding_ok=False
        )
        node = preflight._normalize_remote_payload(
            30773, (0, 1, 2, 3), payload
        )
        report = preflight.build_report(
            target_batch="batch0002",
            worker_count=11,
            launch_layout=(),
            claims=(),
            observations={30773: node},
            required_remote_gpu_layout={30773: (0, 1, 2, 3)},
        )
        gpu = next(
            item
            for item in report["nodes"][0]["gpus"]  # type: ignore[index,union-attr]
            if item["gpu"] == 1
        )
        self.assertFalse(report["ready"])
        self.assertFalse(gpu["physical_binding_ok"])
        self.assertIn("physical_gpu_binding_failed", gpu["reason_codes"])

    def test_probe_accepts_numeric_or_complete_uuid_for_same_physical_gpu(self) -> None:
        for value in (
            "0",
            "GPU-00000000-0000-0000-0000-000000000000",
        ):
            with self.subTest(value=value):
                payload = self.execute_probe_with_visible_device(value)
                self.assertTrue(payload["probe_ok"])
                self.assertTrue(payload["gpus"][0]["physical_binding_ok"])

    def test_probe_rejects_wrong_unknown_and_multiple_visible_devices(self) -> None:
        for value in (
            "1",
            "0,1",
            "GPU-ffffffff-ffff-ffff-ffff-ffffffffffff",
        ):
            with self.subTest(value=value):
                payload = self.execute_probe_with_visible_device(value)
                self.assertFalse(payload["probe_ok"])
                self.assertFalse(payload["gpus"][0]["compute_query_ok"])

    def test_probe_maps_pure_graphics_process_to_exact_gpu(self) -> None:
        payload = self.execute_probe_with_visible_device("0", graphics_only=True)
        self.assertTrue(payload["probe_ok"])
        self.assertEqual(payload["gpus"][0]["compute_process_count"], 1)
        self.assertEqual(payload["gpus"][0]["compute_process_kinds"], ["blender"])
        self.assertTrue(payload["gpus"][0]["physical_binding_ok"])

    def test_exact_locked_wrapper_scopes_transient_nvml_gap_to_one_gpu(self) -> None:
        payload = self.execute_probe_with_visible_device(
            "0",
            nvml_visible=False,
            exact_locked_wrapper=True,
        )

        self.assertTrue(payload["process_attribution_ok"])
        self.assertTrue(payload["probe_ok"])
        by_gpu = {int(row["gpu"]): row for row in payload["gpus"]}
        self.assertFalse(by_gpu[0]["process_tree_query_ok"])
        self.assertFalse(by_gpu[0]["compute_query_ok"])
        self.assertFalse(by_gpu[0]["physical_binding_ok"])
        self.assertTrue(by_gpu[0]["wrapper_process_present"])
        self.assertEqual(by_gpu[0]["lock_state"], "busy")
        self.assertTrue(by_gpu[1]["process_tree_query_ok"])
        self.assertTrue(by_gpu[1]["compute_query_ok"])
        self.assertTrue(by_gpu[1]["physical_binding_ok"])

        node = preflight._normalize_remote_payload(30773, (0, 1), payload)
        self.assertTrue(node.probe_ok)
        self.assertFalse(node.gpus[0].compute_query_ok)
        self.assertTrue(node.gpus[1].compute_query_ok)

    def test_nvml_gap_without_exact_lock_owner_remains_node_fail_closed(self) -> None:
        for changes in (
            {"exact_locked_wrapper": False},
            {"exact_locked_wrapper": True, "wrapper_lock_held": False},
        ):
            with self.subTest(changes=changes):
                payload = self.execute_probe_with_visible_device(
                    "0",
                    nvml_visible=False,
                    **changes,
                )
                self.assertFalse(payload["process_attribution_ok"])
                self.assertFalse(payload["probe_ok"])
                self.assertTrue(
                    all(not row["process_tree_query_ok"] for row in payload["gpus"])
                )
                self.assertTrue(
                    all(not row["compute_query_ok"] for row in payload["gpus"])
                )

    def test_unattributed_or_invalid_gap_remains_node_fail_closed(self) -> None:
        cases = (
            {"ps_visible": False},
            {
                "nvml_visible": False,
                "exact_locked_wrapper": True,
                "value": "0,1",
            },
        )
        for changes in cases:
            with self.subTest(changes=changes):
                value = str(changes.get("value", "0"))
                options = {
                    key: option for key, option in changes.items() if key != "value"
                }
                payload = self.execute_probe_with_visible_device(value, **options)
                self.assertFalse(payload["process_attribution_ok"])
                self.assertFalse(payload["probe_ok"])
                self.assertTrue(
                    all(not row["compute_query_ok"] for row in payload["gpus"])
                )

    def test_probe_ignores_only_its_exact_process_ancestry(self) -> None:
        payload = self.execute_probe_with_visible_device(
            "0",
            extra_process_rows=(
                "222 bash bash -lc '/opt/blender -b probe-wrapper.blend'\n"
            ),
            probe_pid=999,
            probe_parent_by_pid={999: 222, 222: 1},
        )
        self.assertTrue(payload["probe_ok"])
        self.assertTrue(payload["gpus"][0]["compute_query_ok"])

    def test_probe_preserves_detection_of_unrelated_unknown_wrapper(self) -> None:
        payload = self.execute_probe_with_visible_device(
            "0",
            extra_process_rows=(
                "223 bash bash -lc '/opt/blender -b unknown-wrapper.blend'\n"
            ),
        )
        self.assertFalse(payload["probe_ok"])
        self.assertFalse(payload["gpus"][0]["compute_query_ok"])

    def test_probe_does_not_treat_video2blender_path_as_blender_command(self) -> None:
        payload = self.execute_probe_with_visible_device(
            "0",
            extra_process_rows=(
                "224 bash bash /srv/video2blender_bootstrap/"
                "total_asset_node_bootstrap.sh heartbeat\n"
            ),
        )
        self.assertTrue(payload["probe_ok"])
        self.assertTrue(payload["gpus"][0]["compute_query_ok"])


class SshAndCliTests(unittest.TestCase):
    def test_remote_probe_audits_compute_graphics_and_process_tree(self) -> None:
        source = preflight.REMOTE_PROBE_PROGRAM
        self.assertIn("--query-compute-apps=pid,process_name", source)
        self.assertIn('"--query-gpu=index,uuid"', source)
        self.assertIn("pid_actual_uuids", source)
        self.assertIn("visible_gpu == actual_gpu", source)
        self.assertIn("normalize_gpu_uuid(token)", source)
        self.assertIn('if not token or "," in token:', source)
        self.assertIn('"pmon", "-i"', source)
        self.assertNotIn('if "C" not in parts[2].upper():', source)
        self.assertIn('["ps", "-eo", "pid=,comm=,args="]', source)
        self.assertIn("CUDA_VISIBLE_DEVICES=", source)
        self.assertIn("wrapper_process_present", source)
        self.assertIn('"list-panes", "-s", "-t", "=" + holder_session_name', source)
        self.assertIn("#{pane_pid}:#{pane_dead}", source)
        self.assertIn("holder_command_matches", source)
        self.assertIn("holder_lock_fd_matches", source)
        self.assertIn('"/proc/%s/fd/9" % pid', source)
        compile("EXPECTED_GPUS=[0, 1]\n" + source, "<remote-probe>", "exec")

    def test_ssh_probe_disables_password_fallback_and_rejects_bad_json(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="secret path: not json", stderr="private key"
        )
        runner = mock.Mock(return_value=completed)
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "audit_key"
            known_hosts = Path(directory) / "known_hosts"
            identity.write_text("test key", encoding="utf-8")
            identity.chmod(0o600)
            known_hosts.write_text("test host", encoding="utf-8")
            with self.assertRaisesRegex(
                preflight.RemoteProbeError, "remote_json_invalid"
            ) as caught:
                preflight.run_ssh_probe(
                    30422,
                    (0, 1, 2, 3),
                    host="root@10.26.6.88",
                    known_hosts=known_hosts,
                    identity_file=identity,
                    runner=runner,
                )
        command = runner.call_args.args[0]
        self.assertIn("BatchMode=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("PasswordAuthentication=no", command)
        self.assertIn("KbdInteractiveAuthentication=no", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertNotIn("sshpass", command)
        self.assertNotIn("secret path", str(caught.exception))
        self.assertNotIn("private key", str(caught.exception))

    def test_atomic_output_replaces_old_report_without_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preflight.json"
            output.write_text("old", encoding="utf-8")
            preflight.atomic_write_json(output, {"ready": True})
            self.assertEqual(json.loads(output.read_text()), {"ready": True})
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_identity_and_known_hosts_are_mandatory_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "audit_key"
            known_hosts = root / "known_hosts"
            identity.write_text("key", encoding="utf-8")
            known_hosts.write_text("host", encoding="utf-8")
            identity.chmod(0o644)
            with self.assertRaisesRegex(
                preflight.PreflightInputError, "permissions_invalid"
            ):
                preflight.validate_ssh_material(identity, known_hosts)
            identity.chmod(0o600)
            preflight.validate_ssh_material(identity, known_hosts)
            known_hosts.chmod(0o666)
            with self.assertRaisesRegex(
                preflight.PreflightInputError, "known_hosts_permissions_invalid"
            ):
                preflight.validate_ssh_material(identity, known_hosts)
            known_hosts.chmod(0o600)
            known_hosts.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                preflight.PreflightInputError, "known_hosts_permissions_invalid"
            ):
                preflight.validate_ssh_material(identity, known_hosts)
            known_hosts.write_text("host", encoding="utf-8")
            target = root / "known_hosts_target"
            target.write_text("host", encoding="utf-8")
            known_hosts.unlink()
            known_hosts.symlink_to(target)
            with self.assertRaisesRegex(
                preflight.PreflightInputError, "not_regular_file"
            ):
                preflight.validate_ssh_material(identity, known_hosts)
            known_hosts.unlink()
            known_hosts.write_text("host", encoding="utf-8")
            known_hosts.chmod(0o600)
            with self.assertRaisesRegex(
                preflight.PreflightInputError, "identity_missing"
            ):
                preflight.validate_ssh_material(None, known_hosts)
            known_hosts.unlink()
            with self.assertRaisesRegex(
                preflight.PreflightInputError, "known_hosts_unavailable"
            ):
                preflight.validate_ssh_material(identity, known_hosts)

    def test_cli_layout_error_writes_fail_closed_redacted_report_without_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preflight.json"
            with mock.patch.object(preflight, "collect_remote_preflight") as collect:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = preflight.main([
                        "--target-batch", "batch0001",
                        "--worker-count", "11",
                        "--launch-layout", "{bad-json",
                        "--output", str(output),
                    ])
            collect.assert_not_called()
            self.assertEqual(rc, preflight.BLOCKED_EXIT_CODE)
            report = json.loads(output.read_text())
            self.assertFalse(report["ready"])
            self.assertEqual(
                report["error_codes"], ["launch_layout_json_invalid"]
            )
            self.assertTrue(all(not node["reachable"] for node in report["nodes"]))

    def test_collect_audits_all_three_nodes(self) -> None:
        layout = preflight.parse_launch_layout(
            layout_payload(), target_batch="batch0001", worker_count=11
        )
        calls: list[tuple[int, tuple[int, ...]]] = []

        def runner(port: int, gpus: tuple[int, ...]) -> preflight.NodeObservation:
            calls.append((port, tuple(gpus)))
            return clean_node(port)

        report = preflight.collect_remote_preflight(
            target_batch="batch0001",
            worker_count=11,
            launch_layout=layout,
            claims=(),
            controllers=(),
            host="root@10.26.6.88",
            known_hosts=Path("/tmp/fixed-known-hosts"),
            identity_file=Path("/tmp/audit-key"),
            probe_runner=runner,
        )
        self.assertTrue(report["ready"])
        self.assertEqual(
            set(calls),
            {
                (30422, (0, 1, 2, 3)),
                (30773, (0, 1, 2, 3)),
                (30808, (0, 1, 2)),
            },
        )

    def test_cli_fails_closed_when_local_claims_change_during_remote_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preflight.json"
            identity = Path(directory) / "audit_key"
            known_hosts = Path(directory) / "known_hosts"
            identity.write_text("test key", encoding="utf-8")
            identity.chmod(0o600)
            known_hosts.write_text("test host", encoding="utf-8")
            claim = scheduler.WorkerClaim("batch0001", 30773, 0, 4, 11)
            ready_report = preflight.build_report(
                target_batch="batch0001",
                worker_count=11,
                launch_layout=preflight.parse_launch_layout(
                    layout_payload(), target_batch="batch0001", worker_count=11
                ),
                claims=(),
                observations={
                    port: clean_node(port)
                    for port in scheduler.REQUIRED_REMOTE_GPU_LAYOUT
                },
            )
            with mock.patch.object(
                preflight, "detect_worker_claims", side_effect=[(), (claim,)]
            ), mock.patch.object(
                preflight, "detect_screen_controllers", side_effect=[(), ()]
            ), mock.patch.object(
                preflight, "collect_remote_preflight", return_value=ready_report
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = preflight.main([
                        "--target-batch", "batch0001",
                        "--worker-count", "11",
                        "--launch-layout", layout_payload(),
                        "--output", str(output),
                        "--identity-file", str(identity),
                        "--known-hosts", str(known_hosts),
                    ])
            self.assertEqual(rc, preflight.BLOCKED_EXIT_CODE)
            report = json.loads(output.read_text())
            self.assertFalse(report["ready"])
            self.assertEqual(
                report["error_codes"], ["local_control_snapshot_changed"]
            )


class HolderTransactionAndShellTests(unittest.TestCase):
    def test_exact_holder_stop_waits_for_tmux_session_to_disappear(self) -> None:
        with mock.patch.object(holder_tx, "run_remote", return_value="") as remote:
            holder_tx.stop_exact_session(
                "total_asset_gpu_holder_g2",
                "tmux kill-session -t total_asset_gpu_holder_g2 2>/dev/null || true",
                gpu=2,
                host="root@10.26.6.88",
                port=30808,
                identity_file=Path("key"),
                known_hosts=Path("known_hosts"),
            )
        command = remote.call_args.args[0]
        self.assertIn("while tmux has-session", command)
        self.assertIn("sleep 0.5", command)
        self.assertIn("exit 42", command)
        self.assertIn("/tmp/total_asset_gpu_2.lock", command)
        self.assertIn("flock -n 9", command)
        self.assertIn("exit 43", command)
        self.assertIn("sleep 2", command)

    def test_secondary_incremental_layout_uses_only_validated_gpu_subset(self) -> None:
        functions = "\n".join((
            shell_function_source("normalized_secondary_gpu_ids"),
            shell_function_source("launch_layout_json"),
        ))
        base = (
            "BATCH_NAME=batch0001\nGLOBAL_WORKER_COUNT=11\n"
            "PORT=30773\nSECONDARY_PORT=30422\nTERTIARY_PORT=30808\n"
        )
        result = subprocess.run(
            ["bash", "-c", functions + "\n" + base + "launch_layout_json 1\n"],
            env={**os.environ, "TOTAL_ASSET_SECONDARY_GPU_IDS": "2,0"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        workers = json.loads(result.stdout)["workers"]
        secondary = [row for row in workers if row["port"] == 30422]
        self.assertEqual(
            secondary,
            [
                {"port": 30422, "gpu": 0, "worker_index": 0},
                {"port": 30422, "gpu": 2, "worker_index": 2},
            ],
        )
        self.assertEqual(len(workers), 9)

        for invalid in ("", "0,0", "0,4", "0, 1"):
            failed = subprocess.run(
                [
                    "bash", "-c",
                    functions + "\n" + base + "launch_layout_json 1\n",
                ],
                env={**os.environ, "TOTAL_ASSET_SECONDARY_GPU_IDS": invalid},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(failed.returncode, 75, invalid)

    def test_slot_commit_requires_verified_exact_finalizer_proof(self) -> None:
        functions = "\n".join((
            shell_function_source("mark_holder_slot_started"),
            shell_function_source("record_verified_finalizer"),
        ))
        unverified = subprocess.run(
            [
                "bash", "-c",
                functions
                + "\nHOLDER_TRANSACTION_STARTED=''\n"
                + "HOLDER_TRANSACTION_VERIFIED_FINALIZERS=''\n"
                + "mark_holder_slot_started 30773 0 4 11\n",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(unverified.returncode, 75)
        self.assertIn("lacks a verified exact finalizer", unverified.stderr)

        verified = subprocess.run(
            [
                "bash", "-c",
                functions
                + "\nHOLDER_TRANSACTION_STARTED=''\n"
                + "HOLDER_TRANSACTION_VERIFIED_FINALIZERS=''\n"
                + "record_verified_finalizer 30773 0 4 11\n"
                + "mark_holder_slot_started 30773 0 4 11\n"
                + "printf '%s\\n' \"$HOLDER_TRANSACTION_STARTED\"\n",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(verified.stdout.strip(), "30773:0")

    def test_finalizer_audit_rejects_wrong_payload_and_ambiguous_session(self) -> None:
        function = shell_function_source("finalizer_screen_state")
        finalizer = "total_asset_batch0001_g0_finalizer"
        token = "total_asset_gpu_finalizer_v2_" + "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "screen.txt"
            processes = root / "ps.txt"
            screen_bin = root / "screen"
            ps_bin = root / "ps"
            screen_bin.write_text(
                "#!/bin/sh\ncat \"$FAKE_SCREEN_OUTPUT\"\n"
                "exit \"${FAKE_SCREEN_RC:-0}\"\n",
                encoding="utf-8",
            )
            ps_bin.write_text(
                "#!/bin/sh\ncat \"$FAKE_PS_OUTPUT\"\n"
                "exit \"${FAKE_PS_RC:-0}\"\n",
                encoding="utf-8",
            )
            screen_bin.chmod(0o755)
            ps_bin.chmod(0o755)
            listing.write_text(
                f"There is a screen on:\n\t1234.{finalizer}\t(Detached)\n"
                "1 Socket in /tmp/screens.\n",
                encoding="utf-8",
            )
            processes.write_text(
                "1234 1 SCREEN -S exact\n"
                f"2345 1234 /bin/bash -lc payload {token}\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "TOTAL_ASSET_SCREEN_BIN": str(screen_bin),
                "TOTAL_ASSET_PS_BIN": str(ps_bin),
                "FAKE_SCREEN_OUTPUT": str(listing),
                "FAKE_PS_OUTPUT": str(processes),
            }

            def audit(expected_token: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        "bash", "-c",
                        function + "\nfinalizer_screen_state \"$1\" \"$2\"\n",
                        "bash", finalizer, expected_token,
                    ],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            valid = audit(token)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(valid.stdout.strip(), "verified")

            processes.write_text(
                "1234 1 SCREEN -S exact\n"
                f"2300 1234 login -pflq user /bin/bash -lc payload {token}\n"
                f"2345 2300 /bin/bash -lc payload {token}\n",
                encoding="utf-8",
            )
            macos_wrapper_chain = audit(token)
            self.assertEqual(macos_wrapper_chain.returncode, 0)
            self.assertEqual(macos_wrapper_chain.stdout.strip(), "verified")

            processes.write_text(
                "1234 1 SCREEN -S exact\n"
                f"2300 1234 /bin/bash -lc payload {token}\n"
                f"2345 1234 /bin/bash -lc payload {token}\n",
                encoding="utf-8",
            )
            sibling_tokens = audit(token)
            self.assertEqual(sibling_tokens.returncode, 0)
            self.assertEqual(sibling_tokens.stdout.strip(), "mismatch")

            wrong_payload = audit("total_asset_gpu_finalizer_v2_" + "b" * 64)
            self.assertEqual(wrong_payload.returncode, 0, wrong_payload.stderr)
            self.assertEqual(wrong_payload.stdout.strip(), "mismatch")

            listing.write_text(
                f"There are screens on:\n\t1234.{finalizer}\t(Detached)\n"
                f"\t5678.{finalizer}\t(Detached)\n"
                "2 Sockets in /tmp/screens.\n",
                encoding="utf-8",
            )
            ambiguous = audit(token)
            self.assertEqual(ambiguous.returncode, 0, ambiguous.stderr)
            self.assertEqual(ambiguous.stdout.strip(), "mismatch")

            listing.write_text("screen transport failed\n", encoding="utf-8")
            env["FAKE_SCREEN_RC"] = "1"
            failed_closed = audit(token)
            self.assertEqual(failed_closed.returncode, 75)

    def test_stale_finalizer_reap_targets_only_exact_pid_and_name(self) -> None:
        function = shell_function_source("reap_exact_screen_sessions")
        finalizer = "total_asset_batch0001_g0_finalizer"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "screen.txt"
            calls = root / "calls.txt"
            screen_bin = root / "screen"
            listing.write_text(
                "There are screens on:\n"
                f"\t1234.{finalizer}\t(Detached)\n"
                f"\t5678.{finalizer}\t(Detached)\n"
                "\t9999.total_asset_batch0001_g1_finalizer\t(Detached)\n"
                "3 Sockets in /tmp/screens.\n",
                encoding="utf-8",
            )
            screen_bin.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = '-ls' ]; then cat \"$FAKE_SCREEN_OUTPUT\"; exit 0; fi\n"
                "printf '%s\\n' \"$*\" >>\"$FAKE_SCREEN_CALLS\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            screen_bin.chmod(0o755)
            result = subprocess.run(
                [
                    "bash", "-c",
                    function + "\nreap_exact_screen_sessions \"$1\"\n",
                    "bash", finalizer,
                ],
                env={
                    **os.environ,
                    "TOTAL_ASSET_SCREEN_BIN": str(screen_bin),
                    "FAKE_SCREEN_OUTPUT": str(listing),
                    "FAKE_SCREEN_CALLS": str(calls),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [
                    f"-S 1234.{finalizer} -X quit",
                    f"-S 5678.{finalizer} -X quit",
                ],
            )

    def test_claim_without_controller_timeout_is_cleaned_before_rollback(self) -> None:
        wait = "\n".join((
            shell_function_source("cleanup_failed_worker_launch"),
            shell_function_source("wait_for_verified_worker"),
        ))
        harness = """
CLEANED=0
TERMINATED=0
worker_slot_state() {
  if [ "$CLEANED" = "1" ]; then printf '%s\n' inactive; else printf '%s\n' claim_only; fi
}
reap_exact_screen_sessions() { :; }
terminate_exact_worker_tree() { CLEANED=1; TERMINATED=1; }
TOTAL_ASSET_WORKER_START_TIMEOUT_SECONDS=0
TOTAL_ASSET_WORKER_CLEANUP_TIMEOUT_SECONDS=1
set +e
wait_for_verified_worker worker_screen 30773 0 4 11
rc=$?
printf '%s:%s:%s\n' "$rc" "$TERMINATED" "$CLEANED"
"""
        result = subprocess.run(
            ["bash", "-c", wait + harness],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "75:1:1")
        cleanup = shell_function_source("terminate_exact_worker_tree")
        self.assertIn('"30"', cleanup)
        self.assertIn("signal.SIGTERM", cleanup)
        self.assertIn("signal.SIGKILL", cleanup)
        self.assertIn("parse_worker_claims", cleanup)

    def test_worker_cleanup_rejects_pid_reuse_before_destructive_signal(self) -> None:
        cleanup = shell_function_source("terminate_exact_worker_tree")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / "counter"
            fake_ps = root / "ps"
            fake_ps.write_text(
                "#!/bin/sh\n"
                f"n=0; [ ! -e '{counter}' ] || n=$(cat '{counter}')\n"
                "n=$((n + 1))\n"
                f"printf '%s' \"$n\" >'{counter}'\n"
                "if [ \"$n\" -eq 1 ]; then start='Thu Jul 17 12:00:00 2026'; "
                "else start='Thu Jul 17 12:01:00 2026'; fi\n"
                "printf '%s\\n' \"999999 1 $start python3 python3 "
                "run_total_asset_render_worker.py --batch batch0001 "
                "--remote-port 30773 --gpu 2 --worker-index 6 --worker-count 8\"\n",
                encoding="utf-8",
            )
            fake_ps.chmod(0o755)
            result = subprocess.run(
                [
                    "bash", "-c",
                    cleanup
                    + "\nBATCH_NAME=batch0001\n"
                    + "TOTAL_ASSET_PS_BIN=\"$1\"\n"
                    + "TOTAL_ASSET_WORKER_TERM_GRACE_SECONDS=0\n"
                    + "set +e\n"
                    + "terminate_exact_worker_tree 30773 2 6 8\n"
                    + "printf '%s\\n' \"$?\"\n",
                    "bash", str(fake_ps),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "75")

    def test_every_public_formal_launcher_is_canonical_or_explicitly_blocked(self) -> None:
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        guard = shell[shell.index('case "$COMMAND" in'):
                      shell.index("require_mount()")]
        dispatch = shell[shell.rindex('case "$COMMAND" in'):]
        blocked = {
            "pilot20", "pilot1000-cycles", "pilot1000-source",
            "recover20", "recover1000",
        }
        for command in blocked:
            self.assertIn(command, guard)
        blocked_case = dispatch[
            dispatch.index("pilot20|pilot1000-cycles|pilot1000-source|recover20|recover1000)"):
            dispatch.index("  batch)")
        ]
        self.assertIn("block_legacy_formal_launcher", blocked_case)
        for command in (
            "batch", "pilot1000", "pilot1000-handoff",
            "pilot1000-partition0-30422", "batch-partition-30808",
        ):
            self.assertIn(command, guard)
        legacy_block = shell_function_source("block_legacy_formal_launcher")
        self.assertIn("legacy non-canonical formal partition launcher", legacy_block)
        self.assertIn("return 75", legacy_block)

    def test_legacy_transition_handoff_is_fail_closed_and_orders_the_barrier(self) -> None:
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        guard = shell[
            shell.index('case "$COMMAND" in'):shell.index("require_mount()")
        ]
        launcher = shell_function_source("start_legacy_transition_handoff")
        loop = shell_function_source("legacy_transition_handoff_loop")

        # The waiting screen must never own the scheduler lock. Cleanup and
        # resume/primary launch enter their existing guarded commands one at a time.
        self.assertNotIn("legacy-transition-handoff", guard)
        self.assertIn("quarantined-legacy-idle-cleanup", guard)
        self.assertIn("resume", guard)
        self.assertIn("barrier", guard)
        self.assertIn("-u TOTAL_ASSET_SCHEDULER_LOCK_HELD", launcher)
        self.assertIn("-u TOTAL_ASSET_SCHEDULER_LOCK_FD", launcher)
        self.assertIn("must start outside the scheduler lock", launcher)
        self.assertIn("inherited a scheduler lock", loop)

        # mkdir is the atomic single-instance claim. An unexplained stale
        # claim is preserved for exact manual audit rather than auto-removed.
        self.assertIn('name="total_asset_legacy_transition_handoff"', launcher)
        self.assertIn('exact_screen_count "$name"', launcher)
        self.assertIn('mkdir -m 700 -- "$instance_dir"', launcher)
        self.assertIn("requires exact manual audit", launcher)
        self.assertNotIn("recovered: removed stale", launcher)
        self.assertIn("TOTAL_ASSET_LEGACY_TRANSITION_INTERNAL=1", launcher)
        self.assertIn("lacks its exact launcher attestation", loop)
        self.assertIn("lacks one exact controlling screen", loop)
        self.assertIn('validate_legacy_transition_target "$state" "$controller"', launcher)
        self.assertNotIn("skipped: exact legacy transition handoff", launcher)
        self.assertNotIn("BASHPID", launcher + loop)
        self.assertIn("printf '%s\\n' \"$$\"", launcher)
        self.assertIn("printf '%s\\n' \"$$\"", loop)

        readiness = loop.index("quarantined_legacy_remote_ready")
        cleanup = loop.index('bash "$0" quarantined-legacy-idle-cleanup')
        marker_branch = loop.index('if [ -e "$legacy_drain_file" ]')
        resume = loop.index('bash "$0" resume')
        primary_launch = loop.index('bash "$0" pilot1000-handoff')
        continuous = loop.index('bash "$0" continuous')
        self.assertLess(readiness, cleanup)
        self.assertLess(cleanup, marker_branch)
        self.assertLess(marker_branch, resume)
        self.assertLess(resume, primary_launch)
        self.assertLess(primary_launch, continuous)
        self.assertIn("wc8 marker is already absent", loop)
        self.assertIn("TOTAL_ASSET_DRAIN_WORKER_COUNT=8", loop)
        self.assertIn("TOTAL_ASSET_GLOBAL_WORKER_COUNT=11", loop)
        self.assertIn("TOTAL_ASSET_ALLOW_COMPATIBLE_ACTIVE=1", loop)
        self.assertIn("TOTAL_ASSET_TRANSITION_SECONDARY_GPU_IDS", loop)
        self.assertIn("TOTAL_ASSET_INCLUDE_SECONDARY=1", loop)
        self.assertIn("TOTAL_ASSET_SECONDARY_GPU_IDS=0,1,2,3", loop)
        self.assertIn("TOTAL_ASSET_BATCH_SIZE=1000", loop)
        self.assertIn('sleep "$retry_seconds"', loop)
        self.assertIn("exact_screen_count total_asset_batch_scheduler", loop)
        self.assertIn("exact_continuous_scheduler_process_count", loop)
        scheduler_process = shell_function_source(
            "exact_continuous_scheduler_process_count"
        )
        self.assertIn('["ps", "-axo", "args="]', scheduler_process)
        self.assertIn('os.path.basename(executable)', scheduler_process)
        self.assertIn('os.path.basename(token) == "total_asset_scheduler.py"', scheduler_process)
        self.assertIn('argv[matches[0] + 1] == "run"', scheduler_process)
        self.assertNotIn("pkill", launcher + loop)
        self.assertNotIn("screen -S", launcher + loop)
        self.assertIn('local log="$LOG_ROOT/', launcher)

        readiness_helper = shell_function_source(
            "quarantined_legacy_remote_ready"
        )
        self.assertIn("verify_remote_slot_clean", readiness_helper)
        self.assertNotIn("atomic_write", readiness_helper)
        self.assertNotIn("cleanup_quarantined", readiness_helper)

        target = shell_function_source("validate_legacy_transition_target")
        self.assertIn('expected = ("batch0001", 30773, 3, 7, 8)', target)
        self.assertIn("validate_quarantine_controller_token", target)
        self.assertIn("quarantine_cleanup_complete", target)
        self.assertIn("worker_cooperative_ack_v1", target)
        self.assertIn("persisted quarantine evidence", target)

        continuous_launcher = shell_function_source("start_continuous_scheduler")
        self.assertIn("total_asset_legacy_transition_handoff", continuous_launcher)
        self.assertIn("legacy_transition_handoff.instance", continuous_launcher)
        self.assertIn("attest_internal_handoff_continuous_call", continuous_launcher)
        self.assertIn("reserved for the attested transition handoff", continuous_launcher)

        internal_attestation = shell_function_source(
            "attest_internal_handoff_continuous_call"
        )
        self.assertIn('TOTAL_ASSET_LEGACY_TRANSITION_INTERNAL:-0', internal_attestation)
        self.assertIn("exact_screen_count total_asset_legacy_transition_handoff", internal_attestation)
        self.assertIn('python3 - "$owner_file" "$PPID"', internal_attestation)
        self.assertIn("stat.S_ISREG", internal_attestation)
        self.assertIn("metadata.st_uid != os.geteuid()", internal_attestation)

    def test_scheduler_process_attestation_rejects_embedded_shell_text(self) -> None:
        helper = shell_function_source("exact_continuous_scheduler_process_count")
        with tempfile.TemporaryDirectory() as directory:
            fake_ps = Path(directory) / "ps"
            fake_ps.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'/Library/Frameworks/Python.framework/Versions/3.10/Resources/Python.app/Contents/MacOS/Python blender/scripts/total_asset_scheduler.py run --batch-size 1000' "
                "'SCREEN -dmS total_asset_batch_scheduler /bin/bash -lc python3 blender/scripts/total_asset_scheduler.py run' "
                "'/usr/bin/login -pfl user /bin/bash -lc python3 blender/scripts/total_asset_scheduler.py run' "
                "'/bin/bash -lc python3 blender/scripts/total_asset_scheduler.py run' "
                "'python3 blender/scripts/total_asset_scheduler.py status'\n",
                encoding="utf-8",
            )
            fake_ps.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", helper + "\nexact_continuous_scheduler_process_count"],
                env={
                    **os.environ,
                    "PATH": f"{directory}:{os.environ.get('PATH', '')}",
                },
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")

    def test_transition_screen_count_fails_closed_on_screen_transport_error(self) -> None:
        helper = shell_function_source("exact_screen_count")
        with tempfile.TemporaryDirectory() as directory:
            fake_screen = Path(directory) / "screen"
            fake_screen.write_text(
                "#!/bin/sh\necho 'screen transport failed' >&2\nexit 1\n",
                encoding="utf-8",
            )
            fake_screen.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", helper + "\nexact_screen_count target"],
                env={
                    **os.environ,
                    "TOTAL_ASSET_SCREEN_BIN": str(fake_screen),
                },
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 75)
        self.assertEqual(result.stdout, "")

    def test_hidden_legacy_transition_loop_requires_launcher_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_root = Path(directory) / "assets"
            result = subprocess.run(
                ["bash", str(PIPELINE_SHELL), "legacy-transition-handoff-loop"],
                cwd=PROJECT,
                env={
                    **os.environ,
                    "TOTAL_ASSET_ROOT": str(asset_root),
                    "TOTAL_ASSET_LEGACY_TRANSITION_INTERNAL": "1",
                },
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 75)
        self.assertIn("lacks its exact launcher attestation", result.stderr)

    def test_legacy_transition_target_is_bound_to_the_exact_old_g3_slot(self) -> None:
        helper = shell_function_source("validate_legacy_transition_target")

        def validate(payload: dict[str, object], controller: str) -> subprocess.CompletedProcess[str]:
            state.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    helper + '\nvalidate_legacy_transition_target "$1" "$2"',
                    "bash",
                    str(state),
                    controller,
                ],
                cwd=PROJECT,
                text=True,
                capture_output=True,
                check=False,
            )

        canonical = "4321.total_asset_batch0001_g3"
        retry = canonical + "_retry"
        base: dict[str, object] = {
            "schema_version": 1,
            "status": "boundary_stopped",
            "boundary_proof": "not_cooperative",
            "batch": "batch0001",
            "remote_port": 30773,
            "gpu": 3,
            "worker_index": 7,
            "worker_count": 8,
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "legacy.json"
            for status in (
                "boundary_stopped",
                "quarantine_cleanup_running",
                "quarantine_cleanup_blocked",
                "quarantine_cleanup_complete",
            ):
                for controller in (canonical, retry):
                    payload = {
                        **base,
                        "status": status,
                        "controller_session": controller,
                    }
                    result = validate(payload, controller)
                    self.assertEqual(result.returncode, 0, result.stderr)

            invalid_cases = (
                ({**base, "batch": "batch0002"}, canonical),
                ({**base, "remote_port": 30808}, canonical),
                ({**base, "gpu": 2}, canonical),
                ({**base, "worker_index": 6}, canonical),
                ({**base, "worker_count": 11}, canonical),
                ({**base, "status": "waiting_boundary"}, canonical),
                ({**base, "boundary_proof": "worker_cooperative_ack_v1"}, canonical),
                ({**base}, canonical + "_handoff"),
                ({**base, "controller_session": retry}, canonical),
            )
            for payload, controller in invalid_cases:
                result = validate(payload, controller)
                self.assertEqual(result.returncode, 75, (payload, result.stderr))
                self.assertIn("blocked:", result.stderr)

    def test_internal_continuous_attestation_binds_owner_to_caller_ppid(self) -> None:
        screen_helper = shell_function_source("exact_screen_count")
        attestation = shell_function_source(
            "attest_internal_handoff_continuous_call"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "worker_control"
            instance = control / "legacy_transition_handoff.instance"
            instance.mkdir(parents=True, mode=0o700)
            owner = instance / "owner.pid"
            fake_screen = root / "screen"
            fake_screen.write_text(
                "#!/bin/sh\n"
                "printf '\\t4321.total_asset_legacy_transition_handoff\\t(Detached)\\n'\n",
                encoding="utf-8",
            )
            fake_screen.chmod(0o755)

            command = (
                screen_helper
                + "\n"
                + attestation
                + '\nWORKER_CONTROL_ROOT="$1"; '
                + "attest_internal_handoff_continuous_call"
            )
            environment = {
                **os.environ,
                "TOTAL_ASSET_SCREEN_BIN": str(fake_screen),
                "TOTAL_ASSET_LEGACY_TRANSITION_INTERNAL": "1",
            }
            owner.write_text(f"{os.getpid()}\n", encoding="ascii")
            valid = subprocess.run(
                ["bash", "-c", command, "bash", str(control)],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)

            owner.write_text("999999\n", encoding="ascii")
            wrong_owner = subprocess.run(
                ["bash", "-c", command, "bash", str(control)],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(wrong_owner.returncode, 75)

            owner.write_text(f"{os.getpid()}\n", encoding="ascii")
            forged_environment = {
                **environment,
                "TOTAL_ASSET_LEGACY_TRANSITION_INTERNAL": "0",
            }
            forged = subprocess.run(
                ["bash", "-c", command, "bash", str(control)],
                env=forged_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(forged.returncode, 75)

    def test_continuous_is_blocked_by_unattested_handoff_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_root = Path(directory) / "assets"
            instance = (
                asset_root
                / "_asset_inventory"
                / "worker_control"
                / "legacy_transition_handoff.instance"
            )
            instance.mkdir(parents=True)
            fake_screen = Path(directory) / "screen"
            fake_screen.write_text(
                "#!/bin/sh\n"
                "printf 'No Sockets found.\\n' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_screen.chmod(0o755)
            result = subprocess.run(
                ["bash", str(PIPELINE_SHELL), "continuous"],
                cwd=PROJECT,
                env={
                    **os.environ,
                    "TOTAL_ASSET_ROOT": str(asset_root),
                    "TOTAL_ASSET_SCREEN_BIN": str(fake_screen),
                },
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 75)
        self.assertIn("reserved for the attested transition handoff", result.stderr)

    def test_existing_exact_handoff_screen_is_blocked_not_reported_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            state = root / "legacy.json"
            controller = "4321.total_asset_batch0001_g3_retry"
            state.write_text(json.dumps({
                "schema_version": 1,
                "status": "boundary_stopped",
                "boundary_proof": "not_cooperative",
                "batch": "batch0001",
                "remote_port": 30773,
                "gpu": 3,
                "worker_index": 7,
                "worker_count": 8,
                "controller_session": controller,
            }), encoding="utf-8")
            key = root / "key"
            known_hosts = root / "known_hosts"
            key.touch()
            known_hosts.touch()
            fake_screen = root / "screen"
            fake_screen.write_text(
                "#!/bin/sh\n"
                "printf '\\t4321.total_asset_legacy_transition_handoff\\t(Detached)\\n'\n",
                encoding="utf-8",
            )
            fake_screen.chmod(0o755)
            fake_ps = root / "ps"
            fake_ps.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_ps.chmod(0o755)
            result = subprocess.run(
                ["bash", str(PIPELINE_SHELL), "legacy-transition-handoff"],
                cwd=PROJECT,
                env={
                    **os.environ,
                    "PATH": f"{root}:{os.environ.get('PATH', '')}",
                    "TOTAL_ASSET_ROOT": str(asset_root),
                    "TOTAL_ASSET_SCREEN_BIN": str(fake_screen),
                    "TOTAL_ASSET_PREFLIGHT_SSH_KEY": str(key),
                    "TOTAL_ASSET_SSH_KNOWN_HOSTS": str(known_hosts),
                    "TOTAL_ASSET_LEGACY_DRAIN_STATE": str(state),
                    "TOTAL_ASSET_LEGACY_CONTROLLER_SESSION": controller,
                },
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn("handoff screen already exists", result.stderr)
        self.assertNotIn("skipped:", result.stdout)

    def test_worker_slot_audit_failures_are_explicitly_captured(self) -> None:
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        wait = shell_function_source("wait_for_verified_worker")
        self.assertIn('if ! state="$(worker_slot_state', wait)
        self.assertIn("cleanup_failed_worker_launch", wait)
        for start_name, end_name in (
            ("start_pilot1000_partition0_30422()", "start_batch_partition_30808()"),
            ("start_batch_partition_30808()", "start_pilot1000_handoff()"),
            ("start_pilot1000_handoff()", "batch_has_activity()"),
        ):
            launch = shell[shell.index(start_name):shell.index(end_name)]
            self.assertIn('if ! slot_state="$(worker_slot_state', launch)
        cleanup = shell_function_source("cleanup_failed_worker_launch")
        self.assertIn("terminate_exact_worker_tree", cleanup)
        self.assertIn('if ! state="$(worker_slot_state', cleanup)

    def test_formal_partition_launchers_do_not_leave_orphan_monitors(self) -> None:
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        for start_name, end_name in (
            ("start_pilot1000_partition0_30422()", "start_batch_partition_30808()"),
            ("start_batch_partition_30808()", "start_pilot1000_handoff()"),
            ("start_pilot1000_handoff()", "batch_has_activity()"),
        ):
            launch = shell[shell.index(start_name):shell.index(end_name)]
            self.assertNotIn("_monitor", launch, start_name)
            self.assertNotIn("total_asset_pipeline.py gallery", launch, start_name)

    def test_only_batch0002_formal_workers_enable_obsolete_failure_retry(self) -> None:
        retry_args = shell_function_source("formal_retry_args")
        self.assertIn('BATCH_NAME" = "batch0002', retry_args)
        self.assertIn("--retry-obsolete-failures", retry_args)
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        for start_name, end_name in (
            ("start_pilot1000_partition0_30422()", "start_batch_partition_30808()"),
            ("start_batch_partition_30808()", "start_pilot1000_handoff()"),
            ("start_pilot1000_handoff()", "batch_has_activity()"),
        ):
            launch = shell[shell.index(start_name):shell.index(end_name)]
            self.assertIn('retry_args="$(formal_retry_args)"', launch)
            self.assertIn("--resume-unprocessed-only $retry_args", launch)
            self.assertIn("--runtime-aware-partition", launch)

    def test_all_node_preflights_require_blender_pid_uuid_attestation(self) -> None:
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        for start_name, end_name in (
            ("remote_preflight()", "secondary_remote_preflight()"),
            ("secondary_remote_preflight()", "ensure_secondary_holder_audit_runtime()"),
            ("tertiary_remote_preflight()", "start_quarantine_partial_batch()"),
        ):
            preflight = shell[shell.index(start_name):shell.index(end_name)]
            self.assertIn(
                "remote.configure_gpu(run_vulkaninfo_probe=False)", preflight
            )
            self.assertIn("remote.ensure_vulkan_runtime", preflight)
            self.assertIn('for family in ("4.5", "5.1")', preflight)
            self.assertIn("physical_pid_uuid_attested", preflight)
            self.assertNotIn("TOTAL_ASSET_SMOKE", preflight)

    def test_holder_state_is_atomic_deduplicated_and_started_slots_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "holders.json"
            holder_tx.atomic_write_state(state, ())
            holder_tx.add_restore_slot(state, 30773, 0)
            holder_tx.add_restore_slot(state, 30773, 0)
            self.assertEqual(holder_tx.load_state(state), ((30773, 0),))
            self.assertEqual(
                holder_tx.parse_started_slots("30773:0,30808:2"),
                {(30773, 0), (30808, 2)},
            )
            with self.assertRaises(holder_tx.HolderTransactionError):
                holder_tx.parse_started_slots("30773:g0")

    def test_task1_legacy_release_records_rollback_before_exact_kill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "holders.json"
            holder_tx.atomic_write_state(state, ())
            observation = mock.Mock(state="complete")
            with mock.patch.object(
                holder_tx, "observe_precompleted_task1", return_value=observation
            ), mock.patch.object(
                holder_tx, "exact_session_present", side_effect=[True, False]
            ), mock.patch.object(holder_tx, "stop_exact_session") as stop:
                holder_tx.release_task1_legacy(
                    state,
                    runtime_path=Path("runtime.json"),
                    log_path=Path("task1.log"),
                    status_csv=Path("status.csv"),
                    required_rows=800,
                    host="root@10.26.6.88",
                    port=30773,
                    identity_file=Path("key"),
                    known_hosts=Path("known_hosts"),
                )
            self.assertEqual(holder_tx.load_state(state), ((30773, 0),))
            self.assertEqual(stop.call_args.args[0], "GPU_holder")
            self.assertNotIn("pkill", stop.call_args.args[1])

    def test_shell_orders_local_check_release_report_barrier_and_rollback(self) -> None:
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        prepare = shell[shell.index("prepare_remote_launch_preflight()"):
                        shell.index("barrier_preflight()")]
        self.assertLess(prepare.index("local_barrier_precheck"), prepare.index("begin_holder_transaction"))
        self.assertLess(prepare.index("verify_and_release_task1_legacy_holder"), prepare.index("release_launch_layout_holders"))
        self.assertLess(prepare.index("release_launch_layout_holders"), prepare.index("generate_remote_preflight"))
        secondary = shell[shell.index("start_pilot1000_partition0_30422()"):
                          shell.index("start_batch_partition_30808()")]
        self.assertIn("barrier_preflight 1 1 secondary_remote_preflight", secondary)
        self.assertLess(
            secondary.index("normalized_secondary_gpu_ids"),
            secondary.index("barrier_preflight 1 1 secondary_remote_preflight"),
        )
        self.assertIn(
            'TOTAL_ASSET_SECONDARY_GPU_IDS="$secondary_gpu_ids"', secondary
        )
        self.assertIn("remote.ensure_vulkan_runtime", shell)
        self.assertIn("wait_for_verified_worker", secondary)
        screen_position = secondary.index('screen -dmS "$name"')
        wait_position = secondary.index("wait_for_verified_worker", screen_position)
        finalizer_position = secondary.index("ensure_gpu_finalizer", wait_position)
        commit_position = secondary.index("mark_holder_slot_started", finalizer_position)
        self.assertLess(screen_position, wait_position)
        self.assertLess(wait_position, finalizer_position)
        self.assertLess(finalizer_position, commit_position)
        wait_helper = shell_function_source("wait_for_verified_worker")
        self.assertNotIn("mark_holder_slot_started", wait_helper)
        finalizer_helper = shell_function_source("ensure_gpu_finalizer")
        self.assertNotIn("drained: GPU", finalizer_helper)
        self.assertNotIn("if [ -e '$drain_file' ]", finalizer_helper)
        self.assertIn("replacement worker detected", finalizer_helper)
        self.assertIn("total_asset_holder_transaction.py hold", finalizer_helper)
        self.assertIn("finalizer_screen_state", finalizer_helper)
        self.assertIn("reap_exact_screen_sessions", finalizer_helper)
        reap_helper = shell_function_source("reap_exact_screen_sessions")
        self.assertIn('f"{pid}.{name}"', reap_helper)
        self.assertNotIn("pkill", reap_helper)
        for start_name, end_name in (
            ("start_pilot1000_partition0_30422()", "start_batch_partition_30808()"),
            ("start_batch_partition_30808()", "start_pilot1000_handoff()"),
            ("start_pilot1000_handoff()", "batch_has_activity()"),
        ):
            launch = shell[shell.index(start_name):shell.index(end_name)]
            ensure_positions = [
                index for index in range(len(launch))
                if launch.startswith("ensure_gpu_finalizer", index)
            ]
            commit_positions = [
                index for index in range(len(launch))
                if launch.startswith("mark_holder_slot_started", index)
            ]
            self.assertEqual(len(ensure_positions), 2, start_name)
            self.assertEqual(len(commit_positions), 2, start_name)
            self.assertLess(ensure_positions[0], commit_positions[0], start_name)
            self.assertLess(commit_positions[0], ensure_positions[1], start_name)
            self.assertLess(ensure_positions[1], commit_positions[1], start_name)
        resume = shell[shell.index("  resume)"):shell.index("  repair-batch)")]
        self.assertIn('--worker-count "$GLOBAL_WORKER_COUNT"', resume)
        self.assertIn('--drain-worker-count "$drain_worker_count"', resume)
        self.assertIn('--remote-preflight-json "$REMOTE_PREFLIGHT_REPORT"', resume)
        self.assertIn(
            '"$resume_allow_compatible" "${TOTAL_ASSET_INCLUDE_SECONDARY:-0}" "" "$drain_worker_count"',
            resume,
        )
        self.assertIn("--allow-compatible-active", resume)
        repair = shell[shell.index("start_batch_repair()"):
                       shell.index("start_batch_all_servers()")]
        self.assertIn("build_batch0001_deferred_repair_manifests.py validate", repair)
        self.assertIn("--only-status deferred --force", repair)
        self.assertIn("--repair-strategy '$strategy'", repair)
        self.assertIn("--repair-slot-index '$slot_index'", repair)
        self.assertIn("--drain-file '$drain_file'", repair)
        self.assertIn("release_quarantine_partial_holders", repair)
        self.assertIn("ensure_gpu_finalizer", repair)
        self.assertIn("mark_holder_slot_started", repair)
        self.assertLess(
            repair.index('BATCH_NAME="$label"'),
            repair.index("wait_for_verified_worker"),
        )
        self.assertNotIn("pkill", repair)
        handoff = shell_function_source("start_failed_repair_handoff")
        self.assertIn("failed_repair_execution_report", handoff)
        self.assertIn("TOTAL_ASSET_FAILED_REPAIR_INTERNAL=1", handoff)
        self.assertIn("failed-repair-handoff-loop", handoff)
        source_wait = shell_function_source("wait_for_failed_repair_source_slot")
        self.assertIn('worker_slot_state "$SECONDARY_PORT" 0 0 11', source_wait)
        self.assertIn("formal_slot_local_state", source_wait)
        self.assertIn("known_repair:", source_wait)
        self.assertIn("resuming: verified managed repair owner", source_wait)
        self.assertIn(
            'worker_screen="total_asset_batch0001_${SECONDARY_PORT}_g0"',
            source_wait,
        )
        self.assertIn('finalizer="${worker_screen}_finalizer"', source_wait)
        self.assertNotIn("kill", source_wait)
        lane = shell_function_source("run_failed_repair_lane")
        positions = [
            lane.index(group)
            for group in (
                "course_timeout_exact",
                "inferred_blank_timeout",
                "dynamic_static_contract",
                "transport",
                "scene_audit",
            )
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("failed-repair-w0-start", lane)
        self.assertIn('handoff_status="review_required"', lane)
        self.assertIn("batch_complete=false", lane)
        failed_worker = shell_function_source("start_failed_repair_group_worker")
        self.assertIn("--failed-repair-group '$group'", failed_worker)
        self.assertIn("--only-status failed --force", failed_worker)
        self.assertIn("--drain-file '$drain_file'", failed_worker)
        self.assertIn("release_failed_repair_holder", failed_worker)
        self.assertIn("ensure_gpu_finalizer", failed_worker)
        self.assertNotIn("pkill", failed_worker)
        layout = shell_function_source("failed_repair_layout_json")
        self.assertIn('"target_batch": "batch0001"', layout)
        release = shell_function_source("release_failed_repair_holder")
        self.assertIn("--target-batch batch0001", release)
        self.assertNotIn('$BATCH_NAME', release)
        group_wait = shell_function_source("wait_for_failed_repair_group_worker")
        self.assertIn("TOTAL_ASSET_FAILED_GROUP_WAIT_SECONDS", group_wait)
        self.assertIn("if ! verify_failed_repair_holder_lock; then", group_wait)
        self.assertLess(
            group_wait.index("if ! verify_failed_repair_holder_lock; then"),
            group_wait.index('return "$runtime_result"'),
        )
        self.assertIn(
            'return 75',
            group_wait[
                group_wait.index("if ! verify_failed_repair_holder_lock; then"):
                group_wait.index('return "$runtime_result"')
            ],
        )
        group_run = shell_function_source("run_failed_repair_group")
        self.assertIn("attempted_failed", group_run)
        self.assertIn("review_required", group_run)
        self.assertIn("failed-repair-group-launch", group_run)
        self.assertIn("failed_repair_group_resume_action", group_run)
        self.assertIn("failed_repair_guarded_command_with_lock_retry", group_run)
        self.assertIn("adopting: exact failed-repair worker/finalizer", group_run)
        w0_chain = shell_function_source(
            "start_secondary_w0_formal_chain_after_audit"
        )
        self.assertIn("failed_repair_guarded_command_with_lock_retry", w0_chain)
        self.assertIn("formal-slot-chain batch0002", w0_chain)
        continuation = shell_function_source("start_batch0001_w0_continuation")
        self.assertIn("--runtime-aware-partition", continuation)
        self.assertIn("--drain-file '$drain_file'", continuation)
        self.assertIn("--resume-unprocessed-only", continuation)
        self.assertLess(
            continuation.index("--runtime-aware-partition"),
            continuation.index("--drain-file '$drain_file'"),
        )
        self.assertLess(
            continuation.index("--drain-file '$drain_file'"),
            continuation.index("--resume-unprocessed-only"),
        )
        self.assertIn("--runtime-state '$runtime'", continuation)
        self.assertIn("batch0001_failed_handoff_w0_continuation", continuation)
        self.assertIn("batch0001_w0_pending_report", continuation)
        self.assertIn("failed-repair-w0-audit-loop", continuation)
        helper = (PROJECT / "blender/scripts/total_asset_holder_transaction.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("pkill", helper)
        subprocess.run(
            ["bash", "-n", str(PIPELINE_SHELL)],
            check=True,
        )

    def test_failed_repair_lock_retry_is_exact_and_does_not_consume_busy_attempts(
        self,
    ) -> None:
        classifier = shell_function_source(
            "failed_repair_result_is_exact_scheduler_lock_busy"
        )
        retry = shell_function_source(
            "failed_repair_guarded_command_with_lock_retry"
        )
        script = (
            "set -euo pipefail\n"
            + classifier
            + retry
            + "\n"
            + "attempts=0\n"
            + "fake_guarded_launch() {\n"
            + "  attempts=$((attempts + 1))\n"
            + "  printf '%s\\n' \"$attempts\" >\"$ATTEMPT_FILE\"\n"
            + "  if [ \"$attempts\" -lt 3 ]; then\n"
            + "    echo 'scheduler lock busy: /tmp/exact.lock' >&2\n"
            + "    return 75\n"
            + "  fi\n"
            + "  echo 'exact launch succeeded'\n"
            + "}\n"
            + "sleep() { :; }\n"
            + "TOTAL_ASSET_FAILED_LAUNCH_RETRY_SECONDS=1 \\\n"
            + "TOTAL_ASSET_FAILED_LAUNCH_WAIT_SECONDS=10 \\\n"
            + "failed_repair_guarded_command_with_lock_retry \\\n"
            + "  'group=inferred_blank_timeout' fake_guarded_launch\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            attempt_file = Path(temp_dir) / "attempts"
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=PROJECT,
                env={**os.environ, "ATTEMPT_FILE": str(attempt_file)},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(attempt_file.read_text().strip(), "3")
        self.assertEqual(result.stdout.count("will retry attempt="), 2)
        self.assertIn("exact launch succeeded", result.stdout)

        exactness = (
            "set -euo pipefail\n"
            + classifier
            + "\n"
            + "failed_repair_result_is_exact_scheduler_lock_busy 75 '' "
              "'scheduler lock busy: /tmp/exact.lock'\n"
            + "if failed_repair_result_is_exact_scheduler_lock_busy 75 "
              "'unexpected stdout' 'scheduler lock busy: /tmp/exact.lock'; "
              "then exit 21; fi\n"
            + "if failed_repair_result_is_exact_scheduler_lock_busy 75 '' "
              "$'scheduler lock busy: /tmp/exact.lock\\nextra'; then exit 22; fi\n"
            + "if failed_repair_result_is_exact_scheduler_lock_busy 1 '' "
              "'scheduler lock busy: /tmp/exact.lock'; then exit 23; fi\n"
        )
        rejected = subprocess.run(
            ["bash", "-c", exactness],
            cwd=PROJECT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 0, rejected.stderr)

    def test_failed_repair_resume_adopts_only_exact_worker_and_finalizer(self) -> None:
        decision = shell_function_source("failed_repair_group_resume_action")
        script = (
            "set -euo pipefail\n"
            + "SECONDARY_PORT=31722\n"
            + decision
            + "\n"
            + "[ \"$(failed_repair_group_resume_action transport inactive 0)\" = launch ]\n"
            + "[ \"$(failed_repair_group_resume_action transport "
              "known_repair:total_asset_batch0001_failed_repair_transport_31722_g0 1)\" "
              "= adopt ]\n"
            + "if failed_repair_group_resume_action transport "
              "known_repair:total_asset_batch0001_failed_repair_scene_audit_31722_g0 1; "
              "then exit 31; fi\n"
            + "if failed_repair_group_resume_action transport "
              "known_repair:total_asset_batch0001_failed_repair_transport_31722_g0 0; "
              "then exit 32; fi\n"
            + "if failed_repair_group_resume_action transport active 1; "
              "then exit 33; fi\n"
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=PROJECT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_repair_slot_launcher_is_frozen_canonical_and_singleton(self) -> None:
        launcher = shell_function_source("start_failed_repair_slot")
        self.assertIn("failed_repair_execution_report", launcher)
        self.assertIn("total_asset_frozen_repair_adapter.py inspect", launcher)
        self.assertIn("static_fallback_contract|runtime_api_compat", launcher)
        self.assertIn("failed_repair_runtime_lane_report", launcher)
        self.assertIn("other_lane_pending", launcher)
        self.assertIn("failed_repair_slot_group_state", launcher)
        self.assertIn("formal_slot_local_state", launcher)
        self.assertIn("formal_slot_remote_occupancy_preflight", launcher)
        self.assertIn("begin_holder_transaction", launcher)
        self.assertIn("release_failed_repair_slot_holder", launcher)
        self.assertIn("formal_slot_runtime_preflight", launcher)
        self.assertIn("--failed-repair-group '$group'", launcher)
        self.assertIn(
            "--failed-repair-runtime-lane '$runtime_lane'", launcher
        )
        self.assertIn("--only-status failed --force", launcher)
        self.assertIn("--drain-file '$drain_file'", launcher)
        self.assertIn("wait_for_verified_worker", launcher)
        self.assertIn("ensure_gpu_finalizer", launcher)
        self.assertIn("mark_holder_slot_started", launcher)
        self.assertIn("commit_holder_transaction", launcher)
        self.assertIn("active_exact|finalizing_exact", launcher)
        self.assertIn(
            '"$batch" != "batch0000" ] && [ "$batch" != "batch0001"',
            launcher,
        )
        self.assertIn("--batch-index '$batch_index'", launcher)
        self.assertLess(
            launcher.index("failed_repair_execution_report"),
            launcher.index("begin_holder_transaction"),
        )
        self.assertLess(
            launcher.index("failed_repair_runtime_lane_report"),
            launcher.index("begin_holder_transaction"),
        )
        self.assertLess(
            launcher.index("failed_repair_slot_group_state"),
            launcher.index("begin_holder_transaction"),
        )
        self.assertNotIn("pkill", launcher)
        self.assertNotIn("kill", launcher)

        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        guard = shell[
            shell.index('case "$COMMAND" in'):
            shell.index("require_mount()")
        ]
        dispatch = shell[shell.rindex('case "$COMMAND" in'):]
        self.assertIn("failed-repair-slot", guard)
        self.assertIn("  failed-repair-slot)", dispatch)
        self.assertIn('"${6:-}"', dispatch[dispatch.index("  failed-repair-slot)"):])
        subprocess.run(["bash", "-n", str(PIPELINE_SHELL)], check=True)

    def test_failed_repair_slot_lane_report_counts_only_compatible_pending_rows(
        self,
    ) -> None:
        function = shell_function_source("failed_repair_runtime_lane_report")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy.blend"
            modern = root / "modern.blend"
            legacy.write_bytes(b"BLENDER-v300")
            modern.write_bytes(b"BLENDER-v410")
            queue = root / "repair.csv"
            queue.write_text(
                "asset_id,model_file,render_engine_hint\n"
                f"legacy,{legacy},source\n"
                f"modern,{modern},source\n",
                encoding="utf-8",
            )
            report = json.dumps({
                "groups": {
                    "transport": {
                        "pending_asset_ids": ["legacy", "modern"]
                    }
                }
            })
            script = (
                function
                + '\nfailed_repair_runtime_lane_report '
                '"$REPORT" transport "$QUEUE" "$GPU"\n'
            )
            for gpu, expected in (
                ("2", {"lane": "modern_vulkan", "pending": 1,
                       "other_lane_pending": 1}),
                ("0", {"lane": "legacy_gpu0", "pending": 1,
                       "other_lane_pending": 1}),
            ):
                result = subprocess.run(
                    ["bash", "-c", script],
                    cwd=PROJECT,
                    env={
                        **os.environ,
                        "REPORT": report,
                        "QUEUE": str(queue),
                        "GPU": gpu,
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_formal_slot_launcher_and_chain_are_guarded_and_resume_only(self) -> None:
        launcher = shell_function_source("start_formal_slot_worker")
        self.assertLess(
            launcher.index("formal_slot_gate_report"),
            launcher.index("begin_holder_transaction"),
        )
        self.assertLess(
            launcher.index("formal_slot_local_state"),
            launcher.index("release_quarantine_partial_holders"),
        )
        self.assertLess(
            launcher.index("formal_slot_remote_occupancy_preflight"),
            launcher.index("begin_holder_transaction"),
        )
        self.assertIn("formal_slot_runtime_preflight", launcher)
        self.assertIn("if ! formal_slot_remote_occupancy_preflight", launcher)
        self.assertIn("if ! begin_holder_transaction", launcher)
        self.assertIn("if ! release_quarantine_partial_holders", launcher)
        self.assertIn("if ! formal_slot_runtime_preflight", launcher)
        self.assertIn("if ! wait_for_verified_worker", launcher)
        self.assertIn("if ! ensure_gpu_finalizer", launcher)
        self.assertIn("if ! commit_holder_transaction", launcher)
        self.assertIn("--runtime-aware-partition", launcher)
        self.assertIn("--drain-file '$drain_file'", launcher)
        self.assertIn("--resume-unprocessed-only", launcher)
        self.assertLess(
            launcher.index("--runtime-aware-partition"),
            launcher.index("--drain-file '$drain_file'"),
        )
        self.assertLess(
            launcher.index("--drain-file '$drain_file'"),
            launcher.index("--resume-unprocessed-only"),
        )
        self.assertIn("ensure_gpu_finalizer", launcher)
        self.assertIn("mark_holder_slot_started", launcher)
        self.assertIn("commit_holder_transaction", launcher)
        self.assertIn("emit_formal_slot_launch_receipt", launcher)
        self.assertLess(
            launcher.index("commit_holder_transaction"),
            launcher.index("emit_formal_slot_launch_receipt"),
        )
        self.assertNotIn("--force", launcher)
        self.assertNotIn("pkill", launcher)

        loop = shell_function_source("formal_slot_chain_loop")
        self.assertIn("formal-slot-launch", loop)
        self.assertIn("validated_drain_request_id", loop)
        self.assertIn('"drained" "$target_batch"', loop)
        self.assertIn('runtime_status" != "complete', loop)
        self.assertIn("target_partition_complete", loop)
        self.assertIn("index=$((index + 1))", loop)
        self.assertIn("finalizer", loop)
        self.assertIn("chain will not advance", loop)
        self.assertIn("known_repair:", loop)
        self.assertIn("will hand off naturally", loop)
        self.assertIn('>"$launch_stdout_file" 2>"$launch_stderr_file"', loop)
        self.assertIn("formal_slot_launch_result_is_lock_busy", loop)
        self.assertIn(
            "formal_slot_launch_result_is_transient_ssh_timeout", loop
        )
        self.assertIn("scheduler launch lock is busy; exact slot launch will retry", loop)
        self.assertIn("verified transient SSH timeout", loop)
        self.assertIn("TOTAL_ASSET_SLOT_SSH_RETRY_MAX_SECONDS", loop)
        self.assertIn("ssh_retry_delay=$((ssh_retry_delay * 2))", loop)
        self.assertIn("TOTAL_ASSET_FORMAL_SLOT_LAUNCH_NONCE", loop)
        self.assertIn("formal_slot_verified_launch_receipt_token", loop)
        self.assertIn("launch receipt exists but exact worker re-verification failed", loop)
        self.assertIn("launch receipt exists but exact finalizer re-verification failed", loop)
        self.assertIn("formal_slot_local_state", loop)
        self.assertIn("finalizer_screen_state", loop)
        self.assertIn("ignoring only post-receipt rc=", loop)
        self.assertIn("returned success without a verified receipt", loop)
        self.assertIn("guarded slot launch failed", loop)

        starter = shell_function_source("start_formal_slot_chain")
        self.assertIn("total_asset_slot_chain_p", starter)
        self.assertIn("-u TOTAL_ASSET_SCHEDULER_LOCK_HELD", starter)
        self.assertIn("TOTAL_ASSET_SLOT_CHAIN_INTERNAL=1", starter)
        self.assertNotIn("pkill", starter)

        exact_preflight = shell_function_source(
            "formal_slot_remote_occupancy_preflight"
        )
        self.assertIn("--scope-launch-slots", exact_preflight)
        self.assertIn("total_asset_remote_preflight.py", exact_preflight)

        w0_audit = shell_function_source("failed_repair_w0_audit_loop")
        self.assertIn("start_secondary_w0_formal_chain_after_audit", w0_audit)
        w0_chain = shell_function_source(
            "start_secondary_w0_formal_chain_after_audit"
        )
        self.assertIn(
            'formal-slot-chain batch0002 "$SECONDARY_PORT" 0 0',
            w0_chain,
        )

        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        self.assertIn('export TOTAL_ASSET_SECONDARY_PORT="$SECONDARY_PORT"', shell)
        self.assertIn(
            'batch0001_deferred_vulkan_repair|$SECONDARY_PORT|1|1', shell
        )
        dispatch = shell[shell.rindex('case "$COMMAND" in'):]
        formal_dispatch = dispatch[
            dispatch.index("  formal-slot-launch)"):
            dispatch.index("  formal-slot-chain)")
        ]
        self.assertIn("if start_formal_slot_worker", formal_dispatch)
        self.assertIn("exit 0", formal_dispatch)
        self.assertIn('exit "$formal_launch_rc"', formal_dispatch)
        subprocess.run(["bash", "-n", str(PIPELINE_SHELL)], check=True)

    def test_verified_slot_launch_receipt_is_exact_and_ignores_unrelated_output(
        self,
    ) -> None:
        shell = PIPELINE_SHELL.read_text(encoding="utf-8")
        function = shell[
            shell.index("formal_slot_verified_launch_receipt_token() {"):
            shell.index("formal_slot_local_state() {")
        ]
        token = "total_asset_gpu_finalizer_v2_" + ("a" * 64)
        receipt = {
            "schema": "video2blender.formal-slot-launch-receipt.v1",
            "nonce": "b" * 32,
            "batch": "batch0004",
            "remote_port": 31722,
            "gpu": 2,
            "worker_index": 2,
            "worker_count": 11,
            "runtime_path": "/inventory/worker_runtime/formal.json",
            "worker_screen": "total_asset_batch0004_31722_g2",
            "finalizer_screen": "total_asset_batch0004_31722_g2_finalizer",
            "finalizer_token": token,
        }

        def run(payload: str) -> subprocess.CompletedProcess[str]:
            script = (
                "set -euo pipefail\n"
                + function
                + "\nformal_slot_verified_launch_receipt_token "
                + "\"$SLOT_STDOUT\" "
                + "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb batch0004 "
                + "31722 2 2 /inventory/worker_runtime/formal.json\n"
            )
            return subprocess.run(
                ["bash", "-c", script],
                cwd=PROJECT,
                env={**os.environ, "SLOT_STDOUT": payload},
                text=True,
                capture_output=True,
                check=False,
            )

        unrelated_gate = json.dumps({
            "ready": False,
            "target_batch": "batch0000",
            "blockers": ["drain_requested"],
        }, separators=(",", ":"))
        output = "\n".join((
            "preflight: exact slot inactive",
            json.dumps(receipt, separators=(",", ":")),
            "started: formal slot 2/11 batch=batch0004",
            unrelated_gate,
        ))
        result = run(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), token)

        mutations = []
        wrong_nonce = dict(receipt, nonce="c" * 32)
        mutations.append(json.dumps(wrong_nonce, separators=(",", ":")))
        missing = dict(receipt)
        missing.pop("worker_screen")
        mutations.append(json.dumps(missing, separators=(",", ":")))
        extra = dict(receipt, unexpected=True)
        mutations.append(json.dumps(extra, separators=(",", ":")))
        bad_token = dict(receipt, finalizer_token="not-an-attestation")
        mutations.append(json.dumps(bad_token, separators=(",", ":")))
        duplicate = json.dumps(receipt, separators=(",", ":"))
        mutations.append(duplicate + "\n" + duplicate)
        for payload in mutations:
            with self.subTest(payload=payload):
                rejected = run(payload)
                self.assertEqual(rejected.returncode, 75, rejected.stderr)

    def test_formal_slot_local_state_here_doc_executes(self) -> None:
        function = shell_function_source("formal_slot_local_state")
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    "set -euo pipefail\n"
                    + function
                    + "\nformal_slot_local_state batch9999 65534 99 0\n"
                ),
            ],
            cwd=PROJECT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "inactive")

    def test_slot_launch_lock_retry_classifier_is_exact_and_fail_closed(self) -> None:
        function = shell_function_source(
            "formal_slot_launch_result_is_lock_busy"
        )
        script = (
            "set -euo pipefail\n"
            + function
            + "\n"
            + "formal_slot_launch_result_is_lock_busy 75 '' "
              "'scheduler lock busy: /tmp/scheduler.lock'\n"
            + "if formal_slot_launch_result_is_lock_busy 75 'preflight output' "
              "'scheduler lock busy: /tmp/scheduler.lock'; then exit 21; fi\n"
            + "if formal_slot_launch_result_is_lock_busy 75 '' "
              "$'scheduler lock busy: /tmp/scheduler.lock\\nblocked: ownership'; "
              "then exit 22; fi\n"
            + "if formal_slot_launch_result_is_lock_busy 75 '' "
              "'blocked: remote preflight failed'; then exit 23; fi\n"
            + "if formal_slot_launch_result_is_lock_busy 1 '' "
              "'scheduler lock busy: /tmp/scheduler.lock'; then exit 24; fi\n"
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=PROJECT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slot_launch_ssh_timeout_classifier_retries_only_exact_timeouts(self) -> None:
        function = shell_function_source(
            "formal_slot_launch_result_is_transient_ssh_timeout"
        )
        probe_timeout = json.dumps({
            "ready": False,
            "error_codes": ["ssh_timeout"],
            "output_written": True,
        }, separators=(",", ":"))
        hard_probe = json.dumps({
            "ready": False,
            "error_codes": ["holder_present"],
            "output_written": True,
        }, separators=(",", ":"))
        script = (
            "set -euo pipefail\n"
            + function
            + "\n"
            + "formal_slot_launch_result_is_transient_ssh_timeout 75 "
              + repr(probe_timeout)
              + " ''\n"
            + "formal_slot_launch_result_is_transient_ssh_timeout 1 '' "
              "$'Traceback (most recent call last):\\n"
              "subprocess.TimeoutExpired: command timed out\\n"
              "total_asset_worker.RemoteTransportError: worker_ssh_timeout'\n"
            + "formal_slot_launch_result_is_transient_ssh_timeout 1 '' "
              "$'total_asset_worker.RemoteTransportError: exitstatus=255 worker_ssh_failed\\n"
              "ssh: connect to host 10.26.6.88 port 31722: Connection timed out'\n"
            + "if formal_slot_launch_result_is_transient_ssh_timeout 75 "
              + repr(hard_probe)
              + " ''; then exit 31; fi\n"
            + "if formal_slot_launch_result_is_transient_ssh_timeout 1 '' "
              "'RuntimeError: secondary GPU Vulkan routing failed'; then exit 32; fi\n"
            + "if formal_slot_launch_result_is_transient_ssh_timeout 1 '' "
              "'RemoteTransportError: worker_ssh_transport_unavailable'; "
              "then exit 33; fi\n"
            + "if formal_slot_launch_result_is_transient_ssh_timeout 75 '' "
              "'blocked: physical slot ownership is unknown'; then exit 34; fi\n"
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=PROJECT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
