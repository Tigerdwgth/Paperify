"""测试 orchestrator.upload_generated_content 的 tags_per_platform 路由。

仅 mock B站 / 小红书的 upload 实现(小红书还要 mock 掉 chromium 卡片渲染),
不真的调上传, 验证 tag 透传逻辑。
"""
import os
import tempfile
from unittest.mock import patch

FAKE_CARDS = ["/tmp/fake_card_00.png"]


def _make_video_file():
    """临时视频文件占位（orchestrator 会校验存在性）"""
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(b"fake mp4 content")
    return path


def test_bilibili_tags_per_platform_takes_priority():
    """tags_per_platform.bilibili 应覆盖 video_tags 字符串"""
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_bilibili_impl",
                   return_value="BV1xxx") as mock_bili:
            from src.distribution.orchestrator import upload_generated_content
            upload_generated_content(
                platforms=["bilibili"],
                video_path=video, cover_path=None,
                video_title="标题", video_tags="老,关键词", video_desc="desc",
                tags_per_platform={"bilibili": ["VLA", "具身智能", "机器人"]},
            )
            assert mock_bili.call_args.kwargs["tags"] == ["VLA", "具身智能", "机器人"]
    finally:
        os.unlink(video)


def test_bilibili_falls_back_to_video_tags_when_no_tpp():
    """tags_per_platform 缺失时使用老 video_tags"""
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_bilibili_impl",
                   return_value="BV1xxx") as mock_bili:
            from src.distribution.orchestrator import upload_generated_content
            upload_generated_content(
                platforms=["bilibili"],
                video_path=video, cover_path=None,
                video_title="标题", video_tags="VLA,具身智能", video_desc="desc",
            )
            assert mock_bili.call_args.kwargs["tags"] == "VLA,具身智能"
    finally:
        os.unlink(video)


def test_xhs_tags_per_platform_takes_priority():
    """小红书 tags_per_platform.xiaohongshu 优先于 xhs_tags + 兜底。

    小红书只发图文卡片, 标签落在 publish_note 上。
    """
    video = _make_video_file()
    try:
        with patch("src.distribution.xhs_cards.render_note_cards",
                   return_value=FAKE_CARDS), \
             patch("src.distribution.orchestrator._upload_xiaohongshu_note_impl",
                   return_value={"note_id": "n1"}) as mock_xhs:
            from src.distribution.orchestrator import upload_generated_content
            upload_generated_content(
                platforms=["xiaohongshu"],
                video_path=video, cover_path=None,
                video_title="标题", video_tags="x", video_desc="desc",
                xhs_tags=["这条应被覆盖"],
                tags_per_platform={"xiaohongshu": ["VLA", "具身智能", "AI论文笔记"]},
            )
            tags = mock_xhs.call_args.kwargs["tags"]
            # 老 xhs_tags 被合并进来在末尾，但前面是 tags_per_platform
            assert tags[0] == "VLA"
            assert "具身智能" in tags
            assert "AI论文笔记" in tags
            # 老 xhs_tags 也应保留在末尾
            assert "这条应被覆盖" in tags
    finally:
        os.unlink(video)


def test_xhs_uses_fallback_when_nothing_supplied():
    """tags_per_platform 和 xhs_tags 都没传时使用 XHS_FALLBACK_TAGS"""
    video = _make_video_file()
    try:
        with patch("src.distribution.xhs_cards.render_note_cards",
                   return_value=FAKE_CARDS), \
             patch("src.distribution.orchestrator._upload_xiaohongshu_note_impl",
                   return_value={"note_id": "n1"}) as mock_xhs:
            from src.distribution.orchestrator import (
                upload_generated_content, XHS_FALLBACK_TAGS,
            )
            upload_generated_content(
                platforms=["xiaohongshu"],
                video_path=video, cover_path=None,
                video_title="标题", video_tags="x", video_desc="desc",
            )
            tags = mock_xhs.call_args.kwargs["tags"]
            assert tags == list(XHS_FALLBACK_TAGS)
    finally:
        os.unlink(video)


def test_signature_has_tags_per_platform():
    """签名应包含 tags_per_platform=None 默认值"""
    import inspect
    from src.distribution.orchestrator import upload_generated_content
    sig = inspect.signature(upload_generated_content)
    assert "tags_per_platform" in sig.parameters
    assert sig.parameters["tags_per_platform"].default is None
