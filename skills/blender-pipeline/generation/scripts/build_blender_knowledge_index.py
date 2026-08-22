#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import blender_knowledge_common as kc


EXTERNAL_MANIFEST_SCHEMA = "render-knowledge-external-manifest-v1"
EXTERNAL_SOURCE_KINDS = {"total_asset", "bili_linked_asset", "video_replay"}
EXTERNAL_ARTIFACT_TYPES = {
    "total_asset": {"render_review", "formal_status", "knowledge_event"},
    "bili_linked_asset": {"asset_audit", "render_review"},
    "video_replay": {"pipeline_review", "knowledge_event"},
}
EXTERNAL_JSON_SUFFIXES = {".json", ".jsonl"}
GENERATION_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = GENERATION_ROOT.parent


def _bounded_manifest_markdown_paths(manifest_path: Path) -> list[Path]:
    """Resolve one ordered knowledge manifest without leaving the skill root."""

    skill_root = SKILL_ROOT.resolve()
    manifest = manifest_path.expanduser().resolve()
    if manifest != skill_root and skill_root not in manifest.parents:
        raise kc.KnowledgeManifestError(
            f"knowledge manifest is outside the skill root: {manifest}"
        )
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise kc.KnowledgeManifestError(
            f"knowledge manifest is unreadable: {manifest}: {exc}"
        ) from exc
    documents = value.get("documents") if isinstance(value, dict) else None
    if not isinstance(documents, list) or not documents:
        raise kc.KnowledgeManifestError(
            "knowledge manifest documents must be non-empty"
        )
    paths: list[Path] = []
    seen: set[Path] = set()
    for index, entry in enumerate(documents):
        raw = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            raise kc.KnowledgeManifestError(
                f"knowledge manifest document {index} has no path"
            )
        relative = Path(raw)
        if relative.is_absolute() or relative.suffix.casefold() != ".md":
            raise kc.KnowledgeManifestError(
                f"knowledge manifest document {index} is not a relative Markdown path"
            )
        path = (manifest.parent / relative).resolve()
        if path != skill_root and skill_root not in path.parents:
            raise kc.KnowledgeManifestError(
                f"knowledge document escapes the skill root: {raw}"
            )
        if path in seen:
            raise kc.KnowledgeManifestError(
                f"knowledge manifest contains duplicate document: {raw}"
            )
        if not path.is_file():
            raise kc.KnowledgeManifestError(
                f"knowledge manifest document is missing: {path}"
            )
        seen.add(path)
        paths.append(path)
    return paths


def _knowledge_manifest_paths() -> list[Path]:
    roots = [SKILL_ROOT / "knowledge"]
    for raw in os.environ.get("BLENDER_KNOWLEDGE_REFERENCE_ROOTS", "").split(
        os.pathsep
    ):
        if raw.strip():
            candidate = Path(raw).expanduser()
            roots.append(
                candidate if candidate.is_absolute() else SKILL_ROOT / candidate
            )
    manifests: list[Path] = []
    seen: set[Path] = set()
    skill_root = SKILL_ROOT.resolve()
    for root in roots:
        resolved = root.resolve()
        if resolved != skill_root and skill_root not in resolved.parents:
            raise kc.KnowledgeManifestError(
                f"knowledge reference root is outside the skill root: {resolved}"
            )
        manifest = resolved / "manifest.json"
        if manifest not in seen:
            seen.add(manifest)
            manifests.append(manifest)
    return manifests


def collect_skill_chunks() -> list[kc.KnowledgeChunk]:
    paths = [GENERATION_ROOT / "SKILL.md", SKILL_ROOT / "SKILL.md"]
    for manifest in _knowledge_manifest_paths():
        paths.extend(_bounded_manifest_markdown_paths(manifest))
    chunks: list[kc.KnowledgeChunk] = []
    for path in paths:
        if not path.is_file():
            raise kc.KnowledgeManifestError(
                f"required skill document is missing: {path}"
            )
        chunks.extend(
            kc.make_chunk(path, title, body, source_type="skill")
            for title, body in kc.split_markdown_sections(path)
        )
    return chunks


def collect_render_recipe_chunks() -> list[kc.KnowledgeChunk]:
    kc.ensure_script_dir_on_path()
    import render_knowledge

    path = Path(render_knowledge.__file__).resolve()
    chunks: list[kc.KnowledgeChunk] = []
    for recipe in render_knowledge.DEFAULT_RECIPES:
        chunk = kc.make_chunk(
            path,
            f"render_recipe:{recipe.recipe_id}",
            json.dumps(recipe.to_dict(), ensure_ascii=False, indent=2),
            source_type="api_note",
            extra={
                "recipe_id": recipe.recipe_id,
                "recipe_version": recipe.version,
                "recipe_schema_version": recipe.schema_version,
            },
            logical_key=f"render_recipe:{recipe.recipe_id}:{recipe.version}",
        )
        chunk.review_status = recipe.review_status
        chunks.append(chunk)
    return chunks


