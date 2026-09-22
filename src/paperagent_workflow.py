import os
import json
import sys
from openai import OpenAI
from pdf_processor import PDFProcessor
from video_creator import VideoCreator
from get_website_data import download_videos_files
from config import FONT_PATH
from get_arxiv_latest import get_paper_from_arxiv,filter_papers_by_date,Paper
from generate_cover import generate_cover
# from auto_upload_bilibili import upload_video_to_bilibili
import dashscope
import glob
from moviepy import *
from PIL import Image
from concurrent.futures import ThreadPoolExecutor
import shutil
import re
import datetime
import logging

from src.llm_tools.llm_agent import (
    MANUALLY_EXTRACT_IMAGES,
    generate_summary,
    generate_short_summary,
    generate_video_title,
    generate_origin_title,
    generate_structured_video_plan,
    structured_plan_to_text,
    get_paper_demo_website,
    rate_image_importance,
    select_top_images,
    add_context_to_image_explanations,
)
# image agent for qwen-vl
try:
    from src.llm_tools.image_agent import ImageAgent
except Exception:
    # fallback import path
    from llm_tools.image_agent import ImageAgent

try:
    from src.extract_caption import extract_captions_from_pdf
except Exception:
    try:
        from extract_caption import extract_captions_from_pdf
    except Exception:
        extract_captions_from_pdf = None

