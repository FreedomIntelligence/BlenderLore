"""Deterministic Blender snapshots, scope checks, and render comparison."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import struct
from pathlib import Path
from typing import Any, Mapping
import zlib

from .contracts import EditKind, EditRequest, digest_json


class QualityError(RuntimeError):
    """Raised when structural or visual quality gates fail."""


def difference_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Return compact deterministic paths whose canonical snapshot values differ."""
    if type(before) is not type(after):
        return [prefix or "$"]
    if isinstance(before, Mapping):
        result: list[str] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(difference_paths(before[key], after[key], path))
        return result
    if isinstance(before, list):
        if len(before) != len(after):
            return [prefix]
        result = []
        for index, (old, new) in enumerate(zip(before, after)):
            result.extend(difference_paths(old, new, f"{prefix}[{index}]"))
        return result
    return [] if before == after else [prefix]


def _round(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 7)
    if isinstance(value, (list, tuple)):
        return [_round(item) for item in value]
    try:
        return [_round(item) for item in value]
    except TypeError:
        if hasattr(value, "name"):
            return {"name": str(value.name), "type": type(value).__name__}
        return {"type": type(value).__name__}


def _rna_scalar_record(value: Any, excluded: set[str] | None = None) -> dict[str, Any]:
    """Capture stable scalar/array/pointer RNA fields without traversing collections."""
    excluded = (excluded or set()) | {"rna_type"}
    properties = getattr(getattr(value, "bl_rna", None), "properties", ())
    result: dict[str, Any] = {}
    for prop in properties:
        name = prop.identifier
        if name in excluded or prop.type == "COLLECTION":
            continue
        try:
            field = getattr(value, name)
            if prop.type == "POINTER":
                result[name] = (
                    None
                    if field is None
                    else {
                        "name": str(getattr(field, "name", "")),
                        "type": type(field).__name__,
                    }
                )
            elif prop.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}:
                result[name] = _round(field)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
    return result


def _driver_record(animation_data: Any) -> list[dict[str, Any]]:
    if animation_data is None:
        return []
    result = []
    for curve in sorted(
        animation_data.drivers, key=lambda item: (item.data_path, item.array_index)
    ):
        driver = curve.driver
        variables = []
        for variable in sorted(driver.variables, key=lambda item: item.name):
            targets = []
            for target in variable.targets:
                target_id = getattr(target, "id", None)
                targets.append(
                    {
                        "id": None
                        if target_id is None
                        else getattr(target_id, "name", None),
                        "id_type": getattr(target, "id_type", None),
                        "data_path": getattr(target, "data_path", None),
                        "bone_target": getattr(target, "bone_target", None),
                        "transform_type": getattr(target, "transform_type", None),
                        "transform_space": getattr(target, "transform_space", None),
                    }
                )
            variables.append(
                {"name": variable.name, "type": variable.type, "targets": targets}
            )
        result.append(
            {
                "path": curve.data_path,
                "index": curve.array_index,
                "mute": bool(curve.mute),
                "driver": {
                    "type": driver.type,
                    "expression": driver.expression,
                    "use_self": bool(driver.use_self),
                    "variables": variables,
                },
                "modifiers": [
                    {
                        "type": modifier.type,
                        "settings": _rna_scalar_record(modifier, {"type"}),
                    }
                    for modifier in curve.modifiers
                ],
            }
        )
    return result


def _nla_record(animation_data: Any) -> list[dict[str, Any]]:
    if animation_data is None:
        return []
    tracks = []
    for track in animation_data.nla_tracks:
        strips = []
        for strip in track.strips:
            strips.append(
                {
                    "name": strip.name,
                    "type": strip.type,
                    "action": strip.action.name if strip.action else None,
                    "frame_start": _round(strip.frame_start),
                    "frame_end": _round(strip.frame_end),
                    "action_frame_start": _round(strip.action_frame_start),
                    "action_frame_end": _round(strip.action_frame_end),
                    "blend_type": strip.blend_type,
                    "extrapolation": strip.extrapolation,
                    "influence": _round(strip.influence),
                    "repeat": _round(strip.repeat),
                    "scale": _round(strip.scale),
                    "mute": bool(strip.mute),
                }
            )
        tracks.append(
            {
                "name": track.name,
                "mute": bool(track.mute),
                "solo": bool(track.is_solo),
                "strips": strips,
            }
        )
    return tracks