def _external_rows(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix.lower() == ".jsonl":
            rows: list[dict[str, Any]] = []
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="strict").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise kc.KnowledgeManifestError(
                        f"external JSONL row {line_number} is not an object: {path}"
                    )
                rows.append(value)
            return rows
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        kc.KnowledgeManifestError,
    ) as exc:
        raise kc.KnowledgeManifestError(
            f"invalid external knowledge artifact: {path}"
        ) from exc
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return [dict(row) for row in value]
    raise kc.KnowledgeManifestError(
        f"external knowledge artifact must contain an object or object list: {path}"
    )


def _external_row_identity(row: dict[str, Any], row_index: int, row_count: int) -> str:
    if row_count == 1:
        return "record"
    # Event streams may legitimately contain multiple episodes for one asset;
    # prefer their immutable event_id before falling back to asset identity.
    for key in ("event_id", "identity_key", "asset_id", "sample_id", "id"):
        value = str(row.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    raise kc.KnowledgeManifestError(
        f"external multi-record artifact row {row_index} lacks a stable identity"
    )


def collect_external_production_chunks(
    manifest_paths: list[Path] | tuple[Path, ...],
) -> list[kc.KnowledgeChunk]:
    """Read explicitly whitelisted production artifacts without modifying them."""

    chunks: list[kc.KnowledgeChunk] = []
    logical_ids: set[str] = set()
    for manifest_path in manifest_paths:
        manifest_path = Path(manifest_path).expanduser()
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8", errors="strict")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise kc.KnowledgeManifestError(
                f"invalid external knowledge manifest: {manifest_path}"
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != EXTERNAL_MANIFEST_SCHEMA
        ):
            raise kc.KnowledgeManifestError(
                f"unsupported external knowledge manifest schema: {manifest_path}"
            )
        roots_value = manifest.get("roots")
        entries = manifest.get("entries")
        if not isinstance(roots_value, dict) or not isinstance(entries, list):
            raise kc.KnowledgeManifestError(
                f"external manifest requires roots and entries: {manifest_path}"
            )
        roots: dict[str, Path] = {}
        for source_kind, root_value in roots_value.items():
            source_kind = str(source_kind)
            root = Path(str(root_value)).expanduser()
            if source_kind not in EXTERNAL_SOURCE_KINDS or not root.is_absolute():
                raise kc.KnowledgeManifestError(
                    f"invalid external root for {source_kind}: {root_value}"
                )
            resolved_root = root.resolve(strict=True)
            if not resolved_root.is_dir():
                raise kc.KnowledgeManifestError(
                    f"external root is not a directory: {root}"
                )
            roots[source_kind] = resolved_root
        for entry_index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise kc.KnowledgeManifestError(
                    f"external manifest entry {entry_index} is not an object"
                )
            source_kind = str(entry.get("source_kind") or "")
            artifact_type = str(entry.get("artifact_type") or "")
            logical_id = str(entry.get("logical_id") or "").strip()
            relative_text = str(entry.get("path") or "")
            if source_kind not in roots:
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} has no declared root"
                )
            if artifact_type not in EXTERNAL_ARTIFACT_TYPES[source_kind]:
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} has invalid artifact_type"
                )
            if not re.fullmatch(r"[A-Za-z0-9._:/-]+", logical_id):
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} requires a stable logical_id"
                )
            relative = Path(relative_text)
            if not relative_text or relative.is_absolute() or ".." in relative.parts:
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} path must be relative and contained"
                )
            candidate = roots[source_kind] / relative
            if candidate.is_symlink():
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} cannot be a symlink"
                )
            path = candidate.resolve(strict=True)
            try:
                path.relative_to(roots[source_kind])
            except ValueError as exc:
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} escapes its declared root"
                ) from exc
            if not path.is_file() or path.suffix.lower() not in EXTERNAL_JSON_SUFFIXES:
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} must be a JSON/JSONL file"
                )
            rows = _external_rows(path)
            if not rows:
                raise kc.KnowledgeManifestError(
                    f"external entry {entry_index} contains no records"
                )
            for row_index, row in enumerate(rows, start=1):
                record_id = _external_row_identity(row, row_index, len(rows))
                stable_key = f"external:{source_kind}:{logical_id}:{record_id}"
                if stable_key in logical_ids:
                    raise kc.KnowledgeManifestError(
                        f"duplicate external logical source id: {stable_key}"
                    )
                logical_ids.add(stable_key)
                chunk = kc.make_chunk(
                    path,
                    f"external:{source_kind}:{artifact_type}:{logical_id}:{record_id}",
                    json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True),
                    source_type="run_artifact",
                    extra={
                        "source_kind": source_kind,
                        "artifact_type": artifact_type,
                        "external_logical_id": logical_id,
                        "read_only_ingest": True,
                    },
                    logical_key=stable_key,
                )
                chunk.review_status = "candidate"
                chunks.append(chunk)
    return chunks


