#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def fmt_params(params: Any) -> str:
    if not params:
        return ""
    if isinstance(params, dict):
        parts = [f"{k}={v}" for k, v in params.items()]
        return "；参数：" + "，".join(parts)
    return f"；参数：{params}"


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def add_detail_line(lines: list[str], label: str, value: Any) -> None:
    values = as_list(value)
    if values:
        lines.append(f"   - {label}：" + "；".join(values))


def visual_contract_score(
    steps: list[dict[str, Any]], contracts: list[dict[str, Any]]
) -> dict[str, Any]:
    fields = [
        "visual_result",
        "material_color",
        "spatial_relation",
        "surface_detail",
        "implementation_notes",
    ]
    hits = {field: 0 for field in fields}
    for step in steps:
        for field in fields:
            if (
                str(step.get(field) or "").strip()
                and str(step.get(field)).strip().lower() != "none"
            ):
                hits[field] += 1
    contract_items = 0
    for contract in contracts:
        for key in [
            "visible_objects",
            "materials",
            "surface_details",
            "spatial_layout",
            "final_constraints",
            "do_not_omit",
        ]:
            contract_items += len(as_list(contract.get(key)))
    return {
        "step_count": len(steps),
        "field_hits": hits,
        "contract_items": contract_items,
        "has_visual_text_contract": contract_items >= 4
        and (hits["visual_result"] + hits["material_color"] + hits["surface_detail"])
        >= max(1, min(4, len(steps))),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", required=True, type=Path)
    args = parser.parse_args()

    video_dir = args.video_dir
    info = json.loads((video_dir / "source.info.json").read_text(encoding="utf-8"))
    windows = json.loads(
        (video_dir / "rich_evidence/windows.json").read_text(encoding="utf-8")
    )
    ref_status_path = video_dir / "final_reference_status.json"
    if ref_status_path.exists():
        try:
            ref_status = json.loads(
                ref_status_path.read_text(encoding="utf-8", errors="ignore")
            )
        except json.JSONDecodeError:
            ref_status = {}
    else:
        ref_status = {}
    workload_kind = str(info.get("workload_kind") or info.get("source_kind") or "")
    rw2_series = workload_kind == "rw2_series"
    use_reference = (
        not rw2_series
        and ref_status.get("valid") is True
        and ref_status.get("use_in_prompt") is True
        and ref_status.get("priority") == "auxiliary_only"
        and ref_status.get("conflict_policy") == "tutorial_and_steps_win"
    )
    reference_note = (
        "validated auxiliary visual evidence; tutorial steps and verified parameters remain authoritative"
        if use_reference
        else (
            "excluded for RW2; tutorial and verified steps are the only reproduction authority"
            if rw2_series
            else "invalid or unavailable; do not use as a generation target"
        )
    )
    chunk_dir = video_dir / "rich_tutorial_chunks"
    chunks = []
    for path in sorted(chunk_dir.glob("window_*.json")):
        chunks.append(json.loads(path.read_text(encoding="utf-8")))

    lines = [
        f"# {info.get('title', video_dir.name)}",
        "",
        f"- 视频链接：{info.get('webpage_url', '')}",
        f"- 视频源：`{info.get('source_video_path', '')}`",
        f"- 成品视觉参考：`final_reference.png`（{reference_note}）",
        "",
        "## 证据覆盖",
        "",
        f"- 视频总时长：约 {windows[-1]['end_sec']:.1f} 秒",
        f"- 分段窗口：{len(windows)} 个，每段约 60 秒",
        "- 每段证据：ASR/字幕 + OCR 样本 + 5 秒级截图拼图",
        "",
        "## 分段图文教程",
        "",
    ]
    all_steps = []
    all_contracts = []
    for chunk in chunks:
        idx = int(chunk["window_index"])
        start = float(chunk["start_sec"])
        end = float(chunk["end_sec"])
        sheet = windows[idx]["sheet"] if idx < len(windows) else ""
        steps = chunk.get("steps", [])
        contract = chunk.get("visual_contracts") or {}
        if contract:
            all_contracts.append({"window_index": idx, **contract})
        if not steps:
            continue
        lines.extend(
            [
                f"### W{idx:03d}｜{start:.0f}s-{end:.0f}s",
                "",
                f"![W{idx:03d}]({sheet})",
                "",
            ]
        )
        for n, step in enumerate(steps, start=1):
            item = {
                "window_index": idx,
                "start_sec": start,
                "end_sec": end,
                **step,
            }
            all_steps.append(item)
            action = step.get("action", "")
            obj = step.get("object", "")
            evidence = step.get("evidence", "")
            lines.append(
                f"{n}. `{step.get('time_range', '')}` {action}：{obj}{fmt_params(step.get('parameters'))}"
            )
            if evidence:
                lines.append(f"   - 证据：{evidence}")
            add_detail_line(lines, "视觉结果", step.get("visual_result"))
            add_detail_line(lines, "材质/颜色/纹理", step.get("material_color"))
            add_detail_line(lines, "空间关系", step.get("spatial_relation"))
            add_detail_line(lines, "表面细节", step.get("surface_detail"))
            add_detail_line(lines, "代码复现约束", step.get("implementation_notes"))
        if contract:
            lines.extend(["", "#### 窗口视觉文字化约束", ""])
            add_detail_line(lines, "可见对象/部件", contract.get("visible_objects"))
            add_detail_line(lines, "材质/颜色/纹理", contract.get("materials"))
            add_detail_line(lines, "表面细节", contract.get("surface_details"))
            add_detail_line(lines, "空间/相机关系", contract.get("spatial_layout"))
            add_detail_line(lines, "最终视觉约束", contract.get("final_constraints"))
            add_detail_line(lines, "禁止遗漏", contract.get("do_not_omit"))
        uncertain = chunk.get("uncertain_items") or []
        if uncertain:
            lines.append("")
            lines.append("   - 需核验：" + "；".join(map(str, uncertain)))
        lines.append("")

    if all_contracts:
        lines.extend(["## 资产/材质/空间文字化约束总表", ""])
        for contract in all_contracts:
            lines.append(f"### W{int(contract.get('window_index', 0)):03d}")
            add_detail_line(lines, "可见对象/部件", contract.get("visible_objects"))
            add_detail_line(lines, "材质/颜色/纹理", contract.get("materials"))
            add_detail_line(lines, "表面细节", contract.get("surface_details"))
            add_detail_line(lines, "空间/相机关系", contract.get("spatial_layout"))
            add_detail_line(lines, "最终视觉约束", contract.get("final_constraints"))
            add_detail_line(lines, "禁止遗漏", contract.get("do_not_omit"))
            lines.append("")

    lines.extend(
        [
            "## 成品视觉参考",
            "",
            "![final_reference](final_reference.png)"
            if use_reference
            else "`final_reference.png` 未通过有效性校验，本教程以分段步骤和 verified steps 为准。",
            "",
            "`tutorial.md` 是唯一操作真值。证据帧必须绑定到具体步骤，只能辅助理解和验收；如果证据帧、成品图或视觉审核与教程步骤/参数/顺序冲突，以本教程文字为准。未标明的精确参数不得凭空编造。",
            "",
        ]
    )
    tutorial_path_refs = video_dir / "tutorial_path_refs.md"
    tutorial_path_refs.write_text("\n".join(lines), encoding="utf-8")
    (video_dir / "tutorial_rich.md").unlink(missing_ok=True)
    (video_dir / "steps_rich.json").write_text(
        json.dumps({"steps": all_steps}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    quality = visual_contract_score(all_steps, all_contracts)
    (video_dir / "tutorial_visual_contract.json").write_text(
        json.dumps(
            {"contracts": all_contracts, "quality": quality},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "tutorial": str(tutorial_path_refs),
                "steps": len(all_steps),
                "visual_contract": quality,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
