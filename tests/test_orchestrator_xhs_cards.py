"""测试 orchestrator 小红书图文卡片路由(视频不再上传)。"""
import os
import tempfile
from unittest.mock import patch

from src.distribution import orchestrator as orch


def _make_video():
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(b"fake mp4")
    return path


def _call(video, **extra):
    return orch.upload_generated_content(
        platforms=["xiaohongshu"],
        video_path=video, cover_path=None,
        video_title="测试标题", video_tags="a,b",
        video_desc="desc",
        cn_titles=["测试中文标题"],
        summaries=["摘要文本。"],
        **extra,
    )


def test_xhs_uses_note_cards_not_video():
    """卡片渲染成功 → publish_note 收到卡片图; 视频接口不被调用。"""
    video = _make_video()
    fake_cards = ["/tmp/card_00.png", "/tmp/card_01.png"]
    try:
        with patch("src.distribution.xhs_cards.render_note_cards",
                   return_value=fake_cards) as m_cards, \
             patch.object(orch, "_upload_xiaohongshu_note_impl",
                          return_value={"note_id": "n1"}) as m_note, \
             patch.object(orch, "_upload_xiaohongshu_video_impl") as m_video:
            res = _call(video, narration_segments=["旁白一"],
                        scene_frames=["/tmp/f0.png"])
        assert res["xiaohongshu"]["ok"] is True
        assert res["xiaohongshu"]["type"] == "note"
        assert m_note.call_args.kwargs["images"] == fake_cards
        # narration/scene_frames 正确传入卡片渲染
        assert m_cards.call_args.kwargs["narration_segments"] == ["旁白一"]
        assert m_cards.call_args.kwargs["scene_frames"] == ["/tmp/f0.png"]
        m_video.assert_not_called()
    finally:
        os.unlink(video)


def test_xhs_cards_fail_falls_back_to_collect_images():
    """卡片渲染抛异常 → 降级 _collect_xhs_images, 仍发图文。"""
    video = _make_video()
    try:
        with patch("src.distribution.xhs_cards.render_note_cards",
                   side_effect=RuntimeError("render boom")), \
             patch.object(orch, "_collect_xhs_images",
                          return_value=["/tmp/cover.png"]) as m_collect, \
             patch.object(orch, "_upload_xiaohongshu_note_impl",
                          return_value={"note_id": "n2"}) as m_note:
            res = _call(video)
        assert res["xiaohongshu"]["ok"] is True
        m_collect.assert_called_once()
        assert m_note.call_args.kwargs["images"] == ["/tmp/cover.png"]
    finally:
        os.unlink(video)


def test_xhs_no_images_at_all_reports_error():
    """卡片失败且无任何图片 → ok=False 不抛出。"""
    video = _make_video()
    try:
        with patch("src.distribution.xhs_cards.render_note_cards",
                   return_value=[]), \
             patch.object(orch, "_collect_xhs_images", return_value=[]):
            res = _call(video)
        assert res["xiaohongshu"]["ok"] is False
    finally:
        os.unlink(video)
