#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from blender_version_registry import build_version_plan
from PIL import Image, ImageStat

RESAMPLE_LANCZOS = getattr(
    getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
)
SCRIPTS_DIR = Path(__file__).resolve().parent


DYNAMIC_TERMS = [
    "动画",
    "动态",
    "关键帧",
    "绑定",
    "骨骼",
    "模拟",
    "流体",
    "水花",
    "液体",
    "布料",
    "膨胀",
    "粒子",
    "毛发动力",
    "烟雾",
    "火焰",
    "爆炸",
    "刚体",
    "软体",
    "physics",
    "simulation",
    "fluid",
    "splash",
    "cloth",
    "hair dynamics",
    "particle",
    "keyframe",
    "animation",
    "rig",
]

STRONG_DYNAMIC_TERMS = [
    "关键帧",
    "毛发动力",
    "流体",
    "水花",
    "液体",
    "布料",
    "膨胀",
    "模拟",
    "烟雾",
    "火焰",
    "爆炸",
    "刚体",
    "soft body",
    "fluid",
    "splash",
    "cloth",
    "keyframe",
    "simulation",
]

FULL_TRANSCRIPT_DYNAMIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "keyframe",
        re.compile(r"关键帧|插帧|key[\s_-]*frame", re.I),
    ),
    (
        "animated_parameter",
        re.compile(
            r"(?:动画|驱动|animate|animated|key[\s_-]*frame).{0,28}"
            r"(?:noise|scale|rotation|location|position|value|strength|"
            r"噪声|缩放|旋转|位置|数值|强度|颜色)|"
            r"(?:noise|scale|rotation|location|position|value|strength|"
            r"噪声|缩放|旋转|位置|数值|强度|颜色).{0,28}"
            r"(?:动画|驱动|animate|animated|key[\s_-]*frame)",
            re.I,
        ),
    ),
    (
        "scene_time",
        re.compile(r"scene[\s_-]*time|time[\s_-]*node|场景时间|时间节点", re.I),
    ),
)

COLOR_TERMS = [
    "红",
    "橙",
    "黄",
    "绿",
    "青",
    "蓝",
    "紫",
    "粉",
    "白",
    "黑",
    "灰",
    "棕",
    "金",
    "银",
    "透明",
    "red",
    "orange",
    "yellow",
    "green",
    "cyan",
    "blue",
    "purple",
    "pink",
    "white",
    "black",
    "gray",
    "grey",
    "brown",
    "gold",
    "silver",
    "transparent",
]

MATERIAL_TERMS = [
    "材质",
    "贴图",
    "纹理",
    "shader",
    "Shader",
    "PBR",
    "金属",
    "玻璃",
    "塑料",
    "木",
    "布料",
    "皮肤",
    "毛发",
    "发光",
    "透明",
    "粗糙",
    "金属度",
    "法线",
    "凹凸",
    "置换",
    "烘焙",
    "metal",
    "glass",
    "plastic",
    "wood",
    "fabric",
    "skin",
    "hair",
    "emission",
    "transparent",
    "roughness",
    "metallic",
    "normal",
    "bump",
    "displacement",
    "texture",
]

GENERIC_MULTI_TOPIC_COURSE_TERMS = (
    "平面设计",
    "动态图形",
    "角色设计",
    "图标设计",
    "建模",
    "渲染",
    "灯光",
    "材质",
    "全流程",
)


def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term).replace(r"\ ", r"[\s_-]+")
    if re.search(r"[A-Za-z0-9]", term):
        return re.compile(
            rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])",
            re.I,
        )
    return re.compile(escaped, re.I)


def _positive_term_matches(text: str, term: str) -> bool:
    """Ignore explicit negations while retaining real material evidence."""

    for match in _term_pattern(term).finditer(text or ""):
        prefix = (text or "")[max(0, match.start() - 100) : match.start()]
        # Negation belongs to its clause; "no X, but add Y" must retain Y.
        prefix = re.split(
            r"[。！？!?;；\n]|\b(?:but|however|instead)\b|但是|但|而是|"
            r"[，,]\s*(?=(?:另外|然后|再)?(?:添加|使用|设置|创建|连接|建立))",
            prefix,
            flags=re.I,
        )[-1]
        if re.search(
            r"(?:未|没有|并无|无需|无须|无(?!缝|限|穷|论|法)|"
            r"不(?:要|需|会|是|含|包含|使用|设置)|禁止|避免).{0,64}$|"
            r"(?:无|不|非)\s*$",
            prefix,
            re.I,
        ):
            continue
        if re.search(
            r"(?:\bno|\bnot|\bwithout|\bnever|\bdo\s+not|"
            r"\bdoes\s+not|\bdon't|\bdoesn't)(?:[\s,]+[\w'-]+){0,10}[\s,]*$|\bnon[-\s]$",
            prefix,
            re.I,
        ):
            continue
        suffix = (text or "")[match.end() : match.end() + 45]
        if term.casefold() in {"metal", "metallic", "金属", "金属度"} and re.match(
            r"(?:lic|度)?\s*(?:(?:is|to|为|设为|设置为)\s*|[:：=]\s*)?"
            r"0(?:\.0+)?(?![\d.])",
            suffix,
            re.I,
        ):
            continue
        if re.match(
            r"\s*(?:(?:is|are|was|were)\s+(?:not\s+(?:needed|required|used|included)|"
            r"absent|disabled)|(?:不是|不需要|无需|不存在|未使用|被禁用))",
            suffix,
            re.I,
        ):
            continue
        return True
    return False


