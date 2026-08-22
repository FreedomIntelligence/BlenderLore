#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from project_paths import SECRET_ROOT
from video_replay_model_client import (
    call_chat_completions,
    load_stage_checkpoint,
    read_paid_api_secret,
    save_stage_checkpoint,
)
from video_replay_paid_api import (
    BudgetExceeded,
    CircuitOpen,
    DeliveryUnknown,
    DiskPreflightError,
)

RESAMPLE_LANCZOS = getattr(
    getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
)


DEFAULT_SECRET = SECRET_ROOT / "model_api_key"
DEFAULT_ENDPOINT = os.environ.get("BLENDER_PIPELINE_API_ENDPOINT", "").strip()
DEFAULT_MODEL = "gpt-5.6-sol"
TUTORIAL_PROMPT_VERSION = "rich-tutorial-window-v3"


def model_timeout() -> tuple[float, float]:
    """Use a long read bound so slow successful requests are not orphaned."""

    read_seconds = int(os.environ.get("VIDEO_REPLAY_MODEL_READ_TIMEOUT_SECONDS", "600"))
    if read_seconds < 60 or read_seconds > 1800:
        raise ValueError("VIDEO_REPLAY_MODEL_READ_TIMEOUT_SECONDS must be 60..1800")
    return 20.0, float(read_seconds)


def now() -> str:
    return time.strftime("%F %T")


def append_usage(
    video_dir: Path,
    stage: str,
    model: str,
    endpoint: str,
    response_json: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    record = {
        "time": now(),
        "stage": stage,
        "model": model,
        "endpoint": endpoint,
        "usage": usage or {},
        "usage_reported": bool(usage),
    }
    if extra:
        record.update(extra)
    with (video_dir / "api_usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def encode_image(path: Path, max_side: int = 1400) -> str:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), RESAMPLE_LANCZOS)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=86, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def message_text(data: dict[str, Any]) -> str:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    for field in ("content", "reasoning_content"):
        value = message.get(field)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if field == "content" or ("{" in candidate and '"steps"' in candidate):
                return candidate
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            joined = "\n".join(parts).strip()
            if joined and (
                field == "content" or ("{" in joined and '"steps"' in joined)
            ):
                return joined
    return ""


def call_model(
    endpoint: str,
    key: str,
    model: str,
    content_variants: list[list[dict[str, Any]]],
    video_dir: Path,
    stage: str,
    extra: dict[str, Any],
) -> str:
    window_index = int(extra.get("window_index") or 0)
    stage_key = f"tutorial/window-{window_index:03d}"
    primary_content = content_variants[0]
    primary_payload = {
        "model": model,
        "messages": [{"role": "user", "content": primary_content}],
        "temperature": 0.0,
        "max_tokens": 10000,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    primary_semantic_input = {
        "schema": TUTORIAL_PROMPT_VERSION,
        "stage": stage,
        "window_index": window_index,
        "payload": primary_payload,
    }
    checkpoint = load_stage_checkpoint(
        video_dir=video_dir,
        stage=f"{stage_key}/primary",
        prompt_version=TUTORIAL_PROMPT_VERSION,
        model=model,
        semantic_input=primary_semantic_input,
    )
    if checkpoint is not None:
        primary_text = checkpoint.decode("utf-8")
    else:
        response = call_chat_completions(
            video_dir=video_dir,
            stage=f"{stage_key}/primary",
            stage_key=stage_key,
            prompt_version=TUTORIAL_PROMPT_VERSION,
            endpoint=endpoint,
            api_key=key,
            model=model,
            payload=primary_payload,
            timeout=model_timeout(),
            semantic_input=primary_semantic_input,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:1000]}")
        data = response.json()
        append_usage(
            video_dir,
            stage,
            model,
            endpoint,
            data,
            {
                **extra,
                "logical_call_id": response.logical_call_id,
                "provider_request_id": response.provider_request_id,
                "replayed": response.replayed,
            },
        )
        primary_text = message_text(data)
        if primary_text:
            save_stage_checkpoint(
                video_dir=video_dir,
                stage=f"{stage_key}/primary",
                prompt_version=TUTORIAL_PROMPT_VERSION,
                model=model,
                semantic_input=primary_semantic_input,
                output=primary_text.encode("utf-8"),
            )
    try:
        extract_json(primary_text)
        return primary_text
    except (RuntimeError, json.JSONDecodeError):
        pass

    repair_payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "把下面已有响应仅修复为有效 JSON；不得补充、推测或改变任何事实。"
                    "只输出 JSON 对象。\n\n" + primary_text[:30000]
                ),
            }
        ],
        "temperature": 0.0,
        "max_tokens": 3000,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    repair_semantic_input = {
        "schema": TUTORIAL_PROMPT_VERSION,
        "stage": stage,
        "window_index": window_index,
        "previous_response": primary_text,
    }
    checkpoint = load_stage_checkpoint(
        video_dir=video_dir,
        stage=f"{stage_key}/format-repair",
        prompt_version=TUTORIAL_PROMPT_VERSION,
        model=model,
        semantic_input=repair_semantic_input,
    )
    if checkpoint is not None:
        repaired = checkpoint.decode("utf-8")
    else:
        response = call_chat_completions(
            video_dir=video_dir,
            stage=f"{stage_key}/format-repair",
            stage_key=stage_key,
            prompt_version=TUTORIAL_PROMPT_VERSION,
            endpoint=endpoint,
            api_key=key,
            model=model,
            payload=repair_payload,
            timeout=model_timeout(),
            semantic_input=repair_semantic_input,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:1000]}")
        data = response.json()
        append_usage(
            video_dir,
            f"{stage}_format_repair",
            model,
            endpoint,
            data,
            {
                **extra,
                "logical_call_id": response.logical_call_id,
                "provider_request_id": response.provider_request_id,
                "replayed": response.replayed,
            },
        )
        repaired = message_text(data)
        extract_json(repaired)
        save_stage_checkpoint(
            video_dir=video_dir,
            stage=f"{stage_key}/format-repair",
            prompt_version=TUTORIAL_PROMPT_VERSION,
            model=model,
            semantic_input=repair_semantic_input,
            output=repaired.encode("utf-8"),
        )
    extract_json(repaired)
    return repaired


def extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("model output has no JSON object")
    return json.loads(text[start : end + 1])


def normalize_chunk(
    data: dict[str, Any], window: dict[str, Any], window_index: int
) -> dict[str, Any]:
    step_defaults: dict[str, Any] = {
        "time_range": "unknown",
        "action": "unknown",
        "object": "unknown",
        "parameters": {},
        "evidence": "unknown",
        "visual_result": "unknown",
        "material_color": "none",
        "spatial_relation": "none",
        "surface_detail": "none",
        "implementation_notes": "none",
    }
    steps = []
    for item in data.get("steps", []):
        if not isinstance(item, dict):
            continue
        step = dict(step_defaults)
        step.update(item)
        steps.append(step)
    contracts = (
        data.get("visual_contracts")
        if isinstance(data.get("visual_contracts"), dict)
        else {}
    )
    contract_keys = (
        "visible_objects",
        "materials",
        "surface_details",
        "spatial_layout",
        "final_constraints",
        "do_not_omit",
    )
    return {
        "window_index": window_index,
        "start_sec": window["start_sec"],
        "end_sec": window["end_sec"],
        "steps": steps,
        "visual_contracts": {key: contracts.get(key, []) for key in contract_keys},
        "uncertain_items": data.get("uncertain_items", []),
    }


