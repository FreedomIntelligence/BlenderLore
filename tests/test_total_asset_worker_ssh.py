from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_total_asset_render_worker as worker
import batch_bilibili_resource_model_render as task1


class WorkerSshTransportTests(unittest.TestCase):
    NONCE = "a" * 32

    def trailer(self, returncode: int) -> str:
        return f"\n{worker.remote_exit_trailer(self.NONCE)}={returncode}\n"

    def secure_material(self, root: Path) -> tuple[Path, Path]:
        identity = root / "worker_ed25519"
        known_hosts = root / "worker_known_hosts"
        identity.write_text("private-key-placeholder", encoding="utf-8")
        identity.chmod(0o600)
        known_hosts.write_text("[host]:30422 ssh-ed25519 public-key\n", encoding="utf-8")
        known_hosts.chmod(0o600)
        return identity, known_hosts

    def environment(self, identity: Path, known_hosts: Path) -> dict[str, str]:
        return {
            worker.SSH_IDENTITY_ENV: str(identity),
            worker.SSH_KNOWN_HOSTS_ENV: str(known_hosts),
            worker.LEGACY_PASSWORD_SSH_ENV: "0",
        }

    def test_run_uses_strict_key_only_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            completed = subprocess.CompletedProcess(
                [], 0, "ok\n", self.trailer(0)
            )
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(worker.subprocess, "run", return_value=completed) as run, mock.patch.object(
                worker, "spawn_password"
            ) as password, mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ):
                output = worker.Remote(30422, 0).run("true", timeout=17)

            self.assertEqual(output, "ok\n")
            args = run.call_args.args[0]
            self.assertEqual(args[0], "ssh")
            self.assertEqual(args[args.index("-F") + 1], "/dev/null")
            self.assertIn("BatchMode=yes", args)
            self.assertIn("IdentitiesOnly=yes", args)
            self.assertIn("PasswordAuthentication=no", args)
            self.assertIn("KbdInteractiveAuthentication=no", args)
            self.assertIn("PreferredAuthentications=publickey", args)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn(f"UserKnownHostsFile={known_hosts}", args)
            self.assertIn("ServerAliveInterval=30", args)
            self.assertIn("ServerAliveCountMax=6", args)
            self.assertIn("GlobalKnownHostsFile=/dev/null", args)
            self.assertIn(str(identity), args)
            self.assertNotIn("StrictHostKeyChecking=no", args)
            self.assertNotIn("PasswordAuthentication=yes", args)
            self.assertEqual(args[-2:], ["/bin/bash", "-s"])
            self.assertNotIn("true", args)
            self.assertIn("true", run.call_args.kwargs["input"])
            self.assertEqual(run.call_args.kwargs["timeout"], 17)
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["errors"], "replace")
            password.assert_not_called()

    def test_key_only_run_streams_very_large_command_over_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            completed = subprocess.CompletedProcess(
                [], 0, "ok\n", self.trailer(0)
            )
            large_command = "printf x # " + ("a" * 400_000)
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(
                worker.subprocess, "run", return_value=completed
            ) as run, mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ):
                output = worker.Remote(30422, 0).run(large_command)

            self.assertEqual(output, "ok\n")
            args = run.call_args.args[0]
            self.assertEqual(args[-2:], ["/bin/bash", "-s"])
            self.assertLess(max(map(len, args)), 4096)
            self.assertNotIn(large_command, args)
            self.assertIn(large_command, run.call_args.kwargs["input"])

    def test_successful_key_only_run_preserves_redacted_remote_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            completed = subprocess.CompletedProcess(
                [],
                0,
                "render stdout\n",
                "token=do-not-keep\npython traceback\n" + self.trailer(0),
            )
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(
                worker.subprocess, "run", return_value=completed
            ), mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ):
                output = worker.Remote(30422, 0).run("true")

            self.assertIsInstance(output, worker.RemoteCommandOutput)
            self.assertEqual(str(output), "render stdout\n")
            diagnostic = output.diagnostic_text
            self.assertIn("[remote stdout]\nrender stdout", diagnostic)
            self.assertIn("[remote stderr]\n", diagnostic)
            self.assertIn("python traceback", diagnostic)
            self.assertIn("token=[REDACTED]", diagnostic)
            self.assertNotIn("do-not-keep", diagnostic)

    def test_password_transport_decodes_with_replacement_and_redacts_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            password_file = Path(temporary) / "password"
            password_file.write_text("super-secret-value", encoding="utf-8")
            child = mock.Mock()
            child.expect.return_value = 1
            child.before = (
                "non-utf8-was-replaced=�\n"
                "password=super-secret-value\n"
                "https://user:super-secret-value@example.invalid/path"
            )
            child.exitstatus = 0
            child.signalstatus = None
            with mock.patch.object(
                worker.pexpect,
                "spawn",
                return_value=child,
            ) as spawn:
                output = worker.spawn_password(
                    ["ssh", "example.invalid", "true"],
                    9,
                    password_file,
                )

            self.assertIn("non-utf8-was-replaced=�", output)
            self.assertNotIn("super-secret-value", output)
            self.assertIn("password=[REDACTED]", output)
            self.assertIn("https://user:[REDACTED]@example.invalid", output)
            self.assertEqual(spawn.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(spawn.call_args.kwargs["codec_errors"], "replace")

    def test_diagnostic_tail_redacts_common_secret_shapes(self) -> None:
        tail = worker.diagnostic_output_tail(
            b"prefix\xff\x00\ntoken=abc123\npassword: hunter2",
            limit=200,
        )
        self.assertIn("prefix��", tail)
        self.assertNotIn("abc123", tail)
        self.assertNotIn("hunter2", tail)
        self.assertIn("token=[REDACTED]", tail)
        self.assertIn("password: [REDACTED]", tail)

    def test_put_and_get_pass_the_same_key_only_transport_to_rsync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, known_hosts = self.secure_material(root)
            source = root / "source"
            source.mkdir()
            destination = root / "destination"
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(
                worker.subprocess, "run", return_value=completed
            ) as run:
                remote = worker.Remote(30808, 2)
                remote.put(source, "/tmp/input")
                remote.get("/tmp/output", destination)

            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                args = call.args[0]
                self.assertEqual(args[0], "rsync")
                transport = shlex.split(args[args.index("-e") + 1])
                self.assertEqual(transport[0], "ssh")
                self.assertIn("BatchMode=yes", transport)
                self.assertIn("PasswordAuthentication=no", transport)
                self.assertIn("StrictHostKeyChecking=yes", transport)
                self.assertIn(f"UserKnownHostsFile={known_hosts}", transport)
                self.assertIn(str(identity), transport)
                self.assertNotIn("StrictHostKeyChecking=no", transport)
                self.assertIn(
                    f"--timeout={worker.RSYNC_IO_TIMEOUT_SECONDS}", args
                )

    def test_wall_clock_timeout_is_a_transport_attempt_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(
                worker.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["ssh"], 17),
            ), self.assertRaises(worker.RemoteTransportError) as caught:
                worker.Remote(30422, 0).run("true", timeout=17)
            self.assertEqual(caught.exception.code, "worker_ssh_timeout")
            self.assertEqual(caught.exception.stage, "")

    def test_remote_application_exit_is_not_transport_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            completed = subprocess.CompletedProcess(
                [], 0, "application stdout", "application failed" + self.trailer(255)
            )
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(
                worker.subprocess, "run", return_value=completed
            ), mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ), self.assertRaises(worker.RemoteCommandError) as caught:
                worker.Remote(30422, 0).run("exit 255")
            self.assertNotIsInstance(caught.exception, worker.RemoteTransportError)
            self.assertEqual(caught.exception.returncode, 255)
            self.assertEqual(caught.exception.stdout, "application stdout")
            self.assertEqual(caught.exception.stderr, "application failed")

    def test_missing_or_nonterminal_exit_trailer_is_transport_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            environment = self.environment(identity, known_hosts)
            for stderr in (
                "application failed",
                self.trailer(255) + "late output",
                self.trailer(255) + self.trailer(255),
                f"\n{worker.remote_exit_trailer(self.NONCE)}=256\n",
                f"\n{worker.remote_exit_trailer('b' * 32)}=255\n",
            ):
                with self.subTest(stderr=stderr), mock.patch.dict(
                    os.environ, environment, clear=False
                ), mock.patch.object(
                    worker.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", stderr),
                ), mock.patch.object(
                    worker.secrets, "token_hex", return_value=self.NONCE
                ), self.assertRaises(worker.RemoteTransportError) as caught:
                    worker.Remote(30422, 0).run("exit 255")
                self.assertEqual(
                    caught.exception.code, "worker_ssh_protocol_incomplete"
                )
                self.assertEqual(caught.exception.returncode, 0)

    def test_outer_trailer_survives_inner_exec_and_exit_255(self) -> None:
        wrapped = worker.wrap_remote_exit_command(
            "exec /bin/bash -c 'printf remote-output; exit 255'",
            self.NONCE,
        )
        completed = subprocess.run(
            ["/bin/bash", "-c", wrapped],
            text=True,
            capture_output=True,
            check=False,
        )
        clean_stderr, remote_returncode = worker.parse_remote_exit_trailer(
            completed.stderr, self.NONCE
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(remote_returncode, 255)
        self.assertEqual(completed.stdout, "remote-output")
        self.assertEqual(clean_stderr, "")

    def test_local_ssh_255_wins_even_if_partial_output_has_valid_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            completed = subprocess.CompletedProcess(
                [],
                255,
                "VK_ERROR_INITIALIZATION_FAILED",
                self.trailer(255) + "Connection reset by peer",
            )
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(
                worker.subprocess, "run", return_value=completed
            ), mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ), self.assertRaises(worker.RemoteTransportError) as caught:
                worker.Remote(30422, 0).run("exit 255")
            self.assertEqual(caught.exception.returncode, 255)
            self.assertIn("worker_ssh_failed", str(caught.exception))

    def test_identity_and_known_hosts_reject_bad_permissions_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, known_hosts = self.secure_material(root)
            environment = self.environment(identity, known_hosts)

            identity.chmod(0o644)
            with mock.patch.dict(os.environ, environment, clear=False), self.assertRaisesRegex(
                worker.RemoteTransportConfigurationError,
                "identity_permissions_invalid",
            ):
                worker.Remote(30422, 0).run("true")

            identity.chmod(0o600)
            identity_target = root / "identity_target"
            identity_target.write_text("private-key-placeholder", encoding="utf-8")
            identity_target.chmod(0o600)
            identity.unlink()
            identity.symlink_to(identity_target)
            with mock.patch.dict(os.environ, environment, clear=False), self.assertRaisesRegex(
                worker.RemoteTransportConfigurationError,
                "identity_not_regular_file",
            ):
                worker.Remote(30422, 0).run("true")

            identity.unlink()
            identity.write_text("private-key-placeholder", encoding="utf-8")
            identity.chmod(0o600)
            known_hosts.chmod(0o666)
            with mock.patch.dict(os.environ, environment, clear=False), self.assertRaisesRegex(
                worker.RemoteTransportConfigurationError,
                "known_hosts_permissions_invalid",
            ):
                worker.Remote(30422, 0).run("true")

            known_hosts.chmod(0o600)
            known_hosts_target = root / "known_hosts_target"
            known_hosts_target.write_text("host key", encoding="utf-8")
            known_hosts.unlink()
            known_hosts.symlink_to(known_hosts_target)
            with mock.patch.dict(os.environ, environment, clear=False), self.assertRaisesRegex(
                worker.RemoteTransportConfigurationError,
                "known_hosts_not_regular_file",
            ):
                worker.Remote(30422, 0).run("true")

    def test_key_failure_never_falls_back_to_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            failed = subprocess.CompletedProcess([], 255, "", "publickey denied")
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(worker.subprocess, "run", return_value=failed), mock.patch.object(
                worker, "spawn_password"
            ) as password, self.assertRaisesRegex(RuntimeError, "worker_ssh_failed"):
                worker.Remote(30773, 3).run("true")
            password.assert_not_called()

    def test_missing_key_fails_before_transport_or_local_get_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, known_hosts = self.secure_material(root)
            destination = root / "must-not-exist"
            environment = {
                worker.SSH_IDENTITY_ENV: "",
                worker.SSH_KNOWN_HOSTS_ENV: str(known_hosts),
                worker.LEGACY_PASSWORD_SSH_ENV: "0",
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                worker.subprocess, "run"
            ) as run, mock.patch.object(worker, "spawn_password") as password, self.assertRaisesRegex(
                worker.RemoteTransportConfigurationError, "identity_missing"
            ):
                worker.Remote(30422, 0).get("/tmp/output", destination)
            run.assert_not_called()
            password.assert_not_called()
            self.assertFalse(destination.exists())

    def test_legacy_password_transport_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, known_hosts = self.secure_material(root)
            password_file = root / "legacy_password"
            password_file.write_text("placeholder", encoding="utf-8")
            password_file.chmod(0o600)
            environment = {
                worker.SSH_IDENTITY_ENV: "",
                worker.SSH_KNOWN_HOSTS_ENV: str(known_hosts),
                worker.LEGACY_PASSWORD_SSH_ENV: "1",
                "REMOTE_GPU_PASSWORD_FILE_30422": str(password_file),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                worker,
                "spawn_password",
                return_value="legacy-ok" + self.trailer(0),
            ) as password, mock.patch.object(worker.subprocess, "run") as run, mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ):
                output = worker.Remote(30422, 0).run("true")

            self.assertEqual(output, "legacy-ok")
            args = password.call_args.args[0]
            self.assertIn("PasswordAuthentication=yes", args)
            self.assertIn("PubkeyAuthentication=no", args)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn(f"UserKnownHostsFile={known_hosts}", args)
            self.assertNotIn("StrictHostKeyChecking=no", args)
            run.assert_not_called()

    def test_legacy_password_ssh_disconnect_remains_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, known_hosts = self.secure_material(root)
            password_file = root / "legacy_password"
            password_file.write_text("placeholder", encoding="utf-8")
            password_file.chmod(0o600)
            environment = {
                worker.SSH_IDENTITY_ENV: "",
                worker.SSH_KNOWN_HOSTS_ENV: str(known_hosts),
                worker.LEGACY_PASSWORD_SSH_ENV: "1",
                "REMOTE_GPU_PASSWORD_FILE_30422": str(password_file),
            }
            with mock.patch.dict(
                os.environ, environment, clear=False
            ), mock.patch.object(
                worker,
                "spawn_password",
                side_effect=RuntimeError(
                    "exitstatus=255 signalstatus=None\nConnection reset by peer"
                ),
            ), mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ), self.assertRaises(worker.RemoteTransportError) as caught:
                worker.Remote(30422, 0).run("true")
            self.assertEqual(caught.exception.returncode, 255)
            self.assertTrue(
                str(caught.exception).startswith(
                    "exitstatus=255 worker_ssh_failed"
                )
            )

    def test_rsync_transport_cannot_be_overridden_by_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, known_hosts = self.secure_material(Path(temporary))
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(worker.subprocess, "run") as run, self.assertRaisesRegex(
                worker.RemoteTransportConfigurationError,
                "rsync_transport_override_forbidden",
            ):
                worker.Remote(30422, 0).rsync_transfer(
                    "source",
                    "destination",
                    options=["-az", "--rsh=insecure"],
                )
            run.assert_not_called()

    def test_task1_future_remote_calls_share_the_key_only_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, known_hosts = self.secure_material(root)
            source = root / "asset.blend"
            source.write_bytes(b"BLENDER-v500")
            ssh_completed = subprocess.CompletedProcess(
                [], 0, "task1-ok\n", self.trailer(0)
            )
            rsync_completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.dict(
                os.environ, self.environment(identity, known_hosts), clear=False
            ), mock.patch.object(
                worker.subprocess,
                "run",
                side_effect=[ssh_completed, rsync_completed],
            ) as run, mock.patch.object(
                worker.secrets, "token_hex", return_value=self.NONCE
            ):
                output = task1.remote_run("true", timeout=23)
                task1.rsync_to_remote(source, "/tmp/task1/asset.blend")

            self.assertEqual(output, "task1-ok\n")
            ssh_args = run.call_args_list[0].args[0]
            self.assertEqual(ssh_args[0], "ssh")
            self.assertIn("BatchMode=yes", ssh_args)
            self.assertIn("PasswordAuthentication=no", ssh_args)
            self.assertIn("StrictHostKeyChecking=yes", ssh_args)
            rsync_args = run.call_args_list[1].args[0]
            self.assertEqual(rsync_args[0], "rsync")
            rsync_transport = shlex.split(rsync_args[rsync_args.index("-e") + 1])
            self.assertIn("BatchMode=yes", rsync_transport)
            self.assertIn("PasswordAuthentication=no", rsync_transport)
            self.assertNotIn("StrictHostKeyChecking=no", rsync_transport)

    def test_task1_and_holder_sources_have_no_insecure_transport_bypass(self) -> None:
        task1_source = (
            PROJECT / "blender/scripts/batch_bilibili_resource_model_render.py"
        ).read_text(encoding="utf-8")
        shell_source = (
            PROJECT / "blender/scripts/run_total_asset_pipeline.sh"
        ).read_text(encoding="utf-8")
        stop_start = shell_source.index("stop_remote_holder()")
        start_workers = shell_source.index("start_workers()", stop_start)
        holder_source = shell_source[stop_start:start_workers]

        self.assertNotIn("StrictHostKeyChecking=no", task1_source)
        self.assertNotIn("spawn_password", task1_source)
        self.assertIn("SecureRemote", task1_source)
        self.assertNotIn("batch_bilibili_resource_model_render", holder_source)
        self.assertIn("run_total_asset_render_worker", holder_source)
        self.assertIn("holder_remote.run", holder_source)


if __name__ == "__main__":
    unittest.main()
