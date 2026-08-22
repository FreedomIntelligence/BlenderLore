#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from video_replay_animation_contract import (
    ENGINE_PROBE_SCHEMA,
    POSTPROCESS_RENDER_RECEIPT_SCHEMA,
    ROUTE_OVERRIDE_SCHEMA,
    AnimationDeliveryPlan,
    analyze_rgba_pixels,
    build_animation_delivery_plan,
    effective_route,
    motion_plan_has_verified_time_animation,
    sample_source_frames,
)

RENDER_TYPES = {
    "MESH",
    "CURVE",
    "CURVES",
    "SURFACE",
    "META",
    "FONT",
    "POINTCLOUD",
    "VOLUME",
    "GPENCIL",
    "GREASEPENCIL",
}
_POSTPROCESS_ENGINE = ""
_POSTPROCESS_ENGINE_POLICY = ""
_POSTPROCESS_CYCLES_BACKEND = ""
_POSTPROCESS_GPU_ATTESTED = False
_POSTPROCESS_OUTPUT_ATTESTED = False
RW1_PRESENTATION_POLICY_GENERATION = "bounded_source_presentation_v2"
RW1_STATIC_INTERCHANGE_PRESENTATION_POLICY_GENERATION = "bounded_source_presentation_v3"
RW1_STATIC_AUTHORED_CAMERA_MIN_COVERAGE = 0.30
RW1_DYNAMIC_AUTHORED_CAMERA_MIN_COVERAGE = 0.12
RW1_DYNAMIC_GENERATED_CAMERA_BOUNDS_SAMPLES = 12
RW1_DYNAMIC_GENERATED_CAMERA_SAFE_MARGIN = 1.12
RW1_FOCUS_PROJECTION_EVIDENCE_SCHEMA = "video2blender.presentation-focus-projection.v1"
RW1_FOCUS_PROJECTION_SAFE_MARGIN = 0.02
RW1_STATIC_ISO_DIRECTION = (1.6, -2.0, 1.25)
GPU_UUID_RE = re.compile(
    r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
    re.I,
)
NVIDIA_SMI = "/usr/bin/nvidia-smi"
ENGINE_PROBE_COMPUTE_RESOLUTION = 512
ENGINE_PROBE_COMPUTE_SAMPLE_CAP = 65536
ENGINE_PROBE_COMPUTE_TIME_LIMIT_SECONDS = 4.0
ENGINE_PROBE_VISUAL_RESOLUTION = 256
ENGINE_PROBE_VISUAL_SAMPLES = 32
ENGINE_PROBE_VISUAL_TIME_LIMIT_SECONDS = 20.0


class EngineProbeVisualError(RuntimeError):
    def __init__(self, visual_probe: dict, observed_gpu_uuid: str):
        self.visual_probe = dict(visual_probe)
        self.observed_gpu_uuid = str(observed_gpu_uuid)
        metrics = " ".join(
            f"{name}={self.visual_probe.get(name)!r}"
            for name in (
                "sampled_pixels",
                "opaque_pixels",
                "mean_luma",
                "max_luma",
                "luma_range",
                "passed",
            )
        )
        super().__init__("controlled visual probe was blank or non-varying " + metrics)


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def visible_objects():
    return [
        obj
        for obj in bpy.context.scene.objects
        if obj.type in RENDER_TYPES and obj.visible_get() and not obj.hide_render
    ]


def bbox(objects):
    mins = Vector((math.inf, math.inf, math.inf))
    maxs = Vector((-math.inf, -math.inf, -math.inf))
    for obj in objects:
        for corner in obj.bound_box:
            p = obj.matrix_world @ Vector(corner)
            mins.x = min(mins.x, p.x)
            mins.y = min(mins.y, p.y)
            mins.z = min(mins.z, p.z)
            maxs.x = max(maxs.x, p.x)
            maxs.y = max(maxs.y, p.y)
            maxs.z = max(maxs.z, p.z)
    return mins, maxs


def is_explicit_presentation_helper(obj):
    name = str(obj.name_full).casefold()
    helper_tokens = (
        "image_carrier",
        "image carrier",
        "reference image",
        "reference_image",
        "参考图",
        "原图",
        "背景图",
    )
    if any(token in name for token in helper_tokens):
        return True
    dimensions = [abs(float(value)) for value in obj.dimensions]
    diagonal = max(sum(value * value for value in dimensions) ** 0.5, 0.000001)
    flatness = min(dimensions) / diagonal
    has_image_texture = any(
        node.type == "TEX_IMAGE" and getattr(node, "image", None) is not None
        for slot in obj.material_slots
        if slot.material and slot.material.use_nodes
        for node in slot.material.node_tree.nodes
    )
    return bool(
        re.fullmatch(r"\d+", name.strip()) and flatness < 0.01 and has_image_texture
    )


def is_named_framing_backdrop(obj):
    """Exclude authored studio shells from camera bounds, not from rendering."""

    name = str(obj.name_full).casefold().strip()
    return any(
        token in name
        for token in (
            "background",
            "backdrop",
            "cyclorama",
            "infinity wall",
            "studio wall",
            "摄影棚背景",
        )
    )


def is_volume_environment_object(obj):
    name = str(obj.name_full).casefold()
    if "volume scatter" in name or name.strip() in {"fog", "volume"}:
        return True
    for slot in obj.material_slots:
        material = slot.material
        if not material or not material.use_nodes:
            continue
        for node in material.node_tree.nodes:
            if node.type != "OUTPUT_MATERIAL":
                continue
            surface = node.inputs.get("Surface")
            volume = node.inputs.get("Volume")
            if volume and volume.is_linked and surface and not surface.is_linked:
                return True
    return False


