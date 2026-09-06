# 3D-Coding-Blender

本仓库提供端到端的 [Blender 复现 Pipeline](#api-start) 和 [Codex 快速启动](#codex-start)，将教学视频或 Markdown 工作流转为图文教程、可编辑的 Blender 工程及渲染结果。

[English](README.md)

## 第一步：安装必要工具

安装 Python 3.10+、[Blender](https://www.blender.org/download/) 和 [FFmpeg](https://ffmpeg.org/download.html)，将 `blender`、`ffmpeg`、`ffprobe` 加入 `PATH`；然后安装 Python 依赖：

```bash
git clone https://github.com/FreedomIntelligence/3D-Coding-Blender.git
cd 3D-Coding-Blender
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

视频没有字幕时，可通过 `python -m pip install openai-whisper` 安装本地语音识别，或用 `--transcript /path/to/subtitles.srt` 提供转写文本。

使用 Codex 时，CLI 安装与登录方式见[官方文档](https://learn.chatgpt.com/docs/codex/cli)。Codex 工作流使用已登录的账户，无需另配 API key。两种启动方式均不依赖 RW1 数据集。

## 第二步：安装 Skills（仅 Codex）

在仓库根目录执行：

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

在 Codex 中选择 **Blender Pipeline**，或输入 `$blender-pipeline`，再提供输入内容和期望结果。如果没有出现该 Skill，重启 Codex。其他安装方式见 [Skills 官方文档](https://learn.chatgpt.com/docs/build-skills)。

## 第三步：配置渲染（CPU 与 GPU）

Pipeline 使用 Cycles，默认 CPU 渲染。API 启动前，在终端**任选一种**设备配置。

CPU：

```bash
export VIDEO2BLENDER_CYCLES_BACKEND=CPU
```

GPU（Apple Silicon 示例）：

```bash
export VIDEO2BLENDER_CYCLES_BACKEND=METAL
```

GPU 对应后端：Apple Silicon 使用 `METAL`，NVIDIA 使用 `OPTIX` 或 `CUDA`，AMD 使用 `HIP`，Intel 使用 `ONEAPI`。请选择当前 Blender 和硬件支持的后端。

使用 Codex 时，直接在请求中说明“使用 CPU”或“使用 Metal GPU”。如果 Blender 不在 `PATH` 中，API 命令添加 `--blender /path/to/blender`，或向 Skill 提供 Blender 可执行文件的位置。

## 第四步：配置输入与输出

每次选择一种主要输入。文件使用绝对路径，输出指定为**仓库之外的新空目录**。

| 输入类型 | API 参数 | 对应工作流 |
| --- | --- | --- |
| 视频链接 | `--video-url URL` | 提取图文教程 → 按需继续 Blender 复现 |
| 本地视频 | `--video-file /path/to/tutorial.mp4` | 使用本地视频执行相同流程 |
| 视频＋配套工程 | 视频参数＋`--asset /path/to/starter.blend` | 结合配套输入提取教程 → 按需从原工程继续复现 |
| Markdown 图文教程 | `--tutorial /path/to/tutorial.md` | 整理已有教程 → 按需复现，不再进行视频提取 |

按教程实际需要提供配套资源：

- `--asset-root /path/to/project`：包含 `--asset` 及其依赖的完整工程目录。
- `--input-asset /path/to/textures`：附加文件或目录，可重复指定。
- `--preview /path/to/starter.png`：初始状态预览图；`--target-image /path/to/final.png`：最终效果参考图。

Markdown 可包含本地图片或 base64 图片；本地图片需随文档保留。使用纯文字 Markdown 完整复现时，需通过 `--target-image` 提供最终效果图，不能用初始预览图代替。

视频提取保留两种方式：**`visual`**（默认，使用本地图片，建议用于不超过 10 分钟的教学视频）和 **`legacy-rich`**（原有的按时间顺序整理、图片内嵌 base64 的 Markdown 工作流）。分别用 `--tutorial-method visual` 或 `--tutorial-method legacy-rich` 选择。

| 期望结果 | 输出格式 |
| --- | --- |
| 仅图文教程 | `tutorial.md`、配套图片与教程数据；可选 `illustrated_tutorial.html` |
| 完整复现 | 教程文件，以及 `reproduce.py`、`asset.blend`、`render.png`、`six_views/`、`pipeline_review.json`；适用的动画任务另含 `final_effect.mp4` |

添加 `--render-html` 即可生成人类可阅读的 HTML 版。原始输入文件保持不变；结果写入 `--output-dir`。

## 第五步：通过 API 或 Codex 启动

<a id="api-start"></a>
**API 启动**

首次使用时创建私有配置：

```bash
CFG="$HOME/.config/blender-pipeline/pipeline.json"
python run_api.py --configure --config "$CFG"
```

配置过程仅询问以下两项，API key 输入时不显示：

```text
HTTPS Chat Completions endpoint: https://YOUR_PROVIDER/v1/chat/completions
API key (hidden):
```

密钥与配置分开保存在仓库之外。默认配置模型为 `gpt-5.6-sol`。

API 默认执行完整复现，不会再次询问目标；仅需要图文教程时添加 `--extract-only`：

```bash
# 视频链接 → Blender 工程与渲染结果
python run_api.py --config "$CFG" \
  --video-url "https://www.bilibili.com/video/BVID/" \
  --output-dir /path/to/data/video_run

# 本地视频＋原工程＋预览图 → 完整复现
python run_api.py --config "$CFG" \
  --video-file /path/to/input/tutorial.mp4 \
  --asset /path/to/input/project/starter.blend \
  --asset-root /path/to/input/project \
  --preview /path/to/input/project/starter.png \
  --output-dir /path/to/data/asset_run

# 已有 Markdown＋最终效果图 → 完整复现
python run_api.py --config "$CFG" \
  --tutorial /path/to/input/tutorial.md \
  --target-image /path/to/input/final.png \
  --output-dir /path/to/data/markdown_run

# 视频 → 仅 Markdown 与可阅读 HTML
python run_api.py --config "$CFG" \
  --video-file /path/to/input/tutorial.mp4 \
  --extract-only --render-html \
  --output-dir /path/to/data/tutorial_run
```

<a id="codex-start"></a>
**Codex 启动**

选择 **Blender Pipeline** 后，用自然语言提供输入即可，无需手动执行 Python 命令。目标不明确时，Skill 会先询问：

> 你希望只生成图文教程，还是继续复现 Blender 作品？

输入 Markdown 时，对应选择为“仅整理已有教程”或“复现教程结果”。已明确说明目标时，不重复询问。示例：

- “视频：`https://www.bilibili.com/video/BVID/`。只提取图文教程，并生成 HTML。输出到 `/path/to/data/tutorial_run`。”
- “视频：`/path/to/input/tutorial.mp4`；原工程：`/path/to/input/project/starter.blend`；完整工程目录：`/path/to/input/project`；初始预览图：`/path/to/input/project/starter.png`。使用 CPU 完整复现，输出到 `/path/to/data/asset_run`。”
- “图文教程：`/path/to/input/tutorial.md`；最终效果图：`/path/to/input/final.png`。复现作品，输出到 `/path/to/data/markdown_run`。”

Skill 会补齐缺少的输入或输出位置，执行选定流程，并返回结果文件链接。
