"""小红书图文卡片渲染。

把标题 / 旁白文字排版进 3:4 竖版 (1242x1656) 卡片图, 供 publish_note 图文发布:

- 封面卡: 封面图做底 + 底部渐变遮罩 + 中文标题大字
- 叙事卡: 每段旁白一张, 大字号中文排版 + 页码角标
- 论文图卡: 论文原图白底居中 + 「论文原图」角标

复用 js_anim_engine 的 chromium 定位逻辑与 playwright 截图;
文字统一走 title_cleaner.strip_platform_illegal_chars 清洗。
"""

import base64
import html as _html
import logging
import mimetypes
import os
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

CARD_W = 1242
CARD_H = 1656
MAX_NOTE_IMAGES = 9          # 小红书 publish_note 上限
DEFAULT_PAPER_IMAGES = 2     # 默认附带的论文原图数
BRAND = "具身人机"
BADGE = "AI 论文速读"

_FONT_STACK = '"Noto Sans CJK SC", "WenQuanYi Micro Hei", sans-serif'


# ---------------------------------------------------------------------------
# 文本准备
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    try:
        try:
            from src.utils.title_cleaner import strip_platform_illegal_chars
        except ImportError:
            from utils.title_cleaner import strip_platform_illegal_chars  # type: ignore
        return strip_platform_illegal_chars(text)
    except Exception:  # noqa: BLE001
        return (text or "").strip()


def split_text_to_cards(text: str, max_chars: int = 200,
                        max_cards: int = 5) -> List[str]:
    """把长文本按句子边界贪心切分成若干卡片段落(summaries 降级用)。"""
    text = (text or "").strip()
    if not text:
        return []
    sentences = [s for s in re.split(r"(?<=[。！？!?；;])\s*", text) if s.strip()]
    if not sentences:
        sentences = [text]
    cards: List[str] = []
    buf = ""
    for sent in sentences:
        if buf and len(buf) + len(sent) > max_chars:
            cards.append(buf)
            buf = sent
        else:
            buf += sent
        if len(cards) >= max_cards:
            return cards
    if buf and len(cards) < max_cards:
        cards.append(buf)
    return cards


def prepare_card_texts(narration_segments: Optional[List[str]],
                       summaries: Optional[List[str]],
                       max_cards: int = 6) -> List[str]:
    """确定叙事卡的文字段落: 优先旁白, 缺失时降级切分摘要。"""
    segments = [s.strip() for s in (narration_segments or []) if s and s.strip()]
    if segments:
        return segments[:max_cards]
    summary = "\n".join(s.strip() for s in (summaries or []) if s and s.strip())
    return split_text_to_cards(summary, max_cards=max_cards)


def _font_size_for(text: str) -> int:
    """按字数自适应正文字号, 保证单卡放得下。"""
    n = len(text)
    if n <= 60:
        return 52
    if n <= 120:
        return 44
    if n <= 200:
        return 38
    return 32


# ---------------------------------------------------------------------------
# HTML 模板
# ---------------------------------------------------------------------------

def _img_data_uri(path: str) -> Optional[str]:
    try:
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[xhs_cards] 图片读取失败 %s: %s", path, exc)
        return None


def _base_css() -> str:
    return f"""
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ width: {CARD_W}px; height: {CARD_H}px; overflow: hidden; }}
    body {{ font-family: {_FONT_STACK}; }}
    """


