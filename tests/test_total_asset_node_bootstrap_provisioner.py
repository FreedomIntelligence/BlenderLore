from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_node_bootstrap_provisioner as provisioner  # noqa: E402


BOOT = "11111111-2222-3333-4444-555555555555"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly"
RUNTIME_APT_PLATFORM = {
    "os_id": "ubuntu",
    "version_id": "22.04",
    "architecture": "amd64",
}


def attestation(
    attested: bool,
    reasons: list[str] | None = None,
    *,
    shared_libraries: dict[str, bool] | None = None,
    runtime_apt_platform: dict[str, str] | None = RUNTIME_APT_PLATFORM,
) -> str:
    return json.dumps(
        {
            "schema": "video2blender.total-asset-node-bootstrap-attestation.v1",
            "attested": attested,
            "boot_id": BOOT,
            "reasons": reasons or [],
            "shared_libraries": shared_libraries
            or {
                soname: True
                for soname in provisioner.REQUIRED_RENDER_SHARED_LIBRARIES
            },
            "runtime_apt_platform": runtime_apt_platform,
        }
    )


class ScriptedRunner:
    def __init__(
        self, scripts: dict[int, list[tuple[int, str, str]]]
    ) -> None:
        self.scripts = {port: list(items) for port, items in scripts.items()}
        self.calls: list[tuple[int, list[str], dict[str, object]]] = []

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        port = int(command[command.index("-p") + 1])
        self.calls.append((port, command, kwargs))
        if not self.scripts.get(port):
            raise AssertionError(f"unexpected remote call for port {port}: {command}")
        returncode, stdout, stderr = self.scripts[port].pop(0)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class TotalAssetNodeBootstrapProvisionerTests(unittest.TestCase):
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
        self.config = provisioner.NodeBootstrapProvisionConfig(
            ssh_host="root@render.invalid",
            identity_file=self.identity,
            known_hosts=self.known_hosts,
            bootstrap_script=SCRIPTS / "total_asset_node_bootstrap.sh",
            holder_script=SCRIPTS / "gpu_hold.py",
            state_path=self.root / "provision-state.json",
            slot_lock_root=self.root / "slot-locks",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def local_runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] != ["ssh-keygen", "-y"]:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, PUBLIC_KEY + "\n", "")

    def test_fixed_paths_match_existing_31722_bootstrap_contract(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        desired, install = provisioner.desired_node_payload(
            self.config, 31722, approved_key_text=approved
        )
        base = (
            "/F00120250029/lixiang_share/wuminghao_share/"
            "video2blender_bootstrap/p31722"
        )
        self.assertEqual(desired["base"], base)
        self.assertEqual(desired["gpu_count"], 4)
        self.assertEqual(
            desired["config"], "/etc/video2blender/holder-bootstrap.conf"
        )
        self.assertEqual(
            desired["config_values"]["BOOTSTRAP_SCRIPT"],
            f"{base}/total_asset_node_bootstrap.sh",
        )
        self.assertEqual(
            desired["config_values"]["STATE_DIR"], f"{base}/state"
        )
        self.assertIn("@reboot", desired["cron_line"])
        self.assertIn(provisioner.CRON_MARKER, desired["cron_line"])
        self.assertEqual(
            {item["name"] for item in install["files"]},
            {
                "total_asset_node_bootstrap.sh",
                "gpu_hold.py",
                "approved_key.pub",
            },
        )

    def test_30808_bootstrap_protects_all_four_physical_gpus(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        desired, _install = provisioner.desired_node_payload(
            self.config, 30808, approved_key_text=approved
        )
        self.assertEqual(provisioner.PRODUCTION_GPU_LAYOUT[30808], (0, 1, 2, 3))
        self.assertEqual(desired["gpu_count"], 4)
        self.assertEqual(
            desired["pinned_runtime"]["cache"],
            desired["base"] + "/pinned_runtime_570.211.01",
        )
        self.assertEqual(
            desired["config_values"]["PINNED_RUNTIME_DESTINATION"],
            provisioner.PINNED_570_RUNTIME_DESTINATION,
        )
        self.assertEqual(
            desired["config_values"]["PINNED_RUNTIME_MANIFEST_SHA256"],
            provisioner.PINNED_570_RUNTIME_MANIFEST_SHA256,
        )
        self.assertEqual(desired["pinned_runtime"]["directory_mode"], 0o555)
        self.assertEqual(desired["pinned_runtime"]["manifest_mode"], 0o444)
        self.assertIsNone(desired["pinned_profile"])
        self.assertEqual(desired["config_values"]["PINNED_PROFILE_CACHE"], "")
        self.assertEqual(
            desired["config_values"]["PINNED_PROFILE_DESTINATION"], ""
        )
        self.assertIsNone(desired["pinned_device_select"])
        self.assertEqual(
            desired["config_values"]["PINNED_DEVICE_SELECT_CACHE"], ""
        )

    def test_node_specific_profile_cache_is_optional_and_exactly_attested(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        spec = (
            "pinned_vulkan_profiles_v1",
            provisioner.PINNED_PROFILE_DESTINATION,
            "a" * 64,
            0o555,
            0o444,
        )
        with mock.patch.object(
            provisioner, "PINNED_PROFILE_SPECS", {30808: spec}
        ):
            desired, _install = provisioner.desired_node_payload(
                self.config, 30808, approved_key_text=approved
            )

        profile = desired["pinned_profile"]
        self.assertEqual(
            profile["cache"],
            desired["base"] + "/pinned_vulkan_profiles_v1",
        )
        self.assertEqual(
            profile["destination"], provisioner.PINNED_PROFILE_DESTINATION
        )
        self.assertEqual(
            profile["manifest_name"], provisioner.PROFILE_CACHE_MANIFEST_NAME
        )
        self.assertEqual(profile["manifest_sha256"], "a" * 64)
        self.assertEqual(profile["directory_mode"], 0o555)
        self.assertEqual(profile["manifest_mode"], 0o444)
        self.assertEqual(
            desired["config_values"]["PINNED_PROFILE_MANIFEST_SHA256"],
            "a" * 64,
        )

    def test_dynamic_pin_state_drives_both_reboot_cache_specs(self) -> None:
        pin_state = self.root / "profile-cache-pins.json"
        pin_state.write_text(
            json.dumps(
                {
                    "schema": provisioner.PIN_STATE_SCHEMA,
                    "observed_at_epoch": 1000.0,
                    "nodes": {
                        "31722": {
                            "source_boot_id": BOOT,
                            "gpu_uuids": {
                                "0": "gpu-00000000-0000-0000-0000-000000000000",
                                "1": "gpu-11111111-1111-1111-1111-111111111111",
                            },
                            "profile": {
                                "cache_name": "pinned_vulkan_profiles_v1",
                                "destination": provisioner.PINNED_PROFILE_DESTINATION,
                                "manifest_sha256": "a" * 64,
                                "directory_mode": 0o555,
                                "manifest_mode": 0o444,
                            },
                            "device_select": {
                                "cache_name": "pinned_vulkan_device_select_mesa_23.2.1",
                                "destination": provisioner.PINNED_DEVICE_SELECT_DESTINATION,
                                "manifest_sha256": "b" * 64,
                                "directory_mode": 0o555,
                                "manifest_mode": 0o444,
                            },
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        pin_state.chmod(0o600)
        config = replace(self.config, pinned_cache_state=pin_state)
        approved = provisioner.approved_public_key_text(
            config, local_runner=self.local_runner
        )
        desired, _install = provisioner.desired_node_payload(
            config, 31722, approved_key_text=approved
        )
        base = desired["base"]
        self.assertEqual(
            desired["config_values"]["PINNED_PROFILE_CACHE"],
            base + "/pinned_vulkan_profiles_v1",
        )
        self.assertEqual(
            desired["config_values"]["PINNED_PROFILE_MANIFEST_SHA256"],
            "a" * 64,
        )
        self.assertEqual(
            desired["config_values"]["PINNED_DEVICE_SELECT_CACHE"],
            base + "/pinned_vulkan_device_select_mesa_23.2.1",
        )
        self.assertEqual(
            desired["config_values"]["PINNED_DEVICE_SELECT_MANIFEST_SHA256"],
            "b" * 64,
        )

    def test_dynamic_pin_state_rejects_unknown_fields_and_duplicate_gpu_uuid(self) -> None:
        pin_state = self.root / "profile-cache-pins.json"
        node = {
            "source_boot_id": BOOT,
            "gpu_uuids": {
                "0": "gpu-00000000-0000-0000-0000-000000000000",
                "1": "gpu-00000000-0000-0000-0000-000000000000",
            },
            "profile": {
                "cache_name": "pinned_vulkan_profiles_v1",
                "destination": provisioner.PINNED_PROFILE_DESTINATION,
                "manifest_sha256": "a" * 64,
                "directory_mode": 0o555,
                "manifest_mode": 0o444,
            },
            "device_select": {
                "cache_name": "pinned_vulkan_device_select_mesa_23.2.1",
                "destination": provisioner.PINNED_DEVICE_SELECT_DESTINATION,
                "manifest_sha256": "b" * 64,
                "directory_mode": 0o555,
                "manifest_mode": 0o444,
            },
            "unexpected": True,
        }
        pin_state.write_text(
            json.dumps(
                {
                    "schema": provisioner.PIN_STATE_SCHEMA,
                    "observed_at_epoch": 1000.0,
                    "nodes": {"31722": node},
                }
            ),
            encoding="utf-8",
        )
        pin_state.chmod(0o600)
        with self.assertRaisesRegex(
            provisioner.NodeProvisionError, "pinned_cache_state_invalid"
        ):
            provisioner.validate_config(
                replace(self.config, pinned_cache_state=pin_state)
            )

    def test_profile_cache_spec_rejects_30773_and_unfrozen_metadata(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        valid_shape = (
            "pinned_vulkan_profiles_v1",
            provisioner.PINNED_PROFILE_DESTINATION,
            "b" * 64,
            0o555,
            0o444,
        )
        with mock.patch.object(
            provisioner, "PINNED_PROFILE_SPECS", {30773: valid_shape}
        ):
            with self.assertRaisesRegex(
                provisioner.NodeProvisionError, "pinned_profile_port_invalid"
            ):
                provisioner.desired_node_payload(
                    self.config, 30773, approved_key_text=approved
                )

        invalid_mode = (*valid_shape[:3], 0o700, 0o444)
        with mock.patch.object(
            provisioner, "PINNED_PROFILE_SPECS", {31722: invalid_mode}
        ):
            with self.assertRaisesRegex(
                provisioner.NodeProvisionError, "pinned_profile_spec_invalid"
            ):
                provisioner.desired_node_payload(
                    self.config, 31722, approved_key_text=approved
                )

    def test_node_specific_device_select_cache_is_optional_and_exact(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        spec = (
            "pinned_vulkan_device_select_mesa_23.2.1",
            provisioner.PINNED_DEVICE_SELECT_DESTINATION,
            "d" * 64,
            0o555,
            0o444,
        )
        with mock.patch.object(
            provisioner, "PINNED_DEVICE_SELECT_SPECS", {30808: spec}
        ):
            desired, _install = provisioner.desired_node_payload(
                self.config, 30808, approved_key_text=approved
            )

        cache = desired["pinned_device_select"]
        self.assertEqual(
            cache["cache"],
            desired["base"] + "/pinned_vulkan_device_select_mesa_23.2.1",
        )
        self.assertEqual(
            cache["destination"], provisioner.PINNED_DEVICE_SELECT_DESTINATION
        )
        self.assertEqual(
            cache["manifest_name"],
            provisioner.DEVICE_SELECT_CACHE_MANIFEST_NAME,
        )
        self.assertEqual(cache["manifest_sha256"], "d" * 64)
        self.assertEqual(cache["directory_mode"], 0o555)
        self.assertEqual(cache["manifest_mode"], 0o444)

    def test_device_select_cache_rejects_30773_and_wrong_destination(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        valid = (
            "pinned_vulkan_device_select_mesa_23.2.1",
            provisioner.PINNED_DEVICE_SELECT_DESTINATION,
            "e" * 64,
            0o555,
            0o444,
        )
        with mock.patch.object(
            provisioner, "PINNED_DEVICE_SELECT_SPECS", {30773: valid}
        ):
            with self.assertRaisesRegex(
                provisioner.NodeProvisionError,
                "pinned_device_select_port_invalid",
            ):
                provisioner.desired_node_payload(
                    self.config, 30773, approved_key_text=approved
                )
        wrong_destination = (
            valid[0], "/root/unsafe", valid[2], valid[3], valid[4]
        )
        with mock.patch.object(
            provisioner,
            "PINNED_DEVICE_SELECT_SPECS",
            {31722: wrong_destination},
        ):
            with self.assertRaisesRegex(
                provisioner.NodeProvisionError,
                "pinned_device_select_spec_invalid",
            ):
                provisioner.desired_node_payload(
                    self.config, 31722, approved_key_text=approved
                )

    def test_30773_restores_the_distinct_pinned_native_cycles_runtime(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        desired, _install = provisioner.desired_node_payload(
            self.config, 30773, approved_key_text=approved
        )
        self.assertEqual(
            desired["pinned_runtime"]["cache"],
            desired["base"] + "/pinned_native_runtime_565.57.01",
        )
        self.assertEqual(
            desired["config_values"]["PINNED_RUNTIME_DESTINATION"],
            provisioner.PINNED_NATIVE_565_RUNTIME_DESTINATION,
        )
        self.assertEqual(
            desired["config_values"]["PINNED_RUNTIME_MANIFEST_SHA256"],
            provisioner.PINNED_NATIVE_565_RUNTIME_MANIFEST_SHA256,
        )
        self.assertEqual(desired["pinned_runtime"]["directory_mode"], 0o500)
        self.assertEqual(desired["pinned_runtime"]["manifest_mode"], 0o400)

    def test_ssh_is_key_only_fixed_known_hosts_and_has_no_password_path(self) -> None:
        command = provisioner.ssh_base_command(self.config, 31722)
        rendered = " ".join(command)
        self.assertIn("BatchMode=yes", rendered)
        self.assertIn("IdentitiesOnly=yes", rendered)
        self.assertIn("PreferredAuthentications=publickey", rendered)
        self.assertIn("PasswordAuthentication=no", rendered)
        self.assertIn("KbdInteractiveAuthentication=no", rendered)
        self.assertIn("StrictHostKeyChecking=yes", rendered)
        self.assertIn(f"UserKnownHostsFile={self.known_hosts}", rendered)
        self.assertIn("GlobalKnownHostsFile=/dev/null", rendered)
        self.assertNotIn("sshpass", rendered.lower())
        self.assertNotIn("passwordauthentication=yes", rendered.lower())
        self.assertIn("ConnectTimeout=12", rendered)

    def test_attestation_command_has_a_distinct_bounded_wall_clock(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        desired, _install = provisioner.desired_node_payload(
            self.config, 31722, approved_key_text=approved
        )
        runner = ScriptedRunner({31722: [(0, attestation(True), "")]})

        provisioner.attest_node(
            self.config, 31722, desired, runner=runner
        )

        _port, command, kwargs = runner.calls[0]
        self.assertIn("ConnectTimeout=12", command)
        self.assertEqual(kwargs["timeout"], 90)

    def test_remote_command_timeout_is_not_reported_as_connect_timeout(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        desired, _install = provisioner.desired_node_payload(
            self.config, 31722, approved_key_text=approved
        )

        def timeout_runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaisesRegex(
            provisioner.NodeProvisionError, "bootstrap_ssh_command_timeout"
        ):
            provisioner.attest_node(
                self.config, 31722, desired, runner=timeout_runner
            )

    def test_required_sonames_and_only_approved_node_packages_are_frozen(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        node_31722, _install = provisioner.desired_node_payload(
            self.config, 31722, approved_key_text=approved
        )
        node_30808, _install = provisioner.desired_node_payload(
            self.config, 30808, approved_key_text=approved
        )

        self.assertEqual(
            node_31722["required_shared_libraries"],
            ["libSM.so.6", "libICE.so.6"],
        )
        self.assertEqual(
            node_31722["config_values"]["APPROVED_RUNTIME_APT_PACKAGES"],
            "libsm6=2:1.2.3-1build2,libice6=2:1.0.10-1build2",
        )
        self.assertEqual(
            node_31722["approved_runtime_apt_platform"],
            RUNTIME_APT_PLATFORM,
        )
        self.assertEqual(
            node_31722["config_values"]["APPROVED_RUNTIME_OS_ID"],
            "ubuntu",
        )
        self.assertEqual(
            node_31722["config_values"]["APPROVED_RUNTIME_VERSION_ID"],
            "22.04",
        )
        self.assertEqual(
            node_31722["config_values"]["APPROVED_RUNTIME_ARCHITECTURE"],
            "amd64",
        )
        self.assertEqual(
            node_30808["config_values"]["APPROVED_RUNTIME_APT_PACKAGES"],
            "",
        )
        self.assertIsNone(node_30808["approved_runtime_apt_platform"])
        self.assertEqual(
            node_30808["required_shared_libraries"],
            node_31722["required_shared_libraries"],
        )

    def test_shared_library_attestation_is_required_even_if_remote_claims_ready(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        desired, _install = provisioner.desired_node_payload(
            self.config, 31722, approved_key_text=approved
        )
        forged = json.loads(attestation(True))
        forged.pop("shared_libraries")
        runner = ScriptedRunner({31722: [(0, json.dumps(forged), "")]})

        with self.assertRaisesRegex(
            provisioner.NodeProvisionError,
            "bootstrap_remote_shared_library_attestation_invalid",
        ):
            provisioner.attest_node(
                self.config, 31722, desired, runner=runner
            )

    def test_runtime_package_platform_attestation_is_exact_and_fail_closed(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        desired, _install = provisioner.desired_node_payload(
            self.config, 31722, approved_key_text=approved
        )
        mismatch = {
            "os_id": "ubuntu",
            "version_id": "24.04",
            "architecture": "amd64",
        }
        blocked_runner = ScriptedRunner(
            {
                31722: [
                    (
                        0,
                        attestation(
                            False,
                            ["runtime_apt_platform_mismatch"],
                            runtime_apt_platform=mismatch,
                        ),
                        "",
                    )
                ]
            }
        )
        payload = provisioner.attest_node(
            self.config, 31722, desired, runner=blocked_runner
        )
        self.assertFalse(payload["attested"])
        self.assertEqual(payload["runtime_apt_platform"], mismatch)

        forged_runner = ScriptedRunner(
            {
                31722: [
                    (
                        0,
                        attestation(True, runtime_apt_platform=mismatch),
                        "",
                    )
                ]
            }
        )
        with self.assertRaisesRegex(
            provisioner.NodeProvisionError,
            "bootstrap_remote_runtime_platform_attestation_invalid",
        ):
            provisioner.attest_node(
                self.config, 31722, desired, runner=forged_runner
            )

        missing = json.loads(attestation(True))
        missing.pop("runtime_apt_platform")
        missing_runner = ScriptedRunner(
            {31722: [(0, json.dumps(missing), "")]}
        )
        with self.assertRaisesRegex(
            provisioner.NodeProvisionError,
            "bootstrap_remote_runtime_platform_attestation_invalid",
        ):
            provisioner.attest_node(
                self.config, 31722, desired, runner=missing_runner
            )

    def test_runtime_packages_require_an_approved_platform_binding(self) -> None:
        with self.assertRaisesRegex(
            provisioner.NodeProvisionError,
            "bootstrap_runtime_package_platform_unbound",
        ):
            provisioner.validate_config(
                replace(self.config, approved_runtime_apt_platforms={})
            )

    def test_existing_31722_attests_and_is_skipped(self) -> None:
        runner = ScriptedRunner(
            {
                30773: [(255, "", "secret auth detail")],
                30808: [(255, "", "secret auth detail")],
                31722: [(0, attestation(True), "")],
            }
        )
        payload = provisioner.ensure_canonical_nodes(
            self.config,
            runner=runner,
            local_runner=self.local_runner,
            now_epoch=1000.0,
        )
        node = payload["nodes"]["31722"]
        self.assertEqual(node["status"], "attested")
        self.assertEqual(node["action"], "attested_skip")
        self.assertEqual(
            [port for port, _command, _kwargs in runner.calls].count(31722), 1
        )
        for port in (30773, 30808):
            self.assertEqual(payload["nodes"][str(port)]["status"], "pending_auth")
        state_text = self.config.state_path.read_text(encoding="utf-8")
        self.assertNotIn("secret auth detail", state_text)
        self.assertEqual(self.config.state_path.stat().st_mode & 0o777, 0o600)

    def test_authenticated_missing_node_is_atomically_installed_then_attested(self) -> None:
        runner = ScriptedRunner(
            {
                30773: [
                    (
                        0,
                        attestation(
                            False,
                            ["bootstrap_dir_missing"],
                            runtime_apt_platform=None,
                        ),
                        "",
                    ),
                    (
                        0,
                        attestation(
                            False,
                            ["bootstrap_dir_missing"],
                            runtime_apt_platform=None,
                        ),
                        "",
                    ),
                    (0, json.dumps({"installed": True}), ""),
                    (0, '{"status":"protected"}\n', ""),
                    (0, attestation(True, runtime_apt_platform=None), ""),
                ],
                30808: [(255, "", "denied")],
                31722: [(0, attestation(True), "")],
            }
        )
        payload = provisioner.ensure_canonical_nodes(
            self.config,
            runner=runner,
            local_runner=self.local_runner,
            now_epoch=1000.0,
        )
        node = payload["nodes"]["30773"]
        self.assertEqual(node["status"], "attested")
        self.assertEqual(node["action"], "provisioned")
        calls = [call for call in runner.calls if call[0] == 30773]
        self.assertEqual(len(calls), 5)
        copy_input = json.loads(str(calls[2][2]["input"]))
        self.assertEqual(
            {item["mode"] for item in copy_input["files"]}, {0o600, 0o700}
        )
        install_command = calls[3][1][-1]
        self.assertIn("total_asset_node_bootstrap.sh install", install_command)
        self.assertIn("--gpu-count 4", install_command)
        self.assertNotIn("run_total_asset_render_worker", install_command)
        self.assertNotIn("blender -", install_command.lower())
        self.assertNotIn("--pinned-profile", install_command)
        self.assertNotIn("--pinned-device-select", install_command)
        self.assertIn(
            "TOTAL_ASSET_APPROVED_RUNTIME_APT_PACKAGES=", install_command
        )
        self.assertIn("TOTAL_ASSET_APPROVED_RUNTIME_OS_ID=", install_command)
        self.assertIn(
            "TOTAL_ASSET_APPROVED_RUNTIME_VERSION_ID=", install_command
        )
        self.assertIn(
            "TOTAL_ASSET_APPROVED_RUNTIME_ARCHITECTURE=", install_command
        )

    def test_missing_render_library_stays_bootstrap_blocked_after_repair_failure(self) -> None:
        missing = attestation(
            False,
            ["render_shared_library_unavailable"],
            shared_libraries={"libSM.so.6": False, "libICE.so.6": True},
        )
        runner = ScriptedRunner(
            {
                30773: [(255, "", "denied")],
                30808: [(255, "", "denied")],
                31722: [
                    (0, missing, ""),
                    (0, missing, ""),
                    (0, json.dumps({"installed": True}), ""),
                    (0, '{"status":"protected"}\n', ""),
                    (0, missing, ""),
                ],
            }
        )

        payload = provisioner.ensure_canonical_nodes(
            self.config,
            runner=runner,
            local_runner=self.local_runner,
            now_epoch=1000.0,
        )

        node = payload["nodes"]["31722"]
        self.assertEqual(node["status"], "blocked")
        self.assertEqual(
            node["error_code"], "bootstrap_post_install_attestation_failed"
        )
        install_call = [call for call in runner.calls if call[0] == 31722][3]
        self.assertEqual(install_call[2]["timeout"], 420)
        persisted = self.config.state_path.read_text(encoding="utf-8")
        self.assertNotIn("libSM", persisted)
        self.assertNotIn("asset_id", persisted)

    def test_profile_cache_install_arguments_are_added_only_for_frozen_spec(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        spec = (
            "pinned_vulkan_profiles_v1",
            provisioner.PINNED_PROFILE_DESTINATION,
            "c" * 64,
            0o555,
            0o444,
        )
        runner = ScriptedRunner(
            {
                30808: [
                    (0, json.dumps({"installed": True}), ""),
                    (0, '{"status":"protected"}\n', ""),
                ]
            }
        )
        with mock.patch.object(
            provisioner, "PINNED_PROFILE_SPECS", {30808: spec}
        ):
            desired, install_payload = provisioner.desired_node_payload(
                self.config, 30808, approved_key_text=approved
            )
            provisioner.install_node(
                self.config,
                30808,
                desired,
                install_payload,
                runner=runner,
            )

        command = runner.calls[-1][1][-1]
        self.assertIn("--pinned-profile-cache", command)
        self.assertIn("--pinned-profile-destination", command)
        self.assertIn("--pinned-profile-manifest-sha256", command)
        self.assertIn(provisioner.PINNED_PROFILE_DESTINATION, command)
        self.assertIn("c" * 64, command)

    def test_remote_attestation_checks_profile_cache_and_destination_metadata(self) -> None:
        program = provisioner.REMOTE_ATTEST_PROGRAM
        self.assertIn("pinned_profile_cache_dir", program)
        self.assertIn("pinned_profile_cache_manifest", program)
        self.assertIn("pinned_profile_destination_dir", program)
        self.assertIn("pinned_profile_destination_manifest", program)
        self.assertIn("pinned_device_select_cache_dir", program)
        self.assertIn("pinned_device_select_cache_manifest", program)
        self.assertIn("pinned_device_select_destination_dir", program)
        self.assertIn("pinned_device_select_destination_manifest", program)

    def test_device_select_install_arguments_require_frozen_spec(self) -> None:
        approved = provisioner.approved_public_key_text(
            self.config, local_runner=self.local_runner
        )
        spec = (
            "pinned_vulkan_device_select_mesa_23.2.1",
            provisioner.PINNED_DEVICE_SELECT_DESTINATION,
            "f" * 64,
            0o555,
            0o444,
        )
        runner = ScriptedRunner(
            {
                31722: [
                    (0, json.dumps({"installed": True}), ""),
                    (0, '{"status":"protected"}\n', ""),
                ]
            }
        )
        with mock.patch.object(
            provisioner, "PINNED_DEVICE_SELECT_SPECS", {31722: spec}
        ):
            desired, install_payload = provisioner.desired_node_payload(
                self.config, 31722, approved_key_text=approved
            )
            provisioner.install_node(
                self.config,
                31722,
                desired,
                install_payload,
                runner=runner,
            )
        command = runner.calls[-1][1][-1]
        self.assertIn("--pinned-device-select-cache", command)
        self.assertIn("--pinned-device-select-destination", command)
        self.assertIn("--pinned-device-select-manifest-sha256", command)
        self.assertIn(provisioner.PINNED_DEVICE_SELECT_DESTINATION, command)

    def test_auth_failure_uses_per_node_backoff_without_consuming_pending(self) -> None:
        runner = ScriptedRunner(
            {
                30773: [(255, "", "denied")],
                30808: [(255, "", "denied")],
                31722: [(255, "", "denied")],
            }
        )
        first = provisioner.ensure_canonical_nodes(
            self.config,
            runner=runner,
            local_runner=self.local_runner,
            now_epoch=1000.0,
        )
        call_count = len(runner.calls)
        second = provisioner.ensure_canonical_nodes(
            self.config,
            runner=runner,
            local_runner=self.local_runner,
            now_epoch=1010.0,
        )
        self.assertEqual(len(runner.calls), call_count)
        for port in (30773, 30808, 31722):
            raw = first["nodes"][str(port)]
            self.assertEqual(raw["status"], "pending_auth")
            self.assertEqual(raw["action"], "awaiting_key_authentication")
            self.assertEqual(second["nodes"][str(port)]["action"], "backoff")
            self.assertGreater(raw["next_attempt_at_epoch"], 1000.0)

    def test_failed_post_install_attestation_is_redacted_and_retried(self) -> None:
        runner = ScriptedRunner(
            {
                30773: [
                    (
                        0,
                        attestation(
                            False,
                            ["bootstrap_dir_missing"],
                            runtime_apt_platform=None,
                        ),
                        "",
                    ),
                    (
                        0,
                        attestation(
                            False,
                            ["bootstrap_dir_missing"],
                            runtime_apt_platform=None,
                        ),
                        "",
                    ),
                    (0, json.dumps({"installed": True}), ""),
                    (0, "protected\n", ""),
                    (
                        0,
                        attestation(
                            False,
                            ["root_cron_marker_invalid"],
                            runtime_apt_platform=None,
                        ),
                        "",
                    ),
                ],
                30808: [(255, "", "denied")],
                31722: [(255, "", "denied")],
            }
        )
        payload = provisioner.ensure_canonical_nodes(
            self.config,
            runner=runner,
            local_runner=self.local_runner,
            now_epoch=1000.0,
        )
        node = payload["nodes"]["30773"]
        self.assertEqual(node["status"], "blocked")
        self.assertEqual(node["action"], "retry_backoff")
        self.assertEqual(
            node["error_code"], "bootstrap_post_install_attestation_failed"
        )
        self.assertNotIn(
            "root_cron_marker_invalid",
            self.config.state_path.read_text(encoding="utf-8"),
        )

    def test_embedded_remote_programs_compile_and_never_start_render(self) -> None:
        compile(provisioner.REMOTE_ATTEST_PROGRAM, "remote-attest", "exec")
        compile(provisioner.REMOTE_ATOMIC_INSTALL_PROGRAM, "remote-install", "exec")
        source = (
            provisioner.REMOTE_ATTEST_PROGRAM
            + provisioner.REMOTE_ATOMIC_INSTALL_PROGRAM
        ).lower()
        self.assertIn("os.replace", source)
        self.assertIn("ctypes.CDLL", provisioner.REMOTE_ATTEST_PROGRAM)
        self.assertIn(
            'reason("render_shared_library_unavailable")',
            provisioner.REMOTE_ATTEST_PROGRAM,
        )
        self.assertNotIn("run_total_asset_render_worker", source)
        self.assertNotIn("blender -", source)


if __name__ == "__main__":
    unittest.main()
