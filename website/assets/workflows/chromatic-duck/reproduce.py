
import math
from pathlib import Path
import bpy
from mathutils import Vector

OUTPUT_DIR = Path(__file__).resolve().parent

def _clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

def _load_linked_source_if_configured():
    import json
    config_path = OUTPUT_DIR.parent / "linked_source.json"
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text(encoding="utf-8"))
    relative = config.get("selected_model_relative", "")
    source = (OUTPUT_DIR.parent / "linked_source" / relative).resolve()
    if not source.is_file():
        raise RuntimeError("configured linked source model is missing")
    suffix = source.suffix.lower()
    if suffix == ".blend":
        bpy.ops.wm.open_mainfile(filepath=str(source))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(source))
    elif suffix == ".obj":
        try:
            bpy.ops.wm.obj_import(filepath=str(source))
        except Exception:
            bpy.ops.import_scene.obj(filepath=str(source))
    elif suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(source))
    else:
        raise RuntimeError("unsupported linked source model format")
    import hashlib
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != config.get("selected_model_sha256"):
        raise RuntimeError("linked source model hash mismatch")
    scene = bpy.context.scene
    scene["video2blender_type2_source_sha256"] = digest
    source_objects = list(scene.objects)
    for obj in source_objects:
        obj["video2blender_linked_source"] = True
    def fingerprint(obj):
        mesh = getattr(obj, "data", None)
        return (
            tuple(round(value, 7) for row in obj.matrix_world for value in row),
            tuple(slot.material.name if slot.material else "" for slot in obj.material_slots),
            tuple((modifier.name, modifier.type) for modifier in obj.modifiers),
            len(getattr(mesh, "vertices", ())),
            len(getattr(mesh, "polygons", ())),
            bool(obj.hide_render),
        )
    return {
        "config": config,
        "source_objects": source_objects,
        "source_names": {obj.name for obj in source_objects},
        "source_fingerprints": [(obj, fingerprint(obj)) for obj in source_objects],
        "fingerprint": fingerprint,
    }

