# 3D-Coding-Blender

[English](README.md) | 简体中文

从教学视频或已有图文教程中提取操作流程，结合可维护的 Blender 知识库，生成并执行 Blender Python，输出可编辑工程与渲染结果。

本仓库提供两种根目录启动方式：

| 入口 | 模型调用方式 | 适用情况 |
| --- | --- | --- |
| [`run_api.py`](run_api.py) | 用户配置的 HTTPS Chat Completions API | 使用自己的 API 服务与计费账户 |
| [`run_codex.py`](run_codex.py) | 已登录的 Codex CLI | 使用 Codex 启动完整流程，不需要另填 API 密钥 |

两个入口调用同一条复现流程，不是两套建模实现。**普通视频/教程复现不需要 RW1 数据集、私有资产库、远程工作节点或展示页 recipe。** 所需输入由用户明确提供。仅“代码生成成功”或“写出了 `.blend`”不等于复现质量合格。

**导航：** [快速开始](#快速开始) · [输入示例](#3-不同输入情形) · [提取方法](#4-两种视频转教程方法) · [结果](#5-实际执行链与结果含义) · [知识库](#6-知识库存储切分使用和新增) · [参数](#8-完整参数参考) · [维护与限制](#10-开发检查与已知限制)

## 快速开始

先准备 Python 3.10+、Blender、FFmpeg；选择 API 或已登录的 Codex CLI。以下在 macOS/Linux 终端运行，路径均需替换为自己的文件位置：

```bash
git clone https://github.com/FreedomIntelligence/3D-Coding-Blender.git
cd 3D-Coding-Blender
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

**API 用户：** 首次配置后启动。

```bash
python run_api.py --configure --config "$HOME/.config/blender-pipeline/pipeline.json"
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --video-file /path/to/input/tutorial.mp4 --output-dir /path/to/data/run_001
```

**Codex 用户：** 安装 [Codex CLI](https://developers.openai.com/codex/cli/) 并登录后启动。

```bash
codex login
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --output-dir /path/to/data/run_002
```

如果 Blender 不在 `PATH` 中，附加 `--blender /path/to/blender`。每次运行使用新的空输出目录；输出必须位于仓库之外。默认继续执行 Blender 复现，仅提取教程时加 `--extract-only`。完整运行后首先查看 `pipeline_review.json`，不要仅凭模型回复判断是否完成。

## 1. 安装与运行环境

当前运行目标为 macOS、Linux；Windows 请通过 WSL 使用 Linux Python 与 Linux Blender。原生 Windows 的凭据权限及完整 Blender 子进程链尚未验证，不列为当前已支持环境。

- Python 3.10 或更新版本。
- Blender：安装独立应用，推荐与教程的功能/API 相容的版本；通过 `--blender` 指定可执行文件。`bpy` 由 Blender 自带，不要用 pip 安装。
- FFmpeg 和 ffprobe：抽帧与动画编码需要，加入 `PATH`。
- 视频链接下载使用 yt-dlp；登录、付费、下架、受地区限制的视频不保证可自动获取，可改用有权使用的本地视频。
- Codex 模式需要安装 Codex CLI 并先完成 `codex login`。调用使用非交互 `codex exec`，代码生成与视觉复核也走 Codex，不会暗中改用 API。CLI 参数依据 [Codex 命令行文档](https://developers.openai.com/codex/cli/reference/)。

根目录入口显式采用本地渲染设备策略，不要求 NVIDIA UUID 或集群占用证明。Cycles 默认使用 CPU；需要加速时可显式设置 `VIDEO2BLENDER_CYCLES_BACKEND=METAL`（Apple Silicon）或相应 `CUDA/OPTIX/HIP/ONEAPI` 后端，实际不可用则报错，不伪装成已使用 GPU。设备记录会明确区分本地配置与集群精确进程证明，原集群严格策略不被改为宽松默认。

安装完成后可查看完整参数：

```bash
python run_api.py --help
python run_codex.py --help
```

默认知识检索是本地 manifest + 词法检索，不下载嵌入模型，也不要求 Qdrant。仅在主动使用向量检索时额外安装：

```bash
python -m pip install -r skills/blender-pipeline/generation/requirements-knowledge.txt
```

视频有平台字幕时优先使用字幕；也可传入 `--transcript`。本地语音识别依次尝试已安装的 MLX Whisper、OpenAI Whisper、faster-whisper。需要时自行安装其中一种，例如 `python -m pip install openai-whisper`；首次运行可能下载模型。OCR 可选安装 Tesseract。缺少字幕/ASR 时会记录警告，不能把未听清、未看清的参数当成已证实事实。

## 2. 一次配置，一条命令启动

下面的 `/path/to/data`、`/path/to/input` 和 `/path/to/blender` 都是占位路径，需换成实际位置。结果、抽帧和缓存写入用户选择的数据盘；不要把输出目录放在 Git 仓库内。

### API 模式

第一次运行，交互配置 endpoint 和密钥：

```bash
python run_api.py --configure --config "$HOME/.config/blender-pipeline/pipeline.json"
```

输入的 endpoint 必须是无凭据、无查询参数的 HTTPS 地址，以 `/chat/completions` 结尾。密钥输入不回显，单独保存为仓库外的 `model_api_key`，POSIX 权限为 `0600`。配置文件只保存密钥文件路径，不保存密钥值。已有配置可参考 [`pipeline.config.example.json`](pipeline.config.example.json)。不要把真实配置、密钥、cookie 或代理凭据提交到 Git。

启动：

```bash
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" --video-file /path/to/input/tutorial.mp4 --output-dir /path/to/data/run_001
```

不使用配置文件也可以：

```bash
python run_api.py --endpoint https://YOUR_PROVIDER/v1/chat/completions --api-key-file /private/path/model_api_key --video-file /path/to/input/tutorial.mp4 --output-dir /path/to/data/run_002
```

对应环境变量为 `BLENDER_PIPELINE_API_ENDPOINT`、`BLENDER_PIPELINE_API_KEY_FILE` 和 `BLENDER_PIPELINE_BLENDER`。优先级：命令行参数 > 配置文件 > 环境变量。模型默认严格指定 `gpt-5.6-sol`；仅在该模型确实不可调用时，由用户显式选择 `--model gpt-5.5 --fallback-reason "实际不可用原因"`。不使用 GPT-5.4，不做静默降级。第三方服务对模型名称与实际后端的映射由服务商负责，本仓库不能替其证明底层模型身份。

### Codex 模式

```bash
codex login
python run_codex.py --video-file /path/to/input/tutorial.mp4 --output-dir /path/to/data/run_003
```

不需要 API 配置或 API 密钥。Codex 会依次承担所选的视频理解、Blender 代码生成与视觉复核；Blender 执行仍在本机完成。模型遵循相同的显式选择规则。Codex 调用默认只读模型工作区；生成的代码由后续受控 Blender 包装器执行。登录账户的额度仍会被消耗。

如果还希望在 Codex 对话中直接调用此 Skill，可在仓库根目录执行以下可选安装：

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/blender-pipeline" "$HOME/.agents/skills/blender-pipeline"
```

然后在 Codex 对话中用 `$blender-pipeline` 并给出输入、输出路径。不要覆盖已有安装，并保留符号链接指向的仓库位置。Codex 支持符号链接形式的用户 Skill；未显示时重启 Codex。详见[本地 Skill 发现规则](https://developers.openai.com/codex/skills/)。直接运行 `run_codex.py` **不需要**这一步。

### 运行前检查

```bash
# 只打印计划：不写文件、不下载、不读密钥、不调用模型。
python run_codex.py --video-url https://www.bilibili.com/video/YOUR_BVID/ --output-dir /path/to/data/planned_run --dry-run

# 每项各做一次小型真实模型请求，不等同于完成资产复现。
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" --check --output-dir /path/to/data/api_check
python run_codex.py --check --output-dir /path/to/data/codex_check
```

每次正式启动必须使用新的、空的输出目录，避免覆盖上一版结果。`--check` 也使用独立目录。

## 3. 不同输入情形

以下使用 Codex 举例；把 `run_codex.py` 换成 `run_api.py --config /private/path/pipeline.json` 即为 API 方式。

| 情形 | 必需输入 | 相关选项 |
| --- | --- | --- |
| 从零建模教学视频 | 视频文件或视频链接，二选一 | `--video-file` / `--video-url` |
| 在现成模型上演示材质、绑定或动画 | 视频 + 起始工程/模型 | `--asset`，必要时 `--asset-root` |
| 视频引用外部贴图、图片、资源 | 视频 + 实际资源文件 | 重复 `--input-asset` |
| 起始资产有预览图 | 起始模型 + 起始预览 | `--preview`，仅说明输入状态 |
| 有明确的最终目标图片 | 教程 + 最终参考图 | `--target-image`，不是输入资产预览 |
| 已有图文教程 | `.md` + 其引用的图片；纯文字教程需另给最终目标图 | `--tutorial`，纯文字完整复现加 `--target-image` |
| 仅需要提取教程 | 视频及必要配套资源 | `--extract-only`，可加 `--render-html` |

`--asset` 支持 `.blend`、`.glb`、`.gltf`、`.obj`、`.fbx`；格式支持不等于所有插件、修改器和外部依赖均可无损迁移。

### 仅视频文件或链接

```bash
python run_codex.py --video-file /path/to/input/tutorial.mp4 --title "教程标题" --output-dir /path/to/data/video_file_run
python run_codex.py --video-url https://www.bilibili.com/video/YOUR_BVID/ --title "教程标题" --output-dir /path/to/data/video_url_run
```

视频必须包含完成任务需要的实际操作。从中间章节开始、缺少起始模型/贴图、只展示结果或依赖未提供插件的视频，不能靠启动器补齐为可靠的从零教程。模型无法凭一个文件名读取用户未提供的资产。

### 视频 + 配套 `.blend` + 预览图

例如“炫彩鸭”应使用原版鸭子的工程作为起点，而不是把它当成最终炫彩成品：

```bash
python run_codex.py --video-file /path/to/input/chromatic_duck.mp4 --asset /path/to/input/original_duck.blend --preview /path/to/input/original_duck.png --output-dir /path/to/data/chromatic_duck_run
```

流程复制并检查源文件，由可信包装器在执行生成代码前打开它；模型会收到对象、材质、图片依赖等结构信息与预览图。输出检查要求源对象被实际使用，而不只是读取文件后重新生成无关模型。原文件不被覆盖。

若工程有外部贴图或链接文件，应提供完整且有权使用的资源包：

```text
duck_bundle/
  original_duck.blend
  textures/
    body.png
  libraries/
    parts.blend
```

```bash
python run_codex.py --video-file /path/to/input/chromatic_duck.mp4 --asset /path/to/input/duck_bundle/original_duck.blend --asset-root /path/to/input/duck_bundle --preview /path/to/input/original_duck.png --output-dir /path/to/data/duck_bundle_run
```

`--asset-root` 会复制整个指定目录并保留相对结构；请只指向本次确需的资源包，不要指向整块数据盘。建议源 `.blend` 使用相对路径或已经打包资源。缺失或仍指向未提供机器路径的依赖会阻止执行，不会自动替换成随机贴图或材质。自定义插件、外部缓存与特殊节点仍需相容运行环境；接口不保证任意 Blender 工程都能跨版本加载。

### 视频 + 单独贴图或其他辅助文件

```bash
python run_codex.py --video-file /path/to/input/card.mp4 --input-asset /path/to/input/card_front.png --input-asset /path/to/input/textures --output-dir /path/to/data/card_run
```

辅助文件会进入教程输入包。支持的图片由可信包装器校验、加载并打包到 Blender，生成代码通过现有数据块使用它们。PDF、说明文件等可以作为资源保留，但不会仅凭扩展名自动转成可执行建模操作。额外 3D 工程不是任意批量导入接口：主要起始模型用 `--asset`，依赖模型放在 `--asset-root` 并由源工程引用。

有独立的最终参考图时，使用 `--target-image`；不要用它替代起始状态预览：

```bash
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --asset /path/to/input/starter.glb --preview /path/to/input/before.png \
  --target-image /path/to/input/expected.png --output-dir /path/to/data/reference_run
```

### 已有图文教程

```bash
python run_codex.py --tutorial /path/to/input/tutorial.md --output-dir /path/to/data/markdown_run
python run_codex.py --tutorial /path/to/input/tutorial.md --asset /path/to/input/starter.blend --preview /path/to/input/starter.png --render-html --output-dir /path/to/data/markdown_asset_run
```

完整复现需要教程中的图片或独立最终目标图，以便进行视觉复核。**纯文字 Markdown 必须同时提供 `--target-image`**；否则在模型调用前停止。`--preview` 仅表示起始状态，不能替代最终效果依据。只整理/转换纯文字教程时，可以使用 `--extract-only`，不需要目标图：

```bash
python run_codex.py --tutorial /path/to/input/text_only.md \
  --target-image /path/to/input/expected.png --output-dir /path/to/data/text_replay
python run_codex.py --tutorial /path/to/input/text_only.md \
  --extract-only --render-html --output-dir /path/to/data/text_tutorial_only
```

支持带顺序操作标题的 Markdown，例如 `## 1. 创建网格` 或 `## 步骤 1：创建网格`，也支持顶层有序操作列表及 legacy-rich 原有窗口内的操作列表。图片放在该 Markdown 所在目录或其子目录，支持引用式图片和 base64 内嵌图片。远程图片需用户先取得使用授权并保存到本地。适配器保持原文，不要求用户手写 JSON，也不让模型重写现有教程。为兼容下游生成 `steps_verified.json`，其中明确标注“用户提供的操作，未经独立视频验证”，不因文件名就宣称完成了视频核验。

此入口不直接解析 PDF、DOCX 或任意 HTML；应先转换为符合上述结构的 Markdown。只有图片、只有资产、没有教程的自由生成/自由编辑不属于这两个视频复现入口，应使用下方的高级模式。

## 4. 两种视频转教程方法

两种方法都保留在同一个接口中，API/Codex 均可选择：

| 选项 | 实现与输出 | 选择建议 |
| --- | --- | --- |
| `--tutorial-method visual`（默认） | 嵌入的 `video-to-visual-tutorial` skill：总览画面 → 重点回采 → 证据台账 → 连贯分步教程；另有独立评分 rubric | **建议用于不超过 10 分钟的教学视频**，优先面向完整、可读的图文操作流程 |
| `--tutorial-method legacy-rich` | 原生产 rich 流程：5 秒画面采样、完整 60 秒窗口理解、顺序合并，输出 base64 内嵌图片的 `tutorial.md` 与普通图片版路径文档 | 保留原端到端提取方式和已有下游兼容性；不是把新版 Markdown 简单转成 base64 |

```bash
python run_codex.py --video-file /path/to/input/tutorial.mp4 --tutorial-method visual --render-html --output-dir /path/to/data/visual_run
python run_codex.py --video-file /path/to/input/tutorial.mp4 --tutorial-method legacy-rich --render-html --output-dir /path/to/data/rich_run
python run_codex.py --video-file /path/to/input/tutorial.mp4 --extract-only --render-html --output-dir /path/to/data/tutorial_only
```

`--render-html` 是可选的无模型排版步骤，不改变正常 `tutorial.md`，也不改变 Blender 复现输入。base64 是图片编码，不等于图文内容是“建模代码”；支持 data-URI 图片的阅读器可以显示它，但某些编辑器会直接展开编码，届时可看 `tutorial_path_refs.md` 或 HTML。

`visual` 默认 balanced：每十分钟约 16 次调用上限，economy/forensic 使用各自预算；`legacy-rich` 按覆盖全部视频所需窗口数加一次修复预算。`--max-extraction-calls` 可以显式收紧；预算不足时停止，不默默截掉后半段。只有格式问题允许一次受预算限制的文档修复，不做无限重跑。

`--transcript` 支持 JSON/JSONL、SRT、VTT 及支持的平台 JSON 字幕结构；JSONL 单条可写为 `{"start": 0, "end": 4.2, "text": "添加一个立方体。"}`，时间单位为秒。无时间戳的普通文字不是有效的时间证据。平台字幕优先，其次用户字幕，最后尝试本地 ASR；`--asr-language` 仅指定 ASR 语言提示，不指定输出教程的语言。

```bash
python run_codex.py --video-file /path/to/input/tutorial.mp4 \
  --transcript /path/to/input/subtitles.srt --asr-language zh \
  --profile economy --max-extraction-calls 8 --extract-only --render-html \
  --output-dir /path/to/data/transcript_run
```

视频提取的独立接口仍为：

```bash
python skills/blender-pipeline/tutorial-extraction/scripts/extract_video_tutorial.py --video-file /path/to/input/tutorial.mp4 --title "教程标题" --output-dir /path/to/data/tutorial_package --provider codex-cli --tutorial-method visual
```

独立接口只提取教程，不启动 Blender；根目录入口默认继续完整复现。

## 5. 实际执行链与结果含义

```text
明确输入并复制配套资产
  → 所选视频提取方法 / 保留已有 Markdown
  → 本地知识检索
  → 生成材质、动态及 Blender 版本约束
  → 生成 Blender Python
  → 代码安全检查 + Blender 执行
  → 保存后重新打开 + 渲染
  → 材质、静态/动态与视觉检查
```

主要结果都在 `--output-dir` 中：

- `tutorial.md`、`tutorial_path_refs.md`、`steps_verified.json`：复现操作输入。
- `asset.blend`、`reproduce.py`、`render.png`：实际生成时才存在。
- 静态作品的 `six_views/`；有真实动态要求时的 `final_effect.mp4`。简单转台不能代替教程中的模拟或变形。
- `pipeline_review.json`：当前路线的质量判定；失败或未通过项目保持明确状态。
- `launch_manifest.json`、`input_assets.json`、`knowledge_retrieval_pack.json`：启动输入、配套文件及检索记录。
- 可选 `illustrated_tutorial.html`；visual 方法另有完整学习者输入包和 rubric。rubric 不作为模型的建模步骤输入。

启动控制、模型账本和临时文件集中在输出目录的 `.control/`；既有执行器还会在 `replay/`、`agent_trace/` 中保留模型输出和渲染诊断。视频分析缓存位于同一数据盘的相邻 `.video-tutorial-cache/<运行目录名>/`，与可交付教程分离。原始视频、图片、工程、模型响应和私有运行记录不进入代码仓库。审阅/分享结果前须另外确认输入素材权利并检查私有信息，不能直接把整个运行目录当作已清理的公开数据包。

退出码：`0` 表示所选阶段成功；若使用 `--extract-only` 或 `--check`，只表示该阶段成功。完整运行返回 `2` 表示产物仍需审阅；`1` 表示输入、依赖、调用或执行被阻止。即使通过当前自动检查，也不宣称任意教程都能 100% 忠实复现，更不代替最终人工审阅。

## 6. 知识库：存储、切分、使用和新增

### 6.1 存储边界

1. **可维护的通用规则**：[`skills/blender-pipeline/knowledge/`](skills/blender-pipeline/knowledge/index.md)。六份由 manifest 明确列出的 Markdown 按路由、生成、编辑、渲染及运行决策组织；构建器也收录 router 和 generation 入口规则。标记为 `curated`，表示维护过的指导，不伪装成逐个作品的成功复现证据。
2. **正式准入的成功经验**：状态为 `reviewed`，必须有哈希绑定的审阅证据及适用范围，才可进入活跃检索库。
3. **每次运行的索引与检索结果**：根目录启动器在 `.control/knowledge` 构建本地 manifest，不扫描本机历史运行目录、不读取 RW1。
4. **展示页目录元数据**：`reproduction/knowledge/manifest.json` 负责作品、授权和 recipe 可用性，不等于上述成功经验库。当前公开 catalog 是 61 项元数据快照，recipe 未具有独立视觉等价验收而保持禁用；普通视频入口不绕过这些门禁，也不依赖这个目录执行。

失败案例、失败运行片段、未审定的候选和 deprecated 条目不进入活跃检索。失败记录不会转换为知识片段，未审定渲染 recipe 不进入活跃检索。规则中诸如“遇到缺失依赖应停止”的安全建议仍然保留：它们是有用规则，不是失败案例。

### 6.2 严格的切分定义

知识切分以**决策规则**为单位，而非按视频、任务编号或事故堆积：

- 识别代码围栏外的 1–4 级 Markdown 标题，保留首个标题前的非空正文；相同标题按出现次数生成稳定后缀。
- 按空行分隔的完整段落打包；包内指导正文上限为 **2,200 字符**，成功教程候选为 **2,600 字符**。单段超限时按字符硬切，不丢弃尾部。片段之间 **零重叠**。
- 包内不足 80 字符的片段不单独入索引。字符上限不是 token 上限，也不是视频抽帧窗口长度。
- `source_id` 由来源类型、规范路径、章节/分片标题形成稳定身份；正文标准化哈希用于检测内容变化。同一逻辑片段更新不应产生重复记录。

完整规则见 [知识生命周期](skills/blender-pipeline/knowledge/operations-and-knowledge.md)。

### 6.3 使用逻辑

构建器 `build_blender_knowledge_index.py --manifest-only` 从包内允许列表生成索引。检索结合当前教程、对象类别和 Blender 功能，默认取十条；默认采用词法匹配，无网络及向量数据库依赖。安装并主动构建向量索引后才使用对应后端，回退也只能读取同一活跃 manifest。

每个检索摘要最多 1,400 字符，完整来源文档仍为准。两个后端均先过滤准入状态。检索建议不能覆盖教程里的明确操作；只有相容且独立批准的可执行 recipe 才能形成硬约束。知识库的作用是提供可适用的方法和兼容经验，不是替代视频、忽略输入资产或凭空补完未知参数。

### 6.4 什么时候新增，怎么新增

根目录启动器**默认关闭自动知识更新**，不会把用户跑出的教程或失败日志写回公共库。

- 维护通用规则：编辑对应 Markdown，写清适用条件、规则、判断方法和检索词；完成维护审阅后重新构建索引。不要放机器路径、API、原始 trace 或单个失败案例。
- 收集成功观察：明确设置 `BLENDER_KNOWLEDGE_ROOT` 后调用更新器，仅将通过自动检查的观察放入活跃库之外的 `<knowledge-root>_candidates/candidates.jsonl`。失败、缺少验收或不完整记录不产生知识片段。启动器中的环境设置不会自动保留到之后的终端命令：

  ```bash
  BLENDER_KNOWLEDGE_ROOT=/path/to/data/knowledge python skills/blender-pipeline/generation/scripts/update_replay_knowledge_base.py --video-dir /path/to/run
  ```
- 准入可复用成功经验：至少 **五个独立且人工验收通过的资产**，以及不重合、人工审阅通过且**零退化**的 holdout；提供 review ID、资产 SHA-256、规则身份、路由、资产类别和 Blender 版本范围。维护代码调用 `append_unique(chunks, promotion_evidence=...)`，执行 `candidate_promotion_guard` 和哈希绑定准入检查后才能将 `reviewed` 条目放进活跃库。没有“一键强制提升成功”的 CLI。

单次 Blender 退出正常、写出了图片、模型自评成功或“修过错误”，都不足以成为公共成功经验。成功候选在正式准入前也不参与活跃检索。

## 7. 预算、失败处理与安全

- `--max-extraction-calls` 与 `--max-replay-calls` 分别限制提取和代码生成/视觉复核，两者不是共享预算。完整消耗最多为两个阶段之和；分辨率、视频长度和模型收费仍影响实际价格。
- 复现默认最多 8 次调用、500,000 个保守 Token 预算、2 次总尝试（包含第一次）。使用 `--max-replay-tokens`、`--repair-attempts` 调整。调用预算不是价格承诺。
- 同一已持久化调用可以复用；发送结果不明时不会自动重复付费请求。网络恢复不等于允许无限重试。
- 生成代码在执行前接受检查，不能任意读写文件、下载资源或启动子进程。包装器负责已授权资产加载、保存和渲染。这些检查不是操作系统级沙箱；应在适当隔离的环境处理第三方工程和生成代码。未受信任 `.blend` 不启用自动脚本。
- 输入预览不是目标图，配套资产不是可忽略的提示。检查缺失的图片、工程、插件或依赖时应修复输入，不应诱导模型随便造一个替代品。
- 工程和模型输出保留在用户的数据盘。正式分享前清理敏感运行控制记录；不可把“来源可下载”当成允许再分发。
- 用于模型推理的输入、截图证据、教程文字和预览图可能发送至所选模型服务商。使用前应确认这些素材允许通过该服务处理。

## 8. 完整参数参考

两个根目录入口共享参数；其中 API 专用参数不适用于 Codex 模式。API 的配置文件只接受 `endpoint`、`api_key_file`、`model`、`blender`，其他运行选项应通过命令行传入。

| 参数 | 用途 / 默认值 |
| --- | --- |
| `--video-file` / `--video-url` / `--tutorial` | 三选一的主输入；`--check` 或 `--configure` 不需要主输入 |
| `--title` | 教程标题；默认输入文件名，链接输入默认 `video_tutorial` |
| `--asset`、`--asset-root` | 起始模型，以及包含该模型的完整依赖目录 |
| `--input-asset` | 辅助文件或目录，可重复传入 |
| `--preview`、`--target-image` | 起始预览（可重复）与最终目标图（单张），角色不同 |
| `--output-dir` | 必需的新空目录，位于仓库之外；配置模式除外 |
| `--config`、`--configure` | 读取配置；API 模式交互创建配置与密钥文件 |
| `--endpoint`、`--api-key-file` | API 专用；HTTPS Chat Completions 地址及私有密钥文件 |
| `--blender` | Blender 可执行文件；也支持配置、环境变量和 `PATH` |
| `--model`、`--fallback-reason` | 默认 `gpt-5.6-sol`；显式退到 `gpt-5.5` 时必须说明实际不可用原因 |
| `--tutorial-method` | `visual`（默认）或 `legacy-rich`；已有 Markdown 不运行提取 |
| `--profile` | visual 提取预算档位：`economy`、`balanced`（默认）、`forensic`；不改变 legacy-rich 的全视频窗口覆盖 |
| `--transcript`、`--asr-language` | 已有带时间戳字幕与 ASR 语言提示（默认 `auto`） |
| `--render-html` | 额外生成人类可读 HTML，不增加模型调用 |
| `--extract-only` | 教程准备后结束，不执行 Blender 生成 |
| `--max-extraction-calls` | 提取阶段总调用上限，含修复；默认按方法/档位计算 |
| `--max-replay-calls` | 代码生成和视觉复核合计最多 8 次（默认） |
| `--max-replay-tokens` | 复现阶段保守 Token 预留上限，默认 500,000 |
| `--repair-attempts` | 总尝试次数，包含初次；可选 1、2、3，默认 2 |
| `--check` | 检查本地依赖并做一次真实小型模型请求，不执行资产复现 |
| `--dry-run` | 只打印计划，无文件写入、下载、密钥读取或模型调用 |
| `--help` | 显示当前入口的参数说明 |

例如限制整次复现的模型调用与尝试次数：

```bash
python run_api.py --config "$HOME/.config/blender-pipeline/pipeline.json" \
  --tutorial /path/to/input/tutorial.md --max-replay-calls 4 \
  --max-replay-tokens 100000 --repair-attempts 1 \
  --output-dir /path/to/data/bounded_run
```

## 9. 高级接口与非目标场景

根目录入口覆盖视频/Markdown 驱动的普通复现与源资产辅助复现。现有高级接口继续保留：

- [Tutorial Extraction Skill](skills/blender-pipeline/tutorial-extraction/SKILL.md)：仅视频转教程。
- [Generation Skill](skills/blender-pipeline/generation/SKILL.md)：已准备工作区的严格重放及既有 model-direct 接口；后者当前有固定 release-index/批次约束，不冒充任意自由输入的一键接口。
- [Editing Skill](skills/blender-pipeline/editing/SKILL.md)：已有工程的明确局部修改，使用独立的 edit request、before/after 和非目标稳定性合同。
- [Reproduction Skill](skills/blender-pipeline/reproduction/SKILL.md)：已批准 showcase recipe 的授权感知重放，禁用/不完整 recipe 不能启动。

材质约束、Blender 版本选择、代码安全、保存后重开、静态/动态输出和视觉检查由共同的重放执行器负责。模型训练、RW1 数据获取、benchmark 数据发布和展示页部署不是根目录启动器的职责。

主要代码位置：

```text
run_api.py / run_codex.py                 根目录启动入口
pipeline.config.example.json             无密钥的配置模板
skills/blender-pipeline/
  scripts/                               共享启动器、输入适配、包检查
  tutorial-extraction/                    两种视频转教程方法
  generation/                            Blender 代码生成与重放
  editing/                               独立局部编辑接口
  reproduction/                          授权感知的 showcase recipe 路由
  knowledge/                             可维护的通用知识
```

## 10. 开发检查与已知限制

```bash
python skills/blender-pipeline/scripts/validate_package.py
python -m unittest discover -s skills/blender-pipeline/tests
python -m unittest discover -s skills/blender-pipeline/generation/tests
python -m unittest discover -s skills/blender-pipeline/reproduction/tests
python -m unittest discover -s skills/blender-pipeline/editing/tests
python -m unittest discover -s skills/blender-pipeline/tutorial-extraction/tests -p 'test_*tutorial_pipeline.py'
```

设置 `BLENDER_BIN=/path/to/blender` 后，还会运行可选的真实 Blender 生成与编辑测试。GitHub Actions 在 Python 3.10、3.12 下检查公开代码包，不执行付费模型调用。真实模型全链路测试需要另行使用已授权账户及明确预算运行。

本地测试可覆盖接口、资产传递、Markdown 适配、知识准入和预算行为；`--check` 可单独检查真实服务连接。它们不代表所有视频均复现成功，也不构成跨 Blender 版本的完整兼容性证明。涉及真实模型或 Blender 的测试需要相应服务、额度与本机应用。

| 常见情况 | 应对方式 |
| --- | --- |
| 找不到 Blender / FFmpeg / Codex | 安装相应应用并检查 `PATH`；Blender 可显式传 `--blender` |
| 视频链接无法获取 | 确认访问与使用权限，改用合法取得的本地视频；启动器不绕过平台限制 |
| API 拒绝、额度不足、模型不可用 | 检查服务配置、权限及模型映射；不静默换模型或无限重试 |
| 缺失贴图、链接工程或插件 | 使用 `--asset-root` 提供完整依赖，或准备与教程相容的 Blender 环境 |
| Markdown 无法识别操作 | 使用编号操作标题/有序列表，补齐本地图片；不要传只有结论的说明文 |
| 纯文字 Markdown 缺少视觉依据 | 完整复现需补 `--target-image`；只准备教程可用 `--extract-only`；起始 `--preview` 不能代替最终目标 |
| 输出目录已有内容 | 使用新的空目录；入口不会自动覆盖或恢复旧目录 |
| 已有 `.blend` 但退出码为 2 | 查看 `pipeline_review.json` 的未通过项；产物存在不等于合格 |
| 动态效果与视频不符 | 检查真实模拟、形变与时间轴；转台视频不能替代这些效果 |

提交问题时请给出脱敏命令、操作系统、Python/Blender 版本、失败阶段和最小可共享输入。不要上传密钥、完整私有模型响应、未授权视频或个人绝对路径。修改应保持根目录两个入口、两种教程方法与知识准入约束的一致性。

## 11. 许可与素材权利

本仓库当前未附带明确的软件 `LICENSE`。代码公开可见不自动授予任意再分发权；正式作为开源软件发布前，仓库维护者应确定并补充许可证。输入视频、图片、模型、代码依赖与模型服务各自的条款独立适用，本项目不能代替权利人授权。
