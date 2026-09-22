# Paper2Video App

该项目是一个将PDF文件转换为讲解视频的应用程序。用户可以输入一个PDF文件，应用程序将提取其中的文本和图片，并生成一个引人注目的讲解视频。

## 项目结构
```
├── cache
├── config.yaml
├── font
├── output
├── src
│   ├── distribution          # 多平台上传模块
│   │   ├── __init__.py
│   │   ├── orchestrator.py   # 上传编排（视频优先，图文降级）
│   │   ├── bilibili.py       # B站上传（biliup Cookie）
│   │   └── xiaohongshu.py    # 小红书上传（MCP 视频+图文）
│   ├── llm_tools
│   │   ├── llm_agent.py      # LLM 客户端（延迟初始化）
│   │   ├── prompts.py        # 所有 prompt 统一管理
│   │   └── image_agent.py    # Qwen-VL 图片解释
│   ├── utils
│   │   └── audio_helpers.py  # TTS/音频安全处理
│   ├── main.py
│   ├── env_setup.py               # 网络配置 + arxiv 链接解析 + JSR_NETWORK_PROFILE
│   ├── figure_analyzer.py         # SAM3(Edit Banana) + Qwen-VL 论文主图精确分析
│   ├── website_video_pipeline.py  # 网页顶部视频转发（下载并双平台发布）
│   ├── video_creator.py      # 视频合成（Ken Burns + 转场 + 字幕）
│   ├── manim_engine.py       # Manim 动画演示引擎
│   └── manim_references/     # Manim 参考代码与最佳实践
├── tests                     # pytest 单元测试
│   ├── distribution/
│   └── ...
├── requirements.txt
├── README.md
└── README_EN.md
```

## 功能

1. **PDF处理**：从PDF文件中提取文本和图片。
2. **视频创建**：Ken Burns 动画 + 淡入淡出转场 + 半透明字幕，支持目标时长控制。
3. **图文语义匹配**：基于 structured_plan 和 image_explanations 中的 recommended_section 字段，将摘要句子按语义匹配到对应图片，章节标题使用真实的 figure_role。
4. **LLM脚本生成**：5段式结构化视频脚本（opening→intro→method→results），课堂讲解风格。
5. **多平台上传**：B站 / 抖音视频上传 + 小红书图文卡片发布（标题与旁白渲染进图，不再上传视频）。
6. **TTS并发合成**：默认 DashScope cosyvoice-v2，可路由到 Qwen3-TTS / MiniMax Speech-2.8-HD 后端（`JSR_TTS_MODEL` / `JSR_TTS_VOICE` 覆盖），多线程并发加速。
7. **图片智能筛选**：LLM 打分选取最重要的图片，Qwen-VL 生成图片讲解。
8. **Demo 视频下载**：自动从论文项目主页提取并下载演示视频，嵌入最终视频开头。
9. **网页视频转发**：从研究网页提取顶部主视频、封面和页面摘要，直接转发到 B站 与 小红书。
10. **Manim 动画演示** (v3.1)：自动生成论文的 Manim 数学动画演示视频。
    - opencode + DeepSeek-R1 + manim_skill 最佳实践生成 ManimCE 代码
    - 固定 4 场景结构：标题→背景→方法→实验结果
    - TTS 讲解与 structured_plan 脚本同步
    - 动画后自动切换到论文图片，按 caption 智能匹配
    - 自动文本换行（中英文）+ 边界检查防超框
11. **论文主图精确分析**：SAM3(Edit Banana) + Qwen-VL 双路联合分析，用于 MethodScene。
    - Edit Banana 提取精确 bbox_normalized + 十六进制颜色 + 形状先验
    - Qwen-VL-Max 输出语义 JSON（组件/连接/布局/数据流）
    - 双路结果融合后注入 architecture prompt，省去手工描述结构
    - 有精确 bbox 时，manim_context 仅输出语义（避免与精确坐标冲突）
