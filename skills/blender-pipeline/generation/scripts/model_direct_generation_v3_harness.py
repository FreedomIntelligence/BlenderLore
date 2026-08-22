#!/usr/bin/env python3
"""Fail-closed execution and Joint@80 acceptance harness for GEN-V3.

Each attempt makes one model call that yields one canonical ``generate.py``.
The trusted harness executes it from a canonical blank scene, saves and freshly
reopens ``asset.blend``, renders generation-specific evidence, then obtains
generation Rubric and VLM judgements.  Attempts are immutable and capped at
three; a third Joint@80 failure produces a replacement-required receipt.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

# Blender executes ``--python`` files without reliably placing their containing
# directory on ``sys.path``.  Bind sibling contract imports explicitly so the
# same trusted entrypoint works both under CPython and inside Blender.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import model_direct_generation_contracts as generation_contracts


TASK_INDEX_SCHEMA = "video2blender.model-direct-generation-index.v3"
ATTEMPT_RECEIPT_SCHEMA = "video2blender.model-direct-generation-attempt.v3"
TASK_RECEIPT_SCHEMA = "video2blender.model-direct-generation-task-result.v3"
BATCH_RECEIPT_SCHEMA = "video2blender.model-direct-generation-batch-result.v3"
MANUAL_QA_RECEIPT_SCHEMA = "video2blender.model-direct-generation-manual-qa.v1"
MANUAL_DISQUALIFY_RECEIPT_SCHEMA = (
    "video2blender.model-direct-generation-manual-disqualify.v1"
)
RUBRIC_SCHEMA = "video2blender.model-direct-generation-rubric.v1"
VLM_SCHEMA = "video2blender.model-direct-generation-vlm.v1"
SCENE_REPORT_SCHEMA = "video2blender.model-direct-generation-scene-report.v2"
CODEGEN_PROMPT_VERSION = "model-direct-generation-v3-codegen.v5"
JUDGE_PROMPT_VERSION = "model-direct-generation-v3-judge.v2"
KNOWLEDGE_VERSION = "model-direct-generation-v3-harness.v4"
CODEGEN_STAGE = "model_direct_generation_v3_codegen"
JUDGE_STAGE = "model_direct_generation_v3_judge"
CODE_CONTRACT_RETRY_SUFFIX = "code-contract-retry-1"
EXPECTED_TASK_COUNT = 50
MAX_ATTEMPTS = 3
DEFAULT_THRESHOLD = 80.0
ACCEPTANCE_SCOPE = "web_delivery_joint_at_80_candidate"
RUBRIC_WEIGHTS = {
    "requirement": 0.45,
    "structure": 0.25,
    "validity": 0.15,
    "editability": 0.15,
}
VLM_WEIGHTS = {
    "fidelity": 0.50,
    "quality": 0.25,
    "coherence": 0.25,
}
HARD_GATE_KEYS = (
    "execution_success",
    "asset_saved",
    "fresh_reopen",
    "evidence_complete",
    "no_external_dependencies",
)
EVIDENCE_NAMES = (
    "presentation.png",
    "clay.png",
    "normal.png",
    "turntable/frame_000.png",
    "turntable/frame_001.png",
    "turntable/frame_002.png",
    "turntable/frame_003.png",
    "turntable/frame_004.png",
    "turntable/frame_005.png",
    "turntable/frame_006.png",
    "turntable/frame_007.png",
)
SAFE_ID_RE = re.compile(r"GEN-V3-[0-9]{3}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
VISUAL_CONTRACT_KEYS = (
    "hero_face",
    "must_have_parts",
    "silhouette",
    "material_targets",
    "forbid_surface_traits",
)
HERO_FACE_DIRECTIONS = {
    "front": (0.0, -1.0, 0.35),
    "front_right": (1.7, -1.9, 1.35),
    "front_left": (-1.7, -1.9, 1.35),
    "top_front": (0.0, -1.2, 2.2),
    "top_front_right": (1.35, -1.55, 2.0),
    "top_front_left": (-1.35, -1.55, 2.0),
}
SIMPLE_SCENE_EVIDENCE_EXPOSURE = -1.25
TRUSTED_SRGB_HELPER_NAME = "srgb_to_scene_linear_rgba"
DEFAULT_SIMPLE_PRODUCT_FORBIDDEN_NODES = frozenset(
    {"ShaderNodeTexNoise", "ShaderNodeBump", "ShaderNodeDisplacement"}
)
TRUSTED_HARNESS_OWNED_BPY_CALLS = frozenset(
    {
        "bpy.data.fonts.load",
        "bpy.data.images.load",
        "bpy.data.libraries.load",
        "bpy.data.libraries.write",
        "bpy.ops.render.render",
        "bpy.ops.wm.alembic_import",
        "bpy.ops.wm.alembic_export",
        "bpy.ops.wm.append",
        "bpy.ops.wm.link",
        "bpy.ops.wm.open_mainfile",
        "bpy.ops.wm.recover_auto_save",
        "bpy.ops.wm.save_as_mainfile",
        "bpy.ops.wm.save_mainfile",
        "bpy.ops.wm.usd_import",
        "bpy.ops.wm.usd_export",
    }
)


class GenerationV3HarnessError(RuntimeError):
    """The GEN-V3 execution or evidence contract failed closed."""


class GenerationV3ProviderUnavailable(GenerationV3HarnessError):
    """A transient provider failure that must not consume a task attempt."""


class GenerationV3DeliveryUnknown(GenerationV3HarnessError):
    """A possibly delivered paid call that is permanently ineligible to resend."""


class GenerationV3ArtifactRecoveryRequired(GenerationV3HarnessError):
    """Immutable attempt artifacts must be restored before judge replay."""


class GenerationV3CodeContractExhausted(GenerationV3HarnessError):
    """Both bounded code-contract responses were invalid; no quality attempt was spent."""


@dataclass(frozen=True)
class GenerationTask:
    task_id: str
    title: str
    instruction: str
    category: str
    visual_contract: Mapping[str, Any]
    reference_path: Path
    reference_sha256: str
    source_payload_sha256: str

    def model_payload(self) -> dict[str, Any]:
        base = generation_contracts.validate_sanitized_task_payload(
            {
                "task_id": self.task_id,
                "title": self.title,
                "instruction": self.instruction,
                "category": self.category,
            }
        )
        return {
            **base,
            "visual_contract": normalize_visual_contract(self.visual_contract),
        }


@dataclass(frozen=True)
class GeneratedProgram:
    source: str
    logical_call_id: str
    response_replayed: bool = False


class CodeGenerator(Protocol):
    def __call__(
        self,
        task: GenerationTask,
        reference_path: Path,
        attempt: int,
        feedback: Mapping[str, Any] | None,
    ) -> GeneratedProgram: ...


class BlenderExecutor(Protocol):
    def __call__(
        self,
        *,
        blank_blend: Path,
        generate_py: Path,
        task_json: Path,
        attempt_dir: Path,
    ) -> Mapping[str, Any]: ...


class GenerationScorer(Protocol):
    def __call__(
        self,
        task: GenerationTask,
        reference_path: Path,
        attempt: int,
        artifact_bindings: Mapping[str, Any],
        scene_report: Mapping[str, Any],
        source_analysis: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]: ...


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value).rstrip(b"\n")).hexdigest()


def sha256_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _regular_file(path: Path, label: str, *, minimum_size: int = 1) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise GenerationV3HarnessError(f"{label} must be a regular non-symlink file")
    if resolved.stat().st_size < minimum_size:
        raise GenerationV3HarnessError(f"{label} is empty or truncated")
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationV3HarnessError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise GenerationV3HarnessError(f"{label} must be a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(dict(value)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_once(path: Path, data: bytes, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise GenerationV3HarnessError(f"refusing to overwrite {label}") from exc


def _receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["receipt_sha256"] = digest(result)
    return result


def _validate_receipt(value: Mapping[str, Any], schema: str) -> dict[str, Any]:
    if value.get("schema") != schema:
        raise GenerationV3HarnessError(
            f"unexpected receipt schema: {value.get('schema')!r}"
        )
    supplied = str(value.get("receipt_sha256") or "")
    body = dict(value)
    body.pop("receipt_sha256", None)
    if not SHA256_RE.fullmatch(supplied) or digest(body) != supplied:
        raise GenerationV3HarnessError("receipt self-hash drifted")
    return dict(value)


def _reference_from_task(
    task: Mapping[str, Any], *, index_path: Path, reference_root: Path | None
) -> Path:
    preview = task.get("preview")
    if not isinstance(preview, Mapping):
        raise GenerationV3HarnessError(f"{task.get('task_id')} lacks preview media")
    raw_path = str(preview.get("path") or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = index_path.parent / path
        return _regular_file(path, "reference preview")
    raw_url = str(preview.get("url") or "").strip()
    parsed = urlparse(raw_url)
    if not raw_url or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise GenerationV3HarnessError(
            f"{task.get('task_id')} preview URL is not local"
        )
    relative = Path(unquote(parsed.path))
    candidates: list[Path] = []
    if reference_root is not None:
        candidates.append(reference_root / relative.name)
    # generation_tasks.json lives in web/data while URLs are relative to web/.
    candidates.append(index_path.parent.parent / relative)
    for candidate in candidates:
        try:
            return _regular_file(candidate, "reference preview")
        except (FileNotFoundError, GenerationV3HarnessError):
            continue
    raise GenerationV3HarnessError(
        f"{task.get('task_id')} reference preview is unavailable"
    )


def _nonempty_string_list(value: Any, *, label: str, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise GenerationV3HarnessError(f"visual_contract.{label} must be a string list")
    normalized = [str(item or "").strip() for item in value]
    if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
        raise GenerationV3HarnessError(
            f"visual_contract.{label} contains empty or duplicate values"
        )
    return normalized


def normalize_visual_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(VISUAL_CONTRACT_KEYS):
        raise GenerationV3HarnessError("task visual_contract fields drifted")
    hero_face = str(value.get("hero_face") or "").strip()
    silhouette = str(value.get("silhouette") or "").strip()
    if hero_face not in HERO_FACE_DIRECTIONS or not silhouette:
        raise GenerationV3HarnessError(
            "task visual_contract hero/silhouette is invalid"
        )
    return {
        "hero_face": hero_face,
        "must_have_parts": _nonempty_string_list(
            value.get("must_have_parts"), label="must_have_parts", allow_empty=False
        ),
        "silhouette": silhouette,
        "material_targets": _nonempty_string_list(
            value.get("material_targets"), label="material_targets", allow_empty=False
        ),
        "forbid_surface_traits": _nonempty_string_list(
            value.get("forbid_surface_traits"),
            label="forbid_surface_traits",
            allow_empty=True,
        ),
    }


def load_generation_index(
    index_path: Path,
    *,
    reference_root: Path | None = None,
    expected_count: int = EXPECTED_TASK_COUNT,
) -> list[GenerationTask]:
    index_path = _regular_file(index_path, "generation task index")
    payload = _read_json(index_path, "generation task index")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != expected_count:
        raise GenerationV3HarnessError(
            f"generation index must contain exactly {expected_count} tasks"
        )
    if payload.get("schema") not in {None, TASK_INDEX_SCHEMA}:
        raise GenerationV3HarnessError("generation index schema drifted")
    expected_ids = [f"GEN-V3-{index:03d}" for index in range(1, expected_count + 1)]
    if [
        str(item.get("task_id") or "") for item in tasks if isinstance(item, Mapping)
    ] != expected_ids:
        raise GenerationV3HarnessError("generation index IDs/order drifted")
    result: list[GenerationTask] = []
    for raw in tasks:
        if not isinstance(raw, Mapping):
            raise GenerationV3HarnessError("generation task must be an object")
        task_id = str(raw.get("task_id") or "")
        title = str(raw.get("title") or "").strip()
        instruction = str(
            raw.get("instruction_zh") or raw.get("instruction") or ""
        ).strip()
        category = str(raw.get("category") or raw.get("track_type") or "").strip()
        if (
            not SAFE_ID_RE.fullmatch(task_id)
            or not title
            or not instruction
            or not category
        ):
            raise GenerationV3HarnessError(
                f"{task_id or '<missing>'} task fields are invalid"
            )
        reference = _reference_from_task(
            raw, index_path=index_path, reference_root=reference_root
        )
        expected_sha = str((raw.get("preview") or {}).get("sha256") or "").lower()
        actual_sha = sha256_file(reference)
        if expected_sha and (
            not SHA256_RE.fullmatch(expected_sha) or expected_sha != actual_sha
        ):
            raise GenerationV3HarnessError(f"{task_id} reference SHA-256 drifted")
        task = GenerationTask(
            task_id=task_id,
            title=title,
            instruction=instruction,
            category=category,
            visual_contract=normalize_visual_contract(raw.get("visual_contract")),
            reference_path=reference,
            reference_sha256=actual_sha,
            source_payload_sha256=digest(dict(raw)),
        )
        task.model_payload()
        result.append(task)
    return result


def _score_component(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"score", "rationale"}:
        raise GenerationV3HarnessError(f"{label} must contain exactly score/rationale")
    try:
        score = float(value["score"])
    except (TypeError, ValueError) as exc:
        raise GenerationV3HarnessError(f"{label} score is invalid") from exc
    rationale = str(value["rationale"] or "").strip()
    if not math.isfinite(score) or not 0.0 <= score <= 100.0 or not rationale:
        raise GenerationV3HarnessError(f"{label} score/rationale is invalid")
    return {"score": score, "rationale": rationale}


def validate_rubric(
    value: Mapping[str, Any], *, task_id: str, attempt: int
) -> dict[str, Any]:
    required = {"schema", "task_id", "attempt", "components", "critical_criteria"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise GenerationV3HarnessError("generation Rubric payload shape drifted")
    if value.get("schema") != RUBRIC_SCHEMA or value.get("task_id") != task_id:
        raise GenerationV3HarnessError("generation Rubric identity drifted")
    if int(value.get("attempt") or 0) != attempt:
        raise GenerationV3HarnessError("generation Rubric attempt drifted")
    components = value.get("components")
    if not isinstance(components, Mapping) or set(components) != set(RUBRIC_WEIGHTS):
        raise GenerationV3HarnessError("generation Rubric components drifted")
    normalized_components = {
        name: _score_component(components[name], f"Rubric {name}")
        for name in RUBRIC_WEIGHTS
    }
    criteria = value.get("critical_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise GenerationV3HarnessError("generation Rubric needs critical criteria")
    normalized_criteria: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in criteria:
        if not isinstance(row, Mapping) or set(row) != {
            "criterion_id",
            "passed",
            "evidence",
        }:
            raise GenerationV3HarnessError("critical criterion shape drifted")
        criterion_id = str(row.get("criterion_id") or "").strip()
        evidence = str(row.get("evidence") or "").strip()
        if (
            not criterion_id
            or criterion_id in seen
            or type(row.get("passed")) is not bool
            or not evidence
        ):
            raise GenerationV3HarnessError("critical criterion is invalid")
        seen.add(criterion_id)
        normalized_criteria.append(
            {
                "criterion_id": criterion_id,
                "passed": row["passed"],
                "evidence": evidence,
            }
        )
    return {
        "schema": RUBRIC_SCHEMA,
        "task_id": task_id,
        "attempt": attempt,
        "components": normalized_components,
        "critical_criteria": normalized_criteria,
    }


def validate_vlm(
    value: Mapping[str, Any], *, task_id: str, attempt: int, evidence_set_sha256: str
) -> dict[str, Any]:
    required = {
        "schema",
        "task_id",
        "attempt",
        "evidence_set_sha256",
        "components",
        "judge_protocol_valid",
        "summary",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise GenerationV3HarnessError("generation VLM payload shape drifted")
    if value.get("schema") != VLM_SCHEMA or value.get("task_id") != task_id:
        raise GenerationV3HarnessError("generation VLM identity drifted")
    if int(value.get("attempt") or 0) != attempt:
        raise GenerationV3HarnessError("generation VLM attempt drifted")
    if value.get("evidence_set_sha256") != evidence_set_sha256:
        raise GenerationV3HarnessError("generation VLM evidence binding drifted")
    components = value.get("components")
    if not isinstance(components, Mapping) or set(components) != set(VLM_WEIGHTS):
        raise GenerationV3HarnessError("generation VLM components drifted")
    summary = str(value.get("summary") or "").strip()
    if type(value.get("judge_protocol_valid")) is not bool or not summary:
        raise GenerationV3HarnessError("generation VLM protocol/summary is invalid")
    return {
        "schema": VLM_SCHEMA,
        "task_id": task_id,
        "attempt": attempt,
        "evidence_set_sha256": evidence_set_sha256,
        "components": {
            name: _score_component(components[name], f"VLM {name}")
            for name in VLM_WEIGHTS
        },
        "judge_protocol_valid": value["judge_protocol_valid"],
        "summary": summary,
    }


def weighted_score(
    components: Mapping[str, Mapping[str, Any]], weights: Mapping[str, float]
) -> float:
    if set(components) != set(weights):
        raise GenerationV3HarnessError("score component names drifted")
    return round(
        sum(
            float(components[name]["score"]) * weight
            for name, weight in weights.items()
        ),
        6,
    )


def joint_acceptance(
    *,
    hard_gates: Mapping[str, bool],
    rubric: Mapping[str, Any],
    vlm: Mapping[str, Any],
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    if set(hard_gates) != set(HARD_GATE_KEYS) or any(
        type(value) is not bool for value in hard_gates.values()
    ):
        raise GenerationV3HarnessError("generation hard gates drifted")
    rubric_score = weighted_score(rubric["components"], RUBRIC_WEIGHTS)
    vlm_score = weighted_score(vlm["components"], VLM_WEIGHTS)
    critical = all(bool(row["passed"]) for row in rubric["critical_criteria"])
    protocol = bool(vlm["judge_protocol_valid"])
    accepted = (
        all(hard_gates.values())
        and critical
        and protocol
        and rubric_score >= threshold
        and vlm_score >= threshold
    )
    return {
        "acceptance_scope": ACCEPTANCE_SCOPE,
        "execution_is_formal": False,
        "sandbox_attested": False,
        "threshold": float(threshold),
        "rubric_score": rubric_score,
        "vlm_score": vlm_score,
        "hard_gates_passed": all(hard_gates.values()),
        "critical_criteria_passed": critical,
        "judge_protocol_valid": protocol,
        "joint_at_threshold": accepted,
    }


def canonicalize_generated_source(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise GenerationV3HarnessError("model returned empty generated source")
    text = raw.strip()
    tagged = re.search(r"<BLENDER_PY>\s*(.*?)\s*</BLENDER_PY>", text, re.S)
    if tagged:
        text = tagged.group(1).strip()
    fenced = re.fullmatch(r"```(?:python)?\s*(.*?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if "```" in text or "<BLENDER_PY>" in text or "</BLENDER_PY>" in text:
        raise GenerationV3HarnessError("generated source contains mixed prose/fences")
    source = text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    # Some long-form code responses spell the fractional part of a decimal,
    # for example ``0. fifty`` or ``-1. nineteen``.  That is unambiguous model
    # serialization noise rather than an authoring choice.  Normalize only
    # this narrowly bounded token shape before parsing; ordinary prose and
    # identifiers are untouched.
    decimal_words = {
        "zero": "00",
        "one": "01",
        "two": "02",
        "three": "03",
        "four": "04",
        "five": "05",
        "six": "06",
        "seven": "07",
        "eight": "08",
        "nine": "09",
        "ten": "10",
        "eleven": "11",
        "twelve": "12",
        "thirteen": "13",
        "fourteen": "14",
        "fifteen": "15",
        "sixteen": "16",
        "seventeen": "17",
        "eighteen": "18",
        "nineteen": "19",
        "twenty": "20",
        "thirty": "30",
        "forty": "40",
        "fifty": "50",
        "sixty": "60",
        "seventy": "70",
        "eighty": "80",
        "ninety": "90",
    }

    def normalize_decimal(match: re.Match[str]) -> str:
        return f"{match.group('whole')}.{decimal_words[match.group('word').casefold()]}"

    source = re.sub(
        r"(?<![\w.])(?P<whole>-?\d+)\.\s+(?P<word>" + "|".join(decimal_words) + r")\b",
        normalize_decimal,
        source,
        flags=re.IGNORECASE,
    )
    # Blender 5.1 exposes Eevee as ``BLENDER_EEVEE``.  Many otherwise valid
    # generated programs still use the Blender 4.x enum.  Normalize that one
    # versioned enum before hashing/auditing so compatibility is deterministic
    # and the durable generate.py is exactly what was executed.
    source = source.replace("'BLENDER_EEVEE_NEXT'", "'BLENDER_EEVEE'")
    source = source.replace('"BLENDER_EEVEE_NEXT"', '"BLENDER_EEVEE"')
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GenerationV3HarnessError("generated source is not valid Python") from exc
    imports_bpy = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bpy":
                    if alias.asname is not None:
                        raise GenerationV3HarnessError(
                            "generated source must not alias bpy"
                        )
                    imports_bpy = True
        elif isinstance(node, ast.ImportFrom) and node.module == "bpy":
            raise GenerationV3HarnessError(
                "generated source must use exactly 'import bpy'"
            )
    if not imports_bpy:
        raise GenerationV3HarnessError("generated source must explicitly import bpy")
    generation_contracts.audit_generated_python_source(source)

    def dotted(value: ast.AST) -> str:
        names: list[str] = []
        cursor = value
        while isinstance(cursor, ast.Attribute):
            names.append(cursor.attr)
            cursor = cursor.value
        if isinstance(cursor, ast.Name):
            names.append(cursor.id)
        return ".".join(reversed(names))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func)
        if (
            name in TRUSTED_HARNESS_OWNED_BPY_CALLS
            or name.startswith("bpy.ops.import_")
            or name.startswith("bpy.ops.export_")
        ):
            raise GenerationV3HarnessError(
                "generated source requests Blender file/render/save I/O; trusted harness owns it"
            )
    return source


def analyze_generated_source(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    ]
    bpy_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            cursor = node.func
            while isinstance(cursor, ast.Attribute):
                cursor = cursor.value
            if isinstance(cursor, ast.Name) and cursor.id == "bpy":
                bpy_calls += 1
    return {
        "line_count": len(source.splitlines()),
        "function_and_class_names": sorted(names),
        "function_and_class_count": len(names),
        "bpy_call_count": bpy_calls,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def generated_source_policy_prompt() -> str:
    """Describe the actual AST policy literally; do not invent broader bans."""

    import_roots = ", ".join(
        sorted(generation_contracts.GENERATED_SOURCE_FORBIDDEN_IMPORT_ROOTS)
    )
    call_names = ", ".join(
        sorted(generation_contracts.GENERATED_SOURCE_FORBIDDEN_CALL_NAMES)
    )
    dynamic_names = ", ".join(
        sorted(generation_contracts.GENERATED_SOURCE_FORBIDDEN_DYNAMIC_NAMES)
    )
    decoders = ", ".join(
        sorted(generation_contracts.GENERATED_SOURCE_UNPROVABLE_TEXT_CALL_NAMES)
    )
    owned = ", ".join(sorted(TRUSTED_HARNESS_OWNED_BPY_CALLS))
    return (
        "Exact generated-source AST contract: "
        f"forbidden Import/ImportFrom roots=[{import_roots}]; "
        f"forbidden ast.Call terminal names=[{call_names}]; "
        f"forbidden dynamic ast.Name identifiers=[{dynamic_names}]; "
        f"calls [{decoders}] are allowed only when their text result is statically provable; "
        "indirect callables and attributes beginning with '__' are forbidden. "
        f"Trusted-harness-owned bpy calls=[{owned}], plus every bpy.ops.import_* and "
        "bpy.ops.export_* call. Local identifiers that merely begin with words such as "
        "socket, os, or io are not forbidden unless they perform one of the exact AST "
        "operations above. "
        f"Trusted color contract: a global {TRUSTED_SRGB_HELPER_NAME}(rgb_or_rgba) helper "
        "is injected by the build harness. Every Principled Base Color assignment must "
        "directly call that helper with display-sRGB channels in [0,1]; never write a "
        "display RGB/hex-derived tuple directly and never redefine or alias the helper. "
        "The helper uses IEC 61966-2-1 exactly: c/12.92 for c<=0.04045, otherwise "
        "((c+0.055)/1.055)**2.4, so display sRGB 0.5 becomes scene-linear "
        "0.2140411405. Cameras, lights, worlds, view/display settings, render settings, "
        "compositing, and sequencer state are trusted-harness-owned."
    )


def srgb_to_scene_linear_rgba(
    value: Sequence[float],
) -> tuple[float, float, float, float]:
    """Decode display-sRGB RGB(A) with the IEC 61966-2-1 transfer function."""

    if isinstance(value, (str, bytes)) or len(value) not in {3, 4}:
        raise GenerationV3HarnessError("trusted sRGB color must contain RGB or RGBA")
    channels: list[float] = []
    for raw in value:
        if isinstance(raw, bool):
            raise GenerationV3HarnessError("trusted sRGB color channel is invalid")
        try:
            channel = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise GenerationV3HarnessError(
                "trusted sRGB color channel is invalid"
            ) from exc
        if not math.isfinite(channel) or not 0.0 <= channel <= 1.0:
            raise GenerationV3HarnessError("trusted sRGB color channel is out of range")
        channels.append(channel)
    alpha = channels[3] if len(channels) == 4 else 1.0

    def decode(channel: float) -> float:
        return (
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )

    return (decode(channels[0]), decode(channels[1]), decode(channels[2]), alpha)


def _matches_trusted_color(
    color: Sequence[float],
    trusted_colors: Sequence[Sequence[float]],
    *,
    tolerance: float = 1e-6,
) -> bool:
    """Allow only the bounded float32 storage error introduced by Blender sockets."""

    return any(
        len(color) == len(trusted)
        and all(
            abs(float(actual) - float(expected)) <= tolerance
            for actual, expected in zip(color, trusted)
        )
        for trusted in trusted_colors
    )


def enforce_task_source_contract(task: GenerationTask, source: str) -> None:
    """Apply generation-task policy that depends on the task, not global AST safety."""

    tree = ast.parse(source)
    # The canonical blank is intentionally empty.  A syntactically valid but
    # truncated response that only clears the scene or creates materials would
    # otherwise survive the source contract and spend a formal quality attempt
    # before the fresh-reopen gate discovers an empty asset.  Require an
    # explicit object-creation operation up front so that bounded code-contract
    # retry handles this as a code failure, not a visual-quality attempt.
    creation_calls = {
        "bpy.data.objects.new",
    }
    creation_prefixes = (
        "bpy.ops.mesh.primitive_",
        "bpy.ops.curve.primitive_",
        "bpy.ops.surface.primitive_",
        "bpy.ops.object.metaball_add",
        "bpy.ops.object.text_add",
        "bpy.ops.object.effector_add",
    )

    def dotted(value: ast.AST) -> str:
        names: list[str] = []
        cursor = value
        while isinstance(cursor, ast.Attribute):
            names.append(cursor.attr)
            cursor = cursor.value
        if isinstance(cursor, ast.Name):
            names.append(cursor.id)
        return ".".join(reversed(names))

    helper_shadowed = any(
        (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == TRUSTED_SRGB_HELPER_NAME
        )
        or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id == TRUSTED_SRGB_HELPER_NAME
        )
        for node in ast.walk(tree)
    )
    if helper_shadowed:
        raise GenerationV3HarnessError(
            "generated source shadows the trusted sRGB helper"
        )

    string_bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    string_bindings[target.id] = node.value.value

    def subscript_key(value: ast.AST) -> str | None:
        if not isinstance(value, ast.Subscript):
            return None
        key = value.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
        if isinstance(key, ast.Name):
            return string_bindings.get(key.id)
        return None

    base_color_aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if subscript_key(node.value) != "Base Color":
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        base_color_aliases.update(
            target.id for target in targets if isinstance(target, ast.Name)
        )

    def base_color_target(value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "default_value"
            and (
                subscript_key(value.value) == "Base Color"
                or (
                    isinstance(value.value, ast.Name)
                    and value.value.id in base_color_aliases
                )
            )
        )

    def trusted_color_call(value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == TRUSTED_SRGB_HELPER_NAME
            and len(value.args) == 1
            and not value.keywords
        )

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            base_color_target(target) for target in targets
        ) and not trusted_color_call(node.value):
            raise GenerationV3HarnessError(
                "Principled Base Color must directly call the trusted sRGB helper"
            )

    scene_aliases = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and dotted(node.value) == "bpy.context.scene"
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    forbidden_presentation_calls = {
        "bpy.data.lights.new",
        "bpy.data.cameras.new",
        "bpy.data.worlds.new",
        "bpy.ops.object.light_add",
        "bpy.ops.object.camera_add",
    }
    controlled_scene_fields = {
        "render",
        "view_settings",
        "display_settings",
        "world",
        "camera",
        "use_nodes",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_name = dotted(node.func)
            if call_name in forbidden_presentation_calls or any(
                call_name.startswith(alias + ".sequence_editor")
                for alias in scene_aliases
            ):
                raise GenerationV3HarnessError(
                    "generated source mutates trusted camera/light/world/render state"
                )
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            target_name = dotted(target)
            parts = target_name.split(".")
            direct_scene = parts[:3] == ["bpy", "context", "scene"]
            aliased_scene = bool(parts and parts[0] in scene_aliases)
            field_index = 3 if direct_scene else 1
            if (
                (direct_scene or aliased_scene)
                and len(parts) > field_index
                and parts[field_index] in controlled_scene_fields
            ):
                raise GenerationV3HarnessError(
                    "generated source mutates trusted camera/light/world/render state"
                )

    creates_object = any(
        isinstance(node, ast.Call)
        and (
            dotted(node.func) in creation_calls
            or dotted(node.func).startswith(creation_prefixes)
        )
        for node in ast.walk(tree)
    )
    if not creates_object:
        raise GenerationV3HarnessError(
            "generated source must explicitly create at least one Blender object"
        )

    if task.category != "single_object":
        return
    forbidden = sorted(
        {
            str(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in DEFAULT_SIMPLE_PRODUCT_FORBIDDEN_NODES
        }
    )
    if forbidden:
        raise GenerationV3HarnessError(
            "simple-product material policy forbids nodes: " + ", ".join(forbidden)
        )


def _artifact_bindings(attempt_dir: Path) -> dict[str, Any]:
    asset = _regular_file(attempt_dir / "asset.blend", "asset.blend", minimum_size=16)
    evidence_root = attempt_dir / "evidence"
    evidence: dict[str, Any] = {}
    for relative in EVIDENCE_NAMES:
        path = _regular_file(
            evidence_root / relative, f"evidence {relative}", minimum_size=16
        )
        evidence[relative] = {
            "path": str(path.relative_to(attempt_dir)),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    scene_report_path = _regular_file(attempt_dir / "scene_report.json", "scene report")
    scene_report = _read_json(scene_report_path, "scene report")
    if scene_report.get("schema") != SCENE_REPORT_SCHEMA:
        raise GenerationV3HarnessError("scene report schema drifted")
    result = {
        "asset": {
            "path": "asset.blend",
            "sha256": sha256_file(asset),
            "size_bytes": asset.stat().st_size,
        },
        "scene_report": {
            "path": "scene_report.json",
            "sha256": sha256_file(scene_report_path),
            "size_bytes": scene_report_path.stat().st_size,
        },
        "evidence": evidence,
    }
    result["evidence_set_sha256"] = digest(result)
    return result


def _feedback(
    receipt: Mapping[str, Any],
    manual_rejections: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    must_preserve: list[str] = []
    must_fix: list[str] = []
    failure_detail = str(receipt.get("failure_detail") or "").strip()
    if failure_detail:
        must_fix.append(
            f"Resolve {receipt.get('failure_stage') or 'attempt failure'}: {failure_detail}"
        )
    for family in ("rubric", "vlm"):
        components = (receipt.get(family) or {}).get("components") or {}
        for name, row in components.items():
            if not isinstance(row, Mapping):
                continue
            rationale = str(row.get("rationale") or "").strip()
            score = float(row.get("score") or 0.0)
            if not rationale:
                continue
            target = must_preserve if score >= DEFAULT_THRESHOLD else must_fix
            target.append(f"{family}.{name} ({score:.2f}): {rationale}")
    for row in (receipt.get("rubric") or {}).get("critical_criteria") or []:
        if not isinstance(row, Mapping):
            continue
        message = (
            f"critical.{row.get('criterion_id')}: "
            f"{str(row.get('evidence') or '').strip()}"
        )
        (must_preserve if row.get("passed") is True else must_fix).append(message)
    vlm = receipt.get("vlm") or {}
    if vlm.get("judge_protocol_valid") is False:
        must_fix.append("Produce evidence that permits a valid blinded VLM judgement.")
    manual_inventory: list[dict[str, Any]] = []
    for rejected_attempt, decision in sorted((manual_rejections or {}).items()):
        manual_feedback = decision.get("manual_feedback")
        if not isinstance(manual_feedback, Mapping):
            raise GenerationV3HarnessError("manual QA feedback binding is missing")
        must_preserve.extend(manual_feedback.get("must_preserve") or [])
        must_fix.extend(manual_feedback.get("must_fix") or [])
        manual_inventory.append(
            {
                "attempt": rejected_attempt,
                "receipt_sha256": str(decision.get("receipt_sha256") or ""),
                "evidence_set_sha256": str(decision.get("evidence_set_sha256") or ""),
                "operator_decision": str(decision.get("operator_decision") or ""),
            }
        )
    if not must_fix:
        must_fix.append(
            "Raise both independent Joint@80 scores while preserving passed criteria."
        )
    return {
        "prior_attempt": receipt.get("attempt"),
        "failure_stage": receipt.get("failure_stage"),
        "failure_detail": receipt.get("failure_detail"),
        "rubric_score": (receipt.get("acceptance") or {}).get("rubric_score"),
        "vlm_score": (receipt.get("acceptance") or {}).get("vlm_score"),
        "rubric_rationales": {
            name: row.get("rationale")
            for name, row in (
                (receipt.get("rubric") or {}).get("components") or {}
            ).items()
        },
        "vlm_rationales": {
            name: row.get("rationale")
            for name, row in (
                (receipt.get("vlm") or {}).get("components") or {}
            ).items()
        },
        "summary": (receipt.get("vlm") or {}).get("summary"),
        "must_preserve": list(dict.fromkeys(must_preserve)),
        "must_fix": list(dict.fromkeys(must_fix)),
        "manual_qa_rejections": manual_inventory,
    }


def _validated_feedback(
    feedback: Mapping[str, Any] | None, *, attempt: int
) -> Mapping[str, Any] | None:
    if feedback is None:
        if attempt > 1:
            raise GenerationV3HarnessError(
                "later quality attempts require must_preserve/must_fix feedback"
            )
        return None
    if not isinstance(feedback, Mapping):
        raise GenerationV3HarnessError("generation feedback must be an object")
    for key in ("must_preserve", "must_fix"):
        values = feedback.get(key)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or (key == "must_fix" and not values)
        ):
            raise GenerationV3HarnessError(
                "generation feedback requires string-list must_preserve/must_fix"
            )
    return feedback


def load_web_candidate_result(
    task: GenerationTask, results_root: Path
) -> dict[str, Any]:
    """Verify and project one accepted Joint@80 result for publication."""

    results_root = results_root.expanduser().resolve(strict=True)
    task_root = (results_root / task.task_id).resolve(strict=True)
    try:
        task_root.relative_to(results_root)
    except ValueError as exc:
        raise GenerationV3HarnessError("task result escapes results root") from exc
    manual_rejections = _load_manual_rejections(task_root, task)
    manual_disqualifications = _load_manual_disqualifications(task_root, task)
    if manual_disqualifications:
        raise GenerationV3HarnessError(
            "task was manually disqualified from publication"
        )
    result = _validate_receipt(
        _read_json(task_root / "task_result.json", "task result"),
        TASK_RECEIPT_SCHEMA,
    )
    if (
        result.get("task_id") != task.task_id
        or result.get("source_task_sha256") != task.source_payload_sha256
        or result.get("status") != "web_candidate_accepted"
        or result.get("acceptance_scope") != ACCEPTANCE_SCOPE
        or result.get("execution_is_formal") is not False
        or result.get("sandbox_attested") is not False
    ):
        raise GenerationV3HarnessError("published task result identity/status drifted")
    accepted_attempt = int(result.get("accepted_attempt") or 0)
    if manual_rejections and result.get(
        "manual_qa_rejections"
    ) != _manual_rejection_inventory(manual_rejections):
        raise GenerationV3HarnessError("published task manual QA inventory drifted")
    if accepted_attempt in manual_rejections:
        raise GenerationV3HarnessError("published attempt was manually rejected")
    if accepted_attempt not in range(1, MAX_ATTEMPTS + 1):
        raise GenerationV3HarnessError("published task lacks accepted attempt")
    attempt = _verify_attempt_receipt(
        task_root / f"attempt-{accepted_attempt}" / "attempt_receipt.json",
        task,
        accepted_attempt,
    )
    acceptance = attempt.get("acceptance")
    if (
        not isinstance(acceptance, Mapping)
        or acceptance.get("acceptance_scope") != ACCEPTANCE_SCOPE
        or acceptance.get("joint_at_threshold") is not True
        or float(acceptance.get("rubric_score") or 0.0) < DEFAULT_THRESHOLD
        or float(acceptance.get("vlm_score") or 0.0) < DEFAULT_THRESHOLD
    ):
        raise GenerationV3HarnessError("published attempt is not Joint@80 accepted")
    presentation = ((attempt.get("artifacts") or {}).get("evidence") or {}).get(
        "presentation.png"
    )
    if not isinstance(presentation, Mapping):
        raise GenerationV3HarnessError("published attempt lacks presentation binding")
    path = (
        task_root / f"attempt-{accepted_attempt}" / str(presentation.get("path") or "")
    ).resolve(strict=True)
    try:
        path.relative_to(task_root / f"attempt-{accepted_attempt}")
    except ValueError as exc:
        raise GenerationV3HarnessError(
            "presentation binding escapes attempt root"
        ) from exc
    _regular_file(path, "accepted presentation", minimum_size=16)
    if sha256_file(path) != presentation.get("sha256") or path.stat().st_size != int(
        presentation.get("size_bytes") or -1
    ):
        raise GenerationV3HarnessError("accepted presentation binding drifted")
    return {
        "task_id": task.task_id,
        "accepted_attempt": accepted_attempt,
        "rubric_score": float(acceptance["rubric_score"]),
        "vlm_score": float(acceptance["vlm_score"]),
        "acceptance_scope": ACCEPTANCE_SCOPE,
        "execution_is_formal": False,
        "sandbox_attested": False,
        "task_result_sha256": str(result["receipt_sha256"]),
        "attempt_receipt_sha256": str(attempt["receipt_sha256"]),
        "presentation_path": str(path),
        "presentation_sha256": str(presentation["sha256"]),
        "presentation_size_bytes": int(presentation["size_bytes"]),
    }


def _verify_attempt_receipt(
    path: Path, task: GenerationTask, attempt: int
) -> dict[str, Any]:
    value = _validate_receipt(
        _read_json(path, "attempt receipt"), ATTEMPT_RECEIPT_SCHEMA
    )
    if (
        value.get("task_id") != task.task_id
        or int(value.get("attempt") or 0) != attempt
    ):
        raise GenerationV3HarnessError("attempt receipt identity drifted")
    if value.get("source_task_sha256") != task.source_payload_sha256:
        raise GenerationV3HarnessError("attempt receipt task binding drifted")
    attempt_root = path.parent.resolve(strict=True)

    def verify(binding: Any, label: str) -> None:
        if not isinstance(binding, Mapping):
            raise GenerationV3HarnessError(f"{label} binding is missing")
        candidate = (attempt_root / str(binding.get("path") or "")).resolve(strict=True)
        try:
            candidate.relative_to(attempt_root)
        except ValueError as exc:
            raise GenerationV3HarnessError(
                f"{label} binding escapes attempt root"
            ) from exc
        _regular_file(candidate, label)
        if sha256_file(candidate) != binding.get(
            "sha256"
        ) or candidate.stat().st_size != int(binding.get("size_bytes") or -1):
            raise GenerationV3HarnessError(f"{label} binding drifted")

    if isinstance(value.get("generated_program"), Mapping):
        verify(value["generated_program"], "generate.py")
    artifacts = value.get("artifacts")
    if isinstance(artifacts, Mapping):
        verify(artifacts.get("asset"), "asset.blend")
        verify(artifacts.get("scene_report"), "scene report")
        evidence = artifacts.get("evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != set(EVIDENCE_NAMES):
            raise GenerationV3HarnessError("attempt evidence inventory drifted")
        for relative in EVIDENCE_NAMES:
            verify(evidence[relative], f"evidence {relative}")
        body = dict(artifacts)
        supplied = body.pop("evidence_set_sha256", None)
        if supplied != digest(body):
            raise GenerationV3HarnessError("attempt evidence-set binding drifted")
    return value


def _manual_reason(value: Any) -> str:
    reason = str(value or "")
    if (
        not reason
        or reason != reason.strip()
        or len(reason) > 1000
        or any(ord(character) < 32 for character in reason)
    ):
        raise GenerationV3HarnessError(
            "manual QA reason must be 1-1000 canonical printable characters"
        )
    return reason


def _manual_qa_receipt_files(task_root: Path) -> list[Path]:
    decisions = task_root / "manual_qa" / "decisions"
    if not decisions.exists():
        return []
    if not decisions.is_dir() or decisions.is_symlink():
        raise GenerationV3HarnessError("manual QA decisions root is invalid")
    files: list[Path] = []
    for child in sorted(decisions.iterdir(), key=lambda path: path.name):
        if child.name.startswith("._"):
            continue
        if (
            not child.is_file()
            or child.is_symlink()
            or not re.fullmatch(
                r"manual-(?:reject|disqualify)\.[0-9a-f]{64}\.json",
                child.name,
            )
        ):
            raise GenerationV3HarnessError("manual QA decision inventory drifted")
        files.append(child)
    return files


def _manual_archive_path(
    task_root: Path, prior_task_result_sha256: str
) -> tuple[str, Path]:
    relative = (
        f"manual_qa/archived_task_results/task_result.{prior_task_result_sha256}.json"
    )
    return relative, task_root / relative


def _validate_manual_rejection(
    path: Path,
    *,
    task_root: Path,
    task: GenerationTask,
    require_archive: bool,
) -> dict[str, Any]:
    decision = _validate_receipt(
        _read_json(path, "manual QA decision"), MANUAL_QA_RECEIPT_SCHEMA
    )
    expected_name = f"manual-reject.{decision['receipt_sha256']}.json"
    if path.name != expected_name:
        raise GenerationV3HarnessError("manual QA content-addressed filename drifted")
    attempt = int(decision.get("attempt") or 0)
    if (
        decision.get("task_id") != task.task_id
        or decision.get("source_task_sha256") != task.source_payload_sha256
        or decision.get("acceptance_scope") != ACCEPTANCE_SCOPE
        or decision.get("execution_is_formal") is not False
        or decision.get("sandbox_attested") is not False
        or decision.get("operator_decision") != "reject_and_continue"
        or attempt not in range(1, MAX_ATTEMPTS)
    ):
        raise GenerationV3HarnessError("manual QA decision identity/policy drifted")
    _manual_reason(decision.get("reason"))
    evidence_sha = str(decision.get("evidence_set_sha256") or "")
    attempt_sha = str(decision.get("attempt_receipt_sha256") or "")
    prior_sha = str(decision.get("prior_task_result_sha256") or "")
    if not all(
        SHA256_RE.fullmatch(value) for value in (evidence_sha, attempt_sha, prior_sha)
    ):
        raise GenerationV3HarnessError("manual QA SHA binding is invalid")
    attempt_receipt = _verify_attempt_receipt(
        task_root / f"attempt-{attempt}" / "attempt_receipt.json", task, attempt
    )
    if (
        attempt_receipt.get("receipt_sha256") != attempt_sha
        or ((attempt_receipt.get("artifacts") or {}).get("evidence_set_sha256"))
        != evidence_sha
        or not (attempt_receipt.get("acceptance") or {}).get("joint_at_threshold")
    ):
        raise GenerationV3HarnessError("manual QA attempt/evidence binding drifted")
    manual_feedback = decision.get("manual_feedback")
    if not isinstance(manual_feedback, Mapping):
        raise GenerationV3HarnessError("manual QA feedback binding is missing")
    _validated_feedback(manual_feedback, attempt=attempt + 1)
    archive = decision.get("archived_task_result")
    if not isinstance(archive, Mapping):
        raise GenerationV3HarnessError("manual QA archived result binding is missing")
    expected_relative, expected_archive = _manual_archive_path(task_root, prior_sha)
    if (
        archive.get("path") != expected_relative
        or archive.get("receipt_sha256") != prior_sha
    ):
        raise GenerationV3HarnessError("manual QA archived result path drifted")
    if require_archive:
        archive_path = _regular_file(expected_archive, "manual QA archived task result")
        if sha256_file(archive_path) != archive.get(
            "file_sha256"
        ) or archive_path.stat().st_size != int(archive.get("size_bytes") or -1):
            raise GenerationV3HarnessError("manual QA archived result file drifted")
        archived = _validate_receipt(
            _read_json(archive_path, "manual QA archived task result"),
            TASK_RECEIPT_SCHEMA,
        )
        if (
            archived.get("receipt_sha256") != prior_sha
            or archived.get("task_id") != task.task_id
            or archived.get("source_task_sha256") != task.source_payload_sha256
            or archived.get("status") != "web_candidate_accepted"
            or int(archived.get("accepted_attempt") or 0) != attempt
        ):
            raise GenerationV3HarnessError("manual QA archived result identity drifted")
    return decision


def _manual_rejection_inventory(
    decisions: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "attempt": attempt,
            "receipt_sha256": str(decision["receipt_sha256"]),
            "evidence_set_sha256": str(decision["evidence_set_sha256"]),
            "operator_decision": "reject_and_continue",
        }
        for attempt, decision in sorted(decisions.items())
    ]


def _load_manual_rejections(
    task_root: Path, task: GenerationTask
) -> dict[int, dict[str, Any]]:
    decisions: dict[int, dict[str, Any]] = {}
    for path in _manual_qa_receipt_files(task_root):
        if path.name.startswith("manual-disqualify."):
            continue
        decision = _validate_manual_rejection(
            path, task_root=task_root, task=task, require_archive=True
        )
        attempt = int(decision["attempt"])
        if attempt in decisions:
            raise GenerationV3HarnessError(
                "manual QA attempt was rejected more than once"
            )
        decisions[attempt] = decision
    active = task_root / "task_result.json"
    if active.is_file():
        active_result = _validate_receipt(
            _read_json(active, "task result"), TASK_RECEIPT_SCHEMA
        )
        rejected_results = {
            str(decision["prior_task_result_sha256"]) for decision in decisions.values()
        }
        if active_result.get("receipt_sha256") in rejected_results:
            raise GenerationV3HarnessError(
                "manual QA rejection is pending task-result archival"
            )
    return decisions


def _complete_manual_task_result_archive(
    *,
    task_root: Path,
    decision: Mapping[str, Any],
    allow_already_archived: bool = False,
) -> None:
    active = task_root / "task_result.json"
    archive_binding = decision["archived_task_result"]
    archive = task_root / str(archive_binding["path"])
    if not active.is_file():
        if archive.is_file():
            if allow_already_archived:
                archive_path = _regular_file(archive, "manual QA archived task result")
                if sha256_file(archive_path) != archive_binding.get(
                    "file_sha256"
                ) or archive_path.stat().st_size != int(
                    archive_binding.get("size_bytes") or -1
                ):
                    raise GenerationV3HarnessError("manual QA archive collision")
                return
            raise GenerationV3HarnessError("manual QA attempt was already rejected")
        raise GenerationV3HarnessError("manual QA source task result is missing")
    active_result = _validate_receipt(
        _read_json(active, "task result"), TASK_RECEIPT_SCHEMA
    )
    if (
        active_result.get("receipt_sha256") != decision.get("prior_task_result_sha256")
        or sha256_file(active) != archive_binding.get("file_sha256")
        or active.stat().st_size != int(archive_binding.get("size_bytes") or -1)
    ):
        raise GenerationV3HarnessError("manual QA active task result drifted")
    if archive.exists():
        archive_path = _regular_file(archive, "manual QA archived task result")
        if sha256_file(archive_path) != archive_binding.get(
            "file_sha256"
        ) or archive_path.stat().st_size != int(
            archive_binding.get("size_bytes") or -1
        ):
            raise GenerationV3HarnessError("manual QA archive collision")
    else:
        _write_once(
            archive,
            active.read_bytes(),
            "content-addressed manual QA task-result archive",
        )
    active.unlink()
    if active.exists():
        raise GenerationV3HarnessError("manual QA could not retire active task result")


def reject_accepted_attempt(
    *,
    task: GenerationTask,
    output_root: Path,
    attempt: int,
    expected_task_result_sha256: str,
    expected_evidence_sha256: str,
    reason: str,
) -> dict[str, Any]:
    """Immutably reject one automatic acceptance so the next attempt may run."""

    if attempt not in range(1, MAX_ATTEMPTS):
        raise GenerationV3HarnessError(
            "manual QA may continue only from accepted attempt 1 or 2"
        )
    for value, label in (
        (expected_task_result_sha256, "expected task-result SHA"),
        (expected_evidence_sha256, "expected evidence SHA"),
    ):
        if not SHA256_RE.fullmatch(str(value).lower()):
            raise GenerationV3HarnessError(f"{label} is invalid")
    expected_task_result_sha256 = str(expected_task_result_sha256).lower()
    expected_evidence_sha256 = str(expected_evidence_sha256).lower()
    reason = _manual_reason(reason)
    output_root = output_root.expanduser().resolve(strict=True)
    task_root = (output_root / task.task_id).resolve(strict=True)
    try:
        task_root.relative_to(output_root)
    except ValueError as exc:
        raise GenerationV3HarnessError(
            "manual QA task root escapes output root"
        ) from exc

    existing_files = _manual_qa_receipt_files(task_root)
    for path in existing_files:
        if path.name.startswith("manual-disqualify."):
            disqualification = _validate_manual_disqualification(
                path, task_root=task_root, task=task, require_archive=True
            )
            raise GenerationV3HarnessError(
                "task was already manually disqualified: "
                f"{disqualification['receipt_sha256']}"
            )
        existing = _validate_manual_rejection(
            path, task_root=task_root, task=task, require_archive=False
        )
        if int(existing["attempt"]) != attempt:
            _validate_manual_rejection(
                path, task_root=task_root, task=task, require_archive=True
            )
            continue
        if (
            existing.get("prior_task_result_sha256") != expected_task_result_sha256
            or existing.get("evidence_set_sha256") != expected_evidence_sha256
            or existing.get("reason") != reason
        ):
            raise GenerationV3HarnessError("manual QA repeated decision drifted")
        _complete_manual_task_result_archive(task_root=task_root, decision=existing)
        _validate_manual_rejection(
            path, task_root=task_root, task=task, require_archive=True
        )
        return existing

    prior_decisions = _load_manual_rejections(task_root, task)
    task_result_path = _regular_file(
        task_root / "task_result.json", "accepted task result"
    )
    task_result = _validate_receipt(
        _read_json(task_result_path, "accepted task result"), TASK_RECEIPT_SCHEMA
    )
    if (
        task_result.get("task_id") != task.task_id
        or task_result.get("source_task_sha256") != task.source_payload_sha256
        or task_result.get("status") != "web_candidate_accepted"
        or int(task_result.get("accepted_attempt") or 0) != attempt
        or task_result.get("receipt_sha256") != expected_task_result_sha256
    ):
        raise GenerationV3HarnessError("manual QA expected task result drifted")
    expected_inventory = _manual_rejection_inventory(prior_decisions)
    if (
        prior_decisions
        and task_result.get("manual_qa_rejections") != expected_inventory
    ):
        raise GenerationV3HarnessError("task result manual QA inventory drifted")
    attempt_receipt = _verify_attempt_receipt(
        task_root / f"attempt-{attempt}" / "attempt_receipt.json", task, attempt
    )
    evidence_sha = str(
        ((attempt_receipt.get("artifacts") or {}).get("evidence_set_sha256")) or ""
    )
    if evidence_sha != expected_evidence_sha256 or not (
        attempt_receipt.get("acceptance") or {}
    ).get("joint_at_threshold"):
        raise GenerationV3HarnessError("manual QA expected evidence drifted")
    automatic_feedback = _feedback(attempt_receipt)
    manual_preserve = list(automatic_feedback.get("must_preserve") or [])
    if not manual_preserve:
        manual_preserve = [
            "Preserve all automatically passed Joint@80 criteria and visible reference-aligned parts."
        ]
    prior_file_sha = sha256_file(task_result_path)
    prior_size = task_result_path.stat().st_size
    archive_relative, _archive_path = _manual_archive_path(
        task_root, expected_task_result_sha256
    )
    decision = _receipt(
        {
            "schema": MANUAL_QA_RECEIPT_SCHEMA,
            "acceptance_scope": ACCEPTANCE_SCOPE,
            "execution_is_formal": False,
            "sandbox_attested": False,
            "task_id": task.task_id,
            "source_task_sha256": task.source_payload_sha256,
            "attempt": attempt,
            "next_quality_attempt": attempt + 1,
            "operator_decision": "reject_and_continue",
            "reason": reason,
            "prior_task_result_sha256": expected_task_result_sha256,
            "attempt_receipt_sha256": str(attempt_receipt["receipt_sha256"]),
            "evidence_set_sha256": expected_evidence_sha256,
            "manual_feedback": {
                "must_preserve": manual_preserve,
                "must_fix": [f"Manual QA rejection: {reason}"],
            },
            "archived_task_result": {
                "path": archive_relative,
                "receipt_sha256": expected_task_result_sha256,
                "file_sha256": prior_file_sha,
                "size_bytes": prior_size,
            },
        }
    )
    decision_path = (
        task_root
        / "manual_qa"
        / "decisions"
        / f"manual-reject.{decision['receipt_sha256']}.json"
    )
    _write_once(
        decision_path,
        canonical_bytes(decision),
        "content-addressed manual QA decision",
    )
    _complete_manual_task_result_archive(task_root=task_root, decision=decision)
    _validate_manual_rejection(
        decision_path, task_root=task_root, task=task, require_archive=True
    )
    return decision


def _validate_manual_disqualification(
    path: Path,
    *,
    task_root: Path,
    task: GenerationTask,
    require_archive: bool,
) -> dict[str, Any]:
    decision = _validate_receipt(
        _read_json(path, "manual disqualification"),
        MANUAL_DISQUALIFY_RECEIPT_SCHEMA,
    )
    if path.name != f"manual-disqualify.{decision['receipt_sha256']}.json":
        raise GenerationV3HarnessError(
            "manual disqualification content-addressed filename drifted"
        )
    if (
        decision.get("task_id") != task.task_id
        or decision.get("source_task_sha256") != task.source_payload_sha256
        or decision.get("acceptance_scope") != ACCEPTANCE_SCOPE
        or decision.get("execution_is_formal") is not False
        or decision.get("sandbox_attested") is not False
        or decision.get("operator_decision") != "disqualify_and_replace"
        or decision.get("replacement_reason")
        != "manual_quality_gate_failed_after_three_attempts"
        or int(decision.get("attempt") or 0) != MAX_ATTEMPTS
    ):
        raise GenerationV3HarnessError(
            "manual disqualification identity/policy drifted"
        )
    _manual_reason(decision.get("reason"))
    evidence_sha = str(decision.get("evidence_set_sha256") or "")
    attempt_sha = str(decision.get("attempt_receipt_sha256") or "")
    prior_sha = str(decision.get("prior_task_result_sha256") or "")
    if not all(
        SHA256_RE.fullmatch(value) for value in (evidence_sha, attempt_sha, prior_sha)
    ):
        raise GenerationV3HarnessError("manual disqualification SHA binding is invalid")
    attempt_receipt = _verify_attempt_receipt(
        task_root / f"attempt-{MAX_ATTEMPTS}" / "attempt_receipt.json",
        task,
        MAX_ATTEMPTS,
    )
    if (
        attempt_receipt.get("receipt_sha256") != attempt_sha
        or ((attempt_receipt.get("artifacts") or {}).get("evidence_set_sha256"))
        != evidence_sha
        or not (attempt_receipt.get("acceptance") or {}).get("joint_at_threshold")
    ):
        raise GenerationV3HarnessError(
            "manual disqualification attempt/evidence binding drifted"
        )
    prior_rejections = _load_manual_rejections(task_root, task)
    if decision.get("prior_manual_qa_rejections") != _manual_rejection_inventory(
        prior_rejections
    ):
        raise GenerationV3HarnessError(
            "manual disqualification prior QA inventory drifted"
        )
    archive = decision.get("archived_task_result")
    if not isinstance(archive, Mapping):
        raise GenerationV3HarnessError(
            "manual disqualification archived result binding is missing"
        )
    expected_relative, expected_archive = _manual_archive_path(task_root, prior_sha)
    if (
        archive.get("path") != expected_relative
        or archive.get("receipt_sha256") != prior_sha
    ):
        raise GenerationV3HarnessError("manual disqualification archive path drifted")
    if require_archive:
        archive_path = _regular_file(
            expected_archive, "manual disqualification archived task result"
        )
        if sha256_file(archive_path) != archive.get(
            "file_sha256"
        ) or archive_path.stat().st_size != int(archive.get("size_bytes") or -1):
            raise GenerationV3HarnessError(
                "manual disqualification archive file drifted"
            )
        archived = _validate_receipt(
            _read_json(archive_path, "manual disqualification archived task result"),
            TASK_RECEIPT_SCHEMA,
        )
        if (
            archived.get("receipt_sha256") != prior_sha
            or archived.get("task_id") != task.task_id
            or archived.get("source_task_sha256") != task.source_payload_sha256
            or archived.get("status") != "web_candidate_accepted"
            or int(archived.get("accepted_attempt") or 0) != MAX_ATTEMPTS
            or int(archived.get("attempt_count") or 0) != MAX_ATTEMPTS
        ):
            raise GenerationV3HarnessError(
                "manual disqualification archived result identity drifted"
            )
    return decision


def _manual_disqualification_binding(
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "attempt": MAX_ATTEMPTS,
        "receipt_sha256": str(decision["receipt_sha256"]),
        "evidence_set_sha256": str(decision["evidence_set_sha256"]),
        "operator_decision": "disqualify_and_replace",
        "reason": str(decision["reason"]),
    }


def _load_manual_disqualifications(
    task_root: Path, task: GenerationTask
) -> dict[int, dict[str, Any]]:
    decisions: dict[int, dict[str, Any]] = {}
    for path in _manual_qa_receipt_files(task_root):
        if path.name.startswith("manual-reject."):
            continue
        decision = _validate_manual_disqualification(
            path, task_root=task_root, task=task, require_archive=True
        )
        attempt = int(decision["attempt"])
        if attempt in decisions:
            raise GenerationV3HarnessError(
                "task was manually disqualified more than once"
            )
        decisions[attempt] = decision
    active = task_root / "task_result.json"
    if active.is_file():
        active_result = _validate_receipt(
            _read_json(active, "task result"), TASK_RECEIPT_SCHEMA
        )
        rejected_results = {
            str(decision["prior_task_result_sha256"]) for decision in decisions.values()
        }
        if active_result.get("receipt_sha256") in rejected_results:
            raise GenerationV3HarnessError(
                "manual disqualification is pending task-result finalization"
            )
    return decisions


def _finish_manual_disqualification(
    *,
    task_root: Path,
    task: GenerationTask,
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    active = task_root / "task_result.json"
    if active.is_file():
        current = _validate_receipt(
            _read_json(active, "task result"), TASK_RECEIPT_SCHEMA
        )
        if current.get("receipt_sha256") == decision.get("prior_task_result_sha256"):
            _complete_manual_task_result_archive(
                task_root=task_root,
                decision=decision,
                allow_already_archived=True,
            )
        elif current.get("status") == "replacement_required" and current.get(
            "manual_qa_disqualification"
        ) == _manual_disqualification_binding(decision):
            raise GenerationV3HarnessError("task was already manually disqualified")
        else:
            raise GenerationV3HarnessError(
                "manual disqualification active task result drifted"
            )
    else:
        _complete_manual_task_result_archive(
            task_root=task_root,
            decision=decision,
            allow_already_archived=True,
        )
    prior_rejections = _load_manual_rejections(task_root, task)
    attempts = [
        _verify_attempt_receipt(
            task_root / f"attempt-{attempt}/attempt_receipt.json", task, attempt
        )
        for attempt in range(1, MAX_ATTEMPTS + 1)
    ]
    return _finalize_task_receipt(
        task_root,
        task,
        attempts,
        "replacement_required",
        None,
        prior_rejections,
        decision,
    )


def disqualify_accepted_task(
    *,
    task: GenerationTask,
    output_root: Path,
    attempt: int,
    expected_task_result_sha256: str,
    expected_evidence_sha256: str,
    reason: str,
) -> dict[str, Any]:
    """Convert an accepted final attempt into an immutable replacement result."""

    if attempt != MAX_ATTEMPTS:
        raise GenerationV3HarnessError(
            "manual disqualification requires the final quality attempt"
        )
    for value, label in (
        (expected_task_result_sha256, "expected task-result SHA"),
        (expected_evidence_sha256, "expected evidence SHA"),
    ):
        if not SHA256_RE.fullmatch(str(value).lower()):
            raise GenerationV3HarnessError(f"{label} is invalid")
    expected_task_result_sha256 = str(expected_task_result_sha256).lower()
    expected_evidence_sha256 = str(expected_evidence_sha256).lower()
    reason = _manual_reason(reason)
    output_root = output_root.expanduser().resolve(strict=True)
    task_root = (output_root / task.task_id).resolve(strict=True)
    try:
        task_root.relative_to(output_root)
    except ValueError as exc:
        raise GenerationV3HarnessError(
            "manual disqualification task root escapes output root"
        ) from exc

    for path in _manual_qa_receipt_files(task_root):
        if path.name.startswith("manual-reject."):
            _validate_manual_rejection(
                path, task_root=task_root, task=task, require_archive=True
            )
            continue
        existing = _validate_manual_disqualification(
            path, task_root=task_root, task=task, require_archive=False
        )
        if (
            existing.get("prior_task_result_sha256") != expected_task_result_sha256
            or existing.get("evidence_set_sha256") != expected_evidence_sha256
            or existing.get("reason") != reason
        ):
            raise GenerationV3HarnessError(
                "manual disqualification repeated decision drifted"
            )
        return _finish_manual_disqualification(
            task_root=task_root, task=task, decision=existing
        )

    prior_rejections = _load_manual_rejections(task_root, task)
    task_result_path = _regular_file(
        task_root / "task_result.json", "accepted task result"
    )
    task_result = _validate_receipt(
        _read_json(task_result_path, "accepted task result"), TASK_RECEIPT_SCHEMA
    )
    if (
        task_result.get("task_id") != task.task_id
        or task_result.get("source_task_sha256") != task.source_payload_sha256
        or task_result.get("status") != "web_candidate_accepted"
        or int(task_result.get("accepted_attempt") or 0) != MAX_ATTEMPTS
        or int(task_result.get("attempt_count") or 0) != MAX_ATTEMPTS
        or task_result.get("receipt_sha256") != expected_task_result_sha256
        or task_result.get("manual_qa_rejections")
        != _manual_rejection_inventory(prior_rejections)
    ):
        raise GenerationV3HarnessError(
            "manual disqualification expected task result drifted"
        )
    attempts = [
        _verify_attempt_receipt(
            task_root / f"attempt-{number}/attempt_receipt.json", task, number
        )
        for number in range(1, MAX_ATTEMPTS + 1)
    ]
    final_attempt = attempts[-1]
    evidence_sha = str(
        ((final_attempt.get("artifacts") or {}).get("evidence_set_sha256")) or ""
    )
    if evidence_sha != expected_evidence_sha256 or not (
        final_attempt.get("acceptance") or {}
    ).get("joint_at_threshold"):
        raise GenerationV3HarnessError(
            "manual disqualification expected evidence drifted"
        )
    prior_file_sha = sha256_file(task_result_path)
    prior_size = task_result_path.stat().st_size
    archive_relative, _archive_path = _manual_archive_path(
        task_root, expected_task_result_sha256
    )
    decision = _receipt(
        {
            "schema": MANUAL_DISQUALIFY_RECEIPT_SCHEMA,
            "acceptance_scope": ACCEPTANCE_SCOPE,
            "execution_is_formal": False,
            "sandbox_attested": False,
            "task_id": task.task_id,
            "source_task_sha256": task.source_payload_sha256,
            "attempt": MAX_ATTEMPTS,
            "operator_decision": "disqualify_and_replace",
            "replacement_reason": ("manual_quality_gate_failed_after_three_attempts"),
            "reason": reason,
            "prior_task_result_sha256": expected_task_result_sha256,
            "attempt_receipt_sha256": str(final_attempt["receipt_sha256"]),
            "evidence_set_sha256": expected_evidence_sha256,
            "prior_manual_qa_rejections": _manual_rejection_inventory(prior_rejections),
            "archived_task_result": {
                "path": archive_relative,
                "receipt_sha256": expected_task_result_sha256,
                "file_sha256": prior_file_sha,
                "size_bytes": prior_size,
            },
        }
    )
    decision_path = (
        task_root
        / "manual_qa/decisions"
        / f"manual-disqualify.{decision['receipt_sha256']}.json"
    )
    _write_once(
        decision_path,
        canonical_bytes(decision),
        "content-addressed manual disqualification",
    )
    result = _finish_manual_disqualification(
        task_root=task_root, task=task, decision=decision
    )
    _validate_manual_disqualification(
        decision_path, task_root=task_root, task=task, require_archive=True
    )
    return result


def _setup_inputs(
    task_root: Path, task: GenerationTask, blank_blend: Path
) -> tuple[Path, Path, Path]:
    task_root.mkdir(parents=True, exist_ok=True)
    task_json = task_root / "task.json"
    blank_copy = task_root / "blank.blend"
    reference_copy = (
        task_root / f"reference_preview{task.reference_path.suffix.lower() or '.png'}"
    )
    expected_task = canonical_bytes(task.model_payload())
    if not task_json.exists():
        _write_once(task_json, expected_task, "task.json")
    if task_json.read_bytes() != expected_task:
        raise GenerationV3HarnessError("task.json immutable input drifted")
    for source, destination, expected_sha, label in (
        (blank_blend, blank_copy, sha256_file(blank_blend), "blank.blend"),
        (
            task.reference_path,
            reference_copy,
            task.reference_sha256,
            "reference preview",
        ),
    ):
        if not destination.exists():
            shutil.copyfile(source, destination)
        _regular_file(destination, label)
        if sha256_file(destination) != expected_sha:
            raise GenerationV3HarnessError(f"{label} immutable input drifted")
    return task_json, blank_copy, reference_copy


def _remove_recovery_staging(*, task_root: Path, staging: Path, attempt: int) -> None:
    """Remove only this task's staging tree, tolerating disappearance races."""

    expected_name = re.compile(rf"\.attempt-{int(attempt)}\.staging-[1-9][0-9]*\Z")
    try:
        resolved_task_root = task_root.resolve(strict=True)
        resolved_parent = staging.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise GenerationV3HarnessError(
            "recovery staging parent is not the active task root"
        ) from exc
    if resolved_parent != resolved_task_root or not expected_name.fullmatch(
        staging.name
    ):
        raise GenerationV3HarnessError(
            "refusing recovery cleanup outside the exact attempt staging path"
        )
    try:
        staging.lstat()
    except FileNotFoundError:
        return
    if staging.is_symlink():
        raise GenerationV3HarnessError("refusing recovery cleanup of a staging symlink")

    def tolerate_disappearance(_function: Any, _path: str, exc_info: Any) -> None:
        error = exc_info[1]
        if isinstance(error, FileNotFoundError):
            return
        raise error

    try:
        shutil.rmtree(staging, onerror=tolerate_disappearance)
    except FileNotFoundError:
        return
    if os.path.lexists(staging):
        raise GenerationV3HarnessError(
            "recovery staging cleanup did not remove the exact staging path"
        )


