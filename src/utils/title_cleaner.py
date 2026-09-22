import re
from typing import Iterable


EXAGGERATED_TITLE_PHRASES = (
    "首次",
    "首个",
    "首款",
    "第一",
    "突破",
    "新突破",
    "最新进展",
    "重磅",
    "炸裂",
    "震撼",
    "颠覆",
    "必看",
)

# 平台(B站/抖音)标题非法可见符号: B站 21009 只允许
# "中文、英文、数字、日文等可见符号"。以下符号会被平台拒稿或造成标题异常,
# 需在落 meta / 上传前统一剥离。尖括号常来自 LLM 输出的残缺书名号 / HTML 标记
# (如 "<Ba-TAB: ...", 少了配对的 ">"), 是历史上触发 B站 21009 拒稿的主因。
_ILLEGAL_TITLE_CHARS = "<>《》「」『』【】〈〉"

# 控制字符 / 零宽字符 / 方向标记等不可见字符
_CONTROL_CHARS_RE = re.compile(
    "["
    + "".join(
        chr(c)
        for c in list(range(0x00, 0x20))       # C0 控制符
        + [0x7F]                                # DEL
        + list(range(0x200B, 0x2010))          # 零宽空格~方向标记
        + list(range(0x202A, 0x202F))          # 方向覆盖
        + [0xFEFF]                              # BOM / 零宽不换行空格
    )
    + "]"
)


def strip_platform_illegal_chars(title: str) -> str:
    """剥离平台(B站/抖音)标题非法可见符号与不可见控制字符。

    - 去掉尖括号 / 各类括号书名号符号 (``_ILLEGAL_TITLE_CHARS``);
      这类符号常来自 LLM 输出的残缺书名号 / HTML 标记, 会触发 B站 21009 拒稿。
    - 去掉控制字符 / 零宽字符 / 方向标记等不可见字符。
    - 收敛多余空白, strip 首尾常见标点。

    保留合法可见符号: 冒号(: ：)、括号 ()、连字符 -、点 . 等 —— 这些平台接受。
    """
    if not title:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", title)
    for ch in _ILLEGAL_TITLE_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ，。！？、;；-_")


def _normalize_title(text: str) -> str:
    normalized = re.sub(r"[：|]+", " ", text or "")  # 保留英文冒号:用于分隔英文名和中文描述
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" ，。！？、;；-_")


def remove_exaggerated_phrases(title: str, phrases: Iterable[str] = EXAGGERATED_TITLE_PHRASES) -> str:
    cleaned = title or ""
    for phrase in sorted(phrases, key=len, reverse=True):
        cleaned = cleaned.replace(phrase, "")
    return _normalize_title(cleaned)


def sanitize_generated_title(title: str, fallback: str = "论文解读") -> str:
    # 先剥平台非法符号(尖括号 / 残缺书名号 / 控制字符), 再去夸张宣传词。
    cleaned = strip_platform_illegal_chars(title)
    cleaned = remove_exaggerated_phrases(cleaned)
    return cleaned or fallback
