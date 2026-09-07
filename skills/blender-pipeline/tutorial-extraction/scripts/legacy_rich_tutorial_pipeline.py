#!/usr/bin/env python3
"""Adapt the production rich-window tutorial recipe to either model provider.

This module deliberately imports the production prompts and runs the production
evidence preparer/merger. It does not call the historical v2 fragment extractor.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import tutorial_extraction_core as transport
from visual_tutorial_pipeline import (
    CachedCalls,
    IMAGE_RE,
    local_temporaries,
    render_html,
    safe_name,
)

SCHEMA = "video2blender-legacy-rich-tutorial.v1"
PRODUCTION = Path(__file__).resolve().parents[2] / "generation" / "scripts"
WINDOW_SECONDS = 60.0
FRAME_SECONDS = 5.0
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
Error = transport.ExtractionError


def production_module(name: str):
    """Load maintained production primitives without copying their prompts."""
    sys.path.insert(0, str(PRODUCTION))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.pop(0)


def complete_window_budget(
    duration: float, requested_windows: int | None, max_calls: int | None
) -> tuple[int, int]:
    if not math.isfinite(duration) or duration <= 0:
        raise Error("video duration must be positive")
    windows = max(1, math.ceil(duration / WINDOW_SECONDS))
    if requested_windows is not None and requested_windows < 0:
        raise Error("--window-budget must be nonnegative")
    if requested_windows and requested_windows < windows:
        raise Error(
            f"legacy-rich requires all {windows} chronological 60-second windows; "
            "increase --window-budget or use 0 (automatic); no windows were sampled away"
        )
    budget = max_calls if max_calls is not None else windows + 1
    if budget < windows + 1:
        raise Error(
            f"legacy-rich requires --max-calls >= {windows + 1} "
            f"({windows} complete windows plus one repair reserve)"
        )
    return windows, budget


def collect_assets(
    input_assets: Sequence[Path], source_video: Path | None
) -> dict[str, Path]:
    assets: dict[str, Path] = {}
    for raw in input_assets:
        try:
            asset = raw.expanduser().resolve(strict=True)
        except OSError as exc:
            raise Error(f"learner input is unavailable: {raw}") from exc
        if not (asset.is_file() or asset.is_dir()) or asset.name in assets:
            raise Error("input assets must be regular files/folders with unique names")
        paths = [asset] if asset.is_file() else list(asset.rglob("*"))
        if any(
            p.suffix.lower() in VIDEO_SUFFIXES
            or (source_video is not None and p.resolve() == source_video.resolve())
            for p in paths
        ):
            raise Error(
                "input assets must not include the source video or other video files"
            )
        assets[asset.name] = asset
    return assets


def prepare_evidence(
    cache: Path,
    video: Path,
    source: Mapping[str, Any],
    speech: transport.TranscriptResult,
) -> list[dict[str, Any]]:
    """Run the original 5-second-frame / 60-second-window producer once."""
    marker = cache / "prepared.json"
    identity = transport.canonical_sha256(
        {
            "source": dict(source),
            "segments": speech.segments,
            "script": transport.sha256_path(
                PRODUCTION / "prepare_rich_tutorial_evidence.py"
            ),
            "frame_seconds": FRAME_SECONDS,
            "window_seconds": WINDOW_SECONDS,
        }
    )
    stored = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
    windows_path = cache / "rich_evidence" / "windows.json"
    if stored.get("identity") == identity and windows_path.is_file():
        windows = json.loads(windows_path.read_text(encoding="utf-8"))
        if all((cache / item["sheet"]).is_file() for item in windows):
            return windows
    # An empty canonical segments file also prevents the production script from
    # searching unrelated global transcript roots when no narration is available.
    transport.atomic_jsonl(cache / "segments.jsonl", speech.segments)
    transport.run_command(
        [
            sys.executable,
            str(PRODUCTION / "prepare_rich_tutorial_evidence.py"),
            "--video-dir",
            str(cache),
            "--source",
            str(video),
            "--bvid",
            str(source["id"]),
            "--title",
            str(source["title"]),
            "--url",
            str(source["url"]),
            "--target-reference",
            str(cache / "not-provided.png"),
            "--frame-step",
            str(FRAME_SECONDS),
            "--window-sec",
            str(WINDOW_SECONDS),
            "--max-windows",
            "0",
        ],
        timeout=1800,
    )
    windows = json.loads(windows_path.read_text(encoding="utf-8"))
    if not windows or any(not (cache / item["sheet"]).is_file() for item in windows):
        raise Error(
            "production rich evidence did not produce every window contact sheet"
        )
    frame_rows = json.loads(
        (cache / "rich_evidence" / "frame_index.json").read_text(encoding="utf-8")
    )
    if any(
        not any(
            float(w["start_sec"]) <= float(r["timestamp_sec"]) < float(w["end_sec"])
            for r in frame_rows
        )
        for w in windows
    ):
        raise Error("a rich evidence window has no decoded source frames")
    transport.atomic_json(marker, {"identity": identity})
    return windows


def generate_chunks(
    cache: Path, windows: Sequence[Mapping[str, Any]], caller: CachedCalls
) -> dict[str, Any]:
    production = production_module("generate_rich_tutorial_chunks")
    info = json.loads((cache / "source.info.json").read_text(encoding="utf-8"))
    out = cache / "rich_tutorial_chunks"
    out.mkdir(exist_ok=True)
    repair_used = False
    for index, window in enumerate(windows):
        prompt = production.build_prompt(info, window, index)
        images = [cache / str(window["sheet"])]
        value = caller.call(f"rich-window-{index:03d}", prompt, images)
        # The transport stops on malformed JSON or uncertain delivery. Only an
        # already parsed object may receive a single format-only repair; no
        # transport retries or alternate weaker prompts are introduced here.
        if not isinstance(value.get("steps"), list) or any(
            not isinstance(step, dict) for step in value.get("steps", [])
        ):
            if repair_used:
                raise Error("the single rich JSON repair allowance was already used")
            repair_used = True
            value = caller.call(
                f"rich-format-repair-{index:03d}",
                "把下面已有响应仅修复为有效 JSON；不得补充、推测或改变任何事实。"
                "只输出 JSON 对象，steps 必须为对象数组。\n\n"
                + json.dumps(value, ensure_ascii=False),
                images,
            )
            if not isinstance(value.get("steps"), list) or any(
                not isinstance(step, dict) for step in value.get("steps", [])
            ):
                raise Error("rich format repair did not return a steps array")
        transport.atomic_json(
            out / f"window_{index:03d}.json",
            production.normalize_chunk(value, dict(window), index),
        )
    # The cache identity contains source, recipe, model and provider. Remove only
    # this adapter's stale numbered chunk projections if a prior run was longer.
    for path in out.glob("window_*.json"):
        if path.stem[7:].isdigit() and int(path.stem[7:]) >= len(windows):
            path.unlink()
    transport.run_command(
        [
            sys.executable,
            str(PRODUCTION / "merge_rich_tutorial_chunks.py"),
            "--video-dir",
            str(cache),
        ],
        timeout=120,
    )
    steps = json.loads((cache / "steps_rich.json").read_text(encoding="utf-8"))
    if not steps.get("steps"):
        raise Error(
            "production rich tutorial contains no operations; package was not published"
        )
    return {
        "repair_used": repair_used,
        "prompt_version": production.TUTORIAL_PROMPT_VERSION,
    }


def write_package(
    package: Path, cache: Path, source: Mapping[str, Any], assets: Mapping[str, Path]
) -> dict[str, str]:
    input_dir, output = package / "input", package / "output"
    input_dir.mkdir(parents=True)
    (output / "image").mkdir(parents=True)
    for name, asset in assets.items():
        if asset.is_dir():
            shutil.copytree(asset, input_dir / name)
        else:
            shutil.copy2(asset, input_dir / name)
    text = (cache / "tutorial_path_refs.md").read_text(encoding="utf-8")
    # Only rebase production paths and add the actual delivered inputs. The
    # original window prose, parameters, evidence and visual contracts survive.
    text = "\n".join(
        line for line in text.splitlines() if not line.startswith("- 视频源：")
    )

    def copy_image(match):
        relative = match.group(1)
        src = (cache / relative).resolve()
        if not src.is_relative_to(cache.resolve()) or not src.is_file():
            raise Error("production tutorial references unavailable source evidence")
        target = output / "image" / src.name
        if target.exists() and transport.sha256_path(target) != transport.sha256_path(
            src
        ):
            raise Error("production evidence image names collide")
        shutil.copy2(src, target)
        return match.group(0).replace(relative, f"image/{target.name}")

    text = IMAGE_RE.sub(copy_image, text)
    needed = [
        "## You will need",
        "",
        "目标版本：Blender 4.1 / 5.1.2；步骤按视频界面描述，未执行跨版本复现验证。",
        "",
    ]
    if assets:
        for name, asset in assets.items():
            use = (
                "打开并另存工作副本，再按视频步骤继续。"
                if asset.suffix.lower() == ".blend"
                else "保留目录内部相对路径，在视频对应的导入或贴图步骤中使用。"
                if asset.is_dir()
                else "在视频对应的导入或素材加载步骤中使用，保留原件。"
            )
            needed.append(f"- 输入素材：`input/{name}`。{use}")
    else:
        needed.append(
            "输入素材：无，`input/` 文件夹为空；按下文首个窗口描述准备起始场景。"
        )
    first, _, rest = text.partition("\n")
    text = first + "\n\n" + "\n".join(needed) + "\n\n" + rest.lstrip("\n") + "\n"
    refs = output / "tutorial_path_refs.md"
    transport.atomic_text(refs, text)
    embed = production_module("embed_markdown_images")
    stats = embed.embed_markdown(refs, output / "tutorial.md", refs, 1600, 86)
    if stats["missing"] or stats["skipped"] or stats["embedded"] < 1:
        raise Error("production rich tutorial images were not all embedded")
    return {
        "tutorial": "output/tutorial.md",
        "path_refs": "output/tutorial_path_refs.md",
    }


def publish_workspace(
    workspace: Path,
    package: Path,
    cache: Path,
    source: Mapping[str, Any],
    files: Mapping[str, str],
    metadata: Mapping[str, Any],
    html_enabled: bool = False,
) -> dict:
    prefix = (package / "output").relative_to(workspace).as_posix()
    for name, key in (
        ("tutorial.md", "tutorial"),
        ("tutorial_path_refs.md", "path_refs"),
    ):
        text = (package / files[key]).read_text(encoding="utf-8")
        if key == "path_refs":
            text = IMAGE_RE.sub(
                lambda m: m.group(0).replace(m.group(1), f"{prefix}/{m.group(1)}"), text
            )
        text = text.replace(
            "`input/", f"`{package.relative_to(workspace).as_posix()}/input/"
        )
        transport.atomic_text(workspace / name, text)
    steps = json.loads((cache / "steps_rich.json").read_text(encoding="utf-8"))
    transport.atomic_json(workspace / "steps_rich.json", steps)
    transport.atomic_json(
        workspace / "steps_verified.json",
        {
            "schema": SCHEMA + ".steps",
            "authority": "production rich-window extraction projection; no independent Claim Q-Gate was performed",
            "steps": steps["steps"],
        },
    )
    shutil.copy2(
        cache / "tutorial_visual_contract.json",
        workspace / "tutorial_visual_contract.json",
    )
    windows = json.loads(
        (cache / "rich_evidence" / "windows.json").read_text(encoding="utf-8")
    )
    frame_rows = json.loads(
        (cache / "rich_evidence" / "frame_index.json").read_text(encoding="utf-8")
    )
    replay_windows = []
    for index, item in enumerate(windows):
        # Empty intro/outro windows need no learner image, but the replay view
        # still retains their full interval and evidence without learner clutter.
        local_image = package / "output" / "image" / Path(item["sheet"]).name
        if local_image.is_file():
            image_path = local_image.relative_to(workspace).as_posix()
        else:
            dest = workspace / "rich_evidence" / "windows" / Path(item["sheet"]).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache / item["sheet"], dest)
            image_path = dest.relative_to(workspace).as_posix()
        rows = [
            r
            for r in frame_rows
            if item["start_sec"] <= r["timestamp_sec"] < item["end_sec"]
        ]
        replay_windows.append(
            {
                "index": index,
                "start_sec": item["start_sec"],
                "end_sec": item["end_sec"],
                "sheet": image_path,
                "frames": [
                    {"path": image_path, "time": r["timestamp_sec"]} for r in rows
                ],
            }
        )
    transport.atomic_json(workspace / "rich_evidence" / "windows.json", replay_windows)
    manifest = {
        "schema": SCHEMA,
        "tutorial_method": "legacy-rich",
        "status": "complete",
        "source": dict(source),
        "title": source["title"],
        "package": package.relative_to(workspace).as_posix(),
        "files": dict(files),
        **dict(metadata),
        "counts": {
            "steps": len(steps["steps"]),
            "windows": len(windows),
            "images": len(list((package / "output" / "image").iterdir())),
        },
        "tutorial_sha256": transport.sha256_path(workspace / "tutorial.md"),
        "path_refs_sha256": transport.sha256_path(workspace / "tutorial_path_refs.md"),
        "steps_sha256": transport.sha256_path(workspace / "steps_verified.json"),
        "package_hashes": {
            p.relative_to(package).as_posix(): transport.sha256_path(p)
            for p in package.rglob("*")
            if p.is_file() and p.name != ".DS_Store" and not p.name.startswith("._")
        },
    }
    if html_enabled:
        render_html(
            package / files["path_refs"], workspace / "illustrated_tutorial.html"
        )
    transport.atomic_json(workspace / "tutorial_manifest.json", manifest)
    return manifest


def validate_workspace(workspace: Path) -> list[str]:
    """Validate this recipe's real outputs, without implying v2 fact verification."""
    try:
        root = workspace.resolve()
        manifest = json.loads(
            (root / "tutorial_manifest.json").read_text(encoding="utf-8")
        )
        if manifest.get("schema") != SCHEMA:
            return ["not a legacy-rich tutorial workspace"]
        transport.validate_model_fallback(
            manifest["model"],
            manifest.get("fallback_reason", ""),
            provider=manifest.get("provider", ""),
        )
        if manifest.get("provider") not in transport.ALLOWED_PROVIDERS:
            return ["legacy-rich manifest has an invalid provider"]
        package = (root / manifest["package"]).resolve()
        if package.parent != root / "tutorial_package":
            return ["legacy-rich package escapes its owned subject directory"]
        for name, field in (
            ("tutorial.md", "tutorial_sha256"),
            ("tutorial_path_refs.md", "path_refs_sha256"),
            ("steps_verified.json", "steps_sha256"),
        ):
            if transport.sha256_path(root / name) != manifest[field]:
                return [f"{name} changed after projection"]
        for relative, digest in manifest["package_hashes"].items():
            path = (package / relative).resolve()
            if (
                not path.is_relative_to(package)
                or transport.sha256_path(path) != digest
            ):
                return ["learner package changed after projection"]
        embedded = IMAGE_RE.findall((root / "tutorial.md").read_text(encoding="utf-8"))
        if not embedded or any(not ref.startswith("data:image/") for ref in embedded):
            return ["legacy-rich tutorial must contain embedded images"]
        for ref in IMAGE_RE.findall(
            (root / "tutorial_path_refs.md").read_text(encoding="utf-8")
        ):
            image = (root / ref).resolve()
            if not image.is_relative_to(root) or not image.is_file():
                return ["legacy-rich path view has a missing or external image"]
        steps = json.loads((root / "steps_verified.json").read_text(encoding="utf-8"))
        if steps.get("schema") != SCHEMA + ".steps" or not steps.get("steps"):
            return ["legacy-rich steps are missing or mislabeled"]
        windows = json.loads(
            (root / "rich_evidence" / "windows.json").read_text(encoding="utf-8")
        )
        cursor = 0.0
        for window in windows:
            if abs(float(window["start_sec"]) - cursor) > 0.01:
                return ["legacy-rich chronological windows have a coverage gap"]
            cursor = float(window["end_sec"])
        if abs(cursor - float(manifest["source"]["duration_seconds"])) > 0.01:
            return ["legacy-rich windows do not cover the full source video"]
        return []
    except (OSError, ValueError, KeyError, TypeError, Error) as exc:
        return [str(exc)]


