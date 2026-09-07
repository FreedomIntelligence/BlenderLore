# 3D-Coding-Blender

English | [简体中文](README%20ZH.md)

Turn Blender tutorial videos or illustrated guides into editable Blender projects and renders.

This repository provides an [end-to-end Pipeline](#pipeline) and a [Codex quick start](#codex).

<a id="pipeline"></a>

## 1. Install prerequisites

Install Python 3.10+, Blender, and FFmpeg (including `ffprobe`), with Blender and FFmpeg on `PATH`. Commands below target macOS/Linux; on Windows, use Linux Python and Blender in WSL. Replace example paths with your own.

```bash
git clone https://github.com/FreedomIntelligence/3D-Coding-Blender.git
cd 3D-Coding-Blender
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

For Codex, install and sign in to Codex CLI using the [official documentation](https://developers.openai.com/codex/cli/), and ensure the client can run `codex`.

## 2. Install Skills

Run from the repository root to install the Skill into the current user's Codex environment:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

## 3. Configure rendering

Cycles uses the CPU by default. Omit the Blender path setting if Blender is already on `PATH`.

```bash
export BLENDER_PIPELINE_BLENDER=/path/to/blender
export VIDEO2BLENDER_CYCLES_BACKEND=CPU
```

For GPU rendering, replace `CPU` with the appropriate backend: `OPTIX` or `CUDA` for NVIDIA, `METAL` for Apple Silicon, `HIP` for AMD, or `ONEAPI` for Intel.

## 4. Configure inputs and outputs

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

## 5. Start with API / Codex

### API

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

### Codex

Open this repository in Codex, select **Blender Pipeline**, and send the following request (or mention `$blender-pipeline` directly):

```text
$blender-pipeline
Task: Fully reconstruct the Asuka stained-glass window and deliver the Blender project and renders.
Video: https://www.bilibili.com/video/BV18xqdBYEEv/
Pattern input: examples/asuka-stained-glass/input.png (use as the window pattern, not the final reference)
Output: ../blender-results/asuka-codex
```

Paths in this example are relative to the repository root. You can provide multiple video links, one per line, or replace the video with a local file or Markdown tutorial. Include any supporting files in the same message. Codex runs the pipeline; there is no separate Python command to enter.

## Knowledge structure

Knowledge is stored as Markdown with explanatory text and optional code snippets, then split into a JSONL index with source metadata. Example format:

````markdown
## Create a cube
Add a cube with an edge length of 2.

```python
import bpy
bpy.ops.mesh.primitive_cube_add(size=2)
```
````

Each run builds a local index from the bundled knowledge, which can be rebuilt after editing the knowledge documents. Successful experience can be collected as candidates and merged by source identity after [review](skills/blender-pipeline/knowledge/operations-and-knowledge.md), skipping unchanged records and updating or adding entries.
