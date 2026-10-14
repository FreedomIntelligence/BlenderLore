<a href="README.md">English</a> | 简体中文

<h2 align="center">BlenderLore：从互联网教程视频中学习 3D 编程</h2>

<p align="center">
  <strong>如果你喜欢我们的项目，欢迎在 GitHub 上为我们点亮一颗 ⭐ 以获取最新更新。
</strong>
</p>

<p align="center">
  <a href="https://blenderlore.github.io/"><img src="https://img.shields.io/badge/Project-Website-7b61ff" alt="Project Website"></a>
  <a href="https://github.com/FreedomIntelligence/BlenderLore"><img src="https://img.shields.io/badge/GitHub-Repository-111111.svg?logo=github" alt="GitHub Repository"></a>
  <a href="https://huggingface.co/datasets/FreedomIntelligence/BlenderLore"><img src="https://img.shields.io/badge/HuggingFace-Dataset-f6c344.svg?logo=huggingface" alt="Hugging Face Dataset"></a>
  <img src="https://img.shields.io/badge/Paper-Coming_Soon-8f7ad8" alt="Paper Coming Soon">
</p>


## 🔭 概览

BlenderLore 是一个从互联网教程视频中获取并复用程序化知识的 3D 编程数据集与流水线。它将视频证据转换为图文教程，通过 Blender Python 重建可编辑资产，并提取经过审查的方法，用于后续创作任务。该集合包含**约 22K 个项目实例，关联来自 YouTube 和 Bilibili 的超过 3K 个教程视频**。

**泛化能力是 BlenderLore 的核心**。执行代码并检查几何、外观和运动，使推断出的方法得到具体实现的支撑。智能体可以检索、迁移和组合不同教程中的方法，实现新的创作目标。**BlenderLore Skill** 将重建工作流与程序化知识库提供给现有编程智能体，在不更新模型参数的情况下积累外部知识。

![BlenderLore 动态毛发完整样例：从视频证据、图文步骤到 Blender Python 与动画资产](assets/paper/blenderlore_sample.png)

*动态毛发样例：图文步骤将原始视频、可执行重建与可复用的程序化知识联系起来。*

## 🧩 方法

![BlenderLore 流程：教程视频、多模态证据、图文工作流与知识检索、编程智能体循环及验证后的产物](assets/paper/blenderlore_method.png)

1. **恢复图文工作流。** 将视频帧、旁白、屏幕文字与界面变化对齐为有序步骤，记录输入、操作、参数与中间结果。
2. **用源资产支撑方法。** 利用创作者提供的项目，修复缺失组件，将教程步骤关联到具体实现。
3. **重建与检查。** 检索相关知识，生成 Blender Python，执行并渲染场景，修复几何、外观或运动上的偏差。
4. **审查并保留知识。** 记录方法来源、检索关键词、适用范围、实现片段与结果检查项，由人工专家检查最终资产。
5. **迁移与组合。** 跨教程复用方法，创造新资产并扩展程序化知识库。

## 超越教程的泛化

![跨教程组合：Duck GiGi 建模、迁移的绑定与行走方法，以及虹彩材质](assets/paper/blenderlore_composition.png)

BlenderLore 将三个教程中的方法组合成会行走的虹彩 Duck GiGi：以静态角色建模教程构建形体，迁移小鳄鱼教程中的绑定与行走方法，再应用独立教程中的虹彩材质。复用的单位是构建方法，使智能体能够超越原始演示资产，迁移并组合知识。

## 🎁 你将获得什么

<table>
<tr>
<td width="33%" valign="top">

### 01 · 端到端的智能体流水线

每次运行都会重现一个教程工作流，并产出可编辑的 Blender 项目、复现脚本、渲染结果与验证结果。

- 可编辑资产 — `asset.blend`
- 复现脚本 — `reproduce.py`
- 材质与节点图
- 最终渲染图
- 多视角渲染图
- 动画
- 智能体日志

</td>
<td width="33%" valign="top">

### 02 · 高质量的 3D 数据集

通过采集、修复、重建以及跨教程迁移与组合，构建约 22K 个项目实例，关联超过 3K 个教程视频。人工专家逐一检查最终资产文件。

</td>
<td width="33%" valign="top">

### 03 · 可复用的程序化知识库

经过审查的知识条目记录来源、适用范围、实现与检查项。BlenderLore Skill 提供知识库与工作流指引，使编程智能体能够检索、迁移和组合方法，完成新的 3D 任务。

</td>
</tr>
</table>


## 🚀 快速开始

### 1. 安装前置依赖

