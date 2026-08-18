from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_profile_capabilities as capabilities  # noqa: E402


BOOT = "11111111-2222-3333-4444-555555555555"
OTHER_BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GPU_UUID = "gpu-54fad6b2-bbad-95b8-ea94-cc94237c8860"


class DummyRemote:
    port = "30808"


class TotalAssetProfileCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state" / "profiles.json"
        self.identity_file = self.root / "id_ed25519"
        self.identity_file.write_text("private-placeholder\n", encoding="utf-8")
        self.identity_file.chmod(0o600)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            "[render.invalid]:30808 ssh-ed25519 AAAATEST\n", encoding="utf-8"
        )
        self.known_hosts.chmod(0o600)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def boot_probe(self, *_args: object, **_kwargs: object) -> str:
        self.calls.append("boot")
        return BOOT

    def remote_factory(self, *_args: object, **_kwargs: object) -> DummyRemote:
        self.calls.append("remote")
        return DummyRemote()

    def identity_query(self, _remote: object, gpu: int) -> SimpleNamespace:
        self.calls.append("identity")
        return SimpleNamespace(index=gpu, uuid=GPU_UUID)

    def profile_reader(
        self,
        remote: object,
        identity: object,
        family: str,
        *,
        blender_binary: str,
    ) -> dict[str, object]:
        self.calls.append(f"profile:{family}")
        return {
            "port": int(remote.port),
            "gpu": identity.index,
            "gpu_uuid": identity.uuid,
            "family": family,
            "profile": f"/root/profiles/{family}/current",
            "ready": True,
            "runtime_ready": True,
            "manifest": {
                "family": family,
                "gpu_index": identity.index,
                "gpu_uuid": identity.uuid,
                "blender_binary": blender_binary,
                "blender_binary_sha256": "a" * 64,
                "profile_userpref_sha256": "b" * 64,
            },
        }

    def probe(self, **overrides: object):
        arguments = {
            "state_path": self.state,
            "ssh_host": "root@render.invalid",
            "identity_file": self.identity_file,
            "known_hosts": self.known_hosts,
            "remote_port": 30808,
            "gpu": 1,
            "expected_boot_id": BOOT,
            "exact_holder_attested": True,
            "now_epoch": 1000.0,
            "ttl_seconds": 300,
            "boot_probe": self.boot_probe,
            "remote_factory": self.remote_factory,
            "identity_query": self.identity_query,
            "profile_reader": self.profile_reader,
            "blender_binaries": {
                "4.5": "/opt/blender-4.5/blender",
                "5.1": "/opt/blender-5.1/blender",
            },
        }
        arguments.update(overrides)
        return capabilities.strict_slot_capabilities(**arguments)

    def test_exact_live_probe_authorizes_both_families_and_writes_private_cache(self) -> None:
        result = self.probe()
        self.assertEqual(result.attested_vulkan_families, ("4.5", "5.1"))
        self.assertEqual(self.calls.count("boot"), 2)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        entry = payload["entries"]["30808:1"]
        self.assertEqual(entry["boot_id"], BOOT)
        self.assertEqual(entry["gpu_uuid"], GPU_UUID)
        self.assertEqual(set(entry["families"]), {"4.5", "5.1"})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_fresh_cache_is_bound_to_boot_and_skips_remote_probe(self) -> None:
        self.probe()
        self.calls.clear()
        result = self.probe(
            now_epoch=1100.0,
            boot_probe=lambda *_args, **_kwargs: self.fail("cache must not SSH"),
            remote_factory=lambda *_args, **_kwargs: self.fail("cache must not SSH"),
        )
        self.assertEqual(result.attested_vulkan_families, ("4.5", "5.1"))
        self.assertEqual(self.calls, [])

        mismatch = self.probe(
            expected_boot_id=OTHER_BOOT,
            now_epoch=1100.0,
            boot_probe=lambda *_args, **_kwargs: BOOT,
        )
        self.assertEqual(mismatch.attested_vulkan_families, ())

    def test_old_bare_or_handoff_holder_never_runs_profile_probe(self) -> None:
        result = self.probe(
            exact_holder_attested=False,
            boot_probe=lambda *_args, **_kwargs: self.fail("bare holder cannot probe"),
            remote_factory=lambda *_args, **_kwargs: self.fail("bare holder cannot probe"),
        )
        self.assertEqual(result.attested_vulkan_families, ())
        self.assertFalse(self.state.exists())

    def test_wrong_gpu_or_untrusted_profile_fields_fail_closed(self) -> None:
        wrong_identity = self.probe(
            identity_query=lambda _remote, gpu: SimpleNamespace(
                index=gpu + 1, uuid=GPU_UUID
            )
        )
        self.assertEqual(wrong_identity.attested_vulkan_families, ())

        def wrong_binary(
            remote: object,
            identity: object,
            family: str,
            *,
            blender_binary: str,
        ) -> dict[str, object]:
            payload = self.profile_reader(
                remote, identity, family, blender_binary=blender_binary
            )
            payload["manifest"]["blender_binary"] = "/untrusted/blender"
            return payload

        wrong_profile = self.probe(profile_reader=wrong_binary)
        self.assertEqual(wrong_profile.attested_vulkan_families, ())

    def test_expired_or_malformed_cache_never_authorizes_without_live_evidence(self) -> None:
        self.state.parent.mkdir(parents=True)
        self.state.write_text("not-json\n", encoding="utf-8")
        self.state.chmod(0o600)
        result = self.probe(
            boot_probe=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("offline")
            )
        )
        self.assertEqual(result.attested_vulkan_families, ())

        self.state.unlink()
        self.probe()
        expired = self.probe(
            now_epoch=1401.0,
            boot_probe=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("offline")
            ),
        )
        self.assertEqual(expired.attested_vulkan_families, ())

    def test_key_only_remote_forbids_password_fallback_and_redacts_failure(self) -> None:
        observed: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object):
            observed.append(command)
            return subprocess.CompletedProcess(
                command, 255, "secret stdout", "secret stderr"
            )

        remote = capabilities.KeyOnlyProfileRemote(
            30808,
            host="root@render.invalid",
            identity_file=self.identity_file,
            known_hosts=self.known_hosts,
            runner=runner,
        )
        with self.assertRaisesRegex(
            capabilities.ProfileCapabilityError,
            "profile_remote_command_failed",
        ) as caught:
            remote.run("true")
        rendered = " ".join(observed[0])
        self.assertIn("PasswordAuthentication=no", rendered)
        self.assertIn("PreferredAuthentications=publickey", rendered)
        self.assertNotIn("secret stdout", str(caught.exception))
        self.assertNotIn("secret stderr", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
