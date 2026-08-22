#!/usr/bin/env python3
"""Reconcile durable local calls with configured-provider usage rows."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from video_replay_model_client import fetch_provider_usage, read_paid_api_secret


SCHEMA = "video2blender.provider-usage-reconciliation.v1"
BASELINE_SCHEMA = "video2blender.provider-usage-baseline.v1"
CREDIT_RESERVATION_SCHEMA = "video2blender.provider-credit-reservation.v1"
CREDIT_ADJUSTMENT_SCHEMA = "video2blender.provider-credit-adjustment.v1"
UNIQUE_USAGE_MATCH_METHOD = "unique_model_tokens_response_second_v1"
UNIQUE_USAGE_RESPONSE_WINDOW_MATCH_METHOD = "unique_model_tokens_response_window_v1"
UNIQUE_SENT_UNKNOWN_MATCH_METHOD = "unique_sent_unknown_model_delivery_window_v1"
OPERATOR_SENT_UNKNOWN_MATCH_METHOD = "operator_sent_unknown_prompt_evidence_v1"
USAGE_RESPONSE_TIME_TOLERANCE_SECONDS = 2
USAGE_RESPONSE_FALLBACK_WINDOW_SECONDS = 3 * 60
SENT_UNKNOWN_BILLING_WINDOW_SECONDS = 30 * 60
HEALTH_PROBE_RECOVERY_DELAY_NS = 5 * 60 * 1_000_000_000


class ProviderUsageError(RuntimeError):
    pass


class ProviderCreditBudgetExceeded(ProviderUsageError):
    """The durable local provider-credit authorization cannot admit a POST."""


class ProviderUsageReconciliationPending(ProviderUsageError):
    """A successful concurrent POST is not yet matched to provider billing."""


def _release_reconciled_health_probe_delivery_unknown(
    connection: sqlite3.Connection,
) -> bool:
    """Quarantine a billed probe timeout without unblocking business unknowns."""

    circuit = connection.execute(
        "SELECT * FROM global_circuit WHERE singleton=1"
    ).fetchone()
    if (
        circuit is None
        or str(circuit["state"]) != "open"
        or str(circuit["reason"]) != "delivery_unknown"
    ):
        return False
    logical_call_id = str(circuit["probe_logical_call_id"] or "")
    if logical_call_id:
        candidate = connection.execute(
            """
            SELECT c.logical_call_id,c.updated_at_epoch_ns
            FROM api_calls c
            JOIN provider_usage_logs u
              ON u.logical_call_id=c.logical_call_id
            WHERE c.logical_call_id=?
              AND c.state='sent_unknown'
              AND c.asset_id LIKE 'provider_health_probe:%'
              AND u.reconciliation_state='matched'
            """,
            (logical_call_id,),
        ).fetchone()
    else:
        # Compatibility for incidents created before the circuit recorded the
        # probe call ID. The circuit and call timestamps must identify one
        # exact health probe; ordinary asset sent-unknown rows never qualify.
        candidate = connection.execute(
            """
            SELECT c.logical_call_id,c.updated_at_epoch_ns
            FROM api_calls c
            JOIN provider_usage_logs u
              ON u.logical_call_id=c.logical_call_id
            WHERE c.state='sent_unknown'
              AND c.asset_id LIKE 'provider_health_probe:%'
              AND u.reconciliation_state='matched'
              AND ABS(c.updated_at_epoch_ns-?) <= 5000000000
            ORDER BY c.updated_at_epoch_ns DESC
            LIMIT 1
            """,
            (int(circuit["updated_at_epoch_ns"]),),
        ).fetchone()
    if candidate is None:
        return False
    now = time.time_ns()
    retry_after = max(
        now,
        int(candidate["updated_at_epoch_ns"]) + HEALTH_PROBE_RECOVERY_DELAY_NS,
    )
    changed = connection.execute(
        """
        UPDATE global_circuit
        SET reason='provider_health_probe_delivery_unknown',
            status_code=NULL,
            retry_after_epoch_ns=?,
            probe_logical_call_id='',
            updated_at_epoch_ns=?
        WHERE singleton=1 AND state='open' AND reason='delivery_unknown'
        """,
        (retry_after, now),
    ).rowcount
    return changed == 1


def _nonnegative_int(value: object, field: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ProviderUsageError(f"provider {field} is invalid") from exc
    if parsed < 0:
        raise ProviderUsageError(f"provider {field} is negative")
    return parsed


def _signed_int(value: object, field: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ProviderUsageError(f"provider {field} is invalid") from exc


def _usage_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if int(payload.get("error") or 0) != 0:
        raise ProviderUsageError("provider usage payload reports an error")
    data = payload.get("data")
    logs = data.get("logs") if isinstance(data, Mapping) else None
    if not isinstance(logs, list):
        raise ProviderUsageError("provider usage rows are missing")
    if any(not isinstance(row, Mapping) for row in logs):
        raise ProviderUsageError("provider usage row is invalid")
    return list(logs)


def _baseline_normalized(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only immutable identity and billing fields, never IP/note data."""

    provider_log_id = str(raw.get("id") or "").strip()
    if not provider_log_id:
        raise ProviderUsageError("provider usage identity is missing")
    return {
        "schema": BASELINE_SCHEMA,
        "provider_log_id": provider_log_id,
        # Account adjustments can legitimately have no request ID.
        "provider_request_id": str(raw.get("reqid") or "").strip(),
        "prompt_tokens": _nonnegative_int(raw.get("prompt_tokens"), "prompt_tokens"),
        "completion_tokens": _nonnegative_int(
            raw.get("completion_tokens"), "completion_tokens"
        ),
        # A pre-existing top-up/account-adjustment row can have negative fen.
        # Only immutable baseline rows are allowed to use this signed parser.
        "provider_credit_delta": _signed_int(raw.get("fen"), "fen"),
        "credits_remaining": _nonnegative_int(raw.get("yufen"), "yufen"),
        "model": str(raw.get("model") or ""),
        "provider_created_at_epoch": _nonnegative_int(raw.get("ctime"), "ctime"),
    }


