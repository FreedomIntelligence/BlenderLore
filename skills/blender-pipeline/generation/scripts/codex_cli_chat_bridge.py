"""Adapt a pinned, authenticated Codex CLI turn to the existing replay contract.

The CLI is a model transport only: it cannot edit the workspace or run Blender.
The caller owns durable call budgets, replay receipts and all execution gates.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


class CodexBridgeError(RuntimeError):
    pass


def _usage(stdout: str) -> dict[str, int]:
    # Share the extraction transport's strict telemetry/reroute validation.
    extraction = Path(__file__).resolve().parents[2] / "tutorial-extraction/scripts"
    if str(extraction) not in sys.path:
        sys.path.insert(0, str(extraction))
    return importlib.import_module("tutorial_extraction_core")._codex_cli_usage(stdout)


def validate_codex_request(payload: Mapping[str, Any], model: str) -> str:
    """Reject unsupported content before reserving/submitting a durable call."""

    if model not in {"gpt-5.6-sol", "gpt-5.5"}:
        raise CodexBridgeError("Codex replay requires gpt-5.6-sol or explicit gpt-5.5")
    if payload.get("model", model) != model:
        raise CodexBridgeError("payload model differs from the pinned replay model")
    executable = shutil.which("codex")
    if executable is None:
        raise CodexBridgeError("Codex CLI is not installed or is not on PATH")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CodexBridgeError("Codex replay requires nonempty chat messages")
    for message in messages:
        if not isinstance(message, Mapping):
            raise CodexBridgeError("chat message must be an object")
        if message.get("role") not in {"system", "developer", "user", "assistant"}:
            raise CodexBridgeError("unsupported chat role in Codex replay")
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise CodexBridgeError("unsupported chat content in Codex replay")
        for part in content:
            if not isinstance(part, Mapping):
                raise CodexBridgeError("chat content part must be an object")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                continue
            image = part.get("image_url")
            if part.get("type") != "image_url" or not isinstance(image, Mapping):
                raise CodexBridgeError(
                    "Codex replay supports text and embedded images only"
                )
            url = image.get("url")
            if not isinstance(url, str) or not url.startswith("data:image/"):
                raise CodexBridgeError("Codex replay images must be embedded data URLs")
            header, separator, encoded = url.partition(",")
            if not separator or ";base64" not in header:
                raise CodexBridgeError("Codex replay image must use base64 encoding")
            if header.split(";", 1)[0] not in {
                "data:image/png",
                "data:image/jpeg",
                "data:image/webp",
                "data:image/gif",
            }:
                raise CodexBridgeError("unsupported embedded image format")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise CodexBridgeError("invalid embedded image encoding") from exc
            if not decoded or len(decoded) > 32 * 1024 * 1024:
                raise CodexBridgeError("embedded image is empty or exceeds 32 MiB")
    return executable


def send_codex_chat(
    *,
    payload: Mapping[str, Any],
    model: str,
    video_dir: Path,
    logical_call_id: str,
    timeout_seconds: float,
) -> SimpleNamespace:
    """Return a chat-shaped *local CLI receipt*, never an invented API receipt."""

    executable = validate_codex_request(payload, model)
    temporary_root = video_dir.resolve() / ".control/codex_cli"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="turn-", dir=temporary_root) as value:
        temporary = Path(value)
        images: list[Path] = []
        prompt_sections: list[str] = []
        for message in payload["messages"]:
            parts = message["content"]
            content: list[str] = []
            if isinstance(parts, str):
                content.append(parts)
            else:
                for part in parts:
                    if part["type"] == "text":
                        content.append(part["text"])
                    else:
                        url = part["image_url"]["url"]
                        header, encoded = url.split(",", 1)
                        extension = header.split("/", 1)[1].split(";", 1)[0]
                        path = temporary / f"image_{len(images) + 1:03d}.{extension}"
                        path.write_bytes(base64.b64decode(encoded, validate=True))
                        images.append(path)
                        content.append(f"[Attached image {len(images)}: {path.name}]")
            prompt_sections.append(
                f"--- {message['role']} message ---\n" + "\n".join(content)
            )
        prompt = (
            "Act only as the assistant answering the following ordered conversation. "
            "Do not inspect other files, use tools, run commands, or execute generated code. "
            "Return the requested assistant response verbatim in the JSON string field text. "
            "If the task requests Python code, text must contain that complete Python code; "
            "if it requests JSON, text must contain that JSON object as a string.\n\n"
            + "\n\n".join(prompt_sections)
        )
        schema = temporary / "response_schema.json"
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                }
            ),
            encoding="utf-8",
        )
        last = temporary / "response.json"
        command = [
            executable,
            "exec",
            "-m",
            model,
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(last),
            "--output-schema",
            str(schema),
        ]
        for path in images:
            command.extend(["--image", str(path)])
        command.append("-")
        environment = os.environ.copy()
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "BLENDER_PIPELINE_API_KEY_FILE",
            "BLENDER_PIPELINE_API_ENDPOINT",
            "VIDEO_REPLAY_APPROVED_PAID_API_ENDPOINT",
        ):
            environment.pop(name, None)
        environment.update(
            {"TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary)}
        )
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=temporary,
                env=environment,
                timeout=max(1.0, timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexBridgeError(
                "Codex model turn timed out; automatic resubmission is disabled"
            ) from exc
        except OSError as exc:
            raise CodexBridgeError(
                f"Codex could not start: {type(exc).__name__}"
            ) from exc
        if completed.returncode != 0:
            digest = hashlib.sha256(
                completed.stderr.encode("utf-8", errors="replace")
            ).hexdigest()
            raise CodexBridgeError(
                f"Codex failed (exit {completed.returncode}; stderr_sha256={digest})"
            )
        usage = _usage(completed.stdout)
        if not last.is_file() or last.stat().st_size > 2 * 1024 * 1024:
            raise CodexBridgeError("Codex response is absent or exceeds 2 MiB")
        try:
            result = json.loads(last.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError, OSError) as exc:
            raise CodexBridgeError("Codex returned invalid structured output") from exc
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("text"), str)
            or not result["text"].strip()
        ):
            raise CodexBridgeError("Codex returned no assistant response text")
        receipt_id = f"codex-cli-local:{logical_call_id}"
        response = {
            "id": receipt_id,
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result["text"]},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
            "transport": {
                "provider": "codex-cli",
                "receipt_kind": "local_cli_turn",
                "model_identity": "explicit_cli_pin_no_reroute_event",
            },
        }
        return SimpleNamespace(
            status_code=200,
            headers={"x-request-id": receipt_id, "x-model-transport": "codex-cli"},
            content=json.dumps(response, ensure_ascii=False).encode("utf-8"),
        )