# 配置日志记录
logging.basicConfig(
    level=logging.DEBUG,  # 修改为 DEBUG 级别
    format='%(asctime)s - %(levelname)s - %(message)s'
)
file_handler = logging.FileHandler('app.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(file_handler)


def _clean_pipeline_cache(cache_dir="./cache", pic_dir="./pic",
                          manim_temp_dir="./output/manim/temp",
                          manim_media_dir="./output/manim/media",
                          manim_presentation="./output/manim/manim_presentation.mp4"):
    """全量清理旧缓存，确保每次 pipeline 使用干净数据。

    覆盖 cache/pic/manim 所有会跨论文污染的产物：PDF、文本、结构化脚本、音频、
    图像解释、封面、提取图片、manim 渲染中间产物和最终视频。
    """
    import glob as _glob
    import shutil as _shutil

    cache_patterns = [
        "summary*.wav", "expl_*.wav", "test_audio_*.wav",
        "image_explanations*.json",
        "paper_text.txt", "structured_plan.json",
        "cached_pdf.pdf", "cached_pdf.meta.json",
        "dashscope_cover.png", "gemini_cover.png", "ai_cover_bg.png",
    ]
    for pattern in cache_patterns:
        for f in _glob.glob(os.path.join(cache_dir, pattern)):
            try:
                os.remove(f)
                logging.debug("清理缓存: %s", f)
            except OSError:
                pass

    if os.path.exists(pic_dir):
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            for f in _glob.glob(os.path.join(pic_dir, ext)):
                try:
                    os.remove(f)
                except OSError:
                    pass
        logging.info("已清理 pic 目录的图片: %s", pic_dir)

    for d in (manim_temp_dir, manim_media_dir):
        if os.path.exists(d):
            _shutil.rmtree(d, ignore_errors=True)
            logging.info("已清理 Manim 目录: %s", d)
    if os.path.exists(manim_presentation):
        try:
            os.remove(manim_presentation)
            logging.info("已清理 Manim 成片: %s", manim_presentation)
        except OSError:
            pass

    frames_dir = os.path.join(
        os.path.dirname(manim_presentation), "scene_frames")
    if os.path.exists(frames_dir):
        _shutil.rmtree(frames_dir, ignore_errors=True)
        logging.info("已清理旧 scene 帧目录: %s", frames_dir)
    narration_json = os.path.join(
        os.path.dirname(manim_presentation), "narration_segments.json")
    if os.path.exists(narration_json):
        try:
            os.remove(narration_json)
            logging.info("已清理旧旁白段落: %s", narration_json)
        except OSError:
            pass

    logging.info("Pipeline 缓存清理完成（全量）")


# ---- 文件名净化 ----
# LLM 生成的标题常含 "Vision/Language" 这类字符，直接拼进输出路径会指向不存在的子目录
_FILENAME_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    """净化文件名片段：剔除路径非法字符，清空后回落到 fallback。

    Args:
        name: 原始名字（通常是 LLM 生成的标题）
        fallback: 净化后为空时使用的名字

    Returns:
        str: 可安全拼进路径的文件名片段
    """
    cleaned = _FILENAME_ILLEGAL_RE.sub('', name or '').strip()
    return cleaned or fallback


# ---- 时长预算常量 ----
TTS_CHARS_PER_SECOND = 4  # 中文 TTS cosyvoice-v1 约 4 字/秒
SUMMARY_BUDGET_RATIO = 0.6  # 总结占比
IMAGE_BUDGET_RATIO = 0.4    # 图片解释占比
MAX_IMAGES_DEFAULT = 5      # 图片筛选默认上限
DURATION_OVERFLOW_RATIO = 1.1  # TTS 后兜底的弹性比例


def compute_word_budget(target_duration: int = 300, num_images: int = MAX_IMAGES_DEFAULT) -> dict:
    """根据目标视频时长计算文字预算。

    Args:
        target_duration: 目标时长（秒）
        num_images: 图片数量（用于计算每张图的预算）

    Returns:
        dict: {"total", "summary", "images", "per_image"}
    """
    total = target_duration * TTS_CHARS_PER_SECOND
    if num_images <= 0:
        return {"total": total, "summary": total, "images": 0, "per_image": 0}
    summary_budget = int(total * SUMMARY_BUDGET_RATIO)
    image_budget = total - summary_budget
    per_image = image_budget // num_images if num_images > 0 else 0
    return {
        "total": total,
        "summary": summary_budget,
        "images": image_budget,
        "per_image": per_image,
    }


def _select_top_images_with_contexts(images: list, scores: list, contexts,
                                     top_n: int = MAX_IMAGES_DEFAULT):
    """按重要性挑选图片，并把 contexts 的键重映射到筛选后列表的新位置。

    contexts 的键是原始 1-based 图号，而 ImageAgent.explain_images 是按筛选后
    列表的位置取题注（idx + 1）。挑选是「抽取」而非「前缀截断」，不重映射会让
    题注和图片系统性错配。

    Args:
        images: 原始图片列表
        scores: 与 images 等长的重要性分数
        contexts: 原始 1-based 图号 -> 题注；None / 空则原样返回
        top_n: 保留张数

    Returns:
        tuple: (筛选后的图片列表, 重映射后的 contexts)
    """
    kept_indices = select_top_images(list(range(len(images))), scores, top_n=top_n)
    selected = [images[i] for i in kept_indices]
    if not contexts:
        return selected, contexts
    remapped = {}
    for new_pos, old_idx in enumerate(kept_indices):
        caption = contexts.get(old_idx + 1)
        if caption is not None:
            remapped[new_pos + 1] = caption
    return selected, remapped


def extract_abstract_from_text(raw_text: str, limit: int = 1500) -> str:
    """Heuristic extraction of abstract text from full PDF content."""
    if not raw_text:
        return ""
    text = raw_text.strip()
    if not text:
        return ""

    pattern = re.compile(r'(?:^|\n)\s*(Abstract|ABSTRACT|摘要)[:\s]*')
    match = pattern.search(text)
    abstract = ""
    if match:
        start = match.end()
        remainder = text[start:]
        end_pattern = re.compile(r'(?:^|\n)\s*(Keywords|Index Terms|INTRODUCTION|Introduction|\d+\s+Introduction|1\.|I\.)', re.IGNORECASE)
        end_match = end_pattern.search(remainder)
        abstract = remainder[:end_match.start()] if end_match else remainder
    else:
        abstract = text[:limit]

    lines = [line.strip() for line in abstract.splitlines() if line.strip()]
    condensed = ' '.join(lines)
    return condensed[:limit]


def get_videoclips(paper_text: str = "", demo_url: str = "", download_folder: str = './pic'):
    """Download supplementary demo videos if available and return VideoFileClip list."""
    resolved_url = (demo_url or '').strip()

    if not resolved_url and paper_text:
        try:
            candidate = get_paper_demo_website(paper_text[:1500])
            if candidate:
                resolved_url = candidate.strip()
                logging.info("自动获取到的视频网址: %s", resolved_url)
        except Exception as e:
            logging.error("自动获取视频网址失败: %s", e)

    if not resolved_url:
        logging.info("未提供可用的视频网址，跳过演示视频下载")
        return []

    # 清理旧的 mp4，避免混入历史文件
    try:
        for stale_video in glob.glob(os.path.join(download_folder, '*.mp4')):
            os.remove(stale_video)
    except Exception as e:
        logging.warning("清理旧视频文件失败: %s", e)

    download_success = False
    try:
        download_success = download_videos_files(resolved_url, download_folder=download_folder) or False
    except Exception as e:
        logging.error("下载演示视频失败: %s", e)

    logging.info("发现视频网址 %s，尝试获取视频", resolved_url)
    video_paths = sorted(glob.glob(os.path.join(download_folder, '*.mp4')))
    videos = []
    for video in video_paths:
        try:
            videos.append(VideoFileClip(video))
        except Exception as e:
            logging.warning("加载演示视频 %s 失败: %s", video, e)

    if videos:
        logging.info("提取到 %d 个演示视频", len(videos))
    elif not download_success:
        logging.info("未成功获取任何演示视频")

    return videos

def _derive_arxiv_id_for_plan(paper_link):
    """从论文链接解析 arxiv_id, 供公式源码优先路径用; 失败/blog 等返回 None 走 LLM 兜底。"""
    if not paper_link:
        return None
    try:
        from env_setup import parse_arxiv_link as _pal
        aid = _pal(paper_link)
        return aid or None
    except Exception:
        return None


def run_pdf_to_video_pipeline(paper=None,pdf_file_path=None,demowebsite=None,en_title="",prefix="",target_duration=300):
    logging.info("开始程序")
    _clean_pipeline_cache()
    # 输入PDF文件路径
    if not pdf_file_path:
        pdf_file_path, demowebsite = get_inputs()
    # 如果是网络路径，下载到本地
    _pdf_source = pdf_file_path
    pdf_file_path = download_if_remote(pdf_file_path)
    # download_if_remote 以 -1 表示失败（与 generate_daily_arxiv_summary 的约定一致），
    # 不拦住会把 -1 当路径喂进 PDFProcessor
    if not pdf_file_path or pdf_file_path == -1:
        raise RuntimeError(f"无法下载或找到 PDF 文件: {_pdf_source}")
    logging.info("文件存在，开始处理PDF")
    # 创建PDF处理器实例
    pdf_processor = PDFProcessor(pdf_file_path)
    # 提取文本和图片
    logging.info("提取PDF文本")
    text = pdf_processor.extract_text()
    paper_abstract = ""
    if paper and getattr(paper, "abstract", None):
        paper_abstract = paper.abstract.strip()
    if not paper_abstract:
        paper_abstract = extract_abstract_from_text(text)
    images = process_pdf_images(pdf_processor)
    # 预绑定：下面的 try 可能在 ImageAgent() 就抛异常，而这两个名字在 except 之后仍会被读取
    paper_core_summary = text
    explanations = None
    # 尝试使用 qwen-vl 对图片做解释（如果可用）
    try:
        os.makedirs('./cache', exist_ok=True)
        image_agent = ImageAgent()
        # 构建 contexts：从 PDF 提取的 captions 可作为题注
        try:
            from src.extract_caption import extract_captions_from_pdf
        except Exception:
            try:
                from extract_caption import extract_captions_from_pdf
            except Exception:
                extract_captions_from_pdf = None
        contexts = None
        if extract_captions_from_pdf:
            try:
                contexts_dict = extract_captions_from_pdf(pdf_file_path)
                contexts = {}
                for k, v in contexts_dict.items():
                    m = re.search(r"(\d+)", k)
                    if m:
                        idx = int(m.group(1))
                        contexts[idx] = v
            except Exception:
                contexts = None

        # ---- 图片筛选（基于 LLM 打分） ----
        if len(images) > MAX_IMAGES_DEFAULT:
            logging.info(f"图片数量 {len(images)} 超过上限 {MAX_IMAGES_DEFAULT}，进行重要性筛选")
            try:
                captions_for_rating = []
                for img_idx in range(len(images)):
                    cap = contexts.get(img_idx + 1, f"Figure {img_idx + 1}") if contexts else f"Figure {img_idx + 1}"
                    captions_for_rating.append(cap)
                scores = rate_image_importance(captions_for_rating)
                images, contexts = _select_top_images_with_contexts(
                    images, scores, contexts, top_n=MAX_IMAGES_DEFAULT)
                logging.info(f"筛选后保留 {len(images)} 张图片")
            except Exception as e:
                logging.warning(f"图片筛选失败，使用前 {MAX_IMAGES_DEFAULT} 张: {e}")
                images = images[:MAX_IMAGES_DEFAULT]

        # 计算文字预算
        word_budget = compute_word_budget(target_duration, num_images=len(images))
        logging.info(f"文字预算: 总结{word_budget['summary']}字, 图片{word_budget['images']}字 (每张{word_budget['per_image']}字)")

        # 先用LLM总结文章核心内容，然后传递给图像解释
        logging.info("生成文章核心内容总结用于图像解释")
        try:
            paper_core_summary = generate_summary(text, word_budget=word_budget['summary'])
            logging.info("文章核心内容总结生成成功")
        except Exception as e:
            logging.warning(f"生成文章核心内容总结失败: {e}")
            paper_core_summary = text

        explanations = image_agent.explain_images(images, contexts=contexts, paper_abstract=paper_abstract, paper_text=paper_core_summary, per_image_budget=word_budget['per_image'])

        # 为图像解释添加上下文和过渡语句
        logging.info("为图像解释添加上下文和过渡语句")
        try:
            explanations = add_context_to_image_explanations(explanations)
            logging.info("上下文和过渡语句添加成功")
        except Exception as e:
            logging.warning(f"添加上下文和过渡语句失败: {e}")

        with open('./cache/image_explanations.json', 'w', encoding='utf-8') as f:
            json.dump(explanations, f, ensure_ascii=False, indent=2)
        logging.info("已保存图像解释到 ./cache/image_explanations.json")
    except Exception as e:
        logging.warning("调用 image_agent 解释图片失败: %s", e)
        word_budget = compute_word_budget(target_duration, num_images=len(images))

    logging.info("提取到 %d 张图片", len(images))

    videos = get_videoclips(text, demowebsite)
    # 生成摘要（优先使用结构化脚本，回退为普通摘要）
    logging.info("生成结构化视频脚本")
    structured_plan = {}
    try:
        _plan_aid = _derive_arxiv_id_for_plan(getattr(paper, 'link', None) if paper else None)
        structured_plan = generate_structured_video_plan(text, word_budget=word_budget['summary'], arxiv_id=_plan_aid)
        if structured_plan:
            summary = structured_plan_to_text(structured_plan)
            title = generate_video_title(text[:1000])
            logging.info("结构化脚本生成成功，使用5段式叙事")
            # 缓存 structured_plan 和 paper_text 供 Manim 模式使用
            try:
                import json as _json
                with open("./cache/structured_plan.json", "w", encoding="utf-8") as _f:
                    _json.dump(structured_plan, _f, ensure_ascii=False, indent=2)
                with open("./cache/paper_text.txt", "w", encoding="utf-8") as _f:
                    _f.write(text)
                logging.info("已缓存 structured_plan 和 paper_text")
            except Exception as _e:
                logging.warning("缓存 structured_plan 失败: %s", _e)
        else:
            raise ValueError("结构化脚本为空")
    except Exception as e:
        logging.warning(f"结构化脚本生成失败，回退为普通摘要: {e}")
        # 复用已生成的 paper_core_summary，避免重复 LLM 调用
        if paper_core_summary and paper_core_summary != text:
            summary = paper_core_summary
            title = generate_video_title(text[:1000])
            logging.info("复用 paper_core_summary 作为摘要，节省 LLM 调用")
        else:
            title, summary = call_llm(text, word_budget=word_budget['summary'])

    # 创建视频（将图像解释传入 VideoCreator，使每张图像可被讲解）
    logging.info("开始创建视频")
    image_explanations = None
    try:
        if os.path.exists('./cache/image_explanations.json'):
            with open('./cache/image_explanations.json', 'r', encoding='utf-8') as f:
                image_explanations = json.load(f)
        else:
            # 回退到内存里刚算出的解释（explanations 在上面的 try 里生成，失败时为 None）
            image_explanations = explanations
    except Exception as e:
        logging.warning("读取图像解释缓存失败，回退到内存结果: %s", e)
        image_explanations = explanations

    # 将结构化脚本传入 VideoCreator，用于语义匹配图文对应
    video_creator = VideoCreator(images, summary, videos, image_explanations=image_explanations, target_duration=target_duration, structured_plan=structured_plan)
    save_path = f"./output/{sanitize_filename(title, 'video')}.mp4"
    video_path = video_creator.create_video(save_path)
    if not video_path or not os.path.exists(video_path):
        raise RuntimeError(f"视频创建失败，输出文件不存在: {save_path}")
    try:
        generate_cover('./pic/1.png', title, video_path.replace(".mp4", ".png"))
    except Exception as e:
        logging.warning("封面生成失败，跳过封面: %s", e)
    logging.info("视频已成功创建，路径为: %s", video_path)
    #convert to absolute path
    video_path = os.path.abspath(video_path)
    logging.info("已完成本地视频生成，主流程不再自动上传。")
    logging.info("程序结束")
    return video_path

def call_llm(text, word_budget: int = 1000):
    with ThreadPoolExecutor() as executor:
        future_title = executor.submit(generate_video_title, text[:1000])
        future_summary = executor.submit(generate_summary, text, word_budget)
        title = future_title.result()
        summary = future_summary.result()
    if summary:
        logging.info("成功生成摘要")
    else:
        logging.warning("摘要为空")
    return title,summary

def call_llm_multithread(list_of_func_and_params):
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(func, param) for func, param in list_of_func_and_params]
        results = [future.result() for future in futures]
    return results

