#!/usr/bin/env python3
"""Extract timestamped frames, a contact sheet, and optional OCR from a local video."""
import argparse
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw


def run(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise SystemExit("ffmpeg not found; install ffmpeg or imageio-ffmpeg") from exc


def duration_seconds(ffmpeg, video):
    p = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], text=True, capture_output=True)
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    if not m:
        raise SystemExit("could not read video duration")
    return int(m[1])*3600 + int(m[2])*60 + float(m[3])


def contact_sheet(frames, dst, cols=5, thumb_width=240):
    opened = [Image.open(p).convert("RGB") for p in frames]
    ratio = thumb_width / opened[0].width
    size = (thumb_width, int(opened[0].height * ratio))
    rows = math.ceil(len(opened) / cols)
    pad, label_h = 8, 24
    sheet = Image.new("RGB", (cols*(size[0]+pad)+pad, rows*(size[1]+label_h+pad)+pad), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (p, im) in enumerate(zip(frames, opened)):
        x = pad + (i % cols)*(size[0]+pad)
        y = pad + (i // cols)*(size[1]+label_h+pad)
        sheet.paste(im.resize(size), (x, y))
        draw.text((x, y+size[1]+4), p.stem, fill="black")
    sheet.save(dst, quality=88)
    for im in opened:
        im.close()


def frame_at(video, output, timestamp, ffmpeg=None):
    """Decode the requested timestamp rather than guessing an fps-filter offset."""
    ffmpeg = ffmpeg or find_ffmpeg()
    output.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=1920:1920:force_original_aspect_ratio=decrease",
         "-q:v", "2", str(output)])
    if not output.is_file():
        raise ValueError(f"no frame at {timestamp:.3f}s")
    return {"path": f"frames/{output.name}", "timestamp": timestamp,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def prepare(video, output, interval=2.0, ocr=False):
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be a finite positive number")
    output.mkdir(parents=True, exist_ok=True)
    frames_dir = output / "frames"
    frames_dir.mkdir(exist_ok=True)
    ffmpeg = find_ffmpeg()
    duration = duration_seconds(ffmpeg, video)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("video has no positive duration")
    times = sorted({round(min(i * interval, max(0.0, duration - .05)), 3)
                    for i in range(math.ceil(duration / interval))}
                   | {round(max(0.0, duration - .1), 3)})
    entries = []
    for timestamp in times:
        name = f"frame_{round(timestamp * 1000):09d}.jpg"
        path = frames_dir / name
        if path.is_file():
            entry = {"path": f"frames/{name}", "timestamp": timestamp,
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            entry = frame_at(video, path, timestamp, ffmpeg)
        entries.append(entry)
    sheets = []
    for start in range(0, len(entries), 24):
        name = f"contact-sheet-{start // 24:03d}.jpg"
        contact_sheet([output / item["path"] for item in entries[start:start + 24]],
                      output / name, cols=4, thumb_width=360)
        sheets.append(name)
    meta = {"duration_seconds": duration, "interval_seconds": interval,
            "frame_count": len(entries), "frames": entries, "contact_sheets": sheets}
    (output / "analysis.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if ocr:
        tess = shutil.which("tesseract")
        if not tess:
            raise ValueError("--ocr requested but tesseract is not installed")
        lines = []
        for entry in entries:
            text = run([tess, str(output / entry["path"]), "stdout", "-l", "chi_sim+eng", "--psm", "6"]).stdout
            lines.append(f"{entry['timestamp']:.3f}\t{Path(entry['path']).name}\t{' '.join(text.split())}")
        (output / "ocr.tsv").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--ocr", action="store_true")
    args = ap.parse_args()
    meta = prepare(args.video, args.output, args.interval, args.ocr)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
