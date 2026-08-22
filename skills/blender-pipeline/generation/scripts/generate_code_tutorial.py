#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def read_text(path: Path, limit: int | None = None) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if limit and len(text) > limit:
        return text[:limit] + "\n\n...[truncated]\n"
    return text


def detect_outputs(video_dir: Path, asset_dir: Path | None) -> dict[str, str]:
    base = asset_dir or video_dir
    candidates = {
        "asset": [video_dir / "asset.blend", base / "asset.blend"],
        "render": [video_dir / "render.png", base / "render.png"],
        "final_effect": [
            video_dir / "final_effect.mp4",
            base / "final_effect.mp4",
            base / "turntable_5s.mp4",
        ],
        "six_views": [video_dir / "six_views", base / "six_views"],
        "script": [video_dir / "reproduce.py", base / "reproduce.py"],
    }
    found: dict[str, str] = {}
    for key, paths in candidates.items():
        for path in paths:
            if path.exists():
                found[key] = str(path)
                break
    return found


def extract_step_summary(video_dir: Path) -> str:
    path = video_dir / "steps_verified.json"
    if not path.exists():
        path = video_dir / "steps_rich.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return ""
    steps = list(iter_steps(data))
    lines = []
    for idx, step in enumerate(steps[:120], start=1):
        action = step.get("action", "")
        obj = step.get("object", "")
        params = step.get("parameters", {})
        time_range = step.get("time_range", "")
        if action or obj or params:
            lines.append(
                f"{idx}. `{time_range}` {action} | object={obj} | params={params}"
            )
    return "\n".join(lines)


def extract_visual_contract_summary(video_dir: Path) -> str:
    path = video_dir / "tutorial_visual_contract.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return ""
    lines = []
    quality = data.get("quality") or {}
    lines.append(f"- visual_text_contract: {quality}")
    for contract in (data.get("contracts") or [])[:80]:
        idx = contract.get("window_index")
        lines.append(f"- W{int(idx):03d}" if isinstance(idx, int) else "- window")
        for key, label in [
            ("visible_objects", "objects"),
            ("materials", "materials"),
            ("surface_details", "surface_details"),
            ("spatial_layout", "spatial_layout"),
            ("final_constraints", "final_constraints"),
            ("do_not_omit", "do_not_omit"),
        ]:
            value = contract.get(key)
            if isinstance(value, list) and value:
                lines.append(f"  - {label}: " + "；".join(map(str, value[:12])))
            elif value:
                lines.append(f"  - {label}: {value}")
    return "\n".join(lines)


def iter_steps(value: Any):
    if isinstance(value, dict):
        steps = value.get("steps")
        if isinstance(steps, list):
            for item in steps:
                if isinstance(item, dict):
                    yield item
                else:
                    yield {"action": str(item)}
        for key, item in value.items():
            if key != "steps":
                yield from iter_steps(item)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if any(
                    k in item for k in ("action", "object", "parameters", "time_range")
                ):
                    yield item
                else:
                    yield from iter_steps(item)
            else:
                yield {"action": str(item)}


