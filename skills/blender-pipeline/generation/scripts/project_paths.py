"""Shared path resolution for Video2Blender command-line entry points."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


ENV_PREFIX = "BLENDER_PIPELINE_"
LEGACY_ENV_PREFIX = "PAPER12_"


def normalize_legacy_environment(
    environ: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Map deprecated project-specific options to the public package prefix."""

    target = os.environ if environ is None else environ
    for name, value in tuple(target.items()):
        if name.startswith(LEGACY_ENV_PREFIX):
            replacement = ENV_PREFIX + name.removeprefix(LEGACY_ENV_PREFIX)
            target.setdefault(replacement, value)
    return target


normalize_legacy_environment()


def env_path(name: str, default: Path | str) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else Path(default).expanduser()


SCRIPT_ROOT = Path(__file__).resolve().parent
GENERATION_ROOT = SCRIPT_ROOT.parent
PROJECT_ROOT = env_path("VIDEO2BLENDER_PROJECT_ROOT", GENERATION_ROOT).resolve()
OUTPUT_ROOT = env_path("VIDEO2BLENDER_OUTPUT_ROOT", PROJECT_ROOT / "output").resolve()
VIDEO_ROOT = env_path("BLENDER_VIDEO_ROOT", OUTPUT_ROOT / "videos")
CONFIG_ROOT = env_path(
    "VIDEO2BLENDER_CONFIG_ROOT", Path.home() / ".config/blender-pipeline"
)
SECRET_ROOT = env_path("VIDEO2BLENDER_SECRET_ROOT", CONFIG_ROOT / "secrets")
