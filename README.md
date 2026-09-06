# 3D-Coding-Blender

This repository provides an end-to-end [Blender reconstruction pipeline](#api-start) and a [Codex quick start](#codex-start) for turning tutorial videos or Markdown workflows into illustrated tutorials, editable Blender projects, and rendered results.

[简体中文](<README ZH.md>)

## 1. Install the required tools

Install Python 3.10+, [Blender](https://www.blender.org/download/), and [FFmpeg](https://ffmpeg.org/download.html); make `blender`, `ffmpeg`, and `ffprobe` available on `PATH`. Then install the Python requirements:

```bash
git clone https://github.com/FreedomIntelligence/3D-Coding-Blender.git
cd 3D-Coding-Blender
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

For videos without subtitles, install local speech recognition with `python -m pip install openai-whisper`, or provide a transcript using `--transcript /path/to/subtitles.srt`.

For Codex, install the CLI and sign in following the [official installation guide](https://learn.chatgpt.com/docs/codex/cli). The Codex workflow uses your signed-in account; it does not require a separate API key. Neither launch method requires the RW1 dataset.

## 2. Install the Skill — Codex only

Run from the repository root:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

Select **Blender Pipeline** in Codex, or mention `$blender-pipeline`, then provide your input and desired output. If the Skill does not appear, restart Codex. See the [official Skills documentation](https://learn.chatgpt.com/docs/build-skills) for other installation options.

## 3. Configure CPU or GPU rendering

The pipeline uses Cycles and defaults to CPU. For API runs, select **one** device in the terminal before launching.

CPU:

```bash
export VIDEO2BLENDER_CYCLES_BACKEND=CPU
```

GPU (Apple Silicon example):

```bash
export VIDEO2BLENDER_CYCLES_BACKEND=METAL
```

GPU backends: `METAL` for Apple Silicon, `OPTIX` or `CUDA` for NVIDIA, `HIP` for AMD, and `ONEAPI` for Intel. Choose a backend supported by your Blender installation and hardware.

For Codex, state the device in your request, such as “use CPU” or “use the Metal GPU.” If Blender is not on `PATH`, provide its executable path with API option `--blender /path/to/blender`, or tell the Skill where it is installed.

## 4. Prepare inputs and choose the output

Choose one main input. Use absolute paths and a **new, empty output directory outside the repository**.

| Input | API arguments | Workflow |
| --- | --- | --- |
| Video URL | `--video-url URL` | Extract tutorial → optionally reconstruct in Blender |
| Local video | `--video-file /path/to/tutorial.mp4` | Same workflow, using the local video |
| Video with a starter project | Video argument + `--asset /path/to/starter.blend` | Extract tutorial with supplied inputs → optionally reconstruct from the starter |
| Markdown tutorial | `--tutorial /path/to/tutorial.md` | Prepare the existing tutorial → optionally reconstruct; no video extraction |

Supply the resources the tutorial actually needs:

- `--asset-root /path/to/project`: complete project folder containing `--asset`, including its dependencies.
- `--input-asset /path/to/textures`: additional file or directory; repeat for multiple resources.
- `--preview /path/to/starter.png`: starting-state image; `--target-image /path/to/final.png`: finished-result reference.

Markdown may contain local or base64 images. Keep local images with the document. For full reconstruction from text-only Markdown, supply a finished-result image with `--target-image`. A starting-state preview is not a substitute for that image.

Two video-to-tutorial methods are available: **`visual`** (default, local images; recommended for tutorials no longer than 10 minutes) and **`legacy-rich`** (the original chronological Markdown workflow with base64 images). Select with `--tutorial-method visual` or `--tutorial-method legacy-rich`.

| Requested result | Output |
| --- | --- |
| Tutorial only | `tutorial.md`, associated images and tutorial data; optional `illustrated_tutorial.html` |
| Full reconstruction | Tutorial outputs plus `reproduce.py`, `asset.blend`, `render.png`, `six_views/`, and `pipeline_review.json`; animation results include `final_effect.mp4` when applicable |

Add `--render-html` to produce the readable HTML version. Existing input files are preserved; results are written to `--output-dir`.

## 5. Start with API or Codex

<a id="api-start"></a>
**API**

Create a private configuration once:

```bash
CFG="$HOME/.config/blender-pipeline/pipeline.json"
python run_api.py --configure --config "$CFG"
```

The setup asks exactly two questions:

```text
HTTPS Chat Completions endpoint: https://YOUR_PROVIDER/v1/chat/completions
API key (hidden):
```

The key is stored separately from the configuration and outside the repository. The configured model defaults to `gpt-5.6-sol`.

API runs perform full reconstruction by default, without a goal-selection prompt. Use `--extract-only` for tutorial output only:

```bash
# Video URL → Blender project and renders
python run_api.py --config "$CFG" \
  --video-url "https://www.bilibili.com/video/BVID/" \
  --output-dir /path/to/data/video_run

# Local video + starter project and preview → full reconstruction
python run_api.py --config "$CFG" \
  --video-file /path/to/input/tutorial.mp4 \
  --asset /path/to/input/project/starter.blend \
  --asset-root /path/to/input/project \
  --preview /path/to/input/project/starter.png \
  --output-dir /path/to/data/asset_run

# Existing Markdown + finished-result reference → full reconstruction
python run_api.py --config "$CFG" \
  --tutorial /path/to/input/tutorial.md \
  --target-image /path/to/input/final.png \
  --output-dir /path/to/data/markdown_run

# Video → Markdown and readable HTML only
python run_api.py --config "$CFG" \
  --video-file /path/to/input/tutorial.mp4 \
  --extract-only --render-html \
  --output-dir /path/to/data/tutorial_run
```

<a id="codex-start"></a>
**Codex**

Select **Blender Pipeline** and send your input in natural language; there is no Python command to enter. If the goal is unclear, the Skill first asks:

> Do you want only an illustrated tutorial, or should I continue to reconstruct the Blender result?

For Markdown, the choice is to prepare the existing tutorial or reconstruct its result. An explicit goal is used directly, without asking again. Example requests:

- “Video: `https://www.bilibili.com/video/BVID/`. Extract the tutorial only, including HTML. Save to `/path/to/data/tutorial_run`.”
- “Video: `/path/to/input/tutorial.mp4`; starter project: `/path/to/input/project/starter.blend`; project folder: `/path/to/input/project`; preview: `/path/to/input/project/starter.png`. Reconstruct the result using CPU and save to `/path/to/data/asset_run`.”
- “Tutorial: `/path/to/input/tutorial.md`; finished-result image: `/path/to/input/final.png`. Reconstruct the asset and save to `/path/to/data/markdown_run`.”

The Skill collects any missing input or output location, runs the chosen workflow, and returns links to the resulting files.