def recover_existing_chunk(
    out_json: Path,
    raw_path: Path,
    window: dict[str, Any],
    window_index: int,
) -> bool:
    """Repair an invalid projection from its durable raw response, with no POST."""

    if not out_json.is_file():
        return False
    try:
        current = json.loads(out_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = None
    if (
        isinstance(current, dict)
        and current.get("window_index") == window_index
        and isinstance(current.get("steps"), list)
        and isinstance(current.get("visual_contracts"), dict)
    ):
        return True
    if not raw_path.is_file():
        return False
    repaired = normalize_chunk(
        extract_json(raw_path.read_text(encoding="utf-8")),
        window,
        window_index,
    )
    temporary = out_json.with_name(out_json.name + ".tmp")
    temporary.write_text(
        json.dumps(repaired, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, out_json)
    return True


def quality_reaudit_focus(info: dict[str, Any]) -> str:
    """Return a bounded evidence-review focus, never an invented scene fact."""

    instruction = str(info.get("quality_reaudit_instruction") or "").strip()
    if not instruction:
        return ""
    return (
        "人工复检发现上一版成品可能存在以下问题："
        + instruction[:4096]
        + "。请在本窗口截图、ASR/OCR和时间戳中主动寻找支持或反驳证据；"
        "只能把证据确认的内容写成事实，不得把这段复检文字本身当成视频事实。"
        "若本窗口无相关证据，请在 uncertain_items 中明确记录。"
    )


def quality_reaudit_prompt_line(info: dict[str, Any]) -> str:
    focus = quality_reaudit_focus(info)
    return "\n返修证据复核焦点：" + focus if focus else ""


def build_prompt(
    info: dict[str, Any], window: dict[str, Any], window_index: int
) -> str:
    return f"""
你是 Blender 教学视频操作步骤提取员。请只根据本窗口证据提取操作，不要概括整部视频。

视频标题：{info.get("title", "")}
视频链接：{info.get("webpage_url", "")}{quality_reaudit_prompt_line(info)}
窗口编号：{window_index}
时间范围：{window["start_sec"]:.1f}s - {window["end_sec"]:.1f}s

输入证据：
1. 窗口截图拼图：里面每张小图文件名包含时间戳。
2. ASR/字幕：带有这一分钟的语音说明。
3. OCR：来自 Blender UI/字幕区域的文字样本，可能有错误，只作为辅助。

任务：
- 提取本窗口中真实发生的 Blender 操作步骤。
- 每一步必须包含：time_range、action、object、parameters、evidence、visual_result、material_color、spatial_relation、surface_detail、implementation_notes。
- 如果字幕说了一个操作，但截图没有显示具体数值，参数写 unknown，不要猜。
- 如果截图显示了 UI 参数或对象变化，要优先记录“修改后的稳定状态”。
- 不允许只把图片留给后续模型自己理解。必须把截图中的关键视觉信息转写成文字：对象形状、轮廓、颜色、材质、纹理、表面细节、空间关系、遮挡关系、相机角度、最终画面应保留/禁止遗漏的内容。
- 同一轮廓以不同颜色、材质、LOD、前后状态或并排方式出现时，优先视为同一设计的变体/对照版本。只有至少两个时间戳都显示持续物理连接，或视频明确执行了装配/父子操作，才能写成上下级、附着或组合部件。
- 每个窗口都要在 spatial_relation 或 implementation_notes 中写明主体实例数、变体关系及其时间戳证据；证据不足写 uncertain，禁止把遮挡或同屏误写成物理连接。
- 明确区分“完整主体/机身/角色/建筑”和“尾焰、烟雾、水滴、粒子、毛发等附属特效”；附属特效只能补充主体，不能替代或遮掉主体。把主体必备部件写入 visible_objects 与 do_not_omit。
- 材质/颜色/纹理信息即使不是 UI 参数，只要在截图里可见，也要写入 visual_result 或 material_color。
- 如果本窗口是节点、材质、贴图、UV、烘焙或 Substance/PBR 相关内容，必须写清楚最终材质外观，而不是只写“设置材质”。
- 不要输出寒暄、课程宣传、点赞订阅等无关内容。
- 不要把后面窗口才发生的操作提前写进来。

ASR/字幕：
{window.get("asr_text", "")[:3500]}

OCR 样本：
{json.dumps(window.get("ocr_samples", []), ensure_ascii=False)[:2200]}

只输出 JSON：
{{
  "window_index": {window_index},
  "start_sec": {window["start_sec"]},
  "end_sec": {window["end_sec"]},
  "steps": [
    {{
      "time_range": "00:00-00:00",
      "action": "...",
      "object": "...",
      "parameters": {{}},
      "evidence": "ASR/OCR/IMAGE timestamp ...",
      "visual_result": "这一步完成后画面中应看到的具体形状/轮廓/结构",
      "material_color": "这一步涉及或画面可见的颜色、材质、纹理、粗糙度/金属/透明/发光等外观",
      "spatial_relation": "对象之间前后左右上下、贴附、穿插、遮挡、比例关系",
      "surface_detail": "花纹、斑点、凹凸、划痕、节点纹理、贴图细节等；无则写 none",
      "implementation_notes": "给 Blender Python 复现时必须保留的执行约束；无则写 none"
    }}
  ],
  "visual_contracts": {{
    "visible_objects": ["本窗口截图明确可见、最终资产应保留的对象/部件"],
    "materials": ["可见或被设置的颜色/材质/纹理/节点外观"],
    "surface_details": ["表面细节、图案、凹凸、透明、发光、污渍、边缘等"],
    "spatial_layout": ["主要对象空间关系和相机角度"],
    "final_constraints": ["最终渲染/动画必须满足的视觉约束"],
    "do_not_omit": ["后续生成 Blender 资产时不能漏掉的部件或材质细节"]
  }},
  "uncertain_items": []
}}
""".strip()


def build_compact_prompt(
    info: dict[str, Any], window: dict[str, Any], window_index: int
) -> str:
    return f"""
从 Blender 教学视频的本窗口证据提取真实操作。不要猜参数；不确定写 unknown。截图中的最终稳定状态优先于口述中的修改前状态。不同配色/材质但轮廓相同的对象默认是变体或对照；没有跨两个时间戳的持续连接或明确装配操作，不得推断父子、上下堆叠或组合关系。spatial_relation/implementation_notes 必须写主体实例数、变体关系和证据。尾焰、烟雾、水滴、粒子、毛发等附属特效不能替代完整主体；主体必备部件必须进入 visible_objects/do_not_omit。
标题：{info.get("title", "")}{quality_reaudit_prompt_line(info)}
窗口：{window_index}，{window["start_sec"]:.1f}s-{window["end_sec"]:.1f}s
ASR：{window.get("asr_text", "")[:2400]}
OCR：{json.dumps(window.get("ocr_samples", []), ensure_ascii=False)[:1200]}

只返回 JSON 对象。steps 中每步必须有 time_range、action、object、parameters、evidence、visual_result、material_color、spatial_relation、surface_detail、implementation_notes。参数、节点连接、对象关系和修改后的值必须写清楚。图片可见但语音未提到的材质、纹理、形状也要写出。不要加入本窗口未发生的操作。
格式：
{{"window_index":{window_index},"start_sec":{window["start_sec"]},"end_sec":{window["end_sec"]},"steps":[],"visual_contracts":{{"visible_objects":[],"materials":[],"surface_details":[],"spatial_layout":[],"final_constraints":[],"do_not_omit":[]}},"uncertain_items":[]}}
""".strip()


def build_fact_prompt(
    info: dict[str, Any], window: dict[str, Any], window_index: int
) -> str:
    return f"""
读取这张 Blender 教学视频时间拼图，并结合 ASR/OCR，按时间顺序列出实际发生的操作。只抄录证据，不解释、不猜测、不概括。相同轮廓的不同配色默认记录为变体/对照；除非跨两个时间戳持续连接或有明确装配操作，不得写成父子或上下堆叠。记录主体实例数和变体关系，并区分完整主体与尾焰、烟雾、水滴、粒子、毛发等附属特效；特效不能替代主体。
标题：{info.get("title", "")}{quality_reaudit_prompt_line(info)}
范围：{window["start_sec"]:.1f}s-{window["end_sec"]:.1f}s
ASR：{window.get("asr_text", "")[:1800]}
OCR：{json.dumps(window.get("ocr_samples", []), ensure_ascii=False)[:800]}
只输出一行 JSON：{{"steps":[{{"time_range":"MM:SS-MM:SS","action":"具体操作","object":"对象或节点","parameters":{{"参数":"修改后值"}},"evidence":"时间戳及证据","visual_result":"修改后可见结果","material_color":"可见材质颜色纹理","spatial_relation":"对象关系","surface_detail":"表面细节","implementation_notes":"不得改变的执行约束"}}],"uncertain_items":[]}}
没有操作就输出空 steps。未知值写 unknown。
""".strip()


def build_minimal_prompt(info: dict[str, Any], window: dict[str, Any]) -> str:
    focus = quality_reaudit_focus(info)
    repair_line = "返修证据复核焦点：" + focus + "\n" if focus else ""
    return (
        "按时间顺序列出这段 Blender 教程实际执行的操作和明确参数，不猜测，最多 8 条。"
        '只输出简短 JSON：{"steps":[{"action":"...","parameters":{}}]}。\n'
        + repair_line
        + "ASR："
        + str(window.get("asr_text", ""))[:900]
        + "\nOCR："
        + json.dumps(window.get("ocr_samples", []), ensure_ascii=False)[:400]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", required=True, type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--secret", type=Path, default=DEFAULT_SECRET)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    video_dir = args.video_dir
    evidence = video_dir / "rich_evidence"
    windows = json.loads((evidence / "windows.json").read_text(encoding="utf-8"))
    info = json.loads((video_dir / "source.info.json").read_text(encoding="utf-8"))
    key = read_paid_api_secret(args.secret)
    out_dir = video_dir / "rich_tutorial_chunks"
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = windows[args.start : args.start + args.count]
    done = 0
    for idx, window in enumerate(selected, start=args.start):
        out_json = out_dir / f"window_{idx:03d}.json"
        raw_path = out_dir / f"window_{idx:03d}_raw.txt"
        if recover_existing_chunk(out_json, raw_path, window, idx):
            done += 1
            continue
        sheet = video_dir / window["sheet"]
        image_max_side = int(
            os.environ.get("RICH_TUTORIAL_IMAGE_MAX_SIDE", "640") or "640"
        )
        image_item = {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,"
                + encode_image(sheet, max_side=image_max_side)
            },
        }
        content_variants: list[list[dict[str, Any]]] = [
            [{"type": "text", "text": build_prompt(info, window, idx)}, image_item],
            [
                {"type": "text", "text": build_fact_prompt(info, window, idx)},
                image_item,
            ],
            [{"type": "text", "text": build_fact_prompt(info, window, idx)}],
            [{"type": "text", "text": build_minimal_prompt(info, window)}],
        ]
        raw = call_model(
            args.endpoint,
            key,
            args.model,
            content_variants,
            video_dir,
            "tutorial_chunk",
            {"window_index": idx, "image_count": 1},
        )
        raw_path.write_text(raw, encoding="utf-8")
        parsed = normalize_chunk(extract_json(raw), window, idx)
        out_json.write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        done += 1
    print(
        json.dumps(
            {"video_dir": str(video_dir), "chunks_done": done, "out_dir": str(out_dir)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BudgetExceeded as exc:
        print(
            json.dumps(
                {
                    "status": "needs_review_budget_exhausted",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(76)
    except (CircuitOpen, DeliveryUnknown, DiskPreflightError) as exc:
        print(
            json.dumps(
                {
                    "status": "paid_api_blocked",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(75)
