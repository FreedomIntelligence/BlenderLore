from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_failed_repair_slot as repair_slot
import total_asset_scheduler as scheduler
import total_asset_topology as topology


class FailedRepairSlotTests(unittest.TestCase):
    def slot(self) -> repair_slot.FailedRepairSlot:
        return repair_slot.FailedRepairSlot(
            batch="batch0001",
            group="transport",
            remote_port=topology.TERTIARY_REMOTE_PORT,
            gpu=1,
            worker_index=9,
        )

    def test_only_reviewed_batch1_group_and_canonical_location_are_admitted(
        self,
    ) -> None:
        repair_slot.validate_slot(self.slot())
        for changed in (
            {"batch": "batch0002"},
            {"batch": "batch0000"},
            {"group": "unreviewed"},
            {"worker_index": 8},
            {"worker_count": 8},
            {"remote_port": 65530},
        ):
            values = self.slot().__dict__ | changed
            with self.subTest(changed=changed), self.assertRaises(
                repair_slot.FailedRepairSlotError
            ):
                repair_slot.validate_slot(repair_slot.FailedRepairSlot(**values))

    def test_inactive_group_can_be_claimed(self) -> None:
        self.assertEqual(
            repair_slot.evaluate_group_state(
                self.slot(), claims=(), controllers=(), screen_names=()
            ),
            "inactive",
        )

    def test_supported_batch0_generic_group_and_alias_are_admitted(self) -> None:
        self.assertEqual(
            repair_slot.normalize_group("batch0000", "runtime_api"),
            "runtime_api_compat",
        )
        slot = repair_slot.FailedRepairSlot(
            batch="batch0000",
            group="runtime_api",
            remote_port=topology.SECONDARY_REMOTE_PORT,
            gpu=3,
            worker_index=3,
        )
        repair_slot.validate_slot(slot)
        self.assertEqual(
            slot.label,
            "batch0000_failed_repair_runtime_api_compat",
        )
        self.assertEqual(
            repair_slot.normalize_group("batch0001", "legacy_transport"),
            "transport",
        )
        for manual_group in (
            "timeout_scene_audit",
            "dependency_relink_review",
            "source_corrupt_manual",
            "manual_review",
        ):
            with self.subTest(group=manual_group), self.assertRaises(
                repair_slot.FailedRepairSlotError
            ):
                repair_slot.normalize_group("batch0000", manual_group)

    def test_exact_worker_controller_and_finalizer_are_idempotently_active(
        self,
    ) -> None:
        slot = self.slot()
        claim = scheduler.WorkerClaim(
            slot.label,
            slot.remote_port,
            slot.gpu,
            slot.worker_index,
            slot.worker_count,
        )
        controller = scheduler.ScreenController(
            slot.worker_screen,
            slot.label,
            "worker",
            slot.remote_port,
            slot.gpu,
        )
        self.assertEqual(
            repair_slot.evaluate_group_state(
                slot,
                claims=(claim,),
                controllers=(controller,),
                screen_names=(slot.worker_screen, slot.finalizer_screen),
            ),
            "active_exact",
        )
        self.assertEqual(
            repair_slot.evaluate_group_state(
                slot,
                claims=(),
                controllers=(),
                screen_names=(slot.finalizer_screen,),
            ),
            "finalizing_exact",
        )

    def test_duplicate_cross_slot_or_incomplete_group_fails_closed(self) -> None:
        slot = self.slot()
        exact = scheduler.WorkerClaim(
            slot.label,
            slot.remote_port,
            slot.gpu,
            slot.worker_index,
            slot.worker_count,
        )
        other = scheduler.WorkerClaim(
            slot.label,
            topology.PRIMARY_REMOTE_PORT,
            3,
            7,
            11,
        )
        controller = scheduler.ScreenController(
            slot.worker_screen,
            slot.label,
            "worker",
            slot.remote_port,
            slot.gpu,
        )
        cases = (
            ((exact, other), (controller,), (slot.worker_screen, slot.finalizer_screen)),
            ((exact,), (controller,), (slot.worker_screen,)),
            ((exact,), (controller,), (
                slot.worker_screen, slot.finalizer_screen, slot.finalizer_screen
            )),
        )
        for claims, controllers, names in cases:
            with self.subTest(names=names), self.assertRaises(
                repair_slot.FailedRepairSlotError
            ):
                repair_slot.evaluate_group_state(
                    slot,
                    claims=claims,
                    controllers=controllers,
                    screen_names=names,
                )

    def test_live_audit_propagates_detector_failure_as_blocked(self) -> None:
        with mock.patch.object(
            repair_slot, "detect_worker_claims", side_effect=RuntimeError("ps")
        ), self.assertRaisesRegex(
            repair_slot.FailedRepairSlotError, "ownership audit failed"
        ):
            repair_slot.live_group_state(self.slot())


if __name__ == "__main__":
    unittest.main()
