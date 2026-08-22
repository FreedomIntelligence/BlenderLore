"""Material and color edits that preserve existing node graphs."""

from __future__ import annotations

import math
from typing import Any, Mapping

from ..contracts import ContractError, EditRequest


_INPUT_LIMITS = {
    "Metallic": (0.0, 1.0),
    "Roughness": (0.0, 1.0),
    "IOR": (1.0, 4.0),
    "Alpha": (0.0, 1.0),
    "Transmission Weight": (0.0, 1.0),
    "Coat Weight": (0.0, 1.0),
    "Coat Roughness": (0.0, 1.0),
}


def _bpy(module: Any | None) -> Any:
    if module is not None:
        return module
    try:
        import bpy  # type: ignore[import-not-found]

        return bpy
    except ImportError as exc:
        raise RuntimeError("material operators must run inside Blender") from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return value


def _active_principled(material: Any) -> Any:
    if not material.use_nodes or material.node_tree is None:
        raise ContractError(f"material {material.name} must use nodes")
    outputs = [
        node
        for node in material.node_tree.nodes
        if node.bl_idname == "ShaderNodeOutputMaterial" and node.is_active_output
    ]
    if len(outputs) != 1:
        raise ContractError(
            f"material {material.name} must have exactly one active material output"
        )
    surface = outputs[0].inputs.get("Surface")
    if surface is None or len(surface.links) != 1:
        raise ContractError(
            f"material {material.name} Surface must have exactly one linked shader"
        )
    shader = surface.links[0].from_node
    if shader.bl_idname != "ShaderNodeBsdfPrincipled":
        raise ContractError(
            f"material {material.name} active shader is not Principled BSDF"
        )
    return shader


def _set_input(shader: Any, name: str, value: Any) -> None:
    socket = shader.inputs.get(name)
    if socket is None:
        raise ContractError(f"unsupported Principled input: {name}")
    if socket.is_linked:
        raise ContractError(f"cannot overwrite linked Principled input: {name}")
    if name in _INPUT_LIMITS:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ContractError(f"{name} must be numeric")
        low, high = _INPUT_LIMITS[name]
        if not low <= float(value) <= high:
            raise ContractError(f"{name} must be between {low} and {high}")
        socket.default_value = float(value)
    elif name in {"Base Color", "Emission Color"}:
        if not isinstance(value, list) or len(value) not in (3, 4):
            raise ContractError(f"{name} must contain three or four channels")
        rgba = [float(channel) for channel in value]
        if any(not math.isfinite(channel) or not 0 <= channel <= 1 for channel in rgba):
            raise ContractError(f"{name} channels must be finite values from 0 to 1")
        socket.default_value = tuple(
            rgba[:3] + ([1.0] if len(rgba) == 3 else [rgba[3]])
        )
    else:
        raise ContractError(f"Principled input is not allowlisted: {name}")


def apply_material(
    request: EditRequest, bpy_module: Any | None = None
) -> dict[str, Any]:
    bpy = _bpy(bpy_module)
    edits = _mapping(request.parameters.get("materials"), "parameters.materials")
    if set(edits) != set(request.targets.materials):
        raise ContractError("parameters.materials must exactly match targets.materials")
    changed = []
    for name in request.targets.materials:
        inputs = _mapping(edits[name], f"parameters.materials.{name}")
        if not inputs:
            raise ContractError(f"material edit is empty: {name}")
        shader = _active_principled(bpy.data.materials[name])
        for input_name, value in inputs.items():
            _set_input(shader, input_name, value)
        changed.append(name)
    return {"changed_materials": changed}


def apply_color(request: EditRequest, bpy_module: Any | None = None) -> dict[str, Any]:
    bpy = _bpy(bpy_module)
    colors = request.parameters.get("colors")
    adjustments = request.parameters.get("hue_saturation")
    if (colors is None) == (adjustments is None):
        raise ContractError(
            "color edit requires exactly one of colors or hue_saturation"
        )
    edits = _mapping(colors if colors is not None else adjustments, "color parameters")
    if set(edits) != set(request.targets.materials):
        raise ContractError("color parameters must exactly match targets.materials")
    for name in request.targets.materials:
        material = bpy.data.materials[name]
        shader = _active_principled(material)
        base = shader.inputs.get("Base Color")
        if base is None:
            raise ContractError(f"material {name} has no Base Color input")
        if colors is not None:
            _set_input(shader, "Base Color", edits[name])
            continue
        values = _mapping(edits[name], f"hue_saturation.{name}")
        allowed = {"hue", "saturation", "value", "factor"}
        if set(values) - allowed:
            raise ContractError(f"unknown Hue/Saturation fields for {name}")
        if len(base.links) != 1:
            raise ContractError(
                f"Hue/Saturation edit requires one linked Base Color input: {name}"
            )
        old_link = base.links[0]
        source_socket = old_link.from_socket
        node = material.node_tree.nodes.new("ShaderNodeHueSaturation")
        node.name = f"EditHueSaturation_{request.edit_id}_{name}"
        node.label = "Pipeline color edit"
        material.node_tree.links.remove(old_link)
        material.node_tree.links.new(source_socket, node.inputs["Color"])
        material.node_tree.links.new(node.outputs["Color"], base)
        for field, socket_name, default in (
            ("hue", "Hue", 0.5),
            ("saturation", "Saturation", 1.0),
            ("value", "Value", 1.0),
            ("factor", "Fac", 1.0),
        ):
            value = float(values.get(field, default))
            limits = {
                "hue": (0.0, 1.0),
                "saturation": (0.0, 2.0),
                "value": (0.0, 2.0),
                "factor": (0.0, 1.0),
            }
            if (
                not math.isfinite(value)
                or not limits[field][0] <= value <= limits[field][1]
            ):
                raise ContractError(
                    f"Hue/Saturation value is outside the supported range: {field}"
                )
            node.inputs[socket_name].default_value = value
    return {"changed_materials": list(request.targets.materials)}
