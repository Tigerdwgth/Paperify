"""orchestrator 单元测试。

当前实现(src/distribution/orchestrator.py)有两个关键行为, 测试按它写:

1. **小红书只发图文卡片**: xiaohongshu 分支固定走 ``xhs_cards.render_note_cards``
   + ``upload_xiaohongshu_note``; ``upload_xiaohongshu_video`` 已不在路由里
   (仅为兼容旧调用方保留在 xiaohongshu.py)。卡片渲染失败才降级到
   ``_collect_xhs_images``(封面 + ./pic/*.png)。
2. **B站/小红书文案默认不带外链**(外链触发平台限流, 见 LESSONS 2026-05-29)。
   ``_build_compact_description`` 的 ``include_links`` 默认就是 False(fail-safe:
   新增平台 builder 漏传参数时不会把链接发出去), 两个调用点另外也显式传了 False。
   要链接必须显式 ``include_links=True``。开关本身的两条路径单独测。

tests/conftest.py 的 autouse 防线已把真实上传实现 + chromium 渲染换成抛错桩,
但本文件每个上传测试仍自己 patch 到位——防线只是"漏网即刻报错"的兜底。
"""
from unittest.mock import MagicMock

import pytest

from src.distribution.orchestrator import (
    XHS_DEFAULT_TAGS,
    _build_bilibili_desc,
    _build_compact_description,
    _build_xhs_content,
    parse_platforms,
    upload_generated_content,
)

PAPER_LINK = "https://arxiv.org/abs/2501.00001"
PROJECT_LINK = "https://example.com/project"


# =========================================================================
# 夹具: 把小红书图文卡片链路换成可观测的假实现
# =========================================================================
@pytest.fixture
def card_render(monkeypatch):
    """替换 render_note_cards(真身会起 chromium), 记录 kwargs 并返回假卡片。"""
    calls = {}

    def _render(**kwargs):
        calls["kwargs"] = kwargs
        return ["/tmp/fake_card_00.png", "/tmp/fake_card_01.png"]

    monkeypatch.setattr("src.distribution.xhs_cards.render_note_cards", _render)
    return calls


@pytest.fixture
def note_publish(monkeypatch):
    """替换小红书图文发布(真身会打 MCP 发布线上笔记), 记录 kwargs。"""
    calls = {}

    def _publish(**kwargs):
        calls.update(kwargs)
        return {"note_id": "NOTE_OK"}

    monkeypatch.setattr("src.distribution.orchestrator.upload_xiaohongshu_note", _publish)
    return calls


@pytest.fixture
def video_publish_spy(monkeypatch):
    """小红书视频发布探针: 当前实现不应再调用它。"""
    spy = MagicMock(return_value={"note_id": "SHOULD_NOT_HAPPEN"})
    monkeypatch.setattr("src.distribution.orchestrator.upload_xiaohongshu_video", spy)
    return spy


# =========================================================================
# platform 解析
# =========================================================================
def test_parse_platforms_default_dual():
    assert parse_platforms(None) == ["bilibili", "xiaohongshu"]


def test_parse_platforms_none_disables_upload():
    assert parse_platforms("none") == []


def test_parse_platforms_filters_unknown_values():
    assert parse_platforms("bilibili,foo,xiaohongshu") == ["bilibili", "xiaohongshu"]


# =========================================================================
# 上传路由
# =========================================================================
def test_partial_success_does_not_raise(monkeypatch, tmp_path, card_render):
    """单平台失败不应影响其他平台, 也不应抛出。"""
    video = tmp_path / "video.mp4"
    cover = tmp_path / "cover.png"
    video.write_bytes(b"x")
    cover.write_bytes(b"x")

    monkeypatch.setattr(
        "src.distribution.orchestrator.upload_bilibili",
        lambda **kwargs: "BV1TEST",
    )

    def fail_xhs_note(**kwargs):
        raise RuntimeError("xhs note error")

    monkeypatch.setattr("src.distribution.orchestrator.upload_xiaohongshu_note", fail_xhs_note)

    result = upload_generated_content(
        platforms=["bilibili", "xiaohongshu"],
        video_path=str(video),
        cover_path=str(cover),
        video_title="title",
        video_tags="tag1,tag2",
        video_desc="desc",
        cn_titles=["中文标题"],
        origin_titles=["English title"],
    )

    assert result["bilibili"]["ok"] is True
    assert result["xiaohongshu"]["ok"] is False
    assert "xhs note error" in result["xiaohongshu"]["error"]


