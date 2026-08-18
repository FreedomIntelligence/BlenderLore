from __future__ import annotations

import dataclasses
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sync_coordinator import (  # noqa: E402
    ConfigurationError,
    CoordinatorConfig,
    EX_CONFIG,
    EX_TEMPFAIL,
    EX_TIMEOUT,
    GateResult,
    SyncCoordinator,
)
import total_asset_remote_preflight as remote_preflight  # noqa: E402
from total_asset_topology import SECONDARY_REMOTE_PORT  # noqa: E402


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o700)
    return path


class SyncCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.state = self.root / "state"
        self.assets = self.root / "assets"
        self.html = self.root / "html"
        self.project.mkdir()
        (self.assets / "total_render").mkdir(parents=True)
        self.html.mkdir()
        self.key = self.root / "id_ed25519"
        self.key.write_text("test-private-key", encoding="utf-8")
        self.key.chmod(0o600)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text("example.invalid ssh-ed25519 AAAATEST\n", encoding="utf-8")
        self.known_hosts.chmod(0o600)
        self.ssh = executable(self.root / "ssh", "exit 0")
        self.rsync = executable(self.root / "rsync", "exit 0")
        self.release_manifest = self.assets / "_asset_inventory" / "model_gallery_publish_manifest.json"
        self.config = CoordinatorConfig(
            project_root=self.project,
            state_root=self.state,
            asset_root=self.assets,
            html_root=self.html,
            release_manifest=self.release_manifest,
            remote="syncbot@example.invalid",
            remote_port=22,
            remote_asset_root="/srv/video2blender/assets",
            remote_html_root="/srv/video2blender/html",
            ssh_key=self.key,
            known_hosts=self.known_hosts,
            ssh_bin=str(self.ssh),
            rsync_bin=str(self.rsync),
            connect_timeout=2.0,
            wall_timeout=1.0,
            scope_wall_timeouts={scope: 5.0 for scope in ("assets", "total-render", "video-html", "model-html")},
            heartbeat_interval=0.02,
            assets_authorized=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def coordinator(self, **changes: object) -> SyncCoordinator:
        return SyncCoordinator(
            dataclasses.replace(self.config, **changes),
            qa_activity_detector=lambda: (),
        )

    def write_valid_release_manifest(
        self,
        *,
        accepted: int = 900,
        needs_review: int = 50,
        deferred: int = 0,
        failed: int = 50,
        corrupt_hash: bool = False,
    ) -> dict[str, object]:
        page = self.html / "asset_gallery_model_full.html"
        media = self.html / "model_gallery_media" / "asset-1" / "preview.jpg"
        media.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<html>model_gallery_media/asset-1/preview.jpg</html>", encoding="utf-8")
        media.write_bytes(b"jpeg-data")

        def entry(path: Path) -> dict[str, object]:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if corrupt_hash and path == media:
                digest = "0" * 64
            return {
                "path": path.relative_to(self.html).as_posix(),
                "sha256": digest,
                "size": path.stat().st_size,
            }

        manifest: dict[str, object] = {
            "schema_version": 1,
            "batch": "batch0000",
            "batch_size": 1000,
            "terminal_count": 1000,
            "accepted_count": accepted,
            "needs_review_count": needs_review,
            "deferred_count": deferred,
            "failed_count": failed,
            "repair_complete": True,
            "audit_complete": True,
            "gallery_updated": True,
            "files": [entry(page), entry(media)],
        }
        self.release_manifest.parent.mkdir(parents=True, exist_ok=True)
        inventory = self.release_manifest.parent
        catalog = inventory / "total_asset_catalog.csv"
        with catalog.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "asset_id",
                    "model_file",
                    "identity_key",
                    "render_order",
                    "render_batch",
                    "inventory_status",
                    "duplicate_of",
                ),
            )
            writer.writeheader()
            for index in range(1000):
                writer.writerow(
                    {
                        "asset_id": f"asset-{index:04d}",
                        "model_file": f"/models/{index:04d}.blend",
                        "identity_key": f"identity-{index:04d}",
                        "render_order": index + 1,
                        "render_batch": "0000",
                        "inventory_status": "ready",
                        "duplicate_of": "",
                    }
                )
        status_names = (
            ["accepted"] * accepted
            + ["needs_review"] * needs_review
            + ["deferred"] * deferred
            + ["failed"] * failed
        )
        status_path = inventory / "total_asset_render_status_batch0000_quality_audit.jsonl"
        with status_path.open("w", encoding="utf-8") as handle:
            for index, status in enumerate(status_names):
                handle.write(
                    json.dumps(
                        {
                            "batch": "batch0000_quality_audit",
                            "asset_id": f"asset-{index:04d}",
                            "identity_key": f"identity-{index:04d}",
                            "render_order": str(index + 1),
                            "render_batch": "0000",
                            "status": status,
                            "updated_at": "2026-07-16T00:00:00+00:00",
                        }
                    )
                    + "\n"
                )
        from total_asset_scheduler import derive_plan

        plan = derive_plan(
            catalog,
            inventory,
            batch_size=1000,
            active_batches=set(),
            worker_claims=(),
        )
        batch_state = next(item for item in plan.batches if item.key == "batch0000")
        manifest["audit_generation"] = batch_state.generation
        marker = inventory / "scheduler_events" / (
            f"batch0000.audit.{batch_state.generation}.json"
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "event": "audit",
                    "batch": "batch0000",
                    "generation": batch_state.generation,
                    "completed_at": "2026-07-16 00:00:00",
                }
            ),
            encoding="utf-8",
        )
        self.release_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_status_is_read_only_when_state_does_not_exist(self) -> None:
        status = self.coordinator().status("assets")
        self.assertEqual(status["pending"], {})
        self.assertFalse(self.state.exists())

    def test_request_merges_scopes_and_increments_generation(self) -> None:
        coordinator = self.coordinator()
        first = coordinator.request("assets")
        second = coordinator.request("total-render")
        third = coordinator.request("assets")
        status = coordinator.status()
        self.assertEqual((first["generation"], second["generation"], third["generation"]), (1, 2, 3))
        self.assertEqual(status["pending_generation"], 3)
        self.assertEqual(status["pending"]["assets"]["generation"], 3)
        self.assertEqual(status["pending"]["total-render"]["generation"], 2)

    def test_concurrent_requests_are_atomic(self) -> None:
        coordinator = self.coordinator()
        errors: list[BaseException] = []

        def request(scope: str) -> None:
            try:
                coordinator.request(scope)
            except BaseException as error:  # pragma: no cover - diagnostic path
                errors.append(error)

        threads = [
            threading.Thread(target=request, args=("assets" if index % 2 else "video-html",))
            for index in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(coordinator.status()["pending_generation"], 12)
        self.assertEqual(set(coordinator.status()["pending"]), {"assets", "video-html"})

    def test_new_request_is_not_consumed_by_old_generation(self) -> None:
        coordinator = self.coordinator()
        old = coordinator.request("assets")["generation"]
        new = coordinator.request("assets")["generation"]
        self.assertFalse(coordinator._consume_if_unchanged("assets", old))
        self.assertEqual(coordinator.status()["pending"]["assets"]["generation"], new)

    def test_corrupt_pending_state_is_not_overwritten(self) -> None:
        self.state.mkdir()
        pending = self.state / "pending.json"
        pending.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            self.coordinator().request("assets")
        self.assertEqual(pending.read_text(encoding="utf-8"), "{not-json")

    def test_failed_preflight_preserves_pending(self) -> None:
        bad_ssh = executable(self.root / "bad-ssh", "echo 'Permission denied (publickey)' >&2; exit 255")
        coordinator = self.coordinator(ssh_bin=str(bad_ssh))
        coordinator.request("assets")
        exit_code, result = coordinator.run("assets")
        self.assertEqual(exit_code, EX_CONFIG)
        self.assertTrue(result["pending_preserved"])
        self.assertIn("assets", coordinator.status()["pending"])
        persisted = (self.state / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("Permission denied", persisted)
        self.assertNotIn(str(self.key), persisted)
        self.assertNotIn("example.invalid", persisted)

    def test_blackholed_connection_hits_timeout(self) -> None:
        sleeping_ssh = executable(self.root / "sleeping-ssh", "sleep 5")
        coordinator = self.coordinator(ssh_bin=str(sleeping_ssh), connect_timeout=0.08)
        started = time.monotonic()
        ready, code = coordinator.preflight("assets")
        self.assertFalse(ready)
        self.assertEqual(code, "connection_timeout")
        self.assertLess(time.monotonic() - started, 2)

    def test_rsync_wall_timeout_preserves_pending(self) -> None:
        sleeping_rsync = executable(self.root / "sleeping-rsync", "sleep 5")
        coordinator = self.coordinator(
            rsync_bin=str(sleeping_rsync),
            wall_timeout=0.3,
            scope_wall_timeouts={scope: 0.3 for scope in ("assets", "total-render", "video-html", "model-html")},
        )
        coordinator.request("total-render")
        exit_code, result = coordinator.run("total-render")
        self.assertEqual(exit_code, EX_TIMEOUT)
        self.assertEqual(result["error_code"], "wall_clock_timeout")
        self.assertIn("total-render", coordinator.status()["pending"])

    def test_heartbeat_write_failure_stops_run_and_preserves_pending(self) -> None:
        sleeping_ssh = executable(self.root / "heartbeat-ssh", "sleep 5")
        coordinator = self.coordinator(ssh_bin=str(sleeping_ssh))
        coordinator.request("total-render")
        with mock.patch.object(coordinator, "_heartbeat", side_effect=OSError("disk full")):
            exit_code, result = coordinator.run("total-render")
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(result["error_code"], "local_state_failed")
        self.assertIn("total-render", coordinator.status()["pending"])

    def test_pause_preserves_pending_without_remote_command(self) -> None:
        marker = self.root / "remote-called"
        marker_command = executable(self.root / "marker", f"touch '{marker}'; exit 0")
        coordinator = self.coordinator(ssh_bin=str(marker_command), rsync_bin=str(marker_command))
        coordinator.request("assets")
        coordinator.pause()
        exit_code, result = coordinator.run("assets")
        self.assertEqual(exit_code, EX_TEMPFAIL)
        self.assertEqual(result["state"], "paused")
        self.assertFalse(marker.exists())
        self.assertIn("assets", coordinator.status()["pending"])

    def test_corrupt_pause_marker_fails_closed(self) -> None:
        coordinator = self.coordinator()
        coordinator.request("assets")
        coordinator.pause_path.write_text("not-json", encoding="utf-8")
        exit_code, result = coordinator.run("assets")
        self.assertEqual(exit_code, EX_TEMPFAIL)
        self.assertEqual(result["state"], "paused")
        self.assertIn("assets", coordinator.status()["pending"])

    def test_paused_assets_preflight_still_runs_read_only_probes(self) -> None:
        ssh_args = self.root / "ssh-args"
        rsync_args = self.root / "rsync-args"
        recording_ssh = executable(
            self.root / "recording-ssh",
            f"printf '%s\\n' \"$*\" > '{ssh_args}'; exit 0",
        )
        recording_rsync = executable(
            self.root / "recording-rsync",
            f"printf '%s\\n' \"$*\" > '{rsync_args}'; "
            "echo 'V2B_PREFLIGHT_CHANGE:>f+++++++++|123|asset.blend'; exit 0",
        )
        coordinator = self.coordinator(
            ssh_bin=str(recording_ssh), rsync_bin=str(recording_rsync)
        )
        coordinator.pause()
        ready, code = coordinator.preflight("assets")
        self.assertTrue(ready)
        self.assertEqual(code, "ready")
        self.assertIn('[ -w "$target" ]', ssh_args.read_text(encoding="utf-8"))
        self.assertIn("--dry-run", rsync_args.read_text(encoding="utf-8"))
        self.assertEqual(coordinator.last_preflight["change_count"], 1)
        self.assertEqual(coordinator.last_preflight["transfer_bytes"], 123)

    def test_assets_run_needs_independent_authorization_but_preflight_does_not(self) -> None:
        marker = self.root / "rsync-called"
        recording_rsync = executable(
            self.root / "recording-rsync",
            f"touch '{marker}'; exit 0",
        )
        coordinator = self.coordinator(
            rsync_bin=str(recording_rsync), assets_authorized=False
        )
        ready, code = coordinator.preflight("assets")
        self.assertTrue(ready)
        self.assertEqual(code, "ready")
        self.assertTrue(marker.exists())
        marker.unlink()
        coordinator.request("assets")
        exit_code, result = coordinator.run("assets")
        self.assertEqual(exit_code, EX_CONFIG)
        self.assertEqual(result["error_code"], "assets_authorization_required")
        self.assertFalse(marker.exists())
        self.assertIn("assets", coordinator.status()["pending"])

    def test_model_gate_blocks_before_any_remote_write(self) -> None:
        marker = self.root / "remote-called"
        marker_command = executable(self.root / "marker", f"touch '{marker}'; exit 0")
        coordinator = self.coordinator(ssh_bin=str(marker_command), rsync_bin=str(marker_command))
        coordinator.request("model-html")
        exit_code, result = coordinator.run("model-html")
        self.assertEqual(exit_code, EX_CONFIG)
        self.assertEqual(result["error_code"], "model_gate_blocked")
        self.assertFalse(marker.exists())
        self.assertIn("model-html", coordinator.status()["pending"])

    def test_malformed_formal_log_blocks_before_any_remote_write(self) -> None:
        self.write_valid_release_manifest()
        status_path = (
            self.release_manifest.parent
            / "total_asset_render_status_batch0000_quality_audit.jsonl"
        )
        with status_path.open("a", encoding="utf-8") as handle:
            handle.write("{not-json\n")
        marker = self.root / "remote-called-malformed"
        marker_command = executable(
            self.root / "marker-malformed", f"touch '{marker}'; exit 0"
        )
        coordinator = self.coordinator(
            ssh_bin=str(marker_command), rsync_bin=str(marker_command)
        )
        coordinator.request("model-html")
        exit_code, result = coordinator.run("model-html")
        self.assertEqual(exit_code, EX_CONFIG)
        self.assertEqual(result["error_code"], "model_gate_blocked")
        self.assertFalse(marker.exists())
        self.assertIn("model-html", coordinator.status()["pending"])

    def test_model_gate_change_after_staging_never_publishes_live(self) -> None:
        self.write_valid_release_manifest()
        ssh_log = self.root / "ssh-log"
        rsync_log = self.root / "rsync-log"
        recording_ssh = executable(
            self.root / "recording-ssh",
            f"printf '%s\\n' \"$*\" >> '{ssh_log}'; exit 0",
        )
        recording_rsync = executable(
            self.root / "recording-rsync",
            f"printf '%s\\n' \"$*\" >> '{rsync_log}'; exit 0",
        )
        coordinator = self.coordinator(
            ssh_bin=str(recording_ssh), rsync_bin=str(recording_rsync)
        )
        valid_gate = coordinator.model_gate()
        calls = 0

        def changing_gate() -> GateResult:
            nonlocal calls
            calls += 1
            if calls >= 4:
                return GateResult(False, "formal_status_evidence_mismatch")
            return valid_gate

        coordinator.request("model-html")
        with mock.patch.object(coordinator, "model_gate", side_effect=changing_gate):
            exit_code, result = coordinator.run("model-html")
        self.assertEqual(exit_code, EX_CONFIG)
        self.assertEqual(result["error_code"], "model_gate_blocked")
        self.assertIn("model-html", coordinator.status()["pending"])
        ssh_commands = ssh_log.read_text(encoding="utf-8")
        self.assertNotIn(".video2blender-model-current", ssh_commands)
        transfer_commands = [
            line
            for line in rsync_log.read_text(encoding="utf-8").splitlines()
            if "--dry-run" not in line
        ]
        self.assertTrue(transfer_commands)
        self.assertTrue(
            all("/.video2blender-model-staging/" in line for line in transfer_commands)
        )

    def test_model_gate_enforces_thresholds_and_hashes(self) -> None:
        self.write_valid_release_manifest(accepted=849, needs_review=50, deferred=51, failed=50)
        self.assertEqual(self.coordinator().model_gate().code, "publishable_gate_failed")
        self.write_valid_release_manifest(corrupt_hash=True)
        self.assertEqual(self.coordinator().model_gate().code, "release_file_mismatch")
        self.write_valid_release_manifest()
        gate = self.coordinator().model_gate()
        self.assertTrue(gate.ready)
        self.assertEqual(len(gate.files), 2)

    def test_model_gate_requires_hashes_for_every_referenced_media_file(self) -> None:
        self.write_valid_release_manifest()
        page = self.html / "asset_gallery_model_full.html"
        page.write_text(
            "<html>model_gallery_media/asset-2/missing.jpg</html>", encoding="utf-8"
        )
        manifest = json.loads(self.release_manifest.read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = hashlib.sha256(page.read_bytes()).hexdigest()
        manifest["files"][0]["size"] = page.stat().st_size
        self.release_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(
            self.coordinator().model_gate().code,
            "model_gallery_manifest_incomplete",
        )

    def test_model_gate_reconciles_manifest_with_formal_statuses(self) -> None:
        manifest = self.write_valid_release_manifest()
        manifest["accepted_count"] = 901
        manifest["needs_review_count"] = 49
        self.release_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(
            self.coordinator().model_gate().code,
            "formal_status_evidence_mismatch",
        )
        self.write_valid_release_manifest()
        status_path = (
            self.release_manifest.parent
            / "total_asset_render_status_batch0000_quality_audit.jsonl"
        )
        lines = status_path.read_text(encoding="utf-8").splitlines()
        status_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        self.assertEqual(
            self.coordinator().model_gate().code,
            "formal_status_evidence_invalid",
        )

    def test_formal_evidence_digest_includes_same_count_status_identity(self) -> None:
        self.write_valid_release_manifest()
        coordinator = self.coordinator()
        before = coordinator._formal_status_evidence("batch0000")
        self.assertIsNotNone(before)
        status_path = (
            self.release_manifest.parent
            / "total_asset_render_status_batch0000_quality_audit.jsonl"
        )
        rows = [json.loads(line) for line in status_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertEqual(rows[900]["status"], "needs_review")
        rows[0]["status"], rows[900]["status"] = rows[900]["status"], rows[0]["status"]
        status_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        after = coordinator._formal_status_evidence("batch0000")
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        self.assertEqual(before["terminal_count"], after["terminal_count"])
        self.assertNotEqual(before["evidence_digest"], after["evidence_digest"])

    def test_model_gate_requires_generation_bound_audit_attestation(self) -> None:
        manifest = self.write_valid_release_manifest()
        marker = (
            self.release_manifest.parent
            / "scheduler_events"
            / f"batch0000.audit.{manifest['audit_generation']}.json"
        )
        marker.unlink()
        self.assertEqual(
            self.coordinator().model_gate().code,
            "audit_attestation_missing",
        )

    def test_model_gate_rejects_live_qa_activity(self) -> None:
        self.write_valid_release_manifest()
        coordinator = SyncCoordinator(
            self.config,
            qa_activity_detector=lambda: ("screen:batch0000_repair",),
        )
        self.assertEqual(coordinator.model_gate().code, "qa_activity_active")

    def test_model_gate_rejects_holder_without_complete_process_evidence(self) -> None:
        self.write_valid_release_manifest()
        node = remote_preflight.NodeObservation(
            port=SECONDARY_REMOTE_PORT,
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
                    compute_process_kinds=("other",) if gpu == 0 else (),
                    holder_session=gpu == 0,
                    lock_state="busy" if gpu == 0 else "available",
                    wrapper_process_present=False,
                    gpu_uuid=(
                        f"GPU-00000000-0000-0000-0000-{gpu:012x}"
                    ),
                    physical_binding_ok=True,
                    compute_process_gpu_uuids=(
                        (f"GPU-00000000-0000-0000-0000-{gpu:012x}",)
                        if gpu == 0
                        else ()
                    ),
                    holder_descendant_ok=False,
                    holder_lock_owner_ok=gpu == 0,
                    holder_command_ok=gpu == 0,
                )
                for gpu in range(4)
            ),
        )
        coordinator = SyncCoordinator(self.config)
        with mock.patch(
            "total_asset_remote_preflight.run_ssh_probe",
            return_value=node,
        ):
            self.assertEqual(coordinator.model_gate().code, "qa_activity_active")

    def test_status_timestamp_change_invalidates_audit_generation(self) -> None:
        self.write_valid_release_manifest()
        status_path = (
            self.release_manifest.parent
            / "total_asset_render_status_batch0000_quality_audit.jsonl"
        )
        rows = [json.loads(line) for line in status_path.read_text(encoding="utf-8").splitlines()]
        rows[0]["updated_at"] = "2026-07-16T00:00:01+00:00"
        status_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        self.assertEqual(
            self.coordinator().model_gate().code,
            "audit_attestation_mismatch",
        )

    def test_valid_model_release_runs_and_consumes_request(self) -> None:
        self.write_valid_release_manifest()
        coordinator = self.coordinator()
        coordinator.request("model-html")
        exit_code, result = coordinator.run("model-html")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["state"], "complete")
        self.assertTrue(result["request_consumed"])
        self.assertNotIn("model-html", coordinator.status()["pending"])

    def test_model_publish_verifies_immutable_release_not_public_symlinks(self) -> None:
        self.write_valid_release_manifest()
        rsync_log = self.root / "model-rsync-log"
        recording_rsync = executable(
            self.root / "recording-model-rsync",
            f"printf '%s\\n' \"$*\" >> '{rsync_log}'; exit 0",
        )
        coordinator = self.coordinator(rsync_bin=str(recording_rsync))
        coordinator.request("model-html")
        exit_code, result = coordinator.run("model-html")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["state"], "complete")
        verification_commands = [
            line
            for line in rsync_log.read_text(encoding="utf-8").splitlines()
            if "V2B_VERIFY_CHANGE" in line
        ]
        self.assertGreaterEqual(len(verification_commands), 2)
        self.assertIn("/.video2blender-model-staging/", verification_commands[-2])
        self.assertIn("/.video2blender-model-releases/", verification_commands[-1])

    def test_verification_difference_preserves_pending(self) -> None:
        differing_rsync = executable(
            self.root / "differing-rsync",
            "case \" $* \" in *V2B_VERIFY_CHANGE*) echo 'V2B_VERIFY_CHANGE:>f+++++++++|unexpected';; esac; exit 0",
        )
        coordinator = self.coordinator(rsync_bin=str(differing_rsync))
        coordinator.request("total-render")
        exit_code, result = coordinator.run("total-render")
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(result["error_code"], "verification_failed")
        self.assertIn("total-render", coordinator.status()["pending"])

    def test_verification_ignores_non_itemized_banner_output(self) -> None:
        noisy_rsync = executable(
            self.root / "noisy-rsync",
            "echo 'rsync informational banner'; echo 'sent 10 bytes received 20 bytes'; exit 0",
        )
        coordinator = self.coordinator(rsync_bin=str(noisy_rsync))
        coordinator.request("total-render")
        exit_code, result = coordinator.run("total-render")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["state"], "complete")

    def test_key_only_strict_host_options_and_non_root_account(self) -> None:
        coordinator = self.coordinator()
        command = coordinator._ssh_base()
        joined = " ".join(command)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("PreferredAuthentications=publickey", joined)
        self.assertIn("PasswordAuthentication=no", joined)
        self.assertIn("KbdInteractiveAuthentication=no", joined)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("UserKnownHostsFile=", joined)
        self.assertIn("ConnectionAttempts=1", joined)
        self.assertIn("ServerAliveInterval=15", joined)
        self.assertIn("ServerAliveCountMax=3", joined)
        root_coordinator = self.coordinator(remote="root@example.invalid")
        ready, code = root_coordinator.preflight("assets")
        self.assertFalse(ready)
        self.assertEqual(code, "local_configuration_invalid")

    def test_known_hosts_must_be_owned_regular_and_not_writable_by_others(self) -> None:
        self.known_hosts.chmod(0o666)
        ready, code = self.coordinator().preflight("assets")
        self.assertFalse(ready)
        self.assertEqual(code, "local_configuration_invalid")

        self.known_hosts.unlink()
        target = self.root / "known-hosts-target"
        target.write_text("example.invalid ssh-ed25519 AAAATEST\n", encoding="utf-8")
        target.chmod(0o600)
        self.known_hosts.symlink_to(target)
        ready, code = self.coordinator().preflight("assets")
        self.assertFalse(ready)
        self.assertEqual(code, "local_configuration_invalid")

    def test_video_html_scope_never_includes_model_paths(self) -> None:
        command = self.coordinator()._rsync_command(
            "video-html", mode="sync", model_files_from=None
        )
        joined = "\n".join(command)
        self.assertIn("video_gallery_full", joined)
        self.assertNotIn("model_gallery_media", joined)
        self.assertNotIn("asset_gallery_model_full", joined)

    def test_scope_specific_wall_timeout_is_used(self) -> None:
        coordinator = self.coordinator(
            scope_wall_timeouts={
                "assets": 11.0,
                "total-render": 22.0,
                "video-html": 33.0,
                "model-html": 44.0,
            }
        )
        self.assertEqual(coordinator.config.wall_timeout_for("assets"), 11.0)
        self.assertEqual(coordinator.config.wall_timeout_for("model-html"), 44.0)

    def test_legacy_wrappers_reject_implicit_or_pull_sync(self) -> None:
        assets = subprocess.run(
            ["bash", str(SCRIPTS / "sync_blender_assets_to_server.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        html = subprocess.run(
            ["bash", str(SCRIPTS / "sync_blender_html_between_local_server.sh"), "pull", "video-html"],
            capture_output=True,
            text=True,
            check=False,
        )
        videos = subprocess.run(
            ["bash", str(SCRIPTS / "sync_blender_videos_to_server.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        transcripts = subprocess.run(
            ["bash", str(SCRIPTS / "sync_blender_video_transcripts_to_server.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            (assets.returncode, html.returncode, videos.returncode, transcripts.returncode),
            (64, 64, 64, 64),
        )


if __name__ == "__main__":
    unittest.main()
