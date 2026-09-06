"""Portable, explicit-input entry point to the maintained replay pipeline.

Dataset acquisition, showcase recipe enablement, and knowledge promotion are
deliberately not launcher responsibilities.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
REPO = PIPELINE.parent.parent
GENERATION = PIPELINE / "generation/scripts"
EXTRACTION = PIPELINE / "tutorial-extraction/scripts"
MODEL_FORMATS = {".blend", ".glb", ".gltf", ".obj", ".fbx"}
IMAGE_FORMATS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".exr", ".hdr"}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parser(provider: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"Tutorial → knowledge-assisted Blender reproduction ({provider}). No RW1 dataset required."
    )
    p.add_argument(
        "--config",
        type=Path,
        help="JSON config; CLI overrides config, then environment",
    )
    p.add_argument(
        "--configure",
        action="store_true",
        help="API only: interactively create private endpoint/key configuration outside the repository",
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument("--video-file", type=Path)
    source.add_argument("--video-url")
    source.add_argument(
        "--tutorial",
        type=Path,
        help="Existing Markdown tutorial with numbered operation headings and local/base64 images",
    )
    p.add_argument("--title", help="Tutorial title; defaults to the input filename")
    p.add_argument(
        "--asset",
        type=Path,
        help="Authoritative starting .blend/.glb/.gltf/.obj/.fbx; never overwritten",
    )
    p.add_argument(
        "--asset-root",
        type=Path,
        help="Optional authorized dependency bundle root containing --asset; copies this complete directory",
    )
    p.add_argument(
        "--input-asset",
        type=Path,
        action="append",
        default=[],
        help="Supporting texture/file/directory, repeatable",
    )
    p.add_argument(
        "--preview",
        type=Path,
        action="append",
        default=[],
        help="Input/starting-state preview, not the final target; repeatable",
    )
    p.add_argument(
        "--target-image",
        type=Path,
        help="Optional finished-result reference, distinct from --preview",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        help="New empty run directory outside the repository (required)",
    )
    p.add_argument(
        "--tutorial-method", choices=("visual", "legacy-rich"), default="visual"
    )
    p.add_argument(
        "--profile", choices=("economy", "balanced", "forensic"), default="balanced"
    )
    p.add_argument("--model", choices=("gpt-5.6-sol", "gpt-5.5"))
    p.add_argument(
        "--fallback-reason",
        default="",
        help="Required only when selecting gpt-5.5 after sol is unavailable",
    )
    p.add_argument("--transcript", type=Path)
    p.add_argument("--asr-language", default="auto")
    p.add_argument(
        "--render-html",
        action="store_true",
        help="Also render a human-readable tutorial; no extra model call",
    )
    p.add_argument(
        "--extract-only",
        action="store_true",
        help="Stop after tutorial preparation, before Blender generation",
    )
    p.add_argument(
        "--max-extraction-calls",
        type=int,
        help="Hard total extraction-call cap, including repair",
    )
    p.add_argument(
        "--max-replay-calls",
        type=int,
        default=8,
        help="Codegen and visual review call cap (default: 8)",
    )
    p.add_argument(
        "--max-replay-tokens",
        type=int,
        default=500000,
        help="Replay ledger token reservation cap",
    )
    p.add_argument(
        "--repair-attempts",
        type=int,
        choices=(1, 2, 3),
        default=2,
        help="Total replay attempts, including the initial attempt",
    )
    p.add_argument("--blender", help="Blender executable path")
    p.add_argument(
        "--endpoint", help="API only: credential-free HTTPS Chat Completions endpoint"
    )
    p.add_argument(
        "--api-key-file",
        type=Path,
        help="API only: private key file outside repository; POSIX mode 0600",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="One small real model request plus local executable checks; does not claim asset reproduction",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the execution plan; no writes, downloads, credential reads, or model calls",
    )
    return p


def existing(path: Path, *, directory: bool = False) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not (resolved.is_dir() if directory else resolved.is_file()):
        raise ValueError(f"Not a {'directory' if directory else 'file'}: {path}")
    return resolved


def external(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path == REPO or path.is_relative_to(REPO):
        raise ValueError(
            "Run outputs and private configuration must be outside the code repository"
        )
    return path


def endpoint_url(value: str) -> str:
    p = urlparse(value)
    if (
        p.scheme != "https"
        or not p.netloc
        or p.username
        or p.password
        or p.query
        or p.fragment
    ):
        raise ValueError(
            "Use a credential-free HTTPS Chat Completions endpoint (no query/fragment)"
        )
    if not p.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("Endpoint must end with /chat/completions")
    return value.rstrip("/")


def configure(args: argparse.Namespace, provider: str) -> int:
    if provider != "api" or not args.config:
        raise ValueError(
            "Use run_api.py --configure --config /private/path/pipeline.json"
        )
    dest = external(args.config)
    key_path = dest.parent / "model_api_key"
    if dest.exists() or key_path.exists():
        raise ValueError(
            "Configuration/key already exists; choose a new location or edit your existing configuration"
        )
    endpoint = endpoint_url(input("HTTPS Chat Completions endpoint: ").strip())
    key = getpass.getpass("API key (hidden): ").strip()
    if not key or "\n" in key:
        raise ValueError("An API key is required")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = []

    def write_private(path: Path, content: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created.append(path)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if os.name == "posix" and (
                metadata.st_mode & 0o777 != 0o600 or metadata.st_uid != os.getuid()
            ):
                raise ValueError(
                    "Private configuration requires owner-only POSIX mode 0600. "
                    "Choose a private local configuration directory on a filesystem "
                    "that supports these permissions, not exFAT."
                )
            stream.write(content)

    try:
        write_private(key_path, key)
        write_private(
            dest,
            json.dumps(
                {
                    "endpoint": endpoint,
                    "api_key_file": str(key_path),
                    "model": "gpt-5.6-sol",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    except BaseException:
        # Roll back only files exclusively created by this invocation. Never
        # report a usable configuration when its filesystem ignored 0600.
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    print(f"Private configuration created: {dest}")
    return 0


def settings(args: argparse.Namespace, provider: str) -> dict:
    config = json.loads(existing(args.config).read_text()) if args.config else {}
    if not isinstance(config, dict) or set(config) - {
        "endpoint",
        "api_key_file",
        "model",
        "blender",
    }:
        raise ValueError(
            "Config supports only endpoint, api_key_file, model, blender; never put an inline API key in it"
        )
    if any(
        not isinstance(value, str) or not value.strip() for value in config.values()
    ):
        raise ValueError("Every configuration value must be a nonempty string")
    model = args.model or config.get("model") or "gpt-5.6-sol"
    if model not in {"gpt-5.6-sol", "gpt-5.5"}:
        raise ValueError("Model must be gpt-5.6-sol, or explicit gpt-5.5 fallback")
    if (model == "gpt-5.5") != bool(args.fallback_reason.strip()):
        raise ValueError(
            "gpt-5.5 requires --fallback-reason; omit that reason for gpt-5.6-sol"
        )
    if args.max_replay_calls < 1 or args.max_replay_tokens < 1:
        raise ValueError("Replay budgets must be positive")
    if args.max_extraction_calls is not None and args.max_extraction_calls < 1:
        raise ValueError("--max-extraction-calls must be positive")
    endpoint = (
        args.endpoint
        or config.get("endpoint")
        or os.environ.get("BLENDER_PIPELINE_API_ENDPOINT", "")
    )
    key = (
        args.api_key_file
        or config.get("api_key_file")
        or os.environ.get("BLENDER_PIPELINE_API_KEY_FILE", "")
    )
    blender = (
        args.blender
        or config.get("blender")
        or os.environ.get("BLENDER_PIPELINE_BLENDER")
        or shutil.which("blender")
    )
    if (
        not blender
        and Path("/Applications/Blender.app/Contents/MacOS/Blender").is_file()
    ):
        blender = "/Applications/Blender.app/Contents/MacOS/Blender"
    needs_model = args.check or not (args.tutorial and args.extract_only)
    if provider == "api" and not args.dry_run and needs_model:
        endpoint = endpoint_url(endpoint)
        if not key:
            raise ValueError("API requires --config, or --endpoint and --api-key-file")
        key = external(existing(Path(key)))
        if os.name == "posix" and (key.stat().st_mode & 0o777) != 0o600:
            raise ValueError(
                "API key file must have mode 0600; run chmod 600 on that private file"
            )
    if (
        provider == "codex-cli"
        and not args.dry_run
        and needs_model
        and not shutil.which("codex")
    ):
        raise ValueError(
            "Codex CLI is not installed/on PATH; install it and run codex login first"
        )
    if not args.dry_run and (not args.extract_only or args.asset):
        if not blender:
            raise ValueError("Blender not found; specify --blender /path/to/blender")
        blender = str(existing(Path(shutil.which(str(blender)) or blender)))
    return {
        "model": model,
        "endpoint": endpoint,
        "key": str(key),
        "blender": blender or "blender",
    }


def check_dependencies(args: argparse.Namespace, options: dict) -> None:
    modules = ["PIL", "requests", "jsonschema"]
    if args.render_html:
        modules.append("markdown")
    absent = [name for name in modules if importlib.util.find_spec(name) is None]
    if absent:
        raise ValueError(
            "Missing Python packages: "
            + ", ".join(absent)
            + "; install requirements.txt"
        )
    if not (args.tutorial and args.extract_only and not args.check):
        for executable in ("ffmpeg", "ffprobe"):
            if not shutil.which(executable):
                raise ValueError(
                    f"Missing {executable}; install FFmpeg and add it to PATH"
                )
    if args.video_url and importlib.util.find_spec("yt_dlp") is None:
        raise ValueError(
            "Missing yt-dlp; install requirements.txt or supply --video-file"
        )
    if args.check:
        result = subprocess.run(
            [options["blender"], "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        if result.returncode:
            raise ValueError("Configured Blender executable failed its version check")


def safe_copy(source: Path, target: Path) -> None:
    if source.is_dir() and target.resolve().is_relative_to(source.resolve()):
        raise ValueError("Output cannot be nested inside a supporting input directory")
    if source.is_symlink() or (
        source.is_dir() and any(p.is_symlink() for p in source.rglob("*"))
    ):
        raise ValueError(
            f"Input bundle contains symlinks; supply a self-contained copy: {source}"
        )
    if target.exists():
        raise ValueError(
            f"Input name collision: {target.name}; use --asset-root to preserve a complete bundle"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(
            source, target, ignore=shutil.ignore_patterns("._*", ".DS_Store")
        )
    else:
        shutil.copy2(source, target)


def command(argv: list[str], env: dict, *, log: Path | None = None) -> None:
    if log:
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                argv, env=env, stdout=stream, stderr=subprocess.STDOUT
            )
    else:
        result = subprocess.run(argv, env=env)
    if result.returncode:
        raise RuntimeError(
            f"Stage {Path(argv[1] if argv[0] == sys.executable else argv[0]).name} exited {result.returncode}"
            + (f"; details: {log}" if log else "")
        )


def runtime_env(
    out: Path, options: dict, provider: str, args: argparse.Namespace
) -> dict:
    env = os.environ.copy()
    # A new public run must not inherit a private dataset worker's path or GPU policy.
    for name in list(env):
        if name.startswith(
            (
                "RW1_",
                "RW2_",
                "TOTAL_ASSET_",
                "VIDEO_REPLAY_",
                "BLENDER_KNOWLEDGE_",
                "VIDEO2BLENDER_",
                "PAPER12_",
            )
        ):
            env.pop(name)
    control = out / ".control"
    temp = control / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(temp),
            "TEMP": str(temp),
            "TMP": str(temp),
            "XDG_CACHE_HOME": str(control / "cache"),
            "HF_HOME": str(control / "cache/huggingface"),
            "TORCH_HOME": str(control / "cache/torch"),
            "MPLCONFIGDIR": str(control / "cache/matplotlib"),
            "VIDEO2BLENDER_OUTPUT_ROOT": str(out),
            "VIDEO2BLENDER_CONFIG_ROOT": str(control),
            "VIDEO2BLENDER_KEEP_TRACE_IN_PLACE": "1",
            "BLENDER_KNOWLEDGE_ROOT": str(control / "knowledge"),
            "VIDEO_REPLAY_PAID_API_LEDGER": str(control / "model_ledger.sqlite3"),
            "VIDEO_REPLAY_PAID_API_MAX_CALLS_PER_ASSET": str(args.max_replay_calls),
            "VIDEO_REPLAY_PAID_API_GLOBAL_MAX_CALLS": str(args.max_replay_calls),
            "VIDEO_REPLAY_PAID_API_MAX_TOKENS_PER_ASSET": str(args.max_replay_tokens),
            "BLENDER_PIPELINE_PROVIDER": provider,
            "BLENDER_PIPELINE_MODEL": options["model"],
            "BLENDER_PIPELINE_BLENDER": options["blender"],
            "BLENDER_PIPELINE_RENDER_DEVICE_POLICY": "local",
            "VIDEO2BLENDER_RENDER_ENGINE": "CYCLES",
            "BLENDER_PIPELINE_FILM_TRANSPARENT": os.environ.get(
                "BLENDER_PIPELINE_FILM_TRANSPARENT", "0"
            ),
            "VIDEO2BLENDER_CYCLES_BACKEND": os.environ.get(
                "VIDEO2BLENDER_CYCLES_BACKEND", "CPU"
            ),
            "BLENDER_PIPELINE_REPAIR_ATTEMPTS": str(args.repair_attempts - 1),
            "BLENDER_PIPELINE_VISUAL_REVIEW": "1",
        }
    )
    if provider == "api":
        env.update(
            {
                "BLENDER_PIPELINE_API_ENDPOINT": options["endpoint"],
                "BLENDER_PIPELINE_API_KEY_FILE": options["key"],
                "VIDEO_REPLAY_APPROVED_PAID_API_ENDPOINT": options["endpoint"],
            }
        )
    else:
        env.pop("BLENDER_PIPELINE_API_KEY_FILE", None)
        env.pop("BLENDER_PIPELINE_API_ENDPOINT", None)
    return env


def stage_inputs(
    args: argparse.Namespace, out: Path, title: str, env: dict, options: dict
) -> list[Path]:
    linked = out / "linked_source"
    linked.mkdir()
    source_asset = existing(args.asset) if args.asset else None
    staged_asset = None
    if args.asset_root:
        root = existing(args.asset_root, directory=True)
        if not source_asset or not source_asset.is_relative_to(root):
            raise ValueError("--asset-root must contain --asset")
        if out.is_relative_to(root):
            raise ValueError("Output cannot be nested inside the input bundle")
        for item in root.iterdir():
            if item.name.startswith("._") or item.name == ".DS_Store":
                continue
            safe_copy(item, linked / item.name)
        staged_asset = linked / source_asset.relative_to(root)
    elif source_asset:
        staged_asset = linked / source_asset.name
        safe_copy(source_asset, staged_asset)
    records = []
    learner = [linked] if args.asset_root else ([staged_asset] if staged_asset else [])
    for role, paths in (("supporting", args.input_asset), ("preview", args.preview)):
        for raw in paths:
            source = raw.expanduser().resolve(strict=True)
            if (
                role == "supporting"
                and args.asset_root
                and source.is_relative_to(existing(args.asset_root, directory=True))
            ):
                target = linked / source.relative_to(
                    existing(args.asset_root, directory=True)
                )
            else:
                target = (
                    linked if role == "supporting" else out / "input_previews"
                ) / source.name
            if not target.exists():
                safe_copy(source, target)
            elif not args.asset_root or not source.is_relative_to(
                existing(args.asset_root, directory=True)
            ):
                raise ValueError("Duplicate input basename: " + source.name)
            if not args.asset_root or not target.is_relative_to(linked):
                learner.append(target)
            files = sorted(target.rglob("*")) if target.is_dir() else [target]
            for path in files:
                if (
                    path.is_file()
                    and not path.name.startswith("._")
                    and path.name != ".DS_Store"
                ):
                    records.append(
                        {"path": str(path), "role": role, "sha256": digest(path)}
                    )
    # List the bundle's packed/external image assets even if no --input-asset was needed.
    for path in sorted(linked.rglob("*")):
        if (
            path.is_file()
            and not path.name.startswith("._")
            and path.suffix.lower() in IMAGE_FORMATS
            and str(path) not in {r["path"] for r in records}
        ):
            records.append(
                {"path": str(path), "role": "supporting", "sha256": digest(path)}
            )
    write_json(
        out / "input_assets.json",
        {"schema": "blender-pipeline-input-assets.v1", "assets": records},
    )
    info = {
        "title": title,
        "source_kind": "video_replay_type2" if source_asset else "video_replay",
        "webpage_url": args.video_url or "",
        "tutorial_input_assets": [str(p) for p in learner],
    }
    if args.video_file:
        info["source_video_path"] = str(existing(args.video_file))
    if staged_asset:
        config = {
            "selected_model_relative": staged_asset.relative_to(linked).as_posix(),
            "selected_model": str(staged_asset),
            "selected_model_sha256": digest(staged_asset),
        }
        info["linked_source"] = config
        write_json(out / "linked_source.json", config)
        command(
            [
                options["blender"],
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--python",
                str(HERE / "inspect_input_asset.py"),
                "--",
                "--asset",
                str(staged_asset),
                "--root",
                str(linked),
                "--output",
                str(out / "linked_asset_audit.json"),
            ],
            env,
            log=out / ".control/input_asset_inspection.log",
        )
        audit = json.loads((out / "linked_asset_audit.json").read_text())
        if audit.get("status") != "pass":
            raise ValueError(
                "Input asset has missing dependencies: " + "; ".join(audit["issues"])
            )
    if args.target_image:
        from PIL import Image

        Image.open(existing(args.target_image)).convert("RGB").save(
            out / "final_reference.png"
        )
    write_json(out / "source.info.json", info)
    return learner


def model_check(out: Path, env: dict, options: dict, provider: str) -> None:
    # The check uses the exact replay transport, not a separate undocumented API path.
    code = """import json, os, sys
