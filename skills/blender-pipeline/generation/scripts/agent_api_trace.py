"""Lossless API request/response tracing without persisting credentials."""

from __future__ import annotations

import gzip
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from agent_trajectory import AgentTrajectory, TrajectoryError, load_and_verify
except ImportError:  # Package import in unit tests.
    from .agent_trajectory import AgentTrajectory, TrajectoryError, load_and_verify


SCHEMA = "video2blender.agent-api-trace.v1"
SENSITIVE_HEADER = re.compile(r"authorization|api[-_]?key|cookie|token|secret", re.I)
SENSITIVE_QUERY = re.compile(
    r"api[-_]?key|access[-_]?token|token|secret|signature", re.I
)
DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/[A-Za-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\r\n]+)$"
)


def _safe_stage(value: str) -> str:
    stage = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return stage[:80] or "api"


def _redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        str(key): "<redacted>" if SENSITIVE_HEADER.search(str(key)) else str(value)
        for key, value in (headers or {}).items()
    }


def _redact_url(value: str) -> str:
    parts = urlsplit(value)
    query = [
        (key, "<redacted>" if SENSITIVE_QUERY.search(key) else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _atomic_bytes(path: Path, payload: bytes, *, gzip_payload: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            if gzip_payload:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                    compressed.write(payload)
            else:
                raw.write(payload)
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _model_input_attachments(
    recorder: AgentTrajectory, payload: object
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(value: object, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                visit(child, f"{path}.{key}")
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
            return
        if not isinstance(value, str):
            return
        match = DATA_URL_RE.fullmatch(value)
        if match is None:
            return
        try:
            content = base64.b64decode(match.group("data"), validate=True)
        except (ValueError, binascii.Error):
            return
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        extension = match.group("mime").split("/", 1)[1].replace("jpeg", "jpg")
        attachment = recorder.store_bytes(
            content,
            name=f"model_input_{len(attachments) + 1:03d}.{extension}",
            mime_type=match.group("mime"),
        )
        attachment.update(
            {
                "semantic_role": "model_visible_input",
                "payload_path": path,
                "transform": "exact_api_data_url_bytes",
            }
        )
        attachments.append(attachment)

    visit(payload, "payload")
    return attachments


def start_api_trace(
    base_dir: Path,
    *,
    stage: str,
    attempt: int,
    method: str,
    endpoint: str,
    headers: Mapping[str, Any] | None,
    payload: object,
    logical_call_id: str = "",
) -> Path:
    """Persist the complete semantic request before network I/O."""

    if logical_call_id and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", logical_call_id
    ):
        raise ValueError("logical_call_id is not safe for a trace path")
    if logical_call_id:
        trace_name = logical_call_id
    else:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        trace_name = (
            f"{stamp}.{time.time_ns() % 1_000_000_000:09d}"
            f"-a{attempt:02d}-{uuid.uuid4().hex[:8]}"
        )
    trace_dir = base_dir / "agent_api_trace" / _safe_stage(stage) / trace_name
    body = _json_bytes(payload)
    if trace_dir.exists():
        if not logical_call_id:
            raise FileExistsError(trace_dir)
        meta_path = trace_dir / "request_meta.json"
        if not meta_path.is_file():
            raise TrajectoryError("durable API trace directory is incomplete")
        existing = json.loads(meta_path.read_text(encoding="utf-8"))
        if (
            existing.get("payload_sha256") != hashlib.sha256(body).hexdigest()
            or existing.get("method") != method
            or existing.get("endpoint") != _redact_url(endpoint)
        ):
            raise TrajectoryError(
                "logical_call_id already identifies a different API request"
            )
        return trace_dir
    trace_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(trace_dir, 0o700)
    _atomic_bytes(trace_dir / "request_payload.json.gz", body, gzip_payload=True)
    _atomic_bytes(
        trace_dir / "request_meta.json",
        _json_bytes(
            {
                "schema": SCHEMA,
                "stage": stage,
                "attempt": attempt,
                "method": method,
                "endpoint": _redact_url(endpoint),
                "headers": _redact_headers(headers),
                "payload_sha256": hashlib.sha256(body).hexdigest(),
                "payload_size_bytes": len(body),
                "started_at_epoch_ns": time.time_ns(),
            }
        ),
    )
    recorder = AgentTrajectory.from_environment()
    if recorder is not None:
        call_id = (
            "api_" + logical_call_id if logical_call_id else "api_" + uuid.uuid4().hex
        )
        attachment = recorder.store_json(payload, name="request_payload.json")
        model_inputs = _model_input_attachments(recorder, payload)
        events = (
            load_and_verify(recorder.root)[1] if recorder.events_path.is_file() else []
        )
        prior = [
            event
            for event in events
            if event.get("event_type") == "api_request"
            and event.get("tool_call_id") == call_id
        ]
        if not prior:
            recorder.append_event(
                event_type="api_request",
                role="user",
                stage=stage,
                attempt=attempt,
                turn=attempt,
                tool_name="model_api",
                tool_call_id=call_id,
                content={
                    "method": method,
                    "endpoint": _redact_url(endpoint),
                    "headers": _redact_headers(headers),
                    "request_payload_attachment": attachment,
                },
                attachments=[attachment, *model_inputs],
            )
        elif len(prior) != 1 or attachment["sha256"] != (
            next(
                (
                    item
                    for item in prior[0].get("attachments", [])
                    if item.get("name") == "request_payload.json"
                ),
                {},
            ).get("sha256")
        ):
            raise TrajectoryError(
                "logical API call has a conflicting trajectory request event"
            )
        _atomic_bytes(
            trace_dir / "trajectory_link.json",
            _json_bytes(
                {
                    "trajectory_root": str(recorder.root),
                    "tool_call_id": call_id,
                    "stage": stage,
                    "attempt": attempt,
                }
            ),
        )
    return trace_dir


def _trajectory_link(trace_dir: Path) -> tuple[AgentTrajectory, dict[str, Any]] | None:
    link_path = trace_dir / "trajectory_link.json"
    if not link_path.is_file():
        return None
    link = json.loads(link_path.read_text(encoding="utf-8"))
    return AgentTrajectory(Path(str(link["trajectory_root"]))), link


def record_api_response(
    trace_dir: Path, response: Any, *, elapsed_seconds: float
) -> None:
    """Persist the exact response bytes and the prepared request wire body."""

    content = bytes(getattr(response, "content", b""))
    prepared = getattr(response, "request", None)
    request_body = getattr(prepared, "body", b"") if prepared is not None else b""
    if isinstance(request_body, str):
        request_body = request_body.encode("utf-8")
    elif request_body is None:
        request_body = b""
    elif not isinstance(request_body, bytes):
        request_body = bytes(request_body)
    response_meta_path = trace_dir / "response_meta.json"
    if response_meta_path.is_file():
        existing = json.loads(response_meta_path.read_text(encoding="utf-8"))
        if (
            existing.get("response_sha256") != hashlib.sha256(content).hexdigest()
            or existing.get("wire_request_sha256")
            != hashlib.sha256(request_body).hexdigest()
            or existing.get("status_code") != int(getattr(response, "status_code", 0))
        ):
            raise TrajectoryError("durable API response projection conflicts")
    else:
        _atomic_bytes(trace_dir / "response_body.bin.gz", content, gzip_payload=True)
        _atomic_bytes(
            trace_dir / "wire_request_body.bin.gz",
            request_body,
            gzip_payload=True,
        )
        _atomic_bytes(
            response_meta_path,
            _json_bytes(
                {
                    "schema": SCHEMA,
                    "status_code": int(getattr(response, "status_code", 0)),
                    "url": _redact_url(str(getattr(response, "url", ""))),
                    "headers": _redact_headers(getattr(response, "headers", {})),
                    "request_headers": _redact_headers(
                        getattr(prepared, "headers", {}) if prepared is not None else {}
                    ),
                    "response_sha256": hashlib.sha256(content).hexdigest(),
                    "response_size_bytes": len(content),
                    "wire_request_sha256": hashlib.sha256(request_body).hexdigest(),
                    "wire_request_size_bytes": len(request_body),
                    "elapsed_seconds": elapsed_seconds,
                    "completed_at_epoch_ns": time.time_ns(),
                }
            ),
        )
    linked = _trajectory_link(trace_dir)
    if linked is not None:
        recorder, link = linked
        response_attachment = recorder.store_bytes(
            content, name="response_body.bin", mime_type="application/json"
        )
        wire_attachment = recorder.store_bytes(
            request_body, name="wire_request_body.bin", mime_type="application/json"
        )
        events = load_and_verify(recorder.root)[1]
        prior = [
            event
            for event in events
            if event.get("event_type") == "api_response"
            and event.get("tool_call_id") == str(link["tool_call_id"])
        ]
        if not prior:
            recorder.append_event(
                event_type="api_response",
                role="assistant",
                stage=str(link["stage"]),
                attempt=int(link["attempt"]),
                turn=int(link["attempt"]),
                tool_name="model_api",
                tool_call_id=str(link["tool_call_id"]),
                status=(
                    "ok" if int(getattr(response, "status_code", 0)) < 400 else "error"
                ),
                content={
                    "status_code": int(getattr(response, "status_code", 0)),
                    "url": _redact_url(str(getattr(response, "url", ""))),
                    "headers": _redact_headers(getattr(response, "headers", {})),
                    "elapsed_seconds": elapsed_seconds,
                    "response_attachment": response_attachment,
                    "wire_request_attachment": wire_attachment,
                },
                attachments=[response_attachment, wire_attachment],
            )
        elif len(prior) != 1:
            raise TrajectoryError(
                "logical API call has duplicate trajectory response events"
            )


def project_durable_api_call(
    base_dir: Path,
    *,
    logical_call_id: str,
    stage: str,
    attempt: int,
    endpoint: str,
    request_payload: object,
    wire_request_body: bytes,
    response_body: bytes,
    status_code: int,
    response_headers: Mapping[str, Any] | None = None,
    provider_request_id: str = "",
    elapsed_seconds: float = 0.0,
) -> Path:
    """Idempotently project one durable ledger exchange into trace artifacts.

    This helper performs no network I/O.  Its directory and trajectory
    tool_call_id are derived from ``logical_call_id``; repeated projection is a
    no-op, while conflicting bytes fail closed.
    """

    trace_dir = start_api_trace(
        base_dir,
        stage=stage,
        attempt=attempt,
        method="POST",
        endpoint=endpoint,
        headers={},
        payload=request_payload,
        logical_call_id=logical_call_id,
    )
    headers = dict(response_headers or {})
    if provider_request_id and not any(
        str(key).lower() == "x-request-id" for key in headers
    ):
        headers["X-Request-ID"] = provider_request_id
    prepared = SimpleNamespace(body=bytes(wire_request_body), headers={})
    response = SimpleNamespace(
        content=bytes(response_body),
        status_code=int(status_code),
        url=endpoint,
        headers=headers,
        request=prepared,
    )
    record_api_response(trace_dir, response, elapsed_seconds=float(elapsed_seconds))
    return trace_dir


def record_api_exception(
    trace_dir: Path, exc: BaseException, *, elapsed_seconds: float
) -> None:
    # Transport exceptions may embed a URL, request headers, or credentials.
    # Preserve a deterministic diagnostic without persisting the raw text.
    error_bytes = str(exc).encode("utf-8", errors="replace")
    error_summary = (
        f"{type(exc).__name__}:sha256={hashlib.sha256(error_bytes).hexdigest()}"
    )
    _atomic_bytes(
        trace_dir / "exception.json",
        _json_bytes(
            {
                "schema": SCHEMA,
                "exception_type": type(exc).__name__,
                "error_summary": error_summary,
                "elapsed_seconds": elapsed_seconds,
                "completed_at_epoch_ns": time.time_ns(),
            }
        ),
    )
    linked = _trajectory_link(trace_dir)
    if linked is not None:
        recorder, link = linked
        recorder.append_event(
            event_type="api_response",
            role="assistant",
            stage=str(link["stage"]),
            attempt=int(link["attempt"]),
            turn=int(link["attempt"]),
            tool_name="model_api",
            tool_call_id=str(link["tool_call_id"]),
            status="error",
            content={
                "exception_type": type(exc).__name__,
                "error_summary": error_summary,
                "elapsed_seconds": elapsed_seconds,
            },
        )