def get_inputs():
    pdf_file_path = input("请输入PDF文件的路径: ")
    logging.info(f"输入的PDF路径: {pdf_file_path}")
    demowebsite=input("请输入视频网址:")
    return pdf_file_path,demowebsite
def process_pdf_images(pdf_processor,cnt=None):
    """
    处理 PDF 文件中的图片。

    功能：
    - 如果未手动提取图片，则清空 `./pic` 文件夹中的所有图片文件，并从 PDF 文件中提取图片保存到该文件夹。
    - 如果已手动提取图片，则直接从 `./pic` 文件夹中加载图片。
    - 返回提取的图片列表，每个图片以 `PIL.Image` 对象的形式表示。

    参数：
    - pdf_processor: PDFProcessor 对象，用于处理 PDF 文件。

    返回值：
    - images (list): 包含提取图片的列表，每个图片为 `PIL.Image` 对象。
    """
    if not MANUALLY_EXTRACT_IMAGES:
        logging.info("提取PDF图片")
        #remove all the images in the pic folder
        files = glob.glob('./pic/*')
        for f in files:
            os.remove(f)
        pdf_processor.extract_images(cnt=cnt)
        images = glob.glob('./pic/*.png')
        # 按文件名中的数字排序
        images.sort(key=lambda x: int(re.findall(r'\d+', os.path.basename(x))[0]) if re.findall(r'\d+', os.path.basename(x)) else 0)
        logging.info(f"图片文件排序后的顺序: {[os.path.basename(img) for img in images]}")
        images = [Image.open(image) for image in images]
    else:
        images = glob.glob('./pic/*.png')
        # 同样进行排序
        images.sort(key=lambda x: int(re.findall(r'\d+', os.path.basename(x))[0]) if re.findall(r'\d+', os.path.basename(x)) else 0)
        logging.info(f"手动提取图片排序后的顺序: {[os.path.basename(img) for img in images]}")
        images = [Image.open(image) for image in images]
    return images

