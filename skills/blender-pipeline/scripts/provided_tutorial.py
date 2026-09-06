"""Source-preserving Markdown adapter; no model-invented steps or parameters."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

SCHEMA = "video2blender-provided-tutorial.v1"
DESTINATION = r"(<[^>\n]+>|(?:\\.|[^()\s\\])+)"
TITLE = r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\((?:\\.|[^)\\])*\))"""
IMAGE = re.compile(
    r"!\[([^\]\n]*)\]\(\s*" + DESTINATION + r"(?:[ \t]+" + TITLE + r")?[ \t]*\)"
)
REFERENCE = re.compile(r"!\[([^\]\n]*)\](?:\[([^\]\n]*)\])?")
DEFINITION = re.compile(
    r"^[ ]{0,3}\[([^\]\n]+)\]:[ \t]*"
    + DESTINATION
    + r"(?:[ \t]+"
    + TITLE
    + r")?[ \t]*$",
    re.M,
)
STEP = re.compile(
    r"^#{1,6}[ \t]+((?:步骤\s*|Step\s*)?\d+[.、:：)\s].*|步骤\s*\d+.*|Step\s*\d+.*)$",
    re.M | re.I,
)
ORDERED_ITEM = re.compile(r"^[0-9]{1,9}[.)][ \t]+[^\n]*$", re.M)
HEADING = re.compile(r"^#{1,6}[ \t]+[^\n]*$", re.M)
RICH_SECTION = re.compile(r"^##[ \t]+分段图文教程[ \t]*$", re.M)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def markdown_mask(text: str) -> str:
    """Preserve offsets but hide code examples from heading/image discovery."""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        hidden = fence is not None or marker is not None
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif (
                value[0] == fence[0]
                and len(value) >= len(fence)
                and not line[marker.end() :].strip()
            ):
                fence = None
        lines.append(re.sub(r"[^\n]", " ", line) if hidden else line)
    masked = "".join(lines)
    return re.sub(r"(`+)([^\n]*?)\1", lambda m: " " * len(m[0]), masked)