def extract_legacy_rich_tutorial(
    *,
    video_file: Path | None,
    video_url: str | None,
    title: str,
    output_dir: Path,
    profile_name: str,
    model: str,
    transcript: Path | None,
    render_html_enabled: bool,
    endpoint: str = "",
    secret_file: Path | None = None,
    source_url: str = "",
    requested_windows: int | None = None,
    provided_transcript_only: bool = False,
    asr_language_hint: str | None = None,
    provider: str = "api",
    fallback_reason: str = "",
    workspace_mode: bool = False,
    input_assets: Sequence[Path] = (),
    cache_dir: Path | None = None,
    max_calls: int | None = None,
    replace_existing: bool = False,
) -> dict:
    transport.validate_model_fallback(model, fallback_reason, provider=provider)
    if provider not in transport.ALLOWED_PROVIDERS:
        raise Error("provider must be api or codex-cli")
    workspace = output_dir.expanduser().resolve()
    cache_root = (
        (cache_dir or workspace.parent / ".video-tutorial-cache").expanduser().resolve()
    )
    if cache_root.is_relative_to(workspace):
        raise Error("analysis cache must be outside the tutorial workspace")
    subject = safe_name(title)
    package_parent = workspace / "tutorial_package" if workspace_mode else workspace
    destination = package_parent / subject
    owned_package = None
    if replace_existing:
        if not workspace_mode:
            raise Error(
                "--replace-existing is only supported in pipeline workspace mode"
            )
        path = workspace / "tutorial_manifest.json"
        previous = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )
        if previous.get("schema") in {SCHEMA, "video2blender-visual-tutorial.v1"}:
            owned_package = (workspace / previous["package"]).resolve()
            if owned_package.parent != workspace / "tutorial_package":
                raise Error(
                    "previous manifest does not own a tutorial-package subject directory"
                )
    if destination.exists() and destination.resolve() != owned_package:
        raise Error("tutorial package already exists; choose a new output directory")
    assets = collect_assets(input_assets, video_file)
    image_max_side = int(os.environ.get("RICH_TUTORIAL_IMAGE_MAX_SIDE", "640") or "640")
    if image_max_side < 320 or image_max_side > 4096:
        raise Error("RICH_TUTORIAL_IMAGE_MAX_SIDE must be 320..4096")
    cache_root.mkdir(parents=True, exist_ok=True)
    with (
        local_temporaries(cache_root),
        tempfile.TemporaryDirectory(prefix="source-", dir=cache_root) as raw_temp,
    ):
        temporary, platform = Path(raw_temp), []
        if video_file:
            video = video_file.expanduser().resolve(strict=True)
            if source_url and not provided_transcript_only:
                platform = transport.fetch_platform_subtitles(
                    source_url, temporary / "subtitles"
                )
        else:
            source_url = str(video_url)
            video, platform = transport.materialize_url(
                source_url, temporary / "download"
            )
        digest = transport.sha256_path(video)
        duration = transport.ffprobe_duration(video)
        window_count, budget = complete_window_budget(
            duration, requested_windows, max_calls
        )
        source = {
            "id": video.stem,
            "url": source_url,
            "title": title,
            "duration_seconds": duration,
            "sha256": digest,
        }
        identity = transport.canonical_sha256(
            {
                "source": source,
                "model": model,
                "provider": provider,
                "transcript": transport.sha256_path(transcript) if transcript else "",
                "language": asr_language_hint,
                "provided_only": provided_transcript_only,
                "platform": [transport.sha256_path(p) for p in platform],
                "schema": SCHEMA,
                "image_max_side": image_max_side,
            }
        )
        cache = cache_root / f"{digest[:20]}-legacy-rich-{identity[:12]}"
        cache.mkdir(exist_ok=True)
        transcript_cache = cache / "transcript.json"
        if transcript_cache.is_file():
            speech = transport.TranscriptResult(
                **json.loads(transcript_cache.read_text(encoding="utf-8"))
            )
        else:
            speech = transport.resolve_transcript(
                video,
                transcript,
                platform,
                platform_attempted=bool(source_url),
                allow_local_asr=not provided_transcript_only,
                asr_language_hint=asr_language_hint,
            )
            transport.atomic_json(transcript_cache, asdict(speech))
        windows = prepare_evidence(cache, video, source, speech)
        if len(windows) != window_count:
            raise Error(
                "production evidence window count does not cover the complete video"
            )
        profile = replace(
            transport.PROFILES[profile_name], image_max_side=image_max_side
        )
        key = (
            transport.read_secret(secret_file)
            if provider == "api" and secret_file
            else ""
        )
        if provider == "api":
            client = transport.ModelClient(
                endpoint=endpoint,
                key=key,
                model=model,
                profile=profile,
                call_budget=budget,
                max_output_tokens=10000,
            )
        else:
            client = transport.CodexCliModelClient(
                model=model, profile=profile, call_budget=budget
            )
        caller = CachedCalls(client, cache, "", key, endpoint)
        recipe = generate_chunks(cache, windows, caller)
        with tempfile.TemporaryDirectory(
            prefix="package-", dir=cache_root
        ) as raw_staging:
            staging = Path(raw_staging)
            package = staging / subject
            files = write_package(package, cache, source, assets)
            package_parent.mkdir(parents=True, exist_ok=True)
            backup = staging / "previous-package"
            if destination.exists():
                shutil.move(str(destination), backup)
            try:
                shutil.copytree(
                    package,
                    destination,
                    ignore=shutil.ignore_patterns("._*", ".DS_Store", "__MACOSX"),
                )
            except Exception:
                if destination.exists():
                    transport.remove_owned_tree(destination)
                if backup.exists():
                    shutil.move(str(backup), destination)
                raise
        metadata = {
            "model": model,
            "provider": provider,
            "profile": profile_name,
            "tutorial_method": "legacy-rich",
            "fallback_reason": fallback_reason,
            "recipe": {
                "producer": "prepare_rich_tutorial_evidence.py",
                "prompt": "generate_rich_tutorial_chunks.build_prompt",
                "merger": "merge_rich_tutorial_chunks.py",
                "embedder": "embed_markdown_images.py",
                "frame_seconds": FRAME_SECONDS,
                "window_seconds": WINDOW_SECONDS,
                "image_max_side": image_max_side,
                "coverage": "complete",
                **recipe,
            },
            "model_usage": {
                **asdict(client.usage),
                "call_budget": budget,
                "cache_hits": caller.cache_hits,
            },
            "transcript_status": speech.status,
            "warnings": [speech.warning] if speech.warning else [],
        }
        if workspace_mode:
            manifest = publish_workspace(
                workspace,
                destination,
                cache,
                source,
                files,
                metadata,
                render_html_enabled,
            )
            issues = validate_workspace(workspace)
            if issues:
                raise Error(
                    "legacy-rich tutorial adaptation failed: " + "; ".join(issues)
                )
            if (
                owned_package
                and owned_package != destination.resolve()
                and owned_package.is_dir()
            ):
                transport.remove_owned_tree(owned_package)
            return manifest
        if render_html_enabled:
            render_html(
                destination / files["path_refs"],
                workspace / "illustrated_tutorial.html",
            )
        return {
            "schema": SCHEMA,
            "status": "complete",
            "source": source,
            "package": str(destination),
            "files": files,
            **metadata,
        }
