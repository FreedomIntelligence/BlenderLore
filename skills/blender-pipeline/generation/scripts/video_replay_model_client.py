"""Single durable entry point for paid Type1/Type2 model requests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from agent_api_trace import project_durable_api_call
from project_paths import CONFIG_ROOT, PROJECT_ROOT
from video_replay_paid_api import (
    BudgetPolicy,
    CircuitOpen,
    PaidApiLedger,
    StoredResponse,
    canonical_json_bytes,
    decode_chat_response_payload,
    disk_preflight,
    normalize_chat_completions_endpoint,
)


DEFAULT_LEDGER = CONFIG_ROOT / "paid_api/ledger.sqlite3"
DEFAULT_MINIMUM_FREE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_EMERGENCY_RESERVE_BYTES = 64 * 1024 * 1024
APPROVED_ENDPOINT_ENV = "VIDEO_REPLAY_APPROVED_PAID_API_ENDPOINT"
DEFAULT_PROVIDER_USAGE_ENDPOINT = os.environ.get(
    "VIDEO_REPLAY_PROVIDER_USAGE_ENDPOINT", ""
).strip()
PAID_PROVIDER_ENDPOINT = os.environ.get(APPROVED_ENDPOINT_ENV, "").strip()
MAX_SECRET_BYTES = 16 * 1024
PROVIDER_USAGE_RECONCILE_ATTEMPTS = 4
PROVIDER_USAGE_RECONCILE_INTERVAL_SECONDS = 5.0
# The smoke/formal broker's default one-call authorization is intentionally
# above every reconciled Type1/Type2 call observed during the incident repair.
# It is a maximum debit authorization, not an average-cost prediction.
DEFAULT_PROVIDER_CREDIT_RESERVATION_PER_CALL = 60_000
NONRETRYABLE_PROVIDER_HTTP_STATUSES = frozenset(
    {400, 404, 405, 406, 410, 411, 413, 414, 415, 422}
)


class PaidApiSecretError(RuntimeError):
    """The paid-provider credential does not satisfy the local safety contract."""


def validate_paid_api_secret_file(
    path: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """Require a repository-external, owner-only regular credential file.

    This validation intentionally does not read the credential.  The descriptor
    is reopened and revalidated by :func:`read_paid_api_secret` at the exact
    point where a brokered request needs it.
    """

    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise PaidApiSecretError("paid API secret path must be absolute")
    try:
        if candidate.is_symlink():
            raise PaidApiSecretError("paid API secret must not be a symlink")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root.expanduser().resolve())
    except FileNotFoundError as exc:
        raise PaidApiSecretError("paid API secret file is unavailable") from exc
    except ValueError:
        pass
    else:
        raise PaidApiSecretError("paid API secret must be outside the repository")
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise PaidApiSecretError("paid API secret metadata is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PaidApiSecretError(
            "paid API secret must be an owner-owned regular file with mode 0600"
        )
    if metadata.st_size < 1 or metadata.st_size > MAX_SECRET_BYTES:
        raise PaidApiSecretError("paid API secret file size is invalid")
    return resolved


def read_paid_api_secret(
    path: Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> str:
    """Read a validated credential without following a last-moment symlink."""

    resolved = validate_paid_api_secret_file(path, project_root=project_root)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise PaidApiSecretError("paid API secret could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size < 1
            or metadata.st_size > MAX_SECRET_BYTES
        ):
            raise PaidApiSecretError("paid API secret changed during secure open")
        payload = os.read(descriptor, MAX_SECRET_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_SECRET_BYTES:
        raise PaidApiSecretError("paid API secret file size is invalid")
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeError as exc:
        raise PaidApiSecretError("paid API secret is not UTF-8 text") from exc
    if not value or any(character.isspace() for character in value):
        raise PaidApiSecretError("paid API secret content is invalid")
    return value


def model_provider() -> str:
    provider = os.environ.get("BLENDER_PIPELINE_PROVIDER", "api").strip().lower()
    if provider not in {"api", "codex-cli"}:
        raise ValueError("BLENDER_PIPELINE_PROVIDER must be api or codex-cli")
    return provider


def read_model_credential(path: Path) -> str:
    """The authenticated Codex CLI does not require or read an API credential."""

    return "" if model_provider() == "codex-cli" else read_paid_api_secret(path)


def _provider_prompt_version(value: str) -> str:
    return f"codex-cli:{value}" if model_provider() == "codex-cli" else value


def _provider_ledger_path(video_dir: Path) -> Path:
    if (
        model_provider() == "codex-cli"
        and not os.environ.get("VIDEO_REPLAY_PAID_API_LEDGER", "").strip()
    ):
        return video_dir.resolve() / ".control/model_calls/ledger.sqlite3"
    return ledger_path()


@dataclass(frozen=True)
class DurableModelResponse:
    logical_call_id: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    provider_request_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    replayed: bool

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return decode_chat_response_payload(self.content)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def task_identity(video_dir: Path) -> str:
    configured = os.environ.get("VIDEO_REPLAY_TASK_IDENTITY", "").strip()
    if configured:
        return configured
    info = _read_json(video_dir / "source.info.json")
    stable = {
        "bvid": str(info.get("bvid") or info.get("id") or ""),
        "source_video_url": str(
            info.get("webpage_url") or info.get("original_url") or ""
        ),
        "source_video_sha256": str(info.get("source_video_sha256") or ""),
        "directory_name": video_dir.name,
    }
    return "standalone:" + hashlib.sha256(canonical_json_bytes(stable)).hexdigest()


def knowledge_version(video_dir: Path) -> str:
    configured = os.environ.get("VIDEO_REPLAY_KNOWLEDGE_VERSION", "").strip()
    if configured:
        return configured
    pack = _read_json(video_dir / "knowledge_retrieval_pack.json")
    for field in (
        "knowledge_generation",
        "active_manifest_sha256",
        "active_manifest",
        "classifier_version",
    ):
        value = str(pack.get(field) or "").strip()
        if value:
            return value
    return "knowledge-unversioned"


def ledger_path() -> Path:
    configured = os.environ.get("VIDEO_REPLAY_PAID_API_LEDGER", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_LEDGER


def durable_nonretryable_provider_error(
    *,
    asset_id: str | None = None,
    database: Path | None = None,
) -> dict[str, Any] | None:
    """Return a sanitized durable 4xx that cannot succeed by replaying.

    The response body is deliberately excluded: it may contain provider or
    request details and is already preserved in the immutable API ledger.
    """

    identity = str(
        asset_id
        if asset_id is not None
        else os.environ.get("VIDEO_REPLAY_TASK_IDENTITY", "")
    ).strip()
    path = (database or ledger_path()).expanduser()
    if not identity or not path.is_file():
        return None
    provider_statuses = tuple(sorted(NONRETRYABLE_PROVIDER_HTTP_STATUSES))
    if len(provider_statuses) != 10:
        raise RuntimeError("non-retryable provider status query contract drifted")
    try:
        with sqlite3.connect(
            f"file:{path.resolve(strict=False)}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT logical_call_id, stage, stage_key,
                       response_status_code, created_at_epoch_ns
                FROM api_calls
                WHERE asset_id=?
                  AND state IN ('response_durable', 'consumed')
                  AND response_status_code IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ORDER BY created_at_epoch_ns DESC
                LIMIT 1
                """,
                (identity, *provider_statuses),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "logical_call_id": str(row["logical_call_id"]),
        "stage": str(row["stage"]),
        "stage_key": str(row["stage_key"]),
        "status_code": int(row["response_status_code"]),
        "created_at_epoch_ns": int(row["created_at_epoch_ns"]),
    }


