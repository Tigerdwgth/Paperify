"""测试 orchestrator 对 douyin platform 的路由(全部 mock 上传 impl)。"""
import os
import tempfile
from unittest.mock import patch


def _make_video_file():
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(b"fake mp4")
    return path


def test_douyin_in_valid_platforms():
    from src.distribution.orchestrator import VALID_PLATFORMS, parse_platforms
    assert "douyin" in VALID_PLATFORMS
    # parse_platforms 应能识别
    assert parse_platforms("douyin") == ["douyin"]
    assert "douyin" in parse_platforms("bilibili,douyin,xiaohongshu")


def test_douyin_uses_tags_per_platform():
    """tags_per_platform.douyin 应作为 list 传给 douyin.upload()"""
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_douyin_impl",
                   return_value="douyin") as mock_dy:
            from src.distribution.orchestrator import upload_generated_content
            upload_generated_content(
                platforms=["douyin"],
                video_path=video, cover_path=None,
                video_title="测试标题", video_tags="备用,关键词",
                video_desc="desc",
                tags_per_platform={"douyin": ["VLA", "机器人", "AI"]},
            )
            kw = mock_dy.call_args.kwargs
            assert kw["title"] == "测试标题"
            assert kw["tags"] == ["VLA", "机器人", "AI"]
    finally:
        os.unlink(video)


def test_douyin_falls_back_to_video_tags():
    """tags_per_platform 缺失时应从 video_tags(逗号串)取前 5 个"""
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_douyin_impl",
                   return_value="douyin") as mock_dy:
            from src.distribution.orchestrator import upload_generated_content
            upload_generated_content(
                platforms=["douyin"],
                video_path=video, cover_path=None,
                video_title="t",
                video_tags="VLA,机器人,AI论文,大模型,前沿科技,arXiv,论文解读",
                video_desc="desc",
            )
            tags = mock_dy.call_args.kwargs["tags"]
            assert tags == ["VLA", "机器人", "AI论文", "大模型", "前沿科技"]
            assert len(tags) == 5
    finally:
        os.unlink(video)


def test_douyin_title_truncated_to_30():
    """抖音标题超过 30 字应裁剪"""
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_douyin_impl",
                   return_value="douyin") as mock_dy:
            from src.distribution.orchestrator import upload_generated_content
            long_title = "这是一个非常长的标题用来测试抖音是否会自动裁剪标题超过三十个字符的内容"
            upload_generated_content(
                platforms=["douyin"],
                video_path=video, cover_path=None,
                video_title=long_title, video_tags="t",
                video_desc="d",
            )
            assert len(mock_dy.call_args.kwargs["title"]) <= 30
    finally:
        os.unlink(video)


def test_douyin_failure_does_not_raise():
    """upload_douyin 返回 None 时 results 标 ok=False, 不抛异常"""
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_douyin_impl",
                   return_value=None):
            from src.distribution.orchestrator import upload_generated_content
            r = upload_generated_content(
                platforms=["douyin"],
                video_path=video, cover_path=None,
                video_title="t", video_tags="t", video_desc="d",
            )
            assert r["douyin"]["ok"] is False
    finally:
        os.unlink(video)


def test_douyin_exception_caught():
    """upload_douyin 抛异常应被 catch, 不破坏其他 platform"""
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_douyin_impl",
                   side_effect=RuntimeError("cookie 已失效")):
            from src.distribution.orchestrator import upload_generated_content
            r = upload_generated_content(
                platforms=["douyin"],
                video_path=video, cover_path=None,
                video_title="t", video_tags="t", video_desc="d",
            )
            assert r["douyin"]["ok"] is False
            assert "cookie" in r["douyin"]["error"]
    finally:
        os.unlink(video)


def test_upload_strips_illegal_title_chars_bilibili():
    """出口最后一道防线: video_title 带非法尖括号 → 传给 B站前被剥(B站 21009 修复)。"""
    import os as _os
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator.upload_bilibili",
                   return_value="BVFAKE") as mock_bili:
            from src.distribution.orchestrator import upload_generated_content
            upload_generated_content(
                platforms=["bilibili"],
                video_path=video, cover_path=None,
                video_title="<Ba-TAB: 用翻译桥接人类到机器人技能",
                video_tags="机器人,VLA",
                video_desc="desc",
                cn_titles=["<Ba-TAB: 用翻译桥接人类到机器人技能"],
                origin_titles=["Translation as a Bridging Action"],
            )
            title = mock_bili.call_args.kwargs["title"]
            assert title == "Ba-TAB: 用翻译桥接人类到机器人技能", title
            assert "<" not in title and ">" not in title
    finally:
        _os.unlink(video)


def test_upload_strips_illegal_title_chars_douyin():
    """douyin 分支同样受出口清洗保护。"""
    import os as _os
    video = _make_video_file()
    try:
        with patch("src.distribution.orchestrator._upload_douyin_impl",
                   return_value="douyin") as mock_dy:
            from src.distribution.orchestrator import upload_generated_content
            upload_generated_content(
                platforms=["douyin"],
                video_path=video, cover_path=None,
                video_title="<BESTRO: 多人博弈>",
                video_tags="a,b,c,d,e",
                video_desc="desc",
                tags_per_platform={"douyin": ["AI", "机器人"]},
            )
            title = mock_dy.call_args.kwargs["title"]
            assert "<" not in title and ">" not in title
            assert "BESTRO" in title
    finally:
        _os.unlink(video)
