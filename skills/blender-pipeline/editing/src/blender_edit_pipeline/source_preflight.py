"""Blender source binding, target checks, and dependency inventory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import EditKind, EditRequest, sha256_file
from .operators.geometry import (
    geometry_target_unsupported_reason,
    validate_generation_handoff,
)
from .qa import snapshot_scene


class PreflightError(RuntimeError):
    """Raised when an existing Blender source is unsafe to edit."""


def _bpy() -> Any:
    try:
        import bpy  # type: ignore[import-not-found]

        return bpy
    except ImportError as exc:
        raise RuntimeError("source preflight must run inside Blender") from exc


def _external_dependencies(bpy: Any) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    records = []
    records.extend(
        ("image", item.name, item.filepath)
        for item in bpy.data.images
        if item.source == "FILE"
        and item.filepath
        and getattr(item, "packed_file", None) is None
    )
    records.extend(
        ("library", item.name, item.filepath)
        for item in bpy.data.libraries
        if item.filepath and getattr(item, "packed_file", None) is None
    )
    records.extend(
        ("font", item.name, item.filepath)
        for item in bpy.data.fonts
        if item.filepath
        and not item.is_builtin
        and getattr(item, "packed_file", None) is None
    )
    records.extend(
        ("movieclip", item.name, item.filepath)
        for item in bpy.data.movieclips
        if item.filepath and getattr(item, "packed_file", None) is None
    )
    for kind, name, raw_path in records:
        resolved = Path(bpy.path.abspath(raw_path)).resolve()
        dependencies.append(
            {
                "kind": kind,
                "name": name,
                "reference": Path(raw_path).name,
                "exists": resolved.is_file(),
            }
        )
    return sorted(dependencies, key=lambda item: (item["kind"], item["name"]))


def _ensure_local(edit_id: Any, label: str) -> None:
    if edit_id is not None and getattr(edit_id, "library", None) is not None:
        raise PreflightError(f"{label} is linked read-only data")


def audit_current_scene(
    request: EditRequest, bpy_module: Any | None = None
) -> dict[str, Any]:
    bpy = bpy_module or _bpy()
    current = Path(bpy.data.filepath).resolve()
    requested = Path(request.source_blend).expanduser().resolve()
    if not current.is_file() or current != requested:
        raise PreflightError(
            "the open Blender file does not match request.source_blend"
        )
    if current.suffix.lower() != ".blend":
        raise PreflightError("source file must have a .blend extension")
    if sha256_file(current) != request.source_sha256:
        raise PreflightError("source SHA-256 does not match the request")

    generation_handoffs: list[dict[str, Any]] = []
    replacements = request.parameters.get("replacements", {})
    for name in request.targets.objects:
        obj = bpy.data.objects.get(name)
        if obj is None:
            raise PreflightError(f"target object not found: {name}")
        _ensure_local(obj, f"object {name}")
        if obj.users == 0 and not obj.use_fake_user:
            raise PreflightError(f"target object is an unsaved orphan: {name}")
        if request.kind == EditKind.GEOMETRY and obj.type != "MESH":
            raise PreflightError(f"geometry target is not a mesh: {name}")
        if request.kind == EditKind.GEOMETRY and obj.data.shape_keys is not None:
            raise PreflightError(f"geometry target has unsupported shape keys: {name}")
        if request.kind == EditKind.GEOMETRY and len(obj.data.color_attributes) > 0:
            raise PreflightError(
                f"geometry target has unsupported color attributes: {name}"
            )
        if request.kind == EditKind.GEOMETRY:
            if unsupported := geometry_target_unsupported_reason(obj):
                raise PreflightError(
                    f"geometry target {name} cannot receive single-frame visual attestation: {unsupported}"
                )
        spec = replacements.get(name) if isinstance(replacements, dict) else None
        if (
            request.kind == EditKind.GEOMETRY
            and isinstance(spec, dict)
            and "attempt_receipt" in spec
        ):
            try:
                handoff = validate_generation_handoff(spec, name)
                with bpy.data.libraries.load(
                    str(handoff["asset_path"]), link=False
                ) as (available, loaded):
                    if handoff["object"] not in available.objects:
                        raise PreflightError(
                            f"generated asset object not found: {handoff['object']}"
                        )
                    loaded.objects = []
            except PreflightError:
                raise
            except Exception as exc:
                raise PreflightError(str(exc)) from exc
            generation_handoffs.append(
                {
                    key: value
                    for key, value in handoff.items()
                    if key not in {"receipt_path", "asset_path"}
                }
            )
    for name in request.targets.materials:
        material = bpy.data.materials.get(name)
        if material is None:
            raise PreflightError(f"target material not found: {name}")
        if material.users == 0 and not material.use_fake_user:
            raise PreflightError(f"target material is an unsaved orphan: {name}")
        _ensure_local(material, f"material {name}")
    for name in request.targets.lights:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "LIGHT":
            raise PreflightError(f"target light not found: {name}")
        _ensure_local(obj, f"light {name}")
    for name in request.targets.camera:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "CAMERA":
            raise PreflightError(f"target camera not found: {name}")
        _ensure_local(obj, f"camera {name}")
    for name in request.targets.generated_objects:
        if bpy.data.objects.get(name) is not None:
            raise PreflightError(f"generated object name already exists: {name}")
    dependencies = _external_dependencies(bpy)
    missing = [item for item in dependencies if not item["exists"]]
    if missing:
        names = ", ".join(f"{item['kind']}:{item['name']}" for item in missing)
        raise PreflightError(f"missing external dependencies: {names}")
    snapshot = snapshot_scene(bpy)
    return {
        "schema": "blender-source-map/v1",
        "source": {"filename": current.name, "sha256": request.source_sha256},
        "targets": request.targets.as_dict(),
        "generation_handoffs": generation_handoffs,
        "dependencies": dependencies,
        "snapshot_digest": snapshot["digest"],
        "inventory": {
            "objects": sorted(snapshot["objects"]),
            "materials": sorted(snapshot["materials"]),
            "collections": sorted(snapshot["collections"]),
        },
    }