def _minimum_free_bytes() -> int:
    raw = os.environ.get("VIDEO_REPLAY_PAID_API_MIN_FREE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MINIMUM_FREE_BYTES
    value = int(raw)
    if value < 0:
        raise ValueError("VIDEO_REPLAY_PAID_API_MIN_FREE_BYTES must not be negative")
    return value


def _optional_positive_environment(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _emergency_reserve_bytes() -> int:
    raw = os.environ.get("VIDEO_REPLAY_PAID_API_EMERGENCY_RESERVE_BYTES", "").strip()
    if not raw:
        return DEFAULT_EMERGENCY_RESERVE_BYTES
    value = int(raw)
    if value < 0:
        raise ValueError(
            "VIDEO_REPLAY_PAID_API_EMERGENCY_RESERVE_BYTES must not be negative"
        )
    return value


def _provider_credit_reservation() -> tuple[int, int] | None:
    """Return the local cap and one-call maximum debit authorization.

    The fixed reservation is deliberately independent of observed average
    spend: averages are not a safe bound for the next request.  Operators may
    lower or raise the declared maximum with an explicit environment value;
    SQLite still prevents concurrent workers from authorizing the same
    remaining credits.
    """

    limit = _optional_positive_environment("VIDEO_REPLAY_PROVIDER_CREDIT_BUDGET")
    if limit is None:
        return None
    configured = _optional_positive_environment(
        "VIDEO_REPLAY_PROVIDER_CREDIT_RESERVE_PER_CALL"
    )
    reservation = (
        configured
        if configured is not None
        else min(limit, DEFAULT_PROVIDER_CREDIT_RESERVATION_PER_CALL)
    )
    if reservation > limit:
        raise ValueError(
            "VIDEO_REPLAY_PROVIDER_CREDIT_RESERVE_PER_CALL must not exceed "
            "VIDEO_REPLAY_PROVIDER_CREDIT_BUDGET"
        )
    return limit, reservation


def _budget_policy() -> BudgetPolicy:
    return BudgetPolicy(
        max_calls_per_asset=(
            _optional_positive_environment("VIDEO_REPLAY_PAID_API_MAX_CALLS_PER_ASSET")
            or 32
        ),
        max_tokens_per_asset=(
            _optional_positive_environment("VIDEO_REPLAY_PAID_API_MAX_TOKENS_PER_ASSET")
            or 500_000
        ),
        max_total_calls=_optional_positive_environment(
            "VIDEO_REPLAY_PAID_API_GLOBAL_MAX_CALLS"
        ),
        max_total_tokens=_optional_positive_environment(
            "VIDEO_REPLAY_PAID_API_GLOBAL_MAX_TOKENS"
        ),
        stage_call_limits={
            "tutorial": 2,
            "codegen": 5,
            "visual_review": 3,
        },
    )


def paid_api_preflight(
    *,
    ledger: PaidApiLedger | None = None,
    secret_path: Path | None = None,
) -> PaidApiLedger:
    if secret_path is not None:
        validate_paid_api_secret_file(secret_path)
    durable = ledger or PaidApiLedger(
        ledger_path(),
        budget=_budget_policy(),
        minimum_free_bytes=_minimum_free_bytes(),
        emergency_reserve_bytes=_emergency_reserve_bytes(),
    )
    disk_preflight(
        durable.database.parent,
        minimum_free_bytes=durable.minimum_free_bytes,
    )
    circuit = durable.circuit_status()
    if circuit.get("state") == "open":
        raise CircuitOpen(
            f"paid API circuit is open: {circuit.get('reason') or 'unknown'}"
        )
    return durable


def _reserved_tokens(payload: Mapping[str, Any]) -> int:
    output_budget = max(
        1,
        int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 1),
    )
    # Text retains the conservative UTF-8 byte upper bound.  Vision inputs are
    # billed from decoded image tiles, not from the base64 wire expansion; raw
    # bytes caused valid image prompts to reserve hundreds of thousands of
    # fictitious tokens and fail before POST.  16k tokens per image is above
    # the pipeline's <=1600px evidence-image envelope while remaining bounded
    # and additive for concurrent planned calls.
    media_count = 0

    def scrub_media(value: Any) -> Any:
        nonlocal media_count
        if isinstance(value, Mapping):
            return {str(key): scrub_media(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub_media(item) for item in value]
        if (
            isinstance(value, str)
            and value.startswith("data:image/")
            and ";base64," in value[:128]
        ):
            media_count += 1
            return "<content-addressed-image>"
        return value

    text_reserve = len(canonical_json_bytes(scrub_media(payload)))
    prompt_reserve = max(2_000, text_reserve) + media_count * 16_384
    return output_budget + prompt_reserve


def _as_model_response(response: StoredResponse) -> DurableModelResponse:
    return DurableModelResponse(
        logical_call_id=response.logical_call_id,
        status_code=response.status_code,
        headers=dict(response.headers),
        content=response.content,
        provider_request_id=response.provider_request_id,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        total_tokens=response.total_tokens,
        replayed=response.replayed,
    )


def call_chat_completions(
    *,
    video_dir: Path,
    stage: str,
    stage_key: str,
    prompt_version: str,
    endpoint: str,
    api_key: str,
    model: str,
    payload: Mapping[str, Any],
    timeout: tuple[float, float],
    semantic_input: object | None = None,
    ledger: PaidApiLedger | None = None,
) -> DurableModelResponse:
    """Persist, submit at most once, and replay by deterministic call identity."""

    if model_provider() == "codex-cli":
        return _call_codex_completions(
            video_dir=video_dir,
            stage=stage,
            stage_key=stage_key,
            prompt_version=prompt_version,
            model=model,
            payload=payload,
            timeout=timeout,
            semantic_input=semantic_input,
            ledger=ledger,
        )

    if not api_key.strip():
        raise ValueError("paid API key is empty")
    normalized_endpoint = normalize_chat_completions_endpoint(endpoint)
    approved_endpoint = normalize_chat_completions_endpoint(
        os.environ.get(APPROVED_ENDPOINT_ENV, PAID_PROVIDER_ENDPOINT)
    )
    if normalized_endpoint != approved_endpoint:
        raise ValueError("Type1/Type2 paid API endpoint is not the approved provider")
    provider_usage_supported = bool(DEFAULT_PROVIDER_USAGE_ENDPOINT)
    policy = _budget_policy()
    durable = ledger or PaidApiLedger(
        ledger_path(),
        budget=policy,
        minimum_free_bytes=_minimum_free_bytes(),
        emergency_reserve_bytes=_emergency_reserve_bytes(),
    )
    identity = task_identity(video_dir)
    request_payload = dict(payload)
    # The provider explicitly recommends SSE for long GPT requests and its
    # current non-stream compatibility path returns an upstream
    # bad_response_body error for gpt-5.6-sol.  The durable ledger stores the
    # exact raw SSE body; DurableModelResponse normalizes it only for business
    # consumers.
    request_payload["stream"] = True
    request_payload["stream_options"] = {"include_usage": True}
    recovery_generation = os.environ.get(
        "VIDEO_REPLAY_PROVIDER_RECOVERY_GENERATION", ""
    ).strip()
    effective_prompt_version = prompt_version
    effective_semantic_input = (
        request_payload if semantic_input is None else semantic_input
    )
    if recovery_generation:
        effective_prompt_version = (
            f"{prompt_version}:provider-recovery:{recovery_generation}"
        )
        effective_semantic_input = {
            "provider_recovery_generation": recovery_generation,
            "semantic_input": effective_semantic_input,
        }
    envelope = durable.prepare_call(
        asset_id=identity,
        stage=stage,
        stage_key=stage_key,
        request_payload=request_payload,
        semantic_input=effective_semantic_input,
        model=model,
        prompt_version=effective_prompt_version,
        knowledge_version=knowledge_version(video_dir),
        endpoint=endpoint,
        reserved_tokens=_reserved_tokens(request_payload),
    )
    wire_body = canonical_json_bytes(request_payload)
    session = requests.Session()
    session.trust_env = False
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def send(normalized_endpoint: str, exact_wire_body: bytes) -> Any:
        return session.post(
            normalized_endpoint,
            headers=headers,
            data=exact_wire_body,
            timeout=timeout,
        )

    def project(stored: StoredResponse) -> None:
        project_durable_api_call(
            video_dir,
            logical_call_id=stored.logical_call_id,
            stage=stage,
            attempt=1,
            endpoint=envelope.endpoint,
            request_payload=request_payload,
            wire_request_body=wire_body,
            response_body=stored.content,
            status_code=stored.status_code,
            response_headers=stored.headers,
            provider_request_id=stored.provider_request_id,
        )

    if provider_usage_supported and envelope.state not in {
        "response_durable",
        "consumed",
    }:
        _ensure_provider_usage_baseline(
            durable=durable,
            api_key=api_key,
        )
        _reserve_provider_credit_budget(
            durable=durable,
            logical_call_id=envelope.logical_call_id,
            api_key=api_key,
        )
    stored = durable.execute(envelope.logical_call_id, send, projector=project)
    durable.consume(envelope.logical_call_id)
    if stored.status_code < 400 and provider_usage_supported:
        _enforce_provider_credit_budget(durable=durable, api_key=api_key)
    elif stored.status_code >= 400 and provider_usage_supported:
        _settle_nonbillable_error_reservation(
            durable=durable,
            logical_call_id=envelope.logical_call_id,
        )
    return _as_model_response(stored)


def _call_codex_completions(
    *,
    video_dir: Path,
    stage: str,
    stage_key: str,
    prompt_version: str,
    model: str,
    payload: Mapping[str, Any],
    timeout: tuple[float, float],
    semantic_input: object | None,
    ledger: PaidApiLedger | None,
) -> DurableModelResponse:
    from codex_cli_chat_bridge import send_codex_chat, validate_codex_request

    validate_codex_request(payload, model)
    durable = ledger or PaidApiLedger(
        _provider_ledger_path(video_dir),
        budget=_budget_policy(),
        minimum_free_bytes=_minimum_free_bytes(),
        emergency_reserve_bytes=_emergency_reserve_bytes(),
    )
    # This non-routable URI is a ledger namespace only; the sender launches a
    # local CLI and never submits an HTTP request to it.
    endpoint = "https://codex-cli.invalid/v1/chat/completions"
    request_payload = dict(payload)
    envelope = durable.prepare_call(
        asset_id=task_identity(video_dir),
        stage=stage,
        stage_key=stage_key,
        request_payload=request_payload,
        semantic_input=request_payload if semantic_input is None else semantic_input,
        model=model,
        prompt_version=_provider_prompt_version(prompt_version),
        knowledge_version=knowledge_version(video_dir),
        endpoint=endpoint,
        reserved_tokens=_reserved_tokens(request_payload),
    )

    def send(_endpoint: str, exact_body: bytes) -> Any:
        return send_codex_chat(
            payload=json.loads(exact_body),
            model=model,
            video_dir=video_dir,
            logical_call_id=envelope.logical_call_id,
            timeout_seconds=float(
                os.environ.get("BLENDER_PIPELINE_CODEX_TIMEOUT", "900")
            ),
        )

    def project(stored: StoredResponse) -> None:
        project_durable_api_call(
            video_dir,
            logical_call_id=stored.logical_call_id,
            stage=stage,
            attempt=1,
            endpoint=endpoint,
            request_payload=request_payload,
            wire_request_body=canonical_json_bytes(request_payload),
            response_body=stored.content,
            status_code=stored.status_code,
            response_headers=stored.headers,
            provider_request_id=stored.provider_request_id,
        )

    stored = durable.execute(envelope.logical_call_id, send, projector=project)
    durable.consume(envelope.logical_call_id)
    return _as_model_response(stored)


def _ensure_provider_usage_baseline(*, durable: PaidApiLedger, api_key: str) -> None:
    """Freeze reused-key history before this ledger can submit its first POST."""

    from video_replay_provider_usage import ensure_provider_usage_baseline

    try:
        ensure_provider_usage_baseline(
            ledger_path=durable.database,
            fetch_payload=lambda: fetch_provider_usage(
                api_key=api_key,
                start=0,
                limit=300,
            ),
        )
    except Exception as exc:
        durable.open_circuit("provider_usage_baseline_failed")
        raise CircuitOpen(
            "provider usage baseline could not be durably captured before "
            "model submission; paid API circuit opened"
        ) from exc


def _reserve_provider_credit_budget(
    *, durable: PaidApiLedger, logical_call_id: str, api_key: str
) -> None:
    """Persist the provider-credit authorization before any possible POST."""

    authorization = _provider_credit_reservation()
    if authorization is None:
        return
    limit, reservation = authorization
    from video_replay_provider_usage import (
        ProviderCreditBudgetExceeded,
        ProviderUsageReconciliationPending,
        reserve_provider_credit_budget,
    )

    try:
        reserve_provider_credit_budget(
            ledger_path=durable.database,
            logical_call_id=logical_call_id,
            credit_limit=limit,
            reserved_credits=reservation,
        )
    except ProviderUsageReconciliationPending:
        # Another process can finish a paid response between this process
        # preparing its request and reserving credit. Reconcile that durable
        # response, then retry this still-unsent reservation exactly once.
        # This is normal concurrent progress, not a reason to poison the
        # global circuit or submit the current request without authorization.
        _enforce_provider_credit_budget(durable=durable, api_key=api_key)
        try:
            reserve_provider_credit_budget(
                ledger_path=durable.database,
                logical_call_id=logical_call_id,
                credit_limit=limit,
                reserved_credits=reservation,
            )
        except ProviderUsageReconciliationPending as exc:
            raise CircuitOpen(
                "provider billing reconciliation is still pending; "
                "model POST was not attempted"
            ) from exc
    except ProviderCreditBudgetExceeded as exc:
        # Active reservations can temporarily consume the remaining cap.
        # Reject this POST without disturbing already-authorized senders.
        raise CircuitOpen(
            "provider credit cap has insufficient remaining authorization; "
            "model POST was not attempted"
        ) from exc
    except Exception as exc:
        durable.open_circuit("provider_credit_preflight_failed")
        raise CircuitOpen(
            "provider credit preflight could not be durably verified; "
            "paid API circuit opened before model submission"
        ) from exc


def _enforce_provider_credit_budget(*, durable: PaidApiLedger, api_key: str) -> None:
    """Reconcile every successful POST; the optional limit only adds a cap."""

    limit = _optional_positive_environment("VIDEO_REPLAY_PROVIDER_CREDIT_BUDGET")
    # Imported lazily to avoid a module cycle: the reconciliation CLI reuses
    # this module's credential-safe provider log fetcher.
    from video_replay_provider_usage import (
        local_billing_summary,
        reconcile_provider_logs,
    )

    existing = local_billing_summary(ledger_path=durable.database)
    summary: dict[str, Any] | None = (
        existing
        if existing["local_successful_calls"] > 0 and existing["all_matched"]
        else None
    )
    circuit = durable.circuit_status()
    if (
        summary is None
        and circuit.get("state") == "open"
        and circuit.get("reason") == "response_processing_failed"
    ):
        raise CircuitOpen(
            "provider response lacks durable billing identity or usage; "
            "paid API circuit opened"
        )
    last_error: Exception | None = None
    for attempt in range(
        0 if summary is not None else PROVIDER_USAGE_RECONCILE_ATTEMPTS
    ):
        try:
            payload = fetch_provider_usage(api_key=api_key, start=0, limit=300)
            reconciliation = reconcile_provider_logs(
                ledger_path=durable.database,
                payload=payload,
            )
            candidate = local_billing_summary(ledger_path=durable.database)
            if reconciliation["all_matched"] and candidate["all_matched"]:
                summary = candidate
                break
        except Exception as exc:
            last_error = exc
        if attempt + 1 < PROVIDER_USAGE_RECONCILE_ATTEMPTS:
            time.sleep(PROVIDER_USAGE_RECONCILE_INTERVAL_SECONDS)
    if summary is None:
        durable.open_circuit("provider_usage_reconciliation_timeout")
        raise CircuitOpen(
            "provider billing did not reconcile within the bounded polling "
            "window; paid API circuit opened"
        ) from last_error
    if limit is not None and int(summary["credits_consumed"]) >= limit:
        durable.open_circuit("provider_credit_budget_exhausted")


def _settle_nonbillable_error_reservation(
    *, durable: PaidApiLedger, logical_call_id: str
) -> None:
    """Return unused authorization for a durable usage-free HTTP error."""

    if _provider_credit_reservation() is None:
        return
    from video_replay_provider_usage import (
        settle_nonbillable_error_reservation,
    )

    try:
        settle_nonbillable_error_reservation(
            ledger_path=durable.database,
            logical_call_id=logical_call_id,
        )
    except Exception as exc:
        durable.open_circuit("provider_credit_error_settlement_failed")
        raise CircuitOpen(
            "provider error response could not release its durable credit "
            "authorization; paid API circuit opened"
        ) from exc


def fetch_provider_usage(
    *,
    api_key: str,
    start: int = 0,
    limit: int = 300,
    endpoint: str = DEFAULT_PROVIDER_USAGE_ENDPOINT,
    timeout: tuple[float, float] = (20, 60),
) -> dict[str, Any]:
    """Fetch the provider's non-billable usage log without persisting a key."""

    if not api_key.strip():
        raise ValueError("provider usage API key is empty")
    if not endpoint.strip():
        raise ValueError(
            "provider usage endpoint is not configured; set "
            "VIDEO_REPLAY_PROVIDER_USAGE_ENDPOINT"
        )
    if start < 0 or not 1 <= limit <= 300:
        raise ValueError("provider usage cursor or limit is invalid")
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"start": int(start), "limit": int(limit)},
        timeout=timeout,
    )
    if response.status_code >= 400:
        digest = hashlib.sha256(response.content).hexdigest()
        raise RuntimeError(
            f"provider usage HTTP {response.status_code}: body_sha256={digest}"
        )
    payload = response.json()
    if not isinstance(payload, Mapping) or int(payload.get("error") or 0) != 0:
        raise RuntimeError("provider usage response reports an error")
    return dict(payload)


