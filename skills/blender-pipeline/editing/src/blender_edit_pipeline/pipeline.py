"""CLI orchestration for atomic, reopen-verified Blender edits."""

from __future__ import annotations

if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any, Iterator, Mapping

from blender_edit_pipeline.contracts import (
    ContractError,
    atomic_write_json,
    digest_json,
    load_request,
    sha256_file,
)
from blender_edit_pipeline.operators import apply_edit
from blender_edit_pipeline.qa import (
    QualityError,
    difference_paths,
    image_difference,
    require_no_drift,
    require_target_change,
    require_visual_change,
    snapshot_scene,
    validate_non_target_drift,
)
from blender_edit_pipeline.source_preflight import PreflightError, audit_current_scene


class PipelineError(RuntimeError):
    """Raised when publication or reopen verification fails."""


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bpy() -> Any:
    try:
        import bpy  # type: ignore[import-not-found]

        return bpy
    except ImportError as exc:
        raise PipelineError("this command must run inside Blender") from exc


def _blender_arguments(argv: list[str]) -> list[str]:
    return argv[argv.index("--") + 1 :] if "--" in argv else argv[1:]


@contextmanager
def _controlled_render_settings(bpy: Any, resolution: int) -> Iterator[None]:
    scene = bpy.context.scene
    render, image = scene.render, scene.render.image_settings
    old = {
        "filepath": render.filepath,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "file_format": image.file_format,
        "color_mode": image.color_mode,
        "color_depth": image.color_depth,
        "film_transparent": render.film_transparent,
    }
    try:
        render.resolution_x = render.resolution_y = resolution
        render.resolution_percentage = 100
        image.file_format, image.color_mode, image.color_depth = "PNG", "RGBA", "8"
        yield
    finally:
        for key, value in old.items():
            if key in {"file_format", "color_mode", "color_depth"}:
                setattr(image, key, value)
            else:
                setattr(render, key, value)


def _render(bpy: Any, path: Path, resolution: int) -> None:
    if bpy.context.scene.camera is None:
        raise QualityError("visual evidence requires an active scene camera")
    with _controlled_render_settings(bpy, resolution):
        bpy.context.scene.render.filepath = str(path)
        result = bpy.ops.render.render(write_still=True)
        if "FINISHED" not in result or not path.is_file():
            raise QualityError("Blender did not produce the requested render evidence")


def _publish(stage: Path, destination: Path, overwrite: bool) -> None:
    if destination.exists() and not overwrite:
        raise PipelineError(f"output directory already exists: {destination}")
    backup = destination.with_name(f".{destination.name}.backup.{uuid.uuid4().hex}")
    replaced = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            replaced = True
        os.replace(stage, destination)
        if replaced:
            shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        if replaced and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise


def _artifact_record(stage: Path, names: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "path": name,
            "sha256": sha256_file(stage / name),
            "bytes": (stage / name).stat().st_size,
        }
        for name in names
    ]


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise PipelineError(f"{label} must not be a symlink")
    try:
        value = json.loads(requested.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{label} is missing or invalid JSON") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{label} must be a JSON object")
    return value


def _artifact_index(
    root: Path, records: Any, required: set[str]
) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise PipelineError("review package artifact inventory is invalid")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
            raise PipelineError("review package contains an invalid artifact binding")
        name = record.get("path")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in result
        ):
            raise PipelineError("review artifact path must be a unique basename")
        candidate = root / name
        if candidate.is_symlink() or not candidate.is_file():
            raise PipelineError(f"review artifact is missing or not regular: {name}")
        if sha256_file(candidate) != record.get(
            "sha256"
        ) or candidate.stat().st_size != record.get("bytes"):
            raise PipelineError(f"review artifact binding drifted: {name}")
        result[name] = dict(record)
    if set(result) != required:
        raise PipelineError(
            "review package artifact inventory is incomplete or excessive"
        )
    return result


