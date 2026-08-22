#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

import blender_knowledge_common as kc


PROMOTION_DECISION_SCHEMA = "video-replay-knowledge-promotion-decision.v1"
MIN_PROMOTION_ASSETS = 5
FAILURE_TAXONOMY_SCHEMA = "video-replay-failure-taxonomy.v1"
KNOWLEDGE_SNAPSHOT_SCHEMA = "video-replay-knowledge-input-snapshot.v1"

# Ordered so a multi-label episode always has the same primary category.
REPLAY_FAILURE_TAXONOMY: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "identity_preservation",
        "identity",
        frozenset(
            {
                "identity_metadata_incomplete",
                "identity_mismatch",
                "media_hash_manifest_missing",
                "media_hash_mismatch",
                "terminal_artifact_ambiguous",
                "subject_identity_wrong",
            }
        ),
    ),
    (
        "source_preservation",
        "source",
        frozenset(
            {
                "source_video_identity_missing",
                "source_video_identity_mismatch",
                "type2_source_preservation_unverified",
                "source_comparison_unavailable",
            }
        ),
    ),
    (
        "missing_dependencies",
        "dependency",
        frozenset(
            {
                "asset_blend_missing",
                "asset_reopen_failed",
                "external_dependency_missing",
                "missing_texture_dependency",
            }
        ),
    ),
    (
        "missing_media",
        "delivery",
        frozenset(
            {
                "output_directory_missing",
                "source_metadata_missing",
                "pipeline_review_missing",
                "dynamic_media_missing",
                "static_iso_missing",
                "terminal_artifact_unavailable",
            }
        ),
    ),
    (
        "material_integrity",
        "material",
        frozenset(
            {
                "material_assignment_invalid",
                "material_source_not_preserved",
                "missing_texture_dependency",
                "material_mismatch_or_missing",
            }
        ),
    ),
    (
        "dynamic_misroute",
        "motion",
        frozenset(
            {
                "dynamic_route_without_subject_motion",
                "source_static_but_published_dynamic",
                "route_static_dynamic_wrong",
            }
        ),
    ),
    (
        "low_visible_motion",
        "motion",
        frozenset(
            {
                "insufficient_visible_motion",
                "manual_visual_review_subject_motion_unverified",
                "low_or_no_visible_motion",
            }
        ),
    ),
    (
        "highlight_clipping",
        "lighting",
        frozenset(
            {
                "overexposed_subject",
                "manual_visual_review_overexposed_subject",
                "overexposed",
            }
        ),
    ),
    (
        "underexposed_subject",
        "lighting",
        frozenset({"underexposed_subject", "underexposed"}),
    ),
    (
        "composition_mismatch",
        "presentation",
        frozenset({"composition_or_framing"}),
    ),
    (
        "quality_gate_rejection",
        "quality",
        frozenset(
            {
                "pipeline_review_not_pass",
                "pipeline_review_invalid",
                "static_quality_gate_failed",
                "published_route_invalid",
                "quality_probe_failed",
            }
        ),
    ),
    (
        "quality_gate_calibration",
        "quality",
        frozenset({"small_subject_motion_gate_false_negative"}),
    ),
)


class ReplayKnowledgePromotionError(RuntimeError):
    pass


def _canonical_failure_issue_codes(issues: list[Any]) -> set[str]:
    known = {
        issue
        for _category, _family, issue_codes in REPLAY_FAILURE_TAXONOMY
        for issue in issue_codes
    }
    codes: set[str] = set()
    for value in issues:
        text = str(value or "").strip().lower()
        token = "_".join(
            "".join(
                character if character.isalnum() else " " for character in text
            ).split()
        )
        if token in known:
            codes.add(token)
        if "dynamic" in token and ("static" in token or "no_subject_motion" in token):
            codes.add("dynamic_route_without_subject_motion")
        if "motion" in token and any(
            term in token
            for term in ("insufficient", "invisible", "not_visible", "failed")
        ):
            codes.add("insufficient_visible_motion")
        if any(term in token for term in ("overexpos", "highlight_clip")):
            codes.add("overexposed_subject")
        if any(
            term in token
            for term in (
                "unassigned_material",
                "missing_material",
                "placeholder_material",
                "material_not_preserved",
            )
        ):
            codes.add("material_assignment_invalid")
        if "texture" in token and any(
            term in token for term in ("missing", "not_found", "unresolved")
        ):
            codes.add("missing_texture_dependency")
        if "dependenc" in token and any(
            term in token for term in ("missing", "not_found", "unresolved")
        ):
            codes.add("external_dependency_missing")
        if "reopen" in token and any(
            term in token for term in ("failed", "error", "invalid")
        ):
            codes.add("asset_reopen_failed")
    return codes