def build_chunks(
    external_manifests: list[Path] | tuple[Path, ...] = (),
) -> list[kc.KnowledgeChunk]:
    chunks = []
    chunks.extend(collect_skill_chunks())
    chunks.extend(collect_render_recipe_chunks())
    # Production evidence is never discovered implicitly. Callers must opt in
    # with a bounded manifest whose paths, kinds, and artifact types are
    # validated by collect_external_production_chunks().
    chunks.extend(collect_external_production_chunks(external_manifests))
    dedup: dict[str, kc.KnowledgeChunk] = {}
    for chunk in chunks:
        if len(chunk.text.strip()) < 80:
            continue
        dedup[chunk.source_id] = chunk
    return list(dedup.values())


def recreate_qdrant(chunks: list[kc.KnowledgeChunk], target: Path) -> dict[str, Any]:
    from qdrant_client.models import Distance, PointStruct, VectorParams

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=False)
    model = kc.load_embedder()
    sample = model.encode(["dimension probe"], normalize_embeddings=True)[0]
    client = kc.qdrant_client(target)
    try:
        client.recreate_collection(
            collection_name=kc.COLLECTION_NAME,
            vectors_config=VectorParams(size=len(sample), distance=Distance.COSINE),
        )
        texts = [f"{chunk.title}\n{chunk.text}" for chunk in chunks]
        vectors = model.encode(
            texts, batch_size=32, show_progress_bar=True, normalize_embeddings=True
        )
        points = [
            PointStruct(id=idx, vector=vector.tolist(), payload=chunk.payload())
            for idx, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]
        client.upsert(collection_name=kc.COLLECTION_NAME, points=points)
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    return {
        "collection": kc.COLLECTION_NAME,
        "points": len(points),
        "vector_size": len(sample),
    }


def new_build_id(chunks: list[kc.KnowledgeChunk]) -> str:
    digest = hashlib.sha256(
        "\n".join(sorted(chunk.source_hash for chunk in chunks)).encode("utf-8")
    ).hexdigest()[:10]
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{digest}"


def build_generation(
    chunks: list[kc.KnowledgeChunk], *, manifest_only: bool
) -> dict[str, Any]:
    build_id = new_build_id(chunks)
    build_dir = kc.BUILDS_PATH / build_id
    suffix = 1
    while build_dir.exists():
        build_dir = kc.BUILDS_PATH / f"{build_id}-{suffix}"
        suffix += 1
    build_id = build_dir.name
    build_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = build_dir / "blender_knowledge_manifest.jsonl"
    qdrant_path = build_dir / "qdrant"
    summary_path = build_dir / "index_summary.json"
    try:
        kc.write_jsonl(
            [chunk.payload() | {"text": chunk.text} for chunk in chunks], manifest_path
        )
        summary: dict[str, Any] = {
            "build_id": build_id,
            "manifest": str(manifest_path),
            "chunks": len(chunks),
            "render_recipes": len(collect_render_recipe_chunks()),
            "review_status_counts": {},
            "asset_family_counts": {},
        }
        for chunk in chunks:
            summary["review_status_counts"][chunk.review_status] = (
                summary["review_status_counts"].get(chunk.review_status, 0) + 1
            )
            summary["asset_family_counts"][chunk.asset_family] = (
                summary["asset_family_counts"].get(chunk.asset_family, 0) + 1
            )
        if not manifest_only:
            summary["qdrant"] = recreate_qdrant(chunks, qdrant_path)
        kc.atomic_write_json(summary_path, summary)
        current = {
            "schema_version": "blender-knowledge-current-v1",
            "build_id": build_id,
            "manifest": manifest_path.relative_to(kc.KNOWLEDGE_ROOT).as_posix(),
            "summary": summary_path.relative_to(kc.KNOWLEDGE_ROOT).as_posix(),
            "activated_at": time.strftime("%FT%T%z"),
        }
        if not manifest_only:
            current["qdrant"] = qdrant_path.relative_to(kc.KNOWLEDGE_ROOT).as_posix()
        kc.atomic_write_json(kc.CURRENT_BUILD_PATH, current)
        return summary
    except BaseException:
        # The active pointer is written last. Removing an unactivated partial
        # generation cannot affect readers of the previous current.json.
        shutil.rmtree(build_dir, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the local Blender pipeline knowledge index."
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Write JSONL manifest without rebuilding Qdrant.",
    )
    parser.add_argument(
        "--external-manifest",
        type=Path,
        action="append",
        default=[],
        help="Explicit read-only production artifact manifest; may be repeated.",
    )
    args = parser.parse_args()

    env_manifests = [
        Path(value).expanduser()
        for value in os.environ.get("BLENDER_KNOWLEDGE_EXTERNAL_MANIFESTS", "").split(
            os.pathsep
        )
        if value.strip()
    ]
    external_manifests = [*env_manifests, *args.external_manifest]

    with kc.knowledge_store_lock():
        chunks = build_chunks(
            external_manifests=external_manifests,
        )
        summary = build_generation(chunks, manifest_only=args.manifest_only)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
