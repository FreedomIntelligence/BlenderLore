"""Explicitly scoped light, camera, world, and exposure edits."""

from __future__ import annotations

import math
from typing import Any, Mapping

from ..contracts import ContractError, EditRequest


def _bpy(module: Any | None) -> Any:
    if module is not None:
        return module
    try:
        import bpy  # type: ignore[import-not-found]

        return bpy
    except ImportError as exc:
        raise RuntimeError("scene operators must run inside Blender") from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return value


def _number(value: Any, label: str, low: float, high: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ContractError(f"{label} must be between {low} and {high}")
    return result


def _color(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ContractError(f"{label} must have three channels")
    return tuple(_number(channel, label, 0.0, 1.0) for channel in value)  # type: ignore[return-value]


def _vector3(
    value: Any, label: str, low: float, high: float
) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ContractError(f"{label} must contain exactly three values")
    return tuple(_number(component, label, low, high) for component in value)  # type: ignore[return-value]


def _set_world_color(world: Any, color: tuple[float, float, float]) -> None:
    if not world.use_nodes or world.node_tree is None:
        world.color = color
        return
    outputs = [
        node
        for node in world.node_tree.nodes
        if node.bl_idname == "ShaderNodeOutputWorld" and node.is_active_output
    ]
    if len(outputs) != 1:
        raise ContractError(
            "node-based world must have exactly one active World Output"
        )
    surface = outputs[0].inputs.get("Surface")
    if surface is None or len(surface.links) != 1:
        raise ContractError(
            "active World Output Surface must have exactly one linked shader"
        )
    pending, visited, backgrounds = [surface.links[0].from_node], set(), []
    while pending:
        node = pending.pop()
        if node.name in visited:
            continue
        visited.add(node.name)
        if node.bl_idname == "ShaderNodeBackground":
            backgrounds.append(node)
            continue
        for input_socket in node.inputs:
            pending.extend(link.from_node for link in input_socket.links)
    if not backgrounds:
        raise ContractError("active World Output has no reachable Background node")
    for background in backgrounds:
        socket = background.inputs.get("Color")
        if socket is None or socket.is_linked:
            raise ContractError(
                "reachable world Background Color inputs must be unlinked"
            )
        socket.default_value = (*color, 1.0)


def apply_scene(request: EditRequest, bpy_module: Any | None = None) -> dict[str, Any]:
    bpy = _bpy(bpy_module)
    parameters = request.parameters
    for flag in ("allow_world", "allow_exposure"):
        if flag in parameters and type(parameters[flag]) is not bool:
            raise ContractError(f"{flag} must be boolean")
    lights = parameters.get("lights", {})
    if not isinstance(lights, Mapping) or set(lights) != set(request.targets.lights):
        raise ContractError("parameters.lights must exactly match targets.lights")
    for name, raw in lights.items():
        values = _mapping(raw, f"lights.{name}")
        if not values:
            raise ContractError(f"light edit is empty: {name}")
        if set(values) - {"energy", "energy_multiplier", "color", "shadow_soft_size"}:
            raise ContractError(f"unsupported light fields for {name}")
        light = bpy.data.objects[name].data
        if "energy" in values and "energy_multiplier" in values:
            raise ContractError("set energy or energy_multiplier, not both")
        if "energy" in values:
            light.energy = _number(values["energy"], "energy", 0.0, 1_000_000.0)
        if "energy_multiplier" in values:
            factor = _number(
                values["energy_multiplier"], "energy_multiplier", 0.0, 100.0
            )
            light.energy = min(light.energy * factor, 1_000_000.0)
        if "color" in values:
            light.color = _color(values["color"], "light color")
        if "shadow_soft_size" in values:
            light.shadow_soft_size = _number(
                values["shadow_soft_size"], "shadow_soft_size", 0.0, 1000.0
            )
    cameras = parameters.get("cameras", {})
    if not isinstance(cameras, Mapping) or set(cameras) != set(request.targets.camera):
        raise ContractError("parameters.cameras must exactly match targets.camera")
    for name, raw in cameras.items():
        values = _mapping(raw, f"cameras.{name}")
        if not values:
            raise ContractError(f"camera edit is empty: {name}")
        if set(values) - {"location", "rotation", "lens"}:
            raise ContractError(f"unsupported camera fields for {name}")
        obj = bpy.data.objects[name]
        if "location" in values:
            obj.location = _vector3(
                values["location"], "camera location", -1_000_000.0, 1_000_000.0
            )
        if "rotation" in values:
            obj.rotation_euler = _vector3(
                values["rotation"], "camera rotation", -1000.0, 1000.0
            )
        if "lens" in values:
            obj.data.lens = _number(values["lens"], "camera lens", 1.0, 1000.0)
    if parameters.get("allow_world") is True:
        if bpy.context.scene.world is None or "world_color" not in parameters:
            raise ContractError("world edit requires an existing world and world_color")
        _set_world_color(
            bpy.context.scene.world, _color(parameters["world_color"], "world_color")
        )
    elif "world_color" in parameters:
        raise ContractError("world_color requires allow_world=true")
    if parameters.get("allow_exposure") is True:
        if "exposure" not in parameters:
            raise ContractError("exposure edit requires an exposure value")
        bpy.context.scene.view_settings.exposure = _number(
            parameters["exposure"], "exposure", -32.0, 32.0
        )
    elif "exposure" in parameters:
        raise ContractError("exposure requires allow_exposure=true")
    allowed = {
        "lights",
        "cameras",
        "allow_world",
        "world_color",
        "allow_exposure",
        "exposure",
    }
    if set(parameters) - allowed:
        raise ContractError(
            f"unsupported scene parameters: {sorted(set(parameters) - allowed)}"
        )
    return {
        "changed_lights": list(request.targets.lights),
        "changed_cameras": list(request.targets.camera),
        "changed_world": parameters.get("allow_world") is True,
        "changed_exposure": parameters.get("allow_exposure") is True,
    }
