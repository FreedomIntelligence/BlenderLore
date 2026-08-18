from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "blender/scripts/render_total_asset_contract.py"


def ns(**values):
    return SimpleNamespace(**values)


def load_contract_module():
    module_name = "total_asset_contract_material_audit_fixture"
    spec = importlib.util.spec_from_file_location(module_name, CONTRACT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules,
        {
            "bpy": ns(),
            "mathutils": ns(Vector=object),
        },
    ):
        spec.loader.exec_module(module)
    return module


class FakeMaterial:
    def __init__(self, name: str) -> None:
        self.name = name
        self.use_nodes = False
        self.node_tree = None

    def as_pointer(self) -> int:
        return id(self)


class FakeVector:
    def __init__(self, values) -> None:
        self.values = tuple(values)

    def normalized(self):
        return self

    def __mul__(self, value):
        return FakeVector(component * value for component in self.values)

    def __add__(self, other):
        return FakeVector(a + b for a, b in zip(self.values, other.values))

    def __sub__(self, other):
        return FakeVector(a - b for a, b in zip(self.values, other.values))

    def to_track_quat(self, *_args):
        return ns(to_euler=lambda: (0.0, 0.0, 0.0))


class TotalAssetMaterialAuditTests(unittest.TestCase):
    def test_local_asset_library_weak_reference_is_not_a_missing_dependency(self) -> None:
        contract = load_contract_module()
        weak = ns(
            filepath=(
                "F:/software/model/blender-4.5.3-windows-x64/4.5/"
                "datafiles/assets/geometry_nodes/smooth_by_angle.blend"
            )
        )
        node_group = ns(library=None, library_weak_reference=weak)
        pack_all = mock.Mock()
        contract.bpy = ns(
            utils=ns(
                blend_paths=lambda **_kwargs: [
                    "/f/software/model/blender-4.5.3-windows-x64/4.5/"
                    "datafiles/assets/geometry_nodes/smooth_by_angle.blend",
                    "/source/missing-texture.png",
                ]
            ),
            data=ns(node_groups=[node_group]),
            ops=ns(file=ns(pack_all=pack_all)),
        )
        with mock.patch.object(Path, "exists", return_value=False):
            result = contract.pack_external_resources()

        self.assertEqual(result["status"], "packed")
        self.assertEqual(
            result["benign_missing"],
            [
                "/f/software/model/blender-4.5.3-windows-x64/4.5/"
                "datafiles/assets/geometry_nodes/smooth_by_angle.blend"
            ],
        )
        self.assertEqual(result["critical_missing"], ["/source/missing-texture.png"])
        pack_all.assert_called_once_with()

    def test_negative_index_is_unassigned_instead_of_selecting_last_slot(self) -> None:
        contract = load_contract_module()
        material = FakeMaterial("SourceMaterial")
        mesh = ns(
            materials=[material],
            polygons=[ns(material_index=-1), ns(material_index=0)],
        )
        result = contract.material_audit([ns(name="Mesh", type="MESH", data=mesh)])

        self.assertEqual(result["polygon_count"], 2)
        self.assertEqual(result["unassigned_polygon_count"], 1)
        self.assertEqual(result["unassigned_polygon_ratio"], 0.5)
        self.assertEqual(result["objects_without_material"], [])

    def test_negative_index_with_no_slots_is_counted_without_index_error(self) -> None:
        contract = load_contract_module()
        mesh = ns(materials=[], polygons=[ns(material_index=-1)])
        result = contract.material_audit([ns(name="EmptySlots", type="MESH", data=mesh)])

        self.assertEqual(result["unassigned_polygon_count"], 1)
        self.assertEqual(result["objects_without_material"], ["EmptySlots"])

    def test_single_existing_material_slot_repairs_only_invalid_polygons(self) -> None:
        contract = load_contract_module()
        material = FakeMaterial("OnlySourceMaterial")
        polygons = [
            ns(material_index=0),
            ns(material_index=1),
            ns(material_index=-1),
        ]
        mesh = ns(materials=[None, material], polygons=polygons, library=None)
        obj = ns(name="Unambiguous", type="MESH", data=mesh)

        repair = contract.repair_unambiguous_material_slots([obj])
        audit = contract.material_audit([obj])

        self.assertEqual([polygon.material_index for polygon in polygons], [1, 1, 1])
        self.assertEqual(repair["repaired_object_count"], 1)
        self.assertEqual(repair["repaired_polygon_count"], 2)
        self.assertEqual(repair["repaired_objects"], ["Unambiguous"])
        self.assertEqual(audit["unassigned_polygon_count"], 0)

    def test_material_repair_refuses_ambiguous_or_missing_source_material(self) -> None:
        contract = load_contract_module()
        first = FakeMaterial("First")
        second = FakeMaterial("Second")
        ambiguous_polygon = ns(material_index=-1)
        no_material_polygon = ns(material_index=-1)
        objects = [
            ns(
                name="Ambiguous",
                type="MESH",
                data=ns(
                    materials=[first, second],
                    polygons=[ambiguous_polygon],
                    library=None,
                ),
            ),
            ns(
                name="NoMaterial",
                type="MESH",
                data=ns(
                    materials=[], polygons=[no_material_polygon], library=None
                ),
            ),
        ]

        repair = contract.repair_unambiguous_material_slots(objects)

        self.assertEqual(repair["repaired_polygon_count"], 0)
        self.assertEqual(ambiguous_polygon.material_index, -1)
        self.assertEqual(no_material_polygon.material_index, -1)

    def test_studio_lighting_is_idempotent_and_non_additive(self) -> None:
        contract = load_contract_module()
        contract.Vector = FakeVector
        source = ns(
            name="Source_Key",
            type="LIGHT",
            hide_render=False,
            data=ns(energy=200.0),
        )
        scene_objects = [source]

        def remove_object(obj, do_unlink=False):
            del do_unlink
            scene_objects.remove(obj)

        def new_light(_name, _kind):
            return ns(energy=0.0, size=0.0)

        def new_object(name, data):
            return ns(
                name=name,
                type="LIGHT",
                hide_render=False,
                data=data,
                location=FakeVector((0.0, 0.0, 0.0)),
                rotation_euler=None,
            )

        def link_object(obj):
            scene_objects.append(obj)

        contract.bpy = ns(
            context=ns(
                scene=ns(
                    objects=scene_objects,
                    world=ns(node_tree=None),
                ),
                collection=ns(objects=ns(link=link_object)),
            ),
            data=ns(
                objects=ns(remove=remove_object, new=new_object),
                lights=ns(new=new_light),
                worlds=ns(new=lambda _name: ns(node_tree=None)),
            ),
        )
        center = FakeVector((0.0, 0.0, 0.0))
        first = contract.ensure_lighting(center, 1.0, force_studio=True)
        second = contract.ensure_lighting(center, 1.0, force_studio=True)

        helpers = [
            obj for obj in scene_objects
            if obj.name.startswith(contract.CONTRACT_HELPER_PREFIX)
        ]
        self.assertEqual(len(helpers), 3)
        self.assertTrue(source.hide_render)
        self.assertEqual(first["helper_light_count"], 3)
        self.assertEqual(second["helper_light_count"], 3)

    def test_hdri_world_is_source_illumination_not_additive_helper_light(self) -> None:
        contract = load_contract_module()
        environment = ns(
            type="TEX_ENVIRONMENT",
            image=object(),
            mute=False,
            inputs=[],
        )
        color = ns(
            name="Color",
            default_value=(0.0, 0.0, 0.0, 1.0),
            links=[ns(from_node=environment)],
        )
        strength = ns(name="Strength", default_value=1.0, links=[])
        background = ns(
            type="BACKGROUND",
            mute=False,
            inputs=[color, strength],
        )
        surface = ns(name="Surface", links=[ns(from_node=background)])
        output = ns(
            type="OUTPUT_WORLD",
            is_active_output=True,
            mute=False,
            inputs=[surface],
        )
        world = ns(
            use_nodes=True,
            node_tree=ns(nodes=[environment, background, output]),
        )
        contract.bpy = ns(
            context=ns(scene=ns(objects=[], world=world)),
            data=ns(objects=ns(remove=mock.Mock(), new=mock.Mock())),
        )

        result = contract.ensure_lighting(
            FakeVector((0.0, 0.0, 0.0)),
            1.0,
            force_studio=False,
        )

        self.assertEqual(result["profile"], "source_world")
        self.assertTrue(result["source_world_illumination"])
        self.assertEqual(result["source_world_profile"], "world_nodes")
        self.assertEqual(result["helper_light_count"], 0)
        contract.bpy.data.objects.new.assert_not_called()


if __name__ == "__main__":
    unittest.main()
