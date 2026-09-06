#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import blender_knowledge_common as kc
import render_knowledge as rk


def read_video_context(
    video_dir: Path, tutorial: str = "", qa: dict[str, Any] | None = None
) -> dict[str, Any]:
    info = {}
    info_path = video_dir / "source.info.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            info = {}
    if not tutorial:
        for name in ["tutorial_qgate.md", "tutorial.md"]:
            path = video_dir / name
            if path.exists():
                tutorial = path.read_text(encoding="utf-8", errors="ignore")[:6000]
                break
    steps_text = ""
    for name in ["steps_verified.json", "steps.json", "steps_detailed.json"]:
        path = video_dir / name
        if path.exists():
            steps_text = path.read_text(encoding="utf-8", errors="ignore")[:3000]
            break
    subtitle_text = ""
    for path in (
        sorted(video_dir.glob("source*.srt"))[:2]
        + sorted(video_dir.glob("source*.vtt"))[:2]
    ):
        subtitle_text += "\n" + path.read_text(encoding="utf-8", errors="ignore")[:1800]
    qa_text = json.dumps(qa or {}, ensure_ascii=False)[:3000]
    query_text = "\n".join(
        [
            str(info.get("title") or video_dir.name),
            str(info.get("description") or "")[:1200],
            tutorial[:6000],
            steps_text,
            subtitle_text[:3000],
            qa_text,
        ]
    )
    canonical_context, recipe_match = rk.video_replay_knowledge_decision(
        verified_steps=_load_first_json(
            video_dir, ["steps_verified.json", "steps.json", "steps_detailed.json"]
        ),
        title=str(info.get("title") or video_dir.name),
        tutorial=tutorial,
    )
    result = {
        "title": info.get("title") or video_dir.name,
        "url": info.get("webpage_url") or info.get("original_url") or "",
        "query_text": query_text,
        "inferred_asset_family": kc.infer_asset_family(query_text),
        "inferred_pipeline_stage": kc.infer_pipeline_stage(query_text),
        "inferred_blender_feature": kc.infer_blender_feature(query_text),
        "render_knowledge_context": canonical_context.to_dict(),
        "recipe_match": recipe_match.to_dict(),
        "has_tutorial": bool(tutorial.strip()),
        "has_steps": bool(steps_text.strip()),
        "has_final_reference": (video_dir / "final_reference.png").exists(),
    }
    if (
        str(info.get("source_kind") or info.get("workload_kind") or "")
        == "video_replay_type2"
    ):
        linked_audit = _load_first_json(video_dir, ["linked_asset_audit.json"])
        linked_source = info.get("linked_source") or {}
        selected_hash = (
            str(linked_source.get("selected_model_sha256") or "")
            if isinstance(linked_source, dict)
            else ""
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", selected_hash)
            or not isinstance(linked_audit, dict)
            or linked_audit.get("selected_model_sha256") != selected_hash
            or linked_audit.get("status") != "pass"
        ):
            raise kc.KnowledgeManifestError(
                "linked source knowledge requires a passing asset audit with the exact source.info.json SHA-256"
            )
        linked_context, linked_match = rk.bili_linked_asset_knowledge_decision(
            info, linked_audit
        )
        result["linked_asset_knowledge"] = {
            "schema": "blender-pipeline-linked-knowledge-projection.v1",
            "selected_model_sha256": selected_hash,
            "role": "advisory_reproduction_evidence",
            "authority": "video_tutorial_and_verified_steps_win",
            **rk.decision_payload(linked_context, linked_match),
        }
    return result


def _load_first_json(video_dir: Path, names: list[str]) -> Any:
    for name in names:
        path = video_dir / name
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except json.JSONDecodeError:
            continue
    return {}


def qdrant_search(
    query_text: str, family: str, feature: str, top_k: int
) -> list[dict[str, Any]]:
    if kc.active_qdrant_path() is None:
        raise FileNotFoundError(
            "active generation uses deterministic manifest retrieval"
        )
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    model = kc.load_embedder()
    vector = model.encode([query_text], normalize_embeddings=True)[0].tolist()
    client = kc.qdrant_client()
    filters = []
    if family and family != "other":
        filters.append(
            FieldCondition(
                key="asset_family",
                match=MatchAny(any=[family, "other", "material_shader", "motion"]),
            )
        )
    if feature and feature != "other":
        filters.append(
            FieldCondition(
                key="blender_feature", match=MatchAny(any=[feature, "other"])
            )
        )
    qfilter = Filter(
        must=[
            FieldCondition(
                key="review_status", match=MatchAny(any=["curated", "reviewed"])
            )
        ],
        should=filters or None,
    )
    try:
        result = client.query_points(
            collection_name=kc.COLLECTION_NAME,
            query=vector,
            query_filter=qfilter,
            limit=max(top_k * 2, top_k),
            with_payload=True,
        )
        points = getattr(result, "points", result)
    except Exception:
        points = client.search(
            collection_name=kc.COLLECTION_NAME,
            query_vector=vector,
            query_filter=qfilter,
            limit=max(top_k * 2, top_k),
            with_payload=True,
        )
    rows: list[dict[str, Any]] = []
    for point in points:
        payload = dict(point.payload or {})
        if not kc.active_knowledge_eligible(payload):
            continue
        payload["score"] = float(getattr(point, "score", 0.0))
        rows.append(payload)
    rows.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return rows[:top_k]