def _baseline_tuple(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(value["provider_log_id"]),
        str(value["provider_request_id"]),
        int(value["prompt_tokens"]),
        int(value["completion_tokens"]),
        int(value["provider_credit_delta"]),
        int(value["credits_remaining"]),
        str(value["model"]),
        int(value["provider_created_at_epoch"]),
    )


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.expanduser().resolve())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_usage_baseline_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            schema_name TEXT NOT NULL,
            capture_row_count INTEGER NOT NULL,
            capture_sha256 TEXT NOT NULL,
            captured_at_epoch_ns INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_usage_baseline_rows (
            provider_log_id TEXT PRIMARY KEY,
            schema_name TEXT NOT NULL,
            provider_request_id TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            provider_credit_delta INTEGER NOT NULL,
            credits_remaining INTEGER NOT NULL,
            model TEXT NOT NULL,
            provider_created_at_epoch INTEGER NOT NULL,
            normalized_json TEXT NOT NULL,
            captured_at_epoch_ns INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            provider_usage_baseline_one_per_request
        ON provider_usage_baseline_rows(provider_request_id)
        WHERE provider_request_id != ''
        """
    )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS provider_usage_baseline_no_update
        BEFORE UPDATE ON provider_usage_baseline_rows
        BEGIN
            SELECT RAISE(ABORT, 'provider usage baseline is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_usage_baseline_no_delete
        BEFORE DELETE ON provider_usage_baseline_rows
        BEGIN
            SELECT RAISE(ABORT, 'provider usage baseline is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_usage_baseline_state_no_update
        BEFORE UPDATE ON provider_usage_baseline_state
        BEGIN
            SELECT RAISE(ABORT, 'provider usage baseline state is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_usage_baseline_state_no_delete
        BEFORE DELETE ON provider_usage_baseline_state
        BEGIN
            SELECT RAISE(ABORT, 'provider usage baseline state is immutable');
        END;
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_usage_logs (
            provider_log_id TEXT PRIMARY KEY,
            schema_name TEXT NOT NULL,
            provider_request_id TEXT NOT NULL,
            logical_call_id TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            credits_consumed INTEGER NOT NULL,
            credits_remaining INTEGER NOT NULL,
            model TEXT NOT NULL,
            provider_created_at_epoch INTEGER NOT NULL,
            reconciliation_state TEXT NOT NULL CHECK(
                reconciliation_state IN (
                    'matched', 'token_mismatch', 'unmatched'
                )
            ),
            transport_request_id TEXT NOT NULL DEFAULT '',
            match_method TEXT NOT NULL DEFAULT '',
            normalized_json TEXT NOT NULL,
            reconciled_at_epoch_ns INTEGER NOT NULL
        )
        """
    )
    usage_columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(provider_usage_logs)"
        ).fetchall()
    }
    if "transport_request_id" not in usage_columns:
        connection.execute(
            """
            ALTER TABLE provider_usage_logs
            ADD COLUMN transport_request_id TEXT NOT NULL DEFAULT ''
            """
        )
    if "match_method" not in usage_columns:
        connection.execute(
            """
            ALTER TABLE provider_usage_logs
            ADD COLUMN match_method TEXT NOT NULL DEFAULT ''
            """
        )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS provider_usage_by_request
           ON provider_usage_logs(provider_request_id)"""
    )
    duplicate = connection.execute(
        """
        SELECT provider_request_id
        FROM provider_usage_logs
        GROUP BY provider_request_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate is not None:
        raise ProviderUsageError("multiple provider usage rows map to one request ID")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS provider_usage_one_per_request
           ON provider_usage_logs(provider_request_id)"""
    )
    duplicate_owner = connection.execute(
        """
        SELECT logical_call_id
        FROM provider_usage_logs
        WHERE logical_call_id != ''
        GROUP BY logical_call_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate_owner is not None:
        raise ProviderUsageError("multiple provider usage rows map to one logical call")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            provider_usage_one_per_logical_call
        ON provider_usage_logs(logical_call_id)
        WHERE logical_call_id != ''
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_credit_budget_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            schema_name TEXT NOT NULL,
            credit_limit INTEGER NOT NULL CHECK(credit_limit > 0),
            created_at_epoch_ns INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_credit_adjustments (
            provider_log_id TEXT PRIMARY KEY,
            schema_name TEXT NOT NULL,
            provider_request_id TEXT NOT NULL,
            credit_delta INTEGER NOT NULL CHECK(credit_delta < 0),
            credits_remaining INTEGER NOT NULL,
            model TEXT NOT NULL,
            provider_created_at_epoch INTEGER NOT NULL,
            normalized_json TEXT NOT NULL,
            reconciled_at_epoch_ns INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            provider_credit_adjustment_one_per_request
        ON provider_credit_adjustments(provider_request_id)
        WHERE provider_request_id != ''
        """
    )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS provider_credit_adjustments_no_update
        BEFORE UPDATE ON provider_credit_adjustments
        BEGIN
            SELECT RAISE(ABORT, 'provider credit adjustment is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_credit_adjustments_no_delete
        BEFORE DELETE ON provider_credit_adjustments
        BEGIN
            SELECT RAISE(ABORT, 'provider credit adjustment is immutable');
        END;
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_credit_reservations (
            logical_call_id TEXT PRIMARY KEY,
            schema_name TEXT NOT NULL,
            credit_limit INTEGER NOT NULL CHECK(credit_limit > 0),
            reserved_credits INTEGER NOT NULL CHECK(reserved_credits > 0),
            state TEXT NOT NULL CHECK(state IN ('active', 'settled')),
            actual_credits INTEGER,
            provider_log_id TEXT NOT NULL DEFAULT '',
            created_at_epoch_ns INTEGER NOT NULL,
            updated_at_epoch_ns INTEGER NOT NULL,
            settled_at_epoch_ns INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS provider_credit_reservations_by_state
        ON provider_credit_reservations(state)
        """
    )


