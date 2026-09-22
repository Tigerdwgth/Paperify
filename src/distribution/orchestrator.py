import glob
import logging
import os
from typing import Dict, List, Optional

try:
    from .bilibili import upload as _upload_bilibili_impl
    _BILIBILI_IMPORT_ERROR = None
except Exception as exc:
    _upload_bilibili_impl = None
    _BILIBILI_IMPORT_ERROR = exc

try:
    from .xiaohongshu import publish_note as _upload_xiaohongshu_note_impl
    from .xiaohongshu import publish_video as _upload_xiaohongshu_video_impl
    _XHS_IMPORT_ERROR = None
except Exception as exc:
    _upload_xiaohongshu_note_impl = None
    _upload_xiaohongshu_video_impl = None
    _XHS_IMPORT_ERROR = exc

try:
    from .douyin import upload as _upload_douyin_impl
    _DOUYIN_IMPORT_ERROR = None
except Exception as exc:
    _upload_douyin_impl = None
    _DOUYIN_IMPORT_ERROR = exc

logger = logging.getLogger(__name__)

DEFAULT_PLATFORMS = ["bilibili", "xiaohongshu"]
# 抖音不在默认平台里 - 需要 cookie + SAU venv, 调用方主动指定 --platforms 才启用
VALID_PLATFORMS = set(DEFAULT_PLATFORMS) | {"douyin"}

# 小红书 fallback 标签：仅在动态生成 + 显式 xhs_tags 都没有时才用
XHS_FALLBACK_TAGS = ["具身智能", "AI论文笔记", "前沿科技"]
# 兼容老调用：保留旧符号（指向 fallback）
XHS_DEFAULT_TAGS = XHS_FALLBACK_TAGS
XHS_CONTENT_LIMIT = 300
BILIBILI_DESC_LIMIT = 250
XHS_SUMMARY_LIMIT = 90
BILIBILI_SUMMARY_LIMIT = 60


def upload_bilibili(*args, **kwargs):
    if _upload_bilibili_impl is None:
        raise RuntimeError("B站上传依赖未安装") from _BILIBILI_IMPORT_ERROR
    return _upload_bilibili_impl(*args, **kwargs)


def upload_xiaohongshu_note(*args, **kwargs):
    if _upload_xiaohongshu_note_impl is None:
        raise RuntimeError("小红书上传依赖未安装") from _XHS_IMPORT_ERROR
    return _upload_xiaohongshu_note_impl(*args, **kwargs)


def upload_xiaohongshu_video(*args, **kwargs):
    if _upload_xiaohongshu_video_impl is None:
        raise RuntimeError("小红书上传依赖未安装") from _XHS_IMPORT_ERROR
    return _upload_xiaohongshu_video_impl(*args, **kwargs)


def upload_douyin(*args, **kwargs):
    if _upload_douyin_impl is None:
        raise RuntimeError("抖音上传依赖未安装") from _DOUYIN_IMPORT_ERROR
    return _upload_douyin_impl(*args, **kwargs)


def parse_platforms(raw_platforms: Optional[str]) -> List[str]:
    """Parse `--platforms` argument into known platform identifiers."""
    if raw_platforms is None:
        return DEFAULT_PLATFORMS.copy()

    value = raw_platforms.strip().lower()
    if not value or value == "none":
        return []

    results: List[str] = []
    for item in value.split(","):
        platform = item.strip()
        if not platform or platform == "none":
            continue
        if platform in VALID_PLATFORMS:
            if platform not in results:
                results.append(platform)
        else:
            logger.warning("忽略未知平台: %s", platform)
    return results


def _build_xhs_title(video_title: str, cn_titles: Optional[List[str]]) -> str:
    if cn_titles:
        return cn_titles[0][:20]
    if video_title:
        return video_title[:20]
    return "Arxiv论文速览"


def _clean_text(value: Optional[str]) -> str:
    return (value or "").strip()


def _truncate_text(value: str, max_len: int) -> str:
    text = _clean_text(value)
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len == 1:
        return text[:1]
    return text[: max_len - 1].rstrip() + "…"


def _append_line_with_limit(lines: List[str], line: str, total_limit: int) -> bool:
    if not line:
        return True
    candidate = "\n".join(lines + [line]).strip()
    if len(candidate) <= total_limit:
        lines.append(line)
        return True
    return False


