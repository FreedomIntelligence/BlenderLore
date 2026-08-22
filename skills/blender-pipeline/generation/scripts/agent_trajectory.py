"""Append-only, per-asset observable agent trajectories."""

from __future__ import annotations

import fcntl
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "video2blender.agent-trajectory.v1"
EVENT_SCHEMA = "video2blender.agent-trajectory-event.v1"
ENV_ROOT = "VIDEO2BLENDER_AGENT_TRAJECTORY_ROOT"
EMPTY_SHA256 = "0" * 64
SENSITIVE_KEY = re.compile(
    r"authorization|api[-_]?key|cookie|password|passwd|secret|"
    r"extract[-_]?code|passcode|(?:access|refresh|auth)[-_]?token",
    re.I,
)
BEARER_VALUE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
URI_CREDENTIAL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^/@:\s]+:)[^@/\s]+@")
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|api[-_]?key|access[-_]?token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
SENSITIVE_QUERY_VALUE = re.compile(r"(?i)([?&](?:pwd|passcode|extract_code)=)[^&#\s]+")
API_INDEX_SCHEMA = "video2blender.agent-api-call-index.v1"
OBJECT_ARCHIVE_SCHEMA = "video2blender.agent-trajectory-object-archive.v1"
OBJECT_ARCHIVE_NAME = "objects.zip"
OBJECT_ARCHIVE_INDEX_NAME = "objects.index.json"


class TrajectoryError(RuntimeError):
    pass


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_index(
    root: Path, *, verify_archive_hash: bool = True
) -> dict[str, Any] | None:
    index_path = root / OBJECT_ARCHIVE_INDEX_NAME
    archive_path = root / OBJECT_ARCHIVE_NAME
    if not index_path.is_file() and not archive_path.is_file():
        return None
    if not index_path.is_file() or not archive_path.is_file():
        raise TrajectoryError("trajectory object archive is incomplete")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if (
        index.get("schema") != OBJECT_ARCHIVE_SCHEMA
        or index.get("archive") != OBJECT_ARCHIVE_NAME
        or (verify_archive_hash and index.get("sha256") != sha256_file(archive_path))
    ):
        raise TrajectoryError("trajectory object archive index is invalid")
    return index


def _read_attachment_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    if path.is_file():
        return path.read_bytes()
    index = _archive_index(root, verify_archive_hash=False)
    if index is None or not relative.startswith("objects/"):
        raise TrajectoryError("trajectory attachment is missing")
    archive_path = root / OBJECT_ARCHIVE_NAME
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            info = archive.getinfo(relative)
            if info.is_dir() or info.file_size < 0:
                raise TrajectoryError("trajectory object archive member is invalid")
            return archive.read(info)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise TrajectoryError("trajectory object archive member is missing") from exc


