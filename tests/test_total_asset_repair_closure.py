from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock
import zlib


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_batch_failed_repair_manifests as policy
import total_asset_repair_closure as closure


class TotalAssetRepairClosureTests(unittest.TestCase):
    @staticmethod
    def png_bytes() -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk("IHDR".encode(), struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk("IDAT".encode(), zlib.compress(b"\x00\x00\x00\x00"))
            + chunk("IEND".encode(), b"")
        )

    @staticmethod
    def mp4_bytes() -> bytes:
        def box(kind: bytes, payload: bytes) -> bytes:
            return struct.pack(">I4s", len(payload) + 8, kind) + payload

        mvhd = bytearray(100)
        struct.pack_into(">II", mvhd, 12, 1000, 1000)
        sample_table = box(b"stsd", b"\x00\x00\x00\x00")
        track = box(
            b"trak",
            box(b"mdia", box(b"minf", box(b"stbl", sample_table))),
        )
        return (
            box(b"ftyp", b"isom\x00\x00\x00\x00isom")
            + box(b"moov", box(b"mvhd", bytes(mvhd)) + track)
            + box(b"mdat", b"\x00" * 1024)
        )

    @staticmethod
    def seed_closed_attempt(
        inventory: Path,
        manifest: dict[str, object],
        key: str,
        asset_id: str,
    ) -> str:
        attempt_id = "attempt-" + "a" * 32
        metadata = manifest["groups"][key]
        state = closure._load_state(inventory)
        state_key = closure._state_key(str(manifest["generation"]), key)
        state["attempts"][state_key] = 1
        state["launches"][attempt_id] = {
            "generation": manifest["generation"],
            "group_key": key,
            "attempt_id": attempt_id,
            "status": "closed",
            "receipt_schema": closure.LAUNCH_RECEIPT_SCHEMA,
            "queue_sha256": metadata["queue_sha256"],
            "label": metadata["label"],
            "asset_ids": [asset_id],
            "remote_port": 30773,
            "gpu": 1,
            "worker_index": 5,
            "worker_count": 11,
            "exit_code": 0,
        }
        state["asset_attempts"][state_key] = {
            asset_id: {
                "count": 1,
                "attempt_id": attempt_id,
                "expected_status_signature": "test",
                "source_stat_signature": "test",
            }
        }
        state["global_asset_attempts"][asset_id] = {
            "count": 1,
            "attempt_id": attempt_id,
            "generation": manifest["generation"],
            "group_key": key,
            "claimed_at": "2026-07-20 01:30:00",
            "expected_status_signature": "test",
            "source_stat_signature": "test",
        }
        closure._write_state(inventory, state)
        return attempt_id

    @staticmethod
    def write_transport_runtime(
        inventory: Path,
        manifest: dict[str, object],
        key: str,
        attempt_id: str,
        *,
        attempted: int = 0,
        failure_category: str = "remote_transport",
        failure_code: str = "worker_ssh_failed",
        transport_returncode: int | None = 255,
        error: str = (
            "primary_render: exitstatus=255 worker_ssh_failed\n"
            "Connection reset by peer\nclient_loop: send disconnect: Broken pipe"
        ),
        identity_overrides: dict[str, object] | None = None,
    ) -> Path:
        metadata = manifest["groups"][key]
        state = closure._load_state(inventory)
        launch = state["launches"][attempt_id]
        generation = str(manifest["generation"])
        runtime = inventory / "worker_runtime" / (
            f"{metadata['label']}_p{launch['remote_port']}_g{launch['gpu']}_"
            f"w{launch['worker_index']}of{launch['worker_count']}.json"
        )
        runtime.parent.mkdir(parents=True, exist_ok=True)
        queue = (
            closure._generation_dir(inventory, generation)
            / str(metadata["queue_file"])
        )
        payload: dict[str, object] = {
            "schema_version": 1,
            "status": "failed",
            "state": "failed",
            "batch": metadata["label"],
            "remote_port": launch["remote_port"],
            "gpu": launch["gpu"],
            "worker_index": launch["worker_index"],
            "worker_count": launch["worker_count"],
            "queue": str(queue),
            "repair_generation": generation,
            "repair_group_key": key,
            "repair_attempt_id": attempt_id,
            "repair_queue_sha256": metadata["queue_sha256"],
            "attempted": attempted,
            "failure_stage": "primary_render",
            "failure_category": failure_category,
            "failure_code": failure_code,
            "error": error,
        }
        if transport_returncode is not None:
            payload["transport_returncode"] = transport_returncode
        payload.update(identity_overrides or {})
        runtime.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return runtime

    def fixture(self, root: Path) -> tuple[Path, Path, Path, dict[str, object]]:
        inventory = root / "inventory"
        inventory.mkdir()
        model = root / "source.blend"
        model.write_bytes(b"BLENDER-v300-source")
        catalog = inventory / "total_asset_catalog.csv"
        fields = (
            "asset_id",
            "identity_key",
            "model_file",
            "source_root",
            "render_order",
            "render_batch",
            "render_engine_hint",
        )
        with catalog.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({
                "asset_id": "asset-1",
                "identity_key": "identity-1",
                "model_file": str(model),
                "source_root": str(root),
                "render_order": "2001",
                "render_batch": "0002",
                "render_engine_hint": "BLENDER_EEVEE_NEXT",
            })
        status: dict[str, object] = {
            "asset_id": "asset-1",
            "identity_key": "identity-1",
            "render_order": "2001",
            "render_batch": "0002",
            "batch": "batch0002",
            "status": "failed",
            "failure_category": "blender_version_or_api",
            "updated_at": "2026-07-20 01:00:00",
        }
        return catalog, inventory, model, status

    def build(self, catalog: Path, inventory: Path, status: dict[str, object]):
        scheduler = SimpleNamespace(malformed_status_lines=0, topology_errors=())
        with (
            mock.patch.object(closure, "derive_plan", return_value=scheduler),
            mock.patch.object(
                closure, "load_effective_statuses", return_value={"asset-1": status}
            ),
            mock.patch.object(
                closure.repair_policy,
                "classify_repair_group",
                return_value=(policy.RUNTIME_API, ("runtime_or_api_signature",)),
            ),
            mock.patch.object(
                closure,
                "_runtime_lane",
                return_value=("modern_vulkan", "4.5.0", "4.5"),
            ),
        ):
            return closure.build_plan(catalog=catalog, inventory=inventory)

    def test_any_formal_batch_can_produce_reviewed_automatic_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog, inventory, _model, status = self.fixture(Path(temporary))
            plan = self.build(catalog, inventory, status)
            key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            self.assertEqual(plan["automatic_asset_count"], 1)
            self.assertEqual(plan["manual_asset_count"], 0)
            self.assertRegex(
                plan["groups"][key]["label"],
                r"^batch0002_failed_repair_runtime_api_compat_g[0-9a-f]{24}$",
            )
            self.assertEqual(plan["groups"][key]["max_attempts"], 1)

    def test_prior_repair_attempt_is_never_automatically_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog, inventory, _model, status = self.fixture(Path(temporary))
            status["batch"] = "batch0002_failed_repair_runtime_api_compat"
            plan = self.build(catalog, inventory, status)
            self.assertEqual(plan["automatic_asset_count"], 0)
            self.assertEqual(plan["manual_asset_count"], 1)

    def test_committed_generation_is_hash_verified_and_slot_lane_is_hard_gated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog, inventory, _model, status = self.fixture(Path(temporary))
            plan = self.build(catalog, inventory, status)
            manifest = closure.commit_generation(plan, inventory)
            key = next(iter(manifest["groups"]))
            modern_slot = next(
                (port, gpu, worker)
                for (port, gpu), worker in
                closure.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
                if gpu != 0
            )
            with mock.patch.object(
                closure, "load_effective_statuses", return_value={"asset-1": status}
            ):
                report = closure.prepare_launch(
                    catalog=catalog,
                    inventory=inventory,
                    generation=manifest["generation"],
                    group_key=key,
                    remote_port=modern_slot[0],
                    gpu=modern_slot[1],
                    worker_index=modern_slot[2],
                )
                self.assertEqual(report["pending"], 1)
                gpu0_slot = next(
                    (port, gpu, worker)
                    for (port, gpu), worker in
                    closure.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
                    if gpu == 0
                )
                with self.assertRaisesRegex(
                    closure.RepairClosureError, "incompatible"
                ):
                    closure.prepare_launch(
                        catalog=catalog,
                        inventory=inventory,
                        generation=manifest["generation"],
                        group_key=key,
                        remote_port=gpu0_slot[0],
                        gpu=gpu0_slot[1],
                        worker_index=gpu0_slot[2],
                    )
            queue = (
                closure._generation_dir(inventory, manifest["generation"])
                / manifest["groups"][key]["queue_file"]
            )
            queue.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(
                closure.RepairClosureError, "queue hash mismatch"
            ):
                closure.load_generation(inventory, manifest["generation"])

    def test_failed_guarded_launch_does_not_consume_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog, inventory, _model, status = self.fixture(Path(temporary))
            manifest = closure.commit_generation(
                self.build(catalog, inventory, status), inventory
            )
            key = next(iter(manifest["groups"]))
            port, gpu, worker = next(
                (port, gpu, worker)
                for (port, gpu), worker in
                closure.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
                if gpu != 0
            )
            with (
                mock.patch.object(
                    closure, "load_effective_statuses", return_value={"asset-1": status}
                ),
                mock.patch.object(
                    closure.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 75, "", "lock busy"),
                ),
            ):
                result = closure.run_group(
                    project=PROJECT,
                    catalog=catalog,
                    inventory=inventory,
                    generation=manifest["generation"],
                    group_key=key,
                    remote_port=port,
                    gpu=gpu,
                    worker_index=worker,
                )
            self.assertFalse(result["started"])
            state = closure._load_state(inventory)
            self.assertEqual(state["attempts"], {})

    def test_worker_exit_before_confirmation_aborts_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory"
            nonce = "N" * 48
            generation = "repair-" + "1" * 24
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            attempt_id = "attempt-" + "a" * 32
            state = closure._load_state(inventory)
            state["launches"][attempt_id] = {
                "generation": generation,
                "group_key": group_key,
                "attempt_id": attempt_id,
                "status": "reserved",
                "nonce_sha256": closure._nonce_hash(nonce),
            }
            closure._write_state(inventory, state)

            receipt = closure.close_launch(
                inventory=inventory,
                generation=generation,
                group_key=group_key,
                attempt_id=attempt_id,
                nonce=nonce,
                exit_code=75,
            )
            self.assertEqual(receipt["status"], "aborted")
            self.assertEqual(receipt["exit_code"], 75)
            with self.assertRaisesRegex(
                closure.RepairClosureError, "cannot be confirmed"
            ):
                closure.confirm_launch(
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                    nonce=nonce,
                )

    def test_confirmed_launch_heartbeat_renews_bounded_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory"
            nonce = "N" * 48
            generation = "repair-" + "1" * 24
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            attempt_id = "attempt-" + "a" * 32
            state = closure._load_state(inventory)
            state["launches"][attempt_id] = {
                "generation": generation,
                "group_key": group_key,
                "attempt_id": attempt_id,
                "status": "reserved",
                "nonce_sha256": closure._nonce_hash(nonce),
            }
            closure._write_state(inventory, state)
            with mock.patch.object(closure.time, "time", return_value=1000.0):
                closure.confirm_launch(
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                    nonce=nonce,
                )
            with mock.patch.object(closure.time, "time", return_value=1100.0):
                receipt = closure.heartbeat_launch(
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                    nonce=nonce,
                )
            self.assertEqual(
                receipt["lease_expires_at_epoch"],
                1100.0 + closure.LAUNCH_LEASE_SECONDS,
            )

    def test_expired_orphan_closes_only_with_fresh_exact_idle_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            generation = "repair-" + "1" * 24
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            attempt_id = "attempt-" + "a" * 32
            port, gpu, worker_index = next(
                (port, gpu, worker_index)
                for (port, gpu), worker_index in
                closure.CANONICAL_WORKER_INDEX_BY_LOCATION.items()
                if gpu != 0
            )
            state = closure._load_state(inventory)
            state["launches"][attempt_id] = {
                "generation": generation,
                "group_key": group_key,
                "attempt_id": attempt_id,
                "status": "confirmed",
                "nonce_sha256": closure._nonce_hash("N" * 48),
                "receipt_schema": closure.LAUNCH_RECEIPT_SCHEMA,
                "lease_expires_at_epoch": 100.0,
                "remote_port": port,
                "gpu": gpu,
                "worker_index": worker_index,
                "worker_count": 11,
                "asset_ids": [],
            }
            closure._write_state(inventory, state)
            preflight = root / "preflight.json"
            preflight.write_text(json.dumps({
                "schema_version": closure.REMOTE_PREFLIGHT_SCHEMA_VERSION,
                "ready": True,
                "scope": "launch_slots",
                "target_batch": "batch0002",
                "worker_count": 11,
                "observed_at_epoch": 950.0,
                "launch_layout": [{
                    "port": port,
                    "gpu": gpu,
                    "worker_index": worker_index,
                }],
                "local_claim_count": 0,
                "local_controller_count": 0,
                "nodes": [{
                    "port": port,
                    "reachable": True,
                    "gpus": [{
                        "gpu": gpu,
                        "gpu_ok": True,
                        "physical_binding_ok": True,
                    }],
                }],
            }) + "\n", encoding="utf-8")
            with mock.patch.object(closure.time, "time", return_value=1000.0):
                result = closure.reconcile_orphaned_launch(
                    catalog=root / "catalog.csv",
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                    remote_preflight_json=preflight,
                )
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["exit_code"], 78)
            state = closure._load_state(inventory)
            self.assertEqual(
                state["launches"][attempt_id]["close_reason"],
                "orphaned_controller_remote_slot_proven_idle",
            )

    def test_blocked_launch_releases_only_claims_without_a_durable_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            nonce = "N" * 48
            generation = "repair-" + "1" * 24
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            attempt_id = "attempt-" + "a" * 32
            state_key = closure._state_key(generation, group_key)
            state = closure._load_state(inventory)
            state["launches"][attempt_id] = {
                "generation": generation,
                "group_key": group_key,
                "attempt_id": attempt_id,
                "status": "confirmed",
                "nonce_sha256": closure._nonce_hash(nonce),
                "asset_ids": ["landed", "lock-raced"],
            }
            state["asset_attempts"][state_key] = {
                asset_id: {
                    "count": 1,
                    "attempt_id": attempt_id,
                    "expected_status_signature": "e" * 64,
                    "source_stat_signature": "s" * 64,
                }
                for asset_id in ("landed", "lock-raced")
            }
            for asset_id in ("landed", "lock-raced"):
                state["global_asset_attempts"][asset_id] = {
                    "count": 1,
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "group_key": group_key,
                }
            closure._write_state(inventory, state)

            with (
                mock.patch.object(
                    closure,
                    "load_effective_statuses",
                    return_value={
                        "landed": {
                            "status": "accepted",
                            "repair_attempt_id": attempt_id,
                        },
                        "lock-raced": {"status": "failed"},
                    },
                ),
                mock.patch.object(
                    closure, "_status_input_snapshot", return_value=(("stable",),)
                ),
            ):
                receipt = closure.close_launch(
                    catalog=root / "catalog.csv",
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                    nonce=nonce,
                    exit_code=75,
                )

            self.assertEqual(receipt["released_unlanded_claims"], ["lock-raced"])
            state = closure._load_state(inventory)
            self.assertIn("landed", state["asset_attempts"][state_key])
            self.assertIn("landed", state["global_asset_attempts"])
            self.assertNotIn("lock-raced", state["asset_attempts"][state_key])
            self.assertNotIn("lock-raced", state["global_asset_attempts"])
            launch = state["launches"][attempt_id]
            self.assertEqual(launch["claim_release_reason"], "blocked_gpu_busy")
            with (
                mock.patch.object(
                    closure,
                    "load_effective_statuses",
                    return_value={
                        "landed": {
                            "status": "accepted",
                            "repair_attempt_id": attempt_id,
                        }
                    },
                ),
                mock.patch.object(
                    closure, "_status_input_snapshot", return_value=(("stable",),)
                ),
            ):
                reconciled = closure.reconcile_blocked_claims(
                    catalog=root / "catalog.csv",
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                )
            self.assertEqual(reconciled["released_unlanded_claims"], [])

    def test_failed_launch_keeps_unlanded_claim_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory"
            nonce = "N" * 48
            generation = "repair-" + "1" * 24
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            attempt_id = "attempt-" + "a" * 32
            state_key = closure._state_key(generation, group_key)
            state = closure._load_state(inventory)
            state["launches"][attempt_id] = {
                "generation": generation,
                "group_key": group_key,
                "attempt_id": attempt_id,
                "status": "confirmed",
                "nonce_sha256": closure._nonce_hash(nonce),
                "asset_ids": ["asset-1"],
            }
            state["asset_attempts"][state_key] = {
                "asset-1": {"count": 1, "attempt_id": attempt_id},
            }
            state["global_asset_attempts"]["asset-1"] = {
                "count": 1,
                "attempt_id": attempt_id,
                "generation": generation,
                "group_key": group_key,
            }
            closure._write_state(inventory, state)
            closure.close_launch(
                inventory=inventory,
                generation=generation,
                group_key=group_key,
                attempt_id=attempt_id,
                nonce=nonce,
                exit_code=1,
            )
            state = closure._load_state(inventory)
            self.assertIn("asset-1", state["global_asset_attempts"])

    def test_zero_attempt_network_disconnect_can_be_safely_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, inventory, _model, status = self.fixture(root)
            manifest = closure.commit_generation(
                self.build(catalog, inventory, status), inventory
            )
            group_key = next(iter(manifest["groups"]))
            attempt_id = self.seed_closed_attempt(
                inventory, manifest, group_key, "asset-1"
            )
            state = closure._load_state(inventory)
            state["launches"][attempt_id]["exit_code"] = 1
            closure._write_state(inventory, state)
            runtime = self.write_transport_runtime(
                inventory, manifest, group_key, attempt_id
            )

            with mock.patch.object(
                closure,
                "load_effective_statuses",
                return_value={"asset-1": status},
            ):
                result = closure.reconcile_blocked_claims(
                    catalog=catalog,
                    inventory=inventory,
                    generation=str(manifest["generation"]),
                    group_key=group_key,
                    attempt_id=attempt_id,
                )

            self.assertEqual(result["released_unlanded_claims"], ["asset-1"])
            state = closure._load_state(inventory)
            state_key = closure._state_key(str(manifest["generation"]), group_key)
            self.assertNotIn("asset-1", state["asset_attempts"][state_key])
            self.assertNotIn("asset-1", state["global_asset_attempts"])
            launch = state["launches"][attempt_id]
            self.assertEqual(
                launch["claim_release_reason"],
                closure.TRANSPORT_CLAIM_RELEASE_REASON,
            )
            self.assertEqual(launch["transport_runtime_path"], str(runtime))
            self.assertRegex(launch["transport_runtime_sha256"], r"^[0-9a-f]{64}$")

    def test_timeout_without_remote_exit_is_an_allowlisted_transport_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, inventory, _model, status = self.fixture(root)
            manifest = closure.commit_generation(
                self.build(catalog, inventory, status), inventory
            )
            group_key = next(iter(manifest["groups"]))
            attempt_id = self.seed_closed_attempt(
                inventory, manifest, group_key, "asset-1"
            )
            state = closure._load_state(inventory)
            state["launches"][attempt_id]["exit_code"] = 1
            closure._write_state(inventory, state)
            self.write_transport_runtime(
                inventory,
                manifest,
                group_key,
                attempt_id,
                failure_code="worker_ssh_timeout",
                transport_returncode=None,
                error="worker_initialization: worker_ssh_timeout",
            )
            with mock.patch.object(
                closure,
                "load_effective_statuses",
                return_value={"asset-1": status},
            ):
                result = closure.reconcile_blocked_claims(
                    catalog=catalog,
                    inventory=inventory,
                    generation=str(manifest["generation"]),
                    group_key=group_key,
                    attempt_id=attempt_id,
                )
            self.assertEqual(result["released_unlanded_claims"], ["asset-1"])

    def test_transport_reconcile_rejects_ambiguous_or_unsafe_runtime(self) -> None:
        cases = {
            "attempted asset": {"attempted": 1},
            "authentication failure": {
                "error": (
                    "primary_render: exitstatus=255 worker_ssh_failed\n"
                    "Permission denied (publickey). Connection reset by peer"
                ),
            },
            "runtime identity mismatch": {
                "identity_overrides": {
                    "repair_attempt_id": "attempt-" + "b" * 32,
                },
            },
            "remote blender failure": {
                "error": (
                    "primary_render: exitstatus=255 worker_ssh_failed\n"
                    "Blender exited after a segmentation fault"
                ),
            },
            "wrong failure category": {
                "failure_category": "remote_render",
            },
        }
        for name, overrides in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                catalog, inventory, _model, status = self.fixture(root)
                manifest = closure.commit_generation(
                    self.build(catalog, inventory, status), inventory
                )
                group_key = next(iter(manifest["groups"]))
                attempt_id = self.seed_closed_attempt(
                    inventory, manifest, group_key, "asset-1"
                )
                state = closure._load_state(inventory)
                state["launches"][attempt_id]["exit_code"] = 1
                closure._write_state(inventory, state)
                self.write_transport_runtime(
                    inventory,
                    manifest,
                    group_key,
                    attempt_id,
                    **overrides,
                )
                with self.assertRaises(closure.RepairClosureError):
                    closure.reconcile_blocked_claims(
                        catalog=catalog,
                        inventory=inventory,
                        generation=str(manifest["generation"]),
                        group_key=group_key,
                        attempt_id=attempt_id,
                    )
                state = closure._load_state(inventory)
                self.assertIn("asset-1", state["global_asset_attempts"])

    def test_cooperative_drain_releases_only_exact_unlanded_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            inventory.mkdir()
            catalog = root / "catalog.csv"
            catalog.write_text("asset_id\n", encoding="utf-8")
            nonce = "N" * 48
            generation = "repair-" + "1" * 24
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            attempt_id = "attempt-" + "a" * 32
            other_attempt_id = "attempt-" + "b" * 32
            state_key = closure._state_key(generation, group_key)
            claimed = ("landed", "unlanded", "other-attempt-row")
            state = closure._load_state(inventory)
            state["launches"][attempt_id] = {
                "generation": generation,
                "group_key": group_key,
                "attempt_id": attempt_id,
                "status": "confirmed",
                "nonce_sha256": closure._nonce_hash(nonce),
                "asset_ids": list(claimed),
            }
            state["asset_attempts"][state_key] = {
                asset_id: {"count": 1, "attempt_id": attempt_id}
                for asset_id in claimed
            }
            for asset_id in claimed:
                state["global_asset_attempts"][asset_id] = {
                    "count": 1,
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "group_key": group_key,
                }
            closure._write_state(inventory, state)

            status_path = (
                inventory / "total_asset_render_status_batch0002_repair.jsonl"
            )
            status_path.write_text(
                "\n".join([
                    json.dumps({
                        "asset_id": "landed",
                        "identity_key": "identity-landed",
                        "render_order": "2001",
                        "render_batch": "0002",
                        "batch": "batch0002_failed_repair_runtime_api_compat_g"
                        + "1" * 24,
                        "status": "needs_review",
                        "repair_attempt_id": attempt_id,
                    }),
                    json.dumps({
                        "asset_id": "other-attempt-row",
                        "identity_key": "identity-other",
                        "render_order": "2003",
                        "render_batch": "0002",
                        "batch": "batch0002_failed_repair_runtime_api_compat_g"
                        + "1" * 24,
                        "status": "needs_review",
                        "repair_attempt_id": other_attempt_id,
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            original_status = status_path.read_bytes()
            effective = {
                "landed": {
                    "asset_id": "landed",
                    "identity_key": "identity-landed",
                    "render_order": "2001",
                    "render_batch": "0002",
                    "batch": "batch0002_quality_audit",
                    "status": "needs_review",
                },
                "unlanded": {
                    "asset_id": "unlanded",
                    "identity_key": "identity-unlanded",
                    "render_order": "2002",
                    "render_batch": "0002",
                    "batch": "batch0002_quality_audit",
                    "status": "failed",
                },
                "other-attempt-row": {
                    "asset_id": "other-attempt-row",
                    "identity_key": "identity-other",
                    "render_order": "2003",
                    "render_batch": "0002",
                    "batch": "batch0002_quality_audit",
                    "status": "failed",
                },
            }
            with mock.patch.object(
                closure, "load_effective_statuses", return_value=effective
            ):
                receipt = closure.close_launch(
                    catalog=catalog,
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                    nonce=nonce,
                    exit_code=closure.render_worker.COOPERATIVE_DRAIN_EXIT,
                )

            self.assertEqual(
                receipt["released_unlanded_claims"],
                ["other-attempt-row", "unlanded"],
            )
            state = closure._load_state(inventory)
            self.assertIn("landed", state["asset_attempts"][state_key])
            self.assertIn("landed", state["global_asset_attempts"])
            for asset_id in ("unlanded", "other-attempt-row"):
                self.assertNotIn(asset_id, state["asset_attempts"][state_key])
                self.assertNotIn(asset_id, state["global_asset_attempts"])
            self.assertEqual(
                state["launches"][attempt_id]["claim_release_reason"],
                "cooperative_drain",
            )
            self.assertEqual(status_path.read_bytes(), original_status)
            self.assertNotIn(b'"status": "failed"', original_status.splitlines()[0])

            with mock.patch.object(
                closure, "load_effective_statuses", return_value=effective
            ):
                reconciled = closure.reconcile_blocked_claims(
                    catalog=catalog,
                    inventory=inventory,
                    generation=generation,
                    group_key=group_key,
                    attempt_id=attempt_id,
                )
            self.assertEqual(reconciled["released_unlanded_claims"], [])

    def test_superseded_attempt_row_remains_durable_claim_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory"
            inventory.mkdir()
            attempt_id = "attempt-" + "a" * 32
            status_path = inventory / "total_asset_render_status_batch0002_repair.jsonl"
            status_path.write_text(
                json.dumps({
                    "asset_id": "asset-1",
                    "identity_key": "identity-1",
                    "render_order": "2001",
                    "render_batch": "0002",
                    "batch": "batch0002_failed_repair_runtime_api_compat_g"
                    + "1" * 24,
                    "status": "needs_review",
                    "repair_attempt_id": attempt_id,
                })
                + "\n",
                encoding="utf-8",
            )
            effective = {
                "asset-1": {
                    "asset_id": "asset-1",
                    "identity_key": "identity-1",
                    "render_order": "2001",
                    "render_batch": "0002",
                    "batch": "batch0002_quality_audit",
                    "status": "needs_review",
                }
            }
            self.assertEqual(
                closure._durable_attempt_asset_ids(
                    inventory,
                    batch="batch0002",
                    attempt_id=attempt_id,
                    asset_ids=["asset-1"],
                    effective=effective,
                ),
                {"asset-1"},
            )

    def test_global_asset_claim_is_atomic_across_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            model = root / "source.blend"
            model.write_bytes(b"BLENDER-v300-source")
            source_stat = model.stat()
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            generations = (
                "repair-" + "1" * 24,
                "repair-" + "2" * 24,
            )
            attempts = (
                "attempt-" + "a" * 32,
                "attempt-" + "b" * 32,
            )
            nonces = ("N" * 48, "M" * 48)
            row = {
                "asset_id": "asset-1",
                "model_file": str(model),
                "source_size": str(source_stat.st_size),
                "source_mtime_ns": str(source_stat.st_mtime_ns),
                "expected_status_signature": "e" * 64,
                "source_stat_signature": "s" * 64,
            }

            def manifest(generation: str) -> dict[str, object]:
                return {
                    "generation": generation,
                    "groups": {
                        group_key: {
                            "queue_file": "queue.csv",
                            "queue_sha256": "q" * 64,
                            "label": (
                                "batch0002_failed_repair_runtime_api_compat_g"
                                + generation.removeprefix("repair-")
                            ),
                            "max_attempts": 1,
                        }
                    },
                }

            state = closure._load_state(inventory)
            for generation, attempt_id, nonce in zip(
                generations, attempts, nonces
            ):
                state["launches"][attempt_id] = {
                    "generation": generation,
                    "group_key": group_key,
                    "attempt_id": attempt_id,
                    "status": "confirmed",
                    "nonce_sha256": closure._nonce_hash(nonce),
                    "asset_ids": ["asset-1"],
                }
            closure._write_state(inventory, state)

            def claim(index: int) -> str:
                try:
                    return str(closure.claim_asset(
                        catalog=root / "catalog.csv",
                        inventory=inventory,
                        generation=generations[index],
                        group_key=group_key,
                        attempt_id=attempts[index],
                        nonce=nonces[index],
                        asset_id="asset-1",
                    )["claim"])
                except closure.RepairClosureError as exc:
                    if str(exc) == "repair closure lock busy":
                        return "blocked"
                    raise

            with (
                mock.patch.object(
                    closure, "load_generation", side_effect=lambda _i, g: manifest(g)
                ),
                mock.patch.object(closure, "_read_queue", return_value=[row]),
                mock.patch.object(
                    closure, "load_effective_statuses", return_value={"asset-1": {}}
                ),
                mock.patch.object(
                    closure.render_worker,
                    "repair_closure_row_state",
                    return_value="pending",
                ),
                mock.patch.object(
                    closure, "_status_input_snapshot", return_value=(("stable",),)
                ) as status_snapshot,
                mock.patch.object(
                    closure, "_queue_stat_snapshot", return_value=(1, 2, 3, 4)
                ),
            ):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    decisions = list(pool.map(claim, (0, 1)))
                self.assertEqual(decisions.count("claimed"), 1)
                self.assertIn(
                    decisions[1 - decisions.index("claimed")], {"skip", "blocked"}
                )
                if "blocked" in decisions:
                    loser = decisions.index("blocked")
                    self.assertEqual(claim(loser), "skip")

            state = closure._load_state(inventory)
            self.assertGreaterEqual(status_snapshot.call_count, 2)
            owner = state["global_asset_attempts"]["asset-1"]
            self.assertEqual(owner["count"], 1)
            self.assertIn(owner["generation"], generations)
            winner = generations.index(owner["generation"])
            loser = 1 - winner
            for record in state["launches"].values():
                record["status"] = "closed"
                record["receipt_schema"] = closure.LAUNCH_RECEIPT_SCHEMA
                record["exit_code"] = 0
            closure._write_state(inventory, state)
            skipped_report = {
                "key": group_key,
                "pending": 0,
                "pending_asset_ids": [],
                "landed_asset_ids": [],
                "attempted_failed_asset_ids": [],
                "skipped_global_attempt": 1,
                "skipped_global_attempt_asset_ids": ["asset-1"],
            }
            with (
                mock.patch.object(
                    closure,
                    "load_generation",
                    side_effect=lambda _i, g: manifest(g),
                ),
                mock.patch.object(closure, "_read_queue", return_value=[row]),
                mock.patch.object(
                    closure,
                    "load_effective_statuses",
                    return_value={"asset-1": {}},
                ),
                mock.patch.object(
                    closure, "inspect_group", return_value=skipped_report
                ),
            ):
                finalized = closure.finalize_group(
                    catalog=root / "catalog.csv",
                    inventory=inventory,
                    generation=generations[loser],
                    group_key=group_key,
                )
            self.assertTrue(finalized["finalized"])
            self.assertEqual(finalized["outcomes"], {})
            self.assertEqual(finalized["candidate_events_added"], 0)

    def test_claim_rejects_status_append_between_read_and_state_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = root / "inventory"
            model = root / "source.blend"
            model.write_bytes(b"BLENDER-v300-source")
            source_stat = model.stat()
            generation = "repair-" + "1" * 24
            group_key = "batch0002/failed/runtime_api_compat/modern_vulkan"
            attempt_id = "attempt-" + "a" * 32
            nonce = "N" * 48
            row = {
                "asset_id": "asset-1",
                "model_file": str(model),
                "source_size": str(source_stat.st_size),
                "source_mtime_ns": str(source_stat.st_mtime_ns),
                "expected_status_signature": "e" * 64,
                "source_stat_signature": "s" * 64,
            }
            manifest = {
                "generation": generation,
                "groups": {
                    group_key: {
                        "queue_file": "queue.csv",
                        "queue_sha256": "q" * 64,
                        "label": (
                            "batch0002_failed_repair_runtime_api_compat_g"
                            + generation.removeprefix("repair-")
                        ),
                        "max_attempts": 1,
                    }
                },
            }
            state = closure._load_state(inventory)
            state["launches"][attempt_id] = {
                "generation": generation,
                "group_key": group_key,
                "attempt_id": attempt_id,
                "status": "confirmed",
                "nonce_sha256": closure._nonce_hash(nonce),
                "asset_ids": ["asset-1"],
            }
            closure._write_state(inventory, state)
            with (
                mock.patch.object(closure, "load_generation", return_value=manifest),
                mock.patch.object(closure, "_read_queue", return_value=[row]),
                mock.patch.object(
                    closure, "load_effective_statuses", return_value={"asset-1": {}}
                ),
                mock.patch.object(
                    closure.render_worker,
                    "repair_closure_row_state",
                    return_value="pending",
                ),
                mock.patch.object(
                    closure,
                    "_status_input_snapshot",
                    side_effect=[(("before",),), (("after",),)],
                ),
                mock.patch.object(
                    closure, "_queue_stat_snapshot", return_value=(1, 2, 3, 4)
                ),
            ):
                with self.assertRaisesRegex(
                    closure.RepairClosureError, "status evidence changed"
                ):
                    closure.claim_asset(
                        catalog=root / "catalog.csv",
                        inventory=inventory,
                        generation=generation,
                        group_key=group_key,
                        attempt_id=attempt_id,
                        nonce=nonce,
                        asset_id="asset-1",
                    )
            state = closure._load_state(inventory)
            self.assertNotIn("asset-1", state["global_asset_attempts"])

    def test_finalize_requires_landed_contract_and_writes_candidate_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog, inventory, _model, status = self.fixture(root)
            manifest = closure.commit_generation(
                self.build(catalog, inventory, status), inventory
            )
            key = next(iter(manifest["groups"]))
            metadata = manifest["groups"][key]
            queue_row = closure._read_queue(inventory, manifest, key)[0]
            attempt_id = self.seed_closed_attempt(
                inventory, manifest, key, "asset-1"
            )
            output = root / "output"
            views = output / "six_views"
            views.mkdir(parents=True)
            (output / "asset.blend").write_bytes(
                b"BLENDER-v500" + b"\x00" * 128
            )
            review = {
                "asset_id": "asset-1",
                "identity_key": "identity-1",
                "render_order": "2001",
                "render_batch": "0002",
                "batch": metadata["label"],
                "status": "accepted",
                "render_route": "static",
                "repair_generation": manifest["generation"],
                "repair_group_key": key,
                "repair_attempt_id": attempt_id,
                "repair_queue_sha256": metadata["queue_sha256"],
                "repair_expected_status_signature": queue_row[
                    "expected_status_signature"
                ],
            }
            (output / "render_review.json").write_text(
                json.dumps(review) + "\n", encoding="utf-8"
            )
            for name in ("front", "back", "left", "right", "top", "iso"):
                (views / f"{name}.png").write_bytes(self.png_bytes())
            landed = {
                **status,
                "batch": metadata["label"],
                "status": "accepted",
                "output_dir": str(output),
                "render_route": "static",
                "repair_generation": manifest["generation"],
                "repair_group_key": key,
                "repair_attempt_id": attempt_id,
                "repair_queue_sha256": metadata["queue_sha256"],
                "repair_expected_status_signature": queue_row[
                    "expected_status_signature"
                ],
                "knowledge_version": "test-v1",
                "updated_at": "2026-07-20 02:00:00",
            }
            with (
                mock.patch.object(
                    closure,
                    "load_effective_statuses",
                    return_value={"asset-1": landed},
                ),
                mock.patch.object(closure, "_blender_can_open", return_value=True),
            ):
                result = closure.finalize_group(
                    catalog=catalog,
                    inventory=inventory,
                    generation=manifest["generation"],
                    group_key=key,
                )
            self.assertTrue(result["finalized"])
            self.assertEqual(result["improved"], 1)
            events = (
                inventory / "repair_closure/knowledge_events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            event = json.loads(events[0])
            self.assertEqual(event["review_status"], "candidate")
            self.assertEqual(event["event_type"], "improvement")
            manifest_path = inventory / "repair_closure/external_knowledge_manifest.json"
            self.assertTrue(manifest_path.is_file())

    def test_finalize_without_confirmed_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog, inventory, _model, status = self.fixture(Path(temporary))
            manifest = closure.commit_generation(
                self.build(catalog, inventory, status), inventory
            )
            key = next(iter(manifest["groups"]))
            with self.assertRaisesRegex(
                closure.RepairClosureError, "confirmed launch receipt"
            ):
                closure.finalize_group(
                    catalog=catalog,
                    inventory=inventory,
                    generation=manifest["generation"],
                    group_key=key,
                )

    def test_truncated_or_corrupt_media_is_not_a_valid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good_png = root / "good.png"
            good_png.write_bytes(self.png_bytes())
            self.assertTrue(closure._valid_png(good_png))
            corrupt_png = root / "corrupt.png"
            payload = bytearray(self.png_bytes())
            payload[-5] ^= 1
            corrupt_png.write_bytes(payload)
            self.assertFalse(closure._valid_png(corrupt_png))
            truncated_png = root / "truncated.png"
            truncated_png.write_bytes(self.png_bytes()[:-4])
            self.assertFalse(closure._valid_png(truncated_png))

            good_mp4 = root / "good.mp4"
            good_mp4.write_bytes(self.mp4_bytes())
            with mock.patch.object(
                closure, "_ffprobe_can_decode_video", return_value=True
            ) as probe:
                self.assertTrue(closure._valid_mp4(good_mp4))
                probe.assert_called_once_with(good_mp4)
            corrupt_mp4 = root / "corrupt.mp4"
            corrupt_mp4.write_bytes(self.mp4_bytes()[:-7])
            with mock.patch.object(
                closure, "_ffprobe_can_decode_video", return_value=True
            ) as probe:
                self.assertFalse(closure._valid_mp4(corrupt_mp4))
                probe.assert_not_called()

            fake_moov = root / "fake-moov.mp4"
            fake_moov.write_bytes(
                struct.pack(">I4s", 20, b"ftyp")
                + b"isom\x00\x00\x00\x00isom"
                + struct.pack(">I4s", 9, b"moov")
                + b"m"
                + struct.pack(">I4s", 1032, b"mdat")
                + b"\x00" * 1024
            )
            with mock.patch.object(
                closure, "_ffprobe_can_decode_video", return_value=True
            ) as probe:
                self.assertFalse(closure._valid_mp4(fake_moov))
                probe.assert_not_called()

    def test_blend_requires_real_header_and_bounded_open_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            random_file = root / "random.blend"
            random_file.write_bytes(b"not a blend file" * 16)
            with mock.patch.object(
                closure, "_blender_can_open", return_value=True
            ) as probe:
                self.assertFalse(closure._valid_blend(random_file))
                probe.assert_not_called()

            valid_header = root / "header.blend"
            valid_header.write_bytes(b"BLENDER-v500" + b"\x00" * 64)
            with mock.patch.object(
                closure, "_blender_can_open", return_value=True
            ) as probe:
                self.assertTrue(closure._valid_blend(valid_header))
                probe.assert_called_once_with(valid_header)
            with mock.patch.object(
                closure, "_blender_can_open", return_value=False
            ):
                self.assertFalse(closure._valid_blend(valid_header))

    def test_ffprobe_requires_video_dimensions_duration_and_frames(self) -> None:
        payload = {
            "streams": [{
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "nb_read_frames": "24",
            }],
            "format": {"duration": "1.000000"},
        }
        with (
            mock.patch.object(closure, "_probe_executable", return_value="ffprobe"),
            mock.patch.object(
                closure.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(payload)
                ),
            ) as run,
        ):
            self.assertTrue(closure._ffprobe_can_decode_video(Path("video.mp4")))
        self.assertEqual(run.call_args.kwargs["timeout"], 120)
        self.assertEqual(run.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")

        for mutation in (
            {"streams": [], "format": {"duration": "1"}},
            {**payload, "format": {"duration": "0"}},
            {
                "streams": [{**payload["streams"][0], "nb_read_frames": "N/A"}],
                "format": payload["format"],
            },
        ):
            with (
                mock.patch.object(
                    closure, "_probe_executable", return_value="ffprobe"
                ),
                mock.patch.object(
                    closure.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=0, stdout=json.dumps(mutation)
                    ),
                ),
            ):
                self.assertFalse(
                    closure._ffprobe_can_decode_video(Path("video.mp4"))
                )

    def test_blender_probe_is_bounded_and_gpu_hidden(self) -> None:
        with (
            mock.patch.object(closure, "_probe_executable", return_value="blender"),
            mock.patch.object(
                closure.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
        ):
            self.assertTrue(closure._blender_can_open(Path("asset.blend")))
        self.assertEqual(run.call_args.kwargs["timeout"], 120)
        self.assertEqual(run.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")
        self.assertIn("--disable-autoexec", run.call_args.args[0])

    def test_finalizer_screen_explicitly_drops_repair_nonce(self) -> None:
        pipeline = PROJECT / "blender/scripts/run_total_asset_pipeline.sh"
        shell = pipeline.read_text(encoding="utf-8")
        start = shell.index("ensure_gpu_finalizer() {")
        end = shell.index("\n}\n", start) + len("\n}\n")
        function = shell[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_screen = bin_dir / "screen"
            argv_capture = root / "screen.argv"
            env_capture = root / "finalizer.env"
            state = root / "screen.started"
            nonce = "S" * 48
            fake_screen.write_text(
                "#!/bin/bash\n"
                "printf '<%s>\\n' \"$@\" > \"$ARGV_CAPTURE\"\n"
                "if [ -z \"${TOTAL_ASSET_REPAIR_CLOSURE_NONCE+x}\" ]; then "
                "echo unset; else echo inherited; fi > \"$ENV_CAPTURE\"\n"
                ": > \"$STATE\"\n",
                encoding="utf-8",
            )
            fake_screen.chmod(0o755)
            command = function + "\n" + f'''
BATCH_NAME=batch0002_failed_repair_runtime_api_compat_g0123456789abcdef01234567
export ARGV_CAPTURE={str(argv_capture)!r}
export ENV_CAPTURE={str(env_capture)!r}
export STATE={str(state)!r}
LAST_VERIFIED_FINALIZER_NAME=""
LAST_VERIFIED_FINALIZER_TOKEN=""
finalizer_attestation_token() {{ echo total_asset_gpu_finalizer_v2_$(printf '%064d' 0); }}
finalizer_screen_state() {{
  if [ -e "$STATE" ]; then echo verified; else echo absent; fi
}}
record_verified_finalizer() {{ :; }}
reap_exact_screen_sessions() {{ :; }}
ensure_gpu_finalizer repair-worker 30773 1 5 11 "" {str(root / "finalizer.log")!r}
'''
            result = subprocess.run(
                ["bash", "-c", command],
                env={
                    **os.environ,
                    "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                    "TOTAL_ASSET_FINALIZER_START_TIMEOUT_SECONDS": "5",
                    "TOTAL_ASSET_REPAIR_CLOSURE_NONCE": nonce,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(env_capture.read_text(encoding="utf-8").strip(), "unset")
            captured = argv_capture.read_text(encoding="utf-8")
            self.assertNotIn(nonce, captured)
            self.assertIn("<-dmS>", captured)
            self.assertNotIn("TOTAL_ASSET_REPAIR_CLOSURE_NONCE", captured)

    def test_screen_command_inherits_nonce_without_serializing_it(self) -> None:
        pipeline = PROJECT / "blender/scripts/run_total_asset_pipeline.sh"
        shell = pipeline.read_text(encoding="utf-8")
        start = shell.index("start_repair_closure_slot() {")
        end = shell.index("\n}\n", start) + len("\n}\n")
        function = shell[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "screen.argv"
            finalizer_batch = root / "finalizer.batch"
            nonce = "N" * 48
            command = function + "\n" + f'''
ROOT={str(root)!r}
INVENTORY={str(root / "inventory")!r}
QUEUE={str(root / "catalog.csv")!r}
PROJECT={str(PROJECT)!r}
LOG_ROOT={str(root / "logs")!r}
PORT=30773
SECONDARY_PORT=30422
SECONDARY_GL_ROOT=/secondary-gl
TERTIARY_PORT=30808
TERTIARY_GL_ROOT=/tertiary-gl
CAPTURE={str(capture)!r}
FINALIZER_BATCH={str(finalizer_batch)!r}
mkdir -p "$INVENTORY" "$LOG_ROOT"
require_mount() {{ :; }}
require_remote_preflight_credentials() {{ :; }}
formal_slot_validate_args() {{ :; }}
python3() {{ echo '{{"valid":true}}'; }}
formal_slot_report_value() {{
  case "$2" in
    batch) echo batch0002 ;;
    expected_status) echo failed ;;
    repair_group) echo runtime_api_compat ;;
    runtime_lane) echo modern_vulkan ;;
    label) echo batch0002_failed_repair_runtime_api_compat_g0123456789abcdef01234567 ;;
    queue) echo "$QUEUE" ;;
    pending) echo 1 ;;
    queue_sha256) printf '%064d\n' 0 ;;
  esac
}}
worker_drain_file() {{ echo "$INVENTORY/batch0002.wc11.drain.json"; }}
assert_no_drain_request() {{ :; }}
repair_closure_label_state() {{
  if [ -s "$CAPTURE" ]; then echo active_exact; else echo inactive; fi
}}
formal_slot_local_state() {{ echo inactive; }}
failed_repair_slot_layout_json() {{ echo '{{}}'; }}
formal_slot_remote_occupancy_preflight() {{ :; }}
begin_holder_transaction() {{ :; }}
release_failed_repair_slot_holder() {{ :; }}
formal_slot_runtime_preflight() {{ :; }}
screen() {{ printf '%s\n' "$@" > "$CAPTURE"; }}
wait_for_verified_worker() {{ :; }}
ensure_gpu_finalizer() {{ printf '%s\n' "$BATCH_NAME" > "$FINALIZER_BATCH"; }}
cleanup_failed_worker_launch() {{ :; }}
mark_holder_slot_started() {{ :; }}
commit_holder_transaction() {{ :; }}
start_repair_closure_slot repair-0123456789abcdef01234567 \
  batch0002/failed/runtime_api_compat/modern_vulkan 30773 1 5 \
  attempt-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
'''
            result = subprocess.run(
                ["bash", "-c", command],
                env={
                    **os.environ,
                    "TOTAL_ASSET_REPAIR_CLOSURE_NONCE": nonce,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = capture.read_text(encoding="utf-8")
            self.assertNotIn(nonce, captured)
            self.assertNotIn("TOTAL_ASSET_REPAIR_CLOSURE_NONCE", captured)
            self.assertIn("--drain-file", captured)
            self.assertIn("batch0002.wc11.drain.json", captured)
            self.assertIn("heartbeat-launch", captured)
            self.assertIn("sleep 60", captured)
            self.assertIn('kill -TERM "$heartbeat_pid"', captured)
            self.assertEqual(
                finalizer_batch.read_text(encoding="utf-8").strip(),
                "batch0002_failed_repair_runtime_api_compat_"
                "g0123456789abcdef01234567",
            )


if __name__ == "__main__":
    unittest.main()
