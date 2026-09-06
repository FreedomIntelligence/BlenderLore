#!/usr/bin/env python3
"""Run the supplied visual-tutorial skill and adapt its output for replay."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import asdict, replace
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

import prepare_video
import tutorial_extraction_core as transport
import validate_visual_package as package_check

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "video2blender-visual-tutorial.v1"
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
Error = transport.ExtractionError


class RubricError(Error):
    pass


@contextmanager
def local_temporaries(root: Path):
    previous = tempfile.tempdir
    previous_env = os.environ.get("TMPDIR")
    tempfile.tempdir = str(root)
    os.environ["TMPDIR"] = str(root)
    try:
        yield
    finally:
        tempfile.tempdir = previous
        if previous_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_env


def instructions() -> str:
    paths = [
        ROOT / "SKILL.md",
        *[
            ROOT / "references" / name
            for name in (
                "evidence-ledger.md",
                "markdown-layout.md",
                "blender-capability-rubric.md",
            )
        ],
    ]
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    value = re.sub(r"[\s()\[\]#%]+", "-", value)
    if not value or value in {".", ".."}:
        raise Error("a nonempty content name is required")
    if value.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    }:
        value = "tutorial-" + value
    return value[:100].rstrip(" .")


def sampling(duration: float, profile: str) -> float:
    base = 2.0 if duration < 120 else 5.0 if duration <= 600 else 15.0
    return base * {"economy": 2.0, "balanced": 1.0, "forensic": 0.5}[profile]


class CachedCalls:
    def __init__(
        self, client: Any, root: Path, context: str, key: str = "", endpoint: str = ""
    ):
        self.client, self.root, self.context = client, root, context
        self.key, self.endpoint = key, endpoint
        self.cache_hits = 0

    def call(self, stage: str, prompt: str, images: Sequence[Path]) -> dict:
        prompt = self.context + "\n\n" + prompt
        identity = transport.canonical_sha256(
            {
                "model": self.client.model,
                "provider": type(self.client).__name__,
                "prompt": prompt,
                "images": [transport.sha256_path(p) for p in images],
            }
        )
        path = self.root / "responses" / f"{stage}-{identity}.json"
        if path.is_file():
            self.cache_hits += 1
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = self.client.call(prompt, list(images))
            transport.reject_sensitive_echo(value, key=self.key, endpoint=self.endpoint)
            transport.atomic_json(path, value)
        return value


def validate_document(
    doc: Mapping[str, Any],
    frames: Mapping[str, Mapping[str, Any]],
    duration: float,
    available_inputs: set[str],
) -> None:
    for field in ("content_name", "purpose", "starting_scene", "closing"):
        if not isinstance(doc.get(field), str) or not doc[field].strip():
            raise Error(f"tutorial document missing {field}")
    safe_name(doc["content_name"])
    if doc.get("missing_inputs"):
        raise Error(
            "required learner assets are unavailable: "
            + "; ".join(map(str, doc["missing_inputs"]))
        )
    inputs = doc.get("learner_inputs", [])
    if not isinstance(inputs, list):
        raise Error("learner_inputs must be a list")
    for item in inputs:
        if (
            item.get("name") not in available_inputs
            or not str(item.get("use", "")).strip()
        ):
            raise Error("tutorial claims an unavailable or unexplained learner input")
    if doc.get("cover_frame") not in frames:
        raise Error("cover must be an existing source-video frame")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise Error("tutorial has no ordered steps")
    previous = -1.0
    used_frames = {doc["cover_frame"]}
    for i, step in enumerate(steps, 1):
        if step.get("number") != i:
            raise Error("tutorial step numbers must be consecutive")
        start, end = float(step["time_start"]), float(step["time_end"])
        if not 0 <= start < end <= duration or start < previous:
            raise Error(f"invalid operation order in step {i}")
        previous = end
        frame = frames.get(str(step.get("evidence_frame")))
        if frame is None or not start <= float(frame["timestamp"]) <= end:
            raise Error(f"step {i} screenshot is outside its operation")
        if step["evidence_frame"] in used_frames:
            raise Error(f"step {i} reuses the cover or another step screenshot")
        used_frames.add(step["evidence_frame"])
        if (
            not isinstance(step.get("actions"), list)
            or not step["actions"]
            or any(
                not isinstance(action, str) or not action.strip()
                for action in step["actions"]
            )
        ):
            raise Error(f"step {i} has no learner actions")
        for field in ("title", "expected", "caption"):
            if not isinstance(step.get(field), str) or not step[field].strip():
                raise Error(f"step {i} missing {field}")
        claims = step.get("claims", [])
        if not claims:
            raise Error(f"step {i} has no source claims")
        for claim in claims:
            if claim.get("status") not in {"shown", "inferred", "recommendation"}:
                raise Error(f"invalid claim status in step {i}")
            if claim["status"] != "recommendation":
                at = float(claim.get("at", -1))
                if not start <= at <= end:
                    raise Error(f"claim timestamp outside step {i}")
            if claim["status"] == "shown":
                cited = frames.get(str(claim.get("frame")))
                if cited is None or not start <= float(cited["timestamp"]) <= end:
                    raise Error(f"shown claim has no inspected frame in step {i}")
    if not doc.get("final_result_description"):
        raise Error("tutorial must explain its final result")


def timestamp(value: float) -> str:
    return f"{int(value) // 60:02d}:{int(value) % 60:02d}"


def render_markdown(doc: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    lines = [
        f"# {doc['content_name']}教程",
        "",
        doc["purpose"],
        "",
        f"![视频中的最终效果](image/{Path(doc['cover_frame']).name})",
        "",
        doc["final_result_description"],
        "",
        "## You will need",
        "",
        "目标版本：Blender 4.1 / 5.1.2。"
        + str(
            doc.get("compatibility_notes")
            or "以下步骤按视频界面描述，未执行跨版本复现验证。"
        ),
        "",
        f"起始场景：{doc['starting_scene']}",
        "",
    ]
    if doc.get("learner_inputs"):
        for item in doc["learner_inputs"]:
            lines.append(f"- 输入素材：`input/{item['name']}`。{item['use']}")
    else:
        lines.append("输入素材：无，`input/` 文件夹为空。")
    lines.append("")
    parameters = doc.get("parameter_table", [])
    if parameters:
        lines.extend(["## 参数速查", "", "| 设置 | 视频中的设置 |", "| --- | --- |"])
        for row in parameters:
            lines.append(
                "| "
                + str(row["setting"]).replace("|", "\\|")
                + " | "
                + str(row["value"]).replace("|", "\\|")
                + " |"
            )
        lines.append("")
    for step in doc["steps"]:
        lines.extend(
            [
                f"## {step['number']}. {step['title']}（{timestamp(step['time_start'])}–{timestamp(step['time_end'])}）",
                "",
            ]
        )
        lines.extend(f"{i}. {action}" for i, action in enumerate(step["actions"], 1))
        lines.extend(
            [
                "",
                f"![{step['title']}](image/{Path(step['evidence_frame']).name})",
                "",
                step["caption"],
                "",
            ]
        )
        if step.get("explanation"):
            lines.extend([step["explanation"], ""])
        lines.extend([f"完成时：{step['expected']}", ""])
        if step.get("correction"):
            lines.extend([step["correction"], ""])
    lines.extend(
        [
            "## 收尾与保存",
            "",
            doc["closing"],
            "",
            f"来源：[{source.get('id') or source.get('title')}]({source.get('url')})"
            if source.get("url")
            else f"来源：{source.get('title')}（本地视频）",
            "",
        ]
    )
    return "\n".join(lines)


def write_package(
    package: Path,
    doc: Mapping[str, Any],
    rubric: Mapping[str, Any],
    source: Mapping[str, Any],
    analysis: Path,
    frames: Mapping[str, Any],
    assets: Mapping[str, Path],
) -> dict:
    validate_document(doc, frames, float(source["duration_seconds"]), set(assets))
    input_dir, output = package / "input", package / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    (output / "image").mkdir(parents=True, exist_ok=True)
    for item in doc.get("learner_inputs", []):
        src = assets[item["name"]]
        dest = input_dir / item["name"]
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copyfile(src, dest)
    content_name = safe_name(doc["content_name"])
    markdown = render_markdown(doc, source)
    tutorial = output / f"{content_name}教程.md"
    rubric_path = output / f"{content_name}验收评分Rubric.json"
    for ref in IMAGE_RE.findall(markdown):
        frame_key = "frames/" + Path(ref).name
        if frame_key not in frames:
            raise Error(f"unknown screenshot: {ref}")
        shutil.copyfile(analysis / frame_key, output / ref)
    transport.atomic_text(tutorial, markdown)
    transport.atomic_json(rubric_path, rubric)
    allowed = {item["sha256"] for item in frames.values()}
    package_check.validate_markdown(
        tutorial, output / "image", allowed, analysis / doc["cover_frame"]
    )
    try:
        package_check.validate_rubric(rubric_path)
    except SystemExit as exc:
        raise RubricError(str(exc)) from exc
    return {
        "tutorial": str(tutorial.relative_to(package)),
        "rubric": str(rubric_path.relative_to(package)),
    }


def render_html(markdown_path: Path, destination: Path) -> None:
    import markdown

    text = markdown_path.read_text(encoding="utf-8")

    def rebase(match):
        target = markdown_path.parent / match.group(1)
        path = Path(os.path.relpath(target, destination.parent)).as_posix()
        return match.group(0).replace(match.group(1), path)

    text = IMAGE_RE.sub(rebase, text)
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    transport.atomic_text(
        destination,
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>图文教程</title><style>body{max-width:960px;margin:40px auto;padding:0 24px;"
        "color:#20252d;font:16px/1.8 system-ui;background:white}img{max-width:100%;height:auto}"
        "table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #ddd}"
        "h2{margin-top:2em}code{background:#f1f3f6;padding:2px 4px}a{color:#245ca0}"
        "</style><body>" + body + "</body></html>",
    )


def publish_workspace(
    workspace: Path,
    package: Path,
    doc: Mapping[str, Any],
    source: Mapping[str, Any],
    files: Mapping[str, str],
    metadata: Mapping[str, Any],
    frames: Mapping[str, Any],
    html_enabled: bool = False,
) -> dict:
    tutorial = package / files["tutorial"]
    markdown = tutorial.read_text(encoding="utf-8")
    prefix = (package / "output").relative_to(workspace).as_posix()
    rebased = IMAGE_RE.sub(
        lambda m: m.group(0).replace(m.group(1), f"{prefix}/{m.group(1)}"), markdown
    )
    # Input paths in the learner package are relative to its subject directory.
    for item in doc.get("learner_inputs", []):
        rebased = rebased.replace(
            f"`input/{item['name']}`",
            f"`{package.relative_to(workspace).as_posix()}/input/{item['name']}`",
        )
    transport.atomic_text(workspace / "tutorial.md", rebased)
    transport.atomic_text(workspace / "tutorial_path_refs.md", rebased)
    steps, windows = [], []
    evidence_dir = workspace / "rich_evidence" / "windows"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for prior in evidence_dir.glob("w_*.jpg"):
        prior.unlink()
    for index, item in enumerate(doc["steps"]):
        image_path = f"{prefix}/image/{Path(item['evidence_frame']).name}"
        replay_image = evidence_dir / f"w_{index:03d}.jpg"
        shutil.copyfile(workspace / image_path, replay_image)
        steps.append(
            {
                "step_id": f"STEP-{index + 1:03d}",
                "window_index": index,
                "time_range": f"{item['time_start']:.3f}-{item['time_end']:.3f}",
                "action": "；".join(item["actions"]),
                "object": str(item.get("object") or ""),
                "parameters": item.get("parameters", {}),
                "visual_result": item["expected"],
                "material_color": item.get("material_color", ""),
                "spatial_relation": item.get("spatial_relation", ""),
                "surface_detail": item.get("surface_detail", ""),
                "implementation_notes": item.get("explanation", ""),
                "evidence": image_path,
                "source_claims": item["claims"],
            }
        )
        windows.append(
            {
                "index": index,
                "start_sec": item["time_start"],
                "end_sec": item["time_end"],
                "sheet": replay_image.relative_to(workspace).as_posix(),
                "frames": [
                    {
                        "path": replay_image.relative_to(workspace).as_posix(),
                        "time": frames[item["evidence_frame"]]["timestamp"],
                    }
                ],
            }
        )
    transport.atomic_json(
        workspace / "steps_verified.json",
        {
            "schema": SCHEMA + ".steps",
            "authority": "structured projection of the visual tutorial; not legacy Claim Q-Gate",
            "steps": steps,
        },
    )
    transport.atomic_json(
        workspace / "tutorial_visual_contract.json",
        transport.tutorial_visual_contract(steps),
    )
    transport.atomic_json(workspace / "rich_evidence" / "windows.json", windows)
    manifest = {
        "schema": SCHEMA,
        "skill": "video-to-visual-tutorial",
        "tutorial_method": "visual",
        "status": "complete",
        "source": dict(source),
        "title": doc["content_name"],
        "package": package.relative_to(workspace).as_posix(),
        **dict(metadata),
        "counts": {"steps": len(steps), "images": len(set(IMAGE_RE.findall(markdown)))},
        "tutorial_sha256": transport.sha256_path(workspace / "tutorial.md"),
        "steps_sha256": transport.sha256_path(workspace / "steps_verified.json"),
        "package_hashes": {
            p.relative_to(package).as_posix(): transport.sha256_path(p)
            for p in package.rglob("*")
            if p.is_file() and p.name != ".DS_Store" and not p.name.startswith("._")
        },
        "files": dict(files),
    }
    transport.atomic_json(workspace / "tutorial_manifest.json", manifest)
    if html_enabled:
        render_html(tutorial, workspace / "illustrated_tutorial.html")
    return manifest


def validate_workspace(workspace: Path) -> list[str]:
    try:
        manifest = json.loads(
            (workspace / "tutorial_manifest.json").read_text(encoding="utf-8")
        )
        if manifest.get("schema") != SCHEMA:
            return ["not a visual-tutorial workspace"]
        package = (workspace / manifest["package"]).resolve()
        if not package.is_relative_to(workspace.resolve()):
            return ["tutorial package escapes workspace"]
        tutorial = package / manifest["files"]["tutorial"]
        package_check.validate_markdown(tutorial, package / "output" / "image")
        package_check.validate_rubric(package / manifest["files"]["rubric"])
        if (
            transport.sha256_path(workspace / "tutorial.md")
            != manifest["tutorial_sha256"]
        ):
            return ["tutorial.md changed after package projection"]
        if (
            transport.sha256_path(workspace / "steps_verified.json")
            != manifest["steps_sha256"]
        ):
            return ["structured steps changed after package projection"]
        for relative, digest in manifest["package_hashes"].items():
            path = (package / relative).resolve()
            if (
                not path.is_relative_to(package)
                or transport.sha256_path(path) != digest
            ):
                return ["learner package changed after projection"]
        if (workspace / "tutorial_path_refs.md").read_bytes() != (
            workspace / "tutorial.md"
        ).read_bytes():
            return ["tutorial views differ"]
        for ref in IMAGE_RE.findall(
            (workspace / "tutorial.md").read_text(encoding="utf-8")
        ):
            if not (workspace / ref).is_file():
                return ["missing workspace tutorial image"]
        return []
    except (OSError, ValueError, KeyError, SystemExit) as exc:
        return [str(exc)]


def extract_visual_tutorial(
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
    transport.validate_model_fallback(model, fallback_reason)
    workspace = output_dir.expanduser().resolve()
    owned_package = None
    old_manifest = workspace / "tutorial_manifest.json"
    if replace_existing:
        if not workspace_mode:
            raise Error(
                "--replace-existing is only supported in pipeline workspace mode"
            )
        if old_manifest.is_file():
            previous = json.loads(old_manifest.read_text(encoding="utf-8"))
            if previous.get("schema") in {
                SCHEMA,
                "video2blender-legacy-rich-tutorial.v1",
            }:
                owned_package = (workspace / previous["package"]).resolve()
                if owned_package.parent != workspace / "tutorial_package":
                    raise Error(
                        "previous manifest does not own a tutorial-package subject directory"
                    )
    cache_root = (
        (cache_dir or workspace.parent / ".video-tutorial-cache").expanduser().resolve()
    )
    if cache_root.is_relative_to(workspace):
        raise Error("analysis cache must be outside the tutorial workspace")
    cache_root.mkdir(parents=True, exist_ok=True)
    assets = {}
    for raw_asset in input_assets:
        asset = raw_asset.expanduser().resolve(strict=True)
        if asset.name in assets or asset.suffix.lower() in {
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
        }:
            raise Error(
                "input assets must have unique names and must not include source video"
            )
        assets[asset.name] = asset
    profile = replace(transport.PROFILES[profile_name], image_max_side=2400)
    with (
        local_temporaries(cache_root),
        tempfile.TemporaryDirectory(prefix="source-", dir=cache_root) as temporary,
    ):
        source_temp = Path(temporary)
        platform = []
        if video_file:
            video = video_file.expanduser().resolve(strict=True)
            if source_url and not provided_transcript_only:
                platform = transport.fetch_platform_subtitles(
                    source_url, source_temp / "subtitles"
                )
        else:
            source_url = str(video_url)
            video, platform = transport.materialize_url(
                source_url, source_temp / "download"
            )
        digest = transport.sha256_path(video)
        duration = transport.ffprobe_duration(video)
        cache = cache_root / (digest[:20] + "-" + profile_name)
        cache.mkdir(exist_ok=True)
        video_index = prepare_video.prepare(
            video, cache, sampling(duration, profile_name)
        )
        frames = {r["path"]: r for r in video_index["frames"]}
        subtitle_cache = cache / "transcript.json"
        transcript_key = transport.canonical_sha256(
            {
                "provided": transport.sha256_path(transcript) if transcript else "",
                "language": asr_language_hint,
                "provided_only": provided_transcript_only,
                "platform": [transport.sha256_path(p) for p in platform],
            }
        )
        stored_transcript = (
            json.loads(subtitle_cache.read_text()) if subtitle_cache.exists() else {}
        )
        if stored_transcript.get("key") == transcript_key:
            speech = transport.TranscriptResult(**stored_transcript["value"])
        else:
            speech = transport.resolve_transcript(
                video,
                transcript,
                platform,
                platform_attempted=bool(source_url),
                allow_local_asr=not provided_transcript_only,
                asr_language_hint=asr_language_hint,
            )
            transport.atomic_json(
                subtitle_cache, {"key": transcript_key, "value": asdict(speech)}
            )
        budget = max_calls or profile.calls_per_ten_minutes * max(
            1, math.ceil(duration / 600)
        )
        chunks = max(1, math.ceil(duration / 180))
        if requested_windows:
            chunks = min(chunks, requested_windows)
        # All windows remain covered. Cost limits may enlarge windows, never skip them.
        chunks = min(chunks, max(1, (budget - 3) // 2))
        if budget < 5:
            raise Error(
                "visual tutorial needs at least five calls including one repair reserve"
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
                max_output_tokens=18000,
            )
        else:
            client = transport.CodexCliModelClient(
                model=model, profile=profile, call_budget=budget
            )
        caller = CachedCalls(client, cache, instructions(), key, endpoint)
        source = {
            "id": (re.search(r"BV[0-9A-Za-z]{10}", source_url) or [video.stem])[0],
            "url": source_url,
            "title": title,
            "duration_seconds": duration,
            "sha256": digest,
        }
        window_ledgers = []
        inspected_frames = set()
        max_requests = {"economy": 8, "balanced": 18, "forensic": 30}[profile_name]
        for number in range(chunks):
            start, end = number * duration / chunks, (number + 1) * duration / chunks
            rows = [r for r in video_index["frames"] if start <= r["timestamp"] <= end]
            sheets = []
            for offset in range(0, len(rows), 24):
                sheet = cache / f"window-{number:03d}-{offset:03d}.jpg"
                prepare_video.contact_sheet(
                    [cache / r["path"] for r in rows[offset : offset + 24]],
                    sheet,
                    cols=4,
                    thumb_width=360,
                )
                sheets.append(sheet)
            context = {
                "source": source,
                "window": [start, end],
                "frames": rows,
                "available_learner_assets": list(assets),
                "transcript": transport.segments_text(
                    speech.segments, start, end, limit=16000
                ),
                "previous_window_summary": window_ledgers[-1].get("summary", "")
                if window_ledgers
                else "",
            }
            coarse = caller.call(
                f"outline-{number}",
                "Inspect these contact sheets once. Reconstruct ALL distinct operations in this interval, "
                "including setup and final state when present. Group micro-actions by learner intent. "
                "Return JSON with summary, starting_scene, required_inputs, steps, final_result_candidates, "
                "and evidence_requests [{timestamp,reason}]. Every step needs time_start/time_end, title, "
                "actions, expected, evidence_frame, and claims [{text,status,at,frame}]. "
                "Request close inspection for exact values, wiring, modifier order and final result. "
                f"At most {max_requests} local timestamp requests. Frame names encode milliseconds.\n"
                + json.dumps(context, ensure_ascii=False),
                sheets,
            )
            detail_paths = []
            requests = coarse.get("evidence_requests", [])
            if not isinstance(requests, list) or len(requests) > max_requests:
                raise Error(
                    "model requested more focused frames than the selected profile permits"
                )
            for request in requests:
                t = round(float(request["timestamp"]), 3)
                if not start <= t < min(duration, end + 0.001):
                    raise Error("focused timestamp is outside its source interval")
                filename = f"frame_{round(t * 1000):09d}.jpg"
                path = cache / "frames" / filename
                entry = prepare_video.frame_at(video, path, t)
                frames[entry["path"]] = entry
                detail_paths.append(path)
            for step in coarse.get("steps", []):
                if step.get("evidence_frame") in frames:
                    detail_paths.append(cache / step["evidence_frame"])
            for candidate in coarse.get("final_result_candidates", []):
                frame_name = (
                    candidate.get("frame") if isinstance(candidate, dict) else candidate
                )
                if frame_name in frames:
                    detail_paths.append(cache / frame_name)
            detail_paths = list(dict.fromkeys(detail_paths))
            if not detail_paths:
                detail_paths = [cache / rows[-1]["path"]]
            inspected_frames.update(
                p.relative_to(cache).as_posix() for p in detail_paths
            )
            reviewed = caller.call(
                f"review-{number}",
                "Inspect the attached original frames at full size. Correct the draft ledger from visual "
                "evidence, with special attention to exact values, node wiring and operation order. "
                "Keep all useful operations; label uncertain values rather than deleting the entire step. "
                "Only claim 'shown' using a directly attached frame and its timestamp. Return the complete "
                "corrected window ledger (summary, starting_scene, required_inputs, steps, final_result_candidates). "
                "Include input/output continuity for every step.\n"
                + json.dumps(
                    {
                        "context": context,
                        "draft": coarse,
                        "attached_frames": [
                            frames[p.relative_to(cache).as_posix()]
                            for p in detail_paths
                        ],
                    },
                    ensure_ascii=False,
                ),
                detail_paths,
            )
            window_ledgers.append(reviewed)
        authoritative_frames = {
            k: v for k, v in frames.items() if k in inspected_frames
        }
        transport.atomic_json(cache / "reviewed-ledgers.json", window_ledgers)
        prompt = """Create one complete learner-facing tutorial plan from these reviewed ledgers.