def _pdf_cache_meta_path(cache_path: str) -> str:
    """PDF 缓存的元数据路径，元数据里记录来源 URL，作为缓存键。"""
    return os.path.splitext(cache_path)[0] + ".meta.json"


def _read_pdf_cache_url(meta_path: str) -> str:
    """读取缓存 PDF 的来源 URL；元数据缺失或损坏时返回空串（视为未命中）。"""
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return ""
    return meta.get("url", "") if isinstance(meta, dict) else ""


def _write_pdf_cache_url(meta_path: str, url: str) -> None:
    """记录本次下载的来源 URL，供下次缓存命中判断。"""
    try:
        os.makedirs(os.path.dirname(meta_path) or ".", exist_ok=True)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"url": url}, f, ensure_ascii=False)
    except OSError as e:
        logging.warning("写入 PDF 缓存元数据失败: %s", e)


def download_if_remote(pdf_file_path):
    """
    检查并下载远程 PDF 文件。

    功能：
    - 如果给定的 PDF 文件路径是远程 URL，则下载该文件并缓存到 `./cache` 文件夹中。
    - 如果文件路径不是 URL，则直接返回。

    参数：
    - pdf_file_path (str): PDF 文件的路径或 URL。

    返回值：
    - pdf_file_path (str): 如果是远程文件，返回下载后的本地文件路径；否则返回原始路径。

    注意：
    - 下载的文件会保存为 `./cache/cached_pdf.pdf`。
    - 缓存命中要求「来源 URL 与上次下载一致」（记录在 `./cache/cached_pdf.meta.json`）
      且「文件不超过 1 小时」，避免多篇模式下后续论文误读第一篇的 PDF。
    """
    # 本地已存在的文件路径：直接返回，不下载（本地 PDF 入口的解耦点）。
    if pdf_file_path and not pdf_file_path.startswith("http") and os.path.isfile(pdf_file_path):
        return pdf_file_path
    if pdf_file_path.startswith("http"):
        # 缓存检测：缓存路径对所有论文是常量，只看文件新旧会让多篇模式的第 2 篇起
        # 全部读到第 1 篇的 PDF，因此必须「同一来源 URL」+「不超过 1 小时」双条件成立
        source_url = pdf_file_path
        cache_path = os.path.join("./cache", "cached_pdf.pdf")
        meta_path = _pdf_cache_meta_path(cache_path)
        if os.path.isfile(cache_path) and _read_pdf_cache_url(meta_path) == source_url:
            import time as _time
            age = _time.time() - os.path.getmtime(cache_path)
            if age < 3600:
                logging.info("复用缓存 PDF（%.0f 秒前下载，同一 URL）: %s", age, cache_path)
                return cache_path
        logging.info("下载PDF文件")
        def download_file(url, max_retries=3):
            """下载远程文件并保存到本地，带指数退避重试。"""
            import requests
            import time as _time
            file_name = os.path.join("./cache", "cached_pdf.pdf")
            os.makedirs(os.path.dirname(file_name), exist_ok=True)
            for attempt in range(max_retries):
                try:
                    response = requests.get(url, timeout=60)
                    response.raise_for_status()
                    with open(file_name, "wb") as f:
                        f.write(response.content)
                    return file_name
                except Exception as e:
                    wait = 2 ** attempt
                    logging.warning(f"PDF 下载失败 (第{attempt+1}次), {wait}s 后重试: {e}")
                    if attempt < max_retries - 1:
                        _time.sleep(wait)
                    else:
                        logging.error(f"PDF 下载最终失败: {e}")
                        raise
        pdf_file_path = download_file(source_url)
        _write_pdf_cache_url(meta_path, source_url)
        logging.info(f"下载完成，保存路径: {pdf_file_path}")
    # 检查文件是否存在
    if not os.path.isfile(pdf_file_path):
        logging.info("文件不存在，请检查路径。")
        return -1
    return pdf_file_path

