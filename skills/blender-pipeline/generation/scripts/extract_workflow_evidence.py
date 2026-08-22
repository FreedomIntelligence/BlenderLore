#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image, ImageFilter, ImageOps, ImageStat

RESAMPLE_LANCZOS = getattr(
    getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
)

WORKFLOW_TERMS = [
    "workflow",
    "工作流",
    "节点",
    "node",
    "nodes",
    "geometry nodes",
    "几何节点",
    "shader nodes",
    "着色器节点",
    "compositor",
    "合成",
    "node tree",
    "节点树",
]

UI_TERMS = [
    "geometry nodes",
    "shader editor",
    "compositor",
    "attribute",
    "group input",
    "group output",
    "principled",
    "bsdf",
    "mix",
    "multiply",
    "vector",
    "color ramp",
    "noise texture",
    "voronoi",
    "节点",
    "几何节点",
    "着色器",
    "材质",
    "输入",
    "输出",
]

STRICT_NODE_TERMS = [
    "group input",
    "group output",
    "geometry nodes",
    "shader editor",
    "compositor",
    "node tree",
    "principled bsdf",
    "color ramp",
    "noise texture",
    "voronoi",
    "attribute",
    "join geometry",
    "set position",
    "realize instances",
    "distribute points",
    "组输入",
    "组输出",
    "节点树",
    "颜色渐变",
    "噪波纹理",
    "几何体",
    "实例",
]


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_name(value: str, max_len: int = 80) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_")
    return (value or "item")[:max_len]


def ffprobe_duration(source: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
    )
    return float(proc.stdout.strip())


def extract_frame(source: Path, timestamp: float, output: Path) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        return True
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-1",
            str(output),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=90,
    )
    return proc.returncode == 0 and output.exists()


