from __future__ import annotations

import plistlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_source_prefetch_launchd as launchd


class TotalAssetSourcePrefetchLaunchdTests(unittest.TestCase):
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
        self.log = self.root / "logs/prefetch.log"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self) -> dict[str, object]:
        return launchd.build_plist(
            python_executable=Path(sys.executable).resolve(),
            asset_root=self.assets,
            catalog=self.catalog,
            state_root=self.state_root,
            spool_root=self.root / "spool",
            log_path=self.log,
        )

    def test_plist_is_independent_keepalive_zero_model_prefetcher(self) -> None:
        payload = self.payload()
        self.assertEqual(payload["Label"], launchd.LABEL)
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(
            payload["SoftResourceLimits"]["NumberOfFiles"],
            launchd.PREFETCH_MAX_OPEN_FILES,
        )
        arguments = payload["ProgramArguments"]
        self.assertEqual(arguments[:2], ["/usr/bin/caffeinate", "-i"])
        self.assertIn("total_asset_source_prefetch.py", " ".join(arguments))
        self.assertIn("run", arguments)
        self.assertEqual(
            arguments[arguments.index("--db") + 1],
            str(self.state_root / "leases.sqlite3"),
        )
        self.assertNotIn("ssh", plistlib.dumps(payload).decode("utf-8").lower())

    def test_install_uses_immutable_runtime_and_exact_launchd_label(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        plist_path = self.root / "LaunchAgents/prefetch.plist"
        launchd.install_agent(
            plist_path,
            self.payload(),
            asset_root=self.assets,
            catalog=self.catalog,
            state_root=self.state_root,
            runner=runner,
            uid=501,
            runtime_root=self.root / "runtime",
        )
        installed = plistlib.loads(plist_path.read_bytes())
        working = Path(installed["WorkingDirectory"])
        self.assertEqual(working.parent, self.root / "runtime")
        self.assertEqual(len(working.name), 64)
        self.assertIn(
            str(working / "blender/scripts/total_asset_source_prefetch.py"),
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

    def test_status_kickstart_and_uninstall_target_only_exact_label(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.assertEqual(launchd.status_agent(runner=runner, uid=501), 0)
        launchd.kickstart_agent(runner=runner, uid=501)
        plist_path = self.root / "prefetch.plist"
        plist_path.write_bytes(plistlib.dumps({"Label": launchd.LABEL}))
        launchd.uninstall_agent(plist_path, runner=runner, uid=501)
        self.assertFalse(plist_path.exists())
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
