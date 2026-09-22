# CLAUDE.md
本文件为 Claude Code (claude.ai/code) 在此仓库中工作时提供指导。

# 全局规则

- 全程使用中文交流
- 遇到不知道的接口必须要上网搜索或者问我
- 每次更新代码也要更新readme
- 每次更新代码要创建对应的单元测试代码
- 所有prompt 必须要更新在./src/llm_tools/prompts.py 便于随时更改
- mcp使用node v
## 项目概述

论文转视频流水线：从 arXiv 或本地 PDF 提取文本和图片，使用 LLM 生成脚本，通过 DashScope TTS 合成语音，最后使用 moviepy 合成视频。支持上传到 Bilibili 等平台。

## 常用命令

```bash
# 环境搭建
conda create -n paperagent python=3.10
conda activate paperagent
pip install -r requirements.txt
pip install -e .

# 单篇论文转视频
python src/main.py --filename "{论文名或arxiv查询}"

# 生成每日 arXiv 论文总结视频
python src/main.py --filename "cs.RO" --output_language "zh"

# 完整选项
python src/main.py --filename "{query}" --video_length "long" --output_language "zh"

# 控制视频时长（默认300秒）
python src/main.py --filename "{query}" --target_duration 180 --platforms none
```

**前置要求：**
- 安装 [tesseract-ocr](https://tesseract-ocr.github.io/tessdoc/Installation.html)（OCR 功能需要）
- 在 `config.yaml` 中配置 API 密钥 (`llm_api_key`, `dashscope_api_key`)

## 架构

```
src/
├── main.py                 # CLI 入口；解析参数，调用 generate_daily_arxiv_summary
├── paperagent_workflow.py  # 核心流水线编排
│   ├── run_pdf_to_video_pipeline()    # 单篇论文 -> 视频
│   ├── generate_daily_arxiv_summary() # 多篇论文 -> 合并视频
│   └── VideoCreator 类    # moviepy + DashScope TTS 视频合成
├── pdf_processor.py        # PyMuPDF/fitz 封装，提取 PDF 文本和图片
├── llm_tools/
│   ├── llm_agent.py       # LLM 客户端 (qwen/deepseek)；`create_chat_completion()`
│   └── prompts.py         # 提示词字典，key 为函数名（如 generate_summary）
├── distribution/
│   ├── orchestrator.py    # 多平台上传编排；upload_generated_content()
│   ├── bilibili.py        # B站视频上传（使用 cookies）
│   ├── douyin.py          # 抖音上传（chromium daemon / SAU）
│   ├── xiaohongshu.py     # 小红书发布
│   ├── xhs_cards.py       # 小红书图文卡片渲染
│   └── f2_client.py       # 抖音数据读取客户端
├── video_creator.py       # 旧版视频创建（可能已弃用）
├── config.py              # 加载 config.yaml；导出 CACHE_DIR、FONT_PATH 等
└── utils/
    └── audio_helpers.py    # TTS/音频处理工具
```

### 数据流程
1. `main.py` → `paperagent_workflow.generate_daily_arxiv_summary()`
2. 从 arXiv 获取论文 → 通过 `get_arxiv_latest.py` 下载 PDF
3. `PDFProcessor` 提取文本和图片（存入 `./pic`）
4. `ImageAgent` (Qwen-VL) 解释图片；结果缓存到 `./cache/image_explanations.json`
5. `llm_agent` 调用 LLM 生成摘要和标题
6. `VideoCreator` 合并图片 + DashScope TTS 音频 → moviepy 视频
7. `distribution/orchestrator.upload_generated_content()` 分发到 B站/小红书/抖音

## 关键模式

### 提示词组织
- 在 `src/llm_tools/prompts.py` 中添加提示词，key 为函数名（如 `"generate_summary"`）
- LLM 包装函数（如 `generate_summary()`）使用 `inspect.currentframe().f_code.co_name` 查找对应提示词
- 语言后缀根据 `OUTPUT_LANGUAGE` 配置自动追加

### 图片提取
- `llm_agent.py` 中的 `MANUALLY_EXTRACT_IMAGES` 控制：
  - `False`（默认）：从 PDF 提取图片到 `./pic/`
  - `True`：从 `./pic/` 加载预先准备好的图片

### 配置
- `config.yaml`（根目录）：API 密钥、路径 (`cache_dir`, `pic_dir`, `output_dir`)、`font_path`、`output_language`
- `src/config.py`：加载 YAML；导出大写全局变量（如 `FONT_PATH`, `DASHSCOPE_API_KEY`）

### 日志
- 所有日志写入根目录的 `app.log`
- 中间产物查看 `./cache/`（图片解释 JSON、缓存 PDF）
- 生成视频在 `./output/`，图片在 `./pic/`

## 外部依赖

| 服务 | 用途 | 配置项 |
|------|------|--------|
| DashScope | TTS (cosyvoice-v1, voice=longxiaochun) | `dashscope_api_key` |
| DeepSeek/Qwen | LLM 生成摘要/标题 | `llm_api_key` |
| Bilibili | 视频上传 | `bilibili_cookies_file` |
| tesseract-ocr | OCR 备选方案 | `tessdata_prefix` |
| Gemini | 封面图片生成（通过 Tailscale la exit node） | `gemini_api_key` |

## 重要说明

- **自动化测试** - `python -m pytest tests/` 跑全量单测（约 50 秒跑完）。端到端验证仍用 `python src/main.py --filename "/path/to/sample.pdf"`
- **测试默认碰不到真实出口**（`tests/conftest.py` 的 autouse 防线）：非 localhost 的网络请求、B站/小红书/抖音的真实上传实现、小红书卡片的 chromium 渲染，一律被换成抛 `BlockedRealCall` 的桩。历史原因见 LESSONS（实现改了调用路由后，老测试 mock 的函数不在路径上了，于是真的往线上发了笔记）。写新测试时如果撞上 `BlockedRealCall`，**默认答案是把那个调用 mock 掉**，而不是加 marker 放行。确实要打真实链路才用：
  | marker | 放行什么 | 默认行为 |
  |---|---|---|
  | `smoke` | 全部（网络 + 发布 + 浏览器） | 默认 skip，`-m smoke` 才跑 |
  | `allow_network` | 真实外网出口 | 照常执行 |
  | `allow_publish` | 真实发布/上传实现 | 照常执行 |
  | `allow_browser` | 真实 chromium 渲染 | 照常执行 |
- `config.yaml` 中的 API 密钥应移至环境变量（共享仓库时）



## 封面生成（Gemini + Tailscale Exit Node）

封面使用 **Gemini 图片生成 API** 生成可爱漫画风封面，标题直接由 AI 在画面中渲染。

### 访问 Google API 的网络方案

服务器在国内无法直连 Google API。解决方案是通过 Tailscale exit node `la`（美国，100.103.134.38）转发流量：

- `generate_cover.py` 会自动在调用 Gemini 前开启 exit node，调用完毕后关闭
- jdh 用户已配置 `sudo tailscale` 免密码（`/etc/sudoers.d/jdh-tailscale`）
- 如果 la 不可用，自动回退到 DashScope qwen-image

### 配置

`config.yaml` 中需要配置（该文件已在 .gitignore 中，不会推送）：
```yaml
gemini_api_key: <your_google_api_key>
```

`config.py` 中读取为 `GEMINI_API_KEY`。

### 封面生成优先级

1. **Gemini**（gemini-2.5-flash-image）— 通过 Tailscale la exit node 访问
2. **DashScope**（qwen-image）— 回退方案
3. **原始图片** — 最终兜底

### 手动测试

```bash
cd ~/Projects/VlogCutter/JushenRenji/src
python3 generate_cover.py
# 输出到 ./output/cover_test.png
```
## 网络代理注意事项

运行本项目时**必须先取消代理设置**，否则 arXiv 等外部网站连接会被重置：

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
```

服务器可以直接访问 arXiv，设置代理反而会导致 `ConnectionResetError`。

## 抖音补传 / 重新登录（douyin republish）

抖音 cookie: `cache/douyin_cookies.json`（含 sessionid_ss/sessionid/sid_guard）。**抖音对服务器自动登录风控极严**（CDP 自动填手机号/点「获取验证码」完全无效、不发短信），cookie 过期才需重登。

**① 重新登录（仅 cookie 失效时，要人工）**
- 起持久 daemon：`python -m src.chromium_daemon`（自动起 Xvfb :99 + CDP 9222，登录后自动写 cookie）。Xvfb 必须 `setsid Xvfb :99 ...` 持久化，`DISPLAY=:99 xdpyinfo` 验证（pgrep 会假阳性）。
- chromium 缺版本报 `Executable doesn't exist .../chromium-1223` → `HTTPS_PROXY=http://127.0.0.1:7890 patchright install chromium`（下载走 clash，登录/上传走直连）。
- 开 VNC：`x11vnc -storepasswd <pw> ~/.vnc/passwd` + `setsid x11vnc -display :99 -rfbport 5900 -rfbauth ~/.vnc/passwd -forever -shared -bg`。
- 帮用户开远程桌面（密码嵌 URL）：`ssh macair 'open "vnc://:<pw>@100.86.193.52:5900"'` → **用户在真桌面里人工登录**（真人操作不吃风控，发码/扫码均可）→ daemon 自动写 cookie。登完 `pkill x11vnc`。

**② 补传上传（cookie 有效时，几分钟，无需重登）**
- `DISPLAY=:99 JSR_USE_CDP_DAEMON=1 NO_PROXY=douyin.com python tmp/dy_republish.py --base "<output 文件名不含扩展名>" --tags "逗号分隔标签"`
- **必须 `JSR_USE_CDP_DAEMON=1`** 复用登录好的 daemon，否则默认 launch 新浏览器会 `goto content/upload load 超时`。
- daemon 没在跑就先按 ① 起 daemon（cookie 有效会直接 ready，不用 VNC）。

**③ 判定成功看作品管理列表，别信脚本返回**：SAU 常报「等待发布跳转超时 / UPLOAD_RESULT: None」**假失败**，实际已发布。用 `python tmp/dy_manage_shot.py` 截图 + present 检测确认作品管理置顶（状态「审核中」正常）。卡话题标签点击时用 `tmp/douyin_publish_step.py` 收尾（AI声明+发布）。

> 完整排错过程见 `LESSONS_LEARNED.md`「抖音补传完整流程」节。账号绑定手机 18500252035。
