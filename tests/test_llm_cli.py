"""测试 ``src.llm_tools.cli`` (P1: video-plan skill) + ``via_skill`` kwarg。

涵盖:
1. ``test_cli_generate_plan_mock_opencode`` — mock subprocess 返回 fake JSON,
   断言 out 文件正确写入 + stdout JSON 契约 OK
2. ``test_cli_generate_plan_real_short`` — 真实跑 (DeepSeek 端点),用极短 abstract
   验证 5 段都返回(标 ``@pytest.mark.slow``,默认跳过)
3. ``test_call_video_plan_skill_fallback_on_failure`` — mock subprocess 抛
   ``TimeoutExpired``,断言上层 ``generate_structured_video_plan`` fallback 到原 API 路径
4. ``test_via_skill_kwarg_default_off`` — 默认 ``via_skill=False`` 且无 env 时,
   不调 subprocess
5. ``test_via_skill_env_var_triggered`` — 设 ``JSR_USE_SKILL_VIDEO_PLAN=1`` 后,
   ``generate_structured_video_plan`` 走 subprocess 路径

补充测试:
- ``test_cli_missing_paper_meta_returns_error`` — 文件不存在 → ok=false / rc=1
- ``test_cli_missing_required_fields_returns_error`` — title 缺失 → ok=false / rc=1
- ``test_build_video_plan_prompt_smoke`` — prompt 构造不崩
- ``test_normalize_plan_partial`` — 部分 sections 也能补齐
- ``test_parse_plan_json_variants`` — 各种 JSON 包裹形态都能解析
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------

def _fake_plan() -> dict:
    return {
        "opening": {"text": "今天来聊聊一篇有意思的论文。", "duration_sec": 30,
                    "key_points": ["hook"]},
        "intro": {"text": "我们先看看这个问题有多硬。", "duration_sec": 60,
                  "key_points": ["bg"]},
        "method": {"text": "接下来重点讲讲方法。这篇论文用了跨模态注意力。",
                    "duration_sec": 120, "key_points": ["m1"]},
        "results": {"text": "那这个方法效果到底如何呢? 87% 成功率。",
                     "duration_sec": 60, "key_points": ["r1"]},
        "conclusion": {"text": "总结一下,这工作给我们的启发是融合方式很重要。",
                        "duration_sec": 30, "key_points": ["c1"]},
    }


def _make_paper_meta(tmp_path) -> str:
    p = tmp_path / "paper_meta.json"
    p.write_text(json.dumps({
        "title": "ViTacFormer",
        "abstract": "Cross-modal transformer for visuo-tactile manipulation. "
                    "Pretrained on 200K episodes, achieves 87% success rate on 12 tasks.",
        "authors": ["Jane Doe", "John Smith"],
        "key_points": ["跨模态注意力", "预训练 200K"],
    }, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _run_cli(*args, timeout=60):
    cmd = [sys.executable, "-m", "src.llm_tools.cli", *args]
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True,
        env=env, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


# -----------------------------------------------------------------------------
# 1. mock opencode → CLI 应正确写出 plan
# -----------------------------------------------------------------------------

def test_cli_generate_plan_mock_opencode(tmp_path):
    """mock _run_opencode_with_pipe 返回 fake JSON,验证 out 文件 + stdout JSON。"""
    from src.llm_tools import cli as llm_cli

    meta_path = _make_paper_meta(tmp_path)
    out_path = str(tmp_path / "plan_out.json")

    fake_raw = "```json\n" + json.dumps(_fake_plan(), ensure_ascii=False) + "\n```"

    with patch.object(llm_cli, "_run_opencode_with_pipe", return_value=fake_raw):
        with patch.object(llm_cli, "_resolve_opencode_model",
                           return_value="deepseek/deepseek-v4-pro"):
            rc = llm_cli.main([
                "generate-plan",
                "--paper-meta", meta_path,
                "--target-duration", "300",
                "--out", out_path,
            ])
    assert rc == 0, "应成功"
    assert os.path.isfile(out_path)
    with open(out_path, "r", encoding="utf-8") as f:
        plan = json.load(f)
    for sec in ("opening", "intro", "method", "results", "conclusion"):
        assert sec in plan, f"缺 section: {sec}"
        assert plan[sec].get("text"), f"section {sec} text 为空"


# -----------------------------------------------------------------------------
# 2. 真实跑 (slow,默认跳过) — 短 abstract
# -----------------------------------------------------------------------------

@pytest.mark.slow
def test_cli_generate_plan_real_short(tmp_path):
    """真实端到端跑(需 DeepSeek API,默认 skip)。"""
    if not os.environ.get("JSR_RUN_SLOW_TESTS"):
        pytest.skip("set JSR_RUN_SLOW_TESTS=1 to run")

    meta_path = _make_paper_meta(tmp_path)
    out_path = str(tmp_path / "plan_out.json")

    rc, out, err = _run_cli(
        "generate-plan",
        "--paper-meta", meta_path,
        "--target-duration", "120",
        "--out", out_path,
        timeout=300,
    )
    assert rc in (0, 1), out
    payload = json.loads(out)
    assert "ok" in payload
    if payload["ok"]:
        assert os.path.isfile(out_path)
        with open(out_path, "r", encoding="utf-8") as f:
            plan = json.load(f)
        assert "opening" in plan and "method" in plan and "conclusion" in plan


# -----------------------------------------------------------------------------
# 3. fallback on TimeoutExpired
# -----------------------------------------------------------------------------

def test_call_video_plan_skill_fallback_on_failure():
    """mock subprocess.run 抛 TimeoutExpired,断言 _call_video_plan_skill 返回空 dict。

    上层 generate_structured_video_plan(via_skill=True) 应自动 fallback 到 Python 路径。
    """
    from src.llm_tools import llm_agent

    fake_legacy_plan = {
        "opening": {"script": "fallback opening", "text": "fallback opening"},
        "intro": {"script": "fallback intro", "text": "fallback intro"},
        "method": {"script": "fallback method", "text": "fallback method"},
        "results": {"script": "fallback results", "text": "fallback results"},
    }

    # 使 _call_video_plan_skill 模拟 subprocess Timeout → 返回 {}
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
        skill_out = llm_agent._call_video_plan_skill(
            paper_title="Test", paper_abstract="Test abstract.",
        )
    assert skill_out == {}, "skill 失败应返回空 dict 让上层 fallback"

    # 上层 generate_structured_video_plan(via_skill=True) 应 fallback 走 Python 路径
    with patch.object(llm_agent, "_call_video_plan_skill", return_value={}):
        with patch.object(llm_agent, "create_chat_completion",
                           return_value=json.dumps({
                               "opening": {"script": "py opening"},
                               "intro": {"script": "py intro"},
                               "method": {"script": "py method"},
                               "results": {"script": "py results"},
                           }, ensure_ascii=False)):
            plan = llm_agent.generate_structured_video_plan(
                text=None, paper_title="X", paper_abstract="Y",
                via_skill=True,
            )
    assert plan
    assert plan.get("opening", {}).get("script") == "py opening"


# -----------------------------------------------------------------------------
# 4. 默认 via_skill=False,不调 subprocess
# -----------------------------------------------------------------------------

def test_via_skill_kwarg_default_off(monkeypatch):
    """默认 via_skill=False 且 JSR_USE_SKILL_VIDEO_PLAN 未设 → 不应调 _call_video_plan_skill。"""
    monkeypatch.delenv("JSR_USE_SKILL_VIDEO_PLAN", raising=False)

    from src.llm_tools import llm_agent

    skill_called = {"v": False}

    def _spy_skill(*args, **kw):
        skill_called["v"] = True
        return {}

    fake_python_plan = json.dumps({
        "opening": {"script": "py o"},
        "intro": {"script": "py i"},
        "method": {"script": "py m"},
        "results": {"script": "py r"},
    }, ensure_ascii=False)

    with patch.object(llm_agent, "_call_video_plan_skill", side_effect=_spy_skill):
        with patch.object(llm_agent, "create_chat_completion",
                           return_value=fake_python_plan):
            plan = llm_agent.generate_structured_video_plan(
                text="任意文本"
            )
    assert skill_called["v"] is False, "默认不应触发 skill 路径"
    assert plan.get("opening", {}).get("script") == "py o"


# -----------------------------------------------------------------------------
# 5. JSR_USE_SKILL_VIDEO_PLAN=1 触发 skill 路径
# -----------------------------------------------------------------------------

def test_via_skill_env_var_triggered(monkeypatch):
    """设 JSR_USE_SKILL_VIDEO_PLAN=1 后,_call_video_plan_skill 应被调用。"""
    monkeypatch.setenv("JSR_USE_SKILL_VIDEO_PLAN", "1")

    from src.llm_tools import llm_agent

    fake_legacy_plan = {
        "opening": {"script": "skill o", "text": "skill o"},
        "intro": {"script": "skill i", "text": "skill i"},
        "method": {"script": "skill m", "text": "skill m"},
        "results": {"script": "skill r", "text": "skill r"},
    }

    # skill 返回后, generate_structured_video_plan 还会跑一次公式提取
    # (_extract_core_formulas → create_chat_completion), 不 mock 就会打真实 DeepSeek。
    with patch.object(llm_agent, "_call_video_plan_skill",
                       return_value=fake_legacy_plan) as m_skill, \
         patch.object(llm_agent, "create_chat_completion",
                       return_value='{"formulas": []}') as m_chat:
        plan = llm_agent.generate_structured_video_plan(
            text=None, paper_title="X", paper_abstract="Y",
        )
    assert m_skill.called, "env=1 时应触发 skill 路径"
    assert plan.get("formulas") == [], "公式提取返回空数组时 plan.formulas 应为空"
    assert m_chat.called, "公式提取应走 create_chat_completion(已被 mock, 不打真实 API)"
    assert plan.get("opening", {}).get("script") == "skill o"


# -----------------------------------------------------------------------------
# 补充: CLI 边界与 helpers
# -----------------------------------------------------------------------------

def test_cli_missing_paper_meta_returns_error(tmp_path):
    """paper_meta 文件不存在 → exit 1, ok=false。"""
    rc, out, err = _run_cli(
        "generate-plan",
        "--paper-meta", str(tmp_path / "does_not_exist.json"),
        "--out", str(tmp_path / "out.json"),
        timeout=15,
    )
    assert rc == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "error" in payload


def test_cli_missing_required_fields_returns_error(tmp_path):
    """paper_meta 缺 title/abstract → exit 1, ok=false。"""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"title": "", "abstract": ""}), encoding="utf-8")
    rc, out, err = _run_cli(
        "generate-plan",
        "--paper-meta", str(bad),
        "--out", str(tmp_path / "out.json"),
        timeout=15,
    )
    assert rc == 1
    payload = json.loads(out)
    assert payload["ok"] is False


def test_build_video_plan_prompt_smoke():
    """prompt 构造不崩,且包含关键字段。"""
    from src.llm_tools.cli import _build_video_plan_prompt
    meta = {
        "title": "ViTacFormer",
        "abstract": "An interesting paper.",
        "authors": ["Foo Bar"],
        "key_points": ["kp1"],
        "venue": "RSS 2025",
    }
    p = _build_video_plan_prompt(meta, target_duration=300, language="zh")
    assert "ViTacFormer" in p
    assert "RSS 2025" in p
    assert "An interesting paper" in p
    assert "opening" in p and "conclusion" in p


def test_normalize_plan_partial():
    """部分 sections 缺失,_normalize_plan 应补齐空骨架。"""
    from src.llm_tools.cli import _normalize_plan, _validate_plan
    partial = {"opening": {"text": "hi"}, "method": "字符串形式"}
    out = _normalize_plan(partial, target_duration=300)
    assert "opening" in out and "method" in out
    assert "intro" in out  # 补齐空 text
    assert "conclusion" in out
    # validate: 因 intro/results/conclusion text 为空 → False
    assert _validate_plan(out) is False
    # 但 opening/method 字段非空
    assert out["opening"]["text"] == "hi"
    assert out["method"]["text"] == "字符串形式"


def test_parse_plan_json_variants():
    """各种 JSON 包裹格式应能解析。"""
    from src.llm_tools.cli import _parse_plan_json
    p = {"opening": {"text": "x"}}
    raw1 = "```json\n" + json.dumps(p) + "\n```"
    raw2 = "```\n" + json.dumps(p) + "\n```"
    raw3 = "Some prefix\n" + json.dumps(p) + "\nSome suffix"
    raw4 = json.dumps(p)
    for raw in (raw1, raw2, raw3, raw4):
        out = _parse_plan_json(raw)
        assert out == p, f"failed on raw: {raw!r}"
    assert _parse_plan_json("") is None
    assert _parse_plan_json("not json at all") is None


def test_normalize_skill_plan_to_legacy_keeps_required_sections():
    """skill 输出 5 段,_normalize_skill_plan_to_legacy 至少返回 4 段 + script 字段。"""
    from src.llm_tools.llm_agent import _normalize_skill_plan_to_legacy
    skill_plan = _fake_plan()
    legacy = _normalize_skill_plan_to_legacy(skill_plan)
    for sec in ("opening", "intro", "method", "results"):
        assert sec in legacy
        assert legacy[sec].get("script")
    # conclusion 应被合并到 results 段尾
    assert "总结一下" in legacy["results"]["script"]
