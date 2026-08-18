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

import total_asset_holder_guard_launchd as launchd


BOOTSTRAP = SCRIPTS / "total_asset_node_bootstrap.sh"


class NodeBootstrapTests(unittest.TestCase):
    def test_shell_is_valid_and_uses_reboot_without_systemd(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(BOOTSTRAP)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("@reboot", source)
        self.assertIn("CRON_MARKER", source)
        self.assertNotIn("systemctl", source)
        self.assertNotIn("service ", source)

    def test_embedded_python_heredocs_are_complete_and_compile(self) -> None:
        source_lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
        blocks: list[str] = []
        index = 0
        while index < len(source_lines):
            line = source_lines[index]
            if "python3 " not in line or "<<'PY'" not in line:
                index += 1
                continue
            block_start = index + 1
            try:
                block_end = source_lines.index("PY", block_start)
            except ValueError:
                self.fail(f"unterminated Python heredoc starting at line {index + 1}")
            block = "\n".join(source_lines[block_start:block_end]) + "\n"
            compile(block, f"{BOOTSTRAP}:heredoc:{index + 1}", "exec")
            blocks.append(block)
            index = block_end + 1

        self.assertEqual(
            len(blocks),
            1,
            "expected exactly one audited embedded Python heredoc",
        )

    def test_boot_restores_only_ed25519_key_and_starts_per_gpu_holders(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('key_type" = "ssh-ed25519', source)
        self.assertIn("/root/.ssh/authorized_keys", source)
        self.assertIn("for ((gpu=0; gpu<GPU_COUNT; gpu++))", source)
        self.assertIn('total_asset_gpu_holder_g${gpu}', source)
        self.assertIn('/tmp/total_asset_gpu_${gpu}.lock', source)
        self.assertIn("CUDA_VISIBLE_DEVICES=%q", source)
        self.assertIn("/proc/sys/kernel/random/boot_id", source)
        self.assertIn("video2blender.total-asset-node-heartbeat.v1", source)
        self.assertIn("total_asset_node_bootstrap_heartbeat", source)
        self.assertIn("node_heartbeat.json", source)
        self.assertIn("nvidia-smi pmon", source)
        self.assertNotIn("PasswordAuthentication", source)
        self.assertNotIn("blender -", source.lower())
        boot = source[source.index("run_boot()") : source.index("install_bootstrap()")]
        self.assertLess(
            boot.index('protect_gpu "$gpu"'),
            boot.index("restore_pinned_runtime"),
        )
        self.assertLess(
            boot.index('protect_gpu "$gpu"'),
            boot.index("restore_pinned_profile_cache"),
        )
        self.assertLess(
            boot.index('protect_gpu "$gpu"'),
            boot.index("restore_pinned_device_select_cache"),
        )
        self.assertLess(
            boot.index("write_state"),
            boot.index("restore_pinned_profile_cache"),
        )
        self.assertLess(
            boot.index("restore_pinned_runtime"),
            boot.index("restore_pinned_device_select_cache"),
        )
        self.assertLess(
            boot.index("restore_pinned_device_select_cache"),
            boot.index("restore_pinned_profile_cache"),
        )

    def test_render_library_repair_is_bounded_allowlisted_and_after_holders(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        boot = source[source.index("run_boot()") : source.index("install_bootstrap()")]
        holders = boot.index('protect_gpu "$gpu"')
        heartbeat = boot.index('start_heartbeat "$config"')
        dependency_repair = boot.index("ensure_render_shared_libraries")

        self.assertLess(holders, dependency_repair)
        self.assertLess(heartbeat, dependency_repair)
        self.assertIn("render_shared_libraries_present && return 0", source)
        self.assertIn("APPROVED_RUNTIME_APT_PACKAGE_ITEMS", source)
        self.assertIn("approved_runtime_platform_matches || return 1", source)
        self.assertIn("/etc/os-release", source)
        self.assertIn("dpkg --print-architecture", source)
        self.assertIn("APPROVED_RUNTIME_OS_ID", source)
        self.assertIn("APPROVED_RUNTIME_VERSION_ID", source)
        self.assertIn("APPROVED_RUNTIME_ARCHITECTURE", source)
        self.assertIn("DEBIAN_FRONTEND=noninteractive timeout", source)
        self.assertIn("--kill-after=15s", source)
        self.assertIn("DPkg::Lock::Timeout=60", source)
        self.assertIn("--no-install-recommends install", source)
        self.assertNotIn("apt-get upgrade", source)
        self.assertNotIn("apt-get dist-upgrade", source)
        self.assertIn("render_shared_library_repair_failed", boot)
        repair = source[
            source.index("ensure_render_shared_libraries()") : source.index(
                "cron_line()"
            )
        ]
        self.assertLess(
            repair.index("approved_runtime_platform_matches || return 1"),
            repair.index("DEBIAN_FRONTEND=noninteractive timeout"),
        )

    def test_render_dependency_config_is_validated_before_apt_arguments(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        load = source[source.index("load_config()") : source.index("restore_approved_key()")]
        self.assertIn("required_render_sonames_invalid", load)
        self.assertIn("approved_runtime_apt_packages_invalid", load)
        self.assertIn("approved_runtime_platform_missing", load)
        self.assertIn("approved_runtime_os_id_invalid", load)
        self.assertIn("approved_runtime_version_id_invalid", load)
        self.assertIn("approved_runtime_architecture_invalid", load)
        self.assertIn("runtime_apt_timeout_seconds_invalid", load)
        self.assertIn("RUNTIME_APT_TIMEOUT_SECONDS", load)
        self.assertIn("libSM.so.6,libICE.so.6", source)

    def test_bootstrap_uses_digest_checked_persistent_inputs(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("holder_digest_mismatch", source)
        self.assertIn("bootstrap_script_not_persistent", source)
        self.assertIn("bootstrap_config_owner_invalid", source)
        self.assertIn("bootstrap_config_mode_invalid", source)
        self.assertIn("video2blender.total-asset-node-bootstrap-state.v1", source)
        self.assertIn("pinned_runtime_restore_failed", source)
        self.assertIn("PINNED_RUNTIME_MANIFEST_SHA256", source)
        self.assertIn("PINNED_PROFILE_MANIFEST_SHA256", source)
        self.assertIn("profile_cache_manifest.json", source)
        self.assertIn("pinned_profile_restore_failed", source)
        self.assertIn(
            '/root/.local/share/video2blender/vulkan-profiles/v1', source
        )
        self.assertIn("0:0:555", source)
        self.assertIn("0:0:444", source)
        self.assertIn('${PINNED_PROFILE_DESTINATION}.incoming.$$', source)
        self.assertIn("PINNED_DEVICE_SELECT_MANIFEST_SHA256", source)
        self.assertIn("device_select_cache_manifest.json", source)
        self.assertIn("pinned_device_select_restore_failed", source)
        self.assertIn(
            "/root/.local/share/video2blender/"
            "vulkan-device-select-mesa-23.2.1",
            source,
        )
        self.assertIn('${PINNED_DEVICE_SELECT_DESTINATION}.incoming.$$', source)


class HolderGuardLaunchdTests(unittest.TestCase):
    def paths(self, root: Path) -> dict[str, Path]:
        return {
            "python_executable": Path(sys.executable).resolve(),
            "identity_file": root / "id_ed25519",
            "known_hosts": root / "known_hosts",
            "state_path": root / "state.json",
            "lock_path": root / "guard.lock",
            "slot_lock_root": root / "slots",
            "log_path": root / "guard.log",
        }

    def prepare_ssh_material(self, paths: dict[str, Path]) -> None:
        paths["identity_file"].write_text("private-key-placeholder\n", encoding="utf-8")
        paths["identity_file"].chmod(0o600)
        paths["known_hosts"].write_text(
            "render.example ssh-ed25519 AAAATEST\n", encoding="utf-8"
        )
        paths["known_hosts"].chmod(0o600)

    def test_plist_is_keepalive_key_only_and_uses_shared_slot_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            payload = launchd.build_plist(**paths)
        self.assertEqual(payload["Label"], launchd.LABEL)
        self.assertTrue(payload["KeepAlive"])
        self.assertTrue(payload["RunAtLoad"])
        arguments = payload["ProgramArguments"]
        self.assertEqual(arguments[:2], ["/usr/bin/caffeinate", "-i"])
        self.assertIn("total_asset_holder_guard.py", " ".join(arguments))
        self.assertIn("--slot-lock-root", arguments)
        self.assertIn("--identity-file", arguments)
        self.assertEqual(
            arguments[arguments.index("--extra-probe-slot") + 1], "30808:3"
        )
        self.assertNotIn("password", " ".join(arguments).lower())
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["TOTAL_ASSET_REMOTE_PORT"], "30773")
        self.assertEqual(environment["TOTAL_ASSET_SECONDARY_PORT"], "31722")
        self.assertEqual(environment["TOTAL_ASSET_TERTIARY_PORT"], "30808")
        self.assertEqual(environment["TOTAL_ASSET_ALLOW_LEGACY_PASSWORD_SSH"], "0")
        self.assertEqual(
            environment["TOTAL_ASSET_PREFLIGHT_SSH_KEY"],
            str(paths["identity_file"]),
        )
        self.assertEqual(
            environment["TOTAL_ASSET_SSH_KNOWN_HOSTS"],
            str(paths["known_hosts"]),
        )
        self.assertNotIn("TOTAL_ASSET_ROOT", environment)

    def test_holder_plist_accepts_nonsecret_asset_root_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.paths(root)
            payload = launchd.build_plist(**paths, asset_root=root / "assets")
        environment = payload["EnvironmentVariables"]
        self.assertEqual(environment["TOTAL_ASSET_ROOT"], str(root / "assets"))
        rendered = plistlib.dumps(payload).decode("utf-8")
        self.assertNotIn("sshpass", rendered.lower())
        self.assertNotIn("passwordauthentication=yes", rendered.lower())

    def test_install_atomically_replaces_only_exact_launchd_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.paths(root)
            self.prepare_ssh_material(paths)
            payload = launchd.build_plist(**paths)
            plist_path = root / "agent.plist"
            calls: list[list[str]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            launchd.install_agent(
                plist_path,
                payload,
                identity_file=paths["identity_file"],
                known_hosts=paths["known_hosts"],
                runner=runner,
                uid=501,
                runtime_root=root / "runtime",
            )
            installed = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(installed["Label"], launchd.LABEL)
            self.assertEqual(plist_path.stat().st_mode & 0o777, 0o600)
            working_directory = Path(installed["WorkingDirectory"])
            self.assertEqual(working_directory.parent, root / "runtime")
            self.assertEqual(len(working_directory.name), 64)
            self.assertEqual(
                installed["EnvironmentVariables"]["VIDEO2BLENDER_PROJECT_ROOT"],
                str(working_directory),
            )
            self.assertIn(
                str(working_directory / "blender/scripts/total_asset_holder_guard.py"),
                installed["ProgramArguments"],
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

    def test_uninstall_rejects_foreign_plist_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.plist"
            path.write_bytes(plistlib.dumps({"Label": "foreign.service"}))
            with self.assertRaisesRegex(
                launchd.LaunchdConfigError, "label_mismatch"
            ):
                launchd.uninstall_agent(path)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
