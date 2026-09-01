#!/usr/bin/env python3
"""Validate the standalone blender-pipeline skill without external packages."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
FORBIDDEN_ARTIFACT_SUFFIXES = {
    ".7z",
    ".avi",
    ".blend",
    ".blend1",
    ".bz2",
    ".db",
    ".fbx",
    ".gif",
    ".glb",
    ".gltf",
    ".gz",
    ".jpeg",
    ".jpg",
    ".log",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".obj",
    ".png",
    ".rar",
    ".sqlite",
    ".stl",
    ".tar",
    ".tgz",
    ".usda",
    ".usdc",
    ".usd",
    ".webm",
    ".webp",
    ".xz",
    ".zip",
}
REQUIRED_FILES = {
    Path("SKILL.md"),
    Path("generation/SKILL.md"),
    Path("generation/requirements.txt"),
    Path("generation/requirements-knowledge.txt"),
    Path("editing/SKILL.md"),
    Path("editing/pyproject.toml"),
    Path("editing/schemas/assessment.schema.json"),
    Path("editing/schemas/edit_request.schema.json"),
    Path("editing/schemas/review_package.schema.json"),
    Path("editing/schemas/source_map.schema.json"),
    Path("editing/schemas/edit_receipt.schema.json"),
    Path("generation/scripts/render_illustrated_tutorial.py"),
    Path("knowledge/index.md"),
    Path("knowledge/manifest.json"),
    Path("knowledge/manifest.schema.json"),
    Path("knowledge/evidence-and-routing.md"),
    Path("knowledge/generation.md"),
    Path("knowledge/editing.md"),
    Path("knowledge/rendering-and-dynamics.md"),
    Path("knowledge/operations-and-knowledge.md"),
    Path("scripts/validate_package.py"),
    Path("reproduction/SKILL.md"),
    Path("reproduction/references/execution-contract.md"),
    Path("reproduction/scripts/export_public_showcase_knowledge.py"),
    Path("reproduction/scripts/launch_video2blender_reproduction.py"),
    Path("reproduction/scripts/reproduce.sh"),
    Path("reproduction/scripts/validate_public_knowledge.py"),
    Path("reproduction/knowledge/manifest.json"),
    Path("reproduction/knowledge/public_media_attestations.json"),
    Path("reproduction/knowledge/public_showcase_inventory.json"),
}
REQUIRED_DIRS = {
    Path("generation/scripts"),
    Path("generation/tests"),
    Path("editing/src/blender_edit_pipeline"),
    Path("editing/tests"),
    Path("reproduction/tests"),
    Path("tests"),
}
LOCAL_IMPORT_PREFIXES = (
    "agent_",
    "blender_",
    "build_",
    "embed_",
    "extract_",
    "generate_",
    "merge_",
    "model_direct_",
    "prepare_",
    "project_paths",
    "render_",
    "retrieve_",
    "run_video_",
    "rw1_",
    "scan_",
    "update_",
    "video_replay_",
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


@dataclass(frozen=True, order=True)
class Issue:
    check: str
    path: str
    message: str


def relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def iter_files(root: Path, suffixes: set[str] | None = None) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if suffixes is not None and path.suffix.lower() not in suffixes:
            continue
        yield path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if len(lines) < 4 or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return result
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip("\"'")
    return {}


def check_completeness(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for item in sorted(REQUIRED_FILES):
        if not (root / item).is_file():
            issues.append(
                Issue("completeness", item.as_posix(), "required file is missing")
            )
    for item in sorted(REQUIRED_DIRS):
        path = root / item
        if not path.is_dir() or not any(
            candidate.is_file() for candidate in path.rglob("*")
        ):
            issues.append(
                Issue(
                    "completeness",
                    item.as_posix(),
                    "required directory is empty or missing",
                )
            )

    for skill_path in (
        root / "SKILL.md",
        root / "generation/SKILL.md",
        root / "editing/SKILL.md",
        root / "reproduction/SKILL.md",
    ):
        if not skill_path.is_file():
            continue
        metadata = frontmatter(read_text(skill_path))
        for key in ("name", "description"):
            if not metadata.get(key):
                issues.append(
                    Issue(
                        "completeness",
                        relative(skill_path, root),
                        f"frontmatter lacks {key}",
                    )
                )

    top_skill = root / "SKILL.md"
    if top_skill.is_file():
        top_text = read_text(top_skill)
        if len(top_text.splitlines()) > 80:
            issues.append(
                Issue(
                    "completeness",
                    "SKILL.md",
                    "router exceeds the 80-line disclosure budget",
                )
            )
        if frontmatter(top_text).get("name") != root.name:
            issues.append(
                Issue(
                    "completeness",
                    "SKILL.md",
                    "skill name must match the package directory",
                )
            )

    for path in iter_files(root):
        rel = relative(path, root)
        lowered = path.name.lower()
        if path.is_symlink():
            issues.append(
                Issue(
                    "completeness",
                    rel,
                    "symbolic links are not allowed in the standalone package",
                )
            )
        if lowered in {"readme.md", "changelog.md", "changes.md"}:
            issues.append(
                Issue(
                    "completeness",
                    rel,
                    "auxiliary documentation is outside the package contract",
                )
            )
        if path.suffix.lower() in FORBIDDEN_ARTIFACT_SUFFIXES:
            issues.append(
                Issue(
                    "completeness",
                    rel,
                    "generated or binary artifact is not package source",
                )
            )
        if path.stat().st_size > 2_000_000:
            issues.append(
                Issue("completeness", rel, "file exceeds the 2 MB source-package limit")
            )
    return issues


def _sensitive_patterns() -> Sequence[tuple[str, re.Pattern[str]]]:
    return (
        (
            "personal home path",
            re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^/\s]+/"),
        ),
        ("mounted-volume path", re.compile(r"(?<![A-Za-z0-9_])/Volumes/")),
        ("internal share path", re.compile(r"(?<![A-Za-z0-9_])/F[0-9]{8,}/")),
        ("Windows user path", re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\\s]+\\\\")),
        ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
        ("API secret", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
        ("cloud access key", re.compile(r"AKIA[0-9A-Z]{16}")),
        (
            "private key",
            re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
        ),
        ("private WeChat path", re.compile(r"xwechat_files", re.IGNORECASE)),
        ("embedded file URL", re.compile(r"file://", re.IGNORECASE)),
        ("embedded data URI", re.compile(r"data:(?:image|video)/", re.IGNORECASE)),
        ("credential in URL", re.compile(r"https?://[^/\s:@]+:[^/\s@]+@")),
    )


def check_sensitive_paths(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    validator = (root / "scripts/validate_package.py").resolve()
    for path in iter_files(root, TEXT_SUFFIXES):
        if path.resolve() == validator:
            continue
        try:
            text = read_text(path)
        except UnicodeDecodeError:
            issues.append(
                Issue(
                    "sensitive", relative(path, root), "declared text file is not UTF-8"
                )
            )
            continue
        for label, pattern in _sensitive_patterns():
            if label == "embedded data URI" and path.suffix.lower() in {".py", ".sh"}:
                continue
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                issues.append(
                    Issue("sensitive", f"{relative(path, root)}:{line}", label)
                )
    return issues


def _module_exists(base: Path, parts: Sequence[str]) -> bool:
    if not parts:
        return True
    target = base.joinpath(*parts)
    return target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()


def _import_names(node: ast.AST) -> Iterable[tuple[str, int]]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name, 0
    elif isinstance(node, ast.ImportFrom):
        yield node.module or "", node.level


def _check_relative_import(path: Path, module: str, level: int) -> bool:
    base = path.parent
    for _ in range(max(0, level - 1)):
        base = base.parent
    return _module_exists(base, module.split(".") if module else ())


def check_imports(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    generation_scripts = root / "generation/scripts"
    generation_modules = {
        path.stem
        for path in generation_scripts.glob("*.py")
        if path.name != "__init__.py"
    }
    editing_src = root / "editing/src"

    for path in iter_files(root, {".py"}):
        rel = relative(path, root)
        try:
            tree = ast.parse(read_text(path), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            issues.append(Issue("imports", rel, f"Python source does not parse: {exc}"))
            continue

        for node in ast.walk(tree):
            for module, level in _import_names(node):
                if level:
                    if not _check_relative_import(path, module, level):
                        issues.append(
                            Issue(
                                "imports",
                                f"{rel}:{getattr(node, 'lineno', 0)}",
                                f"unresolved relative import {'.' * level}{module}",
                            )
                        )
                    continue

                parts = module.split(".") if module else []
                top = parts[0] if parts else ""
                if top == "blender_edit_pipeline":
                    if not _module_exists(editing_src, parts):
                        issues.append(
                            Issue(
                                "imports",
                                f"{rel}:{getattr(node, 'lineno', 0)}",
                                f"unresolved editing import {module}",
                            )
                        )
                    continue

                in_generation = (
                    generation_scripts in path.parents
                    or root / "generation/tests" in path.parents
                )
                looks_local = top in generation_modules or top.startswith(
                    LOCAL_IMPORT_PREFIXES
                )
                if (
                    in_generation
                    and looks_local
                    and not _module_exists(generation_scripts, parts)
                ):
                    issues.append(
                        Issue(
                            "imports",
                            f"{rel}:{getattr(node, 'lineno', 0)}",
                            f"unresolved generation import {module}",
                        )
                    )
    return issues


def _json_type_matches(value: Any, expected: str) -> bool:
    mapping: Mapping[str, type | tuple[type, ...]] = {
        "array": list,
        "boolean": bool,
        "integer": int,
        "null": type(None),
        "number": (int, float),
        "object": dict,
        "string": str,
    }
    target = mapping.get(expected)
    if target is None:
        return False
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, target)


def _validate_schema(
    value: Any, schema: Mapping[str, Any], location: str = "$"
) -> list[str]:
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{location}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{location}: value is outside the declared enum")
    expected_type = schema.get("type")
    if expected_type and not _json_type_matches(value, expected_type):
        errors.append(f"{location}: expected {expected_type}")
        return errors

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{location}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{location}: undeclared property {key!r}")
        for key, child_schema in properties.items():
            if key in value:
                errors.extend(
                    _validate_schema(value[key], child_schema, f"{location}.{key}")
                )

    if isinstance(value, list):
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < minimum:
            errors.append(f"{location}: requires at least {minimum} items")
        if schema.get("uniqueItems"):
            canonical = [
                json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value
            ]
            if len(canonical) != len(set(canonical)):
                errors.append(f"{location}: items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _validate_schema(item, item_schema, f"{location}[{index}]")
                )

    if isinstance(value, str):
        minimum = schema.get("minLength")
        if minimum is not None and len(value) < minimum:
            errors.append(f"{location}: requires at least {minimum} characters")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, value) is None:
            errors.append(f"{location}: does not match {pattern!r}")
        if schema.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{location}: invalid ISO date")
    return errors


def check_json_schemas(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    parsed: dict[Path, Any] = {}
    for path in iter_files(root, {".json"}):
        try:
            parsed[path] = json.loads(read_text(path))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            issues.append(
                Issue("json-schema", relative(path, root), f"invalid JSON: {exc}")
            )

    schema_path = root / "knowledge/manifest.schema.json"
    manifest_path = root / "knowledge/manifest.json"
    if schema_path not in parsed or manifest_path not in parsed:
        return issues
    schema = parsed[schema_path]
    manifest = parsed[manifest_path]
    if (
        not isinstance(schema, dict)
        or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
    ):
        issues.append(
            Issue(
                "json-schema",
                "knowledge/manifest.schema.json",
                "unsupported or missing schema dialect",
            )
        )
        return issues
    if not isinstance(manifest, dict):
        issues.append(
            Issue(
                "json-schema", "knowledge/manifest.json", "manifest must be an object"
            )
        )
        return issues
    for error in _validate_schema(manifest, schema):
        issues.append(Issue("json-schema", "knowledge/manifest.json", error))

    window = manifest.get("recent_extraction_window", {})
    if isinstance(window, dict):
        start = window.get("from")
        through = window.get("through")
        try:
            if date.fromisoformat(str(start)) > date.fromisoformat(str(through)):
                issues.append(
                    Issue(
                        "json-schema",
                        "knowledge/manifest.json",
                        "knowledge window is reversed",
                    )
                )
        except ValueError:
            pass
        if through != manifest.get("updated_through"):
            issues.append(
                Issue(
                    "json-schema",
                    "knowledge/manifest.json",
                    "updated_through differs from window through",
                )
            )

    declared_paths = [
        item.get("path")
        for item in manifest.get("documents", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]
    if len(declared_paths) != len(set(declared_paths)):
        issues.append(
            Issue(
                "json-schema",
                "knowledge/manifest.json",
                "document paths must be unique",
            )
        )
    declared = set(declared_paths)
    actual = {path.name for path in (root / "knowledge").glob("*.md")}
    if declared != actual:
        issues.append(
            Issue(
                "json-schema",
                "knowledge/manifest.json",
                f"declared Markdown differs from knowledge directory: declared={sorted(declared)}, actual={sorted(actual)}",
            )
        )
    return issues


def _markdown_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split(maxsplit=1)[0]


def check_links(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    root_resolved = root.resolve()
    for path in iter_files(root, {".md"}):
        text = read_text(path)
        for match in MARKDOWN_LINK.finditer(text):
            target = unquote(_markdown_target(match.group(1)))
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("//"):
                continue
            local_part = parsed.path
            if not local_part:
                continue
            candidate = (path.parent / local_part).resolve()
            line = text.count("\n", 0, match.start()) + 1
            rel = f"{relative(path, root)}:{line}"
            if not candidate.is_relative_to(root_resolved):
                issues.append(
                    Issue("links", rel, f"local link escapes package root: {target}")
                )
            elif not candidate.exists():
                issues.append(Issue("links", rel, f"broken local link: {target}"))
    return issues


CHECKS: Mapping[str, Callable[[Path], list[Issue]]] = {
    "completeness": check_completeness,
    "sensitive": check_sensitive_paths,
    "imports": check_imports,
    "json-schema": check_json_schemas,
    "links": check_links,
}


def validate(root: Path, names: Sequence[str] | None = None) -> list[Issue]:
    selected = tuple(names or CHECKS)
    issues: list[Issue] = []
    for name in selected:
        issues.extend(CHECKS[name](root))
    return sorted(set(issues))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help="blender-pipeline skill root"
    )
    parser.add_argument(
        "--check",
        action="append",
        choices=sorted(CHECKS),
        dest="checks",
        help="run only one named check; repeat as needed",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable report"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    issues = validate(root, args.checks)
    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "status": "fail" if issues else "pass",
                    "issues": [asdict(issue) for issue in issues],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    elif issues:
        for issue in issues:
            print(f"{issue.check}: {issue.path}: {issue.message}", file=sys.stderr)
        print(f"FAIL: {len(issues)} package issue(s)", file=sys.stderr)
    else:
        selected = ", ".join(args.checks or CHECKS)
        print(f"PASS: blender-pipeline package ({selected})")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