def presentation_focus_objects(objects):
    """Return subject meshes without detached helpers or broad backdrops."""

    objects = [
        obj
        for obj in objects
        if not is_explicit_presentation_helper(obj)
        and not is_named_framing_backdrop(obj)
    ] or list(objects)
    explicit = [
        obj
        for obj in objects
        if bool(obj.get("video2blender_presentation_focus", False))
    ]
    if explicit:
        return explicit
    surface_objects = [obj for obj in objects if not is_volume_environment_object(obj)]
    if surface_objects:
        objects = surface_objects
    rows = []
    for obj in objects:
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        minimum = Vector(
            tuple(min(point[axis] for point in corners) for axis in range(3))
        )
        maximum = Vector(
            tuple(max(point[axis] for point in corners) for axis in range(3))
        )
        size = maximum - minimum
        rows.append(
            {
                "object": obj,
                "minimum": minimum,
                "maximum": maximum,
                "center": (minimum + maximum) * 0.5,
                "diagonal": max(float(size.length), 0.000001),
                "flatness": (
                    min(abs(float(size.x)), abs(float(size.y)), abs(float(size.z)))
                    / max(float(size.length), 0.000001)
                ),
            }
        )
    if len(rows) < 2:
        return list(objects)
    # A procedural subject is often made from hundreds of small, consistently
    # named pieces (beads, tiles, particles, petals).  Choosing the largest
    # individual volumetric object then frames an unrelated helper cube.  A
    # dominant repeated-name cluster is stronger evidence of the authored
    # subject than any one object's diagonal.
    prefix_groups = {}
    for row in rows:
        name = str(row["object"].name_full)
        prefix = re.sub(r"[\W_]*\d.*$", "", name, flags=re.UNICODE).strip(" ._-")
        if prefix:
            prefix_groups.setdefault(prefix.casefold(), []).append(row)
    repeated = sorted(
        (
            group
            for group in prefix_groups.values()
            if len(group) >= max(12, int(math.ceil(len(rows) * 0.20)))
        ),
        key=lambda group: (
            len(group),
            sum(float(row["diagonal"]) for row in group),
        ),
        reverse=True,
    )
    if repeated:
        core = repeated[0]
        core_minimum = Vector(
            tuple(min(float(row["minimum"][axis]) for row in core) for axis in range(3))
        )
        core_maximum = Vector(
            tuple(max(float(row["maximum"][axis]) for row in core) for axis in range(3))
        )
        core_size = core_maximum - core_minimum
        margin = Vector(
            tuple(max(abs(float(core_size[axis])) * 0.08, 0.05) for axis in range(3))
        )
        included = []
        for row in rows:
            center = row["center"]
            inside = all(
                float(core_minimum[axis] - margin[axis])
                <= float(center[axis])
                <= float(core_maximum[axis] + margin[axis])
                for axis in range(3)
            )
            if inside:
                included.append(row["object"])
        if len(included) >= len(core):
            return included
    diagonals = sorted(float(row["diagonal"]) for row in rows)
    median_diagonal = diagonals[len(diagonals) // 2]
    # Floors, backdrops and detached tutorial panels frequently dominate the
    # raw bounds while containing little subject volume.  Pick the primary
    # subject from genuinely volumetric meshes first.
    candidates = [
        row
        for row in rows
        if float(row["flatness"]) >= 0.02
        and float(row["diagonal"]) <= median_diagonal * 5.0
    ]
    primary = max(candidates or rows, key=lambda row: row["diagonal"])
    primary_radius = max(float(primary["diagonal"]) * 0.5, 0.5)
    included = []
    for row in rows:
        diagonal = float(row["diagonal"])
        distance = float((row["center"] - primary["center"]).length)
        flat_backdrop = (
            float(row["flatness"]) < 0.02
            and diagonal > float(primary["diagonal"]) * 1.4
        )
        detached = distance > primary_radius * 4.0 + min(diagonal, primary_radius) * 0.5
        if not flat_backdrop and not detached:
            included.append(row["object"])
    return included or list(objects)


def presentation_bbox(objects):
    """Ignore tiny, distant helper meshes when framing an edited source scene."""

    return bbox(presentation_focus_objects(objects))


def rw1_focus_identity_from_names(object_names):
    """Return one deterministic identity for the presentation-focus set."""

    names = sorted({str(name) for name in object_names if str(name)})
    canonical = json.dumps(
        names,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "focus_objects": names,
        "focus_object_count": len(names),
        "focus_identity_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def rw1_projection_frame_evidence(
    frame,
    projected_points,
    *,
    expected_corner_count,
    evaluation_error_count=0,
    safe_margin=RW1_FOCUS_PROJECTION_SAFE_MARGIN,
):
    """Describe raw, unclamped camera projection for one sampled frame."""

    safe_margin = float(safe_margin)
    expected_corner_count = int(expected_corner_count)
    evaluation_error_count = int(evaluation_error_count)
    valid_points = []
    nonfinite_corner_count = 0
    for raw_point in projected_points:
        try:
            point = tuple(float(value) for value in raw_point)
        except (TypeError, ValueError):
            nonfinite_corner_count += 1
            continue
        if len(point) != 3 or not all(math.isfinite(value) for value in point):
            nonfinite_corner_count += 1
            continue
        valid_points.append(point)
    front_points = [point for point in valid_points if point[2] > 0.0]
    behind_camera_corner_count = len(valid_points) - len(front_points)
    complete = bool(
        expected_corner_count > 0
        and evaluation_error_count == 0
        and nonfinite_corner_count == 0
        and len(valid_points) == expected_corner_count
        and len(front_points) == expected_corner_count
    )
    raw_bounds = {}
    overflow = {}
    safe_margin_violation = {}
    inside_safe_margin = False
    if front_points:
        minimum_x = min(point[0] for point in front_points)
        maximum_x = max(point[0] for point in front_points)
        minimum_y = min(point[1] for point in front_points)
        maximum_y = max(point[1] for point in front_points)
        minimum_z = min(point[2] for point in front_points)
        maximum_z = max(point[2] for point in front_points)
        raw_bounds = {
            "min_x": minimum_x,
            "max_x": maximum_x,
            "min_y": minimum_y,
            "max_y": maximum_y,
            "min_z": minimum_z,
            "max_z": maximum_z,
        }
        overflow = {
            "left": max(0.0, -minimum_x),
            "right": max(0.0, maximum_x - 1.0),
            "bottom": max(0.0, -minimum_y),
            "top": max(0.0, maximum_y - 1.0),
        }
        safe_margin_violation = {
            "left": max(0.0, safe_margin - minimum_x),
            "right": max(0.0, maximum_x - (1.0 - safe_margin)),
            "bottom": max(0.0, safe_margin - minimum_y),
            "top": max(0.0, maximum_y - (1.0 - safe_margin)),
        }
        inside_safe_margin = bool(
            complete
            and minimum_x >= safe_margin
            and maximum_x <= 1.0 - safe_margin
            and minimum_y >= safe_margin
            and maximum_y <= 1.0 - safe_margin
        )
    return {
        "frame": int(frame),
        "expected_corner_count": expected_corner_count,
        "projected_corner_count": len(valid_points),
        "front_camera_corner_count": len(front_points),
        "behind_camera_corner_count": behind_camera_corner_count,
        "nonfinite_corner_count": nonfinite_corner_count,
        "evaluation_error_count": evaluation_error_count,
        "raw_ndc_bounds": raw_bounds,
        "overflow": overflow,
        "safe_margin_violation": safe_margin_violation,
        "complete": complete,
        "inside_safe_margin": inside_safe_margin,
    }


def rw1_focus_projection_evidence(
    scene,
    camera,
    objects,
    sampled_frames,
    *,
    safe_margin=RW1_FOCUS_PROJECTION_SAFE_MARGIN,
):
    """Project evaluated focus geometry without clamping or extra renders."""

    focus_objects = list(objects)
    identity = rw1_focus_identity_from_names(
        str(obj.name_full) for obj in focus_objects
    )
    frames = sorted({int(frame) for frame in sampled_frames})
    records = []
    prior_frame = int(scene.frame_current)
    try:
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            projected_points = []
            evaluation_error_count = 0
            try:
                evaluated_camera = camera.evaluated_get(depsgraph)
            except Exception:
                evaluated_camera = camera
                evaluation_error_count += 1
            for obj in focus_objects:
                object_points = []
                try:
                    evaluated = obj.evaluated_get(depsgraph)
                    corners = list(evaluated.bound_box)
                    if len(corners) != 8:
                        raise ValueError("evaluated object has no eight-corner bounds")
                    for corner in corners:
                        point = world_to_camera_view(
                            scene,
                            evaluated_camera,
                            evaluated.matrix_world @ Vector(corner),
                        )
                        object_points.append(
                            (float(point.x), float(point.y), float(point.z))
                        )
                except Exception:
                    evaluation_error_count += 1
                    continue
                projected_points.extend(object_points)
            records.append(
                rw1_projection_frame_evidence(
                    frame,
                    projected_points,
                    expected_corner_count=len(focus_objects) * 8,
                    evaluation_error_count=evaluation_error_count,
                    safe_margin=safe_margin,
                )
            )
    finally:
        scene.frame_set(prior_frame)
    complete = bool(
        identity["focus_object_count"] > 0
        and frames
        and len(records) == len(frames)
        and all(record["complete"] for record in records)
    )
    return {
        "schema": RW1_FOCUS_PROJECTION_EVIDENCE_SCHEMA,
        "projection_space": "world_to_camera_view_raw_ndc",
        "geometry_source": "evaluated_object_bound_box",
        "camera": str(camera.name_full),
        "safe_margin": round(float(safe_margin), 8),
        **identity,
        "sampled_frames": frames,
        "sample_count": len(frames),
        "frames": records,
        "complete": complete,
        "all_samples_inside_safe_margin": bool(
            complete and all(record["inside_safe_margin"] for record in records)
        ),
    }


def attach_rw1_focus_projection_evidence(
    out_dir,
    scene,
    camera,
    objects,
    sampled_frames,
):
    """Append fail-closed focus projection evidence to an RW1 receipt."""

    path = Path(out_dir) / "presentation_camera.json"
    if not path.is_file() or not objects:
        return
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "video2blender.presentation-camera.v1":
        raise RuntimeError("RW1 presentation camera receipt is malformed")
    receipt["focus_projection_evidence"] = rw1_focus_projection_evidence(
        scene,
        camera,
        objects,
        sampled_frames,
    )
    atomic_write_json(path, receipt)


def rw1_static_interchange_framing_records(objects):
    """Describe render objects without mutating the imported source scene."""

    records = []
    for obj in objects:
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        minimum = tuple(
            min(float(point[axis]) for point in corners) for axis in range(3)
        )
        maximum = tuple(
            max(float(point[axis]) for point in corners) for axis in range(3)
        )
        parent = obj.parent
        while parent is not None and parent.parent is not None:
            parent = parent.parent
        data = getattr(obj, "data", None)
        vertices = getattr(data, "vertices", ()) or ()
        polygons = getattr(data, "polygons", ()) or ()
        records.append(
            {
                "name": str(obj.name_full),
                "minimum": minimum,
                "maximum": maximum,
                "vertex_count": len(vertices),
                "face_count": len(polygons),
                "assembly_key": (str(parent.name_full) if parent is not None else ""),
                "explicit_helper": is_explicit_presentation_helper(obj),
                "named_backdrop": is_named_framing_backdrop(obj),
            }
        )
    return records


def rw1_static_interchange_camera_bounds(
    objects,
    legacy_minimum: Vector,
    legacy_maximum: Vector,
    legacy_ortho_scale: float,
):
    """Return v3-only generated-camera bounds and an auditable receipt."""

    # Keep the helper isolated to static-interchange framing. Other generation
    # routes must remain deployable with their existing runtime script sets.
    from rw1_static_interchange_framing import (
        build_camera_plan as build_static_interchange_camera_plan,
    )

    plan = build_static_interchange_camera_plan(
        rw1_static_interchange_framing_records(objects),
        legacy_minimum=tuple(float(value) for value in legacy_minimum),
        legacy_maximum=tuple(float(value) for value in legacy_maximum),
        legacy_ortho_scale=legacy_ortho_scale,
        direction=RW1_STATIC_ISO_DIRECTION,
        enabled=True,
    )
    selected_names = set(plan["selected_names"])
    selected_objects = [obj for obj in objects if str(obj.name_full) in selected_names]
    if not selected_objects:
        raise RuntimeError("RW1 static interchange framing selected no source geometry")
    minimum = Vector(plan["minimum"])
    maximum = Vector(plan["maximum"])
    center = (minimum + maximum) * 0.5
    size = maximum - minimum
    receipt = {
        key: value for key, value in plan.items() if key not in {"minimum", "maximum"}
    }
    receipt.update(
        {
            "minimum": [round(float(value), 6) for value in minimum],
            "maximum": [round(float(value), 6) for value in maximum],
            "center": [round(float(value), 6) for value in center],
        }
    )
    return {
        "center": center,
        "radius": max(float(size.length) * 0.5, 0.5),
        "ortho_scale": float(plan["ortho_scale"]),
        "minimum": minimum,
        "maximum": maximum,
        "objects": selected_objects,
        "receipt": receipt,
    }


def rw1_dynamic_camera_bounds(
    camera_mode: str,
    objects,
    source_frames,
    center: Vector,
    radius: float,
    ortho_scale: float,
    min_z: float,
):
    """Use temporal subject bounds only for an RW1 generated camera."""

    if not str(camera_mode).startswith("generated_"):
        return {
            "center": center,
            "radius": radius,
            "ortho_scale": ortho_scale,
            "min_z": min_z,
            "temporal_bounds_applied": False,
            "evidence": {},
        }
    frame_window = tuple(sorted({int(frame) for frame in source_frames}))
    if not frame_window:
        frame_window = (int(bpy.context.scene.frame_current),)
    sampled_frames = sample_source_frames(
        frame_window[0],
        frame_window[-1],
        min(
            RW1_DYNAMIC_GENERATED_CAMERA_BOUNDS_SAMPLES,
            frame_window[-1] - frame_window[0] + 1,
        ),
    )
    scene = bpy.context.scene
    prior_frame = int(scene.frame_current)
    mins = Vector((math.inf, math.inf, math.inf))
    maxs = Vector((-math.inf, -math.inf, -math.inf))
    try:
        for frame in sampled_frames:
            scene.frame_set(frame)
            sample_mins, sample_maxs = bbox(objects)
            for axis in range(3):
                mins[axis] = min(mins[axis], sample_mins[axis])
                maxs[axis] = max(maxs[axis], sample_maxs[axis])
    finally:
        scene.frame_set(prior_frame)
    temporal_center = (mins + maxs) * 0.5
    size = maxs - mins
    temporal_radius = (
        max(float(size.length) * 0.5, 0.5) * RW1_DYNAMIC_GENERATED_CAMERA_SAFE_MARGIN
    )
    temporal_ortho_scale = (
        max(abs(float(size[axis])) for axis in range(3)) * 1.35 or 2.0
    ) * RW1_DYNAMIC_GENERATED_CAMERA_SAFE_MARGIN
    evidence = {
        "temporal_bounds_applied": True,
        "sampled_source_frames": list(sampled_frames),
        "source_frame_window": [frame_window[0], frame_window[-1]],
        "safe_margin": RW1_DYNAMIC_GENERATED_CAMERA_SAFE_MARGIN,
        "center": [round(float(temporal_center[axis]), 6) for axis in range(3)],
        "radius": round(temporal_radius, 6),
        "ortho_scale": round(temporal_ortho_scale, 6),
    }
    return {
        "center": temporal_center,
        "radius": temporal_radius,
        "ortho_scale": temporal_ortho_scale,
        "min_z": float(mins[2]),
        "temporal_bounds_applied": True,
        "evidence": evidence,
    }


def create_rw1_dynamic_generated_camera(profile: str, camera_bounds):
    """Create one delivery-only camera from audited temporal bounds."""

    cam = create_camera(
        camera_bounds["center"],
        camera_bounds["radius"],
        camera_bounds["ortho_scale"],
    )
    cam.data.type = (
        "PERSP" if profile in {"cinematic_scene", "character_loop"} else "ORTHO"
    )
    if cam.data.type != "ORTHO":
        cam.data.lens = 58
    height_factor = 0.72 if profile in {"cinematic_scene", "character_loop"} else 1.05
    cam.location = camera_bounds["center"] + Vector(
        (0.95, -2.35, height_factor)
    ).normalized() * camera_bounds["radius"] * (
        2.45 if cam.data.type == "PERSP" else 2.8
    )
    look_at(cam, camera_bounds["center"])
    camera_distance = float((cam.location - camera_bounds["center"]).length)
    framing_radius = float(camera_bounds["radius"])
    required_clip_end = camera_distance + framing_radius
    if (
        not math.isfinite(camera_distance)
        or camera_distance <= 0.0
        or not math.isfinite(framing_radius)
        or framing_radius <= 0.0
        or not math.isfinite(required_clip_end)
        or required_clip_end <= 0.0
    ):
        raise RuntimeError("RW1_DYNAMIC_GENERATED_CAMERA_CLIP_BOUNDS_INVALID")
    cam.data.clip_end = max(
        float(cam.data.clip_end),
        required_clip_end,
    )
    evidence = camera_bounds.get("evidence")
    if isinstance(evidence, dict):
        evidence.update(
            {
                "camera_distance": round(camera_distance, 6),
                "clip_end": round(float(cam.data.clip_end), 6),
            }
        )
    return cam


def rw1_source_focus_objects(objects):
    """Prefer the animated asset rig over tutorial helpers and backdrops."""

    def ancestor_chain(obj):
        chain = []
        parent = obj.parent
        while parent is not None:
            chain.append(parent)
            parent = parent.parent
        return chain

    renderable = list(objects)
    armatures = [
        obj
        for obj in bpy.context.scene.objects
        if obj.type == "ARMATURE"
        and (
            obj.animation_data is not None
            and (
                obj.animation_data.action is not None
                or any(track.strips for track in obj.animation_data.nla_tracks)
            )
        )
    ]
    ranked = []
    for armature in armatures:
        descendants = [
            obj
            for obj in objects
            if any(parent == armature for parent in ancestor_chain(obj))
        ]
        if descendants:
            ranked.append(
                (
                    len(descendants),
                    sum(max(float(obj.dimensions.length), 0.0) for obj in descendants),
                    descendants,
                )
            )
    if ranked:
        return max(ranked, key=lambda row: (row[0], row[1]))[2]
    animated = []
    for obj in objects:
        chain = [obj, *ancestor_chain(obj)]
        if any(
            owner.animation_data is not None
            and (
                owner.animation_data.action is not None
                or any(track.strips for track in owner.animation_data.nla_tracks)
            )
            for owner in chain
        ):
            animated.append(obj)
    return animated or renderable


def look_at(obj, target: Vector) -> None:
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def ensure_light(
    center: Vector,
    radius: float,
    *,
    force_source_presentation_repair: bool = False,
    recenter_delivery_rig: bool = False,
) -> None:
    # RW1 native source rendering is a fidelity path.  A scene that authors
    # its illumination through the world, emissive materials or compositor
    # must not acquire a generic three-light rig merely because it has no
    # LIGHT objects.
    if preserve_source_render_settings():
        if not force_source_presentation_repair:
            return
    existing = [
        obj
        for obj in bpy.context.scene.objects
        if obj.type == "LIGHT" and obj.visible_get() and not obj.hide_render
    ]
    supplement = os.environ.get("VIDEO2BLENDER_SUPPLEMENT_SOURCE_LIGHTS", "0") == "1"
    if existing and not supplement and not force_source_presentation_repair:
        return
    scale = float(os.environ.get("VIDEO2BLENDER_LIGHT_SCALE", "1.0"))
    # Interchange files can be authored in centimetres or as a broad asset
    # sheet. Because this delivery rig is positioned and sized from the scene
    # radius, fixed power becomes effectively black for those imports. Keep
    # the correction opt-in so native Blend scenes remain untouched.
    if os.environ.get("VIDEO2BLENDER_SCALE_LIGHTS_WITH_SCENE", "0") == "1":
        scale *= max(1.0, min(float(radius), 48.0))
    base_lights = [
        ("View_Key", Vector((2.8, -3.2, 3.2)), 800),
        ("View_Fill", Vector((-3.0, 2.4, 2.2)), 260),
        ("View_Rim", Vector((-2.2, -1.8, 2.8)), 420),
    ]
    supplement_lights = [
        ("View_Supplement_Key", Vector((2.8, -3.2, 3.2)), 320),
        ("View_Supplement_Fill", Vector((-3.0, 2.4, 2.2)), 120),
        ("View_Supplement_Rim", Vector((-2.2, -1.8, 2.8)), 180),
    ]
    if recenter_delivery_rig:
        lights = (
            base_lights
            if any(bpy.data.objects.get(row[0]) for row in base_lights)
            else supplement_lights
        )
    else:
        lights = supplement_lights if existing else base_lights
    for name, offset, energy in lights:
        prior = bpy.data.objects.get(name)
        if prior is not None and prior.type == "LIGHT":
            if recenter_delivery_rig:
                prior.location = center + offset * radius
                prior.data.energy = energy * scale
                prior.data.size = max(radius * 2.2, 2.0)
                look_at(prior, center)
            continue
        data = bpy.data.lights.new(name, "AREA")
        obj = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(obj)
        obj.location = center + offset * radius
        data.energy = energy * scale
        data.size = max(radius * 2.2, 2.0)
        look_at(obj, center)


def expected_gpu_uuid() -> str:
    value = os.environ.get("TOTAL_ASSET_EXPECTED_GPU_UUID", "").strip()
    if GPU_UUID_RE.fullmatch(value) is None:
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
            "expected_gpu_uuid_missing_or_invalid"
        )
    return value.lower()


