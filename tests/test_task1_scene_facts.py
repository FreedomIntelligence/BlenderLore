from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import render_knowledge as rk


def ns(**values):
    return SimpleNamespace(**values)


def action(*paths: str):
    return ns(fcurves=[ns(data_path=path) for path in paths])


def driver(expression: str):
    return ns(driver=ns(expression=expression, variables=[]))


def animation_data(*, current_action=None, nla_actions=(), drivers=()):
    tracks = [
        ns(strips=[ns(action=nla_action) for nla_action in nla_actions])
    ] if nla_actions else []
    return ns(action=current_action, nla_tracks=tracks, drivers=list(drivers))


def data_block(*, animation=None, shape_keys=None):
    return ns(animation_data=animation, shape_keys=shape_keys)


def scene_object(
    name: str,
    object_type: str = "MESH",
    *,
    animation=None,
    data=None,
    modifiers=(),
    particle_systems=(),
    rigid_body=None,
):
    return ns(
        name=name,
        type=object_type,
        animation_data=animation,
        data=data or data_block(),
        modifiers=list(modifiers),
        particle_systems=list(particle_systems),
        rigid_body=rigid_body,
        hide_render=False,
        visible_get=lambda: True,
    )


def load_preview_module(objects, materials=()):
    camera = next((obj for obj in objects if obj.type == "CAMERA"), None)
    scene = ns(
        name="FixtureScene",
        objects=list(objects),
        render=ns(engine="BLENDER_EEVEE_NEXT"),
        camera=camera,
        frame_start=1,
        frame_end=120,
        view_settings=ns(view_transform="AgX", look="Medium High Contrast", exposure=0.0),
        use_nodes=False,
    )
    fake_bpy = ns(
        context=ns(scene=scene),
        data=ns(objects=list(objects), materials=list(materials), images=[], filepath=""),
    )
    module_name = "task1_preview_scene_facts_fixture"
    spec = importlib.util.spec_from_file_location(
        module_name,
        SCRIPTS / "render_existing_model_preview.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules,
        {
            "bpy": fake_bpy,
            "mathutils": ns(Vector=object),
        },
    ):
        spec.loader.exec_module(module)
    return module