def _animation_data_record(value: Any) -> dict[str, Any]:
    animation_data = getattr(value, "animation_data", None)
    return {
        "action": None
        if animation_data is None or animation_data.action is None
        else animation_data.action.name,
        "drivers": _driver_record(animation_data),
        "nla_tracks": _nla_record(animation_data),
    }


def _socket_value(socket: Any) -> Any:
    if not hasattr(socket, "default_value"):
        return None
    value = socket.default_value
    if isinstance(value, (str, bool, int, float)):
        return _round(value)
    return _round(value)


def _material_record(material: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "blend_method": getattr(
            material, "surface_render_method", getattr(material, "blend_method", None)
        ),
        "diffuse_color": _round(material.diffuse_color),
        "use_nodes": bool(material.use_nodes),
        "animation_data": _animation_data_record(material),
    }
    tree = material.node_tree
    if not material.use_nodes or tree is None:
        return record
    record.update(_node_tree_record(tree))
    return record


def _node_tree_record(tree: Any) -> dict[str, Any]:
    return {
        "nodes": sorted(
            (
                {
                    "name": node.name,
                    "type": node.bl_idname,
                    "location": _round(node.location),
                    "properties": _rna_scalar_record(
                        node,
                        {
                            "name",
                            "bl_idname",
                            "location",
                            "inputs",
                            "outputs",
                            "internal_links",
                        },
                    ),
                    "inputs": {
                        socket.name: _socket_value(socket)
                        for socket in node.inputs
                        if not socket.is_linked
                    },
                }
                for node in tree.nodes
            ),
            key=lambda item: item["name"],
        ),
        "links": sorted(
            (
                link.from_node.name,
                link.from_socket.name,
                link.to_node.name,
                link.to_socket.name,
            )
            for link in tree.links
        ),
    }


def _mesh_record(mesh: Any) -> dict[str, Any]:
    return {
        "vertices": [_round(vertex.co) for vertex in mesh.vertices],
        "edges": [list(edge.vertices) for edge in mesh.edges],
        "polygons": [list(polygon.vertices) for polygon in mesh.polygons],
        "uv_layers": {
            layer.name: [_round(item.uv) for item in layer.data]
            for layer in mesh.uv_layers
        },
    }


def _data_record(obj: Any) -> Any:
    data = obj.data
    if data is None:
        return None
    if obj.type == "MESH":
        return {
            "name": data.name,
            "mesh": _mesh_record(data),
            "animation_data": _animation_data_record(data),
        }
    if obj.type == "LIGHT":
        return {
            "name": data.name,
            "type": data.type,
            "energy": _round(data.energy),
            "color": _round(data.color),
            "shadow_soft_size": _round(getattr(data, "shadow_soft_size", 0.0)),
            "spot_size": _round(getattr(data, "spot_size", 0.0)),
            "animation_data": _animation_data_record(data),
        }
    if obj.type == "CAMERA":
        return {
            "name": data.name,
            "type": data.type,
            "lens": _round(data.lens),
            "ortho_scale": _round(data.ortho_scale),
            "clip_start": _round(data.clip_start),
            "clip_end": _round(data.clip_end),
            "animation_data": _animation_data_record(data),
        }
    return {
        "name": data.name,
        "type": obj.type,
        "animation_data": _animation_data_record(data),
    }


def _constraint_record(constraint: Any) -> dict[str, Any]:
    return {
        "name": constraint.name,
        "type": constraint.type,
        "settings": _rna_scalar_record(constraint, {"name", "type"}),
    }


def _object_record(obj: Any) -> dict[str, Any]:
    return {
        "type": obj.type,
        "identity": {
            "location": _round(obj.location),
            "rotation_mode": obj.rotation_mode,
            "rotation_euler": _round(obj.rotation_euler),
            "scale": _round(obj.scale),
            "rotation_quaternion": _round(obj.rotation_quaternion),
            "rotation_axis_angle": _round(obj.rotation_axis_angle),
            "matrix_world": _round(obj.matrix_world),
            "matrix_parent_inverse": _round(obj.matrix_parent_inverse),
            "parent": obj.parent.name if obj.parent else None,
            "collections": sorted(
                collection.name for collection in obj.users_collection
            ),
            "hide_render": bool(obj.hide_render),
            "hide_viewport": bool(obj.hide_viewport),
        },
        "material_slots": [
            slot.material.name if slot.material else None for slot in obj.material_slots
        ],
        "modifiers": [
            {
                "name": modifier.name,
                "type": modifier.type,
                "show_render": bool(modifier.show_render),
                "settings": _rna_scalar_record(modifier, {"name", "type"}),
            }
            for modifier in obj.modifiers
        ],
        "constraints": [
            _constraint_record(constraint) for constraint in obj.constraints
        ],
        "animation_data": _animation_data_record(obj),
        "data": _data_record(obj),
    }


