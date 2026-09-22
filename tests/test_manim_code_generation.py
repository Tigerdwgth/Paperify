"""ManimEngine 代码生成链路 (opencode 子进程调用 + 图分析上下文注入)。

覆盖:
1. opencode 子进程必须带 timeout —— 漏传时 except TimeoutExpired 是永远走不到的
   死分支, opencode 挂住会无限阻塞整条流水线;
2. config.yaml 读不到 key 时不能把环境里原本有效的 DEEPSEEK_API_KEY 覆盖成空串;
3. figure_analyzer 失败路径返回 {"analysis": None} 时 generate_manim_code 不能
   AttributeError 崩掉整条流水线。
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)


OPENCODE_STDOUT = (
    "some log line\n"
    "```python\n"
    "from manim import *\n\n\n"
    "class DemoScene(Scene):\n"
    "    def construct(self):\n"
    "        self.wait(1)\n"
    "```\n"
)


class _Result:
    returncode = 0
    stdout = OPENCODE_STDOUT
    stderr = ""


@pytest.fixture
def engine(tmp_path):
    from manim_engine import ManimEngine
    return ManimEngine(paper_text="论文原文", structured_plan={},
                       output_dir=str(tmp_path / "manim"))


@pytest.fixture
def captured_run(monkeypatch):
    """拦截 opencode 子进程调用, 记录传给 subprocess.run 的参数。"""
    import manim_engine as me
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(me.subprocess, "run", _fake_run)
    return captured


def test_opencode_subprocess_passes_timeout(engine, monkeypatch, captured_run):
    import manim_engine as me
    monkeypatch.setattr(engine, "_get_deepseek_key", lambda: "k")

    code = engine._opencode_generate("prompt", scene_name=None)

    assert "from manim import" in code
    assert captured_run.get("timeout") == me._OPENCODE_TIMEOUT, \
        "opencode 子进程没传 timeout, TimeoutExpired 分支永远走不到"
    assert me._OPENCODE_TIMEOUT > 0


def test_empty_config_key_does_not_clobber_env_key(engine, monkeypatch, captured_run):
    """config.yaml 读空 (cwd 不在项目根) 时沿用环境里的 key, 不覆盖成空串。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key-still-valid")
    monkeypatch.setattr(engine, "_get_deepseek_key", lambda: "")

    engine._opencode_generate("prompt", scene_name=None)

    assert captured_run["env"]["DEEPSEEK_API_KEY"] == "env-key-still-valid"


def test_config_key_overrides_env_key(engine, monkeypatch, captured_run):
    """取到非空 key 时照旧覆盖 (正常路径不受影响)。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-env-key")
    monkeypatch.setattr(engine, "_get_deepseek_key", lambda: "key-from-config")

    engine._opencode_generate("prompt", scene_name=None)

    assert captured_run["env"]["DEEPSEEK_API_KEY"] == "key-from-config"


def test_generate_manim_code_survives_none_analysis(engine, monkeypatch):
    """figure_analysis={"analysis": None} 不能崩: key 在, get 的默认值不生效。"""
    monkeypatch.setattr(engine, "_opencode_generate_with_retry",
                        lambda *a, **k: "from manim import *\n")

    scene_info = {
        "scene_name": "MethodScene",
        "type": "architecture",
        "description": "核心方法展示",
        "figure_analysis": {
            "analysis": None,          # figure_analyzer 失败路径
            "figure_type": "architecture",
            "manim_context": "",
            "eb_manim_elements": "",
        },
    }

    assert engine.generate_manim_code(scene_info) == "from manim import *"


def test_generate_manim_code_with_real_analysis(engine, monkeypatch):
    """有结构化 analysis 时组件数照常统计 (正常路径不受影响)。"""
    monkeypatch.setattr(engine, "_opencode_generate_with_retry",
                        lambda *a, **k: "from manim import *\n")

    scene_info = {
        "scene_name": "MethodScene",
        "type": "architecture",
        "description": "核心方法展示",
        "figure_analysis": {
            "analysis": {"components": [{"name": "encoder"}, {"name": "decoder"}]},
            "figure_type": "architecture",
            "manim_context": "## 图分析上下文",
            "eb_manim_elements": "",
        },
    }

    assert engine.generate_manim_code(scene_info) == "from manim import *"
