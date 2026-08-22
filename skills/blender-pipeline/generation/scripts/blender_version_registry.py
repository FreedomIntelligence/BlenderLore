from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests

from project_paths import OUTPUT_ROOT, PROJECT_ROOT


PROJECT = PROJECT_ROOT

VERSION_PATTERNS = [
    re.compile(r"\bblender[\s_\-:]*([2-5]\.\d+(?:\.\d+)?)\b", re.I),
    re.compile(
        r"\b([2-5]\.\d+(?:\.\d+)?)\s*(?:版|版本|教程|基础|入门|course|tutorial)\b", re.I
    ),
    re.compile(r"\bblender\s*([2-5])\s*(?:基础|教程|入门)\b", re.I),
]

FAMILY_DEFAULTS: dict[str, dict[str, Any]] = {
    "5.1": {
        "api_family": "Blender 5.1 Python API",
        "compatibility": "native_current",
        "candidate_paths": [],
    },
    "5.0": {
        "api_family": "Blender 5.x Python API",
        "compatibility": "minor_port_to_5_1",
        "candidate_paths": [],
    },
    "4.3": {
        "api_family": "Blender 4.x Python API",
        "compatibility": "port_to_execution_version",
        "candidate_paths": [],
    },
    "4.2": {
        "api_family": "Blender 4.2 LTS Python API",
        "compatibility": "prefer_matching_lts_else_port",
        "candidate_paths": [],
    },
    "4.1": {
        "api_family": "Blender 4.x Python API",
        "compatibility": "port_to_execution_version",
        "candidate_paths": [],
    },
    "4.0": {
        "api_family": "Blender 4.0 Python API",
        "compatibility": "port_to_execution_version",
        "candidate_paths": [],
    },
    "3.6": {
        "api_family": "Blender 3.6 LTS Python API",
        "compatibility": "prefer_matching_lts_else_port",
        "candidate_paths": [],
    },
    "3.3": {
        "api_family": "Blender 3.3 LTS Python API",
        "compatibility": "prefer_matching_lts_else_port",
        "candidate_paths": [],
    },
    "2.93": {
        "api_family": "Blender 2.93 LTS Python API",
        "compatibility": "legacy_port_to_execution_version",
        "candidate_paths": [],
    },
    "2.83": {
        "api_family": "Blender 2.83 LTS Python API",
        "compatibility": "legacy_port_to_execution_version",
        "candidate_paths": [],
    },
}

DEFAULT_FALLBACK_PATHS = [
    value
    for value in (
        os.environ.get("BLENDER_PIPELINE_BLENDER", "").strip(),
        shutil.which("blender") or "",
    )
    if value
]

DEFAULT_RELEASE_PATCHES = {
    "5.1": ["5.1.1", "5.1.0"],
    "5.0": ["5.0.0"],
    "4.3": ["4.3.2", "4.3.1", "4.3.0"],
    "4.2": ["4.2.0"],
    "4.1": ["4.1.1", "4.1.0"],
    "4.0": ["4.0.2", "4.0.1", "4.0.0"],
    "3.6": ["3.6.0"],
    "3.3": ["3.3.0"],
    "2.93": ["2.93.0"],
    "2.83": ["2.83.0"],
}

SHARED_TOOLS_ROOTS = [
    Path(value).expanduser()
    for value in os.environ.get("BLENDER_PIPELINE_BLENDER_TOOLS_DIRS", "").split(
        os.pathsep
    )
    if value.strip()
]


