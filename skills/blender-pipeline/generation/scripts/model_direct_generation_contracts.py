"""Fail-closed task and generated-source contracts for GEN-V3.

This module intentionally contains only the contracts exercised by the current
model-direct generation harness. Source acquisition and retired benchmark
contracts belong outside the executable generation boundary.
"""

from __future__ import annotations

import ast
import html
import re
import unicodedata
from typing import Any, Mapping
from urllib.parse import unquote


MODEL_TASK_KEYS = ("task_id", "title", "instruction", "category")
NETWORK_IMPORT_ROOTS = {
    "aiohttp",
    "asyncio",
    "ftplib",
    "http",
    "requests",
    "socket",
    "ssl",
    "subprocess",
    "urllib",
    "webbrowser",
}
FILESYSTEM_ESCAPE_IMPORT_ROOTS = {
    "builtins",
    "glob",
    "io",
    "os",
    "pathlib",
    "shutil",
    "tarfile",
    "tempfile",
    "zipfile",
}
GENERATED_SOURCE_FORBIDDEN_IMPORT_ROOTS = frozenset(
    NETWORK_IMPORT_ROOTS | FILESYSTEM_ESCAPE_IMPORT_ROOTS
)
GENERATED_SOURCE_FORBIDDEN_CALL_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "chr",
        "bytes",
        "bytearray",
        "system",
        "popen",
        "urlopen",
        "connect",
        "create_connection",
        "decode",
    }
)
GENERATED_SOURCE_FORBIDDEN_DYNAMIC_NAMES = frozenset({"__builtins__", "builtins"})
GENERATED_SOURCE_UNPROVABLE_TEXT_CALL_NAMES = frozenset(
    {"format", "replace", "unescape"}
)
BVID_LEAK_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.I)
VIDEO_EXTENSION_RE = re.compile(
    r"(?i)(?:\.3gp|\.avi|\.flv|\.m2ts|\.m4v|\.mkv|\.mov|\.mp4|\.mpeg|"
    r"\.mpg|\.mts|\.ogv|\.ts|\.webm|\.wmv)(?![0-9A-Za-z])"
)
PRIVATE_PATH_MARKERS = (
    "source_video",
    "source_path",
    "source_file",
    "evidence_path",
    "evidence_file",
    "private_path",
    "private_file",
    "private_provenance",
)

# This narrow table exposes common ASCII lookalikes before provenance checks.
# It is a security normalization, not a language transliteration.
ASCII_CONFUSABLES = str.maketrans(
    {
        "А": "A",
        "а": "a",
        "В": "B",
        "в": "b",
        "С": "C",
        "с": "c",
        "Е": "E",
        "е": "e",
        "Н": "H",
        "н": "h",
        "І": "I",
        "і": "i",
        "Ј": "J",
        "ј": "j",
        "К": "K",
        "к": "k",
        "М": "M",
        "м": "m",
        "О": "O",
        "о": "o",
        "Р": "P",
        "р": "p",
        "Ѕ": "S",
        "ѕ": "s",
        "Т": "T",
        "т": "t",
        "Х": "X",
        "х": "x",
        "Ү": "Y",
        "ү": "y",
        "Ѵ": "V",
        "ѵ": "v",
        "Α": "A",
        "α": "a",
        "Β": "B",
        "β": "b",
        "Ε": "E",
        "ε": "e",
        "Η": "H",
        "η": "h",
        "Ι": "I",
        "ι": "i",
        "Κ": "K",
        "κ": "k",
        "Μ": "M",
        "μ": "m",
        "Ν": "N",
        "ν": "v",
        "Ο": "O",
        "ο": "o",
        "Ρ": "P",
        "ρ": "p",
        "Τ": "T",
        "τ": "t",
        "Υ": "Y",
        "υ": "u",
        "Χ": "X",
        "χ": "x",
        "Ζ": "Z",
        "ζ": "z",
        "Ᏼ": "B",
        "ꮄ": "b",
    }
)


class GenerationContractError(ValueError):
    """A model task or generated program escaped the trusted contract."""


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GenerationContractError(f"{label} must be non-empty text")
    return value.strip()


