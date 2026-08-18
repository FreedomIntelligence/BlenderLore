from __future__ import annotations

from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_remote_source_stager_launchd as launchd


class TotalAssetRemoteSourceStagerLaunchdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"
        self.inventory = self.assets / "_asset_inventory"
        self.inventory.mkdir(parents=True)
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.catalog.write_text("asset_id\n", encoding="utf-8")
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        sqlite3.connect(self.state_root / "leases.sqlite3").close()
        self.identity = self.root / "id_ed25519"
        self.identity.write_text("private key test fixture\n", encoding="utf-8")
        self.identity.chmod(0o600)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text("host ssh-ed25519 fixture\n", encoding="utf-8")
        self.known_hosts.chmod(0o644)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self) -> dict[str, object]:
        return launchd.build_plist(
            python_executable=Path(sys.executable).resolve(),
            asset_root=self.assets,
            catalog=self.catalog,
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            state_root=self.state_root,
            spool_root=self.root / "spool",
            log_path=self.root / "logs/stager.log",
            remote_port=30773,
        )

    def test_plist_is_key_only_zero_model_and_uses_canonical_topology(self) -> None:
        payload = self.payload()
        self.assertEqual(payload["Label"], launchd.LABEL)
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        arguments = payload["ProgramArguments"]
        self.assertIn("total_asset_remote_source_stager.py", " ".join(arguments))
        self.assertIn("run", arguments)
        for forbidden in ("--gpu", "--holder", "--slot-lock", "--lease"):
            self.assertNotIn(forbidden, arguments)
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["TOTAL_ASSET_ALLOW_LEGACY_PASSWORD_SSH"], "0")
        self.assertEqual(environment["TOTAL_ASSET_PREFLIGHT_SSH_KEY"], str(self.identity))
        self.assertEqual(
            environment["TOTAL_ASSET_SSH_KNOWN_HOSTS"], str(self.known_hosts)
        )
        self.assertEqual(environment["TOTAL_ASSET_REMOTE_PORT"], "30773")
        self.assertEqual(environment["TOTAL_ASSET_SECONDARY_PORT"], "31722")
        self.assertEqual(environment["TOTAL_ASSET_TERTIARY_PORT"], "30808")
        self.assertNotIn("30422", plistlib.dumps(payload).decode("utf-8"))
        with self.assertRaisesRegex(
            launchd.RemoteSourceStagerLaunchdError, "remote_port_invalid"
        ):
            launchd.build_plist(
                python_executable=Path(sys.executable).resolve(),
                asset_root=self.assets,
                catalog=self.catalog,
                identity_file=self.identity,
                known_hosts=self.known_hosts,
                remote_port=30422,
            )

    def test_install_validates_credentials_and_relocates_immutable_runtime(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        plist_path = self.root / "LaunchAgents/stager.plist"
        launchd.install_agent(
            plist_path,
            self.payload(),
            asset_root=self.assets,
            catalog=self.catalog,
            state_root=self.state_root,
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            runner=runner,
            uid=501,
            runtime_root=self.root / "runtime",
        )
        installed = plistlib.loads(plist_path.read_bytes())
        working = Path(installed["WorkingDirectory"])
        self.assertEqual(working.parent, self.root / "runtime")
        self.assertEqual(len(working.name), 64)
        self.assertIn(
            str(working / "blender/scripts/total_asset_remote_source_stager.py"),
            installed["ProgramArguments"],
        )
        self.assertEqual(plist_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(calls[0][1:], [
            "bootout", f"gui/501/{launchd.LABEL}"
        ])
        self.assertEqual(calls[1][1:3], ["bootstrap", "gui/501"])
        self.assertEqual(calls[2][1:], [
            "kickstart", "-k", f"gui/501/{launchd.LABEL}"
        ])

        self.identity.chmod(0o644)
        with self.assertRaisesRegex(
            launchd.RemoteSourceStagerLaunchdError,
            "ssh_identity_permissions_invalid",
        ):
            launchd.install_agent(
                self.root / "bad.plist",
                self.payload(),
                asset_root=self.assets,
                catalog=self.catalog,
                state_root=self.state_root,
                identity_file=self.identity,
                known_hosts=self.known_hosts,
                runner=runner,
                runtime_root=self.root / "runtime2",
            )

    def test_status_kickstart_and_uninstall_use_only_exact_label(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.assertEqual(launchd.status_agent(runner=runner, uid=501), 0)
        launchd.kickstart_agent(runner=runner, uid=501)
        plist_path = self.root / "stager.plist"
        plist_path.write_bytes(plistlib.dumps({"Label": launchd.LABEL}))
        launchd.uninstall_agent(plist_path, runner=runner, uid=501)
        self.assertEqual(calls[0][1:], [
            "print", f"gui/501/{launchd.LABEL}"
        ])
        self.assertEqual(calls[1][1:], [
            "kickstart", "-k", f"gui/501/{launchd.LABEL}"
        ])
        self.assertEqual(calls[2][1:], [
            "bootout", f"gui/501/{launchd.LABEL}"
        ])


if __name__ == "__main__":
    unittest.main()
