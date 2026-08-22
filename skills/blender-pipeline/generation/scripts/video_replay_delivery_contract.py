from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from PIL import Image

CANONICAL_SIX_VIEW_NAMES = (
    "iso",
    "front",
    "back",
    "left",
    "right",
    "top",
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DELIVERY_RECEIPT_NAME = "delivery_validation_receipt.json"
DELIVERY_RECEIPT_SCHEMA = "video-replay-delivery-validation.v1"
MIN_VIEW_DIMENSION = 16
MIN_BLEND_BYTES = 1024
MIN_MP4_BYTES = 32


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_artifact_binding(path: Path) -> dict[str, Any] | None:
    """Return a content binding only for a fully decodable, useful PNG."""

    if not path.is_file() or path.is_symlink():
        return None
    try:
        size_bytes = path.stat().st_size
        if size_bytes <= len(PNG_SIGNATURE):
            return None
        with path.open("rb") as handle:
            if handle.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
                return None
        with Image.open(path) as image:
            if image.format != "PNG":
                return None
            width, height = image.size
            image.verify()
        if width < MIN_VIEW_DIMENSION or height < MIN_VIEW_DIMENSION:
            return None
        return {
            "size_bytes": size_bytes,
            "sha256": sha256_file(path),
            "width": width,
            "height": height,
            "format": "PNG",
        }
    except (OSError, ValueError, SyntaxError):
        return None


def is_png_artifact(path: Path) -> bool:
    """Return whether *path* is a complete, decodable PNG delivery artifact."""

    return png_artifact_binding(path) is not None


def missing_canonical_six_views(delivery_root: Path) -> tuple[str, ...]:
    """List missing or invalid canonical views below ``delivery_root``."""

    view_dir = delivery_root / "six_views"
    return tuple(
        name
        for name in CANONICAL_SIX_VIEW_NAMES
        if not is_png_artifact(view_dir / f"{name}.png")
    )


def complete_six_view_delivery(delivery_root: Path) -> bool:
    """Require all six named, fully decodable PNG views for publication."""

    return not missing_canonical_six_views(delivery_root)


def _regular_file_binding(
    path: Path,
    *,
    minimum_bytes: int,
) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        size_bytes = path.stat().st_size
        if size_bytes < minimum_bytes:
            return None
        return {"size_bytes": size_bytes, "sha256": sha256_file(path)}
    except OSError:
        return None


def _blend_binding(path: Path) -> dict[str, Any] | None:
    binding = _regular_file_binding(path, minimum_bytes=MIN_BLEND_BYTES)
    if binding is None:
        return None
    try:
        with path.open("rb") as handle:
            header = handle.read(7)
            if not header.startswith((b"BLENDER", b"\x1f\x8b", b"\x28\xb5\x2f\xfd")):
                return None
    except OSError:
        return None
    return binding


def _mp4_binding(path: Path) -> dict[str, Any] | None:
    binding = _regular_file_binding(path, minimum_bytes=MIN_MP4_BYTES)
    if binding is None:
        return None
    try:
        with path.open("rb") as handle:
            if b"ftyp" not in handle.read(32):
                return None
    except OSError:
        return None
    return binding


def _receipt_self_hash(receipt: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_delivery_validation_receipt(
    delivery_root: Path,
    *,
    effective_route: str,
    fresh_reopen: dict[str, Any],
) -> Path:
    """Atomically bind a freshly reopened asset to its reviewed delivery files."""

    if effective_route not in {"static", "dynamic"}:
        raise ValueError(f"invalid effective route: {effective_route!r}")
    asset = _blend_binding(delivery_root / "asset.blend")
    if asset is None:
        raise ValueError("asset.blend is not a valid native Blender file")
    if (
        fresh_reopen.get("valid") is not True
        or fresh_reopen.get("asset_sha256") != asset["sha256"]
        or int(fresh_reopen.get("renderable_object_count", 0)) < 1
    ):
        raise ValueError("asset.blend did not pass an independent fresh reopen")

    views: dict[str, dict[str, Any]] = {}
    for name in CANONICAL_SIX_VIEW_NAMES:
        binding = png_artifact_binding(delivery_root / "six_views" / f"{name}.png")
        if binding is None:
            raise ValueError(f"canonical view is missing or invalid: {name}.png")
        views[name] = binding

    artifacts: dict[str, Any] = {"asset.blend": asset, "six_views": views}
    if effective_route == "dynamic":
        final_effect = _mp4_binding(delivery_root / "final_effect.mp4")
        if final_effect is None:
            raise ValueError("dynamic final_effect.mp4 is missing or invalid")
        artifacts["final_effect.mp4"] = final_effect

    receipt: dict[str, Any] = {
        "schema": DELIVERY_RECEIPT_SCHEMA,
        "effective_route": effective_route,
        "fresh_reopen": fresh_reopen,
        "artifacts": artifacts,
    }
    receipt["receipt_sha256"] = _receipt_self_hash(receipt)
    output = delivery_root / DELIVERY_RECEIPT_NAME
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return output


def delivery_validation_receipt_is_current(
    delivery_root: Path,
    *,
    effective_route: str,
) -> bool:
    """Verify the receipt and every artifact binding before an existing-output skip."""

    receipt_path = delivery_root / DELIVERY_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return False
        if receipt.get("schema") != DELIVERY_RECEIPT_SCHEMA:
            return False
        if receipt.get("effective_route") != effective_route:
            return False
        if receipt.get("receipt_sha256") != _receipt_self_hash(receipt):
            return False
        artifacts = receipt.get("artifacts")
        if not isinstance(artifacts, dict):
            return False

        asset = _blend_binding(delivery_root / "asset.blend")
        if asset is None or artifacts.get("asset.blend") != asset:
            return False
        fresh_reopen = receipt.get("fresh_reopen")
        if not isinstance(fresh_reopen, dict):
            return False
        if (
            fresh_reopen.get("valid") is not True
            or fresh_reopen.get("asset_sha256") != asset["sha256"]
            or int(fresh_reopen.get("renderable_object_count", 0)) < 1
        ):
            return False

        recorded_views = artifacts.get("six_views")
        if not isinstance(recorded_views, dict):
            return False
        for name in CANONICAL_SIX_VIEW_NAMES:
            current = png_artifact_binding(delivery_root / "six_views" / f"{name}.png")
            if current is None or recorded_views.get(name) != current:
                return False

        if effective_route == "dynamic":
            final_effect = _mp4_binding(delivery_root / "final_effect.mp4")
            if (
                final_effect is None
                or artifacts.get("final_effect.mp4") != final_effect
            ):
                return False
        elif "final_effect.mp4" in artifacts:
            return False
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
