# BlenderLore

<p align="center">
  <strong>Learning 3D Coding from Internet Tutorial Videos</strong>
</p>

<p align="center">
  English | <a href="README_zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://3d-coding-blender.github.io/"><img src="https://img.shields.io/badge/Project-Website-7b61ff" alt="Project Website"></a>
  <a href="https://github.com/3D-Coding-Blender/3D-Coding-Blender.github.io"><img src="https://img.shields.io/badge/GitHub-Repository-111111.svg?logo=github" alt="GitHub Repository"></a>
  <a href="https://huggingface.co/"><img src="https://img.shields.io/badge/HuggingFace-Dataset-f6c344.svg?logo=huggingface" alt="Hugging Face Dataset"></a>
  <img src="https://img.shields.io/badge/Paper-Coming_Soon-8f7ad8" alt="Paper Coming Soon">
</p>

> **Status:** Research prototype. The paper and dataset will be released soon.

## Overview

Internet Blender tutorials contain rich, real-world creation knowledge, but that knowledge is difficult for an agent to use directly. Important instructions may appear in narration, on-screen captions, changing interface states, or brief node-graph operations.

BlenderLore converts tutorial videos into timestamped multimodal evidence, reconstructs the demonstrated workflow, and generates executable Blender Python. Each run delivers an editable Blender project, a reproduction script, renders, and validation results.

Generalization is central to BlenderLore. Successful reconstructions are retained as candidate procedural knowledge. When facing an unfamiliar generation or editing task, the agent decomposes the target into reusable construction patterns, retrieves relevant procedural knowledge, and recombines it into task-specific Blender code.

## Method