def reserve_provider_credit_budget(
    *,
    ledger_path: Path,
    logical_call_id: str,
    credit_limit: int,
    reserved_credits: int,
) -> dict[str, Any]:
    """Atomically authorize one possible POST against a durable local cap.

    Reconciled provider debits and every still-active reservation share the
    same SQLite transaction.  This prevents two worker processes from both
    observing the same remaining allowance.  A reservation is intentionally
    retained across process crashes and uncertain delivery; only matched
    provider billing settles it.
    """

    call_id = str(logical_call_id).strip()
    limit = int(credit_limit)
    requested = int(reserved_credits)
    if not call_id:
        raise ValueError("logical_call_id must not be empty")
    if limit < 1 or requested < 1:
        raise ValueError("provider credit limit and reservation must be positive")

    now = time.time_ns()
    resolved = ledger_path.expanduser().resolve()
    with _connect(resolved) as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            budget_state = connection.execute(
                """
                SELECT credit_limit
                FROM provider_credit_budget_state
                WHERE singleton=1
                """
            ).fetchone()
            if budget_state is None:
                connection.execute(
                    """
                    INSERT INTO provider_credit_budget_state(
                        singleton, schema_name, credit_limit,
                        created_at_epoch_ns
                    ) VALUES(1, ?, ?, ?)
                    """,
                    (
                        CREDIT_RESERVATION_SCHEMA,
                        limit,
                        now,
                    ),
                )
            elif int(budget_state["credit_limit"]) != limit:
                connection.execute(
                    """
                    UPDATE global_circuit
                    SET state='open',
                        reason='provider_credit_budget_config_changed',
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
                raise ProviderCreditBudgetExceeded(
                    "provider credit limit changed for an existing ledger"
                )
            if requested > limit:
                connection.execute(
                    """
                    UPDATE global_circuit
                    SET state='open',
                        reason='provider_credit_budget_exhausted',
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
                raise ProviderCreditBudgetExceeded(
                    "one provider call reservation exceeds the local credit cap"
                )
            call = connection.execute(
                """
                SELECT state, response_status_code
                FROM api_calls
                WHERE logical_call_id=?
                """,
                (call_id,),
            ).fetchone()
            if call is None:
                raise ProviderUsageError(
                    "provider credit reservation references an unknown call"
                )
            if str(call["state"]) not in {"planned", "request_durable"}:
                raise ProviderUsageError(
                    "provider credit reservation is only valid before submission"
                )

            existing = connection.execute(
                """
                SELECT * FROM provider_credit_reservations
                WHERE logical_call_id=?
                """,
                (call_id,),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing["credit_limit"]) != limit
                    or int(existing["reserved_credits"]) != requested
                ):
                    raise ProviderUsageError(
                        "provider credit reservation parameters changed"
                    )
                if str(existing["state"]) != "active":
                    raise ProviderUsageError(
                        "a settled provider credit reservation cannot be reused"
                    )
                consumed_row = connection.execute(
                    """
                    SELECT COALESCE(SUM(credits_consumed), 0) AS credits
                    FROM provider_usage_logs
                    WHERE reconciliation_state='matched'
                    """
                ).fetchone()
                active_row = connection.execute(
                    """
                    SELECT COALESCE(SUM(reserved_credits), 0) AS credits
                    FROM provider_credit_reservations
                    WHERE state='active'
                    """
                ).fetchone()
                connection.commit()
                return {
                    "schema": CREDIT_RESERVATION_SCHEMA,
                    "logical_call_id": call_id,
                    "credit_limit": limit,
                    "reserved_credits": requested,
                    "credits_consumed": int(consumed_row["credits"]),
                    "active_reserved_credits": int(active_row["credits"]),
                    "created": False,
                }

            local = connection.execute(
                """
                SELECT
                    COUNT(*) AS successful,
                    SUM(
                        CASE
                            WHEN u.reconciliation_state='matched' THEN 1
                            ELSE 0
                        END
                    ) AS matched,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN u.reconciliation_state='matched'
                                THEN u.credits_consumed
                                ELSE 0
                            END
                        ),
                        0
                    ) AS credits
                FROM api_calls c
                LEFT JOIN provider_usage_logs u
                  ON u.logical_call_id=c.logical_call_id
                WHERE c.state IN ('response_durable', 'consumed')
                  AND c.response_status_code < 400
                """
            ).fetchone()
            unmatched = connection.execute(
                """
                SELECT COUNT(*) AS rows
                FROM provider_usage_logs
                WHERE reconciliation_state!='matched'
                """
            ).fetchone()
            billed = connection.execute(
                """
                SELECT COALESCE(SUM(credits_consumed), 0) AS credits
                FROM provider_usage_logs
                WHERE reconciliation_state='matched'
                """
            ).fetchone()
            assert local is not None and unmatched is not None and billed is not None
            successful = int(local["successful"])
            matched = int(local["matched"] or 0)
            if successful != matched or int(unmatched["rows"]) != 0:
                raise ProviderUsageReconciliationPending(
                    "provider billing must be fully reconciled before another POST"
                )

            # Matched sent-unknown calls have no replayable local response,
            # but their provider debit is real and must still reduce the
            # authorization cap.
            consumed = int(billed["credits"])
            active_row = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_credits), 0) AS credits
                FROM provider_credit_reservations
                WHERE state='active'
                """
            ).fetchone()
            assert active_row is not None
            active = int(active_row["credits"])
            if consumed + active + requested > limit:
                # An in-flight reservation can later settle below its maximum
                # and release allowance.  Reject this contender without
                # stopping the already-authorized sender.  With no active
                # sender, the remaining cap is permanently too small for the
                # declared one-call maximum and the circuit can be opened.
                if active == 0:
                    connection.execute(
                        """
                        UPDATE global_circuit
                        SET state='open',
                            reason='provider_credit_budget_exhausted',
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
                raise ProviderCreditBudgetExceeded(
                    "provider credit reservation would exceed the local cap"
                )

            connection.execute(
                """
                INSERT INTO provider_credit_reservations(
                    logical_call_id, schema_name, credit_limit,
                    reserved_credits, state, actual_credits,
                    provider_log_id, created_at_epoch_ns,
                    updated_at_epoch_ns, settled_at_epoch_ns
                ) VALUES(?, ?, ?, ?, 'active', NULL, '', ?, ?, NULL)
                """,
                (
                    call_id,
                    CREDIT_RESERVATION_SCHEMA,
                    limit,
                    requested,
                    now,
                    now,
                ),
            )
            connection.commit()
        except ProviderCreditBudgetExceeded:
            raise
        except Exception:
            connection.rollback()
            raise
    return {
        "schema": CREDIT_RESERVATION_SCHEMA,
        "logical_call_id": call_id,
        "credit_limit": limit,
        "reserved_credits": requested,
        "credits_consumed": consumed,
        "active_reserved_credits": active + requested,
        "created": True,
    }


