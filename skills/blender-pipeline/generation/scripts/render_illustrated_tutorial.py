#!/usr/bin/env python3
"""Render an optional human-facing HTML view of verified tutorial evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


MAX_EMBED_BYTES = 32 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class TutorialRenderError(RuntimeError):
    """The operational tutorial cannot be rendered safely as HTML."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TutorialRenderError(f"cannot read JSON input {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise TutorialRenderError(f"JSON input must be an object: {path.name}")
    return value


def _inside(path: Path, root: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise TutorialRenderError(f"invalid evidence image: {path}") from exc
    if not resolved.is_file() or resolved.suffix.lower() not in IMAGE_SUFFIXES:
        raise TutorialRenderError(f"unsupported evidence image: {path}")
    return resolved


def _data_uri(path: Path, root: Path, budget: list[int]) -> str:
    resolved = _inside(path, root)
    payload = resolved.read_bytes()
    budget[0] += len(payload)
    if budget[0] > MAX_EMBED_BYTES:
        raise TutorialRenderError("embedded tutorial images exceed the 32 MiB limit")
    media_type = mimetypes.guess_type(resolved.name)[0] or "image/png"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, Mapping):
        return "；".join(
            f"{key}={item}" for key, item in value.items() if str(item).strip()
        )
    return str(value).strip()


def _step_html(step: Mapping[str, Any], index: int) -> str:
    details = []
    for label, key in (
        ("证据", "evidence"),
        ("参数", "parameters"),
        ("视觉结果", "visual_result"),
        ("材质 / 颜色 / 纹理", "material_color"),
        ("空间关系", "spatial_relation"),
        ("表面细节", "surface_detail"),
        ("复现约束", "implementation_notes"),
        ("不确定项", "uncertainty"),
    ):
        value = _text(step.get(key))
        if value:
            details.append(
                f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
            )
    time_range = html.escape(_text(step.get("time_range")))
    action = html.escape(_text(step.get("action")) or "未命名操作")
    subject = html.escape(_text(step.get("object")))
    subject_html = f'<span class="subject">{subject}</span>' if subject else ""
    return (
        '<article class="step">'
        f'<div class="step-index">{index:02d}</div>'
        '<div class="step-body">'
        f'<p class="time">{time_range}</p><h3>{action} {subject_html}</h3>'
        f"<dl>{''.join(details)}</dl></div></article>"
    )


def render(video_dir: Path, output: Path | None = None) -> dict[str, Any]:
    root = video_dir.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise TutorialRenderError("--video-dir must be a directory")
    steps_path = root / "steps_verified.json"
    if not steps_path.is_file():
        steps_path = root / "steps_rich.json"
    steps_value = _read_object(steps_path)
    steps = steps_value.get("steps")
    if not isinstance(steps, list) or not steps:
        raise TutorialRenderError("verified tutorial contains no steps")
    if any(not isinstance(item, Mapping) for item in steps):
        raise TutorialRenderError("every tutorial step must be an object")

    windows_path = root / "rich_evidence/windows.json"
    try:
        windows = json.loads(windows_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TutorialRenderError(f"cannot read evidence windows: {exc}") from exc
    if not isinstance(windows, list):
        raise TutorialRenderError("evidence windows must be an array")
    info = _read_object(root / "source.info.json")

    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for step in steps:
        try:
            window_index = int(step.get("window_index", 0))
        except (TypeError, ValueError) as exc:
            raise TutorialRenderError(
                "tutorial step has an invalid window index"
            ) from exc
        grouped[window_index].append(step)

    budget = [0]
    sections = []
    global_index = 1
    for window_index in sorted(grouped):
        if window_index < 0 or window_index >= len(windows):
            raise TutorialRenderError(
                "tutorial step references a missing evidence window"
            )
        window = windows[window_index]
        if not isinstance(window, Mapping):
            raise TutorialRenderError("evidence window must be an object")
        image_uri = _data_uri(Path(str(window.get("sheet") or "")), root, budget)
        start = float(window.get("start_sec") or 0)
        end = float(window.get("end_sec") or start)
        step_cards = []
        for step in grouped[window_index]:
            step_cards.append(_step_html(step, global_index))
            global_index += 1
        sections.append(
            '<section class="window">'
            f"<header><p>证据窗口 W{window_index:03d}</p>"
            f"<h2>{start:.0f}s – {end:.0f}s</h2></header>"
            f'<img src="{image_uri}" alt="W{window_index:03d} 视频证据拼图">'
            f'<div class="steps">{"".join(step_cards)}</div></section>'
        )

    title = html.escape(str(info.get("title") or root.name))
    source_url = html.escape(
        str(info.get("webpage_url") or info.get("original_url") or "")
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}｜图文教程</title><style>
:root {{ color-scheme:light; --ink:#171717; --muted:#6b6b6b; --line:#ded8ce; --paper:#faf7f1; --accent:#9b4dca }}
* {{ box-sizing:border-box }} body {{ margin:0; color:var(--ink); background:var(--paper); font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif }}
main {{ width:min(1080px,calc(100% - 32px)); margin:0 auto; padding:64px 0 96px }} .hero {{ padding-bottom:42px; border-bottom:1px solid var(--line) }}
.eyebrow,.window header p,.time {{ margin:0; color:var(--muted); font-size:12px; letter-spacing:.12em; text-transform:uppercase }} h1 {{ margin:10px 0 12px; font-size:clamp(36px,7vw,76px); line-height:1.05; letter-spacing:-.04em }}
.source {{ color:var(--muted); overflow-wrap:anywhere }} .notice {{ margin:26px 0 0; padding:14px 18px; border-left:4px solid var(--accent); background:#fff }}
.window {{ padding:52px 0; border-bottom:1px solid var(--line) }} .window h2 {{ margin:2px 0 22px; font-size:28px }} .window>img {{ display:block; width:100%; max-height:620px; object-fit:contain; background:#161616; border-radius:16px }}
.steps {{ display:grid; gap:14px; margin-top:26px }} .step {{ display:grid; grid-template-columns:54px 1fr; gap:14px; padding:20px; background:#fff; border:1px solid var(--line); border-radius:14px }}
.step-index {{ color:var(--accent); font-size:22px; font-variant-numeric:tabular-nums }} .step h3 {{ margin:2px 0 12px; font-size:21px }} .subject {{ color:var(--muted); font-weight:500 }}
dl {{ display:grid; grid-template-columns:minmax(110px,160px) 1fr; gap:6px 16px; margin:0 }} dt {{ color:var(--muted) }} dd {{ margin:0; overflow-wrap:anywhere }}
@media(max-width:640px) {{ main {{ padding-top:36px }} .step {{ grid-template-columns:1fr }} dl {{ grid-template-columns:1fr }} dt {{ margin-top:8px }} }}
</style></head><body><main><header class="hero"><p class="eyebrow">Video2Blender illustrated tutorial</p><h1>{title}</h1>
<p class="source">原视频：{source_url or "未记录"}</p><p class="notice">本页仅供人类阅读；pipeline 的唯一操作真值仍是 <strong>tutorial.md</strong> 与 <strong>steps_verified.json</strong>。</p></header>
{"".join(sections)}</main></body></html>"""

    target = output or (root / "illustrated_tutorial.html")
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise TutorialRenderError("HTML output must stay inside --video-dir") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(target)
    receipt = {
        "schema": "video2blender-illustrated-tutorial-receipt.v1",
        "operational_source": "tutorial.md",
        "steps_source": steps_path.name,
        "html": target.name,
        "html_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "step_count": len(steps),
        "window_count": len(grouped),
        "embedded_media_bytes": budget[0],
    }
    (root / "illustrated_tutorial_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(render(args.video_dir, args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TutorialRenderError as exc:
        print(f"error: {exc}")
        raise SystemExit(2)
