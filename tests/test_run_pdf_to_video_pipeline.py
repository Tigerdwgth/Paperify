# -*- coding: utf-8 -*-
"""测试 ``run_pdf_to_video_pipeline``（单篇 PDF 入口）的几处健壮性缺陷修复。

覆盖:
- ``download_if_remote`` 返回 -1 哨兵时必须报错, 不能把 -1 当路径喂给 PDFProcessor;
- ImageAgent 初始化失败后, 摘要回退分支不能因 ``paper_core_summary`` 未绑定炸 NameError;
- 图像解释缓存读不到时, 回退到内存里刚算出的 explanations（而不是恒为 None）;
- LLM 标题含 ``/`` ``:`` 等字符时必须净化后再拼输出路径;
- 图片筛选后 contexts 的键要重映射成筛选后列表的位置, 否则题注与图片错配。

只 mock 外部副作用 (PDF 解析 / LLM / 视频合成), 被测的分支判断与索引重映射走真实代码。
"""
import json
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


# ============================================================================
# 替身
# ============================================================================

def _make_image_agent(record, error=None):
    """ImageAgent 替身: 记录 explain_images 收到的图片与 contexts。"""

    class _Agent:
        def __init__(self, *args, **kwargs):
            if error is not None:
                raise error

        def explain_images(self, images, contexts=None, **kwargs):
            record["images"] = list(images)
            record["contexts"] = contexts
            record["kwargs"] = kwargs
            return [{"image_index": i, "explanation": "讲解%d" % i}
                    for i in range(len(images))]

    return _Agent


def _make_video_creator(record):
    """VideoCreator 替身: 记录入参并真写出文件（目录不存在会暴露路径未净化）。"""

    class _VideoCreator:
        def __init__(self, images, summary, videos=None, image_explanations=None,
                     target_duration=300, structured_plan=None, **kwargs):
            record["images"] = list(images)
            record["summary"] = summary
            record["image_explanations"] = image_explanations
            record["structured_plan"] = structured_plan

        def create_video(self, save_path):
            record.setdefault("save_paths", []).append(save_path)
            with open(save_path, "wb") as f:
                f.write(b"FAKEMP4")
            return save_path

    return _VideoCreator


def _install_pipeline_mocks(monkeypatch, tmp_path, *, images, captions=None, scores=None,
                            image_agent_error=None, video_title="测试中文标题",
                            structured_plan_error=None):
    """装配单篇流水线所需的全部外部替身, 返回记录用的上下文。"""
    monkeypatch.chdir(tmp_path)
    for name in ("cache", "output", "pic"):
        os.makedirs(tmp_path / name, exist_ok=True)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    ctx = {"agent": {}, "vc": {}, "pdf": str(pdf), "call_llm": []}

    monkeypatch.setattr(wf, "_clean_pipeline_cache", lambda *a, **k: None)

    class _Proc:
        def __init__(self, path):
            ctx["proc_path"] = path

        def extract_text(self):
            return "Fake paper text. " * 50

    monkeypatch.setattr(wf, "PDFProcessor", _Proc)
    monkeypatch.setattr(wf, "process_pdf_images", lambda proc, cnt=None: list(images))

    import src.extract_caption as extract_caption
    monkeypatch.setattr(extract_caption, "extract_captions_from_pdf",
                        lambda path: dict(captions or {}))

    monkeypatch.setattr(wf, "ImageAgent", _make_image_agent(ctx["agent"], image_agent_error))
    monkeypatch.setattr(wf, "add_context_to_image_explanations", lambda expl: expl)
    monkeypatch.setattr(wf, "rate_image_importance",
                        lambda caps: list(scores) if scores else [5] * len(caps))
    monkeypatch.setattr(wf, "generate_summary", lambda *a, **k: "核心总结")
    monkeypatch.setattr(wf, "get_videoclips", lambda *a, **k: [])
    monkeypatch.setattr(wf, "generate_video_title", lambda *a, **k: video_title)
    monkeypatch.setattr(wf, "structured_plan_to_text", lambda plan: "结构化摘要文本")
    monkeypatch.setattr(wf, "generate_cover", lambda *a, **k: None)

    def _plan(*a, **k):
        if structured_plan_error is not None:
            raise structured_plan_error
        return {"opening": "op", "intro": "in", "method": "me",
                "results": "re", "conclusion": "co"}

    monkeypatch.setattr(wf, "generate_structured_video_plan", _plan)

    def _call_llm(text, word_budget=1000):
        ctx["call_llm"].append(word_budget)
        return "兜底标题", "兜底摘要"

    monkeypatch.setattr(wf, "call_llm", _call_llm)
    monkeypatch.setattr(wf, "VideoCreator", _make_video_creator(ctx["vc"]))
    return ctx


