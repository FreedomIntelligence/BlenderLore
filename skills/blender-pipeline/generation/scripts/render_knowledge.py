from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence


CONTEXT_SCHEMA_VERSION = "render-knowledge-context-v1"
RECIPE_SCHEMA_VERSION = "render-knowledge-recipe-v1"
MATCH_SCHEMA_VERSION = "render-knowledge-match-v1"
APPROVAL_SCHEMA_VERSION = "render-knowledge-approval-v1"
CLASSIFIER_VERSION = "render-knowledge-deterministic-2026-07-16-v1"

SOURCE_KINDS = {"total_asset", "bili_linked_asset", "video_replay"}
ROUTES = {"static", "dynamic_candidate", "dynamic", "not_renderable", "unknown"}
REVIEW_STATUSES = {"candidate", "reviewed", "deprecated"}

SUBJECT_FAMILIES = {
    "human_character",
    "animal_character",
    "vehicle",
    "architecture",
    "interior_furniture",
    "environment",
    "plant",
    "machine",
    "electronics",
    "daily_object",
    "apparel",
    "weapon",
    "food",
    "decorative_sculpture",
    "material",
    "scientific",
    "general_model",
    "unknown",
}

MOTION_MECHANISMS = {
    "object_transform",
    "action_nla",
    "rig_pose",
    "shape_key",
    "time_driver",
    "material_animation",
    "geometry_nodes_time",
    "particle_hair",
    "particle_emitter",
    "cloth_softbody",
    "rigid_body",
    "fluid_liquid",
    "smoke_fire",
    "camera_only",
    "unverified_metadata",
    "none",
}

SUBJECT_MOTION_MECHANISMS = MOTION_MECHANISMS - {
    "camera_only",
    "unverified_metadata",
    "none",
}
SIMULATION_MECHANISMS = {
    "particle_hair",
    "particle_emitter",
    "cloth_softbody",
    "rigid_body",
    "fluid_liquid",
    "smoke_fire",
}

AUDIT_ROUTE_KEYS = {
    "render_route",
    "renderable",
    "has_renderable_objects",
}
AUDIT_MOTION_KEYS = {
    "object_animation",
    "object_animated",
    "animated_objects",
    "animated_object_count",
    "object_fcurves",
    "object_transform_animation",
    "action_count",
    "actions",
    "nla_tracks",
    "nla_track_count",
    "action_nla",
    "bone_animated_armatures",
    "animated_bones",
    "pose_bone_fcurves",
    "bone_animation",
    "rig_pose_animation",
    "animated_shape_keys",
    "shape_key_animation",
    "shape_key_fcurves",
    "shape_key_objects",
    "time_dependent_drivers",
    "time_driver_count",
    "time_drivers",
    "driver_uses_frame",
    "time_driven_objects",
    "animated_materials",
    "material_animation",
    "material_fcurves",
    "geometry_nodes_time",
    "geometry_nodes_animation",
    "animated_geometry_nodes",
    "hair_particle_systems",
    "particle_hair",
    "hair_dynamics",
    "animated_particle_systems",
    "particle_emitter",
    "particle_simulation",
    "dynamic_particle_systems",
    "cloth_simulations",
    "cloth_simulation",
    "soft_body_simulations",
    "softbody_simulation",
    "rigid_body_simulations",
    "rigid_body_animation",
    "rigid_bodies",
    "fluid_liquid",
    "liquid_simulations",
    "fluid_simulation",
    "smoke_simulations",
    "fire_simulations",
    "gas_simulations",
    "smoke_fire",
    "simulations",
    "animated_cameras",
    "camera_animation",
    "camera_fcurves",
    "camera_animated",
}
AUDIT_RUNTIME_KEYS = {
    "source_camera_usable",
    "usable_source_camera",
    "has_render_camera",
    "source_camera",
    "dynamic_downgraded_to_static",
    "visible_motion_failed",
}
AUDIT_ENGINE_VERSION_KEYS = {
    "render_engine",
    "engine",
    "blender_version",
    "source_blender_version",
}
AUDIT_SUBJECT_KEYS = {
    "subject_family",
    "asset_family",
    "content_category",
    "subject_type",
}
AUDIT_SEMANTIC_KEYS = (
    AUDIT_ROUTE_KEYS
    | AUDIT_MOTION_KEYS
    | AUDIT_RUNTIME_KEYS
    | AUDIT_ENGINE_VERSION_KEYS
    | AUDIT_SUBJECT_KEYS
)


class RenderKnowledgeError(ValueError):
    pass


def _normal_token(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", token)


def _normal_tokens(values: Iterable[Any]) -> list[str]:
    return sorted({_normal_token(value) for value in values if _normal_token(value)})


def contains_term(text: str, term: str) -> bool:
    """Match CJK phrases literally and Latin terms on real token boundaries."""
    haystack = text or ""
    needle = term.strip()
    if not needle:
        return False
    if re.search(r"[A-Za-z0-9]", needle):
        escaped = re.escape(needle).replace(r"\ ", r"[\s_-]+")
        return (
            re.search(
                rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", haystack, flags=re.I
            )
            is not None
        )
    return needle in haystack


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(contains_term(text, term) for term in terms)


def contains_positive_term(text: str, term: str) -> bool:
    """Require at least one occurrence not governed by an explicit negation."""

    needle = term.strip()
    if not needle:
        return False
    escaped = re.escape(needle).replace(r"\ ", r"[\s_-]+")
    if re.search(r"[A-Za-z0-9]", needle):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
            re.I,
        )
    else:
        pattern = re.compile(escaped, re.I)
    for match in pattern.finditer(text or ""):
        prefix = (text or "")[max(0, match.start() - 28) : match.start()]
        if re.search(
            r"(?:未|没有|并无|不(?:要|需|会|是|包含|使用|设置)?|"
            r"禁止|避免).{0,10}$",
            prefix,
            re.I,
        ):
            continue
        if re.search(
            r"(?:\bno|\bnot|\bwithout|\bnever|\bdo\s+not|"
            r"\bdoes\s+not|\bdon't|\bdoesn't)(?:\s+\w+){0,4}\s*$",
            prefix,
            re.I,
        ):
            continue
        return True
    return False


def _contains_any_positive(text: str, terms: Iterable[str]) -> bool:
    return any(contains_positive_term(text, term) for term in terms)


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _iter_dicts(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _iter_dicts(child)


def _has_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return _normal_token(value) not in {"", "0", "false", "none", "null", "no"}
    return bool(value)


def _audit_has(audit: Mapping[str, Any], *keys: str) -> bool:
    wanted = {_normal_token(key) for key in keys}
    for item in _iter_dicts(audit):
        for key, value in item.items():
            if _normal_token(key) in wanted and _has_value(value):
                return True
    return False


def _audit_value(audit: Mapping[str, Any], *keys: str) -> Any:
    wanted = {_normal_token(key) for key in keys}
    for item in _iter_dicts(audit):
        for key, value in item.items():
            if _normal_token(key) in wanted and value not in (None, ""):
                return value
    return None


def _audit_has_key(audit: Mapping[str, Any], *keys: str) -> bool:
    wanted = {_normal_token(key) for key in keys}
    return any(
        _normal_token(key) in wanted for item in _iter_dicts(audit) for key in item
    )


def _explicit_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    token = _normal_token(value)
    if token in {"true", "yes", "1", "renderable"}:
        return True
    if token in {"false", "no", "0", "none", "not_renderable"}:
        return False
    return None


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+){0,3}", value or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


