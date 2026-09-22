"""测试 src.distribution.xhs_cards 小红书图文卡片渲染。

- 文本切分/来源选择/字号自适应等纯逻辑(无浏览器)
- render_note_cards 的数量上限与标题清洗(mock 渲染层)
- 一个真实渲染 e2e(单张封面卡, 依赖 playwright+chromium)
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.distribution import xhs_cards as xc


# -----------------------------------------------------------------------------
# 纯逻辑
# -----------------------------------------------------------------------------

def test_split_text_to_cards_sentence_boundary():
    text = "第一句话。第二句话！第三句话？第四句话；第五句话。"
    cards = xc.split_text_to_cards(text, max_chars=12, max_cards=5)
    assert cards, "应切出卡片"
    # 每卡不应在句中硬切(除非单句超长): 卡片以句末标点结尾
    for c in cards[:-1]:
        assert c[-1] in "。！？!?；;", f"卡片未按句子边界切: {c!r}"
    # 内容无丢失
    assert "".join(cards) == text


def test_split_text_to_cards_max_cards():
    text = "很长的句子。" * 50
    cards = xc.split_text_to_cards(text, max_chars=30, max_cards=4)
    assert len(cards) <= 4


def test_split_text_empty():
    assert xc.split_text_to_cards("") == []
    assert xc.split_text_to_cards(None) == []


def test_prepare_card_texts_prefers_narration():
    narr = ["旁白一", "旁白二"]
    summ = ["摘要文本。" * 10]
    texts = xc.prepare_card_texts(narr, summ, max_cards=6)
    assert texts == ["旁白一", "旁白二"]


def test_prepare_card_texts_fallback_summaries():
    texts = xc.prepare_card_texts(None, ["第一句。第二句。第三句。"], max_cards=6)
    assert texts, "摘要降级应产出卡片文字"
    assert "第一句" in texts[0]


def test_prepare_card_texts_both_empty():
    assert xc.prepare_card_texts(None, None) == []
    assert xc.prepare_card_texts([], ["  "]) == []


def test_font_size_adaptive_monotonic():
    sizes = [xc._font_size_for("字" * n) for n in (30, 90, 150, 260)]
    assert sizes == sorted(sizes, reverse=True), "字号应随字数递减"


# -----------------------------------------------------------------------------
# render_note_cards 编排逻辑(mock 渲染层)
# -----------------------------------------------------------------------------

def _fake_render(htmls, outs):
    # 假装全部渲染成功: 创建空文件并返回
    for o in outs:
        with open(o, "wb") as f:
            f.write(b"png")
    return list(outs)


def test_render_note_cards_count_limit(tmp_path):
    """10 段旁白 + 上限 9 → 总卡数 ≤ 9。"""
    with patch.object(xc, "_render_htmls_to_pngs", side_effect=_fake_render):
        cards = xc.render_note_cards(
            title="测试标题",
            narration_segments=[f"第{i}段旁白" for i in range(10)],
            out_dir=str(tmp_path),
        )
    assert 0 < len(cards) <= xc.MAX_NOTE_IMAGES


def test_render_note_cards_no_text_returns_empty(tmp_path):
    with patch.object(xc, "_render_htmls_to_pngs", side_effect=_fake_render):
        cards = xc.render_note_cards(
            title="测试", narration_segments=None, summaries=None,
            out_dir=str(tmp_path),
        )
    assert cards == []


def test_render_note_cards_title_cleaned(tmp_path):
    """标题里的非法尖括号应被清洗(B站21009同款防线)。"""
    captured = {}

    def spy_render(htmls, outs):
        captured["htmls"] = htmls
        return _fake_render(htmls, outs)

    with patch.object(xc, "_render_htmls_to_pngs", side_effect=spy_render):
        xc.render_note_cards(
            title="<Ba-TAB: 测试标题",
            narration_segments=["一段旁白"],
            out_dir=str(tmp_path),
        )
    cover_html = captured["htmls"][0]
    assert "&lt;" not in cover_html and "<Ba-TAB" not in cover_html
    assert "Ba-TAB" in cover_html


def test_render_note_cards_paper_images_capped(tmp_path):
    """论文图超出 DEFAULT_PAPER_IMAGES 应被截断; 不存在的路径被过滤。"""
    imgs = []
    for i in range(4):
        p = tmp_path / f"fig{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        imgs.append(str(p))
    imgs.append(str(tmp_path / "not_exist.png"))

    with patch.object(xc, "_render_htmls_to_pngs", side_effect=_fake_render):
        cards = xc.render_note_cards(
            title="t", narration_segments=["一段"],
            paper_images=imgs, out_dir=str(tmp_path / "out"),
        )
    fig_cards = [c for c in cards if "_fig" in c]
    assert len(fig_cards) <= xc.DEFAULT_PAPER_IMAGES


# -----------------------------------------------------------------------------
# 真实渲染 e2e(依赖 playwright + chromium)
# -----------------------------------------------------------------------------

# 真实起 chromium: 需要 allow_browser 显式放行(conftest 默认掐断浏览器渲染层)
@pytest.mark.allow_browser
def test_render_real_single_card(tmp_path):
    pytest.importorskip("playwright.sync_api")
    cards = xc.render_note_cards(
        title="端到端渲染测试",
        narration_segments=["这是一段用于端到端渲染测试的旁白文字。"],
        out_dir=str(tmp_path),
    )
    assert len(cards) == 2  # 封面卡 + 1 叙事卡
    for c in cards:
        assert os.path.getsize(c) > 10_000, f"渲染产物过小: {c}"


# -----------------------------------------------------------------------------
# scene 帧图文合成卡
# -----------------------------------------------------------------------------

def _make_png(path):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return str(path)


def test_scene_frames_render_scene_cards(tmp_path):
    """旁白 + 对齐 scene 帧 → 叙事卡应使用图文合成模板(framewrap)。"""
    frames = [_make_png(tmp_path / f"f{i}.png") for i in range(2)]
    captured = {}

    def spy_render(htmls, outs):
        captured["htmls"] = htmls
        return _fake_render(htmls, outs)

    with patch.object(xc, "_render_htmls_to_pngs", side_effect=spy_render):
        xc.render_note_cards(
            title="t",
            narration_segments=["段一", "段二"],
            scene_frames=frames,
            out_dir=str(tmp_path / "out"),
        )
    text_htmls = captured["htmls"][1:3]
    assert all("framewrap" in h for h in text_htmls), "应使用 scene 图文卡模板"


def test_scene_frames_partial_fallback(tmp_path):
    """某段帧缺失(None/不存在) → 该段降级纯文字卡, 其余仍用图文卡。"""
    frame0 = _make_png(tmp_path / "f0.png")
    captured = {}

    def spy_render(htmls, outs):
        captured["htmls"] = htmls
        return _fake_render(htmls, outs)

    with patch.object(xc, "_render_htmls_to_pngs", side_effect=spy_render):
        xc.render_note_cards(
            title="t",
            narration_segments=["段一", "段二"],
            scene_frames=[frame0, None],
            out_dir=str(tmp_path / "out"),
        )
    h1, h2 = captured["htmls"][1], captured["htmls"][2]
    assert "framewrap" in h1
    assert "framewrap" not in h2


def test_scene_frames_ignored_for_summaries_fallback(tmp_path):
    """文字来源是 summaries 降级时, scene 帧无对应关系, 不应使用。"""
    frame0 = _make_png(tmp_path / "f0.png")
    captured = {}

    def spy_render(htmls, outs):
        captured["htmls"] = htmls
        return _fake_render(htmls, outs)

    with patch.object(xc, "_render_htmls_to_pngs", side_effect=spy_render):
        xc.render_note_cards(
            title="t",
            narration_segments=None,
            summaries=["第一句。第二句。"],
            scene_frames=[frame0],
            out_dir=str(tmp_path / "out"),
        )
    assert all("framewrap" not in h for h in captured["htmls"][1:])