def _animation_record(bpy: Any) -> list[Any]:
    def curves_record(curves: Any) -> list[dict[str, Any]]:
        result = []
        for curve in sorted(
            curves, key=lambda item: (item.data_path, item.array_index)
        ):
            result.append(
                {
                    "path": curve.data_path,
                    "index": curve.array_index,
                    "points": [
                        (_round(point.co), point.interpolation)
                        for point in curve.keyframe_points
                    ],
                }
            )
        return result

    result = []
    actions = [
        action
        for action in bpy.data.actions
        if action.users > 0 or action.use_fake_user
    ]
    for action in sorted(actions, key=lambda item: item.name):
        if hasattr(action, "fcurves"):
            result.append(
                {"name": action.name, "legacy_curves": curves_record(action.fcurves)}
            )
            continue
        layers = []
        for layer_index, layer in enumerate(action.layers):
            strips = []
            for strip_index, strip in enumerate(layer.strips):
                channelbags = []
                for bag in sorted(
                    strip.channelbags, key=lambda item: item.slot.identifier
                ):
                    channelbags.append(
                        {
                            "slot": bag.slot.identifier,
                            "curves": curves_record(bag.fcurves),
                        }
                    )
                strips.append(
                    {
                        "index": strip_index,
                        "type": strip.type,
                        "channelbags": channelbags,
                    }
                )
            layers.append({"index": layer_index, "name": layer.name, "strips": strips})
        result.append(
            {
                "name": action.name,
                "slots": sorted(slot.identifier for slot in action.slots),
                "layers": layers,
            }
        )
    return result


