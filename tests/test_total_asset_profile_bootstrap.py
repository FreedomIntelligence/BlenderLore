from __future__ import annotations

import contextlib
import json
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_profile_bootstrap as bootstrap  # noqa: E402
import total_asset_holder_transaction as holder_transaction  # noqa: E402


BOOT = "11111111-2222-3333-4444-555555555555"
OTHER_BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GPU_UUIDS = {
    0: "gpu-00000000-0000-0000-0000-000000000000",
    1: "gpu-11111111-1111-1111-1111-111111111111",
    2: "gpu-22222222-2222-2222-2222-222222222222",
    3: "gpu-33333333-3333-3333-3333-333333333333",
}
TOPOLOGY = {
    (31722, 0): 0,
    (31722, 1): 1,
    (31722, 2): 2,
    (31722, 3): 3,
    (30773, 0): 4,
    (30773, 1): 5,
    (30773, 2): 6,
    (30773, 3): 7,
    (30808, 0): 8,
    (30808, 1): 9,
    (30808, 2): 10,
}


class FakeRemote:
    port = "31722"

    def __init__(self, ready: set[str], calls: list[str]) -> None:
        self.ready = ready
        self.calls = calls
        self.fail_family: str | None = None

    def run(self, command: str, timeout: int = 30) -> str:
        del timeout
        self.calls.append(command)
        if command.startswith("build:"):
            family = command.split(":", 1)[1]
            if family == self.fail_family:
                raise RuntimeError("safe fake failure")
            self.ready.add(family)
            return "{}\n"
        return ""


class TotalAssetProfileBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity = self.root / "id_ed25519"
        self.identity.write_text("private-placeholder\n", encoding="utf-8")
        self.identity.chmod(0o600)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            "[render.invalid]:31722 ssh-ed25519 AAAATEST\n", encoding="utf-8"
        )
        self.known_hosts.chmod(0o600)
        self.config = bootstrap.ProfileBootstrapConfig(
            remote_port=31722,
            gpu=1,
            ssh_host="root@render.invalid",
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            guard_state=self.root / "guard.json",
            slot_lock_root=self.root / "slot-locks",
            transaction_root=self.root / "transactions",
            pin_state=self.root / "profile-cache-pins.json",
            capability_state=self.root / "profile-capabilities.json",
            provision_state=self.root / "node-provision.json",
            remote_root=PurePosixPath("/persistent/bootstrap"),
        )
        self.calls: list[str] = []
        self.ready: set[str] = set()
        self.sessions = {0, 1}
        self.remote = FakeRemote(self.ready, self.calls)
        self.release_failure = False
        self.restore_failure = False
        self.boot_after_release = BOOT
        self.change_target_uuid_after_release = False
        self.change_target_uuid_after_restore = False
        self.restored_obligations: tuple[tuple[int, int], ...] = ()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def guard_loader(self, *_args: object, **kwargs: object):
        observed = float(kwargs["now_epoch"])
        return {
            0: bootstrap.GuardSlotEvidence(
                31722, 0, 0, "holder_ready", "exact", BOOT, observed
            ),
            1: bootstrap.GuardSlotEvidence(
                31722, 1, 1, "holder_ready", "exact", BOOT, observed
            ),
        }

    def boot_probe(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append("boot")
        return self.boot_after_release if "release" in self.calls else BOOT

    @contextlib.contextmanager
    def slot_lock(self, port: int, gpu: int, **_kwargs: object):
        self.calls.append(f"lock:{port}:{gpu}")
        try:
            yield object()
        finally:
            self.calls.append(f"unlock:{port}:{gpu}")

    def session_present(self, session: str, **_kwargs: object) -> bool:
        match = re.search(r"_holder_g(\d+)$", session)
        if match is None:
            raise AssertionError(f"unexpected holder session: {session}")
        gpu = int(match.group(1))
        return gpu in self.sessions

    def holder_release(self, state: Path, slots, **_kwargs: object) -> None:
        del state, slots
        self.calls.append("release")
        self.sessions.discard(0)
        if self.release_failure:
            raise RuntimeError("partial release")
        self.sessions.discard(1)

    def holder_restore(self, state: Path, started, **_kwargs: object) -> None:
        del started
        self.calls.append("restore")
        if self.restore_failure:
            raise RuntimeError("restore failure")
        self.restored_obligations = bootstrap.load_holder_transaction(state)
        self.sessions.update((0, 1))

    def identity_query(self, _remote: object, gpu: int):
        self.calls.append(f"identity:{gpu}")
        uuid = GPU_UUIDS[gpu]
        if self.change_target_uuid_after_release and gpu == 1 and "release" in self.calls:
            uuid = "gpu-aaaaaaaa-1111-1111-1111-111111111111"
        if self.change_target_uuid_after_restore and gpu == 1 and "restore" in self.calls:
            uuid = "gpu-bbbbbbbb-1111-1111-1111-111111111111"
        return SimpleNamespace(
            index=gpu,
            uuid=uuid,
            pci_selector=f"pci-0000_{gpu + 1:02x}_00_0",
            vendor="10de",
            device="2b85",
            gpu_count=4,
        )

    def profile_status(
        self,
        remote: object,
        identity: object,
        family: str,
        *,
        blender_binary: str,
    ):
        del blender_binary
        self.calls.append(f"status:{family}")
        return {
            "port": int(remote.port),
            "gpu": identity.index,
            "gpu_uuid": identity.uuid,
            "family": family,
            "runtime_ready": True,
            "ready": family in self.ready,
        }

    @staticmethod
    def frozen(_remote: object, *, remote_port: int, remote_root: PurePosixPath):
        del remote_root
        return {
            "schema": bootstrap.FREEZE_SCHEMA,
            "status": "ready",
            "port": remote_port,
            "profile": {
                "cache_name": bootstrap.PROFILE_CACHE_NAME,
                "destination": bootstrap.PINNED_PROFILE_DESTINATION,
                "manifest_sha256": "a" * 64,
                "directory_mode": 0o555,
                "manifest_mode": 0o444,
            },
            "device_select": {
                "cache_name": bootstrap.DEVICE_SELECT_CACHE_NAME,
                "destination": bootstrap.PINNED_DEVICE_SELECT_DESTINATION,
                "manifest_sha256": "b" * 64,
                "directory_mode": 0o555,
                "manifest_mode": 0o444,
            },
        }

    def dependencies(self) -> bootstrap.Dependencies:
        return bootstrap.Dependencies(
            now=lambda: 1000.0,
            boot_probe=self.boot_probe,
            guard_loader=self.guard_loader,
            lock=self.slot_lock,
            session_present=self.session_present,
            transaction_init=bootstrap.initialize_holder_transaction,
            transaction_load=bootstrap.load_holder_transaction,
            holder_release=self.holder_release,
            holder_restore=self.holder_restore,
            remote_factory=lambda *_args, **_kwargs: self.remote,
            identity_query=self.identity_query,
            profile_status=self.profile_status,
            profile_command=lambda **kwargs: f"build:{kwargs['family']}",
            cache_freeze=self.frozen,
        )

    def build(self, dependency: bootstrap.Dependencies | None = None):
        with mock.patch.object(
            bootstrap, "CANONICAL_WORKER_INDEX_BY_LOCATION", TOPOLOGY
        ):
            return bootstrap.build_slot(
                self.config,
                blender_binaries={"4.5": "/opt/blender45", "5.1": "/opt/blender51"},
                dependency=dependency or self.dependencies(),
            )

    def test_build_releases_gpu0_and_target_then_restores_and_pins(self) -> None:
        payload = self.build()
        self.assertEqual(payload["built_families"], ["4.5", "5.1"])
        self.assertTrue(payload["holders_restored"])
        self.assertEqual(self.sessions, {0, 1})
        self.assertLess(self.calls.index("lock:31722:0"), self.calls.index("lock:31722:1"))
        self.assertLess(self.calls.index("release"), self.calls.index("build:4.5"))
        self.assertLess(self.calls.index("build:5.1"), self.calls.index("restore"))
        self.assertLess(self.calls.index("restore"), self.calls.index("unlock:31722:1"))
        pins = json.loads(self.config.pin_state.read_text(encoding="utf-8"))
        node = pins["nodes"]["31722"]
        self.assertEqual(node["source_boot_id"], BOOT)
        self.assertEqual(node["gpu_uuids"], {"0": GPU_UUIDS[0], "1": GPU_UUIDS[1]})
        self.assertEqual(node["profile"]["manifest_sha256"], "a" * 64)
        self.assertEqual(self.config.pin_state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config.transaction_root.glob("*.json")), [])

    def test_already_ready_profiles_freeze_without_releasing_holders(self) -> None:
        self.ready.update(("4.5", "5.1"))
        payload = self.build()
        self.assertEqual(payload["built_families"], [])
        self.assertEqual(payload["already_ready_families"], ["4.5", "5.1"])
        self.assertNotIn("release", self.calls)
        self.assertNotIn("restore", self.calls)
        self.assertEqual(self.sessions, {0, 1})

    def test_profile_failure_restores_both_holders_and_writes_no_pins(self) -> None:
        self.remote.fail_family = "5.1"
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_build_5_1_failed"
        ):
            self.build()
        self.assertIn("restore", self.calls)
        self.assertEqual(self.sessions, {0, 1})
        self.assertEqual(
            set(self.restored_obligations), {(31722, 0), (31722, 1)}
        )
        self.assertFalse(self.config.pin_state.exists())

    def test_cache_freeze_failure_restores_both_holders(self) -> None:
        def freeze_failure(*_args: object, **_kwargs: object):
            raise bootstrap.ProfileBootstrapError("profile_cache_freeze_failed")

        dep = self.dependencies()
        dep.cache_freeze = freeze_failure
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_cache_freeze_failed"
        ):
            self.build(dep)
        self.assertEqual(self.sessions, {0, 1})
        self.assertEqual(
            set(self.restored_obligations), {(31722, 0), (31722, 1)}
        )
        self.assertFalse(self.config.pin_state.exists())

    def test_partial_release_still_restores_both_predeclared_slots(self) -> None:
        self.release_failure = True
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_holder_release_failed"
        ):
            self.build()
        self.assertIn("restore", self.calls)
        self.assertEqual(self.sessions, {0, 1})
        self.assertEqual(
            set(self.restored_obligations), {(31722, 0), (31722, 1)}
        )

    def test_boot_change_after_release_restores_and_aborts_before_build(self) -> None:
        self.boot_after_release = OTHER_BOOT
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_boot_identity_changed"
        ):
            self.build()
        self.assertIn("restore", self.calls)
        self.assertNotIn("build:4.5", self.calls)
        self.assertEqual(self.sessions, {0, 1})

    def test_gpu_uuid_change_after_release_restores_and_aborts_before_build(self) -> None:
        self.change_target_uuid_after_release = True
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_gpu_inventory_changed"
        ):
            self.build()
        self.assertIn("restore", self.calls)
        self.assertNotIn("build:4.5", self.calls)
        self.assertEqual(self.sessions, {0, 1})

    def test_restore_failure_takes_precedence_and_retains_recovery_state(self) -> None:
        self.remote.fail_family = "4.5"
        self.restore_failure = True
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_holder_restore_failed"
        ):
            self.build()
        self.assertEqual(len(list(self.config.transaction_root.glob("*.json"))), 1)

    def test_unresolved_transaction_on_same_node_blocks_before_release(self) -> None:
        unresolved = self.config.transaction_root / "p31722_g2_999.json"
        bootstrap.initialize_holder_transaction(
            unresolved,
            ((31722, 0), (31722, 2)),
        )
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_unresolved_transaction"
        ):
            self.build()
        self.assertNotIn("release", self.calls)
        self.assertEqual(self.sessions, {0, 1})

    def test_uuid_change_after_holder_restore_blocks_pin_publication(self) -> None:
        self.change_target_uuid_after_restore = True
        with self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_gpu_inventory_changed"
        ):
            self.build()
        self.assertEqual(self.sessions, {0, 1})
        self.assertFalse(self.config.pin_state.exists())
        self.assertEqual(list(self.config.transaction_root.glob("*.json")), [])

    def test_holder_transaction_attempts_every_predeclared_restore_slot(self) -> None:
        state = self.root / "restore.json"
        holder_transaction.atomic_write_state(
            state, ((31722, 0), (31722, 1))
        )
        attempted: list[tuple[int, int]] = []

        def start(port: int, gpu: int, **_kwargs: object) -> None:
            attempted.append((port, gpu))
            if gpu == 0:
                raise RuntimeError("gpu0 start failed")

        with mock.patch.object(holder_transaction, "start_holder_slot", start):
            with self.assertRaisesRegex(
                holder_transaction.HolderTransactionError,
                "holder_restore_incomplete",
            ):
                holder_transaction.restore_holders(
                    state,
                    set(),
                    host="root@render.invalid",
                    identity_file=self.identity,
                    known_hosts=self.known_hosts,
                )
        self.assertEqual(attempted, [(31722, 0), (31722, 1)])

    def test_non_exact_gpu0_holder_blocks_before_any_slot_mutation(self) -> None:
        def active_guard(*_args: object, **kwargs: object):
            payload = self.guard_loader(None, **kwargs)
            payload[0] = bootstrap.GuardSlotEvidence(
                31722, 0, 0, "worker_active", "renderer", BOOT, 1000.0
            )
            return payload

        dep = self.dependencies()
        dep.guard_loader = active_guard
        with mock.patch.object(
            bootstrap, "CANONICAL_WORKER_INDEX_BY_LOCATION", TOPOLOGY
        ), self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_exact_holders_required"
        ):
            bootstrap.build_slot(
                self.config,
                blender_binaries={"4.5": "/opt/b45", "5.1": "/opt/b51"},
                dependency=dep,
            )
        self.assertFalse(any(item.startswith("lock:") for item in self.calls))
        self.assertNotIn("release", self.calls)

    def test_local_owner_target_blocks_before_any_slot_mutation(self) -> None:
        def local_owner_guard(*_args: object, **kwargs: object):
            payload = self.guard_loader(None, **kwargs)
            payload[1] = bootstrap.GuardSlotEvidence(
                31722, 1, 1, "local_owner", "cycle72_worker_controller", BOOT, 1000.0
            )
            return payload

        dep = self.dependencies()
        dep.guard_loader = local_owner_guard
        with mock.patch.object(
            bootstrap, "CANONICAL_WORKER_INDEX_BY_LOCATION", TOPOLOGY
        ), self.assertRaisesRegex(
            bootstrap.ProfileBootstrapError, "profile_exact_holders_required"
        ):
            bootstrap.build_slot(
                self.config,
                blender_binaries={"4.5": "/opt/b45", "5.1": "/opt/b51"},
                dependency=dep,
            )
        self.assertFalse(any(item.startswith("lock:") for item in self.calls))
        self.assertNotIn("release", self.calls)

    def test_plan_is_strictly_local_and_reports_both_required_holders(self) -> None:
        dep = self.dependencies()
        dep.remote_factory = lambda *_args, **_kwargs: self.fail("plan cannot SSH")
        with mock.patch.object(
            bootstrap, "CANONICAL_WORKER_INDEX_BY_LOCATION", TOPOLOGY
        ):
            payload = bootstrap.plan_slot(self.config, dependency=dep)
        self.assertEqual(payload["required_holder_gpus"], [0, 1])
        self.assertEqual(payload["families"], ["4.5", "5.1"])

    def test_cache_freeze_command_is_builder_locked_and_uses_atomic_program(self) -> None:
        observed: list[tuple[str, int]] = []

        class Remote:
            def run(self, command: str, timeout: int = 30) -> str:
                observed.append((command, timeout))
                return json.dumps(TotalAssetProfileBootstrapTests.frozen(
                    self, remote_port=31722, remote_root=PurePosixPath("/persistent")
                ))

        payload = bootstrap.freeze_profile_caches(
            Remote(), remote_port=31722, remote_root=PurePosixPath("/persistent")
        )
        self.assertEqual(payload["status"], "ready")
        self.assertIn("total_asset_vulkan_profile_builder.lock", observed[0][0])
        self.assertIn("flock -n 7", observed[0][0])
        self.assertIn("os.rename(payload_root, destination)", observed[0][0])
        self.assertIn("os.replace(live_name, source / manifest_name)", observed[0][0])
        compile(bootstrap.REMOTE_FREEZE_PROGRAM, "<remote-freeze>", "exec")

    def test_pin_state_accumulates_same_boot_and_resets_on_new_boot(self) -> None:
        identities_01 = {
            index: bootstrap.IdentityEvidence(
                index=index,
                uuid=GPU_UUIDS[index],
                pci_selector=f"pci-0000_{index + 1:02x}_00_0",
                vendor="10de",
                device="2b85",
                gpu_count=4,
            )
            for index in (0, 1)
        }
        identities_02 = {
            index: bootstrap.IdentityEvidence(
                index=index,
                uuid=GPU_UUIDS[index],
                pci_selector=f"pci-0000_{index + 1:02x}_00_0",
                vendor="10de",
                device="2b85",
                gpu_count=4,
            )
            for index in (0, 2)
        }
        bootstrap._write_pin_state(
            self.config,
            boot_id=BOOT,
            identities=identities_01,
            frozen=self.frozen(
                self.remote, remote_port=31722, remote_root=self.config.remote_root
            ),
            now_epoch=1000.0,
        )
        bootstrap._write_pin_state(
            replace(self.config, gpu=2),
            boot_id=BOOT,
            identities=identities_02,
            frozen=self.frozen(
                self.remote, remote_port=31722, remote_root=self.config.remote_root
            ),
            now_epoch=1001.0,
        )
        pins = json.loads(self.config.pin_state.read_text(encoding="utf-8"))
        self.assertEqual(
            pins["nodes"]["31722"]["gpu_uuids"],
            {"0": GPU_UUIDS[0], "1": GPU_UUIDS[1], "2": GPU_UUIDS[2]},
        )

        identities_03 = {
            index: bootstrap.IdentityEvidence(
                index=index,
                uuid=GPU_UUIDS[index],
                pci_selector=f"pci-0000_{index + 1:02x}_00_0",
                vendor="10de",
                device="2b85",
                gpu_count=4,
            )
            for index in (0, 3)
        }
        bootstrap._write_pin_state(
            replace(self.config, gpu=3),
            boot_id=OTHER_BOOT,
            identities=identities_03,
            frozen=self.frozen(
                self.remote, remote_port=31722, remote_root=self.config.remote_root
            ),
            now_epoch=1002.0,
        )
        pins = json.loads(self.config.pin_state.read_text(encoding="utf-8"))
        self.assertEqual(
            pins["nodes"]["31722"]["gpu_uuids"],
            {"0": GPU_UUIDS[0], "3": GPU_UUIDS[3]},
        )


if __name__ == "__main__":
    unittest.main()
