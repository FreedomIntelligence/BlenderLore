#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from project_paths import VIDEO_ROOT

RESAMPLE_LANCZOS = getattr(
    getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
)


def draw_safe_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fill: tuple[int, int, int],
) -> None:
    try:
        draw.text(xy, text, fill=fill)
    except UnicodeEncodeError:
        draw.text(xy, text.encode("latin-1", "replace").decode("latin-1"), fill=fill)


def run(cmd: list[str], timeout: int = 300) -> None:
    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )


def extract_frame(source: Path, timestamp: float, output: Path) -> tuple[bool, str]:
    """Extract one evidence frame, tolerating concat-boundary decode failures."""
    attempts = [
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
    ]
    for delta in (-0.5, 0.5, -1.0, 1.0):
        ts = max(0.0, timestamp + delta)
        attempts.append(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{ts:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=1280:-1",
                str(output),
            ]
        )
    last_error = ""
    for cmd in attempts:
        try:
            run(cmd, timeout=120)
            if output.exists() and output.stat().st_size > 0:
                return True, ""
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = repr(exc)
            output.unlink(missing_ok=True)
    return False, last_error


def ffprobe_duration(video: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        text=True,
    ).strip()
    return float(out)


def load_series_sources(video_dir: Path, fallback_source: Path) -> list[dict[str, Any]]:
    manifest = video_dir / "series_parts_manifest.json"
    if not manifest.exists():
        duration = ffprobe_duration(fallback_source)
        return [
            {
                "path": fallback_source,
                "start_sec": 0.0,
                "duration": duration,
                "end_sec": duration,
            }
        ]
    data = json.loads(manifest.read_text(encoding="utf-8", errors="ignore"))
    raw_parts = data.get("parts") if isinstance(data, dict) else data
    if not isinstance(raw_parts, list) or not raw_parts:
        raise RuntimeError(f"bad series parts manifest: {manifest}")
    sources: list[dict[str, Any]] = []
    cursor = 0.0
    for raw in raw_parts:
        item = raw if isinstance(raw, dict) else {"path": raw}
        path = Path(str(item["path"]))
        if not path.is_absolute():
            path = video_dir / path
        duration = float(item.get("duration") or ffprobe_duration(path))
        start = float(item.get("start_sec", cursor))
        sources.append(
            {
                "path": path,
                "start_sec": start,
                "duration": duration,
                "end_sec": start + duration,
            }
        )
        cursor = start + duration
    return sources


def total_duration(sources: list[dict[str, Any]]) -> float:
    return max(float(item["end_sec"]) for item in sources) if sources else 0.0


def map_series_timestamp(
    sources: list[dict[str, Any]], timestamp: float
) -> tuple[Path, float] | None:
    for item in sources:
        if float(item["start_sec"]) <= timestamp < float(item["end_sec"]):
            return Path(item["path"]), max(0.0, timestamp - float(item["start_sec"]))
    if sources and timestamp >= float(sources[-1]["end_sec"]):
        item = sources[-1]
        return Path(item["path"]), max(0.0, float(item["duration"]) - 0.1)
    return None


def extract_series_frame(
    sources: list[dict[str, Any]], timestamp: float, output: Path
) -> tuple[bool, str]:
    mapped = map_series_timestamp(sources, timestamp)
    if not mapped:
        return False, "timestamp outside series sources"
    source, local_ts = mapped
    return extract_frame(source, local_ts, output)


