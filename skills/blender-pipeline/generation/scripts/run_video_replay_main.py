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
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_trajectory import AgentTrajectory
from project_paths import PROJECT_ROOT, SCRIPT_ROOT, SECRET_ROOT
from video_replay_delivery_contract import complete_six_view_delivery


PROJECT = PROJECT_ROOT
SCRIPTS = SCRIPT_ROOT
DEFAULT_SECRET = Path(
    os.environ.get("BLENDER_PIPELINE_API_KEY_FILE", str(SECRET_ROOT / "model_api_key"))
)
DEFAULT_ENDPOINT = os.environ.get(
    "BLENDER_PIPELINE_API_ENDPOINT",
    "",
)
DEFAULT_MODEL = os.environ.get("BLENDER_PIPELINE_MODEL", "gpt-5.6-sol")
MIN_VERIFIED_STEPS = int(
    os.environ.get("BLENDER_PIPELINE_MIN_VERIFIED_STEPS", "4") or "4"
)
MIN_ACTIONABLE_STEPS = int(
    os.environ.get("BLENDER_PIPELINE_MIN_ACTIONABLE_STEPS", "2") or "2"
)
REQUIRE_VISUAL_TEXT_CONTRACT = (
    os.environ.get("BLENDER_PIPELINE_REQUIRE_VISUAL_TEXT_CONTRACT", "1") != "0"
)
ACTIVE_TRAJECTORY: AgentTrajectory | None = None
ROUTE_OVERRIDE_SCHEMA = "video-replay-route-override.v1"


def log(message: str) -> None:
    print(f"[{time.strftime('%F %T')}] {message}", flush=True)


def run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    log("run: " + " ".join(cmd))
    call_id = ""
    if ACTIVE_TRAJECTORY is not None:
        call_id = ACTIVE_TRAJECTORY.record_tool_call(
            "subprocess",
            {"command": cmd, "cwd": os.getcwd(), "environment_overrides": env or {}},
            stage="pipeline_tool",
        )
    result_recorded = False
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")
            stderr = stderr_file.read().decode("utf-8", errors="replace")
            if stdout:
                print(stdout, end="" if stdout.endswith("\n") else "\n", flush=True)
            if stderr:
                print(
                    stderr,
                    end="" if stderr.endswith("\n") else "\n",
                    file=sys.stderr,
                    flush=True,
                )
            if ACTIVE_TRAJECTORY is not None:
                ACTIVE_TRAJECTORY.record_tool_result(
                    "subprocess",
                    call_id,
                    {
                        "returncode": completed.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                    },
                    stage="pipeline_tool",
                    error=(
                        subprocess.CalledProcessError(completed.returncode, cmd)
                        if completed.returncode
                        else None
                    ),
                )
                result_recorded = True
            completed.check_returncode()
        except Exception as exc:
            if ACTIVE_TRAJECTORY is not None and call_id and not result_recorded:
                ACTIVE_TRAJECTORY.record_tool_result(
                    "subprocess", call_id, {}, stage="pipeline_tool", error=exc
                )
            raise


def safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        for child in path.rglob("*"):
            try:
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
        shutil.rmtree(path, ignore_errors=True)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {}


def load_info(video_dir: Path) -> dict:
    path = video_dir / "source.info.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    return {"title": video_dir.name, "webpage_url": "", "bvid": ""}


def effective_motion_plan(video_dir: Path, out_dir: Path | None) -> dict:
    plan = load_json(video_dir / "motion_plan.json")
    if out_dir is None:
        return plan
    override = load_json(out_dir / "route_override.json")
    if (
        override.get("schema") == ROUTE_OVERRIDE_SCHEMA
        and override.get("effective_route") == "static"
        and plan.get("motion_type") == "dynamic"
        and override.get("reason") == "dynamic_without_verified_scene_animation"
    ):
        return {
            **plan,
            "requested_motion_type": "dynamic",
            "motion_type": "static",
            "route_override": override,
        }
    return plan


def blender_route_status(video_dir: Path) -> dict:
    route = load_json(video_dir / "highqal_route.json")
    if route.get("route") == "non_blender":
        return {
            "is_blender": False,
            "reason": route.get("reason") or "highqal_route.json=non_blender",
        }
    info = load_info(video_dir)
    title = str(info.get("title") or video_dir.name).lower()
    blender_terms = ["blender", "bpy", "cycles", "eevee", "几何节点", "着色器节点"]
    non_blender_terms = [
        "godot",
        "unreal",
        "ue5",
        "ue4",
        "unity",
        "c4d",
        "cinema 4d",
        "maya",
        "houdini",
    ]
    if any(term in title for term in non_blender_terms) and not any(
        term in title for term in blender_terms
    ):
        return {
            "is_blender": False,
            "reason": f"title indicates non-Blender workflow: {info.get('title') or video_dir.name}",
        }
    return {"is_blender": True, "reason": ""}


def count_steps(value) -> int:
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key == "steps" and isinstance(item, list):
                total += len(item)
            else:
                total += count_steps(item)
        return total
    if isinstance(value, list):
        return sum(count_steps(item) for item in value)
    return 0


def iter_step_dicts(value):
    if isinstance(value, dict):
        steps = value.get("steps")
        if isinstance(steps, list):
            for item in steps:
                if isinstance(item, dict):
                    yield item
        for key, item in value.items():
            if key != "steps":
                yield from iter_step_dicts(item)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if any(
                    k in item for k in ("action", "object", "parameters", "time_range")
                ):
                    yield item
                else:
                    yield from iter_step_dicts(item)