def test_xhs_publishes_note_cards_not_video(tmp_path, card_render, note_publish, video_publish_spy):
    """小红书走图文卡片: 卡片图进 publish_note, 视频接口一次都不调。"""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")

    result = upload_generated_content(
        platforms=["xiaohongshu"],
        video_path=str(video),
        cover_path=None,
        video_title="测试视频",
        video_tags="",
        video_desc="描述",
        cn_titles=["中文标题"],
        summaries=["这是摘要"],
        narration_segments=["旁白一", "旁白二"],
        scene_frames=["/tmp/frame0.png", None],
    )

    assert result["xiaohongshu"] == {"ok": True, "id": "NOTE_OK", "type": "note"}
    video_publish_spy.assert_not_called()

    # 卡片渲染拿到标题/旁白/摘要/场景帧
    card_kwargs = card_render["kwargs"]
    assert card_kwargs["title"] == "中文标题"
    assert card_kwargs["narration_segments"] == ["旁白一", "旁白二"]
    assert card_kwargs["summaries"] == ["这是摘要"]
    assert card_kwargs["scene_frames"] == ["/tmp/frame0.png", None]

    # 渲染出的卡片图原样进发布接口
    assert note_publish["images"] == ["/tmp/fake_card_00.png", "/tmp/fake_card_01.png"]
    assert note_publish["title"] == "中文标题"
    assert "中文标题" in note_publish["content"]


def test_xhs_card_title_falls_back_to_video_title(tmp_path, card_render, note_publish):
    """没有中文标题时, 卡片标题回退到视频标题(截断 20 字)。"""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")

    upload_generated_content(
        platforms=["xiaohongshu"],
        video_path=str(video),
        cover_path=None,
        video_title="一个没有中文标题字段的视频标题",
        video_tags="",
        video_desc="描述",
    )

    assert card_render["kwargs"]["title"] == "一个没有中文标题字段的视频标题"
    assert note_publish["title"] == "一个没有中文标题字段的视频标题"


def test_xhs_card_render_failure_falls_back_to_local_images(monkeypatch, tmp_path, note_publish):
    """卡片渲染抛异常 → 降级封面 + ./pic/*.png, 仍然发图文。"""
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"x")
    pic_dir = tmp_path / "pic"
    pic_dir.mkdir()
    (pic_dir / "img1.png").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)

    def boom(**kwargs):
        raise RuntimeError("render boom")

    monkeypatch.setattr("src.distribution.xhs_cards.render_note_cards", boom)

    result = upload_generated_content(
        platforms=["xiaohongshu"],
        video_path="",
        cover_path=str(cover),
        video_title="测试",
        video_tags="",
        video_desc="描述",
    )

    assert result["xiaohongshu"]["ok"] is True
    assert result["xiaohongshu"]["type"] == "note"
    assert note_publish["images"] == [str(cover.resolve()), str((pic_dir / "img1.png").resolve())]


def test_xhs_without_video_file_still_publishes_note(tmp_path, card_render, note_publish, video_publish_spy):
    """小红书不依赖视频文件: video_path 为空也照常发图文卡片。"""
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"x")

    result = upload_generated_content(
        platforms=["xiaohongshu"],
        video_path="",
        cover_path=str(cover),
        video_title="测试",
        video_tags="",
        video_desc="描述",
    )

    assert result["xiaohongshu"]["ok"] is True
    assert result["xiaohongshu"]["type"] == "note"
    assert card_render["kwargs"]["cover_path"] == str(cover)
    video_publish_spy.assert_not_called()