def run_task(
    *,
    task: GenerationTask,
    blank_blend: Path,
    blank_blend_sha256: str,
    output_root: Path,
    code_generator: CodeGenerator,
    blender_executor: BlenderExecutor,
    scorer: GenerationScorer,
    threshold: float = DEFAULT_THRESHOLD,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    if max_attempts != MAX_ATTEMPTS:
        raise GenerationV3HarnessError(
            "GEN-V3 policy requires exactly a three-attempt ceiling"
        )
    if float(threshold) != DEFAULT_THRESHOLD:
        raise GenerationV3HarnessError("this curation harness is frozen to Joint@80")
    blank_blend = _regular_file(blank_blend, "canonical blank.blend", minimum_size=16)
    if (
        not SHA256_RE.fullmatch(str(blank_blend_sha256).lower())
        or sha256_file(blank_blend) != str(blank_blend_sha256).lower()
    ):
        raise GenerationV3HarnessError("canonical blank.blend SHA-256 drifted")
    output_root = output_root.expanduser().resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=True)
    task_root = output_root / task.task_id
    task_json, blank_copy, reference_copy = _setup_inputs(task_root, task, blank_blend)
    manual_rejections = _load_manual_rejections(task_root, task)
    manual_disqualifications = _load_manual_disqualifications(task_root, task)
    task_receipt_path = task_root / "task_result.json"
    if task_receipt_path.exists():
        receipt = _validate_receipt(
            _read_json(task_receipt_path, "task result"), TASK_RECEIPT_SCHEMA
        )
        if (
            receipt.get("task_id") != task.task_id
            or receipt.get("source_task_sha256") != task.source_payload_sha256
        ):
            raise GenerationV3HarnessError("task result identity drifted")
        rows = receipt.get("attempt_receipts")
        if not isinstance(rows, list) or len(rows) != int(
            receipt.get("attempt_count") or -1
        ):
            raise GenerationV3HarnessError("task result attempt inventory drifted")
        for row in rows:
            attempt_number = int(row.get("attempt") or 0)
            child = _verify_attempt_receipt(
                task_root / f"attempt-{attempt_number}" / "attempt_receipt.json",
                task,
                attempt_number,
            )
            if child.get("receipt_sha256") != row.get("receipt_sha256"):
                raise GenerationV3HarnessError("task result child receipt drifted")
        if manual_rejections and receipt.get(
            "manual_qa_rejections"
        ) != _manual_rejection_inventory(manual_rejections):
            raise GenerationV3HarnessError("task result manual QA inventory drifted")
        if manual_disqualifications:
            if len(manual_disqualifications) != 1:
                raise GenerationV3HarnessError(
                    "task manual disqualification inventory drifted"
                )
            decision = next(iter(manual_disqualifications.values()))
            if receipt.get("status") != "replacement_required" or receipt.get(
                "manual_qa_disqualification"
            ) != _manual_disqualification_binding(decision):
                raise GenerationV3HarnessError(
                    "task result manual disqualification binding drifted"
                )
        return receipt
    if manual_disqualifications:
        raise GenerationV3HarnessError(
            "manual disqualification is pending replacement task result"
        )
    attempts: list[dict[str, Any]] = []
    feedback: Mapping[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        attempt_dir = task_root / f"attempt-{attempt}"
        receipt_path = attempt_dir / "attempt_receipt.json"
        if attempt_dir.exists():
            if not receipt_path.is_file():
                raise GenerationV3HarnessError(
                    f"partial attempt-{attempt} exists without a terminal receipt"
                )
            prior = _verify_attempt_receipt(receipt_path, task, attempt)
            attempts.append(prior)
            if (prior.get("acceptance") or {}).get("joint_at_threshold"):
                if attempt in manual_rejections:
                    feedback = _feedback(prior, manual_rejections)
                    continue
                return _finalize_task_receipt(
                    task_root,
                    task,
                    attempts,
                    "web_candidate_accepted",
                    attempt,
                    manual_rejections,
                )
            feedback = _feedback(prior, manual_rejections)
            continue
        staging = task_root / f".attempt-{attempt}.staging-{os.getpid()}"
        if staging.exists() or os.path.lexists(staging):
            raise GenerationV3HarnessError(
                f"stale attempt staging exists: {staging.name}"
            )
        staging.mkdir()
        base: dict[str, Any] = {
            "schema": ATTEMPT_RECEIPT_SCHEMA,
            "acceptance_scope": ACCEPTANCE_SCOPE,
            "execution_is_formal": False,
            "sandbox_attested": False,
            "task_id": task.task_id,
            "source_task_sha256": task.source_payload_sha256,
            "attempt": attempt,
            "threshold": float(threshold),
            "input_bindings": {
                "task_json_sha256": sha256_file(task_json),
                "blank_blend_sha256": sha256_file(blank_copy),
                "reference_sha256": sha256_file(reference_copy),
            },
        }
        try:
            generation_feedback = _validated_feedback(feedback, attempt=attempt)
            contract_failures: list[dict[str, Any]] = []
            generated: GeneratedProgram | None = None
            source = ""
            for contract_retry in range(2):
                generated = code_generator(
                    task, reference_copy, attempt, generation_feedback
                )
                try:
                    source = canonicalize_generated_source(generated.source)
                    enforce_task_source_contract(task, source)
                except (
                    GenerationV3HarnessError,
                    generation_contracts.GenerationContractError,
                ) as exc:
                    contract_failures.append(
                        {
                            "retry_index": contract_retry,
                            "logical_call_id": str(generated.logical_call_id),
                            "response_replayed": bool(generated.response_replayed),
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:1000],
                        }
                    )
                    if contract_retry == 0:
                        prior_preserve = (
                            list(generation_feedback.get("must_preserve") or [])
                            if isinstance(generation_feedback, Mapping)
                            else []
                        )
                        prior_fix = (
                            list(generation_feedback.get("must_fix") or [])
                            if isinstance(generation_feedback, Mapping)
                            else []
                        )
                        retry_feedback = (
                            dict(generation_feedback)
                            if isinstance(generation_feedback, Mapping)
                            else {}
                        )
                        retry_feedback.update(
                            {
                                "prior_attempt": attempt,
                                "retry_kind": "code_contract_retry",
                                "code_contract_retry": 1,
                                "must_preserve": prior_preserve,
                                "must_fix": prior_fix
                                + [
                                    "Return code that satisfies the exact generated-source AST "
                                    f"contract; prior violation: {type(exc).__name__}: {str(exc)[:700]}"
                                ],
                            }
                        )
                        generation_feedback = retry_feedback
                        continue
                    raise GenerationV3CodeContractExhausted(
                        "code contract remained invalid after one independent retry; "
                        "quality attempt was not consumed"
                    ) from exc
                break
            if generated is None or not source:
                raise GenerationV3CodeContractExhausted(
                    "code contract retry did not produce a canonical program"
                )
            generate_path = staging / "generate.py"
            _write_once(generate_path, source.encode("utf-8"), "generate.py")
            source_analysis = analyze_generated_source(source)
            base["generated_program"] = {
                "path": "generate.py",
                "sha256": sha256_file(generate_path),
                "size_bytes": generate_path.stat().st_size,
                "logical_call_id": str(generated.logical_call_id),
                "response_replayed": bool(generated.response_replayed),
                "source_analysis": source_analysis,
            }
            base["code_contract_failures_before_success"] = contract_failures
            execution = dict(
                blender_executor(
                    blank_blend=blank_copy,
                    generate_py=generate_path,
                    task_json=task_json,
                    attempt_dir=staging,
                )
            )
            artifacts = _artifact_bindings(staging)
            scene_report = _read_json(staging / "scene_report.json", "scene report")
            hard_gates = {
                "execution_success": bool(execution.get("execution_success")),
                "asset_saved": bool(execution.get("asset_saved")),
                "fresh_reopen": bool(
                    execution.get("fresh_reopen")
                    and scene_report.get("fresh_reopen")
                    and scene_report.get("asset_sha256") == artifacts["asset"]["sha256"]
                ),
                "evidence_complete": bool(execution.get("evidence_complete")),
                "no_external_dependencies": bool(
                    execution.get("no_external_dependencies")
                    and not scene_report.get("external_dependencies")
                    and not scene_report.get("linked_libraries")
                ),
            }
            if not all(hard_gates.values()):
                raise GenerationV3HarnessError(
                    "one or more generation hard gates failed"
                )
            scorer_artifacts = dict(artifacts)
            selected = ["presentation.png", "clay.png", "normal.png"] + [
                f"turntable/frame_{index:03d}.png" for index in (0, 2, 4, 6)
            ]
            scorer_artifacts["_absolute_evidence_paths"] = [
                str(staging / "evidence" / relative) for relative in selected
            ]
            raw_rubric, raw_vlm, judge_meta = scorer(
                task,
                reference_copy,
                attempt,
                scorer_artifacts,
                scene_report,
                source_analysis,
            )
            rubric = validate_rubric(raw_rubric, task_id=task.task_id, attempt=attempt)
            vlm = validate_vlm(
                raw_vlm,
                task_id=task.task_id,
                attempt=attempt,
                evidence_set_sha256=str(artifacts["evidence_set_sha256"]),
            )
            acceptance = joint_acceptance(
                hard_gates=hard_gates,
                rubric=rubric,
                vlm=vlm,
                threshold=threshold,
            )
            terminal_status = (
                "web_candidate_accepted"
                if acceptance["joint_at_threshold"]
                else "below_threshold"
            )
            base.update(
                {
                    "status": terminal_status,
                    "execution": execution,
                    "hard_gates": hard_gates,
                    "artifacts": artifacts,
                    "rubric": rubric,
                    "vlm": vlm,
                    "judge": dict(judge_meta),
                    "acceptance": acceptance,
                }
            )
        except (
            GenerationV3ProviderUnavailable,
            GenerationV3DeliveryUnknown,
            GenerationV3ArtifactRecoveryRequired,
            GenerationV3CodeContractExhausted,
        ):
            # Provider downtime and missing historical judge evidence are
            # recovery conditions, not model-quality attempts.  Preserve no
            # partial task evidence and stop so the same attempt can resume
            # after infrastructure recovery or artifact restoration.
            _remove_recovery_staging(
                task_root=task_root, staging=staging, attempt=attempt
            )
            raise
        except Exception as exc:
            base.update(
                {
                    "status": "attempt_failed",
                    "failure_stage": type(exc).__name__,
                    "failure_detail": str(exc)[:1000],
                    "acceptance": {
                        "threshold": float(threshold),
                        "rubric_score": 0.0,
                        "vlm_score": 0.0,
                        "hard_gates_passed": False,
                        "critical_criteria_passed": False,
                        "judge_protocol_valid": False,
                        "joint_at_threshold": False,
                    },
                }
            )
        attempt_receipt = _receipt(base)
        _atomic_json(staging / "attempt_receipt.json", attempt_receipt)
        os.replace(staging, attempt_dir)
        attempts.append(attempt_receipt)
        if attempt_receipt["acceptance"]["joint_at_threshold"]:
            return _finalize_task_receipt(
                task_root,
                task,
                attempts,
                "web_candidate_accepted",
                attempt,
                manual_rejections,
            )
        feedback = _feedback(attempt_receipt, manual_rejections)
    return _finalize_task_receipt(
        task_root,
        task,
        attempts,
        "replacement_required",
        None,
        manual_rejections,
    )