def actionable_step_count(value) -> int:
    action_terms = [
        "添加",
        "新建",
        "创建",
        "设置",
        "修改",
        "调整",
        "移动",
        "旋转",
        "缩放",
        "复制",
        "删除",
        "连接",
        "应用",
        "打关键帧",
        "关键帧",
        "烘焙",
        "绑定",
        "挤出",
        "倒角",
        "细分",
        "材质",
        "节点",
        "add",
        "create",
        "set",
        "modify",
        "move",
        "rotate",
        "scale",
        "keyframe",
        "bake",
        "bind",
        "extrude",
        "bevel",
        "subdivision",
        "material",
        "node",
    ]
    weak_terms = [
        "展示",
        "预览",
        "查看",
        "播放",
        "成品",
        "最终效果",
        "show",
        "preview",
        "final result",
    ]
    count = 0
    for step in iter_step_dicts(value):
        text = " ".join(
            [
                str(step.get("action") or ""),
                str(step.get("object") or ""),
                json.dumps(step.get("parameters") or {}, ensure_ascii=False),
            ]
        ).lower()
        has_params = bool(step.get("parameters"))
        strong = has_params or any(term.lower() in text for term in action_terms)
        weak_only = (
            text.strip()
            and any(term.lower() in text for term in weak_terms)
            and not strong
        )
        if strong and not weak_only:
            count += 1
    return count


def code_tutorial_status(video_dir: Path) -> dict:
    path = video_dir / "code_tutorial.md"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    return {
        "exists": path.exists(),
        "chars": len(text),
        "mentions_tutorial_md": "tutorial.md" in text,
        "source_rule": "唯一操作真值" in text or "以 `tutorial.md` 为准" in text,
    }


IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\n]+)\)")