def manifest_fallback_search(
    query_text: str, family: str, feature: str, top_k: int
) -> list[dict[str, Any]]:
    words = {w.lower() for w in query_text.replace("_", " ").split() if len(w) >= 2}
    rows = []
    for item in kc.read_jsonl():
        if not kc.active_knowledge_eligible(item):
            continue
        text = f"{item.get('title', '')} {item.get('text', '')} {' '.join(item.get('tags') or [])}".lower()
        score = sum(1 for w in words if w in text)
        if family != "other" and item.get("asset_family") in {
            family,
            "other",
            "material_shader",
            "motion",
        }:
            score += 8
        if feature != "other" and item.get("blender_feature") in {feature, "other"}:
            score += 4
        if item.get("review_status") == "reviewed":
            score += 3
        if score <= 0:
            continue
        row = dict(item)
        row["score"] = float(score)
        rows.append(row)
    rows.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return rows[:top_k]


def compact_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": row.get("source_id"),
        "score": row.get("score"),
        "title": row.get("title"),
        "source_path": row.get("source_path"),
        "source_type": row.get("source_type"),
        "knowledge_type": row.get("knowledge_type"),
        "pipeline_stage": row.get("pipeline_stage"),
        "asset_family": row.get("asset_family"),
        "blender_feature": row.get("blender_feature"),
        "review_status": row.get("review_status"),
        "tags": row.get("tags") or [],
        "logical_source_id": row.get("logical_source_id") or row.get("source_id"),
        "extra": row.get("extra") or {},
        "text": (row.get("text") or row.get("text_preview") or "")[:1400],
    }


def locally_compatible_reviewed(row: dict[str, Any], context: dict[str, Any]) -> bool:
    if row.get("review_status") != "reviewed":
        return False
    recipe_match = context.get("recipe_match") or {}
    if (
        recipe_match.get("decision") != "matched"
        or recipe_match.get("executable") is not True
    ):
        return False
    required_reasons = {
        "hard_filters_passed",
        "promotion_gate_passed",
        "approval_provenance_verified",
    }
    if not required_reasons.issubset(set(recipe_match.get("reasons") or [])):
        return False
    if not recipe_match.get("approval_id"):
        return False
    canonical_context = context.get("render_knowledge_context") or {}
    if canonical_context.get("route") in {None, "", "unknown", "not_renderable"}:
        return False
    extra = row.get("extra") or {}
    # Generic legacy chunks have no independently verifiable engine/version/route
    # scope.  Only the exact reviewed recipe that the canonical matcher approved
    # may become a hard constraint; every other retrieval remains advisory.
    return bool(
        extra.get("recipe_id")
        and extra.get("recipe_id") == recipe_match.get("recipe_id")
        and extra.get("recipe_version") == recipe_match.get("recipe_version")
    )


def admitted_scope_matches(row: dict[str, Any], context: dict[str, Any]) -> bool:
    if not kc.active_knowledge_eligible(row):
        return False
    if row.get("review_status") == "curated":
        return True
    scope = row["extra"]["admission"]["scope"]
    canonical = context.get("render_knowledge_context") or {}
    actual = {
        "route": canonical.get("route"),
        "asset_family": context.get("inferred_asset_family"),
        "blender_version": canonical.get("blender_version"),
    }
    return all(actual[key] and scope.get(key) == actual[key] for key in actual)