def load_segments(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def find_segments(video_dir: Path, bvid: str) -> Path | None:
    candidates = [
        video_dir / "segments.jsonl",
        video_dir / "transcript" / "segments.jsonl",
        video_dir / "transcripts" / "segments.jsonl",
    ]
    configured_roots = [
        Path(value).expanduser()
        for value in os.environ.get("BLENDER_TRANSCRIPT_ROOTS", "").split(os.pathsep)
        if value.strip()
    ]
    transcript_roots = configured_roots or [VIDEO_ROOT / "transcripts"]
    search_bases: list[Path] = []
    for root in transcript_roots:
        for base in (root, root / "Bilibili", root / "YouTube"):
            if base not in search_bases:
                search_bases.append(base)
            candidates.append(base / bvid / "segments.jsonl")
    for path in candidates:
        if path.exists():
            return path
    for base in search_bases:
        if not base.exists():
            continue
        for path in base.glob(f"*{bvid}*/segments.jsonl"):
            if path.exists():
                return path
    return None


def make_sheet(frames: list[Path], output: Path, title: str) -> None:
    thumbs = []
    for frame in frames:
        img = ImageOps.exif_transpose(Image.open(frame)).convert("RGB")
        img.thumbnail((360, 210), RESAMPLE_LANCZOS)
        canvas = Image.new("RGB", (360, 240), "white")
        canvas.paste(img, ((360 - img.width) // 2, 24))
        draw = ImageDraw.Draw(canvas)
        draw_safe_text(draw, (8, 6), frame.stem, fill=(0, 0, 0))
        thumbs.append(canvas)
    cols = 3
    rows = max(1, (len(thumbs) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * 360, rows * 240 + 34), "white")
    draw_safe_text(ImageDraw.Draw(sheet), (10, 8), title, fill=(0, 0, 0))
    for i, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((i % cols) * 360, 34 + (i // cols) * 240))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=90)


def run_ocr(frame: Path) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    img = ImageOps.exif_transpose(Image.open(frame)).convert("RGB")
    w, h = img.size
    crops = {
        "right_ui": img.crop((int(w * 0.55), 0, w, h)),
        "bottom": img.crop((0, int(h * 0.70), w, h)),
    }
    texts = []
    for name, crop in crops.items():
        tmp = frame.with_name(f"{frame.stem}_{name}_ocr.png")
        crop = ImageOps.autocontrast(ImageOps.grayscale(crop))
        crop = crop.resize((crop.width * 2, crop.height * 2), RESAMPLE_LANCZOS)
        crop.save(tmp)
        try:
            proc = subprocess.run(
                [exe, str(tmp), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=12,
            )
            texts.append(" ".join(proc.stdout.split()))
        except subprocess.TimeoutExpired:
            continue
        finally:
            tmp.unlink(missing_ok=True)
    return " ".join(t for t in texts if t).strip()


def select_evenly(values: list[float], limit: int) -> list[tuple[int, float]]:
    """Keep complete, evenly spaced evidence windows before costly extraction."""

    indexed = list(enumerate(values))
    if limit <= 0 or len(indexed) <= limit:
        return indexed
    if limit == 1:
        return [indexed[0]]
    last = len(indexed) - 1
    selected: list[tuple[int, float]] = []
    seen: set[int] = set()
    for slot in range(limit):
        index = round(slot * last / (limit - 1))
        if index not in seen:
            selected.append(indexed[index])
            seen.add(index)
    return selected


def merged_source_info(
    existing: dict[str, Any],
    *,
    title: str,
    url: str,
    bvid: str,
    source: Path,
    target_reference: Path,
) -> dict[str, Any]:
    """Add evidence paths without erasing workload identity or linked assets."""

    return {
        **existing,
        "title": title,
        "webpage_url": url,
        "original_url": url,
        "bvid": bvid,
        "source_video_path": str(source),
        "target_reference_path": str(target_reference),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--target-reference", required=True, type=Path)
    parser.add_argument("--frame-step", type=float, default=5.0)
    parser.add_argument("--window-sec", type=float, default=60.0)
    parser.add_argument("--max-windows", type=int, default=0)
    args = parser.parse_args()

    video_dir = args.video_dir
    video_dir.mkdir(parents=True, exist_ok=True)
    source_link = video_dir / "source.mp4"
    if args.source.absolute() != source_link.absolute():
        if source_link.exists() or source_link.is_symlink():
            source_link.unlink()
        source_link.symlink_to(args.source)
    if args.target_reference.exists():
        dst = video_dir / "target_reference.png"
        if args.target_reference.absolute() != dst.absolute():
            shutil.copy2(args.target_reference, dst)
        # Keep the legacy filename for compatibility, but it is no longer an
        # authoritative target. build_pipeline_specs.py validates it later and
        # records whether it is safe to show the model.
        legacy = video_dir / "final_reference.png"
        if args.target_reference.absolute() != legacy.absolute():
            shutil.copy2(args.target_reference, legacy)
    source_info_path = video_dir / "source.info.json"
    try:
        existing_source_info = json.loads(source_info_path.read_text(encoding="utf-8"))
        if not isinstance(existing_source_info, dict):
            existing_source_info = {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        existing_source_info = {}
    source_info_path.write_text(
        json.dumps(
            merged_source_info(
                existing_source_info,
                title=args.title,
                url=args.url,
                bvid=args.bvid,
                source=args.source,
                target_reference=args.target_reference,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    sources = load_series_sources(video_dir, args.source)
    duration = total_duration(sources)
    evidence = video_dir / "rich_evidence"
    frames_dir = evidence / "frames_5s"
    frames_dir.mkdir(parents=True, exist_ok=True)
    all_window_starts = list(frange(0, duration, args.window_sec))
    selected_windows = select_evenly(all_window_starts, args.max_windows)
    timestamps = sorted(
        {
            round(timestamp, 3)
            for _window_index, start in selected_windows
            for timestamp in frange(
                start,
                min(duration, start + args.window_sec),
                args.frame_step,
            )
        }
    )
    frame_index: list[dict[str, Any]] = []
    frame_errors = []
    for idx, ts in enumerate(timestamps):
        out = frames_dir / f"t_{int(ts):05d}s.jpg"
        if not out.exists():
            ok, err = extract_series_frame(sources, ts, out)
            if not ok:
                frame_errors.append(
                    {
                        "timestamp_sec": ts,
                        "path": str(out.relative_to(video_dir)),
                        "error": err,
                    }
                )
                continue
        if out.exists() and out.stat().st_size > 0:
            frame_index.append(
                {"timestamp_sec": ts, "path": str(out.relative_to(video_dir))}
            )

    seg_path = find_segments(video_dir, args.bvid)
    segments = load_segments(seg_path) if seg_path else []
    windows = []
    ocr_lines = []
    for original_window_index, start in selected_windows:
        end = min(duration, start + args.window_sec)
        window_frames = [
            frames_dir / f"t_{int(x['timestamp_sec']):05d}s.jpg"
            for x in frame_index
            if start <= x["timestamp_sec"] < end
        ]
        sheet = evidence / "windows" / f"w_{int(start):05d}_{int(end):05d}.jpg"
        make_sheet(window_frames, sheet, f"{args.title} | {int(start)}-{int(end)}s")
        asr_text = "\n".join(
            s["text"]
            for s in segments
            if float(s.get("start_sec", 0)) < end
            and float(s.get("end_sec", 0)) >= start
        )
        ocr_samples = []
        for frame in window_frames[::2][:6]:
            text = run_ocr(frame)
            if text:
                item = {
                    "timestamp_sec": int(frame.stem.split("_")[1][:-1]),
                    "text": text,
                }
                ocr_samples.append(item)
                ocr_lines.append({"window_start": start, **item})
        windows.append(
            {
                "start_sec": start,
                "end_sec": end,
                "original_window_index": original_window_index,
                "sheet": str(sheet.relative_to(video_dir)),
                "asr_text": asr_text,
                "ocr_samples": ocr_samples,
            }
        )

    (evidence / "frame_index.json").write_text(
        json.dumps(frame_index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (evidence / "windows.json").write_text(
        json.dumps(windows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (video_dir / "tutorial_window_budget.json").write_text(
        json.dumps(
            {
                "original_windows": len(all_window_starts),
                "selected_windows": len(selected_windows),
                "max_windows": args.max_windows,
                "strategy": "even_complete_windows_before_extraction_v1",
                "original_window_indices": [
                    index for index, _start in selected_windows
                ],
                "frame_step_sec": args.frame_step,
                "extracted_frame_budget": len(timestamps),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if frame_errors:
        with (evidence / "frame_errors.jsonl").open("w", encoding="utf-8") as f:
            for row in frame_errors:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (evidence / "ocr_samples.jsonl").open("w", encoding="utf-8") as f:
        for row in ocr_lines:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "video_dir": str(video_dir),
                "duration": duration,
                "sources": len(sources),
                "frames": len(frame_index),
                "frame_errors": len(frame_errors),
                "windows": len(windows),
                "original_windows": len(all_window_starts),
                "asr_segments": len(segments),
                "segments_path": str(seg_path) if seg_path else "",
                "ocr_samples": len(ocr_lines),
            },
            ensure_ascii=False,
        )
    )
    return 0


def frange(start: float, stop: float, step: float):
    x = start
    while x < stop:
        yield x
        x += step


if __name__ == "__main__":
    raise SystemExit(main())