def read_text(path: Path, limit: int = 40000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[:limit]


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def normalize_version(version: str) -> str:
    parts = re.findall(r"\d+", version)
    if not parts:
        return ""
    if len(parts) == 1:
        return f"{parts[0]}.0"
    return f"{parts[0]}.{parts[1]}" + (f".{parts[2]}" if len(parts) > 2 else "")


def version_family(version: str) -> str:
    version = normalize_version(version)
    if not version:
        return ""
    parts = version.split(".")
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    if major == 2 and minor >= 90:
        return "2.93"
    if major == 2 and minor >= 80:
        return "2.83"
    if major == 3 and minor >= 6:
        return "3.6"
    if major == 3 and minor >= 3:
        return "3.3"
    if major == 4:
        if minor >= 4:
            return f"4.{minor}"
        if minor == 3:
            return "4.3"
        if minor == 2:
            return "4.2"
        if minor == 1:
            return "4.1"
        return "4.0"
    if major == 5:
        if minor >= 2:
            return f"5.{minor}"
        return "5.1" if minor >= 1 else "5.0"
    return version


def extract_versions_from_text(text: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for pattern in VERSION_PATTERNS:
        for match in pattern.finditer(text or ""):
            raw = match.group(1)
            if raw.isdigit():
                raw = f"{raw}.0"
            normalized = normalize_version(raw)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            snippet = text[
                max(0, match.start() - 40) : min(len(text), match.end() + 40)
            ]
            found.append(
                {
                    "version": normalized,
                    "family": version_family(normalized),
                    "evidence": "text",
                    "snippet": " ".join(snippet.split()),
                }
            )
    return found


def parse_blend_header(path: Path) -> str:
    try:
        header = path.read_bytes()[:12].decode("ascii", errors="ignore")
    except Exception:
        return ""
    match = re.search(r"BLENDER[-_a-zA-Z]*v?(\d)(\d{2})", header)
    if not match:
        return ""
    major = match.group(1)
    minor = str(int(match.group(2)))
    return normalize_version(f"{major}.{minor}")


def collect_detection_text(video_dir: Path) -> str:
    info = load_json(video_dir / "source.info.json") or {}
    pieces = [
        video_dir.name,
        str(info.get("title") or ""),
        str(info.get("description") or info.get("desc") or ""),
        read_text(video_dir / "tutorial_path_refs.md", 60000),
        read_text(video_dir / "tutorial.md", 60000),
    ]
    ocr = video_dir / "rich_evidence/ocr_samples.jsonl"
    if ocr.exists():
        pieces.append(read_text(ocr, 40000))
    version_ocr_samples = video_dir / "rich_evidence/blender_version_ocr_samples.jsonl"
    if version_ocr_samples.exists():
        pieces.append(read_text(version_ocr_samples, 40000))
    return "\n".join(pieces)


def detect_ocr_versions(video_dir: Path) -> list[dict[str, str]]:
    data = load_json(video_dir / "rich_evidence/blender_version_ocr.json") or {}
    if data.get("status") != "done":
        return []
    confidence = str(data.get("confidence") or "none")
    detections: list[dict[str, str]] = []
    confirmed = normalize_version(str(data.get("confirmed_version") or ""))
    if confirmed:
        detections.append(
            {
                "version": confirmed,
                "family": version_family(confirmed),
                "evidence": f"video_ocr_confirmed:{confidence}",
                "snippet": f"counts={data.get('version_counts', {})}",
            }
        )
    for item in data.get("detections") or []:
        version = normalize_version(str(item.get("version") or ""))
        if not version:
            continue
        timestamp = item.get("timestamp_sec")
        text = " ".join(str(item.get("ocr_text") or item.get("snippet") or "").split())
        detections.append(
            {
                "version": version,
                "family": version_family(version),
                "evidence": f"video_ocr:{confidence}",
                "snippet": f"t={timestamp}s {text[:180]}".strip(),
            }
        )
    return detections


def detection_priority(item: dict[str, str]) -> int:
    evidence = item.get("evidence", "")
    if evidence.startswith("video_ocr_confirmed:strong"):
        return 100
    if evidence.startswith("blend_header:"):
        return 90
    if evidence.startswith("video_ocr_confirmed:weak"):
        return 78
    if evidence.startswith("video_ocr:strong"):
        return 76
    if evidence.startswith("video_ocr:weak"):
        return 68
    if evidence == "text":
        return 50
    return 40


def detect_source_versions(video_dir: Path) -> list[dict[str, str]]:
    detections = extract_versions_from_text(collect_detection_text(video_dir))
    detections.extend(detect_ocr_versions(video_dir))
    for blend in [video_dir / "asset.blend", video_dir / "source.blend"]:
        version = parse_blend_header(blend)
        if version:
            detections.append(
                {
                    "version": version,
                    "family": version_family(version),
                    "evidence": f"blend_header:{blend.name}",
                    "snippet": blend.name,
                }
            )
    dedup: dict[str, dict[str, str]] = {}
    for item in detections:
        existing = dedup.get(item["version"])
        if not existing or detection_priority(item) > detection_priority(existing):
            dedup[item["version"]] = item
    return sorted(dedup.values(), key=detection_priority, reverse=True)


def executable_version(executable: Path) -> str:
    if not executable.exists():
        return ""
    try:
        proc = subprocess.run(
            [str(executable), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except Exception:
        return ""
    match = re.search(r"Blender\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", proc.stdout)
    return normalize_version(match.group(1)) if match else ""


def first_existing(paths: list[str | Path]) -> Path | None:
    for raw in paths:
        path = Path(raw)
        if path.exists():
            return path
    return None


def release_versions_for(family: str, source_version: str = "") -> list[str]:
    versions: list[str] = []
    normalized = normalize_version(source_version)
    if normalized:
        parts = normalized.split(".")
        if len(parts) == 2:
            normalized = f"{parts[0]}.{parts[1]}.0"
        versions.append(normalized)
    versions.extend(DEFAULT_RELEASE_PATCHES.get(family, []))
    seen: set[str] = set()
    ordered = []
    for version in versions:
        if version and version not in seen:
            seen.add(version)
            ordered.append(version)
    return ordered


def generated_candidate_paths(family: str, source_version: str = "") -> list[str]:
    paths: list[str] = []
    for version in release_versions_for(family, source_version):
        for root in SHARED_TOOLS_ROOTS:
            paths.append(str(root / f"blender-{version}-linux-x64/blender"))
    return paths


def family_record(family: str, source_version: str = "") -> dict[str, Any]:
    rec = dict(FAMILY_DEFAULTS.get(family, {}))
    rec.setdefault("api_family", f"Blender {family or 'unknown'} Python API")
    rec.setdefault("compatibility", "prefer_matching_else_port")
    paths = list(rec.get("candidate_paths", []))
    family_env = "BLENDER_PIPELINE_BLENDER_" + family.replace(".", "_")
    for path in (os.environ.get(family_env, "").strip(), *DEFAULT_FALLBACK_PATHS):
        if path and path not in paths:
            paths.append(path)
    for path in generated_candidate_paths(family, source_version):
        if path not in paths:
            paths.append(path)
    rec["candidate_paths"] = paths
    return rec


def auto_install_enabled() -> bool:
    if os.environ.get("BLENDER_PIPELINE_DISABLE_AUTO_INSTALL_BLENDER") == "1":
        return False
    if os.environ.get("BLENDER_PIPELINE_AUTO_INSTALL_BLENDER") == "1":
        return True
    return platform.system().lower() == "linux"


def install_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in os.environ.get("BLENDER_PIPELINE_BLENDER_TOOLS_DIR", "").split(
        os.pathsep
    ):
        if raw:
            roots.append(Path(raw))
    roots.extend(SHARED_TOOLS_ROOTS)
    return [root for root in roots if root.exists() and os.access(root, os.W_OK)]


def archive_url(version: str) -> str:
    major_minor = ".".join(version.split(".")[:2])
    return f"https://download.blender.org/release/Blender{major_minor}/blender-{version}-linux-x64.tar.xz"


def checksum_url(version: str) -> str:
    major_minor = ".".join(version.split(".")[:2])
    return f"https://download.blender.org/release/Blender{major_minor}/blender-{version}.sha256"


def _parse_release_sha256(text: str, filename: str) -> str:
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+(.+)", line.strip())
        if match and match.group(2) == filename:
            matches.append(match.group(1).lower())
    if len(matches) != 1:
        raise ValueError(f"release checksum entry is missing or ambiguous: {filename}")
    return matches[0]


def _official_release_sha256(version: str) -> str:
    filename = f"blender-{version}-linux-x64.tar.xz"
    with requests.get(
        checksum_url(version),
        headers={"User-Agent": "blender-pipeline/1"},
        timeout=(15, 30),
    ) as response:
        response.raise_for_status()
        parsed = urlparse(response.url)
        if parsed.scheme != "https" or parsed.hostname != "download.blender.org":
            raise ValueError("release checksum redirected outside download.blender.org")
        payload = response.content
    if len(payload) > 1024 * 1024:
        raise ValueError("release checksum document exceeds 1 MiB")
    return _parse_release_sha256(payload.decode("utf-8"), filename)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_official_archive(url: str, destination: Path) -> None:
    with requests.get(
        url,
        headers={"User-Agent": "blender-pipeline/1"},
        stream=True,
        timeout=(30, 300),
    ) as response:
        response.raise_for_status()
        parsed = urlparse(response.url)
        if parsed.scheme != "https" or parsed.hostname != "download.blender.org":
            raise ValueError("release archive redirected outside download.blender.org")
        declared = response.headers.get("Content-Length")
        maximum = 4 * 1024 * 1024 * 1024
        if declared and int(declared) > maximum:
            raise ValueError("release archive exceeds 4 GiB")
        written = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > maximum:
                    raise ValueError("release archive exceeds 4 GiB")
                handle.write(chunk)


def _normalized_archive_path(
    value: str,
    *,
    base: PurePosixPath = PurePosixPath(),
) -> PurePosixPath:
    """Resolve one POSIX archive path without permitting a root escape."""

    raw = PurePosixPath(value)
    if raw.is_absolute():
        raise ValueError(f"archive path is absolute: {value!r}")
    parts = list(base.parts)
    for part in raw.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"archive path escapes extraction root: {value!r}")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise ValueError(f"archive path is empty: {value!r}")
    return PurePosixPath(*parts)


def _extract_tar_safely(archive: Path, destination: Path) -> None:
    """Extract a Blender release after validating every member and link."""

    with tarfile.open(archive) as handle:
        members = handle.getmembers()
        for member in members:
            member_path = _normalized_archive_path(member.name)
            if not (
                member.isfile() or member.isdir() or member.issym() or member.islnk()
            ):
                raise ValueError(
                    f"archive contains an unsupported member type: {member.name!r}"
                )
            if member.issym():
                _normalized_archive_path(
                    member.linkname,
                    base=member_path.parent,
                )
            elif member.islnk():
                _normalized_archive_path(member.linkname)
        # Every member and symbolic/hard-link target is confined above.
        handle.extractall(destination, members=members)  # nosec B202


def install_blender_runtime(family: str, source_version: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"attempted": False, "installed_path": "", "errors": []}
    if not family or not auto_install_enabled():
        result["errors"].append("auto_install_disabled_or_no_family")
        return result
    roots = install_roots()
    if not roots:
        result["errors"].append("no_writable_shared_tools_root")
        return result
    for version in release_versions_for(family, source_version):
        for root in roots:
            exe = root / f"blender-{version}-linux-x64/blender"
            if exe.exists():
                result.update(
                    attempted=False, installed_path=str(exe), reused_existing=True
                )
                return result
    root = roots[0]
    lock = root / f".video2blender_install_{family}.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        owns_lock = True
    except FileExistsError:
        owns_lock = False
    if not owns_lock:
        deadline = time.time() + 900
        while time.time() < deadline:
            existing = first_existing(generated_candidate_paths(family, source_version))
            if existing:
                result.update(
                    attempted=False, installed_path=str(existing), waited_for_lock=True
                )
                return result
            if not lock.exists():
                break
            time.sleep(10)
        result["errors"].append(f"install_lock_timeout:{lock}")
        return result
    try:
        for version in release_versions_for(family, source_version):
            exe = root / f"blender-{version}-linux-x64/blender"
            if exe.exists():
                result.update(
                    attempted=False, installed_path=str(exe), reused_existing=True
                )
                return result
            url = archive_url(version)
            result["attempted"] = True
            result.setdefault("urls", []).append(url)
            download_handle = tempfile.NamedTemporaryFile(
                prefix=f".blender-{version}-",
                suffix=".tar.xz",
                dir=root,
                delete=False,
            )
            download_handle.close()
            archive = Path(download_handle.name)
            extraction_root = Path(
                tempfile.mkdtemp(prefix=f".blender-{version}-extract-", dir=root)
            )
            try:
                _download_official_archive(url, archive)
                expected_sha256 = _official_release_sha256(version)
                if _sha256_file(archive) != expected_sha256:
                    raise RuntimeError("Blender release archive checksum mismatch")
                _extract_tar_safely(archive, extraction_root)
                extracted = extraction_root / f"blender-{version}-linux-x64"
                extracted_exe = extracted / "blender"
                if not extracted_exe.is_file():
                    raise RuntimeError("release archive does not contain Blender")
                if exe.parent.exists():
                    raise RuntimeError(
                        f"incomplete Blender install already exists: {exe.parent}"
                    )
                os.replace(extracted, exe.parent)
                if exe.exists():
                    exe.chmod(exe.stat().st_mode | 0o111)
                    result["installed_path"] = str(exe)
                    return result
                result["errors"].append(f"archive_extracted_but_missing:{exe}")
            except Exception as exc:
                result["errors"].append(f"{version}:{exc!r}")
            finally:
                archive.unlink(missing_ok=True)
                shutil.rmtree(extraction_root, ignore_errors=True)
        return result
    finally:
        lock.unlink(missing_ok=True)


def registry_state_path() -> Path:
    override = os.environ.get("BLENDER_PIPELINE_VERSION_REGISTRY_STATE")
    if override:
        return Path(override)
    return OUTPUT_ROOT / "blender_version_registry_seen.json"


def update_seen_registry(
    detections: list[dict[str, str]], plan: dict[str, Any]
) -> None:
    path = registry_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_json(path) or {"versions": {}, "updated_at": ""}
    versions = data.setdefault("versions", {})
    for item in detections:
        family = item.get("family") or version_family(item.get("version", ""))
        candidates = family_record(family, item.get("version", "")).get(
            "candidate_paths", []
        )
        rec = versions.setdefault(
            item["version"],
            {
                "version": item["version"],
                "family": family,
                "first_seen_at": time.strftime("%F %T"),
                "candidate_paths": candidates,
            },
        )
        rec["last_seen_at"] = time.strftime("%F %T")
        rec["available"] = bool(plan.get("source_family_available"))
        rec["execution_path"] = plan.get("execution", {}).get("path", "")
        rec["compatibility_action"] = plan.get("compatibility_action", "")
        rec["install_status"] = plan.get("install_status", {})
    data["updated_at"] = time.strftime("%F %T")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_version_plan(
    video_dir: Path, fallback_blender: str | Path | None = None
) -> dict[str, Any]:
    detections = detect_source_versions(video_dir)
    source = detections[0] if detections else {}
    family = source.get("family") or ""
    family_rec = family_record(family, source.get("version", "")) if family else {}
    candidates: list[str | Path] = []
    if family_rec:
        candidates.extend(family_rec.get("candidate_paths", []))
    install_status: dict[str, Any] = {}
    if family and not first_existing(candidates):
        install_status = install_blender_runtime(family, source.get("version", ""))
        installed_path = install_status.get("installed_path")
        if installed_path:
            candidates.insert(0, installed_path)
    if fallback_blender:
        candidates.append(str(fallback_blender))
    candidates.extend(DEFAULT_FALLBACK_PATHS)
    selected = first_existing(candidates)
    fallback_selected = first_existing(
        [fallback_blender] if fallback_blender else [] + DEFAULT_FALLBACK_PATHS
    )
    if selected is None:
        selected = fallback_selected
    execution_version = executable_version(selected) if selected else ""
    source_available = bool(
        family_rec and first_existing(family_rec.get("candidate_paths", []))
    )
    if family and source_available:
        action = "execute_with_matching_or_family_blender"
    elif family:
        action = "translate_source_operations_to_execution_blender"
    else:
        action = "unknown_source_version_use_execution_blender"
    plan = {
        "detections": detections,
        "source": {
            "version": source.get("version", ""),
            "family": family,
            "evidence": source.get("evidence", ""),
            "snippet": source.get("snippet", ""),
        },
        "source_family_available": source_available,
        "execution": {
            "path": str(selected) if selected else "",
            "version": execution_version,
            "family": version_family(execution_version) if execution_version else "",
        },
        "compatibility_action": action,
        "install_status": install_status,
        "registry_family": family_rec,
        "policy": [
            "Detect source Blender version from metadata, OCR/tutorial text, and .blend headers when available.",
            "When a new source Blender family appears, register it and try to provision a matching runtime in shared tools on Linux.",
            "Use a matching installed Blender family when available.",
            "If matching runtime is not available, translate operations to the execution Blender API and record the mismatch.",
            "final_reference never overrides tutorial/steps; version plan only controls API/runtime compatibility.",
        ],
    }
    (video_dir / "blender_version_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    update_seen_registry(detections, plan)
    return plan


def prompt_constraints(plan: dict[str, Any]) -> str:
    source = plan.get("source", {})
    execution = plan.get("execution", {})
    source_version = source.get("version") or "unknown"
    execution_version = execution.get("version") or "unknown"
    action = (
        plan.get("compatibility_action")
        or "unknown_source_version_use_execution_blender"
    )
    return f"""
Blender version compatibility:
- Source tutorial Blender version/family: {source_version} / {source.get("family") or "unknown"}.
- Execution Blender version/family: {execution_version} / {execution.get("family") or "unknown"}.
- Compatibility action: {action}.
- Generate code for the execution Blender API, not blindly for the source UI version.
- If tutorial operations come from older Blender versions, translate renamed panels/settings/operators to the execution API.
- Guard version-sensitive APIs with hasattr/getattr or simple fallbacks.
- Geometry Nodes, particles/hair, Eevee/Cycles, material nodes, and add-on APIs changed across versions; prefer stable core bpy APIs and procedural approximations unless evidence requires otherwise.
""".strip()