def current_process_gpu_uuids(
    *, attempts: int = 6, retry_seconds: float = 0.25
) -> list[str]:
    """Return physical GPUs that currently own this exact Blender PID."""

    pid = str(os.getpid())
    observed: set[str] = set()
    for attempt in range(max(1, attempts)):
        try:
            compute = subprocess.run(
                [
                    NVIDIA_SMI,
                    "--query-compute-apps=gpu_uuid,pid",
                    "--format=csv,noheader,nounits",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            if compute.returncode == 0:
                observed.update(
                    parts[0].strip().lower()
                    for line in compute.stdout.splitlines()
                    for parts in [line.split(",", 1)]
                    if len(parts) == 2 and parts[1].strip() == pid
                )
            inventory = subprocess.run(
                [
                    NVIDIA_SMI,
                    "--query-gpu=index,uuid",
                    "--format=csv,noheader,nounits",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            pmon = subprocess.run(
                [NVIDIA_SMI, "pmon", "-c", "1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            index_to_uuid = {
                parts[0].strip(): parts[1].strip().lower()
                for line in inventory.stdout.splitlines()
                for parts in [line.split(",", 1)]
                if inventory.returncode == 0 and len(parts) == 2
            }
            observed.update(
                index_to_uuid[parts[0]]
                for line in pmon.stdout.splitlines()
                for parts in [line.split()]
                if (
                    pmon.returncode == 0
                    and len(parts) >= 2
                    and not line.lstrip().startswith("#")
                    and parts[1] == pid
                    and parts[0] in index_to_uuid
                )
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if observed:
            break
        if attempt + 1 < attempts:
            time.sleep(max(0.0, retry_seconds))
    return sorted(observed)


def attest_current_process_gpu() -> str:
    expected = expected_gpu_uuid()
    marker = os.environ.get("VIDEO2BLENDER_GPU_PROCESS_ATTESTED", "").strip()
    marker_uuid = (
        os.environ.get("VIDEO2BLENDER_GPU_PROCESS_ATTESTED_UUID", "").strip().lower()
    )
    if marker != "1" or marker_uuid != expected:
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
            f"expected_gpu_uuid={expected} "
            "process_monitor_marker="
            f"{marker or 'missing'} "
            "process_monitor_uuid="
            f"{marker_uuid or 'missing'}"
        )
    return expected


def configure_cycles_gpu_exact(scene, backend: str) -> dict:
    """Enable exactly one visible GPU and explicitly disable every CPU."""

    prefs = bpy.context.preferences.addons.get("cycles")
    if prefs is None:
        raise RuntimeError("Cycles addon is unavailable")
    cprefs = prefs.preferences
    normalized = str(backend or "").strip().upper()
    if normalized not in {"CUDA", "OPTIX"}:
        raise RuntimeError(f"unsupported Cycles backend: {normalized}")
    cprefs.compute_device_type = normalized
    cprefs.get_devices()
    devices = list(cprefs.devices)
    gpu_devices = [
        device
        for device in devices
        if str(getattr(device, "type", "")).upper() == normalized
    ]
    if len(gpu_devices) != 1:
        raise RuntimeError(
            f"{normalized.lower()}_gpu_count={len(gpu_devices)} expected=1"
        )
    selected = gpu_devices[0]
    for device in devices:
        device.use = device is selected
    enabled_non_cpu = [
        device
        for device in devices
        if bool(getattr(device, "use", False))
        and str(getattr(device, "type", "")).upper() != "CPU"
    ]
    cpu_enabled = any(
        bool(getattr(device, "use", False))
        for device in devices
        if str(getattr(device, "type", "")).upper() == "CPU"
    )
    if enabled_non_cpu != [selected] or cpu_enabled:
        raise RuntimeError("enabled device set is not exactly one GPU")
    scene.cycles.device = "GPU"
    return {
        "backend": normalized,
        "device_set_exact": True,
        "cpu_device_enabled": False,
        "devices": [
            {
                "name": str(getattr(device, "name", "")),
                "type": str(getattr(device, "type", "")),
                "use": bool(getattr(device, "use", False)),
            }
            for device in devices
        ],
    }


def configure_verified_cycles_gpu(scene) -> bool:
    """Reapply the externally probed backend without permitting CPU fallback."""

    global _POSTPROCESS_CYCLES_BACKEND
    if os.environ.get("VIDEO2BLENDER_POSTPROCESS_CYCLES_GPU_VERIFIED", "0") != "1":
        return False
    backend = os.environ.get("VIDEO2BLENDER_CYCLES_BACKEND", "CUDA").strip().upper()
    try:
        configure_cycles_gpu_exact(scene, backend)
    except Exception:
        return False
    _POSTPROCESS_CYCLES_BACKEND = backend
    return True


def preserve_source_render_settings() -> bool:
    return (
        os.environ.get(
            "VIDEO2BLENDER_PRESERVE_SOURCE_RENDER_SETTINGS",
            "0",
        )
        == "1"
    )


def rw1_authored_camera_projection_usable(
    coverage: float,
    *,
    route: str,
) -> bool:
    """Require product-scale framing before preserving an authored camera."""

    threshold = (
        RW1_DYNAMIC_AUTHORED_CAMERA_MIN_COVERAGE
        if route == "dynamic"
        else RW1_STATIC_AUTHORED_CAMERA_MIN_COVERAGE
    )
    return float(coverage) >= threshold


def _rw1_scene_has_undefined_shader_nodes() -> bool:
    """Fail closed before mapping a legacy Eevee delivery scene."""

    node_trees = []
    for owner in (*bpy.data.materials, *bpy.data.worlds):
        tree = getattr(owner, "node_tree", None)
        if tree is not None:
            node_trees.append(tree)
    node_trees.extend(bpy.data.node_groups)
    for tree in node_trees:
        for node in tree.nodes:
            if str(getattr(node, "type", "")).upper() == "UNDEFINED" or str(
                getattr(node, "bl_idname", "")
            ) in {"NodeUndefined", "ShaderNodeUndefined"}:
                return True
    return False


def _set_preserved_eevee_engine(scene, authored: str) -> tuple[str, bool]:
    """Preserve legacy Eevee when available, otherwise map delivery to Next.

    Blender 4.3+ removed the ``BLENDER_EEVEE`` enum.  Assignment itself is
    the authoritative runtime compatibility probe.  The fallback is allowed
    only when every shader node is available and Eevee Next accepts the
    scene; otherwise the worker records a blocked dependency instead of
    silently switching to another renderer.
    """

    legacy = authored in {"BLENDER_EEVEE", "EEVEE"}
    requested = "BLENDER_EEVEE" if legacy else "BLENDER_EEVEE_NEXT"
    try:
        scene.render.engine = requested
        return str(scene.render.engine), False
    except (TypeError, ValueError):
        if not legacy or _rw1_scene_has_undefined_shader_nodes():
            raise RuntimeError(
                f"RW1_SOURCE_LEGACY_EEVEE_INCOMPATIBLE authored_engine={authored}"
            )
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"RW1_SOURCE_LEGACY_EEVEE_INCOMPATIBLE authored_engine={authored}"
        ) from exc
    if str(scene.render.engine).upper() != "BLENDER_EEVEE_NEXT":
        raise RuntimeError(
            "RW1_SOURCE_LEGACY_EEVEE_INCOMPATIBLE "
            f"authored_engine={authored} mapped_engine={scene.render.engine}"
        )
    return "BLENDER_EEVEE_NEXT", True


def disable_scene_compositing_if_requested(scene) -> bool:
    disabled = (
        os.environ.get(
            "VIDEO2BLENDER_DISABLE_SCENE_COMPOSITING",
            "0",
        )
        == "1"
    )
    if disabled:
        scene.render.use_compositing = False
    return disabled


def set_render_engine(scene) -> str:
    global _POSTPROCESS_ENGINE, _POSTPROCESS_ENGINE_POLICY
    if _POSTPROCESS_ENGINE:
        scene.render.engine = _POSTPROCESS_ENGINE
        # Cycles device state is scene-local and can be restored by an
        # authored file or a temporary probe scene.  Re-apply the exact CUDA
        # selection for every production frame instead of trusting the
        # process-wide probe completed before the real scene was rendered.
        if str(_POSTPROCESS_ENGINE).upper() == "CYCLES":
            if not configure_verified_cycles_gpu(scene):
                raise RuntimeError(
                    "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                    "cached Cycles engine lost its exact GPU binding"
                )
        return _POSTPROCESS_ENGINE
    if preserve_source_render_settings():
        authored = str(scene.render.engine or "").strip().upper()
        if authored == "CYCLES":
            if not configure_verified_cycles_gpu(scene):
                raise RuntimeError(
                    "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                    "authored Cycles scene has no exact reusable GPU proof"
                )
            _POSTPROCESS_ENGINE_POLICY = "rw1_source_authored_engine_preserved"
        elif authored in {
            "BLENDER_EEVEE_NEXT",
            "BLENDER_EEVEE",
            "EEVEE",
        }:
            requested = (
                os.environ.get(
                    "VIDEO2BLENDER_POSTPROCESS_ENGINE",
                    os.environ.get("VIDEO2BLENDER_RENDER_ENGINE", "CYCLES"),
                )
                .strip()
                .upper()
            )
            if requested == "CYCLES":
                # Background Eevee can silently fall back to software after
                # a successful CUDA probe (the tell-tale runtime symptom is
                # EGL_BAD_MATCH plus a many-core Blender process and no GPU
                # PID).  Keep the packed asset's authored engine untouched,
                # but use the already-attested Cycles backend for the
                # in-memory delivery scene. Camera, world, colour management
                # and authored materials remain preserved by the RW1 flag.
                if not configure_verified_cycles_gpu(scene):
                    raise RuntimeError(
                        "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                        "authored Eevee delivery has no exact CUDA mapping"
                    )
                scene.render.engine = "CYCLES"
                _POSTPROCESS_ENGINE_POLICY = (
                    "rw1_source_eevee_delivery_mapped_to_exact_cuda_cycles"
                )
                authored = "CYCLES"
            else:
                selected, compatibility_mapped = _set_preserved_eevee_engine(
                    scene,
                    authored,
                )
                _POSTPROCESS_ENGINE_POLICY = (
                    "rw1_source_legacy_eevee_delivery_mapped_to_next"
                    if compatibility_mapped
                    else "rw1_source_authored_engine_preserved"
                )
                authored = selected
        else:
            raise RuntimeError(
                "RW1_SOURCE_RENDER_ENGINE_UNSUPPORTED "
                f"authored_engine={authored or 'empty'}"
            )
        _POSTPROCESS_ENGINE = str(scene.render.engine)
        return _POSTPROCESS_ENGINE
    requested = (
        os.environ.get(
            "VIDEO2BLENDER_POSTPROCESS_ENGINE",
            os.environ.get("VIDEO2BLENDER_RENDER_ENGINE", "CYCLES"),
        )
        .strip()
        .upper()
    )
    if requested == "CYCLES":
        if not configure_verified_cycles_gpu(scene):
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                "Cycles was selected without an exact reusable GPU proof"
            )
        scene.render.engine = "CYCLES"
        _POSTPROCESS_ENGINE_POLICY = "externally_attested_cycles_gpu"
    elif requested in {"BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "EEVEE"}:
        try:
            scene.render.engine = (
                "BLENDER_EEVEE"
                if requested == "BLENDER_EEVEE"
                else "BLENDER_EEVEE_NEXT"
            )
        except (TypeError, ValueError):
            scene.render.engine = "BLENDER_EEVEE"
        _POSTPROCESS_ENGINE_POLICY = "externally_attested_eevee_gpu"
    else:
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
            f"unsupported_selected_engine={requested or 'empty'}"
        )
    _POSTPROCESS_ENGINE = scene.render.engine
    return _POSTPROCESS_ENGINE


def setup_render(
    output: Path, resolution_x: int, resolution_y: int, samples: int, transparent: bool
) -> bool:
    scene = bpy.context.scene
    engine = set_render_engine(scene)
    preserve_authored = preserve_source_render_settings()
    if engine == "CYCLES":
        if preserve_authored:
            # Keep the source engine, camera, world, colour management and
            # authored asset untouched, but bound the delivery render cost.
            # Some downloaded projects ship with 4K+ Cycles samples; using
            # those settings for every frame of a 4 s preview can outlive the
            # six-hour GPU lease even though --samples already requested a
            # production-safe cap.  This changes only the in-memory delivery
            # scene after asset.blend has been packed and saved.
            scene.cycles.samples = min(
                max(1, int(scene.cycles.samples)),
                max(1, int(samples)),
            )
        else:
            scene.cycles.samples = samples
        scene.cycles.use_denoising = True
    if not preserve_authored:
        if engine != "CYCLES":
            # Blender 4.x removed several legacy ``scene.eevee`` sampling
            # properties.  Keep the native Eevee Next defaults there instead
            # of raising before the fail-safe engine can render anything.
            eevee = getattr(scene, "eevee", None)
            if eevee is not None:
                for name in ("taa_render_samples", "taa_samples"):
                    if hasattr(eevee, name):
                        setattr(eevee, name, max(samples, 1))
                        break
    scene.render.resolution_x = resolution_x
    scene.render.resolution_y = resolution_y
    # Authored files frequently keep a non-100% preview percentage.  Preserve
    # their camera, engine, world, compositor and colour management, but make
    # the delivery contract's pixel dimensions exact.
    scene.render.resolution_percentage = 100
    if not preserve_authored:
        scene.render.film_transparent = transparent
        for view_transform in ("AgX", "Filmic", "Standard"):
            try:
                scene.view_settings.view_transform = view_transform
                break
            except (TypeError, ValueError):
                pass
        for look in ("AgX - Medium High Contrast", "Medium High Contrast", "None"):
            try:
                scene.view_settings.look = look
                break
            except (TypeError, ValueError):
                pass
        scene.view_settings.exposure = float(
            os.environ.get("VIDEO2BLENDER_EXPOSURE", "0.15")
        )
        scene.view_settings.gamma = 1.0
    still_output = True
    try:
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGBA" if transparent else "RGB"
        scene.render.filepath = str(output)
    except TypeError:
        still_output = False
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.ffmpeg.constant_rate_factor = "HIGH"
        scene.render.filepath = str(output.with_suffix(".mp4"))
    return still_output


def render_result_visual_probe(
    *,
    controlled: bool,
    render_path: Path | None = None,
) -> dict:
    loaded_from_disk = False
    if render_path is not None:
        try:
            image = bpy.data.images.load(
                str(render_path),
                check_existing=False,
            )
            loaded_from_disk = True
        except Exception as exc:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_OUTPUT_BLACK "
                f"render_file_unreadable={type(exc).__name__}"
            ) from exc
    else:
        image = bpy.data.images.get("Render Result")
        if image is None:
            raise RuntimeError("VIDEO_REPLAY_RENDER_OUTPUT_BLACK render_result_missing")
    try:
        values = list(image.pixels)
    finally:
        if loaded_from_disk:
            bpy.data.images.remove(image)
    if controlled:
        result = analyze_rgba_pixels(values)
    else:
        # The production gate rejects only an effectively empty framebuffer;
        # detailed exposure and composition remain the downstream QA's job.
        result = analyze_rgba_pixels(
            values,
            minimum_mean_luma=0.0002,
            minimum_max_luma=0.002,
            minimum_luma_range=0.0001,
        )
    return result.to_dict()


def attest_production_render(render_path: Path) -> None:
    global _POSTPROCESS_GPU_ATTESTED, _POSTPROCESS_OUTPUT_ATTESTED
    # Validate the actual published frame. Blender 5.1 background mode can
    # expose an empty in-memory Render Result after a valid disk render.
    visual = render_result_visual_probe(
        controlled=False,
        render_path=render_path,
    )
    if visual["passed"] is not True:
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_OUTPUT_BLACK "
            f"engine={bpy.context.scene.render.engine} "
            f"mean_luma={visual['mean_luma']} "
            f"max_luma={visual['max_luma']} "
            f"luma_range={visual['luma_range']}"
        )
    _POSTPROCESS_OUTPUT_ATTESTED = True
    if str(bpy.context.scene.render.engine).upper() == "CYCLES":
        # The injected monitor observes this Blender PID while Cycles owns its
        # CUDA context and only publishes the marker after a stable exact-UUID
        # handshake.  Once bpy.ops.render returns, Cycles may release that
        # context immediately; querying nvidia-smi here then produces an empty
        # set even though the frame was rendered on the expected card.  Keep
        # the durable in-process handshake as the production fact.  Device
        # selection is independently re-applied immediately before every
        # frame by set_render_engine().
        attest_current_process_gpu()
        _POSTPROCESS_GPU_ATTESTED = True
    elif not _POSTPROCESS_GPU_ATTESTED:
        attest_current_process_gpu()
        _POSTPROCESS_GPU_ATTESTED = True


def render_current_frame(
    output: Path, resolution_x: int, resolution_y: int, samples: int, transparent: bool
) -> None:
    scene = bpy.context.scene
    still_output = setup_render(
        output, resolution_x, resolution_y, samples, transparent
    )
    if still_output:
        bpy.ops.render.render(write_still=True)
        attest_production_render(output)
        return
    clip = output.with_suffix(".mp4")
    original_start, original_end = scene.frame_start, scene.frame_end
    scene.frame_start = scene.frame_current
    scene.frame_end = scene.frame_current
    scene.render.filepath = str(clip)
    bpy.ops.render.render(animation=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(clip), "-frames:v", "1", str(output)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    attest_production_render(output)
    clip.unlink(missing_ok=True)
    scene.frame_start, scene.frame_end = original_start, original_end


def disable_delivery_only_compositor_file_outputs(scene) -> list[str]:
    """Disable source side-channel writes without changing final Composite.

    Authored projects often retain File Output sinks for EXR passes, masks or
    debugging frames.  Those sinks are not part of the RW1 ISO/MP4 contract
    and can multiply per-frame compositing time and disk writes.  Disconnect
    only those sink inputs in the in-memory delivery scene; the packed source
    asset was saved before this renderer starts and remains unchanged.
    """

    tree = getattr(scene, "node_tree", None)
    if not bool(getattr(scene, "use_nodes", False)) or tree is None:
        return []
    disabled = []
    for node in list(tree.nodes):
        if str(getattr(node, "bl_idname", "")) != "CompositorNodeOutputFile":
            continue
        for socket in list(getattr(node, "inputs", ())):
            for link in list(getattr(socket, "links", ())):
                tree.links.remove(link)
        node.mute = True
        disabled.append(str(getattr(node, "name", "File Output")))
    return disabled


def _probe_cube_mesh(name: str):
    mesh = bpy.data.meshes.new(name)
    vertices = [
        (-0.5, -0.5, -0.5),
        (0.5, -0.5, -0.5),
        (0.5, 0.5, -0.5),
        (-0.5, 0.5, -0.5),
        (-0.5, -0.5, 0.5),
        (0.5, -0.5, 0.5),
        (0.5, 0.5, 0.5),
        (-0.5, 0.5, 0.5),
    ]
    faces = [
        (0, 3, 2, 1),
        (4, 5, 6, 7),
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (4, 7, 3, 0),
    ]
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return mesh


def _probe_scene(name: str):
    scene = bpy.data.scenes.new(name)
    scene.render.resolution_x = ENGINE_PROBE_COMPUTE_RESOLUTION
    scene.render.resolution_y = ENGINE_PROBE_COMPUTE_RESOLUTION
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.world = bpy.data.worlds.new(name + "_World")
    scene.world.color = (0.015, 0.025, 0.05)

    mesh = _probe_cube_mesh(name + "_Mesh")
    for index, color in enumerate(((0.8, 0.05, 0.02, 1.0), (0.02, 0.35, 0.8, 1.0))):
        obj = bpy.data.objects.new(f"{name}_Cube_{index}", mesh.copy())
        obj.location.x = -0.72 if index == 0 else 0.72
        scene.collection.objects.link(obj)
        material = bpy.data.materials.new(f"{name}_Material_{index}")
        material.diffuse_color = color
        material.use_nodes = True
        bsdf = material.node_tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = color
            bsdf.inputs["Roughness"].default_value = 0.42
        obj.data.materials.append(material)

    camera_data = bpy.data.cameras.new(name + "_Camera")
    camera = bpy.data.objects.new(name + "_Camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (0.0, -5.5, 1.2)
    look_at(camera, Vector((0.0, 0.0, 0.0)))
    camera_data.lens = 52
    scene.camera = camera

    light_data = bpy.data.lights.new(name + "_Key", "AREA")
    light = bpy.data.objects.new(name + "_Key", light_data)
    scene.collection.objects.link(light)
    light.location = (1.8, -2.8, 3.6)
    light_data.energy = 900
    light_data.size = 4.0
    look_at(light, Vector((0.0, 0.0, 0.0)))
    return scene


def engine_probe_phase_parameters(backend: str = "CUDA") -> dict:
    return {
        "compute_attestation": {
            "resolution_x": ENGINE_PROBE_COMPUTE_RESOLUTION,
            "resolution_y": ENGINE_PROBE_COMPUTE_RESOLUTION,
            "sample_cap": ENGINE_PROBE_COMPUTE_SAMPLE_CAP,
            "time_limit_seconds": ENGINE_PROBE_COMPUTE_TIME_LIMIT_SECONDS,
            "adaptive_sampling": False,
        },
        "visual_verification": {
            "resolution_x": ENGINE_PROBE_VISUAL_RESOLUTION,
            "resolution_y": ENGINE_PROBE_VISUAL_RESOLUTION,
            "samples": ENGINE_PROBE_VISUAL_SAMPLES,
            "time_limit_seconds": ENGINE_PROBE_VISUAL_TIME_LIMIT_SECONDS,
            "reused_cycles_backend": backend,
        },
    }


def _run_controlled_engine_probe(engine: str, *, backend: str = "") -> dict:
    name = "VideoReplayEngineProbe_" + uuid_safe_name(engine + backend)
    scene = _probe_scene(name)
    window = bpy.context.window
    previous = window.scene if window is not None else None
    try:
        if window is not None:
            window.scene = scene
        if engine != "CYCLES" or backend != "CUDA":
            raise RuntimeError("controlled probe requires CYCLES/CUDA")
        scene.render.engine = "CYCLES"
        if not hasattr(scene.cycles, "time_limit") or not hasattr(
            scene.cycles, "use_adaptive_sampling"
        ):
            raise RuntimeError("Cycles bounded probe controls are unavailable")
        scene.cycles.samples = ENGINE_PROBE_COMPUTE_SAMPLE_CAP
        scene.cycles.time_limit = ENGINE_PROBE_COMPUTE_TIME_LIMIT_SECONDS
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.use_denoising = False
        device = configure_cycles_gpu_exact(scene, backend)

        # Phase 1 is deliberately compute-heavy so the independent process
        # monitor can persist three exact PID-to-UUID CUDA samples.  Its
        # time-limited Render Result is not accepted as visual evidence.
        bpy.ops.render.render(scene=scene.name)
        observed = attest_current_process_gpu()

        # Phase 2 reuses the same scene, Cycles engine, and already configured
        # CUDA device set.  A small bounded render replaces the time-limited
        # phase-1 result before the independent visual proof is evaluated.
        scene.render.resolution_x = ENGINE_PROBE_VISUAL_RESOLUTION
        scene.render.resolution_y = ENGINE_PROBE_VISUAL_RESOLUTION
        scene.cycles.samples = ENGINE_PROBE_VISUAL_SAMPLES
        scene.cycles.time_limit = ENGINE_PROBE_VISUAL_TIME_LIMIT_SECONDS
        # Blender 5.1 background mode can leave the in-memory ``Render Result``
        # pixel collection empty when rendering a newly-created non-main
        # scene, even though the render itself completed.  Prove the delivered
        # pixels through a real PNG written by that exact CYCLES/CUDA scene.
        # The temporary file is never a production artifact.
        with tempfile.TemporaryDirectory(
            prefix="video2blender-engine-probe-"
        ) as temporary_root:
            visual_path = Path(temporary_root) / "visual_probe.png"
            scene.render.image_settings.file_format = "PNG"
            scene.render.image_settings.color_mode = "RGBA"
            scene.render.filepath = str(visual_path)
            bpy.ops.render.render(
                scene=scene.name,
                write_still=True,
            )
            loaded = None
            try:
                if visual_path.is_file() and visual_path.stat().st_size > 0:
                    loaded = bpy.data.images.load(
                        str(visual_path),
                        check_existing=False,
                    )
                    visual = analyze_rgba_pixels(list(loaded.pixels)).to_dict()
                else:
                    visual = analyze_rgba_pixels(()).to_dict()
            finally:
                if loaded is not None:
                    bpy.data.images.remove(loaded)
        if visual["passed"] is not True:
            raise EngineProbeVisualError(visual, observed)
        return {
            "status": "verified",
            **device,
            "probe_phases": engine_probe_phase_parameters(backend),
            "gpu_process_attested": True,
            "observed_gpu_uuid": observed,
            "visual_probe": visual,
            "diagnostic": "",
        }
    finally:
        if window is not None and previous is not None:
            window.scene = previous
        bpy.data.scenes.remove(scene)


def uuid_safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value)[:48]


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_render_engine_probe(output: Path) -> dict:
    expected = expected_gpu_uuid()
    candidates: dict[str, dict]
    try:
        cycles = _run_controlled_engine_probe("CYCLES", backend="CUDA")
    except Exception as exc:
        failure_visual = getattr(exc, "visual_probe", None)
        if not isinstance(failure_visual, dict):
            failure_visual = {
                "sampled_pixels": 0,
                "opaque_pixels": 0,
                "mean_luma": 0.0,
                "max_luma": 0.0,
                "luma_range": 0.0,
                "passed": False,
                "metrics_available": False,
            }
        else:
            failure_visual = dict(failure_visual)
            failure_visual["metrics_available"] = True
        failure_observed = (
            str(getattr(exc, "observed_gpu_uuid", "") or "").strip().lower()
        )
        candidates = {
            "CYCLES": {
                "status": "failed",
                "backend": "CUDA",
                "probe_phases": engine_probe_phase_parameters("CUDA"),
                "device_set_exact": bool(failure_observed),
                "cpu_device_enabled": False,
                "gpu_process_attested": bool(failure_observed),
                "observed_gpu_uuid": failure_observed,
                "visual_probe": failure_visual,
                "diagnostic": (f"CUDA:{type(exc).__name__}:{str(exc)[-1200:]}"),
            }
        }
    else:
        candidates = {"CYCLES": cycles}
    receipt = {
        "schema": ENGINE_PROBE_SCHEMA,
        "expected_gpu_uuid": expected,
        "blender_version": str(bpy.app.version_string),
        "candidates": candidates,
        "selected_engine": "",
        "selected_cycles_backend": "",
        "cycles_cpu_fallback_allowed": False,
    }
    candidate = candidates["CYCLES"]
    if (
        candidate.get("status") != "verified"
        or candidate.get("backend") != "CUDA"
        or candidate.get("gpu_process_attested") is not True
        or str(candidate.get("observed_gpu_uuid") or "").lower() != expected
        or candidate.get("visual_probe", {}).get("passed") is not True
    ):
        atomic_write_json(output, receipt)
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
            "cuda_cycles_probe_failed "
            f"status={candidate.get('status')};"
            f"visual={candidate.get('visual_probe')};"
            f"diagnostic={str(candidate.get('diagnostic') or '')[-1200:]}"
        )
    receipt["selected_engine"] = "CYCLES"
    receipt["selected_cycles_backend"] = "CUDA"
    atomic_write_json(output, receipt)
    return receipt


def write_postprocess_render_receipt(out_dir: Path) -> None:
    if not _POSTPROCESS_ENGINE:
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
            "postprocess_engine_not_selected"
        )
    atomic_write_json(
        out_dir / "postprocess_render_receipt.json",
        {
            "schema": POSTPROCESS_RENDER_RECEIPT_SCHEMA,
            "render_engine": _POSTPROCESS_ENGINE,
            "engine_policy": _POSTPROCESS_ENGINE_POLICY,
            "cycles_backend": _POSTPROCESS_CYCLES_BACKEND,
            "cycles_cpu_fallback_allowed": False,
            "gpu_process_attested": _POSTPROCESS_GPU_ATTESTED,
            "output_visual_attested": _POSTPROCESS_OUTPUT_ATTESTED,
            "observed_gpu_uuid": expected_gpu_uuid(),
        },
    )