![BlenderLore pipeline overview](https://BlenderLore.github.io/assets/method-pipeline.png)

1. **Collect tutorials** — select a high-quality Blender tutorial and define the target asset or supported motion.
2. **Recover evidence** — align visual keyframes, OCR, narration, timestamps, interface actions, and Blender-version cues.
3. **Specify the workflow** — convert the evidence into ordered operations and retrieve relevant procedural knowledge.
4. **Code, run, and repair** — generate Blender Python, execute it, render the scene, compare the result, and repair failures.
5. **Verify and retain** — package editable assets and visual evidence, then retain validated patterns for future tasks.

## What You Get

### 01 · An End-to-End Agent Pipeline

Each run recreates a tutorial workflow and delivers an editable Blender project, a reproduction script, renders, and validation results.

- Editable Asset — `asset.blend`
- Reproduction Script — `reproduce.py`
- Materials & Node Graphs
- Final Render
- Multi-View Renders
- Animation
- Agent Log

### 02 · A High-Quality 3D Dataset

Through collection, repair, and reconstruction, we built a high-quality dataset of 23K procedural 3D assets.

### 03 · A Reusable Procedural Knowledge Library

Successful workflows are retained as reusable procedural knowledge. For unfamiliar targets, the agent decomposes the task, retrieves relevant patterns, and recombines them into task-specific Blender code.

## Quick Start

### 1. Install prerequisites

Install Python 3.10+, Blender, and FFmpeg (including `ffprobe`), with Blender and FFmpeg on `PATH`. Commands below target macOS/Linux; on Windows, use Linux Python and Blender in WSL. Replace example paths with your own.

```bash
git clone https://github.com/FreedomIntelligence/BlenderLore.git
cd BlenderLore
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

For Codex, install and sign in to Codex CLI using the [official documentation](https://developers.openai.com/codex/cli/), and ensure the client can run `codex`.

### 2. Install Skills

Run from the repository root to install the Skill into the current user's Codex environment:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

### 3. Configure rendering

Cycles uses the CPU by default. Omit the Blender path setting if Blender is already on `PATH`.

```bash
export BLENDER_PIPELINE_BLENDER=/path/to/blender
export VIDEO2BLENDER_CYCLES_BACKEND=CPU
```

For GPU rendering, replace `CPU` with the appropriate backend: `OPTIX` or `CUDA` for NVIDIA, `METAL` for Apple Silicon, `HIP` for AMD, or `ONEAPI` for Intel.

### 4. Configure inputs and outputs

Keep input files on your computer. For API mode, enter their paths in the terminal launch command in step 5; for Codex, provide paths or links directly in the client's chat composer.

| Input | API argument | Provide in Codex |
| --- | --- | --- |
| Local video | `--video-file` | Absolute path to the video |
| Video URL | `--video-url` (one per run) | Full HTTPS video URLs; multiple links are accepted, one per line |
| Markdown tutorial | `--tutorial` | Absolute path to the `.md` file |

Multiple links produce separate results: Codex runs each video in its own output subdirectory; with the API, run the command once per link with a different output directory.

Markdown tutorials need numbered operations and their referenced images. Text-only tutorials also require a final reference image.

Choose a new or empty output directory outside the repository: use `--output-dir` in API mode, or specify it in the Codex conversation. Output formats are fixed; main files appear as their corresponding stages complete:

```text
run_001/
  tutorial.md             # Illustrated tutorial
  reproduce.py            # Blender Python code
  asset.blend             # Editable project
  render.png              # Rendered image
  six_views/              # Static multi-view renders, when applicable
  final_effect.mp4        # Animation or turntable video, when applicable
  pipeline_review.json    # Review results
```

### 5. Start with API / Codex

#### API

Open a terminal at the repository root and run this command for first-time setup:

```bash
python run_api.py --configure --config "$HOME/.config/blender-pipeline/pipeline.json"
```

Enter the three API settings at the terminal prompts:

```text
HTTPS Chat Completions endpoint: https://YOUR_PROVIDER/v1/chat/completions
API key (hidden):
Model [gpt-5.6-sol]: YOUR_MODEL_ID
```

Use the model ID supplied by your provider; the model must support image input and tool calls. Press Enter to keep the default. The URL and model are saved as `endpoint` and `model` in `~/.config/blender-pipeline/pipeline.json`; the key is saved separately in `model_api_key` in the same directory. Edit these files later, or add `--model "YOUR_MODEL_ID"` to a launch command to override the saved model for that run.

**Minimal example: Asuka stained-glass window.** The [example directory](examples/asuka-stained-glass) contains the [video link](examples/asuka-stained-glass/video_url.txt) and [input illustration](examples/asuka-stained-glass/input.png). The illustration supplies the window pattern, not a finished-result reference; no starter `.blend` is required.

![Asuka illustration supplied as the window-pattern input](examples/asuka-stained-glass/input.png)

After setup, run this complete command from the repository root. It follows the [video tutorial](https://www.bilibili.com/video/BV18xqdBYEEv/) using the supplied illustration, then generates the Blender project and renders:

```bash
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --video-url "https://www.bilibili.com/video/BV18xqdBYEEv/" \
  --input-asset "$PWD/examples/asuka-stained-glass/input.png" \
  --title "Asuka Stained Glass" \
  --output-dir "$PWD/../blender-results/asuka-api"
```

To use another input, replace the video argument as shown in step 4. Additional input images use `--input-asset /path/to/image.png`.

For a tutorial with a starting project, append `--asset /path/to/starter.blend`; for a text-only tutorial, append `--target-image /path/to/target.png`.

<a id="codex"></a>

#### Codex

Open this repository in Codex, select **Blender Pipeline**, and send the following request (or mention `$blender-pipeline` directly):

```text
$blender-pipeline
Task: Fully reconstruct the Asuka stained-glass window and deliver the Blender project and renders.
Video: https://www.bilibili.com/video/BV18xqdBYEEv/
Pattern input: examples/asuka-stained-glass/input.png (use as the window pattern, not the final reference)
Output: ../blender-results/asuka-codex
```

Paths in this example are relative to the repository root. You can provide multiple video links, one per line, or replace the video with a local file or Markdown tutorial. Include any supporting files in the same message. Codex runs the pipeline; there is no separate Python command to enter.

## Citation

```bibtex
@misc{blenderlore2026,
  title        = {BlenderLore: Learning 3D Coding from Internet Tutorial Videos},
  author       = {BlenderLore Team},
  year         = {2026},
  publisher    = {GitHub},
  journal      = {GitHub repository},
  howpublished = {\url{https://github.com/3D-Coding-Blender/3D-Coding-Blender.github.io}},
}
```

## Acknowledgements

This project builds on the Blender ecosystem, Three.js, and the open-source tools that make browser-based 3D visualization and reproducible graphics possible. Individual asset and tutorial credits will be added alongside the final dataset and paper release.

## License

The repository license is **to be confirmed**. Please check the repository before reusing code, media, models, or tutorial-derived assets.