def _security_normalizations(text: str) -> tuple[str, str]:
    """Expose Unicode, URL, and separator obfuscations before leak checks."""

    def compatibility_fold(value: str) -> str:
        characters: list[str] = []
        for character in unicodedata.normalize("NFKC", value):
            category = unicodedata.category(character)
            if category == "Cf" or category.startswith("M"):
                continue
            translated = character.translate(ASCII_CONFUSABLES)
            if len(translated) == 1:
                try:
                    digit = unicodedata.digit(translated)
                except (TypeError, ValueError):
                    pass
                else:
                    if 0 <= digit <= 9:
                        translated = str(digit)
            characters.append(translated)
        return "".join(characters)

    normalized = compatibility_fold(text)
    for _ in range(3):
        decoded = html.unescape(unquote(normalized))
        refolded = compatibility_fold(decoded)
        if refolded == normalized:
            break
        normalized = refolded
    compact = re.sub(r"[\s._:/\\%+~\-]+", "", normalized)
    return normalized, compact


def _contains_private_text(text: str) -> bool:
    normalized, compact = _security_normalizations(text)
    lowered = normalized.casefold()
    compact_lowered = compact.casefold()
    return bool(
        BVID_LEAK_RE.search(normalized)
        or BVID_LEAK_RE.search(compact)
        or "http://" in lowered
        or "https://" in lowered
        or ("http" in compact_lowered and "com" in compact_lowered)
        or ".." in normalized
        or VIDEO_EXTENSION_RE.search(normalized)
        or re.search(
            r"(?i)(?:3gp|avi|flv|m2ts|m4v|mkv|mov|mp4|mpeg|mpg|mts|ogv|webm|wmv)$",
            compact,
        )
        or normalized.startswith(("/", "~", "\\"))
        or re.search(r"[A-Za-z]:[\\/]", normalized)
        or any(token in lowered for token in PRIVATE_PATH_MARKERS)
        or re.search(
            r"(?i)(?:^|[\\/\s'\"`(])(?:source|evidence|private)(?:[\\/._-]|$)",
            normalized,
        )
    )


def make_sanitized_task_payload(
    *, task_id: str, title: str, instruction: str, category: str
) -> dict[str, str]:
    """Build the exact four-field model-visible task payload."""

    payload = {
        "task_id": task_id,
        "title": title,
        "instruction": instruction,
        "category": category,
    }
    return validate_sanitized_task_payload(payload)


def validate_sanitized_task_payload(
    value: Any, *, expected: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """Reject source identity and undeclared fields before a model call."""

    if (
        not isinstance(value, dict)
        or len(value) != len(MODEL_TASK_KEYS)
        or set(value) != set(MODEL_TASK_KEYS)
    ):
        raise GenerationContractError(
            "model task.json must use the exact "
            "task_id/title/instruction/category allowlist"
        )
    result: dict[str, str] = {}
    for key in MODEL_TASK_KEYS:
        text = _nonempty(value.get(key), f"model task.json {key}")
        if _contains_private_text(text):
            raise GenerationContractError(
                "model task.json leaks source identity, URL, video, or private path"
            )
        result[key] = text
    if expected is not None:
        exact = {key: expected.get(key) for key in MODEL_TASK_KEYS}
        if result != exact:
            raise GenerationContractError("model task.json semantic binding drift")
    return result


def _static_string_expression(
    node: ast.AST, bindings: Mapping[str, str] | None = None
) -> str | None:
    bindings = bindings or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp):
        left = _static_string_expression(node.left, bindings)
        if isinstance(node.op, ast.Add):
            right = _static_string_expression(node.right, bindings)
            return left + right if left is not None and right is not None else None
        if isinstance(node.op, ast.Mod) and left is not None:
            if isinstance(node.right, (ast.Tuple, ast.List)):
                values = [
                    _static_string_expression(value, bindings)
                    for value in node.right.elts
                ]
                if any(value is None for value in values):
                    return None
                replacement: Any = tuple(values)
            else:
                replacement = _static_string_expression(node.right, bindings)
                if replacement is None:
                    return None
            try:
                return left % replacement
            except (TypeError, ValueError):
                return None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            expression = value.value if isinstance(value, ast.FormattedValue) else value
            part = _static_string_expression(expression, bindings)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and not node.keywords
        and len(node.args) == 1
    ):
        separator = _static_string_expression(node.func.value, bindings)
        sequence = node.args[0]
        if separator is None or not isinstance(sequence, (ast.List, ast.Tuple)):
            return None
        values = [_static_string_expression(item, bindings) for item in sequence.elts]
        if any(value is None for value in values):
            return None
        return separator.join(value for value in values if value is not None)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        template = _static_string_expression(node.func.value, bindings)
        args = [_static_string_expression(value, bindings) for value in node.args]
        kwargs = {
            str(value.arg): _static_string_expression(value.value, bindings)
            for value in node.keywords
            if value.arg is not None
        }
        if (
            template is None
            or any(value is None for value in args)
            or len(kwargs) != len(node.keywords)
            or any(value is None for value in kwargs.values())
        ):
            return None
        try:
            return template.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError):
            return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and not node.keywords
        and len(node.args) in {2, 3}
    ):
        source = _static_string_expression(node.func.value, bindings)
        old = _static_string_expression(node.args[0], bindings)
        new = _static_string_expression(node.args[1], bindings)
        count = (
            node.args[2].value
            if len(node.args) == 3
            and isinstance(node.args[2], ast.Constant)
            and type(node.args[2].value) is int
            else None
        )
        if (
            source is None
            or old is None
            or new is None
            or (len(node.args) == 3 and count is None)
        ):
            return None
        return (
            source.replace(old, new)
            if count is None
            else source.replace(old, new, count)
        )
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        is_unescape = (
            isinstance(node.func, ast.Name) and node.func.id == "unescape"
        ) or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "html"
            and node.func.attr == "unescape"
        )
        if is_unescape:
            value = _static_string_expression(node.args[0], bindings)
            return html.unescape(value) if value is not None else None
    return None