def create_camera(center: Vector, radius: float, ortho_scale: float):
    data = bpy.data.cameras.new("Six_View_Camera")
    cam = bpy.data.objects.new("Six_View_Camera", data)
    bpy.context.collection.objects.link(cam)
    data.type = "ORTHO"
    data.ortho_scale = ortho_scale
    bpy.context.scene.camera = cam
    return cam


def load_motion_plan(out_dir: Path) -> dict:
    path = out_dir.parent / "motion_plan.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}


def load_material_spec(out_dir: Path) -> dict:
    path = out_dir.parent / "material_spec.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}


def presentation_profile(out_dir: Path, motion_plan: dict) -> str:
    profile = str(motion_plan.get("presentation_profile") or "").strip()
    if profile:
        return profile
    material_spec = load_material_spec(out_dir)
    family = material_spec.get("subject_family")
    if family in {
        "human_character",
        "animal_cartoon_character",
        "stylized_game_character",
    }:
        return (
            "character_loop"
            if motion_plan.get("motion_type") == "dynamic"
            else "character_showcase"
        )
    if family == "scene_environment":
        return "cinematic_scene"
    terms = " ".join(
        str(x).lower()
        for x in motion_plan.get("evidence_terms", [])
        + motion_plan.get("strong_evidence_terms", [])
    )
    if any(
        t in terms
        for t in ["材质", "节点", "shader", "material", "texture", "玻璃", "透明"]
    ):
        return "detail_showcase"
    return "studio_turntable"