def classify_replay_failure_taxonomy(
    issues: list[Any],
) -> dict[str, Any]:
    """Build candidate-only deterministic taxonomy from explicit evidence."""

    normalized = _canonical_failure_issue_codes(issues)
    categories = [
        {
            "category": category,
            "family": family,
            "evidence_issue_codes": sorted(normalized & issue_codes),
            "knowledge_status": "candidate",
        }
        for category, family, issue_codes in REPLAY_FAILURE_TAXONOMY
        if normalized & issue_codes
    ]
    return {
        "schema": FAILURE_TAXONOMY_SCHEMA,
        "primary_category": (str(categories[0]["category"]) if categories else ""),
        "categories": categories,
    }


def _promotion_asset_id(row: dict[str, Any]) -> str:
    return str(row.get("asset_id") or "").strip()


def candidate_promotion_guard(
    candidate_records: list[dict[str, Any]],
    accepted_holdout_records: list[dict[str, Any]],
    *,
    minimum_assets: int = MIN_PROMOTION_ASSETS,
) -> dict[str, Any]:
    """Fail closed unless candidate evidence and accepted holdout both pass."""

    if minimum_assets < MIN_PROMOTION_ASSETS:
        raise ValueError(f"minimum_assets cannot be lower than {MIN_PROMOTION_ASSETS}")
    candidate_asset_rows = [
        _promotion_asset_id(row)
        for row in candidate_records
        if _promotion_asset_id(row)
    ]
    valid_candidate_rows = [
        row
        for row in candidate_records
        if _promotion_asset_id(row)
        and str(row.get("candidate_rule_id") or "").strip()
        and str(row.get("review_status") or "") == "candidate"
        and row.get("human_reviewed") is True
        and str(row.get("outcome") or "") in {"accepted", "pass", "repaired"}
    ]
    candidate_assets = {_promotion_asset_id(row) for row in valid_candidate_rows}
    candidate_rule_ids = {
        str(row.get("candidate_rule_id") or "").strip() for row in valid_candidate_rows
    }
    blockers: list[str] = []
    if len(candidate_assets) < minimum_assets:
        blockers.append("source_specific_requires_five_distinct_human_reviewed_assets")
    if len(valid_candidate_rows) != len(candidate_records):
        blockers.append("candidate_evidence_must_be_candidate_human_reviewed_pass")
    if len(candidate_asset_rows) != len(set(candidate_asset_rows)):
        blockers.append("candidate_evidence_assets_must_be_unique")
    if len(candidate_rule_ids) != 1:
        blockers.append("candidate_evidence_requires_one_rule_identity")

    holdout_assets: set[str] = set()
    holdout_asset_rows: list[str] = []
    holdout_regressions: list[str] = []
    holdout_unattested: list[str] = []
    for row in accepted_holdout_records:
        asset_id = _promotion_asset_id(row)
        if not asset_id:
            holdout_unattested.append("<missing_asset_id>")
            continue
        holdout_asset_rows.append(asset_id)
        holdout_assets.add(asset_id)
        baseline = str(row.get("baseline_status") or "")
        candidate = str(row.get("candidate_status") or "")
        regression = row.get("quality_regression")
        if baseline != "accepted" or candidate != "accepted" or regression is not False:
            if regression is True or candidate != "accepted":
                holdout_regressions.append(asset_id)
            else:
                holdout_unattested.append(asset_id)
    if not holdout_assets:
        blockers.append("accepted_holdout_required")
    if len(holdout_asset_rows) != len(set(holdout_asset_rows)):
        blockers.append("accepted_holdout_assets_must_be_unique")
    if holdout_unattested:
        blockers.append("accepted_holdout_regression_attestation_required")
    if holdout_regressions:
        blockers.append("accepted_holdout_regression_detected")

    core = {
        "schema": PROMOTION_DECISION_SCHEMA,
        "minimum_distinct_assets": minimum_assets,
        "candidate_rule_id": (
            next(iter(candidate_rule_ids)) if len(candidate_rule_ids) == 1 else ""
        ),
        "candidate_asset_ids": sorted(candidate_assets),
        "accepted_holdout_asset_ids": sorted(holdout_assets),
        "holdout_regression_asset_ids": sorted(set(holdout_regressions)),
        "holdout_unattested_asset_ids": sorted(set(holdout_unattested)),
        "blockers": sorted(set(blockers)),
    }
    return {
        **core,
        "eligible_for_reviewed": not blockers,
        "evidence_sha256": hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _require_reviewed_promotion_evidence(
    chunks: list[kc.KnowledgeChunk],
    promotion_evidence: dict[str, Any] | None,
) -> None:
    if not any(chunk.review_status == "reviewed" for chunk in chunks):
        return
    evidence = promotion_evidence or {}
    candidates = evidence.get("candidate_records")
    holdout = evidence.get("accepted_holdout_records")
    if not isinstance(candidates, list) or not isinstance(holdout, list):
        raise ReplayKnowledgePromotionError(
            "reviewed knowledge requires explicit candidate and holdout evidence"
        )
    if any(not isinstance(row, dict) for row in candidates + holdout):
        raise ReplayKnowledgePromotionError(
            "reviewed promotion evidence rows must be objects"
        )
    decision = candidate_promotion_guard(candidates, holdout)
    if not decision["eligible_for_reviewed"]:
        raise ReplayKnowledgePromotionError(
            "reviewed promotion blocked: " + ",".join(decision["blockers"])
        )
    rule_id = str(decision["candidate_rule_id"])
    if any(
        str(chunk.extra.get("candidate_rule_id") or "") != rule_id
        for chunk in chunks
        if chunk.review_status == "reviewed"
    ):
        raise ReplayKnowledgePromotionError(
            "reviewed chunk candidate_rule_id does not match promotion evidence"
        )


def _merge_rows(
    rows: list[dict[str, Any]], chunks: list[kc.KnowledgeChunk]
) -> tuple[list[dict[str, Any]], int]:
    merged = [dict(row) for row in rows]
    positions = {
        str(row.get("logical_source_id") or row.get("source_id") or ""): index
        for index, row in enumerate(merged)
        if row.get("logical_source_id") or row.get("source_id")
    }
    changed = 0
    for chunk in chunks:
        key = chunk.logical_source_id or chunk.source_id
        payload = chunk.payload() | {"text": chunk.text}
        position = positions.get(key)
        if position is not None:
            if merged[position].get("source_hash") == chunk.source_hash:
                continue
            merged[position] = payload
            changed += 1
            continue
        positions[key] = len(merged)
        merged.append(payload)
        changed += 1
    return merged, changed


def _new_generation_id(rows: list[dict[str, Any]]) -> str:
    digest_input = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:10]
    return f"{time.strftime('%Y%m%dT%H%M%S')}-replay-{digest}"