12. **--paper-link 精准入口**：传入 arxiv URL 直接获取论文元数据，跳过关键词搜索和日期过滤。
13. **--pdf 本地 PDF 入口**：传入本地 PDF 文件路径，解耦"获取 PDF"与"处理 PDF"，直接复用标准 PDF 处理链（文本/图片提取、图像解释、Manim 公式/示意动画）出讲解视频。适用于非 arxiv、离线或已下载的 PDF。无 arxiv id 时公式自动走 LLM 兜底、figure 走视觉分析。`arxiv_id = "local-" + md5(abspath(pdf))[:8]`。与 `--paper-link / --filename / --discover / --blog-url` 五选一互斥。
14. **arxiv LaTeX 源码直读** (v3.2)：给定 arxiv_id 时优先从 arxiv e-print tarball 提取 TikZ/矢量图元做结构化分析。
    - 模块 `src/arxiv_source_analyzer.py`：对外唯一入口 `try_structured_figure(arxiv_id, image_path, cache_root, paper_context)`
    - 路径 A（TikZ）：展开 `\input/\include` + 用户宏 → 抽 `\begin{figure}` → `parse_tikz_structure` 解析 `\node`/`\draw`
    - 路径 B（raster PDF）：tarball 已含 `.pdf` 图时，`pymupdf.get_drawings()` 抽矢量对象
    - 路径 C（pdflatex 兜底）：仅有 `.tex` 时跑 `pdflatex` 重编译 → 按 figure label 定位页码 → pymupdf 抽矢量
    - 多图时用 Qwen 按 caption 打分选主图；LaTeX 成功后仍跑 Qwen-VL 补语义字段（key_innovation/data_flow/animation_suggestion）
    - 缓存在 `cache/arxiv_src/<aid>/`，含 `.manifest.json` +（可选）`.compiled.pdf`（不放 /tmp）
    - Kill switch：设 `JSR_DISABLE_LATEX_SOURCE=1` 即回退到 SAM3+VL 路径
    - 依赖：`pylatexenc`（>=2.10）、`pymupdf`、系统 `pdflatex`（可选，仅路径 C 需要）
    - 透传入口：`ManimEngine(paper_text, structured_plan, arxiv_id=...)` 或在 `main.py --paper-link` 分支自动透传
