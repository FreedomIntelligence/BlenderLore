"""Allowlisted edit operator dispatch."""

from __future__ import annotations

from typing import Any

from ..contracts import EditKind, EditRequest
from .geometry import apply_geometry, apply_modeling
from .material import apply_color, apply_material
from .scene import apply_scene


def apply_edit(request: EditRequest, bpy_module: Any | None = None) -> dict[str, Any]:
    dispatch = {
        EditKind.MATERIAL: apply_material,
        EditKind.COLOR: apply_color,
        EditKind.GEOMETRY: apply_geometry,
        EditKind.MODELING: apply_modeling,
        EditKind.SCENE: apply_scene,
    }
    return dispatch[request.kind](request, bpy_module)


__all__ = ["apply_edit"]
