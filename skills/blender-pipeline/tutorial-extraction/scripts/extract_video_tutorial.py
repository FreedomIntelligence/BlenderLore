#!/usr/bin/env python3
"""Video-to-visual-tutorial skill entrypoint, with optional replay adaptation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Sequence

from tutorial_extraction_core import (
    ALLOWED_MODELS,
    ALLOWED_PROVIDERS,
    PROFILES,
    ExtractionError,
    extraction_plan,
    validate_model_fallback,
)
from visual_tutorial_pipeline import extract_visual_tutorial, SCHEMA


def asr_language(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "auto":
        return normalized
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", normalized):
        raise argparse.ArgumentTypeError(
            "ASR language must be auto or a BCP-47-like language code such as zh or en"
        )
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--video-file", type=Path)
    source.add_argument("--video-url")
    parser.add_argument("--title", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="balanced")
    parser.add_argument("--model", choices=sorted(ALLOWED_MODELS), default="gpt-5.6-sol")
    parser.add_argument("--provider", choices=sorted(ALLOWED_PROVIDERS), default="api")
    parser.add_argument(
        "--fallback-reason",
        default="",
        help="required for gpt-5.5 and forbidden for gpt-5.6-sol",
    )
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--input-asset", type=Path, action="append", default=[],
                        help="actual learner input file or dependency folder; repeat as needed")
    parser.add_argument("--cache-dir", type=Path,
                        help="analysis cache outside the tutorial package; defaults beside output-dir")
    parser.add_argument("--max-calls", type=int, help="total model-call cap including at most one repair")
    parser.add_argument(
        "--asr-language",
        type=asr_language,
        default="auto",
        help="local Whisper language hint (default: auto; use zh for Chinese narration)",
    )
    parser.add_argument("--render-html", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source-url",
        default="",
        help=(
            "canonical public source URL to record when --video-file is used; "
            "it may also be used to discover platform subtitles"
        ),
    )
    parser.add_argument(
        "--window-budget",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--provided-transcript-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--workspace-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--replace-existing", action="store_true",
                        help="replace only the existing manifest-owned package in workspace mode")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.video_file is not None:
        try:
            video_file = args.video_file.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ExtractionError(f"--video-file is unavailable: {args.video_file}") from exc
        if not video_file.is_file():
            raise ExtractionError("--video-file must be a regular file")
    else:
        video_file = None
    if args.transcript is not None:
        try:
            transcript = args.transcript.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ExtractionError(f"--transcript is unavailable: {args.transcript}") from exc
        if not transcript.is_file():
            raise ExtractionError("--transcript must be a regular file")
    else:
        transcript = None
    if args.provided_transcript_only and transcript is None:
        raise ExtractionError("--provided-transcript-only requires --transcript")
    profile = PROFILES[args.profile]
    fallback_reason = validate_model_fallback(args.model, args.fallback_reason)
    plan = extraction_plan(
        title=args.title,
        profile=profile,
        model=args.model,
        video_file=video_file,
        video_url=args.video_url,
        output_dir=args.output_dir.expanduser().resolve(),
        render_html_enabled=args.render_html,
        asr_language_hint=None if args.asr_language == "auto" else args.asr_language,
        provider=args.provider,
        fallback_reason=fallback_reason,
    )
    if args.dry_run:
        plan.update({"schema": SCHEMA + ".plan", "skill": "video-to-visual-tutorial",
                     "learner_inputs": [str(p) for p in args.input_asset],
                     "workspace_mode": args.workspace_mode,
                     "package_layout": "<subject>/input + output/<subject>教程.md + output/<subject>验收评分Rubric.json + output/image",
                     "max_calls": args.max_calls})
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    endpoint = ""
    secret_file = None
    if args.provider == "api":
        endpoint = os.environ.get("BLENDER_PIPELINE_API_ENDPOINT", "").strip()
        secret_value = os.environ.get("BLENDER_PIPELINE_API_KEY_FILE", "").strip()
        if not endpoint or not secret_value:
            raise ExtractionError(
                "api execution requires BLENDER_PIPELINE_API_ENDPOINT and "
                "BLENDER_PIPELINE_API_KEY_FILE; codex-cli and dry-run do not"
            )
        secret_file = Path(secret_value)
    if args.window_budget is not None and args.window_budget < 0:
        raise ExtractionError("--window-budget must be nonnegative (0 means automatic)")
    if args.max_calls is not None and args.max_calls < 5:
        raise ExtractionError("--max-calls must be at least 5")
    manifest = extract_visual_tutorial(
        video_file=video_file,
        video_url=args.video_url,
        title=args.title,
        output_dir=args.output_dir,
        profile_name=args.profile,
        model=args.model,
        transcript=transcript,
        render_html_enabled=args.render_html,
        endpoint=endpoint,
        secret_file=secret_file,
        source_url=args.source_url,
        requested_windows=args.window_budget,
        provided_transcript_only=args.provided_transcript_only,
        asr_language_hint=None if args.asr_language == "auto" else args.asr_language,
        provider=args.provider,
        fallback_reason=fallback_reason,
        workspace_mode=args.workspace_mode,
        input_assets=args.input_asset,
        cache_dir=args.cache_dir,
        max_calls=args.max_calls,
        replace_existing=args.replace_existing,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