def infer_tags(text: str) -> list[str]:
    rules = {
        "geometry_modeling": [
            "建模",
            "mesh",
            "extrude",
            "bevel",
            "subdivision",
            "几何",
            "geometry",
        ],
        "materials_shading": [
            "材质",
            "shader",
            "BSDF",
            "material",
            "color",
            "roughness",
        ],
        "uv_texture_baking": ["UV", "贴图", "texture", "bake", "烘焙"],
        "lighting_camera_rendering": [
            "灯光",
            "相机",
            "render",
            "camera",
            "light",
            "Cycles",
            "Eevee",
        ],
        "animation_simulation": [
            "动画",
            "keyframe",
            "simulation",
            "particle",
            "cloth",
            "fluid",
            "rig",
        ],
    }
    lower = text.lower()
    tags = [
        name for name, words in rules.items() if any(w.lower() in lower for w in words)
    ]
    return tags or ["other"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--asset-dir", type=Path)
    parser.add_argument("--output-name", default="code_tutorial.md")
    args = parser.parse_args()

    video_dir = args.video_dir
    info_path = video_dir / "source.info.json"
    info = (
        json.loads(info_path.read_text(encoding="utf-8", errors="ignore"))
        if info_path.exists()
        else {}
    )
    outputs = detect_outputs(video_dir, args.asset_dir)
    code = (
        read_text(Path(outputs.get("script", "")), limit=45000)
        if outputs.get("script")
        else ""
    )
    step_summary = extract_step_summary(video_dir)
    visual_contract_summary = extract_visual_contract_summary(video_dir)
    tutorial_text = read_text(video_dir / "tutorial.md", limit=24000) or read_text(
        video_dir / "tutorial_path_refs.md", limit=24000
    )
    tag_text = "\n".join([info.get("title", ""), tutorial_text, step_summary, code])
    tags = infer_tags(tag_text)

    lines = [
        f"# {info.get('title') or video_dir.name}｜Blender 代码图文教程",
        "",
        "## 真值规则",
        "",
        "- `tutorial.md` 是唯一操作真值；本文件只能把它翻译成 Blender Python/API 执行视角。",
        "- 证据帧、成品图和视觉审核只辅助理解与验收，不能覆盖 `tutorial.md` 的步骤、参数和顺序。",
        "- 未在 `tutorial.md` 中出现的精确数值、额外对象、材质和动画逻辑不得凭空添加。",
        "- 如果本文件和 `tutorial.md` 冲突，必须先修正本文件，再进入代码生成。",
        "",
        "## 来源",
        "",
        f"- 视频链接：{info.get('webpage_url') or info.get('original_url') or ''}",
        f"- 视频源文件：`{info.get('source_video_path') or str(video_dir / 'source.mp4')}`",
        "- GUI 图文教程：`tutorial.md`",
        f"- 技术标签：{', '.join(tags)}",
        "",
        "## 预期输出",
        "",
        "- `reproduce.py`：必须逐条覆盖 `tutorial.md` 中的关键 GUI 操作。",
        "- `tutorial_visual_contract.json`：如果存在，必须覆盖其中的 visible_objects、materials、surface_details、spatial_layout、final_constraints 和 do_not_omit。",
        "- `asset.blend` / `render.png` / `final_effect.mp4`：只能作为执行结果，不能反向修改教程真值。",
    ]
    for key in ["asset", "render", "final_effect", "six_views", "script"]:
        if key in outputs:
            lines.append(f"- {key}: `{outputs[key]}`")
    lines.extend(["", "## GUI 操作到代码的映射", ""])
    lines.append(step_summary or "暂无结构化步骤；后续需要从 tutorial.md 补齐。")
    lines.extend(["", "## 视觉/材质/空间文字化约束", ""])
    lines.append(
        visual_contract_summary
        or "暂无 `tutorial_visual_contract.json`；代码生成阶段必须从 `tutorial.md` 的视觉结果、材质/颜色/纹理、空间关系、表面细节字段补齐。"
    )
    lines.extend(["", "## Blender API 实现边界", ""])
    lines.extend(
        [
            "- 优先使用当前执行 Blender 版本兼容的 `bpy` API。",
            "- 可以用程序化几何、材质节点、关键帧或轻量仿真近似教程效果，但必须保留教程中的对象、参数和顺序。",
            "- 视觉修复只能补充形状、材质或构图差异，不能删除或改写教程中的硬操作。",
            "- 动态教程必须输出真实动画逻辑；静态教程可以输出转台 `final_effect.mp4`。",
        ]
    )
    lines.extend(["", "## 已生成脚本摘要", ""])
    if code:
        imports = sorted(set(re.findall(r"^(?:import|from)\s+.+$", code, flags=re.M)))
        funcs = sorted(
            set(re.findall(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\(", code, flags=re.M))
        )
        lines.append(
            "- 主要模块："
            + ("；".join(imports[:12]) if imports else "未检测到显式 import")
        )
        lines.append(
            "- 主要函数：" + ("；".join(funcs[:20]) if funcs else "未检测到函数定义")
        )
    else:
        lines.append(
            "尚未生成 `reproduce.py`；代码生成阶段必须先读取本文件和 `tutorial.md`。"
        )
    lines.extend(
        [
            "",
            "## 复用经验",
            "",
            "- 后续复现同类视频时，优先复用本文件中的对象组织、材质参数、相机/灯光与动画实现方式。",
            "- 若 GUI 教程与代码实现冲突，以 `tutorial.md` 为准。",
            "",
        ]
    )
    out = video_dir / args.output_name
    out.write_text("\n".join(lines), encoding="utf-8")
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
