"""测试 ManimEngine 的文字重叠守卫 (_inject_text_overlap_guard)。

覆盖:
1. inject_bounds_check 注入后产物语法可编译, 且包含 overlap-guard 标记。
2. 重叠比例数学语义 (与注入代码里 _to_overlap_ratio 一致) 锁定阈值行为。
3. (端到端, 需要 manim) 渲染两个重叠 Text 的场景, 守卫应移除下层文字。
"""
import os
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)


SCENE_SRC = '''from manim import *


class DemoScene(Scene):
    def construct(self):
        a = Text("第一行旧文字")
        b = Text("第二行新文字")
        self.play(FadeIn(a))
        self.wait(0.1)
        self.play(FadeIn(b))
        self.wait(0.1)
'''


@pytest.fixture
def engine():
    from manim_engine import ManimEngine
    tmp = tempfile.mkdtemp(prefix="ovl_guard_")
    return ManimEngine(paper_text="", structured_plan={}, output_dir=tmp)


def test_injection_compiles_and_has_markers(engine):
    out = engine.inject_bounds_check(SCENE_SRC)
    # 注入标记齐全
    assert "Text-overlap guard" in out
    assert "_to_resolve_overlap" in out
    assert "self.play = _to_guarded_play" in out
    # 缩放守卫仍在 (没有被破坏)
    assert "Safe-frame guard" in out
    # 语法可编译
    compile(out, "<injected>", "exec")


def test_injection_idempotent_construct_only(engine):
    """没有 construct 的代码原样返回, 不报错。"""
    src = "from manim import *\nx = 1\n"
    out = engine._inject_text_overlap_guard(src)
    assert out == src


def test_injection_handles_annotated_construct(engine):
    """带返回注解的 def construct(self) -> None: 也要被注入守卫 (finding 5)。"""
    src = (
        "from manim import *\n\n\n"
        "class DemoScene(Scene):\n"
        "    def construct(self) -> None:\n"
        "        self.play(FadeIn(Text('hi')))\n"
    )
    out = engine._inject_text_overlap_guard(src)
    assert "_to_resolve_overlap" in out, "带注解签名未被匹配, 守卫漏注入"
    compile(out, "<annotated>", "exec")


def test_injection_async_construct(engine):
    """async def construct(self) 也要被注入守卫 (review #5: 消除 startswith 回归)。"""
    src = (
        "from manim import *\n\n\n"
        "class DemoScene(Scene):\n"
        "    async def construct(self):\n"
        "        self.play(FadeIn(Text('hi')))\n"
    )
    out = engine._inject_text_overlap_guard(src)
    assert "_to_resolve_overlap" in out, "async 签名未被匹配, 守卫漏注入"
    compile(out, "<async>", "exec")


MULTILINE_PLAY_SRC = '''from manim import *


class DemoScene(Scene):
    def construct(self):
        a = Text("甲")
        b = Text("乙")
        self.play(
            FadeIn(a),
            FadeIn(b),
        )
'''


def test_injection_multiline_trailing_play_compiles(engine):
    """construct 末尾是跨行 self.play(...) 时注入后不能产生 SyntaxError (review #2)。

    旧的 epilogue 括号配平扫描会停在跨行语句开头行 self.play( 处, 把 epilogue
    插进未闭合的调用中间。
    """
    out = engine.inject_bounds_check(MULTILINE_PLAY_SRC)
    compile(out, "<multiline>", "exec")  # 旧逻辑此处会 SyntaxError
    assert "Auto-scale safety net" in out, "scale-safety epilogue 丢失"


def test_render_scene_retry_reinjects_guard(engine, monkeypatch):
    """渲染失败重试时, opencode 修复后的代码也要过 inject_bounds_check (review #1)。"""
    import manim_engine as me
    # opencode 修复返回一段裸 scene (不含任何注入标记)
    bare = (
        "from manim import *\n\n\n"
        "class DemoScene(Scene):\n"
        "    def construct(self):\n"
        "        self.play(FadeIn(Text('x')))\n"
    )
    monkeypatch.setattr(engine, "_opencode_generate_with_retry", lambda *a, **k: bare)

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(me.subprocess, "run", lambda *a, **k: _FakeResult())

    initial = (
        "from manim import *\n\n\n"
        "class DemoScene(Scene):\n"
        "    def construct(self):\n"
        "        self.wait(1)\n"
    )
    out = engine.render_scene(initial, "DemoScene", max_retries=2)
    assert out is None  # 渲染始终失败
    written = open(os.path.join(engine.temp_dir, "DemoScene.py"), encoding="utf-8").read()
    assert "Text-overlap guard" in written, "重试代码未经 inject_bounds_check 注入重叠守卫"
    assert "Safe-frame guard" in written, "重试代码未经 inject_bounds_check 注入缩放守卫"


MULTILINE_FADEOUT_SRC = '''from manim import *


class DemoScene(Scene):
    def construct(self):
        box = Square()
        self.play(FadeIn(box))
        self.wait(1)
        self.play(FadeOut(box),
                  run_time=1.5)
'''


SINGLE_LINE_FADEOUT_SRC = '''from manim import *


class DemoScene(Scene):
    def construct(self):
        box = Square()
        self.play(FadeIn(box))
        self.play(FadeOut(box))
'''


def test_trailing_multiline_fadeout_removed_without_syntax_error(engine):
    """末尾 FadeOut 跨行时整条语句一起换成 wait, 不能留下孤立续行。

    旧实现按整行替换, LLM 常写的 self.play(FadeOut(box),\n run_time=1.5) 会剩下
    孤立的 `run_time=1.5)` -> SyntaxError, 整个 scene 渲染必炸。
    """
    out = engine._remove_trailing_fadeout(MULTILINE_FADEOUT_SRC)
    compile(out, "<multiline-fadeout>", "exec")  # 旧逻辑此处会 SyntaxError
    assert "FadeOut" not in out
    assert "run_time=1.5" not in out
    assert "self.wait(2)  # 保持内容显示" in out


