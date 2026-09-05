#!/usr/bin/env python3
"""Evidence-first video-to-Blender-tutorial extraction primitives."""

from __future__ import annotations

import base64
import hashlib
import html
import importlib.util
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageOps, ImageStat
import requests


SCHEMA_STEPS = "video2blender-tutorial-steps.v2"
SCHEMA_CANDIDATES = "video2blender-tutorial-candidates.v2"
SCHEMA_MANIFEST = "video2blender-tutorial-manifest.v2"
ALLOWED_MODELS = {"gpt-5.6-sol", "gpt-5.5"}
ALLOWED_PROVIDERS = {"api", "codex-cli"}
UNKNOWN_VALUES = {
    "", "unknown", "uncertain", "none", "n/a", "not visible",
    "未知", "不确定", "不可见", "看不清", "无法确认", "无法辨认",
}
ACTION_TERMS = (
    "添加", "创建", "新建", "选择", "挤出", "缩放", "旋转", "移动", "删除",
    "设置", "调整", "连接", "绑定", "权重", "关键帧", "烘焙", "贴图", "材质",
    "节点", "灯光", "渲染", "add", "create", "select", "extrude", "scale",
    "rotate", "move", "delete", "set", "connect", "bind", "keyframe", "bake",
)


class ExtractionError(RuntimeError):
    """The tutorial cannot be extracted without violating its contract."""


@dataclass(frozen=True)
class Profile:
    name: str
    coarse_interval: float
    dense_interval: float
    dense_radius: float
    calls_per_ten_minutes: int
    coarse_frames_per_window: int
    image_max_side: int


PROFILES = {
    "economy": Profile("economy", 2.0, 0.5, 1.5, 8, 6, 900),
    "balanced": Profile("balanced", 1.0, 0.25, 2.0, 16, 8, 1200),
    "forensic": Profile("forensic", 0.5, 0.125, 3.0, 32, 12, 1500),
}


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reported_calls: int = 0
    response_model: str = ""
    finish_reason: str = ""

    def add(
        self, value: Any, *, response_model: str = "", finish_reason: str = ""
    ) -> None:
        if not isinstance(value, Mapping):
            raise ExtractionError("model response is missing per-call usage")
        try:
            prompt = int(value.get("prompt_tokens"))
            completion = int(value.get("completion_tokens"))
            total = int(value.get("total_tokens"))
        except (TypeError, ValueError) as exc:
            raise ExtractionError("model response has invalid per-call usage") from exc
        if prompt < 0 or completion < 0 or total <= 0 or total < prompt + completion:
            raise ExtractionError("model response has inconsistent per-call usage")
        self.calls += 1
        self.reported_calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        self.response_model = response_model
        self.finish_reason = finish_reason


@dataclass
class TranscriptResult:
    status: str
    source: str
    segments: list[dict[str, Any]] = field(default_factory=list)
    warning: str = ""
    attempted_sources: list[str] = field(default_factory=list)
    rejected_segment_count: int = 0
    quality_reasons: list[str] = field(default_factory=list)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def reject_sensitive_echo(value: Any, *, key: str, endpoint: str) -> None:
    """Fail closed if a provider mirrors credentials or its private endpoint."""

    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    lowered = serialized.casefold()
    if key and key in serialized:
        raise ExtractionError("model response contained forbidden credential material")
    if endpoint and endpoint in serialized:
        raise ExtractionError("model response contained forbidden provider metadata")
    if "authorization: bearer" in lowered or '"api_key"' in lowered:
        raise ExtractionError("model response contained forbidden provider metadata")


def model_identity_matches(requested: str, observed: str) -> bool:
    """Allow deployment suffixes without permitting a model-family downgrade."""

    normalized = re.sub(r"[^a-z0-9]+", "", observed.casefold())
    if requested == "gpt-5.6-sol":
        return "gpt56sol" in normalized
    if requested == "gpt-5.5":
        return "gpt55" in normalized and "gpt56" not in normalized
    return False


def validate_model_fallback(model: str, fallback_reason: str = "") -> str:
    """Enforce an auditable whole-run fallback without weakening 5.6 runs."""

    if model not in ALLOWED_MODELS:
        raise ExtractionError("model must be gpt-5.6-sol or explicit fallback gpt-5.5")
    reason = fallback_reason.strip()
    if len(reason) > 500 or any(character in reason for character in "\r\n"):
        raise ExtractionError("--fallback-reason must be a single line of at most 500 characters")
    if model == "gpt-5.5" and not reason:
        raise ExtractionError("gpt-5.5 requires a non-empty --fallback-reason")
    if model == "gpt-5.6-sol" and reason:
        raise ExtractionError("--fallback-reason is forbidden when using gpt-5.6-sol")
    return reason


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    atomic_text(
        path,
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
    )


