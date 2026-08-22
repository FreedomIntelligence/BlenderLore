"""Strict, dependency-free request contracts and atomic JSON utilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping


class ContractError(ValueError):
    """Raised when a request violates the public edit contract."""


class EditKind(str, Enum):
    MATERIAL = "material"
    COLOR = "color"
    GEOMETRY = "geometry"
    MODELING = "modeling"
    SCENE = "scene"


_TARGET_KEYS = {"objects", "materials", "lights", "camera", "generated_objects"}


def _names(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v.strip() for v in value
    ):
        raise ContractError(f"{field_name} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ContractError(f"{field_name} contains duplicate names")
    return tuple(value)


def _finite_tree(value: Any, field_name: str = "parameters") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError(f"{field_name} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{field_name} keys must be strings")
            _finite_tree(child, f"{field_name}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _finite_tree(child, f"{field_name}[{index}]")


@dataclass(frozen=True)
class Targets:
    objects: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    lights: tuple[str, ...] = ()
    camera: tuple[str, ...] = ()
    generated_objects: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Any) -> "Targets":
        if not isinstance(raw, Mapping):
            raise ContractError("targets must be an object")
        unknown = set(raw) - _TARGET_KEYS
        if unknown:
            raise ContractError(f"unknown target fields: {sorted(unknown)}")
        return cls(
            **{key: _names(raw.get(key), f"targets.{key}") for key in _TARGET_KEYS}
        )

    def as_dict(self) -> dict[str, list[str]]:
        return {
            key: list(getattr(self, key))
            for key in sorted(_TARGET_KEYS)
            if getattr(self, key)
        }


@dataclass(frozen=True)
class Evidence:
    render: bool = True
    resolution: int = 256
    min_changed_fraction: float = 0.001
    min_mean_absolute_delta: float = 0.25
    minimum_after_score: float = 80.0

    @classmethod
    def from_mapping(cls, raw: Any) -> "Evidence":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ContractError("evidence must be an object")
        allowed = {
            "render",
            "resolution",
            "min_changed_fraction",
            "min_mean_absolute_delta",
            "minimum_after_score",
        }
        if set(raw) - allowed:
            raise ContractError(
                f"unknown evidence fields: {sorted(set(raw) - allowed)}"
            )
        render = raw.get("render", True)
        resolution = raw.get("resolution", 256)
        fraction = raw.get("min_changed_fraction", 0.001)
        delta = raw.get("min_mean_absolute_delta", 0.25)
        minimum_after_score = raw.get("minimum_after_score", 80.0)
        if type(render) is not bool:
            raise ContractError("evidence.render must be boolean")
        if render is not True:
            raise ContractError(
                "evidence.render must be true; completed edits require visual review"
            )
        if type(resolution) is not int or not 32 <= resolution <= 4096:
            raise ContractError(
                "evidence.resolution must be an integer from 32 to 4096"
            )
        if (
            not isinstance(fraction, (int, float))
            or isinstance(fraction, bool)
            or not 0 <= fraction <= 1
        ):
            raise ContractError("evidence.min_changed_fraction must be from 0 to 1")
        if (
            not isinstance(delta, (int, float))
            or isinstance(delta, bool)
            or not 0 <= delta <= 255
        ):
            raise ContractError(
                "evidence.min_mean_absolute_delta must be from 0 to 255"
            )
        if (
            not isinstance(minimum_after_score, (int, float))
            or isinstance(minimum_after_score, bool)
            or not 80 <= minimum_after_score <= 100
        ):
            raise ContractError("evidence.minimum_after_score must be from 80 to 100")
        return cls(
            render,
            resolution,
            float(fraction),
            float(delta),
            float(minimum_after_score),
        )


@dataclass(frozen=True)
class EditRequest:
    schema: str
    edit_id: str
    kind: EditKind
    source_blend: str
    source_sha256: str
    targets: Targets
    parameters: dict[str, Any]
    evidence: Evidence = field(default_factory=Evidence)

    @classmethod
    def from_mapping(cls, raw: Any) -> "EditRequest":
        if not isinstance(raw, Mapping):
            raise ContractError("request root must be an object")
        required = {
            "schema",
            "edit_id",
            "kind",
            "source_blend",
            "source_sha256",
            "targets",
            "parameters",
        }
        allowed = required | {"evidence"}
        missing, unknown = required - set(raw), set(raw) - allowed
        if missing:
            raise ContractError(f"missing request fields: {sorted(missing)}")
        if unknown:
            raise ContractError(f"unknown request fields: {sorted(unknown)}")
        for key in ("schema", "edit_id", "source_blend", "source_sha256"):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ContractError(f"{key} must be a non-empty string")
        if raw["schema"] != "blender-edit-request/v1":
            raise ContractError("unsupported request schema")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", raw["edit_id"]) is None:
            raise ContractError(
                "edit_id must be a portable identifier of at most 128 characters"
            )
        try:
            kind = EditKind(raw["kind"])
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "kind must be material, color, geometry, modeling, or scene"
            ) from exc
        digest = raw["source_sha256"].lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ContractError(
                "source_sha256 must be a 64-character lowercase hexadecimal digest"
            )
        targets = Targets.from_mapping(raw["targets"])
        parameters = raw["parameters"]
        if not isinstance(parameters, Mapping):
            raise ContractError("parameters must be an object")
        parameters = dict(parameters)
        _finite_tree(parameters)
        _validate_scope(kind, targets, parameters)
        return cls(
            raw["schema"],
            raw["edit_id"],
            kind,
            raw["source_blend"],
            digest,
            targets,
            parameters,
            Evidence.from_mapping(raw.get("evidence")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "edit_id": self.edit_id,
            "kind": self.kind.value,
            "source_blend": self.source_blend,
            "source_sha256": self.source_sha256,
            "targets": self.targets.as_dict(),
            "parameters": self.parameters,
            "evidence": {
                "render": self.evidence.render,
                "resolution": self.evidence.resolution,
                "min_changed_fraction": self.evidence.min_changed_fraction,
                "min_mean_absolute_delta": self.evidence.min_mean_absolute_delta,
                "minimum_after_score": self.evidence.minimum_after_score,
            },
        }


def _validate_scope(
    kind: EditKind, targets: Targets, parameters: Mapping[str, Any]
) -> None:
    if kind in {EditKind.MATERIAL, EditKind.COLOR}:
        if not targets.materials:
            raise ContractError(f"{kind.value} edits require targets.materials")
        if set(targets.as_dict()) - {"materials"}:
            raise ContractError(f"{kind.value} edits may target materials only")
    elif kind == EditKind.GEOMETRY:
        if not targets.objects or set(targets.as_dict()) - {"objects"}:
            raise ContractError("geometry edits require only targets.objects")
    elif kind == EditKind.MODELING:
        if not (targets.objects or targets.generated_objects):
            raise ContractError("modeling edits require target or generated objects")
        if set(targets.as_dict()) - {"objects", "generated_objects"}:
            raise ContractError(
                "modeling edits may target objects and generated_objects only"
            )
    elif kind == EditKind.SCENE:
        allowed = {"lights", "camera"}
        if set(targets.as_dict()) - allowed:
            raise ContractError("scene edits may target lights and camera only")
        scoped = bool(
            targets.lights
            or targets.camera
            or parameters.get("allow_world")
            or parameters.get("allow_exposure")
        )
        if not scoped:
            raise ContractError("scene edit has no declared mutable scope")
    if not parameters:
        raise ContractError("parameters must not be empty")


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_request(path: str | os.PathLike[str]) -> EditRequest:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return EditRequest.from_mapping(json.load(handle))
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid request JSON: {exc}") from exc