def _build_compact_description(
    *,
    video_desc: str,
    cn_titles: Optional[List[str]],
    origin_titles: Optional[List[str]],
    paper_links: Optional[List[str]],
    project_links: Optional[List[str]],
    summaries: Optional[List[str]],
    total_limit: int,
    summary_limit: int,
    include_cn_titles: bool = False,
    # 默认 False 是 fail-safe: 外链会触发平台限流(2026-05-29 B站简介带链接被限流),
    # 新增平台的 builder 漏传这个参数时应当"不带链接", 而不是默默把链接发出去。
    # 要链接的调用方显式传 include_links=True。
    include_links: bool = False,
) -> str:
    lines: List[str] = []
    num_papers = max(
        len(cn_titles or []),
        len(origin_titles or []),
        len(paper_links or []),
        len(project_links or []),
        len(summaries or []),
    )

    for i in range(num_papers):
        cn_title = _clean_text(cn_titles[i]) if cn_titles and i < len(cn_titles) else ""
        title = _clean_text(origin_titles[i]) if origin_titles and i < len(origin_titles) else ""
        paper_link = _clean_text(paper_links[i]) if paper_links and i < len(paper_links) else ""
        project_link = _clean_text(project_links[i]) if project_links and i < len(project_links) else ""
        summary = _clean_text(summaries[i]) if summaries and i < len(summaries) else ""

        for line in (
            f"中文标题：{cn_title}" if include_cn_titles and cn_title else "",
            f"论文标题：{title}" if title else "",
            f"论文链接：{paper_link}" if include_links and paper_link else "",
            f"项目链接：{project_link}" if include_links and project_link else "",
        ):
            if not _append_line_with_limit(lines, line, total_limit):
                return "\n".join(lines).strip()

        if summary:
            existing = "\n".join(lines).strip()
            remaining = total_limit - len(existing) - (1 if existing else 0) - len("摘要：")
            summary_text = _truncate_text(summary, min(summary_limit, remaining))
            if summary_text and not _append_line_with_limit(lines, f"摘要：{summary_text}", total_limit):
                return "\n".join(lines).strip()

    content = "\n".join(lines).strip()
    if content:
        return content
    return _truncate_text(video_desc, total_limit)


def _build_xhs_content(
    video_desc: str,
    cn_titles: Optional[List[str]],
    origin_titles: Optional[List[str]],
    summaries: Optional[List[str]] = None,
    paper_links: Optional[List[str]] = None,
    project_links: Optional[List[str]] = None,
) -> str:
    """构建小红书文案：仅保留论文名+摘要，**不含任何链接**（小红书简介带链接会被限流）。"""
    return _build_compact_description(
        video_desc=video_desc,
        cn_titles=cn_titles,
        origin_titles=origin_titles,
        paper_links=paper_links,
        project_links=project_links,
        summaries=summaries,
        total_limit=XHS_CONTENT_LIMIT,
        summary_limit=XHS_SUMMARY_LIMIT,
        include_cn_titles=True,
        include_links=False,
    )


def _build_bilibili_desc(
    video_desc: str,
    cn_titles: Optional[List[str]],
    origin_titles: Optional[List[str]],
    summaries: Optional[List[str]] = None,
    paper_links: Optional[List[str]] = None,
    project_links: Optional[List[str]] = None,
) -> str:
    """构建B站视频简介：仅保留论文名+摘要，**不含任何链接**（B站简介带链接会被限流）。"""
    return _build_compact_description(
        video_desc=video_desc,
        cn_titles=cn_titles,
        origin_titles=origin_titles,
        paper_links=paper_links,
        project_links=project_links,
        summaries=summaries,
        total_limit=BILIBILI_DESC_LIMIT,
        summary_limit=BILIBILI_SUMMARY_LIMIT,
        include_links=False,
    )


def _collect_xhs_images(cover_path: Optional[str], max_images: int = 8) -> List[str]:
    images: List[str] = []
    seen = set()

    def add_if_exists(path: str) -> None:
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path) and abs_path not in seen:
            images.append(abs_path)
            seen.add(abs_path)

    if cover_path:
        add_if_exists(cover_path)

    for path in sorted(glob.glob("./pic/*.png")):
        add_if_exists(path)
        if len(images) >= max_images:
            break

    return images