def _static_string_bindings(tree: ast.AST) -> dict[str, str]:
    """Fold simple constant assignments before auditing reconstructed text."""

    bindings: dict[str, str] = {}
    assignments = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ),
        key=lambda node: (
            getattr(node, "lineno", 0),
            getattr(node, "col_offset", 0),
        ),
    )
    for assignment in assignments:
        value = _static_string_expression(assignment.value, bindings)
        targets = (
            assignment.targets
            if isinstance(assignment, ast.Assign)
            else [assignment.target]
        )
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if value is None:
                bindings.pop(target.id, None)
            else:
                bindings[target.id] = value
    return bindings


def _dangerous_generated_text(text: str) -> bool:
    normalized, compact = _security_normalizations(text)
    compact_lowered = compact.casefold()
    dynamic_tokens = ("__import__", "__builtins__", "__subclasses__")
    return _contains_private_text(normalized) or any(
        token in compact_lowered for token in dynamic_tokens
    )


def audit_generated_python_source(source: str) -> None:
    """Reject generated code with I/O, reflection, or provenance escapes."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GenerationContractError("generated Python source is invalid") from exc
    bindings = _static_string_bindings(tree)
    for value in bindings.values():
        if _dangerous_generated_text(value):
            raise GenerationContractError(
                "generated script reconstructs source/network/path provenance"
            )
    for node in ast.walk(tree):
        static_text = _static_string_expression(node, bindings)
        if static_text is not None and _dangerous_generated_text(static_text):
            raise GenerationContractError(
                "generated script contains obfuscated source/network/path provenance"
            )
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
            if roots & GENERATED_SOURCE_FORBIDDEN_IMPORT_ROOTS:
                raise GenerationContractError(
                    "generated script imports forbidden I/O module"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in GENERATED_SOURCE_FORBIDDEN_IMPORT_ROOTS:
                raise GenerationContractError(
                    "generated script imports forbidden I/O module"
                )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                raise GenerationContractError(
                    "generated script uses an indirect callable"
                )
            if (
                name in GENERATED_SOURCE_UNPROVABLE_TEXT_CALL_NAMES
                and static_text is None
            ):
                raise GenerationContractError(
                    "generated script uses an unprovable dynamic text decoder"
                )
            if name in GENERATED_SOURCE_FORBIDDEN_CALL_NAMES:
                raise GenerationContractError("generated script requests forbidden I/O")
        elif (
            isinstance(node, ast.Name)
            and node.id in GENERATED_SOURCE_FORBIDDEN_DYNAMIC_NAMES
        ):
            raise GenerationContractError(
                "generated script references a forbidden dynamic namespace"
            )
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise GenerationContractError(
                "generated script uses forbidden reflective access"
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _dangerous_generated_text(node.value):
                raise GenerationContractError(
                    "generated script contains source/network/path provenance"
                )