def build_pack(
    video_dir: Path,
    tutorial: str = "",
    qa: dict[str, Any] | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    context = read_video_context(video_dir, tutorial=tutorial, qa=qa)
    family = context["inferred_asset_family"]
    feature = context["inferred_blender_feature"]
    status = "ok"
    error = ""
    manifest_only = kc.active_qdrant_path() is None
    try:
        if manifest_only:
            results = manifest_fallback_search(
                context["query_text"], family, feature, top_k
            )
            retrieval_backend = "manifest_lexical"
        else:
            results = qdrant_search(context["query_text"], family, feature, top_k)
            retrieval_backend = "qdrant"
    except Exception as exc:
        error = str(exc)[:1000]
        retrieval_backend = "manifest_fallback"
        try:
            results = manifest_fallback_search(
                context["query_text"], family, feature, top_k
            )
        except Exception as fallback_exc:
            results = []
            error = f"{error}; manifest fallback failed: {str(fallback_exc)[:700]}"[
                :1000
            ]
        status = "fallback" if results else "unavailable"
    # Defense in depth for older vector generations and alternate backends.
    results = [row for row in results if admitted_scope_matches(row, context)]
    if not results:
        status = "unavailable"
    if results:
        top = results[0]
        top_family = top.get("asset_family")
        if (
            top_family
            and top_family not in {"other", "material_shader", "motion"}
            and float(top.get("score") or 0) >= 0.75
            and context.get("inferred_asset_family")
            in {"other", "material_shader", "motion", "fluid"}
        ):
            # Retrieval is candidate recall only. It may suggest a family but
            # never overwrites tutorial/verified-step deterministic context.
            context["retrieval_suggested_asset_family"] = top_family
    compact = [compact_result(row) for row in results]
    reviewed = [row for row in compact if locally_compatible_reviewed(row, context)]
    candidates = [row for row in compact if row.get("review_status") == "candidate"]
    deprecated = [row for row in compact if row.get("review_status") == "deprecated"]
    pack = {
        "status": status,
        "error": error,
        "retrieval_backend": retrieval_backend,
        "collection": kc.COLLECTION_NAME,
        "active_manifest": str(kc.active_manifest_path()),
        "knowledge_generation": str(kc._current_build_payload().get("build_id") or ""),
        "query": context["query_text"],
        "context": {k: v for k, v in context.items() if k != "query_text"},
        "policy": {
            "video_specific_evidence_required": True,
            "admission": "curated_guidance_or_reviewed_success_only",
            "curated_knowledge_usage": "scoped_guidance_not_success_artifact_proof",
            "candidate_knowledge_usage": "excluded_from_active_retrieval",
            "reviewed_knowledge_usage": "hard_only_after_executable_canonical_recipe_and_approval",
            "deprecated_knowledge_usage": "never_use_as_guidance",
        },
        "results": compact,
        "reviewed_hits": reviewed,
        "candidate_hits": candidates,
        "deprecated_hits": deprecated,
        "hard_constraint_source_ids": [
            row["source_id"] for row in reviewed if row.get("source_id")
        ],
        "candidate_source_ids": [
            row["source_id"] for row in candidates if row.get("source_id")
        ],
    }
    return pack


def retrieve_for_pipeline(
    video_dir: Path,
    tutorial: str = "",
    qa: dict[str, Any] | None = None,
    output_path: Path | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    pack = build_pack(video_dir, tutorial=tutorial, qa=qa, top_k=top_k)
    linked = pack["context"].get("linked_asset_knowledge")
    if linked:
        linked_path = video_dir / "linked_asset_knowledge.json"
        if linked_path.exists() or linked_path.is_symlink():
            try:
                existing = json.loads(linked_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise kc.KnowledgeManifestError(
                    "existing linked asset knowledge is unreadable; refusing to overwrite it"
                ) from exc
            if (
                not isinstance(existing, dict)
                or existing.get("selected_model_sha256")
                != linked["selected_model_sha256"]
                or existing.get("role") != linked["role"]
            ):
                raise kc.KnowledgeManifestError(
                    "existing linked asset knowledge has a different source or role; refusing to overwrite it"
                )
            # A matching independent record may contain richer review data.
            # Its identity suffices for the downstream check; leave it intact.
        else:
            kc.atomic_write_json(linked_path, linked)
    output = output_path or (video_dir / "knowledge_retrieval_pack.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    return pack


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retrieve Blender pipeline knowledge for one video/tutorial."
    )
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--tutorial", type=Path)
    parser.add_argument("--qa", type=Path)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    tutorial = (
        args.tutorial.read_text(encoding="utf-8", errors="ignore")
        if args.tutorial and args.tutorial.exists()
        else ""
    )
    qa = (
        json.loads(args.qa.read_text(encoding="utf-8"))
        if args.qa and args.qa.exists()
        else {}
    )
    pack = retrieve_for_pipeline(
        args.video_dir.resolve(),
        tutorial=tutorial,
        qa=qa,
        output_path=args.output,
        top_k=args.top_k,
    )
    print(
        json.dumps(
            {
                k: pack[k]
                for k in [
                    "status",
                    "retrieval_backend",
                    "context",
                    "hard_constraint_source_ids",
                    "candidate_source_ids",
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
