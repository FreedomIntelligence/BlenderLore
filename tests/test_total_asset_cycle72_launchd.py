from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_cycle72_launchd as launchd


class TotalAssetCycle72LaunchdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.asset_root = self.root / "assets"
        self.inventory = self.asset_root / "_asset_inventory"
        self.inventory.mkdir(parents=True)
        self.catalog = self.inventory / "total_asset_catalog.csv"
        self.catalog.write_text("asset_id,render_order,render_batch\n", encoding="utf-8")
        self.identity = self.root / "id_ed25519"
        self.identity.write_text("private-key-placeholder\n", encoding="utf-8")
        self.identity.chmod(0o600)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text("render.example ssh-ed25519 AAAATEST\n", encoding="utf-8")
        self.known_hosts.chmod(0o600)
        self.state_root = self.root / "state"
        self.log_path = self.root / "logs" / "supervisor.log"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self) -> dict[str, object]:
        return launchd.build_plist(
            python_executable=Path(sys.executable).resolve(),
            asset_root=self.asset_root,
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            catalog=self.catalog,
            state_root=self.state_root,
            holder_guard_state=self.root / "holder_guard.json",
            slot_lock_root=self.root / "slot-locks",
            log_path=self.log_path,
        )

    def test_plist_is_exact_keepalive_key_only_supervisor(self) -> None:
        payload = self.payload()
        self.assertEqual(payload["Label"], launchd.LABEL)
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(
            payload["SoftResourceLimits"]["NumberOfFiles"],
            launchd.SUPERVISOR_MAX_OPEN_FILES,
        )
        arguments = payload["ProgramArguments"]
        self.assertEqual(arguments[:2], ["/usr/bin/caffeinate", "-i"])
        self.assertIn("total_asset_cycle72_supervisor.py", " ".join(arguments))
        self.assertIn("run", arguments)
        self.assertEqual(
            arguments[arguments.index("--catalog") + 1], str(self.catalog)
        )
        self.assertEqual(
            arguments[arguments.index("--state-root") + 1], str(self.state_root)
        )
        self.assertEqual(
            arguments[arguments.index("--authority-parse-cache") + 1],
            str(self.state_root / "authority_parse_cache.sqlite3"),
        )
        self.assertEqual(
            arguments[arguments.index("--identity-file") + 1], str(self.identity)
        )
        self.assertEqual(
            arguments[arguments.index("--known-hosts") + 1], str(self.known_hosts)
        )
        self.assertEqual(
            arguments[arguments.index("--bootstrap-provision-state") + 1],
            str(self.state_root / "node_bootstrap_provision.json"),
        )
        self.assertEqual(
            arguments[arguments.index("--global-preparation-limit") + 1], "1"
        )
        rendered = plistlib.dumps(payload).decode("utf-8")
        self.assertNotIn("sshpass", rendered.lower())
        self.assertNotIn("passwordauthentication=yes", rendered.lower())
        self.assertNotIn("kbdinteractiveauthentication=yes", rendered.lower())

    def test_global_preparation_limit_is_explicit_and_bounded(self) -> None:
        payload = launchd.build_plist(
            python_executable=Path(sys.executable).resolve(),
            asset_root=self.asset_root,
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            global_preparation_limit=3,
        )
        arguments = payload["ProgramArguments"]
        self.assertEqual(
            arguments[arguments.index("--global-preparation-limit") + 1], "3"
        )
        for invalid in (0, 12):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                launchd.Cycle72LaunchdError,
                "global_preparation_limit_invalid",
            ):
                launchd.build_plist(
                    python_executable=Path(sys.executable).resolve(),
                    asset_root=self.asset_root,
                    identity_file=self.identity,
                    known_hosts=self.known_hosts,
                    global_preparation_limit=invalid,
                )

    def test_node_preparation_limits_are_explicit_and_bounded(self) -> None:
        configured = "31722=4,30773=4,30808=3"
        payload = launchd.build_plist(
            python_executable=Path(sys.executable).resolve(),
            asset_root=self.asset_root,
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            node_preparation_limits=configured,
        )
        arguments = payload["ProgramArguments"]
        self.assertEqual(
            arguments[arguments.index("--node-preparation-limits") + 1],
            configured,
        )
        for invalid in (
            "31722=5,30773=4,30808=3",
            "31722=4,30773=4",
            "31722=4,30773=4,30808=3,30808=2",
            "31722=0,30773=4,30808=3",
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                launchd.Cycle72LaunchdError,
                "node_preparation_limits_invalid",
            ):
                launchd.build_plist(
                    python_executable=Path(sys.executable).resolve(),
                    asset_root=self.asset_root,
                    identity_file=self.identity,
                    known_hosts=self.known_hosts,
                    node_preparation_limits=invalid,
                )

    def test_environment_fixes_replacement_topology_and_nonsecret_paths(self) -> None:
        environment = self.payload()["EnvironmentVariables"]
        self.assertEqual(environment["TOTAL_ASSET_REMOTE_PORT"], "30773")
        self.assertEqual(environment["TOTAL_ASSET_SECONDARY_PORT"], "31722")
        self.assertEqual(environment["TOTAL_ASSET_TERTIARY_PORT"], "30808")
        self.assertEqual(environment["TOTAL_ASSET_ROOT"], str(self.asset_root))
        self.assertEqual(
            environment["TOTAL_ASSET_PREFLIGHT_SSH_KEY"], str(self.identity)
        )
        self.assertEqual(
            environment["TOTAL_ASSET_SSH_KNOWN_HOSTS"], str(self.known_hosts)
        )
        self.assertEqual(environment["TOTAL_ASSET_ALLOW_LEGACY_PASSWORD_SSH"], "0")
        self.assertNotIn("PASSWORD", " ".join(environment).upper().replace(
            "TOTAL_ASSET_ALLOW_LEGACY_PASSWORD_SSH", ""
        ))

    def test_install_uses_only_exact_label_and_atomic_plist(self) -> None:
        payload = self.payload()
        plist_path = self.root / "LaunchAgents" / "cycle72.plist"
        calls: list[list[str]] = []

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        launchd.install_agent(
            plist_path,
            payload,
            asset_root=self.asset_root,
            catalog=self.catalog,
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            runner=runner,
            uid=501,
            runtime_root=self.root / "runtime",
        )
        installed = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(installed["Label"], launchd.LABEL)
        self.assertEqual(plist_path.stat().st_mode & 0o777, 0o600)
        working_directory = Path(installed["WorkingDirectory"])
        self.assertEqual(working_directory.parent, self.root / "runtime")
        self.assertEqual(len(working_directory.name), 64)
        arguments = installed["ProgramArguments"]
        self.assertIn(
            str(
                working_directory
                / "blender/scripts/total_asset_cycle72_supervisor.py"
            ),
            arguments,
        )
        self.assertEqual(
            arguments[arguments.index("--project") + 1], str(working_directory)
        )
        self.assertEqual(
            installed["EnvironmentVariables"]["VIDEO2BLENDER_PROJECT_ROOT"],
            str(working_directory),
        )
        self.assertNotIn(
            str(launchd.PROJECT_ROOT / "blender/scripts"),
            plistlib.dumps(installed).decode("utf-8"),
        )
        self.assertEqual(calls[0][1:], [
            "bootout", f"gui/501/{launchd.LABEL}"
        ])
        self.assertEqual(calls[1][1:3], ["bootstrap", "gui/501"])
        self.assertEqual(calls[2][1:], [
            "kickstart", "-k", f"gui/501/{launchd.LABEL}"
        ])

    def test_status_and_uninstall_need_no_credentials(self) -> None:
        calls: list[list[str]] = []

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "healthy\n", "")

        self.assertEqual(launchd.status_agent(runner=runner, uid=501), 0)
        plist_path = self.root / "cycle72.plist"
        plist_path.write_bytes(plistlib.dumps({"Label": launchd.LABEL}))
        launchd.uninstall_agent(plist_path, runner=runner, uid=501)
        self.assertFalse(plist_path.exists())
        self.assertEqual(calls[0][1:], [
            "print", f"gui/501/{launchd.LABEL}"
        ])
        self.assertEqual(calls[1][1:], [
            "bootout", f"gui/501/{launchd.LABEL}"
        ])

    def test_post_handoff_kickstart_targets_only_exact_label(self) -> None:
        calls: list[list[str]] = []

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        launchd.kickstart_agent(runner=runner, uid=501)
        self.assertEqual(calls, [[
            "/bin/launchctl", "kickstart", "-k",
            f"gui/501/{launchd.LABEL}",
        ]])

    def test_uninstall_rejects_foreign_plist(self) -> None:
        path = self.root / "foreign.plist"
        path.write_bytes(plistlib.dumps({"Label": "foreign.service"}))
        with self.assertRaisesRegex(
            launchd.Cycle72LaunchdError, "label_mismatch"
        ):
            launchd.uninstall_agent(path)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