def _rebuild_qdrant(rows: list[dict[str, Any]], target: Path) -> dict[str, Any]:
    """Build vectors from the exact rows that will be activated as manifest."""

    import build_blender_knowledge_index as build_index

    chunks = [kc.KnowledgeChunk.from_payload(row) for row in rows]
    return build_index.recreate_qdrant(chunks, target)


def _activate_manifest_generation(rows: list[dict[str, Any]], *, changed: int) -> None:
    parent = kc._current_build_payload()
    builds_path = kc.KNOWLEDGE_ROOT / kc.BUILDS_PATH.name
    build_id = _new_generation_id(rows)
    build_dir = builds_path / build_id
    suffix = 1
    while build_dir.exists():
        build_dir = builds_path / f"{build_id}-{suffix}"
        suffix += 1
    build_id = build_dir.name
    build_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = build_dir / "blender_knowledge_manifest.jsonl"
    qdrant_path = build_dir / "qdrant"
    summary_path = build_dir / "index_summary.json"
    try:
        kc.write_jsonl(rows, manifest_path)
        qdrant_summary = _rebuild_qdrant(rows, qdrant_path)
        summary = {
            "schema_version": "blender-knowledge-index-summary-v1",
            "build_id": build_id,
            "generation_kind": "replay_incremental_full_index",
            "manifest": str(manifest_path),
            "qdrant": qdrant_summary,
            "chunks": len(rows),
            "chunks_changed": changed,
            "parent_build_id": str(parent.get("build_id") or ""),
        }
        kc.atomic_write_json(summary_path, summary)
        current = {
            "schema_version": "blender-knowledge-current-v1",
            "build_id": build_id,
            "manifest": manifest_path.relative_to(kc.KNOWLEDGE_ROOT).as_posix(),
            "qdrant": qdrant_path.relative_to(kc.KNOWLEDGE_ROOT).as_posix(),
            "summary": summary_path.relative_to(kc.KNOWLEDGE_ROOT).as_posix(),
            "activated_at": time.strftime("%FT%T%z"),
        }
        kc.atomic_write_json(kc.CURRENT_BUILD_PATH, current)
    except BaseException:
        shutil.rmtree(build_dir, ignore_errors=True)
        raise


