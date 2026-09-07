
import math
from pathlib import Path
import bpy
from mathutils import Vector

OUTPUT_DIR = Path(__file__).resolve().parent

def _clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

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
        scene.render.engine = "CYCLES"
        scene.cycles.samples = _render_samples
        scene.cycles.use_denoising = True
    else:
        try:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
        except TypeError:
            scene.render.engine = "BLENDER_EEVEE"
        if hasattr(scene, "eevee"):
            scene.eevee.taa_render_samples = max(_render_samples, 64)
    prefs = bpy.context.preferences.addons.get("cycles") if scene.render.engine == "CYCLES" else None
    if prefs:
        cprefs = prefs.preferences
        for kind in ("CUDA", "OPTIX"):
            try:
                cprefs.compute_device_type = kind
                cprefs.get_devices()
                for device in cprefs.devices:
                    device.use = device.type != "CPU"
                scene.cycles.device = "GPU"
                break
            except Exception:
                pass
    scene.render.resolution_x = int(__import__("os").environ.get("PAPER12_RENDER_W", "1280"))
    scene.render.resolution_y = int(__import__("os").environ.get("PAPER12_RENDER_H", "720"))
    scene.view_settings.view_transform = "Filmic"
    scene.view_settings.look = "Medium High Contrast"
    scene.view_settings.exposure = 0.05
    world_color = __import__("os").environ.get("PAPER12_WORLD_COLOR", "").strip()
    if world_color and scene.world:
        try:
            vals = tuple(float(x.strip()) for x in world_color.split(",")[:3])
            if len(vals) == 3:
                scene.world.color = vals
        except Exception:
            pass
    scene.render.film_transparent = __import__("os").environ.get("PAPER12_FILM_TRANSPARENT", "1") != "0"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.filepath = str(OUTPUT_DIR / "final.png")
    bpy.ops.render.render(write_still=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT_DIR / "asset.blend"))


# ---- model generated scene builder ----
import bpy


SOURCE_IMAGE = str(Path(__file__).resolve().parent / "source.png")


def set_input(node, name, value):
    socket = node.inputs.get(name)
    if socket is not None:
        socket.default_value = value


def make_material(name, color, metallic=0.0, roughness=0.35):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    set_input(bsdf, "Base Color", color)
    set_input(bsdf, "Metallic", metallic)
    set_input(bsdf, "Roughness", roughness)
    return mat


def add_beveled_cube(name, location, scale, material, bevel=0.035):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    modifier = obj.modifiers.new("Soft bevel", "BEVEL")
    modifier.width = bevel
    modifier.segments = 3
    obj.data.materials.append(material)
    return obj


def build_panel_mesh():
    width = 6.0
    height = 3.43
    verts = [
        (-width / 2, 0.0, -height / 2),
        (width / 2, 0.0, -height / 2),
        (width / 2, 0.0, height / 2),
        (-width / 2, 0.0, height / 2),
    ]
    mesh = bpy.data.meshes.new("Stained_Glass_Panel_Mesh")
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap")
    uv_values = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    for loop in mesh.loops:
        uv_layer.data[loop.index].uv = uv_values[loop.vertex_index]
    obj = bpy.data.objects.new("Stained_Glass_Panel", mesh)
    bpy.context.collection.objects.link(obj)
    solidify = obj.modifiers.new("Tutorial thickness 0.003", "SOLIDIFY")
    solidify.thickness = 0.003
    solidify.offset = -1.0
    bevel = obj.modifiers.new("Edge softening", "BEVEL")
    bevel.width = 0.006
    bevel.segments = 2
    return obj


