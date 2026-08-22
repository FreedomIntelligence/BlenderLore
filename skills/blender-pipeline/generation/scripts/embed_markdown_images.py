#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import re
import shutil
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

RESAMPLE_LANCZOS = getattr(
    getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
)


IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")


def image_data_url(path: Path, max_side: int, quality: int) -> str:
    img = ImageOps.exif_transpose(Image.open(path))
    has_alpha = img.mode in {"RGBA", "LA"} or ("transparency" in img.info)
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), RESAMPLE_LANCZOS)
    buf = BytesIO()
    if has_alpha:
        img = img.convert("RGBA")
        img.save(buf, "PNG", optimize=True)
        mime = "image/png"
    else:
        img = img.convert("RGB")
        img.save(buf, "JPEG", quality=quality, optimize=True)
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def resolve_image(md_path: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    if target.startswith(("data:", "http://", "https://")):
        return None
    if " " in target and not Path(target).exists():
        target = target.split()[0]
    target = target.strip("<>").strip()
    path = Path(target)
    if not path.is_absolute():
        path = md_path.parent / path
    return path if path.exists() and path.is_file() else None


def embed_markdown(
    source: Path, dest: Path, fallback: Path, max_side: int, quality: int
) -> dict[str, int]:
    text = source.read_text(encoding="utf-8", errors="ignore")
    if not fallback.exists() or fallback.resolve() == source.resolve():
        fallback.write_text(text, encoding="utf-8")
    count = 0
    missing = 0
    skipped = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count, missing, skipped
        alt, target = match.group(1), match.group(2)
        path = resolve_image(source, target)
        if path is None:
            skipped += 1
            return match.group(0)
        try:
            url = image_data_url(path, max_side=max_side, quality=quality)
        except Exception:
            missing += 1
            return match.group(0)
        count += 1
        return f"![{alt}]({url})"

    embedded = IMAGE_RE.sub(repl, text)
    dest.write_text(embedded, encoding="utf-8")
    return {"embedded": count, "missing": missing, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", type=Path, required=True)
    parser.add_argument("--source-name", default="tutorial.md")
    parser.add_argument("--dest-name", default="tutorial.md")
    parser.add_argument("--fallback-name", default="tutorial_path_refs.md")
    parser.add_argument("--max-side", type=int, default=1600)
    parser.add_argument("--quality", type=int, default=86)
    args = parser.parse_args()

    video_dir = args.video_dir
    source = video_dir / args.source_name
    dest = video_dir / args.dest_name
    fallback = video_dir / args.fallback_name
    if not source.exists():
        raise FileNotFoundError(source)
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
        source = dest
    stats = embed_markdown(source, dest, fallback, args.max_side, args.quality)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
