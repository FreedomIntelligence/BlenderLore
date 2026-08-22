"""Durable, at-most-once paid API calls for video replay preparation.

The SQLite ledger is the source of truth for network delivery.  Agent
trajectory/SFT files are deliberately treated as derived projections: a
projection failure is recorded, but it can never make a paid call eligible for
another POST.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import email.utils
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit


SCHEMA = "video2blender.api-call-envelope.v2"
CHECKPOINT_SCHEMA = "video2blender.stage-checkpoint.v1"
CALL_STATES = (
    "planned",
    "request_durable",
    "sent",
    "response_durable",
    "consumed",
    "sent_unknown",
)
TERMINAL_RESPONSE_STATES = ("response_durable", "consumed")
SENSITIVE_HEADER = re.compile(
    r"authorization|api[-_]?key|cookie|password|secret|token", re.I
)
DEFAULT_STAGE_LIMITS = {
    "tutorial": 2,
    "codegen": 5,
    "visual_review": 3,
}
UPSTREAM_ERROR_WINDOW_NS = 10 * 60 * 1_000_000_000
UPSTREAM_ERROR_THRESHOLD = 3
# Formal RW1/RW2 calls are multimodal. A tiny, single-flight multimodal probe
# is substantially cheaper than leaving production blocked for half an hour,
# and distinguishes recovery of the text route from the vision route.
UPSTREAM_ERROR_RETRY_NS = 5 * 60 * 1_000_000_000
RECOVERY_PROBE_RETRY_NS = 5 * 60 * 1_000_000_000
RECOVERABLE_CIRCUIT_REASONS = frozenset(
    {
        "http_429",
        "provider_health_probe_delivery_unknown",
        "provider_upstream_error_400",
        "provider_upstream_error_5xx",
    }
)


class PaidApiError(RuntimeError):
    """Base error for the durable paid API layer."""


class BudgetExceeded(PaidApiError):
    pass


class CircuitOpen(PaidApiError):
    pass


class DeliveryUnknown(PaidApiError):
    pass


class InvalidTransition(PaidApiError):
    pass


class DiskPreflightError(PaidApiError):
    pass


def _is_provider_upstream_error(
    status_code: int, content: bytes | bytearray | memoryview
) -> bool:
    """Recognize the provider's transient upstream failure envelope."""

    if int(status_code) != 400:
        return False
    try:
        payload = json.loads(bytes(content).decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    return bool(
        isinstance(error, dict) and str(error.get("type") or "") == "upstream_error"
    )


@dataclasses.dataclass(frozen=True)
class BudgetPolicy:
    max_calls_per_asset: int = 32
    max_tokens_per_asset: int = 500_000
    max_total_calls: int | None = None
    max_total_tokens: int | None = None
    stage_call_limits: Mapping[str, int] = dataclasses.field(
        default_factory=lambda: dict(DEFAULT_STAGE_LIMITS)
    )

    def __post_init__(self) -> None:
        if self.max_calls_per_asset < 1 or self.max_tokens_per_asset < 1:
            raise ValueError("asset budget limits must be positive")
        if self.max_total_calls is not None and self.max_total_calls < 1:
            raise ValueError("global call budget must be positive")
        if self.max_total_tokens is not None and self.max_total_tokens < 1:
            raise ValueError("global token budget must be positive")
        if any(int(value) < 1 for value in self.stage_call_limits.values()):
            raise ValueError("stage call limits must be positive")


@dataclasses.dataclass(frozen=True)
class ApiCallEnvelopeV2:
    logical_call_id: str
    asset_id: str
    stage: str
    stage_key: str
    input_sha256: str
    model: str
    prompt_version: str
    knowledge_version: str
    endpoint: str
    request_sha256: str
    state: str
    reserved_tokens: int
    total_tokens: int | None
    provider_request_id: str
    response_status_code: int | None
    projection_state: str


@dataclasses.dataclass(frozen=True)
class StoredResponse:
    logical_call_id: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    provider_request_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    replayed: bool

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


@dataclasses.dataclass(frozen=True)
class StageCheckpointV1:
    asset_id: str
    stage: str
    input_sha256: str
    model: str
    prompt_version: str
    knowledge_version: str
    output_sha256: str
    output: bytes
    created_at_epoch_ns: int


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def payload_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def deterministic_logical_call_id(
    *,
    asset_id: str,
    stage: str,
    input_sha256: str,
    model: str,
    prompt_version: str,
    knowledge_version: str,
) -> str:
    """Return a stable ID for the semantic call, independent of lease/attempt."""

    fields = {
        "schema": SCHEMA,
        "asset_id": str(asset_id),
        "stage": str(stage),
        "input_sha256": str(input_sha256).lower(),
        "model": str(model),
        "prompt_version": str(prompt_version),
        "knowledge_version": str(knowledge_version),
    }
    if not all(fields[key] for key in fields if key != "schema"):
        raise ValueError("logical call identity fields must not be empty")
    if not re.fullmatch(r"[0-9a-f]{64}", fields["input_sha256"]):
        raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
    return "callv2_" + hashlib.sha256(canonical_json_bytes(fields)).hexdigest()


def normalize_chat_completions_endpoint(value: str) -> str:
    """Accept a provider base URL or a complete chat-completions URL."""

    raw = str(value).strip()
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("paid API endpoint must be an absolute HTTPS URL")
    if parts.username or parts.password:
        raise ValueError("paid API endpoint must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("paid API endpoint must not contain query or fragment data")
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        normalized_path = path
    elif path.endswith("/v1"):
        normalized_path = path + "/chat/completions"
    elif not path:
        normalized_path = "/v1/chat/completions"
    else:
        normalized_path = path + "/v1/chat/completions"
    return urlunsplit((parts.scheme.lower(), parts.netloc, normalized_path, "", ""))


def disk_preflight(path: Path, *, minimum_free_bytes: int) -> None:
    """Fail before network I/O when the durable ledger cannot be trusted."""

    if minimum_free_bytes < 0:
        raise ValueError("minimum_free_bytes must not be negative")
    probe = path.expanduser()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        raise DiskPreflightError(f"disk capacity is unavailable for {probe}") from exc
    if usage.total <= 0 or usage.free < 0 or usage.free > usage.total:
        raise DiskPreflightError(f"disk capacity is invalid for {probe}")
    if usage.free < minimum_free_bytes:
        raise DiskPreflightError(
            f"disk free space is below the paid-call reserve: "
            f"{usage.free} < {minimum_free_bytes}"
        )


def _redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        str(key): "<redacted>" if SENSITIVE_HEADER.search(str(key)) else str(value)
        for key, value in (headers or {}).items()
    }


def _response_parts(response: Any) -> tuple[int, dict[str, str], bytes]:
    status_code = int(getattr(response, "status_code", 0))
    if not 100 <= status_code <= 599:
        raise ValueError("provider response status code is outside HTTP range")
    headers = {
        str(key): str(value)
        for key, value in dict(getattr(response, "headers", {}) or {}).items()
    }
    content = getattr(response, "content", b"")
    if isinstance(content, str):
        content = content.encode("utf-8")
    elif not isinstance(content, bytes):
        content = bytes(content)
    return status_code, headers, content


def _best_effort_response_parts(
    response: Any,
) -> tuple[int, dict[str, str], bytes]:
    """Extract only evidence that is independently safe to serialize.

    This helper is used after a provider returned but the normal response
    adapter raised.  Each field is isolated so one hostile/broken attribute
    cannot prevent preservation of the other evidence.
    """

    try:
        status_code = int(getattr(response, "status_code", 0))
    except BaseException:
        status_code = 0
    try:
        headers = {
            str(key): str(value)
            for key, value in dict(getattr(response, "headers", {}) or {}).items()
        }
    except BaseException:
        headers = {}
    try:
        content = getattr(response, "content", b"")
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        elif not isinstance(content, bytes):
            content = bytes(content)
    except BaseException:
        content = b""
    return status_code, headers, content


def _nonnegative_usage_int(value: object) -> tuple[int, bool]:
    if value is None or value == "":
        return 0, False
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0, True
    if parsed < 0:
        return 0, True
    return parsed, False


def decode_chat_response_payload(content: bytes) -> Mapping[str, object]:
    """Decode either ordinary JSON or an OpenAI-compatible SSE response."""

    text = content.decode("utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        return payload

    chunks: list[Mapping[str, object]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        chunk = json.loads(data)
        if isinstance(chunk, Mapping):
            chunks.append(chunk)
    if not chunks:
        raise ValueError("response is neither JSON nor SSE chat data")
    for chunk in chunks:
        if isinstance(chunk.get("error"), Mapping):
            return chunk

    messages: dict[int, dict[str, object]] = {}
    finish_reasons: dict[int, object] = {}
    usage: Mapping[str, object] | None = None
    for chunk in chunks:
        if isinstance(chunk.get("usage"), Mapping):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            index = int(choice.get("index") or 0)
            message = messages.setdefault(index, {"role": "assistant", "content": ""})
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                delta = choice.get("message")
            if isinstance(delta, Mapping):
                role = delta.get("role")
                if isinstance(role, str) and role:
                    message["role"] = role
                piece = delta.get("content")
                if isinstance(piece, str):
                    message["content"] = str(message["content"]) + piece
            if choice.get("finish_reason") is not None:
                finish_reasons[index] = choice.get("finish_reason")
    first = chunks[0]
    result: dict[str, object] = {
        "id": str(first.get("id") or ""),
        "object": "chat.completion",
        "model": str(first.get("model") or ""),
        "choices": [
            {
                "index": index,
                "message": message,
                "finish_reason": finish_reasons.get(index),
            }
            for index, message in sorted(messages.items())
        ],
    }
    if usage is not None:
        result["usage"] = dict(usage)
    return result


def _usage_from_response(
    content: bytes,
    headers: Mapping[str, str],
    *,
    status_code: int,
) -> tuple[int, int, int, str, str]:
    processing_error = ""
    try:
        payload = decode_chat_response_payload(content)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        payload = {}
        processing_error = "response_json_invalid"
    usage_value = payload.get("usage") if isinstance(payload, Mapping) else {}
    if usage_value is None and status_code >= 400:
        # Error responses commonly omit usage. The returned HTTP status and
        # raw body are durable proof that this logical call completed; absence
        # of success-only billing fields is not a response-parser failure and
        # must not poison unrelated paid callers.
        usage = {}
    elif not isinstance(usage_value, Mapping):
        usage = {}
        processing_error = processing_error or "provider_usage_invalid"
    else:
        usage = usage_value
    prompt, prompt_invalid = _nonnegative_usage_int(usage.get("prompt_tokens"))
    completion, completion_invalid = _nonnegative_usage_int(
        usage.get("completion_tokens")
    )
    total_value = usage.get("total_tokens")
    if total_value is None or total_value == "":
        total = prompt + completion
        total_invalid = False
    else:
        total, total_invalid = _nonnegative_usage_int(total_value)
    if prompt_invalid or completion_invalid or total_invalid:
        processing_error = processing_error or "provider_usage_invalid"
    if status_code < 400 and not usage and not processing_error:
        processing_error = "provider_usage_missing"
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    request_id = (
        lowered.get("x-request-id")
        or lowered.get("request-id")
        or lowered.get("x-shellapi-request-id")
        or (str(payload.get("id") or "") if isinstance(payload, Mapping) else "")
    )
    if status_code < 400 and not request_id:
        processing_error = processing_error or "provider_request_id_missing"
    return prompt, completion, total, request_id, processing_error


def _retry_after_epoch_ns(headers: Mapping[str, str], *, now_epoch_ns: int) -> int:
    lowered = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    value = lowered.get("retry-after", "")
    if value:
        try:
            seconds = max(0.0, float(value))
            return now_epoch_ns + int(seconds * 1_000_000_000)
        except ValueError:
            try:
                moment = email.utils.parsedate_to_datetime(value)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=dt.timezone.utc)
                return max(
                    now_epoch_ns,
                    int(moment.timestamp() * 1_000_000_000),
                )
            except (TypeError, ValueError, OverflowError):
                pass
    # A missing or malformed Retry-After must not create a hot retry loop.
    return now_epoch_ns + 60 * 1_000_000_000


class PaidApiLedger:
    """SQLite/WAL ledger providing at-most-once network submission."""

    def __init__(
        self,
        database: Path,
        *,
        budget: BudgetPolicy | None = None,
        minimum_free_bytes: int = 256 * 1024 * 1024,
        emergency_reserve_bytes: int = 0,
        fault_injector: Callable[[str], None] | None = None,
    ):
        self.database = database.expanduser().resolve()
        self.budget = budget or BudgetPolicy()
        self.minimum_free_bytes = int(minimum_free_bytes)
        self.emergency_reserve_bytes = int(emergency_reserve_bytes)
        self._fault_injector = fault_injector
        if self.emergency_reserve_bytes < 0:
            raise ValueError("emergency_reserve_bytes must not be negative")
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.database.parent, 0o700)
        self._initialize()
        self.recover_orphaned_sends()
        self._ensure_emergency_reserve()

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @property
    def emergency_reserve_path(self) -> Path:
        return self.database.parent / ".paid_api_emergency_reserve"

    @property
    def emergency_response_root(self) -> Path:
        return self.database.parent / "emergency_responses"

    @property
    def sender_lock_root(self) -> Path:
        return self.database.parent / ".paid_api_sender_locks"

    def _sender_lock_path(self, logical_call_id: str) -> Path:
        name = hashlib.sha256(str(logical_call_id).encode("utf-8")).hexdigest()
        return self.sender_lock_root / f"{name}.lock"

    @contextlib.contextmanager
    def _sender_lock(self, logical_call_id: str):
        self.sender_lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.sender_lock_root, 0o700)
        path = self._sender_lock_path(logical_call_id)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise DeliveryUnknown(
                    f"call {logical_call_id} already has an active sender"
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def recover_orphaned_sends(self) -> list[str]:
        """Fail closed for ``sent`` rows whose sender process no longer owns a lock."""

        with self._connect() as connection:
            logical_call_ids = [
                str(row["logical_call_id"])
                for row in connection.execute(
                    "SELECT logical_call_id FROM api_calls WHERE state='sent'"
                ).fetchall()
            ]
        recovered: list[str] = []
        for logical_call_id in logical_call_ids:
            try:
                with self._sender_lock(logical_call_id):
                    now = time.time_ns()
                    with self._connect() as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        changed = connection.execute(
                            """
                            UPDATE api_calls
                            SET state='sent_unknown', updated_at_epoch_ns=?
                            WHERE logical_call_id=? AND state='sent'
                            """,
                            (now, logical_call_id),
                        ).rowcount
                        if changed:
                            connection.execute(
                                """
                                UPDATE global_circuit
                                SET state='open',
                                    reason='interrupted_sender_delivery_unknown',
                                    status_code=NULL,
                                    opened_at_epoch_ns=COALESCE(
                                        opened_at_epoch_ns, ?
                                    ),
                                    retry_after_epoch_ns=NULL,
                                    probe_logical_call_id='',
                                    updated_at_epoch_ns=?
                                WHERE singleton=1
                                """,
                                (now, now),
                            )
                            recovered.append(logical_call_id)
                        connection.commit()
            except DeliveryUnknown:
                # The lock is authoritative evidence that a sender is still
                # alive.  It remains responsible for persisting the response.
                continue
        return recovered

    def _ensure_emergency_reserve(self) -> None:
        if self.emergency_reserve_bytes == 0:
            return
        if self.emergency_response_root.is_dir() and any(
            self.emergency_response_root.glob("*.json")
        ):
            return
        with self._connect() as connection:
            circuit = connection.execute(
                "SELECT state, reason FROM global_circuit WHERE singleton=1"
            ).fetchone()
        if (
            circuit is not None
            and circuit["state"] == "open"
            and circuit["reason"] == "response_persistence_failed"
        ):
            return
        path = self.emergency_reserve_path
        if path.is_file() and path.stat().st_size == self.emergency_reserve_bytes:
            return
        disk_preflight(
            self.database.parent,
            minimum_free_bytes=(self.minimum_free_bytes + self.emergency_reserve_bytes),
        )
        descriptor, name = tempfile.mkstemp(
            prefix=".reserve.", dir=self.database.parent
        )
        temporary = Path(name)
        block = b"\0" * (1024 * 1024)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                remaining = self.emergency_reserve_bytes
                while remaining:
                    chunk = block[: min(len(block), remaining)]
                    handle.write(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _preserve_emergency_response(
        self,
        *,
        logical_call_id: str,
        status_code: int,
        headers: Mapping[str, str],
        content: bytes,
        provider_request_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        failure_reason: str = "",
        error_type: str = "",
        error_sha256: str = "",
    ) -> Path:
        self.emergency_reserve_path.unlink(missing_ok=True)
        root = self.emergency_response_root
        body_path = root / f"{logical_call_id}.body"
        meta_path = root / f"{logical_call_id}.json"
        self._atomic_bytes(body_path, content)
        metadata = {
            "schema": SCHEMA,
            "logical_call_id": logical_call_id,
            "status_code": int(status_code),
            "headers": _redact_headers(headers),
            "response_sha256": hashlib.sha256(content).hexdigest(),
            "response_size_bytes": len(content),
            "provider_request_id": provider_request_id,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(total_tokens),
            "body_file": body_path.name,
            "preserved_at_epoch_ns": time.time_ns(),
        }
        if failure_reason:
            metadata["failure_reason"] = str(failure_reason)
        if error_type:
            metadata["error_type"] = str(error_type)
        if error_sha256:
            metadata["error_sha256"] = str(error_sha256)
        self._atomic_bytes(
            meta_path,
            canonical_json_bytes(metadata),
        )
        return meta_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_calls (
                    logical_call_id TEXT PRIMARY KEY,
                    schema_name TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    stage_key TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    knowledge_version TEXT NOT NULL,
                    endpoint TEXT NOT NULL DEFAULT '',
                    request_body BLOB,
                    request_sha256 TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'planned', 'request_durable', 'sent',
                            'response_durable', 'consumed', 'sent_unknown'
                        )
                    ),
                    reserved_tokens INTEGER NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    provider_request_id TEXT NOT NULL DEFAULT '',
                    response_status_code INTEGER,
                    response_headers_json TEXT,
                    response_body BLOB,
                    response_sha256 TEXT NOT NULL DEFAULT '',
                    projection_state TEXT NOT NULL DEFAULT 'not_started',
                    projection_error TEXT NOT NULL DEFAULT '',
                    created_at_epoch_ns INTEGER NOT NULL,
                    updated_at_epoch_ns INTEGER NOT NULL,
                    sent_at_epoch_ns INTEGER,
                    response_at_epoch_ns INTEGER,
                    consumed_at_epoch_ns INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_api_calls_asset
                    ON api_calls(asset_id, state);
                CREATE INDEX IF NOT EXISTS idx_api_calls_stage
                    ON api_calls(asset_id, stage_key, state);

                CREATE TABLE IF NOT EXISTS global_circuit (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    state TEXT NOT NULL CHECK (state IN ('closed', 'open')),
                    reason TEXT NOT NULL,
                    status_code INTEGER,
                    opened_at_epoch_ns INTEGER,
                    retry_after_epoch_ns INTEGER,
                    probe_logical_call_id TEXT NOT NULL DEFAULT '',
                    updated_at_epoch_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_checkpoints (
                    asset_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    knowledge_version TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    output BLOB NOT NULL,
                    created_at_epoch_ns INTEGER NOT NULL,
                    PRIMARY KEY (
                        asset_id, stage, input_sha256, model,
                        prompt_version, knowledge_version
                    )
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(global_circuit)"
                ).fetchall()
            }
            if "retry_after_epoch_ns" not in columns:
                connection.execute(
                    "ALTER TABLE global_circuit ADD COLUMN retry_after_epoch_ns INTEGER"
                )
            if "probe_logical_call_id" not in columns:
                connection.execute(
                    "ALTER TABLE global_circuit "
                    "ADD COLUMN probe_logical_call_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO global_circuit(
                    singleton, state, reason, status_code,
                    opened_at_epoch_ns, retry_after_epoch_ns,
                    probe_logical_call_id, updated_at_epoch_ns
                ) VALUES (1, 'closed', '', NULL, NULL, NULL, '', 0)
                """
            )
        os.chmod(self.database, 0o600)

    def _row_to_envelope(self, row: sqlite3.Row) -> ApiCallEnvelopeV2:
        return ApiCallEnvelopeV2(
            logical_call_id=str(row["logical_call_id"]),
            asset_id=str(row["asset_id"]),
            stage=str(row["stage"]),
            stage_key=str(row["stage_key"]),
            input_sha256=str(row["input_sha256"]),
            model=str(row["model"]),
            prompt_version=str(row["prompt_version"]),
            knowledge_version=str(row["knowledge_version"]),
            endpoint=str(row["endpoint"]),
            request_sha256=str(row["request_sha256"]),
            state=str(row["state"]),
            reserved_tokens=int(row["reserved_tokens"]),
            total_tokens=(
                int(row["total_tokens"]) if row["total_tokens"] is not None else None
            ),
            provider_request_id=str(row["provider_request_id"]),
            response_status_code=(
                int(row["response_status_code"])
                if row["response_status_code"] is not None
                else None
            ),
            projection_state=str(row["projection_state"]),
        )

    def get_call(self, logical_call_id: str) -> ApiCallEnvelopeV2:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_calls WHERE logical_call_id = ?",
                (logical_call_id,),
            ).fetchone()
        if row is None:
            raise KeyError(logical_call_id)
        return self._row_to_envelope(row)

    def _assert_circuit_closed(
        self,
        connection: sqlite3.Connection,
        *,
        logical_call_id: str,
        now_epoch_ns: int,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM global_circuit WHERE singleton = 1"
        ).fetchone()
        if (
            row is not None
            and row["state"] == "closed"
            and row["reason"] == "recovery_probe_ok"
        ):
            call = connection.execute(
                """
                SELECT updated_at_epoch_ns FROM api_calls
                WHERE logical_call_id = ?
                """,
                (logical_call_id,),
            ).fetchone()
            if call is not None and int(call["updated_at_epoch_ns"]) <= int(
                row["updated_at_epoch_ns"]
            ):
                # Calls queued before the successful single-flight probe must
                # observe that recovery boundary once instead of forming a
                # thundering herd as soon as SQLite releases the probe commit.
                connection.execute(
                    """
                    UPDATE api_calls SET updated_at_epoch_ns = ?
                    WHERE logical_call_id = ?
                    """,
                    (now_epoch_ns, logical_call_id),
                )
                connection.commit()
                raise CircuitOpen(
                    "paid API call predates the completed rate-limit probe"
                )
        if row is not None and row["state"] == "open":
            if (
                row["reason"] in RECOVERABLE_CIRCUIT_REASONS
                and int(row["retry_after_epoch_ns"] or 0) <= now_epoch_ns
                and not str(row["probe_logical_call_id"] or "")
            ):
                changed = connection.execute(
                    """
                    UPDATE global_circuit
                    SET probe_logical_call_id = ?, updated_at_epoch_ns = ?
                    WHERE singleton = 1 AND state = 'open'
                      AND reason IN (
                          'http_429',
                          'provider_health_probe_delivery_unknown',
                          'provider_upstream_error_400',
                          'provider_upstream_error_5xx'
                      )
                      AND COALESCE(probe_logical_call_id, '') = ''
                      AND COALESCE(retry_after_epoch_ns, 0) <= ?
                    """,
                    (logical_call_id, now_epoch_ns, now_epoch_ns),
                ).rowcount
                if changed == 1:
                    return
            suffix = (
                f" (HTTP {row['status_code']})"
                if row["status_code"] is not None
                else ""
            )
            raise CircuitOpen(f"paid API circuit is open: {row['reason']}{suffix}")

    def circuit_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM global_circuit WHERE singleton = 1"
            ).fetchone()
        assert row is not None
        return dict(row)

    def open_circuit(self, reason: str, *, status_code: int | None = None) -> None:
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE global_circuit
                SET state = 'open', reason = ?, status_code = ?,
                    opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                    retry_after_epoch_ns = NULL,
                    probe_logical_call_id = '',
                    updated_at_epoch_ns = ?
                WHERE singleton = 1
                """,
                (str(reason), status_code, now, now),
            )
            connection.commit()

    def close_circuit(self, *, operator_reason: str) -> None:
        if not str(operator_reason).strip():
            raise ValueError("closing the paid API circuit requires an operator reason")
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE global_circuit
                SET state = 'closed', reason = ?, status_code = NULL,
                    opened_at_epoch_ns = NULL,
                    retry_after_epoch_ns = NULL,
                    probe_logical_call_id = '',
                    updated_at_epoch_ns = ?
                WHERE singleton = 1
                """,
                (f"operator_close:{operator_reason}", now),
            )
            connection.commit()

    def _assert_budget(
        self,
        connection: sqlite3.Connection,
        *,
        asset_id: str,
        stage_key: str,
        reserved_tokens: int,
    ) -> None:
        row = connection.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(COALESCE(total_tokens, reserved_tokens)), 0) AS tokens
            FROM api_calls
            WHERE asset_id = ?
            """,
            (asset_id,),
        ).fetchone()
        assert row is not None
        if int(row["calls"]) >= self.budget.max_calls_per_asset:
            raise BudgetExceeded(
                f"asset {asset_id} reached {self.budget.max_calls_per_asset} calls"
            )
        if int(row["tokens"]) + reserved_tokens > self.budget.max_tokens_per_asset:
            raise BudgetExceeded(
                f"asset {asset_id} would exceed "
                f"{self.budget.max_tokens_per_asset} tokens"
            )
        total = connection.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(COALESCE(total_tokens, reserved_tokens)), 0)
                       AS tokens
            FROM api_calls
            """
        ).fetchone()
        assert total is not None
        if (
            self.budget.max_total_calls is not None
            and int(total["calls"]) >= self.budget.max_total_calls
        ):
            raise BudgetExceeded(f"ledger reached {self.budget.max_total_calls} calls")
        if (
            self.budget.max_total_tokens is not None
            and int(total["tokens"]) + reserved_tokens > self.budget.max_total_tokens
        ):
            raise BudgetExceeded(
                f"ledger would exceed {self.budget.max_total_tokens} tokens"
            )
        matching_limits = [
            (prefix, int(limit))
            for prefix, limit in self.budget.stage_call_limits.items()
            if stage_key == prefix or stage_key.startswith(prefix.rstrip("/") + "/")
        ]
        stage_limit = (
            max(matching_limits, key=lambda item: len(item[0]))[1]
            if matching_limits
            else None
        )
        if stage_limit is not None:
            stage_calls = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM api_calls
                    WHERE asset_id = ? AND stage_key = ?
                    """,
                    (asset_id, stage_key),
                ).fetchone()[0]
            )
            if stage_calls >= int(stage_limit):
                raise BudgetExceeded(
                    f"asset {asset_id} stage {stage_key} reached {stage_limit} calls"
                )

    def _assert_reserved_budget_still_valid(
        self,
        connection: sqlite3.Connection,
        *,
        asset_id: str,
    ) -> None:
        """Recheck reservations immediately before a network submission.

        A prior response can contain more tokens than its conservative
        reservation.  Calls that were already planned must not blindly POST
        after that durable usage pushes the lifecycle or smoke budget over its
        hard limit.
        """

        asset = connection.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(COALESCE(total_tokens, reserved_tokens)), 0)
                       AS tokens
            FROM api_calls
            WHERE asset_id = ?
            """,
            (asset_id,),
        ).fetchone()
        total = connection.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(COALESCE(total_tokens, reserved_tokens)), 0)
                       AS tokens
            FROM api_calls
            """
        ).fetchone()
        assert asset is not None and total is not None
        if (
            int(asset["calls"]) > self.budget.max_calls_per_asset
            or int(asset["tokens"]) > self.budget.max_tokens_per_asset
        ):
            raise BudgetExceeded(
                f"asset {asset_id} durable usage exceeds its lifecycle budget"
            )
        if (
            self.budget.max_total_calls is not None
            and int(total["calls"]) > self.budget.max_total_calls
        ):
            raise BudgetExceeded("ledger durable call count exceeds global budget")
        if (
            self.budget.max_total_tokens is not None
            and int(total["tokens"]) > self.budget.max_total_tokens
        ):
            raise BudgetExceeded("ledger durable token usage exceeds global budget")

    def plan_call(
        self,
        *,
        asset_id: str,
        stage: str,
        stage_key: str,
        semantic_input: object,
        model: str,
        prompt_version: str,
        knowledge_version: str,
        reserved_tokens: int,
    ) -> ApiCallEnvelopeV2:
        if reserved_tokens < 1:
            raise ValueError("reserved_tokens must be positive")
        input_digest = payload_sha256(semantic_input)
        call_id = deterministic_logical_call_id(
            asset_id=asset_id,
            stage=stage,
            input_sha256=input_digest,
            model=model,
            prompt_version=prompt_version,
            knowledge_version=knowledge_version,
        )
        disk_preflight(self.database.parent, minimum_free_bytes=self.minimum_free_bytes)
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM api_calls WHERE logical_call_id = ?", (call_id,)
            ).fetchone()
            if existing is None:
                self._assert_budget(
                    connection,
                    asset_id=asset_id,
                    stage_key=stage_key,
                    reserved_tokens=reserved_tokens,
                )
                connection.execute(
                    """
                    INSERT INTO api_calls(
                        logical_call_id, schema_name, asset_id, stage, stage_key,
                        input_sha256, model, prompt_version, knowledge_version,
                        state, reserved_tokens, created_at_epoch_ns,
                        updated_at_epoch_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                    """,
                    (
                        call_id,
                        SCHEMA,
                        asset_id,
                        stage,
                        stage_key,
                        input_digest,
                        model,
                        prompt_version,
                        knowledge_version,
                        reserved_tokens,
                        now,
                        now,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM api_calls WHERE logical_call_id = ?", (call_id,)
                ).fetchone()
            connection.commit()
        assert existing is not None
        return self._row_to_envelope(existing)

    def make_request_durable(
        self,
        logical_call_id: str,
        *,
        endpoint: str,
        request_payload: object,
        wire_body: bytes | None = None,
    ) -> ApiCallEnvelopeV2:
        normalized = normalize_chat_completions_endpoint(endpoint)
        body = (
            canonical_json_bytes(request_payload)
            if wire_body is None
            else bytes(wire_body)
        )
        digest = hashlib.sha256(body).hexdigest()
        disk_preflight(
            self.database.parent,
            minimum_free_bytes=self.minimum_free_bytes + len(body),
        )
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM api_calls WHERE logical_call_id = ?",
                (logical_call_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(logical_call_id)
            state = str(row["state"])
            if state == "planned":
                connection.execute(
                    """
                    UPDATE api_calls
                    SET endpoint = ?, request_body = ?, request_sha256 = ?,
                        state = 'request_durable', updated_at_epoch_ns = ?
                    WHERE logical_call_id = ? AND state = 'planned'
                    """,
                    (normalized, body, digest, now, logical_call_id),
                )
            elif (
                str(row["endpoint"]) != normalized
                or str(row["request_sha256"]) != digest
            ):
                connection.rollback()
                raise InvalidTransition(
                    "logical call already has a different durable request"
                )
            row = connection.execute(
                "SELECT * FROM api_calls WHERE logical_call_id = ?",
                (logical_call_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._row_to_envelope(row)

    def prepare_call(
        self,
        *,
        asset_id: str,
        stage: str,
        request_payload: object,
        model: str,
        prompt_version: str,
        knowledge_version: str,
        endpoint: str,
        reserved_tokens: int,
        semantic_input: object | None = None,
        stage_key: str | None = None,
        wire_body: bytes | None = None,
    ) -> ApiCallEnvelopeV2:
        semantic_value = request_payload if semantic_input is None else semantic_input
        input_digest = payload_sha256(semantic_value)
        normalized_endpoint = normalize_chat_completions_endpoint(endpoint)
        durable_body = (
            canonical_json_bytes(request_payload)
            if wire_body is None
            else bytes(wire_body)
        )
        request_digest = hashlib.sha256(durable_body).hexdigest()
        if reserved_tokens < 1:
            raise ValueError("reserved_tokens must be positive")
        disk_preflight(
            self.database.parent,
            minimum_free_bytes=self.minimum_free_bytes + len(durable_body),
        )
        # Knowledge retrieval may become available between attempts even when
        # the actual provider request is byte-for-byte identical.  A changed
        # bookkeeping-only knowledge_version must never create a second POST
        # for the same asset/stage/input/wire request.  Keep the lookup,
        # logical-call creation, and request WAL transition in one IMMEDIATE
        # transaction so concurrent workers cannot both plan that same wire
        # request under different knowledge-version labels.
        call_id = deterministic_logical_call_id(
            asset_id=asset_id,
            stage=stage,
            input_sha256=input_digest,
            model=model,
            prompt_version=prompt_version,
            knowledge_version=knowledge_version,
        )
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            identical = connection.execute(
                """
                SELECT * FROM api_calls
                WHERE asset_id=? AND stage=? AND stage_key=?
                  AND input_sha256=? AND model=? AND prompt_version=?
                  AND endpoint=? AND request_sha256=?
                  AND state!='planned'
                ORDER BY
                  CASE
                    WHEN state IN ('response_durable','consumed') THEN 0
                    WHEN state='sent_unknown' THEN 1
                    WHEN state='sent' THEN 2
                    WHEN state='request_durable' THEN 3
                    ELSE 4
                  END,
                  created_at_epoch_ns
                LIMIT 1
                """,
                (
                    asset_id,
                    stage,
                    stage_key or stage,
                    input_digest,
                    model,
                    prompt_version,
                    normalized_endpoint,
                    request_digest,
                ),
            ).fetchone()
            if identical is None:
                existing = connection.execute(
                    "SELECT * FROM api_calls WHERE logical_call_id = ?",
                    (call_id,),
                ).fetchone()
                if existing is None:
                    self._assert_budget(
                        connection,
                        asset_id=asset_id,
                        stage_key=stage_key or stage,
                        reserved_tokens=reserved_tokens,
                    )
                    connection.execute(
                        """
                        INSERT INTO api_calls(
                            logical_call_id, schema_name, asset_id, stage,
                            stage_key, input_sha256, model, prompt_version,
                            knowledge_version, state, reserved_tokens,
                            created_at_epoch_ns, updated_at_epoch_ns
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                        """,
                        (
                            call_id,
                            SCHEMA,
                            asset_id,
                            stage,
                            stage_key or stage,
                            input_digest,
                            model,
                            prompt_version,
                            knowledge_version,
                            reserved_tokens,
                            now,
                            now,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM api_calls WHERE logical_call_id = ?",
                        (call_id,),
                    ).fetchone()
                assert existing is not None
                state = str(existing["state"])
                if state == "planned":
                    connection.execute(
                        """
                        UPDATE api_calls
                        SET endpoint = ?, request_body = ?,
                            request_sha256 = ?, state = 'request_durable',
                            updated_at_epoch_ns = ?
                        WHERE logical_call_id = ? AND state = 'planned'
                        """,
                        (
                            normalized_endpoint,
                            durable_body,
                            request_digest,
                            now,
                            call_id,
                        ),
                    )
                elif (
                    str(existing["endpoint"]) != normalized_endpoint
                    or str(existing["request_sha256"]) != request_digest
                ):
                    connection.rollback()
                    raise InvalidTransition(
                        "logical call already has a different durable request"
                    )
                identical = connection.execute(
                    "SELECT * FROM api_calls WHERE logical_call_id = ?",
                    (call_id,),
                ).fetchone()
            connection.commit()
        assert identical is not None
        return self._row_to_envelope(identical)

    def _stored_response(self, row: sqlite3.Row, *, replayed: bool) -> StoredResponse:
        if row["response_body"] is None or row["response_status_code"] is None:
            raise InvalidTransition("call has no durable response")
        return StoredResponse(
            logical_call_id=str(row["logical_call_id"]),
            status_code=int(row["response_status_code"]),
            headers=json.loads(str(row["response_headers_json"] or "{}")),
            content=bytes(row["response_body"]),
            provider_request_id=str(row["provider_request_id"]),
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            total_tokens=int(row["total_tokens"] or 0),
            replayed=replayed,
        )

    def replay_response(self, logical_call_id: str) -> StoredResponse:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_calls WHERE logical_call_id = ?",
                (logical_call_id,),
            ).fetchone()
        if row is None:
            raise KeyError(logical_call_id)
        if row["state"] not in TERMINAL_RESPONSE_STATES:
            raise InvalidTransition(f"call response is not replayable: {row['state']}")
        return self._stored_response(row, replayed=True)

    def execute(
        self,
        logical_call_id: str,
        send: Callable[[str, bytes], Any],
        *,
        projector: Callable[[StoredResponse], Any] | None = None,
    ) -> StoredResponse:
        with self._sender_lock(logical_call_id):
            return self._execute_with_sender_lock(
                logical_call_id,
                send,
                projector=projector,
            )

    def _execute_with_sender_lock(
        self,
        logical_call_id: str,
        send: Callable[[str, bytes], Any],
        *,
        projector: Callable[[StoredResponse], Any] | None = None,
    ) -> StoredResponse:
        """Submit once, or replay a response that is already durable."""

        disk_preflight(self.database.parent, minimum_free_bytes=self.minimum_free_bytes)
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM api_calls WHERE logical_call_id = ?",
                (logical_call_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(logical_call_id)
            if row["state"] in TERMINAL_RESPONSE_STATES:
                connection.commit()
                response = self._stored_response(row, replayed=True)
                if projector is not None:
                    self.project_response(logical_call_id, projector)
                return response
            if row["state"] == "sent":
                connection.execute(
                    """
                    UPDATE api_calls
                    SET state='sent_unknown', updated_at_epoch_ns=?
                    WHERE logical_call_id=? AND state='sent'
                    """,
                    (now, logical_call_id),
                )
                connection.execute(
                    """
                    UPDATE global_circuit
                    SET state='open',
                        reason='interrupted_sender_delivery_unknown',
                        status_code=NULL,
                        opened_at_epoch_ns=COALESCE(opened_at_epoch_ns, ?),
                        retry_after_epoch_ns=NULL,
                        probe_logical_call_id='',
                        updated_at_epoch_ns=?
                    WHERE singleton=1
                    """,
                    (now, now),
                )
                connection.commit()
                raise DeliveryUnknown(
                    f"call {logical_call_id} may already have been delivered"
                )
            if row["state"] == "sent_unknown":
                connection.rollback()
                raise DeliveryUnknown(
                    f"call {logical_call_id} may already have been delivered"
                )
            if row["state"] != "request_durable":
                connection.rollback()
                raise InvalidTransition(
                    f"call must be request_durable before send: {row['state']}"
                )
            self._assert_circuit_closed(
                connection,
                logical_call_id=logical_call_id,
                now_epoch_ns=now,
            )
            self._assert_reserved_budget_still_valid(
                connection,
                asset_id=str(row["asset_id"]),
            )
            changed = connection.execute(
                """
                UPDATE api_calls
                SET state = 'sent', sent_at_epoch_ns = ?, updated_at_epoch_ns = ?
                WHERE logical_call_id = ? AND state = 'request_durable'
                """,
                (now, now, logical_call_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise DeliveryUnknown(
                    f"call {logical_call_id} was claimed by another sender"
                )
            endpoint = str(row["endpoint"])
            body = bytes(row["request_body"])
            connection.commit()

        try:
            raw_response = send(endpoint, body)
        except BaseException:
            failed_at = time.time_ns()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE api_calls
                    SET state = 'sent_unknown', updated_at_epoch_ns = ?
                    WHERE logical_call_id = ? AND state = 'sent'
                    """,
                    (failed_at, logical_call_id),
                )
                connection.execute(
                    """
                    UPDATE global_circuit
                    SET state = 'open', reason = 'delivery_unknown',
                        status_code = NULL,
                        opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                        retry_after_epoch_ns = NULL,
                        probe_logical_call_id = ?,
                        updated_at_epoch_ns = ?
                    WHERE singleton = 1
                    """,
                    (failed_at, logical_call_id, failed_at),
                )
                connection.commit()
            raise

        try:
            status_code, headers, content = _response_parts(raw_response)
        except BaseException as exc:
            # A provider has already returned, so even an adapter/attribute
            # failure is an uncertain delivered call.  Preserve independently
            # extractable evidence and stop every other paid caller.
            status_code, headers, content = _best_effort_response_parts(raw_response)
            error_bytes = str(exc).encode("utf-8", errors="replace")
            error_sha256 = hashlib.sha256(error_bytes).hexdigest()
            try:
                self._preserve_emergency_response(
                    logical_call_id=logical_call_id,
                    status_code=status_code,
                    headers=headers,
                    content=content,
                    provider_request_id="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=int(row["reserved_tokens"]),
                    failure_reason="response_processing_failed",
                    error_type=type(exc).__name__,
                    error_sha256=error_sha256,
                )
            except BaseException:
                pass
            try:
                failed_at = time.time_ns()
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE api_calls
                        SET state = 'sent_unknown', updated_at_epoch_ns = ?
                        WHERE logical_call_id = ? AND state = 'sent'
                        """,
                        (failed_at, logical_call_id),
                    )
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'open',
                            reason = 'response_processing_failed',
                            status_code = NULL,
                            opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                            retry_after_epoch_ns = NULL,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (failed_at, failed_at),
                    )
                    connection.commit()
            except BaseException:
                pass
            raise
        try:
            prompt, completion, total, request_id, processing_error = (
                _usage_from_response(
                    content,
                    headers,
                    status_code=status_code,
                )
            )
        except BaseException:
            # The raw body is already safely serializable.  Preserve it below,
            # charge the complete reservation, and stop the global circuit
            # rather than allowing a parser defect to lose evidence or permit
            # another paid submission.
            prompt = 0
            completion = 0
            total = int(row["reserved_tokens"])
            lowered_headers = {
                str(key).lower(): str(value) for key, value in headers.items()
            }
            request_id = (
                lowered_headers.get("x-request-id")
                or lowered_headers.get("request-id")
                or lowered_headers.get("x-shellapi-request-id")
                or ""
            )
            processing_error = "response_usage_processing_failed"
        # Providers are not guaranteed to return usage on errors.  Charging the
        # reservation in that case is the only fail-closed budget behavior.
        if total <= 0 or processing_error:
            total = int(row["reserved_tokens"])
        response_digest = hashlib.sha256(content).hexdigest()
        completed_at = time.time_ns()
        safe_headers = _redact_headers(headers)
        try:
            self._fault("before_response_commit")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    """
                    UPDATE api_calls
                    SET state = 'response_durable',
                        response_status_code = ?,
                        response_headers_json = ?,
                        response_body = ?,
                        response_sha256 = ?,
                        provider_request_id = ?,
                        prompt_tokens = ?,
                        completion_tokens = ?,
                        total_tokens = ?,
                        response_at_epoch_ns = ?,
                        updated_at_epoch_ns = ?
                    WHERE logical_call_id = ? AND state = 'sent'
                    """,
                    (
                        status_code,
                        json.dumps(safe_headers, sort_keys=True),
                        content,
                        response_digest,
                        request_id,
                        prompt,
                        completion,
                        total,
                        completed_at,
                        completed_at,
                        logical_call_id,
                    ),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    raise InvalidTransition(
                        "response could not be committed exactly once"
                    )
                circuit = connection.execute(
                    "SELECT * FROM global_circuit WHERE singleton = 1"
                ).fetchone()
                assert circuit is not None
                is_recovery_probe = (
                    str(circuit["probe_logical_call_id"] or "") == logical_call_id
                )
                is_upstream_error = _is_provider_upstream_error(status_code, content)
                if status_code in {401, 402, 403}:
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'open', reason = ?,
                            status_code = ?,
                            opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                            retry_after_epoch_ns = NULL,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (
                            f"http_{status_code}",
                            status_code,
                            completed_at,
                            completed_at,
                        ),
                    )
                elif status_code == 429:
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'open', reason = 'http_429',
                            status_code = 429,
                            opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                            retry_after_epoch_ns = ?,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (
                            completed_at,
                            _retry_after_epoch_ns(headers, now_epoch_ns=completed_at),
                            completed_at,
                        ),
                    )
                elif is_recovery_probe and status_code < 400:
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'closed', reason = 'recovery_probe_ok',
                            status_code = NULL, opened_at_epoch_ns = NULL,
                            retry_after_epoch_ns = NULL,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (completed_at,),
                    )
                elif is_recovery_probe:
                    retryable_5xx = 500 <= status_code <= 599
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'open', reason = ?,
                            status_code = ?, retry_after_epoch_ns = ?,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (
                            (
                                "provider_upstream_error_400"
                                if is_upstream_error
                                else (
                                    "provider_upstream_error_5xx"
                                    if retryable_5xx
                                    else f"recovery_probe_http_{status_code}"
                                )
                            ),
                            status_code,
                            (
                                completed_at + RECOVERY_PROBE_RETRY_NS
                                if is_upstream_error or retryable_5xx
                                else None
                            ),
                            completed_at,
                        ),
                    )
                elif is_upstream_error:
                    recent_upstream_errors = 0
                    recent_rows = connection.execute(
                        """
                        SELECT response_body FROM api_calls
                        WHERE response_status_code = 400
                          AND response_at_epoch_ns >= ?
                        """,
                        (completed_at - UPSTREAM_ERROR_WINDOW_NS,),
                    ).fetchall()
                    for recent in recent_rows:
                        body_value = recent["response_body"]
                        if body_value is not None and _is_provider_upstream_error(
                            400, body_value
                        ):
                            recent_upstream_errors += 1
                    if recent_upstream_errors >= UPSTREAM_ERROR_THRESHOLD:
                        connection.execute(
                            """
                            UPDATE global_circuit
                            SET state = 'open',
                                reason = 'provider_upstream_error_400',
                                status_code = 400,
                                opened_at_epoch_ns =
                                    COALESCE(opened_at_epoch_ns, ?),
                                retry_after_epoch_ns = ?,
                                probe_logical_call_id = '',
                                updated_at_epoch_ns = ?
                            WHERE singleton = 1
                            """,
                            (
                                completed_at,
                                completed_at + UPSTREAM_ERROR_RETRY_NS,
                                completed_at,
                            ),
                        )
                if processing_error and status_code not in {401, 402, 403, 429}:
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'open',
                            reason = 'response_processing_failed',
                            status_code = ?,
                            opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                            retry_after_epoch_ns = NULL,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (status_code, completed_at, completed_at),
                    )
                aggregate = connection.execute(
                    """
                    SELECT COALESCE(
                        SUM(COALESCE(total_tokens, reserved_tokens)), 0
                    ) AS tokens
                    FROM api_calls
                    """
                ).fetchone()
                if (
                    self.budget.max_total_tokens is not None
                    and aggregate is not None
                    and int(aggregate["tokens"]) > self.budget.max_total_tokens
                ):
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'open',
                            reason = 'global_token_budget_exhausted',
                            status_code = NULL,
                            opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                            retry_after_epoch_ns = NULL,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (completed_at, completed_at),
                    )
                row = connection.execute(
                    "SELECT * FROM api_calls WHERE logical_call_id = ?",
                    (logical_call_id,),
                ).fetchone()
                connection.commit()
        except BaseException:
            # HTTP already returned, so another POST is never safe.  Best
            # effort persistence of the uncertainty also stops unrelated paid
            # calls; if the disk itself is unavailable the existing `sent`
            # state still prevents a replay of this logical call.
            try:
                self._preserve_emergency_response(
                    logical_call_id=logical_call_id,
                    status_code=status_code,
                    headers=safe_headers,
                    content=content,
                    provider_request_id=request_id,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=total,
                )
            except BaseException:
                pass
            try:
                failed_at = time.time_ns()
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE api_calls
                        SET state = 'sent_unknown', updated_at_epoch_ns = ?
                        WHERE logical_call_id = ? AND state = 'sent'
                        """,
                        (failed_at, logical_call_id),
                    )
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state = 'open',
                            reason = 'response_persistence_failed',
                            status_code = NULL,
                            opened_at_epoch_ns = COALESCE(opened_at_epoch_ns, ?),
                            retry_after_epoch_ns = NULL,
                            probe_logical_call_id = '',
                            updated_at_epoch_ns = ?
                        WHERE singleton = 1
                        """,
                        (failed_at, failed_at),
                    )
                    connection.commit()
            except BaseException:
                pass
            raise
        assert row is not None
        response = self._stored_response(row, replayed=False)
        if projector is not None:
            self.project_response(logical_call_id, projector)
        return response

    def project_response(
        self,
        logical_call_id: str,
        projector: Callable[[StoredResponse], Any],
    ) -> bool:
        """Build a trace/SFT projection without changing delivery eligibility."""

        response = self.replay_response(logical_call_id)
        now = time.time_ns()
        try:
            projector(response)
        except Exception as exc:
            # The projector may wrap HTTP/tool errors containing credentials.
            # Persist a stable diagnostic fingerprint rather than the raw text.
            error_text = str(exc).encode("utf-8", errors="replace")
            summary = (
                f"{type(exc).__name__}:sha256={hashlib.sha256(error_text).hexdigest()}"
            )
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE api_calls
                    SET projection_state = 'pending_repair',
                        projection_error = ?, updated_at_epoch_ns = ?
                    WHERE logical_call_id = ?
                    """,
                    (summary, now, logical_call_id),
                )
            return False
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE api_calls
                SET projection_state = 'complete',
                    projection_error = '', updated_at_epoch_ns = ?
                WHERE logical_call_id = ?
                """,
                (now, logical_call_id),
            )
        return True

    def consume(self, logical_call_id: str) -> StoredResponse:
        now = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM api_calls WHERE logical_call_id = ?",
                (logical_call_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(logical_call_id)
            if row["state"] == "response_durable":
                connection.execute(
                    """
                    UPDATE api_calls
                    SET state = 'consumed', consumed_at_epoch_ns = ?,
                        updated_at_epoch_ns = ?
                    WHERE logical_call_id = ? AND state = 'response_durable'
                    """,
                    (now, now, logical_call_id),
                )
                row = connection.execute(
                    "SELECT * FROM api_calls WHERE logical_call_id = ?",
                    (logical_call_id,),
                ).fetchone()
            elif row["state"] != "consumed":
                connection.rollback()
                raise InvalidTransition(f"call cannot be consumed from {row['state']}")
            connection.commit()
        assert row is not None
        return self._stored_response(row, replayed=True)

    def save_checkpoint(
        self,
        *,
        asset_id: str,
        stage: str,
        semantic_input: object,
        model: str,
        prompt_version: str,
        knowledge_version: str,
        output: bytes | object,
    ) -> StageCheckpointV1:
        input_digest = payload_sha256(semantic_input)
        payload = (
            bytes(output)
            if isinstance(output, (bytes, bytearray))
            else canonical_json_bytes(output)
        )
        output_digest = hashlib.sha256(payload).hexdigest()
        now = time.time_ns()
        disk_preflight(
            self.database.parent,
            minimum_free_bytes=self.minimum_free_bytes + len(payload),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO stage_checkpoints(
                    asset_id, stage, input_sha256, model,
                    prompt_version, knowledge_version, schema_name,
                    output_sha256, output, created_at_epoch_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    stage,
                    input_digest,
                    model,
                    prompt_version,
                    knowledge_version,
                    CHECKPOINT_SCHEMA,
                    output_digest,
                    payload,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM stage_checkpoints
                WHERE asset_id = ? AND stage = ? AND input_sha256 = ?
                  AND model = ? AND prompt_version = ? AND knowledge_version = ?
                """,
                (
                    asset_id,
                    stage,
                    input_digest,
                    model,
                    prompt_version,
                    knowledge_version,
                ),
            ).fetchone()
            connection.commit()
        assert row is not None
        if str(row["output_sha256"]) != output_digest:
            raise InvalidTransition(
                "checkpoint identity already has a different immutable output"
            )
        return self._row_to_checkpoint(row)

    def load_checkpoint(
        self,
        *,
        asset_id: str,
        stage: str,
        semantic_input: object,
        model: str,
        prompt_version: str,
        knowledge_version: str,
    ) -> StageCheckpointV1 | None:
        input_digest = payload_sha256(semantic_input)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM stage_checkpoints
                WHERE asset_id = ? AND stage = ? AND input_sha256 = ?
                  AND model = ? AND prompt_version = ? AND knowledge_version = ?
                """,
                (
                    asset_id,
                    stage,
                    input_digest,
                    model,
                    prompt_version,
                    knowledge_version,
                ),
            ).fetchone()
        return self._row_to_checkpoint(row) if row is not None else None

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> StageCheckpointV1:
        output = bytes(row["output"])
        if hashlib.sha256(output).hexdigest() != str(row["output_sha256"]):
            raise PaidApiError("stage checkpoint output hash is invalid")
        return StageCheckpointV1(
            asset_id=str(row["asset_id"]),
            stage=str(row["stage"]),
            input_sha256=str(row["input_sha256"]),
            model=str(row["model"]),
            prompt_version=str(row["prompt_version"]),
            knowledge_version=str(row["knowledge_version"]),
            output_sha256=str(row["output_sha256"]),
            output=output,
            created_at_epoch_ns=int(row["created_at_epoch_ns"]),
        )
