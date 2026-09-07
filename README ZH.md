# 3D-Coding-Blender

[English](README.md) | 简体中文

将 Blender 教学视频或图文教程转为可编辑的 Blender 工程与渲染结果。

本仓库提供[端到端 Pipeline](#pipeline)与 [Codex 快速启动](#codex)。

<a id="pipeline"></a>

## 1. 安装必要工具

准备 Python 3.10+、Blender、FFmpeg（含 `ffprobe`），将 Blender 和 FFmpeg 加入 `PATH`。以下命令适用于 macOS/Linux；Windows 使用 WSL 中的 Linux Python 和 Blender。示例路径请替换为实际位置。

```bash
git clone https://github.com/FreedomIntelligence/3D-Coding-Blender.git
cd 3D-Coding-Blender
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

使用 Codex 时，先按[官方文档](https://developers.openai.com/codex/cli/)安装并登录 Codex CLI，确保客户端能调用 `codex`。

## 2. 安装 Skills

在仓库根目录执行，将 Skill 安装到当前用户的 Codex 环境：

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

## 3. 配置渲染

Cycles 默认使用 CPU；Blender 已在 `PATH` 中时可省略路径设置。

```bash
export BLENDER_PIPELINE_BLENDER=/path/to/blender
export VIDEO2BLENDER_CYCLES_BACKEND=CPU
```

启用 GPU 时，将 `CPU` 改为对应后端：NVIDIA 用 `OPTIX` 或 `CUDA`，Apple Silicon 用 `METAL`，AMD 用 `HIP`，Intel 用 `ONEAPI`。

## 4. 配置输入与输出

输入文件保存在本机。API 模式在第 5 步的终端启动命令中填写路径；Codex 模式直接在客户端对话框中提供路径或链接。

| 输入 | API 参数 | Codex 对话中提供 |
| --- | --- | --- |
| 本地视频 | `--video-file` | 视频文件的绝对路径 |
| 视频链接 | `--video-url`（每次一个） | 完整 HTTPS 视频链接，可放多个链接，每行一个 |
| Markdown 教程 | `--tutorial` | `.md` 文件的绝对路径 |

多个链接分别生成结果：Codex 会逐个运行，保存到不同输出子目录；API 按每个链接执行一次命令，并使用不同输出目录。

Markdown 教程需包含编号操作和引用的图片；纯文字教程还需提供最终参考图。

输出位置须为仓库外的新目录或空目录：API 使用 `--output-dir`，Codex 在对话中说明。输出格式固定，主要文件如下（对应阶段完成后生成）：

```text
run_001/
  tutorial.md             # 图文教程
  reproduce.py            # Blender Python 代码
  asset.blend             # 可编辑工程
  render.png              # 渲染图
  six_views/              # 静态多视图（适用时）
  final_effect.mp4        # 动画或转台视频（适用时）
  pipeline_review.json    # 审核结果
```

## 5. 启动 API / Codex

### API

在仓库根目录打开终端，首次运行配置命令：

```bash
python run_api.py --configure --config "$HOME/.config/blender-pipeline/pipeline.json"
```

按终端提示填写 API 的三项配置：

```text
HTTPS Chat Completions endpoint: https://YOUR_PROVIDER/v1/chat/completions
API key (hidden):
Model [gpt-5.6-sol]: YOUR_MODEL_ID
```

Model 填写服务商提供的模型 ID，模型需支持图像输入和工具调用；直接回车保留默认值。地址和模型分别保存在 `~/.config/blender-pipeline/pipeline.json` 的 `endpoint`、`model` 字段，密钥单独保存在同目录的 `model_api_key`。后续可编辑这两个文件，或在启动命令中追加 `--model "YOUR_MODEL_ID"`，仅覆盖本次运行的模型。

**最小示例：明日香玻璃窗花。** [示例目录](examples/asuka-stained-glass)包含[视频链接](examples/asuka-stained-glass/video_url.txt)和[输入插画](examples/asuka-stained-glass/input.png)。插画用于制作窗花图案，不是最终效果参考图；本例不需要配套 `.blend` 工程。

![作为窗花图案输入的明日香原始插画](examples/asuka-stained-glass/input.png)

完成配置后，在仓库根目录运行以下完整命令，结合[视频教程](https://www.bilibili.com/video/BV18xqdBYEEv/)和输入插画，生成 Blender 工程及渲染结果：

```bash
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --video-url "https://www.bilibili.com/video/BV18xqdBYEEv/" \
  --input-asset "$PWD/examples/asuka-stained-glass/input.png" \
  --title "Asuka Stained Glass" \
  --output-dir "$PWD/../blender-results/asuka-api"
```

使用其他输入时，按第 4 步替换视频参数；附加输入图片使用 `--input-asset /path/to/image.png`。

教程有配套工程时，在命令后追加 `--asset /path/to/starter.blend`；纯文字教程追加 `--target-image /path/to/target.png`。

<a id="codex"></a>

### Codex

在 Codex 中打开本仓库目录，选择 **Blender Pipeline**，发送下面这段完整请求（也可直接输入 `$blender-pipeline` 引用 Skill）：

```text
$blender-pipeline
任务：完整复现明日香玻璃窗花，交付 Blender 工程和渲染结果。
视频：https://www.bilibili.com/video/BV18xqdBYEEv/
图案输入：examples/asuka-stained-glass/input.png（用作窗花图案，不是最终效果图）
输出：../blender-results/asuka-codex
```

示例中的路径相对于仓库根目录。视频位置可以放多个链接，每行一个，也可换成本地视频或 Markdown 教程路径；配套文件放在同一条消息中。Codex 会执行 pipeline，无需另行输入 Python 启动命令。

## 知识库结构

知识以 Markdown 保存，包含说明文本和可选代码片段，切分后建立带来源信息的 JSONL 索引。格式示意：

````markdown
## 创建立方体
添加边长为 2 的立方体。

```python
import bpy
bpy.ops.mesh.primitive_cube_add(size=2)
```
````

启动时从内置知识构建本地索引，修改知识文档后可重建更新。成功经验可作为候选补充，经[审阅](skills/blender-pipeline/knowledge/operations-and-knowledge.md)后按来源合并，跳过未变化的记录并更新或新增条目。
