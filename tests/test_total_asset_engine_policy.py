from __future__ import annotations

import gzip
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import total_asset_engine_policy as policy  # noqa: E402
import run_total_asset_render_worker as worker  # noqa: E402


class EnginePolicyTests(unittest.TestCase):
    def decide(self, **changes: object) -> policy.EnginePolicyDecision:
        values = {
            "assignment_mode": policy.DYNAMIC_ASSIGNMENT_MODE,
            "source_kind": policy.TOTAL_ASSET_SOURCE_KIND,
            "render_route": "static",
            "original_hint": "source",
            "remote_port": policy.PRIMARY_REMOTE_PORT,
            "gpu_index": 0,
            "vulkan_profile_available": None,
        }
        values.update(changes)
        return policy.decide_effective_engine(**values)

    def test_primary_static_source_and_eevee_fall_back_on_every_gpu(self) -> None:
        for gpu in range(4):
            for hint in ("source", "EEVEE", "BLENDER_EEVEE_NEXT"):
                with self.subTest(gpu=gpu, hint=hint):
                    decision = self.decide(gpu_index=gpu, original_hint=hint)
                    self.assertEqual(decision.effective_hint, "CYCLES")
                    self.assertEqual(decision.rule_id, policy.PRIMARY_CYCLES_RULE)

    def test_nonzero_5090_falls_back_only_when_profile_is_proven_missing(self) -> None:
        for port in (policy.SECONDARY_REMOTE_PORT, policy.TERTIARY_REMOTE_PORT):
            missing = self.decide(
                remote_port=port,
                gpu_index=1,
                vulkan_profile_available=False,
            )
            self.assertEqual(missing.effective_hint, "CYCLES")
            self.assertEqual(
                missing.rule_id, policy.MISSING_PROFILE_CYCLES_RULE
            )
            for available in (None, True):
                preserved = self.decide(
                    remote_port=port,
                    gpu_index=1,
                    vulkan_profile_available=available,
                )
                self.assertEqual(preserved.effective_hint, "SOURCE")
            gpu0 = self.decide(
                remote_port=port,
                gpu_index=0,
                vulkan_profile_available=False,
            )
            self.assertEqual(gpu0.effective_hint, "SOURCE")

    def test_policy_never_changes_dynamic_candidate_cycles_or_other_scope(self) -> None:
        dynamic = self.decide(render_route="dynamic_candidate")
        self.assertEqual(dynamic.effective_hint, "SOURCE")
        cycles = self.decide(original_hint="CYCLES")
        self.assertEqual(cycles.effective_hint, "CYCLES")
        fixed = self.decide(assignment_mode="fixed_partition_v1")
        self.assertEqual(fixed.effective_hint, "SOURCE")
        other_source = self.decide(source_kind="video_replay")
        self.assertEqual(other_source.effective_hint, "SOURCE")

    def test_worker_uses_same_effective_hint_for_capability_attestation(self) -> None:
        model_row = {
            "model_file": "/tmp/model.blend",
            "render_route": "static",
            "render_engine_hint": "source",
        }

        class Remote:
            port = str(policy.PRIMARY_REMOTE_PORT)
            gpu = "2"
            native_system_graphics_runtime = True

            def __init__(self) -> None:
                self.cycles_runtime_calls = 0

            def ensure_nvidia_graphics_runtime(self) -> None:
                self.cycles_runtime_calls += 1

        remote = Remote()
        with mock.patch.object(
            worker, "select_canonical_model", return_value=(Path("/tmp/model.blend"), "")
        ), mock.patch.object(Path, "exists", return_value=True), mock.patch.object(
            worker, "resolve_attempt_runtime", return_value=(
                "4.5", worker.REMOTE_BLENDERS["4.5"], "4.5", "", ()
            )
        ):
            decision = worker.resolve_attempt_engine_policy(
                remote,
                model_row,
                assignment_mode=policy.DYNAMIC_ASSIGNMENT_MODE,
                source_kind=policy.TOTAL_ASSET_SOURCE_KIND,
                selected_family="4.5",
                selected_blender=worker.REMOTE_BLENDERS["4.5"],
                allow_forward_compatible_blender=False,
            )
        self.assertEqual(decision.effective_hint, "CYCLES")
        self.assertEqual(remote.cycles_runtime_calls, 1)


class BoundedBlendHeaderTests(unittest.TestCase):
    def test_raw_gzip_and_bad_headers_fail_closed_with_bounded_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.blend"
            raw.write_bytes(b"BLENDER-v450" + b"x" * 4096)
            zipped = root / "gzip.blend"
            zipped.write_bytes(gzip.compress(b"BLENDER19-02v0510" + b"x" * 4096))
            bad = root / "bad.blend"
            bad.write_bytes(b"not a blend")
            self.assertEqual(policy.source_blender_version(raw), "4.50")
            self.assertEqual(policy.source_runtime_family(raw), "4.5")
            self.assertEqual(policy.source_blender_version(zipped), "5.10")
            self.assertEqual(policy.source_blender_version(bad), "unknown")

    def test_zstd_timeout_terminates_only_the_exact_probe_process(self) -> None:
        class Stdout:
            def fileno(self) -> int:
                return 99

            def close(self) -> None:
                pass

        class Process:
            def __init__(self) -> None:
                self.stdout = Stdout()
                self.terminated = 0
                self.killed = 0
                self.waited = 0

            def poll(self):
                return None

            def terminate(self) -> None:
                self.terminated += 1

            def kill(self) -> None:
                self.killed += 1

            def wait(self, timeout=None):
                self.waited += 1
                return 0

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "slow.blend"
            path.write_bytes(policy.ZSTD_MAGIC + b"invalid")
            process = Process()
            with mock.patch.object(policy.select, "select", return_value=([], [], [])):
                header = policy.read_bounded_blend_header(
                    path,
                    zstd_binary="/usr/bin/zstd",
                    timeout_seconds=0.01,
                    popen=lambda *_args, **_kwargs: process,
                )
        self.assertEqual(header, b"")
        self.assertEqual(process.terminated, 1)
        self.assertEqual(process.killed, 0)
        self.assertEqual(process.waited, 1)


if __name__ == "__main__":
    unittest.main()