def run_ocr(path: Path) -> str:
    exe = shutil.which("tesseract")
    if not exe or not path.exists():
        return ""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    crops = {
        "whole": img,
        "center": img.crop(
            (
                int(img.width * 0.08),
                int(img.height * 0.08),
                int(img.width * 0.92),
                int(img.height * 0.92),
            )
        ),
        "bottom": img.crop((0, int(img.height * 0.65), img.width, img.height)),
    }
    texts: list[str] = []
    for name, crop in crops.items():
        tmp = path.with_name(f"{path.stem}_{name}_workflow_ocr.png")
        crop = ImageOps.autocontrast(ImageOps.grayscale(crop))
        crop = crop.resize((crop.width * 2, crop.height * 2), RESAMPLE_LANCZOS)
        crop.save(tmp)
        try:
            proc = subprocess.run(
                [exe, str(tmp), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
            )
            texts.append(" ".join(proc.stdout.split()))
        except subprocess.TimeoutExpired:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    return " ".join(t for t in texts if t).strip()


def edge_score(path: Path) -> float:
    img = ImageOps.exif_transpose(Image.open(path)).convert("L")
    img.thumbnail((360, 220), RESAMPLE_LANCZOS)
    edges = img.filter(ImageFilter.FIND_EDGES)
    stat = ImageStat.Stat(edges)
    return float(stat.mean[0]) / 255.0


def text_hits(text: str, terms: list[str]) -> list[str]:
    lower = text.lower()
    return sorted({term for term in terms if term.lower() in lower})


def image_candidate_score(
    path: Path, ocr_text: str, context_text: str
) -> tuple[float, list[str]]:
    hits = text_hits(" ".join([ocr_text, context_text]), UI_TERMS)
    workflow_hits = text_hits(" ".join([ocr_text, context_text]), WORKFLOW_TERMS)
    strict_hits = text_hits(ocr_text, STRICT_NODE_TERMS)
    edge = edge_score(path)
    score = 0.0
    reasons: list[str] = []
    if strict_hits:
        score += min(0.50, 0.18 * len(strict_hits))
        reasons.append("strict_node_term:" + ",".join(strict_hits[:5]))
    if workflow_hits:
        score += min(0.18, 0.05 * len(workflow_hits))
        reasons.append("workflow_term:" + ",".join(workflow_hits[:5]))
    if hits:
        score += min(0.22, 0.04 * len(hits))
        reasons.append("node_ui_term:" + ",".join(hits[:6]))
    if edge >= 0.11:
        score += 0.18
        reasons.append(f"high_edge_density:{edge:.3f}")
    elif edge >= 0.075:
        score += 0.08
        reasons.append(f"medium_edge_density:{edge:.3f}")
    if len(ocr_text) >= 80:
        score += 0.07
        reasons.append("ocr_text_rich")
    # Subtitle-only mentions such as "几何节点" often appear over ordinary viewport
    # footage. Keep them as weak hints, but do not let them become a workflow
    # screenshot unless the frame itself shows node-editor evidence.
    has_visual_node_signal = bool(strict_hits) or (edge >= 0.11 and len(hits) >= 3)
    if not has_visual_node_signal:
        score = min(score, 0.24)
        reasons.append("capped_without_visual_node_signal")
    return min(score, 1.0), reasons


def make_contact_sheet(
    items: list[dict[str, Any]], video_dir: Path, output: Path
) -> None:
    if not items:
        output.unlink(missing_ok=True)
        return
    thumbs = []
    for item in items:
        path = video_dir / item["path"]
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((360, 210), RESAMPLE_LANCZOS)
        canvas = Image.new("RGB", (360, 250), "white")
        canvas.paste(img, ((360 - img.width) // 2, 28))
        label = f"{item.get('source', 'frame')} score={item.get('score', 0):.2f}"
        if item.get("timestamp_sec") is not None:
            label += f" t={item['timestamp_sec']:.1f}s"
        try:
            from PIL import ImageDraw

            draw = ImageDraw.Draw(canvas)
            draw.text((8, 8), label, fill=(0, 0, 0))
        except Exception:
            pass
        thumbs.append(canvas)
    cols = 2
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * 360, rows * 250), "white")
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((index % cols) * 360, (index // cols) * 250))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=90)


def fetch_bilibili_view(session: requests.Session, bvid: str) -> dict[str, Any]:
    if not bvid:
        return {}
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://www.bilibili.com/video/{bvid}/",
    }
    try:
        resp = session.get(url, headers=headers, timeout=20)
        data = resp.json()
        return data.get("data") or {}
    except Exception:
        return {}


def extract_picture_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    content = item.get("content") or {}
    for container in [content, item]:
        pictures = container.get("pictures") or container.get("pics") or []
        if isinstance(pictures, dict):
            pictures = [pictures]
        for picture in pictures:
            if not isinstance(picture, dict):
                continue
            for key in ["img_src", "url", "src"]:
                value = picture.get(key)
                if isinstance(value, str) and value:
                    urls.append(value)
    return urls


def fetch_comment_images(
    video_dir: Path, bvid: str, max_pages: int, page_size: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    session = requests.Session()
    session.trust_env = False
    view = fetch_bilibili_view(session, bvid)
    aid = str(view.get("aid") or "")
    if not aid:
        return [], {"status": "missing_aid", "bvid": bvid}
    out_dir = video_dir / "workflow_evidence/comment_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://www.bilibili.com/video/{bvid}/",
    }
    candidates: list[dict[str, Any]] = []
    pages_scanned = 0
    for page in range(1, max_pages + 1):
        url = f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&pn={page}&ps={page_size}"
        try:
            resp = session.get(url, headers=headers, timeout=25)
            data = resp.json().get("data") or {}
        except Exception:
            break
        comments = []
        top = (data.get("upper") or {}).get("top")
        if isinstance(top, dict):
            comments.append(("top_comment", top))
        comments.extend(("comment", item) for item in data.get("replies") or [])
        if not comments:
            break
        pages_scanned = page
        for source, item in comments:
            message = ((item.get("content") or {}).get("message") or "").strip()
            picture_urls = extract_picture_urls(item)
            if not picture_urls:
                continue
            for pic_index, pic_url in enumerate(picture_urls):
                parsed = urlparse(pic_url)
                suffix = Path(parsed.path).suffix.lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                    suffix = ".jpg"
                name = f"{source}_p{page}_{item.get('rpid') or item.get('rpid_str') or 'unknown'}_{pic_index}{suffix}"
                dst = out_dir / safe_name(name, 120)
                if not dst.exists():
                    try:
                        img_resp = session.get(pic_url, headers=headers, timeout=30)
                        if img_resp.status_code < 400:
                            dst.write_bytes(img_resp.content)
                    except Exception:
                        continue
                if dst.exists():
                    ocr_text = run_ocr(dst)
                    score, reasons = image_candidate_score(dst, ocr_text, message)
                    candidates.append(
                        {
                            "source": "bilibili_comment_image",
                            "path": str(dst.relative_to(video_dir)),
                            "comment_source": source,
                            "comment_text": message[:500],
                            "picture_url": pic_url,
                            "ocr_text": ocr_text[:1200],
                            "score": score,
                            "reasons": reasons,
                        }
                    )
        time.sleep(0.4)
    return candidates, {
        "status": "ok",
        "bvid": bvid,
        "aid": aid,
        "pages_scanned": pages_scanned,
    }


def source_context(video_dir: Path) -> tuple[dict[str, Any], str]:
    info = read_json(video_dir / "source.info.json") or {}
    pieces = [
        str(info.get("title") or ""),
        str(info.get("description") or ""),
        str(info.get("desc") or ""),
        str(info.get("material_links") or ""),
        str(info.get("webpage_url") or ""),
    ]
    if (video_dir / "transcript.txt").exists():
        pieces.append(
            (video_dir / "transcript.txt").read_text(encoding="utf-8", errors="ignore")[
                :8000
            ]
        )
    if (video_dir / "tutorial_path_refs.md").exists():
        pieces.append(
            (video_dir / "tutorial_path_refs.md").read_text(
                encoding="utf-8", errors="ignore"
            )[:8000]
        )
    return info, "\n".join(pieces)


def collect_video_frame_candidates(
    video_dir: Path,
    source: Path,
    max_frames: int,
    max_ocr_frames: int,
    min_score: float,
) -> list[dict[str, Any]]:
    evidence = video_dir / "workflow_evidence"
    frames_dir = evidence / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    info, context = source_context(video_dir)
    duration = ffprobe_duration(source)
    timestamps = []
    if duration <= 0:
        return []
    step = max(duration / max_frames, 3.0)
    current = 0.0
    while current < duration:
        timestamps.append(round(current, 3))
        current += step
    frame_rows: list[dict[str, Any]] = []
    for index, ts in enumerate(timestamps):
        frame = frames_dir / f"workflow_t_{int(ts):05d}s.jpg"
        if not extract_frame(source, ts, frame):
            continue
        frame_rows.append(
            {"timestamp_sec": ts, "path": frame, "edge": edge_score(frame)}
        )
    if not frame_rows:
        return []
    frame_rows = sorted(frame_rows, key=lambda item: item["edge"], reverse=True)[
        :max_ocr_frames
    ]
    candidates: list[dict[str, Any]] = []
    for row in frame_rows:
        frame = row["path"]
        ocr_text = run_ocr(frame)
        score, reasons = image_candidate_score(frame, ocr_text, context)
        if score >= min_score:
            candidates.append(
                {
                    "source": "video_frame",
                    "timestamp_sec": row["timestamp_sec"],
                    "path": str(frame.relative_to(video_dir)),
                    "ocr_text": ocr_text[:1200],
                    "score": score,
                    "reasons": reasons,
                }
            )
    return sorted(candidates, key=lambda item: item["score"], reverse=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--max-frame-candidates", type=int, default=80)
    parser.add_argument("--max-ocr-frames", type=int, default=24)
    parser.add_argument("--max-comment-pages", type=int, default=2)
    parser.add_argument("--comment-page-size", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=0.32)
    parser.add_argument("--max-keep", type=int, default=8)
    args = parser.parse_args()

    video_dir = args.video_dir
    info, context = source_context(video_dir)
    bvid = str(info.get("bvid") or info.get("id") or "")
    source = args.source or Path(
        info.get("source_video_path") or video_dir / "source.mp4"
    )
    if not source.exists():
        source = video_dir / "source.mp4"

    title_hits = text_hits(context, WORKFLOW_TERMS)
    video_candidates: list[dict[str, Any]] = []
    if source.exists():
        video_candidates = collect_video_frame_candidates(
            video_dir,
            source,
            args.max_frame_candidates,
            args.max_ocr_frames,
            args.min_score,
        )

    comment_candidates: list[dict[str, Any]] = []
    comment_meta = {"status": "skipped"}
    if bvid and args.max_comment_pages > 0:
        comment_candidates, comment_meta = fetch_comment_images(
            video_dir, bvid, args.max_comment_pages, args.comment_page_size
        )

    all_candidates = sorted(
        video_candidates + comment_candidates,
        key=lambda item: item.get("score", 0),
        reverse=True,
    )
    kept = all_candidates[: args.max_keep]
    contact_sheet = video_dir / "workflow_evidence/workflow_contact_sheet.jpg"
    make_contact_sheet(kept, video_dir, contact_sheet)

    status = "workflow_likely" if kept or title_hits else "no_workflow_evidence"
    if kept and kept[0].get("score", 0) >= 0.55:
        status = "workflow_detected"
    manifest = {
        "status": status,
        "bvid": bvid,
        "title": info.get("title") or video_dir.name,
        "workflow_terms_in_metadata_or_text": title_hits,
        "candidate_count": len(all_candidates),
        "kept_count": len(kept),
        "comment_scan": comment_meta,
        "contact_sheet": str(contact_sheet.relative_to(video_dir))
        if contact_sheet.exists()
        else "",
        "candidates": kept,
        "conversion_target": "Blender Python node/tree construction when node workflow is readable",
        "use_policy": "Use as structured auxiliary evidence; tutorial.md and verified steps still win on conflict.",
    }
    write_json(video_dir / "workflow_manifest.json", manifest)
    write_json(video_dir / "workflow_evidence/workflow_candidates.json", all_candidates)
    print(
        json.dumps(
            {
                "video_dir": str(video_dir),
                "status": status,
                "candidates": len(all_candidates),
                "kept": len(kept),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