def setup_presentation_environment(
    profile: str,
    center: Vector,
    radius: float,
    min_z: float,
    *,
    ground_size: float | None = None,
) -> None:
    if os.environ.get("VIDEO2BLENDER_PRESERVE_SOURCE_ENVIRONMENT", "0") == "1":
        return
    scene = bpy.context.scene
    palettes = {
        "cinematic_scene": ((0.025, 0.030, 0.042), (0.16, 0.18, 0.22, 1.0)),
        "character_loop": ((0.48, 0.78, 0.82), (0.30, 0.76, 0.82, 1.0)),
        "character_showcase": ((0.42, 0.70, 0.78), (0.27, 0.69, 0.77, 1.0)),
        "stylized_showcase": ((0.76, 0.80, 0.90), (0.72, 0.76, 0.86, 1.0)),
        "flat_surface_showcase": ((0.74, 0.80, 0.90), (0.74, 0.78, 0.86, 1.0)),
        "detail_showcase": ((0.58, 0.50, 0.68), (0.86, 0.74, 0.72, 1.0)),
        # Neutral, moderately bright product presentation.  This is
        # intentionally below pure white so white materials retain highlight
        # detail while the image remains readable in Finder and on the web.
        "static_product": ((0.18, 0.19, 0.21), (0.42, 0.43, 0.46, 1.0)),
        "studio_turntable": ((0.04, 0.08, 0.15), (0.08, 0.16, 0.28, 1.0)),
    }
    world_color, floor_color = palettes.get(profile, palettes["studio_turntable"])
    if scene.world:
        scene.world.color = world_color
    if bpy.data.objects.get("Presentation_Ground"):
        return
    bpy.ops.mesh.primitive_plane_add(
        size=(
            float(ground_size) if ground_size is not None else max(radius * 6.0, 4.0)
        ),
        location=(center.x, center.y, min_z - 0.025),
    )
    ground = bpy.context.object
    ground.name = "Presentation_Ground"
    mat = bpy.data.materials.new("Presentation_Ground_Material")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = floor_color
        bsdf.inputs["Roughness"].default_value = 0.78
    ground.data.materials.append(mat)


def scene_has_animation() -> bool:
    def has_animation_data(id_block) -> bool:
        animation_data = getattr(id_block, "animation_data", None)
        if not animation_data:
            return False
        if animation_data.action:
            return True
        if any(track.strips for track in animation_data.nla_tracks):
            return True
        return bool(animation_data.drivers)

    scene = bpy.context.scene
    if has_animation_data(scene) or has_animation_data(scene.world):
        return True
    for obj in scene.objects:
        if has_animation_data(obj) or has_animation_data(getattr(obj, "data", None)):
            return True
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        if has_animation_data(shape_keys):
            return True
    return any(
        has_animation_data(material.node_tree)
        for material in bpy.data.materials
        if material.node_tree
    )


def create_motion_root(objects: list[bpy.types.Object], center: Vector):
    root = bpy.data.objects.new("Final_Effect_Motion_Root", None)
    bpy.context.collection.objects.link(root)
    root.location = center
    for obj in objects:
        if obj.parent:
            continue
        matrix = obj.matrix_world.copy()
        obj.parent = root
        obj.matrix_parent_inverse = root.matrix_world.inverted()
        obj.matrix_world = matrix
    return root