def generate_daily_arxiv_summary(query="cs.RO", date=datetime.datetime.now().strftime(r"%Y-%m-%d"), max_papers=20, output_filename="./output/daily_summary.mp4",long_or_short="short",target_duration=300, paper_link=None, skip_main_video=False, blog_url=None, local_pdf_path=None):
    """
    为每天 arXiv 上的论文生成一个简短的日报性总结视频。
    参数：
    - query (str): arXiv 查询关键词，默认是 "cs.RO"（机器人学）。
    - date (str): 筛选论文的日期（格式：YYYY-MM-DD），默认是 None（不筛选）。
    - max_papers (int): 每日报告的最大论文数量，默认是 5。
    - output_filename (str): 生成的视频文件路径，默认是 "./output/daily_summary.mp4"。
    """
    logging.info("开始生成每日 arXiv 论文总结视频")
    _clean_pipeline_cache()
    _local_aid = None
    if local_pdf_path:
        import hashlib as _hashlib
        _local_aid = "local-" + _hashlib.md5(os.path.abspath(local_pdf_path).encode()).hexdigest()[:8]
    cn_titles = []
    if local_pdf_path:
        # 本地 PDF 模式: link 直接是本地 PDF 路径, 后续 download_if_remote 短路返回本地文件,
        # 复用标准 PDF 处理链 (PDFProcessor 提文本/图, 图像解释, 公式走 LLM 兜底)。
        if not os.path.isfile(local_pdf_path):
            raise RuntimeError(f"本地 PDF 不存在: {local_pdf_path}")
        # 标题: 优先从 PDF 文本启发式提取, 拿不到用文件名 stem; 摘要: 从正文前段提取。
        _local_title = ""
        _local_abstract = ""
        try:
            _lp_proc = PDFProcessor(local_pdf_path)
            _lp_text = _lp_proc.extract_text() or ""
            _local_abstract = extract_abstract_from_text(_lp_text)
            for _ln in (_lp_text.splitlines() if _lp_text else []):
                _ln = _ln.strip()
                if len(_ln) >= 8:
                    _local_title = _ln
                    break
        except Exception as _e:
            logging.warning("[local-pdf] PDF 文本预提取失败, 用文件名兜底: %s", _e)
        if not _local_title:
            _local_title = os.path.splitext(os.path.basename(local_pdf_path))[0]
        papers = [Paper(
            title=_local_title,
            authors=[],
            abstract=_local_abstract,
            link=local_pdf_path,  # 直接是本地路径, download_if_remote 会短路返回
            announced_date="",
            submitted_date="",
            comments="",
        )]
        logging.info(f"[local-pdf] 本地 PDF 入口: path={local_pdf_path} title={_local_title[:80]}")
    elif paper_link:
        # 直接按 arxiv link / id 拉单篇，跳过 HTML 搜索 + 日期过滤
        from env_setup import parse_arxiv_link, fetch_arxiv_by_id
        arxiv_id = parse_arxiv_link(paper_link)
        meta = fetch_arxiv_by_id(arxiv_id)
        submitted = meta.get("submitted_date") or meta.get("updated_date") or ""
        papers = [Paper(
            title=meta["title"],
            authors=[],
            abstract=meta.get("abstract", ""),
            link=meta["pdf_url"],  # 下载时 workflow 会 GET 这个 URL
            announced_date=submitted + "T00:00:00Z" if submitted else "",
            submitted_date=submitted + "T00:00:00Z" if submitted else "",
            comments="",
        )]
        logging.info(f"[paper-link] 精确拉取单篇: id={arxiv_id} title={meta['title'][:80]}")
    elif blog_url:
        # blog 模式: 抓 HTML / 图 / 视频 写到 cache, 构造伪 Paper 跳 PDF 流程
        try:
            from src.blog_pipeline import materialize_blog_as_paper_cache  # type: ignore
        except ImportError:
            from blog_pipeline import materialize_blog_as_paper_cache  # type: ignore
        blog_record = materialize_blog_as_paper_cache(blog_url)
        submitted = blog_record.get("published_date") or ""
        papers = [Paper(
            title=blog_record["title"] or blog_url,
            authors=[],
            abstract=(blog_record.get("description") or "")[:1000],
            link="",  # 空字符串 -> 后续 download_if_remote 跳过
            announced_date=submitted + "T00:00:00Z" if submitted else "",
            submitted_date=submitted + "T00:00:00Z" if submitted else "",
            comments="",
        )]
        logging.info(
            f"[blog-url] 抓取完成: title={(blog_record['title'] or '')[:80]} "
            f"images={len(blog_record.get('image_paths', []))} "
            f"clips={len(blog_record.get('clip_paths', []))}"
        )
    else:
        # 获取最新的论文
        papers = get_paper_from_arxiv(query=query)
        if papers is None:
            raise RuntimeError(f"拉取 arXiv 论文失败，query={query}")
        logging.info(f"找到 {len(papers)} 篇论文,正在筛选...")
        # 过滤日期
        papers = filter_papers_by_date(papers, date)
        # 限制论文数量
        if len(papers) > max_papers:
            papers = papers[:max_papers]
            logging.info(f"限制论文数量为 {max_papers} 篇")
    # 如果没有找到符合条件的论文，抛出异常
    if not papers:
        raise RuntimeError(f"未找到符合条件的论文，query={query}, date={date}, paper_link={paper_link}")
    else:
        logging.info(f"找到 {len(papers)} 篇论文")
        logging.info(f"日期: {date}")
        logging.info([paper.title for paper in papers])  # 使用 Paper 数据类的属性

    # 初始化成功生成的视频片段路径列表
    generated_part_paths = []
    processed_papers = []
    origin_titles = []
    summaries = []  # 每篇论文的中文摘要，用于上传平台文案
    paper_links = []
    project_links = []

    # ---- 时长预算 ----
    if len(papers) > 1:
        per_paper_duration = max(30, target_duration // len(papers))
    else:
        per_paper_duration = target_duration
    logging.info(f"目标总时长: {target_duration}秒, 每篇论文目标: {per_paper_duration}秒")

    for paper_idx, paper in enumerate(papers):
        logging.info(f"处理第 {paper_idx + 1} 篇论文: {paper.title}")
        # 每轮重置：word_budget 依赖本篇的图片数，沿用上一篇的值会按错误的图片数分配字数
        word_budget = None
        if blog_url:
            # blog 模式: 跳过 PDF 下载 + PDFProcessor, 用 cache/paper_text.txt + ./pic/*.png
            logging.info("[blog-url] 跳过 PDF 流程, 从 cache 读取文本/图片")
            pdf_file_path = None
            pdf_processor = None
            paper_text_path = "./cache/paper_text.txt"
            if os.path.exists(paper_text_path):
                with open(paper_text_path, "r", encoding="utf-8") as _bf:
                    text = _bf.read()
            else:
                text = (paper.abstract or paper.title or "")
            import glob as _glob
            images = sorted(
                _glob.glob("./pic/*.png"),
                key=lambda p: int(re.findall(r"\d+", os.path.basename(p))[0])
                if re.findall(r"\d+", os.path.basename(p)) else 0,
            )
            if long_or_short == "short" and len(images) > 2:
                images = images[:2]
        else:
            # 下载论文 PDF
            if local_pdf_path:
                # 本地 PDF: paper.link 即本地路径, 不做 abs->pdf 替换, 直接走短路
                pdf_url = paper.link
            else:
                pdf_url = paper.link.replace("abs", "pdf").split('v1')[0]
            pdf_file_path = download_if_remote(pdf_url)
            if not pdf_file_path or pdf_file_path == -1:
                logging.warning(f"无法下载或找到 PDF 文件: {pdf_url}")
                continue

            # 创建 PDF 处理器实例
            pdf_processor = PDFProcessor(pdf_file_path)

            # 提取文本和图片
            logging.info("提取 PDF 文本和图片")
            text = pdf_processor.extract_text()
            images = process_pdf_images(pdf_processor, cnt=2 if long_or_short == "short" else None)
        paper_abstract = paper.abstract.strip() if getattr(paper, "abstract", None) else extract_abstract_from_text(text)

        # 对提取到的图片尝试做图像解释并保存
        try:
            os.makedirs('./cache', exist_ok=True)
            image_agent = ImageAgent()
            # reuse extract_captions_from_pdf if available
            try:
                # 导入失败时 extract_captions_from_pdf 是 None（名字仍在 globals 里），必须判可调用
                contexts_dict = extract_captions_from_pdf(pdf_file_path) if (pdf_file_path and callable(extract_captions_from_pdf)) else {}
            except Exception as e:
                logging.warning("提取 PDF 题注失败，本篇图片将没有 caption: %s", e)
                contexts_dict = {}
            contexts = {}
            for k, v in (contexts_dict or {}).items():
                m = re.search(r"(\d+)", k)
                if m:
                    idx = int(m.group(1))
                    contexts[idx] = v

            # ---- 图片筛选（基于 LLM 打分） ----
            if len(images) > MAX_IMAGES_DEFAULT:
                logging.info(f"图片数量 {len(images)} 超过上限 {MAX_IMAGES_DEFAULT}，进行重要性筛选")
                try:
                    captions_for_rating = []
                    for img_idx in range(len(images)):
                        cap = contexts.get(img_idx + 1, f"Figure {img_idx + 1}") if contexts else f"Figure {img_idx + 1}"
                        captions_for_rating.append(cap)
                    scores = rate_image_importance(captions_for_rating)
                    images, contexts = _select_top_images_with_contexts(
                        images, scores, contexts, top_n=MAX_IMAGES_DEFAULT)
                    logging.info(f"筛选后保留 {len(images)} 张图片")
                except Exception as e:
                    logging.warning(f"图片筛选失败，使用前 {MAX_IMAGES_DEFAULT} 张: {e}")
                    images = images[:MAX_IMAGES_DEFAULT]

            # 计算文字预算
            word_budget = compute_word_budget(per_paper_duration, num_images=len(images))
            logging.info(f"文字预算: 总结{word_budget['summary']}字, 图片{word_budget['images']}字 (每张{word_budget['per_image']}字)")

            # 先用LLM总结文章核心内容，然后传递给图像解释
            logging.info("生成文章核心内容总结用于图像解释")
            try:
                paper_core_summary = generate_summary(text, word_budget=word_budget['summary'])
                logging.info("文章核心内容总结生成成功")
            except Exception as e:
                logging.warning(f"生成文章核心内容总结失败: {e}")
                paper_core_summary = text  # 如果失败，使用原始文本

            explanations = image_agent.explain_images(images, contexts=contexts, paper_abstract=paper_abstract, paper_text=paper_core_summary, per_image_budget=word_budget['per_image'])

            # 为图像解释添加上下文和过渡语句
            logging.info("为图像解释添加上下文和过渡语句")
            try:
                explanations = add_context_to_image_explanations(explanations)
                logging.info("上下文和过渡语句添加成功")
            except Exception as e:
                logging.warning(f"添加上下文和过渡语句失败: {e}")

            with open(f'./cache/image_explanations_part_{paper_idx+1}.json', 'w', encoding='utf-8') as f:
                json.dump(explanations, f, ensure_ascii=False, indent=2)
            # 同时更新主文件（Manim engine 读取此文件）
            with open('./cache/image_explanations.json', 'w', encoding='utf-8') as f:
                json.dump(explanations, f, ensure_ascii=False, indent=2)
            logging.info("已保存图像解释到 ./cache/image_explanations.json")
        except Exception as e:
            logging.warning("图像解释保存失败: %s", e)
        if not images:
            logging.warning(f"未提取到图片，跳过论文: {paper.title}")
            continue
        
        # 生成简短摘要（注入字数预算）
        logging.info("生成摘要")
        # word_budget 可能因图像解释 try 块提前失败而没算到（循环开头已重置为 None），兜底计算
        if word_budget is None:
            word_budget = compute_word_budget(per_paper_duration, num_images=len(images))
        structured_plan_part = {}
        if long_or_short == "short":
            demo_website, origin_title, short_summary, cn_title = call_llm_multithread(
                [
                    (get_paper_demo_website, text[:1000]),
                    (generate_origin_title, text[:200]),
                    (lambda t: generate_short_summary(t, word_budget=word_budget['summary']), f"{text[:5000]} {paper.comments}"),
                    (generate_video_title, text[:200]),
                ]
            )
        else:
            # long 模式：使用结构化脚本，保证 opening/intro/method/results 语义分组
            demo_website, origin_title, cn_title, structured_plan_part = call_llm_multithread(
                [
                    (get_paper_demo_website, text[:1000]),
                    (generate_origin_title, text[:200]),
                    (generate_video_title, text[:200]),
                    (lambda t: generate_structured_video_plan(
                        t, word_budget=word_budget['summary'],
                        arxiv_id=(_local_aid if local_pdf_path else (None if blog_url else _derive_arxiv_id_for_plan(getattr(paper, 'link', None))))), text),
                ]
            )
            logging.info("生成结构化视频脚本（long 模式）")
            try:
                pass  # structured_plan_part already computed above in parallel
                if structured_plan_part:
                    short_summary = structured_plan_to_text(structured_plan_part)
                    logging.info("结构化脚本生成成功，使用5段式叙事")
                else:
                    raise ValueError("结构化脚本为空")
            except Exception as e:
                logging.warning(f"结构化脚本生成失败，回退为普通摘要: {e}")
                structured_plan_part = {}
                short_summary = generate_summary(paper.comments + text, word_budget=word_budget['summary'])
        
        if not short_summary:
            logging.warning(f"摘要生成失败，跳过论文: {paper.title}")
            continue
        
        # 单篇论文时在循环内生成封面（循环只执行一次）
        # 多篇论文时封面在循环外、合并视频后统一生成，避免每次迭代覆盖同一文件
        # skip_main_video 模式仍然跑 generate_cover (Gemini → DashScope fallback),
        # 否则上传到 B站/小红书会缺真正的设计封面 (光靠 ffmpeg 抽首帧是低质截图)
        if len(papers) == 1:
            try:
                generate_cover('./pic/1.png', cn_title, output_filename.replace(".mp4", ".png"), paper_abstract=paper_abstract)
            except Exception as e:
                logging.warning("单篇论文封面生成失败，跳过封面: %s", e)
        # 限制图片数量为前两张
        if long_or_short == "short":
            images = images[:3]
            
        if skip_main_video:
            # manim-only 模式: 跳过 video_creator 视频合成, 仅记录元数据, 主视频由后续 manim 阶段产出
            # 缓存 paper_text + structured_plan 给 main.py 后续 Manim 段读取, 避免重新调 LLM 生成 (LLM 偶尔返回畸形 JSON)
            try:
                os.makedirs("./cache", exist_ok=True)
                with open("./cache/paper_text.txt", "w", encoding="utf-8") as _f:
                    _f.write(text)
                with open("./cache/structured_plan.json", "w", encoding="utf-8") as _f:
                    json.dump(structured_plan_part, _f, ensure_ascii=False, indent=2)
                logging.info("[skip_main_video] 已缓存 structured_plan + paper_text")
            except Exception as _e:
                logging.warning(f"[skip_main_video] 缓存失败: {_e}")
            logging.info(f"[skip_main_video] 跳过第 {paper_idx + 1} 篇论文的视频合成")
            part_save_path = f"./output/part_{paper_idx + 1}.mp4"
            generated_part_paths.append(part_save_path)
            processed_papers.append(paper)
            origin_titles.append(origin_title)
            cn_titles.append(cn_title)
            summaries.append(short_summary or "")
            paper_links.append((paper.link or "").strip())
            project_links.append("")
            continue

        # 为当前论文创建独立视频
        logging.info(f"创建第 {paper_idx + 1} 篇论文的视频片段")
        # 传入图像解释以便合成时对每张图片进行解读性讲解
        try:
            image_explanations_part = None
            cache_path = f'./cache/image_explanations_part_{paper_idx+1}.json'
            if os.path.exists(cache_path):
                with open(cache_path, 'r', encoding='utf-8') as f:
                    image_explanations_part = json.load(f)
        except Exception:
            image_explanations_part = None
        video_creator = VideoCreator(images, short_summary, video_clips=get_videoclips(text, demo_website), image_explanations=image_explanations_part, target_duration=per_paper_duration, structured_plan=structured_plan_part)
        part_save_path = f"./output/part_{paper_idx + 1}.mp4"
        part_video_path = video_creator.create_video(part_save_path)
        if not part_video_path or not os.path.exists(part_video_path):
            raise RuntimeError(f"视频片段生成失败: {part_save_path}")
        generated_part_paths.append(part_video_path)
        processed_papers.append(paper)
        origin_titles.append(origin_title)
        cn_titles.append(cn_title)
        summaries.append(short_summary or "")
        paper_links.append((paper.link or "").strip())
        # demo_website 主要用于抓取演示视频，缺少可靠校验时不默认当作项目链接对外发布，
        # 避免把数据集/机构主页误写进简介。
        project_links.append("")
    
    # 如果没有任何可用视频片段，抛出异常
    if not generated_part_paths:
        raise RuntimeError("未生成任何可用视频片段，无法创建视频")

    #如果只有一篇论文，重命名part1为论文名.mp4，并返回统一三元组
    if len(papers) == 1:
        part_video_path = generated_part_paths[0]
        date_str=datetime.datetime.now().strftime(r"%Y-%m-%d")
        title_for_filename = sanitize_filename(
            cn_titles[0] if cn_titles else "", "daily_summary")
        new_part_video_path = os.path.abspath(f"./output/{date_str}_{title_for_filename}.mp4")
        if skip_main_video:
            # manim-only 模式: part_video_path 是预定路径不存在, 跳过物理重命名, 仅返回字符串
            logging.info(f"[skip_main_video] 跳过物理重命名, 主视频路径 (待 manim 写入): {new_part_video_path}")
        else:
            if os.path.abspath(part_video_path) != new_part_video_path:
                os.replace(part_video_path, new_part_video_path)
            # 同步重命名封面文件，确保 cover_path = video_path.replace(".mp4", ".png") 能找到
            old_cover = os.path.abspath(output_filename.replace(".mp4", ".png"))
            new_cover = new_part_video_path.replace(".mp4", ".png")
            if os.path.exists(old_cover) and os.path.abspath(old_cover) != os.path.abspath(new_cover):
                os.replace(old_cover, new_cover)
                logging.info(f"封面已重命名: {old_cover} -> {new_cover}")
            if not os.path.exists(new_part_video_path):
                raise RuntimeError(f"单篇视频输出失败: {new_part_video_path}")
            logging.info(f"单篇视频已成功生成，路径为: {new_part_video_path}")
        # 单篇成功 → 记录到 published_papers.json (供 --discover 去重)
        try:
            _aid = locals().get('arxiv_id') or None
            if not _aid and paper_links:
                from env_setup import parse_arxiv_link as _pal
                try:
                    _aid = _pal(paper_links[0])
                except Exception:
                    _aid = None
            _record_published_safe(_aid, (cn_titles[0] if cn_titles else (origin_titles[0] if origin_titles else '')), new_part_video_path, date)
        except Exception as _e:
            logging.warning('[discovery-hook] single record failed: %s', _e)
        return new_part_video_path, origin_titles, cn_titles, summaries, paper_links, project_links

    # 合并所有论文的视频片段（至少一段）
    video_clips = [VideoFileClip(path) for path in generated_part_paths]
    
    logging.info("合并所有论文的视频片段")
    # 将论文的标题以字幕的形式，显示在每段视频的最上方
    title_clips = [
        TextClip(text=_.title, font_size=25,
                 font=FONT_PATH,
                 size=(1920, 1080),
                 color='white',
                 text_align='center', 
                 vertical_align='top',
                 stroke_color='black', 
                 stroke_width=3, 
                duration=video_clips[idx].duration) for idx, _ in enumerate(processed_papers)]
    final_tiles = concatenate_videoclips(title_clips, method="compose")
    final_video = concatenate_videoclips(video_clips, method="compose")
    final_video = CompositeVideoClip([final_video, final_tiles])
    final_video.write_videofile(output_filename, fps=24, codec='libx264', preset='medium')

    # 多篇论文合并完成后，统一生成日报封面（避免循环内反复覆盖）
    try:
        generate_cover('./pic/1.png', "Arxiv具身日报" + str(date), output_filename.replace(".mp4", ".png"))
    except Exception as e:
        logging.warning("日报封面生成失败，跳过封面: %s", e)

    # 转换为绝对路径
    final_video_path = os.path.abspath(output_filename)
    if not os.path.exists(final_video_path):
        raise RuntimeError(f"日报视频输出失败: {final_video_path}")
    logging.info(f"日报视频已成功生成，路径为: {final_video_path}")
    return final_video_path, origin_titles, cn_titles, summaries, paper_links, project_links


# ---- discovery state hook (added by --discover feature) ----
def _record_published_safe(arxiv_id, title, video_path, date_str=None):
    """出片成功后调 paper_discovery._append_published；任何异常不阻断主流程。"""
    if not arxiv_id:
        return
    try:
        from src.paper_discovery import _append_published, _state_path
        import datetime as _dt
        _append_published(_state_path(), {
            'arxiv_id': arxiv_id,
            'title': title or '',
            'date': (date_str or _dt.datetime.now().strftime('%Y-%m-%d')),
            'video_path': video_path or '',
        })
    except Exception as _e:
        logging.warning('[discovery-hook] append_published failed: %s', _e)