def _write_linked_source_runtime_receipt(linked_state):
    if not linked_state:
        return
    import json
    source_objects = linked_state["source_objects"]
    live_objects = [obj for obj in source_objects if obj.name in bpy.data.objects]
    generated_objects = [
        obj for obj in bpy.context.scene.objects
        if obj.name not in linked_state["source_names"]
    ]
    modified_source_objects = [
        obj.name
        for obj, before in linked_state["source_fingerprints"]
        if obj.name in bpy.data.objects and linked_state["fingerprint"](obj) != before
    ]
    integrated_source_objects = [
        obj.name
        for obj in live_objects
        if bool(obj.get("video2blender_linked_source_integrated", False))
        and not obj.hide_render
    ]
    source_renderables = [
        obj for obj in source_objects
        if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}
    ]
    live_source_renderables = [
        obj for obj in source_renderables
        if obj.name in bpy.data.objects and not obj.hide_render
    ]
    actively_used_source_renderables = sorted(
        {
            obj.name
            for obj in live_source_renderables
            if (
                obj.name in modified_source_objects
                or obj.name in integrated_source_objects
            )
        }
    )
    actively_used = bool(live_source_renderables) and bool(
        actively_used_source_renderables
    )
    status = "loaded_and_used" if actively_used else (
        "loaded_but_unmodified" if live_objects else "loaded_but_discarded"
    )
    payload = {
        "schema": "video-replay-linked-source-runtime-receipt.v1",
        "status": status,
        "selected_model_sha256": linked_state["config"].get("selected_model_sha256", ""),
        "source_object_count_loaded": len(source_objects),
        "source_object_count_preserved": len(live_objects),
        "source_renderable_object_count": len(source_renderables),
        "preserved_source_renderable_object_count": len(live_source_renderables),
        "actively_used_source_renderable_count": len(
            actively_used_source_renderables
        ),
        "actively_used_source_renderable_objects": actively_used_source_renderables[
            :256
        ],
        "preserved_source_objects": sorted(obj.name for obj in live_objects)[:256],
        "modified_source_objects": sorted(modified_source_objects)[:256],
        "integrated_source_objects": sorted(integrated_source_objects)[:256],
        "meaningful_use_evidence": (
            "source_object_modified_or_explicitly_integrated_v2"
        ),
        "generated_or_supplemental_object_count": len(generated_objects),
        "generated_or_supplemental_objects": sorted(obj.name for obj in generated_objects)[:256],
        "usage_policy": "video_primary_linked_asset_knowledge_assisted_v1",
    }
    (OUTPUT_DIR / "linked_source_runtime_receipt.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def _renderable_objects():
    return [o for o in bpy.context.scene.objects if o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"} and o.visible_get() and not o.hide_render]

def _bbox(objects):
    mins = Vector((math.inf, math.inf, math.inf))
    maxs = Vector((-math.inf, -math.inf, -math.inf))
    for obj in objects:
        for corner in obj.bound_box:
            p = obj.matrix_world @ Vector(corner)
            mins.x = min(mins.x, p.x); mins.y = min(mins.y, p.y); mins.z = min(mins.z, p.z)
            maxs.x = max(maxs.x, p.x); maxs.y = max(maxs.y, p.y); maxs.z = max(maxs.z, p.z)
    return mins, maxs

def _look_at(obj, target):
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

def _attest_exact_gpu_process():
    import os as _os
    expected = _os.environ.get("TOTAL_ASSET_EXPECTED_GPU_UUID", "").strip().lower()
    if not expected.startswith("gpu-"):
        return None
    _marker = _os.environ.get(
        "VIDEO2BLENDER_GPU_PROCESS_ATTESTED", ""
    ).strip()
    _marker_uuid = _os.environ.get(
        "VIDEO2BLENDER_GPU_PROCESS_ATTESTED_UUID", ""
    ).strip().lower()
    if _marker != "1" or _marker_uuid != expected:
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
            f"expected_gpu_uuid={expected} "
            f"process_monitor_marker={_marker or 'missing'} "
            f"process_monitor_uuid={_marker_uuid or 'missing'}"
        )
    return expected

def _assert_render_result_not_black(_render_path=None):
    # Blender 5.1 background renders from a non-main scene can leave the
    # in-memory Render Result pixel collection empty even though write_still
    # produced a valid image.  The published file is the real contract, so
    # validate that file when one is available.
    _loaded_from_disk = False
    if _render_path is not None:
        try:
            _image = bpy.data.images.load(
                str(_render_path),
                check_existing=False,
            )
            _loaded_from_disk = True
        except Exception as _exc:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_OUTPUT_BLACK "
                f"render_file_unreadable={type(_exc).__name__}"
            ) from _exc
    else:
        _image = bpy.data.images.get("Render Result")
        if _image is None:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_OUTPUT_BLACK render_result_missing"
            )
    try:
        _pixels = list(_image.pixels)
    finally:
        if _loaded_from_disk:
            bpy.data.images.remove(_image)
    _count = len(_pixels) // 4
    _stride = max(1, _count // 16384)
    _lumas = []
    for _index in range(0, _count, _stride):
        _offset = _index * 4
        if float(_pixels[_offset + 3]) <= 0.01:
            continue
        _red, _green, _blue = (
            float(_pixels[_offset]),
            float(_pixels[_offset + 1]),
            float(_pixels[_offset + 2]),
        )
        _lumas.append(
            max(0.0, 0.2126 * _red + 0.7152 * _green + 0.0722 * _blue)
        )
    _mean = sum(_lumas) / max(len(_lumas), 1)
    _maximum = max(_lumas, default=0.0)
    _range = _maximum - min(_lumas, default=0.0)
    if _mean < 0.0002 or _maximum < 0.002 or _range < 0.0001:
        raise RuntimeError(
            "VIDEO_REPLAY_RENDER_OUTPUT_BLACK "
            f"mean_luma={_mean:.8f} max_luma={_maximum:.8f} "
            f"luma_range={_range:.8f}"
        )
    return {
        "mean_luma": round(_mean, 8),
        "max_luma": round(_maximum, 8),
        "luma_range": round(_range, 8),
    }

def _ensure_camera_lights_and_outputs():
    objects = _renderable_objects()
    if not objects:
        raise RuntimeError("build_scene produced no renderable objects")
    mins, maxs = _bbox(objects)
    center = (mins + maxs) * 0.5
    size = maxs - mins
    radius = max(size.length * 0.5, 0.6)
    for obj in list(bpy.context.scene.objects):
        if obj.type in {"CAMERA", "LIGHT"}:
            bpy.data.objects.remove(obj, do_unlink=True)
    data = bpy.data.cameras.new("Paper12_Camera")
    cam = bpy.data.objects.new("Paper12_Camera", data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    cam = bpy.context.scene.camera
    cam.data.type = "ORTHO"
    ortho_scale_mult = float(__import__("os").environ.get("PAPER12_ORTHO_SCALE_MULT", "1.45"))
    cam.data.ortho_scale = max(size.x, size.y, size.z, 1.0) * ortho_scale_mult
    cam_vec_text = __import__("os").environ.get("PAPER12_CAMERA_VECTOR", "1.8,-2.4,1.25")
    try:
        cam_vec = Vector(tuple(float(x.strip()) for x in cam_vec_text.split(",")[:3]))
    except Exception:
        cam_vec = Vector((1.8, -2.4, 1.25))
    cam.location = center + cam_vec.normalized() * radius * 3.2
    _look_at(cam, center)
    light_scale = float(__import__("os").environ.get("PAPER12_LIGHT_SCALE", "1.0"))
    for name, offset, energy in [
        ("Key", Vector((2.5, -3.0, 3.0)), 500),
        ("Fill", Vector((-2.4, 2.2, 2.0)), 160),
        ("Rim", Vector((-2.0, -1.8, 2.5)), 220),
    ]:
        light_data = bpy.data.lights.new(name, "AREA")
        light = bpy.data.objects.new(name, light_data)
        bpy.context.collection.objects.link(light)
        light.location = center + offset.normalized() * radius * 3.0
        light_data.energy = energy * light_scale
        light_data.size = max(radius * 1.8, 1.8)
        _look_at(light, center)
    scene = bpy.context.scene
    _render_default_samples = "160" if __import__("os").environ.get("PAPER12_QUALITY_PROFILE", "draft").lower() == "final" else "48"
    _render_samples = int(__import__("os").environ.get("PAPER12_RENDER_SAMPLES", _render_default_samples))
    _render_engine = __import__("os").environ.get("VIDEO2BLENDER_RENDER_ENGINE", "EEVEE").strip().upper()
    if _render_engine == "CYCLES":
        if __import__("os").environ.get("VIDEO2BLENDER_CYCLES_GPU_VERIFIED", "0") != "1":
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                "generation_cycles_gpu_proof_missing"
            )
        scene.render.engine = "CYCLES"
        scene.cycles.samples = _render_samples
        scene.cycles.use_denoising = True
        _backend = __import__("os").environ.get(
            "VIDEO2BLENDER_CYCLES_BACKEND", ""
        ).strip().upper()
        if _backend not in {"CUDA", "OPTIX"}:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                f"unsupported_cycles_backend={_backend or 'empty'}"
            )
        _addon = bpy.context.preferences.addons.get("cycles")
        if not _addon:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED cycles_addon_missing"
            )
        try:
            _addon.preferences.compute_device_type = _backend
            _addon.preferences.get_devices()
            _devices = list(_addon.preferences.devices)
        except Exception as _exc:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                f"cycles_device_enumeration={type(_exc).__name__}"
            ) from _exc
        _gpu_devices = [
            _device for _device in _devices
            if str(getattr(_device, "type", "")).upper() == _backend
        ]
        if len(_gpu_devices) != 1:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                f"{_backend.lower()}_gpu_count={len(_gpu_devices)} expected=1"
            )
        _selected_device = _gpu_devices[0]
        for _device in _devices:
            _device.use = _device is _selected_device
        if any(
            bool(getattr(_device, "use", False))
            for _device in _devices
            if str(getattr(_device, "type", "")).upper() == "CPU"
        ):
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                "cycles_cpu_device_enabled"
            )
        scene.cycles.device = "GPU"
    else:
        if _render_engine not in {
            "EEVEE", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"
        }:
            raise RuntimeError(
                "VIDEO_REPLAY_RENDER_ENGINE_ATTESTATION_FAILED "
                f"unsupported_selected_engine={_render_engine or 'empty'}"
            )
        try:
            scene.render.engine = (
                "BLENDER_EEVEE"
                if _render_engine == "BLENDER_EEVEE"
                else "BLENDER_EEVEE_NEXT"
            )
        except (TypeError, ValueError):
            scene.render.engine = "BLENDER_EEVEE"
        if hasattr(scene, "eevee"):
            scene.eevee.taa_render_samples = max(_render_samples, 64)
    scene.render.resolution_x = int(__import__("os").environ.get("PAPER12_RENDER_W", "1280"))
    scene.render.resolution_y = int(__import__("os").environ.get("PAPER12_RENDER_H", "720"))
    # Blender 5 removed/renamed parts of the legacy Filmic enum. Setting a
    # missing enum can abort the Python script while Blender still exits 0,
    # leaving no asset.blend behind. Prefer the current transform while
    # retaining compatibility with older production runtimes.
    for _view_transform in ("AgX", "Filmic", "Standard"):
        try:
            scene.view_settings.view_transform = _view_transform
            break
        except (TypeError, ValueError):
            pass
    for _look in ("AgX - Medium High Contrast", "Medium High Contrast", "None"):
        try:
            scene.view_settings.look = _look
            break
        except (TypeError, ValueError):
            pass
    scene.view_settings.exposure = float(
        __import__("os").environ.get("PAPER12_EXPOSURE", "0.15")
    )
    world_color = __import__("os").environ.get("PAPER12_WORLD_COLOR", "").strip()
    if world_color and scene.world:
        try:
            vals = tuple(float(x.strip()) for x in world_color.split(",")[:3])
            if len(vals) == 3:
                scene.world.color = vals
        except Exception:
            pass
    scene.render.film_transparent = __import__("os").environ.get("PAPER12_FILM_TRANSPARENT", "1") != "0"
    # Preserve the editable asset before producing the auxiliary preview.
    # Some linked Blender files expose a source-specific ImageFormatSettings
    # enum containing only FFMPEG.  Assigning PNG then raises TypeError while
    # Blender itself can still exit 0, which used to lose the entire result.
    bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT_DIR / "asset.blend"))
    _preview = OUTPUT_DIR / "final.png"
    try:
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGBA"
        scene.render.filepath = str(_preview)
        bpy.ops.render.render(write_still=True)
    except (TypeError, ValueError):
        # Render a one-frame movie and extract its first frame.  This path
        # keeps the source scene's constrained FFMPEG enum intact while still
        # satisfying the deterministic final.png review contract.
        import shutil as _shutil
        import subprocess as _subprocess
        _preview_movie = OUTPUT_DIR / "render_preview.mp4"
        _frame_start, _frame_end = scene.frame_start, scene.frame_end
        try:
            scene.render.image_settings.file_format = "FFMPEG"
            scene.render.ffmpeg.format = "MPEG4"
            scene.render.ffmpeg.codec = "H264"
            scene.render.ffmpeg.constant_rate_factor = "HIGH"
            scene.render.filepath = str(_preview_movie)
            scene.frame_start = scene.frame_current
            scene.frame_end = scene.frame_current
            bpy.ops.render.render(animation=True)
            _ffmpeg = (
                __import__("os").environ.get("TOTAL_ASSET_FFMPEG")
                or _shutil.which("ffmpeg")
            )
            if not _ffmpeg:
                raise RuntimeError("ffmpeg is unavailable for constrained still output")
            _subprocess.run(
                [_ffmpeg, "-y", "-i", str(_preview_movie), "-frames:v", "1", str(_preview)],
                check=True,
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
        finally:
            scene.frame_start, scene.frame_end = _frame_start, _frame_end
            _preview_movie.unlink(missing_ok=True)
    _preview_visual = _assert_render_result_not_black(_preview)
    _observed_gpu_uuid = _attest_exact_gpu_process()
    import json as _json
    _generation_receipt = {
        "schema": "video-replay-generation-render-receipt.v1",
        "render_engine": scene.render.engine,
        "cycles_backend": (
            __import__("os").environ.get(
                "VIDEO2BLENDER_CYCLES_BACKEND", ""
            ).strip().upper()
            if scene.render.engine == "CYCLES"
            else ""
        ),
        "cycles_cpu_fallback_allowed": _observed_gpu_uuid is None,
        "gpu_process_attested": _observed_gpu_uuid is not None,
        "observed_gpu_uuid": _observed_gpu_uuid,
        "visual_probe": {"passed": True, **_preview_visual},
    }
    _generation_receipt_path = OUTPUT_DIR / "generation_render_receipt.json"
    _generation_receipt_tmp = OUTPUT_DIR / ".generation_render_receipt.json.tmp"
    _generation_receipt_tmp.write_text(
        _json.dumps(
            _generation_receipt,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    _generation_receipt_tmp.replace(_generation_receipt_path)


# ---- isolated model-generated scene builder ----

# Execute model output without sharing the deterministic wrapper namespace.
import builtins as _video2blender_builtins

_video2blender_allowed_import_roots = ('bmesh', 'bpy', 'colorsys', 'math', 'mathutils', 'random')

def _video2blender_safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = str(name or "").partition(".")[0]
    if level or root not in _video2blender_allowed_import_roots:
        raise ImportError("generated import is not allowed: " + str(name))
    imported = _video2blender_builtins.__import__(
        name, globals, locals, fromlist, level
    )
    if root == "bpy":
        return _video2blender_safe_bpy
    return imported

class _Video2BlenderSafeBpy:
    __slots__ = ("_video2blender_value", "_video2blender_path")

    def __init__(self, value, path=("bpy",)):
        object.__setattr__(self, "_video2blender_value", value)
        object.__setattr__(self, "_video2blender_path", tuple(path))

    def __getattribute__(self, name):
        if str(name).startswith("_"):
            raise AttributeError("generated bpy private access is forbidden")
        value = object.__getattribute__(self, "_video2blender_value")
        path = object.__getattribute__(self, "_video2blender_path")
        candidate = (*path, str(name))
        if (
            name == "app"
            or "texts" in candidate
            or "script" in candidate
            or candidate[-1] in (
                "console", "extension", "extensions", "preferences",
                "text",
            )
            or "driver" in candidate[-1].lower()
            or candidate[-1] in (
                "addon_enable", "addon_install", "as_module",
                "context_set_value", "driver", "driver_add", "execfile",
                "load_scripts", "modules_from_path", "open_mainfile",
                "package_install", "python_file_run",
                "read_factory_settings", "read_factory_userpref",
                "read_homefile", "recover_auto_save",
                "recover_last_session",
            )
            or (
                len(candidate) >= 3
                and candidate[1] == "ops"
                and candidate[2] in (
                    "console", "extension", "extensions", "preferences"
                )
            )
            or (
                len(candidate) >= 3
                and candidate[1] == "ops"
                and candidate[2] == "text"
            )
        ):
            raise AttributeError(
                "generated Blender text/script execution is forbidden"
            )
        result = getattr(value, name)
        if (
            candidate == ("bpy", "data")
            or (
                candidate[:2] == ("bpy", "ops")
                and len(candidate) <= 3
            )
            or candidate == ("bpy", "utils")
        ):
            return _Video2BlenderSafeBpy(result, candidate)
        return result

_video2blender_safe_bpy = _Video2BlenderSafeBpy(bpy)

_video2blender_safe_builtins = dict(vars(_video2blender_builtins))
for _video2blender_name in (
    "breakpoint", "compile", "eval", "exec", "exit", "globals", "help",
    "input", "locals", "open", "quit", "setattr", "delattr", "vars"
):
    _video2blender_safe_builtins.pop(_video2blender_name, None)
_video2blender_safe_builtins["__import__"] = _video2blender_safe_import
_video2blender_generated_namespace = {
    "__builtins__": _video2blender_safe_builtins,
    "__name__": "__video2blender_generated__",
    "bpy": _video2blender_safe_bpy,
    "math": math,
    "Vector": Vector,
}
_video2blender_generated_source = 'import bpy\nimport math\nfrom mathutils import Vector\n\n\ndef build_scene():\n    # Object tree:\n    # IridescentDuck\n    #   Body, Head, Arms, Legs\n    #   Belly patch\n    #   Upper/Lower bill\n    #   Eye whites, pupils, eye highlights\n    #   Feet\n    #   Thin iridescent rim shells\n    #   Attached surface speckles\n    #   Nearby emissive sparkle points\n\n    def smooth_object(obj):\n        if obj.type == \'MESH\':\n            for polygon in obj.data.polygons:\n                polygon.use_smooth = True\n\n    def set_principled_input(node, names, value):\n        for name in names:\n            socket = node.inputs.get(name)\n            if socket is not None:\n                socket.default_value = value\n                return socket\n        return None\n\n    def make_simple_material(name, color, roughness=0.35, metallic=0.0,\n                             emission_color=None, emission_strength=0.0):\n        material = bpy.data.materials.new(name)\n        material.use_nodes = True\n        nodes = material.node_tree.nodes\n        principled = nodes.get("Principled BSDF")\n\n        set_principled_input(principled, ("Base Color",), color)\n        set_principled_input(principled, ("Roughness",), roughness)\n        set_principled_input(principled, ("Metallic",), metallic)\n\n        if emission_color is not None:\n            set_principled_input(\n                principled, ("Emission Color", "Emission"), emission_color\n            )\n            set_principled_input(\n                principled, ("Emission Strength",), emission_strength\n            )\n        return material\n\n    def make_iridescent_material():\n        material = bpy.data.materials.new("Material.0_Iridescent_Pearl")\n        material.use_nodes = True\n        nodes = material.node_tree.nodes\n        links = material.node_tree.links\n        nodes.clear()\n\n        output = nodes.new("ShaderNodeOutputMaterial")\n        output.name = "Iridescent Material Output"\n        output.location = (760, 40)\n\n        principled = nodes.new("ShaderNodeBsdfPrincipled")\n        principled.name = "Glossy Iridescent Body"\n        principled.location = (470, 40)\n\n        geometry = nodes.new("ShaderNodeNewGeometry")\n        geometry.name = "Surface Coordinates"\n        geometry.location = (-760, 80)\n\n        noise_large = nodes.new("ShaderNodeTexNoise")\n        noise_large.name = "Large Flowing Color Clouds"\n        noise_large.location = (-530, 170)\n        noise_large.noise_dimensions = \'3D\'\n        if \'Scale\' in noise_large.inputs:\n            noise_large.inputs[\'Scale\'].default_value = 1.65\n        if \'Detail\' in noise_large.inputs:\n            noise_large.inputs[\'Detail\'].default_value = 4.2\n        if \'Roughness\' in noise_large.inputs:\n            noise_large.inputs[\'Roughness\'].default_value = 0.72\n        if \'Distortion\' in noise_large.inputs:\n            noise_large.inputs[\'Distortion\'].default_value = 0.38\n\n        noise_fine = nodes.new("ShaderNodeTexNoise")\n        noise_fine.name = "Fine Pearlescent Mottle"\n        noise_fine.location = (-530, -110)\n        noise_fine.noise_dimensions = \'3D\'\n        if \'Scale\' in noise_fine.inputs:\n            noise_fine.inputs[\'Scale\'].default_value = 6.5\n        if \'Detail\' in noise_fine.inputs:\n            noise_fine.inputs[\'Detail\'].default_value = 3.0\n        if \'Roughness\' in noise_fine.inputs:\n            noise_fine.inputs[\'Roughness\'].default_value = 0.62\n\n        mix_noise = nodes.new("ShaderNodeMix")\n        mix_noise.name = "Continuous Mottle Mix"\n        mix_noise.data_type = \'FLOAT\'\n        mix_noise.location = (-270, 120)\n        if \'Factor\' in mix_noise.inputs:\n            mix_noise.inputs[\'Factor\'].default_value = 0.34\n\n        ramp = nodes.new("ShaderNodeValToRGB")\n        ramp.name = "Blue Cyan Purple Pink Green Pearl Ramp"\n        ramp.location = (-20, 130)\n        color_ramp = ramp.color_ramp\n\n        while len(color_ramp.elements) > 2:\n            color_ramp.elements.remove(color_ramp.elements[-1])\n\n        elements = color_ramp.elements\n        elements[0].position = 0.0\n        elements[0].color = (0.012, 0.025, 0.18, 1.0)\n        elements[1].position = 1.0\n        elements[1].color = (0.95, 0.30, 0.72, 1.0)\n\n        for position, color in (\n            (0.16, (0.08, 0.22, 0.92, 1.0)),\n            (0.33, (0.06, 0.92, 0.98, 1.0)),\n            (0.49, (0.56, 0.98, 0.76, 1.0)),\n            (0.65, (0.48, 0.20, 0.98, 1.0)),\n            (0.82, (1.00, 0.16, 0.62, 1.0)),\n        ):\n            element = color_ramp.elements.new(position)\n            element.color = color\n\n        layer_weight = nodes.new("ShaderNodeLayerWeight")\n        layer_weight.name = "Pearlescent Viewing Angle"\n        layer_weight.location = (-20, -160)\n        if \'Blend\' in layer_weight.inputs:\n            layer_weight.inputs[\'Blend\'].default_value = 0.36\n\n        tint_mix = nodes.new("ShaderNodeMixRGB")\n        tint_mix.name = "Iridescent Edge Tint"\n        tint_mix.blend_type = \'SCREEN\'\n        tint_mix.location = (235, 135)\n        tint_mix.inputs[2].default_value = (0.10, 0.85, 1.0, 1.0)\n\n        bump = nodes.new("ShaderNodeBump")\n        bump.name = "Very Fine Surface Sparkle"\n        bump.location = (245, -150)\n        if \'Strength\' in bump.inputs:\n            bump.inputs[\'Strength\'].default_value = 0.12\n        if \'Distance\' in bump.inputs:\n            bump.inputs[\'Distance\'].default_value = 0.045\n\n        links.new(geometry.outputs["Position"], noise_large.inputs["Vector"])\n        links.new(geometry.outputs["Position"], noise_fine.inputs["Vector"])\n        links.new(noise_large.outputs["Fac"], mix_noise.inputs["A"])\n        links.new(noise_fine.outputs["Fac"], mix_noise.inputs["B"])\n        links.new(mix_noise.outputs["Result"], ramp.inputs["Fac"])\n        links.new(ramp.outputs["Color"], tint_mix.inputs[1])\n        links.new(layer_weight.outputs["Facing"], tint_mix.inputs[0])\n        links.new(noise_fine.outputs["Fac"], bump.inputs["Height"])\n        links.new(tint_mix.outputs["Color"], principled.inputs["Base Color"])\n        links.new(bump.outputs["Normal"], principled.inputs["Normal"])\n        links.new(principled.outputs["BSDF"], output.inputs["Surface"])\n\n        set_principled_input(principled, ("Metallic",), 0.22)\n        set_principled_input(principled, ("Roughness",), 0.22)\n        set_principled_input(principled, ("Coat Weight", "Coat"), 0.42)\n        set_principled_input(principled, ("Coat Roughness",), 0.14)\n        set_principled_input(principled, ("IOR",), 1.46)\n        set_principled_input(\n            principled, ("Emission Color", "Emission"), (0.025, 0.10, 0.18, 1.0)\n        )\n        set_principled_input(principled, ("Emission Strength",), 0.18)\n\n        return material\n\n    def make_rim_material():\n        material = bpy.data.materials.new("BlueWhite_Soft_Rim_Glow")\n        material.use_nodes = True\n        nodes = material.node_tree.nodes\n        links = material.node_tree.links\n        nodes.clear()\n\n        output = nodes.new("ShaderNodeOutputMaterial")\n        output.location = (500, 0)\n\n        transparent = nodes.new("ShaderNodeBsdfTransparent")\n        transparent.location = (30, -110)\n\n        emission = nodes.new("ShaderNodeEmission")\n        emission.location = (30, 90)\n        if \'Color\' in emission.inputs:\n            emission.inputs[\'Color\'].default_value = (0.18, 0.72, 1.0, 1.0)\n        if \'Strength\' in emission.inputs:\n            emission.inputs[\'Strength\'].default_value = 3.2\n\n        layer = nodes.new("ShaderNodeLayerWeight")\n        layer.location = (-440, 30)\n        if \'Blend\' in layer.inputs:\n            layer.inputs[\'Blend\'].default_value = 0.18\n\n        ramp = nodes.new("ShaderNodeValToRGB")\n        ramp.location = (-220, 30)\n        ramp.color_ramp.elements[0].position = 0.28\n        ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)\n        ramp.color_ramp.elements[1].position = 0.78\n        ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)\n\n        mix = nodes.new("ShaderNodeMixShader")\n        mix.location = (270, 0)\n\n        links.new(layer.outputs["Facing"], ramp.inputs["Fac"])\n        links.new(ramp.outputs["Color"], mix.inputs[0])\n        links.new(transparent.outputs["BSDF"], mix.inputs[1])\n        links.new(emission.outputs["Emission"], mix.inputs[2])\n        links.new(mix.outputs["Shader"], output.inputs["Surface"])\n\n        material.surface_render_method = \'DITHERED\'\n        return material\n\n    def add_uv_part(name, location, scale, material, segments=48, rings=32):\n        bpy.ops.mesh.primitive_uv_sphere_add(\n            segments=segments,\n            ring_count=rings,\n            location=location\n        )\n        obj = bpy.context.object\n        obj.name = name\n        obj.scale = scale\n        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)\n        smooth_object(obj)\n        if hasattr(obj.data, \'materials\'):\n            obj.data.materials.append(material)\n        return obj\n\n    def add_ico_part(name, location, scale, material, subdivisions=2):\n        bpy.ops.mesh.primitive_ico_sphere_add(\n            subdivisions=subdivisions,\n            radius=1.0,\n            location=location\n        )\n        obj = bpy.context.object\n        obj.name = name\n        obj.scale = scale\n        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)\n        smooth_object(obj)\n        if hasattr(obj.data, \'materials\'):\n            obj.data.materials.append(material)\n        return obj\n\n    def add_capsule(name, location, radius, depth, rotation, material):\n        bpy.ops.mesh.primitive_uv_sphere_add(\n            segments=40,\n            ring_count=24,\n            location=location\n        )\n        obj = bpy.context.object\n        obj.name = name\n        obj.scale = (radius, radius, depth * 0.5 + radius)\n        obj.rotation_euler = rotation\n        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)\n        smooth_object(obj)\n        if hasattr(obj.data, \'materials\'):\n            obj.data.materials.append(material)\n        return obj\n\n    def add_glow_shell(source, name, scale_factor=1.035):\n        shell = source.copy()\n        shell.data = source.data.copy()\n        shell.name = name\n        shell.scale = (\n            source.scale.x * scale_factor,\n            source.scale.y * scale_factor,\n            source.scale.z * scale_factor,\n        )\n        shell.data.materials.clear()\n        if hasattr(shell.data, \'materials\'):\n            shell.data.materials.append(rim_material)\n        shell.show_transparent = True\n        bpy.context.collection.objects.link(shell)\n        return shell\n\n    iridescent = make_iridescent_material()\n    rim_material = make_rim_material()\n\n    bill_upper_material = make_simple_material(\n        "Bill_Upper_OrangeRed",\n        (0.48, 0.045, 0.018, 1.0),\n        roughness=0.24,\n        metallic=0.02\n    )\n    bill_lower_material = make_simple_material(\n        "Bill_Lower_Coral",\n        (0.92, 0.22, 0.12, 1.0),\n        roughness=0.30\n    )\n    belly_material = make_simple_material(\n        "Belly_Pale_CyanPearl",\n        (0.48, 0.94, 0.91, 1.0),\n        roughness=0.28,\n        metallic=0.08,\n        emission_color=(0.08, 0.30, 0.32, 1.0),\n        emission_strength=0.18\n    )\n    eye_white_material = make_simple_material(\n        "Eye_White", (0.97, 0.99, 1.0, 1.0), roughness=0.18\n    )\n    pupil_material = make_simple_material(\n        "Eye_Deep_Violet",\n        (0.012, 0.004, 0.025, 1.0),\n        roughness=0.16\n    )\n    foot_material = make_simple_material(\n        "Feet_Coral_Orange",\n        (0.95, 0.20, 0.09, 1.0),\n        roughness=0.32\n    )\n    sparkle_white = make_simple_material(\n        "Sparkle_White",\n        (1.0, 1.0, 1.0, 1.0),\n        roughness=0.05,\n        emission_color=(1.0, 1.0, 1.0, 1.0),\n        emission_strength=7.0\n    )\n    sparkle_cyan = make_simple_material(\n        "Sparkle_Cyan",\n        (0.15, 1.0, 0.95, 1.0),\n        roughness=0.05,\n        emission_color=(0.05, 0.8, 1.0, 1.0),\n        emission_strength=6.0\n    )\n    sparkle_pink = make_simple_material(\n        "Sparkle_Pink",\n        (1.0, 0.18, 0.66, 1.0),\n        roughness=0.05,\n        emission_color=(1.0, 0.06, 0.42, 1.0),\n        emission_strength=6.0\n    )\n\n    # Main silhouette: short torso with a much larger rounded head.\n    body = add_uv_part(\n        "Duck_Body",\n        (0.08, 0.02, -0.05),\n        (0.70, 0.48, 1.02),\n        iridescent\n    )\n    head = add_uv_part(\n        "Duck_Large_Rounded_Head",\n        (-0.02, 0.0, 1.28),\n        (0.78, 0.57, 0.91),\n        iridescent\n    )\n\n    # Slight cheek/neck bridge preserves the long, softly continuous profile.\n    neck = add_uv_part(\n        "Duck_Neck_Bridge",\n        (0.00, 0.02, 0.67),\n        (0.60, 0.45, 0.68),\n        iridescent\n    )\n\n    # Arms extend laterally and remain physically embedded in the torso.\n    left_arm = add_capsule(\n        "Duck_Left_Arm",\n        (-0.68, 0.01, 0.15),\n        0.22,\n        0.58,\n        (0.0, math.radians(-68.0), math.radians(-6.0)),\n        iridescent\n    )\n    right_arm = add_capsule(\n        "Duck_Right_Arm",\n        (0.73, 0.02, 0.17),\n        0.22,\n        0.58,\n        (0.0, math.radians(68.0), math.radians(6.0)),\n        iridescent\n    )\n\n    # Short separated legs.\n    left_leg = add_capsule(\n        "Duck_Left_Leg",\n        (-0.31, 0.01, -0.91),\n        0.25,\n        0.38,\n        (0.0, 0.0, math.radians(-4.0)),\n        iridescent\n    )\n    right_leg = add_capsule(\n        "Duck_Right_Leg",\n        (0.35, 0.01, -0.91),\n        0.25,\n        0.38,\n        (0.0, 0.0, math.radians(4.0)),\n        iridescent\n    )\n\n    # Low-profile chest patch attached to the front (-Y) surface.\n    belly = add_uv_part(\n        "Duck_Pale_Belly_Patch",\n        (0.02, -0.452, -0.02),\n        (0.43, 0.045, 0.68),\n        belly_material,\n        segments=40,\n        rings=24\n    )\n\n    # Long, two-piece flattened bill extending to character-left.\n    upper_bill = add_uv_part(\n        "Duck_Upper_Long_Bill",\n        (-0.88, -0.22, 1.40),\n        (0.72, 0.37, 0.22),\n        bill_upper_material,\n        segments=48,\n        rings=24\n    )\n    upper_bill.rotation_euler[1] = math.radians(-4.0)\n\n    lower_bill = add_uv_part(\n        "Duck_Lower_Long_Bill",\n        (-0.87, -0.22, 1.17),\n        (0.66, 0.35, 0.17),\n        bill_lower_material,\n        segments=48,\n        rings=24\n    )\n    lower_bill.rotation_euler[1] = math.radians(3.0)\n\n    # Eyes are shallow patches on the front of the head, not floating balls.\n    left_eye = add_uv_part(\n        "Duck_Left_Eye_White",\n        (-0.31, -0.535, 1.65),\n        (0.235, 0.050, 0.285),\n        eye_white_material,\n        segments=36,\n        rings=20\n    )\n    right_eye = add_uv_part(\n        "Duck_Right_Eye_White",\n        (0.22, -0.548, 1.65),\n        (0.225, 0.050, 0.275),\n        eye_white_material,\n        segments=36,\n        rings=20\n    )\n    left_pupil = add_uv_part(\n        "Duck_Left_Pupil",\n        (-0.355, -0.581, 1.63),\n        (0.080, 0.026, 0.135),\n        pupil_material,\n        segments=28,\n        rings=16\n    )\n    right_pupil = add_uv_part(\n        "Duck_Right_Pupil",\n        (0.17, -0.594, 1.63),\n        (0.078, 0.026, 0.132),\n        pupil_material,\n        segments=28,\n        rings=16\n    )\n    add_uv_part(\n        "Duck_Left_Eye_Highlight",\n        (-0.382, -0.607, 1.685),\n        (0.024, 0.012, 0.035),\n        sparkle_white,\n        segments=20,\n        rings=12\n    )\n    add_uv_part(\n        "Duck_Right_Eye_Highlight",\n        (0.143, -0.620, 1.685),\n        (0.023, 0.012, 0.034),\n        sparkle_white,\n        segments=20,\n        rings=12\n    )\n\n    # Flattened webbed feet, embedded into the lower legs.\n    left_foot = add_uv_part(\n        "Duck_Left_Foot",\n        (-0.35, -0.16, -1.34),\n        (0.40, 0.48, 0.15),\n        foot_material,\n        segments=40,\n        rings=20\n    )\n    left_foot.rotation_euler[2] = math.radians(-8.0)\n\n    right_foot = add_uv_part(\n        "Duck_Right_Foot",\n        (0.40, -0.16, -1.34),\n        (0.40, 0.48, 0.15),\n        foot_material,\n        segments=40,\n        rings=20\n    )\n    right_foot.rotation_euler[2] = math.radians(8.0)\n\n    # Soft blue-white outline represented with thin Fresnel emission shells.\n    for source, shell_name in (\n        (body, "Glow_Body_Rim"),\n        (head, "Glow_Head_Rim"),\n        (neck, "Glow_Neck_Rim"),\n        (left_arm, "Glow_Left_Arm_Rim"),\n        (right_arm, "Glow_Right_Arm_Rim"),\n        (left_leg, "Glow_Left_Leg_Rim"),\n        (right_leg, "Glow_Right_Leg_Rim"),\n    ):\n        add_glow_shell(source, shell_name)\n\n    # Tiny attached speckles across the visible front surface.\n    attached_speckles = (\n        (-0.24, -0.574, 1.98, 0.025, sparkle_white),\n        (0.19, -0.582, 1.91, 0.018, sparkle_cyan),\n        (0.43, -0.503, 1.44, 0.022, sparkle_pink),\n        (-0.41, -0.493, 1.08, 0.018, sparkle_cyan),\n        (0.36, -0.479, 0.72, 0.024, sparkle_white),\n        (-0.41, -0.430, 0.38, 0.020, sparkle_pink),\n        (0.45, -0.427, 0.10, 0.018, sparkle_cyan),\n        (-0.30, -0.416, -0.47, 0.022, sparkle_white),\n        (0.26, -0.420, -0.64, 0.018, sparkle_pink),\n    )\n    for index, (x, y, z, size, material) in enumerate(attached_speckles):\n        add_ico_part(\n            "Attached_Iridescent_Speckle_%02d" % (index + 1),\n            (x, y, z),\n            (size, size * 0.35, size),\n            material,\n            subdivisions=2\n        )\n\n    # Sparse final sparkle points concentrated at the right outline,\n    # belly, and lower legs as seen in the finished effect.\n    sparkle_points = (\n        (0.83, -0.25, 1.75, 0.055, sparkle_cyan),\n        (0.79, -0.30, 1.03, 0.040, sparkle_white),\n        (0.87, -0.25, 0.35, 0.065, sparkle_pink),\n        (0.58, -0.56, -0.18, 0.048, sparkle_white),\n        (0.76, -0.28, -0.70, 0.040, sparkle_cyan),\n        (-0.08, -0.60, -0.58, 0.042, sparkle_pink),\n        (0.18, -0.58, 0.48, 0.038, sparkle_white),\n    )\n    for index, (x, y, z, size, material) in enumerate(sparkle_points):\n        add_ico_part(\n            "Floating_Sparkle_%02d" % (index + 1),\n            (x, y, z),\n            (size, size, size),\n            material,\n            subdivisions=2\n        )\n\n    # Four-point starbursts provide the distinct bright sparkle silhouette.\n    star_specs = (\n        ((0.70, -0.62, 0.55), 0.10, sparkle_white),\n        ((0.61, -0.57, -0.52), 0.085, sparkle_cyan),\n        ((-0.15, -0.61, 0.16), 0.070, sparkle_pink),\n    )\n    for star_index, (location, size, material) in enumerate(star_specs):\n        x, y, z = location\n        add_uv_part(\n            "Star_%02d_Vertical" % (star_index + 1),\n            (x, y, z),\n            (size * 0.20, size * 0.20, size),\n            material,\n            segments=24,\n            rings=12\n        )\n        add_uv_part(\n            "Star_%02d_Horizontal" % (star_index + 1),\n            (x, y, z),\n            (size, size * 0.20, size * 0.20),\n            material,\n            segments=24,\n            rings=12\n        )\n\n    # Keep the entire visible asset centered near the world origin.\n    created_objects = [\n        obj for obj in bpy.context.scene.objects\n        if obj.type == \'MESH\' and (\n            obj.name.startswith("Duck_")\n            or obj.name.startswith("Glow_")\n            or obj.name.startswith("Attached_")\n            or obj.name.startswith("Floating_")\n            or obj.name.startswith("Star_")\n        )\n    ]\n    if created_objects:\n        minimum_z = min(\n            (obj.matrix_world @ Vector(corner)).z\n            for obj in created_objects\n            for corner in obj.bound_box\n        )\n        maximum_z = max(\n            (obj.matrix_world @ Vector(corner)).z\n            for obj in created_objects\n            for corner in obj.bound_box\n        )\n        center_z = (minimum_z + maximum_z) * 0.5\n        for obj in created_objects:\n            obj.location.z -= center_z\n'
_video2blender_builtins.exec(
    _video2blender_builtins.compile(
        _video2blender_generated_source,
        "<model-generated-blender>",
        "exec",
    ),
    _video2blender_generated_namespace,
    _video2blender_generated_namespace,
)
_video2blender_build_scene = _video2blender_generated_namespace.get(
    "build_scene"
)
_video2blender_configure_final_effect_animation = (
    _video2blender_generated_namespace.get(
        "configure_final_effect_animation"
    )
)
if not callable(_video2blender_build_scene):
    raise RuntimeError("Generated code must define build_scene()")
if (
    _video2blender_configure_final_effect_animation is not None
    and not callable(_video2blender_configure_final_effect_animation)
):
    raise RuntimeError(
        "Generated configure_final_effect_animation must be callable"
    )


# ---- deterministic output wrapper ----

if __name__ == "__main__":
    _clear_scene()
    _linked_source_state = _load_linked_source_if_configured()
    _video2blender_build_scene()
    if _video2blender_configure_final_effect_animation is not None:
        _video2blender_configure_final_effect_animation()
    _write_linked_source_runtime_receipt(_linked_source_state)
    _ensure_camera_lights_and_outputs()
