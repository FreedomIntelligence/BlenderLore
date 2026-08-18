from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "blender/scripts"
sys.path.insert(0, str(SCRIPTS))

import total_asset_quarantine_control as quarantine
import total_asset_scheduler as scheduler
import total_asset_slot_watchdog as watchdog
import total_asset_vulkan_profiles as profiles
from run_total_asset_render_worker import REMOTE_BLENDERS


GPU_UUID = "GPU-12345678-1234-1234-1234-123456789abc"


class TotalAssetQuarantineControlTests(unittest.TestCase):
    def setUp(self) -> None:
        (self.port, self.gpu), self.worker_index = next(
            iter(watchdog.CANONICAL_WORKER_INDEX_BY_LOCATION.items())
        )

    def remote_report(
        self,
        *,
        target_batch: str = "batch0001",
        observed_at_epoch: float | None = None,
        claims: tuple[scheduler.WorkerClaim, ...] = (),
        controllers: tuple[scheduler.ScreenController, ...] = (),
    ) -> dict[str, object]:
        return {
            "schema_version": scheduler.REMOTE_PREFLIGHT_SCHEMA_VERSION,
            "scope": "launch_slots",
            "target_batch": target_batch,
            "worker_count": 11,
            "observed_at_epoch": (
                time.time() if observed_at_epoch is None else observed_at_epoch
            ),
            "ready": True,
            "error_codes": [],
            "launch_layout": [{
                "port": self.port,
                "gpu": self.gpu,
                "worker_index": self.worker_index,
            }],
            "local_claim_count": len(claims),
            "local_claims_digest": scheduler.worker_claims_digest(claims),
            "local_controller_count": len(controllers),
            "local_controllers_digest": scheduler.screen_controllers_digest(
                controllers
            ),
            "nodes": [{
                "port": self.port,
                "reachable": True,
                "probe_ok": True,
                "holder_audit_ok": True,
                "unexpected_holder_sessions": 0,
                "legacy_holder_sessions": 0,
                "gpus": [{
                    "gpu": self.gpu,
                    "gpu_ok": True,
                    "holder_ok": True,
                    "lock_ok": True,
                    "launch_member": True,
                    "expected_worker_index": self.worker_index,
                    "claim_count": 0,
                    "claim_state": "none",
                    "compute_process_count": 1,
                    "compute_process_kinds": ["holder"],
                    "gpu_uuid": GPU_UUID,
                    "physical_binding_ok": True,
                    "compute_process_gpu_uuids": [GPU_UUID],
                    "lock_state": "busy",
                    "holder_session_present": True,
                    "holder_descendant_ok": True,
                    "holder_lock_owner_ok": True,
                    "holder_command_ok": True,
                    "parked_holder": True,
                    "wrapper_process_present": False,
                    "reason_codes": [],
                }],
            }],
        }

    def profile_report(self, family: str) -> dict[str, object]:
        manifest = {
            "schema": profiles.PROFILE_SCHEMA,
            "policy": profiles.PROFILE_POLICY,
            "family": family,
            "gpu_index": self.gpu,
            "gpu_uuid": GPU_UUID,
            "blender_binary": REMOTE_BLENDERS[family],
            "preference_writer": profiles.PROFILE_PREFERENCE_WRITER,
            "preferred_device": "10de/2b85/0",
        }
        return {
            "port": self.port,
            "gpu": self.gpu,
            "gpu_uuid": GPU_UUID,
            "family": family,
            "profile": profiles.current_profile_path(family, GPU_UUID),
            "ready": True,
            "observed_at_epoch": time.time(),
            "manifest": manifest,
        }

    def gate(self) -> scheduler.SlotGateReport:
        return scheduler.SlotGateReport(
            True,
            "ready",
            "batch0001",
            1,
            self.port,
            self.gpu,
            self.worker_index,
            11,
            "batch0000",
            91,
            0,
            (),
            91,
            91,
            "a" * 64,
        )

    def evidence(self, path: Path, payload: dict[str, object]) -> quarantine.EvidenceGeneration:
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        info = path.stat()
        return quarantine.EvidenceGeneration(
            path.resolve(),
            hashlib.sha256(raw).hexdigest(),
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
            payload,
        )

    def test_new_and_legacy_quarantines_have_cas_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary)
            path = watchdog.write_slot_quarantine(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                reason="profile_missing",
            )
            current = watchdog.load_slot_quarantine_generation(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
            )
            self.assertRegex(current.generation, r"^[0-9a-f]{32}$")

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("generation_id")
            watchdog.atomic_write_json(path, payload)
            legacy = watchdog.load_slot_quarantine_generation(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
            )
            self.assertRegex(legacy.generation, r"^[0-9a-f]{64}$")
            self.assertEqual(legacy.generation, legacy.sha256)

    def test_remote_preflight_requires_fresh_exact_parked_holder(self) -> None:
        report = self.remote_report()
        self.assertEqual(
            quarantine._validate_remote_preflight(
                report,
                target_batch="batch0001",
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                worker_claims=(),
                screen_controllers=(),
            ),
            GPU_UUID,
        )
        stale = self.remote_report(observed_at_epoch=time.time() - 121)
        with self.assertRaisesRegex(quarantine.QuarantineControlError, "stale"):
            quarantine._validate_remote_preflight(
                stale,
                target_batch="batch0001",
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                worker_claims=(),
                screen_controllers=(),
            )
        no_lock_owner = self.remote_report()
        no_lock_owner["nodes"][0]["gpus"][0]["holder_lock_owner_ok"] = False
        with self.assertRaisesRegex(
            quarantine.QuarantineControlError, "holder_lock_owner_ok"
        ):
            quarantine._validate_remote_preflight(
                no_lock_owner,
                target_batch="batch0001",
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                worker_claims=(),
                screen_controllers=(),
            )

    def test_both_vulkan_profiles_require_the_preflight_uuid(self) -> None:
        for family in ("4.5", "5.1"):
            with self.subTest(family=family):
                quarantine._validate_profile_status(
                    self.profile_report(family),
                    family=family,
                    remote_port=self.port,
                    gpu=self.gpu,
                    gpu_uuid=GPU_UUID,
                )
        mismatch = self.profile_report("5.1")
        mismatch["manifest"]["gpu_uuid"] = (
            "GPU-ffffffff-1234-1234-1234-123456789abc"
        )
        with self.assertRaisesRegex(
            quarantine.QuarantineControlError, "5.1 Vulkan profile"
        ):
            quarantine._validate_profile_status(
                mismatch,
                family="5.1",
                remote_port=self.port,
                gpu=self.gpu,
                gpu_uuid=GPU_UUID,
            )
        stale = self.profile_report("4.5")
        stale["observed_at_epoch"] = time.time() - 121
        with self.assertRaisesRegex(
            quarantine.QuarantineControlError, "4.5 Vulkan profile"
        ):
            quarantine._validate_profile_status(
                stale,
                family="4.5",
                remote_port=self.port,
                gpu=self.gpu,
                gpu_uuid=GPU_UUID,
            )

        boundary_now = time.time()
        boundary = self.profile_report("4.5")
        boundary["observed_at_epoch"] = boundary_now - 120
        quarantine._validate_profile_status(
            boundary,
            family="4.5",
            remote_port=self.port,
            gpu=self.gpu,
            gpu_uuid=GPU_UUID,
            now_epoch=boundary_now,
        )
        future = self.profile_report("4.5")
        future["observed_at_epoch"] = boundary_now + 31
        with self.assertRaisesRegex(
            quarantine.QuarantineControlError, "4.5 Vulkan profile"
        ):
            quarantine._validate_profile_status(
                future,
                family="4.5",
                remote_port=self.port,
                gpu=self.gpu,
                gpu_uuid=GPU_UUID,
                now_epoch=boundary_now,
            )

    def test_local_audit_requires_ready_gate_and_no_target_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary)
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir()
            (inventory / "total_asset_catalog.csv").write_text(
                "asset_id\n", encoding="utf-8"
            )
            with (
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(
                    scheduler, "count_malformed_status_lines", return_value=0
                ),
                mock.patch.object(scheduler, "slot_launch_gate", return_value=self.gate()),
            ):
                result = quarantine._validate_local_idle_slot(
                    asset_root=asset_root,
                    target_batch="batch0001",
                    remote_port=self.port,
                    gpu=self.gpu,
                    worker_index=self.worker_index,
                )
            self.assertTrue(result.gate.ready)

            with (
                mock.patch.object(watchdog, "process_snapshot", return_value=()),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
                mock.patch.object(
                    scheduler, "count_malformed_status_lines", return_value=0
                ),
                mock.patch.object(
                    scheduler,
                    "slot_launch_gate",
                    return_value=replace(
                        self.gate(),
                        ready=False,
                        reason="target_partition_complete",
                    ),
                ),
            ):
                complete = quarantine._validate_local_idle_slot(
                    asset_root=asset_root,
                    target_batch="batch0001",
                    remote_port=self.port,
                    gpu=self.gpu,
                    worker_index=self.worker_index,
                )
            self.assertEqual(complete.gate.reason, "target_partition_complete")

            worker = watchdog.ProcessRow(
                101,
                1,
                101,
                "python3",
                "python3 blender/scripts/run_total_asset_render_worker.py "
                f"--batch batch0001 --remote-port {self.port} --gpu {self.gpu} "
                f"--worker-index {self.worker_index} --worker-count 11",
            )
            with (
                mock.patch.object(
                    watchdog, "process_snapshot", return_value=(worker,)
                ),
                mock.patch.object(watchdog, "screen_snapshot", return_value={}),
            ):
                with self.assertRaisesRegex(
                    quarantine.QuarantineControlError, "worker claim"
                ):
                    quarantine._validate_local_idle_slot(
                        asset_root=asset_root,
                        target_batch="batch0001",
                        remote_port=self.port,
                        gpu=self.gpu,
                        worker_index=self.worker_index,
                    )

    def test_clear_double_checks_then_atomically_archives_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text(
                "asset_id\n", encoding="utf-8"
            )
            active = watchdog.write_slot_quarantine(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                reason="profile_missing",
            )
            generation = watchdog.load_slot_quarantine_generation(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
            )
            preflight = self.evidence(root / "preflight.json", self.remote_report())
            p45 = self.evidence(root / "p45.json", self.profile_report("4.5"))
            p51 = self.evidence(root / "p51.json", self.profile_report("5.1"))
            audit = quarantine.ClearAudit(
                generation,
                preflight,
                p45,
                p51,
                quarantine.LocalSlotAudit((), (), self.gate(), 0),
                GPU_UUID,
            )
            with mock.patch.object(
                quarantine,
                "audit_clear_preconditions",
                side_effect=(audit, audit),
            ) as validate:
                result = quarantine.clear_slot_quarantine(
                    project=ROOT,
                    asset_root=asset_root,
                    target_batch="batch0001",
                    remote_port=self.port,
                    gpu=self.gpu,
                    worker_index=self.worker_index,
                    worker_count=11,
                    expected_generation=generation.generation,
                    preflight_path=preflight.path,
                    profile_4_5_path=p45.path,
                    profile_5_1_path=p51.path,
                )
            self.assertEqual(validate.call_count, 2)
            validation_epochs = [
                item.kwargs["evidence_now_epoch"]
                for item in validate.call_args_list
            ]
            self.assertEqual(validation_epochs[0], validation_epochs[1])
            self.assertEqual(result["status"], "resolved")
            self.assertFalse(active.exists())
            receipt = Path(str(result["resolved_receipt"]))
            self.assertTrue(receipt.is_file())
            self.assertIn(".resolved.", receipt.name)
            self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(), generation.sha256)
            self.assertEqual(watchdog.load_slot_quarantines(asset_root), {})

    def test_changed_second_validation_preserves_active_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset_root = root / "assets"
            inventory = asset_root / "_asset_inventory"
            inventory.mkdir(parents=True)
            (inventory / "total_asset_catalog.csv").write_text(
                "asset_id\n", encoding="utf-8"
            )
            active = watchdog.write_slot_quarantine(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                reason="profile_missing",
            )
            generation = watchdog.load_slot_quarantine_generation(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
            )
            preflight = self.evidence(root / "preflight.json", self.remote_report())
            p45 = self.evidence(root / "p45.json", self.profile_report("4.5"))
            p51 = self.evidence(root / "p51.json", self.profile_report("5.1"))
            first = quarantine.ClearAudit(
                generation,
                preflight,
                p45,
                p51,
                quarantine.LocalSlotAudit((), (), self.gate(), 0),
                GPU_UUID,
            )
            second = quarantine.ClearAudit(
                generation,
                preflight,
                p45,
                p51,
                quarantine.LocalSlotAudit((), (), self.gate(), 0),
                GPU_UUID.upper(),
            )
            with mock.patch.object(
                quarantine,
                "audit_clear_preconditions",
                side_effect=(first, second),
            ):
                with self.assertRaisesRegex(
                    quarantine.QuarantineControlError, "two validation passes"
                ):
                    quarantine.clear_slot_quarantine(
                        project=ROOT,
                        asset_root=asset_root,
                        target_batch="batch0001",
                        remote_port=self.port,
                        gpu=self.gpu,
                        worker_index=self.worker_index,
                        worker_count=11,
                        expected_generation=generation.generation,
                        preflight_path=preflight.path,
                        profile_4_5_path=p45.path,
                        profile_5_1_path=p51.path,
                    )
            self.assertTrue(active.exists())

    def test_watchdog_baseline_accepts_only_exact_resolved_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            active = watchdog.write_slot_quarantine(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                reason="profile_missing",
            )
            baseline = watchdog.capture_control_baseline(ROOT, asset_root)
            generation = watchdog.load_slot_quarantine_generation(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
            )
            receipt = quarantine._atomic_resolve_quarantine(asset_root, generation)
            advanced = watchdog.verify_control_baseline(
                baseline, ROOT, asset_root
            )
            self.assertEqual(advanced.quarantine_hashes, ())
            self.assertFalse(active.exists())
            self.assertTrue(receipt.exists())

    def test_watchdog_rejects_quarantine_disappearance_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset_root = Path(temporary) / "assets"
            active = watchdog.write_slot_quarantine(
                asset_root,
                remote_port=self.port,
                gpu=self.gpu,
                worker_index=self.worker_index,
                reason="profile_missing",
            )
            baseline = watchdog.capture_control_baseline(ROOT, asset_root)
            active.unlink()
            with self.assertRaisesRegex(
                watchdog.WatchdogError, "quarantine generation changed"
            ):
                watchdog.verify_control_baseline(baseline, ROOT, asset_root)


if __name__ == "__main__":
    unittest.main()
