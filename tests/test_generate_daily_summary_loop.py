# -*- coding: utf-8 -*-
"""测试 ``generate_daily_arxiv_summary`` 多篇循环里的逐篇状态隔离与题注守卫。

覆盖:
- 题注提取器导入失败时是 ``None``（名字仍在 globals 里）, 守卫必须判 ``callable``;
  题注提取真失败时要留日志, 不再静默吞掉;
- ``word_budget`` 每轮必须按本篇的图片数重算, 不能沿用上一篇的值
  （旧代码的 ``'word_budget' not in dir()`` 守卫在循环里永远不成立）;
- 图片筛选后 contexts 的键要重映射成筛选后列表的位置。

只 mock 外部副作用 (arXiv / PDF / LLM / moviepy), 循环内的状态管理走真实代码。
"""
import logging
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("moviepy", reason="paperagent_workflow 依赖 moviepy")

import paperagent_workflow as wf  # noqa: E402

_DEFAULT = object()


def _make_paper(idx):
    from get_arxiv_latest import Paper
    return Paper(
        title="Fake Paper %d" % idx,
        authors=["A. Test"],
        abstract="abstract of paper %d" % idx,
        link="https://arxiv.org/abs/9999.0000%d" % idx,
        announced_date="2026-04-29T00:00:00Z",
        submitted_date="2026-04-29T00:00:00Z",
        comments="",
    )


class _FakeClip:
    duration = 1.0


class _FakeFinalClip:
    def write_videofile(self, filename, fps=24, codec=None, preset=None):
        with open(filename, "wb") as f:
            f.write(b"FAKEMP4")


def _install_loop_mocks(monkeypatch, tmp_path, *, papers, image_batches,
                        agent_errors=None, scores=None, captions_fn=_DEFAULT):
    """装配多篇日报流程所需的全部外部替身, 返回记录用的上下文。"""
    monkeypatch.chdir(tmp_path)
    for name in ("cache", "output", "pic"):
        os.makedirs(tmp_path / name, exist_ok=True)
    cached_pdf = tmp_path / "cached.pdf"
    cached_pdf.write_bytes(b"%PDF-1.4 dummy")

    ctx = {"explain_calls": [], "budget_calls": [], "agent_builds": 0}

    monkeypatch.setattr(wf, "_clean_pipeline_cache", lambda *a, **k: None)
    monkeypatch.setattr(wf, "get_paper_from_arxiv", lambda **k: list(papers))
    monkeypatch.setattr(wf, "filter_papers_by_date", lambda ps, date: list(ps))
    monkeypatch.setattr(wf, "download_if_remote", lambda url: str(cached_pdf))
    monkeypatch.setattr(wf, "_record_published_safe", lambda *a, **k: None)

    class _Proc:
        def __init__(self, path):
            ctx["proc_path"] = path

        def extract_text(self):
            return "Fake paper text. " * 50

    monkeypatch.setattr(wf, "PDFProcessor", _Proc)

    batches = [list(b) for b in image_batches]
    counter = {"n": 0}

    def _process_images(proc, cnt=None):
        batch = batches[counter["n"]] if counter["n"] < len(batches) else batches[-1]
        counter["n"] += 1
        return list(batch)

    monkeypatch.setattr(wf, "process_pdf_images", _process_images)

    errors = list(agent_errors or [])

    class _Agent:
        def __init__(self, *a, **k):
            idx = ctx["agent_builds"]
            ctx["agent_builds"] += 1
            err = errors[idx] if idx < len(errors) else None
            if err is not None:
                raise err

        def explain_images(self, images, contexts=None, **kwargs):
            ctx["explain_calls"].append({"images": list(images), "contexts": contexts})
            return [{"image_index": i, "explanation": "讲解%d" % i}
                    for i in range(len(images))]

    monkeypatch.setattr(wf, "ImageAgent", _Agent)

    if captions_fn is _DEFAULT:
        captions_fn = lambda path: {}  # noqa: E731
    monkeypatch.setattr(wf, "extract_captions_from_pdf", captions_fn)

    real_budget = wf.compute_word_budget

    def _budget_spy(target_duration=300, num_images=wf.MAX_IMAGES_DEFAULT):
        ctx["budget_calls"].append((target_duration, num_images))
        return real_budget(target_duration, num_images=num_images)

    monkeypatch.setattr(wf, "compute_word_budget", _budget_spy)

    monkeypatch.setattr(wf, "rate_image_importance",
                        lambda caps: list(scores) if scores else [5] * len(caps))
    monkeypatch.setattr(wf, "add_context_to_image_explanations", lambda expl: expl)
    monkeypatch.setattr(wf, "generate_summary", lambda *a, **k: "核心总结")
    monkeypatch.setattr(wf, "generate_short_summary", lambda *a, **k: "简短摘要")
    monkeypatch.setattr(wf, "generate_origin_title", lambda *a, **k: "Origin Title")
    monkeypatch.setattr(wf, "generate_video_title", lambda *a, **k: "测试中文标题")
    monkeypatch.setattr(wf, "get_paper_demo_website", lambda *a, **k: "")
    monkeypatch.setattr(wf, "generate_structured_video_plan", lambda *a, **k: {})
    monkeypatch.setattr(wf, "structured_plan_to_text", lambda plan: "结构化摘要文本")
    monkeypatch.setattr(wf, "get_videoclips", lambda *a, **k: [])
    monkeypatch.setattr(wf, "generate_cover", lambda *a, **k: None)

    # 合并阶段（多篇）的 moviepy 替身
    monkeypatch.setattr(wf, "VideoFileClip", lambda path: _FakeClip())
    monkeypatch.setattr(wf, "TextClip", lambda **k: _FakeClip())
    monkeypatch.setattr(wf, "concatenate_videoclips", lambda clips, method=None: _FakeClip())
    monkeypatch.setattr(wf, "CompositeVideoClip", lambda clips: _FakeFinalClip())
    return ctx