def redact(value: Any, *, key: str = "") -> Any:
    """Remove credentials while retaining prompts, code, and diagnostics."""

    if key and SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": redact(str(value))}
    if isinstance(value, str):
        clean = BEARER_VALUE.sub(r"\1<redacted>", value)
        clean = URI_CREDENTIAL.sub(r"\1<redacted>@", clean)
        clean = SENSITIVE_QUERY_VALUE.sub(r"\1<redacted>", clean)
        return SENSITIVE_ASSIGNMENT.sub(r"\1\2<redacted>", clean)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AgentTrajectory:
    """Durable event stream plus content-addressed multimodal/tool objects."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.events_path = self.root / "trajectory.jsonl"
        self.manifest_path = self.root / "manifest.json"
        self.lock_path = self.root / ".trajectory.lock"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        identity: Mapping[str, Any],
        completeness: str = "complete_observable_trace",
    ) -> "AgentTrajectory":
        recorder = cls(root)
        recorder.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(recorder.root, 0o700)
        (recorder.root / "objects").mkdir(exist_ok=True, mode=0o700)
        clean_identity = redact(identity)
        if recorder.manifest_path.exists():
            with recorder.lock_path.open("a+b") as lock:
                os.chmod(recorder.lock_path, 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                existing = recorder._recover_manifest_tail_locked(
                    json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
                )
                if existing.get("identity") != clean_identity:
                    raise TrajectoryError(
                        "trajectory identity conflicts with existing manifest"
                    )
                prior_status = str(existing.get("status") or "running")
                existing["status"] = "running"
                existing.pop("completed_at_epoch_ns", None)
                existing["resumed_at_epoch_ns"] = time.time_ns()
                existing["resume_count"] = int(existing.get("resume_count") or 0) + 1
                _atomic_json(recorder.manifest_path, existing)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            recovered = recorder.recover_interrupted_calls()
            recorder.append_event(
                event_type="trajectory_resume",
                role="tool",
                stage="process_recovery",
                content={
                    "prior_status": prior_status,
                    "recovered_call_ids": recovered,
                    "policy": "observable_outputs_only_no_output_reconstruction",
                },
            )
            return recorder
        _atomic_json(
            recorder.manifest_path,
            {
                "schema": SCHEMA,
                "identity": clean_identity,
                "completeness": completeness,
                "created_at_epoch_ns": time.time_ns(),
                "event_count": 0,
                "event_chain_head_sha256": EMPTY_SHA256,
                "status": "running",
            },
        )
        return recorder

    def _recover_manifest_tail_locked(
        self, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Recover a fully written JSONL tail missed by an interrupted manifest swap."""

        if not self.events_path.is_file():
            return dict(manifest)
        rows = self.events_path.read_text(encoding="utf-8").split("\n")
        if rows and not rows[-1]:
            rows.pop()
        expected_count = int(manifest.get("event_count") or 0)
        if len(rows) == expected_count:
            return dict(manifest)
        if len(rows) != expected_count + 1:
            return dict(manifest)
        try:
            tail = json.loads(rows[-1])
        except json.JSONDecodeError:
            return dict(manifest)
        if tail.get("sequence") != len(rows) or tail.get(
            "previous_event_sha256"
        ) != manifest.get("event_chain_head_sha256"):
            return dict(manifest)
        candidate = dict(tail)
        claimed = str(candidate.pop("event_sha256", ""))
        if claimed != hashlib.sha256(canonical_bytes(candidate)).hexdigest():
            return dict(manifest)
        recovered = dict(manifest)
        recovered["event_count"] = len(rows)
        recovered["event_chain_head_sha256"] = claimed
        recovered["manifest_tail_recovery_count"] = (
            int(recovered.get("manifest_tail_recovery_count") or 0) + 1
        )
        recovered["manifest_tail_recovered_at_epoch_ns"] = time.time_ns()
        _atomic_json(self.manifest_path, recovered)
        return recovered

    @classmethod
    def from_environment(cls) -> "AgentTrajectory | None":
        value = os.environ.get(ENV_ROOT, "").strip()
        if not value:
            return None
        recorder = cls(Path(value))
        if not recorder.manifest_path.is_file():
            raise TrajectoryError("configured trajectory manifest is missing")
        return recorder

    def activate_environment(self) -> None:
        os.environ[ENV_ROOT] = str(self.root)

    def _store_bytes(
        self, payload: bytes, *, name: str, mime_type: str
    ) -> dict[str, Any]:
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.root / "objects" / digest[:2] / digest
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            if (
                destination.stat().st_size != len(payload)
                or sha256_file(destination) != digest
            ):
                raise TrajectoryError("content-addressed trajectory object is corrupt")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.", dir=destination.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "name": name,
            "sha256": digest,
            "size_bytes": len(payload),
            "mime_type": mime_type,
            "object_path": str(destination.relative_to(self.root)),
        }

    def store_json(self, payload: object, *, name: str) -> dict[str, Any]:
        return self._store_bytes(
            canonical_bytes(redact(payload)), name=name, mime_type="application/json"
        )

    def store_bytes(
        self, payload: bytes, *, name: str, mime_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        return self._store_bytes(payload, name=name, mime_type=mime_type)

    def store_path(
        self, source: Path, *, semantic_role: str = "artifact"
    ) -> list[dict[str, Any]]:
        source = source.expanduser().resolve()
        if not source.exists():
            return []
        if source.is_symlink():
            raise TrajectoryError(
                f"trajectory attachment cannot be a symlink: {source}"
            )
        if source.is_file():
            sources = [(source, source.name)]
        elif source.is_dir():
            sources = [
                (path, str(path.relative_to(source)))
                for path in sorted(source.rglob("*"))
                if path.is_file() and not path.is_symlink()
            ]
        else:
            raise TrajectoryError(f"unsupported trajectory attachment: {source}")
        stored = []
        for path, relative_name in sources:
            digest = sha256_file(path)
            destination = self.root / "objects" / digest[:2] / digest
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                if (
                    destination.stat().st_size != path.stat().st_size
                    or sha256_file(destination) != digest
                ):
                    raise TrajectoryError(
                        "content-addressed trajectory object is corrupt"
                    )
            else:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{digest}.", dir=destination.parent
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    shutil.copyfile(path, temporary)
                    os.chmod(temporary, 0o600)
                    if sha256_file(temporary) != digest:
                        raise TrajectoryError(
                            "trajectory attachment copy verification failed"
                        )
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            stored.append(
                {
                    "name": relative_name,
                    "semantic_role": semantic_role,
                    "sha256": digest,
                    "size_bytes": path.stat().st_size,
                    "mime_type": mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    "object_path": str(destination.relative_to(self.root)),
                }
            )
        return stored

    def reference_path(
        self, source: Path, *, semantic_role: str = "artifact"
    ) -> list[dict[str, Any]]:
        """Reference landed sibling artifacts without duplicating large media/blends."""

        source = source.expanduser().resolve()
        if not source.exists():
            return []
        if source.is_symlink():
            raise TrajectoryError(
                f"trajectory attachment cannot be a symlink: {source}"
            )
        paths = (
            [source]
            if source.is_file()
            else [
                path
                for path in sorted(source.rglob("*"))
                if path.is_file() and not path.is_symlink()
            ]
        )
        return [
            {
                "name": path.name
                if source.is_file()
                else str(path.relative_to(source)),
                "semantic_role": semantic_role,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "mime_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "relative_path": os.path.relpath(path, self.root),
                "storage": "result_sibling_reference",
            }
            for path in paths
        ]

    def append_event(
        self,
        *,
        event_type: str,
        role: str,
        stage: str,
        content: Any,
        attempt: int = 0,
        turn: int = 0,
        tool_name: str = "",
        tool_call_id: str = "",
        parent_event_ids: Iterable[str] = (),
        attachments: Iterable[Mapping[str, Any]] = (),
        status: str = "ok",
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+b") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            event = {
                "schema": EVENT_SCHEMA,
                "sequence": int(manifest.get("event_count", 0)) + 1,
                "event_id": uuid.uuid4().hex,
                "previous_event_sha256": str(
                    manifest.get("event_chain_head_sha256") or EMPTY_SHA256
                ),
                "timestamp_epoch_ns": time.time_ns(),
                "event_type": event_type,
                "role": role,
                "stage": stage,
                "attempt": int(attempt),
                "turn": int(turn),
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "parent_event_ids": list(parent_event_ids),
                "status": status,
                "content": redact(content),
                "attachments": [redact(dict(item)) for item in attachments],
            }
            event["event_sha256"] = hashlib.sha256(canonical_bytes(event)).hexdigest()
            with self.events_path.open("ab") as handle:
                handle.write(canonical_bytes(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            manifest["event_count"] = event["sequence"]
            manifest["event_chain_head_sha256"] = event["event_sha256"]
            manifest["updated_at_epoch_ns"] = time.time_ns()
            _atomic_json(self.manifest_path, manifest)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return event

    def record_tool_call(
        self,
        tool_name: str,
        tool_input: Any,
        *,
        stage: str,
        attempt: int = 0,
        turn: int = 0,
        attachments: Iterable[Mapping[str, Any]] = (),
    ) -> str:
        call_id = "call_" + uuid.uuid4().hex
        self.append_event(
            event_type="tool_call",
            role="assistant",
            stage=stage,
            content={"input": tool_input},
            attempt=attempt,
            turn=turn,
            tool_name=tool_name,
            tool_call_id=call_id,
            attachments=attachments,
        )
        return call_id

    def record_tool_result(
        self,
        tool_name: str,
        tool_call_id: str,
        output: Any,
        *,
        stage: str,
        attempt: int = 0,
        turn: int = 0,
        attachments: Iterable[Mapping[str, Any]] = (),
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        # Reject the incident-producing shape at the write boundary.  Waiting
        # until SFT/ATIF export to notice an empty or foreign call ID makes a
        # successfully completed paid preparation look retryable.
        if not str(tool_call_id).strip():
            raise TrajectoryError("tool_result requires a non-empty tool_call_id")
        if self.events_path.is_file():
            # Known immutable legacy traces may already contain an orphan
            # written before this boundary check existed.  They must remain
            # appendable for the explicit recovery path, while this new result
            # is still required to match its own prior call.
            _manifest, events = load_and_verify(self.root, allow_unmatched_results=True)
        else:
            events = []
        matching_calls = [
            event
            for event in events
            if event.get("event_type") == "tool_call"
            and event.get("tool_call_id") == tool_call_id
        ]
        if len(matching_calls) != 1:
            raise TrajectoryError(
                "tool_result call_id does not match exactly one prior tool_call"
            )
        if str(matching_calls[0].get("tool_name") or "") != str(tool_name):
            raise TrajectoryError("tool_result tool_name differs from its tool_call")
        if any(
            event.get("event_type") == "tool_result"
            and event.get("tool_call_id") == tool_call_id
            for event in events
        ):
            raise TrajectoryError("tool_result already exists for this call_id")
        content = {"output": output}
        if error is not None:
            content["error"] = {"type": type(error).__name__, "message": str(error)}
        return self.append_event(
            event_type="tool_result",
            role="tool",
            stage=stage,
            content=content,
            attempt=attempt,
            turn=turn,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            attachments=attachments,
            status="error" if error is not None else "ok",
        )

    def recover_interrupted_calls(self) -> list[str]:
        """Close calls whose process died before an observable result was recorded."""

        if not self.events_path.exists():
            return []
        manifest, events = load_and_verify(self.root)
        if manifest.get("status") != "running":
            return []
        calls = {
            str(event["tool_call_id"]): event
            for event in events
            if event.get("event_type") in {"tool_call", "api_request"}
            and event.get("tool_call_id")
        }
        completed = {
            str(event["tool_call_id"])
            for event in events
            if event.get("event_type") in {"tool_result", "api_response"}
            and event.get("tool_call_id")
        }
        recovered = []
        for call_id, event in calls.items():
            if call_id in completed:
                continue
            self.append_event(
                event_type=(
                    "api_response"
                    if event.get("event_type") == "api_request"
                    else "tool_result"
                ),
                role="tool",
                stage=str(event.get("stage") or "process_recovery"),
                attempt=int(event.get("attempt") or 0),
                turn=int(event.get("turn") or 0),
                tool_name=str(event.get("tool_name") or "interrupted_call"),
                tool_call_id=call_id,
                status="error",
                content={
                    "error": {
                        "type": "InterruptedProcess",
                        "message": (
                            "The prior process ended before an observable result was "
                            "durably recorded; no output was reconstructed."
                        ),
                    }
                },
            )
            recovered.append(call_id)
        return recovered

    def finish(self, status: str, outcome: Any) -> None:
        outcome_sha256 = hashlib.sha256(canonical_bytes(redact(outcome))).hexdigest()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("status") == status
            and manifest.get("final_outcome_sha256") == outcome_sha256
        ):
            return
        self.append_event(
            event_type="outcome",
            role="tool",
            stage="terminal",
            content=outcome,
            status=status,
        )
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = status
        manifest["final_outcome_sha256"] = outcome_sha256
        manifest["completed_at_epoch_ns"] = time.time_ns()
        _atomic_json(self.manifest_path, manifest)

    def export_derived(self) -> dict[str, Path]:
        manifest, events = load_and_verify(self.root)
        atif_path = self.root / "atif" / "trajectory.json"
        sft_path = self.root / "sft" / "messages.json"
        api_index_path = self.root / "api_calls" / "index.json"
        validation = validate_observable_trace(manifest, events, self.root)
        _atomic_json(atif_path, export_atif(manifest, events, self.root))
        _atomic_json(
            sft_path,
            export_sft(manifest, events, self.root, validation=validation),
        )
        _atomic_json(
            api_index_path,
            export_api_call_index(manifest, events, validation=validation),
        )
        self._set_projection_state("complete")
        return {"atif": atif_path, "sft": sft_path, "api_index": api_index_path}

    def _set_projection_state(
        self, state: str, error: BaseException | None = None
    ) -> None:
        with self.lock_path.open("a+b") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["derived_projection_state"] = state
            manifest["derived_projection_updated_at_epoch_ns"] = time.time_ns()
            if error is None:
                manifest.pop("derived_projection_error", None)
            else:
                clean = redact(error)
                message = (
                    str(clean.get("message") or "")
                    if isinstance(clean, Mapping)
                    else str(clean)
                )
                manifest["derived_projection_error"] = (
                    f"{type(error).__name__}: {message}"[:1000]
                )
            _atomic_json(self.manifest_path, manifest)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def try_export_derived(self) -> dict[str, Path] | None:
        """Best-effort projection; raw trajectory remains publishable on failure."""

        try:
            return self.export_derived()
        except Exception as exc:
            # Projection state is itself only a convenience view.  ENOSPC or a
            # failed manifest swap must not turn a trace-formatting problem
            # back into an asset/API retry.
            try:
                self._set_projection_state("pending_repair", exc)
            except Exception:
                pass
            return None

    def publish_to(self, destination: Path) -> Path:
        """Atomically place the raw trace beside a landed asset.

        Derived ATIF/SFT/API indexes are best-effort views.  Their failure must
        not make an already completed asset or paid response retryable.
        """

        self.try_export_derived()
        destination = destination.expanduser().resolve()
        if destination == self.root:
            return destination
        self._assert_sibling_references_publishable(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        hold: Path | None = None
        try:
            shutil.rmtree(temporary)
            shutil.copytree(
                self.root,
                temporary,
                ignore=shutil.ignore_patterns(".trajectory.lock"),
            )
            copied_manifest, _events = load_and_verify(temporary)
            source_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if copied_manifest.get("event_chain_head_sha256") != source_manifest.get(
                "event_chain_head_sha256"
            ):
                raise TrajectoryError("published trajectory verification differs")
            if destination.exists():
                existing_manifest = destination / "manifest.json"
                if existing_manifest.is_file():
                    existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
                    if existing.get("event_chain_head_sha256") == source_manifest.get(
                        "event_chain_head_sha256"
                    ) and (
                        existing.get("derived_projection_state")
                        == source_manifest.get("derived_projection_state")
                        or existing.get("derived_projection_state") == "complete"
                    ):
                        shutil.rmtree(temporary)
                        return destination
                hold = (
                    destination.parent
                    / f".{destination.name}.{uuid.uuid4().hex}.replaced"
                )
                os.replace(destination, hold)
            os.replace(temporary, destination)
            if hold is not None:
                shutil.rmtree(hold, ignore_errors=True)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if hold is not None and hold.exists() and not destination.exists():
                os.replace(hold, destination)
            raise

    def publish_compact_to(self, destination: Path) -> Path:
        """Publish a raw trace with content objects packed for ExFAT."""

        self.try_export_derived()
        destination = destination.expanduser().resolve()
        if destination == self.root:
            raise TrajectoryError(
                "compact trajectory destination must differ from source"
            )
        self._assert_sibling_references_publishable(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        hold: Path | None = None
        try:
            shutil.rmtree(temporary)
            shutil.copytree(
                self.root,
                temporary,
                ignore=shutil.ignore_patterns(
                    ".trajectory.lock",
                    "objects",
                    OBJECT_ARCHIVE_NAME,
                    OBJECT_ARCHIVE_INDEX_NAME,
                ),
            )
            source_objects = self.root / "objects"
            archive_path = temporary / OBJECT_ARCHIVE_NAME
            object_count = 0
            total_size = 0
            if source_objects.is_dir():
                with zipfile.ZipFile(
                    archive_path,
                    "w",
                    compression=zipfile.ZIP_STORED,
                    allowZip64=True,
                ) as archive:
                    for source in sorted(source_objects.rglob("*")):
                        if not source.is_file() or source.is_symlink():
                            continue
                        relative = source.relative_to(self.root).as_posix()
                        archive.write(source, relative)
                        object_count += 1
                        total_size += source.stat().st_size
            elif (self.root / OBJECT_ARCHIVE_NAME).is_file():
                shutil.copy2(self.root / OBJECT_ARCHIVE_NAME, archive_path)
                existing = _archive_index(self.root)
                assert existing is not None
                object_count = int(existing.get("object_count") or 0)
                total_size = int(existing.get("total_size_bytes") or 0)
            else:
                with zipfile.ZipFile(
                    archive_path,
                    "w",
                    compression=zipfile.ZIP_STORED,
                    allowZip64=True,
                ):
                    pass
            _atomic_json(
                temporary / OBJECT_ARCHIVE_INDEX_NAME,
                {
                    "schema": OBJECT_ARCHIVE_SCHEMA,
                    "archive": OBJECT_ARCHIVE_NAME,
                    "sha256": sha256_file(archive_path),
                    "object_count": object_count,
                    "total_size_bytes": total_size,
                },
            )
            copied_manifest, _events = load_and_verify(temporary)
            source_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if copied_manifest.get("event_chain_head_sha256") != source_manifest.get(
                "event_chain_head_sha256"
            ):
                raise TrajectoryError(
                    "published compact trajectory verification differs"
                )
            if destination.exists():
                existing_manifest = destination / "manifest.json"
                if existing_manifest.is_file():
                    existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
                    if existing.get("event_chain_head_sha256") == source_manifest.get(
                        "event_chain_head_sha256"
                    ) and (
                        existing.get("derived_projection_state")
                        == source_manifest.get("derived_projection_state")
                        or existing.get("derived_projection_state") == "complete"
                    ):
                        shutil.rmtree(temporary)
                        return destination
                hold = (
                    destination.parent
                    / f".{destination.name}.{uuid.uuid4().hex}.replaced"
                )
                os.replace(destination, hold)
            os.replace(temporary, destination)
            if hold is not None:
                shutil.rmtree(hold, ignore_errors=True)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if hold is not None and hold.exists() and not destination.exists():
                os.replace(hold, destination)
            raise

    def _assert_sibling_references_publishable(self, destination: Path) -> None:
        """Reject a move that would strand relative references outside the result."""

        _manifest, events = load_and_verify(self.root)
        for event in events:
            for attachment in event.get("attachments", []):
                if attachment.get("storage") != "result_sibling_reference":
                    continue
                relative = str(attachment.get("relative_path") or "")
                source = (self.root / relative).resolve()
                target = (destination / relative).resolve()
                if not source.is_file() or not target.is_file():
                    raise TrajectoryError(
                        "published trajectory would strand a result sibling reference"
                    )
                if (
                    source.stat().st_size != target.stat().st_size
                    or sha256_file(source) != sha256_file(target)
                    or sha256_file(target) != attachment.get("sha256")
                ):
                    raise TrajectoryError(
                        "published trajectory sibling reference differs from its source"
                    )

    def relocate_to(self, destination: Path) -> "AgentTrajectory":
        """Move a running trace into its final result directory without losing state."""

        destination = destination.expanduser().resolve()
        if destination == self.root:
            return self
        if destination.exists():
            raise TrajectoryError(
                "trajectory destination already exists during relocation"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.root, destination)
        self.root = destination
        self.events_path = self.root / "trajectory.jsonl"
        self.manifest_path = self.root / "manifest.json"
        self.lock_path = self.root / ".trajectory.lock"
        self.activate_environment()
        return self


def load_and_verify(
    root: Path,
    *,
    allow_unmatched_results: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recorder = AgentTrajectory(root)
    # API tracing may append the final response from a child process while the
    # parent exports ATIF/SFT.  Snapshot both files under the same lock used by
    # append_event so verification never observes a half-written JSONL row.
    with recorder.lock_path.open("a+b") as lock:
        os.chmod(recorder.lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            manifest_text = recorder.manifest_path.read_text(encoding="utf-8")
            events_text = recorder.events_path.read_text(encoding="utf-8")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    manifest = json.loads(manifest_text)
    archive_index = _archive_index(recorder.root)
    archive: zipfile.ZipFile | None = None
    if archive_index is not None:
        try:
            archive = zipfile.ZipFile(recorder.root / OBJECT_ARCHIVE_NAME, "r")
        except zipfile.BadZipFile as exc:
            raise TrajectoryError("trajectory object archive is corrupt") from exc
    events = []
    previous = EMPTY_SHA256
    # JSONL records are delimited only by ASCII LF.  str.splitlines() also
    # splits valid JSON strings containing NEL/line-separator characters from
    # model output, making an intact event look like a truncated JSON row.
    rows = events_text.split("\n")
    for sequence, raw in enumerate(rows, start=1):
        if sequence == len(rows) and not raw:
            continue
        if not raw.strip():
            raise TrajectoryError("trajectory contains a blank event line")
        event = json.loads(raw)
        claimed = event.pop("event_sha256", "")
        actual = hashlib.sha256(canonical_bytes(event)).hexdigest()
        event["event_sha256"] = claimed
        if (
            event.get("sequence") != sequence
            or event.get("previous_event_sha256") != previous
        ):
            raise TrajectoryError("trajectory event sequence or hash chain is invalid")
        if claimed != actual:
            raise TrajectoryError("trajectory event hash is invalid")
        for attachment in event.get("attachments", []):
            relative = attachment.get("object_path") or attachment.get("relative_path")
            if not relative:
                raise TrajectoryError("trajectory attachment has no path")
            relative = str(relative)
            path = recorder.root / relative
            if path.is_file():
                size = path.stat().st_size
                digest = sha256_file(path)
            elif archive is not None and relative.startswith("objects/"):
                try:
                    info = archive.getinfo(relative)
                    with archive.open(info, "r") as handle:
                        digest_state = hashlib.sha256()
                        size = 0
                        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                            size += len(chunk)
                            digest_state.update(chunk)
                    digest = digest_state.hexdigest()
                except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise TrajectoryError(
                        "trajectory object archive member is missing"
                    ) from exc
            else:
                raise TrajectoryError(
                    "trajectory attachment is missing or has wrong size"
                )
            if size != int(attachment["size_bytes"]):
                raise TrajectoryError(
                    "trajectory attachment is missing or has wrong size"
                )
            if digest != attachment["sha256"]:
                raise TrajectoryError("trajectory attachment hash is invalid")
        previous = claimed
        events.append(event)
    if (
        manifest.get("event_count") != len(events)
        or manifest.get("event_chain_head_sha256") != previous
    ):
        raise TrajectoryError("trajectory manifest does not match its event stream")
    calls = {
        event["tool_call_id"]
        for event in events
        if event.get("event_type") in {"tool_call", "api_request"}
    }
    results = {
        event["tool_call_id"]
        for event in events
        if event.get("event_type") in {"tool_result", "api_response"}
    }
    if results - calls and not allow_unmatched_results:
        raise TrajectoryError("trajectory has a result without a matching call")
    if manifest.get("status") not in {"running", "blocked"} and (
        calls - results or (results - calls and not allow_unmatched_results)
    ):
        raise TrajectoryError("completed trajectory has unmatched calls")
    if archive is not None:
        archive.close()
    return manifest, events


def _load_attachment_json(root: Path, attachment: Mapping[str, Any]) -> Any:
    payload = _read_attachment_bytes(root, str(attachment["object_path"]))
    return json.loads(payload.decode("utf-8"))


def _attachment_by_name(
    event: Mapping[str, Any] | None, name: str
) -> dict[str, Any] | None:
    if event is None:
        return None
    return next(
        (
            dict(item)
            for item in event.get("attachments", [])
            if item.get("name") == name
        ),
        None,
    )


def _contains_unredacted_credential(value: Any, *, key: str = "") -> bool:
    if key and SENSITIVE_KEY.search(key):
        return value != "<redacted>"
    if isinstance(value, Mapping):
        return any(
            _contains_unredacted_credential(item, key=str(item_key))
            for item_key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_unredacted_credential(item) for item in value)
    if not isinstance(value, str):
        return False
    if BEARER_VALUE.search(value) and "Bearer <redacted>" not in value:
        return True
    if URI_CREDENTIAL.search(value) and ":<redacted>@" not in value:
        return True
    return False


def _wire_request_has_unredacted_credential(
    root: Path, event: Mapping[str, Any]
) -> bool:
    attachment = _attachment_by_name(event, "wire_request_body.bin")
    if attachment is None:
        return False
    payload = _read_attachment_bytes(root, str(attachment["object_path"]))
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        text = payload.decode("utf-8", errors="ignore")
        return bool(
            (BEARER_VALUE.search(text) and "Bearer <redacted>" not in text)
            or (URI_CREDENTIAL.search(text) and ":<redacted>@" not in text)
        )
    return _contains_unredacted_credential(parsed)


def validate_observable_trace(
    manifest: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    root: Path,
) -> dict[str, Any]:
    """Summarize the SFT safety gates already enforced by load_and_verify()."""

    api_requests = {
        str(event["tool_call_id"]): event
        for event in events
        if event.get("event_type") == "api_request" and event.get("tool_call_id")
    }
    api_responses = {
        str(event["tool_call_id"]): event
        for event in events
        if event.get("event_type") == "api_response" and event.get("tool_call_id")
    }
    unmatched = sorted(set(api_requests) ^ set(api_responses))
    credential_leaks = []
    for event in events:
        if _contains_unredacted_credential(event.get("content", {})):
            credential_leaks.append(str(event.get("event_id") or "unknown"))
        if event.get(
            "event_type"
        ) == "api_response" and _wire_request_has_unredacted_credential(root, event):
            credential_leaks.append(str(event.get("event_id") or "unknown"))
    return {
        "hash_chain_verified": True,
        "attachments_verified": True,
        "call_pairs_complete": not unmatched,
        "unmatched_call_ids": unmatched,
        "credential_scan_passed": not credential_leaks,
        "credential_leak_event_ids": sorted(set(credential_leaks)),
        "api_request_count": len(api_requests),
        "api_response_count": len(api_responses),
        "api_pair_count": len(set(api_requests) & set(api_responses)),
        "terminal": manifest.get("status") not in {"running", "blocked"},
    }


def export_api_call_index(
    manifest: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    *,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a human-navigable index without duplicating lossless payloads."""

    responses = {
        str(event["tool_call_id"]): event
        for event in events
        if event.get("event_type") == "api_response" and event.get("tool_call_id")
    }
    calls = []
    for request in events:
        if request.get("event_type") != "api_request":
            continue
        call_id = str(request.get("tool_call_id") or "")
        response = responses.get(call_id)
        calls.append(
            {
                "call_id": call_id,
                "stage": request.get("stage"),
                "attempt": request.get("attempt"),
                "turn": request.get("turn"),
                "request_event_id": request.get("event_id"),
                "response_event_id": response.get("event_id") if response else None,
                "status": response.get("status") if response else "in_progress",
                "request_payload": _attachment_by_name(request, "request_payload.json"),
                "wire_request_body": _attachment_by_name(
                    response, "wire_request_body.bin"
                ),
                "response_body": _attachment_by_name(response, "response_body.bin"),
                "model_visible_inputs": [
                    dict(item)
                    for item in request.get("attachments", [])
                    if item.get("semantic_role") == "model_visible_input"
                ],
            }
        )
    return {
        "schema": API_INDEX_SCHEMA,
        "identity": manifest.get("identity"),
        "trajectory_status": manifest.get("status"),
        "event_chain_head_sha256": manifest.get("event_chain_head_sha256"),
        "validation": dict(validation),
        "calls": calls,
    }


def _attachment_content_parts(
    attachments: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    parts = []
    for attachment in attachments:
        if str(attachment.get("mime_type") or "").startswith("image/"):
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "media_type": attachment.get("mime_type"),
                        "path": attachment.get("object_path")
                        or attachment.get("relative_path"),
                    },
                    "extra": {
                        "sha256": attachment.get("sha256"),
                        "semantic_role": attachment.get("semantic_role", "model_input"),
                        "name": attachment.get("name", ""),
                    },
                }
            )
    return parts


def _atif_content(
    value: Any,
    attachments: Iterable[Mapping[str, Any]] = (),
) -> str | list[dict[str, Any]]:
    image_parts = _attachment_content_parts(attachments)
    if isinstance(value, str) and not image_parts:
        return value
    if isinstance(value, list):
        text_parts = []
        for item in value:
            if isinstance(item, str):
                text_parts.append({"type": "text", "text": item})
            elif isinstance(item, Mapping) and item.get("type") == "text":
                text_parts.append({"type": "text", "text": str(item.get("text") or "")})
        return text_parts + image_parts
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ([{"type": "text", "text": text}] + image_parts) if image_parts else text


def _timestamp(epoch_ns: object) -> str:
    try:
        value = int(epoch_ns) / 1_000_000_000
    except (TypeError, ValueError):
        value = 0
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat()


def _api_payload(event: Mapping[str, Any], root: Path) -> dict[str, Any]:
    for attachment in event.get("attachments", []):
        if attachment.get("name") == "request_payload.json":
            payload = _load_attachment_json(root, attachment)
            return payload if isinstance(payload, dict) else {}
    return {}


def _api_response(event: Mapping[str, Any], root: Path) -> dict[str, Any]:
    for attachment in event.get("attachments", []):
        if attachment.get("name") == "response_body.bin":
            try:
                payload = json.loads(
                    _read_attachment_bytes(root, str(attachment["object_path"])).decode(
                        "utf-8"
                    )
                )
            except (UnicodeError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _message_content(
    message: Mapping[str, Any], attachments: list[Mapping[str, Any]]
) -> Any:
    content = message.get("content", "")
    if isinstance(content, list):
        text_parts = [
            {"type": "text", "text": str(item.get("text") or "")}
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        ]
        return text_parts + _attachment_content_parts(attachments)
    return _atif_content(content, attachments)


def export_atif(
    manifest: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    root: Path,
) -> dict[str, Any]:
    """Convert the lossless log to the ADP ATIF-v1.7 interchange shape."""

    steps: list[dict[str, Any]] = []
    model_names: list[str] = []
    tool_names: set[str] = set()
    copied_context = False

    def add(step: dict[str, Any]) -> None:
        step["step_id"] = len(steps) + 1
        steps.append(step)

    for event in events:
        event_type = str(event.get("event_type") or "")
        timestamp = _timestamp(event.get("timestamp_epoch_ns"))
        if event_type == "task_context":
            add(
                {
                    "timestamp": timestamp,
                    "source": "user",
                    "message": _atif_content(
                        event.get("content", {}), event.get("attachments", [])
                    ),
                    "llm_call_count": 0,
                    "extra": {
                        "event_id": event.get("event_id"),
                        "stage": event.get("stage"),
                    },
                }
            )
        elif event_type == "api_request":
            payload = _api_payload(event, root)
            model = str(payload.get("model") or "")
            if model:
                model_names.append(model)
            for message in payload.get("messages", []):
                if not isinstance(message, Mapping):
                    continue
                role = str(message.get("role") or "user")
                source = "agent" if role == "assistant" else role
                if source not in {"system", "user", "agent"}:
                    source = "user"
                add(
                    {
                        "timestamp": timestamp,
                        "source": source,
                        "message": _message_content(
                            message, list(event.get("attachments", []))
                        ),
                        "model_name": model or None,
                        "llm_call_count": 0,
                        "is_copied_context": copied_context,
                        "extra": {
                            "api_stage": event.get("stage"),
                            "tool_call_id": event.get("tool_call_id"),
                            "request_payload_sha256": next(
                                (
                                    item.get("sha256")
                                    for item in event.get("attachments", [])
                                    if item.get("name") == "request_payload.json"
                                ),
                                None,
                            ),
                        },
                    }
                )
            copied_context = True
        elif event_type == "api_response":
            response = _api_response(event, root)
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") if isinstance(choice, Mapping) else {}
            message = message if isinstance(message, Mapping) else {}
            content = message.get("content") or ""
            reasoning = message.get("reasoning_content")
            usage = (
                response.get("usage")
                if isinstance(response.get("usage"), Mapping)
                else {}
            )
            add(
                {
                    "timestamp": timestamp,
                    "source": "agent",
                    "message": _atif_content(content),
                    "model_name": model_names[-1] if model_names else None,
                    "reasoning_effort": None,
                    "reasoning_content": reasoning
                    if isinstance(reasoning, str)
                    else None,
                    "metrics": {
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "cached_tokens": (
                            usage.get("prompt_tokens_details", {}).get("cached_tokens")
                            if isinstance(usage.get("prompt_tokens_details"), Mapping)
                            else None
                        ),
                        "cost": None,
                        "prompt_token_ids": None,
                        "completion_token_ids": None,
                        "logprobs": None,
                        "extra": {"raw_usage": dict(usage)},
                    },
                    "llm_call_count": 1,
                    "extra": {
                        "api_stage": event.get("stage"),
                        "tool_call_id": event.get("tool_call_id"),
                        "finish_reason": choice.get("finish_reason")
                        if isinstance(choice, Mapping)
                        else None,
                        "status": event.get("status"),
                    },
                }
            )
        elif event_type == "tool_call":
            tool_name = str(event.get("tool_name") or "tool")
            tool_names.add(tool_name)
            content = event.get("content")
            arguments = (
                content.get("input") if isinstance(content, Mapping) else content
            )
            if not isinstance(arguments, Mapping):
                arguments = {"value": arguments}
            add(
                {
                    "timestamp": timestamp,
                    "source": "agent",
                    "message": "",
                    "tool_calls": [
                        {
                            "tool_call_id": event.get("tool_call_id"),
                            "function_name": tool_name,
                            "arguments": dict(arguments),
                            "extra": {
                                "stage": event.get("stage"),
                                "attachment_sha256": [
                                    item.get("sha256")
                                    for item in event.get("attachments", [])
                                ],
                            },
                        }
                    ],
                    "llm_call_count": 0,
                    "extra": {"event_id": event.get("event_id")},
                }
            )
        elif event_type == "tool_result":
            add(
                {
                    "timestamp": timestamp,
                    "source": "agent",
                    "message": "",
                    "observation": {
                        "results": [
                            {
                                "source_call_id": event.get("tool_call_id"),
                                "content": _atif_content(
                                    event.get("content", {}),
                                    event.get("attachments", []),
                                ),
                                "subagent_trajectory_ref": None,
                                "extra": {
                                    "status": event.get("status"),
                                    "stage": event.get("stage"),
                                    "attachment_sha256": [
                                        item.get("sha256")
                                        for item in event.get("attachments", [])
                                    ],
                                },
                            }
                        ],
                        "extra": None,
                    },
                    "llm_call_count": 0,
                    "extra": {"event_id": event.get("event_id")},
                }
            )
        elif event_type == "outcome":
            add(
                {
                    "timestamp": timestamp,
                    "source": "agent",
                    "message": _atif_content(event.get("content", {})),
                    "llm_call_count": 0,
                    "extra": {"event_id": event.get("event_id"), "terminal": True},
                }
            )
    if not steps:
        raise TrajectoryError("cannot export an empty trajectory to ATIF")
    identity = (
        manifest.get("identity")
        if isinstance(manifest.get("identity"), Mapping)
        else {}
    )
    trajectory_id = str(
        identity.get("work_item_id") or identity.get("asset_id") or root.name
    )
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": str(identity.get("lease_id") or "") or None,
        "trajectory_id": trajectory_id,
        "agent": {
            "name": "video2blender",
            "version": "agent-trajectory-v1",
            "model_name": model_names[-1] if model_names else None,
            "tool_definitions": [
                {
                    "name": name,
                    "description": f"Video2Blender observable tool: {name}",
                    "parameters": {"type": "object", "additionalProperties": True},
                }
                for name in sorted(tool_names)
            ],
            "extra": {"model_names": list(dict.fromkeys(model_names))},
        },
        "steps": steps,
        "notes": "Observable inputs/outputs only; no fabricated hidden chain-of-thought.",
        "final_metrics": {"event_count": manifest.get("event_count")},
        "continued_trajectory_ref": None,
        "extra": {
            "identity": identity,
            "completeness": manifest.get("completeness"),
            "event_chain_head_sha256": manifest.get("event_chain_head_sha256"),
            "status": manifest.get("status"),
        },
        "subagent_trajectories": None,
    }


def export_sft(
    manifest: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    root: Path,
    *,
    validation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validation = dict(validation or validate_observable_trace(manifest, events, root))
    messages = []
    tools: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = event.get("event_type")
        if event_type == "task_context":
            messages.append({"role": "user", "content": event.get("content")})
        elif event_type == "api_request":
            payload = _api_payload(event, root)
            messages.extend(payload.get("messages", []))
        elif event_type == "api_response":
            response = _api_response(event, root)
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") if isinstance(choice, Mapping) else None
            if isinstance(message, Mapping):
                messages.append(dict(message))
        elif event_type == "tool_call":
            name = str(event.get("tool_name") or "tool")
            content = event.get("content")
            arguments = (
                content.get("input") if isinstance(content, Mapping) else content
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": event.get("tool_call_id"),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                }
            )
            tools[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Video2Blender observable tool: {name}",
                    "parameters": {"type": "object", "additionalProperties": True},
                },
            }
        elif event_type == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": event.get("tool_call_id"),
                    "content": json.dumps(event.get("content"), ensure_ascii=False),
                }
            )
    return {
        "schema": "video2blender.agent-trajectory-sft.v1",
        "messages": messages,
        "tools": [tools[name] for name in sorted(tools)],
        "assets": [
            {
                "path": str(path.relative_to(root)),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted((root / "objects").rglob("*"))
            if path.is_file()
        ],
        "metadata": {
            "identity": manifest.get("identity"),
            "completeness": manifest.get("completeness"),
            "status": manifest.get("status"),
            "event_chain_head_sha256": manifest.get("event_chain_head_sha256"),
            "trace_validation": validation,
            "eligible_for_sft": manifest.get("completeness")
            == "complete_observable_trace"
            and manifest.get("status") == "accepted"
            and bool(validation.get("hash_chain_verified"))
            and bool(validation.get("attachments_verified"))
            and bool(validation.get("call_pairs_complete"))
            and bool(validation.get("credential_scan_passed")),
        },
    }
