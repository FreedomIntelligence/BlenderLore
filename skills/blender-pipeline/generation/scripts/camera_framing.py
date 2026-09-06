"""Projection-aware fitting for pipeline-owned orthographic cameras only."""

from __future__ import annotations

import math


def orthographic_scale_factor(bounds, *, margin: float = 0.07) -> float:
    """Zoom out enough to put projected bounds inside a safe camera margin.

    Bounds are raw normalized camera coordinates (left, right, bottom, top),
    not clamped pixel coordinates. Existing orientation and center are kept.
    The camera's projection already accounts for aspect and pixel aspect.
    """
    if not 0.0 <= margin < 0.5:
        raise ValueError("camera margin must be in [0, 0.5)")
    if len(bounds) != 4 or not all(math.isfinite(float(v)) for v in bounds):
        raise ValueError("camera projection bounds must contain four finite values")
    left, right, bottom, top = (float(value) for value in bounds)
    if left > right or bottom > top:
        raise ValueError("camera projection bounds are inverted")
    half_frame = 0.5 - margin
    return max(
        1.0,
        (0.5 - left) / half_frame,
        (right - 0.5) / half_frame,
        (0.5 - bottom) / half_frame,
        (top - 0.5) / half_frame,
    )


def fit_orthographic_camera(scene, camera, objects, *, margin: float = 0.07):
    """Fit evaluated object boxes without changing objects or camera angle.

    Call after selecting the output resolution and camera orientation. This
    helper deliberately rejects perspective cameras; preserving an authored
    perspective composition is a separate source-aware decision.
    """
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector

    if camera.type != "CAMERA" or camera.data.type != "ORTHO":
        raise ValueError("projection fitting requires an orthographic camera")
    objects = list(objects)
    if not objects:
        raise ValueError("projection fitting requires at least one framing object")
    if not math.isfinite(camera.data.ortho_scale) or camera.data.ortho_scale <= 0:
        raise ValueError("orthographic scale must be positive and finite")
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()

    def projected_bounds():
        points = []
        for obj in objects:
            evaluated = obj.evaluated_get(depsgraph)
            for corner in evaluated.bound_box:
                point = world_to_camera_view(
                    scene, camera, evaluated.matrix_world @ Vector(corner)
                )
                if not all(math.isfinite(value) for value in point) or point.z <= 0:
                    raise ValueError(
                        "framing object lies behind or outside a valid camera projection"
                    )
                points.append(point)
        return (
            min(point.x for point in points),
            max(point.x for point in points),
            min(point.y for point in points),
            max(point.y for point in points),
        )

    before = projected_bounds()
    original_scale = float(camera.data.ortho_scale)
    factor = orthographic_scale_factor(before, margin=margin)
    if factor > 1.0:
        camera.data.ortho_scale = original_scale * factor * 1.000001
        bpy.context.view_layer.update()
    after = projected_bounds()
    epsilon = 1e-5
    if not (
        after[0] >= margin - epsilon
        and after[1] <= 1 - margin + epsilon
        and after[2] >= margin - epsilon
        and after[3] <= 1 - margin + epsilon
    ):
        raise RuntimeError(
            "orthographic camera fit did not contain the framing objects"
        )
    return {
        "schema": "video-replay-camera-framing.v1",
        "method": "evaluated_bbox_raw_camera_projection",
        "camera": camera.name,
        "object_count": len(objects),
        "margin": margin,
        "projection_before": list(before),
        "projection_after": list(after),
        "scale_before": original_scale,
        "scale_after": float(camera.data.ortho_scale),
        "adjusted": factor > 1.0,
        "passed": True,
    }