class Task1SceneFactsTests(unittest.TestCase):
    def test_emits_reliable_fields_for_supported_motion_mechanisms(self) -> None:
        transform = scene_object(
            "Transform",
            animation=animation_data(current_action=action("location")),
        )
        nla = scene_object(
            "NLA",
            animation=animation_data(nla_actions=[action("rotation_euler")]),
        )
        rig = scene_object(
            "Rig",
            "ARMATURE",
            animation=animation_data(
                current_action=action('pose.bones["Bone"].rotation_quaternion')
            ),
        )
        shape_keys = data_block(
            animation=animation_data(current_action=action('key_blocks["Smile"].value'))
        )
        shaped = scene_object("Shape", data=data_block(shape_keys=shape_keys))
        timed = scene_object(
            "Timed",
            animation=animation_data(drivers=[driver("frame * 0.1")]),
        )
        generic_driver = scene_object(
            "GenericDriver",
            animation=animation_data(drivers=[driver("var * 2")]),
        )
        material_tree = ns(
            animation_data=animation_data(current_action=action('nodes["Value"].outputs[0].default_value')),
            nodes=[],
        )
        material = ns(name="AnimatedMaterial", node_tree=material_tree)
        node_group = ns(
            animation_data=None,
            nodes=[
                ns(
                    bl_idname="GeometryNodeInputSceneTime",
                    type="",
                    inputs=[],
                    outputs=[ns(is_linked=True)],
                )
            ],
        )
        geometry_nodes = scene_object(
            "GeometryNodes",
            modifiers=[ns(type="NODES", node_group=node_group)],
        )
        hair = scene_object(
            "Hair",
            particle_systems=[
                ns(name="HairSystem", settings=ns(type="HAIR", use_hair_dynamics=True))
            ],
        )
        emitter = scene_object(
            "Emitter",
            particle_systems=[
                ns(name="EmitterSystem", settings=ns(type="EMITTER", use_hair_dynamics=False))
            ],
        )
        cloth = scene_object("Cloth", modifiers=[ns(type="CLOTH")])
        soft = scene_object("Soft", modifiers=[ns(type="SOFT_BODY")])
        rigid = scene_object("Rigid", rigid_body=ns(type="ACTIVE"))
        passive = scene_object("PassiveCollider", rigid_body=ns(type="PASSIVE"))
        liquid = scene_object(
            "Liquid",
            modifiers=[
                ns(type="FLUID", domain_settings=ns(domain_type="LIQUID"), flow_settings=None)
            ],
        )
        smoke = scene_object(
            "Smoke",
            modifiers=[
                ns(type="FLUID", domain_settings=ns(domain_type="GAS"), flow_settings=None)
            ],
        )
        camera = scene_object(
            "Camera",
            "CAMERA",
            animation=animation_data(current_action=action("location")),
        )
        objects = [
            transform,
            nla,
            rig,
            shaped,
            timed,
            generic_driver,
            geometry_nodes,
            hair,
            emitter,
            cloth,
            soft,
            rigid,
            passive,
            liquid,
            smoke,
            camera,
        ]
        facts = load_preview_module(objects, [material]).collect_scene_facts()
        self.assertEqual(set(facts["action_nla"]), {"Transform", "NLA", "Rig", "Shape"})
        self.assertEqual(set(facts["object_transform_animation"]), {"Transform", "NLA"})
        self.assertEqual(facts["bone_animated_armatures"], ["Rig"])
        self.assertEqual(facts["animated_shape_keys"], ["Shape"])
        self.assertIn("Timed", facts["time_dependent_drivers"])
        self.assertNotIn("GenericDriver", facts["time_dependent_drivers"])
        self.assertEqual(facts["animated_materials"], ["AnimatedMaterial"])
        self.assertEqual(facts["geometry_nodes_time"], ["GeometryNodes"])
        self.assertEqual(len(facts["hair_particle_systems"]), 1)
        self.assertEqual(len(facts["animated_particle_systems"]), 1)
        self.assertEqual(facts["cloth_simulations"], ["Cloth"])
        self.assertEqual(facts["soft_body_simulations"], ["Soft"])
        self.assertEqual(facts["rigid_body_simulations"], ["Rigid"])
        self.assertEqual(facts["liquid_simulations"], ["Liquid"])
        self.assertEqual(facts["smoke_simulations"], ["Smoke"])
        self.assertEqual(facts["animated_cameras"], ["Camera"])
        self.assertTrue(facts["has_timeline_animation"])

        context, _ = rk.bili_linked_asset_knowledge_decision(
            {"标题": "mechanism fixture"},
            {"source_scene": facts},
        )
        expected = {
            "object_transform",
            "action_nla",
            "rig_pose",
            "shape_key",
            "time_driver",
            "material_animation",
            "geometry_nodes_time",
            "particle_hair",
            "particle_emitter",
            "cloth_softbody",
            "rigid_body",
            "fluid_liquid",
            "smoke_fire",
        }
        self.assertTrue(expected.issubset(set(context.motion_mechanisms)))
        self.assertNotIn("camera_only", context.motion_mechanisms)
        self.assertEqual(context.route, "dynamic")

    def test_camera_only_and_generic_driver_are_not_subject_motion(self) -> None:
        camera = scene_object(
            "Camera",
            "CAMERA",
            animation=animation_data(drivers=[driver("frame * 0.25")]),
        )
        generic = scene_object(
            "Generic",
            animation=animation_data(drivers=[driver("var * 2")]),
        )
        passive = scene_object("PassiveCollider", rigid_body=ns(type="PASSIVE"))
        unused_time_node = scene_object(
            "UnusedSceneTime",
            modifiers=[
                ns(
                    type="NODES",
                    node_group=ns(
                        animation_data=None,
                        nodes=[
                            ns(
                                bl_idname="GeometryNodeInputSceneTime",
                                type="",
                                inputs=[],
                                outputs=[ns(is_linked=False)],
                            )
                        ],
                    ),
                )
            ],
        )
        facts = load_preview_module(
            [camera, generic, passive, unused_time_node]
        ).collect_scene_facts()
        self.assertEqual(facts["animated_cameras"], ["Camera"])
        self.assertEqual(facts["time_dependent_drivers"], [])
        self.assertEqual(facts["animated_object_count"], 0)
        self.assertFalse(facts["has_timeline_animation"])
        self.assertEqual(facts["driver_count"], 2)
        self.assertEqual(facts["rigid_body_simulations"], [])
        self.assertEqual(facts["geometry_nodes_time"], [])

        context, _ = rk.bili_linked_asset_knowledge_decision(
            {"标题": "camera-only fixture"},
            {"source_scene": facts},
        )
        self.assertEqual(context.route, "static")
        self.assertEqual(context.motion_mechanisms, ["camera_only"])

    def test_passive_rigid_body_requires_explicit_time_animation(self) -> None:
        static_passive = scene_object(
            "StaticPassive",
            rigid_body=ns(type="PASSIVE"),
        )
        animated_passive = scene_object(
            "AnimatedPassive",
            animation=animation_data(current_action=action("rigid_body.kinematic")),
            rigid_body=ns(type="PASSIVE"),
        )
        facts = load_preview_module([static_passive, animated_passive]).collect_scene_facts()
        self.assertEqual(facts["rigid_body_simulations"], ["AnimatedPassive"])


if __name__ == "__main__":
    unittest.main()
