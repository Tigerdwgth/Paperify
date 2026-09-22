import base64
import io
import logging
import os
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

try:  # DashScope image synthesis API
    from dashscope import ImageSynthesis
except Exception:  # pragma: no cover
    ImageSynthesis = None

from src.config import FONT_PATH

LOGGER = logging.getLogger(__name__)


def _save_image_bytes(image_bytes: bytes, output_path: str) -> Image.Image:
    os.makedirs(os.path.dirname(output_path) or "./", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_bytes)
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _create_placeholder(prompt: str, output_path: str) -> Image.Image:
    """Generate a simple placeholder image when synthesis fails."""
    LOGGER.warning("回退到占位图：%s", prompt[:60])
    image = Image.new("RGB", (1920, 1080), color=(18, 24, 38))
    draw = ImageDraw.Draw(image)
    text = "AI Illustration\n" + (prompt[:120] if prompt else "概念示意图")
    try:
        font = ImageFont.truetype(FONT_PATH, 48)
    except Exception:
        font = ImageFont.load_default()
    w, h = draw.multiline_textsize(text, font=font, spacing=12)
    draw.multiline_text(
        ((1920 - w) / 2, (1080 - h) / 2),
        text,
        fill=(230, 230, 230),
        font=font,
        align="center",
        spacing=12,
    )
    os.makedirs(os.path.dirname(output_path) or "./", exist_ok=True)
    image.save(output_path)
    return image


def generate_ai_illustration(prompt: str, output_path: str = "./pic/ai_intro.png", size: str = "1024*768") -> Image.Image:
    """Call DashScope image synthesis (wanx) to create an illustration for the intro segment."""
    if not prompt:
        return _create_placeholder("缺少图像提示", output_path)

    if ImageSynthesis is None:
        LOGGER.error("DashScope ImageSynthesis SDK 不可用，使用占位图")
        return _create_placeholder(prompt, output_path)

    try:
        response = ImageSynthesis.call(
            model="wanx-v1",
            prompt=prompt,
            size=size,
            n=1,
        )
        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(getattr(response, "message", "DashScope image synthesis failed"))

        result = (response.output or {}).get("results", [{}])[0]
        image_bytes: Optional[bytes] = None
        if result.get("image_base64"):
            image_bytes = base64.b64decode(result["image_base64"])
        elif result.get("url"):
            image_bytes = requests.get(result["url"], timeout=30).content

        if not image_bytes:
            raise RuntimeError("未获得可用的图像数据")

        return _save_image_bytes(image_bytes, output_path)
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.error("调用 DashScope 图像生成失败: %s", exc)
        return _create_placeholder(prompt, output_path)
