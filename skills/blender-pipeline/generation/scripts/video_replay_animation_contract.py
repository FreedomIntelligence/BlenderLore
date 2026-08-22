"""Pure planning helpers for bounded video-replay animation delivery."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


SCHEMA = "video-replay-animation-delivery-plan.v1"
ENGINE_PROBE_SCHEMA = "video-replay-render-engine-probe.v1"
GENERATION_RENDER_RECEIPT_SCHEMA = "video-replay-generation-render-receipt.v1"
POSTPROCESS_RENDER_RECEIPT_SCHEMA = "video-replay-postprocess-render-receipt.v1"
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 720
OUTPUT_FPS = 24
OUTPUT_SECONDS = 5.0
OUTPUT_FRAME_COUNT = int(OUTPUT_FPS * OUTPUT_SECONDS)
MAX_RENDER_FRAME_COUNT = 90
ROUTE_OVERRIDE_SCHEMA = "video-replay-route-override.v1"
SUPPORTED_RENDER_ENGINES = frozenset({"CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"})
EEVEE_FATAL_DIAGNOSTIC = re.compile(
    r"shader storage(?: buffer)? blocks?|"
    r"maximum supported storage buffer bindings|"
    r"failed to create shader|"
    r"vkCreateDevice[^\r\n]{0,360}(?:VK_ERROR_|failed)",
    re.I,
)

HIGH_TEMPORAL_COMPLEXITY = re.compile(
    r"fluid|smoke|fire|explosion|particle|cloth|soft[\s_-]*body|"
    r"rigid[\s_-]*body|hair|geometry[\s_-]*nodes?|simulation|"
    r"流体|烟|火|爆炸|粒子|布料|软体|刚体|毛发|几何节点|模拟",
    re.I,
)
MEDIUM_TEMPORAL_COMPLEXITY = re.compile(
    r"action|nla|armature|bone|shape[\s_-]*key|material|shader|"
    r"driver|object[\s_-]*transform|key[\s_-]*frame|animation|"
    r"骨骼|形态键|材质|着色器|驱动|物体|关键帧|动画",
    re.I,
)

# Only explicit, time-varying subject mechanisms may supplement Blender's
# runtime animation-data probe.  Camera motion, hair presence/dynamics and
# unverified metadata are intentionally absent: none of them proves visible
# subject animation on its own.
VERIFIED_TIME_MOTION_MECHANISMS = frozenset(
    {
        "action",
        "action_nla",
        "nla",
        "object_transform",
        "object_transform_animation",
        "bone",
        "bone_animation",
        "bone_animated_armatures",
        "animated_bones",
        "pose_bone_fcurves",
        "rig_pose",
        "rig_pose_animation",
        "shape_key",
        "shape_key_animation",
        "shape_key_fcurves",
        "shape_key_objects",
        "animated_shape_keys",
        "time_driver",
        "time_dependent_drivers",
        "time_driver_count",
        "time_drivers",
        "driver_uses_frame",
        "time_driven_objects",
        "animated_material",
        "animated_materials",
        "material_animation",
        "material_fcurves",
        "geometry_nodes_time",
        "geometry_nodes_animation",
        "animated_geometry_nodes",
        "particle",
        "particles",
        "particle_emitter",
        "animated_particle_systems",
        "particle_simulation",
        "dynamic_particle_systems",
        "cloth",
        "cloth_simulation",
        "cloth_simulations",
        "cloth_softbody",
        "soft_body",
        "softbody",
        "soft_body_simulation",
        "soft_body_simulations",
        "softbody_simulation",
        "softbody_simulations",
        "rigid_body",
        "rigidbody",
        "rigid_body_animation",
        "rigid_body_simulation",
        "rigid_body_simulations",
        "rigid_bodies",
        "fluid",
        "fluid_liquid",
        "fluid_simulation",
        "liquid",
        "liquid_simulation",
        "liquid_simulations",
        "smoke",
        "smoke_fire",
        "smoke_simulation",
        "smoke_simulations",
        "fire_simulation",
        "fire_simulations",
        "gas_simulation",
        "gas_simulations",
    }
)


@dataclass(frozen=True)
class AnimationDeliveryPlan:
    schema: str
    width: int
    height: int
    output_fps: int
    duration_seconds: float
    output_frame_count: int
    temporal_tier: str
    requested_render_fps: int
    render_frame_count: int
    source_frame_start: int
    source_frame_end: int
    source_frames: tuple[int, ...]
    input_sequence_fps: float
    interpolation: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_frames"] = list(self.source_frames)
        return value


@dataclass(frozen=True)
class RenderVisualProbe:
    sampled_pixels: int
    opaque_pixels: int
    mean_luma: float
    max_luma: float
    luma_range: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalized_motion_mechanisms(value: object) -> set[str]:
    """Return normalized explicit mechanism labels from a motion plan field."""

    if isinstance(value, Mapping):
        values = [key for key, present in value.items() if bool(present)]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
    elif isinstance(value, str):
        values = [value]
    else:
        return set()
    return {
        re.sub(r"[^0-9a-z]+", "_", str(item).strip().casefold()).strip("_")
        for item in values
        if str(item).strip()
    }


def motion_plan_has_verified_time_animation(
    motion_plan: Mapping[str, Any],
) -> bool:
    """Return whether the plan names a verified time-varying subject mechanism.

    The route decision separately requires ``motion_type=dynamic``.  This
    helper deliberately examines only ``motion_mechanisms`` so title keywords,
    presentation hints, camera-only motion, hair-only metadata and generic
    unverified animation claims cannot turn a static result into a video.
    """

    mechanisms = _normalized_motion_mechanisms(motion_plan.get("motion_mechanisms"))
    return bool(mechanisms & VERIFIED_TIME_MOTION_MECHANISMS)


def effective_route(
    requested_motion_type: str,
    *,
    verified_scene_animation: bool,
) -> tuple[str, str]:
    """Never manufacture motion solely to satisfy a requested dynamic route."""

    if str(requested_motion_type or "").strip() != "dynamic":
        return "static", ""
    if verified_scene_animation:
        return "dynamic", ""
    return "static", "dynamic_without_verified_scene_animation"


def analyze_rgba_pixels(
    values: Sequence[float],
    *,
    minimum_mean_luma: float = 0.01,
    minimum_max_luma: float = 0.05,
    minimum_luma_range: float = 0.02,
) -> RenderVisualProbe:
    """Detect a controlled render that silently returned a black frame.

    This is deliberately a narrow runtime check rather than an aesthetic
    quality score.  A valid probe must contain visible, non-uniform color.
    """

    pixel_count = len(values) // 4
    if pixel_count <= 0:
        return RenderVisualProbe(0, 0, 0.0, 0.0, 0.0, False)
    stride = max(1, pixel_count // 16384)
    lumas: list[float] = []
    opaque = 0
    for index in range(0, pixel_count, stride):
        offset = index * 4
        red, green, blue, alpha = (
            float(values[offset]),
            float(values[offset + 1]),
            float(values[offset + 2]),
            float(values[offset + 3]),
        )
        if alpha <= 0.01:
            continue
        opaque += 1
        lumas.append(max(0.0, 0.2126 * red + 0.7152 * green + 0.0722 * blue))
    if not lumas:
        return RenderVisualProbe(
            sampled_pixels=math.ceil(pixel_count / stride),
            opaque_pixels=0,
            mean_luma=0.0,
            max_luma=0.0,
            luma_range=0.0,
            passed=False,
        )
    mean_luma = sum(lumas) / len(lumas)
    maximum = max(lumas)
    luma_range = maximum - min(lumas)
    return RenderVisualProbe(
        sampled_pixels=math.ceil(pixel_count / stride),
        opaque_pixels=opaque,
        mean_luma=round(mean_luma, 8),
        max_luma=round(maximum, 8),
        luma_range=round(luma_range, 8),
        passed=(
            mean_luma >= minimum_mean_luma
            and maximum >= minimum_max_luma
            and luma_range >= minimum_luma_range
        ),
    )


def engine_candidate_verified(engine: str, candidate: Mapping[str, Any]) -> bool:
    """Return whether one probe proves both GPU use and visible output."""

    normalized = str(engine or "").strip().upper()
    if normalized not in SUPPORTED_RENDER_ENGINES:
        return False
    if candidate.get("status") != "verified":
        return False
    if candidate.get("gpu_process_attested") is not True:
        return False
    visual = candidate.get("visual_probe")
    if not isinstance(visual, Mapping) or visual.get("passed") is not True:
        return False
    if normalized == "CYCLES":
        return (
            candidate.get("device_set_exact") is True
            and candidate.get("cpu_device_enabled") is False
            and str(candidate.get("backend") or "").upper() in {"CUDA", "OPTIX"}
        )
    diagnostic = str(candidate.get("diagnostic") or "")
    return EEVEE_FATAL_DIAGNOSTIC.search(diagnostic) is None


def select_engine_from_probe(
    candidates: Mapping[str, Mapping[str, Any]],
) -> str:
    """Prefer attested GPU Cycles, then visually attested Eevee, else block."""

    cycles = candidates.get("CYCLES")
    if isinstance(cycles, Mapping) and engine_candidate_verified("CYCLES", cycles):
        return "CYCLES"
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        candidate = candidates.get(engine)
        if isinstance(candidate, Mapping) and engine_candidate_verified(
            engine, candidate
        ):
            return engine
    raise ValueError("no GPU-attested non-black render engine is available")


def validate_engine_probe_receipt(
    receipt: Mapping[str, Any],
) -> list[str]:
    """Validate the immutable engine decision consumed by both render stages."""

    issues: list[str] = []
    if receipt.get("schema") != ENGINE_PROBE_SCHEMA:
        issues.append("render engine probe schema differs")
    candidates = receipt.get("candidates")
    if not isinstance(candidates, Mapping):
        return [*issues, "render engine probe candidates are missing"]
    selected = str(receipt.get("selected_engine") or "").strip().upper()
    if selected not in SUPPORTED_RENDER_ENGINES:
        issues.append(f"selected render engine is invalid: {selected}")
        return issues
    candidate = candidates.get(selected)
    if not isinstance(candidate, Mapping) or not engine_candidate_verified(
        selected, candidate
    ):
        issues.append("selected render engine lacks complete GPU/visual proof")
    try:
        policy_selected = select_engine_from_probe(candidates)
    except ValueError as exc:
        issues.append(str(exc))
    else:
        if policy_selected != selected:
            issues.append(f"selected render engine={selected} policy={policy_selected}")
    expected = str(receipt.get("expected_gpu_uuid") or "").strip().lower()
    observed = (
        str(
            candidate.get("observed_gpu_uuid") if isinstance(candidate, Mapping) else ""
        )
        .strip()
        .lower()
    )
    if not expected or observed != expected:
        issues.append(
            f"render GPU UUID mismatch expected={expected or 'missing'} "
            f"observed={observed or 'missing'}"
        )
    return issues


def validate_generation_render_receipt(
    receipt: Mapping[str, Any],
    engine_probe: Mapping[str, Any],
) -> list[str]:
    """Prove generation reused the exact engine decision without CPU fallback."""

    issues: list[str] = []
    if receipt.get("schema") != GENERATION_RENDER_RECEIPT_SCHEMA:
        issues.append("generation render receipt schema differs")
    engine = str(receipt.get("render_engine") or "").strip().upper()
    selected = str(engine_probe.get("selected_engine") or "").strip().upper()
    if engine != selected:
        issues.append(
            f"generation engine={engine or 'missing'} "
            f"probe engine={selected or 'missing'}"
        )
    if receipt.get("cycles_cpu_fallback_allowed") is not False:
        issues.append("generation permitted Cycles CPU fallback")
    if receipt.get("gpu_process_attested") is not True:
        issues.append("generation Blender PID lacks GPU attestation")
    expected = str(engine_probe.get("expected_gpu_uuid") or "").strip().lower()
    observed = str(receipt.get("observed_gpu_uuid") or "").strip().lower()
    if not expected or observed != expected:
        issues.append(
            f"generation GPU UUID mismatch expected={expected or 'missing'} "
            f"observed={observed or 'missing'}"
        )
    visual = receipt.get("visual_probe")
    if not isinstance(visual, Mapping) or visual.get("passed") is not True:
        issues.append("generation preview is blank or unverified")
    backend = str(receipt.get("cycles_backend") or "").strip().upper()
    expected_backend = (
        str(engine_probe.get("selected_cycles_backend") or "").strip().upper()
    )
    if engine == "CYCLES":
        if backend not in {"CUDA", "OPTIX"} or backend != expected_backend:
            issues.append(
                f"generation Cycles backend={backend or 'missing'} "
                f"probe backend={expected_backend or 'missing'}"
            )
    elif backend:
        issues.append("Eevee generation unexpectedly records a Cycles backend")
    return issues


def validate_postprocess_render_receipt(
    receipt: Mapping[str, Any],
    engine_probe: Mapping[str, Any],
) -> list[str]:
    """Prove the asset-loaded postprocess did not switch engine or device."""

    issues: list[str] = []
    if receipt.get("schema") != POSTPROCESS_RENDER_RECEIPT_SCHEMA:
        issues.append("postprocess render receipt schema differs")
    engine = str(receipt.get("render_engine") or "").strip().upper()
    selected = str(engine_probe.get("selected_engine") or "").strip().upper()
    if engine != selected:
        issues.append(
            f"postprocess engine={engine or 'missing'} "
            f"probe engine={selected or 'missing'}"
        )
    if receipt.get("cycles_cpu_fallback_allowed") is not False:
        issues.append("postprocess permitted Cycles CPU fallback")
    if receipt.get("gpu_process_attested") is not True:
        issues.append("postprocess Blender PID lacks GPU attestation")
    if receipt.get("output_visual_attested") is not True:
        issues.append("postprocess output is blank or unverified")
    expected = str(engine_probe.get("expected_gpu_uuid") or "").strip().lower()
    observed = str(receipt.get("observed_gpu_uuid") or "").strip().lower()
    if not expected or observed != expected:
        issues.append(
            f"postprocess GPU UUID mismatch expected={expected or 'missing'} "
            f"observed={observed or 'missing'}"
        )
    backend = str(receipt.get("cycles_backend") or "").strip().upper()
    expected_backend = (
        str(engine_probe.get("selected_cycles_backend") or "").strip().upper()
    )
    if engine == "CYCLES":
        if backend not in {"CUDA", "OPTIX"} or backend != expected_backend:
            issues.append(
                f"postprocess Cycles backend={backend or 'missing'} "
                f"probe backend={expected_backend or 'missing'}"
            )
    elif backend:
        issues.append("Eevee postprocess unexpectedly records a Cycles backend")
    return issues


def _motion_text(motion_plan: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "motion_mechanisms",
        "evidence_terms",
        "strong_evidence_terms",
        "motion_type",
        "presentation_profile",
        "final_effect_requirement",
    ):
        value = motion_plan.get(key)
        if isinstance(value, Mapping):
            values.extend(f"{name}:{item}" for name, item in value.items())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return " ".join(values)


def temporal_sampling_tier(
    motion_plan: Mapping[str, Any],
) -> tuple[str, int]:
    """Choose unique render FPS without changing final delivery FPS."""

    text = _motion_text(motion_plan)
    if HIGH_TEMPORAL_COMPLEXITY.search(text):
        return "high", 18
    if MEDIUM_TEMPORAL_COMPLEXITY.search(text):
        return "medium", 16
    return "low", 12


def sample_source_frames(start: int, end: int, requested_count: int) -> tuple[int, ...]:
    if end < start:
        start, end = end, start
    available = end - start + 1
    count = max(1, min(int(requested_count), available))
    if count == 1:
        return (start,)
    frames = [
        int(round(start + index * (end - start) / (count - 1)))
        for index in range(count)
    ]
    # Rounding can only create duplicates when the requested count exceeds
    # the available integer frames, but deduplicate defensively and retain
    # both timeline endpoints.
    unique = tuple(dict.fromkeys(frames))
    if unique[-1] != end:
        unique = (*unique[:-1], end)
    return unique


def build_animation_delivery_plan(
    motion_plan: Mapping[str, Any],
    *,
    source_frame_start: int,
    source_frame_end: int,
    output_width: int = OUTPUT_WIDTH,
    output_height: int = OUTPUT_HEIGHT,
    output_fps: int = OUTPUT_FPS,
    duration_seconds: float = OUTPUT_SECONDS,
) -> AnimationDeliveryPlan:
    """Build a delivery contract plus bounded source sampling.

    Legacy callers retain the 720p/24fps/5s defaults.  Research workloads
    pass their explicit contract so this helper cannot silently overwrite it.
    """

    tier, render_fps = temporal_sampling_tier(motion_plan)
    width = max(1, int(output_width))
    height = max(1, int(output_height))
    fps = max(1, int(output_fps))
    seconds = max(0.1, float(duration_seconds))
    output_frame_count = int(round(fps * seconds))
    requested = min(
        MAX_RENDER_FRAME_COUNT,
        max(2, int(round(render_fps * seconds))),
    )
    source_frames = sample_source_frames(
        int(source_frame_start),
        int(source_frame_end),
        requested,
    )
    render_count = len(source_frames)
    input_fps = render_count / seconds
    return AnimationDeliveryPlan(
        schema=SCHEMA,
        width=width,
        height=height,
        output_fps=fps,
        duration_seconds=seconds,
        output_frame_count=output_frame_count,
        temporal_tier=tier,
        requested_render_fps=render_fps,
        render_frame_count=render_count,
        source_frame_start=int(source_frame_start),
        source_frame_end=int(source_frame_end),
        source_frames=source_frames,
        input_sequence_fps=round(input_fps, 8),
        interpolation=(
            "none"
            if render_count >= output_frame_count
            else "motion_compensated_with_cfr_fallback"
        ),
    )


def select_postprocess_engine(
    requested: str,
    *,
    cycles_gpu_verified: bool,
    eevee_next_available: bool = True,
) -> str:
    """Never permit an unverified Cycles request to fall back to CPU."""

    normalized = str(requested or "CYCLES").strip().upper()
    if normalized == "CYCLES" and cycles_gpu_verified:
        return "CYCLES"
    if eevee_next_available:
        return "BLENDER_EEVEE_NEXT"
    return "BLENDER_EEVEE"


def parse_frame_rate(value: object) -> float:
    text = str(value or "0/1")
    numerator, separator, denominator = text.partition("/")
    try:
        if not separator:
            return float(numerator)
        return float(numerator) / max(float(denominator), 1.0)
    except (TypeError, ValueError):
        return 0.0


def validate_delivery_probe(probe: Mapping[str, Any]) -> list[str]:
    """Return contract issues for an ffprobe-derived payload."""

    issues: list[str] = []
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    fps = float(probe.get("fps") or 0.0)
    duration = float(probe.get("duration_seconds") or 0.0)
    frame_count = int(
        probe.get("frame_count") or round(max(duration, 0.0) * max(fps, 0.0))
    )
    if (width, height) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        issues.append(
            f"resolution={width}x{height} expected={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}"
        )
    if not math.isclose(fps, OUTPUT_FPS, abs_tol=0.1):
        issues.append(f"fps={fps:.3f} expected={OUTPUT_FPS}")
    if not 4.8 <= duration <= 5.2:
        issues.append(f"duration={duration:.3f}s expected={OUTPUT_SECONDS:.1f}s")
    if not 118 <= frame_count <= 122:
        issues.append(f"frame_count={frame_count} expected_about={OUTPUT_FRAME_COUNT}")
    return issues


def validate_plan_receipt(receipt: Mapping[str, Any]) -> list[str]:
    """Validate render-frame and engine safeguards recorded by Blender."""

    issues: list[str] = []
    if receipt.get("schema") != SCHEMA:
        issues.append("animation delivery plan schema differs")
    expected = {
        "width": OUTPUT_WIDTH,
        "height": OUTPUT_HEIGHT,
        "output_fps": OUTPUT_FPS,
        "duration_seconds": OUTPUT_SECONDS,
        "output_frame_count": OUTPUT_FRAME_COUNT,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            issues.append(f"{key}={receipt.get(key)} expected={value}")
    rendered = int(receipt.get("render_frame_count") or 0)
    if not 1 <= rendered <= MAX_RENDER_FRAME_COUNT:
        issues.append(f"render_frame_count={rendered} max={MAX_RENDER_FRAME_COUNT}")
    engine = str(receipt.get("postprocess_render_engine") or "")
    policy = str(receipt.get("postprocess_engine_policy") or "")
    if receipt.get("cycles_cpu_fallback_allowed") is not False:
        issues.append("Cycles CPU fallback was not prohibited")
    if engine == "CYCLES" and policy != "externally_attested_cycles_gpu":
        issues.append("Cycles engine lacks external GPU attestation")
    if engine not in {
        "CYCLES",
        "BLENDER_EEVEE_NEXT",
        "BLENDER_EEVEE",
    }:
        issues.append(f"postprocess render engine is invalid: {engine}")
    return issues
