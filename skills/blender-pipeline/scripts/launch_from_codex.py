#!/usr/bin/env python3
"""Run the shared reproduction pipeline from the selectable Codex skill."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_launcher


def main(argv: list[str] | None = None) -> int:
    return pipeline_launcher.main("codex-cli", argv)


if __name__ == "__main__":
    raise SystemExit(main())
