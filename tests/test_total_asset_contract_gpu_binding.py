from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "blender/scripts/render_total_asset_contract.py"
EXPECTED_UUID = "gpu-82edd6ac-dc7b-a6c4-dfb7-71552a93ce0d"


def ns(**values):
    return SimpleNamespace(**values)


def load_contract_module():
    module_name = "total_asset_contract_gpu_binding_fixture"
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


class TotalAssetContractGpuBindingTests(unittest.TestCase):
    def test_dynamic_camera_distance_respects_aspect_and_rescue_margin(self) -> None:
        contract = load_contract_module()
        square = contract.animation_camera_distance(
            1.0, 720, 720, 55.0, 36.0, 1.22
        )
        landscape = contract.animation_camera_distance(
            1.0, 1280, 720, 55.0, 36.0, 1.22
        )
        rescue = contract.animation_camera_distance(
            1.0, 1280, 720, 55.0, 36.0, 1.35
        )

        self.assertGreater(landscape, square)
        self.assertGreater(rescue, landscape)

    def test_auto_animation_camera_preserves_usable_source_camera(self) -> None:
        contract = load_contract_module()
        source = ns(type="CAMERA", hide_render=False)
        contract.bpy = ns(context=ns(scene=ns(camera=source)))
        with mock.patch.object(contract, "add_camera") as add_camera:
            selected = contract.animation_camera(
                object(), object(), 1.0, "auto", 1280, 720, 1.35
            )

        self.assertIs(selected, source)
        add_camera.assert_not_called()

    def test_dynamic_render_forwards_margin_to_fallback_camera(self) -> None:
        contract = load_contract_module()
        scene = ns(frame_start=1, frame_end=2, frame_set=mock.Mock())
        contract.bpy = ns(context=ns(scene=scene))
        contract.STILL_IMAGE_OUTPUT_AVAILABLE = True
        center = object()
        size = object()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(contract, "animation_camera") as camera,
            mock.patch.object(contract, "render_still"),
            mock.patch.object(contract, "encode_video"),
        ):
            contract.render_dynamic(
                Path(temporary),
                center,
                size,
                1.0,
                1280,
                720,
                1,
                2.0,
                4,
                "fallback",
                0.0,
                1.35,
            )

        camera.assert_called_once_with(
            center, size, 1.0, "fallback", 1280, 720, 1.35
        )

    def test_movie_dynamic_render_forwards_margin(self) -> None:
        contract = load_contract_module()
        contract.STILL_IMAGE_OUTPUT_AVAILABLE = False
        out_dir = Path("output")
        center = object()
        size = object()
        with mock.patch.object(
            contract, "render_dynamic_via_movie"
        ) as render_movie:
            contract.render_dynamic(
                out_dir,
                center,
                size,
                1.0,
                1280,
                720,
                24,
                5.0,
                32,
                "fallback",
                0.0,
                1.35,
            )

        render_movie.assert_called_once_with(
            out_dir,
            center,
            size,
            1.0,
            1280,
            720,
            24,
            5.0,
            32,
            "fallback",
            0.0,
            1.35,
        )

    def test_current_blender_pid_must_map_to_exact_expected_uuid(self) -> None:
        contract = load_contract_module()
        probe = subprocess.CompletedProcess(
            [],
            0,
            f"{EXPECTED_UUID}, 4242\nGPU-11111111-1111-1111-1111-111111111111, 9\n",
            "",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"TOTAL_ASSET_EXPECTED_GPU_UUID": EXPECTED_UUID},
                clear=False,
            ),
            mock.patch.object(contract.os, "getpid", return_value=4242),
            mock.patch.object(contract.subprocess, "run", return_value=probe),
        ):
            result = contract.attest_current_process_gpu_uuid(attempts=1)
        self.assertEqual(result["status"], "attested")
        self.assertEqual(result["gpu_uuids"], [EXPECTED_UUID])

    def test_wrong_physical_uuid_fails_closed(self) -> None:
        contract = load_contract_module()
        probe = subprocess.CompletedProcess(
            [],
            0,
            "GPU-11111111-1111-1111-1111-111111111111, 4242\n",
            "",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"TOTAL_ASSET_EXPECTED_GPU_UUID": EXPECTED_UUID},
                clear=False,
            ),
            mock.patch.object(contract.os, "getpid", return_value=4242),
            mock.patch.object(contract.subprocess, "run", return_value=probe),
            self.assertRaisesRegex(
                RuntimeError, "TOTAL_ASSET_GPU_UUID_ATTESTATION_FAILED"
            ),
        ):
            contract.attest_current_process_gpu_uuid(attempts=1)

    def test_graphics_only_process_uses_pmon_index_to_uuid_fallback(self) -> None:
        contract = load_contract_module()
        probes = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess(
                [], 0, f"0, {EXPECTED_UUID}\n", ""
            ),
            subprocess.CompletedProcess(
                [],
                0,
                "# gpu pid type sm mem enc dec command\n0 4242 G - - - - blender\n",
                "",
            ),
        ]
        with (
            mock.patch.dict(
                os.environ,
                {"TOTAL_ASSET_EXPECTED_GPU_UUID": EXPECTED_UUID},
                clear=False,
            ),
            mock.patch.object(contract.os, "getpid", return_value=4242),
            mock.patch.object(contract.subprocess, "run", side_effect=probes),
        ):
            result = contract.attest_current_process_gpu_uuid(attempts=1)
        self.assertEqual(result["status"], "attested")
        self.assertEqual(result["gpu_uuids"], [EXPECTED_UUID])

    def test_cycles_enables_exactly_one_cuda_gpu_and_disables_cpu(self) -> None:
        contract = load_contract_module()
        cuda = ns(name="RTX 5090", type="CUDA", use=False)
        cpu = ns(name="CPU", type="CPU", use=True)
        preferences = ns(
            compute_device_type="",
            devices=[cuda, cpu],
            get_devices=lambda: None,
        )
        scene = ns(
            render=ns(
                resolution_x=0,
                resolution_y=0,
                resolution_percentage=0,
                film_transparent=False,
                image_settings=ns(file_format="", color_mode=""),
                filepath="",
                engine="CYCLES",
            ),
            cycles=ns(samples=0, use_denoising=False, device="CPU"),
            view_settings=ns(look="", exposure=0.0),
        )
        contract.bpy = ns(
            context=ns(
                scene=scene,
                preferences=ns(addons={"cycles": ns(preferences=preferences)}),
            )
        )
        contract.CYCLES_RUNTIME = {"backend": "", "devices": []}
        contract.BASE_EXPOSURE = None
        with mock.patch.dict(
            os.environ, {"TOTAL_ASSET_CYCLES_BACKEND": "CUDA"}, clear=False
        ):
            contract.configure_render(64, 64, 4, Path("frame.png"))
        self.assertTrue(cuda.use)
        self.assertFalse(cpu.use)
        self.assertEqual(contract.CYCLES_RUNTIME["backend"], "CUDA")
        self.assertEqual(
            [item for item in contract.CYCLES_RUNTIME["devices"] if item["use"]],
            [{"name": "RTX 5090", "type": "CUDA", "use": True}],
        )

    def test_cycles_rejects_multiple_visible_cuda_devices(self) -> None:
        contract = load_contract_module()
        preferences = ns(
            compute_device_type="",
            devices=[
                ns(name="GPU0", type="CUDA", use=False),
                ns(name="GPU1", type="CUDA", use=False),
            ],
            get_devices=lambda: None,
        )
        scene = ns(
            render=ns(
                resolution_x=0,
                resolution_y=0,
                resolution_percentage=0,
                film_transparent=False,
                image_settings=ns(file_format="", color_mode=""),
                filepath="",
                engine="CYCLES",
            ),
            cycles=ns(samples=0, use_denoising=False, device="CPU"),
            view_settings=ns(look="", exposure=0.0),
        )
        contract.bpy = ns(
            context=ns(
                scene=scene,
                preferences=ns(addons={"cycles": ns(preferences=preferences)}),
            )
        )
        with self.assertRaisesRegex(
            RuntimeError, "TOTAL_ASSET_CYCLES_DEVICE_ATTESTATION_FAILED"
        ):
            contract.configure_render(64, 64, 4, Path("frame.png"))


if __name__ == "__main__":
    unittest.main()