15. **三平台关键词动态生成**：每篇论文 LLM 一次产出 B站 / 小红书 / 抖音三平台关键词 dict（不再写死），三层兜底：prompt 约束 + 字符过滤 + 数量上限。
16. **抖音上传集成**：基于 [`dreammis/social-auto-upload`](https://github.com/dreammis/social-auto-upload) Playwright 路线，子进程跨 venv 调用，含首次扫码登录的容器内二维码暴露流程 + 短信验证码弹窗自动 fill 入口。
17. **三平台评论自动回复**：B站走 `bilibili-api-python` 官方 API、小红书走 `xhs` 库（SAU venv 子进程）、抖音读路径走 [`Johnserf-Seed/f2`](https://github.com/Johnserf-Seed/f2) mobile API（cookie 月级稳定，跟 PC 创作者中心风控通道隔离）+ 写路径走 chromium daemon CDP。LLM 活泼互动人设统一回复，sqlite 去重 + 日上限节流（B站 50/小红书 15/抖音 20）。
18. **创作者中心数据聚合**：B站直接 API；小红书/抖音 mobile API（f2）；sqlite 存储 + ASCII summary 表 + CLI 子命令 (`fetch` / `summary`)；可选飞书多维表格上报（stub，需配置 lark 凭证）。
19. **Manim-only 模式**：`--manim` 时跳过 main video 渲染（节省 60-95 分钟），仅生成 manim 视频作为最终输出。`paperagent_workflow.generate_daily_arxiv_summary(skip_main_video=True)` 控制；`structured_plan` + `paper_text` 同步缓存到 `cache/` 供后续 manim 阶段读取。
20. **Manim 累加显示 + 文字重叠守卫**：单屏 5 个元素以内禁止中途 FadeOut，元素累加 FadeIn 直至本场景结束统一一次 FadeOut；`_ensure_page_fadeouts` 已禁用避免 post-process 强插中间 FadeOut。`_inject_text_overlap_guard` 运行时包裹 `self.play`，每次播放后按包围盒检测文字 mobject 重叠（交叠面积 / **较大块面积** > 0.5，要求两块大幅互相重合才判定为糊成一团），只保留最上层（最新/ z_index 最高）文字，把被遮挡的下层文字 FadeOut —— 即"新文字出现后只显示最上层的文字"，不重叠的文字仍累加保留。文字类型用 `isinstance` 识别（覆盖 Title 等子类）；阈值用较大块面积归一化，避免小标签压在大段落角落时误删整段。
21. **opencode 全文上下文**：取消论文文本截取（之前 `[:2000]` / `[:1500]`），DeepSeek-V4-Pro 长上下文窗口直接吃论文全文，提升 manim 代码与方法图分析质量。
22. **Chromium daemon + CDP attach**（Docker 容器自包含写路径方案）：长跑 chromium 进程（Xvfb headed）+ aiohttp 健康检查 endpoint，所有抖音写操作（上传/评论回复）通过 `connect_over_cdp` 复用同一 page，避免反复装载 cookie 触发风控吊销。
23. **Docker 化容器自包含部署**：`docker compose up -d` 一键起 paperagent + xhs-mcp + chromium-daemon 三个 service，cookie / config / cache 走 volume mount。详见 `docs/DOCKER.md`。
24. **核心公式讲解 + 自适应 scene**：从论文自动挑选最多 2 个核心公式（损失/目标/核心机制），在 Manim 视频里插入 FormulaScene 逐项讲解——整条公式 Write 出现后，逐项 FadeIn 中文注解 + Indicate 高亮当前项，最后统一 FadeOut。
    - **自适应 scene 编排**：固定 4 段（Title/Intro/Method/Results）基础上，按提取到的有效公式数在 **Method 与 Results 之间**插入 FormulaScene；0 公式 → 4 scene（与老链路字节级一致），1 公式 → 5 scene，≥2 公式 → 6 scene。**公式 ≤ 2、总 scene ≤ 6** 为硬上限，超出截断。
    - **源码优先 + LLM 兜底**：有 arxiv_id 且能拉到 `.tex` 源码时，`extract_equations` 抽行间公式候选喂 LLM 精选（最准）；拿不到源码（含 `blog-` 前缀、无 arxiv_id）时回退到让 LLM 直接从正文识别公式。manim 链路已自动透传 arxiv_id（`paperagent_workflow` 两处 plan 调用 + `main.py` fallback）。
    - **渲染前 LaTeX 预编译校验 + 降级**：每条公式 latex 先经 `validate_latex` 子进程预编译，不过则 LLM 修一次，仍不过直接丢弃，杜绝一个语法错的 `MathTex` 炸掉整条视频渲染。
    - **开关**：`JSR_DISABLE_FORMULA=1` 一键关闭公式提取（功能默认开启，回到纯 4 scene）；`JSR_DISABLE_LATEX_VALIDATE=1` 跳过子进程预编译校验（信任输入，CI/无 latex 环境用）。
    - 编排逻辑在 `ManimEngine._build_scene_defs(plan_scripts)`，公式提取在 `llm_tools.llm_agent._extract_core_formulas(text, arxiv_id)`；集成测试见 `tests/test_formula_pipeline.py`。
25. **JS/Web 示意动画**：从论文自动挑选最多 2 个最值得动起来的场景，由 opencode 生成**自包含 HTML 动画**，headless playwright 逐帧录屏成 mp4，复用 blog 视频链路对齐 TTS 后作为独立 scene 嵌入视频——和公式 scene 平行的一条「外部 mp4 注入」管线。
    - **两类示意**：`concrete`（具象卡通，机器人/操作任务等物理场景）与 `abstract`（抽象机制，算法流程/数据流/网络结构）；plan 层按论文核心任务/机制自动判定 kind。
    - **自适应 scene 编排**：固定 4 段（Title/Intro/Method/Results）基础上，`position=intro_after` 的示意插在 IntroScene 之后、Method 之前（任务引子），`position=method` 的插在 method 区末尾（公式 scene 之后、Results 之前）。**总 scene ≤ 7、额外（公式 + 示意）≤ 3** 为硬上限：公式优先占额度（论文核心），示意用剩余额度且自身上限 2，超出截断丢弃。
    - **技术栈**：标准 **playwright**（非 patchright——其 evaluate 跑在 isolated world，访问不到页面 `window.renderFrame`）+ 复用 `~/.cache/ms-playwright` 已有 chromium（`executable_path` 启动，不必下新版本）+ HTML 侧 `window.renderFrame(n)` **确定性逐帧渲染**（禁 setTimeout/requestAnimationFrame/Date.now，总时长 = `TOTAL_FRAMES/FPS` 精确可控，正好喂 TTS 对齐）。
    - **画面来源（阶段 2 预留）**：spec 含 `prefer_real_demo` 字段，物理拟真度高（布料形变/接触力学/真机操作）的场景标记为优先用论文真实 demo 视频；本阶段统一走 JS 生成，真实 demo 抓取留待阶段 2。
    - **失败降级**：HTML 生成或 HTML→mp4 渲染任一失败，该示意 scene 整条干净丢弃（同步剔除 scene_defs/narrations 并重建索引），不影响其余 scene。
    - **开关**：`JSR_DISABLE_JS_ANIM=1` 一键关闭示意动画提取（功能默认开启，回到公式/纯 4 scene 编排）；plan 提取任何异常一律 fallback `[]`，绝不炸掉整条 plan。
    - 编排逻辑在 `ManimEngine._build_scene_defs(plan_scripts)`（注入 AnimScene）+ `apply_anim_render_results`（渲染失败重建索引），渲染底座在 `js_anim_engine.{_opencode_generate_html, render_html_to_mp4}`，示意提取在 `llm_tools.llm_agent._extract_scene_animations(text, arxiv_id)`；编排层集成测试见 `tests/test_js_anim.py`，渲染底座单测见 `tests/test_js_anim_engine.py`。

## 使用说明

### 安装依赖

```bash
conda create -n paperagent python=3.10
conda activate paperagent
pip install -r requirements.txt
pip install biliup  # B站上传
pip3 install torch torchvision torchaudio  # Linux
pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126  # Windows
pip install -e .
pip install manim  # Manim 动画引擎
npm install -g opencode-ai  # opencode（Manim 代码生成）
npx skills add adithya-s-k/manim_skill --yes  # ManimCE 最佳实践
```

### 修复tesseract兼容性问题

请修改 `anaconda3/envs/paperagent/lib/site-packages/deepdoctection/extern/tessocr.py`:

```python
# 263行 前加入
if not results:
   return all_results
```

```python
# 181行 删掉文件后缀名
with open(tmp_name, "rb") as output_file:
```

---

# 多平台上传功能

## B站上传（biliup Cookie 方式）

### 1. 安装依赖
```bash
pip install biliup
```

### 2. 配置 Cookie
在浏览器中登录 bilibili.com，打开 DevTools (F12) → Application → Cookies，复制以下 4 个值填入 `config.yaml`：

```yaml
bilibili_cookies:
  sessdata: "<SESSDATA>"
  bili_jct: "<bili_jct>"
  dedeuserid: "<DedeUserID>"
  dedeuserid_ckmd5: "<DedeUserID__ckMd5>"
```

### 3. 使用示例
```python
from src.distribution.bilibili import upload

result = upload(
    video_path="/path/to/video.mp4",
    title="视频标题",
    tags="标签1,标签2",
    desc="视频描述",
    cover_path="/path/to/cover.png",
    tid=188  # 分区ID
)

if result:
    print(f"上传成功! BV号: {result}")
```

### 4. 命令行测试
```bash
python -m src.distribution.bilibili --test-upload --video ./output/video.mp4 --title "测试" --tags "测试"
```

---

## 小红书 MCP 上传方式

### 1. 安装 Go 环境
```bash
# Windows: https://go.dev/dl/
# Linux: conda install -c conda-forge go
```

### 2. 启动 MCP 服务
```bash
# 方式一：Docker（推荐）
docker run -p 18060:18060 xpzouying/xiaohongshu-mcp

# 方式二：源码运行
git clone https://github.com/xpzouying/xiaohongshu-mcp
cd xiaohongshu-mcp
go run .
```

### 3. 安装 MCP Python 库
```bash
pip install mcp requests
```

### 4. 使用示例
```python
from src.distribution.xiaohongshu import upload_to_xiaohongshu

# 图文模式
upload_to_xiaohongshu(
    title="笔记标题（限20字）",
    content="正文内容（限1000字）",
    images=["/path/to/image1.jpg", "/path/to/image2.png"],
    is_video=False
)

# 视频模式
upload_to_xiaohongshu(
    title="视频标题",
    content="视频描述",
    video_path="/path/to/video.mp4",
    cover_path="/path/to/cover.png",
    is_video=True
)
```

### 5. 小红书MCP工具函数
| 函数 | 功能 |
|------|------|
| `check_login_status()` | 检查登录状态 |
| `publish_note()` | 发布图文笔记 |
| `publish_video()` | 发布视频 |
| `search_notes()` | 搜索内容 |
| `get_user_profile()` | 获取用户信息 |

---

## 运行应用程序

```bash
# 默认双平台自动上传（B站 + 小红书图文）
python src/main.py --filename "{papername}"

# 仅上传 B站
python src/main.py --filename "{papername}" --platforms bilibili

# 仅上传小红书图文
python src/main.py --filename "{papername}" --platforms xiaohongshu

# 禁用上传，仅生成本地视频
python src/main.py --filename “{papername}” --platforms none

# 生成 Manim 动画演示视频
python src/main.py --filename "{papername}" --manim --platforms none

# Manim + TTS 语音讲解
python src/main.py --filename "{papername}" --manim --manim-tts --platforms none

# 高质量 1080p
python src/main.py --filename "{papername}" --manim --manim-quality high --platforms none

# 控制视频时长（默认300秒=5分钟）
python src/main.py --filename “{papername}” --target_duration 180 --platforms none

# 通过 arxiv 链接直接生成 + 上传（推荐，绕过关键词搜索）
JSR_NETWORK_PROFILE=gsjts python src/main.py \\
    --paper-link https://arxiv.org/abs/2410.11758 \\
    --manim --manim-tts --target_duration 300 \\
    --platforms bilibili,xiaohongshu

# 本地 PDF 直接出视频（解耦"获取 PDF"与"处理 PDF"，非 arxiv / 离线 PDF 适用）
# 无 arxiv id → 公式走 LLM 兜底、figure 走视觉分析；与上述入口五选一互斥
python src/main.py \
    --pdf ./cache/report.pdf \
    --manim --manim-tts --output_language zh \
    --target_duration 300 --platforms none

# --discover 自动选题（HF Daily Papers + arxiv recent + opencode 排序）
# 与 --paper-link / --filename 严格三选一互斥
JSR_NETWORK_PROFILE=gsjts python src/main.py \
    --discover "embodied AI" \
    --discover-sources hf,arxiv \
    --manim --manim-tts --target_duration 300 \
    --platforms bilibili,xiaohongshu

# --discover 跳过 opencode (kill switch, 走启发式)
JSR_DISCOVERY_DISABLE_OPENCODE=1 JSR_NETWORK_PROFILE=gsjts python src/main.py \
    --discover "VLA manipulation" --platforms none

# 自定义选题偏好（默认 config/discovery_profile.yaml）
python src/main.py --discover "world model" \
    --discover-profile config/discovery_profile.yaml --platforms none

# --discover 启用 venues 数据源（profile 里 uncomment venues）
# 例：从 RSS 2025 / NeurIPS 2025 接收论文中挑一篇做选题
# 编辑 config/discovery_profile.yaml 解开 venues: 注释，再正常 --discover

# 将研究网页顶部主视频直接转发到 B站 + 小红书
python src/website_video_pipeline.py --url "https://www.pi.website/research/rlt"

# 仅转发到 B站
python src/website_video_pipeline.py --url "https://www.pi.website/research/rlt" --platforms bilibili
```

说明：
- `--platforms` 支持 `bilibili,xiaohongshu` 的逗号组合，默认值为 `bilibili,xiaohongshu`。
- 平台上传采用”部分成功”策略：某一个平台失败不会阻塞另一个平台。
- `--target_duration` 控制目标视频时长（秒），默认300秒。多篇论文时自动均分到每篇。系统通过文字预算、图片筛选和TTS后裁剪三层机制控制时长。

## 网页视频转发 Pipeline

适用场景：你已经有一个研究网页，希望直接抓取网页顶部主视频并发布到 B站、小红书，而不是重新生成讲解视频。

默认行为：
- 自动抓取页面标题、描述、封面图、PDF 链接与顶部主视频地址
- 自动优先生成中文发布标题；若 LLM 不可用则回退到关键词规则标题
- 下载视频到 `output/`
- 复用现有 B站与小红书上传模块完成发布
- 默认平台为 `bilibili,xiaohongshu`

示例：

```bash
python src/website_video_pipeline.py --url "https://www.pi.website/research/rlt"
python src/website_video_pipeline.py --url "https://www.pi.website/research/rlt" --platforms xiaohongshu
```

说明：
- 当前实现面向 Next.js 研究页，优先提取页面顶部主视频
- 发布标题默认优先使用中文标题，原英文标题仍会保留在简介/正文中
- 小红书视频发布仍受 MCP 限制，上传时可能忽略封面，需在 App 内手动设置

---

## opencode skill 集成 — paper-search

仓库内置 `skills/paper-search/`，把 arxiv（按 topic / 按 venue+year）和 HuggingFace Daily Papers 三种搜索能力包装成 JSON-stdout 的 CLI，方便 opencode 在 `--discover`、`manim` 生成、未来其他流程里直接调。

### 安装本地 skill（推荐，仓库级）

```bash
cd ~/Projects/VlogCutter/JushenRenji
npx skills add ./skills/paper-search --yes
```

执行后 `npx skills` 会把 SKILL.md 同步到 `.agents/skills/paper-search/`，并为本地各 agent（含 opencode / claude code / codex / gemini cli / GitHub Copilot 等）创建 symlink。SKILL.md 改动后重新跑同一条命令即可同步。

### 手动 fallback 安装

如果 `npx skills` 不可用（无 Node 环境）或想直接给 opencode 用：

```bash
mkdir -p .opencode/skills/paper-search
cp -r skills/paper-search/* .opencode/skills/paper-search/
```

### CLI 直接调用

不通过 skill 也能直接跑 CLI（适合脚本 / CI）：

```bash
# 按 topic 搜 arxiv
JSR_NETWORK_PROFILE=gsjts python -m src.discovery_sources.cli arxiv-search \
    --query "diffusion policy" --days 14 --limit 20

# 按会议 + 年份搜 arxiv 接收论文（best-effort 模糊匹配 co: 字段）
JSR_NETWORK_PROFILE=gsjts python -m src.discovery_sources.cli arxiv-by-venue \
    --venue RSS --year 2025 --limit 50

# HF Daily Papers
JSR_NETWORK_PROFILE=gsjts python -m src.discovery_sources.cli hf-daily --limit 20
```

输出统一 JSON 契约：`{"ok": bool, "count": int, "papers": [...]}`，错误时 `ok=false`、`error` 字段、exit code 1，stdout 仍是合法 JSON。

注意：`arxiv-by-venue` 是 best-effort，arxiv `co:` 字段索引并不完全；详见 `skills/paper-search/SKILL.md`。

---

### 视频脚本生成 (skill 路径)

仓库内置 `skills/video-plan/`，把"5 段式结构化视频脚本生成"（opening / intro / method / results / conclusion）包装成 JSON-stdout CLI，方便 opencode / Claude / 其他 agent 在 paper2video 流程外单独调用。

**默认行为不变**：`generate_structured_video_plan(text, ...)` 仍走原 DeepSeek API 单次。设 `JSR_USE_SKILL_VIDEO_PLAN=1` 后，函数内部会改走 subprocess 调本 skill；skill 失败时自动 fallback 回原 Python 路径。

#### 安装本地 skill

```bash
cd ~/Projects/VlogCutter/JushenRenji
npx skills add ./skills/video-plan --yes
```

#### CLI 直接调用

```bash
cat > /tmp/paper_meta.json <<'EOF'
{
  "title": "ViTacFormer",
  "abstract": "Cross-modal transformer for visuo-tactile manipulation...",
  "authors": ["Jane Doe", "John Smith"],
  "key_points": ["跨模态注意力", "200K 预训练"]
}
EOF

JSR_NETWORK_PROFILE=gsjts python -m src.llm_tools.cli generate-plan \
    --paper-meta /tmp/paper_meta.json \
    --target-duration 300 \
    --language zh \
    --out /tmp/plan_out.json
```

输出 stdout：`{"ok": true, "out": "/tmp/plan_out.json", "sections": ["opening","intro","method","results","conclusion"]}`，`/tmp/plan_out.json` 内是 5 段 JSON。

#### 在 paper2video 流程中启用 skill 路径

```bash
JSR_USE_SKILL_VIDEO_PLAN=1 JSR_NETWORK_PROFILE=gsjts python src/main.py \
    --paper-link https://arxiv.org/abs/2506.15953 \
    --target-duration 300
```

`generate_structured_video_plan` 内部会自动 subprocess 调 `python -m src.llm_tools.cli generate-plan`，skill 输出的 5 段会规整成下游期望的 4 段（conclusion 合并到 results 段尾），保持向后兼容。详见 `skills/video-plan/SKILL.md`。

---


### 方法图语义分析 (figure-analysis skill 路径)

仓库内置 `skills/figure-analysis/`，把 `figure_analyzer` 里的"图像语义分析"步骤
（识别 components / connections / key_innovation 等）包装成 JSON-stdout CLI，
内部用 **opencode 多轮 reasoning** 替代 Qwen-VL 单次问到底，
覆盖率更稳，遗漏更少。

**默认行为不变**：`analyze_figure_with_vision_llm(image_path, paper_context)` 仍走原 Qwen-VL `qwen-vl-max` 单次。设 `JSR_USE_SKILL_FIGURE=1` 后，函数会优先 subprocess 调本 skill；skill 失败时自动 fallback 回 Qwen-VL。

opencode 当前主力模型 (deepseek/deepseek-v4-pro 等) 不支持图像直输，本 skill
是基于 `paper_context` (caption / abstract / method 段落) + LaTeX 源码路径的
**文本三轮推理**，分别问：

1. Round 1：列出 5-10 个核心模块 (name / chinese_name / type / description)
2. Round 2：模块之间的连接关系 (from / to / label / type / description)
3. Round 3：figure_type / layout_direction / key_innovation / data_flow / animation_suggestion / has_neural_network / nn_layers

输出 JSON 与 `_merge_analyses` schema 兼容（`source="vision_skill"`, `has_precise_bbox=False`），精确 bbox 仍由 EditBanana / SAM3 / arxiv_latex 等下游路径补。

#### 安装本地 skill

```bash
cd ~/Projects/VlogCutter/JushenRenji
npx skills add ./skills/figure-analysis --yes
```

#### CLI 直接调用

```bash
JSR_NETWORK_PROFILE=gsjts python -m src.figure_cli analyze-figure \
    --image cache/arxiv_src/2506.15953/img/arch.png \
    --paper-context "ViTacFormer: cross-modal transformer for visuo-tactile manipulation" \
    --out /tmp/figure_analysis.json
```

stdout：`{"ok": true, "out": "/tmp/figure_analysis.json", "components": 6, "connections": 5}`。

#### 在 paper2video 流程中启用 skill 路径

```bash
JSR_USE_SKILL_FIGURE=1 JSR_NETWORK_PROFILE=gsjts python src/main.py \
    --paper-link https://arxiv.org/abs/2506.15953 \
    --target-duration 300
```

详见 `skills/figure-analysis/SKILL.md`。

---

### 图片重要性打分 (image-rating skill 路径)

仓库内置 `skills/image-rating/`，把 `llm_tools.llm_agent.rate_image_importance` 的"图片批量打分"步骤包装成 JSON-stdout CLI，内部用 **opencode 多轮决策** 替代原 LLM 单次问到底：

1. Round 1：基于 caption / figure_role / paper_context 给每张图初步打 0-10 分
2. Round 2：自检——视觉化效果差吗？跟其他高分图重复吗？caption 是否对应核心贡献？
3. Round 3：最终分数 + 推荐放在哪一段（opening / intro / method / results / none） + 一句话理由

LESSONS 2026-04-15 Bug1 记过——某次公式图被 LLM 给到 0.92 高分，导致视频里出现长时间公式截图。本 skill 在 prompt 里写明"公式 / 约束 / 损失函数 / 定理截图 ≤ 3 分，section=none"，且在 CLI 代码层 `_enforce_formula_constraint` **再 enforce 一遍**作为双重保险。

**默认行为不变**：`rate_image_importance(captions)` 仍走原 LLM 单次。设 `JSR_USE_SKILL_RATE_IMAGES=1` 后，函数会优先 subprocess 调本 skill；skill 失败时自动 fallback 回原 LLM 单次。返回值始终是 `list[int]`（legacy API 兼容）。

#### 安装本地 skill

```bash
cd ~/Projects/VlogCutter/JushenRenji
npx skills add ./skills/image-rating --yes
```

#### CLI 直接调用

```bash
cat > /tmp/cands.json <<'EOF'
[
  {"image_index": 0, "caption": "Figure 1: Overall architecture", "figure_role": "method", "paper_context": "ViTacFormer"},
  {"image_index": 1, "caption": "Equation 3: Loss function", "figure_role": "method", "paper_context": "ViTacFormer"},
  {"image_index": 2, "caption": "Figure 4: Quantitative comparison", "figure_role": "results", "paper_context": "ViTacFormer"}
]
EOF

JSR_NETWORK_PROFILE=gsjts python -m src.llm_tools.cli rate-images \
    --candidates /tmp/cands.json \
    --target-count 3 \
    --out /tmp/scores.json
```

输出 stdout：`{"ok": true, "scored": 3, "above_5": 2}`，`/tmp/scores.json` 内每张图含 `image_index / score / reason / recommended_section / rejected` 字段。idx=1 公式图会被自动拉到 ≤ 3 分 + section=none。

#### 在 paper2video 流程中启用 skill 路径

```bash
JSR_USE_SKILL_RATE_IMAGES=1 JSR_NETWORK_PROFILE=gsjts python src/main.py \
    --paper-link https://arxiv.org/abs/2506.15953 \
    --target-duration 300
```

`paperagent_workflow.py` 调用 `rate_image_importance(captions)` 时，环境变量触发后会 subprocess 调 `python -m src.llm_tools.cli rate-images`，得到的多轮决策结果转成 legacy `list[int]` 返回，下游 `select_top_images` 完全无感。详见 `skills/image-rating/SKILL.md`。

### 中文标题生成 (title-cn skill 路径)

仓库内置 `skills/title-cn/`,把 `llm_tools.llm_agent.generate_video_title` 的"英文标题翻译为频道风格中文标题"步骤包装成 JSON-stdout CLI,内部用 **opencode 多轮 reasoning** 替代原 LLM 单次问到底:

1. Round 1:基于英文标题 + abstract 摘要,生成 3-5 个候选中文标题
2. Round 2:自检每个候选——字数 ≤ 20?保留专有名词?有动词?跟历史标题撞车?
3. Round 3:选 top 1,输出最终标题 + 一句话理由

频道风格约束:`<英文专有名词>: <核心动作或卖点>` 句式,≤ 20 字,保留 CamelCase/ALLCAPS 专有名词(ViTacFormer / BESTRO / VistaBot / π0 等),禁止"首次/突破/震撼/颠覆"等夸张宣传词。CLI 内置 `_sanitize_title_for_channel` + `_enforce_max_len` + `_ensure_proper_noun` 三重代码层兜底,即使 LLM 输出违规也会被自动修正。标题清洗(`src/utils/title_cleaner.py`)还会剥离**平台非法可见符号**(尖括号 `<>` / 残缺书名号 `《》` / 零宽·控制字符),避免触发 B站 21009「标题只能包含中文、英文、数字、日文等可见符号」拒稿;上传出口 `orchestrator.upload_generated_content` 再对 `video_title` / `cn_titles` 兜一道 `strip_platform_illegal_chars`,作为 B站/抖音/小红书三平台共用的最后一道防线。

**默认行为不变**:`generate_video_title(text)` 仍走原 LLM 单次。设 `JSR_USE_SKILL_TITLE_CN=1` 后,函数会优先 subprocess 调本 skill;skill 失败时自动 fallback 回原 LLM 单次。返回值始终是 `str` (legacy API 兼容)。

#### 安装本地 skill

```bash
cd ~/Projects/VlogCutter/JushenRenji
npx skills add ./skills/title-cn --yes
```

#### CLI 直接调用

```bash
JSR_NETWORK_PROFILE=gsjts python -m src.llm_tools.cli title-cn \
    --en-title "ViTacFormer: Learning Cross-Modal Representation for Visuo-Tactile Dexterous Manipulation" \
    --abstract "We propose ViTacFormer, a cross-modal transformer that fuses visual and tactile inputs..." \
    --out /tmp/title.json \
    --max-len 20
```

输出 stdout:`{"ok": true, "cn_title": "ViTacFormer: 跨模态学灵巧手", "len": 20}`,`/tmp/title.json` 内含 `cn_title / candidates / reason / char_count` 字段。

#### 在 paper2video 流程中启用 skill 路径

```bash
JSR_USE_SKILL_TITLE_CN=1 JSR_NETWORK_PROFILE=gsjts python src/main.py \
    --paper-link https://arxiv.org/abs/2506.15953 \
    --target-duration 300
```

`paperagent_workflow.py` 调用 `generate_video_title(text, paper_title=..., paper_abstract=...)` 时,环境变量触发后会 subprocess 调 `python -m src.llm_tools.cli title-cn`,得到的多轮决策结果作为 `str` 返回,下游 `cn_titles[0]` 完全无感。详见 `skills/title-cn/SKILL.md`。

---

---

## 运行测试

```bash
conda activate paperagent
python -m pytest tests/ -v
```

---

## 部署前置条件

> 整套代码已经落地, 但首次部署需要用户准备以下凭证 / 一次性人工动作。

### 1. 抖音 cookie (首次扫码登录, 一次性)

容器自包含方案: chromium daemon (Xvfb headed) 在容器里跑, 二维码 PNG 暴露到主机 volume:

```bash
docker compose up -d   # 启动 chromium-daemon + xhs-mcp + paperagent
docker compose run --rm paper-video --first-time-douyin-login
# -> ./cache/douyin_qr.png 出现, 主机用 Preview/任意看图工具打开扫一次
# -> daemon 检测到登录态, 自动写 ./cache/douyin_cookies.json
```

cookie 长期保存在 `cache/`, 之后所有抖音写操作 (上传 / 评论回复) 通过 CDP attach 复用 daemon 的同一 page, 不再触发风控反复吊销。

### 2. `cache/post_ids.json` 视频映射 (评论 / 数据模块需要)

文件格式 (key 用 arxiv_id 或视频 mp4 路径):

```json
{
  "1706.03762": {
    "bilibili": "BV1xxxxxxxxx",
    "xiaohongshu": {"note_id": "...", "xsec_token": "..."},
    "douyin": "https://www.douyin.com/video/<aweme_id>"
  }
}
```

无映射的视频会被自动跳过, 不影响其他平台。

### 3. config.yaml (LLM / TTS / 平台凭证)

```bash
cp config.example.yaml config.yaml
# 填: llm_api_key (DeepSeek), dashscope_api_key (TTS),
#     bilibili_cookies (SESSDATA + bili_jct + buvid3), gemini_api_key (封面)
```

### 4. 飞书多维表格上报 (可选)

需要配置 4 个 env: `LARK_APP_ID` / `LARK_APP_SECRET` / `LARK_BASE_APP_TOKEN` / `LARK_BASE_TABLE_ID`, 然后填充 `src/distribution/analytics/lark_uploader.py:push_to_lark`。不配的话, sqlite + CLI summary 已足够本地查看数据。

### 5. Docker 镜像首次 build

```bash
docker compose --profile default build   # 15-30 分钟, 镜像 6-8 GB (含 cuda runtime + chromium)
```

之后 `docker compose run --rm paper-video --paper-link <arxiv-url> --manim --manim-tts` 一键全自动跑通论文 → 视频 → 三平台分发。

### 抖音开放平台 OpenAPI (长期方案)

cookie 路线即使有 chromium daemon, 抖音风控仍可能吊销。一劳永逸的方案是申请抖音开放平台企业开发者资质 (个体户营业执照即可, 1-3 工作日)，拿到 `item.comment` / `video.create` scope 后改走 OAuth refresh token, 永不过期。
---

## 贡献

欢迎任何形式的贡献！请提交问题或拉取请求。

## 许可证

该项目遵循MIT许可证。


## 小红书图文卡片模式

小红书渠道改为**只发图文卡片笔记**(视频仅 B站/抖音)。卡片与视频同源:每张图文卡 = 该 scene 的 manim 合成画面(尾帧,含公式/方法图/结果图)+ 对应旁白文字,3:4 竖版。

**用法**:`--platforms` 含 `xiaohongshu` 时自动走卡片图文发布;单独渲染卡片可用:

```bash
python -m src.distribution.xhs_cards --meta "output/<名>_meta.json" \
  --cover "output/<名>.png" --out output/xhs_cards
```

依赖:xhs-mcp 容器(`docker start xhs-mcp`,端口 18060,cookie 寿命约 3 天需 `check_login_status` 复核)。