def _run(**kwargs):
    params = dict(query="cs.RO", max_papers=5, date="2026-04-29",
                  long_or_short="short", target_duration=120, skip_main_video=True)
    params.update(kwargs)
    return wf.generate_daily_arxiv_summary(**params)


# ============================================================================
# word_budget 逐篇重算
# ============================================================================

def test_word_budget_recomputed_for_each_paper(monkeypatch, tmp_path):
    """第 1 篇成功、第 2 篇图像解释失败时, 第 2 篇必须按自己的图片数重算预算。"""
    papers = [_make_paper(1), _make_paper(2)]
    ctx = _install_loop_mocks(
        monkeypatch, tmp_path,
        papers=papers,
        image_batches=[["img%d" % i for i in range(8)], ["only-one"]],
        agent_errors=[None, RuntimeError("缺少 API key")],
        scores=[1, 9, 1, 8, 1, 7, 6, 5],
    )

    path, titles, cn_titles, summaries, paper_links, project_links = _run()

    assert len(cn_titles) == 2, "两篇论文都应处理完"
    # 120 秒 / 2 篇 = 每篇 60 秒; 第 1 篇筛后 5 张, 第 2 篇 1 张
    assert ctx["budget_calls"] == [(60, 5), (60, 1)], (
        "第 2 篇必须重算 word_budget, 不能沿用第 1 篇按 5 张图算出的预算")
    assert os.path.exists(path)


def test_word_budget_not_recomputed_when_try_block_succeeds(monkeypatch, tmp_path):
    """图像解释正常时每篇只算一次预算（兜底分支不应重复触发）。"""
    papers = [_make_paper(1), _make_paper(2)]
    ctx = _install_loop_mocks(
        monkeypatch, tmp_path,
        papers=papers,
        image_batches=[["a", "b"], ["c"]],
    )

    _run()

    assert ctx["budget_calls"] == [(60, 2), (60, 1)]


# ============================================================================
# 题注提取守卫
# ============================================================================

def test_missing_caption_extractor_is_skipped_quietly(monkeypatch, tmp_path, caplog):
    """题注模块导入失败 (名字为 None) 时直接跳过, 不靠吞 TypeError。"""
    ctx = _install_loop_mocks(
        monkeypatch, tmp_path,
        papers=[_make_paper(1)],
        image_batches=[["img0", "img1"]],
        captions_fn=None,
    )

    with caplog.at_level(logging.WARNING):
        _run()

    assert ctx["explain_calls"][0]["contexts"] == {}
    assert "提取 PDF 题注失败" not in caplog.text, "None 应被 callable 守卫挡住, 而不是调用后报错"


def test_caption_extractor_failure_is_logged(monkeypatch, tmp_path, caplog):
    """题注提取真的失败时必须留日志（旧代码是零日志的裸 except）。"""
    def _boom(path):
        raise ValueError("PyMuPDF 解析失败")

    ctx = _install_loop_mocks(
        monkeypatch, tmp_path,
        papers=[_make_paper(1)],
        image_batches=[["img0"]],
        captions_fn=_boom,
    )

    with caplog.at_level(logging.WARNING):
        _run()

    assert "提取 PDF 题注失败" in caplog.text
    assert ctx["explain_calls"][0]["contexts"] == {}


def test_caption_extractor_success_builds_contexts(monkeypatch, tmp_path):
    """题注提取可用时, contexts 按图号正常构造（老行为不变）。"""
    ctx = _install_loop_mocks(
        monkeypatch, tmp_path,
        papers=[_make_paper(1)],
        image_batches=[["img0", "img1"]],
        captions_fn=lambda path: {"fig_1": "题注1", "fig_2": "题注2"},
    )

    _run()

    assert ctx["explain_calls"][0]["contexts"] == {1: "题注1", 2: "题注2"}


# ============================================================================
# 图片筛选后的 contexts 重映射
# ============================================================================

def test_contexts_remapped_after_selection_in_loop(monkeypatch, tmp_path):
    """循环里筛图后, contexts 的键必须是筛选后列表的位置。"""
    captions = {"fig_%d" % (i + 1): "题注%d" % (i + 1) for i in range(8)}
    ctx = _install_loop_mocks(
        monkeypatch, tmp_path,
        papers=[_make_paper(1)],
        image_batches=[["img%d" % i for i in range(8)]],
        scores=[1, 9, 1, 8, 1, 7, 6, 5],
        captions_fn=lambda path: dict(captions),
    )

    _run()

    call = ctx["explain_calls"][0]
    assert call["images"] == ["img1", "img3", "img5", "img6", "img7"]
    assert call["contexts"] == {1: "题注2", 2: "题注4", 3: "题注6", 4: "题注7", 5: "题注8"}
