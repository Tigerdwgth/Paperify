"""封面生成与上传相关 Bug 修复的单元测试。

覆盖范围：
- Bug 1: 多篇论文时封面不再在循环内被覆盖
- Bug 2: generate_cover 异常不会中断主流程
- Bug 3: 小红书视频封面(MCP 不吃 cover 参数, 改成 ffmpeg 拼进视频开头)
"""

import logging
import os
import sys
from unittest import mock

import pytest

# 确保项目根目录在 sys.path 中
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# paperagent_workflow.py 有大量重量级依赖（moviepy, dashscope 等），
# 对于源码结构验证类测试，直接读取文件文本进行分析，避免导入副作用
_WORKFLOW_PATH = os.path.join(ROOT, "src", "paperagent_workflow.py")


def _read_source(filepath: str) -> str:
    """读取源文件文本内容。"""
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# generate_cover 本身的测试
# ---------------------------------------------------------------------------

def _import_generate_cover():
    """延迟导入 generate_cover，确保 src 目录在 sys.path 中。"""
    src_dir = os.path.join(ROOT, "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    # generate_cover.py 直接从 config 导入 FONT_PATH，需确保 src/ 在 path 中
    from generate_cover import generate_cover
    return generate_cover


class TestGenerateCover:
    """测试 generate_cover 的基本行为。

    当前实现(src/generate_cover.py): 标题由 AI 直接画进画面 ——
    优先 Gemini 生图, 回退 DashScope, 两者都拿不到图时退回传入的原始图片,
    产物统一 resize 成 1280x720(不再用 PIL 叠字, 也不再有 FONT_PATH)。

    两个 AI 生图函数在测试里一律被替换: 既不打真实网络(它们会调 Google /
    DashScope 的生图接口), 又能分别覆盖"AI 可用"和"全部回退"两条分支。
    """

    @staticmethod
    def _module():
        _import_generate_cover()  # 确保 src/ 在 sys.path 里
        import generate_cover as gc_module
        return gc_module

    @staticmethod
    def _disable_ai(monkeypatch, gc_module):
        monkeypatch.setattr(gc_module, "_generate_cover_gemini", lambda *a, **k: "")
        monkeypatch.setattr(gc_module, "_generate_cover_dashscope", lambda *a, **k: "")

    def test_generate_cover_falls_back_to_source_image(self, tmp_path, monkeypatch):
        """AI 生图都不可用时, 回退原始图片并落盘封面。"""
        from PIL import Image

        gc_module = self._module()
        self._disable_ai(monkeypatch, gc_module)

        bg_path = str(tmp_path / "bg.png")
        Image.new("RGB", (200, 150), color=(0, 0, 255)).save(bg_path)
        output_path = str(tmp_path / "cover.png")

        gc_module.generate_cover(bg_path, "测试标题", output_path)

        assert os.path.exists(output_path), "封面文件应被创建"
        assert Image.open(output_path).getpixel((640, 360)) == (0, 0, 255), "应回退到原始图片"

    def test_generate_cover_prefers_ai_image(self, tmp_path, monkeypatch):
        """Gemini 返回图片时用 AI 图, 不用原始图片。"""
        from PIL import Image

        gc_module = self._module()
        ai_path = str(tmp_path / "ai.png")
        Image.new("RGB", (300, 200), color=(0, 255, 0)).save(ai_path)
        monkeypatch.setattr(gc_module, "_generate_cover_gemini", lambda *a, **k: ai_path)
        monkeypatch.setattr(gc_module, "_generate_cover_dashscope", lambda *a, **k: "")

        bg_path = str(tmp_path / "bg.png")
        Image.new("RGB", (200, 150), color=(0, 0, 255)).save(bg_path)
        output_path = str(tmp_path / "cover.png")

        gc_module.generate_cover(bg_path, "测试标题", output_path)

        assert Image.open(output_path).getpixel((640, 360)) == (0, 255, 0), "应使用 AI 生成的封面"

    def test_generate_cover_output_size(self, tmp_path, monkeypatch):
        """生成的封面图片尺寸应为 1280x720。"""
        from PIL import Image

        gc_module = self._module()
        self._disable_ai(monkeypatch, gc_module)

        bg_path = str(tmp_path / "bg.png")
        Image.new("RGB", (400, 300), color="red").save(bg_path)
        output_path = str(tmp_path / "cover.png")

        gc_module.generate_cover(bg_path, "尺寸测试", output_path)

        img = Image.open(output_path)
        assert img.size == (1280, 720), f"封面尺寸应为 (1280, 720)，实际为 {img.size}"

    def test_generate_cover_bad_image_raises(self, tmp_path, monkeypatch):
        """AI 不可用且原始图片也不存在 → 抛异常, 不静默产出坏封面。"""
        gc_module = self._module()
        self._disable_ai(monkeypatch, gc_module)

        with pytest.raises(Exception):
            gc_module.generate_cover("/nonexistent/bg.png", "标题", str(tmp_path / "out.png"))


# ---------------------------------------------------------------------------
# Bug 1: 多篇论文封面覆盖问题（基于源码文本分析）
# ---------------------------------------------------------------------------

class TestMultiPaperCoverNotOverwritten:
    """验证多篇论文时封面生成逻辑：循环内不再为多篇论文生成封面。"""

    def test_loop_body_no_daily_cover(self):
        """循环体内不应包含 'Arxiv具身日报' 的 generate_cover 调用。

        修复前：循环内 if len(papers) > 1: generate_cover(..., "Arxiv具身日报"...)
        修复后：循环内只有 if len(papers) == 1 的封面生成。
        """
        source = _read_source(_WORKFLOW_PATH)
        lines = source.split("\n")

        in_loop = False
        loop_cover_calls = []
        for line in lines:
            stripped = line.strip()
            if "for paper_idx, paper in enumerate(papers):" in stripped:
                in_loop = True
            if in_loop and "generate_cover" in stripped and not stripped.startswith("#"):
                loop_cover_calls.append(stripped)
            if in_loop and "if not generated_part_paths" in stripped:
                in_loop = False

        # 循环内应只有一个 generate_cover 调用
        assert len(loop_cover_calls) == 1, (
            f"循环内应只有1处 generate_cover 调用，实际有 {len(loop_cover_calls)}: {loop_cover_calls}"
        )
        # 且不应含 "Arxiv具身日报"
        assert "Arxiv具身日报" not in loop_cover_calls[0], (
            "循环内不应包含 'Arxiv具身日报' 的封面生成（应移到循环外）"
        )

    def test_daily_cover_after_write_videofile(self):
        """日报封面（Arxiv具身日报）应出现在 write_videofile 之后。"""
        source = _read_source(_WORKFLOW_PATH)
        lines = source.split("\n")

        write_idx = None
        daily_cover_idx = None
        for i, line in enumerate(lines):
            if "write_videofile" in line:
                write_idx = i
            if "Arxiv具身日报" in line and "generate_cover" in line:
                daily_cover_idx = i

        assert write_idx is not None, "源码应包含 write_videofile 调用"
        assert daily_cover_idx is not None, "源码应包含 Arxiv具身日报 封面生成"
        assert daily_cover_idx > write_idx, (
            f"日报封面生成(行{daily_cover_idx})应在 write_videofile(行{write_idx})之后"
        )

    def test_no_cover_overwrite_in_multi_paper_branch(self):
        """循环内的封面生成条件应为 len(papers) == 1，而非 len(papers) > 1。"""
        source = _read_source(_WORKFLOW_PATH)

        # 修复前的 bug 模式：不应存在
        assert 'if len(papers) > 1:' not in source or \
               'generate_cover' not in source.split('if len(papers) > 1:')[1].split('\n')[1], \
            "不应在 'len(papers) > 1' 分支内直接调用 generate_cover"


# ---------------------------------------------------------------------------
# Bug 2: generate_cover 异常处理（基于源码文本分析）
# ---------------------------------------------------------------------------

class TestCoverErrorHandling:
    """验证所有 generate_cover 调用都有 try-except 保护。"""

    def test_all_cover_calls_wrapped_in_try(self):
        """源码中每个 generate_cover 调用都应被 try-except 包裹。"""
        source = _read_source(_WORKFLOW_PATH)
        lines = source.split("\n")

        unprotected = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if "generate_cover" in stripped and not stripped.startswith("#") and "import" not in stripped:
                # 向上查找最近的 try:
                found_try = False
                for j in range(i - 1, max(i - 8, 0), -1):
                    if "try:" in lines[j]:
                        found_try = True
                        break
                if not found_try:
                    unprotected.append(f"行{i + 1}: {stripped}")

        assert len(unprotected) == 0, (
            f"以下 generate_cover 调用未被 try-except 包裹: {unprotected}"
        )

    def test_cover_failure_logs_warning(self):
        """generate_cover 异常处理应使用 logging.warning。"""
        source = _read_source(_WORKFLOW_PATH)
        lines = source.split("\n")

        for i, line in enumerate(lines):
            if "generate_cover" in line and not line.strip().startswith("#") and "import" not in line:
                # 查找对应的 except 块中是否有 warning
                for j in range(i + 1, min(i + 5, len(lines))):
                    if "except" in lines[j]:
                        # 检查接下来几行是否有 warning
                        found_warning = False
                        for k in range(j, min(j + 3, len(lines))):
                            if "warning" in lines[k].lower():
                                found_warning = True
                                break
                        assert found_warning, (
                            f"generate_cover (行{i + 1}) 的 except 块应包含 warning 日志"
                        )
                        break


# ---------------------------------------------------------------------------
# Bug 3: 小红书视频封面处理
# ---------------------------------------------------------------------------

class TestXiaohongshuVideoCover:
    """小红书视频封面: MCP 的 publish_with_video 不吃 cover 参数,
    当前实现改成用 ffmpeg 把封面拼到视频开头(小红书自动取首帧当封面)。
    """

    _FAKE_RESULT = {"content": [{"type": "text", "text": '{"note_id": "n1"}'}]}

    @staticmethod
    def _fake_mount(path, subdir="images"):
        return "/app/%s/%s" % (subdir, os.path.basename(path))

    def test_publish_video_prepends_cover_to_video(self, tmp_path):
        """传入存在的 cover_path → 封面拼进视频开头, 发布拼好的那个视频。"""
        from src.distribution.xiaohongshu import XiaohongshuMCPUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        cover = tmp_path / "c.png"
        cover.write_bytes(b"x")
        patched_video = str(tmp_path / "v_with_cover.mp4")
        prepend_calls = {}

        def fake_prepend(video_path, cover_path, duration=1.5):
            prepend_calls["video"] = video_path
            prepend_calls["cover"] = cover_path
            return patched_video

        uploader = XiaohongshuMCPUploader()
        with mock.patch("src.distribution.xiaohongshu._prepend_cover_to_video",
                        side_effect=fake_prepend), \
             mock.patch("src.distribution.xiaohongshu._copy_to_docker_mount",
                        side_effect=self._fake_mount), \
             mock.patch("src.distribution.xiaohongshu._call_tool",
                        return_value=self._FAKE_RESULT) as m_call:
            uploader.publish_video(title="测试", content="测试内容",
                                   video_path=str(video), cover_path=str(cover))

        assert prepend_calls == {"video": str(video), "cover": str(cover)}
        tool_name, arguments = m_call.call_args.args[0], m_call.call_args.args[1]
        assert tool_name == "publish_with_video"
        assert arguments["video"] == "/app/data/v_with_cover.mp4", "应发布拼了封面的视频"
        assert "cover" not in arguments, "MCP publish_with_video 不支持 cover 参数"

    def test_publish_video_without_cover_keeps_original_video(self, tmp_path):
        """不传 cover_path → 不走 ffmpeg 拼接, 直接发原视频。"""
        from src.distribution.xiaohongshu import XiaohongshuMCPUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        uploader = XiaohongshuMCPUploader()
        with mock.patch("src.distribution.xiaohongshu._prepend_cover_to_video") as m_prepend, \
             mock.patch("src.distribution.xiaohongshu._copy_to_docker_mount",
                        side_effect=self._fake_mount), \
             mock.patch("src.distribution.xiaohongshu._call_tool",
                        return_value=self._FAKE_RESULT) as m_call:
            uploader.publish_video(title="测试", content="测试内容",
                                   video_path=str(video), cover_path=None)

        m_prepend.assert_not_called()
        arguments = m_call.call_args.args[1]
        assert arguments["video"] == "/app/data/v.mp4"
        assert "cover" not in arguments

    def test_publish_video_ignores_missing_cover_file(self, tmp_path):
        """cover_path 指向不存在的文件 → 当没传处理, 不调 ffmpeg。"""
        from src.distribution.xiaohongshu import XiaohongshuMCPUploader

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        uploader = XiaohongshuMCPUploader()
        with mock.patch("src.distribution.xiaohongshu._prepend_cover_to_video") as m_prepend, \
             mock.patch("src.distribution.xiaohongshu._copy_to_docker_mount",
                        side_effect=self._fake_mount), \
             mock.patch("src.distribution.xiaohongshu._call_tool",
                        return_value=self._FAKE_RESULT) as m_call:
            uploader.publish_video(title="测试", content="测试内容",
                                   video_path=str(video),
                                   cover_path=str(tmp_path / "not_exist.png"))

        m_prepend.assert_not_called()
        assert m_call.call_args.args[1]["video"] == "/app/data/v.mp4"