def test_trailing_single_line_fadeout_still_removed(engine):
    """单行 FadeOut 的老行为不变: 换成 wait, 且前面的 FadeIn 不受影响。"""
    out = engine._remove_trailing_fadeout(SINGLE_LINE_FADEOUT_SRC)
    compile(out, "<single-fadeout>", "exec")
    assert "self.play(FadeIn(box))" in out
    assert "FadeOut" not in out
    assert out.rstrip().endswith("self.wait(2)  # 保持内容显示")


def test_fadeout_inside_text_with_parens_not_broken(engine):
    """字符串里的括号不能算进配平, 否则会把后面的代码一起吞掉。"""
    src = (
        "from manim import *\n\n\n"
        "class DemoScene(Scene):\n"
        "    def construct(self):\n"
        "        label = Text(\"结果 (a) 对比\")\n"
        "        self.play(FadeOut(label), run_time=1.0)\n"
    )
    out = engine._remove_trailing_fadeout(src)
    compile(out, "<paren-text>", "exec")
    assert "Text(\"结果 (a) 对比\")" in out, "字符串里的括号导致多吞了代码行"
    assert "FadeOut" not in out


def _overlap_ratio(a, b):
    """复刻注入代码中的 _to_overlap_ratio (5 元组: x0,y0,x1,y1,area)。

    用"交叠面积 / 较大块面积"归一化: 要求两块大幅互相重合才算糊成一团。
    """
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    amax = max(a[4], b[4])
    return inter / amax if amax > 0 else 0.0


def test_overlap_ratio_semantics():
    THRESH = 0.5  # 与注入代码 _TO_THRESH 保持一致
    # 完全重合 -> 1.0
    box = (-1, -1, 1, 1, 4.0)
    assert _overlap_ratio(box, box) == pytest.approx(1.0)
    # 完全分离 -> 0.0
    far = (5, 5, 6, 6, 1.0)
    assert _overlap_ratio(box, far) == 0.0
    # 仅边缘相邻 (无面积) -> 0.0, 不应误判
    edge = (1, -1, 3, 1, 4.0)
    assert _overlap_ratio(box, edge) == 0.0
    # 小标签整块落在大框内 (小标签全被覆盖, 但只占大框一角):
    # 相对"较大块"面积很小, 必须 <= 阈值 —— 否则会误删整个大段落 (finding 1/2)
    small = (-1, -1, 0, 0, 1.0)  # 面积 1, 完全在 box 内, 交叠=1
    assert _overlap_ratio(box, small) == pytest.approx(0.25)  # 1 / max(4,1)=4
    assert _overlap_ratio(box, small) <= THRESH
    # 两块大小相近且高度重合 (真·糊成一团) -> 必须超过阈值, 删下层
    near = (-0.8, -0.8, 1.2, 1.2, 4.0)  # 面积 4, 与 box 交叠 (1.8)*(1.8)=3.24
    assert _overlap_ratio(box, near) == pytest.approx(0.81)  # 3.24 / 4
    assert _overlap_ratio(box, near) > THRESH


@pytest.mark.skipif(
    os.getenv("RUN_MANIM_RENDER") != "1",
    reason="端到端渲染较慢, 需 RUN_MANIM_RENDER=1 显式开启",
)
def test_end_to_end_overlap_removed(engine, tmp_path):
    """渲染含两个重叠 Text 的场景, 守卫应使下层文字被 FadeOut。

    用渲染日志里的 [overlap-guard] 标记 + 末帧仅含一行文字间接验证。
    """
    out = engine.inject_bounds_check(SCENE_SRC)
    script = tmp_path / "DemoScene.py"
    script.write_text(out, encoding="utf-8")
    import subprocess
    r = subprocess.run(
        f"manim render -ql --media_dir {tmp_path}/media {script} DemoScene",
        shell=True, capture_output=True, text=True, timeout=600,
    )
    assert "[overlap-guard]" in (r.stderr + r.stdout), \
        "未触发重叠守卫:\n" + r.stderr[-2000:]


SMALL_LABEL_SRC = '''from manim import *


class DemoScene(Scene):
    def construct(self):
        block = Text("这是一大段需要保留的正文内容很长很长占满画面")
        self.play(FadeIn(block))
        self.wait(0.1)
        tag = Text("注", font_size=18).move_to(block.get_corner(UR))
        self.play(FadeIn(tag))
        self.wait(0.1)
'''


@pytest.mark.skipif(
    os.getenv("RUN_MANIM_RENDER") != "1",
    reason="端到端渲染较慢, 需 RUN_MANIM_RENDER=1 显式开启",
)
def test_end_to_end_small_label_keeps_block(engine, tmp_path):
    """小标签压在大段落角落: 不应触发守卫删整段 (finding 1/2 的回归测试)。"""
    out = engine.inject_bounds_check(SMALL_LABEL_SRC)
    script = tmp_path / "DemoScene.py"
    script.write_text(out, encoding="utf-8")
    import subprocess
    r = subprocess.run(
        f"manim render -ql --media_dir {tmp_path}/media {script} DemoScene",
        shell=True, capture_output=True, text=True, timeout=600,
    )
    assert r.returncode == 0, "渲染失败:\n" + r.stderr[-2000:]
    assert "[overlap-guard]" not in (r.stderr + r.stdout), \
        "小标签压大段落被误判为重叠, 大段落被误删:\n" + (r.stderr + r.stdout)[-2000:]