def _positive_hits(text: str, terms: list[str]) -> list[str]:
    return sorted({term for term in terms if _positive_term_matches(text, term)})


def _positive_motion_hits(text: str, terms: list[str]) -> list[str]:
    """Match complete motion words, including common instructional inflections."""

    aliases = {
        "keyframe": ("keyframe", "keyframes", "key frame", "key frames"),
        "animation": ("animation", "animations", "animate", "animated", "animating"),
        "simulation": (
            "simulation",
            "simulations",
            "simulate",
            "simulated",
            "simulating",
        ),
        "particle": ("particle", "particles"),
        "rig": ("rig", "rigs", "rigged", "rigging"),
    }
    return sorted(
        {
            term
            for term in terms
            if any(
                _positive_term_matches(text, alias)
                for alias in aliases.get(term, (term,))
            )
        }
    )


def is_generic_multi_topic_course_title(title: str) -> bool:
    """Identify one course-wide title reused across page-specific lessons."""

    lowered = str(title or "").lower()
    topic_count = sum(term in lowered for term in GENERIC_MULTI_TOPIC_COURSE_TERMS)
    return topic_count >= 4 and bool(
        re.search(r"(?:^|\s)p\d+\b|ch\d{2}[-_.]\d{2}", lowered, re.I)
    )


HUMAN_TERMS = [
    "人物",
    "角色",
    "人体",
    "人像",
    "肖像",
    "脸",
    "面部",
    "皮肤",
    "眼睛",
    "头发",
    "衣服",
    "裤子",
    "鞋",
    "手",
    "胳膊",
    "腿",
    "身体",
    "姿态",
    "绑定",
    "human",
    "character",
    "portrait",
    "face",
    "skin",
    "eye",
    "hair",
    "shirt",
    "pants",
    "cloth",
    "shoe",
    "hand",
    "arm",
    "leg",
    "body",
    "pose",
    "rig",
]

ANIMAL_CHARACTER_TERMS = [
    "猫",
    "小猫",
    "狗",
    "小狗",
    "鸡",
    "小鸡",
    "鸟",
    "动物",
    "宠物",
    "cat",
    "dog",
    "chicken",
    "bird",
    "animal",
    "pet",
]

# These are everyday-object compounds, not animal-subject evidence.  Bare
# substring matching otherwise routes an egg tutorial ("鸡蛋") as a chicken.
ANIMAL_CHARACTER_EXCLUDED_COMPOUNDS = (
    "鸡蛋",
    "蛋鸡",
)

HUMAN_STRONG_TERMS = [
    "人物",
    "人体",
    "人像",
    "肖像",
    "人类",
    "女孩",
    "男孩",
    "少女",
    "角色设计",
    "human",
    "person",
    "portrait",
    "girl",
    "boy",
]

GENERAL_ASSET_TITLE_TERMS = [
    "苹果",
    "圣诞树",
    "树",
    "植物",
    "基础模型",
    "模型",
    "材质",
    "节点",
    "着色器",
    "shader",
    "material",
    "node",
    "texture",
    "procedural",
    "christmas tree",
    "tree",
    "plant",
    "model",
]

GAME_CHARACTER_TITLE_TERMS = [
    "游戏动画",
    "骨骼动画",
    "游戏角色",
    "角色动画",
    "角色绑定",
    "动画制作流",
    "game animation",
    "character animation",
    "rigging",
    "rig",
]

SCENE_ENVIRONMENT_TITLE_TERMS = [
    "小居",
    "房间",
    "室内",
    "家具",
    "咖啡馆",
    "咖啡店",
    "店铺",
    "厨房",
    "客厅",
    "卧室",
    "家装",
    "建筑",
    "场景",
    "room",
    "interior",
    "cafe",
    "coffee shop",
    "house",
    "home",
    "kitchen",
    "living room",
    "bedroom",
    "architecture",
    "scene",
]


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None