def run_command(
    command: Sequence[str], *, timeout: int = 600, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ExtractionError(f"required executable is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"command timed out: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[-1000:]
        raise ExtractionError(f"command failed: {command[0]}: {detail}") from exc


def ffprobe_duration(video: Path) -> float:
    result = run_command(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video),
        ],
        timeout=60,
        capture=True,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ExtractionError("ffprobe returned an invalid video duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ExtractionError("video duration must be positive")
    return duration


def extract_frame(video: Path, timestamp: float, output: Path, max_side: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    attempts = [timestamp, max(0.0, timestamp - 0.2), timestamp + 0.2]
    for value in attempts:
        try:
            run_command(
                [
                    "ffmpeg", "-y", "-ss", f"{value:.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf",
                    f"scale='min({max_side},iw)':-2", str(output),
                ],
                timeout=90,
            )
        except ExtractionError:
            output.unlink(missing_ok=True)
            continue
        if output.is_file() and output.stat().st_size:
            return
    raise ExtractionError(f"could not decode frame at {timestamp:.3f}s")


def validate_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ExtractionError("--video-url must be a credential-free HTTPS URL")
    return value


def _yt_dlp_command() -> list[str] | None:
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def _platform_subtitle_paths(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.glob("subtitle*")
        if path.suffix.lower() in {".vtt", ".srt", ".json", ".json3"}
    )


def fetch_platform_subtitles(url: str, root: Path) -> list[Path]:
    """Best-effort platform subtitle retrieval without downloading the video."""

    yt_dlp = _yt_dlp_command()
    if yt_dlp is None:
        return []
    root.mkdir(parents=True, exist_ok=True)
    try:
        run_command(
            [
                *yt_dlp, "--no-playlist", "--skip-download", "--write-subs",
                "--write-auto-subs", "--sub-langs", "zh-Hans,zh-CN,zh,en",
                "--sub-format", "vtt/srt/json3/best", "-o",
                str(root / "subtitle"), url,
            ],
            timeout=300,
        )
    except ExtractionError:
        return []
    return _platform_subtitle_paths(root)


def _known_caption_platform(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return (
        host == "bilibili.com"
        or host.endswith(".bilibili.com")
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtu.be"
    )


def materialize_url(url: str, root: Path) -> tuple[Path, list[Path]]:
    yt_dlp = _yt_dlp_command()
    if yt_dlp is None:
        raise ExtractionError("--video-url requires the yt-dlp executable or Python module")
    root.mkdir(parents=True, exist_ok=True)
    video_template = root / "source.%(ext)s"
    run_command(
        [
            *yt_dlp, "--no-playlist", "--merge-output-format", "mp4",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
            "-o", str(video_template), url,
        ],
        timeout=1800,
    )
    videos = sorted(
        path for path in root.glob("source.*") if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
    )
    if not videos:
        raise ExtractionError("yt-dlp produced no video file")
    subtitles = fetch_platform_subtitles(url, root)
    return videos[0], subtitles


def _clock_seconds(value: str) -> float:
    fields = value.strip().replace(",", ".").split(":")
    if not fields:
        return 0.0
    try:
        numbers = [float(part) for part in fields]
    except ValueError:
        return 0.0
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def _clean_caption(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\{\\[^}]+\}", "", value)
    return " ".join(value.replace("\\N", " ").split())


def parse_transcript(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8", errors="ignore")
    rows: list[dict[str, Any]] = []
    if suffix in {".json", ".json3"}:
        value = json.loads(text)
        if isinstance(value, Mapping):
            raw = (
                value.get("body")
                or value.get("events")
                or value.get("segments")
                or []
            )
        elif isinstance(value, list):
            raw = value
        else:
            raw = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                if "segs" in item:
                    caption = "".join(
                        str(part.get("utf8") or "")
                        for part in item.get("segs") or []
                        if isinstance(part, Mapping)
                    )
                    start = float(item.get("tStartMs") or 0) / 1000.0
                    end = start + float(item.get("dDurationMs") or 0) / 1000.0
                else:
                    caption = str(item.get("content") or item.get("text") or "")
                    start = float(item.get("from") or item.get("start_sec") or item.get("start") or 0)
                    end = float(item.get("to") or item.get("end_sec") or item.get("end") or start)
                caption = _clean_caption(caption)
                if caption:
                    rows.append({"start_sec": start, "end_sec": max(start, end), "text": caption})
        return rows
    if suffix == ".jsonl":
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, Mapping):
                continue
            caption = _clean_caption(str(item.get("text") or item.get("content") or ""))
            if caption:
                start = float(item.get("start_sec") or item.get("start") or 0)
                end = float(item.get("end_sec") or item.get("end") or start)
                rows.append({"start_sec": start, "end_sec": max(start, end), "text": caption})
        return rows
    if suffix in {".srt", ".vtt"}:
        lines = text.replace("\r", "").splitlines()
        for index, line in enumerate(lines):
            if "-->" not in line:
                continue
            left, right = line.split("-->", 1)
            caption_lines: list[str] = []
            cursor = index + 1
            while cursor < len(lines) and lines[cursor].strip():
                caption_lines.append(lines[cursor])
                cursor += 1
            caption = _clean_caption(" ".join(caption_lines))
            if caption:
                rows.append(
                    {
                        "start_sec": _clock_seconds(left.split()[0]),
                        "end_sec": _clock_seconds(right.split()[0]),
                        "text": caption,
                    }
                )
        return rows
    # Untimestamped prose is not accepted as operational time evidence.
    return rows


def _transcript_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in re.finditer(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        value = match.group(0)
        if "\u3400" <= value[0] <= "\u9fff":
            # Single Han characters make every normal long Chinese transcript
            # look low-diversity.  Sliding bigrams preserve phrase repetition
            # (for example repeated silent-audio hallucinations) without
            # rejecting ordinary technical narration full of common 字/词.
            if len(value) == 1:
                tokens.append(value)
            else:
                tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(value)
    return tokens


def _segment_quality_reasons(text: str) -> list[str]:
    """Detect obvious ASR corruption without judging tutorial semantics."""

    visible = [character for character in text if not character.isspace()]
    if not visible:
        return ["empty"]
    reasons: list[str] = []
    replacement_count = text.count("\ufffd")
    if replacement_count >= 2 or replacement_count / len(visible) >= 0.08:
        reasons.append("replacement_character_ratio")
    meaningful = [
        character
        for character in visible
        if (character.isascii() and character.isalnum())
        or "\u3400" <= character <= "\u9fff"
    ]
    if len(visible) >= 4 and len(meaningful) / len(visible) < 0.35:
        reasons.append("meaningful_character_ratio")
    tokens = _transcript_tokens(text)
    if len(tokens) >= 6:
        counts = Counter(tokens)
        dominant_ratio = counts.most_common(1)[0][1] / len(tokens)
        diversity = len(counts) / len(tokens)
        if dominant_ratio >= 0.70 and len(counts.most_common(1)[0][0]) <= 3:
            reasons.append("short_token_repetition")
        if diversity <= 0.12 and sum(
            count for _token, count in counts.most_common(3)
        ) / len(tokens) >= 0.90:
            reasons.append("vocabulary_diversity")
    return reasons


def transcript_quality_gate(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    accepted: list[dict[str, Any]] = []
    rejected = 0
    reason_counts: Counter[str] = Counter()
    for row in rows:
        text = _clean_caption(str(row.get("text") or ""))
        reasons = _segment_quality_reasons(text)
        if reasons:
            rejected += 1
            reason_counts.update(reasons)
            continue
        accepted.append({**dict(row), "text": text})

    # Whisper often hallucinates the same one- or two-word phrase once per
    # chunk on silent/music-only inputs. Individual chunks look plausible, so
    # apply a conservative whole-transcript diversity check as well.
    tokens = [
        token
        for row in accepted
        for token in _transcript_tokens(str(row.get("text") or ""))
    ]
    if len(tokens) >= 6:
        counts = Counter(tokens)
        diversity = len(counts) / len(tokens)
        top_three_ratio = sum(
            count for _token, count in counts.most_common(3)
        ) / len(tokens)
        short_repeated_ratio = sum(
            count
            for token, count in counts.items()
            if len(token) <= 3 and count >= 3
        ) / len(tokens)
        dominant_token, dominant_count = counts.most_common(1)[0]
        dominant_ratio = dominant_count / len(tokens)
        if (
            len(tokens) <= 11
            and len(dominant_token) <= 3
            and dominant_ratio >= 0.80
        ) or (diversity <= 0.12 and top_three_ratio >= 0.90) or (
            diversity <= 0.20 and short_repeated_ratio >= 0.80
        ):
            rejected += len(accepted)
            accepted = []
            reason_counts["whole_transcript_repetition"] += 1
    reasons = [
        f"{name}:{count}" for name, count in sorted(reason_counts.items())
    ]
    return accepted, rejected, reasons


def _accepted_transcript_result(
    source: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    attempted_sources: list[str],
) -> TranscriptResult | None:
    accepted, rejected, reasons = transcript_quality_gate(rows)
    if not accepted:
        return None
    warning = ""
    if rejected:
        warning = (
            f"Transcript quality gate removed {rejected} corrupted or repetitive "
            "segment(s)."
        )
    return TranscriptResult(
        "ok",
        source,
        accepted,
        warning,
        attempted_sources,
        rejected,
        reasons,
    )


CHINESE_BLENDER_ASR_PROMPT = (
    "Blender，材质，着色器，节点，建模，渲染，贴图，UV，烘焙，灯光，"
    "绑定，蒙皮，骨骼，动画，几何节点，修改器，关键帧。"
)


def local_asr(video: Path, *, language_hint: str | None = None) -> TranscriptResult:
    model_name = os.environ.get("VIDEO2BLENDER_ASR_MODEL", "small")
    normalized_language = str(language_hint or "").strip().lower() or None
    if normalized_language is not None and not re.fullmatch(
        r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", normalized_language
    ):
        raise ValueError("ASR language hint must be a BCP-47-like language code")
    initial_prompt = (
        CHINESE_BLENDER_ASR_PROMPT
        if normalized_language in {"zh", "zh-cn", "zh-tw"}
        else None
    )
    rejected_reasons: list[str] = []
    rejected_count = 0
    try:
        import mlx_whisper  # type: ignore

        model = os.environ.get("VIDEO2BLENDER_MLX_ASR_MODEL", "mlx-community/whisper-small-mlx-q4")
        mlx_options: dict[str, Any] = {"path_or_hf_repo": model}
        if normalized_language is not None:
            mlx_options["language"] = normalized_language
        if initial_prompt is not None:
            mlx_options["initial_prompt"] = initial_prompt
        value = mlx_whisper.transcribe(str(video), **mlx_options)
        raw = value.get("segments") or []
        rows = [
            {"start_sec": float(item.get("start") or 0), "end_sec": float(item.get("end") or 0), "text": _clean_caption(str(item.get("text") or ""))}
            for item in raw if isinstance(item, Mapping) and str(item.get("text") or "").strip()
        ]
        if rows:
            result = _accepted_transcript_result(
                "local_asr:mlx-whisper",
                rows,
                attempted_sources=["local_asr:mlx-whisper"],
            )
            if result is not None:
                return result
            _accepted, _rejected, reasons = transcript_quality_gate(rows)
            rejected_count += _rejected
            rejected_reasons.extend(reasons)
    except (ImportError, RuntimeError, OSError, ValueError):
        pass
    try:
        import whisper  # type: ignore

        whisper_options: dict[str, Any] = {}
        if normalized_language is not None:
            whisper_options["language"] = normalized_language
        if initial_prompt is not None:
            whisper_options["initial_prompt"] = initial_prompt
        value = whisper.load_model(model_name).transcribe(str(video), **whisper_options)
        rows = [
            {"start_sec": float(item.get("start") or 0), "end_sec": float(item.get("end") or 0), "text": _clean_caption(str(item.get("text") or ""))}
            for item in value.get("segments") or [] if isinstance(item, Mapping) and str(item.get("text") or "").strip()
        ]
        if rows:
            result = _accepted_transcript_result(
                "local_asr:openai-whisper",
                rows,
                attempted_sources=[
                    "local_asr:mlx-whisper",
                    "local_asr:openai-whisper",
                ],
            )
            if result is not None:
                return result
            _accepted, _rejected, reasons = transcript_quality_gate(rows)
            rejected_count += _rejected
            rejected_reasons.extend(reasons)
    except (ImportError, RuntimeError, OSError, ValueError):
        pass
    try:
        from faster_whisper import WhisperModel  # type: ignore

        device = os.environ.get("VIDEO2BLENDER_ASR_DEVICE", "cpu")
        faster_options: dict[str, Any] = {"vad_filter": True}
        if normalized_language is not None:
            faster_options["language"] = normalized_language
        if initial_prompt is not None:
            faster_options["initial_prompt"] = initial_prompt
        segments, _info = WhisperModel(model_name, device=device).transcribe(
            str(video), **faster_options
        )
        rows = [
            {"start_sec": float(item.start), "end_sec": float(item.end), "text": _clean_caption(str(item.text))}
            for item in segments if str(item.text).strip()
        ]
        if rows:
            result = _accepted_transcript_result(
                "local_asr:faster-whisper",
                rows,
                attempted_sources=[
                    "local_asr:mlx-whisper",
                    "local_asr:openai-whisper",
                    "local_asr:faster-whisper",
                ],
            )
            if result is not None:
                return result
            _accepted, _rejected, reasons = transcript_quality_gate(rows)
            rejected_count += _rejected
            rejected_reasons.extend(reasons)
    except (ImportError, RuntimeError, OSError, ValueError):
        pass
    return TranscriptResult(
        "unavailable",
        "none",
        [],
        (
            "No usable platform/user transcript and no working local Whisper "
            "result; empty or low-quality/repetitive ASR was rejected and "
            "extraction continued with visual/OCR evidence only."
        ),
        ["local_asr:mlx-whisper", "local_asr:openai-whisper", "local_asr:faster-whisper"],
        rejected_count,
        sorted(set(rejected_reasons)),
    )


def resolve_transcript(
    video: Path,
    provided: Path | None,
    platform_candidates: Sequence[Path],
    *,
    platform_attempted: bool = False,
    allow_local_asr: bool = True,
    asr_language_hint: str | None = None,
) -> TranscriptResult:
    attempted: list[str] = (
        ["platform_subtitle"] if platform_attempted or platform_candidates else []
    )
    rejected_count = 0
    rejected_reasons: list[str] = []

    def merge_prior_rejections(result: TranscriptResult) -> TranscriptResult:
        if rejected_count:
            result.rejected_segment_count += rejected_count
            result.quality_reasons = sorted(
                set(result.quality_reasons) | set(rejected_reasons)
            )
            prefix = (
                f"Transcript quality gate rejected {rejected_count} segment(s) "
                "from a higher-priority source. "
            )
            result.warning = prefix + result.warning
        return result

    for path in platform_candidates:
        try:
            rows = parse_transcript(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if rows:
            result = _accepted_transcript_result(
                "platform_subtitle", rows, attempted_sources=attempted
            )
            if result is not None:
                return merge_prior_rejections(result)
            _accepted, rejected, reasons = transcript_quality_gate(rows)
            rejected_count += rejected
            rejected_reasons.extend(reasons)
    provided_warning = ""
    if provided is not None:
        attempted.append("provided")
        try:
            rows = parse_transcript(provided)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            rows = []
        if rows:
            result = _accepted_transcript_result(
                "provided", rows, attempted_sources=attempted
            )
            if result is not None:
                return merge_prior_rejections(result)
            _accepted, rejected, reasons = transcript_quality_gate(rows)
            rejected_count += rejected
            rejected_reasons.extend(reasons)
        provided_warning = (
            "Provided transcript contained no timestamped, readable, or "
            "quality-gate-accepted text. "
        )
    if not allow_local_asr:
        warning = provided_warning + (
            "Local ASR was intentionally disabled because the caller supplied "
            "the canonical transcript used by every benchmark arm."
        )
        return TranscriptResult(
            "unavailable",
            "none",
            [],
            warning,
            attempted,
            rejected_count,
            sorted(set(rejected_reasons)),
        )
    result = local_asr(video, language_hint=asr_language_hint)
    result.attempted_sources = attempted + result.attempted_sources
    result.rejected_segment_count += rejected_count
    result.quality_reasons = sorted(
        set(result.quality_reasons) | set(rejected_reasons)
    )
    if provided_warning:
        result.warning = provided_warning + result.warning
    return result


def _ocr_crop(image: Image.Image, region: str) -> Image.Image:
    width, height = image.size
    boxes = {
        "full": (0, 0, width, height),
        "right_ui": (int(width * 0.52), 0, width, height),
        "node_editor": (0, int(height * 0.50), width, height),
        "timeline": (0, int(height * 0.72), width, height),
        "properties": (int(width * 0.62), 0, width, height),
    }
    return image.crop(boxes.get(region, boxes["full"]))


def ocr_image(path: Path, timestamp: float, regions: Sequence[str] = ("right_ui", "node_editor", "timeline")) -> list[dict[str, Any]]:
    executable = shutil.which("tesseract")
    if not executable:
        return []
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    observations: list[dict[str, Any]] = []
    for region in regions:
        crop = _ocr_crop(image, region)
        crop = ImageOps.autocontrast(ImageOps.grayscale(crop))
        crop = ImageEnhance.Contrast(crop).enhance(1.6)
        crop = crop.resize((max(1, crop.width * 2), max(1, crop.height * 2)))
        with tempfile.NamedTemporaryFile(suffix=".png") as handle:
            crop.save(handle.name)
            command = [executable, handle.name, "stdout", "-l", "chi_sim+eng", "--psm", "6"]
            try:
                result = subprocess.run(
                    command, check=False, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, timeout=15,
                )
            except subprocess.TimeoutExpired:
                continue
            text = " ".join(result.stdout.split())
            if not text and "chi_sim" in " ".join(command):
                result = subprocess.run(
                    [executable, handle.name, "stdout", "-l", "eng", "--psm", "6"],
                    check=False, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, timeout=15,
                )
                text = " ".join(result.stdout.split())
        if text:
            observations.append({"timestamp_sec": round(timestamp, 3), "region": region, "text": text})
    return observations


def image_change_score(previous: Path | None, current: Path) -> float:
    if previous is None:
        return 1.0
    left = Image.open(previous).convert("L").resize((160, 160))
    right = Image.open(current).convert("L").resize((160, 160))

    def score(box: tuple[int, int, int, int], denominator: float) -> float:
        difference = ImageChops.difference(left.crop(box), right.crop(box))
        return min(1.0, ImageStat.Stat(difference).mean[0] / denominator)

    # Full-frame motion alone tends to hide one-frame subtitle changes and
    # small node-value edits. The two focused bands keep those events eligible
    # for the limited coarse evidence sheet without running OCR on every frame.
    return max(
        score((0, 0, 160, 160), 64.0),
        score((0, 62, 160, 104), 40.0),
        score((0, 80, 160, 160), 56.0),
    )


def frange(start: float, stop: float, step: float) -> Iterable[float]:
    cursor = start
    while cursor < stop:
        yield round(cursor, 3)
        cursor += step


def timestamp_label(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    return f"{whole // 60:02d}:{whole % 60:02d}"


def parse_time_range(value: Any, fallback_start: float, fallback_end: float) -> tuple[float, float]:
    text = str(value or "")
    matches = re.findall(r"(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?s", text)
    if matches:
        values = [_clock_seconds(item[:-1] if item.endswith("s") else item) for item in matches[:2]]
        if len(values) == 1:
            return values[0], values[0]
        return min(values), max(values)
    return fallback_start, fallback_end


def segments_text(segments: Sequence[Mapping[str, Any]], start: float, end: float, limit: int = 5000) -> str:
    pieces = []
    for item in segments:
        item_start = float(item.get("start_sec") or 0)
        item_end = float(item.get("end_sec") or item_start)
        if item_start <= end and item_end >= start:
            pieces.append(f"[{timestamp_label(item_start)}] {item.get('text', '')}")
    return "\n".join(pieces)[:limit]


def action_cue_timestamps(segments: Sequence[Mapping[str, Any]]) -> list[float]:
    return [
        float(item.get("start_sec") or 0)
        for item in segments
        if any(term in str(item.get("text") or "").lower() for term in ACTION_TERMS)
    ]


def select_coarse_rows(
    rows: Sequence[Mapping[str, Any]], *, cue_times: Sequence[float], limit: int
) -> list[Mapping[str, Any]]:
    """Select regular anchors, change peaks, and ASR cues before costly OCR."""

    if not rows or limit < 1:
        return []
    last = len(rows) - 1
    selected_indices = {
        round(slot * last / max(1, min(3, last)))
        for slot in range(min(4, len(rows)))
    }
    if len(selected_indices) > limit:
        selected_indices = set(sorted(selected_indices)[:limit])
    for cue in cue_times:
        if len(selected_indices) >= limit:
            break
        nearest = min(
            range(len(rows)),
            key=lambda index: abs(
                float(rows[index].get("timestamp_sec") or 0) - cue
            ),
        )
        selected_indices.add(nearest)
    for index in sorted(
        range(len(rows)),
        key=lambda value: float(rows[value].get("change_score") or 0),
        reverse=True,
    ):
        if len(selected_indices) >= limit:
            break
        selected_indices.add(index)
    selected = [rows[index] for index in selected_indices]
    return sorted(selected, key=lambda item: float(item.get("timestamp_sec") or 0))


def make_contact_sheet(
    items: Sequence[tuple[str, Path]],
    output: Path,
    columns: int = 3,
    *,
    tile_size: tuple[int, int] = (420, 270),
) -> None:
    if not items:
        raise ExtractionError("cannot create an empty evidence contact sheet")
    tile_w, tile_h = tile_size
    if tile_w < 320 or tile_h < 220:
        raise ExtractionError("contact-sheet tiles are too small for UI evidence")
    rows = math.ceil(len(items) / columns)
    sheet = Image.new("RGB", (tile_w * columns, tile_h * rows), "#111111")
    draw = ImageDraw.Draw(sheet)
    for index, (label, path) in enumerate(items):
        image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        image.thumbnail((tile_w - 12, tile_h - 38))
        x = (index % columns) * tile_w
        y = (index // columns) * tile_h
        sheet.paste(image, (x + (tile_w - image.width) // 2, y + 32))
        draw.text((x + 8, y + 8), label, fill="white")
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=90)


def verification_sheet_columns(evidence_count: int) -> int:
    """Keep dense five-candidate evidence legible after image-size capping."""

    return 4 if evidence_count > 16 else 3


def build_candidate_verification_packages(
    *,
    candidates: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    output_root: Path,
    sheet_root: Path,
) -> list[dict[str, Any]]:
    """Build one closed visual-evidence package per candidate.

    Verification calls may still batch multiple candidates to respect the
    profile's model-call budget, but each attached contact sheet contains only
    evidence owned by its mapped step. This prevents a visually similar action
    elsewhere in the same analysis window from becoming implicit evidence for
    the candidate being judged.
    """

    role_order = {"pre": 0, "action": 1, "stable": 2, "ocr_best": 3}
    claimed_image_ids: set[str] = set()
    packages: list[dict[str, Any]] = []
    for candidate in candidates:
        step_id = str(candidate.get("step_id") or "").strip()
        if not step_id:
            raise ExtractionError("verification candidate has no step_id")
        owned = sorted(
            (
                item
                for item in evidence
                if str(item.get("step_id") or "") == step_id
            ),
            key=lambda item: (
                role_order.get(str(item.get("role") or ""), 99),
                float(item.get("timestamp_sec") or 0),
                str(item.get("image_id") or ""),
            ),
        )
        if not owned:
            # The caller records a missing verification decision and Q-Gate
            # rejects the candidate. Never create an empty or shared sheet.
            continue
        image_ids = [str(item.get("image_id") or "") for item in owned]
        if any(not image_id for image_id in image_ids):
            raise ExtractionError("verification evidence has no image_id")
        duplicate_ids = claimed_image_ids.intersection(image_ids)
        if duplicate_ids:
            raise ExtractionError(
                "verification evidence is shared across candidate packages: "
                + ", ".join(sorted(duplicate_ids))
            )
        claimed_image_ids.update(image_ids)
        safe_step_id = re.sub(r"[^A-Za-z0-9_-]+", "-", step_id)
        sheet = sheet_root / f"candidate_{safe_step_id}.jpg"
        make_contact_sheet(
            [
                (
                    f"{item['image_id']} | {float(item['timestamp_sec']):.2f}s | "
                    f"{item['role']} | {item['region']}",
                    output_root / str(item["path"]),
                )
                for item in owned
            ],
            sheet,
            columns=2,
            # Candidate isolation prevents semantic leakage; the larger tiles
            # preserve Blender node labels and parameter fields that became
            # unreadable in the former many-candidate window sheet.
            tile_size=(640, 400),
        )
        packages.append(
            {
                "step_id": step_id,
                "candidate": candidate,
                "evidence": owned,
                "sheet": sheet,
            }
        )
    return packages


def encode_image(path: Path, max_side: int) -> str:
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    image.thumbnail((max_side, max_side))
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=86, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def read_secret(path: Path) -> str:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ExtractionError("API key file must not be a symlink")
    resolved = candidate.resolve(strict=True)
    metadata = resolved.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ExtractionError("API key file must be owner-owned regular file mode 0600")
    value = resolved.read_text(encoding="utf-8").strip()
    if not value or any(character.isspace() for character in value):
        raise ExtractionError("API key file content is empty or invalid")
    return value


def parse_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ExtractionError("model response contains no JSON object")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ExtractionError("model response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ExtractionError("model response must be a JSON object")
    return value


def response_text(value: Mapping[str, Any]) -> str:
    choices = value.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") if isinstance(item, Mapping) else str(item)
            for item in content
        )
    return ""


def parse_sse_chat_response(
    raw: bytes, *, expected_model: str
) -> tuple[dict[str, Any], str, str]:
    """Parse a complete OpenAI-compatible SSE response and fail closed."""

    if len(raw) > 32 * 1024 * 1024:
        raise ExtractionError("model SSE response exceeds 32 MiB")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExtractionError("model SSE response is not UTF-8") from exc
    events: list[Mapping[str, Any]] = []
    data_lines: list[str] = []
    done_seen = False

    def consume() -> None:
        nonlocal done_seen
        if not data_lines:
            return
        payload = "\n".join(data_lines).strip()
        data_lines.clear()
        if not payload:
            return
        if payload == "[DONE]":
            done_seen = True
            return
        if done_seen:
            raise ExtractionError("model SSE emitted data after [DONE]")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExtractionError("model SSE event is not JSON") from exc
        if not isinstance(value, Mapping):
            raise ExtractionError("model SSE JSON root is not an object")
        events.append(value)

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            consume()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    consume()
    if not done_seen:
        raise ExtractionError("model SSE stream ended without [DONE]")
    if not events:
        raise ExtractionError("model SSE stream contains no JSON events")

    content_parts: list[str] = []
    models: set[str] = set()
    finish_reasons: set[str] = set()
    usage: Mapping[str, Any] | None = None
    response_ids: set[str] = set()
    for event in events:
        if isinstance(event.get("error"), Mapping):
            raise ExtractionError("model SSE stream contains an error event")
        model = str(event.get("model") or "").strip()
        if not model:
            raise ExtractionError("model SSE event contains no model identity")
        models.add(model)
        if isinstance(event.get("id"), str) and event["id"]:
            response_ids.add(str(event["id"]))
        if event.get("usage") is not None:
            if not isinstance(event.get("usage"), Mapping):
                raise ExtractionError("model SSE usage receipt is invalid")
            usage = event["usage"]
        choices = event.get("choices")
        if choices is None:
            continue
        if not isinstance(choices, list):
            raise ExtractionError("model SSE choices is not an array")
        for choice in choices:
            if not isinstance(choice, Mapping) or choice.get("index") not in (None, 0):
                raise ExtractionError("model SSE returned an invalid or extra choice")
            delta = choice.get("delta")
            if delta is not None:
                if not isinstance(delta, Mapping):
                    raise ExtractionError("model SSE delta is not an object")
                piece = delta.get("content")
                if isinstance(piece, str):
                    content_parts.append(piece)
                elif isinstance(piece, list):
                    for item in piece:
                        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                            content_parts.append(str(item["text"]))
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str) or not finish_reason:
                    raise ExtractionError("model SSE finish_reason is invalid")
                finish_reasons.add(finish_reason)
    accepted_identity = os.environ.get(
        "BLENDER_PIPELINE_EXPECTED_RESPONSE_MODEL", expected_model
    ).strip()
    if not model_identity_matches(expected_model, accepted_identity):
        raise ExtractionError(
            "configured response model identity is outside the requested model family"
        )
    if models != {accepted_identity}:
        raise ExtractionError(
            "model SSE identity mismatch: "
            f"requested={expected_model}, expected_response={accepted_identity}, received={sorted(models)}"
        )
    if len(response_ids) > 1:
        raise ExtractionError("model SSE response ID changed during the stream")
    if finish_reasons != {"stop"}:
        raise ExtractionError(
            f"model SSE did not finish cleanly: {sorted(finish_reasons)}"
        )
    if not isinstance(usage, Mapping):
        raise ExtractionError("model SSE response contains no usage receipt")
    parsed_usage: dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExtractionError(f"model SSE usage has invalid {name}")
        parsed_usage[name] = value
    if parsed_usage["total_tokens"] < (
        parsed_usage["prompt_tokens"] + parsed_usage["completion_tokens"]
    ):
        raise ExtractionError("model SSE usage totals are inconsistent")
    content = "".join(content_parts).strip()
    if not content:
        raise ExtractionError("model SSE stream contains no output text")
    aggregated = {
        "model": accepted_identity,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": parsed_usage,
    }
    return aggregated, accepted_identity, "stop"


class ModelClient:
    def __init__(self, *, endpoint: str, key: str, model: str, profile: Profile, call_budget: int,
                 max_output_tokens: int = 10000):
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ExtractionError("BLENDER_PIPELINE_API_ENDPOINT must be credential-free HTTPS")
        if model not in ALLOWED_MODELS:
            raise ExtractionError("model must be gpt-5.6-sol or explicit fallback gpt-5.5")
        self.endpoint = endpoint
        self.key = key
        self.model = model
        self.profile = profile
        self.call_budget = call_budget
        self.max_output_tokens = max_output_tokens
        self.usage = Usage()

    def call(self, prompt: str, image: Path | Sequence[Path]) -> dict[str, Any]:
        if self.usage.calls >= self.call_budget:
            raise ExtractionError(f"model call budget exhausted ({self.call_budget})")
        images = [image] if isinstance(image, Path) else list(image)
        if not images:
            raise ExtractionError("model call requires at least one evidence image")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for index, item in enumerate(images):
            content.extend(
                [
                    {
                        "type": "text",
                        "text": f"EVIDENCE_SHEET_{index:03d}: {item.name}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + encode_image(item, self.profile.image_max_side)
                        },
                    },
                ]
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "reasoning_effort": "low",
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
        }
        wire_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Idempotency-Key": canonical_sha256(
                {
                    "model": self.model,
                    "prompt": prompt,
                    "images": [sha256_path(item) for item in images],
                }
            ),
        }
        session = requests.Session()
        # Match the transport already proven by the production rich
        # extractor.  In particular, never inherit HTTP(S)_PROXY for a bearer
        # request and never deliver credentials across a redirect.
        session.trust_env = False
        try:
            response = session.post(
                self.endpoint,
                headers=headers,
                data=wire_body,
                timeout=(30, 660),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ExtractionError(
                "model delivery status is uncertain; refusing automatic retry; "
                f"transport={type(exc).__name__}"
            ) from exc
        raw = bytes(response.content)
        if response.status_code >= 300:
            body_digest = hashlib.sha256(raw[: 1024 * 1024]).hexdigest()
            provider_code = ""
            try:
                error_body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_body = None
            if isinstance(error_body, Mapping):
                error_value = error_body.get("error")
                if isinstance(error_value, Mapping):
                    candidate_code = str(error_value.get("code") or "").strip()
                    # Expose only a bounded machine-readable classification,
                    # never the provider message, request ID, body, endpoint,
                    # or credential.  This makes quota failures actionable
                    # without weakening the existing redaction contract.
                    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", candidate_code):
                        provider_code = candidate_code
            code_detail = (
                f"; provider_error_code={provider_code}" if provider_code else ""
            )
            raise ExtractionError(
                f"model request failed with HTTP {response.status_code}; "
                f"body_sha256={body_digest}{code_detail}"
            )
        value, response_model, finish_reason = parse_sse_chat_response(
            raw, expected_model=self.model
        )
        self.usage.add(
            value.get("usage"),
            response_model=response_model,
            finish_reason=finish_reason,
        )
        return parse_json_object(response_text(value))


def _codex_cli_usage(stdout: str) -> dict[str, int]:
    """Read the genuine per-turn usage emitted by ``codex exec --json``."""

    events: list[Mapping[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExtractionError("codex CLI emitted invalid JSONL telemetry") from exc
        if not isinstance(value, Mapping):
            raise ExtractionError("codex CLI telemetry event is not an object")
        events.append(value)

    def nested_mapping(value: Mapping[str, Any], *keys: str) -> Mapping[str, Any] | None:
        current: Any = value
        for key in keys:
            if not isinstance(current, Mapping):
                return None
            current = current.get(key)
        return current if isinstance(current, Mapping) else None

    completed_receipts: list[Mapping[str, Any]] = []
    token_count_receipts: list[Mapping[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type in {"model_reroute", "model.reroute"}:
            raise ExtractionError(
                "codex CLI reported a model reroute; requested model identity is not proven"
            )
        if event_type in {"turn.completed", "turn_completed"}:
            for path in (("usage",), ("payload", "usage")):
                receipt = nested_mapping(event, *path)
                if receipt is not None:
                    completed_receipts.append(receipt)
        elif event_type == "event_msg":
            payload = nested_mapping(event, "payload")
            if payload and payload.get("type") == "token_count":
                receipt = nested_mapping(payload, "info", "last_token_usage")
                if receipt is not None:
                    token_count_receipts.append(receipt)
    if len(completed_receipts) > 1:
        raise ExtractionError(
            "codex CLI must emit exactly one completed-turn usage receipt"
        )
    if completed_receipts:
        receipt = completed_receipts[0]
    elif token_count_receipts:
        receipt = token_count_receipts[-1]
    else:
        raise ExtractionError(
            "codex CLI must emit exactly one completed-turn usage receipt"
        )

    def integer(*names: str) -> int | None:
        for name in names:
            value = receipt.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    prompt = integer("input_tokens", "prompt_tokens")
    completion = integer("output_tokens", "completion_tokens")
    if prompt is None or completion is None:
        raise ExtractionError("codex CLI usage receipt is missing token counts")
    total = integer("total_tokens")
    if total is None:
        total = prompt + completion
    normalized = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if prompt < 0 or completion <= 0 or total < prompt + completion:
        raise ExtractionError("codex CLI usage receipt has inconsistent token counts")
    return normalized


class CodexCliModelClient:
    """Use the authenticated local Codex CLI without creating API receipts."""

    def __init__(self, *, model: str, profile: Profile, call_budget: int):
        if model not in ALLOWED_MODELS:
            raise ExtractionError("model must be gpt-5.6-sol or explicit fallback gpt-5.5")
        executable = shutil.which("codex")
        if executable is None:
            raise ExtractionError("codex CLI provider requires the codex executable")
        self.executable = executable
        self.model = model
        self.profile = profile
        self.call_budget = call_budget
        self.usage = Usage()

    def call(self, prompt: str, image: Path | Sequence[Path]) -> dict[str, Any]:
        if self.usage.calls >= self.call_budget:
            raise ExtractionError(f"model call budget exhausted ({self.call_budget})")
        images = [image] if isinstance(image, Path) else list(image)
        if not images:
            raise ExtractionError("model call requires at least one evidence image")
        resolved_images: list[Path] = []
        for item in images:
            try:
                resolved = item.expanduser().resolve(strict=True)
            except OSError as exc:
                raise ExtractionError(f"evidence image is unavailable: {item.name}") from exc
            if not resolved.is_file():
                raise ExtractionError(f"evidence image is not a regular file: {item.name}")
            resolved_images.append(resolved)
        image_map = "\n".join(
            f"EVIDENCE_SHEET_{index:03d}: {item.name}"
            for index, item in enumerate(resolved_images)
        )
        cli_prompt = (
            prompt
            + "\n\nAttached evidence images, in command-line order:\n"
            + image_map
            + "\nReturn exactly one JSON object with the single property `json_text` "
            + "and no Markdown fencing. The `json_text` value must be a valid "
            + "JSON-encoded string containing the response object requested above."
        )
        with tempfile.TemporaryDirectory(prefix="video2blender-codex-cli-") as temporary_value:
            temporary = Path(temporary_value)
            last_message = temporary / "last_message.json"
            output_schema = temporary / "output_schema.json"
            atomic_json(
                output_schema,
                {
                    "type": "object",
                    "properties": {"json_text": {"type": "string"}},
                    "required": ["json_text"],
                    "additionalProperties": False,
                },
            )
            command = [
                self.executable,
                "exec",
                "-m",
                self.model,
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--json",
                "--output-last-message",
                str(last_message),
                "--output-schema",
                str(output_schema),
            ]
            for item in resolved_images:
                command.extend(["--image", str(item)])
            command.append("-")
            child_environment = os.environ.copy()
            child_environment.pop("BLENDER_PIPELINE_API_ENDPOINT", None)
            child_environment.pop("BLENDER_PIPELINE_API_KEY_FILE", None)
            try:
                completed = subprocess.run(
                    command,
                    input=cli_prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=temporary,
                    env=child_environment,
                    timeout=900,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ExtractionError("codex CLI model call timed out") from exc
            except OSError as exc:
                raise ExtractionError(
                    f"codex CLI model call could not start: {type(exc).__name__}"
                ) from exc
            if completed.returncode != 0:
                stderr_digest = hashlib.sha256(
                    completed.stderr.encode("utf-8", errors="replace")[: 1024 * 1024]
                ).hexdigest()
                raise ExtractionError(
                    f"codex CLI model call failed with exit {completed.returncode}; "
                    f"stderr_sha256={stderr_digest}"
                )
            usage = _codex_cli_usage(completed.stdout)
            if not last_message.is_file():
                raise ExtractionError("codex CLI produced no --output-last-message file")
            if last_message.stat().st_size > 2 * 1024 * 1024:
                raise ExtractionError("codex CLI final message exceeds the JSON size limit")
            try:
                wrapper = parse_json_object(last_message.read_text(encoding="utf-8"))
            except (OSError, UnicodeError) as exc:
                raise ExtractionError("codex CLI final message is unreadable") from exc
            inner_text = wrapper.get("json_text")
            if not isinstance(inner_text, str):
                raise ExtractionError("codex CLI final message lacks json_text")
            value = parse_json_object(inner_text)
        self.usage.add(
            usage,
            # codex exec does not expose an API response-model receipt. This
            # value identifies the explicitly pinned CLI command, while the
            # telemetry parser rejects any model-reroute event.
            response_model=f"codex-cli:{self.model}",
            finish_reason="stop",
        )
        return value


def coarse_batch_prompt(
    *, title: str, windows: Sequence[Mapping[str, Any]],
    max_candidates: int,
) -> str:
    compact_windows = []
    image_map = []
    for image_index, item in enumerate(windows):
        window_index = int(item["window_index"])
        image_map.append(
            f"EVIDENCE_SHEET_{image_index:03d} -> window_index={window_index}"
        )
        compact_windows.append(
            {
                "window_index": window_index,
                "core_range_seconds": [
                    round(float(item["start_sec"]), 3),
                    round(float(item["end_sec"]), 3),
                ],
                "analysis_context_seconds": [
                    round(float(item["analysis_start_sec"]), 3),
                    round(float(item["analysis_end_sec"]), 3),
                ],
                "transcript": str(item.get("transcript") or "")[:2500],
                "ocr": [
                    {
                        "timestamp_sec": row.get("timestamp_sec"),
                        "region": row.get("region"),
                        "text": str(row.get("text") or "")[:300],
                    }
                    for row in list(item.get("ocr") or [])[:16]
                    if isinstance(row, Mapping)
                ],
            }
        )
    return f"""
你是 Blender 教学视频证据提取员。一次独立分析多个时间窗，不得在窗口之间混用证据。
视频标题：{title}

图片顺序与窗口的唯一映射：
{chr(10).join(image_map)}

窗口上下文：
{json.dumps(compact_windows, ensure_ascii=False)}

要求：
1. 必须为每个输入 window_index 返回且只返回一个结果。
2. 按时间顺序提取真实 Blender 操作；宣传、寒暄和纯展示不是步骤。
2a. 先识别标题所指的主任务，并优先保留其“准备/创建 → 核心变换或节点操作 → 关键参数/连接 → 最终检查”证据。只要画面或字幕可见，标题主任务的核心执行步骤不得被导入素材、切换工作区或次要美化步骤挤掉。
3. 每个窗口最多返回 {max_candidates} 个最关键、可复现且可被图片独立验证的步骤。合并同一目标的连续微操作，但每步尽量不超过 20 秒；不得用“继续搭建/调整节点网络”之类无对象、无操作的宽泛摘要代替具体步骤。忽略打开菜单、切换视图、预览或选文件等导航动作。
4. 候选步骤的开始时间必须落在该窗口的 core_range_seconds 内；步骤如果跨越边界，结束时间可以使用 analysis_context_seconds 内的后续证据。这一规则用于避免相邻窗口重复。
5. 如果截图或 OCR 显示了某个确切参数修改，必须优先把它作为一个候选，不能被宽泛结果摘要挤掉。参数、节点连线、骨骼关系、关键帧和修改后值看不清就写 unknown，绝不猜。parameters 的键只使用视频界面中的字段原文（例如“缩放”、“位置 Z”），节点/对象名写在 object 中。
6. 每个候选步骤同时提出需要回采的时间与区域。时间应靠近操作发生处，不只取口述字幕出现帧。
7. 截图标签是 COARSE_全局时间戳。优先寻找修改后的稳定 UI 状态。

只返回 JSON：
{{"windows":[{{"window_index":0,"steps_candidates":[{{"step_id":"W000-S001","time_range":"MM:SS-MM:SS","action":"具体操作","object":"对象/节点/骨骼","parameters":{{"参数":"修改后值或unknown"}},"relation_type":"none|node_connection|parenting|rigging|constraint|spatial_relation","visual_result":"可见结果","material_color":"材质/颜色/纹理","spatial_relation":"空间/连接关系","surface_detail":"表面细节","implementation_notes":"复现约束"}}],"evidence_requests":[{{"step_id":"W000-S001","timestamp_sec":0.0,"region":"full|right_ui|node_editor|timeline|properties","reason":"需要证明的操作或参数"}}],"uncertain_items":[]}}]}}
""".strip()


def split_coarse_batch_result(
    value: Mapping[str, Any], expected_window_indices: Sequence[int]
) -> dict[int, Mapping[str, Any]]:
    """Return an exact one-to-one window mapping; never guess cross-window output."""

    expected = [int(item) for item in expected_window_indices]
    raw_windows = value.get("windows")
    if not isinstance(raw_windows, list):
        # Backward-compatible single-window parsing is useful for local test
        # fixtures and older private providers. Multi-window output must be
        # explicit so evidence can never silently migrate between windows.
        return {expected[0]: value} if len(expected) == 1 else {}
    valid_rows: list[tuple[int, Mapping[str, Any]]] = []
    for raw in raw_windows:
        if not isinstance(raw, Mapping):
            continue
        try:
            window_index = int(raw.get("window_index"))
        except (TypeError, ValueError):
            continue
        if window_index not in expected:
            continue
        valid_rows.append((window_index, raw))
    counts = Counter(window_index for window_index, _raw in valid_rows)
    return {
        window_index: raw
        for window_index, raw in valid_rows
        if counts[window_index] == 1
    }


def verification_prompt(
    *,
    title: str,
    candidates: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    evidence_packages: Sequence[Mapping[str, Any]],
    transcript: str,
) -> str:
    compact_evidence = [
        {
            key: item.get(key)
            for key in (
                "image_id",
                "step_id",
                "timestamp_sec",
                "role",
                "region",
                "ocr_text",
            )
        }
        for item in evidence
    ]
    attachment_map = []
    for attachment_index, package in enumerate(evidence_packages):
        step_id = str(package.get("step_id") or "")
        allowed_ids = [
            str(item.get("image_id") or "")
            for item in package.get("evidence") or []
            if isinstance(item, Mapping)
        ]
        attachment_map.append(
            f"EVIDENCE_SHEET_{attachment_index:03d} -> step_id={step_id}; "
            f"allowed_evidence_ids={json.dumps(allowed_ids, ensure_ascii=False)}"
        )
    return f"""
你是 Blender 教程 Claim Q-Gate 审核员。只用标注为 IMAGE_ID 的密集回采图片和以下字幕审核候选步骤。
标题：{title}

附件与候选步骤的封闭映射（每张附件只包含一个 step 的证据）：
{chr(10).join(attachment_map)}

候选步骤：
{json.dumps(list(candidates), ensure_ascii=False)[:12000]}

证据索引：
{json.dumps(compact_evidence, ensure_ascii=False)[:12000]}

字幕/ASR：
{transcript[:5000]}

规则：
- `accepted` 是唯一可晋级载荷，不得复制看不清的候选字段。action 必须闭环；object、每个辅助字段和每个非 unknown 精确参数只有在存在同名精确 claim 时才写入。若 action 闭环成立但其他字段看不清，保持 verified=true、从 accepted 删除这些字段/参数，并在 uncertain_items 说明，不要抹掉已证实 action。
- 每条 claim 必须给出结构化 `field` 和 `value`，且 value 与 accepted 中的值完全一致；parameter claim 还要填写 parameter 键名。
- accepted.parameters 的键保留证据画面中的 UI 字段原文，节点/对象名仅写入 accepted.object；不得自行改名参数键。
- 节点连接、父子/骨骼/约束/空间关系必须标注 relation_type，并由 connection claim 绑定完整 spatial_relation。
- 每个 claim 只能引用图中存在的 IMAGE_ID；action 看不清或冲突就 verified=false。object、parameter、connection 或辅助字段看不清时仅省略该 claim 和 accepted 字段，并报告不确定性。
- 每个 decision 只能查看它映射的单张 EVIDENCE_SHEET，并且只能引用该映射列出的 allowed_evidence_ids。即使同一请求附带了其他 step 的证据，也绝不能借用其他附件、其他候选或同一时间窗中相邻操作的语义。
- pre 图不能单独证明修改后值；优先引用 action、stable、ocr_best。
- 不得把字幕本身当成看得见的精确 UI 值。
- action claim 必须同时引用本 step 的 ACTION 与 STABLE 两个 IMAGE_ID：ACTION 图必须直接显示 claim 所述操作正在发生，STABLE 图必须直接显示同一操作产生的修改后结果。若 ACTION 显示的是另一操作（例如 claim 写“添加 Voronoi”，画面却在缩放数值），或 STABLE 显示的是另一节点/另一结果，必须 verified=false；不得用候选文字、字幕、OCR 命中或相邻步骤补足这个闭环。
- 如果候选属于标题主任务的核心动作，也仍须满足上述 ACTION→STABLE 视觉闭环；只有口述、只有结果、或操作和结果语义不一致时必须拒绝。

只返回 JSON：
{{"decisions":[{{"step_id":"...","verified":true,"reason":"...","accepted":{{"action":"已证实操作","object":"已证实对象","parameters":{{}},"relation_type":"none|node_connection|parenting|rigging|constraint|spatial_relation","visual_result":"","material_color":"","spatial_relation":"","surface_detail":"","implementation_notes":""}},"claims":[{{"kind":"action|object|parameter|connection|visual_result|material_color|surface_detail|implementation_note","field":"accepted 字段名","parameter":"仅 parameter 使用的键名","value":"与 accepted 完全相同的结构化值","evidence_ids":["IMAGE_ID"]}}]}}],"uncertain_items":[]}}
""".strip()


def merge_verification_batch_decisions(
    value: Mapping[str, Any],
    *,
    expected_step_ids: Iterable[str],
    decisions: dict[str, dict[str, Any]],
    seen_step_ids: set[str],
    invalid_step_ids: set[str],
) -> list[dict[str, Any]]:
    """Merge verification output without allowing ambiguous ID binding.

    Step IDs bind decisions to evidence packages. A repeated ID therefore
    invalidates every decision carrying that ID instead of allowing response
    order to select a winner. IDs emitted for another batch are recorded as
    unknown and never become eligible merely because a later batch expects them.
    """
    expected = {str(item) for item in expected_step_ids if str(item)}
    raw_decisions = value.get("decisions") or []
    if not isinstance(raw_decisions, list):
        raw_decisions = []

    issues: list[dict[str, Any]] = []
    rows: list[tuple[str, Mapping[str, Any]]] = []
    for index, raw in enumerate(raw_decisions):
        if not isinstance(raw, Mapping):
            issues.append(
                {
                    "code": "malformed_verification_decision",
                    "decision_index": index,
                }
            )
            continue
        step_id = str(raw.get("step_id") or "").strip()
        if not step_id:
            issues.append(
                {
                    "code": "missing_verification_step_id",
                    "decision_index": index,
                }
            )
            continue
        rows.append((step_id, raw))

    counts = Counter(step_id for step_id, _raw in rows)
    duplicate_ids = {
        step_id
        for step_id, count in counts.items()
        if count > 1 or step_id in seen_step_ids
    }
    seen_step_ids.update(counts)
    for step_id in sorted(duplicate_ids):
        decisions.pop(step_id, None)
        if step_id not in invalid_step_ids:
            issues.append(
                {
                    "code": "ambiguous_duplicate_verification_decision",
                    "step_id": step_id,
                }
            )
        invalid_step_ids.add(step_id)

    for step_id, raw in rows:
        if step_id in invalid_step_ids:
            continue
        if step_id not in expected:
            issues.append(
                {
                    "code": "unknown_verification_step_id",
                    "step_id": step_id,
                }
            )
            continue
        decisions[step_id] = dict(raw)

    for step_id in sorted(expected):
        if step_id not in decisions and step_id not in invalid_step_ids:
            issues.append(
                {
                    "code": "missing_verification_decision",
                    "step_id": step_id,
                }
            )
    return issues


def normalize_candidates(
    value: Mapping[str, Any], *, window_index: int, start: float, end: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
    raw_steps = value.get("steps_candidates") or value.get("steps") or []
    requests = value.get("evidence_requests") or []
    steps: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}
    raw_step_ids = [
        str(raw.get("step_id") or "")
        for raw in raw_steps
        if isinstance(raw, Mapping) and str(raw.get("step_id") or "")
    ] if isinstance(raw_steps, list) else []
    duplicate_ids = {
        step_id for step_id, count in Counter(raw_step_ids).items() if count > 1
    }
    if isinstance(raw_steps, list):
        for offset, raw in enumerate(raw_steps, 1):
            if not isinstance(raw, Mapping):
                continue
            raw_step_id = str(raw.get("step_id") or "")
            step_id = f"W{window_index:03d}-S{offset:03d}"
            if raw_step_id and raw_step_id not in duplicate_ids:
                id_map[raw_step_id] = step_id
            step_start, step_end = parse_time_range(raw.get("time_range"), start, end)
            relation_type = str(raw.get("relation_type") or "unknown").strip()
            item = {
                "step_id": step_id,
                "window_index": window_index,
                "start_sec": max(start, min(end, step_start)),
                "end_sec": max(start, min(end, max(step_start, step_end))),
                # Missing model time is kept unknown. The coarse analysis
                # bounds are still useful for recapture, but must not become a
                # fabricated operation-level localization receipt.
                "time_range": str(raw.get("time_range") or "unknown"),
                "action": str(raw.get("action") or "unknown").strip(),
                "object": str(raw.get("object") or "").strip(),
                "parameters": (
                    dict(raw.get("parameters") or {})
                    if isinstance(raw.get("parameters"), Mapping)
                    else {}
                ),
                "relation_type": relation_type,
                "visual_result": str(raw.get("visual_result") or "").strip(),
                "material_color": str(raw.get("material_color") or "").strip(),
                "spatial_relation": str(raw.get("spatial_relation") or "").strip(),
                "surface_detail": str(raw.get("surface_detail") or "").strip(),
                "implementation_notes": str(
                    raw.get("implementation_notes") or ""
                ).strip(),
            }
            steps.append(item)
    normalized_requests = []
    duplicate_request_ids: set[str] = set()
    if isinstance(requests, list):
        for raw in requests:
            if not isinstance(raw, Mapping):
                continue
            raw_step_id = str(raw.get("step_id") or "")
            if raw_step_id in duplicate_ids:
                duplicate_request_ids.add(raw_step_id)
                continue
            raw_timestamp = raw.get("timestamp_sec")
            try:
                timestamp = (
                    _clock_seconds(raw_timestamp)
                    if isinstance(raw_timestamp, str)
                    else float(raw_timestamp)
                )
            except (TypeError, ValueError):
                timestamp = end
            if not math.isfinite(timestamp):
                timestamp = end
            normalized_requests.append(
                {
                    "step_id": id_map.get(raw_step_id, raw_step_id),
                    "timestamp_sec": max(start, min(end, timestamp)),
                    "region": requested_region(raw.get("region")),
                    # The model's request reason is not evidence.  It is only
                    # a retrieval hint for choosing the most relevant frame
                    # inside the candidate's dense visual scan.
                    "reason": str(raw.get("reason") or "").strip()[:1000],
                }
            )
    raw_uncertain = value.get("uncertain_items") or []
    uncertain_count = len(raw_uncertain) if isinstance(raw_uncertain, list) else 1
    uncertain: list[Any] = [
        {"code": "model_reported_uncertainty"}
        for _index in range(uncertain_count)
    ]
    uncertain.extend(
        {
            "code": "ambiguous_duplicate_step_id",
            "raw_step_id": step_id,
        }
        for step_id in sorted(duplicate_request_ids)
    )
    return steps, normalized_requests, uncertain


def requested_region(value: Any) -> str:
    region = str(value or "full").lower().strip()
    return region if region in {"full", "right_ui", "node_editor", "timeline", "properties"} else "full"


def candidate_belongs_to_core(
    candidate: Mapping[str, Any], core_start: float, core_end: float
) -> bool:
    """Assign a candidate to exactly one half-open core window."""

    candidate_start = float(candidate.get("start_sec") or 0.0)
    return candidate_start + 1e-6 >= core_start and candidate_start < core_end - 1e-6


def crop_region(source: Path, destination: Path, region: str) -> None:
    image = ImageOps.exif_transpose(Image.open(source)).convert("RGB")
    cropped = _ocr_crop(image, region)
    destination.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(destination, quality=92)


def bounded_dense_scan_times(
    start: float,
    end: float,
    *,
    interval: float,
    limit: int,
    anchors: Sequence[float],
) -> list[float]:
    """Retain request-local detail while keeping dense recapture bounded.

    The old evenly-thinned scan could discard the frame immediately after an
    evidence request.  That is exactly where a fast Blender edit often becomes
    visible.  Preserve the bounds, semantic anchors, and their nearest dense
    neighbors first, then use farthest-point sampling for the remaining slots.
    """

    if limit < 1 or end < start:
        return []
    start = round(start, 3)
    end = round(end, 3)

    def clipped(value: float) -> float:
        return round(max(start, min(end, float(value))), 3)

    base = sorted(
        {
            *frange(start, end + interval * 0.5, interval),
            start,
            end,
            *(clipped(value) for value in anchors),
        }
    )
    priority: list[float] = [start, end]
    priority.extend(clipped(value) for value in anchors)
    # The request and candidate midpoint are the most useful local anchors.
    # Preserve one frame on each side plus a second post frame so ACTION and a
    # genuinely later STABLE state can both survive the scan cap.
    for anchor in list(anchors)[:2]:
        priority.extend(
            clipped(anchor + offset * interval)
            for offset in (1.0, -1.0, 2.0)
        )

    selected: list[float] = []
    # Reserve two slots for coverage outside the immediate anchor cluster.
    # This still lets a semantically relevant frame a few seconds after an
    # imprecise candidate range enter the scan.
    priority_limit = max(1, limit - 2)
    for value in priority:
        if value not in selected:
            selected.append(value)
        if len(selected) >= priority_limit:
            break
    remaining = [value for value in base if value not in selected]
    while remaining and len(selected) < limit:
        # Fill uncovered portions of a long candidate/request span without
        # losing the high-resolution neighborhood above.
        value = max(
            remaining,
            key=lambda item: (
                min(abs(item - kept) for kept in selected),
                -abs(item - clipped(anchors[0] if anchors else start)),
            ),
        )
        selected.append(value)
        remaining.remove(value)
    return sorted(selected)


def _select_dense_action(
    entries: Sequence[Mapping[str, Any]],
    *,
    requested_center: float,
    profile: Profile,
) -> Mapping[str, Any] | None:
    if not entries:
        return None
    semantic = [
        item for item in entries if int(item.get("semantic_score") or 0) > 0
    ]
    if semantic:
        # A visible candidate/object/parameter match outranks timing.  Timing
        # then keeps a persistent node elsewhere in the graph from stealing a
        # later adjacent operation merely because both frames contain its name.
        return max(
            semantic,
            key=lambda item: (
                int(item.get("semantic_score") or 0),
                -abs(float(item["timestamp_sec"]) - requested_center),
                float(item.get("change_from_previous") or 0),
            ),
        )

    local_radius = max(0.75, min(1.5, profile.dense_radius))
    local = [
        item
        for item in entries
        if abs(float(item["timestamp_sec"]) - requested_center) <= local_radius
    ] or list(entries)
    return max(
        local,
        key=lambda item: (
            float(item.get("change_from_previous") or 0)
            - 0.15
            * min(
                1.0,
                abs(float(item["timestamp_sec"]) - requested_center)
                / local_radius,
            ),
            -abs(float(item["timestamp_sec"]) - requested_center),
        ),
    )


def _select_dense_pre(
    entries: Sequence[Mapping[str, Any]],
    action: Mapping[str, Any],
    *,
    profile: Profile,
) -> Mapping[str, Any] | None:
    action_time = float(action["timestamp_sec"])
    before = [
        item
        for item in entries
        if float(item["timestamp_sec"]) < action_time - 1e-6
    ]
    if not before:
        return None
    near = [
        item
        for item in before
        if action_time - float(item["timestamp_sec"])
        <= max(1.0, min(1.5, profile.dense_radius))
    ] or before
    action_semantic = int(action.get("semantic_score") or 0)
    if action_semantic:
        lower_semantic = [
            item
            for item in near
            if int(item.get("semantic_score") or 0) < action_semantic
        ]
        if lower_semantic:
            near = lower_semantic
    return max(near, key=lambda item: float(item["timestamp_sec"]))


def _select_dense_stable(
    entries: Sequence[Mapping[str, Any]],
    action: Mapping[str, Any],
    *,
    profile: Profile,
) -> Mapping[str, Any] | None:
    """Choose a close, settled post-action state or fail closed.

    A long mechanical `step_end + 1s` offset crosses into the next operation in
    tightly edited tutorials.  Limit the stable search to the immediate edit
    neighborhood, require a distinct image, and prefer candidate-semantic
    continuity when OCR can see it.  The downstream visual Q-Gate remains the
    authority; this selector never upgrades text alone into evidence.
    """

    action_time = float(action["timestamp_sec"])
    minimum_delay = max(0.2, min(0.5, profile.dense_interval))
    horizon = max(0.75, min(1.5, profile.dense_radius))
    action_sha = str(action.get("sha256") or "")
    later = [
        item
        for item in entries
        if float(item["timestamp_sec"]) >= action_time + minimum_delay - 1e-6
        and float(item["timestamp_sec"]) <= action_time + horizon + 1e-6
        and str(item.get("sha256") or "") != action_sha
    ]
    if not later:
        return None

    action_semantic = int(action.get("semantic_score") or 0)
    if action_semantic:
        continued = [
            item
            for item in later
            if int(item.get("semantic_score") or 0) > 0
        ]
        if continued:
            later = continued
        else:
            # OCR may disappear when a menu closes.  A visual fallback is
            # permitted only very close to ACTION; jumping farther would turn
            # a neighboring edit into fabricated stable evidence.
            later = [
                item
                for item in later
                if float(item["timestamp_sec"]) - action_time <= 0.75 + 1e-6
            ]
            if not later:
                return None

    settle_window = max(
        0.75,
        min(1.25, profile.dense_interval * 4.0),
    )
    near = [
        item
        for item in later
        if float(item["timestamp_sec"]) - action_time <= settle_window + 1e-6
    ] or later
    action_path = Path(str(action["path"]))
    return min(
        near,
        key=lambda item: (
            float(item.get("change_from_previous") or 0)
            + 0.35 * image_change_score(action_path, Path(str(item["path"]))),
            float(item["timestamp_sec"]) - action_time,
            -int(item.get("semantic_score") or 0),
        ),
    )


def dense_scan_for_step(
    *, video: Path, temporary_root: Path, profile: Profile,
    step: Mapping[str, Any], request: Mapping[str, Any] | None, duration: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Decode one bounded, candidate-owned scan before choosing evidence.

    Every frame receives a globally unique scan ID.  The IDs, timestamps, and
    hashes form the only vocabulary that either the model-assisted localizer or
    the deterministic downgrade path may use.  Keeping selection separate from
    decoding makes it impossible for a model to name an unobserved frame.
    """

    if request is not None and request.get("timestamp_sec") is not None:
        raw_requested = request.get("timestamp_sec")
    elif step.get("end_sec") is not None:
        raw_requested = step.get("end_sec")
    else:
        raw_requested = step.get("start_sec") or 0
    try:
        requested_center = (
            _clock_seconds(raw_requested)
            if isinstance(raw_requested, str)
            else float(raw_requested)
        )
    except (TypeError, ValueError) as exc:
        raise ExtractionError("evidence request timestamp is invalid") from exc
    if not math.isfinite(requested_center):
        raise ExtractionError("evidence request timestamp is invalid")
    requested_center = max(0.0, min(duration - 0.01, requested_center))
    step_start = max(0.0, min(duration - 0.01, float(step.get("start_sec") or 0)))
    step_end = max(
        step_start,
        min(duration - 0.01, float(step.get("end_sec") or step_start)),
    )
    region = requested_region((request or {}).get("region"))
    step_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(step.get("step_id") or "step"))
    scan_start = max(
        0.0,
        min(step_start, requested_center) - profile.dense_radius,
    )
    scan_end = min(
        duration,
        max(step_end, requested_center) + profile.dense_radius + profile.dense_interval,
    )
    scan_limit = {"economy": 9, "balanced": 13, "forensic": 17}[profile.name]
    maximum_timestamp = max(0.0, duration - 0.01)
    scan_times = bounded_dense_scan_times(
        max(0.0, min(maximum_timestamp, scan_start)),
        max(0.0, min(maximum_timestamp, scan_end)),
        interval=profile.dense_interval,
        limit=scan_limit,
        anchors=(
            requested_center,
            (step_start + step_end) / 2.0,
            step_end,
            step_start,
        ),
    )
    entries: list[dict[str, Any]] = []
    previous_crop: Path | None = None
    for index, timestamp in enumerate(scan_times):
        raw = temporary_root / f"{step_id}_scan_{index:03d}.jpg"
        extract_frame(video, timestamp, raw, profile.image_max_side)
        cropped = temporary_root / f"{step_id}_scan_{index:03d}_{region}.jpg"
        crop_region(raw, cropped, region)
        entries.append(
            {
                "scan_id": f"SCAN-{step_id}-{index:03d}",
                "timestamp_sec": round(timestamp, 3),
                "path": cropped,
                "sha256": sha256_path(cropped),
                # OCR is deliberately deferred until localization has selected
                # at most PRE/ACTION/STABLE/OCR.  Tesseract over every dense
                # frame dominated real six-minute runs without improving the
                # visual model's closed-vocabulary choice.
                "ocr": [],
                "relevance_score": 0,
                "semantic_score": 0,
                # The first sampled frame has no predecessor; it is not a
                # fabricated change peak.
                "change_from_previous": (
                    image_change_score(previous_crop, cropped)
                    if previous_crop is not None
                    else 0.0
                ),
            }
        )
        previous_crop = cropped

    sheet = temporary_root / "sheets" / f"candidate_{step_id}.jpg"
    make_contact_sheet(
        [
            (
                f"{entry['scan_id']} | {float(entry['timestamp_sec']):.3f}s",
                Path(str(entry["path"])),
            )
            for entry in entries
        ],
        sheet,
        columns=3,
        # These are the model's source frames rather than a presentation
        # mosaic.  Preserve enough UI detail for node names and numeric fields
        # after the provider's max-side resize.
        tile_size=(720, 450),
    )
    return (
        {
            "step_id": str(step.get("step_id") or ""),
            "candidate": dict(step),
            "request": dict(request or {}),
            "region": region,
            "requested_center": round(requested_center, 3),
            "entries": entries,
            "sheet": sheet,
        },
        [],
    )


def heuristic_dense_localization(
    package: Mapping[str, Any], *, profile: Profile
) -> dict[str, str] | None:
    """Select scan IDs without another model call, then apply the same gate.

    This is an explicit budget downgrade only.  It does not manufacture a
    missing role or weaken temporal/hash validation, so downstream Claim
    Q-Gate behavior is identical to the model-assisted path.
    """

    entries = [
        item for item in package.get("entries") or [] if isinstance(item, Mapping)
    ]
    action = _select_dense_action(
        entries,
        requested_center=float(package.get("requested_center") or 0.0),
        profile=profile,
    )
    if action is None:
        return None
    pre = _select_dense_pre(entries, action, profile=profile)
    stable = _select_dense_stable(entries, action, profile=profile)
    if pre is None or stable is None:
        return None
    best = max(
        entries,
        key=lambda item: (
            int(item.get("relevance_score") or 0),
            -abs(
                float(item["timestamp_sec"])
                - float(package.get("requested_center") or 0.0)
            ),
        ),
    )
    raw = {
        "pre_scan_id": str(pre.get("scan_id") or ""),
        "action_scan_id": str(action.get("scan_id") or ""),
        "stable_scan_id": str(stable.get("scan_id") or ""),
        "ocr_scan_id": str(best.get("scan_id") or ""),
    }
    selection, _issues = validate_dense_localization(raw, package)
    return selection


def validate_dense_localization(
    value: Mapping[str, Any], package: Mapping[str, Any]
) -> tuple[dict[str, str] | None, list[str]]:
    """Bind one localization response to one candidate, failing closed."""

    required = (
        "pre_scan_id",
        "action_scan_id",
        "stable_scan_id",
        "ocr_scan_id",
    )
    issues: list[str] = []
    selected: dict[str, str] = {}
    entries = {
        str(item.get("scan_id") or ""): item
        for item in package.get("entries") or []
        if isinstance(item, Mapping) and str(item.get("scan_id") or "")
    }
    for field_name in required:
        scan_id = value.get(field_name)
        if not isinstance(scan_id, str) or not scan_id.strip():
            issues.append(f"missing_{field_name}")
            continue
        scan_id = scan_id.strip()
        if scan_id not in entries:
            issues.append(f"foreign_or_unknown_{field_name}")
            continue
        selected[field_name] = scan_id
    if issues:
        return None, sorted(set(issues))

    ordered_ids = [
        selected["pre_scan_id"],
        selected["action_scan_id"],
        selected["stable_scan_id"],
    ]
    if len(set(ordered_ids)) != 3:
        issues.append("pre_action_stable_ids_not_distinct")
    ordered = [entries[scan_id] for scan_id in ordered_ids]
    times = [float(item.get("timestamp_sec") or 0.0) for item in ordered]
    if not (times[0] < times[1] < times[2]):
        issues.append("pre_action_stable_time_order_invalid")
    hashes = [str(item.get("sha256") or "") for item in ordered]
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes):
        issues.append("pre_action_stable_hash_missing")
    elif len(set(hashes)) != 3:
        issues.append("pre_action_stable_hashes_not_distinct")
    if issues:
        return None, sorted(set(issues))
    return selected, []


def dense_localization_prompt(
    *, title: str, packages: Sequence[Mapping[str, Any]]
) -> str:
    """Create a candidate-isolated, closed-vocabulary temporal prompt."""

    assert_dense_package_isolation(packages)
    attachment_map: list[str] = []
    compact: list[dict[str, Any]] = []
    for attachment_index, package in enumerate(packages):
        step_id = str(package.get("step_id") or "")
        entries = [
            item
            for item in package.get("entries") or []
            if isinstance(item, Mapping)
        ]
        allowed_ids = [str(item.get("scan_id") or "") for item in entries]
        attachment_map.append(
            f"EVIDENCE_SHEET_{attachment_index:03d} -> step_id={step_id}; "
            f"allowed_scan_ids={json.dumps(allowed_ids, ensure_ascii=False)}"
        )
        compact.append(
            {
                "step_id": step_id,
                "candidate": {
                    key: (
                        value
                        if isinstance(value, (int, float, bool))
                        else str(value)[:500]
                    )
                    for key, value in dict(
                        package.get("candidate") or {}
                    ).items()
                    if key
                    in {
                        "start_sec",
                        "end_sec",
                        "action",
                        "object",
                        "parameters",
                        "relation_type",
                        "visual_result",
                        "spatial_relation",
                    }
                },
                "retrieval_hint": {
                    "timestamp_sec": (package.get("request") or {}).get(
                        "timestamp_sec"
                    ),
                    "reason": str(
                        (package.get("request") or {}).get("reason") or ""
                    )[:500],
                },
                "region": package.get("region"),
                "frames": [
                    {
                        "scan_id": item.get("scan_id"),
                        "timestamp_sec": item.get("timestamp_sec"),
                    }
                    for item in entries
                ],
            }
        )
    return f"""
STAGE=DENSE_TEMPORAL_LOCALIZATION
你是 Blender 操作证据的时序定位器。标题：{title}

附件与候选的封闭映射：
{chr(10).join(attachment_map)}

候选与允许帧：
{json.dumps(compact, ensure_ascii=False)}

只做帧定位，不审核 claim，也不得改写候选内容：
1. 每个 localization 只能查看它映射的单张附件，只能从该候选的 allowed_scan_ids 选择。
2. PRE 是操作发生前的状态；ACTION 必须直接显示候选操作正在发生；STABLE 必须显示同一操作完成后的稳定结果；OCR 是该候选文字/参数最清楚的一帧。
3. 必须满足 PRE < ACTION < STABLE。相邻步骤、只有口述、只有最终展示、或动作语义不一致时，把该候选放入 uncertain_items，不要猜 ID。
4. 每个输入 step_id 最多返回一次；看不清时宁可不返回 localization。

只返回 JSON：
{{"localizations":[{{"step_id":"W000-S001","pre_scan_id":"SCAN-...","action_scan_id":"SCAN-...","stable_scan_id":"SCAN-...","ocr_scan_id":"SCAN-...","reason":"简短可见依据"}}],"uncertain_items":[{{"step_id":"...","reason":"无法形成同一操作的 PRE/ACTION/STABLE"}}]}}
""".strip()


def assert_dense_package_isolation(
    packages: Sequence[Mapping[str, Any]],
) -> None:
    """Reject duplicate candidate or scan identities before model binding."""

    step_ids = [str(package.get("step_id") or "") for package in packages]
    if any(not step_id for step_id in step_ids) or len(set(step_ids)) != len(step_ids):
        raise ExtractionError("dense localization packages have duplicate or empty step_id")
    scan_ids: list[str] = []
    for package in packages:
        owned = [
            str(item.get("scan_id") or "")
            for item in package.get("entries") or []
            if isinstance(item, Mapping)
        ]
        if not owned or any(not scan_id for scan_id in owned):
            raise ExtractionError("dense localization package has no complete scan IDs")
        if len(set(owned)) != len(owned):
            raise ExtractionError("dense localization package repeats a scan ID")
        scan_ids.extend(owned)
    if len(set(scan_ids)) != len(scan_ids):
        raise ExtractionError("dense localization scan ID is shared across candidates")


def merge_dense_localizations(
    value: Mapping[str, Any], *, packages: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    """Validate a batch response without order-based or cross-step binding."""

    assert_dense_package_isolation(packages)
    by_step = {
        str(package.get("step_id") or ""): package
        for package in packages
        if str(package.get("step_id") or "")
    }
    rows = value.get("localizations") or []
    if not isinstance(rows, list):
        rows = []
    parsed: list[tuple[str, Mapping[str, Any]]] = []
    issues: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            issues.append(
                {"code": "malformed_dense_localization", "row_index": index}
            )
            continue
        step_id = str(row.get("step_id") or "").strip()
        if step_id not in by_step:
            issues.append(
                {
                    "code": "unknown_dense_localization_step_id",
                    "step_id": step_id,
                }
            )
            continue
        parsed.append((step_id, row))

    counts = Counter(step_id for step_id, _row in parsed)
    decisions: dict[str, dict[str, str]] = {}
    for step_id, package in by_step.items():
        if counts.get(step_id, 0) != 1:
            issues.append(
                {
                    "code": (
                        "duplicate_dense_localization"
                        if counts.get(step_id, 0) > 1
                        else "missing_dense_localization"
                    ),
                    "step_id": step_id,
                }
            )
            continue
        row = next(raw for candidate_id, raw in parsed if candidate_id == step_id)
        selection, selection_issues = validate_dense_localization(row, package)
        if selection is None:
            issues.append(
                {
                    "code": "invalid_dense_localization",
                    "step_id": step_id,
                    "reasons": selection_issues,
                }
            )
            continue
        decisions[step_id] = selection
    return decisions, issues


def materialize_dense_evidence(
    *, package: Mapping[str, Any], selection: Mapping[str, str], output_root: Path
) -> list[dict[str, Any]]:
    """Copy only validated selected scans into the deliverable evidence set."""

    validated, issues = validate_dense_localization(selection, package)
    if validated is None:
        raise ExtractionError(
            "cannot materialize invalid dense localization: " + ", ".join(issues)
        )
    entries = {
        str(item.get("scan_id") or ""): item
        for item in package.get("entries") or []
        if isinstance(item, Mapping)
    }
    role_fields = (
        ("pre", "pre_scan_id"),
        ("action", "action_scan_id"),
        ("stable", "stable_scan_id"),
        ("ocr_best", "ocr_scan_id"),
    )
    records: list[dict[str, Any]] = []
    safe_step_id = re.sub(
        r"[^A-Za-z0-9_-]+", "-", str(package.get("step_id") or "step")
    )
    for role, field_name in role_fields:
        entry = entries[validated[field_name]]
        source = Path(str(entry["path"]))
        if not source.is_file() or sha256_path(source) != str(entry.get("sha256") or ""):
            raise ExtractionError("dense scan changed after localization")
        image_id = f"IMG-{safe_step_id}-{role.upper()}"
        destination = output_root / "evidence" / "frames" / f"{image_id}.jpg"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        ocr = list(entry.get("ocr") or [])
        records.append(
            {
                "image_id": image_id,
                "step_id": str(package.get("step_id") or ""),
                "timestamp_sec": round(float(entry["timestamp_sec"]), 3),
                "role": role,
                "region": str(package.get("region") or "full"),
                "path": destination.relative_to(output_root).as_posix(),
                "sha256": sha256_path(destination),
                "ocr_text": " | ".join(
                    str(item.get("text") or "")
                    for item in ocr
                    if isinstance(item, Mapping)
                )[:3000],
            }
        )
    return records


def ocr_selected_dense_entries(
    package: Mapping[str, Any], selection: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Run OCR only on the at-most-four frames that survived localization."""

    validated, issues = validate_dense_localization(selection, package)
    if validated is None:
        raise ExtractionError(
            "cannot OCR invalid dense localization: " + ", ".join(issues)
        )
    entries = {
        str(item.get("scan_id") or ""): item
        for item in package.get("entries") or []
        if isinstance(item, dict)
    }
    observations: list[dict[str, Any]] = []
    for scan_id in dict.fromkeys(validated.values()):
        entry = entries[scan_id]
        rows = ocr_image(
            Path(str(entry["path"])),
            float(entry["timestamp_sec"]),
            ("full",),
        )
        entry["ocr"] = rows
        observations.extend(rows)
    return observations


def dense_evidence_for_step(
    *, video: Path, output_root: Path, temporary_root: Path, profile: Profile,
    step: Mapping[str, Any], request: Mapping[str, Any] | None, duration: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compatibility wrapper for the explicit no-extra-call downgrade."""

    package, observations = dense_scan_for_step(
        video=video,
        temporary_root=temporary_root,
        profile=profile,
        step=step,
        request=request,
        duration=duration,
    )
    selection = heuristic_dense_localization(package, profile=profile)
    if selection is None:
        return [], observations
    observations.extend(ocr_selected_dense_entries(package, selection))
    return (
        materialize_dense_evidence(
            package=package, selection=selection, output_root=output_root
        ),
        observations,
    )


def _ocr_term_variants(value: Any) -> set[str]:
    """Return conservative OCR-search terms without inventing UI aliases."""

    text = str(value or "").casefold().strip()
    if not text:
        return set()
    compact = re.sub(r"\s+", "", text)
    terms = {text, compact}
    terms.update(
        match.group(0)
        for match in re.finditer(r"[a-z0-9_.+\-]+|[\u4e00-\u9fff]{2,}", text)
    )
    # OCR often drops one Chinese character. Bigrams retain relevance while
    # avoiding single-character matches such as “值” or “轴”.
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", text):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return {
        term
        for term in terms
        if len(term) >= 2 or any(char.isdigit() for char in term)
    }


def _ocr_relevance_scores(
    step: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    request_reason: str = "",
) -> tuple[int, int]:
    text = " ".join(
        str(item.get("text") or "") for item in observations
    ).casefold()
    compact = re.sub(r"\s+", "", text)

    def hits(values: Iterable[Any]) -> int:
        terms = {
            term
            for value in values
            for term in _ocr_term_variants(value)
        }
        return sum(1 for term in terms if term in text or term in compact)

    parameters = step.get("parameters")
    parameter_items = (
        list(parameters.items()) if isinstance(parameters, Mapping) else []
    )
    value_hits = hits(
        value for _key, value in parameter_items if _not_unknown(value)
    )
    key_hits = hits(key for key, _value in parameter_items)
    object_hits = hits([step.get("object")])
    action_hits = hits([step.get("action")])
    request_hits = hits([request_reason])
    semantic = (
        value_hits * 100_000
        + key_hits * 10_000
        + object_hits * 1_000
        + action_hits * 100
        + request_hits * 10
    )
    length_tiebreak = min(
        999,
        sum(len(str(item.get("text") or "")) for item in observations),
    )
    return semantic, semantic * 1_000 + length_tiebreak


def ocr_semantic_relevance_score(
    step: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    request_reason: str = "",
) -> int:
    """Return only visible candidate-term matches, excluding text volume."""

    semantic, _rank = _ocr_relevance_scores(
        step, observations, request_reason=request_reason
    )
    return semantic


def ocr_relevance_score(
    step: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    request_reason: str = "",
) -> int:
    """Rank a dense OCR frame by candidate semantics, then text volume.

    Exact parameter values and UI field names are substantially more useful
    than a long subtitle. Object/action/request terms provide weaker retrieval
    hints, and total OCR length is only a deterministic tie breaker.  The
    request reason and OCR never become verification evidence by themselves.
    """

    _semantic, rank = _ocr_relevance_scores(
        step, observations, request_reason=request_reason
    )
    return rank


def _not_unknown(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in UNKNOWN_VALUES


def _values_equal(left: Any, right: Any) -> bool:
    return canonical_sha256(left) == canonical_sha256(right)


def qgate_step(
    candidate: Mapping[str, Any], decision: Mapping[str, Any] | None,
    evidence_by_id: Mapping[str, Mapping[str, Any]], duration: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    fatal_reasons: list[str] = []
    dropped_claim_reasons: list[str] = []

    def drop_claim(reason: str) -> None:
        message = f"dropped_claim: {reason}"
        if message not in dropped_claim_reasons:
            dropped_claim_reasons.append(message)

    if not decision or decision.get("verified") is not True:
        return None, [str((decision or {}).get("reason") or "model verification did not accept the step")]
    accepted = decision.get("accepted")
    if not isinstance(accepted, Mapping):
        return None, ["verified decision contains no structured accepted payload"]
    candidate_step_id = str(candidate.get("step_id") or "")
    owned_evidence = [
        item
        for item in evidence_by_id.values()
        if str(item.get("step_id") or "") == candidate_step_id
    ]
    evidence_by_role = {
        str(item.get("role") or ""): item for item in owned_evidence
    }
    if {"pre", "action", "stable"}.issubset(evidence_by_role):
        pre_item = evidence_by_role["pre"]
        action_item = evidence_by_role["action"]
        stable_item = evidence_by_role["stable"]
        pre_time = float(pre_item.get("timestamp_sec") or 0)
        action_time = float(action_item.get("timestamp_sec") or 0)
        stable_time = float(stable_item.get("timestamp_sec") or 0)
        if not (pre_time < action_time < stable_time):
            fatal_reasons.append(
                "evidence roles are not temporally ordered pre < action < stable"
            )
        if str(action_item.get("sha256") or "") == str(
            stable_item.get("sha256") or ""
        ):
            fatal_reasons.append("action and stable evidence are the same image")
    else:
        fatal_reasons.append("candidate lacks pre/action/stable evidence")
    raw_claims = decision.get("claims") or []
    claims: list[dict[str, Any]] = []
    if not isinstance(raw_claims, list):
        drop_claim("claims payload is not an array")
        raw_claims = []
    for claim_index, raw in enumerate(raw_claims):
        if not isinstance(raw, Mapping):
            drop_claim(f"claim[{claim_index}] is not an object")
            continue
        kind = str(raw.get("kind") or "")
        if kind not in {
            "action",
            "object",
            "parameter",
            "connection",
            "visual_result",
            "material_color",
            "surface_detail",
            "implementation_note",
        }:
            drop_claim(f"claim[{claim_index}] has an unsupported kind")
            continue
        raw_evidence_ids = raw.get("evidence_ids")
        if not isinstance(raw_evidence_ids, list):
            drop_claim(f"claim[{claim_index}] evidence_ids is not an array")
            continue
        raw_ids = [str(item) for item in raw_evidence_ids]
        missing_ids = sorted(set(raw_ids) - set(evidence_by_id))
        if missing_ids:
            drop_claim(
                "claim cites nonexistent evidence IDs "
                f"(claim[{claim_index}]): "
                + ", ".join(missing_ids)
            )
            continue
        cross_step_ids = sorted(
            item
            for item in raw_ids
            if item in evidence_by_id
            and str(evidence_by_id[item].get("step_id") or "") != candidate_step_id
        )
        if cross_step_ids:
            drop_claim(
                "claim cites evidence owned by another step "
                f"(claim[{claim_index}]): "
                + ", ".join(cross_step_ids)
            )
            continue
        ids = [item for item in raw_ids if item in evidence_by_id]
        if not ids:
            drop_claim(f"claim[{claim_index}] has no evidence IDs")
            continue
        field_name = str(raw.get("field") or "")
        if not field_name:
            drop_claim(f"claim[{claim_index}] has no field")
            continue
        if "value" not in raw:
            drop_claim(f"claim[{claim_index}] has no value")
            continue
        if not any(str(evidence_by_id[item].get("role")) in {"action", "stable", "ocr_best"} for item in ids):
            drop_claim(
                f"claim[{claim_index}] has no ACTION, STABLE, or OCR evidence"
            )
            continue
        claims.append(
            {
                "_index": claim_index,
                "kind": kind,
                "field": field_name,
                "parameter": str(raw.get("parameter") or ""),
                "value": raw.get("value"),
                "text": f"{field_name}={raw.get('value')}",
                "evidence_ids": sorted(set(ids)),
            }
        )
    def matching_claims(
        kind: str, field_name: str, value: Any, parameter: str = ""
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in claims
            if item["kind"] == kind
            and item["field"] == field_name
            and item["parameter"] == parameter
            and _values_equal(item.get("value"), value)
        ]

    selected_claims: list[dict[str, Any]] = []
    selected_claim_indices: set[int] = set()

    def select_claims(items: Sequence[Mapping[str, Any]]) -> None:
        for item in items:
            claim_index = int(item["_index"])
            if claim_index in selected_claim_indices:
                continue
            selected_claim_indices.add(claim_index)
            selected_claims.append(
                {key: value for key, value in item.items() if key != "_index"}
            )

    action = str(accepted.get("action") or "").strip()
    matching_action_claims = [
        item
        for item in matching_claims("action", "action", action)
        if {"action", "stable"}.issubset(
            {
                str(evidence_by_id[evidence_id].get("role") or "")
                for evidence_id in item["evidence_ids"]
            }
        )
    ]
    if not _not_unknown(action) or not matching_action_claims:
        exact_action_claims = matching_claims("action", "action", action)
        if _not_unknown(action) and exact_action_claims:
            fatal_reasons.append(
                "accepted action is not closed by same-step ACTION and STABLE evidence"
            )
        else:
            fatal_reasons.append(
                "accepted action lacks an exact evidence-bound action claim"
            )
    else:
        select_claims(matching_action_claims)

    raw_object_name = str(accepted.get("object") or "").strip()
    object_name = ""
    if raw_object_name:
        object_claims = matching_claims("object", "object", raw_object_name)
        if _not_unknown(raw_object_name) and object_claims:
            object_name = raw_object_name
            select_claims(object_claims)
        else:
            drop_claim(
                "accepted object lacks an exact evidence-bound object claim"
            )
    accepted_parameters = (
        accepted.get("parameters")
        if isinstance(accepted.get("parameters"), Mapping)
        else {}
    )
    if "parameters" in accepted and not isinstance(
        accepted.get("parameters"), Mapping
    ):
        drop_claim("accepted parameters payload is not an object")
    parameters: dict[str, Any] = {}
    for raw_key, value in accepted_parameters.items():
        key = str(raw_key)
        parameter_claims = matching_claims(
            "parameter", "parameters", value, key
        )
        if key and _not_unknown(value) and parameter_claims:
            parameters[key] = value
            select_claims(parameter_claims)
        else:
            drop_claim(
                f"precise parameter lacks an exact accepted claim: {key or '<empty>'}"
            )

    relation_types = {
        "none",
        "node_connection",
        "parenting",
        "rigging",
        "constraint",
        "spatial_relation",
    }
    raw_relation_type = str(accepted.get("relation_type") or "").strip()
    raw_spatial_relation = str(
        accepted.get("spatial_relation") or ""
    ).strip()
    spatial_relation = (
        raw_spatial_relation if _not_unknown(raw_spatial_relation) else ""
    )
    connection_text = " ".join(
        [action, spatial_relation]
        + [f"{key}={value}" for key, value in accepted_parameters.items()]
    ).lower()
    relationship_requested = any(
        term in connection_text
        for term in (
            "连接",
            "connect",
            "parent",
            "父子",
            "bone",
            "骨骼",
            "rig",
            "绑定",
            "skin",
            "蒙皮",
            "constraint",
            "约束",
            "->",
            "→",
            "feeds",
            "linked",
            "links",
        )
    ) or raw_relation_type not in {"", "none"} or bool(raw_spatial_relation)
    relation_type = "none"
    if relationship_requested:
        connection_claims = matching_claims(
            "connection", "spatial_relation", spatial_relation
        )
        if (
            raw_relation_type in relation_types - {"none"}
            and spatial_relation
            and connection_claims
        ):
            relation_type = raw_relation_type
            select_claims(connection_claims)
        else:
            spatial_relation = ""
            drop_claim(
                "connection/relationship claim is not exactly evidence-bound; "
                "relation_type was downgraded to none and spatial_relation was cleared"
            )
    elif raw_relation_type not in {"", "none"}:
        drop_claim(
            "accepted relation_type is missing or invalid; relation_type was "
            "downgraded to none"
        )

    optional_fields = (
        ("visual_result", "visual_result"),
        ("material_color", "material_color"),
        ("surface_detail", "surface_detail"),
        ("implementation_notes", "implementation_note"),
    )
    accepted_optional: dict[str, str] = {}
    for field_name, claim_kind in optional_fields:
        field_value = str(accepted.get(field_name) or "").strip()
        field_claims = matching_claims(
            claim_kind, field_name, field_value
        )
        if field_value and _not_unknown(field_value) and field_claims:
            accepted_optional[field_name] = field_value
            select_claims(field_claims)
        else:
            accepted_optional[field_name] = ""
            if field_value:
                drop_claim(
                    f"accepted {field_name} lacks an exact evidence-bound claim"
                )

    for claim in claims:
        claim_index = int(claim["_index"])
        if claim_index not in selected_claim_indices:
            drop_claim(
                f"claim[{claim_index}] does not exactly support a promoted accepted field"
            )
    start = max(0.0, float(candidate.get("start_sec") or 0))
    end = min(duration, max(start, float(candidate.get("end_sec") or start)))
    evidence_ids = sorted(
        {
            item
            for claim in selected_claims
            for item in claim["evidence_ids"]
        }
    )
    if not evidence_ids:
        fatal_reasons.append("step has no accepted evidence IDs")
    if fatal_reasons:
        return None, fatal_reasons + dropped_claim_reasons
    step = {
        "step_id": str(candidate.get("step_id") or ""),
        "window_index": int(candidate.get("window_index") or 0),
        "time_range": str(candidate.get("time_range") or f"{timestamp_label(start)}-{timestamp_label(end)}"),
        "start_sec": round(start, 3),
        "end_sec": round(end, 3),
        "action": action,
        "object": object_name,
        "parameters": dict(parameters),
        "relation_type": relation_type,
        "visual_result": accepted_optional["visual_result"],
        "material_color": accepted_optional["material_color"],
        "spatial_relation": spatial_relation,
        "surface_detail": accepted_optional["surface_detail"],
        "implementation_notes": accepted_optional["implementation_notes"],
        "evidence_ids": evidence_ids,
        "claims": selected_claims,
    }
    return step, dropped_claim_reasons


def reconcile_steps(
    steps: Sequence[Mapping[str, Any]],
    *,
    conflicts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ordered = sorted((dict(item) for item in steps), key=lambda item: (float(item.get("start_sec") or 0), str(item.get("step_id") or "")))
    output: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    for item in ordered:
        raw_object = " ".join(str(item.get("object") or "").split())
        object_key = re.sub(r"\W+", "", raw_object.lower())
        if object_key:
            aliases.setdefault(object_key, raw_object)
            item["object"] = aliases[object_key]
        action_key = re.sub(r"\W+", "", str(item.get("action") or "").lower())
        if output:
            previous = output[-1]
            previous_key = re.sub(r"\W+", "", str(previous.get("action") or "").lower())
            previous_start = float(previous.get("start_sec") or 0)
            previous_end = float(previous.get("end_sec") or previous_start)
            item_start = float(item.get("start_sec") or 0)
            item_end = float(item.get("end_sec") or item_start)
            overlaps = item_start <= previous_end + 1.5 and previous_start <= item_end + 1.5
            objects_compatible = (
                not item.get("object")
                or not previous.get("object")
                or item.get("object") == previous.get("object")
            )
            semantic_duplicate = (
                overlaps
                and objects_compatible
                and min(len(action_key), len(previous_key)) >= 10
                and SequenceMatcher(None, action_key, previous_key).ratio() >= 0.62
            )
            if semantic_duplicate and action_key != previous_key:
                def specificity(step: Mapping[str, Any]) -> tuple[int, int, float]:
                    promoted = len(step.get("parameters") or {})
                    promoted += sum(
                        bool(str(step.get(field_name) or "").strip())
                        for field_name in (
                            "object",
                            "visual_result",
                            "material_color",
                            "spatial_relation",
                            "surface_detail",
                            "implementation_notes",
                        )
                    )
                    return (
                        promoted,
                        len(re.sub(r"\s+", "", str(step.get("action") or ""))),
                        float(step.get("end_sec") or 0),
                    )

                kept, dropped = (
                    (item, previous)
                    if specificity(item) > specificity(previous)
                    else (previous, item)
                )
                output[-1] = kept
                if conflicts is not None:
                    conflicts.append(
                        {
                            "stage": "global_reconciliation",
                            "code": "semantic_duplicate_step",
                            "kept_step_id": str(kept.get("step_id") or ""),
                            "dropped_step_id": str(dropped.get("step_id") or ""),
                        }
                    )
                continue
            close = abs(float(item.get("start_sec") or 0) - float(previous.get("start_sec") or 0)) <= 1.5
            if close and action_key == previous_key and item.get("object") == previous.get("object"):
                previous["end_sec"] = max(float(previous.get("end_sec") or 0), float(item.get("end_sec") or 0))
                previous["evidence_ids"] = sorted(set(previous.get("evidence_ids") or []) | set(item.get("evidence_ids") or []))
                previous["claims"] = list(previous.get("claims") or []) + [claim for claim in item.get("claims") or [] if claim not in previous.get("claims", [])]
                for key, value in (item.get("parameters") or {}).items():
                    if not _not_unknown(value):
                        continue
                    if key in previous["parameters"] and not _values_equal(
                        previous["parameters"][key], value
                    ):
                        if conflicts is not None:
                            conflicts.append(
                                {
                                    "stage": "global_reconciliation",
                                    "code": "conflicting_parameter",
                                    "parameter": key,
                                    "step_ids": [
                                        str(previous.get("step_id") or ""),
                                        str(item.get("step_id") or ""),
                                    ],
                                }
                            )
                        previous["parameters"].pop(key, None)
                        previous["claims"] = [
                            claim
                            for claim in previous["claims"]
                            if not (
                                claim.get("kind") == "parameter"
                                and claim.get("parameter") == key
                            )
                        ]
                    else:
                        previous["parameters"][key] = value
                for field_name in (
                    "visual_result",
                    "material_color",
                    "spatial_relation",
                    "surface_detail",
                    "implementation_notes",
                ):
                    left = str(previous.get(field_name) or "").strip()
                    right = str(item.get(field_name) or "").strip()
                    if left and right and left != right:
                        if conflicts is not None:
                            conflicts.append(
                                {
                                    "stage": "global_reconciliation",
                                    "code": "conflicting_field",
                                    "field": field_name,
                                    "step_ids": [
                                        str(previous.get("step_id") or ""),
                                        str(item.get("step_id") or ""),
                                    ],
                                }
                            )
                        previous[field_name] = ""
                    elif right:
                        previous[field_name] = right
                continue
        output.append(item)
    for index, item in enumerate(output, 1):
        item["step_id"] = f"STEP-{index:03d}"
        item["time_range"] = (
            f"{timestamp_label(float(item.get('start_sec') or 0))}-"
            f"{timestamp_label(float(item.get('end_sec') or 0))}"
        )
    return output


def tutorial_markdown(title: str, source_url: str, steps: Sequence[Mapping[str, Any]], evidence_by_id: Mapping[str, Mapping[str, Any]]) -> str:
    lines = [f"# {title}", "", f"- 原视频：{source_url or '未记录'}", "- 本教程只包含通过 Claim Q-Gate 的操作；未证实内容见 `uncertain_items.json`。", "", "## 图文步骤", ""]
    if not steps:
        lines.extend(["没有步骤通过 Claim Q-Gate。", ""])
    for index, step in enumerate(steps, 1):
        lines.extend(
            [
                f"### {index}. {step.get('action') or '未命名操作'}",
                "",
                f"- 时间：`{step.get('time_range', '')}`",
                f"- 对象：{step.get('object') or '未明确'}",
            ]
        )
        parameters = step.get("parameters") or {}
        if parameters:
            lines.append("- 参数：" + "；".join(f"`{key}` = `{value}`" for key, value in parameters.items()))
        for label, key in (
            ("视觉结果", "visual_result"), ("材质/颜色/纹理", "material_color"),
            ("空间关系", "spatial_relation"), ("表面细节", "surface_detail"),
            ("代码复现约束", "implementation_notes"),
        ):
            value = str(step.get(key) or "").strip()
            if value:
                lines.append(f"- {label}：{value}")
        lines.extend(["", "证据：", ""])
        for evidence_id in step.get("evidence_ids") or []:
            item = evidence_by_id.get(str(evidence_id))
            if not item:
                continue
            lines.append(f"![{evidence_id} {item.get('role')} {item.get('timestamp_sec')}s]({item.get('path')})")
        lines.append("")
    lines.extend(["## 使用边界", "", "`tutorial.md` 与 `steps_verified.json` 是同一组已验证步骤的两种视图。候选步骤、OCR 文本或截图不能独立授权额外操作；参数冲突或缺少修改后稳定证据时必须回到视频复核。", ""])
    return "\n".join(lines)


def render_html(output_root: Path, title: str, source_url: str, steps: Sequence[Mapping[str, Any]], evidence_by_id: Mapping[str, Mapping[str, Any]]) -> None:
    cards = []
    for index, step in enumerate(steps, 1):
        images = []
        for evidence_id in step.get("evidence_ids") or []:
            item = evidence_by_id.get(str(evidence_id))
            if item:
                images.append(f'<figure><img src="{html.escape(str(item.get("path")))}" alt="{html.escape(str(evidence_id))}"><figcaption>{html.escape(str(evidence_id))} · {float(item.get("timestamp_sec") or 0):.2f}s · {html.escape(str(item.get("role")))}</figcaption></figure>')
        parameters = "；".join(f"{key}={value}" for key, value in (step.get("parameters") or {}).items())
        cards.append(f'<section><p class="time">{html.escape(str(step.get("time_range") or ""))}</p><h2>{index}. {html.escape(str(step.get("action") or ""))}</h2><p><strong>对象：</strong>{html.escape(str(step.get("object") or ""))}</p><p><strong>参数：</strong>{html.escape(parameters or "无已验证精确参数")}</p><p>{html.escape(str(step.get("visual_result") or ""))}</p><div class="images">{"".join(images)}</div></section>')
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>body{{margin:0;background:#111318;color:#eef1f7;font:16px/1.65 system-ui}}main{{max-width:1100px;margin:auto;padding:48px 24px}}a{{color:#9fc2ff}}section{{margin:28px 0;padding:24px;background:#1b1f28;border:1px solid #343b49;border-radius:16px}}.time,figcaption{{color:#a7afbd}}.images{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}}figure{{margin:0}}img{{width:100%;height:220px;object-fit:contain;background:#08090c;border-radius:10px}}h1{{font-size:clamp(36px,6vw,72px)}}h2{{margin-top:4px}}</style></head><body><main><h1>{html.escape(title)}</h1><p>原视频：<a href="{html.escape(source_url)}">{html.escape(source_url or "未记录")}</a></p><p>显示层由已验证步骤确定；操作真值仍为 tutorial.md 与 steps_verified.json。</p>{"".join(cards)}</main></body></html>'''
    atomic_text(output_root / "illustrated_tutorial.html", document)


def tutorial_visual_contract(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    contracts = {
        "visible_objects": sorted({str(item.get("object")) for item in steps if str(item.get("object") or "").strip()}),
        "materials": sorted({str(item.get("material_color")) for item in steps if str(item.get("material_color") or "").strip()}),
        "surface_details": sorted({str(item.get("surface_detail")) for item in steps if str(item.get("surface_detail") or "").strip()}),
        "spatial_layout": sorted({str(item.get("spatial_relation")) for item in steps if str(item.get("spatial_relation") or "").strip()}),
        "final_constraints": sorted({str(item.get("visual_result")) for item in steps if str(item.get("visual_result") or "").strip()}),
        "do_not_omit": sorted({str(item.get("implementation_notes")) for item in steps if str(item.get("implementation_notes") or "").strip()}),
    }
    field_hits = {
        "visual_result": sum(bool(str(item.get("visual_result") or "").strip()) for item in steps),
        "material_color": sum(bool(str(item.get("material_color") or "").strip()) for item in steps),
        "spatial_relation": sum(bool(str(item.get("spatial_relation") or "").strip()) for item in steps),
        "surface_detail": sum(bool(str(item.get("surface_detail") or "").strip()) for item in steps),
        "implementation_notes": sum(bool(str(item.get("implementation_notes") or "").strip()) for item in steps),
    }
    return {
        "contracts": [{"window_index": 0, **contracts}],
        "quality": {
            "step_count": len(steps),
            "field_hits": field_hits,
            "contract_items": sum(len(value) for value in contracts.values()),
            "has_visual_text_contract": bool(steps) and sum(len(value) for value in contracts.values()) >= 4,
        },
    }


def package_output_hashes(root: Path) -> dict[str, str]:
    """Hash every deliverable except the self-referential manifest."""

    return {
        path.relative_to(root).as_posix(): sha256_path(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "tutorial_manifest.json"
    }


def remove_appledouble_metadata(root: Path) -> None:
    """Remove macOS sidecars created when copying packages to external disks."""

    if not root.exists():
        return
    for path in sorted(root.rglob("._*"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def verification_batch_size(profile: Profile) -> int:
    return {"economy": 3, "balanced": 2, "forensic": 1}[profile.name]


def coarse_batch_size(profile: Profile) -> int:
    return {"economy": 1, "balanced": 2, "forensic": 1}[profile.name]


def dense_localization_batch_size(profile: Profile) -> int:
    """Keep temporal localization candidate-isolated at the window boundary."""

    # Multiple candidate sheets from the *same* window share one call.  Do not
    # mix windows: short, adjacent Blender edits are already difficult to
    # localize and cross-window context increases semantic borrowing risk.
    return 1


def coarse_context_seconds(profile: Profile) -> float:
    return {"economy": 8.0, "balanced": 12.0, "forensic": 15.0}[profile.name]


def coarse_analysis_bounds(
    start: float, end: float, duration: float, profile: Profile
) -> tuple[float, float]:
    """Return symmetric context while preserving canonical core ownership."""

    context = coarse_context_seconds(profile)
    return max(0.0, start - context), min(duration, end + context)


def _maximum_windows_for_budget(
    call_budget: int, verification_group_size: int,
    coarse_group_size: int = 1, localization_group_size: int = 1,
) -> int:
    # Keep one unused call as headroom for a provider retry decision made by a
    # human/operator. The extractor itself never automatically retries an
    # uncertain delivery.
    usable_budget = max(2, call_budget - 1)
    maximum = 1
    while (
        math.ceil((maximum + 1) / coarse_group_size)
        + math.ceil((maximum + 1) / localization_group_size)
        + math.ceil((maximum + 1) / verification_group_size)
        <= usable_budget
    ):
        maximum += 1
    return maximum


def _select_evenly_indexed(
    values: Sequence[float], limit: int
) -> list[tuple[int, float]]:
    """Mirror the production rich extractor's complete-window sampler.

    The returned index is the position in the unabridged 60-second window
    sequence.  Retaining that index here makes the rounding semantics easy to
    compare with ``prepare_rich_tutorial_evidence.select_evenly`` even though
    the v2 extractor currently needs only the selected bounds.
    """

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


def _window_bounds(
    duration: float,
    call_budget: int,
    requested_windows: int | None,
    verification_group_size: int,
    coarse_group_size: int = 1,
    localization_group_size: int = 1,
) -> list[tuple[float, float]]:
    maximum = _maximum_windows_for_budget(
        call_budget,
        verification_group_size,
        coarse_group_size,
        localization_group_size,
    )
    if requested_windows is not None:
        maximum = min(maximum, max(1, requested_windows))
    # The production rich pipeline first constructs fixed, complete
    # 60-second windows and only then selects evenly if the budget is smaller.
    # Do not repartition the full duration into equal-sized windows: that
    # changes every boundary and makes an A/B comparison observe different
    # evidence.
    starts = list(frange(0.0, duration, 60.0))
    if not starts:
        return [(0.0, max(0.0, duration))]
    return [
        (start, min(duration, start + 60.0))
        for _original_index, start in _select_evenly_indexed(starts, maximum)
    ]


def profile_candidate_limit(profile: Profile) -> int:
    """Return the audited per-window candidate ceiling for a profile."""

    return {"economy": 2, "balanced": 6, "forensic": 8}[profile.name]


def extraction_plan(*, title: str, profile: Profile, model: str, video_file: Path | None, video_url: str | None, output_dir: Path, render_html_enabled: bool, asr_language_hint: str | None = None, provider: str = "api", fallback_reason: str = "") -> dict[str, Any]:
    if provider not in ALLOWED_PROVIDERS:
        raise ExtractionError("provider must be api or codex-cli")
    fallback_reason = validate_model_fallback(model, fallback_reason)
    return {
        "schema": "video2blender-tutorial-extraction-plan.v2",
        "title": title,
        "profile": profile.name,
        "model": model,
        "provider": provider,
        "fallback_reason": fallback_reason,
        "input": {"kind": "local_file" if video_file else "video_url", "value": str(video_file) if video_file else validate_url(str(video_url))},
        "output_dir": str(output_dir),
        "render_html": render_html_enabled,
        "asr_language": asr_language_hint or "auto",
        "writes_on_dry_run": False,
    }


def extract_tutorial(
    *, video_file: Path | None, video_url: str | None, title: str,
    output_dir: Path, profile_name: str, model: str, transcript: Path | None,
    render_html_enabled: bool, endpoint: str = "", secret_file: Path | None = None,
    source_url: str = "", requested_windows: int | None = None,
    provided_transcript_only: bool = False,
    asr_language_hint: str | None = None,
    provider: str = "api", fallback_reason: str = "",
) -> dict[str, Any]:
    if profile_name not in PROFILES:
        raise ExtractionError(f"unknown profile: {profile_name}")
    fallback_reason = validate_model_fallback(model, fallback_reason)
    if provider not in ALLOWED_PROVIDERS:
        raise ExtractionError("provider must be api or codex-cli")
    if source_url:
        source_url = validate_url(source_url)
    profile = PROFILES[profile_name]
    key = ""
    if provider == "api":
        if not endpoint or secret_file is None:
            raise ExtractionError("api provider requires endpoint and secret_file")
        key = read_secret(secret_file)
        client: ModelClient | CodexCliModelClient = ModelClient(
            endpoint=endpoint,
            key=key,
            model=model,
            profile=profile,
            call_budget=1,
        )
    else:
        client = CodexCliModelClient(
            model=model,
            profile=profile,
            call_budget=1,
        )
    target_root = output_dir.expanduser().resolve()
    remove_appledouble_metadata(target_root)
    if target_root.exists() and any(target_root.iterdir()):
        allowed = {"source.info.json", "target_reference.png", "final_reference.png", "source.mp4", "reproduction_run_manifest.json"}
        unexpected = [path.name for path in target_root.iterdir() if path.name not in allowed]
        if unexpected:
            raise ExtractionError("output directory is not empty; refusing to mix tutorial runs: " + ", ".join(sorted(unexpected)[:8]))
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="video2blender-tutorial-") as temporary_value:
        temporary = Path(temporary_value)
        # Build the complete package in a private staging directory. Failed
        # provider calls or validators therefore leave no half-built run that
        # blocks a clean retry.
        output_root = temporary / "package"
        output_root.mkdir(parents=True)
        platform_subtitles: list[Path] = []
        if video_file is not None:
            video = video_file.expanduser().resolve(strict=True)
            source_kind = "local_file"
            platform_attempted = bool(
                source_url and _known_caption_platform(source_url)
            )
            if platform_attempted and not provided_transcript_only:
                platform_subtitles = fetch_platform_subtitles(
                    source_url, temporary / "subtitles"
                )
        else:
            url = validate_url(str(video_url))
            video, platform_subtitles = materialize_url(url, temporary / "download")
            source_kind = "downloaded_temporary"
            source_url = url
            platform_attempted = True
        if not video.is_file():
            raise ExtractionError("video input is not a regular file")
        duration = ffprobe_duration(video)
        source_hash = sha256_path(video)
        transcript_result = resolve_transcript(
            video,
            transcript,
            platform_subtitles,
            platform_attempted=platform_attempted and not provided_transcript_only,
            allow_local_asr=not provided_transcript_only,
            asr_language_hint=asr_language_hint,
        )
        if transcript_result.warning:
            warnings.append(transcript_result.warning)
        atomic_jsonl(output_root / "transcript" / "segments.jsonl", transcript_result.segments)
        atomic_json(
            output_root / "transcript" / "source.json",
            {
                "schema": "video2blender-tutorial-transcript-source.v2",
                "status": transcript_result.status,
                "source": transcript_result.source,
                "attempted_sources": transcript_result.attempted_sources,
                "segment_count": len(transcript_result.segments),
                "rejected_segment_count": transcript_result.rejected_segment_count,
                "quality_reasons": transcript_result.quality_reasons,
                "asr_language_hint": asr_language_hint or "auto",
                "warning": transcript_result.warning,
            },
        )

        call_budget = profile.calls_per_ten_minutes * max(1, math.ceil(duration / 600.0))
        client.call_budget = call_budget
        verification_group_size = verification_batch_size(profile)
        coarse_group_size = coarse_batch_size(profile)
        localization_group_size = dense_localization_batch_size(profile)
        windows = _window_bounds(
            duration,
            call_budget,
            requested_windows,
            verification_group_size,
            coarse_group_size,
            localization_group_size,
        )
        max_candidates_per_window = profile_candidate_limit(profile)
        coarse_root = temporary / "coarse"
        coarse_rows: list[dict[str, Any]] = []
        ocr_rows: list[dict[str, Any]] = []
        coarse_ocr_cache: dict[float, list[dict[str, Any]]] = {}
        previous: Path | None = None
        for index, timestamp in enumerate(frange(0.0, duration, profile.coarse_interval)):
            frame = coarse_root / f"coarse_{index:05d}.jpg"
            extract_frame(video, timestamp, frame, profile.image_max_side)
            coarse_rows.append(
                {"timestamp_sec": timestamp, "path": frame, "change_score": image_change_score(previous, frame)}
            )
            previous = frame
        cue_times = action_cue_timestamps(transcript_result.segments)
        candidates: list[dict[str, Any]] = []
        requests_by_step: dict[str, dict[str, Any]] = {}
        uncertain: list[dict[str, Any]] = []
        rich_windows: list[dict[str, Any]] = []
        coarse_work: list[dict[str, Any]] = []
        context_seconds = coarse_context_seconds(profile)
        for window_index, (start, end) in enumerate(windows):
            # Symmetric context lets a window observe the stable result of an
            # operation crossing either boundary. Candidate starts are still
            # required to belong to the canonical core window below.
            analysis_start, analysis_end = coarse_analysis_bounds(
                start, end, duration, profile
            )
            rows = [
                item
                for item in coarse_rows
                if analysis_start <= float(item["timestamp_sec"]) <= analysis_end
            ]
            window_cues = [
                cue for cue in cue_times if analysis_start <= cue <= analysis_end
            ]
            # Low-change parameter edits cluster near many tutorial segment
            # boundaries. Three fixed probes preserve the early operation,
            # its stable value, and the next state without expanding the
            # sheet beyond the profile's evidence budget.
            mandatory: list[dict[str, Any]] = []
            probe_anchors = [
                min(analysis_end, start + probe_offset)
                for probe_offset in (1.0, 8.0, 13.0)
            ]
            if analysis_end > end:
                probe_anchors.append(
                    min(analysis_end, end + min(4.0, context_seconds))
                )
            for anchor in probe_anchors:
                if not rows:
                    break
                nearest = min(
                    rows,
                    key=lambda item: abs(float(item["timestamp_sec"]) - anchor),
                )
                if (
                    abs(float(nearest["timestamp_sec"]) - anchor)
                    <= profile.coarse_interval * 1.5
                ):
                    mandatory.append(nearest)
            mandatory = list(
                {
                    float(item["timestamp_sec"]): item for item in mandatory
                }.values()
            )
            remaining = max(
                1, profile.coarse_frames_per_window - len(mandatory)
            )
            selected = select_coarse_rows(
                rows,
                cue_times=window_cues,
                limit=remaining,
            )
            selected = sorted(
                {
                    float(item["timestamp_sec"]): item
                    for item in [*selected, *mandatory]
                }.values(),
                key=lambda item: float(item["timestamp_sec"]),
            )[: profile.coarse_frames_per_window]
            ocr_frame_limit = {"economy": 4, "balanced": 6, "forensic": 8}[
                profile.name
            ]
            mandatory_times = {
                float(item["timestamp_sec"]) for item in mandatory
            }
            ocr_selected = list(mandatory)
            ocr_selected.extend(
                item
                for item in sorted(
                    selected,
                    key=lambda value: float(value.get("change_score") or 0),
                    reverse=True,
                )
                if float(item["timestamp_sec"]) not in mandatory_times
            )
            ocr_selected = ocr_selected[:ocr_frame_limit]
            selected_indices = {
                index
                for index, item in enumerate(rows)
                if item in ocr_selected
            }
            ocr_indices = sorted(selected_indices)
            window_ocr: list[dict[str, Any]] = []
            for index in ocr_indices:
                item = rows[index]
                timestamp = float(item["timestamp_sec"])
                if timestamp not in coarse_ocr_cache:
                    coarse_ocr_cache[timestamp] = ocr_image(
                        Path(item["path"]), timestamp
                    )
                    ocr_rows.extend(coarse_ocr_cache[timestamp])
                window_ocr.extend(coarse_ocr_cache[timestamp])
            sheet = output_root / "evidence" / "coarse" / f"window_{window_index:03d}.jpg"
            make_contact_sheet(
                [(f"COARSE_{float(item['timestamp_sec']):.2f}s", Path(item["path"])) for item in selected],
                sheet,
            )
            transcript_text = segments_text(
                transcript_result.segments, analysis_start, analysis_end
            )
            work = {
                "window_index": window_index,
                "start_sec": start,
                "end_sec": end,
                "analysis_start_sec": analysis_start,
                "analysis_end_sec": analysis_end,
                "sheet": sheet,
                "transcript": transcript_text,
                "ocr": window_ocr,
            }
            coarse_work.append(work)
            rich_windows.append(
                {
                    "window_index": window_index,
                    "start_sec": start,
                    "end_sec": end,
                    "analysis_start_sec": analysis_start,
                    "analysis_end_sec": analysis_end,
                    "sheet": sheet.relative_to(output_root).as_posix(),
                    "asr_text": transcript_text,
                    "ocr_samples": window_ocr[:24],
                }
            )

        coarse_batch_count = 0
        for offset in range(0, len(coarse_work), coarse_group_size):
            group = coarse_work[offset : offset + coarse_group_size]
            coarse_batch_count += 1
            result = client.call(
                coarse_batch_prompt(
                    title=title,
                    windows=group,
                    max_candidates=max_candidates_per_window,
                ),
                [Path(item["sheet"]) for item in group],
            )
            reject_sensitive_echo(result, key=key, endpoint=endpoint)
            mapped = split_coarse_batch_result(
                result, [int(item["window_index"]) for item in group]
            )
            for work in group:
                window_index = int(work["window_index"])
                if window_index not in mapped:
                    uncertain.append(
                        {
                            "stage": "coarse_batch_mapping",
                            "window_index": window_index,
                            "detail": {"code": "missing_or_ambiguous_window_result"},
                        }
                    )
                    continue
                window_candidates, requests, window_uncertain = normalize_candidates(
                    mapped[window_index],
                    window_index=window_index,
                    start=float(work["analysis_start_sec"]),
                    end=float(work["analysis_end_sec"]),
                )
                in_core: list[dict[str, Any]] = []
                for candidate in window_candidates:
                    core_start = float(work["start_sec"])
                    core_end = float(work["end_sec"])
                    if not candidate_belongs_to_core(
                        candidate, core_start, core_end
                    ):
                        uncertain.append(
                            {
                                "stage": "candidate_selection",
                                "window_index": window_index,
                                "detail": {
                                    "code": "context_only_candidate_rejected",
                                    "step_id": str(candidate.get("step_id") or ""),
                                },
                            }
                        )
                    else:
                        in_core.append(candidate)
                window_candidates = in_core
                if len(window_candidates) > max_candidates_per_window:
                    dropped = window_candidates[max_candidates_per_window:]
                    uncertain.extend(
                        {
                            "stage": "candidate_selection",
                            "window_index": window_index,
                            "detail": {
                                "code": "candidate_budget_exceeded",
                                "step_id": str(item.get("step_id") or ""),
                            },
                        }
                        for item in dropped
                    )
                    window_candidates = window_candidates[:max_candidates_per_window]
                kept_ids = {
                    str(item.get("step_id") or "") for item in window_candidates
                }
                requests = [
                    item
                    for item in requests
                    if str(item.get("step_id") or "") in kept_ids
                ]
                candidates.extend(window_candidates)
                for request in requests:
                    step_id = str(request.get("step_id") or "")
                    if step_id and step_id not in requests_by_step:
                        requests_by_step[step_id] = request
                uncertain.extend(
                    {
                        "stage": "coarse",
                        "window_index": window_index,
                        "detail": item,
                    }
                    for item in window_uncertain
                )

        evidence_records: list[dict[str, Any]] = []
        dense_packages: list[dict[str, Any]] = []
        for candidate in candidates:
            package, dense_ocr = dense_scan_for_step(
                video=video,
                temporary_root=temporary / "dense",
                profile=profile,
                step=candidate,
                request=requests_by_step.get(str(candidate.get("step_id") or "")),
                duration=duration,
            )
            dense_packages.append(package)
            ocr_rows.extend(dense_ocr)

        localization_batch_count = 0
        candidate_window_indices = sorted(
            {
                int(package.get("candidate", {}).get("window_index") or 0)
                for package in dense_packages
            }
        )
        reserved_verification_calls = math.ceil(
            len(candidate_window_indices) / verification_group_size
        )
        localization_capacity = (
            call_budget - client.usage.calls - reserved_verification_calls
        )
        use_model_localizer = localization_capacity >= len(candidate_window_indices)
        if dense_packages and not use_model_localizer:
            warnings.append(
                "Dense temporal localization used the deterministic heuristic "
                "because no model-call budget remained after reserving Claim Q-Gate."
            )

        for window_index in candidate_window_indices:
            window_packages = [
                package
                for package in dense_packages
                if int(package.get("candidate", {}).get("window_index") or 0)
                == window_index
            ]
            selections: dict[str, dict[str, str]] = {}
            if use_model_localizer:
                localization_batch_count += 1
                value = client.call(
                    dense_localization_prompt(title=title, packages=window_packages),
                    [Path(str(package["sheet"])) for package in window_packages],
                )
                reject_sensitive_echo(value, key=key, endpoint=endpoint)
                selections, localization_issues = merge_dense_localizations(
                    value, packages=window_packages
                )
                uncertain.extend(
                    {
                        "stage": "dense_temporal_localization",
                        "window_index": window_index,
                        **issue,
                    }
                    for issue in localization_issues
                )
                for _item in value.get("uncertain_items") or []:
                    uncertain.append(
                        {
                            "stage": "dense_temporal_localization",
                            "window_index": window_index,
                            "code": "model_reported_uncertainty",
                        }
                    )
            else:
                for package in window_packages:
                    step_id = str(package.get("step_id") or "")
                    selection = heuristic_dense_localization(
                        package, profile=profile
                    )
                    if selection is None:
                        uncertain.append(
                            {
                                "stage": "dense_temporal_localization",
                                "window_index": window_index,
                                "step_id": step_id,
                                "code": "heuristic_localization_failed_closed",
                            }
                        )
                    else:
                        selections[step_id] = selection

            for package in window_packages:
                step_id = str(package.get("step_id") or "")
                selection = selections.get(step_id)
                if selection is None:
                    continue
                ocr_rows.extend(
                    ocr_selected_dense_entries(package, selection)
                )
                evidence_records.extend(
                    materialize_dense_evidence(
                        package=package,
                        selection=selection,
                        output_root=output_root,
                    )
                )
        evidence_by_id = {str(item["image_id"]): item for item in evidence_records}
        decisions: dict[str, dict[str, Any]] = {}
        seen_verification_step_ids: set[str] = set()
        invalid_verification_step_ids: set[str] = set()
        verification_work: list[dict[str, Any]] = []
        for window_index, (start, end) in enumerate(windows):
            window_candidates = [
                item
                for item in candidates
                if int(item.get("window_index") or 0) == window_index
            ]
            if not window_candidates:
                continue
            kept_step_ids = {
                str(item.get("step_id") or "") for item in window_candidates
            }
            window_evidence = [
                item
                for item in evidence_records
                if str(item.get("step_id") or "") in kept_step_ids
            ]
            candidate_packages = build_candidate_verification_packages(
                candidates=window_candidates,
                evidence=window_evidence,
                output_root=output_root,
                sheet_root=temporary / "verify" / f"window_{window_index:03d}",
            )
            verification_work.append(
                {
                    "window_index": window_index,
                    "start_sec": start,
                    "end_sec": end,
                    "candidates": window_candidates,
                    "evidence": window_evidence,
                    "transcript": segments_text(
                        transcript_result.segments, start, end
                    ),
                    "candidate_packages": candidate_packages,
                }
            )

        verification_batch_count = 0
        for offset in range(0, len(verification_work), verification_group_size):
            group = verification_work[offset : offset + verification_group_size]
            group_evidence_packages = [
                package
                for item in group
                for package in item["candidate_packages"]
            ]
            mapped_step_ids = {
                str(package.get("step_id") or "")
                for package in group_evidence_packages
            }
            unmapped_candidates = [
                candidate
                for item in group
                for candidate in item["candidates"]
                if str(candidate.get("step_id") or "") not in mapped_step_ids
            ]
            uncertain.extend(
                {
                    "stage": "verification",
                    "code": "missing_candidate_evidence_package",
                    "step_id": str(candidate.get("step_id") or ""),
                }
                for candidate in unmapped_candidates
            )
            if not group_evidence_packages:
                continue
            verification_batch_count += 1
            group_candidates = [
                candidate
                for item in group
                for candidate in item["candidates"]
                if str(candidate.get("step_id") or "") in mapped_step_ids
            ]
            group_evidence = [
                evidence
                for item in group
                for evidence in item["evidence"]
                if str(evidence.get("step_id") or "") in mapped_step_ids
            ]
            transcript_context = "\n\n".join(
                f"WINDOW W{int(item['window_index']):03d} "
                f"[{float(item['start_sec']):.2f}s-{float(item['end_sec']):.2f}s]\n"
                f"{item['transcript']}"
                for item in group
            )
            value = client.call(
                verification_prompt(
                    title=title,
                    candidates=group_candidates,
                    evidence=group_evidence,
                    evidence_packages=group_evidence_packages,
                    transcript=transcript_context,
                ),
                [Path(item["sheet"]) for item in group_evidence_packages],
            )
            reject_sensitive_echo(value, key=key, endpoint=endpoint)
            verification_issues = merge_verification_batch_decisions(
                value,
                expected_step_ids=(
                    str(candidate.get("step_id") or "")
                    for candidate in group_candidates
                ),
                decisions=decisions,
                seen_step_ids=seen_verification_step_ids,
                invalid_step_ids=invalid_verification_step_ids,
            )
            for issue in verification_issues:
                uncertain.append(
                    {
                        "stage": "verification",
                        "window_indices": [
                            int(entry["window_index"]) for entry in group
                        ],
                        **issue,
                    }
                )
            for _item in value.get("uncertain_items") or []:
                uncertain.append(
                    {
                        "stage": "verification",
                        "window_indices": [
                            int(entry["window_index"]) for entry in group
                        ],
                        "code": "model_reported_uncertainty",
                    }
                )

        accepted: list[dict[str, Any]] = []
        candidate_receipts: list[dict[str, Any]] = []
        for candidate in candidates:
            step_id = str(candidate.get("step_id") or "")
            accepted_step, reasons = qgate_step(candidate, decisions.get(step_id), evidence_by_id, duration)
            gate_status = (
                "uncertain"
                if accepted_step is None
                else "accepted_with_dropped_claims"
                if reasons
                else "accepted"
            )
            candidate_receipts.append(
                {
                    **candidate,
                    "gate": {"status": gate_status, "reasons": reasons},
                }
            )
            if accepted_step:
                accepted.append(accepted_step)
                uncertain.extend(
                    {
                        "stage": "qgate",
                        "step_id": step_id,
                        "code": "dropped_claim",
                        "detail": reason,
                    }
                    for reason in reasons
                )
            else:
                uncertain.append({"stage": "qgate", "step_id": step_id, "detail": reasons})
        reconciliation_conflicts: list[dict[str, Any]] = []
        verified = reconcile_steps(accepted, conflicts=reconciliation_conflicts)
        uncertain.extend(reconciliation_conflicts)
        # Keep the complete four-role recapture set for every accepted source
        # step that contributed a claim. Claims still cite only the frames
        # that prove them; the additional frames preserve auditable context.
        for step in verified:
            claimed_ids = {
                str(evidence_id)
                for evidence_id in step.get("evidence_ids") or []
            }
            source_step_ids = {
                str(evidence_by_id[evidence_id].get("step_id") or "")
                for evidence_id in claimed_ids
                if evidence_id in evidence_by_id
            }
            complete_ids = {
                str(item["image_id"])
                for item in evidence_records
                if str(item.get("step_id") or "") in source_step_ids
                and str(item.get("role") or "")
                in {"pre", "action", "stable", "ocr_best"}
            }
            step["evidence_ids"] = sorted(claimed_ids | complete_ids)
        if not verified:
            warnings.append(
                "No candidate passed Claim Q-Gate; steps_verified.json is intentionally empty."
            )
        elif uncertain:
            warnings.append(
                f"{len(uncertain)} candidate or claim issue(s) remain outside verified steps."
            )
        referenced_ids = {str(evidence_id) for step in verified for evidence_id in step.get("evidence_ids") or []}
        evidence_records = [item for item in evidence_records if str(item["image_id"]) in referenced_ids]
        evidence_by_id = {str(item["image_id"]): item for item in evidence_records}
        for path in (output_root / "evidence" / "frames").glob("*"):
            if path.is_file() and path.stem not in referenced_ids:
                path.unlink()
        atomic_json(output_root / "evidence" / "index.json", {"schema": "video2blender-tutorial-evidence.v2", "images": evidence_records})
        atomic_jsonl(output_root / "ocr" / "observations.jsonl", ocr_rows)
        atomic_json(output_root / "steps_verified.json", {"schema": SCHEMA_STEPS, "steps": verified})
        atomic_json(output_root / "steps_candidates.json", {"schema": SCHEMA_CANDIDATES, "steps": candidate_receipts})
        atomic_json(output_root / "uncertain_items.json", {"schema": "video2blender-tutorial-uncertain.v2", "items": uncertain})
        markdown = tutorial_markdown(title, source_url, verified, evidence_by_id)
        atomic_text(output_root / "tutorial.md", markdown)
        atomic_text(output_root / "tutorial_path_refs.md", markdown)
        atomic_json(output_root / "tutorial_visual_contract.json", tutorial_visual_contract(verified))
        atomic_json(output_root / "rich_evidence" / "windows.json", rich_windows)
        if render_html_enabled:
            render_html(output_root, title, source_url, verified, evidence_by_id)
        manifest = {
            "schema": SCHEMA_MANIFEST,
            "status": "complete_with_warnings" if warnings or uncertain else "complete",
            "created_at": utc_now(),
            "title": title,
            "profile": profile.name,
            "model": model,
            "provider": provider,
            "fallback_reason": fallback_reason,
            "source": {"kind": source_kind, "url": source_url, "sha256": source_hash, "duration_seconds": round(duration, 6)},
            "transcript": {
                "status": transcript_result.status,
                "source": transcript_result.source,
                "attempted_sources": transcript_result.attempted_sources,
                "segment_count": len(transcript_result.segments),
                "rejected_segment_count": transcript_result.rejected_segment_count,
                "quality_reasons": transcript_result.quality_reasons,
            },
            "model_usage": {**asdict(client.usage), "call_budget": call_budget},
            "counts": {
                "windows": len(windows),
                "coarse_batches": coarse_batch_count,
                "dense_localization_batches": localization_batch_count,
                "verification_batches": verification_batch_count,
                "candidates": len(candidates),
                "verified_steps": len(verified),
                "uncertain_items": len(uncertain),
                "evidence_images": len(evidence_records),
            },
            "outputs": package_output_hashes(output_root),
            "warnings": warnings,
            "raw_model_responses_persisted": False,
            "source_video_persisted": False,
        }
        atomic_json(output_root / "tutorial_manifest.json", manifest)
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            output_root,
            target_root,
            dirs_exist_ok=True,
            copy_function=shutil.copyfile,
        )
        remove_appledouble_metadata(target_root)
        return manifest
