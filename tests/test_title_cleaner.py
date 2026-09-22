"""测试 ``src.utils.title_cleaner`` 的标题清洗逻辑。

重点覆盖 B站 21009 拒稿修复(非法可见符号剥离):
- ``test_strip_leading_angle_bracket`` — 复现并验证 "<Ba-TAB: ..." 开头尖括号被剥
- ``test_strip_dangling_book_quotes`` — 残缺书名号《 》被剥
- ``test_strip_invisible_control_chars`` — 零宽/方向/BOM/控制符被剥
- ``test_normal_title_unchanged`` — 正常标题(WVM 等)不被破坏
- ``test_legal_punct_preserved`` — 冒号/括号/连字符/点 等合法符号保留
- ``test_sanitize_still_removes_exaggerated`` — 夸张宣传词仍被去除
- ``test_sanitize_strips_illegal_and_exaggerated`` — 非法符号 + 夸张词一起清
- ``test_empty_and_none_safe`` — 空串 / None 安全
- ``test_fallback_when_all_stripped`` — 清完为空时走 fallback
"""
from __future__ import annotations

from src.utils.title_cleaner import (
    sanitize_generated_title,
    strip_platform_illegal_chars,
    remove_exaggerated_phrases,
)


# -----------------------------------------------------------------------------
# 非法可见符号剥离(B站 21009 修复核心)
# -----------------------------------------------------------------------------

def test_strip_leading_angle_bracket():
    """复现本次 bug: LLM 输出 "<Ba-TAB: ..." 开头带残缺尖括号 → B站 21009 拒稿。"""
    bad = "<Ba-TAB: 用翻译桥接人类到机器人技能"
    out = sanitize_generated_title(bad)
    assert out == "Ba-TAB: 用翻译桥接人类到机器人技能"
    assert "<" not in out and ">" not in out


def test_strip_dangling_book_quotes():
    """残缺书名号(只有半边)应被剥, 避免标题异常。"""
    assert strip_platform_illegal_chars("《ViTacFormer: 触觉融合") == "ViTacFormer: 触觉融合"
    assert strip_platform_illegal_chars("ViTacFormer: 触觉融合》") == "ViTacFormer: 触觉融合"
    # 成对书名号也一并去(平台风格统一用英文冒号句式, 不用书名号)
    assert "《" not in strip_platform_illegal_chars("《测试》标题")
    assert "》" not in strip_platform_illegal_chars("《测试》标题")


def test_strip_various_bracket_symbols():
    """各类中文括号/尖括号符号都被剥。"""
    for ch in "<>《》「」『』【】〈〉":
        out = strip_platform_illegal_chars(f"A{ch}B: 标题")
        assert ch not in out, f"{ch!r} 未被剥离: {out!r}"


def test_strip_invisible_control_chars():
    """零宽空格 / 方向标记 / BOM / 控制符 等不可见字符应被剥。"""
    # U+200B 零宽空格, U+202A 方向标记, U+FEFF BOM, \x07 控制符
    # 用 chr() 构造避免源码里出现不可见字符(编辑/传输易损坏)
    s = "AB" + chr(0x200B) + "C" + chr(0x202A) + "D" + chr(0xFEFF) + "E" + chr(0x07) + "F"
    assert strip_platform_illegal_chars(s) == "ABCDEF"


# -----------------------------------------------------------------------------
# 正常标题不被破坏 / 合法符号保留
# -----------------------------------------------------------------------------

def test_normal_title_unchanged():
    """已发布成功的正常标题不应被改动。"""
    for t in (
        "WVM: 让机器人看价值学操作",
        "ENPIRE: 机器人自我进化的真实世界训练",
        "BFM-Zero: 无监督训练就能控制人形机器人",
    ):
        assert sanitize_generated_title(t) == t, f"正常标题被破坏: {t!r}"


def test_legal_punct_preserved():
    """B站接受的可见符号(冒号/括号/连字符/点)必须保留。"""
    t = "pi0 (fast): 高效动作token-解读v2.0"
    assert sanitize_generated_title(t) == t


def test_camel_case_and_numbers_kept():
    """CamelCase / ALLCAPS / 数字 / π 等专有名词字符保留。"""
    t = "π0 DeeR-VLA: 动态推理做操作"
    out = sanitize_generated_title(t)
    assert "π0" in out and "DeeR-VLA" in out


# -----------------------------------------------------------------------------
# 与既有夸张词清洗协同
# -----------------------------------------------------------------------------

def test_sanitize_still_removes_exaggerated():
    """既有的夸张宣传词去除逻辑不受影响。"""
    assert "首次" not in sanitize_generated_title("ViTacFormer: 首次实现做灵巧操作")
    assert "突破" not in sanitize_generated_title("BESTRO: 突破多人博弈学习")


def test_sanitize_strips_illegal_and_exaggerated():
    """非法符号 + 夸张词 同时出现 → 都被清掉。"""
    out = sanitize_generated_title("<BESTRO: 首次突破多人博弈>")
    assert "<" not in out and ">" not in out
    assert "首次" not in out and "突破" not in out
    assert "BESTRO" in out


def test_remove_exaggerated_phrases_direct():
    """remove_exaggerated_phrases 单独调用行为不变(向后兼容)。"""
    assert "震撼" not in remove_exaggerated_phrases("震撼发布新模型")


# -----------------------------------------------------------------------------
# 边界安全
# -----------------------------------------------------------------------------

def test_empty_and_none_safe():
    assert strip_platform_illegal_chars("") == ""
    assert strip_platform_illegal_chars(None) == ""
    assert sanitize_generated_title("") == "论文解读"  # 默认 fallback
    assert sanitize_generated_title(None) == "论文解读"


def test_fallback_when_all_stripped():
    """标题全是非法符号 → 清完为空 → 走 fallback。"""
    assert sanitize_generated_title("<<>>", fallback="兜底标题") == "兜底标题"


def test_custom_fallback():
    assert sanitize_generated_title("", fallback="X") == "X"
