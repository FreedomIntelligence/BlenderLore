from __future__ import annotations

import json
import os
import stat
import struct
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "blender" / "scripts" / "run_a600_server.sh"
GPU_UUID = "GPU-12345678-1234-1234-1234-123456789abc"


class A600ServerShellTests(unittest.TestCase):
    def executable(self, path: Path, source: str) -> Path:
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        self.executable(fake_bin / "uname", "#!/bin/sh\nprintf 'Linux\\n'\n")
        self.executable(
            fake_bin / "nvidia-smi",
            f"""
            #!/bin/sh
            printf '0, {GPU_UUID}, %s\\n' "${{FAKE_GPU_NAME:-NVIDIA RTX A6000}}"
            """,
        )
        self.executable(
            fake_bin / "flock",
            """
            #!/usr/bin/env python3
            import fcntl
            import os
            import sys
            if os.environ.get("FAKE_FLOCK_BUSY") == "1":
                raise SystemExit(1)
            fd = int(sys.argv[-1])
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(1)
            """,
        )
        self.executable(
            fake_bin / "mv",
            """
            #!/usr/bin/env python3
            import os
            import sys
            values = [value for value in sys.argv[1:] if value not in {"-nT", "--"}]
            source, destination = values
            if os.path.lexists(destination):
                raise SystemExit(0)
            os.rename(source, destination)
            """,
        )
        self.executable(fake_bin / "ffmpeg", "#!/bin/sh\nexit 0\n")
        self.executable(
            fake_bin / "ffprobe",
            """
            #!/bin/sh
            case "${1:-}" in
              -version) exit 0 ;;
            esac
            printf '%s\\n' '{"streams":[{"width":1280,"height":720,"avg_frame_rate":"24/1","duration":"5.000"}],"format":{"duration":"5.000"}}'
            """,
        )
        blender_log = root / "blender-invocations.jsonl"
        blender = self.executable(
            fake_bin / "blender",
            """
            #!/usr/bin/env python3
            import json
            import fcntl
            import os
            import struct
            import sys
            from pathlib import Path

            if "--version" in sys.argv:
                print("Blender 4.5.3")
                raise SystemExit(0)
            log = Path(os.environ["FAKE_BLENDER_LOG"])
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"argv": sys.argv[1:], "gpu": os.environ.get("CUDA_VISIBLE_DEVICES"), "expected": os.environ.get("TOTAL_ASSET_EXPECTED_GPU_UUID")}) + "\\n")
            if os.environ.get("ASSERT_GPU_LOCK_HELD") == "1":
                competitor = os.open("/tmp/total_asset_gpu_0.lock", os.O_WRONLY | os.O_CREAT, 0o600)
                try:
                    try:
                        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        raise SystemExit("GPU lock was not held throughout Blender execution")
                finally:
                    os.close(competitor)
            if "--python-expr" in sys.argv:
                raise SystemExit(0)
            values = sys.argv[sys.argv.index("--") + 1:]
            def value(name):
                return values[values.index(name) + 1]
            out = Path(value("--out-dir"))
            out.mkdir(parents=True, exist_ok=True)
            if os.environ.get("FAKE_CREATE_FINAL_OUTPUT") == "1":
                final_name = out.name.split(".staging.", 1)[0].lstrip(".")
                concurrent = out.parent / final_name
                concurrent.mkdir(exist_ok=True)
                (concurrent / "owner.txt").write_text("concurrent", encoding="utf-8")
            route = value("--route")
            if route == "auto":
                route = os.environ.get("FAKE_RESOLVED_ROUTE", "static")
            static_views = value("--static-views")
            (out / "asset.blend").write_bytes(b"BLENDER")
            contract = {
                "render_route": route,
                "packed_resources": {"status": "packed", "critical_missing": []},
                "static_views": static_views if route == "static" else None,
                "fps": 24 if route == "dynamic" else None,
                "duration_sec": 5.0 if route == "dynamic" else None,
            }
            (out / "render_contract.json").write_text(json.dumps(contract), encoding="utf-8")
            if route == "static":
                names = ["iso"] if static_views == "iso-only" else ["front", "back", "left", "right", "top", "iso"]
                views = out / "six_views"
                views.mkdir()
                header = b"\\x89PNG\\r\\n\\x1a\\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1200, 1200)
                for name in names:
                    (views / f"{name}.png").write_bytes(header)
            else:
                (out / "final_effect.mp4").write_bytes(b"FAKE-MP4")
            """,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment.get('PATH', '')}",
                "NVIDIA_SMI_BIN": str(fake_bin / "nvidia-smi"),
                "FAKE_BLENDER_LOG": str(blender_log),
            }
        )
        return environment, blender_log

    def run_script(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_help_documents_contract_and_busy_exit(self) -> None:
        result = self.run_script("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight", result.stdout)
        self.assertIn("1280x720, 24 fps", result.stdout)
        self.assertIn("status 75", result.stdout)

    def test_preflight_maps_index_to_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env, _ = self.fixture(Path(raw))
            result = self.run_script("preflight", "--gpu", "0", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"gpu_uuid={GPU_UUID}", result.stdout)
        self.assertIn("NVIDIA\\ RTX\\ A6000", result.stdout)

    def test_preflight_accepts_rtx_a600_name_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env, _ = self.fixture(Path(raw))
            env["FAKE_GPU_NAME"] = "NVIDIA RTX A600"
            result = self.run_script("preflight", "--gpu", "0", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("not identified as an RTX A600", result.stderr)

    def test_preflight_output_probe_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, _ = self.fixture(root)
            output = root / "not-created" / "result"
            result = self.run_script("preflight", "--output", str(output), env=env)
            exists_after_probe = output.exists() or output.parent.exists()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(exists_after_probe)
        self.assertIn("status=writable_parent", result.stdout)

    def test_preflight_rejects_output_symlink_and_trailing_slash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, _ = self.fixture(root)
            destination = root / "destination"
            destination.mkdir()
            link = root / "link"
            link.symlink_to(destination, target_is_directory=True)
            symlink = self.run_script("preflight", "--output", str(link), env=env)
            trailing = self.run_script("preflight", "--output", f"{destination}/", env=env)
        self.assertEqual(symlink.returncode, 69)
        self.assertIn("rejects symlinks", symlink.stderr)
        self.assertEqual(trailing.returncode, 64)

    def test_highqal_static_is_iso_only_and_gpu_is_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, log = self.fixture(root)
            env["ASSERT_GPU_LOCK_HELD"] = "1"
            source = root / "source.blend"
            source.write_bytes(b"SOURCE")
            output = root / "result"
            result = self.run_script(
                "render", "--input", str(source), "--output", str(output),
                "--gpu", "0", "--route", "static", "--profile", "highqal", env=env,
            )
            records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            views = sorted(path.name for path in (output / "six_views").iterdir())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(views, ["iso.png"])
        self.assertEqual(records[0]["gpu"], GPU_UUID)
        self.assertEqual(records[0]["expected"], GPU_UUID)
        self.assertIn("--static-views", records[0]["argv"])
        self.assertIn("render_complete", result.stdout)

    def test_concurrent_output_is_preserved_and_not_nested(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, _ = self.fixture(root)
            env["FAKE_CREATE_FINAL_OUTPUT"] = "1"
            source = root / "source.blend"
            source.write_bytes(b"SOURCE")
            output = root / "result"
            result = self.run_script(
                "render", "--input", str(source), "--output", str(output),
                "--gpu", "0", "--route", "static", "--profile", "highqal", env=env,
            )
            owner = (output / "owner.txt").read_text(encoding="utf-8")
            nested = list(output.glob(".*.staging.*"))
        self.assertEqual(result.returncode, 78)
        self.assertEqual(owner, "concurrent")
        self.assertEqual(nested, [])
        self.assertIn("existing output was preserved", result.stderr)

    def test_total_static_requires_all_six_views(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, _ = self.fixture(root)
            source = root / "source.fbx"
            source.write_bytes(b"SOURCE")
            output = root / "result"
            result = self.run_script(
                "render", "--input", str(source), "--output", str(output),
                "--gpu", "0", "--route", "static", "--profile", "total", env=env,
            )
            views = sorted(path.name for path in (output / "six_views").iterdir())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(views, ["back.png", "front.png", "iso.png", "left.png", "right.png", "top.png"])

    def test_dynamic_contract_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, _ = self.fixture(root)
            env["FAKE_RESOLVED_ROUTE"] = "dynamic"
            source = root / "source.glb"
            source.write_bytes(b"SOURCE")
            output = root / "result"
            result = self.run_script(
                "render", "--input", str(source), "--output", str(output),
                "--gpu", "0", "--route", "auto", "--profile", "total", env=env,
            )
            blend_exists = (output / "asset.blend").is_file()
            video_exists = (output / "final_effect.mp4").is_file()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(blend_exists)
        self.assertTrue(video_exists)
        self.assertIn("resolved_route=dynamic", result.stdout)

    def test_gpu_lock_contention_exits_75_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, log = self.fixture(root)
            env["FAKE_FLOCK_BUSY"] = "1"
            source = root / "source.blend"
            source.write_bytes(b"SOURCE")
            result = self.run_script(
                "render", "--input", str(source), "--output", str(root / "result"),
                "--gpu", "0", env=env,
            )
        self.assertEqual(result.returncode, 75)
        self.assertIn("gpu_busy", result.stderr)
        self.assertFalse(log.exists())

    def test_rejects_unknown_options_and_existing_output(self) -> None:
        unknown = self.run_script("render", "--surprise", "yes")
        self.assertEqual(unknown.returncode, 64)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, _ = self.fixture(root)
            source = root / "source.blend"
            source.write_bytes(b"SOURCE")
            output = root / "result"
            output.mkdir()
            existing = self.run_script(
                "render", "--input", str(source), "--output", str(output), "--gpu", "0", env=env,
            )
        self.assertEqual(existing.returncode, 64)
        self.assertIn("refusing to overwrite", existing.stderr)


if __name__ == "__main__":
    unittest.main()
