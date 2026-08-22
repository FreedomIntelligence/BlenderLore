"""Conservative camera bounds for generated RW1 interchange previews."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


FRAMING_GENERATION = "dominant_visible_geometry_v1"
STATIC_INTERCHANGE_SAFE_MARGIN = 1.16
LEGACY_STUDIO_GROUND_RADIUS_MULTIPLIER = 6.0
STATIC_INTERCHANGE_STUDIO_GROUND_RADIUS_MULTIPLIER = 24.0
STATIC_INTERCHANGE_STUDIO_GROUND_ORTHO_MULTIPLIER = 12.0


def _vector3(value: object, *, label: str) -> tuple[float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a length-three sequence")
    if len(value) != 3:
        raise ValueError(f"{label} must be a length-three sequence")
    result = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{label} must contain finite numbers")
    return result  # type: ignore[return-value]


def _size(
    minimum: Sequence[float], maximum: Sequence[float]
) -> tuple[float, float, float]:
    return tuple(
        max(0.0, float(maximum[index]) - float(minimum[index])) for index in range(3)
    )  # type: ignore[return-value]


def _length(value: Sequence[float]) -> float:
    return math.sqrt(sum(float(component) ** 2 for component in value))


def _normalize(value: Sequence[float]) -> tuple[float, float, float]:
    length = _length(value)
    if length <= 1e-12:
        raise ValueError("direction must be non-zero")
    return tuple(float(component) / length for component in value)  # type: ignore[return-value]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(float(left[index]) * float(right[index]) for index in range(3))


def _cross(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float]:
    return (
        float(left[1]) * float(right[2]) - float(left[2]) * float(right[1]),
        float(left[2]) * float(right[0]) - float(left[0]) * float(right[2]),
        float(left[0]) * float(right[1]) - float(left[1]) * float(right[0]),
    )


def _bounds(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not rows:
        raise ValueError("at least one framing component is required")
    minimum = tuple(
        min(float(row["minimum"][axis]) for row in rows) for axis in range(3)
    )
    maximum = tuple(
        max(float(row["maximum"][axis]) for row in rows) for axis in range(3)
    )
    return minimum, maximum  # type: ignore[return-value]


def _normalized_record(record: Mapping[str, Any]) -> dict[str, Any]:
    name = str(record.get("name") or "").strip()
    if not name:
        raise ValueError("framing component name is required")
    minimum = _vector3(record.get("minimum"), label=f"{name}.minimum")
    maximum = _vector3(record.get("maximum"), label=f"{name}.maximum")
    if any(minimum[index] > maximum[index] for index in range(3)):
        raise ValueError(f"{name} has inverted bounds")
    size = _size(minimum, maximum)
    diagonal = max(_length(size), 1e-9)
    flatness = min(size) / diagonal
    face_count = max(0, int(record.get("face_count") or 0))
    vertex_count = max(0, int(record.get("vertex_count") or 0))
    return {
        "name": name,
        "minimum": minimum,
        "maximum": maximum,
        "center": tuple((minimum[index] + maximum[index]) * 0.5 for index in range(3)),
        "size": size,
        "diagonal": diagonal,
        "flatness": flatness,
        "support": max(face_count, vertex_count, 1),
        "face_count": face_count,
        "vertex_count": vertex_count,
        "assembly_key": str(record.get("assembly_key") or "").strip(),
        "explicit_helper": bool(record.get("explicit_helper", False)),
        "named_backdrop": bool(record.get("named_backdrop", False)),
    }


def _aabb_gap(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    squared = 0.0
    for axis in range(3):
        gap = max(
            float(left["minimum"][axis]) - float(right["maximum"][axis]),
            float(right["minimum"][axis]) - float(left["maximum"][axis]),
            0.0,
        )
        squared += gap * gap
    return math.sqrt(squared)


def _connected_groups(
    rows: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    if not rows:
        return []
    diagonals = sorted(float(row["diagonal"]) for row in rows)
    median_diagonal = diagonals[len(diagonals) // 2]
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left in enumerate(rows):
        for right_index in range(left_index + 1, len(rows)):
            right = rows[right_index]
            shared_assembly = bool(left["assembly_key"]) and (
                left["assembly_key"] == right["assembly_key"]
            )
            proximity = (
                _aabb_gap(left, right)
                <= min(
                    float(left["diagonal"]),
                    float(right["diagonal"]),
                )
                * 0.75
                + median_diagonal * 0.05
            )
            if shared_assembly or proximity:
                union(left_index, right_index)

    groups: dict[int, list[Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(find(index), []).append(row)
    return list(groups.values())


def select_visible_geometry(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Select only an unambiguous main assembly; otherwise retain the scene.

    The selector is deliberately asymmetric: broad flat backdrops and named
    helpers can be excluded from camera bounds, but real render geometry is
    cropped only when one spatial component has overwhelming mesh support and
    every omitted component is both tiny and insignificant. Comparable or
    scene-like components fail closed to complete non-backdrop bounds.
    """

    rows = [_normalized_record(record) for record in records]
    if not rows:
        raise ValueError("at least one framing component is required")
    if len({str(row["name"]) for row in rows}) != len(rows):
        raise ValueError("framing component names must be unique")

    named_excluded = [
        row for row in rows if row["explicit_helper"] or row["named_backdrop"]
    ]
    candidates = [row for row in rows if row not in named_excluded]
    nonflat = [row for row in candidates if float(row["flatness"]) > 0.0125]
    geometric_backdrops: list[Mapping[str, Any]] = []
    if nonflat:
        nonflat_diagonals = sorted(float(row["diagonal"]) for row in nonflat)
        median_nonflat = nonflat_diagonals[len(nonflat_diagonals) // 2]
        maximum_nonflat = nonflat_diagonals[-1]
        geometric_backdrops = [
            row
            for row in candidates
            if float(row["flatness"]) <= 0.0125
            and float(row["diagonal"])
            >= max(median_nonflat * 2.5, maximum_nonflat * 1.35)
        ]
    candidates = [row for row in candidates if row not in geometric_backdrops]
    backdrop_rows = [*named_excluded, *geometric_backdrops]

    if not candidates:
        candidates = list(rows)
        backdrop_rows = []
        selection_mode = "no_safe_subject_fail_closed"
    elif len(candidates) == 1:
        selection_mode = "single_visible_component"
    else:
        groups = _connected_groups(candidates)
        total_support = sum(int(row["support"]) for row in candidates)
        candidate_minimum, candidate_maximum = _bounds(candidates)
        candidate_diagonal = max(
            _length(_size(candidate_minimum, candidate_maximum)),
            1e-9,
        )
        ranked = sorted(
            groups,
            key=lambda group: (
                sum(int(row["support"]) for row in group),
                _length(_size(*_bounds(group))),
                len(group),
            ),
            reverse=True,
        )
        group_rows = []
        for group in ranked:
            group_minimum, group_maximum = _bounds(group)
            group_rows.append(
                {
                    "rows": group,
                    "support": sum(int(row["support"]) for row in group),
                    "diagonal": _length(_size(group_minimum, group_maximum)),
                }
            )
        dominant = group_rows[0]
        dominant_ratio = float(dominant["support"]) / max(total_support, 1)
        significant = [
            group
            for group in group_rows
            if (
                float(group["support"]) / max(total_support, 1) >= 0.10
                or float(group["diagonal"]) / candidate_diagonal >= 0.25
            )
        ]
        omitted = [group for group in group_rows if group is not dominant]
        omitted_support = sum(int(group["support"]) for group in omitted)
        omitted_diagonal = max(
            (float(group["diagonal"]) for group in omitted),
            default=0.0,
        )
        safely_tiny_omissions = (
            omitted_support / max(total_support, 1) <= 0.05
            and omitted_diagonal <= max(float(dominant["diagonal"]), 1e-9) * 0.20
        )
        if len(groups) == 1:
            # A single spatial/parent assembly is already the conservative
            # answer.  Do not describe it as a cropped dominant component
            # when no geometry was omitted.
            selection_mode = "connected_multi_component_assembly"
        elif dominant_ratio >= 0.82 and len(significant) == 1 and safely_tiny_omissions:
            candidates = list(dominant["rows"])
            selection_mode = "dominant_connected_component"
        else:
            selection_mode = "multiple_significant_components_fail_closed"

    minimum, maximum = _bounds(candidates)
    selected_names = sorted(str(row["name"]) for row in candidates)
    excluded_names = sorted(
        str(row["name"]) for row in rows if str(row["name"]) not in set(selected_names)
    )
    return {
        "generation": FRAMING_GENERATION,
        "selection_mode": selection_mode,
        "minimum": list(minimum),
        "maximum": list(maximum),
        "selected_names": selected_names,
        "excluded_names": excluded_names,
        "backdrop_names": sorted(str(row["name"]) for row in backdrop_rows),
        "input_component_count": len(rows),
        "selected_component_count": len(candidates),
        "fail_closed": selection_mode.endswith("fail_closed"),
    }


def projected_bounds_spans(
    minimum: Sequence[float],
    maximum: Sequence[float],
    direction: Sequence[float],
) -> tuple[float, float]:
    """Return camera-right and camera-up spans for an axis-aligned box."""

    minimum3 = _vector3(minimum, label="minimum")
    maximum3 = _vector3(maximum, label="maximum")
    outward = _normalize(_vector3(direction, label="direction"))
    reference_up = (0.0, 0.0, 1.0)
    if abs(_dot(outward, reference_up)) > 0.98:
        reference_up = (0.0, 1.0, 0.0)
    right = _normalize(_cross(reference_up, outward))
    camera_up = _normalize(_cross(outward, right))
    corners = [
        (
            maximum3[0] if x else minimum3[0],
            maximum3[1] if y else minimum3[1],
            maximum3[2] if z else minimum3[2],
        )
        for x in (False, True)
        for y in (False, True)
        for z in (False, True)
    ]
    horizontal = [_dot(corner, right) for corner in corners]
    vertical = [_dot(corner, camera_up) for corner in corners]
    return max(horizontal) - min(horizontal), max(vertical) - min(vertical)


def orthographic_camera_scale(
    minimum: Sequence[float],
    maximum: Sequence[float],
    direction: Sequence[float],
    *,
    aspect_ratio: float = 1.0,
    safe_margin: float = STATIC_INTERCHANGE_SAFE_MARGIN,
) -> float:
    if not math.isfinite(aspect_ratio) or aspect_ratio <= 0.0:
        raise ValueError("aspect_ratio must be positive and finite")
    if not math.isfinite(safe_margin) or safe_margin < 1.0:
        raise ValueError("safe_margin must be finite and at least one")
    horizontal, vertical = projected_bounds_spans(
        minimum,
        maximum,
        direction,
    )
    required = max(vertical, horizontal / aspect_ratio)
    if required <= 1e-9:
        return 2.0
    return required * safe_margin


def build_camera_plan(
    records: Sequence[Mapping[str, Any]],
    *,
    legacy_minimum: Sequence[float],
    legacy_maximum: Sequence[float],
    legacy_ortho_scale: float,
    direction: Sequence[float],
    enabled: bool,
) -> dict[str, Any]:
    """Build a v3-only plan while making the legacy no-op explicit."""

    legacy_minimum3 = _vector3(legacy_minimum, label="legacy_minimum")
    legacy_maximum3 = _vector3(legacy_maximum, label="legacy_maximum")
    if not enabled:
        return {
            "generation": FRAMING_GENERATION,
            "applied": False,
            "selection_mode": "legacy_path_unchanged",
            "minimum": list(legacy_minimum3),
            "maximum": list(legacy_maximum3),
            "ortho_scale": float(legacy_ortho_scale),
            "safe_margin": None,
            "selected_names": [],
            "excluded_names": [],
            "backdrop_names": [],
        }
    selection = select_visible_geometry(records)
    minimum = _vector3(selection["minimum"], label="selected_minimum")
    maximum = _vector3(selection["maximum"], label="selected_maximum")
    horizontal, vertical = projected_bounds_spans(
        minimum,
        maximum,
        direction,
    )
    result = dict(selection)
    result.update(
        {
            "applied": True,
            "ortho_scale": orthographic_camera_scale(
                minimum,
                maximum,
                direction,
            ),
            "safe_margin": STATIC_INTERCHANGE_SAFE_MARGIN,
            "projected_horizontal_span": horizontal,
            "projected_vertical_span": vertical,
        }
    )
    return result


def studio_ground_size(
    radius: float,
    ortho_scale: float,
    *,
    static_interchange: bool,
) -> float:
    """Keep legacy ground sizing exact outside the static interchange path."""

    radius = max(0.0, float(radius))
    ortho_scale = max(0.0, float(ortho_scale))
    if not static_interchange:
        return max(radius * LEGACY_STUDIO_GROUND_RADIUS_MULTIPLIER, 4.0)
    return max(
        radius * STATIC_INTERCHANGE_STUDIO_GROUND_RADIUS_MULTIPLIER,
        ortho_scale * STATIC_INTERCHANGE_STUDIO_GROUND_ORTHO_MULTIPLIER,
        4.0,
    )