def validate_assessment(
    value: Any,
    *,
    review_sha256: str,
    artifacts: Mapping[str, Mapping[str, Any]],
    minimum_after_score: float,
) -> dict[str, Any]:
    """Validate an externally authored human/VLM assessment and all evidence bindings."""
    if not isinstance(value, dict):
        raise QualityError("assessment must be a JSON object")
    required = {
        "schema",
        "decision",
        "review_package_sha256",
        "bindings",
        "assessor",
        "scores",
        "summary",
    }
    if set(value) != required or value.get("schema") != "blender-edit-assessment/v1":
        raise QualityError("assessment fields or schema are invalid")
    if value.get("decision") != "accepted":
        raise QualityError("external assessment did not accept the candidate")
    if value.get("review_package_sha256") != review_sha256:
        raise QualityError("assessment review-package SHA-256 binding drifted")
    bindings = value.get("bindings")
    expected_bindings = {
        "candidate_sha256": artifacts["candidate.blend"]["sha256"],
        "before_sha256": artifacts["before.png"]["sha256"],
        "after_sha256": artifacts["after.png"]["sha256"],
    }
    if not isinstance(bindings, dict) or bindings != expected_bindings:
        raise QualityError("assessment candidate/image SHA-256 bindings drifted")
    assessor = value.get("assessor")
    if (
        not isinstance(assessor, dict)
        or set(assessor) != {"kind", "name"}
        or assessor.get("kind") not in {"human", "vlm"}
        or not isinstance(assessor.get("name"), str)
        or not assessor["name"].strip()
        or len(assessor["name"]) > 256
    ):
        raise QualityError("assessment requires an identified human or VLM assessor")
    scores = value.get("scores")
    if not isinstance(scores, dict) or set(scores) != {"before", "after"}:
        raise QualityError("assessment requires before and after scores")
    try:
        before_score, after_score = float(scores["before"]), float(scores["after"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise QualityError("assessment scores must be numeric") from exc
    if (
        not math.isfinite(before_score)
        or not math.isfinite(after_score)
        or not 0 <= before_score <= 100
        or not 0 <= after_score <= 100
    ):
        raise QualityError("assessment scores must be finite values from 0 to 100")
    if not math.isfinite(minimum_after_score) or not 80 <= minimum_after_score <= 100:
        raise QualityError("review package minimum score is invalid")
    if after_score < before_score:
        raise QualityError(
            "assessment rejected: after score is lower than before score"
        )
    if after_score < minimum_after_score:
        raise QualityError(
            "assessment rejected: after score is below the required threshold"
        )
    summary = value.get("summary")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > 2000
        or any(ord(character) < 32 and character not in "\t\n" for character in summary)
    ):
        raise QualityError("assessment summary must be non-empty canonical text")
    result = dict(value)
    result["scores"] = {"before": before_score, "after": after_score}
    return result


def prepare_review(
    request_path: Path, review_dir: Path, overwrite: bool = False
) -> dict[str, Any]:
    request = load_request(request_path)
    bpy = _bpy()
    source = Path(request.source_blend).expanduser().resolve()
    destination = review_dir.expanduser().resolve()
    if source == destination / "candidate.blend":
        raise PipelineError("output asset must not overwrite the source file")
    if source.is_relative_to(destination):
        raise PipelineError(
            "source file must not be located inside the output directory"
        )
    if destination.exists() and not overwrite:
        raise PipelineError(f"output directory already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".{destination.name}.staging.{uuid.uuid4().hex}")
    stage.mkdir(mode=0o700)
    try:
        source_map = audit_current_scene(request, bpy)
        before = snapshot_scene(bpy)
        before_render = stage / "before.png"
        after_render = stage / "after.png"
        _render(bpy, before_render, request.evidence.resolution)

        changes = apply_edit(request, bpy)
        pack_result = bpy.ops.file.pack_all()
        if "FINISHED" not in pack_result:
            raise PipelineError("Blender failed to pack external dependencies")
        after = snapshot_scene(bpy)
        require_target_change(before, after, request)
        drift = validate_non_target_drift(before, after, request)
        if drift:
            raise QualityError("non-target drift detected: " + ", ".join(drift))

        asset = stage / "candidate.blend"
        save_result = bpy.ops.wm.save_as_mainfile(
            filepath=str(asset), check_existing=False
        )
        if "FINISHED" not in save_result or not asset.is_file():
            raise PipelineError("Blender failed to save the staged asset")
        reopen_result = bpy.ops.wm.open_mainfile(filepath=str(asset))
        if "FINISHED" not in reopen_result:
            raise PipelineError("Blender failed to reopen the staged asset")
        reopened = snapshot_scene(bpy)
        if reopened["digest"] != after["digest"]:
            paths = difference_paths(after, reopened)
            raise QualityError(
                "reopened scene snapshot does not match the saved scene: "
                + ", ".join(paths[:12])
            )
        require_no_drift(before, reopened, request)

        _render(bpy, after_render, request.evidence.resolution)
        difference = image_difference(before_render, after_render)
        require_visual_change(difference, request)
        atomic_write_json(stage / "source_map.json", source_map)
        artifact_names = [
            "candidate.blend",
            "before.png",
            "after.png",
            "source_map.json",
        ]
        review = {
            "schema": "blender-edit-review-package/v1",
            "status": "awaiting_review",
            "edit_id": request.edit_id,
            "kind": request.kind.value,
            "source": {"filename": source.name, "sha256": request.source_sha256},
            "request_sha256": digest_json(request.as_dict()),
            "targets": request.targets.as_dict(),
            "changes": changes,
            "non_target_drift": [],
            "reopen": {"verified": True, "snapshot_digest": reopened["digest"]},
            "minimum_after_score": request.evidence.minimum_after_score,
            "visual_difference": difference.as_dict(),
            "artifacts": _artifact_record(stage, artifact_names),
        }
        atomic_write_json(stage / "review_package.json", review)
        _publish(stage, destination, overwrite)
        return review
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def apply_pipeline(
    request_path: Path, output_dir: Path, overwrite: bool = False
) -> dict[str, Any]:
    """Compatibility alias: apply now prepares review and never emits completion."""
    return prepare_review(request_path, output_dir, overwrite)


def finalize_pipeline(
    review_dir: Path,
    assessment_path: Path,
    output_dir: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    requested_review_root = review_dir.expanduser()
    if requested_review_root.is_symlink():
        raise PipelineError("review directory must not be a symlink")
    review_root = requested_review_root.resolve(strict=True)
    if not review_root.is_dir():
        raise PipelineError("review directory must be a regular directory")
    destination = output_dir.expanduser().resolve()
    if destination == review_root or destination.is_relative_to(review_root):
        raise PipelineError(
            "completed output must be separate from the review directory"
        )
    if destination.exists() and not overwrite:
        raise PipelineError(f"output directory already exists: {destination}")
    review_path = review_root / "review_package.json"
    review = _load_json_object(review_path, "review package")
    required_review_fields = {
        "schema",
        "status",
        "edit_id",
        "kind",
        "source",
        "request_sha256",
        "targets",
        "changes",
        "non_target_drift",
        "reopen",
        "minimum_after_score",
        "visual_difference",
        "artifacts",
    }
    reopen = review.get("reopen")
    if (
        set(review) != required_review_fields
        or review.get("schema") != "blender-edit-review-package/v1"
        or review.get("status") != "awaiting_review"
        or review.get("non_target_drift") != []
        or not isinstance(reopen, dict)
        or reopen.get("verified") is not True
    ):
        raise PipelineError(
            "review package is not a structurally verified awaiting-review result"
        )
    source = review.get("source")
    try:
        minimum_after_score = float(review["minimum_after_score"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise PipelineError("review package minimum score is invalid") from exc
    if (
        not isinstance(source, dict)
        or set(source) != {"filename", "sha256"}
        or not isinstance(source.get("filename"), str)
        or not source["filename"]
        or not _is_digest(source.get("sha256"))
        or not _is_digest(review.get("request_sha256"))
        or not isinstance(reopen, dict)
        or set(reopen) != {"verified", "snapshot_digest"}
        or not _is_digest(reopen.get("snapshot_digest"))
        or not math.isfinite(minimum_after_score)
        or not 80 <= minimum_after_score <= 100
    ):
        raise PipelineError(
            "review package identity, snapshot, or score binding is invalid"
        )
    artifacts = _artifact_index(
        review_root,
        review.get("artifacts"),
        {"candidate.blend", "before.png", "after.png", "source_map.json"},
    )
    review_sha = sha256_file(review_path)
    assessment_source = assessment_path.expanduser()
    if assessment_source.is_symlink():
        raise PipelineError("assessment must not be a symlink")
    assessment_source = assessment_source.resolve(strict=True)
    assessment = validate_assessment(
        _load_json_object(assessment_source, "assessment"),
        review_sha256=review_sha,
        artifacts=artifacts,
        minimum_after_score=minimum_after_score,
    )
    bpy = _bpy()
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".{destination.name}.staging.{uuid.uuid4().hex}")
    stage.mkdir(mode=0o700)
    try:
        copies = {
            "candidate.blend": "asset.blend",
            "before.png": "before.png",
            "after.png": "after.png",
            "source_map.json": "source_map.json",
        }
        for source_name, destination_name in copies.items():
            shutil.copyfile(review_root / source_name, stage / destination_name)
        shutil.copyfile(review_path, stage / "review_package.json")
        shutil.copyfile(assessment_source, stage / "assessment.json")
        if sha256_file(stage / "asset.blend") != artifacts["candidate.blend"]["sha256"]:
            raise PipelineError("staged candidate asset hash drifted")
        reopen_result = bpy.ops.wm.open_mainfile(filepath=str(stage / "asset.blend"))
        if "FINISHED" not in reopen_result:
            raise PipelineError("Blender failed to reopen the assessed candidate")
        reopened = snapshot_scene(bpy)
        if reopened["digest"] != review["reopen"]["snapshot_digest"]:
            raise QualityError(
                "assessed candidate snapshot no longer matches the review package"
            )
        assessment_sha = sha256_file(stage / "assessment.json")
        visual_evidence = {
            "before": {**artifacts["before.png"], "path": "before.png"},
            "after": {**artifacts["after.png"], "path": "after.png"},
            "difference": review["visual_difference"],
            "assessment": {
                "kind": assessment["assessor"]["kind"],
                "name": assessment["assessor"]["name"],
                "before_score": assessment["scores"]["before"],
                "after_score": assessment["scores"]["after"],
                "minimum_after_score": minimum_after_score,
                "summary": assessment["summary"],
                "sha256": assessment_sha,
            },
        }
        artifact_names = [
            "asset.blend",
            "before.png",
            "after.png",
            "source_map.json",
            "review_package.json",
            "assessment.json",
        ]
        receipt = {
            "schema": "blender-edit-receipt/v1",
            "status": "complete",
            "edit_id": review["edit_id"],
            "kind": review["kind"],
            "source": review["source"],
            "request_sha256": review["request_sha256"],
            "targets": review["targets"],
            "changes": review["changes"],
            "non_target_drift": [],
            "reopen": {"verified": True, "snapshot_digest": reopened["digest"]},
            "review": {
                "package_sha256": review_sha,
                "assessment_sha256": assessment_sha,
            },
            "visual_evidence": visual_evidence,
            "artifacts": _artifact_record(stage, artifact_names),
        }
        atomic_write_json(stage / "receipt.json", receipt)
        _publish(stage, destination, overwrite)
        return receipt
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blender-edit-pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-request")
    validate.add_argument("--request", required=True, type=Path)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--request", required=True, type=Path)
    preflight.add_argument("--output", required=True, type=Path)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--request", required=True, type=Path)
    prepare.add_argument("--review-dir", required=True, type=Path)
    prepare.add_argument("--overwrite", action="store_true")
    apply = commands.add_parser("apply")
    apply.add_argument("--request", required=True, type=Path)
    apply.add_argument("--output-dir", required=True, type=Path)
    apply.add_argument("--overwrite", action="store_true")
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--review-dir", required=True, type=Path)
    finalize.add_argument("--assessment", required=True, type=Path)
    finalize.add_argument("--output-dir", required=True, type=Path)
    finalize.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _blender_arguments(sys.argv if argv is None else ["pipeline", *argv])
    args = _parser().parse_args(arguments)
    try:
        if args.command == "validate-request":
            request = load_request(args.request)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "edit_id": request.edit_id,
                        "kind": request.kind.value,
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "preflight":
            request = load_request(args.request)
            atomic_write_json(args.output, audit_current_scene(request))
        elif args.command == "prepare":
            review = prepare_review(args.request, args.review_dir, args.overwrite)
            print(json.dumps(review, sort_keys=True))
        elif args.command == "apply":
            review = apply_pipeline(args.request, args.output_dir, args.overwrite)
            print(json.dumps(review, sort_keys=True))
        else:
            receipt = finalize_pipeline(
                args.review_dir, args.assessment, args.output_dir, args.overwrite
            )
            print(json.dumps(receipt, sort_keys=True))
        return 0
    except (ContractError, PreflightError, QualityError, PipelineError, OSError) as exc:
        print(
            json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
