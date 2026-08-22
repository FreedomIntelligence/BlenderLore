#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageStat

from blender_version_registry import extract_versions_from_text

RESAMPLE_LANCZOS = getattr(
    getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
)

UI_TERMS = [
    "blender",
    "layout",
    "modeling",
    "sculpting",
    "uv editing",
    "texture paint",
    "shading",
    "animation",
    "rendering",
    "geometry nodes",
    "scripting",
    "object mode",
    "edit mode",
    "布局",
    "建模",
    "雕刻",
    "材质",
    "渲染",
    "动画",
    "物体模式",
    "编辑模式",
]


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


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
        timeout=30,
    )
    try:
        return max(0.0, float(proc.stdout.strip()))
    except Exception:
        return 0.0


def frange(start: float, stop: float, step: float):
    x = start
    while x <= stop:
        yield round(x, 3)
        x += step


def capture_top_left(source: Path, timestamp: float, output: Path) -> bool:
    vf = "scale=1280:-1,crop=iw*0.58:ih*0.18:0:0"
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
            vf,
            str(output),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    return proc.returncode == 0 and output.exists() and output.stat().st_size > 1024


def visual_text_likelihood(path: Path) -> float:
    try:
        img = Image.open(path).convert("L").resize((320, 90), RESAMPLE_LANCZOS)
    except Exception:
        return 0.0
    stat = ImageStat.Stat(img)
    std = stat.stddev[0]
    if std < 4:
        return 0.0
    edges = 0
    px = img.load()
    for y in range(1, img.height - 1, 2):
        for x in range(1, img.width - 1, 2):
            if abs(px[x, y] - px[x + 1, y]) + abs(px[x, y] - px[x, y + 1]) > 38:
                edges += 1
    return min(1.0, edges / 900.0)


