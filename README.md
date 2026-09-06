# 3D-Coding-Blender

English | [简体中文](<README ZH.md>)

Reconstruct editable Blender assets from instructional videos or illustrated Markdown tutorials. The pipeline extracts an operational workflow, retrieves applicable Blender knowledge, generates and executes Blender Python, and evaluates the saved project and rendered result.

| Repository-root entry point | Model provider | Use case |
| --- | --- | --- |
| [`run_api.py`](run_api.py) | A configured HTTPS Chat Completions API | Use your own API service and billing account |
| [`run_codex.py`](run_codex.py) | A signed-in Codex CLI | Run the same pipeline without a separate API key |

Both launchers use the **same reconstruction pipeline**. Ordinary video/tutorial runs require **no RW1 dataset, private asset collection, remote worker, or showcase recipe**. Supply any assets required by the tutorial explicitly. Generating Python or saving a `.blend` file alone does not establish a successful reconstruction.

**Contents:** [Quick start](#quick-start) · [Inputs](#input-examples) · [Extraction methods](#two-video-to-tutorial-methods) · [Outputs](#pipeline-and-outputs) · [Knowledge](#knowledge-storage-and-admission) · [Options](#command-line-reference) · [Development](#development-and-limitations)

## Quick start

Prepare Python 3.10+, Blender, and FFmpeg, then choose an API provider or a signed-in Codex CLI. Run the following in a macOS/Linux terminal. Replace example paths with your own files.

```bash
git clone https://github.com/FreedomIntelligence/3D-Coding-Blender.git
cd 3D-Coding-Blender
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

**API:** configure once, then launch.

```bash
python run_api.py --configure --config "$HOME/.config/blender-pipeline/pipeline.json"
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --video-file /path/to/input/tutorial.mp4 --output-dir /path/to/data/run_001
```

**Codex:** install [Codex CLI](https://developers.openai.com/codex/cli/), sign in, then launch.

```bash
codex login
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --output-dir /path/to/data/run_002
```

If Blender is not on `PATH`, add `--blender /path/to/blender`. Each run requires a new, empty output directory outside the repository. The default is the full reconstruction pipeline; add `--extract-only` to stop after tutorial preparation. For a full run, inspect `pipeline_review.json` rather than relying on a model's completion message.

## Requirements

The current runtime targets macOS and Linux. On Windows, use WSL with Linux Python and Linux Blender. Native Windows credential handling and the complete Blender subprocess chain are not yet a validated support target.

| Component | Requirement |
| --- | --- |
| Python | 3.10 or newer; install root `requirements.txt` |
| Blender | A standalone installation compatible with the tutorial's features and APIs; specify its executable with `--blender` |
| FFmpeg / ffprobe | Required for video inspection, frame extraction, and animation encoding; both must be on `PATH` |
| Video URL downloads | yt-dlp, included in Python requirements; alternatively supply a local video |
| API mode | An accessible HTTPS Chat Completions endpoint, private key file, and the configured model |
| Codex mode | Codex CLI on `PATH`, an authenticated account, and model access |

`bpy` and `bpy_extras` come with Blender; do not install them with pip. Access-controlled, paid, deleted, or region-restricted videos may not download automatically. Use an authorized local copy when appropriate; the launcher does not bypass platform restrictions.

Root launchers use a local rendering policy and do not require cluster GPU identifiers or worker-allocation records. Cycles uses CPU by default. To request an available accelerator, set `VIDEO2BLENDER_CYCLES_BACKEND=METAL` on Apple Silicon, or the applicable `CUDA`, `OPTIX`, `HIP`, or `ONEAPI` backend. An unavailable requested backend is reported as an error, not described as successful GPU execution.

Default knowledge retrieval uses a local manifest and lexical matching. It does not download embedding models or require Qdrant. Install the optional vector-search dependencies only when you intend to build and use that backend:

```bash
python -m pip install -r skills/blender-pipeline/generation/requirements-knowledge.txt
```

Platform subtitles are preferred, followed by a supplied transcript and then local ASR. Local ASR tries installed MLX Whisper, OpenAI Whisper, and faster-whisper implementations. Install one if needed, for example `python -m pip install openai-whisper`; its first use may download a model. Tesseract OCR is optional. Missing or unusable speech evidence produces a warning; uncertain parameters must not become verified facts.

## Provider configuration

### API mode

The interactive `--configure` command creates configuration outside the repository. It accepts a credential-free HTTPS endpoint ending in `/chat/completions`, with no query string or fragment. Secret input is hidden. The API key is stored separately as `model_api_key` with POSIX mode `0600`; the JSON configuration stores its path, not the secret. The command does not overwrite an existing configuration or key.

See [`pipeline.config.example.json`](pipeline.config.example.json) for the four supported configuration fields: `endpoint`, `api_key_file`, `model`, and `blender`. Real configuration files, keys, cookies, and proxy credentials must remain outside Git.

You can also supply settings directly:

```bash
python run_api.py --endpoint https://YOUR_PROVIDER/v1/chat/completions \
  --api-key-file /private/path/model_api_key \
  --video-file /path/to/input/tutorial.mp4 --output-dir /path/to/data/api_run
```

Endpoint, key path, and Blender executable can also use `BLENDER_PIPELINE_API_ENDPOINT`, `BLENDER_PIPELINE_API_KEY_FILE`, and `BLENDER_PIPELINE_BLENDER`. Precedence is command line, then configuration, then environment. Other run options belong on the command line.

The default model is explicitly `gpt-5.6-sol`. If that model is genuinely unavailable, select `--model gpt-5.5 --fallback-reason "Actual reason sol is unavailable"`. There is no silent model substitution or GPT-5.4 fallback. A third-party provider is responsible for its model-name-to-backend mapping; this repository cannot independently attest to that mapping.

### Codex mode

Codex handles tutorial extraction, Blender code generation, and visual review through non-interactive `codex exec`; Blender itself runs locally. The launcher does not silently switch these stages to the API provider. It uses the same explicit model-selection policy and consumes the signed-in account's applicable usage allowance. Model calls use a read-only workspace; the subsequent controlled Blender wrapper executes the generated code. See the [official Codex CLI reference](https://developers.openai.com/codex/cli/reference/) for CLI behavior.

To also make the router available in Codex conversations, optionally link the checked-out skill from the repository root:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

Then invoke `$blender-pipeline` with the input and output paths in a Codex conversation. Do not overwrite an existing installation; keep this checkout at the linked location. Codex supports symlinked user skills; restart it if the skill does not appear. See [local skill discovery](https://developers.openai.com/codex/skills/). This optional step is **not required** for `run_codex.py`.

### Plan or check before a run

```bash
# Print a plan: no writes, downloads, key reads, or model requests.
python run_codex.py --video-url https://www.bilibili.com/video/YOUR_BVID/ \
  --output-dir /path/to/data/planned_run --dry-run

# Each check makes one small real model request and checks local dependencies.
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --check --output-dir /path/to/data/api_check
python run_codex.py --check --output-dir /path/to/data/codex_check
```

A startup check is not an asset-reconstruction test. Check directories must also be new and empty.

## Input examples

The examples below use Codex. For API mode, replace `run_codex.py` with `run_api.py --config /private/path/pipeline.json`; the remaining arguments are unchanged.

| Scenario | Required input | Options |
| --- | --- | --- |
| Tutorial modeling from scratch | One video file or URL | `--video-file` / `--video-url` |
| Material, rigging, or animation tutorial on an existing model | Video and starting model | `--asset`; add `--asset-root` for dependencies |
| Tutorial using external textures or images | Video and the actual resource files | Repeat `--input-asset` |
| Starting asset with an input preview | Starting model and its preview | `--preview`; this is not a final target |
| A separate desired final image | Tutorial and finished-result reference | `--target-image` |
| Existing illustrated tutorial | Markdown with images, or a separate final target for text-only Markdown | `--tutorial`; text-only full replay also needs `--target-image` |
| Tutorial preparation only | Video/tutorial and necessary supporting inputs | `--extract-only`; optionally `--render-html` |

Starting assets may be `.blend`, `.glb`, `.gltf`, `.obj`, or `.fbx`. Recognizing a file format does not guarantee lossless migration of all modifiers, add-ons, or external dependencies.

### Video file or URL only

```bash
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --title "Tutorial title" --output-dir /path/to/data/video_file_run
python run_codex.py --video-url https://www.bilibili.com/video/YOUR_BVID/ \
  --title "Tutorial title" --output-dir /path/to/data/video_url_run
```

The video must actually teach the required operations. A showcase clip, a chapter starting midway through a missing project, or a tutorial requiring unavailable models, textures, or add-ons cannot be reliably converted into a complete from-scratch workflow. A model cannot access a file that you have not supplied.

### Video, starting `.blend`, and input preview

For a chromatic-duck material tutorial, supply the original duck project as the starting asset, not the finished chromatic result:

```bash
python run_codex.py --video-file /path/to/input/chromatic_duck.mp4 \
  --asset /path/to/input/original_duck.blend \
  --preview /path/to/input/original_duck.png \
  --output-dir /path/to/data/chromatic_duck_run
```

The pipeline copies and inspects the source. A trusted wrapper opens it before generated code executes, and the model receives structural information about its objects, materials, and image dependencies, alongside supplied previews. Output checks require actual use of source objects, not merely loading the file before making an unrelated model. The original file is not overwritten.

For external textures or linked libraries, supply a self-contained, authorized bundle:

```text
duck_bundle/
  original_duck.blend
  textures/body.png
  libraries/parts.blend
```

```bash
python run_codex.py --video-file /path/to/input/chromatic_duck.mp4 \
  --asset /path/to/input/duck_bundle/original_duck.blend \
  --asset-root /path/to/input/duck_bundle \
  --preview /path/to/input/original_duck.png \
  --output-dir /path/to/data/duck_bundle_run
```

`--asset-root` copies the entire specified directory while preserving relative structure. Point it only at the relevant input bundle, not an entire data drive. Prefer relative paths or packed resources in `.blend` files. Missing dependencies or unprovided machine-specific paths block execution rather than being replaced with arbitrary resources. External caches, custom add-ons, and specialized nodes still require a compatible environment. Symlink-containing bundles are not accepted; provide self-contained copies.

### Supporting files and a final reference

```bash
python run_codex.py --video-file /path/to/input/card.mp4 \
  --input-asset /path/to/input/card_front.png \
  --input-asset /path/to/input/textures --output-dir /path/to/data/card_run

python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --asset /path/to/input/starter.glb --preview /path/to/input/before.png \
  --target-image /path/to/input/expected.png --output-dir /path/to/data/reference_run
```

Supporting files enter the tutorial input package. Supported images are checked, loaded, and packed into Blender by the trusted wrapper; generated code uses their existing data blocks. Documents can be preserved as resources but are not automatically interpreted as executable modeling instructions. This is not an arbitrary batch-model-import interface: use `--asset` for the authoritative starting model, with source-referenced dependencies under `--asset-root`.

### Existing Markdown tutorial

```bash
python run_codex.py --tutorial /path/to/input/tutorial.md \
  --output-dir /path/to/data/markdown_run
python run_codex.py --tutorial /path/to/input/tutorial.md \
  --asset /path/to/input/starter.blend --preview /path/to/input/starter.png \
  --render-html --output-dir /path/to/data/markdown_asset_run
```

Full reconstruction needs tutorial images or a separate final target for visual review. **Text-only Markdown requires `--target-image`**; otherwise the launcher stops before model calls. `--preview` describes the starting state and cannot substitute for final-result evidence. Text-only preparation with `--extract-only` does not require a target:

```bash
python run_codex.py --tutorial /path/to/input/text_only.md \
  --target-image /path/to/input/expected.png --output-dir /path/to/data/text_replay
python run_codex.py --tutorial /path/to/input/text_only.md \
  --extract-only --render-html --output-dir /path/to/data/text_tutorial_only
```

Use numbered operation headings such as `## 1. Create the mesh` or `## Step 1: Create the mesh`, a top-level ordered operation list, or legacy-rich's numbered operations inside its original windows. Keep local images alongside the Markdown or in subdirectories. Reference-style images and base64-embedded images are supported. Obtain permission and download remote images locally before supplying the tutorial.

The adapter preserves the source text; users do not need to author intermediate JSON or ask a model to rewrite the tutorial. Its compatibility `steps_verified.json` explicitly labels user-provided operations as not independently verified against a video. The filename does not turn supplied prose into validated video evidence.

PDF, DOCX, and arbitrary HTML are not parsed directly. Convert them to appropriately structured Markdown first. Image-only, asset-only, or prompt-only free generation is outside these two tutorial-reconstruction launchers; see [advanced interfaces](#advanced-interfaces).

## Two video-to-tutorial methods

Both API and Codex modes expose the same choice:

| Method | Implementation and output | When to choose it |
| --- | --- | --- |
| `--tutorial-method visual` (default) | Integrated `video-to-visual-tutorial` skill: overview frames, targeted resampling, an evidence ledger, and a continuous illustrated workflow; separate scoring rubric | **Recommended for instructional videos no longer than 10 minutes**, especially when a readable end-to-end tutorial is important |
| `--tutorial-method legacy-rich` | Original production rich workflow: frames sampled every 5 seconds, complete 60-second understanding windows, ordered merging, and base64 images in `tutorial.md`, plus a local-image-path version | Preserve the original end-to-end extraction behavior and downstream compatibility; this is not merely the new tutorial encoded as base64 |

```bash
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --tutorial-method visual --render-html --output-dir /path/to/data/visual_run
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --tutorial-method legacy-rich --render-html --output-dir /path/to/data/rich_run
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --extract-only --render-html --output-dir /path/to/data/tutorial_only
```

HTML rendering is optional and makes no extra model call. It does not change `tutorial.md` or the Blender reconstruction input. Base64 is an image encoding, not modeling code: readers supporting data-URI images render it normally, while other editors expose the encoded text. Use `tutorial_path_refs.md` or HTML in those editors.

The visual method defaults to `balanced`, with approximately 16 calls per ten minutes; `economy` and `forensic` use their respective budgets. Legacy-rich budgets for every required window and one repair call. `--max-extraction-calls` adds an explicit cap. Insufficient budgets stop extraction rather than silently discarding the rest of the video. Document-format repair is bounded, not an unlimited retry loop.

`--transcript` accepts supported JSON/JSONL, SRT, VTT, and platform JSON subtitle structures. A JSONL segment can be `{"start": 0, "end": 4.2, "text": "Add a cube."}`, with times in seconds. Untimestamped prose is not accepted as temporal evidence. Platform subtitles take priority, then the supplied transcript, then local ASR. `--asr-language` is an ASR hint, not a tutorial-output-language selector.

```bash
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --transcript /path/to/input/subtitles.srt --asr-language en \
  --profile economy --max-extraction-calls 8 --extract-only --render-html \
  --output-dir /path/to/data/transcript_run
```

The extraction-only lower-level interface is also available:

```bash
python skills/blender-pipeline/tutorial-extraction/scripts/extract_video_tutorial.py \
  --video-file /path/to/input/tutorial.mp4 --title "Tutorial title" \
  --output-dir /path/to/data/tutorial_package \
  --provider codex-cli --tutorial-method visual
```

Unlike the root launchers, that interface does not continue into Blender.

## Pipeline and outputs

```text
Stage explicit inputs and copy supporting assets
  → Extract a video tutorial / preserve supplied Markdown
  → Retrieve applicable local knowledge
  → Build material, motion, and Blender-version constraints
  → Generate Blender Python
  → Check code and execute Blender
  → Save, reopen, and render
  → Evaluate materials, static/dynamic output, and visual agreement
```

The output directory contains the following, when their corresponding stage completes:

| File or directory | Purpose |
| --- | --- |
| `tutorial.md`, `tutorial_path_refs.md`, `steps_verified.json` | Operational input used for reconstruction |
| `asset.blend`, `reproduce.py`, `render.png` | Generated project, reproduction code, and render |
| `six_views/` | Static-object multi-view renders |
| `final_effect.mp4` | Applicable dynamic result; a turntable is not a substitute for required simulation or deformation |
| `pipeline_review.json` | Current route's acceptance status and unresolved requirements |
| `launch_manifest.json`, `input_assets.json` | Launch configuration and supplied-resource records |
| `knowledge_retrieval_pack.json` | Knowledge retrieved for this run |
| `illustrated_tutorial.html` | Optional human-readable rendering |

The visual method additionally produces its learner input package and scoring rubric. The rubric is not supplied as modeling instructions. A saved output does not automatically imply that all later stages passed.

Run controls, the model ledger, and temporary files are under `.control/`. Existing execution components retain model outputs and rendering diagnostics under `replay/` and `agent_trace/`. Video-analysis cache is separate, on the same data volume at the sibling `.video-tutorial-cache/<run-directory-name>/`. Videos, images, assets, and private execution records do not belong in the source repository. Before sharing a result, review its material rights and private metadata; a whole run directory is not automatically a publication-ready dataset package.

| Exit code | Meaning |
| --- | --- |
| `0` | The requested stage succeeded; `--check` and `--extract-only` do not establish full reconstruction success |
| `2` | A full run produced a result that still needs review |
| `1` | Input, dependency, provider, or execution failure blocked the run |

Passing automated checks is not a guarantee of 100% fidelity for arbitrary tutorials and does not replace human review.

## Knowledge storage and admission

### Storage boundaries

1. **Maintained general guidance:** [`skills/blender-pipeline/knowledge/`](skills/blender-pipeline/knowledge/index.md). Six manifest-listed Markdown documents cover routing, generation, editing, rendering, and operational decisions. The builder also includes router and generation-entry guidance. Their `curated` status means maintained guidance, not independently verified reproduction of every showcase.
2. **Admitted successful experience:** `reviewed` entries require hash-bound approval evidence and a defined applicability scope before active retrieval.
3. **Run-local indexes:** root launchers build a manifest under `.control/knowledge`; they do not scan personal run histories or read RW1.
4. **Showcase catalog metadata:** `reproduction/knowledge/manifest.json` records works, rights, and recipe availability. It is separate from the active success-knowledge store. The public snapshot currently contains 61 metadata entries with recipes disabled pending independent visual-equivalence acceptance. Generic tutorial runs neither depend on nor bypass those recipe gates.

Failed cases, failed-run fragments, unreviewed candidates, and deprecated entries are excluded from active retrieval. Failed records do not become knowledge chunks. Useful preventive rules, such as stopping when dependencies are missing, remain guidance rather than failed-case material.

### Exact chunking policy

The unit of reusable knowledge is a **decision rule**, not a video, task identifier, or accumulated incident log:

- Recognize Markdown heading levels 1–4 outside fenced code; retain non-empty text preceding the first heading. Repeated headings receive stable occurrence suffixes.
- Pack complete blank-line-delimited paragraphs. Bundled-guidance chunks are capped at **2,200 characters**; successful-tutorial candidates use **2,600 characters**. An overlong paragraph is split by character without dropping its tail. Chunks have **zero overlap**.
- Bundled fragments under 80 characters are not independently indexed. Character limits are neither token budgets nor video-analysis window lengths.
- `source_id` uses source kind, canonical path, and section/chunk identity. Normalized-content hashes detect updates without intentionally duplicating a logical source chunk.

See [knowledge lifecycle rules](skills/blender-pipeline/knowledge/operations-and-knowledge.md) for the maintained contract.

### Retrieval

`build_blender_knowledge_index.py --manifest-only` builds from the bundled allowlist. Retrieval combines the current tutorial, asset category, and Blender features; the default is ten lexical matches without network or vector-database requirements. Vector search is optional after explicitly installing and building it. Both backends filter admission state first, and fallback uses the same active manifest.

Each retrieval excerpt is at most 1,400 characters; the full source remains authoritative. Retrieved advice must not override explicit tutorial operations. Only compatible, independently approved executable recipes can impose hard constraints. Knowledge supports methods and compatibility; it does not replace the video, ignore starting assets, or certify invented parameters.

### Adding knowledge

Root launchers **disable automatic knowledge updates**. They do not write a user's tutorial or failed logs into the public library.

- **Maintain general rules:** edit the relevant guidance with applicability, decision rules, validation methods, and retrieval terms; obtain maintenance review and rebuild. Do not add personal paths, credentials, raw traces, or isolated failure narratives.
- **Collect successful observations:** explicitly set `BLENDER_KNOWLEDGE_ROOT` and invoke the updater. Only observations passing automatic checks become candidates, stored outside the active library in `<knowledge-root>_candidates/candidates.jsonl`. Failed, incomplete, or unaccepted runs yield no knowledge chunks. Launcher environment settings do not persist into later shell commands:

  ```bash
  BLENDER_KNOWLEDGE_ROOT=/path/to/data/knowledge python \
    skills/blender-pipeline/generation/scripts/update_replay_knowledge_base.py \
    --video-dir /path/to/run
  ```

- **Admit reusable successful experience:** require at least **five independent, human-accepted assets**, plus a disjoint human-reviewed holdout with **zero regression**. Approval includes a review ID, asset SHA-256 hashes, rule identity, route, asset class, and Blender-version scope. Maintenance code passes `promotion_evidence` to `append_unique(...)`; `candidate_promotion_guard` and hash-bound checks must pass before entries become `reviewed`. There is no force-promote CLI.

A normal Blender exit, a rendered image, a model's self-assessment, or a repaired exception is insufficient for public success-knowledge admission. Even successful candidates remain outside active retrieval until approved.

## Budgets and security

- Extraction and replay have separate call limits: `--max-extraction-calls` and `--max-replay-calls`. Their sum bounds those two stages; video length, resolution, and provider pricing still affect cost.
- Replay defaults to 8 calls, a conservative 500,000-token reservation budget, and 2 total attempts including the initial attempt. Adjust `--max-replay-tokens` and `--repair-attempts` explicitly. A call budget is not a monetary quote.
- Persisted calls can be reused by the execution layer. Ambiguous delivery does not authorize an automatic duplicate paid request; network recovery does not create unlimited retries.
- Generated code is checked before execution and cannot freely access files, download resources, or launch processes. The trusted wrapper handles authorized loading, saving, and rendering. These checks are not an operating-system sandbox; isolate untrusted projects and generated code appropriately.
- Automatic script execution is disabled when opening supplied `.blend` files. Supporting resources and required plug-ins still need a trusted, compatible environment.
- Inputs, frame evidence, tutorial text, and previews used for model inference may be sent to the selected provider. Confirm you have permission to use that service with those materials. Do not infer redistribution rights from download availability.

## Command-line reference

Both root launchers share these options. API-only options do not apply to Codex mode. The API JSON configuration accepts only `endpoint`, `api_key_file`, `model`, and `blender`; other options are command-line arguments.

| Option | Purpose / default |
| --- | --- |
| `--video-file` / `--video-url` / `--tutorial` | Exactly one primary input; not required for `--check` or `--configure` |
| `--title` | Tutorial title; input filename by default, or `video_tutorial` for URL input |
| `--asset`, `--asset-root` | Starting model and its complete dependency bundle containing that model |
| `--input-asset` | Supporting file/directory; repeatable |
| `--preview`, `--target-image` | Starting-state preview (repeatable) and final target (one image); distinct roles |
| `--output-dir` | Required new/empty directory outside the repository, except configuration mode |
| `--config`, `--configure` | Read configuration; interactively create API configuration and private key file |
| `--endpoint`, `--api-key-file` | API-only HTTPS Chat Completions URL and private key file |
| `--blender` | Blender executable; also configurable through JSON, environment, or `PATH` |
| `--model`, `--fallback-reason` | `gpt-5.6-sol` by default; explicit `gpt-5.5` fallback requires an actual unavailability reason |
| `--tutorial-method` | `visual` (default) or `legacy-rich`; supplied Markdown skips extraction |
| `--profile` | Visual extraction budget: `economy`, `balanced` (default), or `forensic`; does not truncate legacy-rich's full-video windows |
| `--transcript`, `--asr-language` | Timestamped transcript and ASR language hint (`auto` by default) |
| `--render-html` | Additional readable HTML without extra model calls |
| `--extract-only` | Stop after tutorial preparation, before Blender generation |
| `--max-extraction-calls` | Total extraction-call cap including repair; method/profile-derived default |
| `--max-replay-calls` | Code generation and visual review combined; default 8 |
| `--max-replay-tokens` | Conservative replay-token reservation cap; default 500,000 |
| `--repair-attempts` | Total attempts including the first: 1, 2, or 3; default 2 |
| `--check` | Dependency checks and one small real model request, not asset reconstruction |
| `--dry-run` | Plan only; no writes, downloads, key reads, or model requests |
| `--help` | Display the current entry point's argument help |

For example, a bounded existing-tutorial run:

```bash
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --tutorial /path/to/input/tutorial.md --max-replay-calls 4 \
  --max-replay-tokens 100000 --repair-attempts 1 \
  --output-dir /path/to/data/bounded_run
```

## Advanced interfaces

The root launchers cover video/Markdown reconstruction with optional source assets. Other maintained interfaces remain separate:

- [Tutorial Extraction Skill](skills/blender-pipeline/tutorial-extraction/SKILL.md): video-to-tutorial only.
- [Generation Skill](skills/blender-pipeline/generation/SKILL.md): strict replay of prepared workspaces and the existing model-direct interface. The latter has release-index/batch constraints; it is not a generic arbitrary-input launcher.
- [Editing Skill](skills/blender-pipeline/editing/SKILL.md): scoped edits to an existing project, with its edit request, before/after records, and non-target-preservation contract.
- [Reproduction Skill](skills/blender-pipeline/reproduction/SKILL.md): license-aware execution of accepted showcase recipes. Disabled or incomplete recipes cannot run.

Material constraints, Blender-version selection, code checks, reopen validation, static/dynamic outputs, and visual review use the shared replay implementation. Model training, RW1 acquisition, benchmark publication, and showcase deployment are not root-launcher responsibilities.

Main code locations:

```text
run_api.py / run_codex.py                 Repository-root launchers
pipeline.config.example.json             Secret-free configuration template
skills/blender-pipeline/
  scripts/                               Shared launcher, input adapters, package checks
  tutorial-extraction/                    Both video-to-tutorial methods
  generation/                            Blender code generation and replay
  editing/                               Separate scoped-editing interface
  reproduction/                          License-aware showcase recipe routing
  knowledge/                             Maintained general guidance
```

## Development and limitations

```bash
python skills/blender-pipeline/scripts/validate_package.py
python -m unittest discover -s skills/blender-pipeline/tests
python -m unittest discover -s skills/blender-pipeline/generation/tests
python -m unittest discover -s skills/blender-pipeline/reproduction/tests
python -m unittest discover -s skills/blender-pipeline/editing/tests
python -m unittest discover -s skills/blender-pipeline/tutorial-extraction/tests -p 'test_*tutorial_pipeline.py'
```

Set `BLENDER_BIN=/path/to/blender` to additionally run the optional real-Blender generation/editing tests. GitHub Actions checks the public package on Python 3.10 and 3.12, without paid model calls. Live provider journeys must be run explicitly with an authorized account and bounded budgets.

Local tests cover interfaces, asset propagation, Markdown adaptation, knowledge admission, and budget behavior. `--check` separately exercises a real provider connection. Neither establishes success on every tutorial or every Blender version. Live model and Blender tests require the corresponding services, allowance, and local applications.

| Problem | Next action |
| --- | --- |
| Blender, FFmpeg, or Codex not found | Install the application and check `PATH`; use an explicit `--blender` path |
| Video URL cannot be fetched | Check access/usage rights; use an authorized local video instead |
| API rejection, insufficient credit, unavailable model | Check provider settings and access; do not silently change models or retry indefinitely |
| Missing textures, linked project, or add-on | Supply the complete `--asset-root` bundle or a compatible environment |
| Markdown operations not recognized | Use numbered operation headings/lists and valid local images, not an outcome-only summary |
| Text-only Markdown lacks visual evidence | Add `--target-image` for full replay, or use `--extract-only`; a starting `--preview` is not a final target |
| Non-empty output directory | Choose a new directory; root launchers do not automatically overwrite or resume an old one |
| A `.blend` exists but exit code is `2` | Inspect unresolved items in `pipeline_review.json`; file existence is not acceptance |
| Animation differs from the video | Check the required simulation, deformation, and timeline; a turntable is insufficient |

When reporting a problem, include a redacted command, OS, Python/Blender versions, failing stage, and a minimal shareable input. Do not post secrets, full private model responses, unauthorized videos, or personal absolute paths. Changes should preserve consistency between both root launchers, both tutorial methods, and knowledge-admission rules.

## License and material rights

This repository does not currently include an explicit software `LICENSE`. Maintainers must select and add one before presenting the repository as licensed open-source software. Public visibility alone is not a blanket permission to redistribute. Videos, images, models, dependencies, and model services have their own applicable terms; this project does not grant rights on behalf of their owners.
