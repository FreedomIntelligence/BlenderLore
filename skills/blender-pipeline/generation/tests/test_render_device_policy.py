from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import run_video_strict_replay as replay


def load_functions(source: str, names: set[str], namespace: dict) -> dict:
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    exec(
        compile(
            ast.Module(body=functions, type_ignores=[]), "render-device-policy", "exec"
        ),
        namespace,
    )
    return namespace


class RenderDevicePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cpu = SimpleNamespace(name="CPU", type="CPU", use=True)
        self.metal = SimpleNamespace(name="Apple GPU", type="METAL", use=False)
        self.preferences = SimpleNamespace(
            compute_device_type="",
            devices=[self.cpu, self.metal],
            get_devices=lambda: None,
        )
        self.scene = SimpleNamespace(
            render=SimpleNamespace(engine="CYCLES"),
            cycles=SimpleNamespace(device="CPU"),
        )
        self.bpy = SimpleNamespace(
            context=SimpleNamespace(
                scene=self.scene,
                preferences=SimpleNamespace(
                    addons={"cycles": SimpleNamespace(preferences=self.preferences)}
                ),
            )
        )
        self.wrapper = load_functions(
            replay.WRAPPER_PREFIX,
            {
                "_attest_exact_gpu_process",
                "_video2blender_device_policy",
                "_video2blender_local_cycles",
                "_video2blender_local_device_evidence",
            },
            {"bpy": self.bpy},
        )
        self.renderer = load_functions(
            (SCRIPTS / "render_asset_six_views_turntable.py").read_text(),
            {
                "render_device_policy",
                "expected_gpu_uuid",
                "attest_current_process_gpu",
                "configure_local_cycles",
                "local_render_device_evidence",
                "attest_production_render",
                "set_render_engine",
                "preserve_source_render_settings",
                "write_postprocess_render_receipt",
            },
            {
                "os": os,
                "re": re,
                "bpy": self.bpy,
                "Path": Path,
                "GPU_UUID_RE": re.compile(
                    r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", re.I
                ),
                "_POSTPROCESS_ENGINE": "",
                "_POSTPROCESS_ENGINE_POLICY": "",
                "_POSTPROCESS_CYCLES_BACKEND": "",
                "_POSTPROCESS_GPU_ATTESTED": False,
                "_POSTPROCESS_OUTPUT_ATTESTED": False,
                "POSTPROCESS_RENDER_RECEIPT_SCHEMA": "test",
            },
        )

    def test_default_cluster_policy_still_requires_exact_uuid_and_monitor(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            for namespace, name in (
                (self.wrapper, "_attest_exact_gpu_process"),
                (self.renderer, "attest_current_process_gpu"),
            ):
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    namespace[name]()
            self.assertEqual(self.renderer["render_device_policy"](), "strict")
        with mock.patch.dict(
            os.environ,
            {
                "TOTAL_ASSET_EXPECTED_GPU_UUID": "GPU-00000000-0000-0000-0000-000000000001"
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                self.renderer["attest_current_process_gpu"]()

    def test_local_cpu_is_recorded_without_fabricating_gpu_attestation(self) -> None:
        with mock.patch.dict(
            os.environ, {"BLENDER_PIPELINE_RENDER_DEVICE_POLICY": "local"}, clear=True
        ):
            for namespace, configure, evidence, attest in (
                (
                    self.wrapper,
                    "_video2blender_local_cycles",
                    "_video2blender_local_device_evidence",
                    "_attest_exact_gpu_process",
                ),
                (
                    self.renderer,
                    "configure_local_cycles",
                    "local_render_device_evidence",
                    "attest_current_process_gpu",
                ),
            ):
                namespace[configure](self.scene)
                receipt = namespace[evidence](self.scene)
                self.assertEqual(receipt["render_device"], "CPU")
                self.assertEqual(receipt["cycles_backend"], "CPU")
                self.assertIs(receipt["gpu_process_attested"], False)
                self.assertEqual(receipt["observed_gpu_uuid"], "")
                self.assertEqual(namespace[attest](), "")

    def test_explicit_metal_uses_available_devices_and_disables_cpu(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "BLENDER_PIPELINE_RENDER_DEVICE_POLICY": "local",
                "VIDEO2BLENDER_CYCLES_BACKEND": "METAL",
            },
            clear=True,
        ):
            for namespace, configure in (
                (self.wrapper, "_video2blender_local_cycles"),
                (self.renderer, "configure_local_cycles"),
            ):
                namespace[configure](self.scene)
                self.assertEqual(self.scene.cycles.device, "GPU")
                self.assertFalse(self.cpu.use)
                self.assertTrue(self.metal.use)
            self.preferences.devices = [self.cpu]
            with self.assertRaises(RuntimeError):
                self.renderer["configure_local_cycles"](self.scene)

    def test_local_postprocess_keeps_visual_gate_and_records_no_uuid(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "BLENDER_PIPELINE_RENDER_DEVICE_POLICY": "local",
                "VIDEO2BLENDER_RENDER_ENGINE": "CYCLES",
            },
            clear=True,
        ):
            self.assertEqual(self.renderer["set_render_engine"](self.scene), "CYCLES")
            self.renderer["render_result_visual_probe"] = lambda **kw: {"passed": True}
            self.renderer["attest_production_render"](Path("render.png"))
            self.assertTrue(self.renderer["_POSTPROCESS_OUTPUT_ATTESTED"])
            self.assertFalse(self.renderer["_POSTPROCESS_GPU_ATTESTED"])
            captured = {}
            self.renderer["atomic_write_json"] = lambda path, receipt: captured.update(
                receipt
            )
            self.renderer["write_postprocess_render_receipt"](Path("unused-output"))
            self.assertEqual(captured["cycles_backend"], "CPU")
            self.assertFalse(captured["gpu_process_attested"])
            self.renderer["render_result_visual_probe"] = lambda **kw: {
                "passed": False,
                "mean_luma": 0,
                "max_luma": 0,
                "luma_range": 0,
            }
            with self.assertRaises(RuntimeError):
                self.renderer["attest_production_render"](Path("render.png"))

    def test_local_startup_disables_autoexec_and_unknown_policy_is_rejected(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ, {"BLENDER_PIPELINE_RENDER_DEVICE_POLICY": "local"}, clear=True
        ):
            self.assertIn(
                "--disable-autoexec",
                replay.blender_replay_command(Path("reproduce.py")),
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotIn(
                "--disable-autoexec",
                replay.blender_replay_command(Path("reproduce.py")),
            )
        with mock.patch.dict(
            os.environ, {"BLENDER_PIPELINE_RENDER_DEVICE_POLICY": "locla"}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                self.renderer["render_device_policy"]()


if __name__ == "__main__":
    unittest.main()
