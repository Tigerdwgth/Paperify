"""ManimEngine.load_pipeline_images 的 script.json 归属判定。

./output 与 ./src/output 下堆着十来个跨月份的历史 *_script.json, 老实现 glob 到
第一个就用, 会拿别的论文的 caption 覆盖本次图片说明, 把配图分错桶 (method/results)。
这里锁定: 只有"本次论文"的 json 才被采用, 判不出就保留 image_explanations 的 caption。
"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)


ARCH_CAPTION = "模型架构 architecture 总览"
TRAIN_CAPTION = "训练细节说明"
OTHER_PAPER_CAPTION = "别的论文的 experiment results table 实验结果"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """构造一个干净的 pipeline 工作目录并 chdir 过去 (所有路径都是相对 cwd 的)。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pic").mkdir()
    for i in range(2):
        (tmp_path / "pic" / f"{i}.png").write_bytes(b"png")
    (tmp_path / "cache").mkdir()
    (tmp_path / "output").mkdir()
    return tmp_path


@pytest.fixture
def engine(tmp_path):
    from manim_engine import ManimEngine
    return ManimEngine(paper_text="", structured_plan={},
                       output_dir=str(tmp_path / "manim_out"))


def _write_current_run_cache(workdir, captions):
    """写本次 pipeline 的 image_explanations.json + paper_text.txt (时间基准)。"""
    (workdir / "cache" / "paper_text.txt").write_text("本次论文正文", encoding="utf-8")
    data = [{"caption": c, "context": c, "figure_role": "",
             "recommended_section": "method"} for c in captions]
    (workdir / "cache" / "image_explanations.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _write_script_json(path, captions, age_days=0):
    payload = {
        "title": "某篇论文",
        "images": [{"image_index": i, "caption": c, "context": c}
                   for i, c in enumerate(captions)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    ts = time.time() - age_days * 86400
    os.utime(path, (ts, ts))
    return path


def test_stale_script_json_is_ignored(engine, workdir):
    """几个月前别的论文的 script.json 不能覆盖本次 caption (否则配图分错桶)。"""
    _write_current_run_cache(workdir, [ARCH_CAPTION, TRAIN_CAPTION])
    _write_script_json(workdir / "output" / "old_paper_script.json",
                       [OTHER_PAPER_CAPTION], age_days=90)

    img_map = engine.load_pipeline_images()

    pic0 = os.path.join(".", "pic", "0.png")
    assert pic0 in img_map["method"], "本次 caption 被历史 json 覆盖, 图分错桶了"
    assert img_map["results"] == [], "用了别的论文的 results caption"


def test_current_run_script_json_is_used(engine, workdir):
    """本次 pipeline 产出的 script.json 照常采用 (正常路径不受影响)。"""
    _write_current_run_cache(workdir, [ARCH_CAPTION, TRAIN_CAPTION])
    path = _write_script_json(
        workdir / "output" / "part_1_script.json",
        [ARCH_CAPTION, "experiment results table 实验结果"])
    now = time.time()
    os.utime(path, (now, now))  # 本次 pipeline 内写出, 晚于 cache 标记

    img_map = engine.load_pipeline_images()

    pic1 = os.path.join(".", "pic", "1.png")
    assert pic1 in img_map["results"], "本次论文的 script.json 没被采用"


def test_fresh_but_foreign_script_json_is_rejected(engine, workdir):
    """mtime 够新但 caption 和本次论文完全对不上 -> 判定不是本次的, 不采用。"""
    explanations = _write_current_run_cache(workdir, [ARCH_CAPTION, TRAIN_CAPTION])
    path = _write_script_json(workdir / "output" / "foreign_script.json",
                              [OTHER_PAPER_CAPTION])
    now = time.time()
    os.utime(path, (now, now))

    assert engine._load_current_script_json(explanations) is None


def test_no_cache_marker_still_rejects_old_json(engine, workdir):
    """没有 cache 标记文件时, 靠新鲜度窗口兜住: 90 天前的 json 一律不认。"""
    _write_script_json(workdir / "output" / "old_paper_script.json",
                       [OTHER_PAPER_CAPTION], age_days=90)

    assert engine._load_current_script_json([]) is None


def test_missing_script_json_keeps_explanation_captions(engine, workdir):
    """一个 script.json 都没有时, 保留 image_explanations 的 caption。"""
    _write_current_run_cache(workdir, [ARCH_CAPTION, TRAIN_CAPTION])

    assert engine._load_current_script_json([]) is None
    img_map = engine.load_pipeline_images()
    assert os.path.join(".", "pic", "0.png") in img_map["method"]