_SEMVER_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _semantic_version_key(value: str) -> tuple[int, ...]:
    match = _SEMVER_PATTERN.fullmatch(str(value or "").strip())
    if not match:
        return ()
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    release_rank = 1 if match.group(4) is None else 0
    return major, minor, patch, release_rank


@dataclass
class RenderKnowledgeContext:
    source_kind: str
    route: str = "unknown"
    render_profile: str = "unknown"
    subject_family: str = "unknown"
    motion_mechanisms: list[str] = field(default_factory=lambda: ["none"])
    geometry_traits: list[str] = field(default_factory=list)
    material_traits: list[str] = field(default_factory=list)
    presentation_profile: str = "unknown"
    runtime_traits: list[str] = field(default_factory=list)
    engine: str = ""
    blender_version: str = ""
    confidence: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    classifier_version: str = CLASSIFIER_VERSION
    schema_version: str = CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.source_kind = _normal_token(self.source_kind)
        self.route = _normal_token(self.route)
        self.render_profile = _normal_token(self.render_profile)
        self.subject_family = _normal_token(self.subject_family)
        self.presentation_profile = _normal_token(self.presentation_profile)
        self.engine = _normal_token(self.engine)
        self.motion_mechanisms = _normal_tokens(self.motion_mechanisms)
        self.geometry_traits = _normal_tokens(self.geometry_traits)
        self.material_traits = _normal_tokens(self.material_traits)
        self.runtime_traits = _normal_tokens(self.runtime_traits)
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise RenderKnowledgeError(
                f"unsupported context schema: {self.schema_version}"
            )
        if self.source_kind not in SOURCE_KINDS:
            raise RenderKnowledgeError(f"invalid source_kind: {self.source_kind}")
        if self.route not in ROUTES:
            raise RenderKnowledgeError(f"invalid route: {self.route}")
        if self.subject_family not in SUBJECT_FAMILIES:
            raise RenderKnowledgeError(f"invalid subject_family: {self.subject_family}")
        unknown_motion = set(self.motion_mechanisms) - MOTION_MECHANISMS
        if unknown_motion:
            raise RenderKnowledgeError(
                f"invalid motion mechanisms: {sorted(unknown_motion)}"
            )
        if len(self.motion_mechanisms) > 1 and "none" in self.motion_mechanisms:
            self.motion_mechanisms.remove("none")
        if not self.motion_mechanisms:
            self.motion_mechanisms = ["none"]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RenderKnowledgeContext":
        data = dict(payload)
        data.setdefault("schema_version", CONTEXT_SCHEMA_VERSION)
        return cls(**data)

    def facts(self) -> set[str]:
        facts = {
            f"source:{self.source_kind}",
            f"route:{self.route}",
            f"render_profile:{self.render_profile}",
            f"subject:{self.subject_family}",
            f"presentation:{self.presentation_profile}",
        }
        facts.update(f"motion:{value}" for value in self.motion_mechanisms)
        facts.update(f"geometry:{value}" for value in self.geometry_traits)
        facts.update(f"material:{value}" for value in self.material_traits)
        facts.update(f"runtime:{value}" for value in self.runtime_traits)
        if self.engine:
            facts.add(f"engine:{self.engine}")
        return facts