def ensure_generic_animation(
    objects: list[bpy.types.Object],
    center: Vector,
    motion_plan: dict,
    fps: int,
    seconds: float,
    profile: str,
) -> None:
    if scene_has_animation():
        return
    scene = bpy.context.scene
    total = max(2, int(round(fps * seconds)))
    scene.frame_start = 1
    scene.frame_end = total
    root = create_motion_root(objects, center)
    terms = " ".join(
        str(x)
        for x in motion_plan.get("evidence_terms", [])
        + motion_plan.get("strong_evidence_terms", [])
    )
    if profile in {"character_loop", "character_showcase"}:
        keys = [
            (1, (0, 0, 0), (1, 1, 1), (0.0, 0.0, -0.05)),
            (total // 4, (0.0, 0.0, 0.035), (1, 1, 1), (0.0, 0.0, 0.06)),
            (total // 2, (0.015, 0.0, 0.0), (1, 1, 1), (0.0, 0.0, -0.04)),
            (3 * total // 4, (0.0, 0.0, 0.03), (1, 1, 1), (0.0, 0.0, 0.05)),
            (total, (0, 0, 0), (1, 1, 1), (0.0, 0.0, -0.05)),
        ]
    elif any(
        t in terms
        for t in [
            "液体",
            "水花",
            "流体",
            "fluid",
            "water",
            "splash",
            "火焰",
            "烟雾",
            "爆炸",
        ]
    ):
        keys = [
            (1, (0, 0, 0), (1, 1, 1), (0, 0, 0)),
            (total // 4, (0.03, 0, 0.05), (1.06, 1.06, 1.06), (0.0, 0.0, 0.05)),
            (total // 2, (-0.03, 0, -0.02), (0.98, 0.98, 1.04), (0.0, 0.0, -0.04)),
            (total, (0, 0, 0), (1, 1, 1), (0, 0, 0)),
        ]
    elif any(
        t in terms for t in ["布料", "毛发", "软体", "粒子", "cloth", "hair", "soft"]
    ):
        keys = [
            (1, (0, 0, 0), (1, 1, 1), (0.0, 0.0, -0.10)),
            (total // 3, (0, 0.03, 0.02), (1.0, 1.02, 0.98), (0.05, 0.0, 0.10)),
            (2 * total // 3, (0, -0.03, -0.01), (1.02, 1.0, 1.0), (-0.04, 0.0, -0.08)),
            (total, (0, 0, 0), (1, 1, 1), (0.0, 0.0, 0.0)),
        ]
    else:
        keys = [
            (1, (0, 0, 0), (1, 1, 1), (0.0, 0.0, -0.16)),
            (total // 2, (0.02, 0, 0.02), (1, 1, 1), (0.0, 0.0, 0.16)),
            (total, (0, 0, 0), (1, 1, 1), (0.0, 0.0, 0.0)),
        ]
    for frame, loc, scale, rot in keys:
        scene.frame_set(max(1, min(total, frame)))
        root.location = center + Vector(loc)
        root.scale = scale
        root.rotation_euler = rot
        root.keyframe_insert("location")
        root.keyframe_insert("scale")
        root.keyframe_insert("rotation_euler")


def render_frame_sequence_to_mp4(frames: Path, fps: int, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(frames / "frame_%04d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )


def render_adaptive_sequence_to_mp4(
    frames: Path,
    plan: AnimationDeliveryPlan,
    output: Path,
) -> str:
    """Encode sparse high-quality Blender frames into the fixed web contract."""

    common = [
        "ffmpeg",
        "-y",
        "-framerate",
        f"{plan.input_sequence_fps:.8f}",
        "-i",
        str(frames / "frame_%04d.png"),
    ]
    spatial = (
        f"scale={plan.width}:{plan.height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1"
    )
    tail = (
        f"tpad=stop_mode=clone:stop_duration={plan.duration_seconds:.3f},"
        f"fps={plan.output_fps},"
        f"trim=end_frame={plan.output_frame_count},"
        f"setpts=N/({plan.output_fps}*TB)"
    )
    attempts = []
    if plan.interpolation != "none":
        attempts.append(
            (
                "motion_compensated",
                spatial + f",minterpolate=fps={plan.output_fps}:"
                "mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1," + tail,
            )
        )
    attempts.append(("cfr_duplicate_fallback", spatial + "," + tail))
    last_error = None
    for mode, video_filter in attempts:
        output.unlink(missing_ok=True)
        try:
            subprocess.run(
                common
                + [
                    "-vf",
                    video_filter,
                    "-frames:v",
                    str(plan.output_frame_count),
                    "-r",
                    str(plan.output_fps),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-video_track_timescale",
                    "24000",
                    str(output),
                ],
                check=True,
            )
            return mode
        except subprocess.CalledProcessError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def render_six_views(
    out_dir: Path,
    center: Vector,
    radius: float,
    ortho_scale: float,
    res: int,
    samples: int,
    *,
    iso_only: bool = False,
    use_authored_camera: bool = False,
    framing_objects: list[bpy.types.Object] | None = None,
    prefer_authored_complex_scene: bool = False,
    static_interchange_framing: dict | None = None,
    record_rw1_focus_projection: bool = False,
) -> None:
    view_dir = out_dir / "six_views"
    view_dir.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    preserve_authored = preserve_source_render_settings()
    static_interchange_requested = bool(
        not preserve_authored
        and os.environ.get(
            "VIDEO2BLENDER_RW1_STATIC_INTERCHANGE_PRESENTATION",
            "0",
        )
        == "1"
    )
    static_studio_background = bool(
        os.environ.get(
            "VIDEO2BLENDER_STATIC_STUDIO_BACKGROUND",
            "0",
        )
        == "1"
    )
    if static_studio_background:
        if static_interchange_requested:
            # As above, only the explicitly enabled non-Blend v3 path has
            # the helper transferred beside this renderer.
            from rw1_static_interchange_framing import studio_ground_size

            presentation_ground_size = studio_ground_size(
                radius,
                ortho_scale,
                static_interchange=True,
            )
        else:
            presentation_ground_size = max(radius * 6.0, 4.0)
        setup_presentation_environment(
            "static_product",
            center,
            radius,
            center.z - radius,
            ground_size=presentation_ground_size,
        )
    else:
        presentation_ground_size = 0.0
    disabled_file_outputs = disable_delivery_only_compositor_file_outputs(scene)
    authored = scene.camera
    authored_camera_probe = {
        "usable": False,
        "clipped_subject_coverage": 0.0,
    }
    # A large authored environment often surrounds the camera, so projecting
    # every object bound produces a deceptively small coverage even when the
    # authored shot is exactly the intended composition.  Prefer that camera
    # for complex scenes; the real-render black-frame probe below remains the
    # fail-closed fallback authority.
    prefer_authored_complex_scene = bool(
        preserve_authored and authored is not None and prefer_authored_complex_scene
    )
    if (
        use_authored_camera
        and authored is not None
        and authored.type == "CAMERA"
        and framing_objects
    ):
        projected = []
        for obj in framing_objects:
            for corner in obj.bound_box:
                point = world_to_camera_view(
                    scene,
                    authored,
                    obj.matrix_world @ Vector(corner),
                )
                if float(point.z) > 0.0:
                    projected.append(point)
        if projected:
            minimum_x = min(float(point.x) for point in projected)
            maximum_x = max(float(point.x) for point in projected)
            minimum_y = min(float(point.y) for point in projected)
            maximum_y = max(float(point.y) for point in projected)
            visible_width = max(
                0.0,
                min(1.0, maximum_x) - max(0.0, minimum_x),
            )
            visible_height = max(
                0.0,
                min(1.0, maximum_y) - max(0.0, minimum_y),
            )
            coverage = visible_width * visible_height
            authored_camera_probe = {
                "usable": rw1_authored_camera_projection_usable(
                    coverage,
                    route="static",
                ),
                "clipped_subject_coverage": round(coverage, 6),
                "required_coverage": (RW1_STATIC_AUTHORED_CAMERA_MIN_COVERAGE),
            }
    if (
        use_authored_camera
        and authored is not None
        and authored.type == "CAMERA"
        and (
            not framing_objects
            or authored_camera_probe["usable"] is True
            or prefer_authored_complex_scene
        )
    ):
        cam = authored
        camera_mode = "authored_source_camera"
    else:
        cam = create_camera(center, radius, ortho_scale)
        camera_mode = (
            "generated_subject_camera_after_authored_visibility_failure"
            if preserve_authored and authored is not None
            else "generated_fallback_camera"
        )
    views = [
        ("iso", Vector(RW1_STATIC_ISO_DIRECTION)),
        ("front", Vector((0, -1, 0.18))),
        ("back", Vector((0, 1, 0.18))),
        ("left", Vector((-1, 0, 0.18))),
        ("right", Vector((1, 0, 0.18))),
        ("top", Vector((0, 0, 1))),
    ]
    if iso_only:
        views = [view for view in views if view[0] == "iso"]
    if cam == authored:
        # Preserve the authored composition for ISO, then use a generated
        # delivery camera for the five canonical orthographic views. Never
        # move the authored camera while producing release evidence.
        views[0] = ("iso", None)
    static_interchange_generated = bool(
        static_interchange_requested and camera_mode == "generated_fallback_camera"
    )
    if preserve_authored or static_interchange_generated:
        presentation_receipt = {
            "schema": "video2blender.presentation-camera.v1",
            "presentation_policy_generation": (
                RW1_STATIC_INTERCHANGE_PRESENTATION_POLICY_GENERATION
                if static_interchange_generated
                else RW1_PRESENTATION_POLICY_GENERATION
            ),
            "mode": camera_mode,
            "camera": cam.name_full,
            "source_render_settings_preserved": preserve_authored,
            "authored_camera_probe": authored_camera_probe,
            "complex_scene_authored_camera_preferred": (prefer_authored_complex_scene),
        }
        if static_interchange_generated:
            presentation_receipt["generated_fallback_framing"] = dict(
                static_interchange_framing or {}
            )
            presentation_receipt["studio_ground"] = {
                "camera_bounds_excluded": True,
                "size": round(float(presentation_ground_size), 6),
            }
        atomic_write_json(
            out_dir / "presentation_camera.json",
            presentation_receipt,
        )
    for name, direction in views:
        output = view_dir / f"{name}.png"
        if direction is not None:
            if cam == authored:
                cam = create_camera(center, radius, ortho_scale)
            cam.location = center + direction.normalized() * radius * 3.0
            look_at(cam, center)
        render_current_frame(
            output,
            res,
            res,
            samples,
            transparent=not static_studio_background,
        )
        if name == "iso" and preserve_authored:
            presentation_repairs: list[str] = (
                ["disabled_delivery_file_outputs"] if disabled_file_outputs else []
            )
            probe = render_result_visual_probe(
                controlled=False,
                render_path=output,
            )
            if (
                camera_mode == "authored_source_camera"
                and float(probe.get("mean_luma") or 0.0) < 0.01
                and float(probe.get("max_luma") or 0.0) < 0.05
            ):
                # Bounding-box projection can accept a disabled or otherwise
                # black authored camera.  Match the dynamic RW1 contract: only
                # after a real render proves the frame black, switch the
                # delivery render to a generated subject camera while leaving
                # the packed source scene and all authored render settings
                # untouched.
                cam = create_camera(center, radius, ortho_scale)
                iso_direction = Vector(RW1_STATIC_ISO_DIRECTION)
                cam.location = center + iso_direction.normalized() * radius * 3.0
                look_at(cam, center)
                camera_mode = (
                    "generated_subject_camera_after_authored_render_probe_failure"
                )
                render_current_frame(
                    output,
                    res,
                    res,
                    samples,
                    transparent=not static_studio_background,
                )
                probe = render_result_visual_probe(
                    controlled=False,
                    render_path=output,
                )
                presentation_repairs.append("generated_subject_camera")
            generated_delivery_is_dark = (
                camera_mode != "authored_source_camera"
                and float(probe.get("mean_luma") or 0.0) < 0.18
            )
            if generated_delivery_is_dark or (
                float(probe.get("mean_luma") or 0.0) < 0.08
                and float(probe.get("max_luma") or 0.0) < 0.10
            ):
                ensure_light(
                    center,
                    radius,
                    force_source_presentation_repair=True,
                )
                render_current_frame(
                    output,
                    res,
                    res,
                    samples,
                    transparent=not static_studio_background,
                )
                probe = render_result_visual_probe(
                    controlled=False,
                    render_path=output,
                )
                presentation_repairs.append("supplemental_delivery_lights")
            if (
                float(probe.get("mean_luma") or 0.0) < 0.01
                and float(probe.get("max_luma") or 0.0) < 0.05
                and bool(scene.render.use_compositing)
            ):
                scene.render.use_compositing = False
                render_current_frame(
                    output,
                    res,
                    res,
                    samples,
                    transparent=not static_studio_background,
                )
                probe = render_result_visual_probe(
                    controlled=False,
                    render_path=output,
                )
                presentation_repairs.append("disabled_broken_delivery_compositor")
            if (
                float(probe.get("mean_luma") or 0.0) < 0.01
                and float(probe.get("max_luma") or 0.0) < 0.05
            ):
                raise RuntimeError(
                    "RW1_SOURCE_PRESENTATION_REMAINS_BLACK_AFTER_BOUNDED_REPAIR"
                )
            if presentation_repairs:
                atomic_write_json(
                    out_dir / "presentation_camera.json",
                    {
                        "schema": "video2blender.presentation-camera.v1",
                        "presentation_policy_generation": RW1_PRESENTATION_POLICY_GENERATION,
                        "mode": camera_mode,
                        "camera": cam.name_full,
                        "source_render_settings_preserved": True,
                        "authored_camera_probe": authored_camera_probe,
                        "delivery_only_repairs": presentation_repairs,
                    },
                )
            atomic_write_json(
                out_dir / "lighting_adaptation.json",
                {
                    "schema": "video-replay-lighting-adaptation.v1",
                    "status": (
                        "bounded_delivery_presentation_repair"
                        if presentation_repairs
                        else "authored_source_preserved"
                    ),
                    "exposure_delta": 0.0,
                    "final_visual_probe": probe,
                    "source_environment_preserved": True,
                    "source_color_management_preserved": True,
                    "source_render_engine_preserved": True,
                    "source_compositor_preserved": (
                        "disabled_broken_delivery_compositor"
                        not in presentation_repairs
                    ),
                    "supplement_source_lights": (
                        "supplemental_delivery_lights" in presentation_repairs
                    ),
                    "delivery_only_repairs": presentation_repairs,
                },
            )
        if (
            name == "iso"
            and not preserve_source_render_settings()
            and os.environ.get("VIDEO2BLENDER_ADAPTIVE_EXPOSURE", "1") == "1"
        ):
            probe = render_result_visual_probe(
                controlled=False,
                render_path=output,
            )
            mean = float(probe.get("mean_luma") or 0.0)
            maximum = float(probe.get("max_luma") or 0.0)
            old_exposure = float(os.environ.get("VIDEO2BLENDER_EXPOSURE", "0.15"))
            target = 0.36 if mean < 0.28 else 0.62
            delta = 0.0
            if 0.0 < mean < 0.28 or mean > 0.78:
                requested_delta = math.log2(target / mean)
                maximum_adjustment = float(
                    os.environ.get(
                        "VIDEO2BLENDER_MAX_EXPOSURE_ADJUSTMENT",
                        "4.5",
                    )
                )
                if requested_delta > 0.0 and maximum > 0.0:
                    # Extremely dark source projects can need substantially
                    # more than 1.5 stops.  Keep that correction bounded by
                    # both the configured adjustment and a highlight ceiling.
                    # A tiny specular highlight may already be at 1.0 while
                    # the rest of the subject is severely underexposed; that
                    # must not turn a positive correction into a negative one.
                    if maximum < 0.92:
                        highlight_delta = math.log2(0.92 / maximum)
                        requested_delta = min(
                            requested_delta,
                            highlight_delta,
                        )
                    else:
                        requested_delta = min(
                            requested_delta,
                            float(
                                os.environ.get(
                                    "VIDEO2BLENDER_MAX_DARK_SCENE_BOOST",
                                    "1.25",
                                )
                            ),
                        )
                delta = max(
                    -maximum_adjustment,
                    min(maximum_adjustment, requested_delta),
                )
            new_exposure = old_exposure + delta
            if abs(delta) >= 0.05:
                os.environ["VIDEO2BLENDER_EXPOSURE"] = f"{new_exposure:.4f}"
                render_current_frame(
                    output,
                    res,
                    res,
                    samples,
                    transparent=not static_studio_background,
                )
                probe = render_result_visual_probe(
                    controlled=False,
                    render_path=output,
                )
            atomic_write_json(
                out_dir / "lighting_adaptation.json",
                {
                    "schema": "video-replay-lighting-adaptation.v1",
                    "status": "adjusted" if abs(delta) >= 0.05 else "preserved",
                    "initial_exposure": old_exposure,
                    "exposure_delta": round(delta, 4),
                    "final_exposure": round(new_exposure, 4),
                    "final_visual_probe": probe,
                    "supplement_source_lights": (
                        os.environ.get("VIDEO2BLENDER_SUPPLEMENT_SOURCE_LIGHTS", "0")
                        == "1"
                    ),
                },
            )
        if name == "iso" and framing_objects and record_rw1_focus_projection:
            attach_rw1_focus_projection_evidence(
                out_dir,
                scene,
                cam,
                framing_objects,
                [int(scene.frame_current)],
            )


def render_turntable(
    out_dir: Path,
    center: Vector,
    radius: float,
    ortho_scale: float,
    width: int,
    height: int,
    fps: int,
    seconds: float,
    samples: int,
    motion_plan: dict,
    min_z: float,
) -> None:
    frames = out_dir / "turntable_frames"
    reset_dir(frames)
    total = max(1, int(round(fps * seconds)))
    turn_frames = max(1, int(round(fps * min(4.0, seconds))))
    profile = presentation_profile(out_dir, motion_plan)
    setup_presentation_environment(profile, center, radius, min_z)
    default_arcs = {
        "cinematic_scene": 14,
        "character_showcase": 24,
        "character_loop": 18,
        "stylized_showcase": 16,
        "flat_surface_showcase": 8,
        "detail_showcase": 10,
        "studio_turntable": 42,
    }
    arc_degrees = float(
        os.environ.get("TURNTABLE_ARC_DEGREES", str(default_arcs.get(profile, 42)))
    )
    cam = create_camera(center, radius, ortho_scale)
    cam.data.type = (
        "PERSP"
        if profile in {"cinematic_scene", "character_showcase", "stylized_showcase"}
        else "ORTHO"
    )
    if cam.data.type == "ORTHO":
        scale_factor = (
            2.10
            if profile == "detail_showcase"
            else (1.18 if profile == "studio_turntable" else 1.08)
        )
        cam.data.ortho_scale = ortho_scale * scale_factor
    else:
        cam.data.lens = 55
        cam.data.dof.use_dof = profile == "cinematic_scene"
        cam.data.dof.focus_distance = radius * 3.0
        cam.data.dof.aperture_fstop = 5.6
    for idx in range(total):
        if idx < turn_frames and turn_frames > 1:
            t = idx / (turn_frames - 1)
            angle = math.radians(-arc_degrees * 0.5 + arc_degrees * t)
        else:
            angle = math.radians(arc_degrees * 0.5)
        if profile == "flat_surface_showcase":
            height_factor = 2.4
        else:
            height_factor = (
                0.72
                if profile
                in {"cinematic_scene", "character_showcase", "stylized_showcase"}
                else 0.95
            )
        distance_factor = 2.75 if cam.data.type == "PERSP" else 3.2
        if profile == "detail_showcase":
            distance_factor = 3.45
        direction = Vector(
            (math.sin(angle) * 1.25, -math.cos(angle) * 2.2, height_factor)
        ).normalized()
        push = 1.0 - 0.035 * (idx / max(total - 1, 1))
        cam.location = center + direction * radius * distance_factor * push
        look_at(cam, center)
        output = frames / f"frame_{idx:04d}.png"
        render_current_frame(output, width, height, samples, transparent=False)
    mp4 = out_dir / "turntable_5s.mp4"
    render_frame_sequence_to_mp4(frames, fps, mp4)


def render_dynamic_final_effect(
    out_dir: Path,
    objects: list[bpy.types.Object],
    center: Vector,
    radius: float,
    ortho_scale: float,
    width: int,
    height: int,
    fps: int,
    seconds: float,
    samples: int,
    motion_plan: dict,
    min_z: float,
) -> None:
    if not (
        scene_has_animation() or motion_plan_has_verified_time_animation(motion_plan)
    ):
        raise RuntimeError("VIDEO_REPLAY_DYNAMIC_WITHOUT_VERIFIED_SCENE_ANIMATION")
    frames = out_dir / "final_effect_frames"
    reset_dir(frames)
    profile = presentation_profile(out_dir, motion_plan)
    setup_presentation_environment(profile, center, radius, min_z)
    # The source timeline is sampled adaptively.  The caller owns the formal
    # delivery contract: legacy replay passes 720p/24fps/5s while RW2 passes
    # 720p/90fps/4s.  Do not replace the caller's values with module defaults.
    cam = create_camera(center, radius, ortho_scale)
    cam.data.type = (
        "PERSP" if profile in {"cinematic_scene", "character_loop"} else "ORTHO"
    )
    if cam.data.type == "ORTHO":
        cam.data.ortho_scale = ortho_scale * 1.12
    else:
        cam.data.lens = 58
        cam.data.dof.use_dof = profile == "cinematic_scene"
        cam.data.dof.focus_distance = radius * 3.0
        cam.data.dof.aperture_fstop = 5.6
    height_factor = 0.72 if profile in {"cinematic_scene", "character_loop"} else 1.05
    cam.location = center + Vector(
        (0.95, -2.35, height_factor)
    ).normalized() * radius * (2.9 if cam.data.type == "PERSP" else 3.3)
    look_at(cam, center)
    scene = bpy.context.scene
    src_start = scene.frame_start
    src_end = max(scene.frame_end, src_start + 1)
    plan = build_animation_delivery_plan(
        motion_plan,
        source_frame_start=src_start,
        source_frame_end=src_end,
        output_width=width,
        output_height=height,
        output_fps=fps,
        duration_seconds=seconds,
    )
    for idx, frame in enumerate(plan.source_frames):
        scene.frame_set(frame)
        output = frames / f"frame_{idx:04d}.png"
        render_current_frame(
            output,
            plan.width,
            plan.height,
            samples,
            transparent=False,
        )
    encoder_mode = render_adaptive_sequence_to_mp4(
        frames, plan, out_dir / "final_effect.mp4"
    )
    receipt = plan.to_dict()
    receipt.update(
        {
            "encoder_mode": encoder_mode,
            "postprocess_render_engine": str(bpy.context.scene.render.engine),
            "postprocess_engine_policy": _POSTPROCESS_ENGINE_POLICY,
            "postprocess_cycles_backend": _POSTPROCESS_CYCLES_BACKEND,
            "cycles_cpu_fallback_allowed": False,
            "gpu_process_attested": _POSTPROCESS_GPU_ATTESTED,
            "output_visual_attested": _POSTPROCESS_OUTPUT_ATTESTED,
            "observed_gpu_uuid": expected_gpu_uuid(),
            "source_render_frames_saved": (
                max(src_end - src_start + 1, plan.output_frame_count)
                - plan.render_frame_count
            ),
            "formal_contract": {
                "width": plan.width,
                "height": plan.height,
                "fps": plan.output_fps,
                "duration_seconds": plan.duration_seconds,
                "frame_count": plan.output_frame_count,
            },
        }
    )
    (out_dir / "animation_delivery_plan.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def render_rw1_source_dynamic(
    out_dir: Path,
    objects: list[bpy.types.Object],
    center: Vector,
    radius: float,
    ortho_scale: float,
    width: int,
    height: int,
    fps: int,
    seconds: float,
    samples: int,
    motion_plan: dict,
    min_z: float,
) -> None:
    """Render a source-project animation under the RW1 4s/90fps contract."""

    if not (
        scene_has_animation() or motion_plan_has_verified_time_animation(motion_plan)
    ):
        raise RuntimeError("RW1_DYNAMIC_WITHOUT_VERIFIED_SCENE_ANIMATION")
    scene = bpy.context.scene
    disabled_file_outputs = disable_delivery_only_compositor_file_outputs(scene)
    src_start = int(scene.frame_start)
    src_end = max(int(scene.frame_end), src_start + 1)
    source_fps = max(
        float(scene.render.fps) / max(float(scene.render.fps_base), 0.000001),
        1.0,
    )
    window_source_frames = max(2, int(round(source_fps * seconds)))
    selected_end = min(src_end, src_start + window_source_frames - 1)
    output_count = max(2, int(round(fps * seconds)))
    requested_render_count = min(
        90,
        max(2, int(round(source_fps * seconds))),
    )
    source_frames = list(
        sample_source_frames(
            src_start,
            selected_end,
            requested_render_count,
        )
    )
    frames = out_dir / "final_effect_frames"
    reset_dir(frames)
    profile = presentation_profile(out_dir, motion_plan)
    preserve_authored = preserve_source_render_settings()
    authored_camera = scene.camera
    authored_camera_probe = {
        "usable": False,
        "maximum_clipped_subject_coverage": 0.0,
        "sampled_source_frames": [],
    }
    if preserve_authored and authored_camera is not None:
        prior_frame = int(scene.frame_current)
        sampled_frames = sorted(
            {
                int(source_frames[0]),
                int(source_frames[len(source_frames) // 2]),
                int(source_frames[-1]),
            }
        )
        coverages = []
        try:
            for source_frame in sampled_frames:
                scene.frame_set(source_frame)
                projected = []
                for obj in objects:
                    for corner in obj.bound_box:
                        point = world_to_camera_view(
                            scene,
                            authored_camera,
                            obj.matrix_world @ Vector(corner),
                        )
                        if float(point.z) > 0.0:
                            projected.append(point)
                if not projected:
                    coverages.append(0.0)
                    continue
                minimum_x = min(float(point.x) for point in projected)
                maximum_x = max(float(point.x) for point in projected)
                minimum_y = min(float(point.y) for point in projected)
                maximum_y = max(float(point.y) for point in projected)
                visible_width = max(
                    0.0,
                    min(1.0, maximum_x) - max(0.0, minimum_x),
                )
                visible_height = max(
                    0.0,
                    min(1.0, maximum_y) - max(0.0, minimum_y),
                )
                coverages.append(visible_width * visible_height)
        finally:
            scene.frame_set(prior_frame)
        maximum_coverage = max(coverages, default=0.0)
        authored_camera_probe = {
            "usable": rw1_authored_camera_projection_usable(
                maximum_coverage,
                route="dynamic",
            ),
            "maximum_clipped_subject_coverage": round(maximum_coverage, 6),
            "required_coverage": (RW1_DYNAMIC_AUTHORED_CAMERA_MIN_COVERAGE),
            "sampled_source_frames": sampled_frames,
        }
    if (
        preserve_authored
        and authored_camera is not None
        and authored_camera_probe["usable"] is True
    ):
        camera_mode = "authored_source_camera"
    else:
        camera_mode = (
            "generated_subject_camera_after_authored_visibility_failure"
            if preserve_authored and authored_camera is not None
            else "generated_fallback_camera"
        )
    camera_bounds = rw1_dynamic_camera_bounds(
        camera_mode,
        objects,
        source_frames,
        center,
        radius,
        ortho_scale,
        min_z,
    )
    if camera_mode == "authored_source_camera":
        cam = authored_camera
    else:
        if not preserve_authored:
            setup_presentation_environment(
                profile,
                camera_bounds["center"],
                camera_bounds["radius"],
                camera_bounds["min_z"],
            )
        cam = create_rw1_dynamic_generated_camera(profile, camera_bounds)
    atomic_write_json(
        out_dir / "presentation_camera.json",
        {
            "schema": "video2blender.presentation-camera.v1",
            "presentation_policy_generation": RW1_PRESENTATION_POLICY_GENERATION,
            "mode": camera_mode,
            "camera": cam.name_full,
            "source_render_settings_preserved": preserve_authored,
            "authored_camera_probe": authored_camera_probe,
            **(
                {"generated_camera_bounds": camera_bounds["evidence"]}
                if camera_bounds["temporal_bounds_applied"]
                else {}
            ),
        },
    )
    if preserve_authored:
        presentation_repairs: list[str] = (
            ["disabled_delivery_file_outputs"] if disabled_file_outputs else []
        )
        scene.frame_set(source_frames[0])
        exposure_probe_path = frames / ".authored_exposure_probe.png"
        render_current_frame(
            exposure_probe_path,
            min(width, 480),
            min(height, 270),
            max(16, min(samples, 32)),
            transparent=False,
        )
        exposure_probe = render_result_visual_probe(
            controlled=False,
            render_path=exposure_probe_path,
        )
        authored_exposure_probe = dict(exposure_probe)
        if (
            camera_mode == "authored_source_camera"
            and float(exposure_probe.get("mean_luma") or 0.0) < 0.01
            and float(exposure_probe.get("max_luma") or 0.0) < 0.05
        ):
            # Projected bounds alone can call a disabled/black authored camera
            # usable.  Keep the source world, lights, engine and colour
            # management unchanged, but fall back to a subject camera when a
            # real render probe proves that the active camera is blank.
            camera_mode = "generated_subject_camera_after_authored_render_probe_failure"
            camera_bounds = rw1_dynamic_camera_bounds(
                camera_mode,
                objects,
                source_frames,
                center,
                radius,
                ortho_scale,
                min_z,
            )
            cam = create_rw1_dynamic_generated_camera(
                profile,
                camera_bounds,
            )
            authored_camera_probe["render_probe_usable"] = False
            authored_camera_probe["render_probe_mean_luma"] = round(
                float(authored_exposure_probe.get("mean_luma") or 0.0),
                8,
            )
            render_current_frame(
                exposure_probe_path,
                min(width, 480),
                min(height, 270),
                max(16, min(samples, 32)),
                transparent=False,
            )
            exposure_probe = render_result_visual_probe(
                controlled=False,
                render_path=exposure_probe_path,
            )
            presentation_repairs.append("generated_subject_camera")
            atomic_write_json(
                out_dir / "presentation_camera.json",
                {
                    "schema": "video2blender.presentation-camera.v1",
                    "presentation_policy_generation": RW1_PRESENTATION_POLICY_GENERATION,
                    "mode": camera_mode,
                    "camera": cam.name_full,
                    "source_render_settings_preserved": True,
                    "authored_camera_probe": authored_camera_probe,
                    "generated_camera_bounds": camera_bounds["evidence"],
                },
            )
        if (
            float(exposure_probe.get("mean_luma") or 0.0) < 0.01
            and float(exposure_probe.get("max_luma") or 0.0) < 0.05
        ):
            # Some source files rely on viewport-only illumination or lights
            # that do not cover a generated delivery camera.  The packed
            # asset.blend has already been saved at this point, so add a
            # bounded in-memory presentation rig only after a real render
            # proves the preserved setup is still effectively black.
            ensure_light(
                camera_bounds["center"],
                camera_bounds["radius"],
                force_source_presentation_repair=True,
            )
            render_current_frame(
                exposure_probe_path,
                min(width, 480),
                min(height, 270),
                max(16, min(samples, 32)),
                transparent=False,
            )
            exposure_probe = render_result_visual_probe(
                controlled=False,
                render_path=exposure_probe_path,
            )
            presentation_repairs.append("supplemental_delivery_lights")
        if (
            float(exposure_probe.get("mean_luma") or 0.0) < 0.01
            and float(exposure_probe.get("max_luma") or 0.0) < 0.05
            and bool(scene.render.use_compositing)
        ):
            # Missing compositor inputs can mask an otherwise intact source
            # scene.  Disable compositing only for the delivery render after
            # both camera and lighting probes have failed; never alter the
            # packed source asset.
            scene.render.use_compositing = False
            render_current_frame(
                exposure_probe_path,
                min(width, 480),
                min(height, 270),
                max(16, min(samples, 32)),
                transparent=False,
            )
            exposure_probe = render_result_visual_probe(
                controlled=False,
                render_path=exposure_probe_path,
            )
            presentation_repairs.append("disabled_broken_delivery_compositor")
        if (
            float(exposure_probe.get("mean_luma") or 0.0) < 0.01
            and float(exposure_probe.get("max_luma") or 0.0) < 0.05
        ):
            raise RuntimeError(
                "RW1_SOURCE_PRESENTATION_REMAINS_BLACK_AFTER_BOUNDED_REPAIR"
            )
        if presentation_repairs:
            atomic_write_json(
                out_dir / "presentation_camera.json",
                {
                    "schema": "video2blender.presentation-camera.v1",
                    "presentation_policy_generation": RW1_PRESENTATION_POLICY_GENERATION,
                    "mode": camera_mode,
                    "camera": cam.name_full,
                    "source_render_settings_preserved": True,
                    "authored_camera_probe": authored_camera_probe,
                    **(
                        {"generated_camera_bounds": camera_bounds["evidence"]}
                        if camera_bounds["temporal_bounds_applied"]
                        else {}
                    ),
                    "delivery_only_repairs": presentation_repairs,
                },
            )
        initial_exposure = float(scene.view_settings.exposure)
        exposure_probe_path.unlink(missing_ok=True)
        atomic_write_json(
            out_dir / "lighting_adaptation.json",
            {
                "schema": "video-replay-lighting-adaptation.v1",
                "status": (
                    "bounded_delivery_presentation_repair"
                    if presentation_repairs
                    else "authored_source_preserved"
                ),
                "initial_exposure": initial_exposure,
                "exposure_delta": 0.0,
                "final_exposure": round(initial_exposure, 4),
                "initial_visual_probe": exposure_probe,
                "authored_camera_visual_probe": authored_exposure_probe,
                "source_environment_preserved": True,
                "source_color_management_preserved": True,
                "source_color_management_only_exposure_adjusted": False,
                "source_render_engine_preserved": True,
                "source_compositor_preserved": (
                    "disabled_broken_delivery_compositor" not in presentation_repairs
                ),
                "supplement_source_lights": (
                    "supplemental_delivery_lights" in presentation_repairs
                ),
                "delivery_only_repairs": presentation_repairs,
            },
        )
    else:
        # Interchange imports do not contain an authored Blender render setup.
        # Keep the bounded exposure fallback for those projects only.
        scene.frame_set(source_frames[0])
        exposure_probe_path = frames / ".exposure_probe.png"
        render_current_frame(
            exposure_probe_path,
            width,
            height,
            max(16, min(samples, 48)),
            transparent=False,
        )
        exposure_probe = render_result_visual_probe(
            controlled=False,
            render_path=exposure_probe_path,
        )
        initial_exposure = float(os.environ.get("VIDEO2BLENDER_EXPOSURE", "0.0"))
        mean_luma = float(exposure_probe.get("mean_luma") or 0.0)
        exposure_delta = 0.0
        if mean_luma > 0.72:
            target_luma = 0.35 if mean_luma > 0.88 else 0.48
            exposure_delta = max(-3.0, math.log2(target_luma / mean_luma))
        elif 0.0 < mean_luma < 0.08:
            exposure_delta = min(1.0, math.log2(0.16 / mean_luma))
        if abs(exposure_delta) >= 0.05:
            os.environ["VIDEO2BLENDER_EXPOSURE"] = (
                f"{initial_exposure + exposure_delta:.4f}"
            )
        exposure_probe_path.unlink(missing_ok=True)
        atomic_write_json(
            out_dir / "lighting_adaptation.json",
            {
                "schema": "video-replay-lighting-adaptation.v1",
                "status": ("adjusted" if abs(exposure_delta) >= 0.05 else "preserved"),
                "initial_exposure": initial_exposure,
                "exposure_delta": round(exposure_delta, 4),
                "final_exposure": round(
                    initial_exposure + exposure_delta,
                    4,
                ),
                "source_environment_preserved": False,
                "source_color_management_preserved": False,
                "source_render_engine_preserved": False,
                "supplement_source_lights": False,
            },
        )
    attach_rw1_focus_projection_evidence(
        out_dir,
        scene,
        cam,
        objects,
        source_frames,
    )
    for index, source_frame in enumerate(source_frames):
        scene.frame_set(source_frame)
        render_current_frame(
            frames / f"frame_{index:04d}.png",
            width,
            height,
            samples,
            transparent=False,
        )
    delivery_plan = AnimationDeliveryPlan(
        schema="video2blender.rw1-animation-delivery.v2",
        width=width,
        height=height,
        output_fps=fps,
        duration_seconds=seconds,
        output_frame_count=output_count,
        temporal_tier="source_project",
        requested_render_fps=int(round(source_fps)),
        render_frame_count=len(source_frames),
        source_frame_start=src_start,
        source_frame_end=selected_end,
        source_frames=tuple(source_frames),
        input_sequence_fps=round(len(source_frames) / seconds, 8),
        interpolation=(
            "none"
            if len(source_frames) >= output_count
            else "motion_compensated_with_cfr_fallback"
        ),
    )
    encoder_mode = render_adaptive_sequence_to_mp4(
        frames,
        delivery_plan,
        out_dir / "final_effect.mp4",
    )
    shutil.rmtree(frames, ignore_errors=True)
    (out_dir / "animation_delivery_plan.json").write_text(
        json.dumps(
            {
                "schema": "video2blender.rw1-animation-delivery.v2",
                "mode": "source_project_animation",
                "width": width,
                "height": height,
                "fps": fps,
                "duration_seconds": seconds,
                "frame_count": output_count,
                "render_frame_count": len(source_frames),
                "rendered_source_frames": source_frames,
                "encoder_mode": encoder_mode,
                "source_render_frames_saved": output_count - len(source_frames),
                "source_frame_start": src_start,
                "source_frame_end": src_end,
                "selected_source_frame_start": src_start,
                "selected_source_frame_end": selected_end,
                "source_fps": source_fps,
                "verified_scene_animation": True,
                "generic_animation_added": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--engine-probe", action="store_true")
    parser.add_argument("--engine-probe-output", default="")
    parser.add_argument(
        "--gpu-attestation-preflight",
        action="store_true",
        help=(
            "run the existing bounded CUDA probe in this Blender process "
            "before a potentially short production render"
        ),
    )
    parser.add_argument(
        "--view-resolution",
        type=int,
        default=int(os.environ.get("SIX_VIEW_RESOLUTION", "1600")),
    )
    parser.add_argument(
        "--animation-width",
        type=int,
        default=int(os.environ.get("TURNTABLE_WIDTH", "1920")),
    )
    parser.add_argument(
        "--animation-height",
        type=int,
        default=int(os.environ.get("TURNTABLE_HEIGHT", "1080")),
    )
    parser.add_argument(
        "--fps", type=int, default=int(os.environ.get("TURNTABLE_FPS", "70"))
    )
    parser.add_argument(
        "--seconds", type=float, default=float(os.environ.get("TURNTABLE_SECONDS", "5"))
    )
    parser.add_argument(
        "--samples", type=int, default=int(os.environ.get("TURNTABLE_SAMPLES", "64"))
    )
    parser.add_argument("--skip-animation", action="store_true")
    parser.add_argument(
        "--rw1-source-contract",
        action="store_true",
        help="preserve source-project motion under RW1's 4-second/90-fps contract",
    )
    parser.add_argument(
        "--use-authored-static-camera",
        action="store_true",
        help=(
            "use the source scene camera for ISO while still rendering the "
            "complete static six-view delivery"
        ),
    )
    view_group = parser.add_mutually_exclusive_group()
    view_group.add_argument("--iso-only", action="store_true")
    view_group.add_argument("--skip-views", action="store_true")
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else None
    args = parser.parse_args(argv)

    if args.engine_probe:
        if not args.engine_probe_output:
            raise SystemExit("--engine-probe-output is required")
        receipt = run_render_engine_probe(Path(args.engine_probe_output))
        print(
            "VIDEO_REPLAY_RENDER_ENGINE_ATTESTED "
            f"engine={receipt['selected_engine']} "
            f"backend={receipt['selected_cycles_backend'] or 'EEVEE_GPU'}"
        )
        return
    if not args.out_dir:
        raise SystemExit("--out-dir is required")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.gpu_attestation_preflight:
        try:
            receipt = _run_controlled_engine_probe("CYCLES", backend="CUDA")
            atomic_write_json(
                out_dir / "gpu_attestation_preflight.json",
                receipt,
            )
        except BaseException as exc:
            print(
                "VIDEO2BLENDER_GPU_ATTESTATION_PREFLIGHT_FAILED "
                f"{type(exc).__name__}:{exc}",
                file=sys.stderr,
                flush=True,
            )
            os._exit(78)
    disable_scene_compositing_if_requested(bpy.context.scene)
    objects = visible_objects()
    if not objects:
        raise SystemExit("no renderable objects")
    focus_objects = (
        rw1_source_focus_objects(objects) if args.rw1_source_contract else objects
    )
    for obj in focus_objects:
        if is_explicit_presentation_helper(obj):
            obj.hide_render = True
    preserve_generated_assembly = (
        os.environ.get("VIDEO2BLENDER_PRESERVE_GENERATED_ASSEMBLY", "0") == "1"
    )
    if preserve_generated_assembly:
        # RW2 replay code creates one subject from many separately named parts.
        # The generic repeated-prefix heuristic is useful for source projects,
        # but can mistake a dense particle/effect cluster (for example an
        # aircraft tail flame) for the whole subject and hide the fuselage.
        # Preserve every generated assembly part while still excluding only
        # explicitly identified reference cards and named studio backdrops.
        presentation_objects = [
            obj
            for obj in focus_objects
            if not is_explicit_presentation_helper(obj)
            and not is_named_framing_backdrop(obj)
        ] or list(focus_objects)
        # Visibility and framing are separate contracts. A complete generated
        # assembly may legitimately include a broad floor or a deliberately
        # cropped context object. Keep those objects visible, but honor an
        # explicit subject-focus selection for camera bounds so context cannot
        # shrink the actual subject to a tiny fraction of the frame. Without
        # an explicit selection, retain complete-assembly bounds; this avoids
        # reviving the repeated-detail heuristic that could frame an aircraft
        # tail flame instead of its fuselage.
        explicit_framing_objects = [
            obj
            for obj in presentation_objects
            if bool(obj.get("video2blender_presentation_focus", False))
        ]
        framing_objects = explicit_framing_objects or presentation_objects
    else:
        presentation_objects = presentation_focus_objects(focus_objects)
        framing_objects = presentation_objects
    if os.environ.get("VIDEO2BLENDER_PRESENTATION_HIDE_FLAT_BACKDROPS", "0") == "1":
        selected = {obj.name_full for obj in presentation_objects}
        hidden = []
        for obj in focus_objects:
            if obj.name_full not in selected:
                obj.hide_render = True
                hidden.append(obj.name_full)
        atomic_write_json(
            out_dir / "presentation_filter.json",
            {
                "schema": "video2blender.presentation-filter.v1",
                "mode": (
                    "preserve_generated_assembly"
                    if preserve_generated_assembly
                    else "hide_non_subject_helpers"
                ),
                "selected_objects": sorted(selected),
                "framing_objects": sorted(obj.name_full for obj in framing_objects),
                "hidden_objects": sorted(hidden),
            },
        )
    mins, maxs = bbox(framing_objects)
    center = (mins + maxs) * 0.5
    size = maxs - mins
    radius = max(size.length * 0.5, 0.5)
    ortho_scale = max(size.x, size.y, size.z) * 1.35 or 2.0
    ensure_light(center, radius)
    motion_plan = load_motion_plan(out_dir)
    verified_scene_animation = (
        scene_has_animation() or motion_plan_has_verified_time_animation(motion_plan)
    )
    route, fallback_reason = effective_route(
        str(motion_plan.get("motion_type") or ""),
        verified_scene_animation=verified_scene_animation,
    )
    # This is the final route decision.  RW1 writes an initial audit hint
    # before this renderer runs, so replace that hint atomically rather than
    # leaving two contradictory route facts for the worker to consume.
    atomic_write_text(out_dir / "effective_route.txt", route)
    if fallback_reason:
        atomic_write_json(
            out_dir / "route_override.json",
            {
                "schema": ROUTE_OVERRIDE_SCHEMA,
                "requested_route": "dynamic",
                "effective_route": "static",
                "reason": fallback_reason,
                "verified_scene_animation": False,
                "camera_only_fallback_forbidden": True,
                "generic_animation_fallback_forbidden": True,
            },
        )
    if args.rw1_source_contract:
        if route == "dynamic":
            render_rw1_source_dynamic(
                out_dir,
                presentation_objects,
                center,
                radius,
                ortho_scale,
                args.animation_width,
                args.animation_height,
                args.fps,
                args.seconds,
                args.samples,
                motion_plan,
                mins.z,
            )
        else:
            static_center = center
            static_radius = radius
            static_ortho_scale = ortho_scale
            static_framing_objects = presentation_objects
            static_interchange_framing = None
            if (
                not preserve_source_render_settings()
                and os.environ.get(
                    "VIDEO2BLENDER_RW1_STATIC_INTERCHANGE_PRESENTATION",
                    "0",
                )
                == "1"
            ):
                camera_bounds = rw1_static_interchange_camera_bounds(
                    focus_objects,
                    mins,
                    maxs,
                    ortho_scale,
                )
                static_center = camera_bounds["center"]
                static_radius = camera_bounds["radius"]
                static_ortho_scale = camera_bounds["ortho_scale"]
                static_framing_objects = camera_bounds["objects"]
                static_interchange_framing = camera_bounds["receipt"]
                ensure_light(
                    static_center,
                    static_radius,
                    recenter_delivery_rig=True,
                )
            render_six_views(
                out_dir,
                static_center,
                static_radius,
                static_ortho_scale,
                args.view_resolution,
                args.samples,
                iso_only=bool(args.iso_only),
                use_authored_camera=args.use_authored_static_camera,
                framing_objects=static_framing_objects,
                # Subject-focus filtering can intentionally reduce a large
                # authored environment to only a few framing objects.  The
                # raw scene size is therefore the stable signal that this is
                # a complex authored shot whose camera should be preserved.
                prefer_authored_complex_scene=(len(bpy.context.scene.objects) >= 20),
                static_interchange_framing=static_interchange_framing,
                record_rw1_focus_projection=True,
            )
            if (
                preserve_source_render_settings()
                and not (out_dir / "lighting_adaptation.json").is_file()
            ):
                atomic_write_json(
                    out_dir / "lighting_adaptation.json",
                    {
                        "schema": "video-replay-lighting-adaptation.v1",
                        "status": "authored_source_preserved",
                        "exposure_delta": 0.0,
                        "source_environment_preserved": True,
                        "source_color_management_preserved": True,
                        "source_render_engine_preserved": True,
                        "supplement_source_lights": False,
                    },
                )
        write_postprocess_render_receipt(out_dir)
        return
    if not args.skip_views or route == "static":
        render_six_views(
            out_dir,
            center,
            radius,
            ortho_scale,
            args.view_resolution,
            args.samples,
            # A route fallback is still a release delivery, not an ISO-only
            # preview. Only an explicit CLI preview request may omit views.
            iso_only=bool(args.iso_only),
            use_authored_camera=args.use_authored_static_camera,
        )
    if args.skip_animation or route == "static":
        write_postprocess_render_receipt(out_dir)
        return
    if route == "dynamic":
        render_dynamic_final_effect(
            out_dir,
            objects,
            center,
            radius,
            ortho_scale,
            args.animation_width,
            args.animation_height,
            args.fps,
            args.seconds,
            args.samples,
            motion_plan,
            mins.z,
        )
    else:
        fps = args.fps
        render_turntable(
            out_dir,
            center,
            radius,
            ortho_scale,
            args.animation_width,
            args.animation_height,
            fps,
            args.seconds,
            args.samples,
            motion_plan,
            mins.z,
        )
    write_postprocess_render_receipt(out_dir)


if __name__ == "__main__":
    main()