# ============================================================================
# -1 哨兵
# ============================================================================

def test_download_failure_sentinel_raises_before_pdf_processor(monkeypatch, tmp_path):
    """download_if_remote 返回 -1 时立即报错, PDFProcessor 一次都不该被构造。"""
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "cache", exist_ok=True)
    monkeypatch.setattr(wf, "_clean_pipeline_cache", lambda *a, **k: None)

    constructed = []
    monkeypatch.setattr(wf, "PDFProcessor", lambda path: constructed.append(path))

    missing = str(tmp_path / "nope.pdf")
    with pytest.raises(RuntimeError) as excinfo:
        wf.run_pdf_to_video_pipeline(pdf_file_path=missing)

    assert missing in str(excinfo.value), "报错信息应带上失败的输入路径"
    assert constructed == [], "-1 哨兵不得被当成路径喂给 PDFProcessor"


# ============================================================================
# ImageAgent 失败后的摘要回退（paper_core_summary 预绑定）
# ============================================================================

def test_image_agent_failure_keeps_summary_fallback_working(monkeypatch, tmp_path):
    """ImageAgent 构造失败 + 结构化脚本失败: 走 call_llm 回退, 不得抛 NameError。"""
    ctx = _install_pipeline_mocks(
        monkeypatch, tmp_path,
        images=["img0"],
        image_agent_error=RuntimeError("缺少 API key"),
        structured_plan_error=ValueError("LLM 返回畸形 JSON"),
    )

    video_path = wf.run_pdf_to_video_pipeline(pdf_file_path=ctx["pdf"])

    assert os.path.exists(video_path)
    assert len(ctx["call_llm"]) == 1, "paper_core_summary 不可用时应调用 call_llm 重新生成"
    assert ctx["vc"]["summary"] == "兜底摘要"


def test_image_agent_ok_reuses_core_summary_on_plan_failure(monkeypatch, tmp_path):
    """ImageAgent 成功时, 结构化脚本失败要复用 paper_core_summary, 不重复调 LLM。"""
    ctx = _install_pipeline_mocks(
        monkeypatch, tmp_path,
        images=["img0"],
        structured_plan_error=ValueError("LLM 返回畸形 JSON"),
    )

    wf.run_pdf_to_video_pipeline(pdf_file_path=ctx["pdf"])

    assert ctx["call_llm"] == [], "已有 paper_core_summary 时不应再调 call_llm"
    assert ctx["vc"]["summary"] == "核心总结"


# ============================================================================
# 图像解释缓存读不到时回退到内存结果
# ============================================================================

def test_image_explanations_fall_back_to_in_memory(monkeypatch, tmp_path):
    """缓存 JSON 写坏时, VideoCreator 仍应拿到内存里刚算出的图像解释。"""
    ctx = _install_pipeline_mocks(monkeypatch, tmp_path, images=["img0", "img1"])

    real_dump = json.dump

    def _dump(obj, fp, **kwargs):
        # 模拟写缓存中途失败: 文件已建但内容不合法, 下游 json.load 会炸
        if getattr(fp, "name", "").endswith("image_explanations.json"):
            raise TypeError("对象不可序列化")
        return real_dump(obj, fp, **kwargs)

    monkeypatch.setattr(wf.json, "dump", _dump)

    wf.run_pdf_to_video_pipeline(pdf_file_path=ctx["pdf"])

    assert ctx["vc"]["image_explanations"] == [
        {"image_index": 0, "explanation": "讲解0"},
        {"image_index": 1, "explanation": "讲解1"},
    ], "缓存不可用时必须回退到内存里的 explanations, 而不是 None"


def test_image_explanations_read_from_cache_when_available(monkeypatch, tmp_path):
    """缓存 JSON 正常时仍从文件读取（老行为不变）。"""
    ctx = _install_pipeline_mocks(monkeypatch, tmp_path, images=["img0"])

    wf.run_pdf_to_video_pipeline(pdf_file_path=ctx["pdf"])

    assert os.path.exists("./cache/image_explanations.json")
    assert ctx["vc"]["image_explanations"] == [{"image_index": 0, "explanation": "讲解0"}]


# ============================================================================
# 标题净化
# ============================================================================

def test_llm_title_with_slash_is_sanitized_in_output_path(monkeypatch, tmp_path):
    """标题含 / 和 : 时输出路径必须仍落在 ./output 下, 且文件真的写出来。"""
    ctx = _install_pipeline_mocks(
        monkeypatch, tmp_path,
        images=["img0"],
        video_title="Vision/Language: 统一模型",
    )

    video_path = wf.run_pdf_to_video_pipeline(pdf_file_path=ctx["pdf"])

    save_path = ctx["vc"]["save_paths"][0]
    assert save_path == "./output/VisionLanguage 统一模型.mp4"
    assert os.path.exists(video_path)