def _cover_card_html(title: str, cover_uri: Optional[str]) -> str:
    bg = (f'<img class="bg" src="{cover_uri}">' if cover_uri else "")
    bg_css = ("" if cover_uri else
              "background: linear-gradient(160deg,#1d2b53 0%,#3e2f5b 55%,#7e2553 100%);")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    {_base_css()}
    body {{ position: relative; {bg_css} }}
    .bg {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
    .shade {{ position:absolute; inset:0;
      background: linear-gradient(180deg, rgba(0,0,0,.18) 0%, rgba(0,0,0,.05) 35%,
                  rgba(0,0,0,.72) 68%, rgba(0,0,0,.92) 100%); }}
    .badge {{ position:absolute; top:64px; left:64px; padding:14px 30px;
      background:#FF4D2E; color:#fff; font-size:30px; font-weight:700;
      border-radius:999px; letter-spacing:2px; }}
    .title {{ position:absolute; left:64px; right:64px; bottom:170px;
      color:#fff; font-size:76px; font-weight:900; line-height:1.32;
      text-shadow: 0 4px 24px rgba(0,0,0,.55); }}
    .brand {{ position:absolute; left:64px; bottom:72px; color:rgba(255,255,255,.85);
      font-size:30px; letter-spacing:3px; }}
    </style></head><body>
    {bg}<div class="shade"></div>
    <div class="badge">{BADGE}</div>
    <div class="title">{_html.escape(title)}</div>
    <div class="brand">@{BRAND}</div>
    </body></html>"""


def _text_card_html(text: str, idx: int, total: int, header: str) -> str:
    fs = _font_size_for(text)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    {_base_css()}
    body {{ background:#FAF7F1; position:relative; padding:150px 110px; }}
    .num {{ position:absolute; top:88px; left:96px; font-size:150px; font-weight:900;
      color:rgba(255,77,46,.16); font-style:italic; }}
    .header {{ position:absolute; top:150px; right:110px; max-width:56%;
      font-size:26px; color:#9a938a; text-align:right; line-height:1.5; }}
    .rule {{ position:absolute; top:270px; left:110px; width:96px; height:10px;
      background:#FF4D2E; border-radius:6px; }}
    .content {{ position:absolute; top:350px; left:110px; right:110px; bottom:190px;
      font-size:{fs}px; line-height:1.78; color:#22201d; font-weight:600;
      display:flex; align-items:center; }}
    .footer {{ position:absolute; bottom:80px; left:110px; right:110px;
      display:flex; justify-content:space-between; align-items:center;
      font-size:26px; color:#b3aca2; }}
    </style></head><body>
    <div class="num">{idx:02d}</div>
    <div class="header">{_html.escape(header)}</div>
    <div class="rule"></div>
    <div class="content"><div>{_html.escape(text)}</div></div>
    <div class="footer"><span>@{BRAND}</span><span>{idx} / {total}</span></div>
    </body></html>"""


def _scene_card_html(frame_uri: str, text: str, idx: int, total: int,
                     header: str) -> str:
    """scene 尾帧(公式/论文图合成画面) + 旁白文字, 上图下文。"""
    fs = min(_font_size_for(text), 40)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    {_base_css()}
    body {{ background:#FAF7F1; position:relative; padding:0; }}
    .num {{ position:absolute; top:52px; left:70px; font-size:96px; font-weight:900;
      color:rgba(255,77,46,.18); font-style:italic; z-index:2; }}
    .header {{ position:absolute; top:84px; right:70px; max-width:60%;
      font-size:26px; color:#9a938a; text-align:right; line-height:1.5; z-index:2; }}
    .framewrap {{ position:absolute; top:190px; left:56px; right:56px;
      display:flex; justify-content:center; }}
    .frame {{ width:100%; border-radius:20px;
      box-shadow:0 14px 52px rgba(0,0,0,.16); }}
    .content {{ position:absolute; top:880px; left:96px; right:96px; bottom:170px;
      font-size:{fs}px; line-height:1.72; color:#22201d; font-weight:600;
      display:flex; align-items:flex-start; overflow:hidden; }}
    .rule {{ position:absolute; top:836px; left:96px; width:96px; height:10px;
      background:#FF4D2E; border-radius:6px; }}
    .footer {{ position:absolute; bottom:74px; left:96px; right:96px;
      display:flex; justify-content:space-between; align-items:center;
      font-size:26px; color:#b3aca2; }}
    </style></head><body>
    <div class="num">{idx:02d}</div>
    <div class="header">{_html.escape(header)}</div>
    <div class="framewrap"><img class="frame" src="{frame_uri}"></div>
    <div class="rule"></div>
    <div class="content"><div>{_html.escape(text)}</div></div>
    <div class="footer"><span>@{BRAND}</span><span>{idx} / {total}</span></div>
    </body></html>"""


def _image_card_html(img_uri: str, idx: int, total: int, caption: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    {_base_css()}
    body {{ background:#FFFFFF; position:relative; padding:120px 80px;
      display:flex; align-items:center; justify-content:center; }}
    .frame {{ max-width:100%; max-height:72%; border-radius:18px;
      box-shadow:0 12px 48px rgba(0,0,0,.12); }}
    .cap {{ position:absolute; bottom:130px; left:80px; right:80px; text-align:center;
      font-size:30px; color:#8a847b; }}
    .footer {{ position:absolute; bottom:70px; left:110px; right:110px;
      display:flex; justify-content:space-between; font-size:26px; color:#b3aca2; }}
    </style></head><body>
    <img class="frame" src="{img_uri}">
    <div class="cap">{_html.escape(caption)}</div>
    <div class="footer"><span>@{BRAND}</span><span>{idx} / {total}</span></div>
    </body></html>"""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _render_htmls_to_pngs(htmls: List[str], out_paths: List[str]) -> List[str]:
    """单浏览器实例批量渲染, 返回成功生成的 png 列表(顺序保持)。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        logger.error("[xhs_cards] playwright 未安装, 无法渲染: %s", exc)
        return []

    try:
        try:
            from src.js_anim_engine import _find_chromium_executable
        except ImportError:
            from js_anim_engine import _find_chromium_executable  # type: ignore
        exe = _find_chromium_executable()
    except Exception:  # noqa: BLE001
        exe = None

    done: List[str] = []
    try:
        with sync_playwright() as p:
            launch_kwargs = {"headless": True}
            if exe:
                launch_kwargs["executable_path"] = exe
            browser = p.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page(
                    viewport={"width": CARD_W, "height": CARD_H},
                    device_scale_factor=1,
                )
                for html_str, out_png in zip(htmls, out_paths):
                    page.set_content(html_str, wait_until="load")
                    page.wait_for_timeout(120)  # data URI 图片解码余量
                    page.screenshot(path=out_png)
                    done.append(out_png)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[xhs_cards] 渲染失败: %s", exc)
    return done


def render_note_cards(
    title: str,
    narration_segments: Optional[List[str]] = None,
    summaries: Optional[List[str]] = None,
    cover_path: Optional[str] = None,
    paper_images: Optional[List[str]] = None,
    scene_frames: Optional[List[Optional[str]]] = None,
    out_dir: str = "./output/xhs_cards",
    max_cards: int = MAX_NOTE_IMAGES,
) -> List[str]:
    """渲染整套小红书图文卡片, 返回图片绝对路径列表(≤9 张)。

    结构: 1 封面卡 + N 叙事卡 + M 论文图卡。
    文字来源优先 narration_segments, 缺失时降级切分 summaries;
    两者都空时返回 [] (调用方自行 fallback)。
    """
    title = _clean(title)
    max_cards = min(max_cards, MAX_NOTE_IMAGES)

    imgs = [p for p in (paper_images or []) if p and os.path.exists(p)]
    imgs = imgs[:DEFAULT_PAPER_IMAGES]
    text_budget = max(1, max_cards - 1 - len(imgs))
    texts = prepare_card_texts(narration_segments, summaries, max_cards=text_budget)
    if not texts:
        logger.warning("[xhs_cards] 无可用文字(旁白/摘要均空), 放弃卡片渲染")
        return []

    total = 1 + len(texts) + len(imgs)
    os.makedirs(out_dir, exist_ok=True)

    htmls: List[str] = []
    outs: List[str] = []

    cover_uri = _img_data_uri(cover_path) if cover_path and os.path.exists(cover_path) else None
    htmls.append(_cover_card_html(title, cover_uri))
    outs.append(os.path.abspath(os.path.join(out_dir, "card_00_cover.png")))

    # scene 帧仅在文字来源为旁白时可对齐使用(summaries 降级时无对应关系)
    use_frames = bool(narration_segments) and bool(scene_frames)
    for i, text in enumerate(texts, start=1):
        frame = scene_frames[i - 1] if (use_frames and i - 1 < len(scene_frames)) else None
        frame_uri = (_img_data_uri(frame)
                     if frame and os.path.exists(frame) else None)
        if frame_uri:
            htmls.append(_scene_card_html(frame_uri, _clean(text), i, total - 1,
                                          header=title))
        else:
            htmls.append(_text_card_html(_clean(text), i, total - 1, header=title))
        outs.append(os.path.abspath(os.path.join(out_dir, f"card_{i:02d}_text.png")))

    for j, img in enumerate(imgs):
        uri = _img_data_uri(img)
        if not uri:
            continue
        idx = len(texts) + 1 + j
        htmls.append(_image_card_html(uri, idx, total - 1, caption="论文原图"))
        outs.append(os.path.abspath(os.path.join(out_dir, f"card_{idx:02d}_fig.png")))

    rendered = _render_htmls_to_pngs(htmls, outs)
    if not rendered:
        logger.error("[xhs_cards] 卡片渲染全部失败")
        return []
    logger.info("[xhs_cards] 卡片渲染完成: %d 张 -> %s", len(rendered), out_dir)
    return rendered


if __name__ == "__main__":  # 手动调试入口
    import argparse
    import json

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="从 meta json 渲染小红书图文卡片")
    parser.add_argument("--meta", required=True, help="*_meta.json 路径")
    parser.add_argument("--cover", default=None, help="封面图路径")
    parser.add_argument("--pics", default="./pic", help="论文图目录")
    parser.add_argument("--out", default="./output/xhs_cards", help="输出目录")
    args = parser.parse_args()

    import glob as _glob
    meta = json.load(open(args.meta, encoding="utf-8"))
    pics = sorted(_glob.glob(os.path.join(args.pics, "*.png")))[:DEFAULT_PAPER_IMAGES]
    cards = render_note_cards(
        title=(meta.get("cn_titles") or ["论文解读"])[0],
        narration_segments=meta.get("narration_segments"),
        summaries=meta.get("summaries"),
        scene_frames=meta.get("scene_frames"),
        cover_path=args.cover,
        paper_images=pics,
        out_dir=args.out,
    )
    print(json.dumps(cards, ensure_ascii=False, indent=2))