def build_stained_glass_material():
    mat = bpy.data.materials.new("Stained_Glass_Voronoi_Lead")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (980, 60)

    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.location = (-1050, 80)

    image = bpy.data.images.load(SOURCE_IMAGE, check_existing=True)
    image.pack()
    image_node = nodes.new("ShaderNodeTexImage")
    image_node.name = "Tutorial image texture"
    image_node.label = "Red-orange dragon image"
    image_node.image = image
    image_node.extension = "EXTEND"
    image_node.interpolation = "Linear"
    image_node.location = (-820, 280)
    links.new(texcoord.outputs["UV"], image_node.inputs["Vector"])

    separate = nodes.new("ShaderNodeSeparateXYZ")
    separate.name = "Vertical panel axis split"
    separate.location = (-1000, -180)
    links.new(texcoord.outputs["Generated"], separate.inputs["Vector"])

    combine = nodes.new("ShaderNodeCombineXYZ")
    combine.name = "Map panel X and Z into 2D"
    combine.location = (-900, -180)
    links.new(separate.outputs["X"], combine.inputs["X"])
    links.new(separate.outputs["Z"], combine.inputs["Y"])

    mapping = nodes.new("ShaderNodeMapping")
    mapping.name = "Tutorial Mapping X 1.75"
    mapping.location = (-820, -180)
    mapping.inputs["Scale"].default_value = (1.75, 1.0, 1.0)
    links.new(combine.outputs["Vector"], mapping.inputs["Vector"])

    voronoi = nodes.new("ShaderNodeTexVoronoi")
    voronoi.name = "2D Voronoi Distance to Edge"
    voronoi.label = "Final scale 50"
    voronoi.voronoi_dimensions = "2D"
    voronoi.feature = "DISTANCE_TO_EDGE"
    voronoi.distance = "EUCLIDEAN"
    voronoi.location = (-540, -170)
    set_input(voronoi, "Scale", 50.0)
    set_input(voronoi, "Randomness", 1.0)
    links.new(mapping.outputs["Vector"], voronoi.inputs["Vector"])

    edge_ramp = nodes.new("ShaderNodeValToRGB")
    edge_ramp.name = "Constant lead edge mask"
    edge_ramp.location = (-280, -150)
    edge_ramp.color_ramp.interpolation = "CONSTANT"
    edge_ramp.color_ramp.elements[0].position = 0.018
    edge_ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
    edge_ramp.color_ramp.elements[1].position = 0.034
    edge_ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    links.new(voronoi.outputs["Distance"], edge_ramp.inputs["Fac"])

    glass = nodes.new("ShaderNodeBsdfPrincipled")
    glass.name = "Colored glass IOR 1.50"
    glass.location = (300, 220)
    set_input(glass, "Roughness", 0.14)
    set_input(glass, "IOR", 1.50)
    set_input(glass, "Transmission Weight", 0.08)
    set_input(glass, "Coat Weight", 0.22)
    set_input(glass, "Coat Roughness", 0.08)
    links.new(image_node.outputs["Color"], glass.inputs["Base Color"])
    if glass.inputs.get("Emission Color") is not None:
        links.new(image_node.outputs["Color"], glass.inputs["Emission Color"])
        set_input(glass, "Emission Strength", 0.16)

    lead = nodes.new("ShaderNodeBsdfPrincipled")
    lead.name = "Dark raised lead"
    lead.location = (300, -170)
    set_input(lead, "Base Color", (0.018, 0.012, 0.016, 1.0))
    set_input(lead, "Metallic", 0.72)
    set_input(lead, "Roughness", 0.28)

    invert = nodes.new("ShaderNodeMath")
    invert.operation = "SUBTRACT"
    invert.location = (-20, -330)
    invert.inputs[0].default_value = 1.0
    links.new(edge_ramp.outputs["Color"], invert.inputs[1])

    bump = nodes.new("ShaderNodeBump")
    bump.name = "Raised lead bump"
    bump.location = (80, -360)
    bump.inputs["Strength"].default_value = 0.32
    bump.inputs["Distance"].default_value = 0.035
    links.new(invert.outputs["Value"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], glass.inputs["Normal"])
    links.new(bump.outputs["Normal"], lead.inputs["Normal"])

    mix = nodes.new("ShaderNodeMixShader")
    mix.name = "Glass and lead mix"
    mix.location = (700, 60)
    links.new(edge_ramp.outputs["Color"], mix.inputs["Fac"])
    links.new(lead.outputs["BSDF"], mix.inputs[1])
    links.new(glass.outputs["BSDF"], mix.inputs[2])
    links.new(mix.outputs["Shader"], output.inputs["Surface"])
    return mat


def build_scene():
    panel = build_panel_mesh()
    panel.data.materials.append(build_stained_glass_material())

    frame_material = make_material(
        "Warm bronze frame",
        (0.19, 0.055, 0.022, 1.0),
        metallic=0.82,
        roughness=0.24,
    )
    half_w = 3.0
    half_h = 1.715
    bar = 0.075
    depth = 0.065
    y = 0.025
    add_beveled_cube("Frame_Top", (0.0, y, half_h + bar), (half_w + bar, depth, bar), frame_material)
    add_beveled_cube("Frame_Bottom", (0.0, y, -half_h - bar), (half_w + bar, depth, bar), frame_material)
    add_beveled_cube("Frame_Left", (-half_w - bar, y, 0.0), (bar, depth, half_h + bar), frame_material)
    add_beveled_cube("Frame_Right", (half_w + bar, y, 0.0), (bar, depth, half_h + bar), frame_material)


# ---- deterministic output wrapper ----

if __name__ == "__main__":
    _clear_scene()
    if "build_scene" not in globals():
        raise RuntimeError("Generated code must define build_scene()")
    build_scene()
    if "configure_final_effect_animation" in globals() and callable(configure_final_effect_animation):
        configure_final_effect_animation()
    _ensure_camera_lights_and_outputs()