def reference_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def operation_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Project source operations without synthesizing headings or timestamps."""
    masked = markdown_mask(text)
    markers = list(STEP.finditer(masked))
    if markers:
        return "numbered_headings", [
            (m.start(), markers[i + 1].start() if i + 1 < len(markers) else len(text))
            for i, m in enumerate(markers)
        ]
    start, end = 0, len(text)
    rich = RICH_SECTION.search(masked)
    if rich:
        start = rich.end()
        boundary = re.compile(r"^#{1,2}[ \t]+", re.M).search(masked, start)
        if boundary:
            end = boundary.start()
    markers = [
        m
        for m in ORDERED_ITEM.finditer(masked, start, end)
        if re.sub(r"^[0-9]{1,9}[.)][ \t]+", "", text[m.start() : m.end()]).strip()
    ]
    spans = []
    for index, marker in enumerate(markers):
        stop = markers[index + 1].start() if index + 1 < len(markers) else end
        # A following window, contract or summary heading is not part of the
        # preceding numbered operation. Full context remains in tutorial.md.
        boundary = HEADING.search(masked, marker.end(), stop)
        if boundary:
            stop = boundary.start()
        spans.append((marker.start(), stop))
    return "rich_ordered_list" if rich else "top_level_ordered_list", spans


def stage(source: Path, workspace: Path, *, render_html: bool = False) -> dict:
    raw = source.read_text(encoding="utf-8-sig")
    masked = markdown_mask(raw)
    _step_format, spans = operation_spans(raw)
    if not spans:
        raise ValueError(
            "Markdown needs numbered operation headings or top-level ordered operation lists; "
            "legacy-rich lists under '## 分段图文教程' are supported."
        )
    if any(
        (workspace / name).exists()
        for name in (
            "tutorial.md",
            "tutorial_path_refs.md",
            "steps_verified.json",
            "tutorial_manifest.json",
            "provided_images",
        )
    ):
        raise ValueError(
            "Provided tutorial outputs already exist; use a new empty workspace"
        )
    if re.search(r"<img\b", masked, re.I):
        raise ValueError(
            "HTML image tags are not supported; use Markdown local/base64 images"
        )
    image_dir = workspace / "provided_images"
    records = []

    def copy_image(alt: str, raw_target: str, title: str = "") -> str:
        target = re.sub(
            r"\\([\\`*_{}\[\]()#+.!<> '\"])", r"\1", raw_target.strip().strip("<>")
        )
        if target.startswith("data:image/"):
            header, encoded = target.split(",", 1)
            if ";base64" not in header:
                raise ValueError("Only base64 data images are supported")
            content = base64.b64decode(encoded, validate=True)
            from PIL import Image
            import io

            picture = Image.open(io.BytesIO(content))
            suffix = {
                "PNG": ".png",
                "JPEG": ".jpg",
                "WEBP": ".webp",
                "GIF": ".gif",
                "TIFF": ".tif",
                "BMP": ".bmp",
            }.get(picture.format)
            if suffix is None:
                raise ValueError("Unsupported embedded image format")
            picture.verify()
        else:
            if "://" in target:
                raise ValueError(
                    "Download/authorize tutorial images yourself; Markdown images must be local or base64."
                )
            path = (source.parent / unquote(target)).resolve(strict=True)
            if not path.is_relative_to(source.parent.resolve()) or not path.is_file():
                raise ValueError(
                    "Tutorial image must be inside the tutorial's directory"
                )
            content = path.read_bytes()
            from PIL import Image
            import io

            Image.open(io.BytesIO(content)).verify()
            suffix = path.suffix.lower()
        image_dir.mkdir(exist_ok=True)
        name = hashlib.sha256(content).hexdigest() + suffix
        dest = image_dir / name
        dest.write_bytes(content)
        relative = dest.relative_to(workspace).as_posix()
        records.append({"path": relative, "sha256": sha(dest)})
        return f"![{alt}]({relative}{' ' + title if title else ''})"

    definitions = {
        reference_key(m[1]): (m[2], m[3] or "") for m in DEFINITION.finditer(masked)
    }
    replacements = []
    covered = []
    for match in IMAGE.finditer(masked):
        replacements.append(
            (match.start(), match.end(), copy_image(match[1], match[2], match[3] or ""))
        )
        covered.append((match.start(), match.end()))
    for match in REFERENCE.finditer(masked):
        if any(start <= match.start() < end for start, end in covered):
            continue
        key = reference_key(match[2] or match[1])
        if match.end() < len(masked) and masked[match.end()] == "(":
            raise ValueError(
                "Unsupported Markdown image destination; use <path with spaces> and an optional quoted title"
            )
        if key not in definitions:
            raise ValueError(
                "Unresolved Markdown image reference: " + (match[2] or match[1])
            )
        target, title = definitions[key]
        replacements.append(
            (match.start(), match.end(), copy_image(match[1], target, title))
        )
    text = raw
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    step_format, spans = operation_spans(text)
    steps = []
    for i, (start, end) in enumerate(spans):
        action = text[start:end].strip()
        step = {
            "step_id": f"S{i + 1:03d}",
            "action": action,
            "object": "",
            "parameters": {},
            "source": "user_provided_tutorial",
            "verification_status": "user_supplied_not_independently_video_verified",
        }
        # The rich merger prints the exact source interval in the list prefix.
        # Preserve that string only when present; generic lists get no invented
        # video start/end times, parameters or verification claims.
        interval = re.match(
            r"^[0-9]{1,9}[.)][ \t]+`(\d{1,3}:\d{2}(?::\d{2})?(?:\.\d+)?"
            r"[ \t]*[-–—][ \t]*\d{1,3}:\d{2}(?::\d{2})?(?:\.\d+)?)`",
            action,
        )
        if interval:
            step["time_range"] = interval[1]
        steps.append(step)
    for name in ("tutorial.md", "tutorial_path_refs.md"):
        (workspace / name).write_text(text, encoding="utf-8")
    (workspace / "steps_verified.json").write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "authority": "user_provided_instructions",
                "verification_note": "Compatibility filename; this adapter does not claim video verification.",
                "steps": steps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema": SCHEMA,
        "tutorial_method": "provided",
        "source_sha256": sha(source),
        "step_format": step_format,
        "counts": {"steps": len(steps)},
        "images": records,
        "files": [
            {"path": n, "sha256": sha(workspace / n)}
            for n in ("tutorial.md", "tutorial_path_refs.md", "steps_verified.json")
        ],
    }
    (workspace / "tutorial_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if render_html:
        import markdown

        body = markdown.markdown(text, extensions=["tables", "fenced_code"])
        (workspace / "illustrated_tutorial.html").write_text(
            "<!doctype html><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'\">"
            "<style>body{max-width:960px;margin:40px auto;font:17px/1.7 system-ui;padding:24px}img{max-width:100%}pre{overflow:auto}</style>"
            + body,
            encoding="utf-8",
        )
    return manifest


def validate_workspace(workspace: Path) -> list[str]:
    try:
        manifest = json.loads((workspace / "tutorial_manifest.json").read_text())
        issues = []
        for item in [*manifest["files"], *manifest["images"]]:
            path = (workspace / item["path"]).resolve()
            if (
                not path.is_relative_to(workspace.resolve())
                or not path.is_file()
                or sha(path) != item["sha256"]
            ):
                issues.append("Missing or changed source item: " + item["path"])
        if manifest["counts"]["steps"] < 1:
            issues.append("No ordered instructions")
        return issues
    except (ValueError, OSError, KeyError) as exc:
        return ["Invalid provided tutorial: " + str(exc)]