from pathlib import Path
from video_replay_model_client import call_chat_completions, read_paid_api_secret
provider=os.environ['BLENDER_PIPELINE_PROVIDER']
r=call_chat_completions(video_dir=Path(sys.argv[1]),stage='codegen',stage_key='startup-check',prompt_version='startup-check-v1',endpoint=os.environ.get('BLENDER_PIPELINE_API_ENDPOINT',''),api_key=read_paid_api_secret(Path(os.environ['BLENDER_PIPELINE_API_KEY_FILE'])) if provider=='api' else '',model=os.environ['BLENDER_PIPELINE_MODEL'],payload={'model':os.environ['BLENDER_PIPELINE_MODEL'],'messages':[{'role':'user','content':'Reply exactly: PIPELINE_READY'}],'max_tokens':100,'reasoning_effort':'low'},timeout=(30,120))
if r.status_code >= 400: raise RuntimeError('Provider rejected startup check: HTTP '+str(r.status_code))
body=r.json(); answer=body['choices'][0]['message']['content']
if 'PIPELINE_READY' not in answer: raise RuntimeError('Unexpected startup response')
print(json.dumps({'provider':provider,'model':os.environ['BLENDER_PIPELINE_MODEL'],'response':'PIPELINE_READY'},ensure_ascii=False))
"""
    process_env = dict(env)
    process_env["PYTHONPATH"] = (
        str(GENERATION) + os.pathsep + process_env.get("PYTHONPATH", "")
    )
    command([sys.executable, "-c", code, str(out)], process_env)


def execute(provider: str, argv: list[str] | None) -> int:
    args = parser(provider).parse_args(argv)
    if args.configure:
        if args.dry_run:
            raise ValueError(
                "--configure cannot be combined with --dry-run; no configuration was written"
            )
        return configure(args, provider)
    if not args.output_dir:
        raise ValueError(
            "--output-dir is required; choose a data disk, not the repository"
        )
    if not args.check and not any((args.video_file, args.video_url, args.tutorial)):
        raise ValueError("Supply exactly one of --video-file, --video-url, --tutorial")
    for path in (
        args.video_file,
        args.tutorial,
        args.asset,
        args.target_image,
        args.transcript,
        *args.preview,
    ):
        if path:
            existing(path)
    if args.asset and args.asset.suffix.lower() not in MODEL_FORMATS:
        raise ValueError("--asset supports .blend, .glb, .gltf, .obj, .fbx")
    if args.asset_root and not args.asset:
        raise ValueError("--asset-root requires --asset")
    if args.asset_root:
        bundle = existing(args.asset_root, directory=True)
        if not existing(args.asset).is_relative_to(bundle):
            raise ValueError("--asset-root must contain --asset")
    for path in args.input_asset:
        resolved = path.expanduser().resolve(strict=True)
        if not (resolved.is_file() or resolved.is_dir()):
            raise ValueError("Supporting inputs must be regular files or directories")
    for path in (*args.preview, *([args.target_image] if args.target_image else [])):
        if path.suffix.lower() not in IMAGE_FORMATS:
            raise ValueError("Preview/target inputs must be supported image files")
    if args.tutorial and args.tutorial.suffix.lower() != ".md":
        raise ValueError(
            "--tutorial currently accepts Markdown (.md), not HTML/PDF/DOCX"
        )
    if args.video_url:
        video_url = urlparse(args.video_url)
        if (
            video_url.scheme != "https"
            or not video_url.netloc
            or video_url.username
            or video_url.password
        ):
            raise ValueError("Video URL must be a credential-free HTTPS URL")
    options = settings(args, provider)
    out = external(args.output_dir)
    if out.exists() and not out.is_dir():
        raise ValueError("--output-dir must name a directory, not an existing file")
    title = (
        args.title or (args.video_file or args.tutorial or Path("video_tutorial")).stem
    )
    plan = {
        "provider": provider,
        "model": options["model"],
        "tutorial_method": "provided" if args.tutorial else args.tutorial_method,
        "title": title,
        "output_dir": str(out),
        "input_project": str(args.asset) if args.asset else None,
        "supporting_inputs": [str(p) for p in args.input_asset],
        "input_previews": [str(p) for p in args.preview],
        "target_image": str(args.target_image) if args.target_image else None,
        "rw1_required": False,
        "knowledge_backend": "bundled reviewed guidance + run-local manifest_lexical",
        "stages": [
            "input staging",
            "tutorial preparation",
            "knowledge retrieval",
            "existing strict Blender replay",
            "route-specific review",
        ],
        "extract_only": args.extract_only,
        "max_extraction_calls": args.max_extraction_calls,
        "max_replay_calls": args.max_replay_calls,
        "max_replay_tokens": args.max_replay_tokens,
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    check_dependencies(args, options)
    if out.exists() and any(out.iterdir()):
        raise ValueError(
            "Output directory must be new/empty; previous results are never silently overwritten"
        )
    out.mkdir(parents=True, exist_ok=True)
    env = runtime_env(out, options, provider, args)
    write_json(out / "launch_manifest.json", plan)
    if args.check:
        model_check(out, env, options, provider)
        return 0
    learner = stage_inputs(args, out, title, env, options)
    if args.tutorial:
        from provided_tutorial import stage

        tutorial_manifest = stage(
            existing(args.tutorial), out, render_html=args.render_html
        )
        if (
            not args.extract_only
            and not args.target_image
            and not tutorial_manifest["images"]
        ):
            raise ValueError(
                "Full replay needs tutorial images or --target-image for visual comparison. "
                "An input-state --preview is not a finished-result reference. "
                "Use --extract-only to prepare a text-only tutorial without replay."
            )
    else:
        cmd = [
            sys.executable,
            str(EXTRACTION / "extract_video_tutorial.py"),
            "--video-file" if args.video_file else "--video-url",
            str(existing(args.video_file)) if args.video_file else args.video_url,
            "--title",
            title,
            "--output-dir",
            str(out),
            "--workspace-mode",
            "--provider",
            provider,
            "--model",
            options["model"],
            "--tutorial-method",
            args.tutorial_method,
            "--profile",
            args.profile,
            "--asr-language",
            args.asr_language,
            "--cache-dir",
            str(out.parent / ".video-tutorial-cache" / out.name),
        ]
        for item in learner:
            cmd.extend(["--input-asset", str(item)])
        if args.transcript:
            cmd.extend(["--transcript", str(existing(args.transcript))])
        if args.fallback_reason:
            cmd.extend(["--fallback-reason", args.fallback_reason])
        if args.max_extraction_calls is not None:
            cmd.extend(["--max-calls", str(args.max_extraction_calls)])
        if args.render_html:
            cmd.append("--render-html")
        command(cmd, env, log=out / ".control/tutorial_extraction.log")
    if args.extract_only:
        print(f"Tutorial ready: {out / 'tutorial.md'}")
        return 0
    command(
        [
            sys.executable,
            str(GENERATION / "build_blender_knowledge_index.py"),
            "--manifest-only",
        ],
        env,
        log=out / ".control/knowledge_index.log",
    )
    command(
        [
            sys.executable,
            str(GENERATION / "run_video_replay_main.py"),
            "--video-dir",
            str(out),
            "--out-name",
            "replay",
            "--skip-knowledge-update",
            "--workflow-evidence",
            "off",
        ],
        env,
        log=out / ".control/reproduction.log",
    )
    review = json.loads((out / "pipeline_review.json").read_text())
    if review.get("status") != "pass":
        print(
            f"Generated result needs review; it is not an accepted reproduction: {out / 'pipeline_review.json'}"
        )
        return 2
    print(f"Reproduction passed its current route checks: {out / 'asset.blend'}")
    return 0


def main(provider: str, argv: list[str] | None = None) -> int:
    try:
        return execute(provider, argv)
    except (
        ValueError,
        OSError,
        RuntimeError,
        ImportError,
        subprocess.TimeoutExpired,
    ) as exc:
        # Do not dump configuration, environment, or provider response bodies.
        print(f"Pipeline stopped: {exc}", file=sys.stderr)
        return 1