def image_is_blank_or_menu_like(path: Path) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not path.exists():
        return False, ["missing"]
    try:
        img = Image.open(path).convert("RGB")
        stat = ImageStat.Stat(img.resize((128, 72)))
        mean = sum(stat.mean) / 3.0
        std = sum(stat.stddev) / 3.0
        if std < 8:
            reasons.append("low_visual_variance")
        if mean < 8 or mean > 247:
            reasons.append("nearly_blank_extreme_brightness")
        # OCR can legitimately miss tiny Blender labels. Detect the stronger
        # application-layout signature instead: multiple full-width panel
        # separators plus a tall right-side properties/outliner boundary.
        # Ordinary renders may contain one horizon, but should not contain
        # both repeated horizontal chrome and a persistent right UI column.
        probe = img.resize((160, 90), RESAMPLE_LANCZOS)
        pixels = probe.load()
        width, height = probe.size

        def line_groups(indices: list[int]) -> list[tuple[int, int]]:
            groups: list[tuple[int, int]] = []
            for index in indices:
                if groups and index <= groups[-1][1] + 1:
                    groups[-1] = (groups[-1][0], index)
                else:
                    groups.append((index, index))
            return groups

        horizontal = []
        for y in range(height - 1):
            changed = sum(
                max(
                    abs(pixels[x, y][channel] - pixels[x, y + 1][channel])
                    for channel in range(3)
                )
                > 10
                for x in range(width)
            )
            if changed / width > 0.5:
                horizontal.append(y)
        vertical = []
        for x in range(width - 1):
            changed = sum(
                max(
                    abs(pixels[x, y][channel] - pixels[x + 1, y][channel])
                    for channel in range(3)
                )
                > 10
                for y in range(height)
            )
            if changed / height > 0.5:
                vertical.append(x)
        horizontal_groups = line_groups(horizontal)
        vertical_groups = line_groups(vertical)
        has_central_separator = any(
            int(height * 0.12) <= start <= int(height * 0.88)
            for start, _end in horizontal_groups
        )
        has_right_panel_boundary = any(
            start >= int(width * 0.75) for start, _end in vertical_groups
        )
        if (
            len(horizontal_groups) >= 2
            and has_central_separator
            and has_right_panel_boundary
        ):
            reasons.append("reference_ui_panel_layout")
    except Exception as exc:
        return True, [f"image_read_error:{exc!r}"]
    return bool(reasons), reasons