def _finalize_task_receipt(
    task_root: Path,
    task: GenerationTask,
    attempts: Sequence[Mapping[str, Any]],
    status: str,
    accepted_attempt: int | None,
    manual_rejections: Mapping[int, Mapping[str, Any]] | None = None,
    manual_disqualification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"web_candidate_accepted", "replacement_required"}:
        raise GenerationV3HarnessError("invalid task terminal status")
    if manual_disqualification is not None and status != "replacement_required":
        raise GenerationV3HarnessError(
            "manual disqualification requires replacement-required status"
        )
    result = _receipt(
        {
            "schema": TASK_RECEIPT_SCHEMA,
            "acceptance_scope": ACCEPTANCE_SCOPE,
            "execution_is_formal": False,
            "sandbox_attested": False,
            "task_id": task.task_id,
            "source_task_sha256": task.source_payload_sha256,
            "status": status,
            "accepted_attempt": accepted_attempt,
            "attempt_count": len(attempts),
            "max_attempts": MAX_ATTEMPTS,
            "manual_qa_rejections": _manual_rejection_inventory(
                manual_rejections or {}
            ),
            "manual_qa_disqualification": (
                _manual_disqualification_binding(manual_disqualification)
                if manual_disqualification is not None
                else None
            ),
            "replacement_reason": (
                None
                if status == "web_candidate_accepted"
                else (
                    "manual_quality_gate_failed_after_three_attempts"
                    if manual_disqualification is not None
                    else "joint_at_80_not_met_after_three_attempts"
                )
            ),
            "attempt_receipts": [
                {
                    "attempt": int(row["attempt"]),
                    "receipt_sha256": str(row["receipt_sha256"]),
                    "status": str(row["status"]),
                    "joint_at_threshold": bool(
                        (row.get("acceptance") or {}).get("joint_at_threshold")
                    ),
                }
                for row in attempts
            ],
        }
    )
    path = task_root / "task_result.json"
    if path.exists():
        existing = _validate_receipt(
            _read_json(path, "task result"), TASK_RECEIPT_SCHEMA
        )
        if existing != result:
            raise GenerationV3HarnessError("task terminal receipt drifted")
        return existing
    _atomic_json(path, result)
    return result