def tutorial_visual_text_status(video_dir: Path) -> dict:
    tutorial_path = video_dir / "tutorial_path_refs.md"
    if not tutorial_path.exists():
        tutorial_path = video_dir / "tutorial.md"
    text = (
        tutorial_path.read_text(encoding="utf-8", errors="ignore")
        if tutorial_path.exists()
        else ""
    )
    text_no_images = IMAGE_RE.sub("", text)
    text_no_images = re.sub(r"data:image/[^)\s]+", "", text_no_images)
    image_count = len(IMAGE_RE.findall(text))
    visual_terms = [
        "视觉结果",
        "材质/颜色/纹理",
        "空间关系",
        "表面细节",
        "代码复现约束",
        "窗口视觉文字化约束",
        "资产/材质/空间文字化约束总表",
        "禁止遗漏",
    ]
    term_hits = {term: text.count(term) for term in visual_terms}
    contract_path = video_dir / "tutorial_visual_contract.json"
    contract = load_json(contract_path)
    quality = contract.get("quality") or {}
    step_count = int(quality.get("step_count") or 0)
    field_hits = quality.get("field_hits") or {}
    contract_items = int(quality.get("contract_items") or 0)
    visual_field_hits = sum(
        int(field_hits.get(key) or 0)
        for key in [
            "visual_result",
            "material_color",
            "spatial_relation",
            "surface_detail",
        ]
    )
    rich_text_chars = len(text_no_images.strip())
    min_contract_items = int(
        os.environ.get("BLENDER_PIPELINE_MIN_VISUAL_CONTRACT_ITEMS", "4") or "4"
    )
    min_visual_field_hits = int(
        os.environ.get("BLENDER_PIPELINE_MIN_VISUAL_FIELD_HITS", "4") or "4"
    )
    has_contract = bool(quality.get("has_visual_text_contract")) or (
        contract_items >= min_contract_items
        and visual_field_hits >= min_visual_field_hits
    )
    enough_text = rich_text_chars >= int(
        os.environ.get("BLENDER_PIPELINE_MIN_TUTORIAL_TEXT_CHARS_EX_IMAGES", "1800")
        or "1800"
    )
    marker_ok = all(
        term_hits.get(term, 0) > 0
        for term in ["视觉结果", "材质/颜色/纹理", "空间关系"]
    )
    status = (
        "pass"
        if (
            not REQUIRE_VISUAL_TEXT_CONTRACT
            or (enough_text and marker_ok and has_contract)
        )
        else "needs_fix"
    )
    review = {
        "status": status,
        "tutorial_source": str(tutorial_path),
        "image_count": image_count,
        "text_chars_excluding_images": rich_text_chars,
        "visual_term_hits": term_hits,
        "contract_path": str(contract_path) if contract_path.exists() else "",
        "contract_items": contract_items,
        "visual_field_hits": visual_field_hits,
        "step_count": step_count,
        "has_visual_text_contract": has_contract,
        "enough_text_excluding_images": enough_text,
        "required": REQUIRE_VISUAL_TEXT_CONTRACT,
    }
    (video_dir / "tutorial_visual_text_review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return review


def select_windows(windows: list[dict], max_windows: int) -> list[dict]:
    if max_windows <= 0 or len(windows) <= max_windows:
        return windows
    if max_windows == 1:
        return [windows[0]]
    selected = []
    last = len(windows) - 1
    seen = set()
    for slot in range(max_windows):
        index = round(slot * last / (max_windows - 1))
        if index not in seen:
            selected.append(windows[index])
            seen.add(index)
    return selected


def generate_tutorial_chunks_bounded(
    video_dir: Path, *, window_count: int, chunk_size: int
) -> None:
    """Run independent evidence windows concurrently without changing coverage."""

    jobs = []
    for start in range(0, window_count, chunk_size):
        jobs.append(
            [
                sys.executable,
                str(SCRIPTS / "generate_rich_tutorial_chunks.py"),
                "--video-dir",
                str(video_dir),
                "--start",
                str(start),
                "--count",
                str(chunk_size),
                "--secret",
                str(DEFAULT_SECRET),
                "--endpoint",
                DEFAULT_ENDPOINT,
                "--model",
                DEFAULT_MODEL,
            ]
        )
    parallelism = min(
        len(jobs),
        max(1, int(os.environ.get("BLENDER_PIPELINE_TUTORIAL_CHUNK_PARALLELISM", "2"))),
    )
    if parallelism <= 1:
        for command in jobs:
            run(command)
        return
    with ThreadPoolExecutor(
        max_workers=parallelism, thread_name_prefix="tutorial-chunk"
    ) as executor:
        futures = [executor.submit(run, command) for command in jobs]
        # Preserve deterministic error selection even though execution overlaps.
        for future in futures:
            future.result()


def ensure_tutorial(video_dir: Path, args: argparse.Namespace) -> None:
    tutorial = video_dir / "tutorial.md"
    path_refs = video_dir / "tutorial_path_refs.md"

    if args.force_tutorial:
        info = load_info(video_dir)
        source = Path(info.get("source_video_path") or video_dir / "source.mp4")
        if not source.exists():
            source = video_dir / "source.mp4"
        target = video_dir / "target_reference.png"
        if not target.exists():
            target = video_dir / "final_reference.png"
        if not source.exists() or not target.exists():
            raise FileNotFoundError(
                "force tutorial needs source.mp4/source_video_path and final_reference.png"
            )
        run(
            [
                sys.executable,
                str(SCRIPTS / "prepare_rich_tutorial_evidence.py"),
                "--video-dir",
                str(video_dir),
                "--source",
                str(source),
                "--bvid",
                str(info.get("bvid") or info.get("id") or video_dir.name),
                "--title",
                str(info.get("title") or video_dir.name),
                "--url",
                str(info.get("webpage_url") or info.get("original_url") or ""),
                "--target-reference",
                str(target),
                "--max-windows",
                str(args.max_windows),
            ]
        )
        # The first pass is only needed to validate the auxiliary reference
        # before tutorial generation. Defer the costly version OCR until the
        # merged tutorial exists, where it runs once with better evidence.
        build_pipeline_specs(video_dir, skip_version_ocr=True)
        windows_path = video_dir / "rich_evidence/windows.json"
        windows = json.loads(windows_path.read_text(encoding="utf-8"))
        original_window_count = len(windows)
        windows = select_windows(windows, args.max_windows)
        if len(windows) != original_window_count:
            full_windows_path = video_dir / "rich_evidence/windows_full.json"
            if not full_windows_path.exists():
                shutil.copy2(windows_path, full_windows_path)
            windows_path.write_text(
                json.dumps(windows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (video_dir / "tutorial_window_budget.json").write_text(
                json.dumps(
                    {
                        "original_windows": original_window_count,
                        "selected_windows": len(windows),
                        "max_windows": args.max_windows,
                        "strategy": "even_coverage",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        chunks_dir = video_dir / "rich_tutorial_chunks"
        if chunks_dir.exists():
            safe_rmtree(chunks_dir)
        generate_tutorial_chunks_bounded(
            video_dir,
            window_count=len(windows),
            chunk_size=max(1, args.chunk_count),
        )
        run(
            [
                sys.executable,
                str(SCRIPTS / "merge_rich_tutorial_chunks.py"),
                "--video-dir",
                str(video_dir),
            ]
        )

    legacy_rich = video_dir / "tutorial_rich.md"
    if legacy_rich.exists() and not path_refs.exists():
        shutil.copy2(legacy_rich, path_refs)
    legacy_rich.unlink(missing_ok=True)

    source_md = path_refs if path_refs.exists() else tutorial
    if not source_md.exists():
        raise FileNotFoundError(f"missing tutorial source in {video_dir}")
    if source_md != path_refs:
        shutil.copy2(source_md, path_refs)
    run(
        [
            sys.executable,
            str(SCRIPTS / "embed_markdown_images.py"),
            "--video-dir",
            str(video_dir),
            "--source-name",
            "tutorial_path_refs.md",
            "--dest-name",
            "tutorial.md",
            "--fallback-name",
            "tutorial_path_refs.md",
            "--max-side",
            str(args.embed_max_side),
        ]
    )
    steps_rich = video_dir / "steps_rich.json"
    steps_verified = video_dir / "steps_verified.json"
    if steps_rich.exists():
        shutil.copy2(steps_rich, steps_verified)


def build_pipeline_specs(video_dir: Path, *, skip_version_ocr: bool = False) -> None:
    env = os.environ.copy()
    if skip_version_ocr:
        env["BLENDER_PIPELINE_SKIP_VERSION_OCR"] = "1"
    run(
        [
            sys.executable,
            str(SCRIPTS / "build_pipeline_specs.py"),
            "--video-dir",
            str(video_dir),
        ],
        env=env,
    )


def retrieve_pre_spec_knowledge(video_dir: Path) -> None:
    """Build advisory/reviewed-compatible context without changing tutorial truth."""
    run(
        [
            sys.executable,
            str(SCRIPTS / "retrieve_blender_knowledge.py"),
            "--video-dir",
            str(video_dir),
        ]
    )


def select_blender_for_replay(video_dir: Path) -> Path | None:
    sys.path.insert(0, str(SCRIPTS))
    from blender_version_registry import build_version_plan

    plan = load_json(video_dir / "blender_version_plan.json")
    if not plan:
        plan = build_version_plan(
            video_dir, fallback_blender=os.environ.get("BLENDER_PIPELINE_BLENDER", "")
        )
    selected = (plan.get("execution") or {}).get("path") or os.environ.get(
        "BLENDER_PIPELINE_BLENDER", ""
    )
    if not selected:
        return None
    path = Path(selected)
    if path.exists():
        return path
    return None


def should_extract_workflow(video_dir: Path, mode: str) -> bool:
    if mode == "off":
        return False
    if mode == "always":
        return True
    info = load_info(video_dir)
    haystack = "\n".join(
        [
            str(info.get("title") or ""),
            str(info.get("description") or ""),
            str(info.get("desc") or ""),
            str(info.get("webpage_url") or ""),
            str(info.get("material_links") or ""),
            (video_dir / "tutorial_path_refs.md").read_text(
                encoding="utf-8", errors="ignore"
            )[:6000]
            if (video_dir / "tutorial_path_refs.md").exists()
            else "",
        ]
    ).lower()
    terms = [
        "workflow",
        "工作流",
        "几何节点",
        "geometry nodes",
        "shader nodes",
        "着色器节点",
        "compositor",
        "合成节点",
        "node tree",
        "节点树",
    ]
    return any(term.lower() in haystack for term in terms)


def extract_workflow_evidence(video_dir: Path, mode: str) -> None:
    if not should_extract_workflow(video_dir, mode):
        return
    info = load_info(video_dir)
    source = Path(info.get("source_video_path") or video_dir / "source.mp4")
    if not source.exists():
        source = video_dir / "source.mp4"
    cmd = [
        sys.executable,
        str(SCRIPTS / "extract_workflow_evidence.py"),
        "--video-dir",
        str(video_dir),
    ]
    if source.exists():
        cmd.extend(["--source", str(source)])
    run(cmd)


def run_draft_gate(video_dir: Path, min_steps: int) -> dict:
    reference_status = load_json(video_dir / "final_reference_status.json")
    steps = (
        load_json(video_dir / "steps_verified.json")
        or load_json(video_dir / "steps_rich.json")
        or {}
    )
    tutorial = video_dir / "tutorial.md"
    tutorial_chars = (
        len(tutorial.read_text(encoding="utf-8", errors="ignore"))
        if tutorial.exists()
        else 0
    )
    step_count = count_steps(steps)
    actionable_count = actionable_step_count(steps)
    code_status = code_tutorial_status(video_dir)
    visual_text_status = tutorial_visual_text_status(video_dir)
    route_status = blender_route_status(video_dir)
    checks = {
        "tutorial.md": tutorial.exists(),
        "tutorial_chars": tutorial_chars,
        "steps_count": step_count,
        "actionable_steps_count": actionable_count,
        "code_tutorial.md": code_status["exists"],
        "code_tutorial_chars": code_status["chars"],
        "code_tutorial_source_rule": code_status["mentions_tutorial_md"]
        and code_status["source_rule"],
        "tutorial_visual_text_contract": visual_text_status.get("status") == "pass",
        "blender_route": route_status["is_blender"],
        "final_reference_valid": bool(reference_status.get("valid")),
        "motion_plan.json": (video_dir / "motion_plan.json").exists(),
        "material_spec.json": (video_dir / "material_spec.json").exists(),
    }
    issues = []
    if not checks["tutorial.md"] or tutorial_chars < 500:
        issues.append("tutorial is missing or too small for reliable replay")
    if step_count < min_steps:
        issues.append(f"too few extracted steps for replay: {step_count} < {min_steps}")
    if actionable_count < max(MIN_ACTIONABLE_STEPS, min_steps // 2):
        issues.append(
            f"too few actionable tutorial steps for replay: {actionable_count}"
        )
    if (
        not code_status["exists"]
        or code_status["chars"] < 500
        or not checks["code_tutorial_source_rule"]
    ):
        issues.append(
            "code_tutorial.md is missing, too small, or does not preserve tutorial.md as the source of truth"
        )
    if visual_text_status.get("status") != "pass":
        issues.append(
            "tutorial visual-text contract failed: "
            f"text_chars_excluding_images={visual_text_status.get('text_chars_excluding_images')}, "
            f"contract_items={visual_text_status.get('contract_items')}, "
            f"visual_field_hits={visual_text_status.get('visual_field_hits')}"
        )
    if not route_status["is_blender"]:
        issues.append(route_status["reason"])
    if not checks["motion_plan.json"]:
        issues.append("missing motion_plan.json")
    if not checks["material_spec.json"]:
        issues.append("missing material_spec.json")
    gate = {
        "status": "pass" if not issues else "needs_fix",
        "checks": checks,
        "issues": issues,
    }
    (video_dir / "draft_gate.json").write_text(
        json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return gate


def run_replay(video_dir: Path, out_name: str, skip: bool) -> Path | None:
    if skip:
        return None
    sys.path.insert(0, str(SCRIPTS))
    import run_video_strict_replay as strict_replay

    strict_replay.OUT_NAME = out_name
    selected_blender = select_blender_for_replay(video_dir)
    if selected_blender:
        strict_replay.BLENDER = selected_blender
    rec = strict_replay.process(video_dir)
    out_dir = video_dir / out_name
    (video_dir / "replay_main_state.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if rec.get("status") not in {"done", "skipped_existing"}:
        raise RuntimeError(f"replay failed: {rec.get('status')}")
    return out_dir


def publish_outputs(video_dir: Path, out_dir: Path | None) -> None:
    if not out_dir or not out_dir.exists():
        return
    motion_plan = effective_motion_plan(video_dir, out_dir)
    turntable_allowed = bool(motion_plan.get("turntable_allowed_as_final_effect", True))
    for name in [
        "asset.blend",
        "render.png",
        "reproduce.py",
        "render_engine_probe.json",
        "generation_render_receipt.json",
        "postprocess_render_receipt.json",
        "route_override.json",
    ]:
        src = out_dir / name
        if src.exists():
            shutil.copy2(src, video_dir / name)
    final_src = out_dir / "final_effect.mp4"
    final_source_type = "final_effect"
    if not final_src.exists() and turntable_allowed:
        final_src = out_dir / "turntable_5s.mp4"
        final_source_type = "static_turntable_fallback"
    if final_src.exists():
        shutil.copy2(final_src, video_dir / "final_effect.mp4")
        (video_dir / "final_effect_source.json").write_text(
            json.dumps(
                {"source_type": final_source_type, "source_path": str(final_src)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    views_src = out_dir / "six_views"
    views_dst = video_dir / "six_views"
    if views_src.exists():
        if views_dst.exists():
            shutil.rmtree(views_dst)
        shutil.copytree(views_src, views_dst)
        legacy_top = views_dst / " .png"
        if legacy_top.exists() and not (views_dst / "top.png").exists():
            legacy_top.rename(views_dst / "top.png")
        elif legacy_top.exists():
            legacy_top.unlink()


def required_pipeline_check_keys(*, dynamic: bool, source_kind: str) -> frozenset[str]:
    """Return only checks that apply to the routed delivery contract."""

    keys = {
        "tutorial.md",
        "steps_verified.json",
        "steps_count",
        "actionable_steps_count",
        "asset.blend",
        "render.png",
        "code_tutorial.md",
        "code_tutorial_chars",
        "code_tutorial_source_rule",
        "tutorial_visual_text_contract",
        "blender_route",
        "motion_plan.json",
        "material_spec.json",
        "character_material_review",
        "final_reference_status.json",
        "replay_status",
        "render_time_review",
        "no_visual_review_warning",
        "final_effect.mp4" if dynamic else "six_views",
    }
    if source_kind == "video_replay_type2":
        keys.update(
            {
                "type2_linked_asset_audit",
                "type2_linked_asset_knowledge",
                "type2_linked_source_runtime_use",
            }
        )
    return frozenset(keys)


def static_preview_available(video_dir: Path) -> bool:
    """Return whether all six canonical static delivery views are valid."""

    return complete_six_view_delivery(video_dir)


def review_pipeline_outputs(video_dir: Path, out_dir: Path | None) -> None:
    motion_plan = effective_motion_plan(video_dir, out_dir)
    material_spec = load_json(video_dir / "material_spec.json")
    reference_status = load_json(video_dir / "final_reference_status.json")
    final_source = load_json(video_dir / "final_effect_source.json")
    replay_state = load_json(video_dir / "replay_main_state.json")
    render_time_review = load_json(
        (out_dir / "render_time_review.json")
        if out_dir
        else video_dir / "render_time_review.json"
    )
    steps = load_json(video_dir / "steps_verified.json") or {}
    step_count = count_steps(steps)
    actionable_count = actionable_step_count(steps)
    code_status = code_tutorial_status(video_dir)
    visual_text_status = tutorial_visual_text_status(video_dir)
    route_status = blender_route_status(video_dir)
    dynamic = motion_plan.get("motion_type") == "dynamic"
    character_review = {"required": False, "status": "pass", "missing": []}
    script_text = ""
    for candidate in [
        video_dir / "reproduce.py",
        (out_dir / "reproduce.py") if out_dir else None,
    ]:
        if candidate and candidate.exists():
            script_text += (
                "\n" + candidate.read_text(encoding="utf-8", errors="ignore").lower()
            )
    if material_spec.get("subject_family") == "human_character":
        groups = {
            "skin": ["skin", "face", "head", "皮肤", "肤色", "脸"],
            "hair": ["hair", "头发", "发"],
            "eyes": ["eye", "pupil", "iris", "眼", "瞳"],
            "clothing": [
                "shirt",
                "cloth",
                "clothes",
                "jacket",
                "pants",
                "trouser",
                "衣",
                "裤",
                "服",
            ],
            "shoes": ["shoe", "boot", "鞋", "靴"],
        }
        hits = {
            name: any(token in script_text for token in tokens)
            for name, tokens in groups.items()
        }
        missing = [name for name, ok in hits.items() if not ok]
        character_review = {
            "required": True,
            "status": "pass" if len(missing) <= 1 else "needs_fix",
            "hits": hits,
            "missing": missing,
            "policy": "human_character outputs must explicitly model/materialize skin, hair, eyes, clothing, and shoes/accessories; no default mannequin.",
        }
        (video_dir / "character_material_review.json").write_text(
            json.dumps(character_review, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif material_spec.get("subject_family") == "animal_cartoon_character":
        groups = {
            "body": ["body", "torso", "身体", "body_mat", "fur", "feather"],
            "eyes": ["eye", "pupil", "iris", "眼", "瞳"],
            "face": [
                "face",
                "mouth",
                "nose",
                "whisker",
                "beak",
                "脸",
                "嘴",
                "鼻",
                "胡须",
                "喙",
            ],
            "appendages": [
                "ear",
                "tail",
                "leg",
                "paw",
                "wing",
                "耳",
                "尾",
                "腿",
                "爪",
                "翅",
            ],
        }
        hits = {
            name: any(token in script_text for token in tokens)
            for name, tokens in groups.items()
        }
        missing = [name for name, ok in hits.items() if not ok]
        character_review = {
            "required": True,
            "status": "pass" if len(missing) <= 1 else "needs_fix",
            "hits": hits,
            "missing": missing,
            "policy": "animal_cartoon_character outputs must explicitly model/materialize body, eyes, face details, and ears/tail/limbs/wings when present; human-only shoe/clothing checks do not apply.",
        }
        (video_dir / "character_material_review.json").write_text(
            json.dumps(character_review, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif material_spec.get("subject_family") == "stylized_game_character":
        groups = {
            "body": ["body", "torso", "身体", "body_mat"],
            "eyes_or_face": [
                "eye",
                "pupil",
                "face",
                "mouth",
                "nose",
                "眼",
                "脸",
                "嘴",
                "鼻",
            ],
            "limbs": ["arm", "leg", "hand", "foot", "limb", "胳膊", "腿", "手"],
            "gear_or_clothing": [
                "armor",
                "belt",
                "strap",
                "glove",
                "boot",
                "cloth",
                "gear",
                "护甲",
                "腰带",
                "绑带",
                "手套",
                "靴",
            ],
            "animation_pose": [
                "keyframe",
                "pose",
                "animation",
                "rig",
                "骨骼",
                "动画",
                "姿态",
            ],
        }
        hits = {
            name: any(token in script_text for token in tokens)
            for name, tokens in groups.items()
        }
        missing = [name for name, ok in hits.items() if not ok]
        character_review = {
            "required": True,
            "status": "pass" if len(missing) <= 1 else "needs_fix",
            "hits": hits,
            "missing": missing,
            "policy": "stylized_game_character outputs must preserve body plan, face/eyes, limbs, visible gear/clothing/accessories, and a pose or animation cue; do not substitute a different generic character.",
        }
        (video_dir / "character_material_review.json").write_text(
            json.dumps(character_review, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    checks = {
        "tutorial.md": (video_dir / "tutorial.md").exists(),
        "steps_verified.json": (video_dir / "steps_verified.json").exists(),
        "steps_count": step_count >= MIN_VERIFIED_STEPS,
        "actionable_steps_count": actionable_count >= MIN_ACTIONABLE_STEPS,
        "asset.blend": (video_dir / "asset.blend").exists(),
        "render.png": (video_dir / "render.png").exists(),
        "six_views": static_preview_available(video_dir),
        "code_tutorial.md": code_status["exists"],
        "code_tutorial_chars": code_status["chars"] >= 500,
        "code_tutorial_source_rule": code_status["mentions_tutorial_md"]
        and code_status["source_rule"],
        "tutorial_visual_text_contract": visual_text_status.get("status") == "pass",
        "blender_route": route_status["is_blender"],
        "motion_plan.json": bool(motion_plan),
        "material_spec.json": bool(material_spec),
        "character_material_review": character_review.get("status") == "pass",
        "final_reference_status.json": bool(reference_status),
        "final_reference_valid": bool(reference_status.get("valid")),
        "final_effect.mp4": (video_dir / "final_effect.mp4").exists(),
        "replay_status": replay_state.get("status") in {"done", "skipped_existing"},
        "render_time_review": bool(render_time_review)
        and render_time_review.get("status") == "pass",
        "no_visual_review_warning": not bool(replay_state.get("visual_review_warning")),
    }
    source_info = load_json(video_dir / "source.info.json")
    if source_info.get("source_kind") == "video_replay_type2":
        linked_audit = load_json(video_dir / "linked_asset_audit.json")
        linked_knowledge = load_json(video_dir / "linked_asset_knowledge.json")
        linked_receipt = load_json(
            (out_dir / "linked_source_runtime_receipt.json")
            if out_dir
            else (video_dir / "linked_source_runtime_receipt.json")
        )
        expected_hash = str(
            (source_info.get("linked_source") or {}).get("selected_model_sha256") or ""
        )
        checks.update(
            {
                "type2_linked_asset_audit": (
                    linked_audit.get("selected_model_sha256") == expected_hash
                ),
                "type2_linked_asset_knowledge": (
                    linked_knowledge.get("selected_model_sha256") == expected_hash
                    and linked_knowledge.get("role") == "advisory_reproduction_evidence"
                ),
                "type2_linked_source_runtime_use": (
                    linked_receipt.get("status") == "loaded_and_used"
                    and linked_receipt.get("selected_model_sha256") == expected_hash
                ),
            }
        )
    issues = []
    if reference_status and not reference_status.get("valid"):
        review_warning = (
            "final_reference is invalid and was ignored as auxiliary visual evidence; "
            "tutorial.md, steps_verified.json, and embedded evidence images remain the source of truth."
        )
        replay_warnings = [
            replay_state.get("visual_review_warning", ""),
            review_warning,
        ]
    else:
        replay_warnings = [replay_state.get("visual_review_warning", "")]
    if step_count < MIN_VERIFIED_STEPS:
        issues.append(f"tutorial step gate failed: steps_count={step_count}")
    if actionable_count < MIN_ACTIONABLE_STEPS:
        issues.append(
            f"tutorial actionable-step gate failed: actionable_steps_count={actionable_count}"
        )
    if (
        not code_status["exists"]
        or code_status["chars"] < 500
        or not checks["code_tutorial_source_rule"]
    ):
        issues.append("code_tutorial.md gate failed")
    if visual_text_status.get("status") != "pass":
        issues.append(
            "tutorial visual-text contract gate failed: "
            f"text_chars_excluding_images={visual_text_status.get('text_chars_excluding_images')}, "
            f"contract_items={visual_text_status.get('contract_items')}, "
            f"visual_field_hits={visual_text_status.get('visual_field_hits')}"
        )
    if not route_status["is_blender"]:
        issues.append(route_status["reason"])
    if replay_state.get("status") and replay_state.get("status") not in {
        "done",
        "skipped_existing",
    }:
        issues.append(f"replay status is not publishable: {replay_state.get('status')}")
    if replay_state.get("visual_review_warning"):
        issues.append("visual review failed; result cannot be downgraded to warning")
    if not render_time_review:
        issues.append(
            "render_time_review.json missing; final-quality render gate was not evaluated"
        )
    elif render_time_review.get("status") != "pass":
        issues.extend(render_time_review.get("issues") or ["render time review failed"])
    if dynamic and final_source.get("source_type") == "static_turntable_fallback":
        issues.append("dynamic task cannot publish static turntable as final_effect")
        checks["final_effect.mp4"] = False
    if dynamic and not (video_dir / "final_effect.mp4").exists():
        issues.append("dynamic task needs a true final_effect.mp4")
    if character_review.get("status") != "pass":
        issues.append(
            "human/character material gate failed: "
            + ", ".join(character_review.get("missing") or [])
        )
    if source_info.get("source_kind") == "video_replay_type2":
        missing_type2 = [
            key
            for key in (
                "type2_linked_asset_audit",
                "type2_linked_asset_knowledge",
                "type2_linked_source_runtime_use",
            )
            if not checks.get(key)
        ]
        if missing_type2:
            issues.append(
                "Type 2 linked-asset reproduction evidence failed: "
                + ", ".join(missing_type2)
            )
    required_checks = required_pipeline_check_keys(
        dynamic=dynamic,
        source_kind=str(source_info.get("source_kind") or ""),
    )
    failed_required_checks = sorted(
        key for key in required_checks if not checks.get(key)
    )
    if failed_required_checks:
        issues.append(
            "required pipeline checks failed: " + ", ".join(failed_required_checks)
        )
    review = {
        "status": "pass" if not failed_required_checks and not issues else "needs_fix",
        "checks": checks,
        "issues": issues,
        "warnings": [warning for warning in replay_warnings if warning],
        "render_time_review": render_time_review,
        "out_dir": str(out_dir) if out_dir else "",
    }
    (video_dir / "pipeline_review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def generate_code_tutorial(video_dir: Path, out_dir: Path | None) -> None:
    cmd = [
        sys.executable,
        str(SCRIPTS / "generate_code_tutorial.py"),
        "--video-dir",
        str(video_dir),
    ]
    if out_dir:
        cmd.extend(["--asset-dir", str(out_dir)])
    run(cmd)


def render_illustrated_tutorial(video_dir: Path) -> None:
    run(
        [
            sys.executable,
            str(SCRIPTS / "render_illustrated_tutorial.py"),
            "--video-dir",
            str(video_dir),
        ]
    )


def update_knowledge(video_dir: Path) -> None:
    run(
        [
            sys.executable,
            str(SCRIPTS / "update_replay_knowledge_base.py"),
            "--video-dir",
            str(video_dir),
        ]
    )


def validate_forced_tutorial_runtime(args: argparse.Namespace) -> None:
    """Fail before workspace writes when paid tutorial extraction is unsafe."""

    if not args.force_tutorial:
        return
    if args.max_windows < 1:
        raise RuntimeError("--force-tutorial requires a positive --max-windows")
    parsed = urllib.parse.urlparse(DEFAULT_ENDPOINT)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError(
            "forced tutorial extraction requires a credential-free HTTPS "
            "BLENDER_PIPELINE_API_ENDPOINT"
        )
    if DEFAULT_MODEL not in {"gpt-5.6-sol", "gpt-5.5"}:
        raise RuntimeError(
            "forced tutorial extraction requires gpt-5.6-sol, with gpt-5.5 "
            "allowed only as the explicit fallback"
        )
    if not DEFAULT_SECRET.is_file() or DEFAULT_SECRET.stat().st_mode & 0o077:
        raise RuntimeError(
            "forced tutorial extraction requires BLENDER_PIPELINE_API_KEY_FILE "
            "to point to an owner-only regular file"
        )


def main() -> int:
    global ACTIVE_TRAJECTORY
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--force-tutorial", action="store_true")
    parser.add_argument("--chunk-count", type=int, default=2)
    parser.add_argument("--embed-max-side", type=int, default=1600)
    parser.add_argument("--out-name", default="main_replay_v1")
    parser.add_argument("--skip-replay", action="store_true")
    parser.add_argument("--skip-knowledge", action="store_true")
    parser.add_argument("--skip-knowledge-update", action="store_true")
    parser.add_argument("--draft-gate", action="store_true")
    parser.add_argument("--draft-only", action="store_true")
    parser.add_argument("--min-draft-steps", type=int, default=MIN_VERIFIED_STEPS)
    parser.add_argument(
        "--max-windows",
        type=int,
        default=int(
            os.environ.get("BLENDER_PIPELINE_MAX_TUTORIAL_WINDOWS", "0") or "0"
        ),
    )
    parser.add_argument(
        "--workflow-evidence",
        choices=["off", "auto", "always"],
        default=os.environ.get("BLENDER_PIPELINE_WORKFLOW_EVIDENCE", "auto"),
    )
    parser.add_argument(
        "--render-tutorial-html",
        action="store_true",
        help=(
            "Optionally render a human-facing HTML view; tutorial.md remains "
            "the operational source of truth."
        ),
    )
    args = parser.parse_args()
    validate_forced_tutorial_runtime(args)

    video_dir = args.video_dir
    if not video_dir.exists():
        raise FileNotFoundError(video_dir)
    info = load_info(video_dir)
    source_kind = str(
        info.get("source_kind") or info.get("workload_kind") or "video_replay"
    )
    trace_root = video_dir / args.out_name / "agent_trace"
    ACTIVE_TRAJECTORY = AgentTrajectory.from_environment()
    owns_trajectory = ACTIVE_TRAJECTORY is None
    if ACTIVE_TRAJECTORY is None:
        ACTIVE_TRAJECTORY = AgentTrajectory.create(
            trace_root,
            identity={
                "asset_id": str(info.get("bvid") or info.get("id") or video_dir.name),
                "work_item_id": f"video-replay-main:{video_dir.name}:{args.out_name}",
                "source_kind": source_kind,
                "pipeline": "run_video_replay_main",
            },
        )
    trace_manifest = load_json(ACTIVE_TRAJECTORY.manifest_path)
    if owns_trajectory and trace_manifest.get("status") != "running":
        raise RuntimeError(
            "existing video replay trajectory is terminal; choose a new out-name or explicitly archive it before rerun"
        )
    ACTIVE_TRAJECTORY.recover_interrupted_calls()
    previous_root = os.environ.get("VIDEO2BLENDER_AGENT_TRAJECTORY_ROOT")
    ACTIVE_TRAJECTORY.activate_environment()
    ACTIVE_TRAJECTORY.append_event(
        event_type="task_context",
        role="user",
        stage="pipeline_start",
        content={
            "video_dir": str(video_dir),
            "source_info": info,
            "arguments": vars(args),
        },
        attachments=ACTIVE_TRAJECTORY.store_path(
            video_dir / "source.info.json", semantic_role="source_evidence"
        ),
    )
    try:
        ensure_tutorial(video_dir, args)
        if args.render_tutorial_html:
            render_illustrated_tutorial(video_dir)
        extract_workflow_evidence(video_dir, args.workflow_evidence)
        if not args.skip_knowledge:
            retrieve_pre_spec_knowledge(video_dir)
        build_pipeline_specs(video_dir)
        generate_code_tutorial(video_dir, None)
        if args.draft_gate or args.draft_only:
            gate = run_draft_gate(video_dir, args.min_draft_steps)
            gate_call = ACTIVE_TRAJECTORY.record_tool_call(
                "draft_gate",
                {"min_steps": args.min_draft_steps},
                stage="draft_gate",
            )
            ACTIVE_TRAJECTORY.record_tool_result(
                "draft_gate", gate_call, gate, stage="draft_gate"
            )
            if gate.get("status") != "pass":
                raise RuntimeError(f"draft gate failed: {gate.get('issues')}")
            if args.draft_only:
                if owns_trajectory:
                    ACTIVE_TRAJECTORY.finish(
                        "accepted", {"status": "draft_gate_passed"}
                    )
                    ACTIVE_TRAJECTORY.export_derived()
                log(f"draft gate passed: {video_dir}")
                return 0
        out_dir = run_replay(video_dir, args.out_name, args.skip_replay)
        publish_outputs(video_dir, out_dir)
        review_pipeline_outputs(video_dir, out_dir)
        if not args.skip_knowledge and not args.skip_knowledge_update:
            update_knowledge(video_dir)
        review = load_json(video_dir / "pipeline_review.json")
        artifact_attachments: list[dict] = []
        for path in (
            video_dir / "asset.blend",
            video_dir / "render.png",
            video_dir / "six_views",
            video_dir / "final_effect.mp4",
            video_dir / "pipeline_review.json",
        ):
            artifact_attachments.extend(
                ACTIVE_TRAJECTORY.reference_path(path, semantic_role="result_artifact")
            )
        ACTIVE_TRAJECTORY.append_event(
            event_type="artifact_manifest",
            role="tool",
            stage="publish",
            content={"pipeline_review": review},
            attachments=artifact_attachments,
        )
        status = "accepted" if review.get("status") == "pass" else "needs_review"
        if owns_trajectory:
            ACTIVE_TRAJECTORY.finish(
                status, {"status": status, "pipeline_review": review}
            )
            ACTIVE_TRAJECTORY.export_derived()
            if (video_dir / "asset.blend").is_file():
                ACTIVE_TRAJECTORY.publish_to(video_dir / "agent_trace")
        log(f"done: {video_dir}")
        return 0
    except Exception as exc:
        if owns_trajectory:
            ACTIVE_TRAJECTORY.finish(
                "failed_exception",
                {
                    "status": "failed_exception",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
            )
            ACTIVE_TRAJECTORY.export_derived()
        raise
    finally:
        ACTIVE_TRAJECTORY = None
        if previous_root is None:
            os.environ.pop("VIDEO2BLENDER_AGENT_TRAJECTORY_ROOT", None)
        else:
            os.environ["VIDEO2BLENDER_AGENT_TRAJECTORY_ROOT"] = previous_root


if __name__ == "__main__":
    raise SystemExit(main())