def run_ocr(path: Path) -> str:
    exe = shutil.which("tesseract")
    if not exe or not path.exists():
        return ""
    try:
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    except Exception:
        return ""
    crops = {
        "top_bar": img.crop((0, 0, img.width, max(24, int(img.height * 0.45)))),
        "top_left": img,
    }
    texts: list[str] = []
    for name, crop in crops.items():
        tmp = path.with_name(f"{path.stem}_{name}_version_ocr.png")
        prep = ImageOps.autocontrast(ImageOps.grayscale(crop))
        prep = prep.resize((prep.width * 3, prep.height * 3), RESAMPLE_LANCZOS)
        prep.save(tmp)
        psm = "7" if name == "top_bar" else "6"
        try:
            proc = subprocess.run(
                [exe, str(tmp), "stdout", "-l", "eng+chi_sim", "--psm", psm],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
            text = " ".join(proc.stdout.split())
            if text:
                texts.append(text)
        except subprocess.TimeoutExpired:
            pass
        tmp.unlink(missing_ok=True)
    return " ".join(texts).strip()


def ui_score(text: str) -> int:
    low = text.lower()
    return sum(1 for term in UI_TERMS if term.lower() in low)


def unique_times(times: list[float], duration: float) -> list[float]:
    seen: set[int] = set()
    out: list[float] = []
    for ts in times:
        ts = max(0.0, min(duration, round(ts, 3)))
        key = int(ts * 10)
        if key in seen:
            continue
        seen.add(key)
        out.append(ts)
    return sorted(out)


def scan_times(
    source: Path, times: list[float], tmpdir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    detections: list[dict[str, Any]] = []
    for idx, ts in enumerate(times):
        frame = tmpdir / f"version_{idx:05d}_{int(ts * 1000):09d}.jpg"
        if not capture_top_left(source, ts, frame):
            continue
        visual = visual_text_likelihood(frame)
        if visual < 0.03:
            continue
        text = run_ocr(frame)
        versions = extract_versions_from_text(text)
        row = {
            "timestamp_sec": ts,
            "visual_text_likelihood": round(visual, 4),
            "ui_score": ui_score(text),
            "ocr_text": text[:600],
            "versions": versions,
        }
        rows.append(row)
        for item in versions:
            detections.append({**item, "timestamp_sec": ts, "ocr_text": text[:600]})
    return rows, detections


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--coarse-step", type=float, default=8.0)
    parser.add_argument("--dense-step", type=float, default=1.5)
    parser.add_argument("--dense-window", type=float, default=4.0)
    parser.add_argument("--max-dense-centers", type=int, default=2)
    parser.add_argument("--max-coarse-frames", type=int, default=24)
    args = parser.parse_args()

    video_dir = args.video_dir
    info = load_json(video_dir / "source.info.json") or {}
    source = args.source or Path(
        info.get("source_video_path") or video_dir / "source.mp4"
    )
    if not source.exists():
        source = video_dir / "source.mp4"
    evidence = video_dir / "rich_evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    output = evidence / "blender_version_ocr.json"
    samples_path = evidence / "blender_version_ocr_samples.jsonl"
    if (
        not source.exists()
        or not shutil.which("ffmpeg")
        or not shutil.which("ffprobe")
        or not shutil.which("tesseract")
    ):
        result = {
            "status": "skipped",
            "reason": "missing_source_or_ffmpeg_or_tesseract",
            "source": str(source),
            "detections": [],
            "confirmed_version": "",
            "confidence": "none",
        }
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0

    duration = ffprobe_duration(source)
    if duration <= 0:
        result = {
            "status": "skipped",
            "reason": "duration_unavailable",
            "source": str(source),
            "detections": [],
            "confirmed_version": "",
            "confidence": "none",
        }
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0

    coarse_step = max(1.0, args.coarse_step)
    if args.max_coarse_frames > 0:
        coarse_step = max(coarse_step, duration / float(args.max_coarse_frames))
    coarse_times = list(frange(0, duration, coarse_step))
    third_centers = [duration / 3.0, duration / 2.0, duration * 2.0 / 3.0]
    all_rows: list[dict[str, Any]] = []
    all_detections: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="version_ocr_") as tmp:
        tmpdir = Path(tmp)
        coarse_rows, coarse_detections = scan_times(source, coarse_times, tmpdir)
        all_rows.extend(coarse_rows)
        all_detections.extend(coarse_detections)
        ui_rows = sorted(
            [r for r in coarse_rows if int(r.get("ui_score", 0)) > 0],
            key=lambda r: (
                int(r.get("ui_score", 0)),
                float(r.get("visual_text_likelihood", 0)),
            ),
            reverse=True,
        )
        centers = [float(r["timestamp_sec"]) for r in ui_rows[: args.max_dense_centers]]
        if not all_detections:
            centers.extend(third_centers)
        dense_times: list[float] = []
        for center in centers:
            dense_times.extend(
                list(
                    frange(
                        center - args.dense_window,
                        center + args.dense_window,
                        max(0.25, args.dense_step),
                    )
                )
            )
        dense_times = unique_times(dense_times, duration)
        dense_rows, dense_detections = scan_times(source, dense_times, tmpdir)
        all_rows.extend(dense_rows)
        all_detections.extend(dense_detections)

    counts: dict[str, int] = {}
    by_version: dict[str, list[dict[str, Any]]] = {}
    for det in all_detections:
        version = det.get("version") or ""
        if not version:
            continue
        counts[version] = counts.get(version, 0) + 1
        by_version.setdefault(version, []).append(det)
    confirmed = ""
    confidence = "none"
    if counts:
        confirmed = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        confidence = "strong" if counts[confirmed] >= 2 else "weak_evidence"
    samples_path.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in all_rows
            if row.get("ocr_text")
        )
        + ("\n" if all_rows else ""),
        encoding="utf-8",
    )
    result = {
        "status": "done",
        "source": str(source),
        "duration_sec": duration,
        "coarse_step_sec": args.coarse_step,
        "dense_step_sec": args.dense_step,
        "detections": all_detections,
        "version_counts": counts,
        "confirmed_version": confirmed,
        "confidence": confidence,
        "samples_jsonl": str(samples_path.relative_to(video_dir)),
    }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