def test_xhs_no_images_at_all_reports_error(monkeypatch, tmp_path, note_publish):
    """卡片渲染返回空且本地无图 → ok=False, 不抛出, 也不发布。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.distribution.xhs_cards.render_note_cards", lambda **kw: [])

    result = upload_generated_content(
        platforms=["xiaohongshu"],
        video_path="",
        cover_path=None,
        video_title="测试",
        video_tags="",
        video_desc="描述",
    )

    assert result["xiaohongshu"]["ok"] is False
    assert "图片" in result["xiaohongshu"]["error"]
    assert note_publish == {}


def test_xhs_empty_publish_result_reports_error(monkeypatch, tmp_path, card_render):
    """publish_note 返回空 → ok=False。"""
    monkeypatch.setattr("src.distribution.orchestrator.upload_xiaohongshu_note", lambda **kw: None)

    result = upload_generated_content(
        platforms=["xiaohongshu"],
        video_path="",
        cover_path=None,
        video_title="测试",
        video_tags="",
        video_desc="描述",
    )

    assert result["xiaohongshu"]["ok"] is False
    assert result["xiaohongshu"]["error"] == "发布返回空结果"


# =========================================================================
# 文案构建: 小红书
# =========================================================================
def test_xhs_content_includes_origin_title():
    """小红书文案必须包含论文原名（英文标题）"""
    content = _build_xhs_content(
        video_desc="",
        cn_titles=["全1比特视觉语言动作模型"],
        origin_titles=["BitVLA: 1-bit Vision-Language-Action Models"],
        summaries=["这是一篇关于1比特模型的论文摘要"],
    )
    assert "BitVLA: 1-bit Vision-Language-Action Models" in content


def test_xhs_content_keeps_short_summary():
    """小红书文案可保留精简摘要。"""
    content = _build_xhs_content(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        summaries=["这是论文的中文摘要，描述了核心方法和实验结果"],
    )
    assert "中文标题" in content
    assert "English Title" in content
    assert "摘要：" in content
    assert "核心方法和实验结果" in content


def test_xhs_content_includes_cn_title():
    """小红书文案必须包含中文标题"""
    content = _build_xhs_content(
        video_desc="",
        cn_titles=["全1比特VLA模型"],
        origin_titles=["BitVLA"],
        summaries=["摘要内容"],
    )
    assert "全1比特VLA模型" in content


def test_xhs_content_truncates_to_limit():
    """小红书文案不超过 XHS_CONTENT_LIMIT(300)"""
    long_summary = "这是一段很长的摘要" * 200
    content = _build_xhs_content(
        video_desc="",
        cn_titles=["标题"],
        origin_titles=["Title"],
        summaries=[long_summary],
    )
    assert len(content) <= 300


def test_xhs_content_backward_compatible():
    """不传 summaries 时不应报错（向后兼容）"""
    content = _build_xhs_content(
        video_desc="desc",
        cn_titles=["标题"],
        origin_titles=["Title"],
    )
    assert "Title" in content
    assert "标题" in content


def test_xhs_content_omits_paper_and_project_links():
    """小红书文案不带任何外链(带链接会被限流), 但标题/摘要照常保留。"""
    content = _build_xhs_content(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        summaries=["这是一段摘要"],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
    )
    assert PAPER_LINK not in content
    assert PROJECT_LINK not in content
    assert "论文链接：" not in content
    assert "项目链接：" not in content
    assert "http" not in content
    assert "中文标题" in content
    assert "English Title" in content
    assert "这是一段摘要" in content


def test_xhs_content_stays_brief_even_with_long_summary():
    """小红书文案应控制在精简长度内, 且仍然不含链接。"""
    content = _build_xhs_content(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        summaries=["很长的摘要" * 200],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
    )
    assert len(content) <= 300
    assert "English Title" in content
    assert "http" not in content


# =========================================================================
# 文案构建: B站
# =========================================================================
def test_bilibili_desc_includes_origin_title():
    """B站描述必须包含论文原名"""
    desc = _build_bilibili_desc(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["BitVLA: 1-bit VLA Models"],
        summaries=["这是摘要"],
    )
    assert "BitVLA: 1-bit VLA Models" in desc


def test_bilibili_desc_keeps_titles_but_omits_links():
    """B站简介保留论文名/摘要, 但不带外链(带链接会被限流)。"""
    desc = _build_bilibili_desc(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["Title"],
        summaries=["论文提出了全新的1比特量化方法"],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
    )
    assert "Title" in desc
    assert "1比特量化方法" in desc
    assert PAPER_LINK not in desc
    assert PROJECT_LINK not in desc
    assert "论文链接：" not in desc
    assert "项目链接：" not in desc


def test_bilibili_desc_omits_cn_title():
    """B站简介只放论文原名, 不重复中文标题(中文标题就是视频标题)。"""
    desc = _build_bilibili_desc(
        video_desc="",
        cn_titles=["一个中文标题"],
        origin_titles=["English Title"],
        summaries=["摘要"],
    )
    assert "中文标题：" not in desc
    assert "English Title" in desc


def test_bilibili_desc_fallback_no_summaries():
    """没有 summaries 时回退到 video_desc"""
    desc = _build_bilibili_desc(
        video_desc="fallback desc",
        cn_titles=None,
        origin_titles=None,
        summaries=None,
    )
    assert desc == "fallback desc"


def test_bilibili_desc_multi_papers():
    """多篇论文时B站简介应包含所有论文标题，并带各自摘要。"""
    desc = _build_bilibili_desc(
        video_desc="",
        cn_titles=["标题A", "标题B"],
        origin_titles=["Paper A", "Paper B"],
        summaries=["摘要A", "摘要B"],
    )
    assert "Paper A" in desc
    assert "Paper B" in desc
    assert "摘要A" in desc
    assert "摘要B" in desc


def test_bilibili_desc_stays_within_upload_limit():
    """B站简介应在上传限制(250)内, 且不含链接。"""
    desc = _build_bilibili_desc(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        summaries=["很长的摘要" * 200],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
    )
    assert len(desc) <= 250
    assert "English Title" in desc
    assert "http" not in desc


# =========================================================================
# include_links 开关本身的两条路径
# =========================================================================
def test_compact_description_omits_links_by_default():
    """include_links 默认 False: 不传这个参数时一律不带链接。

    这是 fail-safe 默认值——新增平台的 builder 忘了传 include_links 时,
    应当"不带链接"而不是默默把外链发出去触发限流。这条测试盯住默认值
    别被谁改回 True。
    """
    text = _build_compact_description(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
        summaries=["这是一段摘要"],
        total_limit=300,
        summary_limit=90,
        include_cn_titles=True,
    )
    assert "http" not in text
    assert "链接" not in text
    # 正文本身不受影响
    assert "中文标题" in text
    assert "English Title" in text


def test_compact_description_includes_links_when_explicitly_enabled():
    """显式 include_links=True 才拼链接——这条路径仍然要能用。"""
    text = _build_compact_description(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
        summaries=["这是一段摘要"],
        total_limit=300,
        summary_limit=90,
        include_cn_titles=True,
        include_links=True,
    )
    assert f"论文链接：{PAPER_LINK}" in text
    assert f"项目链接：{PROJECT_LINK}" in text


def test_compact_description_omits_empty_project_link_when_links_enabled():
    """开着链接时, 空项目链接不占行。"""
    text = _build_compact_description(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        paper_links=[PAPER_LINK],
        project_links=[""],
        summaries=["这是一段摘要"],
        total_limit=300,
        summary_limit=90,
        include_links=True,
    )
    assert f"论文链接：{PAPER_LINK}" in text
    assert "项目链接：" not in text


def test_compact_description_drops_all_links_when_disabled():
    """include_links=False: 论文链接和项目链接都不出现, 其余内容不变。"""
    text = _build_compact_description(
        video_desc="",
        cn_titles=["中文标题"],
        origin_titles=["English Title"],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
        summaries=["这是一段摘要"],
        total_limit=300,
        summary_limit=90,
        include_cn_titles=True,
        include_links=False,
    )
    assert "http" not in text
    assert "链接" not in text
    assert "中文标题" in text
    assert "English Title" in text
    assert "这是一段摘要" in text


def test_compact_description_multi_paper_links_when_enabled():
    """多篇论文开链接时, 每篇的链接都要在。"""
    text = _build_compact_description(
        video_desc="",
        cn_titles=None,
        origin_titles=["Paper A", "Paper B"],
        paper_links=["https://arxiv.org/abs/2501.00001", "https://arxiv.org/abs/2501.00002"],
        project_links=None,
        summaries=None,
        total_limit=500,
        summary_limit=90,
        include_links=True,
    )
    assert "https://arxiv.org/abs/2501.00001" in text
    assert "https://arxiv.org/abs/2501.00002" in text


# =========================================================================
# 上传链路里的文案
# =========================================================================
def test_upload_bilibili_desc_omits_links(monkeypatch, tmp_path):
    """上传到 B站 时简介保留论文名, 但不带任何链接。"""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")

    captured = {}

    def mock_bilibili(**kwargs):
        captured.update(kwargs)
        return "BV1TEST"

    monkeypatch.setattr("src.distribution.orchestrator.upload_bilibili", mock_bilibili)

    upload_generated_content(
        platforms=["bilibili"],
        video_path=str(video),
        cover_path=None,
        video_title="测试",
        video_tags="tag",
        video_desc="原始描述",
        cn_titles=["中文标题"],
        origin_titles=["English Paper Title"],
        summaries=["这是论文的中文摘要内容"],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
    )

    desc = captured["desc"]
    assert "English Paper Title" in desc
    assert "这是论文的中文摘要内容" in desc
    assert "http" not in desc
    assert "论文链接：" not in desc
    assert "项目链接：" not in desc


def test_upload_platform_descriptions_omit_links(monkeypatch, tmp_path, card_render, note_publish):
    """两个平台的成品文案都不含链接。"""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")

    captured = {}

    def mock_bilibili(**kwargs):
        captured["bilibili_desc"] = kwargs.get("desc", "")
        return "BV1TEST"

    monkeypatch.setattr("src.distribution.orchestrator.upload_bilibili", mock_bilibili)

    upload_generated_content(
        platforms=["bilibili", "xiaohongshu"],
        video_path=str(video),
        cover_path=None,
        video_title="测试",
        video_tags="tag",
        video_desc="原始描述",
        cn_titles=["中文标题"],
        origin_titles=["English Paper Title"],
        summaries=["这是论文的中文摘要内容"],
        paper_links=[PAPER_LINK],
        project_links=[PROJECT_LINK],
    )

    for text in (captured["bilibili_desc"], note_publish["content"]):
        assert "English Paper Title" in text
        assert "http" not in text
        assert "论文链接：" not in text
        assert "项目链接：" not in text


# =========================================================================
# 小红书 Tag 测试
# =========================================================================
def test_xhs_default_tags():
    """XHS_DEFAULT_TAGS (fallback) 应非空且至少含 1 个常用频道标签"""
    assert isinstance(XHS_DEFAULT_TAGS, list) and len(XHS_DEFAULT_TAGS) >= 1
    assert "具身智能" in XHS_DEFAULT_TAGS


def test_xhs_note_receives_fallback_tags(tmp_path, card_render, note_publish):
    """什么标签都不传时, 图文发布拿到 fallback 标签"""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")

    upload_generated_content(
        platforms=["xiaohongshu"],
        video_path=str(video),
        cover_path=None,
        video_title="测试",
        video_tags="",
        video_desc="",
    )

    assert list(note_publish["tags"]) == list(XHS_DEFAULT_TAGS)


def test_xhs_custom_tags_used_as_is(tmp_path, card_render, note_publish):
    """显式 xhs_tags 直接使用(不再叠加 fallback), 且不重复"""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")

    upload_generated_content(
        platforms=["xiaohongshu"],
        video_path=str(video),
        cover_path=None,
        video_title="测试",
        video_tags="",
        video_desc="",
        xhs_tags=["机器人", "VLA"],
    )

    tags = note_publish["tags"]
    assert tags == ["机器人", "VLA"]
    assert tags.count("VLA") == 1


def test_xhs_note_fallback_receives_tags(monkeypatch, tmp_path, note_publish):
    """卡片渲染失败降级发图文时, 标签同样要带上"""
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.distribution.xhs_cards.render_note_cards",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("render boom")),
    )

    upload_generated_content(
        platforms=["xiaohongshu"],
        video_path="",
        cover_path=str(cover),
        video_title="测试",
        video_tags="",
        video_desc="",
    )

    assert list(note_publish["tags"]) == list(XHS_DEFAULT_TAGS)
