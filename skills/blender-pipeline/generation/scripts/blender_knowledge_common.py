from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - the production knowledge store is POSIX-only.
    fcntl = None

from project_paths import OUTPUT_ROOT, PROJECT_ROOT


PROJECT = PROJECT_ROOT
KNOWLEDGE_ROOT = Path(
    os.environ.get("BLENDER_KNOWLEDGE_ROOT", OUTPUT_ROOT / "blender_knowledge_store")
)
MANIFEST_PATH = KNOWLEDGE_ROOT / "blender_knowledge_manifest.jsonl"
QDRANT_PATH = KNOWLEDGE_ROOT / "qdrant"
BUILDS_PATH = KNOWLEDGE_ROOT / "builds"
CURRENT_BUILD_PATH = KNOWLEDGE_ROOT / "current.json"
STORE_LOCK_PATH = KNOWLEDGE_ROOT / ".knowledge_store.lock"
COLLECTION_NAME = os.environ.get(
    "BLENDER_KNOWLEDGE_COLLECTION", "blender_pipeline_knowledge"
)
EMBED_MODEL = os.environ.get(
    "BLENDER_KNOWLEDGE_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
ADMISSION_SCHEMA = "blender-knowledge-admission.v1"
SUCCESS_OUTCOMES = {"accepted", "pass", "qa_passed"}


class KnowledgeManifestError(RuntimeError):
    pass


ASSET_FAMILIES = {
    "hair_fur": [
        "hair",
        "fur",
        "毛发",
        "毛球",
        "毛绒",
        "绒毛",
        "particle hair",
        "principled hair",
    ],
    "cloth": [
        "cloth",
        "soft body",
        "softbody",
        "布料",
        "织物",
        "膨胀",
        "充气",
        "pressure",
    ],
    "fluid": [
        "fluid",
        "water",
        "ripple",
        "rain",
        "droplet",
        "水体",
        "水面",
        "水流",
        "水滴",
        "涟漪",
        "雨滴",
        "液体",
        "海浪",
        "气泡",
    ],
    "scientific": [
        "scientific",
        "科研",
        "virus",
        "病毒",
        "bacteria",
        "细菌",
        "mitochondria",
        "线粒体",
        "molecule",
        "protein",
        "分子",
        "纳米",
        "graphene",
        "石墨烯",
    ],
    "character": [
        "character",
        "角色",
        "小猫",
        "企鹅",
        "玩偶",
        "人物",
        "girl",
        "cat",
        "cartoon",
    ],
    "food": [
        "food",
        "排骨",
        "蛋挞",
        "冰淇淋",
        "水蜜桃",
        "柠檬",
        "热狗",
        "威化",
        "苏打",
    ],
    "product": ["product", "产品", "咖啡机", "手环", "CNC", "vending", "贩卖机"],
    "architecture": [
        "building",
        "room",
        "interior",
        "city",
        "house",
        "建筑",
        "室内",
        "别墅",
        "城市",
        "街景",
    ],
    "material_shader": [
        "shader",
        "material",
        "材质",
        "BSDF",
        "node",
        "节点",
        "glass",
        "ice",
        "透明",
        "折射",
    ],
    "motion": [
        "animation",
        "motion",
        "keyframe",
        "camera animation",
        "turntable",
        "动画",
        "关键帧",
        "镜头动画",
        "转台",
    ],
}

PIPELINE_STAGES = {
    "tutorial_extraction": [
        "tutorial",
        "OCR",
        "Q-Gate",
        "关键帧",
        "字幕",
        "图文教程",
        "evidence",
        "IMAGE_ID",
    ],
    "asset_generation": [
        "Blender Python",
        "script",
        "execution plan",
        "生成",
        "资产",
        "template",
        "compiler",
    ],
    "review": ["review", "QA", "audit", "constraint", "检查", "验收", "local review"],
    "repair": ["repair", "fix", "修复", "fallback", "regression"],
    "render": [
        "render",
        "animation.mp4",
        "Cycles",
        "CUDA",
        "渲染",
        "4K",
        "120fps",
        "ffmpeg",
    ],
}

BLENDER_FEATURES = {
    "particle_hair": [
        "particle hair",
        "hair dynamics",
        "毛发动力学",
        "Principled Hair",
        "ShaderNodeBsdfHairPrincipled",
        "clump",
        "brownian",
    ],
    "geometry_nodes": ["geometry nodes", "几何节点", "node tree"],
    "cloth_sim": ["cloth", "soft body", "pressure", "布料", "织物", "膨胀", "充气"],
    "shader_nodes": ["shader", "BSDF", "material", "node", "材质", "节点"],
    "camera": ["camera", "framing", "look_at", "摄像机", "镜头", "构图"],
    "lighting": ["light", "lighting", "world", "softbox", "灯光", "照明"],
    "bake": ["bake", "cache", "ptcache", "烘焙", "缓存"],
}


@dataclass
class KnowledgeChunk:
    source_id: str
    text: str
    title: str
    source_path: str
    source_type: str
    knowledge_type: str
    pipeline_stage: str
    asset_family: str
    blender_feature: str
    review_status: str
    tags: list[str]
    source_hash: str
    extra: dict[str, Any]
    schema_version: str = "knowledge-chunk-v1"
    logical_source_id: str = ""

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["logical_source_id"] = self.logical_source_id or self.source_id
        data["text_preview"] = self.text[:800]
        return data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "KnowledgeChunk":
        """Load both the legacy payload and knowledge-chunk-v1."""
        schema_version = str(payload.get("schema_version") or "knowledge-chunk-v1")
        if schema_version != "knowledge-chunk-v1":
            raise KnowledgeManifestError(
                f"unsupported knowledge chunk schema: {schema_version}"
            )
        source_id = str(
            payload.get("source_id") or payload.get("logical_source_id") or ""
        )
        return cls(
            source_id=source_id,
            text=str(payload.get("text") or payload.get("text_preview") or ""),
            title=str(payload.get("title") or ""),
            source_path=str(payload.get("source_path") or ""),
            source_type=str(payload.get("source_type") or "api_note"),
            knowledge_type=str(payload.get("knowledge_type") or "semantic"),
            pipeline_stage=str(payload.get("pipeline_stage") or "asset_generation"),
            asset_family=str(payload.get("asset_family") or "other"),
            blender_feature=str(payload.get("blender_feature") or "other"),
            review_status=str(payload.get("review_status") or "candidate"),
            tags=list(payload.get("tags") or []),
            source_hash=str(
                payload.get("source_hash")
                or sha256_text(str(payload.get("text") or ""))
            ),
            extra=dict(payload.get("extra") or {}),
            schema_version=schema_version,
            logical_source_id=str(payload.get("logical_source_id") or source_id),
        )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _current_build_payload() -> dict[str, Any]:
    if not CURRENT_BUILD_PATH.exists():
        return {}
    try:
        payload = json.loads(CURRENT_BUILD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    build_id = str(payload.get("build_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", build_id):
        return {}
    return payload


def _safe_build_path(value: str, fallback_name: str) -> Path | None:
    payload = _current_build_payload()
    build_id = str(payload.get("build_id") or "")
    if not build_id:
        return None
    relative = str(payload.get(value) or f"builds/{build_id}/{fallback_name}")
    candidate = (KNOWLEDGE_ROOT / relative).resolve()
    try:
        candidate.relative_to(KNOWLEDGE_ROOT.resolve())
    except ValueError:
        return None
    return candidate


def active_manifest_path() -> Path:
    candidate = _safe_build_path("manifest", "blender_knowledge_manifest.jsonl")
    return candidate if candidate and candidate.exists() else MANIFEST_PATH


def active_qdrant_path() -> Path | None:
    current = _current_build_payload()
    if current:
        # A manifest-only generation deliberately omits this key. Never fall
        # back to a stale legacy vector store for a newer active manifest.
        if not current.get("qdrant"):
            return None
        candidate = _safe_build_path("qdrant", "qdrant")
        return candidate if candidate and candidate.is_dir() else None
    return QDRANT_PATH if QDRANT_PATH.is_dir() else None


@contextmanager
def knowledge_store_lock(path: Path | None = None) -> Iterator[Path]:
    """Serialize build activation and incremental generation updates."""
    if fcntl is None:
        raise RuntimeError("knowledge store locking requires POSIX fcntl")
    lock_path = path or (KNOWLEDGE_ROOT / STORE_LOCK_PATH.name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def read_jsonl(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or active_manifest_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(),
        start=1,
    ):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KnowledgeManifestError(
                f"invalid JSON in knowledge manifest at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise KnowledgeManifestError(
                f"knowledge manifest row {line_number} is not an object"
            )
        schema_version = row.get("schema_version")
        if schema_version not in (None, "", "knowledge-chunk-v1"):
            raise KnowledgeManifestError(
                f"unsupported knowledge chunk schema at line {line_number}: {schema_version}"
            )
        rows.append(row)
    return rows


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def write_jsonl(rows: Iterable[dict[str, Any]], path: Path | None = None) -> None:
    path = path or active_manifest_path()
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    atomic_write_text(path, text)


def keyword_match(
    text: str, mapping: dict[str, list[str]], default: str = "other"
) -> str:
    best = (default, 0)
    for key, words in mapping.items():
        score = sum(1 for word in words if term_in_text(text, word))
        if score > best[1]:
            best = (key, score)
    return best[0]


def multi_keyword_match(
    text: str, mapping: dict[str, list[str]], default: str = "other"
) -> list[str]:
    matches = []
    for key, words in mapping.items():
        if any(term_in_text(text, word) for word in words):
            matches.append(key)
    return matches or [default]


def term_in_text(text: str, term: str) -> bool:
    needle = term.strip()
    if not needle:
        return False
    if re.search(r"[A-Za-z0-9]", needle):
        escaped = re.escape(needle).replace(r"\ ", r"[\s_-]+")
        return (
            re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", text, flags=re.I)
            is not None
        )
    return needle in text


def infer_asset_family(text: str) -> str:
    # Priority matters: shader/material terms are cross-cutting and should not
    # override a concrete tutorial family such as hair, scientific, or cloth.
    for family in [
        "hair_fur",
        "scientific",
        "cloth",
        "fluid",
        "character",
        "food",
        "product",
        "architecture",
    ]:
        if any(term_in_text(text, word) for word in ASSET_FAMILIES[family]):
            return family
    if any(term_in_text(text, word) for word in ASSET_FAMILIES["material_shader"]):
        return "material_shader"
    if any(term_in_text(text, word) for word in ASSET_FAMILIES["motion"]):
        return "motion"
    return "other"


def infer_pipeline_stage(text: str) -> str:
    return keyword_match(text, PIPELINE_STAGES, "asset_generation")


def infer_blender_feature(text: str) -> str:
    return keyword_match(text, BLENDER_FEATURES, "other")


def infer_tags(text: str) -> list[str]:
    tags = set()
    for key in multi_keyword_match(text, ASSET_FAMILIES):
        if key != "other":
            tags.add(key)
    for key in multi_keyword_match(text, BLENDER_FEATURES):
        if key != "other":
            tags.add(key)
    for key in multi_keyword_match(text, PIPELINE_STAGES):
        tags.add(key)
    return sorted(tags)


def source_type_for_path(path: Path) -> str:
    s = str(path)
    if "blender-skills" in s:
        return "skill"
    if "paper-summaries" in s:
        return "paper"
    if "output/blender_tutorial_replay" in s:
        return "run_artifact"
    return "api_note"


def default_review_status(source_type: str, text: str, source_path: str = "") -> str:
    # Content cannot authorize itself. Promotion is applied only from a
    # separate structured approval registry after holdout evaluation.
    if "deprecated" in text.lower() or "弃用" in text:
        return "deprecated"
    return "candidate"


def active_knowledge_eligible(row: dict[str, Any]) -> bool:
    """Admission is structured provenance, never a favorable word in the text."""

    extra = row.get("extra") or {}
    if not isinstance(extra, dict):
        return False
    admission = extra.get("admission") or {}
    if not isinstance(admission, dict) or admission.get("schema") != ADMISSION_SCHEMA:
        return False
    if not row.get("source_hash") or admission.get("source_hash") != row["source_hash"]:
        return False
    body = row.get("text")
    if isinstance(body, str) and sha256_text(body) != row["source_hash"]:
        return False
    if row.get("review_status") == "curated":
        return bool(
            row.get("source_type") == "skill"
            and admission.get("kind") == "curated_guidance"
            and admission.get("provenance") == "packaged_manifest"
        )
    if row.get("review_status") != "reviewed":
        return False
    decision = admission.get("promotion_decision") or {}
    scope = admission.get("scope") or {}
    if not isinstance(decision, dict) or not isinstance(scope, dict):
        return False
    assets = decision.get("candidate_asset_ids") or []
    holdout = decision.get("accepted_holdout_asset_ids") or []
    if not isinstance(assets, list) or not isinstance(holdout, list):
        return False
    if any(not isinstance(value, str) or not value for value in assets + holdout):
        return False
    proof_hash = decision.get("evidence_sha256")
    core = {
        key: value
        for key, value in decision.items()
        if key not in {"eligible_for_reviewed", "evidence_sha256"}
    }
    if proof_hash != sha256_text(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ):
        return False
    return bool(
        admission.get("kind") == "reviewed_success"
        and extra.get("outcome") in SUCCESS_OUTCOMES
        and decision.get("eligible_for_reviewed") is True
        and not decision.get("blockers")
        and len(set(assets)) >= 5
        and holdout
        and not set(assets).intersection(holdout)
        and bool(decision.get("candidate_rule_id"))
        and decision.get("candidate_rule_id") == extra.get("candidate_rule_id")
        and all(
            str(scope.get(key) or "").strip()
            for key in ("route", "asset_family", "blender_version")
        )
    )


def infer_knowledge_type(text: str, source_type: str) -> str:
    lower = text.lower()
    if source_type == "paper":
        return "semantic"
    if source_type == "run_artifact" and any(
        word in lower
        for word in ["failure", "failed", "修复", "失败", "regression", "case"]
    ):
        return "episodic"
    if any(
        word in lower
        for word in ["workflow", "step", "pipeline", "template", "gate", "流程", "步骤"]
    ):
        return "procedural"
    return "semantic"


def _logical_path(source_path: Path) -> str:
    try:
        resolved = source_path.resolve()
    except OSError:
        resolved = source_path.absolute()
    try:
        # Preserve the historical canonical-repository identity format.
        return resolved.relative_to(PROJECT.resolve()).as_posix()
    except (OSError, ValueError):
        pass
    for prefix, root in (
        ("output", OUTPUT_ROOT),
        ("workspace", PROJECT.parent),
    ):
        try:
            relative = resolved.relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        return f"{prefix}/{relative}"
    # Basename-only fallback collapses unrelated external artifacts such as
    # many ``fast_asset_spec.json`` files into one logical source. Retain the
    # normalized absolute identity for unconfigured external roots instead.
    return f"external/{resolved.as_posix().lstrip('/')}"


def make_chunk(
    source_path: Path,
    title: str,
    text: str,
    source_type: str | None = None,
    extra: dict[str, Any] | None = None,
    logical_key: str = "",
) -> KnowledgeChunk:
    clean = re.sub(r"\n{3,}", "\n\n", text.strip())
    stype = source_type or source_type_for_path(source_path)
    source_hash = sha256_text(clean)
    combined = f"{title}\n{clean}"
    logical_identity = (
        logical_key.strip() or f"{stype}:{_logical_path(source_path)}:{title.strip()}"
    )
    source_id = sha256_text(logical_identity)[:24]
    return KnowledgeChunk(
        source_id=source_id,
        text=clean,
        title=title.strip() or source_path.name,
        source_path=str(source_path),
        source_type=stype,
        knowledge_type=infer_knowledge_type(combined, stype),
        pipeline_stage=infer_pipeline_stage(combined),
        asset_family=infer_asset_family(combined),
        blender_feature=infer_blender_feature(combined),
        review_status=default_review_status(stype, combined, str(source_path)),
        tags=infer_tags(combined),
        source_hash=source_hash,
        extra=extra or {},
        logical_source_id=source_id,
    )


def split_markdown_text(
    text: str, *, fallback_title: str, max_chars: int = 2200
) -> list[tuple[str, str]]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    matches = []
    fence_character = ""
    fence_length = 0
    for match in re.finditer(r"^.*$", text, flags=re.M):
        line = match.group(0)
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            token, tail = fence.groups()
            if not fence_character:
                fence_character, fence_length = token[0], len(token)
            elif (
                token[0] == fence_character
                and len(token) >= fence_length
                and not tail.strip()
            ):
                fence_character = ""
            continue
        if not fence_character:
            heading = re.match(r"^(#{1,4})\s+(.+)$", line)
            if heading:
                matches.append((match.start(), heading.group(2).strip()))
    if not matches:
        return [
            (fallback_title + (f" part {index + 1}" if index else ""), part)
            for index, part in enumerate(split_long_text(text, max_chars=max_chars))
        ]
    sections: list[tuple[str, str]] = []
    preamble = text[: matches[0][0]].strip()
    if preamble:
        sections.extend(
            split_markdown_text(
                preamble, fallback_title=fallback_title, max_chars=max_chars
            )
        )
    title_occurrences: dict[str, int] = {}
    for idx, (start, title) in enumerate(matches):
        title_occurrences[title] = title_occurrences.get(title, 0) + 1
        if title_occurrences[title] > 1:
            title = f"{title} occurrence {title_occurrences[title]}"
        end = matches[idx + 1][0] if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        for part_idx, part in enumerate(split_long_text(body, max_chars=max_chars)):
            suffix = f" part {part_idx + 1}" if part_idx else ""
            sections.append((title + suffix, part))
    return sections


def split_markdown_sections(path: Path, max_chars: int = 2200) -> list[tuple[str, str]]:
    return split_markdown_text(
        path.read_text(encoding="utf-8", errors="strict"),
        fallback_title=path.name,
        max_chars=max_chars,
    )


def split_long_text(text: str, max_chars: int = 2200) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in re.split(r"\n\s*\n", text):
        if len(para) > max_chars:
            if current:
                parts.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            # A single giant paragraph must not defeat the documented bound.
            parts.extend(
                para[start : start + max_chars]
                for start in range(0, len(para), max_chars)
            )
            continue
        if current and current_len + len(para) + 2 > max_chars:
            parts.append("\n\n".join(current).strip())
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2
    if current:
        parts.append("\n\n".join(current).strip())
    return parts


def load_embedder():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBED_MODEL)


def qdrant_client(path: Path | None = None):
    selected = path or active_qdrant_path()
    if selected is None or not selected.is_dir():
        raise FileNotFoundError("the active knowledge generation has no Qdrant index")

    from qdrant_client import QdrantClient

    return QdrantClient(path=str(selected))


def ensure_script_dir_on_path() -> None:
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
