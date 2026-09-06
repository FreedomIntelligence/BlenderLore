#!/usr/bin/env python3
"""Validate a Video2Blender tutorial package as a closed evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore[assignment]

import tutorial_extraction_core as core


REQUIRED_FILES = (
    "tutorial.md",
    "tutorial_path_refs.md",
    "tutorial_visual_contract.json",
    "steps_verified.json",
    "steps_candidates.json",
    "tutorial_manifest.json",
    "evidence/index.json",
    "rich_evidence/windows.json",
    "transcript/segments.jsonl",
    "transcript/source.json",
    "ocr/observations.jsonl",
    "uncertain_items.json",
)
WORKSPACE_INPUTS = {
    "source.info.json",
    "target_reference.png",
    "final_reference.png",
    "source.mp4",
    "reproduction_run_manifest.json",
}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
FORBIDDEN_KEY_MARKERS = (
    "api_key",
    "authorization",
    "endpoint",
    "raw_response",
    "provider_response",
    "model_response",
    "secret",
    "bearer_token",
)
UNKNOWN_VALUES = {
    "",
    "unknown",
    "uncertain",
    "none",
    "n/a",
    "not visible",
    "未知",
    "不确定",
    "不可见",
    "看不清",
    "无法确认",
    "无法辨认",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REGIONS = {"full", "right_ui", "node_editor", "timeline", "properties"}
ROLES = {"pre", "action", "stable", "ocr_best"}


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, issues: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues.append(f"invalid JSON {path.relative_to(path.parents[1])}: {exc}")
        return {}


def inside_file(root: Path, raw: str, issues: list[str]) -> Path | None:
    if not raw or Path(raw).is_absolute() or "\\" in raw or ".." in Path(raw).parts:
        issues.append(f"path is not a normalized package-relative path: {raw}")
        return None
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        issues.append(f"path escapes tutorial package: {raw}")
        return None
    if not candidate.is_file():
        issues.append(f"referenced file is missing: {raw}")
        return None
    return candidate


def _not_unknown(value: Any) -> bool:
    return value is not None and str(value).strip().casefold() not in UNKNOWN_VALUES


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _model_identity_matches(requested: str, observed: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", observed.casefold())
    if requested == "gpt-5.6-sol":
        return "gpt56sol" in normalized
    if requested == "gpt-5.5":
        return "gpt55" in normalized and "gpt56" not in normalized
    return False


def _validate_schema(
    instance: Any, schema_path: Path, label: str, issues: list[str]
) -> None:
    if jsonschema is None:
        issues.append(
            "jsonschema dependency is unavailable; package validation cannot continue"
        )
        return
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        errors = sorted(
            validator.iter_errors(instance), key=lambda item: list(item.path)
        )
    except (OSError, UnicodeError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        issues.append(f"cannot load {label} schema: {exc}")
        return
    for error in errors[:30]:
        location = ".".join(str(item) for item in error.path) or "<root>"
        issues.append(f"{label} schema violation at {location}: {error.message}")


def _inspect_sensitive(value: Any, issues: list[str], location: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold()
            if normalized != "raw_model_responses_persisted" and any(
                marker in normalized for marker in FORBIDDEN_KEY_MARKERS
            ):
                issues.append(f"forbidden provider/secret field in {location}: {key}")
            _inspect_sensitive(child, issues, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _inspect_sensitive(child, issues, f"{location}[{index}]")
    elif isinstance(value, str) and "authorization: bearer" in value.casefold():
        issues.append(f"forbidden bearer material in {location}")


def _read_jsonl(path: Path, label: str, issues: list[str]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"invalid {label} JSONL line {number}: {exc}")
            continue
        if not isinstance(value, Mapping):
            issues.append(f"{label} JSONL line {number} is not an object")
            continue
        _inspect_sensitive(value, issues, f"{label}[{number}]")
        rows.append(value)
    return rows


def validate_package(path: Path, *, allow_workspace_source: bool = False) -> list[str]:
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError:
        return [f"tutorial package does not exist: {path}"]
    if not root.is_dir():
        return ["tutorial package path is not a directory"]
    manifest_path = root / "tutorial_manifest.json"
    if manifest_path.is_file():
        try:
            schema = json.loads(manifest_path.read_text(encoding="utf-8")).get("schema")
        except (ValueError, AttributeError):
            schema = None
        if schema == "video2blender-visual-tutorial.v1":
            from visual_tutorial_pipeline import validate_workspace

            return validate_workspace(root)
        if schema == "video2blender-legacy-rich-tutorial.v1":
            from legacy_rich_tutorial_pipeline import validate_workspace

            return validate_workspace(root)
    issues: list[str] = []
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            issues.append(f"required file is missing: {relative}")
    if issues:
        return sorted(set(issues))

    all_files = {
        item.relative_to(root).as_posix(): item
        for item in root.rglob("*")
        if item.is_file()
    }
    allowed_inputs = WORKSPACE_INPUTS if allow_workspace_source else set()
    for relative, item in all_files.items():
        if item.suffix.casefold() in VIDEO_SUFFIXES and not (
            allow_workspace_source and relative == "source.mp4"
        ):
            issues.append(f"forbidden source/video artifact is present: {relative}")
        lowered = item.name.casefold()
        if any(marker in lowered for marker in FORBIDDEN_KEY_MARKERS):
            issues.append(f"forbidden raw/secret artifact is present: {relative}")

    manifest = read_json(root / "tutorial_manifest.json", issues)
    verified = read_json(root / "steps_verified.json", issues)
    candidates_doc = read_json(root / "steps_candidates.json", issues)
    evidence_doc = read_json(root / "evidence/index.json", issues)
    transcript_source = read_json(root / "transcript/source.json", issues)
    uncertain_doc = read_json(root / "uncertain_items.json", issues)
    windows_doc = read_json(root / "rich_evidence/windows.json", issues)
    visual_doc = read_json(root / "tutorial_visual_contract.json", issues)
    json_docs = {
        "manifest": manifest,
        "verified": verified,
        "candidates": candidates_doc,
        "evidence": evidence_doc,
        "transcript": transcript_source,
        "uncertain": uncertain_doc,
        "windows": windows_doc,
        "visual": visual_doc,
    }
    for label, value in json_docs.items():
        if not isinstance(value, (Mapping, list)):
            issues.append(f"{label} JSON root has an invalid type")
        _inspect_sensitive(value, issues, label)

    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    _validate_schema(manifest, schema_root / "manifest.schema.json", "manifest", issues)
    _validate_schema(
        verified, schema_root / "steps.schema.json", "verified steps", issues
    )
    requested_model = (
        str(manifest.get("model") or "") if isinstance(manifest, Mapping) else ""
    )
    provider = (
        str(manifest.get("provider") or "") if isinstance(manifest, Mapping) else ""
    )
    fallback_reason = (
        str(manifest.get("fallback_reason") or "").strip()
        if isinstance(manifest, Mapping)
        else ""
    )
    if provider not in core.ALLOWED_PROVIDERS:
        issues.append("manifest provider must be api or codex-cli")
    if requested_model == "gpt-5.5" and not fallback_reason:
        issues.append("gpt-5.5 manifest requires a non-empty fallback_reason")
    if requested_model == "gpt-5.6-sol" and fallback_reason:
        issues.append("gpt-5.6-sol manifest forbids fallback_reason")
    try:
        core.validate_model_fallback(requested_model, fallback_reason)
    except core.ExtractionError as exc:
        issues.append(f"manifest model fallback gate failed: {exc}")
    source = (
        manifest.get("source")
        if isinstance(manifest, Mapping) and isinstance(manifest.get("source"), Mapping)
        else {}
    )
    source_duration = source.get("duration_seconds")
    if not _finite_number(source_duration) or float(source_duration) <= 0:
        issues.append("manifest source duration_seconds must be positive and finite")
        source_duration_value: float | None = None
    else:
        source_duration_value = float(source_duration)

    def after_video_end(value: Any) -> bool:
        return (
            source_duration_value is not None
            and _finite_number(value)
            and float(value) > source_duration_value
        )

    usage = manifest.get("model_usage") if isinstance(manifest, Mapping) else None
    if not isinstance(usage, Mapping):
        issues.append("manifest model_usage must be an object")
    else:
        calls, reported = usage.get("calls"), usage.get("reported_calls")
        if (
            isinstance(calls, bool)
            or not isinstance(calls, int)
            or calls < 1
            or reported != calls
        ):
            issues.append("every model call must have exactly one usage receipt")
        if not _model_identity_matches(
            requested_model, str(usage.get("response_model") or "")
        ):
            issues.append(
                "model usage response identity does not match requested model family"
            )
        if usage.get("finish_reason") != "stop":
            issues.append("model usage does not contain a clean stop finish reason")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                issues.append(f"model usage {key} must be a positive integer")
        if all(
            isinstance(usage.get(key), int)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        ) and usage.get("total_tokens", 0) < usage.get("prompt_tokens", 0) + usage.get(
            "completion_tokens", 0
        ):
            issues.append("model usage token totals are inconsistent")

    if (
        not isinstance(transcript_source, Mapping)
        or transcript_source.get("schema")
        != "video2blender-tutorial-transcript-source.v2"
    ):
        issues.append("transcript source schema is not v2")
    transcript_rows = _read_jsonl(
        root / "transcript/segments.jsonl", "transcript", issues
    )
    for row in transcript_rows:
        start, end, text = (
            row.get("start_sec"),
            row.get("end_sec"),
            str(row.get("text") or "").strip(),
        )
        if (
            not _finite_number(start)
            or not _finite_number(end)
            or float(end) < float(start)
        ):
            issues.append("transcript segment has an invalid time range")
        elif after_video_end(end):
            issues.append("transcript segment exceeds source video duration")
        if not text:
            issues.append("transcript segment text is empty")
    if isinstance(transcript_source, Mapping):
        if transcript_source.get("segment_count") != len(transcript_rows):
            issues.append("transcript segment_count does not match segments.jsonl")
        if (
            transcript_source.get("status") == "unavailable"
            and not str(transcript_source.get("warning") or "").strip()
        ):
            issues.append("unavailable transcript must have an explicit warning")

    ocr_rows = _read_jsonl(root / "ocr/observations.jsonl", "ocr", issues)
    for row in ocr_rows:
        if (
            not _finite_number(row.get("timestamp_sec"))
            or float(row.get("timestamp_sec", -1)) < 0
        ):
            issues.append("OCR observation has an invalid timestamp")
        elif after_video_end(row.get("timestamp_sec")):
            issues.append("OCR observation exceeds source video duration")
        if row.get("region") not in REGIONS:
            issues.append("OCR observation has an invalid region")
        if not str(row.get("text") or "").strip():
            issues.append("OCR observation text is empty")

    if (
        not isinstance(uncertain_doc, Mapping)
        or uncertain_doc.get("schema") != "video2blender-tutorial-uncertain.v2"
        or not isinstance(uncertain_doc.get("items"), list)
    ):
        issues.append("uncertain_items.json has an invalid v2 structure")
    uncertain_items = (
        uncertain_doc.get("items")
        if isinstance(uncertain_doc, Mapping)
        and isinstance(uncertain_doc.get("items"), list)
        else []
    )

    images = evidence_doc.get("images") if isinstance(evidence_doc, Mapping) else None
    if (
        not isinstance(evidence_doc, Mapping)
        or evidence_doc.get("schema") != "video2blender-tutorial-evidence.v2"
        or not isinstance(images, list)
    ):
        issues.append("evidence index has an invalid v2 structure")
        images = []
    evidence_by_id: dict[str, Mapping[str, Any]] = {}
    for item in images:
        if not isinstance(item, Mapping):
            issues.append("evidence item must be an object")
            continue
        required = {
            "image_id",
            "step_id",
            "timestamp_sec",
            "role",
            "region",
            "path",
            "sha256",
        }
        if not required.issubset(item):
            issues.append("evidence item is missing required fields")
            continue
        image_id = str(item.get("image_id") or "")
        if not image_id or image_id in evidence_by_id:
            issues.append(f"missing or duplicate evidence ID: {image_id}")
            continue
        evidence_by_id[image_id] = item
        if (
            not _finite_number(item.get("timestamp_sec"))
            or float(item.get("timestamp_sec", -1)) < 0
        ):
            issues.append(f"evidence has an invalid timestamp: {image_id}")
        elif after_video_end(item.get("timestamp_sec")):
            issues.append(f"evidence exceeds source video duration: {image_id}")
        if item.get("role") not in ROLES or item.get("region") not in REGIONS:
            issues.append(f"evidence has an invalid role/region: {image_id}")
        if not SHA256_RE.fullmatch(str(item.get("sha256") or "")):
            issues.append(f"evidence has an invalid SHA-256: {image_id}")
        image_path = inside_file(root, str(item.get("path") or ""), issues)
        if image_path is not None:
            if sha256_path(image_path) != item.get("sha256"):
                issues.append(f"evidence hash mismatch: {image_id}")
            try:
                with Image.open(image_path) as value:
                    value.verify()
            except Exception:
                issues.append(f"evidence image is not decodable: {image_id}")

    candidate_steps = (
        candidates_doc.get("steps") if isinstance(candidates_doc, Mapping) else None
    )
    if (
        not isinstance(candidates_doc, Mapping)
        or candidates_doc.get("schema") != core.SCHEMA_CANDIDATES
        or not isinstance(candidate_steps, list)
    ):
        issues.append("candidate steps document has an invalid v2 structure")
        candidate_steps = []
    dropped_uncertainty: dict[str, list[str]] = {}
    for item in uncertain_items:
        if not isinstance(item, Mapping):
            continue
        if item.get("stage") != "qgate" or item.get("code") != "dropped_claim":
            continue
        step_id = str(item.get("step_id") or "")
        detail = str(item.get("detail") or "")
        if not step_id or not detail.startswith("dropped_claim:"):
            issues.append("dropped-claim uncertainty has an invalid receipt")
            continue
        dropped_uncertainty.setdefault(step_id, []).append(detail)
    receipt_drops: dict[str, list[str]] = {}
    for item in candidate_steps:
        gate = item.get("gate") if isinstance(item, Mapping) else None
        if (
            not isinstance(gate, Mapping)
            or gate.get("status")
            not in {"accepted", "accepted_with_dropped_claims", "uncertain"}
            or not isinstance(gate.get("reasons"), list)
        ):
            issues.append("candidate lacks a valid Q-Gate receipt")
            continue
        step_id = str(item.get("step_id") or "")
        status = str(gate.get("status") or "")
        reasons = gate.get("reasons") or []
        if status == "accepted" and reasons:
            issues.append(f"accepted candidate has nonempty Q-Gate reasons: {step_id}")
        if status == "accepted_with_dropped_claims":
            if not reasons or any(
                not isinstance(reason, str) or not reason.startswith("dropped_claim:")
                for reason in reasons
            ):
                issues.append(
                    f"partially accepted candidate lacks dropped-claim reasons: {step_id}"
                )
            receipt_drops[step_id] = [str(reason) for reason in reasons]
            if sorted(receipt_drops[step_id]) != sorted(
                dropped_uncertainty.get(step_id, [])
            ):
                issues.append(
                    f"dropped-claim receipt is not mirrored in uncertainty: {step_id}"
                )
    for step_id in sorted(set(dropped_uncertainty) - set(receipt_drops)):
        issues.append(
            f"orphan dropped-claim uncertainty has no partial-acceptance receipt: {step_id}"
        )

    steps = verified.get("steps") if isinstance(verified, Mapping) else None
    if not isinstance(steps, list):
        issues.append("verified steps must be an array")
        steps = []
    evidence_owner: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        step_id = str(step.get("step_id") or "")
        evidence_ids = [str(value) for value in step.get("evidence_ids") or []]
        evidence_roles = {
            str(evidence_by_id[evidence_id].get("role") or "")
            for evidence_id in evidence_ids
            if evidence_id in evidence_by_id
        }
        if evidence_roles != ROLES:
            issues.append(
                f"verified step must retain pre/action/stable/ocr_best evidence: {step_id}"
            )
        role_items = {
            str(evidence_by_id[evidence_id].get("role") or ""): evidence_by_id[
                evidence_id
            ]
            for evidence_id in evidence_ids
            if evidence_id in evidence_by_id
        }
        if {"pre", "action", "stable"}.issubset(role_items):
            pre_item = role_items["pre"]
            action_item = role_items["action"]
            stable_item = role_items["stable"]
            pre_time = float(pre_item.get("timestamp_sec") or 0)
            action_time = float(action_item.get("timestamp_sec") or 0)
            stable_time = float(stable_item.get("timestamp_sec") or 0)
            if not (pre_time < action_time < stable_time):
                issues.append(
                    f"verified evidence must satisfy pre < action < stable: {step_id}"
                )
            if str(action_item.get("sha256") or "") == str(
                stable_item.get("sha256") or ""
            ):
                issues.append(
                    f"verified action and stable evidence must differ: {step_id}"
                )
        start_sec, end_sec = step.get("start_sec"), step.get("end_sec")
        if (
            not _finite_number(start_sec)
            or not _finite_number(end_sec)
            or float(start_sec) < 0
            or float(end_sec) < float(start_sec)
        ):
            issues.append(f"verified step has an invalid time range: {step_id}")
        elif after_video_end(end_sec):
            issues.append(f"verified step exceeds source video duration: {step_id}")
        for evidence_id in evidence_ids:
            if evidence_id not in evidence_by_id:
                issues.append(
                    f"verified step cites missing evidence: {step_id}:{evidence_id}"
                )
            owner = evidence_owner.setdefault(evidence_id, step_id)
            if owner != step_id:
                issues.append(
                    f"evidence is shared across verified steps: {evidence_id}"
                )
        claims = step.get("claims") if isinstance(step.get("claims"), list) else []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            claim_ids = [str(value) for value in claim.get("evidence_ids") or []]
            if any(value not in evidence_by_id for value in claim_ids):
                issues.append(f"verified claim cites missing evidence: {step_id}")
            if any(value not in evidence_ids for value in claim_ids):
                issues.append(f"verified claim uses cross-step evidence: {step_id}")
            if not any(
                value in evidence_by_id
                and evidence_by_id[value].get("role")
                in {"action", "stable", "ocr_best"}
                for value in claim_ids
            ):
                issues.append(f"verified claim lacks action/post evidence: {step_id}")

        def claim_matches(
            kind: str, field: str, value: Any, parameter: str = ""
        ) -> bool:
            return any(
                isinstance(claim, Mapping)
                and claim.get("kind") == kind
                and claim.get("field") == field
                and str(claim.get("parameter") or "") == parameter
                and core.canonical_sha256(claim.get("value"))
                == core.canonical_sha256(value)
                and bool(claim.get("evidence_ids"))
                for claim in claims
            )

        closing_action_claim = any(
            isinstance(claim, Mapping)
            and claim.get("kind") == "action"
            and claim.get("field") == "action"
            and str(claim.get("parameter") or "") == ""
            and core.canonical_sha256(claim.get("value"))
            == core.canonical_sha256(step.get("action"))
            and {"action", "stable"}.issubset(
                {
                    str(evidence_by_id[evidence_id].get("role") or "")
                    for evidence_id in claim.get("evidence_ids") or []
                    if evidence_id in evidence_by_id
                }
            )
            for claim in claims
        )
        if not closing_action_claim:
            issues.append(f"verified action lacks an exact claim: {step_id}")
        if str(step.get("object") or "") and not claim_matches(
            "object", "object", step.get("object")
        ):
            issues.append(f"verified object lacks an exact claim: {step_id}")
        parameters = (
            step.get("parameters")
            if isinstance(step.get("parameters"), Mapping)
            else {}
        )
        for key, value in parameters.items():
            if not _not_unknown(value):
                issues.append(
                    f"verified parameter contains an unknown value: {step_id}:{key}"
                )
            elif not claim_matches("parameter", "parameters", value, str(key)):
                issues.append(
                    f"verified parameter lacks an exact claim: {step_id}:{key}"
                )
        relation_type = str(step.get("relation_type") or "")
        relation = str(step.get("spatial_relation") or "")
        if relation_type != "none" and not relation:
            issues.append(f"verified relationship has no spatial_relation: {step_id}")
        if relation and not claim_matches("connection", "spatial_relation", relation):
            issues.append(
                f"verified relationship lacks an exact connection claim: {step_id}"
            )
        for field, kind in (
            ("visual_result", "visual_result"),
            ("material_color", "material_color"),
            ("surface_detail", "surface_detail"),
            ("implementation_notes", "implementation_note"),
        ):
            value = str(step.get(field) or "")
            if value and not claim_matches(kind, field, value):
                issues.append(f"verified {field} lacks an exact claim: {step_id}")

        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            kind = str(claim.get("kind") or "")
            field = str(claim.get("field") or "")
            parameter = str(claim.get("parameter") or "")
            value = claim.get("value")
            supports_promoted_field = (
                (
                    kind == "action"
                    and field == "action"
                    and not parameter
                    and core.canonical_sha256(value)
                    == core.canonical_sha256(step.get("action"))
                )
                or (
                    kind == "object"
                    and field == "object"
                    and not parameter
                    and bool(str(step.get("object") or ""))
                    and core.canonical_sha256(value)
                    == core.canonical_sha256(step.get("object"))
                )
                or (
                    kind == "parameter"
                    and field == "parameters"
                    and parameter in parameters
                    and core.canonical_sha256(value)
                    == core.canonical_sha256(parameters.get(parameter))
                )
                or (
                    kind == "connection"
                    and field == "spatial_relation"
                    and not parameter
                    and relation_type != "none"
                    and bool(relation)
                    and core.canonical_sha256(value) == core.canonical_sha256(relation)
                )
                or any(
                    kind == optional_kind
                    and field == optional_field
                    and not parameter
                    and bool(str(step.get(optional_field) or ""))
                    and core.canonical_sha256(value)
                    == core.canonical_sha256(step.get(optional_field))
                    for optional_field, optional_kind in (
                        ("visual_result", "visual_result"),
                        ("material_color", "material_color"),
                        ("surface_detail", "surface_detail"),
                        ("implementation_notes", "implementation_note"),
                    )
                )
            )
            if not supports_promoted_field:
                issues.append(
                    f"verified claim does not support a promoted field: {step_id}:{kind}:{field}"
                )

    title = str(manifest.get("title") or "") if isinstance(manifest, Mapping) else ""
    expected_markdown = core.tutorial_markdown(
        title, str(source.get("url") or ""), steps, evidence_by_id
    )
    tutorial = (root / "tutorial.md").read_text(encoding="utf-8")
    if tutorial != expected_markdown:
        issues.append(
            "tutorial.md is not the exact deterministic projection of verified steps"
        )
    if (root / "tutorial_path_refs.md").read_bytes() != (
        root / "tutorial.md"
    ).read_bytes():
        issues.append("tutorial_path_refs.md must byte-match tutorial.md")
    if visual_doc != core.tutorial_visual_contract(steps):
        issues.append(
            "tutorial_visual_contract.json is not the deterministic verified projection"
        )

    if not isinstance(windows_doc, list):
        issues.append("rich_evidence/windows.json must be an array")
        windows_doc = []
    for index, item in enumerate(windows_doc):
        if (
            not isinstance(item, Mapping)
            or item.get("window_index") != index
            or not _finite_number(item.get("start_sec"))
            or not _finite_number(item.get("end_sec"))
            or float(item.get("end_sec", 0)) < float(item.get("start_sec", 0))
        ):
            issues.append(f"rich evidence window is invalid at index {index}")
            continue
        if float(item.get("start_sec", 0)) < 0 or after_video_end(item.get("end_sec")):
            issues.append(
                f"rich evidence window exceeds source video duration at index {index}"
            )
        inside_file(root, str(item.get("sheet") or ""), issues)

    counts = manifest.get("counts") if isinstance(manifest, Mapping) else None
    if not isinstance(counts, Mapping):
        issues.append("manifest counts must be an object")
    else:
        expected_counts = {
            "windows": len(windows_doc),
            "candidates": len(candidate_steps),
            "verified_steps": len(steps),
            "uncertain_items": len(uncertain_items),
            "evidence_images": len(images),
        }
        for key, expected in expected_counts.items():
            if counts.get(key) != expected:
                issues.append(f"manifest count mismatch: {key}")
        batches = counts.get("verification_batches")
        if isinstance(batches, bool) or not isinstance(batches, int) or batches < 0:
            issues.append("manifest verification_batches is invalid")
    warnings = manifest.get("warnings") if isinstance(manifest, Mapping) else None
    if not isinstance(warnings, list) or any(
        not isinstance(value, str) or not value for value in warnings
    ):
        issues.append("manifest warnings must be an array of nonempty strings")
        warnings = []
    expected_status = (
        "complete_with_warnings" if warnings or uncertain_items else "complete"
    )
    if isinstance(manifest, Mapping) and manifest.get("status") != expected_status:
        issues.append("manifest status does not match warnings/uncertain items")

    outputs = manifest.get("outputs") if isinstance(manifest, Mapping) else None
    actual_outputs = set(all_files) - {"tutorial_manifest.json"} - allowed_inputs
    if not isinstance(outputs, Mapping):
        issues.append("manifest outputs must be an object")
    else:
        declared = {str(key) for key in outputs}
        if declared != actual_outputs:
            missing = sorted(actual_outputs - declared)
            extra = sorted(declared - actual_outputs)
            issues.append(
                f"manifest outputs are not exhaustive (missing={missing}, extra={extra})"
            )
        for raw, expected in outputs.items():
            if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
                issues.append(f"manifest output hash is not SHA-256: {raw}")
                continue
            target = inside_file(root, str(raw), issues)
            if target is not None and sha256_path(target) != expected:
                issues.append(f"manifest output hash mismatch: {raw}")
    return sorted(set(issues))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-workspace-source", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    issues = validate_package(
        args.package, allow_workspace_source=args.allow_workspace_source
    )
    if args.json:
        print(
            json.dumps(
                {"status": "fail" if issues else "pass", "issues": issues},
                ensure_ascii=False,
                indent=2,
            )
        )
    elif issues:
        for issue in issues:
            print(f"error: {issue}", file=sys.stderr)
    else:
        print("PASS: tutorial package")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
