from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_cli_chat_bridge as bridge
import run_video_strict_replay as replay
import video_replay_model_client as client
from video_replay_paid_api import (
    BudgetExceeded,
    BudgetPolicy,
    PaidApiLedger,
    StoredResponse,
)


class CodexBridgeTests(unittest.TestCase):
    def payload(self, image: bool = False) -> dict:
        content = [{"type": "text", "text": "Return Python code."}]
        if image:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(b"image-fixture").decode()
                    },
                }
            )
        return {
            "model": "gpt-5.6-sol",
            "max_tokens": 100,
            "messages": [
                {"role": "system", "content": "Follow supplied evidence."},
                {"role": "user", "content": content},
            ],
        }

    def fake_run(self, command, **kwargs):
        self.command = command
        self.child = kwargs
        last = Path(command[command.index("--output-last-message") + 1])
        last.write_text(
            json.dumps({"text": "def build_scene():\n    pass\n"}), encoding="utf-8"
        )
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 123, "output_tokens": 22},
                }
            )
            + "\n",
        )

    def test_cli_text_and_images_are_forwarded_with_local_temps(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(bridge.shutil, "which", return_value="/opt/codex"),
            patch.object(bridge.subprocess, "run", side_effect=self.fake_run),
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "not-forwarded",
                    "BLENDER_PIPELINE_API_ENDPOINT": "https://example.invalid",
                },
            ),
        ):
            result = bridge.send_codex_chat(
                payload=self.payload(True),
                model="gpt-5.6-sol",
                video_dir=Path(root),
                logical_call_id="call1",
                timeout_seconds=20,
            )
            self.assertIn("--image", self.command)
            self.assertIn("--ignore-user-config", self.command)
            self.assertIn("read-only", self.command)
            self.assertEqual(self.command[self.command.index("-m") + 1], "gpt-5.6-sol")
            self.assertIn("Follow supplied evidence.", self.child["input"])
            self.assertIn("Return Python code.", self.child["input"])
            self.assertNotIn("OPENAI_API_KEY", self.child["env"])
            self.assertTrue(str(self.child["cwd"]).startswith(root))
            self.assertFalse(Path(self.child["cwd"]).exists())
            receipt = json.loads(result.content)
            self.assertEqual(receipt["usage"]["total_tokens"], 145)
            self.assertEqual(receipt["transport"]["receipt_kind"], "local_cli_turn")

    def test_text_only_cli_is_supported(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(bridge.shutil, "which", return_value="/opt/codex"),
            patch.object(bridge.subprocess, "run", side_effect=self.fake_run),
        ):
            bridge.send_codex_chat(
                payload=self.payload(),
                model="gpt-5.6-sol",
                video_dir=Path(root),
                logical_call_id="call1",
                timeout_seconds=20,
            )
            self.assertNotIn("--image", self.command)

    def test_codex_never_reads_api_secret(self):
        with (
            patch.dict(os.environ, {"BLENDER_PIPELINE_PROVIDER": "codex-cli"}),
            patch.object(
                client,
                "read_paid_api_secret",
                side_effect=AssertionError("must not read"),
            ),
        ):
            self.assertEqual(client.read_model_credential(Path("/unavailable")), "")

    def test_api_default_keeps_secret_validation(self):
        with (
            patch.dict(os.environ, {"BLENDER_PIPELINE_PROVIDER": "api"}),
            patch.object(
                client, "read_paid_api_secret", return_value="credential"
            ) as read,
        ):
            self.assertEqual(
                client.read_model_credential(Path("/external/secret")), "credential"
            )
            read.assert_called_once()

    def test_bad_model_and_remote_image_rejected_before_cli(self):
        with patch.object(bridge.shutil, "which", return_value="/opt/codex"):
            with self.assertRaises(bridge.CodexBridgeError):
                bridge.validate_codex_request(self.payload(), "gpt-5.4")
            payload = self.payload(True)
            payload["messages"][1]["content"][1]["image_url"]["url"] = (
                "https://private.invalid/picture.png"
            )
            with self.assertRaises(bridge.CodexBridgeError):
                bridge.validate_codex_request(payload, "gpt-5.6-sol")

    def test_durable_cli_replay_does_not_resubmit_and_enforces_budget(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"BLENDER_PIPELINE_PROVIDER": "codex-cli"}),
            patch.object(bridge.shutil, "which", return_value="/opt/codex"),
            patch.object(bridge.subprocess, "run", side_effect=self.fake_run) as run,
            patch.object(client, "project_durable_api_call"),
        ):
            video_dir = Path(root)
            ledger = PaidApiLedger(
                video_dir / "ledger.sqlite3",
                budget=BudgetPolicy(max_calls_per_asset=1),
                minimum_free_bytes=0,
                emergency_reserve_bytes=0,
            )
            args = dict(
                video_dir=video_dir,
                stage="codegen",
                stage_key="codegen",
                prompt_version="v1",
                endpoint="",
                api_key="",
                model="gpt-5.6-sol",
                payload=self.payload(),
                timeout=(1, 10),
                ledger=ledger,
            )
            first = client.call_chat_completions(**args)
            second = client.call_chat_completions(**args)
            self.assertFalse(first.replayed)
            self.assertTrue(second.replayed)
            self.assertEqual(run.call_count, 1)
            with self.assertRaises(BudgetExceeded):
                client.call_chat_completions(
                    **{**args, "semantic_input": {"different": True}}
                )

    def test_checkpoints_are_provider_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = PaidApiLedger(
                Path(root) / "ledger.sqlite3",
                minimum_free_bytes=0,
                emergency_reserve_bytes=0,
            )
            args = dict(
                video_dir=Path(root),
                stage="codegen",
                prompt_version="v1",
                model="gpt-5.6-sol",
                semantic_input={"a": 1},
                ledger=ledger,
            )
            with patch.dict(os.environ, {"BLENDER_PIPELINE_PROVIDER": "api"}):
                client.save_stage_checkpoint(**args, output=b"api output")
            with patch.dict(os.environ, {"BLENDER_PIPELINE_PROVIDER": "codex-cli"}):
                self.assertIsNone(client.load_stage_checkpoint(**args))
                client.save_stage_checkpoint(**args, output=b"cli output")
                self.assertEqual(client.load_stage_checkpoint(**args), b"cli output")

    def test_model_reroute_rejected(self):
        with self.assertRaises(Exception):
            bridge._usage('{"type":"model_reroute"}\n')

    def test_image_manifest_is_run_local_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            image = directory / "input.png"
            image.write_bytes(b"fixture")
            row = {
                "path": str(image),
                "role": "supporting",
                "sha256": hashlib.sha256(b"fixture").hexdigest(),
            }
            (directory / "input_assets.json").write_text(json.dumps({"assets": [row]}))
            result = replay.supplied_image_manifest(directory)
            self.assertTrue(result[0]["blender_image_name"].startswith("INPUT_"))
            row["sha256"] = "wrong"
            (directory / "input_assets.json").write_text(json.dumps({"assets": [row]}))
            with self.assertRaises(ValueError):
                replay.supplied_image_manifest(directory)

    def test_generated_code_can_use_preloaded_images_not_open_files(self):
        replay.validate_generated_code_safety(
            'import bpy\ndef build_scene():\n    image = bpy.data.images.get("INPUT_example")\n'
        )
        with self.assertRaises(replay.GeneratedCodeSafetyError):
            replay.validate_generated_code_safety(
                'import bpy\ndef build_scene():\n    bpy.data.images.load("/private/path.png")\n'
            )

    def test_prompt_import_contract_matches_unchanged_ast_gate(self):
        prompt = replay.GENERATED_CODE_SAFETY_PROMPT
        self.assertIn(", ".join(sorted(replay.SAFE_GENERATED_IMPORT_ROOTS)), prompt)
        self.assertIn("including json", prompt)
        self.assertIn("plain strings, numbers, or simple lists", prompt)
        with self.assertRaises(replay.GeneratedCodeSafetyError):
            replay.validate_generated_code_safety(
                "import bpy\nimport json\ndef build_scene():\n    pass\n"
            )


