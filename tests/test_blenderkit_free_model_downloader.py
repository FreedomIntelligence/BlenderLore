from __future__ import annotations

import importlib.util
import csv
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "blender/scripts/blenderkit_free_model_downloader.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("blenderkit_free_model_downloader", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)


class BlenderKitPrivateCurlTests(unittest.TestCase):
    def test_queue_wrapper_does_not_trigger_asset_sync_by_default(self) -> None:
        source = (SCRIPT.parent / "run_blenderkit_free_downloads.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('TRIGGER_SYNC="${TRIGGER_SYNC:-0}"', source)
        self.assertNotIn("--max-file-size-mb '$MAX_FILE_SIZE_MB' \\\n+    --trigger-sync", source)

    def test_json_request_keeps_bearer_and_signed_url_out_of_argv(self) -> None:
        secret = "secret-bearer-value"
        signed_url = "https://example.invalid/api?token=signed-value"

        def run(command, **_kwargs):
            joined = " ".join(command)
            self.assertNotIn(secret, joined)
            self.assertNotIn(signed_url, joined)
            config = Path(command[command.index("--config") + 1])
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            payload = config.read_text(encoding="utf-8")
            self.assertIn(secret, payload)
            self.assertIn(signed_url, payload)
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True}).encode(), b"")

        with mock.patch.object(downloader.subprocess, "run", side_effect=run):
            self.assertEqual(
                downloader.curl_json(signed_url, api_key=secret), {"ok": True}
            )

    def test_download_keeps_private_material_out_of_argv_and_is_atomic(self) -> None:
        secret = "secret-bearer-value"
        signed_url = "https://example.invalid/file?token=signed-value"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "asset.blend"

            def run(command, **_kwargs):
                joined = " ".join(command)
                self.assertNotIn(secret, joined)
                self.assertNotIn(signed_url, joined)
                Path(command[command.index("-o") + 1]).write_bytes(b"BLENDER-v450")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with mock.patch.object(downloader.subprocess, "run", side_effect=run):
                downloader.download_file(signed_url, destination, 30, secret)
            self.assertEqual(destination.read_bytes(), b"BLENDER-v450")
            self.assertFalse(destination.with_suffix(".blend.part").exists())

    def test_curl_config_rejects_line_injection(self) -> None:
        with self.assertRaises(ValueError):
            with downloader.private_curl_config("https://example.invalid/\nheader=x"):
                pass

    def test_progress_redacts_endpoint_and_error_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / "progress.csv"
            downloader.append_progress(progress, {
                "asset_id": "asset-a",
                "download_api_url": "https://example.invalid/download?token=private-token",
                "status": "failed",
                "error": "Authorization: Bearer private-bearer https://example.invalid/x?sig=private-sig",
            })
            raw = progress.read_text(encoding="utf-8-sig")
            self.assertNotIn("private-token", raw)
            self.assertNotIn("private-bearer", raw)
            self.assertNotIn("private-sig", raw)
            with progress.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertRegex(row["download_api_url"], r"^sha256:[0-9a-f]{64}$")
            self.assertIn("[redacted]", row["error"])


if __name__ == "__main__":
    unittest.main()
