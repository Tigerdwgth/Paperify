"""ManimEngine.render_scene 的产物归属与重试纪律。

覆盖:
1. 渲染失败 (manim returncode != 0) 时, 盘上残留的同名旧视频不能被当成本次成功
   —— media_dir 与 scene 名都是固定的, 上一篇论文的 {Title,Intro,Method,Results}Scene
   mp4 留在盘上, 老实现会直接 glob 到就返回, retry 不触发、成片混入别的论文画面;
2. manim 报成功但没写出本次产物 (只剩旧 mtime 的文件) 同样算失败;
3. 正常成功路径照常返回本次写出的文件 (修复不能打断主路径);
4. opencode 修复返回空时保留原代码并终止重试, 不拿空串再白烧一轮渲染。
"""
import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)


SCENE_CODE = (
    "from manim import *\n\n\n"
    "class DemoScene(Scene):\n"
    "    def construct(self):\n"
    "        self.wait(1)\n"
)


@pytest.fixture
def engine(tmp_path):
    from manim_engine import ManimEngine
    return ManimEngine(paper_text="", structured_plan={},
                       output_dir=str(tmp_path / "manim"))


def _scene_output_path(engine, scene_name="DemoScene", ext="mp4"):
    """manim 的产物落点: <output_dir>/media/videos/<scene>/720p30/<scene>.<ext>"""
    d = os.path.join(engine.output_dir, "media", "videos", scene_name, "720p30")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{scene_name}.{ext}")


def _write_stale(path, days=180):
    """写一个"上一篇论文留下的"旧视频 (mtime 几个月前)。"""
    with open(path, "wb") as f:
        f.write(b"stale video from another paper")
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class _Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_failed_render_does_not_return_stale_video(engine, monkeypatch):
    """returncode != 0 时必须判失败, 不能把残留旧文件冒充成本次成功。"""
    import manim_engine as me
    stale = _scene_output_path(engine)
    _write_stale(stale)

    monkeypatch.setattr(me.subprocess, "run",
                        lambda *a, **k: _Result(1, stderr="SyntaxError: invalid syntax"))

    out = engine.render_scene(SCENE_CODE, "DemoScene", max_retries=1)

    assert out is None, "渲染失败却返回了上次运行残留的旧视频"
    assert not os.path.exists(stale), "渲染前应先清掉同名历史产物"


def test_returncode_zero_without_fresh_output_is_failure(engine, monkeypatch):
    """manim 返回 0 但盘上只有旧 mtime 的文件 -> 仍判失败 (不是本次产物)。"""
    import manim_engine as me
    target = _scene_output_path(engine)
    _write_stale(target)

    def _fake_run(*a, **k):
        # 模拟"清理没删掉/别处同名残留": 文件存在, 但 mtime 还是几个月前
        _write_stale(target)
        return _Result(0)

    monkeypatch.setattr(me.subprocess, "run", _fake_run)

    assert engine.render_scene(SCENE_CODE, "DemoScene", max_retries=1) is None


def test_successful_render_returns_this_run_output(engine, monkeypatch):
    """正常成功路径不受影响: 返回本次真正写出的 mp4。"""
    import manim_engine as me
    target = _scene_output_path(engine)
    _write_stale(target)  # 上一篇论文的残留, 应被本次产物覆盖

    def _fake_run(*a, **k):
        with open(target, "wb") as f:
            f.write(b"fresh video of this run")
        return _Result(0, stdout="File ready at ...")

    monkeypatch.setattr(me.subprocess, "run", _fake_run)

    out = engine.render_scene(SCENE_CODE, "DemoScene", max_retries=1)

    assert out == target
    with open(out, "rb") as f:
        assert f.read() == b"fresh video of this run"


def test_purge_keeps_output_rendered_in_this_run(engine):
    """只清上一次 run 的残留: 本 run 里刚渲好的同名产物要留着。

    一致性检查会对同一个 scene 重渲, 重渲失败时上层要回退到上一版视频文件。
    """
    target = _scene_output_path(engine)
    with open(target, "wb") as f:
        f.write(b"rendered earlier in this same run")

    engine._purge_stale_scene_outputs(
        os.path.join(engine.output_dir, "media", "videos"), "DemoScene", "mp4")

    assert os.path.exists(target), "本 run 内刚渲好的产物被误删, 一致性检查无法回退"


def test_empty_fix_keeps_original_code_and_stops_retry(engine, monkeypatch):
    """opencode 修复返回空: 保留原代码 + 终止重试, 不把空串写进 .py 再渲一轮。"""
    import manim_engine as me
    script_path = os.path.join(engine.temp_dir, "DemoScene.py")
    rendered_sources = []

    def _fake_run(*a, **k):
        with open(script_path, encoding="utf-8") as f:
            rendered_sources.append(f.read())
        return _Result(1, stderr="boom")

    fix_calls = []

    def _empty_fix(*a, **k):
        fix_calls.append(1)
        return ""

    monkeypatch.setattr(me.subprocess, "run", _fake_run)
    monkeypatch.setattr(engine, "_opencode_generate_with_retry", _empty_fix)

    out = engine.render_scene(SCENE_CODE, "DemoScene", max_retries=3)

    assert out is None
    assert len(fix_calls) == 1, "修复失败后仍在反复调 opencode"
    assert len(rendered_sources) == 1, "修复返回空却又白烧了一轮 manim 渲染"
    assert rendered_sources[0] == SCENE_CODE, "原代码被空串毒化"


def test_nonempty_fix_still_retries_with_injected_code(engine, monkeypatch):
    """修复返回非空时重试链路照常, 且修复版仍过 inject_bounds_check。"""
    import manim_engine as me
    script_path = os.path.join(engine.temp_dir, "DemoScene.py")
    rendered_sources = []

    def _fake_run(*a, **k):
        with open(script_path, encoding="utf-8") as f:
            rendered_sources.append(f.read())
        return _Result(1, stderr="boom")

    fixed = (
        "from manim import *\n\n\n"
        "class DemoScene(Scene):\n"
        "    def construct(self):\n"
        "        self.play(FadeIn(Text('fixed')))\n"
    )
    monkeypatch.setattr(me.subprocess, "run", _fake_run)
    monkeypatch.setattr(engine, "_opencode_generate_with_retry", lambda *a, **k: fixed)

    assert engine.render_scene(SCENE_CODE, "DemoScene", max_retries=2) is None
    assert len(rendered_sources) == 2, "修复成功却没有重试"
    assert "Text-overlap guard" in rendered_sources[1], "修复版未过 inject_bounds_check"
