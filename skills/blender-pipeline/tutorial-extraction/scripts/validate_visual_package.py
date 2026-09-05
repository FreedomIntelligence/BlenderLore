#!/usr/bin/env python3
"""Validate a Markdown tutorial package, evidence ledger, and JSON rubric."""

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath


TAGS = ("GEO", "PROC", "SURF", "SCN", "RIG", "ANM", "SIM", "PIPE")
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def fail(message):
    raise SystemExit(f"FAIL: {message}")


def content_entries(directory):
    """Ignore filesystem metadata created by macOS on removable drives."""
    return [item for item in directory.iterdir()
            if item.name != ".DS_Store" and not item.name.startswith("._")]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_ledger(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    duration = float(data["source"]["duration_seconds"])
    steps = data.get("steps", [])
    if not steps:
        fail("ledger has no steps")
    numbers = [step["number"] for step in steps]
    if numbers != sorted(set(numbers)):
        fail("step numbers must be unique and ascending")
    base = path.parent
    frames = []
    for step in steps:
        start, end = float(step["time_start"]), float(step["time_end"])
        if not 0 <= start <= end <= duration:
            fail(f"invalid time range in step {step['number']}")
        frame = (base / step["evidence_frame"]).resolve()
        if not frame.is_file():
            fail(f"missing evidence frame for step {step['number']}")
        frames.append(frame)
        for claim in step.get("claims", []):
            if claim.get("status") in {"shown", "inferred"}:
                at = claim.get("at")
                if at is None or not start <= float(at) <= end:
                    fail(f"claim timestamp outside step {step['number']}")
    return data, frames


def validate_markdown(path, image_dir, allowed_hashes=None, cover_frame=None):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        fail(f"tutorial is not UTF-8: {exc}")
    if not re.search(r"(?m)^#\s+\S", text):
        fail("tutorial has no H1 title")
    for marker in ("You will need", "4.1", "5.1.2"):
        if marker not in text:
            fail(f"tutorial missing required marker: {marker}")
    for term in ("check id", "verifier", "评分标准", "rubric"):
        if term.lower() in text.lower():
            fail(f"tutorial exposes grading detail: {term}")

    refs = IMAGE_PATTERN.findall(text)
    if not refs:
        fail("tutorial contains no Markdown images")
    normalized = []
    for ref in refs:
        if any(char in ref for char in ("<", ">")) or " " in ref:
            fail(f"image path must be a simple relative path: {ref}")
        pure = PurePosixPath(ref)
        if pure.is_absolute() or pure.parts[:1] != ("image",) or ".." in pure.parts:
            fail(f"image path must stay inside output/image: {ref}")
        target = path.parent.joinpath(*pure.parts).resolve()
        if not target.is_file() or target.parent != image_dir.resolve():
            fail(f"missing or nested tutorial image: {ref}")
        normalized.append(ref)
        if allowed_hashes is not None and digest(target) not in allowed_hashes:
            fail(f"tutorial image is not backed by source evidence: {ref}")

    if len(normalized) != len(set(normalized)):
        fail("tutorial references the same image more than once")
    stored = {
        item.name for item in content_entries(image_dir) if item.is_file()
    }
    referenced = {PurePosixPath(ref).name for ref in normalized}
    if stored != referenced:
        fail(f"image directory and Markdown references differ: stored={sorted(stored)}, referenced={sorted(referenced)}")
    if any(item.is_dir() for item in content_entries(image_dir)):
        fail("output/image must not contain subdirectories")

    if cover_frame and digest(path.parent / normalized[0]) != digest(cover_frame):
        fail("first tutorial image is not the supplied source-video frame")
    return text, len(stored)


def validate_rubric(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid UTF-8 rubric JSON: {exc}")
    rows = data.get("rubric")
    if not isinstance(rows, list) or not rows:
        fail("rubric JSON has no scored rows")
    ids, points, totals = [], 0, {}
    for row in rows:
        check_id = row.get("id", "")
        match = re.fullmatch(r"(GEO|PROC|SURF|SCN|RIG|ANM|SIM|PIPE)-\d{2}", check_id)
        if not match:
            fail(f"invalid rubric check ID: {check_id}")
        capability = row.get("capability")
        if capability != match.group(1) or capability not in TAGS:
            fail(f"capability mismatch for {check_id}")
        if row.get("artifact_verifier_check") != check_id:
            fail(f"verifier mapping mismatch for {check_id}")
        value = row.get("points")
        if not isinstance(value, int) or value <= 0:
            fail(f"invalid points for {check_id}")
        for field in ("criterion", "scoring_rule"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                fail(f"missing {field} for {check_id}")
        ids.append(check_id)
        points += value
        totals[capability] = totals.get(capability, 0) + value
    if len(ids) != len(set(ids)):
        fail("rubric check IDs are not unique")
    if points != 100 or data.get("total_points") != 100:
        fail(f"rubric totals {points}, expected 100")
    if data.get("capability_totals") != totals:
        fail("capability totals do not match scored rows")
    if data.get("status_weights") != {"PASS": 1.0, "PARTIAL": 0.5, "FAIL": 0.0}:
        fail("unexpected status weights")
    verifier = data.get("artifact_verifier", {})
    if verifier.get("engine") != "blender_python":
        fail("rubric has no embedded Blender artifact verifier")
    source = verifier.get("python_source")
    if not isinstance(source, str) or not source.strip():
        fail("embedded artifact verifier source is empty")
    try:
        compile(source, "<embedded-artifact-verifier>", "exec")
    except SyntaxError as exc:
        fail(f"embedded artifact verifier does not compile: {exc}")
    for check_id in ids:
        if check_id not in source:
            fail(f"rubric check has no verifier implementation: {check_id}")
    source_ids = set(re.findall(r"(?:GEO|PROC|SURF|SCN|RIG|ANM|SIM|PIPE)-\d{2}", source))
    if source_ids != set(ids):
        fail(f"verifier checks {sorted(source_ids)} do not match rubric checks {sorted(ids)}")
    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tutorial-dir", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--cover-frame", type=Path)
    args = parser.parse_args()

    package = args.tutorial_dir.resolve()
    input_dir, output_dir = package / "input", package / "output"
    image_dir = output_dir / "image"
    for directory in (input_dir, output_dir, image_dir):
        if not directory.is_dir():
            fail(f"missing directory: {directory}")

    top_level = sorted(content_entries(output_dir))
    files = [item for item in top_level if item.is_file()]
    directories = [item for item in top_level if item.is_dir()]
    tutorials = [item for item in files if item.suffix.lower() == ".md"]
    rubrics = [item for item in files if item.suffix.lower() == ".json"]
    if len(files) != 2 or len(tutorials) != 1 or len(rubrics) != 1 or directories != [image_dir]:
        fail("output must contain one tutorial .md, one rubric .json, and one image directory")

    ledger_data, evidence_frames = validate_ledger(args.ledger.resolve()) if args.ledger else (None, [])
    cover = args.cover_frame.resolve() if args.cover_frame else None
    allowed_hashes = {digest(item) for item in evidence_frames}
    if cover:
        allowed_hashes.add(digest(cover))
    text, image_count = validate_markdown(
        tutorials[0], image_dir,
        allowed_hashes=allowed_hashes if (evidence_frames or cover) else None,
        cover_frame=cover,
    )
    checks = validate_rubric(rubrics[0])
    if ledger_data:
        source_id = str(ledger_data["source"].get("id", ""))
        if source_id and source_id not in text:
            fail("source ID from ledger is missing from tutorial")
    learner_assets = len(content_entries(input_dir))
    print(
        f"PASS: {learner_assets} learner input assets, 2 top-level output files, "
        f"{image_count} tutorial images, {checks} verifier-mapped checks, 100 points"
    )


if __name__ == "__main__":
    main()
