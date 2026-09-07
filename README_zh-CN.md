# BlenderLore

<p align="center">
  <strong>从互联网教程视频中学习程序化 3D 生成</strong>
</p>

<p align="center">
  <a href="README.md">English</a> | 简体中文
</p>

<p align="center">
  <a href="https://3d-coding-blender.github.io/"><img src="https://img.shields.io/badge/项目-展示网页-7b61ff" alt="项目展示网页"></a>
  <a href="https://github.com/3D-Coding-Blender/3D-Coding-Blender.github.io"><img src="https://img.shields.io/badge/GitHub-展示仓库-111111.svg?logo=github" alt="GitHub 展示仓库"></a>
  <a href="https://huggingface.co/"><img src="https://img.shields.io/badge/HuggingFace-数据集-f6c344.svg?logo=huggingface" alt="Hugging Face 数据集"></a>
  <img src="https://img.shields.io/badge/论文-即将发布-8f7ad8" alt="论文即将发布">
</p>

> **状态：** 当前为研究原型，论文与数据集即将发布。

## 项目简介

互联网 Blender 教程包含丰富的真实创作知识，但智能体难以直接使用这些知识。重要指令可能出现在旁白、屏幕字幕、变化的界面状态或短暂的节点图操作中。

BlenderLore 将教程视频转换为带时间戳的多模态证据，重建示范工作流，并生成可执行的 Blender Python。每次运行都会交付可编辑的 Blender 工程、复现脚本、渲染结果和验证结果。

泛化能力是 BlenderLore 的核心。成功的复现结果会被保留为候选程序化知识。面对未见过的生成或编辑任务时，智能体先将目标分解为可复用的构建模式，检索相关程序化知识，并将其重组为面向具体任务的 Blender 代码。

## 工作原理

![BlenderLore 流程概览](https://3d-coding-blender.github.io/assets/method-pipeline.png)

1. **收集教程**——选择高质量 Blender 教程，确定目标资产或支持的动作。
2. **恢复证据**——对齐视觉关键帧、OCR、旁白、时间戳、界面操作和 Blender 版本线索。
3. **定义工作流**——将证据转换为有序操作，并检索相关程序化知识。
4. **编码、运行与修复**——生成 Blender Python，在 Blender 中执行并渲染场景，比较结果并修复失败。
5. **验证与保留**——打包可编辑资产与视觉证据，并将经过验证的模式保留用于后续任务。

## 你将得到

### 01 · 端到端智能体流程

每次运行都会重建教程工作流，并交付可编辑的 Blender 工程、复现脚本、渲染结果和验证结果。

- 可编辑资产——`asset.blend`
- 复现脚本——`reproduce.py`
- 材质与节点图
- 渲染结果
- 多视角效果图
- 效果动画
- Agent 轨迹

### 02 · 高质量 3D 数据集

通过收集、修复与复现，我们构建了一个包含 23K 个程序化 3D 资产的高质量数据集。

### 03 · 可复用的程序化知识库

成功的工作流会被保留为可复用的程序化知识。面对未见过的目标时，智能体会分解任务、检索相关模式，并将其重组为面向具体任务的 Blender 代码。

## 快速开始

### 1. 安装必要工具

准备 Python 3.10+、Blender、FFmpeg（含 `ffprobe`），将 Blender 和 FFmpeg 加入 `PATH`。以下命令适用于 macOS/Linux；Windows 使用 WSL 中的 Linux Python 和 Blender。示例路径请替换为实际位置。

```bash
git clone https://github.com/FreedomIntelligence/BlenderLore.git
cd BlenderLore
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

使用 Codex 时，先按[官方文档](https://developers.openai.com/codex/cli/)安装并登录 Codex CLI，确保客户端能调用 `codex`。

### 2. 安装 Skills

在仓库根目录执行，将 Skill 安装到当前用户的 Codex 环境：

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

### 3. 配置渲染

Cycles 默认使用 CPU；Blender 已在 `PATH` 中时可省略路径设置。

```bash
export BLENDER_PIPELINE_BLENDER=/path/to/blender
export VIDEO2BLENDER_CYCLES_BACKEND=CPU
```

启用 GPU 时，将 `CPU` 改为对应后端：NVIDIA 用 `OPTIX` 或 `CUDA`，Apple Silicon 用 `METAL`，AMD 用 `HIP`，Intel 用 `ONEAPI`。

### 4. 配置输入与输出

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

### 5. 启动 API / Codex

#### API

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

#### Codex

在 Codex 中打开本仓库目录，选择 **Blender Pipeline**，发送下面这段完整请求（也可直接输入 `$blender-pipeline` 引用 Skill）：

```text
$blender-pipeline
任务：完整复现明日香玻璃窗花，交付 Blender 工程和渲染结果。
视频：https://www.bilibili.com/video/BV18xqdBYEEv/
图案输入：examples/asuka-stained-glass/input.png（用作窗花图案，不是最终效果图）
输出：../blender-results/asuka-codex
```

示例中的路径相对于仓库根目录。视频位置可以放多个链接，每行一个，也可换成本地视频或 Markdown 教程路径；配套文件放在同一条消息中。Codex 会执行 pipeline，无需另行输入 Python 启动命令。

## 引用

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

## 致谢

本项目基于 Blender 生态、Three.js 以及支持浏览器端 3D 可视化与可复现图形工作的开源工具构建。各项资产和教程的署名将在最终数据集与论文发布时一并补充。

## 许可证

仓库许可证**尚待确认**。在复用代码、媒体、模型或教程衍生资产之前，请先检查仓库中的最新许可信息。
