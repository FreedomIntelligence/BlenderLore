"""Bounded mesh replacement and declarative modeling operators."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from ..contracts import ContractError, EditRequest, sha256_file


Vector3 = tuple[float, float, float]

_GENERATION_ATTEMPT_SCHEMA = "video2blender.model-direct-generation-attempt.v3"
_GENERATION_ACCEPTANCE_SCOPE = "web_delivery_joint_at_80_candidate"
_GENERATION_HARD_GATES = {
    "execution_success",
    "asset_saved",
    "fresh_reopen",
    "evidence_complete",
    "no_external_dependencies",
}
_DIGEST_CHARS = frozenset("0123456789abcdef")


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _digest_field(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise ContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _regular_file(value: Any, label: str, suffix: str | None = None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty path")
    requested = Path(value).expanduser()
    if requested.is_symlink():
        raise ContractError(f"{label} must not be a symlink")
    try:
        path = requested.resolve(strict=True)
    except OSError as exc:
        raise ContractError(f"{label} is missing or unreadable") from exc
    if not path.is_file() or (suffix is not None and path.suffix.lower() != suffix):
        raise ContractError(f"{label} must be a regular {suffix or ''} file".strip())
    return path


def validate_generation_handoff(spec: Any, target_name: str) -> dict[str, Any]:
    """Verify an accepted GEN-V3 attempt and its exact asset binding."""
    if not isinstance(spec, Mapping):
        raise ContractError(f"replacement for {target_name} must be an object")
    required = {
        "attempt_receipt",
        "attempt_receipt_sha256",
        "asset_blend",
        "asset_blend_sha256",
        "object",
    }
    allowed = required | {
        "fit_to_source_bbox",
        "envelope_tolerance",
        "volume_ratio_tolerance",
        "world_center_tolerance",
    }
    if missing := required - set(spec):
        raise ContractError(
            f"generation handoff for {target_name} is missing {sorted(missing)}"
        )
    if unknown := set(spec) - allowed:
        raise ContractError(
            f"generation handoff for {target_name} has unknown fields {sorted(unknown)}"
        )
    object_name = spec.get("object")
    if not isinstance(object_name, str) or not object_name.strip():
        raise ContractError("generation handoff object must be a non-empty string")
    receipt_path = _regular_file(
        spec.get("attempt_receipt"), "generation attempt receipt", ".json"
    )
    receipt_file_sha = _digest_field(
        spec.get("attempt_receipt_sha256"), "attempt_receipt_sha256"
    )
    if sha256_file(receipt_path) != receipt_file_sha:
        raise ContractError("generation attempt receipt file SHA-256 drifted")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("generation attempt receipt is not valid JSON") from exc
    if not isinstance(receipt, Mapping):
        raise ContractError("generation attempt receipt must be a JSON object")
    self_hash = _digest_field(
        receipt.get("receipt_sha256"), "generation receipt self-hash"
    )
    body = dict(receipt)
    body.pop("receipt_sha256", None)
    if _digest(body) != self_hash:
        raise ContractError("generation attempt receipt self-hash drifted")
    acceptance = receipt.get("acceptance")
    hard_gates = receipt.get("hard_gates")
    score_source = acceptance if isinstance(acceptance, Mapping) else {}
    try:
        rubric_score = float(score_source.get("rubric_score") or 0.0)
        vlm_score = float(score_source.get("vlm_score") or 0.0)
        attempt = int(receipt.get("attempt") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError("generation attempt scores are invalid") from exc
    if not math.isfinite(rubric_score) or not math.isfinite(vlm_score):
        raise ContractError("generation attempt scores must be finite")
    if (
        receipt.get("schema") != _GENERATION_ATTEMPT_SCHEMA
        or receipt.get("acceptance_scope") != _GENERATION_ACCEPTANCE_SCOPE
        or receipt.get("execution_is_formal") is not False
        or receipt.get("sandbox_attested") is not False
        or receipt.get("status") != "web_candidate_accepted"
        or not isinstance(acceptance, Mapping)
        or acceptance.get("acceptance_scope") != _GENERATION_ACCEPTANCE_SCOPE
        or acceptance.get("joint_at_threshold") is not True
        or not isinstance(receipt.get("task_id"), str)
        or not receipt["task_id"].strip()
        or type(receipt.get("attempt")) is not int
        or attempt < 1
        or rubric_score < 80.0
        or vlm_score < 80.0
        or acceptance.get("hard_gates_passed") is not True
        or acceptance.get("critical_criteria_passed") is not True
        or acceptance.get("judge_protocol_valid") is not True
        or not isinstance(hard_gates, Mapping)
        or set(hard_gates) != _GENERATION_HARD_GATES
        or any(value is not True for value in hard_gates.values())
    ):
        raise ContractError(
            "generation handoff requires a Joint@80 accepted attempt receipt"
        )
    artifact_inventory = receipt.get("artifacts")
    artifact = (
        artifact_inventory.get("asset")
        if isinstance(artifact_inventory, Mapping)
        else None
    )
    if not isinstance(artifact, Mapping):
        raise ContractError("generation attempt receipt lacks an asset.blend binding")
    relative = PurePosixPath(str(artifact.get("path") or ""))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ContractError("generation asset binding is not a confined relative path")
    try:
        receipt_asset = (receipt_path.parent / Path(*relative.parts)).resolve(
            strict=True
        )
        receipt_asset.relative_to(receipt_path.parent.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ContractError("generation asset binding escapes or is missing") from exc
    asset_path = _regular_file(spec.get("asset_blend"), "generated asset", ".blend")
    if asset_path != receipt_asset:
        raise ContractError(
            "asset_blend is not the asset bound by the generation receipt"
        )
    asset_sha = _digest_field(spec.get("asset_blend_sha256"), "asset_blend_sha256")
    artifact_size = artifact.get("size_bytes")
    if (
        sha256_file(asset_path) != asset_sha
        or artifact.get("sha256") != asset_sha
        or type(artifact_size) is not int
        or artifact_size != asset_path.stat().st_size
    ):
        raise ContractError("generated asset SHA-256 or size binding drifted")
    return {
        "target": target_name,
        "object": object_name,
        "receipt_path": receipt_path,
        "asset_path": asset_path,
        "attempt_receipt_file_sha256": receipt_file_sha,
        "attempt_receipt_self_sha256": self_hash,
        "asset_sha256": asset_sha,
        "task_id": receipt["task_id"],
        "attempt": attempt,
        "rubric_score": rubric_score,
        "vlm_score": vlm_score,
    }


def bounds(vertices: Sequence[Sequence[float]]) -> tuple[Vector3, Vector3]:
    if not vertices:
        raise ContractError("mesh vertices must not be empty")
    cleaned: list[Vector3] = []
    for vertex in vertices:
        if not isinstance(vertex, (list, tuple)) or len(vertex) != 3:
            raise ContractError("each vertex must have exactly three coordinates")
        point = tuple(float(component) for component in vertex)
        if any(not math.isfinite(component) for component in point):
            raise ContractError("vertex coordinates must be finite")
        cleaned.append(point)  # type: ignore[arg-type]
    return (
        tuple(min(point[axis] for point in cleaned) for axis in range(3)),
        tuple(max(point[axis] for point in cleaned) for axis in range(3)),
    )


def extents(box: tuple[Vector3, Vector3]) -> Vector3:
    return tuple(box[1][axis] - box[0][axis] for axis in range(3))  # type: ignore[return-value]


def center(box: tuple[Vector3, Vector3]) -> Vector3:
    return tuple((box[0][axis] + box[1][axis]) / 2.0 for axis in range(3))  # type: ignore[return-value]


def bbox_volume(box: tuple[Vector3, Vector3]) -> float:
    dimensions = extents(box)
    return dimensions[0] * dimensions[1] * dimensions[2]


def fit_vertices_to_bbox(
    vertices: Sequence[Sequence[float]], target_box: tuple[Vector3, Vector3]
) -> list[Vector3]:
    source_box = bounds(vertices)
    source_extent, target_extent = extents(source_box), extents(target_box)
    source_center = tuple(
        (source_box[0][axis] + source_box[1][axis]) / 2 for axis in range(3)
    )
    target_center = tuple(
        (target_box[0][axis] + target_box[1][axis]) / 2 for axis in range(3)
    )
    if any(value <= 1e-9 for value in source_extent) or any(
        value <= 1e-9 for value in target_extent
    ):
        raise ContractError(
            "source and target bounding boxes must have non-zero size on every axis"
        )
    return [
        tuple(
            (float(vertex[axis]) - source_center[axis])
            * target_extent[axis]
            / source_extent[axis]
            + target_center[axis]
            for axis in range(3)
        )
        for vertex in vertices
    ]  # type: ignore[misc]


def bbox_extent_ratios(
    reference: Sequence[Sequence[float]], candidate: Sequence[Sequence[float]]
) -> Vector3:
    ref, cand = extents(bounds(reference)), extents(bounds(candidate))
    if any(value <= 1e-9 for value in ref):
        raise ContractError("reference bounding box is degenerate")
    return tuple(cand[axis] / ref[axis] for axis in range(3))  # type: ignore[return-value]


def validate_faces(faces: Any, vertex_count: int) -> list[tuple[int, ...]]:
    if not isinstance(faces, list) or not faces:
        raise ContractError("mesh faces must be a non-empty array")
    result = []
    for face in faces:
        if (
            not isinstance(face, list)
            or len(face) < 3
            or any(type(index) is not int for index in face)
        ):
            raise ContractError(
                "each face must contain at least three integer vertex indices"
            )
        if len(set(face)) != len(face) or any(
            index < 0 or index >= vertex_count for index in face
        ):
            raise ContractError(
                "face contains duplicate or out-of-range vertex indices"
            )
        result.append(tuple(face))
    return result


def _bpy(module: Any | None) -> Any:
    if module is not None:
        return module
    try:
        import bpy  # type: ignore[import-not-found]

        return bpy
    except ImportError as exc:
        raise RuntimeError("geometry operators must run inside Blender") from exc


def _entries(request: EditRequest, key: str) -> Mapping[str, Any]:
    value = request.parameters.get(key)
    if not isinstance(value, Mapping):
        raise ContractError(f"parameters.{key} must be an object")
    return value


def _finite_tolerance(
    spec: Mapping[str, Any], key: str, default: float, maximum: float
) -> float:
    try:
        value = float(spec.get(key, default))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError(f"{key} must be numeric") from exc
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ContractError(f"{key} must be between 0 and {maximum}")
    return value


def _matrix_rows(matrix: Any) -> list[list[float]]:
    return [[round(float(value), 7) for value in row] for row in matrix]


def _orientation_rows(matrix: Any) -> list[list[float]]:
    return _matrix_rows(matrix.to_quaternion().to_matrix())


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))


def _world_center(matrix: Any, local_box: tuple[Vector3, Vector3]) -> Vector3:
    local = center(local_box)
    return tuple(
        sum(float(matrix[row][column]) * local[column] for column in range(3))
        + float(matrix[row][3])
        for row in range(3)
    )  # type: ignore[return-value]


_TIME_DEPENDENT_MODIFIERS = {
    "ARMATURE",
    "CLOTH",
    "DYNAMIC_PAINT",
    "EXPLODE",
    "FLUID",
    "MESH_SEQUENCE_CACHE",
    "NODES",
    "OCEAN",
    "PARTICLE_INSTANCE",
    "PARTICLE_SYSTEM",
    "SOFT_BODY",
    "WAVE",
}


def _active_animation(value: Any) -> bool:
    animation = getattr(value, "animation_data", None)
    if animation is None:
        return False
    return bool(
        getattr(animation, "action", None)
        or len(getattr(animation, "drivers", ()))
        or len(getattr(animation, "nla_tracks", ()))
    )


def geometry_target_unsupported_reason(obj: Any) -> str | None:
    """Return why a target cannot have one-frame visual geometry attestation."""
    data = getattr(obj, "data", None)
    if getattr(data, "shape_keys", None) is not None:
        return "shape keys"
    if _active_animation(obj) or _active_animation(data):
        return "object or mesh animation/drivers/NLA"
    if len(getattr(obj, "constraints", ())):
        return "object constraints"
    if getattr(obj, "parent_type", "OBJECT") in {"BONE", "ARMATURE"}:
        return "armature or bone parenting"
    parent = getattr(obj, "parent", None)
    while parent is not None:
        if _active_animation(parent) or len(getattr(parent, "constraints", ())):
            return "animated or constrained parent"
        parent = getattr(parent, "parent", None)
    if getattr(obj, "instance_type", "NONE") != "NONE":
        return "object instancing"
    if bool(getattr(obj, "hide_render", False)):
        return "target hidden from render"
    for modifier in getattr(obj, "modifiers", ()):
        if modifier.type in _TIME_DEPENDENT_MODIFIERS:
            return f"time-dependent {modifier.type} modifier"
        if bool(modifier.show_viewport) != bool(modifier.show_render):
            return f"modifier {modifier.name} has different viewport/render visibility"
        for viewport_field, render_field in (
            ("levels", "render_levels"),
            ("sculpt_levels", "render_levels"),
        ):
            if (
                hasattr(modifier, viewport_field)
                and hasattr(modifier, render_field)
                and getattr(modifier, viewport_field) != getattr(modifier, render_field)
            ):
                return f"modifier {modifier.name} has different viewport/render detail"
    return None


def _evaluated_visible_sample(bpy: Any, obj: Any) -> dict[str, Any]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    depsgraph.update()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    if mesh is None:
        raise ContractError(f"evaluated target has no visible mesh: {obj.name}")
    try:
        matrix = evaluated.matrix_world.copy()
        vertices = [
            tuple(float(value) for value in (matrix @ vertex.co))
            for vertex in mesh.vertices
        ]
        if len(vertices) < 4:
            raise ContractError(
                f"evaluated target mesh is empty or invalid: {obj.name}"
            )
        box = bounds(vertices)
        dimensions = extents(box)
        volume = bbox_volume(box)
        if any(value <= 1e-9 for value in dimensions) or volume <= 1e-12:
            raise ContractError(
                f"evaluated target world envelope is degenerate: {obj.name}"
            )
        return {
            "frame": int(bpy.context.scene.frame_current),
            "vertex_count": len(vertices),
            "world_bounds": [list(box[0]), list(box[1])],
            "world_extents": list(dimensions),
            "world_center": list(center(box)),
            "world_bbox_volume": volume,
            "transform": _matrix_rows(matrix),
            "orientation": _orientation_rows(matrix),
        }
    finally:
        evaluated.to_mesh_clear()


def _validate_evaluated_visible_envelope(
    name: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    envelope_tolerance: float,
    volume_tolerance: float,
    center_tolerance: float,
) -> dict[str, Any]:
    before_extents = before["world_extents"]
    after_extents = after["world_extents"]
    ratios = tuple(
        float(after_extents[axis]) / float(before_extents[axis]) for axis in range(3)
    )
    volume_ratio = float(after["world_bbox_volume"]) / float(
        before["world_bbox_volume"]
    )
    center_delta = _distance(before["world_center"], after["world_center"])
    if before["frame"] != after["frame"]:
        raise ContractError("evaluated visible samples use different evidence frames")
    if before["transform"] != after["transform"]:
        raise ContractError(f"replacement for {name} changes evaluated world transform")
    if before["orientation"] != after["orientation"]:
        raise ContractError(
            f"replacement for {name} changes evaluated world orientation"
        )
    if any(abs(ratio - 1.0) > envelope_tolerance for ratio in ratios):
        raise ContractError(
            f"replacement for {name} exceeds evaluated visible envelope tolerance"
        )
    if abs(volume_ratio - 1.0) > volume_tolerance:
        raise ContractError(
            f"replacement for {name} exceeds evaluated visible volume-ratio tolerance"
        )
    if center_delta > center_tolerance:
        raise ContractError(
            f"replacement for {name} moves the evaluated visible world center"
        )
    return {
        "frame": before["frame"],
        "before": dict(before),
        "after": dict(after),
        "world_extent_ratios": [round(value, 7) for value in ratios],
        "world_bbox_volume_ratio": round(volume_ratio, 7),
        "world_center_delta": round(center_delta, 7),
    }


def _load_generated_mesh(bpy: Any, handoff: Mapping[str, Any]) -> Any:
    """Append one generated object, bake its evaluated mesh, and remove the donor object."""
    asset_path = handoff["asset_path"]
    object_name = handoff["object"]
    with bpy.data.libraries.load(str(asset_path), link=False) as (available, loaded):
        if object_name not in available.objects:
            raise ContractError(f"generated asset object not found: {object_name}")
        loaded.objects = [object_name]
    donor = loaded.objects[0]
    if donor is None or donor.type != "MESH":
        if donor is not None:
            bpy.data.objects.remove(donor, do_unlink=True)
        raise ContractError(
            f"generated replacement object is not a mesh: {object_name}"
        )
    temporary_collection = bpy.data.collections.new("__EDIT_GENERATION_HANDOFF__")
    bpy.context.scene.collection.children.link(temporary_collection)
    temporary_collection.objects.link(donor)
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = donor.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(
            evaluated, preserve_all_data_layers=True, depsgraph=depsgraph
        )
        if mesh is None or len(mesh.vertices) < 4 or len(mesh.polygons) < 1:
            if mesh is not None:
                bpy.data.meshes.remove(mesh)
            raise ContractError(
                "generated replacement evaluates to an empty or invalid mesh"
            )
        donor_matrix = donor.matrix_world.copy()
        for vertex in mesh.vertices:
            vertex.co = donor_matrix @ vertex.co
        return mesh
    finally:
        bpy.data.objects.remove(donor, do_unlink=True)
        bpy.data.collections.remove(temporary_collection)


def _replacement_spec(
    request: EditRequest, bpy: Any, name: str, spec: Mapping[str, Any]
) -> tuple[
    Any | None,
    list[Vector3],
    list[tuple[int, ...]],
    Mapping[str, Any] | None,
    dict[str, Any],
]:
    """Return an imported mesh or validated inline mesh data and provenance."""
    if "attempt_receipt" in spec:
        handoff = validate_generation_handoff(spec, name)
        mesh = _load_generated_mesh(bpy, handoff)
        vertices = [
            tuple(float(value) for value in vertex.co) for vertex in mesh.vertices
        ]
        faces = [
            tuple(int(value) for value in polygon.vertices) for polygon in mesh.polygons
        ]
        provenance = {
            key: value
            for key, value in handoff.items()
            if key not in {"receipt_path", "asset_path"}
        }
        provenance["source"] = "accepted_generation_attempt"
        return mesh, vertices, faces, None, provenance
    allowed = {
        "vertices",
        "faces",
        "uv_layers",
        "fit_to_source_bbox",
        "envelope_tolerance",
        "volume_ratio_tolerance",
        "world_center_tolerance",
    }
    if unknown := set(spec) - allowed:
        raise ContractError(
            f"inline replacement for {name} has unknown fields {sorted(unknown)}"
        )
    vertices = spec.get("vertices")
    if not isinstance(vertices, list) or len(vertices) < 4:
        raise ContractError(f"replacement for {name} requires at least four vertices")
    bounds(vertices)
    faces = validate_faces(spec.get("faces"), len(vertices))
    return (
        None,
        [tuple(map(float, point)) for point in vertices],
        faces,
        spec.get("uv_layers"),
        {
            "source": "declarative_inline_mesh",
        },
    )


def apply_geometry(
    request: EditRequest, bpy_module: Any | None = None
) -> dict[str, Any]:
    bpy = _bpy(bpy_module)
    replacements = _entries(request, "replacements")
    if set(replacements) != set(request.targets.objects):
        raise ContractError(
            "parameters.replacements must exactly match targets.objects"
        )
    changed = []
    measurements: dict[str, Any] = {}
    for name in request.targets.objects:
        spec = replacements[name]
        if not isinstance(spec, Mapping):
            raise ContractError(f"replacement for {name} must be an object")
        obj = bpy.data.objects[name]
        if unsupported := geometry_target_unsupported_reason(obj):
            raise ContractError(
                f"geometry target {name} is unsupported for single-frame visual attestation: {unsupported}"
            )
        before_visible = _evaluated_visible_sample(bpy, obj)
        original_transform = obj.matrix_world.copy()
        original_orientation = _orientation_rows(original_transform)
        old_vertices = [tuple(vertex.co) for vertex in obj.data.vertices]
        old_box = bounds(old_vertices)
        imported_mesh, vertices, faces, uv_layers, provenance = _replacement_spec(
            request, bpy, name, spec
        )
        fit = spec.get("fit_to_source_bbox", True)
        if type(fit) is not bool:
            raise ContractError("fit_to_source_bbox must be boolean")
        fitted = fit_vertices_to_bbox(vertices, old_box) if fit else vertices
        tolerance = _finite_tolerance(spec, "envelope_tolerance", 0.15, 1.0)
        volume_tolerance = _finite_tolerance(spec, "volume_ratio_tolerance", 0.35, 1.0)
        center_tolerance = _finite_tolerance(
            spec, "world_center_tolerance", 1e-5, 100000.0
        )
        ratios = bbox_extent_ratios(old_vertices, fitted)
        if any(abs(ratio - 1.0) > tolerance for ratio in ratios):
            if imported_mesh is not None:
                bpy.data.meshes.remove(imported_mesh)
            raise ContractError(f"replacement for {name} exceeds envelope tolerance")
        new_box = bounds(fitted)
        old_volume = bbox_volume(old_box)
        if old_volume <= 1e-12:
            if imported_mesh is not None:
                bpy.data.meshes.remove(imported_mesh)
            raise ContractError("target bounding box volume is degenerate")
        volume_ratio = bbox_volume(new_box) / old_volume
        if abs(volume_ratio - 1.0) > volume_tolerance:
            if imported_mesh is not None:
                bpy.data.meshes.remove(imported_mesh)
            raise ContractError(
                f"replacement for {name} exceeds bbox volume-ratio tolerance"
            )
        old_world_center = _world_center(original_transform, old_box)
        new_world_center = _world_center(original_transform, new_box)
        world_center_delta = _distance(old_world_center, new_world_center)
        if world_center_delta > center_tolerance:
            if imported_mesh is not None:
                bpy.data.meshes.remove(imported_mesh)
            raise ContractError(
                f"replacement for {name} moves the world envelope center"
            )

        source_mesh = obj.data
        original_materials = [slot.material for slot in obj.material_slots]
        original_uv_names = [layer.name for layer in source_mesh.uv_layers]
        if original_uv_names and not isinstance(uv_layers, Mapping):
            if imported_mesh is None or not set(original_uv_names).issubset(
                layer.name for layer in imported_mesh.uv_layers
            ):
                if imported_mesh is not None:
                    bpy.data.meshes.remove(imported_mesh)
                raise ContractError(
                    f"replacement for {name} must retain every source UV layer"
                )
        if imported_mesh is None:
            mesh = source_mesh.copy() if source_mesh.users > 1 else source_mesh
            if mesh is not source_mesh:
                obj.data = mesh
            if (
                isinstance(uv_layers, Mapping)
                and original_uv_names
                and set(uv_layers) != set(original_uv_names)
            ):
                raise ContractError(
                    f"replacement UV layer names for {name} must match the source"
                )
            mesh.clear_geometry()
            mesh.from_pydata(fitted, [], faces)
            mesh.update(calc_edges=True)
            if isinstance(uv_layers, Mapping):
                while mesh.uv_layers:
                    mesh.uv_layers.remove(mesh.uv_layers[0])
                loop_count = len(mesh.loops)
                for layer_name, coordinates in uv_layers.items():
                    if (
                        not isinstance(layer_name, str)
                        or not isinstance(coordinates, list)
                        or len(coordinates) != loop_count
                    ):
                        raise ContractError(
                            f"UV layer {layer_name!r} must contain one coordinate per mesh loop"
                        )
                    layer = mesh.uv_layers.new(name=layer_name)
                    for index, coordinate in enumerate(coordinates):
                        if not isinstance(coordinate, list) or len(coordinate) != 2:
                            raise ContractError(
                                f"UV layer {layer_name!r} contains an invalid coordinate"
                            )
                        uv = tuple(float(value) for value in coordinate)
                        if any(not math.isfinite(value) for value in uv):
                            raise ContractError(
                                f"UV layer {layer_name!r} contains a non-finite coordinate"
                            )
                        layer.data[index].uv = uv
        else:
            mesh = imported_mesh
            for index, vertex in enumerate(mesh.vertices):
                vertex.co = fitted[index]
            mesh.materials.clear()
            for material in original_materials:
                mesh.materials.append(material)
            slot_count = len(original_materials)
            for polygon in mesh.polygons:
                if slot_count == 0 or polygon.material_index >= slot_count:
                    polygon.material_index = 0
            obj.data = mesh
        if _matrix_rows(obj.matrix_world) != _matrix_rows(original_transform):
            raise ContractError(
                f"replacement for {name} changed the target object transform"
            )
        if _orientation_rows(obj.matrix_world) != original_orientation:
            raise ContractError(
                f"replacement for {name} changed the target object orientation"
            )
        if [slot.material for slot in obj.material_slots] != original_materials:
            raise ContractError(
                f"replacement for {name} changed material-slot identity"
            )
        after_visible = _evaluated_visible_sample(bpy, obj)
        evaluated_validation = _validate_evaluated_visible_envelope(
            name,
            before_visible,
            after_visible,
            envelope_tolerance=tolerance,
            volume_tolerance=volume_tolerance,
            center_tolerance=center_tolerance,
        )
        changed.append(name)
        measurements[name] = {
            "provenance": provenance,
            "fit_to_source_bbox": fit,
            "envelope_tolerance": tolerance,
            "extent_ratios": evaluated_validation["world_extent_ratios"],
            "bbox_volume_ratio": evaluated_validation["world_bbox_volume_ratio"],
            "volume_ratio_tolerance": volume_tolerance,
            "world_center_before": evaluated_validation["before"]["world_center"],
            "world_center_after": evaluated_validation["after"]["world_center"],
            "world_center_delta": evaluated_validation["world_center_delta"],
            "world_center_tolerance": center_tolerance,
            "transform_before": evaluated_validation["before"]["transform"],
            "transform_after": evaluated_validation["after"]["transform"],
            "orientation_before": evaluated_validation["before"]["orientation"],
            "orientation_after": evaluated_validation["after"]["orientation"],
            "evaluated_visible": evaluated_validation,
            "raw_local_fit": {
                "extent_ratios": [round(value, 7) for value in ratios],
                "bbox_volume_ratio": round(volume_ratio, 7),
                "world_center_before": [round(value, 7) for value in old_world_center],
                "world_center_after": [round(value, 7) for value in new_world_center],
                "world_center_delta": round(world_center_delta, 7),
            },
            "material_slots_preserved": True,
            "object_identity_preserved": True,
        }
    return {"changed_objects": changed, "geometry_validation": measurements}


_PRIMITIVES = {
    "cube": "primitive_cube_add",
    "uv_sphere": "primitive_uv_sphere_add",
    "cylinder": "primitive_cylinder_add",
    "cone": "primitive_cone_add",
    "torus": "primitive_torus_add",
}
_MODIFIERS = {"BEVEL", "SOLIDIFY", "SUBSURF"}


def _vec(
    value: Any, label: str, default: tuple[float, float, float]
) -> tuple[float, float, float]:
    value = default if value is None else value
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ContractError(f"{label} must have three values")
    result = tuple(float(item) for item in value)
    if any(not math.isfinite(item) for item in result):
        raise ContractError(f"{label} values must be finite")
    return result  # type: ignore[return-value]


def apply_modeling(
    request: EditRequest, bpy_module: Any | None = None
) -> dict[str, Any]:
    bpy = _bpy(bpy_module)
    operations = request.parameters.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ContractError("parameters.operations must be a non-empty array")
    created, modified = [], []
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise ContractError("each modeling operation must be an object")
        op = operation.get("op")
        if op == "add_primitive":
            allowed_fields = {
                "op",
                "name",
                "primitive",
                "collection",
                "location",
                "rotation",
                "scale",
            }
            if set(operation) - allowed_fields:
                raise ContractError("add_primitive contains unsupported fields")
            name, primitive, collection_name = (
                operation.get("name"),
                operation.get("primitive"),
                operation.get("collection"),
            )
            if name not in request.targets.generated_objects or name in created:
                raise ContractError(
                    "primitive name must be a unique declared generated object"
                )
            if primitive not in _PRIMITIVES:
                raise ContractError(f"primitive is not allowlisted: {primitive}")
            collection = (
                bpy.data.collections.get(collection_name)
                if isinstance(collection_name, str)
                else None
            )
            if collection is None:
                raise ContractError(
                    "add_primitive requires an existing target collection"
                )
            kwargs = {
                "location": _vec(
                    operation.get("location"), "location", (0.0, 0.0, 0.0)
                ),
                "rotation": _vec(
                    operation.get("rotation"), "rotation", (0.0, 0.0, 0.0)
                ),
            }
            getattr(bpy.ops.mesh, _PRIMITIVES[primitive])(**kwargs)
            obj = bpy.context.object
            obj.name = name
            obj.scale = _vec(operation.get("scale"), "scale", (1.0, 1.0, 1.0))
            for existing in list(obj.users_collection):
                existing.objects.unlink(obj)
            collection.objects.link(obj)
            created.append(name)
        elif op == "add_modifier":
            allowed_fields = {"op", "object", "modifier_type", "name", "settings"}
            if set(operation) - allowed_fields:
                raise ContractError("add_modifier contains unsupported fields")
            name, modifier_type = (
                operation.get("object"),
                operation.get("modifier_type"),
            )
            if name not in request.targets.objects or modifier_type not in _MODIFIERS:
                raise ContractError(
                    "modifier object or type is outside the declared allowlist"
                )
            obj = bpy.data.objects[name]
            modifier = obj.modifiers.new(
                name=str(operation.get("name", f"Edit_{modifier_type}")),
                type=modifier_type,
            )
            settings = operation.get("settings", {})
            if not isinstance(settings, Mapping):
                raise ContractError("modifier settings must be an object")
            allowed_settings = {
                "BEVEL": {"width", "segments"},
                "SOLIDIFY": {"thickness", "offset"},
                "SUBSURF": {"levels", "render_levels"},
            }[modifier_type]
            if set(settings) - allowed_settings:
                raise ContractError(f"unsupported {modifier_type} modifier settings")
            for field, value in settings.items():
                if field in {"segments", "levels", "render_levels"}:
                    maximum = 64 if field == "segments" else 6
                    if (
                        type(value) is not int
                        or not 0 <= value <= maximum
                        or (field == "segments" and value < 1)
                    ):
                        raise ContractError(
                            f"invalid integer modifier setting: {field}"
                        )
                else:
                    value = float(value)
                    limits = {
                        "width": (0.0, 100000.0),
                        "thickness": (-100000.0, 100000.0),
                        "offset": (-1.0, 1.0),
                    }
                    if (
                        not math.isfinite(value)
                        or not limits[field][0] <= value <= limits[field][1]
                    ):
                        raise ContractError(
                            f"invalid numeric modifier setting: {field}"
                        )
                setattr(modifier, field, value)
            modified.append(name)
        else:
            raise ContractError(f"unsupported modeling operation: {op}")
    if set(created) != set(request.targets.generated_objects):
        raise ContractError(
            "every declared generated object must be created exactly once"
        )
    if set(modified) != set(request.targets.objects):
        raise ContractError(
            "every declared target object must receive a modeling operation"
        )
    return {"created_objects": created, "changed_objects": sorted(set(modified))}