def snapshot_scene(bpy_module: Any | None = None) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of the active Blender database."""
    if bpy_module is None:
        try:
            import bpy as bpy_module  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("snapshot_scene must run inside Blender") from exc
    bpy = bpy_module
    scene = bpy.context.scene
    world = scene.world
    render = scene.render
    image = render.image_settings
    view = scene.view_settings
    compositor_tree = getattr(scene, "node_tree", None)
    if compositor_tree is None:
        compositor_tree = getattr(scene, "compositing_node_group", None)
    objects = [obj for obj in bpy.data.objects if obj.users > 0 or obj.use_fake_user]
    materials = [
        mat for mat in bpy.data.materials if mat.users > 0 or mat.use_fake_user
    ]
    collections = [
        collection
        for collection in bpy.data.collections
        if collection.users > 0 or collection.use_fake_user
    ]
    snapshot = {
        "objects": {
            obj.name: _object_record(obj)
            for obj in sorted(objects, key=lambda item: item.name)
        },
        "materials": {
            mat.name: _material_record(mat)
            for mat in sorted(materials, key=lambda item: item.name)
        },
        "collections": {
            collection.name: sorted(obj.name for obj in collection.objects)
            for collection in sorted(collections, key=lambda item: item.name)
        },
        "world": None
        if world is None
        else {
            "name": world.name,
            "color": _round(world.color),
            "nodes": None
            if not world.use_nodes or world.node_tree is None
            else _node_tree_record(world.node_tree),
            "animation_data": _animation_data_record(world),
        },
        "compositor": {
            "use_nodes": bool(scene.use_nodes),
            "node_tree": None
            if not scene.use_nodes or compositor_tree is None
            else _node_tree_record(compositor_tree),
        },
        "scene_settings": {
            "camera": scene.camera.name if scene.camera else None,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "frame_step": scene.frame_step,
            "render": {
                "engine": render.engine,
                "resolution_x": render.resolution_x,
                "resolution_y": render.resolution_y,
                "resolution_percentage": render.resolution_percentage,
                "pixel_aspect_x": _round(render.pixel_aspect_x),
                "pixel_aspect_y": _round(render.pixel_aspect_y),
                "fps": render.fps,
                "fps_base": _round(render.fps_base),
                "film_transparent": bool(render.film_transparent),
                "use_file_extension": bool(render.use_file_extension),
                "image": {
                    "file_format": image.file_format,
                    "color_mode": image.color_mode,
                    "color_depth": image.color_depth,
                    "compression": image.compression,
                },
                "cycles_samples": _round(
                    getattr(getattr(scene, "cycles", None), "samples", None)
                ),
            },
            "color_management": {
                "display_device": scene.display_settings.display_device,
                "view_transform": view.view_transform,
                "look": view.look,
                "exposure": _round(view.exposure),
                "gamma": _round(view.gamma),
                "sequencer_color_space": scene.sequencer_colorspace_settings.name,
            },
            "animation_data": _animation_data_record(scene),
        },
        "animation": _animation_record(bpy),
    }
    snapshot["digest"] = digest_json(snapshot)
    return snapshot


def _without(record: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in keys}


def _append_if_changed(drift: list[str], label: str, before: Any, after: Any) -> None:
    if before != after:
        drift.append(label)


def validate_non_target_drift(
    before: Mapping[str, Any], after: Mapping[str, Any], request: EditRequest
) -> list[str]:
    """Return stable drift labels outside the declared edit scope."""
    drift: list[str] = []
    before_objects, after_objects = before["objects"], after["objects"]
    before_names, after_names = set(before_objects), set(after_objects)
    allowed_new = (
        set(request.targets.generated_objects)
        if request.kind == EditKind.MODELING
        else set()
    )
    for name in sorted((after_names - before_names) - allowed_new):
        drift.append(f"objects.{name}:unexpected-addition")
    for name in sorted(before_names - after_names):
        drift.append(f"objects.{name}:unexpected-removal")
    for name in sorted(before_names & after_names):
        old, new = before_objects[name], after_objects[name]
        if request.kind == EditKind.GEOMETRY and name in request.targets.objects:
            _append_if_changed(
                drift,
                f"objects.{name}:identity",
                _without(old, "data"),
                _without(new, "data"),
            )
        elif request.kind == EditKind.MODELING and name in request.targets.objects:
            _append_if_changed(
                drift,
                f"objects.{name}:identity",
                _without(old, "data", "modifiers"),
                _without(new, "data", "modifiers"),
            )
        elif request.kind == EditKind.SCENE and (
            name in request.targets.lights or name in request.targets.camera
        ):
            continue
        else:
            _append_if_changed(drift, f"objects.{name}", old, new)

    for name in sorted(set(before["materials"]) | set(after["materials"])):
        if (
            request.kind in {EditKind.MATERIAL, EditKind.COLOR}
            and name in request.targets.materials
        ):
            continue
        _append_if_changed(
            drift,
            f"materials.{name}",
            before["materials"].get(name),
            after["materials"].get(name),
        )

    if request.kind == EditKind.MODELING and request.targets.generated_objects:
        generated = set(request.targets.generated_objects)
        before_collections, after_collections = (
            before["collections"],
            after["collections"],
        )
        for collection in sorted(set(before_collections) | set(after_collections)):
            old_members = before_collections.get(collection)
            new_members = after_collections.get(collection)
            if old_members is None or new_members is None:
                drift.append(f"collections.{collection}")
                continue
            stripped = [name for name in new_members if name not in generated]
            if stripped != old_members:
                drift.append(f"collections.{collection}")
        expected_membership: dict[str, str] = {}
        for operation in request.parameters.get("operations", []):
            if (
                isinstance(operation, Mapping)
                and operation.get("op") == "add_primitive"
            ):
                expected_membership[str(operation.get("name"))] = str(
                    operation.get("collection")
                )
        for name in sorted(generated):
            actual = sorted(
                collection
                for collection, members in after_collections.items()
                if name in members
            )
            expected = [expected_membership.get(name)]
            if actual != expected:
                drift.append(f"objects.{name}:collection-membership")
    else:
        _append_if_changed(
            drift, "collections", before["collections"], after["collections"]
        )
    if not (
        request.kind == EditKind.SCENE and request.parameters.get("allow_world") is True
    ):
        _append_if_changed(drift, "world", before["world"], after["world"])
    if (
        request.kind == EditKind.SCENE
        and request.parameters.get("allow_exposure") is True
    ):
        old_settings = dict(before["scene_settings"])
        new_settings = dict(after["scene_settings"])
        old_settings["color_management"] = _without(
            old_settings["color_management"], "exposure"
        )
        new_settings["color_management"] = _without(
            new_settings["color_management"], "exposure"
        )
        _append_if_changed(drift, "scene_settings", old_settings, new_settings)
    else:
        _append_if_changed(
            drift, "scene_settings", before["scene_settings"], after["scene_settings"]
        )
    _append_if_changed(
        drift, "compositor", before.get("compositor"), after.get("compositor")
    )
    _append_if_changed(drift, "animation", before["animation"], after["animation"])
    return drift


def require_no_drift(
    before: Mapping[str, Any], after: Mapping[str, Any], request: EditRequest
) -> None:
    drift = validate_non_target_drift(before, after, request)
    if drift:
        raise QualityError("non-target drift detected: " + ", ".join(drift))


def require_target_change(
    before: Mapping[str, Any], after: Mapping[str, Any], request: EditRequest
) -> None:
    """Reject a successful-looking receipt for an edit that changed no declared target."""
    changed = False
    if request.kind in {EditKind.MATERIAL, EditKind.COLOR}:
        changed = any(
            before["materials"].get(name) != after["materials"].get(name)
            for name in request.targets.materials
        )
    elif request.kind == EditKind.GEOMETRY:
        changed = any(
            before["objects"][name].get("data") != after["objects"][name].get("data")
            for name in request.targets.objects
        )
    elif request.kind == EditKind.MODELING:
        changed = any(
            name not in before["objects"] and name in after["objects"]
            for name in request.targets.generated_objects
        )
        changed = changed or any(
            before["objects"][name].get("data") != after["objects"][name].get("data")
            or before["objects"][name].get("modifiers")
            != after["objects"][name].get("modifiers")
            for name in request.targets.objects
        )
    elif request.kind == EditKind.SCENE:
        changed = any(
            before["objects"].get(name) != after["objects"].get(name)
            for name in (*request.targets.lights, *request.targets.camera)
        )
        if request.parameters.get("allow_world") is True:
            changed = changed or before["world"] != after["world"]
        if request.parameters.get("allow_exposure") is True:
            changed = changed or (
                before["scene_settings"]["color_management"].get("exposure")
                != after["scene_settings"]["color_management"].get("exposure")
            )
    if not changed:
        raise QualityError(
            "edit produced no observable change in the declared target scope"
        )


@dataclass(frozen=True)
class ImageDifference:
    width: int
    height: int
    changed_fraction: float
    mean_absolute_delta: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _read_png(path: str | Path) -> tuple[int, int, int, bytes]:
    data = Path(path).read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise QualityError(f"not a PNG file: {Path(path).name}")
    offset, width, height, channels, payload = 8, 0, 0, 0, bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind, chunk = (
            data[offset + 4 : offset + 8],
            data[offset + 8 : offset + 8 + length],
        )
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", chunk)
            )
            if (
                depth != 8
                or color not in (2, 6)
                or compression
                or filtering
                or interlace
            ):
                raise QualityError(
                    "render evidence must be non-interlaced 8-bit RGB or RGBA PNG"
                )
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            payload.extend(chunk)
        elif kind == b"IEND":
            break
    raw, stride = zlib.decompress(payload), width * channels
    if len(raw) != height * (stride + 1):
        raise QualityError("invalid PNG scanline length")
    rows, previous, cursor = bytearray(), bytearray(stride), 0
    for _ in range(height):
        filter_type, cursor = raw[cursor], cursor + 1
        if filter_type > 4:
            raise QualityError(f"unsupported PNG filter type: {filter_type}")
        encoded, cursor = raw[cursor : cursor + stride], cursor + stride
        decoded = bytearray(stride)
        for index, value in enumerate(encoded):
            left = decoded[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            predictor = (0, left, up, (left + up) // 2, _paeth(left, up, upper_left))[
                filter_type
            ]
            decoded[index] = (value + predictor) & 255
        rows.extend(decoded)
        previous = decoded
    return width, height, channels, bytes(rows)


def image_difference(
    before_path: str | Path, after_path: str | Path
) -> ImageDifference:
    bw, bh, bc, before = _read_png(before_path)
    aw, ah, ac, after = _read_png(after_path)
    if (bw, bh, bc) != (aw, ah, ac):
        raise QualityError(
            "before and after renders have different dimensions or channels"
        )
    changed, absolute = 0, 0
    pixel_count = bw * bh
    for offset in range(0, len(before), bc):
        deltas = [
            abs(before[offset + channel] - after[offset + channel])
            for channel in range(3)
        ]
        if any(deltas):
            changed += 1
        absolute += sum(deltas)
    return ImageDifference(bw, bh, changed / pixel_count, absolute / (pixel_count * 3))


def require_visual_change(result: ImageDifference, request: EditRequest) -> None:
    if result.changed_fraction < request.evidence.min_changed_fraction:
        raise QualityError("render changed fraction is below the request threshold")
    if result.mean_absolute_delta < request.evidence.min_mean_absolute_delta:
        raise QualityError("render mean absolute delta is below the request threshold")
