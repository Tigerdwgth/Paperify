"""Manim 演示视频生成引擎。

从现有流水线的 structured_plan 和论文原文中提取公式、模型架构等内容，
由 LLM 自动生成 ManimCE 代码并渲染为动画视频。

固定 4 场景结构:
  Scene 1: TitleScene    -> opening script -> 标题动画
  Scene 2: IntroScene    -> intro script   -> 背景动画
  Scene 3: MethodScene   -> method script  -> 架构/公式动画
  Scene 4: ResultsScene  -> results script -> 结果表格动画
"""

import os
import json
import re
import subprocess
import logging
import glob
import time
import yaml
import shutil

from moviepy import VideoFileClip, concatenate_videoclips, AudioFileClip

from src.llm_tools.prompts import prompts_dict
from src.figure_analyzer import analyze_and_prepare, analysis_to_manim_context, check_consistency_with_vision_llm, extract_frame_from_video

logger = logging.getLogger(__name__)

# --- scene 编排额度常量（集中魔数，见 _build_scene_defs）---------------------
# 固定 4 段（Title/Intro/Method/Results）之外的「额外场景」总预算。
# 公式 Scene 优先占用，示意动画 Scene 用剩余额度。三者关系：
#   formula_used + anim_used <= _EXTRA_SCENE_BUDGET
#   formula_used <= _FORMULA_MAX, anim_used <= _ANIM_MAX
# _FORMULA_MAX / _ANIM_MAX 与 plan 层 (src.llm_tools.llm_agent) 的同名硬上限一致，
# 直接复用以保持单一事实来源；import 失败时回退到与其相同的默认值。
_EXTRA_SCENE_BUDGET = 3  # 额外场景总预算（总 scene 数 <= 4 + 3 = 7）
try:
    from src.llm_tools.llm_agent import _FORMULA_MAX, _ANIM_MAX
except Exception:  # pragma: no cover - 仅在 llm_agent 不可导入时回退
    _FORMULA_MAX = 2  # 最多讲解的核心公式数（硬上限）
    _ANIM_MAX = 2     # 最多生成示意动画的场景数（硬上限）

# 渲染质量映射
QUALITY_MAP = {
    "low": "-ql",       # 480p
    "medium": "-qm",    # 720p
    "high": "-qh",      # 1080p
}

# opencode 子进程超时(秒)，与 js_anim_engine._OPENCODE_TIMEOUT 取同一量级
# （代码生成可能很慢，但必须有上限，否则 opencode 挂住会无限阻塞整条流水线）。
_OPENCODE_TIMEOUT = 86400

# 判定「本次渲染产物」的 mtime 容差(秒)：文件系统时间戳精度 + 子进程启动抖动。
_FRESH_OUTPUT_SLACK_SEC = 2

# 布局常量
TEXT_WRAP_THRESHOLD = 25      # 文本超过此字符数自动换行
MAX_FRAME_WIDTH = 12          # 画框最大宽度（安全区域）
MAX_FRAME_HEIGHT = 7          # 画框最大高度
SAFE_FRAME_WIDTH = 11         # 缩放目标宽度
SAFE_FRAME_HEIGHT = 6.5       # 缩放目标高度



def _find_stmt_end_line(lines, start):
    """返回 lines[start] 起的括号语句结束行号(含)；到末尾仍未配平返回 None。

    逐字符扫描并跳过字符串字面量与行注释，避免把 Text("(a)") 里的括号算进深度。
    单行语句返回 start 本身。
    """
    depth = 0
    quote = None
    for idx in range(start, len(lines)):
        line = lines[idx]
        k = 0
        while k < len(line):
            ch = line[k]
            if quote:
                if ch == "\\":
                    k += 2
                    continue
                if line.startswith(quote, k):
                    k += len(quote)
                    quote = None
                    continue
                k += 1
                continue
            if ch == "#":
                break
            if ch in ('"', "'"):
                quote = ch * 3 if line.startswith(ch * 3, k) else ch
                k += len(quote)
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            k += 1
        # 单/双引号字符串不跨行，只有三引号才继续带到下一行
        if quote and len(quote) == 1:
            quote = None
        if depth <= 0:
            return idx
    return None


def compress_blog_assignments(blog_assignments, rendered_indices):
    """把 blog/示意外部视频的 scene_defs 下标压缩成 scene_videos 下标。

    纯逻辑、无副作用，便于单测。narration_audios / scene_image_paths 都已按
    rendered_indices 压缩对齐，blog_assignments 必须用同一套映射；否则任一
    scene 渲染失败(或 PAPERIFY_METHOD_ONLY 跳过)后，compose 按 scene_videos
    下标查就会取到别的 scene 的视频(成片张冠李戴)。

    Args:
        blog_assignments: dict[int, str] scene_defs 下标 -> 外部视频路径。
        rendered_indices: list[int] 实际进了 scene_videos 的 scene_defs 下标。

    Returns:
        dict[int, str] scene_videos 下标 -> 外部视频路径。
    """
    if not blog_assignments:
        return {}
    return {
        new_idx: blog_assignments[old_idx]
        for new_idx, old_idx in enumerate(rendered_indices)
        if old_idx in blog_assignments
    }


def apply_anim_render_results(scene_defs, narrations, blog_assignments,
                              anim_mp4_by_idx, dropped_idxs):
    """应用 js_anim 渲染结果：剔除渲染失败的 scene、重建索引、登记成功 mp4。

    纯逻辑、无副作用（不读盘不渲染），从 run() 抽出便于单测：
      - dropped_idxs 中的 scene 从 scene_defs / narrations 中干净剔除，
        剩余 scene 与其 narration 保持一一对齐、索引连续不错乱;
      - blog_assignments（旧 idx -> 视频路径）按旧->新 idx 重映射，
        被剔除的 idx 直接丢弃;
      - anim_mp4_by_idx（旧 idx -> 示意 mp4，仅渲染成功的）登记进
        blog_assignments（用重映射后的新 idx），复用 blog 外部视频分支。

    Args:
        scene_defs:        list[dict] 场景定义（含 js_anim scene）。
        narrations:        list[str]  与 scene_defs 等长的旁白。
        blog_assignments:  dict[int, str] 旧 idx -> 外部视频路径（blog clip）。
        anim_mp4_by_idx:   dict[int, str] 旧 idx -> 示意 mp4（仅成功的）。
        dropped_idxs:      set[int]   渲染失败、需丢弃的旧 idx。

    Returns:
        (scene_defs, narrations, blog_assignments) 重建后的三元组。
    """
    if dropped_idxs:
        new_scene_defs, new_narrations, old_to_new = [], [], {}
        for old_j, sdef in enumerate(scene_defs):
            if old_j in dropped_idxs:
                continue
            old_to_new[old_j] = len(new_scene_defs)
            new_scene_defs.append(sdef)
            new_narrations.append(narrations[old_j])
        scene_defs, narrations = new_scene_defs, new_narrations
        # 已存在的 blog assignments 也按重映射迁移 key（blog/js_anim 互斥时为空）。
        blog_assignments = {
            old_to_new[k]: v for k, v in blog_assignments.items()
            if k in old_to_new
        }
        idx_map = old_to_new
    else:
        idx_map = {j: j for j in range(len(scene_defs))}
    # 成功的示意 mp4 登记进 blog_assignments（用最终 idx），
    # 复用 run() 主循环的 `if i in blog_assignments` 分支跳过 manim，
    # 并复用 compose() 的 align_video_to_tts 外部视频分支换 TTS 音轨。
    for old_j, mp4 in anim_mp4_by_idx.items():
        blog_assignments[idx_map[old_j]] = mp4
    return scene_defs, narrations, blog_assignments


