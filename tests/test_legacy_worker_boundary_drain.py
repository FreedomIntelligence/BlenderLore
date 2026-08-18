from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import legacy_worker_boundary_drain as drain
from total_asset_remote_preflight import RemoteProbeError
from total_asset_scheduler import WorkerClaim
import total_asset_scheduler as scheduler


PROJECT = Path(__file__).resolve().parents[1]
WATCHER = PROJECT / "blender/scripts/legacy_worker_boundary_drain.py"


class LegacyWorkerBoundaryDrainTests(unittest.TestCase):
    def test_uncooperative_legacy_worker_is_never_stopped_between_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status = root / "total_asset_render_status_batch0001_p30773_g2.jsonl"
            state = root / "state.json"
            screen = root / "screen"
            worker_script = root / "run_total_asset_render_worker.py"
            next_asset = root / "next-asset-started"
            status.write_text("", encoding="utf-8")
            worker_script.write_text(
                "import json,os,time\n"
                "time.sleep(0.1)\n"
                f"with open({str(status)!r}, 'a') as h:\n"
                " h.write(json.dumps({'batch':'batch0001','asset_id':'000577',"
                f"'status':'needs_review','render_batch':'0001','worker':{status.stem!r},"
                "'remote_gpu_index':'2'})+'\\n'); h.flush(); os.fsync(h.fileno())\n"
                f"open({str(next_asset)!r}, 'w').write('started')\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            screen.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = '-ls' ]; then\n"
                "  printf '\\t4321.total_asset_batch0001_g2\\t(Detached)\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            screen.chmod(0o755)
            worker = subprocess.Popen([
                sys.executable,
                str(worker_script),
                "--batch", "batch0001",
                "--remote-port", "30773",
                "--gpu", "2",
                "--worker-index", "6",
                "--worker-count", "8",
            ])
            watcher = None
            try:
                watcher = subprocess.Popen([
                    sys.executable,
                    str(WATCHER),
                    "--pid", str(worker.pid),
                    "--inventory", str(root),
                    "--status-file", str(status),
                    "--batch", "batch0001",
                    "--remote-port", "30773",
                    "--gpu", "2",
                    "--worker-index", "6",
                    "--worker-count", "8",
                    "--state", str(state),
                    "--controller-session", "4321.total_asset_batch0001_g2",
                    "--screen-bin", str(screen),
                    "--poll-seconds", "0.01",
                ])
                self.assertEqual(watcher.wait(timeout=5), 75)
                payload = json.loads(state.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "blocked_uncooperative_legacy")
                deadline = time.monotonic() + 5
                while not next_asset.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(next_asset.exists())
                process_state = subprocess.run(
                    ["ps", "-p", str(worker.pid), "-o", "state="],
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout
                self.assertNotIn("T", process_state)
            finally:
                if watcher is not None and watcher.poll() is None:
                    watcher.terminate()
                    watcher.wait(timeout=5)
                if worker.poll() is None:
                    os.kill(worker.pid, signal.SIGTERM)
                    os.kill(worker.pid, signal.SIGCONT)
                    worker.wait(timeout=5)

    def boundary_state(
        self, state: Path, *, status: str = "boundary_stopped"
    ) -> tuple[dict[str, object], WorkerClaim]:
        claim = WorkerClaim("batch0001", 30773, 2, 6, 8)
        payload: dict[str, object] = {
            "schema_version": 1,
            "status": status,
            "worker_pid": 987654,
            "worker_start_time": "Thu Jul 17 12:00:00 2026",
            "worker_command": (
                "python3 run_total_asset_render_worker.py --batch batch0001 "
                "--remote-port 30773 --gpu 2 --worker-index 6 --worker-count 8"
            ),
            "batch": claim.batch,
            "remote_port": claim.remote_port,
            "gpu": claim.gpu,
            "worker_index": claim.worker_index,
            "worker_count": claim.worker_count,
            "controller_session": "4321.total_asset_batch0001_g2",
            "boundary_proof": "worker_cooperative_ack_v1",
        }
        state.write_text(json.dumps(payload), encoding="utf-8")
        return payload, claim

    def quarantine_attestation_state(
        self, state: Path
    ) -> tuple[dict[str, object], WorkerClaim, drain.ProcessIdentity, str]:
        claim = drain.QUARANTINED_LEGACY_CLAIM
        controller = "4321.total_asset_batch0001_g3_retry"
        command = (
            "python3 run_total_asset_render_worker.py --batch batch0001 "
            "--remote-port 30773 --gpu 3 --worker-index 7 --worker-count 8"
        )
        identity = drain.ProcessIdentity(
            987654,
            4321,
            "T+",
            "Thu Jul 17 12:00:00 2026",
            command,
        )
        payload: dict[str, object] = {
            "schema_version": 1,
            "status": "boundary_stopped",
            "worker_pid": identity.pid,
            "worker_start_time": identity.started_at,
            "worker_command": identity.command,
            "batch": claim.batch,
            "remote_port": claim.remote_port,
            "gpu": claim.gpu,
            "worker_index": claim.worker_index,
            "worker_count": claim.worker_count,
            "controller_session": controller,
        }
        state.write_text(json.dumps(payload), encoding="utf-8")
        return payload, claim, identity, controller

    def test_quarantine_attestation_is_short_lived_hashed_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            attestation_path = root / "attestation.json"
            original, claim, identity, controller = self.quarantine_attestation_state(
                state
            )
            zombie = drain.ProcessIdentity(
                5620,
                identity.pid,
                "Z",
                "Thu Jul 17 12:00:01 2026",
                "[ssh] <defunct>",
            )

            def identity_reader(pid: int) -> drain.ProcessIdentity | None:
                return zombie if pid == zombie.pid else identity

            attestation = drain.attest_quarantine(
                state,
                attestation_path,
                controller_session=controller,
                ttl_seconds=90,
                identity_reader=identity_reader,
                worker_assertion=lambda *_args, **_kwargs: identity.command,
                claim_discoverer=lambda: {claim: (identity.pid,)},
                tree_discoverer=lambda item: {
                    item.pid: item,
                    zombie.pid: zombie,
                },
                controller_lister=lambda _screen: (controller,),
                epoch_reader=lambda: 1_721_200_000,
            )
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), original)
            self.assertEqual(
                json.loads(attestation_path.read_text(encoding="utf-8")),
                attestation,
            )
            self.assertEqual(
                attestation["schema"], drain.QUARANTINE_ATTESTATION_SCHEMA
            )
            self.assertEqual(attestation["observed_epoch"], 1_721_200_000)
            self.assertEqual(attestation["expires_epoch"], 1_721_200_090)
            self.assertEqual(attestation["target"]["worker_index"], 7)
            self.assertEqual(len(attestation["state_sha256"]), 64)
            self.assertEqual(len(attestation["process_identity_sha256"]), 64)
            evidence = dict(attestation)
            digest = evidence.pop("evidence_digest")
            self.assertEqual(digest, drain.canonical_digest(evidence))

    def test_quarantine_attestation_accepts_legacy_state_without_start_or_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            original, claim, identity, controller = self.quarantine_attestation_state(
                state
            )
            original.pop("worker_start_time")
            original.pop("controller_session")
            state.write_text(json.dumps(original), encoding="utf-8")
            attestation = drain.attest_quarantine(
                state,
                root / "attestation.json",
                controller_session=controller,
                identity_reader=lambda _pid: identity,
                worker_assertion=lambda *_args, **_kwargs: identity.command,
                claim_discoverer=lambda: {claim: (identity.pid,)},
                tree_discoverer=lambda item: {item.pid: item},
                controller_lister=lambda _screen: (controller,),
                epoch_reader=lambda: 1_721_200_000,
            )
            self.assertEqual(attestation["process"]["started_at"], identity.started_at)
            self.assertEqual(attestation["controller_session"], controller)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), original)

    def test_quarantine_attestation_rejects_symlink_and_unowned_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            self.quarantine_attestation_state(state)
            link = root / "state-link.json"
            link.symlink_to(state)
            with self.assertRaisesRegex(
                drain.BoundaryDrainError, "regular owned file|regular file owned"
            ):
                drain.load_owned_regular_state(link)
            with mock.patch.object(drain.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaisesRegex(
                    drain.BoundaryDrainError, "regular file owned"
                ):
                    drain.load_owned_regular_state(state)
            state.chmod(0o666)
            with self.assertRaisesRegex(
                drain.BoundaryDrainError, "regular file owned"
            ):
                drain.load_owned_regular_state(state)

    def test_quarantine_attestation_rejects_live_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            output = root / "attestation.json"
            original, claim, identity, controller = self.quarantine_attestation_state(
                state
            )
            child = drain.ProcessIdentity(
                5621,
                identity.pid,
                "S",
                "Thu Jul 17 12:00:01 2026",
                "ssh active-render",
            )
            with self.assertRaisesRegex(
                drain.BoundaryDrainError, "non-zombie descendant"
            ):
                drain.attest_quarantine(
                    state,
                    output,
                    controller_session=controller,
                    identity_reader=lambda pid: child if pid == child.pid else identity,
                    worker_assertion=lambda *_args, **_kwargs: identity.command,
                    claim_discoverer=lambda: {claim: (identity.pid,)},
                    tree_discoverer=lambda item: {
                        item.pid: item,
                        child.pid: child,
                    },
                    controller_lister=lambda _screen: (controller,),
                )
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), original)

    def test_quarantine_attestation_rejects_other_30773_claim_or_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            _payload, claim, identity, controller = self.quarantine_attestation_state(
                state
            )
            other = WorkerClaim("batch0001", 30773, 2, 6, 8)
            common = {
                "controller_session": controller,
                "identity_reader": lambda _pid: identity,
                "worker_assertion": lambda *_args, **_kwargs: identity.command,
                "tree_discoverer": lambda item: {item.pid: item},
            }
            with self.assertRaisesRegex(
                drain.BoundaryDrainError, "another port 30773 formal worker claim"
            ):
                drain.attest_quarantine(
                    state,
                    root / "claim.json",
                    **common,
                    claim_discoverer=lambda: {
                        claim: (identity.pid,),
                        other: (123456,),
                    },
                    controller_lister=lambda _screen: (controller,),
                )
            with self.assertRaisesRegex(
                drain.BoundaryDrainError, "another port 30773 formal controller"
            ):
                drain.attest_quarantine(
                    state,
                    root / "controller.json",
                    **common,
                    claim_discoverer=lambda: {claim: (identity.pid,)},
                    controller_lister=lambda _screen: (
                        controller,
                        "7654.total_asset_batch0001_g2",
                    ),
                )

    def test_quarantine_attestation_is_narrow_to_exact_retry_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            payload, _claim = self.boundary_state(state)
            payload.pop("boundary_proof")
            state.write_text(json.dumps(payload), encoding="utf-8")
            controller = str(payload["controller_session"])
            identity = drain.ProcessIdentity(
                int(payload["worker_pid"]),
                4321,
                "T",
                str(payload["worker_start_time"]),
                str(payload["worker_command"]),
            )
            with self.assertRaisesRegex(
                drain.BoundaryDrainError, "approved legacy claim"
            ):
                drain.attest_quarantine(
                    state,
                    root / "attestation.json",
                    controller_session=controller,
                    identity_reader=lambda _pid: identity,
                )

    def test_cleanup_rejects_external_boundary_without_worker_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            payload, _claim = self.boundary_state(state)
            payload.pop("boundary_proof")
            state.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                drain.BoundaryDrainError, "not a proven single-asset boundary"
            ):
                drain.cleanup_boundary(
                    state,
                    controller_session="4321.total_asset_batch0001_g2",
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    credential_validator=lambda **_kwargs: None,
                )

    def test_quarantine_controller_accepts_only_exact_retry_alias(self) -> None:
        retry = "4321.total_asset_batch0001_g2_retry"
        self.assertEqual(
            drain.validate_quarantine_controller_token(
                retry, batch="batch0001", remote_port=30773, gpu=2
            ),
            "total_asset_batch0001_g2_retry",
        )
        with self.assertRaises(drain.BoundaryDrainError):
            drain.validate_controller_token(
                retry, batch="batch0001", remote_port=30773, gpu=2
            )
        for invalid in (
            "4321.total_asset_batch0001_g2_handoff",
            "4321.total_asset_batch0001_g2_retry_extra",
            "4321.total_asset_batch0001_g3_retry",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                drain.BoundaryDrainError
            ):
                drain.validate_quarantine_controller_token(
                    invalid, batch="batch0001", remote_port=30773, gpu=2
                )

    def fake_screen(self, root: Path, *, present: bool = False) -> Path:
        marker = root / "controller-present"
        if present:
            marker.write_text("1", encoding="utf-8")
        calls = root / "screen-calls"
        screen = root / "screen"
        screen.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = '-ls' ]; then\n"
            f"  if [ -e '{marker}' ]; then printf '\\t4321.total_asset_batch0001_g2\\t(Detached)\\n'; fi\n"
            "  exit 0\n"
            "fi\n"
            f"printf '%s\\n' \"$*\" >>'{calls}'\n"
            f"rm -f '{marker}'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        screen.chmod(0o755)
        return screen

    def test_cleanup_is_idempotent_and_remote_failure_does_not_consume_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            _payload, claim = self.boundary_state(state)
            screen = self.fake_screen(root, present=True)
            with mock.patch.object(drain, "process_identity", return_value=None):
                with self.assertRaises(drain.BoundaryDrainError):
                    drain.cleanup_boundary(
                        state,
                        controller_session="4321.total_asset_batch0001_g2",
                        host="root@example",
                        known_hosts=root / "known_hosts",
                        identity_file=root / "key",
                        ssh_timeout_seconds=1,
                        credential_validator=lambda **_kwargs: None,
                        screen_bin=str(screen),
                        claim_detector=lambda: (),
                        holder_present=lambda *_args, **_kwargs: False,
                        remote_verifier=mock.Mock(
                            side_effect=RemoteProbeError("ssh_unavailable")
                        ),
                    )
            blocked = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(blocked["status"], "cleanup_blocked")
            self.assertIn("ssh_unavailable", blocked["error"])

            verified: list[WorkerClaim] = []
            with mock.patch.object(drain, "process_identity", return_value=None):
                drain.cleanup_boundary(
                    state,
                    controller_session="4321.total_asset_batch0001_g2",
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    credential_validator=lambda **_kwargs: None,
                    screen_bin=str(screen),
                    claim_detector=lambda: (),
                    holder_present=lambda *_args, **_kwargs: False,
                    remote_verifier=lambda item, **_kwargs: verified.append(item),
                )
                # A completed cleanup is still re-audited, not blindly trusted.
                drain.cleanup_boundary(
                    state,
                    controller_session="4321.total_asset_batch0001_g2",
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    credential_validator=lambda **_kwargs: None,
                    screen_bin=str(screen),
                    claim_detector=lambda: (),
                    holder_present=lambda *_args, **_kwargs: False,
                    remote_verifier=lambda item, **_kwargs: verified.append(item),
                )
            self.assertEqual(verified, [claim, claim])
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["status"],
                "cleanup_complete",
            )

    def test_cleanup_fails_closed_on_unexpected_exit_and_exact_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            self.boundary_state(state, status="worker_exited_unexpected")
            with self.assertRaises(drain.BoundaryDrainError):
                drain.cleanup_boundary(
                    state,
                    controller_session="4321.total_asset_batch0001_g2",
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    credential_validator=lambda **_kwargs: None,
                    screen_bin=str(self.fake_screen(root)),
                )

            _payload, claim = self.boundary_state(state)
            with mock.patch.object(drain, "process_identity", return_value=None):
                with self.assertRaises(drain.BoundaryDrainError):
                    drain.cleanup_boundary(
                        state,
                        controller_session="4321.total_asset_batch0001_g2",
                        host="root@example",
                        known_hosts=root / "known_hosts",
                        identity_file=root / "key",
                        ssh_timeout_seconds=1,
                        credential_validator=lambda **_kwargs: None,
                        screen_bin=str(self.fake_screen(root)),
                        claim_detector=lambda: (claim,),
                        holder_present=lambda *_args, **_kwargs: False,
                        remote_verifier=lambda *_args, **_kwargs: None,
                    )
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["status"],
                "cleanup_blocked",
            )

    def test_pid_reuse_is_never_signalled(self) -> None:
        original = drain.ProcessIdentity(
            999999, 1, "T", "Thu Jul 17 12:00:00 2026", "python worker.py"
        )
        reused = drain.ProcessIdentity(
            999999, 1, "S", "Thu Jul 17 12:01:00 2026", "python unrelated.py"
        )
        with mock.patch.object(drain, "process_identity", return_value=reused), \
             mock.patch.object(drain.os, "kill") as kill:
            with self.assertRaises(drain.BoundaryDrainError):
                drain.signal_identities((original,), signal.SIGKILL)
        kill.assert_not_called()

    def test_state_and_reparent_changes_do_not_look_like_pid_reuse(self) -> None:
        original = drain.ProcessIdentity(
            999998, 4321, "T", "Thu Jul 17 12:00:00 2026", "python worker.py"
        )
        continued = drain.ProcessIdentity(
            999998, 1, "S", "Thu Jul 17 12:00:00 2026", "python worker.py"
        )
        with mock.patch.object(drain, "process_identity", return_value=continued), \
             mock.patch.object(drain.os, "kill") as kill:
            drain.signal_identities((original,), signal.SIGCONT)
        kill.assert_called_once_with(original.pid, signal.SIGCONT)

    def test_real_stopped_process_is_terminated_and_continued(self) -> None:
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        reaper = threading.Thread(target=worker.wait, daemon=True)
        try:
            # Let the child finish exec before sampling the stable command/start
            # identity; heavily loaded macOS runners can otherwise expose the
            # transient posix_spawn state for the first sample.
            time.sleep(0.1)
            identity = drain.process_identity(worker.pid)
            self.assertIsNotNone(identity)
            assert identity is not None
            os.kill(worker.pid, signal.SIGSTOP)
            deadline = time.monotonic() + 5
            stopped = drain.process_identity(worker.pid)
            while (
                (stopped is None or "T" not in stopped.state)
                and time.monotonic() < deadline
            ):
                os.kill(worker.pid, signal.SIGSTOP)
                time.sleep(0.01)
                stopped = drain.process_identity(worker.pid)
            self.assertIsNotNone(stopped)
            assert stopped is not None
            if "T" not in stopped.state:
                self.fail(f"process did not enter stopped state: {stopped.state!r}")
            reaper.start()
            drain.terminate_exact_process_tree(
                stopped, term_grace_seconds=2, kill_grace_seconds=1
            )
            reaper.join(timeout=2)
            self.assertIsNotNone(worker.returncode)
        finally:
            if worker.poll() is None:
                os.kill(worker.pid, signal.SIGKILL)
                os.kill(worker.pid, signal.SIGCONT)
                worker.wait(timeout=5)

    def test_bad_screen_rc1_is_not_treated_as_no_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            screen = Path(temp) / "screen"
            screen.write_text(
                "#!/bin/sh\necho 'permission denied' >&2\nexit 1\n",
                encoding="utf-8",
            )
            screen.chmod(0o755)
            with self.assertRaises(drain.BoundaryDrainError):
                drain.listed_controller_tokens(str(screen))

    def test_orchestrator_blocks_external_watch_and_uses_only_worker_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inventory = root / "inventory"
            states = root / "states"
            inventory.mkdir()
            claim = WorkerClaim("batch0001", 30773, 2, 6, 8)
            with mock.patch.object(
                drain, "discover_claimed_workers", return_value={claim: (123456,)}
            ), mock.patch.object(
                drain, "assert_exact_worker",
                return_value=(
                    "python run_total_asset_render_worker.py --batch batch0001 "
                    "--remote-port 30773 --gpu 2 --worker-index 6 --worker-count 8"
                ),
            ), mock.patch.object(drain.subprocess, "Popen") as popen:
                with self.assertRaises(drain.BoundaryDrainError):
                    drain.orchestrate_boundary_watches(
                        inventory=inventory,
                        batch=claim.batch,
                        worker_count=8,
                        state_root=states,
                    )
            popen.assert_not_called()
            group = json.loads(
                (states / "batch0001.wc8.watch_group.json").read_text(encoding="utf-8")
            )
            self.assertEqual(group["status"], "blocked_uncooperative_legacy")
            self.assertEqual(len(group["slots"]), 1)

            marker = scheduler.create_drain_request(inventory, claim.batch, 8)
            drain_path = scheduler.worker_drain_path(inventory, claim.batch, 8)
            self.assertEqual(marker["status"], "drain_requested")
            with mock.patch.object(
                drain, "discover_claimed_workers", return_value={claim: (123456,)}
            ), mock.patch.object(
                drain, "assert_exact_worker",
                return_value=(
                    "python run_total_asset_render_worker.py --batch batch0001 "
                    "--remote-port 30773 --gpu 2 --worker-index 6 --worker-count 8 "
                    f"--drain-file {drain_path}"
                ),
            ), mock.patch.object(drain.subprocess, "Popen") as popen:
                drain.orchestrate_boundary_watches(
                    inventory=inventory,
                    batch=claim.batch,
                    worker_count=8,
                    state_root=states,
                )
            popen.assert_not_called()
            group = json.loads(
                (states / "batch0001.wc8.watch_group.json").read_text(encoding="utf-8")
            )
            self.assertEqual(group["status"], "cooperative_drain_pending")

    def test_cleanup_records_and_releases_only_the_exact_slot_holder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            _payload, claim = self.boundary_state(state)
            released: list[WorkerClaim] = []
            with mock.patch.object(drain, "process_identity", return_value=None):
                drain.cleanup_boundary(
                    state,
                    controller_session="4321.total_asset_batch0001_g2",
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    credential_validator=lambda **_kwargs: None,
                    screen_bin=str(self.fake_screen(root)),
                    claim_detector=lambda: (),
                    holder_present=lambda item, **_kwargs: item == claim,
                    holder_releaser=lambda item, **_kwargs: released.append(item),
                    remote_verifier=lambda *_args, **_kwargs: None,
                )
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(released, [claim])
            self.assertEqual(payload["holder_release"], {
                "port": 30773,
                "gpu": 2,
                "session": "total_asset_gpu_holder_g2",
                "state": "released",
            })

    def test_stop_pending_reconciles_a_previously_stopped_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            payload, _claim = self.boundary_state(state, status="stop_pending")
            identity = drain.ProcessIdentity(
                int(payload["worker_pid"]),
                1,
                "T",
                str(payload["worker_start_time"]),
                str(payload["worker_command"]),
            )
            with mock.patch.object(drain, "process_identity", return_value=identity), \
                 mock.patch.object(drain, "assert_exact_worker"), \
                 mock.patch.object(drain.os, "kill") as kill:
                drain.reconcile_stop_pending(state, payload)
            kill.assert_not_called()
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["status"],
                "boundary_stopped",
            )

    def test_cleanup_bad_credentials_performs_no_destructive_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            original, _claim = self.boundary_state(state)
            tree = mock.Mock()
            holder = mock.Mock()
            with self.assertRaises(Exception):
                drain.cleanup_boundary(
                    state,
                    controller_session="4321.total_asset_batch0001_g2",
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    screen_bin=str(self.fake_screen(root, present=True)),
                    credential_validator=mock.Mock(
                        side_effect=drain.PreflightInputError(
                            "ssh_identity_permissions_invalid"
                        )
                    ),
                    tree_terminator=tree,
                    holder_releaser=holder,
                )
            tree.assert_not_called()
            holder.assert_not_called()
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), original)

    def test_orchestrator_cannot_bypass_busy_lock_with_forged_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inventory = root / "inventory"
            inventory.mkdir()
            state_root = root / "states"
            lock_path = inventory / "total_asset_scheduler.lock"
            with scheduler.SchedulerLock(lock_path), mock.patch.dict(
                os.environ,
                {"TOTAL_ASSET_SCHEDULER_LOCK_HELD": "1"},
                clear=False,
            ), mock.patch.object(drain, "discover_claimed_workers") as discover:
                os.environ.pop("TOTAL_ASSET_SCHEDULER_LOCK_FD", None)
                with self.assertRaises(drain.BoundaryDrainError):
                    drain.orchestrate_boundary_watches(
                        inventory=inventory,
                        batch="batch0001",
                        worker_count=8,
                        state_root=state_root,
                    )
            discover.assert_not_called()

    def test_target_cleanup_probe_allows_other_gpu_exact_holder(self) -> None:
        claim = WorkerClaim("batch0001", 30773, 2, 6, 8)
        node = drain.NodeObservation(
            port=30773,
            probe_ok=True,
            holder_audit_ok=True,
            unexpected_holder_sessions=0,
            legacy_holder_sessions=0,
            gpus=tuple(
                drain.GpuObservation(
                    gpu=gpu,
                    gpu_query_ok=True,
                    compute_query_ok=True,
                    compute_process_count=1 if gpu == 1 else 0,
                    compute_process_kinds=("other",) if gpu == 1 else (),
                    holder_session=gpu == 1,
                    lock_state="busy" if gpu == 1 else "available",
                    wrapper_process_present=False,
                )
                for gpu in range(4)
            ),
        )
        with mock.patch.object(drain, "run_ssh_probe", return_value=node) as probe:
            drain.verify_remote_slot_clean(
                claim,
                host="root@example",
                known_hosts=Path("known_hosts"),
                identity_file=Path("key"),
                timeout_seconds=1,
            )
        self.assertEqual(probe.call_args.args[1], (0, 1, 2, 3))

    def test_quarantine_remote_failure_has_zero_local_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            payload, _claim = self.boundary_state(state)
            payload.pop("boundary_proof")
            payload["controller_session"] = (
                "4321.total_asset_batch0001_g2_retry"
            )
            state.write_text(json.dumps(payload), encoding="utf-8")
            identity = drain.ProcessIdentity(
                int(payload["worker_pid"]),
                4321,
                "T",
                "Thu Jul 17 12:00:00 2026",
                str(payload["worker_command"]),
            )
            quit_controller = mock.Mock()
            terminate = mock.Mock()
            with mock.patch.object(drain, "process_identity", return_value=identity), \
                 mock.patch.object(drain, "assert_exact_worker"):
                with self.assertRaises(drain.RemoteProbeError):
                    drain.cleanup_quarantined_legacy_idle(
                        state,
                        controller_session=(
                            "4321.total_asset_batch0001_g2_retry"
                        ),
                        host="root@example",
                        known_hosts=root / "known_hosts",
                        identity_file=root / "key",
                        ssh_timeout_seconds=1,
                        credential_validator=lambda **_kwargs: None,
                        remote_verifier=mock.Mock(
                            side_effect=drain.RemoteProbeError("ssh_unavailable")
                        ),
                        controller_quitter=quit_controller,
                        tree_terminator=terminate,
                    )
            quit_controller.assert_not_called()
            terminate.assert_not_called()
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), payload)

    def test_quarantine_retry_rejects_persisted_controller_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            payload, _claim = self.boundary_state(state)
            payload.pop("boundary_proof")
            state.write_text(json.dumps(payload), encoding="utf-8")
            remote = mock.Mock()
            with self.assertRaisesRegex(
                drain.BoundaryDrainError,
                "controller session changed across quarantine retries",
            ):
                drain.cleanup_quarantined_legacy_idle(
                    state,
                    controller_session="4321.total_asset_batch0001_g2_retry",
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    credential_validator=lambda **_kwargs: None,
                    remote_verifier=remote,
                )
            remote.assert_not_called()
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8")), payload
            )

    def test_quarantine_allows_revalidated_zombie_child_and_never_signals_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            payload, claim = self.boundary_state(state)
            payload.pop("boundary_proof")
            payload["controller_session"] = (
                "4321.total_asset_batch0001_g2_retry"
            )
            state.write_text(json.dumps(payload), encoding="utf-8")
            identity = drain.ProcessIdentity(
                int(payload["worker_pid"]),
                4321,
                "T",
                "Thu Jul 17 12:00:00 2026",
                str(payload["worker_command"]),
            )
            zombie = drain.ProcessIdentity(
                5620,
                identity.pid,
                "Z",
                "Thu Jul 17 12:00:01 2026",
                "[ssh] <defunct>",
            )
            controller = "4321.total_asset_batch0001_g2_retry"
            remote = mock.Mock()
            quit_controller = mock.Mock()
            terminate = mock.Mock()
            claims = mock.Mock(side_effect=[{claim: (identity.pid,)}, {}])
            with mock.patch.object(
                 drain,
                 "process_identity",
                 side_effect=lambda pid: zombie if pid == zombie.pid else identity,
                 ), \
                 mock.patch.object(drain, "assert_exact_worker"), \
                 mock.patch.object(
                     drain, "listed_controller_tokens",
                     side_effect=[(controller,), ()],
                 ):
                drain.cleanup_quarantined_legacy_idle(
                    state,
                    controller_session=controller,
                    host="root@example",
                    known_hosts=root / "known_hosts",
                    identity_file=root / "key",
                    ssh_timeout_seconds=1,
                    credential_validator=lambda **_kwargs: None,
                    remote_verifier=remote,
                    claim_discoverer=claims,
                    tree_discoverer=lambda item: {
                        item.pid: item,
                        zombie.pid: zombie,
                    },
                    controller_quitter=quit_controller,
                    tree_terminator=terminate,
                )
            self.assertEqual(remote.call_count, 2)
            quit_controller.assert_called_once_with(
                controller,
                expected_name="total_asset_batch0001_g2_retry",
                screen_bin="screen",
            )
            terminate.assert_called_once()
            self.assertEqual(
                set(terminate.call_args.kwargs["tracked"]),
                {identity.pid},
            )
            final = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "quarantine_cleanup_complete")
            self.assertEqual(final["boundary_claim"], "not_cooperative")
            self.assertEqual(
                final["asset_disposition"],
                "pending_or_existing_terminal_unchanged",
            )

    def test_quarantine_blocks_any_non_zombie_child_before_local_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state.json"
            payload, _claim = self.boundary_state(state)
            payload.pop("boundary_proof")
            state.write_text(json.dumps(payload), encoding="utf-8")
            identity = drain.ProcessIdentity(
                int(payload["worker_pid"]),
                4321,
                "T",
                "Thu Jul 17 12:00:00 2026",
                str(payload["worker_command"]),
            )
            live_child = drain.ProcessIdentity(
                5621,
                identity.pid,
                "S",
                "Thu Jul 17 12:00:01 2026",
                "ssh active-render",
            )
            quit_controller = mock.Mock()
            terminate = mock.Mock()
            remote = mock.Mock()
            with mock.patch.object(
                drain,
                "process_identity",
                side_effect=lambda pid: (
                    live_child if pid == live_child.pid else identity
                ),
            ), mock.patch.object(drain, "assert_exact_worker"):
                with self.assertRaisesRegex(
                    drain.BoundaryDrainError, "active local child tree"
                ):
                    drain.cleanup_quarantined_legacy_idle(
                        state,
                        controller_session="4321.total_asset_batch0001_g2",
                        host="root@example",
                        known_hosts=root / "known_hosts",
                        identity_file=root / "key",
                        ssh_timeout_seconds=1,
                        credential_validator=lambda **_kwargs: None,
                        remote_verifier=remote,
                        tree_discoverer=lambda item: {
                            item.pid: item,
                            live_child.pid: live_child,
                        },
                        controller_quitter=quit_controller,
                        tree_terminator=terminate,
                    )
            self.assertEqual(remote.call_count, 1)
            quit_controller.assert_not_called()
            terminate.assert_not_called()
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), payload)


if __name__ == "__main__":
    unittest.main()