@dataclass(frozen=True)
class ApprovalEvidenceRecord:
    asset_id: str
    source_kind: str
    holdout: bool
    human_reviewed: bool
    reviewer_identity: str
    outcome: str = "pass"
    rare_dynamic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", str(self.asset_id or "").strip())
        object.__setattr__(self, "source_kind", _normal_token(self.source_kind))
        object.__setattr__(
            self, "reviewer_identity", str(self.reviewer_identity or "").strip()
        )
        object.__setattr__(self, "outcome", _normal_token(self.outcome))
        if not self.asset_id:
            raise RenderKnowledgeError("approval evidence asset_id is required")
        if self.source_kind not in SOURCE_KINDS:
            raise RenderKnowledgeError(
                f"invalid approval evidence source_kind: {self.source_kind}"
            )
        if not isinstance(self.holdout, bool) or not isinstance(
            self.human_reviewed, bool
        ):
            raise RenderKnowledgeError(
                "approval evidence holdout and human_reviewed must be booleans"
            )
        if not isinstance(self.rare_dynamic, bool):
            raise RenderKnowledgeError(
                "approval evidence rare_dynamic must be a boolean"
            )
        if not self.human_reviewed or not self.reviewer_identity:
            raise RenderKnowledgeError(
                "every approval evidence record must have an identified human review"
            )
        if self.outcome != "pass":
            raise RenderKnowledgeError(
                "approval evidence records must be passing samples"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(
        cls, value: "ApprovalEvidenceRecord | Mapping[str, Any]"
    ) -> "ApprovalEvidenceRecord":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise RenderKnowledgeError("approval evidence record must be an object")
        return cls(**dict(value))


def approval_evidence_digest(
    records: Iterable[ApprovalEvidenceRecord | Mapping[str, Any]],
) -> str:
    normalized = sorted(
        (ApprovalEvidenceRecord.from_value(record).to_dict() for record in records),
        key=lambda record: (record["source_kind"], record["asset_id"]),
    )
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RecipeApproval:
    """Approval supplied by a trusted registry, never by a recipe payload."""

    approval_id: str
    recipe_id: str
    recipe_version: str
    reviewer_identity: str
    approved_at: str
    evidence_digest: str
    validated_assets: int = 0
    validated_source_kinds: list[str] = field(default_factory=list)
    evidence_records: list[dict[str, Any]] = field(default_factory=list)
    evaluation_status: str = "pass"
    schema_version: str = APPROVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_id", str(self.approval_id or "").strip())
        object.__setattr__(self, "recipe_id", _normal_token(self.recipe_id))
        object.__setattr__(
            self, "recipe_version", str(self.recipe_version or "").strip()
        )
        object.__setattr__(
            self, "reviewer_identity", str(self.reviewer_identity or "").strip()
        )
        object.__setattr__(self, "approved_at", str(self.approved_at or "").strip())
        object.__setattr__(
            self, "evidence_digest", str(self.evidence_digest or "").strip().lower()
        )
        try:
            declared_assets = int(self.validated_assets)
        except (TypeError, ValueError) as exc:
            raise RenderKnowledgeError(
                "approval validated_assets must be an integer"
            ) from exc
        declared_sources = _normal_tokens(self.validated_source_kinds)
        records = [
            ApprovalEvidenceRecord.from_value(value) for value in self.evidence_records
        ]
        normalized_records = sorted(
            (record.to_dict() for record in records),
            key=lambda record: (record["source_kind"], record["asset_id"]),
        )
        identities = [
            (record["source_kind"], record["asset_id"]) for record in normalized_records
        ]
        if len(identities) != len(set(identities)):
            raise RenderKnowledgeError(
                "approval evidence assets must be unique per source_kind"
            )
        if normalized_records:
            computed_digest = approval_evidence_digest(normalized_records)
            if computed_digest != self.evidence_digest:
                raise RenderKnowledgeError(
                    "approval evidence_digest does not match evidence_records"
                )
            derived_assets = len(normalized_records)
            derived_sources = sorted(
                {record["source_kind"] for record in normalized_records}
            )
            if declared_assets not in {0, derived_assets}:
                raise RenderKnowledgeError(
                    "approval validated_assets disagrees with evidence_records"
                )
            if declared_sources and declared_sources != derived_sources:
                raise RenderKnowledgeError(
                    "approval validated_source_kinds disagrees with evidence_records"
                )
        else:
            # Legacy scalar claims remain parseable, but are deliberately not
            # trusted for executable promotion.
            derived_assets = 0
            derived_sources = []
        object.__setattr__(self, "evidence_records", normalized_records)
        object.__setattr__(self, "validated_assets", derived_assets)
        object.__setattr__(self, "validated_source_kinds", derived_sources)
        object.__setattr__(
            self, "evaluation_status", _normal_token(self.evaluation_status)
        )
        if self.schema_version != APPROVAL_SCHEMA_VERSION:
            raise RenderKnowledgeError(
                f"unsupported approval schema: {self.schema_version}"
            )
        if not self.approval_id or not self.recipe_id or not self.recipe_version:
            raise RenderKnowledgeError(
                "approval identity and recipe identity are required"
            )
        if not _semantic_version_key(self.recipe_version):
            raise RenderKnowledgeError(
                "approval recipe_version must be semantic versioning"
            )
        if not self.reviewer_identity or not self.approved_at:
            raise RenderKnowledgeError("approval reviewer and timestamp are required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_digest):
            raise RenderKnowledgeError(
                "approval evidence_digest must be a SHA-256 hex digest"
            )
        if declared_assets < 0:
            raise RenderKnowledgeError("approval validated_assets cannot be negative")
        if set(self.validated_source_kinds) - SOURCE_KINDS:
            raise RenderKnowledgeError(
                f"invalid approval source kinds: {self.validated_source_kinds}"
            )
        if self.evaluation_status not in {"pass", "fail"}:
            raise RenderKnowledgeError(
                f"invalid approval evaluation status: {self.evaluation_status}"
            )

    @property
    def registry_key(self) -> str:
        return f"{self.recipe_id}@{self.recipe_version}"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RecipeApproval":
        data = dict(payload)
        data.setdefault("schema_version", APPROVAL_SCHEMA_VERSION)
        return cls(**data)


@dataclass
class RecipeSpec:
    recipe_id: str
    version: str
    review_status: str
    source_kinds: list[str]
    routes: list[str]
    render_profiles: list[str] = field(default_factory=list)
    required_traits: list[str] = field(default_factory=list)
    excluded_traits: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    min_blender_version: str = ""
    max_blender_version: str = ""
    presentation_profile: str = ""
    requires_source_preservation: bool = False
    priority: int = 0
    validated_assets: int = 0
    validated_source_kinds: list[str] = field(default_factory=list)
    human_reviewed: bool = False
    rare_dynamic: bool = False
    quality_gates: list[str] = field(default_factory=list)
    fallback_recipe_id: str = ""
    schema_version: str = RECIPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.recipe_id = _normal_token(self.recipe_id)
        self.version = str(self.version or "").strip()
        self.review_status = _normal_token(self.review_status)
        self.source_kinds = _normal_tokens(self.source_kinds)
        self.routes = _normal_tokens(self.routes)
        self.render_profiles = _normal_tokens(self.render_profiles)
        self.required_traits = _normal_tokens(self.required_traits)
        self.excluded_traits = _normal_tokens(self.excluded_traits)
        self.engines = _normal_tokens(self.engines)
        self.validated_source_kinds = _normal_tokens(self.validated_source_kinds)
        self.presentation_profile = _normal_token(self.presentation_profile)
        self.fallback_recipe_id = _normal_token(self.fallback_recipe_id)
        if self.schema_version != RECIPE_SCHEMA_VERSION:
            raise RenderKnowledgeError(
                f"unsupported recipe schema: {self.schema_version}"
            )
        if not self.recipe_id:
            raise RenderKnowledgeError("recipe_id is required")
        if not _semantic_version_key(self.version):
            raise RenderKnowledgeError("recipe version must be semantic versioning")
        if self.review_status not in REVIEW_STATUSES:
            raise RenderKnowledgeError(f"invalid review_status: {self.review_status}")
        if set(self.source_kinds) - SOURCE_KINDS:
            raise RenderKnowledgeError(
                f"invalid recipe source kinds: {self.source_kinds}"
            )
        if set(self.routes) - ROUTES:
            raise RenderKnowledgeError(f"invalid recipe routes: {self.routes}")
        overlap = set(self.required_traits) & set(self.excluded_traits)
        if overlap:
            raise RenderKnowledgeError(
                f"traits cannot be both required and excluded: {sorted(overlap)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RecipeSpec":
        data = dict(payload)
        if "review_status" not in data and "status" in data:
            data["review_status"] = data.pop("status")
        data.setdefault("schema_version", RECIPE_SCHEMA_VERSION)
        return cls(**data)

    def promotion_blockers(self, approval: RecipeApproval | None = None) -> list[str]:
        if self.review_status == "deprecated":
            return ["recipe_is_deprecated"]
        if self.review_status == "candidate":
            return ["candidate_is_advisory_only"]
        blockers: list[str] = []
        # Recipe fields are descriptive and cannot authorize their own promotion.
        # Approval must come from a separate trusted registry supplied by the caller.
        if approval is None:
            return ["approval_provenance_required"]
        if (
            approval.recipe_id != self.recipe_id
            or approval.recipe_version != self.version
        ):
            blockers.append("approval_recipe_identity_mismatch")
        if approval.evaluation_status != "pass":
            blockers.append("approval_evaluation_not_passed")
        records = list(approval.evidence_records)
        if not records:
            blockers.append("approval_evidence_records_required")
        validated_scope = {
            str(record["source_kind"])
            for record in records
            if str(record["source_kind"]) in set(self.source_kinds)
        }
        cross_pipeline = len(self.source_kinds) > 1
        if cross_pipeline:
            holdout_records = [
                record for record in records if record["holdout"] is True
            ]
            if len(holdout_records) < 20:
                blockers.append("cross_pipeline_requires_20_holdout_assets")
            holdout_sources = {
                str(record["source_kind"])
                for record in holdout_records
                if str(record["source_kind"]) in set(self.source_kinds)
            }
            if len(holdout_sources) < 2:
                blockers.append("cross_pipeline_requires_two_source_kinds")
        else:
            if not validated_scope:
                blockers.append("approval_source_scope_mismatch")
            source_records = [
                record
                for record in records
                if str(record["source_kind"]) in set(self.source_kinds)
            ]
            if len(source_records) < 5:
                blockers.append("source_specific_requires_five_assets")
        if self.rare_dynamic:
            rare_records = [
                record for record in records if record["rare_dynamic"] is True
            ]
            if len(rare_records) < 5:
                blockers.append("rare_dynamic_requires_five_reviewed_assets")
        return blockers


@dataclass
class RecipeMatch:
    context: RenderKnowledgeContext
    recipe_id: str = ""
    recipe_version: str = ""
    confidence: float = 0.0
    decision: str = "abstain"
    reasons: list[str] = field(default_factory=list)
    candidate_recipe_ids: list[str] = field(default_factory=list)
    deprecated_recipe_ids: list[str] = field(default_factory=list)
    blocked_recipes: dict[str, list[str]] = field(default_factory=dict)
    approval_id: str = ""
    fallback_reason: str = ""
    schema_version: str = MATCH_SCHEMA_VERSION

    @property
    def executable(self) -> bool:
        return self.decision == "matched" and bool(self.recipe_id)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["executable"] = self.executable
        return data


SUBJECT_TERMS: list[tuple[str, list[str]]] = [
    (
        "human_character",
        [
            "human",
            "person",
            "portrait",
            "girl",
            "boy",
            "人物",
            "人像",
            "角色",
            "女孩",
            "男孩",
            "人体",
        ],
    ),
    (
        "animal_character",
        [
            "animal",
            "cat",
            "dog",
            "bird",
            "creature",
            "动物",
            "猫",
            "狗",
            "鸟",
            "生物",
            "宠物",
        ],
    ),
    (
        "vehicle",
        [
            "vehicle",
            "car",
            "truck",
            "motorcycle",
            "aircraft",
            "ship",
            "车辆",
            "汽车",
            "卡车",
            "摩托",
            "飞机",
            "船",
        ],
    ),
    (
        "architecture",
        ["architecture", "building", "house", "tower", "建筑", "楼房", "别墅", "房屋"],
    ),
    (
        "interior_furniture",
        [
            "interior",
            "furniture",
            "chair",
            "sofa",
            "table",
            "room",
            "室内",
            "家具",
            "椅子",
            "沙发",
            "桌子",
            "房间",
        ],
    ),
    (
        "environment",
        [
            "environment",
            "landscape",
            "city",
            "street",
            "terrain",
            "环境",
            "自然场景",
            "大场景",
            "城市",
            "街景",
            "地形",
        ],
    ),
    ("plant", ["plant", "tree", "flower", "grass", "植物", "树", "花", "草"]),
    (
        "machine",
        [
            "machine",
            "robot",
            "industrial",
            "mechanical",
            "机械",
            "机器",
            "机器人",
            "工业",
        ],
    ),
    (
        "electronics",
        [
            "electronics",
            "camera",
            "phone",
            "computer",
            "device",
            "电子",
            "相机",
            "手机",
            "电脑",
            "设备",
        ],
    ),
    (
        "apparel",
        [
            "apparel",
            "garment",
            "dress",
            "shirt",
            "shoe",
            "clothing",
            "服饰",
            "衣服",
            "裙子",
            "衬衫",
            "鞋",
        ],
    ),
    ("weapon", ["weapon", "sword", "gun", "rifle", "刀", "剑", "枪", "武器"]),
    (
        "food",
        ["food", "fruit", "cake", "bread", "食品", "食物", "水果", "蛋糕", "面包"],
    ),
    (
        "decorative_sculpture",
        ["sculpture", "statue", "ornament", "sculpt", "雕塑", "雕像", "摆件", "装饰"],
    ),
    (
        "scientific",
        [
            "scientific",
            "molecule",
            "protein",
            "virus",
            "laboratory",
            "科学",
            "分子",
            "蛋白质",
            "病毒",
            "实验",
        ],
    ),
    (
        "material",
        [
            "material",
            "shader",
            "texture",
            "材质",
            "着色器",
            "纹理",
            "贴图",
            "材质节点",
            "着色节点",
        ],
    ),
    (
        "daily_object",
        [
            "product",
            "appliance",
            "bottle",
            "cup",
            "lamp",
            "产品",
            "家电",
            "瓶",
            "杯",
            "灯",
        ],
    ),
]


def infer_subject_family(*texts: str) -> str:
    for text in texts:
        if not text:
            continue
        for family, terms in SUBJECT_TERMS:
            if _contains_any(text, terms):
                return family
        if _contains_any(text, ["model", "asset", "object", "模型", "资产", "物体"]):
            return "general_model"
    return "unknown"


def _mechanisms_from_audit(audit: Mapping[str, Any]) -> list[str]:
    mechanisms: set[str] = set()
    key_groups = {
        "object_transform": (
            "object_animation",
            "object_animated",
            "object_fcurves",
            "object_transform_animation",
        ),
        "action_nla": (
            "action_count",
            "actions",
            "nla_tracks",
            "nla_track_count",
            "action_nla",
        ),
        "rig_pose": (
            "bone_animated_armatures",
            "animated_bones",
            "pose_bone_fcurves",
            "bone_animation",
            "rig_pose_animation",
        ),
        "shape_key": (
            "animated_shape_keys",
            "shape_key_animation",
            "shape_key_fcurves",
            "shape_key_objects",
        ),
        "time_driver": (
            "time_dependent_drivers",
            "time_driver_count",
            "time_drivers",
            "driver_uses_frame",
            "time_driven_objects",
        ),
        "material_animation": (
            "animated_materials",
            "material_animation",
            "material_fcurves",
        ),
        "geometry_nodes_time": (
            "geometry_nodes_time",
            "geometry_nodes_animation",
            "animated_geometry_nodes",
        ),
        "particle_hair": ("hair_particle_systems", "particle_hair", "hair_dynamics"),
        "particle_emitter": (
            "animated_particle_systems",
            "particle_emitter",
            "particle_simulation",
            "dynamic_particle_systems",
        ),
        "cloth_softbody": (
            "cloth_simulations",
            "cloth_simulation",
            "soft_body_simulations",
            "softbody_simulation",
        ),
        "rigid_body": (
            "rigid_body_simulations",
            "rigid_body_animation",
            "rigid_bodies",
        ),
        "fluid_liquid": ("fluid_liquid", "liquid_simulations", "fluid_simulation"),
        "smoke_fire": (
            "smoke_simulations",
            "fire_simulations",
            "gas_simulations",
            "smoke_fire",
        ),
    }
    for mechanism, keys in key_groups.items():
        if _audit_has(audit, *keys):
            mechanisms.add(mechanism)
    dynamic_particles = _audit_value(audit, "dynamic_particle_systems") or []
    if isinstance(dynamic_particles, list) and any(
        ":HAIR" in str(item).upper() for item in dynamic_particles
    ):
        mechanisms.discard("particle_emitter")
        mechanisms.add("particle_hair")
        if any(":HAIR" not in str(item).upper() for item in dynamic_particles):
            mechanisms.add("particle_emitter")
    simulations = _audit_value(audit, "simulations") or []
    simulation_text = _json_text(simulations).upper()
    if (
        "CLOTH" in simulation_text
        or "SOFT_BODY" in simulation_text
        or "SOFTBODY" in simulation_text
    ):
        mechanisms.add("cloth_softbody")
    if "RIGID_BODY" in simulation_text or "RIGIDBODY" in simulation_text:
        mechanisms.add("rigid_body")
    if "FLUID" in simulation_text or "LIQUID" in simulation_text:
        mechanisms.add("fluid_liquid")
    if any(term in simulation_text for term in ["SMOKE", "FIRE", "GAS"]):
        mechanisms.add("smoke_fire")
    camera_animated = _audit_has(
        audit,
        "animated_cameras",
        "camera_animation",
        "camera_fcurves",
        "camera_animated",
    )
    if camera_animated and not (mechanisms & SUBJECT_MOTION_MECHANISMS):
        mechanisms.add("camera_only")
    return sorted(mechanisms or {"none"})


def _mechanisms_from_text(text: str) -> list[str]:
    mechanisms: set[str] = set()
    patterns = {
        "action_nla": [
            "animation action",
            "action strip",
            "bpy.data.actions",
            "NLA",
            "动作片段",
            "非线性动画",
        ],
        "shape_key": ["shape key", "形态键", "形状键"],
        "material_animation": [
            "animated material",
            "material animation",
            "材质动画",
            "着色器动画",
        ],
        "geometry_nodes_time": [
            "geometry nodes time",
            "scene time",
            "几何节点时间",
            "场景时间",
        ],
        "particle_hair": ["hair dynamics", "particle hair", "毛发动力", "毛发模拟"],
        "particle_emitter": [
            "particle simulation",
            "particle animation",
            "粒子模拟",
            "粒子动画",
        ],
        "rigid_body": ["rigid body", "刚体模拟", "刚体动画"],
        "fluid_liquid": [
            "liquid simulation",
            "fluid simulation",
            "流体模拟",
            "液体模拟",
            "水花模拟",
        ],
        "smoke_fire": [
            "smoke simulation",
            "fire simulation",
            "烟雾模拟",
            "火焰模拟",
            "爆炸模拟",
        ],
        "time_driver": [
            "frame driver",
            "time driver",
            "driver uses frame",
            "时间驱动",
            "帧驱动",
        ],
    }
    for mechanism, terms in patterns.items():
        if _contains_any(text, terms):
            mechanisms.add(mechanism)
    cloth_dynamic = any(
        re.search(pattern, text, flags=re.I)
        for pattern in [
            r"cloth.{0,24}(sim(?:ulation)?|dynamic|physics|inflate|collision|animate)",
            r"(sim(?:ulation)?|dynamic|physics|inflate|collision|animate).{0,24}cloth",
            r"布料.{0,12}(模拟|动力|解算|膨胀|碰撞|动画)",
            r"(模拟|动力|解算|膨胀|碰撞|动画).{0,12}布料",
            r"soft[\s_-]*body",
            r"软体.{0,8}(模拟|动画|动力)",
        ]
    )
    if cloth_dynamic:
        mechanisms.add("cloth_softbody")
    rig_animation = any(
        re.search(pattern, text, flags=re.I)
        for pattern in [
            r"(rig|armature|bone|骨骼|绑定).{0,24}(animation|animate|keyframe|动作|动画|关键帧)",
            r"(animation|animate|keyframe|动作|动画|关键帧).{0,24}(rig|armature|bone|骨骼|绑定)",
        ]
    )
    if rig_animation:
        mechanisms.add("rig_pose")
    transform_animation = any(
        re.search(pattern, text, flags=re.I)
        for pattern in [
            r"object.{0,16}(transform|location|rotation|scale).{0,16}(animation|keyframe)",
            r"物体.{0,12}(移动|旋转|缩放).{0,12}(动画|关键帧)",
        ]
    )
    if transform_animation:
        mechanisms.add("object_transform")
    camera_animated = _contains_any(
        text,
        [
            "camera animation",
            "camera orbit",
            "camera keyframe",
            "镜头动画",
            "相机动画",
            "相机环绕",
        ],
    )
    if camera_animated and not mechanisms:
        mechanisms.add("camera_only")
    return sorted(mechanisms)


def _infer_traits(
    text: str, audit: Mapping[str, Any]
) -> tuple[list[str], list[str], list[str]]:
    geometry: set[str] = set()
    material: set[str] = set()
    runtime: set[str] = set()
    if _contains_any(text, ["thin", "flat plane", "card", "薄片", "平面卡片"]):
        geometry.add("thin_flat")
    if _contains_any(
        text, ["large scene", "environment", "interior", "大场景", "环境", "室内"]
    ):
        geometry.add("large_scene")
    if _contains_any(
        text, ["reference plane", "background plane", "参考图平面", "背景图平面"]
    ):
        geometry.add("reference_plane")
    if _contains_any(text, ["hair geometry", "fur", "毛发", "绒毛"]):
        geometry.add("hair_fur")
    material_groups = {
        "fabric": [
            "cloth material",
            "fabric",
            "textile",
            "布料材质",
            "织物材质",
            "布料纹理",
        ],
        "glass_transmission": [
            "glass",
            "transmission",
            "refraction",
            "玻璃",
            "透射",
            "折射",
        ],
        "volume": ["volume", "volumetric", "体积材质", "体积雾"],
        "hair": ["principled hair", "hair shader", "毛发材质", "毛发着色器"],
        "emissive": ["emission", "emissive", "glow", "发光", "自发光"],
        "subsurface": ["subsurface", "skin shader", "次表面", "皮肤材质"],
        "toon": ["toon", "cel shading", "卡通材质", "赛璐璐"],
        "missing_texture": ["missing texture", "missing image", "贴图丢失", "缺少贴图"],
    }
    for trait, terms in material_groups.items():
        if _contains_any_positive(text, terms):
            material.add(trait)
    if not material and _contains_any_positive(
        text, ["material", "shader", "PBR", "材质", "着色器"]
    ):
        material.add("opaque_pbr")
    if _contains_any(text, ["plugin", "add-on", "addon", "插件"]):
        runtime.add("plugin_dependency")
    if _contains_any(
        text, ["external dependency", "linked library", "外部依赖", "链接库"]
    ):
        runtime.add("external_dependency")
    if _contains_any(text, ["bake", "cache", "烘焙", "缓存"]):
        runtime.add("bake_cache")
    if _audit_has(
        audit,
        "source_camera_usable",
        "usable_source_camera",
        "has_render_camera",
        "source_camera",
    ):
        runtime.add("source_camera")
    return sorted(geometry), sorted(material), sorted(runtime)


def build_render_knowledge_context(
    source_kind: str,
    *,
    scene_audit: Mapping[str, Any] | None = None,
    verified_steps: Any = None,
    catalog: Mapping[str, Any] | None = None,
    title: str = "",
    tutorial: str = "",
) -> RenderKnowledgeContext:
    audit = dict(scene_audit or {})
    metadata = dict(catalog or {})
    audit_is_semantic = bool(audit) and _audit_has_key(audit, *AUDIT_SEMANTIC_KEYS)
    steps_present = (
        bool(verified_steps)
        if not isinstance(verified_steps, str)
        else bool(verified_steps.strip())
    )
    audit_text = _json_text(audit) if audit_is_semantic else ""
    steps_text = _json_text(verified_steps) if steps_present else ""
    metadata_text = _json_text(metadata)
    title_text = str(title or "")
    tutorial_text = str(tutorial or "")

    evidence: list[dict[str, Any]] = []
    if audit_is_semantic:
        evidence.append({"source": "scene_audit", "priority": 4, "deterministic": True})
    if steps_present:
        evidence.append(
            {"source": "verified_steps", "priority": 3, "deterministic": False}
        )
    if metadata:
        evidence.append({"source": "catalog", "priority": 2, "deterministic": False})
    if title_text or tutorial_text:
        evidence.append(
            {"source": "title_tutorial", "priority": 1, "deterministic": False}
        )

    audit_subject_text = _json_text(
        _audit_value(
            audit,
            "subject_family",
            "asset_family",
            "content_category",
            "subject_type",
        )
    )
    subject_family = infer_subject_family(
        audit_subject_text,
        steps_text,
        metadata_text,
        title_text,
        tutorial_text,
    )
    audit_motion_known = audit_is_semantic and _audit_has_key(audit, *AUDIT_MOTION_KEYS)
    if audit_motion_known:
        mechanisms = _mechanisms_from_audit(audit)
        if mechanisms == ["none"] and _audit_has(
            audit, "animated_objects", "animated_object_count"
        ):
            # Legacy audits collapsed several mechanisms (including generic
            # drivers) into a single count. Preserve the signal as uncertain
            # metadata, never as a proven object transform.
            mechanisms = ["unverified_metadata"]
    elif steps_present:
        mechanisms = _mechanisms_from_text(steps_text)
    else:
        mechanisms = _mechanisms_from_text("\n".join([title_text, tutorial_text]))

    metadata_route = _normal_token(
        metadata.get("render_route") or metadata.get("route")
    )
    audited_route = _normal_token(_audit_value(audit, "render_route"))
    renderable = _audit_value(audit, "renderable", "has_renderable_objects")
    renderable_state = _explicit_bool(renderable)
    if audited_route == "not_renderable" or renderable_state is False:
        route = "not_renderable"
        route_source = "scene_audit"
    elif audited_route in {"static", "dynamic"}:
        route = audited_route
        route_source = "scene_audit"
    elif renderable_state is True:
        if set(mechanisms) & SUBJECT_MOTION_MECHANISMS:
            route = "dynamic"
        elif "unverified_metadata" in mechanisms:
            route = "dynamic_candidate"
        else:
            route = "static"
        route_source = "scene_audit" if audit_motion_known else "scene_audit_partial"
    elif audit_motion_known:
        if set(mechanisms) & SUBJECT_MOTION_MECHANISMS:
            route = "dynamic"
        elif "unverified_metadata" in mechanisms:
            route = "dynamic_candidate"
        else:
            route = "static"
        route_source = "scene_audit"
    elif steps_present and set(mechanisms) & SUBJECT_MOTION_MECHANISMS:
        route = "dynamic_candidate"
        route_source = "verified_steps"
    elif steps_present:
        route = "static"
        route_source = "verified_steps"
    elif metadata_route == "not_renderable":
        route = "not_renderable"
        route_source = "catalog"
    elif metadata_route in ROUTES:
        route = metadata_route
        route_source = "catalog"
        if route in {"dynamic", "dynamic_candidate"} and mechanisms in ([], ["none"]):
            mechanisms = ["unverified_metadata"]
            route = "dynamic_candidate"
    elif title_text or tutorial_text or steps_text:
        route = (
            "dynamic_candidate"
            if set(mechanisms) & SUBJECT_MOTION_MECHANISMS
            else "static"
        )
        route_source = "title_tutorial"
    else:
        route = "unknown"
        route_source = "none"

    # A producer's coarse route hint cannot override audited absence of
    # subject motion. Camera-only animation and non-time-dependent drivers are
    # presentation/runtime facts, not a dynamic subject route.
    if (
        route == "dynamic"
        and audit_motion_known
        and not (set(mechanisms) & SUBJECT_MOTION_MECHANISMS)
    ):
        route = "dynamic_candidate" if "unverified_metadata" in mechanisms else "static"

    if not mechanisms:
        mechanisms = ["none"]
    geometry: list[str] = []
    material: list[str] = []
    runtime: list[str] = []
    trait_sources: list[tuple[str, Mapping[str, Any]]] = []
    if audit_is_semantic:
        trait_sources.append((audit_text, audit))
    if steps_present:
        trait_sources.append((steps_text, {}))
    if metadata:
        trait_sources.append((metadata_text, {}))
    if title_text or tutorial_text:
        trait_sources.append(("\n".join([title_text, tutorial_text]), {}))
    for trait_text, trait_audit in trait_sources:
        inferred_geometry, inferred_material, inferred_runtime = _infer_traits(
            trait_text,
            trait_audit,
        )
        if not geometry and inferred_geometry:
            geometry = inferred_geometry
        if not material and inferred_material:
            material = inferred_material
        if not runtime and inferred_runtime:
            runtime = inferred_runtime
    if _normal_token(source_kind) == "bili_linked_asset":
        runtime.append("preserve_source")
    if set(mechanisms) & SIMULATION_MECHANISMS:
        runtime.append("simulation")
    if _audit_has(audit, "dynamic_downgraded_to_static", "visible_motion_failed"):
        runtime.append("dynamic_fallback")
        route = "static"

    engine = _normal_token(
        _audit_value(audit, "render_engine", "engine")
        or metadata.get("render_engine_hint")
        or metadata.get("engine")
    )
    version = str(
        _audit_value(audit, "blender_version", "source_blender_version")
        or metadata.get("blender_version")
        or ""
    )

    if route == "dynamic":
        if set(mechanisms) & SIMULATION_MECHANISMS:
            render_profile = "simulation_dynamic"
        elif "source_camera" in runtime:
            render_profile = "source_camera_dynamic"
        else:
            render_profile = "dynamic_union_bounds"
    elif route == "static":
        if "dynamic_fallback" in runtime:
            render_profile = "static_fallback"
        elif subject_family == "material":
            render_profile = "material_detail"
        elif "large_scene" in geometry:
            render_profile = "large_scene"
        elif "source_camera" in runtime:
            render_profile = "source_camera_static"
        else:
            render_profile = "six_view"
    else:
        render_profile = "unknown"

    if route in {"unknown", "not_renderable"}:
        presentation = "unknown"
    elif route == "static" and "dynamic_fallback" in runtime:
        presentation = "studio_turntable"
    elif subject_family in {"human_character", "animal_character"}:
        presentation = "character_loop" if route == "dynamic" else "character_showcase"
    elif subject_family in {"architecture", "interior_furniture", "environment"}:
        presentation = "cinematic_scene"
    elif subject_family == "material":
        presentation = "detail_showcase"
    elif set(mechanisms) & SIMULATION_MECHANISMS:
        presentation = "simulation_preview"
    else:
        presentation = "studio_turntable"

    confidence = {
        "scene_audit": 0.98,
        "scene_audit_partial": 0.82,
        "verified_steps": 0.86,
        "catalog": 0.68,
        "title_tutorial": 0.55,
        "none": 0.25,
    }[route_source]
    if route == "unknown" or subject_family == "unknown":
        confidence = min(confidence, 0.60)

    return RenderKnowledgeContext(
        source_kind=source_kind,
        route=route,
        render_profile=render_profile,
        subject_family=subject_family,
        motion_mechanisms=mechanisms,
        geometry_traits=geometry,
        material_traits=material,
        presentation_profile=presentation,
        runtime_traits=runtime,
        engine=engine,
        blender_version=version,
        confidence=confidence,
        evidence=evidence,
    )


def _recipe_compatibility(
    recipe: RecipeSpec, context: RenderKnowledgeContext
) -> list[str]:
    reasons: list[str] = []
    if context.source_kind not in recipe.source_kinds:
        reasons.append("source_kind_mismatch")
    if context.route not in recipe.routes:
        reasons.append("route_mismatch")
    if recipe.render_profiles and context.render_profile not in recipe.render_profiles:
        reasons.append("render_profile_mismatch")
    facts = context.facts()
    missing = sorted(set(recipe.required_traits) - facts)
    excluded = sorted(set(recipe.excluded_traits) & facts)
    if missing:
        reasons.append("missing_traits:" + ",".join(missing))
    if excluded:
        reasons.append("excluded_traits:" + ",".join(excluded))
    if recipe.engines:
        if not context.engine:
            reasons.append("engine_unknown")
        elif context.engine not in recipe.engines:
            reasons.append("engine_mismatch")
    current_version = _version_tuple(context.blender_version)
    if recipe.min_blender_version or recipe.max_blender_version:
        if not current_version:
            reasons.append("blender_version_unknown")
        else:
            if recipe.min_blender_version and current_version < _version_tuple(
                recipe.min_blender_version
            ):
                reasons.append("blender_version_too_old")
            if recipe.max_blender_version and current_version > _version_tuple(
                recipe.max_blender_version
            ):
                reasons.append("blender_version_too_new")
    if recipe.presentation_profile:
        if (
            not context.presentation_profile
            or context.presentation_profile == "unknown"
        ):
            reasons.append("presentation_profile_unknown")
        elif context.presentation_profile != recipe.presentation_profile:
            reasons.append("presentation_profile_mismatch")
    if (
        recipe.requires_source_preservation
        and "preserve_source" not in context.runtime_traits
    ):
        reasons.append("source_preservation_required")
    return reasons


def _context_evidence_strength(
    context: RenderKnowledgeContext,
) -> tuple[int, int, int, int]:
    priorities = [
        int(item.get("priority") or 0)
        for item in context.evidence
        if isinstance(item, Mapping)
    ]
    deterministic_priorities = [
        int(item.get("priority") or 0)
        for item in context.evidence
        if isinstance(item, Mapping) and item.get("deterministic") is True
    ]
    return (
        max(deterministic_priorities, default=0),
        max(priorities, default=0),
        round(context.confidence * 1_000_000),
        len(
            {
                str(item.get("source") or "")
                for item in context.evidence
                if isinstance(item, Mapping)
            }
        ),
    )


@dataclass(frozen=True)
class _RankedRecipe:
    score: float
    specificity: int
    evidence_strength: tuple[int, int, int, int]
    validated_assets: int
    semantic_version: tuple[int, ...]
    priority: int
    recipe: RecipeSpec
    approval: RecipeApproval

    def sort_key(self) -> tuple[Any, ...]:
        return (
            -self.specificity,
            tuple(-value for value in self.evidence_strength),
            -self.validated_assets,
            tuple(-value for value in self.semantic_version),
            -self.priority,
            self.recipe.recipe_id,
            self.recipe.version,
        )


def match_recipe(
    context: RenderKnowledgeContext,
    recipes: Iterable[RecipeSpec],
    *,
    minimum_confidence: float = 0.75,
    approvals: Mapping[str, RecipeApproval | Mapping[str, Any]] | None = None,
) -> RecipeMatch:
    reviewed: list[_RankedRecipe] = []
    candidates: list[str] = []
    deprecated: list[str] = []
    blocked: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for recipe in recipes:
        key = (recipe.recipe_id, recipe.version)
        if key in seen:
            raise RenderKnowledgeError(
                f"duplicate recipe id/version: {recipe.recipe_id}@{recipe.version}"
            )
        seen.add(key)
        compatibility = _recipe_compatibility(recipe, context)
        if recipe.review_status == "deprecated":
            deprecated.append(recipe.recipe_id)
            continue
        if compatibility:
            blocked[recipe.recipe_id] = compatibility
            continue
        if recipe.review_status == "candidate":
            candidates.append(recipe.recipe_id)
            continue
        approval_value = (approvals or {}).get(f"{recipe.recipe_id}@{recipe.version}")
        approval = (
            approval_value
            if isinstance(approval_value, RecipeApproval)
            else RecipeApproval.from_dict(approval_value)
            if isinstance(approval_value, Mapping)
            else None
        )
        promotion = recipe.promotion_blockers(approval)
        if promotion:
            blocked[recipe.recipe_id] = promotion
            continue
        specificity = (
            len(recipe.required_traits)
            + len(recipe.excluded_traits)
            + bool(recipe.render_profiles)
            + bool(recipe.engines)
            + bool(recipe.min_blender_version or recipe.max_blender_version)
        )
        score = min(0.99, context.confidence + min(0.08, specificity * 0.01))
        if approval is None:
            raise RenderKnowledgeError(
                "promotion gate passed without approval provenance"
            )
        reviewed.append(
            _RankedRecipe(
                score=score,
                specificity=specificity,
                evidence_strength=_context_evidence_strength(context),
                validated_assets=approval.validated_assets,
                semantic_version=_semantic_version_key(recipe.version),
                priority=recipe.priority,
                recipe=recipe,
                approval=approval,
            )
        )
    reviewed.sort(key=_RankedRecipe.sort_key)
    if not reviewed:
        return RecipeMatch(
            context=context,
            candidate_recipe_ids=sorted(candidates),
            deprecated_recipe_ids=sorted(deprecated),
            blocked_recipes=blocked,
            reasons=["no_reviewed_compatible_recipe"],
            fallback_reason="use_existing_conservative_baseline",
        )
    selected_rank = reviewed[0]
    confidence = selected_rank.score
    selected = selected_rank.recipe
    selected_approval = selected_rank.approval
    if confidence < minimum_confidence:
        return RecipeMatch(
            context=context,
            confidence=confidence,
            candidate_recipe_ids=sorted(candidates),
            deprecated_recipe_ids=sorted(deprecated),
            blocked_recipes=blocked,
            reasons=[
                f"confidence_below_threshold:{confidence:.3f}<{minimum_confidence:.3f}"
            ],
            fallback_reason="use_existing_conservative_baseline",
        )
    return RecipeMatch(
        context=context,
        recipe_id=selected.recipe_id,
        recipe_version=selected.version,
        confidence=confidence,
        decision="matched",
        reasons=[
            "reviewed_recipe",
            "hard_filters_passed",
            "promotion_gate_passed",
            "approval_provenance_verified",
        ],
        approval_id=selected_approval.approval_id,
        candidate_recipe_ids=sorted(candidates),
        deprecated_recipe_ids=sorted(deprecated),
        blocked_recipes=blocked,
    )


_VALIDATED_SOURCES = sorted(SOURCE_KINDS)


def _baseline_recipe(
    recipe_id: str,
    *,
    routes: list[str],
    profiles: list[str],
    required: list[str] | None = None,
    excluded: list[str] | None = None,
    presentation: str,
    priority: int,
    fallback: str = "",
) -> RecipeSpec:
    return RecipeSpec(
        recipe_id=recipe_id,
        version="1.0.0",
        # These structured recipes are a faithful encoding of the current
        # conservative behavior, but they have not yet passed the new
        # cross-pipeline holdout and human-promotion gate.  Keep them advisory
        # until validation evidence is recorded instead of inventing it here.
        review_status="candidate",
        source_kinds=_VALIDATED_SOURCES,
        routes=routes,
        render_profiles=profiles,
        required_traits=required or [],
        excluded_traits=excluded or [],
        presentation_profile=presentation,
        priority=priority,
        validated_assets=0,
        validated_source_kinds=[],
        human_reviewed=False,
        quality_gates=[
            "required_output_contract",
            "material_integrity",
            "subject_framing",
        ],
        fallback_recipe_id=fallback,
    )


DEFAULT_RECIPES: tuple[RecipeSpec, ...] = (
    _baseline_recipe(
        "static_six_view",
        routes=["static"],
        profiles=["six_view"],
        excluded=["motion:unverified_metadata"],
        presentation="studio_turntable",
        priority=10,
    ),
    _baseline_recipe(
        "static_source_camera",
        routes=["static"],
        profiles=["source_camera_static"],
        required=["runtime:source_camera"],
        presentation="cinematic_scene",
        priority=30,
    ),
    _baseline_recipe(
        "static_material_detail",
        routes=["static"],
        profiles=["material_detail"],
        required=["subject:material"],
        presentation="detail_showcase",
        priority=40,
    ),
    _baseline_recipe(
        "static_large_scene",
        routes=["static"],
        profiles=["large_scene"],
        required=["geometry:large_scene"],
        presentation="cinematic_scene",
        priority=40,
    ),
    _baseline_recipe(
        "dynamic_source_camera",
        routes=["dynamic"],
        profiles=["source_camera_dynamic"],
        required=["runtime:source_camera"],
        presentation="cinematic_scene",
        priority=50,
        fallback="static_source_camera",
    ),
    _baseline_recipe(
        "dynamic_union_bounds",
        routes=["dynamic"],
        profiles=["dynamic_union_bounds"],
        presentation="character_loop",
        priority=30,
        fallback="static_six_view",
    ),
    _baseline_recipe(
        "dynamic_simulation",
        routes=["dynamic"],
        profiles=["simulation_dynamic"],
        required=["runtime:simulation"],
        presentation="simulation_preview",
        priority=60,
        fallback="static_six_view",
    ),
    _baseline_recipe(
        "dynamic_to_static_fallback",
        routes=["static"],
        profiles=["static_fallback"],
        required=["runtime:dynamic_fallback"],
        presentation="studio_turntable",
        priority=70,
    ),
)


def match_default_recipe(
    context: RenderKnowledgeContext, minimum_confidence: float = 0.75
) -> RecipeMatch:
    return match_recipe(context, DEFAULT_RECIPES, minimum_confidence=minimum_confidence)


def bili_linked_asset_knowledge_decision(
    row: Mapping[str, Any], asset_audit: Mapping[str, Any]
) -> tuple[RenderKnowledgeContext, RecipeMatch]:
    """Post-asset_audit adapter for Task 1 conservative source rendering."""
    audit = dict(asset_audit or {})
    source_scene = dict(audit.get("source_scene") or {})
    if source_scene:
        audit["renderable"] = int(source_scene.get("renderable_object_count") or 0) > 0
        audit["source_camera_usable"] = (
            int(source_scene.get("camera_count") or 0) > 0
            and int(source_scene.get("light_count") or 0) > 0
        )
    title = " ".join(
        str(row.get(key) or "")
        for key in ["标题", "source_title", "素材标题", "分类", "title"]
    )
    context = build_render_knowledge_context(
        "bili_linked_asset",
        scene_audit=audit,
        catalog=row,
        title=title,
    )
    return context, match_default_recipe(context)


def video_replay_knowledge_decision(
    *, title: str, tutorial: str, verified_steps: Any
) -> tuple[RenderKnowledgeContext, RecipeMatch]:
    """Pre-spec adapter; retrieval cannot replace tutorial or verified steps."""
    context = build_render_knowledge_context(
        "video_replay",
        verified_steps=verified_steps,
        title=title,
        tutorial=tutorial,
    )
    return context, match_default_recipe(context)


def decision_payload(
    context: RenderKnowledgeContext, match: RecipeMatch
) -> dict[str, Any]:
    return {
        "render_knowledge_context": context.to_dict(),
        "recipe_match": match.to_dict(),
    }
