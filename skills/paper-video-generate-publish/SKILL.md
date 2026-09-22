---
name: paper-video-generate-publish
description: Use when working in the JushenRenji repo and the user asks to update the latest paper video, generate a paper explainer by title, publish or republish to Bilibili/Xiaohongshu, update Bilibili cookies, or verify upload results.
---

# Paper Video Generate And Publish

## Overview

This skill is for the `JushenRenji` repo's daily paper workflow: generate a single-paper video, publish it to `bilibili` and `xiaohongshu`, and handle common retry paths.

Follow the repo defaults unless the user explicitly overrides them:
- whole conversation in Chinese
- do not keep asking for confirmation
- default platforms are `bilibili,xiaohongshu`
- use the auto-generated title, description, tags, and cover

## When to Use

Use this skill when the user asks for any of the following inside this repo:
- “更新最新视频” or “更新这篇论文”
- “发到 B站 和 小红书” or “补发 B站”
- “更新 cookie” and retry Bilibili upload
- “看下效果 / 核验结果 / 有没有被截断”

Do not use this skill for generic code changes unrelated to the paper video pipeline.

## Project Facts You Must Remember

- Main entry point: `src/main.py`
- Default upload platforms are `bilibili,xiaohongshu`
- Monday rolls back to last Friday for arXiv date selection; Saturday/Sunday also roll back to Friday logic in `src/main.py`
- Compact platform descriptions are built in `src/distribution/orchestrator.py`
- Description rules already implemented:
  - include paper title
  - include paper link
  - include project link only when it actually exists
  - do not invent a project link
- Single-paper outputs are renamed to `output/YYYY-MM-DD_<中文标题>.mp4` and matching `.png`

## Standard Workflow

### 1. Generate and publish a paper by title

Use this command template:

```bash
cd /home/jdh/Projects/VlogCutter/JushenRenji
source ~/miniconda3/etc/profile.d/conda.sh
conda activate paperagent
mkdir -p tmp
PYTHONUNBUFFERED=1 PYTHONPATH=src TTS_MAX_WORKERS=1 TTS_SERIAL_RETRY_ATTEMPTS=2 \
python src/main.py --filename '<论文标题>' 2>&1 | tee tmp/<slug>_publish_<YYYYMMDD>.log
```

Notes:
- Keep `--filename` equal to the user-given paper title unless they ask for another query.
- Do not ask whether to publish unless the user explicitly asks to stop publishing.
- If the user asks for a newer or longer version, let the render finish; do not switch to a fast path unless they ask.

### 2. Verify after generation

Always verify with commands instead of trusting logs casually:

```bash
ls -lt output | head -20
ffprobe -v error -show_entries format=duration,size -of json '<video_path>'
tail -n 120 tmp/<slug>_publish_<YYYYMMDD>.log
```

Report at least:
- output video path
- cover path
- duration
- paper title
- paper link
- Xiaohongshu result
- Bilibili result

If Bilibili failed, quote the concrete error such as `code=-101, 账号未登录`.

## Bilibili Retry SOP

### 1. Update cookies in `config.yaml`

The current code reads only these fields from `bilibili_cookies`:
- `sessdata`
- `bili_jct`
- `dedeuserid`
- `dedeuserid_ckmd5`

Ignore response-header noise such as `set-cookie`, `Path`, `Domain`, `Expires`, and `SameSite`.

### 2. Retry only Bilibili without regenerating video

Use `src.distribution.orchestrator.upload_generated_content(...)` against the existing output video.

Template:

```bash
cd /home/jdh/Projects/VlogCutter/JushenRenji
source ~/miniconda3/etc/profile.d/conda.sh
conda activate paperagent
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 - <<'PY' 2>&1 | tee tmp/<slug>_bili_retry_<YYYYMMDD>.log
from src.distribution.orchestrator import upload_generated_content

result = upload_generated_content(
    platforms=['bilibili'],
    video_path='output/<video>.mp4',
    cover_path='output/<video>.png',
    video_title='<中文标题>',
    video_tags='人工智能,具身智能,机器人,模仿学习,强化学习,自动驾驶,具身人机',
    video_desc='<英文标题>',
    cn_titles=['<中文标题>'],
    origin_titles=['<英文标题>'],
    summaries=None,
    paper_links=['<论文链接>'],
    project_links=[''],
    bilibili_tid=188,
)
print(result)
PY
```

Then verify:

```bash
tail -n 80 tmp/<slug>_bili_retry_<YYYYMMDD>.log
```

Success means the submit result contains `code: 0` and a `bvid`.

## Description Rules

When retrying or checking content, the effective Bilibili/Xiaohongshu compact description should stay short and follow this order:
- `论文标题：...`
- `论文链接：...`
- `项目链接：...` only if non-empty
- optional summary only if available and still within limit

For Bilibili the total limit is 250 characters.
For Xiaohongshu the total limit is 300 characters.

## Xiaohongshu Notes

- The repo uses the MCP-based uploader in `src/distribution/xiaohongshu.py`
- Docker service is auto-started when possible
- First-time login can require:

```bash
python -m src.distribution.xiaohongshu --login
```

- Current MCP video publish path may ignore `cover_path`; tell the user to adjust cover in the app if needed

## Common Pitfalls

- Do not claim success before checking the final upload result in the log
- Do not say Bilibili succeeded if the returned result is empty
- Do not invent project links from organization, dataset, or demo homepages
- Do not regenerate the full video when the task is only “补发 B站”
- Do not forget that Monday's query date is the previous Friday
- **Never batch republish without first verifying per-paper × per-platform last status.** Before any backfill or retry (especially right after fixing an underlying bug like an xhs-mcp image upgrade, douyin cookie reset, or bilibili cookie refresh), grep historic upload logs (`output/*_run*.log`, `output/*_upload*.log`, `output/*_retry*.log`, `tmp/*publish*.log`) and skip the (paper, platform) pairs that already returned `ok=True` / a valid `BVxxx`. Only enqueue the actual failures (`ok=False`, "发布返回空结果", `HTTP 请求失败`). Xiaohongshu and Bilibili do not dedupe server-side, so blind republish creates duplicate notes that pollute the account.

## Fast Checklist

1. Run the generation command or the single-platform retry command.
2. Wait for the process to finish fully.
3. Verify output file and duration with `ffprobe`.
4. Verify upload result from the log tail.
5. Reply with concrete paths, duration, paper link, and per-platform status.