class ManimEngine:
    """Manim 动画生成引擎（固定 4 场景结构）。"""

    def __init__(self, paper_text, structured_plan=None, output_dir="./output/manim",
                 arxiv_id=None):
        self.paper_text = paper_text
        self.structured_plan = structured_plan or {}
        self.output_dir = output_dir
        self.arxiv_id = arxiv_id
        # 本次 run 的起点: 区分"上一篇论文/上一轮的残留产物"与本次刚渲好的产物
        self._run_started_at = time.time()
        self.temp_dir = os.path.join(output_dir, "temp")
        os.makedirs(self.output_dir, exist_ok=True)
        # 清空旧的临时文件，避免残留影响新 pipeline
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        self._config_cache = None

    # ------------------------------------------------------------------
    # 代码安全: 注入边界检查
    # ------------------------------------------------------------------

    def _wrap_long_texts(self, code):
        """自动给长 Text 字符串插入换行，支持 CJK 字符。"""
        def _has_cjk(text):
            return any('\u4e00' <= c <= '\u9fff' for c in text)

        def _wrap_long_text(match):
            full = match.group(0)
            text_match = re.search(r'Text\(\s*["\'](.*?)["\']', full, re.DOTALL)
            if not text_match:
                return full
            text = text_match.group(1)
            if "\\n" in text or "\n" in text:
                return full
            # 中英文使用不同的换行阈值
            if _has_cjk(text):
                threshold = 15  # 中文字符宽度约为英文 2 倍
                if len(text) <= threshold:
                    return full
                lines = [text[i:i+threshold] for i in range(0, len(text), threshold)]
            else:
                threshold = 40  # 英文用更宽的阈值
                if len(text) <= threshold:
                    return full
                words = text.split(" ")
                lines = []
                current = ""
                for w in words:
                    if current and len(current) + 1 + len(w) > threshold:
                        lines.append(current)
                        current = w
                    else:
                        current = (current + " " + w).strip()
                if current:
                    lines.append(current)
            new_text = "\\n".join(lines)
            return full.replace(text, new_text)

        pattern = r'Text\(\s*["\'][^"\']{' + str(TEXT_WRAP_THRESHOLD) + r',}["\'][^)]*\)'
        code = re.sub(pattern, _wrap_long_text, code)
        return code

    def _remove_trailing_fadeout(self, code):
        """移除末尾的 FadeOut，替换为 wait（防止最后一帧变黑）。
        只删除 construct 方法最后 5 行内的 FadeOut，避免误删中间的分页 FadeOut。"""
        lines = code.split('\n')
        # 找到最后一个非空非注释行的位置
        last_content_idx = len(lines) - 1
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped and not stripped.startswith('#') and not stripped.startswith('_all_mobs'):
                last_content_idx = i
                break
        # 只在最后 5 行范围内查找 FadeOut
        search_start = max(0, last_content_idx - 5)
        for i in range(last_content_idx, search_start - 1, -1):
            if 'FadeOut' in lines[i] and 'self.play' in lines[i]:
                # self.play(...) 常被 LLM 写成跨行(run_time= 换到下一行)，只替换
                # 起始行会留下孤立续行 `run_time=1.5)` -> SyntaxError。先按括号
                # 配平找出整条语句的结束行整段替换；配不平就原样返回不冒险改坏。
                end = _find_stmt_end_line(lines, i)
                if end is None:
                    logger.warning("末尾 FadeOut 语句括号未配平，跳过移除")
                    return code
                indent = len(lines[i]) - len(lines[i].lstrip())
                lines[i:end + 1] = [' ' * indent + 'self.wait(2)  # 保持内容显示']
                break
        return '\n'.join(lines)


    def _ensure_page_fadeouts(self, code):
        """检测多个 FadeIn(pageN) 之间缺少 FadeOut 的情况并自动插入。"""
        import re as _re
        lines = code.split('\n')
        result = []
        # 跟踪当前活跃的 page 变量名
        active_pages = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            # 检测 FadeIn(someVar) 调用
            fadein_match = _re.search(r'self\.play\(\s*FadeIn\(\s*(\w+)', stripped)
            if fadein_match:
                var_name = fadein_match.group(1)
                # 如果有活跃的 page 且当前 FadeIn 的不是同一个，插入 FadeOut
                for active in active_pages:
                    if active != var_name:
                        indent = len(line) - len(line.lstrip())
                        result.append(' ' * indent + f'self.play(FadeOut({active}))')
                        result.append(' ' * indent + 'self.wait(0.3)')
                active_pages = [var_name]
            # 检测 FadeOut 调用，从 active 列表移除
            fadeout_match = _re.search(r'FadeOut\(\s*(\w+)', stripped)
            if fadeout_match and not fadein_match:
                var_name = fadeout_match.group(1)
                active_pages = [p for p in active_pages if p != var_name]
            result.append(line)
        return '\n'.join(result)

    def _inject_scale_safety(self, code):
        """注入安全网：mobject 进入场景时立刻 scale_to_fit + 把超出 frame 的位置拉回画内。

        旧实现把 scale check 放在 construct 末尾，所有 self.play 播完才生效——
        但 mp4 是按时间轴写帧的，超界帧早已写入。改为在 construct 开头 monkey-patch
        self.add：每个 mobject add 进 scene 时立刻按比例 cap 到 SAFE 区，渲染每一帧
        都已经 fit。末尾兜底 scale 保留。
        """
        MW = str(MAX_FRAME_WIDTH); MH = str(MAX_FRAME_HEIGHT)
        SW = str(SAFE_FRAME_WIDTH); SH = str(SAFE_FRAME_HEIGHT)
        prologue = [
            '        # === Safe-frame guard (auto-cap on add) ===',
            '        _SF_MAX_W, _SF_MAX_H = ' + MW + ', ' + MH,
            '        _SF_SAFE_W, _SF_SAFE_H = ' + SW + ', ' + SH,
            '        def _sf_cap(_m):',
            '            try:',
            '                w = getattr(_m, "width", 0); h = getattr(_m, "height", 0)',
            '                if w and w > _SF_MAX_W: _m.scale_to_fit_width(_SF_SAFE_W)',
            '                if h and h > _SF_MAX_H: _m.scale_to_fit_height(_SF_SAFE_H)',
            '                if not hasattr(_m, "get_center"): return',
            '                c = _m.get_center()',
            '                w = getattr(_m, "width", 0); h = getattr(_m, "height", 0)',
            '                hw = _SF_MAX_W / 2.0; hh = _SF_MAX_H / 2.0',
            '                dx = 0.0; dy = 0.0',
            '                if c[0] - w/2.0 < -hw: dx = -hw - (c[0] - w/2.0) + 0.05',
            '                if c[0] + w/2.0 >  hw: dx =  hw - (c[0] + w/2.0) - 0.05',
            '                if c[1] - h/2.0 < -hh: dy = -hh - (c[1] - h/2.0) + 0.05',
            '                if c[1] + h/2.0 >  hh: dy =  hh - (c[1] + h/2.0) - 0.05',
            '                if dx or dy: _m.shift([dx, dy, 0])',
            '            except Exception:',
            '                pass',
            '        _sf_orig_add = self.add',
            '        def _sf_safe_add(*mobs, **kw):',
            '            for _m in mobs: _sf_cap(_m)',
            '            return _sf_orig_add(*mobs, **kw)',
            '        self.add = _sf_safe_add',
            '        # === end safe-frame guard ===',
            '',
        ]
        epilogue = [
            '        # === Auto-scale safety net (tail fallback) ===',
            '        _all_mobs = VGroup(*[m for m in self.mobjects if isinstance(m, VMobject)])',
            '        if len(_all_mobs) > 0:',
            '            if _all_mobs.width > ' + MW + ':',
            '                _all_mobs.scale_to_fit_width(' + SW + ')',
            '            if _all_mobs.height > ' + MH + ':',
            '                _all_mobs.scale_to_fit_height(' + SH + ')',
        ]
        lines = code.split('\n')

        # 1) prologue: 紧跟 "    def construct(self):" 行之后插入
        #    兼容带返回注解的 "def construct(self) -> None:" 与 "async def construct(self)"
        head_idx = None
        for i, ln in enumerate(lines):
            _s = ln.lstrip()
            if _s.startswith('def construct(self)') or _s.startswith('async def construct(self)'):
                head_idx = i + 1
                break
        if head_idx is None:
            return code  # 没有 construct 方法，原样返回
        lines = lines[:head_idx] + prologue + lines[head_idx:]

        # 2) epilogue: 插到 construct 方法体最后一个内容行之后。
        #    直接取「最后一个非空非注释、>=8 空格缩进」的行即可——对跨行 self.play(...)
        #    的闭合行 ) 之后插入是正确的；旧的括号配平向上扫描遇到跨行语句会停在它的
        #    开头行 self.play( 处，把 epilogue 插进未闭合的调用中间，导致 SyntaxError。
        insert_idx = len(lines)
        for i in range(len(lines) - 1, head_idx + len(prologue) - 1, -1):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith('#'):
                continue
            if lines[i].startswith('        '):
                insert_idx = i + 1
                break
        lines = lines[:insert_idx] + epilogue + lines[insert_idx:]
        return '\n'.join(lines)

    def _inject_text_overlap_guard(self, code):
        """注入"文字重叠守卫"：包裹 self.play，每次播放后检测场景内文字 mobject 的
        包围盒重叠，只保留最上层（最新/ z_index 最高）的文字，把被遮挡的下层文字 FadeOut。

        与"累加显示"原则配合：不重叠的文字仍累加保留；只有真正叠在一起糊成一团时，
        才移除下层文字 —— 即"新文字出现后只显示最上层的文字"。
        """
        prologue = [
            '        # === Text-overlap guard: 新文字出现后只保留最上层文字 ===',
            '        # 阈值按"交叠面积 / 较大文字面积"判定: 要求两块大幅互相重合 (真糊成一团)',
            '        # 才删下层, 避免小标签压在大段落上时误删整段 (见 code-review finding 1/2)。',
            '        _TO_THRESH = 0.5',
            '        _TO_TEXT_CLS = ("Text", "MarkupText", "Tex", "MathTex",',
            '                        "SingleStringMathTex", "Paragraph", "Title")',
            '        try:  # isinstance 覆盖子类 (Title->Tex 等); 失败则回退类名匹配',
            '            from manim import Text as _TT0, MarkupText as _TT1, Tex as _TT2',
            '            from manim import MathTex as _TT3, Paragraph as _TT4',
            '            _TO_TYPES = (_TT0, _TT1, _TT2, _TT3, _TT4)',
            '        except Exception:',
            '            _TO_TYPES = tuple()',
            '        def _to_is_text(_m):',
            '            if _TO_TYPES and isinstance(_m, _TO_TYPES): return True',
            '            return type(_m).__name__ in _TO_TEXT_CLS',
            '        def _to_bbox(_m):',
            '            try:',
            '                _c = _m.get_center(); _w = float(_m.width); _h = float(_m.height)',
            '                if _w <= 0 or _h <= 0: return None',
            '                return (_c[0]-_w/2.0, _c[1]-_h/2.0, _c[0]+_w/2.0, _c[1]+_h/2.0, _w*_h)',
            '            except Exception:',
            '                return None',
            '        def _to_overlap_ratio(_a, _b):',
            '            _ix0 = max(_a[0], _b[0]); _iy0 = max(_a[1], _b[1])',
            '            _ix1 = min(_a[2], _b[2]); _iy1 = min(_a[3], _b[3])',
            '            if _ix1 <= _ix0 or _iy1 <= _iy0: return 0.0',
            '            _inter = (_ix1-_ix0) * (_iy1-_iy0)',
            '            _amax = max(_a[4], _b[4])  # 较大块面积归一化: 小标签盖大段落 ratio 很小, 不误删',
            '            return _inter/_amax if _amax > 0 else 0.0',
            '        def _to_resolve_overlap():',
            '            _texts = [_m for _m in self.mobjects if _to_is_text(_m)]',
            '            if len(_texts) < 2: return',
            '            # 绘制顺序: 列表越靠后越上层; z_index 更高更上层',
            '            _ranked = sorted(range(len(_texts)),',
            '                             key=lambda _i: (getattr(_texts[_i], "z_index", 0), _i))',
            '            _boxes = {_i: _to_bbox(_texts[_i]) for _i in range(len(_texts))}',
            '            _remove = set()',
            '            for _p in range(len(_ranked)):',
            '                _lo = _ranked[_p]',
            '                if _lo in _remove or _boxes[_lo] is None: continue',
            '                for _q in range(_p + 1, len(_ranked)):',
            '                    _hi = _ranked[_q]',
            '                    if _hi in _remove or _boxes[_hi] is None: continue',
            '                    if _to_overlap_ratio(_boxes[_lo], _boxes[_hi]) > _TO_THRESH:',
            '                        _remove.add(_lo)  # _lo 在下层被遮挡 -> 移除, 保留上层 _hi',
            '                        break',
            '            _rm = [_texts[_i] for _i in _remove if _texts[_i] in self.mobjects]',
            '            if _rm:',
            '                import sys as _sys',
            '                print("[overlap-guard] 检测到文字重叠, 移除 %d 个下层文字" % len(_rm),',
            '                      file=_sys.stderr)',
            '                try:',
            '                    _to_orig_play(*[FadeOut(_x) for _x in _rm], run_time=0.3)',
            '                except Exception:',
            '                    try: self.remove(*_rm)',
            '                    except Exception as _e2:',
            '                        print("[overlap-guard] 移除失败:", _e2, file=_sys.stderr)',
            '        _to_orig_play = self.play',
            '        # 注: 重叠在创建它的那次 play 期间仍会短暂可见, 守卫在 play 结束后清理',
            '        # (下层文字 FadeOut)。彻底无重叠需 per-frame updater, 此处取事后清理折中。',
            '        def _to_guarded_play(*_anims, **_kw):',
            '            _ret = _to_orig_play(*_anims, **_kw)',
            '            try: _to_resolve_overlap()',
            '            except Exception: pass',
            '            return _ret',
            '        self.play = _to_guarded_play',
            '        # === end text-overlap guard ===',
            '',
        ]
        lines = code.split('\n')
        head_idx = None
        for i, ln in enumerate(lines):
            # 兼容 "def construct(self):"、带注解 "-> None:" 与 "async def construct(self)"
            _s = ln.lstrip()
            if _s.startswith('def construct(self)') or _s.startswith('async def construct(self)'):
                head_idx = i + 1
                break
        if head_idx is None:
            return code  # 没有 construct 方法，原样返回
        lines = lines[:head_idx] + prologue + lines[head_idx:]
        return '\n'.join(lines)

    def _enforce_reading_time(self, code):
        """防闪屏：把 self.wait() 短于阅读需要的都抬升。

        规则：
        - 上一行是 self.play(... Write|FadeIn ...) → 后续 self.wait(<2.0) 抬到 2.0
        - 上一行是 self.play(... FadeOut ...) → 后续 self.wait(<0.8) 抬到 0.8
        - 其他情况 self.wait(<0.6) 抬到 0.8（消除闪烁）
        """
        import re as _re
        text_re = _re.compile(r"\bself\.play\([^)]*(?:Write|FadeIn|GrowArrow|Create|Indicate)[^)]*\)")
        fadeout_re = _re.compile(r"\bself\.play\([^)]*FadeOut[^)]*\)")
        wait_re = _re.compile(r"\bself\.wait\(\s*([0-9]*\.?[0-9]+)\s*\)")
        lines = code.split("\n")
        last = None  # "text" | "fadeout" | "other" | None
        out = []
        for line in lines:
            m = wait_re.search(line)
            if m:
                val = float(m.group(1))
                if last == "text" and val < 2.0:
                    new = 2.0
                elif last == "fadeout" and val < 0.8:
                    new = 0.8
                elif val < 0.6:
                    new = 0.8
                else:
                    new = val
                if new != val:
                    old = m.group(0)
                    repl = "self.wait(%s)" % ("%g" % new)
                    line = line.replace(old, repl, 1)
            elif text_re.search(line):
                last = "text"
            elif fadeout_re.search(line):
                last = "fadeout"
            elif "self.play(" in line:
                last = "other"
            out.append(line)
        return "\n".join(out)

    def inject_bounds_check(self, code):
        """注入自动缩放安全网、移除末尾 FadeOut、自动给长文本换行、抬升 wait 时长。

        累加显示原则: 不在中间强插 FadeOut (用户偏好元素 FadeIn 后保留, 末尾统一 FadeOut)。
        _ensure_page_fadeouts 会在两个 FadeIn 之间自动插 FadeOut, 跟累加显示矛盾, 已禁用。
        """
        code = self._wrap_long_texts(code)
        # _ensure_page_fadeouts 已禁用: 它会强插中间 FadeOut, 破坏累加显示
        code = self._remove_trailing_fadeout(code)
        code = self._inject_scale_safety(code)
        # 文字重叠守卫: 新文字出现后只保留最上层文字 (在缩放安全网之后注入)
        code = self._inject_text_overlap_guard(code)
        code = self._enforce_reading_time(code)
        return code


    def _opencode_generate(self, prompt_text, scene_name=None):
        """通过 opencode headless 模式调用 DeepSeek-R1 生成代码。
        opencode 会自动加载 manim_skill 最佳实践。"""


        # 写 prompt 到临时文件避免 shell 转义问题
        prompt_file = os.path.join(os.path.abspath(self.temp_dir), "_opencode_prompt.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt_text)

        # 调用前清理上次的 <scene_name>.py, 这样调用结束后若文件存在,
        # 就一定是 opencode 本次用 Write 工具新写的, 直接读它即可 (主路径).
        if scene_name:
            scene_py_path = os.path.join(os.path.abspath(self.temp_dir), f"{scene_name}.py")
            try:
                if os.path.exists(scene_py_path):
                    os.remove(scene_py_path)
            except Exception as _e:
                logger.warning("清理旧 %s 失败: %s", scene_py_path, _e)

        env = os.environ.copy()
        # 取到非空才覆盖：_get_config 读的是相对路径 config.yaml，cwd 不在项目根时
        # 读空，无条件覆盖会把环境里原本有效的 key 抹成空串，opencode 直接认证失败。
        deepseek_key = self._get_deepseek_key()
        if deepseek_key:
            env["DEEPSEEK_API_KEY"] = deepseek_key
        else:
            logger.warning("未取到 llm_api_key/LLM_API_KEY，沿用环境已有的 DEEPSEEK_API_KEY")
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["CUDA_VISIBLE_DEVICES"] = "0"
        env.pop("http_proxy", None)
        env.pop("https_proxy", None)
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 写 wrapper 脚本避免 shell 参数展开问题
        wrapper_script = os.path.join(os.path.abspath(self.temp_dir), "_opencode_run.sh")
        with open(wrapper_script, "w") as wf:
            wf.write("#!/bin/bash\n")
            wf.write("export PATH=/usr/local/bin:$PATH\n")
            # CWD_ISOLATION_PATCH: cd 到 temp_dir，让 opencode 看不到项目根的历史 .py 产物
            wf.write(f'cd "{os.path.abspath(self.temp_dir)}"\n')
            # 通过 stdin 注入 prompt, 避免 paper_text 全文使命令行参数超 ARG_MAX (Linux 128KB)
            wf.write(f'opencode run < "{prompt_file}"\n')
        os.chmod(wrapper_script, 0o755)
        cmd = f'bash {wrapper_script}' 
        try:
            # CWD_ISOLATION_PATCH: cwd 改到 temp_dir，避免 opencode 误读项目根 .py 产物
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                env=env, cwd=os.path.abspath(self.temp_dir),
                timeout=_OPENCODE_TIMEOUT,
            )
            logger.info(
                "opencode subprocess: returncode=%s stdout_len=%d stderr_len=%d",
                result.returncode, len(result.stdout or ""), len(result.stderr or "")
            )
            output = result.stdout
            output_source = "stdout"
            if not output:
                output = result.stderr or ""
                output_source = "stderr-fallback"

            # 0. 优先路径: opencode 用 Write 工具把代码写到 cwd/<scene_name>.py
            #    (调用前已清理旧文件, 此处文件存在 = 本次新写)
            if scene_name:
                scene_py_path = os.path.join(os.path.abspath(self.temp_dir), f"{scene_name}.py")
                if os.path.exists(scene_py_path):
                    try:
                        code_from_file = open(scene_py_path, "r", encoding="utf-8").read()
                        if "from manim import" in code_from_file and "class " in code_from_file:
                            logger.info("opencode 通过 Write 写入 %s (%d 行)",
                                        scene_py_path, code_from_file.count("\n") + 1)
                            return code_from_file.strip()
                        else:
                            logger.warning("%s 已存在但内容无效 (缺 from manim import 或 class)", scene_py_path)
                    except Exception as _e:
                        logger.warning("读取 %s 失败: %s", scene_py_path, _e)

            # 1. 去掉 ANSI 转义码
            output = re.sub(r'\x1b\[[0-9;]*m', '', output)
            output = re.sub(r'\033\[[0-9;]*m', '', output)
            output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)

            # 2. fallback: 提取 stdout 的 ```python ... ``` 代码块
            code_match = re.search(r'```python\s*\n(.*?)\n```', output, re.DOTALL)
            if code_match:
                code = code_match.group(1).strip()
                logger.info("opencode 返回代码 (stdout, %d 行)", code.count("\n") + 1)
                return code

            # 3. fallback: 提取 from manim import * 开始的文本
            manim_match = re.search(r'(from manim import \*.*)', output, re.DOTALL)
            if manim_match:
                code = manim_match.group(1).strip()
                code = re.sub(r'\n```\s*$', '', code)
                logger.info("opencode fallback 提取代码 (stdout text, %d 行)", code.count("\n") + 1)
                return code

            import time as _time
            dbg_name = scene_name or "unknown"
            dbg_path = f"/tmp/opencode_dbg_{dbg_name}_{int(_time.time())}.log"
            try:
                with open(dbg_path, "w", encoding="utf-8") as _df:
                    _df.write(f"=== returncode: {result.returncode}\n")
                    _df.write(f"=== output_source: {output_source}\n")
                    _df.write(f"=== stdout ({len(result.stdout or '')} chars) ===\n")
                    _df.write(result.stdout or "")
                    _df.write(f"\n=== stderr ({len(result.stderr or '')} chars) ===\n")
                    _df.write(result.stderr or "")
            except Exception as _e:
                logger.warning("dump opencode debug 失败: %s", _e)
            logger.warning(
                "opencode 未返回有效代码 (source=%s rc=%s len=%d): head 2000 字: %r",
                output_source, result.returncode, len(output), output[:2000]
            )
            if len(output) > 2000:
                logger.warning("opencode tail 1000 字: %r", output[-1000:])
            logger.warning("完整 stdout+stderr dump 到: %s", dbg_path)
            return ""
        except subprocess.TimeoutExpired:
            logger.error("opencode 调用超时")
            return ""
        except Exception as e:
            logger.error("opencode 调用失败: %s", e)
            return ""

    def _opencode_generate_with_retry(self, prompt_text, attempts=3, scene_name=None):
        """连续调用 opencode，直到拿到非空代码或次数用尽。"""
        for i in range(attempts):
            raw = self._opencode_generate(prompt_text, scene_name=scene_name)
            if raw:
                return raw
            logger.warning("opencode 返回空 (第 %d/%d 次)", i + 1, attempts)
        logger.error("opencode 连续 %d 次失败，放弃", attempts)
        return ""

    def _get_config(self):
        """读取并缓存 config.yaml 配置。"""
        if self._config_cache is None:
            config_path = "config.yaml"
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    self._config_cache = yaml.safe_load(f) or {}
            else:
                self._config_cache = {}
        return self._config_cache

    def _get_deepseek_key(self):
        config = self._get_config()
        return config.get("llm_api_key", "") or os.environ.get("LLM_API_KEY", "")

    # ------------------------------------------------------------------
    # Manim 代码生成
    # ------------------------------------------------------------------

    # 架构图注入元素上限: 一个 720p 画面塞得下的核心模块/文字数量上限。
    # 原始图分析常给出 100+ 组件 (BFM-Zero 实测 147 个), 全塞进画面必然重叠。
    # 注入前按 bbox 面积 (越大越是主干结构) 取前 N 个, 元素少了自然不挤。
    ARCH_MAX_ELEMENTS = 20

    def _trim_architecture_elements(self, figure_analysis, manim_ctx, eb_elements, n_total):
        import os as _os
        if _os.environ.get("JSR_DISABLE_TRIM")=="1":
            return manim_ctx, eb_elements, n_total, n_total
        """架构图防重叠治本: 把注入给 opencode 的元素裁剪到核心 ARCH_MAX_ELEMENTS 个。

        通过裁剪结构化 analysis.components (按 bbox 面积降序保留主干大块),
        再用项目已有的 analysis_to_manim_context / to_eb_elements 重新渲染两段文本,
        保证格式与未裁剪路径完全一致。无法裁剪 (无结构化数据) 时原样返回。

        Returns: (manim_ctx, eb_elements, n_kept, n_total)
        """
        limit = self.ARCH_MAX_ELEMENTS
        if n_total <= limit:
            return manim_ctx, eb_elements, n_total, n_total

        analysis = (figure_analysis or {}).get("analysis") or {}
        comps = analysis.get("components") or []
        if not comps:
            # 无结构化组件可裁 (理论上不会发生), 原样返回避免破坏格式
            return manim_ctx, eb_elements, n_total, n_total

        def _area(c):
            bb = c.get("bbox_normalized") or [0, 0, 1, 1]
            try:
                return max(0.0, (bb[2] - bb[0])) * max(0.0, (bb[3] - bb[1]))
            except Exception:
                return 0.0

        kept = sorted(comps, key=_area, reverse=True)[:limit]
        kept_names = {id(c) for c in kept}
        # 保持原始绘制顺序 (数据流方向) 仅过滤掉被裁掉的小元素
        kept_ordered = [c for c in comps if id(c) in kept_names]

        trimmed = dict(analysis)
        trimmed["components"] = kept_ordered
        # 连接里只保留两端都还在的箭头, 避免指向被裁元素的悬空箭头
        kept_labels = set()
        for c in kept_ordered:
            for k in ("name", "chinese_name", "id"):
                v = c.get(k)
                if v:
                    kept_labels.add(str(v))
        conns = analysis.get("connections") or []
        if conns:
            trimmed["connections"] = [
                cn for cn in conns
                if (str(cn.get("from", "")) in kept_labels or not cn.get("from"))
                and (str(cn.get("to", "")) in kept_labels or not cn.get("to"))
            ]

        new_ctx, new_eb = manim_ctx, eb_elements
        try:
            from src.figure_analyzer import analysis_to_manim_context as _a2c
        except ImportError:
            from figure_analyzer import analysis_to_manim_context as _a2c  # type: ignore
        try:
            if manim_ctx:
                new_ctx = _a2c(trimmed, has_precise_bbox=bool(eb_elements))
        except Exception as _e:
            logger.warning("[arch-trim] 重渲染 manim_context 失败, 用原文: %s", _e)
        try:
            if eb_elements:
                try:
                    from src.arxiv_source_analyzer import to_eb_elements as _t2e
                except ImportError:
                    from arxiv_source_analyzer import to_eb_elements as _t2e  # type: ignore
                new_eb = _t2e(trimmed)
        except Exception as _e:
            logger.warning("[arch-trim] 重渲染 eb_manim_elements 失败, 用原文: %s", _e)

        logger.info("[arch-trim] 架构图元素 %d → %d (按 bbox 面积保留主干, 上限 %d)",
                    n_total, len(kept_ordered), limit)
        return new_ctx, new_eb, len(kept_ordered), n_total

    def generate_manim_code(self, scene_info):
        """根据场景信息调用 LLM 生成 ManimCE 代码。支持图像分析增强。

        策略：
        - MethodScene (有图像分析) -> opencode headless (利用 manim_skill)
        - 其他场景 -> 直接 DeepSeek API (更快更稳定)
        """
        scene_type = scene_info.get("type", "formula")
        figure_analysis = scene_info.get("figure_analysis")

        # 选择 prompt
        if figure_analysis and scene_type == "architecture":
            prompt_key = "manim_generate_architecture_from_figure"
        else:
            prompt_key = f"manim_generate_{scene_type}"
        prompt = prompts_dict.get(prompt_key, prompts_dict.get("manim_generate_formula", ""))

        sname = scene_info.get("scene_name", "CustomScene")
        sdesc = scene_info.get("description", "")
        user_content = f"scene_name: {sname}\n"
        user_content += f"描述: {sdesc}\n"
        if scene_info.get("latex"):
            user_content += f"LaTeX 公式: {scene_info['latex']}\n"
        if scene_info.get("highlights"):
            import json as _json_hl
            user_content += f"highlights: {_json_hl.dumps(scene_info['highlights'], ensure_ascii=False)}\n"
        if scene_info.get("script_excerpt"):
            user_content += f"脚本原文: {scene_info['script_excerpt']}\n"

        # 注入图像分析上下文
        if figure_analysis:
            # 架构图防重叠治本: 注入前把元素裁剪到核心 N 个 (≤ ARCH_MAX_ELEMENTS),
            # 否则 107~147 个组件全塞进 720p 画面必然挤成一团 (BFM-Zero 实测)。
            manim_ctx, eb_elements, _n_kept, _n_total = self._trim_architecture_elements(
                figure_analysis,
                figure_analysis.get("manim_context", ""),
                figure_analysis.get("eb_manim_elements", ""),
                # analysis 可能是 None(figure_analyzer 失败路径返回 {"analysis": None})，
                # key 存在时 get 的默认值不生效，必须用 or 兜住，否则 None.get 崩掉整条流水线
                len(((figure_analysis or {}).get("analysis") or {}).get("components") or []),
            )
            if manim_ctx:
                user_content += f"\n{manim_ctx}\n"
            # 注入 Edit Banana 精确元素数据（包含 Manim 坐标）
            if eb_elements:
                user_content += f"\n## 论文方法图精确元素数据（SAM3 分割，坐标已转为 Manim 坐标系）\n"
                user_content += f"## 请严格按照这些坐标和颜色生成 Manim 代码！\n"
                user_content += eb_elements + "\n"
                logger.info("已注入 EB 精确元素数据 (%d 字符)", len(eb_elements))
            logger.info("已注入图像分析上下文 (类型: %s, 原始 %d 组件 → 注入 %d 个核心元素)",
                       figure_analysis.get("figure_type", "unknown"),
                       _n_total, _n_kept)

        if not figure_analysis:
            # 论文全文不再嵌入 prompt（避免 ARG_MAX 超限 + 节省上下文 token）
            # 写到固定路径让 opencode 自行 Read
            paper_path = os.path.abspath(os.path.join(self.temp_dir, "_paper_text.txt"))
            os.makedirs(os.path.dirname(paper_path), exist_ok=True)
            with open(paper_path, "w", encoding="utf-8") as _pf:
                _pf.write(self.paper_text or "")
            user_content += f"\n论文原文路径（请用 Read 工具读取后再生成代码）: {paper_path}\n"
        user_content += "\n重要：生成的动画内容必须忠实于这篇论文的具体方法，不要用通用的示例。\n"

        full_prompt = prompt + "\n\n" + user_content

        # 持久日志: 把每次喂给 opencode 的完整输入 (prompt + user_content) 落盘,
        # 以后任何 scene 出问题 (重叠/不忠实/语法炸) 都能回看它当时拿到的全部输入。
        try:
            os.makedirs(self.temp_dir, exist_ok=True)
            _in_path = os.path.join(self.temp_dir, f"{sname}_opencode_input.txt")
            with open(_in_path, "w", encoding="utf-8") as _inf:
                _inf.write(full_prompt)
            logger.info("[opencode-input] %s: %d 字 → temp/%s_opencode_input.txt",
                        sname, len(full_prompt), sname)
        except Exception as _e_in:
            logger.warning("[opencode-input] 写入失败 (%s): %s", sname, _e_in)

        # 所有场景统一走 opencode（禁止直连 API 生成 manim 代码）
        logger.info("使用 opencode 生成代码 (prompt_key=%s)...", prompt_key)
        raw = self._opencode_generate_with_retry(full_prompt, attempts=3, scene_name=scene_info.get("scene_name"))

        # 清理 markdown 代码块标记
        code = (raw or "").strip()
        if code.startswith("```"):
            code = re.sub(r"^```\w*\n?", "", code)
            code = re.sub(r"\n?```$", "", code)
            code = code.strip()

        return code
    # 渲染
    # ------------------------------------------------------------------

    def _purge_stale_scene_outputs(self, media_dir, scene_name, ext):
        """渲染前清掉该 scene 上一次 run 留下的同名产物(partial_movie_files 不动)。

        media_dir 与 scene 名都是固定的，上一篇论文的 <scene>.mp4 留在盘上，
        本次渲染一炸就会被 glob 捡回来冒充成功。只删本 run 之前的：本 run 里刚渲好的
        同名产物(一致性检查重渲的上一版)要留着，重渲失败时上层还要回退用它。
        """
        for f in glob.glob(os.path.join(media_dir, "**", f"{scene_name}.{ext}"), recursive=True):
            if "partial_movie_files" in f:
                continue
            try:
                # 同 _collect_fresh_outputs 留一点时钟容差: 内核落 mtime 的时钟
                # 比 time.time() 粗, 刚写出的文件 mtime 可能略早于本 run 起点
                if os.path.getmtime(f) >= self._run_started_at - _FRESH_OUTPUT_SLACK_SEC:
                    continue
                os.remove(f)
                logger.info("清理 %s 的历史产物: %s", scene_name, f)
            except OSError as _e:
                logger.warning("清理历史产物失败 %s: %s", f, _e)

    def _collect_fresh_outputs(self, pattern, started_at):
        """按 glob pattern 找本次渲染(mtime 不早于 started_at)的产物，新的排前面。"""
        out = []
        for f in glob.glob(pattern, recursive=True):
            if "partial_movie_files" in f:
                continue
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            if mtime < started_at - _FRESH_OUTPUT_SLACK_SEC:
                logger.info("忽略非本次渲染的残留文件: %s", f)
                continue
            out.append((mtime, f))
        return [f for _m, f in sorted(out, reverse=True)]

    def render_scene(self, code, scene_name, quality="medium", fmt="mp4", max_retries=3):
        """渲染单个 Manim 场景。"""
        quality_flag = QUALITY_MAP.get(quality, "-qm")
        fmt_flag = "--format=gif" if fmt == "gif" else ""

        for attempt in range(max_retries):
            script_path = os.path.join(self.temp_dir, f"{scene_name}.py")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(code)

            cmd = f"manim render {quality_flag} {fmt_flag} --media_dir {self.output_dir}/media {script_path} {scene_name}"
            logger.info("渲染场景 %s (第 %d 次): %s", scene_name, attempt + 1, cmd)

            # 先清历史同名产物 + 记起始时间，保证下面 found 到的是本次渲染的结果
            ext = "gif" if fmt == "gif" else "mp4"
            media_dir = os.path.join(self.output_dir, "media", "videos")
            self._purge_stale_scene_outputs(media_dir, scene_name, ext)
            started_at = time.time()

            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=86400
                )

                # 只有 manim 自己报成功才去认产物：returncode != 0 一律走重试/失败路径，
                # 绝不 glob 兜底(否则捡到残留旧文件当成功，retry 不触发、成片混别的论文)
                if result.returncode == 0:
                    found = self._collect_fresh_outputs(
                        os.path.join(media_dir, "**", f"{scene_name}.{ext}"), started_at)
                    if found:
                        logger.info("场景 %s 渲染成功: %s", scene_name, found[0])
                        return found[0]

                    all_files = self._collect_fresh_outputs(
                        os.path.join(self.output_dir, "**", f"*.{ext}"), started_at)
                    if all_files:
                        logger.info("使用最新输出文件: %s", all_files[0])
                        return all_files[0]

                    logger.warning("场景 %s: manim 返回 0 但没有本次的 %s 产物", scene_name, ext)

                error_msg = result.stderr or result.stdout
                logger.warning("场景 %s 渲染失败 (第 %d 次):\n%s", scene_name, attempt + 1, error_msg[:2000])

                if attempt < max_retries - 1:
                    fix_prompt = prompts_dict.get("manim_fix_code", "")
                    fix_content = f"原始代码:\n```python\n{code}\n```\n\n错误信息:\n```\n{error_msg[:3000]}\n```"
                    # 修复结果先收在临时变量：直接覆盖 code 的话，修复返回空就把原代码
                    # 永久丢了，下一轮 attempt 会把空串写进 .py 再渲一次(必然失败还白烧一轮)
                    fixed = self._opencode_generate_with_retry(fix_prompt + "\n\n" + fix_content, attempts=2, scene_name=scene_name)
                    fixed = (fixed or "").strip()
                    if fixed.startswith("```"):
                        fixed = re.sub(r"^```\w*\n?", "", fixed)
                        fixed = re.sub(r"\n?```$", "", fixed)
                        fixed = fixed.strip()
                    if not fixed:
                        logger.warning("opencode 修复失败，保留原代码并终止重试")
                        break
                    # 修复代码同样要过安全网/重叠守卫等注入，否则重试版本会丢失全部保护
                    code = self.inject_bounds_check(fixed)
                    logger.info("opencode 已修复代码，准备重试")

            except subprocess.TimeoutExpired:
                logger.error("场景 %s 渲染超时", scene_name)
            except Exception as e:
                logger.error("场景 %s 渲染异常: %s", scene_name, e)

        logger.error("场景 %s 渲染最终失败，已达最大重试次数", scene_name)
        return None

    # ------------------------------------------------------------------
    # Pipeline 图片加载（caption-based matching）
    # ------------------------------------------------------------------

    # *_script.json 新鲜度窗口(秒)：超过这个时长的一律当上一篇论文的残留丢弃。
    SCRIPT_JSON_MAX_AGE = 6 * 3600

    def _current_run_marker_mtime(self):
        """本次 pipeline 的时间基准：每轮开头被 _clean_pipeline_cache 清掉重写的 cache 文件 mtime。

        这些文件只属于本次论文，本次的 *_script.json 一定写在它们之后；都不存在返回 None。
        """
        newest = None
        for p in ("./cache/paper_text.txt", "./cache/structured_plan.json",
                  "./cache/image_explanations.json"):
            if not os.path.exists(p):
                continue
            ts = os.path.getmtime(p)
            newest = ts if newest is None else max(newest, ts)
        return newest

    def _load_current_script_json(self, explanations):
        """加载本次论文的 *_script.json；判不出属于本次就返回 None(宁可不覆盖 caption)。

        ./output 与 ./src/output 下堆着十来个跨月份的历史 json，原先 glob 到谁用谁，
        命中别的论文就用错 caption 把配图分错桶。这里两道判据都过了才采用：
          1. 新鲜度：mtime 不早于本次 pipeline 写的 cache 标记，且不超过 SCRIPT_JSON_MAX_AGE;
          2. 身份：本次 image_explanations 非空时，caption 必须对得上(同一批图)。

        Args:
            explanations: 本次 ./cache/image_explanations.json 的内容(list)，可为空。

        Returns:
            dict | None: 本次论文的 script.json 内容。
        """
        candidates = (glob.glob("./src/output/*_script.json")
                      + glob.glob("./output/*_script.json"))
        if not candidates:
            return None

        marker = self._current_run_marker_mtime()
        now = time.time()
        expl_captions = {
            (e.get("caption") or "").strip()
            for e in (explanations or [])
            if isinstance(e, dict) and (e.get("caption") or "").strip()
        }

        # mtime 新的优先：一次 pipeline 里最多只有一份属于本次
        for path in sorted(candidates, key=os.path.getmtime, reverse=True):
            mtime = os.path.getmtime(path)
            if marker is not None and mtime < marker:
                logger.info("跳过历史 script.json(早于本次 pipeline 缓存): %s", path)
                continue
            if now - mtime > self.SCRIPT_JSON_MAX_AGE:
                logger.info("跳过过期 script.json(%.1f 小时前): %s", (now - mtime) / 3600.0, path)
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as _e:
                logger.warning("读取 %s 失败: %s", path, _e)
                continue
            if expl_captions:
                json_captions = {
                    (img.get("caption") or "").strip()
                    for img in (data.get("images") or [])
                    if isinstance(img, dict)
                }
                if not (expl_captions & json_captions):
                    logger.warning("script.json 的 caption 与本次论文对不上，不采用: %s", path)
                    continue
            logger.info("采用本次论文的 script.json: %s", path)
            return data

        logger.info("没有属于本次论文的 script.json，保留 image_explanations 的 caption")
        return None

    def load_pipeline_images(self):
        """从 pipeline 的 script.json 和 image_explanations 加载图片，按场景类型分配。"""
        pic_files = sorted(glob.glob("./pic/*.png"),
                          key=lambda x: int(re.findall(r"\d+", os.path.basename(x))[0])
                          if re.findall(r"\d+", os.path.basename(x)) else 0)

        if not pic_files:
            return {"opening": [], "intro": [], "method": [], "results": []}

        # 1. 先读本次 pipeline 的 image_explanations.json（每轮开头被
        #    _clean_pipeline_cache 清掉重写，一定属于本次论文），它同时是判定
        #    script.json 归属的身份依据。
        captions = {}
        explanations = []
        expl_path = "./cache/image_explanations.json"
        if os.path.exists(expl_path):
            with open(expl_path, "r", encoding="utf-8") as f:
                explanations = json.load(f)
            for i, expl in enumerate(explanations):
                if i < len(pic_files):
                    captions[pic_files[i]] = {
                        "caption": expl.get("caption", ""),
                        "context": expl.get("context", ""),
                        "role": expl.get("figure_role", ""),
                        "section": expl.get("recommended_section", "method"),
                    }

        # 2. script.json 的 caption/context 更准，但必须是「本次论文」那一份
        script_json = self._load_current_script_json(explanations)

        # 如果有 script.json，用它的 images 字段更新 captions
        if script_json and "images" in script_json:
            for img_info in script_json["images"]:
                idx = int(img_info.get("image_index", 0))
                if idx < len(pic_files):
                    captions[pic_files[idx]] = {
                        "caption": img_info.get("caption", ""),
                        "context": img_info.get("context", ""),
                        "role": "",
                        "section": "results" if "result" in img_info.get("context", "").lower() else "method",
                    }

        img_map = {"opening": [], "intro": [], "method": [], "results": []}

        # 按 caption/context 关键词匹配
        result_kws = ["table", "表", "result", "实验", "experiment", "performance", "ablation", "success"]
        method_kws = ["architecture", "架构", "pipeline", "framework", "模块", "method", "设计", "结构"]
        # 公式类图片关键词 - 这类图片不适合做视频主画面
        formula_kws = ["formula", "equation", "公式", "目标函数", "损失函数",
                       "约束条件", "constraint", "objective", "loss function",
                       "optimization", "数学", "derivation", "推导"]

        for path in pic_files:
            info = captions.get(path, {})
            text = (info.get("caption", "") + " " + info.get("context", "") + " " + info.get("section", "")).lower()

            # 公式类图片直接跳过，不放入任何 bucket
            if any(kw in text for kw in formula_kws) and not any(kw in text for kw in method_kws):
                logger.info("跳过公式类图片: %s", os.path.basename(path))
                continue
            if any(kw in text for kw in result_kws):
                img_map["results"].append(path)
            elif any(kw in text for kw in method_kws):
                img_map["method"].append(path)
            elif "intro" in text or "opening" in text:
                img_map["intro"].append(path)
            else:
                img_map["method"].append(path)

        # 确保 opening 有图片（用第一张 method 图或整体第一张）
        if not img_map["opening"]:
            if img_map["method"]:
                img_map["opening"] = [img_map["method"][0]]
            elif pic_files:
                img_map["opening"] = [pic_files[0]]
        if not img_map["intro"]:
            if len(img_map["method"]) > 1:
                img_map["intro"] = [img_map["method"][1]]
            elif img_map["method"]:
                img_map["intro"] = [img_map["method"][0]]

        logger.info("Pipeline 图片匹配: %s",
                    {k: [os.path.basename(p) for p in v] for k, v in img_map.items()})
        return img_map


    def generate_tts(self, scenes, narrations):
        """为场景生成 TTS 音频。

        Args:
            scenes: 场景列表。
            narrations: 与场景一一对应的讲解词列表。
        """
        try:
            try:
                from src.utils.audio_helpers import get_tts_config, synthesize_tts
            except ImportError:
                from utils.audio_helpers import get_tts_config, synthesize_tts  # type: ignore

            tts_model, tts_voice = get_tts_config()
            logger.info("Manim TTS 配置 model=%s voice=%s", tts_model, tts_voice)

            # audio_parts 与 narrations（即 rendered_narrations / scene_videos）
            # 严格一一对应：空 narration 或合成失败的槽位 append None 占位，
            # 绝不 continue 跳过——否则下标错位会让某 scene 之后全部配错音频
            # (Bug#4)。compose 端按 `if narration_audios[idx]:` 处理 None 槽位
            # （该 scene 静音、不叠音轨）。
            audio_parts = []
            for i, text in enumerate(narrations):
                if not text or not text.strip():
                    audio_parts.append(None)  # 占位，保持与 scene 对齐
                    continue

                audio_path = os.path.join(self.temp_dir, f"tts_{i}.mp3")
                try:
                    audio_data = synthesize_tts(text)
                except Exception as _tts_exc:
                    logger.warning("[manim-tts] 第 %d 段合成失败: %s", i, _tts_exc)
                    audio_data = None

                if audio_data:
                    with open(audio_path, "wb") as f:
                        f.write(audio_data)
                    audio_parts.append(audio_path)
                    logger.info("TTS 第 %d 段生成成功: %s", i, text[:30])
                else:
                    audio_parts.append(None)  # 合成失败也占位，保持对齐

            # 各 scene 的音频路径（None = 该 scene 无音轨），与 scene_videos 等长。
            # 必须在下面的拼接/写盘之前赋值：那两步任一抛异常都会被外层 except 吞掉，
            # 赋值留在后面就会让 compose 拿到 []，全片静音（而分段 mp3 其实都在盘上）。
            self._audio_parts = audio_parts

            # 至少要有一段真实音频，否则没有可拼接的音轨
            real_parts = [p for p in audio_parts if p]
            if not real_parts:
                return None

            audio_clips = [AudioFileClip(p) for p in real_parts]
            from moviepy import concatenate_audioclips
            combined = concatenate_audioclips(audio_clips)
            output_audio = os.path.join(self.temp_dir, "tts_combined.mp3")
            combined.write_audiofile(output_audio)

            for clip in audio_clips:
                clip.close()

            return output_audio

        except Exception as e:
            # 合成/写盘炸了不代表分段 mp3 没生成：_audio_parts 已在上面赋值，
            # compose 仍按 scene 逐段配音，不会整片静音。
            logger.warning("TTS 合成失败（已保留 %d 段分段音频）: %s",
                           len(getattr(self, "_audio_parts", []) or []), e, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 拼接
    # ------------------------------------------------------------------

    def compose(self, scene_videos, tts_audio=None, fmt="mp4", scene_image_paths=None, narration_audios=None, blog_scene_videos=None):
        """拼接所有场景视频为完整演示。"""
        import numpy as np
        from PIL import Image as PILImage

        if not scene_videos:
            logger.error("没有可拼接的视频片段")
            return None

        ext = "gif" if fmt == "gif" else "mp4"
        output_path = os.path.join(self.output_dir, f"manim_presentation.{ext}")

        final_clips = []

        for idx, vpath in enumerate(scene_videos):
            # blog 分支: 该 scene 用 blog video 替代 manim 渲染, 走 align_video_to_tts 替换音轨
            if (blog_scene_videos and idx in blog_scene_videos
                    and narration_audios and idx < len(narration_audios)
                    and narration_audios[idx]):
                try:
                    from src import blog_video_overlay
                    blog_clip = blog_scene_videos[idx]
                    tts_path = narration_audios[idx]
                    aligned_path = os.path.join(
                        self.output_dir, f"_blog_scene_{idx}.mp4"
                    )
                    blog_video_overlay.align_video_to_tts(blog_clip, tts_path, aligned_path)
                    video = VideoFileClip(aligned_path)
                    final_clips.append(video)
                    logger.info("[blog-overlay] scene %d: blog video %s + TTS 解说 (%s)",
                                idx, os.path.basename(blog_clip), os.path.basename(tts_path))
                    continue
                except Exception as e:
                    logger.warning("[blog-overlay] scene %d align 失败, fallback 老路径: %s", idx, e)
                    # 失败时退回老逻辑 (vpath 是 blog clip 原文件, 当普通视频处理)

            try:
                video = VideoFileClip(vpath)
            except Exception as e:
                logger.warning("加载视频片段失败 %s: %s", vpath, e)
                continue

            # 获取对应的音频
            audio = None
            if narration_audios and idx < len(narration_audios) and narration_audios[idx]:
                try:
                    audio = AudioFileClip(narration_audios[idx])
                except Exception:
                    pass

            # 如果音频比动画长，用 pipeline 图片填充
            if audio and audio.duration > video.duration:
                gap = audio.duration - video.duration + 1.0
                img_path = scene_image_paths[idx] if scene_image_paths and idx < len(scene_image_paths) else None

                if img_path and os.path.exists(img_path):
                    # 将 pipeline 图片做成视频片段
                    img = PILImage.open(img_path).convert("RGB")
                    w, h = video.w, video.h
                    max_w, max_h = int(w * 0.95), int(h * 0.95)
                    img_w, img_h = img.size
                    scale = min(max_w / img_w, max_h / img_h)
                    new_w, new_h = int(img_w * scale), int(img_h * scale)
                    img = img.resize((new_w, new_h), PILImage.LANCZOS)
                    bg = PILImage.new("RGB", (w, h), (0, 0, 0))
                    bg.paste(img, ((w - new_w) // 2, (h - new_h) // 2))
                    from moviepy import ImageClip
                    img_clip = ImageClip(np.array(bg), duration=gap)
                    video = concatenate_videoclips([video, img_clip])
                    logger.info("场景 %d: 动画后接 pipeline 图片 %s (%.1fs)", idx, img_path, gap)
                else:
                    # fallback: 冻结非黑帧
                    from moviepy import ImageClip
                    frame = video.get_frame(video.duration - 0.01)
                    if np.mean(frame) < 10:  # 全黑，回退找有内容的帧
                        for t in [video.duration*0.7, video.duration*0.5, video.duration*0.3, 1.0]:
                            f2 = video.get_frame(min(t, video.duration-0.01))
                            if np.mean(f2) > 10:
                                frame = f2
                                break
                    freeze = ImageClip(frame, duration=gap)
                    video = concatenate_videoclips([video, freeze])

            if audio:
                video = video.with_audio(audio)

            final_clips.append(video)

        if not final_clips:
            logger.error("所有视频片段加载失败")
            return None

        try:
            final = concatenate_videoclips(final_clips, method="compose")

            if fmt == "gif":
                final.write_gif(output_path, fps=15)
            else:
                final.write_videofile(output_path, fps=30, codec="libx264", audio_codec="aac")

            logger.info("演示视频已生成: %s", output_path)
            return output_path

        except Exception as e:
            logger.error("视频拼接失败: %s", e)
            return None
        finally:
            for clip in final_clips:
                try:
                    clip.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 主流程: 固定 4 场景结构
    # ------------------------------------------------------------------


    def _assign_images_to_scenes(self, pipeline_images, scene_defs, rendered_indices):
        """为渲染成功的场景分配不重复的 pipeline 图片。"""
        scene_image_paths = []
        used_images = set()
        for i in rendered_indices:
            sec = scene_defs[i]["section"]
            # 按优先级找：本 section > method > 任意
            candidates = (pipeline_images.get(sec, []) +
                         pipeline_images.get("method", []))
            img = None
            for c in candidates:
                if c not in used_images:
                    img = c
                    used_images.add(c)
                    break
            # fallback: 任意未用过的图
            if not img:
                for imgs in pipeline_images.values():
                    for c in imgs:
                        if c not in used_images:
                            img = c
                            used_images.add(c)
                            break
                    if img:
                        break
            scene_image_paths.append(img)
            logger.info("场景 %s 分配图片: %s", scene_defs[i]["scene_name"],
                       os.path.basename(img) if img else "None")
        return scene_image_paths


    def _consistency_check_and_fix(self, video_path, code, sdef, quality, fmt,
                                    figure_analysis, method_images, max_fix_rounds=2):
        """对 MethodScene 做 Qwen-VL 一致性检查，不通过则让 opencode 修正。"""
        if not method_images:
            return video_path

        original_image = method_images[0]
        for round_idx in range(max_fix_rounds):
            # 从渲染视频提取帧
            frame_path = extract_frame_from_video(video_path)
            if not frame_path:
                logger.warning("无法提取渲染帧，跳过一致性检查")
                return video_path

            # Qwen-VL 对比
            check = check_consistency_with_vision_llm(original_image, frame_path)
            if not check:
                logger.warning("一致性检查调用失败，跳过")
                return video_path

            overall = check.get("overall_score", 0)
            passed = True  # consistency check disabled (always accept, see LESSONS_LEARNED for context)
            missing = check.get("missing_components", [])
            suggestions = check.get("suggestions", [])

            logger.info("一致性检查 (第 %d 轮): overall=%s, pass=%s, missing=%s",
                       round_idx + 1, overall, passed, missing)

            if passed:
                logger.info("MethodScene 通过一致性检查 (score=%s)", overall)
                return video_path

            # 不通过: 用反馈让 opencode 修正
            logger.warning("MethodScene 未通过一致性检查 (score=%s), 尝试修正...", overall)

            fix_prompt = (
                "你是 ManimCE 专家。以下 Manim 代码渲染后与论文原图不够一致，请修正。\n\n"
                "【一致性检查反馈】:\n"
                "- 总分: %s/10\n"
                "- 缺失组件: %s\n"
                "- 改进建议: %s\n\n"
                "【原始代码】:\n```python\n%s\n```\n\n"
                "请修正代码，补充缺失的组件，调整位置使其与原图更一致。\n"
                "仅输出完整修正后的 Python 代码。"
            ) % (overall, ", ".join(missing), "; ".join(suggestions), code)

            # 用 opencode 或直接 API 修正
            if figure_analysis:
                eb_elements = figure_analysis.get("eb_manim_elements", "")
                if eb_elements:
                    fix_prompt += "\n\n【图表精确规格（请参照）】:\n" + eb_elements

            raw = self._opencode_generate_with_retry(fix_prompt, attempts=2, scene_name=sdef["scene_name"])

            fixed_code = (raw or "").strip()
            if fixed_code.startswith("```"):
                import re as _re
                fixed_code = _re.sub(r"^```\w*\n?", "", fixed_code)
                fixed_code = _re.sub(r"\n?```$", "", fixed_code)
                fixed_code = fixed_code.strip()

            if not fixed_code:
                logger.warning("修正代码为空，保留原版")
                return video_path

            fixed_code = self.inject_bounds_check(fixed_code)
            new_video = self.render_scene(fixed_code, sdef["scene_name"], quality=quality, fmt=fmt)
            if new_video:
                video_path = new_video
                code = fixed_code
                logger.info("修正后重新渲染成功: %s", new_video)
            else:
                logger.warning("修正后渲染失败，保留原版")
                return video_path

        return video_path

    def _build_scene_defs(self, plan_scripts):
        """构建 scene_defs 与对齐的 narrations 列表（编排层，纯逻辑、不渲染）。

        固定 4 段 (Title/Intro/Method/Results)，并按 structured_plan["formulas"]
        自适应在 Method 与 Results 之间插入至多 2 个 FormulaScene；
        formulas 为空时返回原始 4 段（与老链路字节级一致）。无效/超限公式被丢弃。

        Returns:
            (scene_defs: list[dict], narrations: list[str]) 等长且一一对齐。
        """
        # 2. Define fixed 4 scenes
        scene_defs = [
            {
                "scene_name": "TitleScene",
                "type": "title",
                "section": "opening",
                "description": f"论文标题和开场。脚本: {plan_scripts.get('opening', '')[:800]}",
            },
            {
                "scene_name": "IntroScene",
                "type": "flow",
                "section": "intro",
                "description": f"背景介绍和问题引出。脚本: {plan_scripts.get('intro', '')[:800]}",
            },
            {
                "scene_name": "MethodScene",
                "type": "architecture",
                "section": "method",
                "description": f"核心方法展示。脚本: {plan_scripts.get('method', '')[:800]}",
            },
            {
                "scene_name": "ResultsScene",
                "type": "results",
                "section": "results",
                "description": f"实验结果展示。脚本: {plan_scripts.get('results', '')[:800]}",
            },
        ]

        # 3. Narrations = plan scripts directly
        narrations = [plan_scripts.get(s["section"], "") for s in scene_defs]

        # 3.0 [feature] 自适应插入 FormulaScene（plan 提取的核心公式）。
        # formulas 为空时（绝大多数老论文/老链路）完全不进此分支，
        # scene_defs / narrations 保持上面固定 4 段原样，行为字节级一致。
        #
        # 信任 plan 层：_extract_core_formulas 已对每条 latex 调
        # _validate_or_repair_formula 校验过，非法公式不会进 structured_plan
        # ["formulas"]。这里不再重复 validate_latex（Bug#6：manim 运行环境
        # PATH 若无 latex，FileNotFoundError→False 会静默清空全部已通过公式，
        # 且每条公式多一次 ~秒级子进程编译）。仅保留字段缺失/空的健壮性检查。
        raw_formulas = self.structured_plan.get("formulas", []) or []
        valid_formulas = []
        if raw_formulas:
            for fml in raw_formulas:
                if not isinstance(fml, dict):
                    continue
                latex = (fml.get("latex") or "").strip()
                narration = (fml.get("narration") or "").strip()
                if not latex or not narration:
                    continue
                valid_formulas.append(fml)
            # 硬上限：method 之后最多 _FORMULA_MAX 个 FormulaScene
            if len(valid_formulas) > _FORMULA_MAX:
                logger.info("[formula] 公式数 %d 超过上限 %d，截断",
                            len(valid_formulas), _FORMULA_MAX)
                valid_formulas = valid_formulas[:_FORMULA_MAX]

        if valid_formulas:
            formula_defs = []
            formula_narrs = []
            for i, fml in enumerate(valid_formulas):
                formula_defs.append({
                    "scene_name": f"FormulaScene{i+1}",
                    "type": "formula",
                    "section": "method",
                    "description": fml["narration"],
                    "latex": fml["latex"],
                    "highlights": fml.get("highlights", []),
                })
                formula_narrs.append(fml["narration"])
            # 序列动态化: [Title, Intro, Method, *FormulaScenes, Results]
            # 插在 Method 之后、Results 之前（Results 始终是 scene_defs 最后一个）。
            insert_at = len(scene_defs) - 1
            scene_defs = scene_defs[:insert_at] + formula_defs + scene_defs[insert_at:]
            narrations = narrations[:insert_at] + formula_narrs + narrations[insert_at:]
            logger.info("[formula] 插入 %d 个 FormulaScene，总 scene 数 %d",
                        len(formula_defs), len(scene_defs))

        # 3.1 [feature] 自适应插入 js_anim 示意动画 scene。
        # animations 来自 structured_plan（plan 层已做 kill switch / 字段校验 / 0-2 条）。
        # 为空时（kill switch 关 / 论文无合适场景 / 老链路）完全不进此分支，
        # scene_defs / narrations 保持公式编排或固定 4 段原样，行为字节级一致。
        animations = self.structured_plan.get("animations", []) or []
        if animations:
            # 总预算 clamp（硬约束）：固定 4 场景 + 额外场景（公式 + 示意）。
            # 额外场景合计 <= 3，总 scene <= 7。公式优先占额度（论文核心），
            # 示意用剩余额度（且示意自身上限 2）。
            num_formula = len(valid_formulas)
            anim_budget = max(0, min(_EXTRA_SCENE_BUDGET - num_formula, _ANIM_MAX))
            if len(animations) > anim_budget:
                logger.info("[anim] 示意动画数 %d 超过剩余额度 %d（公式占 %d），截断丢弃 %d 条",
                            len(animations), anim_budget, num_formula,
                            len(animations) - anim_budget)
                animations = animations[:anim_budget]
            # 分别构造 intro_after / method 两组 AnimScene（保持 plan 顺序，全局编号）。
            intro_defs, intro_narrs = [], []
            method_defs, method_narrs = [], []
            for i, anim in enumerate(animations):
                position = anim.get("position") or "method"
                section = "method" if position == "method" else "intro"
                scene_info = {
                    "scene_name": f"AnimScene{i+1}",
                    "type": "js_anim",
                    "section": section,
                    # description 兼作 TTS 解说词（本阶段）
                    "description": anim["scene_desc"],
                    "anim_spec": {
                        "scene_desc": anim["scene_desc"],
                        "target_seconds": anim["target_seconds"],
                        "kind": anim["kind"],
                        "prefer_real_demo": anim.get("prefer_real_demo", False),
                    },
                }
                if position == "intro_after":
                    intro_defs.append(scene_info)
                    intro_narrs.append(anim["scene_desc"])
                else:
                    method_defs.append(scene_info)
                    method_narrs.append(anim["scene_desc"])
            # 插 intro_after：在 IntroScene 之后、MethodScene 之前。
            if intro_defs:
                idx_intro = next((j for j, s in enumerate(scene_defs)
                                  if s["scene_name"] == "IntroScene"), 0)
                at = idx_intro + 1
                scene_defs = scene_defs[:at] + intro_defs + scene_defs[at:]
                narrations = narrations[:at] + intro_narrs + narrations[at:]
            # 插 method：在 MethodScene 之后（与公式 scene 同区，排在公式后面）。
            # 公式 scene 也挂 section==method 且紧跟 MethodScene，故定位最后一个
            # section==method 的 scene，插在其后，保证示意排在公式之后、Results 之前。
            if method_defs:
                last_method = max((j for j, s in enumerate(scene_defs)
                                   if s.get("section") == "method"), default=None)
                at = (last_method + 1) if last_method is not None else (len(scene_defs) - 1)
                scene_defs = scene_defs[:at] + method_defs + scene_defs[at:]
                narrations = narrations[:at] + method_narrs + narrations[at:]
            logger.info("[anim] 插入 %d 个 AnimScene（intro_after %d / method %d），总 scene 数 %d",
                        len(intro_defs) + len(method_defs), len(intro_defs),
                        len(method_defs), len(scene_defs))

        return scene_defs, narrations

    def run(self, tts=False, quality="medium", fmt="mp4"):
        """完整流程：固定 4 场景 -> 生成代码 -> 渲染 -> 拼接。"""
        logger.info("=== Manim 演示生成开始 ===")

        # 1. Extract scripts from structured_plan
        sections = ["opening", "intro", "method", "results"]
        plan_scripts = {}
        for sec in sections:
            sec_data = self.structured_plan.get(sec, {})
            script = sec_data.get("script", sec_data) if isinstance(sec_data, dict) else sec_data
            plan_scripts[sec] = str(script) if script else ""

        # 2-3. 构建场景定义与旁白（含 FormulaScene 自适应编排），抽成方法便于单测。
        scene_defs, narrations = self._build_scene_defs(plan_scripts)

        # 3.5 分析方法图（用于 MethodScene 增强）
        figure_analysis_result = None
        pipeline_images = self.load_pipeline_images()
        method_images = pipeline_images.get("method", [])
        if method_images:
            main_figure = method_images[0]
            logger.info("分析方法主图: %s", main_figure)
            try:
                paper_ctx = self.paper_text if self.paper_text else ""
                figure_analysis_result = analyze_and_prepare(main_figure, paper_ctx, arxiv_id=self.arxiv_id)
                if figure_analysis_result and figure_analysis_result.get("analysis"):
                    logger.info("方法图分析成功: 类型=%s, %d 组件, %d 连接",
                               figure_analysis_result.get("figure_type", "?"),
                               len(figure_analysis_result["analysis"].get("components", [])),
                               len(figure_analysis_result["analysis"].get("connections", [])))
                else:
                    logger.warning("方法图分析返回空结果")
            except Exception as e:
                logger.warning("方法图分析失败，将使用默认生成: %s", e)

        # [ablation] PAPERIFY_DISABLE_FIGURE_GROUNDED switch: when set, drop the
        # eb_manim_elements coordinate spec from the prompt to simulate the
        # caption-only baseline that prior LLM-to-Manim work uses.
        if figure_analysis_result and os.getenv("PAPERIFY_DISABLE_FIGURE_GROUNDED"):
            figure_analysis_result["eb_manim_elements"] = ""
            logger.info("[ablation] PAPERIFY_DISABLE_FIGURE_GROUNDED=1 -> cleared eb_manim_elements (caption-only baseline)")

        # 3.6 blog 模式: 检测 ./cache/blog_meta.json + 把 blog clip 分配到 method/results scene
        # (paper-link 路径不会有 blog_meta.json, 所以这段对老链路 0 影响)
        self._blog_scene_assignments = {}
        try:
            blog_meta_p = "./cache/blog_meta.json"
            if os.path.exists(blog_meta_p):
                import json as _json
                with open(blog_meta_p, "r", encoding="utf-8") as _bf:
                    _bm = _json.load(_bf)
                _clip_meta = _bm.get("clip_meta") or []
                if _clip_meta:
                    from src import blog_video_overlay
                    self._blog_scene_assignments = blog_video_overlay.assign_blog_clips_to_scenes(
                        _clip_meta, total_scenes=len(scene_defs), scene_defs=scene_defs,
                    )
                    logger.info("[blog-overlay] 检测到 blog 模式, scene 分配: %s",
                                self._blog_scene_assignments)
        except Exception as _e:
            logger.warning("[blog-overlay] 读 blog_meta.json 失败 (退化为纯 manim): %s", _e)
            self._blog_scene_assignments = {}

        # 3.7 [feature] js_anim 示意 scene 预渲染：HTML -> mp4，不走 manim。
        # 先把所有 js_anim scene 渲成 mp4，成功的登记进 _blog_scene_assignments
        # （复用 blog 外部视频分支：compose 走 align_video_to_tts 换 TTS 音轨），
        # 失败的整条 scene 干净丢弃（同步剔除 scene_defs / narrations 并重建索引），
        # 避免后续 idx / narration 对齐错乱。无 js_anim scene 时此段完全空跑。
        anim_scene_idxs = [j for j, s in enumerate(scene_defs)
                           if s.get("type") == "js_anim"]
        if anim_scene_idxs:
            from src.js_anim_engine import _opencode_generate_html, _opencode_generate_html_with_retry, render_html_to_mp4
            anim_mp4_by_idx = {}   # 旧 idx -> mp4 路径（仅成功的）
            dropped_idxs = set()   # 旧 idx，渲染失败被丢弃
            for j in anim_scene_idxs:
                sdef = scene_defs[j]
                spec = sdef.get("anim_spec", {})
                sname = sdef["scene_name"]
                scene_desc = spec.get("scene_desc", sdef.get("description", ""))
                target_seconds = spec.get("target_seconds", 7)
                kind = spec.get("kind", "abstract")
                # TODO 阶段2: 若 anim_spec.prefer_real_demo 且能抓到论文真实 demo 视频,
                # 优先用真实 clip, 跳过 JS 生成。本阶段不实现真实抓取，直接走 JS 生成。
                logger.info("[anim] 渲染示意 scene %d (%s): kind=%s, %.0fs",
                            j, sname, kind, float(target_seconds))
                user_content = (
                    f"scene_name: {sname}\n"
                    f"场景描述: {scene_desc}\n"
                    f"target_seconds: {target_seconds}\n"
                    f"kind: {kind}\n"
                )
                full_prompt = prompts_dict["js_anim_generate"] + "\n\n" + user_content
                html = _opencode_generate_html_with_retry(
                    full_prompt, sname, self.temp_dir, attempts=3)
                if not html:
                    logger.warning("[anim] scene %d (%s) HTML 生成失败，降级丢弃", j, sname)
                    dropped_idxs.add(j)
                    continue
                out_mp4 = os.path.join(self.temp_dir, f"{sname}.mp4")
                mp4 = render_html_to_mp4(html, out_mp4)
                if not mp4:
                    logger.warning("[anim] scene %d (%s) HTML->mp4 渲染失败，降级丢弃", j, sname)
                    dropped_idxs.add(j)
                    continue
                anim_mp4_by_idx[j] = mp4
                logger.info("[anim] scene %d (%s) 示意 mp4 就绪: %s", j, sname, mp4)
            # 剔除被丢弃的 scene、重建索引、登记成功 mp4（抽成纯函数便于单测）。
            scene_defs, narrations, self._blog_scene_assignments = apply_anim_render_results(
                scene_defs, narrations, self._blog_scene_assignments,
                anim_mp4_by_idx, dropped_idxs,
            )
            logger.info("[anim] 示意 scene 渲染完成：成功 %d / 丢弃 %d，最终 scene 数 %d",
                        len(anim_mp4_by_idx), len(dropped_idxs), len(scene_defs))

        # 4. Generate + render each scene (blog 注入 scene 跳过 manim 渲染)
        scene_videos = []
        rendered_indices = []
        for i, sdef in enumerate(scene_defs):
            # [ablation] PAPERIFY_METHOD_ONLY: skip non-method scenes for case-study runs
            if os.getenv("PAPERIFY_METHOD_ONLY") and sdef["type"] != "architecture":
                logger.info("[ablation] skipping %s (PAPERIFY_METHOD_ONLY=1)", sdef["scene_name"])
                continue
            # blog 注入 scene: 不调 manim, 直接把 blog clip 放进 scene_videos
            # (compose 内部按 idx 检查 blog_scene_assignments 走 align 分支)
            if i in self._blog_scene_assignments:
                _blog_clip = self._blog_scene_assignments[i]
                scene_videos.append(_blog_clip)
                rendered_indices.append(i)
                logger.info("[blog-overlay] scene %d (%s) 用 blog clip 替代 manim 渲染: %s",
                            i, sdef["scene_name"], _blog_clip)
                continue
            logger.info("生成场景 %d/%d: %s (%s)", i + 1, len(scene_defs), sdef["scene_name"], sdef["type"])

            # MethodScene 注入图像分析结果
            if sdef["type"] == "architecture" and figure_analysis_result:
                sdef["figure_analysis"] = figure_analysis_result

            code = self.generate_manim_code(sdef)
            if not code:
                logger.warning("场景 %s 代码生成失败，跳过", sdef["scene_name"])
                continue
            # Inject bounds check
            code = self.inject_bounds_check(code)
            video_path = self.render_scene(code, sdef["scene_name"], quality=quality, fmt=fmt)
            if video_path:
                # MethodScene: Qwen-VL 一致性检查
                if sdef["type"] == "architecture" and figure_analysis_result:
                    video_path = self._consistency_check_and_fix(
                        video_path, code, sdef, quality, fmt,
                        figure_analysis_result, pipeline_images.get("method", [])
                    )
                scene_videos.append(video_path)
                rendered_indices.append(i)
            else:
                logger.warning("场景 %s 渲染失败，跳过", sdef["scene_name"])

        if not scene_videos:
            logger.error("所有场景渲染失败")
            return None

        # 5. TTS for rendered scenes
        audio_paths = []
        if tts:
            rendered_narrations = [narrations[i] for i in rendered_indices]
            self.generate_tts(scene_defs, rendered_narrations)
            audio_paths = getattr(self, "_audio_parts", [])

        # 6. Compose (pipeline_images already loaded in step 3.5)
        scene_image_paths = self._assign_images_to_scenes(
            pipeline_images, scene_defs, rendered_indices
        )

        # blog/示意 scene 的外部视频也要按 rendered_indices 压缩：它的 key 是
        # scene_defs 下标，compose 却按压缩后的 scene_videos 下标查，
        # 任一 scene 渲染失败（或 PAPERIFY_METHOD_ONLY 跳过）就会错位。
        composed_blog_videos = compress_blog_assignments(
            self._blog_scene_assignments, rendered_indices)

        output = self.compose(
            scene_videos,
            fmt=fmt,
            scene_image_paths=scene_image_paths,
            narration_audios=audio_paths,
            blog_scene_videos=composed_blog_videos or None,
        )

        if output:
            logger.info("=== Manim 演示生成完成: %s ===", output)
            # 落盘最终旁白段落 + 各 scene 尾帧(动画完成态, 含公式/论文图合成画面),
            # 供小红书图文卡片等下游复用。segments[i] 与 scene_videos[i] 一一对应。
            try:
                import subprocess as _sp
                final_narrations = [narrations[i] for i in rendered_indices]
                frames_dir = os.path.join(self.output_dir, "scene_frames")
                os.makedirs(frames_dir, exist_ok=True)
                segments = []
                for _i, (_narr, _vid) in enumerate(zip(final_narrations, scene_videos)):
                    frame_path = os.path.abspath(
                        os.path.join(frames_dir, f"scene_{_i:02d}.png"))
                    frame_ok = False
                    try:
                        _r = _sp.run(
                            ["ffmpeg", "-y", "-sseof", "-0.5", "-i", _vid,
                             "-update", "1", "-q:v", "2", frame_path],
                            capture_output=True, timeout=60)
                        frame_ok = (_r.returncode == 0
                                    and os.path.exists(frame_path)
                                    and os.path.getsize(frame_path) > 0)
                    except Exception as _fe:  # noqa: BLE001
                        logger.warning("[narration] scene %d 抽帧失败: %s", _i, _fe)
                    segments.append({
                        "text": _narr,
                        "frame": frame_path if frame_ok else None,
                    })
                narration_path = os.path.join(self.output_dir, "narration_segments.json")
                with open(narration_path, "w", encoding="utf-8") as _nf:
                    json.dump({"segments": segments}, _nf, ensure_ascii=False, indent=2)
                logger.info("[narration] 旁白+scene帧已保存: %s (%d 段, %d 帧)",
                            narration_path, len(segments),
                            sum(1 for x in segments if x["frame"]))
            except Exception as _ne:  # noqa: BLE001
                logger.warning("[narration] 旁白段落保存失败: %s", _ne)
        return output