Return JSON with content_name (subject, no platform title/ID), purpose, starting_scene,
compatibility_notes (state untested versions), learner_inputs [{name,use}], missing_inputs [],
cover_frame, final_result_description, parameter_table [{setting,value}], closing,
and steps [{number,time_start,time_end,title,actions:[string],evidence_frame,caption,
explanation,expected,correction,object,parameters:{},claims:[{text,status,at,frame}]}].
Use Chinese prose, preserve relevant original UI names. Every exact fact must be supported by a
shown claim; label recommendations clearly in learner prose. Do not insert harness/QA/grading
instructions, check IDs or background project assumptions. Explain where inputs and objects
come from and what each operation produces. Preserve the whole process, not only isolated
verified fragments. Merge duplicate adjacent micro-actions, retain chronological causal order.
Use one unique screenshot per step and a different actual final-result frame for the cover.
Use nonoverlapping chronological time ranges of positive duration for the ordered steps.
Choose frames ONLY from inspected_frames, and keep step screenshots within the step time range.
If a required asset is unavailable, return it in missing_inputs rather than inventing it.
Do not claim cross-version execution tests have taken place. Do not write a Blender solution.
"""
        document_context = json.dumps(
            {
                "source": source,
                "ledgers": window_ledgers,
                "inspected_frames": list(authoritative_frames.values()),
                "available_inputs": list(assets),
            },
            ensure_ascii=False,
        )
        document_image = [cache / next(iter(authoritative_frames))]
        doc = caller.call("document", prompt + document_context, document_image)
        repair_used = False
        try:
            validate_document(doc, authoritative_frames, duration, set(assets))
        except Error as exc:
            if doc.get("missing_inputs"):
                raise
            doc = caller.call(
                "document-repair",
                prompt
                + document_context
                + "\nCorrect only these format/source-binding problems without inventing evidence: "
                + str(exc)
                + "\nDraft: "
                + json.dumps(doc, ensure_ascii=False),
                document_image,
            )
            repair_used = True
            validate_document(doc, authoritative_frames, duration, set(assets))
        rubric_prompt = """Write the separate 100-point JSON rubric specified by the skill.
