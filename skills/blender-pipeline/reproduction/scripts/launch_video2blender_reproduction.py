#!/usr/bin/env python3
"""Fail-closed launcher for knowledge-backed Video2Blender reproduction runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPRODUCTION_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE_ROOT = REPRODUCTION_ROOT / "knowledge"
DEFAULT_RUN_ROOT = (
    Path(
        os.environ.get(
            "VIDEO2BLENDER_REPRODUCTION_OUTPUT_ROOT",
            str(REPRODUCTION_ROOT / "output"),
        )
    )
    .expanduser()
    .resolve()
)
VIDEO_REPLAY_MAIN = PROJECT_ROOT / "generation/scripts/run_video_replay_main.py"
RUN_SCHEMA = "video2blender-reproduction-run.v1"
MANIFEST_NAMES = ("manifest.json", "catalog.json")
ENTRYPOINT_KINDS = {"python", "blender_project"}
ENTRYPOINT_PLACEHOLDERS = {
    "{run_manifest}",
    "{video_dir}",
    "{run_dir}",
    "{output_dir}",
    "{asset}",
    "{image}",
    "{tutorial}",
}
RECIPE_MODES = {"deterministic-script", "source-project", "documented-gap"}
INPUT_ALIASES = {
    "video": "video_url",
    "videos": "video_url",
    "url": "video_url",
    "video_url": "video_url",
    "tutorial": "tutorial",
    "tutorials": "tutorial",
    "image": "image",
    "images": "image",
    "reference": "image",
    "references": "image",
    "asset": "asset",
    "source_asset": "asset",
    "source_project": "asset",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".exr"}
TUTORIAL_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".html", ".htm"}
ASSET_SUFFIXES = {".blend", ".fbx", ".obj", ".glb", ".gltf", ".usd", ".usda", ".usdc"}
DISTRIBUTION_SCHEMA_VERSION = "1.0"
DISTRIBUTION_POLICY_MODE = "hybrid"
CODE_DISTRIBUTION = "open-source"
ASSET_DELIVERY_MODES = {"bundled-open", "user-supplied", "blocked"}
SOURCE_LICENSE_STATUSES = {
    "verified-open",
    "restricted",
    "unknown",
    "not-applicable",
    "blocked",
}
USER_SUPPLIED_LICENSE_STATUSES = {"verified-open", "restricted", "unknown"}
OPEN_SOURCE_CONTENTS = {"recipe", "adapter", "parameters", "verification-contract"}


class LaunchError(RuntimeError):
    """An input or knowledge contract is not safe enough to execute."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LaunchError(f"required JSON file is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LaunchError(f"required JSON file is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LaunchError(f"JSON root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validate_url(value: str, label: str) -> str:
    if not _is_url(value):
        raise LaunchError(f"{label} must be an http(s) URL: {value!r}")
    if urllib.parse.urlparse(value).username or urllib.parse.urlparse(value).password:
        raise LaunchError(f"{label} must not embed credentials")
    return value


def _local_input(value: str, *, label: str, suffixes: set[str]) -> dict[str, Any]:
    if _is_url(value):
        return {"kind": "url", "value": _validate_url(value, label)}
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise LaunchError(f"{label} file does not exist: {path}")
    if suffixes and path.suffix.lower() not in suffixes:
        allowed = ", ".join(sorted(suffixes))
        raise LaunchError(
            f"{label} has unsupported suffix {path.suffix!r}; expected one of: {allowed}"
        )
    return {
        "kind": "file",
        "value": str(path),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _asset_input(value: str) -> dict[str, Any]:
    if _is_url(value):
        raise LaunchError(
            "--asset must be a local file; remote assets are not executed"
        )
    return _local_input(value, label="asset", suffixes=ASSET_SUFFIXES)


def _find_manifest(knowledge_root: Path) -> Path:
    if knowledge_root.is_file():
        if knowledge_root.name not in MANIFEST_NAMES:
            raise LaunchError(
                f"knowledge manifest must be named one of {MANIFEST_NAMES}: {knowledge_root}"
            )
        return knowledge_root.resolve()
    if not knowledge_root.is_dir():
        raise LaunchError(
            "showcase reproduction knowledge package is missing; expected "
            f"{knowledge_root / 'manifest.json'} (or catalog.json)"
        )
    found = [
        knowledge_root / name
        for name in MANIFEST_NAMES
        if (knowledge_root / name).is_file()
    ]
    if len(found) != 1:
        raise LaunchError(
            f"knowledge root must contain exactly one of {MANIFEST_NAMES}; found {len(found)} in {knowledge_root}"
        )
    return found[0].resolve()


def _recipe_map(catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    recipes = catalog.get("recipes")
    result: dict[str, dict[str, Any]] = {}
    if isinstance(recipes, Mapping):
        iterator: Iterable[tuple[str, Any]] = (
            (str(key), value) for key, value in recipes.items()
        )
    elif isinstance(recipes, list):
        iterator = (
            (str(item.get("id") or item.get("recipe_id") or ""), item)
            for item in recipes
            if isinstance(item, Mapping)
        )
    else:
        raise LaunchError("knowledge manifest recipes must be a mapping or list")
    for key, raw in iterator:
        if not key or not isinstance(raw, Mapping):
            raise LaunchError("every recipe must be an object with a non-empty id")
        recipe = dict(raw)
        recipe_id = str(recipe.get("id") or recipe.get("recipe_id") or key).strip()
        if recipe_id != key and isinstance(recipes, Mapping):
            raise LaunchError(f"recipe key/id mismatch: {key!r} != {recipe_id!r}")
        if recipe_id in result:
            raise LaunchError(f"duplicate recipe id: {recipe_id}")
        result[recipe_id] = recipe
    if not result:
        raise LaunchError("knowledge manifest contains no recipes")
    return result


def _path_within(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _resolve_package_path(value: str, *, knowledge_root: Path, label: str) -> Path:
    raw = Path(value).expanduser()
    candidates = (
        [raw] if raw.is_absolute() else [knowledge_root / raw, PROJECT_ROOT / raw]
    )
    existing = [candidate.resolve() for candidate in candidates if candidate.exists()]
    if len(existing) != 1:
        raise LaunchError(
            f"{label} must resolve to exactly one existing path: {value!r}"
        )
    resolved = existing[0]
    if not _path_within(resolved, (knowledge_root, PROJECT_ROOT)):
        raise LaunchError(
            f"{label} escapes the allowed project/knowledge roots: {resolved}"
        )
    return resolved


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _validate_evidence_paths(
    recipe: Mapping[str, Any], knowledge_root: Path
) -> list[str]:
    resolved: list[str] = []
    for value in _strings(recipe.get("evidence_paths")):
        if _is_url(value):
            raise LaunchError(
                f"recipe evidence_paths must be package-local, not a URL: {value}"
            )
        path = _resolve_package_path(
            value, knowledge_root=knowledge_root, label="evidence path"
        )
        if not path.is_file():
            raise LaunchError(f"recipe evidence path is not a file: {path}")
        resolved.append(str(path))
    if not resolved:
        raise LaunchError("recipe has no verified evidence_paths")
    return sorted(set(resolved))


def _required_inputs(value: Any) -> set[str]:
    required: set[str] = set()
    if isinstance(value, list):
        items = list(value)
    elif isinstance(value, Mapping):
        declared = value.get("required", [])
        if not isinstance(declared, list):
            raise LaunchError("recipe inputs.required must be a list")
        items = list(declared)
        for key, spec in value.items():
            if key == "required":
                continue
            if spec is True or (
                isinstance(spec, Mapping) and spec.get("required") is True
            ):
                items.append(key)
    elif value in (None, ""):
        return required
    else:
        raise LaunchError("recipe inputs must be a list or object")
    for item in items:
        token = str(item).strip().lower().replace("-", "_")
        canonical = INPUT_ALIASES.get(token)
        if not canonical:
            raise LaunchError(
                f"recipe declares an unsupported required input: {item!r}"
            )
        required.add(canonical)
    return required


def _validate_dynamic_contract(recipe: Mapping[str, Any]) -> None:
    contract = recipe.get("dynamic_contract")
    if contract in (None, {}):
        return
    if not isinstance(contract, Mapping):
        raise LaunchError("dynamic_contract must be an object")
    if contract.get("required") is not True:
        return
    missing = []
    if not str(contract.get("motion_type") or "").strip():
        missing.append("motion_type")
    if not isinstance(contract.get("subject_channels"), list) or not contract.get(
        "subject_channels"
    ):
        missing.append("subject_channels")
    acceptance = contract.get("acceptance")
    if not isinstance(acceptance, (list, Mapping)) or not acceptance:
        missing.append("acceptance")
    if not isinstance(contract.get("turntable_only"), bool):
        missing.append("turntable_only")
    if missing:
        raise LaunchError(
            "dynamic recipe contract is incomplete: " + ", ".join(missing)
        )


def _nonempty_string_list(value: Any, *, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise LaunchError(f"{label} must be a non-empty string list")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise LaunchError(f"{label} must not contain duplicates")
    return normalized


def _validate_asset_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LaunchError("distribution_contract.asset_provenance must be an object")
    missing = sorted(
        field
        for field in ("evidence_status", "license_id", "evidence_refs")
        if field not in value
    )
    if missing:
        raise LaunchError(
            "distribution_contract.asset_provenance is incomplete: missing "
            + ", ".join(missing)
        )
    evidence_status = value.get("evidence_status")
    if not isinstance(evidence_status, str) or not evidence_status.strip():
        raise LaunchError(
            "distribution_contract.asset_provenance.evidence_status must be a non-empty string"
        )
    license_id = value.get("license_id")
    if license_id is not None and (
        not isinstance(license_id, str) or not license_id.strip()
    ):
        raise LaunchError(
            "distribution_contract.asset_provenance.license_id must be a non-empty string or null"
        )
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list) or any(
        not isinstance(item, str) or not item.strip() for item in evidence_refs
    ):
        raise LaunchError(
            "distribution_contract.asset_provenance.evidence_refs must be a string list"
        )
    normalized = dict(value)
    normalized["evidence_status"] = evidence_status.strip()
    normalized["license_id"] = (
        license_id.strip() if isinstance(license_id, str) else None
    )
    normalized["evidence_refs"] = [item.strip() for item in evidence_refs]
    return normalized


def _validate_user_asset_verification(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LaunchError(
            "distribution_contract.user_asset_verification must be an object"
        )
    missing = sorted(
        field
        for field in ("accepted_sha256", "semantic_validation_required")
        if field not in value
    )
    if missing:
        raise LaunchError(
            "distribution_contract.user_asset_verification is incomplete: missing "
            + ", ".join(missing)
        )
    hashes = value.get("accepted_sha256")
    if not isinstance(hashes, list) or any(
        not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
        for item in hashes
    ):
        raise LaunchError(
            "distribution_contract.user_asset_verification.accepted_sha256 "
            "must be a list of SHA-256 digests"
        )
    normalized_hashes = list(hashes)
    if len(set(normalized_hashes)) != len(normalized_hashes):
        raise LaunchError(
            "distribution_contract.user_asset_verification.accepted_sha256 "
            "must not contain duplicates"
        )
    semantic_required = value.get("semantic_validation_required")
    if not isinstance(semantic_required, bool):
        raise LaunchError(
            "distribution_contract.user_asset_verification.semantic_validation_required "
            "must be a boolean"
        )
    normalized = dict(value)
    normalized["accepted_sha256"] = normalized_hashes
    normalized["semantic_validation_required"] = semantic_required
    return normalized


def _resolve_bundled_asset(value: str, *, knowledge_root: Path) -> Path:
    if _is_url(value):
        raise LaunchError(
            "distribution_contract.bundled_asset_relative_path must not be a URL"
        )
    relative = Path(value).expanduser()
    if relative.is_absolute() or ".." in relative.parts:
        raise LaunchError(
            "distribution_contract.bundled_asset_relative_path must be a safe package-relative path"
        )
    resolved = (knowledge_root / relative).resolve()
    if not _path_within(resolved, (knowledge_root,)):
        raise LaunchError(f"bundled asset escapes the knowledge package: {resolved}")
    if not resolved.is_file():
        raise LaunchError(f"bundled asset file does not exist: {resolved}")
    if resolved.suffix.lower() not in ASSET_SUFFIXES:
        allowed = ", ".join(sorted(ASSET_SUFFIXES))
        raise LaunchError(
            f"bundled asset has unsupported suffix {resolved.suffix!r}; expected one of: {allowed}"
        )
    return resolved


def _validate_distribution_contract(
    recipe: Mapping[str, Any], knowledge_root: Path
) -> tuple[dict[str, Any], Path | None]:
    value = recipe.get("distribution_contract")
    if not isinstance(value, Mapping):
        raise LaunchError(
            "recipe distribution_contract is required and must be an object"
        )
    required_fields = {
        "schema_version",
        "policy_mode",
        "code_distribution",
        "asset_delivery",
        "requires_user_asset",
        "auto_download_allowed",
        "source_license_status",
        "open_source_contents",
        "asset_provenance",
        "user_asset_verification",
        "bundled_asset_relative_path",
        "rationale",
    }
    missing = sorted(required_fields - set(value))
    if missing:
        raise LaunchError(
            "recipe distribution_contract is incomplete: missing " + ", ".join(missing)
        )
    if value.get("schema_version") != DISTRIBUTION_SCHEMA_VERSION:
        raise LaunchError(
            "distribution_contract.schema_version must be "
            f"{DISTRIBUTION_SCHEMA_VERSION!r}"
        )
    if value.get("policy_mode") != DISTRIBUTION_POLICY_MODE:
        raise LaunchError(
            f"distribution_contract.policy_mode must be {DISTRIBUTION_POLICY_MODE!r}"
        )
    if value.get("code_distribution") != CODE_DISTRIBUTION:
        raise LaunchError(
            f"distribution_contract.code_distribution must be {CODE_DISTRIBUTION!r}"
        )
    delivery = value.get("asset_delivery")
    if delivery not in ASSET_DELIVERY_MODES:
        raise LaunchError(
            "distribution_contract.asset_delivery must be one of: "
            + ", ".join(sorted(ASSET_DELIVERY_MODES))
        )
    requires_user_asset = value.get("requires_user_asset")
    if not isinstance(requires_user_asset, bool):
        raise LaunchError("distribution_contract.requires_user_asset must be a boolean")
    auto_download_allowed = value.get("auto_download_allowed")
    if auto_download_allowed is not False:
        raise LaunchError(
            "distribution_contract.auto_download_allowed must be false; "
            "asset auto-download is forbidden"
        )
    source_license_status = value.get("source_license_status")
    if source_license_status not in SOURCE_LICENSE_STATUSES:
        raise LaunchError(
            "distribution_contract.source_license_status must be one of: "
            + ", ".join(sorted(SOURCE_LICENSE_STATUSES))
        )
    open_source_contents = _nonempty_string_list(
        value.get("open_source_contents"),
        label="distribution_contract.open_source_contents",
    )
    if set(open_source_contents) != OPEN_SOURCE_CONTENTS:
        raise LaunchError(
            "distribution_contract.open_source_contents must declare exactly: "
            + ", ".join(sorted(OPEN_SOURCE_CONTENTS))
        )
    asset_provenance = _validate_asset_provenance(value.get("asset_provenance"))
    user_asset_verification = _validate_user_asset_verification(
        value.get("user_asset_verification")
    )
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise LaunchError("distribution_contract.rationale must be a non-empty string")
    bundled_value = value.get("bundled_asset_relative_path")
    if bundled_value is not None and (
        not isinstance(bundled_value, str) or not bundled_value.strip()
    ):
        raise LaunchError(
            "distribution_contract.bundled_asset_relative_path must be a non-empty string or null"
        )

    bundled_asset: Path | None = None
    if delivery == "blocked":
        raise LaunchError(
            "recipe distribution contract blocks asset delivery: " + rationale.strip()
        )
    if delivery == "user-supplied":
        if requires_user_asset is not True:
            raise LaunchError(
                "user-supplied asset delivery requires requires_user_asset=true"
            )
        if bundled_value is not None:
            raise LaunchError(
                "user-supplied asset delivery must not declare a bundled asset path"
            )
        if source_license_status not in USER_SUPPLIED_LICENSE_STATUSES:
            raise LaunchError(
                "user-supplied asset delivery requires source_license_status "
                "to be 'verified-open', 'restricted', or 'unknown'"
            )
        if (
            not user_asset_verification["accepted_sha256"]
            and not user_asset_verification["semantic_validation_required"]
        ):
            raise LaunchError(
                "user-supplied asset delivery has no hash or semantic verification gate"
            )
    else:
        if requires_user_asset is not False:
            raise LaunchError(
                "bundled-open asset delivery requires requires_user_asset=false"
            )
        if source_license_status != "verified-open":
            raise LaunchError(
                "bundled-open asset delivery requires source_license_status='verified-open'"
            )
        if asset_provenance.get("evidence_status") != "verified-open":
            raise LaunchError(
                "bundled-open asset delivery requires verified-open provenance evidence"
            )
        if not isinstance(asset_provenance.get("license_id"), str):
            raise LaunchError(
                "bundled-open asset delivery requires an asset provenance license_id"
            )
        if not asset_provenance.get("evidence_refs"):
            raise LaunchError(
                "bundled-open asset delivery requires asset provenance evidence_refs"
            )
        for evidence_ref in asset_provenance["evidence_refs"]:
            if _is_url(evidence_ref):
                raise LaunchError(
                    "bundled-open asset provenance evidence must be package-local"
                )
            evidence_relative = Path(evidence_ref)
            if evidence_relative.is_absolute() or ".." in evidence_relative.parts:
                raise LaunchError(
                    "bundled-open asset provenance evidence must use a safe relative path"
                )
            evidence_path = _resolve_package_path(
                evidence_ref,
                knowledge_root=knowledge_root,
                label="bundled asset provenance evidence",
            )
            if not evidence_path.is_file():
                raise LaunchError(
                    f"bundled asset provenance evidence is not a file: {evidence_path}"
                )
        if not user_asset_verification["accepted_sha256"]:
            raise LaunchError(
                "bundled-open asset delivery requires an accepted asset SHA-256"
            )
        if not isinstance(bundled_value, str):
            raise LaunchError(
                "bundled-open asset delivery requires bundled_asset_relative_path"
            )
        bundled_value = bundled_value.strip()
        bundled_asset = _resolve_bundled_asset(
            bundled_value, knowledge_root=knowledge_root
        )

    normalized = dict(value)
    normalized.update(
        {
            "open_source_contents": open_source_contents,
            "asset_provenance": asset_provenance,
            "user_asset_verification": user_asset_verification,
            "bundled_asset_relative_path": bundled_value,
            "rationale": rationale.strip(),
        }
    )
    return normalized, bundled_asset


def _select_distribution_asset(
    contract: Mapping[str, Any], bundled_asset: Path | None, cli_asset: str | None
) -> dict[str, Any]:
    delivery = str(contract["asset_delivery"])
    if delivery == "user-supplied":
        if not cli_asset:
            raise LaunchError(
                "user-supplied asset delivery requires an explicit local --asset"
            )
        asset = _asset_input(cli_asset)
        origin = "user-provided"
        user_provided = True
    elif delivery == "bundled-open":
        if cli_asset:
            raise LaunchError(
                "bundled-open asset delivery does not allow a --asset override"
            )
        if bundled_asset is None:
            raise LaunchError(
                "bundled-open asset delivery has no validated bundled asset"
            )
        asset = _asset_input(str(bundled_asset))
        origin = "package-bundled-open"
        user_provided = False
    else:
        raise LaunchError("recipe distribution contract blocks asset delivery")

    accepted_hashes = contract["user_asset_verification"]["accepted_sha256"]
    if accepted_hashes and asset["sha256"] not in accepted_hashes:
        raise LaunchError(
            f"{delivery} asset SHA-256 is not accepted by the distribution contract"
        )
    asset["provenance"] = {
        "origin": origin,
        "asset_delivery": delivery,
        "user_provided": user_provided,
        "auto_downloaded": False,
        "source_license_status": contract["source_license_status"],
    }
    return asset


def _validate_entrypoint(
    recipe: Mapping[str, Any], knowledge_root: Path
) -> dict[str, Any]:
    entrypoint = recipe.get("entrypoint")
    if not isinstance(entrypoint, Mapping):
        raise LaunchError("executable recipe entrypoint must be an object")
    kind = str(entrypoint.get("kind") or "").strip()
    if kind not in ENTRYPOINT_KINDS:
        raise LaunchError(f"unsupported recipe entrypoint kind: {kind!r}")
    path_value = str(entrypoint.get("path") or "").strip()
    if not path_value:
        raise LaunchError("recipe entrypoint.path is required")
    path = _resolve_package_path(
        path_value, knowledge_root=knowledge_root, label="entrypoint path"
    )
    if kind == "python" and (not path.is_file() or path.suffix.lower() != ".py"):
        raise LaunchError(f"python entrypoint must be a .py file: {path}")
    normalized: dict[str, Any] = {"kind": kind, "path": str(path)}
    if kind == "python":
        argv = entrypoint.get("argv", [])
        if not isinstance(argv, list) or any(
            not isinstance(item, str) or "\x00" in item for item in argv
        ):
            raise LaunchError("python entrypoint.argv must be a list of safe strings")
        markers = {
            marker for item in argv for marker in re.findall(r"\{[^{}]+\}", item)
        }
        unknown = sorted(markers - ENTRYPOINT_PLACEHOLDERS)
        if unknown:
            raise LaunchError(
                "python entrypoint.argv contains unsupported placeholders: "
                + ", ".join(unknown)
            )
        normalized["argv"] = argv
    else:
        if path.suffix.lower() != ".blend":
            raise LaunchError(
                f"blender_project entrypoint.path must be a .blend file: {path}"
            )
        script_value = str(entrypoint.get("reproduce_script") or "").strip()
        if not script_value:
            raise LaunchError("blender_project entrypoint.reproduce_script is required")
        script = _resolve_package_path(
            script_value, knowledge_root=knowledge_root, label="reproduce script"
        )
        if not script.is_file() or script.suffix.lower() != ".py":
            raise LaunchError(f"reproduce_script must be a .py file: {script}")
        normalized["reproduce_script"] = str(script)
    return normalized


def _video_replay_enabled(recipe: Mapping[str, Any]) -> bool:
    value = recipe.get("video_replay")
    if value is True:
        return True
    if value in (False, None, {}):
        return False
    if not isinstance(value, Mapping):
        raise LaunchError("video_replay must be a boolean or object")
    return value.get("enabled") is True


def _validate_recipe(
    catalog: Mapping[str, Any], recipe_id: str, knowledge_root: Path
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
    list[str],
    set[str],
    bool,
    dict[str, Any],
    Path | None,
]:
    recipes = _recipe_map(catalog)
    if recipe_id not in recipes:
        raise LaunchError(
            f"unknown recipe id {recipe_id!r}; no execution guess was made"
        )
    recipe = recipes[recipe_id]
    required_fields = {
        "id",
        "title",
        "category",
        "coverage_status",
        "enabled",
        "mode",
        "entrypoint",
        "video_replay",
        "inputs",
        "dynamic_contract",
        "evidence_paths",
        "gaps",
        "distribution_contract",
    }
    missing = sorted(field for field in required_fields if field not in recipe)
    if missing:
        raise LaunchError(
            f"recipe {recipe_id!r} is incomplete: missing {', '.join(missing)}"
        )
    distribution, bundled_asset = _validate_distribution_contract(
        recipe, knowledge_root
    )
    if recipe.get("enabled") is not True:
        delivery = str(distribution.get("asset_delivery") or "unknown")
        replay = _video_replay_enabled(recipe)
        gaps = recipe.get("gaps")
        gap_count = len(gaps) if isinstance(gaps, list) else 0
        asset_hint = (
            "supply a lawful local --asset after the recipe is accepted"
            if delivery == "user-supplied"
            else f"asset delivery is {delivery}"
        )
        extraction_hint = (
            "video tutorial extraction is declared"
            if replay
            else "video tutorial extraction is not declared"
        )
        raise LaunchError(
            f"recipe {recipe_id!r} is disabled; {asset_hint}; "
            f"{extraction_hint}; documented gaps: {gap_count}"
        )
    mode = str(recipe.get("mode") or "")
    if mode not in RECIPE_MODES:
        raise LaunchError(f"recipe {recipe_id!r} has unsupported mode {mode!r}")
    gaps = recipe.get("gaps")
    if (
        mode == "documented-gap"
        or (isinstance(gaps, list) and gaps)
        or gaps not in ([], None)
    ):
        raise LaunchError(
            f"recipe {recipe_id!r} has documented knowledge gaps and is not executable"
        )
    coverage = str(recipe.get("coverage_status") or "").strip()
    declared = catalog.get("coverage_status_values")
    if not coverage or not isinstance(declared, list) or coverage not in declared:
        raise LaunchError(
            f"recipe {recipe_id!r} has undeclared coverage_status {coverage!r}"
        )
    blocked_terms = (
        "gap",
        "missing",
        "partial",
        "blocked",
        "unknown",
        "draft",
        "uncovered",
        "not_covered",
        "incomplete",
        "unsupported",
        "unverified",
        "unreviewed",
        "existing-source-only",
    )
    if any(term in coverage.lower() for term in blocked_terms):
        raise LaunchError(
            f"recipe {recipe_id!r} coverage is not executable: {coverage}"
        )
    adapter_contract = recipe.get("adapter_contract")
    if adapter_contract is not None:
        if not isinstance(adapter_contract, Mapping):
            raise LaunchError(
                f"recipe {recipe_id!r} adapter_contract must be an object"
            )
        if adapter_contract.get("visual_equivalence_verified") is not True:
            raise LaunchError(
                f"recipe {recipe_id!r} visual equivalence is not verified"
            )
    _validate_dynamic_contract(recipe)
    evidence_paths = _validate_evidence_paths(recipe, knowledge_root)
    required_inputs = _required_inputs(recipe.get("inputs"))
    if (
        distribution["asset_delivery"] == "user-supplied"
        and "asset" not in required_inputs
    ):
        raise LaunchError(
            "user-supplied distribution contract requires inputs.required to include asset"
        )
    video_replay = _video_replay_enabled(recipe)
    entrypoint = None if video_replay else _validate_entrypoint(recipe, knowledge_root)
    return (
        recipe,
        entrypoint,
        evidence_paths,
        required_inputs,
        video_replay,
        distribution,
        bundled_asset,
    )


def _source_url(recipe: Mapping[str, Any], cli_url: str | None) -> str | None:
    if cli_url:
        return _validate_url(cli_url, "video URL")
    replay = recipe.get("video_replay")
    if isinstance(replay, Mapping) and replay.get("source_url"):
        return _validate_url(str(replay["source_url"]), "recipe video source URL")
    return None


def _check_required_inputs(required: set[str], sources: Mapping[str, Any]) -> None:
    absent = []
    for name in sorted(required):
        value = sources.get(name)
        if value in (None, [], ""):
            absent.append(name)
    if absent:
        raise LaunchError("recipe-required inputs are missing: " + ", ".join(absent))


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    knowledge_arg = Path(args.knowledge_root).expanduser().resolve()
    manifest_path = _find_manifest(knowledge_arg)
    knowledge_root = manifest_path.parent
    catalog = _read_json(manifest_path)
    (
        recipe,
        entrypoint,
        evidence_paths,
        required,
        replay_enabled,
        distribution_contract,
        bundled_asset,
    ) = _validate_recipe(catalog, args.recipe_id, knowledge_root)
    tutorials = [
        _local_input(value, label="tutorial", suffixes=TUTORIAL_SUFFIXES)
        for value in args.tutorial
    ]
    images = [
        _local_input(value, label="image", suffixes=IMAGE_SUFFIXES)
        for value in args.image
    ]
    asset = _select_distribution_asset(distribution_contract, bundled_asset, args.asset)
    video_url = _source_url(recipe, args.video_url)
    sources = {
        "video_url": video_url,
        "tutorial": tutorials,
        "image": images,
        "asset": asset,
    }
    tutorial_mode = str(args.tutorial_mode)
    if tutorials and tutorial_mode != "provided":
        raise LaunchError("--tutorial inputs require --tutorial-mode provided")
    if tutorial_mode == "extract":
        if not replay_enabled:
            raise LaunchError(
                "tutorial extraction is only valid for a video-replay recipe"
            )
        if tutorials:
            raise LaunchError("tutorial extraction cannot be combined with --tutorial")
        if not (video_url and images):
            raise LaunchError(
                "tutorial extraction requires both --video-url and at least one --image"
            )
        if args.tutorial_window_budget is None or args.tutorial_window_budget < 1:
            raise LaunchError(
                "tutorial extraction requires a positive --tutorial-window-budget"
            )
    elif replay_enabled and not tutorials:
        raise LaunchError(
            "video replay requires --tutorial, or explicit --tutorial-mode extract "
            "with --video-url, --image, and --tutorial-window-budget"
        )
    if args.render_tutorial_html and tutorial_mode != "extract":
        raise LaunchError("--render-tutorial-html requires --tutorial-mode extract")
    required_sources = dict(sources)
    if tutorial_mode == "extract":
        required_sources["tutorial"] = {
            "kind": "derived",
            "value": "tutorial.md",
        }
    _check_required_inputs(required, required_sources)
    recipe_slug = (
        re.sub(r"[^a-zA-Z0-9._-]+", "-", args.recipe_id).strip("-._") or "recipe"
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + recipe_slug
    run_dir = (DEFAULT_RUN_ROOT / run_id).resolve()
    selected_recipe = dict(recipe)
    plan: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "created_at": _now(),
        "status": "planned",
        "dry_run": not args.execute,
        "project_root": str(PROJECT_ROOT),
        "run_dir": str(run_dir),
        "video_dir": str(run_dir / "video"),
        "knowledge": {
            "enabled": True,
            "root": str(knowledge_root),
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "catalog_schema": catalog.get("schema"),
            "recipe_id": args.recipe_id,
            "recipe_sha256": _canonical_sha256(selected_recipe),
            "coverage_status": recipe.get("coverage_status"),
            "evidence_paths": evidence_paths,
        },
        "recipe": selected_recipe,
        "distribution_contract": distribution_contract,
        "distribution": {
            "tier": distribution_contract["policy_mode"],
            "code_openness": distribution_contract["code_distribution"],
            "asset_delivery": distribution_contract["asset_delivery"],
            "source_license_status": distribution_contract["source_license_status"],
            "auto_download_allowed": distribution_contract["auto_download_allowed"],
            "asset_provenance": distribution_contract["asset_provenance"],
            "resolved_asset_provenance": {
                **asset["provenance"],
                "sha256": asset["sha256"],
                "size_bytes": asset["size_bytes"],
                "location_scope": "validated-source",
            },
        },
        "sources": sources,
        "tutorial_extraction": {
            "mode": tutorial_mode,
            "model": args.tutorial_model if tutorial_mode == "extract" else None,
            "window_budget": (
                args.tutorial_window_budget if tutorial_mode == "extract" else None
            ),
            "render_human_html": bool(args.render_tutorial_html),
        },
        "route": "video_replay" if replay_enabled else str(entrypoint["kind"]),
        "entrypoint": entrypoint,
    }
    return plan


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _write_run_state(manifest_path: Path, plan: Mapping[str, Any]) -> None:
    _write_json(manifest_path, plan)
    video_dir_value = str(plan.get("video_dir") or "")
    video_dir = Path(video_dir_value) if video_dir_value else None
    if video_dir is not None and video_dir.is_dir():
        _write_json(video_dir / "reproduction_run_manifest.json", plan)


def _download(url: str, destination: Path, *, max_bytes: int) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Video2Blender-Reproduce/1"}
    )
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            destination.open("wb") as target,
        ):
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise LaunchError(f"remote input exceeds {max_bytes} bytes: {url}")
                target.write(chunk)
    except LaunchError:
        raise
    except (OSError, ValueError) as exc:
        raise LaunchError(f"failed to download remote input {url}: {exc}") from exc


def _materialize_refs(
    refs: list[Mapping[str, Any]], destination: Path, prefix: str
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, ref in enumerate(refs, 1):
        if ref["kind"] == "file":
            source = Path(str(ref["value"]))
            suffix = source.suffix.lower()
            target = destination / f"{prefix}_{index:02d}{suffix}"
            shutil.copy2(source, target)
        else:
            parsed = urllib.parse.urlparse(str(ref["value"]))
            suffix = Path(parsed.path).suffix.lower()
            target = destination / f"{prefix}_{index:02d}{suffix}"
            _download(str(ref["value"]), target, max_bytes=64 * 1024 * 1024)
        paths.append(target)
    return paths


def _merge_tutorials(paths: list[Path], destination: Path) -> None:
    sections = []
    for index, path in enumerate(paths, 1):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise LaunchError(f"tutorial is not valid UTF-8 text: {path}") from exc
        sections.append(f"# Tutorial evidence {index}: {path.name}\n\n{text.strip()}\n")
    destination.write_text("\n".join(sections), encoding="utf-8")


def _stage(plan: dict[str, Any]) -> Path:
    run_dir = Path(plan["run_dir"])
    if run_dir.exists():
        raise LaunchError(f"run directory already exists: {run_dir}")
    video_dir = Path(plan["video_dir"])
    video_dir.mkdir(parents=True)
    manifest_path = run_dir / "run_manifest.json"
    tutorials = _materialize_refs(
        plan["sources"]["tutorial"], run_dir / "inputs/tutorials", "tutorial"
    )
    images = _materialize_refs(
        plan["sources"]["image"], run_dir / "inputs/images", "image"
    )
    for source, staged in zip(plan["sources"]["tutorial"], tutorials, strict=True):
        source["staged_value"] = str(staged)
    for source, staged in zip(plan["sources"]["image"], images, strict=True):
        source["staged_value"] = str(staged)
    if tutorials:
        _merge_tutorials(tutorials, video_dir / "tutorial_path_refs.md")
        shutil.copy2(video_dir / "tutorial_path_refs.md", video_dir / "tutorial.md")
    if images:
        shutil.copy2(images[0], video_dir / "target_reference.png")
        shutil.copy2(images[0], video_dir / "final_reference.png")
    asset = plan["sources"].get("asset")
    linked_source: dict[str, Any] | None = None
    if asset:
        source = Path(asset["value"])
        target = run_dir / "inputs" / ("source_asset" + source.suffix.lower())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _sha256(target) != asset["sha256"]:
            raise LaunchError("asset changed while it was being staged")
        asset["value"] = str(target)
        asset["staged_value"] = str(target)
        provenance = asset.get("provenance")
        if not isinstance(provenance, dict):
            raise LaunchError("planned asset has no validated provenance")
        provenance["location_scope"] = "isolated-run"
        resolved_provenance = plan.get("distribution", {}).get(
            "resolved_asset_provenance"
        )
        if not isinstance(resolved_provenance, dict):
            raise LaunchError("planned distribution has no resolved asset provenance")
        resolved_provenance["location_scope"] = "isolated-run"
        resolved_provenance["staged_path"] = str(target)
        linked_source = {
            "selected_model": str(target),
            "selected_model_sha256": asset["sha256"],
        }
    source_info = {
        "schema": "video2blender-reproduction-source.v1",
        "id": plan["run_id"],
        "title": plan["recipe"].get("title") or plan["knowledge"]["recipe_id"],
        "webpage_url": plan["sources"].get("video_url") or "",
        "original_url": plan["sources"].get("video_url") or "",
        "source_kind": "video_replay_type2" if linked_source else "video_replay",
        "workload_kind": "video_replay_type2" if linked_source else "video_replay",
        "linked_source": linked_source,
        "reproduction_knowledge_manifest": str(manifest_path),
        "reproduction_recipe_id": plan["knowledge"]["recipe_id"],
    }
    _write_json(video_dir / "source.info.json", source_info)
    _write_run_state(manifest_path, plan)
    return manifest_path


def _expand_argv(
    argv: list[str], manifest_path: Path, plan: Mapping[str, Any]
) -> list[str]:
    asset = plan["sources"].get("asset") or {}
    image_sources = plan["sources"].get("image") or []
    tutorial_sources = plan["sources"].get("tutorial") or []

    def first_value(values: Any) -> str:
        if not isinstance(values, list) or not values:
            return ""
        first = values[0]
        if not isinstance(first, Mapping):
            return ""
        return str(first.get("staged_value") or first.get("value") or "")

    substitutions = {
        "{run_manifest}": str(manifest_path),
        "{video_dir}": str(plan["video_dir"]),
        "{run_dir}": str(plan["run_dir"]),
        "{output_dir}": str(Path(str(plan["run_dir"])) / "outputs"),
        "{asset}": str(asset.get("staged_value") or asset.get("value") or ""),
        "{image}": first_value(image_sources),
        "{tutorial}": first_value(tutorial_sources),
    }
    expanded = []
    for item in argv:
        for marker, replacement in substitutions.items():
            item = item.replace(marker, replacement)
        expanded.append(item)
    return expanded


def _blender_executable() -> str:
    configured = os.environ.get("BLENDER_PIPELINE_BLENDER", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise LaunchError(
                f"BLENDER_PIPELINE_BLENDER does not point to a file: {path}"
            )
        return str(path)
    discovered = shutil.which("blender")
    if not discovered:
        raise LaunchError(
            "Blender executable is unavailable; set BLENDER_PIPELINE_BLENDER"
        )
    return discovered


def command_for(plan: Mapping[str, Any], manifest_path: Path) -> list[str]:
    route = plan["route"]
    if route == "video_replay":
        command = [
            sys.executable,
            str(VIDEO_REPLAY_MAIN),
            "--video-dir",
            str(plan["video_dir"]),
        ]
        extraction = plan.get("tutorial_extraction") or {}
        if extraction.get("mode") == "extract":
            command.extend(
                [
                    "--force-tutorial",
                    "--max-windows",
                    str(extraction["window_budget"]),
                ]
            )
            if extraction.get("render_human_html") is True:
                command.append("--render-tutorial-html")
        return command
    entrypoint = plan.get("entrypoint") or {}
    if route == "python":
        argv = _expand_argv(list(entrypoint.get("argv") or []), manifest_path, plan)
        if not argv:
            argv = ["--run-manifest", str(manifest_path)]
        return [sys.executable, str(entrypoint["path"]), *argv]
    if route == "blender_project":
        return [
            _blender_executable(),
            "--background",
            str(entrypoint["path"]),
            "--python",
            str(entrypoint["reproduce_script"]),
            "--",
            "--run-manifest",
            str(manifest_path),
        ]
    raise LaunchError(f"unsupported planned route: {route!r}")


def _download_source_video(plan: dict[str, Any]) -> list[str] | None:
    """Materialize source.mp4 only when tutorial extraction actually needs it."""

    extraction = plan.get("tutorial_extraction") or {}
    if plan["route"] != "video_replay" or extraction.get("mode") != "extract":
        return None
    url = str(plan["sources"].get("video_url") or "")
    if not url:
        raise LaunchError("video replay tutorial extraction has no video URL")
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise LaunchError(
            "yt-dlp is required to materialize --video-url for tutorial extraction"
        )
    video_dir = Path(plan["video_dir"])
    output_template = video_dir / "source.%(ext)s"
    command = [
        ytdlp,
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "--output",
        str(output_template),
        url,
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if completed.returncode:
        raise LaunchError(f"yt-dlp failed with return code {completed.returncode}")
    source = video_dir / "source.mp4"
    if not source.is_file():
        candidates = sorted(video_dir.glob("source.*"))
        if len(candidates) != 1:
            raise LaunchError("yt-dlp completed without one unambiguous source video")
        candidates[0].replace(source)
    return command


def _validate_extraction_runtime(plan: Mapping[str, Any]) -> None:
    extraction = plan.get("tutorial_extraction") or {}
    if extraction.get("mode") != "extract":
        return
    endpoint = os.environ.get("BLENDER_PIPELINE_API_ENDPOINT", "").strip()
    if not endpoint:
        raise LaunchError("tutorial extraction requires BLENDER_PIPELINE_API_ENDPOINT")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise LaunchError("BLENDER_PIPELINE_API_ENDPOINT must be an HTTPS URL")
    if parsed.username or parsed.password:
        raise LaunchError("BLENDER_PIPELINE_API_ENDPOINT must not embed credentials")
    secret_value = os.environ.get("BLENDER_PIPELINE_API_KEY_FILE", "").strip()
    if not secret_value:
        raise LaunchError("tutorial extraction requires BLENDER_PIPELINE_API_KEY_FILE")
    secret_path = Path(secret_value).expanduser().resolve()
    if not secret_path.is_file():
        raise LaunchError(
            "BLENDER_PIPELINE_API_KEY_FILE must point to an owner-only regular file"
        )
    if secret_path.stat().st_mode & 0o077:
        raise LaunchError("BLENDER_PIPELINE_API_KEY_FILE must be owner-only")


def execute(plan: dict[str, Any]) -> int:
    _validate_extraction_runtime(plan)
    manifest_path = _stage(plan)
    plan["status"] = "running"
    plan["started_at"] = _now()
    _write_run_state(manifest_path, plan)
    try:
        preparation_command = _download_source_video(plan)
        command = command_for(plan, manifest_path)
        if preparation_command:
            plan["preparation_command"] = preparation_command
        plan["command"] = command
        _write_run_state(manifest_path, plan)
        env = os.environ.copy()
        env.update(
            {
                "VIDEO2BLENDER_REPRODUCTION_KNOWLEDGE_ENABLED": "1",
                "VIDEO2BLENDER_REPRODUCTION_KNOWLEDGE_ROOT": plan["knowledge"]["root"],
                "VIDEO2BLENDER_REPRODUCTION_MANIFEST": str(manifest_path),
                "VIDEO2BLENDER_REPRODUCTION_RECIPE_ID": plan["knowledge"]["recipe_id"],
            }
        )
        extraction = plan.get("tutorial_extraction") or {}
        if extraction.get("mode") == "extract":
            env["BLENDER_PIPELINE_MODEL"] = str(extraction["model"])
        completed = subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=False)
        plan["returncode"] = completed.returncode
        plan["status"] = "completed" if completed.returncode == 0 else "failed"
        return completed.returncode
    except Exception as exc:
        plan["status"] = "failed"
        plan["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        plan["finished_at"] = _now()
        _write_run_state(manifest_path, plan)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch an explicitly selected, knowledge-backed Video2Blender reproduction recipe."
    )
    parser.add_argument("--video-url")
    parser.add_argument(
        "--tutorial",
        action="append",
        default=[],
        help="Tutorial path or http(s) URL; repeatable.",
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Reference image path or http(s) URL; repeatable.",
    )
    parser.add_argument("--asset", help="Optional local source asset/project.")
    parser.add_argument("--target", "--recipe-id", dest="recipe_id", required=True)
    parser.add_argument("--knowledge-root", type=Path, default=DEFAULT_KNOWLEDGE_ROOT)
    parser.add_argument(
        "--tutorial-mode",
        choices=("provided", "extract"),
        default="provided",
        help=(
            "Use supplied tutorial files, or explicitly authorize the rich "
            "video-to-tutorial preparation stage."
        ),
    )
    parser.add_argument(
        "--tutorial-model",
        choices=("gpt-5.6-sol", "gpt-5.5"),
        default="gpt-5.6-sol",
        help="Model for extraction; gpt-5.5 is an explicit fallback only.",
    )
    parser.add_argument(
        "--tutorial-window-budget",
        type=int,
        help="Maximum complete 60-second evidence windows for extraction.",
    )
    parser.add_argument(
        "--render-tutorial-html",
        action="store_true",
        help=(
            "Also render a human-facing HTML view from the same tutorial steps; "
            "tutorial.md remains the operational source of truth."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the manifest without writing or executing (default).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Stage inputs and execute the selected recipe.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        plan = build_plan(args)
        if not args.execute:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        return execute(plan)
    except LaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