def upload_generated_content(
    platforms: List[str],
    video_path: str,
    cover_path: Optional[str],
    video_title: str,
    video_tags: str,
    video_desc: str,
    cn_titles: Optional[List[str]] = None,
    origin_titles: Optional[List[str]] = None,
    summaries: Optional[List[str]] = None,
    paper_links: Optional[List[str]] = None,
    project_links: Optional[List[str]] = None,
    bilibili_tid: int = 188,
    xhs_tags: Optional[List[str]] = None,
    tags_per_platform: Optional[Dict[str, List[str]]] = None,
    narration_segments: Optional[List[str]] = None,
    scene_frames: Optional[List[Optional[str]]] = None,
) -> Dict[str, Dict[str, object]]:
    """
    Upload generated assets to selected platforms.

    Returns a per-platform result summary without raising on single-platform failures.
    """
    results: Dict[str, Dict[str, object]] = {}

    # 上传出口最后一道防线: 剥离标题非法可见符号(尖括号/残缺书名号/控制字符),
    # 防 LLM 生成的坏标题触发 B站 21009 拒稿(历史坑, 见 tests/test_title_cleaner.py)。
    try:
        from src.utils.title_cleaner import strip_platform_illegal_chars as _strip_illegal
        if video_title:
            video_title = _strip_illegal(video_title) or video_title
        if cn_titles:
            cn_titles = [(_strip_illegal(t) or t) if t else t for t in cn_titles]
    except Exception as _e:  # noqa: BLE001
        logger.warning("标题非法符号剥离失败, 用原标题: %s", _e)

    if "bilibili" in platforms:
        try:
            if not video_path or not os.path.exists(video_path):
                raise FileNotFoundError(f"视频文件不存在: {video_path}")
            bili_desc = _build_bilibili_desc(
                video_desc=video_desc,
                cn_titles=cn_titles,
                origin_titles=origin_titles,
                summaries=summaries,
                paper_links=paper_links,
                project_links=project_links,
            )
            bili_tags_used = video_tags
            if tags_per_platform and tags_per_platform.get("bilibili"):
                bili_tags_used = tags_per_platform["bilibili"]
            bv_id = upload_bilibili(
                video_path=video_path,
                title=video_title,
                tags=bili_tags_used,
                desc=bili_desc,
                cover_path=cover_path,
                tid=bilibili_tid,
            )
            if bv_id:
                results["bilibili"] = {"ok": True, "id": bv_id}
            else:
                results["bilibili"] = {"ok": False, "error": "上传返回空结果"}
        except Exception as exc:
            logger.exception("B站上传失败")
            results["bilibili"] = {"ok": False, "error": str(exc)}

    if "xiaohongshu" in platforms:
        xhs_title = _build_xhs_title(video_title=video_title, cn_titles=cn_titles)
        xhs_content = _build_xhs_content(
            video_desc=video_desc,
            cn_titles=cn_titles,
            origin_titles=origin_titles,
            summaries=summaries,
            paper_links=paper_links,
            project_links=project_links,
        )
        # 关键词优先级: tags_per_platform.xiaohongshu > xhs_tags > XHS_FALLBACK_TAGS
        if tags_per_platform and tags_per_platform.get("xiaohongshu"):
            final_tags = list(tags_per_platform["xiaohongshu"])
            # 老调用方传的 xhs_tags 也合并进去（去重保序）
            if xhs_tags:
                for t in xhs_tags:
                    if t not in final_tags:
                        final_tags.append(t)
        elif xhs_tags:
            final_tags = list(xhs_tags)
        else:
            final_tags = list(XHS_FALLBACK_TAGS)

        # 小红书只发图文卡片(标题/旁白文字渲染进图), 不再上传视频。
        # publish_video 保留在 xiaohongshu.py 以兼容旧调用方, 此处不再路由。
        if True:
            try:
                images: List[str] = []
                try:
                    from .xhs_cards import render_note_cards
                    paper_imgs = sorted(glob.glob("./pic/*.png"))[:2]
                    card_title = cn_titles[0] if cn_titles else xhs_title
                    images = render_note_cards(
                        title=card_title,
                        narration_segments=narration_segments,
                        summaries=summaries,
                        cover_path=cover_path,
                        paper_images=paper_imgs,
                        scene_frames=scene_frames,
                    )
                except Exception as card_exc:  # noqa: BLE001
                    logger.warning("小红书图文卡片渲染失败, 降级封面+论文图: %s", card_exc)

                if not images:
                    images = _collect_xhs_images(cover_path)
                if not images:
                    raise FileNotFoundError("小红书上传缺少可用图片（卡片渲染失败且封面与 ./pic/*.png 均不存在）")

                publish_result = upload_xiaohongshu_note(
                    title=xhs_title,
                    content=xhs_content,
                    images=images,
                    tags=final_tags,
                )
                if publish_result:
                    note_id = publish_result.get("note_id") if isinstance(publish_result, dict) else None
                    results["xiaohongshu"] = {"ok": True, "id": note_id, "type": "note"}
                else:
                    results["xiaohongshu"] = {"ok": False, "error": "发布返回空结果"}
            except Exception as exc:
                logger.exception("小红书图文上传失败")
                results["xiaohongshu"] = {"ok": False, "error": str(exc)}

    if "douyin" in platforms:
        try:
            if not video_path or not os.path.exists(video_path):
                raise FileNotFoundError(f"视频文件不存在: {video_path}")
            # 关键词路由: tags_per_platform.douyin > 兜底转换 video_tags
            if tags_per_platform and tags_per_platform.get("douyin"):
                dy_tags = list(tags_per_platform["douyin"])
            else:
                # 抖音 fallback: 从 video_tags(B站逗号串)取前 5 个
                if isinstance(video_tags, str):
                    dy_tags = [t.strip() for t in video_tags.split(",") if t.strip()][:5]
                elif isinstance(video_tags, (list, tuple)):
                    dy_tags = list(video_tags)[:5]
                else:
                    dy_tags = []
            # 抖音标题上限约 30 字符, 这里软裁剪保险
            dy_title = (video_title or "")[:30]
            ret = upload_douyin(
                video_path=video_path,
                title=dy_title,
                tags=dy_tags,
                cover_path=cover_path,
                desc=video_desc,
            )
            if ret:
                results["douyin"] = {"ok": True, "id": ret}
            else:
                results["douyin"] = {"ok": False, "error": "上传返回空结果"}
        except Exception as exc:
            logger.exception("抖音上传失败")
            results["douyin"] = {"ok": False, "error": str(exc)}

    return results