Use only GEO, PROC, SURF, SCN, RIG, ANM, SIM, PIPE. Match every scored ID to complete
non-destructive Blender Python in artifact_verifier.python_source. Return the rubric object
itself with schema_version,title,source_video,artifact_type,blender_versions,total_points,
status_weights,capability_totals,rubric,artifact_verifier. Each rubric row must have id,
capability,criterion,points,scoring_rule,artifact_verifier_check. status_weights must be
{"PASS":1.0,"PARTIAL":0.5,"FAIL":0.0}. artifact_verifier.engine must be blender_python.
Inspect the future submitted blend without modifying/saving it. Report JSON scores, do not
generate the target model. Cover only observable operations taught by the tutorial.
Unknown source values must not become exact scoring thresholds. No extra commentary.
"""
        rubric = caller.call(
            "rubric",
            rubric_prompt + render_markdown(doc, source),
            [cache / doc["cover_frame"]],
        )
        subject = safe_name(doc["content_name"])
        package_parent = workspace / "tutorial_package" if workspace_mode else workspace
        destination = package_parent / subject
        if destination.exists() and destination.resolve() != owned_package:
            raise Error(
                "tutorial package already exists; choose a new output directory"
            )
        with tempfile.TemporaryDirectory(prefix="package-", dir=cache_root) as staging:
            package = Path(staging) / subject
            try:
                files = write_package(
                    package, doc, rubric, source, cache, authoritative_frames, assets
                )
            except RubricError as exc:
                if repair_used:
                    raise Error(
                        "the single repair allowance was already used; " + str(exc)
                    ) from exc
                # One shared document/rubric repair allowance, never an unlimited loop.
                rubric = caller.call(
                    "rubric-repair",
                    rubric_prompt
                    + "\nFix this validation error: "
                    + str(exc)
                    + "\n"
                    + json.dumps(rubric, ensure_ascii=False),
                    [cache / doc["cover_frame"]],
                )
                transport.remove_owned_tree(package)
                files = write_package(
                    package, doc, rubric, source, cache, authoritative_frames, assets
                )
            package_parent.mkdir(parents=True, exist_ok=True)
            backup = Path(staging) / "previous-package"
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
        transport.atomic_json(cache / "document.json", doc)
        transport.atomic_json(
            cache / "ledger.json", {"source": source, "steps": doc["steps"]}
        )
        metadata = {
            "model": model,
            "provider": provider,
            "profile": profile_name,
            "tutorial_method": "visual",
            "fallback_reason": fallback_reason,
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
                doc,
                source,
                files,
                metadata,
                authoritative_frames,
                render_html_enabled,
            )
            issues = validate_workspace(workspace)
            if issues:
                raise Error("pipeline tutorial adaptation failed: " + "; ".join(issues))
            if (
                owned_package
                and owned_package != destination.resolve()
                and owned_package.is_dir()
            ):
                transport.remove_owned_tree(owned_package)
            return manifest
        if render_html_enabled:
            render_html(
                destination / files["tutorial"], workspace / "illustrated_tutorial.html"
            )
        return {
            "schema": SCHEMA,
            "status": "complete",
            "package": str(destination),
            "files": files,
            **metadata,
        }