class SubprocessBlenderExecutor:
    def __init__(
        self, blender: Path, *, timeout_seconds: int = 1800, resolution: int = 512
    ):
        self.blender = _regular_file(blender, "Blender executable")
        self.timeout_seconds = int(timeout_seconds)
        self.resolution = int(resolution)
        if self.timeout_seconds < 1 or not 256 <= self.resolution <= 2048:
            raise GenerationV3HarnessError("Blender timeout/resolution is invalid")

    def _run(self, command: Sequence[str], label: str) -> str:
        completed = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
        )
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        # Blender may return 0 even when a ``--python`` entrypoint raised.  A
        # Python traceback is therefore a hard execution failure in addition
        # to the process exit code.
        python_failed = "Traceback (most recent call last)" in output
        if completed.returncode != 0 or python_failed:
            raise GenerationV3HarnessError(
                f"{label} failed with exit {completed.returncode}: {output[-2000:]}"
            )
        return output[-4000:]

    def __call__(
        self,
        *,
        blank_blend: Path,
        generate_py: Path,
        task_json: Path,
        attempt_dir: Path,
    ) -> Mapping[str, Any]:
        asset = attempt_dir / "asset.blend"
        script = Path(__file__).resolve()
        build_output = self._run(
            [
                str(self.blender),
                "--background",
                str(blank_blend),
                "--python",
                str(script),
                "--",
                "_blender-build",
                "--generate",
                str(generate_py),
                "--asset",
                str(asset),
            ],
            "Blender generation",
        )
        _regular_file(asset, "asset.blend", minimum_size=16)
        evidence_output = self._run(
            [
                str(self.blender),
                "--background",
                str(asset),
                "--python",
                str(script),
                "--",
                "_blender-evidence",
                "--asset",
                str(asset),
                "--task-json",
                str(task_json),
                "--evidence-dir",
                str(attempt_dir / "evidence"),
                "--scene-report",
                str(attempt_dir / "scene_report.json"),
                "--resolution",
                str(self.resolution),
                "--turntable-frames",
                "8",
            ],
            "Blender fresh-reopen evidence",
        )
        _artifact_bindings(attempt_dir)
        report = _read_json(attempt_dir / "scene_report.json", "scene report")
        fresh_reopen = bool(
            report.get("fresh_reopen")
            and report.get("asset_sha256") == sha256_file(asset)
        )
        return {
            "execution_success": True,
            "asset_saved": True,
            "fresh_reopen": fresh_reopen,
            "evidence_complete": True,
            "no_external_dependencies": not report.get("external_dependencies")
            and not report.get("linked_libraries"),
            "build_log_sha256": hashlib.sha256(build_output.encode()).hexdigest(),
            "evidence_log_sha256": hashlib.sha256(evidence_output.encode()).hexdigest(),
            "blender_version": str(report.get("blender_version") or ""),
        }