def append_unique(
    chunks: list[kc.KnowledgeChunk],
    *,
    promotion_evidence: dict[str, Any] | None = None,
) -> int:
    _require_reviewed_promotion_evidence(chunks, promotion_evidence)
    with kc.knowledge_store_lock():
        rows = kc.read_jsonl(kc.active_manifest_path())
        merged, changed = _merge_rows(rows, chunks)
        if changed:
            _activate_manifest_generation(merged, changed=changed)
        return changed


def candidate_failure_episode_chunks(
    episode_path: Path,
) -> list[kc.KnowledgeChunk]:
    resolved = episode_path.expanduser().resolve()
    chunks: list[kc.KnowledgeChunk] = []
    for line_number, line in enumerate(
        resolved.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            episode = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayKnowledgePromotionError(
                f"candidate episode JSONL malformed at line {line_number}"
            ) from exc
        if (
            not isinstance(episode, dict)
            or episode.get("schema") != "video-replay-quality-failure-episode.v2"
            or episode.get("promotion_status") != "candidate"
            or episode.get("production_enforcement")
            != "candidate_only_no_production_parameter_change"
            or not str(episode.get("episode_id") or "")
        ):
            raise ReplayKnowledgePromotionError("candidate failure episode is invalid")
        work_item_id = str(episode.get("work_item_id") or "")
        episode_id = str(episode["episode_id"])
        chunk = kc.make_chunk(
            resolved,
            f"{work_item_id}:manual_visual_failure:{episode_id}",
            json.dumps(episode, ensure_ascii=False, indent=2),
            source_type="run_artifact",
            extra={
                "source_kind": str(episode.get("workload_kind") or ""),
                "outcome": "manual_visual_not_publishable",
                "promotion_status": "candidate",
                "production_enforcement": (
                    "candidate_only_no_production_parameter_change"
                ),
                "candidate_rule_ids": list(episode.get("candidate_rule_ids") or []),
            },
            logical_key=("video_replay_manual_quality_failure:" + episode_id),
        )
        chunk.knowledge_type = "episodic"
        chunk.review_status = "candidate"
        chunks.append(chunk)
    return chunks


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def replay_source_identity_from_info(
    info: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    source_kind = str(
        info.get("source_kind") or info.get("workload_kind") or "video_replay"
    )
    if source_kind not in {
        "video_replay",
        "video_replay_type1",
        "video_replay_type2",
    }:
        source_kind = "video_replay"
    semantic_source_kind = (
        "bili_linked_asset" if source_kind == "video_replay_type2" else "video_replay"
    )
    evidence: dict[str, Any] = {
        "source_kind": source_kind,
        "semantic_source_kind": semantic_source_kind,
        "source_video_url": str(
            info.get("webpage_url") or info.get("original_url") or ""
        ),
    }
    if source_kind == "video_replay_type2":
        evidence.update(
            {
                "material_assisted": True,
                "material_links_sha256": str(info.get("material_links_sha256") or ""),
                "linked_source": dict(info.get("linked_source") or {}),
            }
        )
    else:
        evidence["pure_video_eligible"] = bool(info.get("pure_video_eligibility"))
    return source_kind, semantic_source_kind, evidence


def replay_source_identity(video_dir: Path) -> tuple[str, str, dict[str, Any]]:
    return replay_source_identity_from_info(load_json(video_dir / "source.info.json"))


def build_knowledge_input_snapshot(
    video_dir: Path,
    *,
    terminal_status: str = "",
    fallback_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze every small input needed by the asynchronous knowledge rebuild."""

    review = load_json(video_dir / "pipeline_review.json")
    if not review and fallback_review is not None:
        review = dict(fallback_review)
    if terminal_status == "accepted" and review.get("status") != "pass":
        # Publish recovery can prove an already accepted asset without
        # reconstructing its old mutable work directory.  It must not create a
        # false failure episode merely because the historical review file is no
        # longer colocated with the recovered result.
        review = {
            "status": "pass",
            "checks": {"terminal_accepted": True},
            "issues": [],
        }
    documents: dict[str, str] = {}
    for name in ("tutorial.md", "code_tutorial.md"):
        path = video_dir / name
        if path.is_file():
            documents[name] = path.read_text(encoding="utf-8", errors="ignore")
    return {
        "schema": KNOWLEDGE_SNAPSHOT_SCHEMA,
        "video_dir_name": video_dir.name,
        "source_info": load_json(video_dir / "source.info.json"),
        "pipeline_review": review,
        "linked_asset_knowledge": load_json(video_dir / "linked_asset_knowledge.json"),
        "documents": documents,
    }


def _split_markdown_text(
    text: str, *, fallback_title: str, max_chars: int = 2600
) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^(#{1,4})\s+(.+)$", text, flags=re.M))
    if not matches:
        return [(fallback_title, text[:max_chars])]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        title = match.group(2).strip()
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        for part_index, part in enumerate(
            kc.split_long_text(body, max_chars=max_chars)
        ):
            suffix = f" part {part_index + 1}" if part_index else ""
            sections.append((title + suffix, part))
    return sections


def chunks_from_knowledge_snapshot(
    snapshot: Mapping[str, Any],
    *,
    video_dir: Path,
    updated_at: str,
) -> tuple[list[kc.KnowledgeChunk], bool]:
    """Create deterministic candidate chunks from an immutable event snapshot."""

    if snapshot.get("schema") != KNOWLEDGE_SNAPSHOT_SCHEMA:
        raise ReplayKnowledgePromotionError(
            "video replay knowledge snapshot schema is invalid"
        )
    source_info = snapshot.get("source_info")
    review = snapshot.get("pipeline_review")
    linked_knowledge = snapshot.get("linked_asset_knowledge")
    documents = snapshot.get("documents")
    if (
        not isinstance(source_info, Mapping)
        or not isinstance(review, Mapping)
        or not isinstance(linked_knowledge, Mapping)
        or not isinstance(documents, Mapping)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in documents.items()
        )
    ):
        raise ReplayKnowledgePromotionError(
            "video replay knowledge snapshot payload is invalid"
        )
    source_kind, semantic_source_kind, source_evidence = (
        replay_source_identity_from_info(source_info)
    )
    logical_video_id = str(
        snapshot.get("logical_video_id")
        or snapshot.get("video_dir_name")
        or video_dir.name
    ).strip()
    if not logical_video_id:
        raise ReplayKnowledgePromotionError(
            "video replay knowledge snapshot logical identity is missing"
        )
    chunks: list[kc.KnowledgeChunk] = []
    passed = review.get("status") == "pass"
    if passed:
        for name, source_type in (
            ("tutorial.md", "run_artifact"),
            ("code_tutorial.md", "run_artifact"),
        ):
            text = str(documents.get(name) or "")
            if len(text.encode("utf-8")) < 80:
                continue
            path = video_dir / name
            for title, body in _split_markdown_text(
                text, fallback_title=name, max_chars=2600
            ):
                chunk = kc.make_chunk(
                    path,
                    f"{logical_video_id}:{title}",
                    body,
                    source_type=source_type,
                    extra={
                        "video_dir": str(video_dir),
                        "updated_at": updated_at,
                        "outcome": "qa_passed",
                        "source_kind": source_kind,
                        "semantic_source_kind": semantic_source_kind,
                        "source_evidence": source_evidence,
                        "linked_asset_knowledge": (
                            dict(linked_knowledge)
                            if source_kind == "video_replay_type2"
                            else {}
                        ),
                        "promotion_status": "candidate",
                        "promotion_guard": {
                            "minimum_distinct_human_reviewed_assets": (
                                MIN_PROMOTION_ASSETS
                            ),
                            "accepted_holdout_zero_regression_required": True,
                        },
                    },
                    logical_key=(f"{source_kind}:{logical_video_id}:{name}:{title}"),
                )
                # One video can never promote itself into executable guidance.
                chunk.review_status = "candidate"
                chunks.append(chunk)
    else:
        review_issues = list(
            review.get("issues")
            if isinstance(review.get("issues"), list)
            else ["pipeline_review.json is missing or invalid"]
        )
        failure_taxonomy = classify_replay_failure_taxonomy(review_issues)
        failure_text = json.dumps(
            {
                "schema": "video-replay-quality-failure-episode.v2",
                "status": review.get("status") or "missing_review",
                "issues": review_issues,
                "checks": review.get("checks") or {},
                "failure_taxonomy": failure_taxonomy,
                "candidate_rule_ids": [
                    "video_replay_quality:" + str(category.get("category") or "")
                    for category in failure_taxonomy["categories"]
                    if str(category.get("category") or "")
                ],
                "promotion_status": "candidate",
                "promotion_guard": {
                    "minimum_distinct_human_reviewed_assets": (MIN_PROMOTION_ASSETS),
                    "accepted_holdout_zero_regression_required": True,
                },
                "production_enforcement": (
                    "candidate_only_no_production_parameter_change"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        chunk = kc.make_chunk(
            video_dir / "pipeline_review.json",
            f"{logical_video_id}:failure_episode",
            failure_text,
            source_type="run_artifact",
            extra={
                "video_dir": str(video_dir),
                "updated_at": updated_at,
                "outcome": "qa_failed",
                "source_kind": source_kind,
                "semantic_source_kind": semantic_source_kind,
                "source_evidence": source_evidence,
            },
            logical_key=f"{source_kind}:{logical_video_id}:failure_episode",
        )
        chunk.knowledge_type = "episodic"
        chunk.review_status = "candidate"
        chunks.append(chunk)
    return chunks, passed


def update_from_knowledge_snapshot(
    snapshot: Mapping[str, Any],
    *,
    video_dir: Path,
    updated_at: str,
) -> dict[str, Any]:
    chunks, passed = chunks_from_knowledge_snapshot(
        snapshot,
        video_dir=video_dir,
        updated_at=updated_at,
    )
    changed = append_unique(chunks)
    return {
        "knowledge_manifest": str(kc.active_manifest_path()),
        "chunks_changed": changed,
        "chunks_added": changed,
        "chunk_count": len(chunks),
        "outcome": "qa_passed" if passed else "qa_failed",
    }


def update_video_dir(video_dir: Path) -> dict[str, Any]:
    return update_from_knowledge_snapshot(
        build_knowledge_input_snapshot(video_dir),
        video_dir=video_dir,
        updated_at=time.strftime("%F %T"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--status", choices=["candidate"], default="candidate")
    args = parser.parse_args()

    print(
        json.dumps(
            update_video_dir(args.video_dir),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