def save_stage_checkpoint(
    *,
    video_dir: Path,
    stage: str,
    prompt_version: str,
    model: str,
    semantic_input: object,
    output: bytes | object,
    ledger: PaidApiLedger | None = None,
) -> None:
    durable = ledger or PaidApiLedger(
        _provider_ledger_path(video_dir),
        minimum_free_bytes=_minimum_free_bytes(),
        emergency_reserve_bytes=_emergency_reserve_bytes(),
    )
    durable.save_checkpoint(
        asset_id=task_identity(video_dir),
        stage=stage,
        semantic_input=semantic_input,
        model=model,
        prompt_version=_provider_prompt_version(prompt_version),
        knowledge_version=knowledge_version(video_dir),
        output=output,
    )


def load_stage_checkpoint(
    *,
    video_dir: Path,
    stage: str,
    prompt_version: str,
    model: str,
    semantic_input: object,
    ledger: PaidApiLedger | None = None,
) -> bytes | None:
    durable = ledger or PaidApiLedger(
        _provider_ledger_path(video_dir),
        minimum_free_bytes=_minimum_free_bytes(),
        emergency_reserve_bytes=_emergency_reserve_bytes(),
    )
    checkpoint = durable.load_checkpoint(
        asset_id=task_identity(video_dir),
        stage=stage,
        semantic_input=semantic_input,
        model=model,
        prompt_version=_provider_prompt_version(prompt_version),
        knowledge_version=knowledge_version(video_dir),
    )
    return checkpoint.output if checkpoint is not None else None