def settle_nonbillable_error_reservation(
    *,
    ledger_path: Path,
    logical_call_id: str,
) -> dict[str, Any]:
    """Release authorization after a durable, usage-free HTTP error."""

    call_id = str(logical_call_id).strip()
    if not call_id:
        raise ValueError("logical_call_id must not be empty")
    now = time.time_ns()
    with _connect(ledger_path.expanduser().resolve()) as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            call = connection.execute(
                """
                SELECT state, response_status_code, provider_request_id,
                       prompt_tokens, completion_tokens
                FROM api_calls
                WHERE logical_call_id=?
                """,
                (call_id,),
            ).fetchone()
            reservation = connection.execute(
                """
                SELECT state, reserved_credits, actual_credits,
                       provider_log_id
                FROM provider_credit_reservations
                WHERE logical_call_id=?
                """,
                (call_id,),
            ).fetchone()
            if call is None or reservation is None:
                raise ProviderUsageError(
                    "nonbillable settlement references a missing call or reservation"
                )
            status_code = int(call["response_status_code"] or 0)
            if (
                str(call["state"]) not in {"response_durable", "consumed"}
                or status_code < 400
                or str(call["provider_request_id"] or "")
                or int(call["prompt_tokens"] or 0) != 0
                or int(call["completion_tokens"] or 0) != 0
            ):
                raise ProviderUsageError(
                    "only a durable usage-free HTTP error can release authorization"
                )
            state = str(reservation["state"])
            if (
                state == "settled"
                and int(reservation["actual_credits"] or 0) == 0
                and str(reservation["provider_log_id"]) == f"http_error:{status_code}"
            ):
                connection.commit()
                return {
                    "schema": CREDIT_RESERVATION_SCHEMA,
                    "logical_call_id": call_id,
                    "state": "settled_nonbillable",
                    "actual_credits": 0,
                    "created": False,
                }
            if state != "active":
                raise ProviderUsageError("provider credit reservation is not active")
            connection.execute(
                """
                UPDATE provider_credit_reservations
                SET state='settled', actual_credits=0,
                    provider_log_id=?, updated_at_epoch_ns=?,
                    settled_at_epoch_ns=?
                WHERE logical_call_id=? AND state='active'
                """,
                (f"http_error:{status_code}", now, now, call_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "schema": CREDIT_RESERVATION_SCHEMA,
        "logical_call_id": call_id,
        "state": "settled_nonbillable",
        "actual_credits": 0,
        "created": True,
    }


def cancel_unsent_provider_credit_reservation(
    *,
    ledger_path: Path,
    logical_call_id: str,
) -> dict[str, Any]:
    """Release a never-sent reservation after its asset became terminal."""

    call_id = str(logical_call_id).strip()
    if not call_id:
        raise ValueError("logical_call_id must not be empty")
    now = time.time_ns()
    with _connect(ledger_path.expanduser().resolve()) as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            call = connection.execute(
                """
                SELECT state, sent_at_epoch_ns, response_status_code
                FROM api_calls
                WHERE logical_call_id=?
                """,
                (call_id,),
            ).fetchone()
            reservation = connection.execute(
                """
                SELECT state, actual_credits, provider_log_id
                FROM provider_credit_reservations
                WHERE logical_call_id=?
                """,
                (call_id,),
            ).fetchone()
            if call is None or reservation is None:
                raise ProviderUsageError(
                    "unsent cancellation references a missing call or reservation"
                )
            if (
                str(call["state"]) not in {"planned", "request_durable"}
                or call["sent_at_epoch_ns"] is not None
                or call["response_status_code"] is not None
            ):
                raise ProviderUsageError(
                    "only a never-sent logical call can release authorization"
                )
            state = str(reservation["state"])
            if (
                state == "settled"
                and int(reservation["actual_credits"] or 0) == 0
                and str(reservation["provider_log_id"]) == "cancelled_unsent"
            ):
                connection.commit()
                return {
                    "schema": CREDIT_RESERVATION_SCHEMA,
                    "logical_call_id": call_id,
                    "state": "cancelled_unsent",
                    "created": False,
                }
            if state != "active":
                raise ProviderUsageError("provider credit reservation is not active")
            connection.execute(
                """
                UPDATE provider_credit_reservations
                SET state='settled', actual_credits=0,
                    provider_log_id='cancelled_unsent', updated_at_epoch_ns=?,
                    settled_at_epoch_ns=?
                WHERE logical_call_id=? AND state='active'
                """,
                (now, now, call_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "schema": CREDIT_RESERVATION_SCHEMA,
        "logical_call_id": call_id,
        "state": "cancelled_unsent",
        "created": True,
    }


def _local_call_candidates(
    connection: sqlite3.Connection,
    *,
    billing_request_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    provider_created_at_epoch: int,
    provider_log_id: str,
) -> tuple[list[sqlite3.Row], str]:
    """Resolve provider billing IDs without pretending they are transport IDs.

    The provider returns an HTTP ``X-Request-Id`` and a distinct billing
    ``reqid``.
    Direct identity remains preferred.  The fallback is deliberately strict:
    exact model/token counts, a response timestamp within two seconds, exactly
    one unclaimed successful local call, and no prior billing owner.
    """

    # Once an ambiguous sent-unknown debit has been explicitly attributed by
    # an operator, the immutable provider-usage row is the authority on later
    # reconciliation passes. Re-running the broad delivery-window search
    # would rediscover the original ambiguity forever and strand every future
    # paid call even though the incident is already durably quarantined.
    pinned = connection.execute(
        """
        SELECT c.logical_call_id, c.provider_request_id, c.prompt_tokens,
               c.completion_tokens, c.model, c.response_at_epoch_ns,
               c.sent_at_epoch_ns
        FROM provider_usage_logs u
        JOIN api_calls c ON c.logical_call_id=u.logical_call_id
        WHERE u.provider_log_id=?
          AND u.reconciliation_state='matched'
          AND u.match_method=?
        """,
        (provider_log_id, OPERATOR_SENT_UNKNOWN_MATCH_METHOD),
    ).fetchall()
    if pinned:
        return list(pinned), OPERATOR_SENT_UNKNOWN_MATCH_METHOD

    direct = connection.execute(
        """
        SELECT logical_call_id, provider_request_id, prompt_tokens,
               completion_tokens, model, response_at_epoch_ns
        FROM api_calls
        WHERE provider_request_id=?
        """,
        (billing_request_id,),
    ).fetchall()
    if direct:
        return list(direct), "transport_request_id"

    possible = connection.execute(
        """
        SELECT logical_call_id, provider_request_id, prompt_tokens,
               completion_tokens, model, response_at_epoch_ns
        FROM api_calls
        WHERE state IN ('response_durable', 'consumed')
          AND response_status_code < 400
          AND prompt_tokens=?
          AND completion_tokens=?
          AND model=?
          AND response_at_epoch_ns IS NOT NULL
        """,
        (prompt_tokens, completion_tokens, model),
    ).fetchall()
    candidates: list[sqlite3.Row] = []
    for call in possible:
        response_second = int(call["response_at_epoch_ns"]) // 1_000_000_000
        if (
            abs(response_second - provider_created_at_epoch)
            > USAGE_RESPONSE_TIME_TOLERANCE_SECONDS
        ):
            continue
        owner = connection.execute(
            """
            SELECT provider_log_id
            FROM provider_usage_logs
            WHERE logical_call_id=?
              AND provider_log_id != ?
            """,
            (call["logical_call_id"], provider_log_id),
        ).fetchone()
        if owner is None:
            candidates.append(call)
    if candidates:
        return candidates, UNIQUE_USAGE_MATCH_METHOD

    # Some provider rows are timestamped while a long-running completion is
    # still being generated instead of when the HTTP response reaches us.
    # Permit that provider-specific skew only when the final prompt and
    # completion token counts are both known, exact, and identify exactly one
    # unclaimed successful response.  This check must run before the
    # sent-unknown fallback: a durable response with exact output tokens is
    # stronger evidence than a request that never returned.
    window_candidates: list[sqlite3.Row] = []
    for call in possible:
        response_second = int(call["response_at_epoch_ns"]) // 1_000_000_000
        elapsed = response_second - provider_created_at_epoch
        if not 0 <= elapsed <= USAGE_RESPONSE_FALLBACK_WINDOW_SECONDS:
            continue
        owner = connection.execute(
            """
            SELECT provider_log_id
            FROM provider_usage_logs
            WHERE logical_call_id=?
              AND provider_log_id != ?
            """,
            (call["logical_call_id"], provider_log_id),
        ).fetchone()
        if owner is None:
            window_candidates.append(call)
    if len(window_candidates) == 1:
        return (
            window_candidates,
            UNIQUE_USAGE_RESPONSE_WINDOW_MATCH_METHOD,
        )
    if len(window_candidates) > 1:
        return (
            window_candidates,
            UNIQUE_USAGE_RESPONSE_WINDOW_MATCH_METHOD,
        )

    # A sender can die after the provider accepted the request but before the
    # response became durable.  Such a call must remain non-replayable, yet its
    # provider debit still needs a durable owner so budget accounting cannot
    # forget the spend.  Match only one unclaimed sent-unknown call with the
    # exact model and a bounded one-way delivery window; ambiguity fails
    # closed in the caller.
    uncertain = connection.execute(
        """
        SELECT logical_call_id, provider_request_id, prompt_tokens,
               completion_tokens, model, response_at_epoch_ns,
               sent_at_epoch_ns
        FROM api_calls
        WHERE state='sent_unknown'
          AND model=?
          AND sent_at_epoch_ns IS NOT NULL
        """,
        (model,),
    ).fetchall()
    uncertain_candidates: list[sqlite3.Row] = []
    for call in uncertain:
        sent_second = int(call["sent_at_epoch_ns"]) // 1_000_000_000
        elapsed = provider_created_at_epoch - sent_second
        if not 0 <= elapsed <= SENT_UNKNOWN_BILLING_WINDOW_SECONDS:
            continue
        owner = connection.execute(
            """
            SELECT provider_log_id
            FROM provider_usage_logs
            WHERE logical_call_id=?
              AND provider_log_id != ?
            """,
            (call["logical_call_id"], provider_log_id),
        ).fetchone()
        if owner is None:
            uncertain_candidates.append(call)
    return uncertain_candidates, UNIQUE_SENT_UNKNOWN_MATCH_METHOD


def record_operator_sent_unknown_match(
    *,
    ledger_path: Path,
    raw_provider_row: Mapping[str, Any],
    logical_call_id: str,
    operator_reason: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Durably attribute one otherwise ambiguous billed sent-unknown call.

    This never changes the call out of ``sent_unknown`` and therefore never
    makes it replayable. It only gives an already-observed provider debit one
    auditable owner so later budget reconciliation can proceed.
    """

    reason = str(operator_reason).strip()
    if not reason:
        raise ProviderUsageError("operator sent-unknown match needs a reason")
    provider_log_id = str(raw_provider_row.get("id") or "").strip()
    billing_request_id = str(raw_provider_row.get("reqid") or "").strip()
    if not provider_log_id or not billing_request_id:
        raise ProviderUsageError("operator match provider identity is missing")
    prompt_tokens = _nonnegative_int(
        raw_provider_row.get("prompt_tokens"), "prompt_tokens"
    )
    completion_tokens = _nonnegative_int(
        raw_provider_row.get("completion_tokens"), "completion_tokens"
    )
    consumed = _nonnegative_int(_signed_int(raw_provider_row.get("fen"), "fen"), "fen")
    balance = _nonnegative_int(raw_provider_row.get("yufen"), "yufen")
    created_at = _nonnegative_int(raw_provider_row.get("ctime"), "ctime")
    model = str(raw_provider_row.get("model") or "")
    now = time.time_ns()
    with _connect(ledger_path) as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            if (
                connection.execute(
                    "SELECT 1 FROM provider_usage_logs WHERE provider_log_id=?",
                    (provider_log_id,),
                ).fetchone()
                is not None
            ):
                raise ProviderUsageError("operator match provider row already exists")
            call = connection.execute(
                """
                SELECT * FROM api_calls
                WHERE logical_call_id=? AND state='sent_unknown'
                """,
                (logical_call_id,),
            ).fetchone()
            if call is None or str(call["model"]) != model:
                raise ProviderUsageError(
                    "operator match target is not the exact sent-unknown call"
                )
            candidates, method = _local_call_candidates(
                connection,
                billing_request_id=billing_request_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model=model,
                provider_created_at_epoch=created_at,
                provider_log_id=provider_log_id,
            )
            candidate_ids = {
                str(candidate["logical_call_id"]) for candidate in candidates
            }
            if (
                method != UNIQUE_SENT_UNKNOWN_MATCH_METHOD
                or len(candidate_ids) < 2
                or logical_call_id not in candidate_ids
            ):
                raise ProviderUsageError(
                    "operator match is not an ambiguous sent-unknown incident"
                )
            normalized = {
                "schema": SCHEMA,
                "provider_log_id": provider_log_id,
                "provider_request_id": billing_request_id,
                "provider_billing_request_id": billing_request_id,
                "transport_request_id": "",
                "logical_call_id": logical_call_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "credits_consumed": consumed,
                "credits_remaining": balance,
                "model": model,
                "provider_created_at_epoch": created_at,
                "reconciliation_state": "matched",
                "match_method": OPERATOR_SENT_UNKNOWN_MATCH_METHOD,
                "operator_reason": reason,
                "operator_evidence": dict(evidence),
                "ambiguous_candidate_ids": sorted(candidate_ids),
            }
            connection.execute(
                """
                INSERT INTO provider_usage_logs(
                    provider_log_id, schema_name, provider_request_id,
                    logical_call_id, prompt_tokens, completion_tokens,
                    credits_consumed, credits_remaining, model,
                    provider_created_at_epoch, reconciliation_state,
                    transport_request_id, match_method, normalized_json,
                    reconciled_at_epoch_ns
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider_log_id,
                    SCHEMA,
                    billing_request_id,
                    logical_call_id,
                    prompt_tokens,
                    completion_tokens,
                    consumed,
                    balance,
                    model,
                    created_at,
                    "matched",
                    "",
                    OPERATOR_SENT_UNKNOWN_MATCH_METHOD,
                    json.dumps(
                        normalized,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return normalized


def provider_usage_baseline_status(*, ledger_path: Path) -> dict[str, Any]:
    with _connect(ledger_path) as connection:
        _initialize(connection)
        state = connection.execute(
            """
            SELECT schema_name, capture_row_count, capture_sha256,
                   captured_at_epoch_ns
            FROM provider_usage_baseline_state
            WHERE singleton=1
            """
        ).fetchone()
    if state is None:
        return {
            "schema": BASELINE_SCHEMA,
            "initialized": False,
            "row_count": 0,
            "capture_sha256": "",
            "captured_at_epoch_ns": 0,
        }
    return {
        "schema": str(state["schema_name"]),
        "initialized": True,
        "row_count": int(state["capture_row_count"]),
        "capture_sha256": str(state["capture_sha256"]),
        "captured_at_epoch_ns": int(state["captured_at_epoch_ns"]),
    }


def capture_provider_usage_baseline(
    *, ledger_path: Path, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Atomically freeze provider rows observed before the first model POST.

    A later call is idempotent and can verify overlapping rows, but it can
    never append newly observed rows to the already frozen baseline.
    """

    rows = _usage_rows(payload)
    normalized_rows = [_baseline_normalized(row) for row in rows]
    seen_log_ids: set[str] = set()
    seen_request_ids: set[str] = set()
    for row in normalized_rows:
        log_id = str(row["provider_log_id"])
        request_id = str(row["provider_request_id"])
        if log_id in seen_log_ids:
            raise ProviderUsageError("provider baseline repeats a log ID")
        if request_id and request_id in seen_request_ids:
            raise ProviderUsageError("provider baseline repeats a request ID")
        seen_log_ids.add(log_id)
        if request_id:
            seen_request_ids.add(request_id)
    encoded_capture = json.dumps(
        sorted(normalized_rows, key=lambda item: str(item["provider_log_id"])),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    capture_sha256 = hashlib.sha256(encoded_capture.encode("utf-8")).hexdigest()
    now = time.time_ns()
    created = False
    verified = 0
    ignored_new = 0
    with _connect(ledger_path) as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = connection.execute(
                """
                SELECT * FROM provider_usage_baseline_state
                WHERE singleton=1
                """
            ).fetchone()
            if state is None:
                posted = connection.execute(
                    """
                    SELECT COUNT(*) AS rows
                    FROM api_calls
                    WHERE state IN (
                        'sent', 'response_durable', 'consumed', 'sent_unknown'
                    )
                       OR sent_at_epoch_ns IS NOT NULL
                    """
                ).fetchone()
                reconciled = connection.execute(
                    "SELECT COUNT(*) AS rows FROM provider_usage_logs"
                ).fetchone()
                if (posted is not None and int(posted["rows"]) != 0) or (
                    reconciled is not None and int(reconciled["rows"]) != 0
                ):
                    raise ProviderUsageError(
                        "provider baseline must be captured before first model POST"
                    )
                for row in normalized_rows:
                    encoded = json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    connection.execute(
                        """
                        INSERT INTO provider_usage_baseline_rows(
                            provider_log_id, schema_name,
                            provider_request_id, prompt_tokens,
                            completion_tokens, provider_credit_delta,
                            credits_remaining, model,
                            provider_created_at_epoch, normalized_json,
                            captured_at_epoch_ns
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["provider_log_id"],
                            BASELINE_SCHEMA,
                            row["provider_request_id"],
                            row["prompt_tokens"],
                            row["completion_tokens"],
                            row["provider_credit_delta"],
                            row["credits_remaining"],
                            row["model"],
                            row["provider_created_at_epoch"],
                            encoded,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO provider_usage_baseline_state(
                        singleton, schema_name, capture_row_count,
                        capture_sha256, captured_at_epoch_ns
                    ) VALUES(1, ?, ?, ?, ?)
                    """,
                    (
                        BASELINE_SCHEMA,
                        len(normalized_rows),
                        capture_sha256,
                        now,
                    ),
                )
                created = True
                verified = len(normalized_rows)
            else:
                for observed in normalized_rows:
                    existing = connection.execute(
                        """
                        SELECT * FROM provider_usage_baseline_rows
                        WHERE provider_log_id=?
                           OR (
                               provider_request_id != ''
                               AND provider_request_id=?
                           )
                        """,
                        (
                            observed["provider_log_id"],
                            observed["provider_request_id"],
                        ),
                    ).fetchone()
                    if existing is None:
                        ignored_new += 1
                        continue
                    stored = {
                        "provider_log_id": existing["provider_log_id"],
                        "provider_request_id": existing["provider_request_id"],
                        "prompt_tokens": existing["prompt_tokens"],
                        "completion_tokens": existing["completion_tokens"],
                        "provider_credit_delta": existing["provider_credit_delta"],
                        "credits_remaining": existing["credits_remaining"],
                        "model": existing["model"],
                        "provider_created_at_epoch": existing[
                            "provider_created_at_epoch"
                        ],
                    }
                    if _baseline_tuple(stored) != _baseline_tuple(observed):
                        raise ProviderUsageError(
                            "provider usage history changed for a baseline row"
                        )
                    verified += 1
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    status = provider_usage_baseline_status(ledger_path=ledger_path)
    return {
        **status,
        "created": created,
        "verified_observed_rows": verified,
        "ignored_new_rows": ignored_new,
    }


def ensure_provider_usage_baseline(
    *,
    ledger_path: Path,
    fetch_payload: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """Single-flight baseline capture used immediately before possible POST."""

    resolved = ledger_path.expanduser().resolve()
    lock_path = resolved.with_name(resolved.name + ".provider_usage_baseline.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        status = provider_usage_baseline_status(ledger_path=resolved)
        if status["initialized"]:
            return {**status, "created": False}
        payload = fetch_payload()
        if not isinstance(payload, Mapping):
            raise ProviderUsageError("provider usage payload is invalid")
        return capture_provider_usage_baseline(
            ledger_path=resolved,
            payload=payload,
        )
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def reconcile_provider_logs(
    *, ledger_path: Path, payload: Mapping[str, Any]
) -> dict[str, Any]:
    logs = _usage_rows(payload)
    counts = {"matched": 0, "token_mismatch": 0, "unmatched": 0}
    adjustment_count = 0
    credits = 0
    remaining: int | None = None
    remaining_at_epoch = -1
    seen_request_ids: set[str] = set()
    seen_log_ids: set[str] = set()
    baseline_skipped = 0
    with _connect(ledger_path) as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for raw in logs:
                provider_log_id = str(raw.get("id") or "").strip()
                request_id = str(raw.get("reqid") or "").strip()
                if not provider_log_id:
                    raise ProviderUsageError("provider usage identity is missing")
                if provider_log_id in seen_log_ids:
                    raise ProviderUsageError("provider usage payload repeats a log ID")
                seen_log_ids.add(provider_log_id)
                if request_id and request_id in seen_request_ids:
                    raise ProviderUsageError(
                        "provider usage payload repeats a request ID"
                    )
                if request_id:
                    seen_request_ids.add(request_id)
                baseline = connection.execute(
                    """
                    SELECT * FROM provider_usage_baseline_rows
                    WHERE provider_log_id=?
                       OR (
                           provider_request_id != ''
                           AND provider_request_id=?
                       )
                    """,
                    (provider_log_id, request_id),
                ).fetchone()
                if baseline is not None:
                    observed = _baseline_normalized(raw)
                    stored = {
                        "provider_log_id": baseline["provider_log_id"],
                        "provider_request_id": baseline["provider_request_id"],
                        "prompt_tokens": baseline["prompt_tokens"],
                        "completion_tokens": baseline["completion_tokens"],
                        "provider_credit_delta": baseline["provider_credit_delta"],
                        "credits_remaining": baseline["credits_remaining"],
                        "model": baseline["model"],
                        "provider_created_at_epoch": baseline[
                            "provider_created_at_epoch"
                        ],
                    }
                    if _baseline_tuple(stored) != _baseline_tuple(observed):
                        raise ProviderUsageError(
                            "provider usage history changed for a baseline row"
                        )
                    baseline_skipped += 1
                    continue
                signed_delta = _signed_int(raw.get("fen"), "fen")
                if signed_delta < 0:
                    if not request_id:
                        raise ProviderUsageError(
                            "post-baseline provider credit adjustment is missing "
                            "its payment request identity"
                        )
                    prompt_tokens = _nonnegative_int(
                        raw.get("prompt_tokens"), "prompt_tokens"
                    )
                    completion_tokens = _nonnegative_int(
                        raw.get("completion_tokens"), "completion_tokens"
                    )
                    if prompt_tokens != 0 or completion_tokens != 0:
                        raise ProviderUsageError(
                            "provider credit adjustment contains token usage"
                        )
                    balance = _nonnegative_int(raw.get("yufen"), "yufen")
                    created_at = _nonnegative_int(raw.get("ctime"), "ctime")
                    model = str(raw.get("model") or "")
                    if model != "pay":
                        raise ProviderUsageError(
                            "negative provider credit row is not a payment"
                        )
                    normalized = {
                        "schema": CREDIT_ADJUSTMENT_SCHEMA,
                        "provider_log_id": provider_log_id,
                        "provider_request_id": request_id,
                        "credit_delta": signed_delta,
                        "credits_remaining": balance,
                        "model": model,
                        "provider_created_at_epoch": created_at,
                    }
                    encoded = json.dumps(
                        normalized,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    existing_adjustment = connection.execute(
                        """
                        SELECT * FROM provider_credit_adjustments
                        WHERE provider_log_id=?
                        """,
                        (provider_log_id,),
                    ).fetchone()
                    immutable_observed = (
                        request_id,
                        signed_delta,
                        balance,
                        model,
                        created_at,
                    )
                    if existing_adjustment is None:
                        request_owner = connection.execute(
                            """
                            SELECT provider_log_id
                            FROM provider_credit_adjustments
                            WHERE provider_request_id=? AND provider_request_id!=''
                            """,
                            (request_id,),
                        ).fetchone()
                        if request_owner is not None:
                            raise ProviderUsageError(
                                "provider adjustment request ID maps to multiple rows"
                            )
                        connection.execute(
                            """
                            INSERT INTO provider_credit_adjustments(
                                provider_log_id, schema_name,
                                provider_request_id, credit_delta,
                                credits_remaining, model,
                                provider_created_at_epoch, normalized_json,
                                reconciled_at_epoch_ns
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                provider_log_id,
                                CREDIT_ADJUSTMENT_SCHEMA,
                                request_id,
                                signed_delta,
                                balance,
                                model,
                                created_at,
                                encoded,
                                time.time_ns(),
                            ),
                        )
                    else:
                        immutable_existing = (
                            str(existing_adjustment["provider_request_id"]),
                            int(existing_adjustment["credit_delta"]),
                            int(existing_adjustment["credits_remaining"]),
                            str(existing_adjustment["model"]),
                            int(existing_adjustment["provider_created_at_epoch"]),
                        )
                        if immutable_existing != immutable_observed:
                            raise ProviderUsageError(
                                "provider credit adjustment history changed"
                            )
                    adjustment_count += 1
                    if created_at >= remaining_at_epoch:
                        remaining = balance
                        remaining_at_epoch = created_at
                    continue
                if not request_id:
                    raise ProviderUsageError("provider usage identity is missing")
                prompt_tokens = _nonnegative_int(
                    raw.get("prompt_tokens"), "prompt_tokens"
                )
                completion_tokens = _nonnegative_int(
                    raw.get("completion_tokens"), "completion_tokens"
                )
                consumed = _nonnegative_int(signed_delta, "fen")
                balance = _nonnegative_int(raw.get("yufen"), "yufen")
                created_at = _nonnegative_int(raw.get("ctime"), "ctime")
                model = str(raw.get("model") or "")
                calls, match_method = _local_call_candidates(
                    connection,
                    billing_request_id=request_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model=model,
                    provider_created_at_epoch=created_at,
                    provider_log_id=provider_log_id,
                )
                if len(calls) > 1:
                    raise ProviderUsageError(
                        "provider billing row ambiguously maps to local calls"
                    )
                if not calls:
                    logical_call_id = ""
                    transport_request_id = ""
                    match_method = ""
                    state = "unmatched"
                else:
                    call = calls[0]
                    logical_call_id = str(call["logical_call_id"])
                    transport_request_id = str(call["provider_request_id"] or "")
                    if match_method in {
                        UNIQUE_SENT_UNKNOWN_MATCH_METHOD,
                        OPERATOR_SENT_UNKNOWN_MATCH_METHOD,
                    }:
                        state = "matched"
                    else:
                        state = (
                            "matched"
                            if int(call["prompt_tokens"] or 0) == prompt_tokens
                            and int(call["completion_tokens"] or 0) == completion_tokens
                            else "token_mismatch"
                        )
                normalized = {
                    "schema": SCHEMA,
                    "provider_log_id": provider_log_id,
                    "provider_request_id": request_id,
                    "provider_billing_request_id": request_id,
                    "transport_request_id": transport_request_id,
                    "logical_call_id": logical_call_id,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "credits_consumed": consumed,
                    "credits_remaining": balance,
                    "model": model,
                    "provider_created_at_epoch": created_at,
                    "reconciliation_state": state,
                    "match_method": match_method,
                }
                encoded = json.dumps(
                    normalized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing = connection.execute(
                    """SELECT * FROM provider_usage_logs
                       WHERE provider_log_id=?""",
                    (provider_log_id,),
                ).fetchone()
                if existing is not None:
                    immutable_existing = (
                        str(existing["provider_request_id"]),
                        int(existing["prompt_tokens"]),
                        int(existing["completion_tokens"]),
                        int(existing["credits_consumed"]),
                        int(existing["credits_remaining"]),
                        str(existing["model"]),
                        int(existing["provider_created_at_epoch"]),
                    )
                    immutable_observed = (
                        request_id,
                        prompt_tokens,
                        completion_tokens,
                        consumed,
                        balance,
                        model,
                        created_at,
                    )
                    if immutable_existing != immutable_observed:
                        raise ProviderUsageError(
                            "provider usage history changed for an existing row"
                        )
                    old_state = str(existing["reconciliation_state"])
                    old_logical_call_id = str(existing["logical_call_id"])
                    if old_state == "unmatched" and state in {
                        "matched",
                        "token_mismatch",
                    }:
                        connection.execute(
                            """
                            UPDATE provider_usage_logs
                            SET logical_call_id=?, reconciliation_state=?,
                                transport_request_id=?, match_method=?,
                                normalized_json=?, reconciled_at_epoch_ns=?
                            WHERE provider_log_id=?
                              AND reconciliation_state='unmatched'
                            """,
                            (
                                logical_call_id,
                                state,
                                transport_request_id,
                                match_method,
                                encoded,
                                time.time_ns(),
                                provider_log_id,
                            ),
                        )
                    elif old_state != state or old_logical_call_id != logical_call_id:
                        # In particular, token_mismatch is an immutable
                        # incident that requires explicit human repair.
                        raise ProviderUsageError(
                            "provider reconciliation state regressed or changed"
                        )
                else:
                    request_owner = connection.execute(
                        """
                        SELECT provider_log_id FROM provider_usage_logs
                        WHERE provider_request_id=?
                        """,
                        (request_id,),
                    ).fetchone()
                    if request_owner is not None:
                        raise ProviderUsageError(
                            "provider request ID maps to multiple usage rows"
                        )
                    connection.execute(
                        """INSERT INTO provider_usage_logs(
                               provider_log_id, schema_name, provider_request_id,
                               logical_call_id, prompt_tokens, completion_tokens,
                               credits_consumed, credits_remaining, model,
                               provider_created_at_epoch, reconciliation_state,
                               transport_request_id, match_method,
                               normalized_json, reconciled_at_epoch_ns
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            provider_log_id,
                            SCHEMA,
                            request_id,
                            logical_call_id,
                            prompt_tokens,
                            completion_tokens,
                            consumed,
                            balance,
                            model,
                            created_at,
                            state,
                            transport_request_id,
                            match_method,
                            encoded,
                            time.time_ns(),
                        ),
                    )
                if state == "matched" and logical_call_id:
                    reservation = connection.execute(
                        """
                        SELECT * FROM provider_credit_reservations
                        WHERE logical_call_id=?
                        """,
                        (logical_call_id,),
                    ).fetchone()
                    if reservation is not None:
                        reservation_state = str(reservation["state"])
                        if reservation_state == "active":
                            settled_at = time.time_ns()
                            connection.execute(
                                """
                                UPDATE provider_credit_reservations
                                SET state='settled', actual_credits=?,
                                    provider_log_id=?,
                                    updated_at_epoch_ns=?,
                                    settled_at_epoch_ns=?
                                WHERE logical_call_id=? AND state='active'
                                """,
                                (
                                    consumed,
                                    provider_log_id,
                                    settled_at,
                                    settled_at,
                                    logical_call_id,
                                ),
                            )
                            if consumed > int(reservation["reserved_credits"]):
                                connection.execute(
                                    """
                                    UPDATE global_circuit
                                    SET state='open',
                                        reason=
                                          'provider_credit_reservation_exceeded',
                                        status_code=NULL,
                                        opened_at_epoch_ns=
                                          COALESCE(opened_at_epoch_ns, ?),
                                        retry_after_epoch_ns=NULL,
                                        probe_logical_call_id='',
                                        updated_at_epoch_ns=?
                                    WHERE singleton=1
                                    """,
                                    (settled_at, settled_at),
                                )
                        elif (
                            int(reservation["actual_credits"] or -1) != consumed
                            or str(reservation["provider_log_id"]) != provider_log_id
                        ):
                            raise ProviderUsageError(
                                "settled provider credit reservation changed"
                            )
                counts[state] += 1
                credits += consumed
                if created_at >= remaining_at_epoch:
                    remaining = balance
                    remaining_at_epoch = created_at
            _release_reconciled_health_probe_delivery_unknown(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "schema": SCHEMA,
        "rows": len(logs),
        "baseline_skipped": baseline_skipped,
        "credit_adjustments": adjustment_count,
        "reconciled_rows": len(logs) - baseline_skipped - adjustment_count,
        "counts": counts,
        "credits_consumed": credits,
        "credits_remaining": remaining,
        "all_matched": counts["unmatched"] == 0 and counts["token_mismatch"] == 0,
    }


def local_billing_summary(*, ledger_path: Path) -> dict[str, Any]:
    """Require every locally delivered request to have one matched bill row."""

    with _connect(ledger_path) as connection:
        _initialize(connection)
        rows = connection.execute(
            """
            SELECT c.logical_call_id, c.provider_request_id,
                   u.reconciliation_state, u.credits_consumed,
                   u.credits_remaining
            FROM api_calls c
            LEFT JOIN provider_usage_logs u
              ON u.logical_call_id=c.logical_call_id
            WHERE c.state IN ('response_durable', 'consumed')
              AND c.response_status_code < 400
            ORDER BY c.created_at_epoch_ns
            """
        ).fetchall()
        provider_unmatched_row = connection.execute(
            """
            SELECT COUNT(*) AS rows,
                   COALESCE(SUM(credits_consumed), 0) AS credits
            FROM provider_usage_logs
            WHERE reconciliation_state='unmatched'
            """
        ).fetchone()
        matched_billing_row = connection.execute(
            """
            SELECT COUNT(*) AS rows,
                   COALESCE(SUM(credits_consumed), 0) AS credits,
                   MIN(credits_remaining) AS credits_remaining
            FROM provider_usage_logs
            WHERE reconciliation_state='matched'
            """
        ).fetchone()
        latest_balance_row = connection.execute(
            """
            SELECT credits_remaining
            FROM (
                SELECT credits_remaining, provider_created_at_epoch
                FROM provider_usage_logs
                UNION ALL
                SELECT credits_remaining, provider_created_at_epoch
                FROM provider_credit_adjustments
            )
            ORDER BY provider_created_at_epoch DESC
            LIMIT 1
            """
        ).fetchone()
        sent_unknown_billed_row = connection.execute(
            """
            SELECT COUNT(*) AS rows
            FROM api_calls c
            JOIN provider_usage_logs u
              ON u.logical_call_id=c.logical_call_id
            WHERE c.state='sent_unknown'
              AND u.reconciliation_state='matched'
            """
        ).fetchone()
        assert (
            provider_unmatched_row is not None
            and matched_billing_row is not None
            and sent_unknown_billed_row is not None
        )
    missing = 0
    mismatch = 0
    for row in rows:
        if (
            not str(row["provider_request_id"] or "")
            or row["reconciliation_state"] is None
        ):
            missing += 1
            continue
        if str(row["reconciliation_state"]) != "matched":
            mismatch += 1
            continue
    credits = int(matched_billing_row["credits"])
    remaining = (
        int(latest_balance_row["credits_remaining"])
        if latest_balance_row is not None
        else None
    )
    return {
        "schema": SCHEMA,
        "local_successful_calls": len(rows),
        "matched": len(rows) - missing - mismatch,
        "missing": missing,
        "mismatch": mismatch,
        "provider_unmatched": int(provider_unmatched_row["rows"]),
        "provider_unmatched_credits": int(provider_unmatched_row["credits"]),
        "sent_unknown_billed": int(sent_unknown_billed_row["rows"]),
        "credits_consumed": credits,
        "credits_remaining": remaining,
        "all_matched": (
            missing == 0 and mismatch == 0 and int(provider_unmatched_row["rows"]) == 0
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--secret", type=Path, required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = fetch_provider_usage(
        api_key=read_paid_api_secret(args.secret),
        start=args.start,
        limit=args.limit,
    )
    result = reconcile_provider_logs(
        ledger_path=args.ledger,
        payload=payload,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["all_matched"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