def ocr_reference_text(path: Path) -> str:
    exe = shutil.which("tesseract")
    if not exe or not path.exists():
        return ""
    try:
        img = Image.open(path).convert("RGB")
        crops = {
            "whole": img,
            "top": img.crop((0, 0, img.width, int(img.height * 0.24))),
            "center": img.crop(
                (
                    int(img.width * 0.12),
                    int(img.height * 0.10),
                    int(img.width * 0.88),
                    int(img.height * 0.88),
                )
            ),
        }
        texts = []
        for name, crop in crops.items():
            tmp = (
                Path(tempfile.gettempdir()) / f"pipeline_ref_ocr_{path.stem}_{name}.png"
            )
            proc_img = crop.convert("L")
            proc_img = proc_img.resize(
                (proc_img.width * 2, proc_img.height * 2), RESAMPLE_LANCZOS
            )
            proc_img.save(tmp)
            proc = subprocess.run(
                [exe, str(tmp), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=18,
            )
            tmp.unlink(missing_ok=True)
            texts.append(" ".join(proc.stdout.split()))
        return " ".join(t for t in texts if t).strip()
    except Exception:
        return ""


def validate_reference(video_dir: Path) -> dict[str, Any]:
    ref = video_dir / "final_reference.png"
    target = video_dir / "target_reference.png"
    candidate = target if target.exists() else ref
    invalid, reasons = image_is_blank_or_menu_like(candidate)
    reference_ocr = ocr_reference_text(candidate)

    ocr_path = video_dir / "rich_evidence/ocr_samples.jsonl"
    # We cannot assume OCR aligns with the reference frame, so OCR is only a weak
    # signal. The prompt-side rule below is stricter: reference never overrides
    # tutorial/steps even if this status is valid.
    ui_keyword_hits = 0
    ui_terms = [
        "菜单",
        "推荐",
        "资源",
        "资源库",
        "网盘",
        "下载",
        "分享",
        "收藏",
        "投币",
        "评论",
        "弹幕",
        "Bilibili",
        "bilibili",
        "bibl",
        "Render Result",
        "ViewLayer",
        "Combined",
        "View",
        "Image",
    ]
    ref_ui_hits = sum(1 for term in ui_terms if term.lower() in reference_ocr.lower())
    ocr_lower = reference_ocr.lower()
    hard_ref_ui_terms = [
        "blender",
        "course",
        "tutorial",
        "masterclass",
        "complete",
        "ultimate",
        "become a 3d artist",
        "3d artist",
        "promo",
        "poster",
        "title",
        "episode",
        "合集",
        "全",
        "完结",
        "课程",
        "教程",
        "大师课",
        "案例",
        "封面",
        "片头",
        "perspective",
        "object mode",
        "edit mode",
        "layout",
        "outliner",
        "properties",
        "timeline",
        "collection",
        "scene collection",
        "shader editor",
        "geometry nodes",
        "modifier",
        "transform",
        "select box",
        "camera view",
        "view layer",
        "viewlayer",
        "render result",
        "播放",
        "暂停",
        "进度条",
        "关注",
        "已关注",
        "三连",
    ]
    hard_ref_ui_hits = sum(1 for term in hard_ref_ui_terms if term in ocr_lower)
    blender_topbar_fuzzy = any(
        term in ocr_lower
        for term in [
            "vemayer",
            "comes",
            "renser",
            "renae",
            "viewlayer",
            "combined",
            "render result",
        ]
    )
    noisy_blender_topbar = ("bibl" in ocr_lower or "bili" in ocr_lower) and any(
        term in ocr_lower
        for term in [
            "vew",
            "mage",
            "view",
            "image",
            "render",
            "renser",
            "vemayer",
            "comes",
        ]
    )
    promo_title_signals = [
        "ultimate weapon",
        "masterclass",
        "complete",
        "become a 3d artist",
        "课程",
        "教程",
        "大师课",
        "完结",
        "合集",
    ]
    promo_title_hits = sum(1 for term in promo_title_signals if term in ocr_lower)
    if (
        hard_ref_ui_hits >= 1
        or ref_ui_hits >= 2
        or blender_topbar_fuzzy
        or noisy_blender_topbar
        or promo_title_hits >= 1
    ):
        reasons.append("reference_ocr_ui_or_platform_signal")
        invalid = True
    if promo_title_hits >= 1:
        reasons.append("reference_looks_like_title_or_promo_card")
    if ocr_path.exists():
        sample = read_text(ocr_path)[:8000]
        ui_keyword_hits = sum(1 for term in ui_terms if term in sample)

    if ui_keyword_hits >= 5:
        reasons.append("high_ui_or_platform_ocr_signal")
        invalid = True

    status = {
        "reference_path": str(candidate) if candidate.exists() else "",
        "valid": bool(candidate.exists() and not invalid),
        "use_in_prompt": bool(candidate.exists() and not invalid),
        "priority": "auxiliary_only",
        "conflict_policy": "tutorial_and_steps_win",
        "reasons": reasons,
        "ui_keyword_hits": ui_keyword_hits,
        "reference_ocr_ui_hits": ref_ui_hits,
        "reference_ocr_hard_ui_hits": hard_ref_ui_hits,
        "reference_ocr_excerpt": reference_ocr[:600],
    }
    (video_dir / "final_reference_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return status


def scan_full_transcript_motion(
    video_dir: Path,
) -> dict[str, Any]:
    """Scan every local transcript segment without sending it to the model."""

    try:
        from prepare_rich_tutorial_evidence import find_segments, load_segments
    except ImportError:
        return {
            "available": False,
            "dynamic": False,
            "camera_only": False,
            "signals": [],
            "segment_count": 0,
            "source": "",
        }
    source_info = load_json(video_dir / "source.info.json")
    bvid = str(source_info.get("bvid") or source_info.get("id") or "").strip()
    path = find_segments(video_dir, bvid) if bvid else None
    rows = load_segments(path) if path is not None else []
    signals: list[dict[str, Any]] = []
    non_camera_terms = re.compile(
        r"geometry[\s_-]*nodes?|几何节点|noise|噪声|material|shader|"
        r"材质|着色器|object|物体|shape[\s_-]*key|形态键|骨骼|rig|"
        r"cloth|布料|fluid|流体|particle|粒子|emission|发光",
        re.I,
    )
    for index, row in enumerate(rows):
        text = str(
            row.get("text") or row.get("transcript") or row.get("content") or ""
        ).strip()
        if not text:
            continue
        for signal, pattern in FULL_TRANSCRIPT_DYNAMIC_PATTERNS:
            match = pattern.search(text)
            if match is None or not _positive_term_matches(text, match.group(0)):
                continue
            camera_context = bool(
                re.search(r"camera|camera view|相机|摄像机|镜头", text, re.I)
            )
            signals.append(
                {
                    "segment_index": index,
                    "start_sec": row.get("start", row.get("start_sec", "")),
                    "signal": signal,
                    "camera_context": camera_context,
                    "non_camera_context": bool(non_camera_terms.search(text)),
                    "excerpt": text[:240],
                }
            )
            break
        if len(signals) >= 32:
            break
    camera_only = bool(signals) and all(
        item["camera_context"] and not item["non_camera_context"] for item in signals
    )
    return {
        "available": bool(path is not None and rows),
        "dynamic": bool(signals and not camera_only),
        "camera_only": camera_only,
        "signals": signals,
        "segment_count": len(rows),
        "source": str(path or ""),
    }


def build_motion_plan(
    video_dir: Path, title: str, tutorial: str, steps: Any
) -> dict[str, Any]:
    steps_text = _step_instruction_text(steps)[:50000]
    title_steps_haystack = "\n".join([title, steps_text]).lower()
    haystack = "\n".join(
        [title, _instructional_text(tutorial)[:50000], steps_text]
    ).lower()
    hits = _positive_motion_hits(haystack, DYNAMIC_TERMS)
    strong_hits = _positive_motion_hits(title_steps_haystack, STRONG_DYNAMIC_TERMS)
    title_l = title.lower()
    inflation_motion_context = bool(
        re.search(
            r"(?:布料|软体|模拟|动力|动画|关键帧|解算|碰撞).{0,16}膨胀|"
            r"膨胀.{0,16}(?:布料|软体|模拟|动力|动画|关键帧|解算|碰撞)",
            title_steps_haystack,
            re.I,
        )
    )
    if "膨胀" in strong_hits and not inflation_motion_context:
        # “膨胀/腐蚀” is also a static shader/mask operation.  It is motion
        # evidence only when the title/verified steps bind it to simulation or
        # an explicit time-varying operation.
        strong_hits = [term for term in strong_hits if term != "膨胀"]
    cloth_is_only_material = any(
        term in strong_hits for term in ["布料", "cloth"]
    ) and not (
        re.search(r"布料.{0,8}(模拟|动力|解算|膨胀|动画|碰撞)", title_steps_haystack)
        or re.search(r"(模拟|动力|解算|膨胀|动画|碰撞).{0,8}布料", title_steps_haystack)
        or re.search(
            r"cloth.{0,16}(sim|simulation|dynamic|physics|inflate|collision|animate)",
            title_steps_haystack,
        )
        or re.search(
            r"(sim|simulation|dynamic|physics|inflate|collision|animate).{0,16}cloth",
            title_steps_haystack,
        )
        or (
            "布料" in title_l
            and any(
                term in title_l for term in ["模拟", "动力", "膨胀", "动画", "碰撞"]
            )
        )
        or (
            "cloth" in title_l
            and any(
                term in title_l
                for term in ["sim", "dynamic", "inflate", "animation", "collision"]
            )
        )
    )
    if cloth_is_only_material:
        strong_hits = [term for term in strong_hits if term not in {"布料", "cloth"}]
    weak_strong_hits = _positive_motion_hits(haystack, STRONG_DYNAMIC_TERMS)
    if "膨胀" in weak_strong_hits and not inflation_motion_context:
        weak_strong_hits = [term for term in weak_strong_hits if term != "膨胀"]
    # OCR and frame summaries can contain noisy words such as "smoke" or "fluid".
    # A single noisy term must not turn a static asset replay into a dynamic task.
    transcript_scan = scan_full_transcript_motion(video_dir)
    dynamic = (
        bool(strong_hits)
        or len(hits) >= 3
        or len(weak_strong_hits) >= 2
        or bool(transcript_scan["dynamic"])
    )
    camera_motion_context = bool(
        re.search(
            r"(?:camera|相机|摄像机|镜头).{0,28}"
            r"(?:animation|animate|key[\s_-]*frame|动画|关键帧)|"
            r"(?:animation|animate|key[\s_-]*frame|动画|关键帧).{0,28}"
            r"(?:camera|相机|摄像机|镜头)",
            title_steps_haystack,
            re.I,
        )
    )
    non_camera_motion_context = bool(
        re.search(
            r"(?:object|geometry|material|shader|noise|scale|rotation|"
            r"location|shape[\s_-]*key|rig|bone|cloth|fluid|particle|"
            r"物体|几何|材质|噪声|缩放|旋转|位置|形态键|骨骼|布料|"
            r"流体|粒子).{0,28}"
            r"(?:animation|animate|key[\s_-]*frame|动画|关键帧)|"
            r"(?:animation|animate|key[\s_-]*frame|动画|关键帧).{0,28}"
            r"(?:object|geometry|material|shader|noise|scale|rotation|"
            r"location|shape[\s_-]*key|rig|bone|cloth|fluid|particle|"
            r"物体|几何|材质|噪声|缩放|旋转|位置|形态键|骨骼|布料|"
            r"流体|粒子)",
            title_steps_haystack,
            re.I,
        )
    )
    if (
        camera_motion_context
        and not non_camera_motion_context
        and not transcript_scan["dynamic"]
    ):
        dynamic = False
    scene_terms = [
        "房间",
        "室内",
        "小居",
        "咖啡馆",
        "咖啡店",
        "店铺",
        "厨房",
        "客厅",
        "餐厅",
        "建筑",
        "room",
        "interior",
        "cafe",
        "coffee shop",
        "kitchen",
        "living room",
    ]
    character_terms = [
        "角色",
        "人物",
        "小猫",
        "猫",
        "狗",
        "动物",
        "骨骼",
        "绑定",
        "character",
        "cat",
        "dog",
        "animal",
        "rig",
    ]
    detail_terms = [
        "玻璃",
        "透明",
        "试管",
        "花",
        "器皿",
        "实验",
        "金属",
        "材质",
        "节点",
        "着色器",
        "程序化",
        "shader",
        "material",
        "texture",
        "procedural",
    ]
    flat_surface_terms = ["拼豆", "马赛克", "pixel art"]
    stylized_terms = ["卡通", "lowpoly", "low poly"]
    if any(term in title_l for term in flat_surface_terms):
        presentation_profile = "flat_surface_showcase"
    elif any(term in title_l for term in stylized_terms):
        presentation_profile = "stylized_showcase"
    elif any(term in title_l for term in detail_terms):
        presentation_profile = "detail_showcase"
    elif _positive_hits(title_steps_haystack, character_terms):
        presentation_profile = "character_loop" if dynamic else "character_showcase"
    elif _positive_hits(title_steps_haystack, scene_terms):
        presentation_profile = "cinematic_scene"
    elif any(term in haystack for term in detail_terms):
        presentation_profile = "detail_showcase"
    else:
        presentation_profile = "studio_turntable"
    plan = {
        "motion_type": "dynamic" if dynamic else "static",
        "final_effect_requirement": (
            "Recreate the demonstrated motion/simulation from the tutorial; a simple turntable is not an acceptable final_effect."
            if dynamic
            else "Use a polished 4-6 second asset showcase. A slow turntable/push-in is acceptable when no explicit demonstrated motion is extracted."
        ),
        "turntable_allowed_as_final_effect": not dynamic,
        "presentation_profile": presentation_profile,
        "presentation_policy": (
            "Final video is a presentation layer only: it may add camera movement, studio floor/background, and lighting, "
            "but it must not change tutorial-verified asset geometry, material facts, or animation behavior."
        ),
        "evidence_terms": hits,
        "strong_evidence_terms": strong_hits,
        "target_resolution": "720p",
        "target_fps": 90,
        "full_transcript_scan": transcript_scan,
    }
    (video_dir / "motion_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return plan


def _instructional_text(text: str) -> str:
    """Keep tutorial meaning, not attribution, URL payloads or author names."""

    lines = []
    attribution = re.compile(
        r"^(?:source(?:\s+(?:video|url|link|frame))?|video\s+(?:source|url|link)|"
        r"author|uploader|up\s*主|credits?|来源|视频来源|原视频(?:链接)?|"
        r"视频(?:原帧|链接)|作者|作者署名)\s*(?:[:：|｜]|$)",
        re.I,
    )
    for line in (text or "").splitlines():
        plain = re.sub(r"^[\s#>*_`|\-]+", "", line)
        plain = re.sub(r"[*_`]", "", plain)
        if attribution.match(plain):
            continue
        # Link labels can describe an operation; their destinations cannot.
        line = re.sub(r"!?\[([^\]\n]*)\]\([^\n]*?\)", r"\1", line)
        line = re.sub(r"https?://\S+|data:image/\S+", "", line)
        lines.append(line)
    return "\n".join(lines)


def _step_instruction_text(steps: Any) -> str:
    """Project semantic step fields without schema, provenance or QA metadata."""

    semantic_fields = {
        "action",
        "instruction",
        "instructions",
        "operation",
        "operations",
        "description",
        "object",
        "objects",
        "target",
        "parameters",
        "parameter",
        "input",
        "inputs",
        "output",
        "outputs",
        "expected_result",
        "result",
        "node",
        "nodes",
        "connection",
        "connections",
        "material",
        "materials",
        "geometry",
        "modifier",
        "modifiers",
        "title",
        "name",
        "visual_result",
        "material_color",
        "spatial_relation",
        "surface_detail",
        "implementation_notes",
        "relation_type",
        "motion",
        "animation",
    }

    def semantic_value(value: Any) -> str:
        if isinstance(value, dict):
            return "\n".join(
                f"{key}: {semantic_value(item)}"
                for key, item in value.items()
                if key not in {"url", "path", "sha256", "source", "evidence_id"}
            )
        if isinstance(value, list):
            return "\n".join(semantic_value(item) for item in value)
        return _instructional_text(str(value)) if value is not None else ""

    def project(value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(project(item) for item in value)
        if isinstance(value, dict):
            return "\n".join(
                project(item) if key == "steps" else semantic_value(item)
                for key, item in value.items()
                if key == "steps" or key in semantic_fields
            )
        return semantic_value(value)

    return project(steps or {})


def _scene_subject_hits(title: str) -> list[str]:
    # Starting from an empty/default scene says nothing about the final asset
    # family. Keep actual room/interior/scene subjects elsewhere in the title.
    subject_title = re.sub(
        r"\b(?:empty|blank|default|startup)\s+(?:blender\s+)?scene\b|"
        r"(?:空白?|默认|初始|启动)(?:的)?(?:Blender\s*)?场景",
        "",
        title,
        flags=re.I,
    )
    return _positive_hits(subject_title, SCENE_ENVIRONMENT_TITLE_TERMS)


def _procedural_material_hits(text: str) -> list[str]:
    """Procedural geometry alone does not require a textured surface."""

    patterns = (
        r"\bprocedural(?:ly)?[\s_-]+(?:texture|material|shader|shading)s?\b",
        r"程序化(?:的)?(?:纹理|材质|着色器?|贴图)",
        r"(?:使用|用|通过)?程序化(?:方式|方法)?(?:生成|制作|创建|构建)(?:纹理|材质|着色器|贴图)",
    )
    return (
        ["procedural_material"]
        if any(
            _positive_term_matches(text, match.group(0))
            for pattern in patterns
            for match in re.finditer(pattern, text, re.I)
        )
        else []
    )


def build_material_spec(
    video_dir: Path, title: str, tutorial: str, steps: Any
) -> dict[str, Any]:
    step_text = _step_instruction_text(steps)
    text = "\n".join([title, _instructional_text(tutorial), step_text])
    title_l = title.lower()
    # 金属/金属度 describes a shader property, not the gold colour. Negated
    # transparency likewise must not turn an opaque surface transparent.
    color_hits = _positive_hits(re.sub(r"金属度?", "", text), COLOR_TERMS)
    material_hits = _positive_hits(text, MATERIAL_TERMS)
    texture_detail_terms = [
        "噪波",
        "噪声",
        "凹凸",
        "noise",
        "bump",
        "wave",
        "voronoi",
        "color ramp",
        "colorramp",
        "grain",
        "speckles",
        "color variation",
        "纹理变化",
        "颗粒",
        "斑点",
        "木纹",
    ]
    image_texture_hits = _positive_hits(
        text, ["image texture", "图像纹理", "图片纹理", "图像贴图", "图片贴图"]
    )
    procedural_texture_hits = _positive_hits(
        text,
        ["noise", "噪声", "噪波", "voronoi", "wave texture"],
    ) + _procedural_material_hits(text)
    texture_detail_hits = _positive_hits(
        text, texture_detail_terms
    ) + _procedural_material_hits(text)
    # A uniform BSDF Roughness value is material setup, not surface texture.
    # Spatial roughness detail still counts when explicitly described.
    roughness_patterns = (
        r"\broughness[\s_-]+(?:map|texture|variation|pattern|noise)\b",
        r"\b(?:varying|variable|nonuniform|non-uniform|textured)\s+roughness\b",
        r"粗糙度(?:贴图|纹理|变化|分布|噪声)|(?:变化|随机|不均匀)的?粗糙度",
    )
    for pattern in roughness_patterns:
        for match in re.finditer(pattern, text, re.I):
            if _positive_term_matches(text, match.group(0)):
                texture_detail_hits.append("roughness_variation")
                break
    texture_detail_hits = sorted(set(texture_detail_hits))
    title_texture_hits = _positive_hits(
        title, texture_detail_terms
    ) + _procedural_material_hits(title)
    human_hits = _positive_hits(text, HUMAN_TERMS)
    animal_text = text
    animal_title = title
    for compound in ANIMAL_CHARACTER_EXCLUDED_COMPOUNDS:
        animal_text = re.sub(re.escape(compound), "", animal_text, flags=re.I)
        animal_title = re.sub(re.escape(compound), "", animal_title, flags=re.I)
    animal_hits = _positive_hits(animal_text, ANIMAL_CHARACTER_TERMS)
    title_animal_hits = _positive_hits(animal_title, ANIMAL_CHARACTER_TERMS)
    title_human_strong_hits = _positive_hits(title, HUMAN_STRONG_TERMS)
    title_general_hits = _positive_hits(title_l, GENERAL_ASSET_TITLE_TERMS)
    title_game_hits = _positive_hits(title_l, GAME_CHARACTER_TITLE_TERMS)
    title_scene_hits = _scene_subject_hits(title)
    text_confirms_human = bool(
        _positive_hits(
            step_text, ["人物", "角色", "人形", "character", "human", "face", "skin"]
        )
    )
    text_confirms_animal = bool(
        _positive_hits(
            step_text, ["小猫", "猫", "狗", "动物", "animal", "cat", "dog", "pet"]
        )
    )
    if title_scene_hits:
        subject_family = "scene_environment"
    elif title_game_hits:
        subject_family = "stylized_game_character"
    elif title_animal_hits:
        subject_family = "animal_cartoon_character"
    elif title_human_strong_hits and (
        not is_generic_multi_topic_course_title(title) or text_confirms_human
    ):
        subject_family = "human_character"
    elif title_general_hits:
        subject_family = "general_asset"
    elif len(human_hits) >= 6 and not animal_hits and text_confirms_human:
        subject_family = "human_character"
    elif animal_hits and text_confirms_animal:
        subject_family = "animal_cartoon_character"
    else:
        subject_family = "general_asset"
    spec = {
        "subject_family": subject_family,
        "human_terms_detected": human_hits,
        "animal_terms_detected": animal_hits,
        "title_human_strong_terms_detected": title_human_strong_hits,
        "title_animal_terms_detected": title_animal_hits,
        "title_game_character_terms_detected": title_game_hits,
        "title_general_asset_terms_detected": title_general_hits,
        "title_scene_environment_terms_detected": title_scene_hits,
        "colors_detected": color_hits,
        "material_terms_detected": material_hits,
        "texture_detail_terms_detected": texture_detail_hits,
        "title_texture_terms_detected": title_texture_hits,
        "texture_detail_required": bool(title_texture_hits or texture_detail_hits),
        "image_texture_required": bool(image_texture_hits),
        "procedural_texture_required": bool(procedural_texture_hits),
        "hard_constraints": [
            "When a geometry tutorial shows no material or color operations, allow neutral, untextured gray/white presentation. Preserve explicitly specified or clearly demonstrated colors and materials when present; do not invent colors, patterns, or shader detail merely to replace an unspecified default surface.",
            "Every visible major object must receive a named material with base color and roughness.",
            "When exact texture files are unavailable, approximate the visual material procedurally instead of dropping the material.",
            "Preserve metallic/glass/transparent/emissive/rough/fabric/hair qualities when detected in tutorial text or evidence images.",
        ],
        "texture_policy": "Use available procedural substitutes when external texture files are not present; never ignore described texture/material intent.",
    }
    if spec["texture_detail_required"]:
        spec["hard_constraints"].extend(
            [
                "The final render must visibly show the procedural/material texture detail described by the tutorial, such as grain, speckles, waves, color variation, bump, roughness variation, or shader-node pattern.",
                "A uniform surface is a failure only when the tutorial actually demonstrates spatial variation; a uniform image texture or constant BSDF parameter does not imply variation.",
                "When the tutorial is mainly about material nodes, the material appearance is more important than inventing extra model geometry.",
            ]
        )
    if spec["image_texture_required"]:
        spec["hard_constraints"].append(
            "Use the supplied Image Texture and preserve its demonstrated node connections. "
            "Do not replace an available image with invented procedural noise; a uniform input image may produce a uniform surface."
        )
    if subject_family == "human_character":
        spec["hard_constraints"].extend(
            [
                "For human/character assets, skin must use a warm skin-toned material with roughness/subsurface-like softness; never use default gray/white plastic skin.",
                "Separate face, eyes, hair, clothing, pants, shoes, and accessories into distinct named materials.",
                "Use dark eye materials, plausible hair color/material, and fabric-like rough clothing unless the tutorial evidence says otherwise.",
                "Do not leave the face blank when the evidence shows visible facial features; approximate eyes, brows, nose, mouth, and hair silhouette procedurally.",
                "Avoid mannequin-like all-gray bodies for character tutorials. If exact textures are missing, create procedural color zones and simple facial details.",
            ]
        )
        spec["character_material_policy"] = (
            "Characters require explicit skin, hair, eye, clothing, and shoe materials. "
            "Default gray/white is a failure unless the source evidence explicitly depicts a white mannequin."
        )
    elif subject_family == "stylized_game_character":
        spec["hard_constraints"].extend(
            [
                "For game/rigged character tutorials, preserve the target character body plan, silhouette, face, limbs, armor/clothing/gear, and pose identity.",
                "Do not replace the target character with a different animal, creature, mascot, or simplified toy character just because tutorial text mentions generic character terms.",
                "If the final reference shows armor, straps, belts, horns/ears/tail, gloves, boots, weapons, or other gear, approximate those visible parts procedurally.",
                "Dynamic/game animation tutorials must include a visible pose or keyframed motion, not only a static neutral model.",
            ]
        )
        spec["character_material_policy"] = (
            "Stylized game characters require separate body, face/eye, clothing/armor/gear, and accessory materials. "
            "The generated subject must not change into another unrelated character category."
        )
    elif subject_family == "animal_cartoon_character":
        spec["hard_constraints"].extend(
            [
                "For animal/cartoon character assets, create distinct body, face, eyes, ears/wings/tail/limbs when present.",
                "Use plausible fur/feather/cartoon surface color and roughness; do not leave the character as default gray plastic.",
                "Preserve major props and surrounding scene elements shown in the finished target, such as sofa, room, wall, reflective floor, or lights when present.",
                "Do not require human clothing/shoes unless the animal character visibly wears them.",
            ]
        )
        spec["character_material_policy"] = (
            "Animal/cartoon characters require explicit body, eye, face-detail, limb/ear/tail or wing materials. "
            "Human-only clothing/shoe requirements do not apply unless visible in the target."
        )
    elif subject_family == "scene_environment":
        spec["hard_constraints"].extend(
            [
                "For room/interior/environment scenes, preserve the finished-scene composition: room opening/window/backdrop, floor-wall relationship, and main furniture/props.",
                "Do not convert a frontal finished room into a roofed closed box or tiny dollhouse cutaway unless the target evidence shows that exact composition.",
                "Large walls/ceilings/background props must not hide the main visible furniture or window/backdrop.",
            ]
        )
        spec["scene_environment_policy"] = (
            "Scene/environment tutorials require a coherent camera-facing composition with the main furniture, windows/backdrop, floor, walls, and support props visible."
        )
    (video_dir / "material_spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return spec


def run_version_ocr_scan(video_dir: Path) -> dict[str, Any]:
    if os.environ.get("BLENDER_PIPELINE_SKIP_VERSION_OCR") == "1":
        return {"status": "skipped", "reason": "BLENDER_PIPELINE_SKIP_VERSION_OCR=1"}
    script = SCRIPTS_DIR / "scan_blender_version_ocr.py"
    if not script.exists():
        return {"status": "skipped", "reason": "missing_scan_script"}
    output = video_dir / "rich_evidence/blender_version_ocr.json"
    try:
        subprocess.run(
            [sys.executable, str(script), "--video-dir", str(video_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=int(os.environ.get("BLENDER_PIPELINE_VERSION_OCR_TIMEOUT", "240")),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "output": str(output)}
    except Exception as exc:
        return {"status": "error", "error": repr(exc), "output": str(output)}
    data = load_json(output)
    return (
        data
        if isinstance(data, dict)
        else {"status": "missing_output", "output": str(output)}
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", required=True, type=Path)
    args = parser.parse_args()
    video_dir = args.video_dir
    info = load_json(video_dir / "source.info.json") or {}
    title = str(info.get("title") or video_dir.name)
    tutorial = read_text(video_dir / "tutorial.md") or read_text(
        video_dir / "tutorial_path_refs.md"
    )
    steps = (
        load_json(video_dir / "steps_verified.json")
        or load_json(video_dir / "steps_rich.json")
        or {}
    )
    ref = validate_reference(video_dir)
    motion = build_motion_plan(video_dir, title, tutorial, steps)
    material = build_material_spec(video_dir, title, tutorial, steps)
    version_ocr = run_version_ocr_scan(video_dir)
    version_plan = build_version_plan(
        video_dir, fallback_blender=os.environ.get("BLENDER_PIPELINE_BLENDER", "")
    )
    print(
        json.dumps(
            {
                "final_reference_status": ref,
                "motion_plan": motion,
                "material_spec": material,
                "blender_version_ocr": version_ocr,
                "blender_version_plan": version_plan,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