安装 Python 3.10+、Blender 和 FFmpeg（包括 `ffprobe`），并将 Blender 和 FFmpeg 加入 `PATH`。以下命令面向 macOS/Linux；在 Windows 上，请在 WSL 中使用 Linux 版的 Python 和 Blender。请将示例路径替换为你自己的路径。

```bash
git clone https://github.com/FreedomIntelligence/BlenderLore.git
cd BlenderLore
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

若使用 Codex，请参考[官方文档](https://developers.openai.com/codex/cli/)安装并登录 Codex CLI，并确保客户端可以运行 `codex`。

### 2. 安装 Skills

在仓库根目录运行以下命令，将该 Skill 安装到当前用户的 Codex 环境中：

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

### 3. 配置渲染

Cycles 默认使用 CPU 渲染。如果 Blender 已经在 `PATH` 中，可以省略 Blender 路径的设置。

```bash
export BLENDER_PIPELINE_BLENDER=/path/to/blender
export VIDEO2BLENDER_CYCLES_BACKEND=CPU
```

如需 GPU 渲染，请将 `CPU` 替换为对应的后端：NVIDIA 使用 `OPTIX` 或 `CUDA`，Apple Silicon 使用 `METAL`，AMD 使用 `HIP`，Intel 使用 `ONEAPI`。

### 4. 配置输入与输出

请将输入文件保存在本机。使用 API 模式时，在第 5 步的终端启动命令中填写其路径；使用 Codex 时，可直接在客户端聊天输入框中提供路径或链接。

| 输入 | API 参数 | 在 Codex 中提供 |
| --- | --- | --- |
| 本地视频 | `--video-file` | 视频的绝对路径 |
| 视频链接 | `--video-url` | 完整的 HTTPS 视频链接 |
| Markdown 教程 | `--tutorial` | `.md` 文件的绝对路径 |

Markdown 教程需要包含编号的操作步骤及其引用的图片。纯文本教程还需要提供一张最终参考图。

请在仓库之外选择一个新建或空的输出目录：API 模式下使用 `--output-dir`，或在 Codex 对话中指定。输出格式是固定的；主要文件会在对应阶段完成后依次出现：

```text
run_001/
  tutorial.md             # 图文教程
  reproduce.py             # Blender Python 代码
  asset.blend              # 可编辑项目
  render.png               # 渲染图像
  six_views/               # 静态多视角渲染图（如适用）
  final_effect.mp4         # 动画或转台视频（如适用）
  pipeline_review.json     # 审查结果
```

### 5. 通过 API / Codex 启动

#### API

在仓库根目录打开终端，运行以下命令进行首次配置：

```bash
python run_api.py --configure --config "$HOME/.config/blender-pipeline/pipeline.json"
```

在终端提示中，输入以 `/chat/completions` 结尾的 HTTPS API 端点地址以及你的 API 密钥。端点会保存在 `~/.config/blender-pipeline/pipeline.json` 的 `endpoint` 字段中；密钥会保存在同一目录下的 `model_api_key` 字段中。之后可通过编辑这些文件来修改配置。

在同一终端中启动程序，将输入路径和输出目录替换为你自己的路径。其他输入类型请使用第 4 步中对应的参数：

```bash
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --video-file /path/to/input/tutorial.mp4 --output-dir /path/to/data/run_api
```

对于带有初始项目的教程，请追加 `--asset /path/to/starter.blend`；对于纯文本教程，请追加 `--target-image /path/to/target.png`。

#### Codex

在聊天输入框中输入 `/blender-pipeline`：

```text
/blender-pipeline
Input: /path/to/input/tutorial.mp4
Output: /path/to/data/run_skill
```

你可以将输入替换为完整的视频链接或 Markdown 教程路径。请在同一条消息中附上任何配套项目或参考图片的路径。

## 📖 引用

```bibtex
@misc{blenderlore2026,
  title        = {BlenderLore: Learning 3D Coding from Internet Tutorial Videos},
  author       = {FreedomAI Team},
  year         = {2026},
  publisher    = {GitHub},
  journal      = {GitHub repository},
  howpublished = {\url{https://github.com/FreedomIntelligence/BlenderLore}},
}
```

## 🙏 致谢

本项目基于 [Blender](https://www.blender.org/)（免费开源的 3D 创作套件）构建。

## 📄 许可证

本项目基于 Apache 2.0 许可证发布。

## 📬 联系方式

如有问题或反馈，请通过以下邮箱联系我们：<a href="mailto:minghaowu@link.cuhk.edu.cn">minghaowu@link.cuhk.edu.cn</a>
