#!/usr/bin/env python3
"""Configure and launch tutorial-to-Blender reproduction with an API provider."""

from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(
    0, str(Path(__file__).resolve().parent / "skills/blender-pipeline/scripts")
)
from pipeline_launcher import main

if __name__ == "__main__":
    raise SystemExit(main("api"))
