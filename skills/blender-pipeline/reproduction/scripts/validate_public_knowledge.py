#!/usr/bin/env python3
"""Validate the portable public-showcase catalog and distribution gates."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


EXPECTED_COMMIT = "1115f547a5ecccdeacd0e2dde396326f040b89f6"
EXPECTED_SECTIONS = {
    "duck": 6,
    "editing-workflow-chromatic-duck": 1,
    "glass-heart": 10,
    "hero": 4,
    "materials": 5,
    "objects": 16,
    "sofa": 18,
    "workflow-stained-glass": 1,
}
EXPECTED_UNIQUE_MEDIA = 59
EXPECTED_UNIQUE_MEDIA_BYTES = 62_243_603
EXPECTED_DELIVERIES = {"blocked": 30, "user-supplied": 31}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PRIVATE_MARKERS = (
    "/" + "Users/",
    "/" + "Volumes/",
    "xwechat" + "_files",
    "file:" + "//",
    "private" + "_evidence",
    "data:" + "image/",
    "data:" + "video/",
)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} root must be an object")
    return value


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def validate(root: Path) -> list[str]:
    issues: list[str] = []
    root = root.resolve()
    try:
        catalog = _read(root / "manifest.json")
        inventory = _read(root / "public_showcase_inventory.json")
        attestations = _read(root / "public_media_attestations.json")
    except ValueError as exc:
        return [str(exc)]
    recipes = catalog.get("recipes")
    items = inventory.get("items")
    media = attestations.get("items")
    if not isinstance(recipes, Mapping) or len(recipes) != 61:
        issues.append("catalog must contain exactly 61 recipes")
        recipes = {}
    if not isinstance(items, list) or len(items) != 61:
        issues.append("inventory must contain exactly 61 items")
        items = []
    if not isinstance(media, Mapping) or len(media) != 61:
        issues.append("media attestations must contain exactly 61 items")
        media = {}
    item_map = {
        str(item.get("recipe_id")): item for item in items if isinstance(item, Mapping)
    }
    if set(recipes) != set(item_map) or set(recipes) != set(media):
        issues.append("recipe, inventory, and attestation IDs must be identical")
    source = catalog.get("public_showcase_source")
    commit = source.get("commit_sha") if isinstance(source, Mapping) else None
    if commit != EXPECTED_COMMIT:
        issues.append("catalog is not pinned to the current approved public commit")
    if inventory.get("source", {}).get("commit_sha") != commit:
        issues.append("inventory commit differs from catalog")
    if attestations.get("source_commit") != commit:
        issues.append("attestation commit differs from catalog")
    sections = Counter(
        str(item.get("section")) for item in items if isinstance(item, Mapping)
    )
    if dict(sections) != EXPECTED_SECTIONS:
        issues.append("public section counts differ from the approved snapshot")
    if catalog.get("showcase_sections") != EXPECTED_SECTIONS:
        issues.append("catalog section counts differ from inventory")

    deliveries: Counter[str] = Counter()
    gap_count = 0
    dynamic_count = 0
    for recipe_id, recipe in recipes.items():
        if not isinstance(recipe, Mapping):
            issues.append(f"{recipe_id}: recipe must be an object")
            continue
        if recipe.get("enabled") is not False:
            issues.append(f"{recipe_id}: public recipe must remain disabled")
        if recipe.get("entrypoint") is not None:
            issues.append(
                f"{recipe_id}: metadata-only recipe must not ship an entrypoint"
            )
        if recipe.get("portable_status") != "metadata-only-not-executable":
            issues.append(f"{recipe_id}: invalid portable status")
        adapter = recipe.get("adapter_contract")
        if (
            not isinstance(adapter, Mapping)
            or adapter.get("visual_equivalence_verified") is not False
        ):
            issues.append(f"{recipe_id}: visual equivalence must remain unverified")
        gaps = recipe.get("gaps")
        if not isinstance(gaps, list):
            issues.append(f"{recipe_id}: gaps must be a list")
        else:
            gap_count += len(gaps)
        dynamic = recipe.get("dynamic_contract")
        if (
            isinstance(dynamic, Mapping)
            and dynamic.get("required") is True
            and dynamic.get("turntable_only") is not True
        ):
            dynamic_count += 1
        distribution = recipe.get("distribution_contract")
        if not isinstance(distribution, Mapping):
            issues.append(f"{recipe_id}: distribution contract is missing")
            continue
        delivery = str(distribution.get("asset_delivery"))
        deliveries[delivery] += 1
        if delivery not in EXPECTED_DELIVERIES:
            issues.append(f"{recipe_id}: source assets must not be bundled")
        if distribution.get("auto_download_allowed") is not False:
            issues.append(f"{recipe_id}: automatic asset download must be false")
        if distribution.get("bundled_asset_relative_path") is not None:
            issues.append(f"{recipe_id}: metadata export cannot name a bundled asset")
        evidence_paths = recipe.get("evidence_paths")
        if evidence_paths != [
            "public_showcase_inventory.json",
            "public_media_attestations.json",
        ]:
            issues.append(f"{recipe_id}: invalid portable evidence paths")
        item = item_map.get(recipe_id, {})
        acceptance = recipe.get("inputs", {}).get("public_acceptance", {})
        if acceptance.get("media_role") != "public-acceptance-reference-only":
            issues.append(f"{recipe_id}: public media role is not acceptance-only")
        if acceptance.get("media_sha256") != item.get("media_sha256"):
            issues.append(f"{recipe_id}: acceptance hash differs from inventory")
        attestation = media.get(recipe_id, {})
        if (
            attestation.get("path") != item.get("media_site_path")
            or attestation.get("sha256") != item.get("media_sha256")
            or attestation.get("size_bytes") != item.get("media_size_bytes")
        ):
            issues.append(f"{recipe_id}: media attestation differs from inventory")
        if not SHA256.fullmatch(str(item.get("media_sha256") or "")):
            issues.append(f"{recipe_id}: invalid public media SHA-256")
    if dict(deliveries) != EXPECTED_DELIVERIES:
        issues.append("asset-delivery counts differ from the approved snapshot")
    if dynamic_count != 16:
        issues.append("dynamic-contract count must be 16")
    if gap_count != 129:
        issues.append("documented public gap count must be 129")
    unique_media = {
        str(item.get("sha256")): item
        for item in media.values()
        if isinstance(item, Mapping)
    }
    if len(unique_media) != EXPECTED_UNIQUE_MEDIA:
        issues.append("unique public media count must be 59")
    if (
        sum(int(item.get("size_bytes") or 0) for item in unique_media.values())
        != EXPECTED_UNIQUE_MEDIA_BYTES
    ):
        issues.append("unique public media bytes differ from the approved snapshot")
    policy = catalog.get("distribution_policy", {}).get("code_license", {})
    if (
        policy.get("status") != "pending-selection"
        or policy.get("license_id") is not None
        or policy.get("license_file") is not None
        or policy.get("publication_ready") is not False
    ):
        issues.append("code license must remain pending until the owner selects one")
    if catalog.get("source_assets_included") is not False:
        issues.append("source_assets_included must be false")
    if catalog.get("tutorial_media_included") is not False:
        issues.append("tutorial_media_included must be false")
    for text in _strings((catalog, inventory, attestations)):
        if any(marker.lower() in text.lower() for marker in PRIVATE_MARKERS):
            issues.append(
                "portable knowledge contains a private path or embedded media"
            )
            break
    return sorted(set(issues))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1] / "knowledge"
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    issues = validate(args.root)
    if args.json:
        print(json.dumps({"status": "fail" if issues else "pass", "issues": issues}))
    elif issues:
        for issue in issues:
            print(f"error: {issue}")
    else:
        print("PASS: portable public showcase knowledge")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
