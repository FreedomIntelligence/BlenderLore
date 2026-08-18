from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "blender" / "scripts"


class ProjectPathsTests(unittest.TestCase):
    def _experiment_root(self, override: str | None) -> str:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS)
        if override is None:
            env.pop("VIDEO2BLENDER_EXPERIMENT_ROOT", None)
        else:
            env["VIDEO2BLENDER_EXPERIMENT_ROOT"] = override
        return subprocess.check_output(
            [
                sys.executable,
                "-c",
                "from project_paths import EXPERIMENT_ROOT; print(EXPERIMENT_ROOT)",
            ],
            env=env,
            text=True,
        ).strip()

    def test_experiment_root_defaults_to_shared_external_directory(self) -> None:
        self.assertEqual(
            self._experiment_root(None),
            "/Volumes/My Book/video2blender_experiments",
        )

    def test_experiment_root_allows_explicit_environment_override(self) -> None:
        self.assertEqual(
            self._experiment_root("~/isolated-video2blender-smoke"),
            str(Path.home() / "isolated-video2blender-smoke"),
        )


if __name__ == "__main__":
    unittest.main()