def _image_data_url(path: Path) -> str:
    path = _regular_file(path, "prompt image")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = path.read_bytes()
    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _message_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], Mapping)
    ):
        raise GenerationV3HarnessError("provider response lacks choices")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise GenerationV3HarnessError("provider response lacks message")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text = "\n".join(
            str(row.get("text") or "") for row in content if isinstance(row, Mapping)
        ).strip()
        if text:
            return text
    raise GenerationV3HarnessError("provider response content is empty")


def _provider_unavailable_reason(response: Any) -> str | None:
    """Classify transport/upstream envelopes that are not model outputs."""

    status_code = int(getattr(response, "status_code", 200))
    if status_code >= 500:
        return f"http_{status_code}"
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    error_type = str(error.get("type") or "").strip().lower()
    marker = " ".join(
        str(error.get(field) or "").lower()
        for field in ("message", "localized_message", "code")
    )
    if error_type in {"server_error", "upstream_error"}:
        return f"http_{status_code}_{error_type}"
    if "deactivated_workspace" in marker or "http 402" in marker:
        return f"http_{status_code}_upstream_unavailable"
    return None


def _bind_paid_budget(
    ledger_path: Path, *, max_total_calls: int, max_total_tokens: int
) -> None:
    """Freeze one global budget across restarts of the dedicated GEN-V3 ledger."""

    try:
        with sqlite3.connect(ledger_path, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_direct_gen_v3_budget_binding(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_name TEXT NOT NULL,
                    max_total_calls INTEGER NOT NULL,
                    max_total_tokens INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT schema_name,max_total_calls,max_total_tokens "
                "FROM model_direct_gen_v3_budget_binding WHERE singleton=1"
            ).fetchone()
            expected = (
                "video2blender.model-direct-generation-paid-budget.v1",
                int(max_total_calls),
                int(max_total_tokens),
            )
            if row is None:
                usage = connection.execute(
                    "SELECT COUNT(*) AS calls,"
                    "COALESCE(SUM(COALESCE(total_tokens,reserved_tokens)),0) AS tokens "
                    "FROM api_calls"
                ).fetchone()
                if (
                    int(usage["calls"]) > expected[1]
                    or int(usage["tokens"]) > expected[2]
                ):
                    raise GenerationV3HarnessError(
                        "existing paid ledger already exceeds the requested budget"
                    )
                connection.execute(
                    "INSERT INTO model_direct_gen_v3_budget_binding "
                    "(singleton,schema_name,max_total_calls,max_total_tokens) VALUES (1,?,?,?)",
                    expected,
                )
            elif (
                str(row["schema_name"]),
                int(row["max_total_calls"]),
                int(row["max_total_tokens"]),
            ) != expected:
                raise GenerationV3HarnessError(
                    "GEN-V3 paid ledger is bound to another global budget"
                )
            connection.commit()
    except sqlite3.Error as exc:
        raise GenerationV3HarnessError(
            "GEN-V3 paid ledger budget binding failed"
        ) from exc


@contextmanager
def _approved_endpoint(endpoint: str):
    from video_replay_paid_api import normalize_chat_completions_endpoint

    normalized = normalize_chat_completions_endpoint(endpoint)
    key = "VIDEO_REPLAY_APPROVED_PAID_API_ENDPOINT"
    previous = os.environ.get(key)
    os.environ[key] = normalized
    try:
        yield normalized
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


class PaidChatBackend:
    """Exact-once code generation and VLM scoring over the shared paid ledger."""

    def __init__(
        self,
        *,
        api_root: Path,
        ledger_path: Path,
        endpoint: str,
        api_key: str,
        code_model: str,
        judge_model: str,
        max_total_calls: int,
        max_total_tokens: int,
        max_tokens_per_call: int = 100_000,
        minimum_free_bytes: int = 0,
        emergency_reserve_bytes: int = 0,
        call_chat: Callable[..., Any] | None = None,
    ):
        from video_replay_paid_api import BudgetPolicy, PaidApiLedger

        if not api_key.strip() or max_total_calls < 1 or max_total_tokens < 1:
            raise GenerationV3HarnessError(
                "paid backend credentials/budget are invalid"
            )
        self.api_root = api_root.expanduser().resolve(strict=False)
        self.api_root.mkdir(parents=True, exist_ok=True)
        self.endpoint = endpoint
        self.api_key = api_key
        self.code_model = code_model
        self.judge_model = judge_model
        self.max_tokens_per_call = int(max_tokens_per_call)
        stage_call_limits: dict[str, int] = {}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            for stage in (CODEGEN_STAGE, JUDGE_STAGE):
                base = f"{stage}/attempt-{attempt}"
                stage_call_limits[base] = 1
                stage_call_limits[f"{base}/provider-retry-1"] = 1
            contract = f"{CODEGEN_STAGE}/attempt-{attempt}/{CODE_CONTRACT_RETRY_SUFFIX}"
            stage_call_limits[contract] = 1
            stage_call_limits[f"{contract}/provider-retry-1"] = 1
        self.ledger = PaidApiLedger(
            ledger_path.expanduser().resolve(strict=False),
            budget=BudgetPolicy(
                max_calls_per_asset=MAX_ATTEMPTS * 6,
                max_tokens_per_asset=MAX_ATTEMPTS * 6 * self.max_tokens_per_call,
                max_total_calls=int(max_total_calls),
                max_total_tokens=int(max_total_tokens),
                stage_call_limits=stage_call_limits,
            ),
            minimum_free_bytes=int(minimum_free_bytes),
            emergency_reserve_bytes=int(emergency_reserve_bytes),
        )
        _bind_paid_budget(
            self.ledger.database,
            max_total_calls=int(max_total_calls),
            max_total_tokens=int(max_total_tokens),
        )
        if call_chat is None:
            import video_replay_model_client as client

            call_chat = client.call_chat_completions
        self.call_chat = call_chat

    def _api_dir(self, task: GenerationTask) -> Path:
        path = self.api_root / task.task_id
        path.mkdir(parents=True, exist_ok=True)
        source_info = path / "source.info.json"
        expected = canonical_bytes(
            {"id": task.task_id, "workload_kind": "model_direct_generation_v3"}
        )
        if not source_info.exists():
            _write_once(source_info, expected, "API identity")
        elif source_info.read_bytes() != expected:
            raise GenerationV3HarnessError("API task identity drifted")
        knowledge = path / "knowledge_retrieval_pack.json"
        expected_knowledge = canonical_bytes(
            {"knowledge_generation": KNOWLEDGE_VERSION}
        )
        if not knowledge.exists():
            _write_once(knowledge, expected_knowledge, "API knowledge identity")
        elif knowledge.read_bytes() != expected_knowledge:
            raise GenerationV3HarnessError("API knowledge identity drifted")
        return path

    def _replay_durable_stage(
        self,
        *,
        task: GenerationTask,
        stage: str,
        stage_key: str,
        attempt: int,
        model: str,
        prompt_version: str,
        semantic_input: Mapping[str, Any],
    ) -> Any | None:
        """Reproject and reuse the unique durable response for one attempt stage.

        Archived task receipts can change the locally reconstructed feedback for
        a later attempt without changing the already-authorized paid stage.  In
        that case ``prepare_call`` correctly refuses a second logical call at
        the one-call stage limit.  Resolve the immutable stage first so recovery
        reuses its response instead of turning that refusal into a task failure.
        """

        import video_replay_model_client as client
        from agent_api_trace import project_durable_api_call

        video_dir = self._api_dir(task)
        asset_id = client.task_identity(video_dir)
        database = self.ledger.database.expanduser().resolve(strict=False)
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT logical_call_id,input_sha256,model,prompt_version,endpoint,
                           request_body,request_sha256,response_sha256
                    FROM api_calls
                    WHERE asset_id=? AND stage=? AND stage_key=?
                      AND state IN ('response_durable','consumed')
                    ORDER BY created_at_epoch_ns,logical_call_id
                    """,
                    (asset_id, stage, stage_key),
                ).fetchall()
        except sqlite3.Error as exc:
            raise GenerationV3HarnessError(
                "durable stage replay lookup failed"
            ) from exc
        if not rows:
            return None
        if len(rows) != 1:
            raise GenerationV3HarnessError(
                f"durable stage replay is ambiguous: {task.task_id} {stage_key}"
            )
        row = rows[0]
        if str(row["model"]) != model or str(row["prompt_version"]) != prompt_version:
            raise GenerationV3HarnessError(
                f"durable stage replay contract drifted: {task.task_id} {stage_key}"
            )
        if stage == JUDGE_STAGE:
            from video_replay_paid_api import payload_sha256

            if str(row["input_sha256"]) != payload_sha256(semantic_input):
                raise GenerationV3ArtifactRecoveryRequired(
                    "durable judge semantic input/evidence binding drifted; "
                    f"restore the original {task.task_id} attempt-{attempt} "
                    "artifacts before replay"
                )
        request_body = bytes(row["request_body"] or b"")
        if not request_body or hashlib.sha256(request_body).hexdigest() != str(
            row["request_sha256"]
        ):
            raise GenerationV3HarnessError("durable replay request binding drifted")
        try:
            request_payload = json.loads(request_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GenerationV3HarnessError(
                "durable replay request is not canonical JSON"
            ) from exc
        if not isinstance(request_payload, Mapping):
            raise GenerationV3HarnessError(
                "durable replay request payload is not an object"
            )
        logical_call_id = str(row["logical_call_id"])
        stored = self.ledger.replay_response(logical_call_id)
        if hashlib.sha256(stored.content).hexdigest() != str(row["response_sha256"]):
            raise GenerationV3HarnessError("durable replay response binding drifted")

        def project(response: Any) -> None:
            project_durable_api_call(
                video_dir,
                logical_call_id=logical_call_id,
                stage=stage,
                attempt=attempt,
                endpoint=str(row["endpoint"]),
                request_payload=request_payload,
                wire_request_body=request_body,
                response_body=response.content,
                status_code=response.status_code,
                response_headers=response.headers,
                provider_request_id=response.provider_request_id,
            )

        if not self.ledger.project_response(logical_call_id, project):
            raise GenerationV3HarnessError("durable stage reprojection failed")
        replayed = self.ledger.consume(logical_call_id)
        return client.DurableModelResponse(
            logical_call_id=replayed.logical_call_id,
            status_code=replayed.status_code,
            headers=dict(replayed.headers),
            content=replayed.content,
            provider_request_id=replayed.provider_request_id,
            prompt_tokens=replayed.prompt_tokens,
            completion_tokens=replayed.completion_tokens,
            total_tokens=replayed.total_tokens,
            replayed=True,
        )

    def _call(
        self,
        *,
        task: GenerationTask,
        stage: str,
        attempt: int,
        model: str,
        prompt_version: str,
        payload: Mapping[str, Any],
        semantic_input: Mapping[str, Any],
        code_contract_retry: int = 0,
    ) -> Any:
        from requests.exceptions import RequestException
        from video_replay_paid_api import CircuitOpen, DeliveryUnknown

        if code_contract_retry not in {0, 1} or (
            code_contract_retry and stage != CODEGEN_STAGE
        ):
            raise GenerationV3HarnessError("code-contract retry stage is invalid")
        base_stage_key = f"{stage}/attempt-{attempt}"
        if code_contract_retry:
            base_stage_key += f"/{CODE_CONTRACT_RETRY_SUFFIX}"
        last_failure = "unknown"
        with _approved_endpoint(self.endpoint):
            for provider_retry in range(2):
                retry_input = dict(semantic_input)
                stage_key = base_stage_key
                if provider_retry:
                    stage_key += f"/provider-retry-{provider_retry}"
                    retry_input["provider_retry"] = provider_retry
                response = self._replay_durable_stage(
                    task=task,
                    stage=stage,
                    stage_key=stage_key,
                    attempt=attempt,
                    model=model,
                    prompt_version=prompt_version,
                    semantic_input=retry_input,
                )
                if response is None:
                    try:
                        response = self.call_chat(
                            video_dir=self._api_dir(task),
                            stage=stage,
                            stage_key=stage_key,
                            prompt_version=prompt_version,
                            endpoint=self.endpoint,
                            api_key=self.api_key,
                            model=model,
                            payload=payload,
                            timeout=(20.0, 600.0),
                            semantic_input=retry_input,
                            ledger=self.ledger,
                        )
                    except (DeliveryUnknown, RequestException) as exc:
                        # The exact-once ledger has already quarantined a
                        # transport failure after the request became eligible
                        # for delivery.  It must never become a quality failure
                        # or enter the provider-retry stage: the logical call is
                        # permanently no-resend and requires operator recovery.
                        raise GenerationV3DeliveryUnknown(
                            "paid model delivery is unknown; preserve the "
                            "sent_unknown logical call and do not resend"
                        ) from exc
                    except CircuitOpen as exc:
                        # Budget, provider, or delivery-uncertainty circuits are
                        # infrastructure gates.  Stop before writing an attempt
                        # receipt so a clean task retains all quality attempts.
                        raise GenerationV3ProviderUnavailable(
                            f"paid model circuit is open ({exc})"
                        ) from exc
                unavailable = _provider_unavailable_reason(response)
                if unavailable is None:
                    return response
                last_failure = unavailable
        raise GenerationV3ProviderUnavailable(
            f"provider remained unavailable after bounded retry ({last_failure})"
        )

    def generate(
        self,
        task: GenerationTask,
        reference_path: Path,
        attempt: int,
        feedback: Mapping[str, Any] | None,
    ) -> GeneratedProgram:
        feedback = _validated_feedback(feedback, attempt=attempt)
        code_contract_retry = (
            int(feedback.get("code_contract_retry") or 0)
            if isinstance(feedback, Mapping)
            else 0
        )
        if code_contract_retry not in {0, 1}:
            raise GenerationV3HarnessError("code_contract_retry must be zero or one")
        task_payload = task.model_payload()
        material_policy = (
            "For this single-object/simple-product task, do not create "
            "ShaderNodeTexNoise, ShaderNodeBump, or ShaderNodeDisplacement; use "
            "clean Principled materials with trusted sRGB-converted base color, metallic, "
            "and roughness. "
            "Create only the requested asset: do not create floor/ground planes, backdrops, "
            "pedestals, text, logos, studio sets, or environment geometry; the trusted harness "
            "supplies the entire presentation environment. "
            if task.category == "single_object"
            else ""
        )
        prompt = (
            "Return only one complete Blender Python program inside <BLENDER_PY> tags. "
            "Start from the already-open blank scene and create the complete editable asset. "
            "Use procedural Blender data only: no filesystem reads, network, subprocesses, "
            "external libraries, imported assets, or tool/agent calls. Use clear semantic object "
            "and material names; construct all visible parts and presentation materials. "
            "The trusted harness owns all cameras, lights, world, render settings, rendering, and "
            "saving: do not create or modify any of them. Target Blender 5.1; if an Eevee enum is "
            "unavoidably needed, use BLENDER_EEVEE, never BLENDER_EEVEE_NEXT.\n"
            "Treat TASK.visual_contract as mandatory: construct every must_have_part, match the "
            "silhouette and material_targets, avoid every forbid_surface_trait, and orient the "
            "declared hero_face toward the corresponding trusted presentation camera.\n"
            f"{material_policy}\n"
            f"{generated_source_policy_prompt()}\n"
            f"TASK={json.dumps(task_payload, ensure_ascii=False, sort_keys=True)}\n"
            f"PRIOR_ATTEMPT_FEEDBACK={json.dumps(feedback, ensure_ascii=False, sort_keys=True) if feedback else 'none'}"
        )
        payload = {
            "model": self.code_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You write one deterministic, executable bpy scene-generation program.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(reference_path)},
                        },
                    ],
                },
            ],
            "max_completion_tokens": min(64_000, self.max_tokens_per_call),
        }
        response = self._call(
            task=task,
            stage=CODEGEN_STAGE,
            attempt=attempt,
            model=self.code_model,
            prompt_version=CODEGEN_PROMPT_VERSION,
            payload=payload,
            semantic_input={
                "schema": CODEGEN_PROMPT_VERSION,
                "task": task_payload,
                "reference_sha256": task.reference_sha256,
                "attempt": attempt,
                "feedback": feedback,
                "code_contract_retry": code_contract_retry,
            },
            code_contract_retry=code_contract_retry,
        )
        return GeneratedProgram(
            source=_message_text(response.json()),
            logical_call_id=str(response.logical_call_id),
            response_replayed=bool(response.replayed),
        )

    def score(
        self,
        task: GenerationTask,
        reference_path: Path,
        attempt: int,
        artifact_bindings: Mapping[str, Any],
        scene_report: Mapping[str, Any],
        source_analysis: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        evidence_sha = str(artifact_bindings["evidence_set_sha256"])
        images: list[Path] = [reference_path]
        # Absolute paths are transient and excluded from the durable receipt;
        # every one is authenticated against its content-addressed binding.
        bound_paths = artifact_bindings.get("_absolute_evidence_paths")
        selected = ["presentation.png", "clay.png", "normal.png"] + [
            f"turntable/frame_{index:03d}.png" for index in (0, 2, 4, 6)
        ]
        if not isinstance(bound_paths, list) or len(bound_paths) != len(selected):
            raise GenerationV3HarnessError("paid scorer lacks absolute evidence paths")
        evidence_bindings = artifact_bindings.get("evidence")
        if not isinstance(evidence_bindings, Mapping):
            raise GenerationV3HarnessError("paid scorer lacks evidence bindings")
        for relative, raw_path in zip(selected, bound_paths):
            path = _regular_file(Path(raw_path), f"judge evidence {relative}")
            binding = evidence_bindings.get(relative)
            if not isinstance(binding, Mapping) or sha256_file(path) != binding.get(
                "sha256"
            ):
                raise GenerationV3HarnessError(
                    f"judge evidence binding drifted: {relative}"
                )
            images.append(path)
        prompt = {
            "instruction": task.instruction,
            "category": task.category,
            "visual_contract": normalize_visual_contract(task.visual_contract),
            "attempt": attempt,
            "evidence_set_sha256": evidence_sha,
            "scene_report": scene_report,
            "generated_source_analysis": source_analysis,
            "rubric_weights": RUBRIC_WEIGHTS,
            "vlm_weights": VLM_WEIGHTS,
            "joint_policy": "hard_gates AND all_critical AND rubric>=80 AND vlm>=80",
            "required_output": {
                "rubric": {
                    "schema": RUBRIC_SCHEMA,
                    "task_id": task.task_id,
                    "attempt": attempt,
                    "components": {
                        name: {"score": "0..100", "rationale": "specific evidence"}
                        for name in RUBRIC_WEIGHTS
                    },
                    "critical_criteria": [
                        {
                            "criterion_id": "requirement-critical",
                            "passed": True,
                            "evidence": "specific evidence",
                        }
                    ],
                },
                "vlm": {
                    "schema": VLM_SCHEMA,
                    "task_id": task.task_id,
                    "attempt": attempt,
                    "evidence_set_sha256": evidence_sha,
                    "components": {
                        name: {"score": "0..100", "rationale": "specific evidence"}
                        for name in VLM_WEIGHTS
                    },
                    "judge_protocol_valid": True,
                    "summary": "specific concise assessment",
                },
            },
        }
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Judge this generated Blender asset against the reference and task. "
                    "The first image is the reference; subsequent images are presentation, "
                    "clay, normal, and ordered turntable views. Score every dimension "
                    "independently; do not let attractive rendering compensate for wrong "
                    "structure or fidelity. Return exactly one JSON object with top-level "
                    "keys rubric and vlm.\n"
                    + json.dumps(prompt, ensure_ascii=False, sort_keys=True)
                ),
            }
        ]
        content.extend(
            {"type": "image_url", "image_url": {"url": _image_data_url(path)}}
            for path in images
        )
        payload = {
            "model": self.judge_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a strict blinded 3D asset evaluator. Output JSON only.",
                },
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": min(8_000, self.max_tokens_per_call),
        }
        response = self._call(
            task=task,
            stage=JUDGE_STAGE,
            attempt=attempt,
            model=self.judge_model,
            prompt_version=JUDGE_PROMPT_VERSION,
            payload=payload,
            semantic_input={
                "schema": JUDGE_PROMPT_VERSION,
                "task_sha256": task.source_payload_sha256,
                "reference_sha256": task.reference_sha256,
                "evidence_set_sha256": evidence_sha,
                "attempt": attempt,
            },
        )
        try:
            parsed = json.loads(_message_text(response.json()))
        except json.JSONDecodeError as exc:
            raise GenerationV3HarnessError("judge response is not JSON") from exc
        if not isinstance(parsed, Mapping) or set(parsed) != {"rubric", "vlm"}:
            raise GenerationV3HarnessError("judge response top-level shape drifted")
        return (
            parsed["rubric"],
            parsed["vlm"],
            {
                "logical_call_id": str(response.logical_call_id),
                "response_replayed": bool(response.replayed),
                "judge_model": self.judge_model,
                "prompt_version": JUDGE_PROMPT_VERSION,
            },
        )


def _blender_build(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", type=Path, required=True)
    parser.add_argument("--asset", type=Path, required=True)
    args = parser.parse_args(argv)
    import bpy  # type: ignore

    source = args.generate.read_text(encoding="utf-8")
    generation_contracts.audit_generated_python_source(source)
    converted_colors: list[tuple[float, float, float, float]] = []

    def tracked_srgb_to_scene_linear_rgba(
        value: Sequence[float],
    ) -> tuple[float, float, float, float]:
        converted = srgb_to_scene_linear_rgba(value)
        converted_colors.append(converted)
        return converted

    namespace = {
        "__name__": "__main__",
        "__file__": str(args.generate),
        TRUSTED_SRGB_HELPER_NAME: tracked_srgb_to_scene_linear_rgba,
    }
    # The fail-closed AST contract above rejects imports and capabilities outside
    # the declarative Blender subset before this isolated build step executes it.
    exec(compile(source, str(args.generate), "exec"), namespace, namespace)  # nosec B102
    principled_colors: list[tuple[float, float, float, float]] = []
    for material in bpy.data.materials:
        if not material.use_nodes or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if getattr(node, "type", "") != "BSDF_PRINCIPLED":
                continue
            socket = node.inputs.get("Base Color")
            if socket is not None:
                principled_colors.append(
                    tuple(float(value) for value in socket.default_value)
                )
    if principled_colors and (
        not converted_colors
        or any(
            not _matches_trusted_color(color, converted_colors)
            for color in principled_colors
        )
    ):
        raise GenerationV3HarnessError(
            "generated Principled materials bypassed the trusted sRGB runtime helper"
        )
    bpy.context.scene["GEN_V3_TRUSTED_SRGB_HELPER_CALL_COUNT"] = len(converted_colors)
    args.asset.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.asset), check_existing=False)
    if not args.asset.is_file() or args.asset.stat().st_size < 16:
        raise GenerationV3HarnessError("Blender did not save asset.blend")
    return 0


def _blender_create_blank(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    import bpy  # type: ignore

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection_name in (
        "meshes",
        "curves",
        "materials",
        "images",
        "cameras",
        "lights",
        "actions",
    ):
        collection = getattr(bpy.data, collection_name, ())
        for datablock in list(collection):
            if getattr(datablock, "users", 0) == 0:
                collection.remove(datablock)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 250
    scene.frame_set(1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output), check_existing=False)
    if not args.output.is_file() or args.output.stat().st_size < 16:
        raise GenerationV3HarnessError("Blender did not save canonical blank.blend")
    return 0


def create_canonical_blank(
    *, blender: Path, output: Path, timeout_seconds: int = 300
) -> dict[str, Any]:
    blender = _regular_file(blender, "Blender executable")
    output = output.expanduser().resolve(strict=False)
    if output.exists() or os.path.lexists(output):
        raise GenerationV3HarnessError("canonical blank output must be absent")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Blender silently appends ``.blend`` when the requested filepath does not
    # end with that suffix.  Keep the staging name itself canonical so the
    # trusted parent can verify and atomically publish the exact file it asked
    # Blender to create.
    staging = output.parent / f".{output.stem}.staging-{os.getpid()}.blend"
    completed = subprocess.run(
        [
            str(blender),
            "--background",
            "--factory-startup",
            "--python",
            str(Path(__file__).resolve()),
            "--",
            "_blender-create-blank",
            "--output",
            str(staging),
        ],
        text=True,
        capture_output=True,
        timeout=int(timeout_seconds),
        check=False,
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
    )
    if completed.returncode != 0:
        staging.unlink(missing_ok=True)
        raise GenerationV3HarnessError(
            "canonical blank creation failed: "
            + ((completed.stdout or "") + (completed.stderr or ""))[-2000:]
        )
    _regular_file(staging, "canonical blank staging", minimum_size=16)
    os.replace(staging, output)
    return {
        "status": "created",
        "path": str(output),
        "sha256": sha256_file(output),
        "size_bytes": output.stat().st_size,
    }


def _scene_report(bpy: Any, *, asset: Path) -> dict[str, Any]:
    renderable_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME", "GPENCIL"}
    objects = list(bpy.data.objects)
    visible = [
        obj
        for obj in objects
        if obj.type in renderable_types and not obj.hide_render and obj.visible_get()
    ]
    invalid_transforms: list[str] = []
    for obj in objects:
        values = list(obj.location) + list(obj.rotation_euler) + list(obj.scale)
        if any(not math.isfinite(float(value)) for value in values) or any(
            abs(float(value)) < 1e-8 for value in obj.scale
        ):
            invalid_transforms.append(obj.name)
    external: list[str] = []
    for image in bpy.data.images:
        if image.source == "FILE" and image.filepath and not image.packed_file:
            external.append(f"image:{image.name}")
    for collection_name in ("fonts", "movieclips", "sounds", "cache_files", "volumes"):
        collection = getattr(bpy.data, collection_name, ())
        for datablock in collection:
            filepath = str(getattr(datablock, "filepath", "") or "")
            packed = bool(getattr(datablock, "packed_file", None))
            if filepath and not packed and filepath not in {"<builtin>", "Bfont"}:
                external.append(f"{collection_name}:{datablock.name}")
    mesh_vertices = sum(len(obj.data.vertices) for obj in objects if obj.type == "MESH")
    mesh_polygons = sum(len(obj.data.polygons) for obj in objects if obj.type == "MESH")
    return {
        "schema": SCENE_REPORT_SCHEMA,
        "fresh_reopen": Path(str(bpy.data.filepath or "")).resolve() == asset.resolve(),
        "asset_sha256": sha256_file(asset),
        "blender_version": bpy.app.version_string,
        "object_count": len(objects),
        "renderable_object_count": len(visible),
        "object_names": sorted(obj.name for obj in objects),
        "object_types": {
            kind: sum(obj.type == kind for obj in objects)
            for kind in sorted({obj.type for obj in objects})
        },
        "mesh_vertex_count": mesh_vertices,
        "mesh_polygon_count": mesh_polygons,
        "material_count": len(bpy.data.materials),
        "material_names": sorted(material.name for material in bpy.data.materials),
        "collection_count": len(bpy.data.collections),
        "linked_libraries": sorted(
            library.filepath for library in bpy.data.libraries if library.filepath
        ),
        "external_dependencies": sorted(external),
        "invalid_transform_objects": sorted(invalid_transforms),
        "trusted_srgb_helper_call_count": int(
            bpy.context.scene.get("GEN_V3_TRUSTED_SRGB_HELPER_CALL_COUNT", 0)
        ),
    }


def _look_at(obj: Any, target: Any) -> None:
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _bounds(bpy: Any, objects: Sequence[Any]) -> tuple[Any, float]:
    from mathutils import Vector  # type: ignore

    points = [
        obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box
    ]
    if not points:
        raise GenerationV3HarnessError(
            "freshly reopened scene has no renderable bounds"
        )
    minimum = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    maximum = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    center = (minimum + maximum) * 0.5
    extent = maximum - minimum
    radius = max(float(extent.x), float(extent.y), float(extent.z), 0.1)
    return center, radius


def _material(bpy: Any, name: str, *, normal: bool = False) -> Any:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    if normal:
        geometry = nodes.new("ShaderNodeNewGeometry")
        multiply = nodes.new("ShaderNodeVectorMath")
        multiply.operation = "SCALE"
        multiply.inputs[3].default_value = 0.5
        add = nodes.new("ShaderNodeVectorMath")
        add.operation = "ADD"
        add.inputs[1].default_value = (0.5, 0.5, 0.5)
        emission = nodes.new("ShaderNodeEmission")
        material.node_tree.links.new(geometry.outputs["Normal"], multiply.inputs[0])
        material.node_tree.links.new(multiply.outputs["Vector"], add.inputs[0])
        material.node_tree.links.new(add.outputs["Vector"], emission.inputs["Color"])
        material.node_tree.links.new(
            emission.outputs["Emission"], output.inputs["Surface"]
        )
    else:
        principled = nodes.new("ShaderNodeBsdfPrincipled")
        principled.inputs["Base Color"].default_value = (0.62, 0.65, 0.68, 1.0)
        principled.inputs["Roughness"].default_value = 0.72
        material.node_tree.links.new(
            principled.outputs["BSDF"], output.inputs["Surface"]
        )
    return material


def _evidence_color_management(scene: Any, *, category: str) -> dict[str, Any]:
    """Bind one deterministic display/output profile for every future render."""

    view = scene.view_settings
    display_settings = getattr(scene, "display_settings", None)
    image_settings = getattr(getattr(scene, "render", None), "image_settings", None)
    if display_settings is None or image_settings is None:
        raise GenerationV3HarnessError("evidence color settings are unavailable")
    simple_scene = category == "simple_scene"
    profile = (
        "simple_scene_reference_color_v3_standard"
        if simple_scene
        else "single_object_legacy_pinned_v1"
    )
    expected_transform = "Standard" if simple_scene else "AgX"
    expected_look = "None"
    expected_exposure = SIMPLE_SCENE_EVIDENCE_EXPOSURE if simple_scene else 0.0
    try:
        display_settings.display_device = "sRGB"
        image_settings.file_format = "PNG"
        image_settings.color_mode = "RGBA"
        image_settings.color_depth = "8"
        view.view_transform = expected_transform
        view.look = expected_look
        view.exposure = expected_exposure
        view.gamma = 1.0
    except (TypeError, ValueError) as exc:
        raise GenerationV3HarnessError(
            f"{profile} evidence color/output contract is unsupported"
        ) from exc
    actual = {
        "profile": profile,
        "view_transform": str(view.view_transform),
        "look": str(view.look),
        "exposure": float(view.exposure),
        "gamma": float(view.gamma),
        "display_device": str(display_settings.display_device),
        "output_file_format": str(image_settings.file_format),
        "output_color_mode": str(image_settings.color_mode),
        "output_color_depth": str(image_settings.color_depth),
    }
    expected = {
        "view_transform": expected_transform,
        "look": expected_look,
        "exposure": float(expected_exposure),
        "gamma": 1.0,
        "display_device": "sRGB",
        "output_file_format": "PNG",
        "output_color_mode": "RGBA",
        "output_color_depth": "8",
    }
    if any(actual[key] != value for key, value in expected.items()):
        raise GenerationV3HarnessError(
            f"{profile} evidence color/output contract did not bind"
        )
    return actual


def _blender_evidence(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--task-json", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--scene-report", type=Path, required=True)
    parser.add_argument("--resolution", type=int, required=True)
    parser.add_argument("--turntable-frames", type=int, required=True)
    args = parser.parse_args(argv)
    if args.turntable_frames != 8:
        raise GenerationV3HarnessError(
            "generation evidence requires eight turntable views"
        )
    import bpy  # type: ignore
    from mathutils import Vector  # type: ignore

    task = _read_json(args.task_json, "task.json")
    generation_contracts.validate_sanitized_task_payload(
        {key: task.get(key) for key in generation_contracts.MODEL_TASK_KEYS}
    )
    visual_contract = normalize_visual_contract(task.get("visual_contract"))
    asset = _regular_file(args.asset, "freshly reopened asset.blend", minimum_size=16)
    report = _scene_report(bpy, asset=asset)
    report["hero_face"] = visual_contract["hero_face"]
    report["visual_contract"] = visual_contract
    if (
        not report["fresh_reopen"]
        or report["renderable_object_count"] < 1
        or report["invalid_transform_objects"]
    ):
        raise GenerationV3HarnessError(
            "freshly reopened scene is empty or has invalid transforms"
        )
    renderable_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME", "GPENCIL"}
    visible = [
        obj
        for obj in bpy.data.objects
        if obj.type in renderable_types and not obj.hide_render and obj.visible_get()
    ]
    center, radius = _bounds(bpy, visible)
    scene = bpy.context.scene
    source_light_names = sorted(
        obj.name for obj in bpy.data.objects if obj.type == "LIGHT"
    )
    source_camera_names = sorted(
        obj.name for obj in bpy.data.objects if obj.type == "CAMERA"
    )
    source_world_names = sorted(world.name for world in bpy.data.worlds)
    scene.camera = None
    scene.world = None
    for obj in list(bpy.data.objects):
        if obj.type in {"LIGHT", "CAMERA"}:
            bpy.data.objects.remove(obj, do_unlink=True)
    for datablocks in (bpy.data.lights, bpy.data.cameras, bpy.data.worlds):
        for datablock in list(datablocks):
            datablocks.remove(datablock, do_unlink=True)
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    report["trusted_presentation_sanitization"] = {
        "removed_source_lights": source_light_names,
        "removed_source_cameras": source_camera_names,
        "removed_source_worlds": source_world_names,
        "compositing_enabled": bool(scene.render.use_compositing),
        "sequencer_enabled": bool(scene.render.use_sequencer),
    }
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except (TypeError, ValueError):
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = args.resolution
    scene.render.resolution_y = args.resolution
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    report["evidence_color_management"] = _evidence_color_management(
        scene, category=str(task.get("category") or task.get("track_type") or "")
    )
    _atomic_json(args.scene_report, report)
    world = bpy.data.worlds.new("GEN_V3_Evidence_World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (
        0.025,
        0.032,
        0.045,
        1.0,
    )
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.35
    scene.world = world
    camera_data = bpy.data.cameras.new("GEN_V3_Evidence_Camera")
    camera = bpy.data.objects.new("GEN_V3_Evidence_Camera", camera_data)
    scene.collection.objects.link(camera)
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = radius * 1.65
    camera.location = (
        center
        + Vector(HERO_FACE_DIRECTIONS[visual_contract["hero_face"]]).normalized()
        * radius
        * 3.2
    )
    _look_at(camera, center)
    scene.camera = camera
    # Area-light power and emitter size must scale with the generated asset.
    # Fixed wattage on an emitter sized at four times a large asset produced
    # nearly black evidence, while tiny assets were acceptable by accident.
    light_power_scale = max(radius * radius, 1.0)
    for index, (offset, base_energy, size_factor) in enumerate(
        (
            ((2.5, -3.0, 4.0), 220.0, 0.70),
            ((-3.0, -1.0, 2.5), 120.0, 0.95),
            ((1.0, 3.0, 3.5), 160.0, 0.75),
        )
    ):
        data = bpy.data.lights.new(f"GEN_V3_Evidence_Light_{index}", "AREA")
        data.energy = base_energy * light_power_scale
        data.shape = "DISK"
        data.size = max(size_factor * radius, 0.75)
        obj = bpy.data.objects.new(data.name, data)
        scene.collection.objects.link(obj)
        obj.location = center + Vector(offset).normalized() * radius * 3.0
        _look_at(obj, center)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    def render(relative: str) -> None:
        path = args.evidence_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        if not path.is_file() or path.stat().st_size < 16:
            raise GenerationV3HarnessError(f"Blender did not render {relative}")

    render("presentation.png")
    clay = _material(bpy, "GEN_V3_Clay")
    normal = _material(bpy, "GEN_V3_Normal", normal=True)
    scene.view_layers[0].material_override = clay
    render("clay.png")
    scene.view_layers[0].material_override = normal
    render("normal.png")
    scene.view_layers[0].material_override = None
    elevation = radius * 1.25
    distance = radius * 3.2
    for index in range(args.turntable_frames):
        angle = 2.0 * math.pi * index / args.turntable_frames
        camera.location = center + Vector(
            (math.cos(angle) * distance, math.sin(angle) * distance, elevation)
        )
        _look_at(camera, center)
        render(f"turntable/frame_{index:03d}.png")
    return 0


def _secret(path: Path) -> str:
    import video_replay_model_client as client

    client.validate_paid_api_secret_file(path)
    return client.read_paid_api_secret(path)


def finalize_batch_receipt(
    *,
    tasks: Sequence[GenerationTask],
    results: Sequence[Mapping[str, Any]],
    output_root: Path,
) -> dict[str, Any]:
    if len(tasks) != EXPECTED_TASK_COUNT or len(results) != EXPECTED_TASK_COUNT:
        raise GenerationV3HarnessError(
            "batch receipt requires the exact 50-task release"
        )
    if [task.task_id for task in tasks] != [
        str(row.get("task_id") or "") for row in results
    ]:
        raise GenerationV3HarnessError("batch task/result order drifted")
    for row in results:
        _validate_receipt(row, TASK_RECEIPT_SCHEMA)
    accepted = sum(row["status"] == "web_candidate_accepted" for row in results)
    batch = _receipt(
        {
            "schema": BATCH_RECEIPT_SCHEMA,
            "acceptance_scope": ACCEPTANCE_SCOPE,
            "execution_is_formal": False,
            "sandbox_attested": False,
            "status": (
                "web_candidate_complete"
                if accepted == len(results)
                else "replacement_required"
            ),
            "task_count": len(results),
            "accepted_count": accepted,
            "replacement_required_count": len(results) - accepted,
            "source_tasks_sha256": digest(
                [
                    {
                        "task_id": task.task_id,
                        "source_task_sha256": task.source_payload_sha256,
                        "reference_sha256": task.reference_sha256,
                    }
                    for task in tasks
                ]
            ),
            "task_receipts": [
                {
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "receipt_sha256": row["receipt_sha256"],
                }
                for row in results
            ],
        }
    )
    batch_path = output_root.expanduser().resolve(strict=False) / "batch_result.json"
    if batch_path.exists():
        existing = _validate_receipt(
            _read_json(batch_path, "batch result"), BATCH_RECEIPT_SCHEMA
        )
        if existing != batch:
            raise GenerationV3HarnessError("batch terminal receipt drifted")
        return existing
    _atomic_json(batch_path, batch)
    return batch


def _run_cli(args: argparse.Namespace, tasks: list[GenerationTask]) -> dict[str, Any]:
    backend = PaidChatBackend(
        api_root=args.output_root / "_api",
        ledger_path=args.ledger,
        endpoint=args.endpoint,
        api_key=_secret(args.secret_file),
        code_model=args.code_model,
        judge_model=args.judge_model,
        max_total_calls=args.max_total_calls,
        max_total_tokens=args.max_total_tokens,
        minimum_free_bytes=args.minimum_free_bytes,
        emergency_reserve_bytes=args.emergency_reserve_bytes,
    )
    executor = SubprocessBlenderExecutor(
        args.blender, timeout_seconds=args.blender_timeout, resolution=args.resolution
    )
    selected = (
        tasks
        if args.command == "run-batch"
        else [next((task for task in tasks if task.task_id == args.task_id), None)]
    )
    if selected == [None]:
        raise GenerationV3HarnessError(f"unknown task ID: {args.task_id}")
    results = []
    for task in selected:
        assert isinstance(task, GenerationTask)
        results.append(
            run_task(
                task=task,
                blank_blend=args.blank,
                blank_blend_sha256=args.blank_sha256,
                output_root=args.output_root,
                code_generator=backend.generate,
                blender_executor=executor,
                scorer=backend.score,
                threshold=args.threshold,
            )
        )
    accepted = sum(row["status"] == "web_candidate_accepted" for row in results)
    summary = {
        "acceptance_scope": ACCEPTANCE_SCOPE,
        "execution_is_formal": False,
        "sandbox_attested": False,
        "status": (
            "web_candidate_complete"
            if accepted == len(results)
            else "replacement_required"
        ),
        "task_count": len(results),
        "accepted_count": accepted,
        "replacement_required_count": len(results) - accepted,
        "tasks": results,
    }
    if args.command != "run-batch":
        return summary
    return finalize_batch_receipt(
        tasks=tasks, results=results, output_root=args.output_root
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_blank = subparsers.add_parser("create-blank")
    create_blank.add_argument("--blender", type=Path, required=True)
    create_blank.add_argument("--output", type=Path, required=True)
    create_blank.add_argument("--timeout", type=int, default=300)
    validate = subparsers.add_parser("validate-index")
    validate.add_argument("--tasks", type=Path, required=True)
    validate.add_argument("--reference-root", type=Path)
    manual_reject = subparsers.add_parser("manual-reject")
    manual_reject.add_argument("--tasks", type=Path, required=True)
    manual_reject.add_argument("--reference-root", type=Path)
    manual_reject.add_argument("--output-root", type=Path, required=True)
    manual_reject.add_argument("--task-id", required=True)
    manual_reject.add_argument("--attempt", type=int, required=True)
    manual_reject.add_argument("--expected-task-result-sha256", required=True)
    manual_reject.add_argument("--expected-evidence-sha256", required=True)
    manual_reject.add_argument("--reason", required=True)
    manual_disqualify = subparsers.add_parser("manual-disqualify")
    manual_disqualify.add_argument("--tasks", type=Path, required=True)
    manual_disqualify.add_argument("--reference-root", type=Path)
    manual_disqualify.add_argument("--output-root", type=Path, required=True)
    manual_disqualify.add_argument("--task-id", required=True)
    manual_disqualify.add_argument("--attempt", type=int, required=True)
    manual_disqualify.add_argument("--expected-task-result-sha256", required=True)
    manual_disqualify.add_argument("--expected-evidence-sha256", required=True)
    manual_disqualify.add_argument("--reason", required=True)
    for name in ("run-task", "run-batch"):
        command = subparsers.add_parser(name)
        command.add_argument("--tasks", type=Path, required=True)
        command.add_argument("--reference-root", type=Path)
        command.add_argument("--blank", type=Path, required=True)
        command.add_argument("--blank-sha256", required=True)
        command.add_argument("--output-root", type=Path, required=True)
        command.add_argument("--blender", type=Path, required=True)
        command.add_argument("--ledger", type=Path, required=True)
        command.add_argument("--secret-file", type=Path, required=True)
        command.add_argument("--endpoint", required=True)
        command.add_argument("--code-model", required=True)
        command.add_argument("--judge-model", required=True)
        command.add_argument("--max-total-calls", type=int, required=True)
        command.add_argument("--max-total-tokens", type=int, required=True)
        command.add_argument("--minimum-free-bytes", type=int, default=0)
        command.add_argument("--emergency-reserve-bytes", type=int, default=0)
        command.add_argument("--blender-timeout", type=int, default=1800)
        command.add_argument("--resolution", type=int, default=512)
        command.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
        if name == "run-task":
            command.add_argument("--task-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    values = (
        list(sys.argv[sys.argv.index("--") + 1 :])
        if argv is None and "--" in sys.argv
        else list(argv)
        if argv is not None
        else sys.argv[1:]
    )
    if values and values[0] == "_blender-build":
        return _blender_build(values[1:])
    if values and values[0] == "_blender-create-blank":
        return _blender_create_blank(values[1:])
    if values and values[0] == "_blender-evidence":
        return _blender_evidence(values[1:])
    parser = build_parser()
    args = parser.parse_args(values)
    if args.command == "create-blank":
        sys.stdout.buffer.write(
            canonical_bytes(
                create_canonical_blank(
                    blender=args.blender,
                    output=args.output,
                    timeout_seconds=args.timeout,
                )
            )
        )
        return 0
    tasks = load_generation_index(
        args.tasks,
        reference_root=args.reference_root,
        expected_count=EXPECTED_TASK_COUNT,
    )
    if args.command == "validate-index":
        result = {
            "status": "valid",
            "schema": TASK_INDEX_SCHEMA,
            "task_count": len(tasks),
            "task_ids_sha256": digest([task.task_id for task in tasks]),
            "references_sha256": digest([task.reference_sha256 for task in tasks]),
        }
    elif args.command == "manual-reject":
        task = next((row for row in tasks if row.task_id == args.task_id), None)
        if task is None:
            raise GenerationV3HarnessError(f"unknown task ID: {args.task_id}")
        result = reject_accepted_attempt(
            task=task,
            output_root=args.output_root,
            attempt=args.attempt,
            expected_task_result_sha256=args.expected_task_result_sha256,
            expected_evidence_sha256=args.expected_evidence_sha256,
            reason=args.reason,
        )
    elif args.command == "manual-disqualify":
        task = next((row for row in tasks if row.task_id == args.task_id), None)
        if task is None:
            raise GenerationV3HarnessError(f"unknown task ID: {args.task_id}")
        result = disqualify_accepted_task(
            task=task,
            output_root=args.output_root,
            attempt=args.attempt,
            expected_task_result_sha256=args.expected_task_result_sha256,
            expected_evidence_sha256=args.expected_evidence_sha256,
            reason=args.reason,
        )
    else:
        result = _run_cli(args, tasks)
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GenerationV3HarnessError as exc:
        sys.stderr.write(f"error: {exc}\n")
        raise SystemExit(2)
