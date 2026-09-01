#!/usr/bin/env python3
"""Export the current public showcase as portable, metadata-only knowledge."""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


PUBLIC_STATUS = "public-at-pinned-snapshot"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
FORBIDDEN_VALUES = (
    re.compile(r"/(?:Users|Volumes)/"),
    re.compile(r"(?:^|/)xwechat" r"_files/", re.IGNORECASE),
    re.compile(r"(?:^|/)private_evidence", re.IGNORECASE),
    re.compile("file:" + "//", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
    re.compile(r"(?:gh[pousr]_|sk-|hk-)[A-Za-z0-9_-]{20,}"),
)
SEMANTIC_INPUT_KEYS = {
    "adapter_profile",
    "asset_role",
    "batch_partition",
    "candidate_number",
    "legacy_edit_task_id",
    "native_motion_required",
    "public_task_id",
    "runtime_requirements",
    "special_requirements",
    "target_object",
    "task_id",
    "turntable_only",
}
PUBLIC_URL_KEYS = {
    "geometry_source_url",
    "material_nodes_public_url",
    "material_tutorial_url",
    "source_image_public_url",
    "tutorial_cover_public_url",
    "workflow_code_public_url",
}
ATTESTED_ROLES = {
    "accepted_preview": "accepted-preview",
    "accepted_project": "accepted-project",
    "accepted_video": "accepted-video",
    "material_nodes": "material-nodes",
}


class ExportError(RuntimeError):
    """The source catalog cannot be exported without weakening a contract."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExportError(f"JSON root must be an object: {path}")
    return value


def _write_object(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _https(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("https://") or "@" in text.split("/", 3)[2]:
        raise ExportError(f"{label} must be a credential-free HTTPS URL")
    return text


def _artifacts(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for prefix, role in ATTESTED_ROLES.items():
        digest = str(inputs.get(f"{prefix}_sha256") or "").strip()
        if not digest:
            continue
        if not SHA256.fullmatch(digest):
            raise ExportError(f"invalid {prefix}_sha256")
        row: dict[str, Any] = {"role": role, "sha256": digest}
        size = inputs.get(f"{prefix}_size_bytes")
        if size is not None:
            if not isinstance(size, int) or isinstance(size, bool) or size < 1:
                raise ExportError(f"invalid {prefix}_size_bytes")
            row["size_bytes"] = size
        public_url = inputs.get(f"{prefix}_public_url")
        if public_url:
            row["public_url"] = _https(public_url, f"{prefix}_public_url")
        result.append(row)
    return result


def _sanitize_inputs(
    inputs: Mapping[str, Any], distribution: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, Any]:
    required = inputs.get("required")
    if not isinstance(required, list) or any(
        not isinstance(value, str) for value in required
    ):
        raise ExportError("recipe inputs.required must be a string list")
    verification = distribution.get("user_asset_verification")
    if not isinstance(verification, Mapping):
        raise ExportError("recipe lacks user_asset_verification")
    hashes = verification.get("accepted_sha256")
    if not isinstance(hashes, list) or any(
        not SHA256.fullmatch(str(value)) for value in hashes
    ):
        raise ExportError("recipe accepted_sha256 must contain only SHA-256 digests")
    result: dict[str, Any] = {
        "required": list(required),
        "source_asset_binding": {
            "delivery": distribution.get("asset_delivery"),
            "requires_user_asset": distribution.get("requires_user_asset"),
            "accepted_sha256": list(hashes),
            "semantic_validation_required": verification.get(
                "semantic_validation_required"
            ),
        },
        "public_acceptance": {
            "item_id": item.get("item_id"),
            "media_url": _https(item.get("media_url"), "public media URL"),
            "media_role": "public-acceptance-reference-only",
            "media_sha256": item.get("media_sha256"),
            "media_size_bytes": item.get("media_size_bytes"),
            "snapshot_commit": item.get("commit_sha"),
        },
    }
    tutorial_url = item.get("tutorial_url")
    if tutorial_url:
        result["public_acceptance"]["tutorial_url"] = _https(
            tutorial_url, "public tutorial URL"
        )
    expected = str(inputs.get("expected_asset_sha256") or "").strip()
    if expected:
        if not SHA256.fullmatch(expected):
            raise ExportError("invalid expected_asset_sha256")
        result["source_asset_binding"]["expected_sha256"] = expected
    expected_size = inputs.get("expected_asset_size_bytes")
    if expected_size is not None:
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 1
        ):
            raise ExportError("invalid expected_asset_size_bytes")
        result["source_asset_binding"]["expected_size_bytes"] = expected_size
    semantic = {
        key: copy.deepcopy(inputs[key])
        for key in sorted(SEMANTIC_INPUT_KEYS)
        if key in inputs
    }
    if semantic:
        result["semantic_contract"] = semantic
    public_urls = {
        key: _https(inputs[key], key)
        for key in sorted(PUBLIC_URL_KEYS)
        if inputs.get(key)
    }
    if public_urls:
        result["public_evidence_urls"] = public_urls
    artifacts = _artifacts(inputs)
    if artifacts:
        result["artifact_attestations"] = artifacts
    return result


def _sanitize_recipe(
    recipe_id: str, recipe: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, Any]:
    if recipe.get("id") != recipe_id:
        raise ExportError(f"recipe key/id mismatch: {recipe_id}")
    distribution = recipe.get("distribution_contract")
    if not isinstance(distribution, Mapping):
        raise ExportError(f"recipe lacks distribution contract: {recipe_id}")
    if distribution.get("auto_download_allowed") is not False:
        raise ExportError(f"recipe permits asset auto-download: {recipe_id}")
    if distribution.get("asset_delivery") not in {"blocked", "user-supplied"}:
        raise ExportError(f"public export cannot bundle source assets: {recipe_id}")
    inputs = recipe.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ExportError(f"recipe inputs must be an object: {recipe_id}")
    gaps = recipe.get("gaps")
    if not isinstance(gaps, list):
        raise ExportError(f"recipe gaps must be a list: {recipe_id}")
    adapter = recipe.get("adapter_contract")
    if not isinstance(adapter, Mapping):
        raise ExportError(f"recipe adapter contract is missing: {recipe_id}")
    if adapter.get("visual_equivalence_verified") is not False:
        raise ExportError(
            f"public recipe unexpectedly claims visual verification: {recipe_id}"
        )
    video_replay = copy.deepcopy(recipe.get("video_replay") or {"enabled": False})
    if (
        not isinstance(video_replay, Mapping)
        or video_replay.get("enabled") is not False
    ):
        raise ExportError(
            f"public recipe unexpectedly enables video replay: {recipe_id}"
        )
    source_url = video_replay.get("source_url")
    if source_url:
        video_replay["source_url"] = _https(source_url, "video source URL")
    return {
        "id": recipe_id,
        "title": recipe.get("title"),
        "category": recipe.get("category"),
        "coverage_status": recipe.get("coverage_status"),
        "enabled": False,
        "mode": recipe.get("mode"),
        "entrypoint": None,
        "video_replay": video_replay,
        "inputs": _sanitize_inputs(inputs, distribution, item),
        "dynamic_contract": copy.deepcopy(recipe.get("dynamic_contract")),
        "evidence_paths": [
            "public_showcase_inventory.json",
            "public_media_attestations.json",
        ],
        "gaps": copy.deepcopy(gaps),
        "adapter_contract": copy.deepcopy(adapter),
        "distribution_contract": copy.deepcopy(distribution),
        "public_showcase_status": PUBLIC_STATUS,
        "portable_status": "metadata-only-not-executable",
    }


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _assert_portable(value: Any) -> None:
    for text in _strings(value):
        for pattern in FORBIDDEN_VALUES:
            if pattern.search(text):
                raise ExportError(
                    f"portable export contains forbidden value: {pattern.pattern}"
                )


def export(
    source_manifest: Path,
    source_inventory: Path,
    source_attestations: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source = _read_object(source_manifest)
    inventory = _read_object(source_inventory)
    attestations = _read_object(source_attestations)
    items = inventory.get("items")
    recipes = source.get("recipes")
    attested = attestations.get("items")
    if (
        not isinstance(items, list)
        or not isinstance(recipes, Mapping)
        or not isinstance(attested, Mapping)
    ):
        raise ExportError("source inventory, recipes, or attestations are malformed")
    item_by_recipe = {
        str(item.get("recipe_id")): item for item in items if isinstance(item, Mapping)
    }
    if len(item_by_recipe) != 61 or set(item_by_recipe) != set(attested):
        raise ExportError(
            "public inventory and media attestations must contain the same 61 IDs"
        )
    public_recipes = {
        recipe_id: recipe
        for recipe_id, recipe in recipes.items()
        if isinstance(recipe, Mapping)
        and recipe.get("public_showcase_status") == PUBLIC_STATUS
    }
    if set(public_recipes) != set(item_by_recipe):
        raise ExportError("public recipes do not exactly match the public inventory")
    source_commit = str(inventory.get("source", {}).get("commit_sha") or "")
    if (
        not GIT_COMMIT.fullmatch(source_commit)
        or attestations.get("source_commit") != source_commit
    ):
        raise ExportError("public source commits are missing or inconsistent")
    sanitized = {
        recipe_id: _sanitize_recipe(
            recipe_id, public_recipes[recipe_id], item_by_recipe[recipe_id]
        )
        for recipe_id in sorted(public_recipes)
    }
    section_counts = Counter(str(item.get("section")) for item in items)
    gap_count = sum(len(recipe["gaps"]) for recipe in sanitized.values())
    dynamic_count = sum(
        1
        for recipe in sanitized.values()
        if isinstance(recipe.get("dynamic_contract"), Mapping)
        and recipe["dynamic_contract"].get("required") is True
        and recipe["dynamic_contract"].get("turntable_only") is not True
    )
    policy = copy.deepcopy(source.get("distribution_policy"))
    if not isinstance(policy, Mapping):
        raise ExportError("source distribution policy is missing")
    code_license = policy.get("code_license")
    if (
        not isinstance(code_license, Mapping)
        or code_license.get("publication_ready") is not False
    ):
        raise ExportError(
            "code publication must remain blocked until an owner selects a license"
        )
    public_source = copy.deepcopy(source.get("public_showcase_source"))
    if not isinstance(public_source, dict):
        raise ExportError("public showcase source is missing")
    public_source["inventory_path"] = "public_showcase_inventory.json"
    public_source["media_attestations_path"] = "public_media_attestations.json"
    catalog = {
        "schema": "showcase-reproduction-public-catalog.v1",
        "public_showcase_source": public_source,
        "distribution_policy": policy,
        "coverage_status_values": list(source.get("coverage_status_values") or []),
        "showcase_sections": dict(sorted(section_counts.items())),
        "coverage_claim": {
            "public_item_count": len(items),
            "recipe_count": len(sanitized),
            "enabled_count": 0,
            "visual_equivalence_verified_count": 0,
            "dynamic_contract_count": dynamic_count,
            "documented_gap_count": gap_count,
            "strict_reproduction_coverage": False,
            "scope": "public identity, input binding, acceptance, and distribution contracts",
        },
        "source_assets_included": False,
        "tutorial_media_included": False,
        "recipes": sanitized,
    }
    portable_inventory = copy.deepcopy(inventory)
    portable_inventory["source"]["inventory_path"] = "public_showcase_inventory.json"
    portable_inventory["source"]["media_attestations_path"] = (
        "public_media_attestations.json"
    )
    portable_attestations = copy.deepcopy(attestations)
    for value in (catalog, portable_inventory, portable_attestations):
        _assert_portable(value)
    return catalog, portable_inventory, portable_attestations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-inventory", type=Path, required=True)
    parser.add_argument("--source-attestations", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalog, inventory, attestations = export(
        args.source_manifest, args.source_inventory, args.source_attestations
    )
    output_root = args.output_root.expanduser().resolve()
    _write_object(output_root / "manifest.json", catalog)
    _write_object(output_root / "public_showcase_inventory.json", inventory)
    _write_object(output_root / "public_media_attestations.json", attestations)
    print(
        json.dumps(
            {
                "recipes": len(catalog["recipes"]),
                "public_items": len(inventory["items"]),
                "output_root": str(output_root),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExportError as exc:
        print(f"error: {exc}")
        raise SystemExit(2)