class ApiModelIdentityTests(unittest.TestCase):
    def test_custom_model_is_preserved_in_wire_request_and_ledger(self):
        model = "Vendor/Vision-Code:2026-09"
        endpoint = "https://example.invalid/v1/chat/completions"
        ledger = Mock()
        ledger.prepare_call.return_value = SimpleNamespace(
            logical_call_id="fixture", state="planned", endpoint=endpoint
        )
        stored = StoredResponse(
            logical_call_id="fixture",
            status_code=200,
            headers={},
            content=b'{"choices":[{"message":{"content":"done"}}]}',
            provider_request_id="",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            replayed=False,
        )

        def execute(_call_id, send, **_kwargs):
            request = ledger.prepare_call.call_args.kwargs["request_payload"]
            send(endpoint, json.dumps(request).encode("utf-8"))
            return stored

        ledger.execute.side_effect = execute
        with (
            patch.object(client, "task_identity", return_value="fixture"),
            patch.object(client, "knowledge_version", return_value="fixture-v1"),
            patch.dict(
                os.environ,
                {
                    "BLENDER_PIPELINE_PROVIDER": "api",
                    client.APPROVED_ENDPOINT_ENV: endpoint,
                },
            ),
            patch.object(client, "DEFAULT_PROVIDER_USAGE_ENDPOINT", ""),
            patch.object(client.requests, "Session") as session,
        ):
            result = client.call_chat_completions(
                video_dir=Path("/unused"),
                stage="codegen",
                stage_key="codegen",
                prompt_version="v1",
                endpoint=endpoint,
                api_key="fixture-not-a-secret",
                model=model,
                payload={"model": model, "messages": [], "max_tokens": 100},
                timeout=(1, 10),
                ledger=ledger,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(ledger.prepare_call.call_args.kwargs["model"], model)
        wire = json.loads(session.return_value.post.call_args.kwargs["data"])
        self.assertEqual(wire["model"], model)
        self.assertFalse(session.return_value.trust_env)

    def test_invalid_or_mismatched_model_rejected_before_request(self):
        with patch.object(client.requests, "Session") as session:
            for model in (
                "", " padded", "padded ", "line\nbreak", "x" * 257, "Vendor/Model"
            ):
                with self.subTest(model=model), self.assertRaises(ValueError):
                    client.call_chat_completions(
                        video_dir=Path("/unused"),
                        stage="codegen",
                        stage_key="codegen",
                        prompt_version="v1",
                        endpoint="https://example.invalid/v1/chat/completions",
                        api_key="fixture-not-a-secret",
                        model=model,
                        payload={"model": "different-model", "messages": []},
                        timeout=(1, 10),
                    )
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