def test_sanitize_filename_rules():
    """净化函数本身: 剔除非法字符, 清空后回落 fallback, 正常标题不受影响。"""
    assert wf.sanitize_filename("Vision/Language: 统一模型") == "VisionLanguage 统一模型"
    assert wf.sanitize_filename('a\\b*c?d"e<f>g|h') == "abcdefgh"
    assert wf.sanitize_filename("  正常标题  ") == "正常标题"
    assert wf.sanitize_filename("///", "daily_summary") == "daily_summary"
    assert wf.sanitize_filename("", "daily_summary") == "daily_summary"
    assert wf.sanitize_filename(None, "daily_summary") == "daily_summary"


# ============================================================================
# 图片筛选后 contexts 重映射
# ============================================================================

def test_contexts_remapped_after_image_selection(monkeypatch, tmp_path):
    """8 张图筛到 5 张后, 传给 ImageAgent 的 contexts 必须按新位置重排。"""
    images = ["img%d" % i for i in range(8)]
    captions = {"fig_%d" % (i + 1): "题注%d" % (i + 1) for i in range(8)}
    # 分数让 0-based 下标 1,3,5,6,7 入选
    scores = [1, 9, 1, 8, 1, 7, 6, 5]
    ctx = _install_pipeline_mocks(monkeypatch, tmp_path, images=images,
                                  captions=captions, scores=scores)

    wf.run_pdf_to_video_pipeline(pdf_file_path=ctx["pdf"])

    assert ctx["agent"]["images"] == ["img1", "img3", "img5", "img6", "img7"]
    assert ctx["agent"]["contexts"] == {
        1: "题注2", 2: "题注4", 3: "题注6", 4: "题注7", 5: "题注8",
    }, "contexts 的键必须是筛选后列表的位置, 否则题注与图片错配"


def test_contexts_untouched_when_no_selection(monkeypatch, tmp_path):
    """图片数不超过上限时不触发筛选, contexts 保持原始 1-based 图号。"""
    images = ["img0", "img1", "img2"]
    captions = {"fig_1": "题注1", "fig_2": "题注2", "fig_3": "题注3"}
    ctx = _install_pipeline_mocks(monkeypatch, tmp_path, images=images, captions=captions)

    wf.run_pdf_to_video_pipeline(pdf_file_path=ctx["pdf"])

    assert ctx["agent"]["images"] == images
    assert ctx["agent"]["contexts"] == {1: "题注1", 2: "题注2", 3: "题注3"}


# ============================================================================
# 重映射辅助函数
# ============================================================================

def test_select_top_images_with_contexts_matches_select_top_images():
    """挑选结果必须与 select_top_images 完全一致（只是多返回重映射的 contexts）。"""
    images = ["a", "b", "c", "d", "e", "f", "g"]
    scores = [3, 10, 1, 9, 2, 8, 7]
    contexts = {i + 1: "cap%d" % (i + 1) for i in range(len(images))}

    selected, remapped = wf._select_top_images_with_contexts(images, scores, contexts, top_n=3)

    assert selected == wf.select_top_images(images, scores, top_n=3)
    assert selected == ["b", "d", "f"]
    assert remapped == {1: "cap2", 2: "cap4", 3: "cap6"}


def test_select_top_images_with_contexts_none_contexts():
    """contexts 为 None 时原样返回 None, 不构造空字典。"""
    selected, remapped = wf._select_top_images_with_contexts(
        ["a", "b", "c"], [1, 2, 3], None, top_n=2)
    assert selected == ["b", "c"]
    assert remapped is None


def test_select_top_images_with_contexts_partial_captions():
    """部分图片没有题注时, 只重映射存在的键, 不塞 None。"""
    images = ["a", "b", "c", "d"]
    scores = [1, 5, 2, 6]
    contexts = {2: "cap2"}  # 只有原始第 2 张有题注

    selected, remapped = wf._select_top_images_with_contexts(images, scores, contexts, top_n=2)

    assert selected == ["b", "d"]
    assert remapped == {1: "cap2"}


def test_select_top_images_with_contexts_no_truncation_needed():
    """图片数不超过 top_n 时, 图片与 contexts 都原样保留。"""
    images = ["a", "b"]
    contexts = {1: "cap1", 2: "cap2"}
    selected, remapped = wf._select_top_images_with_contexts(images, [1, 2], contexts, top_n=5)
    assert selected == images
    assert remapped == contexts
