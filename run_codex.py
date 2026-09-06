#!/usr/bin/env python3
"""Compatibility entry point for the selectable blender-pipeline Codex skill."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(
    0, str(Path(__file__).resolve().parent / "skills/blender-pipeline/scripts")
)
from launch_from_codex import main

if __name__ == "__main__":
    raise SystemExit(main())
